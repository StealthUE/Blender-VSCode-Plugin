"""Placing objects: on the ground (or a print bed), centred, origin moved, turned flat for printing.

    vsblender.place_on_ground(objects)          lowest evaluated point to z = 0
    vsblender.center_on_origin(objects)         bounds centre to x = y = 0
    vsblender.set_origin(obj, "base")           origin to the bottom centre (game assets), mesh kept in place
    vsblender.orient_flat(obj)                  largest flat face down (printing)
    vsblender.stats(obj)                        triangles, size, volume, shells, closed (scene units and mm)

All use the evaluated mesh (after modifiers), not the bounding box corners, which overestimate the
extent of a rotated object. They call view_layer.update() first, because matrix_world is stale
right after changing location or rotation inside a script.
"""
from __future__ import annotations

import math

import numpy as np
from mathutils import Matrix, Vector

import bpy

from . import meshdata, units as units_mod


def _list(objects) -> list:
    if objects is None:
        return []
    if isinstance(objects, bpy.types.Object):
        return [objects]
    if isinstance(objects, str):
        ob = bpy.data.objects.get(objects)
        if ob is None:
            raise KeyError(f"no object named {objects!r}")
        return [ob]
    out = []
    for item in objects:
        out += _list(item)
    return out


def _roots(objects: list) -> list:
    """The objects whose parents are not also in the list (moving those moves their children)."""
    names = {o.name for o in objects}
    out = []
    for ob in objects:
        p = ob.parent
        nested = False
        while p is not None:
            if p.name in names:
                nested = True
                break
            p = p.parent
        if not nested:
            out.append(ob)
    return out


def _points(objects: list, deps) -> np.ndarray:
    pts = []
    for ob in objects:
        if ob.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
            arr = meshdata.object_arrays(ob, deps)
            if len(arr.verts):
                pts.append(arr.verts)
        else:
            pts.append(np.array([ob.matrix_world.translation[:]]))
    if not pts:
        raise ValueError("no geometry in the given objects")
    return np.vstack(pts)


def _shift(objects: list, delta) -> None:
    move = Matrix.Translation(Vector(delta))
    for ob in _roots(objects):
        ob.matrix_world = move @ ob.matrix_world


def place_on_ground(objects, z: float = 0.0, as_group: bool = True) -> float:
    """Move objects up or down so their lowest evaluated point is at z. Returns the shift (scene units).

    as_group keeps their relative positions; as_group=False drops each one separately (returns the
    last shift).
    """
    obs = _list(objects)
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    shift = 0.0
    groups = [obs] if as_group else [[o] for o in obs]
    for group in groups:
        low = float(_points(group, deps)[:, 2].min())
        shift = float(z) - low
        _shift(group, (0.0, 0.0, shift))
    bpy.context.view_layer.update()
    return shift


place_on_bed = place_on_ground


def center_on_origin(objects, axes: str = "xy", as_group: bool = True) -> Vector:
    """Move objects so the centre of their evaluated bounds is at 0 on the given axes. Returns the shift."""
    obs = _list(objects)
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    groups = [obs] if as_group else [[o] for o in obs]
    delta = Vector((0, 0, 0))
    for group in groups:
        pts = _points(group, deps)
        centre = (pts.min(axis=0) + pts.max(axis=0)) / 2
        delta = Vector([-centre[i] if "xyz"[i] in axes.lower() else 0.0 for i in range(3)])
        _shift(group, delta)
    bpy.context.view_layer.update()
    return delta


center_on_bed = center_on_origin


def set_origin(obj, where="base") -> Vector:
    """Move an object's origin without moving its geometry: base (bottom centre), center (bounds
    centre), bounds_min, or a world point (x, y, z). Children stay where they are. Returns the new origin."""
    ob = _list(obj)[0]
    if ob.type != "MESH":
        raise ValueError(f"{ob.name} is a {ob.type}; set_origin moves mesh data")
    if ob.data.users > 1:
        raise ValueError(f"{ob.name}'s mesh is shared by {ob.data.users} objects; moving its data would move them too")
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    pts = _points([ob], deps)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    if isinstance(where, str):
        key = where.lower()
        if key == "base":
            target = Vector(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]))
        elif key == "center":
            target = Vector((lo + hi) / 2)
        elif key == "bounds_min":
            target = Vector(lo)
        else:
            raise ValueError("where must be base, center, bounds_min or a point")
    else:
        target = Vector(where)
    children = {c: c.matrix_world.copy() for c in ob.children}
    world = ob.matrix_world.copy()
    local_target = world.inverted_safe() @ target
    ob.data.transform(Matrix.Translation(-local_target))
    ob.matrix_world = world @ Matrix.Translation(local_target)
    bpy.context.view_layer.update()
    for child, matrix in children.items():
        child.matrix_world = matrix
    ob.data.update()
    return target


def _hull_normals(points: np.ndarray, limit: int = 12) -> list:
    """Largest flat faces of the convex hull: [(unit normal, area)], biggest first."""
    import bmesh

    bm = bmesh.new()
    try:
        step = max(1, len(points) // 20000)
        for p in points[::step]:
            bm.verts.new(p)
        bmesh.ops.convex_hull(bm, input=bm.verts)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        groups = {}
        for f in bm.faces:
            n = f.normal.normalized()
            key = tuple(np.round(np.array(n), 3))
            entry = groups.setdefault(key, [Vector((0, 0, 0)), 0.0])
            area = f.calc_area()
            entry[0] += n * area
            entry[1] += area
    finally:
        bm.free()
    out = [(entry[0].normalized(), entry[1]) for entry in groups.values() if entry[1] > 0]
    out.sort(key=lambda item: -item[1])
    return out[:limit]


def _overhang_area(arrays: meshdata.MeshArrays, rot: np.ndarray, max_deg: float, layer: float) -> tuple:
    normals, areas = meshdata.tri_normals_areas(arrays)
    n = normals @ rot.T
    v = arrays.verts @ rot.T
    zmin = v[:, 2].min()
    tri_z = v[arrays.tris][:, :, 2]
    on_bed = (tri_z.max(axis=1) <= zmin + layer) & (n[:, 2] < -0.98)
    over = (n[:, 2] < -math.sin(math.radians(90.0 - max_deg))) & ~on_bed
    return float(areas[over].sum()), float(areas[on_bed].sum())


def orient_flat(obj, method: str = "largest_face", max_overhang_deg: float = 45.0) -> dict:
    """Turn an object for printing and put it on the ground.

    largest_face: the biggest flat face of its convex hull goes down. min_overhang: of the six axis
    directions and the largest hull faces, the one that leaves the least overhanging area.
    Returns the rotation applied (degrees), bed contact and overhang area (scene units squared).
    """
    ob = _list(obj)[0]
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    arrays = meshdata.object_arrays(ob, deps)
    if not len(arrays.tris):
        raise ValueError(f"{ob.name} has no faces")
    down = Vector((0, 0, -1))
    candidates = [n for n, _a in _hull_normals(arrays.verts)]
    if method == "min_overhang":
        candidates += [Vector(v) for v in ((0, 0, -1), (0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0))]
    elif method != "largest_face":
        raise ValueError("method must be largest_face or min_overhang")
    lo, hi = arrays.bounds()
    layer = max(float(np.linalg.norm(hi - lo)) * 1e-3, 1e-9)
    best = None
    for n in candidates[: (1 if method == "largest_face" else len(candidates))]:
        q = n.rotation_difference(down)
        rot = np.array(q.to_matrix())
        over, contact = _overhang_area(arrays, rot, max_overhang_deg, layer)
        score = (over, -contact) if method == "min_overhang" else (0.0, -contact)
        if best is None or score < best[0]:
            best = (score, q, over, contact)
    _score, q, over, contact = best
    centre = Vector((lo + hi) / 2)
    turn = Matrix.Translation(centre) @ q.to_matrix().to_4x4() @ Matrix.Translation(-centre)
    for root in _roots([ob]):
        root.matrix_world = turn @ root.matrix_world
    place_on_ground([ob])
    euler = q.to_euler("XYZ")
    return {"rotation_deg": [round(math.degrees(a), 3) for a in euler], "contact_area": round(contact, 6),
            "overhang_area": round(over, 6), "units": units_mod.label()}


def stats(obj) -> dict:
    """Triangles, size, volume, area, shells and whether the evaluated mesh is closed."""
    ob = _list(obj)[0]
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    arrays = meshdata.object_arrays(ob, deps)
    lo, hi = arrays.bounds()
    u = units_mod.units()
    size = hi - lo
    tol = max(float(np.linalg.norm(size)) * 1e-7, 1e-12)
    st = meshdata.mesh_stats(arrays, tol)
    return {
        "object": ob.name, "tris": st["tris"], "size": [round(float(v), 6) for v in size],
        "size_mm": [round(float(v) * u["bu_to_mm"], 4) for v in size], "volume": st["volume"],
        "volume_mm3": st["volume"] * u["bu_to_mm"] ** 3, "area": st["area"], "shells": st["shells"],
        "closed": st["watertight"], "open_edges": st["open_edges"], "non_manifold_edges": st["non_manifold_edges"],
        "units": u["label"],
    }

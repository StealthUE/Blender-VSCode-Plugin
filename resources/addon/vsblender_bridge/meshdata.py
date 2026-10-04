"""Triangle meshes as numpy arrays: read from objects or files, measure, check, write.

    arrays = object_arrays(obj, depsgraph)        evaluated, world space, modifiers applied
    welded, remap = weld(arrays, tol)
    edge_stats(welded)                            open edges, non-manifold edges, flipped neighbours
    components(welded)                            shell label per triangle
    volume_area(welded)                           signed volume and area
    read_stl / read_obj / read_3mf / read_mesh_file(path)
    write_stl / write_obj / write_3mf

Nothing here changes the scene. bpy is only needed for object_arrays, to_mesh and from_bmesh.
"""
from __future__ import annotations

import io
import math
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

import numpy as np


class MeshArrays:
    """verts (V, 3) float64, tris (T, 3) int64. Optional per-triangle face (source polygon) and material."""

    __slots__ = ("verts", "tris", "face", "material", "name")

    def __init__(self, verts, tris, face=None, material=None, name: str = ""):
        self.verts = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
        self.tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        self.face = None if face is None else np.asarray(face, dtype=np.int64)
        self.material = None if material is None else np.asarray(material, dtype=np.int64)
        self.name = name

    def __len__(self) -> int:
        return len(self.tris)

    def copy(self) -> "MeshArrays":
        return MeshArrays(self.verts.copy(), self.tris.copy(),
                          None if self.face is None else self.face.copy(),
                          None if self.material is None else self.material.copy(), self.name)

    def bounds(self) -> tuple:
        if not len(self.verts):
            return np.zeros(3), np.zeros(3)
        used = self.verts[np.unique(self.tris)] if len(self.tris) else self.verts
        return used.min(axis=0), used.max(axis=0)

    def transformed(self, matrix) -> "MeshArrays":
        """Apply a 4x4 matrix (mathutils or nested lists). Flips winding for a mirroring matrix."""
        m = np.asarray([list(row) for row in matrix], dtype=np.float64)
        verts = self.verts @ m[:3, :3].T + m[:3, 3]
        tris = self.tris[:, ::-1].copy() if np.linalg.det(m[:3, :3]) < 0 else self.tris
        return MeshArrays(verts, tris, self.face, self.material, self.name)

    def scaled(self, factor: float) -> "MeshArrays":
        return MeshArrays(self.verts * float(factor), self.tris, self.face, self.material, self.name)

    def moved(self, offset) -> "MeshArrays":
        return MeshArrays(self.verts + np.asarray(offset, dtype=np.float64), self.tris, self.face, self.material, self.name)


def concat(parts) -> MeshArrays:
    parts = [p for p in parts if p is not None and len(p.verts)]
    if not parts:
        return MeshArrays(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    verts, tris, mats = [], [], []
    base = 0
    for p in parts:
        verts.append(p.verts)
        tris.append(p.tris + base)
        mats.append(p.material if p.material is not None else np.zeros(len(p.tris), dtype=np.int64))
        base += len(p.verts)
    return MeshArrays(np.vstack(verts), np.vstack(tris), material=np.concatenate(mats), name=parts[0].name)


# ----------------------------------------------------------------------------- from Blender
def _mesh_arrays(mesh) -> MeshArrays:
    mesh.calc_loop_triangles()
    nv = len(mesh.vertices)
    nt = len(mesh.loop_triangles)
    verts = np.empty(nv * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)
    tris = np.empty(nt * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", tris)
    face = np.empty(nt, dtype=np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", face)
    material = np.zeros(nt, dtype=np.int64)
    if len(mesh.polygons):
        poly_mat = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("material_index", poly_mat)
        material = poly_mat[face].astype(np.int64)
    return MeshArrays(verts.astype(np.float64), tris.astype(np.int64), face.astype(np.int64), material)


def object_arrays(ob, depsgraph=None, world: bool = True, instances: bool = True, max_instances: int = 10000,
                  warnings: list | None = None) -> MeshArrays:
    """The evaluated mesh of an object (modifiers, geometry nodes, curves and text converted), as triangles.

    world: in world space, with the winding flipped when the object is mirrored (negative scale), so
    normals still point out. instances: include geometry-node, particle and collection instances
    whose parent is this object (up to max_instances).
    """
    import bpy

    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    eo = ob.evaluated_get(depsgraph)
    parts = []
    mesh = None
    try:
        mesh = eo.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    except RuntimeError:
        mesh = None
    try:
        if mesh is not None and len(mesh.polygons):
            arr = _mesh_arrays(mesh)
            arr.name = ob.name
            parts.append(arr.transformed(eo.matrix_world) if world else arr)
    finally:
        if mesh is not None:
            eo.to_mesh_clear()
    if instances:
        count = 0
        inverse = None if world else eo.matrix_world.inverted_safe()
        for inst in depsgraph.object_instances:
            if not inst.is_instance:
                continue
            parent = inst.parent
            if parent is None or getattr(parent, "original", parent) != ob:
                continue
            if count >= max_instances:
                if warnings is not None:
                    warnings.append(f"{ob.name}: more than {max_instances} instances; the rest are left out")
                break
            data = getattr(inst.object, "data", None)
            if not hasattr(data, "loop_triangles"):
                continue
            try:
                arr = _mesh_arrays(data)
            except Exception:
                continue
            matrix = inst.matrix_world.copy()
            if inverse is not None:
                matrix = inverse @ matrix
            parts.append(arr.transformed(matrix))
            count += 1
    out = concat(parts)
    out.name = ob.name
    if len(parts) == 1:
        out.face = parts[0].face
    return out


def from_bmesh(bm) -> MeshArrays:
    """Triangles of a BMesh, without changing it."""
    bm.verts.index_update()
    verts = np.array([v.co[:] for v in bm.verts], dtype=np.float64).reshape(-1, 3)
    loops = bm.calc_loop_triangles()
    tris = np.array([[l.vert.index for l in tri] for tri in loops], dtype=np.int64).reshape(-1, 3)
    face = np.array([tri[0].face.index for tri in loops], dtype=np.int64) if loops else np.zeros(0, dtype=np.int64)
    material = np.array([tri[0].face.material_index for tri in loops], dtype=np.int64) if loops else np.zeros(0, dtype=np.int64)
    return MeshArrays(verts, tris, face, material)


def to_mesh(arrays: MeshArrays, name: str):
    """A new bpy Mesh from triangle arrays (material_index set when the arrays carry one)."""
    import bpy

    me = bpy.data.meshes.new(name)
    nv, nt = len(arrays.verts), len(arrays.tris)
    try:
        me.vertices.add(nv)
        me.vertices.foreach_set("co", arrays.verts.astype(np.float32).ravel())
        me.loops.add(nt * 3)
        me.loops.foreach_set("vertex_index", arrays.tris.astype(np.int32).ravel())
        me.polygons.add(nt)
        me.polygons.foreach_set("loop_start", np.arange(0, nt * 3, 3, dtype=np.int32))
        if arrays.material is not None and len(arrays.material) == nt:
            me.polygons.foreach_set("material_index", arrays.material.astype(np.int32))
        me.update(calc_edges=True)
        # New faces are smooth by default (4.1+): averaged normals over triangulated flats render as streaks.
        if hasattr(me, "shade_flat"):
            me.shade_flat()
    except Exception:
        bpy.data.meshes.remove(me)
        me = bpy.data.meshes.new(name)
        me.from_pydata(arrays.verts.tolist(), [], arrays.tris.tolist())
        if arrays.material is not None and len(arrays.material) == len(me.polygons):
            me.polygons.foreach_set("material_index", arrays.material.astype(np.int32))
        me.update()
    return me


# ----------------------------------------------------------------------------- topology
def weld(arrays: MeshArrays, tol: float) -> tuple:
    """Merge vertices closer than about tol (quantised to a grid of tol), drop unused ones.

    Returns (welded arrays, remap from old vertex index to new). Triangles that collapse keep their
    repeated indices so callers can count them as degenerate.
    """
    v = arrays.verts
    if not len(v):
        return arrays.copy(), np.zeros(0, dtype=np.int64)
    tol = max(float(tol), 1e-12)
    keys = np.round(v / tol).astype(np.int64)
    _uniq, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    verts = v[first]
    tris = inverse[arrays.tris]
    return MeshArrays(verts, tris, arrays.face, arrays.material, arrays.name), inverse


def _edges(tris):
    return np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])


def edge_stats(arrays: MeshArrays) -> dict:
    """Open (boundary) edges, non-manifold edges (shared by 3+ triangles), and inconsistent winding.

    Run on welded arrays. Returns counts plus up to 2000 sample edge midpoints for each problem, and
    per-triangle flags (tri_open, tri_nonmanifold, tri_flipped) for highlighting.
    """
    tris = arrays.tris
    nv = max(1, len(arrays.verts))
    ok = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    good = tris[ok]
    idx = np.nonzero(ok)[0]
    e = _edges(good)
    owner = np.concatenate([idx, idx, idx])
    und = np.sort(e, axis=1)
    key = und[:, 0] * nv + und[:, 1]
    _u, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    per = counts[inv]
    dkey = e[:, 0] * nv + e[:, 1]
    _du, dinv, dcounts = np.unique(dkey, return_inverse=True, return_counts=True)
    dper = dcounts[dinv.reshape(-1)]
    open_mask = per == 1
    nm_mask = per > 2
    flip_mask = (per == 2) & (dper > 1)
    out = {
        "edges": int(len(counts)),
        "open_edges": int((counts == 1).sum()),
        "non_manifold_edges": int((counts > 2).sum()),
        "flipped_edges": int(len(np.unique(key[flip_mask]))),
        "degenerate_tris": int((~ok).sum()),
    }
    t = len(tris)
    flags = {}
    for name, mask in (("tri_open", open_mask), ("tri_nonmanifold", nm_mask), ("tri_flipped", flip_mask)):
        f = np.zeros(t, dtype=bool)
        f[owner[mask]] = True
        flags[name] = f
    out["flags"] = flags
    mids = (arrays.verts[e[:, 0]] + arrays.verts[e[:, 1]]) / 2
    out["at"] = {
        "open_edges": _sample(mids[open_mask]),
        "non_manifold_edges": _sample(mids[nm_mask]),
        "flipped_edges": _sample(mids[flip_mask]),
    }
    if out["open_edges"]:
        out["holes"] = _count_loops(und[open_mask])
    return out


def _sample(points, k: int = 2000) -> np.ndarray:
    if len(points) <= k:
        return points
    step = len(points) / k
    return points[(np.arange(k) * step).astype(int)]


def _count_loops(edges) -> int:
    """Connected groups of boundary edges: the number of holes."""
    if not len(edges):
        return 0
    verts, inv = np.unique(edges.ravel(), return_inverse=True)
    pairs = inv.reshape(-1, 2)
    labels = _union(len(verts), pairs)
    return int(len(np.unique(labels)))


def _union(n: int, pairs) -> np.ndarray:
    """Connected-component labels of n nodes linked by pairs (min-label propagation with pointer jumping)."""
    parent = np.arange(n, dtype=np.int64)
    if not len(pairs):
        return parent
    a, b = pairs[:, 0], pairs[:, 1]
    for _ in range(200):
        pa, pb = parent[a], parent[b]
        low = np.minimum(pa, pb)
        new = parent.copy()
        np.minimum.at(new, pa, low)
        np.minimum.at(new, pb, low)
        while True:
            jumped = new[new]
            if np.array_equal(jumped, new):
                break
            new = jumped
        if np.array_equal(new, parent):
            break
        parent = new
    return parent


def components(arrays: MeshArrays) -> np.ndarray:
    """Shell label (0..k-1) per triangle: triangles connected through shared vertices."""
    tris = arrays.tris
    if not len(tris):
        return np.zeros(0, dtype=np.int64)
    pairs = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]]])
    labels = _union(len(arrays.verts), pairs)
    _u, shell = np.unique(labels[tris[:, 0]], return_inverse=True)
    return shell.reshape(-1)


def tri_normals_areas(arrays: MeshArrays) -> tuple:
    v = arrays.verts
    t = arrays.tris
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    cross = np.cross(b - a, c - a)
    length = np.linalg.norm(cross, axis=1)
    areas = 0.5 * length
    normals = np.divide(cross, length[:, None], out=np.zeros_like(cross), where=length[:, None] > 0)
    return normals, areas


def volume_area(arrays: MeshArrays, labels=None) -> dict:
    """Signed volume (positive when normals point out) and area, in total and per shell."""
    v = arrays.verts
    t = arrays.tris
    if not len(t):
        return {"volume": 0.0, "area": 0.0, "shell_volume": np.zeros(0), "shell_area": np.zeros(0)}
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    # Relative to the centre of the bounds, so large offsets do not cost precision.
    centre = (v.min(axis=0) + v.max(axis=0)) / 2
    a, b, c = a - centre, b - centre, c - centre
    vol = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0
    _n, areas = tri_normals_areas(arrays)
    out = {"volume": float(vol.sum()), "area": float(areas.sum())}
    if labels is not None and len(labels):
        k = int(labels.max()) + 1
        out["shell_volume"] = np.bincount(labels, weights=vol, minlength=k)
        out["shell_area"] = np.bincount(labels, weights=areas, minlength=k)
    return out


# ----------------------------------------------------------------------------- reading files
_STL_DTYPE = np.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def read_stl(path: str, weld_tol: float | None = 1e-6) -> MeshArrays:
    """Binary or ASCII STL, in the file's units (usually mm). Vertices are welded when weld_tol is set."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(84)
    binary = False
    if len(head) == 84:
        count = struct.unpack("<I", head[80:84])[0]
        binary = size == 84 + 50 * count
    if binary:
        data = np.fromfile(path, dtype=_STL_DTYPE, count=count, offset=84)
        corners = data["v"].astype(np.float64).reshape(-1, 3)
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
        if not nums:
            raise ValueError(f"{path} is neither a binary nor an ASCII STL")
        corners = np.array(nums, dtype=np.float64)
    tris = np.arange(len(corners), dtype=np.int64).reshape(-1, 3)
    arrays = MeshArrays(corners, tris, name=os.path.splitext(os.path.basename(path))[0])
    if weld_tol:
        arrays, _remap = weld(arrays, weld_tol)
    return arrays


def read_obj(path: str) -> MeshArrays:
    """Vertices and faces of an OBJ (polygons fan-triangulated). Materials, UVs and normals are ignored."""
    verts, tris = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("f "):
                idx = []
                for token in line.split()[1:]:
                    i = int(token.split("/")[0])
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
    if not tris:
        raise ValueError(f"{path} has no faces")
    return MeshArrays(verts, tris, name=os.path.splitext(os.path.basename(path))[0])


_UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _matrix_3mf(text: str | None) -> np.ndarray:
    m = np.identity(4)
    if not text:
        return m
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        return m
    # 3MF: row vectors, p' = p * M with M 4x3 given row by row.
    m3 = np.array(v, dtype=np.float64).reshape(4, 3)
    m[:3, :3] = m3[:3, :].T
    m[:3, 3] = m3[3, :]
    return m


def read_3mf(path: str) -> list:
    """Every build item of a 3MF as [(name, MeshArrays in mm)]. Components and item transforms are applied."""
    with zipfile.ZipFile(path) as archive:
        model_names = [n for n in archive.namelist() if n.lower().endswith(".model")]
        if not model_names:
            raise ValueError(f"{path} has no 3D model part")
        main = next((n for n in model_names if n.lower().endswith("3d/3dmodel.model")), model_names[0])
        roots = {n: ET.fromstring(archive.read(n)) for n in model_names}
    root = roots[main]
    factor = _UNIT_MM.get(root.get("unit", "millimeter"), 1.0)
    objects = {}
    for model_name, model in roots.items():
        for res in model.iter():
            if _local(res.tag) != "object":
                continue
            oid = res.get("id")
            mesh_el = next((c for c in res if _local(c.tag) == "mesh"), None)
            comps = next((c for c in res if _local(c.tag) == "components"), None)
            entry = {"name": res.get("name") or f"object {oid}", "mesh": None, "components": []}
            if mesh_el is not None:
                verts, tris = [], []
                for el in mesh_el.iter():
                    tag = _local(el.tag)
                    if tag == "vertex":
                        verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                    elif tag == "triangle":
                        tris.append((int(el.get("v1")), int(el.get("v2")), int(el.get("v3"))))
                entry["mesh"] = MeshArrays(verts, tris)
            if comps is not None:
                for comp in comps:
                    if _local(comp.tag) == "component":
                        entry["components"].append((comp.get("objectid"), _matrix_3mf(comp.get("transform"))))
            key = oid if model_name == main else f"{model_name}#{oid}"
            objects.setdefault(oid, entry)
            objects[key] = entry

    def flatten(oid, matrix, depth=0):
        entry = objects.get(oid)
        if entry is None or depth > 16:
            return []
        out = []
        if entry["mesh"] is not None:
            out.append(entry["mesh"].transformed(matrix))
        for child, m in entry["components"]:
            out += flatten(child, matrix @ m, depth + 1)
        return out

    items = []
    build = next((c for c in root if _local(c.tag) == "build"), None)
    for item in (build if build is not None else []):
        if _local(item.tag) != "item":
            continue
        oid = item.get("objectid")
        parts = flatten(oid, _matrix_3mf(item.get("transform")))
        if parts:
            arrays = concat(parts).scaled(factor)
            arrays.name = objects.get(oid, {}).get("name", oid)
            items.append((arrays.name, arrays))
    if not items:
        raise ValueError(f"{path} has no build items with geometry")
    return items


def read_svg_mesh(path: str, plane: str = "xy") -> MeshArrays:
    """An SVG's outline as a flat triangulated region (no thickness), in SVG user units, y up."""
    from . import geom2d

    loops = geom2d.svg_loops(path)
    pts, tris = geom2d.triangulate(loops)
    if not len(tris):
        raise ValueError(f"{path} has no filled region")
    zeros = np.zeros(len(pts))
    if plane == "xz":
        verts = np.column_stack([pts[:, 0], zeros, pts[:, 1]])
    elif plane == "yz":
        verts = np.column_stack([zeros, pts[:, 0], pts[:, 1]])
    else:
        verts = np.column_stack([pts[:, 0], pts[:, 1], zeros])
    return MeshArrays(verts, tris, name=os.path.splitext(os.path.basename(path))[0])


def read_mesh_file(path: str, plane: str = "xy") -> MeshArrays:
    """STL, OBJ, 3MF (all build items) or SVG (flat outline in plane) as one mesh, in the file's units."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".stl":
        return read_stl(path)
    if ext == ".obj":
        return read_obj(path)
    if ext == ".3mf":
        items = read_3mf(path)
        out = concat([a for _n, a in items])
        out.name = os.path.splitext(os.path.basename(path))[0]
        return out
    if ext == ".svg":
        return read_svg_mesh(path, plane)
    raise ValueError(f"unsupported file type {ext}; use .stl, .obj, .3mf or .svg")


# ----------------------------------------------------------------------------- writing files
def _atomic_write(path: str, write) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_stl(path: str, arrays: MeshArrays, binary: bool = True, name: str = "") -> int:
    """Write triangles as STL (binary by default). Returns the file size."""
    normals, _areas = tri_normals_areas(arrays)
    corners = arrays.verts[arrays.tris]
    label = (name or arrays.name or "vsblender")[:60]

    def write(tmp):
        if binary:
            data = np.zeros(len(arrays.tris), dtype=_STL_DTYPE)
            data["normal"] = normals.astype(np.float32)
            data["v"] = corners.astype(np.float32)
            with open(tmp, "wb") as handle:
                header = f"VSBlender {label}".encode("ascii", "replace")[:80].ljust(80, b" ")
                handle.write(header)
                handle.write(struct.pack("<I", len(arrays.tris)))
                handle.write(data.tobytes())
        else:
            safe = re.sub(r"\s+", "_", label) or "vsblender"
            with open(tmp, "w", encoding="ascii", newline="\n") as handle:
                handle.write(f"solid {safe}\n")
                for n, (a, b, c) in zip(normals, corners):
                    handle.write(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n    outer loop\n")
                    for p in (a, b, c):
                        handle.write(f"      vertex {p[0]:.6e} {p[1]:.6e} {p[2]:.6e}\n")
                    handle.write("    endloop\n  endfacet\n")
                handle.write(f"endsolid {safe}\n")

    _atomic_write(path, write)
    return os.path.getsize(path)


def write_obj(path: str, parts) -> int:
    """Write [(name, MeshArrays)] as one OBJ with an object per part (geometry only)."""
    def write(tmp):
        base = 1
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# VSBlender\n")
            for name, arrays in parts:
                handle.write(f"o {re.sub(r'\s+', '_', name) or 'part'}\n")
                buf = io.StringIO()
                np.savetxt(buf, arrays.verts, fmt="v %.6f %.6f %.6f")
                handle.write(buf.getvalue())
                buf = io.StringIO()
                np.savetxt(buf, arrays.tris + base, fmt="f %d %d %d")
                handle.write(buf.getvalue())
                base += len(arrays.verts)

    _atomic_write(path, write)
    return os.path.getsize(path)


_CT = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
</Types>
"""


def _rels(thumbnail: bool) -> str:
    thumb = ('\n <Relationship Target="/Metadata/thumbnail.png" Id="rel1" '
             'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>') if thumbnail else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>' + thumb + "\n</Relationships>\n")


def _xml(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def write_3mf(path: str, parts, title: str = "", application: str = "VSBlender", assembly: bool = False,
              thumbnail: str | None = None, metadata: dict | None = None) -> int:
    """Write [(name, MeshArrays in mm)] as a 3MF package (core spec, unit millimeter).

    One object and one build item per part, so slicers show each part by name on one plate.
    assembly=True wraps the parts in one object with components (one object with several parts).
    """
    model = io.StringIO()
    model.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    model.write('<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n')
    meta = {"Title": title or (parts[0][0] if parts else "model"), "Application": application}
    meta.update(metadata or {})
    for key, value in meta.items():
        model.write(f' <metadata name="{_xml(key)}">{_xml(value)}</metadata>\n')
    model.write(" <resources>\n")
    ids = []
    for index, (name, arrays) in enumerate(parts, start=1):
        model.write(f'  <object id="{index}" type="model" name="{_xml(name)}">\n   <mesh>\n    <vertices>\n')
        buf = io.StringIO()
        np.savetxt(buf, arrays.verts, fmt='     <vertex x="%.5f" y="%.5f" z="%.5f"/>')
        model.write(buf.getvalue())
        model.write("    </vertices>\n    <triangles>\n")
        buf = io.StringIO()
        np.savetxt(buf, arrays.tris, fmt='     <triangle v1="%d" v2="%d" v3="%d"/>')
        model.write(buf.getvalue())
        model.write("    </triangles>\n   </mesh>\n  </object>\n")
        ids.append(index)
    build_ids = ids
    if assembly and len(ids) > 1:
        top = len(ids) + 1
        model.write(f'  <object id="{top}" type="model" name="{_xml(title or "Assembly")}">\n   <components>\n')
        for i in ids:
            model.write(f'    <component objectid="{i}"/>\n')
        model.write("   </components>\n  </object>\n")
        build_ids = [top]
    model.write(" </resources>\n <build>\n")
    for i in build_ids:
        model.write(f'  <item objectid="{i}"/>\n')
    model.write(" </build>\n</model>\n")
    has_thumb = bool(thumbnail and os.path.isfile(thumbnail))

    def write(tmp):
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CT)
            archive.writestr("_rels/.rels", _rels(has_thumb))
            archive.writestr("3D/3dmodel.model", model.getvalue())
            if has_thumb:
                archive.write(thumbnail, "Metadata/thumbnail.png")

    _atomic_write(path, write)
    return os.path.getsize(path)


def mesh_stats(arrays: MeshArrays, weld_tol: float) -> dict:
    """Quick watertightness summary: counts only, for export warnings and Solid.check."""
    welded, _remap = weld(arrays, weld_tol)
    stats = edge_stats(welded)
    labels = components(welded)
    va = volume_area(welded)
    return {"tris": int(len(arrays.tris)), "open_edges": stats["open_edges"],
            "non_manifold_edges": stats["non_manifold_edges"], "flipped_edges": stats["flipped_edges"],
            "degenerate_tris": stats["degenerate_tris"], "shells": int(labels.max() + 1) if len(labels) else 0,
            "volume": va["volume"], "area": va["area"],
            "watertight": stats["open_edges"] == 0 and stats["non_manifold_edges"] == 0 and stats["flipped_edges"] == 0}


def fmt_num(value: float, digits: int = 4) -> str:
    """value with at most digits decimals, trailing zeros of the decimals removed (90.0 -> "90")."""
    if not math.isfinite(value):
        return str(value)
    text = f"{value:.{max(0, int(digits))}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"

"""spatial op sweep and op through: moving-part clearance and open light/wire/screw paths. Read-only.

sweep    moves a copy of an object's mesh through a motion (a turn about an axis, or a slide) and,
         at each step, tests it against the parts that stand still: a collision (faces cross, or one
         is inside the other) and otherwise the smallest gap. Nothing in the scene moves.
through  shoots rays through openings (glyph windows, screw holes, wire channels) and reports the
         rays something blocks, and what.

Everything is in world space, in scene units, from the evaluated meshes.
"""
from __future__ import annotations

import math
import zlib

import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

import bpy

from . import marks, meshdata

GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT"}


def _objects(spec, root: str) -> list:
    from . import inspect_tools

    return [o for o in inspect_tools.resolve_objects(spec, root) if o.type in GEOMETRY]


def _scene_parts(exclude: set) -> list:
    """Visible geometry that is part of the scene: not staging, references or the movers. Hardware
    stand-ins are kept: they are what a moving part must clear."""
    out = []
    for ob in bpy.context.scene.objects:
        if ob.type not in GEOMETRY or ob.name in exclude or ob.name.startswith("_vsblender"):
            continue
        if marks.left_out(ob) or ob.hide_render or ob.hide_get():
            continue
        out.append(ob)
    return out


def _arrays(obs: list, deps) -> meshdata.MeshArrays:
    return meshdata.concat([meshdata.object_arrays(o, deps) for o in obs])


def _tree(arrays: meshdata.MeshArrays) -> BVHTree:
    return BVHTree.FromPolygons(arrays.verts.tolist(), arrays.tris.tolist(), all_triangles=True, epsilon=0.0)


def _frame(name) -> Matrix:
    if not name:
        return Matrix.Identity(4)
    ob = bpy.data.objects.get(str(name))
    if ob is None:
        raise KeyError(f"frame: no object named {name!r}")
    return ob.matrix_world.copy()


def _axis(value) -> Vector:
    if isinstance(value, str):
        from .geo import axis_vector

        return axis_vector(value)
    v = Vector(tuple(value)[:3])
    if v.length == 0:
        raise ValueError("axis must not be zero")
    return v.normalized()


def _values(spec, default_count: int = 24) -> list:
    if spec is None:
        return [360.0 * i / default_count for i in range(default_count)]
    if isinstance(spec, dict):
        start, stop = float(spec.get("from", 0.0)), float(spec.get("to", 360.0))
        step = float(spec.get("step") or (stop - start) / default_count)
        if step == 0:
            raise ValueError("step must not be zero")
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        if count > 3600:
            raise ValueError(f"{count} steps; use a larger step (at most 3600)")
        return [start + step * i for i in range(count)]
    return [float(v) for v in spec]


def motion_steps(motion: dict, mover_origin: Vector) -> tuple:
    """([(value, world matrix)], what moves, the unit of value)."""
    if not isinstance(motion, dict):
        raise ValueError("motion is {rotate: {axis, pivot, angles}} or {translate: [dx, dy, dz], steps}")
    frame = _frame(motion.get("frame"))
    rot = frame.to_3x3()
    if "rotate" in motion:
        spec = motion["rotate"] or {}
        axis_local = _axis(spec.get("axis", "Z"))
        axis = (rot @ axis_local).normalized()
        pivot = spec.get("pivot")
        if isinstance(pivot, str):
            ob = bpy.data.objects.get(pivot)
            if ob is None:
                raise KeyError(f"pivot: no object named {pivot!r}")
            centre = ob.matrix_world.translation.copy()
        elif pivot is not None:
            centre = frame @ Vector(tuple(pivot)[:3])
        else:
            centre = frame.translation.copy() if motion.get("frame") else mover_origin
        steps = []
        for angle in _values(spec.get("angles"), 24):
            m = Matrix.Translation(centre) @ Matrix.Rotation(math.radians(angle), 4, axis) @ Matrix.Translation(-centre)
            steps.append((angle, m))
        return steps, f"turned about {tuple(round(c, 4) for c in axis)} through {tuple(round(c, 4) for c in centre)}", "deg"
    if "translate" in motion:
        delta = rot @ Vector(tuple(motion["translate"])[:3])
        count = max(1, int(motion.get("steps") or 20))
        steps = [(i / count, Matrix.Translation(delta * (i / count))) for i in range(count + 1)]
        return steps, f"slid by {tuple(round(c, 4) for c in delta)}", "fraction of the travel"
    raise ValueError("motion needs rotate or translate")


def _inside(tree: BVHTree, point, directions) -> bool:
    """Ray parity along two directions: odd crossings on both means inside."""
    for direction in directions:
        n, q = 0, Vector(point)
        for _ in range(400):
            loc, _nrm, _i, _d = tree.ray_cast(q, direction, 1e9)
            if loc is None:
                break
            n += 1
            q = loc + direction * 1e-7
        if n % 2 == 0:
            return False
    return True


_DIRS = (Vector((0.31, 0.27, 0.91)).normalized(), Vector((-0.6, 0.71, -0.36)).normalized())


def _sample(points: np.ndarray, k: int) -> np.ndarray:
    if len(points) <= k:
        return points
    return points[np.linspace(0, len(points) - 1, k).astype(int)]


def sweep(params: dict, root: str) -> dict:
    """Move `object` through `motion` and test it against `against` (default: the rest of the scene)."""
    deps = bpy.context.evaluated_depsgraph_get()
    names = params.get("object") or params.get("target")
    if not names:
        raise ValueError("sweep needs object (the part that moves)")
    movers = _objects([names] if isinstance(names, str) else list(names), root)
    if not movers:
        raise ValueError(f"no geometry object named {names!r}")
    mover = _arrays(movers, deps)
    if not len(mover.tris):
        raise ValueError("the moving part has no faces")
    exclude = {o.name for o in movers}
    against = params.get("against")
    obstacles = _objects(against, root) if against not in (None, "", []) else _scene_parts(exclude)
    obstacles = [o for o in obstacles if o.name not in exclude]
    steps, what, unit = motion_steps(params.get("motion") or {}, movers[0].matrix_world.translation.copy())
    lo, hi = mover.bounds()
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    # Parts further than reach from anywhere the mover goes are listed as out of reach, not measured.
    reach = float(params.get("reach") or diag * 0.25)
    samples = int(params.get("samples") or 1500)
    # Swept bounds: the mover's box corners at every step.
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    swept = []
    for _v, m in steps:
        mm = np.array(m)
        swept.append(corners @ mm[:3, :3].T + mm[:3, 3])
    swept = np.vstack(swept)
    s_lo, s_hi = swept.min(axis=0) - reach, swept.max(axis=0) + reach
    rows = []
    skipped = []
    candidates = []
    for ob in obstacles:
        arrays = meshdata.object_arrays(ob, deps)
        if not len(arrays.tris):
            continue
        o_lo, o_hi = arrays.bounds()
        if np.any(o_hi < s_lo) or np.any(o_lo > s_hi):
            skipped.append(ob.name)
            continue
        candidates.append((ob, arrays, _tree(arrays)))
    mover_samples = _sample(mover.verts, samples)
    for ob, arrays, tree in candidates:
        obstacle_samples = _sample(arrays.verts, samples)
        best_gap, best_at, first_hit, hits = math.inf, None, None, 0
        for value, m in steps:
            mm = np.array(m)
            verts = mover.verts @ mm[:3, :3].T + mm[:3, 3]
            moved = BVHTree.FromPolygons(verts.tolist(), mover.tris.tolist(), all_triangles=True, epsilon=0.0)
            crossing = moved.overlap(tree)
            contained = False
            if not crossing:
                contained = _inside(tree, verts[0], _DIRS) or _inside(moved, arrays.verts[0], _DIRS)
            if crossing or contained:
                hits += 1
                if first_hit is None:
                    first_hit = {"at": round(value, 4), "pairs": len(crossing), "inside": contained}
                if best_gap > 0.0:
                    best_gap, best_at = 0.0, round(value, 4)
                continue
            pts = mover_samples @ mm[:3, :3].T + mm[:3, 3]
            gap = math.inf
            for p in pts:
                found = tree.find_nearest(Vector(p))
                if found[0] is not None:
                    gap = min(gap, found[3])
            for p in obstacle_samples:
                found = moved.find_nearest(Vector(p))
                if found[0] is not None:
                    gap = min(gap, found[3])
            if gap < best_gap:
                best_gap, best_at = gap, round(value, 4)
        rows.append({"object": ob.name, "smallest_gap": round(best_gap, 6) if math.isfinite(best_gap) else None,
                     "at": best_at, "collisions": hits, "first_collision": first_hit})
    rows.sort(key=lambda r: (r["smallest_gap"] if r["smallest_gap"] is not None else math.inf))
    clear = all(r["collisions"] == 0 for r in rows)
    tight = float(params.get("tolerance") or 0.0)
    lines = [f"sweep: {', '.join(o.name for o in movers)} {what}, {len(steps)} steps ({unit}); "
             f"{len(rows)} part(s) in reach, {len(skipped)} out of reach"]
    for r in rows[:20]:
        if r["collisions"]:
            fc = r["first_collision"]
            lines.append(f"- COLLIDES with {r['object']} at {r['collisions']} of {len(steps)} steps, first at {fc['at']}"
                         + (" (one inside the other)" if fc["inside"] else f" ({fc['pairs']} crossing face pairs)"))
        else:
            flag = " TIGHT" if tight and r["smallest_gap"] is not None and r["smallest_gap"] < tight else ""
            lines.append(f"- clears {r['object']}: smallest gap {meshdata.fmt_num(r['smallest_gap'] or 0.0, 4)} at {r['at']}{flag}")
    if not rows:
        lines.append("- nothing comes within reach of the moving part")
    return {"op": "sweep", "clear": clear, "steps": len(steps), "motion": what, "unit": unit, "parts": rows,
            "out_of_reach": skipped[:50], "text": "\n".join(lines)}


def _inside_2d(x: float, y: float, loops) -> bool:
    c = 0
    for lp in loops:
        lp = np.asarray(lp, float)
        for (x0, y0), (x1, y1) in zip(lp, np.roll(lp, -1, axis=0)):
            if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                c += 1
    return c % 2 == 1


def through(params: dict, root: str) -> dict:
    """Rays through openings. loops: 2D outlines in the frame's XY plane (one opening each, or
    [[loop, hole], ...] with holes); rays start at local z = start and run along direction (local,
    default +Z) for length. points: explicit 3D start points (world, or the frame's) instead."""
    deps = bpy.context.evaluated_depsgraph_get()
    targets = params.get("targets")
    obs = _objects(targets, root) if targets not in (None, "", []) else _scene_parts(set())
    parts = []
    owner = []
    for i, ob in enumerate(obs):
        arrays = meshdata.object_arrays(ob, deps)
        if len(arrays.tris):
            parts.append(arrays)
            owner.append(np.full(len(arrays.tris), i, dtype=np.int64))
    if not parts:
        raise ValueError("nothing to block the rays: no geometry in targets")
    combined = meshdata.concat(parts)
    who = np.concatenate(owner)
    tree = _tree(combined)
    frame = _frame(params.get("frame"))
    rot = frame.to_3x3()
    direction = (rot @ Vector(tuple(params.get("direction") or (0.0, 0.0, 1.0))[:3])).normalized()
    length = float(params.get("length") or 1e6)
    start = float(params.get("start") or 0.0)
    samples = max(1, int(params.get("samples") or 30))
    openings = []
    if params.get("points"):
        pts = [frame @ Vector(tuple(p)[:3]) for p in params["points"]]
        openings.append(("points", pts))
    for index, entry in enumerate(params.get("loops") or []):
        loops = entry if (len(entry) and isinstance(entry[0][0], (list, tuple))) else [entry]
        loops = [np.asarray(l, float) for l in loops]
        label = (params.get("labels") or [])[index] if index < len(params.get("labels") or []) else str(index)
        allpts = np.vstack(loops)
        lo, hi = allpts.min(axis=0), allpts.max(axis=0)
        rng = np.random.default_rng(zlib.crc32(str(label).encode()))
        pts, guard = [], 0
        while len(pts) < samples and guard < samples * 100:
            guard += 1
            x, y = lo + rng.random(2) * (hi - lo)
            if _inside_2d(x, y, loops):
                pts.append(frame @ Vector((x, y, start)))
        openings.append((label, pts))
    if not openings:
        raise ValueError("through needs loops (openings in the frame's XY plane) or points")
    rows = []
    for label, pts in openings:
        blocked = 0
        first = None
        for p in pts:
            loc, _n, index, dist = tree.ray_cast(p, direction, length)
            if loc is not None:
                blocked += 1
                name = obs[int(who[index])].name if index is not None and index < len(who) else None
                if first is None or dist < first["distance"]:
                    first = {"object": name, "distance": round(dist, 6), "at": [round(c, 5) for c in loc]}
        rows.append({"opening": label, "rays": len(pts), "blocked": blocked, "first_block": first})
    lines = [f"through: {len(rows)} opening(s), rays along {tuple(round(c, 4) for c in direction)} for {meshdata.fmt_num(length, 4)}"]
    for r in rows[:40]:
        if not r["rays"]:
            lines.append(f"- {r['opening']}: no sample point inside the outline")
        elif r["blocked"]:
            b = r["first_block"]
            lines.append(f"- {r['opening']}: {r['blocked']} of {r['rays']} rays BLOCKED, nearest by {b['object']} after "
                         f"{meshdata.fmt_num(b['distance'], 4)}")
        else:
            lines.append(f"- {r['opening']}: open ({r['rays']} rays)")
    return {"op": "through", "open": all(r["blocked"] == 0 and r["rays"] for r in rows), "openings": rows,
            "text": "\n".join(lines)}

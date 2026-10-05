"""measure: shapes, not just bounds. Read-only, on scene objects (evaluated) or reference files.

op section    outline of a slice: plane {axis: z, at: 1.2}, {point, normal}, or {angle: 20} (the half-plane
              through the ring axis at that angle, drawn as radius across and depth up); points, width,
              depth, area, and with image the cut drawn on a grid
op profile    min/max of one coordinate binned along another: along r (radius), angle, depth, x, y or z
op depthmap   height map over the target seen from a view (top, front...): ASCII rows, optional PNG
op angular    occupancy against angle in a radius band and height band (notches, gaps, repeats)
op pitch      the repeat period of the angular occupancy (teeth every 9.23 degrees)

Frames: axis (x, y, z) with center gives radius and angle (counter-clockwise from the first other
axis); ring {up, front, center, clockwise} gives clock angles (clockwise from the top seen from the
front, as geo.ring) and depth toward the viewer. Windows on every op: angle [a0, a1], radius [r0, r1],
height (or depth) [d0, d1]. segment: 40 folds every 40 degrees onto one segment (angular).

Sources: targets (scene objects), file (STL, OBJ, 3MF, SVG; part selects an OBJ material or group),
or ref (a name in references.json; "peg/Chevron" or part for one part). Files outside the workspace
need a referenceRoots folder. compare_to measures a second source the same way, on the same bins,
and reports the differences. Results are in scene units, with the unit label.
"""
from __future__ import annotations

import fnmatch
import math
import os

import numpy as np

import bpy

from . import meshdata, units as units_mod

AXES = {"x": 0, "y": 1, "z": 2}
_AXIS_WORDS = {"x": "x", "y": "y", "z": "z", "r": "r", "radius": "r", "radial": "r", "angle": "angle", "theta": "angle",
               "a": "angle", "depth": "depth", "d": "depth", "height": "depth", "h": "depth"}
OPS = ("section", "profile", "depthmap", "angular", "pitch")


def _axis(name, what: str = "axis") -> int:
    key = str(name or "z").strip().lower().lstrip("+-")
    if key not in AXES:
        raise ValueError(f"{what} must be x, y or z, not {name!r}")
    return AXES[key]


def _coordinate(name, what: str, default: str) -> str:
    key = str(name or default).strip().lower()
    if key not in _AXIS_WORDS:
        raise ValueError(f"{what} must be r (radius), angle, depth, x, y or z, not {name!r}")
    return _AXIS_WORDS[key]


# ----------------------------------------------------------------------------- frames
class Frame:
    """Radius, angle and depth of points: about an axis (math angles), or a ring (clock angles)."""

    def __init__(self, params: dict):
        ring = params.get("ring")
        self.center = np.asarray((ring or {}).get("center") or params.get("center") or [0, 0, 0], float)
        if ring:
            from . import geo

            r = geo.Ring(up=ring.get("up", "+Y"), front=ring.get("front", "+Z"), center=tuple(self.center),
                         clockwise=ring.get("clockwise", True) is not False)
            self.right = np.asarray(r.right, float)
            self.up = np.asarray(r.up, float)
            self.front = np.asarray(r.front, float)
            self.sign = r.sign
            self.clock = True
            self.label = "clock angle (clockwise from the top, seen from the front)"
        else:
            axis = _axis(params.get("axis"))
            first, second = [i for i in range(3) if i != axis]
            self.right = np.eye(3)[first]
            self.up = np.eye(3)[second]
            self.front = np.eye(3)[axis]
            self.sign = -1.0  # atan2(second, first): counter-clockwise from the first axis
            self.clock = False
            self.label = f"angle about {'xyz'[axis]} from +{'xyz'[first]} toward +{'xyz'[second]}"

    def polar(self, pts) -> tuple:
        rel = np.asarray(pts, float) - self.center
        x, y = rel @ self.right, rel @ self.up
        r = np.hypot(x, y)
        if self.clock:
            ang = np.degrees(np.arctan2(self.sign * x, y)) % 360.0
        else:
            ang = np.degrees(np.arctan2(y, x)) % 360.0
        return r, ang, rel @ self.front

    def direction(self, a: float) -> np.ndarray:
        t = math.radians(a)
        if self.clock:
            return self.right * (self.sign * math.sin(t)) + self.up * math.cos(t)
        return self.right * math.cos(t) + self.up * math.sin(t)


def _in_window(ang, a0: float, a1: float):
    span = (float(a1) - float(a0)) % 360.0
    if abs(float(a1) - float(a0)) >= 360.0:
        return np.ones_like(ang, dtype=bool)
    return ((ang - float(a0)) % 360.0) <= span + 1e-9


def _pair(value, what: str):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{what} must be [from, to]")
    return float(value[0]), float(value[1])


def _mask(frame: Frame, pts, params: dict):
    """Which points are inside the angle, radius and height (depth) windows."""
    r, ang, depth = frame.polar(pts)
    keep = np.ones(len(r), dtype=bool)
    window = _pair(params.get("angle"), "angle")
    if window:
        keep &= _in_window(ang, *window)
    band = _pair(params.get("radius") if params.get("radius") is not None else params.get("r"), "radius")
    if band:
        keep &= (r >= band[0]) & (r <= band[1])
    height = _pair(params.get("height") if params.get("height") is not None else params.get("depth"), "height")
    if height:
        keep &= (depth >= min(height)) & (depth <= max(height))
    return keep


def _restrict(arrays, frame: Frame, params: dict):
    """The triangles whose centroids are inside the windows (all of them when none is given)."""
    if not any(params.get(k) is not None for k in ("angle", "radius", "r", "height", "depth")):
        return arrays
    keep = _mask(frame, arrays.verts[arrays.tris].mean(axis=1), params)
    if not keep.any():
        raise ValueError("nothing of this source is inside the angle / radius / height window")
    return arrays.select(keep)


# ----------------------------------------------------------------------------- sources
def _allowed(path: str, root: str, roots: list) -> bool:
    from . import preview

    return any(r and preview._under(path, r) for r in [root, *roots])


def _resolve_file(raw: str, root: str, roots: list) -> str:
    full = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    if not _allowed(full, root, roots):
        raise ValueError(f"{raw} is outside the workspace. Add its folder to \"referenceRoots\" in .blender-ai/config.json, "
                         "or bring it in with import_reference")
    if not os.path.isfile(full):
        raise FileNotFoundError(raw)
    return full


def _transform_matrix(transform: dict, file_units: str):
    from mathutils import Euler, Matrix, Vector

    factor = units_mod.file_to_bu(file_units)
    scale = transform.get("scale", 1.0)
    scale = Vector(scale) if isinstance(scale, (list, tuple)) else Vector((float(scale),) * 3)
    rot = [math.radians(float(v)) for v in (transform.get("rotation_deg") or [0, 0, 0])][:3]
    loc = Vector([float(v) for v in (transform.get("location") or [0, 0, 0])][:3])
    return Matrix.Translation(loc) @ Euler(rot, "XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*(scale * factor), 1.0))


def reference_entry(name: str, root: str) -> tuple:
    """(entry, part) for 'peg' or 'peg/Chevron' from references.json."""
    from . import preview

    refs = preview._reference_set(root)
    base, _, part = str(name).partition("/")
    entry = refs.get(str(name)) or refs.get(base)
    if entry is None:
        raise KeyError(f"no reference named {name!r} in references.json. Known: {', '.join(refs) or 'none'} "
                       "(import_reference registers one)")
    if refs.get(str(name)) is not None:
        part = ""
    return entry, part


def _ref_arrays(spec: dict, root: str, roots: list):
    entry, part = reference_entry(spec["ref"], root)
    wanted = part or spec.get("part") or spec.get("parts")
    if isinstance(wanted, str):
        wanted = [wanted]
    files = []
    parts = entry.get("parts") or {}
    if parts:
        names = [n for n in parts if not wanted or any(fnmatch.fnmatchcase(n, p) or n == p for p in wanted)]
        if not names:
            raise KeyError(f"reference {spec['ref']!r} has no part matching {wanted}. Parts: {', '.join(list(parts)[:60])}")
        files = [parts[n] for n in names]
        label = f"{str(spec['ref']).split('/')[0]}/{names[0]}" if len(names) == 1 else f"{spec['ref']} ({len(names)} parts)"
    else:
        files = [entry["file"]]
        label = str(spec["ref"])
    arrays = []
    for raw in files:
        path = _resolve_file(raw, root, roots)
        arr = meshdata.read_mesh_file(path, plane=str((entry.get("transform") or {}).get("plane") or "xy"),
                                      part=wanted if not parts and wanted else None)
        arrays.append(arr)
    merged = meshdata.concat(arrays) if len(arrays) > 1 else arrays[0]
    transform = dict(entry.get("transform") or {})
    transform.update(spec.get("transform") or {})
    file_units = str(spec.get("units") or transform.get("units") or entry.get("units") or "mm")
    return merged.transformed(_transform_matrix(transform, file_units)), label, True


def _source_arrays(spec: dict, root: str, roots: list | None = None) -> tuple:
    """(MeshArrays in world scene units, label, units_given) for {targets}, {file, transform} or {ref}."""
    roots = roots or []
    if spec.get("ref"):
        return _ref_arrays(spec, root, roots)
    if spec.get("file"):
        path = _resolve_file(str(spec["file"]), root, roots)
        transform = spec.get("transform") or {}
        arrays = meshdata.read_mesh_file(path, plane=str(transform.get("plane") or "xy"), part=spec.get("part"))
        given = transform.get("units") or spec.get("units")
        label = os.path.basename(path) + (f" ({arrays.name})" if spec.get("part") else "")
        return arrays.transformed(_transform_matrix(transform, str(given or "mm"))), label, bool(given)
    from . import inspect_tools

    obs = inspect_tools.resolve_objects(spec.get("targets"), root)
    obs = [o for o in obs if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}]
    if not obs:
        raise ValueError("no geometry to measure: pass targets (names or a find selector), file, or ref")
    deps = bpy.context.evaluated_depsgraph_get()
    arrays = meshdata.concat([meshdata.object_arrays(o, deps) for o in obs])
    label = obs[0].name if len(obs) == 1 else f"{len(obs)} objects"
    return arrays, label, True


def _unit_warning(arrays, label: str, warnings: list) -> None:
    """A file read in the wrong unit is 1000 times too big or small next to the scene."""
    lo, hi = arrays.bounds()
    size = float(np.linalg.norm(hi - lo))
    scene_lo, scene_hi = np.full(3, np.inf), np.full(3, -np.inf)
    from mathutils import Vector

    for ob in bpy.context.scene.objects:
        if ob.type != "MESH" or ob.hide_render or ob.name.startswith("_vsblender"):
            continue
        for corner in ob.bound_box:
            p = np.asarray(ob.matrix_world @ Vector(corner))
            scene_lo, scene_hi = np.minimum(scene_lo, p), np.maximum(scene_hi, p)
    if not np.all(np.isfinite(scene_lo)) or size <= 0:
        return
    scene = float(np.linalg.norm(scene_hi - scene_lo))
    if scene <= 0:
        return
    ratio = size / scene
    if ratio > 30 or ratio < 1 / 30:
        warnings.append(f"{label} is {ratio:.3g} times the size of the scene ({meshdata.fmt_num(size, 4)} against "
                        f"{meshdata.fmt_num(scene, 4)} {units_mod.units()['symbol']}): if that is wrong, pass units (mm, cm, m, in) "
                        "or set them in references.json")


# ----------------------------------------------------------------------------- section
def _plane(params: dict, arrays, frame: Frame) -> tuple:
    plane = params.get("plane") or {"axis": "z"}
    if "angle" in plane:
        # The half-plane through the axis at that angle: radius across, depth up.
        u = frame.direction(float(plane["angle"]))
        n = np.cross(frame.front, u)
        n /= np.linalg.norm(n)
        return frame.center, n, u, frame.front, True
    if "normal" in plane:
        n = np.asarray(plane["normal"], float)
        n = n / np.linalg.norm(n)
        p = np.asarray(plane.get("point") or [0, 0, 0], float)
    else:
        axis = _axis(plane.get("axis"), "plane.axis")
        n = np.zeros(3)
        n[axis] = 1.0
        lo, hi = arrays.bounds()
        at = plane.get("at")
        p = np.zeros(3)
        p[axis] = float(at) if at is not None else float((lo[axis] + hi[axis]) / 2)
        # Axis-aligned cuts report along world axes: z -> (x, y), y -> (x, z), x -> (y, z).
        first, second = [i for i in range(3) if i != axis]
        u = np.zeros(3)
        u[first] = 1.0
        v = np.zeros(3)
        v[second] = 1.0
        return p, n, u, v, False
    # In-plane axes: u, v with u x v = n.
    helper = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(helper, n)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return p, n, u, v, False


def _axis_name(vec) -> str:
    v = np.asarray(vec, float)
    i = int(np.argmax(np.abs(v)))
    return "xyz"[i] if abs(v[i]) > 0.999 else "u"


def _cut(arrays, p, n, u, v, half: bool) -> np.ndarray:
    """Segments where the triangles cross the plane, as (S, 2, 2) in (u, v)."""
    d = (arrays.verts - p) @ n
    t = arrays.tris
    dt = d[t]
    crosses = (dt.min(axis=1) < 0) & (dt.max(axis=1) > 0)
    pts3, dd = arrays.verts[t[crosses]], dt[crosses]
    if not len(pts3):
        return np.zeros((0, 2, 2))
    hits = []
    for i0, i1 in ((0, 1), (1, 2), (2, 0)):
        e = (dd[:, i0] < 0) != (dd[:, i1] < 0)
        s = np.where(e, dd[:, i0] / np.where(e, dd[:, i0] - dd[:, i1], 1.0), 0.0)
        q = pts3[:, i0] + (pts3[:, i1] - pts3[:, i0]) * s[:, None]
        hits.append((e, q))
    count = sum(e.astype(int) for e, _q in hits)
    ok = count == 2
    pairs = []
    for k in np.flatnonzero(ok):
        pairs.append([q[k] for e, q in hits if e[k]][:2])
    if not pairs:
        return np.zeros((0, 2, 2))
    seg = np.array(pairs)
    uv = np.stack([(seg - p) @ u, (seg - p) @ v], axis=-1)
    if half:
        uv = uv[(uv[:, :, 0] >= 0).all(axis=1)]
    return uv


def section(arrays, params: dict, frame: Frame) -> dict:
    from . import geom2d

    p, n, u, v, half = _plane(params, arrays, frame)
    uv = _cut(arrays, p, n, u, v, half)
    if not len(uv):
        return {"loops": 0, "segments": uv, "text": "the plane does not cut the geometry (inside the windows)"}
    loops = _chain(uv)
    loops = [geom2d.simplify(l, float(params.get("tol") or 0.0) or _auto_tol(uv)) for l in loops]
    lo = uv.reshape(-1, 2).min(axis=0)
    hi = uv.reshape(-1, 2).max(axis=0)
    closed = [l for l in loops if len(l) >= 3]
    area = geom2d.area(closed) if closed else 0.0
    budget = int(params.get("max_points") or 200)
    total = sum(len(l) for l in loops)
    step = max(1, math.ceil(total / budget))
    digits = _digits(hi - lo)
    out_loops = [[[round(float(x), digits), round(float(y), digits)] for x, y in l[::step]] for l in loops]
    if half:
        plane_text = f"half-plane at {meshdata.fmt_num(float(params['plane']['angle']), 3)}° ({frame.label}): u = radius, v = depth"
        u_name, v_name = "radius", "depth"
    else:
        plane_text = f"normal {np.round(n, 4).tolist()} through {np.round(p, 5).tolist()}"
        u_name, v_name = _axis_name(u), _axis_name(v)
    text = (f"section ({plane_text}): {len(loops)} loop(s), {u_name} {meshdata.fmt_num(lo[0], digits)}..{meshdata.fmt_num(hi[0], digits)}, "
            f"{v_name} {meshdata.fmt_num(lo[1], digits)}..{meshdata.fmt_num(hi[1], digits)} (width {meshdata.fmt_num(hi[0] - lo[0], digits)}, "
            f"depth {meshdata.fmt_num(hi[1] - lo[1], digits)}), area {meshdata.fmt_num(area, digits + 2)}")
    shown = int(params.get("text_points") if params.get("text_points") is not None else 60)
    if shown > 0:
        text += "\n" + _points_text(out_loops, shown, u_name, v_name)
    return {"loops": len(loops), "width": round(float(hi[0] - lo[0]), 6), "depth": round(float(hi[1] - lo[1]), 6),
            "area": round(float(area), 6), "min_uv": np.round(lo, 6).tolist(), "max_uv": np.round(hi, 6).tolist(),
            "plane": plane_text, "u_axis": np.round(u, 4).tolist(), "v_axis": np.round(v, 4).tolist(), "points": out_loops,
            "segments": uv, "axes_names": [u_name, v_name], "text": text}


def _digits(span) -> int:
    """Decimals that resolve about a thousandth of the extent."""
    size = float(np.max(np.abs(span))) if np.size(span) else 1.0
    return max(0, min(8, int(math.ceil(-math.log10(max(size, 1e-12) / 1000.0)))))


def _points_text(loops, budget: int, u_name: str, v_name: str) -> str:
    total = sum(len(l) for l in loops)
    step = max(1, math.ceil(total / max(1, budget)))
    lines = [f"points ({u_name}, {v_name}){f', every {step}th' if step > 1 else ''}:"]
    for i, loop in enumerate(loops[:12]):
        pts = loop[::step] or loop[:1]
        lines.append(f"  loop {i + 1} ({len(loop)} pts): " + " ".join(f"({x:g}, {y:g})" for x, y in pts))
    if len(loops) > 12:
        lines.append(f"  ... {len(loops) - 12} more loops")
    return "\n".join(lines)


def _auto_tol(uv) -> float:
    pts = uv.reshape(-1, 2)
    return float(np.max(pts.max(axis=0) - pts.min(axis=0))) * 1e-4


def _chain(uv) -> list:
    """Join segment end points into polylines (closed loops where they close)."""
    pts = uv.reshape(-1, 2)
    span = float(np.max(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    q = np.round(pts / (span * 1e-7)).astype(np.int64)
    _u, ids = np.unique(q, axis=0, return_inverse=True)
    ids = ids.reshape(-1, 2)
    coords = {}
    for (a, b), (pa, pb) in zip(ids, uv):
        coords[a] = pa
        coords[b] = pb
    nbr = {}
    for a, b in ids:
        if a == b:
            continue
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)
    seen = set()
    loops = []
    for start in nbr:
        if start in seen:
            continue
        loop = [start]
        seen.add(start)
        prev, cur = None, start
        while True:
            nxt = [k for k in nbr[cur] if k != prev and k not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            loop.append(cur)
        if len(loop) >= 2:
            loops.append(np.array([coords[k] for k in loop]))
    loops.sort(key=lambda l: -len(l))
    return loops


def _deviation(seg_a, seg_b, samples: int = 4000) -> dict | None:
    """How far outline a strays from outline b: points along a, distance to the nearest segment of b."""
    if not len(seg_a) or not len(seg_b):
        return None
    lengths = np.linalg.norm(seg_a[:, 1] - seg_a[:, 0], axis=1)
    total = float(lengths.sum())
    if total <= 0:
        return None
    rng = np.random.default_rng(0)
    pick = rng.choice(len(seg_a), size=min(samples, max(64, len(seg_a) * 4)), p=lengths / total)
    t = rng.random(len(pick))[:, None]
    pts = seg_a[pick, 0] + (seg_a[pick, 1] - seg_a[pick, 0]) * t
    a, b = seg_b[:, 0], seg_b[:, 1]
    ab = b - a
    denom = np.maximum((ab * ab).sum(axis=1), 1e-30)
    best = np.full(len(pts), np.inf)
    for start in range(0, len(pts), 256):
        chunk = pts[start:start + 256]
        w = chunk[:, None, :] - a[None, :, :]
        s = np.clip((w * ab[None]).sum(axis=2) / denom[None], 0.0, 1.0)
        nearest = a[None] + s[..., None] * ab[None]
        dist = np.linalg.norm(chunk[:, None, :] - nearest, axis=2).min(axis=1)
        best[start:start + 256] = dist
    worst = int(np.argmax(best))
    return {"max": float(best[worst]), "p95": float(np.percentile(best, 95)), "mean": float(best.mean()),
            "at": [float(pts[worst, 0]), float(pts[worst, 1])]}


# ----------------------------------------------------------------------------- profile
def _surface_samples(arrays, count: int):
    normals, areas = meshdata.tri_normals_areas(arrays)
    total = float(areas.sum())
    if total <= 0:
        return arrays.verts
    rng = np.random.default_rng(0)
    idx = rng.choice(len(arrays.tris), size=count, p=areas / total)
    r1, r2 = rng.random(count), rng.random(count)
    s = np.sqrt(r1)
    a, b, c = (arrays.verts[arrays.tris[idx, k]] for k in range(3))
    return (1 - s)[:, None] * a + (s * (1 - r2))[:, None] * b + (s * r2)[:, None] * c


def _coords(pts, name: str, frame: Frame):
    if name in ("x", "y", "z"):
        return pts[:, AXES[name]]
    r, ang, depth = frame.polar(pts)
    return {"r": r, "angle": ang, "depth": depth}[name]


def profile(arrays, params: dict, frame: Frame, edges=None) -> dict:
    """value (min and max) per bin along `along`: a ring's cross-section is value depth along r."""
    pts = _surface_samples(arrays, int(params.get("samples") or 60000))
    pts = pts[_mask(frame, pts, params)]
    if not len(pts):
        raise ValueError("no surface inside the windows")
    along = _coordinate(params.get("along"), "along", "r")
    value = _coordinate(params.get("value"), "value", "depth" if params.get("ring") else "z")
    x = _coords(pts, along, frame)
    y = _coords(pts, value, frame)
    if edges is None:
        bins = max(1, min(400, int(params.get("bins") or 24)))
        lo = float(params["min"]) if params.get("min") is not None else float(x.min())
        hi = float(params["max"]) if params.get("max") is not None else float(x.max())
        edges = np.linspace(lo, hi, bins + 1)
    bins = len(edges) - 1
    which = np.clip(np.digitize(x, edges) - 1, 0, bins - 1)
    inside = (x >= edges[0]) & (x <= edges[-1])
    rows = []
    digits = _digits(np.ptp(y) if len(y) else 1.0)
    for i in range(bins):
        sel = (which == i) & inside
        if sel.any():
            rows.append([round(float(edges[i]), 6), round(float(edges[i + 1]), 6), round(float(y[sel].min()), digits),
                         round(float(y[sel].max()), digits), int(sel.sum())])
        else:
            rows.append([round(float(edges[i]), 6), round(float(edges[i + 1]), 6), None, None, 0])
    lines = [f"{value} range per {along} bin ({bins} bins, {int(inside.sum())} surface samples):"]
    for a, b, mn, mx, n in rows:
        lines.append(f"  {along} {meshdata.fmt_num(a, 5)}..{meshdata.fmt_num(b, 5)}: " +
                     (f"{value} {meshdata.fmt_num(mn, digits)}..{meshdata.fmt_num(mx, digits)}" if n else "empty"))
    return {"rows": rows, "edges": edges, "columns": [f"{along}_from", f"{along}_to", f"{value}_min", f"{value}_max", "samples"],
            "axes_names": [along, value], "text": "\n".join(lines)}


# ----------------------------------------------------------------------------- depthmap
def depthmap(arrays, params: dict, out: str | None) -> dict:
    """Ray grid from a view direction over the bounds: nearest-surface height per cell."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    view = str(params.get("view") or "top").lower()
    dirs = {"top": (2, -1), "bottom": (2, 1), "front": (1, 1), "back": (1, -1), "left": (0, 1), "right": (0, -1)}
    if view not in dirs:
        raise ValueError(f"view must be one of {', '.join(dirs)}")
    axis, sign = dirs[view]
    others = [i for i in range(3) if i != axis]
    res = max(8, min(128, int(params.get("res") or 48)))
    lo, hi = arrays.bounds()
    region = params.get("region")
    if isinstance(region, dict) and "min" in region and "max" in region:
        lo = np.maximum(lo, np.asarray(region["min"], float))
        hi = np.minimum(hi, np.asarray(region["max"], float))
    tree = BVHTree.FromPolygons(arrays.verts.tolist(), arrays.tris.tolist(), all_triangles=True)
    span = hi - lo
    aspect = span[others[1]] / max(span[others[0]], 1e-12)
    cols = res
    rows = max(4, min(128, int(round(res * aspect))))
    start = hi[axis] + span[axis] * 0.01 + 1e-9 if sign < 0 else lo[axis] - span[axis] * 0.01 - 1e-9
    direction = Vector([0, 0, 0])
    direction[axis] = float(sign)
    grid = np.full((rows, cols), np.nan)
    for r in range(rows):
        for c in range(cols):
            origin = [0.0, 0.0, 0.0]
            origin[others[0]] = lo[others[0]] + (c + 0.5) / cols * span[others[0]]
            origin[others[1]] = hi[others[1]] - (r + 0.5) / rows * span[others[1]]
            origin[axis] = start
            hit, _n, _i, _d = tree.ray_cast(Vector(origin), direction, float(span[axis] * 1.05 + 1e-9))
            if hit is not None:
                grid[r, c] = hit[axis]
    finite = grid[np.isfinite(grid)]
    if not len(finite):
        return {"text": "nothing hit", "rows": rows, "cols": cols}
    gmin, gmax = float(finite.min()), float(finite.max())
    ramp = " .:-=+*#%@"
    ascii_rows = []
    for r in range(rows):
        line = ""
        for c in range(cols):
            vval = grid[r, c]
            if not np.isfinite(vval):
                line += " "
            else:
                t = (vval - gmin) / (gmax - gmin) if gmax > gmin else 1.0
                t = t if sign < 0 else 1.0 - t  # nearer to the viewer is denser
                line += ramp[1 + int(round(t * (len(ramp) - 2)))]
        ascii_rows.append(line.rstrip())
    result = {"view": view, "rows": rows, "cols": cols, "near": gmax if sign < 0 else gmin, "far": gmin if sign < 0 else gmax,
              "cell": [round(float(span[others[0]] / cols), 6), round(float(span[others[1]] / rows), 6)], "grid": grid,
              "text": f"depth map from {view}, {cols}x{rows} cells, {'xyz'[axis]} {meshdata.fmt_num(gmin, 5)}..{meshdata.fmt_num(gmax, 5)} "
                      f"(denser characters are nearer):\n" + "\n".join(ascii_rows)}
    if out:
        from . import sheet

        img = np.zeros((rows, cols, 4), dtype=np.float32)
        t = np.where(np.isfinite(grid), (grid - gmin) / max(gmax - gmin, 1e-12), 0.0)
        if sign > 0:
            t = 1.0 - t
        img[..., 0] = img[..., 1] = img[..., 2] = np.where(np.isfinite(grid), 0.15 + 0.85 * t, 0.0)
        img[..., 3] = 1.0
        scale = max(1, 512 // max(rows, cols))
        img = np.kron(img, np.ones((scale, scale, 1), dtype=np.float32))
        sheet.save(out, img)
        result["file"] = out
    return result


# ----------------------------------------------------------------------------- angular, pitch
def angular(arrays, params: dict, frame: Frame) -> dict:
    """Occupancy (surface present) per angle bin, inside the radius and height windows. segment folds
    every `segment` degrees onto one (so a repeated detail is measured once)."""
    pts = _surface_samples(arrays, int(params.get("samples") or 80000))
    keep = _mask(frame, pts, params)
    r, ang, _depth = frame.polar(pts[keep])
    segment = float(params.get("segment") or 0.0)
    window = _pair(params.get("angle"), "angle")
    if segment:
        start = float(params.get("segment_start") if params.get("segment_start") is not None else (window[0] if window else 0.0))
        ang = (ang - start) % segment
        lo_a, hi_a = 0.0, segment
    elif window:
        start = window[0]
        ang = (ang - start) % 360.0
        lo_a, hi_a = 0.0, (window[1] - window[0]) % 360.0 or 360.0
    else:
        start, lo_a, hi_a = 0.0, 0.0, 360.0
    step = float(params.get("step") or 0.0)
    bins = int(round((hi_a - lo_a) / step)) if step else int(params.get("bins") or min(3600, max(36, int((hi_a - lo_a) * 4))))
    bins = max(4, min(36000, bins))
    hist, edges = np.histogram(ang, bins=bins, range=(lo_a, hi_a))
    occupied = hist > max(1, hist.max() * 0.05) if hist.max() else hist > 0
    runs = []
    i = 0
    width = (hi_a - lo_a) / bins
    while i < bins:
        if occupied[i]:
            j = i
            while j + 1 < bins and occupied[j + 1]:
                j += 1
            runs.append([round(start + lo_a + i * width, 4), round(start + lo_a + (j + 1) * width, 4)])
            i = j + 1
        else:
            i += 1
    full = not segment and not window
    if full and len(runs) > 1 and runs[0][0] == 0.0 and runs[-1][1] == 360.0:
        runs[0] = [runs[-1][0] - 360.0, runs[0][1]]
        runs.pop()
    where = f"segment of {segment:g}° from {start:g}°" if segment else (f"window {window[0]:g}..{window[1]:g}°" if window else "full turn")
    text = f"{len(runs)} occupied arc(s), {where}, {bins} bins of {width:.3g}° ({int(keep.sum())} samples in the band; {frame.label})"
    limit = int(params.get("max_arcs") or 80)
    if runs:
        text += ": " + ", ".join(f"{a:g}..{b:g}°" for a, b in runs[:limit]) + (f" ... (+{len(runs) - limit}; narrow the window)" if len(runs) > limit else "")
    return {"bins": bins, "occupied": runs, "histogram": hist.tolist(), "edges": edges.tolist(), "start": start, "text": text}


def pitch(arrays, params: dict, frame: Frame) -> dict:
    data = angular(arrays, dict(params, bins=int(params.get("bins") or 1440), angle=None, segment=None), frame)
    hist = np.asarray(data["histogram"], float)
    # Occupancy, not sample counts: side faces put spikes at every edge, which favours harmonics.
    occ = (hist > max(1.0, hist.max() * 0.05)).astype(float) if hist.max() else hist
    filled = float((occ > 0).mean()) if len(occ) else 0.0
    if filled > 0.95 or filled < 0.02:
        return {"period_deg": None, "occupied": round(filled, 3),
                "text": f"no repeating pattern: {filled * 100:.0f}% of the angles are occupied in this band. Narrow the radius "
                        "[r0, r1] or height [h0, h1] band to the repeating feature (teeth, glyphs, holes)."}
    occ = occ - occ.mean()
    n = len(occ)
    power = np.abs(np.fft.rfft(occ)) ** 2
    power[0] = 0.0
    if n <= 4 or power.max() <= 0:
        return {"text": "no repeating pattern found", "period_deg": None}
    # The fundamental: the lowest frequency with at least half the strongest peak's power.
    strong = np.nonzero(power[1:n // 2] >= power[1:n // 2].max() * 0.5)[0]
    k = int(strong[0] + 1)
    if k < 2:
        return {"period_deg": None, "text": "no repeating pattern: the band has one large gap or feature, not a repeat"}
    period = 360.0 / k
    strength = float(power[k] / max(power.sum(), 1e-12))
    return {"period_deg": round(period, 4), "count": k, "strength": round(strength, 3),
            "text": f"repeats {k} times around: every {period:.4f}° (strength {strength:.2f} of the signal)"}


# ----------------------------------------------------------------------------- images
COLOURS = [(0.95, 0.95, 0.95, 1.0), (1.0, 0.47, 0.24, 1.0), (0.35, 0.78, 1.0, 1.0)]


def _nice_step(span: float, lines: int = 12) -> float:
    raw = max(span, 1e-12) / lines
    mag = 10 ** math.floor(math.log10(raw))
    for k in (1, 2, 5, 10):
        if raw <= k * mag:
            return k * mag
    return 10 * mag


class Plot:
    """A drawing in (u, v) with a labelled grid, for sections and profiles."""

    def __init__(self, lo, hi, longest: int = 1024, labels=("u", "v")):
        span = np.maximum(np.asarray(hi, float) - np.asarray(lo, float), 1e-9)
        pad = span * 0.04
        self.lo = np.asarray(lo, float) - pad
        self.hi = np.asarray(hi, float) + pad
        span = self.hi - self.lo
        scale = longest / float(span.max())
        self.w = max(64, int(round(span[0] * scale)))
        self.h = max(64, int(round(span[1] * scale)))
        self.px = scale
        self.img = np.zeros((self.h, self.w, 4), dtype=np.float32)
        self.img[..., :3] = 0.07
        self.img[..., 3] = 1.0
        self.labels = labels
        # Grid lines about 12 pixels apart: a 1 cm grid on a gate's 80 cm cross-section.
        self.step = _nice_step(float(max(span)), lines=max(8, longest // 12))

    def _xy(self, pts):
        pts = np.asarray(pts, float)
        x = ((pts[..., 0] - self.lo[0]) * self.px).astype(int)
        y = ((self.hi[1] - pts[..., 1]) * self.px).astype(int)
        return x, y

    def grid(self) -> None:
        from . import sheet

        step = self.step
        for axis in (0, 1):
            first = math.ceil(self.lo[axis] / step)
            last = math.floor(self.hi[axis] / step)
            for k in range(first, last + 1):
                value = k * step
                major = k % 5 == 0
                shade = 0.32 if major else 0.17
                if axis == 0:
                    x = int((value - self.lo[0]) * self.px)
                    if 0 <= x < self.w:
                        self.img[:, x, :3] = np.maximum(self.img[:, x, :3], shade)
                        if major:
                            sheet.draw_text(self.img, meshdata.fmt_num(value, 6), x + 3, self.h - 16, 1, (0.6, 0.6, 0.6, 1.0))
                else:
                    y = int((self.hi[1] - value) * self.px)
                    if 0 <= y < self.h:
                        self.img[y, :, :3] = np.maximum(self.img[y, :, :3], shade)
                        if major:
                            sheet.draw_text(self.img, meshdata.fmt_num(value, 6), 3, max(0, y - 9), 1, (0.6, 0.6, 0.6, 1.0))
        sheet.draw_text(self.img, f"{self.labels[0]} across, {self.labels[1]} up; grid {meshdata.fmt_num(step, 6)}, "
                        f"bold every {meshdata.fmt_num(step * 5, 6)}", 6, 4, 1, (0.85, 0.85, 0.85, 1.0))

    def segments(self, seg, colour) -> None:
        seg = np.asarray(seg, float)
        if not len(seg):
            return
        lengths = np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1) * self.px
        for s, n in zip(seg, np.maximum(2, (lengths * 1.5).astype(int))):
            t = np.linspace(0.0, 1.0, int(min(n, 4000)))[:, None]
            x, y = self._xy(s[0] + (s[1] - s[0]) * t)
            ok = (x >= 0) & (x < self.w) & (y >= 0) & (y < self.h)
            self.img[y[ok], x[ok]] = colour

    def legend(self, items) -> None:
        from . import sheet

        y = 16
        for text, colour in items:
            sheet.draw_text(self.img, text, 6, y, 1, colour)
            y += 11

    def save(self, out: str) -> str:
        from . import sheet

        sheet.save(out, self.img)
        return out


def _profile_segments(result) -> np.ndarray:
    """A profile as vertical bars (one per bin, min to max), for drawing."""
    segs = []
    for a, b, mn, mx, n in result["rows"]:
        if not n:
            continue
        x = (a + b) / 2
        segs.append([[x, mn], [x, mx]])
    return np.array(segs) if segs else np.zeros((0, 2, 2))


def _draw(results: list, labels: list, out: str, kind: str) -> str:
    seg_sets = [r["segments"] if kind == "section" else _profile_segments(r) for r in results]
    pts = np.concatenate([s.reshape(-1, 2) for s in seg_sets if len(s)]) if any(len(s) for s in seg_sets) else np.zeros((1, 2))
    plot = Plot(pts.min(axis=0), pts.max(axis=0), labels=results[0].get("axes_names") or ("u", "v"))
    plot.grid()
    for i, segs in enumerate(seg_sets):
        plot.segments(segs, COLOURS[i % len(COLOURS)])
    plot.legend([(text, COLOURS[i % len(COLOURS)]) for i, text in enumerate(labels)])
    return plot.save(out)


# ----------------------------------------------------------------------------- measure
def _differences(op: str, a: dict, b: dict, label_a: str, label_b: str) -> str:
    fmt = meshdata.fmt_num
    if op == "section":
        lines = []
        for key in ("width", "depth", "area"):
            if isinstance(a.get(key), (int, float)) and isinstance(b.get(key), (int, float)):
                lines.append(f"{key} {fmt(a[key], 6)} vs {fmt(b[key], 6)} (difference {fmt(a[key] - b[key], 6)})")
        dev = _deviation(a.get("segments", np.zeros((0, 2, 2))), b.get("segments", np.zeros((0, 2, 2))))
        back = _deviation(b.get("segments", np.zeros((0, 2, 2))), a.get("segments", np.zeros((0, 2, 2))))
        if dev and back:
            lines.append(f"outline distance: {label_a} from {label_b} at most {fmt(dev['max'], 6)} (95% within {fmt(dev['p95'], 6)}), "
                         f"worst at ({fmt(dev['at'][0], 5)}, {fmt(dev['at'][1], 5)}); {label_b} from {label_a} at most "
                         f"{fmt(back['max'], 6)} (95% within {fmt(back['p95'], 6)}), worst at ({fmt(back['at'][0], 5)}, {fmt(back['at'][1], 5)})")
        return "differences: " + "; ".join(lines) if lines else ""
    if op == "profile":
        rows = []
        worst = None
        for ra, rb in zip(a["rows"], b["rows"]):
            if not ra[4] or not rb[4]:
                rows.append(f"  {fmt(ra[0], 5)}..{fmt(ra[1], 5)}: " + ("only in " + (label_a if ra[4] else label_b) if ra[4] or rb[4] else "empty in both"))
                continue
            dmin, dmax = ra[2] - rb[2], ra[3] - rb[3]
            rows.append(f"  {fmt(ra[0], 5)}..{fmt(ra[1], 5)}: min {fmt(ra[2], 6)} vs {fmt(rb[2], 6)} ({fmt(dmin, 6)}), "
                        f"max {fmt(ra[3], 6)} vs {fmt(rb[3], 6)} ({fmt(dmax, 6)})")
            for d, side in ((dmin, "min"), (dmax, "max")):
                if worst is None or abs(d) > abs(worst[0]):
                    worst = (d, side, ra[0], ra[1])
        head = f"differences per bin ({label_a} vs {label_b}, the same bins):"
        tail = f"largest deviation: {fmt(worst[0], 6)} in the {worst[1]} at {fmt(worst[2], 5)}..{fmt(worst[3], 5)}" if worst else ""
        return "\n".join([head, *rows, tail])
    if op in ("angular",):
        sa = {tuple(x) for x in a.get("occupied", [])}
        sb = {tuple(x) for x in b.get("occupied", [])}
        ha, hb = np.asarray(a["histogram"]) > 0, np.asarray(b["histogram"]) > 0
        if ha.shape == hb.shape:
            agree = float((ha == hb).mean() * 100)
            return (f"differences: {len(sa)} vs {len(sb)} arcs; the occupancy agrees in {agree:.1f}% of the bins; "
                    f"only in {label_a}: {int((ha & ~hb).sum())} bins, only in {label_b}: {int((hb & ~ha).sum())} bins")
        return f"differences: {len(sa)} vs {len(sb)} arcs"
    if op == "pitch" and a.get("period_deg") and b.get("period_deg"):
        return f"differences: period {fmt(a['period_deg'], 4)}° vs {fmt(b['period_deg'], 4)}° ({fmt(a['period_deg'] - b['period_deg'], 4)}°)"
    if op == "depthmap" and "grid" in a and "grid" in b and np.shape(a["grid"]) == np.shape(b["grid"]):
        diff = np.asarray(a["grid"]) - np.asarray(b["grid"])
        finite = diff[np.isfinite(diff)]
        if len(finite):
            return f"differences: height {fmt(float(np.abs(finite).max()), 6)} at most, mean {fmt(float(np.abs(finite).mean()), 6)} over {len(finite)} cells"
    return ""


def measure(params: dict, root: str) -> dict:
    op = str(params.get("op") or "section").strip().lower()
    if op not in OPS:
        raise ValueError(f"op must be one of {', '.join(OPS)}, not {params.get('op')!r}")
    roots = [r for r in params.get("reference_roots") or [] if isinstance(r, str)]
    frame = Frame(params)
    warnings = []
    arrays, label, given = _source_arrays(params, root, roots)
    if not given:
        _unit_warning(arrays, label, warnings)
    if op in ("section", "depthmap"):
        arrays = _restrict(arrays, frame, params)

    def run(arr, out=None, edges=None):
        if op == "depthmap":
            return depthmap(arr, params, out)
        if op == "section":
            return section(arr, params, frame)
        if op == "profile":
            return profile(arr, params, frame, edges)
        return angular(arr, params, frame) if op == "angular" else pitch(arr, params, frame)

    result = run(arrays, params.get("out") if op == "depthmap" else None)
    result["source"] = label
    result["units"] = units_mod.label()
    result["text"] = f"{label}: {result['text']}"
    other = params.get("compare_to")
    second = None
    if other:
        spec = other if isinstance(other, dict) else {"targets": other}
        other_arrays, other_label, other_given = _source_arrays(spec, root, roots)
        if not other_given:
            _unit_warning(other_arrays, other_label, warnings)
        if op in ("section", "depthmap"):
            other_arrays = _restrict(other_arrays, frame, params)
        if op == "profile":
            # The same bins for both: over both ranges unless min and max were given.
            if params.get("min") is None or params.get("max") is None:
                pa = profile(arrays, dict(params, bins=2), frame)
                pb = profile(other_arrays, dict(params, bins=2), frame)
                lo = float(params["min"]) if params.get("min") is not None else min(pa["edges"][0], pb["edges"][0])
                hi = float(params["max"]) if params.get("max") is not None else max(pa["edges"][-1], pb["edges"][-1])
                edges = np.linspace(lo, hi, max(1, min(400, int(params.get("bins") or 24))) + 1)
                result = profile(arrays, params, frame, edges)
                result["source"], result["units"] = label, units_mod.label()
                result["text"] = f"{label}: {result['text']}"
            second = profile(other_arrays, params, frame, result["edges"])
        else:
            second = run(other_arrays)
        second["source"] = other_label
        diff = _differences(op, result, second, label, other_label)
        result["text"] += f"\n{other_label}: {second['text']}" + (f"\n{diff}" if diff else "")
        result["compare_to"] = {k: v for k, v in second.items() if k not in ("segments", "grid", "edges")}
    if params.get("image") and params.get("out") and op in ("section", "profile"):
        sets = [result] + ([second] if second else [])
        names = [label] + ([second["source"]] if second else [])
        result["file"] = _draw(sets, names, params["out"], op)
    for key in ("segments", "grid", "edges"):
        result.pop(key, None)
    if warnings:
        result["text"] += "\nwarnings: " + "; ".join(warnings)
    result["text"] += f"\nunits: {result['units']}"
    return result

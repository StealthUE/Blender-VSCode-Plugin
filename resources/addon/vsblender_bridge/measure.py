"""measure: shapes, not just bounds. Read-only, on scene objects (evaluated) or external mesh files.

op section    outline of a slice: plane {axis: z, at: 1.2} or {point, normal}; width, depth, area, points
op profile    min/max of one axis binned along another axis or a radius (a ring's cross-section)
op depthmap   height map over the target seen from a view (top, front...): ASCII rows, optional PNG
op angular    occupancy against angle about an axis, in a radius band (finds notches, gaps, repeats)
op pitch      the repeat period of the angular occupancy (teeth every 9.23 degrees)

file=path (+ transform {location, rotation_deg, scale, units}) measures a reference without importing
it (STL, OBJ, 3MF, SVG); units default to mm. compare_to: a second source (targets or file), measured
the same way, with the differences. Results are in scene units, with the unit label.
"""
from __future__ import annotations

import math
import os

import numpy as np

import bpy

from . import meshdata, units as units_mod

AXES = {"x": 0, "y": 1, "z": 2}


def _source_arrays(spec: dict, root: str) -> tuple:
    """(MeshArrays in world scene units, label) for {targets} or {file, transform}."""
    if spec.get("file"):
        from mathutils import Euler, Matrix, Vector

        from . import preview

        path = preview._resolve_file(str(spec["file"]), root)
        transform = spec.get("transform") or {}
        arrays = meshdata.read_mesh_file(path, plane=str(transform.get("plane") or "xy"))
        factor = units_mod.file_to_bu(str(transform.get("units") or spec.get("units") or "mm"))
        scale = transform.get("scale", 1.0)
        scale = Vector(scale) if isinstance(scale, (list, tuple)) else Vector((float(scale),) * 3)
        rot = [math.radians(float(v)) for v in (transform.get("rotation_deg") or [0, 0, 0])][:3]
        loc = Vector([float(v) for v in (transform.get("location") or [0, 0, 0])][:3])
        matrix = Matrix.Translation(loc) @ Euler(rot, "XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*(scale * factor), 1.0))
        return arrays.transformed(matrix), os.path.basename(path)
    from . import inspect_tools

    obs = inspect_tools.resolve_objects(spec.get("targets"), root)
    obs = [o for o in obs if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}]
    if not obs:
        raise ValueError("no geometry to measure: pass targets (names or a find selector) or file")
    deps = bpy.context.evaluated_depsgraph_get()
    arrays = meshdata.concat([meshdata.object_arrays(o, deps) for o in obs])
    label = obs[0].name if len(obs) == 1 else f"{len(obs)} objects"
    return arrays, label


def _plane(params: dict, arrays) -> tuple:
    plane = params.get("plane") or {"axis": "z"}
    if "normal" in plane:
        n = np.asarray(plane["normal"], float)
        n = n / np.linalg.norm(n)
        p = np.asarray(plane.get("point") or [0, 0, 0], float)
    else:
        axis = AXES[str(plane.get("axis") or "z").lower()]
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
        return p, n, u, v
    # In-plane axes: u, v with u x v = n.
    helper = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(helper, n)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return p, n, u, v


def _axis_name(vec) -> str:
    v = np.asarray(vec, float)
    i = int(np.argmax(np.abs(v)))
    return "xyz"[i] if abs(v[i]) > 0.999 else "u"


def section(arrays, params: dict) -> dict:
    from . import geom2d

    p, n, u, v = _plane(params, arrays)
    d = (arrays.verts - p) @ n
    t = arrays.tris
    dt = d[t]
    crosses = (dt.min(axis=1) < 0) & (dt.max(axis=1) > 0)
    segs = []
    for tri, dd in zip(t[crosses], dt[crosses]):
        pts = []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if (dd[a] < 0) != (dd[b] < 0):
                s = dd[a] / (dd[a] - dd[b])
                pts.append(arrays.verts[tri[a]] + s * (arrays.verts[tri[b]] - arrays.verts[tri[a]]))
        if len(pts) == 2:
            segs.append(pts)
    if not segs:
        return {"loops": 0, "text": "the plane does not cut the geometry"}
    seg = np.array(segs)
    uv = np.stack([seg @ u, seg @ v], axis=-1)  # (S, 2, 2)
    loops = _chain(uv)
    loops = [geom2d.simplify(l, float(params.get("tol") or 0.0) or _auto_tol(uv)) for l in loops]
    lo = uv.reshape(-1, 2).min(axis=0)
    hi = uv.reshape(-1, 2).max(axis=0)
    area = geom2d.area([l for l in loops if len(l) >= 3]) if loops else 0.0
    budget = int(params.get("max_points") or 200)
    total = sum(len(l) for l in loops)
    step = max(1, math.ceil(total / budget))
    out_loops = [[[round(float(x), 5), round(float(y), 5)] for x, y in l[::step]] for l in loops]
    plane_text = f"normal {np.round(n, 4).tolist()} through {np.round(p, 5).tolist()}"
    return {"loops": len(loops), "width": round(float(hi[0] - lo[0]), 6), "depth": round(float(hi[1] - lo[1]), 6),
            "area": round(float(area), 6), "min_uv": np.round(lo, 6).tolist(), "max_uv": np.round(hi, 6).tolist(),
            "plane": plane_text, "u_axis": np.round(u, 4).tolist(), "v_axis": np.round(v, 4).tolist(), "points": out_loops,
            "text": f"section ({plane_text}): {len(loops)} loop(s), width {meshdata.fmt_num(hi[0] - lo[0], 5)} along {_axis_name(u)}, "
                    f"depth {meshdata.fmt_num(hi[1] - lo[1], 5)} along {_axis_name(v)}, area {meshdata.fmt_num(area, 5)}"}


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


def _radial(arrays, params: dict):
    """Radius and angle of every vertex about an axis through center."""
    axis = AXES[str(params.get("axis") or "z").lower()]
    center = np.asarray(params.get("center") or [0, 0, 0], float)
    others = [i for i in range(3) if i != axis]
    rel = arrays.verts - center
    a, b = rel[:, others[0]], rel[:, others[1]]
    r = np.hypot(a, b)
    ang = np.degrees(np.arctan2(b, a)) % 360.0
    return r, ang, rel[:, axis]


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


def profile(arrays, params: dict) -> dict:
    """value axis min/max per bin along `along` (x, y, z, or r: radius about axis through center)."""
    pts = _surface_samples(arrays, int(params.get("samples") or 40000))
    along = str(params.get("along") or "r").lower()
    value = AXES[str(params.get("value") or "z").lower()]
    if along == "r":
        axis = AXES[str(params.get("axis") or "z").lower()]
        center = np.asarray(params.get("center") or [0, 0, 0], float)
        others = [i for i in range(3) if i != axis]
        rel = pts - center
        x = np.hypot(rel[:, others[0]], rel[:, others[1]])
    else:
        x = pts[:, AXES[along]]
    y = pts[:, value]
    bins = int(params.get("bins") or 24)
    lo = float(params.get("min", x.min()))
    hi = float(params.get("max", x.max()))
    edges = np.linspace(lo, hi, bins + 1)
    which = np.clip(np.digitize(x, edges) - 1, 0, bins - 1)
    rows = []
    for i in range(bins):
        sel = which == i
        if sel.any():
            rows.append([round(float(edges[i]), 5), round(float(edges[i + 1]), 5), round(float(y[sel].min()), 5),
                         round(float(y[sel].max()), 5), int(sel.sum())])
        else:
            rows.append([round(float(edges[i]), 5), round(float(edges[i + 1]), 5), None, None, 0])
    axis_name = "xyz"[value]
    lines = [f"{axis_name} range per {along} bin ({bins} bins, {len(pts)} surface samples):"]
    for a, b, mn, mx, n in rows:
        lines.append(f"  {along} {meshdata.fmt_num(a, 4)}..{meshdata.fmt_num(b, 4)}: " +
                     (f"{axis_name} {meshdata.fmt_num(mn, 4)}..{meshdata.fmt_num(mx, 4)}" if n else "empty"))
    return {"rows": rows, "columns": [f"{along}_from", f"{along}_to", f"{axis_name}_min", f"{axis_name}_max", "samples"],
            "text": "\n".join(lines)}


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
              "cell": [round(float(span[others[0]] / cols), 6), round(float(span[others[1]] / rows), 6)],
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


def angular(arrays, params: dict) -> dict:
    """Occupancy (surface present) per angle bin about an axis, within a radius band and height band."""
    pts = _surface_samples(arrays, int(params.get("samples") or 60000))
    axis = AXES[str(params.get("axis") or "z").lower()]
    center = np.asarray(params.get("center") or [0, 0, 0], float)
    others = [i for i in range(3) if i != axis]
    rel = pts - center
    r = np.hypot(rel[:, others[0]], rel[:, others[1]])
    ang = np.degrees(np.arctan2(rel[:, others[1]], rel[:, others[0]])) % 360.0
    sel = np.ones(len(pts), dtype=bool)
    band = params.get("radius") or params.get("r")
    if band:
        sel &= (r >= float(band[0])) & (r <= float(band[1]))
    hband = params.get("height")
    if hband:
        sel &= (rel[:, axis] >= float(hband[0])) & (rel[:, axis] <= float(hband[1]))
    bins = int(params.get("bins") or 360)
    hist, _edges = np.histogram(ang[sel], bins=bins, range=(0.0, 360.0))
    occupied = hist > max(1, hist.max() * 0.05) if hist.max() else hist > 0
    runs = []
    i = 0
    while i < bins:
        if occupied[i]:
            j = i
            while j + 1 < bins and occupied[j + 1]:
                j += 1
            runs.append([round(i * 360.0 / bins, 3), round((j + 1) * 360.0 / bins, 3)])
            i = j + 1
        else:
            i += 1
    if len(runs) > 1 and runs[0][0] == 0.0 and runs[-1][1] == 360.0:
        runs[0] = [runs[-1][0] - 360.0, runs[0][1]]
        runs.pop()
    text = f"{len(runs)} occupied arc(s) of {bins} bins ({int(sel.sum())} samples in the band)"
    if runs:
        text += ": " + ", ".join(f"{a:g}..{b:g}°" for a, b in runs[:24]) + (" ..." if len(runs) > 24 else "")
    return {"bins": bins, "occupied": runs, "histogram": hist.tolist(), "text": text}


def pitch(arrays, params: dict) -> dict:
    data = angular(arrays, dict(params, bins=int(params.get("bins") or 1440)))
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


OPS = {"section": section, "profile": profile, "angular": angular, "pitch": pitch}


def measure(params: dict, root: str) -> dict:
    op = str(params.get("op") or "section")
    if op not in OPS and op != "depthmap":
        raise ValueError("op must be section, profile, depthmap, angular or pitch")
    arrays, label = _source_arrays(params, root)

    def run(arr, out=None):
        return depthmap(arr, params, out) if op == "depthmap" else OPS[op](arr, params)

    result = run(arrays, params.get("out"))
    result["source"] = label
    result["units"] = units_mod.label()
    result["text"] = f"{label}: {result['text']}"
    other = params.get("compare_to")
    if other:
        other_arrays, other_label = _source_arrays(other if isinstance(other, dict) else {"targets": other}, root)
        second = run(other_arrays)
        second["source"] = other_label
        result["compare_to"] = second
        diffs = []
        for key in ("width", "depth", "area", "period_deg"):
            if isinstance(result.get(key), (int, float)) and isinstance(second.get(key), (int, float)):
                diffs.append(f"{key} {meshdata.fmt_num(result[key], 5)} vs {meshdata.fmt_num(second[key], 5)} "
                             f"(difference {meshdata.fmt_num(result[key] - second[key], 5)})")
        result["text"] += f"\n{other_label}: {second['text']}" + ("\ndifferences: " + "; ".join(diffs) if diffs else "")
    result["text"] += f"\nunits: {result['units']}"
    return result

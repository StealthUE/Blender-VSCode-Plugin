"""2D outlines: SVG to loops, even-odd triangulation, simple shapes, offsets.

A loop is an (N, 2) float array, implicitly closed. Regions are given by several loops under the
even-odd rule (a loop inside another is a hole), which is what SVG fills and glyph outlines use.

    svg_loops(path, size=40)      SVG fills and strokes -> outline loops, y up, optionally scaled
    triangulate(loops)            -> (points, triangles) counter-clockwise, holes left open
    circle(d), rect(w, h, r), regular(n, d), polygon(points), offset(loops, delta)

triangulate uses mathutils.geometry.delaunay_2d_cdt and keeps the triangles inside by even-odd:
tessellate_polygon mis-fills concave outlines (a "Λ" comes out as a solid triangle).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

import numpy as np

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TOK = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_INHERITED = ("fill", "stroke", "stroke-width", "stroke-linecap", "fill-rule", "fill-opacity", "stroke-opacity",
              "display", "visibility")


# ----------------------------------------------------------------------------- shapes
def circle(d: float, n: int | None = None, center=(0.0, 0.0)) -> np.ndarray:
    """A circle of diameter d as a counter-clockwise loop. n defaults to a smooth 64."""
    n = int(n or 64)
    t = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return np.column_stack([center[0] + d / 2 * np.cos(t), center[1] + d / 2 * np.sin(t)])


def regular(n: int, d: float, center=(0.0, 0.0), rotation_deg: float = 0.0) -> np.ndarray:
    """Regular polygon with n sides inscribed in a circle of diameter d (a hexagon: across corners)."""
    t = np.linspace(0.0, 2.0 * math.pi, int(n), endpoint=False) + math.radians(rotation_deg)
    return np.column_stack([center[0] + d / 2 * np.cos(t), center[1] + d / 2 * np.sin(t)])


def rect(w: float, h: float, r: float = 0.0, center: bool = True, segments: int = 8) -> np.ndarray:
    """Rectangle w by h, corners rounded with radius r, counter-clockwise."""
    x0, y0 = (-w / 2, -h / 2) if center else (0.0, 0.0)
    x1, y1 = x0 + w, y0 + h
    r = max(0.0, min(float(r), w / 2, h / 2))
    if r <= 0:
        return np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], float)
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90.0), (x1 - r, y1 - r, 0.0), (x0 + r, y1 - r, 90.0), (x0 + r, y0 + r, 180.0)):
        for i in range(segments + 1):
            a = math.radians(a0 + 90.0 * i / segments)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return np.array(pts, float)


def polygon(points) -> np.ndarray:
    return np.asarray(points, float).reshape(-1, 2)


def signed_area(loop) -> float:
    p = np.asarray(loop, float)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def area(loops) -> float:
    """Area of a region given by loops under the even-odd rule."""
    loops = [np.asarray(l, float) for l in loops]
    total = 0.0
    for i, lp in enumerate(loops):
        depth = sum(1 for j, other in enumerate(loops) if j != i and inside_evenodd(lp[:1, 0], lp[:1, 1], [other])[0])
        total += abs(signed_area(lp)) * (1 if depth % 2 == 0 else -1)
    return total


def orient(loops) -> list:
    """Outer loops counter-clockwise, holes clockwise (by nesting depth)."""
    loops = [np.asarray(l, float) for l in loops]
    out = []
    for i, lp in enumerate(loops):
        depth = sum(1 for j, other in enumerate(loops) if j != i and inside_evenodd(lp[:1, 0], lp[:1, 1], [other])[0])
        ccw = signed_area(lp) > 0
        want_ccw = depth % 2 == 0
        out.append(lp if ccw == want_ccw else lp[::-1].copy())
    return out


def resample(loop, n: int) -> np.ndarray:
    """n points evenly spaced by arc length along a closed loop, starting at its first point."""
    p = np.asarray(loop, float)
    closed = np.vstack([p, p[:1]])
    seg = np.hypot(*np.diff(closed, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.linspace(0.0, s[-1], int(n), endpoint=False)
    return np.column_stack([np.interp(t, s, closed[:, 0]), np.interp(t, s, closed[:, 1])])


def bounds(loops) -> tuple:
    pts = np.vstack([np.asarray(l, float) for l in loops])
    return pts.min(axis=0), pts.max(axis=0)


def transform(loops, scale=1.0, offset=(0.0, 0.0), rotate_deg: float = 0.0, mirror_x: bool = False) -> list:
    c, s = math.cos(math.radians(rotate_deg)), math.sin(math.radians(rotate_deg))
    out = []
    for lp in loops:
        p = np.asarray(lp, float) * scale
        if mirror_x:
            p = p * np.array([-1.0, 1.0])
            p = p[::-1]
        p = p @ np.array([[c, s], [-s, c]]) + np.asarray(offset, float)
        out.append(p)
    return out


# ----------------------------------------------------------------------------- even-odd and triangulation
def inside_evenodd(px, py, loops) -> np.ndarray:
    px = np.asarray(px, float)
    py = np.asarray(py, float)
    inside = np.zeros(np.shape(px), bool)
    for lp in loops:
        p = np.asarray(lp, float)
        a = p
        b = np.roll(p, -1, axis=0)
        for (ax, ay), (bx, by) in zip(a, b):
            cond = (ay > py) != (by > py)
            dy = (by - ay) if by != ay else 1e-12
            xi = ax + (py - ay) * (bx - ax) / dy
            inside ^= cond & (px < xi)
    return inside


def triangulate(loops, eps: float = 1e-9):
    """Constrained Delaunay triangulation of the region the loops bound (even-odd).

    Returns (points (N, 2), triangles (M, 3) int), every triangle counter-clockwise.
    """
    from mathutils import Vector
    from mathutils.geometry import delaunay_2d_cdt

    coords, edges = [], []
    for lp in loops:
        p = np.asarray(lp, float)
        if len(p) > 1 and np.allclose(p[0], p[-1]):
            p = p[:-1]
        if len(p) < 3:
            continue
        base = len(coords)
        coords.extend(Vector((float(x), float(y))) for x, y in p)
        n = len(p)
        edges.extend((base + k, base + (k + 1) % n) for k in range(n))
    if not coords:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    span = max(1e-12, float(np.ptp(np.array([(v.x, v.y) for v in coords]), axis=0).max()))
    out = delaunay_2d_cdt(coords, edges, [], 0, span * eps, True)
    verts, faces = out[0], out[2]
    pts = np.array([(v.x, v.y) for v in verts], float)
    tris = np.array([f for f in faces if len(f) == 3], dtype=np.int64).reshape(-1, 3)
    if len(tris):
        c = pts[tris].mean(axis=1)
        keep = inside_evenodd(c[:, 0], c[:, 1], loops)
        tris = tris[keep]
        a, b, cc = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
        cross = (b[:, 0] - a[:, 0]) * (cc[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (cc[:, 0] - a[:, 0])
        flip = cross < 0
        tris[flip] = tris[flip][:, ::-1]
        tris = tris[np.abs(cross) > 0]
    return pts, tris


def boundary_edges(tris) -> np.ndarray:
    """Directed edges used by exactly one triangle: the outline, oriented with the region on the left."""
    tris = np.asarray(tris, dtype=np.int64)
    if not len(tris):
        return np.zeros((0, 2), dtype=np.int64)
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    key = np.sort(e, axis=1)
    _u, inv, counts = np.unique(key[:, 0] * (int(e.max()) + 1) + key[:, 1], return_inverse=True, return_counts=True)
    return e[counts[inv] == 1]


# ----------------------------------------------------------------------------- offsets
def offset(loops, delta: float, res: float | None = None, tol: float | None = None) -> list:
    """Grow (delta > 0) or shrink (delta < 0) a region by a distance, with round corners.

    Traced from a distance field, so overlapping or touching results merge cleanly.
    """
    loops = [np.asarray(l, float) for l in loops]
    lo, hi = bounds(loops)
    size = float(max(hi - lo))
    res = float(res or size / 300.0)
    pad = abs(delta) + 3 * res
    xs = np.arange(lo[0] - pad, hi[0] + pad + res, res)
    ys = np.arange(lo[1] - pad, hi[1] + pad + res, res)
    px, py = np.meshgrid(xs, ys)
    F = _region_field(loops, px, py) + delta
    F[0, :] = F[-1, :] = F[:, 0] = F[:, -1] = -1.0
    tol = float(tol if tol is not None else res * 0.25)
    return orient([simplify(l, tol) for l in _march(F, xs, ys)])


def _region_field(loops, px, py) -> np.ndarray:
    """Signed distance to the region boundary, positive inside (even-odd)."""
    dist = np.full(px.shape, 1e18)
    for lp in loops:
        p = np.asarray(lp, float)
        for a, b in zip(p, np.roll(p, -1, axis=0)):
            dist = np.minimum(dist, _seg_dist(px, py, a, b))
    inside = inside_evenodd(px, py, loops)
    return np.where(inside, dist, -dist)


# ----------------------------------------------------------------------------- SVG parsing
def _style(el, inherited: dict) -> dict:
    out = {k: v for k, v in inherited.items() if k in _INHERITED}
    for k in _INHERITED:
        if el.get(k) is not None:
            out[k] = el.get(k)
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _transform(text) -> np.ndarray:
    m = np.identity(3)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in _NUM.findall(args)]
        t = np.identity(3)
        if name == "translate":
            t[0, 2], t[1, 2] = v[0], (v[1] if len(v) > 1 else 0.0)
        elif name == "matrix" and len(v) >= 6:
            t[:2, :] = [[v[0], v[2], v[4]], [v[1], v[3], v[5]]]
        elif name == "scale":
            t[0, 0], t[1, 1] = v[0], (v[1] if len(v) > 1 else v[0])
        elif name == "rotate":
            a = math.radians(v[0])
            r = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
            if len(v) == 3:
                c1 = np.array([[1, 0, v[1]], [0, 1, v[2]], [0, 0, 1]])
                c2 = np.array([[1, 0, -v[1]], [0, 1, -v[2]], [0, 0, 1]])
                r = c1 @ r @ c2
            t = r
        elif name == "skewX":
            t[0, 1] = math.tan(math.radians(v[0]))
        elif name == "skewY":
            t[1, 0] = math.tan(math.radians(v[0]))
        m = m @ t
    return m


def _arc(p0, rx, ry, phi, large, sweep, p1, steps_per_rad=8):
    if rx == 0 or ry == 0:
        return [p1]
    phi = math.radians(phi)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cx1, cy1 = co * rx * y1 / ry, -co * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = ang(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(4, int(abs(dt) * steps_per_rad * 2))
    pts = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return pts


def _bezier(p0, p1, p2, p3, n=16):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def parse_path(d: str) -> list:
    """SVG path data -> [(points, closed)]. M L H V C S Q T A Z, absolute and relative."""
    toks = _TOK.findall(d or "")
    subs, pts = [], []
    cur = (0.0, 0.0)
    start = cur
    cmd = None
    i = 0
    last_c = None  # previous cubic control point (for S)
    last_q = None  # previous quadratic control point (for T)

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if pts:
                    subs.append((pts, True))
                pts = []
                cur = start
                last_c = last_q = None
                continue
        if cmd is None:
            raise ValueError("path data must start with a command")
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = cur if rel else (0.0, 0.0)
        if c == "M":
            if pts:
                subs.append((pts, False))
            cur = (ox + num(), oy + num())
            start = cur
            pts = [cur]
            cmd = "l" if rel else "L"
            last_c = last_q = None
        elif c == "L":
            cur = (ox + num(), oy + num())
            pts.append(cur)
            last_c = last_q = None
        elif c == "H":
            cur = ((ox if rel else 0.0) + num(), cur[1])
            pts.append(cur)
            last_c = last_q = None
        elif c == "V":
            cur = (cur[0], (oy if rel else 0.0) + num())
            pts.append(cur)
            last_c = last_q = None
        elif c == "C":
            p1 = (ox + num(), oy + num())
            p2 = (ox + num(), oy + num())
            p3 = (ox + num(), oy + num())
            pts.extend(_bezier(cur, p1, p2, p3))
            last_c, last_q, cur = p2, None, p3
        elif c == "S":
            p1 = (2 * cur[0] - last_c[0], 2 * cur[1] - last_c[1]) if last_c else cur
            p2 = (ox + num(), oy + num())
            p3 = (ox + num(), oy + num())
            pts.extend(_bezier(cur, p1, p2, p3))
            last_c, last_q, cur = p2, None, p3
        elif c in "QT":
            if c == "Q":
                q = (ox + num(), oy + num())
            else:
                q = (2 * cur[0] - last_q[0], 2 * cur[1] - last_q[1]) if last_q else cur
            p3 = (ox + num(), oy + num())
            p1 = (cur[0] + 2 / 3 * (q[0] - cur[0]), cur[1] + 2 / 3 * (q[1] - cur[1]))
            p2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
            pts.extend(_bezier(cur, p1, p2, p3))
            last_q, last_c, cur = q, None, p3
        elif c == "A":
            rx, ry, phi, large, sweep = num(), num(), num(), num(), num()
            p1 = (ox + num(), oy + num())
            pts.extend(_arc(cur, rx, ry, phi, int(large), int(sweep), p1))
            cur = p1
            last_c = last_q = None
        else:
            raise ValueError(f"unsupported path command {cmd}")
    if pts:
        subs.append((pts, False))
    return subs


def _ellipse(cx, cy, rx, ry, n=64):
    t = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
    return list(zip(cx + rx * np.cos(t), cy + ry * np.sin(t)))


def _length(value, default=0.0) -> float:
    if value is None:
        return default
    found = _NUM.findall(str(value))
    return float(found[0]) if found else default


def _element_subpaths(el) -> list:
    tag = el.tag.rsplit("}", 1)[-1]
    if tag == "path":
        return parse_path(el.get("d") or "")
    if tag == "rect":
        x, y = _length(el.get("x")), _length(el.get("y"))
        w, h = _length(el.get("width")), _length(el.get("height"))
        rx = _length(el.get("rx"), -1.0)
        ry = _length(el.get("ry"), -1.0)
        r = max(rx, ry, 0.0)
        if w <= 0 or h <= 0:
            return []
        loop = rect(w, h, r, center=False) + np.array([x, y])
        return [(list(map(tuple, loop)), True)]
    if tag == "circle":
        r = _length(el.get("r"))
        return [(_ellipse(_length(el.get("cx")), _length(el.get("cy")), r, r), True)] if r > 0 else []
    if tag == "ellipse":
        rx, ry = _length(el.get("rx")), _length(el.get("ry"))
        return [(_ellipse(_length(el.get("cx")), _length(el.get("cy")), rx, ry), True)] if rx > 0 and ry > 0 else []
    if tag == "line":
        return [([(_length(el.get("x1")), _length(el.get("y1"))), (_length(el.get("x2")), _length(el.get("y2")))], False)]
    if tag in ("polyline", "polygon"):
        v = [float(x) for x in _NUM.findall(el.get("points") or "")]
        pts = list(zip(v[0::2], v[1::2]))
        return [(pts, tag == "polygon")] if len(pts) >= 2 else []
    return []


def load_shapes(path: str) -> list:
    """SVG file -> shapes {kind: fill|stroke, subpaths: [(Nx2, closed)], width, cap}, in SVG user units.

    Group transforms and inherited styles are applied. Hidden elements (display:none) are skipped.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    shapes = []

    def walk(el, matrix, inherited):
        style = _style(el, inherited)
        if style.get("display") == "none" or style.get("visibility") == "hidden":
            return
        m = matrix @ _transform(el.get("transform"))
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("defs", "clipPath", "mask", "pattern", "symbol", "metadata", "title", "desc", "style", "text"):
            return
        subs = []
        for pts, closed in _element_subpaths(el):
            a = np.asarray(pts, float).reshape(-1, 2)
            if len(a) < 2:
                continue
            a = (m[:2, :2] @ a.T).T + m[:2, 2]
            subs.append((a, closed))
        if subs:
            fill = style.get("fill", "black")
            stroke = style.get("stroke", "none")
            if fill not in ("none", "") and float(_length(style.get("fill-opacity"), 1.0)) > 0:
                shapes.append({"kind": "fill", "subpaths": subs})
            if stroke not in ("none", "") and float(_length(style.get("stroke-opacity"), 1.0)) > 0:
                scale = math.sqrt(abs(np.linalg.det(m[:2, :2]))) or 1.0
                shapes.append({"kind": "stroke", "subpaths": subs,
                               "width": _length(style.get("stroke-width"), 1.0) * scale,
                               "cap": style.get("stroke-linecap", "butt")})
        for child in el:
            walk(child, m, style)

    walk(root, np.identity(3), {})
    return shapes


# ----------------------------------------------------------------------------- distance field and contours
def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-24:
        return np.hypot(px - ax, py - ay)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0.0, 1.0)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _box_sd(px, py, a, b, half_w, ext):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return half_w - np.hypot(px - ax, py - ay)
    ux, uy = dx / L, dy / L
    mx, my = (ax + bx) / 2, (ay + by) / 2
    along = (px - mx) * ux + (py - my) * uy
    across = -(px - mx) * uy + (py - my) * ux
    qx = np.abs(along) - (L / 2 + ext)
    qy = np.abs(across) - half_w
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
    inside = np.minimum(np.maximum(qx, qy), 0)
    return -(outside + inside)


def field(shapes, px, py) -> np.ndarray:
    """Union signed distance field of the shapes, positive inside."""
    F = np.full(px.shape, -1e18)
    for sh in shapes:
        if sh["kind"] == "fill":
            loops = [np.asarray(p, float) for p, _c in sh["subpaths"]]
            F = np.maximum(F, _region_field(loops, px, py))
        else:
            w2 = sh["width"] / 2
            cap = sh["cap"]
            for pts, closed in sh["subpaths"]:
                pts = np.asarray(pts, float)
                n = len(pts)
                segs = [(pts[k], pts[(k + 1) % n]) for k in range(n if closed else n - 1)]
                for a, b in segs:
                    F = np.maximum(F, _box_sd(px, py, a, b, w2, 0.0))
                joints = range(n) if closed else range(1, n - 1)
                for k in joints:
                    F = np.maximum(F, w2 - np.hypot(px - pts[k][0], py - pts[k][1]))
                if not closed and segs:
                    for end, (a, b) in ((pts[0], segs[0]), (pts[-1], segs[-1])):
                        if cap == "round":
                            F = np.maximum(F, w2 - np.hypot(px - end[0], py - end[1]))
                        elif cap == "square":
                            F = np.maximum(F, _box_sd(px, py, a, b, w2, w2))
    return F


def _march(F, xs, ys) -> list:
    """Zero-level loops of F (rows = y, cols = x). F must be negative on the border."""
    H, W = F.shape
    pts = {}

    def edge_pt(key):
        if key in pts:
            return pts[key]
        kind, i, j = key
        if kind == "h":
            v0, v1 = F[i, j], F[i, j + 1]
            t = v0 / (v0 - v1)
            p = (xs[j] + t * (xs[j + 1] - xs[j]), ys[i])
        else:
            v0, v1 = F[i, j], F[i + 1, j]
            t = v0 / (v0 - v1)
            p = (xs[j], ys[i] + t * (ys[i + 1] - ys[i]))
        pts[key] = p
        return p

    nbr = {}

    def connect(a, b):
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)

    pos = F > 0
    code = (pos[:-1, :-1].astype(np.int8) | (pos[:-1, 1:] << 1) | (pos[1:, 1:] << 2) | (pos[1:, :-1] << 3))
    table = {1: [("l", "t")], 2: [("t", "r")], 3: [("l", "r")], 4: [("r", "b")], 6: [("t", "b")], 7: [("l", "b")],
             8: [("b", "l")], 9: [("b", "t")], 11: [("b", "r")], 12: [("r", "l")], 13: [("r", "t")], 14: [("t", "l")]}
    for i, j in zip(*np.nonzero((code != 0) & (code != 15))):
        c = int(code[i, j])
        keys = {"t": ("h", i, j), "b": ("h", i + 1, j), "l": ("v", i, j), "r": ("v", i, j + 1)}
        if c in (5, 10):
            centre = (F[i, j] + F[i, j + 1] + F[i + 1, j] + F[i + 1, j + 1]) / 4 > 0
            if (c == 5) == centre:
                segs = [("l", "b"), ("t", "r")] if c == 5 else [("l", "t"), ("r", "b")]
            else:
                segs = [("l", "t"), ("r", "b")] if c == 5 else [("t", "r"), ("b", "l")]
        else:
            segs = table[c]
        for a, b in segs:
            connect(keys[a], keys[b])
    loops = []
    seen = set()
    for startk in nbr:
        if startk in seen:
            continue
        loop = [startk]
        seen.add(startk)
        prev, cur = None, startk
        while True:
            nxt = [k for k in nbr[cur] if k != prev and k not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            loop.append(cur)
        if len(loop) >= 3:
            loops.append(np.array([edge_pt(k) for k in loop]))
    return loops


def simplify(loop, tol: float) -> np.ndarray:
    """Douglas-Peucker on a closed loop."""
    loop = np.asarray(loop, float)
    n = len(loop)
    if n < 8 or tol <= 0:
        return loop
    d = np.hypot(*(loop - loop[0]).T)
    i1 = int(np.argmax(d))

    def dp(pts):
        if len(pts) < 3:
            return pts
        a, b = pts[0], pts[-1]
        ab = b - a
        L = math.hypot(*ab)
        if L < 1e-12:
            dist = np.hypot(*(pts - a).T)
        else:
            dist = np.abs(ab[0] * (pts[:, 1] - a[1]) - ab[1] * (pts[:, 0] - a[0])) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            left = dp(pts[:k + 1])
            right = dp(pts[k:])
            return np.vstack([left[:-1], right])
        return np.vstack([a, b])

    first = dp(loop[:i1 + 1])
    second = dp(np.vstack([loop[i1:], loop[:1]]))
    out = np.vstack([first[:-1], second[:-1]])
    return out if len(out) >= 3 else loop


def _view_box(path: str):
    root = ET.parse(path).getroot()
    v = [float(x) for x in _NUM.findall(root.get("viewBox") or "")]
    return v if len(v) == 4 else None


def svg_loops(path: str, size: float | None = None, res: float | None = None, tol: float | None = None,
              flip_y: bool = True, center: bool = False, mode: str = "auto") -> list:
    """Outline loops of an SVG: the union of its fills and strokes.

    mode auto: files with only fills give their exact polygons (sharp corners stay sharp); any
    stroke switches to a distance field traced with marching squares (overlapping strokes merge).
    flip_y: SVG y points down; the loops come back y up. size scales the longer side to size.
    center moves the bounding box centre to the origin. Units are the SVG's user units unless size.
    """
    shapes = load_shapes(path)
    if not shapes:
        raise ValueError(f"no visible shapes in {path}")
    strokes = any(sh["kind"] == "stroke" for sh in shapes)
    if mode == "fill" or (mode == "auto" and not strokes):
        loops = [np.asarray(p, float) for sh in shapes if sh["kind"] == "fill" for p, _c in sh["subpaths"] if len(p) >= 3]
        loops = [l[:-1] if len(l) > 3 and np.allclose(l[0], l[-1]) else l for l in loops]
    else:
        allpts = np.vstack([np.asarray(p, float) for sh in shapes for p, _c in sh["subpaths"]])
        extent = float(max(allpts.max(axis=0) - allpts.min(axis=0)))
        res = float(res or max(extent / 400.0, 1e-6))
        widest = max([sh.get("width", 0.0) for sh in shapes] + [0.0])
        pad = widest + 3 * res
        lo = allpts.min(axis=0) - pad
        hi = allpts.max(axis=0) + pad
        xs = np.arange(lo[0], hi[0] + res, res)
        ys = np.arange(lo[1], hi[1] + res, res)
        if len(xs) * len(ys) > 4_000_000:
            raise ValueError("the SVG is too detailed for this resolution; pass a larger res")
        px, py = np.meshgrid(xs, ys)
        F = field(shapes, px, py)
        F[0, :] = F[-1, :] = F[:, 0] = F[:, -1] = -1.0
        loops = [simplify(l, float(tol if tol is not None else res * 0.25)) for l in _march(F, xs, ys)]
    loops = [l for l in loops if len(l) >= 3 and abs(signed_area(l)) > 1e-12]
    if not loops:
        raise ValueError(f"no closed outlines in {path}")
    if flip_y:
        loops = [l * np.array([1.0, -1.0]) for l in loops]
    if size:
        lo, hi = bounds(loops)
        scale = float(size) / max(1e-12, float(max(hi - lo)))
        loops = [l * scale for l in loops]
    if center:
        lo, hi = bounds(loops)
        mid = (lo + hi) / 2
        loops = [l - mid for l in loops]
    return orient(loops)

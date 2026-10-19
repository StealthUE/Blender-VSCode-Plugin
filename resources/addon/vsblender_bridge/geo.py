"""vsblender.geo: build meshes for any model, rendered, game-ready or 3D printed.

Lengths are scene units (Blender units, BU). The one exception is m=<size> on threads, holes and nut
traps: an ISO metric size, so its standard dimensions are millimetres, converted to BU.

Two layers:

1. Solids (most scripts): immutable values. Nothing exists in Blender until to_object.
       from vsblender import geo
       plate = geo.box(60, 40, 4, fillet=3, edges="vertical")
       part = plate + geo.cylinder(d=10, h=12).move(15, 0, 0) - geo.hole(m=3, depth=20).move(15, 0, 12)
       part.to_object("Bracket", materials=["Grey"])
   Primitives are centred in X and Y and stand on z = 0 (center=True centres them in Z too).
   + - & are union, difference, intersection (Blender's boolean; Manifold solver when the inputs are
   closed). An intersection of closed solids whose surfaces never meet is decided by ray parity,
   not a solver (the shells of each that lie inside the other). geo.join(a, b) combines without a boolean, which is faster and fine for render and game
   models. to_object updates an existing object in place: its material slots, modifiers and
   animation survive.

2. Builders (detailed shapes): add faces to a BMesh you pass first, and return the new faces.
       geo.lathe(bm, [(r, z), ...]), geo.prism(bm, loops, h0, h1), geo.sweep(bm, profile, path),
       geo.loft(bm, sections), geo.polar_block(bm, ...), geo.mirror_weld(bm), geo.finish(bm)
   geo.sweep and geo.loft return a Solid when called without a BMesh.

3. Rings (gates, dials, wheels, clock faces): geo.ring(up="+Y", front="+Z") works in clock angles,
   clockwise from the top as seen from the front: ring.pt, ring.theta (for lathe), ring.on (a prism
   mapping onto a surface), ring.band (a sector following a surface), ring.pattern (one segment,
   mirrored and repeated), ring.svgs (SVG outlines at a radius; box= keeps a glyph set's sizes),
   ring.chevron (a point, two wings, a shallow V), ring.radial_hole, and ring.plan_split /
   ring.split (cut a ring into pieces that fit the bed, with half-laps, pins and screws).

4. Mechanical: thread, hole and nut_trap (at=, axis= put them on any face), gear and rack
   (involute; geom2d.gear_pair checks a mesh).

Geometry for a child of a rotated root, or an object whose transform is keyed:
to_object(name, parent=root, space="local"). Linked duplicates: to_object(..., shared="all").
After booleans: .clean() removes zero-area slivers without merging faces into n-gons, and refuses a
result that crosses itself; a cutter whose shells overlap: a.difference(b, union_cutters=True), and
a.intersect(join, union_cutters=True). .named("Part") names a solid in warnings.

Traps this module avoids: tessellate_polygon mis-fills concave outlines (CDT with even-odd is used);
a ring of vertices on the axis leaves non-manifold edges (a single apex vertex is used).
"""
from __future__ import annotations

import json
import math
import os

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector

from . import geom2d, meshdata
from . import units as units_mod

shapes2d = geom2d

# ----------------------------------------------------------------------------- ISO tables (millimetres)
COARSE_PITCH = {1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75,
                14: 2.0, 16: 2.0, 20: 2.5, 24: 3.0}
# ISO 273 clearance holes: close, normal (medium), loose.
CLEARANCE = {2: (2.2, 2.4, 2.6), 2.5: (2.7, 2.9, 3.1), 3: (3.2, 3.4, 3.6), 4: (4.3, 4.5, 4.8), 5: (5.3, 5.5, 5.8),
             6: (6.4, 6.6, 7.0), 8: (8.4, 9.0, 10.0), 10: (10.5, 11.0, 12.0), 12: (13.0, 13.5, 14.5)}
# Heat-set inserts (typical short inserts; check your insert's datasheet): hole diameter, depth.
INSERT = {2: (3.2, 4.0), 2.5: (3.5, 4.5), 3: (4.0, 5.7), 4: (5.6, 8.1), 5: (6.4, 9.5), 6: (8.0, 12.7)}
# ISO 4032 hex nuts: width across flats, height.
NUT = {2: (4.0, 1.6), 2.5: (5.0, 2.0), 3: (5.5, 2.4), 4: (7.0, 3.2), 5: (8.0, 4.7), 6: (10.0, 5.2), 8: (13.0, 6.8),
       10: (16.0, 8.4), 12: (18.0, 10.8)}
# ISO 4762 socket head cap screws: head diameter, head height. Counterbores add 0.6 mm and 0.2 mm.
CAP_HEAD = {2: (3.8, 2.0), 2.5: (4.5, 2.5), 3: (5.5, 3.0), 4: (7.0, 4.0), 5: (8.5, 5.0), 6: (10.0, 6.0), 8: (13.0, 8.0)}
# ISO 10642 countersunk head diameter (90 degrees).
COUNTERSINK = {3: 6.72, 4: 8.96, 5: 11.2, 6: 13.44, 8: 17.92}


def _m_key(m) -> float:
    v = float(m)
    return int(v) if v.is_integer() else v


def _table(table: dict, m, what: str):
    key = _m_key(m)
    if key not in table:
        raise ValueError(f"no {what} for M{m}; known sizes: {', '.join('M' + str(k) for k in table)}")
    return table[key]


# ----------------------------------------------------------------------------- units and tolerances
def _bu_per_mm() -> float:
    return 1.0 / units_mod.units()["bu_to_mm"]


def _mm(value: float) -> float:
    """Millimetres to scene units."""
    return float(value) * _bu_per_mm()


def _printer() -> dict | None:
    """The printer profile when this is a print project (print_mm template, mm units with a printer set)."""
    from . import helpers

    profile = helpers._run.get("printer")
    if units_mod.template() == "print_mm":
        return profile or helpers.DEFAULT_PRINTER
    if profile and profile.get("configured") and units_mod.is_print_scene():
        return profile
    return None


def _warn(message: str) -> None:
    from . import helpers

    helpers.warn(message)


def segments_for(radius: float, tol: float | None = None, angle: float = 360.0, min_seg: int = 12,
                 max_seg: int | None = None, multiple: int = 4) -> int:
    """Segments for an arc of `angle` degrees so the chord deviates at most tol from the true circle.

    Default tol: 0.02 mm in print projects (what a printer resolves), otherwise 0.2% of the radius,
    which looks round under smooth shading (about 52 segments for a full circle).
    """
    radius = abs(float(radius))
    if radius <= 0:
        return min_seg
    printing = _printer() is not None
    if tol is None:
        tol = _mm(0.02) if printing else radius * 0.002
    max_seg = max_seg or (512 if printing else 128)
    ratio = min(1.0, max(1e-9, float(tol) / radius))
    per_full = math.pi / math.acos(1.0 - ratio)
    n = per_full * abs(angle) / 360.0
    n = int(math.ceil(n / multiple) * multiple) if multiple > 1 else int(math.ceil(n))
    return max(min_seg if angle >= 360 else 2, min(max_seg, n))


def _fillet_segments(span: float, radius: float, segments: int | None) -> int:
    """Segments for a 90 degree round. An explicit count is used as given.

    Otherwise the chord error is 2% of the larger of the solid's span and the radius, and the
    count stays between 3 and 8. A 9 mm round on a small bead is a handful of segments, not the
    hundreds segments_for would pick from the radius alone.
    """
    if segments is not None:
        return max(1, int(segments))
    radius = abs(float(radius))
    tol = max(abs(float(span)), radius, 1e-9) * 0.02
    return max(3, segments_for(radius or 1e-9, tol=tol, angle=90.0, min_seg=2, max_seg=8, multiple=1))


# ----------------------------------------------------------------------------- low-level builders
def _apply_matrix(bm, verts, matrix) -> None:
    if matrix is not None:
        bmesh.ops.transform(bm, matrix=Matrix(matrix), verts=list(verts))


def _new_face(bm, verts, mat: int, faces: list) -> None:
    try:
        face = bm.faces.new(verts)
    except ValueError:
        return
    face.material_index = mat
    faces.append(face)


def _cap(bm, coords2d, verts3d, mat: int, reverse: bool, faces: list) -> None:
    """Triangulate a planar outline (2D coords) whose 3D vertices already exist, and add the faces."""
    loop = np.asarray(coords2d, float)
    if len(loop) < 3:
        return
    pts, tris = geom2d.triangulate([loop])
    if not len(tris):
        return
    lookup = []
    for p in pts:
        d = np.hypot(*(loop - p).T)
        k = int(np.argmin(d))
        lookup.append(verts3d[k] if d[k] < 1e-9 * (1 + np.abs(loop).max()) else None)
    if any(v is None for v in lookup):
        # The triangulation added points (a self-touching outline); fall back to one n-gon.
        _new_face(bm, list(reversed(verts3d)) if reverse else list(verts3d), mat, faces)
        return
    for t in tris:
        tri = [lookup[i] for i in t]
        _new_face(bm, tri[::-1] if reverse else tri, mat, faces)


def lathe(bm, profile, segments: int | None = None, a0: float = 0.0, a1: float = 360.0, caps: bool = True,
          mat: int = 0, matrix=None, eps: float | None = None) -> list:
    """Revolve a profile [(r, z), ...] about the Z axis from angle a0 to a1 (degrees, from +X).

    Order the profile counter-clockwise in the (r, z) plane and the faces point outward: for a solid
    of revolution, go out along the bottom, up the outside and back in along the top. Points with
    r = 0 become a single apex vertex. A partial sweep (a1 - a0 < 360) gets flat end caps when caps.
    """
    prof = [(max(0.0, float(r)), float(z)) for r, z in profile]
    if len(prof) > 2 and math.isclose(prof[0][0], prof[-1][0]) and math.isclose(prof[0][1], prof[-1][1]):
        prof = prof[:-1]
        closed = True
    else:
        closed = False
    if len(prof) < 2:
        raise ValueError("a lathe profile needs at least two points")
    span = float(a1) - float(a0)
    full = abs(span) >= 360.0 - 1e-9
    rmax = max(r for r, _z in prof)
    eps = eps if eps is not None else max(rmax, 1e-12) * 1e-9
    n = segments or segments_for(rmax, angle=360.0 if full else abs(span))
    count = n if full else n + 1
    angles = [math.radians(a0 + span * j / n) for j in range(count)]
    rings = []
    for r, z in prof:
        if r <= eps:
            rings.append([bm.verts.new((0.0, 0.0, z))])
        else:
            rings.append([bm.verts.new((r * math.cos(a), r * math.sin(a), z)) for a in angles])
    faces = []
    pairs = list(zip(range(len(prof) - 1), range(1, len(prof))))
    if closed:
        pairs.append((len(prof) - 1, 0))
    steps = n if full else n
    for i, k in pairs:
        ring_i, ring_k = rings[i], rings[k]
        for j in range(steps):
            jn = (j + 1) % count if full else j + 1
            a = ring_i[0] if len(ring_i) == 1 else ring_i[j]
            b = ring_i[0] if len(ring_i) == 1 else ring_i[jn]
            c = ring_k[0] if len(ring_k) == 1 else ring_k[jn]
            d = ring_k[0] if len(ring_k) == 1 else ring_k[j]
            quad = [a, b, c, d]
            unique = []
            for v in quad:
                if v not in unique:
                    unique.append(v)
            if len(unique) >= 3:
                _new_face(bm, unique, mat, faces)
    if not full and caps:
        outline = list(range(len(prof)))
        coords = [prof[i] for i in outline]
        if not closed:
            # Close the region along the axis: the end points drop onto r = 0 unless they are there.
            if prof[-1][0] > eps:
                coords.append((0.0, prof[-1][1]))
            if prof[0][0] > eps:
                coords.append((0.0, prof[0][1]))
        axis_verts = {}
        for idx in range(len(prof), len(coords)):
            axis_verts[idx] = bm.verts.new((0.0, 0.0, coords[idx][1]))
        for angle_index, reverse in ((0, False), (count - 1, True)):
            verts3d = []
            for idx in range(len(coords)):
                if idx < len(prof):
                    ring = rings[idx]
                    verts3d.append(ring[0] if len(ring) == 1 else ring[angle_index])
                else:
                    verts3d.append(axis_verts[idx])
            _cap(bm, coords, verts3d, mat, reverse if span > 0 else not reverse, faces)
    if span < 0:
        for f in faces:
            f.normal_flip()
    new_verts = {v for ring in rings for v in ring} | {v for f in faces for v in f.verts}
    _apply_matrix(bm, new_verts, matrix)
    return faces


def polar_block(bm, a0: float, a1: float, r0: float, r1: float, z0: float, z1: float, top=None,
                seg_deg: float = 2.0, mat: int = 0, matrix=None) -> list:
    """Closed annular sector between angles a0..a1 (degrees), radii r0..r1, heights z0..z1.

    top(r) -> z gives a sloped or curved top instead of z1 (sampled across the radius).
    """
    if top is None:
        profile = [(r0, z0), (r1, z0), (r1, z1), (r0, z1)]
    else:
        samples = max(2, int(abs(r1 - r0) / max(abs(r1 - r0), 1e-12) * 8))
        rs = np.linspace(r1, r0, samples + 1)
        profile = [(r0, z0), (r1, z0)] + [(float(r), float(top(float(r)))) for r in rs]
    segments = max(1, int(math.ceil(abs(a1 - a0) / max(seg_deg, 1e-6))))
    return lathe(bm, profile + [profile[0]], segments=segments, a0=a0, a1=a1, caps=True, mat=mat, matrix=matrix)


_AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


def axis_vector(axis) -> Vector:
    """'+Y', '-Z', 'x' or a 3-vector, as a unit Vector."""
    if isinstance(axis, str):
        text = axis.strip().upper()
        sign = -1.0 if text.startswith("-") else 1.0
        key = text.lstrip("+-")
        if key not in _AXES:
            raise ValueError(f"axis must be +X, -X, +Y, -Y, +Z, -Z or a vector, not {axis!r}")
        return Vector(_AXES[key]) * sign
    vec = Vector(axis)
    if vec.length < 1e-12:
        raise ValueError("axis vector is zero")
    return vec.normalized()


class Ring:
    """The frame gates, dials, clock faces, wheels and turbines think in: an angle clockwise from the top
    as seen from the front, a radius from the centre, and a depth toward the viewer.

        ring = geo.ring(up="+Y", front="+Z")     # a ring in XY seen from +Z (a gate's local frame)
        ring.pt(40, 3.0)                         # the point 40 degrees clockwise from the top, r = 3
        ring.theta(40)                           # the same angle for geo.lathe / polar_block (from +X, CCW)
        geo.prism(bm, loops, 0, 0.01, ring.on(depth, a=40))   # an outline (u = arc length, v = radius) on a surface
        ring.band(bm, -20, 20, 2.7, 2.8, depth, lo=-0.01, hi=0.03)   # a sector whose faces follow depth(r)
        ring.pattern(segment, 9, mirror="back")  # one segment built once, its back copy, nine around

    depth(r) is the surface height toward the viewer at radius r (a cone face is linear in r).
    side=-1 builds on the back: depths are mirrored through the ring's plane.
    """

    def __init__(self, up="+Y", front="+Z", center=(0.0, 0.0, 0.0), clockwise: bool = True):
        self.up = axis_vector(up)
        self.front = axis_vector(front)
        if abs(self.up.dot(self.front)) > 1e-6:
            raise ValueError("up and front must be perpendicular")
        self.right = self.up.cross(self.front)      # the viewer's right, seen from the front
        self.sign = 1.0 if clockwise else -1.0
        self.center = Vector(center)
        # Canonical lathe frame (angles from +X counter-clockwise about +Z) to this ring.
        self.matrix = Matrix.Translation(self.center) @ Matrix((
            (self.right.x, self.up.x, self.front.x, 0.0),
            (self.right.y, self.up.y, self.front.y, 0.0),
            (self.right.z, self.up.z, self.front.z, 0.0),
            (0.0, 0.0, 0.0, 1.0)))

    def __repr__(self) -> str:
        return f"<Ring up {tuple(round(v, 3) for v in self.up)} front {tuple(round(v, 3) for v in self.front)} " \
               f"{'clockwise' if self.sign > 0 else 'counter-clockwise'}>"

    def theta(self, a: float) -> float:
        """The lathe angle (degrees from the ring's right, counter-clockwise) of clock angle a."""
        return 90.0 - self.sign * float(a)

    def direction(self, a: float) -> Vector:
        t = math.radians(a)
        return self.right * (self.sign * math.sin(t)) + self.up * math.cos(t)

    def pt(self, a: float, r: float, d: float = 0.0) -> Vector:
        """The point at clock angle a, radius r and depth d (toward the viewer)."""
        return self.center + self.direction(a) * float(r) + self.front * float(d)

    def angle_of(self, point) -> float:
        """The clock angle of a point, 0..360."""
        rel = Vector(point) - self.center
        return math.degrees(math.atan2(self.sign * rel.dot(self.right), rel.dot(self.up))) % 360.0

    def radius_of(self, point) -> float:
        rel = Vector(point) - self.center
        return math.hypot(rel.dot(self.right), rel.dot(self.up))

    def on(self, depth=None, a: float = 0.0, side: int = 1):
        """to3d for geo.prism: an outline in (u, v) where u is arc length (clockwise positive) around
        clock angle a and v the radius, standing h above the surface depth(v)."""
        surface = depth or (lambda _r: 0.0)

        def to3d(u, v, h):
            angle = a + math.degrees(u / v) if v else a
            return tuple(self.pt(angle, v, side * (surface(v) + h)))
        return to3d

    def band(self, bm, a0: float, a1: float, r0: float, r1: float, depth=None, lo: float = 0.0, hi: float = 0.0,
             side: int = 1, mat: int = 0, seg_deg: float = 0.5, samples: int = 2) -> list:
        """A closed sector from clock angle a0 to a1 between radii r0 and r1, whose two faces follow the
        surface: from depth(r) + lo to depth(r) + hi (side=-1: on the back). samples: points across the
        radius (2 is exact for a cone; more for a curved surface). a1 - a0 = 360 makes a full ring."""
        surface = depth or (lambda _r: 0.0)
        rs = [r0 + (r1 - r0) * i / max(1, samples - 1) for i in range(max(2, samples))]
        bottom = [(r, side * (surface(r) + lo)) for r in rs]
        top = [(r, side * (surface(r) + hi)) for r in reversed(rs)]
        prof = bottom + top
        span = float(a1) - float(a0)
        if abs(span) >= 360.0 - 1e-9:
            t0, t1 = 0.0, 360.0
        else:
            t0, t1 = sorted((self.theta(a0), self.theta(a1)))
        n = max(1, int(math.ceil(abs(t1 - t0) / max(seg_deg, 1e-6))))
        faces = lathe(bm, prof + [prof[0]], segments=n, a0=t0, a1=t1, mat=mat, matrix=self.matrix)
        if (hi - lo) * side < 0:  # the profile ran clockwise: point the faces out
            for face in faces:
                face.normal_flip()
        return faces

    def _reflect(self, normal) -> Matrix:
        n = Vector(normal).normalized()
        m = Matrix.Identity(3) - 2.0 * Matrix((n * n.x, n * n.y, n * n.z)).transposed()
        return Matrix.Translation(self.center) @ m.to_4x4() @ Matrix.Translation(-self.center)

    def rotation(self, a: float) -> Matrix:
        """Turns by a degrees in the clock direction about the ring's axis."""
        return Matrix.Translation(self.center) @ Matrix.Rotation(-self.sign * math.radians(a), 4, self.front) \
            @ Matrix.Translation(-self.center)

    def pattern(self, segment, count: int, mirror: str | None = None, symmetric: bool = False, start: float = 0.0,
                step: float | None = None) -> "Solid":
        """A segment built once, repeated count times around (every step degrees, default 360/count).

        segment: a Solid, or a builder function filling a BMesh (finished, normals out). symmetric=True
        adds its mirror image about the centre line at clock angle start (build one half). mirror="back"
        adds its mirror image through the ring's plane (the same detail on the back). Combined without
        booleans (geo.join)."""
        piece = segment if isinstance(segment, Solid) else _solid_from(segment, getattr(segment, "__name__", "segment"))
        parts = [piece]
        if symmetric:
            parts.append(piece.transform(self._reflect(self.direction(start + 90.0)), f"{piece.desc}.mirror(centre line)"))
        if mirror in ("back", "front"):
            parts += [p.transform(self._reflect(self.front), f"{p.desc}.mirror(back)") for p in list(parts)]
        elif mirror:
            raise ValueError("mirror must be 'back' (through the ring's plane)")
        one = join(*parts) if len(parts) > 1 else piece
        pitch = float(step) if step is not None else 360.0 / int(count)
        copies = [one.transform(self.rotation(pitch * i), f"{one.desc}") for i in range(int(count))]
        out = join(*copies)
        out.desc = f"{one.desc}.ring_pattern({count}{', mirror=' + repr(mirror) if mirror else ''}{', symmetric' if symmetric else ''})"
        return out

    def svgs(self, bm, paths, radius: float, height: float, z0: float, z1: float, angles=None, mat: int = 0,
             box=None, min_stroke: float | None = None, mode: str = "auto") -> list:
        """Each SVG as a prism on the ring, centred at radius, between depths z0 and z1 along the ring's
        front axis. angles: one clock angle per file (default: evenly spaced).

        box: how a glyph is scaled. None scales each file by its own drawing (its y span becomes
        height), so glyphs of different sizes all come out the same height. box="viewbox" (or True)
        scales every file by its viewBox and centres it on the viewBox, so a glyph set keeps the sizes
        and positions it was drawn with; box=48 does the same for a 48-unit design box.
        A page rectangle (a loop that is exactly the viewBox) is left out; nothing else is.
        min_stroke (scene units): strokes thinner than this are widened (geom2d.svg_loops min_stroke).
        mode: svg_loops mode ("auto", or "fill" for filled glyphs with hairline outlines).
        """
        paths = [str(p) for p in paths]
        if not paths:
            return []
        if angles is None:
            step = 360.0 / len(paths)
            angles = [step * i for i in range(len(paths))]
        elif len(list(angles)) != len(paths):
            raise ValueError("angles must have one entry per SVG")
        faces = []
        for path, angle in zip(paths, angles):
            loops, scale, centre = self._svg_glyph(path, float(height), box, min_stroke, mode)
            placed = [np.column_stack([
                (loop[:, 0] - centre[0]) * scale,
                float(radius) + (loop[:, 1] - centre[1]) * scale,
            ]) for loop in loops]
            faces += prism(bm, placed, z0, z1, self.on(None, a=float(angle)), mat=mat)
        return faces

    @staticmethod
    def _svg_glyph(path: str, height: float, box, min_stroke, mode: str) -> tuple:
        """(loops y up, scale to scene units, centre in SVG units) for one glyph file."""
        view = geom2d.svg_view_box(path)
        loops = geom2d.svg_loops(path, mode=mode)
        if view is not None:
            # The page rectangle: a 4-corner loop on the viewBox's edges (y flipped by svg_loops).
            vx0, vx1 = view[0], view[0] + view[2]
            vy0, vy1 = -(view[1] + view[3]), -view[1]
            tol = 0.01 * max(view[2], view[3])

            def page(loop):
                lo, hi = loop.min(axis=0), loop.max(axis=0)
                return (len(geom2d.simplify(loop, tol)) <= 5 and abs(lo[0] - vx0) < tol and abs(hi[0] - vx1) < tol
                        and abs(lo[1] - vy0) < tol and abs(hi[1] - vy1) < tol)

            kept = [l for l in loops if not page(l)]
            loops = kept or loops
        pts = np.vstack(loops)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        if box in (None, False):
            scale = height / max(float(hi[1] - lo[1]), 1e-12)
            centre = (lo + hi) / 2.0
        else:
            box_h = view[3] if (box is True or str(box).lower() == "viewbox") and view else float(box) \
                if not isinstance(box, str) else None
            if box_h is None:
                raise ValueError(f"{os.path.basename(path)} has no viewBox; pass box=<design box height>")
            scale = height / box_h
            centre = np.array([view[0] + view[2] / 2.0, -(view[1] + view[3] / 2.0)]) if view else (lo + hi) / 2.0
        if min_stroke:
            loops = geom2d.svg_loops(path, mode=mode, min_stroke=float(min_stroke) / scale)
            if view is not None:
                loops = [l for l in loops if not page(l)] or loops
        return loops, scale, centre

    def radial_hole(self, angle: float, r: float, depth: float = 0.0, m=None, d: float | None = None,
                    length: float = 10.0, inward: bool = True, **hole_kw) -> "Solid":
        """A hole cutter on a round part: its opening at clock angle `angle`, radius r and depth `depth`
        (toward the viewer), drilled toward the centre (inward=True, from the outside) or away from it
        (inward=False, from inside a bore). length is how deep it goes. Other arguments as geo.hole
        (fit, counterbore, countersink, teardrop)."""
        direction = self.direction(float(angle)) * (-1.0 if inward else 1.0)
        return hole(m=m, d=d, depth=length, at=tuple(self.pt(float(angle), float(r), float(depth))),
                    axis=tuple(direction), **hole_kw)

    # -- splitting a ring to fit a bed
    def extent(self, solid) -> dict:
        """Radius and depth range of a solid in this ring's frame."""
        co = np.array([v.co[:] for v in solid.bm.verts], dtype=np.float64)
        rel = co - np.array(self.center[:])
        right, up, front = (np.array(v[:]) for v in (self.right, self.up, self.front))
        r = np.hypot(rel @ right, rel @ up)
        d = rel @ front
        return {"r": (float(r.min()), float(r.max())), "depth": (float(d.min()), float(d.max()))}

    def sector(self, a0: float, a1: float, r0: float, r1: float, d0: float, d1: float, seg_deg: float = 2.0) -> "Solid":
        """A closed annular sector from clock angle a0 to a1 (clockwise), radii r0..r1, depths d0..d1."""
        span = float(a1) - float(a0)
        if span <= 0:
            raise ValueError("a sector needs a1 > a0 (clock angles, clockwise)")
        if span >= 360.0 - 1e-9:
            return _solid_from(lambda bm: self.band(bm, 0.0, 360.0, r0, r1, None, d0, d1, seg_deg=seg_deg),
                               _call("sector", a0, a1))
        return _solid_from(lambda bm: self.band(bm, a0, a1, r0, r1, None, d0, d1, seg_deg=seg_deg), _call("sector", a0, a1))

    def split(self, solid, seams, lap: float | None = None, lap_deg: float | None = None, z_split: float | None = None,
              fixings=None, pin: dict | None = None, screw: int = 3, front_skin: float | None = None,
              overshoot_deg: float = 1.0) -> list:
        """Cut a ring into pieces at clock angles `seams`, so each fits the bed. Returns one Solid per
        piece, clockwise from the first seam, each with meta["span"] = (from, to) and its name.

        lap: a half-lap joint, as an arc length at the middle radius (scene units), or lap_deg in
        degrees (a whole number of gear teeth keeps a cut off the teeth). Each piece then runs on past
        its end seam by the lap: between the laps it is full depth, on the lap at its start it keeps
        the front half, and on the lap at its end the back half, so the face shows only a hairline.
        z_split: the depth of the lap's joint face (default: halfway through).
        fixings: [("pin" | "screw", fraction across the lap, radius)]. Through the back half: pin slip
        holes, or screw clearance holes counterbored from the back. Into the front half, blind from
        the joint face: pin press holes, or heat-set insert holes, front_skin (default 1.2 mm) short of
        the visible face. pin: {"tight": 2.95, "slip": 3.15} in mm; screw: the M size.
        """
        seams = sorted({round(float(s) % 360.0, 6) for s in seams})
        if len(seams) < 2:
            # One cut opens the ring but makes no pieces: nothing to split.
            return [solid._derive(solid.desc)]
        ext = self.extent(solid)
        (r_min, r_max), (d_min, d_max) = ext["r"], ext["depth"]
        reach = max(r_max * 0.05, _mm(2.0))
        rlo, rhi = max(0.0, r_min - reach), r_max + reach
        dlo, dhi = d_min - reach, d_max + reach
        r_mid = (r_min + r_max) / 2.0
        d = float(lap_deg) if lap_deg is not None else (math.degrees(float(lap) / r_mid) if lap else 0.0)
        split_at = float(z_split) if z_split is not None else (d_min + d_max) / 2.0
        pins = {"tight": 2.95, "slip": 3.15}
        pins.update(pin or {})
        skin = float(front_skin) if front_skin is not None else _mm(1.2)
        spans = [(a, seams[(i + 1) % len(seams)] + (360.0 if i + 1 == len(seams) else 0.0)) for i, a in enumerate(seams)]
        if d and any(b - a <= d for a, b in spans):
            raise ValueError(f"a lap of {d:.3g} degrees is as long as the shortest piece: shorten the lap or use fewer seams")
        # Fixing cutters per seam: (for the piece ending there, for the piece starting there).
        at_seam = {}
        if d and fixings:
            blind = max(_mm(1.0), (d_max - split_at) - skin)
            for seam in seams:
                before, after = [], []
                for kind, frac, radius in fixings:
                    angle = seam + d * float(frac)
                    back = tuple(self.pt(angle, float(radius), d_min))
                    joint = tuple(self.pt(angle, float(radius), split_at))
                    toward_front = tuple(self.front)
                    through = (split_at - d_min) + _mm(0.5)
                    if kind == "pin":
                        before.append(hole(d=_mm(pins["slip"]), depth=through, at=back, axis=toward_front))
                        after.append(hole(d=_mm(pins["tight"]), depth=blind, at=joint, axis=toward_front))
                    elif kind == "screw":
                        before.append(hole(m=screw, depth=through, counterbore=True, at=back, axis=toward_front))
                        after.append(hole(m=screw, fit="insert", depth=min(blind, _mm(_table(INSERT, screw, "heat-set insert")[1] + 0.5)),
                                          at=joint, axis=toward_front))
                    else:
                        raise ValueError("fixings are ('pin' | 'screw', fraction, radius)")
                at_seam[round(seam, 6)] = (before, after)
        pieces = []
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for index, (a, b) in enumerate(spans):
            if d:
                cutters = [self.sector(b + d, a + 360.0, rlo, rhi, dlo, dhi),
                           self.sector(a - overshoot_deg, a + d, rlo, rhi, dlo, split_at),
                           self.sector(b, b + d + overshoot_deg, rlo, rhi, split_at, dhi)]
                cutters += at_seam.get(round(b % 360.0, 6), ([], []))[0] + at_seam.get(round(a % 360.0, 6), ([], []))[1]
            else:
                cutters = [self.sector(b, a + 360.0, rlo, rhi, dlo, dhi)]
            piece = solid.difference(join(*cutters), union_cutters=True)
            label = f"{solid.label or 'ring'} {letters[index % 26]}"
            piece.meta.update(label=label, span=(round(a % 360.0, 4), round((b + d) % 360.0, 4)))
            pieces.append(piece)
        return pieces

    def plan_split(self, target, printer: dict | None = None, avoid=None, snap: float | None = None, phase: float = 0.0,
                   counts=range(1, 13), lap: float | None = None, lap_deg: float | None = None,
                   margin: float = 2.0) -> dict:
        """The fewest equal pieces of a ring that fit the printer's bed (turned if need be), with no seam
        inside an `avoid` span. target: a Solid in this ring's frame, or (r_in, r_out) in scene units.
        avoid: [(a0, a1)] clock-angle spans a seam must not fall in (chevrons, glyphs). snap: seams only
        at phase + k x snap (a gear's tooth pitch with phase on a tooth space). Returns {count, seams,
        span_deg, fit} (fit as vsblender.fit_on_bed), or count None when nothing up to max(counts) fits.
        """
        from . import helpers, placement

        if hasattr(target, "bm"):
            r_in, r_out = self.extent(target)["r"]
        else:
            r_in, r_out = (float(v) for v in target)
        profile = dict(helpers.DEFAULT_PRINTER)
        profile.update(printer or helpers.printer() or {})
        bed = profile.get("build_volume") or [220, 220, 250]
        to_mm = units_mod.units()["bu_to_mm"]
        r_mid = (r_in + r_out) / 2.0
        extra = float(lap_deg) if lap_deg is not None else (math.degrees(float(lap) / r_mid) if lap else 0.0)
        spans = [(float(a0) % 360.0, float(a1) % 360.0) for a0, a1 in (avoid or [])]

        def blocked(angle: float) -> bool:
            angle %= 360.0
            for a0, a1 in spans:
                inside = (a0 <= angle <= a1) if a0 <= a1 else (angle >= a0 or angle <= a1)
                if inside:
                    return True
            return False

        for count in counts:
            span = 360.0 / int(count)
            outline = _sector_outline(r_in * to_mm, r_out * to_mm, min(360.0, span + extra))
            fit = placement.bed_fit(outline, bed[:2], margin)
            if fit is None:
                continue
            step = float(snap) if snap else 0.5
            offsets = np.arange(0.0, span, step) if int(count) > 1 else [0.0]
            for off in offsets:
                seams = [(float(phase) + off + k * span) % 360.0 for k in range(int(count))]
                if int(count) == 1 or not any(blocked(s) for s in seams):
                    return {"count": int(count), "seams": [round(s, 4) for s in seams] if int(count) > 1 else [],
                            "span_deg": round(span, 4), "lap_deg": round(extra, 4), "fit": fit}
        return {"count": None, "seams": [], "span_deg": None, "lap_deg": round(extra, 4), "fit": None}

    def chevron(self, bm, angle: float, outer: float, inner: float, spread: float = 28.0, wing: float = 0.45,
                notch: float = 0.15, z0: float = 0.0, z1: float = 0.02, count: int = 1, mat: int = 0) -> list:
        """A chevron pointing inward: a triangle, two wings, and a shallow V in the outer edge.

        outer and inner are radii. spread is the full angular width in degrees. wing is the fraction
        of that width each wing occupies on the outer edge (the rest is the V). notch is how far the
        V cuts back, as a fraction of the radial span. count repeats it every 360/count degrees.
        """
        outer, inner = float(outer), float(inner)
        span = outer - inner
        if span <= 0:
            raise ValueError("outer radius must be greater than inner")
        wing = min(0.95, max(0.05, float(wing)))
        notch = min(0.95, max(0.0, float(notch)))
        half = math.radians(float(spread)) / 2.0
        shoulder = half * (1.0 - wing)
        notch_r = outer - span * notch

        def uv(ang: float, radius: float):
            return (radius * ang, radius)

        loop = [
            uv(0.0, inner),
            uv(-half, outer),
            uv(-shoulder, outer),
            uv(0.0, notch_r),
            uv(shoulder, outer),
            uv(half, outer),
        ]
        faces = []
        n = max(1, int(count))
        step = 360.0 / n
        for i in range(n):
            faces += prism(bm, [loop], z0, z1, self.on(None, a=float(angle) + step * i), mat=mat)
        return faces


def _sector_outline(r_in: float, r_out: float, span: float, n: int = 96) -> np.ndarray:
    """An annular sector's flat outline (bisector along +y); a 360 span is the outer circle."""
    if span >= 360.0 - 1e-9:
        t = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
        return np.column_stack([r_out * np.cos(t), r_out * np.sin(t)])
    half = math.radians(span / 2.0)
    t = np.linspace(-half, half, n + 1)
    outer = np.column_stack([r_out * np.sin(t), r_out * np.cos(t)])
    inner = np.column_stack([r_in * np.sin(t[::-1]), r_in * np.cos(t[::-1])])
    return np.vstack([outer, inner])


def ring(up="+Y", front="+Z", center=(0.0, 0.0, 0.0), clockwise: bool = True) -> Ring:
    """A ring frame: clock angles from the top seen from the front (see Ring)."""
    return Ring(up=up, front=front, center=center, clockwise=clockwise)


def prism(bm, loops, h0: float, h1: float, to3d=None, mat: int = 0, matrix=None) -> list:
    """Closed prism of a 2D region (loops, even-odd: inner loops are holes) between heights h0 and h1.

    to3d(u, v, h) -> (x, y, z) maps the outline onto a surface, for reliefs and engravings. Without
    it the prism stands along Z.
    """
    loops = [np.asarray(l, float) for l in loops if len(l) >= 3]
    pts, tris = geom2d.triangulate(loops)
    if not len(tris):
        return []
    edges = geom2d.boundary_edges(tris)
    used = sorted(set(tris.ravel().tolist()) | set(edges.ravel().tolist()))
    f = to3d or (lambda u, v, h: (u, v, h))
    top = {i: bm.verts.new(f(float(pts[i][0]), float(pts[i][1]), float(h1))) for i in used}
    bot = {i: bm.verts.new(f(float(pts[i][0]), float(pts[i][1]), float(h0))) for i in used}
    faces = []
    for a, b, c in tris:
        _new_face(bm, [top[a], top[b], top[c]], mat, faces)
        _new_face(bm, [bot[c], bot[b], bot[a]], mat, faces)
    for a, b in edges:
        _new_face(bm, [top[a], bot[a], bot[b], top[b]], mat, faces)
    if h1 < h0:
        for face in faces:
            face.normal_flip()
    _apply_matrix(bm, set(top.values()) | set(bot.values()), matrix)
    return faces


polygon_prism = prism


def _frames(points, closed: bool, up=(0.0, 0.0, 1.0)):
    """Rotation-minimising frames (tangent, normal, binormal) along a polyline (parallel transport)."""
    pts = [Vector(p) for p in points]
    n = len(pts)
    tangents = []
    for i in range(n):
        if closed:
            t = pts[(i + 1) % n] - pts[i - 1]
        elif i == 0:
            t = pts[1] - pts[0]
        elif i == n - 1:
            t = pts[-1] - pts[-2]
        else:
            t = pts[i + 1] - pts[i - 1]
        tangents.append(t.normalized() if t.length > 0 else Vector((0, 0, 1)))
    upv = Vector(up)
    normal = upv - tangents[0] * upv.dot(tangents[0])
    if normal.length < 1e-6:
        normal = tangents[0].orthogonal()
    normal.normalize()
    normals = [normal]
    for i in range(1, n):
        rot = tangents[i - 1].rotation_difference(tangents[i])
        nn = rot @ normals[-1]
        nn = (nn - tangents[i] * nn.dot(tangents[i])).normalized()
        normals.append(nn)
    if closed and n > 2:
        # Distribute the twist left between the last frame and the first over the whole loop.
        rot = tangents[-1].rotation_difference(tangents[0])
        end = rot @ normals[-1]
        cross = end.cross(normals[0])
        ang = math.atan2(cross.dot(tangents[0]), end.dot(normals[0]))
        for i in range(n):
            q = Matrix.Rotation(ang * i / n, 3, tangents[i])
            normals[i] = (q @ normals[i]).normalized()
    binormals = [t.cross(nv).normalized() for t, nv in zip(tangents, normals)]
    return pts, tangents, normals, binormals


def _sweep_bm(bm, profile, path, samples: int | None = None, closed_path: bool = False, caps: bool = True,
              up=(0.0, 0.0, 1.0), scale=None, twist=None, mat: int = 0, matrix=None) -> list:
    prof = np.asarray(profile, float)
    if len(prof) < 3:
        raise ValueError("a sweep profile is a closed 2D loop of at least 3 points")
    if geom2d.signed_area(prof) < 0:
        prof = prof[::-1]
    if callable(path):
        count = int(samples or 64)
        ts = [i / count for i in range(count)] if closed_path else [i / (count - 1) for i in range(count)]
        points = [Vector(path(t)) for t in ts]
    else:
        points = [Vector(p) for p in path]
        ts = [i / max(1, len(points) - (0 if closed_path else 1)) for i in range(len(points))]
    if len(points) < 2:
        raise ValueError("a sweep path needs at least two points")
    pts, tangents, normals, binormals = _frames(points, closed_path, up)
    rings = []
    for i, (p, t, nv, bv) in enumerate(zip(pts, tangents, normals, binormals)):
        s = float(scale(ts[i])) if callable(scale) else float(scale or 1.0)
        tw = math.radians(float(twist(ts[i]))) if callable(twist) else math.radians(float(twist or 0.0)) * ts[i]
        c, sn = math.cos(tw), math.sin(tw)
        ring = []
        for u, v in prof:
            uu, vv = (u * c - v * sn) * s, (u * sn + v * c) * s
            ring.append(bm.verts.new(p + nv * uu + bv * vv))
        rings.append(ring)
    faces = []
    m = len(prof)
    count = len(rings)
    spans = count if closed_path else count - 1
    for k in range(spans):
        lo, hi = rings[k], rings[(k + 1) % count]
        for a in range(m):
            b = (a + 1) % m
            _new_face(bm, [hi[a], lo[a], lo[b], hi[b]], mat, faces)
    if caps and not closed_path:
        _cap(bm, prof, rings[0], mat, True, faces)
        _cap(bm, prof, rings[-1], mat, False, faces)
    _apply_matrix(bm, {v for ring in rings for v in ring}, matrix)
    return faces


def _loft_bm(bm, sections, closed: bool = False, caps: bool = True, resample: int | None = None, mat: int = 0,
             matrix=None) -> list:
    secs = [np.asarray(s, float) for s in sections]
    if len(secs) < 2:
        raise ValueError("a loft needs at least two sections")
    count = int(resample or max(len(s) for s in secs))
    out = []
    for s in secs:
        if s.shape[1] == 2:
            s = np.column_stack([s, np.zeros(len(s))])
        closed_s = np.vstack([s, s[:1]])
        seg = np.linalg.norm(np.diff(closed_s, axis=0), axis=1)
        acc = np.concatenate([[0.0], np.cumsum(seg)])
        t = np.linspace(0.0, acc[-1], count, endpoint=False)
        out.append(np.column_stack([np.interp(t, acc, closed_s[:, i]) for i in range(3)]))
    # Align each section's start to the previous one, so the loft does not twist.
    for i in range(1, len(out)):
        prev = out[i - 1]
        best = min(range(count), key=lambda k: float(np.linalg.norm(np.roll(out[i], -k, axis=0) - prev, axis=1).sum()))
        out[i] = np.roll(out[i], -best, axis=0)
    rings = [[bm.verts.new(tuple(p)) for p in s] for s in out]
    faces = []
    spans = len(rings) if closed else len(rings) - 1
    for k in range(spans):
        lo, hi = rings[k], rings[(k + 1) % len(rings)]
        for a in range(count):
            b = (a + 1) % count
            _new_face(bm, [lo[a], lo[b], hi[b], hi[a]], mat, faces)
    if caps and not closed:
        for ring, s in ((rings[0], out[0]), (rings[-1], out[-1])):
            centre = s.mean(axis=0)
            _u, _s, vt = np.linalg.svd(s - centre)
            coords = (s - centre) @ vt[:2].T
            _cap(bm, coords, ring, mat, False, faces)
    bmesh.ops.recalc_face_normals(bm, faces=faces)
    _apply_matrix(bm, {v for ring in rings for v in ring}, matrix)
    return faces


def mirror_weld(bm, axis: str = "X", merge: float | None = None) -> None:
    """Mirror everything across the plane through the origin normal to axis, and weld the seam."""
    axis = axis.upper()
    if axis not in ("X", "Y", "Z"):
        raise ValueError("axis must be X, Y or Z")
    merge = merge if merge is not None else _merge_dist(bm)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    bmesh.ops.mirror(bm, geom=geom, axis=axis, merge_dist=merge)


def _merge_dist(bm) -> float:
    if not len(bm.verts):
        return 1e-9
    co = np.array([v.co[:] for v in bm.verts])
    return max(float(np.linalg.norm(co.max(axis=0) - co.min(axis=0))) * 1e-6, 1e-9)


def finish(bm, merge: float | None = None, dissolve_degenerate: bool = True, delete_loose: bool = True,
           recalc_normals: bool = True) -> dict:
    """Weld coincident vertices, remove degenerate and loose geometry, point the normals outward.

    Returns counts of what is still open (boundary_edges) or non-manifold, so a script can assert a
    closed result.
    """
    merge = merge if merge is not None else _merge_dist(bm)
    before = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge)
    merged = before - len(bm.verts)
    if dissolve_degenerate:
        bmesh.ops.dissolve_degenerate(bm, dist=merge, edges=bm.edges)
    if delete_loose:
        loose_v = [v for v in bm.verts if not v.link_faces]
        if loose_v:
            bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
        loose_e = [e for e in bm.edges if not e.link_faces]
        if loose_e:
            bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
    if recalc_normals and len(bm.faces):
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    non_manifold = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary)
    return {"merged": merged, "boundary_edges": boundary, "non_manifold_edges": non_manifold,
            "closed": boundary == 0 and non_manifold == 0}


def smooth_by_angle(me, deg: float = 30.0) -> None:
    """Smooth shading with sharp edges above deg (Blender's Shade Auto Smooth)."""
    me.shade_smooth()
    if hasattr(me, "set_sharp_from_angle"):
        me.set_sharp_from_angle(angle=math.radians(deg))


def uv_box(bm, size: float | None = None, name: str = "UVMap") -> None:
    """Box-projected UVs (no operator, no edit mode): each face is projected along its dominant axis."""
    layer = bm.loops.layers.uv.get(name) or bm.loops.layers.uv.new(name)
    if size is None:
        if not len(bm.verts):
            return
        co = np.array([v.co[:] for v in bm.verts])
        size = float(max(co.max(axis=0) - co.min(axis=0))) or 1.0
    inv = 1.0 / float(size)
    for face in bm.faces:
        n = face.normal
        ax = max(range(3), key=lambda i: abs(n[i]))
        a, b = [(1, 2), (0, 2), (0, 1)][ax]
        sign = 1.0 if n[ax] >= 0 else -1.0
        for loop in face.loops:
            co = loop.vert.co
            u = co[a] * inv * (sign if ax != 1 else -sign)
            loop[layer].uv = (u, co[b] * inv)


def replace_mesh(ob, bm, keep_slots: bool = True, smooth_deg: float | None = None, shared: str = "error"):
    """Put new geometry into an object, keeping the object, its mesh datablock and its material slots.

    The mesh is rewritten in place, so modifiers, constraints, animation, custom properties and the
    material slots stay. Replacing ob.data with a new mesh instead drops the slots, and materials
    keyed per slot then lose their last user. A mesh other objects share (linked duplicates):
    shared="all" writes it for every object that uses it, shared="copy" gives this object its own copy
    first, and the default refuses. Shape keys cannot survive a topology change: the mesh is swapped
    and a warning is raised.
    """
    me = ob.data
    if ob.type != "MESH" or me is None:
        raise ValueError(f"{ob.name} is not a mesh object")
    if shared not in ("error", "copy", "all"):
        raise ValueError("shared must be 'error', 'copy' or 'all'")
    users = [o for o in bpy.data.objects if o.type == "MESH" and o.data == me]
    if len(users) > 1 or me.users > 1:
        if shared == "copy":
            me = me.copy()
            ob.data = me
            users = [ob]
        elif shared != "all":
            others = [o.name for o in users if o != ob]
            named = f" ({', '.join(others[:4])}{' ...' if len(others) > 4 else ''})" if others else ""
            raise ValueError(f"{ob.name}'s mesh {me.name} is shared by {max(me.users, len(users))} users{named}. "
                             "to_object(..., shared='all') updates every object that uses it (linked duplicates); "
                             "shared='copy' gives this object its own mesh")
    materials = list(me.materials) if keep_slots else []
    if me.shape_keys is not None:
        _warn(f"{ob.name}: its shape keys cannot follow new geometry; the mesh was replaced without them")
        new = bpy.data.meshes.new(me.name)
        bm.to_mesh(new)
        old_name = me.name
        for user in users or [ob]:
            user.data = new
        if me.users == 0:
            bpy.data.meshes.remove(me)
        new.name = old_name
        me = new
        for mat in materials:
            me.materials.append(mat)
    else:
        had_uv = len(me.uv_layers) > 0
        had_groups = len(ob.vertex_groups) > 0
        bm.to_mesh(me)
        if had_uv and not len(me.uv_layers):
            _warn(f"{ob.name}: the new geometry has no UV map (the old one had); pass uv='box' to to_object")
        if had_groups:
            _warn(f"{ob.name}: vertex group weights do not survive new geometry")
    if smooth_deg is not None:
        smooth_by_angle(me, smooth_deg)
    me.update()
    return me


# ----------------------------------------------------------------------------- Solid
def _fmt(v) -> str:
    if isinstance(v, float):
        return meshdata.fmt_num(v, 4)
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(_fmt(x) for x in v) + ")"
    return repr(v) if isinstance(v, str) else str(v)


def _call(name: str, *args, **kwargs) -> str:
    parts = [_fmt(a) for a in args] + [f"{k}={_fmt(v)}" for k, v in kwargs.items() if v not in (None, False, 0, 0.0, "")]
    return f"{name}({', '.join(parts)})"


class Solid:
    """A mesh as a value. Every method returns a new Solid; nothing touches Blender until to_object."""

    __slots__ = ("bm", "desc", "log", "meta")

    def __init__(self, bm, desc: str = "solid", log: list | None = None, meta: dict | None = None):
        self.bm = bm
        self.desc = desc
        self.log = list(log or [])
        # What to record on the object: the part it was built from (vsblender.part) and its parameters.
        self.meta = dict(meta or {})

    # -- construction
    def copy(self) -> "Solid":
        return Solid(self.bm.copy(), self.desc, self.log, self.meta)

    def _derive(self, desc: str) -> "Solid":
        return Solid(self.bm.copy(), desc, self.log, self.meta)

    def __repr__(self) -> str:
        lo, hi = self.bounds
        size = hi - lo
        return f"<Solid {self.desc[:80]} {len(self.bm.faces)} faces size {size.x:.4g} x {size.y:.4g} x {size.z:.4g}>"

    # -- naming
    @property
    def label(self) -> str | None:
        """The name warnings use for this solid (named(), or the object to_object writes)."""
        return self.meta.get("label")

    def named(self, label: str) -> "Solid":
        """The same solid with a name that warnings about it use. Transforms and clean() keep it, and a
        boolean keeps the name of its first operand."""
        out = self._derive(self.desc)
        out.meta["label"] = str(label)
        return out

    # -- transforms
    def transform(self, matrix, desc: str | None = None) -> "Solid":
        out = self._derive(desc or f"{self.desc}.transform(...)")
        m = Matrix(matrix)
        bmesh.ops.transform(out.bm, matrix=m, verts=out.bm.verts)
        if m.to_3x3().determinant() < 0:
            bmesh.ops.reverse_faces(out.bm, faces=out.bm.faces)
        out.bm.normal_update()
        return out

    def move(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> "Solid":
        if isinstance(x, (list, tuple, Vector)):
            x, y, z = (list(x) + [0.0, 0.0])[:3]
        return self.transform(Matrix.Translation((x, y, z)), f"{self.desc}.move({_fmt(x)}, {_fmt(y)}, {_fmt(z)})")

    def rotate(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, about=(0.0, 0.0, 0.0)) -> "Solid":
        """Rotate by Euler angles in degrees (XYZ order) about a point."""
        c = Vector(about)
        m = Matrix.Translation(c) @ Euler((math.radians(x), math.radians(y), math.radians(z)), "XYZ").to_matrix().to_4x4() \
            @ Matrix.Translation(-c)
        return self.transform(m, f"{self.desc}.rotate({_fmt(x)}, {_fmt(y)}, {_fmt(z)})")

    def scale(self, s=1.0, about=(0.0, 0.0, 0.0)) -> "Solid":
        sx, sy, sz = (s, s, s) if isinstance(s, (int, float)) else tuple(s)
        c = Vector(about)
        m = Matrix.Translation(c) @ Matrix.Diagonal((sx, sy, sz, 1.0)) @ Matrix.Translation(-c)
        return self.transform(m, f"{self.desc}.scale({_fmt(s)})")

    def mirror(self, axis: str = "X", both: bool = False) -> "Solid":
        """Mirror across the plane through the origin normal to axis. both=True keeps the original and welds."""
        axis = axis.upper()
        d = {"X": (-1, 1, 1), "Y": (1, -1, 1), "Z": (1, 1, -1)}[axis]
        if both:
            out = self._derive(f"{self.desc}.mirror({axis!r}, both=True)")
            mirror_weld(out.bm, axis)
            finish(out.bm)
            return out
        return self.transform(Matrix.Diagonal((*d, 1.0)), f"{self.desc}.mirror({axis!r})")

    def centered(self, x: bool = True, y: bool = True, z: bool = False) -> "Solid":
        lo, hi = self.bounds
        c = (lo + hi) / 2
        return self.move(-c.x if x else 0.0, -c.y if y else 0.0, -c.z if z else 0.0)

    def on_ground(self, z: float = 0.0) -> "Solid":
        lo, _hi = self.bounds
        return self.move(0.0, 0.0, z - lo.z)

    on_bed = on_ground

    def material(self, index: int) -> "Solid":
        """Set every face's material slot index (slots are assigned in to_object(materials=[...]))."""
        out = self._derive(f"{self.desc}.material({index})")
        for f in out.bm.faces:
            f.material_index = int(index)
        return out

    def bevel(self, width: float, segments: int = 3, angle: float = 30.0, profile: float = 0.5,
              edges: str = "sharp") -> "Solid":
        """Round (or chamfer, segments=1) the edges sharper than angle degrees. edges: sharp, all,
        vertical, top, bottom (top/bottom: edges at the highest/lowest z)."""
        out = self._derive(f"{self.desc}.bevel({_fmt(width)}, segments={segments})")
        sel = _select_edges(out.bm, edges, angle)
        if sel:
            bmesh.ops.bevel(out.bm, geom=sel, offset=float(width), offset_type="OFFSET", segments=max(1, int(segments)),
                            profile=float(profile), affect="EDGES", clamp_overlap=True)
            out.bm.normal_update()
        return out

    def array(self, count: int, offset) -> "Solid":
        """count copies, each moved by offset from the last, combined without a boolean."""
        step = Vector(offset)
        parts = [self.move(*(step * i)) for i in range(int(count))]
        out = join(*parts)
        out.desc = f"{self.desc}.array({count}, {_fmt(tuple(step))})"
        return out

    def polar(self, count: int, axis: str = "Z", center=(0.0, 0.0, 0.0), angle: float = 360.0) -> "Solid":
        """count copies rotated about an axis through center, combined without a boolean."""
        axis = axis.upper()
        step = angle / count if abs(angle) >= 360 else angle / max(1, count - 1)
        parts = []
        for i in range(int(count)):
            a = step * i
            rot = {"X": (a, 0, 0), "Y": (0, a, 0), "Z": (0, 0, a)}[axis]
            parts.append(self.rotate(*rot, about=center))
        out = join(*parts)
        out.desc = f"{self.desc}.polar({count}, {axis!r})"
        return out

    # -- booleans
    def union(self, *others, solver: str = "auto", union_cutters: bool = False) -> "Solid":
        return _boolean(self, list(others), "UNION", solver, union_cutters)

    def difference(self, *others, solver: str = "auto", union_cutters: bool = False) -> "Solid":
        """self minus others. union_cutters=True when a cutter's shells overlap each other (they
        are then subtracted as their union; the warning says when it is needed)."""
        return _boolean(self, list(others), "DIFFERENCE", solver, union_cutters)

    def intersect(self, *others, solver: str = "auto", union_cutters: bool = False) -> "Solid":
        """self and others in common. union_cutters=True: the others (and the shells inside each, a
        geo.join of parts that touch or overlap) are unioned first, so the result is self & (b | c | ...);
        without it several operands give self & b & c, and a join whose shells overlap reports volume
        that no single part has."""
        return _boolean(self, list(others), "INTERSECT", solver, union_cutters)

    def clean(self, dist: float | None = None) -> "Solid":
        """Remove the zero-area slivers booleans leave, without welding separate shells and without
        merging faces into n-gons (an n-gon around a hole can be filled across it when tessellated).

        A sliver whose shortest edge is under dist has that edge collapsed, only where the mesh stays
        manifold (the edge's ends share exactly the two neighbours of its faces). A sliver with a vertex
        lying on its long edge has that edge flipped with the neighbouring triangle. Faces next to a
        sliver are triangulated first. What cannot be fixed safely is left and counted in the log.

        The result is checked against the input: more open, non-manifold or flipped edges, more face
        crossings (meshdata.crossings), more shells, or a changed volume return the input unchanged,
        with a warning that names the solid (named()). dist: the length below which an edge or a
        triangle's height is a sliver; default diagonal x 1e-8.
        """
        tol = float(dist) if dist else _merge_dist(self.bm) * 1e-2
        if not _sliver_faces(self.bm, tol):
            out = self._derive(self.desc)
            out.log.append("clean: no slivers")
            return out
        before = self.check(crossings=True)
        out = self._derive(f"{self.desc}.clean()")
        fixed, left = _remove_slivers(out.bm, tol)
        loose = [v for v in out.bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(out.bm, geom=loose, context="VERTS")
        out.bm.normal_update()
        after = out.check(crossings=True)
        worse = [k for k in ("open_edges", "non_manifold_edges", "flipped_edges", "crossings", "shells") if after[k] > before[k]]
        scale = max(abs(before["volume"]), float(np.prod([max(v, 1e-12) for v in before["size"]])) * 1e-6, 1e-30)
        if abs(after["volume"] - before["volume"]) > scale * 1e-6:
            worse.append("volume")
        if worse:
            changes = ", ".join(f"{k} {_fmt(before[k])} -> {_fmt(after[k])}" for k in worse)
            _warn(f"clean() on {_who(self)} would have changed {changes}; the solid was left as it was")
            out.bm.free()
            return self._derive(self.desc)
        out.log.append(f"clean: {_zero_area(self)} -> {_zero_area(out)} zero-area triangles "
                       f"({fixed} collapsed or flipped, {left} left)")
        return out

    def __add__(self, other):
        return self.union(other)

    def __sub__(self, other):
        return self.difference(other)

    def __and__(self, other):
        return self.intersect(other)

    # -- queries
    @property
    def bounds(self) -> tuple:
        if not len(self.bm.verts):
            return Vector((0, 0, 0)), Vector((0, 0, 0))
        co = np.array([v.co[:] for v in self.bm.verts])
        return Vector(co.min(axis=0)), Vector(co.max(axis=0))

    @property
    def size(self) -> Vector:
        lo, hi = self.bounds
        return hi - lo

    @property
    def volume(self) -> float:
        return float(self.bm.calc_volume(signed=True))

    @property
    def area(self) -> float:
        return float(sum(f.calc_area() for f in self.bm.faces))

    @property
    def tris(self) -> int:
        return int(sum(len(f.verts) - 2 for f in self.bm.faces))

    @property
    def is_manifold(self) -> bool:
        return bool(len(self.bm.faces)) and all(e.is_manifold for e in self.bm.edges)

    def arrays(self) -> meshdata.MeshArrays:
        return meshdata.from_bmesh(self.bm)

    def check(self, crossings: bool = False) -> dict:
        """Closed, consistently wound, shells and volume, in scene units.

        Welded the way check_model and the exporter weld (meshdata.best_weld): the near-coincident
        vertices booleans leave are joined, unless joining them makes more open or non-manifold edges
        than float-noise welding does. crossings=True also counts triangle pairs that meet without being
        neighbours, on the unwelded mesh (meshdata.crossings): cut, overlap, touch, and their sum.
        """
        arrays = self.arrays()
        stats = meshdata.stable_stats(arrays)
        stats["size"] = [round(v, 6) for v in self.size]
        if crossings:
            found = meshdata.crossings(arrays)
            stats["crossings"] = found["cut"] + found["overlap"] + found["touch"]
            stats["crossing_kinds"] = {k: found[k] for k in ("cut", "overlap", "touch")}
        return stats

    # -- output
    def to_bmesh(self):
        return self.bm.copy()

    def to_mesh(self, name: str):
        me = bpy.data.meshes.new(name)
        self.bm.to_mesh(me)
        return me

    def to_object(self, name: str, materials=None, collection=None, smooth_deg: float | None = 30.0,
                  uv: str | None = "box", modifiers=None, replace: bool = True, parent=None, space: str = "world",
                  shared: str = "error", hardware: bool | None = None):
        """Create the object, or update an existing one of that name in place.

        space="world" (default): the solid is in world coordinates; an existing object keeps its
        placement and the geometry is mapped into it. space="local": the solid is in the object's own
        frame (with parent=, the parent's frame), written as is: right for children of a rotated root
        and for objects whose transform is keyed, which are never touched. parent: an object or name;
        new objects go into the parent's collection unless collection is given.
        materials: names or Material datablocks for the slots (created as plain materials when
        missing; vsblender.material sets their look). smooth_deg: smooth shading with sharp edges
        above this angle (None: flat). uv="box": box-projected UVs. modifiers: [("BEVEL", {"width":
        0.002}), ...], created or updated by name. Records how it was built in ob["ai_spec"].
        shared: when the object's mesh is shared with other objects (linked duplicates, a print copy),
        "all" writes the new geometry for all of them, "copy" splits this object off, "error" refuses.
        hardware: True marks it a stand-in (vsblender_hardware: a bearing, a motor, glass), which
        check_model and export_model leave out by default; False clears the mark.
        """
        if space not in ("world", "local"):
            raise ValueError("space must be world or local")
        parent_ob = parent
        if isinstance(parent, str):
            parent_ob = bpy.data.objects.get(parent)
            if parent_ob is None:
                raise KeyError(f"no object named {parent!r} to parent {name} to")
        ob = bpy.data.objects.get(name)
        bm = self.bm.copy()
        try:
            if uv == "box":
                uv_box(bm)
            if ob is not None and replace:
                if ob.type != "MESH":
                    raise ValueError(f"{name} exists and is a {ob.type}, not a mesh")
                if parent_ob is not None and ob.parent != parent_ob:
                    world = ob.matrix_world.copy()
                    ob.parent = parent_ob
                    if space == "local":
                        ob.matrix_parent_inverse.identity()
                    else:
                        ob.matrix_parent_inverse = parent_ob.matrix_world.inverted_safe()
                        ob.matrix_world = world
                    bpy.context.view_layer.update()
                if space == "world":
                    if _transform_keyed(ob):
                        _warn(f"{name}'s transform is keyed, so world-space geometry was mapped through its pose at frame "
                              f"{bpy.context.scene.frame_current}; pass space='local' to write it in the object's own frame")
                    inverse = ob.matrix_world.inverted_safe()
                    bmesh.ops.transform(bm, matrix=inverse, verts=bm.verts)
                    if inverse.to_3x3().determinant() < 0:
                        bmesh.ops.reverse_faces(bm, faces=bm.faces)
                replace_mesh(ob, bm, keep_slots=True, shared=shared)
            else:
                if ob is not None:
                    raise ValueError(f"{name} already exists (replace=False)")
                me = bpy.data.meshes.new(name)
                bm.to_mesh(me)
                ob = bpy.data.objects.new(name, me)
                coll = collection
                if isinstance(coll, str):
                    coll = bpy.data.collections.get(coll)
                    if coll is None:
                        coll = bpy.data.collections.new(collection)
                        bpy.context.scene.collection.children.link(coll)
                if coll is None and parent_ob is not None and parent_ob.users_collection:
                    coll = parent_ob.users_collection[0]
                (coll or bpy.context.scene.collection).objects.link(ob)
                if parent_ob is not None:
                    ob.parent = parent_ob
                    if space == "local":
                        ob.matrix_parent_inverse.identity()
                    else:
                        # World-space geometry stays where it is: the parent's transform is undone.
                        ob.matrix_parent_inverse = parent_ob.matrix_world.inverted_safe()
        finally:
            bm.free()
        me = ob.data
        if materials is not None:
            import array

            from . import looks

            mats = [m if isinstance(m, bpy.types.Material) else looks.ensure_material(str(m)) for m in materials]
            current = list(me.materials)
            if [m.name for m in current if m] != [m.name for m in mats]:
                # clear() resets every polygon material_index to 0. Keep the indices the solid
                # already set, and skip clear() on a mesh that has no slots yet.
                count = len(me.polygons)
                saved = array.array("i", [0]) * count if count else None
                if saved is not None:
                    me.polygons.foreach_get("material_index", saved)
                if current:
                    me.materials.clear()
                for m in mats:
                    me.materials.append(m)
                if saved is not None and len(me.materials):
                    limit = len(me.materials) - 1
                    if any(i > limit for i in saved):
                        saved = array.array("i", (i if i <= limit else limit for i in saved))
                    me.polygons.foreach_set("material_index", saved)
        if smooth_deg is not None:
            smooth_by_angle(me, smooth_deg)
        else:
            for p in me.polygons:
                p.use_smooth = False
        if modifiers:
            from . import helpers

            for item in modifiers:
                if isinstance(item, dict):
                    item = dict(item)
                    kind = item.pop("type")
                    helpers.modifier(ob, kind, **item)
                else:
                    kind, props = item[0], (item[1] if len(item) > 1 else {})
                    helpers.modifier(ob, kind, **dict(props))
        me.update()
        try:
            spec = json.loads(ob.get("ai_spec") or "{}") if isinstance(ob.get("ai_spec"), str) else {}
        except ValueError:
            spec = {}
        spec["geo"] = self.desc[:1500]
        lo, hi = self.bounds
        spec["size"] = [round(float(v), 6) for v in (hi - lo)]
        if self.meta.get("part"):
            spec["part"] = self.meta["part"]
            spec["part_params"] = self.meta.get("params") or {}
        if hardware is not None:
            from . import marks

            marks.set_hardware(ob, bool(hardware))
        text = json.dumps(spec, default=str)
        if len(text) > 2000:  # the geo description goes first: the parameters are what matter
            spec["geo"] = spec["geo"][: max(0, 1500 - (len(text) - 2000))]
            text = json.dumps(spec, default=str)[:2000]
        ob["ai_spec"] = text
        return ob


def _who(solid) -> str:
    """How a warning names a solid: its label (named(), to_object), else the start of its description."""
    label = solid.meta.get("label") if solid is not None else None
    if label:
        return str(label)
    desc = solid.desc if solid is not None else "solid"
    return desc if len(desc) <= 60 else desc[:57] + "..."


def _sliver_faces(bm, tol: float) -> list:
    """Faces whose tessellation has a triangle with an edge, or a height, under tol."""
    if not len(bm.faces):
        return []
    bm.faces.index_update()
    arrays = meshdata.from_bmesh(bm)
    if not len(arrays.tris):
        return []
    v, t = arrays.verts, arrays.tris
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    edges = np.stack([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], axis=1)
    longest = edges.max(axis=1)
    twice_area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    height = np.divide(twice_area, longest, out=np.zeros_like(twice_area), where=longest > 0)
    bad = (edges.min(axis=1) < tol) | (height < tol)
    if not bad.any():
        return []
    bm.faces.ensure_lookup_table()
    return [bm.faces[int(i)] for i in np.unique(arrays.face[bad])]


def _can_collapse(edge) -> bool:
    """The link condition: collapsing this edge keeps a manifold triangle mesh manifold."""
    if not edge.is_manifold or len(edge.link_faces) != 2:
        return False
    a, b = edge.verts
    if not all(e.is_manifold for e in list(a.link_edges) + list(b.link_edges)):
        return False
    opposite = set()
    for f in edge.link_faces:
        if len(f.verts) != 3:
            return False
        opposite.update(v for v in f.verts if v is not a and v is not b)
    common = {e.other_vert(a) for e in a.link_edges} & {e.other_vert(b) for e in b.link_edges}
    return len(opposite) == 2 and common == opposite


def _flip_cap(edge, face) -> bool:
    """Flip the long edge of a cap sliver (face) with the triangle across it, when the sliver's third
    vertex lies inside that edge and the two new triangles face the way the neighbour does."""
    if not edge.is_manifold or len(edge.link_faces) != 2:
        return False
    other = edge.link_faces[0] if edge.link_faces[1] == face else edge.link_faces[1]
    if len(other.verts) != 3 or len(face.verts) != 3:
        return False
    a, b = edge.verts
    v = next(x for x in face.verts if x is not a and x is not b)
    w = next(x for x in other.verts if x is not a and x is not b)
    ab = b.co - a.co
    length2 = ab.length_squared
    if length2 <= 0:
        return False
    t = (v.co - a.co).dot(ab) / length2
    if not (0.0 < t < 1.0):
        return False
    if any(e.other_vert(v) is w for e in v.link_edges):
        return False
    other.normal_update()
    reference = other.normal.copy()
    new = bmesh.utils.edge_rotate(edge, False)
    if new is None:
        return False
    for f in new.link_faces:
        f.normal_update()
        if f.normal.dot(reference) <= 0:
            bmesh.utils.edge_rotate(new, True)
            return False
    return True


def _remove_slivers(bm, tol: float, rounds: int = 8) -> tuple:
    """(fixed, left): collapse short edges and flip cap slivers, in rounds until nothing changes."""
    fixed = 0
    for _ in range(rounds):
        faces = _sliver_faces(bm, tol)
        if not faces:
            return fixed, 0
        region = set(faces)
        for f in faces:
            for v in f.verts:
                region.update(v.link_faces)
        ngons = [f for f in region if len(f.verts) > 3]
        if ngons:
            bmesh.ops.triangulate(bm, faces=ngons, quad_method="BEAUTY", ngon_method="BEAUTY")
        changed = 0
        for f in _sliver_faces(bm, tol):
            if not f.is_valid or len(f.verts) != 3:
                continue
            ordered = sorted(f.edges, key=lambda e: e.calc_length())
            short, long_ = ordered[0], ordered[-1]
            if short.calc_length() < tol:
                if _can_collapse(short):
                    bmesh.ops.collapse(bm, edges=[short], uvs=True)
                    changed += 1
                continue
            if _flip_cap(long_, f):
                changed += 1
        fixed += changed
        if not changed:
            break
    return fixed, len(_sliver_faces(bm, tol))


def _zero_area(solid) -> int:
    """Triangles with no area, as check_model counts them (relative to the size of the solid)."""
    arrays = solid.arrays()
    if not len(arrays.tris):
        return 0
    lo, hi = arrays.bounds()
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    _n, areas = meshdata.tri_normals_areas(arrays)
    return int((areas < (diag * 1e-7) ** 2).sum())


def _transform_keyed(ob) -> bool:
    ad = ob.animation_data
    if ad is None or ad.action is None:
        return False
    from . import helpers

    return any(fc.data_path in ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale")
               for fc in helpers.fcurves(ob)) or any(d.data_path in ("location", "rotation_euler", "scale") for d in ad.drivers)


def _select_edges(bm, which: str, angle: float) -> list:
    which = (which or "sharp").lower()
    co = np.array([v.co[:] for v in bm.verts]) if len(bm.verts) else np.zeros((1, 3))
    zmin, zmax = float(co[:, 2].min()), float(co[:, 2].max())
    tol = max(1e-9, (zmax - zmin) * 1e-6)
    limit = math.radians(angle)
    out = []
    for e in bm.edges:
        if not e.is_manifold:
            continue
        sharp = e.calc_face_angle(0.0) > limit
        a, b = e.verts[0].co, e.verts[1].co
        d = (b - a)
        vertical = d.length > 0 and abs(d.normalized().z) > 0.999
        top = abs(a.z - zmax) < tol and abs(b.z - zmax) < tol
        bottom = abs(a.z - zmin) < tol and abs(b.z - zmin) < tol
        keep = {"all": sharp, "sharp": sharp, "vertical": sharp and vertical, "top": sharp and top,
                "bottom": sharp and bottom, "top+vertical": sharp and (top or vertical),
                "bottom+vertical": sharp and (bottom or vertical), "top+bottom": sharp and (top or bottom)}.get(which)
        if keep is None:
            raise ValueError("edges must be sharp, all, vertical, top, bottom, top+vertical, bottom+vertical or top+bottom")
        if keep:
            out.append(e)
    return out


def _new_bm() -> "bmesh.types.BMesh":
    return bmesh.new()


def _solid_from(builder, desc: str) -> Solid:
    bm = bmesh.new()
    builder(bm)
    finish(bm)
    return Solid(bm, desc)


def solid(src, evaluated: bool = True) -> Solid:
    """A Solid from a BMesh (copied), a Mesh, an Object (world space, after modifiers when evaluated),
    MeshArrays, or a function that fills a BMesh."""
    bm = bmesh.new()
    if isinstance(src, bmesh.types.BMesh):
        bm.free()
        return Solid(src.copy(), "solid(bmesh)")
    if callable(src) and not isinstance(src, (bpy.types.ID, meshdata.MeshArrays)):
        src(bm)
        return Solid(bm, "solid(builder)")
    if isinstance(src, bpy.types.Mesh):
        bm.from_mesh(src)
        return Solid(bm, f"solid({src.name!r})")
    if isinstance(src, bpy.types.Object):
        if evaluated:
            deps = bpy.context.evaluated_depsgraph_get()
            me = bpy.data.meshes.new_from_object(src.evaluated_get(deps), depsgraph=deps)
        else:
            me = bpy.data.meshes.new_from_object(src)
        try:
            bm.from_mesh(me)
        finally:
            bpy.data.meshes.remove(me)
        bmesh.ops.transform(bm, matrix=src.matrix_world, verts=bm.verts)
        if src.matrix_world.to_3x3().determinant() < 0:
            bmesh.ops.reverse_faces(bm, faces=bm.faces)
        return Solid(bm, f"solid({src.name!r})")
    if isinstance(src, meshdata.MeshArrays):
        me = meshdata.to_mesh(src, "_vsblender_geo_arrays")
        try:
            bm.from_mesh(me)
        finally:
            bpy.data.meshes.remove(me)
        return Solid(bm, "solid(arrays)")
    bm.free()
    raise TypeError("solid() takes a BMesh, Mesh, Object, MeshArrays or a builder function")


def join(*solids) -> Solid:
    """Combine solids into one mesh without a boolean (overlaps stay as separate shells)."""
    items = [s for s in solids if s is not None]
    if len(items) == 1 and isinstance(items[0], (list, tuple)):
        items = list(items[0])
    bm = bmesh.new()
    log = []
    for s in items:
        me = bpy.data.meshes.new("_vsblender_geo_join")
        try:
            s.bm.to_mesh(me)
            bm.from_mesh(me)
        finally:
            bpy.data.meshes.remove(me)
        log += s.log
    return Solid(bm, "join(" + ", ".join(s.desc for s in items)[:600] + ")", log)


# ----------------------------------------------------------------------------- booleans
def _solvers() -> list:
    try:
        return [e.identifier for e in bpy.types.BooleanModifier.bl_rna.properties["solver"].enum_items]
    except Exception:
        return ["EXACT"]


def _closed(bm) -> bool:
    return bool(len(bm.faces)) and all(e.is_manifold for e in bm.edges)


def _shell_labels(bm) -> list:
    """Shell index per face (faces connected through edges)."""
    bm.faces.ensure_lookup_table()
    label = [-1] * len(bm.faces)
    shell = 0
    for start in bm.faces:
        if label[start.index] >= 0:
            continue
        stack = [start]
        label[start.index] = shell
        while stack:
            face = stack.pop()
            for edge in face.edges:
                for other in edge.link_faces:
                    if label[other.index] < 0:
                        label[other.index] = shell
                        stack.append(other)
        shell += 1
    return label


def crossing_shells(bm, max_faces: int = 400_000) -> tuple:
    """(shells, crossing face pairs between different shells) of one solid. Shells that cut through
    each other are not one closed solid, which the Manifold solver needs from each operand."""
    if len(bm.faces) > max_faces:
        return 0, 0
    bm.faces.index_update()
    labels = _shell_labels(bm)
    shells = max(labels) + 1 if labels else 0
    if shells < 2:
        return shells, 0
    from mathutils.bvhtree import BVHTree

    tree = BVHTree.FromBMesh(bm, epsilon=0.0)
    pairs = tree.overlap(tree)
    crossing = sum(1 for a, b in pairs if a < b and labels[a] != labels[b])
    return shells, crossing


def _split_shells(bm) -> list:
    """One BMesh per shell, so each becomes its own boolean operand."""
    bm.faces.index_update()
    labels = _shell_labels(bm)
    count = max(labels) + 1 if labels else 0
    if count < 2:
        return [bm.copy()]
    arrays = meshdata.from_bmesh(bm)
    tri_shell = np.asarray(labels, dtype=np.int64)[arrays.face]
    out = []
    for shell in range(count):
        tris = arrays.tris[tri_shell == shell]
        used, inverse = np.unique(tris, return_inverse=True)
        part = meshdata.MeshArrays(arrays.verts[used], inverse.reshape(-1, 3),
                                   material=arrays.material[tri_shell == shell] if arrays.material is not None else None)
        me = meshdata.to_mesh(part, "_vsblender_geo_shell")
        try:
            piece = bmesh.new()
            piece.from_mesh(me)
        finally:
            bpy.data.meshes.remove(me)
        out.append(piece)
    return out


_PARITY_DIRS = (Vector((0.31, 0.27, 0.91)).normalized(), Vector((-0.6, 0.71, -0.36)).normalized())


def _parity_inside(tree, point, step: float):
    """Ray parity in two directions: True inside, False outside, None when they disagree (a ray grazed an edge)."""
    votes = []
    for direction in _PARITY_DIRS:
        n, q = 0, Vector(point)
        for _ in range(2000):
            loc, _nrm, _i, _d = tree.ray_cast(q, direction, 1e12)
            if loc is None:
                break
            n += 1
            q = loc + direction * step
        votes.append(n % 2 == 1)
    return votes[0] if votes[0] == votes[1] else None


def _shell_inside(piece, tree, tol: float):
    """Whether one closed shell, whose surface does not cross the other solid's, lies inside it.
    Vertices on the other surface (touching) are skipped; None when no vertex decides."""
    piece.verts.ensure_lookup_table()
    count = len(piece.verts)
    votes = []
    for i in sorted({int(k) for k in np.linspace(0, count - 1, min(count, 16))}):
        co = piece.verts[i].co
        hit = tree.find_nearest(co)
        if hit[0] is None or hit[3] <= tol:
            continue
        vote = _parity_inside(tree, co, tol)
        if vote is None:
            continue
        if votes and vote != votes[0]:
            return None
        votes.append(vote)
        if len(votes) == 3:
            break
    return votes[0] if votes else None


def _disjoint_intersect(base_bm, cut_bm):
    """The intersection of two closed solids whose surfaces never cross: the shells of each that lie
    inside the other, decided by ray parity, so no solver has to guess. The solvers decide this case
    by a containment test that can be wrong: Exact once returned a whole board standing clear in a
    box's hollow, after Manifold's correct empty result was retried. None when the surfaces meet, an
    operand is not closed, or parity cannot decide a shell; the solvers run then."""
    from mathutils.bvhtree import BVHTree

    if not (len(base_bm.faces) and len(cut_bm.faces) and _closed(base_bm) and _closed(cut_bm)):
        return None
    tree_base = BVHTree.FromBMesh(base_bm, epsilon=0.0)
    tree_cut = BVHTree.FromBMesh(cut_bm, epsilon=0.0)
    if tree_base.overlap(tree_cut):
        return None
    # Shells that cut through each other (a join of overlapping parts) are not one solid: parity
    # counts their overlap as outside. Those go to the solvers, which warn about them.
    if crossing_shells(base_bm)[1] or crossing_shells(cut_bm)[1]:
        return None
    co = np.array([v.co[:] for v in base_bm.verts] + [v.co[:] for v in cut_bm.verts])
    # Touching distance and ray step alike: well above float32 noise in the BVH, far below a wall.
    tol = float(np.linalg.norm(co.max(axis=0) - co.min(axis=0))) * 1e-6 + 1e-12
    kept, pieces = [], []
    try:
        for bm, other in ((base_bm, tree_cut), (cut_bm, tree_base)):
            for piece in _split_shells(bm):
                pieces.append(piece)
                inside = _shell_inside(piece, other, tol)
                if inside is None:
                    return None
                if inside:
                    kept.append(piece)
        result = bmesh.new()
        for piece in kept:
            me = bpy.data.meshes.new("_vsblender_geo_disjoint")
            try:
                piece.to_mesh(me)
                result.from_mesh(me)
            finally:
                bpy.data.meshes.remove(me)
        return result
    finally:
        for piece in pieces:
            piece.free()


def _boolean(base: Solid, others: list, op: str, solver: str = "auto", union_cutters: bool = False) -> Solid:
    """Blender's Boolean modifier on temporary objects. union_cutters: every shell of every operand
    becomes its own operand, so shells that overlap inside one cutter (studs drawn over groove lines)
    cut as their union instead of leaving crescents; without it, such overlaps are reported."""
    others = [o for o in others if o is not None]
    symbol = {"UNION": "+", "DIFFERENCE": "-", "INTERSECT": "&"}[op]
    desc = f"({base.desc}) {symbol} " + f" {symbol} ".join(f"({o.desc})" for o in others)
    if not others:
        return base._derive(base.desc)
    shells, crossing = crossing_shells(base.bm)
    if crossing:
        _warn(f"{op.lower()}: {_who(base)} has {shells} shells that cut through each other ({crossing} crossing face pairs), "
              "so it is not one closed solid and the result can count the overlap twice. Union its parts first "
              "(geo.union(*parts, union_cutters=True)).")
    if not union_cutters:
        for o in others:
            shells, crossing = crossing_shells(o.bm)
            if crossing:
                fix = "a.intersect(b, union_cutters=True)" if op == "INTERSECT" else "a.difference(b, union_cutters=True)"
                _warn(f"{op.lower()}: {_who(o)} has {shells} shells, and some cut through each other ({crossing} crossing face "
                      f"pairs). Booleans need each operand to be one closed solid, so the overlaps can "
                      f"{'count twice' if op == 'INTERSECT' else 'cut as crescents instead of their union'}. "
                      f"Pass union_cutters=True ({fix}).")
                break
    if union_cutters and op == "INTERSECT":
        # self & (b | c | ...): Blender intersects every operand of a collection with every other.
        pieces = [Solid(bm, f"shell of {_who(o)}") for o in others for bm in _split_shells(o.bm)]
        if len(pieces) > 1:
            whole = _boolean_run(pieces[0], pieces[1:], [p.bm for p in pieces[1:]], "UNION", solver,
                                 "union of the operands", True)
        else:
            whole = pieces[0]
        try:
            out = _boolean_run(base, [whole], [whole.bm], op, solver, desc, False)
        finally:
            for p in pieces:
                p.bm.free()
            if whole not in pieces:
                whole.bm.free()
        return out
    other_bms = [o.bm for o in others]
    split = []
    if union_cutters:
        for bm in other_bms:
            split.extend(_split_shells(bm))
        other_bms = split
    try:
        return _boolean_run(base, others, other_bms, op, solver, desc, union_cutters)
    finally:
        for bm in split:
            bm.free()


def _open_edges(bm) -> int:
    return sum(1 for e in bm.edges if not e.is_manifold)


def _volume_bound(op: str, base_bm, other_bms: list) -> tuple:
    """(largest believable result volume, what it is) for a boolean, from the volumes of the operands
    that are closed. (None, "") when no closed operand bounds it: an open mesh has no volume to trust."""
    closed_b = _closed(base_bm)
    if op == "INTERSECT":
        trusted = ([abs(float(base_bm.calc_volume(signed=True)))] if closed_b else []) + \
                  [abs(float(bm.calc_volume(signed=True))) for bm in other_bms if _closed(bm)]
        return (min(trusted), "the smaller closed operand") if trusted else (None, "")
    if op == "DIFFERENCE":
        return (abs(float(base_bm.calc_volume(signed=True))), "the solid it was cut from") if closed_b else (None, "")
    if closed_b and all(_closed(bm) for bm in other_bms):
        total = abs(float(base_bm.calc_volume(signed=True))) + sum(abs(float(bm.calc_volume(signed=True))) for bm in other_bms)
        return total, "the operands together"
    return None, ""


def _boolean_run(base: Solid, others: list, other_bms: list, op: str, solver: str, desc: str, union_cutters: bool) -> Solid:
    available = _solvers()
    wanted = str(solver or "auto").upper()
    if wanted == "AUTO":
        closed = _closed(base.bm) and all(_closed(o.bm) for o in others)
        order = (["MANIFOLD"] if closed and "MANIFOLD" in available else []) + ["EXACT"]
    else:
        if wanted == "FAST":
            wanted = "FLOAT"
        if wanted not in available:
            raise ValueError(f"solver must be auto or one of {', '.join(available)}")
        order = [wanted]
    log = list(base.log)
    for o in others:
        log += o.log
    operands = [base] + list(others)
    not_closed = [o for o in operands if not _closed(o.bm)]
    if wanted == "AUTO" and not_closed and "MANIFOLD" in available:
        for o in not_closed:
            _warn(f"{op.lower()} used the Exact solver because {_who(o)} is not closed ({_open_edges(o.bm)} open or "
                  "non-manifold edges); the Manifold solver needs every operand closed (o.check(), o.clean())")
    bound, bound_what = _volume_bound(op, base.bm, other_bms)
    lo, hi = base.bounds
    slack = float((hi - lo).length) ** 3 * 1e-9 + 1e-15
    meta = {"label": base.label} if base.label else {}
    if op == "INTERSECT" and len(other_bms) == 1:
        decided = _disjoint_intersect(base.bm, other_bms[0])
        if decided is not None:
            what = "nothing in common" if not len(decided.faces) else f"volume {_fmt(abs(float(decided.calc_volume(signed=True))))}"
            log.append(f"intersect: the surfaces do not meet; ray parity decided it ({what}), no solver")
            return Solid(decided, desc, log, meta)
    last_error = None
    for index, name in enumerate(order):
        import time
        started = time.time()
        try:
            bm = _run_boolean(base.bm, other_bms, op, name)
        except Exception as exc:  # a solver that refuses the input: try the next one
            last_error = exc
            continue
        ms = int((time.time() - started) * 1000)
        ok = len(bm.faces) > 0 or op == "INTERSECT"
        closed_in = _closed(base.bm) and all(_closed(o.bm) for o in others)
        result = abs(float(bm.calc_volume(signed=True))) if len(bm.faces) else 0.0
        if ok and bound is not None and result > bound * 1.001 + slack:
            because = f" ({', '.join(_who(o) for o in not_closed)} not closed)" if not_closed else ""
            message = (f"{op.lower()} with the {name} solver gave a volume of {_fmt(result)}, more than {bound_what} "
                       f"({_fmt(bound)}){because}: the result is inside out or overlaps itself")
            if wanted != "AUTO":
                _warn(message + "; it was kept because solver= was given")
            elif index < len(order) - 1:
                bm.free()
                last_error = RuntimeError(message)
                continue
            else:
                bm.free()
                raise RuntimeError(f"boolean {message}. Make every operand closed (check(), clean()), or pass "
                                   "solver= to keep such a result")
        if ok and (not closed_in or _closed(bm) or index == len(order) - 1):
            extra = f", {len(other_bms)} cutter shells as separate operands" if union_cutters else ""
            log.append(f"{op.lower()}: {name} solver, {ms} ms{extra}" + ("" if index == 0 else " (after a retry)"))
            return Solid(bm, desc, log, meta)
        bm.free()
        last_error = RuntimeError(f"{name} gave an {'empty' if not ok else 'open'} result")
    raise RuntimeError(f"boolean {op.lower()} failed: {last_error}")


def _run_boolean(base_bm, other_bms: list, op: str, solver: str):
    """Evaluate a Boolean modifier on temporary objects (removed again) and read the result."""
    scene = bpy.context.scene
    temps = []
    collection = bpy.data.collections.new("_vsblender_geo_tmp")
    cutters = bpy.data.collections.new("_vsblender_geo_cut")
    scene.collection.children.link(collection)
    collection.children.link(cutters)
    try:
        me = bpy.data.meshes.new("_vsblender_geo_base")
        temps.append(me)
        base_bm.to_mesh(me)
        ob = bpy.data.objects.new("_vsblender_geo_base", me)
        temps.append(ob)
        collection.objects.link(ob)
        for i, obm in enumerate(other_bms):
            cme = bpy.data.meshes.new(f"_vsblender_geo_cut{i}")
            temps.append(cme)
            obm.to_mesh(cme)
            cob = bpy.data.objects.new(f"_vsblender_geo_cut{i}", cme)
            temps.append(cob)
            cutters.objects.link(cob)
        mod = ob.modifiers.new("_vsblender_bool", "BOOLEAN")
        mod.operation = op
        mod.operand_type = "COLLECTION"
        mod.collection = cutters
        mod.solver = solver
        try:
            mod.material_mode = "INDEX"
        except Exception:
            pass
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        result = bmesh.new()
        eval_me = bpy.data.meshes.new_from_object(ob.evaluated_get(deps), depsgraph=deps)
        temps.append(eval_me)
        result.from_mesh(eval_me)
        return result
    finally:
        for idb in reversed(temps):
            try:
                if isinstance(idb, bpy.types.Object):
                    bpy.data.objects.remove(idb, do_unlink=True)
                else:
                    bpy.data.meshes.remove(idb)
            except (ReferenceError, RuntimeError):
                pass
        for coll in (cutters, collection):
            try:
                bpy.data.collections.remove(coll)
            except (ReferenceError, RuntimeError):
                pass


def union(*solids, solver: str = "auto", union_cutters: bool = False) -> Solid:
    solids = [s for s in solids if s is not None]
    return solids[0].union(*solids[1:], solver=solver, union_cutters=union_cutters)


def difference(base: Solid, *cutters, solver: str = "auto", union_cutters: bool = False) -> Solid:
    return base.difference(*cutters, solver=solver, union_cutters=union_cutters)


def intersect(*solids, solver: str = "auto", union_cutters: bool = False) -> Solid:
    solids = [s for s in solids if s is not None]
    return solids[0].intersect(*solids[1:], solver=solver, union_cutters=union_cutters)


# ----------------------------------------------------------------------------- primitives
def _place(s: Solid, center: bool) -> Solid:
    """Primitives stand on z = 0 centred in X and Y, or are centred in all three with center=True."""
    out = s.centered(True, True, True) if center else s.centered(True, True, False).on_ground()
    out.desc = s.desc
    return out


def box(x: float, y: float | None = None, z: float | None = None, fillet: float = 0.0, chamfer: float = 0.0,
        edges: str = "all", center: bool = False, segments: int | None = None) -> Solid:
    """A box x by y by z. fillet rounds and chamfer bevels the chosen edges (all, vertical, top, bottom...)."""
    y = x if y is None else y
    z = x if z is None else z
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(float(x), float(y), float(z)), verts=bm.verts)
    s = Solid(bm, _call("box", x, y, z, fillet=fillet, chamfer=chamfer, edges=edges if (fillet or chamfer) and edges != "all" else None))
    if fillet or chamfer:
        width = float(fillet or chamfer)
        if chamfer and not fillet:
            segs = 1
        else:
            span = max(abs(float(x)), abs(float(y)), abs(float(z)))
            segs = _fillet_segments(span, width, segments)
        sel = _select_edges(s.bm, "sharp" if edges == "all" else edges, 30.0)
        bmesh.ops.bevel(s.bm, geom=sel, offset=width, offset_type="OFFSET", segments=segs, profile=0.5,
                        affect="EDGES", clamp_overlap=True)
        s.bm.normal_update()
    return _place(s, center)


def _radius(d, r) -> float:
    if d is None and r is None:
        raise ValueError("give d (diameter) or r (radius)")
    return float(r) if r is not None else float(d) / 2.0


def cylinder(d: float | None = None, h: float = 1.0, r: float | None = None, segments: int | None = None,
             tol: float | None = None, chamfer: float = 0.0, fillet: float = 0.0, center: bool = False) -> Solid:
    """A cylinder of diameter d (or radius r) and height h, with n-gon caps."""
    radius = _radius(d, r)
    n = segments or segments_for(radius, tol)
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=n, radius1=radius, radius2=radius, depth=float(h))
    s = Solid(bm, _call("cylinder", d=d, r=r, h=h, chamfer=chamfer, fillet=fillet))
    if chamfer or fillet:
        width = float(fillet or chamfer)
        if chamfer and not fillet:
            segs = 1
        else:
            span = max(abs(radius) * 2.0, abs(float(h)))
            segs = _fillet_segments(span, width, None)
        sel = _select_edges(s.bm, "top+bottom", 30.0)
        bmesh.ops.bevel(s.bm, geom=sel, offset=width, offset_type="OFFSET", segments=segs, profile=0.5,
                        affect="EDGES", clamp_overlap=True)
        s.bm.normal_update()
    return _place(s, center)


def cone(d1: float, d2: float, h: float, segments: int | None = None, center: bool = False) -> Solid:
    """A cone or frustum: diameter d1 at the bottom, d2 at the top (0 for a point)."""
    r1, r2 = float(d1) / 2, float(d2) / 2
    profile = [(0.0, 0.0), (r1, 0.0), (r2, float(h)), (0.0, float(h))]
    n = segments or segments_for(max(r1, r2))
    s = _solid_from(lambda bm: lathe(bm, profile, segments=n), _call("cone", d1, d2, h))
    return _place(s, center)


def tube(od: float, id: float, h: float, segments: int | None = None, center: bool = False) -> Solid:  # noqa: A002
    """A tube (pipe) of outer diameter od and inner diameter id."""
    ro, ri = float(od) / 2, float(id) / 2
    if ri >= ro:
        raise ValueError("id must be smaller than od")
    profile = [(ri, 0.0), (ro, 0.0), (ro, float(h)), (ri, float(h)), (ri, 0.0)]
    n = segments or segments_for(ro)
    s = _solid_from(lambda bm: lathe(bm, profile, segments=n), _call("tube", od, id, h))
    return _place(s, center)


def sphere(d: float | None = None, r: float | None = None, segments: int | None = None, kind: str = "uv",
           center: bool = False) -> Solid:
    """A sphere. kind uv (poles, good for smooth shading) or ico (even triangles, good for printing)."""
    radius = _radius(d, r)
    bm = bmesh.new()
    if kind == "ico":
        n = segments or segments_for(radius)
        subdiv = max(1, min(7, int(round(math.log(max(n, 12) / 5.0, 2)))))
        bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=radius)
    else:
        n = segments or segments_for(radius)
        bmesh.ops.create_uvsphere(bm, u_segments=n, v_segments=max(6, n // 2), radius=radius)
    return _place(Solid(bm, _call("sphere", d=d, r=r, kind=kind if kind != "uv" else None)), center)


def torus(D: float, d: float, segments: int | None = None, ring_segments: int | None = None, center: bool = False) -> Solid:
    """A torus: D is the diameter through the tube centres, d the tube diameter."""
    R, r = float(D) / 2, float(d) / 2
    m = ring_segments or segments_for(r)
    loop = geom2d.circle(2 * r, m, center=(R, r))
    profile = [tuple(p) for p in loop] + [tuple(loop[0])]
    n = segments or segments_for(R + r)
    s = _solid_from(lambda bm: lathe(bm, profile, segments=n), _call("torus", D, d))
    return _place(s, center)


def revolve(profile, segments: int | None = None, a0: float = 0.0, a1: float = 360.0, center: bool = False,
            place: bool = False) -> Solid:
    """Revolve a profile [(r, z), ...] about Z (see lathe). Kept where it is unless place=True."""
    s = _solid_from(lambda bm: lathe(bm, profile, segments=segments, a0=a0, a1=a1), _call("revolve", f"{len(profile)} points"))
    return _place(s, center) if place else s


def extrude(loops, h: float, z0: float = 0.0) -> Solid:
    """Extrude 2D loops (one outline, or outlines with holes by even-odd) from z0 up by h."""
    if isinstance(loops, np.ndarray) and loops.ndim == 2:
        loops = [loops]
    elif loops and isinstance(loops[0], (tuple, list)) and len(loops[0]) == 2 and isinstance(loops[0][0], (int, float)):
        loops = [loops]
    s = _solid_from(lambda bm: prism(bm, loops, float(z0), float(z0) + float(h)), _call("extrude", f"{len(loops)} loop(s)", h))
    return s


def sweep(*args, **kwargs):
    """sweep(bm, profile, path, ...) adds faces to bm; sweep(profile, path, ...) returns a Solid.

    profile: closed 2D loop (u, v) around the path; path: points or a function t -> point (t in 0..1,
    samples points). scale(t) and twist(t) (degrees) vary the profile along the path.
    """
    if args and isinstance(args[0], bmesh.types.BMesh):
        return _sweep_bm(*args, **kwargs)
    profile, path = args[0], args[1]
    rest = args[2:]
    return _solid_from(lambda bm: _sweep_bm(bm, profile, path, *rest, **kwargs), _call("sweep", "profile", "path"))


def loft(*args, **kwargs):
    """loft(bm, sections, ...) adds faces; loft(sections, ...) returns a Solid. Sections are closed
    loops of 3D points (or 2D at z = 0), resampled to the same count and aligned to avoid twisting."""
    if args and isinstance(args[0], bmesh.types.BMesh):
        return _loft_bm(*args, **kwargs)
    sections = args[0]
    return _solid_from(lambda bm: _loft_bm(bm, sections, *args[1:], **kwargs), _call("loft", f"{len(sections)} sections"))


def text(body: str, size: float, depth: float, font: str | None = None, align_x: str = "CENTER",
         align_y: str = "CENTER", spacing: float = 1.0, fix_overlaps: bool = False) -> Solid:
    """Text as a solid: size is the font size (cap height is about 0.7 of it), depth the thickness.

    Lies in the XY plane from z = 0 to z = depth, aligned on the origin. font: a .ttf/.otf path
    (Blender's built-in font otherwise). fix_overlaps unions overlapping glyphs (script fonts).
    """
    curve = bpy.data.curves.new("_vsblender_geo_text", "FONT")
    ob = None
    loaded = None
    collection = bpy.data.collections.new("_vsblender_geo_tmp")
    bpy.context.scene.collection.children.link(collection)
    try:
        curve.body = str(body)
        curve.size = float(size)
        curve.extrude = float(depth) / 2.0
        curve.space_character = float(spacing)
        curve.align_x = align_x.upper()
        curve.align_y = align_y.upper()
        if font:
            before = set(bpy.data.fonts)
            curve.font = bpy.data.fonts.load(font, check_existing=True)
            loaded = curve.font if curve.font not in before else None
        ob = bpy.data.objects.new("_vsblender_geo_text", curve)
        collection.objects.link(ob)
        deps = bpy.context.evaluated_depsgraph_get()
        me = bpy.data.meshes.new_from_object(ob.evaluated_get(deps), depsgraph=deps)
        bm = bmesh.new()
        try:
            bm.from_mesh(me)
        finally:
            bpy.data.meshes.remove(me)
    finally:
        if ob is not None:
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.data.curves.remove(curve)
        bpy.data.collections.remove(collection)
        if loaded is not None and loaded.users == 0:
            bpy.data.fonts.remove(loaded)
    bmesh.ops.translate(bm, vec=(0.0, 0.0, float(depth) / 2.0), verts=bm.verts)
    finish(bm)
    s = Solid(bm, _call("text", body, size, depth))
    if fix_overlaps:
        s = _self_union(s)
    return s


def _self_union(s: Solid) -> Solid:
    scene = bpy.context.scene
    collection = bpy.data.collections.new("_vsblender_geo_tmp")
    scene.collection.children.link(collection)
    me = bpy.data.meshes.new("_vsblender_geo_self")
    ob = None
    try:
        s.bm.to_mesh(me)
        ob = bpy.data.objects.new("_vsblender_geo_self", me)
        collection.objects.link(ob)
        mod = ob.modifiers.new("_vsblender_bool", "BOOLEAN")
        mod.operation = "UNION"
        mod.solver = "EXACT"
        mod.use_self = True
        mod.operand_type = "COLLECTION"
        mod.collection = bpy.data.collections.new("_vsblender_geo_empty")
        deps = bpy.context.evaluated_depsgraph_get()
        eval_me = bpy.data.meshes.new_from_object(ob.evaluated_get(deps), depsgraph=deps)
        bm = bmesh.new()
        bm.from_mesh(eval_me)
        bpy.data.meshes.remove(eval_me)
        empty = mod.collection
    finally:
        if ob is not None:
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.data.meshes.remove(me)
        bpy.data.collections.remove(collection)
    try:
        bpy.data.collections.remove(empty)
    except Exception:
        pass
    out = Solid(bm, s.desc + ".fix_overlaps()", s.log + ["self union: EXACT"])
    return out


# ----------------------------------------------------------------------------- mechanical parts
def _check_length(length: float, diameter: float, what: str) -> None:
    """m= sizes are millimetres while lengths are scene units: warn when they look mixed up."""
    if diameter > 0 and abs(length) > diameter * 200:
        _warn(f"{what} {length:g} is {abs(length) / diameter:,.0f} times the diameter. Lengths are scene units "
              f"({units_mod.label()}); use vsblender.mm(...) for millimetres.")


def _iso_d(m, d) -> float:
    """Nominal diameter in scene units: m (ISO size, millimetres) or d (scene units)."""
    if m is not None:
        return _mm(float(m))
    if d is None:
        raise ValueError("give m (ISO metric size, e.g. 3 for M3) or d (diameter in scene units)")
    return float(d)


def thread(m=None, d: float | None = None, pitch: float | None = None, length: float = 10.0, internal: bool = False,
           clearance: float | None = None, starts: int = 1, left: bool = False, chamfer: bool = True,
           quality: int = 16, segments: int | None = None, at=None, axis=None) -> Solid:
    """An ISO metric (60 degree) threaded rod from z = 0 to length, or with internal=True the shape to
    subtract for a threaded hole (slightly larger, overshooting both ends).

    m: ISO size (M8 -> m=8, pitch from the coarse table). d and pitch: scene units. clearance: total
    diametral play (default 0.2 mm in print projects, else 0). The profile is built by displacing a
    grid, so it is closed by construction. at and axis ("+Z" default) put its start at a point and run
    it along a direction.
    """
    major = _iso_d(m, d)
    if pitch is None:
        if m is None:
            raise ValueError("give pitch with d (or use m for the ISO coarse pitch)")
        pitch = _mm(_table(COARSE_PITCH, m, "coarse pitch"))
    pitch = float(pitch)
    printer = _printer()
    if clearance is None:
        clearance = _mm(0.2) if printer is not None else 0.0
    if printer is not None and pitch < 3 * _mm(float(printer.get("layer_height") or 0.2)):
        _warn(f"thread pitch {pitch:.3g} is under 3 layer heights: it will print poorly. Use a heat-set insert or a nut.")
    H = 0.8660254 * pitch
    R = major / 2.0 + (clearance / 2.0 if internal else -clearance / 2.0)
    r_root = R - 5.0 * H / 8.0
    lead = pitch * max(1, int(starts))
    over = pitch if internal else 0.0
    z0, z1 = -over, float(length) + over
    n = segments or max(48, segments_for(R))
    dz = pitch / max(4, int(quality))
    nz = int(math.ceil((z1 - z0) / dz)) + 1
    if n * nz > 2_000_000:
        raise ValueError(f"this thread would have {n * nz:,} vertices: length {length:g} is {float(length) / pitch:,.0f} pitches. "
                         f"Lengths are scene units ({units_mod.label()}); m= sizes are millimetres. "
                         f"For 20 mm in this scene pass length=vsblender.mm(20).")
    _check_length(float(length), major, "length")
    zs = np.linspace(z0, z1, nz)
    thetas = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
    direction = -1.0 if left else 1.0
    Z, T = np.meshgrid(zs, thetas, indexing="ij")
    u = ((Z - direction * lead * T / (2 * math.pi)) / pitch) % 1.0
    w = np.abs(u - 0.5)  # 0 at the crest centre... flipped below
    w = 0.5 - w          # distance from the crest centre in pitches, 0..0.5
    crest, root = 1.0 / 16.0, 3.0 / 8.0
    frac = np.clip((w - crest) / (root - crest), 0.0, 1.0)
    radius = R - frac * (R - r_root)
    if chamfer and not internal:
        radius = np.minimum(radius, r_root + (Z - z0))
        radius = np.minimum(radius, r_root + (z1 - Z))
        radius = np.maximum(radius, r_root * 0.98)
    X = radius * np.cos(T)
    Y = radius * np.sin(T)
    bm = bmesh.new()
    grid = [[bm.verts.new((float(X[k, j]), float(Y[k, j]), float(Z[k, j]))) for j in range(n)] for k in range(nz)]
    for k in range(nz - 1):
        for j in range(n):
            jn = (j + 1) % n
            bm.faces.new((grid[k][j], grid[k][jn], grid[k + 1][jn], grid[k + 1][j]))
    bottom = bm.verts.new((0.0, 0.0, z0))
    top = bm.verts.new((0.0, 0.0, z1))
    for j in range(n):
        jn = (j + 1) % n
        bm.faces.new((bottom, grid[0][jn], grid[0][j]))
        bm.faces.new((top, grid[-1][j], grid[-1][jn]))
    finish(bm, recalc_normals=False)
    size = f"M{_m_key(m)}" if m is not None else _fmt(major)
    return _placed(Solid(bm, _call("thread", size, pitch=pitch, length=length, internal=internal, left=left)), at, axis, "+Z")


def _hole_d(m, d, fit: str) -> float:
    printer = _printer()
    comp = _mm(float(printer.get("hole_compensation") or 0.0)) if printer is not None else 0.0
    if m is None:
        if d is None:
            raise ValueError("give m (ISO size) or d (diameter in scene units)")
        return float(d) + comp
    fit = (fit or "normal").lower()
    if fit in ("close", "normal", "loose"):
        value = _table(CLEARANCE, m, "clearance hole")[("close", "normal", "loose").index(fit)]
    elif fit == "tap":
        value = float(m) - _table(COARSE_PITCH, m, "coarse pitch")
    elif fit == "insert":
        value = _table(INSERT, m, "heat-set insert")[0]
        comp = 0.0  # insert holes are specified as printed sizes
    elif fit == "press":
        value = float(m) - 0.1
    elif fit == "exact":
        value = float(m)
    else:
        raise ValueError("fit must be close, normal, loose, tap, insert, press or exact")
    return _mm(value) + comp


def _axis_frame(axis, native: str) -> Matrix:
    """Rotation taking a cutter built along native ("-Z": a hole going down, "+Z": a rod going up) to
    axis, rolled so its local +Y points as near world +Z as it can (a teardrop's peak stays up)."""
    a = axis_vector(axis).normalized()
    z = -a if native == "-Z" else a
    up = Vector((0.0, 0.0, 1.0))
    y = up - z * up.dot(z)
    if y.length < 1e-6:
        y = Vector((0.0, 1.0, 0.0)) - z * z.y
    y.normalize()
    x = y.cross(z).normalized()
    return Matrix((x, y, z)).transposed().to_4x4()


def _placed(s: Solid, at, axis, native: str) -> Solid:
    """s moved so its opening is at `at` and it runs along `axis` (into the material for a hole)."""
    default = "-Z" if native == "-Z" else "+Z"
    if at is None and (axis is None or (isinstance(axis, str) and axis.strip().upper() == default)):
        return s
    m = _axis_frame(axis if axis is not None else default, native)
    if at is not None:
        m = Matrix.Translation(Vector(tuple(at)[:3])) @ m
    out = s.transform(m, s.desc)
    out.desc = s.desc + (f".at({_fmt(tuple(at))})" if at is not None else "") + (f".axis({axis!r})" if axis is not None else "")
    return out


def hole(m=None, d: float | None = None, depth: float = 10.0, fit: str = "normal", counterbore=None, countersink=None,
         teardrop: bool = False, segments: int | None = None, at=None, axis=None) -> Solid:
    """A cutter for a hole, to subtract: its open end at z = 0, going down depth (overshooting up a little).

    at=(x, y, z) puts the opening there, and axis ("-Z" default, "+Y", "-X" or a vector) is the
    direction the hole goes into the material: hole(m=3, depth=8, at=(0, 12, 5), axis="+Y") drills in
    from a face looking toward -Y. Round parts: geo.ring(...).radial_hole.
    m with fit: close/normal/loose (ISO 273 clearance), tap (tap drill), insert (heat-set insert),
    press, exact. counterbore=True (cap screw head) or (diameter, depth); countersink=True (90 degree
    flat head) or diameter. teardrop=True adds a 45 degree peak: on a horizontal axis it points up, so
    the hole prints without support. In print projects the printer's hole compensation is added.
    """
    dia = _hole_d(m, d, fit)
    depth = float(depth)
    _check_length(depth, dia, "depth")
    over = max(_mm(0.05), depth * 0.01)
    r = dia / 2.0
    cb = None
    if counterbore:
        if counterbore is True:
            if m is None:
                raise ValueError("counterbore=True needs m; otherwise pass (diameter, depth)")
            head_d, head_h = _table(CAP_HEAD, m, "cap screw head")
            cb = (_mm(head_d + 0.6) / 2.0, _mm(head_h + 0.2))
        else:
            cb = (float(counterbore[0]) / 2.0, float(counterbore[1]))
    sink = None
    if countersink:
        if countersink is True:
            if m is None:
                raise ValueError("countersink=True needs m; otherwise pass the head diameter")
            sink = _mm(_table(COUNTERSINK, m, "countersunk head") + 0.4) / 2.0
        else:
            sink = float(countersink) / 2.0
    # One revolved stepped profile: a single closed solid. Overlapping pieces (a cylinder joined to a
    # wider counterbore cylinder) are not a valid solid, and subtracting them leaves self-intersections.
    profile = [(0.0, -depth), (r, -depth)]
    if cb is not None:
        cb_r, cb_h = cb
        profile += [(r, -cb_h), (cb_r, -cb_h), (cb_r, over)]
    elif sink is not None and sink > r:
        rise = sink - r  # 90 degrees: the cone widens one unit per unit of depth
        profile += [(r, -rise), (sink + over, over)]
    else:
        profile += [(r, over)]
    profile += [(0.0, over)]
    widest = max(p[0] for p in profile)
    n = segments or segments_for(widest)
    s = _solid_from(lambda bm: lathe(bm, profile, segments=n), "hole")
    if teardrop:
        loop = geom2d.circle(dia, segments or segments_for(r))
        keep = [p for p in loop if not (p[1] > 0 and abs(p[0]) < r * math.sqrt(0.5) + 1e-12)]
        tip = (0.0, r * math.sqrt(2.0))
        pts = np.array(keep)
        ang = np.mod(np.arctan2(pts[:, 1], pts[:, 0]), 2 * math.pi)
        pts = pts[np.argsort(ang)]
        insert_at = int(np.searchsorted(np.sort(ang), math.pi / 2))
        pts = np.vstack([pts[:insert_at], [tip], pts[insert_at:]])
        peak = extrude([pts], depth + over, z0=-depth)
        s = s.union(peak) if (cb is not None or sink is not None) else peak
    size = f"M{_m_key(m)}" if m is not None else _fmt(dia)
    s.desc = _call("hole", size, depth=depth, fit=fit if m is not None else None, counterbore=bool(counterbore),
                   countersink=bool(countersink), teardrop=teardrop)
    return _placed(s, at, axis, "-Z")


def nut_trap(m, depth: float | None = None, clearance: float | None = None, at=None, axis=None) -> Solid:
    """A hexagonal pocket for an ISO 4032 nut, to subtract: open at z = 0, going down depth
    (default the nut height plus 0.2 mm). clearance: added across the flats (default 0.3 mm in print
    projects). at and axis place it as for hole()."""
    af, height = _table(NUT, m, "hex nut")
    if clearance is None:
        clearance = _mm(0.3) if _printer() is not None else 0.0
    af_bu = _mm(af) + float(clearance)
    depth = float(depth) if depth is not None else _mm(height + 0.2)
    over = max(_mm(0.05), depth * 0.01)
    across_corners = af_bu / math.cos(math.radians(30))
    loop = geom2d.regular(6, across_corners, rotation_deg=30.0)
    s = extrude([loop], depth + over, z0=-depth)
    s.desc = _call("nut_trap", f"M{_m_key(m)}", depth=depth)
    return _placed(s, at, axis, "-Z")


# ----------------------------------------------------------------------------- gears
def gear(teeth: int, module: float, thickness: float, bore: float | None = None, hub_d: float | None = None,
         hub_h: float = 0.0, shift: float = 0.0, backlash: float | None = None, phase: float = 0.0,
         clearance: float = 0.25, tip_relief: float = 0.0) -> Solid:
    """An involute spur gear standing on z = 0, centred on the Z axis.

    module and backlash are millimetres, like m= on threads (module 1.5: pitch diameter = 1.5 mm x
    teeth); thickness, bore, hub_d and hub_h are scene units. backlash defaults to 0.1 mm in print
    projects (0 otherwise), taken off each tooth. shift: profile shift (geom2d.gear_pair says when a
    small pinion needs it). phase: the clock angle of tooth 0 (degrees clockwise from +Y). hub_d/hub_h
    add a boss on top; bore cuts a hole through both.
    """
    if backlash is None:
        backlash = 0.1 if _printer() is not None else 0.0
    m_bu = _mm(module)
    loop = geom2d.spur(int(teeth), m_bu, shift=shift, backlash=_mm(backlash), clearance=clearance, phase=phase,
                       tip_relief=_mm(tip_relief))
    s = extrude([loop], float(thickness))
    if hub_d and hub_h:
        hub = cylinder(d=float(hub_d), h=float(hub_h) + float(thickness) * 0.5).move(0, 0, float(thickness) * 0.5)
        s = s.union(hub)
    if bore:
        total = float(thickness) + float(hub_h or 0.0)
        s = s.difference(cylinder(d=float(bore), h=total + 2 * _mm(1.0)).move(0, 0, -_mm(1.0)))
    s.desc = _call("gear", int(teeth), module=module, thickness=thickness, bore=bore, hub_d=hub_d, hub_h=hub_h,
                   shift=shift, phase=phase)
    s.meta.update({"gear": {"teeth": int(teeth), "module_mm": float(module), "shift": float(shift)}})
    return s


def rack(module: float, teeth: int, height: float, thickness: float, backlash: float | None = None,
         clearance: float = 0.25, extra: float = 0.0) -> Solid:
    """A straight rack: teeth along +Y facing +X, the pitch line at x = 0, from z = 0 up by thickness.
    module and backlash are millimetres; height (pitch line to the back, toward -X), thickness and
    extra (plain bar at each end) are scene units."""
    if backlash is None:
        backlash = 0.1 if _printer() is not None else 0.0
    loop = geom2d.rack(_mm(module), int(teeth), float(height), backlash=_mm(backlash), clearance=clearance,
                       extra=float(extra))
    s = extrude([loop], float(thickness))
    s.desc = _call("rack", module=module, teeth=int(teeth), height=height, thickness=thickness)
    return s

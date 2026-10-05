"""check_model: is a model fit for its purpose? Read-only.

purpose general, render, game or print decides which checks run and how serious each finding is:
an open mesh is fine for a render, a warning for a game asset, and an error for a 3D print.

Everything is computed from the evaluated meshes (modifiers applied) in world space with numpy, and
for print in millimetres against the printer profile. The highlight image is rendered from temporary
copies in the preview's throwaway scene: the user's objects and materials are never touched.
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

import bpy

from . import meshdata, units as units_mod

PURPOSES = ("general", "render", "game", "print")
# finding -> level per purpose (general, render, game, print). None: not checked for that purpose.
LEVELS = {
    "open_edges": ("info", "info", "warn", "error"),
    "non_manifold": ("warn", "info", "warn", "error"),
    "normals": ("warn", "error", "error", "error"),
    "degenerate": ("warn", "warn", "warn", "error"),
    "loose": ("info", "info", "warn", "warn"),
    "coincident_faces": ("warn", "error", "warn", "warn"),
    "self_intersection": (None, None, None, "error"),
    "overlapping_shells": (None, None, None, "warn"),
    "transforms": ("info", "info", "warn", None),
    "negative_scale": ("warn", "warn", "warn", "info"),
    "tris": ("info", "info", "info", "info"),
    "too_many_tris": (None, None, "warn", None),
    "ngons": (None, None, "info", None),
    "materials": ("warn", "warn", "warn", None),
    "uvs": (None, "warn", "warn", None),
    "shading": (None, "info", "info", None),
    "unit_suspect": ("warn", "warn", "warn", "warn"),
    "hidden_by_keys": ("info", "info", "info", "info"),
    "too_big": (None, None, None, "error"),
    "no_bed_contact": (None, None, None, "error"),
    "small_contact": (None, None, None, "warn"),
    "floating_shell": (None, None, None, "warn"),
    "off_bed": (None, None, None, "info"),
    "overhang": (None, None, None, "warn"),
    "thin_wall": (None, None, None, "warn"),
    "vanishing_wall": (None, None, None, "error"),
}
GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
# Highlight colours (linear RGB) by category, and the legend.
COLOURS = [("ok", (0.55, 0.56, 0.58)), ("error", (0.85, 0.05, 0.04)), ("warning", (0.95, 0.42, 0.02)),
           ("overhang", (0.95, 0.78, 0.05)), ("thin", (0.10, 0.35, 0.95)), ("bed", (0.10, 0.70, 0.20))]
CAT = {name: i for i, (name, _c) in enumerate(COLOURS)}


def default_purpose() -> str:
    template = units_mod.template()
    if template == "print_mm":
        return "print"
    if template == "game":
        return "game"
    return "general"


def default_targets(root: str) -> list:
    from . import marks

    out = []
    for ob in bpy.context.scene.objects:
        if ob.type not in GEOMETRY_TYPES or ob.name.startswith("_vsblender"):
            continue
        if marks.left_out(ob) or ob.hide_render or ob.hide_get():
            continue
        if ob.display_type in {"WIRE", "BOUNDS"} and ob.hide_render is False and any(
                m.type == "BOOLEAN" and m.object == ob for o in bpy.context.scene.objects for m in o.modifiers):
            continue  # a boolean cutter
        out.append(ob)
    return out


def resolve_targets(spec, root: str) -> list:
    if spec in (None, "", []):
        return default_targets(root)
    from . import inspect_tools

    obs = inspect_tools.resolve_objects(spec, root)
    return [o for o in obs if o.type in GEOMETRY_TYPES and not o.name.startswith("_vsblender")]


class _Report:
    def __init__(self, name: str, purpose: str):
        self.name = name
        self.purpose = purpose
        self.findings = []
        self.passed = []
        self.facts = {}
        self.ms = {}

    def add(self, code: str, message: str, count: int | None = None, at=None, level: str | None = None) -> None:
        if level is None:
            levels = LEVELS.get(code)
            level = levels[PURPOSES.index(self.purpose)] if levels else "warn"
        if level is None:
            return
        item = {"code": code, "level": level, "message": message}
        if count is not None:
            item["count"] = int(count)
        if at is not None and len(at):
            item["at"] = [[round(float(c), 5) for c in p] for p in np.asarray(at)[:10]]
        self.findings.append(item)

    def ok(self, text: str) -> None:
        self.passed.append(text)

    @property
    def status(self) -> str:
        levels = {f["level"] for f in self.findings}
        return "FAIL" if "error" in levels else "WARN" if "warn" in levels else "OK"


def _timed(report: _Report, key: str, started: float) -> None:
    report.ms[key] = int((time.time() - started) * 1000)


def _fmt(v: float, digits: int = 3) -> str:
    return meshdata.fmt_num(float(v), digits)


def _size_text(size, unit: str) -> str:
    return " x ".join(_fmt(v, 3) for v in size) + f" {unit}"


def keyed_visibility(ob) -> dict | None:
    """When an object's visibility is keyed: whether it is hidden now, and the frames it shows from.
    {'hidden_now': True, 'channels': ['hide_viewport'], 'visible_from': 90} or None."""
    from . import helpers

    curves = [fc for fc in helpers.fcurves(ob) if fc.data_path in ("hide_viewport", "hide_render")]
    if not curves:
        return None
    scene = bpy.context.scene
    now = scene.frame_current
    hidden_now = any(fc.evaluate(now) >= 0.5 for fc in curves)
    shows = []
    for fc in curves:
        for kp in fc.keyframe_points:
            if kp.co[1] < 0.5:
                shows.append(int(round(kp.co[0])))
    return {"hidden_now": hidden_now, "channels": sorted({fc.data_path for fc in curves}),
            "visible_from": min(shows) if shows else None, "frame": now}


def rest_matrix(ob):
    """World matrix from the object's own (keyed) transform and its parents', without the depsgraph."""
    matrix = ob.matrix_basis.copy()
    child = ob
    while child.parent is not None:
        matrix = child.parent.matrix_basis @ child.matrix_parent_inverse @ matrix
        child = child.parent
    return matrix


def rest_arrays(ob):
    """The object's own mesh (no modifiers) placed by its parent chain, for an object the depsgraph
    does not evaluate (hidden in the viewport): its evaluated copy has no geometry and a zero matrix."""
    if ob.type != "MESH" or ob.data is None:
        return None
    arrays = meshdata._mesh_arrays(ob.data)
    arrays.name = ob.name
    return arrays.transformed(rest_matrix(ob))


def check_model(params: dict, root: str) -> tuple:
    frame = params.get("frame")
    if frame is None:
        return _check_model(params, root)
    scene = bpy.context.scene
    saved = scene.frame_current
    scene.frame_set(int(frame))
    try:
        return _check_model(params, root)
    finally:
        scene.frame_set(saved)


def _check_model(params: dict, root: str) -> tuple:
    purpose = str(params.get("purpose") or default_purpose()).lower()
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}")
    targets = resolve_targets(params.get("targets"), root)
    if not targets:
        raise ValueError("no mesh, curve or text objects to check (references, hidden objects and cutters are left out by default)")
    deep = bool(params.get("deep"))
    from . import helpers

    printer = dict(helpers.DEFAULT_PRINTER)
    printer.update(params.get("printer") or {})
    u = units_mod.units()
    to_mm = float(params.get("mm_per_unit") or u["bu_to_mm"])
    max_tris = int(params.get("max_tris") or 50000)
    deps = bpy.context.evaluated_depsgraph_get()
    warnings = []
    reports = []
    highlights = []
    for ob in targets[:50]:
        report, highlight = _check_object(ob, deps, purpose, printer, to_mm, max_tris, deep, params, warnings)
        reports.append(report)
        if highlight is not None:
            highlights.append(highlight)
    if len(targets) > 50:
        warnings.append(f"checked the first 50 of {len(targets)} objects; pass targets to choose")
    result = {
        "purpose": purpose,
        "units": u["label"],
        "objects": [{"name": r.name, "status": r.status, "findings": r.findings, "passed": r.passed, **r.facts,
                     "ms": r.ms} for r in reports],
    }
    if purpose == "print":
        result["printer"] = {k: printer.get(k) for k in ("name", "preset", "build_volume", "nozzle", "layer_height",
                                                         "min_wall", "max_overhang_deg", "material", "configured")}
    result["text"] = _text(result, reports, purpose, printer, u)
    if params.get("image", True) and highlights and params.get("out"):
        try:
            image = _highlight_image(highlights, params, root, purpose)
            result.update(image)
        except Exception as exc:
            warnings.append(f"no highlight image: {exc}")
    return result, warnings


def _check_object(ob, deps, purpose: str, printer: dict, to_mm: float, max_tris: int, deep: bool, params: dict,
                  warnings: list):
    report = _Report(ob.name, purpose)
    started = time.time()
    keyed = keyed_visibility(ob)
    rest = False
    if ob.hide_viewport or (keyed and keyed["hidden_now"] and "hide_viewport" in keyed["channels"]):
        # Not evaluated while hidden: check its own mesh where its keys and parents put it.
        arrays = rest_arrays(ob)
        rest = arrays is not None
        if arrays is None:
            arrays = meshdata.object_arrays(ob, deps, warnings=warnings)
    else:
        arrays = meshdata.object_arrays(ob, deps, warnings=warnings)
    if keyed and keyed["hidden_now"]:
        shows = f" (visible from frame {keyed['visible_from']})" if keyed["visible_from"] is not None else ""
        report.add("hidden_by_keys", f"hidden at frame {keyed['frame']} by keys on {', '.join(keyed['channels'])}{shows}; "
                   f"checked its {'mesh without modifiers' if rest else 'mesh'} instead (frame= checks another frame)", level="info")
    elif rest:
        report.add("hidden_by_keys", "hidden in the viewport, so it is not evaluated: checked its mesh without modifiers", level="info")
    _timed(report, "mesh", started)
    t = len(arrays.tris)
    if not t:
        report.add("degenerate", "no faces after modifiers", level="warn" if purpose != "print" else "error")
        return report, None
    lo, hi = arrays.bounds()
    size = hi - lo
    diag = float(np.linalg.norm(size)) or 1.0
    report.facts["tris"] = t
    report.facts["size"] = [round(float(v), 6) for v in size]
    if purpose == "print":
        report.facts["size_mm"] = [round(float(v) * to_mm, 3) for v in size]
    started = time.time()
    welded, _remap = meshdata.weld(arrays, diag * 1e-7)
    stats = meshdata.edge_stats(welded)
    labels = meshdata.components(welded)
    shells = int(labels.max()) + 1 if len(labels) else 0
    va = meshdata.volume_area(welded, labels)
    _timed(report, "topology", started)
    report.facts["shells"] = shells
    category = np.zeros(t, dtype=np.int64)
    flags = stats["flags"]
    huge = t > 2_000_000

    # -- topology
    if stats["open_edges"]:
        report.add("open_edges", f"{stats['open_edges']} open edges around {stats.get('holes', '?')} hole(s): the surface is not closed",
                   stats["open_edges"], stats["at"]["open_edges"])
        category[flags["tri_open"]] = CAT["error"] if _level("open_edges", purpose) == "error" else CAT["warning"]
    else:
        report.ok("closed (no open edges)")
    if stats["non_manifold_edges"]:
        report.add("non_manifold", f"{stats['non_manifold_edges']} edges shared by 3 or more faces",
                   stats["non_manifold_edges"], stats["at"]["non_manifold_edges"])
        category[flags["tri_nonmanifold"]] = CAT["error"] if _level("non_manifold", purpose) == "error" else CAT["warning"]
    else:
        report.ok("manifold edges")
    inverted = []
    if "shell_volume" in va:
        inverted = [i for i, v in enumerate(va["shell_volume"]) if v < 0 and _shell_closed(stats, labels, i)]
    if stats["flipped_edges"] or inverted:
        parts = []
        if stats["flipped_edges"]:
            parts.append(f"{stats['flipped_edges']} edges between faces facing opposite ways")
        if inverted:
            parts.append(f"{len(inverted)} shell(s) inside out (normals point in)")
        report.add("normals", "; ".join(parts) + ". Recalculate normals (bmesh.ops.recalc_face_normals)",
                   stats["flipped_edges"] + len(inverted), stats["at"]["flipped_edges"])
        category[flags["tri_flipped"]] = CAT["error"]
        for i in inverted:
            category[labels == i] = CAT["error"]
    else:
        report.ok("normals consistent and pointing out")
    _normals, areas = meshdata.tri_normals_areas(welded)
    degenerate = (areas < (diag * 1e-7) ** 2) | (welded.tris[:, 0] == welded.tris[:, 1]) | \
                 (welded.tris[:, 1] == welded.tris[:, 2]) | (welded.tris[:, 0] == welded.tris[:, 2])
    if degenerate.any():
        report.add("degenerate", f"{int(degenerate.sum())} zero-area triangles", int(degenerate.sum()),
                   welded.verts[welded.tris[degenerate][:, 0]])
        category[degenerate] = CAT["warning"]
    loose = len(welded.verts) - len(np.unique(welded.tris))
    if loose > 0:
        report.add("loose", f"{loose} vertices not used by any face", loose)
    if not huge:
        key = np.sort(welded.tris, axis=1)
        _u, inv, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
        dup = counts[inv.reshape(-1)] > 1
        dup &= ~degenerate
        if dup.any():
            report.add("coincident_faces", f"{int(dup.sum())} triangles lie exactly on top of others (z-fighting, double walls)",
                       int(dup.sum()), welded.verts[welded.tris[dup][:, 0]])
            category[dup] = CAT["warning"]

    # -- self intersection (print)
    if _level("self_intersection", purpose) and not huge:
        cap = 2_000_000 if deep else 300_000
        if t <= cap:
            started = time.time()
            same, other, at = _self_intersections(welded, labels)
            _timed(report, "self_intersection", started)
            if same:
                report.add("self_intersection", f"{same} pairs of faces cut through each other within one shell", same, at)
                category[_tris_in(at, welded)] = CAT["error"]
            else:
                report.ok("no self-intersections")
            if other:
                report.add("overlapping_shells", f"{other} face pairs where separate shells overlap; the slicer merges them "
                           "(fine for printing, but booleans would make one clean solid)", other)
        else:
            report.add("self_intersection", f"skipped above {cap:,} triangles (deep=true raises the limit)", level="info")

    # -- object level
    m = rest_matrix(ob) if rest else ob.matrix_world
    scale = m.to_scale()
    if m.to_3x3().determinant() < 0:
        report.add("negative_scale", "negative scale (mirrored): exporters that ignore it flip the normals; apply the scale")
    if purpose != "print" and (any(abs(s - 1.0) > 1e-4 for s in scale) or any(abs(a) > 1e-4 for a in ob.rotation_euler)):
        report.add("transforms", f"rotation or scale not applied (scale {_fmt(scale.x)}, {_fmt(scale.y)}, {_fmt(scale.z)})")
    report.add("tris", f"{t:,} triangles, {shells} shell(s)", t)
    if purpose == "game" and t > max_tris:
        report.add("too_many_tris", f"{t:,} triangles is over the budget of {max_tris:,} (max_tris)", t)
    if ob.type == "MESH":
        _mesh_checks(ob, report, purpose)
    unit_ok = _size_sanity(report, size, purpose, to_mm, printer)

    # -- print
    if purpose == "print" and not huge:
        _print_checks(report, welded, labels, va, category, printer, to_mm, deep, params, unit_ok, ob)
    elif purpose == "print":
        report.add("tris", "over 2,000,000 triangles: only size and volume were checked", level="info")
        report.facts["volume_mm3"] = round(abs(va["volume"]) * to_mm ** 3, 2)
    else:
        report.facts["volume"] = round(va["volume"], 6) if not stats["open_edges"] else None
    highlight = None if huge or t > 1_000_000 else (welded, category)
    return report, highlight


def _level(code: str, purpose: str):
    return LEVELS[code][PURPOSES.index(purpose)]


def _shell_closed(stats: dict, labels, shell: int) -> bool:
    flags = stats["flags"]
    mask = labels == shell
    return not (flags["tri_open"][mask].any() or flags["tri_nonmanifold"][mask].any())


def _tris_in(points, arrays) -> np.ndarray:
    mask = np.zeros(len(arrays.tris), dtype=bool)
    if points is None or not len(points):
        return mask
    centroids = arrays.verts[arrays.tris].mean(axis=1)
    for p in np.asarray(points)[:2000]:
        mask[int(np.argmin(np.linalg.norm(centroids - p, axis=1)))] = True
    return mask


def _self_intersections(arrays, labels) -> tuple:
    from mathutils.bvhtree import BVHTree

    tree = BVHTree.FromPolygons(arrays.verts.tolist(), arrays.tris.tolist(), all_triangles=True, epsilon=0.0)
    pairs = tree.overlap(tree)
    if not pairs:
        return 0, 0, np.zeros((0, 3))
    p = np.array(pairs, dtype=np.int64)
    p = p[p[:, 0] < p[:, 1]]
    ta, tb = arrays.tris[p[:, 0]], arrays.tris[p[:, 1]]
    shares = np.zeros(len(p), dtype=bool)
    for i in range(3):
        for j in range(3):
            shares |= ta[:, i] == tb[:, j]
    p = p[~shares]
    if not len(p):
        return 0, 0, np.zeros((0, 3))
    same = labels[p[:, 0]] == labels[p[:, 1]]
    at = arrays.verts[arrays.tris[p[same][:, 0]]].mean(axis=1)
    return int(same.sum()), int((~same).sum()), at


def _mesh_checks(ob, report: _Report, purpose: str) -> None:
    me = ob.data
    slots = ob.material_slots
    if _level("materials", purpose):
        if not len(slots):
            report.add("materials", "no material (renders and exports with the default grey)")
        elif any(s.material is None for s in slots):
            report.add("materials", f"{sum(1 for s in slots if s.material is None)} empty material slot(s)")
        else:
            report.ok(f"{len(slots)} material(s)")
            if len(slots) > 1 and len(me.polygons):
                indices = np.empty(len(me.polygons), dtype=np.int32)
                me.polygons.foreach_get("material_index", indices)
                if int(indices.max()) == 0:
                    report.add("materials", f"{len(slots)} slots, only slot 0 used")
    if len(me.polygons):
        sizes = np.empty(len(me.polygons), dtype=np.int32)
        me.polygons.foreach_get("loop_total", sizes)
        areas = np.empty(len(me.polygons), dtype=np.float64)
        me.polygons.foreach_get("area", areas)
        tris = int(np.maximum(sizes - 2, 0).sum())
        area = float(areas.sum())
        if area > 1e-12 and tris / area > 100_000:
            report.add("density", f"{tris / area:,.0f} tris/m²", level="info")
    if _level("uvs", purpose):
        textured = any(s.material is not None and getattr(s.material, "node_tree", None) is not None and
                       any(n.bl_idname == "ShaderNodeTexImage" for n in s.material.node_tree.nodes) for s in slots)
        if not len(me.uv_layers) and (textured or purpose == "game"):
            report.add("uvs", "no UV map" + (" but an image texture is used" if textured else "") +
                       ": add one (geo to_object(uv='box'), or unwrap)")
        elif len(me.uv_layers):
            report.ok("UV map present")
    if _level("ngons", purpose):
        sizes = np.empty(len(me.polygons), dtype=np.int32)
        me.polygons.foreach_get("loop_total", sizes)
        ngons = int((sizes > 4).sum())
        if ngons:
            report.add("ngons", f"{ngons} n-gons (more than 4 sides); engines triangulate them on import", ngons)
    if _level("shading", purpose) and len(me.polygons) > 64:
        smooth = np.empty(len(me.polygons), dtype=bool)
        me.polygons.foreach_get("use_smooth", smooth)
        if not smooth.any():
            report.add("shading", "flat shaded everywhere; curved surfaces look faceted (geo.smooth_by_angle(mesh, 30))")


def _size_sanity(report: _Report, size, purpose: str, to_mm: float, printer: dict) -> bool:
    largest_mm = float(max(size)) * to_mm
    if purpose == "print":
        build = max(printer.get("build_volume") or [220, 220, 250])
        if largest_mm > 5 * build or largest_mm < 1.0:
            hint = "mm_per_unit=1 if this file was modelled with 1 unit = 1 mm at unit scale 1.0" if largest_mm > 5 * build else \
                "the part is under 1 mm: check the scene's unit scale"
            report.add("unit_suspect", f"the largest side is {_fmt(largest_mm)} mm: {hint}")
            return False
        return True
    if largest_mm < 0.5 or largest_mm > 2_000_000:
        report.add("unit_suspect", f"the largest side is {_fmt(largest_mm / 1000.0)} m: check the scene's unit scale")
        return False
    return True


def _print_checks(report: _Report, welded, labels, va: dict, category, printer: dict, to_mm: float, deep: bool,
                  params: dict, unit_ok: bool, ob) -> None:
    mm = welded.scaled(to_mm)
    normals, areas = meshdata.tri_normals_areas(mm)
    layer = float(printer.get("layer_height") or 0.2)
    zmin = float(mm.verts[:, 2].min())
    tri_z = mm.verts[mm.tris][:, :, 2]
    lo, hi = mm.bounds()
    size = hi - lo
    volume = abs(va["volume"]) * to_mm ** 3
    area = va["area"] * to_mm ** 2
    report.facts["volume_mm3"] = round(volume, 2)
    report.facts["area_mm2"] = round(area, 2)
    # Fits the printer (as placed on the bed, optionally turned 90 degrees about Z).
    bx, by, bz = (float(v) for v in (printer.get("build_volume") or [220, 220, 250]))
    fits = size[0] <= bx and size[1] <= by and size[2] <= bz
    turned = size[1] <= bx and size[0] <= by and size[2] <= bz
    if fits or turned:
        report.ok(f"fits {printer.get('name', 'the printer')} ({_fmt(bx)} x {_fmt(by)} x {_fmt(bz)} mm)"
                  + ("" if fits else " when turned 90 degrees about Z"))
    elif unit_ok:
        report.add("too_big", f"{_size_text(size, 'mm')} does not fit the {_fmt(bx)} x {_fmt(by)} x {_fmt(bz)} mm build volume; "
                   "split it, scale it, or lay it flat")
    if abs(zmin) > layer / 2 and params.get("place_on_bed", True) is False:
        report.add("off_bed", f"the lowest point is at z {_fmt(zmin)} mm, not on the bed (export_model places it on the bed)")
    # Bed contact: downward faces at the bottom.
    bed = (normals[:, 2] < -0.98) & (tri_z.max(axis=1) <= zmin + layer / 2)
    contact = float(areas[bed].sum())
    report.facts["bed_contact_mm2"] = round(contact, 2)
    category[bed & (category == 0)] = CAT["bed"]
    if contact <= 1e-6:
        report.add("no_bed_contact", "touches the bed only at a point or an edge: it will not stick. Lay a flat face down "
                   "(vsblender.orient_flat), or add a flat base")
    elif contact < 50.0:
        report.add("small_contact", f"only {_fmt(contact, 1)} mm² touches the bed: it may come loose (add a brim or a wider base)")
    else:
        report.ok(f"{_fmt(contact, 0)} mm² on the bed")
    # Floating shells: start above the bed and touch nothing below.
    shells = int(labels.max()) + 1 if len(labels) else 0
    if shells > 1:
        boxes = []
        for i in range(shells):
            idx = np.unique(mm.tris[labels == i])
            boxes.append((mm.verts[idx].min(axis=0), mm.verts[idx].max(axis=0)))
        floating = []
        for i, (blo, bhi) in enumerate(boxes):
            if blo[2] <= zmin + layer / 2:
                continue
            supported = any(j != i and np.all(boxes[j][0] <= bhi + layer) and np.all(boxes[j][1] >= blo - layer) for j in range(shells))
            if not supported:
                floating.append((i, blo))
        if floating:
            report.add("floating_shell", f"{len(floating)} separate part(s) start in mid-air with nothing under them",
                       len(floating), [np.array([b[0], b[1], b[2]]) / to_mm for _i, b in floating])
    # Overhangs.
    max_deg = float(printer.get("max_overhang_deg") or 45.0)
    limit = -math.sin(math.radians(max_deg))
    over = (normals[:, 2] < limit) & ~bed
    ceiling = over & (normals[:, 2] < -0.98)
    over_area = float(areas[over].sum())
    report.facts["overhang_mm2"] = round(over_area, 2)
    if over_area > 1.0:
        worst = int(np.argmin(np.where(over, normals[:, 2], 1.0)))
        worst_deg = math.degrees(math.asin(min(1.0, -float(normals[worst, 2]))))
        z_worst = float(tri_z[worst].mean())
        text = f"{_fmt(over_area, 1)} mm² overhangs more than {_fmt(max_deg)}° (worst {_fmt(worst_deg, 0)}° at z {_fmt(z_worst, 1)} mm)"
        if ceiling.any():
            text += f"; {_fmt(float(areas[ceiling].sum()), 1)} mm² of it is flat ceiling (bridges or support)"
        report.add("overhang", text + ". Needs support, a chamfer instead of a fillet underneath, or another orientation",
                   int(over.sum()), mm.verts[mm.tris[over]].mean(axis=1)[:10] / to_mm)
        category[over & (category == 0)] = CAT["overhang"]
    else:
        report.ok(f"no overhangs over {_fmt(max_deg)}°")
    # Thin walls.
    started = time.time()
    min_wall = float(printer.get("min_wall") or 0.8)
    nozzle = float(printer.get("nozzle") or 0.4)
    thin = _wall_thickness(mm, normals, areas, 20000 if deep else 3000)
    _timed(report, "walls", started)
    if thin is not None:
        thickness, weights, tri_idx, points = thin
        below = thickness < min_wall
        vanish = thickness < nozzle * 1.1
        if vanish.any():
            share = float(weights[vanish].sum() / weights.sum() * 100.0)
            sliver = share < 1.0
            report.add("vanishing_wall", (f"small slivers ({'under 0.1' if share < 0.1 else 'about ' + _fmt(share, 1)}% of the surface) are thinner than {_fmt(nozzle * 1.1)} mm "
                       f"(thinnest {_fmt(float(thickness.min()), 2)} mm): the slicer drops them, which is usually harmless (thread ends, sharp edges)")
                       if sliver else (f"about {_fmt(share, 1)}% of the surface is thinner than {_fmt(nozzle * 1.1)} mm "
                       f"(thinnest {_fmt(float(thickness.min()), 2)} mm): the slicer will drop it"), int(vanish.sum()),
                       points[vanish][:10] / to_mm, level="warn" if sliver else None)
            category[np.unique(tri_idx[vanish])] = CAT["thin"]
        elif below.any():
            share = float(weights[below].sum() / weights.sum() * 100.0)
            report.add("thin_wall", f"about {_fmt(share, 1)}% of the surface is thinner than {_fmt(min_wall)} mm "
                       f"(thinnest {_fmt(float(thickness[below].min()), 2)} mm)", int(below.sum()), points[below][:10] / to_mm)
            category[np.unique(tri_idx[below])] = CAT["thin"]
        else:
            report.ok(f"walls at least {_fmt(min_wall)} mm (thinnest sampled {_fmt(float(thickness.min()), 2)} mm)")
        report.facts["thinnest_wall_mm"] = round(float(thickness.min()), 3)
    # Filament estimate.
    line_w = nozzle * 1.125
    shell = min(volume, area * 2 * line_w)
    infill = max(0.0, volume - shell) * 0.15
    used = shell + infill
    density = float(printer.get("density") or 1.24)
    fil_d = float(printer.get("filament_diameter") or 1.75)
    grams = used * density / 1000.0
    metres = used / (math.pi * (fil_d / 2) ** 2) / 1000.0
    report.facts["filament"] = {"grams": round(grams, 1), "metres": round(metres, 2), "solid_grams": round(volume * density / 1000.0, 1),
                                "note": "rough: 2 perimeters and 15% infill; the slicer's estimate is the one to trust"}
    if params.get("suggest_orientation"):
        report.facts["orientations"] = _suggest_orientations(mm, max_deg, layer)


def _wall_thickness(mm, normals, areas, samples: int):
    """Sample the surface and cast rays inward: the distance to the opposite wall is the thickness."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    total = float(areas.sum())
    if total <= 0:
        return None
    rng = np.random.default_rng(0)
    count = int(min(samples, max(200, len(mm.tris) * 2)))
    tri_idx = rng.choice(len(mm.tris), size=count, p=areas / total)
    r1, r2 = rng.random(count), rng.random(count)
    s = np.sqrt(r1)
    a, b, c = (mm.verts[mm.tris[tri_idx, k]] for k in range(3))
    points = (1 - s)[:, None] * a + (s * (1 - r2))[:, None] * b + (s * r2)[:, None] * c
    tree = BVHTree.FromPolygons(mm.verts.tolist(), mm.tris.tolist(), all_triangles=True)
    lo, hi = mm.bounds()
    reach = float(np.linalg.norm(hi - lo)) + 1.0
    eps = max(reach * 1e-6, 1e-5)
    thickness = np.full(count, np.inf)
    for i in range(count):
        n = Vector(normals[tri_idx[i]])
        if n.length < 0.5:
            continue
        origin = Vector(points[i]) - n * eps
        direction = -n
        hit, hnormal, _index, dist = tree.ray_cast(origin, direction, reach)
        if hit is not None and hnormal.dot(direction) > 0:
            thickness[i] = dist + eps
    finite = np.isfinite(thickness)
    if not finite.any():
        return None
    return thickness[finite], areas[tri_idx[finite]], tri_idx[finite], points[finite]


def _suggest_orientations(mm, max_deg: float, layer: float) -> list:
    from mathutils import Vector

    from . import placement

    candidates = [Vector(v) for v in ((0, 0, -1), (0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0))]
    candidates += [n for n, _a in placement._hull_normals(mm.verts, limit=8)]
    rows = []
    seen = set()
    for n in candidates:
        key = tuple(np.round(np.array(n), 2))
        if key in seen:
            continue
        seen.add(key)
        q = n.rotation_difference(Vector((0, 0, -1)))
        rot = np.array(q.to_matrix())
        over, contact = placement._overhang_area(mm, rot, max_deg, layer)
        v = mm.verts @ rot.T
        height = float(v[:, 2].max() - v[:, 2].min())
        euler = q.to_euler("XYZ")
        rows.append({"rotation_deg": [round(math.degrees(a), 2) for a in euler], "overhang_mm2": round(over, 1),
                     "bed_contact_mm2": round(contact, 1), "height_mm": round(height, 2)})
    rows.sort(key=lambda r: (r["overhang_mm2"], -r["bed_contact_mm2"], r["height_mm"]))
    return rows[:3]


def _text(result: dict, reports: list, purpose: str, printer: dict, u: dict) -> str:
    lines = [f"purpose: {purpose}    units: {u['label']}"]
    if purpose == "print":
        bv = printer.get("build_volume") or []
        lines.append(f"printer: {printer.get('name')} ({' x '.join(_fmt(v) for v in bv)} mm, {printer.get('nozzle')} mm nozzle, "
                     f"{printer.get('material')})" + ("" if printer.get("configured") else " - default profile; set \"printer\" in .blender-ai/config.json"))
    for r in reports:
        size = r.facts.get("size_mm") if purpose == "print" else r.facts.get("size")
        unit = "mm" if purpose == "print" else u["symbol"]
        head = f"{r.status}  {r.name}"
        if size:
            head += f"  ({_size_text(size, unit)}, {r.facts.get('tris', 0):,} tris, {r.facts.get('shells', 0)} shell(s))"
        lines.append(head)
        order = {"error": 0, "warn": 1, "info": 2}
        for f in sorted(r.findings, key=lambda f: order[f["level"]]):
            if f["code"] == "tris":
                continue
            at = f" at {', '.join('(' + ', '.join(_fmt(c, 3) for c in p) + ')' for p in f.get('at', [])[:2])}" if f.get("at") else ""
            lines.append(f"  {f['level']:5s} {f['code']}: {f['message']}{at}")
        if r.passed:
            lines.append(f"  ok    {'; '.join(r.passed)}")
        if r.facts.get("filament"):
            fil = r.facts["filament"]
            lines.append(f"  filament: about {fil['grams']} g ({fil['metres']} m) of {printer.get('material')}; {_fmt(r.facts['volume_mm3'], 0)} mm³ solid. {fil['note']}")
        for o in r.facts.get("orientations", []):
            lines.append(f"  orientation {o['rotation_deg']}: overhang {o['overhang_mm2']} mm², bed {o['bed_contact_mm2']} mm², height {o['height_mm']} mm")
    return "\n".join(lines)


def _highlight_image(highlights: list, params: dict, root: str, purpose: str) -> dict:
    """Render the checked meshes coloured by finding, from temporary objects only."""
    from . import preview, sheet

    temps = []
    mats = []
    objects = []
    try:
        for name, colour in COLOURS:
            mat = bpy.data.materials.new(f"_vsblender_check_{name}")
            mat.diffuse_color = (*colour, 1.0)
            mats.append(mat)
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for index, (arrays, category) in enumerate(highlights):
            arr = meshdata.MeshArrays(arrays.verts, arrays.tris, material=category)
            me = meshdata.to_mesh(arr, f"_vsblender_check_{index}")
            temps.append(me)
            for mat in mats:
                me.materials.append(mat)
            ob = bpy.data.objects.new(f"_vsblender_check_{index}", me)
            temps.append(ob)
            objects.append(ob)
            lo, hi = arrays.bounds()
            low, high = np.minimum(low, lo), np.maximum(high, hi)
        views = params.get("views") or (["iso", "bottom"] if purpose == "print" else ["iso"])
        if isinstance(views, str):
            views = [views]
        out = params["out"]
        base = os.path.splitext(out)[0]
        parts, labels = [], []
        used = {CAT["ok"]}
        for _arr, category in highlights:
            used |= set(np.unique(category).tolist())
        legend = " ".join(f"{_LEGEND[name]}" for name, _c in COLOURS if CAT[name] in used and name != "ok")
        try:
            for i, view in enumerate(views[:4]):
                part = f"{base}.check{i}.png"
                render_params = {"view": view, "shading": "solid", "size": int(params.get("size") or 512),
                                 "bounds": [list(low), list(high)], "outline": False, "cavity": False}
                preview.render_view(render_params, part, objects=objects, root=root)
                parts.append(part)
                labels.append(f"{view}  {legend}".strip())
            composed = sheet.compose_files(parts, out, labels=labels, cell=min(int(params.get("size") or 512), 768),
                                           max_bytes=int(params.get("max_bytes") or 0) or None)
        finally:
            for part in parts:
                try:
                    os.remove(part)
                except OSError:
                    pass
        return {"file": composed["file"], "image_views": views[:4], "legend": legend}
    finally:
        for idb in reversed(temps):
            try:
                if isinstance(idb, bpy.types.Object):
                    bpy.data.objects.remove(idb, do_unlink=True)
                else:
                    bpy.data.meshes.remove(idb)
            except (ReferenceError, RuntimeError):
                pass
        for mat in mats:
            try:
                bpy.data.materials.remove(mat)
            except (ReferenceError, RuntimeError):
                pass


_LEGEND = {"error": "RED=ERROR", "warning": "ORANGE=WARNING", "overhang": "YELLOW=OVERHANG", "thin": "BLUE=THIN",
           "bed": "GREEN=BED", "ok": "GREY=OK"}

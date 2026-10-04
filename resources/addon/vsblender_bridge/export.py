"""export_model: write models out for a slicer, a game engine, a web viewer or another DCC.

Two engines:
- Mesh formats for printing (STL, 3MF, and OBJ geometry for print) are written here in pure Python
  from the evaluated meshes, in millimetres, without operators: the user's selection and scene are
  untouched. For printing, parts are placed on the bed and centred (3MF on the bed centre).
- glTF/GLB, FBX, USD/USDZ, OBJ with materials and PLY use Blender's exporters, which need
  selection. They run in a background Blender on a copy of the session (job.py kind "export"), so the
  user's selection and settings never change. glTF and USD are metres by definition: a millimetre
  scene is scaled in the copy.

Existing files are never overwritten unless overwrite=true. Each export is journaled.
"""
from __future__ import annotations

import hashlib
import os
import re

import numpy as np

import bpy

from . import history, meshdata, units as units_mod

LIVE_FORMATS = {"stl", "3mf"}
JOB_FORMATS = {"glb", "gltf", "fbx", "obj", "usd", "usda", "usdc", "usdz", "ply"}
FORMATS = LIVE_FORMATS | JOB_FORMATS
DEFAULT_FORMAT = {"print": "3mf", "game": "glb", "render": "glb", "general": "glb"}


def _under(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def safe_name(name: str) -> str:
    out = re.sub(r"[^\w\-. ]+", "_", str(name)).strip(" .") or "model"
    return out[:120]


def plan(params: dict, root: str) -> dict:
    """Resolve targets, format and output paths. Raises when an output exists and overwrite is false."""
    from . import checks

    purpose = str(params.get("purpose") or checks.default_purpose()).lower()
    if purpose not in checks.PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(checks.PURPOSES)}")
    fmt = str(params.get("format") or DEFAULT_FORMAT[purpose]).lower().lstrip(".")
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(sorted(FORMATS))}")
    explicit = params.get("targets") not in (None, "", [])
    targets = checks.resolve_targets(params.get("targets"), root)
    if not targets:
        raise ValueError("nothing to export: no visible mesh, curve or text objects (references are left out)")
    if not explicit and len(targets) > 10:
        raise ValueError(f"{len(targets)} objects would be exported; pass targets (names or a find selector) to choose")
    split = str(params.get("split") or ("objects" if fmt in ("stl", "ply") else "one")).lower()
    if split not in ("one", "objects"):
        raise ValueError("split must be one or objects")
    if fmt == "stl" and split == "one" and len(targets) > 1 and purpose == "print":
        pass  # one STL with all parts is allowed; slicers split it by shell
    blend = bpy.data.filepath
    base_dir = os.path.join(os.path.dirname(blend), "exports") if blend else os.path.join(root, "exports")
    raw = params.get("path")
    ext = {"usd": "usdc"}.get(fmt, fmt)
    stem = safe_name(targets[0].name if len(targets) == 1 else (os.path.splitext(os.path.basename(blend))[0] if blend else "model"))
    if raw:
        full = raw if os.path.isabs(raw) else os.path.join(root, raw)
        full = os.path.abspath(full)
        if os.path.splitext(full)[1]:
            folder, single = os.path.dirname(full), full
            if split == "objects" and len(targets) > 1:
                raise ValueError("split=objects writes one file per object: pass a folder as path")
            if os.path.splitext(full)[1].lower().lstrip(".") not in (ext, fmt) and not (fmt == "usd" and full.lower().endswith((".usd", ".usda", ".usdc"))):
                raise ValueError(f"path ends in {os.path.splitext(full)[1]}, but format is {fmt}")
        else:
            folder, single = full, None
    else:
        folder, single = base_dir, None
    if not _under(folder, root):
        raise ValueError("exports must be written inside the workspace")
    if split == "objects":
        outputs = [(ob.name, os.path.join(folder, f"{safe_name(ob.name)}.{ext}"), [ob.name]) for ob in targets]
    else:
        outputs = [("all", single or os.path.join(folder, f"{stem}.{ext}"), [ob.name for ob in targets])]
    exists = [p for _n, p, _t in outputs if os.path.exists(p)]
    if exists and not params.get("overwrite"):
        rel = ", ".join(history.rel(root, p) for p in exists)
        raise FileExistsError(f"refusing to overwrite {rel}. Pass overwrite=true, or another path.")
    place = params.get("place_on_bed")
    place = (purpose == "print") if place is None else bool(place)
    center = params.get("center")
    center = (purpose == "print") if center is None else bool(center)
    u = units_mod.units()
    return {
        "purpose": purpose, "format": fmt, "split": split, "outputs": outputs, "place_on_bed": place, "center": center,
        "mm_per_unit": float(params.get("mm_per_unit") or u["bu_to_mm"]), "bu_to_m": u["bu_to_m"],
        "apply_modifiers": params.get("apply_modifiers", True) is not False, "assembly": bool(params.get("assembly")),
        "ascii": bool(params.get("ascii")), "live": fmt in LIVE_FORMATS, "units": u["label"],
    }


def _parts(names: list, deps, mm_per_unit: float, warnings: list) -> list:
    parts = []
    for name in names:
        ob = bpy.data.objects[name]
        arrays = meshdata.object_arrays(ob, deps, warnings=warnings)
        if not len(arrays.tris):
            warnings.append(f"{name} has no faces; left out")
            continue
        mm = arrays.scaled(mm_per_unit)
        lo, hi = mm.bounds()
        welded, _remap = meshdata.weld(mm, max(float(np.linalg.norm(hi - lo)) * 1e-7, 1e-9))
        t = welded.tris
        ok = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
        welded = meshdata.MeshArrays(welded.verts, t[ok], material=None, name=name)
        parts.append((name, welded))
    return parts


def _place(parts: list, place: bool, center, bed=None) -> list:
    """Shift a group of parts so it stands on z = 0, centred on (0, 0) or on the bed centre."""
    if not parts:
        return parts
    lows = np.min([p.bounds()[0] for _n, p in parts], axis=0)
    highs = np.max([p.bounds()[1] for _n, p in parts], axis=0)
    shift = np.zeros(3)
    if place:
        shift[2] = -lows[2]
    if center:
        mid = (lows + highs) / 2
        target = np.array([bed[0] / 2, bed[1] / 2]) if bed is not None else np.zeros(2)
        shift[:2] = target - mid[:2]
    return [(n, p.moved(shift)) for n, p in parts]


def run_live(spec: dict, root: str, params: dict) -> tuple:
    """STL / 3MF from the evaluated meshes. Returns (result, warnings)."""
    from . import helpers

    warnings = []
    printer = dict(helpers.DEFAULT_PRINTER)
    printer.update(params.get("printer") or {})
    bed = printer.get("build_volume") or [220, 220, 250]
    deps = bpy.context.evaluated_depsgraph_get()
    files = []
    for label, path, names in spec["outputs"]:
        parts = _parts(names, deps, spec["mm_per_unit"], warnings)
        if not parts:
            continue
        on_bed = spec["format"] == "3mf" and spec["center"]
        parts = _place(parts, spec["place_on_bed"], spec["center"], bed if on_bed else None)
        for name, arrays in parts:
            st = meshdata.mesh_stats(arrays, 1e-9)
            if not st["watertight"]:
                warnings.append(f"{name}: {st['open_edges']} open, {st['non_manifold_edges']} non-manifold, "
                                f"{st['flipped_edges']} flipped edges. Slicers may repair or misprint it; run check_model purpose=print.")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if spec["format"] == "stl":
            combined = meshdata.concat([a for _n, a in parts])
            meshdata.write_stl(path, combined, binary=not spec["ascii"], name=os.path.splitext(os.path.basename(path))[0])
        else:
            title = os.path.splitext(os.path.basename(path))[0]
            meshdata.write_3mf(path, parts, title=title, assembly=spec["assembly"],
                               application=f"VSBlender (Blender {bpy.app.version_string})",
                               metadata={"Designer": "", "Description": f"Exported from {os.path.basename(bpy.data.filepath) or 'an unsaved session'}"})
        lows = np.min([a.bounds()[0] for _n, a in parts], axis=0)
        highs = np.max([a.bounds()[1] for _n, a in parts], axis=0)
        files.append({"file": history.rel(root, path), "objects": [n for n, _a in parts],
                      "tris": int(sum(len(a.tris) for _n, a in parts)), "bytes": os.path.getsize(path),
                      "size_mm": [round(float(v), 3) for v in highs - lows], "sha256": _sha(path)})
    if not files:
        raise ValueError("nothing was written: the targets have no faces")
    return _finish(spec, files, root, params), warnings


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finish(spec: dict, files: list, root: str, params: dict) -> dict:
    lines = [f"exported {len(files)} file(s) as {spec['format']} for {spec['purpose']}"
             + (" (mm, on the bed" + (", centred" if spec["center"] else "") + ")" if spec["format"] in LIVE_FORMATS else "")]
    for f in files:
        size = f" {' x '.join(meshdata.fmt_num(v, 2) for v in f['size_mm'])} mm," if f.get("size_mm") else ""
        lines.append(f"- {f['file']}:{size} {f.get('tris', '?')} tris, {f['bytes'] // 1024} KB ({', '.join(f['objects'][:6])})")
    if spec["format"] == "3mf":
        lines.append("Bambu Studio, OrcaSlicer and PrusaSlicer open it with each part named; Bambu Studio says it is not "
                     "from Bambu Lab and loads the geometry only, which is expected.")
    try:
        history.journal(root, {"time": history.now_iso(), "event": "export", "actor": str(params.get("actor") or "ai"),
                               "format": spec["format"], "purpose": spec["purpose"], "files": files,
                               "reason": params.get("reason") or None})
    except Exception:
        pass
    return {"text": "\n".join(lines), "files": files, "format": spec["format"], "purpose": spec["purpose"]}


# ----------------------------------------------------------------------------- Blender exporters (headless)
def run_operators(spec: dict) -> list:
    """Export with Blender's operators. Runs in a background Blender on a copy: it changes selection,
    and for glTF/USD/FBX from a non-metre scene it scales the objects to metres."""
    scene = bpy.context.scene
    fmt = spec["format"]
    files = []
    scale = float(spec.get("bu_to_m") or 1.0)
    names_all = sorted({n for _l, _p, names in spec["outputs"] for n in names})
    if fmt in ("glb", "gltf", "fbx", "usd", "usda", "usdc", "usdz") and abs(scale - 1.0) > 1e-9:
        from mathutils import Matrix

        doomed = set(names_all)
        roots = [bpy.data.objects[n] for n in names_all if not (bpy.data.objects[n].parent and bpy.data.objects[n].parent.name in doomed)]
        m = Matrix.Diagonal((scale, scale, scale, 1.0))
        for ob in roots:
            ob.matrix_world = m @ ob.matrix_world
        scene.unit_settings.scale_length = 1.0
        scene.unit_settings.length_unit = "METERS"
    for label, path, names in spec["outputs"]:
        for ob in bpy.data.objects:
            try:
                ob.select_set(False)
            except RuntimeError:
                pass
        for n in names:
            ob = bpy.data.objects[n]
            ob.hide_set(False)
            ob.select_set(True)
        bpy.context.view_layer.objects.active = bpy.data.objects[names[0]]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        apply = bool(spec.get("apply_modifiers", True))
        if fmt in ("glb", "gltf"):
            bpy.ops.export_scene.gltf(filepath=path, export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
                                      use_selection=True, export_apply=apply)
        elif fmt == "fbx":
            bpy.ops.export_scene.fbx(filepath=path, use_selection=True, use_mesh_modifiers=apply, apply_unit_scale=True,
                                     path_mode="COPY", embed_textures=True)
        elif fmt in ("usd", "usda", "usdc", "usdz"):
            bpy.ops.wm.usd_export(filepath=path, selected_objects_only=True, export_materials=True)
        elif fmt == "obj":
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True, apply_modifiers=apply,
                                  export_materials=True, global_scale=1.0)
        elif fmt == "ply":
            bpy.ops.wm.ply_export(filepath=path, export_selected_objects=True, apply_modifiers=apply)
        else:
            raise ValueError(f"no operator export for {fmt}")
        if not os.path.isfile(path):
            raise RuntimeError(f"the {fmt} exporter did not write {path}")
        files.append({"file": path, "objects": names, "bytes": os.path.getsize(path), "sha256": _sha(path)})
    return files


def export_model(params: dict, root: str) -> tuple:
    """Bridge method. Live formats are written now; the others return a job spec for a background
    Blender (or run inline when this already is one, params inline=true)."""
    spec = plan(params, root)
    if spec["live"]:
        return run_live(spec, root, params)
    if params.get("inline"):
        files = run_operators(spec)
        for f in files:
            f["file"] = history.rel(root, f["file"])
        return _finish(spec, files, root, params), []
    return {"job": spec, "text": f"{spec['format']} export needs Blender's exporter; running it in a background Blender on a copy"}, []

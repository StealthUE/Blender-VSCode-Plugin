"""Background jobs for VSBlender: final renders and previews of saved copies.

    blender -b COPY.blend --python job.py -- SPEC.json

The .blend is a copy written for this job (or a checkpoint), so overrides never reach the user's
file. Progress lines on stdout:
    VSB_PROGRESS {"done": 1, "total": 4, "frame": 25}
    VSB_RESULT {...}            the last line; status "done" or "error"

Spec kinds:
    render     frames, camera, overrides, as (still | frames | sheet | mp4), output, preview, preview_max
    preview    the add-on's offscreen preview of this file: params, out
    compose    images side by side, optionally with a difference heatmap: paths, out, labels, diff
    call       one bridge method on this file (Blender is not running): method, params
    export     Blender's exporters (glTF, FBX, USD, OBJ, PLY) on this copy: export (a plan from export.plan)
    new_blend  a new .blend from a template, no input file: path, template, printer
"""
import importlib.util
import json
import os
import sys
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.environ.get("VSBLENDER_ADDON_SRC") or os.path.join(HERE, "addon", "vsblender_bridge")
OVERRIDES = {"engine", "samples", "resolution_x", "resolution_y", "resolution_percentage", "film_transparent",
             "use_compositing", "use_motion_blur", "use_denoising", "frame_step"}


def emit(tag, payload):
    print(f"{tag} {json.dumps(payload)}", flush=True)


def load_module(name):
    path = os.path.join(ADDON, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"vsblender_job_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_addon():
    """The add-on package, for its offscreen preview."""
    spec = importlib.util.spec_from_file_location("vsblender_bridge", os.path.join(ADDON, "__init__.py"),
                                                  submodule_search_locations=[ADDON])
    module = importlib.util.module_from_spec(spec)
    sys.modules["vsblender_bridge"] = module
    spec.loader.exec_module(module)
    return module


def frame_list(spec, scene):
    frames = spec.get("frames")
    if frames is None:
        return [int(spec.get("frame", scene.frame_current))]
    if isinstance(frames, dict):
        start = int(frames.get("start", scene.frame_start))
        end = int(frames.get("end", scene.frame_end))
        step = max(1, int(frames.get("step", 1)))
        if end < start:
            raise ValueError(f"frames end {end} is before start {start}")
        return list(range(start, end + 1, step))
    out = [int(f) for f in frames]
    if not out:
        raise ValueError("frames is an empty list")
    return out


def apply_overrides(scene, spec, warnings):
    r = scene.render
    camera = spec.get("camera")
    if camera:
        ob = bpy.data.objects.get(camera)
        if ob is None or ob.type != "CAMERA":
            raise KeyError(f"no camera object named {camera!r}")
        scene.camera = ob
    if scene.camera is None:
        raise RuntimeError("the scene has no camera; pass camera")
    for key, value in (spec.get("overrides") or {}).items():
        if key not in OVERRIDES:
            warnings.append(f"override {key} is not supported; supported: {', '.join(sorted(OVERRIDES))}")
            continue
        if key == "engine":
            try:
                r.engine = value
            except TypeError as exc:
                raise ValueError(f"engine {value!r}: {exc}") from exc
        elif key == "samples":
            if hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
                scene.eevee.taa_render_samples = int(value)
            if hasattr(scene, "cycles"):
                scene.cycles.samples = int(value)
        elif key == "use_denoising":
            if hasattr(scene, "cycles"):
                scene.cycles.use_denoising = bool(value)
        elif key == "frame_step":
            scene.frame_step = int(value)
        else:
            setattr(r, key, value)


def save_preview(path, out, longest, max_bytes=0):
    """A smaller copy of the result that fits in a tool reply. Returns the path written (.jpg when
    the PNG was over max_bytes)."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        scale = min(1.0, float(longest) / max(w, h, 1))
        if scale < 1.0:
            img.scale(max(1, round(w * scale)), max(1, round(h * scale)))
        img.filepath_raw = out
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)
    if max_bytes:
        out, _note = load_module("sheet").fit(out, int(max_bytes))
    return out


def render(spec):
    scene = bpy.data.scenes.get(spec.get("scene") or "") or bpy.context.scene
    if bpy.context.window is not None:
        bpy.context.window.scene = scene
    warnings = []
    apply_overrides(scene, spec, warnings)
    r = scene.render
    mode = spec.get("as") or "still"
    frames = frame_list(spec, scene)
    output = spec["output"]
    os.makedirs(os.path.dirname(output), exist_ok=True)
    started = time.time()
    if mode == "mp4":
        if isinstance(spec.get("frames"), list):
            raise ValueError("as=mp4 needs a frame range {start, end, step}, not a list of frames")
        settings = r.image_settings
        if hasattr(settings, "media_type"):
            settings.media_type = "VIDEO"
        settings.file_format = "FFMPEG"
        r.ffmpeg.format = "MPEG4"
        r.ffmpeg.codec = "H264"
        try:
            r.ffmpeg.constant_rate_factor = "MEDIUM"
        except Exception:
            pass
        scene.frame_start, scene.frame_end = frames[0], frames[-1]
        if len(frames) > 1:
            scene.frame_step = max(1, frames[1] - frames[0])
        r.filepath = output
        emit("VSB_PROGRESS", {"done": 0, "total": len(frames), "frame": frames[0]})
        with bpy.context.temp_override(scene=scene):
            bpy.ops.render.render(animation=True)
        written = [p for p in os.listdir(os.path.dirname(output)) if p.lower().endswith((".mp4", ".mkv", ".avi"))]
        candidates = [os.path.join(os.path.dirname(output), p) for p in written]
        latest = max(candidates, key=os.path.getmtime) if candidates else None
        if latest and os.path.abspath(latest) != os.path.abspath(output):
            os.replace(latest, output)
        return {"status": "done", "files": [output], "seconds": round(time.time() - started, 1), "warnings": warnings,
                "frames": frames}
    settings = r.image_settings
    if hasattr(settings, "media_type"):
        settings.media_type = "IMAGE"
    settings.file_format = "PNG"
    files = []
    frame_dir = spec.get("frame_dir") or os.path.dirname(output)
    for index, frame in enumerate(frames):
        emit("VSB_PROGRESS", {"done": index, "total": len(frames), "frame": frame})
        scene.frame_set(frame)
        if mode == "still":
            target = output
        else:
            target = os.path.join(frame_dir, f"frame_{frame:04d}.png")
        r.filepath = target
        with bpy.context.temp_override(scene=scene):
            bpy.ops.render.render(write_still=True)
        files.append(target)
    emit("VSB_PROGRESS", {"done": len(frames), "total": len(frames), "frame": frames[-1]})
    result = {"status": "done", "files": files, "frames": frames, "seconds": round(time.time() - started, 1),
              "warnings": warnings, "resolution": [r.resolution_x * r.resolution_percentage // 100,
                                                   r.resolution_y * r.resolution_percentage // 100],
              "engine": r.engine, "camera": scene.camera.name}
    if mode == "sheet":
        sheet = load_module("sheet")
        labels = [f"frame {f}" for f in frames]
        composed = sheet.compose_files(files, output, labels=labels, cell=int(spec.get("cell") or 360))
        result["files"] = [output]
        result["frame_files"] = files
        result["sheet"] = composed
    preview = spec.get("preview")
    if preview:
        result["preview"] = save_preview(result["files"][0], preview, int(spec.get("preview_max") or 1024),
                                         int(spec.get("max_bytes") or 0))
    return result


def preview(spec):
    addon = load_addon()
    params = dict(spec.get("params") or {})
    out = spec["out"]
    if params.get("views"):
        result, warnings = addon.preview_mod.preview(dict(params, out=out), [os.path.dirname(out)])
    else:
        result, warnings = addon.preview_mod.render_view(params, out)
    if params.get("max_bytes"):
        result["file"], _note = addon.sheet.fit(result["file"], int(params["max_bytes"]))
    result["warnings"] = warnings
    result["status"] = "done"
    return result


def compose(spec):
    sheet = load_module("sheet")
    result = sheet.compose_files(spec["paths"], spec["out"], labels=spec.get("labels"), columns=spec.get("columns"),
                                 cell=int(spec.get("cell") or 512), diff=bool(spec.get("diff")),
                                 max_bytes=int(spec.get("max_bytes") or 0) or None)
    result["status"] = "done"
    return result


def call(spec):
    """A bridge method on this file, for tools used while Blender is not running (source: saved file)."""
    addon = load_addon()
    addon.helpers.install_modules()
    response = addon.dispatch({"id": 1, "method": spec["method"], "params": dict(spec.get("params") or {}, inline=True)})
    if not response.get("ok"):
        return {"status": "error", "error": response.get("error") or "failed"}
    return {"status": "done", "result": response.get("result"), "warnings": response.get("warnings") or []}


def export(spec):
    addon = load_addon()
    addon.helpers.install_modules()
    files = addon.export_mod.run_operators(spec["export"])
    return {"status": "done", "files": files}


def _viewport(clip_start, clip_end, distance):
    """Best effort: the 3D views saved with the file (used when it is opened with Load UI)."""
    done = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.clip_start = clip_start
                space.clip_end = clip_end
                region = getattr(space, "region_3d", None)
                if region is not None:
                    region.view_distance = distance
                    region.view_location = (0.0, 0.0, 0.0)
                done += 1
    return done


def _curve(name, points, collection, cyclic=True):
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    spline = data.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for p, co in zip(spline.points, points):
        p.co = (co[0], co[1], co[2], 1.0)
    spline.use_cyclic_u = cyclic
    ob = bpy.data.objects.new(name, data)
    collection.objects.link(ob)
    return ob


def new_blend(spec):
    """A new file from a template. Runs with --factory-startup and no input file."""
    from mathutils import Vector

    path = os.path.abspath(spec["path"])
    if os.path.exists(path):
        raise FileExistsError(f"{path} exists")
    template = spec.get("template") or "empty"
    if template not in ("empty", "render", "game", "print_mm"):
        raise ValueError("template must be empty, render, game or print_mm")
    scene = bpy.context.scene
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.cameras, bpy.data.lights, bpy.data.materials):
        for idb in list(coll):
            if idb.users == 0:
                coll.remove(idb)
    us = scene.unit_settings
    us.system = "METRIC"
    us.system_rotation = "DEGREES"
    applied = []
    if template == "print_mm":
        us.scale_length = 0.001
        us.length_unit = "MILLIMETERS"
        us.mass_unit = "GRAMS"
        bed = [float(v) for v in ((spec.get("printer") or {}).get("build_volume") or [220, 220, 250])]
        w, d, h = bed
        coll = bpy.data.collections.new("Print Bed")
        scene.collection.children.link(coll)
        outline = _curve("Print Bed", [(-w / 2, -d / 2, 0), (w / 2, -d / 2, 0), (w / 2, d / 2, 0), (-w / 2, d / 2, 0)], coll)
        notch = _curve("Print Bed Front", [(-8, -d / 2, 0), (0, -d / 2 + 10, 0), (8, -d / 2, 0)], coll, cyclic=False)
        for ob in (outline, notch):
            ob.hide_render = True
            ob.hide_select = True
            ob["vsblender_reference"] = "build_plate"
        outline["build_volume_mm"] = bed
        applied.append(f"build plate outline {w:g} x {d:g} mm, centred on the origin (front marked at -Y; height {h:g} mm)")
        views = _viewport(0.1, 10000.0, max(w, d) * 1.6)
        applied.append(f"viewport clipping for millimetres in {views} 3D view(s)")
    else:
        us.scale_length = 1.0
        us.length_unit = "METERS"
    if template == "render":
        try:
            scene.render.engine = "BLENDER_EEVEE"
        except TypeError:
            pass
        world = bpy.data.worlds.new("Studio")
        scene.world = world
        try:
            bg = world.node_tree.nodes.get("Background")
            bg.inputs["Color"].default_value = (0.05, 0.05, 0.055, 1.0)
            bg.inputs["Strength"].default_value = 1.0
        except Exception:
            world.color = (0.05, 0.05, 0.055)
        stage = bpy.data.collections.new("Stage")
        scene.collection.children.link(stage)
        floor_me = bpy.data.meshes.new("Stage Floor")
        floor_me.from_pydata([(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)], [], [(0, 1, 2, 3)])
        mat = bpy.data.materials.new("Stage Floor")
        mat.diffuse_color = (0.18, 0.18, 0.19, 1.0)
        try:
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            bsdf.inputs["Base Color"].default_value = (0.18, 0.18, 0.19, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.85
        except Exception:
            pass
        floor_me.materials.append(mat)
        floor = bpy.data.objects.new("Stage Floor", floor_me)
        floor["vsblender_reference"] = "stage"
        stage.objects.link(floor)
        target = Vector((0.0, 0.0, 0.5))
        cam_data = bpy.data.cameras.new("Camera")
        cam_data.lens = 50
        cam = bpy.data.objects.new("Camera", cam_data)
        cam.location = (4.0, -4.0, 2.4)
        cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
        stage.objects.link(cam)
        scene.camera = cam
        for name, loc, energy, size in (("Key Light", (3.0, -2.5, 4.0), 600.0, 2.0), ("Fill Light", (-3.5, -2.0, 2.5), 200.0, 3.0),
                                        ("Rim Light", (0.5, 4.0, 3.5), 400.0, 1.5)):
            data = bpy.data.lights.new(name, "AREA")
            data.energy = energy
            data.size = size
            light = bpy.data.objects.new(name, data)
            light.location = loc
            light.rotation_euler = (target - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
            stage.objects.link(light)
        applied.append("EEVEE, a camera aimed at (0, 0, 0.5), key/fill/rim area lights, a dark grey world, "
                       "and a floor (Stage collection, left out of checks and exports)")
    scene["vsblender_template"] = template
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, compress=True)
    return {"status": "done", "file": path, "template": template, "applied": applied,
            "units": "1 BU = 1 mm" if template == "print_mm" else "1 BU = 1 m"}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        raise SystemExit("usage: blender -b FILE --python job.py -- SPEC.json")
    with open(argv[0], encoding="utf-8") as handle:
        spec = json.load(handle)
    kind = spec.get("kind")
    if kind == "render":
        return render(spec)
    if kind == "preview":
        return preview(spec)
    if kind == "compose":
        return compose(spec)
    if kind == "call":
        return call(spec)
    if kind == "export":
        return export(spec)
    if kind == "new_blend":
        return new_blend(spec)
    raise SystemExit(f"unknown job kind {kind!r}")


if __name__ == "__main__":
    try:
        outcome = main()
    except BaseException as exc:  # noqa: BLE001 - the caller needs a result line whatever happened
        outcome = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-3000:]}
    emit("VSB_RESULT", outcome)
    sys.stdout.flush()
    if outcome.get("status") != "done":
        sys.exit(1)

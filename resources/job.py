"""Background jobs for VSBlender: final renders and previews of saved copies.

    blender -b COPY.blend --python job.py -- SPEC.json

The .blend is a copy written for this job (or a checkpoint), so overrides never reach the user's
file. Progress lines on stdout:
    VSB_PROGRESS {"done": 1, "total": 4, "frame": 25}
    VSB_RESULT {...}            the last line; status "done" or "error"

Spec kinds:
    render   frames, camera, overrides, as (still | frames | sheet | mp4), output, preview, preview_max
    preview  the add-on's offscreen preview of this file: params, out
    compose  images side by side, optionally with a difference heatmap: paths, out, labels, diff
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
        return list(range(start, end + 1, step))
    return [int(f) for f in frames]


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


def save_preview(path, out, longest):
    """A smaller copy of the result that fits in a tool reply."""
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
        save_preview(result["files"][0], preview, int(spec.get("preview_max") or 1024))
        result["preview"] = preview
    return result


def preview(spec):
    addon = load_addon()
    params = dict(spec.get("params") or {})
    out = spec["out"]
    if params.get("views"):
        result, warnings = addon.preview_mod.preview(dict(params, out=out), [os.path.dirname(out)])
    else:
        result, warnings = addon.preview_mod.render_view(params, out)
    result["warnings"] = warnings
    result["status"] = "done"
    return result


def compose(spec):
    sheet = load_module("sheet")
    result = sheet.compose_files(spec["paths"], spec["out"], labels=spec.get("labels"), columns=spec.get("columns"),
                                 cell=int(spec.get("cell") or 512), diff=bool(spec.get("diff")))
    result["status"] = "done"
    return result


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

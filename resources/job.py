"""Background jobs for VSBlender: final renders and previews of saved copies.

    blender -b COPY.blend --python job.py -- SPEC.json

The .blend is a copy written for this job (or a checkpoint), so overrides never reach the user's
file. Progress lines on stdout:
    VSB_PROGRESS {"done": 1, "total": 4, "frame": 25}
    VSB_RESULT {...}            the last line; status "done" or "error"

Spec kinds:
    render     frames, camera, overrides, as (still | frames | sheet | mp4), output, preview, preview_max, ffmpeg
               (frames and mp4 default to the scene's frame range; mp4 is encoded by ffmpeg when given)
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


def frame_list(spec, scene, mode="still"):
    """The frames to render. Without frames, a still is the current frame (or frame), and frames and
    mp4 are the scene's whole frame range: one frame of a video is never what was asked for."""
    frames = spec.get("frames")
    if frames is None:
        if mode in ("mp4", "frames"):
            return list(range(int(scene.frame_start), int(scene.frame_end) + 1, max(1, int(scene.frame_step or 1))))
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


def _fps(scene) -> float:
    return float(scene.render.fps) / float(scene.render.fps_base or 1.0)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def render_video(scene, spec, frames, output, warnings, started):
    """A video that plays everywhere, written under a temporary name and moved into place when it is
    complete, so a half-written file never sits at the output path.

    With ffmpeg (spec ffmpeg): every frame as a PNG, with progress per frame, then H.264, yuv420p and
    +faststart. Without it: Blender's own FFmpeg writer, with progress from render_post.
    """
    r = scene.render
    total = len(frames)
    folder = os.path.dirname(output)
    stem = os.path.splitext(os.path.basename(output))[0]
    tmp = os.path.join(folder, f".{stem}.partial.mp4")
    _remove(tmp)
    fps = _fps(scene)
    ffmpeg = spec.get("ffmpeg")
    result = {"status": "done", "files": [output], "frames": frames, "frames_expected": total, "fps": round(fps, 3),
              "warnings": warnings, "camera": scene.camera.name, "engine": r.engine,
              "resolution": [r.resolution_x * r.resolution_percentage // 100, r.resolution_y * r.resolution_percentage // 100]}
    if ffmpeg and os.path.isfile(ffmpeg):
        import subprocess

        frame_dir = spec.get("frame_dir") or os.path.join(folder, f"{stem}_frames")
        os.makedirs(frame_dir, exist_ok=True)
        settings = r.image_settings
        if hasattr(settings, "media_type"):
            settings.media_type = "IMAGE"
        settings.file_format = "PNG"
        settings.color_mode = "RGB"
        files = []
        for index, frame in enumerate(frames):
            emit("VSB_PROGRESS", {"done": index, "total": total, "frame": frame, "stage": "rendering"})
            scene.frame_set(frame)
            r.filepath = os.path.join(frame_dir, f"seq_{index:05d}.png")
            with bpy.context.temp_override(scene=scene):
                bpy.ops.render.render(write_still=True)
            files.append(r.filepath)
        emit("VSB_PROGRESS", {"done": total, "total": total, "frame": frames[-1], "stage": "encoding"})
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", f"{fps:.6g}", "-i", os.path.join(frame_dir, "seq_%05d.png"),
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-crf", str(int(spec.get("crf") or 18)), "-preset", "medium", "-movflags", "+faststart", tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if proc.returncode != 0 or not os.path.isfile(tmp):
            _remove(tmp)
            raise RuntimeError(f"ffmpeg could not encode the frames (exit {proc.returncode}): {proc.stderr.strip()[-800:]}. "
                               f"The frames are kept in {frame_dir}")
        os.replace(tmp, output)
        result["encoder"] = "ffmpeg: H.264, yuv420p, +faststart"
        result["frames_rendered"] = len(files)
        if spec.get("preview"):
            picks = sorted({0, total // 3, (2 * total) // 3, total - 1})
            sheet = load_module("sheet")
            composed = sheet.compose_files([files[i] for i in picks], spec["preview"],
                                           labels=[f"frame {frames[i]}" for i in picks], cell=270,
                                           max_bytes=int(spec.get("max_bytes") or 0) or None)
            result["preview"] = composed["file"]
        if spec.get("keep_frames"):
            result["frame_files"] = files
        else:
            for path in files:
                _remove(path)
            try:
                os.rmdir(frame_dir)
            except OSError:
                pass
    else:
        settings = r.image_settings
        if hasattr(settings, "media_type"):
            settings.media_type = "VIDEO"
        settings.file_format = "FFMPEG"
        r.ffmpeg.format = "MPEG4"
        r.ffmpeg.codec = "H264"
        try:
            r.ffmpeg.constant_rate_factor = "HIGH"
        except Exception:
            pass
        scene.frame_start, scene.frame_end = frames[0], frames[-1]
        scene.frame_step = max(1, frames[1] - frames[0])
        r.use_file_extension = True
        r.filepath = os.path.join(folder, f".{stem}.partial")
        done = [0]

        def on_frame(*_args):
            done[0] += 1
            emit("VSB_PROGRESS", {"done": done[0], "total": total, "frame": frames[min(done[0], total) - 1], "stage": "rendering"})

        emit("VSB_PROGRESS", {"done": 0, "total": total, "frame": frames[0], "stage": "rendering"})
        bpy.app.handlers.render_post.append(on_frame)
        try:
            with bpy.context.temp_override(scene=scene):
                bpy.ops.render.render(animation=True)
        finally:
            bpy.app.handlers.render_post.remove(on_frame)
        # Blender adds the frame range and the extension to the name: find what it wrote.
        written = [os.path.join(folder, p) for p in os.listdir(folder)
                   if p.startswith(f".{stem}.partial") and p.lower().endswith((".mp4", ".mkv", ".avi", ".mov"))]
        if not written:
            raise RuntimeError("Blender's FFmpeg writer produced no file")
        latest = max(written, key=os.path.getmtime)
        os.replace(latest, output)
        for path in written:
            if path != latest:
                _remove(path)
        result["encoder"] = "Blender's FFmpeg writer (H.264). Set \"ffmpeg\" in .blender-ai/config.json for yuv420p and +faststart"
        result["frames_rendered"] = done[0]
        if done[0] and done[0] < total:
            warnings.append(f"Blender reported {done[0]} of {total} frames")
    result["seconds"] = round(time.time() - started, 1)
    result["bytes"] = os.path.getsize(output)
    return result


def render(spec):
    scene = bpy.data.scenes.get(spec.get("scene") or "") or bpy.context.scene
    if bpy.context.window is not None:
        bpy.context.window.scene = scene
    warnings = []
    apply_overrides(scene, spec, warnings)
    r = scene.render
    mode = spec.get("as") or "still"
    frames = frame_list(spec, scene, mode)
    output = spec["output"]
    os.makedirs(os.path.dirname(output), exist_ok=True)
    started = time.time()
    if mode == "mp4":
        if isinstance(spec.get("frames"), list):
            raise ValueError("as=mp4 needs a frame range {start, end, step}, not a list of frames")
        if len(frames) < 2:
            raise ValueError(f"as=mp4 needs at least 2 frames; the range gives {len(frames)}. Use as=still for one frame")
        return render_video(scene, spec, frames, output, warnings, started)
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
    result = {"status": "done", "files": files, "frames": frames, "frames_expected": len(frames), "frames_rendered": len(files),
              "seconds": round(time.time() - started, 1),
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
            ob["vsblender_stage"] = "build_plate"
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
        floor["vsblender_stage"] = "floor"
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


def _safe(name):
    import re

    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "part"


def _import_file(path, axes):
    """Blender's own importer for the file, in a background Blender. axes "blender": as File > Import
    does (a Y-up OBJ stands up in Z); "native": the file's own coordinates."""
    ext = os.path.splitext(path)[1].lower()
    native = axes == "native"
    before = set(bpy.data.objects)
    note = ""
    if ext == ".obj":
        kwargs = {"filepath": path, "use_split_objects": False, "use_split_groups": False}
        if native:
            kwargs.update(forward_axis="Y", up_axis="Z")
        bpy.ops.wm.obj_import(**kwargs)
    elif ext == ".fbx":
        kwargs = {"filepath": path}
        if native:
            kwargs.update(use_manual_orientation=True, axis_forward="Y", axis_up="Z")
        bpy.ops.import_scene.fbx(**kwargs)
    elif ext in (".gltf", ".glb"):
        bpy.ops.import_scene.gltf(filepath=path)
        if native:
            note = "glTF is Y-up by definition and Blender's importer always turns it Z-up; axes native has no effect"
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    else:
        raise ValueError(f"import_reference reads OBJ, FBX, glTF/GLB, STL and PLY, not {ext} (3MF: use measure/overlay with file)")
    return [ob for ob in bpy.data.objects if ob not in before and ob.type == "MESH"], note


def import_reference(spec):
    """A big reference (scan, show model, CAD export) as per-part binary STL plus a light overlay, so
    measure and preview overlays can read it quickly and nothing of it goes into the user's file."""
    import numpy as np

    meshdata = load_module("meshdata")
    src = spec["file"]
    out_dir = spec["out_dir"]
    split = spec.get("split") or "material"
    if split not in ("material", "object", "none"):
        raise ValueError("split must be material, object or none")
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(out_dir):
        if name.lower().endswith(".stl"):
            os.remove(os.path.join(out_dir, name))
    started = time.time()
    emit("VSB_PROGRESS", {"done": 0, "total": 4, "stage": "importing"})
    obs, note = _import_file(src, spec.get("axes") or "blender")
    if not obs:
        raise ValueError(f"{src} has no mesh objects")
    t_import = time.time() - started
    emit("VSB_PROGRESS", {"done": 1, "total": 4, "stage": "splitting"})
    deps = bpy.context.evaluated_depsgraph_get()
    groups = {}
    for ob in obs:
        arr = meshdata.object_arrays(ob, deps, world=True, instances=False)
        if split == "object":
            groups.setdefault(ob.name, []).append(arr)
            continue
        if split == "none":
            groups.setdefault("all", []).append(arr)
            continue
        names = [s.material.name.split(".")[0] if s.material else "default" for s in ob.material_slots] or ["default"]
        mat = arr.material if arr.material is not None else np.zeros(len(arr.tris), dtype=np.int64)
        for idx in np.unique(mat):
            name = names[int(idx)] if int(idx) < len(names) else "default"
            groups.setdefault(name, []).append(arr.select(mat == idx))
    emit("VSB_PROGRESS", {"done": 2, "total": 4, "stage": "writing parts"})
    parts = {}
    rows = []
    radial = spec.get("radial") or None
    used = set()
    for name, chunks in sorted(groups.items()):
        arrays = meshdata.concat(chunks)
        file_name = _safe(name)
        while file_name.lower() in used:
            file_name += "_"
        used.add(file_name.lower())
        path = os.path.join(out_dir, f"{file_name}.stl")
        meshdata.write_stl(path, arrays, name=name)
        parts[name] = path
        lo, hi = arrays.bounds()
        row = {"part": name, "tris": int(len(arrays.tris)), "min": [round(float(v), 5) for v in lo],
               "max": [round(float(v), 5) for v in hi], "bytes": os.path.getsize(path)}
        if radial:
            axis = {"x": 0, "y": 1, "z": 2}[str(radial.get("axis") or "z").lower()]
            center = np.asarray(radial.get("center") or [0, 0, 0], float)
            a, b = [i for i in range(3) if i != axis]
            rel = arrays.verts - center
            r = np.hypot(rel[:, a], rel[:, b])
            ang = np.degrees(np.arctan2(rel[:, a], rel[:, b])) % 360.0
            row["r"] = [round(float(np.percentile(r, 0.5)), 4), round(float(np.percentile(r, 99.5)), 4)]
            row["depth"] = [round(float(np.percentile(rel[:, axis], 0.5)), 4), round(float(np.percentile(rel[:, axis], 99.5)), 4)]
            row["angles_covered_deg"] = int(len(np.unique(np.floor(ang))))
        rows.append(row)
    emit("VSB_PROGRESS", {"done": 3, "total": 4, "stage": "overlay"})
    whole = meshdata.concat([meshdata.concat(chunks) for chunks in groups.values()])
    lo, hi = whole.bounds()
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    cell = float(spec.get("overlay_cell") or 0.0) or diag / 600.0
    overlay = os.path.join(out_dir, "_overlay.stl")
    if len(whole.tris) <= int(spec.get("overlay_max_tris") or 150000):
        meshdata.write_stl(overlay, whole, name="overlay")
        overlay_tris = len(whole.tris)
    else:
        # Vertex clustering: every vertex snaps to a grid of `cell`; collapsed and repeated triangles go.
        q = np.round(whole.verts / cell).astype(np.int64)
        uniq, inv = np.unique(q, axis=0, return_inverse=True)
        tris = inv.reshape(-1)[whole.tris]
        keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
        tris = np.unique(np.sort(tris[keep], axis=1), axis=0)
        meshdata.write_stl(overlay, meshdata.MeshArrays(uniq * cell, tris), name="overlay")
        overlay_tris = len(tris)
    for ob in obs:
        bpy.data.objects.remove(ob, do_unlink=True)
    emit("VSB_PROGRESS", {"done": 4, "total": 4, "stage": "done"})
    return {"status": "done", "parts": parts, "rows": rows, "overlay": overlay, "overlay_tris": int(overlay_tris),
            "overlay_cell": cell, "tris": int(len(whole.tris)), "bounds": [lo.tolist(), hi.tolist()],
            "seconds": {"import": round(t_import, 1), "total": round(time.time() - started, 1)}, "note": note,
            "objects": len(obs)}


def video_frames(spec):
    """Frames of a reference video as one labelled contact sheet: at times [...] or every 1/fps
    seconds from start to end, optionally cropped, or as differences from the frame before (diff),
    which is what makes a small moving light visible. Needs ffmpeg."""
    import subprocess

    import numpy as np

    sheet = load_module("sheet")
    ffmpeg = spec["ffmpeg"]
    video = spec["video"]
    times = [float(t) for t in spec.get("times") or []]
    if not times:
        start = float(spec.get("start") or 0.0)
        end = float(spec["end"]) if spec.get("end") is not None else start + 10.0
        fps = float(spec.get("fps") or 1.0)
        if fps <= 0 or end <= start:
            raise ValueError("fps must be positive and end after start")
        times = [round(start + i / fps, 3) for i in range(int((end - start) * fps) + 1)]
    cap = int(spec.get("max_tiles") or 16)
    if len(times) > cap:
        raise ValueError(f"{len(times)} frames is too many for one sheet (limit {cap}); pass fewer times, a lower fps or a shorter range")
    work = spec["work"]
    os.makedirs(work, exist_ok=True)
    files = []
    for i, t in enumerate(times):
        emit("VSB_PROGRESS", {"done": i, "total": len(times), "stage": "extracting"})
        path = os.path.join(work, f"t{i:03d}.png")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not os.path.isfile(path):
            raise RuntimeError(f"ffmpeg could not read {video} at {t:.3f} s: {proc.stderr.strip()[-400:]}")
        files.append(path)
    crop = spec.get("crop")
    images = [sheet.load(p) for p in files]
    if crop:
        images = [sheet.crop_pixels(img, crop) for img in images]
    labels = [f"{t:g} s" for t in times]
    if spec.get("diff"):
        out_images = [images[0]]
        out_labels = [labels[0]]
        for prev, cur, label, plabel in zip(images, images[1:], labels[1:], labels):
            delta = np.abs(cur[..., :3] - prev[..., :3]).max(axis=2)
            img = np.zeros_like(cur)
            img[..., 3] = 1.0
            # The new frame dimmed, with what changed since the one before in bright red.
            img[..., :3] = cur[..., :3] * 0.35
            img[..., 0] = np.maximum(img[..., 0], np.clip(delta * 4.0, 0, 1))
            out_images.append(img)
            out_labels.append(f"{label} (red: changed since {plabel})")
        images, labels = out_images, out_labels
    composed = sheet.compose(images, labels, cell=int(spec.get("cell") or 300))
    out = spec["out"]
    sheet.save(out, composed)
    result = {"status": "done", "file": out, "times": times, "tiles": len(images)}
    if spec.get("max_bytes"):
        result["file"], note = sheet.fit(out, int(spec["max_bytes"]))
        if note:
            result["note"] = note
    for p in files:
        try:
            os.remove(p)
        except OSError:
            pass
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
    if kind == "call":
        return call(spec)
    if kind == "export":
        return export(spec)
    if kind == "new_blend":
        return new_blend(spec)
    if kind == "import_reference":
        return import_reference(spec)
    if kind == "video_frames":
        return video_frames(spec)
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

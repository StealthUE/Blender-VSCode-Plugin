"""Offscreen previews: a throwaway scene and camera, so the user's viewport, camera and render
settings never change.

What a preview includes:
- view camera copies the scene camera's lens, sensor, shift and clipping. Depth of field only with dof.
- material and rendered shading copy the scene's colour management (view transform, look, exposure).
  rendered also copies the EEVEE and Cycles settings (volumetrics, shadows, samples cap).
- The compositor (glare, bloom, lens effects) only with compositor=true, Blender 5 or newer.
- Motion blur only with motion_blur=true. The background is never transparent.
- Axis views (front, back, left, right, top, bottom) are orthographic by default, iso is perspective.
- Without a target, the view frames the main subject: ground planes, skies and scattered series are
  left out of the framing (not hidden). framing="all" frames every object.
"""
from __future__ import annotations

import math
import os
import tempfile

import bpy
from mathutils import Vector

from . import sheet

VIEWS = {
    "front": Vector((0.0, -1.0, 0.0)),
    "back": Vector((0.0, 1.0, 0.0)),
    "left": Vector((-1.0, 0.0, 0.0)),
    "right": Vector((1.0, 0.0, 0.0)),
    "top": Vector((0.0, 0.0, 1.0)),
    "bottom": Vector((0.0, 0.0, -1.0)),
    "iso": Vector((1.0, -1.0, 1.0)),
}
GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GPENCIL", "GREASEPENCIL"}
MAX_PIXELS = 16_000_000


def _under(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def _scene():
    try:
        scene = bpy.context.scene
        if scene is not None:
            return scene
    except Exception:
        pass
    return bpy.data.scenes[0] if bpy.data.scenes else None


def _set_engine(scene, names: list) -> str:
    last = None
    for name in names:
        try:
            scene.render.engine = name
            return scene.render.engine
        except Exception as exc:
            last = exc
    raise RuntimeError(f"no usable render engine ({last})")


def bounds(objects) -> tuple:
    """World bounds of the geometry, read from evaluated objects.

    An object made since the last depsgraph update still has an empty bound_box on the original,
    which put the camera inside it. Lights, cameras and empties only count when there is no geometry.
    """
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None
    usable = [obj for obj in objects if obj is not None and not obj.name.startswith("_vsblender")]
    shaped = [obj for obj in usable if obj.type in GEOMETRY]
    low = Vector((1e18, 1e18, 1e18))
    high = Vector((-1e18, -1e18, -1e18))
    found = False
    for original in shaped or usable:
        obj = original
        if depsgraph is not None:
            try:
                obj = original.evaluated_get(depsgraph)
            except Exception:
                obj = original
        try:
            corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        except Exception:
            corners = [obj.matrix_world.translation.copy()]
        for point in corners:
            low.x, low.y, low.z = min(low.x, point.x), min(low.y, point.y), min(low.z, point.z)
            high.x, high.y, high.z = max(high.x, point.x), max(high.y, point.y), max(high.z, point.z)
            found = True
    if not found:
        return Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 0.0))
    return low, high


def subject_bounds(objects):
    """Bounds of the main subject: without ground planes, skies, far props, and scattered series.

    The same rule as the ingest overviews (blender_ingest.framing). Returns (low, high, note), or
    None when there is nothing to leave out.
    """
    from . import blender_ingest

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None
    boxes = {}
    for original in objects:
        if original.type not in GEOMETRY or original.name.startswith("_vsblender"):
            continue
        obj = original.evaluated_get(depsgraph) if depsgraph is not None else original
        try:
            points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        except Exception:
            continue
        low = [min(p[i] for p in points) for i in range(3)]
        high = [max(p[i] for p in points) for i in range(3)]
        boxes[original.name] = {"bbox": [low, high], "hidden": {"render": False}}
    if len(boxes) < 4:
        return None
    box, excluded, scattered = blender_ingest.framing({"objects": boxes}, main=True)
    if box is None or (not excluded and not scattered):
        return None
    parts = []
    if excluded:
        parts.append(f"left out {', '.join(excluded[:4])}{' ...' if len(excluded) > 4 else ''}")
    if scattered:
        parts.append(f"{len(scattered)} scattered objects ({blender_ingest.series_base(scattered[0])} ...) reach outside the frame")
    return Vector(box[0]), Vector(box[1]), "framed on the main subject: " + "; ".join(parts) + ". framing=all shows everything."


def _look_at(cam, location, target, view: str) -> None:
    """A camera looks down its local -Z with local Y up. Top and bottom have no single up, so they are set."""
    cam.location = location
    if view == "top":
        cam.rotation_euler = (0.0, 0.0, 0.0)
        return
    if view == "bottom":
        cam.rotation_euler = (math.pi, 0.0, 0.0)
        return
    direction = Vector(target) - Vector(location)
    if direction.length < 1e-8:
        return
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _collect(src, target_name):
    if target_name:
        obj = src.objects.get(target_name) or bpy.data.objects.get(target_name)
        if obj is None:
            raise KeyError(f"no object named {target_name}")
        return [obj, *getattr(obj, "children_recursive", [])]
    return [obj for obj in src.objects if obj.type != "CAMERA" and not obj.hide_render and not obj.name.startswith("_vsblender")]


def _scene_contains(scene, obj) -> bool:
    if obj.name in scene.collection.objects:
        return True
    try:
        children = scene.collection.children_recursive
    except Exception:
        children = scene.collection.children
    return any(obj.name in child.objects for child in children)


def _copy_rna(src, dst, skip=()) -> None:
    if src is None or dst is None:
        return
    for prop in src.bl_rna.properties:
        key = prop.identifier
        if prop.is_readonly or key in skip or key == "rna_type" or prop.type in {"POINTER", "COLLECTION"}:
            continue
        # Viewport-only settings; some have update callbacks that need a screen.
        if key.startswith("preview_"):
            continue
        try:
            setattr(dst, key, getattr(src, key))
        except Exception:
            pass


def _new_preview_scene(src, isolate_objects=None):
    """New scene only. Never scene.copy(): a shared master collection must not be deleted with the preview."""
    preview = bpy.data.scenes.new("_vsblender_preview")
    private = bpy.data.collections.new("_vsblender_preview")
    preview.collection.children.link(private)
    if isolate_objects is None:
        for child in list(src.collection.children):
            if child.name.startswith("_vsblender"):
                continue
            try:
                preview.collection.children.link(child)
            except RuntimeError:
                pass
        for obj in list(src.collection.objects):
            if obj.name.startswith("_vsblender"):
                continue
            try:
                preview.collection.objects.link(obj)
            except RuntimeError:
                pass
    else:
        # Isolated: only the target hierarchy, plus the lights so material shading is not black.
        wanted = list(isolate_objects) + [o for o in src.objects if o.type == "LIGHT" and o not in isolate_objects]
        for obj in wanted:
            try:
                private.objects.link(obj)
            except RuntimeError:
                pass
    preview.world = src.world
    preview.frame_start = src.frame_start
    preview.frame_end = src.frame_end
    preview.frame_set(src.frame_current)
    return preview, private


def _cleanup(scene, cam, cam_data, private) -> None:
    if private is not None:
        for obj in list(private.objects):
            try:
                private.objects.unlink(obj)
            except Exception:
                pass
    if cam is not None:
        try:
            bpy.data.objects.remove(cam, do_unlink=True)
        except Exception:
            pass
    if cam_data is not None:
        try:
            if cam_data.users == 0:
                bpy.data.cameras.remove(cam_data)
        except Exception:
            pass
    if scene is not None:
        try:
            bpy.data.scenes.remove(scene)
        except Exception:
            pass
    if private is not None:
        try:
            if private.users == 0:
                bpy.data.collections.remove(private)
        except Exception:
            pass


def purge_leftovers() -> None:
    for scene in list(bpy.data.scenes):
        if scene.name.startswith("_vsblender_preview"):
            try:
                bpy.data.scenes.remove(scene)
            except Exception:
                pass
    for collection in list(bpy.data.collections):
        if collection.name.startswith("_vsblender_preview") and collection.users == 0:
            try:
                bpy.data.collections.remove(collection)
            except Exception:
                pass
    for obj in list(bpy.data.objects):
        if obj.name.startswith("_vsblender_preview"):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
    for image in list(bpy.data.images):
        if image.name.startswith("_vsblender"):
            try:
                bpy.data.images.remove(image)
            except Exception:
                pass


def _render_still(scene) -> None:
    """Render one scene without touching the user's scene. temp_override needs Blender 3.2 or newer."""
    if getattr(bpy.context, "temp_override", None) is None:
        raise RuntimeError("offscreen preview needs Blender 3.2 or newer")
    with bpy.context.temp_override(scene=scene):
        bpy.ops.render.render(write_still=True)


def _resolution(params: dict, src, view: str) -> tuple:
    width, height = params.get("width"), params.get("height")
    if width or height:
        w = int(width or height)
        h = int(height or width)
    else:
        size = max(64, min(4096, int(params.get("size") or 512)))
        aspect = params.get("aspect")
        if aspect is None:
            aspect = "camera" if view == "camera" else "square"
        if aspect == "camera":
            r = src.render
            ratio = (r.resolution_x * r.pixel_aspect_x) / max(1.0, r.resolution_y * r.pixel_aspect_y)
        elif aspect == "square":
            ratio = 1.0
        else:
            try:
                ratio = float(aspect)
            except (TypeError, ValueError) as exc:
                raise ValueError("aspect must be camera, square, or a number such as 1.777") from exc
        ratio = max(0.1, min(10.0, ratio))
        w, h = (size, max(16, round(size / ratio))) if ratio >= 1 else (max(16, round(size * ratio)), size)
    w, h = max(16, min(8192, w)), max(16, min(8192, h))
    if w * h > MAX_PIXELS:
        raise ValueError(f"{w}x{h} is over the {MAX_PIXELS // 1_000_000} MP preview limit")
    return w, h


def render_view(params: dict, out: str) -> dict:
    view = str(params.get("view") or "iso")
    shading = str(params.get("shading") or "solid")
    if view != "camera" and view not in VIEWS:
        raise ValueError("view must be camera, front, back, left, right, top, bottom, or iso")
    if shading not in {"solid", "material", "rendered"}:
        raise ValueError("shading must be solid, material, or rendered")
    projection = params.get("projection")
    if projection not in (None, "", "ortho", "persp"):
        raise ValueError("projection must be ortho or persp")
    if not projection:
        projection = "persp" if view in ("iso", "camera") else "ortho"
    src = _scene()
    if src is None:
        raise RuntimeError("no scene")
    target_name = params.get("target") or None
    if target_name is not None and not isinstance(target_name, str):
        raise ValueError("target must be an object name")
    isolate = bool(params.get("isolate")) and bool(target_name)
    frame = params.get("frame")
    width, height = _resolution(params, src, view)
    saved_frame = src.frame_current
    warnings = []
    applied = []
    framing_note = ""
    scene = cam = cam_data = private = None
    try:
        objects = _collect(src, target_name)
        scene, private = _new_preview_scene(src, objects if isolate else None)
        if frame is not None:
            scene.frame_set(int(frame))
        try:
            scene.view_layers[0].update()
        except Exception:
            pass
        if target_name and not isolate and objects and not _scene_contains(scene, objects[0]):
            try:
                private.objects.link(objects[0])
            except RuntimeError:
                pass
        if view == "camera":
            src_cam = src.camera
            if src_cam is None:
                raise RuntimeError("this file has no camera; use view iso or front")
            if src_cam.type == "CAMERA":
                cam_data = src_cam.data.copy()
                cam_data.name = "_vsblender_preview_cam"
                if not params.get("dof"):
                    cam_data.dof.use_dof = False
                elif cam_data.dof.use_dof:
                    applied.append("depth of field")
            else:
                cam_data = bpy.data.cameras.new("_vsblender_preview_cam")
            cam = bpy.data.objects.new("_vsblender_preview_cam", cam_data)
            private.objects.link(cam)
            cam.matrix_world = src_cam.matrix_world.copy()
            projection = "ortho" if cam_data.type == "ORTHO" else "persp"
        else:
            cam_data = bpy.data.cameras.new("_vsblender_preview_cam")
            cam = bpy.data.objects.new("_vsblender_preview_cam", cam_data)
            private.objects.link(cam)
            box = params.get("bounds")
            subject = None
            if box:  # Set by the live ingest, which frames the main subject rather than every object.
                low, high = Vector(box[0]), Vector(box[1])
            elif not target_name and params.get("framing") != "all":
                subject = subject_bounds(objects)
            if subject is not None:
                low, high, framing_note = subject
            elif not box:
                low, high = bounds(objects)
            center = (low + high) * 0.5
            extent = high - low
            radius = max(extent.length * 0.5, 0.25)
            direction = VIEWS[view].normalized()
            aspect = width / height
            cam_data.sensor_fit = "AUTO"
            if projection == "ortho":
                # Width and height of the box as seen along the view direction.
                axes = {"front": (0, 2), "back": (0, 2), "left": (1, 2), "right": (1, 2), "top": (0, 1), "bottom": (0, 1)}
                if view in axes:
                    a, b = axes[view]
                    seen_w, seen_h = extent[a], extent[b]
                else:
                    seen_w = seen_h = radius * 2
                cam_data.type = "ORTHO"
                # With sensor_fit AUTO, ortho_scale is the longer side of the image.
                if aspect >= 1:
                    scale = max(seen_w, seen_h * aspect)
                else:
                    scale = max(seen_h, seen_w / aspect)
                cam_data.ortho_scale = max(scale, 0.1) * 1.12
                distance = radius * 2.0 + 1.0
            else:
                lens = 50.0
                # The lens angle covers the longer side; the bounding sphere has to fit the shorter one.
                half = math.atan(36.0 / (2.0 * lens))
                half = math.atan(math.tan(half) * min(aspect, 1.0 / aspect))
                distance = max(0.5, radius / math.sin(half) * 1.05)
                cam_data.lens = lens
            _look_at(cam, center + direction * distance, center, view)
            cam_data.clip_start = max(0.001, distance / 1000.0)
            cam_data.clip_end = max(1000.0, distance * 4.0 + radius * 4.0)
        scene.camera = cam

        if shading == "solid":
            wanted = ["BLENDER_WORKBENCH"]
        elif shading == "material":
            wanted = ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"]
        else:
            wanted = [src.render.engine, "BLENDER_EEVEE", "CYCLES", "BLENDER_WORKBENCH"]
        engine = _set_engine(scene, wanted)
        if shading != "solid" and engine == "BLENDER_WORKBENCH":
            warnings.append(f"{shading} preview fell back to Workbench")
        if shading != "solid":
            _copy_rna(src.view_settings, scene.view_settings)
            _copy_rna(src.display_settings, scene.display_settings)
            try:
                scene.view_settings.look = src.view_settings.look
            except Exception:
                pass
        if shading == "rendered":
            _copy_rna(getattr(src, "eevee", None), getattr(scene, "eevee", None))
            _copy_rna(getattr(src, "cycles", None), getattr(scene, "cycles", None), skip={"device"})
        samples = params.get("samples")
        eevee = getattr(scene, "eevee", None)
        if eevee is not None and hasattr(eevee, "taa_render_samples"):
            current = int(getattr(src.eevee, "taa_render_samples", 16) or 16) if hasattr(src, "eevee") else 16
            eevee.taa_render_samples = max(1, int(samples)) if samples else min(current, 32)
        cycles = getattr(scene, "cycles", None)
        if cycles is not None and hasattr(cycles, "samples"):
            current = int(getattr(getattr(src, "cycles", None), "samples", 32) or 32)
            cycles.samples = max(1, int(samples)) if samples else min(current, 32)
        if params.get("compositor"):
            group = getattr(src, "compositing_node_group", None)
            if hasattr(scene, "compositing_node_group"):
                if group is not None:
                    scene.compositing_node_group = group
                    scene.render.use_compositing = True
                    applied.append("compositor")
                else:
                    warnings.append("compositor=true, but this scene has no compositor node group")
            else:
                warnings.append("compositor previews need Blender 5 or newer (scene.compositing_node_group)")
        else:
            scene.render.use_compositing = False
        if params.get("motion_blur"):
            scene.render.use_motion_blur = bool(src.render.use_motion_blur)
            for key in ("motion_blur_shutter", "motion_blur_position"):
                if hasattr(src.render, key):
                    try:
                        setattr(scene.render, key, getattr(src.render, key))
                    except Exception:
                        pass
            if src.render.use_motion_blur:
                applied.append("motion blur")
        else:
            scene.render.use_motion_blur = False
        display = getattr(scene, "display", None)
        shading_settings = getattr(display, "shading", None) if display else None
        if shading_settings is not None:
            try:
                shading_settings.light = "STUDIO"
                shading_settings.color_type = "MATERIAL"
            except Exception:
                pass
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = os.path.abspath(out)
        scene.render.film_transparent = False
        _render_still(scene)
        if not os.path.isfile(scene.render.filepath):
            raise RuntimeError("render finished without writing the png")
        return {
            "file": scene.render.filepath,
            "view": view,
            "shading": shading,
            "engine": engine,
            "width": width,
            "height": height,
            "projection": projection,
            "isolate": isolate,
            "frame": int(frame) if frame is not None else saved_frame,
            "applied": applied,
            "framing": framing_note,
        }, warnings
    finally:
        _cleanup(scene, cam, cam_data, private)
        # Objects are shared with the preview scene, so evaluating another frame there wrote animated
        # values onto them. Re-evaluate the user's frame to put them back.
        if frame is not None:
            try:
                src.frame_set(saved_frame)
            except Exception:
                pass


def preview(params: dict, roots: list):
    out = params.get("out")
    if not isinstance(out, str) or not out:
        raise ValueError("out path is required")
    allowed = [tempfile.gettempdir(), *roots]
    if bpy.data.filepath:
        allowed.append(os.path.dirname(bpy.data.filepath))
    if not any(root and _under(out, root) for root in allowed):
        raise ValueError("refusing to write the preview outside the workspace or temp directory")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    views = params.get("views")
    if not views:
        return render_view(params, out)
    if not isinstance(views, list) or not all(isinstance(v, str) for v in views):
        raise ValueError("views must be a list of view names")
    if len(views) > 9:
        raise ValueError("at most 9 views in one contact sheet")
    parts, labels, results, warnings = [], [], [], []
    base, _ext = os.path.splitext(os.path.abspath(out))
    try:
        for index, view in enumerate(views):
            single = dict(params, view=view, views=None)
            part = f"{base}.part{index}.png"
            result, more = render_view(single, part)
            parts.append(part)
            results.append(result)
            warnings.extend(more)
            label = view
            if params.get("target"):
                label += f" - {params['target']}"
            labels.append(label)
        cell = max(results[0]["height"], 64)
        composed = sheet.compose_files(parts, out, labels=labels, cell=min(cell, 768))
    finally:
        for part in parts:
            try:
                os.remove(part)
            except OSError:
                pass
    first = results[0]
    return {
        "file": composed["file"],
        "views": views,
        "shading": first["shading"],
        "engine": first["engine"],
        "width": composed["width"],
        "height": composed["height"],
        "projection": {r["view"]: r["projection"] for r in results},
        "isolate": first["isolate"],
        "applied": first["applied"],
        "framing": next((r["framing"] for r in results if r.get("framing")), ""),
    }, sorted(set(warnings))

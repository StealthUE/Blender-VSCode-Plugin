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
- Solid shading shows cavity and outlines by default, so plain-coloured models read.
- region frames a box or sphere; crop zooms into part of the framed image at full resolution.
- overlay draws other objects or external mesh files (STL, OBJ, 3MF, SVG) over the image as wire,
  x-ray or silhouette, from a second pass. Hidden and wireframe-display objects work too.
- material previews one material on a sphere and floor.

Everything the preview creates is named _vsblender_* and removed again, also after a crash
(purge_leftovers runs when a file loads).
"""
from __future__ import annotations

import math
import os
import tempfile

import bpy
from mathutils import Matrix, Vector

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
MAX_SIDE = 8192
TEMP_PREFIXES = ("_vsblender_preview", "_vsblender_overlay", "_vsblender_check", "_vsblender_ball", "_vsblender_geo")
OVERLAY_STYLES = ("wire", "xray", "silhouette")
MAX_OVERLAYS = 6


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


def _new_preview_scene(src, isolate_objects=None, with_lights=True):
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
        lights = [o for o in src.objects if o.type == "LIGHT" and o not in isolate_objects] if with_lights else []
        for obj in list(isolate_objects) + lights:
            try:
                private.objects.link(obj)
            except RuntimeError:
                pass
    preview.world = src.world
    preview.frame_start = src.frame_start
    preview.frame_end = src.frame_end
    preview.frame_set(src.frame_current)
    return preview, private


class _Temps:
    """Datablocks a preview creates for itself. Removed in reverse order, whatever happens."""

    def __init__(self):
        self.items = []

    def add(self, idb):
        self.items.append(idb)
        return idb

    def clear(self) -> None:
        for idb in reversed(self.items):
            try:
                if isinstance(idb, bpy.types.Object):
                    bpy.data.objects.remove(idb, do_unlink=True)
                elif isinstance(idb, bpy.types.Scene):
                    bpy.data.scenes.remove(idb)
                elif isinstance(idb, bpy.types.Mesh):
                    bpy.data.meshes.remove(idb)
                elif isinstance(idb, bpy.types.Material):
                    bpy.data.materials.remove(idb)
                elif isinstance(idb, bpy.types.Light):
                    bpy.data.lights.remove(idb)
                elif isinstance(idb, bpy.types.Camera):
                    bpy.data.cameras.remove(idb)
                elif isinstance(idb, bpy.types.Collection):
                    bpy.data.collections.remove(idb)
                elif isinstance(idb, bpy.types.World):
                    bpy.data.worlds.remove(idb)
            except (ReferenceError, RuntimeError):
                pass
        self.items = []


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
    """Remove anything a preview, overlay or check image left behind (after a crash mid-render)."""
    def doomed(name: str) -> bool:
        return name.startswith(TEMP_PREFIXES)

    for scene in list(bpy.data.scenes):
        if doomed(scene.name) and len(bpy.data.scenes) > 1:
            try:
                bpy.data.scenes.remove(scene)
            except Exception:
                pass
    for attr in ("objects", "meshes", "curves", "materials", "lights", "cameras", "worlds"):
        coll = getattr(bpy.data, attr)
        for idb in list(coll):
            if not doomed(idb.name):
                continue
            try:
                if attr == "objects":
                    coll.remove(idb, do_unlink=True)
                elif idb.users == 0:
                    coll.remove(idb)
            except Exception:
                pass
    for collection in list(bpy.data.collections):
        if doomed(collection.name) and collection.users == 0:
            try:
                bpy.data.collections.remove(collection)
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
    w, h = max(16, min(MAX_SIDE, w)), max(16, min(MAX_SIDE, h))
    if w * h > MAX_PIXELS:
        raise ValueError(f"{w}x{h} is over the {MAX_PIXELS // 1_000_000} MP preview limit")
    return w, h


# ----------------------------------------------------------------------------- framing helpers
def _region_box(region) -> tuple:
    """region: {center: [x,y,z], radius: r} or {min: [...], max: [...]} in scene units."""
    if not isinstance(region, dict):
        raise ValueError("region must be {center, radius} or {min, max}")
    if "center" in region:
        center = Vector([float(v) for v in region["center"]][:3])
        radius = float(region.get("radius") or 0.0)
        if radius <= 0:
            raise ValueError("region.radius must be positive")
        return center - Vector((radius,) * 3), center + Vector((radius,) * 3)
    if "min" in region and "max" in region:
        low = Vector([float(v) for v in region["min"]][:3])
        high = Vector([float(v) for v in region["max"]][:3])
        return Vector(map(min, low, high)), Vector(map(max, low, high))
    raise ValueError("region must be {center, radius} or {min, max}")


def _parse_crop(crop):
    if crop is None:
        return None
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise ValueError("crop must be [x0, y0, x1, y1] as fractions of the image, (0, 0) top left")
    x0, y0, x1, y1 = (float(v) for v in crop)
    x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        raise ValueError("crop box is too small (under 1% of the image)")
    return x0, y0, x1, y1


def _apply_crop(cam_data, width: int, height: int, crop) -> tuple:
    """Zoom the camera onto crop (fractions of the framed image) and return the new resolution.

    Works on the camera, not the pixels, so the crop is rendered at full resolution. Needs
    sensor_fit AUTO (every generated view; the scene camera when it uses AUTO). Returns None when the
    camera cannot be adjusted, and the caller crops the pixels instead.
    """
    if cam_data.sensor_fit != "AUTO":
        return None
    x0, y0, x1, y1 = crop
    long_side = max(width, height)
    crop_w, crop_h = width * (x1 - x0), height * (y1 - y0)
    k = long_side / max(crop_w, crop_h)
    new_w, new_h = max(16, round(crop_w * k)), max(16, round(crop_h * k))
    while new_w * new_h > MAX_PIXELS or max(new_w, new_h) > MAX_SIDE:
        k *= 0.9
        new_w, new_h = max(16, round(crop_w * k)), max(16, round(crop_h * k))
    new_long = max(new_w, new_h)
    dx = width * (x0 + x1) / 2 - width / 2
    dy = height / 2 - height * (y0 + y1) / 2
    if cam_data.type == "ORTHO":
        scale = cam_data.ortho_scale
        pixel = scale / long_side
        new_scale = pixel / k * new_long
        cam_data.shift_x = (cam_data.shift_x * scale + dx * pixel) / new_scale
        cam_data.shift_y = (cam_data.shift_y * scale + dy * pixel) / new_scale
        cam_data.ortho_scale = new_scale
    else:
        sensor = cam_data.sensor_width
        lens = cam_data.lens
        pixel = sensor / long_side
        off_x = cam_data.shift_x * sensor + dx * pixel
        off_y = cam_data.shift_y * sensor + dy * pixel
        new_lens = lens * k * long_side / new_long
        cam_data.lens = new_lens
        cam_data.shift_x = off_x * new_lens / lens / sensor
        cam_data.shift_y = off_y * new_lens / lens / sensor
    return new_w, new_h


def _pixel_world(cam, cam_data, width: int, height: int, point: Vector) -> float:
    """Size of one pixel in scene units at a point, for line widths that look the same at any zoom."""
    long_side = max(width, height)
    if cam_data.type == "ORTHO":
        return cam_data.ortho_scale / long_side
    distance = max(1e-6, (cam.matrix_world.translation - point).length)
    return distance * (cam_data.sensor_width / cam_data.lens) / long_side


# ----------------------------------------------------------------------------- material ball
def _material_ball(material_name: str, temps: _Temps):
    """A sphere with the material on a grey floor, lit by a sun. Returns (objects, bounds)."""
    import bmesh

    name = material_name[3:] if material_name.startswith("MA:") else material_name
    material = bpy.data.materials.get(name)
    if material is None:
        close = [m.name for m in bpy.data.materials if name.lower() in m.name.lower()][:8]
        raise KeyError(f"no material named {name}" + (f". Did you mean: {', '.join(close)}" if close else ""))
    ball_mesh = temps.add(bpy.data.meshes.new("_vsblender_ball"))
    bm = bmesh.new()
    try:
        bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=32, radius=1.0, calc_uvs=True)
        bm.to_mesh(ball_mesh)
    finally:
        bm.free()
    ball_mesh.shade_smooth()
    ball_mesh.materials.append(material)
    ball = temps.add(bpy.data.objects.new("_vsblender_ball", ball_mesh))
    ball.location = (0.0, 0.0, 1.0)
    floor_mesh = temps.add(bpy.data.meshes.new("_vsblender_ball_floor"))
    floor_mesh.from_pydata([(-4, -4, 0), (4, -4, 0), (4, 4, 0), (-4, 4, 0)], [], [(0, 1, 2, 3)])
    floor_mat = temps.add(bpy.data.materials.new("_vsblender_ball_floor"))
    floor_mat.diffuse_color = (0.35, 0.35, 0.36, 1.0)
    try:
        bsdf = floor_mat.node_tree.nodes.get("Principled BSDF") if floor_mat.node_tree else None
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (0.35, 0.35, 0.36, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.8
    except Exception:
        pass
    floor_mesh.materials.append(floor_mat)
    floor = temps.add(bpy.data.objects.new("_vsblender_ball_floor", floor_mesh))
    sun_data = temps.add(bpy.data.lights.new("_vsblender_ball_sun", "SUN"))
    sun_data.energy = 3.0
    sun_data.angle = math.radians(8.0)
    sun = temps.add(bpy.data.objects.new("_vsblender_ball_sun", sun_data))
    sun.rotation_euler = (math.radians(50.0), 0.0, math.radians(35.0))
    return [ball, floor, sun], (Vector((-1.2, -1.2, -0.05)), Vector((1.2, 1.2, 2.2)))


# ----------------------------------------------------------------------------- overlays
def _overlay_entries(params: dict, root: str | None) -> list:
    entries = params.get("overlay")
    if not entries:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        raise ValueError("overlay must be a list of {objects | file | ref, style, color, opacity}")
    if len(entries) > MAX_OVERLAYS:
        raise ValueError(f"at most {MAX_OVERLAYS} overlays")
    out = []
    refs = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each overlay is an object such as {\"objects\": \"_REF *\", \"style\": \"wire\"}")
        entry = dict(entry)
        if entry.get("ref"):
            if refs is None:
                refs = _reference_set(root)
            ref = refs.get(str(entry["ref"]))
            if ref is None:
                raise KeyError(f"no reference named {entry['ref']} in references.json. Known: {', '.join(refs) or 'none'}")
            entry = {**ref, **{k: v for k, v in entry.items() if k != "ref"}}
        style = str(entry.get("style") or "wire")
        if style not in OVERLAY_STYLES:
            raise ValueError(f"overlay style must be one of {', '.join(OVERLAY_STYLES)}")
        entry["style"] = style
        if not entry.get("objects") and not entry.get("file"):
            raise ValueError("an overlay needs objects (a name, list or find selector), file, or ref")
        out.append(entry)
    return out


def _reference_set(root: str | None) -> dict:
    """References stored next to the sidecar: .blender-ai/<name>/references.json, [{name, file, transform, ...}]."""
    from . import history
    import json

    path = os.path.join(history.sidecar_dir(root or ""), "references.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    items = data.get("references", data) if isinstance(data, dict) else data
    out = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = {k: v for k, v in item.items() if k not in ("name", "notes")}
    return out


def _resolve_file(path: str, root: str | None) -> str:
    full = path if os.path.isabs(path) else os.path.join(root or os.getcwd(), path)
    full = os.path.abspath(full)
    if root and not _under(full, root):
        raise ValueError(f"overlay file must be inside the workspace: {path}")
    if not os.path.isfile(full):
        raise FileNotFoundError(path)
    return full


def _file_mesh(entry: dict, root: str | None, temps: _Temps, name: str):
    """An external file as a temporary object, in scene units. Files are millimetres unless transform.units says so."""
    from . import meshdata, units as units_mod

    path = _resolve_file(str(entry["file"]), root)
    transform = entry.get("transform") or {}
    file_units = str(transform.get("units") or entry.get("units") or "mm")
    arrays = meshdata.read_mesh_file(path, plane=str(transform.get("plane") or "xy"))
    factor = units_mod.file_to_bu(file_units)
    scale = transform.get("scale", 1.0)
    scale = Vector(scale) if isinstance(scale, (list, tuple)) else Vector((float(scale),) * 3)
    rot = [math.radians(float(v)) for v in (transform.get("rotation_deg") or [0, 0, 0])][:3]
    loc = Vector([float(v) for v in (transform.get("location") or [0, 0, 0])][:3])
    from mathutils import Euler
    matrix = Matrix.Translation(loc) @ Euler(rot, "XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*(scale * factor), 1.0))
    mesh = temps.add(meshdata.to_mesh(arrays, name))
    obj = temps.add(bpy.data.objects.new(name, mesh))
    obj.matrix_world = matrix
    return obj


def _object_copies(entry: dict, root: str | None, depsgraph, temps: _Temps, prefix: str) -> list:
    """Evaluated mesh copies of the objects an overlay names, so hidden and wire-display objects draw too."""
    from . import inspect_tools

    found = inspect_tools.resolve_objects(entry["objects"], root or "", include_hidden=True)
    copies = []
    for index, ob in enumerate(found):
        if ob.type not in GEOMETRY or ob.name.startswith("_vsblender"):
            continue
        mesh = None
        try:
            mesh = bpy.data.meshes.new_from_object(ob.evaluated_get(depsgraph), preserve_all_data_layers=False,
                                                   depsgraph=depsgraph)
        except Exception:
            try:
                mesh = bpy.data.meshes.new_from_object(ob)
            except Exception:
                mesh = None
        if mesh is None or not len(mesh.polygons) and not len(mesh.edges):
            if mesh is not None:
                bpy.data.meshes.remove(mesh)
            continue
        mesh.name = f"{prefix}_{index}"
        temps.add(mesh)
        copy = temps.add(bpy.data.objects.new(f"{prefix}_{index}", mesh))
        copy.matrix_world = ob.matrix_world.copy()
        copies.append(copy)
    if not copies:
        raise ValueError(f"overlay objects {entry['objects']!r} matched no geometry")
    return copies


def _overlay_pass(entry: dict, index: int, src, cam, cam_data, width: int, height: int, out_base: str,
                  root: str | None, temps: _Temps) -> str:
    """Render the overlay geometry alone (flat colour, transparent background) from the preview camera."""
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None
    prefix = f"_vsblender_overlay_{index}"
    if entry.get("file"):
        objects = [_file_mesh(entry, root, temps, prefix)]
    else:
        objects = _object_copies(entry, root, depsgraph, temps, prefix)
    scene = temps.add(bpy.data.scenes.new(f"{prefix}_scene"))
    for obj in objects:
        scene.collection.objects.link(obj)
    scene.collection.objects.link(cam)
    scene.camera = cam
    if entry["style"] == "wire":
        for obj in objects:
            mesh = obj.data
            if len(mesh.polygons) > 20000:
                import bmesh
                bm = bmesh.new()
                try:
                    bm.from_mesh(mesh)
                    bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(5.0), verts=bm.verts, edges=bm.edges)
                    bm.to_mesh(mesh)
                finally:
                    bm.free()
            center = obj.matrix_world @ (sum((Vector(c) for c in obj.bound_box), Vector()) / 8.0)
            scale = max(1e-9, max(abs(v) for v in obj.matrix_world.to_scale()))
            px = float(entry.get("width_px") or 1.6)
            mod = obj.modifiers.new("_vsblender_wire", "WIREFRAME")
            mod.thickness = _pixel_world(cam, cam_data, width, height, center) * px / scale
            mod.use_replace = True
            mod.use_even_offset = False
            mod.use_relative_offset = False
    _set_engine(scene, ["BLENDER_WORKBENCH"])
    shading = scene.display.shading
    shading.light = "FLAT"
    shading.color_type = "SINGLE"
    shading.single_color = sheet.parse_color(entry.get("color"))
    shading.show_cavity = False
    shading.show_object_outline = False
    shading.show_shadows = False
    shading.show_xray = False
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_compositing = False
    path = f"{out_base}.overlay{index}.png"
    scene.render.filepath = path
    scene.frame_set(src.frame_current)
    _render_still(scene)
    try:
        scene.collection.objects.unlink(cam)
    except Exception:
        pass
    return path


def _apply_overlays(out: str, entries: list, src, cam, cam_data, width: int, height: int, root: str | None,
                    temps: _Temps) -> list:
    base = sheet.load(out)
    notes = []
    stem = os.path.splitext(os.path.abspath(out))[0]
    for index, entry in enumerate(entries):
        layer_path = _overlay_pass(entry, index, src, cam, cam_data, width, height, stem, root, temps)
        try:
            layer = sheet.load(layer_path)
        finally:
            try:
                os.remove(layer_path)
            except OSError:
                pass
        style = entry["style"]
        opacity = entry.get("opacity")
        opacity = float(opacity) if opacity is not None else (0.35 if style == "xray" else 1.0)
        base = sheet.overlay(base, layer, sheet.parse_color(entry.get("color")), style=style, opacity=opacity,
                             width=int(entry.get("width_px") or 2))
        what = entry.get("file") or entry.get("objects")
        notes.append(f"{style} {what}")
    sheet.save(out, base)
    return notes


# ----------------------------------------------------------------------------- render
def _solid_detail(scene, params: dict) -> list:
    """Workbench settings that make relief and edges readable: cavity, outline, shadow, matcap, x-ray."""
    shading = scene.display.shading
    applied = []
    try:
        shading.light = "STUDIO"
        color_type = str(params.get("color_type") or "MATERIAL").upper()
        shading.color_type = color_type if color_type in {"MATERIAL", "OBJECT", "RANDOM", "SINGLE", "VERTEX", "TEXTURE"} else "MATERIAL"
    except Exception:
        pass
    cavity = params.get("cavity", True)
    outline = params.get("outline", True)
    shadow = params.get("shadow", False)
    try:
        shading.show_cavity = bool(cavity)
        if cavity:
            shading.cavity_type = "BOTH"
            applied.append("cavity")
        shading.show_object_outline = bool(outline)
        if outline:
            applied.append("outline")
        shading.show_shadows = bool(shadow)
        if shadow:
            applied.append("shadow")
        if params.get("xray"):
            shading.show_xray = True
            shading.xray_alpha = float(params.get("xray_alpha") or 0.5)
            applied.append("x-ray")
    except Exception:
        pass
    matcap = params.get("matcap")
    if matcap:
        try:
            shading.light = "MATCAP"
            shading.studio_light = str(matcap)
            applied.append(f"matcap {matcap}")
        except Exception:
            names = [s.name for s in bpy.context.preferences.studio_lights if s.type == "MATCAP"]
            raise ValueError(f"unknown matcap {matcap}. Available: {', '.join(names[:30])}")
    return applied


def render_view(params: dict, out: str, objects=None, root: str | None = None):
    """Render one view to out. Returns (result, warnings).

    objects: render exactly these objects (isolated, no scene lights), e.g. temporary check meshes.
    """
    view = str(params.get("view") or "iso")
    material_name = params.get("material")
    shading = str(params.get("shading") or ("material" if material_name else "solid"))
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
    if params.get("aspect") is None and material_name:
        params = dict(params, aspect="square")
    crop = _parse_crop(params.get("crop"))
    region = _region_box(params["region"]) if params.get("region") else None
    overlays = _overlay_entries(params, root)
    frame = params.get("frame")
    width, height = _resolution(params, src, view)
    saved_frame = src.frame_current
    warnings = []
    applied = []
    framing_note = ""
    temps = _Temps()
    scene = cam = cam_data = private = None
    fixed_box = None
    explicit = objects is not None
    try:
        if material_name:
            objects, fixed_box = _material_ball(str(material_name), temps)
            explicit = True
        isolate = explicit or (bool(params.get("isolate")) and bool(target_name))
        if not explicit:
            objects = _collect(src, target_name)
        scene, private = _new_preview_scene(src, objects if isolate else None, with_lights=not explicit)
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
        if view == "camera" and not explicit:
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
            if region is not None:
                warnings.append("region is ignored for view camera; use crop to zoom")
        else:
            if view == "camera":
                view = "iso"
                projection = params.get("projection") or "persp"
            cam_data = bpy.data.cameras.new("_vsblender_preview_cam")
            cam = bpy.data.objects.new("_vsblender_preview_cam", cam_data)
            private.objects.link(cam)
            box = params.get("bounds")
            subject = None
            if region is not None:
                low, high = region
                framing_note = "framed on the region"
            elif fixed_box is not None:
                low, high = fixed_box
            elif box:  # Set by the live ingest and the check image: frames exactly this box.
                low, high = Vector(box[0]), Vector(box[1])
            elif not target_name and not explicit and params.get("framing") != "all":
                subject = subject_bounds(objects)
            if region is None and fixed_box is None and not box:
                if subject is not None:
                    low, high, framing_note = subject
                else:
                    low, high = bounds(objects)
            center = (low + high) * 0.5
            extent = high - low
            # Only empties or lights: no size to frame, so show one scene unit around them.
            radius = extent.length * 0.5 if extent.length > 1e-9 else 1.0
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
                cam_data.ortho_scale = max(scale, radius * 0.05, 1e-4) * 1.12
                distance = radius * 3.0 + radius * 0.5
            else:
                lens = 50.0
                # The lens angle covers the longer side; the bounding sphere has to fit the shorter one.
                half = math.atan(36.0 / (2.0 * lens))
                half = math.atan(math.tan(half) * min(aspect, 1.0 / aspect))
                distance = radius / math.sin(half) * 1.05
                cam_data.lens = lens
            _look_at(cam, center + direction * distance, center, view)
            # Clipping scales with the subject, so a 20 mm part in a millimetre scene is not clipped.
            cam_data.clip_start = max(distance * 1e-4, 1e-6)
            cam_data.clip_end = distance * 4.0 + radius * 4.0
        scene.camera = cam
        pixel_crop = None
        if crop is not None:
            resized = _apply_crop(cam_data, width, height, crop)
            if resized is None:
                pixel_crop = crop
                warnings.append("the scene camera does not use sensor fit Auto, so crop cuts the rendered pixels (lower resolution)")
            else:
                width, height = resized
            applied.append("crop")

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
        if material_name and not samples:
            samples = 8
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
        if engine == "BLENDER_WORKBENCH":
            applied += _solid_detail(scene, params)
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
        overlay_notes = []
        if overlays:
            overlay_notes = _apply_overlays(scene.render.filepath, overlays, src, cam, cam_data, width, height, root, temps)
            applied.append("overlay")
        if pixel_crop is not None:
            sheet.save(scene.render.filepath, sheet.crop_pixels(sheet.load(scene.render.filepath), pixel_crop))
            width = round(width * (pixel_crop[2] - pixel_crop[0]))
            height = round(height * (pixel_crop[3] - pixel_crop[1]))
        result = {
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
        }
        if overlay_notes:
            result["overlays"] = overlay_notes
        if material_name:
            result["material"] = str(material_name)
        return result, warnings
    finally:
        _cleanup(scene, cam, cam_data, private)
        temps.clear()
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
    root = roots[0] if roots else None
    max_bytes = int(params.get("max_bytes") or 0)
    views = params.get("views")
    if not views:
        result, warnings = render_view(params, out, root=root)
        if max_bytes:
            result["file"], note = sheet.fit(result["file"], max_bytes)
            if note:
                result["note"] = note
        return result, warnings
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
            result, more = render_view(single, part, root=root)
            parts.append(part)
            results.append(result)
            warnings.extend(more)
            label = view
            if params.get("target"):
                label += f" - {params['target']}"
            labels.append(label)
        cell = max(results[0]["height"], 64)
        composed = sheet.compose_files(parts, out, labels=labels, cell=min(cell, 768), max_bytes=max_bytes or None)
    finally:
        for part in parts:
            try:
                os.remove(part)
            except OSError:
                pass
    first = results[0]
    out_result = {
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
    }
    if composed.get("note"):
        out_result["note"] = composed["note"]
    if first.get("overlays"):
        out_result["overlays"] = first["overlays"]
    return out_result, sorted(set(warnings))

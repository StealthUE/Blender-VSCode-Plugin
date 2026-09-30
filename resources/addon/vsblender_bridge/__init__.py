"""VSBlender bridge.

Listens on 127.0.0.1 for one JSON object per connection. bpy is only touched on
Blender's main thread: the GUI path queues work onto a timer, and the headless
path accepts connections on the main thread.
"""
from __future__ import annotations

import io
import json
import math
import os
import queue
import socket
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout

import bpy
from bpy.app.handlers import persistent
from mathutils import Vector

# Legacy add-ons (scripts/addons) are skipped by Preferences without bl_info.
# Extensions read blender_manifest.toml instead and ignore this.
bl_info = {
    "name": "VSBlender Bridge",
    "author": "Massive Dynamic Engineering",
    "version": (0, 1, 0),
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar > VSBlender",
    "description": "Local bridge for the VSBlender VS Code extension",
    "category": "Development",
}

ADDON_VERSION = "0.1.0"
# Answered on the socket thread. Everything else runs on Blender's main thread.
_NO_MAIN_THREAD = {"ping"}
# How long a connection waits for the main thread. The caller's own timeout is usually shorter.
_MAIN_THREAD_WAIT = 600
_PREFIX = {
    "objects": "OB",
    "meshes": "ME",
    "materials": "MA",
    "lights": "LI",
    "cameras": "CA",
    "collections": "CO",
    "images": "IM",
    "worlds": "WO",
    "node_groups": "NT",
    "texts": "TX",
}
_VIEWS = {
    "front": Vector((0.0, -1.0, 0.0)),
    "back": Vector((0.0, 1.0, 0.0)),
    "left": Vector((-1.0, 0.0, 0.0)),
    "right": Vector((1.0, 0.0, 0.0)),
    "top": Vector((0.0, 0.0, 1.0)),
    "bottom": Vector((0.0, 0.0, -1.0)),
    "iso": Vector((1.0, -1.0, 1.0)),
}
_state = {
    "registered": False,
    "running": False,
    "blocking": False,
    "sock": None,
    "thread": None,
    "queue": queue.Queue(),
    "error": "",
    "port": 0,
    "busy": "",
}


def port() -> int:
    env = os.environ.get("VSBLENDER_PORT", "")
    if env.isdigit():
        value = int(env)
        if 1024 <= value <= 65535:
            return value
    try:
        addon = bpy.context.preferences.addons.get(__name__)
        if addon is not None:
            return int(addon.preferences.port)
    except Exception:
        pass
    return 47876


def workspace_root() -> str:
    env = os.environ.get("VSBLENDER_WORKSPACE", "")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    start = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else os.getcwd()
    current = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(current, ".blender-ai", "config.json")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start)
        current = parent


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


def _jsonable(value, depth: int = 0):
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # NaN and inf are not JSON; the Node side would fail to parse the whole reply.
        return round(value, 6) if math.isfinite(value) else str(value)
    if isinstance(value, bpy.types.ID):
        return value.name
    if hasattr(value, "to_list"):
        try:
            return [_jsonable(item, depth + 1) for item in value.to_list()]
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth + 1) for item in value]
    return str(value)


def _fingerprint() -> dict:
    found = {}
    for attr, prefix in _PREFIX.items():
        collection = getattr(bpy.data, attr, None)
        if collection is None:
            continue
        for item in collection:
            if item.name.startswith("_vsblender"):
                continue
            key = f"{prefix}:{item.name}"
            if attr == "objects":
                found[key] = (
                    tuple(round(v, 5) for v in item.location),
                    tuple(round(v, 5) for v in item.rotation_euler),
                    tuple(round(v, 5) for v in item.scale),
                    bool(item.hide_viewport),
                    bool(item.hide_render),
                    tuple((slot.material.name if slot.material else "") for slot in item.material_slots),
                )
            else:
                found[key] = item.name
    return found


def _changed(before: dict, after: dict) -> list:
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


def session_info() -> dict:
    scene = _scene()
    info = {
        "product": "vsblender",
        "addon_version": ADDON_VERSION,
        "blender_version": bpy.app.version_string,
        "blender_version_tuple": list(bpy.app.version),
        "file": bpy.data.filepath,
        "dirty": bool(bpy.data.is_dirty),
        "port": port(),
        "workspace": workspace_root(),
        "objects": len(bpy.data.objects),
        "materials": len(bpy.data.materials),
    }
    if scene is not None:
        unit = scene.unit_settings
        info.update({
            "scene": scene.name,
            "frame": scene.frame_current,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": scene.render.fps,
            "engine": scene.render.engine,
            "unit_system": unit.system,
            "unit_scale": unit.scale_length,
            "length_unit": unit.length_unit,
        })
        cycles = getattr(scene, "cycles", None)
        if cycles is not None and hasattr(cycles, "device"):
            info["render_device"] = cycles.device
    try:
        info["mode"] = bpy.context.mode
    except Exception:
        info["mode"] = None
    return info


def open_file(params: dict) -> dict:
    raw = params.get("path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("path is required")
    target = os.path.abspath(raw)
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    current = os.path.abspath(bpy.data.filepath) if bpy.data.filepath else ""
    if current and os.path.normcase(current) == os.path.normcase(target):
        return {"opened": True, "already": True, "file": bpy.data.filepath, "dirty": bool(bpy.data.is_dirty)}
    if bpy.data.is_dirty:
        return {"opened": False, "reason": "unsaved changes", "file": bpy.data.filepath}
    bpy.ops.wm.open_mainfile(filepath=target)
    return {"opened": True, "file": bpy.data.filepath, "dirty": bool(bpy.data.is_dirty)}


class ScriptError(Exception):
    """The user's script failed. The message is already formatted for the caller."""

    def __init__(self, message: str, changed: list):
        super().__init__(message)
        self.changed = changed


def _script_traceback(exc: BaseException, script: str) -> str:
    """Start the traceback at the user's file, so the first frame is their file:line, not the bridge's."""
    tb = exc.__traceback__
    wanted = os.path.normcase(script)
    while tb is not None and os.path.normcase(tb.tb_frame.f_code.co_filename) != wanted:
        tb = tb.tb_next
    return "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))


def _undo_push(message: str) -> None:
    """One undo step per script, so Ctrl+Z in Blender reverts the AI's change as a unit."""
    if bpy.app.background:
        return
    try:
        windows = bpy.context.window_manager.windows
        if not windows:
            return
        with bpy.context.temp_override(window=windows[0]):
            bpy.ops.ed.undo_push(message=message)
    except Exception:
        pass


def run_script(params: dict):
    raw = params.get("path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("path is required")
    script = os.path.abspath(raw)
    root = workspace_root()
    if not _under(script, root):
        raise ValueError(f"path is outside the workspace: {script}")
    if not script.lower().endswith(".py"):
        raise ValueError("path must be a .py file")
    if not os.path.isfile(script):
        raise FileNotFoundError(script)
    function = params.get("function")
    args = params.get("args") or {}
    if function is not None and not isinstance(function, str):
        raise ValueError("function must be a string")
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    with open(script, encoding="utf-8") as handle:
        source = handle.read()
    try:
        code = compile(source, script, "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"{exc.filename}:{exc.lineno}: {exc.msg}") from exc

    before = _fingerprint()
    stdout = io.StringIO()
    stderr = io.StringIO()
    namespace = {"__name__": "__main__", "__file__": script, "bpy": bpy}
    try:
        import mathutils
        namespace["mathutils"] = mathutils
    except Exception:
        pass
    previous = os.getcwd()
    warnings = []
    value = None
    failure = ""
    try:
        os.chdir(root)
        import warnings as warnings_mod
        with warnings_mod.catch_warnings(record=True) as caught:
            warnings_mod.simplefilter("always")
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    exec(code, namespace)
                    if function:
                        fn = namespace.get(function)
                        if not callable(fn):
                            raise RuntimeError(f"{function} is not defined in {script}")
                        value = fn(**args)
                    else:
                        value = namespace.get("result")
                except (Exception, SystemExit) as exc:
                    # SystemExit too: sys.exit() in a script must not reach Blender's timer loop.
                    failure = _script_traceback(exc, script)
        warnings = [f"{item.category.__name__}: {item.message}" for item in caught][:40]
    finally:
        os.chdir(previous)
    changed = _changed(before, _fingerprint())
    if changed:
        _undo_push(f"VSBlender: {os.path.basename(script)}")
    text_out = stdout.getvalue()
    text_err = stderr.getvalue()
    if len(text_out) > 200000:
        text_out = text_out[:200000] + "\n… stdout truncated"
    if len(text_err) > 200000:
        text_err = text_err[:200000] + "\n… stderr truncated"
    if failure:
        parts = [failure.rstrip()]
        if text_out.strip():
            parts.append("--- stdout before the error ---\n" + text_out.rstrip())
        if text_err.strip():
            parts.append("--- stderr ---\n" + text_err.rstrip())
        if changed:
            parts.append("changed before the error: " + ", ".join(changed[:60]))
        raise ScriptError("\n".join(parts), changed)
    return {
        "file": script,
        "result": _jsonable(value),
        "stdout": text_out,
        "stderr": text_err,
    }, warnings, changed


def _set_engine(scene, names: list) -> str:
    last = None
    for name in names:
        try:
            scene.render.engine = name
            return scene.render.engine
        except Exception as exc:
            last = exc
    raise RuntimeError(f"no usable render engine ({last})")


_GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GPENCIL", "GREASEPENCIL"}


def _bounds(objects) -> tuple:
    """World bounds of the geometry, read from evaluated objects.

    An object made since the last depsgraph update (for example by the script that just ran)
    still has an empty bound_box on the original, which put the camera inside it. Lights,
    cameras and empties only count when there is no geometry at all.
    """
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None
    usable = [obj for obj in objects if obj is not None and not obj.name.startswith("_vsblender")]
    shaped = [obj for obj in usable if obj.type in _GEOMETRY]
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
        corners = []
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


def _look_at(cam, location, target, view: str) -> None:
    """Aim a camera: it looks down its local -Z with local Y up.

    Straight down or up there is no single "up", so those two are set explicitly
    (+Y world at the top of the image for the top view).
    """
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


def _collect_objects(src, target_name: str | None):
    if target_name:
        obj = src.objects.get(target_name) or bpy.data.objects.get(target_name)
        if obj is None:
            raise KeyError(f"no object named {target_name}")
        children = list(getattr(obj, "children_recursive", []))
        return [obj, *children]
    return [obj for obj in src.objects if obj.type != "CAMERA" and not obj.hide_render and not obj.name.startswith("_vsblender")]


def _scene_contains(scene, obj) -> bool:
    if obj.name in scene.collection.objects:
        return True
    try:
        children = scene.collection.children_recursive
    except Exception:
        children = scene.collection.children
    for child in children:
        if obj.name in child.objects:
            return True
    return False


def _new_preview_scene(src):
    """New scene only. Never scene.copy(): a shared master collection must not be deleted with the preview."""
    preview = bpy.data.scenes.new("_vsblender_preview")
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
    preview.world = src.world
    preview.frame_start = src.frame_start
    preview.frame_end = src.frame_end
    preview.frame_set(src.frame_current)
    return preview


def _cleanup_preview(scene, cam, cam_data, private, linked) -> None:
    for obj in linked:
        try:
            private.objects.unlink(obj)
        except Exception:
            pass
    try:
        if private is not None and scene is not None:
            scene.collection.children.unlink(private)
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
    if private is not None:
        try:
            if private.users == 0:
                bpy.data.collections.remove(private)
        except Exception:
            pass
    if scene is not None:
        try:
            bpy.data.scenes.remove(scene)
        except Exception:
            pass


def _render_still(scene) -> None:
    """Render one scene without changing the user's scene.

    context.temp_override exists in Blender 3.2 and later. Passing a context
    dict on 2.92 reaches the render, then the dependency graph crashes while
    two scenes share the same objects, so that path is not used.
    """
    override = getattr(bpy.context, "temp_override", None)
    if override is None:
        raise RuntimeError("offscreen preview needs Blender 3.2 or newer")
    with bpy.context.temp_override(scene=scene):
        bpy.ops.render.render(write_still=True)


def preview(params: dict):
    view = str(params.get("view") or "iso")
    shading = str(params.get("shading") or "solid")
    if view != "camera" and view not in _VIEWS:
        raise ValueError("view must be camera, front, back, left, right, top, bottom, or iso")
    if shading not in {"solid", "material", "rendered"}:
        raise ValueError("shading must be solid, material, or rendered")
    try:
        size = int(params.get("size") or 512)
    except (TypeError, ValueError) as exc:
        raise ValueError("size must be an integer") from exc
    size = max(64, min(2048, size))
    out = params.get("out")
    if not isinstance(out, str) or not out:
        raise ValueError("out path is required")
    import tempfile
    roots = [tempfile.gettempdir(), workspace_root()]
    if bpy.data.filepath:
        roots.append(os.path.dirname(bpy.data.filepath))
    if not any(root and _under(out, root) for root in roots):
        raise ValueError("refusing to write the preview outside the workspace or temp directory")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    src = _scene()
    if src is None:
        raise RuntimeError("no scene")
    saved_frame = src.frame_current
    target_name = params.get("target")
    if target_name is not None and not isinstance(target_name, str):
        raise ValueError("target must be a string")
    frame = params.get("frame")
    warnings = []
    scene = cam = cam_data = private = None
    linked = []
    try:
        scene = _new_preview_scene(src)
        if frame is not None:
            scene.frame_set(int(frame))
        try:
            scene.view_layers[0].update()
        except Exception:
            pass
        objects = _collect_objects(src, target_name)
        cam_data = bpy.data.cameras.new("_vsblender_preview_cam")
        cam = bpy.data.objects.new("_vsblender_preview_cam", cam_data)
        private = bpy.data.collections.new("_vsblender_preview")
        private.objects.link(cam)
        scene.collection.children.link(private)
        scene.camera = cam
        if target_name and objects:
            obj = objects[0]
            if not _scene_contains(scene, obj):
                try:
                    private.objects.link(obj)
                    linked.append(obj)
                except RuntimeError:
                    pass
        if view == "camera":
            src_cam = src.camera
            if src_cam is None:
                raise RuntimeError("this file has no camera; use view iso or front")
            cam.matrix_world = src_cam.matrix_world.copy()
            if src_cam.type == "CAMERA":
                cam_data.lens = src_cam.data.lens
                cam_data.clip_start = src_cam.data.clip_start
                cam_data.clip_end = src_cam.data.clip_end
        else:
            low, high = _bounds(objects)
            center = (low + high) * 0.5
            extent = max((high - low).length, 0.5)
            lens = 50.0
            sensor = 36.0
            fov = 2.0 * math.atan(sensor / (2.0 * lens))
            distance = max(0.5, (extent * 0.5) / math.tan(fov * 0.5) * 1.35)
            direction = _VIEWS[view].normalized()
            _look_at(cam, center + direction * distance, center, view)
            cam_data.lens = lens
            cam_data.clip_start = max(0.01, distance / 100.0)
            cam_data.clip_end = max(1000.0, distance * 20.0)

        if shading == "solid":
            wanted = ["BLENDER_WORKBENCH"]
        elif shading == "material":
            wanted = ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"]
        else:
            wanted = [src.render.engine, "BLENDER_EEVEE", "CYCLES", "BLENDER_WORKBENCH"]
        engine = _set_engine(scene, wanted)
        if shading != "solid" and engine == "BLENDER_WORKBENCH":
            warnings.append(f"{shading} preview fell back to Workbench")
        eevee = getattr(scene, "eevee", None)
        if eevee is not None and hasattr(eevee, "taa_render_samples"):
            eevee.taa_render_samples = min(int(getattr(eevee, "taa_render_samples", 16) or 16), 32)
        cycles = getattr(scene, "cycles", None)
        if cycles is not None and hasattr(cycles, "samples"):
            cycles.samples = min(int(cycles.samples or 16), 32)
        display = getattr(scene, "display", None)
        shading_settings = getattr(display, "shading", None) if display else None
        if shading_settings is not None:
            try:
                shading_settings.light = "STUDIO"
                shading_settings.color_type = "MATERIAL"
            except Exception:
                pass
        scene.render.resolution_x = size
        scene.render.resolution_y = size
        scene.render.resolution_percentage = 100
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
            "size": size,
        }, warnings
    finally:
        _cleanup_preview(scene, cam, cam_data, private, linked)
        # Objects are shared with the preview scene, so evaluating another frame there
        # wrote animated values onto them. Re-evaluate the user's frame to put them back.
        if frame is not None:
            try:
                src.frame_set(saved_frame)
            except Exception:
                pass


def dispatch(req: dict) -> dict:
    started = time.time()
    method = req.get("method")
    params = req.get("params") or {}
    ident = req.get("id")
    if not isinstance(params, dict):
        return {"id": ident, "ok": False, "error": "params must be an object"}
    warnings = []
    changed = []
    try:
        if method == "ping":
            # No bpy here: ping runs on the socket thread so it answers while Blender is busy.
            result = {"product": "vsblender", "version": ADDON_VERSION, "port": _state["port"], "busy": _state["busy"]}
        elif method == "session_info":
            result = session_info()
        elif method == "open_file":
            result = open_file(params)
        elif method == "run_script":
            result, warnings, changed = run_script(params)
        elif method == "preview":
            result, warnings = preview(params)
        else:
            return {"id": ident, "ok": False, "error": f"unknown method {method}"}
        return {
            "id": ident,
            "ok": True,
            "result": result,
            "warnings": warnings,
            "changed": changed,
            "ms": int((time.time() - started) * 1000),
        }
    except ScriptError as exc:
        return {"id": ident, "ok": False, "error": str(exc), "changed": exc.changed, "ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return {"id": ident, "ok": False, "error": detail, "ms": int((time.time() - started) * 1000)}


def _read_line(conn: socket.socket, limit: int = 2_000_000) -> str:
    conn.settimeout(60)
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("request too large")
    return data.split(b"\n", 1)[0].decode("utf-8")


def _send(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _handle_conn(conn: socket.socket, direct: bool) -> None:
    try:
        line = _read_line(conn)
        if not line.strip():
            return
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        if direct or req.get("method") in _NO_MAIN_THREAD:
            _send(conn, dispatch(req))
            return
        done = threading.Event()
        holder = {}

        def job():
            _state["busy"] = str(req.get("method") or "")
            try:
                holder["resp"] = dispatch(req)
            except Exception as exc:
                holder["resp"] = {"id": req.get("id"), "ok": False, "error": str(exc)}
            finally:
                _state["busy"] = ""
            done.set()

        _state["queue"].put(job)
        if not done.wait(_MAIN_THREAD_WAIT):
            _send(conn, {"id": req.get("id"), "ok": False, "error": f"Blender did not answer within {_MAIN_THREAD_WAIT}s"})
            return
        _send(conn, holder.get("resp") or {"ok": False, "error": "empty response"})
    except Exception as exc:
        try:
            _send(conn, {"ok": False, "error": str(exc)})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _drain():
    try:
        while True:
            job = _state["queue"].get_nowait()
            try:
                job()
            except Exception:
                traceback.print_exc()
    except queue.Empty:
        pass
    return 0.05 if _state["running"] and not _state["blocking"] else None


def _bind() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # On Windows SO_REUSEADDR lets a second Blender bind a port that is already
            # listening, and the two then split the requests. Exclusive use makes that bind fail.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound = port()
        sock.bind(("127.0.0.1", bound))
        sock.listen(8)
    except OSError:
        sock.close()
        raise
    _state["port"] = bound
    return sock


def _purge_leftovers() -> None:
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


def ensure_server() -> None:
    if bpy.app.background or _state["blocking"]:
        return
    if _state["running"] and _state["thread"] is not None and _state["thread"].is_alive():
        return
    stop_server()
    try:
        sock = _bind()
    except OSError as exc:
        _state["error"] = f"port {port()} is in use ({exc})"
        return
    _state["sock"] = sock
    _state["running"] = True
    _state["error"] = ""

    def loop():
        sock.settimeout(0.5)
        while _state["running"]:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_handle_conn, args=(conn, False), daemon=True).start()

    thread = threading.Thread(target=loop, name="vsblender-bridge", daemon=True)
    _state["thread"] = thread
    thread.start()
    if not bpy.app.timers.is_registered(_drain):
        bpy.app.timers.register(_drain, first_interval=0.05, persistent=True)


def serve_blocking() -> None:
    """Headless Blender has no timer loop, so accept on the main thread."""
    stop_server()
    _purge_leftovers()
    sock = _bind()
    _state["sock"] = sock
    _state["running"] = True
    _state["blocking"] = True
    _state["error"] = ""
    sock.settimeout(0.5)
    try:
        while _state["running"]:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            _handle_conn(conn, True)
    finally:
        _state["blocking"] = False
        _state["running"] = False


def stop_server() -> None:
    _state["running"] = False
    sock = _state.get("sock")
    _state["sock"] = None
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    _state["thread"] = None


def server_running() -> bool:
    if _state["blocking"]:
        return True
    thread = _state.get("thread")
    return bool(_state["running"] and thread is not None and thread.is_alive())


@persistent
def _load_post(_dummy):
    # A preview interrupted by a crash can be saved into the file; clear it when the file loads.
    _purge_leftovers()
    ensure_server()


def _purge_once():
    _purge_leftovers()
    return None


class VSBlenderPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    port: bpy.props.IntProperty(name="Port", default=47876, min=1024, max=65535)

    def draw(self, _context):
        self.layout.prop(self, "port")


class VSBLENDER_OT_toggle(bpy.types.Operator):
    bl_idname = "vsblender.toggle"
    bl_label = "Start / Stop VSBlender Bridge"

    def execute(self, _context):
        if server_running():
            stop_server()
        else:
            ensure_server()
        return {"FINISHED"}


class VSBLENDER_PT_panel(bpy.types.Panel):
    bl_label = "VSBlender"
    bl_idname = "VSBLENDER_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VSBlender"

    def draw(self, _context):
        layout = self.layout
        if server_running():
            layout.label(text=f"Listening on {port()}")
        elif _state["error"]:
            layout.label(text=_state["error"])
        else:
            layout.label(text="Stopped")
        layout.operator("vsblender.toggle", text="Stop" if server_running() else "Start")


_CLASSES = (VSBlenderPreferences, VSBLENDER_OT_toggle, VSBLENDER_PT_panel)


def register():
    if not _state["registered"]:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
        if _load_post not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_load_post)
        _state["registered"] = True
        # bpy.data is off limits while an add-on registers (at startup and when enabled from
        # Preferences), so the cleanup waits for the first timer tick.
        if not bpy.app.background and not bpy.app.timers.is_registered(_purge_once):
            bpy.app.timers.register(_purge_once, first_interval=0.1)
    ensure_server()


def unregister():
    stop_server()
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    _state["registered"] = False

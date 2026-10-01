"""VSBlender bridge.

Listens on 127.0.0.1 for one JSON object per connection. bpy is only touched on
Blender's main thread: the GUI path queues work onto a timer, and the headless
path accepts connections on the main thread. ping and cancel are answered on the
socket thread, so they work while a script runs.
"""
from __future__ import annotations

import hashlib
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

from . import blender_ingest, helpers, history, inspect_tools, preview as preview_mod, sheet, snapshot

# Legacy add-ons (scripts/addons) are skipped by Preferences without bl_info.
# Extensions read blender_manifest.toml instead and ignore this.
bl_info = {
    "name": "VSBlender Bridge",
    "author": "Massive Dynamic Engineering",
    "version": (0, 2, 0),
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar > VSBlender",
    "description": "Local bridge for the VSBlender VS Code extension",
    "category": "Development",
}

ADDON_VERSION = "0.2.0"
# Answered on the socket thread. Everything else runs on Blender's main thread.
_NO_MAIN_THREAD = {"ping", "cancel"}
# How long a connection waits for the main thread. The caller's own timeout is usually shorter.
_MAIN_THREAD_WAIT = 900
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
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth + 1) for item in value]
    return str(value)


# ----------------------------------------------------------------------------- session
def _devices(scene) -> dict:
    """Which device each engine renders on. EEVEE and Workbench always use the GPU."""
    out = {"engine": scene.render.engine, "eevee": "GPU (always)", "workbench": "GPU (always)"}
    cycles = getattr(scene, "cycles", None)
    if cycles is not None and hasattr(cycles, "device"):
        device = cycles.device
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            backend = prefs.compute_device_type
            if device == "GPU":
                device = f"GPU ({backend})" if backend and backend != "NONE" else "GPU requested, but no GPU backend is enabled in Preferences, so CPU"
        except Exception:
            pass
        out["cycles"] = device
    return out


def session_info() -> dict:
    scene = _scene()
    root = workspace_root()
    info = {
        "product": "vsblender",
        "addon_version": ADDON_VERSION,
        "blender_version": bpy.app.version_string,
        "blender_version_tuple": list(bpy.app.version),
        "file": bpy.data.filepath,
        "dirty": bool(bpy.data.is_dirty),
        "background": bool(bpy.app.background),
        "port": port(),
        "workspace": root,
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
            "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage],
            "camera": scene.camera.name if scene.camera else None,
            "unit_system": unit.system,
            "unit_scale": unit.scale_length,
            "length_unit": unit.length_unit,
            "render_devices": _devices(scene),
        })
    try:
        info["mode"] = bpy.context.mode
    except Exception:
        info["mode"] = None
    try:
        info["sidecar"] = history.sidecar_status(root)
    except Exception as exc:
        info["sidecar"] = {"status": "unknown", "detail": str(exc)}
    try:
        recent = history.list_checkpoints(root)[-5:]
        info["checkpoints"] = [{k: e.get(k) for k in ("id", "label", "time", "auto")} for e in recent]
    except Exception:
        info["checkpoints"] = []
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


# ----------------------------------------------------------------------------- run_script
class ScriptError(Exception):
    """The user's script failed. The message is already formatted for the caller."""

    def __init__(self, message: str, changed: list, changes: dict | None = None):
        super().__init__(message)
        self.changed = changed
        self.changes = changes or {}


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


def _prepare_imports(root: str, lib_paths: list) -> list:
    """Put the workspace helper folders on sys.path and drop workspace modules imported by earlier
    calls, so an edited helper is read again. The add-on's own modules are never dropped."""
    own = os.path.dirname(os.path.abspath(__file__))
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path or name == "vsblender" or name.startswith(__name__):
            continue
        if _under(path, root) and not _under(path, own):
            del sys.modules[name]
    added = []
    for entry in lib_paths:
        folder = entry if os.path.isabs(entry) else os.path.join(root, entry)
        if os.path.isdir(folder) and _under(folder, root) and folder not in sys.path:
            sys.path.insert(0, folder)
            added.append(folder)
    sys.modules["vsblender"] = helpers
    return added


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
    reason = str(params.get("reason") or "").strip()
    cp_opts = params.get("checkpoint") if isinstance(params.get("checkpoint"), dict) else {}
    lib_paths = params.get("lib_paths") or ["scripts/lib"]
    with open(script, encoding="utf-8") as handle:
        source = handle.read()
    try:
        code = compile(source, script, "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"{exc.filename}:{exc.lineno}: {exc.msg}") from exc
    rel_script = history.rel(root, script)
    script_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()

    started = time.time()
    before = snapshot.take()
    checkpoint_entry = None
    if cp_opts.get("auto", True):
        try:
            checkpoint_entry = history.checkpoint(root, auto=True, keep=int(cp_opts.get("keep", 10)),
                                                  max_mb=float(cp_opts.get("max_mb", 300)), script=rel_script,
                                                  reason=reason or None, snap=before)
        except Exception as exc:
            checkpoint_entry = {"skipped": f"checkpoint failed: {exc}"}
    stdout = io.StringIO()
    stderr = io.StringIO()
    namespace = {"__name__": "__main__", "__file__": script, "bpy": bpy, "vsblender": helpers}
    try:
        import mathutils
        namespace["mathutils"] = mathutils
    except Exception:
        pass
    previous = os.getcwd()
    added_paths = []
    warnings = []
    value = None
    failure = ""
    helpers._begin(rel_script, root)
    # No .pyc in the workspace: a helper edited twice within a second, at the same size, would
    # otherwise be loaded from the stale cache.
    wrote_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        os.chdir(root)
        added_paths = _prepare_imports(root, lib_paths)
        import warnings as warnings_mod
        with snapshot.DepsgraphRecorder() as recorder:
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
                    except helpers.Cancelled as exc:
                        failure = f"Cancelled at {rel_script}: {exc}. Changes made before the cancel are kept " \
                                  "(one undo step in Blender)."
                    except (Exception, SystemExit) as exc:
                        # SystemExit too: sys.exit() in a script must not reach Blender's timer loop.
                        failure = _script_traceback(exc, script)
            recorder.flush()
        warnings = [f"{item.category.__name__}: {item.message}" for item in caught][:40]
    finally:
        helpers._end()
        sys.dont_write_bytecode = wrote_bytecode
        os.chdir(previous)
        for folder in added_paths:
            try:
                sys.path.remove(folder)
            except ValueError:
                pass
    report = snapshot.diff(before, snapshot.take(), recorder.updates)
    changed = snapshot.flat(report)
    ms = int((time.time() - started) * 1000)
    cp_id = checkpoint_entry.get("id") if isinstance(checkpoint_entry, dict) else None
    if not changed and cp_id:
        history.discard_checkpoint(root, cp_id)
        cp_id = None
    if changed:
        _undo_push(f"VSBlender: {os.path.basename(script)}")
        history.mark_edited()
        entry = {
            "time": history.now_iso(), "event": "script", "actor": str(params.get("actor") or "ai"),
            "script": rel_script, "script_sha256": script_sha, "reason": reason or None, "ok": not failure,
            "result": _jsonable(value) if not failure else None, "error": failure.splitlines()[-1] if failure else None,
            "changes": report, "changed": changed, "ms": ms, "checkpoint": cp_id,
            "blend": history.rel(root, bpy.data.filepath) if bpy.data.filepath else None,
        }
        result_text = json.dumps(entry["result"], ensure_ascii=False, default=str)
        if len(result_text) > 4000:
            entry["result"] = result_text[:4000] + "... (truncated)"
        try:
            history.journal(root, entry)
            what = f"`{rel_script}`" + (" (failed partway)" if failure else "")
            history.notes_log(root, history.log_line(entry["actor"], what, reason, report, cp_id))
        except Exception as exc:
            warnings.append(f"journal not written: {exc}")
    text_out = stdout.getvalue()
    text_err = stderr.getvalue()
    if len(text_out) > 200000:
        text_out = text_out[:200000] + "\n... stdout truncated"
    if len(text_err) > 200000:
        text_err = text_err[:200000] + "\n... stderr truncated"
    if not reason and changed:
        warnings.append("no reason was given, so the journal entry says only what changed, not why")
    if isinstance(checkpoint_entry, dict) and checkpoint_entry.get("skipped"):
        warnings.append(f"no checkpoint: {checkpoint_entry['skipped']}")
    if failure:
        parts = [failure.rstrip()]
        if text_out.strip():
            parts.append("--- stdout before the error ---\n" + text_out.rstrip())
        if text_err.strip():
            parts.append("--- stderr ---\n" + text_err.rstrip())
        if changed:
            parts.append("changed before the error: " + snapshot.summary(report, 20))
            if cp_id:
                parts.append(f"checkpoint from before the script: {cp_id} (restore_checkpoint)")
        raise ScriptError("\n".join(parts), changed, report)
    return {
        "file": script,
        "result": _jsonable(value),
        "stdout": text_out,
        "stderr": text_err,
        "changes": report,
        "checkpoint": cp_id,
        "ms": ms,
    }, warnings, changed


# ----------------------------------------------------------------------------- checkpoints, ingest, history
def checkpoint(params: dict) -> dict:
    root = workspace_root()
    entry = history.checkpoint(root, label=str(params.get("label") or "manual"), auto=False,
                               reason=params.get("reason") or None)
    history.journal(root, {"time": history.now_iso(), "event": "checkpoint", "actor": params.get("actor") or "ai",
                           "checkpoint": entry["id"], "label": entry["label"]})
    return entry


def restore_checkpoint(params: dict) -> dict:
    root = workspace_root()
    cid = str(params.get("id") or "")
    if not cid:
        raise ValueError("id is required (see session_info.checkpoints, or 'last')")
    result = history.restore(root, cid)
    _undo_push(f"VSBlender: restore {result['restored']}")
    history.mark_edited()
    history.journal(root, {"time": history.now_iso(), "event": "restore", "actor": params.get("actor") or "ai",
                           "checkpoint": result["restored"], "safety_checkpoint": result["safety_checkpoint"],
                           "reason": params.get("reason") or None})
    history.notes_log(root, history.log_line(params.get("actor") or "ai", f"restored checkpoint `{result['restored']}`",
                                             params.get("reason"), None, result["safety_checkpoint"]))
    return result


def list_checkpoints(_params: dict) -> dict:
    return {"checkpoints": history.list_checkpoints(workspace_root())}


def save_copy(params: dict) -> dict:
    """A copy of the session for a background job (render, diff). Never changes the user's file."""
    root = workspace_root()
    out = params.get("path")
    if not isinstance(out, str) or not _under(out, root):
        raise ValueError("path must be inside the workspace")
    history.save_copy(out, relative_remap=True)
    scene = _scene()
    return {"file": out, "source": bpy.data.filepath, "dirty": bool(bpy.data.is_dirty),
            "scene": scene.name if scene else None, "frame": scene.frame_current if scene else None,
            "autoexec": bool(bpy.context.preferences.filepaths.use_scripts_auto_execute)}


def ingest_live(params: dict) -> dict:
    root = workspace_root()
    out = history.sidecar_dir(root)
    result = blender_ingest.live_ingest(out, preview_mod.render_view, actor=str(params.get("actor") or "ai"),
                                        reason=str(params.get("reason") or ""), force=bool(params.get("force")),
                                        previews=params.get("previews", True) is not False,
                                        preview_size=int(params.get("preview_size") or 512))
    result["out"] = history.rel(root, result.get("out", out))
    return result


def manifest(params: dict) -> dict:
    """The manifest of the live session, written to a file in the workspace (for diff)."""
    root = workspace_root()
    out = params.get("path")
    if not isinstance(out, str) or not _under(out, root):
        raise ValueError("path must be inside the workspace")
    man = blender_ingest.build_manifest(bpy.data.filepath, None)
    blender_ingest.write_json(out, man)
    return {"file": out, "objects": len(man["objects"])}


def diff_manifests(params: dict) -> dict:
    a, b = (history.load_json(params.get(key) or "") for key in ("a", "b"))
    if a is None or b is None:
        raise FileNotFoundError("both manifest files are needed")
    return {"changes": blender_ingest.diff_manifests(a, b)}


def compose(params: dict) -> dict:
    root = workspace_root()
    import tempfile
    out = params.get("out")
    paths = params.get("paths") or []
    allowed = [root, tempfile.gettempdir()]
    if not isinstance(out, str) or not any(_under(out, r) for r in allowed):
        raise ValueError("out must be inside the workspace or the temp folder")
    for path in paths:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
    return sheet.compose_files(paths, out, labels=params.get("labels"), columns=params.get("columns"),
                               cell=int(params.get("cell") or 512), diff=bool(params.get("diff")))


def object_facts(params: dict) -> dict:
    """Type, parent, size and materials, for roles.json entries of objects the sidecar does not know yet."""
    out = {}
    for name in params.get("names") or []:
        ob = bpy.data.objects.get(name)
        if ob is None:
            continue
        out[name] = {"type": ob.type, "parent": ob.parent.name if ob.parent else None,
                     "dimensions": [round(v, 4) for v in ob.dimensions],
                     "materials": [s.material.name for s in ob.material_slots if s.material]}
    return out


def write_role_property(params: dict):
    """Stamp ai_role on an object, so a reviewed role travels with the .blend. One undo step."""
    name = params.get("target")
    ob = bpy.data.objects.get(name or "")
    if ob is None:
        raise KeyError(f"no object named {name!r}")
    before = snapshot.take()
    ob["ai_role"] = str(params.get("role") or "")
    report = snapshot.diff(before, snapshot.take())
    _undo_push(f"VSBlender: role of {ob.name}")
    history.mark_edited()
    return {"object": ob.name, "property": "ai_role"}, [], snapshot.flat(report)


# ----------------------------------------------------------------------------- dispatch
def _with_text(result) -> dict:
    return result if isinstance(result, dict) else {"value": result}


def dispatch(req: dict) -> dict:
    started = time.time()
    method = req.get("method")
    params = req.get("params") or {}
    ident = req.get("id")
    if not isinstance(params, dict):
        return {"id": ident, "ok": False, "error": "params must be an object"}
    warnings = []
    changed = []
    root = None
    try:
        if method == "ping":
            # No bpy here: ping runs on the socket thread so it answers while Blender is busy.
            result = {"product": "vsblender", "version": ADDON_VERSION, "port": _state["port"], "busy": _state["busy"],
                      "progress": helpers.progress_state()}
        elif method == "cancel":
            result = {"cancelled": helpers.request_cancel(), "busy": _state["busy"]}
        elif method == "session_info":
            result = session_info()
        elif method == "open_file":
            result = open_file(params)
        elif method == "run_script":
            result, warnings, changed = run_script(params)
        elif method == "preview":
            root = workspace_root()
            result, warnings = preview_mod.preview(params, [root])
        elif method == "checkpoint":
            result = checkpoint(params)
        elif method == "restore_checkpoint":
            result = restore_checkpoint(params)
        elif method == "list_checkpoints":
            result = list_checkpoints(params)
        elif method == "save_copy":
            result = save_copy(params)
        elif method == "ingest_live":
            result = ingest_live(params)
        elif method == "manifest":
            result = manifest(params)
        elif method == "diff_manifests":
            result = diff_manifests(params)
        elif method == "compose":
            result = compose(params)
        elif method == "api":
            result = inspect_tools.api(str(params.get("query") or ""), int(params.get("limit") or 60))
        elif method == "node_schema":
            result = inspect_tools.node_schema(str(params.get("bl_idname") or ""), params.get("props") or None)
        elif method == "describe":
            result = inspect_tools.describe(str(params.get("target") or ""), workspace_root(), blender_ingest)
        elif method == "find":
            result = inspect_tools.find(str(params.get("selector") or ""), workspace_root(), int(params.get("limit") or 100))
        elif method == "spatial":
            result = inspect_tools.spatial(params, workspace_root())
        elif method == "object_facts":
            result = object_facts(params)
        elif method == "write_role_property":
            result, warnings, changed = write_role_property(params)
        else:
            return {"id": ident, "ok": False, "error": f"unknown method {method}"}
        return {
            "id": ident,
            "ok": True,
            "result": _jsonable(_with_text(result)),
            "warnings": warnings,
            "changed": changed,
            "ms": int((time.time() - started) * 1000),
        }
    except ScriptError as exc:
        return {"id": ident, "ok": False, "error": str(exc), "changed": exc.changed, "changes": _jsonable(exc.changes),
                "ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        if isinstance(exc, KeyError) and detail.startswith(("'", '"')):
            detail = detail[1:-1]
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
    preview_mod.purge_leftovers()
    for tree in list(bpy.data.node_groups):
        if tree.name.startswith(("_vsblender_scratch", "_ingest_scratch")):
            try:
                bpy.data.node_groups.remove(tree)
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
    """Headless Blender has no timer loop, so accept on the main thread.

    ping and cancel still need an answer while a request runs, so connections are accepted on a
    thread and those two are answered there; everything else is handed to the main thread.
    """
    stop_server()
    _purge_leftovers()
    sock = _bind()
    _state["sock"] = sock
    _state["running"] = True
    _state["blocking"] = True
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
    try:
        while _state["running"]:
            try:
                job = _state["queue"].get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job()
            except Exception:
                traceback.print_exc()
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
    history.reset_edited()
    ensure_server()


@persistent
def _save_post(*_args):
    # Saved: the edits made through the bridge are in the file now. A copy (checkpoint) does not count.
    if not history.writing_copy():
        history.reset_edited()


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
        try:
            recent = history.list_checkpoints(workspace_root())[-3:]
        except Exception:
            recent = []
        if recent:
            box = layout.box()
            box.label(text="Recent checkpoints")
            for entry in reversed(recent):
                box.label(text=f"{entry.get('id', '')}  {entry.get('label', '')}"[:60])


_CLASSES = (VSBlenderPreferences, VSBLENDER_OT_toggle, VSBLENDER_PT_panel)


def register():
    if not _state["registered"]:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
        if _load_post not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_load_post)
        if _save_post not in bpy.app.handlers.save_post:
            bpy.app.handlers.save_post.append(_save_post)
        _state["registered"] = True
        # bpy.data is off limits while an add-on registers (at startup and when enabled from
        # Preferences), so the cleanup waits for the first timer tick.
        if not bpy.app.background and not bpy.app.timers.is_registered(_purge_once):
            bpy.app.timers.register(_purge_once, first_interval=0.1)
    sys.modules["vsblender"] = helpers
    ensure_server()


def unregister():
    stop_server()
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if _save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_save_post)
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    if sys.modules.get("vsblender") is helpers:
        del sys.modules["vsblender"]
    _state["registered"] = False

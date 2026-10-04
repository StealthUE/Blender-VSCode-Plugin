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
import re
import socket
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout

import bpy
from bpy.app.handlers import persistent

from . import blender_ingest, checks, helpers, history, inspect_tools, preview as preview_mod, sheet, snapshot
from . import export as export_mod
from . import units as units_mod

# Legacy add-ons (scripts/addons) are skipped by Preferences without bl_info.
# Extensions read blender_manifest.toml instead and ignore this.
bl_info = {
    "name": "VSBlender Bridge",
    "author": "Massive Dynamic Engineering",
    "version": (0, 3, 0),
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar > VSBlender",
    "description": "Local bridge for the VSBlender VS Code extension",
    "category": "Development",
}

ADDON_VERSION = "0.3.0"
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
            "units": units_mod.units(scene),
            "template": units_mod.template(scene) or None,
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
    # A headless Blender never sets is_dirty, so edits made through the bridge are tracked separately.
    if history.has_unsaved_edits():
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


def import_folders(root: str, lib_paths: list, script: str | None = None) -> list:
    """Folders a script can import from, nearest first: the script's own folder, then each lib/
    from there up to the workspace root, then the configured libPaths. So a model's scripts can
    live next to its .blend (TestObject/scripts/ with TestObject/scripts/lib/)."""
    folders = []

    def add(folder: str) -> None:
        folder = os.path.abspath(folder)
        if os.path.isdir(folder) and _under(folder, root) and \
                os.path.normcase(folder) not in {os.path.normcase(f) for f in folders}:
            folders.append(folder)

    if script:
        here = os.path.dirname(os.path.abspath(script))
        add(here)
        current = here
        while _under(current, root):
            add(os.path.join(current, "lib"))
            parent = os.path.dirname(current)
            if parent == current or os.path.normcase(current) == os.path.normcase(os.path.abspath(root)):
                break
            current = parent
    for entry in lib_paths:
        add(entry if os.path.isabs(entry) else os.path.join(root, entry))
    return folders


def _prepare_imports(root: str, lib_paths: list, script: str | None = None) -> list:
    """Put the workspace helper folders on sys.path and drop workspace modules imported by earlier
    calls, so an edited helper is read again. The add-on's own modules are never dropped."""
    own = os.path.dirname(os.path.abspath(__file__))
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path or name == "vsblender" or name.startswith("vsblender.") or name.startswith(__name__):
            continue
        if _under(path, root) and not _under(path, own):
            del sys.modules[name]
    added = []
    for folder in reversed(import_folders(root, lib_paths, script)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
            added.append(folder)
    helpers.install_modules()
    return added


_DIRECTIVE = re.compile(r"^\s*#\s*vsblender:\s*(.+?)\s*$", re.IGNORECASE)


def script_directives(source: str) -> dict:
    """`# vsblender: <key> [value]` lines near the top of a script (the first 40 lines).

    read-only            no checkpoint; warn if the script changed anything anyway
    atomic off           keep partial changes when the script fails (default: roll back)
    rerun-after a, b     this script repairs what scripts a and b reset (pipeline hint)
    """
    out = {"read_only": False, "atomic": None, "rerun_after": []}
    for line in source.splitlines()[:40]:
        match = _DIRECTIVE.match(line)
        if not match:
            continue
        for part in match.group(1).split(";"):
            words = part.strip().split(None, 1)
            if not words:
                continue
            key = words[0].lower().replace("_", "-").rstrip(":")
            value = words[1].strip() if len(words) > 1 else ""
            if key in ("read-only", "readonly"):
                out["read_only"] = True
            elif key == "atomic":
                out["atomic"] = value.lower() not in ("off", "false", "no", "0")
            elif key == "rerun-after":
                out["rerun_after"] += [v for v in re.split(r"[,\s]+", value) if v]
    return out


def _has_anim(idb) -> bool:
    data = getattr(idb, "animation_data", None) if idb is not None else None
    if not data:
        return False
    try:
        return bool(data.action or len(data.drivers) or len(data.nla_tracks))
    except Exception:
        return False


def animated_ids() -> set:
    """Typed ids of datablocks that carry animation: their own, their node tree's or their shape keys'."""
    out = set()
    for attr in snapshot.COLLECTIONS:
        if attr in ("libraries", "actions", "scenes"):
            continue
        coll = getattr(bpy.data, attr, None)
        if coll is None:
            continue
        for idb in coll:
            if snapshot.skip_name(idb.name):
                continue
            if _has_anim(idb) or _has_anim(getattr(idb, "node_tree", None)) or _has_anim(getattr(idb, "shape_keys", None)):
                out.add(snapshot.typed(attr, idb.name))
    return out


def _lost_animation(report: dict, before: set, after: set) -> list:
    """Removed datablocks that had animation, rebuilt ones that lost it, and removed actions."""
    removed = set(report.get("removed", []))
    rebuilt = set(report.get("recreated", []))
    lost = (removed & before) | ((rebuilt & before) - after)
    lost |= {item for item in removed if item.startswith("AC:")}
    return sorted(lost)


def _stamp_provenance(report: dict, rel_script: str, sha: str, reason: str) -> list:
    """Record which script built each object the run added or rebuilt (ai_built_*), and which one last
    changed the geometry of the others (ai_modified_*). Done after the change report, so the stamps
    themselves are not reported as edits."""
    when = history.now_iso()
    built = {item[3:] for item in report.get("added", []) + report.get("recreated", []) if item.startswith("OB:")}
    modified = report.get("modified", {})
    changed = {name for name, aspects in modified.get("objects", {}).items() if {"data", "modifiers"} & set(aspects)}
    geometry = {("meshes", n) for n in modified.get("meshes", {})} | {("curves", n) for n in modified.get("curves", {})}
    geometry |= {(attr, item[3:]) for item in report.get("added", []) + report.get("recreated", [])
                 for prefix, attr in (("ME:", "meshes"), ("CU:", "curves")) if item.startswith(prefix)}
    if geometry:
        for ob in bpy.data.objects:
            data = ob.data
            if data is None:
                continue
            attr = "meshes" if isinstance(data, bpy.types.Mesh) else "curves" if isinstance(data, bpy.types.Curve) else ""
            if (attr, data.name) in geometry:
                changed.add(ob.name)
    stamped = []
    for name in sorted(built | changed):
        ob = bpy.data.objects.get(name)
        if ob is None or ob.library is not None or snapshot.skip_name(name):
            continue
        try:
            if name in built:
                ob["ai_built_by"] = rel_script
                ob["ai_built_sha"] = sha[:12]
                ob["ai_built_at"] = when
                if reason:
                    ob["ai_built_reason"] = reason[:200]
            else:
                ob["ai_modified_by"] = rel_script
                ob["ai_modified_at"] = when
            stamped.append(name)
        except Exception:
            pass
    return stamped


def _resolve_script(raw, root: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("path is required")
    script = os.path.abspath(raw)
    if not _under(script, root):
        raise ValueError(f"path is outside the workspace: {script}")
    if not script.lower().endswith(".py"):
        raise ValueError("path must be a .py file")
    if not os.path.isfile(script):
        raise FileNotFoundError(script)
    return script


def _load_script(script: str):
    with open(script, encoding="utf-8") as handle:
        source = handle.read()
    try:
        code = compile(source, script, "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"{exc.filename}:{exc.lineno}: {exc.msg}") from exc
    return source, code


class _Run:
    """What one script execution produced: its value, failure text, output, warnings, geometry updates."""

    def __init__(self):
        self.value = None
        self.failure = ""
        self.cancelled = False
        self.stdout = ""
        self.stderr = ""
        self.warnings: list = []
        self.updates: dict = {}


def _execute(script: str, code, root: str, lib_paths: list, rel_script: str, function=None, args=None,
             context: dict | None = None) -> _Run:
    """Run compiled code in a fresh namespace with the workspace import folders, capturing output,
    warnings, depsgraph geometry updates and the failure (traceback from the user's file)."""
    run = _Run()
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
    helpers._begin(rel_script, root, context)
    # No .pyc in the workspace: a helper edited twice within a second, at the same size, would
    # otherwise be loaded from the stale cache.
    wrote_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    caught = []
    try:
        os.chdir(root)
        added_paths = _prepare_imports(root, lib_paths, script)
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
                            run.value = fn(**(args or {}))
                        else:
                            run.value = namespace.get("result")
                    except helpers.Cancelled as exc:
                        run.cancelled = True
                        run.failure = f"Cancelled at {rel_script}: {exc}."
                    except (Exception, SystemExit) as exc:
                        # SystemExit too: sys.exit() in a script must not reach Blender's timer loop.
                        run.failure = _script_traceback(exc, script)
            recorder.flush()
        run.updates = recorder.updates
        run.warnings = [f"{item.category.__name__}: {item.message}" for item in caught][:40]
    finally:
        run.warnings += helpers._end()
        sys.dont_write_bytecode = wrote_bytecode
        os.chdir(previous)
        for folder in added_paths:
            try:
                sys.path.remove(folder)
            except ValueError:
                pass
    run.stdout = stdout.getvalue()
    run.stderr = stderr.getvalue()
    if len(run.stdout) > 200000:
        run.stdout = run.stdout[:200000] + "\n... stdout truncated"
    if len(run.stderr) > 200000:
        run.stderr = run.stderr[:200000] + "\n... stderr truncated"
    return run


def _rollback(root: str, cp_id: str | None, label: str) -> tuple:
    """Put the session back to the checkpoint from before a failed run. Returns (rolled_back_id, note)."""
    if not cp_id:
        return None, "Not rolled back: there was no checkpoint from before the run (checkpoints are off, " \
                     "the file is over the size limit, or the script is marked read-only). The partial changes are kept."
    try:
        history.rollback(root, cp_id)
    except Exception as exc:
        return None, f"Rolling back to {cp_id} failed ({exc}). The partial changes are kept; restore_checkpoint {cp_id} undoes them."
    _undo_push(f"VSBlender: rolled back {label}")
    history.mark_edited()
    return cp_id, f"Rolled back to checkpoint {cp_id}: the session is as it was before the script. " \
                  "Python state the script set up (handlers, timers, driver_namespace) is not rolled back."


def run_script(params: dict):
    root = workspace_root()
    script = _resolve_script(params.get("path"), root)
    function = params.get("function")
    args = params.get("args") or {}
    if function is not None and not isinstance(function, str):
        raise ValueError("function must be a string")
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    reason = str(params.get("reason") or "").strip()
    cp_opts = params.get("checkpoint") if isinstance(params.get("checkpoint"), dict) else {}
    lib_paths = params.get("lib_paths") or ["scripts/lib"]
    source, code = _load_script(script)
    rel_script = history.rel(root, script)
    script_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    directives = script_directives(source)
    atomic = params.get("atomic")
    if not isinstance(atomic, bool):
        atomic = directives["atomic"] if directives["atomic"] is not None else True
    read_only = directives["read_only"]

    started = time.time()
    anim_before = animated_ids()
    before = snapshot.take()
    checkpoint_entry = None
    if cp_opts.get("auto", True) and not read_only:
        try:
            checkpoint_entry = history.checkpoint(root, auto=True, keep=int(cp_opts.get("keep", 10)),
                                                  max_mb=float(cp_opts.get("max_mb", 300)), script=rel_script,
                                                  reason=reason or None, snap=before)
        except Exception as exc:
            checkpoint_entry = {"skipped": f"checkpoint failed: {exc}"}
    context = {"printer": params.get("printer") if isinstance(params.get("printer"), dict) else None,
               "script": rel_script}
    run = _execute(script, code, root, lib_paths, rel_script, function, args, context)
    report = snapshot.diff(before, snapshot.take(), run.updates)
    lost = _lost_animation(report, anim_before, animated_ids())
    if lost:
        report["lost_animation"] = lost
    changed = snapshot.flat(report)
    warnings = list(run.warnings)
    ms = int((time.time() - started) * 1000)
    cp_id = checkpoint_entry.get("id") if isinstance(checkpoint_entry, dict) else None
    if not changed and cp_id:
        history.discard_checkpoint(root, cp_id)
        cp_id = None
    rolled_back = None
    rollback_note = ""
    if run.failure and changed and atomic:
        rolled_back, rollback_note = _rollback(root, cp_id, os.path.basename(script))
    stamped = []
    if changed and not run.failure and params.get("provenance", True) is not False:
        stamped = _stamp_provenance(report, rel_script, script_sha, reason)
    if changed:
        if not rolled_back:
            _undo_push(f"VSBlender: {os.path.basename(script)}")
        history.mark_edited()
        entry = {
            "time": history.now_iso(), "event": "script", "actor": str(params.get("actor") or "ai"),
            "script": rel_script, "script_sha256": script_sha, "reason": reason or None, "ok": not run.failure,
            "result": _jsonable(run.value) if not run.failure else None,
            "error": run.failure.splitlines()[-1] if run.failure else None,
            "changes": report, "changed": changed, "ms": ms, "checkpoint": cp_id,
            "blend": history.rel(root, bpy.data.filepath) if bpy.data.filepath else None,
        }
        if rolled_back:
            entry["rolled_back"] = rolled_back
        result_text = json.dumps(entry["result"], ensure_ascii=False, default=str)
        if len(result_text) > 4000:
            entry["result"] = result_text[:4000] + "... (truncated)"
        try:
            history.journal(root, entry)
            state = " (failed, rolled back)" if rolled_back else " (failed partway)" if run.failure else ""
            history.notes_log(root, history.log_line(entry["actor"], f"`{rel_script}`{state}", reason, report, cp_id))
        except Exception as exc:
            warnings.append(f"journal not written: {exc}")
    if not reason and changed and not rolled_back:
        warnings.append("no reason was given, so the journal entry says only what changed, not why")
    if read_only and changed:
        warnings.append("the script is marked read-only (# vsblender: read-only) but changed: " + snapshot.summary(report, 8))
    if isinstance(checkpoint_entry, dict) and checkpoint_entry.get("skipped"):
        warnings.append(f"no checkpoint: {checkpoint_entry['skipped']}")
    if lost and not rolled_back:
        warnings.append("removed datablocks that had animation: " + ", ".join(lost[:12])
                        + (f" (+{len(lost) - 12} more)" if len(lost) > 12 else "")
                        + ". If that was not intended, rebuild them with their keys, or restore_checkpoint.")
    if run.failure:
        parts = [run.failure.rstrip()]
        if run.stdout.strip():
            parts.append("--- stdout before the error ---\n" + run.stdout.rstrip())
        if run.stderr.strip():
            parts.append("--- stderr ---\n" + run.stderr.rstrip())
        if changed:
            parts.append("changed before the error: " + snapshot.summary(report, 20))
            if rollback_note:
                parts.append(rollback_note)
            elif cp_id:
                parts.append(f"atomic: false, so the partial changes are kept (one undo step). "
                             f"Checkpoint from before the script: {cp_id} (restore_checkpoint).")
        if warnings:
            parts.append("warnings: " + "; ".join(warnings))
        raise ScriptError("\n".join(parts), changed, report)
    out = {
        "file": script,
        "result": _jsonable(run.value),
        "stdout": run.stdout,
        "stderr": run.stderr,
        "changes": report,
        "checkpoint": cp_id,
        "ms": ms,
    }
    if stamped:
        out["provenance"] = f"{len(stamped)} object(s) stamped with this script (ai_built_by / ai_modified_by)"
    if directives["rerun_after"]:
        out["rerun_after"] = directives["rerun_after"]
    if changed:
        try:
            stale = out_of_date(script, root)
        except Exception:
            stale = []
        if stale:
            out["out_of_date"] = stale
    return out, warnings, changed


# ----------------------------------------------------------------------------- pipelines
def find_pipeline(script: str, root: str) -> str | None:
    """pipeline.json in the script's folder or a parent folder up to the workspace root."""
    current = os.path.dirname(os.path.abspath(script))
    while _under(current, root):
        candidate = os.path.join(current, "pipeline.json")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current or os.path.normcase(current) == os.path.normcase(os.path.abspath(root)):
            break
        current = parent
    return None


def load_pipeline(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
        raise ValueError(f"{path}: steps must be a non-empty list of script names")
    after = data.get("after") or {}
    if not isinstance(after, dict):
        raise ValueError(f"{path}: after must map a step to the steps to re-run after it")
    folder = os.path.dirname(path)
    names = [os.path.splitext(s)[0] for s in steps]
    for name in names:
        if not os.path.isfile(os.path.join(folder, name + ".py")):
            raise FileNotFoundError(f"{path}: step {name} has no {name}.py next to it")
    graph = {os.path.splitext(k)[0]: [os.path.splitext(v)[0] for v in (vals if isinstance(vals, list) else [vals])]
             for k, vals in after.items()}
    # A step re-running a step it depends on would loop for ever.
    state = {}

    def visit(node, trail):
        if state.get(node) == 1:
            raise ValueError(f"{path}: after has a cycle: {' -> '.join(trail + [node])}")
        if state.get(node) == 2:
            return
        state[node] = 1
        for nxt in graph.get(node, []):
            visit(nxt, trail + [node])
        state[node] = 2

    for node in graph:
        visit(node, [])
    return {"path": path, "folder": folder, "steps": names, "after": graph}


def _directive_dependents(script: str) -> list:
    """Scripts in the same folder whose header says `# vsblender: rerun-after <this script>`."""
    stem = os.path.splitext(os.path.basename(script))[0]
    folder = os.path.dirname(script)
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".py") or name == os.path.basename(script):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                head = "".join(handle.readline() for _ in range(40))
        except OSError:
            continue
        if stem in script_directives(head)["rerun_after"]:
            out.append(os.path.splitext(name)[0])
    return out


def out_of_date(script: str, root: str) -> list:
    """Steps that should run again after this script: pipeline.json's after map and rerun-after headers."""
    stem = os.path.splitext(os.path.basename(script))[0]
    found = []
    pipe = find_pipeline(script, root)
    if pipe:
        try:
            found += load_pipeline(pipe)["after"].get(stem, [])
        except Exception:
            pass
    found += _directive_dependents(script)
    seen = []
    for item in found:
        if item not in seen:
            seen.append(item)
    return seen


def run_pipeline(params: dict):
    """Run pipeline steps as one transaction: one checkpoint, one undo step, rolled back on failure."""
    root = workspace_root()
    raw = params.get("pipeline")
    if not isinstance(raw, str) or not raw:
        raise ValueError("pipeline (the path of a pipeline.json) is required")
    path = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    if not _under(path, root):
        raise ValueError("pipeline must be inside the workspace")
    pipe = load_pipeline(path)
    steps = pipe["steps"]
    start = params.get("from") or steps[0]
    start = os.path.splitext(os.path.basename(str(start)))[0]
    if start not in steps:
        raise ValueError(f"{start} is not a step; steps: {', '.join(steps)}")
    end = params.get("to")
    end = os.path.splitext(os.path.basename(str(end)))[0] if end else steps[-1]
    if end not in steps:
        raise ValueError(f"{end} is not a step; steps: {', '.join(steps)}")
    i0, i1 = steps.index(start), steps.index(end)
    if i1 < i0:
        raise ValueError(f"{end} comes before {start}")
    if params.get("mode") == "affected":
        wanted = {start}
        frontier = [start]
        while frontier:
            node = frontier.pop()
            for nxt in pipe["after"].get(node, []):
                if nxt not in wanted:
                    wanted.add(nxt)
                    frontier.append(nxt)
        plan = [s for s in steps if s in wanted]
    else:
        plan = steps[i0:i1 + 1]
    reason = str(params.get("reason") or "").strip()
    cp_opts = params.get("checkpoint") if isinstance(params.get("checkpoint"), dict) else {}
    lib_paths = params.get("lib_paths") or ["scripts/lib"]
    atomic = params.get("atomic") is not False
    rel_pipe = history.rel(root, path)
    started = time.time()
    anim_before = animated_ids()
    before = snapshot.take()
    checkpoint_entry = None
    if cp_opts.get("auto", True):
        try:
            checkpoint_entry = history.checkpoint(root, auto=True, keep=int(cp_opts.get("keep", 10)),
                                                  max_mb=float(cp_opts.get("max_mb", 300)), script=rel_pipe,
                                                  reason=reason or None, snap=before)
        except Exception as exc:
            checkpoint_entry = {"skipped": f"checkpoint failed: {exc}"}
    cp_id = checkpoint_entry.get("id") if isinstance(checkpoint_entry, dict) else None
    printer = params.get("printer") if isinstance(params.get("printer"), dict) else None
    ran = []
    warnings = []
    failure = ""
    failed_step = None
    step_before = before
    updates = {}
    for index, name in enumerate(plan):
        script = os.path.join(pipe["folder"], name + ".py")
        rel_script = history.rel(root, script)
        source, code = _load_script(script)
        sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        run = _execute(script, code, root, lib_paths, f"{rel_pipe} step {index + 1}/{len(plan)}: {name}", None, None,
                       {"printer": printer, "script": rel_script})
        for attr, names in run.updates.items():
            updates.setdefault(attr, {}).update(names)
        step_after = snapshot.take()
        step_report = snapshot.diff(step_before, step_after, run.updates)
        warnings += [f"{name}: {w}" for w in run.warnings]
        entry = {"step": name, "ms": None, "changes": snapshot.summary(step_report, 8)}
        if run.stdout.strip():
            entry["stdout"] = run.stdout[-2000:]
        if run.failure:
            failure = run.failure
            failed_step = name
            entry["error"] = run.failure.strip().splitlines()[-1]
            ran.append(entry)
            break
        if snapshot.flat(step_report):
            _stamp_provenance(step_report, rel_script, sha, reason)
            step_after = snapshot.take()
        entry["result"] = _jsonable(run.value)
        ran.append(entry)
        step_before = step_after
    report = snapshot.diff(before, snapshot.take(), updates)
    lost = _lost_animation(report, anim_before, animated_ids())
    if lost:
        report["lost_animation"] = lost
    changed = snapshot.flat(report)
    if not changed and cp_id:
        history.discard_checkpoint(root, cp_id)
        cp_id = None
    rolled_back = None
    note = ""
    if failure and changed and atomic:
        rolled_back, note = _rollback(root, cp_id, os.path.basename(path))
    if changed:
        if not rolled_back:
            _undo_push(f"VSBlender: {os.path.basename(pipe['folder'])} pipeline {plan[0]}..{plan[-1]}")
        history.mark_edited()
        journal_entry = {"time": history.now_iso(), "event": "pipeline", "actor": str(params.get("actor") or "ai"),
                         "pipeline": rel_pipe, "steps": [r["step"] for r in ran], "reason": reason or None,
                         "ok": not failure, "changes": report, "changed": changed, "checkpoint": cp_id,
                         "ms": int((time.time() - started) * 1000)}
        if rolled_back:
            journal_entry["rolled_back"] = rolled_back
        try:
            history.journal(root, journal_entry)
            state = " (failed, rolled back)" if rolled_back else " (failed partway)" if failure else ""
            history.notes_log(root, history.log_line(journal_entry["actor"], f"pipeline `{rel_pipe}` {plan[0]}..{plan[-1]}{state}",
                                                     reason, report, cp_id))
        except Exception as exc:
            warnings.append(f"journal not written: {exc}")
    if lost and not rolled_back:
        warnings.append("removed datablocks that had animation: " + ", ".join(lost[:12]))
    if failure:
        parts = [f"step {failed_step} failed:", failure.rstrip(),
                 "steps run: " + ", ".join(r["step"] for r in ran)]
        if changed:
            parts.append("changed before the error: " + snapshot.summary(report, 20))
            parts.append(note or (f"atomic: false, so the partial changes are kept. Checkpoint from before the pipeline: {cp_id}." if cp_id else ""))
        raise ScriptError("\n".join(p for p in parts if p), changed, report)
    return {"pipeline": rel_pipe, "steps": ran, "changes": report, "checkpoint": cp_id,
            "ms": int((time.time() - started) * 1000)}, warnings, changed


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
                               cell=int(params.get("cell") or 512), diff=bool(params.get("diff")),
                               max_bytes=int(params.get("max_bytes") or 0) or None)


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
        elif method == "check_model":
            result, warnings = checks.check_model(params, workspace_root())
        elif method == "export_model":
            result, warnings = export_mod.export_model(params, workspace_root())
        elif method == "timeline":
            result = inspect_tools.timeline(params, workspace_root())
        elif method == "measure":
            from . import measure as measure_mod
            result = measure_mod.measure(params, workspace_root())
        elif method == "run_pipeline":
            result, warnings, changed = run_pipeline(params)
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
    helpers.install_modules()
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
    helpers.uninstall_modules()
    _state["registered"] = False

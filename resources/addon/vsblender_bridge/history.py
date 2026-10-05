"""Checkpoints, restore, the AI change journal, and the sidecar status of the live session.

Checkpoints are copies written with save_as_mainfile(copy=True), so the user's file, its path and
its dirty flag are untouched. Restore keeps the user's file path: it removes the session's data and
appends everything from the checkpoint, instead of opening the checkpoint as a file.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

import bpy

from . import snapshot

# Datablock types a checkpoint restore replaces. UI data (screens, workspaces, brushes) is left alone.
CONTENT = ("objects", "meshes", "materials", "lights", "cameras", "collections", "images", "worlds",
           "node_groups", "texts", "actions", "curves", "armatures", "lattices", "metaballs", "fonts",
           "textures", "particles", "grease_pencils", "grease_pencils_v3", "hair_curves", "pointclouds",
           "volumes", "lightprobes", "speakers", "cache_files", "movieclips", "masks", "sounds")
_OLD_SCENE = "_vsblender_restore_old"
_sha_cache: dict = {}
# A headless Blender never sets bpy.data.is_dirty (no undo pushes), so edits made through the
# bridge are counted here too, with how many runs changed the scene since the last save and when the
# first of them was. Reset when a file loads or is saved.
_session = {"edited": False, "copying": False, "runs": 0, "first": None, "saved_at": None}


def writing_copy() -> bool:
    return bool(_session["copying"])


def mark_edited(run: bool = True) -> None:
    """A change through the bridge. run: count it as one run since the last save (scripts,
    pipelines, restores, appends)."""
    _session["edited"] = True
    if run:
        _session["runs"] += 1
        if _session["first"] is None:
            _session["first"] = datetime.datetime.now().astimezone()


def reset_edited(saved: bool = False) -> None:
    _session.update(edited=False, runs=0, first=None)
    if saved:
        _session["saved_at"] = datetime.datetime.now().astimezone()


def has_unsaved_edits() -> bool:
    return bool(bpy.data.is_dirty) or _session["edited"]


def unsaved() -> dict:
    """What the file on disk does not have yet: runs through the bridge since the last save, and
    whether Blender itself has unsaved changes (edits by hand count there)."""
    first = _session["first"]
    minutes = int((datetime.datetime.now().astimezone() - first).total_seconds() // 60) if first else None
    out = {"dirty": bool(bpy.data.is_dirty) or bool(_session["edited"]), "runs": int(_session["runs"]),
           "first": first.isoformat(timespec="seconds") if first else None, "minutes": minutes,
           "saved_at": _session["saved_at"].isoformat(timespec="seconds") if _session["saved_at"] else None,
           "file": bpy.data.filepath or None}
    out["text"] = unsaved_text(out)
    return out


def unsaved_text(state: dict) -> str:
    if not state.get("file"):
        return "the session has never been saved to a .blend"
    if state.get("runs"):
        when = "just now" if not state.get("minutes") else f"{state['minutes']} min ago"
        return (f"{state['runs']} run(s) since the last save, the first {when}. The file on disk does not have them; "
                "save when the user wants it (the save tool)")
    if state.get("dirty"):
        return "Blender has unsaved changes (made by hand, or by tools that do not count runs)"
    return "none: the file on disk matches the session"


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def sidecar_dir(root: str, blend: str | None = None) -> str:
    blend = bpy.data.filepath if blend is None else blend
    if blend:
        return os.path.join(os.path.dirname(blend), ".blender-ai", os.path.splitext(os.path.basename(blend))[0])
    return os.path.join(root, ".blender-ai", "untitled")


def rel(root: str, path: str) -> str:
    try:
        out = os.path.relpath(path, root)
    except ValueError:
        return path
    return path if out.startswith("..") else out.replace(os.sep, "/")


def load_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def file_sha256(path: str) -> str | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(path)), stat.st_mtime_ns, stat.st_size)
    if key in _sha_cache:
        return _sha_cache[key]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    _sha_cache.clear()
    _sha_cache[key] = digest.hexdigest()
    return _sha_cache[key]


# ----------------------------------------------------------------------------- saving copies
def _window_override():
    if bpy.app.background:
        return None
    try:
        windows = bpy.context.window_manager.windows
        return windows[0] if windows else None
    except Exception:
        return None


def save_main(path: str | None = None, compress: bool = True) -> str:
    """Save the session to its own file (or save it as path, which then becomes the open file).

    The thumbnail is drawn by Blender as usual; if that fails outside a 3D view, the save is retried
    without one.
    """
    target = os.path.abspath(path or bpy.data.filepath)
    if not target:
        raise ValueError("the session has never been saved: pass path")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    same = bool(bpy.data.filepath) and os.path.normcase(os.path.abspath(bpy.data.filepath)) == os.path.normcase(target)
    kwargs = {"filepath": target, "check_existing": False, "compress": bool(compress), "relative_remap": True}

    def run():
        window = _window_override()
        op = bpy.ops.wm.save_mainfile if same else bpy.ops.wm.save_as_mainfile
        if window is not None:
            with bpy.context.temp_override(window=window):
                result = op(**kwargs)
        else:
            result = op(**kwargs)
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender did not save {target} ({', '.join(result)})")

    try:
        run()
    except RuntimeError as exc:
        prefs = bpy.context.preferences
        paths = getattr(prefs, "filepaths", None)
        old = getattr(paths, "file_preview_type", None) if paths is not None else None
        if old is None or old == "NONE":
            raise
        was_dirty = getattr(prefs, "is_dirty", None)
        try:
            paths.file_preview_type = "NONE"
            run()
        finally:
            paths.file_preview_type = old
            if was_dirty is not None:
                try:
                    prefs.is_dirty = was_dirty
                except Exception:
                    pass
        print(f"vsblender: saved without a thumbnail ({exc})")
    return target


def save_copy(path: str, relative_remap: bool = True, compress: bool = False) -> None:
    """Write the session to path without changing the open file, its path or its dirty flag.

    compress: checkpoints are kept, so they are compressed (zstd, a fraction of the size). Copies for
    background jobs are read once and thrown away, so they are written uncompressed, which is faster.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    kwargs = {"filepath": os.path.abspath(path), "copy": True, "check_existing": False,
              "relative_remap": relative_remap, "compress": bool(compress)}
    prefs = bpy.context.preferences
    paths = getattr(prefs, "filepaths", None)
    # A thumbnail would draw the user's viewport; a copy does not need one.
    old_preview = getattr(paths, "file_preview_type", None) if paths is not None else None
    was_dirty = getattr(prefs, "is_dirty", None)
    _session["copying"] = True
    try:
        if old_preview is not None:
            paths.file_preview_type = "NONE"
        window = _window_override()
        if window is not None:
            with bpy.context.temp_override(window=window):
                bpy.ops.wm.save_as_mainfile(**kwargs)
        else:
            bpy.ops.wm.save_as_mainfile(**kwargs)
    finally:
        _session["copying"] = False
        if old_preview is not None:
            try:
                paths.file_preview_type = old_preview
                if was_dirty is not None:
                    prefs.is_dirty = was_dirty
            except Exception:
                pass


# ----------------------------------------------------------------------------- checkpoints
def _index_path(root: str) -> str:
    return os.path.join(sidecar_dir(root), "checkpoints", "index.json")


def list_checkpoints(root: str) -> list:
    data = load_json(_index_path(root))
    return data if isinstance(data, list) else []


def _write_index(root: str, entries: list) -> None:
    write_json(_index_path(root), entries)


def _new_id(folder: str) -> str:
    base = "cp-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    cid, n = base, 1
    while os.path.exists(os.path.join(folder, cid + ".blend")):
        n += 1
        cid = f"{base}-{n}"
    return cid


def checkpoint(root: str, label: str = "", auto: bool = False, keep: int = 10, max_mb: float = 300.0,
               script: str | None = None, reason: str | None = None, snap: dict | None = None,
               budget_mb: float | None = None) -> dict:
    """Write a checkpoint. auto ones are pruned to `keep` and to `budget_mb` on disk (oldest first);
    labelled ones stay until removed by hand. The entry says how big it is and what all checkpoints
    of this file take."""
    folder = os.path.join(sidecar_dir(root), "checkpoints")
    if auto and bpy.data.filepath:
        try:
            size_mb = os.path.getsize(bpy.data.filepath) / 1e6
        except OSError:
            size_mb = 0.0
        if size_mb > max_mb:
            return {"skipped": f"the file is {size_mb:.0f} MB, over the {max_mb:.0f} MB auto-checkpoint limit"}
    os.makedirs(folder, exist_ok=True)
    cid = _new_id(folder)
    path = os.path.join(folder, cid + ".blend")
    save_copy(path, relative_remap=True, compress=True)
    try:
        scene = bpy.context.scene.name
    except Exception:
        scene = bpy.data.scenes[0].name if bpy.data.scenes else ""
    entry = {
        "id": cid,
        "file": rel(root, path),
        "label": label or ("before " + os.path.basename(script) if script else ""),
        "auto": bool(auto),
        "time": now_iso(),
        "blend": rel(root, bpy.data.filepath) if bpy.data.filepath else None,
        "scene": scene,
        "bytes": os.path.getsize(path),
        "signature": snapshot.signature(snap),
        "fake_users": fake_user_names(),
    }
    if script:
        entry["script"] = script
    if reason:
        entry["reason"] = reason
    refs = _reference_names()
    if refs:
        entry["references"] = refs[:20]
    entries = [e for e in list_checkpoints(root) if isinstance(e, dict)]
    entries.append(entry)
    entries, dropped = _prune(root, entries, keep, budget_mb, cid)
    _write_index(root, entries)
    out = dict(entry)
    out["total_bytes"] = sum(int(e.get("bytes") or 0) for e in entries)
    out["count"] = len(entries)
    if dropped:
        out["dropped"] = dropped
    return out


def _reference_names() -> list:
    try:
        from . import marks
        return marks.reference_objects()
    except Exception:
        return []


def _prune(root: str, entries: list, keep: int, budget_mb: float | None = None, newest: str | None = None) -> tuple:
    """Drop the oldest auto checkpoints over `keep`, then while all of them take more than budget_mb.
    The checkpoint just written is never dropped. Returns (entries, dropped ids)."""
    autos = [e for e in entries if e.get("auto")]
    drop = autos[:-keep] if keep > 0 and len(autos) > keep else []
    if budget_mb and budget_mb > 0:
        left = [e for e in autos if e not in drop]
        total = sum(int(e.get("bytes") or 0) for e in left)
        for e in list(left):
            if total <= budget_mb * 1e6 or e.get("id") == newest:
                break
            drop.append(e)
            total -= int(e.get("bytes") or 0)
    for entry in drop:
        _remove_file(root, entry)
    gone = {e["id"] for e in drop}
    return [e for e in entries if e.get("id") not in gone], sorted(gone)


def _remove_file(root: str, entry: dict) -> None:
    path = os.path.join(root, entry.get("file", "")) if entry.get("file") else ""
    for candidate in (path, path[:-6] + ".manifest.json" if path.endswith(".blend") else ""):
        if candidate and os.path.isfile(candidate):
            try:
                os.remove(candidate)
            except OSError:
                pass


def discard_checkpoint(root: str, cid: str) -> None:
    """Drop an auto checkpoint again when the script turned out not to change anything."""
    entries = list_checkpoints(root)
    keep = [e for e in entries if e.get("id") != cid]
    for entry in entries:
        if entry.get("id") == cid:
            _remove_file(root, entry)
    _write_index(root, keep)


def find_checkpoint(root: str, cid: str) -> dict:
    entries = list_checkpoints(root)
    if cid in ("last", "latest") and entries:
        return entries[-1]
    for entry in entries:
        if entry.get("id") == cid:
            return entry
    matches = [e for e in entries if e.get("id", "").startswith(cid)]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(e.get("id", "") for e in entries[-8:]) or "none"
    raise KeyError(f"no checkpoint {cid}. Recent: {known}")


def restore(root: str, cid: str) -> dict:
    """Replace the session's data with a checkpoint's. A checkpoint of the current state comes first."""
    entry = find_checkpoint(root, cid)
    path = os.path.join(root, entry["file"])
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    safety = checkpoint(root, label=f"before restoring {entry['id']}")
    try:
        _replace_data(path, entry.get("scene") or "", entry.get("fake_users"))
    except Exception as exc:
        raise RuntimeError(f"restore stopped partway ({exc}). The state from before the restore is checkpoint "
                           f"{safety.get('id')}: restore that id to get it back.") from exc
    return {"restored": entry["id"], "safety_checkpoint": safety.get("id"), "file": bpy.data.filepath,
            "scene": bpy.context.scene.name if bpy.context.scene else None}


def rollback(root: str, cid: str) -> dict:
    """Put the session back to a checkpoint after a failed script, without a safety checkpoint of the
    failed state (the error report already says what the script changed before it failed)."""
    entry = find_checkpoint(root, cid)
    path = os.path.join(root, entry["file"])
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    _replace_data(path, entry.get("scene") or "", entry.get("fake_users"))
    return entry


def fake_user_names() -> dict:
    """Datablocks kept by a fake user, by collection: appending a checkpoint does not keep the flag."""
    out = {}
    for attr in CONTENT:
        collection = getattr(bpy.data, attr, None)
        if collection is None:
            continue
        names = [idb.name for idb in collection if idb.library is None and idb.use_fake_user]
        if names:
            out[attr] = names
    return out


def _replace_data(path: str, active_scene: str, fake_users: dict | None = None) -> None:
    window_scenes = []
    if not bpy.app.background:
        for window in bpy.context.window_manager.windows:
            window_scenes.append((window, window.scene.name if window.scene else ""))
    # Rename the current scenes so the appended ones get their own names back. They stay until the
    # windows have switched, because Blender needs a scene in every window at all times.
    old_scenes = list(bpy.data.scenes)
    for index, scene in enumerate(old_scenes):
        scene.name = f"{_OLD_SCENE}_{index}"
    keep_images = {img.name for img in bpy.data.images if img.type in {"RENDER_RESULT", "COMPOSITING"}}
    doomed = []
    for attr in CONTENT:
        collection = getattr(bpy.data, attr, None)
        if collection is None:
            continue
        for idb in collection:
            if idb.library is not None or (attr == "images" and idb.name in keep_images):
                continue
            doomed.append(idb)
    if doomed:
        bpy.data.batch_remove(doomed)
    appended = {}
    with bpy.data.libraries.load(path, link=False) as (source, target):
        target.scenes = list(source.scenes)
        for attr in CONTENT:
            if hasattr(source, attr) and hasattr(target, attr):
                names = list(getattr(source, attr))
                if attr == "images":
                    names = [n for n in names if n not in keep_images]
                setattr(target, attr, names)
                appended[attr] = names
    # Appending drops fake users, so a text block (fake user, no other users) would be lost by the
    # next save or checkpoint. Put back the ones the checkpoint recorded; for an older checkpoint
    # without that record, text blocks (which always have one) are the only safe guess.
    for attr, names in appended.items():
        collection = getattr(bpy.data, attr, None)
        if collection is None:
            continue
        wanted = set((fake_users or {}).get(attr, [])) if fake_users is not None else None
        for item in names:
            idb = item if isinstance(item, bpy.types.ID) else collection.get(item)
            if idb is None or idb.library is not None:
                continue
            if (wanted is not None and idb.name in wanted) or (wanted is None and attr == "texts" and idb.users == 0):
                try:
                    idb.use_fake_user = True
                except Exception:
                    pass
    restored = [s for s in bpy.data.scenes if not s.name.startswith(_OLD_SCENE)]
    if not restored:
        raise RuntimeError("the checkpoint had no scene")
    fallback = bpy.data.scenes.get(active_scene) or restored[0]
    for window, name in window_scenes:
        # name was read before the rename, so it is the scene's own name, now held by the appended copy.
        target = bpy.data.scenes.get(name) if name else None
        window.scene = target if target is not None else fallback
    if bpy.app.background or not window_scenes:
        _remove_old_scenes()
    else:
        bpy.app.timers.register(_remove_old_scenes, first_interval=0.2)
    # The library entry for the checkpoint is not needed once its data is local.
    for library in list(bpy.data.libraries):
        if os.path.normcase(os.path.abspath(bpy.path.abspath(library.filepath))) == os.path.normcase(os.path.abspath(path)):
            try:
                bpy.data.libraries.remove(library)
            except Exception:
                pass


def _remove_old_scenes():
    for scene in [s for s in bpy.data.scenes if s.name.startswith(_OLD_SCENE)]:
        if len(bpy.data.scenes) <= 1:
            break
        try:
            bpy.data.scenes.remove(scene)
        except Exception:
            pass
    return None


# ----------------------------------------------------------------------------- journal
def journal(root: str, entry: dict) -> str:
    path = os.path.join(sidecar_dir(root), "journal.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return path


def notes_log(root: str, line: str) -> bool:
    """Append to NOTES.md. The change log is its last section, so the end of the file is the log."""
    path = os.path.join(sidecar_dir(root), "NOTES.md")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if "## Change log" not in text:
        text = text.rstrip("\n") + "\n\n## Change log\n"
    text = text.rstrip("\n") + "\n" + line.replace("\n", " ") + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return True


_INTENT = "## Intent & constraints"


def write_intent(root: str, text: str, replace: bool = False) -> str:
    """Add to (or rewrite) the hand-written Intent & constraints section of NOTES.md, which re-ingests
    keep. Returns the section. The same as the notes tool, for scripts (vsblender.intent)."""
    path = os.path.join(sidecar_dir(root), "NOTES.md")
    if not os.path.isfile(path):
        raise FileNotFoundError("NOTES.md does not exist yet: ingest the file first")
    with open(path, encoding="utf-8") as handle:
        notes = handle.read()
    body = str(text).strip()
    start = notes.find(_INTENT)
    if start < 0:
        log = notes.find("## Change log")
        section = f"{_INTENT}\n\n{body}\n\n"
        notes = notes[:log] + section + notes[log:] if log >= 0 else notes.rstrip() + "\n\n" + section
        merged = body
    else:
        after = start + len(_INTENT)
        nxt = notes.find("\n## ", after)
        end = len(notes) if nxt < 0 else nxt + 1
        current = re.sub(r"<!--\s*Hand-written by people[\s\S]*?-->\s*", "", notes[after:end]).strip()
        merged = body if replace or not current else f"{current}\n\n{body}"
        notes = f"{notes[:after]}\n\n{merged}\n\n{notes[end:].lstrip(chr(10))}"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(notes)
    journal(root, {"time": now_iso(), "event": "intent", "actor": "ai", "mode": "replace" if replace else "append",
                   "text": body[:2000]})
    return merged


def log_line(actor: str, what: str, reason: str | None, report: dict | None, checkpoint_id: str | None) -> str:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    why = f": {reason.strip().rstrip('.')}." if reason else "."
    changes = f" {snapshot.summary(report, 6)}." if report is not None else ""
    cp = f" Checkpoint `{checkpoint_id}`." if checkpoint_id else ""
    return f"- {stamp} [{actor}] {what}{why}{changes}{cp}"


# ----------------------------------------------------------------------------- live status
def sidecar_status(root: str) -> dict:
    """Whether the sidecar describes the live session.

    current: it does. stale: the .blend on disk changed since the ingest. new: no ingest yet.
    diverged: Blender has unsaved changes that the sidecar does not describe.
    """
    blend = bpy.data.filepath
    folder = sidecar_dir(root)
    if not blend:
        return {"status": "unsaved", "sidecar": rel(root, folder),
                "detail": "the session has never been saved, so it has no sidecar. ingest(live=true) writes one under .blender-ai/untitled/."}
    state = load_json(os.path.join(folder, "state.json"))
    out = {"sidecar": rel(root, folder)}
    if not isinstance(state, dict):
        out["status"] = "new"
        out["detail"] = "not ingested yet. Call ingest."
        return out
    out["ingested_at"] = state.get("ingested_at")
    out["source"] = state.get("source", "disk")
    disk = "current" if state.get("sha256") == file_sha256(blend) else "stale"
    out["disk"] = disk
    live_sig = None
    if state.get("source") == "live" or has_unsaved_edits():
        live_sig = snapshot.signature()
    if live_sig is not None:
        matches = state.get("signature") == live_sig
        out["status"] = "current" if matches else "diverged"
    else:
        out["status"] = disk
    details = {
        "current": "the sidecar describes this session." if out["source"] == "live" else "the sidecar matches the file.",
        "stale": "the .blend on disk changed since the ingest. Call ingest.",
        "diverged": "Blender has unsaved changes the sidecar does not describe. Call ingest with live=true.",
    }
    out["detail"] = details.get(out["status"], "")
    return out


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "unnamed"

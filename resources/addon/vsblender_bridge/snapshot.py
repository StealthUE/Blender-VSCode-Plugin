"""Before/after snapshots of bpy.data, used to say what a script changed.

Every datablock is keyed by session_uid, so a delete-and-recreate under the same name shows up as
recreated, and a rename as renamed. Each datablock gets a small dict of aspect -> hash
("transform", "nodes", "keys", ...), so a change is reported with the part that changed. Scene
settings are kept as individual property paths, so the report can say "view_settings.look".

No relative imports: blender_ingest.py loads this file directly when it runs headless.
"""
from __future__ import annotations

import hashlib
import math

import bpy

# bpy.data collection -> typed-id prefix. The collection name is also the category in reports.
COLLECTIONS = {
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
    "actions": "AC",
    "scenes": "SC",
    "curves": "CU",
    "armatures": "AR",
    "lattices": "LT",
    "metaballs": "MB",
    "fonts": "VF",
    "textures": "TE",
    "particles": "PA",
    "grease_pencils": "GP",
    "grease_pencils_v3": "GP",
    "hair_curves": "CV",
    "pointclouds": "PT",
    "volumes": "VO",
    "lightprobes": "LP",
    "speakers": "SK",
    "cache_files": "CF",
    "movieclips": "MC",
    "masks": "MK",
    "libraries": "LB",
}

# Scene sub-structs whose settings are compared property by property.
SCENE_STRUCTS = ("render", "eevee", "cycles", "view_settings", "display_settings", "unit_settings",
                 "display", "display.shading", "sequencer_colorspace_settings")
SCENE_TOP = ("camera", "world", "frame_start", "frame_end", "frame_step", "frame_current", "use_gravity",
             "compositing_node_group", "use_nodes", "background_set", "active_clip", "use_preview_range")
# Reported as changes, but left out of signature(): scrubbing the timeline is not an edit, and a
# session_uid is only stable within one Blender session.
VOLATILE = {"frame_current", "data_uid"}
# Blender drops datablocks without users when it saves, except these.
KEEP_UNUSED = {"scenes", "texts", "libraries"}

# Node fields that move with layout and say nothing about the result.
NODE_SKIP = {"location", "location_absolute", "width", "height", "dimensions", "select", "show_options",
             "show_preview", "hide", "show_texture", "use_custom_color", "color", "warning_propagation",
             "is_active_output", "color_tag", "parent"}
GENERIC_SKIP = {"rna_type", "name", "name_full", "session_uid", "users", "use_fake_user", "use_extra_user", "tag",
                "is_evaluated", "is_runtime_data", "is_missing", "is_embedded_data", "is_library_indirect",
                "original", "is_dirty", "preview", "library_weak_reference", "asset_data", "override_library",
                "id_type", "is_editmode", "library", "filepath_raw",
                # Reading it logs "matches no enum" on every snapshot in Blender 5.2.
                "wireframe_color_type"}
MODIFIER_SKIP = {"show_expanded", "is_active", "persistent_uid", "is_override_data", "show_on_cage",
                 "show_in_editmode", "use_pin_to_last"}
# Above this many vertices in total, mesh coordinates are not hashed (counts and the depsgraph still are).
COORD_HASH_LIMIT = 2_000_000


def skip_name(name: str) -> bool:
    return name.startswith("_vsblender") or name.startswith("_ingest")


def _round(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        rounded = round(value, 5)
        return 0.0 if rounded == 0 else rounded
    return value


def _is_id_struct(struct) -> bool:
    while struct is not None:
        if getattr(struct, "identifier", "") == "ID":
            return True
        struct = getattr(struct, "base", None)
    return False


def _value(prop, value):
    if prop.type == "ENUM" and getattr(prop, "is_enum_flag", False):
        return tuple(sorted(value))
    if isinstance(value, str) or not hasattr(value, "__len__"):
        return _round(value)
    try:
        flat = []
        for item in value:
            if hasattr(item, "__len__") and not isinstance(item, str):
                flat.extend(_round(float(x)) for x in item)
            else:
                flat.append(_round(item))
        return tuple(flat)
    except (TypeError, ValueError):
        return str(value)


def rna_values(struct, skip=()) -> dict:
    """Writable scalar, enum, string and array properties, plus ID pointers by name."""
    out = {}
    if struct is None:
        return out
    try:
        props = struct.bl_rna.properties
    except Exception:
        return out
    for prop in props:
        key = prop.identifier
        if key in skip or key in GENERIC_SKIP or key.startswith("bl_") or prop.type == "COLLECTION":
            continue
        if prop.type == "POINTER":
            if not _is_id_struct(getattr(prop, "fixed_type", None)):
                continue
            try:
                target = getattr(struct, key)
            except Exception:
                continue
            out[key] = target.name if target is not None else None
            continue
        if prop.is_readonly:
            continue
        try:
            out[key] = _value(prop, getattr(struct, key))
        except Exception:
            continue
    return out


def _digest(value) -> str:
    return hashlib.blake2b(repr(value).encode("utf-8", "replace"), digest_size=8).hexdigest()


def _custom_props(idb) -> tuple:
    out = []
    try:
        for key in sorted(idb.keys()):
            if key.startswith("_") or key in ("cycles", "cycles_visibility"):
                continue
            value = idb[key]
            if hasattr(value, "to_dict"):
                value = value.to_dict()
            elif hasattr(value, "to_list"):
                value = value.to_list()
            out.append((key, repr(value)[:500]))
    except Exception:
        pass
    return tuple(out)


def socket_value(sock):
    if not hasattr(sock, "default_value"):
        return None
    try:
        value = sock.default_value
    except Exception:
        return None
    if isinstance(value, bpy.types.ID):
        return value.name
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _round(value)
    try:
        return tuple(_round(float(x)) for x in value)
    except (TypeError, ValueError):
        return str(value)


def node_signature(node) -> tuple:
    parts = [node.bl_idname, node.label, bool(getattr(node, "mute", False))]
    parts.append(tuple(sorted(rna_values(node, NODE_SKIP).items())))
    parts.append(tuple((s.identifier, socket_value(s)) for s in node.inputs))
    # Value and RGB nodes keep their value on the output socket.
    parts.append(tuple((s.identifier, socket_value(s)) for s in node.outputs if hasattr(s, "default_value")))
    ramp = getattr(node, "color_ramp", None)
    if ramp is not None:
        parts.append((ramp.interpolation, ramp.color_mode,
                      tuple((_round(e.position), tuple(_round(c) for c in e.color)) for e in ramp.elements)))
    mapping = getattr(node, "mapping", None)
    if mapping is not None and hasattr(mapping, "curves"):
        try:
            parts.append(tuple(tuple((_round(p.location[0]), _round(p.location[1])) for p in curve.points)
                               for curve in mapping.curves))
        except Exception:
            pass
    return tuple(parts)


def tree_aspects(tree) -> dict:
    """nodes, links and interface of a node tree, each as one hash."""
    if tree is None:
        return {}
    nodes = tuple(sorted((node.name, node_signature(node)) for node in tree.nodes))
    links = tuple(sorted(
        f"{link.from_node.name}.{link.from_socket.identifier}>{link.to_node.name}.{link.to_socket.identifier}"
        + ("~" if getattr(link, "is_muted", False) else "")
        for link in tree.links
    ))
    out = {"nodes": _digest(nodes), "links": _digest(links)}
    interface = getattr(tree, "interface", None)
    if interface is not None:
        try:
            items = tuple((getattr(item, "item_type", ""), getattr(item, "name", ""), getattr(item, "in_out", ""),
                           getattr(item, "socket_type", "")) for item in interface.items_tree)
            out["interface"] = _digest(items)
        except Exception:
            pass
    return out


def _anim_action(idb):
    ad = getattr(idb, "animation_data", None)
    if ad is None:
        return None
    action = ad.action
    slot = getattr(ad, "action_slot", None)
    return (action.name if action else None, getattr(slot, "identifier", None) if slot else None,
            tuple((d.data_path, d.array_index, d.driver.expression) for d in ad.drivers),
            tuple(t.name for t in getattr(ad, "nla_tracks", [])))


def action_fcurves(action) -> list:
    """Every fcurve of an action: layered (4.4+) through channelbags, legacy through action.fcurves."""
    found = []
    for layer in getattr(action, "layers", None) or []:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                found.extend(bag.fcurves)
    if not found:
        try:
            found.extend(action.fcurves)
        except Exception:
            pass
    return found


def _keys_digest(fcurves) -> str:
    rows = []
    for fc in fcurves:
        keys = tuple((_round(k.co[0]), _round(k.co[1]), k.interpolation,
                      _round(k.handle_left[1]), _round(k.handle_right[1])) for k in fc.keyframe_points)
        rows.append((fc.data_path, fc.array_index, keys, tuple(m.type for m in fc.modifiers), bool(fc.mute)))
    return _digest(tuple(sorted(rows, key=lambda r: (r[0], r[1]))))


def _mesh_aspects(me, hash_coords: bool) -> dict:
    counts = (len(me.vertices), len(me.edges), len(me.polygons), len(me.loops))
    out = {"counts": _digest(counts)}
    if hash_coords and counts[0]:
        try:
            import numpy as np
            co = np.empty(counts[0] * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            out["geometry"] = hashlib.blake2b(np.round(co, 5).tobytes(), digest_size=8).hexdigest()
            if counts[2]:
                idx = np.empty(counts[3], dtype=np.int32)
                me.loops.foreach_get("vertex_index", idx)
                mat = np.empty(counts[2], dtype=np.int32)
                me.polygons.foreach_get("material_index", mat)
                out["topology"] = hashlib.blake2b(idx.tobytes() + mat.tobytes(), digest_size=8).hexdigest()
        except Exception:
            pass
    out["attributes"] = _digest(tuple(sorted((a.name, a.domain, a.data_type) for a in me.attributes))
                                + tuple(uv.name for uv in me.uv_layers))
    # Material slots are reported once, on the objects that show them, not again on the mesh.
    if me.shape_keys is not None:
        out["shape_keys"] = _digest(tuple((k.name, _round(k.value), k.mute) for k in me.shape_keys.key_blocks))
    return out


def _modifier_row(md) -> tuple:
    row = [md.name, md.type, tuple(sorted(rna_values(md, MODIFIER_SKIP).items()))]
    if md.type == "NODES":
        try:
            row.append(tuple(sorted((k, repr(md[k])[:200]) for k in md.keys())))
        except Exception:
            pass
    return tuple(row)


def _object_aspects(ob) -> dict:
    rot = (tuple(_round(v) for v in ob.rotation_euler), tuple(_round(v) for v in ob.rotation_quaternion),
           tuple(_round(v) for v in ob.rotation_axis_angle), ob.rotation_mode)
    transform = (tuple(_round(v) for v in ob.location), rot, tuple(_round(v) for v in ob.scale),
                 tuple(_round(v) for v in ob.delta_location), tuple(_round(v) for v in ob.delta_rotation_euler),
                 tuple(_round(v) for v in ob.delta_scale))
    data = ob.data
    return {
        "transform": _digest(transform),
        "parent": _digest((ob.parent.name if ob.parent else None, ob.parent_type, ob.parent_bone)),
        "visibility": _digest((ob.hide_viewport, ob.hide_render, ob.hide_select, ob.display_type,
                               getattr(ob, "visible_camera", True), getattr(ob, "visible_shadow", True))),
        "materials": _digest(tuple((slot.link, slot.material.name if slot.material else None)
                                   for slot in ob.material_slots)),
        "data": _digest(data.name if data else None),
        # Catches ob.data = new_mesh when the new mesh has the old name. Session-only, so not in signature().
        "data_uid": _digest(getattr(data, "session_uid", None) if data else None),
        "modifiers": _digest(tuple(_modifier_row(md) for md in ob.modifiers)),
        "constraints": _digest(tuple((c.name, c.type, c.mute, _round(c.influence),
                                      getattr(getattr(c, "target", None), "name", None)) for c in ob.constraints)),
        "collections": _digest(tuple(sorted(c.name for c in ob.users_collection))),
        "props": _digest(_custom_props(ob)),
        "animation": _digest(_anim_action(ob)),
        "instancing": _digest((ob.instance_type, ob.instance_collection.name if ob.instance_collection else None)),
    }


def scene_values(scene) -> dict:
    """Flat {path: value} of the scene settings that decide what a render looks like."""
    out = {}
    for key in SCENE_TOP:
        if not hasattr(scene, key):
            continue
        try:
            value = getattr(scene, key)
        except Exception:
            continue
        out[key] = value.name if isinstance(value, bpy.types.ID) else _round(value)
    for path in SCENE_STRUCTS:
        struct = scene
        try:
            for part in path.split("."):
                struct = getattr(struct, part)
        except AttributeError:
            continue
        for key, value in rna_values(struct, {"filepath"} if path == "render" else ()).items():
            out[f"{path}.{key}"] = value
    for layer in scene.view_layers:
        out[f"view_layers[{layer.name}].use"] = layer.use
    return out


def _with_tree(idb) -> dict:
    out = {"settings": _digest(tuple(sorted(rna_values(idb).items()))), "props": _digest(_custom_props(idb)),
           "animation": _digest(_anim_action(idb))}
    tree = getattr(idb, "node_tree", None)
    if tree is not None:
        out.update(tree_aspects(tree))
        # Material and world keyframes live on the embedded node tree, not on the material.
        out["tree_animation"] = _digest(_anim_action(tree))
    return out


def _aspects(attr: str, idb, hash_coords: bool) -> dict:
    if attr == "objects":
        return _object_aspects(idb)
    if attr == "meshes":
        return _mesh_aspects(idb, hash_coords)
    if attr == "node_groups":
        out = tree_aspects(idb)
        out["animation"] = _digest(_anim_action(idb))
        return out
    if attr == "actions":
        return {"keys": _keys_digest(action_fcurves(idb)),
                "slots": _digest(tuple(s.identifier for s in getattr(idb, "slots", [])))}
    if attr == "collections":
        return {"objects": _digest(tuple(sorted(o.name for o in idb.objects))),
                "children": _digest(tuple(sorted(c.name for c in idb.children))),
                "settings": _digest((idb.hide_render, idb.hide_viewport, idb.hide_select))}
    if attr == "texts":
        return {"text": _digest(idb.as_string())}
    if attr == "images":
        # Not image.size: reading it loads the pixels from disk.
        return {"settings": _digest((idb.filepath, idb.source, idb.colorspace_settings.name,
                                     idb.packed_file is not None, idb.alpha_mode))}
    if attr == "scenes":
        return {key: _digest(value) for key, value in scene_values(idb).items()}
    if attr == "libraries":
        return {"filepath": _digest(idb.filepath)}
    return _with_tree(idb)


def kept_on_save() -> set | None:
    """session_uids of the datablocks that survive a save and reload.

    Blender drops datablocks nobody uses. That cascades: a material used only by an orphaned mesh
    is written once, comes back with no users, and is dropped on the next save. So this walks from
    what Blender always keeps (scenes, texts, UI data, fake users) along bpy.data.user_map().
    """
    try:
        users_of = bpy.data.user_map()
    except Exception:
        return None
    uses = {}
    for used, users in users_of.items():
        for user in users:
            uses.setdefault(user.session_uid, []).append(used)
    root_types = (bpy.types.Scene, bpy.types.Text, bpy.types.Library, bpy.types.WindowManager,
                  bpy.types.WorkSpace, bpy.types.Screen)
    stack = [idb for idb in users_of if isinstance(idb, root_types) or idb.use_fake_user
             or getattr(idb, "use_extra_user", False)]
    seen = set()
    while stack:
        idb = stack.pop()
        uid = idb.session_uid
        if uid in seen:
            continue
        seen.add(uid)
        stack.extend(uses.get(uid, ()))
    return seen


def _tracked(attr: str, idb, kept: set | None) -> bool:
    if skip_name(idb.name):
        return False
    if attr == "images" and idb.type in {"RENDER_RESULT", "COMPOSITING"}:
        return False
    # Orphans are not saved, so they are not part of the scene: a mesh left behind by a deleted object
    # counts as removed, and a live session compares equal to its saved copy.
    if attr not in KEEP_UNUSED:
        if kept is not None:
            return idb.session_uid in kept
        return idb.users > 0 or idb.use_fake_user
    return True


def take(hash_coords: bool | None = None) -> dict:
    """{(attr, session_uid): (name, {aspect: hash})} for every datablock VSBlender tracks.

    The raw scene values are kept under ("_scene_values", 0) so a diff can name old and new values.
    """
    if hash_coords is None:
        try:
            hash_coords = sum(len(me.vertices) for me in bpy.data.meshes) <= COORD_HASH_LIMIT
        except Exception:
            hash_coords = False
    snap = {}
    raw_scenes = {}
    kept = kept_on_save()
    for attr in COLLECTIONS:
        collection = getattr(bpy.data, attr, None)
        if collection is None:
            continue
        for idb in collection:
            if not _tracked(attr, idb, kept):
                continue
            uid = getattr(idb, "session_uid", None) or idb.as_pointer()
            try:
                aspects = _aspects(attr, idb, hash_coords)
            except Exception as exc:  # One broken datablock must not hide everything else.
                aspects = {"error": str(exc)}
            snap[(attr, uid)] = (idb.name, aspects)
            if attr == "scenes":
                raw_scenes[idb.name] = scene_values(idb)
    snap[("_scene_values", 0)] = ("", raw_scenes)
    return snap


def signature(snap: dict | None = None) -> str:
    """One hash of a snapshot without session ids, so two Blender sessions can be compared."""
    snap = take() if snap is None else snap
    rows = sorted((attr, name, tuple(sorted((k, v) for k, v in aspects.items() if k not in VOLATILE)))
                  for (attr, _uid), (name, aspects) in snap.items() if attr != "_scene_values")
    return hashlib.sha256(repr(rows).encode("utf-8", "replace")).hexdigest()


def derived_signature(objects) -> str:
    """Hash of where the source objects of a generated object are, and what shape they have.

    vsblender.mark_derived() stores it on the generated object; the ingester recomputes it and warns
    when a source moved since. Rounded coarsely so a save and reload gives the same value.
    """
    rows = []
    for ob in sorted(objects, key=lambda o: o.name):
        matrix = tuple(round(v, 3) + 0.0 for row in ob.matrix_world for v in row)
        dims = tuple(round(v, 3) + 0.0 for v in ob.dimensions)
        data = ob.data
        count = len(data.vertices) if isinstance(data, bpy.types.Mesh) else None
        rows.append((ob.name, matrix, dims, data.name if data else None, count))
    return hashlib.blake2b(repr(rows).encode("utf-8"), digest_size=8).hexdigest()


def typed(attr: str, name: str) -> str:
    return f"{COLLECTIONS.get(attr, attr.upper()[:2])}:{name}"


_SINGULAR = {"meshes": "mesh", "node_groups": "node group", "grease_pencils": "grease pencil",
             "grease_pencils_v3": "grease pencil", "hair_curves": "hair curves", "pointclouds": "point cloud",
             "lightprobes": "light probe", "cache_files": "cache file", "movieclips": "movie clip",
             "libraries": "library", "particles": "particle system", "fonts": "font"}


def singular(attr: str) -> str:
    """'meshes' -> 'mesh', 'objects' -> 'object': for messages."""
    return _SINGULAR.get(attr, attr[:-1] if attr.endswith("s") else attr)


def diff(before: dict, after: dict, depsgraph: dict | None = None) -> dict:
    """Categorised report: added, removed, recreated, renamed, and modified[category][name] = [aspects]."""
    keys_before = {k for k in before if k[0] != "_scene_values"}
    keys_after = {k for k in after if k[0] != "_scene_values"}
    added = {(k[0], after[k][0]) for k in keys_after - keys_before}
    removed = {(k[0], before[k][0]) for k in keys_before - keys_after}
    # Same type and name gone and back with a new session id: the script deleted and rebuilt it.
    recreated = added & removed
    added -= recreated
    removed -= recreated
    renamed = []
    modified: dict = {}
    raw_before = before.get(("_scene_values", 0), ("", {}))[1]
    raw_after = after.get(("_scene_values", 0), ("", {}))[1]
    for key in keys_before & keys_after:
        attr = key[0]
        name_b, asp_b = before[key]
        name_a, asp_a = after[key]
        if name_b != name_a:
            renamed.append({"from": typed(attr, name_b), "to": typed(attr, name_a)})
        changed = sorted({"data" if k == "data_uid" else k
                          for k in set(asp_b) | set(asp_a) if asp_b.get(k) != asp_a.get(k)})
        if not changed:
            continue
        if attr == "scenes":
            old, new = raw_before.get(name_b, {}), raw_after.get(name_a, {})
            changed = [f"{path}: {old.get(path)!r} -> {new.get(path)!r}" for path in changed][:40]
        modified.setdefault(attr, {})[name_a] = changed
    # The depsgraph only adds what the snapshot could not see: geometry edits when coordinates were
    # not hashed. Where they were, a modifier change would show up a second time as a mesh update.
    rebuilt = added | recreated
    hashed = {(k[0], name) for k, (name, aspects) in after.items() if k[0] != "_scene_values" and "geometry" in aspects}
    names_before = {(k[0], before[k][0]) for k in keys_before}
    names_after = {(k[0], after[k][0]) for k in keys_after}
    temporary = set()
    for attr, names in (depsgraph or {}).items():
        for name in names:
            if (attr, name) not in names_after:
                # Built and removed again by the script (an import it measured and deleted), or removed:
                # neither is an edit of something that is still there.
                if (attr, name) not in names_before:
                    temporary.add(typed(attr, name))
                continue
            if (attr, name) in rebuilt or (attr, name) in hashed or name in modified.get(attr, {}):
                continue
            modified.setdefault(attr, {})[name] = ["geometry (depsgraph)"]
    out = {
        "added": sorted(typed(a, n) for a, n in added),
        "removed": sorted(typed(a, n) for a, n in removed),
        "recreated": sorted(typed(a, n) for a, n in recreated),
        "renamed": sorted(renamed, key=lambda item: item["to"]),
        "modified": {attr: dict(sorted(items.items())) for attr, items in sorted(modified.items())},
    }
    if temporary:
        out["temporary"] = sorted(temporary)
    return out


def flat(report: dict) -> list:
    """Typed ids of everything in a report, for callers that only want a list."""
    ids = set(report.get("added", [])) | set(report.get("removed", [])) | set(report.get("recreated", []))
    ids |= {item["to"] for item in report.get("renamed", [])}
    for attr, items in report.get("modified", {}).items():
        ids |= {typed(attr, name) for name in items}
    return sorted(ids)


def is_empty(report: dict) -> bool:
    return not flat(report)


def summary(report: dict, limit: int = 12) -> str:
    """One line for a journal or change log: "added OB:A; materials SG Stone (nodes); scene: render.engine"."""
    parts = []
    for key in ("added", "removed", "recreated"):
        items = report.get(key) or []
        if items:
            more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
            parts.append(f"{key} {', '.join(items[:limit])}{more}")
    if report.get("renamed"):
        parts.append("renamed " + ", ".join(f"{r['from']} -> {r['to']}" for r in report["renamed"][:limit]))
    for attr, items in (report.get("modified") or {}).items():
        names = list(items)
        if attr == "scenes":
            for name in names:
                paths = [entry.split(":", 1)[0] for entry in items[name]]
                parts.append(f"scene {name}: {', '.join(paths[:limit])}")
            continue
        shown = ", ".join(f"{n} ({'/'.join(items[n][:3])})" for n in names[:limit])
        more = f" (+{len(names) - limit} more)" if len(names) > limit else ""
        parts.append(f"{attr} {shown}{more}")
    temporary = report.get("temporary") or []
    if temporary and parts:
        parts.append(f"temporary {', '.join(temporary[:limit])} (added and removed)")
    return "; ".join(parts) or ("no changes" + (f" (temporary {', '.join(temporary[:limit])}: added and removed)" if temporary else ""))


class DepsgraphRecorder:
    """Records geometry updates from depsgraph_update_post while a script runs.

    Only geometry datablocks, not objects: an object is re-evaluated for many reasons that are not
    edits (a camera after a resolution change, a lamp after an energy change). Mesh edits are also
    caught by the coordinate hash; this covers files too large to hash.
    The handler only fires when the depsgraph is evaluated, so call flush() before leaving.
    """

    _MAP = {"Mesh": "meshes", "Curve": "curves", "Curves": "hair_curves",
            "PointCloud": "pointclouds", "Volume": "volumes", "GreasePencil": "grease_pencils_v3"}

    def __init__(self):
        self.updates: dict = {}

    def _handler(self, _scene, depsgraph):
        try:
            for update in depsgraph.updates:
                if not update.is_updated_geometry:
                    continue
                idb = getattr(update.id, "original", None) or update.id
                attr = self._MAP.get(type(idb).__name__)
                if attr is None or skip_name(idb.name):
                    continue
                self.updates.setdefault(attr, {})[idb.name] = True
        except Exception:
            pass

    def __enter__(self):
        self.flush()  # Evaluate what was already pending, so it is not blamed on the script.
        bpy.app.handlers.depsgraph_update_post.append(self._handler)
        return self

    @staticmethod
    def flush():
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

    def __exit__(self, *_exc):
        try:
            bpy.app.handlers.depsgraph_update_post.remove(self._handler)
        except ValueError:
            pass
        return False

"""
blender_ingest.py - build AI context files for a .blend the assistant hasn't seen before (or that changed since).

Run headless. The .blend is only read, never saved:
    blender -b --factory-startup --disable-autoexec FILE.blend --python-exit-code 1 --python blender_ingest.py -- [options]

The VSBlender add-on also imports this module to ingest the live session (ingest live=true): the same
manifest, roles and notes, built from what is open in Blender, with offscreen previews.

Options:
    --out DIR               output folder (default: <blend dir>/.blender-ai/<blend name>/)
    --force                 re-ingest even if the file is unchanged since last time
    --actor NAME            who made the change being recorded: external (default), ai, human
    --reason TEXT           why the file changed (goes into journal.jsonl and the NOTES.md change log)
    --no-previews           skip preview renders
    --no-lookdev            render the scene-camera preview with Workbench instead of EEVEE
    --ignore-annotations    infer every role even where the file already carries role/description properties
    --preview-size N        longest preview edge in pixels (default 768)
    --max-group-previews N  focused previews of the largest groups (default 8)
    --manifest-to FILE      only write the manifest to FILE (used to diff checkpoints)
    --diff A B --diff-to F  compare two manifest files and write the changes to F (no .blend needed)

Outputs (in --out):
    state.json      fingerprint (sha256) deciding "seen before / changed since"
    manifest.json   deterministic structural description: hierarchy, transforms, meshes, modifiers, materials as
                    node graphs, animation, images, libraries, issues. Text, so git diffs of it read as a changelog.
    roles.json      what each object is for: from annotations in the file, or inferred with evidence + confidence
    NOTES.md        overview for the AI and people; hand-written sections survive re-ingest
    journal.jsonl   append-only history: first ingest, then every change detected (who / why / what)
    previews/       scene camera (file's own look), front/right/top/iso, and each large group
    texts/          copies of text blocks embedded in the .blend (never executed)

The last stdout line is `INGEST_RESULT {json}` for the calling plugin.
"""
import argparse
import ast
import datetime
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict

import bmesh
import bpy
from mathutils import Vector


def _load_snapshot():
    """snapshot.py sits next to this file. In the add-on it is a package module; headless it is loaded by path."""
    try:
        from . import snapshot  # noqa: PLC0415 - only valid inside the add-on package
        return snapshot
    except ImportError:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot.py")
        spec = importlib.util.spec_from_file_location("vsblender_snapshot", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


_snapshot = _load_snapshot()

INGEST_VERSION = 3
GEOM_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL", "GPENCIL"}
GENERIC_TOKENS = {"cube", "plane", "sphere", "ico", "icosphere", "cylinder", "cone", "torus", "circle", "grid", "mesh",
                  "object", "obj", "geo", "geometry", "empty", "default", "untitled", "new", "copy", "final", "low",
                  "high", "mat", "material", "shape", "bezier", "curve", "nurbs", "text", "blend", "the", "and"}
ANNOTATION_KEY = re.compile(r"(^|[_.\s])(role|purpose|description|desc|notes?|intent)$", re.I)
OBJ_REF_KEYS = {"object", "offset_object", "mirror_object", "target", "curve", "start_cap", "end_cap", "auxiliary_target"}
NODE_SKIP = {"location", "location_absolute", "width", "height", "name", "label", "select", "show_options",
             "show_preview", "hide", "mute", "show_texture", "use_custom_color", "color", "parent",
             "warning_propagation", "is_active_output", "color_tag", "target"}
MOD_SKIP = {"name", "show_viewport", "show_render", "show_in_editmode", "show_on_cage", "show_expanded",
            "is_active", "use_pin_to_last", "persistent_uid", "is_override_data", "use_apply_on_spline"}
GEN_BEGIN, GEN_END = "<!-- ai:generated:begin -->", "<!-- ai:generated:end -->"


# ----------------------------------------------------------------------------- small helpers
def rnd(v, n=4):
    if isinstance(v, float):
        r = round(v, n)
        return 0.0 if r == 0 else r
    if isinstance(v, (list, tuple)):
        return [rnd(x, n) for x in v]
    return v


def vec(v, n=4):
    return [rnd(float(x), n) for x in v]


def fmt(v):
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, list):
        return "(" + ", ".join(f"{x:.2f}" if isinstance(x, float) else str(x) for x in v) + ")"
    return str(v)


def jsonable(v):
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return rnd(v)
    if isinstance(v, bpy.types.ID):
        return v.name
    if hasattr(v, "to_dict"):
        return {k: jsonable(x) for k, x in v.to_dict().items()}
    if hasattr(v, "to_list"):
        return jsonable(v.to_list())
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    try:
        return [jsonable(x) for x in v]
    except TypeError:
        return str(v)


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "unnamed"


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def name_tokens(name):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[^A-Za-z]+", s) if len(t) > 2 and t.lower() not in GENERIC_TOKENS]


def series_base(name):
    return re.sub(r"[\s._-]*\d+$", "", name)


def series_label(name):
    """The number that ends a series member's name ('AG Chevron 7' -> '7'), or the name."""
    match = re.search(r"(\d+)$", name)
    return match.group(1) if match else name


def natural_key(name):
    """Sort 'Rock 2' before 'Rock 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


_ROLE_PROP = re.compile(r"(^|[_.\s])(role|purpose|description|desc|notes?|intent)$", re.I)


def root_props(o):
    """Custom properties of a root object worth showing in the tree: what a script recorded about the
    whole build (ag_dial = 'locks at frames ...'), not roles (already shown) or VSBlender's stamps."""
    out = {}
    for k, v in (o.get("custom_props") or {}).items():
        if _ROLE_PROP.search(k) or k.startswith(("ai_", "vsblender_", "_")):
            continue
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        out[k] = text if len(text) <= 240 else text[:237] + "..."
        if len(out) >= 6:
            break
    return out


def custom_props(idb):
    out = {}
    for k in sorted(idb.keys()):
        if k.startswith("_") or k in ("cycles", "cycles_visibility"):
            continue
        # Provenance stamps and specs are reported on their own (built_by, spec, appended_from).
        if k.startswith(("ai_built_", "ai_modified_", "ai_appended_")) or k == "ai_spec":
            continue
        v = jsonable(idb[k])
        if isinstance(v, str) and len(v) > 2000:
            v = v[:2000] + "..."
        out[k] = v
    return out


def prop_values(s, skip=()):
    """(value, RNA default) of every writable scalar, enum and string property, plus ID pointers."""
    out = {}
    for p in s.bl_rna.properties:
        k = p.identifier
        if k == "rna_type" or k in skip or k.startswith("bl_") or p.is_readonly:
            continue
        try:
            v = getattr(s, k)
            if p.type in {"BOOLEAN", "INT", "FLOAT"}:
                if p.is_array:
                    v, d = rnd([x for x in v]), rnd(list(p.default_array))
                else:
                    v, d = rnd(v), rnd(p.default)
            elif p.type == "ENUM":
                v, d = (sorted(v), sorted(p.default_flag)) if p.is_enum_flag else (v, p.default)
            elif p.type == "STRING":
                d = p.default
            elif p.type == "POINTER":
                if isinstance(v, bpy.types.ID):
                    out[k] = (v.name, None)
                continue
            else:
                continue
            out[k] = (v, d)
        except Exception:
            continue
    return out


def nondefault(s, skip=()):
    """RNA properties whose value differs from the default. Version-proof way to describe any struct."""
    return {k: v for k, (v, d) in prop_values(s, skip).items() if v != d}


def sockval(s):
    try:
        v = s.default_value
    except Exception:
        return None
    if isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return rnd(v)
    if isinstance(v, bpy.types.ID):
        return v.name
    try:
        return vec(v)
    except (TypeError, ValueError):
        return None


_NODE_DEFAULTS = {}


def _node_baseline(bl_idname, tree_type):
    """Socket values and properties of a freshly added node, which is what "unchanged" means.

    RNA defaults are not enough: a new Principled BSDF is MULTI_GGX / RANDOM_WALK although the RNA
    default says otherwise, so comparing against RNA flagged every one of them as edited.
    """
    key = (tree_type, bl_idname)
    if key not in _NODE_DEFAULTS:
        sockets, props = {}, {}
        ng = bpy.data.node_groups.new("_ingest_scratch", tree_type)
        try:
            n = ng.nodes.new(bl_idname)
            sockets = {s.identifier: sockval(s) for s in n.inputs if hasattr(s, "default_value")}
            props = {k: v for k, (v, _d) in prop_values(n, NODE_SKIP).items()}
        except Exception:
            pass
        bpy.data.node_groups.remove(ng)
        _NODE_DEFAULTS[key] = (sockets, props)
    return _NODE_DEFAULTS[key]


def node_defaults(bl_idname, tree_type):
    return _node_baseline(bl_idname, tree_type)[0]


def changed_props(n):
    """Node properties that differ from a freshly added node of the same type."""
    _sockets, base = _node_baseline(n.bl_idname, n.id_data.bl_idname)
    out = {}
    for k, (v, d) in prop_values(n, NODE_SKIP).items():
        if k in base:
            if base[k] != v:
                out[k] = v
        elif v != d:
            out[k] = v
    return out


def changed_inputs(n):
    dflt = node_defaults(n.bl_idname, n.id_data.bl_idname)
    out = {}
    for s in n.inputs:
        if s.is_linked or not getattr(s, "enabled", True) or not hasattr(s, "default_value"):
            continue
        v = sockval(s)
        if v is not None and (s.identifier not in dflt or dflt[s.identifier] != v):
            out[s.identifier] = (s.name, v)
    return out


# ----------------------------------------------------------------------------- node trees
def node_short(n):
    bits = [v for v in changed_props(n).values() if isinstance(v, str)]
    for name, v in changed_inputs(n).values():
        if len(bits) >= 6:
            break
        if not isinstance(v, str):
            bits.append(f"{name}={fmt(v)}")
    if n.type == "VALTORGB":
        bits.append(f"{len(n.color_ramp.elements)} stops")
    label = n.label or n.bl_label
    return f"{label}[{', '.join(bits)}]" if bits else label


def chain_summary(n, depth=0, seen=frozenset()):
    me = node_short(n)
    if depth >= 6 or n.name in seen:
        return me
    parts = [f"{s.name} <- {chain_summary(l.from_node, depth + 1, seen | {n.name})}"
             for s in n.inputs for l in s.links if not l.is_muted]
    return f"{me}({'; '.join(parts)})" if parts else me


def reachable(n, acc=None):
    acc = set() if acc is None else acc
    if n.name in acc:
        return acc
    acc.add(n.name)
    for s in n.inputs:
        for l in s.links:
            if not l.is_muted:
                reachable(l.from_node, acc)
    return acc


def node_info(n):
    d = {"type": n.bl_idname}
    if n.label:
        d["label"] = n.label
    props = changed_props(n)
    if props:
        d["props"] = props
    vals = {ident: v for ident, (_, v) in changed_inputs(n).items()}
    if vals:
        d["inputs"] = vals
    if n.type == "VALTORGB":
        cr = n.color_ramp
        d["ramp"] = {"interpolation": cr.interpolation,
                     "stops": [[rnd(e.position, 3), vec(e.color, 3)] for e in cr.elements]}
    if getattr(n, "node_tree", None) is not None:
        d["group"] = n.node_tree.name
    if getattr(n, "image", None) is not None:
        d["image"] = n.image.name
    return d


def tree_info(nt, output_type):
    outs = [n for n in nt.nodes if n.type == output_type]
    out = next((n for n in outs if getattr(n, "is_active_output", False)), outs[0] if outs else None)
    live = reachable(out) if out else set()
    live_nodes = [nt.nodes[x] for x in live]

    def principled_has(inp, test):
        for n in live_nodes:
            if n.type == "BSDF_PRINCIPLED" and inp in n.inputs:
                s = n.inputs[inp]
                if s.is_linked or test(sockval(s)):
                    return True
        return False

    return {
        "summary": chain_summary(out) if out else "no output node",
        "nodes": {n.name: node_info(n) for n in sorted(nt.nodes, key=lambda n: n.name)},
        "links": sorted(f"{l.from_node.name}.{l.from_socket.identifier} -> {l.to_node.name}.{l.to_socket.identifier}"
                        + (" (muted)" if l.is_muted else "") for l in nt.links),
        "emissive": any(n.type == "EMISSION" for n in live_nodes)
                    or principled_has("Emission Strength", lambda v: isinstance(v, float) and v > 0),
        "transmissive": any(n.type in {"BSDF_GLASS", "BSDF_TRANSPARENT", "BSDF_REFRACTION"} for n in live_nodes)
                        or principled_has("Transmission Weight", lambda v: isinstance(v, float) and v > 0),
        "images": sorted({n.image.name for n in nt.nodes if n.type == "TEX_IMAGE" and n.image}),
    }


# ----------------------------------------------------------------------------- animation
_SOCKET_PATH = re.compile(r'^nodes\["((?:[^"\\]|\\.)*)"\]\.(inputs|outputs)\[(\d+)\]\.default_value$')


def channel_label(idb, data_path, index):
    """A readable channel name: 'Principled BSDF › Emission Strength' for a socket, and the array
    index only when the property is an array ('location[2]', but 'energy')."""
    label = data_path
    m = _SOCKET_PATH.match(data_path)
    if m is not None and hasattr(idb, "nodes"):
        node = idb.nodes.get(m.group(1).replace('\\"', '"'))
        if node is not None:
            sockets = node.inputs if m.group(2) == "inputs" else node.outputs
            i = int(m.group(3))
            if i < len(sockets):
                label = f"{node.name} › {sockets[i].name}"
    try:
        value = idb.path_resolve(data_path)
        is_array = hasattr(value, "__len__") and not isinstance(value, str)
    except Exception:
        is_array = True
    return f"{label}[{index}]" if is_array else label


def anim_channels(idb):
    ad = getattr(idb, "animation_data", None) if idb is not None else None
    if not ad:
        return None
    out = {}
    act = ad.action
    if act is not None:
        fcs = None
        if getattr(ad, "action_slot", None) is not None:          # layered actions (4.4+)
            try:
                from bpy_extras import anim_utils
                cb = anim_utils.action_get_channelbag_for_slot(act, ad.action_slot)
                fcs = list(cb.fcurves) if cb else []
            except Exception:
                fcs = None
        if fcs is None:
            fcs = list(safe(lambda: act.fcurves, []) or [])
        chans = []
        for fc in fcs:
            a, b = fc.range()
            chans.append({"path": f"{fc.data_path}[{fc.array_index}]",
                          "label": channel_label(idb, fc.data_path, fc.array_index),
                          "keys": len(fc.keyframe_points), "frames": [rnd(a, 1), rnd(b, 1)]})
        out["action"] = act.name
        out["channels"] = sorted(chans, key=lambda c: c["path"])
    drivers = sorted(f"{d.data_path}[{d.array_index}] = {d.driver.expression or d.driver.type}" for d in ad.drivers)
    if drivers:
        out["drivers"] = drivers
    if len(getattr(ad, "nla_tracks", [])):
        out["nla_tracks"] = [t.name for t in ad.nla_tracks]
    return out or None


def _fcurves_of(idb):
    ad = getattr(idb, "animation_data", None) if idb is not None else None
    if not ad or ad.action is None:
        return []
    if getattr(ad, "action_slot", None) is not None:
        try:
            from bpy_extras import anim_utils
            cb = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
            return list(cb.fcurves) if cb else []
        except Exception:
            pass
    return list(safe(lambda: ad.action.fcurves, []) or [])


_HIDE_WORDS = {"hide_render": ("hidden in renders", "shown in renders"),
               "hide_viewport": ("hidden in the viewport", "shown in the viewport")}


def _beat_value(data_path, value):
    # + 0.0 turns -0.0 into 0.0, so a turn starts at 0°, not -0°.
    if "rotation_euler" in data_path or data_path.endswith(("angle", "rotation")):
        return f"{math.degrees(value) + 0.0:.4g}°"
    return f"{value + 0.0:.4g}"


def animation_beats(scene):
    """When things happen: each channel's changes in time order across the scene.

    A beat is a run of keys whose values keep changing: 'f145-154 AG Chevron Light 1 Emission Strength
    0 -> 9 -> 5.5'. Holds (equal neighbouring keys) end a run. A constant key changes the value at the
    next key, so a step lands on that frame. Returns (frame, line) pairs sorted by frame.
    """
    sources = []
    seen = set()

    def add(label, idb):
        if idb is None or idb.as_pointer() in seen:
            return
        seen.add(idb.as_pointer())
        sources.append((label, idb))

    for ob in scene.objects:
        add(f"**{ob.name}**", ob)
        if ob.data is not None:
            add(f"**{ob.name}**", ob.data)
            add(f"**{ob.name}**", getattr(ob.data, "shape_keys", None))
    for coll, prefix in ((bpy.data.materials, "MA:"), (bpy.data.worlds, "WO:")):
        for idb in coll:
            add(f"**{prefix}{idb.name}**", idb)
            add(f"**{prefix}{idb.name}**", getattr(idb, "node_tree", None))
    add(f"**scene {scene.name}**", scene)
    beats = []
    shown = {}  # (frame, label, shown?) -> where: renders and viewport keyed together read as one beat
    for label, idb in sources:
        for fc in _fcurves_of(idb):
            keys = [(float(k.co[0]), float(k.co[1]), k.interpolation) for k in fc.keyframe_points]
            channel = channel_label(idb, fc.data_path, fc.array_index)
            channel = channel.split(" › ", 1)[-1] if " › " in channel else channel
            base_path = fc.data_path.split(".")[-1]
            i = 0
            while i < len(keys) - 1:
                if abs(keys[i + 1][1] - keys[i][1]) <= 1e-9:
                    i += 1
                    continue
                j = i + 1
                while j < len(keys) - 1 and abs(keys[j + 1][1] - keys[j][1]) > 1e-9:
                    j += 1
                run = keys[i:j + 1]
                f0 = run[1][0] if run[0][2] == "CONSTANT" else run[0][0]
                f1 = run[-1][0]
                span = f"f{f0:g}" if f0 == f1 else f"f{f0:g}-{f1:g}"
                if base_path in _HIDE_WORDS and len(run) == 2:
                    where = "renders" if base_path == "hide_render" else "the viewport"
                    shown.setdefault((f0, span, label, not run[-1][1]), []).append(where)
                else:
                    stepped = all(k[2] == "CONSTANT" for k in run[:-1])
                    values = [_beat_value(fc.data_path, k[1]) for k in run]
                    if len(values) <= 4:
                        change = " -> ".join(values)
                    else:
                        change = f"{values[0]} -> {values[-1]} ({len(values)} keys{', stepped' if stepped else ''})"
                    beats.append((f0, f"{span} {label} {channel} {change}"))
                i = j
    for (f0, span, label, visible), places in shown.items():
        places = sorted(set(places))
        beats.append((f0, f"{span} {label} {'shown' if visible else 'hidden'} in {' and '.join(places)}"))
    beats.sort(key=lambda b: (b[0], b[1]))
    return beats


# ----------------------------------------------------------------------------- manifest
def modifier_info(md):
    d = {"name": md.name, "type": md.type, "enabled": [md.show_viewport, md.show_render],
         "settings": nondefault(md, MOD_SKIP)}
    if md.type == "NODES":
        d["inputs"] = {k: jsonable(md[k]) for k in md.keys()}
    return d


def constraint_info(c):
    d = {"name": c.name, "type": c.type, "influence": rnd(c.influence), "enabled": not c.mute}
    t = getattr(c, "target", None)
    if t is not None:
        d["target"] = t.name
        if getattr(c, "subtarget", ""):
            d["subtarget"] = c.subtarget
    return d


def object_info(ob, deps, scene, vl):
    eo = ob.evaluated_get(deps)
    o = {
        "type": ob.type,
        "parent": ob.parent.name if ob.parent else None,
        "children": sorted(c.name for c in ob.children),
        "collections": sorted(c.name for c in ob.users_collection),
        "location": vec(ob.location),
        "scale": vec(ob.scale),
        "rotation_mode": ob.rotation_mode,
        "world_location": vec(eo.matrix_world.translation),
        "hidden": {"viewport": ob.hide_viewport, "render": ob.hide_render,
                   "view_layer": safe(ob.hide_get, None), "in_view_layer": ob.name in vl.objects},
        "data": ob.data.name if ob.data is not None else None,
        "materials": [s.material.name if s.material else None for s in ob.material_slots],
        "modifiers": [modifier_info(m) for m in ob.modifiers],
        "constraints": [constraint_info(c) for c in ob.constraints],
        "custom_props": custom_props(ob),
    }
    if ob.parent and ob.parent_type != "OBJECT":
        o["parent_type"] = ob.parent_type + (f":{ob.parent_bone}" if ob.parent_bone else "")
    if ob.library:
        o["library"] = ob.library.filepath
    if ob.override_library is not None:
        o["library_override"] = True
    if ob.rotation_mode == "QUATERNION":
        o["rotation_quat"] = vec(ob.rotation_quaternion)
    elif ob.rotation_mode == "AXIS_ANGLE":
        o["rotation_axis_angle"] = vec(ob.rotation_axis_angle)
    else:
        o["rotation_deg"] = [rnd(math.degrees(a), 3) for a in ob.rotation_euler]

    if ob.type in GEOM_TYPES:
        if ob.hide_viewport and ob.type == "MESH" and len(ob.data.vertices):
            # Hidden in the viewport (often by keys): the depsgraph does not evaluate it, so its evaluated
            # bounds are empty. Its own mesh, placed by its own and its parents' transforms, instead.
            m = ob.matrix_basis.copy()
            child = ob
            while child.parent is not None:
                m = child.parent.matrix_basis @ child.matrix_parent_inverse @ m
                child = child.parent
            pts = [m @ Vector(c) for c in ob.bound_box] if any(any(c) for c in ob.bound_box) else \
                [m @ v.co for v in ob.data.vertices[:200000]]
            o["evaluated"] = False
            size = [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]
            o["dimensions"] = vec(size)
        else:
            pts = [eo.matrix_world @ Vector(c) for c in eo.bound_box]
            o["dimensions"] = vec(eo.dimensions)
        o["bbox"] = [vec([min(p[i] for p in pts) for i in range(3)]), vec([max(p[i] for p in pts) for i in range(3)])]

    if ob.type == "MESH":
        o["mesh"] = mesh_info(ob, eo)
    elif ob.type == "LIGHT":
        L = ob.data
        o["light"] = {"light_type": L.type, "energy": rnd(L.energy), "color": vec(L.color, 3)}
        for k in ("shape", "size", "spot_size", "angle", "shadow_soft_size"):
            if hasattr(L, k):
                v = getattr(L, k)
                o["light"][k] = rnd(math.degrees(v), 2) if k in ("spot_size", "angle") else jsonable(v)
    elif ob.type == "CAMERA":
        C = ob.data
        o["camera"] = {"type": C.type, "lens": rnd(C.lens, 2), "sensor_width": rnd(C.sensor_width, 2),
                       "ortho_scale": rnd(C.ortho_scale, 3), "clip": [rnd(C.clip_start), rnd(C.clip_end)],
                       "dof": C.dof.use_dof, "is_scene_camera": scene.camera == ob}
    elif ob.type == "EMPTY":
        o["empty"] = {"display": ob.empty_display_type, "size": rnd(ob.empty_display_size),
                      "instance_collection": ob.instance_collection.name
                      if ob.instance_type == "COLLECTION" and ob.instance_collection else None}
    elif ob.type == "ARMATURE":
        o["armature"] = {"bones": len(ob.data.bones), "bone_names": [b.name for b in ob.data.bones][:60]}
    elif ob.type == "FONT":
        o["text"] = ob.data.body[:300]
    elif ob.type in {"CURVE", "SURFACE"}:
        o["curve"] = {"splines": len(ob.data.splines), "bevel_depth": rnd(ob.data.bevel_depth),
                      "extrude": rnd(ob.data.extrude)}

    anim = {k: v for k, v in (("object", anim_channels(ob)),
                              ("data", anim_channels(ob.data) if ob.data is not None else None),
                              ("shape_keys", anim_channels(getattr(ob.data, "shape_keys", None)))) if v}
    if anim:
        o["animation"] = anim
    derived = derived_info(ob)
    if derived:
        o["derived"] = derived
    built = {k[9:] if k.startswith("ai_built_") else k[3:]: str(ob[k]) for k in ob.keys()
             if k in ("ai_built_by", "ai_built_sha", "ai_built_at", "ai_built_reason", "ai_modified_by", "ai_modified_at")}
    if built:
        o["built_by"] = built
    if ob.get("ai_appended_from"):
        o["appended_from"] = {"file": str(ob["ai_appended_from"]), "object": str(ob.get("ai_appended_object") or ob.name)}
    if isinstance(ob.get("ai_spec"), str):
        try:
            o["spec"] = json.loads(ob["ai_spec"])
        except ValueError:
            o["spec"] = {"text": str(ob["ai_spec"])[:500]}
    return o


def derived_info(ob):
    """vsblender.mark_derived() stamps generated objects; say whether their sources moved since."""
    if "ai_derived_from" not in ob.keys():
        return None
    names = [n.strip() for n in str(ob["ai_derived_from"]).split(",") if n.strip()]
    sources = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
    out = {"from": names, "script": str(ob.get("ai_regenerate_with", "")) or None}
    missing = [n for n in names if n not in bpy.data.objects]
    if missing:
        out["missing"] = missing
    stored = ob.get("ai_derived_signature")
    if stored is not None:
        out["stale"] = bool(missing) or _snapshot.derived_signature(sources) != str(stored)
    return out


def mesh_info(ob, eo):
    import numpy as np
    me = ob.data
    nf = len(me.polygons)
    info = {"verts": len(me.vertices), "edges": len(me.edges), "faces": nf, "users": me.users,
            "uv_layers": [uv.name for uv in me.uv_layers],
            "color_attributes": [a.name for a in getattr(me, "color_attributes", [])],
            "vertex_groups": [g.name for g in ob.vertex_groups][:60],
            "shape_keys": [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else []}
    if nf:
        lt = np.empty(nf, dtype=np.int32)
        me.polygons.foreach_get("loop_total", lt)
        mi = np.empty(nf, dtype=np.int32)
        me.polygons.foreach_get("material_index", mi)
        info["tris"] = int((lt - 2).sum())
        info["ngons"] = int((lt > 4).sum())
        info["faces_per_material_slot"] = {str(i): int(c) for i, c in enumerate(np.bincount(mi)) if c}
    if len(me.vertices) <= 300_000:
        bm = bmesh.new()
        bm.from_mesh(me)
        info["boundary_edges"] = sum(1 for e in bm.edges if e.is_boundary)
        info["non_manifold_edges"] = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary)
        info["loose_verts"] = sum(1 for v in bm.verts if not v.link_edges)
        bm.free()
    if len(ob.modifiers):
        em = safe(eo.to_mesh)
        if em is not None:
            info["evaluated"] = {"verts": len(em.vertices), "faces": len(em.polygons)}
            eo.to_mesh_clear()
    return info


def material_info(mat):
    d = {"users": mat.users, "fake_user": mat.use_fake_user, "viewport_color": vec(mat.diffuse_color, 3)}
    if mat.library:
        d["library"] = mat.library.filepath
    if mat.node_tree is None:
        d["summary"] = "no node tree (viewport colour only)"
        return d
    d.update(tree_info(mat.node_tree, "OUTPUT_MATERIAL"))
    return d


def text_info(t):
    """Line count, and for Python text blocks the public names a script can import from them."""
    d = {"lines": len(t.lines), "external_file": t.filepath or None, "use_module": t.use_module}
    source = t.as_string()
    if t.name.endswith(".py") or source.lstrip().startswith(("import ", "from ", "def ", "class ", '"""')):
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            d["python"] = f"does not parse: line {e.lineno}: {e.msg}"
            return d
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names += [x.id for x in node.targets if isinstance(x, ast.Name) and x.id.isupper()]
        d["exports"] = [n for n in names if not n.startswith("_")]
        d["import"] = f'bpy.data.texts["{t.name}"].as_module()'
    return d


def build_manifest(blend_path, sha):
    scene = bpy.context.scene
    vl = bpy.context.view_layer
    deps = bpy.context.evaluated_depsgraph_get()
    r, us = scene.render, scene.unit_settings

    objs = {ob.name: object_info(ob, deps, scene, vl) for ob in sorted(scene.objects, key=lambda o: o.name)}

    def col_tree(col):
        d = {"objects": sorted(o.name for o in col.objects)}
        if col.children:
            d["children"] = {c.name: col_tree(c) for c in sorted(col.children, key=lambda c: c.name)}
        if getattr(col, "hide_render", False):
            d["hide_render"] = True
        return d

    images = {}
    for img in sorted(bpy.data.images, key=lambda i: i.name):
        if img.type in {"RENDER_RESULT", "COMPOSITING"}:
            continue
        absp = bpy.path.abspath(img.filepath, library=img.library) if img.filepath else ""
        packed = img.packed_file is not None
        images[img.name] = {"filepath": img.filepath, "packed": packed, "source": img.source, "users": img.users,
                            "colorspace": img.colorspace_settings.name,
                            "missing": bool(img.source == "FILE" and not packed and not (absp and os.path.exists(absp)))}

    world = scene.world
    comp = getattr(scene, "compositing_node_group", None)
    anim = {}
    for coll, label in ((bpy.data.materials, "material"), (bpy.data.worlds, "world"),
                        (bpy.data.node_groups, "node_group"), (bpy.data.scenes, "scene")):
        for idb in coll:
            a = anim_channels(idb)
            b = anim_channels(getattr(idb, "node_tree", None)) if label in ("material", "world") else None
            if a or b:
                anim[f"{label}:{idb.name}"] = {k: v for k, v in (("id", a), ("node_tree", b)) if v}

    types = Counter(o["type"] for o in objs.values())
    meshes = [o["mesh"] for o in objs.values() if "mesh" in o]
    size_mb = rnd(os.path.getsize(blend_path) / 1e6, 2) if blend_path and os.path.exists(blend_path) else 0.0
    man = {
        "ingest_version": INGEST_VERSION,
        # bpy.data.version is (major, minor, file subversion): 5.2.45 is not a Blender release.
        "file": {"name": os.path.basename(blend_path) if blend_path else "untitled.blend", "size_mb": size_mb,
                 "saved_with": f"{bpy.data.version[0]}.{bpy.data.version[1]}", "file_subversion": bpy.data.version[2],
                 "ingested_with": bpy.app.version_string,
                 "other_scenes": sorted(s.name for s in bpy.data.scenes if s != scene)},
        "scene": {"name": scene.name, "engine": r.engine,
                  "resolution": [r.resolution_x, r.resolution_y, r.resolution_percentage],
                  "fps": rnd(r.fps / r.fps_base, 3), "frame_range": [scene.frame_start, scene.frame_end],
                  "frame_current": scene.frame_current,
                  "camera": scene.camera.name if scene.camera else None,
                  "world": world.name if world else None,
                  "world_summary": tree_info(world.node_tree, "OUTPUT_WORLD")["summary"]
                  if world and world.node_tree else None,
                  "units": f"{us.system}, {us.length_unit}, scale {rnd(us.scale_length)}",
                  "color": f"{scene.view_settings.view_transform} / {scene.view_settings.look}",
                  "compositor": comp.name if comp else None,
                  "view_layers": [v.name for v in scene.view_layers]},
        "collections": col_tree(scene.collection),
        "objects": objs,
        "objects_not_in_scene": sorted(o.name for o in bpy.data.objects if o.name not in objs),
        "materials": {m.name: material_info(m) for m in sorted(bpy.data.materials, key=lambda m: m.name)},
        "images": images,
        "node_groups": {g.name: {"type": g.bl_idname, "users": g.users, "nodes": len(g.nodes)}
                        for g in sorted(bpy.data.node_groups, key=lambda g: g.name)},
        "animation": anim,
        "libraries": [{"name": l.name, "filepath": l.filepath,
                       "exists": os.path.exists(bpy.path.abspath(l.filepath))} for l in bpy.data.libraries],
        "texts": {t.name: text_info(t) for t in sorted(bpy.data.texts, key=lambda t: t.name)},
        "stats": {"objects_by_type": dict(sorted(types.items())),
                  "verts": sum(m["verts"] for m in meshes), "faces": sum(m["faces"] for m in meshes),
                  "faces_after_modifiers": sum(m.get("evaluated", m)["faces"] for m in meshes),
                  "materials": len(bpy.data.materials), "images": len(images)},
    }
    man["beats"] = [text for _frame, text in safe(lambda: animation_beats(scene), []) or []][:400]
    man["issues"] = build_issues(man)
    return man


# ----------------------------------------------------------------------------- issues ("before you modify")
def build_issues(man):
    warn, info, grouped = [], [], defaultdict(list)

    def add(sev, subject, msg):
        (warn if sev == "warn" else info).append({"severity": sev, "subject": subject, "message": msg})

    for n, img in man["images"].items():
        if img["missing"]:
            add("warn", f"image:{n}", f"texture file missing: {img['filepath']}")
    for lib in man["libraries"]:
        if not lib["exists"]:
            add("warn", f"library:{lib['name']}", f"linked library missing: {lib['filepath']}")
    for n, t in man["texts"].items():
        if t["use_module"]:
            add("warn", f"text:{n}", "registered as a module: runs on file load when auto-run scripts is enabled")
    for n, o in man["objects"].items():
        mods = [m["type"] for m in o["modifiers"]]
        if any(s < 0 for s in o["scale"]):
            add("warn", n, "negative scale (mirrored): applying scale flips normals")
        elif o["type"] == "MESH" and any(abs(s - 1) > 1e-4 for s in o["scale"]) and \
                set(mods) & {"BEVEL", "SOLIDIFY", "ARRAY", "SCREW", "WELD", "REMESH", "DISPLACE"}:
            add("info", n, f"unapplied scale {o['scale']} with {mods}: modifier widths are scaled too")
        me = o.get("mesh")
        if me:
            if me["users"] > 1:
                add("warn", n, f"mesh data '{o['data']}' is shared by {me['users']} objects: editing it changes all of them")
            if me.get("non_manifold_edges"):
                add("info", n, f"{me['non_manifold_edges']} non-manifold edges (booleans / 3D printing may fail)")
            if me.get("ngons") and "SUBSURF" in mods:
                add("info", n, f"{me['ngons']} n-gons under Subdivision Surface (may pinch)")
            if me.get("evaluated", me)["faces"] > 1_000_000:
                add("info", n, "heavy: over 1M faces after modifiers")
            if not me["uv_layers"] and any(man["materials"].get(m, {}).get("images") for m in o["materials"] if m):
                add("warn", n, "uses image textures but has no UV map")
        if o["type"] in GEOM_TYPES and not any(o["materials"]):
            grouped["no material (renders default grey)"].append(n)
        if o["type"] in GEOM_TYPES and not name_tokens(n):
            grouped["generic names, so roles come from geometry only"].append(n)
        if o["hidden"]["viewport"] != o["hidden"]["render"]:
            where = "viewport" if o["hidden"]["viewport"] else "render"
            add("warn", n, f"hidden in {where} only: viewport screenshots and renders won't match")
        a = o.get("animation", {})
        for part, ch in a.items():
            paths = sorted({c["path"].split("[")[0] for c in ch.get("channels", [])})
            if paths:
                add("warn", n, f"keyframed {part} properties {paths}: direct edits are overwritten on frame change, edit the keys")
            if ch.get("drivers"):
                add("warn", n, f"driven properties: {ch['drivers'][:3]}")
        if o["constraints"]:
            add("info", n, f"transform controlled by constraints {[c['type'] for c in o['constraints']]}")
        if o.get("library"):
            add("warn", n, f"linked from {o['library']}: edit the source file or make it local")
        der = o.get("derived")
        if der:
            how = f" by {der['script']}" if der.get("script") else ""
            src = ", ".join(der["from"][:6]) + (f" (+{len(der['from']) - 6} more)" if len(der["from"]) > 6 else "")
            if der.get("missing"):
                add("warn", n, f"generated from {src}{how}, but {', '.join(der['missing'])} no longer exist: regenerate it")
            elif der.get("stale"):
                add("warn", n, f"generated from {src}{how}; those moved or changed since, so it is out of date: re-run the script")
            else:
                add("info", n, f"generated from {src}{how}: change the sources and regenerate, not this object")
    for key, a in man["animation"].items():
        labels = sorted({c.get("label") or c["path"] for part in a.values() for c in part.get("channels", [])})
        if labels:
            add("warn", key, f"keyframed {', '.join(labels[:4])}{' ...' if len(labels) > 4 else ''}: edit the keys, not the values")
    for n, m in man["materials"].items():
        if m["users"] == 0 and not m["fake_user"]:
            grouped["unused materials (dropped on save)"].append(f"material:{n}")
    for msg, names in grouped.items():
        add("info", ", ".join(names[:6]) + (f" (+{len(names) - 6} more)" if len(names) > 6 else ""),
            f"{len(names)} x {msg}")
    return merge_issues(warn) + merge_issues(info)


def merge_issues(items):
    """One line for the same message on several subjects (nine chevron materials with the same keys)."""
    order, by_msg = [], defaultdict(list)
    for item in items:
        key = (item["severity"], item["message"])
        if key not in by_msg:
            order.append(key)
        by_msg[key].append(item["subject"])
    out = []
    for sev, msg in order:
        subjects = by_msg[(sev, msg)]
        if len(subjects) == 1:
            out.append({"severity": sev, "subject": subjects[0], "message": msg})
            continue
        shown = ", ".join(subjects[:6]) + (f" (+{len(subjects) - 6} more)" if len(subjects) > 6 else "")
        out.append({"severity": sev, "subject": shown, "message": msg, "subjects": subjects})
    return out


# ----------------------------------------------------------------------------- roles
def shape_word(d):
    dx, dy, dz = d
    a, b, c = sorted(d)
    if c < 1e-6:
        return "point-like"
    if a < 0.05 * c and b > 0.3 * c:
        return "flat (panel / plate / disk)"
    if dz >= 2.5 * max(dx, dy):
        return "tall (column / post)"
    if a < 0.15 * c and b < 0.15 * c:
        return "long and thin (rod / beam)"
    if c < 1.6 * a:
        return "compact (roughly cubic)"
    return "elongated block"


def build_roles(man, ignore_annotations):
    objs = man["objects"]
    boxes = {n: (Vector(o["bbox"][0]), Vector(o["bbox"][1])) for n, o in objs.items() if o.get("bbox")}
    diags = [(b[1] - b[0]).length for b in boxes.values()] or [1.0]
    med = statistics.median(diags) or 1.0
    min_z = min((b[0].z for b in boxes.values()), default=0.0)
    centers = {n: (b[0] + b[1]) / 2 for n, b in boxes.items()}
    medc = Vector([statistics.median(c[i] for c in centers.values()) for i in range(3)]) if centers else Vector()
    md = statistics.median([(c - medc).length for c in centers.values()]) if centers else 1.0
    series = Counter(series_base(n) for n in objs)
    refs = defaultdict(list)
    for n, o in objs.items():
        for c in o["constraints"]:
            if c.get("target"):
                refs[c["target"]].append(f"{n} ({c['type']} constraint)")
        for m in o["modifiers"]:
            for k in OBJ_REF_KEYS & set(m["settings"]):
                if m["settings"][k] in objs:
                    refs[m["settings"][k]].append(f"{n} ({m['type']} modifier)")

    roles = {}
    for n, o in objs.items():
        t, ev, qual = o["type"], [], []
        role, conf, source = None, 0.3, "inferred"
        if not ignore_annotations:
            ann = {k: v for k, v in o["custom_props"].items() if ANNOTATION_KEY.search(k) and isinstance(v, str)}
            if ann:
                role, conf, source = " ".join(ann.values()), 1.0, "annotation"
                ev.append(f"custom properties {sorted(ann)}")
        if role is None:
            toks = name_tokens(n)
            if t == "CAMERA":
                role, conf = ("active scene camera" if o["camera"]["is_scene_camera"] else "camera"), 0.95
                ev.append(f"lens {o['camera']['lens']} mm")
            elif t == "LIGHT":
                L = o["light"]
                role, conf = f"{L['light_type'].lower()} light, {L['energy']} W", 0.95
            elif t == "EMPTY":
                kids = o["children"]
                if o["empty"]["instance_collection"]:
                    role, conf = f"instance of collection '{o['empty']['instance_collection']}'", 0.9
                elif refs.get(n):
                    role, conf = f"target / helper used by {', '.join(refs[n][:3])}", 0.85
                elif kids:
                    role, conf = f"pivot / group for {len(kids)} children ({', '.join(kids[:4])}{', ...' if len(kids) > 4 else ''})", 0.8
                else:
                    role = "helper empty (marker or target?)"
                if toks:
                    role = f"'{' '.join(toks)}': {role}"
            elif t == "ARMATURE":
                role, conf = f"rig with {o['armature']['bones']} bones", 0.9
            else:
                if toks:
                    role, conf = f"'{' '.join(toks)}' (from its name)", 0.6
                    ev.append(f"name tokens {toks}")
                else:
                    ev.append("generic name: role guessed from geometry only")
                if n in boxes:
                    mn, mx = boxes[n]
                    d = list(mx - mn)
                    diag = (mx - mn).length
                    shape = shape_word(d)
                    ev.append(f"shape {shape}, {d[0]:.2f} x {d[1]:.2f} x {d[2]:.2f} m")
                    inside = sum(1 for m, c in centers.items() if m != n and all(mn[i] <= c[i] <= mx[i] for i in range(3)))
                    if d[2] < 0.03 * max(d[0], d[1]) and diag > 3 * med and mn.z <= min_z + 0.05 * diag:
                        qual.append("large flat base (ground / floor)")
                        conf = max(conf, 0.7)
                    elif diag > 5 * med and inside >= 0.8 * (len(centers) - 1):
                        qual.append("encloses most of the scene (backdrop / environment / room)")
                    elif (centers[n] - medc).length > 8 * md + med:
                        qual.append("far from the main subject (background element)")
                    if role is None:
                        role = f"unnamed {shape} {t.lower()}"
                        conf = 0.25
            mats = [m for m in o["materials"] if m]
            if any(man["materials"].get(m, {}).get("emissive") for m in mats):
                qual.append("emissive (glows)")
            if any(man["materials"].get(m, {}).get("transmissive") for m in mats):
                qual.append("transparent / glass")
            if o["parent"]:
                qual.append(f"part of '{o['parent']}'")
            if series[series_base(n)] >= 3:
                qual.append(f"one of {series[series_base(n)]} '{series_base(n)}' objects")
            if o.get("mesh", {}).get("users", 1) > 1:
                qual.append(f"shares mesh '{o['data']}' with {o['mesh']['users'] - 1} other object(s)")
            if refs.get(n) and t != "EMPTY":
                qual.append(f"referenced by {', '.join(refs[n][:3])}")
            if o.get("animation"):
                qual.append("animated")
            if o["modifiers"]:
                ev.append(f"modifiers {[m['type'] for m in o['modifiers']]}")
            if qual:
                role = f"{role}; " + "; ".join(qual)
        roles[n] = {"type": t, "role": role, "confidence": conf, "source": source, "evidence": ev,
                    "needs_review": source == "inferred", "parent": o["parent"],
                    "dimensions": o.get("dimensions"), "materials": [m for m in o["materials"] if m]}
    return roles


# ----------------------------------------------------------------------------- previews
def _box(items):
    return (Vector([min(i[1][k] for i in items) for k in range(3)]),
            Vector([max(i[2][k] for i in items) for k in range(3)]))


def framing(man, names=None, main=False):
    """Bounds to frame, the size/distance outliers left out, and (with main) scattered series left out.

    Outliers are ground planes, skies and far props. With main=True, a series of five or more
    look-alike objects (Rock 00 ... Rock 15) that spreads beyond the rest of the scene is scatter:
    the frame is set on the other objects, so the subject is not a speck in the middle of the dunes.
    Returns (bounds or None, outliers, scatter left outside the frame).
    """
    items = [(n, Vector(o["bbox"][0]), Vector(o["bbox"][1])) for n, o in man["objects"].items()
             if o.get("bbox") and not o["hidden"]["render"] and (names is None or n in names)]
    if not items:
        return None, [], []
    excluded = set()
    if len(items) >= 4:
        diags = sorted((mx - mn).length for _, mn, mx in items)
        p90 = diags[int(0.9 * (len(diags) - 1))]
        excluded |= {n for n, mn, mx in items if (mx - mn).length > 3 * p90 + 1e-6}
        rest = [i for i in items if i[0] not in excluded]
        cs = [(mn + mx) / 2 for _, mn, mx in rest]
        med = Vector([statistics.median(c[i] for c in cs) for i in range(3)])
        dists = [(c - med).length for c in cs]
        lim = 6 * statistics.median(dists) + p90
        excluded |= {i[0] for i, dd in zip(rest, dists) if dd > lim}
    keep = [i for i in items if i[0] not in excluded] or items
    if main and names is None and len(keep) >= 6:
        series = Counter(series_base(i[0]) for i in keep)
        scatter = {i[0] for i in keep if series[series_base(i[0])] >= 5}
        core = [i for i in keep if i[0] not in scatter]
        if core and scatter:
            cmn, cmx = _box(core)
            pad = (cmx - cmn) * 0.15
            inside = [i for i in keep if i[0] in scatter and all(
                cmn[k] - pad[k] <= (i[1][k] + i[2][k]) / 2 <= cmx[k] + pad[k] for k in range(3))]
            outside = sorted(scatter - {i[0] for i in inside})
            if outside:
                return _box(core + inside), sorted(excluded), outside
    return _box(keep), sorted(excluded), []


def setup_workbench(scene):
    r = scene.render
    try:
        r.engine = "BLENDER_WORKBENCH"
    except TypeError:
        return False
    r.use_compositing = False
    r.film_transparent = False
    sh = scene.display.shading
    for k, v in (("light", "STUDIO"), ("color_type", "RANDOM"), ("show_cavity", True), ("cavity_type", "BOTH"),
                 ("show_object_outline", True), ("show_shadows", False)):
        safe(lambda: setattr(sh, k, v))
    safe(lambda: setattr(scene.display, "render_aa", "8"))
    if scene.world is None:
        scene.world = bpy.data.worlds.new("_ingest_world")
    scene.world.color = (0.17, 0.17, 0.19)
    return True


def render_previews(out, man, args):
    pdir = os.path.join(out, "previews")
    os.makedirs(pdir, exist_ok=True)
    for f in os.listdir(pdir):
        if f.endswith(".png"):
            os.remove(os.path.join(pdir, f))
    scene = bpy.context.scene
    r = scene.render
    size = args.preview_size
    results = []
    r.resolution_percentage = 100
    r.image_settings.file_format = "PNG"

    def shoot(fname, label):
        r.filepath = os.path.join(pdir, fname)
        t = time.time()
        try:
            bpy.ops.render.render(write_still=True)
            results.append({"file": f"previews/{fname}", "view": label, "seconds": round(time.time() - t, 1)})
        except Exception as e:
            results.append({"file": None, "view": label, "error": str(e)})

    # 1. the file's own camera, with its own look
    cam0 = scene.camera
    if cam0 is not None:
        aspect = (r.resolution_x * r.pixel_aspect_x) / max(1.0, r.resolution_y * r.pixel_aspect_y)
        r.resolution_x, r.resolution_y = (size, max(1, round(size / aspect))) if aspect >= 1 else (max(1, round(size * aspect)), size)
        engine = None
        if not args.no_lookdev:
            for eng in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
                try:
                    r.engine = eng
                    engine = eng
                    safe(lambda: setattr(scene.eevee, "taa_render_samples", 16))
                    break
                except TypeError:
                    pass
        if engine is None:
            setup_workbench(scene)
        shoot("camera.png", f"scene camera '{cam0.name}' ({'EEVEE, file materials' if engine else 'Workbench'})")

    # 2. overview + group views: Workbench, random colour per object so parts are easy to tell apart
    bounds, excluded, scattered = framing(man, main=True)
    if bounds is None or not setup_workbench(scene):
        return results
    cd = bpy.data.cameras.new("_ingest_cam")
    cam = bpy.data.objects.new("_ingest_cam", cd)
    scene.collection.objects.link(cam)
    scene.camera = cam
    r.resolution_x = r.resolution_y = size
    all_obs = [o for o in scene.objects if o.name in man["objects"]]
    orig_hide = {o.name: o.hide_render for o in all_obs}

    def show_only(names):
        for o in all_obs:
            o.hide_render = orig_hide[o.name] or (names is not None and o.name not in names)

    def aim(bmn, bmx, d, ortho):
        c, ext = (bmn + bmx) / 2, bmx - bmn
        diag = max(ext.length, 1e-3)
        if ortho:
            cd.type = "ORTHO"
            w = {(0, 1, 0): (ext.x, ext.z), (-1, 0, 0): (ext.y, ext.z), (0, 0, -1): (ext.x, ext.y)}[tuple(d)]
            cd.ortho_scale = max(w[0], w[1], 1e-3) * 1.12
            dist = diag * 1.5 + 1.0
        else:
            cd.type = "PERSP"
            cd.lens, cd.sensor_width = 50.0, 36.0
            dist = (diag / 2) / math.sin(math.atan(18.0 / 50.0)) * 1.05
        cam.location = c - Vector(d).normalized() * dist
        # to_track_quat(..., "Y") is degenerate when the view looks along world Y (the front of a gate).
        try:
            from . import helpers
            helpers.aim(cam, c, track="-Z")
        except Exception:
            direction = Vector(c) - Vector(cam.location)
            if direction.length < 1e-8:
                cam.rotation_euler = (0.0, 0.0, 0.0)
            else:
                dn = direction.normalized()
                axes = (("Z", Vector((0.0, 0.0, 1.0))), ("Y", Vector((0.0, 1.0, 0.0))), ("X", Vector((1.0, 0.0, 0.0))))
                up = min(axes, key=lambda item: abs(float(dn.dot(item[1]))))[0]
                cam.rotation_euler = direction.to_track_quat("-Z", up).to_euler()
        cd.clip_start, cd.clip_end = max(0.001, dist * 0.001), dist * 4 + diag

    main = set(man["objects"]) - set(excluded)
    note = f"; hidden as outliers: {', '.join(excluded)}" if excluded else ""
    if scattered:
        note += f"; framed on the main subject, {len(scattered)} scattered objects reach outside it"
    show_only(main)
    for name, d, ortho in (("front", (0, 1, 0), True), ("right", (-1, 0, 0), True),
                           ("top", (0, 0, -1), True), ("iso", (-1, 1, -0.8), False)):
        aim(bounds[0], bounds[1], d, ortho)
        shoot(f"{name}.png", f"{name} ({'orthographic' if ortho else 'perspective'}, Workbench random colours{note})")
    if scattered:
        whole, _excl, _ = framing(man)
        aim(whole[0], whole[1], (0, 0, -1), True)
        shoot("scene_top.png", f"whole scene from the top, including {series_base(scattered[0])} and the other scatter "
                               f"(orthographic, Workbench)")

    # 3. focused views of the largest groups: parent hierarchies and top-level collections
    for fname, label, members, _root in preview_groups(man, main, scene)[:args.max_group_previews]:
        gb, _, _ = framing(man, members)
        if gb is None:
            continue
        show_only(members)
        aim(gb[0], gb[1], (-1, 1, -0.8), False)
        shoot(f"{fname}.png", f"{label}: {len(members)} objects (perspective, Workbench)")
    show_only(None)
    return results


def preview_groups(man, main, scene):
    """Parent hierarchies and top-level collections worth a close-up: (file, label, members, root object)."""
    objs = man["objects"]
    kids = defaultdict(list)
    for n, o in objs.items():
        if o["parent"] in objs:
            kids[o["parent"]].append(n)

    def descendants(n):
        out = [n]
        for k in kids.get(n, []):
            out += descendants(k)
        return out

    geom = {n for n in main if objs[n].get("bbox")}
    groups = []
    for n, o in objs.items():
        if o["parent"] is None and kids.get(n):
            members = set(descendants(n)) & geom
            if len(members) >= 2:
                groups.append((f"group_{slug(n)}", f"hierarchy under '{n}'", members, n))

    def col_objects(col):
        s = {o.name for o in col.objects}
        for c in col.children:
            s |= col_objects(c)
        return s

    seen = [g[2] for g in groups]
    for col in scene.collection.children:
        members = col_objects(col) & geom
        if len(members) >= 2 and len(members) < 0.9 * len(geom) and members not in seen:
            groups.append((f"collection_{slug(col.name)}", f"collection '{col.name}'", members, None))
    groups.sort(key=lambda g: -len(g[2]))
    return groups


def render_previews_offscreen(out, man, args, render_view):
    """Previews of the live session through the add-on's offscreen preview, which never touches the
    user's scene, camera or render settings. Same files as the headless previews."""
    pdir = os.path.join(out, "previews")
    os.makedirs(pdir, exist_ok=True)
    for f in os.listdir(pdir):
        if f.endswith(".png"):
            os.remove(os.path.join(pdir, f))
    results = []
    size = args.preview_size

    def shoot(fname, label, params):
        t = time.time()
        try:
            render_view(dict(params, size=size), os.path.join(pdir, fname))
            results.append({"file": f"previews/{fname}", "view": label, "seconds": round(time.time() - t, 1)})
        except Exception as e:
            results.append({"file": None, "view": label, "error": str(e)})

    scene = bpy.context.scene
    if scene.camera is not None:
        shoot("camera.png", f"scene camera '{scene.camera.name}' (EEVEE, file materials)",
              {"view": "camera", "shading": "rendered" if not args.no_lookdev else "solid"})
    bounds, excluded, scattered = framing(man, main=True)
    if bounds is None:
        return results
    box = [list(bounds[0]), list(bounds[1])]
    note = f"; framed on the main subject, {len(scattered)} scattered objects reach outside it" if scattered else ""
    for name, ortho in (("front", True), ("right", True), ("top", True), ("iso", False)):
        shoot(f"{name}.png", f"{name} ({'orthographic' if ortho else 'perspective'}, Workbench{note})",
              {"view": name, "shading": "solid", "bounds": box, "projection": "ortho" if ortho else "persp"})
    if scattered:
        whole, _excl, _ = framing(man)
        shoot("scene_top.png", "whole scene from the top, including the scatter (orthographic, Workbench)",
              {"view": "top", "shading": "solid", "bounds": [list(whole[0]), list(whole[1])], "projection": "ortho"})
    main = set(man["objects"]) - set(excluded)
    for fname, label, members, root in preview_groups(man, main, scene)[:args.max_group_previews]:
        if root is None:
            continue
        shoot(f"{fname}.png", f"{label}: {len(members)} objects (perspective, Workbench, isolated)",
              {"view": "iso", "shading": "solid", "target": root, "isolate": True})
    return results


# ----------------------------------------------------------------------------- change detection
def detect_renames(old_objects, new_objects):
    """Objects that only changed name: same type, and the same mesh data or the same place and size.

    Objects are keyed by name, so without this a rename reads as one removal and one addition.
    """
    gone = [n for n in old_objects if n not in new_objects]
    born = [n for n in new_objects if n not in old_objects]

    def key(o, by_data):
        if by_data:
            return (o["type"], o.get("data")) if o.get("data") else None
        return (o["type"], tuple(o.get("world_location") or ()), tuple(o.get("dimensions") or ()))

    renames = {}
    for by_data in (True, False):
        old_keys = defaultdict(list)
        for n in gone:
            if n not in renames:
                k = key(old_objects[n], by_data)
                if k:
                    old_keys[k].append(n)
        new_keys = defaultdict(list)
        for n in born:
            if n not in renames.values():
                k = key(new_objects[n], by_data)
                if k:
                    new_keys[k].append(n)
        for k, olds in old_keys.items():
            news = new_keys.get(k, [])
            if len(olds) == 1 and len(news) == 1:
                renames[olds[0]] = news[0]
    return renames


_VISIBILITY = {"viewport": ("hidden in the viewport", "shown in the viewport"),
               "render": ("hidden in renders", "shown in renders"),
               "view_layer": ("hidden in the view layer", "unhidden in the view layer"),
               "in_view_layer": ("added to the view layer", "excluded from the view layer")}


def visibility_words(old, new):
    """{'viewport': False, 'render': True, ...} -> {...} as words: 'hidden in renders, shown in the viewport'."""
    parts = []
    for key, (on, off) in _VISIBILITY.items():
        if old.get(key) == new.get(key):
            continue
        parts.append(on if bool(new.get(key)) else off)
    return ", ".join(parts) or "changed"


def dict_change_words(old, new):
    """Which keys of a dict were added, removed or changed: 'ag_dial changed, ag_role added'."""
    parts = [f"{k} added" for k in sorted(set(new) - set(old))]
    parts += [f"{k} removed" for k in sorted(set(old) - set(new))]
    parts += [f"{k} changed" for k in sorted(set(old) & set(new)) if old[k] != new[k]]
    return ", ".join(parts[:8]) + (f" (+{len(parts) - 8} more)" if len(parts) > 8 else "") or "changed"


def diff_manifests(old, new):
    out = []

    def add(subject, change):
        out.append({"subject": subject, "change": change})

    oo, no = old.get("objects", {}), new.get("objects", {})
    renames = detect_renames(oo, no)
    for old_name, new_name in sorted(renames.items()):
        add(new_name, f"renamed from {old_name}")
    for n in sorted(set(no) - set(oo) - set(renames.values())):
        add(n, f"added ({no[n]['type']})")
    for n in sorted(set(oo) - set(no) - set(renames)):
        add(n, "removed")
    pairs = [(n, n) for n in sorted(set(oo) & set(no))] + sorted(renames.items())
    for n_old, n in pairs:
        a, b = oo[n_old], no[n]
        for k in ("parent", "location", "rotation_deg", "rotation_quat", "scale", "materials", "hidden", "data",
                  "custom_props", "light", "camera"):
            if a.get(k) == b.get(k):
                continue
            if k == "hidden":
                add(n, f"visibility: {visibility_words(a.get(k) or {}, b.get(k) or {})}")
            elif k == "custom_props":
                add(n, f"custom properties: {dict_change_words(a.get(k) or {}, b.get(k) or {})}")
            else:
                add(n, f"{k}: {a.get(k)} -> {b.get(k)}")
        if a.get("modifiers") != b.get("modifiers"):
            add(n, f"modifiers changed: {[m['type'] for m in a.get('modifiers', [])]} -> {[m['type'] for m in b.get('modifiers', [])]}")
        ma, mb = a.get("mesh"), b.get("mesh")
        if ma and mb and (ma["verts"], ma["faces"]) != (mb["verts"], mb["faces"]):
            add(n, f"geometry edited: {ma['verts']} -> {mb['verts']} verts, {ma['faces']} -> {mb['faces']} faces")
        elif a.get("dimensions") != b.get("dimensions") and a.get("scale") == b.get("scale"):
            add(n, f"geometry reshaped: dimensions {a.get('dimensions')} -> {b.get('dimensions')}")
        if a.get("animation") != b.get("animation"):
            add(n, "animation changed")
    om, nm = old.get("materials", {}), new.get("materials", {})
    for n in sorted(set(nm) - set(om)):
        add(f"material:{n}", "added")
    for n in sorted(set(om) - set(nm)):
        add(f"material:{n}", "removed")
    for n in sorted(set(om) & set(nm)):
        if (om[n].get("nodes"), om[n].get("links")) != (nm[n].get("nodes"), nm[n].get("links")):
            add(f"material:{n}", "node values or links changed")
    os_, ns = old.get("scene", {}), new.get("scene", {})
    for k in sorted(set(os_) | set(ns)):
        if os_.get(k) != ns.get(k):
            add("scene", f"{k}: {os_.get(k)} -> {ns.get(k)}")
    if old.get("animation") != new.get("animation"):
        add("animation", "material / world / scene keyframes changed")
    return out


# ----------------------------------------------------------------------------- NOTES.md
def notes_generated(man, roles, state, previews):
    f, s, st = man["file"], man["scene"], man["stats"]
    sha = (state.get("sha256") or "unsaved")[:12]
    sub = f" (file subversion {f['file_subversion']})" if f.get("file_subversion") is not None else ""
    L = [f"**File** `{f['name']}` - {f['size_mb']} MB - saved with Blender {f['saved_with']}{sub} - "
         f"sha256 `{sha}` - ingested {state['ingested_at']} with Blender {f['ingested_with']}",
         ""]
    if state.get("source") == "live":
        L += ["**Source** the live Blender session" + (", including changes that are not saved to the .blend yet"
                                                       if state.get("dirty") else "") + ".", ""]
    L += [
         f"**Scene** `{s['name']}` - {s['engine']} - {s['resolution'][0]}x{s['resolution'][1]} - "
         f"frames {s['frame_range'][0]}-{s['frame_range'][1]} @ {s['fps']} fps - camera `{s['camera']}` - "
         f"world `{s['world']}` - units {s['units']} - colour {s['color']}",
         "",
         f"**Contents** {', '.join(f'{v} {k.lower()}' for k, v in st['objects_by_type'].items())} - "
         f"{st['verts']:,} verts / {st['faces']:,} faces ({st['faces_after_modifiers']:,} after modifiers) - "
         f"{st['materials']} materials - {st['images']} images"]
    n_inf = sum(1 for r in roles.values() if r["source"] == "inferred")
    L += ["", f"**Roles** {len(roles) - n_inf} from annotations in the file, {n_inf} inferred (need review)."]

    if previews:
        L += ["", "## Previews", ""]
        for p in previews:
            L.append(f"- ![{p['view']}]({p['file']}) {p['view']}" if p.get("file") else f"- {p['view']}: failed ({p.get('error')})")

    L += ["", "## Object tree", "", "`[a]` = annotated in the file, `[?]` = inferred, needs review", ""]
    objs = man["objects"]
    kids = defaultdict(list)
    for n, o in objs.items():
        kids[o["parent"] if o["parent"] in objs else None].append(n)

    def line(n, depth):
        o, r = objs[n], roles[n]
        size = f" {o['mesh']['verts']:,}v/{o['mesh']['faces']:,}f" if o.get("mesh") else ""
        role = r["role"] if len(r["role"]) <= 170 else r["role"][:167] + "..."
        tag = "[a]" if r["source"] == "annotation" else ("[r]" if r["source"] in ("ai", "human") else "[?]")
        text = f"{'  ' * depth}- **{n}** {o['type']}{size} - {role} `{tag}`"
        # A root holds what its scripts recorded about the whole (a dial schedule, a build note).
        props = root_props(o) if o["parent"] is None and kids.get(n) else {}
        if props:
            text += "\n" + "\n".join(f"{'  ' * (depth + 1)}- `{k}`: {v}" for k, v in props.items())
        return text

    def emit(names, depth):
        by_series = defaultdict(list)
        for n in sorted(names, key=natural_key):
            by_series[series_base(n)].append(n)
        for base, members in sorted(by_series.items(), key=lambda kv: natural_key(kv[1][0])):
            leafs = [m for m in members if not kids.get(m)]
            if len(leafs) >= 4:
                o0 = objs[leafs[0]]
                role = series_role([roles[m]["role"] for m in leafs], [series_label(m) for m in leafs])
                L.append(f"{'  ' * depth}- **{leafs[0]} ... {leafs[-1]}** ({len(leafs)} x {o0['type']}) - "
                         f"{role if len(role) <= 360 else role[:357] + '...'}")
                members = [m for m in members if m not in leafs]
            for m in members:
                L.append(line(m, depth))
                emit(kids.get(m, []), depth + 1)

    emit(kids[None], 0)

    mats = [(n, m) for n, m in man["materials"].items() if m["users"]]
    if mats:
        L += ["", "## Materials", ""]
        groups = defaultdict(list)
        order = []
        for n, m in mats:
            # Flags follow keyed values (emission keyed to 0 is not emissive at this frame), so they are not part of the key.
            key = (series_base(n), _structure(m.get("summary", "")))
            if key not in groups:
                order.append(key)
            groups[key].append((n, m))
        for key in order:
            members = groups[key]
            n, m = members[0]
            flags = [k for k in ("emissive", "transmissive") if any(mm.get(k) for _nn, mm in members)]
            summ = m.get("summary", "")
            summ = summ if len(summ) <= 260 else summ[:257] + "..."
            if len(members) >= 3:
                users = sum(mm["users"] for _nn, mm in members)
                same = "identical" if len({mm.get("summary") for _nn, mm in members}) == 1 else "identical apart from values (keyed or tuned)"
                L.append(f"- **{members[0][0]} ... {members[-1][0]}** ({len(members)} materials, {users} users"
                         f"{', ' + ', '.join(flags) if flags else ''}; {same}): `{summ}`")
            else:
                for n, m in members:
                    summ = m.get("summary", "")
                    summ = summ if len(summ) <= 260 else summ[:257] + "..."
                    L.append(f"- **{n}** ({m['users']} users{', ' + ', '.join(flags) if flags else ''}): `{summ}`")

    anim = [(n, o["animation"]) for n, o in objs.items() if o.get("animation")] + list(man["animation"].items())
    if anim:
        L += ["", "## Animation", ""]

        def anim_parts(a):
            parts = []
            for part, ch in a.items():
                for c in ch.get("channels", [])[:4]:
                    parts.append(f"{c.get('label') or c['path']} ({c['keys']} keys, f{c['frames'][0]:g}-{c['frames'][1]:g})")
                if len(ch.get("channels", [])) > 4:
                    parts.append(f"+{len(ch['channels']) - 4} more")
                parts += ch.get("drivers", [])
            return parts

        groups = defaultdict(list)
        order = []
        for n, a in anim:
            channels = tuple(sorted(c.get("label") or c["path"] for ch in a.values() for c in ch.get("channels", [])))
            key = (n.split(":", 1)[0] + ":" + series_base(n.split(":", 1)[-1]) if ":" in n else series_base(n), channels)
            if key not in groups:
                order.append(key)
            groups[key].append((n, a))
        for key in order:
            members = sorted(groups[key], key=lambda item: natural_key(item[0]))
            if len(members) >= 3:
                frames = [c["frames"] for _n, a in members for ch in a.values() for c in ch.get("channels", [])]
                f0 = min(fr[0] for fr in frames) if frames else 0
                f1 = max(fr[1] for fr in frames) if frames else 0
                labels = ", ".join(list(key[1])[:4]) + (f", +{len(key[1]) - 4} more" if len(key[1]) > 4 else "")
                numbers = [series_label(n) for n, _a in members]
                which = number_list([float(x) for x in numbers]) if all(x.isdigit() for x in numbers) else ""
                L.append(f"- **{members[0][0]} ... {members[-1][0]}** ({len(members)}{': ' + which if which else ''}; the same "
                         f"channels, keys between f{f0:g} and f{f1:g}, see Animation beats): {labels}")
            else:
                for n, a in members:
                    L.append(f"- **{n}**: {'; '.join(anim_parts(a))}")

    beats = man.get("beats") or []
    if beats:
        L += ["", "## Animation beats", "",
              "What changes when, in time order (`timeline` lists every key; `timeline` op evaluate gives values at frames).", ""]
        L += [f"- {b}" for b in beats[:80]]
        if len(beats) > 80:
            L.append(f"- ... {len(beats) - 80} more: call `timeline` with frames {{start, end}}")

    built = defaultdict(list)
    for n, o in objs.items():
        script = (o.get("built_by") or {}).get("by") or (o.get("derived") or {}).get("script")
        if script:
            built[script].append(n)
    if built:
        L += ["", "## Built by scripts", "",
              "Objects a script created or rebuilt (run_script stamps them). Change them by editing and re-running the script.", ""]
        for script, names in sorted(built.items()):
            L.append(f"- `{script}`: {collapse_names(names)}")

    specs = [(n, o["spec"]) for n, o in objs.items() if o.get("spec")]
    if specs:
        L += ["", "## Parameters", "", "What objects were built from (vsblender.spec, geo to_object). Intent as data.", ""]
        L += parameter_lines(specs)

    if man["issues"]:
        L += ["", "## Before you modify", ""]
        for i in man["issues"]:
            L.append(f"- {'**warn**' if i['severity'] == 'warn' else 'info'} `{i['subject']}`: {i['message']}")

    if man["texts"]:
        L += ["", "## Text blocks inside the .blend", "",
              "The author's own notes and scripts; copies are in `texts/`. Read these before inferring anything.", ""]
        for n, t in man["texts"].items():
            extra = ", runs on load" if t["use_module"] else ""
            if t.get("exports") is not None:
                names = ", ".join(t["exports"][:12]) + (f", +{len(t['exports']) - 12} more" if len(t["exports"]) > 12 else "")
                extra += f"; importable Python: `{t['import']}` gives {names or 'no public names'}"
            elif t.get("python"):
                extra += f"; Python that {t['python']}"
            L.append(f"- `texts/{slug(n)}` ({t['lines']} lines{extra})")

    L += ["", "## Files next to this one", "",
          "- `manifest.json`: full structural description (diff it to see what changed)",
          "- `roles.json`: per-object role, confidence, evidence. After reviewing, set `source` to `ai` or `human` "
          "(or call `set_role`); reviewed roles are kept on re-ingest",
          "- `journal.jsonl`: every ingest and every AI script that changed the scene, with actor, reason and changes",
          "- `checkpoints/`: copies of the session from before AI scripts (`restore_checkpoint`)",
          "- `state.json`: fingerprint used to decide whether to re-ingest"]
    return "\n".join(L)


_NUMBERS = re.compile(r"-?\d+(?:\.\d+)?")


_VALUE_PAIR = re.compile(r"\s*[A-Za-z][A-Za-z ]*=(?:\([^)]*\)|[^,\])]+)\s*,?")


def _structure(summary):
    """A material summary without its parameter values: two materials that differ only in values
    (a keyed strength, a tuned colour) have the same structure."""
    return _VALUE_PAIR.sub("", summary).replace("[]", "")


def geo_summary(desc):
    """A geo build description, short. Descriptions of solids made by builder functions are mostly
    '<lambda>' and say nothing, so they become counts of what was done: '1 difference, 2 unions, 4
    joins, 2 polar arrays (x9)'."""
    text = str(desc)
    if "<lambda>" not in text and "builder" not in text and len(text) <= 160:
        return text
    counts = []
    for label, pattern in (("difference", r"\) - \("), ("union", r"\) \+ \("), ("intersection", r"\) & \("),
                           ("join", r"\bjoin\("), ("transform", r"\.transform\("), ("mirror", r"\.mirror\("),
                           ("bevel", r"\.bevel\(")):
        n = len(re.findall(pattern, text))
        if n:
            counts.append(f"{n} {label}{'s' if n > 1 else ''}")
    polar = re.findall(r"\.polar\((\d+)", text)
    if polar:
        counts.append(f"{len(polar)} polar array{'s' if len(polar) > 1 else ''} (x{', x'.join(dict.fromkeys(polar))})")
    builders = len(re.findall(r"<lambda>|solid\(builder\)", text))
    if builders:
        counts.insert(0, f"{builders} builder function{'s' if builders > 1 else ''}")
    return "built from " + ", ".join(counts) if counts else "geo build"


def _spec_value(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def parameter_lines(specs):
    """One line per object, or one per numbered series: each key with its values in member order,
    as a range when they step evenly, '-' where a member has no value (lock_order=7,1,2,3,-,-,4,5,6)."""
    lines = []
    groups = defaultdict(list)
    for n, spec in sorted(specs, key=lambda item: natural_key(item[0])):
        groups[series_base(n)].append((n, dict(spec)))
    for _base, members in groups.items():
        for _n, spec in members:
            if "geo" in spec:
                spec["geo"] = geo_summary(spec["geo"])
        if len(members) >= 3:
            keys = list(dict.fromkeys(k for _n, s in members for k in s))
            parts = []
            for k in keys:
                values = [s.get(k) for _n, s in members]
                if all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)) for v in values):
                    parts.append(f"{k}={number_list([None if v is None else float(v) for v in values])}")
                elif len({json.dumps(v, sort_keys=True) for v in values}) == 1:
                    parts.append(f"{k}={_spec_value(values[0])}")
                else:
                    parts.append(f"{k}=" + ",".join("-" if v is None else _spec_value(v) for v in values))
            text = ", ".join(parts)
            lines.append(f"- **{members[0][0]} ... {members[-1][0]}** ({len(members)}): {text[:400]}{'...' if len(text) > 400 else ''}")
            continue
        for n, spec in members:
            text = ", ".join(f"{k}={_spec_value(v)}" for k, v in spec.items())
            lines.append(f"- **{n}**: {text[:300]}{'...' if len(text) > 300 else ''}")
    return lines


def collapse_names(names):
    """'SG Chevron 0 ... SG Chevron 8 (9)' for numbered series, plain names otherwise."""
    groups = defaultdict(list)
    for n in sorted(names):
        groups[series_base(n)].append(n)
    parts = []
    for _base, members in sorted(groups.items(), key=lambda kv: kv[1][0]):
        if len(members) >= 3:
            parts.append(f"{members[0]} ... {members[-1]} ({len(members)})")
        else:
            parts.extend(members)
    return ", ".join(parts)


def number_list(values):
    """A column of numbers, in member order: '1-7' or '0-320 step 40' only when they step evenly,
    otherwise listed ('7,13,14,17'); None (a member without it) is '-'."""
    shown = ["-" if v is None else f"{v:g}" for v in values]
    nums = [v for v in values if v is not None]
    if not nums:
        return "-"
    if len(set(shown)) == 1:
        return shown[0]
    if len(nums) == len(values) and len(nums) >= 3:
        step = nums[1] - nums[0]
        if step and all(abs((b - a) - step) < 1e-9 for a, b in zip(nums, nums[1:])):
            return f"{nums[0]:g}-{nums[-1]:g}" if abs(step) == 1 else f"{nums[0]:g}-{nums[-1]:g} step {step:g}"
    return ",".join(shown)


def _fill(template, rows):
    """A number template ('Chevron # at # deg') filled with each number column of rows."""
    columns = list(zip(*rows)) if rows else []
    out = template
    for col in columns:
        out = out.replace("#", number_list(list(col)), 1)
    return out


def _numbers(text):
    return [float(x) for x in _NUMBERS.findall(text)]


def series_role(roles, labels=None):
    """What a collapsed series has in common.

    One shared template: the numbers that vary as a range when they step evenly ('symbol 1-7'), or
    listed ('glyph 7,13,14,17,21,29,31'). Several templates: their common start, then each variant
    with the members it applies to ('locks symbol 1,2,3 of a 7-symbol dial [1,2,3]; unused [4,5]').
    labels: a short name per member (its series number), in the same order as roles.
    """
    pairs = [(labels[i] if labels else str(i), r) for i, r in enumerate(roles) if r]
    if not pairs:
        return ""
    distinct = list(dict.fromkeys(r for _l, r in pairs))
    if len(distinct) == 1:
        return distinct[0]
    masked = [_NUMBERS.sub("#", r) for _l, r in pairs]
    if len(set(masked)) == 1:
        return _fill(masked[0], [_numbers(r) for _l, r in pairs])
    # The common start of the masked roles, cut back to a word or clause boundary.
    prefix = os.path.commonprefix(masked)
    cut = max(prefix.rfind(" "), prefix.rfind(";"), prefix.rfind(","), prefix.rfind(":"))
    prefix = prefix[:cut + 1] if cut > 0 else ""
    head_count = prefix.count("#")
    head = _fill(prefix, [_numbers(r)[:head_count] for _l, r in pairs]).strip() if prefix.strip() else ""
    variants = {}
    for (label, role), mask in zip(pairs, masked):
        rest_mask = mask[len(prefix):].strip(" ;,")
        variants.setdefault(rest_mask, []).append((label, _numbers(role)[head_count:]))
    if len(variants) > 4 or not head:
        return f"{len(distinct)} different roles, e.g. {distinct[-1]}"
    parts = []
    for rest, members in variants.items():
        text = _fill(rest, [nums for _l, nums in members])
        parts.append(f"{text} [{', '.join(l for l, _n in members)}]")
    return f"{head} " + "; ".join(parts)


DEFAULT_TAIL = """
## Intent & constraints

<!-- Hand-written by people, or by the AI after review. Kept across re-ingests. What must not change, and why. -->

References go through import_reference and stay out of the scene.

## Change log

<!-- One line per change: who, what, why. The ingester appends the changes it detects. Keep this section last. -->
"""


def write_notes(path, man, roles, state, previews, log_line):
    tail = DEFAULT_TAIL
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        if GEN_END in old:
            tail = old.split(GEN_END, 1)[1]
    head = (f"# {man['file']['name']}: context notes\n\n"
            "> Generated by `blender_ingest.py`. Everything between the `ai:generated` markers is rewritten on each "
            "ingest; anything after them is kept. Read **Before you modify** first.\n\n")
    body = head + GEN_BEGIN + "\n" + notes_generated(man, roles, state, previews) + "\n" + GEN_END + tail.rstrip("\n")
    body += "\n" + log_line + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)


# ----------------------------------------------------------------------------- main
def parse_args(argv=None):
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="blender_ingest")
    p.add_argument("--out")
    p.add_argument("--force", action="store_true")
    p.add_argument("--actor", default="external")
    p.add_argument("--reason", default="")
    p.add_argument("--no-previews", action="store_true")
    p.add_argument("--no-lookdev", action="store_true")
    p.add_argument("--ignore-annotations", action="store_true")
    p.add_argument("--preview-size", type=int, default=768)
    p.add_argument("--max-group-previews", type=int, default=8)
    p.add_argument("--manifest-to")
    p.add_argument("--diff", nargs=2, metavar=("A", "B"))
    p.add_argument("--diff-to")
    return p.parse_args(argv)


def sidecar_for(blend):
    return os.path.join(os.path.dirname(blend), ".blender-ai", os.path.splitext(os.path.basename(blend))[0])


_EXPLAINS = ("script", "pipeline", "restore", "append", "save", "role")


def journal_since(path, since):
    """Journal entries written after `since` (an ISO time) that change the scene or save it: the AI's
    runs and saves. A file saved after them changed because of them, not by an unknown hand."""
    if not since or not os.path.exists(path):
        return []
    try:
        start = datetime.datetime.fromisoformat(since)
    except ValueError:
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
                when = datetime.datetime.fromisoformat(str(e.get("time")))
            except (ValueError, TypeError):
                continue
            if e.get("event") not in _EXPLAINS or e.get("rolled_back"):
                continue
            # Times have whole seconds: a run in the same second as the ingest came after it.
            try:
                later = when >= start
            except TypeError:  # one of them without a time zone
                later = when.replace(tzinfo=None) >= start.replace(tzinfo=None)
            if later:
                out.append(e)
    return out


def live_ingest(out, render_view, actor="ai", reason="", force=False, previews=True, preview_size=512):
    """Ingest what is open in this Blender, without saving it. Called by the VSBlender add-on."""
    argv = ["--actor", actor or "ai", "--preview-size", str(preview_size), "--max-group-previews", "4"]
    if reason:
        argv += ["--reason", reason]
    if force:
        argv.append("--force")
    if not previews:
        argv.append("--no-previews")
    return ingest(parse_args(argv), out=out, render_view=render_view, source="live")


def ingest(args, out=None, render_view=None, source="disk"):
    """Write NOTES.md, manifest.json, roles.json, journal.jsonl and state.json for the open file.

    source "disk": a headless Blender opened the saved file. source "live": the add-on calls this in
    the user's Blender, and the sidecar then describes the session including unsaved changes.
    """
    blend = bpy.data.filepath
    if not blend and source == "disk":
        raise SystemExit("open a saved .blend first: blender -b FILE.blend --python blender_ingest.py")
    out = out or args.out or sidecar_for(blend)
    os.makedirs(out, exist_ok=True)
    p = lambda name: os.path.join(out, name)

    sha = file_sha256(blend) if blend else None
    # Before anything else: the headless previews below change render settings and visibility.
    signature = _snapshot.signature()
    old_state = load_json(p("state.json"))
    if old_state and old_state.get("ingest_version") == INGEST_VERSION and not args.force:
        if source == "disk" and old_state.get("source", "disk") == "disk" and old_state.get("sha256") == sha:
            return {"status": "unchanged", "out": out, "source": source}
        if source == "live" and old_state.get("source") == "live" and old_state.get("signature") == signature:
            return {"status": "unchanged", "out": out, "source": source}

    old_manifest = load_json(p("manifest.json"))
    upgraded = bool(old_manifest) and old_manifest.get("ingest_version") != INGEST_VERSION
    old_roles = (load_json(p("roles.json")) or {}).get("objects", {})
    state = {"ingest_version": INGEST_VERSION, "blend": blend or None, "sha256": sha,
             "size": os.path.getsize(blend) if blend else 0,
             "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(blend)).astimezone().isoformat(timespec="seconds")
             if blend else None,
             "ingested_at": now(), "blender": bpy.app.version_string, "source": source, "signature": signature}
    if source == "live":
        state["dirty"] = bool(bpy.data.is_dirty)

    man = build_manifest(blend, sha)
    roles = build_roles(man, args.ignore_annotations)
    # An ingester upgrade changes what the manifest records, so a diff would report noise.
    changes = diff_manifests(old_manifest, man) if old_manifest and not upgraded else None
    renames = detect_renames(old_manifest.get("objects", {}), man["objects"]) if old_manifest else {}

    changed_subjects = {c["subject"] for c in changes or []}
    for n, r in roles.items():
        o = old_roles.get(n)
        old_name = next((a for a, b in renames.items() if b == n), None)
        if (o is None or o.get("source") not in ("ai", "human")) and old_name:
            o = old_roles.get(old_name)
        if o and o.get("source") in ("ai", "human"):
            r.update({k: o[k] for k in ("role", "source", "confidence", "needs_review") if k in o})
            if n in changed_subjects:
                r["changed_since_review"] = True
                r["needs_review"] = True

    previews = []
    if not args.no_previews:
        if render_view is not None:
            previews = render_previews_offscreen(out, man, args, render_view)
        elif bpy.app.background:
            previews = render_previews(out, man, args)
        else:
            print("previews skipped: they change render settings, so they only run in a background Blender (-b)")
    man["previews"] = previews

    tdir = p("texts")
    os.makedirs(tdir, exist_ok=True)
    for f in os.listdir(tdir):
        os.remove(os.path.join(tdir, f))
    for t in bpy.data.texts:
        with open(os.path.join(tdir, slug(t.name)), "w", encoding="utf-8", newline="\n") as f:
            f.write(t.as_string())

    write_json(p("manifest.json"), man)
    write_json(p("roles.json"), {
        "about": "What each object is for. source: annotation (from the file), inferred (guess, see evidence), "
                 "ai / human (reviewed; kept across re-ingests). Write reviewed roles back to the .blend as an "
                 "'ai_role' custom property when edits to the file are allowed.",
        "objects": roles})

    stamp = now()[:16].replace("T", " ")
    where = "live session" if source == "live" else "file"
    prev = ((old_state or {}).get("sha256") or "?")[:8]
    if changes is None and upgraded:
        event = "reingest"
        log_line = f"- {stamp} [ingest] notes rebuilt from the {where} by a newer ingester: {len(man['objects'])} objects."
    elif changes is None:
        event = "first_ingest"
        log_line = f"- {stamp} [ingest] first seen ({where}): {len(man['objects'])} objects, no prior history."
    else:
        event = ("live_change" if changes else "live_unchanged") if source == "live" else ("change" if changes else "resaved")
        who = args.actor
        why = f" {args.reason}." if args.reason else ""
        summary = "; ".join(f"{c['subject']}: {c['change']}" for c in changes[:8])
        more = f"; +{len(changes) - 8} more (journal.jsonl)" if len(changes) > 8 else ""
        # A save after the AI's runs: the journal (and the lines above) already say what changed and why.
        explained = journal_since(p("journal.jsonl"), (old_state or {}).get("ingested_at")) if source == "disk" else []
        runs = [e for e in explained if e.get("event") != "save"]
        saves = [e for e in explained if e.get("event") == "save"]
        if source == "live":
            log_line = (f"- {stamp} [{who}]{why} live ingest, {len(changes)} change(s) since the last ingest: {summary}{more}"
                        if changes else f"- {stamp} [{who}]{why} live ingest, no structural changes.")
        elif changes and runs:
            event = "saved"
            if who in ("external", "ingest"):
                who = str((saves[-1] if saves else runs[-1]).get("actor") or "ai")
            reason = args.reason or (saves[-1].get("reason") if saves else "") or ""
            why = f": {reason.rstrip('.')}" if reason else ""
            log_line = (f"- {stamp} [{who}] saved{why}. {len(changes)} change(s), from {len(runs)} run(s) logged above since the "
                        f"last ingest (and any edits by hand) (sha {prev} -> {sha[:8]}).")
        else:
            log_line = (f"- {stamp} [{who}]{why} {len(changes)} change(s) detected (sha {prev} -> {sha[:8]}): {summary}{more}"
                        if changes else f"- {stamp} [{who}]{why} file re-saved, no structural changes (sha {prev} -> {sha[:8]}).")
    write_notes(p("NOTES.md"), man, roles, state, previews, log_line)

    record = {"time": state["ingested_at"], "event": event,
              "actor": (who if event == "saved" else args.actor) if changes is not None else "ingest", "source": source,
              "reason": args.reason or None, "sha256": sha,
              "previous_sha256": old_state.get("sha256") if old_state else None,
              "changes": [f"{c['subject']}: {c['change']}" for c in changes or []]}
    if event == "saved":
        record["explained_by"] = [e.get("script") or e.get("pipeline") or e.get("event") for e in runs]
    with open(p("journal.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    write_json(p("state.json"), state)
    return {"status": event, "out": out, "source": source, "objects": len(man["objects"]),
            "changes": len(changes or []), "issues": len(man["issues"]),
            "previews": len([x for x in previews if x.get("file")])}


def main():
    args = parse_args()
    if args.diff:
        a, b = (load_json(path) for path in args.diff)
        if a is None or b is None:
            raise SystemExit(f"could not read both manifests: {args.diff}")
        changes = diff_manifests(a, b)
        if args.diff_to:
            write_json(args.diff_to, changes)
        return {"status": "diff", "changes": len(changes), "out": args.diff_to}
    if args.manifest_to:
        man = build_manifest(bpy.data.filepath, file_sha256(bpy.data.filepath) if bpy.data.filepath else None)
        os.makedirs(os.path.dirname(os.path.abspath(args.manifest_to)), exist_ok=True)
        write_json(args.manifest_to, man)
        return {"status": "manifest", "out": args.manifest_to, "objects": len(man["objects"])}
    return ingest(args)


if __name__ == "__main__":
    t0 = time.time()
    try:
        result = main()
    except SystemExit as e:
        result = {"status": "error", "error": str(e)}
    result["seconds"] = round(time.time() - t0, 1)
    print("INGEST_RESULT " + json.dumps(result))
    if result["status"] == "error":
        sys.exit(1)

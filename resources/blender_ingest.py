"""
blender_ingest.py - build AI context files for a .blend the assistant hasn't seen before (or that changed since).

Run headless. The .blend is only read, never saved:
    blender -b --factory-startup --disable-autoexec FILE.blend --python-exit-code 1 --python blender_ingest.py -- [options]

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

INGEST_VERSION = 1
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


def custom_props(idb):
    out = {}
    for k in sorted(idb.keys()):
        if k.startswith("_") or k in ("cycles", "cycles_visibility"):
            continue
        v = jsonable(idb[k])
        if isinstance(v, str) and len(v) > 2000:
            v = v[:2000] + "..."
        out[k] = v
    return out


def nondefault(s, skip=()):
    """RNA properties whose value differs from the default. Version-proof way to describe any struct."""
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
                    out[k] = v.name
                continue
            else:
                continue
            if v != d:
                out[k] = v
        except Exception:
            continue
    return out


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


def node_defaults(bl_idname, tree_type):
    key = (tree_type, bl_idname)
    if key not in _NODE_DEFAULTS:
        d = {}
        ng = bpy.data.node_groups.new("_ingest_scratch", tree_type)
        try:
            n = ng.nodes.new(bl_idname)
            d = {s.identifier: sockval(s) for s in n.inputs if hasattr(s, "default_value")}
        except Exception:
            pass
        bpy.data.node_groups.remove(ng)
        _NODE_DEFAULTS[key] = d
    return _NODE_DEFAULTS[key]


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
    bits = [v for v in nondefault(n, NODE_SKIP).values() if isinstance(v, str)]
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
    props = nondefault(n, NODE_SKIP)
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
            chans.append({"path": f"{fc.data_path}[{fc.array_index}]", "keys": len(fc.keyframe_points),
                          "frames": [rnd(a, 1), rnd(b, 1)]})
        out["action"] = act.name
        out["channels"] = sorted(chans, key=lambda c: c["path"])
    drivers = sorted(f"{d.data_path}[{d.array_index}] = {d.driver.expression or d.driver.type}" for d in ad.drivers)
    if drivers:
        out["drivers"] = drivers
    if len(getattr(ad, "nla_tracks", [])):
        out["nla_tracks"] = [t.name for t in ad.nla_tracks]
    return out or None


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
        pts = [eo.matrix_world @ Vector(c) for c in eo.bound_box]
        o["bbox"] = [vec([min(p[i] for p in pts) for i in range(3)]), vec([max(p[i] for p in pts) for i in range(3)])]
        o["dimensions"] = vec(eo.dimensions)

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
    return o


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
    man = {
        "ingest_version": INGEST_VERSION,
        "file": {"name": os.path.basename(blend_path), "size_mb": rnd(os.path.getsize(blend_path) / 1e6, 2),
                 "saved_with": ".".join(map(str, bpy.data.version)), "ingested_with": bpy.app.version_string,
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
        "texts": {t.name: {"lines": len(t.lines), "external_file": t.filepath or None,
                           "use_module": t.use_module} for t in sorted(bpy.data.texts, key=lambda t: t.name)},
        "stats": {"objects_by_type": dict(sorted(types.items())),
                  "verts": sum(m["verts"] for m in meshes), "faces": sum(m["faces"] for m in meshes),
                  "faces_after_modifiers": sum(m.get("evaluated", m)["faces"] for m in meshes),
                  "materials": len(bpy.data.materials), "images": len(images)},
    }
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
    for key, a in man["animation"].items():
        paths = sorted({c["path"] for part in a.values() for c in part.get("channels", [])})
        if paths:
            add("warn", key, f"keyframed values {paths[:4]}{' ...' if len(paths) > 4 else ''}: edit the keys, not the values")
    for n, m in man["materials"].items():
        if m["users"] == 0 and not m["fake_user"]:
            grouped["unused materials (dropped on save)"].append(f"material:{n}")
    for msg, names in grouped.items():
        add("info", ", ".join(names[:6]) + (f" (+{len(names) - 6} more)" if len(names) > 6 else ""),
            f"{len(names)} x {msg}")
    return warn + info


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
def framing(man, names=None):
    items = [(n, Vector(o["bbox"][0]), Vector(o["bbox"][1])) for n, o in man["objects"].items()
             if o.get("bbox") and not o["hidden"]["render"] and (names is None or n in names)]
    if not items:
        return None, []
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
    mn = Vector([min(i[1][k] for i in keep) for k in range(3)])
    mx = Vector([max(i[2][k] for i in keep) for k in range(3)])
    return (mn, mx), sorted(excluded)


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
    bounds, excluded = framing(man)
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
        cam.rotation_euler = Vector(d).to_track_quat("-Z", "Y").to_euler()
        cd.clip_start, cd.clip_end = max(0.001, dist * 0.001), dist * 4 + diag

    main = set(man["objects"]) - set(excluded)
    note = f"; hidden as outliers: {', '.join(excluded)}" if excluded else ""
    show_only(main)
    for name, d, ortho in (("front", (0, 1, 0), True), ("right", (-1, 0, 0), True),
                           ("top", (0, 0, -1), True), ("iso", (-1, 1, -0.8), False)):
        aim(bounds[0], bounds[1], d, ortho)
        shoot(f"{name}.png", f"{name} ({'orthographic' if ortho else 'perspective'}, Workbench random colours{note})")

    # 3. focused views of the largest groups: parent hierarchies and top-level collections
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
                groups.append((f"group_{slug(n)}", f"hierarchy under '{n}'", members))

    def col_objects(col):
        s = {o.name for o in col.objects}
        for c in col.children:
            s |= col_objects(c)
        return s

    seen = [g[2] for g in groups]
    for col in scene.collection.children:
        members = col_objects(col) & geom
        if len(members) >= 2 and len(members) < 0.9 * len(geom) and members not in seen:
            groups.append((f"collection_{slug(col.name)}", f"collection '{col.name}'", members))
    groups.sort(key=lambda g: -len(g[2]))
    for fname, label, members in groups[:args.max_group_previews]:
        gb, _ = framing(man, members)
        if gb is None:
            continue
        show_only(members)
        aim(gb[0], gb[1], (-1, 1, -0.8), False)
        shoot(f"{fname}.png", f"{label}: {len(members)} objects (perspective, Workbench)")
    show_only(None)
    return results


# ----------------------------------------------------------------------------- change detection
def diff_manifests(old, new):
    out = []

    def add(subject, change):
        out.append({"subject": subject, "change": change})

    oo, no = old.get("objects", {}), new.get("objects", {})
    for n in sorted(set(no) - set(oo)):
        add(n, f"added ({no[n]['type']})")
    for n in sorted(set(oo) - set(no)):
        add(n, "removed")
    for n in sorted(set(oo) & set(no)):
        a, b = oo[n], no[n]
        for k in ("parent", "location", "rotation_deg", "rotation_quat", "scale", "materials", "hidden", "data",
                  "custom_props", "light", "camera"):
            if a.get(k) != b.get(k):
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
    L = [f"**File** `{f['name']}` - {f['size_mb']} MB - saved with Blender {f['saved_with']} - "
         f"sha256 `{state['sha256'][:12]}` - ingested {state['ingested_at']} with Blender {f['ingested_with']}",
         "",
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
        return f"{'  ' * depth}- **{n}** {o['type']}{size} - {role} `{tag}`"

    def emit(names, depth):
        by_series = defaultdict(list)
        for n in sorted(names):
            by_series[series_base(n)].append(n)
        for base, members in sorted(by_series.items(), key=lambda kv: kv[1][0]):
            leafs = [m for m in members if not kids.get(m)]
            if len(leafs) >= 4:
                o0 = objs[leafs[0]]
                L.append(f"{'  ' * depth}- **{leafs[0]} ... {leafs[-1]}** ({len(leafs)} x {o0['type']}) - "
                         f"{roles[leafs[0]]['role'][:140]}")
                members = [m for m in members if m not in leafs]
            for m in members:
                L.append(line(m, depth))
                emit(kids.get(m, []), depth + 1)

    emit(kids[None], 0)

    mats = [(n, m) for n, m in man["materials"].items() if m["users"]]
    if mats:
        L += ["", "## Materials", ""]
        for n, m in mats:
            flags = [k for k in ("emissive", "transmissive") if m.get(k)]
            summ = m.get("summary", "")
            summ = summ if len(summ) <= 260 else summ[:257] + "..."
            L.append(f"- **{n}** ({m['users']} users{', ' + ', '.join(flags) if flags else ''}): `{summ}`")

    anim = [(n, o["animation"]) for n, o in objs.items() if o.get("animation")] + list(man["animation"].items())
    if anim:
        L += ["", "## Animation", ""]
        for n, a in anim:
            parts = []
            for part, ch in a.items():
                for c in ch.get("channels", [])[:4]:
                    parts.append(f"{c['path']} ({c['keys']} keys, f{c['frames'][0]:g}-{c['frames'][1]:g})")
                if len(ch.get("channels", [])) > 4:
                    parts.append(f"+{len(ch['channels']) - 4} more")
                parts += ch.get("drivers", [])
            L.append(f"- **{n}**: {'; '.join(parts)}")

    if man["issues"]:
        L += ["", "## Before you modify", ""]
        for i in man["issues"]:
            L.append(f"- {'**warn**' if i['severity'] == 'warn' else 'info'} `{i['subject']}`: {i['message']}")

    if man["texts"]:
        L += ["", "## Text blocks inside the .blend", "",
              "The author's own notes and scripts; copies are in `texts/`. Read these before inferring anything.", ""]
        for n, t in man["texts"].items():
            L.append(f"- `texts/{slug(n)}` ({t['lines']} lines{', runs on load' if t['use_module'] else ''})")

    L += ["", "## Files next to this one", "",
          "- `manifest.json`: full structural description (diff it to see what changed)",
          "- `roles.json`: per-object role, confidence, evidence. After reviewing, set `source` to `ai` or `human`; "
          "reviewed roles are kept on re-ingest",
          "- `journal.jsonl`: every detected change with actor and reason",
          "- `state.json`: fingerprint used to decide whether to re-ingest"]
    return "\n".join(L)


DEFAULT_TAIL = """
## Intent & constraints

<!-- Hand-written by people, or by the AI after review. Kept across re-ingests. What must not change, and why. -->

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
def parse_args():
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
    return p.parse_args(argv)


def main():
    args = parse_args()
    blend = bpy.data.filepath
    if not blend:
        raise SystemExit("open a saved .blend first: blender -b FILE.blend --python blender_ingest.py")
    out = args.out or os.path.join(os.path.dirname(blend), ".blender-ai", os.path.splitext(os.path.basename(blend))[0])
    os.makedirs(out, exist_ok=True)
    p = lambda name: os.path.join(out, name)

    sha = file_sha256(blend)
    old_state = load_json(p("state.json"))
    if (old_state and old_state.get("sha256") == sha and old_state.get("ingest_version") == INGEST_VERSION
            and not args.force):
        return {"status": "unchanged", "out": out}

    old_manifest = load_json(p("manifest.json"))
    old_roles = (load_json(p("roles.json")) or {}).get("objects", {})
    state = {"ingest_version": INGEST_VERSION, "blend": blend, "sha256": sha, "size": os.path.getsize(blend),
             "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(blend)).astimezone().isoformat(timespec="seconds"),
             "ingested_at": now(), "blender": bpy.app.version_string}

    man = build_manifest(blend, sha)
    roles = build_roles(man, args.ignore_annotations)
    changes = diff_manifests(old_manifest, man) if old_manifest else None

    changed_subjects = {c["subject"] for c in changes or []}
    for n, r in roles.items():
        o = old_roles.get(n)
        if o and o.get("source") in ("ai", "human"):
            r.update({k: o[k] for k in ("role", "source", "confidence", "needs_review") if k in o})
            if n in changed_subjects:
                r["changed_since_review"] = True
                r["needs_review"] = True

    previews = []
    if not args.no_previews:
        if bpy.app.background:
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
    if changes is None:
        event = "first_ingest"
        log_line = f"- {stamp} [ingest] first seen: {len(man['objects'])} objects, no prior history."
    else:
        event = "change" if changes else "resaved"
        who = args.actor
        why = f" {args.reason}." if args.reason else ""
        summary = "; ".join(f"{c['subject']}: {c['change']}" for c in changes[:8])
        more = f"; +{len(changes) - 8} more (journal.jsonl)" if len(changes) > 8 else ""
        prev = old_state["sha256"][:8] if old_state else "?"
        log_line = (f"- {stamp} [{who}]{why} {len(changes)} change(s) detected (sha {prev} -> {sha[:8]}): {summary}{more}"
                    if changes else f"- {stamp} [{who}]{why} file re-saved, no structural changes (sha {prev} -> {sha[:8]}).")
    write_notes(p("NOTES.md"), man, roles, state, previews, log_line)

    with open(p("journal.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"time": state["ingested_at"], "event": event, "actor": args.actor if changes is not None else "ingest",
                            "reason": args.reason or None, "sha256": sha,
                            "previous_sha256": old_state.get("sha256") if old_state else None,
                            "changes": [f"{c['subject']}: {c['change']}" for c in changes or []]},
                           ensure_ascii=False) + "\n")
    write_json(p("state.json"), state)
    return {"status": event, "out": out, "objects": len(man["objects"]), "changes": len(changes or []),
            "issues": len(man["issues"]), "previews": len([x for x in previews if x.get("file")])}


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

"""Read-only lookups for the AI: api, node_schema, describe, find, spatial.

Nothing here changes the scene. node_schema adds a scratch node group and removes it again.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import math
import os
import re

import bpy
from mathutils import Vector

from . import history, marks, snapshot

_ROOTS = ("bpy.context.", "bpy.data.", "C.", "D.")
_GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL", "GPENCIL"}


# ----------------------------------------------------------------------------- formatting
def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, bpy.types.ID):
        return repr(value.name)
    if isinstance(value, str):
        return repr(value) if len(value) < 80 else repr(value[:77] + "...")
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        try:
            items = list(value)
        except TypeError:
            return str(value)
        if len(items) > 16:
            return f"[{len(items)} items]"
        return "(" + ", ".join(_fmt(v) for v in items) + ")"
    return str(value)


def _prop_line(prop, instance=None, live_enum: bool = False) -> str:
    kind = prop.type
    if kind == "POINTER" or kind == "COLLECTION":
        target = getattr(getattr(prop, "fixed_type", None), "identifier", "?")
        line = f"{prop.identifier}: {kind} -> {target}"
    else:
        if getattr(prop, "is_array", False) and getattr(prop, "array_length", 0):
            kind += f"[{prop.array_length}]"
        if instance is not None:
            try:
                default = _fmt(getattr(instance, prop.identifier))
            except Exception:
                default = "?"
        elif prop.type == "ENUM":
            default = repr(sorted(prop.default_flag)) if prop.is_enum_flag else repr(prop.default)
        elif getattr(prop, "is_array", False):
            default = _fmt(list(prop.default_array))
        else:
            default = _fmt(getattr(prop, "default", None))
        line = f"{prop.identifier}: {kind} = {default}"
        if prop.type in {"INT", "FLOAT"}:
            lo, hi = getattr(prop, "hard_min", None), getattr(prop, "hard_max", None)
            if lo is not None and hi is not None and (lo > -1e30 or hi < 1e30):
                line += f" [{_fmt(lo)}..{_fmt(hi)}]"
        if prop.type == "ENUM":
            items = enum_values(prop, instance) if live_enum else [i.identifier for i in prop.enum_items]
            if items:
                shown = items[:24]
                line += " {" + ", ".join(shown) + (f", +{len(items) - 24}" if len(items) > 24 else "") + "}"
    if prop.is_readonly:
        line += " (read-only)"
    desc = (prop.description or "").strip()
    if desc:
        line += f"  # {desc[:110]}"
    return line


def enum_values(prop, instance=None) -> list:
    """Enum items, including dynamic ones that RNA does not list.

    view_settings.look and render.engine are filled at run time, so bl_rna reports only a
    placeholder. Assigning an invalid value makes Blender list the valid ones in the TypeError, and
    the assignment itself never happens.
    """
    static = [i.identifier for i in prop.enum_items]
    if instance is None or prop.is_readonly or getattr(prop, "is_enum_flag", False):
        return static
    try:
        setattr(instance, prop.identifier, "\u0000vsblender-probe")
    except TypeError as exc:
        match = re.search(r"not found in (\(.*\))", str(exc))
        if match:
            try:
                live = list(ast.literal_eval(match.group(1)))
                if live:
                    return [str(v) for v in live]
            except (ValueError, SyntaxError):
                pass
    except Exception:
        pass
    return static


# ----------------------------------------------------------------------------- api
def _type_by_name(name: str):
    name = name.replace("bpy.types.", "")
    return getattr(bpy.types, name, None)


def _resolve_live(path: str):
    """Walk a path like scene.eevee, object.data.energy, bpy.data.objects["Cube"].location."""
    text = path.strip()
    for prefix in _ROOTS:
        if text.startswith(prefix):
            base = bpy.context if prefix in ("bpy.context.", "C.") else bpy.data
            text = text[len(prefix):]
            break
    else:
        first = re.match(r"^[A-Za-z_]\w*", text)
        if first is None:
            raise KeyError(path)
        word = first.group(0)
        context_words = {"scene", "object", "active_object", "view_layer", "window_manager", "preferences",
                         "collection", "selected_objects", "material", "world"}
        if word == "world":
            base, text = bpy.context.scene, text  # scene.world
        elif word == "material":
            ob = bpy.context.object
            if ob is None or ob.active_material is None:
                raise KeyError("there is no active object with a material")
            base, text = ob, "active_material" + text[len(word):]
        elif word in context_words:
            base = bpy.context
        elif hasattr(bpy.data, word):
            base = bpy.data
        else:
            raise KeyError(path)
    current = base
    parent, last = None, None
    for token in re.finditer(r'\.?([A-Za-z_]\w*)|\[\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|-?\d+)\s*\]', text):
        name, key = token.group(1), token.group(2)
        parent = current
        if name is not None:
            last = name
            current = getattr(current, name)
        else:
            last = None
            current = current[ast.literal_eval(key)]
    return parent, last, current


def _inherited(cls) -> tuple:
    """Property and function names a type inherits from Node or ID (location, width, select, users...),
    which every node or datablock has and which bury the ones that matter."""
    props, funcs, bases = set(), set(), []
    for base in (bpy.types.Node, bpy.types.ID):
        if cls is not base and issubclass(cls, base):
            props |= {p.identifier for p in base.bl_rna.properties}
            funcs |= {f.identifier for f in base.bl_rna.functions}
            bases.append(base.__name__)
    return props, funcs, bases


def api(query: str, limit: int = 60, inherited: bool = False) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required, e.g. ShaderNodeTexSky, ColorManagedViewSettings.look, scene.eevee, or a word")
    # 1. A type, or Type.property.
    head, _, tail = query.replace("bpy.types.", "").partition(".")
    cls = _type_by_name(head)
    if cls is not None and hasattr(cls, "bl_rna"):
        rna = cls.bl_rna
        if tail:
            prop = rna.properties.get(tail)
            if prop is None:
                raise KeyError(f"{head} has no property {tail}. Properties: {', '.join(p.identifier for p in rna.properties)[:600]}")
            lines = [f"{head}.{_prop_line(prop)}"]
            if prop.type == "ENUM":
                for item in prop.enum_items:
                    lines.append(f"  {item.identifier}: {item.name}" + (f" - {item.description[:90]}" if item.description else ""))
                lines.append("Dynamic enums list more values on a live instance: pass a path such as scene.view_settings.look.")
            return {"text": "\n".join(lines)}
        lines = [f"{rna.identifier}" + (f" ({rna.base.identifier})" if rna.base else "") + (f": {rna.description}" if rna.description else "")]
        if issubclass(cls, bpy.types.Node):
            lines.append(f"Sockets are made when a node is created: node_schema(\"{rna.identifier}\") lists them.")
        skip_props, skip_funcs, bases = (set(), set(), []) if inherited else _inherited(cls)
        hidden = 0
        for prop in rna.properties:
            if prop.identifier == "rna_type":
                continue
            if prop.identifier in skip_props:
                hidden += 1
                continue
            lines.append("  " + _prop_line(prop))
        funcs = [f.identifier for f in rna.functions if f.identifier not in skip_funcs]
        if funcs:
            lines.append("functions: " + ", ".join(funcs))
        if hidden:
            lines.insert(1, f"({hidden} properties every {' / '.join(bases)} has are left out; inherited: true lists them)")
        return {"text": "\n".join(lines[: limit + 3])}
    # 2. A live path.
    try:
        parent, last, value = _resolve_live(query)
    except (KeyError, AttributeError, IndexError, TypeError, ValueError, SyntaxError):
        parent = value = None
        last = None
        live = False
    else:
        live = True
    if live:
        if hasattr(value, "bl_rna") and not isinstance(value, (str, bytes)):
            lines = [f"{query}: {value.bl_rna.identifier}" + (f" = {_fmt(value)}" if isinstance(value, bpy.types.ID) else "")]
            if hasattr(value, "__len__") and hasattr(value, "keys") and not hasattr(value, "is_property_set"):
                lines.append("  items: " + ", ".join(list(value.keys())[:limit]))
            for prop in value.bl_rna.properties:
                if prop.identifier == "rna_type":
                    continue
                lines.append("  " + _prop_line(prop, value, live_enum=prop.type == "ENUM"))
            return {"text": "\n".join(lines[: limit + 2])}
        if parent is not None and last and hasattr(parent, "bl_rna"):
            prop = parent.bl_rna.properties.get(last)
            if prop is not None:
                lines = [f"{query} = {_fmt(value)}", "  " + _prop_line(prop, parent, live_enum=True)]
                if prop.type == "ENUM":
                    live_items = enum_values(prop, parent)
                    static = [i.identifier for i in prop.enum_items]
                    if live_items != static:
                        lines.append(f"  live values ({len(live_items)}), RNA lists only {static}:")
                    for item in live_items:
                        lines.append(f"    {item}")
                return {"text": "\n".join(lines)}
        return {"text": f"{query} = {_fmt(value)}"}
    # 3. A word: search type names and property identifiers.
    needle = query.lower()
    hits = []
    for name in dir(bpy.types):
        cls = getattr(bpy.types, name, None)
        rna = getattr(cls, "bl_rna", None)
        if rna is None:
            continue
        if needle in name.lower():
            hits.append(f"type {name}")
        for prop in rna.properties:
            if needle in prop.identifier.lower() and prop.identifier != "rna_type":
                hits.append(f"{name}.{prop.identifier}: {prop.type}")
        if len(hits) > limit * 4:
            break
    hits = sorted(set(hits), key=lambda h: (not h.startswith("type"), len(h)))[:limit]
    if not hits:
        raise KeyError(f"nothing in bpy.types matches {query!r}")
    return {"text": f"{len(hits)} matches for {query!r} (pass Type or Type.property for details):\n  " + "\n  ".join(hits)}


# ----------------------------------------------------------------------------- node_schema
def _tree_type(bl_idname: str) -> str:
    for prefix, tree in (("ShaderNode", "ShaderNodeTree"), ("GeometryNode", "GeometryNodeTree"),
                         ("FunctionNode", "GeometryNodeTree"), ("CompositorNode", "CompositorNodeTree"),
                         ("TextureNode", "TextureNodeTree")):
        if bl_idname.startswith(prefix):
            return tree
    return "ShaderNodeTree"


def _socket_line(sock) -> str:
    kind = sock.type
    sub = sock.bl_idname.replace("NodeSocket", "")
    line = f"{sock.identifier}"
    if sock.name != sock.identifier:
        line += f' "{sock.name}"'
    line += f": {kind}"
    if sub and sub.upper() != kind and sub.lower() not in (kind.lower(), "float", "color", "vector", "shader"):
        line += f" ({sub})"
    if hasattr(sock, "default_value"):
        value = snapshot.socket_value(sock)
        if value is not None:
            line += f" = {_fmt(value)}"
        try:
            prop = sock.bl_rna.properties["default_value"]
            lo, hi = getattr(prop, "soft_min", None), getattr(prop, "soft_max", None)
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and (lo > -1e30 and hi < 1e30) and kind in ("VALUE", "INT"):
                line += f" [{_fmt(lo)}..{_fmt(hi)}]"
        except Exception:
            pass
    return line


def node_schema(bl_idname: str, props: dict | None = None) -> dict:
    cls = _type_by_name(bl_idname or "")
    if cls is None or not isinstance(cls, type) or not issubclass(cls, bpy.types.Node):
        needle = (bl_idname or "").lower().replace("shadernode", "").replace("geometrynode", "")
        nodes = []
        for name in dir(bpy.types):
            candidate = getattr(bpy.types, name, None)
            if needle and needle in name.lower() and isinstance(candidate, type) and issubclass(candidate, bpy.types.Node):
                nodes.append(name)
        raise KeyError(f"no node type {bl_idname!r}" + (f". Did you mean: {', '.join(nodes[:20])}" if nodes else ""))
    tree_type = _tree_type(bl_idname)
    tree = bpy.data.node_groups.new("_vsblender_scratch", tree_type)
    try:
        try:
            node = tree.nodes.new(bl_idname)
        except RuntimeError as exc:
            raise RuntimeError(f"{bl_idname} cannot be added to a {tree_type}: {exc}") from exc
        applied = []
        for key, value in (props or {}).items():
            try:
                setattr(node, key, value)
                applied.append(f"{key}={value!r}")
            except Exception as exc:
                raise ValueError(f"could not set {key}={value!r}: {exc}") from exc
        lines = [f'{bl_idname} "{node.bl_label}" in {tree_type}' + (f" with {', '.join(applied)}" if applied else "")]
        skip = snapshot.NODE_SKIP | {"name", "label", "parent", "location", "width", "height"}
        props_lines = []
        for prop in node.bl_rna.properties:
            if prop.identifier in skip or prop.identifier.startswith("bl_") or prop.identifier == "rna_type":
                continue
            if prop.type == "COLLECTION" or (prop.is_readonly and prop.type != "POINTER"):
                continue
            if prop.identifier in ("inputs", "outputs", "internal_links", "dimensions", "type", "mute", "select",
                                   "hide", "show_options", "show_preview", "show_texture", "use_custom_color", "color"):
                continue
            props_lines.append("  " + _prop_line(prop, node))
        if props_lines:
            lines.append("properties:")
            lines.extend(props_lines)
        for kind, sockets in (("inputs", node.inputs), ("outputs", node.outputs)):
            enabled = [s for s in sockets if getattr(s, "enabled", True)]
            hidden = [s.identifier for s in sockets if not getattr(s, "enabled", True)]
            lines.append(f"{kind}:" if enabled else f"{kind}: none")
            for sock in enabled:
                lines.append("  " + _socket_line(sock))
            if hidden:
                lines.append(f"  unavailable with these settings: {', '.join(hidden)}")
        lines.append("Use identifiers (left column) in code: names repeat, and are translated in a non-English UI.")
        return {"text": "\n".join(lines)}
    finally:
        bpy.data.node_groups.remove(tree)


# ----------------------------------------------------------------------------- targets
_PREFIX_ATTR = {code: attr for attr, code in snapshot.COLLECTIONS.items()}


def find_datablock(target: str):
    if not isinstance(target, str) or not target:
        raise ValueError("target is required")
    if len(target) > 3 and target[2] == ":" and target[:2] in _PREFIX_ATTR:
        attr = _PREFIX_ATTR[target[:2]]
        found = getattr(bpy.data, attr).get(target[3:])
        if found is None:
            raise KeyError(f"no {snapshot.singular(attr)} named {target[3:]!r}")
        return attr, found
    for attr in ("objects", "materials", "collections", "node_groups", "worlds", "meshes", "lights", "cameras",
                 "images", "actions", "texts", "scenes"):
        found = getattr(bpy.data, attr).get(target)
        if found is not None:
            return attr, found
    close = [o.name for o in bpy.data.objects if target.lower() in o.name.lower()][:10]
    raise KeyError(f"nothing is named {target!r}" + (f". Objects with that in the name: {', '.join(close)}" if close else ""))


def _roles(root: str) -> dict:
    data = history.load_json(os.path.join(history.sidecar_dir(root), "roles.json")) or {}
    return data.get("objects", {}) if isinstance(data, dict) else {}


def _world_box(ob, deps):
    try:
        ev = ob.evaluated_get(deps)
        pts = [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    except Exception:
        pts = [ob.matrix_world.translation.copy()]
    low = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    high = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return low, high


def _r(v, n=4):
    return [round(float(x), n) + 0.0 for x in v]


def describe(target: str, root: str, ingest_mod, frame=None) -> dict:
    """One datablock in full. frame: describe it as it is at that frame (the user's frame is restored)."""
    if frame is None:
        return _describe(target, root, ingest_mod)
    scene = bpy.context.scene
    saved = scene.frame_current
    scene.frame_set(int(frame))
    try:
        out = _describe(target, root, ingest_mod)
    finally:
        scene.frame_set(saved)
    return out


def _describe(target: str, root: str, ingest_mod) -> dict:
    from . import checks

    attr, idb = find_datablock(target)
    scene = bpy.context.scene
    deps = bpy.context.evaluated_depsgraph_get()
    out = {"id": snapshot.typed(attr, idb.name)}
    if attr == "objects":
        info = ingest_mod.object_info(idb, deps, scene, bpy.context.view_layer)
        info["frame"] = scene.frame_current
        role = _roles(root).get(idb.name)
        if role:
            info["role"] = {k: role.get(k) for k in ("role", "source", "confidence") if k in role}
        info["descendants"] = [c.name for c in getattr(idb, "children_recursive", [])][:80]
        keyed = checks.keyed_visibility(idb)
        if keyed:
            shows = f", visible from frame {keyed['visible_from']}" if keyed["visible_from"] is not None else ""
            info["keyed_visibility"] = (f"{'hidden' if keyed['hidden_now'] else 'shown'} at frame {keyed['frame']} by keys on "
                                        f"{', '.join(keyed['channels'])}{shows}")
        if info.get("evaluated") is False:
            low, high = (Vector(info["bbox"][0]), Vector(info["bbox"][1]))
            info["note"] = "hidden in the viewport, so not evaluated: bounds are from its own mesh, without modifiers"
        else:
            low, high = _world_box(idb, deps)
        info["world_bbox"] = [_r(low), _r(high)]
        info["world_center"] = _r((low + high) / 2)
        if idb.material_slots:
            mats = {}
            for slot in idb.material_slots:
                if slot.material is not None and slot.material.name not in mats:
                    mats[slot.material.name] = ingest_mod.material_info(slot.material).get("summary", "")[:300]
            info["material_summaries"] = mats
        from . import units as units_mod
        info["units"] = units_mod.label()
        out.update(info)
    elif attr == "materials":
        info = ingest_mod.material_info(idb)
        out.update({k: v for k, v in info.items() if k != "nodes"})
        out["nodes"] = info.get("nodes", {})
    elif attr == "worlds":
        out["settings"] = snapshot.rna_values(idb)
        if idb.node_tree is not None:
            out.update(ingest_mod.tree_info(idb.node_tree, "OUTPUT_WORLD"))
    elif attr == "node_groups":
        out.update({"type": idb.bl_idname, "users": idb.users})
        out["nodes"] = {n.name: ingest_mod.node_info(n) for n in idb.nodes}
        out["links"] = sorted(f"{l.from_node.name}.{l.from_socket.identifier} -> {l.to_node.name}.{l.to_socket.identifier}" for l in idb.links)
    elif attr == "collections":
        out.update({"objects": sorted(o.name for o in idb.objects), "children": sorted(c.name for c in idb.children),
                    "all_objects": len(idb.all_objects), "hide_render": idb.hide_render})
    elif attr == "meshes":
        out.update({"verts": len(idb.vertices), "edges": len(idb.edges), "faces": len(idb.polygons),
                    "materials": [m.name if m else None for m in idb.materials],
                    "uv_layers": [uv.name for uv in idb.uv_layers],
                    "attributes": sorted(f"{a.name} ({a.domain}, {a.data_type})" for a in idb.attributes)})
    elif attr == "actions":
        out["channels"] = [f"{fc.data_path}[{fc.array_index}]: {len(fc.keyframe_points)} keys "
                           f"{_r(fc.range(), 1)}" for fc in snapshot.action_fcurves(idb)]
    elif attr == "scenes":
        out["settings"] = {k: v for k, v in snapshot.scene_values(idb).items()
                           if not k.startswith(("display.", "display_settings", "sequencer"))}
    else:
        out["settings"] = snapshot.rna_values(idb)
        tree = getattr(idb, "node_tree", None)
        if tree is not None:
            out.update(ingest_mod.tree_info(tree, "OUTPUT_LIGHT" if attr == "lights" else "OUTPUT_MATERIAL"))
    anim = ingest_mod.anim_channels(idb)
    tree = getattr(idb, "node_tree", None)
    tree_anim = ingest_mod.anim_channels(tree) if tree is not None else None
    if anim and attr != "objects":
        out["animation"] = anim
    if tree_anim:
        out["node_tree_animation"] = tree_anim
    try:
        users = bpy.data.user_map(subset=[idb]).get(idb, set())
        out["used_by"] = sorted(snapshot.typed(_attr_of(u), u.name) for u in users if not u.name.startswith("_vsblender"))[:60]
    except Exception:
        pass
    props = {k: v for k, v in idb.items()} if hasattr(idb, "items") else {}
    if props and attr != "objects":
        out["custom_props"] = {k: str(v)[:300] for k, v in props.items() if not k.startswith("_")}
    return {"text": json.dumps(out, indent=1, ensure_ascii=False, default=str)}


_CLASS_ATTR = {"Object": "objects", "Mesh": "meshes", "Material": "materials", "Light": "lights",
               "Camera": "cameras", "Collection": "collections", "Image": "images", "World": "worlds",
               "Text": "texts", "Action": "actions", "Scene": "scenes", "Curve": "curves", "TextCurve": "curves",
               "SurfaceCurve": "curves", "Armature": "armatures", "Lattice": "lattices", "MetaBall": "metaballs",
               "VectorFont": "fonts", "ParticleSettings": "particles", "Library": "libraries"}


def _attr_of(idb) -> str:
    if isinstance(idb, bpy.types.NodeTree):
        return "node_groups"
    if isinstance(idb, bpy.types.Texture):
        return "textures"
    return _CLASS_ATTR.get(type(idb).__name__, type(idb).__name__.lower() + "s")


# ----------------------------------------------------------------------------- find
def _tokens(selector: str) -> list:
    pattern = r'\(|\)|"[^"]*"|\'[^\']*\'|\[[^\]]*\]|[^\s()"\'\[]+(?:"[^"]*"|\'[^\']*\'|\[[^\]]*\])?[^\s()]*'
    return re.findall(pattern, selector)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


class _Ctx:
    def __init__(self, root: str):
        self.deps = bpy.context.evaluated_depsgraph_get()
        self.roles = _roles(root)
        self.boxes = {}
        self.ingest_emissive = {}

    def box(self, ob):
        if ob.name not in self.boxes:
            self.boxes[ob.name] = _world_box(ob, self.deps)
        return self.boxes[ob.name]


def _emissive(mat) -> bool:
    tree = getattr(mat, "node_tree", None)
    if tree is None:
        return False
    for n in tree.nodes:
        if n.type == "EMISSION":
            return True
        if n.type == "BSDF_PRINCIPLED":
            s = n.inputs.get("Emission Strength")
            if s is not None and (s.is_linked or (getattr(s, "default_value", 0) or 0) > 0):
                return True
    return False


def _term(term: str, ob, ctx: _Ctx):
    """True/False plus a short reason."""
    low = term.lower()
    if ":" in term and not term.startswith(("'", '"')):
        key, _, raw = term.partition(":")
        key = key.lower()
        value = _unquote(raw)
        if key == "type":
            return ob.type == value.upper()
        if key == "name":
            return fnmatch.fnmatchcase(ob.name, value)
        if key == "collection":
            return any(fnmatch.fnmatchcase(c.name, value) for c in ob.users_collection) or any(
                fnmatch.fnmatchcase(c.name, value) and ob.name in c.all_objects for c in bpy.data.collections)
        if key == "material":
            return any(s.material is not None and fnmatch.fnmatchcase(s.material.name, value) for s in ob.material_slots)
        if key == "parent":
            return ob.parent is not None and fnmatch.fnmatchcase(ob.parent.name, value)
        if key == "built_by":
            script = str(ob.get("ai_built_by") or ob.get("ai_modified_by") or ob.get("ai_regenerate_with") or "")
            return bool(script) and (fnmatch.fnmatchcase(script, value) or fnmatch.fnmatchcase(os.path.basename(script), value)
                                     or value.lower() in script.lower())
        if key == "part":
            try:
                part = json.loads(ob.get("ai_spec") or "{}").get("part") if isinstance(ob.get("ai_spec"), str) else None
            except ValueError:
                part = None
            return bool(part) and fnmatch.fnmatchcase(str(part), value)
        if key == "from":
            source = str(ob.get("ai_appended_from") or "")
            return bool(source) and (fnmatch.fnmatchcase(source, value) or value.lower() in source.lower())
        if key in ("children_of", "under"):
            p = ob.parent
            while p is not None:
                if fnmatch.fnmatchcase(p.name, value):
                    return True
                p = p.parent
            return False
        if key == "has":
            kind, _, what = value.partition(":")
            if kind == "modifier":
                return any(not what or m.type == what.upper() or fnmatch.fnmatchcase(m.name, what) for m in ob.modifiers)
            if kind == "constraint":
                return any(not what or c.type == what.upper() for c in ob.constraints)
            if kind == "material":
                return any(s.material is not None for s in ob.material_slots)
            raise ValueError(f"has:{kind} is not known; use has:modifier[:TYPE], has:constraint[:TYPE], has:material")
        if key == "prop":
            name, _, want = value.partition("=")
            if name not in ob.keys():
                return False
            return not want or str(ob[name]) == want
        if key == "within":
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", value)]
            if len(nums) != 6:
                raise ValueError("within:[x0,y0,z0,x1,y1,z1]")
            low, high = ctx.box(ob)
            c = (low + high) / 2
            return all(nums[i] <= c[i] <= nums[i + 3] for i in range(3))
        if key == "near":
            match = re.match(r'^(.*?)\s*<\s*(-?\d+(?:\.\d+)?)$', value)
            if not match:
                raise ValueError('near:"Object"<distance')
            other = bpy.data.objects.get(_unquote(match.group(1)))
            if other is None:
                raise KeyError(f"near: no object named {match.group(1)!r}")
            if other == ob:
                return False
            a, b = ctx.box(ob), ctx.box(other)
            return ((a[0] + a[1]) / 2 - (b[0] + b[1]) / 2).length < float(match.group(2))
        raise ValueError(f"unknown selector {key}:")
    if "~" in term:
        key, _, raw = term.partition("~")
        if key.lower() != "role":
            raise ValueError(f"unknown selector {key}~ (only role~text)")
        text = _unquote(raw).lower()
        role = str(ctx.roles.get(ob.name, {}).get("role") or "")
        for k in ob.keys():
            if k.lower().endswith(("role", "description", "purpose")):
                role += " " + str(ob[k])
        return text in role.lower()
    flags = {
        "animated": lambda: bool(ob.animation_data and (ob.animation_data.action or ob.animation_data.drivers))
        or bool(ob.data is not None and getattr(ob.data, "animation_data", None) and ob.data.animation_data.action),
        "emissive": lambda: any(s.material is not None and _emissive(s.material) for s in ob.material_slots),
        "visible": lambda: not ob.hide_render and not ob.hide_get(),
        "hidden": lambda: ob.hide_render or ob.hide_get(),
        "selected": lambda: ob.select_get(),
        "active": lambda: bpy.context.view_layer.objects.active == ob,
        "derived": lambda: "ai_derived_from" in ob.keys(),
        "built": lambda: "ai_built_by" in ob.keys(),
        "reference": lambda: marks.is_reference(ob),
        "stage": lambda: marks.is_stage(ob),
        "linked": lambda: ob.library is not None,
    }
    if low in flags:
        try:
            return bool(flags[low]())
        except Exception:
            return False
    return fnmatch.fnmatchcase(ob.name, _unquote(term)) or (not any(ch in term for ch in "*?[") and _unquote(term).lower() in ob.name.lower())


def _parse(tokens: list):
    """Boolean expression over terms: not > and > or, with parentheses. Adjacent terms mean and."""
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def take():
        pos[0] += 1
        return tokens[pos[0] - 1]

    def expr():
        node = conj()
        while peek() is not None and peek().lower() == "or":
            take()
            node = ("or", node, conj())
        return node

    def conj():
        node = unary()
        while peek() is not None and peek() != ")" and peek().lower() != "or":
            if peek().lower() == "and":
                take()
            node = ("and", node, unary())
        return node

    def unary():
        tok = peek()
        if tok is None:
            raise ValueError("selector ended early")
        if tok.lower() == "not":
            take()
            return ("not", unary())
        if tok == "(":
            take()
            node = expr()
            if peek() != ")":
                raise ValueError("missing )")
            take()
            return node
        return ("term", take())

    tree = expr()
    if pos[0] != len(tokens):
        raise ValueError(f"could not read the selector after {' '.join(tokens[:pos[0]])!r}")
    return tree


def _evaluate(tree, ob, ctx, reasons):
    kind = tree[0]
    if kind == "term":
        ok = _term(tree[1], ob, ctx)
        if ok:
            reasons.append(tree[1])
        return ok
    if kind == "not":
        return not _evaluate(tree[1], ob, ctx, [])
    left = _evaluate(tree[1], ob, ctx, reasons)
    if kind == "and":
        return left and _evaluate(tree[2], ob, ctx, reasons)
    return left or _evaluate(tree[2], ob, ctx, reasons)


def find(selector: str, root: str, limit: int = 100) -> dict:
    if not selector or not selector.strip():
        raise ValueError("selector is required, e.g. type:MESH and children_of:\"SG Stargate\"")
    tree = _parse(_tokens(selector))
    ctx = _Ctx(root)
    hits = []
    for ob in sorted(bpy.context.scene.objects, key=lambda o: o.name):
        if ob.name.startswith("_vsblender"):
            continue
        reasons = []
        if _evaluate(tree, ob, ctx, reasons):
            role = ctx.roles.get(ob.name, {}).get("role")
            hits.append({"name": ob.name, "type": ob.type, "matched": sorted(set(reasons)),
                         **({"role": str(role)[:120]} if role else {})})
    shown = hits[:limit]
    lines = [f"{len(hits)} object(s) match {selector!r}" + (f"; first {limit} shown" if len(hits) > limit else "")]
    for hit in shown:
        lines.append(f"- {hit['name']} ({hit['type']})" + (f": {hit['role']}" if hit.get("role") else "")
                     + (f"  [{', '.join(hit['matched'])}]" if hit["matched"] else ""))
    return {"text": "\n".join(lines), "names": [h["name"] for h in hits]}


# ----------------------------------------------------------------------------- spatial
def resolve_objects(spec, root: str, include_hidden: bool = True) -> list:
    """Objects for a target spec: None (the scene), an exact name, a find selector, or a list of names.

    include_hidden=False drops objects hidden in the viewport or for rendering (unless named exactly).
    """
    obs = _objects_for(spec, root)
    if include_hidden or not isinstance(spec, str) or spec in bpy.data.objects:
        return obs
    return [o for o in obs if not o.hide_render and not o.hide_get()]


def _objects_for(spec, root: str) -> list:
    if spec is None:
        return [o for o in bpy.context.scene.objects if not o.name.startswith("_vsblender")]
    if isinstance(spec, str):
        if spec in bpy.data.objects:
            return [bpy.data.objects[spec]]
        return [bpy.data.objects[n] for n in find(spec, root, limit=100000)["names"]]
    out = []
    for name in spec:
        ob = bpy.data.objects.get(name)
        if ob is None:
            raise KeyError(f"no object named {name!r}")
        out.append(ob)
    return out


def _box_dict(low, high) -> dict:
    return {"min": _r(low), "max": _r(high), "center": _r((low + high) / 2), "size": _r(high - low)}


def _gap(a, b) -> tuple:
    """Per-axis gap between two boxes (negative = overlap) and the distance between them."""
    per = [max(b[0][i] - a[1][i], a[0][i] - b[1][i]) for i in range(3)]
    outside = [max(0.0, g) for g in per]
    return per, math.sqrt(sum(g * g for g in outside))


def spatial(params: dict, root: str) -> dict:
    from . import units as units_mod

    out = _spatial(params, root)
    out["units"] = units_mod.label()
    return out


def _spatial(params: dict, root: str) -> dict:
    op = params.get("op") or "bbox"
    deps = bpy.context.evaluated_depsgraph_get()
    scene = bpy.context.scene
    if op == "bbox":
        obs = _objects_for(params.get("targets"), root)
        rows = {}
        total_low = Vector((math.inf,) * 3)
        total_high = Vector((-math.inf,) * 3)
        for ob in obs[:200]:
            low, high = _world_box(ob, deps)
            rows[ob.name] = _box_dict(low, high)
            total_low = Vector(map(min, total_low, low))
            total_high = Vector(map(max, total_high, high))
        out = {"objects": rows}
        if len(rows) > 1:
            out["combined"] = _box_dict(total_low, total_high)
        return out
    if op == "raycast":
        origin = Vector(params.get("origin") or (0, 0, 0))
        direction = Vector(params.get("direction") or (0, 0, -1))
        if direction.length == 0:
            raise ValueError("direction must not be zero")
        direction.normalize()
        distance = float(params.get("max_distance") or 1e6)
        hit, loc, normal, _index, ob, _m = scene.ray_cast(deps, origin, direction, distance=distance)
        if not hit:
            return {"hit": False}
        original = getattr(ob, "original", ob)
        return {"hit": True, "object": original.name, "location": _r(loc), "normal": _r(normal),
                "distance": round((loc - origin).length, 4)}
    if op == "drop":
        from . import helpers
        points = params.get("points") or []
        if not points or len(points) > 2000:
            raise ValueError("points: 1 to 2000 [x, y] pairs")
        limit = _objects_for(params.get("targets"), root) if params.get("targets") else None
        rows = []
        for point in points:
            hit = helpers.raycast_down(float(point[0]), float(point[1]), params.get("from_z"), objects=limit, depsgraph=deps)
            rows.append(None if hit is None else {"xy": _r(point[:2]), "z": round(hit[0].z, 4), "object": hit[2].name,
                                                   "normal": _r(hit[1])})
        return {"hits": rows}
    if op in ("nearest", "distance", "below", "above", "touching"):
        if op == "distance":
            a = _objects_for([params["a"]], root)[0]
            b = _objects_for([params["b"]], root)[0]
            ba, bb = _world_box(a, deps), _world_box(b, deps)
            per, dist = _gap(ba, bb)
            return {"a": a.name, "b": b.name, "center_distance": round(((ba[0] + ba[1]) / 2 - (bb[0] + bb[1]) / 2).length, 4),
                    "bbox_gap": round(dist, 4), "gap_per_axis": _r(per),
                    "overlap": all(g < 0 for g in per)}
        target = _objects_for([params.get("target")], root)[0]
        tb = _world_box(target, deps)
        # Default tolerance: 0.5% of the target's size, so it means the same in a mm or a metre scene.
        touch_tol = float(params.get("tolerance") or max((tb[1] - tb[0]).length * 0.005, 1e-9))
        rows = []
        for ob in _objects_for(params.get("targets"), root):
            # Empties, lamps and cameras have no surface to be under, over or touching.
            if ob == target or ob.type not in _GEOMETRY:
                continue
            ob_box = _world_box(ob, deps)
            per, dist = _gap(tb, ob_box)
            xy_overlap = per[0] < 0 and per[1] < 0
            if op == "below" and not (xy_overlap and ob_box[1].z <= tb[0].z + 1e-4):
                continue
            if op == "above" and not (xy_overlap and ob_box[0].z >= tb[1].z - 1e-4):
                continue
            if op == "touching" and dist > touch_tol:
                continue
            rows.append((dist, ob.name, _r(per)))
        rows.sort()
        k = int(params.get("k") or 10)
        return {"target": target.name, op: [{"name": n, "bbox_gap": round(d, 4), "gap_per_axis": p} for d, n, p in rows[:k]]}
    if op == "within":
        low = Vector(params.get("min") or (-1e9,) * 3)
        high = Vector(params.get("max") or (1e9,) * 3)
        names = []
        for ob in _objects_for(params.get("targets"), root):
            ob_low, ob_high = _world_box(ob, deps)
            if all(ob_high[i] >= low[i] and ob_low[i] <= high[i] for i in range(3)):
                names.append(ob.name)
        return {"objects": names}
    raise ValueError("op must be bbox, raycast, drop, nearest, distance, below, above, touching, or within")


# ----------------------------------------------------------------------------- timeline
def _anim_sources(objects) -> list:
    """(label, idb) pairs whose animation_data can hold keys: objects, their data, shape keys,
    materials and node trees."""
    seen = set()
    out = []

    def add(label, idb):
        if idb is None or idb.as_pointer() in seen:
            return
        seen.add(idb.as_pointer())
        out.append((label, idb))

    for ob in objects:
        add(f"OB:{ob.name}", ob)
        data = getattr(ob, "data", None)
        if data is not None:
            add(f"{type(data).__name__}:{data.name}", data)
            add(f"{data.name} shape keys", getattr(data, "shape_keys", None))
            add(f"{data.name} nodes", getattr(data, "node_tree", None))
        for slot in getattr(ob, "material_slots", []):
            if slot.material is not None:
                add(f"MA:{slot.material.name}", slot.material)
                add(f"MA:{slot.material.name}", getattr(slot.material, "node_tree", None))
    return out


def _channel_fcurves(idb) -> list:
    ad = getattr(idb, "animation_data", None)
    if not ad or ad.action is None:
        return []
    act = ad.action
    if getattr(ad, "action_slot", None) is not None:
        try:
            from bpy_extras import anim_utils
            bag = anim_utils.action_get_channelbag_for_slot(act, ad.action_slot)
            return list(bag.fcurves) if bag else []
        except Exception:
            pass
    return list(getattr(act, "fcurves", []) or [])


def _value_text(data_path: str, value: float) -> str:
    if "rotation_euler" in data_path or data_path.endswith(("angle", "rotation")):
        return f"{math.degrees(value):.3f}°"
    return _fmt(value)


def timeline(params: dict, root: str) -> dict:
    """Keyframes in time order across objects (op keys), or channel values at given frames (op evaluate)."""
    from . import blender_ingest, helpers

    op = params.get("op") or "keys"
    scene = bpy.context.scene
    if op == "evaluate":
        target = params.get("target")
        path = params.get("path")
        if not target or not path:
            raise ValueError("evaluate needs target (a datablock or typed id) and path, e.g. rotation_euler "
                             "or Principled BSDF/Emission Strength")
        owner, data_path, index = helpers.resolve(target, str(path))
        curves = [fc for fc in _channel_fcurves(owner) if fc.data_path == data_path and (index is None or fc.array_index == index)]
        if not curves:
            raise KeyError(f"{data_path} on {owner.name} has no keyframes")
        frames = params.get("frames") or [scene.frame_start, scene.frame_current, scene.frame_end]
        rows = {}
        for fc in curves:
            label = blender_ingest.channel_label(owner, fc.data_path, fc.array_index)
            rows[label] = {str(f): _value_text(fc.data_path, fc.evaluate(float(f))) for f in frames}
        lines = [f"{owner.name}: {label} " + ", ".join(f"f{k} = {v}" for k, v in values.items()) for label, values in rows.items()]
        return {"text": "\n".join(lines), "values": rows}
    if op != "keys":
        raise ValueError("op must be keys or evaluate")
    spec = params.get("targets")
    objects = _objects_for(spec, root) if spec else [o for o in scene.objects if not o.name.startswith("_vsblender")]
    sources = _anim_sources(objects)
    if not spec and scene.world is not None:
        sources += [(f"WO:{scene.world.name}", scene.world), (f"WO:{scene.world.name}", getattr(scene.world, "node_tree", None))]
    frames = params.get("frames")
    lo, hi = -math.inf, math.inf
    wanted = None
    if isinstance(frames, dict):
        lo, hi = float(frames.get("start", -math.inf)), float(frames.get("end", math.inf))
    elif isinstance(frames, list):
        wanted = {round(float(f), 3) for f in frames}
    events = {}
    drivers = []
    for label, idb in sources:
        if idb is None:
            continue
        name = label.split(":", 1)[-1]
        for fc in _channel_fcurves(idb):
            channel = blender_ingest.channel_label(idb, fc.data_path, fc.array_index)
            points = list(fc.keyframe_points)
            for i, kp in enumerate(points):
                f = round(float(kp.co[0]), 3)
                if not (lo <= f <= hi) or (wanted is not None and f not in wanted):
                    continue
                prev = points[i - 1] if i > 0 else None
                change = f" (from {_value_text(fc.data_path, prev.co[1])} at {prev.co[0]:g})" if prev is not None else ""
                ease = kp.interpolation.lower()
                if kp.interpolation not in ("CONSTANT", "LINEAR") and kp.easing != "AUTO":
                    ease += " " + kp.easing.lower().replace("_", " ")
                events.setdefault(f, []).append(f"{name} {channel} = {_value_text(fc.data_path, kp.co[1])}{change}, {ease}")
        ad = getattr(idb, "animation_data", None)
        if ad is not None:
            for d in ad.drivers:
                drivers.append(f"{label} {d.data_path}[{d.array_index}] = {d.driver.expression or d.driver.type}")
    limit = int(params.get("limit") or 200)
    lines = []
    for f in sorted(events):
        for entry in events[f]:
            lines.append(f"frame {f:g}: {entry}")
    total = len(lines)
    text = [f"{total} keyframe(s) on {len(events)} frame(s); scene frames {scene.frame_start}-{scene.frame_end} at {scene.render.fps} fps"]
    text += lines[:limit]
    if total > limit:
        text.append(f"... {total - limit} more (pass frames {{start, end}}, targets, or limit)")
    if drivers:
        text.append("drivers: " + "; ".join(drivers[:20]))
    return {"text": "\n".join(text), "frames": sorted(events), "keys": total}

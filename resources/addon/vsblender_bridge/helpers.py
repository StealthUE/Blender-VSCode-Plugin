"""Helpers for workspace scripts. Inside run_script: `import vsblender`.

    sock(node, "Fac")                    socket by identifier, then by displayed name
    build(tree, {...}, links=[...])      create or patch nodes by name, link them, lay them out
    set_keys(mat, "Principled BSDF/Emission Strength", [(1, 0), (24, 8)])
    scale_keys(obj, "rotation_euler", lambda v: v * 2, index=2)
    bbox_world(obj) / raycast_down(x, y)
    progress(0.4, "raycasting")          reported to the AI client; raises Cancelled when it cancels
    with view3d_override(): bpy.ops...   for operators that need a 3D view
    mark_derived(obj, sources)           record that obj is generated from sources by this script
"""
from __future__ import annotations

import contextlib
import math
import os
import re
import sys
import time

import bpy
from mathutils import Vector

from . import snapshot

__all__ = [
    "Cancelled", "progress", "cancelled", "sock", "node", "link", "build", "layout", "ramp", "fcurves",
    "fcurve", "resolve", "set_keys", "scale_keys", "bbox_world", "raycast_down", "view3d_override",
    "mark_derived", "socket_path",
]


class Cancelled(BaseException):
    """Raised by progress() after the client cancels. A BaseException, so `except Exception` lets it through."""


_progress = {"fraction": None, "message": "", "updated": 0.0, "cancel": False, "active": False, "started": 0.0,
             "label": ""}
_root = {"path": ""}


def _begin(label: str, root: str) -> None:
    _progress.update(fraction=None, message="", updated=time.time(), cancel=False, active=True,
                     started=time.time(), label=label)
    _root["path"] = root


def _end() -> None:
    _progress.update(active=False, cancel=False)


def progress_state() -> dict:
    """Read on the socket thread by ping, while the main thread runs the script."""
    if not _progress["active"]:
        return {}
    return {"label": _progress["label"], "fraction": _progress["fraction"], "message": _progress["message"],
            "seconds": round(time.time() - _progress["started"], 1)}


def request_cancel() -> bool:
    if not _progress["active"]:
        return False
    _progress["cancel"] = True
    return True


def progress(fraction: float | None = None, message: str = "") -> None:
    """Report progress of a long script. Cheap; call it in loops. Raises Cancelled after a cancel."""
    if fraction is not None:
        try:
            _progress["fraction"] = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            pass
    if message:
        _progress["message"] = str(message)[:200]
    _progress["updated"] = time.time()
    if _progress["cancel"]:
        raise Cancelled("cancelled by the client")


def cancelled() -> bool:
    return bool(_progress["cancel"])


# ----------------------------------------------------------------------------- nodes
def sock(node, key, output: bool = False):
    """A socket by identifier, then by displayed name, preferring sockets that are enabled.

    The Mix node has several sockets named "A"; by name this returns the one the current data_type
    uses. Identifiers (A_Color, Factor_Float, Fac) are exact.
    """
    sockets = node.outputs if output else node.inputs
    if isinstance(key, int):
        return sockets[key]
    for s in sockets:
        if s.identifier == key:
            return s
    named = [s for s in sockets if s.name == key]
    enabled = [s for s in named if getattr(s, "enabled", True)]
    if enabled or named:
        return (enabled or named)[0]
    lowered = [s for s in sockets if s.name.lower() == str(key).lower() or s.identifier.lower() == str(key).lower()]
    if lowered:
        return lowered[0]
    kind = "outputs" if output else "inputs"
    names = ", ".join(f"{s.identifier}" + (f" ({s.name})" if s.name != s.identifier else "") for s in sockets)
    raise KeyError(f"{node.name} has no {kind[:-1]} {key!r}. {kind}: {names}")


def socket_path(node, socket) -> str:
    """The data path animation uses for a socket's value, relative to the node tree."""
    kind = "outputs" if socket.is_output else "inputs"
    collection = node.outputs if socket.is_output else node.inputs
    index = list(collection).index(socket)
    return f'nodes["{node.name}"].{kind}[{index}].default_value'


def _set_input(node, key, value) -> None:
    target = sock(node, key)
    try:
        target.default_value = value
    except (TypeError, ValueError):
        if isinstance(value, (tuple, list)) and len(value) == 3:
            target.default_value = (*value, 1.0)
        else:
            raise


def node(tree, bl_idname: str, name: str | None = None, inputs: dict | None = None, props: dict | None = None):
    """Get the node called name if it has this type, or make it. Then set props and inputs."""
    found = tree.nodes.get(name) if name else None
    if found is not None and found.bl_idname != bl_idname:
        tree.nodes.remove(found)
        found = None
    if found is None:
        found = tree.nodes.new(bl_idname)
        if name:
            found.name = name
    for key, value in (props or {}).items():
        setattr(found, key, value)
    for key, value in (inputs or {}).items():
        _set_input(found, key, value)
    return found


def link(tree, from_node, from_key, to_node, to_key):
    return tree.links.new(sock(from_node, from_key, output=True), sock(to_node, to_key))


def build(tree, spec: dict, links=(), clear: bool = False, arrange: bool = True) -> dict:
    """Create or patch a node graph.

        build(mat.node_tree, {
            "Principled BSDF": ("ShaderNodeBsdfPrincipled", {"Roughness": 0.6}),
            "Noise": ("ShaderNodeTexNoise", {"Scale": 4.0}, {"noise_dimensions": "3D"}),
            "Material Output": "ShaderNodeOutputMaterial",
        }, links=[("Noise", "Fac", "Principled BSDF", "Roughness"),
                  ("Principled BSDF", "BSDF", "Material Output", "Surface")])

    Keys are node names. Keep the names of keyframed nodes: material fcurves address nodes by name,
    so a recreated node under another name loses its animation. clear=True removes nodes not in spec.
    """
    made = {}
    if clear:
        for existing in list(tree.nodes):
            if existing.name not in spec:
                tree.nodes.remove(existing)
    for name, entry in spec.items():
        if isinstance(entry, str):
            entry = (entry,)
        bl_idname = entry[0]
        inputs = entry[1] if len(entry) > 1 else None
        props = entry[2] if len(entry) > 2 else None
        made[name] = node(tree, bl_idname, name, props=props)
        if inputs:
            for key, value in inputs.items():
                _set_input(made[name], key, value)
    for a, a_key, b, b_key in links:
        link(tree, made.get(a) or tree.nodes[a], a_key, made.get(b) or tree.nodes[b], b_key)
    if arrange:
        layout(tree)
    return made


def layout(tree, dx: float = 280.0, dy: float = 220.0) -> None:
    """Columns by distance from the output nodes, so the graph reads left to right."""
    depth = {}
    outputs = [n for n in tree.nodes if not n.outputs or n.type.startswith("OUTPUT") or n.bl_idname == "NodeGroupOutput"]

    def visit(n, d, seen):
        if n.name in seen:
            return
        if depth.get(n.name, -1) >= d:
            return
        depth[n.name] = d
        for s in n.inputs:
            for lk in s.links:
                visit(lk.from_node, d + 1, seen | {n.name})

    for out in outputs:
        visit(out, 0, frozenset())
    for n in tree.nodes:
        depth.setdefault(n.name, 0)
    columns = {}
    for n in sorted(tree.nodes, key=lambda item: item.name):
        if n.type == "FRAME":
            continue
        columns.setdefault(depth[n.name], []).append(n)
    for d, items in columns.items():
        top = (len(items) - 1) * dy / 2
        for i, n in enumerate(items):
            n.location = (-d * dx, top - i * dy)


def ramp(color_ramp_node, stops, interpolation: str | None = None):
    """stops: [(position, (r, g, b[, a])), ...]."""
    elements = color_ramp_node.color_ramp.elements
    while len(elements) < len(stops):
        elements.new(0.5)
    while len(elements) > len(stops):
        elements.remove(elements[-1])
    for element, (position, color) in zip(elements, stops):
        element.position = position
        element.color = tuple(color) if len(color) == 4 else (*color, 1.0)
    if interpolation:
        color_ramp_node.color_ramp.interpolation = interpolation
    return color_ramp_node


# ----------------------------------------------------------------------------- keyframes
_ID_KINDS = (("OB", "objects"), ("MA", "materials"), ("WO", "worlds"), ("LI", "lights"), ("CA", "cameras"),
             ("ME", "meshes"), ("NT", "node_groups"), ("SC", "scenes"))


def _datablock(name: str):
    if ":" in name[:3]:
        prefix, rest = name.split(":", 1)
        for code, attr in _ID_KINDS:
            if code == prefix:
                found = getattr(bpy.data, attr).get(rest)
                if found is None:
                    raise KeyError(f"no {attr[:-1]} named {rest!r}")
                return found
    for _code, attr in _ID_KINDS:
        found = getattr(bpy.data, attr).get(name)
        if found is not None:
            return found
    return None


def _owner(idb):
    """Where keyframes for a datablock live: materials, worlds and lights key their node tree."""
    if isinstance(idb, (bpy.types.Material, bpy.types.World, bpy.types.Light)):
        tree = getattr(idb, "node_tree", None)
        return tree if tree is not None else idb
    return idb


def _split_index(path: str):
    """'location[2]' -> ('location', 2). A trailing index after a name is an array element."""
    match = re.match(r"^(.*?[A-Za-z_ ])\[(\d+)\]$", path)
    if match:
        return match.group(1), int(match.group(2))
    return path, None


def resolve(target, path: str):
    """(owner, data_path, index) for keyframing.

    target is a datablock, a typed id ("MA:SG Chevron Light 3") or a name. path is an RNA data path
    ('location', 'nodes["Wave Texture"].inputs[6].default_value'), or "Node/Socket" on a node tree,
    or just a socket name when only one node in the tree has it. With target=None, path can start with
    the datablock name: "SG Chevron Light 3/Principled BSDF/Emission Strength".
    """
    index = None
    if target is None or isinstance(target, str):
        name = target
        if name is None:
            if "/" not in path:
                raise ValueError("pass a target, or a path that starts with the datablock name")
            name, path = path.split("/", 1)
        found = _datablock(name)
        if found is None:
            raise KeyError(f"no object, material, world, light, camera or node group named {name!r}")
        target = found
    tree = _owner(target)
    is_tree = isinstance(tree, bpy.types.NodeTree)
    if "/" not in path:
        data_path, index = _split_index(path)
        for candidate in (target, tree, getattr(target, "data", None)):
            # "energy" on a light object means its light data; node paths on a material mean its tree.
            if candidate is not None and isinstance(candidate, bpy.types.ID) and _is_rna_path(candidate, data_path):
                return candidate, data_path, index
        if not is_tree:
            raise KeyError(f"{data_path!r} is not a property of {target.name}")
    if not is_tree:
        raise ValueError(f"{target.name} has no node tree for the path {path!r}")
    node_name, _, socket_name = path.rpartition("/")
    socket_name, index = _split_index(socket_name)
    if node_name:
        n = tree.nodes.get(node_name)
        if n is None:
            raise KeyError(f"no node {node_name!r} in {target.name}. Nodes: {', '.join(x.name for x in tree.nodes)}")
        s = sock(n, socket_name)
    else:
        hits = [(n, s) for n in tree.nodes for s in n.inputs
                if (s.identifier == socket_name or s.name == socket_name) and hasattr(s, "default_value")]
        if len(hits) != 1:
            where = ", ".join(n.name for n, _s in hits) or "no node"
            raise KeyError(f"{socket_name!r} matches {where} in {target.name}; write 'Node name/{socket_name}'")
        n, s = hits[0]
    return tree, socket_path(n, s), index


def _is_rna_path(idb, path: str) -> bool:
    try:
        idb.path_resolve(path)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def fcurves(idb) -> list:
    """The fcurves animating a datablock, through its action slot on Blender 4.4 and newer."""
    owner = _owner(idb)
    ad = getattr(owner, "animation_data", None)
    if ad is None or ad.action is None:
        return []
    slot = getattr(ad, "action_slot", None)
    if slot is not None:
        try:
            from bpy_extras import anim_utils
            bag = anim_utils.action_get_channelbag_for_slot(ad.action, slot)
            return list(bag.fcurves) if bag is not None else []
        except Exception:
            pass
    return list(snapshot.action_fcurves(ad.action))


def fcurve(idb, data_path: str, index: int | None = None):
    for fc in fcurves(idb):
        if fc.data_path == data_path and (index is None or fc.array_index == index):
            return fc
    return None


def _assign(owner, data_path: str, index, value) -> None:
    parent_path, _, attr = _rsplit_attr(data_path)
    parent = owner.path_resolve(parent_path) if parent_path else owner
    if index is not None:
        current = getattr(parent, attr)
        current[index] = value
    else:
        setattr(parent, attr, value)


def _rsplit_attr(path: str):
    depth = 0
    quote = False
    for i in range(len(path) - 1, -1, -1):
        ch = path[i]
        if ch == '"':
            quote = not quote
        elif not quote and ch == "]":
            depth += 1
        elif not quote and ch == "[":
            depth -= 1
        elif not quote and depth == 0 and ch == ".":
            return path[:i], ".", path[i + 1:]
    return "", "", path


def set_keys(target, path: str, keys, index: int | None = None, interpolation: str | None = None) -> list:
    """Insert keys [(frame, value), ...]. Handles layered actions and slots. Returns the fcurves keyed."""
    owner, data_path, path_index = resolve(target, path)
    index = path_index if index is None else index
    touched = []
    for frame, value in keys:
        _assign(owner, data_path, index, value)
        owner.keyframe_insert(data_path, index=-1 if index is None else index, frame=frame)
    for fc in fcurves(owner):
        if fc.data_path == data_path and (index is None or fc.array_index == index):
            if interpolation:
                frames = {float(f) for f, _v in keys}
                for key in fc.keyframe_points:
                    if float(key.co[0]) in frames:
                        key.interpolation = interpolation
            fc.update()
            touched.append(fc)
    return touched


def scale_keys(target, path: str, fn, index: int | None = None) -> int:
    """Apply fn(value) to every key of a channel, moving the handles with it. Returns the key count."""
    owner, data_path, path_index = resolve(target, path)
    index = path_index if index is None else index
    count = 0
    for fc in fcurves(owner):
        if fc.data_path != data_path or (index is not None and fc.array_index != index):
            continue
        for key in fc.keyframe_points:
            old = key.co[1]
            new = float(fn(old))
            key.co[1] = new
            key.handle_left[1] += new - old
            key.handle_right[1] += new - old
            count += 1
        fc.update()
    if not count:
        raise KeyError(f"{data_path} on {owner.name} has no keys")
    return count


# ----------------------------------------------------------------------------- space
def _depsgraph():
    return bpy.context.evaluated_depsgraph_get()


def bbox_world(objects, evaluated: bool = True):
    """(min, max) world bounds of one object or several, after modifiers when evaluated."""
    if isinstance(objects, bpy.types.Object):
        objects = [objects]
    deps = _depsgraph() if evaluated else None
    low = Vector((math.inf,) * 3)
    high = Vector((-math.inf,) * 3)
    for ob in objects:
        src = ob.evaluated_get(deps) if deps is not None else ob
        for corner in src.bound_box:
            p = src.matrix_world @ Vector(corner)
            low = Vector(map(min, low, p))
            high = Vector(map(max, high, p))
    return low, high


def raycast_down(x: float, y: float, z: float | None = None, objects=None, depsgraph=None):
    """First surface below (x, y). objects limits the hit to those objects. Returns (location, normal, object) or None."""
    deps = depsgraph or _depsgraph()
    scene = bpy.context.scene
    allowed = None if objects is None else {o.name for o in objects}
    if z is None:
        tops = [bbox_world(o)[1].z for o in (objects or scene.objects) if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}]
        z = (max(tops) if tops else 0.0) + 1.0
    origin = Vector((x, y, z))
    down = Vector((0.0, 0.0, -1.0))
    for _ in range(64):
        hit, location, normal, _index, ob, _matrix = scene.ray_cast(deps, origin, down)
        if not hit:
            return None
        original = getattr(ob, "original", ob)
        if allowed is None or original.name in allowed:
            return location, normal, original
        origin = location + down * 1e-4
    return None


@contextlib.contextmanager
def view3d_override():
    """Context for operators that need a 3D view. Not available in a background Blender."""
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            with bpy.context.temp_override(window=window, area=area, region=region,
                                           space_data=area.spaces.active, scene=window.scene):
                yield
            return
    raise RuntimeError("no 3D view is open (a background Blender has none); use bpy.data or bmesh instead")


# ----------------------------------------------------------------------------- derived objects
def mark_derived(obj, sources, script: str | None = None) -> str:
    """Record that obj is generated from sources by a script. The ingester warns when a source moves."""
    if script is None:
        caller = sys._getframe(1).f_globals.get("__file__", "")
        script = caller
    root = _root["path"]
    if script and root:
        try:
            relative = os.path.relpath(script, root)
            if not relative.startswith(".."):
                script = relative.replace(os.sep, "/")
        except ValueError:
            pass
    bpy.context.view_layer.update()
    names = [s.name if hasattr(s, "name") else str(s) for s in sources]
    obj["ai_derived_from"] = ", ".join(names)
    if script:
        obj["ai_regenerate_with"] = script
    obj["ai_derived_signature"] = snapshot.derived_signature([bpy.data.objects[n] for n in names])
    return obj["ai_derived_signature"]

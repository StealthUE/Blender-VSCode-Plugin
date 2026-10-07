"""Helpers for workspace scripts. Inside run_script: `import vsblender`.

    sock(node, "Fac")                    socket by identifier, then by displayed name
    build(tree, {...}, links=[...])      create or patch nodes by name, link them, lay them out
    set_keys(mat, "Principled BSDF/Emission Strength", [(1, 0), (24, 8)], replace=True)
    clear_keys(obj) / clear_keys(mat, "Principled BSDF/Emission Strength")
    scale_keys(obj, "rotation_euler", lambda v: v * 2, index=2)
    bbox_world(obj) / raycast_down(x, y)
    progress(0.4, "raycasting")          reported to the AI client; raises Cancelled when it cancels
    with view3d_override(): bpy.ops...   for operators that need a 3D view
    mark_derived(obj, sources)           record that obj is generated from sources by this script
    spec(obj, n=39, pitch_deg=9.23)      record the parameters an object was built from

Modelling (see vsblender.geo for solids and mesh builders):
    material("Brass", color="brass", metallic=1, roughness=0.3)
    modifier(obj, "BEVEL", width=0.002)  get-or-create by name, so re-runs do not stack modifiers
    place_on_ground(objs), center_on_origin(objs), set_origin(obj, "base"), orient_flat(obj), stats(obj)
    units() / mm(20) / m(1.5) / to_mm(x)  what a Blender unit is, and real sizes in Blender units
    printer()                            the 3D-printer profile (millimetres)
    read_stl / read_obj / read_3mf(path, volumes="model") / read_mesh_file, svg_loops(path)
    read_3mf drops a slicer's negative, modifier and support meshes (volumes="all" keeps them)
    part("dhd", keys_per_ring=18)        a reusable builder from parts/dhd.py, recorded on the objects
    intent("...")                        NOTES.md's Intent & constraints (kept across re-ingests)
    is_main()                            True in the script run_script runs, False when imported
    aim(camera, target)                  point an object's track axis at a target (up axis follows the view)
    dial(current, target, min_travel=0)  signed travel in degrees; add it to the running angle
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
    "mark_derived", "socket_path", "aim", "dial", "ref_section",
]


class Cancelled(BaseException):
    """Raised by progress() after the client cancels. A BaseException, so `except Exception` lets it through."""


_progress = {"fraction": None, "message": "", "updated": 0.0, "cancel": False, "active": False, "started": 0.0,
             "label": ""}
_root = {"path": ""}
# What the current run_script call knows: the printer profile (purpose "print"), the script, and
# warnings helpers raise along the way (reported with the run, not printed).
_run = {"printer": None, "script": "", "warnings": [], "reference_roots": []}

# Submodules scripts import as vsblender.<name>. Loaded on first use, so a plain `import vsblender`
# stays cheap.
_SUBMODULES = ("geo", "meshdata", "geom2d")


def _begin(label: str, root: str, context: dict | None = None) -> None:
    _progress.update(fraction=None, message="", updated=time.time(), cancel=False, active=True,
                     started=time.time(), label=label)
    _root["path"] = root
    context = context or {}
    _run.update(printer=context.get("printer"), script=context.get("script") or label, warnings=[],
                reference_roots=list(context.get("reference_roots") or []))


def _end() -> list:
    """Stop the run; returns the warnings helpers collected during it."""
    _progress.update(active=False, cancel=False)
    collected, _run["warnings"] = list(_run["warnings"]), []
    return collected[:40]


def warn(message: str) -> None:
    """A warning from a helper, shown in the run_script reply (not printed to stdout)."""
    text = str(message)
    if text not in _run["warnings"]:
        _run["warnings"].append(text)


def install_modules() -> None:
    """Make `import vsblender` and `from vsblender import geo` / `import vsblender.geo` work."""
    import importlib
    this = sys.modules[__name__]
    sys.modules["vsblender"] = this
    package = __name__.rsplit(".", 1)[0]
    for name in _SUBMODULES:
        try:
            module = importlib.import_module(f"{package}.{name}")
        except ImportError:
            continue
        sys.modules[f"vsblender.{name}"] = module


def uninstall_modules() -> None:
    if sys.modules.get("vsblender") is sys.modules[__name__]:
        del sys.modules["vsblender"]
    for name in _SUBMODULES:
        sys.modules.pop(f"vsblender.{name}", None)


def __getattr__(name: str):
    # PEP 562: vsblender.geo without an explicit import.
    if name in _SUBMODULES:
        import importlib
        return importlib.import_module(f"{__name__.rsplit('.', 1)[0]}.{name}")
    raise AttributeError(f"module 'vsblender' has no attribute {name!r}")


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
                    raise KeyError(f"no {snapshot.singular(attr)} named {rest!r}")
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
    """The fcurves animating a datablock, through its action slot on Blender 4.4 and newer.
    Materials, worlds and lights: their node tree's."""
    return _own_fcurves(_owner(idb))


def _own_fcurves(owner) -> list:
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


def _remove_fcurve(owner, fc) -> None:
    ad = owner.animation_data
    slot = getattr(ad, "action_slot", None)
    if slot is not None:
        try:
            from bpy_extras import anim_utils
            bag = anim_utils.action_get_channelbag_for_slot(ad.action, slot)
            if bag is not None:
                bag.fcurves.remove(fc)
                return
        except Exception:
            pass
    ad.action.fcurves.remove(fc)


def clear_keys(target, path: str | None = None, index: int | None = None) -> int:
    """Remove keyframes so a script can rebuild them from scratch: one channel (path, optionally one
    array index), or with path=None all of target's animation (its action is removed when nothing
    else uses it). Materials and worlds: their node tree's keys. Returns the number of keys removed."""
    if path is not None:
        owner, data_path, path_index = resolve(target, path)
        index = path_index if index is None else index
        removed = 0
        for fc in list(fcurves(owner)):
            if fc.data_path == data_path and (index is None or fc.array_index == index):
                removed += len(fc.keyframe_points)
                _remove_fcurve(owner, fc)
        return removed
    idb = _datablock(target) if isinstance(target, str) else target
    if idb is None:
        raise KeyError(f"no object, material, world, light, camera or node group named {target!r}")
    removed = 0
    actions = {}
    owners = [idb] if _owner(idb) is idb else [idb, _owner(idb)]
    for owner in owners:
        ad = getattr(owner, "animation_data", None)
        if ad is None:
            continue
        removed += sum(len(fc.keyframe_points) for fc in _own_fcurves(owner))
        if ad.action is not None:
            actions[ad.action.name] = ad.action
        owner.animation_data_clear()
    # By name, so an action shared by the object and its tree is removed once (see the gotcha).
    for action in actions.values():
        if action.users == 0:
            bpy.data.actions.remove(action)
    return removed


def set_keys(target, path: str, keys, index: int | None = None, interpolation: str | None = None,
             replace: bool = False) -> list:
    """Insert keys [(frame, value), ...]. Handles layered actions and slots. Returns the fcurves keyed.
    replace=True clears the channel first, so a re-run leaves exactly these keys."""
    owner, data_path, path_index = resolve(target, path)
    index = path_index if index is None else index
    if replace:
        for fc in list(fcurves(owner)):
            if fc.data_path == data_path and (index is None or fc.array_index == index):
                _remove_fcurve(owner, fc)
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


# ----------------------------------------------------------------------------- modelling
# The printer profile scripts see when the workspace configures none (purpose print, generic FDM).
DEFAULT_PRINTER = {
    "preset": "generic_220", "name": "Generic 220 mm FDM", "build_volume": [220, 220, 250], "nozzle": 0.4,
    "layer_height": 0.2, "min_wall": 0.8, "max_overhang_deg": 45, "material": "PLA", "density": 1.24,
    "filament_diameter": 1.75, "hole_compensation": 0.15, "configured": False,
}


def printer() -> dict:
    """The 3D-printer profile (millimetres) from .blender-ai/config.json, or the generic default.
    configured is False when the workspace set none."""
    return dict(_run.get("printer") or DEFAULT_PRINTER)


def reference_roots() -> list:
    """Folders outside the workspace that reference files may come from ("referenceRoots")."""
    return list(_run.get("reference_roots") or [])


def modifier(obj, kind: str, name: str | None = None, **props):
    """Get or create a modifier by name and set its properties, so a re-run updates it instead of
    adding another. modifier(ob, "BEVEL", width=0.002, segments=3); modifier(ob, "SUBSURF", levels=2)."""
    kind = kind.upper()
    aliases = {"SUBDIVISION": "SUBSURF", "SUBDIV": "SUBSURF", "WEIGHTED_NORMALS": "WEIGHTED_NORMAL", "MIRROR_": "MIRROR"}
    kind = aliases.get(kind, kind)
    name = name or kind.replace("_", " ").title()
    mod = obj.modifiers.get(name)
    if mod is not None and mod.type != kind:
        obj.modifiers.remove(mod)
        mod = None
    if mod is None:
        mod = obj.modifiers.new(name, kind)
    for key, value in props.items():
        if isinstance(value, str) and key in ("object", "mirror_object", "offset_object", "target"):
            value = bpy.data.objects.get(value)
        try:
            setattr(mod, key, value)
        except (AttributeError, TypeError, ValueError) as exc:
            known = sorted(p.identifier for p in mod.bl_rna.properties if not p.is_readonly)
            raise AttributeError(f"{kind} modifier: cannot set {key} ({exc}). Settable: {', '.join(known[:40])}") from exc
    return mod


def apply_modifiers(obj, keep: tuple = ()) -> None:
    """Bake the modifier stack into the mesh (evaluated geometry), then remove the modifiers, without
    operators. Material slots stay. keep: modifier names to leave in place (not applied)."""
    import bmesh
    from . import geo

    deps = bpy.context.evaluated_depsgraph_get()
    kept = [m for m in obj.modifiers if m.name in keep]
    for m in kept:
        m.show_viewport = False
    deps.update()
    me = bpy.data.meshes.new_from_object(obj.evaluated_get(deps), depsgraph=deps)
    try:
        bm = bmesh.new()
        bm.from_mesh(me)
        geo.replace_mesh(obj, bm, keep_slots=True)
        bm.free()
    finally:
        bpy.data.meshes.remove(me)
    for m in list(obj.modifiers):
        if m.name not in keep:
            obj.modifiers.remove(m)
    for m in kept:
        m.show_viewport = True


def part_folders() -> list:
    """Where parts live: parts/ next to the running script and in each folder above it, up to the
    workspace root (nearest first)."""
    root = _root["path"] or os.getcwd()
    script = _run.get("script") or ""
    here = os.path.dirname(os.path.join(root, script)) if script else root
    folders = []
    current = os.path.abspath(here)
    while True:
        for candidate in (os.path.join(current, "parts"), os.path.join(current, "scripts", "parts")):
            if os.path.isdir(candidate) and candidate not in folders:
                folders.append(candidate)
        if os.path.normcase(current) == os.path.normcase(os.path.abspath(root)):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return folders


def part(name: str, **params):
    """Build a reusable part: parts/<name>.py defines build(**params) returning a Solid (or a dict of
    name -> Solid) and optionally PARAMS (the defaults) and DESCRIPTION. Folders: parts/ next to the
    running script and above it, up to the workspace root. The result records the part and its
    parameters, so to_object stamps them on the object (ai_spec part, part_params) and NOTES lists them.

        dhd = vsblender.part("dhd", keys_per_ring=18, glyph_set="pegasus")
        dhd.to_object("PDHD Base", materials=["PDHD Stone"])
    """
    import importlib.util

    folders = part_folders()
    path = next((os.path.join(f, f"{name}.py") for f in folders if os.path.isfile(os.path.join(f, f"{name}.py"))), None)
    if path is None:
        known = sorted({os.path.splitext(n)[0] for f in folders for n in os.listdir(f) if n.endswith(".py") and not n.startswith("_")})
        raise KeyError(f"no part {name!r}: looked for {name}.py in {', '.join(folders) or 'parts/ (none found)'}"
                       + (f". Parts: {', '.join(known)}" if known else ""))
    spec_ = importlib.util.spec_from_file_location(f"vsblender_part_{name}", path)
    module = importlib.util.module_from_spec(spec_)
    folder = os.path.dirname(path)
    added = folder not in sys.path
    if added:  # a part may import its own helpers from parts/
        sys.path.insert(0, folder)
    try:
        spec_.loader.exec_module(module)
        build_fn = getattr(module, "build", None)
        if not callable(build_fn):
            raise AttributeError(f"{path} has no build(**params) function")
        merged = dict(getattr(module, "PARAMS", {}) or {})
        unknown = [k for k in params if merged and k not in merged]
        if unknown:
            warn(f"part {name}: {', '.join(unknown)} not in its PARAMS ({', '.join(merged)})")
        merged.update(params)
        result = build_fn(**merged)
    finally:
        if added:
            try:
                sys.path.remove(folder)
            except ValueError:
                pass
    rel = os.path.relpath(path, _root["path"]).replace(os.sep, "/") if _root["path"] else path
    meta = {"part": name, "params": {k: v for k, v in merged.items() if isinstance(v, (int, float, str, bool, list, tuple))},
            "file": rel}
    items = result.values() if isinstance(result, dict) else [result]
    for item in items:
        if hasattr(item, "meta"):
            item.meta.update(meta)
    return result


def intent(text: str, replace: bool = False) -> str:
    """Write what the next session must know into NOTES.md's Intent & constraints section (kept across
    re-ingests, always in context_pack): where sizes live, conventions, what to re-run, what is keyed."""
    from . import history

    return history.write_intent(_root["path"], text, replace=replace)


def is_main() -> bool:
    """True in the script run_script is running, False when another script imports it: keep build
    code in functions and the run code under `if vsblender.is_main():` (or `if __name__ == "__main__":`),
    and other projects can import the builders without running them."""
    return sys._getframe(1).f_globals.get("__name__") == "__main__"


# World axes tried as the "up" hint for to_track_quat, in the order a tie is broken.
_UP_AXES = (("Z", Vector((0.0, 0.0, 1.0))), ("Y", Vector((0.0, 1.0, 0.0))), ("X", Vector((1.0, 0.0, 0.0))))


def aim(ob, target, track: str = "-Z"):
    """Point `track` (an object's local axis) at target and return the euler it landed on.

    The up axis is the world axis least parallel to the view, so a camera that looks along
    world Y still gets a finite rotation. to_track_quat(track, "Y") is degenerate in that case.
    """
    direction = Vector(target) - Vector(ob.location)
    if direction.length < 1e-8:
        return ob.rotation_euler.copy()
    d = direction.normalized()
    up = min(_UP_AXES, key=lambda item: abs(float(d.dot(item[1]))))[0]
    ob.rotation_euler = direction.to_track_quat(track, up).to_euler()
    return ob.rotation_euler.copy()


def dial(current: float, target: float, clockwise: bool = True, min_travel: float = 0.0) -> float:
    """Signed change in degrees from current to target.

    Modulo is only used to pick the signed delta. Add the result to the running angle and store
    that: the keys keep the unwrapped angle, so the object does not snap back a turn. When the
    short delta is below min_travel, one full turn is added.
    """
    cur = float(current) % 360.0
    tgt = float(target) % 360.0
    if clockwise:
        delta = (tgt - cur) % 360.0
        if delta < float(min_travel):
            delta += 360.0
        return delta
    delta = (cur - tgt) % 360.0
    if delta < float(min_travel):
        delta += 360.0
    return -delta


def ref_section(name, plane=None, ring=None, **kwargs):
    """Section a registered reference (references.json) without putting it in the scene.

    ring=True uses a geo.Ring frame (up +Y, front +Z). A dict is passed through as the ring frame.
    """
    from . import measure as measure_mod

    params = {"op": "section", "ref": name}
    if plane is not None:
        params["plane"] = plane
    if ring is True:
        params["ring"] = {}
    elif isinstance(ring, dict):
        params["ring"] = ring
    params.update(kwargs)
    return measure_mod.measure(params, _root["path"])


def spec(obj, **params) -> dict:
    """Record the parameters an object was built from (intent as data), e.g.
    spec(ring, glyphs=39, pitch_deg=9.231, track_r=(2.542, 2.845)). Shown by describe and in NOTES.md.
    Merges with what is there; a value of None removes the key."""
    import json

    try:
        current = json.loads(obj.get("ai_spec") or "{}") if isinstance(obj.get("ai_spec"), str) else {}
    except ValueError:
        current = {}
    for key, value in params.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = list(value) if isinstance(value, tuple) else value
    text = json.dumps(current, default=str)
    if len(text) > 2000:
        raise ValueError(f"spec is {len(text)} characters; keep it under 2000 (the key parameters, not the geometry)")
    obj["ai_spec"] = text
    return current


# Re-exported so scripts reach everything through `import vsblender`.
from .units import units, mm, m, to_mm, label as units_label  # noqa: E402
from .looks import material, ref_image  # noqa: E402
from .placement import place_on_ground, place_on_bed, center_on_origin, center_on_bed, set_origin, orient_flat, stats  # noqa: E402
from .meshdata import read_stl, read_obj, read_3mf, read_mesh_file  # noqa: E402
from .geom2d import svg_loops  # noqa: E402

__all__ += [
    "warn", "printer", "modifier", "apply_modifiers", "spec", "units", "mm", "m", "to_mm", "units_label",
    "clear_keys", "intent", "is_main", "aim", "dial", "ref_section", "part", "part_folders", "reference_roots",
    "material", "ref_image", "place_on_ground", "place_on_bed", "center_on_origin", "center_on_bed", "set_origin",
    "orient_flat", "stats", "read_stl", "read_obj", "read_3mf", "read_mesh_file", "svg_loops", "geo",
]

"""Materials and reference images for modelling.

    vsblender.material("Brass", color="#c8a24a", metallic=1, roughness=0.3)
    vsblender.ref_image("refs/side.png", view="side", size=2.0)

material() keeps the node names "Principled BSDF" and "Material Output", so keyframes on the
material survive a rebuild, and also sets the viewport colour, so solid previews show the real
colour instead of grey.
"""
from __future__ import annotations

import math
import os

import bpy

NAMED = {
    "white": "#f2f2f2", "black": "#141414", "grey": "#808080", "gray": "#808080", "light grey": "#c0c0c0",
    "dark grey": "#404040", "red": "#c0282d", "orange": "#e8731c", "yellow": "#f2c230", "green": "#3a9a4a",
    "blue": "#2f62c4", "navy": "#1d2b53", "purple": "#7a4cc2", "pink": "#e889b4", "brown": "#6b4527",
    "wood": "#a0703c", "brass": "#c8a24a", "gold": "#d4af37", "copper": "#b87333", "steel": "#9aa1a8",
    "aluminium": "#c4c8cc", "aluminum": "#c4c8cc", "chrome": "#dcdfe3", "rubber": "#1e1e1e", "glass": "#e8f0f2",
    "pla grey": "#8a8d91", "pla white": "#eeeeec", "pla black": "#1b1b1c",
}
METALS = {"brass", "gold", "copper", "steel", "aluminium", "aluminum", "chrome"}


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def color_value(value, default=(0.8, 0.8, 0.8)) -> tuple:
    """A colour as linear RGB: '#rrggbb' or a name (sRGB, as a colour picker shows it), or [r, g, b] linear."""
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        text = NAMED.get(value.strip().lower(), value).strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            raise ValueError(f"colour {value!r}: use '#rrggbb', a name ({', '.join(sorted(NAMED)[:12])}...) or [r, g, b]")
        rgb = [int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
        return tuple(_srgb_to_linear(c) for c in rgb)
    vals = [float(v) for v in list(value)[:3]]
    if max(vals) > 1.0:
        vals = [_srgb_to_linear(v / 255.0) for v in vals]
    return tuple(vals)


def _set_input(node, names, value) -> bool:
    from . import helpers

    for name in names if isinstance(names, (list, tuple)) else [names]:
        try:
            sock = helpers.sock(node, name)
        except KeyError:
            continue
        try:
            if hasattr(sock, "default_value"):
                current = sock.default_value
                if hasattr(current, "__len__") and not isinstance(value, (list, tuple)):
                    value = [value] * len(current)
                elif hasattr(current, "__len__") and len(value) != len(current):
                    value = list(value) + [1.0] * (len(current) - len(value))
                sock.default_value = value
                return True
        except (TypeError, ValueError):
            continue
    return False


def ensure_material(name: str):
    """The material named name, created as a plain grey material when missing."""
    mat = bpy.data.materials.get(name)
    if mat is None:
        key = name.strip().lower()
        if key in NAMED:
            mat = material(name, color=key, metallic=1.0 if key in METALS else 0.0,
                           roughness=0.35 if key in METALS else 0.5)
        else:
            mat = material(name)
    return mat


def material(name: str, color=None, roughness: float | None = None, metallic: float | None = None, emission=None,
             strength: float | None = None, alpha: float | None = None, image: str | None = None,
             normal: str | None = None, ior: float | None = None, transmission: float | None = None,
             subsurface: float | None = None):
    """Create or update a Principled material. Only the arguments given are changed.

    color: '#rrggbb', a name (brass, steel, wood, red...) or linear [r, g, b]. emission: a colour;
    strength its strength. alpha below 1 turns on blending. image: a base-colour texture path
    (uses the mesh's UV map; geo.Solid.to_object makes box UVs). normal: a normal-map image path.
    """
    from . import helpers

    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    if getattr(mat, "node_tree", None) is None and hasattr(mat, "use_nodes"):
        mat.use_nodes = True
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None or bsdf.bl_idname != "ShaderNodeBsdfPrincipled":
        bsdf = helpers.node(tree, "ShaderNodeBsdfPrincipled", "Principled BSDF")
    out = tree.nodes.get("Material Output") or next((n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None)
    if out is None:
        out = helpers.node(tree, "ShaderNodeOutputMaterial", "Material Output")
    if not out.inputs["Surface"].is_linked:
        tree.links.new(bsdf.outputs[0], out.inputs["Surface"])
    if color is not None:
        rgb = color_value(color)
        _set_input(bsdf, ["Base Color"], (*rgb, 1.0))
        mat.diffuse_color = (*rgb, mat.diffuse_color[3])
    if roughness is not None:
        _set_input(bsdf, ["Roughness"], float(roughness))
        mat.roughness = float(roughness)
    if metallic is not None:
        _set_input(bsdf, ["Metallic"], float(metallic))
        mat.metallic = float(metallic)
    if ior is not None:
        _set_input(bsdf, ["IOR"], float(ior))
    if transmission is not None:
        _set_input(bsdf, ["Transmission Weight", "Transmission"], float(transmission))
    if subsurface is not None:
        _set_input(bsdf, ["Subsurface Weight", "Subsurface"], float(subsurface))
    if emission is not None:
        rgb = color_value(emission)
        _set_input(bsdf, ["Emission Color", "Emission"], (*rgb, 1.0))
        _set_input(bsdf, ["Emission Strength"], float(strength if strength is not None else 1.0))
    elif strength is not None:
        _set_input(bsdf, ["Emission Strength"], float(strength))
    if alpha is not None:
        _set_input(bsdf, ["Alpha"], float(alpha))
        d = mat.diffuse_color
        mat.diffuse_color = (d[0], d[1], d[2], float(alpha))
        for attr, value in (("surface_render_method", "BLENDED" if alpha < 1 else "DITHERED"),
                            ("blend_method", "BLEND" if alpha < 1 else "OPAQUE")):
            if hasattr(mat, attr):
                try:
                    setattr(mat, attr, value)
                except (TypeError, ValueError):
                    pass
    if image:
        tex = helpers.node(tree, "ShaderNodeTexImage", "Base Color Texture")
        tex.image = bpy.data.images.load(os.path.abspath(image), check_existing=True)
        tree.links.new(tex.outputs["Color"], helpers.sock(bsdf, "Base Color"))
    if normal:
        tex = helpers.node(tree, "ShaderNodeTexImage", "Normal Texture")
        tex.image = bpy.data.images.load(os.path.abspath(normal), check_existing=True)
        try:
            tex.image.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
        nmap = helpers.node(tree, "ShaderNodeNormalMap", "Normal Map")
        tree.links.new(tex.outputs["Color"], helpers.sock(nmap, "Color"))
        tree.links.new(nmap.outputs["Normal"], helpers.sock(bsdf, "Normal"))
    if image or normal:
        helpers.layout(tree)
    return mat


_VIEWS = {
    "front": (90.0, 0.0, 0.0),
    "back": (90.0, 0.0, 180.0),
    "side": (90.0, 0.0, 90.0),
    "right": (90.0, 0.0, 90.0),
    "left": (90.0, 0.0, -90.0),
    "top": (0.0, 0.0, 0.0),
    "bottom": (180.0, 0.0, 0.0),
}


def ref_image(path: str, view: str = "front", size: float | None = None, opacity: float = 0.5, offset=(0.0, 0.0, 0.0),
              name: str | None = None, collection: str = "References"):
    """A blueprint image in the viewport: an image empty facing the given view, hidden from renders.

    Shown only in the matching orthographic view (front, side, top...). Marked as a reference, so
    check_model and export_model leave it out. The AI compares against references with the
    reference tool or preview overlays, not by looking at this empty.
    """
    view = view.lower()
    if view not in _VIEWS:
        raise ValueError(f"view must be one of {', '.join(_VIEWS)}")
    img = bpy.data.images.load(os.path.abspath(path), check_existing=True)
    name = name or f"Ref {view} {os.path.splitext(os.path.basename(path))[0]}"
    ob = bpy.data.objects.get(name)
    if ob is None:
        ob = bpy.data.objects.new(name, None)
        coll = bpy.data.collections.get(collection)
        if coll is None:
            coll = bpy.data.collections.new(collection)
            bpy.context.scene.collection.children.link(coll)
        coll.objects.link(ob)
    ob.empty_display_type = "IMAGE"
    ob.data = img
    if size is not None:
        ob.empty_display_size = float(size)
    ob.rotation_euler = tuple(math.radians(a) for a in _VIEWS[view])
    ob.location = offset
    ob.use_empty_image_alpha = True
    ob.color = (1.0, 1.0, 1.0, float(opacity))
    try:
        ob.show_empty_image_only_axis_aligned = True
        ob.show_empty_image_perspective = False
    except AttributeError:
        pass
    ob.hide_render = True
    ob["vsblender_reference"] = "image"
    return ob

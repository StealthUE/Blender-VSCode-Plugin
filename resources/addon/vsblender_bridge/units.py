"""Scene units. Every length in and out of VSBlender is in Blender units (BU); this says what one is.

Most scenes are 1 BU = 1 m. Print projects (the print_mm template) are 1 BU = 1 mm: unit scale 0.001,
lengths shown in millimetres. Scripts work in BU; mm() and m() convert real sizes to BU.

    vsblender.units()        {"bu_to_mm": 1.0, "label": "1 BU = 1 mm", ...}
    vsblender.mm(20)         20 mm in BU (20.0 in a mm scene, 0.02 in a metre scene)
    vsblender.to_mm(0.02)    BU to mm
"""
from __future__ import annotations

import bpy

_SYMBOL = {"KILOMETERS": "km", "METERS": "m", "CENTIMETERS": "cm", "MILLIMETERS": "mm", "MICROMETERS": "µm",
           "MILES": "mi", "FEET": "ft", "INCHES": "in", "THOU": "thou"}
_TO_M = {"km": 1000.0, "m": 1.0, "cm": 0.01, "mm": 0.001, "µm": 1e-6, "um": 1e-6, "mi": 1609.344, "ft": 0.3048,
         "in": 0.0254, "thou": 2.54e-5}


def _scene(scene=None):
    if scene is not None:
        return scene
    try:
        if bpy.context.scene is not None:
            return bpy.context.scene
    except Exception:
        pass
    return bpy.data.scenes[0] if bpy.data.scenes else None


def units(scene=None) -> dict:
    """What one Blender unit is in this scene."""
    scene = _scene(scene)
    if scene is None:
        return {"system": "NONE", "length_unit": "", "scale_length": 1.0, "bu_to_m": 1.0, "bu_to_mm": 1000.0,
                "symbol": "m", "label": "1 BU = 1 m"}
    us = scene.unit_settings
    system = us.system
    scale = float(us.scale_length or 1.0) if system != "NONE" else 1.0
    # scale_length is stored as a 32-bit float (0.001 reads back as 0.0010000000475): snap to a
    # standard unit when it is within float precision of one.
    for exact_m in _TO_M.values():
        if abs(scale - exact_m) <= exact_m * 1e-6:
            scale = exact_m
            break
    length_unit = getattr(us, "length_unit", "") or ""
    bu_to_m = scale
    # The unit a BU reads as: the display unit when it matches the scale, else metres times scale.
    symbol = _SYMBOL.get(length_unit, "")
    if not symbol or length_unit == "ADAPTIVE" or abs(_TO_M.get(symbol, 1.0) - bu_to_m) > bu_to_m * 1e-6:
        symbol = next((s for s, v in _TO_M.items() if v == scale and s in _SYMBOL.values()), "") or symbol or "m"
    exact = abs(_TO_M.get(symbol, 1.0) - bu_to_m) <= bu_to_m * 1e-6
    if exact:
        label = f"1 BU = 1 {symbol}"
    else:
        label = f"1 BU = {_fmt(bu_to_m)} m (Blender displays {symbol})"
    return {
        "system": system,
        "length_unit": length_unit,
        "scale_length": scale,
        "bu_to_m": bu_to_m,
        "bu_to_mm": bu_to_m * 1000.0,
        "symbol": symbol if exact else "BU",
        "label": label,
    }


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def label(scene=None) -> str:
    return units(scene)["label"]


def mm(value: float, scene=None) -> float:
    """value millimetres in Blender units."""
    return float(value) / units(scene)["bu_to_mm"]


def m(value: float, scene=None) -> float:
    """value metres in Blender units."""
    return float(value) / units(scene)["bu_to_m"]


def to_mm(value: float, scene=None) -> float:
    """value Blender units in millimetres."""
    return float(value) * units(scene)["bu_to_mm"]


def file_to_bu(file_units: str = "mm", scene=None) -> float:
    """Factor from a file's units (mm for STL/3MF, usually) to this scene's Blender units."""
    key = str(file_units or "mm").strip().lower()
    aliases = {"millimeter": "mm", "millimeters": "mm", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
               "centimeter": "cm", "centimeters": "cm", "inch": "in", "inches": "in", "bu": "bu"}
    key = aliases.get(key, key)
    if key == "bu":
        return 1.0
    if key not in _TO_M:
        raise ValueError(f"unknown units {file_units!r}; use mm, cm, m, in, ft or bu")
    return _TO_M[key] / units(scene)["bu_to_m"]


def is_print_scene(scene=None) -> bool:
    """A scene made for printing: the print_mm template, or millimetre units."""
    scene = _scene(scene)
    if scene is None:
        return False
    try:
        if scene.get("vsblender_template") == "print_mm":
            return True
    except Exception:
        pass
    return abs(units(scene)["bu_to_mm"] - 1.0) < 1e-6


def template(scene=None) -> str:
    """The template a scene was made from with new_blend ('' if none)."""
    scene = _scene(scene)
    try:
        return str(scene.get("vsblender_template") or "") if scene is not None else ""
    except Exception:
        return ""

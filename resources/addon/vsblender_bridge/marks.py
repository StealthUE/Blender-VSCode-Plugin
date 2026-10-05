"""What an object is to VSBlender: model, staging, or measuring reference.

vsblender_stage       template staging: the render template's floor, the print template's build plate.
                      Left out of checks and exports, but it belongs in the file.
vsblender_reference   a measuring reference (an imported scan, a reference mesh): left out of checks and
                      exports, and the save tool refuses while one is in the scene, because a reference
                      makes the file and every checkpoint heavier. Objects named "_REF ..." count too.

Files made by 0.3.0 marked their staging objects vsblender_reference = "stage" / "build_plate"; those
still read as staging.
"""
from __future__ import annotations

import bpy

STAGE_VALUES = {"stage", "build_plate"}


def is_stage(ob) -> bool:
    try:
        return "vsblender_stage" in ob.keys() or str(ob.get("vsblender_reference", "")) in STAGE_VALUES
    except Exception:
        return False


def is_reference(ob) -> bool:
    """A measuring reference, not a staging object."""
    try:
        if ob.name.startswith("_REF"):
            return True
        return "vsblender_reference" in ob.keys() and not is_stage(ob)
    except Exception:
        return False


def left_out(ob) -> bool:
    """Not part of the model: staging or reference."""
    return is_stage(ob) or is_reference(ob)


def reference_objects(scene=None) -> list:
    objects = scene.objects if scene is not None else bpy.data.objects
    return sorted(ob.name for ob in objects if is_reference(ob) and not ob.name.startswith("_vsblender"))

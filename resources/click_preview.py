"""One still of the .blend Blender was started with.

    blender -b model.blend --python click_preview.py -- out.png

Background Blender does not start the bridge, so this does not take the port
or open the file in a window another chat already has.
"""
from __future__ import annotations

import os
import sys

import bpy
from mathutils import Vector


def _bounds():
    low = Vector((1e18, 1e18, 1e18))
    high = Vector((-1e18, -1e18, -1e18))
    found = False
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        found = True
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            low.x, low.y, low.z = min(low.x, world.x), min(low.y, world.y), min(low.z, world.z)
            high.x, high.y, high.z = max(high.x, world.x), max(high.y, world.y), max(high.z, world.z)
    if not found:
        return Vector((0, 0, 0)), 1.0
    return (low + high) * 0.5, max((high - low).length, 0.01)


def _ensure_camera(scene):
    if scene.camera is not None:
        return
    center, size = _bounds()
    data = bpy.data.cameras.new("Click Preview")
    data.lens = 50
    cam = bpy.data.objects.new("Click Preview", data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.location = center + Vector((size, -size, size * 0.55))
    direction = center - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def main() -> None:
    argv = sys.argv
    if "--" not in argv or len(argv) <= argv.index("--") + 1:
        raise SystemExit("usage: blender -b file.blend --python click_preview.py -- out.png")
    out = os.path.abspath(argv[argv.index("--") + 1])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    scene = bpy.context.scene
    _ensure_camera(scene)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 960
    scene.render.resolution_y = 540
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = out
    bpy.ops.render.render(write_still=True)
    print("VSB_PREVIEW " + out, flush=True)


if __name__ == "__main__":
    main()

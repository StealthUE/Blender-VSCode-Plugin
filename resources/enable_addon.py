"""Copy the VSBlender bridge into this Blender's user scripts and enable it.

    blender -b --python enable_addon.py

VSBLENDER_ADDON_SRC is the vsblender_bridge directory (the folder that contains __init__.py).
"""
import os
import shutil
import sys
import traceback

import addon_utils
import bpy

SRC = os.environ.get("VSBLENDER_ADDON_SRC", "")


def copy_tree(src: str, dest: str) -> None:
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def set_port(module: str) -> None:
    """Save the workspace port in the add-on preferences, so a Blender the user opens
    themselves listens where the MCP config expects it."""
    raw = os.environ.get("VSBLENDER_PORT", "")
    if not raw.isdigit():
        return
    addon = bpy.context.preferences.addons.get(module)
    prefs = getattr(addon, "preferences", None) if addon is not None else None
    if prefs is not None and hasattr(prefs, "port"):
        prefs.port = int(raw)


def enable(module: str) -> None:
    addon_utils.modules(refresh=True)
    addon_utils.enable(module, default_set=True, persistent=True)
    try:
        _loaded, enabled = addon_utils.check(module)
    except Exception as exc:
        raise RuntimeError(f"could not enable {module}") from exc
    if not enabled and module not in sys.modules:
        raise RuntimeError(f"Blender did not enable {module}")
    set_port(module)


def save_prefs() -> None:
    try:
        bpy.ops.wm.save_userpref()
    except Exception as exc:
        print("VSBLENDER_PREF_SAVE", exc)


def install_legacy() -> str:
    scripts = bpy.utils.user_resource("SCRIPTS", path="addons", create=True)
    dest = os.path.join(scripts, "vsblender_bridge")
    copy_tree(SRC, dest)
    bpy.utils.refresh_script_paths()
    enable("vsblender_bridge")
    save_prefs()
    return dest


def install_extension() -> str:
    root = bpy.utils.user_resource("EXTENSIONS")
    if not root:
        raise RuntimeError("this Blender has no extensions directory")
    dest = os.path.join(root, "user_default", "vsblender_bridge")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    copy_tree(SRC, dest)
    enable("bl_ext.user_default.vsblender_bridge")
    save_prefs()
    return dest


def main() -> None:
    if not SRC or not os.path.isfile(os.path.join(SRC, "__init__.py")):
        raise SystemExit("VSBLENDER_INSTALL_FAIL missing VSBLENDER_ADDON_SRC")
    errors = []
    for installer in (install_legacy, install_extension):
        try:
            dest = installer()
        except Exception:
            errors.append(traceback.format_exc())
            continue
        print("VSBLENDER_INSTALL_OK", dest)
        return
    print("VSBLENDER_INSTALL_FAIL")
    print("\n".join(errors))
    raise SystemExit(1)


if __name__ == "__main__":
    main()

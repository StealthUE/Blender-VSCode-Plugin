"""Start the VSBlender bridge in this Blender process.

GUI, returning to Blender after the server is up:
    blender [file.blend] --python start_bridge.py

Headless, until the process is killed:
    blender -b [file.blend] --python start_bridge.py

VSBLENDER_ADDON_SRC points at the vsblender_bridge directory shipped with the extension.
The installed add-on is used when it is already enabled.
"""
import importlib.util
import os
import sys

import bpy

SRC = os.environ.get("VSBLENDER_ADDON_SRC", "")


def load():
    import addon_utils

    try:
        addon_utils.enable("vsblender_bridge", default_set=False, persistent=False)
    except Exception as exc:
        print("VSBLENDER_ENABLE_SKIP", exc)
    for name, module in list(sys.modules.items()):
        if module is not None and (name == "vsblender_bridge" or name.endswith(".vsblender_bridge")):
            return module
    if not SRC:
        raise SystemExit("VSBLENDER_START_FAIL add-on is not installed and VSBLENDER_ADDON_SRC is unset")
    init = os.path.join(SRC, "__init__.py")
    spec = importlib.util.spec_from_file_location("vsblender_bridge", init)
    if spec is None or spec.loader is None:
        raise SystemExit(f"VSBLENDER_START_FAIL cannot load {init}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["vsblender_bridge"] = module
    # The package directory has to be on sys.path so a later reload can find it.
    parent = os.path.dirname(SRC)
    if parent and parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    module = load()
    module.register()
    if bpy.app.background:
        module.serve_blocking()
    else:
        module.ensure_server()
        print("VSBLENDER_LISTENING", module.port())


if __name__ == "__main__":
    main()

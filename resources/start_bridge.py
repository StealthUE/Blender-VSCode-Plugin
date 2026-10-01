"""Start the VSBlender bridge in this Blender process.

GUI, returning to Blender after the server is up:
    blender [file.blend] --python start_bridge.py

Headless, until the process is killed:
    blender -b [file.blend] --python start_bridge.py

VSBLENDER_ADDON_SRC points at the vsblender_bridge directory shipped with the extension.
The installed add-on is used when it is already enabled and is the same version; an older
install is switched off for this session and the shipped copy runs instead.
"""
import importlib.util
import os
import re
import sys

import bpy

SRC = os.environ.get("VSBLENDER_ADDON_SRC", "")


def shipped_version() -> str:
    try:
        with open(os.path.join(SRC, "__init__.py"), encoding="utf-8") as handle:
            match = re.search(r'^ADDON_VERSION = "([^"]+)"', handle.read(), re.M)
        return match.group(1) if match else ""
    except OSError:
        return ""


def load():
    import addon_utils

    try:
        addon_utils.enable("vsblender_bridge", default_set=False, persistent=False)
    except Exception as exc:
        print("VSBLENDER_ENABLE_SKIP", exc)
    wanted = shipped_version() if SRC else ""
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "vsblender_bridge" or name.endswith(".vsblender_bridge")):
            continue
        installed = getattr(module, "ADDON_VERSION", "")
        if not wanted or installed == wanted:
            return module
        print(f"VSBLENDER_OLD_ADDON installed {installed}, extension ships {wanted}: using the shipped copy")
        try:
            addon_utils.disable(name, default_set=False)
        except Exception as exc:
            print("VSBLENDER_DISABLE_SKIP", exc)
            try:
                module.unregister()
            except Exception:
                pass
        break
    if not SRC:
        raise SystemExit("VSBLENDER_START_FAIL add-on is not installed and VSBLENDER_ADDON_SRC is unset")
    return load_from_source()


def load_from_source():
    """Import the add-on package straight from the extension folder (not installed in Blender)."""
    init = os.path.join(SRC, "__init__.py")
    # submodule_search_locations makes it a package, so its `from . import x` imports resolve.
    spec = importlib.util.spec_from_file_location("vsblender_bridge", init, submodule_search_locations=[SRC])
    if spec is None or spec.loader is None:
        raise SystemExit(f"VSBLENDER_START_FAIL cannot load {init}")
    for name in [n for n in sys.modules if n == "vsblender_bridge" or n.startswith("vsblender_bridge.")]:
        del sys.modules[name]
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

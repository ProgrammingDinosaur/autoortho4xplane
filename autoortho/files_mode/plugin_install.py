"""Install the AutoOrtho X-Plane plugin and tell it where the gate is.

The plugin (``autoortho/xplane_plugin``) holds X-Plane's loading screen at a
flight start until the start area is built, and removes stale AutoOrtho
junctions when X-Plane starts without AutoOrtho. It is inert otherwise.
"""

import hashlib
import logging
import os
import shutil
import sys

log = logging.getLogger(__name__)

PLUGIN_DIR_NAME = "AutoOrtho"
CONFIG_NAME = "autoortho_gate.txt"


def _platform_dir():
    if sys.platform == "win32":
        return "win_x64"
    if sys.platform == "darwin":
        return "mac_x64"
    return "lin_x64"


def bundled_binary():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(here, "xplane_plugin", "bin", _platform_dir(), "AutoOrtho.xpl")]
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "xplane_plugin", "bin", _platform_dir(), "AutoOrtho.xpl"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def plugin_root(xplane_path):
    return os.path.join(xplane_path, "Resources", "plugins", PLUGIN_DIR_NAME)


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def install(xplane_path, files_root, gate_port, custom_scenery=None):
    """Copy the plugin when it differs, and write the gate config.

    Returns ``(installed, message)``. Never fails the session: without the
    plugin, X-Plane is simply not held at a start.
    """
    if not xplane_path or not os.path.isdir(os.path.join(xplane_path, "Resources", "plugins")):
        return False, "X-Plane plugins folder not found"
    root = plugin_root(xplane_path)
    os.makedirs(root, exist_ok=True)
    write_config(root, files_root, gate_port, custom_scenery)
    src = bundled_binary()
    if src is None:
        return False, "plugin binary not bundled for this platform (build xplane_plugin)"
    dst_dir = os.path.join(root, _platform_dir())
    dst = os.path.join(dst_dir, "AutoOrtho.xpl")
    try:
        if os.path.isfile(dst) and _digest(dst) == _digest(src):
            return True, "up to date"
        os.makedirs(dst_dir, exist_ok=True)
        tmp = dst + ".ao-new"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        log.info("X-Plane plugin installed at %s", dst)
        return True, "installed"
    except OSError as exc:
        # X-Plane running keeps the .xpl loaded on Windows: try next start.
        log.warning("Could not update the X-Plane plugin (%s); the installed one stays", exc)
        return os.path.isfile(dst), f"not updated: {exc}"


def write_config(root, files_root, gate_port, custom_scenery=None):
    lines = [
        f"port={int(gate_port)}",
        f"files_root={os.path.abspath(files_root)}",
    ]
    if custom_scenery:
        lines.append(f"custom_scenery={os.path.abspath(custom_scenery)}")
    tmp = os.path.join(root, CONFIG_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(root, CONFIG_NAME))


def remove(xplane_path):
    root = plugin_root(xplane_path)
    if not os.path.isdir(root):
        return False
    shutil.rmtree(root)
    return True


def installed(xplane_path):
    dst = os.path.join(plugin_root(xplane_path), _platform_dir(), "AutoOrtho.xpl")
    return os.path.isfile(dst)

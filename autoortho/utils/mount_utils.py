import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def cleanup_mountpoint(mountpoint):
    placeholder_path = os.path.join(mountpoint, ".AO_PLACEHOLDER")
    if os.path.lexists(mountpoint):
        log.info(f"Cleaning up mountpoint: {mountpoint}")
        os.rmdir(mountpoint)
    if os.path.ismount(mountpoint):
        log.debug(f"Skipping cleanup: still mounted: {mountpoint}")
    else:
        for d in ('Earth nav data', 'terrain', 'textures'):
            os.makedirs(os.path.join(mountpoint, d), exist_ok=True)
        Path(placeholder_path).touch()


def _is_nuitka_compiled() -> bool:
    # Nuitka exposes __compiled__ on the module that was compiled.
    import sys
    m = sys.modules.get("__main__")
    return bool(getattr(m, "__compiled__", False))
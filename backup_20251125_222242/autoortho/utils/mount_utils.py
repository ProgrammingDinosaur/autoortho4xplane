import os
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


_IGNORE_FILES = {".DS_Store", ".metadata_never_index"}
_AO_PLACEHOLDER_ITEMS = {"Earth nav data", "terrain", "textures", ".AO_PLACEHOLDER"}

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


def is_only_ao_placeholder(mountpoint: str) -> bool:
    """True if the directory contains only our known placeholder structure."""
    try:
        entries = [e for e in os.listdir(mountpoint) if e not in _IGNORE_FILES]
    except FileNotFoundError:
        return True  # treat missing dir as 'empty'
    except Exception as e:
        log.debug(f"is_only_ao_placeholder listdir failed: {e}")
        return False
    return set(entries).issubset(_AO_PLACEHOLDER_ITEMS)


def clear_ao_placeholder(mountpoint: str) -> None:
    """Remove our placeholder structure (and only that)."""
    try:
        for name in _AO_PLACEHOLDER_ITEMS:
            p = os.path.join(mountpoint, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.lexists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        log.info(f"Cleared AO placeholder from: {mountpoint}")
    except Exception as e:
        log.warning(f"clear_ao_placeholder failed for {mountpoint}: {e}")
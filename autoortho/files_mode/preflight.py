"""Checks before files mode starts (plan section 5.2.4)."""

import logging
import os
import sys

from . import fsops

log = logging.getLogger(__name__)

GB = 1024 ** 3
HEADROOM_GB = 2


class PreflightError(Exception):
    """A reason files mode cannot start, in words for the user."""


def check(files_root, disk_gb, scenery_path=None):
    """Raise PreflightError when the files root is unusable."""
    files_root = os.path.abspath(files_root)
    drive = os.path.splitdrive(files_root)[0]
    if sys.platform == "win32" and drive and not os.path.exists(drive + os.sep):
        raise PreflightError(
            f"The files folder's drive {drive} is not available (unplugged?). "
            "Files mode never falls back to another disk."
        )
    parent = files_root
    while parent and not os.path.exists(parent):
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    if scenery_path and not os.path.isdir(scenery_path):
        raise PreflightError(f"Scenery folder not found: {scenery_path}")
    fs = fsops.fs_type(parent)
    if fs in fsops.REFUSED_FS:
        raise PreflightError(
            f"The files folder is on a {fs.upper()} disk. Files mode needs hard links and "
            "sparse files (NTFS, APFS, ext4, btrfs or xfs): on this disk every texture "
            "would take its full 11 to 45 MB."
        )
    os.makedirs(files_root, exist_ok=True)
    meta = os.path.join(files_root, "_ao")
    if not fsops.probe_hard_links(meta):
        raise PreflightError("The files folder's disk does not support hard links.")
    if not fsops.probe_sparse(meta):
        raise PreflightError("The files folder's disk does not support sparse files.")
    free = fsops.free_bytes(files_root)
    needed = (float(disk_gb) + HEADROOM_GB) * GB
    # Textures already built count against the budget but not as free space,
    # so a shortfall only warns; the evictor keeps the pack within budget.
    if free < needed:
        log.warning("Free space %.1f GB is below the %.0f GB budget + %d GB",
                    free / GB, float(disk_gb), HEADROOM_GB)
        if free < HEADROOM_GB * GB:
            raise PreflightError(
                f"Only {free / GB:.1f} GB free on the files folder's disk; "
                f"files mode needs at least {HEADROOM_GB} GB beyond its budget."
            )
    return fs

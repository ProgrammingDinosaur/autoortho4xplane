"""
cache_cleanup.py - Remove AutoOrtho artifacts from the cache directory.

Only files and folders AutoOrtho itself writes are matched, so a cache
directory pointed at a shared location never loses unrelated user files.
"""

import os
import re
import shutil
from typing import Callable, Optional, Tuple

# Folders AutoOrtho creates directly under the cache directory.
CACHE_SUBDIRS = ("dds_cache", "terrain_index")

# Interrupted atomic writes: ".{name}.{uuid}.tmp" (chunk downloads) and
# "{name}.tmp.{pid}" (DDS cache writes).
_TEMP_RE = re.compile(r"(^\..+\.tmp$)|(\.tmp\.\d+$)", re.IGNORECASE)

# Seasons index cache, e.g. "+24+049.si".
_SEASON_INDEX_RE = re.compile(r"^[+-]\d{2}[+-]\d{3}\.si$", re.IGNORECASE)

# Seasons DSF processing scratch files.
_SEASON_SCRATCH_RE = re.compile(
    r"^((ao|global)_.+\.dsf\.txt|temp_mesh_(7z_)?.+\.dsf)$", re.IGNORECASE
)

_IMAGE_EXTS = (".jpg", ".jpeg", ".dds")


def is_temp_file(name: str) -> bool:
    return bool(_TEMP_RE.search(name))


def is_cache_file(name: str) -> bool:
    """Return True if a top-level file name is an AutoOrtho cache artifact."""
    lower = name.lower()
    return (
        lower.endswith(_IMAGE_EXTS)
        or is_temp_file(name)
        or bool(_SEASON_INDEX_RE.match(name))
        or bool(_SEASON_SCRATCH_RE.match(name))
    )


def remove_stale_temp_files(cache_dir: str, cancel_event=None) -> int:
    """Delete leftover temp files from interrupted writes. Returns count."""
    removed = 0
    if not os.path.isdir(cache_dir):
        return 0
    for entry in os.scandir(cache_dir):
        if cancel_event is not None and cancel_event.is_set():
            break
        if entry.is_file() and is_temp_file(entry.name):
            try:
                os.remove(entry.path)
                removed += 1
            except OSError:
                pass
    return removed


def delete_all_cache_contents(
    cache_dir: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[int, int]:
    """Delete every AutoOrtho artifact in ``cache_dir``.

    Returns ``(files_removed, failures)``. Files that cannot be removed
    (for example because another process holds them open) are counted as
    failures instead of raising.
    """
    emit = progress_callback or (lambda _msg: None)
    removed = 0
    failures = 0
    if not os.path.isdir(cache_dir):
        return 0, 0

    for entry in os.scandir(cache_dir):
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in CACHE_SUBDIRS:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    if os.path.exists(entry.path):
                        failures += 1
                    else:
                        removed += 1
            elif entry.is_file(follow_symlinks=False) and is_cache_file(entry.name):
                os.remove(entry.path)
                removed += 1
        except OSError:
            failures += 1

    emit(f"Deleted {removed} cache entries.")
    return removed, failures

"""The low-zoom base: chunks at a coarse zoom kept on disk for good.

One ZL12 chunk covers a whole ZL16 tile, so once it is on disk the tile's
base mipmap, and every placeholder stretched from it, is real imagery and
never needs the network again. These chunks are small (10-25 KB) and few,
so they are exempt from JPEG eviction. With the packed chunk store they are
rows marked kept (``chunk_store.FLAG_KEEP``), which eviction skips; with one
file per chunk they keep the normal naming (``{col}_{row}_{zoom}_{maptype}.jpg``)
and the disk budget skips them by name (:func:`protector`).
"""

import re

_CHUNK_NAME = re.compile(r"^-?\d+_-?\d+_(\d+)_[^.]+\.jpe?g$", re.IGNORECASE)


def chunk_zoom(filename):
    """The zoom of a chunk JPEG's file name, or None if it is not a chunk."""
    match = _CHUNK_NAME.match(filename)
    return int(match.group(1)) if match else None


def is_base_chunk(filename, max_zoom):
    """Whether a cached chunk file belongs to the low-zoom base."""
    zoom = chunk_zoom(filename)
    return zoom is not None and zoom <= max_zoom


def protector(enabled, max_zoom):
    """A predicate for the disk budget manager, or None when the base is off."""
    if not enabled:
        return None
    max_zoom = int(max_zoom)
    return lambda filename: is_base_chunk(filename, max_zoom)

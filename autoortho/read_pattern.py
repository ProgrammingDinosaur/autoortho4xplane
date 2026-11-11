"""Classify X-Plane's DDS reads by which mipmap they actually want.

X-Plane reads a texture in 64 KB chunks aligned to 64 KB:

* First the *header chunk*: the 128-byte DDS header plus the start of
  mipmap 0. It reads this for every texture it loads, near or far. It only
  reads on into mipmap 0 when the texture is next to the camera.
* Then, starting from the chunk that holds the start of the finest level it
  wants, it reads to the end of the file. Because chunks are 64 KB aligned,
  the read for level *k* usually starts with the last bytes of level *k-1*.

Only the level X-Plane wants should trigger downloads or waits. The header
chunk's piece of mipmap 0 and the tail of the finer level in front of the
wanted one are *incidental*: they are answered with whatever is at hand.

The functions here are pure (the cursor holds the only state) so the tile,
the tests and any diagnostics apply the same rules.
"""

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

DDS_HEADER_SIZE = 128
# X-Plane's read size. FUSE layers may split one of its reads into smaller
# pieces (macFUSE uses 32 KB), so pieces that continue the header chunk up to
# this offset still belong to it.
XPLANE_CHUNK = 65536


@dataclass
class ReadCursor:
    """What one open of a texture has read so far."""

    last_end: int = -1
    header_chunk_end: Optional[int] = None

    def reset(self):
        self.last_end = -1
        self.header_chunk_end = None


def mip_at(mipmaps: Sequence, position: int) -> int:
    """Index of the mipmap holding byte ``position`` (the last one past the end)."""
    for mipmap in mipmaps:
        if position < mipmap.endpos:
            return mipmap.idx
    return mipmaps[-1].idx


def is_header_chunk(mipmaps: Sequence, offset: int, end: int) -> bool:
    """Whether a read is X-Plane's header chunk.

    It starts inside the DDS header, ends inside mipmap 0, and covers less
    than half of mipmap 0. Reading all of mipmap 0 from the start is a real
    request for it, not a header chunk.
    """
    if not mipmaps:
        return False
    mm0 = mipmaps[0]
    return (
        offset < mm0.startpos
        and end > mm0.startpos
        and mip_at(mipmaps, end - 1) == 0
        and (end - mm0.startpos) * 2 < mm0.length
    )


def classify(
    mipmaps: Sequence,
    cursor: ReadCursor,
    offset: int,
    length: int,
) -> Tuple[bool, Optional[int], int]:
    """Classify one read and note it on ``cursor``.

    Returns ``(header_chunk, wanted, first_needed)``:

    * ``header_chunk``: the read is X-Plane's header chunk.
    * ``wanted``: the level this read shows X-Plane wants (None when the read
      only continues an earlier one or is incidental).
    * ``first_needed``: levels below this index are incidental for this read.
      They must not trigger downloads or waits, and must not be sealed as
      the served version of that level.

    This is deliberately more conservative than OrthoStreamXP's rules. FUSE
    can split, merge or read ahead, so a read that starts inside a level
    without crossing into the next one is still treated as needed.
    """
    end = offset + length
    if not mipmaps or length <= 0:
        return False, None, 0
    mm0 = mipmaps[0]
    if end <= mm0.startpos:
        cursor.last_end = end
        return False, None, len(mipmaps)

    header = is_header_chunk(mipmaps, offset, end) or (
        # The next FUSE piece of the same header chunk.
        cursor.header_chunk_end is not None
        and offset == cursor.header_chunk_end
        and end <= XPLANE_CHUNK
        and mip_at(mipmaps, end - 1) == 0
        and (end - mm0.startpos) * 2 < mm0.length
    )
    data_start = max(offset, mm0.startpos)
    first = mip_at(mipmaps, data_start)
    last = mip_at(mipmaps, end - 1)

    if header:
        wanted, first_needed = None, last + 1
        cursor.header_chunk_end = end
    elif data_start == mipmaps[first].startpos:
        wanted, first_needed = first, first
    elif first == 0 and data_start == (cursor.header_chunk_end or -1):
        # X-Plane reads on into mipmap 0 exactly where the header chunk
        # ended: the texture is next to the camera and it wants level 0.
        wanted, first_needed = 0, 0
    elif data_start == cursor.last_end:
        # The next piece of a read already in progress.
        wanted, first_needed = None, first
    elif last > first:
        # Starts in the tail of a finer level and runs into the next one:
        # the coarser level is the one X-Plane wants.
        wanted, first_needed = first + 1, first + 1
    else:
        wanted, first_needed = None, first

    cursor.last_end = end
    return header, wanted, first_needed

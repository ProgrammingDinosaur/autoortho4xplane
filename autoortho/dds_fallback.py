"""Placeholder DDS bytes for a texture read that cannot be served.

When a tile read fails or times out, the FUSE layer still answers X-Plane
(an I/O error crashes it on Windows). The answer must be a valid DDS:
X-Plane rejects a texture whose header is not one, draws that terrain white
and does not try the file again until the scenery reloads. Served while a
DSF loads, an invalid header turns the whole 1x1 degree tile white.

So the placeholder carries a real header for the size X-Plane was told the
file has, followed by solid blocks in ``missing_color``. X-Plane then shows
that colour, and real imagery replaces it on the next reload.
"""

import struct
from functools import lru_cache
from typing import Optional

try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

DDS_HEADER_SIZE = 128
DEFAULT_MISSING_COLOR = (66, 77, 55)

# Header flags as pydds.DDS writes them, so a placeholder header is byte for
# byte the header a real texture of the same size gets.
_DDSD_FLAGS = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000
_DDPF_FOURCC = 0x4
_CAPS = 0x1000 | 0x400000          # TEXTURE | MIPMAP
_SIDES = tuple(1 << n for n in range(2, 17))   # 4 .. 65536 pixels


def is_bc3(fmt) -> bool:
    return str(fmt).upper() in ("BC3", "DXT5")


def block_bytes(fmt) -> int:
    """Bytes per compressed block: 16 for BC3/DXT5, 8 for BC1/DXT1."""
    return 16 if is_bc3(fmt) else 8


def dds_size(width: int, height: int, fmt) -> int:
    """File size of a DDS with every mipmap down to 1x1, as pydds lays it out."""
    block = block_bytes(fmt)
    size = DDS_HEADER_SIZE
    w, h = int(width), int(height)
    while w >= 1 and h >= 1:
        size += max(1, (w * h) >> 4) * block
        w >>= 1
        h >>= 1
    return size


@lru_cache(maxsize=64)
def dds_header(width: int, height: int, fmt) -> bytes:
    """The 128-byte header pydds writes for a ``width`` x ``height`` texture."""
    block = block_bytes(fmt)
    mipmaps = 0
    w, h = int(width), int(height)
    while w >= 1 and h >= 1:
        mipmaps += 1
        w >>= 1
        h >>= 1
    header = struct.pack(
        "<4s7I44s2I4s5I5I",
        b"DDS ", 124, _DDSD_FLAGS, int(height), int(width),
        max(1, (int(width) * int(height)) >> 4) * block, 0, mipmaps,
        b"\0" * 44,
        32, _DDPF_FOURCC, b"DXT5" if block == 16 else b"DXT1", 0, 0, 0, 0, 0,
        _CAPS, 0, 0, 0,
        0,
    )
    assert len(header) == DDS_HEADER_SIZE
    return header


def side_for_size(size: int, fmt) -> Optional[int]:
    """Side of the square texture whose DDS is ``size`` bytes, or None."""
    for side in _SIDES:
        if dds_size(side, side, fmt) == size:
            return side
    return None


def header_for_size(size: Optional[int], fmt, default_side: int = 4096) -> bytes:
    """A header matching a file of ``size`` bytes.

    AutoOrtho textures are square, so the size names the side. A size that
    fits no square texture (never expected) gets the largest one that fits
    in it, so the header never promises more bytes than the file has.
    """
    side = side_for_size(size, fmt) if size else None
    if side is None:
        side = default_side
        if size:
            fitting = [s for s in _SIDES if dds_size(s, s, fmt) <= size]
            if fitting:
                side = fitting[-1]
    return dds_header(side, side, fmt)


def _rgb565(r: int, g: int, b: int) -> int:
    return ((r >> 3) & 0x1F) << 11 | ((g >> 2) & 0x3F) << 5 | ((b >> 3) & 0x1F)


def configured_missing_color():
    try:
        missing = CFG.autoortho.missing_color
        if isinstance(missing, (list, tuple)) and len(missing) >= 3:
            return tuple(max(0, min(255, int(v))) for v in missing[:3])
    except Exception:
        pass
    return DEFAULT_MISSING_COLOR


@lru_cache(maxsize=16)
def missing_block(fmt, color=DEFAULT_MISSING_COLOR) -> bytes:
    """One solid, opaque block of ``color``."""
    c = _rgb565(*color).to_bytes(2, "little")
    colour = c + c + b"\0\0\0\0"
    if is_bc3(fmt):
        return b"\xff\xff\0\0\0\0\0\0" + colour   # alpha 255 everywhere
    return colour


def fallback_bytes(offset: int, length: int, header: bytes, fmt,
                   color=None) -> bytes:
    """Bytes ``offset`` .. ``offset + length`` of a placeholder DDS.

    ``header`` is the 128-byte header to serve; the data after it is solid
    ``color`` blocks, aligned to the block grid whatever the read offset.
    """
    offset = max(0, int(offset))
    length = max(0, int(length))
    if length == 0:
        return b""
    out = bytearray()
    if offset < DDS_HEADER_SIZE:
        take = min(length, DDS_HEADER_SIZE - offset)
        out += header[offset:offset + take]
        offset += take
        length -= take
    if length > 0:
        block = missing_block(fmt, tuple(color or configured_missing_color()))
        size = len(block)
        phase = (offset - DDS_HEADER_SIZE) % size
        count = (phase + length + size - 1) // size
        out += (block * count)[phase:phase + length]
    return bytes(out)

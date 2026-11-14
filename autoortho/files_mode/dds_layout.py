"""DDS header and mip offsets for a texture size, identical to ``pydds.DDS``.

Files mode writes full-size DDS files with exactly the header FUSE would
serve for the same tile, so the two backends are interchangeable.
"""

import struct
from dataclasses import dataclass
from functools import lru_cache

HEADER_SIZE = 128

DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PIXELFORMAT = 0x1000
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000


def block_size(dxt_format):
    return 16 if str(dxt_format).upper() == "BC3" else 8


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    dxt_format: str
    blocksize: int
    header: bytes
    offsets: tuple  # ((start, length), ...) per mip
    total_size: int

    @property
    def mip_count(self):
        return len(self.offsets)

    def level_range(self, first, last=None):
        """``(start, end)`` bytes spanning mips ``first..last`` (inclusive)."""
        last = self.mip_count - 1 if last is None else last
        return self.offsets[first][0], self.offsets[last][0] + self.offsets[last][1]


@lru_cache(maxsize=32)
def layout(width, height=None, dxt_format="BC1"):
    height = width if height is None else height
    bs = block_size(dxt_format)
    offsets = []
    cur = HEADER_SIZE
    w, h = int(width), int(height)
    while w >= 1 and h >= 1:
        length = max(1, (w * h) >> 4) * bs
        offsets.append((cur, length))
        cur += length
        w >>= 1
        h >>= 1
    header = _header(int(width), int(height), len(offsets), bs)
    return Layout(int(width), int(height), "BC3" if bs == 16 else "BC1", bs,
                  header, tuple(offsets), cur)


def _header(width, height, mip_count, bs):
    flags = (DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT
             | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE)
    linear = max(1, (width * height) >> 4) * bs
    four_cc = b"DXT5" if bs == 16 else b"DXT1"
    head = struct.pack(
        "<4sIIIIIII44sII4sIIIIIIIIII",
        b"DDS ", 124, flags, height, width, linear, 0, mip_count, b"\0" * 44,
        32, 0x4, four_cc, 0, 0, 0, 0, 0, 0x1000 | 0x400000, 0, 0, 0, 0,
    )
    assert len(head) == HEADER_SIZE
    return head


def neutral_block(dxt_format="BC1", rgb=(66, 77, 55)):
    """One block of the missing colour (``pydds._get_missing_color_bc1_block``)."""
    r, g, b = (int(c) for c in rgb[:3])
    rgb565 = ((r >> 3) & 0x1F) << 11 | ((g >> 2) & 0x3F) << 5 | ((b >> 3) & 0x1F)
    colour = rgb565.to_bytes(2, "little")
    block = colour + colour + b"\0\0\0\0"
    if block_size(dxt_format) == 16:
        return b"\xFF\xFF\0\0\0\0\0\0" + block
    return block


def is_valid_header(data, lay):
    return len(data) >= HEADER_SIZE and data[:4] == b"DDS " and data[:HEADER_SIZE] == lay.header

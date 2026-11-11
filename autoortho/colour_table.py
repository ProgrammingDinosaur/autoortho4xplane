"""Recolour DXT colour blocks as X-Plane reads them.

A DXT colour block holds two RGB565 colours and 2-bit indices that pick
between them (and two colours in between). Changing the two colours changes
every pixel of the block, so a colour adjustment can be made on the bytes as
they are served, through a lookup table over all 65,536 RGB565 colours,
instead of being baked into built and cached textures.

That keeps the cache as downloaded: changing season settings never
invalidates it, and every texture gets the same adjustment however it was
built (the native pipeline never applied seasons).

Ported from OrthoStreamXP's ``ColourTable.cs``.
"""

from functools import lru_cache

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard dependency
    np = None

DDS_HEADER_SIZE = 128


def _decode565(c):
    r = ((c >> 11) & 31) * 255.0 / 31
    g = ((c >> 5) & 63) * 255.0 / 63
    b = (c & 31) * 255.0 / 31
    return r, g, b


def _encode565(r, g, b):
    r = np.clip(np.rint(r * 31 / 255), 0, 31).astype(np.uint16)
    g = np.clip(np.rint(g * 63 / 255), 0, 63).astype(np.uint16)
    b = np.clip(np.rint(b * 31 / 255), 0, 31).astype(np.uint16)
    return (r << 11) | (g << 5) | b


@lru_cache(maxsize=32)
def saturation_table(saturation):
    """RGB565 -> RGB565 table that scales saturation like AoImage.desaturate."""
    s = max(0.0, min(1.0, float(saturation)))
    c = np.arange(65536, dtype=np.uint32)
    r, g, b = _decode565(c)
    luma = 0.212671 * r + 0.715160 * g + 0.072169 * b
    x = (1.0 - s) * luma
    table = _encode565(s * r + x, s * g + x, s * b + x)
    table.setflags(write=False)
    return table


def block_bytes(dxt_format):
    """Bytes per block for a pydds format name: 8 for BC1/DXT1, 16 for BC3/DXT5."""
    name = str(dxt_format).upper()
    if name in ("BC1", "DXT1"):
        return 8
    if name in ("BC3", "DXT5", "BC2", "DXT3"):
        return 16
    return 0


def apply(data, file_offset, block_size, table):
    """Recolour the whole colour blocks in ``data`` (bytes of a DDS from ``file_offset``).

    Returns new bytes. Blocks cut by either end of the buffer are left as
    they are, so the two halves of a block always agree, whichever reads
    they come in. BC1 blocks keep their opaque four-colour mode (first colour
    the larger): where the table turns the order around, the two colours are
    swapped together with their pixel indices. Three-colour BC1 blocks are
    left as made.
    """
    if not data or block_size not in (8, 16) or table is None:
        return data
    start = max(file_offset, DDS_HEADER_SIZE) - DDS_HEADER_SIZE
    first = DDS_HEADER_SIZE + -(-start // block_size) * block_size
    end = file_offset + len(data)
    count = (end - first) // block_size
    if count <= 0:
        return data
    out = bytearray(data)
    base = first - file_offset
    blocks = np.frombuffer(
        out, dtype=np.uint8, count=count * block_size, offset=base
    ).reshape(count, block_size)
    colour = blocks[:, 8:16] if block_size == 16 else blocks
    c0 = colour[:, 0].astype(np.uint16) | (colour[:, 1].astype(np.uint16) << 8)
    c1 = colour[:, 2].astype(np.uint16) | (colour[:, 3].astype(np.uint16) << 8)
    n0 = table[c0]
    n1 = table[c1]
    if block_size == 8:
        opaque = c0 > c1
        swap = opaque & (n0 < n1)
        flat = opaque & (n0 == n1)
        n0, n1 = np.where(swap, n1, n0), np.where(swap, n0, n1)
        # Swapped colours: index 0<->1 and 2<->3, i.e. flip every index's low bit.
        colour[swap, 4:8] ^= 0x55
        # One colour: every index 0, the unused second colour one step below.
        colour[flat, 4:8] = 0
        n1 = np.where(flat & (n0 > 0), n0 - 1, n1)
        keep = ~opaque
        n0 = np.where(keep, c0, n0)
        n1 = np.where(keep, c1, n1)
    colour[:, 0] = n0 & 0xFF
    colour[:, 1] = n0 >> 8
    colour[:, 2] = n1 & 0xFF
    colour[:, 3] = n1 >> 8
    return bytes(out)

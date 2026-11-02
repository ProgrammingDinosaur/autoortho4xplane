"""BC1/BC3 block utilities that work on compressed data directly.

Nearest-neighbour upscaling of BC1/BC3 blocks without recompression.

Every output pixel samples one source pixel, and the 16 pixels of an output
block always fall inside a single source block. A block therefore upscales by
keeping the source endpoints and remapping its per-pixel indices, which is
exact for nearest-neighbour sampling and costs a few integer operations per
block. It is used to serve lower-mipmap imagery in place of missing color.
"""

import logging

log = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with every build
    np = None


def numpy_module():
    """numpy, or None when unavailable (callers then serve missing color)."""
    return np


def _remap_indices(src_bits, src_pixel, bits_per_pixel, shift=0):
    """Gather ``bits_per_pixel`` index fields for 16 output pixels.

    ``src_bits`` holds each source block's packed indices, ``src_pixel`` is a
    (16, N) array with the source pixel (0..15) every output pixel samples.
    """
    mask = np.uint64((1 << bits_per_pixel) - 1)
    out = np.zeros(src_bits.shape, dtype=np.uint64)
    for pixel in range(16):
        field = (
            src_bits >> (np.uint64(shift) + src_pixel[pixel] * np.uint64(bits_per_pixel))
        ) & mask
        out |= field << np.uint64(shift + pixel * bits_per_pixel)
    return out


def upscale_blocks(
    source,
    source_width_blocks,
    source_height_blocks,
    out_blocks,
    out_width_blocks,
    levels,
    blocksize,
    source_first_row=0,
):
    """Upscale source blocks into the output blocks with linear indices
    ``out_blocks`` (iterable of ints) of a mipmap ``levels`` steps larger.

    ``source`` holds whole block rows of the source mipmap starting at block
    row ``source_first_row``; ``source_height_blocks`` is the full source
    mipmap height. Returns the output blocks concatenated in ``out_blocks``
    order, plus the absolute linear index of the source block each one
    sampled (callers use it to skip unbuilt sources).
    """
    if np is None or levels < 1 or blocksize not in (8, 16):
        return None, None
    out_blocks = np.asarray(out_blocks, dtype=np.int64)
    if out_blocks.size == 0:
        return b"", out_blocks

    block_x = out_blocks % out_width_blocks
    block_y = out_blocks // out_width_blocks
    pixel_x = np.arange(4, dtype=np.int64)
    # Source pixel coordinates for every output pixel, shaped (4, N).
    src_px = ((block_x[None, :] * 4) + pixel_x[:, None]) >> levels
    src_py = ((block_y[None, :] * 4) + pixel_x[:, None]) >> levels
    src_block_x = np.minimum(src_px[0] >> 2, source_width_blocks - 1)
    src_block_y = np.minimum(src_py[0] >> 2, source_height_blocks - 1)
    src_index = src_block_y * source_width_blocks + src_block_x

    # Output pixel p = y * 4 + x samples source pixel (sy & 3) * 4 + (sx & 3).
    sub_x = src_px & 3
    sub_y = src_py & 3
    src_pixel = (
        (sub_y[:, None, :] * 4) + sub_x[None, :, :]
    ).reshape(16, -1).astype(np.uint64)

    words = np.frombuffer(source, dtype="<u8").reshape(-1, blocksize // 8)
    local_index = src_index - source_first_row * source_width_blocks
    if local_index.min() < 0 or local_index.max() >= len(words):
        return None, None
    picked = words[local_index]
    out = np.empty_like(picked)
    if blocksize == 8:
        colour = picked[:, 0]
        out[:, 0] = (colour & np.uint64(0xFFFFFFFF)) | _remap_indices(
            colour, src_pixel, 2, shift=32
        )
    else:
        alpha = picked[:, 0]
        out[:, 0] = (alpha & np.uint64(0xFFFF)) | _remap_indices(
            alpha, src_pixel, 3, shift=16
        )
        colour = picked[:, 1]
        out[:, 1] = (colour & np.uint64(0xFFFFFFFF)) | _remap_indices(
            colour, src_pixel, 2, shift=32
        )
    return out.astype("<u8").tobytes(), src_index


def _bc1_colour(block_word):
    """RGB of a BC1 block whose 16 indices are all the same."""
    c0 = int(block_word & 0xFFFF)
    c1 = int((block_word >> 16) & 0xFFFF)
    index = int((block_word >> 32) & 3)

    def rgb(c):
        return (
            ((c >> 11) & 0x1F) * 255 // 31,
            ((c >> 5) & 0x3F) * 255 // 63,
            (c & 0x1F) * 255 // 31,
        )

    a, b = rgb(c0), rgb(c1)
    if index == 0:
        return a
    if index == 1:
        return b
    if c0 > c1:
        if index == 2:
            return tuple((2 * x + y) // 3 for x, y in zip(a, b))
        return tuple((x + 2 * y) // 3 for x, y in zip(a, b))
    if index == 2:
        return tuple((x + y) // 2 for x, y in zip(a, b))
    return (0, 0, 0)


def painted_missing_chunks(
    data,
    chunks_width,
    chunks_height,
    blocksize,
    missing_color,
    candidates,
    tolerance=8,
):
    """Chunk indices (row-major) whose compressed pixels are one uniform
    field of ``missing_color``.

    The native builder fills a chunk whose JPEG fails to decode with the
    missing color and does not report which chunk failed, so this inspects
    the output. ``data`` covers ``chunks_height`` full chunk rows of a
    mipmap-0 image ``chunks_width`` chunks wide; only ``candidates`` (chunks
    built from provider JPEGs) are checked.
    """
    if np is None or not candidates or blocksize not in (8, 16):
        return []
    blocks_per_chunk = 64  # 256 px chunk / 4 px blocks
    words_per_block = blocksize // 8
    expected = chunks_width * chunks_height * blocks_per_chunk ** 2 * words_per_block
    words = np.frombuffer(data, dtype="<u8")
    if words.size != expected:
        return []
    # (chunk_row, block_row, chunk_col, block_col, word)
    grid = words.reshape(
        chunks_height,
        blocks_per_chunk,
        chunks_width,
        blocks_per_chunk,
        words_per_block,
    )
    painted = []
    for index in candidates:
        chunk_row, chunk_col = divmod(int(index), chunks_width)
        if chunk_row >= chunks_height:
            continue
        chunk = grid[chunk_row, :, chunk_col, :, :]
        first = chunk[0, 0]
        if not (chunk == first).all():
            continue
        colour_word = int(first[-1])
        indices = (colour_word >> 32) & 0xFFFFFFFF
        if indices not in (0, 0x55555555, 0xAAAAAAAA, 0xFFFFFFFF):
            continue
        colour = _bc1_colour(colour_word)
        if all(abs(a - int(b)) <= tolerance for a, b in zip(colour, missing_color)):
            painted.append(int(index))
    return painted

"""Item 9: season colours applied to DXT blocks as they are served."""

import random
import struct

import numpy as np

from autoortho import colour_table

H = colour_table.DDS_HEADER_SIZE


def _rgb(c):
    return (
        ((c >> 11) & 31) * 255 / 31,
        ((c >> 5) & 63) * 255 / 63,
        (c & 31) * 255 / 31,
    )


def _pixels(block):
    """The 16 RGB pixels of a BC1 block (opaque four-colour mode)."""
    c0, c1, idx = struct.unpack("<HHI", block)
    a, b = np.array(_rgb(c0)), np.array(_rgb(c1))
    palette = [a, b, (2 * a + b) / 3, (a + 2 * b) / 3]
    return [palette[(idx >> (2 * i)) & 3] for i in range(16)]


def _desaturate(rgb, s):
    luma = 0.212671 * rgb[0] + 0.715160 * rgb[1] + 0.072169 * rgb[2]
    return np.array([s * v + (1 - s) * luma for v in rgb])


def test_full_saturation_is_identity():
    table = colour_table.saturation_table(1.0)
    assert np.array_equal(table, np.arange(65536, dtype=np.uint16))


def test_blocks_are_desaturated_like_aoimage():
    rng = random.Random(7)
    table = colour_table.saturation_table(0.55)
    blocks = bytearray()
    for _ in range(200):
        c0, c1 = sorted(rng.sample(range(65536), 2), reverse=True)
        blocks += struct.pack("<HHI", c0, c1, rng.getrandbits(32))
    out = colour_table.apply(bytes(blocks), H, 8, table)
    for i in range(200):
        before = _pixels(bytes(blocks[i * 8:i * 8 + 8]))
        after = _pixels(out[i * 8:i * 8 + 8])
        c0, c1 = struct.unpack("<HH", out[i * 8:i * 8 + 4])
        assert c0 > c1 or (c0 == c1 == 0)  # still the opaque four-colour mode
        for p, q in zip(before, after):
            assert np.abs(_desaturate(p, 0.55) - q).max() < 12  # 565 rounding


def test_three_colour_blocks_are_left_alone():
    table = colour_table.saturation_table(0.3)
    block = struct.pack("<HHI", 0x1000, 0xF800, 0x12345678)  # c0 <= c1
    assert colour_table.apply(block, H, 8, table) == block


def test_blocks_cut_by_the_read_are_left_alone():
    table = colour_table.saturation_table(0.3)
    blocks = struct.pack("<HHI", 0xF800, 0x07E0, 0) * 3
    # A read starting 3 bytes into the first block and ending 2 bytes into the third.
    piece = blocks[3:18]
    out = colour_table.apply(piece, H + 3, 8, table)
    assert out[:5] == piece[:5]     # tail of block 0
    assert out[13:] == piece[13:]   # head of block 2
    assert out[5:13] != piece[5:13]  # block 1 recoloured


def test_header_is_never_touched():
    table = colour_table.saturation_table(0.0)
    data = bytes(range(128)) + struct.pack("<HHI", 0xF800, 0x001F, 0)
    out = colour_table.apply(data, 0, 8, table)
    assert out[:128] == data[:128]
    assert out[128:] != data[128:]


def test_bc3_colour_half_is_recoloured_and_alpha_kept():
    table = colour_table.saturation_table(0.0)
    alpha = bytes(range(8))
    block = alpha + struct.pack("<HHI", 0x07E0, 0xF800, 0xAAAAAAAA)
    out = colour_table.apply(block, H, 16, table)
    assert out[:8] == alpha
    c0, c1 = struct.unpack("<HH", out[8:12])
    assert _rgb(c0)[0] == _rgb(c0)[2] or abs(_rgb(c0)[0] - _rgb(c0)[2]) < 9  # grey


def test_block_bytes_by_format():
    assert colour_table.block_bytes("BC1") == 8
    assert colour_table.block_bytes("BC3") == 16
    assert colour_table.block_bytes("RGBA") == 0


def test_tile_recolours_served_bytes(tmp_path, monkeypatch):
    from autoortho import getortho

    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    monkeypatch.setattr(
        getortho,
        "season_colour_table",
        lambda row, col, zoom: colour_table.saturation_table(0.0),
    )
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    try:
        block = struct.pack("<HHI", 0xF800, 0x07E0, 0)
        out = tile._recolour(block, H)
        assert out != block
        monkeypatch.setattr(getortho, "season_colour_table", lambda *a: None)
        assert tile._recolour(block, H) == block
    finally:
        tile.close()

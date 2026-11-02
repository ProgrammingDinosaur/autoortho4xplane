"""Unbuilt DDS ranges are served from the nearest built lower mipmap."""

import struct
import types

import pytest

from autoortho import getortho, pydds
from autoortho.dds_manifest import MipmapSource


def _bc1_block(c0, c1, indices):
    packed = 0
    for pixel, index in enumerate(indices):
        packed |= (index & 3) << (2 * pixel)
    return struct.pack("<HHI", c0, c1, packed)


def _bc3_block(a0, a1, alpha_indices, c0, c1, indices):
    packed = 0
    for pixel, index in enumerate(alpha_indices):
        packed |= (index & 7) << (3 * pixel)
    alpha = struct.pack("<BB", a0, a1) + packed.to_bytes(6, "little")
    return alpha + _bc1_block(c0, c1, indices)


def _decode_bc1(block):
    """(endpoints, 16 indices) of a BC1 colour block."""
    c0, c1, packed = struct.unpack("<HHI", block)
    return (c0, c1), [(packed >> (2 * p)) & 3 for p in range(16)]


def _decode_bc3_alpha(block):
    a0, a1 = block[0], block[1]
    packed = int.from_bytes(block[2:8], "little")
    return (a0, a1), [(packed >> (3 * p)) & 7 for p in range(16)]


def _source_blocks(count, bc3=False):
    blocks = []
    for k in range(count):
        indices = [(k + p) % 4 for p in range(16)]
        if bc3:
            alpha = [(k + 2 * p) % 8 for p in range(16)]
            blocks.append(_bc3_block(10 + k, 200, alpha, 100 + k, 7, indices))
        else:
            blocks.append(_bc1_block(100 + k, 7, indices))
    return blocks


def _pixel_index(blocks, width_blocks, px, py, decode):
    block = blocks[(py // 4) * width_blocks + px // 4]
    _endpoints, indices = decode(block)
    return indices[(py % 4) * 4 + px % 4]


@pytest.mark.parametrize("bc3", [False, True])
def test_unbuilt_mipmap_zero_is_nearest_neighbour_of_mipmap_one(bc3):
    dds = pydds.DDS(32, 32, dxt_format="BC3" if bc3 else "BC1")
    bs = dds.blocksize
    source = _source_blocks(4 * 4, bc3)  # mipmap 1 is 16x16 px = 4x4 blocks
    dds.replace_mipmap_dense(1, b"".join(source))

    mm0 = dds.mipmap_list[0]
    served = dds.read_served(mm0.startpos, mm0.length)
    out = [served[i:i + bs] for i in range(0, len(served), bs)]
    colour = (lambda b: _decode_bc1(b[8:])) if bc3 else _decode_bc1

    for py in range(32):
        for px in range(32):
            assert _pixel_index(out, 8, px, py, colour) == _pixel_index(
                [b[8:] if bc3 else b for b in source], 4, px // 2, py // 2,
                _decode_bc1,
            )
            if bc3:
                assert _pixel_index(out, 8, px, py, _decode_bc3_alpha) == (
                    _pixel_index(source, 4, px // 2, py // 2, _decode_bc3_alpha)
                )
    # Endpoints come from the sampled source block.
    for by in range(8):
        for bx in range(8):
            got = out[by * 8 + bx]
            want = source[(by // 2) * 4 + bx // 2]
            if bc3:
                assert got[:2] == want[:2]  # alpha endpoints
                assert got[8:12] == want[8:12]  # colour endpoints
            else:
                assert got[:4] == want[:4]


def test_deeper_mipmap_used_when_closer_one_is_unbuilt():
    dds = pydds.DDS(32, 32)
    source = _source_blocks(2 * 2)  # mipmap 2 is 8x8 px = 2x2 blocks
    dds.replace_mipmap_dense(2, b"".join(source))
    mm0 = dds.mipmap_list[0]

    served = dds.read_served(mm0.startpos, mm0.length)

    first = served[:8]
    assert first[:4] == source[0][:4]
    # Four-times upscale: every pixel of a block samples one source pixel.
    _endpoints, indices = _decode_bc1(first)
    assert len(set(indices)) == 1


def test_built_bytes_are_served_unchanged_and_partial_reads_align():
    dds = pydds.DDS(32, 32)
    dds.replace_mipmap_dense(1, b"".join(_source_blocks(16)))
    mm0 = dds.mipmap_list[0]
    built = b"\x01" * (8 * 8)  # first block row of mipmap 0
    dds.write_mipmap_at(0, 0, built)

    whole = dds.read_served(mm0.startpos, mm0.length)
    assert whole[:64] == built
    # An unaligned read inside the unbuilt part matches the whole read.
    part = dds.read_served(mm0.startpos + 77, 50)
    assert part == whole[77:127]


def test_no_built_lower_mipmap_keeps_missing_color():
    dds = pydds.DDS(32, 32)
    mm0 = dds.mipmap_list[0]
    assert dds.read_served(mm0.startpos, 64) == dds.read_at(mm0.startpos, 64)


def test_raw_read_is_not_synthesized():
    # Persistence reads raw bytes; display substitutes must never be saved.
    dds = pydds.DDS(32, 32)
    dds.replace_mipmap_dense(1, b"".join(_source_blocks(16)))
    mm0 = dds.mipmap_list[0]
    assert dds.read_at(mm0.startpos, mm0.length) == pydds.get_fallback_bytes(
        mm0.length, dds.blocksize
    )


def test_tile_serves_unbuilt_row_from_lower_mipmap(monkeypatch, tmp_path):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(
        0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16
    )
    mm1 = tile.dds.mipmap_list[1]
    blocks = mm1.length // tile.dds.blocksize
    tile.dds.replace_mipmap_dense(
        1, _bc1_block(0x1234, 0, [1] * 16) * blocks
    )
    mm0 = tile.dds.mipmap_list[0]
    row_bytes = mm0.length // tile.chunks_per_col

    data = tile._read_with_mm0_sealing(mm0.startpos, row_bytes)

    assert data != pydds.get_fallback_bytes(row_bytes, tile.dds.blocksize)
    manifest = tile._mm0_rows.manifest(0)
    assert set(manifest.sources) == {int(MipmapSource.LOWER_MIPMAP_MEMORY)}
    assert not manifest.is_exact


# ---------------------------------------------------------------------------
# Chunks the native decoder painted with missing color
# ---------------------------------------------------------------------------

from autoortho.dxt_upscale import painted_missing_chunks  # noqa: E402

MISSING = (66, 77, 55)


def _chunk_row(chunk_blocks, chunks_width):
    """Partial-build bytes for one chunk row: ``chunk_blocks[c]`` is the
    8-byte block repeated over chunk ``c`` (or a callable per block)."""
    out = bytearray()
    for block_row in range(64):
        for chunk in range(chunks_width):
            block = chunk_blocks[chunk]
            for block_col in range(64):
                out += block(block_row, block_col) if callable(block) else block
    return bytes(out)


def _rgb565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def test_detects_chunk_filled_with_missing_color():
    painted = pydds.get_fallback_bytes(8, 8)
    # Encoders may also emit c0=colour, c1=other with every index on c0.
    painted_alt = struct.pack("<HHI", _rgb565(*MISSING), 0x0000, 0)
    textured = lambda r, c: struct.pack("<HHI", 0x1234 + r, 0x0101 + c, 0xE4E4E4E4)
    blue = struct.pack("<HHI", _rgb565(20, 20, 220), _rgb565(20, 20, 220), 0)
    data = _chunk_row([painted, textured, painted_alt, blue], 4)

    assert painted_missing_chunks(data, 4, 1, 8, MISSING, [0, 1, 2, 3]) == [0, 2]
    # Only chunks built from provider JPEGs are candidates.
    assert painted_missing_chunks(data, 4, 1, 8, MISSING, [1, 2]) == [2]


def test_corrupt_jpeg_is_rebuilt_with_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    width = tile.chunks_per_row
    mm = tile.dds.mipmap_list[0]
    row_bytes = mm.length // tile.chunks_per_col
    painted = pydds.get_fallback_bytes(8, 8)
    textured = struct.pack("<HHI", 0x1234, 0x0101, 0xE4E4E4E4)
    first_build = _chunk_row([painted] + [textured] * (width - 1), width)
    rebuilt = b"\x22" * row_bytes

    grid = tile._get_chunk_grid(16)
    corrupt = grid.get(0)
    corrupt.data = b"\xff\xd8\xff" + b"\x00" * 200
    corrupt.ready.set()
    cache_file = tmp_path / f"{corrupt.chunk_id}.jpg"
    cache_file.write_bytes(corrupt.data)
    monkeypatch.setattr(
        tile,
        "_collect_row_chunk_jpegs",
        lambda *args, **kwargs: ([b"jpeg"] * width, width, 1),
    )
    resolved = []

    def resolve(jpegs, startrow, chunks_width, zoom, deadline, *args):
        resolved.append([i for i, data in enumerate(jpegs) if data is None])
        sources = list(jpegs)
        sources[0] = {"pixels": b"\x00" * 262144, "mode": "RGBA",
                      "width": 256, "height": 256}
        return sources, [0], [], {0: MipmapSource.LOWER_ZL_CACHE}

    monkeypatch.setattr(tile, "_resolve_partial_sources", resolve)
    builds = []

    def build(**kwargs):
        builds.append(kwargs)
        data = first_build if len(builds) == 1 else rebuilt
        return types.SimpleNamespace(
            success=True, data=data, bytes_written=len(data),
            elapsed_ms=1.0, error=None,
        )

    monkeypatch.setattr(
        getortho,
        "_get_native_dds",
        lambda: types.SimpleNamespace(
            build_partial_mipmap=build, build_partial_mipmap_v2=build
        ),
    )

    assert tile._try_native_partial_mipmap_build(0, 0, 0, row_bytes, None)

    assert resolved == [[0]]
    assert len(builds) == 2
    manifest = tile._mm0_rows.manifest(0)
    assert manifest.sources[0] == int(MipmapSource.LOWER_ZL_CACHE)
    assert manifest.sources[1] == int(MipmapSource.TARGET_PROVIDER)
    assert tile.dds.read_at(mm.startpos, row_bytes) == rebuilt
    # The bad download is dropped so the next request fetches it again.
    assert not cache_file.exists()
    assert corrupt.data is None and not corrupt.ready.is_set()


def _painted_row_tile(monkeypatch, tmp_path, jpeg):
    """Tile whose first native row build paints chunk 0 missing color."""
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    width = tile.chunks_per_row
    mm = tile.dds.mipmap_list[0]
    row_bytes = mm.length // tile.chunks_per_col
    painted = pydds.get_fallback_bytes(8, 8)
    textured = struct.pack("<HHI", 0x1234, 0x0101, 0xE4E4E4E4)
    data = _chunk_row([painted] + [textured] * (width - 1), width)
    monkeypatch.setattr(
        tile,
        "_collect_row_chunk_jpegs",
        lambda *args, **kwargs: ([jpeg] * width, width, 1),
    )
    calls = {"builds": 0, "resolves": 0}

    def build(**kwargs):
        calls["builds"] += 1
        return types.SimpleNamespace(
            success=True, data=data, bytes_written=len(data),
            elapsed_ms=1.0, error=None,
        )

    def resolve(*args, **kwargs):
        calls["resolves"] += 1
        raise AssertionError("no fallback rebuild expected")

    monkeypatch.setattr(tile, "_resolve_partial_sources", resolve)
    monkeypatch.setattr(
        getortho,
        "_get_native_dds",
        lambda: types.SimpleNamespace(
            build_partial_mipmap=build, build_partial_mipmap_v2=build
        ),
    )
    return tile, row_bytes, calls


def test_speculative_build_with_corrupt_chunk_is_left_for_live(
    monkeypatch, tmp_path
):
    tile, row_bytes, calls = _painted_row_tile(monkeypatch, tmp_path, b"jpeg")

    assert not tile._try_native_partial_mipmap_build(
        0, 0, 0, row_bytes, None, priority=getortho.PRIORITY_PREFETCH
    )
    assert calls == {"builds": 1, "resolves": 0}
    assert tile._mm0_rows.manifest(0) is None


def test_uniform_real_chunk_matching_missing_color_is_kept(
    monkeypatch, tmp_path
):
    from autoortho.aoimage import AoImage

    source = tmp_path / "flat.jpg"
    AoImage.new("RGBA", (256, 256), MISSING).write_jpg(str(source))
    tile, row_bytes, calls = _painted_row_tile(
        monkeypatch, tmp_path, source.read_bytes()
    )

    assert tile._try_native_partial_mipmap_build(0, 0, 0, row_bytes, None)

    assert calls == {"builds": 1, "resolves": 0}
    assert tile._mm0_rows.manifest(0).is_exact


def test_excluded_lower_mipmap_is_not_an_upscale_source():
    dds = pydds.DDS(32, 32)
    dds.replace_mipmap_dense(1, b"".join(_source_blocks(16)))
    deeper = _source_blocks(4)
    dds.replace_mipmap_dense(2, b"".join(deeper))
    mm0 = dds.mipmap_list[0]

    served = dds.read_served(mm0.startpos, 8, exclude_sources={1})

    assert served[:4] == deeper[0][:4]


def test_sealed_substitute_row_is_kept_for_the_open(monkeypatch, tmp_path):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    mm1 = tile.dds.mipmap_list[1]
    blocks = mm1.length // tile.dds.blocksize
    tile.dds.replace_mipmap_dense(1, _bc1_block(0x1234, 0, [1] * 16) * blocks)
    mm0 = tile.dds.mipmap_list[0]
    row_bytes = mm0.length // tile.chunks_per_col

    first = tile._read_with_mm0_sealing(mm0.startpos, row_bytes)
    # The lower mipmap changes later in the same open...
    tile.dds.replace_mipmap_dense(1, _bc1_block(0x4321, 0, [2] * 16) * blocks)
    again = tile._read_with_mm0_sealing(mm0.startpos, row_bytes)

    # ...but the sealed row keeps what was first served.
    assert again == first
    assert mm0.gaps(0, row_bytes) == []
    assert mm0.gaps(row_bytes, 2 * row_bytes) == [(row_bytes, 2 * row_bytes)]


def test_written_row_without_manifest_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    mm1 = tile.dds.mipmap_list[1]
    blocks = mm1.length // tile.dds.blocksize
    tile.dds.replace_mipmap_dense(1, _bc1_block(0x1234, 0, [1] * 16) * blocks)
    mm0 = tile.dds.mipmap_list[0]
    row_bytes = mm0.length // tile.chunks_per_col
    real = b"\x5a" * row_bytes
    tile.dds.write_mipmap_at(0, 0, real)

    assert tile._read_with_mm0_sealing(mm0.startpos, row_bytes) == real

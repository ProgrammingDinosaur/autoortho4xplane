"""Item 1: only the mipmap X-Plane wants triggers downloads."""

import types

import pytest

from autoortho import dds_manifest, getortho, read_pattern, streaming_options

HEADER = read_pattern.DDS_HEADER_SIZE
CHUNK = 65536


def _layout(size=4096, block=8):
    """Mipmaps of a square BC1 texture, as pydds lays them out."""
    mipmaps, pos, idx = [], HEADER, 0
    while size >= 4:
        length = (size // 4) * (size // 4) * block
        mipmaps.append(
            types.SimpleNamespace(
                idx=idx, startpos=pos, endpos=pos + length, length=length
            )
        )
        pos += length
        size //= 2
        idx += 1
    return mipmaps


def _aligned_read_for(mipmaps, level):
    """The 64 KB-aligned read X-Plane issues for ``level``."""
    start = mipmaps[level].startpos // CHUNK * CHUNK
    return start, CHUNK


def test_header_chunk_wants_nothing():
    mm = _layout()
    header, wanted, first_needed = read_pattern.classify(
        mm, read_pattern.ReadCursor(), 0, CHUNK
    )
    assert header is True
    assert wanted is None
    assert first_needed == 1  # mipmap 0 bytes are incidental


def test_reading_on_from_header_chunk_wants_level_zero():
    mm = _layout()
    cursor = read_pattern.ReadCursor()
    read_pattern.classify(mm, cursor, 0, CHUNK)
    header, wanted, first_needed = read_pattern.classify(mm, cursor, CHUNK, CHUNK)
    assert not header
    assert wanted == 0
    assert first_needed == 0


def test_read_for_coarser_level_skips_tail_of_finer_one():
    mm = _layout()
    cursor = read_pattern.ReadCursor()
    read_pattern.classify(mm, cursor, 0, CHUNK)
    offset, length = _aligned_read_for(mm, 2)
    assert mm[1].startpos < offset < mm[1].endpos  # starts in mipmap 1's tail
    header, wanted, first_needed = read_pattern.classify(mm, cursor, offset, length)
    assert not header
    assert wanted == 2
    assert first_needed == 2


def test_continuation_of_level_zero_into_level_one_is_needed():
    mm = _layout(256)  # mipmap 0 is 32 KB: one read covers it and mipmap 1
    cursor = read_pattern.ReadCursor()
    read_pattern.classify(mm, cursor, 0, 1024)
    # Next piece starts where the last read ended: not a tail read.
    _header, _wanted, first_needed = read_pattern.classify(mm, cursor, 1024, CHUNK)
    assert first_needed == 0


def test_macfuse_split_header_chunk_stays_incidental():
    # macFUSE splits X-Plane's 64 KB reads into 32 KB pieces.
    mm = _layout()
    cursor = read_pattern.ReadCursor()
    half = CHUNK // 2
    assert read_pattern.classify(mm, cursor, 0, half)[2] == 1
    header, wanted, first_needed = read_pattern.classify(mm, cursor, half, half)
    assert header and wanted is None and first_needed == 1
    # Reading on past X-Plane's first chunk is a real request for level 0.
    header, wanted, first_needed = read_pattern.classify(mm, cursor, CHUNK, half)
    assert not header and wanted == 0 and first_needed == 0


def test_read_starting_exactly_at_a_level_wants_it():
    mm = _layout()
    _h, wanted, first_needed = read_pattern.classify(
        mm, read_pattern.ReadCursor(), mm[3].startpos, 4096
    )
    assert wanted == 3 and first_needed == 3


def test_read_inside_one_level_is_still_needed():
    # FUSE may split or read ahead: stay conservative and build it.
    mm = _layout()
    _h, _wanted, first_needed = read_pattern.classify(
        mm, read_pattern.ReadCursor(), mm[0].startpos + 3 * CHUNK, CHUNK
    )
    assert first_needed == 0


def test_whole_mipmap_zero_read_is_not_header_chunk():
    mm = _layout(256)
    header, wanted, _ = read_pattern.classify(
        mm, read_pattern.ReadCursor(), 0, mm[0].endpos
    )
    assert not header and wanted == 0


# ---------------------------------------------------------------- the tile


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    calls = types.SimpleNamespace(bytes=[], mipmaps=[])

    def fake_get_bytes(offset, length, time_budget=None):
        calls.bytes.append((t.find_mipmap_pos(offset), offset, length))
        return True

    def fake_get_mipmap(mipmap=0, time_budget=None):
        calls.mipmaps.append(mipmap)
        return True

    monkeypatch.setattr(t, "get_bytes", fake_get_bytes)
    monkeypatch.setattr(t, "get_mipmap", fake_get_mipmap)
    t.calls = calls
    yield t
    t.close()


def _flag(monkeypatch, name, value):
    original = streaming_options.flag
    monkeypatch.setattr(
        streaming_options,
        "flag",
        lambda n: value if n == name else original(n),
    )


def test_header_chunk_downloads_no_level_zero_rows(tile):
    data = tile._read_dds_bytes_budgeted(0, CHUNK, getortho.TimeBudget(5))
    assert len(data) == CHUNK
    assert not any(level == 0 for level, _o, _l in tile.calls.bytes)
    assert tile.calls.mipmaps == [tile.max_mipmap]  # the base level only


def test_header_chunk_does_not_seal_level_zero(tile):
    tile._read_dds_bytes_budgeted(0, CHUNK, getortho.TimeBudget(5))
    assert tile._mm0_rows._states[0] not in dds_manifest._SERVED_STATES
    # Reading on into mipmap 0 now asks for its rows.
    tile._read_dds_bytes_budgeted(CHUNK, CHUNK, getortho.TimeBudget(5))
    assert any(level == 0 for level, _o, _l in tile.calls.bytes)


def test_tail_of_finer_level_is_not_built(tile):
    mm = tile.dds.mipmap_list
    tile._read_dds_bytes_budgeted(0, CHUNK, getortho.TimeBudget(5))
    offset, length = _aligned_read_for(mm, 2)
    tile._read_dds_bytes_budgeted(offset, length, getortho.TimeBudget(5))
    built = {level for level, _o, _l in tile.calls.bytes}
    assert 1 not in built


def test_classifier_off_restores_old_behavior(tile, monkeypatch):
    _flag(monkeypatch, "read_classifier", False)
    tile._read_dds_bytes_budgeted(0, CHUNK, getortho.TimeBudget(5))
    assert any(level == 0 for level, _o, _l in tile.calls.bytes)

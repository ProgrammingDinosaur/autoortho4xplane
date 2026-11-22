"""Performance plan, item 3: getortho's chunks on the packed chunk store."""

import os

import pytest

from autoortho import chunk_store, getortho


def _jpeg(tag) -> bytes:
    return b"\xff\xd8\xff" + str(tag).encode() * 40


@pytest.fixture
def store_on(monkeypatch):
    previous = chunk_store.enabled()
    chunk_store.close_all()
    chunk_store.configure(True)
    monkeypatch.setattr(getortho, "disk_budget_manager", None)
    yield
    chunk_store.close_all()
    chunk_store.configure(previous)


def test_saved_chunk_is_found_by_the_next_chunk(tmp_path, store_on):
    first = getortho.Chunk(40, 50, "BI", 16, cache_dir=str(tmp_path), skip_cache_check=True)
    first.save_cache(_jpeg("x"))
    # No file of the old layout is written.
    assert not os.path.exists(first.cache_path)
    chunk_store.flush_all()
    second = getortho.Chunk(40, 50, "BI", 16, cache_dir=str(tmp_path))
    assert second.ready.is_set()
    assert second.data == _jpeg("x")


def test_batch_read_goes_through_the_store(tmp_path, store_on):
    store = chunk_store.open_store(str(tmp_path))
    for c in range(4):
        store.put(c, 0, 16, "BI", _jpeg(c))
    paths = [os.path.join(str(tmp_path), f"{c}_0_16_BI.jpg") for c in range(6)]
    found = getortho._batch_read_cache_files(paths)
    assert set(found) == set(paths[:4])


def test_remembered_no_imagery_settles_without_a_download(tmp_path, store_on):
    sea = getortho.Chunk(7, 8, "BI", 15, cache_dir=str(tmp_path), skip_cache_check=True)
    assert chunk_store.mark_no_imagery_path(sea.cache_path)
    chunk_store.flush_all()

    # The next session: a chunk created with a cache check.
    again = getortho.Chunk(7, 8, "BI", 15, cache_dir=str(tmp_path))
    assert again.ready.is_set()
    assert again.permanent_failure
    assert again.failure_reason == "no_imagery"
    assert again.data == b""

    # And through a texture's batch probe.
    grid_chunk = getortho.Chunk(7, 8, "BI", 15, cache_dir=str(tmp_path), skip_cache_check=True)
    getortho.ChunkGrid._probe_chunks([grid_chunk])
    assert grid_chunk.ready.is_set()
    assert grid_chunk.failure_reason == "no_imagery"


def test_old_files_are_read_through(tmp_path, store_on):
    path = os.path.join(str(tmp_path), "3_4_16_BI.jpg")
    with open(path, "wb") as handle:
        handle.write(_jpeg("legacy"))
    chunk = getortho.Chunk(3, 4, "BI", 16, cache_dir=str(tmp_path))
    assert chunk.ready.is_set() and chunk.data == _jpeg("legacy")
    chunk_store.flush_all()
    assert chunk_store.open_store(str(tmp_path)).get(3, 4, 16, "BI") == _jpeg("legacy")


def test_corrupt_chunk_is_removed_from_the_store(tmp_path, store_on, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    try:
        grid = tile._get_chunk_grid(tile.max_zoom)
        chunk = grid.ensure_range(0, 1)[0]
        store = chunk_store.open_store(str(tmp_path))
        store.put(chunk.col, chunk.row, chunk.zoom, chunk.maptype, _jpeg("bad"))
        store.flush()
        tile._discard_corrupt_chunks(tile.max_zoom, 0, [0])
        assert store.get(chunk.col, chunk.row, chunk.zoom, chunk.maptype) is None
    finally:
        tile.close()

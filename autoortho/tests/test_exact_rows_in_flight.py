"""Performance plan, item 7: exact row states seen by a prefetch pass."""

import pytest

from autoortho import getortho


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    yield t
    t.close()


def test_counts_rows_queued_building_and_unsaved(tile):
    tile._exact_row_states.update(
        {0: "build_queued", 1: "building", 2: "persisted", 3: "unbuilt"}
    )
    assert tile.exact_rows_in_flight() == 2


def test_row_run_on_a_closed_tile_resets_its_state(tile, monkeypatch):
    released = []
    monkeypatch.setattr(
        getortho._exact_row_capacity, "release", lambda: released.append(1)
    )
    tile._exact_row_states[4] = "build_queued"
    tile._closed = True
    tile._run_exact_prefetch_row(4)
    assert tile._exact_row_states[4] == "unbuilt"
    assert tile.exact_rows_in_flight() == 0
    assert released == [1]

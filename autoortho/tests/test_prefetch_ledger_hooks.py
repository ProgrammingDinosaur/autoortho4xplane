"""The prefetch ledger's hooks: prefetch paths, completion, eviction, reads."""

import math
import time
import types

import pytest

from autoortho import getortho, paging_model, prefetch_ledger
from autoortho.prefetch import CandidateState, PrefetchCoordinator, PrefetchKey, PrefetchSource


def _rc(lat, lon, zoom=16):
    n = 2 ** zoom
    col = int((lon + 180) / 360 * n) // 16 * 16
    row = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n) // 16 * 16
    return row, col


@pytest.fixture
def ledger(monkeypatch):
    fresh = prefetch_ledger.PrefetchLedger()
    monkeypatch.setattr(getortho, "tile_ledger", fresh)
    return fresh


@pytest.fixture
def bumps(monkeypatch):
    counted = []
    monkeypatch.setattr(getortho, "bump", lambda name, value=1: counted.append(name))
    return counted


# ------------------------------------------------------------ _prefetch_tile


class _RealTileCacher:
    """Opens real tiles the way TileCacher does: Tile(col, row, ...)."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.tiles = {}

    def _open_tile(self, row, col, maptype, zoom):
        key = (row, col, maptype, zoom)
        tile = self.tiles.get(key)
        if tile is None:
            tile = getortho.Tile(col, row, maptype, zoom, cache_dir=self.cache_dir,
                                 max_zoom=zoom)
            self.tiles[key] = tile
        tile.refs = 1
        return tile

    def _close_tile(self, row, col, maptype, zoom):
        self.tiles[(row, col, maptype, zoom)].refs = 0

    def close(self):
        for tile in self.tiles.values():
            tile.chunks.clear()
            tile.close()


@pytest.fixture
def real_prefetcher(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "is_prefetch_runtime_allowed", lambda *a, **k: True)
    monkeypatch.setattr(getortho, "tile_completion_tracker", None)
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    monkeypatch.setattr(
        getortho, "chunk_getter",
        types.SimpleNamespace(submit=lambda chunk, **kwargs: True),
    )
    p = getortho.SpatialPrefetcher()
    p._tile_cacher = _RealTileCacher(str(tmp_path))
    yield p
    p._tile_cacher.close()


def test_prefetch_tile_records_key_level_and_source(real_prefetcher, ledger):
    submitted, complete = real_prefetcher._prefetch_tile(
        22880, 34320, 16, "BI", first_mip=2, source="paging"
    )
    assert submitted > 0 and complete
    entry = ledger.get(prefetch_ledger.ledger_key(22880, 34320, 16))
    assert entry.finest_mip == 2 and entry.sources == {"paging"}
    assert entry.done_at is None


def test_one_tile_has_one_key_on_both_sides(real_prefetcher, ledger):
    real_prefetcher._prefetch_tile(22880, 34320, 16, "BI")
    tile = real_prefetcher._tile_cacher.tiles[(22880, 34320, "BI", 16)]
    read_key = prefetch_ledger.ledger_key(tile.row, tile.col, tile.tilename_zoom)
    assert read_key in ledger
    assert ledger.get(read_key).sources == {"velocity"}


def test_prefetch_tile_found_complete_is_done(real_prefetcher, ledger):
    tile = real_prefetcher._tile_cacher._open_tile(22880, 34320, "BI", 16)
    for mipmap in range(3, tile.max_mipmap + 1):
        tile._create_chunks(tile.max_zoom - mipmap)
        for chunk in tile.chunks.get(tile.max_zoom - mipmap, []):
            chunk.ready.set()
    submitted, complete = real_prefetcher._prefetch_tile(
        22880, 34320, 16, "BI", first_mip=3, source="ter"
    )
    assert submitted == 0 and complete
    entry = ledger.get(prefetch_ledger.ledger_key(22880, 34320, 16))
    assert entry.done_mip == 3


def test_truncated_prefetch_is_still_noted(real_prefetcher, ledger):
    submitted, complete = real_prefetcher._prefetch_tile(
        22880, 34320, 16, "BI", max_chunks=3, source="simbrief"
    )
    assert submitted == 3 and not complete
    assert ledger.get(prefetch_ledger.ledger_key(22880, 34320, 16)).sources == {"simbrief"}


def test_tile_xplane_holds_is_not_noted(real_prefetcher, ledger):
    tile = real_prefetcher._tile_cacher._open_tile(22880, 34320, "BI", 16)
    real_prefetcher._tile_cacher._open_tile = lambda *a: (setattr(tile, "refs", 2) or tile)
    assert real_prefetcher._prefetch_tile(22880, 34320, 16, "BI") == (0, True)
    assert len(ledger) == 0


# ------------------------------------------------------------ coordinator


class _Cacher:
    def _resolve_maptype(self, row, col, maptype, zoom):
        return "EOX"


def _coordinator(published, completed):
    return PrefetchCoordinator(
        tile_cacher=_Cacher(),
        chunk_getter=types.SimpleNamespace(),
        scenery_id="test",
        on_publish=lambda key, source: published.append((key, source)),
        on_complete=completed.append,
    )


def test_coordinator_reports_publish_and_complete():
    published, completed = [], []
    coordinator = _coordinator(published, completed)
    key = coordinator.make_key(1, 2, 16, 17, "BI")
    coordinator.publish(key, source=PrefetchSource.DSF, generation="g")
    coordinator.publish(key, source=PrefetchSource.VELOCITY, generation=1)
    assert published == [(key, "dsf"), (key, "velocity")]
    coordinator._finish_candidate(coordinator._candidates[key], CandidateState.COMPLETE)
    assert completed == [key]


def test_coordinator_live_and_rejected_publishes_are_not_reported():
    published, completed = [], []
    coordinator = _coordinator(published, completed)
    key = coordinator.make_key(1, 2, 16, 17, "BI")
    coordinator.publish(key, source=PrefetchSource.LIVE, generation="live")
    assert published == []
    coordinator._accepting = False
    coordinator.publish(key, source=PrefetchSource.DSF, generation="g")
    assert published == []


def test_coordinator_failure_is_not_complete():
    published, completed = [], []
    coordinator = _coordinator(published, completed)
    key = coordinator.make_key(1, 2, 16, 17, "BI")
    coordinator.publish(key, source=PrefetchSource.DSF, generation="g")
    coordinator._finish_candidate(coordinator._candidates[key], CandidateState.FAILED)
    assert completed == []


def test_dsf_maptype_override_shares_the_read_key(ledger):
    # DSF prefetch resolved the tile to EOX; X-Plane reads it as BI.
    key = PrefetchKey("test", 22880, 34320, 16, 17, "EOX")
    getortho._ledger_coordinator_published(key, "dsf")
    tile = types.SimpleNamespace(row=22880, col=34320, tilename_zoom=16, maptype="BI")
    read_key = prefetch_ledger.ledger_key(tile.row, tile.col, tile.tilename_zoom)
    assert ledger.get(read_key).sources == {"dsf"}
    getortho._ledger_coordinator_complete(key)
    assert ledger.get(read_key).done_mip == 0


# ------------------------------------------------------------ completion, eviction


def test_tile_complete_callback_marks_done(ledger, monkeypatch):
    monkeypatch.setattr(getortho, "background_dds_builder", None)
    ledger.note_queued(prefetch_ledger.ledger_key(5, 6, 16), 0, "velocity", 0.0)
    tile = types.SimpleNamespace(row=5, col=6, tilename_zoom=16)
    getortho._on_tile_complete_callback("t", tile, partial=True)
    assert ledger.get((5, 6, 16)).done_at is None
    getortho._on_tile_complete_callback("t", tile)
    assert ledger.get((5, 6, 16)).done_mip == 0


def test_eviction_records_the_finest_real_level(ledger):
    ledger.note_queued((5, 6, 16), 0, "velocity", time.monotonic() - 30)
    tile = types.SimpleNamespace(
        row=5, col=6, tilename_zoom=16,
        dds=types.SimpleNamespace(mipmap_list=[0] * 6), real_from=lambda: 0,
    )
    getortho._ledger_evicted(tile)
    assert ledger.classify_miss((5, 6, 16), 0, time.monotonic()) == "evicted"


def test_eviction_of_an_unbuilt_tile_is_not_done(ledger):
    ledger.note_queued((5, 6, 16), 0, "velocity", time.monotonic() - 30)
    tile = types.SimpleNamespace(
        row=5, col=6, tilename_zoom=16,
        dds=types.SimpleNamespace(mipmap_list=[0] * 6), real_from=lambda: 6,
    )
    getortho._ledger_evicted(tile)
    assert ledger.classify_miss((5, 6, 16), 0, time.monotonic()) == "late"


# ------------------------------------------------------------ pauses


def test_ram_brake_pause_is_recorded(ledger, monkeypatch):
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    monkeypatch.setattr(getortho, "is_live_building", lambda: False)
    monkeypatch.setattr(getortho, "ram_brake", types.SimpleNamespace(engaged=True))
    p = getortho.SpatialPrefetcher()
    p._do_prefetch_cycle()
    assert ledger.paused_reason(time.monotonic()) == "ram"
    assert p._last_skip_at is not None


# ------------------------------------------------------------ the read side


@pytest.fixture
def near_tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    monkeypatch.setattr(getortho.spatial_prefetcher, "prefetch_radius_nm", 10.0)
    row, col = _rc(47.0, 8.0)
    tile = getortho.Tile(col, row, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    tile._read_starting = False
    lat, lon = paging_model.tile_centre(row, col, 16, getortho.Tile.width)
    tile.centre = (lat, lon)
    yield tile
    tile.close()


def _camera(monkeypatch, lat, lon, track=90.0, speed=120.0):
    monkeypatch.setattr(getortho, "current_camera", lambda: (lat, lon, 1000.0, track, speed))


def test_never_queued_off_the_track_is_wide(near_tile, ledger, bumps, monkeypatch):
    lat, lon = near_tile.centre
    # 20 km south of the tile, flying east: the tile is 20 km off the track.
    _camera(monkeypatch, lat - 20_000 / 111_320, lon, track=90.0)
    near_tile._note_near_miss(0)
    assert bumps == ["near_miss_never_wide"]


def test_never_queued_on_the_track_is_inside(near_tile, ledger, bumps, monkeypatch):
    lat, lon = near_tile.centre
    # 20 km west of the tile, flying east: right on the track.
    _camera(monkeypatch, lat, lon - 20_000 / (111_320 * math.cos(math.radians(lat))))
    near_tile._note_near_miss(0)
    assert bumps == ["near_miss_never_inside"]


def test_queued_not_done_is_late(near_tile, ledger, bumps, monkeypatch):
    _camera(monkeypatch, *near_tile.centre)
    key = prefetch_ledger.ledger_key(near_tile.row, near_tile.col, near_tile.tilename_zoom)
    ledger.note_queued(key, 0, "velocity", time.monotonic() - 30)
    near_tile._note_near_miss(0)
    assert bumps == ["near_miss_late"]


def test_never_queued_while_paused_names_the_pause(near_tile, ledger, bumps, monkeypatch):
    _camera(monkeypatch, *near_tile.centre)
    ledger.note_paused(time.monotonic(), "ram")
    near_tile._note_near_miss(0)
    assert bumps == ["near_miss_paused_ram", "near_miss_never_paused"]


def test_starting_slow_or_no_position_counts_nothing(near_tile, ledger, bumps, monkeypatch):
    _camera(monkeypatch, *near_tile.centre)
    near_tile._read_starting = True
    near_tile._note_near_miss(0)
    near_tile._read_starting = False
    _camera(monkeypatch, *near_tile.centre, speed=10.0)
    near_tile._note_near_miss(0)
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    near_tile._note_near_miss(0)
    assert bumps == []

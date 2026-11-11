"""Item 6: how long a read may wait depends on how near the texture is."""

import pytest

from autoortho import getortho, streaming_options, wait_bands


def test_bands_by_level():
    assert wait_bands.band_of(0, 4) == wait_bands.NEAR
    assert wait_bands.band_of(1, 4) == wait_bands.NEAR
    assert wait_bands.band_of(2, 4) == wait_bands.MIDDLE
    assert wait_bands.band_of(4, 4) == wait_bands.FAR
    assert wait_bands.band_of(7, 4) == wait_bands.FAR


def test_camera_can_only_make_a_read_nearer():
    assert wait_bands.nearer(wait_bands.FAR, wait_bands.NEAR) == wait_bands.NEAR
    assert wait_bands.nearer(wait_bands.NEAR, wait_bands.FAR) == wait_bands.NEAR


def test_waits_follow_config():
    assert wait_bands.wait_seconds(wait_bands.NEAR, starting=True) == 60.0
    assert wait_bands.wait_seconds(wait_bands.MIDDLE, starting=False) == 6.0
    assert wait_bands.wait_seconds(wait_bands.FAR, starting=True) == 0.5


def test_flight_starts_parked_and_ends_when_moving():
    t = wait_bands.StartTracker()
    assert t.update(True, 47.0, 8.0, 0.0, 0.0)
    assert t.update(True, 47.0, 8.0, 3.0, 1.0)  # taxiing
    assert not t.update(True, 47.0, 8.0005, 60.0, 2.0)


def test_teleport_starts_again():
    t = wait_bands.StartTracker()
    t.update(True, 47.0, 8.0, 80.0, 0.0)
    assert not t.starting
    assert t.update(True, 51.0, 0.0, 0.0, 1.0)


def test_near_read_without_position_starts_again():
    t = wait_bands.StartTracker()
    t.update(True, 47.0, 8.0, 80.0, 0.0)
    assert not t.update(False, 0, 0, 0, 5.0, near_read=False)
    assert t.update(False, 0, 0, 0, 6.0, near_read=True)


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    yield t
    t.close()


def test_far_read_gets_short_budget(tile, monkeypatch):
    monkeypatch.setattr(getortho, "flight_is_starting", lambda near_read=False: True)
    budget = tile._band_budget(4, getortho.TimeBudget(60))
    assert budget.max_seconds == pytest.approx(0.5)


def test_near_read_waits_long_while_starting(tile, monkeypatch):
    monkeypatch.setattr(getortho, "flight_is_starting", lambda near_read=False: True)
    assert tile._band_budget(0, getortho.TimeBudget(600)).max_seconds == 60.0
    monkeypatch.setattr(getortho, "flight_is_starting", lambda near_read=False: False)
    assert tile._band_budget(0, getortho.TimeBudget(600)).max_seconds == 6.0


def test_band_never_exceeds_tile_budget(tile, monkeypatch):
    monkeypatch.setattr(getortho, "flight_is_starting", lambda near_read=False: True)
    assert tile._band_budget(0, getortho.TimeBudget(2)).max_seconds <= 2.0


def test_band_budget_does_not_replace_tile_budget(tile):
    tile_budget = tile._get_or_create_tile_time_budget()
    tile.mark_live(getortho.TimeBudget(0.5))
    assert tile._tile_time_budget is tile_budget


# ---------------------------------------------------------------- item 7


def test_chunk_priorities_order_bands_below_prefetch():
    near = wait_bands.chunk_priority(wait_bands.NEAR, 0)
    middle = wait_bands.chunk_priority(wait_bands.MIDDLE, 2)
    far = wait_bands.chunk_priority(wait_bands.FAR, 4)
    assert near < middle < far < getortho.PRIORITY_PREFETCH


def test_mark_live_raises_queued_chunks_to_band_only(tile):
    import types

    queued = types.SimpleNamespace(priority=104, in_queue=True, in_flight=False, prefetch=True)
    urgent = types.SimpleNamespace(priority=0, in_queue=True, in_flight=False, prefetch=False)
    tile.chunks = {12: [queued, urgent]}
    tile._read_band = wait_bands.FAR
    tile.mark_live(None)
    assert queued.priority == wait_bands.chunk_priority(wait_bands.FAR, 0)
    assert queued.prefetch is False
    assert urgent.priority == 0  # never lowered


def test_cached_reads_do_not_pause_prefetch(monkeypatch):
    controller = getortho.LivePressureController()
    controller.live_read_start()
    assert not controller.is_under_pressure("network")
    controller.update_download_pressure(True)
    assert controller.is_under_pressure("network")


def test_pause_comes_back_when_flag_is_off(monkeypatch):
    original = streaming_options.flag
    monkeypatch.setattr(
        streaming_options,
        "flag",
        lambda n: False if n == "prefetch_during_live_reads" else original(n),
    )
    controller = getortho.LivePressureController()
    controller.live_read_start()
    assert controller.is_under_pressure("network")

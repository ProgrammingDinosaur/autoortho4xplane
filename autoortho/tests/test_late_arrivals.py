"""How long imagery took after a read stopped waiting (the wait rule's evidence)."""

import math
import threading
import types

from autoortho import getortho, wait_bands


def _chunk(ready=False, failed=False):
    chunk = types.SimpleNamespace(ready=threading.Event(), permanent_failure=failed)
    if ready:
        chunk.ready.set()
    return chunk


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _tracker():
    counted = []
    clock = _Clock()
    tracker = getortho.LateArrivals(clock=clock, count=counted.append, auto_start=False)
    return tracker, clock, counted


def test_buckets_and_percentiles():
    assert wait_bands.late_bucket(0.4) == 0
    assert wait_bands.late_bucket(7) == wait_bands.LATE_BUCKETS_S.index(8)
    assert wait_bands.late_bucket(500) == len(wait_bands.LATE_BUCKETS_S)
    counts = [0] * (len(wait_bands.LATE_BUCKETS_S) + 1)
    counts[wait_bands.late_bucket(3)] = 8
    counts[wait_bands.late_bucket(20)] = 2
    assert wait_bands.late_percentile(counts, 0.8) == (3.0, 10)
    assert wait_bands.late_percentile(counts, 0.9) == (20.0, 10)
    counts[-1] = 90
    assert wait_bands.late_percentile(counts, 0.8) == (math.inf, 100)
    assert wait_bands.late_percentile([], 0.8) == (None, 0)


def test_arrival_is_counted_from_the_start_of_the_read():
    tracker, clock, counted = _tracker()
    chunks = [_chunk(), _chunk(ready=True)]
    assert tracker.note("tile", 0, chunks, started=97.0, starting=False)
    assert tracker.check() == 0 and counted == []
    clock.now = 104.0
    chunks[0].ready.set()
    assert tracker.check() == 1
    # 7 s after the read started: the 8 s bucket, in flight.
    assert counted == [wait_bands.late_stat_key(False, wait_bands.late_bucket(7))]
    assert tracker.watched() == 0


def test_nothing_pending_is_not_watched():
    tracker, _clock, _counted = _tracker()
    assert not tracker.note("tile", 0, [_chunk(ready=True)], 100.0, False)
    assert tracker.watched() == 0


def test_one_watch_per_tile_level():
    tracker, _clock, _counted = _tracker()
    assert tracker.note("tile", 1, [_chunk()], 100.0, True)
    assert not tracker.note("tile", 1, [_chunk()], 100.0, True)
    assert tracker.note("tile", 2, [_chunk()], 100.0, True)


def test_failed_downloads_are_not_late_arrivals():
    tracker, clock, counted = _tracker()
    chunk = _chunk()
    tracker.note("tile", 0, [chunk], 100.0, False)
    chunk.permanent_failure = True
    chunk.ready.set()
    clock.now = 101.0
    assert tracker.check() == 1
    assert counted == []


def test_never_arrived_counts_as_overflow_while_starting():
    tracker, clock, counted = _tracker()
    tracker.note("tile", 0, [_chunk()], 100.0, True)
    clock.now = 100.0 + wait_bands.LATE_GIVE_UP_S + 1
    assert tracker.check() == 1
    assert counted == [wait_bands.late_stat_key(True, len(wait_bands.LATE_BUCKETS_S))]


def test_watch_list_is_bounded():
    tracker, _clock, _counted = _tracker()
    tracker.MAX_WATCHED = 2
    assert tracker.note("a", 0, [_chunk()], 100.0, False)
    assert tracker.note("b", 0, [_chunk()], 100.0, False)
    assert not tracker.note("c", 0, [_chunk()], 100.0, False)


def test_tile_watches_the_chunks_of_the_level_it_gave_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tracker, _clock, _counted = _tracker()
    monkeypatch.setattr(getortho, "late_arrivals", tracker)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    try:
        pending = _chunk()
        tile.chunks[tile.max_zoom - 2] = types.SimpleNamespace(materialized=lambda: [pending])
        tile._read_band = wait_bands.MIDDLE
        tile._read_starting = True
        tile._note_late(2, 95.0)
        assert tracker.watched() == 1
        # Far reads never wait, so they are never late.
        tile._read_band = wait_bands.FAR
        tile._note_late(3, 95.0)
        assert tracker.watched() == 1
    finally:
        tile.chunks.clear()
        tile.close()

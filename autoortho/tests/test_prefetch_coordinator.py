import os
import queue
import sys
import threading
import time

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

from prefetch import (
    CandidateState,
    IndexedPriorityQueue,
    PrefetchBatchReason,
    PrefetchCapacitySnapshot,
    PrefetchCoordinator,
    PrefetchSource,
    PrefetchSubmitStatus,
)


class ReadyEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.callbacks = []

    def add_callback(self, callback):
        if self.is_set():
            callback(self)
        else:
            self.callbacks.append(callback)

    def set(self):
        was_set = self.is_set()
        super().set()
        if not was_set:
            callbacks, self.callbacks = self.callbacks, []
            for callback in callbacks:
                callback(self)


class FakeChunk:
    def __init__(self, index, cached=False):
        self.chunk_id = f"chunk-{index}"
        self.ready = ReadyEvent()
        self.data = None
        self.priority = 100
        self.prefetch = True
        self.in_queue = False
        self.in_flight = False
        self.cancelled = False
        if cached:
            self.data = b"jpeg"
            self.ready.set()

    def cancel(self):
        self.cancelled = True
        self.ready.set()


class FakeGrid:
    def __init__(self, count, cached=()):
        self.logical_length = count
        self.cached = set(cached)
        self.slots = {}
        self.ranges = []

    def ensure_range(self, start, end):
        self.ranges.append((start, end))
        for index in range(start, end):
            self.slots.setdefault(
                index, FakeChunk(index, cached=index in self.cached)
            )
        return [self.slots[index] for index in range(start, end)]

    def materialized(self):
        return tuple(self.slots[index] for index in sorted(self.slots))


class FakeTile:
    def __init__(self, row, col, maptype, terrain_zoom, target_zoom, count=8):
        self.row = row
        self.col = col
        self.maptype = maptype
        self.tilename_zoom = terrain_zoom
        self.max_zoom = target_zoom
        self.id = f"{row}_{col}_{maptype}_{terrain_zoom}"
        self._closed = False
        self.chunks = {target_zoom: FakeGrid(count)}

    def _get_chunk_grid(self, zoom):
        return self.chunks[zoom]


class FakeCacher:
    def __init__(self, chunk_count=8):
        self.chunk_count = chunk_count
        self.opens = 0
        self.closes = 0
        self.tiles = {}

    def _resolve_maptype(self, row, col, maptype, zoom):
        return "BI" if maptype in {"Default", "Custom Map"} else maptype

    def _open_tile(self, row, col, maptype, terrain_zoom):
        self.opens += 1
        key = (row, col, maptype, terrain_zoom)
        return self.tiles.setdefault(
            key,
            FakeTile(
                row,
                col,
                maptype,
                terrain_zoom,
                terrain_zoom,
                self.chunk_count,
            ),
        )

    def _close_tile(self, row, col, maptype, terrain_zoom):
        self.closes += 1


class FakeGetter:
    def __init__(self, available=8, statuses=()):
        self.available = available
        self.statuses = list(statuses)
        self.submitted = []
        self.cancelled = []
        self.snapshot_calls = 0
        self.reprioritized = 0

    def prefetch_capacity_snapshot(self):
        self.snapshot_calls += 1
        return PrefetchCapacitySnapshot(
            queue_available=self.available,
            stage_available=self.available,
            deferred_available=self.available,
            broker_background_available=self.available,
        )

    def queue_depths(self):
        return {"live": 0, "prefetch": 0}

    def submit_prefetch(self, chunk, **kwargs):
        status = (
            self.statuses.pop(0)
            if self.statuses
            else PrefetchSubmitStatus.ACCEPTED
        )
        if status == PrefetchSubmitStatus.ACCEPTED:
            chunk.in_queue = True
            self.submitted.append(chunk)
        return status

    def reprioritize_queue(self):
        self.reprioritized += 1

    def cancel_prefetch_work(self, reason):
        for chunk in self.submitted:
            if not chunk.ready.is_set():
                chunk.cancel()
        return len(self.submitted)

    def cancel_chunks(self, chunks, reason):
        self.cancelled.extend(chunks)
        for chunk in chunks:
            chunk.cancel()
        return len(chunks)


def make_coordinator(*, count=8, available=8, burst=4, statuses=(), cap=16):
    cacher = FakeCacher(count)
    getter = FakeGetter(available, statuses)
    coordinator = PrefetchCoordinator(
        tile_cacher=cacher,
        chunk_getter=getter,
        scenery_id="test",
        admission_burst=burst,
        max_candidates=cap,
        max_tile_leases=4,
        max_materialized_chunks=burst,
        max_jpeg_bytes=64 * 1024 * 1024,
    )
    return coordinator, cacher, getter


def publish(coordinator, row=1, generation=1, **kwargs):
    key = coordinator.make_key(row, 2, 17, 17, "Default")
    coordinator.publish(
        key,
        source=PrefetchSource.VELOCITY,
        generation=generation,
        distance_meters=kwargs.get("distance_meters", 100),
        eta_seconds=kwargs.get("eta_seconds", 10),
        quality_class=kwargs.get("quality_class", 2),
    )
    return key


def test_indexed_queue_reprioritizes_without_counting_tombstones():
    work = IndexedPriorityQueue(maxsize=2)
    work.put("background", item_key="a", item_priority=100)
    work.put("other", item_key="b", item_priority=50)
    assert work.reprioritize("a", 0)
    assert work.qsize() == 2
    assert work.get_nowait() == "background"
    work.task_done()
    assert work.get_nowait() == "other"
    work.task_done()


def test_candidates_coalesce_sources_and_reprioritize():
    coordinator, _, _ = make_coordinator()
    key = publish(coordinator, distance_meters=1000)
    assert not coordinator.publish(
        key,
        source=PrefetchSource.DSF,
        generation="dsf",
        distance_meters=50,
        quality_class=4,
    )
    candidate = coordinator._candidates[key]
    assert candidate.sources == {
        PrefetchSource.VELOCITY,
        PrefetchSource.DSF,
    }
    assert candidate.distance_meters == 50


def test_candidate_count_is_hard_bounded():
    coordinator, _, _ = make_coordinator(cap=3)
    for row in range(10):
        publish(coordinator, row=row, distance_meters=row)
    assert coordinator.snapshot()["candidates"] == 3


def test_replacing_generation_stales_absent_candidates():
    coordinator, _, _ = make_coordinator()
    old = publish(coordinator, row=1, generation=1)
    new = coordinator.make_key(2, 2, 17, 17, "BI")
    coordinator.replace_generation(
        PrefetchSource.VELOCITY,
        2,
        [{"key": new, "distance_meters": 10}],
    )
    assert old not in coordinator._candidates
    assert new in coordinator._candidates


def test_replacing_full_generation_frees_capacity_before_publish():
    coordinator, _, _ = make_coordinator(cap=1)
    old = publish(coordinator, row=1, generation=1)
    new = coordinator.make_key(2, 2, 17, 17, "BI")
    coordinator.replace_generation(
        PrefetchSource.VELOCITY,
        2,
        [{"key": new, "distance_meters": 10}],
    )
    assert old not in coordinator._candidates
    assert new in coordinator._candidates


def test_active_stale_candidate_does_not_block_replacement_capacity():
    coordinator, _, getter = make_coordinator(count=1, burst=1, cap=1)
    old = publish(coordinator, row=1, generation=1)
    retired = coordinator._candidates[old]
    coordinator._admit(retired)
    getter.cancel_chunks = lambda chunks, reason: 0
    new = coordinator.make_key(2, 2, 17, 17, "BI")

    coordinator.replace_generation(
        PrefetchSource.VELOCITY,
        2,
        [{"key": new, "distance_meters": 10}],
    )

    assert old not in coordinator._candidates
    assert retired.sequence in coordinator._retired
    assert new in coordinator._candidates


def test_no_capacity_does_not_open_or_materialize_tile():
    coordinator, cacher, _ = make_coordinator(available=0)
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    assert result.reason == PrefetchBatchReason.NO_CAPACITY
    assert result.cursor.next_position == 0
    assert cacher.opens == 0


def test_lazy_admission_never_exceeds_scan_budget_or_uses_lower_zoom():
    coordinator, cacher, getter = make_coordinator(count=20, burst=4)
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    tile = candidate.lease.tile
    assert result.scanned == 4
    assert result.submitted == 4
    assert tile.chunks[17].ranges == [(0, 4)]
    assert set(tile.chunks) == {17}
    assert len(getter.submitted) == 4
    assert cacher.closes == 0


def test_first_authoritative_no_capacity_preserves_cursor_and_drops_idle_lease():
    coordinator, cacher, _ = make_coordinator(
        count=8,
        statuses=(
            PrefetchSubmitStatus.ACCEPTED,
            PrefetchSubmitStatus.NO_CAPACITY,
        ),
    )
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    assert result.reason == PrefetchBatchReason.NO_CAPACITY
    assert result.cursor.next_position == 1
    assert result.scanned == 2
    assert candidate.lease is not None

    candidate.active_chunks.clear()
    coordinator._defer_candidate(candidate, result.reason)
    assert candidate.cursor.next_position == 1
    assert candidate.lease is None
    assert cacher.closes == 1


def test_exact_completion_queues_build_and_releases_lease():
    coordinator, cacher, getter = make_coordinator(count=2, burst=2)

    class Builder:
        def submit(self, tile, priority, completion_callback):
            completion_callback(True)
            return True

    coordinator.set_builder(Builder())
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    assert result.reason == PrefetchBatchReason.DOWNLOADING
    for chunk in getter.submitted:
        chunk.data = b"exact"
        chunk.in_queue = False
        chunk.ready.set()
    assert key not in coordinator._candidates
    assert cacher.closes == 1


def test_exact_completion_releases_when_predictive_builds_are_disabled():
    coordinator, cacher, getter = make_coordinator(count=1, burst=1)
    coordinator.set_builder(False)
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    assert result.reason == PrefetchBatchReason.DOWNLOADING
    chunk = getter.submitted[0]
    chunk.data = b"exact"
    chunk.ready.set()
    assert key not in coordinator._candidates
    assert cacher.closes == 1


def test_coordinator_parks_fully_scanned_downloads_without_busy_spin():
    coordinator, _, getter = make_coordinator(count=2, burst=2)
    coordinator.start()
    try:
        publish(coordinator)
        deadline = time.monotonic() + 1.0
        while len(getter.submitted) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(getter.submitted) == 2
        calls_after_admission = getter.snapshot_calls
        time.sleep(0.1)
        assert getter.snapshot_calls == calls_after_admission
    finally:
        coordinator.stop()


def test_live_pressure_defers_distant_candidate_without_opening_tile():
    coordinator, cacher, getter = make_coordinator()
    key = publish(coordinator, quality_class=5)
    getter.prefetch_capacity_snapshot = lambda: PrefetchCapacitySnapshot(
        queue_available=8,
        stage_available=8,
        deferred_available=8,
        live_queued=1,
        broker_background_available=8,
    )
    result = coordinator._admit(coordinator._candidates[key])
    assert result.reason == PrefetchBatchReason.LIVE_PRESSURE
    assert cacher.opens == 0


def test_live_promotion_transfers_tile_ownership_and_rekeys_chunks():
    coordinator, cacher, getter = make_coordinator(count=2, burst=2)
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    coordinator._admit(candidate)
    tile = candidate.lease.tile

    assert coordinator.promote_tile(tile)

    assert key not in coordinator._candidates
    assert cacher.closes == 1
    assert getter.reprioritized == 1
    assert all(chunk.priority == 0 for chunk in getter.submitted)
    assert all(chunk.prefetch is False for chunk in getter.submitted)
    assert coordinator.is_known(key)
    assert not coordinator.publish(
        key,
        source=PrefetchSource.DSF,
        generation="while-live",
    )
    coordinator.release_live_tile(tile)
    assert not coordinator.is_known(key)
    assert coordinator.publish(
        key,
        source=PrefetchSource.DSF,
        generation="after-close",
    )


def test_runtime_disabled_result_is_retryable_and_releases_idle_lease():
    coordinator, cacher, _ = make_coordinator(
        count=2,
        statuses=(PrefetchSubmitStatus.DISABLED,),
    )
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    assert result.reason == PrefetchBatchReason.DISABLED
    coordinator._defer_candidate(candidate, result.reason)
    assert candidate.state == CandidateState.WAITING_FOR_CAPACITY
    assert candidate.lease is None
    assert cacher.closes == 1


def test_stale_candidate_cannot_be_revived_by_defer():
    coordinator, _, _ = make_coordinator(available=0)
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    candidate.state = CandidateState.STALE
    coordinator._queue.remove(key)
    coordinator._defer_candidate(
        candidate, PrefetchBatchReason.NO_CAPACITY
    )
    assert candidate.state == CandidateState.STALE
    assert coordinator._queue.qsize() == 0


def test_stale_generation_cancels_queued_chunks_and_releases_after_settlement():
    coordinator, cacher, getter = make_coordinator(count=2, burst=2)
    key = publish(coordinator, generation=1)
    candidate = coordinator._candidates[key]
    coordinator._admit(candidate)
    assert candidate.state == CandidateState.DOWNLOADING
    replacement = coordinator.make_key(9, 2, 17, 17, "BI")
    coordinator.replace_generation(
        PrefetchSource.VELOCITY,
        2,
        [{"key": replacement}],
    )
    assert getter.cancelled
    assert key not in coordinator._candidates
    assert cacher.closes == 1


def test_failed_candidate_keeps_lease_until_other_active_chunks_settle():
    coordinator, cacher, getter = make_coordinator(count=2, burst=2)
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    coordinator._admit(candidate)
    first, second = getter.submitted

    first.data = b""
    first.ready.set()
    assert candidate.state == CandidateState.FAILED
    assert candidate.lease is not None
    assert cacher.closes == 0

    second.data = b"exact"
    second.ready.set()
    assert key not in coordinator._candidates
    assert cacher.closes == 1


def test_generation_change_cannot_revive_candidate_during_admission():
    coordinator, cacher, getter = make_coordinator(count=2, burst=2)
    key = publish(coordinator, generation=1)
    candidate = coordinator._candidates[key]
    tile = cacher._open_tile(1, 2, "BI", 17)
    grid = tile.chunks[17]
    started = threading.Event()
    release = threading.Event()
    original_ensure_range = grid.ensure_range

    def blocking_ensure_range(start, end):
        started.set()
        assert release.wait(1.0)
        return original_ensure_range(start, end)

    grid.ensure_range = blocking_ensure_range
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "value", coordinator._admit(candidate)
        )
    )
    thread.start()
    assert started.wait(1.0)

    replacement = coordinator.make_key(9, 2, 17, 17, "BI")
    coordinator.replace_generation(
        PrefetchSource.VELOCITY,
        2,
        [{"key": replacement}],
    )
    release.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert result["value"].reason == PrefetchBatchReason.STALE
    assert key not in coordinator._candidates
    assert getter.submitted == []
    assert cacher.closes == 1


def test_new_generation_replaces_terminal_candidate_with_active_work():
    coordinator, _, getter = make_coordinator(count=1, burst=1)
    key = publish(coordinator, generation=1)
    retired = coordinator._candidates[key]
    coordinator._admit(retired)
    retired.state = CandidateState.STALE
    coordinator._queue.remove(key)

    assert coordinator.publish(
        key,
        source=PrefetchSource.VELOCITY,
        generation=2,
    )
    replacement = coordinator._candidates[key]
    assert replacement is not retired
    assert replacement.state == CandidateState.DISCOVERED

    chunk = getter.submitted[0]
    chunk.data = b"exact"
    chunk.ready.set()
    assert coordinator._candidates[key] is replacement


def test_generation_index_is_removed_with_completed_candidate():
    coordinator, _, _ = make_coordinator()
    key = coordinator.make_key(1, 2, 17, 17, "BI")
    coordinator.publish(
        key,
        source=PrefetchSource.DSF,
        generation="dsf-path",
    )
    candidate = coordinator._candidates[key]
    coordinator._finish_candidate(candidate, CandidateState.COMPLETE)
    assert coordinator._generation_keys == {}


def test_already_ready_submission_race_records_exact_coverage():
    coordinator, _, getter = make_coordinator(count=1, burst=1)

    def become_ready(chunk, **kwargs):
        chunk.data = b"exact"
        chunk.ready.set()
        return PrefetchSubmitStatus.ALREADY_READY

    getter.submit_prefetch = become_ready
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    result = coordinator._admit(candidate)
    assert result.reason == PrefetchBatchReason.TARGET_COMPLETE
    assert candidate.coverage == {0}
    assert candidate.state == CandidateState.TARGET_READY


def test_builder_exception_clears_pending_ownership():
    coordinator, cacher, _ = make_coordinator()
    key = publish(coordinator)
    candidate = coordinator._candidates[key]
    assert coordinator._ensure_lease(candidate) is not None

    class BrokenBuilder:
        @staticmethod
        def submit(*args, **kwargs):
            raise RuntimeError("broken")

    coordinator.set_builder(BrokenBuilder())
    coordinator._queue_build(candidate)
    assert candidate.build_pending is False
    assert key not in coordinator._candidates
    assert cacher.closes == 1

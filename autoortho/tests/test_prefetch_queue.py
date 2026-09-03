import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

import getortho


class FakeChunk:
    def __init__(self, chunk_id, *, prefetch=False, priority=0):
        self.chunk_id = chunk_id
        self.tile_id = "tile"
        self.prefetch = prefetch
        self.priority = priority
        self.ready = threading.Event()
        self.data = None
        self.permanent_failure = False
        self.failure_reason = None
        self.fetchtime = None
        self.url = None
        self.in_queue = False
        self.in_flight = False
        self.cancelled = False
        self._profile_queued_at = None

    def __lt__(self, other):
        return (self.priority, self.chunk_id) < (
            other.priority,
            other.chunk_id,
        )

    def cancel(self):
        self.cancelled = True
        self.ready.set()


@pytest.fixture(autouse=True)
def clear_chunk_coalescing():
    getortho.ChunkGetter._queued_chunk_ids.clear()
    getortho.ChunkGetter._queued_chunk_objs.clear()
    getortho.ChunkGetter._queued_chunk_waiters.clear()
    yield
    getortho.ChunkGetter._queued_chunk_ids.clear()
    getortho.ChunkGetter._queued_chunk_objs.clear()
    getortho.ChunkGetter._queued_chunk_waiters.clear()


def test_prefetch_is_rejected_until_flight_connection(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: False
    )
    getter = getortho.ChunkGetter(0)
    prefetch = FakeChunk("prefetch", prefetch=True, priority=100)
    live = FakeChunk("live")

    assert getter.submit(prefetch) is False
    assert getter.submit(live) is True
    assert getter.queue_depths() == {"live": 1, "prefetch": 0}


def test_prefetch_policy_requires_a_valid_connected_flight(monkeypatch):
    monkeypatch.setattr(
        getortho.datareftracker, "has_ever_connected", False
    )
    monkeypatch.setattr(getortho.datareftracker, "running", True)
    monkeypatch.setattr(getortho.datareftracker, "connected", False)
    monkeypatch.setattr(getortho.datareftracker, "data_valid", False)
    assert getortho.is_prefetch_runtime_allowed() is False

    monkeypatch.setattr(
        getortho.datareftracker, "has_ever_connected", True
    )
    monkeypatch.setattr(getortho.datareftracker, "connected", True)
    monkeypatch.setattr(getortho.datareftracker, "data_valid", True)
    monkeypatch.setattr(getortho, "is_live_building", lambda: False)
    assert getortho.is_prefetch_runtime_allowed() is True


def test_worker_prefetch_uses_parent_shared_flight_state(monkeypatch):
    monkeypatch.setattr(getortho.datareftracker, "running", False)
    monkeypatch.setattr(
        getortho.datareftracker, "has_ever_connected", False
    )
    monkeypatch.setattr(getortho.datareftracker, "connected", False)
    monkeypatch.setattr(getortho.datareftracker, "data_valid", False)
    monkeypatch.setattr(getortho, "is_live_building", lambda: False)
    monkeypatch.setattr(
        getortho,
        "get_stat",
        lambda key: {
            "connected": True,
            "data_valid": True,
            "has_ever_connected": True,
            "lat": 20.5,
            "lon": -99.5,
            "alt": 1000.0,
            "hdg": 90.0,
            "spd": 80.0,
            "local_time_sec": 36000.0,
            "pressure_alt": 5000.0,
            "sun_pitch": 20.0,
            "timestamp": getortho.time.time(),
        }
        if key == "flight_state"
        else 0,
    )
    monkeypatch.setattr(
        getortho, "_shared_flight_state_last_poll", 0.0
    )
    monkeypatch.setattr(
        getortho, "_shared_flight_state_allowed", False
    )

    assert getortho.is_prefetch_runtime_allowed() is True
    assert getortho.datareftracker.lat == 20.5
    assert getortho.datareftracker.spd == 80.0


def test_live_admission_is_not_bounded_by_prefetch_capacity(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: False
    )
    getter = getortho.ChunkGetter(0)

    for index in range(2_000):
        assert getter.submit(FakeChunk(f"live-{index}")) is True

    assert getter.queue_depths()["live"] == 2_000


def test_prefetch_queue_capacity_is_independent_from_admission_burst(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: True
    )
    monkeypatch.setattr(
        getortho.CFG.autoortho, "prefetch_max_chunks", 32
    )
    getter = getortho.ChunkGetter(0)

    capacity = getter._prefetch_queue_capacity
    accepted = [
        getter.submit(
            FakeChunk(f"prefetch-{index}", prefetch=True, priority=100)
        )
        for index in range(capacity + 1)
    ]

    assert capacity != 32
    assert accepted.count(True) == capacity
    assert accepted[-1] is False
    assert getter.submit(FakeChunk("live")) is True
    assert getter.queue_depths() == {
        "live": 1,
        "prefetch": capacity,
    }


def test_live_work_is_selected_before_prefetch(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: True
    )
    getter = getortho.ChunkGetter(0)
    prefetch = FakeChunk("prefetch", prefetch=True, priority=100)
    live = FakeChunk("live")
    assert getter.submit(prefetch)
    assert getter.submit(live)

    work_queue, item = getter._get_next_work()

    assert work_queue is getter.live_queue
    assert item[0] is live
    assert getter.queue_depths()["prefetch"] == 1


def test_paused_prefetch_is_not_dequeued_or_requeued(monkeypatch):
    allowed = {"value": True}
    monkeypatch.setattr(
        getortho,
        "is_prefetch_runtime_allowed",
        lambda: allowed["value"],
    )
    getter = getortho.ChunkGetter(0)
    prefetch = FakeChunk("prefetch", prefetch=True, priority=100)
    assert getter.submit(prefetch)
    allowed["value"] = False

    work_queue, item = getter._get_next_work()

    assert work_queue is None
    assert item is None
    assert getter.queue_depths()["prefetch"] == 1


def test_live_duplicate_promotes_queued_prefetch(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: True
    )
    getter = getortho.ChunkGetter(0)
    queued = FakeChunk("same", prefetch=True, priority=100)
    live_waiter = FakeChunk("same", prefetch=False, priority=0)
    assert getter.submit(queued)

    assert getter.submit(live_waiter)

    assert queued.prefetch is False
    assert getter.queue_depths() == {"live": 1, "prefetch": 0}
    _priority, _args, _kwargs = getter.live_queue.get_nowait()
    assert _priority is queued


def test_cancelling_prefetch_does_not_remove_live_work(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: True
    )
    getter = getortho.ChunkGetter(0)
    prefetch = FakeChunk("prefetch", prefetch=True, priority=100)
    live = FakeChunk("live")
    assert getter.submit(prefetch)
    assert getter.submit(live)

    assert getter.cancel_prefetch_work("live pressure") == 1

    assert prefetch.cancelled is True
    assert live.cancelled is False
    assert getter.queue_depths() == {"live": 1, "prefetch": 0}


def test_strict_target_disables_live_lower_zoom_fallback(monkeypatch):
    monkeypatch.setattr(
        getortho.CFG.autoortho,
        "prefetch_quality_mode",
        "strict_target",
        raising=False,
    )
    tile = getortho.Tile.__new__(getortho.Tile)
    assert tile._get_fallback_level() == 0
    assert tile.get_fallback_level() == 0


def test_predictive_builder_rejects_degraded_target_data():
    ready = FakeChunk("ready")
    ready.data = b"exact"
    ready.ready.set()
    missing = FakeChunk("missing")
    missing.ready.set()

    class FakeTile:
        id = "degraded"
        max_zoom = 17
        chunks = {17: [ready, missing]}

    builder = getortho.BackgroundDDSBuilder(None)
    assert builder.submit(FakeTile()) is False
    assert builder.queue_size == 0


def test_predictive_builder_accepts_complete_exact_target_data():
    chunks = [FakeChunk(f"ready-{index}") for index in range(2)]
    for chunk in chunks:
        chunk.data = b"exact"
        chunk.ready.set()

    class FakeTile:
        id = "exact"
        max_zoom = 17

        def __init__(self, exact_chunks):
            self.chunks = {17: exact_chunks}

    builder = getortho.BackgroundDDSBuilder(None)
    try:
        assert builder.submit(FakeTile(chunks)) is True
        assert builder.queue_size == 1
    finally:
        builder.stop()


def test_coordinator_exact_build_is_admitted_before_full_grid_materialization():
    class SparseGrid:
        logical_length = 4

        @staticmethod
        def materialized():
            return ()

    class FakeTile:
        id = "exact-from-cache"
        max_zoom = 17
        chunks = {17: SparseGrid()}

    builder = getortho.BackgroundDDSBuilder(None)
    assert builder.submit(FakeTile()) is False
    assert builder.submit(FakeTile(), exact_target=True) is True
    assert builder.queue_size == 1
    builder.stop()


def test_predictive_builder_parks_memory_without_executor_work():
    class Grid:
        logical_length = 128

    class Tile:
        max_zoom = 17

        def __init__(self, tile_id):
            self.id = tile_id
            self.chunks = {17: Grid()}

    builder = getortho.BackgroundDDSBuilder(None)
    builder._byte_budget = 64 * 1024 * 1024
    try:
        assert builder.submit(Tile("one"), exact_target=True)
        assert builder.submit(Tile("two"), exact_target=True)
        assert builder.submit(Tile("three"), exact_target=True)
        assert builder._requests["three"].state == (
            getortho._DDSBuildState.WAITING_FOR_MEMORY
        )
        assert builder._active_builds == 0
        assert builder.queue_size == 3
    finally:
        builder.stop()


def test_predictive_builder_parks_once_during_live_pressure():
    class Grid:
        logical_length = 1

        @staticmethod
        def ensure_all():
            return []

    class Tile:
        id = "pressure"
        max_zoom = 17
        chunks = {17: Grid()}
        _closed = False
        _is_live = False
        _predictive_complete_at = None

        @staticmethod
        def _clear_mm0_promotion_pin():
            return None

    builder = getortho.BackgroundDDSBuilder(None, build_interval_sec=0.01)
    for _ in range(6):
        getortho.live_pressure_controller.live_read_start()
    try:
        builder.start()
        assert builder.submit(Tile(), exact_target=True)
        deadline = time.monotonic() + 1.0
        while not builder._waiting_live and time.monotonic() < deadline:
            time.sleep(0.005)
        assert list(builder._waiting_live) == ["pressure"]
        assert builder._active_builds == 0
        assert builder._queue.qsize() == 0
    finally:
        builder.stop()
        for _ in range(6):
            getortho.live_pressure_controller.live_read_end()


def test_imminent_background_share_is_bounded_and_refilled():
    controller = getortho.LivePressureController(
        imminent_share=0.10,
        token_capacity=2,
    )
    controller.live_read_start()
    try:
        assert controller.allow_imminent(consume=True)
        assert controller.allow_imminent(consume=True)
        assert not controller.allow_imminent(consume=True)
        for _ in range(10):
            controller.note_provider_completion()
        assert controller.imminent_tokens() == 1
        assert controller.allow_imminent(consume=True)
        assert not controller.allow_imminent(consume=True)
    finally:
        controller.live_read_end()


def test_build_pressure_hysteresis_clears_while_network_remains_live():
    controller = getortho.LivePressureController(
        build_threshold=2,
        clearance_hysteresis=0.25,
    )
    stop = threading.Event()
    completed = threading.Event()
    controller.live_read_start()
    controller.live_read_start()
    waiter = threading.Thread(
        target=lambda: (
            controller.wait_until_clear(stop, kind="build"),
            completed.set(),
        )
    )
    waiter.start()
    controller.live_read_end()
    try:
        assert completed.wait(0.6)
        assert controller.is_under_pressure("network")
        assert not controller.is_under_pressure("build")
    finally:
        stop.set()
        controller.notify_state_change()
        controller.live_read_end()
        waiter.join(1.0)


def test_dsf_prefetch_reports_pressure_without_advancing_cursor(monkeypatch):
    class RejectingCoordinator:
        max_candidates = 2

        @staticmethod
        def make_key(*args):
            return args

        @staticmethod
        def publish(*args, **kwargs):
            return False

        @staticmethod
        def is_known(key):
            return False

    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: True
    )
    monkeypatch.setattr(
        getortho,
        "get_tiles_for_dsf",
        lambda path: [(1, 2, "BI", 17), (3, 4, "BI", 17)],
    )
    monkeypatch.setattr(
        getortho, "prefetch_coordinator", RejectingCoordinator()
    )
    monkeypatch.setattr(
        getortho.spatial_prefetcher,
        "_tile_cacher",
        SimpleNamespace(
            _get_target_zoom_level=lambda zoom, row, col: zoom
        ),
    )
    monkeypatch.setattr(
        getortho.spatial_prefetcher,
        "_get_maptype_filter",
        lambda: None,
    )

    result = getortho.prefetch_dsf("/tmp/+00+000.dsf")

    assert result == {
        "submitted": 0,
        "complete": False,
        "pressure": True,
        "cursor": 0,
    }


def test_dsf_prefetch_reports_runtime_pressure_as_deferred(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: False
    )

    assert getortho.prefetch_dsf("/tmp/+00+000.dsf", cursor=7) == {
        "submitted": 0,
        "complete": False,
        "pressure": True,
        "cursor": 7,
    }


def test_mark_live_checks_coordinator_even_when_tile_is_already_live(
    monkeypatch,
):
    promoted = []
    monkeypatch.setattr(
        getortho,
        "prefetch_coordinator",
        SimpleNamespace(promote_tile=lambda tile: promoted.append(tile)),
    )
    tile = getortho.Tile.__new__(getortho.Tile)
    tile._is_live = True

    tile.mark_live()

    assert promoted == [tile]

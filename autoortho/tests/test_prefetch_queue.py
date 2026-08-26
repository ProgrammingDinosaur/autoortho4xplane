import os
import sys
import threading

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


def test_prefetch_has_its_own_global_bound(monkeypatch):
    monkeypatch.setattr(
        getortho, "is_prefetch_runtime_allowed", lambda: True
    )
    monkeypatch.setattr(
        getortho.CFG.autoortho, "prefetch_max_chunks", 32
    )
    getter = getortho.ChunkGetter(0)

    accepted = [
        getter.submit(
            FakeChunk(f"prefetch-{index}", prefetch=True, priority=100)
        )
        for index in range(33)
    ]

    assert accepted.count(True) == 32
    assert accepted[-1] is False
    assert getter.submit(FakeChunk("live")) is True
    assert getter.queue_depths() == {"live": 1, "prefetch": 32}


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

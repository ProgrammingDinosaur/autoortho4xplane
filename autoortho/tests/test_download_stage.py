"""Unit tests for the asynchronous chunk download stage in ``getortho``.

These cover the split of ``Chunk.get()`` into a non-blocking
``begin_network_attempt`` / ``finish_network_attempt`` pair, and the admission
rules of ``_BrokerDownloadStage`` (bounded outstanding work with slots reserved
for live requests).  Nothing here touches the network.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aoconfig  # noqa: E402
import getortho  # noqa: E402


JPEG = b"\xff\xd8\xff" + b"\x00" * 64


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "image/jpeg"}
        self.closed = False

    def close(self):
        self.closed = True


class FakeFuture:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def exception(self):
        return self._error

    def result(self):
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture
def chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "tile_completion_tracker", None, raising=False)
    return getortho.Chunk(
        1, 2, "BI", 13, cache_dir=str(tmp_path), tile_id="tile-1"
    )


def _prepare(chunk):
    request = chunk.begin_network_attempt()
    assert isinstance(request, getortho._NetworkRequest)
    return request


# ---------------------------------------------------------------------------
# begin_network_attempt
# ---------------------------------------------------------------------------

def test_begin_network_attempt_reports_backoff_instead_of_sleeping(chunk):
    chunk.attempt = 5
    request = _prepare(chunk)

    assert request.delay == pytest.approx(0.5)
    assert chunk.attempt == 6
    assert request.url.startswith("https://t.ssl.ak.tiles.virtualearth.net/")
    assert chunk.download_started.is_set()


def test_begin_network_attempt_stops_after_max_attempts(chunk):
    chunk.attempt = getortho.MAX_TOTAL_ATTEMPTS
    outcome = chunk.begin_network_attempt()

    assert isinstance(outcome, getortho._AttemptOutcome)
    assert outcome.resolved is True
    assert chunk.permanent_failure is True
    assert chunk.failure_reason == "max_total_attempts"
    assert chunk.ready.is_set()


def test_begin_network_attempt_honours_cancellation(chunk):
    chunk.cancelled = True
    outcome = chunk.begin_network_attempt()

    assert isinstance(outcome, getortho._AttemptOutcome)
    assert outcome.resolved is True
    assert chunk.data is None


# ---------------------------------------------------------------------------
# finish_network_attempt
# ---------------------------------------------------------------------------

def test_successful_response_persists_and_marks_ready(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(request, FakeResponse(200, JPEG))

    assert outcome.resolved is True
    assert outcome.retry_request is None
    assert chunk.data == JPEG
    assert chunk.ready.is_set()
    assert chunk.permanent_failure is False


def test_non_jpeg_body_is_rejected_but_still_resolves(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(
        request, FakeResponse(200, b"<html>nope</html>")
    )

    assert outcome.resolved is True
    assert chunk.data == b""
    assert chunk.ready.is_set()


def test_permanent_status_is_terminal(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(request, FakeResponse(404))

    assert outcome.resolved is True
    assert chunk.permanent_failure is True
    assert chunk.failure_reason == "404"
    assert chunk.ready.is_set()


def test_transient_status_requests_backoff_without_sleeping(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(request, FakeResponse(429))

    assert outcome.resolved is False
    assert outcome.requeue_delay > 0
    assert chunk.permanent_failure is False
    assert chunk.retry_count == 1


def test_transient_status_gives_up_after_max_retries(chunk):
    request = _prepare(chunk)
    chunk.retry_count = getortho.MAX_TRANSIENT_RETRIES[503] - 1
    outcome = chunk.finish_network_attempt(request, FakeResponse(503))

    assert outcome.resolved is True
    assert chunk.permanent_failure is True
    assert chunk.failure_reason == "503_max_retries"


def test_apple_403_rotates_token_once_then_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "tile_completion_tracker", None, raising=False)
    resets = []

    class FakeAppleTokenService:
        generation = 7
        version = "v1"
        apple_token = "token-a"

        def reset_apple_maps_token(self, expected_generation=None):
            resets.append(expected_generation)
            FakeAppleTokenService.generation += 1
            FakeAppleTokenService.apple_token = "token-b"

    monkeypatch.setattr(
        getortho, "apple_token_service", FakeAppleTokenService(), raising=False
    )
    chunk = getortho.Chunk(3, 4, "APPLE", 14, cache_dir=str(tmp_path))

    request = _prepare(chunk)
    assert "token-a" in request.url

    outcome = chunk.finish_network_attempt(request, FakeResponse(403))
    assert resets == [7]
    assert outcome.resolved is False
    retry = outcome.retry_request
    assert retry is not None
    assert "token-b" in retry.url
    assert retry.apple_retried is True
    assert retry.delay == 0.0

    # A second 403 on the retry must not rotate the token again.
    outcome = chunk.finish_network_attempt(retry, FakeResponse(403))
    assert resets == [7]
    assert outcome.retry_request is None
    assert outcome.resolved is False


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------

def test_cancellation_error_is_terminal_and_silent(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(
        request, error=getortho.BrokerCancelledError("cancelled")
    )

    assert outcome.resolved is True
    assert outcome.requeue_delay == 0.0
    assert outcome.retry_request is None
    assert chunk.cancelled is True
    assert chunk.ready.is_set()


def test_connection_error_requests_backoff(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(
        request, error=getortho.requests.exceptions.ConnectionError("boom")
    )

    assert outcome.resolved is False
    assert outcome.requeue_delay > 0
    assert chunk.cancelled is False


def test_unexpected_error_does_not_backoff(chunk):
    request = _prepare(chunk)
    outcome = chunk.finish_network_attempt(request, error=ValueError("weird"))

    assert outcome.resolved is False
    assert outcome.requeue_delay == 0.0


# ---------------------------------------------------------------------------
# Getter finalisation
# ---------------------------------------------------------------------------

class _StubGetter(getortho.Getter):
    def __init__(self):
        self.submitted = []
        self.abandoned = []
        self._inflight_objs = set()
        self._inflight_objs_lock = threading.Lock()

    def submit(self, obj, *args, **kwargs):
        self.submitted.append(obj)
        return True

    def _abandon_duplicate_waiters(self, obj):
        self.abandoned.append(obj)


def test_finalize_does_not_resubmit_cancelled_chunks(chunk):
    getter = _StubGetter()
    chunk.cancelled = True
    chunk.in_flight = True
    getter._inflight_objs.add(chunk)

    assert getter._finalize_attempt(chunk, (), {}, False) is False
    assert getter.submitted == []
    assert chunk.in_flight is False
    assert chunk not in getter._inflight_objs


def test_finalize_does_not_resubmit_permanent_failures(chunk):
    getter = _StubGetter()
    chunk.permanent_failure = True

    assert getter._finalize_attempt(chunk, (), {}, False) is False
    assert getter.submitted == []


def test_finalize_resubmits_transient_failures(chunk):
    getter = _StubGetter()
    chunk.in_flight = True

    assert getter._finalize_attempt(chunk, (), {}, False) is True
    assert getter.submitted == [chunk]


def test_finalize_abandons_waiters_when_resubmit_is_refused(chunk):
    class _RefusingGetter(_StubGetter):
        def submit(self, obj, *args, **kwargs):
            return False

    getter = _RefusingGetter()
    assert getter._finalize_attempt(chunk, (), {}, False) is False
    assert getter.abandoned == [chunk]


# ---------------------------------------------------------------------------
# Stage admission
# ---------------------------------------------------------------------------

class _RecordingGetter:
    def __init__(self):
        self.settled = []
        self.abandoned = []

    def _settle_attempt(self, obj, args, kwargs, resolved, error=None,
                        quiet=False):
        self.settled.append((obj, resolved, error, quiet))

    def _abandon_duplicate_waiters(self, obj):
        self.abandoned.append(obj)
        obj.cancelled = True


def _make_stage(max_outstanding=8):
    getter = _RecordingGetter()
    stage = getortho._BrokerDownloadStage(getter, max_outstanding, workers=1)
    return getter, stage


def _fake_chunk(prefetch=False, priority=0):
    obj = getortho.Chunk.__new__(getortho.Chunk)
    obj.prefetch = prefetch
    obj.priority = priority
    return obj


def test_stage_reserves_capacity_for_live_requests():
    _, stage = _make_stage(max_outstanding=8)
    try:
        # 8 slots, 2 reserved => background may take at most 6.
        for _ in range(6):
            assert stage._try_admit(_fake_chunk(prefetch=True)) is True
        assert stage._try_admit(_fake_chunk(prefetch=True)) is False

        # Live work can still use the reserved remainder.
        assert stage._try_admit(_fake_chunk(priority=0)) is True
        assert stage._try_admit(_fake_chunk(priority=0)) is True
        assert stage._try_admit(_fake_chunk(priority=0)) is False
        assert stage.outstanding() == 8
    finally:
        stage.stop(timeout=2.0)


def test_stage_releases_slots():
    _, stage = _make_stage(max_outstanding=4)
    try:
        obj = _fake_chunk(prefetch=True)
        assert stage._try_admit(obj) is True
        assert stage.outstanding() == 1
        stage._release(obj)
        assert stage.outstanding() == 0
    finally:
        stage.stop(timeout=2.0)


def test_stage_classifies_healing_priority_as_background():
    _, stage = _make_stage(max_outstanding=4)
    try:
        assert stage._is_background(
            _fake_chunk(priority=getortho.PRIORITY_NETWORK_HEALING)
        ) is True
        assert stage._is_background(
            _fake_chunk(priority=getortho.PRIORITY_LIVE)
        ) is False
    finally:
        stage.stop(timeout=2.0)


def test_stage_declines_unknown_call_shapes():
    getter, stage = _make_stage(max_outstanding=4)
    try:
        obj = _fake_chunk()
        assert stage.dispatch(obj, ("positional",), {}, 0) is False
        assert stage.dispatch(obj, (), {"session": object()}, 0) is False
        assert stage.outstanding() == 0
        assert getter.settled == []
    finally:
        stage.stop(timeout=2.0)


def test_stage_scheduler_runs_deferred_work():
    _, stage = _make_stage(max_outstanding=4)
    try:
        done = threading.Event()
        stage._schedule(0.01, done.set)
        assert done.wait(timeout=5.0) is True
    finally:
        stage.stop(timeout=2.0)


def test_completion_exception_releases_admission_and_settles():
    getter, stage = _make_stage(max_outstanding=4)
    obj = _fake_chunk()
    obj.tile_id = "tile"
    obj.chunk_id = "chunk"
    obj._broker_request_id = "request"
    obj.finish_network_attempt = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("token refresh failed")
    )
    state = getortho._AsyncDownload(
        obj,
        (),
        {},
        0,
        object(),
    )
    state.started_at = getortho.time.monotonic()

    try:
        assert stage._try_admit(obj)
        stage._apply_result(
            state,
            FakeFuture(response=FakeResponse(403)),
        )

        assert stage.outstanding() == 0
        assert len(getter.settled) == 1
        assert getter.settled[0][0] is obj
        assert getter.settled[0][1] is False
        assert isinstance(getter.settled[0][2], RuntimeError)
    finally:
        stage.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Strict admission: capacity never falls back to a synchronous download
# ---------------------------------------------------------------------------

@pytest.fixture
def healthy_broker(monkeypatch):
    """Pretend a broker client is available without opening a socket."""

    sentinel = object()
    monkeypatch.setattr(getortho, "_get_http2_client", lambda: sentinel)
    return sentinel


def _fill(stage, count, prefetch=True):
    held = []
    for _ in range(count):
        obj = _fake_chunk(prefetch=prefetch,
                          priority=getortho.PRIORITY_PREFETCH if prefetch else 0)
        assert stage._try_admit(obj) is True
        held.append(obj)
    return held


def test_dispatch_defers_instead_of_falling_back_to_sync(healthy_broker):
    getter, stage = _make_stage(max_outstanding=4)
    try:
        started = []
        stage._start = lambda obj, args, kwargs, idx: started.append(obj)
        _fill(stage, 4, prefetch=False)

        obj = _fake_chunk(priority=0)
        assert stage.dispatch(obj, (), {}, 0) is True, (
            "a full stage must keep ownership instead of forcing a "
            "synchronous request"
        )
        assert started == []
        assert stage.deferred_depth() == 1
        assert stage.outstanding() == 4
        assert getter.settled == []
    finally:
        stage.stop(timeout=2.0)


def test_release_admits_deferred_work(healthy_broker):
    _, stage = _make_stage(max_outstanding=2)
    try:
        started = []
        stage._start = lambda obj, args, kwargs, idx: started.append(obj)
        held = _fill(stage, 2, prefetch=False)

        deferred = _fake_chunk(priority=0)
        assert stage.dispatch(deferred, (), {}, 0) is True
        assert stage.deferred_depth() == 1

        stage._release(held[0])

        deadline = time.monotonic() + 5.0
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started == [deferred]
        assert stage.deferred_depth() == 0
        assert stage.outstanding() == 2
    finally:
        stage.stop(timeout=2.0)


def test_deferred_queue_serves_live_work_first(healthy_broker):
    _, stage = _make_stage(max_outstanding=4)
    try:
        started = []
        stage._start = lambda obj, args, kwargs, idx: started.append(obj)
        held = _fill(stage, 4, prefetch=False)

        background = _fake_chunk(prefetch=True,
                                 priority=getortho.PRIORITY_PREFETCH)
        live = _fake_chunk(priority=0)
        assert stage.dispatch(background, (), {}, 0) is True
        assert stage.dispatch(live, (), {}, 0) is True
        assert stage.deferred_depth() == 2

        stage._release(held[0])
        deadline = time.monotonic() + 5.0
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started == [live], "live work must overtake queued prefetch"
    finally:
        stage.stop(timeout=2.0)


def test_background_saturation_still_lets_live_work_proceed(healthy_broker):
    """Prefetch fills its whole budget; a live chunk still gets a slot."""

    _, stage = _make_stage(max_outstanding=8)
    try:
        started = []
        stage._start = lambda obj, args, kwargs, idx: started.append(obj)
        # 8 slots, 2 reserved for live => background saturates at 6.
        held = _fill(stage, 6, prefetch=True)
        assert stage._try_admit(_fake_chunk(prefetch=True)) is False

        queued_background = [
            _fake_chunk(prefetch=True, priority=getortho.PRIORITY_PREFETCH)
            for _ in range(5)
        ]
        for obj in queued_background:
            assert stage.dispatch(obj, (), {}, 0) is True
        assert stage.deferred_depth() == 5

        live = _fake_chunk(priority=0)
        assert stage.dispatch(live, (), {}, 0) is True

        deadline = time.monotonic() + 5.0
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started == [live], (
            "the reserved live slots must be usable while prefetch is "
            "saturated and queued"
        )
        assert stage.deferred_depth() == 5

        # Freeing background slots then releases queued prefetch work.
        stage._release(held[0])
        stage._release(held[1])
        deadline = time.monotonic() + 5.0
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started[1] in queued_background
    finally:
        stage.stop(timeout=2.0)


def test_deferred_overflow_requeues_quietly_instead_of_going_sync(healthy_broker):
    getter, stage = _make_stage(max_outstanding=2)
    try:
        stage._max_deferred = 1
        stage._BACKPRESSURE_TIMEOUT = 0.05
        stage._start = lambda obj, args, kwargs, idx: None
        _fill(stage, 2, prefetch=False)

        first = _fake_chunk(priority=0)
        assert stage.dispatch(first, (), {}, 0) is True
        assert stage.deferred_depth() == 1

        overflow = _fake_chunk(priority=0)
        assert stage.dispatch(overflow, (), {}, 0) is True, (
            "overflow must be requeued, never handed back for a synchronous "
            "download"
        )
        assert stage.deferred_depth() == 1
        assert getter.settled == [(overflow, False, None, True)]
    finally:
        stage.stop(timeout=2.0)


def test_dispatch_declines_when_no_broker_is_available(monkeypatch):
    getter, stage = _make_stage(max_outstanding=2)
    monkeypatch.setattr(getortho, "_get_http2_client", lambda: None)
    try:
        assert stage.dispatch(_fake_chunk(priority=0), (), {}, 0) is False
        assert getter.settled == []
    finally:
        stage.stop(timeout=2.0)


def test_stop_retires_deferred_chunks(healthy_broker):
    getter, stage = _make_stage(max_outstanding=1)
    try:
        stage._start = lambda obj, args, kwargs, idx: None
        _fill(stage, 1, prefetch=False)
        parked = _fake_chunk(priority=0)
        assert stage.dispatch(parked, (), {}, 0) is True
        assert stage.deferred_depth() == 1
    finally:
        stage.stop(timeout=2.0)

    assert getter.abandoned == [parked]
    assert getter.settled == [(parked, False, None, True)]
    assert stage.deferred_depth() == 0


def test_shutdown_race_retires_deferring_chunk(healthy_broker):
    """Stop racing an in-progress defer must not spawn a sync download."""

    getter, stage = _make_stage(max_outstanding=1)
    stage._start = lambda obj, args, kwargs, idx: None
    _fill(stage, 1, prefetch=False)
    stage.stop(timeout=2.0)

    late = _fake_chunk(priority=0)
    # dispatch() already declined (the stage is gone), but a caller that got
    # past that check must still be handled without touching the network.
    assert stage.dispatch(late, (), {}, 0) is False
    assert stage._defer(getortho._DeferredDispatch(late, (), {}, 0)) is True
    assert getter.abandoned == [late]
    assert getter.settled == [(late, False, None, True)]


def test_peak_outstanding_never_exceeds_configured_admission(healthy_broker):
    """Hammer dispatch from several threads; the bound must hold exactly."""

    max_outstanding = 6
    getter, stage = _make_stage(max_outstanding=max_outstanding)
    admitted = []
    admitted_lock = threading.Lock()

    def _fake_start(obj, args, kwargs, idx):
        with admitted_lock:
            admitted.append(obj)
        # Return the slot the way a completed download would.
        stage._release(obj)

    try:
        stage._start = _fake_start
        chunks = [_fake_chunk(priority=0) for _ in range(200)]

        def _worker(items):
            for obj in items:
                assert stage.dispatch(obj, (), {}, 0) is True

        threads = [
            threading.Thread(target=_worker, args=(chunks[i::4],))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        deadline = time.monotonic() + 10.0
        while stage.deferred_depth() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert stage.deferred_depth() == 0
        assert len(admitted) == len(chunks)
        assert stage.peak_outstanding() <= max_outstanding
        assert getter.settled == []
    finally:
        stage.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Worker pool sizing
# ---------------------------------------------------------------------------

def test_worker_count_follows_dispatch_workers_with_broker(monkeypatch):
    monkeypatch.setenv("AO_HTTP2_BROKER_ADDR", "tcp://127.0.0.1:5599")
    monkeypatch.setenv("AO_HTTP2_BROKER_TOKEN", "token")
    monkeypatch.setattr(getortho, "HTTP2Broker", object, raising=False)
    monkeypatch.setattr(
        getortho,
        "resolve_provider_setting",
        lambda name, cfg=None: {"download_dispatch_workers": 4}.get(name, 128),
    )

    assert getortho._broker_env_configured() is True
    assert getortho._resolve_getter_workers() == 4


def test_worker_count_keeps_legacy_sizing_without_broker(monkeypatch):
    monkeypatch.delenv("AO_HTTP2_BROKER_ADDR", raising=False)
    monkeypatch.delenv("AO_HTTP2_BROKER_TOKEN", raising=False)
    monkeypatch.setattr(
        getortho,
        "resolve_provider_setting",
        lambda name, cfg=None: {"download_dispatch_workers": 4}.get(name, 128),
    )
    monkeypatch.setattr(getortho.CFG, "autoortho", getortho.CFG.autoortho)
    monkeypatch.setenv("AO_RUN_MODE", "mount_worker")

    assert getortho._broker_env_configured() is False
    workers = getortho._resolve_getter_workers()
    assert workers > 4


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_provider_settings_defaults():
    from types import SimpleNamespace

    cfg = SimpleNamespace(autoortho=SimpleNamespace())
    assert aoconfig.resolve_provider_setting("provider_max_in_flight", cfg) == 128
    assert aoconfig.resolve_provider_setting("provider_max_connections", cfg) == 64
    assert aoconfig.resolve_provider_setting("download_dispatch_workers", cfg) == 4
    assert aoconfig.resolve_provider_setting("provider_queue_timeout", cfg) == 60.0


def test_legacy_aliases_apply_only_when_new_setting_is_default():
    from types import SimpleNamespace

    legacy_only = SimpleNamespace(
        autoortho=SimpleNamespace(max_concurrent_downloads=64)
    )
    assert (
        aoconfig.resolve_provider_setting("provider_max_in_flight", legacy_only)
        == 64
    )

    both = SimpleNamespace(
        autoortho=SimpleNamespace(
            max_concurrent_downloads=64, provider_max_in_flight=256
        )
    )
    assert (
        aoconfig.resolve_provider_setting("provider_max_in_flight", both) == 256
    )

    untouched = SimpleNamespace(
        autoortho=SimpleNamespace(max_concurrent_downloads=256)
    )
    assert (
        aoconfig.resolve_provider_setting("provider_max_in_flight", untouched)
        == 128
    )

    extreme_legacy = SimpleNamespace(
        autoortho=SimpleNamespace(max_concurrent_downloads=2000)
    )
    assert (
        aoconfig.resolve_provider_setting(
            "provider_max_in_flight",
            extreme_legacy,
        )
        == 256
    )


def test_provider_settings_are_clamped_and_fault_tolerant():
    from types import SimpleNamespace

    huge = SimpleNamespace(autoortho=SimpleNamespace(provider_max_in_flight=99999))
    assert aoconfig.resolve_provider_setting("provider_max_in_flight", huge) == 1024

    junk = SimpleNamespace(autoortho=SimpleNamespace(provider_max_in_flight="nope"))
    assert aoconfig.resolve_provider_setting("provider_max_in_flight", junk) == 128

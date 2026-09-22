"""End-to-end test: chunk downloads run through the async broker stage.

Proves the actual goal of the async client: with only a handful of downloader
threads, far more than that many provider requests are in flight at once, and
every chunk still reaches a correct terminal state.
"""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

httpx = pytest.importorskip("httpx", reason="httpx is required for the broker")
pytest.importorskip("zmq", reason="pyzmq is required for the broker")
pytest.importorskip("msgpack", reason="msgpack is required for the broker")

import http2_broker as hb  # noqa: E402
import getortho  # noqa: E402


JPEG = b"\xff\xd8\xff" + b"\x00" * 512

WORKERS = 4
CHUNKS = 80
# The synchronous worker pool used to clamp real concurrency at 64.
LEGACY_WORKER_CAP = 64


@pytest.fixture
def broker_env(monkeypatch):
    """Run an in-process broker and point getortho's client at it."""

    state = {"peak": 0, "live": 0, "seen": []}
    release = threading.Event()
    lock = threading.Lock()

    async def handler(request):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            state["seen"].append(str(request.url))
        try:
            # Hold every request until the test has observed peak concurrency.
            while not release.is_set():
                await asyncio.sleep(0.01)
        finally:
            with lock:
                state["live"] -= 1
        return httpx.Response(200, content=JPEG)

    broker = hb.HTTP2Broker(
        in_process=True,
        transport=httpx.MockTransport(handler),
        max_concurrency=256,
        max_connections=256,
        max_pending=256,
    )
    broker.start()
    env = broker.client_environment()
    monkeypatch.setenv("AO_HTTP2_BROKER_ADDR", env["AO_HTTP2_BROKER_ADDR"])
    monkeypatch.setenv("AO_HTTP2_BROKER_TOKEN", env["AO_HTTP2_BROKER_TOKEN"])
    monkeypatch.setattr(getortho, "_http2_client", None, raising=False)
    monkeypatch.setattr(getortho, "_http2_client_failed", False, raising=False)
    monkeypatch.setattr(getortho, "tile_completion_tracker", None, raising=False)

    try:
        yield broker, state, release
    finally:
        release.set()
        getortho._close_http2_client()
        broker.stop(timeout=5.0)


def _drain(getter, chunks, timeout=30.0):
    deadline = time.monotonic() + timeout
    for chunk in chunks:
        remaining = deadline - time.monotonic()
        assert chunk.ready.wait(timeout=max(0.0, remaining)), f"{chunk} never ready"


def test_async_stage_exceeds_worker_count_in_flight(tmp_path, broker_env):
    broker, state, release = broker_env

    getter = getortho.ChunkGetter(WORKERS)
    assert getter._async_stage is not None
    try:
        chunks = [
            getortho.Chunk(i, 0, "BI", 13, priority=0, cache_dir=str(tmp_path))
            for i in range(CHUNKS)
        ]
        for chunk in chunks:
            assert getter.submit(chunk) is True

        deadline = time.monotonic() + 20.0
        while state["peak"] <= LEGACY_WORKER_CAP and time.monotonic() < deadline:
            time.sleep(0.02)

        # The whole point: concurrency is bounded by the broker pending budget,
        # not by the number of downloader threads (previously capped at 64).
        assert state["peak"] > LEGACY_WORKER_CAP, (
            f"peak in-flight {state['peak']} did not exceed the legacy "
            f"{LEGACY_WORKER_CAP}-request cap with only {WORKERS} threads"
        )

        release.set()
        _drain(getter, chunks)

        for chunk in chunks:
            assert chunk.data == JPEG
            assert chunk.permanent_failure is False
            assert chunk.in_flight is False

        assert getter._async_stage.outstanding() == 0
    finally:
        release.set()
        getter.stop()


def test_async_stage_marks_permanent_failures(tmp_path, monkeypatch):
    async def handler(request):
        return httpx.Response(404, content=b"nope")

    broker = hb.HTTP2Broker(
        in_process=True,
        transport=httpx.MockTransport(handler),
        max_concurrency=32,
        max_connections=32,
        max_pending=64,
    )
    broker.start()
    env = broker.client_environment()
    monkeypatch.setenv("AO_HTTP2_BROKER_ADDR", env["AO_HTTP2_BROKER_ADDR"])
    monkeypatch.setenv("AO_HTTP2_BROKER_TOKEN", env["AO_HTTP2_BROKER_TOKEN"])
    monkeypatch.setattr(getortho, "_http2_client", None, raising=False)
    monkeypatch.setattr(getortho, "_http2_client_failed", False, raising=False)
    monkeypatch.setattr(getortho, "tile_completion_tracker", None, raising=False)

    getter = getortho.ChunkGetter(2)
    try:
        chunks = [
            getortho.Chunk(500 + i, 1, "BI", 13, priority=0, cache_dir=str(tmp_path))
            for i in range(8)
        ]
        for chunk in chunks:
            assert getter.submit(chunk) is True
        _drain(getter, chunks, timeout=20.0)

        for chunk in chunks:
            assert chunk.permanent_failure is True
            assert chunk.failure_reason == "404"
            assert chunk.data == b""
            assert chunk.in_flight is False
        assert getter._async_stage.outstanding() == 0
    finally:
        getter.stop()
        getortho._close_http2_client()
        broker.stop(timeout=5.0)


def test_cancellation_during_async_download_is_not_retried(tmp_path, broker_env):
    broker, state, release = broker_env

    getter = getortho.ChunkGetter(2)
    try:
        chunk = getortho.Chunk(
            900, 2, "BI", 13, priority=0, cache_dir=str(tmp_path)
        )
        assert getter.submit(chunk) is True

        deadline = time.monotonic() + 10.0
        while not chunk._broker_request_id and time.monotonic() < deadline:
            time.sleep(0.01)
        assert chunk._broker_request_id, "request was never dispatched"

        chunk.cancel()
        assert chunk.ready.wait(timeout=10.0)

        # The chunk is retired, not resubmitted, and the slot is released.
        deadline = time.monotonic() + 10.0
        while getter._async_stage.outstanding() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert getter._async_stage.outstanding() == 0
        assert chunk.cancelled is True
        assert chunk.in_flight is False
        assert chunk.in_queue is False
        assert getter.live_queue.qsize() == 0
    finally:
        release.set()
        getter.stop()


def test_stage_falls_back_to_sync_path_without_broker(tmp_path, monkeypatch):
    """No broker configured => the direct requests.Session path still works."""

    monkeypatch.delenv("AO_HTTP2_BROKER_ADDR", raising=False)
    monkeypatch.delenv("AO_HTTP2_BROKER_TOKEN", raising=False)
    monkeypatch.setattr(getortho, "_http2_client", None, raising=False)
    monkeypatch.setattr(getortho, "_http2_client_failed", False, raising=False)
    monkeypatch.setattr(getortho, "tile_completion_tracker", None, raising=False)

    calls = []

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            calls.append(url)
            return _FakeResponse()

    class _FakeResponse:
        status_code = 200
        content = JPEG
        headers = {"content-type": "image/jpeg"}

        def close(self):
            return None

    getter = getortho.ChunkGetter(1)
    try:
        # Force every worker onto the direct-session fallback.
        getter.localdata.session = FakeSession()
        chunk = getortho.Chunk(
            7, 7, "BI", 13, priority=0, cache_dir=str(tmp_path)
        )
        assert getter._dispatch_async(chunk, (), {}, 0) is False
        result = chunk.get(idx=0, session=FakeSession())
        assert result is True
        assert chunk.data == JPEG
        assert calls
    finally:
        getter.stop()


@pytest.fixture
def small_admission(monkeypatch):
    """Pin the stage to a small, easy-to-observe admission budget."""

    limit = 8
    real = getortho.resolve_provider_setting

    def _resolve(name, *args, **kwargs):
        if name == "provider_max_in_flight":
            return limit
        if name == "download_dispatch_workers":
            return 3
        return real(name, *args, **kwargs)

    monkeypatch.setattr(getortho, "resolve_provider_setting", _resolve)
    return limit


def test_peak_in_flight_never_exceeds_configured_admission(
    tmp_path, broker_env, small_admission, monkeypatch
):
    """Far more chunks than slots: the provider still sees at most the limit."""

    broker, state, release = broker_env

    sync_calls = []
    real_get = getortho._profiled_http_get
    monkeypatch.setattr(
        getortho,
        "_profiled_http_get",
        lambda *a, **kw: (sync_calls.append(a), real_get(*a, **kw))[1],
    )

    getter = getortho.ChunkGetter(WORKERS)
    assert getter._async_stage is not None
    stage = getter._async_stage
    try:
        chunks = [
            getortho.Chunk(i, 40, "BI", 13, priority=0, cache_dir=str(tmp_path))
            for i in range(120)
        ]
        for chunk in chunks:
            assert getter.submit(chunk) is True

        # Let the queue saturate, then confirm the provider never saw more
        # than the configured number of simultaneous requests.
        deadline = time.monotonic() + 20.0
        while state["peak"] < small_admission and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state["peak"] == small_admission
        assert stage.deferred_depth() > 0, "excess work must be parked, not dropped"

        release.set()
        _drain(getter, chunks, timeout=60.0)

        assert state["peak"] <= small_admission, (
            f"peak in-flight {state['peak']} exceeded the configured "
            f"admission budget {small_admission}"
        )
        assert stage.peak_outstanding() <= small_admission
        assert not sync_calls, (
            "a configured healthy broker must never fall back to a "
            "synchronous request because of capacity"
        )
        for chunk in chunks:
            assert chunk.data == JPEG
            assert chunk.permanent_failure is False
        assert stage.outstanding() == 0
        assert stage.deferred_depth() == 0
    finally:
        release.set()
        getter.stop()


def test_background_saturation_still_completes_live_chunks(
    tmp_path, broker_env, small_admission
):
    """Prefetch floods the stage; a live chunk still gets through."""

    broker, state, release = broker_env

    getter = getortho.ChunkGetter(WORKERS)
    stage = getter._async_stage
    assert stage is not None
    try:
        prefetch = [
            getortho.Chunk(
                i,
                41,
                "BI",
                13,
                priority=getortho.PRIORITY_PREFETCH,
                cache_dir=str(tmp_path),
            )
            for i in range(60)
        ]
        for chunk in prefetch:
            chunk.is_prefetch = True
            assert getter.submit(chunk) is True

        # Wait until prefetch has saturated its share of the budget.
        deadline = time.monotonic() + 20.0
        while stage.deferred_depth() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stage.deferred_depth() > 0

        live = getortho.Chunk(
            999, 41, "BI", 13, priority=0, cache_dir=str(tmp_path)
        )
        assert getter.submit(live) is True

        # The reserved live capacity means this request reaches the provider
        # even though prefetch is saturated and queued behind it.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if live._broker_request_id:
                break
            time.sleep(0.01)
        assert live._broker_request_id, "live chunk was starved by prefetch"

        release.set()
        _drain(getter, prefetch + [live], timeout=60.0)
        assert live.data == JPEG
        assert state["peak"] <= small_admission
    finally:
        release.set()
        getter.stop()

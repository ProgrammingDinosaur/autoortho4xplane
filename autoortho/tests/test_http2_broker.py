"""Unit tests for autoortho.http2_broker (the shared HTTP/2 download broker).

These tests exercise the broker entirely in "in-process" mode: the broker
server runs on a background thread of the test process (instead of a
spawned subprocess) and is handed a deterministic ``httpx.MockTransport``,
so nothing here touches the network or depends on external services.

Requires: httpx (with the optional ``h2`` extra for HTTP/2 support), pyzmq
and msgpack. If any of these are missing the whole module is skipped so the
test suite still collects cleanly.
"""

import asyncio
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

httpx = pytest.importorskip("httpx", reason="httpx is required for http2_broker")
pytest.importorskip("zmq", reason="pyzmq is required for http2_broker")
pytest.importorskip("msgpack", reason="msgpack is required for http2_broker")

import http2_broker as hb  # noqa: E402  (import after importorskip guards)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_broker(handler, **kwargs):
    """Build and start an in-process HTTP2Broker backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    broker = hb.HTTP2Broker(in_process=True, transport=transport, **kwargs)
    broker.start()
    return broker


def run_in_thread(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) on a background thread, capturing result/exception."""
    box = {}

    def _runner():
        try:
            box["result"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - want to capture any exception
            box["error"] = exc

    thread = threading.Thread(target=_runner)
    thread.start()
    return thread, box


@pytest.fixture(autouse=True)
def _cleanup_running_brokers():
    """Safety net: ensure every broker created in a test is stopped afterwards."""
    created = []
    orig_init = hb.HTTP2Broker.__init__

    def _tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created.append(self)

    hb.HTTP2Broker.__init__ = _tracking_init
    try:
        yield
    finally:
        hb.HTTP2Broker.__init__ = orig_init
        for broker in created:
            try:
                broker.stop(timeout=2.0)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Basic response compatibility
# ---------------------------------------------------------------------------

def test_response_is_duck_type_compatible():
    async def handler(request):
        return httpx.Response(200, content=b"hello world", headers={"X-Test": "1"})

    broker = make_broker(handler)
    resp = broker.get("http://example.test/tile")

    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert resp.headers.get("x-test") == "1"
    # close() must be a harmless no-op, like requests/httpx responses.
    resp.close()
    resp.close()


def test_provider_redirects_are_followed():
    requests_seen = []

    async def handler(request):
        requests_seen.append(str(request.url))
        if request.url.path == "/tile":
            return httpx.Response(
                301,
                headers={"Location": "https://example.test/imagery"},
            )
        return httpx.Response(200, content=b"redirected-tile")

    broker = make_broker(handler)
    response = broker.get("http://example.test/tile")

    assert response.status_code == 200
    assert response.content == b"redirected-tile"
    assert requests_seen == [
        "http://example.test/tile",
        "https://example.test/imagery",
    ]


def test_attached_client_does_not_stop_owner():
    async def handler(request):
        return httpx.Response(200, content=b"shared")

    owner = make_broker(handler)
    attached = hb.HTTP2Broker.connect(owner._address, owner._token)

    assert attached.get("http://example.test/tile").content == b"shared"
    attached.stop()
    assert owner.get("http://example.test/tile").content == b"shared"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_request_with_invalid_token_is_rejected():
    async def handler(request):  # pragma: no cover - should never be reached
        return httpx.Response(200, content=b"should-not-be-called")

    broker = make_broker(handler)
    # Simulate a tampered/stale client: swap in a bogus token after the
    # handshake has already succeeded with the real one.
    broker._token = "not-the-real-token"

    with pytest.raises(hb.BrokerAuthError):
        broker.get("http://example.test/secure")


def test_handshake_fails_with_wrong_token_before_start_completes():
    async def handler(request):  # pragma: no cover
        return httpx.Response(200, content=b"unused")

    transport = httpx.MockTransport(handler)
    broker = hb.HTTP2Broker(in_process=True, transport=transport, handshake_timeout=1.0)
    # Force the handshake itself to use a token that won't match the one the
    # server stores internally, by monkeypatching _handshake to send a bad
    # token, without touching internals of the running server.
    real_handshake = broker._handshake

    def _bad_handshake():
        broker._token = "corrupted-before-handshake"
        real_handshake()

    broker._handshake = _bad_handshake
    with pytest.raises(hb.BrokerStartupError):
        broker.start()


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

def test_priority_queue_services_lower_numbers_first():
    order = []
    order_lock = threading.Lock()
    gate = threading.Event()

    async def handler(request):
        url = str(request.url)
        if url.endswith("/gate"):
            while not gate.is_set():
                await asyncio.sleep(0.01)
        with order_lock:
            order.append(url.rsplit("/", 1)[-1])
        return httpx.Response(200, content=url.encode())

    broker = make_broker(handler, max_concurrency=1)

    # Occupy the single worker so later, differently-prioritized requests
    # are guaranteed to still be sitting in the priority queue together.
    t_gate, _ = run_in_thread(broker.get, "http://example.test/gate")
    time.sleep(0.2)

    t_b, _ = run_in_thread(broker.get, "http://example.test/b", priority=5)
    t_c, _ = run_in_thread(broker.get, "http://example.test/c", priority=1)
    t_d, _ = run_in_thread(broker.get, "http://example.test/d", priority=10)
    time.sleep(0.3)

    gate.set()
    for t in (t_gate, t_b, t_c, t_d):
        t.join(timeout=5)

    assert order == ["gate", "c", "b", "d"]


# ---------------------------------------------------------------------------
# Deduplication / coalescing
# ---------------------------------------------------------------------------

def test_identical_requests_are_coalesced_into_one_http_call():
    call_count = 0
    call_lock = threading.Lock()

    async def handler(request):
        nonlocal call_count
        with call_lock:
            call_count += 1
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=b"shared-body")

    broker = make_broker(handler, max_concurrency=4)

    threads = []
    boxes = []
    for _ in range(5):
        t, box = run_in_thread(broker.get, "http://example.test/dedup")
        threads.append(t)
        boxes.append(box)
    for t in threads:
        t.join(timeout=5)

    assert call_count == 1
    for box in boxes:
        assert "error" not in box
        assert box["result"].content == b"shared-body"


def test_requests_with_different_headers_are_not_coalesced():
    seen_headers = []
    seen_lock = threading.Lock()

    async def handler(request):
        with seen_lock:
            seen_headers.append(request.headers.get("x-variant"))
        return httpx.Response(200, content=b"ok")

    broker = make_broker(handler)

    broker.get("http://example.test/same-url", headers={"X-Variant": "a"})
    broker.get("http://example.test/same-url", headers={"X-Variant": "b"})

    assert sorted(seen_headers) == ["a", "b"]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_queued_request_never_reaches_transport():
    calls = []
    calls_lock = threading.Lock()
    gate = threading.Event()

    async def handler(request):
        url = str(request.url)
        if url.endswith("/gate"):
            while not gate.is_set():
                await asyncio.sleep(0.01)
        with calls_lock:
            calls.append(url)
        return httpx.Response(200, content=b"ok")

    broker = make_broker(handler, max_concurrency=1)

    t_gate, _ = run_in_thread(broker.get, "http://example.test/gate")
    time.sleep(0.2)  # gate request occupies the sole worker

    t_target, box = run_in_thread(broker.get, "http://example.test/target", request_id="target-req")
    time.sleep(0.2)  # target request is now sitting in the priority queue

    broker.cancel("target-req")
    time.sleep(0.2)
    gate.set()

    t_gate.join(timeout=5)
    t_target.join(timeout=5)

    assert isinstance(box.get("error"), hb.BrokerCancelledError)
    assert all(not url.endswith("/target") for url in calls)


def test_cancel_one_waiter_leaves_other_waiter_unaffected():
    calls = []
    calls_lock = threading.Lock()

    async def handler(request):
        with calls_lock:
            calls.append(str(request.url))
        await asyncio.sleep(0.3)
        return httpx.Response(200, content=b"shared")

    broker = make_broker(handler, max_concurrency=2)

    t1, box1 = run_in_thread(broker.get, "http://example.test/shared", request_id="req-1")
    t2, box2 = run_in_thread(broker.get, "http://example.test/shared", request_id="req-2")
    time.sleep(0.1)

    broker.cancel("req-1")

    t1.join(timeout=5)
    t2.join(timeout=5)

    assert isinstance(box1.get("error"), hb.BrokerCancelledError)
    assert "error" not in box2
    assert box2["result"].content == b"shared"
    assert len(calls) == 1  # still only one outbound HTTP call for both waiters


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

def test_request_times_out_when_response_takes_too_long():
    async def handler(request):
        await asyncio.sleep(2.0)
        return httpx.Response(200, content=b"too-slow")

    broker = make_broker(handler)

    start = time.monotonic()
    with pytest.raises(hb.BrokerTimeoutError):
        broker.get(
            "http://example.test/slow",
            timeout=hb.RequestTimeout(connect=0.1, read=0.1, write=0.1, pool=0.1),
        )
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # must not wait for the full 2s handler delay


def test_queue_wait_does_not_consume_network_timeout():
    gate = threading.Event()

    async def handler(request):
        if request.url.path == "/gate":
            while not gate.is_set():
                await asyncio.sleep(0.01)
        return httpx.Response(200, content=request.url.path.encode())

    broker = make_broker(
        handler,
        max_concurrency=1,
        queue_timeout=3.0,
    )
    gate_future = broker.submit_async(
        "http://example.test/gate",
        timeout=hb.RequestTimeout(
            connect=5.0,
            read=5.0,
            write=5.0,
            pool=5.0,
        ),
    )
    time.sleep(0.1)
    queued = broker.submit_async(
        "http://example.test/queued",
        timeout=hb.RequestTimeout(
            connect=0.1,
            read=0.1,
            write=0.1,
            pool=0.1,
        ),
    )

    # This exceeds the queued request's complete HTTP timeout budget. It must
    # remain pending because that budget is armed only after STARTED.
    time.sleep(1.1)
    assert queued.done() is False

    gate.set()
    assert gate_future.result(timeout=5.0).content == b"/gate"
    assert queued.result(timeout=5.0).content == b"/queued"


def test_queue_timeout_is_reported_separately():
    gate = threading.Event()

    async def handler(request):
        if request.url.path == "/gate":
            while not gate.is_set():
                await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"ok")

    broker = make_broker(
        handler,
        max_concurrency=1,
        queue_timeout=0.2,
    )
    first = broker.submit_async("http://example.test/gate")
    time.sleep(0.05)
    queued = broker.submit_async("http://example.test/queued")

    try:
        with pytest.raises(
            hb.BrokerTimeoutError,
            match="broker queue",
        ):
            queued.result(timeout=2.0)
    finally:
        gate.set()

    assert first.result(timeout=5.0).status_code == 200


# ---------------------------------------------------------------------------
# Bounded payload / response size
# ---------------------------------------------------------------------------

def test_oversized_request_payload_is_rejected_before_sending():
    async def handler(request):  # pragma: no cover - should never be reached
        return httpx.Response(200, content=b"unused")

    broker = make_broker(handler, max_request_bytes=128)

    huge_headers = {"X-Huge": "a" * 1000}
    with pytest.raises(hb.BrokerProtocolError):
        broker.get("http://example.test/x", headers=huge_headers)


def test_oversized_response_body_is_rejected():
    async def handler(request):
        return httpx.Response(200, content=b"x" * 4096)

    broker = make_broker(handler, max_response_bytes=64)

    with pytest.raises(hb.ResponseTooLargeError):
        broker.get("http://example.test/big")


def test_oversized_response_declared_via_content_length_is_rejected_early():
    async def handler(request):
        # Content-Length lies about a huge body up front; the real body is
        # tiny, but the broker must reject based on the declared length
        # without needing to read it.
        return httpx.Response(
            200,
            content=b"ok",
            headers={"Content-Length": str(10 * 1024 * 1024)},
        )

    broker = make_broker(handler, max_response_bytes=64)

    with pytest.raises(hb.ResponseTooLargeError):
        broker.get("http://example.test/lying-content-length")


# ---------------------------------------------------------------------------
# Startup failure / unavailable dependency
# ---------------------------------------------------------------------------

def test_startup_failure_propagates_as_broker_startup_error():
    def bad_server_factory(**kwargs):
        raise RuntimeError("simulated startup failure")

    broker = hb.HTTP2Broker(in_process=True, server_factory=bad_server_factory)
    with pytest.raises(hb.BrokerStartupError):
        broker.start()


def test_handshake_timeout_propagates_as_broker_startup_error():
    class _HangingServer:
        """Binds nothing new, never answers HELLO -- forces a handshake timeout."""

        def __init__(self, **kwargs):
            self._router = kwargs["router"]

        async def run(self):
            await asyncio.Event().wait()

    broker = hb.HTTP2Broker(
        in_process=True,
        server_factory=_HangingServer,
        handshake_timeout=0.3,
    )
    with pytest.raises(hb.BrokerStartupError):
        broker.start()


def test_server_loop_crash_is_reported_instead_of_plain_timeout():
    class _CrashingServer:
        def __init__(self, **kwargs):
            self._router = kwargs["router"]

        async def run(self):
            raise RuntimeError("simulated selector failure")

    broker = hb.HTTP2Broker(
        in_process=True,
        server_factory=_CrashingServer,
        handshake_timeout=1.0,
    )
    with pytest.raises(
        hb.BrokerStartupError,
        match="simulated selector failure",
    ):
        broker.start()
    assert broker._zmq_ctx is None


def test_windows_broker_uses_selector_event_loop(monkeypatch):
    monkeypatch.setattr(hb.sys, "platform", "win32")
    loop = hb._new_broker_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        hb._close_broker_event_loop(loop)


def test_missing_dependency_raises_broker_unavailable_error(monkeypatch):
    monkeypatch.setattr(hb, "httpx", None)
    broker = hb.HTTP2Broker()
    with pytest.raises(hb.BrokerUnavailableError):
        broker.start()


def test_transport_requires_in_process_mode():
    async def handler(request):  # pragma: no cover
        return httpx.Response(200)

    with pytest.raises(ValueError):
        hb.HTTP2Broker(transport=httpx.MockTransport(handler), in_process=False)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def test_graceful_stop_joins_server_thread_and_rejects_further_requests():
    async def handler(request):
        return httpx.Response(200, content=b"ok")

    broker = make_broker(handler)
    resp = broker.get("http://example.test/x")
    assert resp.status_code == 200

    threads_before = threading.active_count()
    broker.stop(timeout=2.0)

    # Give the daemon thread a brief moment to fully unwind after join().
    deadline = time.monotonic() + 2.0
    while threading.active_count() >= threads_before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() < threads_before

    with pytest.raises(hb.BrokerShutdownError):
        broker.get("http://example.test/x")

    # Calling stop() again must be a harmless no-op.
    broker.stop(timeout=2.0)


def test_context_manager_starts_and_stops_broker():
    async def handler(request):
        return httpx.Response(200, content=b"ctx-ok")

    transport = httpx.MockTransport(handler)
    with hb.HTTP2Broker(in_process=True, transport=transport) as broker:
        resp = broker.get("http://example.test/ctx")
        assert resp.content == b"ctx-ok"
    assert broker._started is False


# ---------------------------------------------------------------------------
# Asynchronous / pipelined client
# ---------------------------------------------------------------------------

def test_async_client_exceeds_legacy_thread_cap():
    """More than 64 requests may be outstanding without 64 blocked threads."""

    total = 96
    gate = threading.Event()
    peak = {"value": 0}
    live = {"value": 0}

    async def handler(request):
        live["value"] += 1
        peak["value"] = max(peak["value"], live["value"])
        try:
            while not gate.is_set():
                await asyncio.sleep(0.01)
        finally:
            live["value"] -= 1
        return httpx.Response(200, content=request.url.path.encode())

    broker = make_broker(
        handler,
        max_concurrency=total,
        max_connections=total,
        max_pending=total,
    )

    threads_before = threading.active_count()
    futures = [
        broker.submit_async(f"http://example.test/tile/{i}") for i in range(total)
    ]

    deadline = time.monotonic() + 10.0
    while peak["value"] < total and time.monotonic() < deadline:
        time.sleep(0.01)
    assert peak["value"] == total, f"only {peak['value']} concurrent requests"

    # The whole batch is in flight, yet no per-request client threads exist.
    assert threading.active_count() - threads_before < 8
    assert broker.pending_count() == total

    gate.set()

    for i, future in enumerate(futures):
        resp = future.result(timeout=10.0)
        assert resp.status_code == 200
        assert resp.content == f"/tile/{i}".encode()

    assert broker.pending_count() == 0
    stats = broker.stats()
    assert stats["submitted"] == total
    assert stats["completed"] == total
    assert stats["peak_pending"] == total


def test_max_pending_is_strictly_bounded():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"ok")

    broker = make_broker(handler, max_pending=4, reserved_live_slots=0)

    futures = [broker.submit_async(f"http://example.test/{i}") for i in range(4)]
    assert broker.pending_count() == 4

    with pytest.raises(hb.BrokerCapacityError):
        broker.submit_async("http://example.test/overflow")

    assert broker.pending_count() == 4
    assert broker.stats()["rejected_capacity"] == 1

    gate.set()
    for future in futures:
        assert future.result(timeout=10.0).content == b"ok"
    assert broker.pending_count() == 0


def test_reserved_slots_keep_live_capacity_for_live_requests():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"ok")

    broker = make_broker(handler, max_pending=4, reserved_live_slots=2)

    background = [
        broker.submit_async(f"http://example.test/bg/{i}", priority=500)
        for i in range(2)
    ]
    # Background work stops at max_pending - reserved_live_slots ...
    with pytest.raises(hb.BrokerCapacityError):
        broker.submit_async("http://example.test/bg/extra", priority=500)

    # ... but the reserved slots are still available to live requests.
    live = [
        broker.submit_async(f"http://example.test/live/{i}", priority=0)
        for i in range(2)
    ]
    with pytest.raises(hb.BrokerCapacityError):
        broker.submit_async("http://example.test/live/extra", priority=0)

    stats = broker.stats()
    assert stats["pending"] == 4
    assert stats["pending_live"] == 2
    assert stats["pending_background"] == 2
    assert stats["rejected_capacity_live"] == 1

    gate.set()
    for future in background + live:
        assert future.result(timeout=10.0).status_code == 200


def test_cancelled_future_completes_exactly_once():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"late")

    broker = make_broker(handler, max_pending=8)

    future = broker.submit_async("http://example.test/slow")
    calls = []
    future.add_done_callback(calls.append)

    assert broker.cancel(future.request_id) is True
    # A redundant cancel must not settle the future a second time.
    assert broker.cancel(future.request_id) is False

    with pytest.raises(hb.BrokerCancelledError):
        future.result(timeout=5.0)
    assert future.cancelled() is True

    gate.set()
    time.sleep(0.3)

    assert len(calls) == 1
    assert broker.pending_count() == 0
    assert broker.stats()["cancelled"] == 1


def test_done_callback_runs_once_per_future_under_load():
    async def handler(request):
        return httpx.Response(200, content=b"ok")

    broker = make_broker(handler, max_pending=64)

    counts = {}
    lock = threading.Lock()

    def _record(future):
        with lock:
            counts[future.request_id] = counts.get(future.request_id, 0) + 1

    futures = []
    for i in range(64):
        future = broker.submit_async(f"http://example.test/{i}")
        future.add_done_callback(_record)
        futures.append(future)

    for future in futures:
        future.result(timeout=10.0)

    time.sleep(0.2)
    assert len(counts) == 64
    assert set(counts.values()) == {1}


def test_async_timeout_settles_future_with_timeout_error():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"never")

    broker = make_broker(handler, max_pending=4)
    future = broker.submit_async(
        "http://example.test/timeout",
        timeout=hb.RequestTimeout(connect=0.1, read=0.1, write=0.1, pool=0.1),
    )

    with pytest.raises(hb.BrokerTimeoutError):
        future.result(timeout=10.0)
    assert broker.pending_count() == 0

    gate.set()


def test_blocking_get_still_works_alongside_async_submissions():
    async def handler(request):
        return httpx.Response(200, content=request.url.path.encode())

    broker = make_broker(handler, max_pending=32)

    pipelined = [broker.submit_async(f"http://example.test/a/{i}") for i in range(8)]
    resp = broker.get("http://example.test/sync")

    assert resp.status_code == 200
    assert resp.content == b"/sync"
    for i, future in enumerate(pipelined):
        assert future.result(timeout=10.0).content == f"/a/{i}".encode()
    assert broker.pending_count() == 0


def test_shutdown_settles_outstanding_futures():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"never")

    broker = make_broker(handler, max_pending=8)
    futures = [broker.submit_async(f"http://example.test/{i}") for i in range(4)]

    gate.set()
    broker.stop(timeout=5.0)

    for future in futures:
        assert future.done() is True
        assert future.exception() is not None


# ---------------------------------------------------------------------------
# Multipart (low-copy) IPC
# ---------------------------------------------------------------------------

def test_reply_frames_keep_body_out_of_msgpack():
    """The metadata frame must not carry the JPEG bytes."""
    payload = b"\xff\xd8\xff\xe0" + b"J" * 4096

    async def handler(request):
        return httpx.Response(200, content=payload)

    captured = []
    orig_handle = hb._ClientDispatcher._handle_reply

    def _spy(self, reply, body=None):
        captured.append((dict(reply), body))
        return orig_handle(self, reply, body)

    hb._ClientDispatcher._handle_reply = _spy
    try:
        broker = make_broker(handler)
        resp = broker.get("http://example.test/tile.jpg")
    finally:
        hb._ClientDispatcher._handle_reply = orig_handle

    assert resp.content == payload
    assert captured, "no reply was observed"
    reply, body = captured[-1]
    assert body == payload
    assert "content" not in reply
    assert reply["content_length"] == len(payload)


def test_empty_body_round_trips_as_empty_frame():
    async def handler(request):
        return httpx.Response(204, content=b"")

    broker = make_broker(handler)
    resp = broker.get("http://example.test/empty")

    assert resp.status_code == 204
    assert resp.content == b""


def test_build_response_accepts_legacy_embedded_content():
    """Older servers embedded the body in msgpack; the client still accepts it."""
    dispatcher = hb._ClientDispatcher.__new__(hb._ClientDispatcher)
    dispatcher._max_response_bytes = hb.DEFAULT_MAX_RESPONSE_BYTES

    resp = dispatcher._build_response(
        {"type": "RESPONSE", "id": "x", "status_code": 200, "headers": {},
         "content": b"legacy", "content_length": 6},
        None,
    )
    assert resp.content == b"legacy"


def test_build_response_rejects_truncated_body():
    dispatcher = hb._ClientDispatcher.__new__(hb._ClientDispatcher)
    dispatcher._max_response_bytes = hb.DEFAULT_MAX_RESPONSE_BYTES

    with pytest.raises(hb.BrokerProtocolError):
        dispatcher._build_response(
            {"type": "RESPONSE", "id": "x", "status_code": 200, "headers": {},
             "content_length": 99},
            b"short",
        )


def test_build_response_enforces_client_size_bound():
    dispatcher = hb._ClientDispatcher.__new__(hb._ClientDispatcher)
    dispatcher._max_response_bytes = 8

    with pytest.raises(hb.ResponseTooLargeError):
        dispatcher._build_response(
            {"type": "RESPONSE", "id": "x", "status_code": 200, "headers": {},
             "content_length": 16},
            b"0123456789abcdef",
        )


def test_large_body_survives_multipart_round_trip():
    payload = bytes(range(256)) * 2048  # 512 KiB

    async def handler(request):
        return httpx.Response(200, content=payload)

    broker = make_broker(handler)
    resp = broker.get("http://example.test/big.jpg")

    assert resp.content == payload
    assert len(resp.content) == len(payload)


def test_coalescing_still_delivers_body_to_every_waiter():
    gate = threading.Event()
    payload = b"shared-body" * 512

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=payload)

    broker = make_broker(handler, max_pending=16)
    futures = [broker.submit_async("http://example.test/same.jpg") for _ in range(5)]
    time.sleep(0.1)
    gate.set()

    for future in futures:
        assert future.result(timeout=10.0).content == payload


def test_server_response_bound_still_enforced_over_multipart():
    async def handler(request):
        return httpx.Response(200, content=b"x" * 4096)

    broker = make_broker(handler, max_response_bytes=1024)

    with pytest.raises(hb.ResponseTooLargeError):
        broker.get("http://example.test/toobig")


# ---------------------------------------------------------------------------
# Adaptive per-origin concurrency: controller unit tests
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _controller(**kwargs):
    clock = _FakeClock()
    kwargs.setdefault("initial", 4)
    kwargs.setdefault("minimum", 2)
    kwargs.setdefault("maximum", 16)
    kwargs.setdefault("success_threshold", 3)
    kwargs.setdefault("cooldown", 1.0)
    return hb._AdaptiveConcurrencyController(clock=clock, **kwargs), clock


def test_controller_starts_at_ceiling_when_initial_is_zero():
    ctl, _ = _controller(initial=0)
    assert ctl.limit_for("https://a.test") == 16


def test_controller_additive_increase_after_sustained_success():
    ctl, _ = _controller()
    origin = "https://tiles.test"

    assert ctl.limit_for(origin) == 4
    for _ in range(2):
        ctl.on_outcome(origin, status_code=200)
    assert ctl.limit_for(origin) == 4, "must not grow before the threshold"

    ctl.on_outcome(origin, status_code=200)
    assert ctl.limit_for(origin) == 5

    for _ in range(3):
        ctl.on_outcome(origin, status_code=200)
    assert ctl.limit_for(origin) == 6


def test_controller_never_exceeds_maximum():
    ctl, _ = _controller(initial=15, maximum=16, success_threshold=1)
    origin = "https://tiles.test"
    for _ in range(20):
        ctl.on_outcome(origin, status_code=200)
    assert ctl.limit_for(origin) == 16


@pytest.mark.parametrize("status", [429, 500, 503])
def test_controller_multiplicative_decrease_on_overload(status):
    ctl, clock = _controller(initial=10, decrease_factor=0.5)
    origin = "https://tiles.test"

    ctl.on_outcome(origin, status_code=status)
    assert ctl.limit_for(origin) == 5

    clock.advance(2.0)
    ctl.on_outcome(origin, status_code=status)
    assert ctl.limit_for(origin) == 2


def test_controller_decreases_on_timeout_and_transport_error():
    ctl, clock = _controller(initial=10, decrease_factor=0.5)
    origin = "https://tiles.test"

    assert ctl.on_outcome(origin, error=hb.BrokerTimeoutError("slow")) == "throttle"
    assert ctl.limit_for(origin) == 5

    clock.advance(2.0)
    assert ctl.on_outcome(
        origin, error=httpx.ConnectError("boom")
    ) == "throttle"
    assert ctl.limit_for(origin) == 2


@pytest.mark.parametrize("status", [403, 410])
def test_controller_ignores_provider_policy_statuses(status):
    ctl, _ = _controller(initial=8)
    origin = "https://tiles.test"

    for _ in range(10):
        assert ctl.on_outcome(origin, status_code=status) == "neutral"
    assert ctl.limit_for(origin) == 8


def test_controller_cooldown_collapses_a_failure_burst():
    ctl, clock = _controller(initial=16, decrease_factor=0.5, cooldown=5.0)
    origin = "https://tiles.test"

    for _ in range(6):
        ctl.on_outcome(origin, status_code=503)
    assert ctl.limit_for(origin) == 8, "burst must produce a single decrease"

    clock.advance(5.0)
    ctl.on_outcome(origin, status_code=503)
    assert ctl.limit_for(origin) == 4


def test_controller_floor_protects_low_volume_origins():
    ctl, clock = _controller(initial=8, minimum=3, decrease_factor=0.5)
    origin = "https://tiles.test"
    for _ in range(10):
        ctl.on_outcome(origin, status_code=503)
        clock.advance(2.0)
    assert ctl.limit_for(origin) == 3


def test_controller_recovers_after_a_decrease():
    ctl, _ = _controller(initial=8, decrease_factor=0.5, success_threshold=2)
    origin = "https://tiles.test"

    ctl.on_outcome(origin, status_code=503)
    assert ctl.limit_for(origin) == 4

    for _ in range(4):
        ctl.on_outcome(origin, status_code=200)
    assert ctl.limit_for(origin) == 6


def test_controller_keeps_origins_independent():
    ctl, _ = _controller(initial=8, decrease_factor=0.5)
    bad, good = "https://bad.test", "https://good.test"

    ctl.on_outcome(bad, status_code=503)
    assert ctl.limit_for(bad) == 4
    assert ctl.limit_for(good) == 8

    assert ctl.try_acquire(good) is True
    assert ctl.active_for(bad) == 0


def test_controller_permits_are_bounded_and_released():
    ctl, _ = _controller(initial=2)
    origin = "https://tiles.test"

    assert ctl.try_acquire(origin) is True
    assert ctl.try_acquire(origin) is True
    assert ctl.try_acquire(origin) is False

    ctl.release(origin)
    assert ctl.try_acquire(origin) is True
    assert ctl.snapshot()[origin]["peak_active"] == 2


def test_controller_disabled_is_a_passthrough():
    ctl, _ = _controller(enabled=False, initial=1, maximum=16)
    origin = "https://tiles.test"
    for _ in range(10):
        assert ctl.try_acquire(origin) is True
    ctl.on_outcome(origin, status_code=503)
    assert ctl.limit_for(origin) == 16


def test_controller_rejects_invalid_decrease_factor():
    with pytest.raises(ValueError):
        hb._AdaptiveConcurrencyController(decrease_factor=1.0)
    with pytest.raises(ValueError):
        hb._AdaptiveConcurrencyController(decrease_factor=0.0)


def test_origin_key_normalizes_scheme_and_host():
    assert hb._origin_key("https://Tiles.Test:443/a/b?c=1") == "https://tiles.test:443"
    assert hb._origin_key("http://a.test/x") == "http://a.test"
    assert hb._origin_key("not a url") == "unknown"


# ---------------------------------------------------------------------------
# Adaptive per-origin concurrency: end-to-end through the broker
# ---------------------------------------------------------------------------

def test_origin_concurrency_never_exceeds_limit():
    limit = 4
    gate = threading.Event()
    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    async def handler(request):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        try:
            while not gate.is_set():
                await asyncio.sleep(0.01)
        finally:
            with lock:
                state["live"] -= 1
        return httpx.Response(200, content=b"ok")

    broker = make_broker(
        handler,
        max_concurrency=32,
        max_connections=32,
        max_pending=64,
        origin_initial_concurrency=limit,
        origin_success_threshold=10_000,  # never ramp up during the test
    )
    futures = [broker.submit_async(f"http://one.test/{i}") for i in range(24)]

    deadline = time.monotonic() + 3.0
    while state["peak"] < limit and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)
    assert state["peak"] == limit

    gate.set()
    for future in futures:
        assert future.result(timeout=10.0).status_code == 200
    assert state["peak"] == limit


def test_saturated_origin_does_not_starve_another_origin():
    gate = threading.Event()
    served_fast = threading.Event()

    async def handler(request):
        if request.url.host == "slow.test":
            while not gate.is_set():
                await asyncio.sleep(0.01)
            return httpx.Response(200, content=b"slow")
        served_fast.set()
        return httpx.Response(200, content=b"fast")

    broker = make_broker(
        handler,
        max_concurrency=8,
        max_connections=8,
        max_pending=64,
        origin_initial_concurrency=2,
        origin_success_threshold=10_000,
    )
    slow = [broker.submit_async(f"http://slow.test/{i}") for i in range(24)]
    time.sleep(0.2)

    fast = broker.submit_async("http://fast.test/now")
    assert fast.result(timeout=5.0).content == b"fast"
    assert served_fast.is_set()

    gate.set()
    for future in slow:
        assert future.result(timeout=10.0).status_code == 200


def test_server_stats_reports_origin_table():
    async def handler(request):
        return httpx.Response(200, content=b"ok")

    broker = make_broker(
        handler,
        origin_initial_concurrency=5,
        origin_min_concurrency=2,
    )
    broker.get("http://stats.test/tile")

    stats = broker.server_stats(timeout=5.0)

    assert stats["adaptive"]["enabled"] is True
    assert stats["adaptive"]["initial"] == 5
    assert stats["adaptive"]["minimum"] == 2
    assert "http://stats.test" in stats["origins"]
    entry = stats["origins"]["http://stats.test"]
    assert entry["limit"] == 5
    assert entry["active"] == 0
    assert entry["peak_active"] >= 1
    assert entry["throttles"] == 0


def test_server_stats_shows_throttle_reduction():
    async def handler(request):
        return httpx.Response(503, content=b"busy")

    broker = make_broker(
        handler,
        origin_initial_concurrency=8,
        origin_min_concurrency=2,
        origin_decrease_factor=0.5,
        origin_cooldown_seconds=0.0,
    )
    for i in range(3):
        assert broker.get(f"http://busy.test/{i}").status_code == 503

    stats = broker.server_stats(timeout=5.0)
    entry = stats["origins"]["http://busy.test"]
    assert entry["throttles"] == 3
    assert entry["decreases"] >= 1
    assert entry["limit"] < 8


def test_adaptive_limit_is_clamped_to_global_max_concurrency():
    async def handler(request):
        return httpx.Response(200, content=b"ok")

    broker = make_broker(
        handler,
        max_concurrency=3,
        max_connections=3,
        origin_initial_concurrency=99,
    )
    broker.get("http://clamp.test/tile")
    stats = broker.server_stats(timeout=5.0)

    assert stats["adaptive"]["maximum"] == 3
    assert stats["origins"]["http://clamp.test"]["limit"] == 3


def test_parked_requests_are_settled_on_shutdown():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=b"ok")

    broker = make_broker(
        handler,
        max_concurrency=8,
        max_pending=32,
        origin_initial_concurrency=1,
        origin_min_concurrency=1,
    )
    futures = [broker.submit_async(f"http://park.test/{i}") for i in range(6)]
    time.sleep(0.2)

    gate.set()
    broker.stop(timeout=5.0)

    for future in futures:
        assert future.done() is True


def _wait_for_backlog(broker, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    depth = None
    while time.monotonic() < deadline:
        depth = broker.server_stats(timeout=2.0)["backlog_depth"]
        if depth == expected:
            return depth
        time.sleep(0.02)
    raise AssertionError(f"backlog stayed at {depth}, expected {expected}")


def test_saturated_origin_parks_instead_of_holding_workers():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=request.url.path.encode())

    broker = make_broker(
        handler,
        max_concurrency=8,
        max_pending=32,
        origin_initial_concurrency=1,
        origin_min_concurrency=1,
        origin_success_threshold=10_000,
    )
    futures = [broker.submit_async(f"http://park2.test/p{i}") for i in range(5)]

    _wait_for_backlog(broker, 4)
    stats = broker.server_stats(timeout=2.0)
    assert stats["origins"]["http://park2.test"]["active"] == 1
    assert stats["origins"]["http://park2.test"]["deferred"] >= 4

    gate.set()
    for future in futures:
        assert future.result(timeout=15.0).status_code == 200
    assert broker.server_stats(timeout=2.0)["backlog_depth"] == 0


def test_cancelling_parked_requests_does_not_strand_the_origin():
    gate = threading.Event()

    async def handler(request):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, content=request.url.path.encode())

    broker = make_broker(
        handler,
        max_concurrency=8,
        max_pending=32,
        origin_initial_concurrency=1,
        origin_min_concurrency=1,
        origin_success_threshold=10_000,
    )
    futures = [broker.submit_async(f"http://cancel.test/p{i}") for i in range(5)]
    _wait_for_backlog(broker, 4)

    for future in futures:
        broker.cancel(future.request_id)
    for future in futures:
        with pytest.raises(hb.BrokerCancelledError):
            future.result(timeout=5.0)

    _wait_for_backlog(broker, 0)

    gate.set()
    assert broker.get("http://cancel.test/after").content == b"/after"

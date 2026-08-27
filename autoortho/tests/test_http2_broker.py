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

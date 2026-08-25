"""Shared HTTP/2 download broker.

This module implements a small, self-contained broker that centralizes
outbound HTTP/2 GET traffic behind a single process, so that many callers
(threads, and potentially multiple worker processes) can share connection
pools, request coalescing, prioritization and bounded concurrency instead of
each opening their own client.

Architecture
------------
* The broker server (`_RouterServer` + `_BrokerCore`) runs its own asyncio
  event loop and owns a single `httpx.AsyncClient(http2=True)`. It normally
  runs inside a dedicated ``multiprocessing`` process started with the
  ``spawn`` start method (`_ProcessRuntime`), but can also run on a
  background thread inside the caller's own process (`_ThreadRuntime`) for
  tests or embedding scenarios that want to inject a fake transport.
* IPC between callers and the broker uses ``zmq``: the server binds a
  ``ROUTER`` socket on ``127.0.0.1`` (random port, loopback only). Callers
  use one ``DEALER`` socket per thread (`HTTP2Broker._get_socket`), since
  zmq sockets are not safe to share across threads.
* Messages are small dictionaries encoded with ``msgpack``. Every message
  sent to the broker must carry a per-broker-instance auth token generated
  with `secrets.token_urlsafe`, verified with a constant-time comparison.
  The token (and any header that looks like a credential) is never written
  to logs.
* Requests are coalesced by ``(method, url, normalized headers)``: if a
  second caller asks for the same resource while the first request is
  still queued or in flight, both share the single outbound HTTP call.
  Cancelling one caller's request only cancels the shared work once no
  other caller is still waiting on it.

Public surface
--------------
* `HTTP2Broker` -- client handle: `start()`, `stop()`, `get()`, `cancel()`.
* `BrokerResponse` -- minimal response object exposing ``status_code``,
  ``content``, ``headers`` and a no-op ``close()``, so it's a drop-in
  replacement anywhere a ``requests``/``httpx`` response was used.
* `RequestTimeout` -- per-request connect/read/write/pool timeout bundle.
* Exceptions: `BrokerError` and its subclasses (see below).

Testing without the network
----------------------------
``HTTP2Broker(in_process=True, transport=<httpx.AsyncBaseTransport>)`` runs
the server on a background thread of the *same* process instead of
spawning a child, so a fake/deterministic ``httpx`` transport (for example
``httpx.MockTransport``) can be handed to it directly, without pickling and
without any real network access.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import multiprocessing
import queue as _queue_module
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

try:
    import httpx
except ImportError:  # pragma: no cover - exercised via BrokerUnavailableError
    httpx = None

try:
    import h2  # noqa: F401 - validates the httpx HTTP/2 extra
except ImportError:  # pragma: no cover - production dependency validation
    h2 = None

try:
    import zmq
    import zmq.asyncio
except ImportError:  # pragma: no cover - exercised via BrokerUnavailableError
    zmq = None

try:
    import msgpack
except ImportError:  # pragma: no cover - exercised via BrokerUnavailableError
    msgpack = None


__all__ = [
    "HTTP2Broker",
    "BrokerResponse",
    "RequestTimeout",
    "BrokerError",
    "BrokerUnavailableError",
    "BrokerStartupError",
    "BrokerProtocolError",
    "BrokerAuthError",
    "BrokerTimeoutError",
    "BrokerShutdownError",
    "BrokerCancelledError",
    "ResponseTooLargeError",
]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_HANDSHAKE_TIMEOUT = 5.0
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_CONNECTIONS = 16
DEFAULT_PRIORITY = 10

# Header names (case-insensitive) that must never be written verbatim to logs.
_REDACT_HEADER_NAMES = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BrokerError(Exception):
    """Base class for all http2_broker errors."""


class BrokerUnavailableError(BrokerError):
    """A required optional dependency (httpx, pyzmq, msgpack) is missing."""


class BrokerStartupError(BrokerError):
    """The broker process/thread failed to start or complete its handshake."""


class BrokerProtocolError(BrokerError):
    """A message violated the wire protocol (malformed, oversized, unknown)."""


class BrokerAuthError(BrokerProtocolError):
    """A message was rejected because its auth token was missing/invalid."""


class BrokerTimeoutError(BrokerError):
    """A request (or the handshake) exceeded its allotted timeout."""


class BrokerShutdownError(BrokerError):
    """A request was rejected because the broker is stopped/stopping."""


class BrokerCancelledError(BrokerError):
    """A request was cancelled via `HTTP2Broker.cancel`."""


class ResponseTooLargeError(BrokerProtocolError):
    """A response body exceeded the configured maximum size."""


_ERROR_TYPE_MAP: Dict[str, type] = {
    "BrokerError": BrokerError,
    "BrokerUnavailableError": BrokerUnavailableError,
    "BrokerStartupError": BrokerStartupError,
    "BrokerProtocolError": BrokerProtocolError,
    "BrokerAuthError": BrokerAuthError,
    "BrokerTimeoutError": BrokerTimeoutError,
    "BrokerShutdownError": BrokerShutdownError,
    "BrokerCancelledError": BrokerCancelledError,
    "ResponseTooLargeError": ResponseTooLargeError,
    # Common httpx timeout exceptions collapse to a single client-side type.
    "ConnectTimeout": BrokerTimeoutError,
    "ReadTimeout": BrokerTimeoutError,
    "WriteTimeout": BrokerTimeoutError,
    "PoolTimeout": BrokerTimeoutError,
    "TimeoutException": BrokerTimeoutError,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _redact_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Return a copy of *headers* safe to include in a log message."""
    redacted: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        lowered = key.lower()
        if lowered in _REDACT_HEADER_NAMES or "token" in lowered or "secret" in lowered or "key" in lowered:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _normalize_headers(headers: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
    """Normalize headers (case-insensitive, sorted) for dedupe-key purposes."""
    if not headers:
        return ()
    return tuple(sorted((str(k).lower(), str(v)) for k, v in headers.items()))


def _dedupe_key(method: str, url: str, headers: Optional[Dict[str, str]]):
    return (str(method).upper(), str(url), _normalize_headers(headers))


def _safe_url_for_log(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.lower()
            if (
                "token" in lowered
                or "key" in lowered
                or "secret" in lowered
                or "auth" in lowered
            ):
                value = "***"
            query.append((key, value))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
        )
    except Exception:
        return "<invalid-url>"


def _require_dependencies(*, require_http2: bool = False) -> None:
    missing = []
    if httpx is None:
        missing.append("httpx")
    if zmq is None:
        missing.append("pyzmq")
    if msgpack is None:
        missing.append("msgpack")
    if require_http2 and h2 is None:
        missing.append("h2")
    if missing:
        raise BrokerUnavailableError(
            "http2_broker requires the following packages, which are not "
            "installed: " + ", ".join(missing)
        )


def _encode(obj: Dict[str, Any]) -> bytes:
    if msgpack is None:
        raise BrokerUnavailableError("msgpack is required for broker IPC but is not installed")
    return msgpack.packb(obj, use_bin_type=True)


def _decode(buf: bytes) -> Dict[str, Any]:
    if msgpack is None:
        raise BrokerUnavailableError("msgpack is required for broker IPC but is not installed")
    try:
        obj = msgpack.unpackb(buf, raw=False, strict_map_key=False)
    except Exception as exc:
        raise BrokerProtocolError(f"failed to decode broker message: {exc}") from exc
    if not isinstance(obj, dict):
        raise BrokerProtocolError("broker message envelope must be a mapping")
    return obj


@dataclass(frozen=True)
class RequestTimeout:
    """Per-request connect/read/write/pool timeout, in seconds."""

    connect: float = 5.0
    read: float = 30.0
    write: float = 10.0
    pool: float = 5.0

    def __post_init__(self):
        for name in ("connect", "read", "write", "pool"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0 or value > 600:
                raise ValueError(
                    f"{name} timeout must be finite and between 0 and 600 seconds"
                )

    def to_dict(self) -> Dict[str, float]:
        return {"connect": self.connect, "read": self.read, "write": self.write, "pool": self.pool}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RequestTimeout":
        data = data or {}
        return cls(
            connect=float(data.get("connect", 5.0)),
            read=float(data.get("read", 30.0)),
            write=float(data.get("write", 10.0)),
            pool=float(data.get("pool", 5.0)),
        )

    def total_seconds(self) -> float:
        """Approximate worst-case wall time for a single request round-trip."""
        return self.connect + self.write + self.read + self.pool


class BrokerResponse:
    """Minimal response object, duck-type compatible with requests/httpx.

    Exposes ``status_code``, ``content`` and ``headers`` attributes plus a
    no-op ``close()`` so existing call sites written against
    ``requests``/``httpx`` responses keep working unmodified.
    """

    __slots__ = ("status_code", "content", "headers")

    def __init__(self, status_code: int, content: bytes, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self.content = content or b""
        self.headers = headers or {}

    def close(self) -> None:
        # Content is already fully buffered; nothing to release.
        return None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"BrokerResponse(status_code={self.status_code}, content_len={len(self.content)})"


# ---------------------------------------------------------------------------
# Server-side: coalescing + priority queue + bounded outbound concurrency
# ---------------------------------------------------------------------------

@dataclass
class _CoalescedEntry:
    key: tuple
    method: str
    url: str
    headers: Dict[str, str]
    priority: int
    timeout: RequestTimeout
    waiters: Dict[str, bytes] = field(default_factory=dict)  # request_id -> zmq identity
    task: Optional["asyncio.Task"] = None
    cancelled: bool = False


ReplyCallback = Callable[[bytes, Dict[str, Any]], "asyncio.Future"]


class _BrokerCore:
    """Owns the httpx client, priority queue and coalescing/cancellation state."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        max_connections: int,
        max_response_bytes: int,
        reply_cb: ReplyCallback,
        transport: Any = None,
    ):
        _require_dependencies(require_http2=transport is None)
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
        client_kwargs: Dict[str, Any] = {
            "http2": transport is None,
            "limits": limits,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._max_response_bytes = max_response_bytes
        self._max_concurrency = max(1, int(max_concurrency))
        self._reply_cb = reply_cb
        self._queue: "asyncio.PriorityQueue" = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._entries: Dict[tuple, _CoalescedEntry] = {}
        self._by_request_id: Dict[str, _CoalescedEntry] = {}
        self._workers: list = []
        self._stopping = False

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker_loop()) for _ in range(self._max_concurrency)]

    async def stop(self) -> None:
        self._stopping = True
        for entry in list(self._entries.values()):
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        await self._client.aclose()

    async def submit(
        self,
        *,
        identity: bytes,
        request_id: str,
        method: str,
        url: str,
        headers: Dict[str, str],
        priority: int,
        timeout: RequestTimeout,
    ) -> None:
        if self._stopping:
            await self._reply_cb(identity, {
                "type": "ERROR", "id": request_id,
                "error": {"type": "BrokerShutdownError", "message": "broker is shutting down"},
            })
            return

        key = _dedupe_key(method, url, headers)
        entry = self._entries.get(key)
        if entry is not None and not entry.cancelled:
            entry.waiters[request_id] = identity
            self._by_request_id[request_id] = entry
            log.debug("Coalesced request %s into existing entry for %s", request_id, url)
            return

        entry = _CoalescedEntry(key=key, method=method, url=url, headers=dict(headers), priority=priority, timeout=timeout)
        entry.waiters[request_id] = identity
        self._entries[key] = entry
        self._by_request_id[request_id] = entry
        await self._queue.put((priority, next(self._seq), entry))

    async def cancel(self, request_id: Optional[str]) -> None:
        if not request_id:
            return
        entry = self._by_request_id.pop(request_id, None)
        if entry is None:
            return

        identity = entry.waiters.pop(request_id, None)
        if identity is not None:
            await self._reply_cb(identity, {
                "type": "ERROR", "id": request_id,
                "error": {"type": "BrokerCancelledError", "message": "request was cancelled"},
            })

        if entry.waiters:
            # Other callers are still waiting on this coalesced request; keep it alive.
            return

        entry.cancelled = True
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
        self._entries.pop(entry.key, None)

    async def _worker_loop(self) -> None:
        while True:
            _priority, _seq, entry = await self._queue.get()
            try:
                if entry.cancelled or not entry.waiters:
                    continue
                entry.task = asyncio.create_task(self._execute(entry))
                try:
                    await entry.task
                except asyncio.CancelledError:
                    if self._stopping:
                        raise
            except asyncio.CancelledError:
                raise
            finally:
                entry.task = None
                self._queue.task_done()

    async def _execute(self, entry: _CoalescedEntry) -> None:
        message: Optional[Dict[str, Any]] = None
        try:
            timeout = httpx.Timeout(
                connect=entry.timeout.connect,
                read=entry.timeout.read,
                write=entry.timeout.write,
                pool=entry.timeout.pool,
            )
            async with self._client.stream(entry.method, entry.url, headers=entry.headers, timeout=timeout) as resp:
                content_length = resp.headers.get("content-length")
                if content_length is not None and int(content_length) > self._max_response_bytes:
                    raise ResponseTooLargeError(
                        f"response Content-Length {content_length} exceeds bound {self._max_response_bytes}"
                    )
                chunks = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise ResponseTooLargeError(
                            f"response body exceeded bound of {self._max_response_bytes} bytes"
                        )
                    chunks.append(chunk)
                message = {
                    "type": "RESPONSE",
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "content": b"".join(chunks),
                }
        except asyncio.CancelledError:
            # Cancelled either because the last waiter unsubscribed (waiters
            # is already empty -- nobody to notify) or because the broker is
            # shutting down while waiters are still pending (notify them).
            for request_id, identity in dict(entry.waiters).items():
                self._by_request_id.pop(request_id, None)
                await self._reply_cb(identity, {
                    "type": "ERROR", "id": request_id,
                    "error": {"type": "BrokerShutdownError", "message": "broker is shutting down"},
                })
            self._entries.pop(entry.key, None)
            raise
        except Exception as exc:
            message = {"type": "ERROR", "error": {"type": type(exc).__name__, "message": str(exc)}}
        finally:
            self._entries.pop(entry.key, None)

        if message is not None:
            for request_id, identity in dict(entry.waiters).items():
                self._by_request_id.pop(request_id, None)
                out = dict(message)
                out["id"] = request_id
                await self._reply_cb(identity, out)


# ---------------------------------------------------------------------------
# Server-side: zmq ROUTER message dispatch
# ---------------------------------------------------------------------------

class _RouterServer:
    """Binds/owns the ROUTER socket and dispatches decoded messages to `_BrokerCore`."""

    def __init__(self, *, router, token: str, config: Dict[str, Any], transport: Any = None):
        self._router = router
        self._token = token
        self._max_request_bytes = int(config.get("max_request_bytes", DEFAULT_MAX_REQUEST_BYTES))
        self._stop_event = asyncio.Event()
        self._core = _BrokerCore(
            max_concurrency=config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            max_connections=config.get("max_connections", DEFAULT_MAX_CONNECTIONS),
            max_response_bytes=config.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
            reply_cb=self._send,
            transport=transport,
        )

    async def _send(self, identity: bytes, message: Dict[str, Any]) -> None:
        try:
            await self._router.send_multipart([identity, _encode(message)])
        except Exception:
            log.debug("Failed to send broker reply to client", exc_info=True)

    async def run(self) -> None:
        await self._core.start()
        try:
            while not self._stop_event.is_set():
                try:
                    identity, raw = await asyncio.wait_for(self._router.recv_multipart(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                asyncio.create_task(self._handle_message(identity, raw))
        finally:
            await self._core.stop()
            self._router.close(0)

    async def _handle_message(self, identity: bytes, raw: bytes) -> None:
        if len(raw) > self._max_request_bytes:
            log.warning("Dropping oversized broker message (%d bytes)", len(raw))
            return

        try:
            msg = _decode(raw)
        except BrokerProtocolError as exc:
            await self._send(identity, {"type": "ERROR", "error": {"type": "BrokerProtocolError", "message": str(exc)}})
            return

        token = msg.get("token")
        if not token or not isinstance(token, str) or not secrets.compare_digest(token, self._token):
            log.warning("Rejected broker message: invalid or missing auth token")
            if msg.get("type") in ("HELLO", "REQUEST"):
                await self._send(identity, {
                    "type": "ERROR", "id": msg.get("id"),
                    "error": {"type": "BrokerAuthError", "message": "invalid auth token"},
                })
            return

        mtype = msg.get("type")
        if mtype == "HELLO":
            await self._send(identity, {"type": "HELLO_ACK"})
        elif mtype == "REQUEST":
            await self._handle_request(identity, msg)
        elif mtype == "CANCEL":
            await self._core.cancel(msg.get("id"))
        elif mtype == "SHUTDOWN":
            self._stop_event.set()
        else:
            await self._send(identity, {
                "type": "ERROR", "id": msg.get("id"),
                "error": {"type": "BrokerProtocolError", "message": f"unknown message type {mtype!r}"},
            })

    async def _handle_request(self, identity: bytes, msg: Dict[str, Any]) -> None:
        try:
            request_id = str(msg["id"])
            url = str(msg["url"])
            method = str(msg.get("method", "GET"))
            headers = {str(k): str(v) for k, v in dict(msg.get("headers") or {}).items()}
            priority = int(msg.get("priority", DEFAULT_PRIORITY))
            req_timeout = RequestTimeout.from_dict(msg.get("timeout") or {})
        except (KeyError, TypeError, ValueError) as exc:
            await self._send(identity, {
                "type": "ERROR", "id": msg.get("id"),
                "error": {"type": "BrokerProtocolError", "message": f"malformed request: {exc}"},
            })
            return

        log.debug(
            "Broker request %s %s headers=%s",
            method,
            _safe_url_for_log(url),
            _redact_headers(headers),
        )
        await self._core.submit(
            identity=identity, request_id=request_id, method=method, url=url,
            headers=headers, priority=priority, timeout=req_timeout,
        )


# ---------------------------------------------------------------------------
# Server runtimes: out-of-process (production) and in-thread (tests/embedding)
# ---------------------------------------------------------------------------

def _server_config(*, max_concurrency, max_connections, max_request_bytes, max_response_bytes) -> Dict[str, Any]:
    return {
        "max_concurrency": max_concurrency,
        "max_connections": max_connections,
        "max_request_bytes": max_request_bytes,
        "max_response_bytes": max_response_bytes,
    }


def _process_entrypoint(token: str, handshake_queue, config: Dict[str, Any]) -> None:
    """Entrypoint for the spawned broker process. Must stay picklable/top-level."""
    try:
        _require_dependencies(require_http2=True)
    except BrokerUnavailableError as exc:
        handshake_queue.put(("error", str(exc)))
        return
    try:
        asyncio.run(_serve_process(token, handshake_queue, config))
    except Exception as exc:  # pragma: no cover - defensive, logged best-effort
        log.error("Broker process crashed: %s", exc)


async def _serve_process(token: str, handshake_queue, config: Dict[str, Any]) -> None:
    ctx = zmq.asyncio.Context()
    router = ctx.socket(zmq.ROUTER)
    router.setsockopt(zmq.LINGER, 0)
    try:
        port = router.bind_to_random_port("tcp://127.0.0.1")
    except Exception as exc:
        handshake_queue.put(("error", f"{type(exc).__name__}: {exc}"))
        return

    try:
        server = _RouterServer(router=router, token=token, config=config)
    except Exception as exc:
        handshake_queue.put(("error", f"{type(exc).__name__}: {exc}"))
        router.close(0)
        return

    handshake_queue.put(("ok", port))
    try:
        await server.run()
    finally:
        ctx.destroy(linger=0)


class _ProcessRuntime:
    """Runs the broker server in a dedicated spawn-safe multiprocessing process."""

    def __init__(self, *, token: str, config: Dict[str, Any]):
        self._token = token
        self._config = config
        self._mp_ctx = multiprocessing.get_context("spawn")
        self._queue = self._mp_ctx.Queue()
        self._process: Optional[multiprocessing.process.BaseProcess] = None

    def start(self, handshake_timeout: float) -> int:
        self._process = self._mp_ctx.Process(
            target=_process_entrypoint,
            args=(self._token, self._queue, self._config),
            daemon=True,
            name="http2-broker",
        )
        self._process.start()
        try:
            status, payload = self._queue.get(timeout=handshake_timeout)
        except _queue_module.Empty:
            self._force_stop(timeout=1.0)
            raise BrokerStartupError("timed out waiting for broker process to start")
        if status != "ok":
            self._force_stop(timeout=1.0)
            raise BrokerStartupError(f"broker process failed to start: {payload}")
        return int(payload)

    def stop(self, timeout: float = 5.0) -> None:
        if self._process is None:
            return
        self._process.join(timeout=timeout)
        self._force_stop(timeout=max(0.0, timeout / 2))

    def _force_stop(self, timeout: float) -> None:
        proc = self._process
        if proc is None or not proc.is_alive():
            return
        proc.terminate()
        proc.join(timeout=timeout)
        if proc.is_alive():
            log.warning("Broker process did not exit after terminate(); killing it")
            proc.kill()
            proc.join(timeout=timeout)


class _ThreadRuntime:
    """Runs the broker server on a background thread of the caller's process.

    Used for tests and for any in-process embedding scenario that wants to
    hand the broker a fake ``httpx`` transport directly (no pickling, no
    real network access required).
    """

    def __init__(self, *, token: str, config: Dict[str, Any], transport: Any = None, server_factory=None):
        self._token = token
        self._config = config
        self._transport = transport
        self._server_factory = server_factory or _RouterServer
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._zmq_ctx = None

    def start(self, handshake_timeout: float) -> int:
        ready: "_queue_module.Queue" = _queue_module.Queue()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ctx = zmq.asyncio.Context()
            self._zmq_ctx = ctx
            router = ctx.socket(zmq.ROUTER)
            router.setsockopt(zmq.LINGER, 0)
            try:
                port = router.bind_to_random_port("tcp://127.0.0.1")
            except Exception as exc:
                ready.put(("error", f"{type(exc).__name__}: {exc}"))
                loop.close()
                return
            try:
                server = self._server_factory(router=router, token=self._token, config=self._config, transport=self._transport)
            except Exception as exc:
                ready.put(("error", f"{type(exc).__name__}: {exc}"))
                router.close(0)
                loop.close()
                return
            self._server = server
            ready.put(("ok", port))
            try:
                loop.run_until_complete(server.run())
            except Exception as exc:  # pragma: no cover - defensive
                log.error("In-process broker server crashed: %s", exc)
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, name="http2-broker-inprocess", daemon=True)
        self._thread.start()
        try:
            status, payload = ready.get(timeout=handshake_timeout)
        except _queue_module.Empty:
            raise BrokerStartupError("timed out waiting for in-process broker to start")
        if status != "ok":
            raise BrokerStartupError(f"in-process broker failed to start: {payload}")
        return int(payload)

    def stop(self, timeout: float = 5.0) -> None:
        stop_event = getattr(self._server, "_stop_event", None)
        loop = self._loop
        if stop_event is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("In-process broker thread did not stop within %.1fs", timeout)


# ---------------------------------------------------------------------------
# Public client-facing broker handle
# ---------------------------------------------------------------------------

class HTTP2Broker:
    """Client handle for the shared HTTP/2 download broker.

    Typical production usage::

        broker = HTTP2Broker()
        broker.start()
        try:
            resp = broker.get("https://example.com/tile.jpg", priority=5)
        finally:
            broker.stop()

    Test/embedding usage, with no subprocess and no real network access::

        transport = httpx.MockTransport(handler)
        broker = HTTP2Broker(in_process=True, transport=transport)
        broker.start()
    """

    def __init__(
        self,
        *,
        in_process: bool = False,
        transport: Any = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        server_factory=None,
    ):
        if transport is not None and not in_process:
            raise ValueError("transport injection is only supported with in_process=True")

        self._in_process = in_process
        self._transport = transport
        self._server_factory = server_factory
        self._max_concurrency = max_concurrency
        self._max_connections = max_connections
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._handshake_timeout = handshake_timeout

        self._token = secrets.token_urlsafe(32)
        self._zmq_ctx = None
        self._runtime = None
        self._address: Optional[str] = None
        self._local = threading.local()
        self._sockets_lock = threading.Lock()
        self._sockets: list = []
        self._started = False
        self._stopped = False
        self._owns_runtime = True

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Start the broker server and complete the auth handshake.

        Raises `BrokerUnavailableError` if a required dependency is
        missing, or `BrokerStartupError` if the process/thread fails to
        start or the handshake does not complete within
        ``handshake_timeout`` seconds.
        """
        if self._started:
            return
        _require_dependencies(
            require_http2=(
                not self._in_process
                or (
                    self._transport is None
                    and self._server_factory is None
                )
            )
        )

        self._zmq_ctx = zmq.Context()
        config = _server_config(
            max_concurrency=self._max_concurrency,
            max_connections=self._max_connections,
            max_request_bytes=self._max_request_bytes,
            max_response_bytes=self._max_response_bytes,
        )

        if self._in_process:
            runtime = _ThreadRuntime(token=self._token, config=config, transport=self._transport, server_factory=self._server_factory)
        else:
            runtime = _ProcessRuntime(token=self._token, config=config)

        try:
            port = runtime.start(self._handshake_timeout)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerStartupError(f"failed to start broker: {exc}") from exc

        self._runtime = runtime
        self._address = f"tcp://127.0.0.1:{port}"

        try:
            self._handshake()
        except Exception:
            try:
                runtime.stop(timeout=1.0)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._runtime = None
            raise

        self._started = True

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the broker to shut down gracefully, then force-stop it."""
        if not self._started or self._stopped:
            return
        self._stopped = True
        if self._owns_runtime:
            try:
                sock = self._get_socket()
                sock.send(_encode({"type": "SHUTDOWN", "token": self._token}))
            except Exception:
                log.debug("Failed to send broker shutdown message", exc_info=True)

        if self._owns_runtime and self._runtime is not None:
            self._runtime.stop(timeout=timeout)

        self._close_sockets()
        self._started = False

    def client_environment(self) -> Dict[str, str]:
        if not self._started or not self._address:
            raise BrokerShutdownError("broker is not running")
        return {
            "AO_HTTP2_BROKER_ADDR": self._address,
            "AO_HTTP2_BROKER_TOKEN": self._token,
        }

    @classmethod
    def connect(cls, address: str, token: str) -> "HTTP2Broker":
        """Attach a client-only handle to an existing broker process."""
        _require_dependencies(require_http2=True)
        if not address.startswith("tcp://127.0.0.1:"):
            raise BrokerProtocolError(
                "broker clients may only connect to a loopback TCP endpoint"
            )
        if not token:
            raise BrokerAuthError("broker auth token is required")
        broker = cls()
        broker._token = token
        broker._address = address
        broker._zmq_ctx = zmq.Context()
        broker._runtime = None
        broker._owns_runtime = False
        broker._handshake()
        broker._started = True
        return broker

    # -- requests -------------------------------------------------------

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        *,
        priority: int = DEFAULT_PRIORITY,
        timeout: Optional[RequestTimeout] = None,
        request_id: Optional[str] = None,
    ) -> BrokerResponse:
        """Fetch *url*, blocking the calling thread until a response arrives.

        Lower ``priority`` values are serviced first. Identical requests
        (same URL and normalized headers) issued concurrently share a
        single outbound HTTP call. Raises a `BrokerError` subclass on
        failure (see module docstring).
        """
        if not self._started or self._stopped:
            raise BrokerShutdownError("broker is not running")

        req_timeout = timeout or RequestTimeout()
        req_id = request_id or uuid.uuid4().hex
        envelope = {
            "type": "REQUEST",
            "token": self._token,
            "id": req_id,
            "method": "GET",
            "url": url,
            "headers": dict(headers or {}),
            "priority": int(priority),
            "timeout": req_timeout.to_dict(),
        }
        payload = _encode(envelope)
        if len(payload) > self._max_request_bytes:
            raise BrokerProtocolError(
                f"request payload ({len(payload)} bytes) exceeds the maximum of {self._max_request_bytes} bytes"
            )

        sock = self._get_socket()
        sock.send(payload)

        wait_seconds = req_timeout.total_seconds() + 0.5
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        expires_at = time.monotonic() + wait_seconds
        while True:
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                self.cancel(req_id)
                raise BrokerTimeoutError(
                    "timed out waiting for broker response"
                )
            events = dict(poller.poll(timeout=max(1, int(remaining * 1000))))
            if sock not in events:
                continue
            reply = _decode(sock.recv())
            if reply.get("id") not in (None, req_id):
                log.debug("Discarding stale broker reply")
                continue
            return self._handle_reply(reply)

    def cancel(self, request_id: str) -> None:
        """Cancel a previously issued request by id (best effort, fire-and-forget)."""
        if not self._started or not request_id:
            return
        try:
            sock = self._get_socket()
            sock.send(_encode({"type": "CANCEL", "token": self._token, "id": request_id}))
        except Exception:
            log.debug("Failed to send broker cancel for %s", request_id, exc_info=True)

    # -- internals --------------------------------------------------------

    def _handshake(self) -> None:
        sock = self._zmq_ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.connect(self._address)
            sock.send(_encode({"type": "HELLO", "token": self._token}))
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            events = dict(poller.poll(timeout=int(self._handshake_timeout * 1000)))
            if sock not in events:
                raise BrokerStartupError("broker handshake timed out")
            reply = _decode(sock.recv())
            if reply.get("type") == "ERROR":
                err = reply.get("error", {})
                raise BrokerStartupError(f"broker handshake rejected: {err.get('message', 'unknown error')}")
            if reply.get("type") != "HELLO_ACK":
                raise BrokerStartupError(f"unexpected handshake reply: {reply.get('type')!r}")
        finally:
            sock.close(0)

    def _get_socket(self):
        sock = getattr(self._local, "socket", None)
        if sock is None:
            sock = self._zmq_ctx.socket(zmq.DEALER)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(self._address)
            self._local.socket = sock
            with self._sockets_lock:
                self._sockets.append(sock)
        return sock

    def _close_sockets(self) -> None:
        with self._sockets_lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            try:
                sock.close(0)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        if self._zmq_ctx is not None:
            try:
                self._zmq_ctx.destroy(linger=0)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._zmq_ctx = None

    def _handle_reply(self, reply: Dict[str, Any]) -> BrokerResponse:
        msg_type = reply.get("type")
        if msg_type == "RESPONSE":
            return BrokerResponse(
                status_code=reply.get("status_code", 0),
                content=reply.get("content", b""),
                headers=reply.get("headers", {}),
            )
        if msg_type == "ERROR":
            err = reply.get("error", {}) or {}
            etype = err.get("type", "BrokerError")
            message = err.get("message", "broker request failed")
            exc_cls = _ERROR_TYPE_MAP.get(etype, BrokerProtocolError)
            raise exc_cls(message)
        raise BrokerProtocolError(f"unexpected reply type: {msg_type!r}")

    def __enter__(self) -> "HTTP2Broker":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

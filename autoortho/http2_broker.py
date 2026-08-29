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
  ``ROUTER`` socket on ``127.0.0.1`` (random port, loopback only). Each
  `HTTP2Broker` handle owns a single ``DEALER`` socket that is only ever
  touched by its dispatcher thread (`_ClientDispatcher`), since zmq
  sockets are not safe to share across threads.
* Requests and control messages are small dictionaries encoded with
  ``msgpack`` and sent as a single frame. Replies that carry a body (a
  JPEG tile, for example) are sent as **two** frames: msgpack metadata
  (status, headers, ``content_length``) followed by the raw body. The
  payload is therefore never packed into, or unpacked out of, a msgpack
  buffer -- it travels straight from the HTTP read into the zmq message
  and back out as ``BrokerResponse.content``.
* Every message sent to the broker must carry a per-broker-instance auth
  token generated with `secrets.token_urlsafe`, verified with a
  constant-time comparison. The token (and any header that looks like a
  credential) is never written to logs.
* Requests are coalesced by ``(method, url, normalized headers)``: if a
  second caller asks for the same resource while the first request is
  still queued or in flight, both share the single outbound HTTP call.
  Cancelling one caller's request only cancels the shared work once no
  other caller is still waiting on it.

Public surface
--------------
* `HTTP2Broker` -- client handle: `start()`, `stop()`, `get()`,
  `submit_async()`, `cancel()`.
* `BrokerFuture` -- handle for an in-flight asynchronous request. Completes
  exactly once with a `BrokerResponse` or a `BrokerError`.
* `BrokerResponse` -- minimal response object exposing ``status_code``,
  ``content``, ``headers`` and a no-op ``close()``, so it's a drop-in
  replacement anywhere a ``requests``/``httpx`` response was used.
* `RequestTimeout` -- per-request connect/read/write/pool timeout bundle.
* Exceptions: `BrokerError` and its subclasses (see below).

Client concurrency model
------------------------
`HTTP2Broker` owns exactly one ``DEALER`` socket, driven by a dedicated
dispatcher thread (`_ClientDispatcher`). Callers never touch that socket:
`submit_async()` appends an envelope to a lock-protected outbox and wakes
the dispatcher through an ``inproc`` PUSH/PULL pair, then returns a
`BrokerFuture`. This decouples the number of outstanding requests from the
number of caller threads -- hundreds of requests can be in flight without
one blocked Python thread each. `get()` is implemented on top of
`submit_async()` and keeps its original blocking semantics.

Admission control is bounded: at most ``max_pending`` requests may be
outstanding, and ``reserved_live_slots`` of those are reserved for
requests whose ``priority`` is below ``live_priority_threshold``, so
prefetch/healing traffic can never consume the whole budget.

Adaptive per-origin concurrency
-------------------------------
The server additionally bounds how many requests may be *executing* per
origin (``scheme://host:port``) with an AIMD controller
(`_AdaptiveConcurrencyController`): the per-origin cap grows by
``step`` after a run of successful responses and shrinks multiplicatively
when an origin answers with 429/5xx or times out. 403/410 are provider
policy answers, not overload signals, and never shrink the cap. Requests
for a saturated origin are parked in a per-origin backlog instead of
occupying a worker, so a slow provider cannot starve the others.

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
import collections
import heapq
import itertools
import logging
import math
import multiprocessing
import queue as _queue_module
import secrets
import sys
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

try:
    from autoortho.diagnostics import profile_gauge, record_stage
except ImportError:
    try:
        from diagnostics import profile_gauge, record_stage
    except ImportError:  # pragma: no cover - standalone broker use
        def profile_gauge(_name, _value):
            return None

        def record_stage(_stage, _duration_ms, **_kwargs):
            return None

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
    "BrokerFuture",
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
    "BrokerCapacityError",
    "ResponseTooLargeError",
]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_HANDSHAKE_TIMEOUT = 10.0
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_CONNECTIONS = 16
DEFAULT_PRIORITY = 10

# Client-side admission control. `max_pending` bounds the number of request
# envelopes that may be outstanding on the single DEALER socket at once, which
# is what decouples download concurrency from the caller thread count.
DEFAULT_MAX_PENDING = 128
MAX_PENDING_LIMIT = 4096
# Requests numerically below this priority are treated as live/foreground work
# and may draw on the reserved slice of `max_pending`. getortho uses
# PRIORITY_LIVE=0..~20 for live mipmaps and >=100 for prefetch/healing.
DEFAULT_LIVE_PRIORITY_THRESHOLD = 100
# Fraction of `max_pending` that only live requests may occupy.
DEFAULT_RESERVED_LIVE_FRACTION = 0.25
# Extra wall time granted on top of the per-request timeout before the client
# gives up on a reply that the server should already have produced.
CLIENT_TIMEOUT_GRACE_SECONDS = 0.5
# Upper bound on how long the dispatcher may sit in poll() with no work, so
# stop requests and newly expired deadlines are noticed promptly.
_DISPATCHER_POLL_MS = 250

# Adaptive per-origin concurrency (server side). The controller starts at
# `initial`, adds `step` after `success_threshold` consecutive good responses
# and multiplies by `decrease_factor` when an origin signals overload
# (429/5xx/timeout), never dropping below `minimum` nor rising above the
# process-wide `max_concurrency`.
#
# `initial = 0` means "start at the ceiling": by default the controller only
# ever *reacts* to overload, so a healthy provider sees exactly the same
# concurrency it saw before adaptive control existed. Operators who prefer a
# gentle ramp can set a positive initial value.
DEFAULT_ADAPTIVE_CONCURRENCY = True
DEFAULT_ORIGIN_MIN_CONCURRENCY = 2
DEFAULT_ORIGIN_INITIAL_CONCURRENCY = 0
DEFAULT_ORIGIN_INCREASE_STEP = 1
DEFAULT_ORIGIN_SUCCESS_THRESHOLD = 8
DEFAULT_ORIGIN_DECREASE_FACTOR = 0.7
DEFAULT_ORIGIN_COOLDOWN_SECONDS = 2.0

# HTTP statuses that are provider policy answers rather than overload
# signals. They must never shrink an origin's concurrency budget.
_NEUTRAL_STATUS_CODES = frozenset({403, 410})

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
    """A request was cancelled via `HTTP2Broker.cancel` or `BrokerFuture.cancel`."""


class BrokerCapacityError(BrokerError):
    """A request was refused because the client's pending budget is full."""


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
    "BrokerCapacityError": BrokerCapacityError,
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


def _origin_key(url: str) -> str:
    """Return ``scheme://host:port`` for *url*, the adaptive-control key."""
    try:
        parts = urlsplit(url)
    except Exception:
        return "unknown"
    scheme = (parts.scheme or "https").lower()
    netloc = (parts.netloc or "").lower()
    if not netloc:
        return "unknown"
    return f"{scheme}://{netloc}"


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


def _new_broker_event_loop() -> asyncio.AbstractEventLoop:
    """Return a loop compatible with zmq.asyncio on every platform.

    Windows defaults to ProactorEventLoop, which lacks add_reader and makes
    pyzmq fail as soon as ROUTER.recv_multipart() starts unless Tornado happens
    to be installed. The broker has no need for proactor-specific subprocess
    support, so a selector loop is the direct and dependency-free solution.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def _close_broker_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass
    try:
        loop.close()
    except Exception:
        pass


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
# Client-side: asynchronous request handles
# ---------------------------------------------------------------------------

class BrokerFuture:
    """Handle for one in-flight broker request.

    A future is completed **exactly once**, by whichever of the following
    happens first: a response arrives, the broker reports an error, the
    per-request deadline expires, the caller cancels it, the broker shuts
    down, or the client dispatcher fails. Completion is enforced by the
    dispatcher, which removes the future from its pending map under a lock
    before settling it.

    Done callbacks receive the future itself and always run on the client
    dispatcher thread. They must be short and non-blocking; hand work off to
    another queue rather than performing I/O inline.
    """

    __slots__ = (
        "request_id",
        "priority",
        "reserved_live",
        "created_at",
        "deadline",
        "network_timeout",
        "started",
        "_lock",
        "_event",
        "_result",
        "_error",
        "_done",
        "_callbacks",
    )

    def __init__(
        self,
        request_id: str,
        *,
        priority: int,
        reserved_live: bool,
        deadline: float,
        network_timeout: float,
    ):
        self.request_id = request_id
        self.priority = priority
        self.reserved_live = reserved_live
        self.created_at = time.monotonic()
        self.deadline = deadline
        self.network_timeout = max(0.1, float(network_timeout))
        self.started = False
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._result: Optional[BrokerResponse] = None
        self._error: Optional[BaseException] = None
        self._done = False
        self._callbacks: Optional[List[Callable[["BrokerFuture"], Any]]] = []

    # -- completion (dispatcher side) ----------------------------------

    def _settle(
        self,
        result: Optional[BrokerResponse],
        error: Optional[BaseException],
    ) -> bool:
        """Complete this future. Returns False if it was already done."""
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._result = result
            self._error = error
            callbacks, self._callbacks = self._callbacks, None
        self._event.set()
        for callback in callbacks or ():
            self._invoke_callback(callback)
        return True

    def _invoke_callback(self, callback: Callable[["BrokerFuture"], Any]) -> None:
        try:
            callback(self)
        except Exception:
            # A misbehaving callback must never take the dispatcher thread
            # down or prevent the remaining callbacks from running.
            log.exception("Broker future callback failed for %s", self.request_id)

    # -- consumer side --------------------------------------------------

    def done(self) -> bool:
        return self._done

    def cancelled(self) -> bool:
        return self._done and isinstance(self._error, BrokerCancelledError)

    def add_done_callback(self, callback: Callable[["BrokerFuture"], Any]) -> None:
        with self._lock:
            if not self._done:
                self._callbacks.append(callback)  # type: ignore[union-attr]
                return
        self._invoke_callback(callback)

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def exception(self, timeout: Optional[float] = None) -> Optional[BaseException]:
        if not self._event.wait(timeout):
            raise BrokerTimeoutError("timed out waiting for broker response")
        return self._error

    def result(self, timeout: Optional[float] = None) -> BrokerResponse:
        """Block until completion; return the response or raise the error."""
        if not self._event.wait(timeout):
            raise BrokerTimeoutError("timed out waiting for broker response")
        if self._error is not None:
            raise self._error
        # A settled future always carries either a result or an error.
        return self._result  # type: ignore[return-value]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        state = "done" if self._done else "pending"
        return f"BrokerFuture({self.request_id}, priority={self.priority}, {state})"


class _ClientDispatcher:
    """Owns the single client DEALER socket and multiplexes requests on it.

    All socket traffic happens on `_run`'s thread. Producer threads only
    append to a lock-protected outbox/pending map and then poke an ``inproc``
    PUSH socket (guarded by its own mutex, which provides the full memory
    barrier zmq requires when a socket is used from more than one thread).
    """

    def __init__(
        self,
        *,
        ctx,
        address: str,
        cancel_factory: Callable[[str], Dict[str, Any]],
        max_pending: int,
        reserved_live_slots: int,
        live_priority_threshold: int,
        max_request_bytes: int,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self._ctx = ctx
        self._address = address
        self._cancel_factory = cancel_factory
        self._max_pending = max(1, min(MAX_PENDING_LIMIT, int(max_pending)))
        self._reserved_live_slots = max(
            0, min(self._max_pending - 1, int(reserved_live_slots))
        )
        self._live_priority_threshold = int(live_priority_threshold)
        self._max_request_bytes = int(max_request_bytes)
        self._max_response_bytes = int(max_response_bytes)

        self._lock = threading.Lock()
        self._pending: Dict[str, BrokerFuture] = {}
        self._deadlines: List[Tuple[float, str]] = []
        self._outbox: "collections.deque" = collections.deque()
        self._stopping = False
        self._failure: Optional[BaseException] = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dealer = None
        self._wake_pull = None
        self._wake_push = None
        self._wake_lock = threading.Lock()

        self._stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "timed_out": 0,
            "cancelled": 0,
            "rejected_capacity": 0,
            "rejected_capacity_live": 0,
            "peak_pending": 0,
        }

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        wake_address = f"inproc://ao-broker-wake-{uuid.uuid4().hex}"
        self._wake_pull = self._ctx.socket(zmq.PULL)
        self._wake_pull.setsockopt(zmq.LINGER, 0)
        self._wake_pull.bind(wake_address)
        self._wake_push = self._ctx.socket(zmq.PUSH)
        self._wake_push.setsockopt(zmq.LINGER, 0)
        # A single queued wake byte is enough; extra pokes may be dropped.
        self._wake_push.setsockopt(zmq.SNDHWM, 4)
        self._wake_push.connect(wake_address)

        self._dealer = self._ctx.socket(zmq.DEALER)
        self._dealer.setsockopt(zmq.LINGER, 0)
        self._dealer.setsockopt(zmq.SNDHWM, max(1024, self._max_pending * 4))
        self._dealer.setsockopt(zmq.RCVHWM, max(1024, self._max_pending * 4))
        self._dealer.connect(self._address)

        self._thread = threading.Thread(
            target=self._run,
            name="http2-broker-client",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
        self._stop_event.set()
        self._wake()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - pathological
                log.warning(
                    "Broker client dispatcher did not stop within %.1fs", timeout
                )
        # The dispatcher owns (and closes) the DEALER/PULL sockets; the PUSH
        # socket belongs to the producer side and is closed here.
        with self._wake_lock:
            push, self._wake_push = self._wake_push, None
            if push is not None:
                try:
                    push.close(0)
                except Exception:
                    log.debug("Failed to close broker wake socket", exc_info=True)
        # Anything still pending after the thread exited (or if it never
        # started) must still be settled exactly once.
        self._drain_pending(BrokerShutdownError("broker client is shutting down"))

    # -- producer API ---------------------------------------------------

    def is_live_priority(self, priority: int) -> bool:
        return int(priority) < self._live_priority_threshold

    def submit(
        self,
        envelope: Dict[str, Any],
        *,
        priority: int,
        deadline: float,
        network_timeout: float,
    ) -> BrokerFuture:
        payload = _encode(envelope)
        if len(payload) > self._max_request_bytes:
            raise BrokerProtocolError(
                f"request payload ({len(payload)} bytes) exceeds the maximum "
                f"of {self._max_request_bytes} bytes"
            )

        request_id = str(envelope["id"])
        reserved_live = self.is_live_priority(priority)
        future = BrokerFuture(
            request_id,
            priority=int(priority),
            reserved_live=reserved_live,
            deadline=deadline,
            network_timeout=network_timeout,
        )

        with self._lock:
            if self._failure is not None:
                raise BrokerShutdownError(
                    f"broker client dispatcher failed: {self._failure}"
                )
            if self._stopping:
                raise BrokerShutdownError("broker client is shutting down")
            limit = self._max_pending
            if not reserved_live:
                limit -= self._reserved_live_slots
            if len(self._pending) >= limit:
                self._stats["rejected_capacity"] += 1
                if reserved_live:
                    self._stats["rejected_capacity_live"] += 1
                raise BrokerCapacityError(
                    f"broker client has {len(self._pending)} pending requests; "
                    f"limit for this priority is {limit}"
                )
            if request_id in self._pending:
                raise BrokerProtocolError(
                    f"duplicate in-flight broker request id {request_id!r}"
                )
            self._pending[request_id] = future
            heapq.heappush(self._deadlines, (deadline, request_id))
            self._outbox.append(payload)
            self._stats["submitted"] += 1
            if len(self._pending) > self._stats["peak_pending"]:
                self._stats["peak_pending"] = len(self._pending)

        self._wake()
        return future

    def cancel(self, request_id: str) -> bool:
        """Settle *request_id* as cancelled and tell the server to drop it."""
        if not request_id:
            return False
        cancel_payload = self._cancel_payload(request_id)
        with self._lock:
            future = self._pending.pop(request_id, None)
            if cancel_payload is not None:
                self._outbox.append(cancel_payload)
            if future is not None:
                self._stats["cancelled"] += 1
        settled = False
        if future is not None:
            settled = future._settle(
                None, BrokerCancelledError("request was cancelled")
            )
        self._wake()
        return settled

    def send_control(self, message: Dict[str, Any]) -> None:
        with self._lock:
            self._outbox.append(_encode(message))
        self._wake()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            snapshot = dict(self._stats)
            snapshot["pending"] = len(self._pending)
            snapshot["pending_live"] = sum(
                1 for fut in self._pending.values() if fut.reserved_live
            )
            snapshot["max_pending"] = self._max_pending
            snapshot["reserved_live_slots"] = self._reserved_live_slots
        snapshot["pending_background"] = snapshot["pending"] - snapshot["pending_live"]
        return snapshot

    # -- internals ------------------------------------------------------

    def _cancel_payload(self, request_id: str) -> Optional[bytes]:
        try:
            return _encode(self._cancel_factory(request_id))
        except Exception:
            log.debug("Could not encode broker cancel envelope", exc_info=True)
            return None

    def _wake(self) -> None:
        with self._wake_lock:
            push = self._wake_push
            if push is None:
                return
            try:
                push.send(b"\x01", zmq.NOBLOCK)
            except zmq.Again:
                # A wake is already queued; the dispatcher will see our work.
                pass
            except zmq.ZMQError:
                log.debug("Broker wake socket unavailable", exc_info=True)

    def _run(self) -> None:
        poller = zmq.Poller()
        poller.register(self._dealer, zmq.POLLIN)
        poller.register(self._wake_pull, zmq.POLLIN)
        failure: Optional[BaseException] = None
        try:
            while not self._stop_event.is_set():
                self._flush_outbox()
                events = dict(poller.poll(timeout=self._poll_timeout_ms()))
                if self._wake_pull in events:
                    self._drain_wake()
                if self._dealer in events:
                    self._drain_replies()
                self._expire_deadlines()
                profile_gauge("broker.client_pending", len(self._pending))
            # Final flush so queued CANCEL/SHUTDOWN envelopes reach the server.
            self._flush_outbox()
        except Exception as exc:
            failure = exc
            log.error("Broker client dispatcher failed: %s", exc, exc_info=True)
        finally:
            with self._lock:
                self._stopping = True
                self._failure = failure
            for sock in (self._dealer, self._wake_pull):
                try:
                    if sock is not None:
                        sock.close(0)
                except Exception:
                    log.debug("Failed closing broker client socket", exc_info=True)
            self._dealer = None
            self._wake_pull = None
            if failure is not None:
                self._drain_pending(
                    BrokerError(f"broker client dispatcher failed: {failure}")
                )
            else:
                self._drain_pending(
                    BrokerShutdownError("broker client is shutting down")
                )

    def _poll_timeout_ms(self) -> int:
        with self._lock:
            # A non-empty outbox here means a send hit EAGAIN; retry soon but
            # do not spin the CPU while the send buffer drains.
            backlog = 5 if self._outbox else None
            deadline = self._peek_deadline_locked()
        if backlog is not None:
            return backlog
        if deadline is None:
            return _DISPATCHER_POLL_MS
        remaining_ms = int((deadline - time.monotonic()) * 1000.0)
        return max(0, min(_DISPATCHER_POLL_MS, remaining_ms))

    def _peek_deadline_locked(self) -> Optional[float]:
        while self._deadlines:
            deadline, request_id = self._deadlines[0]
            future = self._pending.get(request_id)
            if future is not None and future.deadline == deadline:
                return deadline
            heapq.heappop(self._deadlines)
        return None

    def _flush_outbox(self) -> None:
        dealer = self._dealer
        if dealer is None:
            return
        while True:
            with self._lock:
                if not self._outbox:
                    return
                payload = self._outbox.popleft()
            try:
                dealer.send(payload, zmq.NOBLOCK)
            except zmq.Again:
                # Send buffer is full; retry on the next loop iteration.
                with self._lock:
                    self._outbox.appendleft(payload)
                return

    def _drain_wake(self) -> None:
        sock = self._wake_pull
        while sock is not None:
            try:
                sock.recv(zmq.NOBLOCK)
            except zmq.Again:
                return

    def _drain_replies(self) -> None:
        dealer = self._dealer
        while dealer is not None:
            try:
                frames = dealer.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return
            if not frames:
                log.warning("Discarding empty broker reply")
                continue
            if len(frames) > 2:
                log.warning(
                    "Discarding broker reply with %d frames (expected 1 or 2)",
                    len(frames),
                )
                continue
            try:
                reply = _decode(frames[0])
            except BrokerProtocolError as exc:
                log.warning("Discarding undecodable broker reply: %s", exc)
                continue
            body = frames[1] if len(frames) == 2 else None
            self._handle_reply(reply, body)

    def _build_response(
        self, reply: Dict[str, Any], body: Optional[bytes]
    ) -> BrokerResponse:
        """Assemble a `BrokerResponse` from the metadata + body frames.

        ``body`` is ``None`` for a single-frame reply, in which case the
        payload is read from the metadata (older/embedded encoding).
        """
        content = reply.get("content", b"") if body is None else body
        if content is None:
            content = b""
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise BrokerProtocolError(
                f"broker response body has type {type(content).__name__}"
            )
        if not isinstance(content, bytes):
            content = bytes(content)
        declared = reply.get("content_length")
        if declared is not None and int(declared) != len(content):
            raise BrokerProtocolError(
                f"broker response declared {declared} bytes but carried "
                f"{len(content)}"
            )
        if len(content) > self._max_response_bytes:
            raise ResponseTooLargeError(
                f"response body ({len(content)} bytes) exceeds the client "
                f"bound of {self._max_response_bytes} bytes"
            )
        return BrokerResponse(
            status_code=reply.get("status_code", 0),
            content=content,
            headers=reply.get("headers", {}),
        )

    def _handle_reply(
        self, reply: Dict[str, Any], body: Optional[bytes] = None
    ) -> None:
        request_id = reply.get("id")
        if request_id is None:
            log.debug("Discarding broker reply without a request id")
            return
        request_id = str(request_id)

        msg_type = reply.get("type")
        if msg_type == "STARTED":
            with self._lock:
                future = self._pending.get(request_id)
                if future is None or future.started:
                    return
                future.started = True
                future.deadline = (
                    time.monotonic()
                    + future.network_timeout
                    + CLIENT_TIMEOUT_GRACE_SECONDS
                )
                heapq.heappush(
                    self._deadlines,
                    (future.deadline, request_id),
                )
                self._stats.setdefault("started", 0)
                self._stats["started"] += 1
            return

        result: Optional[BrokerResponse] = None
        error: Optional[BaseException] = None
        if msg_type == "RESPONSE":
            try:
                result = self._build_response(reply, body)
            except BrokerError as exc:
                error = exc
        elif msg_type == "ERROR":
            err = reply.get("error", {}) or {}
            exc_cls = _ERROR_TYPE_MAP.get(
                err.get("type", "BrokerError"), BrokerProtocolError
            )
            error = exc_cls(err.get("message", "broker request failed"))
        else:
            error = BrokerProtocolError(f"unexpected reply type: {msg_type!r}")

        with self._lock:
            future = self._pending.pop(request_id, None)
            if future is None:
                return
            if error is None:
                self._stats["completed"] += 1
            else:
                self._stats["failed"] += 1

        future._settle(result, error)

    def _expire_deadlines(self) -> None:
        now = time.monotonic()
        while True:
            with self._lock:
                deadline = self._peek_deadline_locked()
                if deadline is None or deadline > now:
                    return
                _deadline, request_id = heapq.heappop(self._deadlines)
                future = self._pending.pop(request_id, None)
                if future is None:
                    continue
                self._stats["timed_out"] += 1
                cancel_payload = self._cancel_payload(request_id)
                if cancel_payload is not None:
                    self._outbox.append(cancel_payload)
            future._settle(
                None,
                BrokerTimeoutError(
                    "timed out after broker started provider request"
                    if future.started
                    else "timed out waiting in broker queue"
                ),
            )

    def _drain_pending(self, error: BaseException) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    return
                _request_id, future = self._pending.popitem()
            future._settle(None, error)


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
    origin: str = "unknown"
    seq: int = 0
    waiters: Dict[str, bytes] = field(default_factory=dict)  # request_id -> zmq identity
    task: Optional["asyncio.Task"] = None
    cancelled: bool = False
    parked: bool = False
    started: bool = False
    queued_at: float = field(default_factory=time.monotonic)


ReplyCallback = Callable[..., "asyncio.Future"]


@dataclass
class _OriginState:
    """Mutable AIMD state for one origin."""

    limit: int
    active: int = 0
    peak_active: int = 0
    successes: int = 0
    throttles: int = 0
    increases: int = 0
    decreases: int = 0
    deferred: int = 0
    last_decrease: float = float("-inf")


def _classify_origin_outcome(
    status_code: Optional[int] = None,
    error: Optional[BaseException] = None,
) -> str:
    """Map one request outcome onto ``success`` / ``throttle`` / ``neutral``.

    Only genuine overload signals (429, 5xx, timeouts and transport-level
    failures) shrink an origin's budget. Provider policy answers -- 403 and
    410 in particular -- are neutral: they say nothing about how much load
    the origin can take, and penalising them would collapse concurrency for
    a provider that simply refuses some tiles.
    """
    if error is not None:
        if isinstance(error, (BrokerTimeoutError, ResponseTooLargeError)):
            return "throttle" if isinstance(error, BrokerTimeoutError) else "neutral"
        if httpx is not None:
            if isinstance(error, httpx.TimeoutException):
                return "throttle"
            if isinstance(error, httpx.TransportError):
                return "throttle"
        return "neutral"
    if status_code is None:
        return "neutral"
    status = int(status_code)
    if status in _NEUTRAL_STATUS_CODES:
        return "neutral"
    if status == 429 or status >= 500:
        return "throttle"
    return "success"


class _AdaptiveConcurrencyController:
    """Bounded additive-increase / multiplicative-decrease limiter per origin.

    The controller never hands out more than ``maximum`` permits for one
    origin and never shrinks below ``minimum``, so a low-volume origin keeps
    a usable floor even after a bad patch. ``cooldown`` collapses a burst of
    simultaneous failures into a single decrease, which stops one bad second
    from driving the budget straight to the floor.

    All state is plain data mutated from the broker's event loop thread, so
    no locking is needed; the injectable ``clock`` keeps tests deterministic.
    """

    def __init__(
        self,
        *,
        enabled: bool = DEFAULT_ADAPTIVE_CONCURRENCY,
        initial: int = DEFAULT_ORIGIN_INITIAL_CONCURRENCY,
        minimum: int = DEFAULT_ORIGIN_MIN_CONCURRENCY,
        maximum: int = DEFAULT_MAX_CONCURRENCY,
        step: int = DEFAULT_ORIGIN_INCREASE_STEP,
        success_threshold: int = DEFAULT_ORIGIN_SUCCESS_THRESHOLD,
        decrease_factor: float = DEFAULT_ORIGIN_DECREASE_FACTOR,
        cooldown: float = DEFAULT_ORIGIN_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.enabled = bool(enabled)
        self._maximum = max(1, int(maximum))
        self._minimum = max(1, min(self._maximum, int(minimum)))
        initial_value = int(initial)
        # 0 (or negative) means "start at the ceiling" -- react to overload
        # only, which keeps default behaviour identical to a fixed limiter.
        if initial_value <= 0:
            initial_value = self._maximum
        self._initial = max(self._minimum, min(self._maximum, initial_value))
        self._step = max(1, int(step))
        self._success_threshold = max(1, int(success_threshold))
        factor = float(decrease_factor)
        if not (0.0 < factor < 1.0):
            raise ValueError(
                "decrease_factor must be strictly between 0 and 1, "
                f"got {decrease_factor!r}"
            )
        self._decrease_factor = factor
        self._cooldown = max(0.0, float(cooldown))
        self._clock = clock
        self._origins: Dict[str, _OriginState] = {}

    # -- state ----------------------------------------------------------

    def _state(self, origin: str) -> _OriginState:
        state = self._origins.get(origin)
        if state is None:
            state = _OriginState(limit=self._initial)
            self._origins[origin] = state
        return state

    def limit_for(self, origin: str) -> int:
        return self._maximum if not self.enabled else self._state(origin).limit

    def active_for(self, origin: str) -> int:
        return self._state(origin).active

    def available(self, origin: str) -> int:
        state = self._state(origin)
        if not self.enabled:
            return self._maximum
        return max(0, state.limit - state.active)

    # -- permits --------------------------------------------------------

    def try_acquire(self, origin: str) -> bool:
        state = self._state(origin)
        if self.enabled and state.active >= state.limit:
            return False
        state.active += 1
        if state.active > state.peak_active:
            state.peak_active = state.active
        return True

    def release(self, origin: str) -> None:
        state = self._state(origin)
        state.active = max(0, state.active - 1)

    def note_deferred(self, origin: str) -> None:
        self._state(origin).deferred += 1

    # -- feedback -------------------------------------------------------

    def on_outcome(
        self,
        origin: str,
        *,
        status_code: Optional[int] = None,
        error: Optional[BaseException] = None,
    ) -> str:
        verdict = _classify_origin_outcome(status_code, error)
        if verdict == "success":
            self._on_success(origin)
        elif verdict == "throttle":
            self._on_throttle(origin)
        return verdict

    def _on_success(self, origin: str) -> None:
        state = self._state(origin)
        state.successes += 1
        if state.successes < self._success_threshold:
            return
        state.successes = 0
        if state.limit >= self._maximum:
            return
        state.limit = min(self._maximum, state.limit + self._step)
        state.increases += 1
        self._report(origin, state, "increase")

    def _on_throttle(self, origin: str) -> None:
        state = self._state(origin)
        state.throttles += 1
        state.successes = 0
        now = self._clock()
        if now - state.last_decrease < self._cooldown:
            # Still inside the cooldown for the previous decrease: one bad
            # burst must not multiply the budget down repeatedly.
            return
        state.last_decrease = now
        if state.limit <= self._minimum:
            return
        reduced = int(state.limit * self._decrease_factor)
        if reduced >= state.limit:
            reduced = state.limit - 1
        state.limit = max(self._minimum, reduced)
        state.decreases += 1
        self._report(origin, state, "decrease")

    def _report(self, origin: str, state: _OriginState, action: str) -> None:
        log.debug(
            "Adaptive concurrency %s for %s: limit=%d active=%d throttles=%d",
            action,
            origin,
            state.limit,
            state.active,
            state.throttles,
        )
        record_stage(
            "broker.origin_limit_change",
            0.0,
            outcome=action,
            details={
                "origin": origin,
                "limit": state.limit,
                "active": state.active,
                "throttles": state.throttles,
            },
        )

    # -- reporting ------------------------------------------------------

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {
            origin: {
                "limit": state.limit,
                "active": state.active,
                "peak_active": state.peak_active,
                "throttles": state.throttles,
                "increases": state.increases,
                "decreases": state.decreases,
                "deferred": state.deferred,
            }
            for origin, state in self._origins.items()
        }

    def settings(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "initial": self._initial,
            "minimum": self._minimum,
            "maximum": self._maximum,
            "step": self._step,
            "success_threshold": self._success_threshold,
            "decrease_factor": self._decrease_factor,
            "cooldown": self._cooldown,
        }


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
        adaptive: Optional[Dict[str, Any]] = None,
    ):
        _require_dependencies(require_http2=transport is None)
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
        client_kwargs: Dict[str, Any] = {
            "http2": transport is None,
            # Match requests.get(), which historically followed imagery
            # provider redirects. Several providers redirect HTTP tile URLs to
            # HTTPS and must not be reported as failed with a 301/302 response.
            "follow_redirects": True,
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
        self._active_requests = 0

        adaptive_kwargs = dict(adaptive or {})
        adaptive_kwargs.setdefault("maximum", self._max_concurrency)
        adaptive_kwargs["maximum"] = min(
            self._max_concurrency, max(1, int(adaptive_kwargs["maximum"]))
        )
        self._limiter = _AdaptiveConcurrencyController(**adaptive_kwargs)
        # Entries waiting for a permit on their origin. They are parked here
        # rather than blocking a worker, so a saturated (or throttled)
        # provider can never starve the other origins.
        self._backlog: Dict[str, "collections.deque"] = {}

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
        # Queued and parked entries never started a task, so nothing else will
        # ever answer their waiters: settle them here instead of letting the
        # client sit until its deadline expires.
        for entry in list(self._entries.values()):
            await self._fail_waiters(
                entry,
                "BrokerShutdownError",
                "broker is shutting down",
            )
        self._entries.clear()
        self._backlog.clear()
        await self._client.aclose()

    async def _fail_waiters(
        self, entry: _CoalescedEntry, error_type: str, message: str
    ) -> None:
        for request_id in list(entry.waiters):
            identity = entry.waiters.pop(request_id, None)
            if identity is None:
                continue
            self._by_request_id.pop(request_id, None)
            await self._reply_cb(identity, {
                "type": "ERROR", "id": request_id,
                "error": {"type": error_type, "message": message},
            })

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
            if entry.started:
                await self._reply_cb(
                    identity,
                    {"type": "STARTED", "id": request_id},
                )
            return

        entry = _CoalescedEntry(
            key=key,
            method=method,
            url=url,
            headers=dict(headers),
            priority=priority,
            timeout=timeout,
            origin=_origin_key(url),
            seq=next(self._seq),
        )
        entry.waiters[request_id] = identity
        self._entries[key] = entry
        self._by_request_id[request_id] = entry
        self._queue.put_nowait((priority, entry.seq, entry))

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
        self._unpark(entry)
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
        self._entries.pop(entry.key, None)

    # -- per-origin admission --------------------------------------------

    def _park(self, entry: _CoalescedEntry) -> None:
        """Hold *entry* until its origin has a free permit.

        Called only after `try_acquire` failed. Nothing awaits between that
        check and this append, so the permit state cannot change underneath
        us and a wakeup can never be lost: the eventual `release` for the
        origin is what re-queues this entry.
        """
        entry.parked = True
        self._backlog.setdefault(entry.origin, collections.deque()).append(entry)
        self._limiter.note_deferred(entry.origin)
        profile_gauge("broker.origin_backlog", self._backlog_depth())

    def _unpark(self, entry: _CoalescedEntry) -> None:
        if not entry.parked:
            return
        entry.parked = False
        backlog = self._backlog.get(entry.origin)
        if backlog is None:
            return
        try:
            backlog.remove(entry)
        except ValueError:
            pass
        if not backlog:
            self._backlog.pop(entry.origin, None)

    def _resume_origin(self, origin: str) -> None:
        backlog = self._backlog.get(origin)
        if not backlog:
            return
        slots = self._limiter.available(origin)
        while backlog and slots > 0:
            entry = backlog.popleft()
            entry.parked = False
            if entry.cancelled or not entry.waiters:
                continue
            self._queue.put_nowait((entry.priority, entry.seq, entry))
            slots -= 1
        if not backlog:
            self._backlog.pop(origin, None)

    def _backlog_depth(self) -> int:
        return sum(len(items) for items in self._backlog.values())

    def stats(self) -> Dict[str, Any]:
        """Server-side counters, returned to clients via the STATS message."""
        return {
            "active_requests": self._active_requests,
            "queue_depth": self._queue.qsize(),
            "backlog_depth": self._backlog_depth(),
            "entries": len(self._entries),
            "max_concurrency": self._max_concurrency,
            "adaptive": self._limiter.settings(),
            "origins": self._limiter.snapshot(),
        }

    async def _worker_loop(self) -> None:
        while True:
            _priority, _seq, entry = await self._queue.get()
            acquired = False
            try:
                if entry.cancelled or not entry.waiters:
                    continue
                if not self._limiter.try_acquire(entry.origin):
                    self._park(entry)
                    continue
                acquired = True
                record_stage(
                    "broker.queue_wait",
                    (time.monotonic() - entry.queued_at) * 1000.0,
                    details={
                        "priority": entry.priority,
                        "waiters": len(entry.waiters),
                        "origin": entry.origin,
                    },
                )
                entry.started = True
                for request_id, identity in list(entry.waiters.items()):
                    if request_id not in entry.waiters:
                        continue
                    await self._reply_cb(
                        identity,
                        {"type": "STARTED", "id": request_id},
                    )
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
                if acquired:
                    self._limiter.release(entry.origin)
                    self._resume_origin(entry.origin)
                self._queue.task_done()

    async def _execute(self, entry: _CoalescedEntry) -> None:
        message: Optional[Dict[str, Any]] = None
        body: Optional[bytes] = None
        request_started = time.monotonic()
        outcome = "ok"
        self._active_requests += 1
        profile_gauge("broker.active_requests", self._active_requests)
        try:
            timeout = httpx.Timeout(
                connect=entry.timeout.connect,
                read=entry.timeout.read,
                write=entry.timeout.write,
                pool=entry.timeout.pool,
            )
            async with self._client.stream(entry.method, entry.url, headers=entry.headers, timeout=timeout) as resp:
                headers_received = time.monotonic()
                record_stage(
                    "broker.time_to_first_byte",
                    (headers_received - request_started) * 1000.0,
                    outcome="ok" if resp.status_code < 400 else "failed",
                    details={
                        "status_code": resp.status_code,
                        "http_version": resp.http_version,
                    },
                )
                content_length = resp.headers.get("content-length")
                if content_length is not None and int(content_length) > self._max_response_bytes:
                    raise ResponseTooLargeError(
                        f"response Content-Length {content_length} exceeds bound {self._max_response_bytes}"
                    )
                chunks = []
                total = 0
                body_started = time.monotonic()
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise ResponseTooLargeError(
                            f"response body exceeded bound of {self._max_response_bytes} bytes"
                        )
                    chunks.append(chunk)
                record_stage(
                    "broker.body_read",
                    (time.monotonic() - body_started) * 1000.0,
                    details={
                        "response_bytes": total,
                        "http_version": resp.http_version,
                    },
                )
                body = b"".join(chunks)
                self._limiter.on_outcome(
                    entry.origin, status_code=resp.status_code
                )
                # The body travels in its own zmq frame; only the (small)
                # metadata is msgpack-encoded.
                message = {
                    "type": "RESPONSE",
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "content_length": len(body),
                }
        except asyncio.CancelledError:
            outcome = "cancelled"
            # Cancelled either because the last waiter unsubscribed (waiters
            # is already empty -- nobody to notify) or because the broker is
            # shutting down while waiters are still pending (notify them).
            await self._fail_waiters(
                entry, "BrokerShutdownError", "broker is shutting down"
            )
            self._entries.pop(entry.key, None)
            raise
        except Exception as exc:
            outcome = "failed"
            self._limiter.on_outcome(entry.origin, error=exc)
            message = {"type": "ERROR", "error": {"type": type(exc).__name__, "message": str(exc)}}
        finally:
            self._entries.pop(entry.key, None)
            self._active_requests = max(0, self._active_requests - 1)
            profile_gauge("broker.active_requests", self._active_requests)
            profile_gauge("broker.queue_depth", self._queue.qsize())
            profile_gauge(
                f"broker.origin_limit.{entry.origin}",
                self._limiter.limit_for(entry.origin),
            )
            record_stage(
                "broker.http_request",
                (time.monotonic() - request_started) * 1000.0,
                outcome=outcome,
                details={
                    "priority": entry.priority,
                    "waiters": len(entry.waiters),
                    "origin": entry.origin,
                },
            )

        if message is not None:
            for request_id, identity in dict(entry.waiters).items():
                self._by_request_id.pop(request_id, None)
                out = dict(message)
                out["id"] = request_id
                await self._reply_cb(identity, out, body)
            entry.waiters.clear()


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
            adaptive=config.get("adaptive"),
        )

    async def _send(
        self,
        identity: bytes,
        message: Dict[str, Any],
        body: Optional[bytes] = None,
    ) -> None:
        """Send one reply: metadata frame, plus a body frame when present.

        ``copy=False`` hands the already-materialised body buffer to zmq
        without copying it again, which is the whole point of keeping the
        JPEG out of the msgpack envelope.
        """
        try:
            frames = [identity, _encode(message)]
            if message.get("type") == "RESPONSE":
                frames.append(body if body is not None else b"")
            await self._router.send_multipart(frames, copy=False)
        except Exception:
            log.debug("Failed to send broker reply to client", exc_info=True)

    async def run(self) -> None:
        await self._core.start()
        try:
            while not self._stop_event.is_set():
                try:
                    frames = await asyncio.wait_for(self._router.recv_multipart(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if len(frames) != 2:
                    log.warning(
                        "Dropping broker message with %d frames (expected 2)",
                        len(frames),
                    )
                    continue
                identity, raw = frames
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
            if msg.get("type") in ("HELLO", "REQUEST", "STATS"):
                await self._send(identity, {
                    "type": "ERROR", "id": msg.get("id"),
                    "error": {"type": "BrokerAuthError", "message": "invalid auth token"},
                })
            return

        mtype = msg.get("type")
        if mtype == "HELLO":
            await self._send(
                identity,
                {"type": "HELLO_ACK", "id": msg.get("id")},
            )
        elif mtype == "REQUEST":
            await self._handle_request(identity, msg)
        elif mtype == "STATS":
            await self._handle_stats(identity, msg)
        elif mtype == "CANCEL":
            await self._core.cancel(msg.get("id"))
        elif mtype == "SHUTDOWN":
            self._stop_event.set()
        else:
            await self._send(identity, {
                "type": "ERROR", "id": msg.get("id"),
                "error": {"type": "BrokerProtocolError", "message": f"unknown message type {mtype!r}"},
            })

    async def _handle_stats(self, identity: bytes, msg: Dict[str, Any]) -> None:
        """Answer a STATS request as a normal RESPONSE with a msgpack body."""
        payload = _encode(self._core.stats())
        await self._send(
            identity,
            {
                "type": "RESPONSE",
                "id": msg.get("id"),
                "status_code": 200,
                "headers": {"content-type": "application/msgpack"},
                "content_length": len(payload),
            },
            payload,
        )

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

def _adaptive_config(
    *,
    enabled: bool = DEFAULT_ADAPTIVE_CONCURRENCY,
    initial: int = DEFAULT_ORIGIN_INITIAL_CONCURRENCY,
    minimum: int = DEFAULT_ORIGIN_MIN_CONCURRENCY,
    maximum: Optional[int] = None,
    step: int = DEFAULT_ORIGIN_INCREASE_STEP,
    success_threshold: int = DEFAULT_ORIGIN_SUCCESS_THRESHOLD,
    decrease_factor: float = DEFAULT_ORIGIN_DECREASE_FACTOR,
    cooldown: float = DEFAULT_ORIGIN_COOLDOWN_SECONDS,
) -> Dict[str, Any]:
    """Normalize the adaptive-concurrency knobs into a picklable dict."""
    config: Dict[str, Any] = {
        "enabled": bool(enabled),
        "initial": int(initial),
        "minimum": int(minimum),
        "step": int(step),
        "success_threshold": int(success_threshold),
        "decrease_factor": float(decrease_factor),
        "cooldown": float(cooldown),
    }
    if maximum is not None:
        config["maximum"] = int(maximum)
    return config


def _server_config(
    *,
    max_concurrency,
    max_connections,
    max_request_bytes,
    max_response_bytes,
    profile_environment=None,
    adaptive=None,
) -> Dict[str, Any]:
    return {
        "max_concurrency": max_concurrency,
        "max_connections": max_connections,
        "max_request_bytes": max_request_bytes,
        "max_response_bytes": max_response_bytes,
        "profile_environment": dict(profile_environment or {}),
        "adaptive": dict(adaptive or {}),
    }


def _process_entrypoint(token: str, handshake_queue, config: Dict[str, Any]) -> None:
    """Entrypoint for the spawned broker process. Must stay picklable/top-level."""
    profiler_started = False
    profile_environment = config.get("profile_environment") or {}
    if profile_environment:
        import os

        os.environ.update(
            {str(key): str(value) for key, value in profile_environment.items()}
        )
        try:
            try:
                from autoortho.diagnostics import start_worker_profiler_from_env
            except ImportError:
                from diagnostics import start_worker_profiler_from_env
            profiler_started = (
                start_worker_profiler_from_env("http2-broker") is not None
            )
        except Exception as exc:
            log.warning("Could not start broker performance diagnostics: %s", exc)
    try:
        _require_dependencies(require_http2=True)
    except BrokerUnavailableError as exc:
        handshake_queue.put(("error", str(exc)))
        return
    loop = _new_broker_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _serve_process(token, handshake_queue, config)
        )
    except BaseException as exc:  # pragma: no cover - process boundary
        message = f"{type(exc).__name__}: {exc}"
        try:
            handshake_queue.put(("runtime_error", message))
        except Exception:
            pass
        log.error("Broker process crashed: %s", message)
    finally:
        _close_broker_event_loop(loop)
        if profiler_started:
            try:
                try:
                    from autoortho.diagnostics import stop_active_profiler
                except ImportError:
                    from diagnostics import stop_active_profiler
                stop_active_profiler()
            except Exception as exc:
                log.warning("Could not finalize broker diagnostics: %s", exc)


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

    def diagnostic(self) -> Optional[str]:
        messages = []
        while True:
            try:
                status, payload = self._queue.get_nowait()
            except _queue_module.Empty:
                break
            if status in ("error", "runtime_error"):
                messages.append(str(payload))
        proc = self._process
        if proc is not None and not proc.is_alive():
            messages.append(
                f"broker process exited with code {proc.exitcode}"
            )
        return "; ".join(messages) or None

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._process, "pid", None)


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
        self._server_task: Optional[asyncio.Task] = None
        self._zmq_ctx = None
        self._runtime_error: Optional[str] = None
        self._stopping = False

    def start(self, handshake_timeout: float) -> int:
        ready: "_queue_module.Queue" = _queue_module.Queue()

        def _run() -> None:
            loop = _new_broker_event_loop()
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
                self._server_task = loop.create_task(server.run())
                loop.run_until_complete(self._server_task)
            except BaseException as exc:  # pragma: no cover - thread boundary
                if not (
                    self._stopping
                    and isinstance(exc, asyncio.CancelledError)
                ):
                    self._runtime_error = f"{type(exc).__name__}: {exc}"
                    log.error(
                        "In-process broker server crashed: %s",
                        self._runtime_error,
                    )
            finally:
                try:
                    ctx.destroy(linger=0)
                except Exception:
                    pass
                _close_broker_event_loop(loop)

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
        self._stopping = True
        stop_event = getattr(self._server, "_stop_event", None)
        loop = self._loop
        if loop is not None:
            try:
                def _request_stop():
                    if stop_event is not None:
                        stop_event.set()
                    task = self._server_task
                    if task is not None and not task.done():
                        task.cancel()

                loop.call_soon_threadsafe(_request_stop)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("In-process broker thread did not stop within %.1fs", timeout)

    def diagnostic(self) -> Optional[str]:
        if self._runtime_error:
            return self._runtime_error
        if self._thread is not None and not self._thread.is_alive():
            return "broker server thread exited unexpectedly"
        return None


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

    Pipelined usage, where many requests are outstanding without one
    blocked caller thread each::

        futures = [broker.submit_async(url, priority=0) for url in urls]
        for future in futures:
            future.add_done_callback(on_tile_bytes)
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
        max_pending: int = DEFAULT_MAX_PENDING,
        reserved_live_slots: Optional[int] = None,
        live_priority_threshold: int = DEFAULT_LIVE_PRIORITY_THRESHOLD,
        server_factory=None,
        profile_environment: Optional[Dict[str, str]] = None,
        adaptive_concurrency: bool = DEFAULT_ADAPTIVE_CONCURRENCY,
        origin_initial_concurrency: int = DEFAULT_ORIGIN_INITIAL_CONCURRENCY,
        origin_min_concurrency: int = DEFAULT_ORIGIN_MIN_CONCURRENCY,
        origin_max_concurrency: Optional[int] = None,
        origin_increase_step: int = DEFAULT_ORIGIN_INCREASE_STEP,
        origin_success_threshold: int = DEFAULT_ORIGIN_SUCCESS_THRESHOLD,
        origin_decrease_factor: float = DEFAULT_ORIGIN_DECREASE_FACTOR,
        origin_cooldown_seconds: float = DEFAULT_ORIGIN_COOLDOWN_SECONDS,
        queue_timeout: float = 60.0,
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
        self._queue_timeout = max(0.1, min(600.0, float(queue_timeout)))
        self._max_pending = max(1, min(MAX_PENDING_LIMIT, int(max_pending)))
        if reserved_live_slots is None:
            reserved_live_slots = int(
                round(self._max_pending * DEFAULT_RESERVED_LIVE_FRACTION)
            )
        self._reserved_live_slots = max(
            0, min(self._max_pending - 1, int(reserved_live_slots))
        )
        self._live_priority_threshold = int(live_priority_threshold)
        self._profile_environment = dict(profile_environment or {})
        self._adaptive = _adaptive_config(
            enabled=adaptive_concurrency,
            initial=origin_initial_concurrency,
            minimum=origin_min_concurrency,
            maximum=origin_max_concurrency,
            step=origin_increase_step,
            success_threshold=origin_success_threshold,
            decrease_factor=origin_decrease_factor,
            cooldown=origin_cooldown_seconds,
        )

        self._token = secrets.token_urlsafe(32)
        self._zmq_ctx = None
        self._runtime = None
        self._address: Optional[str] = None
        self._dispatcher: Optional[_ClientDispatcher] = None
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
            profile_environment=self._profile_environment,
            adaptive=self._adaptive,
        )

        if self._in_process:
            runtime = _ThreadRuntime(token=self._token, config=config, transport=self._transport, server_factory=self._server_factory)
        else:
            runtime = _ProcessRuntime(token=self._token, config=config)

        try:
            port = runtime.start(self._handshake_timeout)
        except BrokerError:
            self._close_sockets()
            raise
        except Exception as exc:
            self._close_sockets()
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
            self._address = None
            self._close_sockets()
            raise

        try:
            self._start_dispatcher()
        except Exception as exc:
            try:
                runtime.stop(timeout=1.0)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._runtime = None
            self._address = None
            self._close_sockets()
            raise BrokerStartupError(
                f"failed to start broker client dispatcher: {exc}"
            ) from exc

        self._started = True

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the broker to shut down gracefully, then force-stop it."""
        if not self._started or self._stopped:
            return
        self._stopped = True
        if self._owns_runtime:
            self._send_control_message({"type": "SHUTDOWN", "token": self._token})

        if self._owns_runtime and self._runtime is not None:
            self._runtime.stop(timeout=timeout)

        dispatcher, self._dispatcher = self._dispatcher, None
        if dispatcher is not None:
            dispatcher.stop(timeout=timeout)

        self._close_sockets()
        self._started = False

    def client_environment(self) -> Dict[str, str]:
        if not self._started or not self._address:
            raise BrokerShutdownError("broker is not running")
        return {
            "AO_HTTP2_BROKER_ADDR": self._address,
            "AO_HTTP2_BROKER_TOKEN": self._token,
        }

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._runtime, "pid", None)

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @property
    def reserved_live_slots(self) -> int:
        return self._reserved_live_slots

    def pending_count(self) -> int:
        """Number of requests currently outstanding on the client socket."""
        dispatcher = self._dispatcher
        return dispatcher.pending_count() if dispatcher is not None else 0

    def stats(self) -> Dict[str, int]:
        """Snapshot of client dispatcher counters (for diagnostics/tests)."""
        dispatcher = self._dispatcher
        if dispatcher is None:
            return {
                "pending": 0,
                "max_pending": self._max_pending,
                "reserved_live_slots": self._reserved_live_slots,
            }
        return dispatcher.stats()

    @classmethod
    def connect(
        cls,
        address: str,
        token: str,
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        reserved_live_slots: Optional[int] = None,
        live_priority_threshold: int = DEFAULT_LIVE_PRIORITY_THRESHOLD,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        queue_timeout: float = 60.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> "HTTP2Broker":
        """Attach a client-only handle to an existing broker process."""
        _require_dependencies(require_http2=True)
        if not address.startswith("tcp://127.0.0.1:"):
            raise BrokerProtocolError(
                "broker clients may only connect to a loopback TCP endpoint"
            )
        if not token:
            raise BrokerAuthError("broker auth token is required")
        broker = cls(
            max_pending=max_pending,
            reserved_live_slots=reserved_live_slots,
            live_priority_threshold=live_priority_threshold,
            max_request_bytes=max_request_bytes,
            queue_timeout=queue_timeout,
            max_response_bytes=max_response_bytes,
        )
        broker._token = token
        broker._address = address
        broker._zmq_ctx = zmq.Context()
        broker._runtime = None
        broker._owns_runtime = False
        try:
            broker._handshake()
            broker._start_dispatcher()
        except Exception:
            broker._close_sockets()
            raise
        broker._started = True
        return broker

    # -- requests -------------------------------------------------------

    def submit_async(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        *,
        priority: int = DEFAULT_PRIORITY,
        timeout: Optional[RequestTimeout] = None,
        request_id: Optional[str] = None,
    ) -> BrokerFuture:
        """Queue a GET without blocking and return its `BrokerFuture`.

        The request is written to the shared client socket by the dispatcher
        thread, so an arbitrary number of requests can be outstanding without
        a Python thread each. Raises `BrokerCapacityError` immediately when
        the pending budget for this priority class is exhausted; requests
        below ``live_priority_threshold`` may additionally draw on the
        reserved live slots.
        """
        if not self._started or self._stopped:
            raise BrokerShutdownError("broker is not running")
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise BrokerShutdownError("broker client dispatcher is not running")

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
        deadline = (
            time.monotonic()
            + self._queue_timeout
        )
        return dispatcher.submit(
            envelope,
            priority=int(priority),
            deadline=deadline,
            network_timeout=req_timeout.total_seconds(),
        )

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
        req_timeout = timeout or RequestTimeout()
        future = self.submit_async(
            url,
            headers,
            priority=priority,
            timeout=req_timeout,
            request_id=request_id,
        )
        # The dispatcher already enforces the same deadline; the extra second
        # only covers scheduling jitter before it settles the future.
        wait_seconds = (
            self._queue_timeout
            + req_timeout.total_seconds()
            + CLIENT_TIMEOUT_GRACE_SECONDS
            + 1.0
        )
        try:
            return future.result(timeout=wait_seconds)
        except BrokerTimeoutError:
            self.cancel(future.request_id)
            raise

    def cancel(self, request_id: str) -> bool:
        """Cancel a previously issued request by id (best effort).

        The matching future, if still pending, is settled with
        `BrokerCancelledError` and the broker is asked to drop the work.
        Returns True when this call is the one that settled the future.
        """
        if not request_id:
            return False
        dispatcher = self._dispatcher
        if dispatcher is None:
            return False
        return dispatcher.cancel(request_id)

    def server_stats(self, *, timeout: float = 5.0) -> Dict[str, Any]:
        """Ask the broker server for its counters (blocking round trip).

        Includes the adaptive per-origin table: ``limit``, ``active``,
        ``peak_active``, ``throttles``, ``increases``, ``decreases`` and how
        often requests for that origin had to wait for a permit.
        """
        if not self._started or self._stopped:
            raise BrokerShutdownError("broker is not running")
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise BrokerShutdownError("broker client dispatcher is not running")
        envelope = {
            "type": "STATS",
            "token": self._token,
            "id": uuid.uuid4().hex,
        }
        future = dispatcher.submit(
            envelope,
            priority=0,
            deadline=time.monotonic() + timeout,
            network_timeout=timeout,
        )
        response = future.result(timeout=timeout + CLIENT_TIMEOUT_GRACE_SECONDS)
        return _decode(response.content)

    # -- internals --------------------------------------------------------

    def _start_dispatcher(self) -> None:
        if self._dispatcher is not None:
            return

        def _cancel_envelope(request_id: str) -> Dict[str, Any]:
            return {"type": "CANCEL", "token": self._token, "id": request_id}

        dispatcher = _ClientDispatcher(
            ctx=self._zmq_ctx,
            address=self._address,
            cancel_factory=_cancel_envelope,
            max_pending=self._max_pending,
            reserved_live_slots=self._reserved_live_slots,
            live_priority_threshold=self._live_priority_threshold,
            max_request_bytes=self._max_request_bytes,
            max_response_bytes=self._max_response_bytes,
        )
        dispatcher.start()
        self._dispatcher = dispatcher

    def _send_control_message(self, message: Dict[str, Any]) -> None:
        """Send a fire-and-forget control frame on a short-lived socket.

        Control frames are rare (shutdown), and using a dedicated socket
        keeps them independent of the dispatcher's lifecycle while still
        allowing zmq to flush the frame in the background.
        """
        ctx = self._zmq_ctx
        if ctx is None or not self._address:
            return
        sock = None
        try:
            sock = ctx.socket(zmq.DEALER)
            sock.setsockopt(zmq.LINGER, 1000)
            sock.connect(self._address)
            sock.send(_encode(message))
        except Exception:
            log.debug("Failed to send broker control message", exc_info=True)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # pragma: no cover - best-effort cleanup
                    log.debug("Failed to close broker control socket", exc_info=True)

    def _handshake(self) -> None:
        sock = self._zmq_ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.connect(self._address)
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            handshake_id = uuid.uuid4().hex
            hello = _encode(
                {
                    "type": "HELLO",
                    "token": self._token,
                    "id": handshake_id,
                }
            )
            deadline = time.monotonic() + self._handshake_timeout
            next_send = 0.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send:
                    sock.send(hello)
                    next_send = now + 0.25
                remaining_ms = max(
                    1,
                    min(250, int((deadline - now) * 1000)),
                )
                events = dict(poller.poll(timeout=remaining_ms))
                if sock not in events:
                    diagnostic = self._runtime_diagnostic()
                    if diagnostic:
                        raise BrokerStartupError(
                            "broker server exited before handshake: "
                            + diagnostic
                        )
                    continue
                reply = _decode(sock.recv())
                if reply.get("id") not in (None, handshake_id):
                    continue
                if reply.get("type") == "ERROR":
                    err = reply.get("error", {})
                    raise BrokerStartupError(
                        "broker handshake rejected: "
                        + err.get("message", "unknown error")
                    )
                if reply.get("type") == "HELLO_ACK":
                    return
                raise BrokerStartupError(
                    f"unexpected handshake reply: {reply.get('type')!r}"
                )
            diagnostic = self._runtime_diagnostic()
            suffix = f": {diagnostic}" if diagnostic else ""
            raise BrokerStartupError(
                "broker handshake timed out" + suffix
            )
        finally:
            sock.close(0)

    def _runtime_diagnostic(self) -> Optional[str]:
        runtime = self._runtime
        if runtime is None:
            return None
        diagnostic = getattr(runtime, "diagnostic", None)
        if diagnostic is None:
            return None
        try:
            return diagnostic()
        except Exception:
            return None

    def _close_sockets(self) -> None:
        dispatcher, self._dispatcher = self._dispatcher, None
        if dispatcher is not None:
            dispatcher.stop(timeout=2.0)
        if self._zmq_ctx is not None:
            try:
                self._zmq_ctx.destroy(linger=0)
            except Exception:  # pragma: no cover - best-effort cleanup
                log.debug("Failed to destroy broker zmq context", exc_info=True)
            self._zmq_ctx = None

    def __enter__(self) -> "HTTP2Broker":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

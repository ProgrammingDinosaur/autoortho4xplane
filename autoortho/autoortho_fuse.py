#!/usr/bin/env python

#from __future__ import with_statement
import os
import re
import time
import math
import errno
import ctypes
import threading
import inspect
import queue
import heapq
import itertools

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho import flighttrack
except ImportError:
    import flighttrack

from collections import defaultdict, OrderedDict
from functools import wraps, lru_cache

try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

try:
    from autoortho.utils.constants import system_type
except ImportError:
    from utils.constants import system_type

try:
    from autoortho import read_trace
except ImportError:
    import read_trace

try:
    from autoortho import wait_bands
except ImportError:
    import wait_bands

try:
    from autoortho.time_exclusion import (
        time_exclusion_manager, _find_global_scenery_dsf, _parse_dsf_coordinates,
    )
except ImportError:
    from time_exclusion import (
        time_exclusion_manager, _find_global_scenery_dsf, _parse_dsf_coordinates,
    )

try:
    from autoortho import tile_stream
except ImportError:
    import tile_stream

import logging
log = logging.getLogger(__name__)

try:
    from autoortho.mfusepy import FUSE, FuseOSError, Operations, fuse_get_context, _libfuse
except ImportError:
    from mfusepy import FUSE, FuseOSError, Operations, fuse_get_context, _libfuse

try:
    from autoortho import getortho
except ImportError:
    import getortho

try:
    from autoortho import disk_texture
except ImportError:
    import disk_texture

try:
    from autoortho.diagnostics import profile_span, record_stage
except ImportError:
    from diagnostics import profile_span, record_stage

try:
    from autoortho import dds_fallback, debug_faults
except ImportError:
    import dds_fallback
    import debug_faults

# AO_DEBUG_FAIL_DDS: make chosen texture reads fail on purpose (debug_faults.py).
_DDS_FAULTS = debug_faults.DdsFaults.from_env()


# ---------------------------------------------------------------------------
# Native reads (hybrid read path plan, S2; [streaming] native_direct_reads).
#
# The read callback of a mount's fuse_operations is swapped for native code
# (aopipeline/aofast.c) that answers reads of complete textures served from
# their cache file without Python. It hands every other read, and any read it
# has a doubt about, to the Python callback it replaced. The swap happens in
# mfusepy's call of fuse_main_real, which gets the struct.
# ---------------------------------------------------------------------------

_native_pending = threading.local()  # the AutoOrtho whose FUSE() is starting
_native_hook_lock = threading.Lock()
_native_hooked = set()  # ids of the mfusepy modules whose fuse_main_real is wrapped
_native_registry = {}  # tagged handle -> DiskTexture, for the drained samples
_native_registry_lock = threading.Lock()
_native_drain_started = False


def _aofast():
    if __package__ and __package__.startswith("autoortho"):
        from autoortho.aopipeline import AoFast
    else:
        from aopipeline import AoFast
    return AoFast


def _native_reads_setting():
    value = getattr(getattr(CFG, "streaming", None), "native_direct_reads", False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _hook_fuse_main(module):
    """Wrap ``module.fuse_main_real`` once: a FUSE() started inside
    :func:`native_reads_for` gets its read callback swapped."""
    with _native_hook_lock:
        if id(module) in _native_hooked:
            return
        real = module.fuse_main_real

        def fuse_main_real(argc, argv, ops_pointer, size, ctx):
            ao = getattr(_native_pending, "ao", None)
            _native_pending.ao = None
            if ao is not None:
                try:
                    ao._attach_native(ops_pointer.contents)
                except Exception as exc:
                    log.warning(f"Native texture reads not installed: {exc}")
            try:
                return real(argc, argv, ops_pointer, size, ctx)
            finally:
                if ao is not None:
                    ao._detach_native()

        module.fuse_main_real = fuse_main_real
        _native_hooked.add(id(module))


class native_reads_for:
    """Context manager around ``FUSE(ao, ...)``: native reads for that mount
    when ``[streaming] native_direct_reads`` is on and the library has them.

    With a read trace (``AO_READ_TRACE``) as well, native reads stay on: the
    trace keeps opens (marked ``native``) and releases, and the reads native
    code answers are not in it. A trace meant for replaying needs every read:
    record it with native reads off.

    Off, or anything missing: the mount behaves exactly as without it.
    """

    def __init__(self, ao, fuse_class=None):
        self.ao = ao
        self.fuse_class = fuse_class or FUSE

    def __enter__(self):
        ao = self.ao
        if not _native_reads_setting():
            return ao
        try:
            native = _aofast().load()
            if native is None:
                return ao
            import sys
            module = sys.modules.get(self.fuse_class.__module__)
            if module is None or not hasattr(module, "fuse_main_real"):
                return ao
            native.set_fh_offset(module.fuse_file_info.fh.offset)
            _hook_fuse_main(module)
        except Exception as exc:
            log.warning(f"Native texture reads unavailable: {exc}")
            return ao
        ao._native_candidate = native
        _native_pending.ao = ao
        return ao

    def __exit__(self, *exc_info):
        _native_pending.ao = None
        return False


def _start_native_drain(native):
    """Once a second: wanted levels to the paging model, counters to STATS."""
    global _native_drain_started
    with _native_registry_lock:
        if _native_drain_started:
            return
        _native_drain_started = True

    def loop():
        last = {}
        while True:
            time.sleep(1.0)
            try:
                _drain_native_once(native, last)
            except Exception as exc:
                log.debug(f"Native read drain failed: {exc}")

    threading.Thread(target=loop, name="AO-native-reads", daemon=True).start()


def _feed_native_samples(texture, samples):
    for level, count in samples:
        for _ in range(min(int(count), 4)):
            texture.note_wanted_level(int(level))


def _drain_native_once(native, last):
    by_handle = {}
    for handle, level, count in native.drain():
        by_handle.setdefault(handle, []).append((level, count))
    if by_handle:
        with _native_registry_lock:
            textures = {h: _native_registry.get(h) for h in by_handle}
        for handle, samples in by_handle.items():
            texture = textures.get(handle)
            if texture is not None:
                _feed_native_samples(texture, samples)
    stats = native.stats()
    for name in ("reads", "bytes", "deferred", "deferred_rows", "deferred_io",
                 "deferred_closed", "deferred_level", "past_end"):
        delta = stats.get(name, 0) - last.get(name, 0)
        if delta > 0:
            _bump_stat(f"dds_native_{name}", delta)
    last.update(stats)


def _bump_stat(name, value=1):
    """Count ``name`` in STATS; never fails a read."""
    try:
        getortho.bump(name, value)
    except Exception:
        pass


def _get_fuse_pid_for_log():
    """Return the calling process ID for diagnostics, or None if unavailable."""
    try:
        _uid, _gid, pid = fuse_get_context()
    except Exception as e:
        log.debug(f"FUSE context unavailable while opening DSF; continuing without caller PID: {e}")
        return None
    return pid

def deg2num(lat_deg, lon_deg, zoom):
  lat_rad = math.radians(lat_deg)
  n = 2.0 ** zoom
  xtile = int((lon_deg + 180.0) / 360.0 * n)
  ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
  return (xtile, ytile)


def tilemeters(lat_deg, zoom):
    y = 64120000 * math.cos(math.radians(lat_deg)) / (pow(2, zoom))
    x = 64120000 / (pow(2, zoom))
    return (x, y)


MEMTRACE = False
# Debug lines written on every FUSE call (getattr, open, read, opendir...)
# only when AO_TRACE_READS=1 (see getortho.TRACE_READS). X-Plane makes
# thousands of these calls a second while it loads scenery, which filled the
# rotated log in seconds and pushed out everything else, startup included.
TRACE_READS = os.environ.get("AO_TRACE_READS") == "1"


def locked(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            result = fn(self, *args, **kwargs)
        return result
    return wrapped

def fuse_config_by_os() -> dict:
    overrides = {"connection": {}, "config": {}}
    if os.name == 'posix':
        overrides["config"].update(dict(
            uid=-1,
            gid=-1,
            set_uid=1,
            set_gid=1,
        ))
    if system_type == 'linux':
        overrides["config"].update(dict(
            uid=-1,
            gid=-1,
            set_uid=1,
            set_gid=1,
        ))
    elif system_type == 'darwin':
        # Leave defaults
        pass
    elif system_type == 'windows':
        overrides["config"].update(dict(
            uid=-1,
            gid=-1,
            set_uid=1,
            set_gid=1,
        ))
    return overrides

def fuse_option_profiles_by_os(nothreads: bool, mount_name: str) -> dict:

    options = dict(
        nothreads=nothreads,
        foreground=True,
        allow_other=True,
    )
    if system_type == 'linux':
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
        ))
    elif system_type == 'darwin':
        # Calculate daemon_timeout based on the longest possible tile budget
        # + 60s buffer. This prevents macFUSE from killing the worker when
        # operations take longer than the default 60 second timeout.
        #
        # Longest tile budget (matches _calculate_build_timeout):
        # - Base: wait_bands.tile_budget_seconds() (the longest band wait)
        # - Startup: the first-load budget (10x, capped at 1800s)
        # - Fallback: + fallback_timeout if enabled (default 30s)
        # - Buffer: + 15s for DDS operations
        # - Safety: + 60s additional buffer for daemon_timeout
        # Never below the first-load cap: 3.1.0 used it with default settings
        # (1875 s timeout), and the 60 s default near wait gave only 675 s.
        # macFUSE drops the whole mount when one request outlives this.
        max_startup_budget = max(
            wait_bands.tile_budget_seconds(initial_load=True),
            wait_bands.INITIAL_LOAD_CAP_S,
        )

        # Add fallback timeout if enabled
        fallback_extends = getattr(CFG.autoortho, 'fallback_extends_budget', False)
        if isinstance(fallback_extends, str):
            fallback_extends = fallback_extends.lower().strip() in ('true', '1', 'yes', 'on')
        fallback_timeout = 0
        if fallback_extends:
            fallback_timeout = getattr(CFG.autoortho, 'fallback_timeout', 30)
            if isinstance(fallback_timeout, str):
                try:
                    fallback_timeout = float(fallback_timeout)
                except ValueError:
                    fallback_timeout = 30
        
        # daemon_timeout = max_build_timeout + 60s safety buffer
        # max_build_timeout = max_startup_budget + fallback_timeout + 15s buffer
        daemon_timeout = int(max_startup_budget + fallback_timeout + 15 + 60)
        
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
            volname=mount_name,
            daemon_timeout=daemon_timeout,
        ))

    elif system_type == 'windows':
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
            VolumeName=mount_name,
            FileSystemName=mount_name,
        ))

    return options


class DSFPrefetchScheduler:
    """Bounded, demand-aware scheduler for DSF prefetch submissions."""

    PENDING = "pending"
    RUNNING = "running"
    DEFERRED = "deferred"
    WAITING_FOR_FLIGHT = "waiting_for_flight"
    COMPLETE = "complete"

    def __init__(
        self,
        submit,
        runtime_allowed,
        live_demand=None,
        runtime_gate=None,
        before_submit=None,
        worker_count=1,
        queue_size=16,
        grace_period=2.0,
        retry_delay=0.25,
        max_retry_delay=5.0,
        max_retries=3,
        poll_interval=0.1,
        clock=None,
    ):
        self._submit = submit
        self._runtime_allowed = runtime_allowed
        self._live_demand = live_demand or (lambda: False)
        self._runtime_gate = runtime_gate
        self._before_submit = before_submit
        self._worker_count = max(1, min(2, int(worker_count)))
        self._queue_size = max(1, int(queue_size))
        self._queue = queue.Queue(maxsize=self._queue_size)
        self._max_entries = max(self._queue_size, self._queue_size * 4)
        self._grace_period = max(0.0, float(grace_period))
        self._retry_delay = max(0.0, float(retry_delay))
        self._max_retry_delay = max(
            self._retry_delay, float(max_retry_delay)
        )
        self._max_retries = max(0, int(max_retries))
        self._poll_interval = max(0.01, float(poll_interval))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._workers = []
        self._entries = {}
        self._deferred_heap = []
        self._deferred_sequence = itertools.count()
        self._completed = OrderedDict()
        self._completed_ttl = 600.0
        self._completed_limit = max(64, self._queue_size * 4)
        self._allowed_since = None
        self._cursor_arg = self._find_cursor_arg()

    @staticmethod
    def _bump(name, value=1):
        try:
            getortho.bump(name, value)
        except Exception:
            pass

    def _report_depth(self):
        try:
            getortho.profile_gauge(
                "dsf_prefetch.queue_depth",
                self._queue.qsize(),
            )
        except Exception:
            pass

    def _find_cursor_arg(self):
        try:
            parameters = inspect.signature(self._submit).parameters
        except (TypeError, ValueError):
            return None
        if "cursor" in parameters:
            return "cursor"
        if "state" in parameters:
            return "state"
        if "prefetch_state" in parameters:
            return "prefetch_state"
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return "cursor"
        return None

    def schedule(self, path):
        """Schedule a path once; duplicate pending/running paths are coalesced."""
        path = os.path.normcase(os.path.abspath(path))
        with self._lock:
            if self._stop.is_set():
                return False
            now = self._clock()
            self._purge_completed_locked(now)
            if path in self._entries or path in self._completed:
                self._bump("dsf_prefetch_coalesced")
                return False
            if len(self._entries) >= self._max_entries:
                self._bump("dsf_prefetch_backpressure")
                return False
            entry = {
                "state": self.PENDING,
                "cursor": None,
                "retries": 0,
                "pressure_retries": 0,
                "next_attempt": now,
                "revision": 0,
            }
            self._entries[path] = entry
            try:
                self._queue.put_nowait(path)
            except queue.Full:
                self._defer(entry, self._poll_interval, pressure=True, path=path)
            self._start_workers_locked()
            self._report_depth()
            return True

    def state(self, path):
        """Return a path state for diagnostics and focused tests."""
        with self._lock:
            entry = self._entries.get(path)
            if entry:
                return entry["state"]
            self._purge_completed_locked(self._clock())
            return self.COMPLETE if path in self._completed else None

    def worker_count(self):
        return len(self._workers)

    def shutdown(self, timeout=1.0):
        """Cancel queued/deferred work and wait briefly for active workers."""
        self._stop.set()
        if self._runtime_gate is not None:
            try:
                self._runtime_gate.notify_state_change()
            except Exception:
                pass
        with self._lock:
            self._entries.clear()
            self._deferred_heap.clear()
            self._completed.clear()
            workers = tuple(self._workers)
        for _worker in workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break
        for worker in workers:
            worker.join(timeout=max(0.0, timeout))

    close = shutdown

    def _start_workers_locked(self):
        if self._workers:
            return
        for index in range(self._worker_count):
            worker = threading.Thread(
                target=self._worker,
                name=f"DSFPrefetchWorker-{index + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _defer(self, entry, delay, pressure=False, path=None):
        entry["state"] = self.DEFERRED
        entry["next_attempt"] = self._clock() + max(0.0, delay)
        entry["revision"] += 1
        if path is None:
            path = next(
                (
                    candidate
                    for candidate, current in self._entries.items()
                    if current is entry
                ),
                None,
            )
        if path is not None:
            heapq.heappush(
                self._deferred_heap,
                (
                    entry["next_attempt"],
                    next(self._deferred_sequence),
                    path,
                    entry["revision"],
                ),
            )
        if pressure:
            entry["pressure_retries"] += 1
            self._bump("dsf_prefetch_backpressure")
        self._bump("dsf_prefetch_retries")

    def _defer_delay(self, attempts):
        return min(
            self._retry_delay * (2 ** max(0, attempts - 1)),
            self._max_retry_delay,
        )

    def _promote_deferred(self):
        now = self._clock()
        with self._lock:
            while self._deferred_heap and self._deferred_heap[0][0] <= now:
                next_attempt, sequence, path, revision = heapq.heappop(
                    self._deferred_heap
                )
                entry = self._entries.get(path)
                if (
                    entry is None
                    or entry["state"] != self.DEFERRED
                    or entry["revision"] != revision
                ):
                    continue
                try:
                    self._queue.put_nowait(path)
                except queue.Full:
                    heapq.heappush(
                        self._deferred_heap,
                        (next_attempt, sequence, path, revision),
                    )
                    self._bump("dsf_prefetch_backpressure")
                    return
                entry["state"] = self.PENDING
                self._report_depth()

    def _next_wait(self):
        now = self._clock()
        with self._lock:
            while self._deferred_heap:
                deadline, _sequence, path, revision = self._deferred_heap[0]
                entry = self._entries.get(path)
                if (
                    entry is not None
                    and entry["state"] == self.DEFERRED
                    and entry["revision"] == revision
                ):
                    return min(
                        self._poll_interval,
                        max(0.0, deadline - now),
                    )
                heapq.heappop(self._deferred_heap)
        return self._poll_interval

    def _purge_completed_locked(self, now):
        while self._completed:
            path, deadline = next(iter(self._completed.items()))
            if deadline > now:
                break
            self._completed.pop(path, None)

    def _can_run(self):
        if (
            not self._runtime_allowed()
            or self._live_demand()
        ):
            self._allowed_since = None
            return False, self._poll_interval
        now = self._clock()
        if self._allowed_since is None:
            self._allowed_since = now
        remaining = self._grace_period - (now - self._allowed_since)
        return remaining <= 0.0, max(0.0, remaining)

    def _call_submit(self, path, cursor):
        if cursor is not None and self._cursor_arg:
            return self._submit(path, **{self._cursor_arg: cursor})
        return self._submit(path)

    @staticmethod
    def _submission_result(result):
        """Normalize legacy integer and optional progress-aware API responses."""
        if isinstance(result, int):
            return result, True, False, None
        if isinstance(result, tuple):
            submitted = int(result[0]) if result else 0
            if len(result) == 2:
                second = result[1]
                if isinstance(second, bool):
                    return submitted, second, not second, None
                return submitted, False, True, second
            if len(result) >= 3:
                second, third = result[1:3]
                if isinstance(second, bool):
                    return submitted, second, not second, third
                return submitted, bool(third), not bool(third), second
            return submitted, True, False, None
        if isinstance(result, dict):
            cursor = result.get(
                "cursor",
                result.get(
                    "state",
                    result.get(
                        "next_cursor",
                        result.get("next_state", result.get("prefetch_state")),
                    ),
                ),
            )
            complete = result.get(
                "complete", result.get("completed", result.get("done"))
            )
            pressure = bool(
                result.get("pressure")
                or result.get("deferred")
                or result.get("partial")
            )
            if complete is None:
                complete = not pressure and cursor is None
            return (
                int(result.get("submitted", result.get("count", 0))),
                bool(complete),
                pressure or not bool(complete),
                cursor,
            )
        cursor = getattr(
            result,
            "cursor",
            getattr(
                result,
                "state",
                getattr(
                    result,
                    "next_cursor",
                    getattr(result, "next_state", None),
                ),
            ),
        )
        complete = getattr(
            result,
            "complete",
            getattr(result, "completed", getattr(result, "done", None)),
        )
        pressure = bool(
            getattr(result, "pressure", False)
            or getattr(result, "deferred", False)
            or getattr(result, "partial", False)
        )
        if complete is None:
            complete = not pressure and cursor is None
        return (
            int(getattr(result, "submitted", getattr(result, "count", 0))),
            bool(complete),
            pressure or not bool(complete),
            cursor,
        )

    def _worker(self):
        while not self._stop.is_set():
            self._promote_deferred()
            try:
                path = self._queue.get(timeout=self._next_wait())
            except queue.Empty:
                continue
            self._report_depth()
            if path is None:
                return
            with self._lock:
                entry = self._entries.get(path)
                if not entry or entry["state"] != self.PENDING:
                    continue
                entry["state"] = self.RUNNING
                cursor = entry["cursor"]
            if (
                self._runtime_gate is not None
                and not self._runtime_allowed()
            ):
                with self._lock:
                    current = self._entries.get(path)
                    if current is not None:
                        current["state"] = self.WAITING_FOR_FLIGHT
                self._bump("dsf_prefetch_waiting_for_flight")
                if not self._runtime_gate.wait_until_allowed(self._stop):
                    return
                if not self._runtime_allowed():
                    with self._lock:
                        current = self._entries.get(path)
                        if current is not None:
                            self._defer(
                                current,
                                self._poll_interval,
                                path=path,
                            )
                    continue
                with self._lock:
                    current = self._entries.get(path)
                    if current is None:
                        continue
                    current["state"] = self.RUNNING
                    cursor = current["cursor"]
            allowed, delay = self._can_run()
            if not allowed:
                with self._lock:
                    if path in self._entries:
                        self._defer(
                            entry,
                            delay or self._poll_interval,
                            path=path,
                        )
                continue
            try:
                if self._before_submit:
                    self._before_submit()
                result = self._call_submit(path, cursor)
                submitted, complete, pressure, next_cursor = (
                    self._submission_result(result)
                )
            except Exception as exc:
                with self._lock:
                    entry = self._entries.get(path)
                    if not entry:
                        continue
                    entry["retries"] += 1
                    if entry["retries"] > self._max_retries:
                        del self._entries[path]
                        log.error(
                            "DSF prefetch failed permanently for %s: %s",
                            path,
                            exc,
                        )
                    else:
                        self._defer(
                            entry,
                            self._defer_delay(entry["retries"]),
                            path=path,
                        )
                continue

            with self._lock:
                entry = self._entries.get(path)
                if not entry:
                    continue
                if complete:
                    self._entries.pop(path, None)
                    self._completed[path] = (
                        self._clock() + self._completed_ttl
                    )
                    self._completed.move_to_end(path)
                    while len(self._completed) > self._completed_limit:
                        self._completed.popitem(last=False)
                    log.info(
                        "DSF prefetch queued %d chunks for %s", submitted, path
                    )
                else:
                    if next_cursor is not None:
                        entry["cursor"] = next_cursor
                    entry["retries"] = 0
                    self._defer(
                        entry,
                        self._defer_delay(entry["pressure_retries"] + 1),
                        pressure=pressure,
                        path=path,
                    )


class _DirectHandle:
    """One X-Plane open of a texture served from its cache file.

    ``live`` turns on when the file failed a read: the rest of this open is
    served by a live tile (holding a tile reference until release).
    """

    __slots__ = ("texture", "live", "lock", "native", "native_reads", "native_table")

    def __init__(self, texture, native=0, native_reads=None, native_table=-1):
        self.texture = texture
        self.live = False
        self.lock = threading.Lock()
        # The tagged handle when native code answers this open's reads, and
        # the season table entry it uses (-1: none).
        self.native = native
        self.native_reads = native_reads
        self.native_table = native_table


class _KeyLocks:
    """One lock per key, kept only while a thread is using or awaiting it.

    A plain ``defaultdict(threading.Lock)`` kept a lock for every texture
    ever read, for the rest of the session. Here a lock is dropped once
    every thread that checked it out has checked it back in, so two threads
    working on the same key always share one lock and an idle key costs
    nothing.

    Each ``checkout()`` must be matched by exactly one ``checkin()``
    (``release()`` does both the unlock and the check-in): one too many
    would drop a lock another thread still holds.
    """

    def __init__(self):
        self._mutex = threading.Lock()
        self._entries = {}  # key -> [lock, threads using it]

    def checkout(self, key) -> threading.Lock:
        """The key's lock, not acquired, counted as in use by the caller."""
        with self._mutex:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = [threading.Lock(), 0]
            entry[1] += 1
            return entry[0]

    def checkin(self, key) -> None:
        with self._mutex:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] <= 0:
                del self._entries[key]

    def release(self, key, lock) -> None:
        """Unlock a checked-out lock the caller holds, and check it in."""
        try:
            lock.release()
        finally:
            self.checkin(key)

    def __len__(self) -> int:
        with self._mutex:
            return len(self._entries)


class AutoOrtho(Operations):

    open_paths = []
    read_paths = []

    path_dict = {}
    tile_dict = {}

    fh_locks = {}

    fh = 1000

    default_uid = -1
    default_gid = -1

    startup = True

    # Streaming defaults, so instances built without __init__ (tests) work.
    _stream = None
    # Texture reads recorded for the replay benchmark (AO_READ_TRACE).
    _read_trace = None
    # Real texture sizes recorded at open (not getattr's estimates).
    _opened_sizes = None
    _OPENED_SIZES_MAX = 8192
    # Native reads (aopipeline/AoFast.py) for this mount, once installed.
    _native = None
    _native_mount = -1
    _native_candidate = None
    _virtual_files = {}
    _virtual_lock = threading.Lock()
    _stream_fallback = set()
    _stream_placeholders = set()

    def __init__(self, root, cache_dir='.cache', *args, **kwargs):
        log.info(f"ROOT: {root}")
        self.dds_re = re.compile(r".*/(\d+)[-_](\d+)[-_]((?!ZL)\S*)(\d{2}).dds")
        self.ktx2_re = re.compile(r".*/(\d+)[-_](\d+)[-_]((?!ZL)\D*)(\d+).ktx2")
        self.dsf_re = re.compile(r".*/[-+]\d+[-+]\d+.dsf")
        self.ter_re = re.compile(r".*/\d+[-_]\d+[-_](\D*)(\d+).ter")
        # Row, column, map type and zoom of a tile's terrain definition, for
        # load-ahead (X-Plane opens these 20-45 s before their textures).
        self.ter_tile_re = re.compile(
            r".*/(\d+)[-_](\d+)[-_](\S*?)(\d{2})(?:_(?:sea|water)_overlay)?\.ter$"
        )
        self.root = os.path.abspath(root)
        self.cache_dir = cache_dir
        self._read_trace = read_trace.from_environment(position=_trace_position)

        getortho.clear_shutdown_request()
        # Shared workers pass one TileCacher to every mount they serve.
        self.tc = kwargs.get("tile_cacher") or getortho.TileCacher(cache_dir)
        
        # Register terrain index for this scenery
        # This indexes .ter files to discover actual tile zoom levels
        # Critical for predictive DDS: ensures we prefetch the exact tiles X-Plane will request
        terrain_folder = os.path.join(self.root, "terrain")
        scenery_name = os.path.basename(self.root)
        self._scenery_name = scenery_name
        self._terrain_lookup = getortho.register_terrain_index(
            terrain_folder, scenery_name
        )
        self._runtime_services_started = False
        self._runtime_services_lock = threading.Lock()
        self._dsf_prefetch_scheduler = kwargs.get(
            "dsf_prefetch_scheduler"
        ) or DSFPrefetchScheduler(
            getattr(getortho, "prefetch_dsf", lambda _path: 0),
            runtime_allowed=getattr(
                getortho, "is_flight_runtime_allowed", lambda: False
            ),
            runtime_gate=getattr(getortho, "flight_runtime_gate", None),
            live_demand=getattr(getortho, "has_live_pressure", lambda: False),
            before_submit=self._ensure_runtime_services,
            worker_count=kwargs.get("dsf_prefetch_workers", 1),
            queue_size=kwargs.get("dsf_prefetch_queue_size", 16),
            grace_period=kwargs.get("dsf_prefetch_grace_period", 2.0),
        )
    
        #self.path_condition = threading.Condition()
        #self.read_lock = threading.Lock()
        self.open_paths = []
        self.read_paths = []
        self.path_dict = {}
        self.tile_dict = {}
        self.fh_locks = defaultdict(threading.Lock)
        self.startup = True
        self._lock = threading.RLock()
        self._tile_locks = _KeyLocks()
        self._size_cache = OrderedDict()  # LRU cache for DDS sizes
        self._opened_sizes = OrderedDict()  # real sizes from opens (see _dds_size_for_read)
        self._size_cache_max = 5000  # Max entries before LRU eviction
        self._negative_getattr_cache = OrderedDict()  # LRU cache for avoiding getattr probes for files that don't exist
        self._negative_getattr_cache_max = 16384  # Max entries before LRU eviction
        self._negative_getattr_cache_lock = threading.Lock()
        self._ft_started = False
        self._ft_start_lock = threading.Lock()

        # Track redirected DSF file handles: fh -> redirect_path
        # Used when time exclusion redirects DSF reads to global scenery
        self._redirected_dsf_fhs = {}

        # Textures served from their cache file: fh -> _DirectHandle. Their
        # handles start above any OS file descriptor; live DDS opens use 0.
        self._direct = {}
        self._direct_lock = threading.Lock()
        self._direct_fh = itertools.count(1 << 32)

        self.use_ns = kwargs.get("use_ns", False)

        # Streaming package: DSFs and masks are fetched on first open and
        # .ter files are generated, instead of coming from an install.
        self._stream = None
        self._virtual_files = {}
        self._virtual_lock = threading.Lock()
        self._virtual_fh = itertools.count(1 << 40)
        self._stream_fallback = set()
        self._stream_placeholders = set()  # masks served as blank stand-ins
        marker = tile_stream.read_marker(self.root)
        if marker is not None:
            try:
                if marker.get("kind") == "overlay":
                    # Overlays the classic download already installed win.
                    hidden = tile_stream.overlay_hidden_dsfs(
                        str(getattr(CFG.paths, "scenery_path", "") or "")
                    )
                else:
                    hidden = tile_stream.installed_dsfs(
                        os.path.dirname(self.root), self.root
                    )
                self._stream = tile_stream.StreamPackage(
                    self.root,
                    marker,
                    on_terrain=self._terrain_lookup.add_virtual_tiles,
                    hidden=hidden,
                )
                threading.Thread(
                    target=tile_stream.refresh_index,
                    args=(self.root, marker),
                    name="tile-index-refresh",
                    daemon=True,
                ).start()
            except Exception as exc:
                log.error(f"Tile stream disabled for {self.root}: {exc}")
                self._stream = None

        # Initialize time exclusion manager with dataref tracker
        try:
            from autoortho.datareftrack import dt as datareftracker
        except ImportError:
            from datareftrack import dt as datareftracker
        time_exclusion_manager.set_dataref_tracker(datareftracker)

    def _ensure_runtime_services(self):
        if self._runtime_services_started:
            return
        with self._runtime_services_lock:
            if self._runtime_services_started:
                return
            getortho.start_prefetcher(self.tc, self._scenery_name)
            getortho.start_predictive_dds(self.tc)
            self._runtime_services_started = True

    def _schedule_dsf_prefetch(self, path):
        if not self._dsf_prefetch_scheduler.schedule(path):
            log.debug("DSF prefetch already scheduled or stopped: %s", path)

    def shutdown(self):
        """Stop background DSF scheduling when this FUSE runtime exits."""
        self._dsf_prefetch_scheduler.shutdown()
        if self._stream is not None:
            self._stream.shutdown()

    def destroy(self, path=None):
        self.shutdown()

    # Helpers
    # =======

    def init_with_config(self, conn_info, config_3):
        """Called by libfuse during mount. Configure read sizes and caching.

        - Prefer configuring sizes here over mount options to avoid unknown-option
          issues across different libfuse builds.
        """

        # Conn Info Overrides
        overrides = fuse_config_by_os()
        
        # Configure kernel connection limits when available
        try:
            if conn_info is not None:
                if overrides["connection"]:
                    for k, v in overrides["connection"].items():
                        if hasattr(conn_info, k):
                            setattr(conn_info, k, v)
                            log.debug(f"FUSE: set conn.{k}={v}")
                else:
                    log.debug(f"FUSE: no connection overrides")

            if config_3 is not None:
                if overrides["config"]:
                    for k, v in overrides["config"].items():
                        if hasattr(config_3, k):
                            setattr(config_3, k, v)
                            if k == "uid":
                                self.default_uid = v
                            if k == "gid":
                                self.default_gid = v
                            log.debug(f"FUSE: set config.{k}={v}")
                else:
                    log.debug(f"FUSE: no config overrides")
        except Exception as e:
            log.warning(f"FUSE: failed to apply fuse_config tuning: {e}")

    def _ensure_flighttrack_started(self, reason_path=None):
        """Start flight tracking exactly once, when first DDS is touched."""
        if self._ft_started:
            return
        log.info(f"First DDS access at {reason_path}. Starting flight tracker.")
        try:
            flighttrack.ft.start()
        except Exception as e:
            # Keep the FS alive even if flighttrack fails; just log loudly.
            log.error(f"Failed to start flight tracker: {e}")
            # Do NOT mark _ft_started; we'll retry once on next DDS trigger.
            return

        self._ft_started = True

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.abspath(os.path.join(self.root, partial))
        return path

    def _tile_key(self, row, col, maptype, zoom):
        return (row, col, maptype, zoom)

    def _calculate_build_timeout(self):
        """
        Calculate dynamic build_timeout from the tile budget (wait_bands).

        This ensures the FUSE lock timeout is always sufficient for tile building:
        - Base: wait_bands.tile_budget_seconds() (time allowed for chunk downloads)
        - Startup: the first-load budget while X-Plane has not connected yet
        - Extended: + fallback_timeout if fallback_extends_budget is enabled
        - Buffer: + 15 seconds for DDS operations and overhead
        
        Returns timeout in seconds (int).
        """
        # The first-load budget applies before X-Plane ever connected (must
        # match getortho.py); stutters that briefly drop the connection do
        # not get extended timeouts.
        # getortho already imported the tracker under whichever name this
        # build uses. Importing it again here by its bare name failed in
        # package and frozen builds, so every read paid a full sys.path
        # search for a module that never loads.
        datareftracker = getattr(getortho, "datareftracker", None)
        initial_load = (
            datareftracker is not None
            and not getattr(datareftracker, 'has_ever_connected', False)
        )
        tile_budget = wait_bands.tile_budget_seconds(initial_load)
        
        # Check if fallback extends the budget
        fallback_extends = getattr(CFG.autoortho, 'fallback_extends_budget', False)
        if isinstance(fallback_extends, str):
            fallback_extends = fallback_extends.lower().strip() in ('true', '1', 'yes', 'on')
        
        # Get fallback_timeout if extended fallbacks are enabled (default 30s)
        fallback_timeout = 0
        if fallback_extends:
            fallback_timeout = getattr(CFG.autoortho, 'fallback_timeout', 30)
            if isinstance(fallback_timeout, str):
                try:
                    fallback_timeout = float(fallback_timeout)
                except ValueError:
                    fallback_timeout = 30
        
        # Calculate total: budget + extended fallback + 15s buffer
        # The 15s buffer accounts for DDS read/write, processing overhead, and safety margin
        build_timeout = int(tile_budget + fallback_timeout + 15)
        
        return build_timeout

    def _failfast(self, msg, exc=None):
        log.error(msg)
        if exc:
            log.exception("cause:", exc_info=exc)
        # In previous versions we called fuse_exit here, which kills the mount and
        # the simulator session on transient per-tile errors (e.g. cache sharing violations).
        # Instead, surface an I/O error back to the caller and keep the FS alive.
        raise FuseOSError(errno.EIO)

    # Filesystem methods
    # ==================

    def _access(self, path, mode):
        if TRACE_READS:
            log.debug(f"ACCESS: {path}")
        #m = re.match(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds", path)
        #if m:
        #    log.info(f"ACCESS: Found DDS file {path}: %s " % str(m.groups()))
        full_path = self._full_path(path)
        if not os.access(full_path, mode):
            raise FuseOSError(errno.EACCES)

    def chmod(self, path, mode):
        full_path = self._full_path(path)
        return os.chmod(full_path, mode)

    def chown(self, path, uid, gid):
        full_path = self._full_path(path)
        return os.chown(full_path, uid, gid)

    @lru_cache(maxsize=1024)
    def _calculate_dds_size(self, zoom):
        """Calculate the actual DDS file size based on tile parameters and current configuration.
        
        IMPORTANT: In dynamic zoom mode, the actual tile zoom can vary based on altitude prediction.
        To avoid truncated texture issues, we calculate size for the maximum configured
        zoom level this tile type can use, still capped to the X-Plane limit of
        tile zoom + 1.  This ensures FUSE reports a size >= the actual DDS size
        without inflating X-Plane's VRAM budget beyond what the config can produce.
        """
        try:
            # Convert parameters to the format expected by the tile system
            zoom = int(zoom)
            
            # Check if dynamic zoom mode is enabled
            max_zoom_mode = str(CFG.autoortho.max_zoom_mode).lower()
            
            if max_zoom_mode == "dynamic":
                # Dynamic mode chooses the tile's real zoom later, during open(),
                # based on predicted altitude.  During getattr(), report the
                # maximum zoom the configured quality steps can actually return
                # for this tile type, not the theoretical zoom + 1 for every tile.
                max_regular, max_airport = self.tc.dynamic_zoom_manager.get_max_zoom_levels()
                if zoom == 18 and not CFG.autoortho.using_custom_tiles:
                    uncapped_target_zoom = max_airport
                else:
                    uncapped_target_zoom = max_regular
                max_zoom = min(zoom + 1, uncapped_target_zoom)
            else:
                # Fixed mode - use the configured target zoom levels
                if CFG.autoortho.using_custom_tiles:
                    uncapped_target_zoom = self.tc.target_zoom_level
                else:
                    uncapped_target_zoom = self.tc.target_zoom_level_near_airports if zoom == 18 else self.tc.target_zoom_level
                max_zoom = min(zoom + 1, uncapped_target_zoom)
            
            # Replicate tile dimension calculation logic from Tile.__init__
            width = 16  # Default tile width in chunks
            height = 16  # Default tile height in chunks
            
            tilezoom_diff = zoom - int(max_zoom)
            
            if tilezoom_diff >= 0:
                chunks_per_row = width >> tilezoom_diff
                chunks_per_col = height >> tilezoom_diff
            else:
                chunks_per_row = width << (-tilezoom_diff)
                chunks_per_col = height << (-tilezoom_diff)
            
            # Calculate DDS dimensions in pixels
            dds_width = chunks_per_row * 256
            dds_height = chunks_per_col * 256
            
            # Replicate DDS size calculation logic from pydds.DDS.__init__
            if CFG.pydds.format == 'BC3':
                blocksize = 16
            else:
                blocksize = 8
            
            # Calculate total size including all mipmaps
            curbytes = 128  # DDS header size
            current_width = dds_width
            current_height = dds_height
            
            while (current_width >= 1) and (current_height >= 1):
                mipmap_size = max(1, (current_width * current_height >> 4)) * blocksize
                curbytes += mipmap_size
                current_width = current_width >> 1
                current_height = current_height >> 1
            
            log.debug(f"Calculated DDS size for zoom {zoom}: {curbytes} bytes "
                     f"(dimensions: {dds_width}x{dds_height}, max_zoom: {max_zoom}, tilezoom_diff: {tilezoom_diff})")
            
            return curbytes
            
        except Exception as e:
            log.warning(f"Failed to calculate DDS size for {zoom}: {e}, using fallback")
            # Fallback to hardcoded values if calculation fails
            if CFG.pydds.format == "BC1":
                return 11184952
            else:
                return 22369776


    def getattr(self, path, fh=None):
        if TRACE_READS:
            log.debug(f"GETATTR {path}")
        
        # Check for DSF time exclusion redirect
        # Instead of hiding DSF files (which breaks X-Plane's indexing),
        # we redirect to X-Plane's global scenery DSF files
        if self.dsf_re.match(path):
            redirect_path = time_exclusion_manager.get_redirect_path(path)
            if redirect_path:
                log.debug(f"GETATTR: Redirecting DSF to global scenery: {path} -> {redirect_path}")
                return self._getattr_redirected_dsf(redirect_path)
        
        # Generate fresh timestamps for each call
        now = int(time.time())
        
        m = self.dds_re.match(path)
        if m:
            # DDS files are virtual - use cached size calculation but fresh timestamps
            trace = getattr(self, "_read_trace", None)
            if trace is None:
                return self._getattr_dds(path, m, now)
            # Timed like an open: the file system asks this for every open,
            # and it waits for its turn the same way (ready share plan, R8).
            started = time.monotonic()
            attrs = self._getattr_dds(path, m, now)
            trace.event(
                "getattr", path, ms=round((time.monotonic() - started) * 1000.0, 3),
                tid=threading.get_ident(), pid=_get_fuse_pid_for_log(),
            )
            return attrs
        elif path.endswith(".poison"):
            return self._getattr_poison(now)
        elif path.endswith("AOISWORKING"):
            return self._getattr_marker(now)
        else:
            if self._stream is not None:
                attrs = self._getattr_stream(path)
                if attrs is not None:
                    return attrs
            # Real filesystem files - never cache, always get fresh stats
            return self._getattr_real_file(path)

    @staticmethod
    def _stream_rel(path):
        return path.replace("\\", "/").lstrip("/")

    @staticmethod
    def _dsf_latlon(rel):
        m = re.search(r"([+-]\d{2})([+-]\d{3})\.dsf$", rel)
        return int(m.group(1)), int(m.group(2))

    def _virtual_attrs(self, size):
        stamp = int(self._stream.index.built or time.time())
        return {
            'st_atime': stamp,
            'st_ctime': stamp,
            'st_mtime': stamp,
            'st_mode': 33060,  # regular file, read-only
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': size,
        }

    def _getattr_stream(self, path):
        """Attributes of a streamable file not fetched yet, or None.

        Must stay cheap: directory listings stat every entry, so nothing is
        downloaded here. A DSF reports its size from the index, or the Global
        Scenery file's when it can't be fetched right now.
        """
        if not path.endswith((".dsf", ".png", ".ter")):
            return None
        if os.path.exists(self._full_path(path)):
            return None
        rel = self._stream_rel(path)
        stream = self._stream
        if rel.endswith(".ter"):
            if not rel.startswith("terrain/"):
                return None
            data = stream.ter_bytes(rel.split("/", 1)[1])
            return None if data is None else self._virtual_attrs(len(data))
        size = stream.remote_size(rel)
        if size is None:
            return None
        if rel.endswith(".png") and (rel in self._stream_placeholders
                                     or stream.remote.in_cooldown()):
            return self._virtual_attrs(len(stream.placeholder_png(rel)))
        if rel.endswith(".dsf") and (rel in self._stream_fallback or stream.unavailable(rel)):
            if stream.kind == "overlay":
                lat, lon = self._dsf_latlon(rel)
                return self._virtual_attrs(len(tile_stream.empty_overlay_dsf(lat, lon)))
            folder, fname = _parse_dsf_coordinates(path)
            global_dsf = _find_global_scenery_dsf(folder, fname) if folder else None
            if global_dsf:
                return self._getattr_redirected_dsf(global_dsf)
        return self._virtual_attrs(size)

    def _open_stream_dsf(self, path, flags):
        """Make a streamed DSF local, or open Global Scenery in its place.

        Returns an open handle for the fallback, or None when the AutoOrtho
        file is now on disk and the normal open should proceed.
        """
        rel = self._stream_rel(path)
        if not self._stream.streams(rel):
            return None
        if self._stream.ensure_local(rel) is not None:
            self._stream_fallback.discard(rel)
            self._stream.prefetch_neighbors(rel)
            return None
        self._stream.prefetch_neighbors(rel)
        if self._stream.kind == "overlay":
            # Global Scenery can't stand in for an overlay; an empty one keeps
            # X-Plane loading (that tile just lacks roads and objects).
            lat, lon = self._dsf_latlon(rel)
            log.info(f"OPEN: overlay DSF [{path}] not streamed yet; serving an empty overlay")
            self._stream_fallback.add(rel)
            fh = next(self._virtual_fh)
            with self._virtual_lock:
                self._virtual_files[fh] = tile_stream.empty_overlay_dsf(lat, lon)
            time_exclusion_manager.register_dsf_open(path)
            return fh
        folder, fname = _parse_dsf_coordinates(path)
        global_dsf = _find_global_scenery_dsf(folder, fname) if folder else None
        if not global_dsf:
            log.warning(f"OPEN: DSF [{path}] could not be streamed and has no Global Scenery file")
            raise FuseOSError(errno.ENOENT)
        log.info(f"OPEN: DSF [{path}] not streamed yet; using Global Scenery {global_dsf}")
        if system_type == 'windows':
            fh = os.open(global_dsf, flags | os.O_BINARY)
        else:
            fh = os.open(global_dsf, flags)
        self._stream_fallback.add(rel)
        self._redirected_dsf_fhs[fh] = global_dsf
        time_exclusion_manager.register_dsf_open(path)
        return fh

    @staticmethod
    def _resolve_dds_maptype(maptype: str) -> str:
        """Resolve virtual DDS maptype, remapping non-source placeholders.

        Some scenery packages use ``ZL`` (zoom-level notation) in .ter
        filenames instead of a real map source identifier.  When X-Plane
        reads these .ter files it requests the corresponding DDS through
        FUSE.  We remap ``ZL`` (and the empty string) to a concrete
        source so the tile pipeline can actually fetch imagery.
        """
        if not maptype or maptype.upper() == "ZL":
            override = getattr(CFG.autoortho, 'maptype_override', None)
            if override and override not in ("Use tile default",):
                return override
            return "BI"
        return maptype

    def _get_dds_size_cached(self, row, col, maptype, zoom):
        """Get DDS size from cache or compute it.
        
        Uses _size_cache OrderedDict with LRU eviction to prevent unbounded
        memory growth. Cache can be updated with actual tile sizes when tiles
        are opened (see open() method).
        """
        key = (row, col, maptype, zoom)
        dds_size = self._size_cache.get(key)
        if dds_size is not None:
            # Move to end for LRU
            self._size_cache.move_to_end(key)
            return dds_size
        
        dds_size = self._calculate_dds_size(str(zoom))
        # Evict oldest entries if at limit
        while len(self._size_cache) >= self._size_cache_max:
            self._size_cache.popitem(last=False)
        self._size_cache[key] = dds_size
        return dds_size
    
    def _dds_size_for_read(self, row, col, maptype, zoom):
        """The real size of this texture as its last open found it, or None.

        Only sizes an open recorded (a live tile's or the cache file's) are
        used, never the estimate getattr computes from the configured zoom:
        with dynamic or airport zoom that estimate can be smaller than the
        texture, and cutting a read there would hand X-Plane short data.
        Unknown: nothing is cut here (the tile and the cache file cut at
        their own size).
        """
        sizes = getattr(self, "_opened_sizes", None)
        if not sizes:
            return None
        size = sizes.get((row, col, maptype, zoom))
        return size if size and size > 0 else None

    def _fallback_dds(self, row, col, maptype, zoom, offset, length,
                      reason="error", tile=None):
        """Bytes for a texture read that can't be served in time.

        X-Plane keeps whatever it reads first until it loads the texture
        again, so this serves the best that needs no download or build: the
        tile's own bytes when the tile is in memory (its real header, what is
        built, unbuilt levels upscaled from coarser built ones), otherwise a
        placeholder with a valid header for the size X-Plane was given and
        solid missing_color (see dds_fallback.py). An invalid header would
        make X-Plane keep the terrain white.

        Counted as ``fuse_fallback_<reason>``; ``fuse_fallback_imagery``
        counts the ones served from the tile.
        """
        _bump_stat(f"fuse_fallback_{reason}")
        fmt = CFG.pydds.format
        if _DDS_FAULTS.zero_header:
            header = b"\0" * dds_fallback.DDS_HEADER_SIZE
            return dds_fallback.fallback_bytes(offset, length, header, fmt)
        try:
            size = self._get_dds_size_cached(row, col, maptype, zoom)
        except Exception:
            size = None
        if tile is None and reason != "shutdown":
            tile = self._tile_in_memory(row, col, maptype, zoom)
        data = self._tile_standin(tile, size, offset, length)
        if data is not None:
            _bump_stat("fuse_fallback_imagery")
            return data
        header = dds_fallback.header_for_size(size, fmt)
        return dds_fallback.fallback_bytes(offset, length, header, fmt)

    def _tile_in_memory(self, row, col, maptype, zoom):
        """The tile already in memory for this texture, or None. No lock:
        a dict lookup, safe from any read thread."""
        tc = getattr(self, "tc", None)
        tiles = getattr(tc, "tiles", None)
        to_id = getattr(tc, "_to_tile_id", None)
        if tiles is None or to_id is None:
            return None
        try:
            return tiles.get(to_id(row, col, maptype, zoom))
        except Exception:
            return None

    @staticmethod
    def _tile_standin(tile, size, offset, length):
        """The tile's servable bytes without downloading or building, or None
        when it has none or they would not match the size X-Plane was told."""
        standin = getattr(tile, "standin_bytes", None)
        dds = getattr(tile, "dds", None)
        if standin is None or dds is None:
            return None
        if size and getattr(dds, "total_size", None) != size:
            return None
        try:
            data = standin(offset, length)
        except Exception as exc:
            log.debug(f"Stand-in bytes for {getattr(tile, 'id', tile)} failed: {exc}")
            return None
        if not isinstance(data, (bytes, bytearray)) or len(data) != length:
            return None
        if offset == 0 and length >= 4 and bytes(data[:4]) != b"DDS ":
            return None
        return bytes(data)

    def _set_dds_size_cached(self, row, col, maptype, zoom, size):
        """Set actual DDS size in cache with LRU eviction."""
        key = (row, col, maptype, zoom)
        # Real sizes only, for cutting reads at the end (_dds_size_for_read).
        opened = getattr(self, "_opened_sizes", None)
        if opened is None:
            opened = self._opened_sizes = OrderedDict()
        opened[key] = size
        opened.move_to_end(key)
        while len(opened) > self._OPENED_SIZES_MAX:
            opened.popitem(last=False)
        # Evict oldest entries if at limit
        while len(self._size_cache) >= self._size_cache_max:
            self._size_cache.popitem(last=False)
        self._size_cache[key] = size
        self._size_cache.move_to_end(key)

    def _getattr_dds(self, path, match, now):
        """Get attributes for virtual DDS files."""
        self._ensure_flighttrack_started(reason_path=path)
        row, col, maptype, zoom = match.groups()
        maptype = self._resolve_dds_maptype(maptype)
        dds_size = self._get_dds_size_cached(int(row), int(col), maptype, int(zoom))
        if TRACE_READS:
            log.debug("GETATTR: Fetch for path: %s", path)

        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': dds_size,
        }
        if TRACE_READS:
            log.debug(f"GETATTR: ATTRS: {attrs}")
        return attrs

    def _getattr_poison(self, now):
        """Handle poison pill for shutdown."""
        if system_type == "darwin":
            # Stopping the request loop inside getattr strands the kernel's
            # pending requests. macOS must detach before the loop exits.
            log.warning("Ignoring legacy poison probe on macOS; use OS unmount")
        else:
            log.info("Poison pill.  Exiting!")
            try:
                getortho.begin_shutdown("poison pill")
            except Exception:
                pass
            fuse_ptr = ctypes.c_void_p(_libfuse.fuse_get_context().contents.fuse)
            do_fuse_exit(fuse_ptr=fuse_ptr)
        
        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': 0,
        }
        return attrs

    def _getattr_marker(self, now):
        """Get attributes for AOISWORKING marker file."""
        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': 0,
        }
        return attrs

    def _getattr_real_file(self, path):
        """Get attributes for real filesystem files.

        Positive results are kept uncached so real files reflect the backing
        scenery directory.  Missing paths get a short negative cache because
        X-Plane can probe the same library/texture fallback paths many times
        during scenery loading.
        """
        full_path = self._full_path(path)
        cache_key = os.path.normcase(full_path)

        with self._negative_getattr_cache_lock:
            if cache_key in self._negative_getattr_cache:
                # Cache Hit:  We already know file does not exist.
                self._negative_getattr_cache.move_to_end(cache_key)  # LRU
                raise FuseOSError(errno.ENOENT)

        if TRACE_READS:
            log.debug(f"GETATTR FULLPATH {full_path}")
        try:
            st = os.lstat(full_path)
        except OSError as e:
            if TRACE_READS:
                log.debug(f"GETATTR: lstat failed for {full_path}: {e}")
            if getattr(e, "errno", None) in (errno.ENOENT, errno.ENOTDIR):
                with self._negative_getattr_cache_lock:
                    # Cache Miss + Missing File: We just found out file does not exist
                    while len(self._negative_getattr_cache) >= self._negative_getattr_cache_max:
                        self._negative_getattr_cache.popitem(last=False)
                    self._negative_getattr_cache[cache_key] = None
            raise FuseOSError(errno.ENOENT)
        if TRACE_READS:
            log.debug(f"GETATTR: Orig stat: {st}")
        attrs = {k: getattr(st, k) for k in (
            'st_atime',
            'st_ctime',
            'st_gid',
            'st_mode',
            'st_mtime',
            'st_nlink',
            'st_size',
            'st_uid',
            'st_ino',
            'st_dev',
        )}
        if TRACE_READS:
            log.debug(f"GETATTR: ATTRS: {attrs}")
        return attrs

    def _getattr_redirected_dsf(self, redirect_path):
        """Get attributes for a DSF file redirected to global scenery.
        
        When time exclusion is active, DSF reads are redirected to X-Plane's
        global scenery instead of serving empty/hidden files. This ensures
        X-Plane always has terrain data.
        
        Args:
            redirect_path: Absolute path to the global scenery DSF file
            
        Returns:
            dict: File attributes from the redirected DSF file
        """
        try:
            st = os.lstat(redirect_path)
            log.debug(f"GETATTR: Redirected DSF stat: {st}")
            attrs = {k: getattr(st, k) for k in (
                'st_atime',
                'st_ctime',
                'st_gid',
                'st_mode',
                'st_mtime',
                'st_nlink',
                'st_size',
                'st_uid',
                'st_ino',
                'st_dev',
            )}
            log.debug(f"GETATTR: Redirected DSF ATTRS: {attrs}")
            return attrs
        except OSError as e:
            log.warning(f"GETATTR: Failed to stat redirected DSF {redirect_path}: {e}")
            raise FuseOSError(e.errno)

    def readdir(self, path, fh):
        """List directory contents.
        
        DSF files are ALWAYS shown regardless of time exclusion state.
        X-Plane indexes DSF files at flight load time - hiding them causes
        missing terrain. Instead, we redirect reads to global scenery DSF files.
        """
        if path in ["/textures", "/terrain"]:
            return ['.', '..', 'AOISWORKING']

        full_path = self._full_path(path)
        if os.path.isdir(full_path):
            entries = ['.', '..']
            for entry in os.listdir(full_path):
                # DSF files are always included - never filter them out
                # Time exclusion is handled by redirecting reads, not hiding files
                if self._stream is not None and (
                    entry == tile_stream.MARKER_NAME or ".part" in entry
                ):
                    continue
                entries.append(entry)
            if self._stream is not None:
                parts = self._stream_rel(path).split("/")
                if len(parts) == 2 and parts[0] == tile_stream.DSF_DIR:
                    present = set(entries)
                    entries.extend(n for n in self._stream.dsf_names(parts[1])
                                   if n not in present)
            return entries
        return ['.', '..']

    def readlink(self, path):
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            # Path name is absolute, sanitize it.
            return os.path.relpath(pathname, self.root)
        else:
            return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)

    @lru_cache(maxsize=128)
    def statfs(self, path):
        #log.info(f"STATFS: {path}")
        full_path = self._full_path(path)
        if system_type == 'windows':
            stats = {
                    'f_bavail':47602498, 
                    'f_bfree':47602498,
                    'f_blocks':124699647, 
                    'f_favail':1000000, 
                    'f_ffree':1000000, 
                    'f_files':999, 
                    'f_frsize':4096,
                    'f_flag':1024,
                    'f_bsize':4096 
            }
            return stats
            # st = os.stat(full_path)
            # return dict((key, getattr(st, key)) for key in ('f_bavail', 'f_bfree',
            #     'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            #     'f_frsize', 'f_namemax'))
        elif system_type == 'linux' or system_type == 'darwin':
            stv = os.statvfs(full_path)
            #log.info(stv)
            stats = {}
            possible_keys = ['f_bavail', 'f_bfree',
                'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
                'f_frsize', 'f_namemax']
            
            for key in possible_keys:
                if hasattr(stv, key):
                    stats[key] = getattr(stv, key)
            return stats

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def symlink(self, name, target):
        return os.symlink(target, self._full_path(name))

    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    def link(self, target, name):
        return os.link(self._full_path(name), self._full_path(target))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    # File methods
    # ============

    #@locked
    def open(self, path, flags):

        full_path = self._full_path(path)
        if TRACE_READS:
            log.debug(f"OPEN: {path} {flags} FULLPATH {full_path}")

        # Handle DSF files with time exclusion redirect
        if self.dsf_re.match(path):
            _pid = _get_fuse_pid_for_log()
            pid_text = f" by pid={_pid}" if _pid is not None else ""
            # Check if DSF should be redirected to global scenery
            redirect_path = time_exclusion_manager.get_redirect_path(path)
            if redirect_path:
                log.info(f"OPEN: DSF [{path}]{pid_text} opened in GLOBAL SCENERY mode (time exclusion active) -> {redirect_path}")
                try:
                    if system_type == 'windows':
                        fh = os.open(redirect_path, flags | os.O_BINARY)
                    else:
                        fh = os.open(redirect_path, flags)
                    # Track this as a redirected DSF for release()
                    self._redirected_dsf_fhs[fh] = redirect_path
                    # Register this DSF as being in use (safe transition)
                    time_exclusion_manager.register_dsf_open(path)
                    return fh
                except OSError as e:
                    log.warning(f"OPEN: Failed to open redirected DSF {redirect_path}: {e}, falling back to ORTHO mode")
                    # Fall through to open the original file
            
            if self._stream is not None:
                if os.path.exists(full_path):
                    self._stream.touch(full_path)
                    self._stream.note_local_dsf(full_path)
                    self._stream.prefetch_neighbors(self._stream_rel(path))
                else:
                    fallback_fh = self._open_stream_dsf(path, flags)
                    if fallback_fh is not None:
                        return fallback_fh

            # Normal mode - serve AutoOrtho ortho scenery
            log.info(f"OPEN: DSF [{path}]{pid_text} opened in ORTHO mode (AutoOrtho scenery)")
            self._schedule_dsf_prefetch(path)
            # Register this DSF as being in use (prevents redirect during active use)
            time_exclusion_manager.register_dsf_open(path)
        
        dds_match = self.dds_re.match(path)
        if dds_match:
            self._ensure_runtime_services()
            row, col, maptype, zoom = dds_match.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            maptype = self._resolve_dds_maptype(maptype)

            # Register non-BI maptypes for terrain lookup (custom Ortho4XP tiles)
            # This allows the prefetcher to also check for custom tile maptypes
            if maptype != "BI":
                getortho.register_discovered_maptype(maptype)

            tile_id = f"{row}_{col}_{maptype}_{zoom}"
            trace_started = time.monotonic() if self._read_trace is not None else None
            getortho.note_texture_requested(row, col, zoom)
            # A finished texture is served from its cache file, with no
            # tile in memory (see disk_texture.py).
            direct = getortho.open_disk_texture(self.tc, row, col, maptype, zoom)
            if direct is not None:
                native = self._native
                fh, table = (
                    self._register_native(native, direct) if native is not None else (0, -1)
                )
                if not fh:
                    native = None
                    fh = next(self._direct_fh)
                with self._direct_lock:
                    self._direct[fh] = _DirectHandle(
                        direct, native=fh if native else 0, native_reads=native,
                        native_table=table,
                    )
                self._set_dds_size_cached(row, col, maptype, zoom, direct.size)
                if trace_started is not None:
                    self._trace_open(
                        path, trace_started,
                        "partial" if getattr(direct, "finest_level", 0) else "direct",
                        native=bool(native),
                    )
                return fh
            with profile_span(
                "fuse.dds_open",
                tile_id=tile_id,
                details={"path": path},
            ):
                t = self.tc._open_tile(row, col, maptype, zoom)
            try:
                self._set_dds_size_cached(row, col, maptype, zoom, t.dds.total_size)
            except Exception:
                log.debug(f"OPEN: Failed getting cache size for tile at {path}")
                pass
            if trace_started is not None:
                self._trace_open(path, trace_started, "live")
            return 0

        if path.endswith('AOISWORKING'):
            return 0

        ter_match = self.ter_tile_re.match(path)
        if ter_match:
            try:
                row, col, maptype, zoom = ter_match.groups()
                getortho.note_terrain_opened(
                    int(row), int(col), self._resolve_dds_maptype(maptype), int(zoom)
                )
            except Exception as exc:  # load-ahead is best effort
                log.debug(f"Load-ahead could not note {path}: {exc}")

        if self._stream is not None and not os.path.exists(full_path):
            rel = self._stream_rel(path)
            if rel.startswith("terrain/") and rel.endswith(".ter"):
                data = self._stream.ter_bytes(rel.split("/", 1)[1])
                if data is None:
                    raise FuseOSError(errno.ENOENT)
                fh = next(self._virtual_fh)
                with self._virtual_lock:
                    self._virtual_files[fh] = data
                return fh
            if rel.endswith(".png") and self._stream.entry(rel) is not None:
                if self._stream.ensure_local(rel) is None:
                    # X-Plane stops with an error on a missing terrain mask;
                    # a blank one keeps it flying (default water on that tile).
                    log.warning(f"OPEN: water mask [{path}] could not be streamed "
                                "in time; serving a blank stand-in until the scenery reloads")
                    self._stream_placeholders.add(rel)
                    fh = next(self._virtual_fh)
                    with self._virtual_lock:
                        self._virtual_files[fh] = self._stream.placeholder_png(rel)
                    return fh
                self._stream_placeholders.discard(rel)
        elif self._stream is not None and path.endswith(".png"):
            self._stream.touch(full_path)

        if system_type == 'windows':
            return os.open(full_path, flags | os.O_BINARY)
        return os.open(full_path, flags)

    def _trace_open(self, path, started, tier, native=False):
        """A texture open in the read trace, timed like a read: ``t`` is when
        it ended, ``ms`` how long it took, ``tier`` what will answer it."""
        trace = self._read_trace
        if trace is None:
            return
        trace.event(
            "open", path, ms=round((time.monotonic() - started) * 1000.0, 3),
            tier=tier, native=True if native else None, tid=threading.get_ident(),
            pid=_get_fuse_pid_for_log(),
        )

    def _switch_direct_to_live(self, direct, row, col, maptype, zoom):
        """Serve the rest of a direct open from a live tile.

        The cache file stays open until release: another read of this open
        may still be using it.
        """
        with direct.lock:
            if direct.live:
                return
            if direct.native and direct.native_reads is not None:
                # Native code must not answer from the file any more: one
                # open is served from one source.
                direct.native_reads.disable(direct.native)
            self.tc._open_tile(row, col, maptype, zoom)
            direct.live = True
        getortho.bump("dds_direct_fallbacks")

    # -- native reads ---------------------------------------------------------

    def _attach_native(self, ops):
        native = self._native_candidate
        if native is None:
            return
        mount = native.install(ops)
        if mount < 0:
            log.warning("Native texture reads: no free mount entry, reads stay in Python")
            return
        self._native = native
        self._native_mount = mount
        _start_native_drain(native)
        log.info(f"Native texture reads on for {self.root} ({native.version})")

    def _detach_native(self):
        native, self._native = self._native, None
        if native is not None:
            native.uninstall(self._native_mount)
            self._native_mount = -1

    def _register_native(self, native, texture):
        """``(tagged handle, season table entry)`` of a texture native code
        can answer, or ``(0, -1)``."""
        try:
            spec = texture.native_spec()
            if spec is None:
                return 0, -1
            handle, table = native.register(**spec)
        except Exception as exc:
            log.debug(f"Native registration of {texture.tile_id} failed: {exc}")
            return 0, -1
        if not handle:
            _bump_stat("dds_native_register_failed")
            return 0, -1
        texture.classified_natively = True
        texture.on_rows_verified = lambda rows, h=handle: native.mark_verified(h, rows)
        with _native_registry_lock:
            _native_registry[handle] = texture
        _bump_stat("dds_native_opens")
        if spec.get("finest_level"):
            _bump_stat("dds_native_partial_opens")
        if table >= 0:
            _bump_stat("dds_native_season_opens")
        return handle, table

    def _unregister_native(self, direct):
        native = direct.native_reads
        if not direct.native or native is None:
            return
        try:
            samples = native.unregister(direct.native)
            native.release_table(direct.native_table)
        except Exception as exc:
            log.error(f"Native read slot release failed: {exc}")
            samples = []
        with _native_registry_lock:
            _native_registry.pop(direct.native, None)
        if samples:
            _feed_native_samples(direct.texture, samples)

    def _create(self, path, mode, fi=None):
        uid, gid, pid = fuse_get_context()
        full_path = self._full_path(path)
        fd = os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)
        os.chown(full_path,uid,gid) #chown to context uid & gid
        return fd

    def read(self, path, length, offset, fh):
        trace = self._read_trace
        if trace is None or not self.dds_re.match(path):
            return self._read(path, length, offset, fh)
        direct = self._direct.get(fh) if fh else None
        tier = "live"
        if direct is not None and not direct.live:
            tier = "partial" if getattr(direct.texture, "finest_level", 0) else "direct"
        started = time.monotonic()
        data = self._read(path, length, offset, fh)
        if tier != "live" and direct.live:
            tier += "_to_live"  # the file could not answer: switched this open
        # The thread is the file system's, not X-Plane's: reads in flight on
        # different threads at once show how many X-Plane overlaps (with the
        # start, t - ms).
        trace.event(
            "read", path, offset=int(offset), length=int(length),
            ms=round((time.monotonic() - started) * 1000.0, 2),
            tier=tier, tid=threading.get_ident(), got=len(data),
        )
        return data

    #@profile
    #@lru_cache
    def _read(self, path, length, offset, fh):
        if TRACE_READS:
            log.debug(f"READ: {path} {offset} {length} {fh}")
        m = self.dds_re.match(path)
        if m:
            row, col, maptype, zoom = m.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            maptype = self._resolve_dds_maptype(maptype)
            # A read at or past the end gets nothing and one that straddles
            # it is cut at the end, as a plain file answers. Without this a
            # live texture answered every read with a full piece, so a
            # program that reads to end-of-file (an antivirus, an indexer, a
            # copy) never stopped and kept the worker busy.
            size = self._dds_size_for_read(row, col, maptype, zoom)
            if size is not None:
                if offset >= size or length <= 0:
                    _bump_stat("fuse_reads_past_end")
                    return b""
                if offset + length > size:
                    length = size - offset
            if getortho.is_shutdown_requested():
                log.info(f"Shutdown in progress, returning fallback data for {path}")
                return self._fallback_dds(row, col, maptype, zoom, offset, length, "shutdown")
            if _DDS_FAULTS.active and _DDS_FAULTS.should_fail(row, col, zoom):
                return self._fallback_dds(row, col, maptype, zoom, offset, length, "injected")
            direct = self._direct.get(fh) if fh else None
            if direct is not None and not direct.live:
                try:
                    return direct.texture.read(offset, length)
                except disk_texture.DiskTextureFailed as exc:
                    if isinstance(exc, disk_texture.DiskTextureLevelMissing):
                        # A partial entry: X-Plane came closer than the
                        # paging model expected. Expected, not a fault.
                        log.debug(f"Direct read of {path} wants a finer level, serving it live: {exc}")
                    elif isinstance(exc, disk_texture.DiskTextureStale):
                        log.debug(f"Cache entry of {path} changed during the open, serving it live: {exc}")
                    else:
                        log.warning(f"Direct read of {path} failed, serving it live: {exc}")
                    try:
                        self._switch_direct_to_live(direct, row, col, maptype, zoom)
                    except Exception as switch_exc:
                        log.error(f"Could not serve {path} live: {switch_exc}")
                        return self._fallback_dds(row, col, maptype, zoom, offset, length, "direct_failed")
            key = self._tile_key(row, col, maptype, zoom)
            tile_id = f"{row}_{col}_{maptype}_{zoom}"
            request_started = time.monotonic()
            lock = self._tile_locks.checkout(key)
            lock_held = False

            # Calculate build_timeout dynamically from the tile budget (wait_bands)
            # This ensures the FUSE lock timeout is always >= the tile build time
            # Formula: tile budget + fallback_timeout (if enabled) + 15s buffer
            #
            # The buffer accounts for:
            # - DDS read/write operations after tile is built
            # - Any processing overhead
            # - Margin of safety to prevent premature lock timeout
            try:
                build_timeout = self._calculate_build_timeout()
                request_deadline = request_started + build_timeout

                lock_started = time.monotonic()
                lock_held = lock.acquire(
                    timeout=max(0.0, request_deadline - time.monotonic())
                )
            finally:
                if not lock_held:
                    # Timed out or failed before holding it: this read is
                    # done with the lock either way.
                    self._tile_locks.checkin(key)
            if not lock_held:
                lock_wait_ms = (time.monotonic() - lock_started) * 1000.0
                record_stage(
                    "fuse.tile_lock_wait",
                    lock_wait_ms,
                    tile_id=tile_id,
                    outcome="timeout",
                )
                record_stage(
                    "fuse.dds_read",
                    (time.monotonic() - request_started) * 1000.0,
                    tile_id=tile_id,
                    outcome="fallback",
                    details={
                        "reason": "tile_lock_timeout",
                        "offset": offset,
                        "length": length,
                    },
                )
                # CRITICAL FIX: Instead of raising EIO (which causes CTD on Windows
                # due to EXCEPTION_IN_PAGE_ERROR), return fallback placeholder data.
                # X-Plane will show a gray/missing texture, but won't crash.
                log.error(f"Tile build lock timeout for {key} after {build_timeout}s - returning fallback data")

                # ═══════════════════════════════════════════════════════════════
                # ENHANCED DIAGNOSTICS: Log tile state to help debug lock stalls
                # ═══════════════════════════════════════════════════════════════
                try:
                    t = self.tc.tiles.get(self.tc._to_tile_id(row, col, maptype, zoom))
                    if t:
                        diag_refs = getattr(t, 'refs', 'N/A')
                        diag_ready = t.ready.is_set() if hasattr(t, 'ready') else 'N/A'
                        diag_dds = t.dds is not None if hasattr(t, 'dds') else 'N/A'
                        diag_chunks = len(t.chunks) if hasattr(t, 'chunks') else 'N/A'
                        diag_budget = None
                        if hasattr(t, '_tile_time_budget') and t._tile_time_budget:
                            budget = t._tile_time_budget
                            diag_budget = f"elapsed={budget.elapsed:.1f}s, exhausted={budget.exhausted}"

                        log.error(f"  DIAGNOSTIC: tile={t}, refs={diag_refs}, ready={diag_ready}, "
                                 f"has_dds={diag_dds}, chunk_zooms={diag_chunks}, budget={diag_budget}")
                    else:
                        log.error(f"  DIAGNOSTIC: Tile not found in cache (may have been evicted)")
                except Exception as diag_err:
                    log.error(f"  DIAGNOSTIC: Failed to gather tile state: {diag_err}")
                # ═══════════════════════════════════════════════════════════════

                return self._fallback_dds(row, col, maptype, zoom, offset, length, "lock_timeout")

            read_outcome = "ok"
            admitted = False
            weight = 0
            t = None
            try:
                # Inside the try: the lock is held from here on, and only
                # the finally below gives it back.
                record_stage(
                    "fuse.tile_lock_wait",
                    (time.monotonic() - lock_started) * 1000.0,
                    tile_id=tile_id,
                )
                with profile_span(
                    "tile.cache_lookup",
                    tile_id=tile_id,
                ):
                    t = self.tc._get_tile(row, col, maptype, zoom)
                # Admission bounds detailed builds. Header chunks and
                # base-level reads skip it: queued behind near reads that
                # wait up to a minute while a flight loads, they would time
                # out into the fallback texture instead of the base level.
                needs_slot = getattr(t, "needs_live_slot", None)
                if needs_slot is None or needs_slot(offset, length):
                    slot_weight = getattr(t, "live_slot_weight", None)
                    weight = slot_weight(offset, length) if slot_weight else 0
                    admitted = getortho.acquire_live_tile_slot(
                        max(0.0, request_deadline - time.monotonic()),
                        weight=weight,
                    )
                    if not admitted:
                        read_outcome = "fallback"
                        log.error(
                            "Live tile admission timeout for %s after %ss",
                            key,
                            build_timeout,
                        )
                        return self._fallback_dds(row, col, maptype, zoom, offset, length, "admission_timeout", tile=t)
                # Tile/build coordinators provide the fine-grained safety below.
                # Release the FUSE lookup lock before cache, network, decode, or
                # compression work so disjoint row reads can proceed together.
                lock_held = False
                self._tile_locks.release(key, lock)
                data = t.read_dds_bytes(offset, length)
                if data is None:
                    read_outcome = "fallback"
                    log.error(f"Tile read returned None for {key} - returning fallback data")
                    return self._fallback_dds(row, col, maptype, zoom, offset, length, "read_none", tile=t)
                return data
            except FuseOSError:
                read_outcome = "fallback"
                log.error(f"FUSE error for tile {key} - returning fallback data to prevent CTD")
                return self._fallback_dds(row, col, maptype, zoom, offset, length, "fuse_error", tile=t)
            except Exception as e:
                read_outcome = "fallback"
                log.error(f"Tile read/build failed for {key} - returning fallback data to prevent CTD")
                log.exception("cause:", exc_info=e)
                return self._fallback_dds(row, col, maptype, zoom, offset, length, "exception", tile=t)
            finally:
                try:
                    if admitted:
                        getortho.release_live_tile_slot(weight=weight)
                finally:
                    # Even if giving the slot back failed: a tile lock left
                    # held would time out every later read of this texture.
                    if lock_held:
                        lock_held = False
                        self._tile_locks.release(key, lock)
                record_stage(
                    "fuse.dds_read",
                    (time.monotonic() - request_started) * 1000.0,
                    tile_id=tile_id,
                    outcome=read_outcome,
                    details={
                        "offset": offset,
                        "length": length,
                    },
                )

        virtual = self._virtual_files.get(fh)
        if virtual is not None:
            return virtual[offset:offset + length]

        # Regular file passthrough
        with self.fh_locks.setdefault(fh, threading.Lock()):
            os.lseek(fh, offset, os.SEEK_SET)
            try:
                return os.read(fh, length)
            except OSError as e:
                raise FuseOSError(e.errno)

    # X-Plane never writes to files in the FUSE mount
    def _write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def truncate(self, path, length, fh=None):
        log.debug(f"TRUNCATE")
        full_path = self._full_path(path)
        with open(full_path, 'r+') as f:
            f.truncate(length)

    # X-Plane never writes to files in the FUSE mount
    def _flush(self, path, fh):
        # No-op for virtual DDS files; fsync real files.
        if self.dds_re.match(path):
            return 0
        try:
            os.fsync(fh)
        except OSError as e:
            raise FuseOSError(e.errno)
        return 0

    def releasedir(self, path, fh):
        if TRACE_READS:
            log.debug(f"RELEASEDIR: {path}")
        return 0

    def opendir(self, path):
        if TRACE_READS:
            log.debug(f"OPENDIR: {path}")
        return 0

    #@locked
    def release(self, path, fh):
        if TRACE_READS:
            log.debug(f"RELEASE: {path}")
        
        # Unregister DSF close for time exclusion tracking
        if self.dsf_re.match(path):
            log.debug(f"RELEASE: DSF closed: {path}")
            time_exclusion_manager.register_dsf_close(path)
            
            # Clean up redirected DSF tracking
            if fh in self._redirected_dsf_fhs:
                log.debug(f"RELEASE: Cleaning up redirected DSF handle: {fh}")
                del self._redirected_dsf_fhs[fh]
        
        dds_match = self.dds_re.match(path)
        if dds_match:
            trace = self._read_trace
            if trace is None:
                return self._release_dds(dds_match, fh)
            # ``t`` is when the release ended and ``ms`` how long it took,
            # as for an open.
            started = time.monotonic()
            try:
                return self._release_dds(dds_match, fh)
            finally:
                trace.event(
                    "release", path,
                    ms=round((time.monotonic() - started) * 1000.0, 3),
                    tid=threading.get_ident(),
                )
        if fh in self._virtual_files:
            with self._virtual_lock:
                self._virtual_files.pop(fh, None)
            return 0
        try:
            return os.close(fh)
        finally:
            self.fh_locks.pop(fh, None)

    def _release_dds(self, dds_match, fh):
        """Release one open of a texture: its cache file or its live tile."""
        row, col, maptype, zoom = dds_match.groups()
        row = int(row)
        col = int(col)
        zoom = int(zoom)
        maptype = self._resolve_dds_maptype(maptype)
        if fh:
            with self._direct_lock:
                direct = self._direct.pop(fh, None)
            if direct is not None:
                # Waits for native reads in flight, then closes the slot.
                self._unregister_native(direct)
                with direct.lock:
                    direct.texture.close()
                    live = direct.live
                if live:
                    self.tc._close_tile(row, col, maptype, zoom)
                return 0
        self.tc._close_tile(row, col, maptype, zoom)
        return 0

    # X-Plane never writes to files in the FUSE mount
    def _fsync(self, path, fdatasync, fh):
        if TRACE_READS:
            log.debug(f"FSYNC: {path}")
        return self.flush(path, fh)


    def close(self, path=None, fh=None):
        if TRACE_READS:
            log.debug(f"CLOSE: {path}")
        if path is None and fh is None:
            self.shutdown()
        return 0


def do_fuse_exit(fuse_ptr=None):
    log.info("fuse_exit called")
    #time.sleep(1)
    if not fuse_ptr:
        fuse_ptr = ctypes.c_void_p(_libfuse.fuse_get_context().contents.fuse)
    _libfuse.fuse_exit(fuse_ptr)


def _trace_position():
    """The aircraft position for the read trace (None without one)."""
    camera = getortho.current_camera()
    if camera is None:
        return None
    lat, lon, agl_m, hdg, speed = camera
    return {
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "agl_m": round(float(agl_m), 1),
        "hdg": round(float(hdg or 0.0), 1),
        "gs": round(float(speed or 0.0), 1),
    }


def run(ao, mountpoint, name="", nothreads=False):
    log.info(f"MOUNT: {mountpoint}")
    options = fuse_option_profiles_by_os(nothreads, name)

    log.info(f"Starting FUSE mount")
    log.info(f"Loading FUSE with options: "
            f"{', '.join(sorted(map(str, options.keys())))}")

    try:
        with native_reads_for(ao, FUSE):
            FUSE(ao, os.path.abspath(mountpoint), **options)
        log.info(f"FUSE: Exiting mount {mountpoint}")
        return
    except Exception as e:
        log.error(f"FUSE mount failed with non-negotiable error: {e}")
        raise
    finally:
        ao.shutdown()

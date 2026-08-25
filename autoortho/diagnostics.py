"""Low-overhead, session-scoped performance and memory diagnostics."""

from __future__ import annotations

import functools
import heapq
import json
import logging
import math
import os
import platform
import re
import socket
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psutil

log = logging.getLogger(__name__)

_PROFILE_ENV_ENABLED = "AO_PROFILE_ENABLED"
_PROFILE_ENV_SESSION_DIR = "AO_PROFILE_SESSION_DIR"
_PROFILE_ENV_SESSION_ID = "AO_PROFILE_SESSION_ID"
_PROFILE_ENV_INTERVAL = "AO_PROFILE_INTERVAL"
_PROFILE_ENV_SLOW_MS = "AO_PROFILE_SLOW_MS"
_PROFILE_ENV_MAX_EVENTS = "AO_PROFILE_MAX_EVENTS"
_PROFILE_ENV_ALLOCATIONS = "AO_PROFILE_ALLOCATIONS"

_HISTOGRAM_BOUNDS_MS = (
    0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0,
    250.0, 500.0, 1_000.0, 2_500.0, 5_000.0, 10_000.0,
    30_000.0, 60_000.0, 120_000.0, math.inf,
)
_MAX_DETAIL_ITEMS = 16
_MAX_DETAIL_LENGTH = 512
_MAX_SELF_MEMORY_SAMPLES = 36_000
_MAX_OBSERVED_MEMORY_SAMPLES = 7_200

_active_profiler: Optional["PerformanceProfiler"] = None
_active_profiler_lock = threading.RLock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _safe_detail_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_DETAIL_LENGTH]
    if isinstance(value, (list, tuple)):
        return [_safe_detail_value(item) for item in value[:_MAX_DETAIL_ITEMS]]
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_DETAIL_ITEMS:
                break
            result[str(key)[:64]] = _safe_detail_value(item)
        return result
    return repr(value)[:_MAX_DETAIL_LENGTH]


def _sanitize_details(details: Optional[dict]) -> dict:
    return _safe_detail_value(details or {})


def _safe_file_component(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._") or "process"


def _sanitize_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_json(item) for item in value]
    return repr(value)


def _append_bounded_sample(samples: list[tuple], sample: tuple, limit: int) -> None:
    samples.append(sample)
    if len(samples) <= limit:
        return

    compacted = [samples[0]]
    for index in range(1, len(samples), 2):
        pair = samples[index:index + 2]
        if len(pair) == 1:
            compacted.append(pair[0])
            continue
        first, second = pair
        compacted.append(
            (
                second[0],
                second[1],
                max(first[2], second[2]),
                max(first[3], second[3]),
                max(first[4], second[4]),
                max(first[5], second[5]),
                max(first[6], second[6]),
                max(first[7], second[7]),
                max(first[8], second[8]),
                max(first[9], second[9]),
            )
        )
    samples[:] = compacted


@dataclass
class StageAggregate:
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = math.inf
    max_ms: float = 0.0
    error_count: int = 0
    histogram: list[int] = field(
        default_factory=lambda: [0] * len(_HISTOGRAM_BOUNDS_MS)
    )
    outcomes: Counter = field(default_factory=Counter)

    def observe(self, duration_ms: float, outcome: str) -> None:
        duration_ms = max(0.0, float(duration_ms))
        self.count += 1
        self.total_ms += duration_ms
        self.min_ms = min(self.min_ms, duration_ms)
        self.max_ms = max(self.max_ms, duration_ms)
        self.outcomes[outcome] += 1
        if outcome in {"error", "timeout", "failed", "fallback"}:
            self.error_count += 1
        for index, bound in enumerate(_HISTOGRAM_BOUNDS_MS):
            if duration_ms <= bound:
                self.histogram[index] += 1
                break

    def percentile(self, percentile: float) -> float:
        if self.count <= 0:
            return 0.0
        target = max(1, math.ceil(self.count * percentile))
        cumulative = 0
        for bound, count in zip(_HISTOGRAM_BOUNDS_MS, self.histogram):
            cumulative += count
            if cumulative >= target:
                return self.max_ms if math.isinf(bound) else bound
        return self.max_ms

    def to_dict(self) -> dict:
        minimum = 0.0 if self.count == 0 or math.isinf(self.min_ms) else self.min_ms
        return {
            "count": self.count,
            "total_ms": round(self.total_ms, 3),
            "avg_ms": round(self.total_ms / self.count, 3) if self.count else 0.0,
            "min_ms": round(minimum, 3),
            "p50_ms": round(self.percentile(0.50), 3),
            "p95_ms": round(self.percentile(0.95), 3),
            "p99_ms": round(self.percentile(0.99), 3),
            "max_ms": round(self.max_ms, 3),
            "error_count": self.error_count,
            "outcomes": dict(self.outcomes),
            "histogram_bounds_ms": [
                "inf" if math.isinf(bound) else bound
                for bound in _HISTOGRAM_BOUNDS_MS
            ],
            "histogram_counts": list(self.histogram),
        }


@dataclass
class GaugeAggregate:
    current: float
    minimum: float
    maximum: float
    samples: int = 1

    def observe(self, value: float) -> None:
        self.current = value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        self.samples += 1

    def to_dict(self) -> dict:
        return {
            "current": self.current,
            "min": self.minimum,
            "max": self.maximum,
            "samples": self.samples,
        }


class ProfileSpan:
    def __init__(
        self,
        profiler: Optional["PerformanceProfiler"],
        stage: str,
        tile_id: Optional[str] = None,
        details: Optional[dict] = None,
    ):
        self._profiler = profiler
        self.stage = stage
        self.tile_id = tile_id
        self.details = details or {}
        self.outcome = "ok"
        self._start = 0.0

    def __enter__(self) -> "ProfileSpan":
        if self._profiler is not None:
            self._start = time.perf_counter()
        return self

    def annotate(self, **details: Any) -> None:
        self.details.update(details)

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._profiler is None:
            return False
        if exc_type is not None:
            self.outcome = "error"
            self.details.setdefault("error", exc_type.__name__)
        duration_ms = (time.perf_counter() - self._start) * 1000.0
        self._profiler.record(
            self.stage,
            duration_ms,
            tile_id=self.tile_id,
            outcome=self.outcome,
            details=self.details,
        )
        return False


class PerformanceProfiler:
    """Collect timing histograms, slow operations, gauges, and process samples."""

    def __init__(
        self,
        session_dir: Path,
        session_id: str,
        role: str,
        sample_interval: float = 1.0,
        slow_operation_ms: float = 250.0,
        max_slow_operations: int = 200,
        trace_python_allocations: bool = False,
        metadata: Optional[dict] = None,
    ):
        self.session_dir = Path(session_dir)
        self.session_id = str(session_id)
        self.role = str(role)
        self.sample_interval = sample_interval
        self.slow_operation_ms = slow_operation_ms
        self.max_slow_operations = max_slow_operations
        self.trace_python_allocations = trace_python_allocations
        self.metadata = _sanitize_json(metadata or {})
        self.pid = os.getpid()
        self.started_wall = time.time()
        self.started_monotonic = time.monotonic()
        self.started_at = _utc_now_iso()
        self._stages: dict[str, StageAggregate] = {}
        self._gauges: dict[str, GaugeAggregate] = {}
        self._slow_operations: list[tuple[float, int, dict]] = []
        self._memory_samples: list[tuple] = []
        self._observed_processes: dict[int, dict] = {}
        self._sequence = 0
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None
        self._process: Optional[psutil.Process] = None
        self._tracemalloc = None
        self._allocation_start = None
        self._stopped = False

    def start(self) -> "PerformanceProfiler":
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._process = psutil.Process(self.pid)
        try:
            self._process.cpu_percent(interval=None)
        except (psutil.Error, OSError):
            pass

        if self.trace_python_allocations:
            try:
                import tracemalloc

                tracemalloc.start(10)
                self._tracemalloc = tracemalloc
                self._allocation_start = tracemalloc.take_snapshot()
            except Exception as exc:
                log.warning("Python allocation tracing could not start: %s", exc)
                self._tracemalloc = None

        self._sample_process()
        self._sampler_thread = threading.Thread(
            target=self._sample_loop,
            name=f"AO-Profiler-{self.pid}",
            daemon=True,
        )
        self._sampler_thread.start()
        log.info(
            "Performance diagnostics started: role=%s session=%s",
            self.role,
            self.session_id,
        )
        return self

    def span(
        self,
        stage: str,
        tile_id: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> ProfileSpan:
        return ProfileSpan(self, stage, tile_id=tile_id, details=details)

    def record(
        self,
        stage: str,
        duration_ms: float,
        tile_id: Optional[str] = None,
        outcome: str = "ok",
        details: Optional[dict] = None,
    ) -> None:
        if self._stopped:
            return
        stage = str(stage)
        outcome = str(outcome or "ok")
        with self._lock:
            aggregate = self._stages.setdefault(stage, StageAggregate())
            aggregate.observe(duration_ms, outcome)
            if duration_ms < self.slow_operation_ms:
                return

            self._sequence += 1
            event = {
                "stage": stage,
                "duration_ms": round(float(duration_ms), 3),
                "tile_id": str(tile_id) if tile_id is not None else None,
                "outcome": outcome,
                "thread": threading.current_thread().name,
                "elapsed_s": round(time.monotonic() - self.started_monotonic, 3),
                "timestamp": _utc_now_iso(),
                "details": _sanitize_details(details),
            }
            item = (float(duration_ms), self._sequence, event)
            if len(self._slow_operations) < self.max_slow_operations:
                heapq.heappush(self._slow_operations, item)
            elif item[0] > self._slow_operations[0][0]:
                heapq.heapreplace(self._slow_operations, item)

    def set_gauge(self, name: str, value: Any) -> None:
        if self._stopped:
            return
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            gauge = self._gauges.get(name)
            if gauge is None:
                self._gauges[name] = GaugeAggregate(
                    numeric_value, numeric_value, numeric_value
                )
            else:
                gauge.observe(numeric_value)

    def child_environment(self) -> dict[str, str]:
        return {
            _PROFILE_ENV_ENABLED: "1",
            _PROFILE_ENV_SESSION_DIR: str(self.session_dir),
            _PROFILE_ENV_SESSION_ID: self.session_id,
            _PROFILE_ENV_INTERVAL: str(self.sample_interval),
            _PROFILE_ENV_SLOW_MS: str(self.slow_operation_ms),
            _PROFILE_ENV_MAX_EVENTS: str(self.max_slow_operations),
            _PROFILE_ENV_ALLOCATIONS: "1" if self.trace_python_allocations else "0",
        }

    def register_process(self, pid: int, role: str) -> None:
        """Sample a child process so memory data survives an ungraceful exit."""
        try:
            process = psutil.Process(int(pid))
            process.cpu_percent(interval=None)
        except (psutil.Error, OSError, ValueError):
            return
        entry = {
            "pid": int(pid),
            "role": str(role),
            "process": process,
            "started_wall_time": time.time(),
            "samples": [],
        }
        with self._lock:
            self._observed_processes[int(pid)] = entry
        self._sample_observed_process(entry)

    def stop(
        self,
        stats_snapshot: Optional[dict] = None,
        finalize_session: bool = False,
    ) -> Optional[Path]:
        if self._stopped:
            return self.session_dir / "report.md" if finalize_session else None
        self._stopped = True
        self._stop_event.set()
        if self._sampler_thread and self._sampler_thread.is_alive():
            self._sampler_thread.join(timeout=max(2.0, self.sample_interval * 2.0))
        self._sample_process()

        allocation_stats = self._collect_allocation_stats()
        profile = self._build_process_profile(allocation_stats)
        process_path = self.session_dir / (
            f"process-{self.pid}-{_safe_file_component(self.role)}.json"
        )
        _atomic_json_write(process_path, profile)
        self._write_observed_process_profiles()

        report_path = None
        if finalize_session:
            report_path = finalize_session_report(
                self.session_dir,
                self.session_id,
                stats_snapshot=stats_snapshot,
            )
        log.info("Performance diagnostics stopped: %s", process_path)
        return report_path

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self.sample_interval):
            self._sample_process()
            with self._lock:
                observed = list(self._observed_processes.values())
            for entry in observed:
                self._sample_observed_process(entry)

    def _sample_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            memory = process.memory_info()
            rss = int(memory.rss)
            vms = int(memory.vms)
            uss = 0
            try:
                full_memory = process.memory_full_info()
                uss = int(getattr(full_memory, "uss", 0) or 0)
            except (
                AttributeError,
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                NotImplementedError,
            ):
                pass
            cpu_percent = float(process.cpu_percent(interval=None))
            threads = int(process.num_threads())
            io_read = 0
            io_write = 0
            try:
                io_counters = process.io_counters()
                io_read = int(getattr(io_counters, "read_bytes", 0) or 0)
                io_write = int(getattr(io_counters, "write_bytes", 0) or 0)
            except (
                AttributeError,
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                NotImplementedError,
            ):
                pass

            physical_footprint = 0
            if sys.platform == "darwin":
                try:
                    from autoortho.aostats import _get_macos_phys_footprint
                except ImportError:
                    try:
                        from aostats import _get_macos_phys_footprint
                    except ImportError:
                        _get_macos_phys_footprint = None
                if _get_macos_phys_footprint is not None:
                    physical_footprint = int(_get_macos_phys_footprint() or 0)

            sample = (
                round(time.time(), 3),
                round(time.monotonic() - self.started_monotonic, 3),
                rss,
                uss,
                vms,
                physical_footprint,
                round(cpu_percent, 2),
                threads,
                io_read,
                io_write,
            )
            with self._lock:
                _append_bounded_sample(
                    self._memory_samples,
                    sample,
                    _MAX_SELF_MEMORY_SAMPLES,
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return

    def _sample_observed_process(self, entry: dict) -> None:
        process = entry["process"]
        try:
            memory = process.memory_info()
            uss = 0
            try:
                full_memory = process.memory_full_info()
                uss = int(getattr(full_memory, "uss", 0) or 0)
            except (
                AttributeError,
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                NotImplementedError,
            ):
                pass
            io_read = 0
            io_write = 0
            try:
                io_counters = process.io_counters()
                io_read = int(getattr(io_counters, "read_bytes", 0) or 0)
                io_write = int(getattr(io_counters, "write_bytes", 0) or 0)
            except (
                AttributeError,
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                NotImplementedError,
            ):
                pass
            sample = (
                round(time.time(), 3),
                round(time.monotonic() - self.started_monotonic, 3),
                int(memory.rss),
                uss,
                int(memory.vms),
                0,
                round(float(process.cpu_percent(interval=None)), 2),
                int(process.num_threads()),
                io_read,
                io_write,
            )
            with self._lock:
                _append_bounded_sample(
                    entry["samples"],
                    sample,
                    _MAX_OBSERVED_MEMORY_SAMPLES,
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return

    def _collect_allocation_stats(self) -> list[dict]:
        if self._tracemalloc is None:
            return []
        try:
            snapshot = self._tracemalloc.take_snapshot()
            if self._allocation_start is not None:
                statistics = snapshot.compare_to(self._allocation_start, "lineno")
            else:
                statistics = snapshot.statistics("lineno")
            result = []
            for stat in statistics[:25]:
                frame = stat.traceback[0]
                result.append(
                    {
                        "location": f"{frame.filename}:{frame.lineno}",
                        "size_bytes": int(stat.size),
                        "size_diff_bytes": int(getattr(stat, "size_diff", stat.size)),
                        "count": int(stat.count),
                        "count_diff": int(getattr(stat, "count_diff", stat.count)),
                    }
                )
            return result
        except Exception as exc:
            log.debug("Allocation snapshot failed: %s", exc)
            return []
        finally:
            try:
                self._tracemalloc.stop()
            except Exception:
                pass

    def _build_process_profile(self, allocation_stats: list[dict]) -> dict:
        with self._lock:
            stages = {
                name: aggregate.to_dict()
                for name, aggregate in sorted(self._stages.items())
            }
            gauges = {
                name: aggregate.to_dict()
                for name, aggregate in sorted(self._gauges.items())
            }
            slow_operations = [
                item[2]
                for item in sorted(
                    self._slow_operations,
                    key=lambda item: item[0],
                    reverse=True,
                )
            ]
            memory_samples = list(self._memory_samples)

        ended_wall = time.time()
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "role": self.role,
            "pid": self.pid,
            "started_at": self.started_at,
            "ended_at": _utc_now_iso(),
            "started_wall_time": self.started_wall,
            "ended_wall_time": ended_wall,
            "duration_seconds": round(ended_wall - self.started_wall, 3),
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": psutil.cpu_count(logical=True),
                "physical_cpu_count": psutil.cpu_count(logical=False),
                "total_memory_bytes": int(psutil.virtual_memory().total),
            },
            "settings": {
                "sample_interval_seconds": self.sample_interval,
                "slow_operation_ms": self.slow_operation_ms,
                "max_slow_operations": self.max_slow_operations,
                "python_allocation_tracing": self.trace_python_allocations,
            },
            "metadata": self.metadata,
            "stages": stages,
            "gauges": gauges,
            "slow_operations": slow_operations,
            "memory_samples": [
                {
                    "timestamp": sample[0],
                    "elapsed_s": sample[1],
                    "rss_bytes": sample[2],
                    "uss_bytes": sample[3],
                    "vms_bytes": sample[4],
                    "physical_footprint_bytes": sample[5],
                    "cpu_percent": sample[6],
                    "threads": sample[7],
                    "read_bytes": sample[8],
                    "write_bytes": sample[9],
                }
                for sample in memory_samples
            ],
            "python_allocations": allocation_stats,
        }

    def _write_observed_process_profiles(self) -> None:
        with self._lock:
            observed = list(self._observed_processes.values())
        for entry in observed:
            pid = entry["pid"]
            if list(self.session_dir.glob(f"process-{pid}-*.json")):
                continue
            samples = list(entry["samples"])
            if not samples:
                continue
            ended_wall = samples[-1][0]
            profile = {
                "schema_version": 1,
                "session_id": self.session_id,
                "role": entry["role"],
                "pid": pid,
                "started_at": datetime.fromtimestamp(
                    entry["started_wall_time"], timezone.utc
                ).isoformat(),
                "ended_at": datetime.fromtimestamp(
                    ended_wall, timezone.utc
                ).isoformat(),
                "started_wall_time": entry["started_wall_time"],
                "ended_wall_time": ended_wall,
                "duration_seconds": round(
                    max(0.0, ended_wall - entry["started_wall_time"]), 3
                ),
                "host": {
                    "hostname": socket.gethostname(),
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "cpu_count": psutil.cpu_count(logical=True),
                    "physical_cpu_count": psutil.cpu_count(logical=False),
                    "total_memory_bytes": int(psutil.virtual_memory().total),
                },
                "settings": {
                    "sample_interval_seconds": self.sample_interval,
                    "observed_by_parent": True,
                },
                "metadata": {
                    "observed_by_parent": True,
                    "note": "Worker did not write a graceful shutdown profile.",
                },
                "stages": {},
                "gauges": {},
                "slow_operations": [],
                "memory_samples": [
                    {
                        "timestamp": sample[0],
                        "elapsed_s": sample[1],
                        "rss_bytes": sample[2],
                        "uss_bytes": sample[3],
                        "vms_bytes": sample[4],
                        "physical_footprint_bytes": sample[5],
                        "cpu_percent": sample[6],
                        "threads": sample[7],
                        "read_bytes": sample[8],
                        "write_bytes": sample[9],
                    }
                    for sample in samples
                ],
                "python_allocations": [],
            }
            path = self.session_dir / (
                f"process-{pid}-{_safe_file_component(entry['role'])}-observed.json"
            )
            _atomic_json_write(path, profile)


def get_profiler() -> Optional[PerformanceProfiler]:
    return _active_profiler


def profile_span(
    stage: str,
    tile_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> ProfileSpan:
    return ProfileSpan(get_profiler(), stage, tile_id=tile_id, details=details)


def record_stage(
    stage: str,
    duration_ms: float,
    tile_id: Optional[str] = None,
    outcome: str = "ok",
    details: Optional[dict] = None,
) -> None:
    profiler = get_profiler()
    if profiler is not None:
        profiler.record(
            stage,
            duration_ms,
            tile_id=tile_id,
            outcome=outcome,
            details=details,
        )


def profile_gauge(name: str, value: Any) -> None:
    profiler = get_profiler()
    if profiler is not None:
        profiler.set_gauge(name, value)


def profiled_stage(stage: str, tile_arg: Optional[int] = None) -> Callable:
    """Decorate a function and record inclusive wall-clock duration."""

    def decorator(function: Callable) -> Callable:
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            tile_id = None
            if tile_arg is not None and len(args) > tile_arg:
                tile_id = args[tile_arg]
            elif args:
                tile_id = getattr(args[0], "id", None)
                if tile_id is None:
                    tile_id = getattr(args[0], "tile_id", None)

            with profile_span(stage, tile_id=tile_id) as span:
                result = function(*args, **kwargs)
                if isinstance(result, bool):
                    span.outcome = "ok" if result else "failed"
                elif result is None:
                    span.outcome = "miss"
                return result

        return wrapped

    return decorator


def start_parent_profiler(cfg: Any) -> Optional[PerformanceProfiler]:
    diagnostics_cfg = getattr(cfg, "diagnostics", None)
    if diagnostics_cfg is None or not _as_bool(
        getattr(diagnostics_cfg, "performance_profiling", False)
    ):
        return None

    report_root = Path(
        os.path.expanduser(
            str(
                getattr(
                    diagnostics_cfg,
                    "report_dir",
                    "~/.autoortho-data/reports",
                )
            )
        )
    )
    session_id = (
        datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    session_dir = report_root / f"performance-{session_id}"
    _apply_report_retention(
        report_root,
        _safe_int(
            getattr(diagnostics_cfg, "max_reports", 20),
            20,
            1,
            500,
        ),
    )

    metadata = {"config": _config_snapshot(cfg)}
    profiler = PerformanceProfiler(
        session_dir=session_dir,
        session_id=session_id,
        role="parent",
        sample_interval=_safe_number(
            getattr(diagnostics_cfg, "sample_interval_seconds", 1.0),
            1.0,
            0.1,
            60.0,
        ),
        slow_operation_ms=_safe_number(
            getattr(diagnostics_cfg, "slow_operation_ms", 250.0),
            250.0,
            1.0,
            300_000.0,
        ),
        max_slow_operations=_safe_int(
            getattr(diagnostics_cfg, "max_slow_operations", 200),
            200,
            10,
            10_000,
        ),
        trace_python_allocations=_as_bool(
            getattr(diagnostics_cfg, "python_allocation_tracing", False)
        ),
        metadata=metadata,
    )
    return _activate_profiler(profiler)


def start_worker_profiler_from_env(role: str) -> Optional[PerformanceProfiler]:
    if not _as_bool(os.getenv(_PROFILE_ENV_ENABLED), False):
        return None
    session_dir = os.getenv(_PROFILE_ENV_SESSION_DIR)
    session_id = os.getenv(_PROFILE_ENV_SESSION_ID)
    if not session_dir or not session_id:
        log.warning("Profiling was enabled without a session directory or ID")
        return None
    profiler = PerformanceProfiler(
        session_dir=Path(session_dir),
        session_id=session_id,
        role=f"mount-worker:{role}",
        sample_interval=_safe_number(
            os.getenv(_PROFILE_ENV_INTERVAL), 1.0, 0.1, 60.0
        ),
        slow_operation_ms=_safe_number(
            os.getenv(_PROFILE_ENV_SLOW_MS), 250.0, 1.0, 300_000.0
        ),
        max_slow_operations=_safe_int(
            os.getenv(_PROFILE_ENV_MAX_EVENTS), 200, 10, 10_000
        ),
        trace_python_allocations=_as_bool(
            os.getenv(_PROFILE_ENV_ALLOCATIONS), False
        ),
    )
    return _activate_profiler(profiler)


def stop_active_profiler(
    stats_snapshot: Optional[dict] = None,
    finalize_session: bool = False,
) -> Optional[Path]:
    global _active_profiler
    with _active_profiler_lock:
        profiler = _active_profiler
        _active_profiler = None
    if profiler is None:
        return None
    return profiler.stop(
        stats_snapshot=stats_snapshot,
        finalize_session=finalize_session,
    )


def _activate_profiler(profiler: PerformanceProfiler) -> PerformanceProfiler:
    global _active_profiler
    with _active_profiler_lock:
        if _active_profiler is not None:
            return _active_profiler
        _active_profiler = profiler.start()
        return _active_profiler


def _config_snapshot(cfg: Any) -> dict:
    sections = {}
    for section_name in (
        "diagnostics",
        "autoortho",
        "cache",
        "pydds",
        "fuse",
    ):
        section = getattr(cfg, section_name, None)
        if section is None:
            continue
        values = {}
        for key, value in vars(section).items():
            if key.startswith("_"):
                continue
            values[key] = _sanitize_json(value)
        sections[section_name] = values
    return sections


def _apply_report_retention(report_root: Path, max_reports: int) -> None:
    try:
        report_root.mkdir(parents=True, exist_ok=True)
        existing = sorted(
            (
                path
                for path in report_root.iterdir()
                if path.is_dir() and path.name.startswith("performance-")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old_dir in existing[max(0, max_reports - 1):]:
            for child in old_dir.iterdir():
                if child.is_file():
                    child.unlink()
            old_dir.rmdir()
    except OSError as exc:
        log.debug("Could not apply diagnostics report retention: %s", exc)


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def finalize_session_report(
    session_dir: Path,
    session_id: str,
    stats_snapshot: Optional[dict] = None,
) -> Path:
    process_profiles = []
    for path in sorted(Path(session_dir).glob("process-*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                profile = json.load(handle)
            if profile.get("session_id") == session_id:
                process_profiles.append(profile)
        except (OSError, ValueError) as exc:
            log.warning("Could not read diagnostics process profile %s: %s", path, exc)

    summary = _build_session_summary(
        session_id,
        process_profiles,
        stats_snapshot=stats_snapshot,
    )
    json_path = Path(session_dir) / "report.json"
    markdown_path = Path(session_dir) / "report.md"
    _atomic_json_write(json_path, summary)
    temporary = markdown_path.with_name(f".{markdown_path.name}.{os.getpid()}.tmp")
    temporary.write_text(_render_markdown(summary), encoding="utf-8")
    os.replace(temporary, markdown_path)
    return markdown_path


def _build_session_summary(
    session_id: str,
    process_profiles: list[dict],
    stats_snapshot: Optional[dict],
) -> dict:
    process_rows = []
    stage_rows = []
    slow_operations = []
    allocation_rows = []
    parent_metadata = {}

    for profile in process_profiles:
        if profile.get("role") == "parent":
            parent_metadata = profile.get("metadata", {})
        samples = profile.get("memory_samples", [])
        rss_values = [int(sample.get("rss_bytes", 0) or 0) for sample in samples]
        uss_values = [int(sample.get("uss_bytes", 0) or 0) for sample in samples]
        footprint_values = [
            int(sample.get("physical_footprint_bytes", 0) or 0)
            for sample in samples
        ]
        cpu_values = [float(sample.get("cpu_percent", 0) or 0) for sample in samples]
        thread_values = [int(sample.get("threads", 0) or 0) for sample in samples]
        process_rows.append(
            {
                "role": profile.get("role"),
                "pid": profile.get("pid"),
                "duration_seconds": profile.get("duration_seconds", 0),
                "peak_rss_bytes": max(rss_values, default=0),
                "peak_uss_bytes": max(uss_values, default=0),
                "peak_physical_footprint_bytes": max(footprint_values, default=0),
                "peak_memory_bytes": (
                    max(footprint_values, default=0)
                    or max(rss_values, default=0)
                ),
                "memory_metric": (
                    "physical footprint"
                    if max(footprint_values, default=0) > 0
                    else "RSS"
                ),
                "rss_growth_bytes": (
                    rss_values[-1] - rss_values[0] if len(rss_values) >= 2 else 0
                ),
                "peak_cpu_percent": max(cpu_values, default=0),
                "peak_threads": max(thread_values, default=0),
                "sample_count": len(samples),
            }
        )
        for stage, aggregate in profile.get("stages", {}).items():
            stage_rows.append(
                {
                    "role": profile.get("role"),
                    "pid": profile.get("pid"),
                    "stage": stage,
                    **aggregate,
                }
            )
        for operation in profile.get("slow_operations", []):
            slow_operations.append(
                {
                    "role": profile.get("role"),
                    "pid": profile.get("pid"),
                    **operation,
                }
            )
        for allocation in profile.get("python_allocations", []):
            allocation_rows.append(
                {
                    "role": profile.get("role"),
                    "pid": profile.get("pid"),
                    **allocation,
                }
            )

    process_rows.sort(key=lambda row: row["peak_memory_bytes"], reverse=True)
    stage_rows.sort(key=lambda row: row.get("total_ms", 0), reverse=True)
    slow_operations.sort(key=lambda row: row.get("duration_ms", 0), reverse=True)
    allocation_rows.sort(
        key=lambda row: row.get("size_diff_bytes", 0),
        reverse=True,
    )

    aggregate_memory = _aggregate_memory_timeline(process_profiles)
    peak_total_rss = max(
        (sample["total_rss_bytes"] for sample in aggregate_memory),
        default=0,
    )
    peak_total_uss = max(
        (sample["total_uss_bytes"] for sample in aggregate_memory),
        default=0,
    )
    peak_total_memory = max(
        (sample["total_effective_memory_bytes"] for sample in aggregate_memory),
        default=0,
    )
    diagnostics = _diagnostic_flags(
        process_rows,
        stage_rows,
        peak_total_memory,
        parent_metadata,
        process_profiles,
    )

    started = min(
        (profile.get("started_wall_time", math.inf) for profile in process_profiles),
        default=0,
    )
    ended = max(
        (profile.get("ended_wall_time", 0) for profile in process_profiles),
        default=0,
    )
    return {
        "schema_version": 1,
        "session_id": session_id,
        "generated_at": _utc_now_iso(),
        "duration_seconds": round(max(0.0, ended - started), 3) if started else 0,
        "process_count": len(process_profiles),
        "peak_total_memory_bytes": peak_total_memory,
        "peak_total_rss_bytes": peak_total_rss,
        "peak_total_uss_bytes": peak_total_uss,
        "processes": process_rows,
        "stages": stage_rows,
        "slow_operations": slow_operations,
        "aggregate_memory_timeline": aggregate_memory,
        "python_allocations": allocation_rows,
        "gauges": [
            {
                "role": profile.get("role"),
                "pid": profile.get("pid"),
                "name": name,
                **gauge,
            }
            for profile in process_profiles
            for name, gauge in profile.get("gauges", {}).items()
        ],
        "stats_snapshot": _sanitize_json(stats_snapshot or {}),
        "metadata": parent_metadata,
        "diagnostic_flags": diagnostics,
        "process_profiles": process_profiles,
    }


def _aggregate_memory_timeline(process_profiles: list[dict]) -> list[dict]:
    buckets: dict[int, dict[str, dict]] = {}
    ended_by_process: dict[str, float] = {}
    for profile in process_profiles:
        pid = int(profile.get("pid", 0) or 0)
        process_key = f"{pid}:{profile.get('role', '')}"
        ended_by_process[process_key] = float(
            profile.get("ended_wall_time", 0) or 0
        )
        for sample in profile.get("memory_samples", []):
            # The UI permits 0.1-second sampling, so retain decisecond buckets
            # instead of collapsing every sample from a process into one second.
            bucket = int(float(sample.get("timestamp", 0) or 0) * 10.0)
            buckets.setdefault(bucket, {})[process_key] = sample

    last_by_process: dict[str, dict] = {}
    result = []
    for timestamp_bucket in sorted(buckets):
        timestamp = timestamp_bucket / 10.0
        last_by_process.update(buckets[timestamp_bucket])
        for process_key in list(last_by_process):
            if timestamp > ended_by_process.get(process_key, timestamp) + 1.0:
                last_by_process.pop(process_key, None)
        total_footprint = sum(
            int(sample.get("physical_footprint_bytes", 0) or 0)
            for sample in last_by_process.values()
        )
        result.append(
            {
                "timestamp": timestamp,
                "total_rss_bytes": sum(
                    int(sample.get("rss_bytes", 0) or 0)
                    for sample in last_by_process.values()
                ),
                "total_uss_bytes": sum(
                    int(sample.get("uss_bytes", 0) or 0)
                    for sample in last_by_process.values()
                ),
                "total_physical_footprint_bytes": total_footprint,
                "total_effective_memory_bytes": sum(
                    int(sample.get("physical_footprint_bytes", 0) or 0)
                    or max(
                        int(sample.get("rss_bytes", 0) or 0),
                        int(sample.get("uss_bytes", 0) or 0),
                    )
                    for sample in last_by_process.values()
                ),
                "process_count": len(last_by_process),
            }
        )
    return result


def _diagnostic_flags(
    processes: list[dict],
    stages: list[dict],
    peak_total_memory: int,
    metadata: dict,
    process_profiles: Optional[list[dict]] = None,
) -> list[str]:
    flags = []
    by_stage = {}
    for row in stages:
        by_stage.setdefault(row["stage"], []).append(row)

    incomplete_workers = [
        profile
        for profile in (process_profiles or [])
        if str(profile.get("role", "")).startswith("mount-worker:")
        and bool(profile.get("settings", {}).get("observed_by_parent"))
    ]
    if incomplete_workers:
        flags.append(
            f"{len(incomplete_workers)} mount worker profile(s) were reconstructed "
            "from parent process samples after an ungraceful exit; flight-stage "
            "latency and worker gauges are incomplete."
        )

    def worst_p95(stage: str, *, worker_only: bool = False) -> float:
        rows = by_stage.get(stage, [])
        if worker_only:
            rows = [
                row
                for row in rows
                if str(row.get("role", "")).startswith("mount-worker:")
            ]
        return max(
            (float(row.get("p95_ms", 0) or 0) for row in rows),
            default=0.0,
        )

    if worst_p95("chunk.queue_wait", worker_only=True) > 100:
        flags.append(
            "Chunk queue p95 exceeds 100 ms; download workers are saturated or "
            "prefetch work is delaying live requests."
        )
    if worst_p95("network.http_request", worker_only=True) > 1_000:
        flags.append(
            "HTTP request p95 exceeds 1 second; network/provider latency is a "
            "primary contributor to tile delivery time."
        )
    if max(
        worst_p95("dds.buffer_pool_wait", worker_only=True),
        worst_p95("dds.builder_pool_wait", worker_only=True),
    ) > 50:
        flags.append(
            "DDS pool wait p95 exceeds 50 ms; builder concurrency is higher than "
            "available native buffers/builders."
        )
    if worst_p95("dds.native_mipmap_build", worker_only=True) > 500:
        flags.append(
            "Native mipmap build p95 exceeds 500 ms; decode/compression CPU work "
            "should be profiled against configured native thread concurrency."
        )
    if any(row.get("rss_growth_bytes", 0) > 256 * 1024 * 1024 for row in processes):
        flags.append(
            "At least one process retained more than 256 MiB of additional RSS "
            "during the session; inspect its gauges and Python allocation diff."
        )

    cache_limit_gb = (
        metadata.get("config", {})
        .get("cache", {})
        .get("cache_mem_limit")
    )
    try:
        cache_limit_bytes = float(cache_limit_gb) * 1024 ** 3
    except (TypeError, ValueError):
        cache_limit_bytes = 0
    if cache_limit_bytes and peak_total_memory > cache_limit_bytes:
        flags.append(
            "Observed aggregate peak RSS exceeded the configured memory cache "
            "limit; the limit does not cover all process/native overhead."
        )
    if not flags:
        flags.append(
            "No built-in threshold fired; rank stages by p95/max and inspect the "
            "slow-operation traces for the dominant path."
        )
    return flags


def _format_mib(value: Any) -> str:
    try:
        return f"{float(value) / (1024 ** 2):,.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _format_ms(value: Any) -> str:
    try:
        return f"{float(value):,.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render_markdown(summary: dict) -> str:
    lines = [
        "# AutoOrtho Flight Performance Report",
        "",
        f"- **Session:** `{summary['session_id']}`",
        f"- **Duration:** {summary['duration_seconds'] / 60.0:,.1f} minutes",
        f"- **Processes profiled:** {summary['process_count']}",
        f"- **Peak aggregate measured memory:** {_format_mib(summary['peak_total_memory_bytes'])} MiB",
        f"- **Peak aggregate RSS:** {_format_mib(summary['peak_total_rss_bytes'])} MiB",
        f"- **Peak aggregate USS:** {_format_mib(summary['peak_total_uss_bytes'])} MiB",
        "",
        "## Diagnostic flags",
        "",
    ]
    lines.extend(f"- {flag}" for flag in summary["diagnostic_flags"])

    lines.extend(
        [
            "",
            "## Memory by process",
            "",
            "| Process role | PID | Peak memory MiB | Metric | Peak RSS MiB | Peak USS MiB | RSS growth MiB | Peak CPU % | Peak threads |",
            "|---|---:|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary["processes"]:
        lines.append(
            f"| {_markdown_escape(row['role'])} | {row['pid']} | "
            f"{_format_mib(row['peak_memory_bytes'])} | "
            f"{_markdown_escape(row['memory_metric'])} | "
            f"{_format_mib(row['peak_rss_bytes'])} | "
            f"{_format_mib(row['peak_uss_bytes'])} | "
            f"{_format_mib(row['rss_growth_bytes'])} | "
            f"{row['peak_cpu_percent']:,.1f} | {row['peak_threads']} |"
        )

    lines.extend(
        [
            "",
            "## Slowest stages",
            "",
            "Durations are inclusive; nested stages can overlap and must not be added together.",
            "",
            "| Process role | Stage | Count | Avg ms | p50 ms | p95 ms | p99 ms | Max ms | Errors | Total s |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary["stages"][:50]:
        lines.append(
            f"| {_markdown_escape(row['role'])} | `{_markdown_escape(row['stage'])}` | "
            f"{row['count']} | {_format_ms(row['avg_ms'])} | "
            f"{_format_ms(row['p50_ms'])} | {_format_ms(row['p95_ms'])} | "
            f"{_format_ms(row['p99_ms'])} | {_format_ms(row['max_ms'])} | "
            f"{row['error_count']} | {float(row['total_ms']) / 1000.0:,.1f} |"
        )

    lines.extend(
        [
            "",
            "## Slowest individual operations",
            "",
            "| Process role | Stage | Duration ms | Tile | Outcome | Thread | Details |",
            "|---|---|---:|---|---|---|---|",
        ]
    )
    for row in summary["slow_operations"][:100]:
        details = json.dumps(row.get("details", {}), sort_keys=True)
        lines.append(
            f"| {_markdown_escape(row['role'])} | `{_markdown_escape(row['stage'])}` | "
            f"{_format_ms(row['duration_ms'])} | "
            f"`{_markdown_escape(row.get('tile_id') or '-')}` | "
            f"{_markdown_escape(row.get('outcome', ''))} | "
            f"{_markdown_escape(row.get('thread', ''))} | "
            f"`{_markdown_escape(details)}` |"
        )

    gauges = sorted(
        summary.get("gauges", []),
        key=lambda row: (str(row.get("role")), str(row.get("name"))),
    )
    if gauges:
        lines.extend(
            [
                "",
                "## Resource gauges",
                "",
                "| Process role | Gauge | Current | Minimum | Maximum | Samples |",
                "|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in gauges:
            lines.append(
                f"| {_markdown_escape(row['role'])} | `{_markdown_escape(row['name'])}` | "
                f"{row['current']:,.1f} | {row['min']:,.1f} | "
                f"{row['max']:,.1f} | {row['samples']} |"
            )

    allocations = summary.get("python_allocations", [])
    lines.extend(["", "## Python allocation growth", ""])
    if not allocations:
        lines.append(
            "Python allocation tracing was disabled. Enable "
            "`python_allocation_tracing` only for a dedicated diagnostic flight "
            "because it adds profiling overhead and cannot see native C buffers."
        )
    else:
        lines.extend(
            [
                "| Process role | Location | Growth MiB | Objects added |",
                "|---|---|---:|---:|",
            ]
        )
        for row in allocations[:50]:
            lines.append(
                f"| {_markdown_escape(row['role'])} | "
                f"`{_markdown_escape(row['location'])}` | "
                f"{_format_mib(row['size_diff_bytes'])} | {row['count_diff']} |"
            )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Start with `fuse.dds_read` for X-Plane-visible latency, then follow its nested tile, cache, network, image, and DDS stages.",
            "- Compare `chunk.queue_wait` with `network.http_request`: queue time indicates local worker contention; HTTP time indicates provider/network delay.",
            "- Compare DDS pool waits with native build time: pool waits indicate concurrency pressure; build time indicates CPU/decode/compression cost.",
            "- RSS includes shared/native allocations; USS better estimates memory unique to each process. On macOS, per-process physical footprint is also present in raw JSON.",
            "- `report.json` contains the full one-second memory timeline, histograms, counters, configuration snapshot, and per-process profiles used by this report.",
            "",
        ]
    )
    return "\n".join(lines)

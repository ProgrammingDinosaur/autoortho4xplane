"""Record X-Plane's texture opens and reads, for the replay benchmark.

Performance plan, item 0. ``tools/bench/replay.py`` replays a flight's reads
against a mount; this module records them. It is off unless the mount worker
starts with ``AO_READ_TRACE=<file>``: then every DDS open, read and release is
appended to that file as one JSON line, with the time since the first event,
and the aircraft position once a second (``op: "pos"``), which the replay
feeds back through a fake X-Plane.

    {"t": 12.345, "op": "open", "path": "/textures/21728_34432_BI16.dds", "ms": 0.6,
     "tier": "direct", "tid": 1234}
    {"t": 12.350, "op": "read", "path": "...", "offset": 0, "length": 65536, "ms": 4.1,
     "tier": "direct", "tid": 1234, "got": 65536}
    {"t": 13.000, "op": "pos", "lat": 62.06, "lon": -7.27, "agl_m": 950, "hdg": 88, "gs": 120}

An open's or a read's ``t`` is when it ended (it started ``ms`` earlier).
An open's ``tier`` is what will answer it (``direct``, ``partial``, ``live``;
``native`` when native reads do). A read's ``tier`` is the
path that answered it: ``direct`` or ``partial`` (the cache file),
``live`` (a tile in memory), or ``direct_to_live`` / ``partial_to_live`` when
the file could not answer and the open switched. ``tid`` is the file system's
thread; reads in flight on different threads at once show how many X-Plane
overlaps. ``got`` is the bytes returned.

Lines are written by a background thread: the FUSE threads only queue them.
No Qt and no getortho import here.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

ENV = "AO_READ_TRACE"
_SEPARATORS = (",", ":")


class ReadTrace:
    def __init__(self, path: str, position: Optional[Callable[[], Optional[dict]]] = None):
        self.path = path
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._t0: Optional[float] = None
        self._position = position
        self._stop = threading.Event()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")
        self._writer = threading.Thread(target=self._write_loop, name="ReadTrace", daemon=True)
        self._writer.start()
        self._sampler = None
        if position is not None:
            self._sampler = threading.Thread(
                target=self._position_loop, name="ReadTracePos", daemon=True
            )
            self._sampler.start()

    def _now(self) -> float:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        return round(now - self._t0, 4)

    def event(self, op: str, path: str, **fields) -> None:
        record = {"t": self._now(), "op": op, "path": path}
        record.update({k: v for k, v in fields.items() if v is not None})
        self._queue.put(record)

    def _position_loop(self) -> None:
        while not self._stop.wait(1.0):
            if self._t0 is None:
                continue  # nothing read yet
            try:
                position = self._position()
            except Exception:
                position = None
            if position:
                record = {"t": self._now(), "op": "pos"}
                record.update(position)
                self._queue.put(record)

    def _write_loop(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                break
            lines = [record]
            try:
                while len(lines) < 512:
                    item = self._queue.get_nowait()
                    if item is None:
                        self._queue.put(None)
                        break
                    lines.append(item)
            except queue.Empty:
                pass
            try:
                self._handle.write(
                    "".join(json.dumps(r, separators=_SEPARATORS) + "\n" for r in lines)
                )
                self._handle.flush()
            except OSError as exc:
                log.warning("Read trace write failed: %s", exc)
        try:
            self._handle.close()
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        self._queue.put(None)
        self._writer.join(timeout=5.0)


_trace: Optional[ReadTrace] = None
_trace_lock = threading.Lock()


def from_environment(position: Optional[Callable[[], Optional[dict]]] = None) -> Optional[ReadTrace]:
    """The process's trace when ``AO_READ_TRACE`` names a file, else None."""
    global _trace
    path = os.environ.get(ENV, "").strip()
    if not path:
        return None
    with _trace_lock:
        if _trace is None:
            try:
                _trace = ReadTrace(path, position=position)
                log.info("Recording texture reads to %s", path)
            except OSError as exc:
                log.warning("Could not open read trace %s: %s", path, exc)
                return None
        return _trace


def load(path: str) -> list:
    """The events of a trace file, in time order."""
    events = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    events.sort(key=lambda e: float(e.get("t", 0.0)))
    return events


def summarize(events: list, fast_ms: float = 2.0) -> dict:
    """What a recorded flight says about the read path (hybrid read path
    plan, N6).

    * ``opens``, ``ready_opens``: texture opens, and those whose every read
      was answered from the cache file (tier ``direct`` or ``partial``).
    * ``reads`` and ``reads_by_tier``.
    * ``max_overlap``: the most reads in flight at once, and
      ``fast_overlap``: how many reads of ``fast_ms`` or less were in flight
      at once, as {count: reads} (what a native read path would serve).
    * ``ready_open_ms``: p50, p95 and mean time of the opens of ready
      textures, and ``ready_open_time_share``: the share of a ready
      texture's time (its open and its reads) that the open takes. These two
      decide whether a native open (stage S5) is worth building: see gate C
      in docs/developer/hybrid-read-path-plan.md. None in traces recorded
      before opens were timed.
    * ``native_opens``: opens answered by native reads (``native_direct_reads``
      on). Their reads never reach Python and are not in the trace: such an
      open is ready by its own tier, counts in ``ready_open_ms``, and is
      left out of ``ready_open_time_share``, which is then the share over
      textures read in Python (reads slower than native ones, so a lower
      bound of the share with native reads).
    """
    opens = 0
    native_opens = 0
    open_tiers = []  # per open: set of tiers its reads used
    open_native = []  # per open: answered by native reads
    open_ms = []  # per open: its time, or None
    read_ms = []  # per open: the time of its reads
    current = {}  # path -> index into open_tiers
    by_tier = {}
    intervals = []
    fast = []
    reads = 0
    for event in events:
        op = event.get("op")
        path = event.get("path")
        if op == "open":
            opens += 1
            current[path] = len(open_tiers)
            native = bool(event.get("native")) and event.get("tier") in ("direct", "partial")
            native_opens += native
            open_native.append(native)
            open_tiers.append({event["tier"]} if native else set())
            try:
                open_ms.append(float(event["ms"]) if "ms" in event else None)
            except (TypeError, ValueError):
                open_ms.append(None)
            read_ms.append(0.0)
        elif op == "read":
            reads += 1
            tier = event.get("tier", "unknown")
            by_tier[tier] = by_tier.get(tier, 0) + 1
            index = current.get(path)
            if index is not None:
                open_tiers[index].add(tier)
            try:
                end = float(event.get("t", 0.0))
                ms = float(event.get("ms", 0.0))
            except (TypeError, ValueError):
                continue
            if index is not None:
                read_ms[index] += ms
            interval = (end - ms / 1000.0, end)
            intervals.append(interval)
            if ms <= fast_ms:
                fast.append(interval)
        elif op == "release":
            current.pop(path, None)
    ready_indices = [
        i for i, tiers in enumerate(open_tiers)
        if tiers and tiers <= {"direct", "partial"}
    ]
    ready = len(ready_indices)
    timed = [i for i in ready_indices if open_ms[i] is not None]
    ready_open_ms = None
    ready_open_share = None
    if timed:
        values = sorted(open_ms[i] for i in timed)

        def pct(q):
            return round(values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))], 3)

        ready_open_ms = {
            "p50": pct(0.50), "p95": pct(0.95),
            "mean": round(sum(values) / len(values), 3), "n": len(values),
        }
        in_python = [i for i in timed if not open_native[i]]
        total = sum(open_ms[i] + read_ms[i] for i in in_python)
        if total > 0:
            ready_open_share = round(sum(open_ms[i] for i in in_python) / total, 4)

    def overlaps(spans):
        points = []
        for start, end in spans:
            points.append((start, 1))
            points.append((end, -1))
        points.sort(key=lambda p: (p[0], p[1]))
        depth = 0
        histogram = {}
        for _t, step in points:
            depth += step
            if step > 0:
                histogram[depth] = histogram.get(depth, 0) + 1
        return histogram

    all_hist = overlaps(intervals)
    return {
        "opens": opens,
        "ready_opens": ready,
        "ready_share": round(ready / opens, 4) if opens else None,
        "reads": reads,
        "reads_by_tier": by_tier,
        "max_overlap": max(all_hist) if all_hist else 0,
        "fast_overlap": dict(sorted(overlaps(fast).items())),
        "ready_open_ms": ready_open_ms,
        "ready_open_time_share": ready_open_share,
        "native_opens": native_opens,
    }


if __name__ == "__main__":  # python -m autoortho.read_trace <trace.jsonl>
    import sys

    print(json.dumps(summarize(load(sys.argv[1])), indent=2))

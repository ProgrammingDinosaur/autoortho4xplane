"""Record X-Plane's texture opens and reads, for the replay benchmark.

Performance plan, item 0. ``tools/bench/replay.py`` replays a flight's reads
against a mount; this module records them. It is off unless the mount worker
starts with ``AO_READ_TRACE=<file>``: then every DDS open, read and release is
appended to that file as one JSON line, with the time since the first event,
and the aircraft position once a second (``op: "pos"``), which the replay
feeds back through a fake X-Plane.

    {"t": 12.345, "op": "open", "path": "/textures/21728_34432_BI16.dds"}
    {"t": 12.350, "op": "read", "path": "...", "offset": 0, "length": 65536, "ms": 4.1}
    {"t": 13.000, "op": "pos", "lat": 62.06, "lon": -7.27, "agl_m": 950, "hdg": 88, "gs": 120}

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

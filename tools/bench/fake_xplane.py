"""A stand-in for X-Plane's UDP dataref feed, for the replay benchmark.

AutoOrtho subscribes to datarefs with ``RREF`` requests on X-Plane's UDP port
(``[flightdata] xplane_udp_port``) and X-Plane answers with ``RREF`` packets
once a second. Without a feed, ``flight_is_starting()`` stays true for the
whole replay: every near read waits the 60 s loading-screen band and tile
budgets get the 10x initial-load factor, so the results describe a loading
screen instead of a flight. This class answers every subscriber with a
position taken from a callable (the replayed trace's ``pos`` events).

The dataref order is read from ``autoortho.datareftrack.DatarefTracker``, so
the indexes match what AutoOrtho asked for.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Dict, Optional

# Values sent for datarefs a trace does not record.
_DEFAULTS = {
    "sim/time/local_time_sec": 12 * 3600.0,
    "sim/graphics/scenery/sun_pitch_degrees": 45.0,
    "sim/graphics/view/window_width": 2560.0,
    "sim/graphics/view/field_of_view_deg": 70.0,
    "sim/operation/misc/frame_rate_period": 1 / 40.0,
    "sim/time/paused": 0.0,
}


def _dataref_names():
    try:
        from autoortho.datareftrack import DatarefTracker
    except ImportError:  # pragma: no cover
        from datareftrack import DatarefTracker
    return [entry[0] for entry in DatarefTracker.datarefs]


class FakeXPlane:
    """Answers RREF subscriptions on ``port`` with ``position()`` once a second.

    ``position()`` returns a dict with ``lat``, ``lon``, ``agl_m``, ``hdg``
    and ``gs`` (m/s), or None to stay silent (X-Plane's loading screen).
    """

    def __init__(self, port: int, position: Callable[[], Optional[dict]], rate_hz: float = 1.0):
        self.port = int(port)
        self.position = position
        self.interval = 1.0 / max(0.1, float(rate_hz))
        self._names = _dataref_names()
        self._subscribers: Dict[tuple, Dict[int, str]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.settimeout(0.2)
        self.packets_sent = 0
        self._threads = [
            threading.Thread(target=self._listen, name="FakeXPlaneListen", daemon=True),
            threading.Thread(target=self._send_loop, name="FakeXPlaneSend", daemon=True),
        ]

    def start(self) -> "FakeXPlane":
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        try:
            self._sock.close()
        except OSError:
            pass

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if len(data) < 413 or not data.startswith(b"RREF"):
                continue
            _cmd, freq, index, raw = struct.unpack("<5sii400s", data[:413])
            name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
            with self._lock:
                wanted = self._subscribers.setdefault(addr, {})
                if freq <= 0:
                    wanted.pop(index, None)
                else:
                    wanted[index] = name

    def _value(self, name: str, position: dict) -> float:
        if name.endswith("/latitude"):
            return float(position["lat"])
        if name.endswith("/longitude"):
            return float(position["lon"])
        if name.endswith("/y_agl"):
            return float(position.get("agl_m", 0.0))
        if name.endswith("/mag_psi"):
            return float(position.get("hdg", 0.0))
        if name.endswith("/groundspeed"):
            return float(position.get("gs", 0.0))
        if name.endswith("/pressure_altitude"):
            return float(position.get("agl_m", 0.0)) * 3.28084
        return float(_DEFAULTS.get(name, 0.0))

    def _send_loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                position = self.position()
            except Exception:
                position = None
            if not position:
                continue
            with self._lock:
                subscribers = {addr: dict(refs) for addr, refs in self._subscribers.items()}
            for addr, refs in subscribers.items():
                if not refs:
                    continue
                payload = b"RREF\x00" + b"".join(
                    struct.pack("<if", index, self._value(name, position))
                    for index, name in sorted(refs.items())
                )
                try:
                    self._sock.sendto(payload, addr)
                    self.packets_sent += 1
                except OSError:
                    continue


class TracePositions:
    """The trace's ``pos`` events, played back against a clock."""

    def __init__(self, events, clock: Callable[[], float]):
        self._positions = [e for e in events if e.get("op") == "pos"]
        self._clock = clock

    def __call__(self) -> Optional[dict]:
        now = self._clock()
        latest = None
        for event in self._positions:
            if float(event["t"]) > now:
                break
            latest = event
        if latest is None and self._positions:
            # Before the first recorded position X-Plane was loading: silent.
            return None
        return latest


def wait_for_subscriber(fake: FakeXPlane, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with fake._lock:
            if any(fake._subscribers.values()):
                return True
        time.sleep(0.1)
    return False

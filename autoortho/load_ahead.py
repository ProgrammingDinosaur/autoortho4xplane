"""Warm the textures of the scenery X-Plane is loading, nearest first.

X-Plane loads a DSF in two steps. It first opens every terrain definition
(``.ter``) the DSF uses, then 20-45 s later asks for their textures one after
another. Each ``.ter`` name gives its texture, so the ``.ter`` opens are an
exact list of the textures X-Plane is about to read, at the zoom the package
uses there.

This queue collects them and hands them to a warm function one at a time,
always the one nearest to where X-Plane is loading right now: its latest
texture request if recent, otherwise the aircraft. The warm function decides
how much of each texture to fetch (see ``getortho.load_ahead_warm``).
"""

import logging
import math
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

REQUEST_FOCUS_SECONDS = 5.0
# A texture warmed this recently is not queued again for the same level.
REWARM_SECONDS = 120.0
_MAX_WAITING = 50000


def _tile_centre(row, col, zoom, span=16):
    n = 2.0 ** zoom
    lon = (col + span / 2) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (row + span / 2) / n))))
    return lat, lon


class LoadAhead:
    """A nearest-first queue of textures from ``.ter`` opens."""

    def __init__(self, warm, aircraft=None, clock=time.monotonic):
        """
        Args:
            warm: ``warm(row, col, maptype, zoom) -> bool``. Starts fetching
                what the texture needs; False when it could not start now
                (no capacity), so the texture is tried again later.
            aircraft: ``() -> (lat, lon)`` or None.
        """
        self._warm = warm
        self._aircraft = aircraft or (lambda: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._waiting = OrderedDict()  # key -> (row, col, maptype, zoom, lat, lon)
        self._warmed_at = {}
        self._focus = None  # (lat, lon, at)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.queued = 0
        self.warmed = 0

    # ------------------------------------------------------------ events

    def note_terrain(self, row, col, maptype, zoom):
        """X-Plane opened the .ter of this texture."""
        key = (int(row), int(col), str(maptype), int(zoom))
        lat, lon = _tile_centre(key[0], key[1], key[3])
        with self._lock:
            warmed = self._warmed_at.get(key)
            if warmed is not None and self._clock() - warmed < REWARM_SECONDS:
                return
            if key in self._waiting or len(self._waiting) >= _MAX_WAITING:
                return
            self._waiting[key] = (*key, lat, lon)
            self.queued += 1
        self._wake.set()

    def note_texture_request(self, row, col, zoom):
        """X-Plane asked for a texture: it is loading around there now."""
        lat, lon = _tile_centre(int(row), int(col), int(zoom))
        with self._lock:
            self._focus = (lat, lon, self._clock())
            for key in [k for k in self._waiting if k[0] == row and k[1] == col and k[3] == zoom]:
                del self._waiting[key]  # being built on request now

    # ------------------------------------------------------------ queue

    def _focus_point(self):
        now = self._clock()
        if self._focus is not None and now - self._focus[2] < REQUEST_FOCUS_SECONDS:
            return self._focus[:2]
        aircraft = self._aircraft()
        if aircraft is not None:
            return aircraft
        return None if self._focus is None else self._focus[:2]

    def take_next(self):
        """Remove and return the waiting texture nearest the focus."""
        with self._lock:
            if not self._waiting:
                return None
            focus = self._focus_point()
            if focus is None:
                key, entry = self._waiting.popitem(last=False)
                return entry
            cos_lat = math.cos(math.radians(focus[0]))
            best_key, best = None, math.inf
            for key, entry in self._waiting.items():
                d_lat = entry[4] - focus[0]
                d_lon = (entry[5] - focus[1]) * cos_lat
                distance = d_lat * d_lat + d_lon * d_lon
                if distance < best:
                    best_key, best = key, distance
            return self._waiting.pop(best_key)

    def requeue(self, entry):
        with self._lock:
            self._waiting.setdefault(tuple(entry[:4]), entry)

    def waiting(self):
        with self._lock:
            return len(self._waiting)

    def run_once(self):
        """Warm the next texture. Returns False when nothing could be started."""
        entry = self.take_next()
        if entry is None:
            return False
        row, col, maptype, zoom = entry[:4]
        try:
            started = self._warm(row, col, maptype, zoom)
        except Exception as exc:
            log.debug(f"Load-ahead warm of {row}_{col}_{maptype}{zoom} failed: {exc}")
            started = True  # do not retry a texture that raises
        if not started:
            self.requeue(entry)
            return False
        with self._lock:
            self._warmed_at[tuple(entry[:4])] = self._clock()
            if len(self._warmed_at) > _MAX_WAITING:
                oldest = sorted(self._warmed_at.items(), key=lambda kv: kv[1])
                for key, _at in oldest[: len(oldest) // 2]:
                    del self._warmed_at[key]
        self.warmed += 1
        return True

    # ------------------------------------------------------------ thread

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="TerrainLoadAhead", daemon=True
        )
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            if self.run_once():
                continue
            self._wake.wait(0.25)
            self._wake.clear()

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

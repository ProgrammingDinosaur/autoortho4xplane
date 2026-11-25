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
# Side of a grid cell in degrees. The nearest waiting texture is searched
# in growing windows of cells around the focus. A ZL16 texture is about
# 0.09 deg wide, so a cell holds a few at ZL16 and a few dozen at ZL18.
# With 0.1, a take costs 65-165 us for 1,000 to 18,000 waiting at ZL16-18;
# the whole-queue scan it replaced took 0.2 to 3.3 ms.
_CELL_DEG = 0.1


def _cell(lat, lon):
    return (math.floor(lat / _CELL_DEG), math.floor(lon / _CELL_DEG))


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
        # (row, col, zoom) -> map types waiting: a texture request removes
        # its entries without scanning the queue, which holds thousands
        # while X-Plane loads, under the lock every texture open takes.
        self._by_tile = {}
        # Grid cell -> keys waiting there: take_next looks at the cells near
        # the focus, not at the whole queue (0.5 ms per take with 3,000
        # waiting, holding the lock and the GIL, once per texture warmed;
        # Python opens of the mount wait that long).
        self._cells = {}
        self._bounds = None  # [min y, max y, min x, max x] of cells used
        self.max_waiting = 0  # the most textures waiting at once
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
            self._add(key, (*key, lat, lon))
            self.queued += 1
        self._wake.set()

    def note_texture_request(self, row, col, zoom):
        """X-Plane asked for a texture: it is loading around there now."""
        lat, lon = _tile_centre(int(row), int(col), int(zoom))
        row, col, zoom = int(row), int(col), int(zoom)
        with self._lock:
            self._focus = (lat, lon, self._clock())
            for maptype in list(self._by_tile.get((row, col, zoom), ())):
                # Being built on request now.
                self._remove((row, col, maptype, zoom))

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
                key = next(iter(self._waiting))
            else:
                key = self._nearest(focus)
            return self._remove(key)

    def _nearest(self, focus):
        """The waiting key nearest ``focus`` (lock held, queue not empty).

        Looks at grid cells in growing windows around the focus's cell and
        stops once no cell outside the window can hold anything nearer than
        the best found. A window is ``wide`` times wider in cells than tall,
        so that it is about square on the ground (a degree of longitude is
        cos(latitude) of a degree of latitude). Distances are as before:
        degrees, longitude scaled by the focus's latitude.
        """
        lat, lon = focus
        cos_lat = math.cos(math.radians(lat))
        wide = min(16, max(1, math.ceil(1.0 / max(abs(cos_lat), 1e-6))))
        cells = self._cells
        cy, cx = _cell(lat, lon)
        # How far the windows may have to grow (the bounds only grow until
        # the queue empties, so this may be more than needed).
        min_y, max_y, min_x, max_x = self._bounds
        reach = max(
            abs(min_y - cy), abs(max_y - cy),
            -(-max(abs(min_x - cx), abs(max_x - cx)) // wide),
        )
        if (2 * reach + 1) * (2 * wide * reach + 1) > 4 * len(self._waiting) + 64:
            # The focus is far from everything waiting: the windows would
            # cost more than looking at every texture.
            return self._nearest_of(self._waiting, lat, lon, cos_lat)[0]
        best_key, best = None, math.inf
        for r in range(reach + 1):
            if r:
                # Anything not seen yet lies outside window r - 1: at least
                # the focus's distance to that window's nearest edge away.
                s = r - 1
                edge = min(
                    lat - (cy - s) * _CELL_DEG,
                    (cy + s + 1) * _CELL_DEG - lat,
                    (lon - (cx - wide * s) * _CELL_DEG) * abs(cos_lat),
                    ((cx + wide * s + 1) * _CELL_DEG - lon) * abs(cos_lat),
                )
                if edge * edge >= best:
                    break
            inner = wide * (r - 1)  # columns window r - 1 already covered
            for dy in range(-r, r + 1):
                if abs(dy) < r:  # rows seen before: only the new columns
                    columns = (
                        list(range(-wide * r, -inner))
                        + list(range(inner + 1, wide * r + 1))
                    ) if r else (0,)
                else:
                    columns = range(-wide * r, wide * r + 1)
                y = cy + dy
                for dx in columns:
                    keys = cells.get((y, cx + dx))
                    if keys:
                        key, distance = self._nearest_of(keys, lat, lon, cos_lat)
                        if distance < best:
                            best_key, best = key, distance
        return best_key

    def _nearest_of(self, keys, lat, lon, cos_lat):
        waiting = self._waiting
        best_key, best = None, math.inf
        for key in keys:
            entry = waiting[key]
            d_lat = entry[4] - lat
            d_lon = (entry[5] - lon) * cos_lat
            distance = d_lat * d_lat + d_lon * d_lon
            if distance < best:
                best_key, best = key, distance
        return best_key, best

    def _add(self, key, entry):
        """Queue ``entry`` and index it (lock held)."""
        self._waiting[key] = entry
        self._by_tile.setdefault((key[0], key[1], key[3]), set()).add(key[2])
        cell = _cell(entry[4], entry[5])
        self._cells.setdefault(cell, set()).add(key)
        bounds = self._bounds
        if bounds is None:
            self._bounds = [cell[0], cell[0], cell[1], cell[1]]
        else:
            bounds[0] = min(bounds[0], cell[0])
            bounds[1] = max(bounds[1], cell[0])
            bounds[2] = min(bounds[2], cell[1])
            bounds[3] = max(bounds[3], cell[1])
        if len(self._waiting) > self.max_waiting:
            self.max_waiting = len(self._waiting)

    def _remove(self, key):
        """Take ``key`` out of the queue and its indexes (lock held)."""
        entry = self._waiting.pop(key)
        tile = (key[0], key[1], key[3])
        maptypes = self._by_tile.get(tile)
        if maptypes is not None:
            maptypes.discard(key[2])
            if not maptypes:
                del self._by_tile[tile]
        cell = _cell(entry[4], entry[5])
        keys = self._cells.get(cell)
        if keys is not None:
            keys.discard(key)
            if not keys:
                del self._cells[cell]
                if not self._cells:
                    self._bounds = None
        return entry

    def requeue(self, entry):
        key = tuple(entry[:4])
        with self._lock:
            if key not in self._waiting:
                self._add(key, entry)

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

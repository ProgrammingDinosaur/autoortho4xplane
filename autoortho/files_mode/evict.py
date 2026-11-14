"""Disk budget: coarsen or remove textures X-Plane cannot be showing.

Every round (10 s, never while X-Plane loads scenery), at most
``MAX_PER_ROUND`` textures. Above the budget, down to 90 % of it:

1. textures behind the aircraft first, then the least recently needed;
2. only outside the squares X-Plane may hold (with its lag) and beyond the
   512 px ring plus camera reach, so X-Plane cannot be showing them sharper
   than what is left (A10: it never reads a coarser level again);
3. coarsened in place to 256 px (holes punched over the sharper levels);
4. beyond 300 km and outside the materialized squares: deleted.
"""

import logging
import math

import numpy as np

from . import geo, rings
from .store import NEUTRAL

log = logging.getLogger(__name__)

MAX_PER_ROUND = 50
TARGET_FRACTION = 0.9
DELETE_BEYOND_KM = 300.0


class Evictor:
    def __init__(self, store, budget_bytes, ring_model=None, camera_reach_km=10.0):
        self.store = store
        self.budget_bytes = int(budget_bytes)
        self.rings = ring_model or rings.RingModel()
        self.camera_reach_km = float(camera_reach_km)
        self.counters = {"files_evicted_coarsened": 0, "files_evicted_deleted": 0,
                         "files_evicted_bytes": 0}

    def used_bytes(self):
        return self.store.index.total_alloc()

    def run_round(self, flight, xp_tiles, materialized, max_items=MAX_PER_ROUND):
        used = self.used_bytes()
        if used <= self.budget_bytes or not flight.valid:
            return 0
        target = int(self.budget_bytes * TARGET_FRACTION)
        states = [s for s in self.store.index.all()
                  if s.built_level != NEUTRAL or s.copied_level is not None]
        if not states:
            return 0
        n = len(states)
        rows = np.fromiter((s.row for s in states), dtype=np.float64, count=n)
        cols = np.fromiter((s.col for s in states), dtype=np.float64, count=n)
        zooms = np.fromiter((s.zoom for s in states), dtype=np.float64, count=n)
        scale = np.power(2.0, zooms)
        half = 8.0
        lats = np.degrees(np.arctan(np.sinh(np.pi - 2 * np.pi * (rows + half) / scale)))
        lons = (cols + half) / scale * 360.0 - 180.0
        coslat = math.cos(math.radians(flight.lat))
        dx = (((lons - flight.lon) + 540.0) % 360.0 - 180.0) * 111.32 * coslat
        dy = (lats - flight.lat) * 110.574
        dist = np.hypot(dx, dy)
        trk = math.radians(flight.track_deg)
        ahead = (dx * math.sin(trk) + dy * math.cos(trk)) / np.maximum(dist, 1e-6)
        safe = rings.screen_scale(flight.width_px, flight.fov_deg)
        ring3 = np.fromiter(
            (self.rings.ring_km(3, s.zoom) for s in states), dtype=np.float64, count=n
        ) * safe * self.rings.margin + self.camera_reach_km
        squares = [(math.floor(la), math.floor(lo)) for la, lo in zip(lats, lons)]
        eligible = [
            k for k in range(n)
            if squares[k] not in xp_tiles and dist[k] > ring3[k]
        ]
        if not eligible:
            return 0
        last = np.fromiter((states[k].last_needed_at or 0.0 for k in eligible), dtype=np.float64,
                           count=len(eligible))
        behind = np.asarray([ahead[k] < 0 for k in eligible])
        # Behind first (False sorts before True after negation), then oldest.
        order = np.lexsort((last, ~behind))
        freed = 0
        done = 0
        for o in order:
            if used - freed <= target or done >= max_items:
                break
            k = eligible[int(o)]
            s = states[k]
            if dist[k] > DELETE_BEYOND_KM and squares[k] not in materialized:
                got = self.store.delete(s.region, s.name)
                if got:
                    self.counters["files_evicted_deleted"] += 1
            elif s.built_level < rings.LOAD_LEVEL or (
                s.copied_level is not None and s.copied_level < rings.LOAD_LEVEL
            ):
                got = self.store.coarsen(s.region, s.name, rings.LOAD_LEVEL)
                if got:
                    self.counters["files_evicted_coarsened"] += 1
            else:
                got = 0
            freed += got
            done += 1
        self.counters["files_evicted_bytes"] += freed
        if freed:
            log.info("Disk budget: freed %.1f MB (%d textures)", freed / 1e6, done)
        return freed

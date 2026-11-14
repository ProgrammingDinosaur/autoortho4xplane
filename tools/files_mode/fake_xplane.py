"""A stand-in X-Plane that reads files-mode textures the way A1-A4 say.

* It keeps X-Plane's DSF window (A8). When a square enters it, it reads the
  header and the 256 px level of every texture of that square (A2).
* As the aircraft moves, it reads each texture's level again whenever the
  ring of a sharper level is crossed (A1, A3), counted from the top of the
  file (A4), and never a coarser one (A10).

Every read is classified by what the file holds at that level: ``real``
(built imagery), ``neutral`` (the neutral colour), ``hole`` (zeros: a sparse
level never written) or ``missing`` (no file). The main regression gate for
the planner: a recorded or synthetic flight should give no ``hole`` or
``missing`` reads, and few ``neutral`` ones.

Usage from tests::

    xp = FakeXPlane(session)
    xp.start(lat, lon)                    # what X-Plane reads at a start
    xp.move(lat, lon)                     # each sample of the flight
    xp.report()
"""

import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "autoortho"))

from files_mode import dds_layout, geo, rings, window  # noqa: E402


class FakeXPlane:
    def __init__(self, session, ring_model=None):
        self.session = session
        self.rings = ring_model or rings.RingModel(margin=1.0)
        self.window = window.XPlaneWindow()
        self.loaded = set()
        self.held = {}          # (region, name) -> sharpest mip read
        self.reads = Counter()
        self.bad = []           # (kind, name, mip, distance_km)
        self.neutral = dds_layout.neutral_block(session.cfg.dxt_format, session.cfg.missing_rgb)

    def _classify(self, region, name, mip):
        path = self.session.store.path(region, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            return "missing"
        top = _top_from_size(size, self.session.cfg.dxt_format)
        if top is None:
            return "bad-size"
        lay = dds_layout.layout(top, top, self.session.cfg.dxt_format)
        mip = min(mip, lay.mip_count - 1)
        start, length = lay.offsets[mip]
        with open(path, "rb") as handle:
            if handle.read(4) != b"DDS ":
                return "bad-header"
            handle.seek(start)
            data = handle.read(min(length, 64))
        if data == b"\0" * len(data):
            return "hole"
        if data == (self.neutral * 8)[:len(data)]:
            return "neutral"
        return "real"

    def _read(self, region, name, mip, distance_km):
        kind = self._classify(region, name, mip)
        self.reads[kind] += 1
        if kind != "real":
            self.bad.append((kind, name, mip, round(distance_km, 1)))
        prev = self.held.get((region, name))
        self.held[(region, name)] = mip if prev is None else min(prev, mip)

    def _load_squares(self, squares, lat, lon):
        for square in squares - self.loaded:
            for cat, idx in self.session.catalogue.textures_in(square):
                for i in idx:
                    d = geo.haversine_km(lat, lon, float(cat.lats[i]), float(cat.lons[i]))
                    self._read(cat.region, cat.name(int(i)), rings.LOAD_LEVEL, d)
        self.loaded = set(squares)

    def _upgrade(self, lat, lon):
        for square in self.loaded:
            for cat, idx in self.session.catalogue.textures_in(square):
                for i in idx:
                    d = geo.haversine_km(lat, lon, float(cat.lats[i]), float(cat.lons[i]))
                    need = self.rings.level_needed(d, int(cat.zooms[i]), float(cat.size_m[i]))
                    key = (cat.region, cat.name(int(i)))
                    if need < self.held.get(key, rings.LOAD_LEVEL):
                        self._read(cat.region, key[1], need, d)

    def start(self, lat, lon):
        self.window.reset(lat, lon)
        self._load_squares(self.window.tiles(), lat, lon)
        self._upgrade(lat, lon)

    def move(self, lat, lon):
        if self.window.follow(lat, lon):
            self._load_squares(self.window.tiles(), lat, lon)
        self._upgrade(lat, lon)

    def report(self):
        return {"reads": dict(self.reads), "bad": list(self.bad)}


def _top_from_size(size, fmt):
    for top in (512, 1024, 2048, 4096, 8192, 16384):
        if dds_layout.layout(top, top, fmt).total_size == size:
            return top
    return None


if __name__ == "__main__":
    print(__doc__)

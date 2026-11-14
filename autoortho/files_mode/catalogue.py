"""Which textures exist, and in which DSF squares (one index per region).

Built from the names in the region's ``terrain`` folder alone: each
``<row>_<col>_<maptype><zoom>[_water_overlay|_sea_overlay].ter`` names the
texture ``<row>_<col>_<maptype><zoom>.dds``, whose centre and size follow
from the name (``geo``). That avoids reading over a million ``.ter`` files
per region; a listing of North America (1.28 M names) is a few seconds,
and the result is cached in ``_ao/catalogue/<region>.v1.npz``.

A ``.ter`` whose name does not parse is read for its ``BASE_TEX`` line, so
custom Ortho4XP tiles with unusual names still work.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import geo

log = logging.getLogger(__name__)

CATALOGUE_VERSION = 1
_BASE_TEX_RE = re.compile(
    r"^\s*BASE_TEX(?:_NOWRAP)?\s+\.\./textures/(\S+\.dds)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True)
class TextureRef:
    region: str
    row: int
    col: int
    maptype: str
    zoom: int

    @property
    def name(self):
        return geo.texture_name(self.row, self.col, self.maptype, self.zoom)


class RegionCatalogue:
    """Textures of one region, as arrays, with a square -> rows index."""

    def __init__(self, region, rows, cols, zooms, maptype_ids, maptypes,
                 fingerprint=""):
        self.region = region
        self.rows = np.asarray(rows, dtype=np.int32)
        self.cols = np.asarray(cols, dtype=np.int32)
        self.zooms = np.asarray(zooms, dtype=np.int8)
        self.maptype_ids = np.asarray(maptype_ids, dtype=np.int16)
        self.maptypes = list(maptypes)
        self.fingerprint = fingerprint
        self._derive()

    def _derive(self):
        n = len(self.rows)
        half = geo.CHUNKS_PER_TEXTURE / 2
        z = self.zooms.astype(np.float64)
        scale = np.power(2.0, z)
        y = self.rows.astype(np.float64) + half
        x = self.cols.astype(np.float64) + half
        self.lats = np.degrees(np.arctan(np.sinh(np.pi - 2.0 * np.pi * y / scale)))
        self.lons = x / scale * 360.0 - 180.0
        self.size_m = (
            geo.EARTH_CIRCUMFERENCE_M * np.cos(np.radians(self.lats)) / scale
            * geo.CHUNKS_PER_TEXTURE
        )
        # Square index: a texture crossing an edge belongs to every square
        # it touches.
        north = np.degrees(np.arctan(np.sinh(
            np.pi - 2.0 * np.pi * self.rows.astype(np.float64) / scale
        )))
        south = np.degrees(np.arctan(np.sinh(
            np.pi - 2.0 * np.pi * (self.rows.astype(np.float64) + geo.CHUNKS_PER_TEXTURE) / scale
        )))
        west = self.cols.astype(np.float64) / scale * 360.0 - 180.0
        east = (self.cols.astype(np.float64) + geo.CHUNKS_PER_TEXTURE) / scale * 360.0 - 180.0
        eps = 1e-9
        lat0 = np.floor(south + eps).astype(np.int32)
        lat1 = np.floor(north - eps).astype(np.int32)
        lon0 = np.floor(west + eps).astype(np.int32)
        lon1 = np.floor(east - eps).astype(np.int32)
        by_square = {}
        simple = (lat0 == lat1) & (lon0 == lon1)
        idx = np.nonzero(simple)[0]
        if len(idx):
            keys = (lat0[idx].astype(np.int64) + 90) * 360 + (lon0[idx].astype(np.int64) + 180)
            order = np.argsort(keys, kind="stable")
            keys_sorted = keys[order]
            idx_sorted = idx[order]
            bounds = np.nonzero(np.diff(keys_sorted))[0] + 1
            starts = np.concatenate(([0], bounds))
            ends = np.concatenate((bounds, [len(keys_sorted)]))
            for start, end in zip(starts, ends):
                key = int(keys_sorted[start])
                square = (key // 360 - 90, key % 360 - 180)
                by_square[square] = list(idx_sorted[start:end])
        for i in np.nonzero(~simple)[0]:
            for la in range(int(lat0[i]), int(lat1[i]) + 1):
                for lo in range(int(lon0[i]), int(lon1[i]) + 1):
                    by_square.setdefault((la, lo), []).append(int(i))
        self._by_square = {
            square: np.asarray(sorted(set(int(v) for v in members)), dtype=np.int64)
            for square, members in by_square.items()
        }
        self._index_by_name = None
        log.debug("Catalogue %s: %d textures in %d squares", self.region, n,
                  len(self._by_square))

    def __len__(self):
        return len(self.rows)

    @property
    def squares(self):
        return set(self._by_square)

    def indices_in(self, square):
        return self._by_square.get(tuple(square), np.empty(0, dtype=np.int64))

    def name(self, i):
        return geo.texture_name(
            int(self.rows[i]), int(self.cols[i]),
            self.maptypes[int(self.maptype_ids[i])], int(self.zooms[i]),
        )

    def ref(self, i):
        return TextureRef(
            self.region, int(self.rows[i]), int(self.cols[i]),
            self.maptypes[int(self.maptype_ids[i])], int(self.zooms[i]),
        )

    def find(self, name):
        if self._index_by_name is None:
            self._index_by_name = {self.name(i): i for i in range(len(self))}
        return self._index_by_name.get(name)

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.ao-tmp-{os.getpid()}.npz"
        np.savez(
            tmp,
            version=np.int32(CATALOGUE_VERSION),
            rows=self.rows, cols=self.cols, zooms=self.zooms,
            maptype_ids=self.maptype_ids,
            maptypes=np.asarray(self.maptypes, dtype=object),
            fingerprint=np.asarray(self.fingerprint),
        )
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, region):
        with np.load(path, allow_pickle=True) as data:
            if int(data["version"]) != CATALOGUE_VERSION:
                raise ValueError("catalogue version changed")
            return cls(
                region,
                data["rows"], data["cols"], data["zooms"], data["maptype_ids"],
                [str(m) for m in data["maptypes"]],
                fingerprint=str(data["fingerprint"]),
            )


def region_fingerprint(region_root):
    """Changes when the region's terrain or DSFs change (mtime and count)."""
    parts = []
    for sub in ("terrain", "Earth nav data", "textures"):
        path = os.path.join(region_root, sub)
        try:
            st = os.stat(path)
            parts.append(f"{sub}:{st.st_mtime_ns}:{st.st_ino}")
        except OSError:
            parts.append(f"{sub}:missing")
    dsfs = 0
    end = os.path.join(region_root, "Earth nav data")
    try:
        for folder in os.scandir(end):
            if folder.is_dir():
                dsfs += sum(1 for f in os.scandir(folder.path) if f.name.endswith(".dsf"))
    except OSError:
        pass
    parts.append(f"dsf:{dsfs}")
    return "|".join(parts)


def scan_region(region_root, region=None, progress=None, stop=None):
    """Build a catalogue from the region's ``terrain`` folder."""
    region = region or os.path.basename(os.path.normpath(region_root))
    terrain = os.path.join(region_root, "terrain")
    started = time.monotonic()
    seen = set()
    rows, cols, zooms, mids = [], [], [], []
    maptypes = []
    maptype_index = {}
    unparsed = 0
    count = 0
    with os.scandir(terrain) as entries:
        for entry in entries:
            count += 1
            if stop is not None and count % 50000 == 0 and stop.is_set():
                raise InterruptedError("catalogue scan stopped")
            if progress is not None and count % 100000 == 0:
                progress(count)
            name = entry.name
            if not name.endswith(".ter"):
                continue
            parsed = geo.parse_ter_name(name)
            if parsed is None:
                parsed = _parse_ter_file(entry.path)
                if parsed is None:
                    unparsed += 1
                    continue
            row, col, maptype, zoom = parsed
            key = (row, col, maptype, zoom)
            if key in seen:
                continue
            seen.add(key)
            mid = maptype_index.get(maptype)
            if mid is None:
                mid = maptype_index[maptype] = len(maptypes)
                maptypes.append(maptype)
            rows.append(row)
            cols.append(col)
            zooms.append(zoom)
            mids.append(mid)
    log.info(
        "Catalogue %s: %d textures from %d terrain files in %.1fs (%d unreadable)",
        region, len(rows), count, time.monotonic() - started, unparsed,
    )
    return RegionCatalogue(region, rows, cols, zooms, mids, maptypes,
                           fingerprint=region_fingerprint(region_root))


def _parse_ter_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096)
    except OSError:
        return None
    match = _BASE_TEX_RE.search(text)
    if not match:
        return None
    return geo.parse_texture_name(match.group(1))


class Catalogue:
    """All regions' catalogues, loaded from cache or scanned in background."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.regions = {}
        self._lock = threading.Lock()

    def cache_path(self, region):
        return os.path.join(self.cache_dir, f"{region}.v{CATALOGUE_VERSION}.npz")

    def load_or_scan(self, region, region_root, stop=None):
        path = self.cache_path(region)
        fingerprint = region_fingerprint(region_root)
        if os.path.isfile(path):
            try:
                cat = RegionCatalogue.load(path, region)
                if cat.fingerprint == fingerprint:
                    with self._lock:
                        self.regions[region] = cat
                    return cat
                log.info("Catalogue %s is out of date; rescanning", region)
            except Exception as exc:
                log.info("Catalogue cache %s unreadable (%s); rescanning", path, exc)
        cat = scan_region(region_root, region, stop=stop)
        try:
            cat.save(path)
        except OSError as exc:
            log.warning("Could not save catalogue %s: %s", path, exc)
        with self._lock:
            self.regions[region] = cat
        return cat

    def textures_in(self, square):
        """``[(region_catalogue, indices)]`` of every region with the square."""
        out = []
        with self._lock:
            cats = list(self.regions.values())
        for cat in cats:
            idx = cat.indices_in(square)
            if len(idx):
                out.append((cat, idx))
        return out

    def squares(self):
        with self._lock:
            cats = list(self.regions.values())
        out = set()
        for cat in cats:
            out |= cat.squares
        return out

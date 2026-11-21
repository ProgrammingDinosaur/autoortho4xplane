"""Stream scenery tiles from published release zips, one file at a time.

A scenery package normally has to be downloaded and unpacked in full before
it can be used. A streaming package is an empty folder under
``z_autoortho/scenery`` holding an ``ao_stream.json`` marker. Its FUSE mount
lists every DSF in a tile index, and fetches a DSF (and the water masks it
needs) the first time X-Plane opens it, using HTTP Range requests against the
original release zips. Each file inside a zip is compressed on its own, so
one file can be read without the rest of the archive.

``.ter`` files are not fetched at all: every one in the published packages
follows one of three templates whose values come from the file name
(:func:`synthesize_ter`). The index carries the few that do not match.

The index is a gzipped JSON file written by ``scripts/build_tile_index.py``.
"""

from __future__ import annotations

import array
import base64
import bisect
import functools
import gzip
import hashlib
import io
import itertools
import json
import logging
import math
import os
import queue
import re
import struct
import sys
import threading
import time
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

# Format 2 stores entries packed (see pack_files); format 1 is still read.
INDEX_FORMAT = 2
READABLE_INDEX_FORMATS = (1, 2)
MARKER_NAME = "ao_stream.json"
INDEX_NAME = "tile_index.json.gz"
DSF_DIR = "Earth nav data"

# Where published tile indexes live. Each source is a set of release zips
# plus an index built from them by scripts/build_tile_index.py.
INDEX_BASE_URL = (
    "https://github.com/ProgrammingDinosaur/autoortho4xplane/releases/download/tile-index/"
)
# The first available source is the default in the UI.
SOURCES = OrderedDict([
    ("xp12", {
        "label": "XP12 set (flightwusel v1.4, by jonaseberle)",
        "available": True,
        "ortho_index": INDEX_BASE_URL + "tile_index_xp12.json.gz",
        "overlay_index": None,  # base mesh only; pair with simHeaven
    }),
    ("xp11", {
        "label": "XP11 legacy set (kubilus1/autoortho-scenery)",
        "available": True,
        "ortho_index": INDEX_BASE_URL + "tile_index_xp11.json.gz",
        "overlay_index": INDEX_BASE_URL + "overlay_index_xp11.json.gz",
    }),
])
# Names of the areas the release zips are grouped by (the index's
# ``assets[].region``), for picking a whole area on the map.
REGION_LABELS = {
    "afr": "Africa", "asi": "Asia", "aus_pac": "Australia & Pacific",
    "eur": "Europe", "na": "North America", "sa": "South America",
    "afr-n": "Africa North", "afr-s": "Africa South", "asi-c": "Asia Central",
    "asi-n": "Asia North", "asi-s": "Asia South", "aus-pac": "Australia & Pacific",
    "eur-e": "Europe East", "eur-w": "Europe West", "greenland": "Greenland",
    "na-n": "North America North", "na-s": "North America South",
}


def region_label(region: str) -> str:
    return REGION_LABELS.get(region, region.replace("_", " ").replace("-", " ").title())


ORTHO_PACKAGE = "z_ao_stream_{source}"
OVERLAY_PACKAGE = "yAO_stream_{source}_overlays"
LEGACY_OVERLAY_FOLDER = "yAutoOrtho_Overlays"


def dsf_rel(lat: int, lon: int) -> str:
    """Package-relative path of the DSF for a 1x1 degree tile."""
    folder = f"{lat // 10 * 10:+03d}{lon // 10 * 10:+04d}"
    return f"{DSF_DIR}/{folder}/{lat:+03d}{lon:+04d}.dsf"


DSF_NAME_RE = re.compile(r"^([+-]\d{2})([+-]\d{3})\.dsf$")
MASK_NAME_RE = re.compile(r"^(\d+)_(\d+)_ZL(\d+)\.png$")


def dsf_cell(name: str) -> Optional[Tuple[int, int]]:
    """(lat, lon) of a DSF file name such as ``+39-009.dsf``."""
    m = DSF_NAME_RE.match(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def mask_cell(name: str) -> Optional[Tuple[int, int]]:
    """The 1x1 degree tile a water mask (``<row>_<col>_ZL<z>.png``) belongs to.

    A mask covers one 16x16 block of web-mercator tiles; its centre decides
    the tile. Blocks that straddle a degree line are also used by the
    neighbouring DSF, which fetches the mask again if it is missing.
    """
    m = MASK_NAME_RE.match(name)
    if not m:
        return None
    row, col, zoom = int(m.group(1)), int(m.group(2)), int(m.group(3))
    lat, lon, _ = ter_geometry(row, col, zoom)
    return math.floor(lat), math.floor(lon)


# Tiles the user chose to keep for offline flying. Eviction skips them and
# they don't count toward the disk limit.
PIN_NAME = "pinned_tiles.json"


def read_pins(root: str) -> set:
    """Pinned (lat, lon) tiles of one streaming package."""
    try:
        with open(os.path.join(root, PIN_NAME), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    pins = set()
    for name in data.get("tiles", []) if isinstance(data, dict) else []:
        cell = dsf_cell(f"{name}.dsf")
        if cell:
            pins.add(cell)
    return pins


def write_pins(root: str, pins: Iterable[Tuple[int, int]]) -> None:
    names = sorted(f"{lat:+03d}{lon:+04d}" for lat, lon in set(pins))
    path = os.path.join(root, PIN_NAME)
    if not names:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    with open(path + ".part", "w", encoding="utf-8") as fh:
        json.dump({"tiles": names}, fh, indent=1)
    os.replace(path + ".part", path)


def stored_file_cell(name: str) -> Optional[Tuple[int, int]]:
    """The tile a stored DSF or water mask belongs to, by file name."""
    if name.endswith(".dsf"):
        return dsf_cell(name)
    if name.endswith(".png"):
        return mask_cell(name)
    return None

# Bytes read past the fixed local header so the name and extra field come in
# the same request as the data. Longer extra fields cost a second request.
_LOCAL_HEADER_SLACK = 512
_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_LOCAL_HEADER_SIG = 0x04034B50

# Index entry fields: [asset, header_offset, compressed_size, size, method, crc]
_ENTRY_ASSET, _ENTRY_OFFSET, _ENTRY_CSIZE, _ENTRY_SIZE, _ENTRY_METHOD, _ENTRY_CRC = range(6)
_PACKED_ENTRY = struct.Struct("<HQIIHI")
# Water masks are almost all of an index's files; format 2 keys them by
# (row, col, zoom) instead of by name.
_MASK_NAME = re.compile(r"textures/(0|[1-9]\d*)_(0|[1-9]\d*)_ZL(0|[1-9]\d*)\.png")

EARTH_RADIUS = 6378137.0


# ---------------------------------------------------------------------------
# .ter synthesis
# ---------------------------------------------------------------------------

TER_NAME_RE = re.compile(
    r"^(?P<row>\d+)_(?P<col>\d+)_(?P<maptype>[A-Za-z]+?)(?P<zoom>\d{2})"
    r"(?P<kind>_water_overlay|_sea_overlay|_water|_sea)?\.ter$"
)

# Defaults match the kubilus1/autoortho-scenery XP11 packages; an index can
# override any of them under "ter". XP12 builds from Ortho4XP 1.40 use
# decal=None, mask_zl=16, decal_on_sea=True and add "_water"/"_sea" files.
DEFAULT_TER_PARAMS = {
    "decal": "lib/g10/decals/maquify_2_green_key.dcl",
    "decal_on_sea": False,   # Ortho4XP 1.30 decorated land only; 1.40 sea too
    "land_shadow": True,     # False writes NO_SHADOW on land (terrain_casts_shadows)
    "mask_zl": 14,
    "mask_name": "{row}_{col}_ZL{zoom}.png",
}


def ter_geometry(row: int, col: int, zoom: int) -> Tuple[float, float, int]:
    """Centre and approximate width in metres of a 16x16 texture block.

    Same arithmetic as Ortho4XP's ``gtile_to_wgs84`` and
    ``webmercator_pixel_size`` so the printed values match its files.
    """
    rat_x = (col + 8) / (2 ** (zoom - 1)) - 1
    rat_y = 1 - (row + 8) / (2 ** (zoom - 1))
    lon = rat_x * 180
    lat = 360 / math.pi * math.atan(math.exp(math.pi * rat_y)) - 90
    pixel = 2 * math.pi * EARTH_RADIUS * math.cos(math.pi * lat / 180) / (2 ** (zoom + 8))
    return lat, lon, int(pixel * 4096)


def synthesize_ter(
    name: str,
    params: Optional[dict] = None,
    exceptions: Optional[Dict[str, str]] = None,
    roughness: Optional[float] = None,
) -> Optional[bytes]:
    """Return the contents of ``terrain/<name>``, or None if it can't be made."""
    text = None
    if exceptions:
        text = exceptions.get(name)
    if text is None:
        m = TER_NAME_RE.match(name)
        if not m:
            return None
        p = dict(DEFAULT_TER_PARAMS)
        if params:
            p.update(params)
        row, col, zoom = int(m["row"]), int(m["col"]), int(m["zoom"])
        maptype, kind = m["maptype"], m["kind"] or ""
        lat, lon, size = ter_geometry(row, col, zoom)
        base = f"{row}_{col}_{maptype}{zoom}"
        center = f"{lat:.5f} {lon:.5f} {size}"
        # Same branches as Ortho4XP's create_terrain_file.
        tri_type = 1 if kind.startswith("_water") else 2 if kind.startswith("_sea") else 0
        overlay = kind.endswith("_overlay")
        lines = ["A", "800", "TERRAIN", "", f"LOAD_CENTER {center} 4096",
                 f"BASE_TEX_NOWRAP ../textures/{base}.dds"]
        if tri_type and not overlay:  # XP12 water
            lines.append("WATER_COLOR_MASK")
        elif tri_type == 1:
            lines.append("BORDER_TEX ../textures/water_transition.png")
        elif tri_type == 2:
            mask_px = 4096 // 2 ** (zoom - int(p["mask_zl"]))
            mask_name = _mask_name(p, row, col, maptype, zoom)
            lines += [f"LOAD_CENTER_BORDER {center} {mask_px}",
                      f"BORDER_TEX ../textures/{mask_name}"]
        if p.get("decal") and (tri_type == 0 or (tri_type == 2 and p.get("decal_on_sea"))):
            lines.append(f"DECAL_LIB {p['decal']}")
        lines.append("WET" if tri_type else "NO_ALPHA")
        if tri_type or not p.get("land_shadow", True):
            lines.append("NO_SHADOW")
        text = "\n".join(lines) + "\n"
    if roughness is not None:
        text = re.sub(r"(?m)^SUPER_ROUGHNESS .*\n?", "", text)
        if not text.endswith("\n"):
            text += "\n"
        text += f"SUPER_ROUGHNESS {roughness}\n"
    return text.encode("ascii")


def _mask_name(params: dict, row: int, col: int, maptype: str, zoom: int) -> str:
    template = params.get("mask_name_by_zoom", {}).get(str(zoom), params["mask_name"])
    return template.format(row=row, col=col, maptype=maptype, zoom=zoom)


def border_textures(ter_bytes: bytes) -> List[str]:
    """Texture files named by BORDER_TEX lines, relative to the package."""
    out = []
    for line in ter_bytes.decode("ascii", "replace").splitlines():
        if line.startswith("BORDER_TEX "):
            ref = line.split(None, 1)[1].strip()
            if ref.startswith("../"):
                ref = ref[3:]
            out.append(ref)
    return out


# ---------------------------------------------------------------------------
# DSF terrain list
# ---------------------------------------------------------------------------

def _defn_table(data: bytes, table_id: bytes) -> Optional[List[str]]:
    """A string table inside the DEFN atom of an uncompressed DSF, or None.

    Atom ids are stored byte-reversed. 7z-compressed DSFs return None.
    """
    if len(data) < 12 or data[:8] != b"XPLNEDSF":
        return None
    end = len(data) - 16  # trailing MD5
    pos = 12
    while pos + 8 <= end:
        atom_id = data[pos:pos + 4]
        length = struct.unpack_from("<I", data, pos + 4)[0]
        if length < 8:
            return None
        if atom_id == b"NFED":
            sub = pos + 8
            sub_end = min(pos + length, end)
            while sub + 8 <= sub_end:
                sub_id = data[sub:sub + 4]
                sub_len = struct.unpack_from("<I", data, sub + 4)[0]
                if sub_len < 8:
                    return None
                if sub_id == table_id:
                    table = data[sub + 8:sub + sub_len]
                    return [n.decode("utf-8", "replace") for n in table.split(b"\0") if n]
                sub += sub_len
            return []
        pos += length
    return None


def dsf_terrain_names(data: bytes) -> Optional[List[str]]:
    """Terrain definitions (TERT table) listed in an uncompressed DSF, or None."""
    return _defn_table(data, b"TRET")


def dsf_raster_names(data: bytes) -> Optional[List[str]]:
    """Raster layers (DEMN table) of an uncompressed DSF, or None."""
    return _defn_table(data, b"NMED")


# X-Plane 12 seasonal rasters are named spr1, sum1, fal1, win1, ...
_SEASON_RASTERS = ("spr", "sum", "fal", "win")


def package_has_native_seasons(package_dir: str) -> bool:
    """Whether a scenery package's DSFs already carry X-Plane 12's seasonal
    rasters, so the Seasons patch has nothing to add. Checks the first DSF
    (packages are built with one tool and settings); cached per file."""
    dsf_root = os.path.join(package_dir, DSF_DIR)
    try:
        folders = sorted(e.name for e in os.scandir(dsf_root) if e.is_dir())
    except OSError:
        return False
    for folder in folders:
        try:
            names = sorted(e.name for e in os.scandir(os.path.join(dsf_root, folder))
                           if e.is_file() and e.name.lower().endswith(".dsf"))
        except OSError:
            continue
        if names:
            path = os.path.join(dsf_root, folder, names[0])
            try:
                st = os.stat(path)
            except OSError:
                return False
            # Unpacking a 7z DSF takes seconds, so the answer is kept in the
            # package for as long as that DSF is unchanged.
            key = {"dsf": f"{folder}/{names[0]}", "size": st.st_size,
                   "mtime_ns": st.st_mtime_ns}
            cache = os.path.join(package_dir, NATIVE_SEASONS_CACHE)
            try:
                with open(cache, encoding="utf-8") as fh:
                    cached = json.load(fh)
                if {k: cached.get(k) for k in key} == key:
                    return bool(cached.get("seasons"))
            except (OSError, ValueError, AttributeError):
                pass
            found = _dsf_has_seasons(path)
            try:
                with open(cache, "w", encoding="utf-8") as fh:
                    json.dump(dict(key, seasons=found), fh)
            except OSError:
                pass
            return found
    return False


NATIVE_SEASONS_CACHE = ".ao_native_seasons.json"


def _dsf_has_seasons(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    if data.startswith(SEVENZIP_MAGIC):
        data = unpack_7z_dsf(path) or b""
    rasters = dsf_raster_names(data) or []
    return any(name.startswith(_SEASON_RASTERS) for name in rasters)


SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


def unpack_7z_dsf(path: str) -> Optional[bytes]:
    """The DSF inside a 7z-compressed DSF file, using the bundled 7-Zip."""
    try:
        try:
            from autoortho.utils.sevenzip import SevenZipFile
        except ImportError:
            from utils.sevenzip import SevenZipFile
        with SevenZipFile(path) as archive:
            names = archive.getnames()
            return archive.extract_to_bytes(names[0]) if names else None
    except Exception as exc:
        log.debug("Tile stream: could not unpack %s: %s", path, exc)
        return None


def terrain_tiles(names: Iterable[str]) -> List[Tuple[int, int, str, int]]:
    """(row, col, maptype, zoom) for each ortho .ter in a DSF terrain list."""
    tiles = set()
    for name in names:
        base = name.rsplit("/", 1)[-1]
        m = TER_NAME_RE.match(base)
        if m:
            tiles.add((int(m["row"]), int(m["col"]), m["maptype"], int(m["zoom"])))
    return sorted(tiles)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def _mask_key(rel: str) -> Optional[int]:
    """Integer key of a water mask path, or None for any other file."""
    match = _MASK_NAME.fullmatch(rel)
    if not match:
        return None
    row, col, zoom = (int(g) for g in match.groups())
    if row >= 1 << 28 or col >= 1 << 28 or zoom >= 1 << 8:
        return None
    return (row << 36) | (col << 8) | zoom


def _u64_array(raw: bytes) -> array.array:
    keys = array.array("Q")
    keys.frombytes(raw)
    if sys.byteorder == "big":
        keys.byteswap()  # stored little-endian
    return keys


def pack_files(files: Dict[str, list]) -> dict:
    """Format 2 ``files`` block from a {path: entry} mapping.

    Parsing a million entries as JSON lists costs about 500 MB that Python
    keeps after they are freed, so entries are packed into one blob
    (24 bytes each): first those of ``names``, sorted, then the water masks
    in the order of ``masks``, their sorted integer keys (little-endian
    uint64).
    """
    names, masks = [], []
    for rel, entry in files.items():
        key = _mask_key(rel)
        if key is None:
            names.append((rel, entry))
        else:
            masks.append((key, entry))
    names.sort()
    masks.sort()
    keys = array.array("Q", (key for key, _ in masks))
    if sys.byteorder == "big":
        keys.byteswap()
    entries = b"".join(_PACKED_ENTRY.pack(*entry) for _, entry in names + masks)
    return {
        "names": [rel for rel, _ in names],
        "masks": base64.b64encode(keys.tobytes()).decode("ascii"),
        "entries": base64.b64encode(entries).decode("ascii"),
    }


class TileIndex:
    """Where each streamable file lives inside the published zips."""

    def __init__(self, data: dict):
        fmt = data.get("format")
        if fmt not in READABLE_INDEX_FORMATS:
            raise ValueError(f"Unsupported tile index format {fmt!r}")
        self.name = data.get("name", "")
        self.variant = data.get("variant", "")
        self.built = data.get("built", 0)
        self.assets: List[dict] = data["assets"]
        files = data["files"] if fmt == 2 else pack_files(data["files"])
        self._names: Dict[str, int] = {rel: i for i, rel in enumerate(files["names"])}
        self._mask_keys = _u64_array(base64.b64decode(files["masks"]))
        self._entries = base64.b64decode(files["entries"])
        if len(self._entries) != len(self) * _PACKED_ENTRY.size:
            raise ValueError("Tile index entries don't match its file list")
        ter = data.get("ter", {})
        self.ter_params: dict = ter.get("params", {})
        self.ter_exceptions: Dict[str, str] = ter.get("exceptions", {})
        self._dsf_dirs: Dict[str, List[str]] = {}
        for rel in self._names:
            if rel.startswith(DSF_DIR + "/") and rel.endswith(".dsf"):
                _, folder, fname = rel.split("/")
                self._dsf_dirs.setdefault(folder, []).append(fname)

    def __len__(self) -> int:
        return len(self._names) + len(self._mask_keys)

    @property
    def dsf_count(self) -> int:
        return sum(len(names) for names in self._dsf_dirs.values())

    @classmethod
    def load(cls, path: str) -> "TileIndex":
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def lookup(self, rel: str) -> Optional[list]:
        """The entry of a file, or None if the index doesn't have it."""
        slot = self._names.get(rel)
        if slot is None:
            key = _mask_key(rel)
            if key is None:
                return None
            keys = self._mask_keys
            pos = bisect.bisect_left(keys, key)
            if pos == len(keys) or keys[pos] != key:
                return None
            slot = len(self._names) + pos
        return list(_PACKED_ENTRY.unpack_from(self._entries, slot * _PACKED_ENTRY.size))

    def dsf_folders(self) -> List[str]:
        return sorted(self._dsf_dirs)

    def dsf_names(self, folder: str) -> List[str]:
        return sorted(self._dsf_dirs.get(folder, ()))

    def asset_url(self, asset: int) -> str:
        return self.assets[asset]["url"]

    def cells(self) -> set:
        """Every tile the index has a DSF for."""
        out = set()
        for names in self._dsf_dirs.values():
            for name in names:
                cell = dsf_cell(name)
                if cell is not None:
                    out.add(cell)
        return out

    def regions(self) -> Dict[str, List[Tuple[int, int]]]:
        """Tiles per area (the ``region`` of the zip each DSF is in)."""
        out: Dict[str, List[Tuple[int, int]]] = {}
        for folder, names in self._dsf_dirs.items():
            for name in names:
                entry = self.lookup(f"{DSF_DIR}/{folder}/{name}")
                cell = dsf_cell(name)
                if entry is None or cell is None:
                    continue
                asset = self.assets[entry[_ENTRY_ASSET]]
                region = asset.get("region") or ""
                out.setdefault(region, []).append(cell)
        return out


# ---------------------------------------------------------------------------
# Remote access
# ---------------------------------------------------------------------------

class ThrottledError(Exception):
    """The host refused us; stop asking for a while."""


class RemoteAssets:
    """Range reads against release assets, with signed-URL reuse and backoff.

    A GitHub download link answers with a redirect to a signed storage URL
    that stays valid for about an hour. Only that first step touches
    github.com, so the signed URL is kept and reused for every later read of
    the same asset until shortly before it expires.
    """

    SIGNED_URL_MARGIN = 120.0
    DEFAULT_SIGNED_TTL = 600.0
    MIN_COOLDOWN = 60.0
    MAX_COOLDOWN = 900.0
    # (connect, read) seconds. The read timeout is the longest silence
    # allowed between bytes, not the whole download, so it can be short: a
    # dropped CDN connection then costs seconds, not the OS's ~20 s reset.
    DEFAULT_TIMEOUT = (5.0, 10.0)
    ATTEMPTS = 3
    RETRY_ERRORS = (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout)
    # Pooled connections idle longer than this are closed before the next
    # request. CDNs and home routers drop idle connections without telling
    # either end; reusing one then stalls until the read timeout. A fresh
    # TLS connection costs ~0.1 s.
    IDLE_RESET = 30.0

    def __init__(self, session: Optional[requests.Session] = None,
                 timeout=None, clock: Callable[[], float] = time.time):
        if session is None:
            session = requests.Session()
            # Room for every download thread of a package to keep a connection.
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        self.session = session
        self.session.headers.setdefault("User-Agent", "autoortho-tile-stream")
        self._active = 0
        self._last_used = 0.0
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT
        elif isinstance(timeout, (int, float)):
            timeout = (min(5.0, float(timeout)), float(timeout))
        self.timeout = timeout
        self._clock = clock
        self._signed: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._cooldown = self.MIN_COOLDOWN
        self.stats = {"resolves": 0, "ranges": 0, "bytes": 0, "throttled": 0}

    # -- backoff --------------------------------------------------------
    def in_cooldown(self) -> bool:
        return self._clock() < self._cooldown_until

    def _throttle(self, response) -> None:
        retry_after = None
        try:
            retry_after = float(response.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
        with self._lock:
            wait = max(retry_after or 0.0, self._cooldown)
            self._cooldown_until = self._clock() + wait
            self._cooldown = min(self._cooldown * 2, self.MAX_COOLDOWN)
            self.stats["throttled"] += 1
        log.warning("Tile stream: host returned %s, pausing fetches for %.0fs",
                    response.status_code, wait)

    def _ok(self) -> None:
        self._cooldown = self.MIN_COOLDOWN

    # -- signed URLs ----------------------------------------------------
    def _resolve(self, url: str) -> str:
        now = self._clock()
        with self._lock:
            cached = self._signed.get(url)
        if cached and cached[1] > now:
            return cached[0]
        r = self.session.get(url, headers={"Range": "bytes=0-0"},
                             allow_redirects=False, timeout=self.timeout)
        self.stats["resolves"] += 1
        if r.status_code in (301, 302, 303, 307, 308):
            signed = r.headers["Location"]
            expires = _signed_expiry(signed, now) or now + self.DEFAULT_SIGNED_TTL
        elif r.status_code in (200, 206):
            signed, expires = url, now + 86400.0  # host serves ranges directly
        elif r.status_code in (403, 429) or r.status_code >= 500:
            self._throttle(r)
            raise ThrottledError(f"{r.status_code} resolving {url}")
        else:
            raise IOError(f"HTTP {r.status_code} resolving {url}")
        with self._lock:
            self._signed[url] = (signed, expires - self.SIGNED_URL_MARGIN)
        return signed

    def _begin(self) -> None:
        """Start a request, first dropping connections that sat idle."""
        now = time.monotonic()
        with self._lock:
            stale = (self._active == 0 and self._last_used
                     and now - self._last_used > self.IDLE_RESET)
            self._active += 1
            self._last_used = now
        if stale:
            close = getattr(self.session, "close", None)
            if close is not None:
                close()  # the session reconnects on the next request
            self.stats["idle_resets"] = self.stats.get("idle_resets", 0) + 1

    def _end(self) -> None:
        with self._lock:
            self._active -= 1
            self._last_used = time.monotonic()

    def read_range(self, url: str, start: int, end: int) -> bytes:
        """Bytes ``start`` to ``end`` inclusive."""
        if self.in_cooldown():
            raise ThrottledError("cooling down")
        self._begin()
        try:
            return self._read_range(url, start, end)
        finally:
            self._end()

    def _read_range(self, url: str, start: int, end: int) -> bytes:
        refreshed = False
        last_error: Optional[Exception] = None
        for _attempt in range(self.ATTEMPTS):
            try:
                signed = self._resolve(url)
                r = self.session.get(signed, headers={"Range": f"bytes={start}-{end}"},
                                     timeout=self.timeout)
            except self.RETRY_ERRORS as exc:  # dropped or stalled connection
                last_error = exc
                log.debug("Tile stream: reading %s failed (%s), retrying", url, exc)
                continue
            if r.status_code == 206:
                self._ok()
                self.stats["ranges"] += 1
                self.stats["bytes"] += len(r.content)
                return r.content
            if r.status_code == 200 and start == 0:
                self._ok()
                return r.content[:end + 1]
            if r.status_code in (401, 403) and not refreshed and signed != url:
                refreshed = True
                with self._lock:  # signed URL expired early; get a new one
                    self._signed.pop(url, None)
                continue
            if r.status_code in (403, 429) or r.status_code >= 500:
                self._throttle(r)
                raise ThrottledError(f"{r.status_code} reading {url}")
            raise IOError(f"HTTP {r.status_code} reading {url} [{start}-{end}]")
        raise IOError(f"Could not read {url}: {last_error}")


def _signed_expiry(url: str, now: float) -> Optional[float]:
    """Expiry time of a signed storage URL, if it says."""
    q = parse_qs(urlparse(url).query)
    if "se" in q:  # Azure SAS
        try:
            return datetime.strptime(q["se"][0], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    if "X-Amz-Expires" in q:  # S3
        try:
            return now + float(q["X-Amz-Expires"][0])
        except ValueError:
            return None
    return None


def extract_member(raw: bytes, entry: list) -> Tuple[Optional[bytes], int]:
    """Decode one zip member from bytes starting at its local header.

    Returns (data, needed): data is None when ``raw`` is too short, and
    ``needed`` is then the total length required.
    """
    if len(raw) < _LOCAL_HEADER.size:
        return None, _LOCAL_HEADER.size + _LOCAL_HEADER_SLACK + entry[_ENTRY_CSIZE]
    fields = _LOCAL_HEADER.unpack_from(raw, 0)
    if fields[0] != _LOCAL_HEADER_SIG:
        raise IOError("Bad zip local header")
    name_len, extra_len = fields[9], fields[10]
    start = _LOCAL_HEADER.size + name_len + extra_len
    stop = start + entry[_ENTRY_CSIZE]
    if len(raw) < stop:
        return None, stop
    payload = raw[start:stop]
    method = entry[_ENTRY_METHOD]
    if method == 0:
        data = payload
    elif method == 8:
        data = zlib.decompress(payload, -15)
    else:
        raise IOError(f"Unsupported zip compression method {method}")
    if len(data) != entry[_ENTRY_SIZE]:
        raise IOError(f"Size mismatch: {len(data)} != {entry[_ENTRY_SIZE]}")
    if zlib.crc32(data) & 0xFFFFFFFF != entry[_ENTRY_CRC]:
        raise IOError("CRC mismatch")
    return data, stop


def asset_read(remote: RemoteAssets, asset: dict, start: int, end: int) -> bytes:
    """Bytes ``start``..``end`` of an asset, which may be split into parts.

    Overlay zips are published as raw byte splits (``.zip.00``, ``.zip.01``
    ...) because of GitHub's 2 GiB file limit; offsets run across them.
    """
    parts = asset.get("parts")
    if not parts:
        return remote.read_range(asset["url"], start, end)
    out = []
    base = 0
    for part in parts:
        p_start, p_end = base, base + part["size"] - 1
        if end >= p_start and start <= p_end:
            lo, hi = max(start, p_start), min(end, p_end)
            out.append(remote.read_range(part["url"], lo - base, hi - base))
        base += part["size"]
        if base > end:
            break
    return b"".join(out)


def fetch_member(remote: RemoteAssets, index: TileIndex, entry: list) -> bytes:
    """Download and decode one file from its release zip."""
    asset = index.assets[entry[_ENTRY_ASSET]]
    offset = entry[_ENTRY_OFFSET]
    want = _LOCAL_HEADER.size + _LOCAL_HEADER_SLACK + entry[_ENTRY_CSIZE]
    if asset.get("size"):
        want = min(want, asset["size"] - offset)
    raw = asset_read(remote, asset, offset, offset + want - 1)
    data, needed = extract_member(raw, entry)
    if data is None:
        raw += asset_read(remote, asset, offset + len(raw), offset + needed - 1)
        data, _ = extract_member(raw, entry)
        if data is None:
            raise IOError("Short read from release asset")
    return data


# Members closer than this are read in one request, the bytes between them
# discarded. A tile's water masks are usually one unbroken run in the zip
# (XP12 set: 48 masks per tile, one run for 90% of tiles), so a tile costs
# two requests instead of fifty.
COALESCE_GAP = 256 * 1024
COALESCE_SPAN = 64 * 1024 * 1024   # largest single read


def _member_span(entry: list) -> int:
    """Bytes from a member's local header to the end of its data, assuming
    the name and extra fields fit the usual slack."""
    return _LOCAL_HEADER.size + _LOCAL_HEADER_SLACK + entry[_ENTRY_CSIZE]


def coalesce_runs(entries: List[list], max_gap: int = COALESCE_GAP,
                  max_span: int = COALESCE_SPAN) -> List[List[int]]:
    """Group entries (by position in ``entries``) into runs that one Range
    request each can read: same asset, in offset order, gaps under
    ``max_gap``, at most ``max_span`` bytes unless one member alone is
    larger."""
    order = sorted(range(len(entries)),
                   key=lambda i: (entries[i][_ENTRY_ASSET], entries[i][_ENTRY_OFFSET]))
    runs: List[List[int]] = []
    start = end = 0
    for i in order:
        e = entries[i]
        e_start, e_end = e[_ENTRY_OFFSET], e[_ENTRY_OFFSET] + _member_span(e)
        if (runs and e[_ENTRY_ASSET] == entries[runs[-1][0]][_ENTRY_ASSET]
                and e_start - end <= max_gap and max(end, e_end) - start <= max_span):
            runs[-1].append(i)
            end = max(end, e_end)
        else:
            runs.append([i])
            start, end = e_start, e_end
    return runs


def fetch_run(remote: RemoteAssets, index: TileIndex, entries: List[list]) -> List[bytes]:
    """Download several members of one asset with a single Range request
    (see ``coalesce_runs``). Returns their data in the order given."""
    if len(entries) == 1:
        return [fetch_member(remote, index, entries[0])]
    asset = index.assets[entries[0][_ENTRY_ASSET]]
    start = min(e[_ENTRY_OFFSET] for e in entries)
    end = max(e[_ENTRY_OFFSET] + _member_span(e) for e in entries)
    if asset.get("size"):
        end = min(end, asset["size"])
    raw = asset_read(remote, asset, start, end - 1)
    out = []
    for e in entries:
        data, _ = extract_member(raw[e[_ENTRY_OFFSET] - start:], e)
        if data is None:  # long extra field ran past the read
            data = fetch_member(remote, index, e)
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Package runtime
# ---------------------------------------------------------------------------

def read_marker(root: str) -> Optional[dict]:
    path = os.path.join(root, MARKER_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("Tile stream: unreadable %s: %s", path, exc)
        return None


class PriorityWorkers:
    """A small thread pool that runs the lowest priority number first.

    Jobs of equal priority run in submission order.
    """

    def __init__(self, workers: int, name: str):
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._order = itertools.count()
        self._stopped = False
        self._threads = [threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
                         for i in range(max(1, workers))]
        for t in self._threads:
            t.start()

    def submit(self, priority: int, fn: Callable, *args) -> None:
        if self._stopped:
            raise RuntimeError("PriorityWorkers is shut down")
        self._queue.put((priority, next(self._order), fn, args))

    def _run(self) -> None:
        while True:
            _, _, fn, args = self._queue.get()
            if fn is None:
                return
            try:
                fn(*args)
            except Exception as exc:  # jobs handle their own errors
                log.debug("Tile stream: background job failed: %s", exc)

    def shutdown(self) -> None:
        self._stopped = True
        for _ in self._threads:  # sorts after every queued job
            self._queue.put((float("inf"), next(self._order), None, ()))


class StreamPackage:
    """Runtime for one streaming scenery folder.

    Fetched files are written into the package folder itself, so once a tile
    has been fetched every other part of AutoOrtho sees an ordinary file.

    Three queues keep what X-Plane needs soonest first:

    - opens (X-Plane is waiting) go to the foreground pool;
    - water masks go to their own pool, never capped, with the masks of
      tiles X-Plane has opened ahead of those of prefetched tiles. X-Plane
      reads a tile's masks seconds after its DSF, and masks are tiny, so they
      must not wait behind (or be dropped for) 15 MB neighbour DSFs;
    - neighbour DSFs go to the background pool, at most ``MAX_BACKGROUND``
      queued.

    The folder is kept under ``disk_limit_gb`` by deleting the least
    recently used files.
    """

    PROTECT_SECONDS = 2 * 3600   # never evict files used in the last two hours
    EVICT_INTERVAL = 60.0
    MAX_BACKGROUND = 32          # neighbour DSFs queued at once
    MASK_URGENT = 0              # masks of a tile X-Plane has opened
    MASK_AHEAD = 1               # masks of a prefetched tile

    def __init__(self, root: str, marker: dict, index: Optional[TileIndex] = None,
                 remote: Optional[RemoteAssets] = None,
                 on_terrain: Optional[Callable[[List[Tuple[int, int, str, int]]], None]] = None,
                 hidden: Optional[Iterable[str]] = None):
        self.root = os.path.abspath(root)
        self.marker = marker
        self.kind = marker.get("kind", "ortho")
        self.index = index or TileIndex.load(self._index_path(marker))
        self.remote = remote or RemoteAssets(timeout=marker.get("http_timeout"))
        self.open_timeout = float(marker.get("open_timeout", 30))
        self.roughness = marker.get("roughness")
        self.prefetch_ring = int(marker.get("prefetch_ring", 1))
        # False: only tiles already stored are offered to X-Plane (the rest
        # come from Global Scenery) and no DSF is downloaded while flying.
        self.fetch_missing = bool(marker.get("fetch_missing", True))
        limit_gb = marker.get("disk_limit_gb")
        self.disk_limit = int(float(limit_gb) * 1e9) if limit_gb else 0
        self.on_terrain = on_terrain
        self.hidden = set(hidden or ())
        self._pool = ThreadPoolExecutor(max_workers=int(marker.get("workers", 3)),
                                        thread_name_prefix="tile-stream")
        self._bg_pool = ThreadPoolExecutor(max_workers=int(marker.get("prefetch_workers", 2)),
                                           thread_name_prefix="tile-prefetch")
        self._mask_pool = PriorityWorkers(int(marker.get("mask_workers", 4)), "tile-mask")
        self._mask_priority: Dict[str, int] = {}  # queued masks -> priority
        # Foreground (X-Plane is waiting) and background downloads are tracked
        # apart, so an open never waits on, or is woken by, a prefetch.
        self._inflight_fg: Dict[str, threading.Event] = {}
        self._inflight_bg: Dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()
        self._replace_lock = threading.Lock()
        self._background_pending = 0
        self._failed: Dict[str, float] = {}
        self._failed_open: Dict[str, float] = {}
        self._noted: set = set()
        self._ter_cache: Dict[str, bytes] = {}
        self._ter_lock = threading.Lock()
        self._usage: Optional[int] = None
        self._usage_lock = threading.Lock()
        self._last_evict = 0.0
        self.stats = {"fetched": 0, "prefetched": 0, "evicted": 0, "failed": 0}
        for sub in (DSF_DIR, "terrain", "textures"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        for folder in self.index.dsf_folders():
            os.makedirs(os.path.join(self.root, DSF_DIR, folder), exist_ok=True)
        log.info("Tile stream: %s ready with %d DSFs (%s, %s, %s), %d hidden, limit %s",
                 os.path.basename(self.root), sum(len(self.index.dsf_names(f))
                 for f in self.index.dsf_folders()), self.index.name,
                 self.index.variant, self.kind, len(self.hidden),
                 f"{self.disk_limit/1e9:.0f} GB" if self.disk_limit else "none")

    def _index_path(self, marker: dict) -> str:
        path = marker.get("index", INDEX_NAME)
        if not os.path.isabs(path):
            path = os.path.join(self.root, path)
        return path

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._bg_pool.shutdown(wait=False, cancel_futures=True)
        self._mask_pool.shutdown()

    @staticmethod
    def _is_mask(rel: str) -> bool:
        return rel.startswith("textures/")

    # -- listing --------------------------------------------------------
    def dsf_names(self, folder: str) -> List[str]:
        """DSFs to list beside the stored ones (none when only stored tiles
        are offered)."""
        if not self.fetch_missing:
            return []
        return [n for n in self.index.dsf_names(folder)
                if f"{DSF_DIR}/{folder}/{n}" not in self.hidden]

    def streams(self, rel: str) -> bool:
        """Whether X-Plane may be offered ``rel`` before it is stored. Masks
        always are: a stored tile must never miss one."""
        if self.fetch_missing or not rel.endswith(".dsf"):
            return self.entry(rel) is not None
        return False

    def entry(self, rel: str) -> Optional[list]:
        if rel in self.hidden:
            return None
        return self.index.lookup(rel)

    def remote_size(self, rel: str) -> Optional[int]:
        """Size of a file X-Plane may be offered before it is stored."""
        e = self.entry(rel) if self.streams(rel) else None
        return e[_ENTRY_SIZE] if e else None

    def recently_failed(self, rel: str, window: float = 300.0) -> bool:
        return self._recent(self._failed, rel, window)

    def unavailable(self, rel: str) -> bool:
        """True if a fetch now would not be tried (throttled or just failed)."""
        return self.remote.in_cooldown() or self.recently_failed(rel)

    def local_path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    # -- fetching -------------------------------------------------------
    OPEN_ATTEMPTS = 3

    def ensure_local(self, rel: str, timeout: Optional[float] = None) -> Optional[str]:
        """Path to ``rel`` on disk, fetching it first if needed.

        X-Plane is waiting, so this uses its own download (never one queued
        behind a prefetch of the same file) and retries a failed download
        while time remains. A prefetch that failed doesn't stop it; an open
        that failed in the last minutes does, so an offline sim doesn't wait
        on every file.
        """
        local = self.local_path(rel)
        if os.path.exists(local):
            return local
        if (self.entry(rel) is None or self.remote.in_cooldown()
                or self._recent(self._failed_open, rel)):
            return None
        deadline = time.monotonic() + (self.open_timeout if timeout is None else timeout)
        for _ in range(self.OPEN_ATTEMPTS):
            event = self._start_fetch(rel, background=False)
            event.wait(max(0.0, deadline - time.monotonic()))
            if os.path.exists(local):
                return local
            if time.monotonic() >= deadline or self.remote.in_cooldown():
                break
        self._failed_open[rel] = time.time()
        return None

    @staticmethod
    def _recent(failures: Dict[str, float], rel: str, window: float = 300.0) -> bool:
        t = failures.get(rel)
        return t is not None and time.time() - t < window

    def prefetch(self, rel: str, priority: int = MASK_AHEAD) -> bool:
        """Fetch ``rel`` in the background if it isn't on disk yet.

        ``priority`` orders water masks (lower first); neighbour DSFs are
        skipped while ``MAX_BACKGROUND`` of them are already queued.
        """
        if (os.path.exists(self.local_path(rel)) or self.entry(rel) is None
                or self.unavailable(rel)):
            return False
        if not self._is_mask(rel) and self._background_pending >= self.MAX_BACKGROUND:
            return False
        self._start_fetch(rel, background=True, priority=priority)
        return True

    def placeholder_png(self, rel: str) -> bytes:
        """A stand-in for a water mask that couldn't be fetched in time.

        X-Plane stops with an error when a terrain file's mask is missing.
        An all-black mask (all sea) of the expected size keeps it flying;
        that tile shows default water until the scenery reloads.
        """
        size = 256
        m = re.search(r"_ZL(\d+)\.png$", rel)
        if m:
            params = dict(DEFAULT_TER_PARAMS, **(self.index.ter_params or {}))
            try:
                size = max(1, 4096 // 2 ** (int(m.group(1)) - int(params["mask_zl"])))
            except (KeyError, ValueError):
                pass
        return blank_mask_png(min(size, 4096))

    def prefetch_neighbors(self, rel: str) -> int:
        """Queue the DSFs around ``rel``; X-Plane loads them next.

        Opening a tile at the edge of the loaded area means the aircraft is
        heading that way, so the ring around it also covers the tiles ahead.
        """
        m = re.search(r"([+-]\d{2})([+-]\d{3})\.dsf$", rel)
        if not m or self.prefetch_ring <= 0 or not self.fetch_missing:
            return 0
        lat0, lon0 = int(m.group(1)), int(m.group(2))
        queued = 0
        ring = self.prefetch_ring
        for dlat in range(-ring, ring + 1):
            for dlon in range(-ring, ring + 1):
                if dlat == 0 and dlon == 0:
                    continue
                lat, lon = lat0 + dlat, (lon0 + dlon + 180) % 360 - 180
                if not -90 <= lat < 90:
                    continue
                queued += self.prefetch(dsf_rel(lat, lon))
        return queued

    def _start_fetch(self, rel: str, background: bool,
                     priority: int = MASK_AHEAD) -> threading.Event:
        """Queue a download of ``rel``; the event is set when it ends.

        A foreground request for a file already being prefetched starts its
        own download rather than waiting: the prefetch may be stuck on a
        dropped connection. Whichever finishes first stores the file. A mask
        already queued is queued again if it has become more urgent.
        """
        mask = background and self._is_mask(rel)
        with self._inflight_lock:
            inflight = self._inflight_bg if background else self._inflight_fg
            event = inflight.get(rel)
            if event is None and background:
                event = self._inflight_fg.get(rel)  # already being opened
            if event is not None:
                if not (mask and rel in self._mask_priority
                        and priority < self._mask_priority[rel]):
                    return event
                self._mask_priority[rel] = priority  # bump: run it sooner
            else:
                event = threading.Event()
                inflight[rel] = event
                if mask:
                    self._mask_priority[rel] = priority
                elif background:
                    self._background_pending += 1
        try:
            if mask:
                self._mask_pool.submit(priority, self._fetch, rel, event, True)
            else:
                pool = self._bg_pool if background else self._pool
                pool.submit(self._fetch, rel, event, background)
        except RuntimeError:  # pool shut down
            self._fetch_done(rel, event, background)
        return event

    def _fetch_done(self, rel: str, event: threading.Event, background: bool) -> None:
        with self._inflight_lock:
            inflight = self._inflight_bg if background else self._inflight_fg
            owner = inflight.get(rel) is event
            if owner:
                inflight.pop(rel, None)
            if background and self._is_mask(rel):
                if owner:
                    self._mask_priority.pop(rel, None)
            elif background:
                self._background_pending -= 1
        event.set()

    def _fetch(self, rel: str, event: threading.Event, background: bool = False) -> None:
        started = time.monotonic()
        local = self.local_path(rel)
        tmp = f"{local}.part{threading.get_ident()}"
        try:
            if os.path.exists(local):
                return
            data = fetch_member(self.remote, self.index, self.index.lookup(rel))
            if not self._store(local, tmp, data):
                return  # the other download of this file finished first
            self._failed.pop(rel, None)
            self._failed_open.pop(rel, None)
            self.stats["prefetched" if background else "fetched"] += 1
            log.info("Tile stream: %s %s (%.1f MB) in %.2fs",
                     "prefetched" if background else "fetched", rel,
                     len(data) / 1e6, time.monotonic() - started)
            self._account(len(data))
            if rel.endswith(".dsf"):
                self._after_dsf(data, local, urgent=not background)
        except ThrottledError as exc:
            self._failed[rel] = time.time()
            self.stats["failed"] += 1
            log.info("Tile stream: %s not fetched: %s", rel, exc)
        except Exception as exc:
            self._failed[rel] = time.time()
            self.stats["failed"] += 1
            log.warning("Tile stream: %s %s failed: %s",
                        "prefetching" if background else "fetching", rel, exc)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self._fetch_done(rel, event, background)

    def _store(self, local: str, tmp: str, data: bytes) -> bool:
        """Write ``data`` to ``local`` atomically unless it appeared meanwhile."""
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(data)
        with self._replace_lock:
            if os.path.exists(local):
                return False
            os.replace(tmp, local)
        return True

    # -- keeping tiles for offline use ----------------------------------
    def fetch_now(self, rel: str) -> int:
        """Download ``rel`` on this thread. Returns the bytes stored (0 if it
        was already on disk or isn't in the index). Raises on failure."""
        local = self.local_path(rel)
        if os.path.exists(local) or self.entry(rel) is None:
            return 0
        tmp = f"{local}.part{threading.get_ident()}"
        try:
            data = fetch_member(self.remote, self.index, self.index.lookup(rel))
            if not self._store(local, tmp, data):
                return 0
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        self._account(len(data))
        return len(data)

    def fetch_many(self, rels: Iterable[str],
                   cancelled: Callable[[], bool] = lambda: False) -> Tuple[int, int]:
        """Download files not on disk yet, neighbours in a zip read with one
        request (``coalesce_runs``). Returns (bytes, files) stored. Raises
        on failure; files stored before it stay."""
        wanted, entries = [], []
        for rel in dict.fromkeys(rels):
            entry = self.entry(rel)
            if entry is None or os.path.exists(self.local_path(rel)):
                continue
            wanted.append(rel)
            entries.append(entry)
        total = files = 0
        for run in coalesce_runs(entries):
            if cancelled():
                break
            datas = fetch_run(self.remote, self.index, [entries[i] for i in run])
            for i, data in zip(run, datas):
                local = self.local_path(wanted[i])
                tmp = f"{local}.part{threading.get_ident()}"
                try:
                    stored = self._store(local, tmp, data)
                finally:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
                if stored:
                    self._account(len(data))
                    total += len(data)
                    files += 1
        return total, files

    def tile_masks(self, lat: int, lon: int) -> List[str]:
        """Water masks the stored DSF of a tile needs (package-relative)."""
        local = self.local_path(dsf_rel(lat, lon))
        try:
            with open(local, "rb") as fh:
                data = fh.read()
        except OSError:
            return []
        if data.startswith(SEVENZIP_MAGIC):
            data = unpack_7z_dsf(local) or b""
        return self.mask_rels(dsf_terrain_names(data) or [])

    def mask_rels(self, names: Iterable[str]) -> List[str]:
        """Water masks named by the overlay .ter files in a terrain list."""
        out = []
        for name in names:
            if not name.startswith("terrain/") or "_overlay" not in name:
                continue
            ter = self.ter_bytes(name.split("/", 1)[1])
            if ter:
                out.extend(border_textures(ter))
        return out

    def download_tile(self, lat: int, lon: int,
                      cancelled: Callable[[], bool] = lambda: False) -> Tuple[int, int]:
        """Fetch a tile's DSF and water masks now. Returns (bytes, files)
        downloaded; a tile the index doesn't have returns (0, 0)."""
        rel = dsf_rel(lat, lon)
        if self.entry(rel) is None:
            return 0, 0
        got = self.fetch_now(rel)
        if cancelled():
            return got, int(got > 0)
        mask_bytes, mask_files = self.fetch_many(self.tile_masks(lat, lon), cancelled)
        return got + mask_bytes, int(got > 0) + mask_files

    # -- disk budget ----------------------------------------------------
    def touch(self, local: str) -> None:
        """Mark a file as recently used (eviction goes by modification time)."""
        try:
            os.utime(local, None)
        except OSError:
            pass

    def _stored_files(self) -> List[os.DirEntry]:
        out = []
        dsf_root = os.path.join(self.root, DSF_DIR)
        try:
            dirs = [os.path.join(dsf_root, d) for d in os.listdir(dsf_root)]
        except OSError:
            dirs = []
        for sub in dirs + [os.path.join(self.root, "textures")]:
            try:
                for e in os.scandir(sub):
                    if e.is_file() and e.name.endswith((".dsf", ".png")):
                        out.append(e)
            except OSError:
                continue
        return out

    @staticmethod
    def _stored_rel(entry: os.DirEntry) -> str:
        """Package-relative path of a stored DSF or mask."""
        if entry.name.endswith(".png"):
            return f"textures/{entry.name}"
        folder = os.path.basename(os.path.dirname(entry.path))
        return f"{DSF_DIR}/{folder}/{entry.name}"

    def usage(self) -> Tuple[int, int]:
        """(bytes, DSF count) stored in the package folder."""
        files = self._stored_files()
        total = sum(e.stat().st_size for e in files)
        with self._usage_lock:
            self._usage = total
        return total, sum(1 for e in files if e.name.endswith(".dsf"))

    def _account(self, added: int) -> None:
        if not self.disk_limit:
            return
        with self._usage_lock:
            if self._usage is not None:
                self._usage += added
            over = self._usage is None or self._usage > self.disk_limit
            due = time.monotonic() - self._last_evict > self.EVICT_INTERVAL
            if not (over and due):
                return
            self._last_evict = time.monotonic()
        try:
            self._bg_pool.submit(self.evict)
        except RuntimeError:
            pass

    def evict(self) -> int:
        """Delete least recently used files until under the disk limit.

        Files of pinned tiles are never deleted and don't count toward the
        limit.
        """
        if not self.disk_limit:
            return 0
        pins = read_pins(self.root)
        stats = {}
        for e in self._stored_files():
            if pins and stored_file_cell(e.name) in pins:
                continue
            if self.index.lookup(self._stored_rel(e)) is None:
                continue  # can't be downloaded again (moved in from a region)
            try:
                stats[e.path] = e.stat()
            except OSError:
                continue
        total = sum(st.st_size for st in stats.values())
        removed = 0
        if total > self.disk_limit:
            cutoff = time.time() - self.PROTECT_SECONDS
            for path, st in sorted(stats.items(), key=lambda kv: kv[1].st_mtime):
                if total <= self.disk_limit * 0.9 or st.st_mtime >= cutoff:
                    break  # under budget, or everything left was used recently
                try:
                    os.remove(path)
                except OSError:
                    continue
                total -= st.st_size
                removed += 1
        with self._usage_lock:
            self._usage = total
        self.stats["evicted"] += removed
        if removed:
            log.info("Tile stream: removed %d least recently used files, %.1f GB stored",
                     removed, total / 1e9)
        return removed

    def note_local_dsf(self, local_path: str) -> None:
        """X-Plane opened a DSF already on disk: make sure its masks follow.

        Its masks were queued when it was prefetched, unless that was in an
        earlier session; queuing them again as urgent is cheap either way.
        """
        key = os.path.normcase(local_path)
        with self._inflight_lock:
            if key in self._noted:
                return
            self._noted.add(key)

        def work():
            try:
                with open(local_path, "rb") as fh:
                    self._after_dsf(fh.read(), local_path, urgent=True)
            except OSError as exc:
                log.debug("Tile stream: could not read %s: %s", local_path, exc)
        try:
            self._mask_pool.submit(self.MASK_URGENT, work)
        except RuntimeError:
            pass

    def _after_dsf(self, data: bytes, path: Optional[str] = None,
                   urgent: bool = False) -> None:
        """Queue the tile's water masks and report its textures."""
        if data.startswith(SEVENZIP_MAGIC) and path:
            data = unpack_7z_dsf(path) or b""
        names = dsf_terrain_names(data)
        if names is None:
            return
        self.note_terrain(names, urgent=urgent)

    def note_terrain(self, names: List[str], urgent: bool = False) -> None:
        tiles = terrain_tiles(names)
        if tiles and self.on_terrain:
            try:
                self.on_terrain(tiles)
            except Exception as exc:
                log.debug("Tile stream: terrain callback failed: %s", exc)
        for tex in self.mask_rels(names):
            self.prefetch(tex, self.MASK_URGENT if urgent else self.MASK_AHEAD)

    # -- terrain --------------------------------------------------------
    def ter_bytes(self, name: str) -> Optional[bytes]:
        """A terrain file: the real one in ``terrain/`` if the package has it
        (tiles moved in from another set keep their own), else generated."""
        with self._ter_lock:
            cached = self._ter_cache.get(name)
        if cached is not None:
            return cached
        data = None
        try:
            with open(os.path.join(self.root, "terrain", name), "rb") as fh:
                data = fh.read()
        except OSError:
            pass
        if data is None:
            data = synthesize_ter(name, self.index.ter_params,
                                  self.index.ter_exceptions, self.roughness)
        if data is not None:
            with self._ter_lock:
                if len(self._ter_cache) > 50000:
                    self._ter_cache.clear()
                self._ter_cache[name] = data
        return data


def installed_dsfs(scenery_dir: str, exclude: str) -> set:
    """DSFs present in the other packages beside a streaming one."""
    found = set()
    exclude = os.path.normcase(os.path.abspath(exclude))
    try:
        packages = list(os.scandir(scenery_dir))
    except OSError:
        return found
    for pkg in packages:
        if not pkg.is_dir() or os.path.normcase(os.path.abspath(pkg.path)) == exclude:
            continue
        if os.path.isfile(os.path.join(pkg.path, MARKER_NAME)):
            continue
        dsf_root = os.path.join(pkg.path, DSF_DIR)
        try:
            folders = list(os.scandir(dsf_root))
        except OSError:
            continue
        for folder in folders:
            if not folder.is_dir():
                continue
            try:
                for f in os.scandir(folder.path):
                    if f.name.endswith(".dsf"):
                        found.add(f"{DSF_DIR}/{folder.name}/{f.name}")
            except OSError:
                continue
    return found


def overlay_hidden_dsfs(custom_scenery_dir: str) -> set:
    """Overlay DSFs already installed by the classic overlay download."""
    found = set()
    dsf_root = os.path.join(custom_scenery_dir, LEGACY_OVERLAY_FOLDER, DSF_DIR)
    try:
        folders = list(os.scandir(dsf_root))
    except OSError:
        return found
    for folder in folders:
        if folder.is_dir():
            try:
                found.update(f"{DSF_DIR}/{folder.name}/{f.name}"
                             for f in os.scandir(folder.path) if f.name.endswith(".dsf"))
            except OSError:
                continue
    return found


# ---------------------------------------------------------------------------
# Empty overlay DSF
# ---------------------------------------------------------------------------

def _atom(atom_id: str, payload: bytes) -> bytes:
    return atom_id[::-1].encode("ascii") + struct.pack("<I", len(payload) + 8) + payload


def _string_table(strings: Iterable[str]) -> bytes:
    return b"".join(s.encode("utf-8") + b"\0" for s in strings)


@functools.lru_cache(maxsize=8)
def blank_mask_png(size: int) -> bytes:
    """An all-black 8-bit grayscale PNG, ``size`` pixels square.

    Ortho4XP water masks are 255 on land (photo shown) and 0 on sea (X-Plane
    water shows through), so black means "all water".
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    pixels = zlib.compress((b"\x00" * (size + 1)) * size, 9)  # filter byte + row
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels)
            + chunk(b"IEND", b""))


def empty_overlay_dsf(lat: int, lon: int) -> bytes:
    """A valid overlay DSF with nothing in it.

    Served in place of an overlay tile that can't be fetched: X-Plane has
    already seen the tile listed, and a missing file would be a load error,
    while Global Scenery can't stand in for an overlay.
    """
    props = ["sim/west", str(lon), "sim/east", str(lon + 1),
             "sim/south", str(lat), "sim/north", str(lat + 1),
             "sim/overlay", "1", "sim/creation_agent", "AutoOrtho"]
    body = (
        _atom("HEAD", _atom("PROP", _string_table(props)))
        + _atom("DEFN", _atom("TERT", b"") + _atom("OBJT", b"")
                + _atom("POLY", b"") + _atom("NETW", b"") + _atom("DEMN", b""))
        + _atom("GEOD", b"")
        + _atom("CMDS", b"")
    )
    data = b"XPLNEDSF" + struct.pack("<I", 1) + body
    return data + hashlib.md5(data).digest()


# ---------------------------------------------------------------------------
# Setting packages up
# ---------------------------------------------------------------------------

def _parse_index_bytes(data: bytes) -> TileIndex:
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
        return TileIndex(json.load(fh))  # validates the format


def download_index(url: str, dest: str, session: Optional[requests.Session] = None,
                   timeout: float = 60.0) -> dict:
    """Fetch a published index to ``dest`` (atomically) and return its summary."""
    session = session or requests.Session()
    r = session.get(url, timeout=timeout, headers={"User-Agent": "autoortho-tile-stream"})
    if r.status_code != 200:
        raise IOError(f"HTTP {r.status_code} downloading {url}")
    parsed = _parse_index_bytes(r.content)
    with open(dest + ".part", "wb") as fh:
        fh.write(r.content)
    os.replace(dest + ".part", dest)
    return {"name": parsed.name, "built": parsed.built, "dsfs": parsed.dsf_count,
            "last_modified": r.headers.get("Last-Modified", "")}


def write_marker(root: str, marker: dict) -> None:
    path = os.path.join(root, MARKER_NAME)
    with open(path + ".part", "w", encoding="utf-8") as fh:
        json.dump(marker, fh, indent=2)
    os.replace(path + ".part", path)


def refresh_index(root: str, marker: dict, max_age_days: float = 7.0,
                  session: Optional[requests.Session] = None) -> bool:
    """Replace a package's index if the published one changed. Best effort.

    The new index is used from the next mount on.
    """
    url = marker.get("index_url")
    if not url:
        return False
    path = os.path.join(root, marker.get("index", INDEX_NAME))
    try:
        if time.time() - os.path.getmtime(path) < max_age_days * 86400:
            return False
    except OSError:
        pass
    session = session or requests.Session()
    headers = {"User-Agent": "autoortho-tile-stream"}
    if marker.get("index_last_modified"):
        headers["If-Modified-Since"] = marker["index_last_modified"]
    try:
        r = session.get(url, timeout=60, headers=headers)
        if r.status_code == 304:
            os.utime(path, None)
            return False
        if r.status_code != 200:
            return False
        _parse_index_bytes(r.content)
        with open(path + ".part", "wb") as fh:
            fh.write(r.content)
        os.replace(path + ".part", path)
        write_marker(root, dict(marker, index_last_modified=r.headers.get("Last-Modified", "")))
        log.info("Tile stream: updated index for %s", os.path.basename(root))
        return True
    except Exception as exc:
        log.info("Tile stream: index refresh for %s skipped: %s", os.path.basename(root), exc)
        return False


def stream_packages(ao_scenery_dir: str) -> Dict[str, dict]:
    """Streaming packages under z_autoortho/scenery: folder name -> marker."""
    out = {}
    try:
        entries = sorted(os.scandir(ao_scenery_dir), key=lambda e: e.name)
    except OSError:
        return out
    for e in entries:
        if e.is_dir():
            marker = read_marker(e.path)
            if marker is not None:
                out[e.name] = marker
    return out


def enable_streaming(ao_scenery_dir: str, source: str, overlays: bool = True,
                     disk_limit_gb: float = 20.0, roughness: Optional[float] = None,
                     session: Optional[requests.Session] = None,
                     fetch_missing: Optional[bool] = None) -> List[str]:
    """Create the streaming package(s) for a source and fetch their indexes.

    Returns the folder names set up. Overlays get a quarter of the disk
    limit (their tiles are smaller).
    """
    spec = SOURCES.get(source)
    if not spec or not spec.get("available"):
        raise ValueError(f"Scenery source {source!r} is not available yet")
    created = [_create_package(ao_scenery_dir, source, "ortho", float(disk_limit_gb),
                               roughness=roughness, session=session,
                               fetch_missing=fetch_missing)]
    if overlays and spec.get("overlay_index"):
        created.append(_create_package(ao_scenery_dir, source, "overlay",
                                       _overlay_limit(disk_limit_gb), session=session,
                                       fetch_missing=fetch_missing))
    else:
        _remove_package(ao_scenery_dir, OVERLAY_PACKAGE.format(source=source))
    return created


def _overlay_limit(disk_limit_gb: float) -> float:
    return max(1.0, float(disk_limit_gb) / 4)


def _create_package(ao_scenery_dir: str, source: str, kind: str, limit: float,
                    roughness: Optional[float] = None,
                    session: Optional[requests.Session] = None,
                    fetch_missing: Optional[bool] = None) -> str:
    """Create (or refresh) one streaming package folder and download its index.
    ``fetch_missing`` None keeps the folder's current choice (default on)."""
    spec = SOURCES[source]
    if kind == "overlay":
        name, url = OVERLAY_PACKAGE.format(source=source), spec.get("overlay_index")
    else:
        name, url = ORTHO_PACKAGE.format(source=source), spec["ortho_index"]
    if not url:
        raise ValueError(f"{spec['label']} has no {kind} tiles")
    root = os.path.join(ao_scenery_dir, name)
    is_new = not os.path.isdir(root)
    old_marker = read_marker(root) or {}
    if fetch_missing is None:
        fetch_missing = old_marker.get("fetch_missing", True)
    os.makedirs(root, exist_ok=True)
    try:
        info = download_index(url, os.path.join(root, INDEX_NAME), session=session)
    except Exception:
        if is_new:  # don't leave an empty folder that would be mounted
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        raise
    marker = {"source": source, "kind": kind, "index": INDEX_NAME,
              "index_url": url, "index_last_modified": info["last_modified"],
              "disk_limit_gb": float(limit)}
    if roughness is not None and kind == "ortho":
        marker["roughness"] = _check_roughness(roughness)
    if not fetch_missing:
        marker["fetch_missing"] = False
    if kind == "ortho" and old_marker.get("seasons"):
        marker["seasons"] = True  # a refresh of the same set keeps it
    write_marker(root, marker)
    return name


def _check_roughness(value: float) -> float:
    value = round(float(value), 2)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"Terrain reflectivity must be between 0 and 1, not {value}")
    return value


def _streamed_ortho(ao_scenery_dir: str) -> Tuple[str, dict]:
    for name, marker in stream_packages(ao_scenery_dir).items():
        if marker.get("kind", "ortho") == "ortho":
            return name, marker
    raise ValueError("Scenery streaming is off")


def set_stream_overlays(ao_scenery_dir: str, enabled: bool,
                        session: Optional[requests.Session] = None) -> Optional[str]:
    """Add or remove the overlay package of the scenery set being streamed.

    Removing it deletes the overlay tiles stored so far. Returns the folder
    name added or removed.
    """
    _, ortho = _streamed_ortho(ao_scenery_dir)
    source = ortho.get("source", "")
    name = OVERLAY_PACKAGE.format(source=source)
    if not enabled:
        return name if _remove_package(ao_scenery_dir, name) else None
    limit = _overlay_limit(ortho.get("disk_limit_gb") or 20)
    return _create_package(ao_scenery_dir, source, "overlay", limit, session=session)


def change_stream_source(ao_scenery_dir: str, source: str,
                         session: Optional[requests.Session] = None) -> List[str]:
    """Stream another scenery set, keeping the overlay choice and disk limit.

    The new set is set up first; the old set's packages and tiles are removed
    only once that worked, so a failed download leaves streaming as it was.
    """
    old_name, ortho = _streamed_ortho(ao_scenery_dir)
    if ortho.get("source") == source:
        return []
    overlays = any(m.get("kind") == "overlay" for m in stream_packages(ao_scenery_dir).values())
    created = enable_streaming(ao_scenery_dir, source, overlays=overlays,
                               disk_limit_gb=ortho.get("disk_limit_gb") or 20,
                               roughness=ortho.get("roughness"), session=session,
                               fetch_missing=ortho.get("fetch_missing", True))
    # Tiles downloaded on the map (and regions moved in) aren't the old
    # set's to delete: they go into the new set's folder first.
    carry_kept_tiles(ao_scenery_dir, old_name, ORTHO_PACKAGE.format(source=source))
    disable_streaming(ao_scenery_dir, keep_source=source)
    return created


def _carry_seasons_record(old_root: str, new_root: str, cells) -> None:
    """Keep which moved tiles have seasons (``stream_seasons``; moving a
    file keeps its size and time, which the record checks)."""
    name = "seasons_tiles.json"
    try:
        with open(os.path.join(old_root, name), "r", encoding="utf-8") as fh:
            old = json.load(fh).get("tiles", {})
    except (OSError, ValueError, AttributeError):
        return
    keys = {os.path.basename(dsf_rel(*c))[:-4] for c in cells}
    moved = {k: v for k, v in old.items() if k in keys}
    if not moved:
        return
    try:
        with open(os.path.join(new_root, name), "r", encoding="utf-8") as fh:
            tiles = json.load(fh).get("tiles", {})
    except (OSError, ValueError, AttributeError):
        tiles = {}
    tiles.update(moved)
    with open(os.path.join(new_root, name), "w", encoding="utf-8") as fh:
        json.dump({"tiles": tiles}, fh)


def carry_kept_tiles(ao_scenery_dir: str, old_name: str, new_name: str,
                     workers: int = 4) -> int:
    """Move the kept (pinned) tiles of one ortho package into another, of
    another scenery set. Returns the number of tiles moved.

    Each DSF's terrain list names the terrain files and masks it uses. The
    terrain files are written into the new package as real files (with the
    old set's settings, or the real file the old package had), because the
    new set would generate its own; masks are hard-linked (copied where
    links don't work). The DSF moves last, so an interrupted run leaves no
    DSF without its files. Masks and terrain files the new index doesn't
    have are never evicted.
    """
    old_root = os.path.join(ao_scenery_dir, old_name)
    new_root = os.path.join(ao_scenery_dir, new_name)
    pins = read_pins(old_root)
    old_marker, new_marker = read_marker(old_root), read_marker(new_root)
    if not pins or old_marker is None or new_marker is None:
        return 0
    old = StreamPackage(old_root, old_marker, remote=RemoteAssets())
    try:
        def terrain_of(cell):
            path = old.local_path(dsf_rel(*cell))
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                return cell, None
            if data.startswith(SEVENZIP_MAGIC):
                data = unpack_7z_dsf(path) or b""
            return cell, dsf_terrain_names(data)

        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="carry-tiles") as pool:
            found = list(pool.map(terrain_of, sorted(pins)))
        # Every texture a kept tile uses must be on disk before anything
        # moves: the new set's index may not have it (XP11 water edges use
        # water_transition.png, which the XP12 set doesn't publish). A
        # failed download stops the switch with nothing moved.
        for _cell, names in found:
            for tex in old.mask_rels(names or []):
                if not os.path.exists(old.local_path(tex)) and old.entry(tex) is not None:
                    old.fetch_now(tex)
        carried = []
        for cell, names in found:
            if names is None:
                continue  # kept but not stored
            for name in names:
                if not name.startswith("terrain/"):
                    continue
                dst = os.path.join(new_root, *name.split("/"))
                data = old.ter_bytes(name.split("/", 1)[1])
                if data is not None and not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as fh:
                        fh.write(data)
            for mask in old.mask_rels(names):
                src, dst = old.local_path(mask), os.path.join(new_root, *mask.split("/"))
                if os.path.exists(src) and not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    try:
                        os.link(src, dst)
                    except OSError:
                        import shutil
                        shutil.copy2(src, dst)
            rel = dsf_rel(*cell)
            dst = os.path.join(new_root, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                os.remove(dst)  # the kept tile wins over one fetched while flying
            os.replace(old.local_path(rel), dst)
            carried.append(cell)
        write_pins(new_root, read_pins(new_root) | set(carried))
        _carry_seasons_record(old_root, new_root, carried)
        log.info("Tile stream: moved %d kept tiles from %s to %s",
                 len(carried), old_name, new_name)
        return len(carried)
    finally:
        old.shutdown()


def set_stream_roughness(ao_scenery_dir: str, roughness: Optional[float]) -> str:
    """Set the terrain reflectivity (SUPER_ROUGHNESS) of streamed tiles, or
    clear it with None. Their .ter files are generated on demand, so this
    applies to every streamed tile from the next mount on. Returns the
    package name."""
    name, ortho = _streamed_ortho(ao_scenery_dir)
    marker = dict(ortho)
    if roughness is None:
        marker.pop("roughness", None)
    else:
        marker["roughness"] = _check_roughness(roughness)
    write_marker(os.path.join(ao_scenery_dir, name), marker)
    return name


def set_stream_seasons(ao_scenery_dir: str, enabled: bool) -> str:
    """Whether tiles downloaded on the map get X-Plane 12's season rasters
    (XP11 set; see :mod:`stream_seasons`). Returns the package name."""
    name, ortho = _streamed_ortho(ao_scenery_dir)
    marker = dict(ortho)
    if enabled:
        marker["seasons"] = True
    else:
        marker.pop("seasons", None)
    write_marker(os.path.join(ao_scenery_dir, name), marker)
    return name


def set_fetch_missing(ao_scenery_dir: str, enabled: bool) -> None:
    """Whether tiles not stored yet are offered to X-Plane and downloaded
    while flying (on), or only stored tiles are used and the rest come from
    Global Scenery (off). Applies from the next mount."""
    for name, marker in stream_packages(ao_scenery_dir).items():
        marker = dict(marker)
        if enabled:
            marker.pop("fetch_missing", None)
        else:
            marker["fetch_missing"] = False
        write_marker(os.path.join(ao_scenery_dir, name), marker)


def update_stream_settings(ao_scenery_dir: str, disk_limit_gb: float) -> None:
    """Apply a new disk limit to existing streaming packages."""
    for name, marker in stream_packages(ao_scenery_dir).items():
        limit = float(disk_limit_gb)
        if marker.get("kind") == "overlay":
            limit = _overlay_limit(limit)
        write_marker(os.path.join(ao_scenery_dir, name), dict(marker, disk_limit_gb=limit))


def _remove_package(ao_scenery_dir: str, name: str) -> bool:
    import shutil
    root = os.path.join(ao_scenery_dir, name)
    if not os.path.isfile(os.path.join(root, MARKER_NAME)):
        return False  # never delete a folder this module didn't create
    shutil.rmtree(root, ignore_errors=True)
    return True


def disable_streaming(ao_scenery_dir: str, source: Optional[str] = None,
                      keep_source: Optional[str] = None) -> List[str]:
    """Delete streaming packages and their tiles: all, one source's, or all
    but ``keep_source``'s."""
    removed = []
    for name, marker in stream_packages(ao_scenery_dir).items():
        if source is not None and marker.get("source") != source:
            continue
        if keep_source is not None and marker.get("source") == keep_source:
            continue
        if _remove_package(ao_scenery_dir, name):
            removed.append(name)
    return removed


def stream_summary(ao_scenery_dir: str) -> dict:
    """What the Scenery Library shows: whether streaming is on, its settings,
    and the tiles stored per package kind."""
    packages = stream_packages(ao_scenery_dir)
    summary = {"enabled": False, "source": None, "overlays": False,
               "disk_limit_gb": None, "roughness": None, "fetch_missing": True,
               "seasons": False,
               "ortho": (0, 0), "overlay": (0, 0)}
    for name, marker in packages.items():
        kind = marker.get("kind", "ortho")
        if kind not in ("ortho", "overlay"):
            continue
        summary[kind] = _package_usage(os.path.join(ao_scenery_dir, name))
        if kind == "overlay":
            summary["overlays"] = True
        else:
            summary.update(enabled=True, source=marker.get("source"),
                           disk_limit_gb=marker.get("disk_limit_gb"),
                           roughness=marker.get("roughness"),
                           fetch_missing=bool(marker.get("fetch_missing", True)),
                           seasons=bool(marker.get("seasons")))
    return summary


def stream_usage(ao_scenery_dir: str) -> Tuple[int, int]:
    """(bytes, DSF count) stored by all streaming packages."""
    total = count = 0
    for name in stream_packages(ao_scenery_dir):
        size, dsfs = _package_usage(os.path.join(ao_scenery_dir, name))
        total += size
        count += dsfs
    return total, count


def _package_usage(root: str) -> Tuple[int, int]:
    """(bytes, DSF count) of the tiles stored in one streaming package."""
    total = count = 0
    dirs = []
    try:
        dirs = [e.path for e in os.scandir(os.path.join(root, DSF_DIR)) if e.is_dir()]
    except OSError:
        pass
    for d in dirs + [os.path.join(root, "textures")]:
        try:
            for f in os.scandir(d):
                if f.is_file() and f.name.endswith((".dsf", ".png")):
                    total += f.stat().st_size
                    count += f.name.endswith(".dsf")
        except OSError:
            continue
    return total, count


def ensure_pack_order(custom_scenery_dir: str, ortho_name: str,
                      overlay_name: Optional[str]) -> bool:
    """Keep streamed overlays directly above AutoOrtho orthos in scenery_packs.ini.

    A missing ortho entry is added at the bottom (lowest priority); the
    overlay goes right below the classic yAutoOrtho_Overlays entry if there
    is one, else right above the first AutoOrtho ortho entry. Returns True if
    the file changed. The original is kept once as scenery_packs.ini.ao_backup.
    """
    ini = os.path.join(custom_scenery_dir, "scenery_packs.ini")
    if not os.path.isfile(ini):
        return False
    with open(ini, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    original = list(lines)

    def entry(name):
        return f"SCENERY_PACK Custom Scenery/{name}/"

    def find(name):
        for i, line in enumerate(lines):
            if line.rstrip("/").endswith(f"Custom Scenery/{name}"):
                return i
        return None

    if find(ortho_name) is None:
        lines.append(entry(ortho_name))
    if overlay_name:
        idx = find(overlay_name)
        line = lines.pop(idx) if idx is not None else entry(overlay_name)
        legacy = find(LEGACY_OVERLAY_FOLDER)
        anchors = [i for i, text in enumerate(lines)
                   if re.search(r"Custom Scenery/z_ao_[^/]*/?$", text)]
        if legacy is not None:
            lines.insert(legacy + 1, line)
        elif anchors:
            lines.insert(anchors[0], line)
        else:
            lines.append(line)
    if lines == original:
        return False
    backup = ini + ".ao_backup"
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write("\n".join(original) + "\n")
    with open(ini + ".part", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(ini + ".part", ini)
    return True


def ensure_all_pack_orders(ao_scenery_dir: str, custom_scenery_dir: str) -> bool:
    """Run ensure_pack_order for every source that has streaming packages."""
    changed = False
    packages = stream_packages(ao_scenery_dir)
    for source in {m.get("source") for m in packages.values()}:
        ortho = ORTHO_PACKAGE.format(source=source)
        overlay = OVERLAY_PACKAGE.format(source=source)
        if ortho in packages:
            changed |= ensure_pack_order(custom_scenery_dir, ortho,
                                         overlay if overlay in packages else None)
    return changed

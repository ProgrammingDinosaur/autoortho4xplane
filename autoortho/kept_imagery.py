"""Imagery downloaded ahead for kept tiles, and keeping it from cleanup.

When tiles are downloaded on the map, their imagery can be downloaded too
so they fly offline without blurry textures. The chunk set is exact: the
stored DSF's terrain list names every texture as (row, col, maptype, zoom),
each 16x16 web-mercator chunks at that zoom; at another zoom the same area
is covered by the matching chunks there. Chunks are written to the cache
folder with the imagery pipeline's names (``<col>_<row>_<zoom>_<maptype>.jpg``),
so AutoOrtho finds them like any chunk it downloaded itself.

The provider is the one AutoOrtho would use for that texture: the
maptype override, the custom map's per-tile choice, or the scenery's own
(see ``TileCacher._resolve_maptype`` in getortho). Apple Maps needs a
session token from the imagery pipeline and isn't downloaded ahead.

Cleanup must leave kept imagery alone. ``kept_imagery.json`` in the cache
folder records the highest zoom kept per tile; the mount workers read it at
start (downloading is locked while AutoOrtho runs) and the JPEG budget, the
JPEG cleanup after a texture is built, and the DDS cache's LRU eviction all
skip files of kept tiles (:func:`protector`, :func:`dds_protector`).

No Qt and no getortho import here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

if __package__:
    from . import provider_urls, tile_stream
else:  # running from inside the autoortho folder
    import provider_urls
    import tile_stream

log = logging.getLogger(__name__)

Cell = Tuple[int, int]
Chunk = Tuple[int, int, int, str]           # col, row, zoom, maptype
KEPT_FILE = "kept_imagery.json"
TEXTURE_CHUNKS = 16                         # a texture is 16x16 chunks
CHUNK_RE = re.compile(r"^(\d+)_(\d+)_(\d+)_([A-Za-z0-9]+)\.jpg$")
DDS_TILE_RE = re.compile(r"^[+-]\d{2}[+-]\d{3}$")
UNSUPPORTED = ("APPLE",)
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Which tiles keep imagery
# ---------------------------------------------------------------------------

def _key(cell: Cell) -> str:
    return f"{cell[0]:+03d}{cell[1]:+04d}"


def _cell(key: str) -> Optional[Cell]:
    if not DDS_TILE_RE.match(key):
        return None
    return int(key[:3]), int(key[3:])


def read_kept(cache_dir: str) -> Dict[Cell, int]:
    """Tile -> highest zoom whose imagery is kept."""
    try:
        with open(os.path.join(cache_dir, KEPT_FILE), "r", encoding="utf-8") as fh:
            tiles = json.load(fh).get("tiles", {})
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for key, zoom in tiles.items():
        cell = _cell(key)
        if cell is not None and isinstance(zoom, int):
            out[cell] = zoom
    return out


def set_kept(cache_dir: str, cells: Iterable[Cell], zoom: Optional[int]) -> None:
    """Keep imagery up to ``zoom`` for ``cells`` (a higher zoom already
    kept stays), or stop keeping it with None."""
    with _lock:
        kept = read_kept(cache_dir)
        for cell in cells:
            if zoom is None:
                kept.pop(cell, None)
            else:
                kept[cell] = max(zoom, kept.get(cell, 0))
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, KEPT_FILE)
        tmp = f"{path}.{uuid.uuid4().hex}.part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tiles": {_key(c): z for c, z in sorted(kept.items())}}, fh)
        os.replace(tmp, path)


def chunk_cell(col: int, row: int, zoom: int) -> Cell:
    """The 1x1 degree tile holding a chunk's centre (as the Cache view)."""
    import math
    n = 1 << zoom
    lon = (col + 0.5) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (row + 0.5) / n))))
    return math.floor(lat), math.floor(lon)


def protector(cache_dir: str, base: Optional[Callable[[str], bool]] = None
              ) -> Optional[Callable[[str], bool]]:
    """A ``protect_jpeg`` predicate: True for chunk files of kept tiles (up
    to their kept zoom) or that ``base`` protects. None if nothing is."""
    kept = read_kept(cache_dir) if cache_dir else {}
    if not kept:
        return base
    lats: Dict[Tuple[int, int], int] = {}
    lons: Dict[Tuple[int, int], int] = {}

    def protect(name: str) -> bool:
        if base is not None and base(name):
            return True
        m = CHUNK_RE.match(os.path.basename(name))
        if not m:
            return False
        col, row, zoom = int(m[1]), int(m[2]), int(m[3])
        # Latitude depends on the row only, longitude on the column only.
        lat = lats.get((row, zoom))
        if lat is None:
            lat = lats[(row, zoom)] = chunk_cell(0, row, zoom)[0]
        lon = lons.get((col, zoom))
        if lon is None:
            lon = lons[(col, zoom)] = chunk_cell(col, 0, zoom)[1]
        limit = kept.get((lat, lon))
        return limit is not None and zoom <= limit

    return protect


def dds_protector(cache_dir: str) -> Optional[Callable[[str], bool]]:
    """True for built textures (``dds_cache/<band>/<tile>/...``) of kept
    tiles. None if no tile keeps imagery."""
    kept = read_kept(cache_dir) if cache_dir else {}
    if not kept:
        return None

    def protect(path: str) -> bool:
        # dds_cache/<10 degree band>/<1 degree tile>/...: the band folder
        # has the same form as the tile's, so take the one after it.
        parts = re.split(r"[\\/]", path or "")
        if "dds_cache" in parts:
            i = len(parts) - 1 - parts[::-1].index("dds_cache")
            cell = _cell(parts[i + 2]) if i + 2 < len(parts) else None
        else:
            cells = [c for c in map(_cell, parts) if c is not None]
            cell = cells[-1] if cells else None
        return cell is not None and cell in kept

    return protect


# ---------------------------------------------------------------------------
# Which chunks
# ---------------------------------------------------------------------------

def texture_chunks(row: int, col: int, tex_zoom: int, zoom: int) -> Iterable[Tuple[int, int]]:
    """(col, row) of the chunks at ``zoom`` covering one texture."""
    if zoom >= tex_zoom:
        scale = 1 << (zoom - tex_zoom)
        cols = range(col * scale, (col + TEXTURE_CHUNKS) * scale)
        rows = range(row * scale, (row + TEXTURE_CHUNKS) * scale)
    else:
        shift = tex_zoom - zoom
        cols = range(col >> shift, -(-(col + TEXTURE_CHUNKS) >> shift))
        rows = range(row >> shift, -(-(row + TEXTURE_CHUNKS) >> shift))
    for r in rows:
        for c in cols:
            yield c, r


def maptype_resolver(override: str = "", custom_map=None) -> Callable[[int, int, str, int], str]:
    """Same choice as ``TileCacher._resolve_maptype``: the provider for a
    texture (row, col, scenery maptype, zoom)."""
    override = str(override or "")

    def resolve(row: int, col: int, maptype: str, zoom: int) -> str:
        if not override or override == "Use tile default":
            return maptype
        if override == "Custom Map":
            if custom_map is not None:
                lat, lon = _texture_centre(row, col, zoom)
                picked = custom_map.get_maptype(lat, lon)
                if picked:
                    return picked
            return maptype if maptype != "Custom Map" else "BI"
        return override

    return resolve


def _texture_centre(row: int, col: int, zoom: int) -> Tuple[float, float]:
    import math
    n = 1 << zoom
    lon = (col + TEXTURE_CHUNKS / 2) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (row + TEXTURE_CHUNKS / 2) / n))))
    return lat, lon


def tile_textures(pkg_root: str, cell: Cell) -> List[Tuple[int, int, str, int]]:
    """Textures a stored DSF uses (empty if the tile isn't stored)."""
    path = os.path.join(pkg_root, *tile_stream.dsf_rel(*cell).split("/"))
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    if data.startswith(tile_stream.SEVENZIP_MAGIC):
        data = tile_stream.unpack_7z_dsf(path) or b""
    return tile_stream.terrain_tiles(tile_stream.dsf_terrain_names(data) or [])


@dataclass
class Plan:
    chunks: List[Chunk] = field(default_factory=list)   # not cached yet
    cached: int = 0                                     # already in the cache
    unsupported: Set[str] = field(default_factory=set)  # providers skipped
    tiles: List[Cell] = field(default_factory=list)     # tiles with textures


def plan(pkg_root: str, cache_dir: str, cells: Iterable[Cell], zoom: int,
         resolve: Optional[Callable[[int, int, str, int], str]] = None,
         cancelled: Callable[[], bool] = lambda: False) -> Plan:
    """The chunks to download for ``cells`` at ``zoom``."""
    resolve = resolve or maptype_resolver()
    out = Plan()
    seen: Set[Chunk] = set()
    try:
        present = set(os.listdir(cache_dir))
    except OSError:
        present = set()
    for cell in cells:
        if cancelled():
            break
        textures = tile_textures(pkg_root, cell)
        if textures:
            out.tiles.append(cell)
        for row, col, maptype, tex_zoom in textures:
            provider = resolve(row, col, maptype, tex_zoom)
            if provider.upper() in UNSUPPORTED:
                out.unsupported.add(provider)
                continue
            for c, r in texture_chunks(row, col, tex_zoom, zoom):
                chunk = (c, r, zoom, provider)
                if chunk in seen:
                    continue
                seen.add(chunk)
                if f"{c}_{r}_{zoom}_{provider}.jpg" in present:
                    out.cached += 1
                else:
                    out.chunks.append(chunk)
    return out


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

class Throttled(Exception):
    """The provider refused us (403/429); stop for now."""


@dataclass
class DownloadResult:
    files: int = 0
    bytes: int = 0
    no_imagery: int = 0          # the provider has none at this zoom
    failed: int = 0
    cancelled: bool = False
    throttled: str = ""


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def _no_imagery(headers) -> bool:
    for name, value in (headers or {}).items():
        if str(name).lower() == "x-ve-tile-info":
            return str(value).strip().lower() == "no-tile"
    return False


def fetch_chunk(session, chunk: Chunk, cache_dir: str, attempts: int = 3,
                timeout=(5, 15)) -> Tuple[str, int]:
    """Download one chunk into the cache. Returns ("ok", bytes),
    ("no_imagery", 0) or ("failed", 0); raises Throttled on 403/429."""
    col, row, zoom, maptype = chunk
    for attempt in range(attempts):
        url, headers = provider_urls.tile_url(maptype, col, row, zoom, server_num=attempt)
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
        except Exception as exc:  # dropped connection, timeout
            log.debug("Imagery: %s failed (%s)", url, exc)
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
            continue
        try:
            if resp.status_code in (403, 429):
                raise Throttled(f"{maptype} answered {resp.status_code}")
            if resp.status_code == 404:
                return "no_imagery", 0
            if resp.status_code != 200:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
                continue
            if _no_imagery(resp.headers):
                return "no_imagery", 0
            data = resp.content
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()
        if not _is_jpeg(data):
            continue
        path = os.path.join(cache_dir, f"{col}_{row}_{zoom}_{maptype}.jpg")
        tmp = os.path.join(cache_dir, f".{col}_{row}_{zoom}_{maptype}.jpg.{uuid.uuid4().hex}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return "ok", len(data)
    return "failed", 0


def download(chunks: List[Chunk], cache_dir: str, workers: int = 8,
             progress: Optional[Callable[[int, int, int], None]] = None,
             cancelled: Callable[[], bool] = lambda: False,
             session_factory: Optional[Callable[[], object]] = None) -> DownloadResult:
    """Download ``chunks`` into ``cache_dir``. ``progress(done, total,
    bytes)`` is called about every 1%. A refusal (403/429) stops the run."""
    if session_factory is None:
        import requests

        def session_factory():
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=4,
                                                    pool_maxsize=workers)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            return session
    os.makedirs(cache_dir, exist_ok=True)
    result = DownloadResult()
    stop = threading.Event()
    local = threading.local()
    total = len(chunks)
    step = max(1, total // 100)
    done = 0

    def one(chunk):
        if stop.is_set() or cancelled():
            return None
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = session_factory()
        try:
            return fetch_chunk(session, chunk, cache_dir)
        except Throttled:
            stop.set()
            raise

    with ThreadPoolExecutor(max_workers=max(1, workers),
                            thread_name_prefix="imagery-ahead") as pool:
        futures = [pool.submit(one, c) for c in chunks]
        for future in as_completed(futures):
            try:
                got = future.result()
            except Throttled as exc:
                result.throttled = result.throttled or str(exc)
                continue
            except Exception as exc:
                log.warning("Imagery: chunk failed: %s", exc)
                got = ("failed", 0)
            if got is None:
                if not stop.is_set():
                    result.cancelled = True
                continue
            status, size = got
            if status == "ok":
                result.files += 1
                result.bytes += size
            elif status == "no_imagery":
                result.no_imagery += 1
            else:
                result.failed += 1
            done += 1
            if progress and (done % step == 0 or done == total):
                progress(done, total, result.bytes)
    log.info("Imagery: downloaded %d chunks (%.1f MB), %d without imagery, %d failed%s",
             result.files, result.bytes / 1e6, result.no_imagery, result.failed,
             f", stopped: {result.throttled}" if result.throttled else "")
    return result

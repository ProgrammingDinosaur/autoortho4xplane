"""What AutoOrtho has stored on disk, per 1x1 degree tile.

Two kinds of data are kept per tile:

- streamed base mesh: DSFs and water masks in the streaming packages under
  ``z_autoortho/scenery`` (see :mod:`tile_stream`), plus the overlay DSF when
  overlays are streamed;
- imagery: the chunk JPEGs in the cache folder, named
  ``<col>_<row>_<zoom>_<maptype>.jpg`` in web-mercator tile coordinates, and
  the built textures under ``dds_cache/<10 degree band>/<1 degree tile>/``.

:func:`scan` builds a :class:`CacheInventory` with ``os.scandir`` only (the
directory entries carry size and mtime on Windows, so ~150k chunk files take
well under a second). :func:`delete_cells`, :func:`pin_cells` and
:func:`keep_offline` change what is stored; callers must only use them while
AutoOrtho is stopped.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

if __package__:
    from . import tile_stream
else:  # running from inside the autoortho folder
    import tile_stream

log = logging.getLogger(__name__)

Cell = Tuple[int, int]

CHUNK_RE = re.compile(r"^(\d+)_(\d+)_(\d+)_([A-Za-z0-9]+)\.jpg$")
DDS_DIR = "dds_cache"
DDS_TILE_RE = re.compile(r"^[+-]\d{2}[+-]\d{3}$")
DDS_ZOOM_RE = re.compile(r"_z(\d+)\.dd[sm]$")

# Average chunk JPEG size when the cache has none at a zoom level yet
# (Bing imagery measured at ZL13-18 runs 12-18 KB).
DEFAULT_CHUNK_BYTES = 16_000


def cell_name(cell: Cell) -> str:
    return f"{cell[0]:+03d}{cell[1]:+04d}"


def band_name(cell: Cell) -> str:
    lat, lon = cell
    return f"{lat // 10 * 10:+03d}{lon // 10 * 10:+04d}"


@dataclass
class CellUsage:
    """Bytes stored for one 1x1 degree tile."""

    dsf_bytes: int = 0
    overlay_bytes: int = 0
    mask_bytes: int = 0
    mask_files: int = 0
    chunk_bytes: int = 0
    chunk_files: int = 0
    dds_bytes: int = 0
    dds_files: int = 0
    zooms: Set[int] = field(default_factory=set)
    maptypes: Set[str] = field(default_factory=set)
    mesh_used: float = 0.0      # newest mtime of the mesh files
    imagery_used: float = 0.0   # newest mtime of the imagery files
    pinned: bool = False

    @property
    def mesh_bytes(self) -> int:
        return self.dsf_bytes + self.overlay_bytes + self.mask_bytes

    @property
    def imagery_bytes(self) -> int:
        return self.chunk_bytes + self.dds_bytes

    @property
    def total(self) -> int:
        return self.mesh_bytes + self.imagery_bytes

    @property
    def has_mesh(self) -> bool:
        return self.mesh_bytes > 0

    @property
    def has_imagery(self) -> bool:
        return self.imagery_bytes > 0

    @property
    def last_used(self) -> float:
        return max(self.mesh_used, self.imagery_used)

    @property
    def state(self) -> str:
        """'both', 'mesh', 'imagery', or 'none' (pinned but not stored)."""
        if self.has_mesh and self.has_imagery:
            return "both"
        if self.has_mesh:
            return "mesh"
        if self.has_imagery:
            return "imagery"
        return "none"


@dataclass
class CacheInventory:
    cells: Dict[Cell, CellUsage] = field(default_factory=dict)
    packages: List[Tuple[str, str]] = field(default_factory=list)  # (name, kind)
    cache_dir: str = ""
    files: int = 0
    seconds: float = 0.0
    # zoom -> [chunk files, chunk bytes], for size estimates.
    chunk_zooms: Dict[int, List[int]] = field(default_factory=dict)

    def cell(self, cell: Cell) -> CellUsage:
        usage = self.cells.get(cell)
        if usage is None:
            usage = self.cells[cell] = CellUsage()
        return usage

    @property
    def streaming(self) -> bool:
        return bool(self.packages)

    @property
    def mesh_bytes(self) -> int:
        return sum(u.mesh_bytes for u in self.cells.values())

    @property
    def imagery_bytes(self) -> int:
        return sum(u.imagery_bytes for u in self.cells.values())

    @property
    def mesh_cells(self) -> int:
        return sum(1 for u in self.cells.values() if u.has_mesh)

    @property
    def imagery_cells(self) -> int:
        return sum(1 for u in self.cells.values() if u.has_imagery)

    @property
    def pinned_cells(self) -> int:
        return sum(1 for u in self.cells.values() if u.pinned)

    def chunk_average(self, zoom: int) -> int:
        files, size = self.chunk_zooms.get(zoom, (0, 0))
        if files >= 50:
            return max(1, size // files)
        files = sum(v[0] for v in self.chunk_zooms.values())
        size = sum(v[1] for v in self.chunk_zooms.values())
        return max(1, size // files) if files >= 50 else DEFAULT_CHUNK_BYTES


# ---------------------------------------------------------------------------
# Tile coordinates
# ---------------------------------------------------------------------------

def tile_lat(row: float, zoom: int) -> float:
    n = math.pi * (1.0 - 2.0 * row / (1 << zoom))
    return math.degrees(math.atan(math.sinh(n)))


def tile_lon(col: float, zoom: int) -> float:
    return col / (1 << zoom) * 360.0 - 180.0


def lat_row(lat: float, zoom: int) -> float:
    lat = max(-85.05112878, min(85.05112878, lat))
    phi = math.radians(lat)
    return (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0 * (1 << zoom)


def chunk_cell(col: int, row: int, zoom: int) -> Cell:
    """The 1x1 degree tile holding the centre of a web-mercator tile."""
    return (math.floor(tile_lat(row + 0.5, zoom)),
            math.floor(tile_lon(col + 0.5, zoom)))


class _CellMapper:
    """chunk_cell with the row and column halves memoised (rows repeat a lot)."""

    def __init__(self):
        self._lat: Dict[Tuple[int, int], int] = {}
        self._lon: Dict[Tuple[int, int], int] = {}

    def __call__(self, col: int, row: int, zoom: int) -> Cell:
        key = (row, zoom)
        lat = self._lat.get(key)
        if lat is None:
            lat = self._lat[key] = math.floor(tile_lat(row + 0.5, zoom))
        key = (col, zoom)
        lon = self._lon.get(key)
        if lon is None:
            lon = self._lon[key] = math.floor(tile_lon(col + 0.5, zoom))
        return lat, lon


def chunks_in_cell(cell: Cell, zoom: int) -> int:
    """How many web-mercator tiles at ``zoom`` cover a 1x1 degree tile."""
    lat, lon = cell
    n = 1 << zoom
    x0 = math.floor((lon + 180.0) / 360.0 * n)
    x1 = math.ceil((lon + 181.0) / 360.0 * n)
    y0 = math.floor(lat_row(lat + 1, zoom))
    y1 = math.ceil(lat_row(lat, zoom))
    return max(0, x1 - x0) * max(0, y1 - y0)


def estimate_imagery(cells: Iterable[Cell], zooms: Iterable[int],
                     inventory: Optional[CacheInventory] = None) -> Dict[int, Tuple[int, int]]:
    """zoom -> (chunks, bytes) to cache every chunk of ``cells`` at that zoom.

    Uses the average chunk size already in the cache when there is one.
    """
    cells = list(cells)
    out = {}
    for zoom in zooms:
        count = sum(chunks_in_cell(c, zoom) for c in cells)
        avg = inventory.chunk_average(zoom) if inventory else DEFAULT_CHUNK_BYTES
        out[zoom] = (count, count * avg)
    return out


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _scandir(path: str) -> List[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _package_files(root: str):
    """(DirEntry, cell) of every stored DSF and mask in a streaming package."""
    dsf_root = os.path.join(root, tile_stream.DSF_DIR)
    for folder in _scandir(dsf_root):
        if not folder.is_dir():
            continue
        for f in _scandir(folder.path):
            if f.name.endswith(".dsf"):
                cell = tile_stream.dsf_cell(f.name)
                if cell:
                    yield f, cell
    for f in _scandir(os.path.join(root, "textures")):
        if f.name.endswith(".png"):
            cell = tile_stream.mask_cell(f.name)
            if cell:
                yield f, cell


def _scan_packages(inv: CacheInventory, ao_scenery_dir: str) -> None:
    for name, marker in tile_stream.stream_packages(ao_scenery_dir).items():
        kind = marker.get("kind", "ortho")
        root = os.path.join(ao_scenery_dir, name)
        inv.packages.append((name, kind))
        for cell in tile_stream.read_pins(root):
            inv.cell(cell).pinned = True
        for f, cell in _package_files(root):
            try:
                st = f.stat()
            except OSError:
                continue
            usage = inv.cell(cell)
            inv.files += 1
            if f.name.endswith(".png"):
                usage.mask_bytes += st.st_size
                usage.mask_files += 1
            elif kind == "overlay":
                usage.overlay_bytes += st.st_size
            else:
                usage.dsf_bytes += st.st_size
            usage.mesh_used = max(usage.mesh_used, st.st_mtime)


def _scan_chunks(inv: CacheInventory, cache_dir: str,
                 cancelled: Callable[[], bool]) -> None:
    mapper = _CellMapper()
    match = CHUNK_RE.match
    for i, f in enumerate(_scandir(cache_dir)):
        if i % 5000 == 0 and cancelled():
            return
        m = match(f.name)
        if not m:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        col, row, zoom = int(m.group(1)), int(m.group(2)), int(m.group(3))
        usage = inv.cell(mapper(col, row, zoom))
        usage.chunk_bytes += st.st_size
        usage.chunk_files += 1
        usage.zooms.add(zoom)
        usage.maptypes.add(m.group(4))
        if st.st_mtime > usage.imagery_used:
            usage.imagery_used = st.st_mtime
        totals = inv.chunk_zooms.setdefault(zoom, [0, 0])
        totals[0] += 1
        totals[1] += st.st_size
        inv.files += 1


def _scan_dds(inv: CacheInventory, cache_dir: str,
              cancelled: Callable[[], bool]) -> None:
    for band in _scandir(os.path.join(cache_dir, DDS_DIR)):
        if cancelled():
            return
        if not band.is_dir():
            continue
        for tile in _scandir(band.path):
            cell = tile_stream.dsf_cell(tile.name + ".dsf")
            if cell is None or not tile.is_dir():
                continue
            usage = None
            for maptype in _scandir(tile.path):
                if not maptype.is_dir():
                    continue
                for f in _scandir(maptype.path):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if usage is None:
                        usage = inv.cell(cell)
                    usage.dds_bytes += st.st_size
                    usage.dds_files += 1
                    usage.maptypes.add(maptype.name)
                    m = DDS_ZOOM_RE.search(f.name)
                    if m:
                        usage.zooms.add(int(m.group(1)))
                    if st.st_mtime > usage.imagery_used:
                        usage.imagery_used = st.st_mtime
                    inv.files += 1


def scan(ao_scenery_dir: str, cache_dir: str,
         cancelled: Optional[Callable[[], bool]] = None) -> CacheInventory:
    """Everything stored per tile. Missing folders count as empty."""
    cancelled = cancelled or (lambda: False)
    started = time.monotonic()
    inv = CacheInventory(cache_dir=cache_dir or "")
    if ao_scenery_dir and os.path.isdir(ao_scenery_dir):
        _scan_packages(inv, ao_scenery_dir)
    if cache_dir and os.path.isdir(cache_dir):
        _scan_chunks(inv, cache_dir, cancelled)
        _scan_dds(inv, cache_dir, cancelled)
    inv.seconds = time.monotonic() - started
    log.debug("Cache inventory: %d files in %d tiles scanned in %.2fs",
              inv.files, len(inv.cells), inv.seconds)
    return inv


# ---------------------------------------------------------------------------
# Changing what is stored
# ---------------------------------------------------------------------------

@dataclass
class ChangeResult:
    bytes: int = 0
    files: int = 0
    tiles: int = 0
    errors: List[str] = field(default_factory=list)
    missing: List[Cell] = field(default_factory=list)  # not in any index
    cancelled: bool = False


def _remove(path: str, result: ChangeResult) -> None:
    try:
        size = os.stat(path).st_size
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        result.errors.append(f"{path}: {exc}")
        return
    result.bytes += size
    result.files += 1


def _delete_mesh(ao_scenery_dir: str, cells: Set[Cell], result: ChangeResult) -> None:
    for name in tile_stream.stream_packages(ao_scenery_dir):
        root = os.path.join(ao_scenery_dir, name)
        for cell in cells:
            _remove(os.path.join(root, *tile_stream.dsf_rel(*cell).split("/")), result)
        for f in _scandir(os.path.join(root, "textures")):
            if f.name.endswith(".png") and tile_stream.mask_cell(f.name) in cells:
                _remove(f.path, result)
        pins = tile_stream.read_pins(root)
        if pins & cells:
            tile_stream.write_pins(root, pins - cells)


def _delete_imagery(cache_dir: str, cells: Set[Cell], result: ChangeResult) -> None:
    mapper = _CellMapper()
    for f in _scandir(cache_dir):
        m = CHUNK_RE.match(f.name)
        if m and mapper(int(m.group(1)), int(m.group(2)), int(m.group(3))) in cells:
            _remove(f.path, result)
    dds_root = os.path.join(cache_dir, DDS_DIR)
    for cell in cells:
        band = os.path.join(dds_root, band_name(cell))
        tile = os.path.join(band, cell_name(cell))
        for maptype in _scandir(tile):
            if maptype.is_dir():
                for f in _scandir(maptype.path):
                    _remove(f.path, result)
                _rmdir(maptype.path)
        _rmdir(tile)
        _rmdir(band)


def _rmdir(path: str) -> None:
    try:
        os.rmdir(path)  # only succeeds when empty
    except OSError:
        pass


def delete_cells(ao_scenery_dir: str, cache_dir: str, cells: Iterable[Cell],
                 mesh: bool = True, imagery: bool = True) -> ChangeResult:
    """Delete the streamed mesh and/or cached imagery of ``cells``.

    Deleting a tile's mesh also stops keeping it for offline use. Only files
    AutoOrtho names itself are removed: DSFs and masks inside streaming
    packages, chunk JPEGs matching the cache naming, and ``dds_cache`` tile
    folders.
    """
    cells = set(cells)
    result = ChangeResult(tiles=len(cells))
    if mesh and ao_scenery_dir and os.path.isdir(ao_scenery_dir):
        _delete_mesh(ao_scenery_dir, cells, result)
    if imagery and cache_dir and os.path.isdir(cache_dir):
        _delete_imagery(cache_dir, cells, result)
    log.info("Cache: deleted %d files (%.1f MB) from %d tiles (mesh=%s, imagery=%s)",
             result.files, result.bytes / 1e6, len(cells), mesh, imagery)
    return result


def pin_cells(ao_scenery_dir: str, cells: Iterable[Cell], pinned: bool = True) -> int:
    """Keep (or stop keeping) tiles in every streaming package. Returns the
    number of packages changed."""
    cells = set(cells)
    changed = 0
    for name in tile_stream.stream_packages(ao_scenery_dir):
        root = os.path.join(ao_scenery_dir, name)
        pins = tile_stream.read_pins(root)
        new = pins | cells if pinned else pins - cells
        if new != pins:
            tile_stream.write_pins(root, new)
            changed += 1
    return changed


def mesh_download_estimate(ao_scenery_dir: str, cells: Iterable[Cell]) -> Tuple[int, int, int]:
    """(bytes, files, tiles covered) of streamed DSFs ``cells`` still need.

    Water masks aren't counted: they are listed inside each DSF and are
    small (a few percent of the tile).
    """
    cells = set(cells)
    size = files = 0
    covered: Set[Cell] = set()
    for name, marker in tile_stream.stream_packages(ao_scenery_dir).items():
        root = os.path.join(ao_scenery_dir, name)
        path = marker.get("index", tile_stream.INDEX_NAME)
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        try:
            index = tile_stream.TileIndex.load(path)
        except (OSError, ValueError) as exc:
            log.warning("Cache: could not read %s: %s", path, exc)
            continue
        for cell in cells:
            rel = tile_stream.dsf_rel(*cell)
            entry = index.lookup(rel)
            if entry is None:
                continue
            covered.add(cell)
            if not os.path.exists(os.path.join(root, *rel.split("/"))):
                size += entry[tile_stream._ENTRY_SIZE]
                files += 1
    return size, files, len(covered)


def _hidden_for(ao_scenery_dir: str, root: str, marker: dict,
                custom_scenery_dir: str) -> set:
    """DSFs the mount hides because an installed package already has them
    (same rule as autoortho_fuse)."""
    if marker.get("kind") == "overlay":
        return tile_stream.overlay_hidden_dsfs(custom_scenery_dir or "")
    return tile_stream.installed_dsfs(ao_scenery_dir, root)


def keep_offline(ao_scenery_dir: str, cells: Iterable[Cell],
                 custom_scenery_dir: str = "",
                 progress: Optional[Callable[[int, int, int], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None,
                 package_factory: Optional[Callable[..., "tile_stream.StreamPackage"]] = None,
                 ) -> ChangeResult:
    """Pin ``cells`` and download their streamed DSFs and water masks now.

    ``progress(done, total, bytes)`` is called after each tile of each
    package. Tiles none of the indexes have (open water, no coverage) end up
    in ``missing`` and aren't pinned. A throttled host stops the download;
    what was fetched so far stays pinned.
    """
    cells = sorted(set(cells))
    cancelled = cancelled or (lambda: False)
    factory = package_factory or tile_stream.StreamPackage
    packages = tile_stream.stream_packages(ao_scenery_dir)
    if not packages:
        raise ValueError("Scenery streaming is off")
    result = ChangeResult(tiles=len(cells))
    total = len(cells) * len(packages)
    done = 0
    covered: Set[Cell] = set()
    for name, marker in packages.items():
        root = os.path.join(ao_scenery_dir, name)
        hidden = _hidden_for(ao_scenery_dir, root, marker, custom_scenery_dir)
        pkg = factory(root, marker, hidden=hidden)
        try:
            available = [c for c in cells
                         if pkg.entry(tile_stream.dsf_rel(*c)) is not None]
            covered.update(available)
            pins = tile_stream.read_pins(root)
            if not set(available) <= pins:
                tile_stream.write_pins(root, pins | set(available))
            done += len(cells) - len(available)
            for cell in available:
                if cancelled():
                    result.cancelled = True
                    break
                try:
                    got, files = pkg.download_tile(*cell, cancelled=cancelled)
                except tile_stream.ThrottledError as exc:
                    result.errors.append(f"{cell_name(cell)}: {exc}")
                    result.cancelled = True
                    break
                except Exception as exc:
                    result.errors.append(f"{cell_name(cell)}: {exc}")
                    got = files = 0
                result.bytes += got
                result.files += files
                done += 1
                if progress:
                    progress(done, total, result.bytes)
        finally:
            pkg.shutdown()
        if result.cancelled:
            break
    result.missing = [c for c in cells if c not in covered]
    log.info("Cache: kept %d tiles for offline use, downloaded %d files (%.1f MB), "
             "%d without streamed scenery, %d errors", len(covered), result.files,
             result.bytes / 1e6, len(result.missing), len(result.errors))
    return result

"""Move installed scenery regions into the streaming package.

The old region installer put each region in ``z_autoortho/scenery/
z_ao_<id>`` with ``z_autoortho/<id>_info.json`` beside it. Every region it
installed comes from kubilus1/autoortho-scenery, the XP11 set, so a region
can move into an XP11 streaming package: its DSFs and water masks are moved
(same volume, so nothing is copied), its tiles pinned, and the region folder
removed. The map's Cache view is then the one place that shows and manages
stored tiles.

Files are moved as they are, not checked against the index:

- the Seasons patch rewrote DSFs (7z-compressed, with season rasters); they
  keep their seasons;
- regions overlap, and the index takes a shared tile from one region's zip,
  so the other region's copy differs but is just as valid;
- a few tiles of newer releases aren't in the index; the streaming mount
  lists every DSF stored in the package, so they still load.

What must match is the set: XP11 tiles need the XP11 ``.ter`` settings
(mask zoom 14), which the package generates from its index. A region whose
DSFs the index mostly doesn't know stays installed.

Order matters if the move is interrupted: tiles are pinned first (the
package never evicts them), then DSFs move, then masks (a DSF still in the
region keeps its masks until every DSF has moved), and the region folder
goes last. Running it again finishes the job.

Overlays installed in ``Custom Scenery/yAutoOrtho_Overlays`` move into the
streamed overlay package DSF by DSF (that folder is shared by all regions);
DSFs the overlay index doesn't have stay.

No Qt here; callers must only use it while AutoOrtho is stopped.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

if __package__:
    from . import tile_stream
else:  # running from inside the autoortho folder
    import tile_stream

log = logging.getLogger(__name__)

Cell = Tuple[int, int]
REGION_PREFIX = "z_ao_"
SEASON_BACKUPS = "dsf_backups"
REGION_SET = "xp11"          # what the region installer installed
MIN_KNOWN = 0.5              # share of a region's DSFs the index must have


@dataclass
class RegionCheck:
    """Whether an installed region can move into the streaming package."""
    name: str                 # folder, e.g. z_ao_eur
    region_id: str            # e.g. eur
    dsfs: int = 0
    known: int = 0            # DSFs the index has
    size: int = 0             # bytes of the folder's DSFs and masks
    tiles: List[Cell] = field(default_factory=list)
    seasons: bool = False     # the Seasons patch was applied
    reason: str = ""          # why it can't move; empty if it can

    @property
    def movable(self) -> bool:
        return not self.reason


@dataclass
class AdoptResult:
    regions: List[str] = field(default_factory=list)   # moved region ids
    kept: Dict[str, str] = field(default_factory=dict)  # region id -> reason
    tiles: int = 0
    files: int = 0
    bytes: int = 0
    overlays: int = 0          # overlay DSFs moved
    cancelled: bool = False
    errors: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = []
        if self.regions:
            parts.append(f"moved {len(self.regions)} region(s), {self.tiles} tiles")
        if self.overlays:
            parts.append(f"{self.overlays} overlay tiles")
        if self.kept:
            parts.append(f"kept {len(self.kept)} installed")
        return ", ".join(parts) or "nothing to move"


def installed_regions(ao_scenery_dir: str) -> List[str]:
    """Region folders the old installer made (no streaming marker)."""
    try:
        entries = sorted(os.scandir(ao_scenery_dir), key=lambda e: e.name)
    except OSError:
        return []
    return [e.name for e in entries
            if e.is_dir() and e.name.startswith(REGION_PREFIX)
            and not os.path.isfile(os.path.join(e.path, tile_stream.MARKER_NAME))
            and os.path.isdir(os.path.join(e.path, tile_stream.DSF_DIR))]


def _dsfs(root: str) -> List[str]:
    """Package-relative paths of the DSFs in a scenery folder."""
    out = []
    dsf_root = os.path.join(root, tile_stream.DSF_DIR)
    try:
        folders = sorted(os.listdir(dsf_root))
    except OSError:
        return out
    for folder in folders:
        try:
            names = sorted(os.listdir(os.path.join(dsf_root, folder)))
        except OSError:
            continue
        out += [f"{tile_stream.DSF_DIR}/{folder}/{n}" for n in names
                if tile_stream.DSF_NAME_RE.match(n)]
    return out


def _masks(root: str) -> List[str]:
    try:
        names = sorted(os.listdir(os.path.join(root, "textures")))
    except OSError:
        return []
    return [f"textures/{n}" for n in names if tile_stream.MASK_NAME_RE.match(n)]


def _has_season_backups(root: str) -> bool:
    for _dirpath, _dirs, files in os.walk(os.path.join(root, SEASON_BACKUPS)):
        if files:
            return True
    return False


def _info_path(ao_scenery_dir: str, region_id: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(ao_scenery_dir)),
                        f"{region_id}_info.json")


def _read_info(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def check_region(ao_scenery_dir: str, name: str, index: "tile_stream.TileIndex",
                 variant: str = "") -> RegionCheck:
    """Whether a region can move into a package with ``index`` (cheap: lists
    and stats files only)."""
    root = os.path.join(ao_scenery_dir, name)
    check = RegionCheck(name=name, region_id=name[len(REGION_PREFIX):])
    info = _read_info(_info_path(ao_scenery_dir, check.region_id))
    check.seasons = _has_season_backups(root) or bool(info.get("processed_dsf_seasons"))
    dsfs = _dsfs(root)
    check.dsfs = len(dsfs)
    for rel in dsfs:
        check.size += _size(os.path.join(root, *rel.split("/")))
        cell = tile_stream.dsf_cell(os.path.basename(rel))
        if cell:
            check.tiles.append(cell)
        if index.lookup(rel) is not None:
            check.known += 1
    for rel in _masks(root):
        check.size += _size(os.path.join(root, *rel.split("/")))
    variant = variant or index.variant
    if not dsfs:
        check.reason = "It has no DSFs."
    elif variant != REGION_SET:
        check.reason = ("It is part of the XP11 set; switch the streamed set to "
                        "XP11 to move it, or remove it to use the streamed set there.")
    elif check.known < MIN_KNOWN * check.dsfs:
        check.reason = (f"The streamed set has only {check.known} of its "
                        f"{check.dsfs} tiles.")
    return check


def _ortho_package(ao_scenery_dir: str) -> Tuple[str, dict]:
    for name, marker in tile_stream.stream_packages(ao_scenery_dir).items():
        if marker.get("kind", "ortho") == "ortho":
            return os.path.join(ao_scenery_dir, name), marker
    raise ValueError("Scenery streaming is off")


def _load_index(root: str, marker: dict) -> "tile_stream.TileIndex":
    path = marker.get("index", tile_stream.INDEX_NAME)
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    return tile_stream.TileIndex.load(path)


def _variant(marker: dict, index: "tile_stream.TileIndex") -> str:
    return marker.get("source") or index.variant


def check_regions(ao_scenery_dir: str) -> List[RegionCheck]:
    """Check every installed region against the streamed set."""
    names = installed_regions(ao_scenery_dir)
    if not names:
        return []
    root, marker = _ortho_package(ao_scenery_dir)
    index = _load_index(root, marker)
    return [check_region(ao_scenery_dir, n, index, _variant(marker, index))
            for n in names]


def _move(src: str, dst: str) -> bool:
    """Move a file; if the destination already has it, drop the source.
    Returns True if the destination got a new file."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.remove(src)
        return False
    os.replace(src, dst)
    return True


def adopt_regions(ao_scenery_dir: str, region_names: Optional[List[str]] = None,
                  custom_scenery_dir: str = "",
                  progress: Optional[Callable[[str, int, int], None]] = None,
                  cancelled: Callable[[], bool] = lambda: False) -> AdoptResult:
    """Move installed regions (all, or the folders in ``region_names``) into
    the streaming package, pin their tiles and remove them. Then move
    installed overlays the streamed overlay index has.

    ``progress(region_id, done, total)`` is called as files move.
    """
    result = AdoptResult()
    pkg_root, marker = _ortho_package(ao_scenery_dir)
    index = _load_index(pkg_root, marker)
    variant = _variant(marker, index)
    names = installed_regions(ao_scenery_dir)
    if region_names is not None:
        wanted = set(region_names)
        names = [n for n in names if n in wanted]
    for name in names:
        if cancelled():
            result.cancelled = True
            break
        check = check_region(ao_scenery_dir, name, index, variant)
        if not check.movable:
            result.kept[check.region_id] = check.reason
            log.info("Adopt: keeping %s installed: %s", name, check.reason)
            continue
        try:
            moved = _adopt_checked(ao_scenery_dir, check, pkg_root, marker,
                                   progress, cancelled)
        except OSError as exc:
            result.errors.append(f"{check.region_id}: {exc}")
            log.error("Adopt: moving %s failed: %s", name, exc)
            continue
        if moved is None:
            result.cancelled = True
            break
        result.regions.append(check.region_id)
        result.tiles += len(check.tiles)
        result.files += moved[0]
        result.bytes += moved[1]
    if not result.cancelled and custom_scenery_dir:
        try:
            result.overlays = adopt_overlays(ao_scenery_dir, custom_scenery_dir,
                                             progress, cancelled)
        except (OSError, ValueError) as exc:
            result.errors.append(f"overlays: {exc}")
    log.info("Adopt: %s", result)
    return result


def _adopt_checked(ao_scenery_dir: str, check: RegionCheck, pkg_root: str,
                   marker: dict, progress, cancelled) -> Optional[Tuple[int, int]]:
    """Move a checked region. Returns (files moved, bytes), or None if
    cancelled midway (what moved stays moved and pinned)."""
    root = os.path.join(ao_scenery_dir, check.name)
    pins = tile_stream.read_pins(pkg_root)
    if not set(check.tiles) <= pins:
        tile_stream.write_pins(pkg_root, pins | set(check.tiles))
    rels = _dsfs(root) + _masks(root)   # DSFs first: see the module docstring
    files = size = 0
    for i, rel in enumerate(rels, 1):
        if cancelled():
            return None
        src = os.path.join(root, *rel.split("/"))
        n = _size(src)
        if _move(src, os.path.join(pkg_root, *rel.split("/"))):
            files += 1
            size += n
        if progress and (i % 50 == 0 or i == len(rels)):
            progress(check.region_id, i, len(rels))
    info_path = _info_path(ao_scenery_dir, check.region_id)
    roughness = _region_roughness(_read_info(info_path))
    if roughness is not None and marker.get("roughness") is None:
        tile_stream.set_stream_roughness(ao_scenery_dir, roughness)
        marker["roughness"] = roughness
    shutil.rmtree(root)
    if os.path.exists(info_path):
        os.remove(info_path)
    log.info("Adopt: moved %s into %s (%d tiles, %d files, %.1f GB)", check.name,
             os.path.basename(pkg_root), len(check.tiles), files, size / 1e9)
    return files, size


def _region_roughness(info: dict) -> Optional[float]:
    """The terrain reflectivity the region's patch applied, if any."""
    value = info.get("roughness_value")
    if value is None or not info.get("processed_ter_roughness"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def adopt_overlays(ao_scenery_dir: str, custom_scenery_dir: str,
                   progress=None, cancelled: Callable[[], bool] = lambda: False) -> int:
    """Move installed overlay DSFs the streamed overlay index has into the
    overlay package and pin them. Returns the number moved."""
    target = None
    for name, marker in tile_stream.stream_packages(ao_scenery_dir).items():
        if marker.get("kind") == "overlay":
            target = (os.path.join(ao_scenery_dir, name), marker)
    src_root = os.path.join(custom_scenery_dir, tile_stream.LEGACY_OVERLAY_FOLDER)
    if target is None or not os.path.isdir(src_root):
        return 0
    pkg_root, marker = target
    index = _load_index(pkg_root, marker)
    if _variant(marker, index) != REGION_SET:
        return 0
    known = [rel for rel in _dsfs(src_root) if index.lookup(rel) is not None]
    tiles = {tile_stream.dsf_cell(os.path.basename(r)) for r in known} - {None}
    pins = tile_stream.read_pins(pkg_root)
    if not tiles <= pins:
        tile_stream.write_pins(pkg_root, pins | tiles)
    moved = 0
    for i, rel in enumerate(known, 1):
        if cancelled():
            break
        _move(os.path.join(src_root, *rel.split("/")),
              os.path.join(pkg_root, *rel.split("/")))
        moved += 1
        if progress and (i % 50 == 0 or i == len(known)):
            progress("overlays", i, len(known))
    return moved

"""Move installed scenery regions into the streaming package.

The old region installer put each region in ``z_autoortho/scenery/
z_ao_<id>`` with ``z_autoortho/<id>_info.json`` beside it. Every region it
installed comes from kubilus1/autoortho-scenery, the XP11 set. A region
moves into the streaming package: its DSFs and water masks go into the
package folder (same volume, so nothing is copied), its tiles are pinned,
and the region folder is removed. The map's Scenery & Cache view is then the
one place that shows and manages stored tiles. AutoOrtho does this by itself
for every installed region when it starts (``migrate``).

Files are moved as they are, not checked against the index:

- the Seasons patch rewrote DSFs (7z-compressed, with season rasters); they
  keep their seasons;
- regions overlap, and the index takes a shared tile from one region's zip,
  so the other region's copy differs but is just as valid;
- a few tiles of newer releases aren't in the index; the streaming mount
  lists every DSF stored in the package, so they still load.

Any set works. XP11 tiles need XP11 terrain files (mask zoom 14): in an
XP11 package they are generated from the index, and in a package of another
set (XP12) the region's own ``terrain/*.ter`` files go along, since a real
terrain file in the package wins over a generated one
(``StreamPackage.ter_bytes``). Moved files the package's index doesn't have
are never evicted or deleted with a tile's masks, because they can't be
downloaded again. A region whose DSFs the index mostly doesn't know (not
the same area) stays installed.

Nothing may be missing if the move is interrupted: tiles are pinned first
(never evicted), masks and terrain files are hard-linked into the package
(instant on the same NTFS volume; copied where links don't work), then the
DSFs move, and the region folder goes last. Until its last DSF has moved
the region still has all its files, and every moved DSF finds its files in
the package. Running it again finishes the job.

A region stays installed, with the reason, when it is turned off in
``scenery_packs.ini`` (moving would turn its tiles back on), or when it is on
another drive (a junction or symlink, as the docs once suggested) and the
scenery drive hasn't room for a copy. With room it is copied over and the
link removed; the link's target is never deleted.

Overlays installed in ``Custom Scenery/yAutoOrtho_Overlays`` are left where
they are: that folder works with any scenery set, and an XP12 set has no
overlay package that could hold them.

No Qt here; callers must only use it while AutoOrtho is stopped.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
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
PROGRESS_INTERVAL = 0.25     # seconds between progress reports while moving


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

    @property
    def leftover(self) -> bool:
        """No DSFs left: an earlier move took them but didn't remove the
        folder. Moving it only removes what is left."""
        return self.dsfs == 0


MIGRATION_SET = "xp11"   # the set a migrated install starts with


@dataclass
class AdoptResult:
    regions: List[str] = field(default_factory=list)   # moved region ids
    cleaned: List[str] = field(default_factory=list)   # leftover folders removed
    kept: Dict[str, str] = field(default_factory=dict)  # region id -> reason
    tiles: int = 0
    files: int = 0
    bytes: int = 0
    overlays: int = 0          # overlay DSFs moved
    cancelled: bool = False
    errors: List[str] = field(default_factory=list)
    created: List[str] = field(default_factory=list)   # packages set up first
    seasons: bool = False      # a moved region had the Seasons patch

    def __str__(self) -> str:
        parts = []
        if self.regions:
            parts.append(f"moved {len(self.regions)} region(s), {self.tiles} tiles")
        if self.cleaned:
            parts.append(f"removed {len(self.cleaned)} emptied region folder(s)")
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
    """Every texture of a region: water masks and the shared ones such as
    ``water_transition.png``, which XP11 water edges use and another set's
    index may not have."""
    try:
        names = sorted(os.listdir(os.path.join(root, "textures")))
    except OSError:
        return []
    return [f"textures/{n}" for n in names if n.lower().endswith(".png")]


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


SPACE_MARGIN = 2 * 1024 ** 3   # left free on the scenery drive after a copy


def disabled_packs(custom_scenery_dir: str) -> set:
    """Folder names turned off in X-Plane's scenery_packs.ini."""
    out = set()
    try:
        with open(os.path.join(custom_scenery_dir, "scenery_packs.ini"), "r",
                  encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("SCENERY_PACK_DISABLED "):
                    path = line.split(None, 1)[1].strip().replace("\\", "/")
                    out.add(path.rstrip("/").rsplit("/", 1)[-1])
    except OSError:
        pass
    return out


def _same_drive(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def _is_link(path: str) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _p: False)
    return os.path.islink(path) or isjunction(path)


def check_region(ao_scenery_dir: str, name: str, index: "tile_stream.TileIndex",
                 variant: str = "", pkg_root: str = "",
                 disabled: Optional[set] = None) -> RegionCheck:
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
    if not dsfs:
        # A leftover: nothing for X-Plane to load, and the move that took its
        # DSFs had put its terrain and textures in the package first. Moving
        # it finishes that job and removes what is left.
        return check
    if disabled and name in disabled:
        check.reason = ("It's turned off in X-Plane's scenery list. Turn it on "
                        "to move it, or remove it.")
    elif check.known < MIN_KNOWN * check.dsfs:
        check.reason = (f"The scenery set has only {check.known} of its "
                        f"{check.dsfs} tiles.")
    elif pkg_root and not _same_drive(root, pkg_root):
        free = shutil.disk_usage(pkg_root).free
        if free < check.size + SPACE_MARGIN:
            check.reason = (f"It's on another drive, and the scenery drive has "
                            f"{free / 1024 ** 3:.0f} GB free for its "
                            f"{check.size / 1024 ** 3:.0f} GB.")
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


def check_regions(ao_scenery_dir: str, custom_scenery_dir: str = "") -> List[RegionCheck]:
    """Check every installed region against the streamed set."""
    names = installed_regions(ao_scenery_dir)
    if not names:
        return []
    root, marker = _ortho_package(ao_scenery_dir)
    index = _load_index(root, marker)
    disabled = disabled_packs(custom_scenery_dir) if custom_scenery_dir else set()
    return [check_region(ao_scenery_dir, n, index, _variant(marker, index),
                         pkg_root=root, disabled=disabled)
            for n in names]


def _terrain(root: str) -> List[str]:
    try:
        names = sorted(os.listdir(os.path.join(root, "terrain")))
    except OSError:
        return []
    return [f"terrain/{n}" for n in names if n.endswith(".ter")]


def _link(src: str, dst: str) -> bool:
    """Put a second name on a file (or copy it where links don't work),
    unless the destination already has one. Returns True if it was added."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return False
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return True


def _move(src: str, dst: str) -> bool:
    """Move a file; if the destination already has it, drop the source.
    Returns True if the destination got a new file."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.remove(src)
        return False
    try:
        os.replace(src, dst)
    except OSError:
        # Another drive: copy to a temporary name, then swap it in, so the
        # package never holds half a DSF.
        tmp = dst + ".part"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        os.remove(src)
    return True


def adopt_regions(ao_scenery_dir: str, region_names: Optional[List[str]] = None,
                  custom_scenery_dir: str = "",
                  progress: Optional[Callable[[str, int, int], None]] = None,
                  cancelled: Callable[[], bool] = lambda: False) -> AdoptResult:
    """Move installed regions (all, or the folders in ``region_names``) into
    the streaming package, pin their tiles and remove them.

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
    disabled = disabled_packs(custom_scenery_dir) if custom_scenery_dir else set()
    for name in names:
        if cancelled():
            result.cancelled = True
            break
        check = check_region(ao_scenery_dir, name, index, variant,
                             pkg_root=pkg_root, disabled=disabled)
        if not check.movable:
            result.kept[check.region_id] = check.reason
            log.info("Adopt: keeping %s installed: %s", name, check.reason)
            continue
        try:
            moved = _adopt_checked(ao_scenery_dir, check, pkg_root, marker,
                                   progress, cancelled,
                                   own_terrain=variant != REGION_SET)
        except OSError as exc:
            result.errors.append(f"{check.region_id}: {exc}")
            log.error("Adopt: moving %s failed: %s", name, exc)
            continue
        if moved is None:
            result.cancelled = True
            break
        if check.leftover:
            result.cleaned.append(check.region_id)
            continue
        result.regions.append(check.region_id)
        result.tiles += len(check.tiles)
        result.files += moved[0]
        result.bytes += moved[1]
        result.seasons = result.seasons or check.seasons
    log.info("Adopt: %s", result)
    return result


def _adopt_checked(ao_scenery_dir: str, check: RegionCheck, pkg_root: str,
                   marker: dict, progress, cancelled,
                   own_terrain: bool = False) -> Optional[Tuple[int, int]]:
    """Move a checked region (with its terrain files if ``own_terrain``).
    Returns (files moved, bytes), or None if cancelled midway: what moved
    stays moved and pinned, and the region keeps everything it still needs."""
    root = os.path.join(ao_scenery_dir, check.name)
    pins = tile_stream.read_pins(pkg_root)
    if not set(check.tiles) <= pins:
        tile_stream.write_pins(pkg_root, pins | set(check.tiles))
    linked = (_terrain(root) if own_terrain else []) + _masks(root)
    dsfs = _dsfs(root)
    total = len(linked) + len(dsfs)
    files = size = 0
    last_report = None
    for i, rel in enumerate(linked + dsfs, 1):
        if cancelled():
            return None
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(pkg_root, *rel.split("/"))
        n = _size(src)
        added = _link(src, dst) if i <= len(linked) else _move(src, dst)
        if added and not rel.startswith("terrain/"):
            files += 1
            size += n
        if progress:
            # By time, not file count: links take microseconds, and each
            # report crosses to the UI thread.
            now = time.monotonic()
            if (i == total or last_report is None
                    or now - last_report >= PROGRESS_INTERVAL):
                last_report = now
                progress(check.region_id, i, total)
    if own_terrain and check.tiles:
        # Its tiles are another set's than the package's: say so (the map
        # shows it, and deleting one removes its own terrain files too).
        sources = tile_stream.read_tile_sources(pkg_root)
        sources.update({c: REGION_SET for c in check.tiles})
        tile_stream.write_tile_sources(pkg_root, sources)
    info_path = _info_path(ao_scenery_dir, check.region_id)
    roughness = _region_roughness(_read_info(info_path))
    # With its own terrain files the region keeps its reflectivity in them.
    if not own_terrain and roughness is not None and marker.get("roughness") is None:
        tile_stream.set_stream_roughness(ao_scenery_dir, roughness)
        marker["roughness"] = roughness
    try:
        _remove_region(root)
    except OSError as exc:
        # Its tiles are in the package already, so the move counts; what is
        # left has no DSFs and the next start removes it.
        log.warning("Adopt: moved %s but couldn't remove its folder yet: %s",
                    check.name, exc)
        return files, size
    if os.path.exists(info_path):
        os.remove(info_path)
    if check.leftover:
        log.info("Adopt: removed %s (no DSFs left; an earlier move took them)",
                 check.name)
    else:
        log.info("Adopt: moved %s into %s (%d tiles, %d files, %.1f GB)",
                 check.name, os.path.basename(pkg_root), len(check.tiles),
                 files, size / 1e9)
    return files, size


def _remove_region(root: str) -> None:
    if _is_link(root):
        # A junction or symlink to another drive: remove the link only;
        # what is left at its target (terrain files) is the user's.
        try:
            os.unlink(root)
        except OSError:
            os.rmdir(root)
    else:
        shutil.rmtree(root)


def _region_roughness(info: dict) -> Optional[float]:
    """The terrain reflectivity the region's patch applied, if any."""
    value = info.get("roughness_value")
    if value is None or not info.get("processed_ter_roughness"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def needs_migration(ao_scenery_dir: str) -> bool:
    """Whether regions installed with an earlier version are still there."""
    return bool(ao_scenery_dir) and bool(installed_regions(ao_scenery_dir))


def migrate(ao_scenery_dir: str, custom_scenery_dir: str = "",
            session=None, progress=None,
            cancelled: Callable[[], bool] = lambda: False) -> AdoptResult:
    """Move every installed region into AutoOrtho scenery, as a new version
    does on its first start.

    Without a scenery set yet (an install that never streamed), the XP11 set
    is set up first: the regions are from it, and with downloading while
    flying off it behaves as before (tiles not installed come from Global
    Scenery). No overlay package is set up: installed overlays stay in
    ``yAutoOrtho_Overlays``, which keeps working. If a moved region had the
    Seasons patch, the XP11 package adds seasons to tiles downloaded later. That downloads its index (about 6 MB); if it
    fails this raises and nothing has changed, so the next start tries again.
    Regions that can't move stay installed and are listed in the Scenery
    Library.
    """
    if not needs_migration(ao_scenery_dir):
        return AdoptResult()
    created = []
    try:
        _ortho_package(ao_scenery_dir)
    except ValueError:
        created = tile_stream.enable_streaming(
            ao_scenery_dir, MIGRATION_SET, overlays=False,
            fetch_missing=False, session=session)
        log.info("Adopt: set up %s for the installed regions", ", ".join(created))
    result = adopt_regions(ao_scenery_dir, custom_scenery_dir=custom_scenery_dir,
                           progress=progress, cancelled=cancelled)
    result.created = list(created)
    if result.seasons:
        root, marker = _ortho_package(ao_scenery_dir)
        if marker.get("source") == MIGRATION_SET and not marker.get("seasons"):
            tile_stream.set_stream_seasons(ao_scenery_dir, True)
    return result

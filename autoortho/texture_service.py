"""The seam between texture demand and getortho's tile building.

FUSE mode keeps serving reads through ``Tile.read_dds_bytes``. Files mode
asks this service for the levels of a texture and writes them to disk. Both
use the same chunk download, broker, encoder and season colours, so a file
written here holds what FUSE would serve for the same tile.

This module must never import a FUSE module (checked by a test): the files
worker starts without WinFsp or macFUSE installed.
"""

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Files mode never waits on X-Plane: a build has no deadline of its own, only
# this ceiling so a stuck provider cannot hold a builder forever.
FILES_MODE_MAX_S = 120.0


def resolve_source_maptype(maptype, override=None):
    """The imagery source for a texture's maptype.

    ``ZL`` (zoom notation used by some packages) and the empty maptype are
    placeholders, not sources: they resolve to the maptype override when one
    is set, else Bing. Other maptypes are used as named. The texture's file
    name never changes.
    """
    if not maptype or str(maptype).upper() == "ZL":
        if override and override not in ("Use tile default",):
            return override
        return "BI"
    return maptype


@dataclass(frozen=True)
class TextureKey:
    row: int
    col: int
    maptype: str   # as named in the .ter (may be "ZL" or custom)
    zoom: int      # the texture's own zoom (from its name)

    @property
    def name(self) -> str:
        return f"{self.row}_{self.col}_{self.maptype}{self.zoom}.dds"


class DemandSource(str, Enum):
    FUSE_READ = "fuse_read"
    PLANNER = "planner"
    START = "start"
    PREFETCH = "prefetch"


@dataclass(frozen=True)
class Demand:
    key: TextureKey
    level: int          # mip index from the top of the file: 0 = full size
    urgency_s: float    # seconds until X-Plane reads that level (<= 0: now)
    source: DemandSource
    max_zoom: Optional[int] = None   # pins the file's size (files mode)


@dataclass(frozen=True)
class BuildPolicy:
    max_seconds: Optional[float] = None   # None: FILES_MODE_MAX_S
    stuck_piece_s: float = 10.0
    allow_parent_fallback: bool = True
    # Download priority for the whole build (getortho.PRIORITY_*); None keeps
    # getortho's own per-read priorities.
    priority: Optional[int] = None


@dataclass
class BuiltLevels:
    key: TextureKey
    max_zoom: int
    width: int
    dxt_format: str
    header: bytes
    total_size: int
    levels: dict = field(default_factory=dict)    # mip -> bytes
    offsets: dict = field(default_factory=dict)   # mip -> (offset, length)
    patchy: bool = False
    season_key: Optional[str] = None
    provider: str = ""

    @property
    def first_level(self):
        return min(self.levels) if self.levels else None


def _getortho():
    try:
        from autoortho import getortho
    except ImportError:
        import getortho
    return getortho


def target_zoom(tile_cacher, zoom, cfg=None):
    """The build zoom files mode pins for a texture of ``zoom``.

    Fixed mode follows ``max_zoom`` / ``max_zoom_near_airports``. Dynamic
    mode lowers the zoom by predicted altitude in FUSE mode; in files mode
    the planner already builds only the levels each distance needs, so the
    file takes the sharpest zoom the steps allow. Never above ``zoom + 1``
    (X-Plane's limit), and within the texture cap.
    """
    if cfg is None:
        try:
            from autoortho.aoconfig import CFG as cfg
        except ImportError:
            from aoconfig import CFG as cfg
    custom = bool(getattr(cfg.autoortho, "using_custom_tiles", False))
    mode = str(getattr(tile_cacher, "max_zoom_mode", "fixed")).lower()
    wanted = None
    if mode == "dynamic":
        try:
            steps = tile_cacher.dynamic_zoom_manager.get_steps()
            if steps:
                if int(zoom) == 18 and not custom:
                    wanted = max(int(s.zoom_level_airports) for s in steps)
                else:
                    wanted = max(int(s.zoom_level) for s in steps)
        except Exception:
            wanted = None
    if wanted is None:
        if custom:
            wanted = int(tile_cacher.target_zoom_level)
        elif int(zoom) == 18:
            wanted = int(tile_cacher.target_zoom_level_near_airports)
        else:
            wanted = int(tile_cacher.target_zoom_level)
    wanted = min(int(zoom) + 1, wanted)
    try:
        try:
            from autoortho import texture_plan
        except ImportError:
            import texture_plan
        cap = texture_plan.cap_from_config(cfg)
        if cap:
            wanted = texture_plan.capped_zoom(int(zoom), wanted, cap)
    except Exception:
        pass
    return wanted


def season_key_for(row, col, zoom):
    """Identity of the season colours baked into a texture (None: none)."""
    go = _getortho()
    try:
        if go.season_colour_table(row, col, zoom) is None:
            return None
        return f"{0.01 * go.season_saturation_locked(row, col, zoom):.3f}"
    except Exception:
        return None


class TextureService:
    """Builds the levels of a texture with today's tile machinery."""

    def __init__(self, tile_cacher, executor=None, cfg=None):
        self.tc = tile_cacher
        self.executor = executor
        self.cfg = cfg
        self._cancelled = set()
        self._lock = threading.Lock()

    def _override(self):
        cfg = self.cfg
        if cfg is None:
            try:
                from autoortho.aoconfig import CFG as cfg
            except ImportError:
                from aoconfig import CFG as cfg
        return getattr(cfg.autoortho, "maptype_override", None)

    def build(self, demand: Demand, policy: BuildPolicy = BuildPolicy()) -> BuiltLevels:
        go = _getortho()
        hint = getattr(go, "files_priority", None)
        if policy.priority is not None and hint is not None:
            with hint(policy.priority):
                return self._build(go, demand, policy)
        return self._build(go, demand, policy)

    def _build(self, go, demand, policy):
        key = demand.key
        source = resolve_source_maptype(key.maptype, self._override())
        max_zoom = demand.max_zoom
        if max_zoom is None:
            max_zoom = target_zoom(self.tc, key.zoom, self.cfg)
        budget_s = policy.max_seconds or FILES_MODE_MAX_S
        deadline = time.monotonic() + budget_s
        tile = self.tc.open_tile(key.row, key.col, source, key.zoom, max_zoom=max_zoom)
        try:
            dds = tile.dds
            first = max(0, min(int(demand.level), len(dds.mipmap_list) - 1))
            build_from = min(first, tile.max_mipmap)
            for mip in range(build_from, tile.max_mipmap + 1):
                if self._is_cancelled(key):
                    raise CancelledBuild(key.name)
                remaining = max(0.0, deadline - time.monotonic())
                # Always an explicit budget: get_mipmap would otherwise fall
                # back to FUSE's wait band.
                tile.get_mipmap(mip, time_budget=go.TimeBudget(remaining))
            levels = {}
            offsets = {}
            season = season_key_for(key.row, key.col, key.zoom)
            for mm in dds.mipmap_list:
                if mm.idx < first:
                    continue
                data = dds.read_at(mm.startpos, mm.length)
                if season is not None:
                    data = tile._recolour(data, mm.startpos)
                levels[mm.idx] = bytes(data)
                offsets[mm.idx] = (mm.startpos, mm.length)
            patchy = False
            try:
                patchy = tile.real_from() > first
            except Exception:
                patchy = False
            header = dds.header.getvalue() if dds.header is not None else bytes(dds)
            return BuiltLevels(
                key=key, max_zoom=int(tile.max_zoom), width=int(dds.width),
                dxt_format=str(dds.dxt_format), header=bytes(header[:128]),
                total_size=int(dds.total_size), levels=levels, offsets=offsets,
                patchy=patchy, season_key=season, provider=source,
            )
        finally:
            self.tc.close_tile(key.row, key.col, source, key.zoom)
            with self._lock:
                self._cancelled.discard(key)

    def submit(self, demand, policy, on_done: Callable):
        if self.executor is None:
            fut = Future()
            try:
                fut.set_result(self.build(demand, policy))
            except Exception as exc:
                fut.set_exception(exc)
        else:
            fut = self.executor.submit(self.build, demand, policy)
        fut.add_done_callback(
            lambda f: on_done(f.exception() if f.exception() else f.result())
        )
        return fut

    def cancel(self, key):
        with self._lock:
            self._cancelled.add(key)
        return True

    def _is_cancelled(self, key):
        with self._lock:
            return key in self._cancelled


class CancelledBuild(Exception):
    pass

"""Guards: compare the zoom target and memory settings with this PC.

Guards warn and suggest a fix; they never block. Each check returns a
:class:`GuardResult` with a level the UI shows as a traffic light:

* graphics memory: the target's photo textures (see :mod:`target_cost`) plus
  what X-Plane needs for itself, against the graphics card;
* RAM: the memory cache against what X-Plane and the system need;
* disk: free space on the cache drive against what flying adds;
* download: what the target needs to keep up, against the last connection
  test and the download limit (information only without either).

The limits in ``[limits]`` (see :mod:`limits`) set the graphics memory budget
and the RAM ceiling when the user set them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

if __package__:
    from autoortho import limits as limits_module
    from autoortho import target_cost as tc
else:
    import limits as limits_module
    import target_cost as tc

GB = 1024 ** 3

DEFAULT_XPLANE_VRAM_RESERVE_GB = 5.0
VRAM_WARN_SHARE = 0.9          # amber above 90 % of the card
RAM_OVERHEAD_GB = 1.0          # the app and its workers beside the cache
RAM_KEEP_FREE_GB = 6.0         # red when less than this is left for X-Plane
RAM_WARN_SHARE = 0.35          # amber when the cache takes more of RAM
DISK_BAD_FREE = 5 * GB
DISK_WARN_FREE = 10 * GB


class Level(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    BAD = "bad"

    @property
    def rank(self):
        return {"ok": 0, "info": 0, "warn": 1, "bad": 2}[self.value]


@dataclass(frozen=True)
class Fix:
    """A suggested change: config text by "section.key"."""

    label: str
    changes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GuardResult:
    resource: str      # "vram", "ram", "disk", "download"
    level: Level
    title: str         # short, for the light: "Graphics memory"
    message: str       # one sentence
    fix: Optional[Fix] = None


def _gb(value: float) -> str:
    gb = value / GB
    return f"{gb:.1f} GB" if gb < 10 else f"{gb:.0f} GB"


# ------------------------------------------------------------------- VRAM


def vram_need(cost: "tc.TargetCost", reserve_gb: float) -> int:
    return cost.peak_vram.vram_bytes + int(reserve_gb * GB)


@dataclass(frozen=True)
class VramBudget:
    """Graphics memory photo textures may take: red above ``bad``, amber
    above ``warn``."""

    bad: int
    warn: int
    detail: str
    custom: bool = False


def vram_budget(hw, reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB, limits=None):
    """The budget from the [limits] vram_gb (when set) or the card."""
    if limits is not None and limits.is_custom("vram_gb") and limits.vram_gb:
        bad = int(limits.vram_gb * GB)
        return VramBudget(
            bad, int(bad * VRAM_WARN_SHARE),
            f"your limit of {limits.vram_gb:g} GB", True,
        )
    if not hw or not hw.vram:
        return None
    reserve = int(float(reserve_gb) * GB)
    return VramBudget(
        hw.vram - reserve,
        int(hw.vram * VRAM_WARN_SHARE) - reserve,
        f"{_gb(hw.vram)} card minus {float(reserve_gb):g} GB for X-Plane",
    )


def check_vram(cost, hw, reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB, fix=None,
               limits=None):
    peak = cost.peak_vram
    textures = peak.vram_bytes
    where = peak.scenario.label.lower()
    budget = vram_budget(hw, reserve_gb, limits)
    if budget is None:
        return GuardResult(
            "vram",
            Level.INFO,
            "Graphics memory",
            f"Photo textures need about {_gb(textures)} ({where}). "
            "This PC's graphics memory is unknown, so it is not checked.",
        )
    detail = (
        f"about {_gb(textures)} of photo textures ({where}) for "
        f"{_gb(max(0, budget.bad))} ({budget.detail})"
    )
    if textures > budget.bad:
        lead = (
            "Over your graphics memory limit" if budget.custom
            else "Too much for this graphics card"
        )
        return GuardResult(
            "vram",
            Level.BAD,
            "Graphics memory",
            f"{lead}: {detail}. Expect stutters or blurry scenery when it "
            "runs out.",
            fix,
        )
    if textures > budget.warn:
        return GuardResult(
            "vram",
            Level.WARN,
            "Graphics memory",
            f"Close to the limit: {detail}. Heavy aircraft or high X-Plane "
            "texture settings may not fit.",
            fix,
        )
    return GuardResult(
        "vram", Level.OK, "Graphics memory", f"Fits: {detail}."
    )


def _raise_vram_limit(cost, hw, reserve_gb, limits):
    """Back to the automatic graphics memory limit, when that alone fits."""
    if limits is None or not limits.is_custom("vram_gb"):
        return None
    auto = vram_budget(hw, reserve_gb, None)
    if auto is None or cost.peak_vram.vram_bytes > auto.warn:
        return None
    return Fix(
        f"Use the automatic graphics memory limit ({_gb(max(0, auto.bad))})",
        {"limits.vram_gb": "auto"},
    )


def _cost_for(mode, zoom, airport, steps, k, fmt):
    return tc.target_cost(mode, zoom, airport, steps, k=k, fmt=fmt)


def suggest_target(
    mode,
    max_zoom,
    airport_zoom,
    steps,
    hw,
    reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB,
    *,
    k=tc.DEFAULT_K,
    fmt="BC1",
    max_rounds=8,
    limits=None,
) -> Optional[Fix]:
    """The nearest target that fits the graphics card, one level at a time.

    Each round lowers by one the zoom level that saves the most graphics
    memory, until it fits the amber line of the budget. Returns None when
    there is no budget, the target already fits, or nothing can be lowered.
    """
    budget = vram_budget(hw, reserve_gb, limits)
    if budget is None:
        return None
    limit = budget.warn
    dynamic = str(mode).lower() == "dynamic" and bool(tc._steps(steps))
    zoom, airport = int(max_zoom), int(airport_zoom)
    ordered = [list(step) for step in tc._steps(steps)] if dynamic else []

    def peak(z, a, s):
        cost = _cost_for(mode, z, a, [
            {"altitude_ft": alt, "zoom_level": sz, "zoom_level_airports": sa}
            for alt, sz, sa in s
        ], k, fmt)
        return cost.peak_vram.vram_bytes

    current = peak(zoom, airport, ordered)
    if current <= limit:
        return None
    changed = False
    for _round in range(max_rounds):
        options = []
        if dynamic:
            for index, (_alt, sz, sa) in enumerate(ordered):
                if sz > 12:
                    trial = [list(step) for step in ordered]
                    trial[index][1] -= 1
                    options.append((peak(zoom, airport, trial), trial, zoom, airport))
                if sa > 12 and sa > sz:
                    trial = [list(step) for step in ordered]
                    trial[index][2] -= 1
                    options.append((peak(zoom, airport, trial), trial, zoom, airport))
        else:
            if zoom > 12:
                options.append((peak(zoom - 1, airport, []), [], zoom - 1, airport))
            if airport > 12 and airport > zoom:
                options.append((peak(zoom, airport - 1, []), [], zoom, airport - 1))
        if not options:
            break
        best = min(options, key=lambda option: option[0])
        if best[0] >= current:
            break
        current, ordered, zoom, airport = best
        changed = True
        if current <= limit:
            break
    if not changed:
        return None
    if dynamic:
        steps_out = [
            {"altitude_ft": alt, "zoom_level": sz, "zoom_level_airports": sa}
            for alt, sz, sa in ordered
        ]
        label = "Lower the quality steps to " + ", ".join(
            f"ZL{sz}/{sa} from {alt:,} ft" for alt, sz, sa in ordered
        )
        return Fix(label, {"autoortho.dynamic_zoom_steps": steps_out})
    return Fix(
        f"Use ZL{zoom} (airports ZL{airport})",
        {
            "autoortho.max_zoom": str(zoom),
            "autoortho.max_zoom_near_airports": str(airport),
        },
    )


# -------------------------------------------------------------------- RAM


def recommended_cache_gb(hw) -> int:
    return max(2, min(8, hw.ram_gb // 4)) if hw and hw.ram_total else 4


def check_ram(cache_mem_limit_gb, hw, limits=None):
    if not hw or not hw.ram_total:
        return GuardResult(
            "ram", Level.INFO, "RAM", "This PC's RAM is unknown."
        )
    total = hw.ram_total
    planned = (float(cache_mem_limit_gb) + RAM_OVERHEAD_GB) * GB
    left = total - planned
    detail = (
        f"memory cache {float(cache_mem_limit_gb):g} GB (about "
        f"{_gb(planned)} with the app) of {hw.ram_gb} GB RAM"
    )
    recommended = recommended_cache_gb(hw)
    ceiling = limits.cache_ceiling_gb if limits is not None else None
    if ceiling is not None:
        recommended = min(recommended, ceiling)
    fix = None
    if recommended < float(cache_mem_limit_gb):
        fix = Fix(
            f"Use a {recommended} GB memory cache",
            {"cache.cache_mem_limit": str(recommended)},
        )
    if left < RAM_KEEP_FREE_GB * GB:
        return GuardResult(
            "ram",
            Level.BAD,
            "RAM",
            f"Leaves too little for X-Plane: {detail}.",
            fix,
        )
    if planned > total * RAM_WARN_SHARE:
        return GuardResult(
            "ram",
            Level.WARN,
            "RAM",
            f"Large for this PC: {detail}. X-Plane may run short of memory.",
            fix,
        )
    if ceiling is not None and float(cache_mem_limit_gb) > ceiling:
        tag = "your" if limits.is_custom("ram_gb") else "the automatic"
        return GuardResult(
            "ram",
            Level.WARN,
            "RAM",
            f"Above {tag} RAM limit of {limits.ram_gb:g} GB: {detail}. "
            "AutoOrtho will brake when it gets there.",
            fix,
        )
    return GuardResult("ram", Level.OK, "RAM", f"Fits: {detail}.")


# ------------------------------------------------------------------- disk


def free_bytes(path) -> Optional[int]:
    if not path:
        return None
    current = Path(str(path)).expanduser()
    try:
        while not current.exists() and current != current.parent:
            current = current.parent
        return shutil.disk_usage(current).free
    except OSError:
        return None


def check_disk(cache_dir, cost, cache_limit_gb=None, cache_used_bytes=None,
               margin_gb=2.0):
    """Free space on the cache drive against what the cache may still take.

    With a cache limit, the cache can grow by (limit - used); the drive needs
    that plus the safety margin. Without one, free space alone is judged.
    """
    free = free_bytes(cache_dir)
    per_hour = cost.cache_bytes_per_hour
    if free is None:
        return GuardResult(
            "disk",
            Level.INFO,
            "Disk",
            f"About {_gb(per_hour)} of new imagery per hour of flying.",
        )
    margin = int(float(margin_gb) * GB)
    detail = (
        f"{_gb(free)} free on the cache drive; about {_gb(per_hour)} of new "
        "imagery per hour of flying"
    )
    if free < max(DISK_BAD_FREE, margin):
        return GuardResult(
            "disk",
            Level.BAD,
            "Disk",
            f"Almost full: {detail}. AutoOrtho stops adding to the cache "
            "until space is freed. Free some space or move the cache.",
        )
    if cache_limit_gb:
        room = max(0, int(float(cache_limit_gb) * GB) - int(cache_used_bytes or 0))
        if free < room + margin:
            return GuardResult(
                "disk",
                Level.WARN,
                "Disk",
                f"The cache may still grow by {_gb(room)}, but only "
                f"{_gb(free)} is free on its drive. Lower the disk cache "
                "limit or free some space.",
                Fix(
                    f"Use a {max(10, int((free - margin) / GB + (cache_used_bytes or 0) / GB))} GB disk cache",
                    {"cache.file_cache_size": str(max(
                        10, int((free - margin) / GB + (cache_used_bytes or 0) / GB)
                    ))},
                ),
            )
    if free < max(DISK_WARN_FREE, 2 * per_hour):
        return GuardResult("disk", Level.WARN, "Disk", f"Getting full: {detail}.")
    return GuardResult("disk", Level.OK, "Disk", f"Fits: {detail}.")


# --------------------------------------------------------------- download


DOWNLOAD_WARN_SHARE = 0.8      # amber when the target needs more of the line


def check_download(cost, limits=None, connection=None):
    """What the target needs to keep up, against the line and the limit.

    The line speed is the last connection test's; without one, and without a
    bandwidth limit, this is information only.
    """
    busiest = cost.peak_rate
    needed = busiest.mbit_per_s
    start = max(cost.phases, key=lambda phase: phase.initial_bytes)
    need_text = (
        f"about {needed:.0f} Mbit/s to keep up "
        f"({busiest.scenario.label.lower()}); a new area loads about "
        f"{_gb(start.initial_bytes)}"
    )
    caps = []
    if connection is not None and connection.mbits > 0:
        caps.append((connection.mbits, "your connection"))
    if limits is not None and limits.bandwidth_mbps:
        caps.append((limits.bandwidth_mbps, "your download limit"))
    if not caps:
        return GuardResult(
            "download",
            Level.INFO,
            "Download",
            f"Needs {need_text}. Test your connection to check it.",
        )
    available, what = min(caps)
    detail = f"{need_text}, of {available:.0f} Mbit/s ({what})"
    if needed > available * DOWNLOAD_WARN_SHARE:
        fix = None
        line = connection.mbits if connection is not None else 0
        if (
            what == "your download limit"
            and line * DOWNLOAD_WARN_SHARE >= needed
        ):
            raised = int(min(line, max(needed / DOWNLOAD_WARN_SHARE, needed + 5)) + 0.999)
            fix = Fix(
                f"Raise the download limit to {raised} Mbit/s",
                {"limits.bandwidth_mbps": str(raised)},
            )
        return GuardResult(
            "download",
            Level.WARN,
            "Download",
            f"Tight: {detail}. Expect blurry moments in new areas.",
            fix,
        )
    return GuardResult("download", Level.OK, "Download", f"Fits: {detail}.")


def download_info(cost):
    """The download need alone (kept for callers without limits)."""
    return check_download(cost)


# --------------------------------------------------------- target advice

# Fixed targets from sharpest down, tried in order.
_CANDIDATES = (
    (17, 18), (16, 18), (16, 17), (15, 17), (15, 16), (14, 16), (13, 15),
)


def suggest_target_for_pc(
    hw,
    limits=None,
    connection=None,
    reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB,
    *,
    k=tc.DEFAULT_K,
    fmt="BC1",
):
    """The sharpest fixed target every known fact allows, with the reason.

    The graphics memory budget must hold it (amber line), and the line or
    download limit must carry it with a fifth to spare. Without either fact
    the answer is ZL16 with ZL18 at airports, a good start on most PCs.
    """
    budget = vram_budget(hw, reserve_gb, limits)
    caps = []
    if connection is not None and connection.mbits > 0:
        caps.append(connection.mbits)
    if limits is not None and limits.bandwidth_mbps:
        caps.append(limits.bandwidth_mbps)
    line = min(caps) if caps else None
    if budget is None and line is None:
        return Fix(
            "ZL16, airports ZL18: a good start on most PCs. Test your "
            "connection for advice that fits yours.",
            _fixed_changes(16, 18),
        )
    for zoom, airport in _CANDIDATES:
        cost = tc.target_cost("fixed", zoom, airport, k=k, fmt=fmt)
        if budget is not None and cost.peak_vram.vram_bytes > budget.warn:
            continue
        if line is not None and cost.peak_rate.mbit_per_s > line * DOWNLOAD_WARN_SHARE:
            continue
        facts = []
        if budget is not None:
            facts.append(f"graphics memory {budget.detail}")
        if line is not None:
            facts.append(f"downloads up to {line:.0f} Mbit/s")
        return Fix(
            f"ZL{zoom}, airports ZL{airport}: fits " + " and ".join(facts) + ".",
            _fixed_changes(zoom, airport),
        )
    zoom, airport = _CANDIDATES[-1]
    return Fix(
        f"ZL{zoom}, airports ZL{airport}: the lightest target; this PC is "
        "short of graphics memory or bandwidth even for that.",
        _fixed_changes(zoom, airport),
    )


def _fixed_changes(zoom, airport):
    return {
        "autoortho.max_zoom_mode": "fixed",
        "autoortho.max_zoom": str(zoom),
        "autoortho.max_zoom_near_airports": str(airport),
    }


# ------------------------------------------------------------------ all


def worst(results):
    return max((result.level for result in results), key=lambda lvl: lvl.rank,
               default=Level.OK)


def check_target(
    mode,
    max_zoom,
    airport_zoom,
    steps,
    hw,
    *,
    cache_mem_limit_gb=None,
    cache_dir=None,
    reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB,
    k=tc.DEFAULT_K,
    fmt="BC1",
    with_fix=True,
    limits=None,
    connection=None,
    disk_cache_gb=None,
    cache_used_bytes=None,
    disk_margin_gb=2.0,
):
    """Every guard for a target and memory cache, worst first.

    RAM and disk are left out when their value is not given.
    """
    cost = tc.target_cost(mode, max_zoom, airport_zoom, steps, k=k, fmt=fmt)
    vram = check_vram(cost, hw, reserve_gb, limits=limits)
    if with_fix and vram.level.rank:
        fix = _raise_vram_limit(cost, hw, reserve_gb, limits)
        if fix is None:
            fix = suggest_target(
                mode, max_zoom, airport_zoom, steps, hw, reserve_gb, k=k,
                fmt=fmt, limits=limits,
            )
        vram = check_vram(cost, hw, reserve_gb, fix, limits=limits)
    results = [vram]
    if cache_mem_limit_gb is not None:
        results.append(check_ram(cache_mem_limit_gb, hw, limits))
    if cache_dir is not None:
        results.append(check_disk(
            cache_dir, cost, disk_cache_gb, cache_used_bytes, disk_margin_gb
        ))
    results.append(check_download(cost, limits, connection))
    return sorted(results, key=lambda result: -result.level.rank)


def target_of(cfg):
    """(mode, max_zoom, airport_zoom, steps) saved in ``cfg``."""
    ao = cfg.autoortho
    return (
        str(getattr(ao, "max_zoom_mode", "fixed")),
        int(getattr(ao, "max_zoom", 16)),
        int(getattr(ao, "max_zoom_near_airports", 18)),
        _steps_of(cfg),
    )


def paging_k(cfg):
    try:
        k = float(getattr(cfg.streaming, "paging_k", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        k = 0.0
    return k if k > 0 else tc.DEFAULT_K


def texture_format(cfg):
    return str(getattr(getattr(cfg, "pydds", None), "format", "BC1"))


def warnings_enabled(cfg):
    value = getattr(getattr(cfg, "guards", None), "warnings", True)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def check_config(cfg, hw, *, with_fix=True, include_disk=True,
                 connection=None):
    """Every guard for the saved config, worst first."""
    mode, zoom, airport, steps = target_of(cfg)
    try:
        active = limits_module.from_config(cfg, hw)
    except Exception:
        active = None
    return check_target(
        mode,
        zoom,
        airport,
        steps,
        hw,
        cache_mem_limit_gb=getattr(cfg.cache, "cache_mem_limit", 4),
        cache_dir=getattr(cfg.paths, "cache_dir", "") if include_disk else None,
        disk_cache_gb=getattr(cfg.cache, "file_cache_size", None),
        reserve_gb=reserve_gb(cfg),
        k=paging_k(cfg),
        fmt=texture_format(cfg),
        with_fix=with_fix,
        limits=active,
        connection=connection,
    )


def reserve_gb(cfg):
    try:
        return float(getattr(cfg.guards, "xplane_vram_reserve_gb"))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_XPLANE_VRAM_RESERVE_GB


def _steps_of(cfg):
    raw = getattr(cfg.autoortho, "dynamic_zoom_steps", [])
    if isinstance(raw, str):
        import ast

        try:
            return ast.literal_eval(raw) if raw.strip() else []
        except (ValueError, SyntaxError):
            return []
    return raw or []

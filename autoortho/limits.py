"""Limits: what the user allows AutoOrtho to use of this PC.

Each limit in ``[limits]`` is ``auto`` (worked out from the hardware) or the
user's own ceiling:

* ``ram_gb``: RAM for AutoOrtho (memory cache, build buffers, the app). Auto
  is a quarter of RAM plus 0.5 GB for the app, and always leaves X-Plane and
  the system at least max(4 GB, 20 %).
* ``vram_gb``: graphics memory for photo textures. Auto is the card's memory
  minus ``[guards] xplane_vram_reserve_gb``; unknown when the card is.
* ``bandwidth_mbps``: download speed. Auto is no limit.
* ``cpu_cores``: physical cores for building textures. Auto keeps two for
  X-Plane (one on PCs with four cores or fewer).

Limits are ceilings for the values AutoOrtho picks itself (defaults and the
hardware baseline). A value the user set by hand is kept even above a limit;
the guards then say so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

GB = 1024 ** 3

AUTO = "auto"
NAMES = ("ram_gb", "vram_gb", "bandwidth_mbps", "cpu_cores")
APP_OVERHEAD_GB = 0.5          # the app process beside the memory cache
MIN_CACHE_GB = 2               # the memory cache's documented minimum
DEFAULT_XPLANE_VRAM_RESERVE_GB = 5.0
# Settings whose automatic value a limit can lower ("section.key").
GOVERNED = (
    "cache.cache_mem_limit",
    "autoortho.predictive_dds_memory_mb",
    "autoortho.live_builder_concurrency",
    "autoortho.background_builder_workers",
)


def parse(text) -> Optional[float]:
    """A limit's value: None for auto, else a positive number."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text > 0 else None
    raw = str(text).strip().lower()
    if raw in ("", AUTO, "0"):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def auto_ram_gb(hw) -> Optional[float]:
    if not hw or not hw.ram_total:
        return None
    ram = hw.ram_total / GB
    keep = max(4.0, 0.2 * ram)
    share = ram * 0.25 + APP_OVERHEAD_GB
    value = max(MIN_CACHE_GB + APP_OVERHEAD_GB, min(share, ram - keep))
    return math.floor(value * 2) / 2   # half-GB steps read better


def auto_vram_gb(hw, reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB) -> Optional[float]:
    if not hw or not hw.vram:
        return None
    return max(1.0, round(hw.vram / GB - float(reserve_gb), 1))


def auto_cpu_cores(hw) -> Optional[int]:
    if not hw or not hw.physical_cores:
        return None
    cores = int(hw.physical_cores)
    return max(1, cores - 2) if cores >= 6 else max(1, cores - 1)


@dataclass(frozen=True)
class Limits:
    ram_gb: Optional[float]
    vram_gb: Optional[float]
    bandwidth_mbps: Optional[float]   # None: no limit
    cpu_cores: Optional[int]
    threads_per_core: int = 1
    custom: frozenset = field(default_factory=frozenset)
    auto: dict = field(default_factory=dict)

    @property
    def cpu_threads(self) -> Optional[int]:
        """Logical threads AutoOrtho may run native builds on."""
        if self.cpu_cores is None:
            return None
        return max(1, int(self.cpu_cores) * max(1, self.threads_per_core))

    @property
    def cache_ceiling_gb(self) -> Optional[int]:
        """The largest memory cache the RAM limit leaves room for."""
        if self.ram_gb is None:
            return None
        return max(MIN_CACHE_GB, int(math.floor(self.ram_gb - APP_OVERHEAD_GB)))

    @property
    def bytes_per_second(self) -> float:
        """Download ceiling for the broker (0: unlimited)."""
        if not self.bandwidth_mbps:
            return 0.0
        return self.bandwidth_mbps * 1_000_000 / 8

    def is_custom(self, name) -> bool:
        return name in self.custom


def resolve(values, hw, reserve_gb=DEFAULT_XPLANE_VRAM_RESERVE_GB) -> Limits:
    """Limits from ``values`` (name -> text, e.g. the [limits] section)."""
    get = values.get if isinstance(values, dict) else (
        lambda name, default=None: getattr(values, name, default)
    )
    auto = {
        "ram_gb": auto_ram_gb(hw),
        "vram_gb": auto_vram_gb(hw, reserve_gb),
        "bandwidth_mbps": None,
        "cpu_cores": auto_cpu_cores(hw),
    }
    chosen = {}
    custom = set()
    for name in NAMES:
        value = parse(get(name, AUTO))
        if value is None:
            chosen[name] = auto[name]
        else:
            custom.add(name)
            chosen[name] = value
    if chosen["cpu_cores"] is not None:
        chosen["cpu_cores"] = max(1, int(round(chosen["cpu_cores"])))
        if hw and hw.physical_cores:
            chosen["cpu_cores"] = min(chosen["cpu_cores"], int(hw.physical_cores))
    tpc = 1
    if hw and hw.physical_cores and hw.logical_cores:
        tpc = max(1, int(hw.logical_cores) // max(1, int(hw.physical_cores)))
    return Limits(
        chosen["ram_gb"],
        chosen["vram_gb"],
        chosen["bandwidth_mbps"],
        chosen["cpu_cores"],
        tpc,
        frozenset(custom),
        auto,
    )


def from_config(cfg, hw) -> Limits:
    try:
        reserve = float(getattr(cfg.guards, "xplane_vram_reserve_gb"))
    except (AttributeError, TypeError, ValueError):
        reserve = DEFAULT_XPLANE_VRAM_RESERVE_GB
    section = getattr(cfg, "limits", None)
    return resolve(section if section is not None else {}, hw, reserve)


def clamp_automatic(values: dict, limits: Limits) -> dict:
    """Clamp automatic values ("section.key" -> text) to the limits.

    Only the settings a limit governs are touched; the result has the same
    keys plus any value a limit lowered.
    """
    out = dict(values)
    ceiling = limits.cache_ceiling_gb
    if ceiling is not None and "cache.cache_mem_limit" in out:
        current = float(out["cache.cache_mem_limit"])
        if current > ceiling:
            out["cache.cache_mem_limit"] = str(ceiling)
    if limits.ram_gb is not None and "autoortho.predictive_dds_memory_mb" in out:
        share = max(128, int(limits.ram_gb * 1024 * 0.25) // 64 * 64)
        if int(out["autoortho.predictive_dds_memory_mb"]) > share:
            out["autoortho.predictive_dds_memory_mb"] = str(share)
    if limits.cpu_cores is not None:
        # Native threads are capped by the core limit itself (cpu_threads).
        # Live builders mostly wait on downloads, so they may exceed the
        # cores by two; this only keeps them from queueing for threads.
        for key, allowed in (
            ("autoortho.live_builder_concurrency", max(4, limits.cpu_cores + 2)),
            ("autoortho.background_builder_workers", max(1, limits.cpu_cores // 3)),
        ):
            if key in out and int(out[key]) > allowed:
                out[key] = str(allowed)
    return out


def describe(limits: Limits) -> dict:
    """One short line per limit, for the UI ("4.5 GB (auto)")."""
    def fmt(name, value, unit):
        if value is None:
            text = "no limit" if name == "bandwidth_mbps" else "unknown"
        else:
            number = f"{value:g}"
            text = f"{number} {unit}".strip()
        tag = "your limit" if limits.is_custom(name) else "auto"
        return f"{text} ({tag})"

    return {
        "ram_gb": fmt("ram_gb", limits.ram_gb, "GB"),
        "vram_gb": fmt("vram_gb", limits.vram_gb, "GB"),
        "bandwidth_mbps": fmt("bandwidth_mbps", limits.bandwidth_mbps, "Mbit/s"),
        "cpu_cores": fmt("cpu_cores", limits.cpu_cores, "cores"),
    }

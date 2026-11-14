"""The tuning plan: starting values from the quality target, line and PC.

The hardware baseline (:func:`hardware.baseline_values`) sizes memory and
workers to the PC alone. The plan adds what the quality target and the
connection decide, so a flight starts near the right values and the governor
only has to fine-tune:

* how much imagery the target needs: :mod:`target_cost`'s busiest flight
  phase (Mbit/s to keep up) and the download to load a new area;
* what the line gives: the last connection test of this provider, capped by
  the bandwidth limit;
* the ratio of the two (``r``), which says whether prefetch has room to run
  ahead (r of 3 or more) or must stay close to the aircraft (r near 1).

Rules (each value stays inside the governor's lever range, so the governor
can move it further on evidence):

* Prefetch chunks per cycle: enough to admit up to three times the need per
  second, never more than the line carries (at least the packaged 64).
* Predictive build memory: room for every background builder to decode one
  texture of the target at once (RGBA), within a quarter of the RAM limit.
  Only ever raised over the hardware baseline.

With a measured line only:

* Prefetch radius: 30 nm scaled by r / 3, 15 to 60 nm (wider when the line
  has room for turns, narrower when it must spend everything on the track).
* Prefetch lookahead and paging lead: the packaged 10 min and 60 s while
  r >= 3, lengthened up to 2x (lead 2.5x) as r falls toward 1, so prefetch
  starts earlier on a tight line. Below r = 1 the line cannot keep up at
  all and longer lookahead only builds a backlog: the defaults stay.
* Wait for near imagery: 1.5x the time one full near texture of the target
  takes on the line (plus a chunk's round trip), 6 to 12 s. Only raised.
* Wait while starting: 1.2x the time to load a new area, 60 to 120 s. Only
  raised.

Download concurrency from the line is sized in aoconfig, next to the
bandwidth limit's ceiling, with the connection test's per-request speed.

Pure: no I/O. Values are config text by "section.key".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

if __package__:
    from autoortho import target_cost as tc
else:
    import target_cost as tc

MB = 1024 ** 2

DEFAULT_CHUNKS = 64
MAX_CHUNKS = 2048
DEFAULT_RADIUS_NM = 30
DEFAULT_LOOKAHEAD_MIN = 10
DEFAULT_LEAD_S = 60
DEFAULT_BAND_WAIT_S = 6.0
DEFAULT_NEAR_WAIT_S = 60.0
DEFAULT_CHUNK_SECONDS = 0.13
ROOMY = 3.0            # r at which prefetch has room to run ahead
PREFETCH_SHARE = 3.0   # prefetch may admit up to this many times the need


@dataclass(frozen=True)
class Plan:
    values: Dict[str, str] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)
    need_mbps: float = 0.0
    capacity_mbps: Optional[float] = None

    @property
    def ratio(self) -> Optional[float]:
        if not self.capacity_mbps or self.need_mbps <= 0:
            return None
        return self.capacity_mbps / self.need_mbps


def _round_to(value: float, step: float) -> float:
    return round(value / step) * step


def _clamp(value, low, high):
    return max(low, min(high, value))


def _number(text, default) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return float(default)


def largest_texture_px(cost: "tc.TargetCost") -> float:
    """Side of the largest texture the target builds (scenery or airport)."""
    largest = 0.0
    for phase in cost.phases:
        scenario = phase.scenario
        largest = max(largest, tc.texture_px(scenario.zoom, tc.SCENERY_TILE_ZOOM))
        if scenario.airport_zoom is not None:
            largest = max(
                largest, tc.texture_px(scenario.airport_zoom, tc.AIRPORT_TILE_ZOOM)
            )
    return largest or float(tc.TEXTURE_PX)


def plan(
    cost: "tc.TargetCost",
    *,
    line_mbps: Optional[float] = None,
    bandwidth_mbps: Optional[float] = None,
    chunk_seconds: Optional[float] = None,
    background_workers: int = 2,
    ram_limit_gb: Optional[float] = None,
    prefetch_interval_s: float = 1.0,
    current: Optional[Dict[str, str]] = None,
) -> Plan:
    """Starting values for ``cost`` on this line and PC.

    ``current`` holds the values chosen so far (hardware baseline, else the
    packaged default) for the "only raised" rules.
    """
    current = current or {}
    values: Dict[str, str] = {}
    reasons: Dict[str, str] = {}
    need = max(0.1, float(cost.peak_rate.mbit_per_s))
    caps = [c for c in (line_mbps, bandwidth_mbps) if c and c > 0]
    capacity = min(caps) if caps else None
    ratio = capacity / need if capacity else None
    need_text = f"the target needs about {need:.0f} Mbit/s"
    line_text = (
        f"{need_text} of {capacity:.0f} Mbit/s" if capacity else need_text
    )

    # Prefetch chunks per cycle: admit up to PREFETCH_SHARE x the need.
    rate_mbps = need * PREFETCH_SHARE
    if capacity:
        rate_mbps = min(rate_mbps, capacity)
    per_cycle = rate_mbps * 1_000_000 / 8 / tc.CHUNK_BYTES * max(0.1, prefetch_interval_s)
    chunks = int(_clamp(math.ceil(per_cycle / 64.0) * 64, DEFAULT_CHUNKS, MAX_CHUNKS))
    values["autoortho.prefetch_max_chunks"] = str(chunks)
    reasons["autoortho.prefetch_max_chunks"] = (
        f"prefetch keeps up with {rate_mbps:.0f} Mbit/s; {line_text}"
    )

    # Predictive build memory: every builder decodes one texture at once.
    side = largest_texture_px(cost)
    workers = max(1, int(background_workers))
    need_mb = int(math.ceil(workers * side * side * 4 / MB / 128.0) * 128)
    if ram_limit_gb:
        # In 128 MB steps: inside both the limit's clamp and the lever's range.
        need_mb = min(need_mb, max(128, int(ram_limit_gb * 1024 * 0.25) // 128 * 128))
    have_mb = _number(current.get("autoortho.predictive_dds_memory_mb"), 512)
    if need_mb > have_mb:
        values["autoortho.predictive_dds_memory_mb"] = str(need_mb)
        reasons["autoortho.predictive_dds_memory_mb"] = (
            f"{workers} background builders x {int(side)} px textures"
        )

    if not capacity:
        return Plan(values, reasons, need, None)

    cap_bytes = capacity * 1_000_000 / 8
    # Prefetch corridor, scaled by the room the line has.
    radius = int(_clamp(_round_to(DEFAULT_RADIUS_NM * _clamp(ratio / ROOMY, 0.5, 2.0), 5),
                        15, 60))
    values["autoortho.prefetch_radius_nm"] = str(radius)
    reasons["autoortho.prefetch_radius_nm"] = f"corridor for {line_text}"

    if ratio >= 1.0:
        stretch = _clamp(ROOMY / ratio, 1.0, 2.0)
        lookahead = int(round(DEFAULT_LOOKAHEAD_MIN * stretch))
        lead = int(_clamp(_round_to(DEFAULT_LEAD_S * _clamp(ROOMY / ratio, 1.0, 2.5), 15),
                          30, 180))
        values["autoortho.prefetch_lookahead"] = str(lookahead)
        values["streaming.paging_lead_seconds"] = str(lead)
        reasons["autoortho.prefetch_lookahead"] = f"start prefetch earlier: {line_text}"
        reasons["streaming.paging_lead_seconds"] = f"start prefetch earlier: {line_text}"

    # Wait for near imagery: one full near texture on this line.
    latency = chunk_seconds if chunk_seconds and chunk_seconds > 0 else DEFAULT_CHUNK_SECONDS
    texture_bytes = (side / tc.CHUNK_PX) ** 2 * tc.CHUNK_BYTES
    texture_s = texture_bytes / cap_bytes + latency
    band = float(_clamp(math.ceil(1.5 * texture_s), DEFAULT_BAND_WAIT_S, 12.0))
    have_band = _number(current.get("streaming.band_max_wait_seconds"), DEFAULT_BAND_WAIT_S)
    if band > have_band:
        values["streaming.band_max_wait_seconds"] = f"{band:g}"
        reasons["streaming.band_max_wait_seconds"] = (
            f"a {int(side)} px texture takes about {texture_s:.0f} s at {capacity:.0f} Mbit/s"
        )

    # Wait while starting: loading a new area.
    start_bytes = max(phase.initial_bytes for phase in cost.phases)
    area_s = start_bytes / cap_bytes
    near = float(_clamp(math.ceil(1.2 * area_s / 10.0) * 10, DEFAULT_NEAR_WAIT_S, 120.0))
    have_near = _number(current.get("streaming.near_wait_seconds"), DEFAULT_NEAR_WAIT_S)
    if near > have_near:
        values["streaming.near_wait_seconds"] = f"{near:g}"
        reasons["streaming.near_wait_seconds"] = (
            f"a new area takes about {area_s:.0f} s to load at {capacity:.0f} Mbit/s"
        )
    return Plan(values, reasons, need, capacity)

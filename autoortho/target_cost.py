"""What a zoom target costs: graphics memory, downloads and disk.

The estimate follows X-Plane's texture paging (see :mod:`paging_model`):
X-Plane wants full detail of a texture only out to ``k`` of its own pixels
from the camera, half size out to ``2k``, and so on. A texture stays in
graphics memory while its scenery is loaded, at the most detail it was wanted
in the last 20 minutes or so (OrthoStreamXP measured its peaks over 20-minute
spans). So for each flight phase this flies a straight path for 20 minutes and
adds up, per texture, the detail wanted at its closest approach:

* graphics memory: each texture's DDS at that detail, smaller levels included;
* download: the imagery chunks that detail needs, at the average chunk size;
* the download rate: what the path adds beyond the area loaded at its start.

Scenery textures come from ZL16 tiles and airport textures from ZL18 tiles
(the standard AutoOrtho packages); a texture is built at the target zoom,
at most one level above its tile (X-Plane's limit), so ZL17 on a ZL16 tile is
8192 px and ZL15 is 2048 px.

These are estimates for comparison and warnings, not promises: a wide screen
or a wide field of view (larger ``k``) and turning flight hold more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

if __package__:
    from autoortho.paging_model import DEFAULT_K, mip_for_k, tile_size_m
else:
    from paging_model import DEFAULT_K, mip_for_k, tile_size_m

GB = 1024 ** 3

TEXTURE_PX = 4096           # a texture at its tile's own zoom: 16 x 256 px
CHUNK_PX = 256
SCENERY_TILE_ZOOM = 16
AIRPORT_TILE_ZOOM = 18
# Average downloaded imagery chunk: 3.11 GB over 193,401 chunks on a recorded
# ZL17 flight (diagnostics report, September 2026).
CHUNK_BYTES = 16_000
HELD_MINUTES = 20
# Textures further than this from the aircraft are not loaded at all. Far
# textures are wanted at a few dozen pixels, so the total barely depends on it.
LOADED_RADIUS_M = 80_000
# Around an airport, Ortho4XP builds ZL18 tiles over roughly this radius.
AIRPORT_RADIUS_M = 2_500
EYE_HEIGHT_M = 3.0
REFERENCE_LATITUDE = 45.0
FT = 0.3048
KT = 1852.0 / 3600.0


def texture_bytes(side_px: float, fmt: str = "BC1") -> int:
    """A DDS texture with all smaller levels: BC1 half a byte a pixel, BC3 one."""
    side = max(4.0, float(side_px))
    per_pixel = 1.0 if str(fmt).upper() in ("BC3", "DXT5") else 0.5
    return int(side * side * per_pixel * 4 / 3)


def texture_px(zoom: int, tile_zoom: int) -> float:
    """Full size of a texture built at ``zoom`` for a tile of ``tile_zoom``."""
    built = min(int(zoom), tile_zoom + 1)
    return TEXTURE_PX * 2.0 ** (built - tile_zoom)


def typical_speed_kt(altitude_ft: float) -> float:
    if altitude_ft < 3_000:
        return 160.0
    if altitude_ft < 10_000:
        return 250.0
    if altitude_ft < 20_000:
        return 320.0
    if altitude_ft < 30_000:
        return 400.0
    return 450.0


@dataclass(frozen=True)
class Scenario:
    """One flight phase: a height, a speed and the zoom levels in use."""

    label: str
    altitude_ft: float
    speed_kt: float
    zoom: int
    # Set for phases that start or end at an airport: its ZL18 textures,
    # wanted at full detail on the ground, are still held.
    airport_zoom: Optional[int] = None


@dataclass(frozen=True)
class ScenarioCost:
    scenario: Scenario
    vram_bytes: int          # photo textures held in graphics memory
    initial_bytes: int       # download to load the area when arriving there
    rate_bytes_per_s: float  # download to keep up while flying

    @property
    def mbit_per_s(self) -> float:
        return self.rate_bytes_per_s * 8 / 1_000_000


@dataclass(frozen=True)
class TargetCost:
    phases: tuple

    @property
    def peak_vram(self) -> ScenarioCost:
        return max(self.phases, key=lambda cost: cost.vram_bytes)

    @property
    def peak_rate(self) -> ScenarioCost:
        return max(self.phases, key=lambda cost: cost.rate_bytes_per_s)

    @property
    def cache_bytes_per_hour(self) -> float:
        """New imagery an hour of flying adds, at the busiest phase."""
        return self.peak_rate.rate_bytes_per_s * 3600


class _Sweep:
    """Textures of one tile zoom around a straight path, summed."""

    def __init__(self, zoom, tile_zoom, lat, k, fmt):
        self.size_m = tile_size_m(lat, tile_zoom)
        self.full_px = texture_px(zoom, tile_zoom)
        self.k = k
        self.fmt = fmt

    def _side(self, distance_m):
        mip = mip_for_k(self.k, distance_m, self.size_m, self.full_px)
        return self.full_px / (2 ** mip)

    def totals(self, height_m, path_m, radius_m):
        """(graphics memory, imagery bytes) of the textures held."""
        size = self.size_m
        reach = int(math.ceil(radius_m / size))
        steps = int(math.ceil(path_m / size)) if path_m > 0 else 0
        vram = 0.0
        data = 0.0
        for i in range(-reach, steps + reach + 1):
            x = i * size
            if x < 0:
                along = -x
            elif x > path_m:
                along = x - path_m
            else:
                along = 0.0
            if along > radius_m:
                continue
            for j in range(0, reach + 1):
                y = j * size
                ground = math.hypot(along, y)
                if ground > radius_m:
                    break
                weight = 1 if j == 0 else 2   # the path's other side
                side = self._side(math.hypot(ground, height_m))
                vram += weight * texture_bytes(side, self.fmt)
                data += weight * (side / CHUNK_PX) ** 2 * CHUNK_BYTES
        return vram, data


def estimate(
    scenario: Scenario,
    *,
    k: float = DEFAULT_K,
    lat: float = REFERENCE_LATITUDE,
    fmt: str = "BC1",
) -> ScenarioCost:
    height = max(EYE_HEIGHT_M, scenario.altitude_ft * FT)
    path = max(0.0, scenario.speed_kt) * KT * HELD_MINUTES * 60
    scenery = _Sweep(scenario.zoom, SCENERY_TILE_ZOOM, lat, k, fmt)
    vram, data = scenery.totals(height, path, LOADED_RADIUS_M)
    _vram0, data0 = (
        scenery.totals(height, 0.0, LOADED_RADIUS_M) if path else (vram, data)
    )
    if scenario.airport_zoom is not None:
        # Wanted from the ground, then held through the climb.
        airport = _Sweep(scenario.airport_zoom, AIRPORT_TILE_ZOOM, lat, k, fmt)
        a_vram, a_data = airport.totals(EYE_HEIGHT_M, 0.0, AIRPORT_RADIUS_M)
        vram += a_vram
        data += a_data
        data0 += a_data
    duration = HELD_MINUTES * 60
    rate = max(0.0, data - data0) / duration if path else 0.0
    return ScenarioCost(scenario, int(vram), int(data0), rate)


# ---------------------------------------------------------------- targets


def _steps(steps) -> list:
    """(altitude_ft, zoom, airport_zoom) sorted by altitude."""
    result = []
    for step in steps or ():
        if isinstance(step, dict):
            altitude = step.get("altitude_ft", 0)
            zoom = step.get("zoom_level", 16)
            airport = step.get("zoom_level_airports", 18)
        else:
            altitude = getattr(step, "altitude_ft", 0)
            zoom = getattr(step, "zoom_level", 16)
            airport = getattr(step, "zoom_level_airports", 18)
        result.append((int(altitude), int(zoom), int(airport)))
    return sorted(result)


def scenarios_for(
    mode: str,
    max_zoom: int,
    airport_zoom: int,
    steps: Iterable = (),
) -> list:
    """The flight phases that bound a target's cost."""
    if str(mode).lower() == "dynamic":
        ordered = _steps(steps)
        if ordered:
            _alt, base_zoom, base_airport = ordered[0]
            phases = [Scenario("At the airport", 0, 0, base_zoom, base_airport)]
            for altitude, zoom, airport in ordered:
                shown = max(altitude, 1_500)
                label = (
                    f"From {altitude:,} ft" if altitude
                    else "Departure and approach"
                )
                phases.append(Scenario(
                    label,
                    shown,
                    typical_speed_kt(shown),
                    zoom,
                    airport if not altitude else None,
                ))
            return phases
    zoom = int(max_zoom)
    return [
        Scenario("At the airport", 0, 0, zoom, int(airport_zoom)),
        Scenario(
            "Departure and approach",
            1_500,
            typical_speed_kt(1_500),
            zoom,
            int(airport_zoom),
        ),
        Scenario("10,000 ft", 10_000, typical_speed_kt(10_000), zoom),
        Scenario("Cruise", 35_000, typical_speed_kt(35_000), zoom),
    ]


def target_cost(
    mode: str,
    max_zoom: int,
    airport_zoom: int,
    steps: Iterable = (),
    *,
    k: float = DEFAULT_K,
    fmt: str = "BC1",
) -> TargetCost:
    phases = tuple(
        estimate(scenario, k=k, fmt=fmt)
        for scenario in scenarios_for(mode, max_zoom, airport_zoom, steps)
    )
    return TargetCost(phases)


def target_cost_from_config(cfg) -> TargetCost:
    """The cost of the target saved in ``cfg``."""
    if __package__:
        from autoortho import guards
    else:
        import guards
    mode, zoom, airport, steps = guards.target_of(cfg)
    return target_cost(
        mode, zoom, airport, steps,
        k=guards.paging_k(cfg), fmt=guards.texture_format(cfg),
    )

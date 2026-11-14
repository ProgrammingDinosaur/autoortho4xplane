"""Shared fixtures for the files-mode tests: synthetic regions, a fake
texture service (no network, no getortho) and a fake flight feed."""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from files_mode import dds_layout, geo  # noqa: E402
from texture_service import BuiltLevels  # noqa: E402


def tile_rc(lat, lon, zoom):
    """Row/col of the 16-chunk-aligned texture containing (lat, lon)."""
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return (y // 16) * 16, (x // 16) * 16


def textures_around(lat, lon, zoom=16, radius=3, maptype="BI"):
    row, col = tile_rc(lat, lon, zoom)
    out = []
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            out.append((row + 16 * dr, col + 16 * dc, maptype, zoom))
    return out


def make_region(base, name, textures, overlays=True, masks=True, dsfs=None):
    """A region folder like AutoOrtho's installer makes (names only)."""
    root = os.path.join(base, name)
    terrain = os.path.join(root, "terrain")
    tex = os.path.join(root, "textures")
    end = os.path.join(root, "Earth nav data")
    os.makedirs(terrain, exist_ok=True)
    os.makedirs(tex, exist_ok=True)
    for row, col, maptype, zoom in textures:
        stem = f"{row}_{col}_{maptype}{zoom}"
        lat, lon = geo.texture_centre(row, col, zoom)
        size = geo.texture_size_m(row, col, zoom)
        body = (
            "A\n800\nTERRAIN\n\n"
            f"LOAD_CENTER {lat:.5f} {lon:.5f} {size:.0f} 4096\n"
            f"BASE_TEX_NOWRAP ../textures/{stem}.dds\n"
        )
        with open(os.path.join(terrain, stem + ".ter"), "w") as handle:
            handle.write(body)
        if overlays:
            with open(os.path.join(terrain, stem + "_water_overlay.ter"), "w") as handle:
                handle.write(body + "WET\n")
        if masks:
            with open(os.path.join(tex, f"{row}_{col}_ZL{zoom}.png"), "wb") as handle:
                handle.write(b"\x89PNG mask")
    with open(os.path.join(tex, "water_transition.png"), "wb") as handle:
        handle.write(b"\x89PNG")
    squares = dsfs
    if squares is None:
        squares = set()
        for row, col, _m, zoom in textures:
            squares.update(geo.squares_of(row, col, zoom))
    for lat, lon in squares:
        folder = os.path.join(end, geo.square_folder(lat, lon))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, geo.square_name(lat, lon) + ".dsf"), "wb") as handle:
            handle.write(b"XPLNEDSF")
    return root


def level_byte(mip):
    return 0x10 + mip


class FakeService:
    """Builds deterministic levels: every byte of mip m is 0x10 + m."""

    def __init__(self, fmt="BC1", patchy=False, season_fn=None):
        self.fmt = fmt
        self.patchy = patchy
        self.season_fn = season_fn
        self.calls = []
        self.policies = []

    class _TC:
        max_zoom_mode = "fixed"
        target_zoom_level = 16
        target_zoom_level_near_airports = 18

    tc = _TC()

    def build(self, demand, policy=None):
        self.calls.append(demand)
        self.policies.append(policy)
        key = demand.key
        max_zoom = demand.max_zoom if demand.max_zoom is not None else key.zoom
        top = int(4096 * 2 ** (max_zoom - key.zoom))
        lay = dds_layout.layout(top, top, self.fmt)
        levels = {}
        offsets = {}
        for mip, (start, length) in enumerate(lay.offsets):
            if mip < demand.level:
                continue
            levels[mip] = bytes([level_byte(mip)]) * length
            offsets[mip] = (start, length)
        return BuiltLevels(
            key=key, max_zoom=max_zoom, width=top, dxt_format=lay.dxt_format,
            header=lay.header, total_size=lay.total_size, levels=levels, offsets=offsets,
            patchy=self.patchy, provider="BI",
            season_key=self.season_fn(key.row, key.col, key.zoom) if self.season_fn else None,
        )


class FakeFlight:
    """A ``flight_state`` dict source like the parent's reporter."""

    def __init__(self, lat, lon, alt_m=0.0, hdg=0.0, spd=0.0):
        self.state = {
            "connected": True, "data_valid": True, "lat": lat, "lon": lon, "alt": alt_m,
            "hdg": hdg, "hpath": hdg, "spd": spd, "window_width": 2704, "fov_deg": 65,
        }
        self.valid = True

    def __call__(self):
        import time
        if not self.valid:
            return None
        state = dict(self.state)
        state["timestamp"] = time.time()
        return state

    def move(self, lat, lon):
        self.state["lat"] = lat
        self.state["lon"] = lon

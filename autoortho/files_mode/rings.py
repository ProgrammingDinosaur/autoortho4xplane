"""Which mip level X-Plane reads of a texture, by distance (A3-A6).

Levels are counted from the top of the file (A4): file mip 0 is read
within ring 0, mip 1 within ring 1, and so on, whatever the file's size.
The rings are those measured for a ZL16 texture (about 7 km across) by
OrthoStudio XP Live's probe flights (``live/levels-test.md``: the first
probe flight's rings times 1.13); a texture of another zoom uses the same
law scaled by its zoom. Until our own telemetry settles whether X-Plane
scales by zoom or by the texture's ground size, the larger of the two is
used: building early is safe, building late shows the neutral colour.

``_ao/rings.json`` (written by ``tools/files_mode/fit_rings.py`` from FUSE
telemetry) overrides the table and the constants.
"""

import json
import logging
import math
import os

log = logging.getLogger(__name__)

RINGS_ZL16_KM = {3: 81.4, 2: 42.9, 1: 23.7, 0: 13.6}
ZL16_SIZE_M = 7100.0
# The level X-Plane reads when it loads a DSF (A2): 256 px of a 4096 px file.
LOAD_LEVEL = 4
# Build this much wider than X-Plane reads.
MARGIN = 1.11
REF_WIDTH_PX = 2704.0
REF_FOV_DEG = 65.0
MAX_SCALE = 4.0
# Texture quality notches below "Maximum" each drop the finest level (A6).
QUALITY_MAX = 5


class RingModel:
    def __init__(self, rings=None, margin=MARGIN, size_scaling=True):
        self.rings = dict(rings or RINGS_ZL16_KM)
        self.margin = float(margin)
        self.size_scaling = bool(size_scaling)

    @classmethod
    def load(cls, path=None):
        """The default model, overridden by ``rings.json`` when present."""
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                rings = {int(k): float(v) for k, v in data.get("rings_zl16_km", {}).items()}
                model = cls(
                    rings=rings or None,
                    margin=float(data.get("margin", MARGIN)),
                    size_scaling=bool(data.get("size_scaling", True)),
                )
                log.info("Files mode rings loaded from %s: %s", path, model.rings)
                return model
            except Exception as exc:
                log.warning("Ignoring unreadable rings file %s: %s", path, exc)
        return cls()

    def ring_km(self, level, zoom, size_m=None):
        """Outer radius of ``level`` for a texture of ``zoom`` (no margin)."""
        if level not in self.rings:
            return 0.0 if level < min(self.rings) else math.inf
        base = self.rings[level]
        by_zoom = base * (2.0 ** (16 - int(zoom)))
        if self.size_scaling and size_m:
            return max(by_zoom, base * float(size_m) / ZL16_SIZE_M)
        return by_zoom

    def level_needed(self, distance_km, zoom, size_m=None, scale=1.0,
                     finest=0, margin=None, reach_km=0.0):
        """The sharpest file mip X-Plane may read at ``distance_km``.

        ``scale`` widens the rings (screen width and field of view, A5),
        ``margin`` builds ahead of X-Plane, ``reach_km`` widens every ring
        but the load level's. Never sharper than ``finest`` (A6).
        """
        margin = self.margin if margin is None else margin
        for level in sorted(self.rings):
            if level < finest:
                continue
            radius = self.ring_km(level, zoom, size_m) * scale * margin + reach_km
            if distance_km <= radius:
                return level
        return LOAD_LEVEL

    def distance_to_ring_km(self, distance_km, level, zoom, size_m=None,
                            scale=1.0, margin=None, reach_km=0.0):
        """How far the aircraft still is from ``level``'s ring (<= 0: inside)."""
        margin = self.margin if margin is None else margin
        radius = self.ring_km(level, zoom, size_m) * scale * margin + reach_km
        return distance_km - radius


def screen_scale(width_px, fov_deg):
    """How much wider X-Plane's rings are than at 2704 px and 65 degrees (A5)."""
    try:
        width = float(width_px)
        fov = float(fov_deg)
    except (TypeError, ValueError):
        return 1.0
    if width <= 0 or fov <= 1 or fov >= 179:
        return 1.0
    scale = (width / REF_WIDTH_PX) * (
        math.tan(math.radians(REF_FOV_DEG / 2)) / math.tan(math.radians(fov / 2))
    )
    return max(1.0, min(MAX_SCALE, scale))


def finest_for_quality(tex_res):
    """The finest file mip X-Plane reads at a texture quality notch (A6).

    ``tex_res`` is X-Plane's notch with ``QUALITY_MAX`` for "Maximum";
    None (unknown) builds everything.
    """
    if tex_res is None:
        return 0
    try:
        notch = int(tex_res)
    except (TypeError, ValueError):
        return 0
    return max(0, min(LOAD_LEVEL, QUALITY_MAX - notch))


def top_px_for(tile_zoom, max_zoom):
    """File top size: 4096 px at the tile's own zoom, doubling per zoom above."""
    diff = int(max_zoom) - int(tile_zoom)
    diff = max(-4, min(1, diff))
    return int(4096 * (2.0 ** diff))


def mip_count(top_px):
    return int(math.log2(top_px)) + 1


def mip_px(top_px, mip):
    return max(1, int(top_px) >> int(mip))

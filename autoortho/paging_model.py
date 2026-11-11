"""Which mipmap X-Plane wants of a tile at a given distance.

X-Plane does not document its texture paging. This is a model with one
constant, ``k`` (measured in flight by OrthoStreamXP; see its
``PagingModel.cs``):

* ``x`` is the distance from the camera to the tile's near edge, in the
  tile's own ground pixels: ``x = edge_distance_m * full_px / size_m``.
* X-Plane wants mipmap ``n`` while ``x < 2^n * k``: full detail out to ``k``,
  half size out to ``2k``, quarter size out to ``4k``, and so on.

Measured with a 3840 px screen at 62 degrees field of view, k was about
7500-8200. Until it is measured here, k is guessed from X-Plane's window
width and field of view (screen width / (2 tan(FOV/2)), times 2.5).
:class:`PagingMeasurements` refines it from the levels X-Plane actually reads.
"""

import math
import threading
from dataclasses import dataclass
from typing import Optional

DEFAULT_K = 8000.0
SCREEN_GUESS_FACTOR = 2.5
MIN_K, MAX_K = 1000.0, 40000.0
EARTH_CIRCUMFERENCE_M = 40075016.686
EARTH_RADIUS_M = 6371000.0
# Below this ground speed (about 20 kt: taxiing) the path is not predicted.
MOVING_SPEED_MPS = 10.0
# Samples before a fitted k replaces the guess, and the agreement it needs.
MIN_FIT_SAMPLES = 200
MIN_FIT_AGREEMENT = 0.6
_MAX_SAMPLES = 50000


@dataclass(frozen=True)
class PagingFit:
    k: float
    low: float
    high: float
    agreement: float
    samples: int


def guess_k(screen_width_px, fov_deg):
    return SCREEN_GUESS_FACTOR * screen_width_px / (
        2 * math.tan(math.radians(fov_deg) / 2)
    )


def edge_pixels(centre_distance_m, size_m, full_px):
    """Distance to the tile's near edge in the tile's own ground pixels."""
    return max(0.0, centre_distance_m - size_m / 2) * full_px / size_m


def mip_for_k(k, centre_distance_m, size_m, full_px):
    x = edge_pixels(centre_distance_m, size_m, full_px) / k
    return 0 if x < 1 else int(math.floor(math.log2(x))) + 1


def fit(samples, minimum=20):
    """The k agreeing with the most ``(x, mip)`` samples, or None.

    A read of level n at x means 2^(n-1) k <= x < 2^n k (level 0 has no lower
    end), so x / 2^n < k <= x / 2^(n-1). Sweeping k upwards, passing a lower
    bound fixes one disagreement and passing an upper bound adds one.
    """
    if len(samples) < minimum:
        return None
    bounds = []
    for x, mip in samples:
        bounds.append((x / 2 ** mip, -1))
        if mip >= 1:
            bounds.append((x / 2 ** (mip - 1), +1))
    bounds.sort()
    disagree = len(samples)
    best = None
    low = high = 0.0
    for i, (value, change) in enumerate(bounds):
        disagree += change
        nxt = bounds[i + 1][0] if i + 1 < len(bounds) else math.inf
        if (best is None or disagree < best) and nxt > value:
            best, low, high = disagree, value, nxt
    if best is None:
        return None
    k = low * 1.25 if math.isinf(high) else math.sqrt(low * high)
    return PagingFit(k, low, high, 1 - best / len(samples), len(samples))


class PagingModel:
    """The paging constant k: set by the user, guessed, then measured."""

    def __init__(self, k: Optional[float] = None):
        self._lock = threading.Lock()
        self.fixed = bool(k)
        self._k = float(k) if k else DEFAULT_K
        self._samples = []
        self._fitted: Optional[PagingFit] = None

    @property
    def k(self):
        return self._k

    def use_screen(self, screen_width_px, fov_deg):
        """First guess from X-Plane's window, unless k was set or measured."""
        if self.fixed or self._fitted is not None:
            return
        if not screen_width_px or screen_width_px < 100 or not (5 < fov_deg < 170):
            return
        self._k = min(MAX_K, max(MIN_K, round(guess_k(screen_width_px, fov_deg))))

    def mip_for(self, centre_distance_m, size_m, full_px):
        return mip_for_k(self._k, centre_distance_m, size_m, full_px)

    def note_read(self, mip, centre_distance_m, size_m, full_px):
        """X-Plane wanted ``mip`` at this distance: evidence for k."""
        if centre_distance_m is None or size_m <= 0 or full_px <= 0:
            return
        with self._lock:
            if len(self._samples) < _MAX_SAMPLES:
                self._samples.append(
                    (edge_pixels(centre_distance_m, size_m, full_px), int(mip))
                )
            count = len(self._samples)
            if self.fixed or count < MIN_FIT_SAMPLES or count % 50:
                return
            samples = list(self._samples)
        result = fit(samples, MIN_FIT_SAMPLES)
        if result is not None and result.agreement >= MIN_FIT_AGREEMENT:
            with self._lock:
                self._fitted = result
                self._k = min(MAX_K, max(MIN_K, result.k))

    @property
    def fitted(self):
        return self._fitted


# ---------------------------------------------------------------- geometry


def tile_centre(row, col, zoom, span_tiles=16):
    """Centre (lat, lon) of a texture whose top-left slippy tile is col,row."""
    x = col + span_tiles / 2
    y = row + span_tiles / 2
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def tile_size_m(lat, zoom, span_tiles=16):
    """Ground width of a texture of ``span_tiles`` tiles at ``zoom``."""
    return span_tiles * EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat)) / 2 ** zoom


def ground_distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def camera_distance_m(cam_lat, cam_lon, height_agl_m, lat, lon):
    """Straight-line distance from the camera to a point on the ground."""
    ground = ground_distance_m(cam_lat, cam_lon, lat, lon)
    return math.hypot(ground, max(0.0, height_agl_m or 0.0))


def soon_distance_m(distance_m, cam_lat, cam_lon, lat, lon, track_deg, speed_mps, lead_s):
    """The distance ``lead_s`` seconds from now, if the aircraft keeps its track."""
    if speed_mps is None or speed_mps < MOVING_SPEED_MPS or track_deg is None:
        return distance_m
    to_tile = bearing_deg(cam_lat, cam_lon, lat, lon)
    closing = speed_mps * math.cos(math.radians(to_tile - track_deg))
    return max(0.0, distance_m - max(0.0, closing) * lead_s)

"""How long one of X-Plane's reads may wait, by how near the texture is.

* Near: X-Plane wants mipmap 0 or 1, so the texture is next to the camera. It
  waits for real imagery.
* Middle: a reduced level. It waits as long as near reads do.
* Far: the base level (one low-zoom chunk). It barely waits: it gets the
  best data at hand, and the real data is fetched behind it.

While a flight is starting (the loading screen and the time parked after
it), X-Plane does not read those textures again for a while, so near and
middle reads may wait much longer then (``near_wait_seconds``).
"""

import math

try:
    from autoortho import streaming_options
except ImportError:
    import streaming_options

NEAR_MIPS = 1
NEAR, MIDDLE, FAR = "near", "middle", "far"
_ORDER = {NEAR: 0, MIDDLE: 1, FAR: 2}
MOVING_SPEED_MPS = 10.0
# Faster than any aircraft between two position reports: a new flight or a
# teleport, which starts the flight again.
JUMP_SPEED_MPS = 400.0


def band_of(mip, base_mip):
    if mip <= NEAR_MIPS:
        return NEAR
    if mip >= base_mip:
        return FAR
    return MIDDLE


def nearer(a, b):
    """The nearer of two bands (the camera can only make a read nearer)."""
    return a if _ORDER[a] <= _ORDER[b] else b


def chunk_priority(band, mip):
    """Download priority of a live chunk (lower goes first), below prefetch's 100.

    Near reads first, then middle reads (finer levels first), then far ones.
    """
    if band == NEAR:
        return 0.0
    if band == MIDDLE:
        return 10.0 + max(0, int(mip))
    return 30.0


def wait_seconds(band, starting):
    if band == FAR:
        return max(0.0, streaming_options.number("far_wait_seconds"))
    if starting:
        return max(0.0, streaming_options.number("near_wait_seconds"))
    return max(0.0, streaming_options.number("band_max_wait_seconds"))


class StartTracker:
    """Whether a flight is starting: from the start until the aircraft first
    moves faster than taxiing, and again after a jump or while X-Plane reads
    near textures without sending positions (it is loading another place)."""

    def __init__(self):
        self.starting = True
        self._last = None  # (lat, lon, t)

    def update(self, connected, lat, lon, speed_mps, now, near_read=False):
        if not connected:
            if near_read:
                self.starting = True
            self._last = None
            return self.starting
        if self._last is not None:
            last_lat, last_lon, last_t = self._last
            dt = max(0.05, now - last_t)
            d_lat = (lat - last_lat) * 111320.0
            d_lon = (lon - last_lon) * 111320.0 * math.cos(math.radians(lat))
            if math.hypot(d_lat, d_lon) / dt > JUMP_SPEED_MPS:
                self.starting = True
        self._last = (lat, lon, now)
        if speed_mps is not None and speed_mps >= MOVING_SPEED_MPS:
            self.starting = False
        return self.starting

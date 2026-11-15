"""How long one of X-Plane's reads may wait, by how near the texture is.

* Near: X-Plane wants mipmap 0 or 1, so the texture is next to the camera. It
  waits for real imagery.
* Middle: a reduced level. It waits as long as near reads do.
* Far: the base level (one low-zoom chunk). It barely waits: it gets the
  best data at hand, and the real data is fetched behind it.

While a flight is starting (the loading screen and the time parked after
it), X-Plane does not read those textures again for a while, so near and
middle reads may wait much longer then (``near_wait_seconds``).

These bands are the only live wait policy. The per-chunk wait, the tile
budget and the startup extension (the retired ``maxwait``,
``tile_time_budget``, ``use_time_budget`` and ``suspend_maxwait``) are
derived from them below.
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


# The bands are the only wait policy: the values below replace the retired
# [autoortho] maxwait, suspend_maxwait, tile_time_budget and use_time_budget.

# A tile's budget grows this much while X-Plane loads the first flight
# (before it ever connected), as suspend_maxwait did.
INITIAL_LOAD_FACTOR = 10.0
INITIAL_LOAD_CAP_S = 1800.0


def chunk_wait_seconds(starting):
    """Longest wait for one row of chunks (was ``maxwait``).

    A near or middle read's own wait: the read's band budget bounds it, so a
    row may use what is left of it.
    """
    return wait_seconds(NEAR, starting)


def tile_budget_seconds(initial_load=False):
    """A tile's budget across all its reads (was ``tile_time_budget``).

    The longest any read may wait, so a band's wait is never cut short by
    the tile's own budget; much longer while the first flight loads.
    """
    budget = max(0.1, wait_seconds(NEAR, True), wait_seconds(NEAR, False))
    if initial_load:
        budget = min(budget * INITIAL_LOAD_FACTOR, INITIAL_LOAD_CAP_S)
    return budget


def http_read_timeout_seconds():
    """Read timeout of one HTTP/1.1 download: an in-flight read's wait plus 1 s."""
    return wait_seconds(NEAR, False) + 1.0


# How long X-Plane's imagery actually took when a near or middle read gave
# up waiting (OrthoStreamXP's "needed" times): counted from the start of the
# read until the level's chunks arrived. Upper bounds in seconds; a last
# bucket holds longer ones. The governor raises a wait only when most of
# these arrived soon after the wait ended.
LATE_BUCKETS_S = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 45, 60, 90, 120, 180)
LATE_GIVE_UP_S = LATE_BUCKETS_S[-1]


def late_bucket(seconds):
    """Index of the bucket for an arrival after ``seconds`` (last: longer)."""
    for index, bound in enumerate(LATE_BUCKETS_S):
        if seconds <= bound:
            return index
    return len(LATE_BUCKETS_S)


def late_stat_key(starting, index):
    return f"late_{'start' if starting else 'flight'}_b{index}"


def late_percentile(counts, share):
    """The bucket bound at ``share`` of the arrivals in ``counts``.

    ``counts`` has one entry per bucket plus the overflow. Returns
    ``(seconds, total)``; seconds is ``inf`` when the share falls in the
    overflow bucket and None without arrivals.
    """
    counts = [max(0, int(c or 0)) for c in (counts or ())]
    total = sum(counts)
    if total <= 0:
        return None, 0
    need = share * total
    running = 0
    for index, count in enumerate(counts):
        running += count
        if running >= need:
            if index < len(LATE_BUCKETS_S):
                return float(LATE_BUCKETS_S[index]), total
            return math.inf, total
    return math.inf, total


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

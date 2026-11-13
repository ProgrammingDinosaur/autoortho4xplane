"""What prefetch did for each tile, to explain blurry near reads.

Each mount worker keeps one :class:`PrefetchLedger`. The prefetch paths note
the tiles they queue (and to which level), the completion hooks note when a
tile's imagery is in, and the tile cacher notes evictions. When X-Plane then
reads a near tile blurry, :meth:`PrefetchLedger.classify_miss` says what
prefetch had done for it:

* ``never``: no prefetch path queued it in time (or at all);
* ``evicted``: it was complete at the wanted level, then left memory;
* ``coarse``: only a coarser level than X-Plane wanted was queued;
* ``late``: it was queued at the wanted level but not complete yet;
* ``done``: it was complete and still in memory; blurry for another reason.

The governor sums these per kind (``near_miss_*`` counters) and tunes the
lever that fits the dominant cause (see
``docs/developer/governor-behind-detection-plan.md``).

The module is pure: no getortho import, and every method takes the time, so
tests run without a tile cacher.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque, FrozenSet, Hashable, Optional, Tuple

# A queue this soon before the read reacted to X-Plane; it was not foresight.
MIN_LEAD_S = 2.0
# A tile at least this far off the track (as a share of the prefetch radius)
# lay outside the corridor.
WIDE_SHARE = 0.8
# A track change of more than TURN_DEG within TURN_WINDOW_S is a turn.
TURN_DEG = 30.0
TURN_WINDOW_S = 60.0
# A prefetch pause this recent can explain a tile that was never queued.
PAUSE_LOOKBACK_S = 60.0

MISS_KINDS = (
    "never_wide",
    "never_inside",
    "never_paused",
    "never_turn",
    "coarse",
    "late",
    "evicted",
    "done",
)
PAUSE_REASONS = ("ram", "live", "gate")

Key = Tuple[int, int, int]


def ledger_key(row, col, zoom) -> Key:
    """The ledger's key for a tile: (row, col, file zoom), without the maptype.

    DSF prefetch may rewrite the maptype while reads use the resolved one, so
    a key with it would split one tile into two entries.
    """
    return (int(row), int(col), int(zoom))


@dataclass
class LedgerEntry:
    first_queued: float                 # monotonic time of the first queue
    finest_mip: int                     # finest level any path queued (0 = full)
    sources: FrozenSet[str] = field(default_factory=frozenset)
    done_at: Optional[float] = None
    done_mip: Optional[int] = None      # finest level known complete
    evicted_at: Optional[float] = None
    last_seen: float = 0.0              # for the TTL


def _angle_diff(a, b) -> float:
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def cross_track_m(ground_m, bearing_to_tile_deg, track_deg) -> float:
    """Signed distance of a point from the line along the aircraft's track."""
    return float(ground_m) * math.sin(math.radians(float(bearing_to_tile_deg) - float(track_deg)))


class PrefetchLedger:
    """A bounded record of what prefetch queued and finished, per tile."""

    MAX_ENTRIES = 8192       # ~100 B each: under 1 MB
    TTL_S = 1200.0           # 20 min, as the graphics memory meter

    def __init__(self, max_entries: Optional[int] = None, ttl_s: Optional[float] = None):
        self.max_entries = int(max_entries or self.MAX_ENTRIES)
        self.ttl_s = float(ttl_s if ttl_s is not None else self.TTL_S)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Hashable, LedgerEntry]" = OrderedDict()
        self._track: Deque[Tuple[float, float]] = deque(maxlen=int(TURN_WINDOW_S) + 5)
        self._paused_at: Optional[float] = None
        self._paused_reason: str = ""

    # -- prefetch side ----------------------------------------------------

    def note_queued(self, key, mip, source, now) -> None:
        """A prefetch path queued ``key`` from level ``mip`` (0 = full detail)."""
        mip = max(0, int(mip))
        with self._lock:
            self._expire_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                entry = LedgerEntry(first_queued=now, finest_mip=mip,
                                    sources=frozenset((str(source),)), last_seen=now)
                self._entries[key] = entry
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
                return
            entry.first_queued = min(entry.first_queued, now)
            entry.finest_mip = min(entry.finest_mip, mip)
            if source not in entry.sources:
                entry.sources = entry.sources | {str(source)}
            entry.last_seen = now
            self._entries.move_to_end(key)

    def note_done(self, key, mip, now) -> None:
        """Level ``mip`` and the coarser ones of ``key`` are complete."""
        mip = max(0, int(mip))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            evicted_since = entry.evicted_at is not None and (
                entry.done_at is None or entry.evicted_at >= entry.done_at
            )
            if entry.done_mip is None or evicted_since:
                # Complete again after an eviction: only what is in now counts.
                entry.done_mip = mip
            else:
                entry.done_mip = min(entry.done_mip, mip)
            entry.done_at = now
            entry.last_seen = now
            self._entries.move_to_end(key)

    def note_evicted(self, key, now, done_mip: Optional[int] = None) -> None:
        """``key`` left memory. ``done_mip``: its finest complete level then, if known."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if done_mip is not None and entry.done_at is None:
                # Complete without a completion hook seeing it (no tracker).
                entry.done_mip = max(0, int(done_mip))
                entry.done_at = now
            entry.evicted_at = now
            entry.last_seen = now
            self._entries.move_to_end(key)

    def note_paused(self, now, reason) -> None:
        """A prefetch cycle was skipped (``reason``: ram, live or gate)."""
        with self._lock:
            self._paused_at = now
            self._paused_reason = str(reason)

    def paused_reason(self, now, lookback_s: float = PAUSE_LOOKBACK_S) -> str:
        """Why prefetch was last paused, if within ``lookback_s``; else ''."""
        with self._lock:
            if self._paused_at is None or now - self._paused_at > lookback_s:
                return ""
            return self._paused_reason or "gate"

    def note_track(self, now, track_deg) -> None:
        """The aircraft's track; kept at most once a second for turn detection."""
        if track_deg is None:
            return
        with self._lock:
            if self._track and now - self._track[-1][0] < 1.0:
                return
            self._track.append((now, float(track_deg) % 360.0))

    def turning(self, now) -> bool:
        """The track changed by more than TURN_DEG in the last TURN_WINDOW_S."""
        with self._lock:
            recent = [deg for t, deg in self._track if now - t <= TURN_WINDOW_S]
        if len(recent) < 2:
            return False
        latest = recent[-1]
        return max(_angle_diff(deg, latest) for deg in recent) > TURN_DEG

    # -- read side --------------------------------------------------------

    def get(self, key) -> Optional[LedgerEntry]:
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else LedgerEntry(**vars(entry))

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def classify_miss(self, key, wanted_mip, now) -> str:
        """What prefetch had done for a tile X-Plane just read blurry.

        Returns ``never``, ``evicted``, ``coarse``, ``late`` or ``done``
        (checked in that order).
        """
        wanted = max(0, int(wanted_mip))
        with self._lock:
            self._expire_locked(now)
            entry = self._entries.get(key)
            if entry is None or now - entry.first_queued < MIN_LEAD_S:
                return "never"
            done = entry.done_at is not None and entry.done_mip is not None \
                and entry.done_mip <= wanted
            if done and entry.evicted_at is not None and entry.evicted_at >= entry.done_at:
                return "evicted"
            if entry.finest_mip > wanted:
                return "coarse"
            if not done:
                return "late"
            return "done"

    def never_kind(self, now, *, cross_track_m=None, radius_m=None) -> str:
        """Which ``never_*`` counter a never-queued miss goes to.

        A recent pause comes first (prefetch did not run), then a turn (the
        straight corridor could not cover it), then the geometry: outside
        WIDE_SHARE of the radius is ``wide``, else ``inside``.
        """
        if self.paused_reason(now):
            return "never_paused"
        if self.turning(now):
            return "never_turn"
        if cross_track_m is not None and radius_m:
            if abs(cross_track_m) > WIDE_SHARE * radius_m:
                return "never_wide"
        return "never_inside"

    # -- housekeeping -----------------------------------------------------

    def _expire_locked(self, now) -> None:
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if now - entry.last_seen <= self.ttl_s:
                break
            self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._track.clear()
            self._paused_at = None
            self._paused_reason = ""

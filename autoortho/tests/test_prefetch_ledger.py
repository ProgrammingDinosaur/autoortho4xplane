"""The prefetch ledger: what prefetch did for a tile X-Plane read blurry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prefetch_ledger as pl  # noqa: E402
from prefetch_ledger import PrefetchLedger, ledger_key  # noqa: E402

KEY = ledger_key(100, 200, 16)


def test_never_queued_is_never():
    ledger = PrefetchLedger()
    assert ledger.classify_miss(KEY, 0, now=10.0) == "never"


def test_queued_too_close_to_the_read_is_never():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 0, "velocity", now=10.0)
    assert ledger.classify_miss(KEY, 0, now=10.0 + pl.MIN_LEAD_S - 0.1) == "never"
    assert ledger.classify_miss(KEY, 0, now=10.0 + pl.MIN_LEAD_S + 0.1) == "late"


def test_queued_not_done_is_late():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 0, "velocity", now=0.0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "late"
    # Done only to a coarser level than wanted: still late.
    ledger.note_done(KEY, 2, now=20.0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "late"


def test_only_a_coarser_level_queued_is_coarse():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 3, "paging", now=0.0)
    assert ledger.classify_miss(KEY, 1, now=30.0) == "coarse"
    # X-Plane wanted the queued level: it is late, not coarse.
    assert ledger.classify_miss(KEY, 3, now=30.0) == "late"


def test_done_and_in_memory_is_done():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 0, "velocity", now=0.0)
    ledger.note_done(KEY, 0, now=10.0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "done"
    assert ledger.classify_miss(KEY, 2, now=30.0) == "done"


def test_done_then_evicted_is_evicted():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 0, "velocity", now=0.0)
    ledger.note_done(KEY, 0, now=10.0)
    ledger.note_evicted(KEY, now=20.0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "evicted"
    # Complete again after the eviction: in memory.
    ledger.note_done(KEY, 0, now=25.0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "done"


def test_eviction_records_completion_the_hooks_missed():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 0, "velocity", now=0.0)
    ledger.note_evicted(KEY, now=20.0, done_mip=0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "evicted"


def test_evicted_before_done_is_late():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 0, "velocity", now=0.0)
    ledger.note_evicted(KEY, now=20.0)
    assert ledger.classify_miss(KEY, 0, now=30.0) == "late"


def test_finest_mip_and_earliest_time_merge():
    ledger = PrefetchLedger()
    ledger.note_queued(KEY, 3, "paging", now=5.0)
    ledger.note_queued(KEY, 0, "velocity", now=9.0)
    ledger.note_queued(KEY, 2, "ter", now=1.0)
    entry = ledger.get(KEY)
    assert entry.finest_mip == 0
    assert entry.first_queued == 1.0
    assert entry.sources == {"paging", "velocity", "ter"}


def test_unknown_keys_ignore_done_and_evict():
    ledger = PrefetchLedger()
    ledger.note_done(KEY, 0, now=1.0)
    ledger.note_evicted(KEY, now=2.0)
    assert KEY not in ledger
    assert ledger.classify_miss(KEY, 0, now=30.0) == "never"


def test_lru_bound():
    ledger = PrefetchLedger(max_entries=3)
    for i in range(5):
        ledger.note_queued(ledger_key(i, 0, 16), 0, "velocity", now=float(i))
    assert len(ledger) == 3
    assert ledger_key(0, 0, 16) not in ledger
    assert ledger_key(4, 0, 16) in ledger
    # A touched entry is kept over older ones.
    ledger.note_queued(ledger_key(2, 0, 16), 0, "dsf", now=5.0)
    ledger.note_queued(ledger_key(9, 0, 16), 0, "velocity", now=6.0)
    assert ledger_key(2, 0, 16) in ledger
    assert ledger_key(3, 0, 16) not in ledger


def test_ttl_expiry():
    ledger = PrefetchLedger(ttl_s=100.0)
    ledger.note_queued(KEY, 0, "velocity", now=0.0)
    assert ledger.classify_miss(KEY, 0, now=50.0) == "late"
    assert ledger.classify_miss(KEY, 0, now=150.0) == "never"
    assert KEY not in ledger


def test_turn_detection():
    ledger = PrefetchLedger()
    for i in range(20):
        ledger.note_track(float(i), 90.0)
    assert not ledger.turning(19.0)
    for i in range(20, 40):
        ledger.note_track(float(i), 90.0 + (i - 19) * 3)   # 60 degrees
    assert ledger.turning(39.0)
    # Long after the turn the old track has aged out.
    for i in range(40, 120):
        ledger.note_track(float(i), 150.0)
    assert not ledger.turning(119.0)


def test_turn_across_north():
    ledger = PrefetchLedger()
    ledger.note_track(0.0, 350.0)
    ledger.note_track(5.0, 10.0)
    assert not ledger.turning(5.0)       # 20 degrees, not 340
    ledger.note_track(10.0, 30.0)
    assert ledger.turning(10.0)


def test_track_sampled_at_most_once_a_second():
    ledger = PrefetchLedger()
    ledger.note_track(0.0, 0.0)
    ledger.note_track(0.5, 90.0)
    assert not ledger.turning(0.5)


def test_never_kind_order():
    ledger = PrefetchLedger()
    radius = 10 * 1852.0
    assert ledger.never_kind(0.0, cross_track_m=0.9 * radius, radius_m=radius) == "never_wide"
    assert ledger.never_kind(0.0, cross_track_m=-0.9 * radius, radius_m=radius) == "never_wide"
    assert ledger.never_kind(0.0, cross_track_m=0.5 * radius, radius_m=radius) == "never_inside"
    assert ledger.never_kind(0.0) == "never_inside"
    ledger.note_track(0.0, 0.0)
    ledger.note_track(5.0, 45.0)
    assert ledger.never_kind(5.0, cross_track_m=0.9 * radius, radius_m=radius) == "never_turn"
    ledger.note_paused(5.0, "ram")
    assert ledger.never_kind(5.0) == "never_paused"
    assert ledger.paused_reason(5.0) == "ram"
    assert ledger.paused_reason(5.0 + pl.PAUSE_LOOKBACK_S + 1) == ""


def test_cross_track():
    assert pl.cross_track_m(1000.0, 90.0, 0.0) == 1000.0
    assert abs(pl.cross_track_m(1000.0, 0.0, 0.0)) < 1e-9
    assert pl.cross_track_m(1000.0, 270.0, 0.0) == -1000.0

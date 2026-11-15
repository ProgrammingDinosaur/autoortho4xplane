"""The emergency brake for very low free RAM."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ram_brake import GB, RELEASE_AFTER_S, RamBrake  # noqa: E402

TOTAL = 16 * GB


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _brake(free, **kwargs):
    clock = _Clock()
    state = {"free": free}
    events = []
    brake = RamBrake(
        reader=lambda: (state["free"], TOTAL),
        clock=clock,
        on_change=lambda engaged, *_: events.append(engaged),
        **kwargs,
    )
    return brake, clock, state, events


def test_thresholds_scale_with_ram():
    assert RamBrake.thresholds(16 * GB) == (int(1.5 * GB), int(2.5 * GB))
    engage, release = RamBrake.thresholds(64 * GB)
    assert engage == int(64 * GB * 0.08)
    assert release == int(64 * GB * 0.12)


def test_engages_below_threshold_and_releases_after_recovery():
    brake, clock, state, events = _brake(8 * GB)
    assert brake.engaged is False

    state["free"] = 1 * GB
    clock.advance(1)
    assert brake.engaged is True
    assert brake.activations == 1
    assert events == [True]

    # Between the thresholds: still braking.
    state["free"] = 2 * GB
    clock.advance(30)
    assert brake.engaged is True

    # Above release, but not long enough.
    state["free"] = 4 * GB
    clock.advance(1)
    assert brake.engaged is True
    clock.advance(RELEASE_AFTER_S - 2)
    assert brake.engaged is True
    clock.advance(2)
    assert brake.engaged is False
    assert events == [True, False]


def test_dip_during_recovery_restarts_the_wait():
    brake, clock, state, _events = _brake(1 * GB)
    assert brake.sample(force=True) is True
    state["free"] = 4 * GB
    clock.advance(1)
    brake.sample()
    clock.advance(RELEASE_AFTER_S - 1)
    state["free"] = 2 * GB
    brake.sample()
    state["free"] = 4 * GB
    clock.advance(1)
    brake.sample()
    clock.advance(RELEASE_AFTER_S - 1)
    assert brake.sample() is True


def test_samples_at_most_once_a_second():
    calls = []
    clock = _Clock()
    brake = RamBrake(
        reader=lambda: calls.append(1) or (8 * GB, TOTAL), clock=clock
    )
    brake.sample()
    brake.sample()
    clock.advance(0.5)
    brake.sample()
    assert len(calls) == 1
    clock.advance(0.6)
    brake.sample()
    assert len(calls) == 2


def test_disabled_never_engages():
    brake, _clock, _state, _events = _brake(100, enabled=False)
    assert brake.engaged is False
    assert brake.cache_limit(4 * GB) == 4 * GB


def test_cache_limit_halves_while_engaged():
    brake, _clock, _state, _events = _brake(1 * GB)
    assert brake.cache_limit(4 * GB) == 2 * GB
    assert brake.cache_limit(512 * 1024 * 1024) == 512 * 1024 * 1024


def test_reader_failure_keeps_state():
    def broken():
        raise OSError("no psutil")

    brake = RamBrake(reader=broken)
    assert brake.sample(force=True) is False

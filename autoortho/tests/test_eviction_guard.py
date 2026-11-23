"""Memory eviction that frees nothing pauses (eviction_guard.py)."""

import sys
from pathlib import Path

AUTOORTHO_DIR = Path(__file__).resolve().parents[1]
if str(AUTOORTHO_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOORTHO_DIR))

import eviction_guard as eg  # noqa: E402
from eviction_guard import EvictionStallGuard  # noqa: E402

MB = eg.MB


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _guard():
    clock = Clock()
    metrics = []
    return EvictionStallGuard(clock=clock, metric=metrics.append), clock, metrics


def _stall(guard, mem=3700 * MB):
    for _ in range(eg.STALL_PASSES):
        guard.note_pass(0, mem)


def test_allows_eviction_until_passes_stall():
    guard, _clock, metrics = _guard()
    for _ in range(eg.STALL_PASSES - 1):
        guard.note_pass(0, 3700 * MB)
        assert guard.allow(3700 * MB)
    guard.note_pass(0, 3700 * MB)
    assert guard.paused
    assert not guard.allow(3700 * MB)
    assert metrics == ["evict_stalled"]


def test_a_pass_that_frees_memory_resets_the_count():
    guard, _clock, _metrics = _guard()
    guard.note_pass(0, 3700 * MB)
    guard.note_pass(0, 3700 * MB)
    guard.note_pass(200 * MB, 3500 * MB)
    guard.note_pass(0, 3500 * MB)
    assert guard.allow(3500 * MB)


def test_resumes_after_the_back_off():
    guard, clock, _metrics = _guard()
    _stall(guard)
    clock.now += eg.BASE_BACKOFF_S - 1
    assert not guard.allow(3700 * MB)
    clock.now += 2
    assert guard.allow(3700 * MB)
    assert not guard.paused


def test_resumes_early_when_memory_grows():
    guard, _clock, _metrics = _guard()
    _stall(guard, 3700 * MB)
    assert not guard.allow(3800 * MB)            # under 5 % more
    assert guard.allow(int(3700 * MB * 1.06))    # real growth


def test_back_off_doubles_while_it_keeps_stalling_and_is_capped():
    guard, clock, _metrics = _guard()
    waits = []
    for _ in range(6):
        _stall(guard)
        start = clock.now
        while not guard.allow(3700 * MB):
            clock.now += 1
        waits.append(clock.now - start)
    assert waits[0] == eg.BASE_BACKOFF_S
    assert waits[1] == 2 * eg.BASE_BACKOFF_S
    assert max(waits) == eg.MAX_BACKOFF_S


def test_freeing_memory_resets_the_back_off():
    guard, clock, _metrics = _guard()
    _stall(guard)
    clock.now += eg.BASE_BACKOFF_S
    assert guard.allow(3700 * MB)
    guard.note_pass(100 * MB, 3600 * MB)
    _stall(guard)
    clock.now += eg.BASE_BACKOFF_S
    assert guard.allow(3600 * MB)


def test_warns_once_then_logs_quietly(caplog):
    guard, clock, _metrics = _guard()
    with caplog.at_level("INFO", logger="eviction_guard"):
        _stall(guard)
        clock.now += 10_000
        guard.allow(3700 * MB)
        _stall(guard)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "cache_mem_limit" in warnings[0].getMessage()

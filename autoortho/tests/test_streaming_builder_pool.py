"""
test_streaming_builder_pool.py - StreamingBuilderPool waiting and lifecycle

Runs the real pool against a fake native layer that only tracks handles, so
no native library is loaded: waiting for a builder, closing the pool with
builders still out, and builders whose native reset or creation fails.
"""

import os
import sys
import threading
import time

import pytest

# Add repository root to path for package-style imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from autoortho.aopipeline.AoDDS import StreamingBuilderPool  # noqa: E402

CONFIG = {"chunks_per_side": 2}


class FakeNative:
    """Stands in for the native library: hands out and tracks handles."""

    def __init__(self):
        self._next = 1000
        self.alive = set()
        self.destroyed = []
        self.fail_reset = False
        self.fail_create = False
        self.create_calls = 0

    def aodds_builder_create(self, *_args):
        self.create_calls += 1
        if self.fail_create:
            return None
        self._next += 1
        self.alive.add(self._next)
        return self._next

    def aodds_builder_reset(self, handle, *_args):
        if self.fail_reset:
            raise OSError("simulated native reset failure")
        assert handle in self.alive, f"reset of a destroyed builder {handle}"

    def aodds_builder_destroy(self, handle):
        assert handle in self.alive, f"double destroy of {handle}"
        self.alive.remove(handle)
        self.destroyed.append(handle)


@pytest.fixture
def pool_and_native():
    pool = StreamingBuilderPool(pool_size=1)
    native = FakeNative()
    pool._lib = native
    pool._initialized = True
    yield pool, native
    pool.close()


def _acquire_in_thread(pool, timeout):
    """Start an acquire on another thread; returns (result list, done event)."""
    result = []
    done = threading.Event()

    def run():
        try:
            result.append(pool.acquire(CONFIG, timeout=timeout))
        except Exception as exc:  # surfaced through the result
            result.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return result, done


def test_returned_builder_is_reused(pool_and_native):
    pool, native = pool_and_native
    first = pool.acquire(CONFIG)
    handle = first._handle
    first.release()

    second = pool.acquire(CONFIG)

    assert second._handle == handle
    assert native.create_calls == 1
    second.release()


def test_release_wakes_a_waiting_thread(pool_and_native):
    pool, _native = pool_and_native
    # With the periodic look this far out, only the release's own wake-up
    # can hand the builder over in time.
    pool._WAIT_SLICE_S = 30.0
    held = pool.acquire(CONFIG)
    result, done = _acquire_in_thread(pool, timeout=30)
    time.sleep(0.05)
    assert not done.is_set()

    held.release()

    assert done.wait(2), "the waiting thread was not woken by the release"
    assert result[0] is not None and not isinstance(result[0], Exception)
    result[0].release()


def test_growing_the_pool_wakes_a_waiting_thread(pool_and_native):
    pool, native = pool_and_native
    pool._WAIT_SLICE_S = 30.0
    held = pool.acquire(CONFIG)
    result, done = _acquire_in_thread(pool, timeout=30)
    time.sleep(0.05)

    pool.resize(2)

    assert done.wait(2), "the waiting thread was not woken by the resize"
    assert len(native.alive) == 2
    result[0].release()
    held.release()


def test_missed_wakeup_costs_one_wait_slice_at_most(pool_and_native):
    pool, _native = pool_and_native
    pool._WAIT_SLICE_S = 0.05
    held = pool.acquire(CONFIG)
    result, done = _acquire_in_thread(pool, timeout=30)
    time.sleep(0.05)

    # Hand the builder back behind the pool's back: no wake-up is sent.
    with pool._lock:
        pool._in_use.clear()
        pool._available.append(held._handle)
    held._released = True

    assert done.wait(2), "the waiting thread never looked at the pool again"
    result[0].release()


def test_acquire_times_out_when_no_builder_comes_back(pool_and_native):
    pool, _native = pool_and_native
    held = pool.acquire(CONFIG)
    started = time.monotonic()

    assert pool.acquire(CONFIG, timeout=0.15) is None

    assert 0.14 <= time.monotonic() - started < 1.0
    held.release()


def test_close_wakes_waiting_threads_with_none(pool_and_native):
    pool, native = pool_and_native
    pool._WAIT_SLICE_S = 30.0
    held = pool.acquire(CONFIG)
    result, done = _acquire_in_thread(pool, timeout=30)
    time.sleep(0.05)

    pool.close()

    assert done.wait(2), "the waiting thread was not woken by the close"
    assert result == [None]
    held.release()
    assert not native.alive


def test_builder_released_after_close_is_destroyed(pool_and_native):
    pool, native = pool_and_native
    builder = pool.acquire(CONFIG)

    pool.close()
    assert len(native.alive) == 1

    builder.release()

    assert not native.alive
    assert pool.available_count == 0
    assert pool._total_created == 0


def test_closed_pool_hands_out_nothing(pool_and_native):
    pool, native = pool_and_native
    pool.acquire(CONFIG).release()

    pool.close()
    pool.close()  # a second close is harmless

    assert pool.acquire(CONFIG, timeout=0.05) is None
    assert pool.resize(4) == 1
    assert not native.alive
    assert len(native.destroyed) == 1


def test_failed_reset_on_checkout_frees_the_slot(pool_and_native):
    pool, native = pool_and_native
    pool.acquire(CONFIG).release()
    native.fail_reset = True

    with pytest.raises(OSError, match="reset failure"):
        pool.acquire(CONFIG)

    assert not native.alive
    assert pool._total_created == 0
    assert not pool._in_use
    native.fail_reset = False
    replacement = pool.acquire(CONFIG, timeout=0.5)
    assert replacement is not None
    replacement.release()


def test_failed_reset_on_release_discards_the_builder(pool_and_native):
    pool, native = pool_and_native
    builder = pool.acquire(CONFIG)
    handle = builder._handle
    native.fail_reset = True

    builder.release()

    assert native.destroyed == [handle]
    assert pool.available_count == 0
    native.fail_reset = False
    replacement = pool.acquire(CONFIG, timeout=0.5)
    assert replacement is not None and replacement._handle != handle
    replacement.release()


def test_failed_creation_is_retried_sparingly(pool_and_native):
    pool, native = pool_and_native
    native.fail_create = True
    started = time.monotonic()

    assert pool.acquire(CONFIG, timeout=0.2) is None

    assert time.monotonic() - started >= 0.19
    # 50 ms between attempts; the old loop made about one per millisecond.
    assert 2 <= native.create_calls <= 8


def test_many_threads_share_a_small_pool():
    pool = StreamingBuilderPool(pool_size=2)
    native = FakeNative()
    pool._lib = native
    pool._initialized = True
    guard = threading.Lock()
    state = {"held": 0, "most_held": 0, "builds": 0, "failures": []}

    def worker():
        for _ in range(40):
            try:
                builder = pool.acquire(CONFIG, timeout=10)
            except Exception as exc:
                state["failures"].append(repr(exc))
                return
            if builder is None:
                state["failures"].append("timeout")
                return
            with guard:
                state["held"] += 1
                state["most_held"] = max(state["most_held"], state["held"])
            time.sleep(0.0005)
            with guard:
                state["held"] -= 1
                state["builds"] += 1
            builder.release()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not state["failures"]
    assert state["builds"] == 8 * 40
    assert state["most_held"] <= 2
    assert native.create_calls <= 2
    pool.close()
    assert not native.alive

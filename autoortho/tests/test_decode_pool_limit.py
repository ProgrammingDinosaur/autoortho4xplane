"""A decode pool at its memory limit must not block a caller forever.

Builds hold decode buffers while they wait for more, and the threads that
would release buffers can be queued behind them at the Python build gate.
With an unbounded wait at the limit, the mount worker deadlocked and X-Plane
froze mid-flight. Acquire now waits a bounded time, then goes over the limit.
"""
import os
import sys
import threading
import time
from ctypes import CDLL

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

CHUNK_RGBA_BYTES = 256 * 256 * 4


def _library():
    from aopipeline import AoDecode

    # AO_TEST_AOPIPELINE_LIB lets a freshly built library be tested before it
    # replaces the checked-in one (which a running AutoOrtho keeps locked).
    path = os.environ.get("AO_TEST_AOPIPELINE_LIB")
    if path:
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.dirname(AoDecode._get_lib_path()))
        lib = CDLL(path)
        AoDecode._setup_signatures(lib)
        return lib
    try:
        return AoDecode._load_library()
    except Exception as exc:  # pragma: no cover - platform without the library
        pytest.skip(f"native decode library unavailable: {exc}")


def test_acquire_at_the_limit_returns_instead_of_blocking():
    lib = _library()
    # Two fixed buffers and a limit that leaves no room for overflow.
    pool = lib.aodecode_create_pool_ex(2, 2 * CHUNK_RGBA_BYTES)
    assert pool
    held = [lib.aodecode_acquire_buffer(pool) for _ in range(2)]
    assert all(held)

    result = {}

    def acquire():
        started = time.monotonic()
        result["buffer"] = lib.aodecode_acquire_buffer(pool)
        result["seconds"] = time.monotonic() - started

    worker = threading.Thread(target=acquire, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    blocked = worker.is_alive()
    if blocked:
        # Old library: free a buffer so the thread can finish before failing.
        lib.aodecode_release_buffer(pool, held.pop())
        worker.join(timeout=5.0)
    try:
        assert not blocked, "acquire blocked at the pool's memory limit"
        assert result["buffer"], "acquire returned NULL past the limit"
        # It still waits briefly first: the limit is back-pressure.
        assert 0.3 <= result["seconds"] < 3.0, result["seconds"]
    finally:
        for buffer in held:
            lib.aodecode_release_buffer(pool, buffer)
        if result.get("buffer"):
            lib.aodecode_release_buffer(pool, result["buffer"])
        lib.aodecode_destroy_pool(pool)


def test_released_buffer_wakes_a_waiting_acquire():
    lib = _library()
    pool = lib.aodecode_create_pool_ex(1, CHUNK_RGBA_BYTES)
    assert pool
    first = lib.aodecode_acquire_buffer(pool)
    result = {}

    def acquire():
        started = time.monotonic()
        result["buffer"] = lib.aodecode_acquire_buffer(pool)
        result["seconds"] = time.monotonic() - started

    worker = threading.Thread(target=acquire, daemon=True)
    worker.start()
    time.sleep(0.05)
    lib.aodecode_release_buffer(pool, first)
    worker.join(timeout=5.0)
    try:
        assert not worker.is_alive()
        assert result["buffer"]
        # Woken by the release, well before the bounded wait runs out.
        assert result["seconds"] < 0.4, result["seconds"]
    finally:
        if result.get("buffer"):
            lib.aodecode_release_buffer(pool, result["buffer"])
        lib.aodecode_destroy_pool(pool)

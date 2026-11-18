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
        if hasattr(lib, "aodecode_pool_over_limit_total"):
            # Counted, so a flight shows when the limit is too low.
            assert lib.aodecode_pool_over_limit_total(pool) == 1
    finally:
        for buffer in held:
            lib.aodecode_release_buffer(pool, buffer)
        if result.get("buffer"):
            lib.aodecode_release_buffer(pool, result["buffer"])
        lib.aodecode_destroy_pool(pool)


def test_allocations_within_the_limit_are_not_counted_over_it():
    lib = _library()
    if not hasattr(lib, "aodecode_pool_over_limit_total"):
        pytest.skip("library predates the over-limit counter")
    pool = lib.aodecode_create_pool_ex(1, 3 * CHUNK_RGBA_BYTES)
    held = [lib.aodecode_acquire_buffer(pool) for _ in range(3)]
    try:
        assert all(held)
        assert lib.aodecode_pool_over_limit_total(pool) == 0
    finally:
        for buffer in held:
            lib.aodecode_release_buffer(pool, buffer)
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


def _overflow_count(lib, pool):
    from ctypes import byref, c_int32, c_int64

    total, available, acquired, count = (c_int32() for _ in range(4))
    overflow_bytes, limit = c_int64(), c_int64()
    lib.aodecode_pool_stats_ex(
        pool, byref(total), byref(available), byref(acquired),
        byref(count), byref(overflow_bytes), byref(limit),
    )
    return count.value, overflow_bytes.value


def test_freed_overflow_images_leave_the_pool_count():
    # Decoded images in overflow buffers were freed straight to the heap, so
    # the pool kept counting them: in a flight the count grew by 768 with each
    # streaming build until the pool looked full for good.
    from ctypes import POINTER, byref, c_uint8, cast

    from aopipeline import AoDecode

    lib = _library()
    jpeg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "testfiles",
        "test_tile_small.jpg",
    )
    with open(jpeg_path, "rb") as handle:
        jpeg = handle.read()
    data = (c_uint8 * len(jpeg)).from_buffer_copy(jpeg)
    pool = lib.aodecode_create_pool_ex(2, 0)
    images = []
    try:
        for _ in range(5):  # two fixed buffers, three overflow
            image = AoDecode.DecodedImage()
            assert lib.aodecode_single(
                cast(data, POINTER(c_uint8)), len(jpeg), byref(image), pool
            )
            images.append(image)
        assert _overflow_count(lib, pool)[0] == 3
    finally:
        for image in images:
            lib.aodecode_free_image(byref(image), pool)
    try:
        assert _overflow_count(lib, pool) == (0, 0)
    finally:
        lib.aodecode_destroy_pool(pool)

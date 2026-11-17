"""Native builds must not leave an OpenMP team behind on every calling thread.

libgomp keeps a team of worker threads for each thread that ran a parallel
region, until that thread exits. Builds run on many long-lived threads (FUSE
readers, build and prefetch workers), so the mount worker grew from about
160 threads to 634 while a flight loaded and stayed near 470.
``AoDDS.release_thread_team`` frees the calling thread's team.
"""
import os
import sys
import threading
from ctypes import CDLL

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

psutil = pytest.importorskip("psutil")

CALLERS = 12
TEAM = 4
JPEG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "testfiles",
    "test_tile_small.jpg",
)


@pytest.fixture
def aodds(monkeypatch):
    from aopipeline import AoDDS

    # AO_TEST_AOPIPELINE_LIB lets a freshly built library be tested before it
    # replaces the checked-in one (which a running AutoOrtho keeps locked).
    path = os.environ.get("AO_TEST_AOPIPELINE_LIB")
    if path:
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.dirname(AoDDS._get_lib_path()))
        lib = CDLL(path)
        AoDDS._setup_signatures(lib)
        lib.aodds_init_ispc()
        monkeypatch.setattr(AoDDS, "_aodds", lib)
    try:
        lib = AoDDS._load_library()
    except Exception as exc:  # pragma: no cover - platform without the library
        pytest.skip(f"native library unavailable: {exc}")
    if not hasattr(lib, "aodds_build_partial_mipmap_mt"):
        pytest.skip("library cannot bound a build's threads")
    return AoDDS


def _build_on_threads(aodds, release):
    """Extra threads while CALLERS threads that each ran a build stay alive."""
    baseline = psutil.Process().num_threads()
    with open(JPEG_PATH, "rb") as handle:
        jpeg = handle.read()
    started = threading.Barrier(CALLERS + 1)
    finish = threading.Event()
    results = []

    def caller():
        result = aodds.build_partial_mipmap(
            [jpeg] * 8, 8, 1, max_threads=TEAM
        )
        results.append(result.success)
        if release:
            aodds.release_thread_team()
        started.wait()
        finish.wait(10)  # stay alive, as FUSE and worker threads do

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(CALLERS)]
    for thread in threads:
        thread.start()
    started.wait(30)
    count = psutil.Process().num_threads()
    finish.set()
    for thread in threads:
        thread.join(10)
    assert results == [True] * CALLERS
    return count - baseline


def test_released_teams_do_not_stay_with_their_threads(aodds):
    if not aodds.release_thread_team():
        pytest.skip("this OpenMP runtime cannot release one thread's team")
    # Released first: on Windows the kept teams outlive their threads.
    released = _build_on_threads(aodds, release=True)
    kept = _build_on_threads(aodds, release=False)
    # Without the release every caller keeps TEAM - 1 workers.
    assert kept >= CALLERS + CALLERS * (TEAM - 1) // 2, (kept, released)
    # With it only the callers themselves remain (plus slack).
    assert released <= CALLERS + TEAM, (kept, released)

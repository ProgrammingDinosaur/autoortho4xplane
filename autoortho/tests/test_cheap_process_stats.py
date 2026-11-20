"""Process stats that run every second must not use psutil's costly calls."""
import sys

import psutil
import pytest

from autoortho import aostats, getortho


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_private_working_set_matches_uss():
    # Allocate and touch memory so the figure is well above noise.
    block = bytearray(64 * 2**20)
    for i in range(0, len(block), 4096):
        block[i] = 1
    fast = aostats._get_windows_private_working_set()
    uss = psutil.Process().memory_full_info().uss
    assert fast > 0
    assert abs(fast - uss) < max(16 * 2**20, uss * 0.05)


def test_private_working_set_is_zero_off_windows(monkeypatch):
    monkeypatch.setattr(aostats.sys, "platform", "linux")
    assert aostats._get_windows_private_working_set() == 0


def test_thread_count_is_cached_between_refreshes(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr(aostats.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(aostats, "_thread_count", (0.0, 0))

    class Proc:
        calls = 0

        def num_threads(self):
            Proc.calls += 1
            return 3

    proc = Proc()
    assert aostats._cached_thread_count(proc) == 3
    clock[0] += 1.0
    assert aostats._cached_thread_count(proc) == 3
    assert Proc.calls == 1
    clock[0] += aostats._THREAD_COUNT_SECONDS
    aostats._cached_thread_count(proc)
    assert Proc.calls == 2


def test_cache_write_creates_its_directory_once(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "_known_write_dirs", set())
    calls = []
    real_makedirs = getortho.os.makedirs

    def counting_makedirs(path, *args, **kwargs):
        calls.append(path)
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(getortho.os, "makedirs", counting_makedirs)
    folder = tmp_path / "16" / "100"
    for i in range(5):
        assert getortho._atomic_write(str(folder / f"{i}.jpg"), b"x" * 10)
    # (os.makedirs calls itself for the missing parent too)
    assert calls.count(str(folder)) == 1
    assert sorted(p.name for p in folder.iterdir()) == [f"{i}.jpg" for i in range(5)]


def test_cache_write_recreates_a_directory_removed_after_its_first_write(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(getortho, "_known_write_dirs", set())
    folder = tmp_path / "16" / "100"
    assert getortho._atomic_write(str(folder / "a.jpg"), b"a")
    shutil.rmtree(tmp_path / "16")  # e.g. a cache cleanup
    assert getortho._atomic_write(str(folder / "b.jpg"), b"b")
    assert (folder / "b.jpg").read_bytes() == b"b"

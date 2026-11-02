"""Regressions for telemetry loss, native CPU admission, and FUSE shutdown."""
import concurrent.futures
import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoortho import aostats, getortho


@pytest.fixture
def fuse_module(monkeypatch):
    # These are callback tests, not driver integration tests. Loading the
    # locally installed FUSE dylib without mounting also crashes CFRelease
    # in its process-exit destructor on some macOS installations.
    stub = SimpleNamespace(
        FUSE=object, Operations=object, FuseOSError=OSError,
        fuse_get_context=lambda: (0, 0, 0), _libfuse=SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "autoortho.mfusepy", stub)
    path = Path(__file__).resolve().parents[1] / "autoortho_fuse.py"
    spec = importlib.util.spec_from_file_location("_test_fuse_stability", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stats(monkeypatch):
    monkeypatch.setattr(aostats, "_store", None)
    monkeypatch.setattr(aostats, "_manager", None)
    monkeypatch.setattr(aostats, "_local_stats", {})
    monkeypatch.setattr(aostats.STATS, "_remote", None)
    return aostats


@pytest.mark.parametrize("error", [EOFError, BrokenPipeError, ConnectionResetError])
@pytest.mark.parametrize("operation", ["get", "set", "inc", "many", "delete", "items", "iter", "len"])
def test_stats_manager_loss_falls_back_to_local_store(stats, error, operation):
    class DeadStore:
        def __getattr__(self, name):
            def fail(*args):
                raise error("manager disconnected")
            return fail
    stats.bind_local_store(DeadStore())
    operations = {
        "get": lambda: stats.get_stat("flight_state"),
        "set": lambda: stats.set_stat("count", 1),
        "inc": lambda: stats.inc_stat("count"),
        "many": lambda: stats.inc_many({"count": 1}),
        "delete": lambda: stats.delete_stat("count"),
        "items": lambda: list(stats.STATS.items()),
        "iter": lambda: list(stats.STATS),
        "len": lambda: len(stats.STATS),
    }
    operations[operation]()
    assert stats._store is None
    assert stats.STATS._remote is None
    stats.set_stat("count", 4)
    assert stats.inc_stat("count", 2) == 6
    stats.inc_many({"count": 3})
    assert stats.STATS["count"] == 9
    stats.delete_stat("count")
    assert stats.get_stat("count") == 0


def test_stats_rebinding_preserves_imported_references(stats):
    reference = stats.STATS
    first, second = stats.StatsStore(), stats.StatsStore()
    stats.bind_local_store(first)
    stats.set_stat("count", 10)
    stats.bind_local_store(second)
    assert stats.STATS is reference
    assert reference.get("count", 0) == 0
    reference["count"] = 20
    assert second.get("count") == 20
    assert first.get("count") == 10


def test_lost_manager_disables_speculative_flight_work(stats, monkeypatch):
    class DeadStore:
        def get(self, *args):
            raise EOFError("manager disconnected")
    stats.bind_local_store(DeadStore())
    monkeypatch.setattr(getortho, "get_stat", stats.get_stat)
    monkeypatch.setattr(getortho, "_shared_flight_state_last_poll", 0.0)
    monkeypatch.setattr(getortho, "_shared_flight_state_allowed", True)
    monkeypatch.setattr(getortho.datareftracker, "running", False)
    assert getortho._sync_shared_flight_state() is False


@pytest.fixture
def cpu_budget(monkeypatch):
    monkeypatch.setattr(getortho, "CURRENT_CPU_COUNT", 12)
    monkeypatch.setattr(getortho, "_active_native_builds", 0)
    monkeypatch.setattr(getortho, "_active_native_thread_count", 0)
    monkeypatch.setattr(getortho, "_peak_native_thread_count", 0)
    monkeypatch.setattr(getortho, "_native_build_waiters", getortho.deque())
    monkeypatch.setattr(getortho, "_shutdown_requested", threading.Event())
    monkeypatch.setattr(getortho.CFG.autoortho, "native_pipeline_threads", 0)
    monkeypatch.delenv("AO_MOUNT_WORKER_COUNT", raising=False)


@pytest.mark.parametrize("workers", [1, 4, 6, 32])
def test_overlapping_native_work_never_exceeds_worker_cpu_share(cpu_budget, monkeypatch, workers):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", str(workers))
    allowed = max(1, 12 // workers)
    start = threading.Barrier(8)
    def build():
        start.wait(timeout=2)
        with getortho._native_build_context() as threads:
            active, total = getortho._native_thread_load()
            assert 1 <= threads <= allowed
            assert 1 <= active and total <= allowed
            # Allow competing threads to enter while this reservation is held.
            threading.Event().wait(0.01)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(build) for _ in range(8)]
        for future in futures:
            future.result(timeout=3)
    assert getortho._native_thread_load() == (0, 0)
    assert getortho.native_thread_load_peak_and_reset()[2] <= allowed


def test_native_explicit_override_is_still_capped(cpu_budget, monkeypatch):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "6")
    monkeypatch.setattr(getortho.CFG.autoortho, "native_pipeline_threads", 32)
    with getortho._native_build_context() as threads:
        assert threads == 2
    assert getortho._native_thread_load() == (0, 0)


def test_native_waiter_is_cancelled_on_shutdown(cpu_budget, monkeypatch):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "12")
    waiting = threading.Event()
    def wait_for_slot():
        waiting.set()
        with getortho._native_build_context():
            pytest.fail("Waiting build started during shutdown")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with getortho._native_build_context():
            future = executor.submit(wait_for_slot)
            assert waiting.wait(1)
            getortho._shutdown_requested.set()
            with getortho._native_build_condition:
                getortho._native_build_condition.notify_all()
            with pytest.raises(getortho._BackgroundBuildDeferred):
                future.result(timeout=1)
    assert getortho._native_thread_load() == (0, 0)


def test_native_reservation_released_on_build_failure(cpu_budget):
    with pytest.raises(RuntimeError):
        with getortho._native_build_context():
            raise RuntimeError("build failed")
    assert getortho._native_thread_load() == (0, 0)


def test_native_full_reservation_cannot_be_overtaken(cpu_budget, monkeypatch):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "6")  # two-thread share
    order = []
    def build(name, threads=None):
        with getortho._native_build_context(threads=threads):
            order.append(name)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        with getortho._native_build_context(threads=1):
            partial = executor.submit(build, "partial", 2)
            with getortho._native_build_condition:
                assert getortho._native_build_condition.wait_for(
                    lambda: len(getortho._native_build_waiters) == 1, timeout=1
                )
            small = executor.submit(build, "small", 1)
            with getortho._native_build_condition:
                assert getortho._native_build_condition.wait_for(
                    lambda: len(getortho._native_build_waiters) == 2, timeout=1
                )
        partial.result(timeout=1)
        small.result(timeout=1)
    assert order == ["partial", "small"]
    assert not getortho._native_build_waiters


def test_live_native_build_precedes_waiting_prefetch(cpu_budget, monkeypatch):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "6")
    order = []
    def build(name, priority):
        with getortho._native_build_context(threads=1, priority=priority):
            order.append(name)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        with getortho._native_build_context(threads=2):
            background = executor.submit(build, "background", getortho.PRIORITY_PREFETCH)
            with getortho._native_build_condition:
                assert getortho._native_build_condition.wait_for(
                    lambda: len(getortho._native_build_waiters) == 1, timeout=1
                )
            live = executor.submit(build, "live", getortho.PRIORITY_LIVE)
            with getortho._native_build_condition:
                assert getortho._native_build_condition.wait_for(
                    lambda: len(getortho._native_build_waiters) == 2, timeout=1
                )
        live.result(timeout=1)
        background.result(timeout=1)
    assert order == ["live", "background"]
    assert getortho._native_thread_load() == (0, 0)


def test_legacy_native_build_reserves_default_openmp_team(cpu_budget, monkeypatch):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "6")
    def build(**kwargs):
        assert getortho._native_thread_load() == (1, 2)
        return SimpleNamespace(
            success=True, data=b"DDS data", chunks_decoded=1,
            chunks_found=1, mipmaps=1, elapsed_ms=1.0,
        )
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: SimpleNamespace(
        build_tile_native_detailed=build,
    ))
    assert getortho._build_dds_native("/cache", 0, 0, "BI", 16, 16,
                                     "BC1", (0, 0, 0)) == b"DDS data"
    assert getortho._native_thread_load() == (0, 0)


def test_future_exact_row_reserves_default_openmp_team(cpu_budget, monkeypatch, tmp_path):
    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "6")
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    ready = threading.Event()
    ready.set()
    chunks = [SimpleNamespace(ready=ready, data=b"jpeg")] * tile.chunks_per_row
    monkeypatch.setattr(tile, "_get_chunk_grid", lambda zoom: SimpleNamespace(
        ensure_rows=lambda *args: chunks,
    ))
    monkeypatch.setattr(getortho, "dynamic_dds_cache", object())
    queued = []
    monkeypatch.setattr(tile, "_queue_partial_rows", queued.extend)
    data = b"\x11" * (tile.dds.mipmap_list[0].length // tile.chunks_per_col)
    def build(**kwargs):
        assert getortho._native_thread_load() == (1, 2)
        return SimpleNamespace(success=True, data=data)
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: SimpleNamespace(
        build_partial_mipmap=build,
    ))
    assert tile._build_future_exact_row(1)
    assert len(queued) == 1
    assert queued[0].data == data
    assert queued[0].manifest.row_index == 1
    assert getortho._native_thread_load() == (0, 0)


def test_cancelled_future_exact_row_releases_capacity(monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    assert capacity.acquire(blocking=False)
    monkeypatch.setattr(getortho, "_exact_row_capacity", capacity)
    def cancelled(row):
        raise getortho._BackgroundBuildDeferred("shutdown requested")
    tile = SimpleNamespace(
        _closed=False, _exact_row_lock=threading.Lock(),
        _exact_row_states={1: "build_queued"},
        _mm0_rows=SimpleNamespace(state=lambda row: getortho.RowState.SERVED_EXACT),
        _build_future_exact_row=cancelled,
    )
    monkeypatch.setattr(getortho, "is_shutdown_requested", lambda: False)
    getortho.Tile._run_exact_prefetch_row(tile, 1)
    assert tile._exact_row_states[1] == "unbuilt"
    assert capacity.acquire(blocking=False)
    capacity.release()


def test_macos_poison_does_not_exit_fuse_or_cancel_from_getattr(monkeypatch, fuse_module):
    monkeypatch.setattr(fuse_module, "system_type", "darwin")
    def forbidden(*args, **kwargs):
        raise AssertionError("Do not stop FUSE from a filesystem callback")
    monkeypatch.setattr(fuse_module, "do_fuse_exit", forbidden)
    monkeypatch.setattr(getortho, "begin_shutdown", forbidden)
    ao = fuse_module.AutoOrtho.__new__(fuse_module.AutoOrtho)
    assert ao._getattr_poison(123)["st_size"] == 0


def test_macos_mount_always_stays_in_foreground(monkeypatch, fuse_module):
    monkeypatch.setattr(fuse_module, "system_type", "darwin")
    assert fuse_module.fuse_option_profiles_by_os(False, "z_ao")["foreground"] is True


def test_stats_batcher_stop_joins_background_thread(stats):
    batcher = stats.StatsBatcher(flush_interval=30)
    batcher.add("count", 2)
    batcher.stop()
    assert stats.get_stat("count") == 2
    assert not batcher._t.is_alive()


def test_tile_cleaner_stop_wakes_sleeping_thread(cpu_budget):
    cacher = getortho.TileCacher.__new__(getortho.TileCacher)
    cacher._clean_stop = threading.Event()
    cacher.clean_t = threading.Thread(target=lambda: cacher._clean_stop.wait(30))
    cacher.clean_t.start()
    cacher.stop()
    assert not cacher.clean_t.is_alive()

"""Phase 4: live levers (LiveSettings, resizable gates, broker, parent)."""

import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import aoconfig  # noqa: E402
import connection_test  # noqa: E402
import hardware  # noqa: E402
from hardware import GB, HardwareInfo  # noqa: E402
from live_settings import GENERATION_KEY, LEVERS_KEY, LiveSettings, ResizableGate  # noqa: E402


class _Store(dict):
    def read(self, key):
        return self.get(key, 0)

    def write(self, key, value):
        self[key] = value


# -- ResizableGate -----------------------------------------------------------


def test_gate_bounds_and_overrelease():
    gate = ResizableGate(2)
    assert gate.acquire() and gate.acquire()
    assert gate.acquire(blocking=False) is False
    assert gate.acquire(timeout=0.01) is False
    gate.release()
    assert gate.acquire(timeout=0.01) is True
    gate.release()
    gate.release()
    with pytest.raises(ValueError):
        gate.release()


def test_gate_grows_and_wakes_waiters():
    gate = ResizableGate(1)
    gate.acquire()
    got = []
    worker = threading.Thread(target=lambda: got.append(gate.acquire(timeout=2)))
    worker.start()
    time.sleep(0.05)
    assert not got
    gate.resize(2)
    worker.join(2)
    assert got == [True]
    assert gate.held == 2


def test_gate_shrinks_without_taking_permits():
    gate = ResizableGate(3)
    for _ in range(3):
        gate.acquire()
    gate.resize(1)
    assert gate.held == 3 and gate.limit == 1
    gate.release()
    gate.release()
    assert gate.acquire(blocking=False) is False      # 1 held = limit
    gate.release()
    assert gate.acquire(blocking=False) is True


# -- LiveSettings ------------------------------------------------------------


def _cfg():
    return SimpleNamespace(
        autoortho=SimpleNamespace(live_tile_admission=16, prefetch_radius_nm=30),
        cache=SimpleNamespace(cache_mem_limit=4),
    )


def test_publish_reaches_worker_cfg_and_hooks():
    store = _Store()
    now = [0.0]
    parent = LiveSettings(reader=store.read, writer=store.write)
    worker_cfg = _cfg()
    worker = LiveSettings(
        worker_cfg, reader=store.read, writer=store.write, clock=lambda: now[0]
    )
    seen = []
    worker.on_change("autoortho.live_tile_admission", seen.append)
    assert worker.get("autoortho.live_tile_admission") == 16

    parent.publish({"autoortho.live_tile_admission": 24})
    now[0] += 2
    assert worker.get("autoortho.live_tile_admission") == 24
    assert worker_cfg.autoortho.live_tile_admission == 24
    assert seen == [24]

    # Merged with earlier levers; an unchanged one does not fire again.
    parent.publish({"cache.cache_mem_limit": 6})
    now[0] += 2
    changed = worker.refresh()
    assert changed == {"cache.cache_mem_limit": 6}
    assert store[LEVERS_KEY] == {
        "autoortho.live_tile_admission": 24, "cache.cache_mem_limit": 6,
    }
    assert seen == [24]


def test_refresh_is_rate_limited_and_survives_errors():
    calls = []

    def reader(key):
        calls.append(key)
        raise OSError("manager gone")

    now = [0.0]
    worker = LiveSettings(_cfg(), reader=reader, writer=lambda *a: None,
                          clock=lambda: now[0])
    assert worker.refresh() == {}
    assert worker.refresh() == {}
    assert len(calls) == 1
    assert worker.get("autoortho.prefetch_radius_nm") == 30


def test_hook_failure_does_not_stop_others():
    store = _Store()
    worker = LiveSettings(_cfg(), reader=store.read, writer=store.write)
    seen = []
    worker.on_change("x.y", lambda v: 1 / 0)
    worker.on_change("x.y", seen.append)
    LiveSettings(reader=store.read, writer=store.write).publish({"x.y": 3})
    worker.refresh(force=True)
    assert seen == [3]


def test_background_poll_applies_without_readers():
    store = _Store()
    cfg = _cfg()
    worker = LiveSettings(cfg, reader=store.read, writer=store.write, poll_every=0.02)
    worker.start()
    try:
        LiveSettings(reader=store.read, writer=store.write).publish(
            {"autoortho.prefetch_radius_nm": 45}
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and cfg.autoortho.prefetch_radius_nm != 45:
            time.sleep(0.01)
        assert cfg.autoortho.prefetch_radius_nm == 45
    finally:
        worker.stop()


def test_clear_resets_published():
    store = _Store()
    live = LiveSettings(reader=store.read, writer=store.write)
    live.publish({"a.b": 1})
    live.clear()
    assert live.published() == {}
    assert store[GENERATION_KEY]


# -- getortho levers -----------------------------------------------------------


def test_getortho_hooks_resize_engine_parts(monkeypatch):
    import getortho

    original = getortho._live_tile_admission.limit
    try:
        getortho._on_live_admission("24")
        assert getortho._live_tile_admission.limit == 24
        getortho._on_live_admission("junk")
        assert getortho._live_tile_admission.limit == 24
    finally:
        getortho._live_tile_admission.resize(original)

    old_lim = getortho.TileCacher.cache_mem_lim
    try:
        getortho._on_live_cache_limit(3)
        assert getortho.TileCacher.cache_mem_lim == 3 * GB
    finally:
        getortho.TileCacher.cache_mem_lim = old_lim

    monkeypatch.setattr(getortho, "_live_cpu_threads", 0)
    getortho._on_live_cpu_threads(2)
    assert getortho._cpu_thread_total() == min(2, getortho.CURRENT_CPU_COUNT)
    getortho._on_live_cpu_threads(0)

    loaded = []
    monkeypatch.setattr(getortho.spatial_prefetcher, "_load_config",
                        lambda: loaded.append(1))
    getortho._on_live_prefetch(40)
    assert loaded == [1]


def test_background_builder_setters(monkeypatch):
    import getortho

    builder = getortho.BackgroundDDSBuilder(dds_cache=None, max_workers=2)
    builder.set_max_workers(6)
    assert builder._max_workers == 6
    builder.set_max_workers(99)
    assert builder._max_workers == builder.MAX_WORKERS_CEILING
    builder.set_build_interval(0.5)
    assert builder._build_interval == 0.5
    monkeypatch.setattr(getortho, "_active_mount_worker_count", lambda: 2)
    builder.set_byte_budget_mb(1024)
    assert builder._byte_budget == 512 * 1024 * 1024


# -- broker levers -------------------------------------------------------------


def test_adaptive_ceiling_moves_but_keeps_its_floor():
    from http2_broker import _AdaptiveConcurrencyController

    controller = _AdaptiveConcurrencyController(
        initial=64, minimum=16, maximum=128, connection_limit=64,
    )
    for _ in range(3):
        controller.try_acquire("https://a")
    assert controller.set_maximum(32) == 32
    assert controller.limit_for("https://a") <= 32
    assert controller.set_maximum(4) == 16          # never below the floor
    assert controller.set_maximum(256) == 256


def test_broker_core_configure_changes_rate_and_ceiling():
    from http2_broker import _BrokerCore

    async def go():
        async def reply(*_args, **_kwargs):
            return None

        import httpx

        core = _BrokerCore(
            max_concurrency=32, max_connections=8, max_response_bytes=1 << 20,
            reply_cb=reply, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )
        applied = core.configure(origin_max_concurrency=500, max_bytes_per_second=1e6)
        assert applied["origin_max_concurrency"] <= 32
        assert core._rate is not None and core._rate.rate == 1e6
        core.configure(max_bytes_per_second=2e6)
        assert core._rate.rate == 2e6
        core.configure(max_bytes_per_second=0)
        assert core._rate is None
        await core._client.aclose()

    asyncio.run(go())


def test_broker_client_configure_sends_control():
    from http2_broker import HTTP2Broker

    broker = HTTP2Broker(in_process=True)
    assert broker.configure(origin_max_concurrency=10) is False   # not started
    sent = []
    broker._started = True
    broker._dispatcher = SimpleNamespace(send_control=sent.append)
    assert broker.configure(origin_max_concurrency=10, max_bytes_per_second=5) is True
    assert sent[0]["type"] == "CONFIG"
    assert sent[0]["origin_max_concurrency"] == 10
    assert sent[0]["max_bytes_per_second"] == 5.0


# -- config: recompute ---------------------------------------------------------


@pytest.fixture
def pc(monkeypatch, tmp_path):
    monkeypatch.setattr(
        connection_test, "RESULT_PATH", str(tmp_path / "connection.json")
    )
    aoconfig.hardware.set_for_tests(HardwareInfo(32, 16, 64 * GB, 24 * GB))
    yield
    aoconfig.hardware.set_for_tests(None)


def test_recompute_follows_new_limits_and_keeps_own_values(tmp_path, pc):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[autoortho]\nbackground_builder_workers = 3\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 8
    cfg.set_value("limits", "ram_gb", "3")
    changed = cfg.recompute_automatic()
    assert changed["cache.cache_mem_limit"] == 2
    assert cfg.source("cache", "cache_mem_limit") == "limit"
    assert "autoortho.background_builder_workers" not in changed
    assert cfg.autoortho.background_builder_workers == 3

    cfg.set_value("limits", "ram_gb", "auto")
    assert cfg.recompute_automatic()["cache.cache_mem_limit"] == 8
    assert cfg.recompute_automatic() == {}


# -- parent publishing ---------------------------------------------------------


def test_parent_publishes_levers_and_configures_broker(tmp_path, pc, monkeypatch):
    import autoortho as ao_module

    store = _Store()
    mount = ao_module.AOMount.__new__(ao_module.AOMount)
    mount.cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    mount._live_settings_handle = LiveSettings(reader=store.read, writer=store.write)
    calls = []
    mount._download_broker = SimpleNamespace(configure=lambda **kw: calls.append(kw))
    mount.cfg.set_value("limits", "bandwidth_mbps", "40")
    levers = mount.apply_live_levers()
    assert levers["limits.bytes_per_second"] == 5_000_000
    assert store[LEVERS_KEY]["limits.cpu_threads"] > 0
    assert calls and calls[-1]["max_bytes_per_second"] == 5_000_000
    assert "origin_max_concurrency" in calls[-1]

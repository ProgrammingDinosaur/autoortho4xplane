"""Limits in the engine: broker bandwidth, RAM brake, CPU threads, guards."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guards  # noqa: E402
import limits  # noqa: E402
import target_cost as tc  # noqa: E402
from connection_test import Result, Round  # noqa: E402
from guards import GB, Level  # noqa: E402
from hardware import HardwareInfo  # noqa: E402
from ram_brake import RamBrake  # noqa: E402

PC = HardwareInfo(16, 8, 32 * GB, 12 * GB)


# -- broker bandwidth -----------------------------------------------------


def test_byte_rate_limiter_holds_the_rate():
    from http2_broker import _ByteRateLimiter

    now = [0.0]
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    async def go():
        limiter = _ByteRateLimiter(1_000_000, clock=lambda: now[0], sleep=sleep)
        for _ in range(40):
            await limiter.consume(100_000)        # 4 MB in all

    asyncio.run(go())
    # The first quarter second of bytes is free (the bucket); the rest waits.
    assert sum(slept) == pytest.approx(4.0 - 0.25, rel=0.01)


def test_broker_passes_the_limit_to_its_core():
    from http2_broker import _server_config

    config = _server_config(
        max_concurrency=8, max_connections=8, max_request_bytes=1,
        max_response_bytes=1, max_bytes_per_second=125_000,
    )
    assert config["max_bytes_per_second"] == 125_000


# -- RAM brake ------------------------------------------------------------


def test_brake_engages_when_autoortho_is_over_its_limit():
    now = [0.0]
    brake = RamBrake(reader=lambda: (20 * GB, 32 * GB), clock=lambda: now[0])
    brake.note_own_memory(5 * GB, 4 * GB)
    assert brake.sample(force=True) is True
    assert brake.reason == "limit"
    # Back under 90 % of the limit for 20 s: released.
    brake.note_own_memory(3 * GB, 4 * GB)
    now[0] += 1
    brake.sample()
    now[0] += 21
    assert brake.sample() is False


def test_brake_ignores_own_memory_without_a_limit():
    brake = RamBrake(reader=lambda: (20 * GB, 32 * GB))
    brake.note_own_memory(50 * GB, None)
    assert brake.sample(force=True) is False


# -- CPU threads ----------------------------------------------------------


def test_native_thread_budget_follows_the_cpu_limit(monkeypatch):
    import getortho

    monkeypatch.setenv("AO_MOUNT_WORKER_COUNT", "2")
    monkeypatch.setenv("AO_CPU_THREAD_LIMIT", "8")
    assert getortho._native_cpu_budget() == min(8, getortho.CURRENT_CPU_COUNT) // 2
    monkeypatch.setenv("AO_CPU_THREAD_LIMIT", "100000")
    assert getortho._cpu_thread_total() == getortho.CURRENT_CPU_COUNT


def test_supervisor_sizes_openmp_from_the_limit():
    from process_supervisor import apply_openmp_budget

    env = apply_openmp_budget(
        {"AO_CPU_THREAD_LIMIT": "6", "AO_MOUNT_WORKER_COUNT": "3"}
    )
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["OMP_THREAD_LIMIT"] == "2"
    env = apply_openmp_budget({"AO_MOUNT_WORKER_COUNT": "1"})
    assert env["OMP_THREAD_LIMIT"] == str(os.cpu_count() or 1)


# -- guards with limits ---------------------------------------------------


def _result(mbits):
    return Result("2026-11-12T10:00:00+00:00", "BI",
                  [Round(64, 256, 0, 4_000_000, 1.0, 0.1, mbits)], 64)


def test_custom_vram_limit_is_the_budget():
    cost = tc.target_cost("fixed", 16, 18)
    custom = limits.resolve({"vram_gb": "2"}, PC)
    result = guards.check_vram(cost, PC, limits=custom)
    assert result.level is Level.BAD
    assert "Over your graphics memory limit" in result.message
    auto = limits.resolve({}, PC)
    assert guards.check_vram(cost, PC, limits=auto).level is Level.OK


def test_download_check_uses_connection_and_limit():
    cost = tc.target_cost("fixed", 17, 18)       # about 12 Mbit/s
    assert guards.check_download(cost).level is Level.INFO
    assert guards.check_download(cost, connection=_result(200)).level is Level.OK
    tight = guards.check_download(cost, connection=_result(10))
    assert tight.level is Level.WARN
    assert "blurry" in tight.message
    capped = guards.check_download(
        cost, limits.resolve({"bandwidth_mbps": "8"}, PC), _result(200)
    )
    assert capped.level is Level.WARN
    assert "your download limit" in capped.message


def test_ram_above_limit_warns_with_fix():
    active = limits.resolve({"ram_gb": "4"}, PC)
    result = guards.check_ram(6, PC, active)
    assert result.level is Level.WARN
    assert "your RAM limit of 4 GB" in result.message
    assert result.fix.changes == {"cache.cache_mem_limit": "3"}


def test_suggest_target_for_pc():
    big = HardwareInfo(16, 8, 32 * GB, 24 * GB)
    fix = guards.suggest_target_for_pc(big, connection=_result(500))
    assert fix.changes["autoortho.max_zoom"] == "17"
    assert fix.changes["autoortho.max_zoom_mode"] == "fixed"

    slow = guards.suggest_target_for_pc(big, connection=_result(8))
    assert int(slow.changes["autoortho.max_zoom"]) < 17
    assert "8 Mbit/s" in slow.label

    small_card = HardwareInfo(16, 8, 32 * GB, 8 * GB)
    fix = guards.suggest_target_for_pc(small_card)
    assert int(fix.changes["autoortho.max_zoom"]) <= 15

    unknown = guards.suggest_target_for_pc(HardwareInfo(16, 8, 32 * GB))
    assert unknown.changes["autoortho.max_zoom"] == "16"
    assert "good start" in unknown.label

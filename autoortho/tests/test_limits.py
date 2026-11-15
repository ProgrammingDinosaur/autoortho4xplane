"""Resource limits: auto values, parsing, clamping, and the config layer."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aoconfig  # noqa: E402
import limits  # noqa: E402
from hardware import GB, HardwareInfo  # noqa: E402

PC16 = HardwareInfo(16, 8, 16 * GB, 8 * GB)
PC64 = HardwareInfo(32, 16, 64 * GB, 24 * GB)
PC8 = HardwareInfo(8, 4, 8 * GB)


@pytest.mark.parametrize("text, expected", [
    ("auto", None), ("", None), ("AUTO", None), ("0", None),
    ("6", 6.0), ("4.5", 4.5), ("junk", None), (None, None), (12, 12.0),
])
def test_parse(text, expected):
    assert limits.parse(text) == expected


def test_auto_values():
    assert limits.auto_ram_gb(PC16) == 4.5      # quarter + 0.5
    assert limits.auto_ram_gb(PC8) == 2.5       # never under cache minimum
    assert limits.auto_ram_gb(PC64) == 16.5
    assert limits.auto_vram_gb(PC16, 5) == 3.0
    assert limits.auto_vram_gb(PC8) is None
    assert limits.auto_cpu_cores(PC16) == 6
    assert limits.auto_cpu_cores(PC8) == 3       # four cores: keep one
    assert limits.auto_cpu_cores(HardwareInfo(1, 1, GB)) == 1


def test_auto_ram_always_leaves_room_for_xplane():
    for ram in (8, 12, 16, 24, 32, 48, 64, 128):
        pc = HardwareInfo(8, 4, ram * GB)
        value = limits.auto_ram_gb(pc)
        assert value <= ram - max(4, 0.2 * ram) or value == 2.5


def test_baseline_values_fit_auto_limits():
    # The hardware baseline must never be lowered by the automatic limits.
    import hardware

    for cores, ram in ((4, 8), (8, 16), (12, 32), (16, 64), (32, 192)):
        pc = HardwareInfo(cores * 2, cores, ram * GB)
        values = hardware.baseline_values(pc)
        resolved = limits.resolve({}, pc)
        assert limits.clamp_automatic(values, resolved) == values


def test_resolve_user_values_and_threads():
    resolved = limits.resolve(
        {"ram_gb": "6", "cpu_cores": "99", "bandwidth_mbps": "50"}, PC16
    )
    assert resolved.ram_gb == 6.0
    assert resolved.cpu_cores == 8              # capped at physical cores
    assert resolved.cpu_threads == 16           # two threads per core
    assert resolved.bandwidth_mbps == 50
    assert resolved.bytes_per_second == 50_000_000 / 8
    assert resolved.is_custom("ram_gb") and not resolved.is_custom("vram_gb")
    assert resolved.vram_gb == 3.0              # auto: 8 GB card minus 5
    assert resolved.cache_ceiling_gb == 5


def test_clamp_lowers_only_governed_values():
    resolved = limits.resolve({"ram_gb": "3", "cpu_cores": "2"}, PC64)
    out = limits.clamp_automatic(
        {
            "cache.cache_mem_limit": "8",
            "autoortho.predictive_dds_memory_mb": "1024",
            "autoortho.live_builder_concurrency": "12",
            "autoortho.background_builder_workers": "4",
            "autoortho.other": "7",
        },
        resolved,
    )
    assert out == {
        "cache.cache_mem_limit": "2",
        "autoortho.predictive_dds_memory_mb": "768",
        "autoortho.live_builder_concurrency": "4",
        "autoortho.background_builder_workers": "1",
        "autoortho.other": "7",
    }


def test_describe():
    text = limits.describe(limits.resolve({"bandwidth_mbps": "40"}, PC16))
    assert text["ram_gb"] == "4.5 GB (auto)"
    assert text["bandwidth_mbps"] == "40 Mbit/s (your limit)"
    assert limits.describe(limits.resolve({}, PC8))["vram_gb"] == "unknown (auto)"


# -- config layer ---------------------------------------------------------


@pytest.fixture
def pc(monkeypatch, tmp_path):
    import connection_test

    monkeypatch.setattr(
        connection_test, "RESULT_PATH", str(tmp_path / "connection.json")
    )

    def use(info):
        aoconfig.hardware.set_for_tests(info)
    yield use
    aoconfig.hardware.set_for_tests(None)


def test_limit_lowers_automatic_values_and_says_so(tmp_path, pc):
    pc(PC64)
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[limits]\nram_gb = 3\ncpu_cores = 2\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 2
    assert cfg.source("cache", "cache_mem_limit") == "limit"
    assert cfg.autoortho.live_builder_concurrency == 4
    assert cfg.source("autoortho", "live_builder_concurrency") == "limit"
    text = path.read_text(encoding="utf-8")
    assert "# (lowered to fit [limits]; the packaged default is 4)" in text
    assert "ram_gb = 3" in text


def test_limit_does_not_override_users_own_value(tmp_path, pc):
    pc(PC64)
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[limits]\nram_gb = 3\n"
        "[cache]\ncache_mem_limit = 12\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 12
    assert cfg.source("cache", "cache_mem_limit") == "custom"


def test_limits_apply_without_the_baseline(tmp_path, pc):
    pc(PC64)
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[tuning]\nhardware_baseline = False\n"
        "[limits]\nram_gb = 3\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 2          # packaged 4, lowered
    assert cfg.autoortho.live_builder_concurrency == 8  # packaged default


def test_connection_test_warm_starts_concurrency(tmp_path, pc):
    import connection_test

    pc(PC16)
    connection_test.save_result(connection_test.Result(
        "2026-11-12T10:00:00+00:00", "BI",
        [connection_test.Round(256, 1024, 0, 16_000_000, 4.0, 0.2, 90.0)],
        256,
    ))
    cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    assert cfg.autoortho.provider_origin_initial_concurrency == 256
    assert cfg.source("autoortho", "provider_origin_initial_concurrency") == "measured"


def test_limits_validate_auto_or_number():
    spec = aoconfig.settings_registry().get("limits", "ram_gb")
    assert spec.kind == "auto"
    assert spec.validate("Auto") == (True, "auto", "")
    assert spec.validate("6") == (True, "6", "")
    assert spec.validate("4.5") == (True, "4.5", "")
    assert spec.validate("lots")[0] is False
    assert spec.validate("0")[0] is False

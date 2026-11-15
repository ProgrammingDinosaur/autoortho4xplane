"""The hardware baseline layer in the config: scaled defaults, never pinned."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aoconfig  # noqa: E402
from hardware import GB, HardwareInfo  # noqa: E402

BIG_PC = HardwareInfo(32, 16, 64 * GB)        # cache 8, builders 12, bg 4
SMALL_PC = HardwareInfo(4, 2, 8 * GB)         # cache 2, builders 4, bg 1


@pytest.fixture
def pc():
    def use(info):
        aoconfig.hardware.set_for_tests(info)
    yield use
    aoconfig.hardware.set_for_tests(None)


def _active(text):
    return [
        line for line in text.splitlines()
        if line and not line.startswith(("#", "["))
    ]


def test_untouched_settings_follow_the_pc(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 8
    assert cfg.autoortho.live_builder_concurrency == 12
    assert cfg.autoortho.background_builder_workers == 4
    assert cfg.source("cache", "cache_mem_limit") == "hardware"
    assert not cfg.is_custom("cache", "cache_mem_limit")

    text = path.read_text(encoding="utf-8")
    assert _active(text) == [f"config_revision = {aoconfig.CONFIG_REVISION}"]
    assert "# (set for this PC; the packaged default is 4)" in text
    assert "# cache_mem_limit = 8" in text

    pc(SMALL_PC)
    other = aoconfig.AOConfig(str(path))
    assert other.cache.cache_mem_limit == 2
    assert other.autoortho.live_builder_concurrency == 4


def test_users_own_value_wins(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[cache]\ncache_mem_limit = 4\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    # An active line in a sparse file is a choice, even at the packaged default.
    assert cfg.cache.cache_mem_limit == 4
    assert cfg.source("cache", "cache_mem_limit") == "custom"
    assert "cache_mem_limit = 4" in _active(path.read_text(encoding="utf-8"))


def test_old_file_at_packaged_default_moves_to_the_baseline(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 1\n[cache]\ncache_mem_limit = 4\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 8
    assert cfg.source("cache", "cache_mem_limit") == "hardware"


def test_value_equal_to_baseline_is_not_pinned(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    cfg = aoconfig.AOConfig(str(path))
    cfg.set_value("cache", "cache_mem_limit", "8")
    cfg.save()
    assert "cache_mem_limit = 8" not in _active(path.read_text(encoding="utf-8"))


def test_reset_returns_to_the_baseline(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    cfg = aoconfig.AOConfig(str(path))
    cfg.set_value("cache", "cache_mem_limit", "16")
    assert cfg.source("cache", "cache_mem_limit") == "custom"
    cfg.reset_to_default("cache", "cache_mem_limit")
    assert cfg.cache.cache_mem_limit == 8
    assert cfg.automatic_text("cache", "cache_mem_limit") == "8"
    assert cfg.source("cache", "cache_mem_limit") == "hardware"


def test_baseline_can_be_turned_off(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[tuning]\nhardware_baseline = False\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.cache.cache_mem_limit == 4
    assert cfg.source("cache", "cache_mem_limit") == "default"


def test_turning_it_off_does_not_pin_the_scaled_values(tmp_path, pc):
    pc(BIG_PC)
    path = tmp_path / ".autoortho"
    cfg = aoconfig.AOConfig(str(path))
    cfg.set_value("tuning", "hardware_baseline", "False")
    cfg.save()
    text = path.read_text(encoding="utf-8")
    assert "hardware_baseline = False" in _active(text)
    assert "cache_mem_limit = 8" not in _active(text)
    assert aoconfig.AOConfig(str(path)).cache.cache_mem_limit == 4


def test_guard_settings_are_registered():
    registry = aoconfig.settings_registry()
    assert registry.get("guards", "warnings").default is True
    assert registry.get("guards", "xplane_vram_reserve_gb").range == (0, 24)
    assert registry.get("guards", "ram_brake").default is True
    assert registry.get("tuning", "hardware_baseline").default is True

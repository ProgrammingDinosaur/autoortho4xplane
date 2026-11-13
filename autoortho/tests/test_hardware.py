"""Hardware detection helpers and the hardware baseline rules."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware  # noqa: E402
from hardware import GB, HardwareInfo  # noqa: E402


def _pc(cores, ram_gb, vram_gb=None):
    return HardwareInfo(
        cores * 2,
        cores,
        int(ram_gb * GB),
        int(vram_gb * GB) if vram_gb else None,
    )


@pytest.mark.parametrize(
    ("cores", "ram", "expected"),
    [
        # 8 GB laptop: smallest cache, fewest builders.
        (4, 8, {"cache.cache_mem_limit": "2",
                "autoortho.predictive_dds_memory_mb": "256",
                "autoortho.live_builder_concurrency": "4",
                "autoortho.background_builder_workers": "1"}),
        # 16 GB, 8 cores: today's packaged defaults.
        (8, 16, {"cache.cache_mem_limit": "4",
                 "autoortho.predictive_dds_memory_mb": "512",
                 "autoortho.live_builder_concurrency": "8",
                 "autoortho.background_builder_workers": "2"}),
        (12, 32, {"cache.cache_mem_limit": "8",
                  "autoortho.predictive_dds_memory_mb": "768",
                  "autoortho.live_builder_concurrency": "12",
                  "autoortho.background_builder_workers": "3"}),
        # Workstation: capped.
        (32, 192, {"cache.cache_mem_limit": "8",
                   "autoortho.predictive_dds_memory_mb": "1024",
                   "autoortho.live_builder_concurrency": "12",
                   "autoortho.background_builder_workers": "4"}),
    ],
)
def test_baseline_scales_with_the_pc(cores, ram, expected):
    assert hardware.baseline_values(_pc(cores, ram)) == expected


def test_mid_range_pc_keeps_packaged_defaults():
    import aoconfig

    registry = aoconfig.settings_registry()
    for name, value in hardware.baseline_values(_pc(8, 16)).items():
        section, key = name.split(".", 1)
        assert registry.get(section, key).default_text == value, name


def test_ram_in_whole_gb():
    # Windows reports a little less than the installed RAM.
    assert HardwareInfo(8, 4, int(31.8 * GB)).ram_gb == 32


def test_unknown_values_give_no_baseline():
    assert hardware.baseline_values(HardwareInfo(0, 0, 0)) == {}


def test_registry_numbers():
    assert hardware._registry_number(8 * GB) == 8 * GB
    assert hardware._registry_number((8 * GB).to_bytes(8, "little")) == 8 * GB
    assert hardware._registry_number((4 * GB - 1).to_bytes(4, "little")) == 4 * GB - 1
    # A REG_DWORD above 2 GB can come back negative.
    assert hardware._registry_number(-1) == 0xFFFFFFFF
    assert hardware._registry_number("junk") == 0


def test_apple_unified_share():
    assert hardware.apple_unified_vram(24 * GB) == int(24 * GB * 2 / 3)
    assert hardware.apple_unified_vram(64 * GB) == int(64 * GB * 0.75)


def test_summary_mentions_unknown_graphics_memory():
    assert "graphics memory unknown" in _pc(8, 16).summary()
    assert "12 GB graphics memory" in _pc(8, 16, 12).summary()


def test_detect_is_cached_and_replaceable():
    fake = _pc(6, 24, 10)
    hardware.set_for_tests(fake)
    try:
        assert hardware.detect() is fake
        assert hardware.cached() is fake
        assert hardware.basic() is fake
    finally:
        hardware.set_for_tests(None)
    assert hardware.cached() is None

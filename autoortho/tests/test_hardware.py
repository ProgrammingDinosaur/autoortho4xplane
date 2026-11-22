"""Hardware detection helpers and the hardware baseline rules."""

import os
import sys
from types import SimpleNamespace

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


def test_summary_shows_small_sizes_in_mb():
    # 512 MB used to print as "0 GB".
    assert "512 MB graphics memory" in _pc(6, 32, 0.5).summary()
    assert hardware.size_text(int(11.7 * GB)) == "12 GB"


# ------------------------------------------------------------- GPU detection

MiB = 1024 ** 2
Card = hardware.GpuCandidate


@pytest.mark.parametrize(
    ("vendor", "name", "vram", "integrated"),
    [
        (0x10DE, "NVIDIA GeForce RTX 4070", 12 * GB, False),
        (0x10DE, "NVIDIA GeForce GT 1030", 2 * GB, False),
        (0x1002, "AMD Radeon(TM) Graphics", 16 * GB, True),    # shared RAM
        (0x1002, "AMD Radeon(TM) 780M Graphics", 512 * MiB, True),
        (0x1002, "AMD Radeon(TM) Vega 8 Graphics", 2 * GB, True),
        (0x1002, "AMD Radeon 610M", 512 * MiB, True),
        (0x1002, "AMD Radeon RX 7800 XT", 16 * GB, False),
        (0x1002, "AMD Radeon RX 6800M", 12 * GB, False),
        (0x1002, "AMD Radeon Pro W6800", 32 * GB, False),
        (0x1002, "", 512 * MiB, True),                         # APU in sysfs
        (0x1002, "", 12 * GB, False),
        (0x8086, "Intel(R) UHD Graphics 770", 128 * MiB, True),
        (0x8086, "Intel(R) Iris(R) Xe Graphics", 128 * MiB, True),
        (0x8086, "Intel(R) Arc(TM) Graphics", 128 * MiB, True),
        (0x8086, "Intel(R) Arc(TM) 140V GPU (16GB)", 16 * GB, True),
        (0x8086, "Intel(R) Arc(TM) A770 Graphics", 16 * GB, False),
        (0x8086, "Intel(R) Arc(TM) A370M Graphics", 4 * GB, False),
        (0x8086, "Intel(R) Arc(TM) B580 Graphics", 12 * GB, False),
        (0, "Intel Iris Plus Graphics 655", 1536 * MiB, True),  # Intel Mac
        (0, "AMD Radeon Pro 5500M", 8 * GB, False),
    ],
)
def test_integrated_gpus_are_recognised(vendor, name, vram, integrated):
    assert hardware.looks_integrated(vendor, name, vram) is integrated


def test_integrated_gpu_never_wins():
    igpu = Card(16 * GB, "AMD Radeon(TM) Graphics", "registry", True)
    card = Card(12 * GB, "NVIDIA GeForce RTX 4070", "registry")
    assert hardware.pick_card([igpu, card]) is card
    # Only an integrated GPU: unknown, never its carve-out or RAM share.
    assert hardware.pick_card([igpu]) is None
    assert hardware.pick_card([]) is None


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _drm(tmp_path, cards):
    """A fake /sys/class/drm: name -> {file under device/: contents}."""
    for name, files in cards.items():
        (tmp_path / name / "device").mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            _write(tmp_path / name / "device" / rel, text)
    return str(tmp_path)


APU = {"vendor": "0x1002", "mem_info_vram_total": str(512 * MiB)}
NVIDIA_CARD = {"vendor": "0x10de"}   # the NVIDIA driver publishes no size


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(hardware, "_nvml_adapters", lambda: [])
    monkeypatch.setattr(hardware, "_nvidia_smi_adapters", lambda: [])

    def use_drm(root):
        monkeypatch.setattr(
            hardware, "_sysfs_adapters",
            lambda real=hardware._sysfs_adapters: real(root),
        )
    return use_drm


def test_linux_apu_beside_an_unreadable_card_is_unknown(linux, tmp_path):
    # The 3.4.1 report: a Ryzen APU's 512 MB carve-out was taken for the
    # graphics card while the 12 GB card went unseen.
    linux(_drm(tmp_path, {"card0": APU, "card0-HDMI-A-1": {},
                          "card1": NVIDIA_CARD}))
    assert hardware._detect_vram(32 * GB) == (None, "", "", False)


def test_linux_nvidia_card_through_nvml(linux, tmp_path, monkeypatch):
    linux(_drm(tmp_path, {"card0": APU, "card1": NVIDIA_CARD}))
    monkeypatch.setattr(hardware, "_nvml_adapters", lambda: [
        Card(12 * GB, "NVIDIA GeForce RTX 4070", "nvml")
    ])
    smi = []
    monkeypatch.setattr(hardware, "_nvidia_smi_adapters", lambda: smi.append(1))
    assert hardware._detect_vram(32 * GB) == (
        12 * GB, "NVIDIA GeForce RTX 4070", "nvml", False
    )
    assert not smi      # NVML answered: nvidia-smi not run


def test_linux_falls_back_to_nvidia_smi(linux, tmp_path, monkeypatch):
    linux(_drm(tmp_path, {"card0": APU}))
    monkeypatch.setattr(hardware, "_nvidia_smi_adapters", lambda: [
        Card(12 * GB, "NVIDIA GeForce RTX 3060", "nvidia-smi")
    ])
    assert hardware._detect_vram(32 * GB)[:3] == (
        12 * GB, "NVIDIA GeForce RTX 3060", "nvidia-smi"
    )


def test_linux_intel_xe_card_beside_an_apu(linux, tmp_path):
    linux(_drm(tmp_path, {
        "card0": APU,
        "card1": {"vendor": "0x8086",
                  "tile0/physical_vram_size_bytes": str(12 * GB)},
    }))
    assert hardware._detect_vram(32 * GB)[:3] == (12 * GB, "Intel GPU", "sysfs")


def test_linux_discrete_amd_card_beside_an_apu(linux, tmp_path):
    linux(_drm(tmp_path, {
        "card0": APU,
        "card1": {"vendor": "0x1002", "mem_info_vram_total": str(12 * GB),
                  "product_name": "AMD Radeon RX 6700 XT"},
    }))
    assert hardware._detect_vram(32 * GB)[:3] == (
        12 * GB, "AMD Radeon RX 6700 XT", "sysfs"
    )


def test_one_failing_probe_does_not_hide_the_others(linux, tmp_path,
                                                    monkeypatch):
    linux(_drm(tmp_path, {"card1": {"vendor": "0x1002",
                                    "mem_info_vram_total": str(8 * GB)}}))

    def broken():
        raise OSError("NVML blew up")
    monkeypatch.setattr(hardware, "_nvml_adapters", broken)
    assert hardware._detect_vram(32 * GB)[0] == 8 * GB


def test_frozen_build_gives_tools_the_original_library_path(monkeypatch):
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="linux",
                                                         frozen=True))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/nvidia")
    env = hardware._subprocess_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/nvidia"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in hardware._subprocess_env()


def test_windows_takes_the_card_and_its_exact_size(monkeypatch):
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="win32"))
    # DXGI leaves out Windows' reservation; the iGPU shows its carve-out.
    monkeypatch.setattr(hardware, "_dxgi_adapters", lambda: [
        Card(int(11.7 * GB), "NVIDIA GeForce RTX 4070", "dxgi"),
        Card(512 * MiB, "AMD Radeon(TM) Graphics", "dxgi", True),
    ])
    # The registry gives the iGPU half the RAM and keeps a removed card.
    monkeypatch.setattr(hardware, "_registry_adapters", lambda: [
        Card(12 * GB, "NVIDIA GeForce RTX 4070", "registry"),
        Card(16 * GB, "AMD Radeon(TM) Graphics", "registry", True),
        Card(24 * GB, "NVIDIA GeForce RTX 3090", "registry"),
    ])
    assert hardware._detect_vram(32 * GB) == (
        12 * GB, "NVIDIA GeForce RTX 4070", "dxgi", False
    )


def test_windows_registry_alone_when_dxgi_fails(monkeypatch):
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(hardware, "_dxgi_adapters", lambda: [])
    monkeypatch.setattr(hardware, "_registry_adapters", lambda: [
        Card(16 * GB, "AMD Radeon(TM) Graphics", "registry", True),
        Card(12 * GB, "NVIDIA GeForce RTX 4070", "registry"),
    ])
    assert hardware._detect_vram(32 * GB)[:3] == (
        12 * GB, "NVIDIA GeForce RTX 4070", "registry"
    )


def test_registry_size_only_corrects_a_close_dxgi_size():
    dxgi = [Card(8 * GB, "Same Name GPU", "dxgi")]
    registry = [Card(64 * GB, "Same Name GPU", "registry")]   # implausible
    assert hardware.with_registry_sizes(dxgi, registry) == dxgi


MAC_PROFILE = """{"SPDisplaysDataType": [
  {"sppci_model": "Intel UHD Graphics 630", "sppci_bus": "spdisplays_builtin",
   "spdisplays_vram_shared": "1536 MB"},
  {"sppci_model": "AMD Radeon Pro 5500M", "sppci_bus": "spdisplays_pcie_device",
   "spdisplays_vram": "8 GB"}
]}"""


def test_intel_mac_takes_the_discrete_card(monkeypatch):
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        hardware, "_run",
        lambda command, timeout=4.0: MAC_PROFILE if "system_profiler" in command else "",
    )
    assert hardware._detect_vram(32 * GB)[:3] == (
        8 * GB, "AMD Radeon Pro 5500M", "system_profiler"
    )


def test_intel_build_under_rosetta_reads_unified_memory(monkeypatch):
    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware, "_run", lambda command, timeout=4.0: "1\n")
    vram, name, source, unified = hardware._detect_vram(24 * GB)
    assert (name, source, unified) == ("Apple silicon", "unified", True)
    assert vram == hardware.apple_unified_vram(24 * GB)


@pytest.mark.skipif(sys.platform != "win32", reason="DXGI is Windows only")
def test_dxgi_probe_runs_on_this_pc():
    for card in hardware._dxgi_adapters():
        assert card.source == "dxgi" and card.vram >= 0


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

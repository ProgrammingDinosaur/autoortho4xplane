"""What this PC has: CPU cores, RAM and graphics memory.

Used for the hardware baseline (memory and worker defaults scaled to the PC)
and for the guards that compare a zoom target with the graphics card. Every
probe is best effort: an unknown value is None, never a guess, and callers
treat None as "do not warn".

Graphics memory comes from:

* Windows: the display adapter class in the registry
  (``HardwareInformation.qwMemorySize``, which the driver fills in; the largest
  card wins, so an integrated GPU beside a discrete one is ignored). Same
  source as OrthoStreamXP's ``GraphicsMemory.CardBytes``.
* Linux: ``nvidia-smi``, then ``mem_info_vram_total`` in sysfs (AMD, Intel Arc).
* macOS on Apple silicon: unified memory; the GPU may use about two thirds of
  RAM (three quarters above 36 GB). Intel Macs ask ``system_profiler``.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2

_DISPLAY_CLASS = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
)


@dataclass(frozen=True)
class HardwareInfo:
    logical_cores: int
    physical_cores: int
    ram_total: int                  # bytes
    vram: Optional[int] = None      # bytes; None when unknown
    gpu_name: str = ""
    vram_source: str = ""           # "registry", "nvidia-smi", "sysfs", ...
    unified_memory: bool = False    # GPU shares system RAM (Apple silicon)

    @property
    def ram_gb(self) -> int:
        """Physical RAM in whole GB (31.8 GB reported reads as 32)."""
        return max(1, int(round(self.ram_total / GB)))

    def summary(self) -> str:
        parts = [
            f"{self.physical_cores} cores ({self.logical_cores} threads)",
            f"{self.ram_gb} GB RAM",
        ]
        if self.vram:
            name = f" ({self.gpu_name})" if self.gpu_name else ""
            kind = "shared " if self.unified_memory else ""
            parts.append(f"{self.vram / GB:.0f} GB {kind}graphics memory{name}")
        else:
            parts.append("graphics memory unknown")
        return ", ".join(parts)


# ---------------------------------------------------------------- CPU / RAM


def _cores():
    logical = os.cpu_count() or 1
    physical = None
    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
        logical = psutil.cpu_count(logical=True) or logical
    except Exception:
        pass
    if not physical:
        # Assume two threads per core on SMT machines.
        physical = max(1, logical // 2) if logical >= 4 else logical
    return int(logical), int(physical)


def ram_total() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return 0


def ram_available() -> Optional[int]:
    """RAM the system can hand out right now, in bytes (None if unknown)."""
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return None


# ------------------------------------------------------------------- VRAM


def _registry_number(value) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFFFFFFFFFF if value >= 0 else value & 0xFFFFFFFF
    if isinstance(value, (bytes, bytearray)):
        if len(value) >= 8:
            return int.from_bytes(value[:8], "little")
        if len(value) >= 4:
            return int.from_bytes(value[:4], "little")
    return 0


def _windows_vram():
    try:
        import winreg
    except ImportError:
        return None, ""
    best, best_name = 0, ""
    try:
        cards = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS)
    except OSError:
        return None, ""
    with cards:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(cards, index)
            except OSError:
                break
            index += 1
            try:
                card = winreg.OpenKey(cards, name)
            except OSError:
                continue  # "Properties" and similar keys Windows keeps to itself
            with card:
                size = 0
                for value_name in (
                    "HardwareInformation.qwMemorySize",
                    "HardwareInformation.MemorySize",
                ):
                    try:
                        size = _registry_number(
                            winreg.QueryValueEx(card, value_name)[0]
                        )
                    except OSError:
                        continue
                    if size:
                        break
                if size > best:
                    best = size
                    try:
                        best_name = str(winreg.QueryValueEx(card, "DriverDesc")[0])
                    except OSError:
                        best_name = ""
    return (best or None), best_name


def _run(command, timeout=4.0):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _nvidia_smi_vram():
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.total,name",
        "--format=csv,noheader,nounits",
    ])
    best, best_name = 0, ""
    for line in out.splitlines():
        fields = [part.strip() for part in line.split(",", 1)]
        try:
            size = int(float(fields[0])) * MB
        except (ValueError, IndexError):
            continue
        if size > best:
            best, best_name = size, fields[1] if len(fields) > 1 else ""
    return (best or None), best_name


def _sysfs_vram():
    best = 0
    for path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            with open(path, encoding="ascii") as handle:
                best = max(best, int(handle.read().strip()))
        except (OSError, ValueError):
            continue
    return best or None


_SIZE_RE = re.compile(r"([\d.]+)\s*(GB|MB)", re.IGNORECASE)


def _mac_intel_vram():
    out = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=6.0)
    try:
        cards = json.loads(out).get("SPDisplaysDataType", [])
    except (ValueError, AttributeError):
        return None, ""
    best, best_name = 0, ""
    for card in cards:
        text = str(card.get("spdisplays_vram") or card.get("_spdisplays_vram") or "")
        match = _SIZE_RE.search(text)
        if not match:
            continue
        size = float(match.group(1)) * (GB if match.group(2).upper() == "GB" else MB)
        if size > best:
            best, best_name = int(size), str(card.get("sppci_model", ""))
    return (best or None), best_name


def apple_unified_vram(ram_bytes: int) -> int:
    """Memory the GPU may use on Apple silicon (macOS's default wired limit)."""
    share = 0.75 if ram_bytes > 36 * GB else 2 / 3
    return int(ram_bytes * share)


def _detect_vram(ram_bytes):
    """(bytes or None, gpu name, source, unified)."""
    if sys.platform == "win32":
        size, name = _windows_vram()
        return size, name, "registry" if size else "", False
    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            return apple_unified_vram(ram_bytes), "Apple silicon", "unified", True
        size, name = _mac_intel_vram()
        return size, name, "system_profiler" if size else "", False
    size, name = _nvidia_smi_vram()
    if size:
        return size, name, "nvidia-smi", False
    size = _sysfs_vram()
    return size, "", "sysfs" if size else "", False


# ---------------------------------------------------------------- caching

_lock = threading.Lock()
_cached: Optional[HardwareInfo] = None
_cached_basic: Optional[HardwareInfo] = None


def basic() -> HardwareInfo:
    """Cores and RAM only: cheap, safe to call in every process."""
    global _cached_basic
    with _lock:
        if _cached_basic is None:
            logical, physical = _cores()
            _cached_basic = HardwareInfo(logical, physical, ram_total())
        return _cached_basic


def detect(refresh: bool = False) -> HardwareInfo:
    """Cores, RAM and graphics memory (probed once, then cached)."""
    global _cached
    if not refresh:
        with _lock:
            if _cached is not None:
                return _cached
    cpu_ram = basic()
    try:
        vram, name, source, unified = _detect_vram(cpu_ram.ram_total)
    except Exception as exc:  # never let a probe break the UI
        log.debug("Graphics memory probe failed: %s", exc)
        vram, name, source, unified = None, "", "", False
    info = HardwareInfo(
        cpu_ram.logical_cores,
        cpu_ram.physical_cores,
        cpu_ram.ram_total,
        vram,
        name,
        source,
        unified,
    )
    with _lock:
        _cached = info
    log.info("Hardware: %s", info.summary())
    return info


def cached() -> Optional[HardwareInfo]:
    """The full detection result if :func:`detect` already ran, else None."""
    with _lock:
        return _cached


def set_for_tests(info: Optional[HardwareInfo]) -> None:
    """Replace the detected hardware (None clears the cache)."""
    global _cached, _cached_basic
    with _lock:
        _cached = info
        _cached_basic = info


# ------------------------------------------------------- hardware baseline


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def baseline_values(info: HardwareInfo) -> dict:
    """Starting values scaled to this PC, as config text by "section.key".

    Only settings whose packaged default suits a mid-range PC but is wrong at
    either end. The user's own value always wins over these. Rules:

    * Memory cache: a quarter of RAM, 2 to 8 GB (16 GB RAM keeps today's 4).
    * Live builders: one per physical core, 4 to 12.
    * Background builders: one per three cores beyond two, 1 to 4.
    * Predictive build memory: 256 MB on 8 GB RAM up to 1 GB above 32 GB.
    """
    values = {}
    if info.ram_total > 0:
        ram = info.ram_gb
        values["cache.cache_mem_limit"] = str(_clamp(ram // 4, 2, 8))
        if ram <= 8:
            predictive = 256
        elif ram <= 16:
            predictive = 512
        elif ram <= 32:
            predictive = 768
        else:
            predictive = 1024
        values["autoortho.predictive_dds_memory_mb"] = str(predictive)
    if info.physical_cores > 0:
        cores = info.physical_cores
        values["autoortho.live_builder_concurrency"] = str(_clamp(cores, 4, 12))
        values["autoortho.background_builder_workers"] = str(
            _clamp((cores - 2) // 3, 1, 4)
        )
    return values

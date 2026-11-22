"""What this PC has: CPU cores, RAM and graphics memory.

Used for the hardware baseline (memory and worker defaults scaled to the PC)
and for the guards that compare a zoom target with the graphics card. Every
probe is best effort: an unknown value is None, never a guess, and callers
treat None as "do not warn".

Graphics memory: every probe lists the graphics adapters it can see, each
marked integrated or not, and the largest non-integrated card wins. An
integrated GPU never counts: what it reports is either a small BIOS carve-out
(512 MB) or a share of system RAM (16 GB), neither of which is a card's
memory. A PC with only an integrated GPU therefore reads as unknown.

* Windows: DXGI (``DedicatedVideoMemory``, the card's own memory) lists the
  adapters; the display adapter class in the registry
  (``HardwareInformation.qwMemorySize``) gives the exact size of the same card,
  and is the whole answer when DXGI is unavailable.
* Linux: NVML (``libnvidia-ml``, so no ``nvidia-smi`` on PATH is needed), then
  ``nvidia-smi``, plus every DRM card in sysfs: ``mem_info_vram_total``
  (amdgpu) and ``tile*/physical_vram_size_bytes`` (Intel xe, discrete only).
  Intel Arc on the older i915 driver and NVIDIA on nouveau publish no memory
  size and read as unknown.
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
from dataclasses import dataclass, replace
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
            parts.append(f"{size_text(self.vram)} {kind}graphics memory{name}")
        else:
            parts.append("graphics memory unknown")
        return ", ".join(parts)


def size_text(value: int) -> str:
    """Bytes as whole GB, or MB below 1 GB (512 MB must not read as "0 GB")."""
    if value < GB:
        return f"{value / MB:.0f} MB"
    return f"{value / GB:.0f} GB"


@dataclass(frozen=True)
class GpuCandidate:
    """One graphics adapter a probe found."""

    vram: int                       # bytes the probe reported
    name: str = ""
    source: str = ""
    integrated: bool = False

    def describe(self) -> str:
        kind = "integrated" if self.integrated else "card"
        name = self.name or "unnamed"
        return f"{name} {size_text(self.vram)} {kind} via {self.source}"


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

NVIDIA, AMD, INTEL = 0x10DE, 0x1002, 0x8086
_VENDOR_NAMES = {NVIDIA: "NVIDIA GPU", AMD: "AMD GPU", INTEL: "Intel GPU"}

# An AMD or Intel adapter reporting less than this is an integrated GPU's BIOS
# carve-out (usually 512 MB), not a card: X-Plane 12 runs on no card this small.
INTEGRATED_MAX = 2 * GB

# Intel's discrete cards carry an A- or B-series model ("Arc(TM) A770",
# "Arc(TM) A370M", "Arc(TM) Pro B60"); the integrated ones do not
# ("Arc(TM) Graphics", "Arc(TM) 140V GPU", "UHD Graphics 770", "Iris(R) Xe").
_INTEL_DISCRETE = re.compile(r"\bArc\b.*?\b(?:Pro\s+)?[AB]\d{2,3}[A-Z]?\b", re.I)
# AMD's integrated GPUs: "AMD Radeon(TM) Graphics", "Radeon(TM) 780M Graphics",
# "AMD Radeon(TM) Vega 8 Graphics", "AMD Radeon 610M". Cards name a series.
_AMD_INTEGRATED = re.compile(
    r"(?:Graphics\s*$|\bRadeon(?:\(TM\))?\s+\d{3}M\b)", re.I
)
_AMD_CARD_WORDS = re.compile(r"\b(?:RX|Pro|FirePro|R9)\b", re.I)


def looks_integrated(vendor: int, name: str, vram: int) -> bool:
    """Whether an adapter is an integrated GPU (judged by vendor, name, size)."""
    name = (name or "").strip()
    lowered = name.lower()
    if vendor == NVIDIA or "nvidia" in lowered:
        return False
    if vendor == INTEL or lowered.startswith("intel"):
        if name:
            return not _INTEL_DISCRETE.search(name)
        return vram < INTEGRATED_MAX
    if vendor == AMD or "radeon" in lowered:
        if (name and _AMD_INTEGRATED.search(name)
                and not _AMD_CARD_WORDS.search(name)):
            return True
    return vram < INTEGRATED_MAX


def pick_card(candidates) -> Optional[GpuCandidate]:
    """The largest adapter that is not integrated, or None."""
    cards = [c for c in candidates if c.vram > 0 and not c.integrated]
    return max(cards, key=lambda c: c.vram) if cards else None


def _registry_number(value) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFFFFFFFFFF if value >= 0 else value & 0xFFFFFFFF
    if isinstance(value, (bytes, bytearray)):
        if len(value) >= 8:
            return int.from_bytes(value[:8], "little")
        if len(value) >= 4:
            return int.from_bytes(value[:4], "little")
    return 0


# Windows ---------------------------------------------------------------------

_DXGI_ADAPTER_FLAG_SOFTWARE = 0x2
_IDXGIFACTORY1_ENUMADAPTERS1 = 12      # vtable slots
_IDXGIADAPTER1_GETDESC1 = 10
_IUNKNOWN_RELEASE = 2


def _dxgi_adapters():
    """Every hardware adapter DXGI lists, with its dedicated video memory.

    ``DedicatedVideoMemory`` is the card's own memory; an integrated GPU
    reports only its BIOS carve-out there. Creating the factory loads no
    driver, so this is cheap and safe in the UI process.
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class DXGI_ADAPTER_DESC1(ctypes.Structure):
        _fields_ = [
            ("Description", ctypes.c_wchar * 128),
            ("VendorId", wintypes.UINT),
            ("DeviceId", wintypes.UINT),
            ("SubSysId", wintypes.UINT),
            ("Revision", wintypes.UINT),
            ("DedicatedVideoMemory", ctypes.c_size_t),
            ("DedicatedSystemMemory", ctypes.c_size_t),
            ("SharedSystemMemory", ctypes.c_size_t),
            ("AdapterLuid", LUID),
            ("Flags", wintypes.UINT),
        ]

    def method(obj, slot, *argtypes):
        vtable = ctypes.cast(
            obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        return prototype(vtable[slot])

    try:
        create = ctypes.WinDLL("dxgi").CreateDXGIFactory1
    except (OSError, AttributeError):
        return []
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    iid_factory1 = GUID(  # {770aae78-f26f-4dba-a829-253c83d1b387}
        0x770AAE78, 0xF26F, 0x4DBA,
        (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
    )
    factory = ctypes.c_void_p()
    if create(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0 or not factory:
        return []
    found = []
    seen = set()
    try:
        enum_adapters = method(
            factory, _IDXGIFACTORY1_ENUMADAPTERS1,
            wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
        )
        for index in range(16):
            adapter = ctypes.c_void_p()
            if enum_adapters(factory, index, ctypes.byref(adapter)) != 0 or not adapter:
                break  # DXGI_ERROR_NOT_FOUND: no more adapters
            desc = DXGI_ADAPTER_DESC1()
            try:
                ok = method(
                    adapter, _IDXGIADAPTER1_GETDESC1,
                    ctypes.POINTER(DXGI_ADAPTER_DESC1),
                )(adapter, ctypes.byref(desc)) == 0
            finally:
                method(adapter, _IUNKNOWN_RELEASE)(adapter)
            if not ok or desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE:
                continue  # Microsoft Basic Render Driver
            # A virtual display adapter that renders on a card is listed with
            # that card's description under a LUID of its own, so duplicates
            # are expected; the largest card wins either way.
            luid = (desc.AdapterLuid.LowPart, desc.AdapterLuid.HighPart)
            if luid in seen:
                continue
            seen.add(luid)
            vram = int(desc.DedicatedVideoMemory)
            name = desc.Description.strip()
            found.append(GpuCandidate(
                vram, name, "dxgi", looks_integrated(desc.VendorId, name, vram)
            ))
    finally:
        method(factory, _IUNKNOWN_RELEASE)(factory)
    return found


_VENDOR_ID_RE = re.compile(r"ven_([0-9a-f]{4})", re.I)


def _registry_adapters():
    """Display adapters in the registry (the fallback when DXGI fails).

    The driver fills in ``HardwareInformation.qwMemorySize``; for an
    integrated GPU that is a share of system RAM, so integrated adapters are
    told apart by vendor and name instead.
    """
    try:
        import winreg
    except ImportError:
        return []
    found = []
    try:
        cards = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS)
    except OSError:
        return []

    def text(key, value_name):
        try:
            return str(winreg.QueryValueEx(key, value_name)[0])
        except OSError:
            return ""

    with cards:
        index = 0
        while True:
            try:
                key_name = winreg.EnumKey(cards, index)
            except OSError:
                break
            index += 1
            try:
                card = winreg.OpenKey(cards, key_name)
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
                if not size:
                    continue
                name = text(card, "DriverDesc").strip()
                match = _VENDOR_ID_RE.search(text(card, "MatchingDeviceId"))
                vendor = int(match.group(1), 16) if match else 0
                found.append(GpuCandidate(
                    size, name, "registry", looks_integrated(vendor, name, size)
                ))
    return found


# Linux -----------------------------------------------------------------------


def _subprocess_env():
    """The environment for system tools, without PyInstaller's library path.

    A frozen build points LD_LIBRARY_PATH at its bundled libraries, which can
    break tools such as ``nvidia-smi``; PyInstaller keeps the original in
    ``*_ORIG``.
    """
    if sys.platform == "win32" or not getattr(sys, "frozen", False):
        return None
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(key + "_ORIG", None)
        if original is not None:
            env[key] = original
        else:
            env.pop(key, None)
    return env


def _run(command, timeout=4.0):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_subprocess_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _nvml_adapters():
    """NVIDIA cards through NVML, which ships with the driver.

    Unlike ``nvidia-smi`` this needs nothing on PATH (Flatpak, AppImage and
    distro packages that leave the tool out).
    """
    import ctypes

    lib = None
    for lib_name in ("libnvidia-ml.so.1", "libnvidia-ml.so"):
        try:
            lib = ctypes.CDLL(lib_name)
            break
        except OSError:
            continue
    if lib is None:
        return []

    class Memory(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong),
            ("used", ctypes.c_ulonglong),
        ]

    def symbol(*names):
        for symbol_name in names:
            function = getattr(lib, symbol_name, None)
            if function is not None:
                return function
        raise AttributeError(names[0])

    try:
        init = symbol("nvmlInit_v2", "nvmlInit")
        get_count = symbol("nvmlDeviceGetCount_v2", "nvmlDeviceGetCount")
        get_handle = symbol(
            "nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex"
        )
        get_memory = symbol("nvmlDeviceGetMemoryInfo")
        get_name = symbol("nvmlDeviceGetName")
        shutdown = symbol("nvmlShutdown")
    except AttributeError:
        return []
    if init() != 0:
        return []
    found = []
    try:
        count = ctypes.c_uint()
        if get_count(ctypes.byref(count)) != 0:
            return []
        for index in range(min(count.value, 16)):
            handle = ctypes.c_void_p()
            if get_handle(ctypes.c_uint(index), ctypes.byref(handle)) != 0:
                continue
            memory = Memory()
            if get_memory(handle, ctypes.byref(memory)) != 0:
                continue
            buffer = ctypes.create_string_buffer(96)
            name = ""
            if get_name(handle, buffer, ctypes.c_uint(len(buffer))) == 0:
                name = buffer.value.decode("utf-8", "replace").strip()
            found.append(GpuCandidate(int(memory.total), name, "nvml"))
    finally:
        shutdown()
    return found


def _nvidia_smi_adapters():
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.total,name",
        "--format=csv,noheader,nounits",
    ])
    found = []
    for line in out.splitlines():
        fields = [part.strip() for part in line.split(",", 1)]
        try:
            size = int(float(fields[0])) * MB
        except (ValueError, IndexError):
            continue
        found.append(GpuCandidate(
            size, fields[1] if len(fields) > 1 else "", "nvidia-smi"
        ))
    return found


_SYSFS_DRM = "/sys/class/drm"
_CARD_RE = re.compile(r"card\d+")


def _read_text(path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _read_int(path) -> int:
    try:
        return int(_read_text(path), 0)  # "0x1002" and "12868124672"
    except ValueError:
        return 0


def _sysfs_adapters(root=_SYSFS_DRM):
    """Cards whose kernel driver publishes a memory size in sysfs.

    * amdgpu: ``mem_info_vram_total``. An APU publishes its BIOS carve-out
      there, which ``looks_integrated`` rejects by size.
    * Intel xe: ``tile*/physical_vram_size_bytes``, present on discrete
      cards only.
    """
    found = []
    seen = set()
    for card in sorted(glob.glob(os.path.join(root, "card*"))):
        if not _CARD_RE.fullmatch(os.path.basename(card)):
            continue  # connectors such as card0-HDMI-A-1
        device = os.path.join(card, "device")
        real = os.path.realpath(device)
        if real in seen:
            continue
        seen.add(real)
        vendor = _read_int(os.path.join(device, "vendor"))
        product = _read_text(os.path.join(device, "product_name"))
        label = product or _VENDOR_NAMES.get(vendor, "")
        vram = _read_int(os.path.join(device, "mem_info_vram_total"))
        if vram:
            found.append(GpuCandidate(
                vram, label, "sysfs",
                looks_integrated(vendor or AMD, product, vram),
            ))
            continue
        vram = sum(
            _read_int(path) for path in glob.glob(
                os.path.join(device, "tile*", "physical_vram_size_bytes")
            )
        )
        if vram:
            found.append(GpuCandidate(vram, label or "Intel GPU", "sysfs"))
    return found


# macOS -----------------------------------------------------------------------

_SIZE_RE = re.compile(r"([\d.]+)\s*(GB|MB)", re.IGNORECASE)


def _mac_adapters():
    out = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=6.0)
    try:
        cards = json.loads(out).get("SPDisplaysDataType", [])
    except (ValueError, AttributeError):
        return []
    found = []
    for card in cards:
        # Integrated GPUs list "spdisplays_vram_shared" instead.
        text = str(card.get("spdisplays_vram") or card.get("_spdisplays_vram") or "")
        match = _SIZE_RE.search(text)
        if not match:
            continue
        size = int(float(match.group(1)) * (GB if match.group(2).upper() == "GB" else MB))
        name = str(card.get("sppci_model", ""))
        builtin = card.get("sppci_bus") == "spdisplays_builtin"
        found.append(GpuCandidate(
            size, name, "system_profiler",
            builtin or looks_integrated(0, name, size),
        ))
    return found


def _is_apple_silicon() -> bool:
    if platform.machine() == "arm64":
        return True
    # An Intel build running under Rosetta still sits on Apple silicon.
    return _run(["sysctl", "-n", "hw.optional.arm64"]).strip() == "1"


def apple_unified_vram(ram_bytes: int) -> int:
    """Memory the GPU may use on Apple silicon (macOS's default wired limit)."""
    share = 0.75 if ram_bytes > 36 * GB else 2 / 3
    return int(ram_bytes * share)


# All -------------------------------------------------------------------------


def _safely(probe):
    try:
        return list(probe())
    except Exception as exc:  # one broken probe must not hide the others
        log.debug("Graphics probe %s failed: %s", probe.__name__, exc)
        return []


def with_registry_sizes(dxgi, registry):
    """DXGI's adapters, sized by the registry entry for the same card.

    DXGI leaves out what Windows reserves (a 32 GB card reads 31.4 GB); the
    registry has the size on the box. DXGI still decides which adapters exist
    and which are integrated: the registry also keeps disabled and removed
    adapters, and gives an integrated GPU a share of system RAM.
    """
    exact = {}
    for card in registry:
        if card.name and not card.integrated:
            exact[card.name] = max(exact.get(card.name, 0), card.vram)
    sized = []
    for card in dxgi:
        size = exact.get(card.name, 0)
        if not card.integrated and card.vram <= size <= card.vram * 1.25:
            card = replace(card, vram=size)
        sized.append(card)
    return sized


def probe_adapters():
    """Every graphics adapter this OS's probes can see."""
    if sys.platform == "win32":
        registry = _safely(_registry_adapters)
        dxgi = _safely(_dxgi_adapters)
        return with_registry_sizes(dxgi, registry) if dxgi else registry
    if sys.platform == "darwin":
        return _safely(_mac_adapters)
    nvidia = _safely(_nvml_adapters) or _safely(_nvidia_smi_adapters)
    return nvidia + _safely(_sysfs_adapters)


def _detect_vram(ram_bytes):
    """(bytes or None, gpu name, source, unified)."""
    if sys.platform == "darwin" and _is_apple_silicon():
        return apple_unified_vram(ram_bytes), "Apple silicon", "unified", True
    candidates = probe_adapters()
    counts = {}
    for candidate in candidates:
        text = candidate.describe()
        counts[text] = counts.get(text, 0) + 1
    log.info(
        "Graphics adapters: %s",
        "; ".join(
            text if n == 1 else f"{text} (x{n})" for text, n in counts.items()
        ) or "none found",
    )
    card = pick_card(candidates)
    if card is None:
        return None, "", "", False
    return card.vram, card.name, card.source, False


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

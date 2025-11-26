# winsetup.py
import os
import sys
import glob
import logging
import shutil
import subprocess
from ctypes import CDLL
from ctypes.util import find_library

from autoortho.core.config import CFG

log = logging.getLogger(__name__)

try:
    import winreg as reg
except ImportError:
    log.error("Failed to import winreg")

_PLACEHOLDER = ".AO_PLACEHOLDER"
_ALLOWED_PLACEHOLDER = {"Earth nav data", "terrain", "textures", _PLACEHOLDER, "desktop.ini"}


def _looks_like_placeholder(path):
    try:
        entries = set(os.listdir(path))
    except Exception:
        return False
    # Empty or subset of allowed placeholder files/dirs
    return entries.issubset(_ALLOWED_PLACEHOLDER)


def _is_64bit_python():
    return sys.maxsize > 0xFFFFFFFF


def _arch():
    return "x64" if _is_64bit_python() else "x86"


def _try_load(dll_path):
    """Try to load a DLL in a safe way and return a canonical path on success."""
    try:
        # On Py 3.8+ add directory to the DLL search path to resolve dependencies
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.dirname(dll_path))
        CDLL(dll_path)
        return os.path.abspath(dll_path)
    except Exception as e:
        log.debug(f"Probe load failed for {dll_path}: {e}")
        return None

def _find_winfsp_from_registry():
    if not reg:
        return None
    try:
        with reg.OpenKey(reg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WinFsp", 0, reg.KEY_READ | reg.KEY_WOW64_32KEY) as k:
            install_dir, _ = reg.QueryValueEx(k, "InstallDir")
        if not install_dir or not os.path.isdir(install_dir):
            return None
        # WinFsp 2.x places binaries under SxS\sxs.*\bin; also check legacy bin
        candidates = []
        # Legacy:
        legacy = os.path.join(install_dir, "bin", f"winfsp-{_arch()}.dll")
        candidates.append(legacy)
        # SxS:
        sxs_glob = os.path.join(install_dir, "SxS", "sxs.*", "bin", f"winfsp-{_arch()}.dll")
        candidates.extend(glob.glob(sxs_glob))
        for c in candidates:
            if os.path.exists(c):
                loaded = _try_load(c)
                if loaded:
                    return loaded
        return None
    except Exception as e:
        log.debug(f"WinFsp registry probe failed: {e}")
        return None


def _find_winfsp_tools_from_registry():
    """
    Try to locate WinFsp CLI tools (fsptool/launchctl) from the registry InstallDir.
    Returns a list of absolute paths to candidate executables.
    """
    if not reg:
        return []
    tools = []
    try:
        with reg.OpenKey(reg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WinFsp", 0, reg.KEY_READ | reg.KEY_WOW64_32KEY) as k:
            install_dir, _ = reg.QueryValueEx(k, "InstallDir")
        if not install_dir or not os.path.isdir(install_dir):
            return []
        # Legacy bin and SxS bin
        bin_dirs = [
            os.path.join(install_dir, "bin"),
        ]
        bin_dirs.extend(glob.glob(os.path.join(install_dir, "SxS", "sxs.*", "bin")))
        exe_names = ("fsptool-x64.exe", "fsptool.exe", "launchctl-x64.exe", "launchctl.exe")
        for b in bin_dirs:
            for exe in exe_names:
                cand = os.path.join(b, exe)
                if os.path.exists(cand):
                    tools.append(cand)
    except Exception as e:
        log.debug(f"WinFsp tools registry probe failed: {e}")
    return tools


def _find_dokan_fuse():
    # Try common names for Dokan FUSE wrapper (v2 and legacy)
    for name in ("dokanfuse2.dll", "dokanfuse.dll"):
        p = find_library(name)
        if p:
            loaded = _try_load(p if os.path.isabs(p) else name)
            if loaded:
                return loaded
        # Fallback to system locations
        system32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", name)
        if os.path.exists(system32):
            loaded = _try_load(system32)
            if loaded:
                return loaded
    return None


def find_win_libs():
    """
    Return (mode, dll_path) where mode is 'winfsp-FUSE' or 'dokan-FUSE'.
    Sets FUSE_LIBRARY_PATH to the discovered DLL.
    """
    fuse_libs = []

    log.info("Looking for Dokan ...")
    dokan = _find_dokan_fuse()
    if dokan:
        log.info(f"Dokan found at {dokan}")
        fuse_libs.append(("dokan-FUSE", dokan))
    else:
        log.info("Dokan not found.")

    log.info("Looking for WinFSP ...")
    winfsp = _find_winfsp_from_registry()
    if winfsp:
        log.info(f"Found WinFSP at {winfsp}")
        fuse_libs.append(("winfsp-FUSE", winfsp))
    else:
        log.info("WinFSP not found.")

    if not fuse_libs:
        log.error("No required Windows libs detected! Please install Dokan or WinFsp.")
        return None, None

    # Honor preference but keep loadability: pick preferred if present, else fallback.
    prefer_winfsp = getattr(CFG, "windows", None) and getattr(CFG.windows, "prefer_winfsp", False)
    ordered = sorted(
        fuse_libs,
        key=lambda x: (0 if (prefer_winfsp and x[0].startswith("winfsp"))
                       or (not prefer_winfsp and x[0].startswith("dokan")) else 1)
    )
    fusemode, fuselib = ordered[0]
    os.environ["FUSE_LIBRARY_PATH"] = fuselib  # many FUSE shims honor this

    log.info(f"Will use detected {fusemode} with libs {fuselib}")
    return fusemode, fuselib


def setup_winfsp_mount(path):
    """
    Ensure mountpoint is in a WinFsp-safe state:
    - It must NOT exist before mount.
    - If it exists and looks like our placeholder, remove it.
    - Otherwise, refuse to proceed to avoid data loss.
    """
    path = os.path.expanduser(path)
    if os.path.lexists(path):
        if not os.path.isdir(path):
            log.error(f"Mount point ({path}) exists but is not a directory.")
            return False
        if _looks_like_placeholder(path):
            log.info(f"Removing placeholder directory before WinFsp mount: {path}")
            try:
                shutil.rmtree(path)
            except Exception as e:
                log.error(f"Failed to remove placeholder {path}: {e}")
                return False
        else:
            log.error(
                f"Mount point ({path}) already exists and is not a known placeholder. "
                "WinFsp requires it to not exist; please move or rename it manually."
            )
            return False
    # At this point the path must NOT exist; WinFsp will create/delete it.
    return True


def setup_dokan_mount(path):
    """
    For Dokan FUSE directory mounts, require an existing, empty directory.
    (Most Dokan apps mount to a drive letter; directory mounts should be empty.)
    """
    path = os.path.expanduser(path)
    if os.path.lexists(path):
        if not os.path.isdir(path):
            log.error(f"Mount point {path} exists but is not a directory.")
            return False
        # Treat placeholder as “empty enough”
        if not _looks_like_placeholder(path):
            if os.listdir(path):  # non-empty and not our placeholder
                log.error(f"Mount point {path} exists and is not empty.")
                return False
    else:
        try:
            os.makedirs(path, exist_ok=True)
            log.info(f"Created mountpoint for Dokan: {path}")
        except OSError as e:
            log.error(f"Failed creating mountpoint {path}: {e}")
            return False
    return True

def force_unmount(path):
    """
    Best-effort force unmount on Windows for both WinFsp and Dokan.
    No exception if not installed.
    """
    try:        # Dokan: dokanctl.exe /u <mount> (drive letter or mount dir)
        if shutil.which("dokanctl.exe"):
            subprocess.run(["dokanctl.exe", "/u", path],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # WinFsp: fsptool umount <path>
        # Prefer PATH tools, then registry-located tools
        candidates = []
        for t in ("fsptool-x64.exe", "fsptool.exe", "launchctl-x64.exe", "launchctl.exe"):
            p = shutil.which(t)
            if p:
                candidates.append(p)
        if not candidates:
            candidates.extend(_find_winfsp_tools_from_registry())
        for tool in candidates:
            try:
                subprocess.run([tool, "umount", path],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except Exception:
                continue
    except Exception as e:
        log.debug(f"force_unmount({path}) failed (ignored): {e}")


def cleanup_orphaned_mounts():
    """
    Cleanup any orphaned WinFSP/Dokan mounts from previous crashes.
    
    This should be called on startup to ensure clean state.
    When AutoOrtho crashes, the virtual filesystem can be left mounted
    but not responding, causing EXCEPTION_IN_PAGE_ERROR in X-Plane.
    """
    import glob
    from autoortho.core.config import CFG
    
    try:
        # Get configured mount paths from scenery packs
        if not hasattr(CFG, 'scenery_packs') or not CFG.scenery_packs:
            return
        
        log.info("Checking for orphaned mounts from previous crashes...")
        
        for pack in CFG.scenery_packs:
            if not hasattr(pack, 'textures'):
                continue
            
            texture_path = pack.textures
            if not texture_path or not os.path.exists(texture_path):
                continue
            
            # Check if this is a symlink/junction to a mount point
            if os.path.islink(texture_path):
                target = os.readlink(texture_path)
                
                # Try to access the mount - if it fails, it's orphaned
                try:
                    # Attempt to list directory - this will fail for orphaned mounts
                    os.listdir(target)
                    log.debug(f"Mount point {target} is healthy")
                except (OSError, PermissionError) as e:
                    log.warning(f"Detected orphaned mount at {target}: {e}")
                    log.info(f"Attempting to cleanup orphaned mount: {target}")
                    force_unmount(target)
                    
                    # Give it a moment to cleanup
                    import time
                    time.sleep(1)
                    
                    # Verify cleanup
                    try:
                        os.listdir(target)
                        log.info(f"Mount point {target} is now accessible")
                    except:
                        log.warning(f"Mount point {target} still inaccessible after cleanup")
        
        log.info("Orphaned mount cleanup complete")
        
    except Exception as e:
        log.warning(f"Failed to cleanup orphaned mounts: {e}")
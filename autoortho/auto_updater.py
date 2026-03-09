"""
Auto-updater for AutoOrtho.

Downloads, extracts, and installs updates from GitHub releases.
Pure logic module with no Qt dependency — all UI interaction happens
via callbacks and return values.
"""

import os
import re
import sys
import time
import shutil
import logging
import zipfile
import tarfile
import platform
import subprocess
import tempfile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reuse the shared download session from downloader.py
# ---------------------------------------------------------------------------

try:
    from autoortho.downloader import _get_download_session
except ImportError:
    from downloader import _get_download_session

try:
    from autoortho.utils.constants import system_type
except ImportError:
    from utils.constants import system_type


# ---------------------------------------------------------------------------
# 1. Asset selection
# ---------------------------------------------------------------------------

def _get_linux_codename():
    """Read UBUNTU_CODENAME from /etc/os-release, or None."""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("UBUNTU_CODENAME="):
                    return line.strip().split("=", 1)[1]
    except (OSError, IndexError):
        pass
    return None


def find_platform_asset(assets):
    """Select the correct release asset for the current platform.

    Parameters
    ----------
    assets : list[dict]
        The ``assets`` array from a GitHub release API response.

    Returns
    -------
    tuple[str, str] | tuple[None, None]
        ``(browser_download_url, asset_name)`` or ``(None, None)``.
    """
    if not assets:
        return None, None

    if system_type == "linux":
        pattern = re.compile(r"autoortho_linux_.*\.tar\.gz$", re.IGNORECASE)
        codename = _get_linux_codename()
        candidates = [
            a for a in assets
            if pattern.search(a.get("name", ""))
        ]
        if not candidates:
            return None, None
        # Prefer the build matching the local Ubuntu codename
        if codename:
            for a in candidates:
                if codename in a["name"]:
                    return a["browser_download_url"], a["name"]
        # Fallback: first Linux asset
        return candidates[0]["browser_download_url"], candidates[0]["name"]

    elif system_type == "darwin":
        pattern = re.compile(r"AutoOrtho_mac_.*\.zip$", re.IGNORECASE)
        for a in assets:
            if pattern.search(a.get("name", "")):
                return a["browser_download_url"], a["name"]
        return None, None

    elif system_type == "windows":
        # Use the .zip, NOT the .exe NSIS installer
        pattern = re.compile(r"autoortho_win_.*\.zip$", re.IGNORECASE)
        for a in assets:
            if pattern.search(a.get("name", "")):
                return a["browser_download_url"], a["name"]
        return None, None

    return None, None


# ---------------------------------------------------------------------------
# 2. Install directory detection
# ---------------------------------------------------------------------------

def get_install_dir():
    """Return the top-level directory of the current installation.

    - Linux / Windows: the directory containing the ``autoortho`` executable.
    - macOS: the ``.app`` bundle path (e.g. ``/Applications/AutoOrtho.app``).
    """
    exe = os.path.abspath(sys.executable)

    if system_type == "darwin":
        # Walk up from  .../AutoOrtho.app/Contents/MacOS/autoortho
        parts = exe.split(os.sep)
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].endswith(".app"):
                return os.sep.join(parts[: i + 1])
        # Fallback if not inside a .app bundle
        return os.path.dirname(exe)

    # Linux / Windows: the folder that contains the executable
    return os.path.dirname(exe)


# ---------------------------------------------------------------------------
# 3. Permission check
# ---------------------------------------------------------------------------

def check_write_permission(install_dir):
    """Return True if we can write to *install_dir*."""
    try:
        # For macOS .app bundles, check the parent directory (we need to
        # remove/move the bundle itself).
        check_dir = install_dir
        if system_type == "darwin" and install_dir.endswith(".app"):
            check_dir = os.path.dirname(install_dir)

        fd, tmp = tempfile.mkstemp(dir=check_dir, prefix=".ao_perm_check_")
        os.close(fd)
        os.remove(tmp)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 4. Download
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5


def download_update(asset_url, asset_name, download_dir,
                    progress_callback=None, cancel_check=None):
    """Download a release archive to *download_dir*.

    Parameters
    ----------
    asset_url : str
        ``browser_download_url`` from the GitHub API.
    asset_name : str
        Filename for the downloaded archive.
    download_dir : str
        Directory for temporary downloads (``CFG.paths.download_dir``).
    progress_callback : callable, optional
        Called with a dict: ``{status, pcnt_done, MBps, bytes_downloaded, bytes_total}``.
    cancel_check : callable, optional
        Returns True if the download should be cancelled.

    Returns
    -------
    str
        Path to the completed archive file.

    Raises
    ------
    RuntimeError
        If all retries are exhausted or the download is cancelled.
    """
    os.makedirs(download_dir, exist_ok=True)
    dest = os.path.join(download_dir, asset_name)
    part = dest + ".part"

    session = _get_download_session()
    headers = {"User-Agent": "autoortho4xplane-auto-updater"}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = session.get(asset_url, headers=headers, stream=True,
                               timeout=30)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            fetched = 0
            start = time.monotonic()

            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1_048_576):
                    if cancel_check and cancel_check():
                        resp.close()
                        _safe_remove(part)
                        raise RuntimeError("Download cancelled by user")
                    f.write(chunk)
                    fetched += len(chunk)
                    if progress_callback and total > 0:
                        elapsed = max(time.monotonic() - start, 0.001)
                        progress_callback({
                            "status": f"Downloading... "
                                      f"{fetched / 1_048_576:.1f} / "
                                      f"{total / 1_048_576:.1f} MB",
                            "pcnt_done": fetched / total * 100,
                            "MBps": fetched / elapsed / 1_048_576,
                            "bytes_downloaded": fetched,
                            "bytes_total": total,
                        })

            os.replace(part, dest)
            log.info("Update downloaded: %s", dest)
            return dest

        except RuntimeError:
            raise  # re-raise cancellation
        except Exception as exc:
            log.warning("Download attempt %d/%d failed: %s",
                        attempt, _MAX_RETRIES, exc)
            _safe_remove(part)
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Failed to download update after {_MAX_RETRIES} attempts"
    )


# ---------------------------------------------------------------------------
# 5. Extract
# ---------------------------------------------------------------------------

def extract_update(archive_path, staging_dir):
    """Extract *archive_path* into *staging_dir*.

    Returns the path to the top-level directory inside the archive.
    """
    # Clean previous staging
    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(staging_dir)
    elif archive_path.endswith(".tar.gz") or archive_path.endswith(".tgz"):
        with tarfile.open(archive_path) as tf:
            try:
                tf.extractall(staging_dir, filter="data")
            except TypeError:
                # Python < 3.12 does not support the filter parameter
                tf.extractall(staging_dir)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive_path}")

    # Find the single top-level directory inside staging
    entries = os.listdir(staging_dir)
    if len(entries) == 1 and os.path.isdir(
            os.path.join(staging_dir, entries[0])):
        return os.path.join(staging_dir, entries[0])
    # If the archive extracted flat files, staging_dir itself is the root
    return staging_dir


# ---------------------------------------------------------------------------
# 6. Apply (swap)
# ---------------------------------------------------------------------------

def apply_update(staging_dir, install_dir):
    """Replace *install_dir* with the contents of *staging_dir*.

    On Windows this writes a batch script and prepares staging but the
    actual swap happens after the process exits (see ``launch_and_exit``).
    """
    extracted_root = _find_extracted_root(staging_dir)

    if system_type == "windows":
        # On Windows the running executable is locked.  The swap is
        # deferred to a batch script executed by launch_and_exit().
        # Store the staging path for launch_and_exit to use.
        _write_state(staging_dir, install_dir)
        log.info("Windows: update staged at %s, swap deferred to restart",
                 staging_dir)
        return

    # Linux / macOS — we can swap immediately
    if system_type == "darwin":
        _apply_macos(extracted_root, install_dir)
    else:
        _apply_linux(extracted_root, install_dir)


def _find_extracted_root(staging_dir):
    """Return the single top-level dir inside *staging_dir*, or staging_dir."""
    entries = os.listdir(staging_dir)
    if len(entries) == 1 and os.path.isdir(
            os.path.join(staging_dir, entries[0])):
        return os.path.join(staging_dir, entries[0])
    return staging_dir


def _apply_linux(extracted_root, install_dir):
    """Replace *install_dir* with *extracted_root* on Linux."""
    log.info("Linux: replacing %s with %s", install_dir, extracted_root)
    shutil.rmtree(install_dir)
    shutil.move(extracted_root, install_dir)
    # Ensure the main binary is executable
    exe = os.path.join(install_dir, "autoortho")
    if os.path.isfile(exe):
        os.chmod(exe, 0o755)


def _apply_macos(extracted_root, install_dir):
    """Replace *install_dir* (.app bundle) with *extracted_root* on macOS."""
    # The archive contains AutoOrtho.app — find it
    app_dir = extracted_root
    if not extracted_root.endswith(".app"):
        for entry in os.listdir(extracted_root):
            if entry.endswith(".app"):
                app_dir = os.path.join(extracted_root, entry)
                break

    log.info("macOS: replacing %s with %s", install_dir, app_dir)
    shutil.rmtree(install_dir)
    shutil.move(app_dir, install_dir)

    # Remove quarantine attribute so Gatekeeper doesn't block
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", install_dir],
            capture_output=True, timeout=10,
        )
    except Exception as exc:
        log.warning("Could not remove quarantine xattr: %s", exc)


# ---------------------------------------------------------------------------
# 7. Restart
# ---------------------------------------------------------------------------

_STATE_FILE = ".ao_update_state"


def _write_state(staging_dir, install_dir):
    """Persist staging/install paths for the Windows batch script."""
    state_path = os.path.join(
        os.path.dirname(staging_dir), _STATE_FILE
    )
    with open(state_path, "w") as f:
        f.write(f"{staging_dir}\n{install_dir}\n")


def launch_and_exit(install_dir):
    """Restart the application from *install_dir*.

    On Linux this replaces the current process via ``os.execv``.
    On macOS it launches the ``.app`` bundle and exits.
    On Windows it spawns a batch script that waits for this process to die,
    swaps directories, and relaunches.

    This function does **not** return.
    """
    if system_type == "linux":
        new_exe = os.path.join(install_dir, "autoortho")
        log.info("Linux: restarting via os.execv(%s)", new_exe)
        os.execv(new_exe, [new_exe])

    elif system_type == "darwin":
        log.info("macOS: restarting via 'open -n %s'", install_dir)
        subprocess.Popen(["open", "-n", install_dir])
        sys.exit(0)

    elif system_type == "windows":
        state_path = os.path.join(
            os.path.expanduser("~"), ".autoortho-data", "downloads",
            _STATE_FILE,
        )
        if not os.path.isfile(state_path):
            log.error("Windows: update state file not found at %s",
                      state_path)
            sys.exit(1)

        with open(state_path) as f:
            lines = f.read().strip().splitlines()
        staging_dir, install_dir = lines[0], lines[1]
        extracted_root = _find_extracted_root(staging_dir)

        # Find the actual content directory inside the extracted root
        # (e.g., autoortho_release/ inside the zip)
        content_dir = extracted_root

        pid = os.getpid()
        bat_path = os.path.join(
            os.path.dirname(staging_dir), "ao_update.bat"
        )

        bat_content = f"""@echo off
:wait
tasklist /FI "PID eq {pid}" 2>nul | find /i "autoortho" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
rmdir /s /q "{install_dir}"
move /Y "{content_dir}" "{install_dir}"
rmdir /s /q "{staging_dir}" 2>nul
del "{state_path}" 2>nul
start "" "{os.path.join(install_dir, 'autoortho.exe')}"
del "%~f0"
"""
        with open(bat_path, "w") as f:
            f.write(bat_content)

        log.info("Windows: launching update batch script %s", bat_path)
        # CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["cmd", "/c", bat_path],
            creationflags=0x08000000,
        )
        sys.exit(0)


# ---------------------------------------------------------------------------
# 8. Cleanup
# ---------------------------------------------------------------------------

def cleanup_staging(download_dir):
    """Remove leftover staging directory and state file (best-effort)."""
    staging = os.path.join(download_dir, "ao_update_staging")
    _safe_rmtree(staging)
    state = os.path.join(download_dir, _STATE_FILE)
    _safe_remove(state)


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _safe_rmtree(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
    except OSError:
        pass

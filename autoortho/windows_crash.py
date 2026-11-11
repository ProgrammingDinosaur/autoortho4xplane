"""Launch the bundled out-of-process Windows crash collector.

Only local diagnostics are written. Neither command lines, configuration nor
environment values are copied into the report. The dump itself contains native
thread stacks and may include memory referenced by those stacks.
"""

import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid

if __package__:
    from .native_build import verify_bundle
else:
    from native_build import verify_bundle


log = logging.getLogger(__name__)
READY_ENV = "AO_CRASH_READY_EVENT"
WAIT_TIMEOUT_MS = 15000
MAX_CRASH_REPORTS = 3


def native_directory():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
        candidate = base / "autoortho" / "aopipeline" / "lib" / "windows"
        if candidate.is_dir():
            return candidate
        return base / "aopipeline" / "lib" / "windows"
    return Path(__file__).resolve().parent / "aopipeline" / "lib" / "windows"


def _kernel32():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def wait_for_collector():
    """Gate native imports until attachment, with a bounded fail-open wait."""
    name = os.environ.pop(READY_ENV, None)
    if sys.platform != "win32" or not name:
        return
    kernel = _kernel32()
    event = kernel.OpenEventW(0x00100000, False, name)  # SYNCHRONIZE
    if not event:
        return
    try:
        if kernel.WaitForSingleObject(event, WAIT_TIMEOUT_MS) != 0:
            print("Native crash collector did not become ready; continuing startup", file=sys.stderr)
    finally:
        kernel.CloseHandle(event)


def check_native_build():
    """Reject a mismatched packaged DLL before any native imports."""
    if sys.platform != "win32":
        return
    try:
        manifest = verify_bundle(native_directory(), expected_platform="windows")
        print(f"Verified Windows native build: {manifest['revision']}", file=sys.stderr)
    except (OSError, ValueError, KeyError) as exc:
        if getattr(sys, "frozen", False):
            raise RuntimeError("Packaged Windows native build failed verification") from exc
        print(f"Development native build is unverified: {exc}", file=sys.stderr)


def prune_reports(directory):
    """Keep three completed bundles; never delete an active collector's files."""
    directory = Path(directory)
    completed = sorted(directory.glob("*.complete"), key=lambda p: p.stat().st_mtime, reverse=True)
    for marker in completed[MAX_CRASH_REPORTS:]:
        prefix = marker.name.removesuffix(".complete")
        for suffix in (".dmp", ".crash.json", ".modules.jsonl", ".build.json", ".complete"):
            path = directory / (prefix + suffix)
            if not path.is_symlink():
                path.unlink(missing_ok=True)


class WindowsCrashCollector:
    """Owns the readiness event and companion process for one worker."""

    def __init__(self, directory):
        self.process = None
        self.prefix = None
        self.report_logged = False
        self.kernel = _kernel32()
        self.event_name = "Local\\AutoOrthoCrash-" + uuid.uuid4().hex
        self.event = self.kernel.CreateEventW(None, True, False, self.event_name)
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        self.directory = Path(directory)

    def start(self, worker_pid, stderr):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            prune_reports(self.directory)
            prefix = self.directory / ("worker-" + str(worker_pid) + "-" + uuid.uuid4().hex)
            self.prefix = prefix
            library_dir = native_directory()
            manifest = verify_bundle(library_dir, expected_platform="windows")
            (prefix.with_suffix(".build.json")).write_text(
                json.dumps({"worker_pid": worker_pid, "native_build": manifest}, indent=2) + "\n",
                encoding="utf-8",
            )
            # The executable and all arguments come from this installation and
            # AO-created identifiers, never a shell or a configurable command.
            self.process = subprocess.Popen(
                [str(library_dir / "ao-crash-collector.exe"), str(worker_pid),
                 str(os.getpid()), self.event_name, str(prefix)],
                stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                close_fds=True,
            )
            log.info("Native crash collector started for worker pid %s; reports: %s", worker_pid, self.directory)
        except Exception:
            self.kernel.SetEvent(self.event)
            # no-mse: rule=sdk-compliance reason="Local crash capture diagnostics"
            log.exception("Could not start native crash collector for worker pid %s", worker_pid)

    def close(self):
        # Only called after the worker exits. A captured fatal exception has
        # already been written and continued before the worker can exit.
        try:
            if self.process is not None:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=2)
            if self.prefix is not None and not self.report_logged:
                report = self.prefix.with_suffix(".crash.json")
                if report.is_file():
                    # no-mse: rule=sdk-compliance reason="Local crash capture diagnostics"
                    log.error("Native worker crash evidence saved: %s", report)
                    self.report_logged = True
                else:
                    # Attach/startup failure may leave provenance without a
                    # crash. It has already been explained in the worker log.
                    self.prefix.with_suffix(".build.json").unlink(missing_ok=True)
        finally:
            if self.event:
                self.kernel.CloseHandle(self.event)
                self.event = None

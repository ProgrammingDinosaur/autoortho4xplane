"""Start processes outside the running AutoOrtho: the helper and the new app."""

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

HELPER_MODE = "apply_update"
PLAN_ENV = "AO_UPDATE_PLAN"

# Variables AutoOrtho sets for its own child processes; a separately started
# AutoOrtho must not inherit them.
_AO_CHILD_VARS = (
    "AO_RUN_MODE",
    PLAN_ENV,
    "AO_WORKER_CONTROL_STDIN",
    "AO_STATS_ADDR",
    "AO_STATS_AUTH",
    "AO_LOG_ADDR",
    "_MEIPASS2",
)

# Windows process creation flags.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def clean_env(environ=None):
    """A copy of the environment for an independent AutoOrtho process.

    PyInstaller marks its own children through ``_PYI_*`` variables and, on
    Linux, prepends the bundle to LD_LIBRARY_PATH (saving the original in
    ``*_ORIG``). PYINSTALLER_RESET_ENVIRONMENT=1 tells the started program's
    bootloader that it is a new instance, not a child of this one.
    """
    env = dict(os.environ if environ is None else environ)
    for key in list(env):
        if key in _AO_CHILD_VARS or key.startswith("_PYI_"):
            env.pop(key, None)
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(key + "_ORIG", None)
        if original is not None:
            env[key] = original
        elif getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _popen_detached(command, env, cwd):
    kwargs = dict(
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(
                command,
                creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB,
                **kwargs,
            )
        except OSError:
            # The job this process runs in does not allow breaking away.
            return subprocess.Popen(command, creationflags=flags, **kwargs)
    return subprocess.Popen(command, start_new_session=True, **kwargs)


def helper_command(helper_executable):
    return [str(helper_executable)]


def spawn_helper(helper_executable, plan_file):
    """Start ``helper_executable`` in update-helper mode, detached."""
    env = clean_env()
    env["AO_RUN_MODE"] = HELPER_MODE
    env[PLAN_ENV] = str(plan_file)
    cwd = os.path.expanduser("~")
    log.info("Starting update helper %s with plan %s", helper_executable, plan_file)
    return _popen_detached(helper_command(helper_executable), env, cwd)


def relaunch_command(platform, root, executable):
    """How to start the app at ``root`` (``executable`` inside it)."""
    if platform == "macos":
        # -W keeps "open" running until the app quits, so the helper can see
        # an app that exits right away; -n starts a new instance.
        return ["/usr/bin/open", "-n", "-W", str(root)]
    return [str(executable)]


def start_app(command):
    """Start the (new) app; returns the Popen to watch."""
    env = clean_env()
    return _popen_detached(command, env, os.path.expanduser("~"))


def run_elevated_and_wait(executable, arguments, timeout=None):
    """Windows: run ``executable`` through ShellExecuteEx and wait for it.

    Lets Windows show the administrator prompt an installer's manifest asks
    for (CreateProcess would fail with ERROR_ELEVATION_REQUIRED). Returns the
    exit code, or None when the prompt was declined.
    """
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NO_CONSOLE = 0x00008000
    ERROR_CANCELLED = 1223
    INFINITE = 0xFFFFFFFF

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "open"
    info.lpFile = str(executable)
    # NSIS reads its own options (/S, /INSTDIR="...") from the raw command
    # line, so a prepared string is passed through untouched.
    if isinstance(arguments, str):
        info.lpParameters = arguments
    else:
        info.lpParameters = subprocess.list2cmdline([str(a) for a in arguments])
    info.lpDirectory = str(Path(executable).parent)
    info.nShow = 1  # SW_SHOWNORMAL

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            return None
        raise OSError(error, f"Could not start {executable} (error {error}).")
    if not info.hProcess:
        raise OSError("The installer started without a process handle.")
    try:
        wait_ms = INFINITE if timeout is None else int(timeout * 1000)
        kernel32.WaitForSingleObject(info.hProcess, wait_ms)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise OSError("Could not read the installer's exit code.")
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)

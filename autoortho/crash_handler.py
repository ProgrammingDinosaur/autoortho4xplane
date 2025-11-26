#!/usr/bin/env python3
"""
Crash handler for C-level crashes (segfaults, access violations).

Key insight: Python's `signal` module does NOT work for C-level crashes
in Nuitka-compiled code. We MUST use `faulthandler` which is designed
for this purpose and works with compiled C code.

Usage:
    from crash_handler import install_crash_handler
    install_crash_handler()
"""

import os
import sys

# Enable faulthandler to stderr immediately (silent - no user-visible output)
try:
    import faulthandler
    faulthandler.enable(file=sys.stderr, all_threads=True)
except Exception:
    pass  # Will be retried in install_crash_handler()

import signal
import logging
import traceback
from datetime import datetime

# Try to import LOGS_DIR, but have a fallback
try:
    from utils.constants import LOGS_DIR
except Exception:
    # Fallback if import fails
    LOGS_DIR = os.path.join(os.path.expanduser("~"), ".autoortho-data", "logs")


log = logging.getLogger(__name__)

# Track if crash handler is installed
_crash_handler_installed = False

# Global file handle for faulthandler (must stay open!)
_fault_log_file = None

# Breadcrumb file for tracking last operation before crash
_breadcrumb_file = None
_breadcrumb_enabled = False


def breadcrumb(operation):
    """
    Write a breadcrumb showing current operation.

    Call this before risky operations (C calls, etc) so that if
    a crash happens, we know what was being done.

    Example:
        breadcrumb("compressing tile 12345_6789 mipmap 3")
        _ispc.CompressBlocksBC3(...)  # If this crashes, breadcrumb shows it
    """
    global _breadcrumb_file, _breadcrumb_enabled

    if not _breadcrumb_enabled or _breadcrumb_file is None:
        return

    try:
        # Seek to beginning and overwrite (keeps file small)
        _breadcrumb_file.seek(0)
        _breadcrumb_file.write(f"{datetime.now().isoformat()} | {operation}\n")
        _breadcrumb_file.flush()
        os.fsync(_breadcrumb_file.fileno())  # Force to disk
    except Exception:
        pass


def _get_crash_log_path():
    """Get path to crash log file."""
    crash_dir = LOGS_DIR
    os.makedirs(crash_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(crash_dir, f"crash_{timestamp}.log")


def _write_crash_info(crash_type, sig_info=None, frame_info=None):
    """Write crash information to log file and stderr."""
    crash_log = _get_crash_log_path()

    if frame_info:
        stack_trace = ''.join(traceback.format_stack(frame_info))
    else:
        stack_trace = ''.join(traceback.format_stack())

    crash_msg = f"""
{'=' * 70}
AUTOORTHO CRASH DETECTED
{'=' * 70}
Crash Type: {crash_type}
Time: {datetime.now().isoformat()}
Python Version: {sys.version}
Platform: {sys.platform}

Signal Info: {sig_info if sig_info else 'N/A'}

Stack Trace:
{stack_trace}

This crash report has been saved to:
{crash_log}

Please report this crash with the log file to:
https://github.com/ProgrammingDinosaur/autoortho4xplane/issues
{'=' * 70}
"""

    # Write to crash log file
    try:
        with open(crash_log, 'w') as f:
            f.write(crash_msg)
        print(f"\nCrash log written to: {crash_log}", file=sys.stderr)
    except Exception as e:
        print(f"Failed to write crash log: {e}", file=sys.stderr)

    # Write to stderr
    print(crash_msg, file=sys.stderr)

    # Also log via logging system (may not work if logger is corrupted)
    try:
        log.critical(crash_msg)
        for handler in log.handlers:
            handler.flush()
    except Exception:
        pass


def _signal_handler(signum, frame):
    """Handle Unix signals (SIGSEGV, SIGABRT, etc)."""
    sig_names = {
        signal.SIGSEGV: "SIGSEGV (Segmentation Fault)",
        signal.SIGABRT: "SIGABRT (Abort)",
        signal.SIGFPE: "SIGFPE (Floating Point Exception)",
        signal.SIGILL: "SIGILL (Illegal Instruction)",
    }

    if hasattr(signal, 'SIGBUS'):
        sig_names[signal.SIGBUS] = "SIGBUS (Bus Error)"

    sig_name = sig_names.get(signum, f"Signal {signum}")
    _write_crash_info(sig_name, sig_info=signum, frame_info=frame)

    # Re-raise the signal with default handler to generate core dump
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _install_unix_handlers():
    """Install signal handlers for Unix-like systems."""
    signals_to_catch = [
        signal.SIGSEGV,  # Segmentation fault
        signal.SIGABRT,  # Abort
        signal.SIGFPE,   # Floating point exception
        signal.SIGILL,   # Illegal instruction
    ]

    # SIGBUS only exists on Unix
    if hasattr(signal, 'SIGBUS'):
        signals_to_catch.append(signal.SIGBUS)

    for sig in signals_to_catch:
        try:
            signal.signal(sig, _signal_handler)
            log.info(f"Installed crash handler for {sig}")
        except (OSError, RuntimeError) as e:
            log.warning(f"Could not install handler for {sig}: {e}")


def _install_windows_handlers():
    """
    Install Windows-specific handlers.

    Note: On Windows, faulthandler is the primary mechanism.
    This function sets up additional Windows-specific features.
    """
    try:
        import ctypes

        # Set the process to NOT show the Windows Error Reporting dialog
        # This allows the crash to be logged without waiting for user input
        kernel32 = ctypes.windll.kernel32
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002

        # Allow the crash to happen without showing modal dialogs
        # faulthandler will capture it before the dialog would appear
        kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)

        log.info("Windows error mode configured for crash capture")

    except Exception as e:
        log.warning(f"Could not configure Windows error handling: {e}")
        # This is OK - faulthandler is the primary mechanism anyway


def _install_exception_hook():
    """Install global exception hook for uncaught Python exceptions."""
    original_excepthook = sys.excepthook

    def custom_excepthook(exc_type, exc_value, exc_traceback):
        """Log uncaught exceptions before exiting."""
        if issubclass(exc_type, KeyboardInterrupt):
            # Don't log keyboard interrupts (user pressed Ctrl+C)
            original_excepthook(exc_type, exc_value, exc_traceback)
            return

        crash_msg = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        log.critical(f"Uncaught exception:\n{crash_msg}")

        # Also write to crash log
        _write_crash_info("Uncaught Python Exception", sig_info=str(exc_value))

        # Call original handler
        original_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = custom_excepthook
    log.info("Installed Python exception hook")


def _install_faulthandler():
    """
    Install Python's faulthandler - THIS IS THE KEY FOR NUITKA BUILDS!

    faulthandler is a built-in Python module that:
    - Catches SIGSEGV, SIGFPE, SIGABRT, SIGBUS, SIGILL
    - Works with C extensions and Nuitka-compiled code
    - Dumps the Python traceback to a file before dying
    - Works on ALL platforms including Windows

    Unlike signal.signal(), faulthandler works even when the crash
    happens in C code (like Nuitka-generated code or C libraries).
    """
    global _fault_log_file

    try:
        # Create the crash log directory
        crash_dir = LOGS_DIR
        os.makedirs(crash_dir, exist_ok=True)

        # Create a persistent crash log file
        # This file MUST stay open for faulthandler to write to it
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        crash_log_path = os.path.join(
            crash_dir, f"crash_{timestamp}_pid{pid}.log"
        )

        # Open file in unbuffered mode for immediate writes
        _fault_log_file = open(crash_log_path, 'w', buffering=1)

        # Write header immediately
        _fault_log_file.write("=" * 70 + "\n")
        _fault_log_file.write("AUTOORTHO CRASH LOG\n")
        _fault_log_file.write("=" * 70 + "\n")
        _fault_log_file.write(f"Process ID: {pid}\n")
        _fault_log_file.write(f"Started: {datetime.now().isoformat()}\n")
        _fault_log_file.write(f"Python Version: {sys.version}\n")
        _fault_log_file.write(f"Platform: {sys.platform}\n")
        _fault_log_file.write(f"Executable: {sys.executable}\n")
        _fault_log_file.write("=" * 70 + "\n\n")
        _fault_log_file.write(
            "If this file contains a stack trace below, AutoOrtho crashed.\n\n"
        )
        _fault_log_file.flush()

        # Enable faulthandler to write to FILE (not stderr)
        # stderr may not exist in Nuitka GUI builds
        faulthandler.enable(file=_fault_log_file, all_threads=True)

        # Also set up breadcrumb file for tracking last operation
        global _breadcrumb_file, _breadcrumb_enabled
        try:
            breadcrumb_path = os.path.join(crash_dir, "last_operation.txt")
            _breadcrumb_file = open(breadcrumb_path, 'w', buffering=1)
            _breadcrumb_file.write("AutoOrtho started\n")
            _breadcrumb_file.flush()
            _breadcrumb_enabled = True
        except Exception:
            pass

        log.info(f"Crash handler active: {crash_log_path}")
        return True

    except Exception as e:
        log.warning(f"Faulthandler file setup failed: {e}")
        # Fallback: stderr only (might not work in GUI builds)
        try:
            faulthandler.enable(file=sys.stderr, all_threads=True)
        except Exception:
            pass
        return False


def install_crash_handler():
    """
    Install crash handlers for the platform.

    This should be called early in the application startup, preferably
    in __main__.py before importing any C extensions.

    CRITICAL: This uses Python's faulthandler module, which works with
    Nuitka-compiled code (unlike signal.signal which does NOT work).

    Returns:
        bool: True if handler was installed successfully
    """
    global _crash_handler_installed

    if _crash_handler_installed:
        return True

    # Install faulthandler (works with Nuitka-compiled code)
    _install_faulthandler()

    # Install Python exception hook
    _install_exception_hook()

    # Install platform-specific handlers as backup
    try:
        if sys.platform.startswith('linux') or sys.platform == 'darwin':
            _install_unix_handlers()
        elif sys.platform == 'win32':
            _install_windows_handlers()
    except Exception as e:
        log.debug(f"Platform handler setup: {e}")

    _crash_handler_installed = True

    # Log to file only (not spamming console)
    crash_log = get_crash_log_path()
    log.info(f"Crash handler enabled, logs: {crash_log}")

    return True


def dump_traceback_now(reason="manual"):
    """
    Dump the current traceback of all threads to the crash log and stderr.

    This is useful for debugging hangs or understanding state.

    Args:
        reason: String describing why the dump was requested
    """
    global _fault_log_file

    try:
        timestamp = datetime.now().isoformat()
        header = (
            f"\n{'=' * 70}\n"
            f"TRACEBACK DUMP - {reason}\n"
            f"Time: {timestamp}\n"
            f"{'=' * 70}\n"
        )

        # Dump to stderr
        print(header, file=sys.stderr)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

        # Dump to log file if available
        if _fault_log_file:
            _fault_log_file.write(header)
            faulthandler.dump_traceback(file=_fault_log_file, all_threads=True)
            _fault_log_file.flush()

        log.info(f"Traceback dump completed: {reason}")

    except Exception as e:
        log.error(f"Failed to dump traceback: {e}")


def enable_core_dumps():
    """Enable core dumps on Unix systems (for debugging)."""
    if sys.platform.startswith('linux') or sys.platform == 'darwin':
        try:
            import resource
            resource.setrlimit(
                resource.RLIMIT_CORE,
                (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
            )
            log.info("Core dumps enabled")
        except Exception as e:
            log.warning(f"Could not enable core dumps: {e}")


def get_crash_log_path():
    """Return the current crash log path, if faulthandler is enabled."""
    global _fault_log_file
    if _fault_log_file:
        return _fault_log_file.name
    return None


if __name__ == "__main__":
    # Test the crash handler
    logging.basicConfig(level=logging.DEBUG)

    print("=" * 60)
    print("AUTOORTHO CRASH HANDLER TEST")
    print("=" * 60)
    print(f"\nPlatform: {sys.platform}")
    print(f"Python: {sys.version}")
    print(f"Executable: {sys.executable}")
    print(f"\nLog directory: {LOGS_DIR}")
    print()

    print("Installing crash handler...")
    install_crash_handler()

    crash_log = get_crash_log_path()
    print(f"\nCrash log file: {crash_log}")

    print("\n" + "=" * 60)
    print("Choose a test:")
    print("1. Segmentation fault (SIGSEGV) - WILL CRASH")
    print("2. Python exception")
    print("3. Dump current traceback (no crash)")
    print("4. Exit cleanly")
    print("=" * 60)

    choice = input("Enter choice (1-4): ").strip()

    if choice == "1":
        print("\n[!] Triggering segfault via ctypes...")
        print(f"[!] Check {crash_log} for the crash log")
        print("[!] Crashing in 2 seconds...")
        import time
        time.sleep(2)
        import ctypes
        ctypes.string_at(0)  # This will segfault
    elif choice == "2":
        print("\n[!] Raising Python exception...")
        raise RuntimeError("Test exception from crash handler test")
    elif choice == "3":
        print("\n[*] Dumping traceback...")
        dump_traceback_now("test_dump")
        print(f"\n[*] Traceback written to: {crash_log}")
        print("[*] Exiting cleanly.")
    else:
        print("\n[*] Exiting cleanly.")

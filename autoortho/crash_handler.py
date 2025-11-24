#!/usr/bin/env python3
"""
Crash handler to capture and log C-level crashes (segfaults, access violations).

This module sets up signal handlers (Unix) and SEH handlers (Windows) to:
1. Log crash information before the process dies
2. Generate crash dumps for debugging
3. Provide meaningful error messages to users

Usage:
    from crash_handler import install_crash_handler
    install_crash_handler()
"""

import os
import sys
import signal
import logging
import traceback
from datetime import datetime
from utils.constants import LOGS_DIR


log = logging.getLogger(__name__)

# Track if crash handler is installed
_crash_handler_installed = False


def _get_crash_log_path():
    """Get path to crash log file."""
    crash_dir = LOGS_DIR
    os.makedirs(crash_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(crash_dir, f"crash_{timestamp}.log")


def _write_crash_info(crash_type, sig_info=None, frame_info=None):
    """Write crash information to log file and stderr."""
    crash_log = _get_crash_log_path()
    
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
{''.join(traceback.format_stack(frame_info) if frame_info else traceback.format_stack())}

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
        # Flush all handlers
        for handler in log.handlers:
            handler.flush()
    except:
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
    """Install Windows SEH (Structured Exception Handling)."""
    try:
        import ctypes
        import ctypes.wintypes
        
        # Windows exception codes
        EXCEPTION_ACCESS_VIOLATION = 0xC0000005
        EXCEPTION_ARRAY_BOUNDS_EXCEEDED = 0xC000008C
        EXCEPTION_DATATYPE_MISALIGNMENT = 0x80000002
        EXCEPTION_FLT_DIVIDE_BY_ZERO = 0xC000008E
        EXCEPTION_INT_DIVIDE_BY_ZERO = 0xC0000094
        EXCEPTION_STACK_OVERFLOW = 0xC00000FD
        
        exception_names = {
            EXCEPTION_ACCESS_VIOLATION: "Access Violation",
            EXCEPTION_ARRAY_BOUNDS_EXCEEDED: "Array Bounds Exceeded",
            EXCEPTION_DATATYPE_MISALIGNMENT: "Datatype Misalignment",
            EXCEPTION_FLT_DIVIDE_BY_ZERO: "Float Divide by Zero",
            EXCEPTION_INT_DIVIDE_BY_ZERO: "Integer Divide by Zero",
            EXCEPTION_STACK_OVERFLOW: "Stack Overflow",
        }
        
        def windows_exception_handler(exception_record, establisher_frame, 
                                      context_record, dispatcher_context):
            """Handle Windows SEH exceptions."""
            exc_code = exception_record.contents.ExceptionCode
            exc_addr = exception_record.contents.ExceptionAddress
            exc_name = exception_names.get(exc_code, f"Exception 0x{exc_code:08X}")
            
            crash_info = f"{exc_name} at address 0x{exc_addr:016X}"
            _write_crash_info("Windows Exception", sig_info=crash_info)
            
            # EXCEPTION_CONTINUE_SEARCH = 1 (let default handler run)
            return 1
        
        # This is complex and may not work reliably with Python
        # Better to use Windows Error Reporting (WER) or minidump generation
        log.info("Windows SEH handler installation attempted (limited functionality)")
        
    except ImportError:
        log.warning("Could not install Windows exception handler (ctypes not available)")
    except Exception as e:
        log.warning(f"Failed to install Windows exception handler: {e}")


def _install_exception_hook():
    """Install global exception hook for uncaught Python exceptions."""
    original_excepthook = sys.excepthook
    
    def custom_excepthook(exc_type, exc_value, exc_traceback):
        """Log uncaught exceptions before exiting."""
        if issubclass(exc_type, KeyboardInterrupt):
            # Don't log keyboard interrupts (user pressed Ctrl+C)
            original_excepthook(exc_type, exc_value, exc_traceback)
            return
        
        crash_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        log.critical(f"Uncaught exception:\n{crash_msg}")
        
        # Also write to crash log
        _write_crash_info("Uncaught Python Exception", sig_info=str(exc_value))
        
        # Call original handler
        original_excepthook(exc_type, exc_value, exc_traceback)
    
    sys.excepthook = custom_excepthook
    log.info("Installed Python exception hook")


def install_crash_handler():
    """
    Install crash handlers for the platform.
    
    This should be called early in the application startup, preferably
    in __main__.py before importing any C extensions.
    
    Returns:
        bool: True if handler was installed successfully
    """
    global _crash_handler_installed
    
    if _crash_handler_installed:
        log.debug("Crash handler already installed")
        return True
    
    log.info("Installing crash handlers...")
    
    # Always install Python exception hook
    _install_exception_hook()
    
    # Install platform-specific handlers
    if sys.platform.startswith('linux') or sys.platform == 'darwin':
        _install_unix_handlers()
    elif sys.platform == 'win32':
        _install_windows_handlers()
    else:
        log.warning(f"Unknown platform {sys.platform}, limited crash handling")
    
    _crash_handler_installed = True
    log.info("Crash handler installation complete")
    
    return True


def enable_core_dumps():
    """
    Enable core dumps on Unix systems (for debugging).
    
    This is optional and should only be used during development/debugging.
    """
    if sys.platform.startswith('linux') or sys.platform == 'darwin':
        try:
            import resource
            # Set core dump size to unlimited
            resource.setrlimit(resource.RLIMIT_CORE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
            log.info("Core dumps enabled")
        except Exception as e:
            log.warning(f"Could not enable core dumps: {e}")


# Optional: Minidump generation for Windows
def setup_windows_minidump():
    """
    Set up Windows minidump generation using dbghelp.dll.
    
    This creates .dmp files that can be analyzed with WinDbg or Visual Studio.
    """
    if sys.platform != 'win32':
        return
    
    try:
        import ctypes
        from ctypes import wintypes
        
        # This is a simplified version - full implementation would be more complex
        log.info("Windows minidump setup attempted (requires dbghelp.dll)")
        
        # In practice, you'd want to use a library like 'minidump' or 'crashpad'
        # for production-quality crash dump generation
        
    except Exception as e:
        log.warning(f"Could not setup Windows minidump: {e}")


if __name__ == "__main__":
    # Test the crash handler
    logging.basicConfig(level=logging.INFO)
    
    print("Testing crash handler...")
    install_crash_handler()
    
    print("\nCrash handler installed. Testing...")
    print("Choose a test:")
    print("1. Segmentation fault (SIGSEGV)")
    print("2. Python exception")
    print("3. Exit cleanly")
    
    choice = input("Enter choice (1-3): ").strip()
    
    if choice == "1":
        print("\nTriggering segfault via ctypes...")
        import ctypes
        ctypes.string_at(0)  # This will segfault
    elif choice == "2":
        print("\nRaising Python exception...")
        raise RuntimeError("Test exception from crash handler test")
    else:
        print("\nExiting cleanly.")


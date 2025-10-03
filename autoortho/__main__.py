import os
import sys

if os.environ.get("AO_RUN_MODE") == "macfuse_worker":
    # Absolute import is robust under Nuitka for the entry module
    from macfuse_worker import main as _ao_worker_main
    _ao_worker_main()
    os._exit(0)


import logging
import logging.handlers
import atexit, signal, threading
import platform
from aoconfig import CFG
from pathlib import Path
from utils.constants import system_type

# ---------------------------------------------------------------------------
# Global cleanup hook – ensures we release worker threads, caches & C buffers
# ---------------------------------------------------------------------------

def _global_shutdown(signum=None, frame=None):
    """Run once on interpreter exit or termination signals to free memory."""
    if getattr(_global_shutdown, "_done", False):
        return
    _global_shutdown._done = True

    log = logging.getLogger(__name__)
    try:
        log.info("Shutdown requested. Draining background threads...")
        alive = threading.enumerate()
        # Report alive threads for diagnostics
        for t in alive:
            if t is threading.current_thread():
                continue
            log.debug(
                "Thread alive: name=%s ident=%s daemon=%s",
                t.name,
                t.ident,
                t.daemon,
            )
    except Exception:
        pass

    try:
        from autoortho.getortho import shutdown as _go_shutdown
        _go_shutdown()
    except Exception:
        pass

    # Join remaining non-daemon threads (best effort)
    try:
        for t in threading.enumerate():
            if t is threading.current_thread() or t.daemon:
                continue
            try:
                t.join(timeout=2)
            except Exception:
                pass
    except Exception:
        pass

    # macOS sometimes leaves background workers around; if only daemon
    # threads remain, or stubborn non-daemon threads won't join, force exit.
    try:
        remaining = [
            t for t in threading.enumerate()
            if t is not threading.current_thread()
        ]
        non_daemons = [t for t in remaining if not t.daemon]
        if system_type == "darwin" and non_daemons:
            log.warning(
                "Force exiting on macOS; non-daemon threads still alive: %s",
                [t.name for t in non_daemons],
            )
            os._exit(0)
    except Exception:
        pass


# Register the hooks as early as possible
atexit.register(_global_shutdown)
for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _global_shutdown)
    except Exception:
        # Signals may not be available on some platforms (e.g., Windows < py3.8)
        pass


def setuplogs():
    log_dir = os.path.join(os.path.expanduser("~"), ".autoortho-data", "logs")
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)

    log_level=logging.DEBUG if os.environ.get('AO_DEBUG') or CFG.general.debug else logging.INFO
    logging.basicConfig(
            #filename=os.path.join(log_dir, "autoortho.log"),
            level=log_level,
            handlers=[
                #logging.FileHandler(filename=os.path.join(log_dir, "autoortho.log")),
                logging.handlers.RotatingFileHandler(
                    filename=os.path.join(log_dir, "autoortho.log"),
                    maxBytes=10485760,
                    backupCount=5
                ),
                logging.StreamHandler() if sys.stdout is not None else logging.NullHandler()
            ]
    )
    log = logging.getLogger(__name__)
    log.info(f"Setup logs: {log_dir}, log level: {log_level}")


# If SSL_CERT_DIR is not set, default to /etc/ssl/certs when available for Linux users.
try:
    if system_type == 'linux' and "SSL_CERT_DIR" not in os.environ:
            os.environ["SSL_CERT_DIR"] = "/etc/ssl/certs"
    if system_type == "darwin" and ".app" in sys.argv[0]:
        macos_dir = Path(sys.argv[0]).resolve().parents[0]  # .../Contents/MacOS
        pem = macos_dir / "certifi" / "cacert.pem"
        if pem.exists():
            os.environ.setdefault("SSL_CERT_FILE", str(pem))
except Exception:
    pass

import autoortho

if __name__ == "__main__":
    try:
        setuplogs()
        autoortho.main()
    except Exception as _fatal_err:
        import traceback
        logging.getLogger(__name__).exception("Fatal error during startup: %s", _fatal_err)
        try:
            if os.name == "nt":
                import ctypes
                log_path = os.path.join(os.path.expanduser("~"), ".autoortho-data", "logs", "autoortho.log")
                msg = (
                    "AutoOrtho failed to start.\n\n"
                    + str(_fatal_err)
                    + "\n\nSee log for details:\n"
                    + log_path
                )
                ctypes.windll.user32.MessageBoxW(None, msg, "AutoOrtho Error", 0x00000010)
        except Exception:
            pass

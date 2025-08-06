import os
import sys
import logging
import logging.handlers
import atexit, signal, threading
from aoconfig import CFG

# ---------------------------------------------------------------------------
# Global cleanup hook – ensures we release worker threads, caches & C buffers
# ---------------------------------------------------------------------------

def _global_shutdown(signum=None, frame=None):
    """Run once on interpreter exit or termination signals to free memory."""
    if getattr(_global_shutdown, "_done", False):
        return
    _global_shutdown._done = True

    try:
        from autoortho.getortho import shutdown as _go_shutdown
        _go_shutdown()
    except Exception:
        pass

    # Join remaining non-daemon threads (best effort)
    for t in threading.enumerate():
        if t is threading.current_thread() or t.daemon:
            continue
        try:
            t.join(timeout=2)
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

import autoortho

if __name__ == "__main__":
    setuplogs()
    autoortho.main()

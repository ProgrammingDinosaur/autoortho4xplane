import argparse
import gc
import logging
import logging.handlers
import os
import signal
import sys

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.mfusepy import FUSE
except ImportError:
    from mfusepy import FUSE

try:
    from autoortho.autoortho_fuse import AutoOrtho, fuse_option_profiles_by_os
except ImportError:
    from autoortho_fuse import AutoOrtho, fuse_option_profiles_by_os

try:
    from autoortho.aostats import update_process_memory_stat, clear_process_memory_stat
except ImportError:
    from aostats import update_process_memory_stat, clear_process_memory_stat

log = logging.getLogger(__name__)

# Install crash handler for macOS worker subprocess
# CRITICAL: macOS FUSE workers must NOT install signal handlers!
# macFUSE uses internal signal handling (SIGBUS, etc.) and our custom handlers
# interfere with this, causing the FUSE event loop to exit silently.
# We only install the Python exception hook, skipping signal handlers.
try:
    try:
        from autoortho.crash_handler import install_crash_handler
    except ImportError:
        from crash_handler import install_crash_handler
    # skip_signal_handlers=True prevents interference with macFUSE's signal handling
    install_crash_handler(skip_signal_handlers=True)
    log.debug("Crash handler installed in macOS worker subprocess (signal handlers skipped)")
except Exception as e:
    # Don't fail if crash handler can't be installed
    print(f"Warning: macOS worker could not install crash handler: {e}", file=sys.stderr)


def configure_worker_logging(mount_name, loglevel: str):
    addr = os.getenv("AO_LOG_ADDR")

    # A filter that annotates every record with the mount id
    class AddMount(logging.Filter):
        def filter(self, record):
            record.mount = mount_name
            return True

    root = logging.getLogger()
    # First try socket logging to the parent
    if addr:
        host, port = addr.split(":")
        try:
            # set format to include mount name
            logging.basicConfig(
                format='[WORKER %(process)d][%(mount)s]: %(message)s',
                stream=sys.stdout
            )
            sh = logging.handlers.SocketHandler(host, int(port))
            # Replace any existing handlers with the socket handler
            root.handlers[:] = []
            root.addHandler(sh)
            root.setLevel(logging.INFO)
            root.addFilter(AddMount())
            root.info("Worker logging routed to parent via SocketHandler")
            return
        except Exception as e:
            # fall back to local console if socket setup fails
            log.error(f"Worker logging routed to parent via SocketHandler failed: {e}")
    else:
        # in addition to console
        from pathlib import Path
        log_dir = Path.home() / ".autoortho-data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / f"worker-{mount_name}.log",
                                                maxBytes=10_485_760, backupCount=3)
        root.addHandler(fh)
        root.setLevel(loglevel)
        root.addFilter(AddMount())
        root.info("Worker logging routed to local file")

    # Fallback: local console logging
    logging.basicConfig(
        level=loglevel,
        format='[WORKER %(process)d][%(mount)s]: %(message)s',
        stream=sys.stdout
    )
    root.addFilter(AddMount())


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--mountpoint", required=True)
    ap.add_argument("--nothreads", action="store_true")
    ap.add_argument("--volname")
    ap.add_argument("--loglevel", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="DEBUG")
    args, unknown = ap.parse_known_args()

    configure_worker_logging(args.volname, args.loglevel)

    log.info(f"MOUNT: {args.mountpoint}")
    additional_args = fuse_option_profiles_by_os(args.nothreads, args.volname)

    log.info("Starting FUSE mount")
    log.debug(
            "Loading FUSE with options: %s",
            ", ".join(sorted(map(str, additional_args.keys())))
    )

    def _handle_sigusr1(signum, frame):
        """Reload maptype override from config on SIGUSR1."""
        try:
            try:
                from autoortho.aoconfig import CFG
            except ImportError:
                from aoconfig import CFG
            CFG.load()
            new_maptype = CFG.autoortho.maptype_override
            try:
                from autoortho.getortho import TileCacher
            except ImportError:
                from getortho import TileCacher
            for obj in gc.get_objects():
                try:
                    if isinstance(obj, TileCacher):
                        obj.maptype_override = new_maptype
                        if new_maptype == "Custom Map":
                            try:
                                from autoortho.utils.custom_map import reload_custom_map_config
                            except ImportError:
                                from utils.custom_map import reload_custom_map_config
                            obj.custom_map = reload_custom_map_config()
                        elif new_maptype == "APPLE":
                            try:
                                from autoortho.utils.apple_token_service import apple_token_service
                            except ImportError:
                                from utils.apple_token_service import apple_token_service
                            apple_token_service.reset_apple_maps_token()
                        else:
                            obj.custom_map = None
                        log.info(f"Worker reloaded maptype to {new_maptype}")
                except Exception:
                    pass
        except Exception as e:
            log.error(f"SIGUSR1 handler failed: {e}")

    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    try:
        # Initial heartbeat before mounting
        try:
            update_process_memory_stat()
        except Exception:
            pass
        FUSE(AutoOrtho(args.root, use_ns=True), os.path.abspath(args.mountpoint), **additional_args)
        log.info(f"FUSE: Exiting mount {args.mountpoint}")
    except Exception as e:
        log.error(f"FUSE mount failed with non-negotiable error: {e}")
        raise
    finally:
        try:
            try:
                from autoortho.getortho import stats_batcher, shutdown
            except ImportError:
                from getortho import stats_batcher, shutdown
            if stats_batcher:
                stats_batcher.stop()
            shutdown()
        except Exception as e:
            log.error(f"Error stopping stats batcher: {e}")
            pass
        try:
            clear_process_memory_stat()
        except Exception:
            pass


if __name__ == "__main__":
    main()

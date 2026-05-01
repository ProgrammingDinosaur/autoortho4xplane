import argparse
import ctypes
import gc
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

try:
    from autoortho.mount_setup import setupmount
except ImportError:
    from mount_setup import setupmount

try:
    from autoortho.utils.constants import system_type
except ImportError:
    from utils.constants import system_type

try:
    from autoortho.aostats import update_process_memory_stat, clear_process_memory_stat
except ImportError:
    from aostats import update_process_memory_stat, clear_process_memory_stat

log = logging.getLogger(__name__)

RELOAD_GENERATION_STAT = "worker_reload_config_generation"


def configure_worker_logging(mount_name, loglevel: str):
    addr = os.getenv("AO_LOG_ADDR")

    class AddMount(logging.Filter):
        def filter(self, record):
            record.mount = mount_name
            return True

    root = logging.getLogger()
    level = getattr(logging, str(loglevel).upper(), logging.INFO)

    if addr:
        host, port = addr.split(":")
        try:
            logging.basicConfig(
                format="[WORKER %(process)d][%(mount)s]: %(message)s",
                stream=sys.stdout,
            )
            sh = logging.handlers.SocketHandler(host, int(port))
            sh.addFilter(AddMount())
            root.handlers[:] = []
            root.addHandler(sh)
            root.setLevel(level)
            root.addFilter(AddMount())
            root.info("Worker logging routed to parent via SocketHandler")
            return
        except Exception as exc:
            log.error("Worker SocketHandler setup failed: %s", exc)

    from pathlib import Path

    log_dir = Path.home() / ".autoortho-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / f"worker-{mount_name}.log",
        maxBytes=10_485_760,
        backupCount=3,
    )
    fh.setFormatter(logging.Formatter("[WORKER %(process)d][%(mount)s]: %(message)s"))
    fh.addFilter(AddMount())
    root.handlers[:] = []
    root.addHandler(fh)
    root.setLevel(level)
    root.addFilter(AddMount())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("[WORKER %(process)d][%(mount)s]: %(message)s"))
    console.addFilter(AddMount())
    root.addHandler(console)
    root.info("Worker logging routed to local file")


def _install_crash_handler():
    try:
        try:
            from autoortho.crash_handler import install_crash_handler
        except ImportError:
            from crash_handler import install_crash_handler
        install_crash_handler(skip_signal_handlers=True)
        log.debug("Crash handler installed in mount worker with signal handlers skipped")
    except Exception as exc:
        print(f"Warning: mount worker could not install crash handler: {exc}", file=sys.stderr)


def _begin_worker_shutdown(reason):
    try:
        try:
            from autoortho import getortho
        except ImportError:
            import getortho
        getortho.begin_shutdown(reason)
    except Exception as exc:
        log.debug("Worker shutdown signal cleanup failed: %s", exc)


def _handle_shutdown_signal(signum, frame):
    log.info("Worker received signal %s; beginning shutdown", signum)
    _begin_worker_shutdown(f"worker signal {signum}")
    raise SystemExit(128 + int(signum))


def _install_signal_handlers():
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_shutdown_signal)
        except Exception:
            pass

    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is not None:
        try:
            signal.signal(sigusr1, _handle_sigusr1)
        except Exception:
            pass


def _reload_maptype_from_config():
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
                    log.info("Worker reloaded maptype to %s", new_maptype)
            except Exception:
                pass
    except Exception as exc:
        log.error("Maptype reload failed: %s", exc)


def _handle_sigusr1(signum, frame):
    """Reload maptype override from config on SIGUSR1."""
    _reload_maptype_from_config()


def _start_reload_poll_thread():
    try:
        try:
            from autoortho.aostats import get_stat
        except ImportError:
            from aostats import get_stat
    except Exception:
        return

    try:
        last_seen = get_stat(RELOAD_GENERATION_STAT)
    except Exception:
        last_seen = 0

    def _poll():
        nonlocal last_seen
        while True:
            time.sleep(1.0)
            try:
                generation = get_stat(RELOAD_GENERATION_STAT)
                if generation and generation != last_seen:
                    last_seen = generation
                    _reload_maptype_from_config()
            except Exception:
                pass

    t = threading.Thread(target=_poll, name="AO-WorkerReloadPoll", daemon=True)
    t.start()


def _runtime_for_platform():
    if system_type == "windows":
        try:
            from autoortho import winsetup
        except ImportError:
            import winsetup

        systemtype, libpath = winsetup.find_win_libs()
        if not systemtype or not libpath:
            raise RuntimeError("No usable Windows FUSE backend was found")

        try:
            from autoortho import mfusepy
        except ImportError:
            import mfusepy
        mfusepy._libfuse = ctypes.CDLL(libpath)
    elif system_type == "darwin":
        systemtype = "macOS"
    else:
        systemtype = "Linux-FUSE"

    try:
        from autoortho.mfusepy import FUSE
    except ImportError:
        from mfusepy import FUSE

    try:
        from autoortho.autoortho_fuse import AutoOrtho, fuse_option_profiles_by_os
    except ImportError:
        from autoortho_fuse import AutoOrtho, fuse_option_profiles_by_os

    return FUSE, AutoOrtho, fuse_option_profiles_by_os, systemtype


def _clear_root_poison(root):
    try:
        poison_root = os.path.join(os.path.expanduser(root), ".poison")
        if os.path.exists(poison_root):
            os.remove(poison_root)
    except Exception as exc:
        log.debug("Ignoring failure to remove root poison file: %s", exc)


def _shutdown_getortho():
    try:
        try:
            from autoortho.getortho import stats_batcher, shutdown
        except ImportError:
            from getortho import stats_batcher, shutdown
        if stats_batcher:
            stats_batcher.stop()
        shutdown()
    except Exception as exc:
        log.error("Error stopping getortho worker subsystems: %s", exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--nothreads", action="store_true")
    parser.add_argument("--volname")
    parser.add_argument(
        "--loglevel",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="DEBUG",
    )
    args, unknown = parser.parse_known_args()

    mount_name = args.volname or os.path.basename(os.path.abspath(args.mountpoint))
    configure_worker_logging(mount_name, args.loglevel)
    _install_crash_handler()
    _install_signal_handlers()
    _start_reload_poll_thread()

    log.info("MOUNT: %s", args.mountpoint)
    _clear_root_poison(args.root)
    FUSE, AutoOrtho, fuse_option_profiles_by_os, mount_systemtype = _runtime_for_platform()
    additional_args = fuse_option_profiles_by_os(args.nothreads, mount_name)

    log.info("Starting FUSE mount")
    log.debug(
        "Loading FUSE with options: %s",
        ", ".join(sorted(map(str, additional_args.keys()))),
    )

    try:
        try:
            update_process_memory_stat()
        except Exception:
            pass

        with setupmount(args.mountpoint, mount_systemtype) as mountpoint:
            FUSE(
                AutoOrtho(args.root, use_ns=True),
                os.path.abspath(mountpoint),
                **additional_args,
            )
        log.info("FUSE: Exiting mount %s", args.mountpoint)
    except SystemExit:
        raise
    except Exception as exc:
        log.error("FUSE mount failed with non-negotiable error: %s", exc)
        raise
    finally:
        _begin_worker_shutdown("worker exit")
        _shutdown_getortho()
        try:
            clear_process_memory_stat()
        except Exception:
            pass


if __name__ == "__main__":
    main()

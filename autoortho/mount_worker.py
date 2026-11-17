import argparse
import atexit
import gc
import json
import logging
import logging.handlers
import os
import queue
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
_worker_stopping = threading.Event()
_log_listener = None


def _start_control_thread(mountpoint):
    if os.getenv("AO_WORKER_CONTROL_STDIN") != "1":
        return

    def control():
        try:
            descriptor = os.getenv("AO_WORKER_CONTROL_FD")
            if descriptor is not None:
                stream = os.fdopen(int(descriptor), "rb", buffering=0)
            else:
                # Windows workers use the redirected stdin pipe. GUI builds
                # may set sys.stdin=None even though descriptor 0 is valid.
                stream = os.fdopen(os.dup(0), "rb", buffering=0)
            command = stream.readline()
            if command in (b"shutdown\n", "shutdown\n", b"", ""):
                _worker_stopping.set()
                _begin_worker_shutdown("parent requested shutdown")
                if not command and system_type == "darwin":
                    # Parent disappeared: detach through the OS, with the FUSE
                    # loop still alive. Never exit FUSE from a file callback.
                    try:
                        from autoortho.macsetup import unmount_macfuse
                    except ImportError:
                        from macsetup import unmount_macfuse
                    unmount_macfuse(mountpoint)
        except (OSError, ValueError) as exc:
            log.debug("Worker control channel closed: %s", exc)
        finally:
            if "stream" in locals():
                stream.close()

    thread = threading.Thread(target=control, name="AO-WorkerControl", daemon=True)
    thread.start()
    return thread


def configure_worker_logging(mount_name, loglevel: str):
    global _log_listener
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
            # FUSE threads hand records to a queue; one thread pickles and
            # sends them. Sending from the caller made every read thread wait
            # on the socket handler's lock, and with DEBUG on (the default)
            # that lock was where full-resolution texture reads spent most
            # of their time.
            records = queue.SimpleQueue()
            qh = logging.handlers.QueueHandler(records)
            qh.addFilter(AddMount())
            listener = logging.handlers.QueueListener(records, sh)
            listener.start()
            atexit.register(listener.stop)
            _log_listener = listener
            root.handlers[:] = []
            root.addHandler(qh)
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
        log.debug("Python crash hook installed; Python fatal-signal callbacks skipped")
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
        while not _worker_stopping.wait(1.0):
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

        # mfusepy configures ctypes signatures and binds fuse_main_real while it
        # imports. Replacing mfusepy._libfuse afterwards leaves those bindings
        # pointing at different CDLL instances and makes fuse_get_context return
        # a plain integer. Select the backend before importing mfusepy instead.
        os.environ["FUSE_LIBRARY_PATH"] = os.path.abspath(libpath)
        try:
            from autoortho import mfusepy
        except ImportError:
            import mfusepy

        loaded_path = os.path.normcase(
            os.path.abspath(str(getattr(mfusepy, "_libfuse_path", "")))
        )
        requested_path = os.path.normcase(os.path.abspath(libpath))
        if loaded_path != requested_path:
            raise RuntimeError(
                "mfusepy was imported before the selected Windows FUSE backend "
                f"was configured (loaded={loaded_path!r}, requested={requested_path!r})"
            )
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


def _poison_mount(mountpoint):
    """Ask one of this process's own FUSE loops to exit (see _getattr_poison)."""
    try:
        os.lstat(os.path.join(mountpoint, ".poison"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.debug("Poison trigger for %s failed: %s", mountpoint, exc)


def _serve_shared(mounts, nothreads, runtime):
    """Serve every mount from this process, each FUSE loop on its own thread.

    All mounts share one TileCacher: one memory limit, one prefetcher, one
    DDS builder. If one mount fails while the others run, the others are
    unmounted too and the worker exits with an error, as a per-mount worker
    failure did before (the parent then fails all mounts).
    """
    FUSE, AutoOrtho, fuse_option_profiles_by_os, mount_systemtype = runtime
    try:
        from autoortho import getortho
    except ImportError:
        import getortho

    tile_cacher = getortho.TileCacher(".cache")
    failures = []
    mounted = {}
    lock = threading.Lock()
    ended_early = threading.Event()

    def serve(root, mountpoint, volname):
        _clear_root_poison(root)
        try:
            with setupmount(mountpoint, mount_systemtype) as mp:
                path = os.path.abspath(mp)
                ao = AutoOrtho(root, use_ns=True, tile_cacher=tile_cacher)
                with lock:
                    if ended_early.is_set():
                        return  # another mount already failed
                    mounted[mountpoint] = path
                log.info("Starting FUSE mount %s", mountpoint)
                FUSE(ao, path, **fuse_option_profiles_by_os(nothreads, volname))
            log.info("FUSE: Exiting mount %s", mountpoint)
        except BaseException as exc:  # one mount failing fails the worker
            log.error("FUSE mount %s failed: %s", mountpoint, exc)
            with lock:
                failures.append((mountpoint, exc))
        finally:
            with lock:
                mounted.pop(mountpoint, None)
                if not _worker_stopping.is_set():
                    # A mount ended on its own (unmounted or failed): the
                    # others go too, so the parent sees the worker exit.
                    ended_early.set()

    threads = [
        threading.Thread(
            target=serve, args=spec, name=f"AO-FUSE-{spec[2] or index}",
            daemon=False,
        )
        for index, spec in enumerate(mounts)
    ]
    for thread in threads:
        thread.start()

    def poison_remaining():
        with lock:
            remaining = list(mounted.values())
        for path in remaining:
            _poison_mount(path)

    try:
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(0.5)
            if ended_early.is_set():
                # Repeated: a mount may still be starting when the first
                # poison is sent, and a poison before FUSE serves the path
                # does nothing.
                poison_remaining()
    except BaseException:
        # A signal (SystemExit) reaches the main thread only: unmount the
        # FUSE threads too, or they keep the process alive.
        ended_early.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and any(t.is_alive() for t in threads):
            poison_remaining()
            for thread in threads:
                thread.join(0.2)
        raise
    if failures:
        mountpoint, exc = failures[0]
        raise RuntimeError(f"FUSE mount {mountpoint} failed: {exc}") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--nothreads", action="store_true")
    parser.add_argument("--volname")
    parser.add_argument(
        "--mounts-json",
        help="Every (root, mountpoint, volname) to serve from this process.",
    )
    parser.add_argument(
        "--loglevel",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="DEBUG",
    )
    args, unknown = parser.parse_known_args()

    mounts = None
    if args.mounts_json:
        mounts = [tuple(str(part) for part in spec) for spec in json.loads(args.mounts_json)]
    shared = bool(mounts) and len(mounts) > 1
    if shared and system_type == "darwin":
        # macFUSE cannot run several mounts from one process through the
        # Python FUSE binding; the parent never asks for this on macOS.
        parser.error("--mounts-json with several mounts is not supported on macOS")

    mount_name = "shared" if shared else (
        args.volname or os.path.basename(os.path.abspath(args.mountpoint))
    )
    configure_worker_logging(mount_name, args.loglevel)
    try:
        try:
            from autoortho.diagnostics import start_worker_profiler_from_env
        except ImportError:
            from diagnostics import start_worker_profiler_from_env
        start_worker_profiler_from_env(mount_name)
    except Exception as exc:
        log.error("Could not start worker performance diagnostics: %s", exc)
    _install_crash_handler()
    _install_signal_handlers()
    _start_reload_poll_thread()

    if shared:
        log.info("MOUNT (shared worker): %s", ", ".join(m[1] for m in mounts))
    else:
        log.info("MOUNT: %s", args.mountpoint)
        _clear_root_poison(args.root)
    runtime = _runtime_for_platform()
    FUSE, AutoOrtho, fuse_option_profiles_by_os, mount_systemtype = runtime
    if sys.platform != "win32":
        if __package__:
            from .posix_crash import capture_worker_images
        else:
            from posix_crash import capture_worker_images
        capture_worker_images()
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

        if shared:
            _start_control_thread(os.path.abspath(os.path.expanduser(mounts[0][1])))
            _serve_shared(mounts, args.nothreads, runtime)
        else:
            with setupmount(args.mountpoint, mount_systemtype) as mountpoint:
                ao = AutoOrtho(args.root, use_ns=True)
                _start_control_thread(os.path.abspath(mountpoint))
                FUSE(
                    ao,
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
        _worker_stopping.set()
        # Persist flight timings before subsystem cleanup. The parent may signal
        # this process as soon as the mount disappears.
        try:
            try:
                from autoortho.diagnostics import stop_active_profiler
            except ImportError:
                from diagnostics import stop_active_profiler
            stop_active_profiler()
        except Exception as exc:
            log.error("Could not finalize worker performance diagnostics: %s", exc)
        _begin_worker_shutdown("worker exit")
        _shutdown_getortho()
        try:
            clear_process_memory_stat()
        except Exception:
            pass


if __name__ == "__main__":
    main()

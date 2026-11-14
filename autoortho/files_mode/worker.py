"""Files worker process (``AO_RUN_MODE=files_worker``).

One process for all regions, started and stopped by the parent like the
FUSE mount workers. It never imports a FUSE module. It reports through the
shared stats store:

* ``files_worker_state``: ``{"state": "starting|ready|error|stopped", ...}``
* ``files_stats``: the session's counters, every 2 s.
"""

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

log = logging.getLogger(__name__)

STATE_STAT = "files_worker_state"
STATS_STAT = "files_stats"
_stopping = threading.Event()


def _imports():
    try:
        from autoortho import aostats, getortho, texture_service
        from autoortho.aoconfig import CFG
    except ImportError:
        import aostats
        import getortho
        import texture_service
        from aoconfig import CFG
    return aostats, getortho, texture_service, CFG


def configure_logging(loglevel):
    root = logging.getLogger()
    level = getattr(logging, str(loglevel).upper(), logging.INFO)
    addr = os.getenv("AO_LOG_ADDR")
    fmt = logging.Formatter("[FILES %(process)d]: %(message)s")
    root.handlers[:] = []
    if addr:
        host, port = addr.split(":")
        try:
            handler = logging.handlers.SocketHandler(host, int(port))
            root.addHandler(handler)
            root.setLevel(level)
            return
        except Exception as exc:
            print(f"Files worker log socket failed: {exc}", file=sys.stderr)
    from pathlib import Path
    log_dir = Path.home() / ".autoortho-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "worker-files.log",
                                              maxBytes=10_485_760, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    root.setLevel(level)


def _control_thread(on_shutdown):
    if os.getenv("AO_WORKER_CONTROL_STDIN") != "1":
        return None

    def run():
        try:
            descriptor = os.getenv("AO_WORKER_CONTROL_FD")
            if descriptor is not None:
                stream = os.fdopen(int(descriptor), "rb", buffering=0)
            else:
                stream = os.fdopen(os.dup(0), "rb", buffering=0)
            command = stream.readline()
            if command in (b"shutdown\n", b""):
                on_shutdown("parent requested shutdown" if command else "parent gone")
        except (OSError, ValueError) as exc:
            log.debug("Files worker control channel closed: %s", exc)

    t = threading.Thread(target=run, name="AO-FilesControl", daemon=True)
    t.start()
    return t


def parse_regions(items):
    regions = {}
    for item in items or ():
        name, _, root = item.partition("=")
        if name and root:
            regions[name] = root
    return regions


def build_config(args, CFG):
    try:
        from autoortho.files_mode.session import FilesConfig
    except ImportError:
        from files_mode.session import FilesConfig
    files = getattr(CFG, "files", None)

    def opt(name, default):
        value = getattr(files, name, default) if files is not None else default
        return default if value in (None, "") else value

    missing = getattr(CFG.autoortho, "missing_color", [66, 77, 55])
    try:
        rgb = tuple(int(c) for c in missing[:3])
    except Exception:
        rgb = (66, 77, 55)
    return FilesConfig(
        files_root=args.files_root,
        custom_scenery=args.custom_scenery,
        regions=parse_regions(args.region),
        xplane_path=args.xplane_path or getattr(CFG.paths, "xplane_path", ""),
        disk_gb=max(10.0, float(opt("disk_gb", 20))),
        camera_reach_km=float(opt("camera_reach_km", 10)),
        strategy=str(opt("materialize", "link")).lower(),
        gate_port=int(opt("gate_port", 49317)),
        dxt_format=str(getattr(CFG.pydds, "format", "BC1")).upper(),
        missing_rgb=rgb,
        imagery_floor_zl=int(opt("imagery_floor_zl", 13)),
        install_plugin=str(opt("install_plugin", True)).lower() in ("true", "1", "yes", "on"),
        exclude_from_search=str(opt("exclude_from_search", False)).lower()
        in ("true", "1", "yes", "on"),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="autoortho files worker")
    parser.add_argument("--files-root", required=True)
    parser.add_argument("--custom-scenery", required=True)
    parser.add_argument("--region", action="append", default=[],
                        help="name=root of each region (repeat)")
    parser.add_argument("--xplane-path", default="")
    parser.add_argument("--loglevel", default="INFO")
    args, _unknown = parser.parse_known_args(argv)
    configure_logging(args.loglevel)
    try:
        try:
            from autoortho.diagnostics import start_worker_profiler_from_env
        except ImportError:
            from diagnostics import start_worker_profiler_from_env
        start_worker_profiler_from_env("files")
    except Exception as exc:
        log.debug("Worker performance diagnostics unavailable: %s", exc)
    aostats, getortho, texture_service, CFG = _imports()
    try:
        from autoortho.files_mode.session import FilesSession
    except ImportError:
        from files_mode.session import FilesSession

    def set_state(state, **extra):
        try:
            aostats.set_stat(STATE_STAT, {"state": state, "pid": os.getpid(),
                                          "ts": time.time(), **extra})
        except Exception:
            pass

    set_state("starting")
    session = None

    def shutdown(reason):
        if _stopping.is_set():
            return
        _stopping.set()
        log.info("Files worker stopping: %s", reason)

    def on_signal(signum, _frame):
        shutdown(f"signal {signum}")

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except Exception:
                pass
    _control_thread(shutdown)

    failed = False
    try:
        getortho.clear_shutdown_request()
        cfg = build_config(args, CFG)
        tc = getortho.TileCacher(CFG.paths.cache_dir)
        if cfg.imagery_floor_zl and tc.min_zoom < cfg.imagery_floor_zl:
            # Bing's ZL12 and coarser come from other imagery whose colours
            # clash; files mode builds every low level ahead of time, so it
            # takes them from ZL13 (plan 5.6.5).
            tc.min_zoom = int(cfg.imagery_floor_zl)
        service = texture_service.TextureService(tc, cfg=CFG)
        try:
            from autoortho.files_mode.route import RouteFeed
            from autoortho.files_mode.dsf_swap import DsfSwapper
        except ImportError:
            from files_mode.route import RouteFeed
            from files_mode.dsf_swap import DsfSwapper
        route = RouteFeed(aostats.get_stat, cfg=CFG)

        def dsf_swapper(pack):
            try:
                from autoortho.time_exclusion import time_exclusion_manager
            except ImportError:
                from time_exclusion import time_exclusion_manager
            # Sun pitch reaches getortho's tracker from the parent's snapshot.
            time_exclusion_manager.set_dataref_tracker(getortho.datareftracker)
            enabled = lambda: str(getattr(CFG.time_exclusion, "enabled", False)).lower() in (
                "true", "1", "yes", "on")
            return DsfSwapper(pack, cfg.xplane_path, time_exclusion_manager.is_exclusion_active,
                              enabled=enabled)

        session = FilesSession(
            cfg, lambda: aostats.get_stat("flight_state"), service=service,
            route_fn=route.route, season_key_fn=texture_service.season_key_for,
            dsf_swapper_factory=dsf_swapper,
        )
        port = session.start()
        # Governor levers published by the parent (live_settings).
        for name in (session.LEVER_BUILDERS, session.LEVER_LOOKAHEAD, session.LEVER_MARGIN):
            getortho.live_settings.on_change(
                name, lambda value, name=name: session.apply_levers({name: value}))
        getortho.live_settings.start()
        set_state("ready", port=port, regions=sorted(cfg.regions))
        log.info("Files mode ready: %d regions, gate on port %d", len(cfg.regions), port)
        while not _stopping.wait(2.0):
            try:
                getortho._sync_shared_flight_state()  # sun pitch for time exclusion
            except Exception:
                pass
            try:
                aostats.set_stat(STATS_STAT, session.stats())
                aostats.update_process_memory_stat()
            except Exception:
                pass
    except Exception as exc:
        log.exception("Files mode failed to start")
        failed = True
        set_state("error", error=str(exc))
        _stopping.set()
    finally:
        if session is not None:
            try:
                session.stop()
            except Exception:
                log.exception("Files mode stop failed")
        try:
            try:
                from autoortho.diagnostics import stop_active_profiler
            except ImportError:
                from diagnostics import stop_active_profiler
            stop_active_profiler()
        except Exception as exc:
            log.debug("Worker performance diagnostics not finalized: %s", exc)
        try:
            getortho.begin_shutdown("files worker exit")
            if getortho.stats_batcher:
                getortho.stats_batcher.stop()
            getortho.shutdown()
        except Exception as exc:
            log.debug("getortho shutdown: %s", exc)
        if not failed:
            set_state("stopped")
        try:
            aostats.clear_process_memory_stat()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

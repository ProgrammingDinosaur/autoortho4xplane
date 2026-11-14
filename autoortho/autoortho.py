#!/usr/bin/env python

import os
import subprocess
import sys
import time
import shutil
import signal
import tempfile
import platform
import argparse
import concurrent.futures
import threading
import socketserver
import logging.handlers
import pickle
import struct


from multiprocessing.managers import BaseManager

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho import aoconfig
except ImportError:
    import aoconfig

try:
    from autoortho import aostats
except ImportError:
    import aostats

try:
    from autoortho import winsetup
except ImportError:
    import winsetup

try:
    from autoortho import macsetup
except ImportError:
    import macsetup

try:
    from autoortho.utils.mount_utils import (
        cleanup_mountpoint,
        cleanup_stale_mount_folders,
        safe_ismount,
    )
except ImportError:
    from utils.mount_utils import (
        cleanup_mountpoint,
        cleanup_stale_mount_folders,
        safe_ismount,
    )

try:
    from autoortho.utils.constants import MAPTYPES, system_type
except ImportError:
    from utils.constants import MAPTYPES, system_type

try:
    from autoortho.process_supervisor import (
        AOProcessSupervisor,
        DEFAULT_WORKER_STOP_TIMEOUT,
    )
except ImportError:
    from process_supervisor import AOProcessSupervisor, DEFAULT_WORKER_STOP_TIMEOUT

try:
    from autoortho.diagnostics import (
        start_parent_profiler,
        stop_active_profiler,
    )
except ImportError:
    from diagnostics import start_parent_profiler, stop_active_profiler

try:
    from autoortho.http2_broker import (
        HTTP2Broker,
        BrokerError as HTTP2BrokerError,
        BrokerStartupError as HTTP2BrokerStartupError,
    )
except ImportError:
    try:
        from http2_broker import (
            HTTP2Broker,
            BrokerError as HTTP2BrokerError,
            BrokerStartupError as HTTP2BrokerStartupError,
        )
    except ImportError:
        HTTP2Broker = None
        HTTP2BrokerError = Exception
        HTTP2BrokerStartupError = Exception

try:
    from autoortho.version import __version__
except ImportError:
    from version import __version__

import logging
log = logging.getLogger(__name__)

import geocoder

# Import PyQt6 modules

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

try:
    from autoortho.ui import main_window as config_ui
except ImportError:
    from ui import main_window as config_ui

try:
    from autoortho.ui.theme import apply_theme
except ImportError:
    from ui.theme import apply_theme

USE_QT = True


class MountError(Exception):
    pass


class AutoOrthoError(Exception):
    pass


class StatsManager(BaseManager): 
    pass


class LogRecordStreamHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            # Read 4-byte length prefix
            chunk = self._recvall(4)
            if not chunk:
                break
            slen = struct.unpack('>L', chunk)[0]
            # Read the pickled LogRecord
            chunk = self._recvall(slen)
            if not chunk:
                break
            record_dict = pickle.loads(chunk)
            record = logging.makeLogRecord(record_dict)
            # Forward to parent's logging
            logging.getLogger(record.name).handle(record)

    def _recvall(self, n):
        data = b''
        while len(data) < n:
            packet = self.connection.recv(n - len(data))
            if not packet:
                return None
            data += packet
        return data


class LogServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


_SERVER_STATS_STORE = None

def _get_or_create_stats_store():
    """Factory used by the StatsManager server process to expose a singleton store."""
    global _SERVER_STATS_STORE
    if _SERVER_STATS_STORE is None:
        _SERVER_STATS_STORE = aostats.StatsStore()
    return _SERVER_STATS_STORE


# Share one mount setup implementation with subprocess workers.
try:
    from autoortho.mount_setup import setupmount
except ImportError:
    from mount_setup import setupmount


def diagnose(CFG, mount_timeout=60.0, retry_interval=0.25):

    location = geocoder.ip("me")
    mount_failed = False
    provider_failures = []

    def _check_mount(mount):
        try:
            if system_type == 'darwin':
                return macsetup.is_macfuse_mount(mount)
            else:
                return os.path.isdir(os.path.join(mount, 'textures'))
        except Exception:
            return False

    mounts = [s.get('mount') for s in CFG.scenery_mounts]
    mount_results = [False] * len(mounts)
    probe_done = [threading.Event() for _ in mounts]
    deadline = time.monotonic() + max(0.0, float(mount_timeout))

    def _probe_mount(index, mount):
        try:
            while time.monotonic() < deadline:
                if _check_mount(mount):
                    mount_results[index] = True
                    return
                probe_done[index].wait(max(0.01, retry_interval))
        finally:
            probe_done[index].set()

    for index, mount in enumerate(mounts):
        threading.Thread(
            target=_probe_mount,
            args=(index, mount),
            name=f"diag_mount_{index}",
            daemon=True,
        ).start()

    log.info("Waiting for mounts...")
    log.info(
        "Checking %d mount(s) in parallel (up to %.1fs)...",
        len(mounts),
        mount_timeout,
    )
    for done in probe_done:
        remaining = max(0.0, deadline - time.monotonic())
        done.wait(remaining)

    log.info("\n\n")
    log.info("------------------------------------")
    log.info(" Diagnostic check ...")
    log.info("------------------------------------")
    log.info(f"Detected system: {platform.uname()}")
    log.info(f"Detected location {location.address}")
    log.info(f"Detected installed scenery:")
    for index, scenery in enumerate(CFG.scenery_mounts):
        root = scenery.get('root')
        log.info(f"    {root}")
        ret = mount_results[index]
        log.info(f"        Mounted? {ret}")
        if not ret:
            mount_failed = True

    log.info(f"Checking maptypes:")
    import getortho
    for maptype in MAPTYPES:
        if maptype in ("Use tile default", "Custom Map"):
            continue
        # Use ignore_cleanup_errors=True to handle race with async cache writes
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            c = getortho.Chunk(2176, 3232, maptype, 13, cache_dir=tmpdir)
            ret = c.get()
            # Give async cache writer a moment to complete or detect deleted dir
            time.sleep(0.1)
            if ret:
                log.info(f"    Maptype: {maptype} OK!")
            else:
                log.warning(f"    Maptype: {maptype} FAILED!")
                provider_failures.append(maptype)

    log.info("------------------------------------")
    if mount_failed:
        log.warning("***************")
        log.warning("***************")
        log.warning("MOUNT FAILURES DETECTED!!")
        log.warning("Please review the filesystem and FUSE setup.")
        log.warning("***************")
        log.warning("***************")
        return False
    if provider_failures:
        log.warning(
            "Optional imagery provider check(s) failed: %s. "
            "Mounted scenery remains available; provider availability "
            "does not determine mount health.",
            ", ".join(provider_failures),
        )
    log.info(" Diagnostics done.  All checks passed")
    return True


class AOMount:
    mounts_running = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.mount_threads = []
        self.mount_workers = []
        self.mac_os_procs = []
        self._active_mountpoints = []
        self.mount_worker_supervisor = AOProcessSupervisor()
        self._performance_profiler = None
        self._performance_profiler_env = {}
        self._download_broker = None
        self._download_broker_env = {}

        # Start shared stats manager and reporter/log servers
        self.start_stats_manager()

        self._reporter_stop = threading.Event()
        self._reporter_thread = None
        self.start_reporter()

        self.start_log_server()

    def start_log_server(self):
        """Start once in the parent. Returns (host, port) bound on 127.0.0.1."""
        server = LogServer(('127.0.0.1', 0), LogRecordStreamHandler)
        host, port = server.server_address
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.log_server = server
        self.log_thread = t
        self.log_host = host
        self.log_port = port
        self.log_addr = f"{self.log_host}:{self.log_port}"
        return

    def stop_log_server(self, join_timeout: float = 2.0):
        server = getattr(self, "log_server", None)
        t = getattr(self, "log_thread", None)
        try:
            if server:
                server.shutdown()
        except Exception:
            pass
        if t and t.is_alive():
            try:
                t.join(timeout=join_timeout)
            except Exception:
                pass
        self.log_server = None
        self.log_thread = None
        self.log_host = None
        self.log_port = None
        self.log_addr = None
        return

    def start_stats_manager(self, authkey: bytes = b'AO4XPSTATS'):
        # Register the store exposure BEFORE starting the manager so the server
        # process exports a singleton StatsStore with the required API.
        StatsManager.register(
            'get_store',
            callable=_get_or_create_stats_store,
            exposed=['inc', 'inc_many', 'set', 'get', 'delete', 'keys', 'snapshot']
        )

        mgr = StatsManager(address=('127.0.0.1', 0), authkey=authkey)
        mgr.start()

        # Obtain a proxy to the server-owned store and bind helpers to it so
        # that all stats updates/readouts go through the shared store.
        store_proxy = mgr.get_store()
        aostats.bind_local_store(store_proxy)
        self._shared_store = store_proxy
        host, port = mgr.address
        log.info(f"StatsManager listening on {host}:{port}")
        self.stats_manager = mgr
        self.stats_host = host
        self.stats_port = port
        self.stats_auth = authkey
        self.stats_addr = f"{self.stats_host}:{self.stats_port}"
        return

    def stop_stats_manager(self, join_timeout: float = 2.0):
        aostats.bind_local_store(None)
        mgr = getattr(self, "stats_manager", None)
        if mgr:
            try:
                mgr.shutdown()
            except Exception as e:
                log.error(f"Error stopping stats manager: {e}")
        self.stats_manager = None
        self._shared_store = None
        self.stats_host = None
        self.stats_port = None
        self.stats_auth = None
        self.stats_addr = None
        return

    def launch_mount_worker(
            self, root: str,
            mountpoint: str,
            volname: str,
            nothreads: bool,
            stats_addr=None,
            stats_auth=None,
            log_addr=None,
    ):
        self._start_performance_diagnostics()
        log.info(f"AutoOrtho:  root: {root}  mountpoint: {mountpoint}")
        loglevel = getattr(self.cfg.general, 'file_log_level', 'INFO').upper()
        worker_env = dict(self._performance_profiler_env)
        worker_env.update(self._download_broker_env)
        worker_env["AO_MOUNT_WORKER_COUNT"] = str(getattr(
            self, "_worker_cpu_share_count",
            max(1, len(getattr(self.cfg, "scenery_mounts", []))),
        ))
        handle = self.mount_worker_supervisor.start_mount_worker(
            root,
            mountpoint,
            volname,
            nothreads,
            stats_addr=stats_addr,
            stats_auth=stats_auth,
            log_addr=log_addr,
            loglevel=loglevel,
            extra_env=worker_env,
        )
        self.mount_workers.append(handle)
        if self._performance_profiler is not None:
            self._performance_profiler.register_process(
                handle.pid,
                f"mount-worker:{volname}",
            )
        # Backward-compatible process list used by maptype/custom-map reload code.
        self.mac_os_procs.append(handle.process)
        return handle

    def launch_macfuse_worker(
            self, root: str,
            mountpoint: str,
            volname: str,
            nothreads: bool,
            stats_addr=None,
            stats_auth=None,
            log_addr=None,
    ) -> subprocess.Popen:
        handle = self.launch_mount_worker(
            root,
            mountpoint,
            volname,
            nothreads,
            stats_addr=stats_addr,
            stats_auth=stats_auth,
            log_addr=log_addr,
        )
        return handle.process

    def stop_mount_workers(self, timeout: float = DEFAULT_WORKER_STOP_TIMEOUT):
        log.info("Stopping mount workers...")
        stopped = self.mount_worker_supervisor.stop_all(timeout=timeout)
        self.mount_workers = list(self.mount_worker_supervisor.handles)
        self.mac_os_procs = [h.process for h in self.mount_workers]
        self.mount_threads = []
        return stopped

    def stop_macfuse_workers(self, timeout: float = DEFAULT_WORKER_STOP_TIMEOUT):
        self.stop_mount_workers(timeout=timeout)
        return

    def _start_performance_diagnostics(self):
        if getattr(self, "_performance_profiler", None) is not None:
            return
        try:
            profiler = start_parent_profiler(self.cfg)
        except Exception as exc:
            log.error("Could not start performance diagnostics: %s", exc)
            profiler = None
        self._performance_profiler = profiler
        self._performance_profiler_env = (
            profiler.child_environment() if profiler is not None else {}
        )
        if profiler is not None:
            manager_process = getattr(
                getattr(self, "stats_manager", None),
                "_process",
                None,
            )
            manager_pid = getattr(manager_process, "pid", None)
            if manager_pid:
                profiler.register_process(manager_pid, "stats-manager")

    def _finish_performance_diagnostics(self):
        if getattr(self, "_performance_profiler", None) is None:
            return
        stats_snapshot = {}
        try:
            store = getattr(self, "_shared_store", None)
            if store is not None:
                stats_snapshot = store.snapshot()
        except Exception as exc:
            log.debug("Could not snapshot final diagnostics counters: %s", exc)
        try:
            report_path = stop_active_profiler(
                stats_snapshot=stats_snapshot,
                finalize_session=True,
            )
            if report_path is not None:
                log.info("Flight performance report written to %s", report_path)
        except Exception as exc:
            log.error("Could not finalize performance diagnostics: %s", exc)
        finally:
            self._performance_profiler = None
            self._performance_profiler_env = {}

    def reporter(self):
        while True:
            time.sleep(10)
            snap = self._shared_store.snapshot()
            log.info(f"STATS: {snap}")

    def start_reporter(
        self,
        interval_sec: float = 10.0,
        aggregation_interval_sec: float = 1.0,
    ):
        """Aggregate process memory frequently while logging less often."""
        if self._reporter_thread and self._reporter_thread.is_alive():
            return
        self._reporter_stop.clear()

        def _reporter_loop():
            last_log = time.monotonic()
            while not self._reporter_stop.wait(aggregation_interval_sec):
                try:
                    # Update this process's own memory heartbeat so it's counted
                    try:
                        aostats.update_process_memory_stat()
                    except Exception:
                        pass

                    # Aggregate RSS across all live processes reporting into the shared store
                    total_rss = 0
                    proc_count = 0
                    now_ts = int(time.time())
                    try:
                        keys = self._shared_store.keys()
                        for k in keys:
                            if isinstance(k, str) and (
                                k.startswith('proc_mem_effective_bytes:')
                                or k.startswith('proc_mem_rss_bytes:')
                            ):
                                pid = k.split(':', 1)[1]
                                if (
                                    k.startswith('proc_mem_rss_bytes:')
                                    and self._shared_store.get(
                                        f'proc_mem_effective_bytes:{pid}', 0
                                    )
                                ):
                                    continue
                                # Liveness check: heartbeat within last 45 seconds
                                alive_ts = self._shared_store.get(f'proc_alive_ts:{pid}', 0)
                                if isinstance(alive_ts, (int, float)):
                                    alive_ok = (now_ts - int(alive_ts)) <= 45
                                else:
                                    alive_ok = False
                                if not alive_ok:
                                    # Clean stale entries
                                    try:
                                        self._shared_store.delete(k)
                                        self._shared_store.delete(f'proc_alive_ts:{pid}')
                                    except Exception:
                                        pass
                                    continue
                                try:
                                    val = int(self._shared_store.get(k, 0) or 0)
                                except Exception:
                                    val = 0
                                if val > 0:
                                    total_rss += val
                                    proc_count += 1
                        try:
                            # Publish aggregated memory stats
                            self._shared_store.set('cur_mem_mb', total_rss // 1048576)
                            # Add timestamp for staleness detection by workers
                            self._shared_store.set('cur_mem_mb_ts', int(time.time()))
                            # Publish proc_count for debugging memory discrepancies
                            self._shared_store.set('mem_proc_count', proc_count)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    # Mount workers do not run their own X-Plane UDP listener.
                    # Publish one bounded snapshot so they can enable predictive
                    # work only after the parent confirms a valid flight.
                    try:
                        try:
                            from autoortho.datareftrack import dt
                        except ImportError:
                            from datareftrack import dt
                        with dt._lock:
                            flight_state = {
                                "connected": bool(dt.connected),
                                "data_valid": bool(dt.data_valid),
                                "has_ever_connected": bool(
                                    dt.has_ever_connected
                                ),
                                "lat": float(dt.lat),
                                "lon": float(dt.lon),
                                "alt": float(dt.alt),
                                "hdg": float(dt.hdg),
                                "spd": float(dt.spd),
                                "local_time_sec": float(dt.local_time_sec),
                                "pressure_alt": float(dt.pressure_alt),
                                "sun_pitch": float(dt.sun_pitch),
                                "timestamp": time.time(),
                            }
                        self._shared_store.set(
                            "flight_state", flight_state
                        )
                    except Exception:
                        pass

                    # Aggregate mm and partial-mm counters into averages and counts
                    try:
                        keys = self._shared_store.keys()
                        mm_counts = {}
                        for k in keys:
                            if not isinstance(k, str):
                                continue
                            if k.startswith('mm_count:'):
                                try:
                                    mm = int(k.split(':', 1)[1])
                                except Exception:
                                    continue
                                cnt = int(self._shared_store.get(k, 0) or 0)
                                if cnt > 0:
                                    mm_counts[mm] = cnt
                        # Publish aggregated counts only when non-empty; otherwise remove
                        try:
                            if mm_counts:
                                self._shared_store.set('mm_counts', mm_counts)
                            else:
                                try:
                                    self._shared_store.delete('mm_counts')
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    except Exception:
                        pass

                    if time.monotonic() - last_log < interval_sec:
                        continue
                    last_log = time.monotonic()
                    snap = self._shared_store.snapshot()
                    # Hide internal per-process and batching keys from logs
                    # Keep proc_mem_mb for debugging memory issues
                    try:
                        def _is_internal(k):
                            return (
                                (isinstance(k, str) and (
                                    k.startswith('proc_mem_effective_bytes') or
                                    k.startswith('proc_mem_rss_bytes') or
                                    k.startswith('proc_alive_ts') or
                                    k.startswith('proc_threads') or
                                    k.startswith('last_tile_access_ts') or
                                    k.startswith('mm_count:') or
                                    k.startswith('mm_time_total_ms:') or
                                    k.startswith('partial_mm_count:') or
                                    k.startswith('partial_mm_time_total_ms:')
                                )) or k in (
                                    'proc_count',
                                    'cur_mem_mb_ts',
                                    'flight_state',
                                )
                            )
                        filtered = {k: v for k, v in snap.items() if not _is_internal(k)}
                    except Exception:
                        filtered = snap

                    # Ensure nested dicts are logged with numerically sorted keys
                    try:
                        for _name in ('mm_counts',):
                            _val = filtered.get(_name)
                            if isinstance(_val, dict) and _val:
                                try:
                                    sorted_items = sorted(_val.items(), key=lambda kv: int(kv[0]))
                                except Exception:
                                    sorted_items = sorted(_val.items(), key=lambda kv: kv[0])
                                filtered[_name] = {k: v for k, v in sorted_items}
                    except Exception:
                        pass
                    log.info("STATS: %s", filtered)
                except Exception as e:
                    log.error("reporter() exception: %s", e, exc_info=True)

        t = threading.Thread(target=_reporter_loop, name="AO-Reporter", daemon=True)
        t.start()
        self._reporter_thread = t
        log.info("Stats reporter thread started (will log every %.1f seconds)", interval_sec)

    def stop_reporter(self, join_timeout: float = 3.0):
        """Stop the periodic global-stats logger and join the thread."""
        log.info("Stopping reporter...")
        if not self._reporter_thread:
            log.info("Reporter thread not running.")
            return
        self._reporter_stop.set()
        if self._reporter_thread.is_alive():
            self._reporter_thread.join(timeout=join_timeout)
        self._reporter_thread = None

    def _resource_limits(self):
        """The [limits] in effect (None when they cannot be worked out)."""
        try:
            try:
                from autoortho import hardware, limits
            except ImportError:
                import hardware
                import limits
            return limits.from_config(self.cfg, hardware.basic())
        except Exception as exc:
            log.debug("Resource limits unavailable: %s", exc)
            return None

    def _apply_cpu_limit(self):
        """Native build threads for this process and every worker it starts."""
        active = self._resource_limits()
        threads = active.cpu_threads if active is not None else None
        if threads:
            os.environ["AO_CPU_THREAD_LIMIT"] = str(threads)
            log.info(
                "CPU limit: %d cores (%d threads) for building textures",
                active.cpu_cores,
                threads,
            )
        else:
            os.environ.pop("AO_CPU_THREAD_LIMIT", None)

    def _ensure_parent_services(self):
        self._apply_cpu_limit()
        if not getattr(self, "stats_manager", None):
            self.start_stats_manager()
        if not getattr(self, "log_server", None):
            self.start_log_server()
        self.start_download_broker()
        self.start_reporter()

    def start_download_broker(self):
        if self._download_broker is not None:
            return
        self._start_performance_diagnostics()
        enabled = getattr(self.cfg.autoortho, "http2_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower().strip() in ("true", "1", "yes", "on")
        if not enabled:
            return
        if HTTP2Broker is None:
            log.warning(
                "HTTP/2 broker dependencies are unavailable; using direct "
                "HTTP/1.1 downloads"
            )
            return
        try:
            max_in_flight = aoconfig.resolve_provider_setting(
                "provider_max_in_flight", self.cfg
            )
            max_connections = min(
                max_in_flight,
                aoconfig.resolve_provider_setting(
                    "provider_max_connections", self.cfg
                ),
            )
            max_concurrency = max_in_flight
            origin_max = aoconfig.resolve_provider_setting(
                "provider_origin_max_concurrency", self.cfg
            )
            origin_initial = aoconfig.resolve_provider_setting(
                "provider_origin_initial_concurrency", self.cfg
            )
            if origin_initial <= 0:
                origin_initial = min(max_in_flight, max_connections)
            broker_kwargs = {
                "max_concurrency": max_concurrency,
                "max_connections": max_connections,
                "max_pending": max_in_flight,
                "max_response_bytes": 8 * 1024 * 1024,
                "profile_environment": dict(
                    getattr(self, "_performance_profiler_env", {})
                ),
                "adaptive_concurrency": aoconfig.resolve_provider_setting(
                    "provider_adaptive_concurrency", self.cfg
                ),
                "adaptive_controller_v2": aoconfig.resolve_provider_setting(
                    "provider_adaptive_controller_v2", self.cfg
                ),
                "transport_trace": aoconfig.resolve_provider_setting(
                    "provider_transport_trace", self.cfg
                ),
                "origin_initial_concurrency": aoconfig.resolve_provider_setting(
                    "provider_origin_initial_concurrency", self.cfg
                ),
                "origin_min_concurrency": aoconfig.resolve_provider_setting(
                    "provider_origin_min_concurrency", self.cfg
                ),
                # 0 means "no separate ceiling"; the broker then clamps to
                # max_concurrency on its own.
                "origin_max_concurrency": origin_max or None,
                "origin_increase_step": aoconfig.resolve_provider_setting(
                    "provider_origin_increase_step", self.cfg
                ),
                "origin_success_threshold": aoconfig.resolve_provider_setting(
                    "provider_origin_success_threshold", self.cfg
                ),
                "origin_decrease_factor": aoconfig.resolve_provider_setting(
                    "provider_origin_decrease_factor", self.cfg
                ),
                "origin_cooldown_seconds": aoconfig.resolve_provider_setting(
                    "provider_origin_cooldown_seconds", self.cfg
                ),
                "queue_timeout": aoconfig.resolve_provider_setting(
                    "provider_queue_timeout", self.cfg
                ),
            }
            broker_kwargs["origin_initial_concurrency"] = origin_initial
            active_limits = self._resource_limits()
            if active_limits is not None and active_limits.bytes_per_second:
                broker_kwargs["max_bytes_per_second"] = (
                    active_limits.bytes_per_second
                )
                log.info(
                    "Download limit: %.0f Mbit/s",
                    active_limits.bandwidth_mbps,
                )
            broker = HTTP2Broker(**broker_kwargs)
            try:
                broker.start()
                broker_mode = "process"
            except HTTP2BrokerStartupError as process_exc:
                log.warning(
                    "HTTP/2 broker process startup failed; retrying with "
                    "an in-process selector-loop server: %s",
                    process_exc,
                )
                broker = HTTP2Broker(
                    in_process=True,
                    **broker_kwargs,
                )
                broker.start()
                broker_mode = "thread"
            self._download_broker = broker
            self._download_broker_env = broker.client_environment()
            broker_pid = getattr(broker, "pid", None)
            if broker_pid and self._performance_profiler is not None:
                self._performance_profiler.register_process(
                    broker_pid,
                    "http2-broker",
                )
            os.environ.update(self._download_broker_env)
            log.info(
                "Shared HTTP/2 broker started "
                "(mode=%s, concurrency=%d, connections=%d)",
                broker_mode,
                max_concurrency,
                max_connections,
            )
        except HTTP2BrokerError as exc:
            log.warning(
                "Could not start shared HTTP/2 broker; using direct "
                "HTTP/1.1 downloads: %s",
                exc,
            )

    def _live_settings(self):
        """Parent-side handle for publishing live levers to the workers."""
        live = getattr(self, "_live_settings_handle", None)
        if live is None:
            try:
                from autoortho.live_settings import LiveSettings
            except ImportError:
                from live_settings import LiveSettings
            live = LiveSettings(self.cfg)
            self._live_settings_handle = live
        return live

    def publish_levers(self, levers):
        """Send lever values to every running mount worker and the broker.

        ``levers`` maps "section.key" to a value. Download levers go to the
        broker directly (it lives in this process tree's broker process);
        everything is also published for the workers' LiveSettings.
        """
        if not levers:
            return {}
        self._live_settings().publish(dict(levers))
        broker = getattr(self, "_download_broker", None)
        if broker is not None:
            kwargs = {}
            if "autoortho.provider_origin_max_concurrency" in levers:
                value = int(levers["autoortho.provider_origin_max_concurrency"] or 0)
                if value <= 0:
                    value = int(aoconfig.resolve_provider_setting(
                        "provider_max_in_flight", self.cfg
                    ))
                kwargs["origin_max_concurrency"] = value
            if "limits.bytes_per_second" in levers:
                kwargs["max_bytes_per_second"] = float(
                    levers["limits.bytes_per_second"] or 0
                )
            if kwargs:
                try:
                    broker.configure(**kwargs)
                except Exception as exc:
                    log.debug("Broker reconfigure failed: %s", exc)
        log.info(
            "Live levers: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(levers.items())),
        )
        return dict(levers)

    # -- governor (settings refactor phases 5-6) ---------------------------

    def _governor_modules(self):
        try:
            from autoortho import governor, hardware, connection_test
            from autoortho.aostats import get_stat, set_stat
            from autoortho.ram_brake import RamBrake
        except ImportError:
            import governor
            import hardware
            import connection_test
            from aostats import get_stat, set_stat
            from ram_brake import RamBrake
        return governor, hardware, connection_test, get_stat, set_stat, RamBrake

    def _vram_budget_bytes(self, hardware_module, active):
        """The graphics memory budget the photo textures should stay under."""
        try:
            try:
                from autoortho import guards
            except ImportError:
                import guards
            hw = hardware_module.cached() or hardware_module.detect()
            budget = guards.vram_budget(hw, guards.reserve_gb(self.cfg), active)
            return int(budget.bad) if budget is not None and budget.bad > 0 else None
        except Exception as exc:
            log.debug("Graphics memory budget unavailable: %s", exc)
            return None

    @staticmethod
    def _collected_stat(prefix):
        """A per-process list (``prefix:<pid>``) joined over the workers."""
        try:
            from autoortho.aostats import STATS
        except ImportError:
            from aostats import STATS
        joined = []
        for key, value in STATS.items():
            if isinstance(key, str) and key.startswith(prefix + ":") and value:
                try:
                    joined.extend(value)
                except TypeError:
                    continue
        return joined

    @staticmethod
    def _largest_stat(prefix):
        """A per-process gauge's largest value over the workers."""
        try:
            from autoortho.aostats import STATS
        except ImportError:
            from aostats import STATS
        largest = 0
        for key, value in STATS.items():
            if isinstance(key, str) and key.startswith(prefix + ":"):
                try:
                    largest = max(largest, int(value or 0))
                except (TypeError, ValueError):
                    continue
        return largest

    @staticmethod
    def _summed_stat(prefix):
        """A per-process gauge (``prefix:<pid>``) summed over the workers."""
        try:
            from autoortho.aostats import STATS
        except ImportError:
            from aostats import STATS
        total = 0
        for key, value in STATS.items():
            if isinstance(key, str) and key.startswith(prefix + ":"):
                try:
                    total += int(value or 0)
                except (TypeError, ValueError):
                    continue
        return total

    def _governor_mode(self):
        mode = str(getattr(getattr(self.cfg, "governor", None), "mode", "auto"))
        return mode.strip().lower()

    def _governor_profile_key(self, governor_module, connection_module):
        mode, zoom, airport, steps = (
            str(getattr(self.cfg.autoortho, "max_zoom_mode", "fixed")),
            int(getattr(self.cfg.autoortho, "max_zoom", 16)),
            int(getattr(self.cfg.autoortho, "max_zoom_near_airports", 18)),
            getattr(self.cfg.autoortho, "dynamic_zoom_steps", []),
        )
        target = {"mode": mode, "zoom": zoom, "airport": airport, "steps": str(steps)}
        provider = connection_module.provider_for(
            getattr(self.cfg.autoortho, "maptype_override", "")
        )
        return governor_module.profile_key(
            target, provider, governor_module.network_fingerprint()
        )

    def _governor_bounds(self, hardware_module, RamBrake, active):
        """The governor's limits: line, bandwidth, RAM, VRAM and CPU budget."""
        total = hardware_module.basic().ram_total or 0
        result = aoconfig._connection_for(self.cfg)
        info = hardware_module.basic()
        threads = getattr(active, "cpu_threads", None) if active is not None else None
        logical = getattr(info, "logical_cores", 0) or os.cpu_count() or 0
        return {
            "line_mbps": result.mbits if result is not None and result.mbits else None,
            "bandwidth_mbps": getattr(active, "bandwidth_mbps", None),
            "ram_limit_bytes": (
                int((active.ram_gb - 0.5) * 1024 ** 3)
                if active is not None and active.ram_gb else None
            ),
            "brake_free_bytes": RamBrake.thresholds(total)[0] if total else None,
            "protect_sim_fps": str(getattr(getattr(self.cfg, "limits", None),
                                           "protect_sim_fps", True)).lower()
            in ("true", "1", "yes", "on"),
            "vram_budget_bytes": self._vram_budget_bytes(hardware_module, active),
            # The share of the PC the CPU-core limit gives AutoOrtho.
            "ao_cpu_budget_percent": (
                min(100.0, 100.0 * threads / logical) if threads and logical else None
            ),
        }

    def sync_governor(self, adopt=()):
        """Hand settings applied mid-flight to the running governor.

        Limits, the user's own values (now pinned) and values back on
        automatic are worked out again from the config; ``adopt`` names the
        automatic values the new limits changed, which the governor takes
        over. In Auto the governor's values are published again so the
        workers match it. Returns what was published.
        """
        service = getattr(self, "governor_service", None)
        if service is None:
            return {}
        try:
            (governor_module, hardware_module, _conn,
             _get, _set, RamBrake) = self._governor_modules()
            active = self._resource_limits()
            levers = governor_module.levers_for(self.cfg, active)
            values, baseline, pinned = governor_module.lever_state(self.cfg, levers)
            return service.reconfigure(
                levers, values, baseline, pinned, adopt=adopt,
                **self._governor_bounds(hardware_module, RamBrake, active),
            )
        except Exception:
            log.exception("Could not update the governor with the new settings")
            return {}

    def start_governor(self):
        """Run the governor while streaming (Auto tunes, Advise suggests)."""
        if getattr(self, "governor_service", None) is not None:
            return self.governor_service
        self._after_flight = False
        try:
            (governor_module, hardware_module, connection_module,
             get_stat, set_stat, RamBrake) = self._governor_modules()
            # The workers load the saved config fresh; work the automatic
            # values (baseline, plan, limits) out again here so the governor
            # starts from the same ones even if the target, the limits or
            # the connection test changed since this process loaded it.
            try:
                refreshed = self.cfg.recompute_automatic()
                if refreshed:
                    self.publish_levers(refreshed)
            except Exception:
                log.debug("Could not refresh the automatic values", exc_info=True)
            active = self._resource_limits()
            levers = governor_module.levers_for(self.cfg, active)
            values, baseline, pinned = governor_module.lever_state(self.cfg, levers)
            # Values AutoOrtho picked itself are kept inside the levers'
            # ranges (and a 0 "largest allowed" is a number): make the
            # workers match.
            corrections = {}
            for name, value in values.items():
                if name in pinned:
                    continue
                section, key = name.split(".", 1)
                try:
                    configured = float(self.cfg.value(section, key))
                except (TypeError, ValueError):
                    continue
                if configured != value and not (
                    name in governor_module.ZERO_IS_CEILING and configured <= 0
                ):
                    corrections[name] = value
            if corrections:
                self.publish_levers(corrections)
            mode = self._governor_mode()
            key = self._governor_profile_key(governor_module, connection_module)
            # Auto starts where earlier flights settled; Advise and Auto both
            # keep the suggestions the user accepted (never as pins).
            learned = governor_module.LearnedProfiles().warm_start(key, mode)
            warm = {}
            for name, value in learned.items():
                spec = levers.get(name)
                if spec is None or name in pinned or name not in values:
                    continue
                warm[name] = spec.clamp(value)
            # What the Tuning page marks as "Learned".
            self._governor_learned = dict(warm)
            if warm:
                values.update(warm)
                self.publish_levers(warm)
                log.info("Governor: warm start from earlier flights: %s", warm)
            governor = governor_module.Governor(
                levers,
                values,
                baseline=baseline,
                mode=mode,
                pinned=pinned,
                **self._governor_bounds(hardware_module, RamBrake, active),
            )
            broker = getattr(self, "_download_broker", None)

            def broker_stats():
                current = getattr(self, "_download_broker", None)
                return current.server_stats(timeout=1.0) if current is not None else {}

            def flight():
                try:
                    from autoortho.datareftrack import dt
                except ImportError:
                    from datareftrack import dt
                return dt.get_flight_data()

            record = None
            if str(getattr(getattr(self.cfg, "governor", None), "record_samples", True)).lower() in ("true", "1", "yes", "on"):
                record = os.path.join(
                    governor_module.DATA_DIR,
                    time.strftime("samples-%Y%m%d-%H%M%S.jsonl"),
                )
            service = governor_module.GovernorService(
                governor,
                read_stat=get_stat,
                publish=self.publish_levers,
                broker_stats=broker_stats if broker is not None else None,
                flight=flight,
                record_path=record,
                sum_stat=self._summed_stat,
                max_stat=self._largest_stat,
                collect_stat=self._collected_stat,
            )
            service.start()
            self.governor_service = service
            self._governor_key = key
            self._governor_plan = self._plan_record()
            log.info("Governor started (%s)", mode)
            return service
        except Exception:
            log.exception("Could not start the governor")
            self.governor_service = None
            return None

    def _plan_record(self):
        """What the tuning plan started this flight from (None without one).

        The target, the plan's need, line and ratio, and each setting it
        planned (the download ceiling too, when the line sized it).
        """
        plan = getattr(self.cfg, "_plan", None)
        if plan is None:
            return None
        record = plan.describe()
        try:
            ao = self.cfg.autoortho
            record["target"] = {
                "mode": str(getattr(ao, "max_zoom_mode", "fixed")),
                "zoom": int(getattr(ao, "max_zoom", 16)),
                "airport_zoom": int(getattr(ao, "max_zoom_near_airports", 18)),
            }
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            if self.cfg.source("autoortho", "provider_origin_max_concurrency") == "planned":
                record["values"]["autoortho.provider_origin_max_concurrency"] = str(
                    self.cfg.value("autoortho", "provider_origin_max_concurrency")
                )
                record["reasons"]["autoortho.provider_origin_max_concurrency"] = (
                    "downloads the measured line carries"
                )
        except Exception:
            pass
        return record

    @staticmethod
    def _settled_plan(record, governor):
        """The plan record with where each planned setting ended up.

        ``settings`` maps each planned setting to {planned, settled, moved}:
        the gap between plan and settled value is the plan's error.
        """
        if not record:
            return None
        out = dict(record)
        settings = {}
        for name, text in record.get("values", {}).items():
            try:
                planned = float(text)
            except (TypeError, ValueError):
                continue
            settled = governor.values.get(name)
            entry = {"planned": planned, "reason": record.get("reasons", {}).get(name, "")}
            if settled is not None:
                entry["settled"] = float(settled)
                entry["moved"] = float(settled) != planned
                entry["pinned"] = name in governor.pinned
            settings[name] = entry
        out["settings"] = settings
        return out

    def stop_governor(self):
        """Stop it; leave the flight summary for the report; learn."""
        service = getattr(self, "governor_service", None)
        self.governor_service = None
        if service is None:
            return None
        service.stop()
        try:
            governor_module, _hw, _conn, _get, set_stat, _rb = self._governor_modules()
            governor = service.governor
            summary = (service.flight_summary() if hasattr(service, "flight_summary")
                       else governor.summary())
            plan = self._settled_plan(getattr(self, "_governor_plan", None), governor)
            if plan:
                summary["plan"] = plan
            set_stat("governor/summary", summary)
            self.last_flight = summary
            self._after_flight = True     # Home shows the last-flight card
            governor_module.save_last_flight(summary)
            if governor.mode == "auto":
                # Only where the levers settled away from the automatic
                # values: a lever back at its baseline follows the plan (and
                # its later improvements) instead of a stored copy.
                learned = {
                    name: value for name, value in governor.values.items()
                    if name not in governor.pinned
                    and value != governor.baseline.get(name)
                }
                governor_module.LearnedProfiles().put(
                    getattr(self, "_governor_key", "default"),
                    learned,
                    {
                        "peak_mbps": governor.peak_mbps,
                        "fidelity": summary.get("fidelity"),
                        "peak_graphics_memory_gb": summary.get("peak_graphics_memory_gb"),
                    },
                )
            log.info("Governor stopped: %s", summary.get("verdict"))
            return summary
        except Exception:
            log.exception("Could not finish the governor's flight summary")
            return None

    def apply_live_levers(self):
        """After Apply while streaming: put new limits into effect now.

        Recomputes the values AutoOrtho picks itself under the new limits and
        publishes the ones that changed, plus the limits the workers enforce
        directly (CPU threads, RAM, bandwidth).
        """
        changed = self.cfg.recompute_automatic()
        active = self._resource_limits()
        levers = dict(changed)
        if active is not None:
            levers["limits.cpu_threads"] = active.cpu_threads or 0
            levers["limits.ram_gb"] = active.ram_gb or 0
            levers["limits.bytes_per_second"] = active.bytes_per_second
        published = self.publish_levers(levers)
        # The governor takes the new ranges and the changed automatic values.
        published.update(self.sync_governor(adopt=changed))
        return published

    def stop_download_broker(self):
        broker = getattr(self, "_download_broker", None)
        self._download_broker = None
        self._download_broker_env = {}
        os.environ.pop("AO_HTTP2_BROKER_ADDR", None)
        os.environ.pop("AO_HTTP2_BROKER_TOKEN", None)
        if broker is not None:
            try:
                broker.stop()
            except Exception as exc:
                log.error("Error stopping shared HTTP/2 broker: %s", exc)

    def _launch_scenery_worker(self, root, mountpoint, threading_enabled=True):
        if threading_enabled:
            log.info("Running %s in multi-threaded mode.", mountpoint)
            nothreads = False
        else:
            log.info("Running %s in single-threaded mode.", mountpoint)
            nothreads = True

        root = os.path.expanduser(root)
        mountpoint = os.path.expanduser(mountpoint)
        volname = os.path.basename(os.path.normpath(mountpoint)) or mountpoint

        self._active_mountpoints.append(mountpoint)
        return self.launch_mount_worker(
            root,
            mountpoint,
            volname,
            nothreads,
            self.stats_addr,
            self.stats_auth,
            self.log_addr,
        )

    def _monitor_mount_workers(self):
        healthy = True
        while self.mounts_running:
            for handle in list(self.mount_workers):
                ret = handle.process.poll()
                if ret is not None:
                    log.error(
                        "Mount worker pid %s for %s exited with code %s; failing all mounts.",
                        handle.pid,
                        handle.mountpoint,
                        ret,
                    )
                    self.mounts_running = False
                    healthy = False
                    break
            time.sleep(0.5)
        return healthy

    def mount_workers_alive(self):
        """Return True only when at least one launched mount worker is alive."""
        return bool(self.mount_workers) and all(
            handle.process.poll() is None
            for handle in self.mount_workers
        )

    def mount_single(self, root, mountpoint, threading_enabled=True, blocking=True):
        if self.mount_workers or self._active_mountpoints:
            raise MountError("Previous mount workers have not stopped")
        self._ensure_parent_services()
        self.mounts_running = True
        self._active_mountpoints = []
        self.mount_workers = []
        self.mac_os_procs = []
        self._worker_cpu_share_count = 1
        handle = self._launch_scenery_worker(root, mountpoint, threading_enabled)
        if not blocking:
            return handle

        try:
            self._monitor_mount_workers()
        except (KeyboardInterrupt, SystemExit) as err:
            self.mounts_running = False
            log.info(f"Exiting due to {err}")
        finally:
            log.info("Shutting down ...")
            self.unmount_sceneries()
        return handle

    def mount_sceneries(self, blocking=True):
        if self.mount_workers or self._active_mountpoints:
            log.error("Cannot start streaming until previous mounts have stopped")
            return False
        self._ensure_parent_services()

        # Clean up any stale mount folders left behind from a crash
        try:
            custom_scenery_path = getattr(self.cfg, 'xplane_custom_scenery_path', None)
            if custom_scenery_path:
                cleanup_stale_mount_folders(custom_scenery_path)
        except Exception as e:
            log.warning(f"Failed to cleanup stale mount folders: {e}")

        if not self.cfg.scenery_mounts:
            log.warning(f"No installed sceneries detected.  Exiting.")
            return False

        self.mounts_running = True
        self._active_mountpoints = []
        self.mount_workers = []
        self.mac_os_procs = []
        self._worker_cpu_share_count = max(1, len(self.cfg.scenery_mounts))
        try:
            for scenery in self.cfg.scenery_mounts:
                self._launch_scenery_worker(
                    scenery.get('root'),
                    scenery.get('mount'),
                    self.cfg.fuse.threading,
                )
        except Exception:
            log.exception("Failed to launch scenery mount workers")
            self.unmount_sceneries(force=True)
            return False

        if not blocking:
            log.info("Running mounts in non-blocking mode.")
            time.sleep(1)
            healthy = self.mount_workers_alive() and diagnose(self.cfg)
            if not healthy:
                log.error("Mount startup diagnostics failed")
                self.unmount_sceneries(force=True)
                return False
            self.start_governor()
            return True

        self.start_governor()

        healthy = True
        try:
            def handle_sigterm(sig, frame):
                raise(SystemExit)

            signal.signal(signal.SIGTERM, handle_sigterm)

            time.sleep(1)
            # Check things out
            diagnose(self.cfg)

            healthy = self._monitor_mount_workers()

        except (KeyboardInterrupt, SystemExit) as err:
            self.mounts_running = False
            log.info(f"Exiting due to {err}")
        finally:
            log.info("Shutting down ...")
            self.unmount_sceneries()
        return healthy

    def unmount_sceneries(self, force=False):
        log.info("Unmounting ...")
        self.mounts_running = False
        self.stop_governor()
        try:
            self._live_settings().clear()
        except Exception as exc:
            log.debug("Could not clear live levers: %s", exc)

        # Stop TimeExclusionManager
        try:
            try:
                from autoortho.time_exclusion import time_exclusion_manager
            except ImportError:
                from time_exclusion import time_exclusion_manager
            time_exclusion_manager.stop()
        except Exception as e:
            log.debug(f"Error stopping time_exclusion_manager: {e}")

        mountpoints = list(dict.fromkeys(self._active_mountpoints))
        if not mountpoints:
            mountpoints = [
                os.path.expanduser(scenery.get('mount'))
                for scenery in self.cfg.scenery_mounts
                if scenery.get('mount')
            ]

        if system_type == "darwin":
            supervisor = getattr(self, "mount_worker_supervisor", None)
            if supervisor is not None:
                supervisor.prepare_shutdown()
            # Each helper has a deadline; detach all regions concurrently so
            # one stuck filesystem cannot hold up the remaining mounts.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(8, len(mountpoints))),
                thread_name_prefix="ao-unmount",
            ) as executor:
                results = list(executor.map(
                    lambda path: self.unmount(
                        path, force=force, wait_timeout=DEFAULT_WORKER_STOP_TIMEOUT
                    ),
                    mountpoints,
                ))
            if not all(results):
                log.error("Unmount incomplete; keeping workers and shared services alive")
                return False
        else:
            for mountpoint in mountpoints:
                self.unmount(
                    mountpoint, force=force,
                    wait_timeout=DEFAULT_WORKER_STOP_TIMEOUT,
                )

        stopped = self.stop_mount_workers(timeout=DEFAULT_WORKER_STOP_TIMEOUT)
        if stopped is False:
            log.error("Workers still alive; keeping shared services for a shutdown retry")
            return False
        self.stop_download_broker()

        remaining_mounts = [
            mountpoint for mountpoint in mountpoints
            if safe_ismount(mountpoint)
        ]

        if remaining_mounts:
            log.error("Unmount incomplete; mountpoints still active: %s", remaining_mounts)
            return False

        if system_type == "darwin":
            for mountpoint in mountpoints:
                try:
                    cleanup_mountpoint(mountpoint)
                except OSError as exc:
                    log.warning("Could not restore mount placeholder %s: %s", mountpoint, exc)

        self._finish_performance_diagnostics()

        self.stop_reporter()

        self.stop_stats_manager()

        self.stop_log_server()

        self._active_mountpoints = []
        log.info("Unmount complete")
        return True

    def domount(self, root, mountpoint, threading=True):
        return self._launch_scenery_worker(root, mountpoint, threading)

    def unmount(self, mountpoint, force=False, wait_timeout=10.0):
        log.info(f"Shutting down {mountpoint}")
        if system_type == "darwin":
            return macsetup.unmount_macfuse(mountpoint, force=force, timeout=wait_timeout)
        poison_path = os.path.join(mountpoint, ".poison")

        try:
            os.lstat(poison_path)  # triggers getattr('.poison') -> fuse_exit
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug(f"Poison trigger stat failed: {exc}")

        if not force:
            deadline = time.time() + wait_timeout
            while time.time() < deadline:
                if not safe_ismount(mountpoint):
                    break
                time.sleep(0.5)

        if safe_ismount(mountpoint):
            try:
                import subprocess
                if system_type == 'linux':
                    log.info(f"Force unmounting {mountpoint} via fusermount -u -z")
                    if shutil.which("fusermount"):
                        subprocess.run(["fusermount", "-u", "-z", mountpoint],
                                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.run(["umount", "-l", mountpoint],
                                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif system_type == 'windows':
                    log.info(f"Force unmounting {mountpoint} via winsetup.force_unmount")
                    try:
                        winsetup.force_unmount(mountpoint)  # implement this in winsetup for both backends
                    except Exception as exc:
                        log.warning(f"Windows force unmount failed: {exc}")
            except Exception as exc:
                log.warning(f"Force unmount attempt failed: {exc}")
        return not safe_ismount(mountpoint)


class AOMountUI(AOMount, config_ui.ConfigUI):
    def __init__(self, *args, **kwargs):
        AOMount.__init__(self, *args, **kwargs)
        config_ui.ConfigUI.__init__(self, *args, **kwargs)
    
    def mount_sceneries(self, blocking=True):
        """Mount sceneries using AOMount functionality"""
        return AOMount.mount_sceneries(self, blocking)
    
    def unmount_sceneries(self, force=False):
        """Unmount sceneries using AOMount functionality"""
        return AOMount.unmount_sceneries(self, force=force)


def main():
    log.info(f"AutoOrtho version: {__version__}")

    parser = argparse.ArgumentParser(
        description="AutoOrtho: X-Plane scenery streamer"
    )
    parser.add_argument(
        "root",
        help = "Root directory of orthophotos",
        nargs="?"
    )
    parser.add_argument(
        "mountpoint",
        help = "Directory within X-Plane 11 custom scenery folder to mount",
        nargs="?"
    )
    parser.add_argument(
        "-c",
        "--configure",
        default=False,
        action="store_true",
        help = "Run the configuration setup again."
    )
    parser.add_argument(
        "-H",
        "--headless",
        default=False,
        action="store_true",
        help = "Run in headless mode."
    )

    args = parser.parse_args()

    CFG = aoconfig.CFG
    if args.configure or (CFG.general.showconfig and not args.headless):
        # Show cfgui at start
        run_headless = False
    else:
        # Don't show cfgui
        run_headless = True


    import flighttrack
    ftrack = threading.Thread(
        target=flighttrack.run,
        daemon=True
    )

    # Start helper threads
    ftrack.start()

    from datareftrack import dt
    dt.start()
    
    # Run things
    if args.root and args.mountpoint:
        # Just mount specific requested dirs
        root = args.root
        mountpoint = args.mountpoint
        log.info("root: %s", root)
        log.info("mountpoint: %s", mountpoint)
        aom = AOMount(CFG)
        aom.mount_single(
            root,
            mountpoint,
            CFG.fuse.threading,
            blocking=True,
        )
    elif run_headless:
        log.info("Running headless")
        aom = AOMount(CFG)
        aom.mount_sceneries()
    else:
        log.info("Running CFG UI")
        if USE_QT:
            app = QApplication(sys.argv)
            apply_theme(app)
            
            # Set application icon - required for macOS dock icon visibility
            if system_type == 'darwin':
                # Use native macOS icon format
                app.setWindowIcon(QIcon(":/imgs/ao-icon.icns"))
            elif system_type == 'windows':
                app.setWindowIcon(QIcon(":/imgs/ao-icon.ico"))
            else:
                app.setWindowIcon(QIcon(":/imgs/ao-icon.png"))
            
            cfgui = AOMountUI(CFG)
            cfgui.show()
            app.exec()
        else:
            cfgui = AOMountUI(CFG)
            cfgui.setup()

    dt.stop()
    flighttrack.ft.stop()

    log.info("AutoOrtho exit.")

    # Ensure global shutdown runs after FUSE on macOS.
    # Drain workers; force-exit if threads remain.
    try:
        from autoortho.__main__ import _global_shutdown as __global_shutdown
        __global_shutdown()
    except Exception:
        pass

    # macOS: final safeguard. Force-terminate to avoid lingering
    # macFUSE C-threads.
    if system_type == "darwin":
        os._exit(0)


if __name__ == '__main__':
    main()

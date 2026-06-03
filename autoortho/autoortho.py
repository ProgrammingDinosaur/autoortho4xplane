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
import threading
import socketserver
import logging.handlers
import pickle
import struct


from contextlib import contextmanager
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
    from autoortho import config_ui_qt as config_ui
except ImportError:
    import config_ui_qt as config_ui

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


_SERVER_STATS_STORE = None

def _get_or_create_stats_store():
    """Factory used by the StatsManager server process to expose a singleton store."""
    global _SERVER_STATS_STORE
    if _SERVER_STATS_STORE is None:
        _SERVER_STATS_STORE = aostats.StatsStore()
    return _SERVER_STATS_STORE


@contextmanager
def setupmount(mountpoint, systemtype):
    mountpoint = os.path.expanduser(mountpoint)
    placeholder_path = os.path.join(mountpoint, ".AO_PLACEHOLDER")
    created_mount_dir = False
    had_placeholder = False

    # Preflight: ensure mount dir is a directory and not currently mounted
    if safe_ismount(mountpoint):
        log.warning(f"{mountpoint} is already mounted; attempting to unmount")
        try:
            if systemtype in ("winfsp-FUSE", "dokan-FUSE"):
                try:
                    winsetup.force_unmount(mountpoint)
                except Exception as exc:
                    log.debug(f"Windows force_unmount preflight failed: {exc}")
            elif systemtype == "macOS":
                try:
                    import subprocess
                    subprocess.run(["diskutil", "unmount", "force", mountpoint],
                                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as exc:
                    log.debug(f"macOS preflight unmount failed: {exc}")
            elif systemtype == "Linux-FUSE":
                try:
                    import subprocess
                    if shutil.which("fusermount"):
                        subprocess.run(["fusermount", "-u", "-z", mountpoint],
                                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.run(["umount", "-l", mountpoint],
                                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as exc:
                    log.debug(f"Linux preflight unmount failed: {exc}")
        except Exception as exc:
            log.debug(f"Preflight unmount exception (ignored): {exc}")
        # Wait briefly for unmount to complete
        deadline = time.time() + 10
        while time.time() < deadline:
            if not safe_ismount(mountpoint):
                break
            time.sleep(0.5)
        if safe_ismount(mountpoint):
            raise MountError(f"{mountpoint} is already mounted")
    # For WinFsp, the directory must NOT exist; let winsetup handle removal of placeholders.
    if systemtype != "winfsp-FUSE":
        if not os.path.exists(mountpoint):
            os.makedirs(mountpoint, exist_ok=True)
        elif not os.path.isdir(mountpoint):
            raise MountError(f"{mountpoint} exists but is not a directory")

        # If it's not empty and doesn't look like our placeholder, refuse
        if os.listdir(mountpoint):
            # Treat certain metadata or stale control files as ignorable
            try:
                entries = [e for e in os.listdir(mountpoint) if e not in ('.DS_Store', '.metadata_never_index', '.poison')]
            except Exception:
                entries = os.listdir(mountpoint)

            # If only ignorable files remain, clean them up and proceed
            if not entries:
                try:
                    poison_fp = os.path.join(mountpoint, '.poison')
                    if os.path.exists(poison_fp):
                        os.remove(poison_fp)
                except Exception:
                    pass
            elif os.path.exists(placeholder_path):
                # Remove our placeholder content to ensure an empty dir for FUSE
                try:
                    for name in ('Earth nav data', 'terrain', 'textures'):
                        p = os.path.join(mountpoint, name)
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                        elif os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    # Remove the placeholder marker file/dir last
                    try:
                        if os.path.isdir(placeholder_path) and not os.path.islink(placeholder_path):
                            shutil.rmtree(placeholder_path, ignore_errors=True)
                        elif os.path.exists(placeholder_path):
                            os.remove(placeholder_path)
                    except Exception:
                        pass
                except Exception as e:
                    log.warning(f"Failed to cleanup placeholder content at {mountpoint}: {e}")
                # After cleanup, verify directory is now empty
                if any(e for e in os.listdir(mountpoint) if e not in ('.DS_Store', '.metadata_never_index')):
                    raise MountError(f"Mount point {mountpoint} is not empty after cleanup")
            else:
                raise MountError(f"Mount point {mountpoint} exists and is not empty")

    # Platform-specific setup
    if systemtype == "Linux-FUSE":
        pass
    elif systemtype == "dokan-FUSE":
        if not winsetup.setup_dokan_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    elif systemtype == "winfsp-FUSE":
        if not winsetup.setup_winfsp_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    elif systemtype == "macOS":
        if not macsetup.setup_macfuse_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    else:
        raise MountError(f"Unknown system type: {systemtype} for mount {mountpoint}")

    try:
        yield mountpoint

    finally:
        # Do not remove if still mounted; just try to present placeholder content.
        try:
            cleanup_mountpoint(mountpoint)
        except Exception as e:
            log.warning(f"Failed to cleanup mountpoint {mountpoint}: {e}")


def diagnose(CFG):

    location = geocoder.ip("me")

    log.info("Waiting for mounts...")
    for scenery in CFG.scenery_mounts:
        mount = scenery.get('mount')
        ret = False
        # Use shorter sleeps with more attempts to avoid long pauses
        for i in range(40):
            time.sleep(0.25)
            try:
                if system_type == 'darwin':
                    # Require an actual FUSE mount on macOS; placeholders can mask failures
                    ret = macsetup.is_macfuse_mount(mount)
                else:
                    ret = os.path.isdir(os.path.join(mount, 'textures'))
            except Exception:
                ret = False
            if ret:
                break
            log.info('.')

    failed = False
    log.info("\n\n")
    log.info("------------------------------------")
    log.info(" Diagnostic check ...")
    log.info("------------------------------------")
    log.info(f"Detected system: {platform.uname()}")
    log.info(f"Detected location {location.address}")
    log.info(f"Detected installed scenery:")
    for scenery in CFG.scenery_mounts:
        root = scenery.get('root')
        mount = scenery.get('mount')
        log.info(f"    {root}")
        try:
            if system_type == 'darwin':
                ret = macsetup.is_macfuse_mount(mount)
            else:
                ret = os.path.isdir(os.path.join(mount, 'textures'))
        except Exception:
            ret = False
        log.info(f"        Mounted? {ret}")
        if not ret:
            failed = True

    log.info(f"Checking maptypes:")
    import getortho
    for maptype in MAPTYPES:
        if maptype in ("Use tile default", "Custom Map"):
            continue
        # Use ignore_cleanup_errors=True to handle race with async cache writes
        # The async cache writer may still be writing when the temp dir is cleaned up
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            c = getortho.Chunk(2176, 3232, maptype, 13, cache_dir=tmpdir)
            ret = c.get()
            # Give async cache writer a moment to complete or detect deleted dir
            time.sleep(0.1)
            if ret:
                log.info(f"    Maptype: {maptype} OK!")
            else:
                log.warning(f"    Maptype: {maptype} FAILED!")
                failed = True

    log.info("------------------------------------")
    if failed:
        log.warning("***************")
        log.warning("***************")
        log.warning("FAILURES DETECTED!!")
        log.warning("Please review logs and setup.")
        log.warning("***************")
        log.warning("***************")
        return False
    else:
        log.info(" Diagnostics done.  All checks passed")
        return True
    log.info("------------------------------------\n\n")


class AOMount:
    mounts_running = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.mount_threads = []
        self.mount_workers = []
        self.mac_os_procs = []
        self._active_mountpoints = []
        self.mount_worker_supervisor = AOProcessSupervisor()

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
        log.info(f"AutoOrtho:  root: {root}  mountpoint: {mountpoint}")
        loglevel = getattr(self.cfg.general, 'file_log_level', 'INFO').upper()
        handle = self.mount_worker_supervisor.start_mount_worker(
            root,
            mountpoint,
            volname,
            nothreads,
            stats_addr=stats_addr,
            stats_auth=stats_auth,
            log_addr=log_addr,
            loglevel=loglevel,
        )
        self.mount_workers.append(handle)
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
        self.mount_worker_supervisor.stop_all(timeout=timeout)
        self.mount_workers = []
        self.mac_os_procs = []
        self.mount_threads = []
        return

    def stop_macfuse_workers(self, timeout: float = DEFAULT_WORKER_STOP_TIMEOUT):
        self.stop_mount_workers(timeout=timeout)
        return

    def reporter(self):
        while True:
            time.sleep(10)
            snap = self._shared_store.snapshot()
            log.info(f"STATS: {snap}")

    def start_reporter(self, interval_sec: float = 10.0):
        """Start the periodic global-stats logger (macOS parent)."""
        if self._reporter_thread and self._reporter_thread.is_alive():
            return
        self._reporter_stop.clear()

        def _reporter_loop():
            while not self._reporter_stop.wait(interval_sec):
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
                            if isinstance(k, str) and k.startswith('proc_mem_rss_bytes:'):
                                pid = k.split(':', 1)[1]
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

                    snap = self._shared_store.snapshot()
                    # Hide internal per-process and batching keys from logs
                    # Keep proc_mem_mb for debugging memory issues
                    try:
                        def _is_internal(k):
                            return (
                                (isinstance(k, str) and (
                                    k.startswith('proc_mem_rss_bytes') or
                                    k.startswith('proc_alive_ts') or
                                    k.startswith('proc_threads') or
                                    k.startswith('last_tile_access_ts') or
                                    k.startswith('mm_count:') or
                                    k.startswith('mm_time_total_ms:') or
                                    k.startswith('partial_mm_count:') or
                                    k.startswith('partial_mm_time_total_ms:')
                                )) or k in ('proc_count', 'cur_mem_mb_ts')
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

    def _ensure_parent_services(self):
        if not getattr(self, "stats_manager", None):
            self.start_stats_manager()
        if not getattr(self, "log_server", None):
            self.start_log_server()
        self.start_reporter()

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
                    break
            time.sleep(0.5)

    def mount_single(self, root, mountpoint, threading_enabled=True, blocking=True):
        self._ensure_parent_services()
        self.mounts_running = True
        self._active_mountpoints = []
        self.mount_workers = []
        self.mac_os_procs = []
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
            return

        self.mounts_running = True
        self._active_mountpoints = []
        self.mount_workers = []
        self.mac_os_procs = []
        for scenery in self.cfg.scenery_mounts:
            self._launch_scenery_worker(
                scenery.get('root'),
                scenery.get('mount'),
                self.cfg.fuse.threading,
            )

        if not blocking:
            log.info("Running mounts in non-blocking mode.")
            time.sleep(1)
            diagnose(self.cfg)
            return

        try:
            def handle_sigterm(sig, frame):
                raise(SystemExit)

            signal.signal(signal.SIGTERM, handle_sigterm)

            time.sleep(1)
            # Check things out
            diagnose(self.cfg)

            self._monitor_mount_workers()

        except (KeyboardInterrupt, SystemExit) as err:
            self.mounts_running = False
            log.info(f"Exiting due to {err}")
        finally:
            log.info("Shutting down ...")
            self.unmount_sceneries()

    def unmount_sceneries(self, force=False):
        log.info("Unmounting ...")
        self.mounts_running = False

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

        for mountpoint in mountpoints:
            self.unmount(
                mountpoint,
                force=force,
                wait_timeout=DEFAULT_WORKER_STOP_TIMEOUT,
            )

        self.stop_mount_workers(timeout=DEFAULT_WORKER_STOP_TIMEOUT)

        self.stop_reporter()

        self.stop_stats_manager()

        self.stop_log_server()

        self._active_mountpoints = []
        log.info("Unmount complete")

    def domount(self, root, mountpoint, threading=True):
        return self._launch_scenery_worker(root, mountpoint, threading)

    def unmount(self, mountpoint, force=False, wait_timeout=10.0):
        log.info(f"Shutting down {mountpoint}")
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
                if system_type == 'darwin':
                    log.info(f"Force unmounting {mountpoint} via diskutil")
                    subprocess.run(["diskutil", "unmount", "force", mountpoint],
                                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif system_type == 'linux':
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


class AOMountUI(AOMount, config_ui.ConfigUI):
    def __init__(self, *args, **kwargs):
        AOMount.__init__(self, *args, **kwargs)
        config_ui.ConfigUI.__init__(self, *args, **kwargs)
    
    def mount_sceneries(self, blocking=True):
        """Mount sceneries using AOMount functionality"""
        return AOMount.mount_sceneries(self, blocking)
    
    def unmount_sceneries(self):
        """Unmount sceneries using AOMount functionality"""
        return AOMount.unmount_sceneries(self)


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

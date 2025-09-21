#!/usr/bin/env python

import os
import subprocess
import sys
import time
import ctypes
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


import aoconfig
import aostats
import winsetup
import macsetup
from utils.mount_utils import cleanup_mountpoint
from utils.constants import MAPTYPES, system_type

from version import __version__

import logging
log = logging.getLogger(__name__)

import geocoder

# Import PyQt6 modules

from PySide6.QtWidgets import QApplication
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
    if os.path.ismount(mountpoint):
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
            if not os.path.ismount(mountpoint):
                break
            time.sleep(0.5)
        if os.path.ismount(mountpoint):
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
                    # Prefer OS-level FUSE readiness on macOS
                    ret = macsetup.is_macfuse_mount(mount) or os.path.isdir(os.path.join(mount, 'textures'))
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
                ret = macsetup.is_macfuse_mount(mount) or os.path.isdir(os.path.join(mount, 'textures'))
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
        if maptype == "Use tile default":
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            c = getortho.Chunk(2176, 3232, maptype, 13, cache_dir=tmpdir)
            ret = c.get()
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
        self.mac_os_procs = []

        # Start shared stats manager and reporter/log servers
        self.start_stats_manager()

        self._reporter_stop = threading.Event()
        self._reporter_thread = None
        self.start_reporter()

        if system_type == "darwin":
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

    def launch_macfuse_worker(
            self, root: str,
            mountpoint: str,
            volname: str,
            nothreads: bool,
            stats_addr=None,
            stats_auth=None,
            log_addr=None,
    ) -> subprocess.Popen:
        log.info(f"AutoOrtho:  root: {root}  mountpoint: {mountpoint}")

        env = os.environ.copy()
        if stats_addr:
            env['AO_STATS_ADDR'] = stats_addr
            env['AO_STATS_AUTH'] = stats_auth.decode('utf-8')

        if log_addr:
            env['AO_LOG_ADDR'] = log_addr

        cmd = [sys.executable, os.path.abspath("autoortho/macfuse_worker.py"),
            "--root", root, "--mountpoint", mountpoint, "--volname", volname]

        if nothreads:
            cmd.append("--nothreads")
        p = subprocess.Popen(cmd, env=env)
        log.info(f"FUSE process for mount {volname} started with pid: {p.pid}")
        return p

    def stop_macfuse_workers(self, timeout: float = 10.0):
        log.info("Send SIGTERM to macOS processes...")
        for p in self.mac_os_procs:
            if p.poll() is None:
                p.terminate()

        log.info("Wait on macOS processes...")
        try:
            for p in self.mac_os_procs:
                p.wait(timeout=timeout)
        except Exception as e:
            log.error(f"Error waiting on macOS processes: {e}")
            pass

        for p in self.mac_os_procs:
            if p.poll() is None:
                log.warning("Process %s still alive; sending SIGKILL", p.pid)
                try:
                    p.kill()
                except Exception as e:
                    log.error(f"Error killing macOS process: {e}")
                    pass
        self.mac_os_procs = []
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
                            # 'proc_count' is not required externally; only publish cur_mem_mb
                            self._shared_store.set('cur_mem_mb', total_rss // 1048576)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    # Aggregate mm and partial-mm counters into averages and counts
                    try:
                        keys = self._shared_store.keys()
                        mm_counts = {}
                        mm_averages = {}
                        p_counts = {}
                        p_averages = {}
                        for k in keys:
                            if not isinstance(k, str):
                                continue
                            if k.startswith('mm_count:'):
                                try:
                                    mm = int(k.split(':', 1)[1])
                                except Exception:
                                    continue
                                cnt = int(self._shared_store.get(k, 0) or 0)
                                tot = int(self._shared_store.get(f'mm_time_total_ms:{mm}', 0) or 0)
                                if cnt > 0:
                                    mm_counts[mm] = cnt
                                    mm_averages[mm] = round(tot / cnt / 1000.0, 3)
                            elif k.startswith('partial_mm_count:'):
                                try:
                                    mm = int(k.split(':', 1)[1])
                                except Exception:
                                    continue
                                cnt = int(self._shared_store.get(k, 0) or 0)
                                tot = int(self._shared_store.get(f'partial_mm_time_total_ms:{mm}', 0) or 0)
                                if cnt > 0:
                                    p_counts[mm] = cnt
                                    p_averages[mm] = round(tot / cnt / 1000.0, 3)
                        # Publish aggregated views only when non-empty; otherwise remove
                        try:
                            if mm_counts:
                                self._shared_store.set('mm_counts', mm_counts)
                                self._shared_store.set('mm_averages', mm_averages)
                            else:
                                try:
                                    self._shared_store.delete('mm_counts')
                                    self._shared_store.delete('mm_averages')
                                except Exception:
                                    pass

                            if p_counts:
                                self._shared_store.set('partial_mm_counts', p_counts)
                                self._shared_store.set('partial_mm_averages', p_averages)
                            else:
                                try:
                                    self._shared_store.delete('partial_mm_counts')
                                    self._shared_store.delete('partial_mm_averages')
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    except Exception:
                        pass

                    snap = self._shared_store.snapshot()
                    # Hide internal per-process and batching keys from logs
                    try:
                        def _is_internal(k):
                            return (
                                (isinstance(k, str) and (
                                    k.startswith('proc_mem_rss_bytes') or
                                    k.startswith('proc_alive_ts') or
                                    k.startswith('mm_count:') or
                                    k.startswith('mm_time_total_ms:') or
                                    k.startswith('partial_mm_count:') or
                                    k.startswith('partial_mm_time_total_ms:')
                                )) or k in ('proc_count',)
                            )
                        filtered = {k: v for k, v in snap.items() if not _is_internal(k)}
                    except Exception:
                        filtered = snap

                    # Ensure nested dicts are logged with numerically sorted keys
                    try:
                        for _name in ('mm_counts', 'mm_averages', 'partial_mm_counts', 'partial_mm_averages'):
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
                    log.debug("reporter(): %s", e)

        t = threading.Thread(target=_reporter_loop, name="AO-Reporter", daemon=True)
        t.start()
        self._reporter_thread = t

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

    def mount_sceneries(self, blocking=True):
        if not self.cfg.scenery_mounts:
            log.warning(f"No installed sceneries detected.  Exiting.")
            return

        self.mounts_running = True
        for scenery in self.cfg.scenery_mounts:
            if system_type == "darwin":
                self.domount(
                    scenery.get('root'),
                    scenery.get('mount'),
                    self.cfg.fuse.threading
                )
            else:
                t = threading.Thread(
                    target=self.domount,
                    daemon=False,
                    args=(
                        scenery.get('root'),
                        scenery.get('mount'),
                        self.cfg.fuse.threading
                    )
                )
                t.start()
                self.mount_threads.append(t)

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

            while self.mounts_running:

                if system_type == "darwin": 
                    for p in self.mac_os_procs:
                        if p.poll() is not None:   # process has exited
                            log.error(f"FUSE process {p.pid} exited; failing all mounts.")
                            self.mounts_running = False
                            break

                else:
                    for t in list(self.mount_threads):
                        if not t.is_alive():
                            log.error(f"Mount thread {t.name or t.ident} died; failing all mounts.")
                            self.mounts_running = False
                            break
  
                time.sleep(0.5)

        except (KeyboardInterrupt, SystemExit) as err:
            self.mounts_running = False
            log.info(f"Exiting due to {err}")
        finally:
            log.info("Shutting down ...")
            self.unmount_sceneries()

    def unmount_sceneries(self, force=False):
        log.info("Unmounting ...")
        self.mounts_running = False
        for scenery in self.cfg.scenery_mounts:
            self.unmount(scenery.get('mount'), force)

        self.stop_reporter()

        log.info("Wait on mount threads...")
        for t in self.mount_threads:
            t.join(5)
            log.info(f"Thread {t.ident} exited.")

        if system_type == "darwin":
            self.stop_macfuse_workers()

        self.stop_stats_manager()

        self.stop_log_server()

        log.info("Unmount complete")

    def domount(self, root, mountpoint, threading=True):

        if threading:
            log.info("Running in multi-threaded mode.")
            nothreads = False
        else:
            log.info("Running in single-threaded mode.")
            nothreads = True

        root = os.path.expanduser(root)

        # Cleanup: remove any stale poison file in the root to prevent
        # accidental FUSE self-termination if touched after mount
        try:
            poison_root = os.path.join(root, ".poison")
            if os.path.exists(poison_root):
                os.remove(poison_root)
        except Exception as exc:
            log.debug(f"Ignoring failure to remove root poison file: {exc}")

        try:
            if system_type == 'windows':
                systemtype, libpath = winsetup.find_win_libs()
                with setupmount(mountpoint, systemtype) as mount:
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import mfusepy
                    import autoortho_fuse
                    mfusepy._libfuse = ctypes.CDLL(libpath)
                    autoortho_fuse.run(
                            autoortho_fuse.AutoOrtho(root),
                            mount,
                            mount.split('/')[-1],
                            nothreads
                    )
            elif system_type == 'darwin':
                # systemtype, libpath = macsetup.find_mac_libs()
                systemtype = "macOS"
                with setupmount(mountpoint, systemtype) as mount:
                    process = self.launch_macfuse_worker(
                        root, mountpoint, mount.split('/')[-1], nothreads,
                        self.stats_addr, self.stats_auth, self.log_addr
                    )
                    self.mac_os_procs.append(process)
            else:
                # Linux
                with setupmount(mountpoint, "Linux-FUSE") as mount:
                    log.info("Running in FUSE mode.")
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import autoortho_fuse
                    autoortho_fuse.run(
                            autoortho_fuse.AutoOrtho(root),
                            mount,
                            mount.split('/')[-1],
                            nothreads
                    )

        except Exception as err:
            log.exception(f"Exception in FUSE mount: {err}")
            # Per your spec, a failure while X-Plane is running should terminate everything.
            time.sleep(5)
            os._exit(2)

    def unmount(self, mountpoint, force=False):
        log.info(f"Shutting down {mountpoint}")
        poison_path = os.path.join(mountpoint, ".poison")

        try:
            os.lstat(poison_path)  # triggers getattr('.poison') -> fuse_exit
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug(f"Poison trigger stat failed: {exc}")

        if not force:
            deadline = time.time() + 10
            while time.time() < deadline:
                if not os.path.ismount(mountpoint):
                    break
                time.sleep(0.5)

        if os.path.ismount(mountpoint):
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

    # Run things
    if args.root and args.mountpoint:
        # Just mount specific requested dirs
        root = args.root
        mountpoint = args.mountpoint
        log.info("root: %s", root)
        log.info("mountpoint: %s", mountpoint)
        aom = AOMount(CFG)
        aom.domount(
            root,
            mountpoint,
            CFG.fuse.threading
        )
    elif run_headless:
        log.info("Running headless")
        aom = AOMount(CFG)
        aom.mount_sceneries()
    else:
        log.info("Running CFG UI")
        if USE_QT:
            app = QApplication(sys.argv)
            cfgui = AOMountUI(CFG)
            cfgui.show()
            app.exec()
        else:
            cfgui = AOMountUI(CFG)
            cfgui.setup()

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

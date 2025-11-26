#!/usr/bin/env python

#from __future__ import with_statement
import os
import re
import time
import math
import errno
import ctypes
import threading

import flighttrack

from collections import defaultdict
from functools import wraps, lru_cache

from aoconfig import CFG
from utils.constants import system_type
import logging
log = logging.getLogger(__name__)

from mfusepy import FUSE, FuseOSError, Operations, fuse_get_context, _libfuse

import getortho

def deg2num(lat_deg, lon_deg, zoom):
  lat_rad = math.radians(lat_deg)
  n = 2.0 ** zoom
  xtile = int((lon_deg + 180.0) / 360.0 * n)
  ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
  return (xtile, ytile)


def tilemeters(lat_deg, zoom):
    y = 64120000 * math.cos(math.radians(lat_deg)) / (pow(2, zoom))
    x = 64120000 / (pow(2, zoom))
    return (x, y)


MEMTRACE = False


def locked(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            result = fn(self, *args, **kwargs)
        return result
    return wrapped

def fuse_config_by_os() -> dict:
    overrides = {"connection": {}, "config": {}}
    if os.name == 'posix':
        overrides["config"].update(dict(
            uid=-1,
            gid=-1,
            set_uid=1,
            set_gid=1,
        ))
    if system_type == 'linux':
        overrides["config"].update(dict(
            uid=-1,
            gid=-1,
            set_uid=1,
            set_gid=1,
        ))
    elif system_type == 'darwin':
        # Leave defaults
        pass
    elif system_type == 'windows':
        overrides["config"].update(dict(
            uid=-1,
            gid=-1,
            set_uid=1,
            set_gid=1,
        ))
    return overrides

def fuse_option_profiles_by_os(nothreads: bool, mount_name: str) -> dict:

    options = dict(
        nothreads=nothreads,
        foreground=True,
        allow_other=True,
    )
    if system_type == 'linux':
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
        ))
    elif system_type == 'darwin':
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
            volname=mount_name,
        ))

    elif system_type == 'windows':
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
            VolumeName=mount_name,
            FileSystemName=mount_name,
        ))

    return options


class AutoOrtho(Operations):

    open_paths = []
    read_paths = []

    path_dict = {}
    tile_dict = {}

    fh_locks = {}

    fh = 1000

    default_uid = -1
    default_gid = -1

    startup = True

    def __init__(self, root, cache_dir='.cache', *args, **kwargs):
        log.info(f"ROOT: {root}")
        self.dds_re = re.compile(r".*/(\d+)[-_](\d+)[-_]((?!ZL)\S*)(\d{2}).dds")
        self.ktx2_re = re.compile(r".*/(\d+)[-_](\d+)[-_]((?!ZL)\D*)(\d+).ktx2")
        self.dsf_re = re.compile(r".*/[-+]\d+[-+]\d+.dsf")
        self.ter_re = re.compile(r".*/\d+[-_]\d+[-_](\D*)(\d+).ter")
        self.root = os.path.abspath(root)
        self.cache_dir = cache_dir

        self.tc = getortho.TileCacher(cache_dir)
    
        #self.path_condition = threading.Condition()
        #self.read_lock = threading.Lock()
        self.open_paths = []
        self.read_paths = []
        self.path_dict = {}
        self.tile_dict = {}
        self.fh_locks = defaultdict(threading.Lock)
        self.startup = True
        self._lock = threading.RLock()
        self._tile_locks = defaultdict(threading.Lock)
        self._size_cache = {}
        self._ft_started = False
        self._ft_start_lock = threading.Lock()

        self.use_ns = kwargs.get("use_ns", False)

    # Helpers
    # =======

    def init_with_config(self, conn_info, config_3):
        """Called by libfuse during mount. Configure read sizes and caching.

        - Prefer configuring sizes here over mount options to avoid unknown-option
          issues across different libfuse builds.
        """

        # Conn Info Overrides
        overrides = fuse_config_by_os()
        
        # Configure kernel connection limits when available
        try:
            if conn_info is not None:
                if overrides["connection"]:
                    for k, v in overrides["connection"].items():
                        if hasattr(conn_info, k):
                            setattr(conn_info, k, v)
                            log.debug(f"FUSE: set conn.{k}={v}")
                else:
                    log.debug(f"FUSE: no connection overrides")

            if config_3 is not None:
                if overrides["config"]:
                    for k, v in overrides["config"].items():
                        if hasattr(config_3, k):
                            setattr(config_3, k, v)
                            if k == "uid":
                                self.default_uid = v
                            if k == "gid":
                                self.default_gid = v
                            log.debug(f"FUSE: set config.{k}={v}")
                else:
                    log.debug(f"FUSE: no config overrides")
        except Exception as e:
            log.warning(f"FUSE: failed to apply fuse_config tuning: {e}")

    def _ensure_flighttrack_started(self, reason_path=None):
        """Start flight tracking exactly once, when first DDS is touched."""
        if self._ft_started:
            return
        log.info(f"First DDS access at {reason_path}. Starting flight tracker.")
        try:
            flighttrack.ft.start()
        except Exception as e:
            # Keep the FS alive even if flighttrack fails; just log loudly.
            log.error(f"Failed to start flight tracker: {e}")
            # Do NOT mark _ft_started; we'll retry once on next DDS trigger.
            return

        self._ft_started = True

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.abspath(os.path.join(self.root, partial))
        return path

    def _tile_key(self, row, col, maptype, zoom):
        return (row, col, maptype, zoom)

    def _failfast(self, msg, exc=None):
        log.error(msg)
        if exc:
            log.exception("cause:", exc_info=exc)
        # In previous versions we called fuse_exit here, which kills the mount and
        # the simulator session on transient per-tile errors (e.g. cache sharing violations).
        # Instead, surface an I/O error back to the caller and keep the FS alive.
        raise FuseOSError(errno.EIO)

    # Filesystem methods
    # ==================

    def _access(self, path, mode):
        log.debug(f"ACCESS: {path}")
        #m = re.match(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds", path)
        #if m:
        #    log.info(f"ACCESS: Found DDS file {path}: %s " % str(m.groups()))
        full_path = self._full_path(path)
        if not os.access(full_path, mode):
            raise FuseOSError(errno.EACCES)

    def chmod(self, path, mode):
        full_path = self._full_path(path)
        return os.chmod(full_path, mode)

    def chown(self, path, uid, gid):
        full_path = self._full_path(path)
        return os.chown(full_path, uid, gid)

    @lru_cache(maxsize=1024)
    def _calculate_dds_size(self, zoom):
        """Calculate the actual DDS file size based on tile parameters and current configuration."""
        try:
            # Convert parameters to the format expected by the tile system
            zoom = int(zoom)
            
            # Replicate the max_zoom selection logic from TileCacher
            if CFG.autoortho.using_custom_tiles:
                uncapped_target_zoom = self.tc.target_zoom_level
            else:
                uncapped_target_zoom = self.tc.target_zoom_level_near_airports if zoom == 18 else self.tc.target_zoom_level

            max_zoom = min(zoom + 1,uncapped_target_zoom)
            
            # Replicate tile dimension calculation logic from Tile.__init__
            width = 16  # Default tile width in chunks
            height = 16  # Default tile height in chunks
            
            tilezoom_diff = zoom - int(max_zoom)
            
            if tilezoom_diff >= 0:
                chunks_per_row = width >> tilezoom_diff
                chunks_per_col = height >> tilezoom_diff
            else:
                chunks_per_row = width << (-tilezoom_diff)
                chunks_per_col = height << (-tilezoom_diff)
            
            # Calculate DDS dimensions in pixels
            dds_width = chunks_per_row * 256
            dds_height = chunks_per_col * 256
            
            # Replicate DDS size calculation logic from pydds.DDS.__init__
            if CFG.pydds.format == 'BC3':
                blocksize = 16
            else:
                blocksize = 8
            
            # Calculate total size including all mipmaps
            curbytes = 128  # DDS header size
            current_width = dds_width
            current_height = dds_height
            
            while (current_width >= 1) and (current_height >= 1):
                mipmap_size = max(1, (current_width * current_height >> 4)) * blocksize
                curbytes += mipmap_size
                current_width = current_width >> 1
                current_height = current_height >> 1
            
            log.debug(f"Calculated DDS size for zoom {zoom}: {curbytes} bytes "
                     f"(dimensions: {dds_width}x{dds_height}, max_zoom: {max_zoom}, tilezoom_diff: {tilezoom_diff})")
            
            return curbytes
            
        except Exception as e:
            log.warning(f"Failed to calculate DDS size for {zoom}: {e}, using fallback")
            # Fallback to hardcoded values if calculation fails
            if CFG.pydds.format == "BC1":
                return 11184952
            else:
                return 22369776


    @lru_cache(maxsize=1024)
    def getattr(self, path, fh=None):
        log.debug(f"GETATTR {path}")


        m = self.dds_re.match(path)

        now = int(time.time())
        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
        }
        if m:
            self._ensure_flighttrack_started(reason_path=path)
            row, col, maptype, zoom = m.groups()
            key = (int(row), int(col), maptype, int(zoom))
            dds_size = self._size_cache.get(key)
            log.debug("GETATTR: Fetch for path: %s", path)
            if dds_size is None:
                dds_size = self._calculate_dds_size(zoom)

            attrs['st_size'] = dds_size

        elif path.endswith(".poison"):

            attrs['st_size'] = 0
            log.info("Poison pill.  Exiting!")
            fuse_ptr = ctypes.c_void_p(_libfuse.fuse_get_context().contents.fuse)
            do_fuse_exit(fuse_ptr=fuse_ptr)

        elif path.endswith("AOISWORKING"):
            attrs['st_size'] = 0

        else:
            full_path = self._full_path(path)
            exists = os.path.exists(full_path)
            log.debug(f"GETATTR FULLPATH {full_path}  Exists? {exists}")
            st = os.lstat(full_path)
            log.debug(f"GETATTR: Orig stat: {st}")
            attrs = {k: getattr(st, k) for k in (
                'st_atime',
                'st_ctime',
                'st_gid',
                'st_mode',
                'st_mtime',
                'st_nlink',
                'st_size',
                'st_uid',
                'st_ino',
                'st_dev',
                )}

        log.debug(f"GETATTR: ATTRS: {attrs}")
        return attrs

    @lru_cache(maxsize=1024)
    def readdir(self, path, fh):

        if path in ["/textures", "/terrain"]:
            return ['.', '..', 'AOISWORKING']

        full_path = self._full_path(path)
        if os.path.isdir(full_path):
            return ['.', '..', *os.listdir(full_path)]
        return ['.', '..']

    def readlink(self, path):
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            # Path name is absolute, sanitize it.
            return os.path.relpath(pathname, self.root)
        else:
            return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)

    @lru_cache
    def statfs(self, path):
        #log.info(f"STATFS: {path}")
        full_path = self._full_path(path)
        if system_type == 'windows':
            stats = {
                    'f_bavail':47602498, 
                    'f_bfree':47602498,
                    'f_blocks':124699647, 
                    'f_favail':1000000, 
                    'f_ffree':1000000, 
                    'f_files':999, 
                    'f_frsize':4096,
                    'f_flag':1024,
                    'f_bsize':4096 
            }
            return stats
            # st = os.stat(full_path)
            # return dict((key, getattr(st, key)) for key in ('f_bavail', 'f_bfree',
            #     'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            #     'f_frsize', 'f_namemax'))
        elif system_type == 'linux' or system_type == 'darwin':
            stv = os.statvfs(full_path)
            #log.info(stv)
            stats = {}
            possible_keys = ['f_bavail', 'f_bfree',
                'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
                'f_frsize', 'f_namemax']
            
            for key in possible_keys:
                if hasattr(stv, key):
                    stats[key] = getattr(stv, key)
            return stats

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def symlink(self, name, target):
        return os.symlink(target, self._full_path(name))

    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    def link(self, target, name):
        return os.link(self._full_path(name), self._full_path(target))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    # File methods
    # ============

    #@locked
    def open(self, path, flags):

        log.debug(f"OPEN: {path} {flags}")
        full_path = self._full_path(path)
        log.debug(f"OPEN: FULLPATH {full_path}")

        if self.dsf_re.match(path):
            log.info(f"OPEN: Detected DSF open: {path}")
        
        dds_match = self.dds_re.match(path)
        if dds_match:
            row, col, maptype, zoom = dds_match.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            t = self.tc._open_tile(row, col, maptype, zoom)
            try:
                self._size_cache[(row, col, maptype, zoom)] = t.dds.total_size
            except Exception:
                log.debug(f"OPEN: Failed getting cache size for tile at {path}")
                pass
            return 0

        if path.endswith('AOISWORKING'):
            return 0

        if system_type == 'windows':
            return os.open(full_path, flags | os.O_BINARY)
        return os.open(full_path, flags)

    def _create(self, path, mode, fi=None):
        uid, gid, pid = fuse_get_context()
        full_path = self._full_path(path)
        fd = os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)
        os.chown(full_path,uid,gid) #chown to context uid & gid
        return fd

    #@profile
    #@lru_cache
    def read(self, path, length, offset, fh):
        log.debug(f"READ: {path} {offset} {length} {fh}")
        m = self.dds_re.match(path)
        if m:
            row, col, maptype, zoom = m.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            key = self._tile_key(row, col, maptype, zoom)
            lock = self._tile_locks[key]
            if not lock.acquire(timeout=CFG.fuse.build_timeout if hasattr(CFG.fuse, 'build_timeout') else 60):
                self._failfast(f"Tile build lock timeout for {key}")
            try:
                t = self.tc._get_tile(row, col, maptype, zoom)
                data = t.read_dds_bytes(offset, length)
                if data is None:
                    log.error(f"Tile read returned None for {key}")
                    raise FuseOSError(errno.EIO)
                return data
            except FuseOSError:
                # Propagate specific FS error without killing the mount
                raise
            except Exception as e:
                # Log and map to EIO, but do not exit the whole FUSE session
                log.error(f"Tile read/build failed for {key}")
                log.exception("cause:", exc_info=e)
                raise FuseOSError(errno.EIO)
            finally:
                lock.release()

        # Regular file passthrough
        with self.fh_locks.setdefault(fh, threading.Lock()):
            os.lseek(fh, offset, os.SEEK_SET)
            try:
                return os.read(fh, length)
            except OSError as e:
                raise FuseOSError(e.errno)

    # X-Plane never writes to files in the FUSE mount
    def _write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def truncate(self, path, length, fh=None):
        log.debug(f"TRUNCATE")
        full_path = self._full_path(path)
        with open(full_path, 'r+') as f:
            f.truncate(length)

    # X-Plane never writes to files in the FUSE mount
    def _flush(self, path, fh):
        # No-op for virtual DDS files; fsync real files.
        if self.dds_re.match(path):
            return 0
        try:
            os.fsync(fh)
        except OSError as e:
            raise FuseOSError(e.errno)
        return 0

    def releasedir(self, path, fh):
        log.debug(f"RELEASEDIR: {path}")
        return 0
    
    def opendir(self, path):
        log.debug(f"OPENDIR: {path}")
        return 0 

    #@locked
    def release(self, path, fh):
        log.debug(f"RELEASE: {path}")
        dds_match = self.dds_re.match(path)
        if dds_match:
            row, col, maptype, zoom = dds_match.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            self.tc._close_tile(row, col, maptype, zoom)
            return 0
        try:
            return os.close(fh)
        finally:
            self.fh_locks.pop(fh, None)

    # X-Plane never writes to files in the FUSE mount
    def _fsync(self, path, fdatasync, fh):
        log.debug(f"FSYNC: {path}")
        return self.flush(path, fh)


    def close(self, path, fh):
        log.debug(f"CLOSE: {path}")
        return 0


def do_fuse_exit(fuse_ptr=None):
    log.info("fuse_exit called")
    #time.sleep(1)
    if not fuse_ptr:
        fuse_ptr = ctypes.c_void_p(_libfuse.fuse_get_context().contents.fuse)
    _libfuse.fuse_exit(fuse_ptr)


def run(ao, mountpoint, name="", nothreads=False):
    log.info(f"MOUNT: {mountpoint}")
    options = fuse_option_profiles_by_os(nothreads, name)

    log.info(f"Starting FUSE mount")
    log.info(f"Loading FUSE with options: "
            f"{', '.join(sorted(map(str, options.keys())))}")

    try:
        FUSE(ao, os.path.abspath(mountpoint), **options)
        log.info(f"FUSE: Exiting mount {mountpoint}")
        return
    except Exception as e:
        log.error(f"FUSE mount failed with non-negotiable error: {e}")
        raise
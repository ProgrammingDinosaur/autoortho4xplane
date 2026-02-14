#!/usr/bin/env python

#from __future__ import with_statement
import os
import re
import time
import math
import errno
import ctypes
import threading

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho import flighttrack
except ImportError:
    import flighttrack

from collections import defaultdict, OrderedDict
from functools import wraps, lru_cache

try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

try:
    from autoortho.utils.constants import system_type
except ImportError:
    from utils.constants import system_type

try:
    from autoortho.time_exclusion import time_exclusion_manager
except ImportError:
    from time_exclusion import time_exclusion_manager

import logging
log = logging.getLogger(__name__)

try:
    from autoortho.mfusepy import FUSE, FuseOSError, Operations, fuse_get_context, _libfuse
except ImportError:
    from mfusepy import FUSE, FuseOSError, Operations, fuse_get_context, _libfuse

try:
    from autoortho import getortho
except ImportError:
    import getortho

def _rgb_to_rgb565(r: int, g: int, b: int) -> int:
    """Convert RGB888 to RGB565 format used by BC1/DXT1 compression."""
    # RGB565: 5 bits red (high), 6 bits green (mid), 5 bits blue (low)
    r5 = (r >> 3) & 0x1F
    g6 = (g >> 2) & 0x3F
    b5 = (b >> 3) & 0x1F
    return (r5 << 11) | (g6 << 5) | b5

def _get_fallback_bc1_block() -> bytes:
    """
    Generate an 8-byte BC1/DXT1 block using the configured missing_color.
    
    BC1 block format:
    - 2 bytes: color0 (RGB565, little-endian)
    - 2 bytes: color1 (RGB565, little-endian)
    - 4 bytes: 4x4 pixel indices (2 bits each)
    
    For a solid color, we set color0 = color1 and all indices to 0.
    """
    try:
        # Get missing_color from config [R, G, B]
        missing = CFG.autoortho.missing_color
        if isinstance(missing, (list, tuple)) and len(missing) >= 3:
            r, g, b = int(missing[0]), int(missing[1]), int(missing[2])
        else:
            # Fallback to default gray if config is malformed
            r, g, b = 66, 77, 55
    except Exception:
        # Fallback to default if config not available
        r, g, b = 66, 77, 55
    
    # Convert to RGB565
    rgb565 = _rgb_to_rgb565(r, g, b)
    
    # Pack as little-endian 2-byte value (appears twice for color0 and color1)
    color_bytes = rgb565.to_bytes(2, 'little')
    
    # BC1 block: color0, color1, 4 bytes of indices (all 0 for solid color)
    return color_bytes + color_bytes + b'\x00\x00\x00\x00'

# Cache the BC1 block to avoid recomputing on every call
_cached_bc1_block = None

# DDS header size is always 128 bytes
_DDS_HEADER_SIZE = 128

def _generate_fallback_dds_bytes(offset: int, length: int) -> bytes:
    """
    Generate fallback DDS bytes for when tile generation fails.
    
    This returns valid DDS-compatible data that X-Plane can safely render,
    preventing EXCEPTION_IN_PAGE_ERROR crashes on Windows.
    
    The fallback uses the configured missing_color from [autoortho] section,
    converted to BC1/DXT1 compressed format. This ensures the placeholder
    texture visually matches the missing tile color the user has configured.
    
    The function properly handles the offset parameter to return the correct
    portion of the fallback DDS data:
    - Bytes 0-127: DDS header region (returns zeros for valid structure)
    - Bytes 128+: Mipmap data region (returns BC1 blocks in missing_color)
    """
    global _cached_bc1_block
    
    # Generate and cache the BC1 block on first use
    if _cached_bc1_block is None:
        _cached_bc1_block = _get_fallback_bc1_block()
    
    block = _cached_bc1_block
    block_size = 8  # BC1 blocks are 8 bytes each
    
    result = bytearray()
    current_offset = offset
    remaining = length
    
    # Handle header region (bytes 0-127)
    # Return zeros for header bytes - this is safe because X-Plane will see
    # an invalid/empty DDS header and handle it gracefully
    if current_offset < _DDS_HEADER_SIZE:
        header_bytes_needed = min(remaining, _DDS_HEADER_SIZE - current_offset)
        result.extend(b'\x00' * header_bytes_needed)
        current_offset += header_bytes_needed
        remaining -= header_bytes_needed
    
    # Handle mipmap data region (bytes 128+)
    if remaining > 0:
        # Calculate position within mipmap data (offset from byte 128)
        mipmap_offset = current_offset - _DDS_HEADER_SIZE
        
        # Calculate where we are within the 8-byte BC1 block pattern
        block_phase = mipmap_offset % block_size
        
        # If we're not aligned to block boundary, add partial block first
        if block_phase > 0:
            partial_len = min(remaining, block_size - block_phase)
            result.extend(block[block_phase:block_phase + partial_len])
            remaining -= partial_len
        
        # Add complete blocks
        if remaining >= block_size:
            full_blocks = remaining // block_size
            result.extend(block * full_blocks)
            remaining -= full_blocks * block_size
        
        # Add any remaining partial block
        if remaining > 0:
            result.extend(block[:remaining])
    
    return bytes(result)

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
        # Calculate daemon_timeout based on max possible tile_time_budget + 60s buffer.
        # This prevents macFUSE from killing the worker when operations take longer
        # than the default 60 second timeout.
        #
        # Max tile_time_budget calculation (matches _calculate_build_timeout):
        # - Base: tile_time_budget (default 180s)
        # - Startup multiplier: 10x during initial loading (capped at 1800s)
        # - Fallback: + fallback_timeout if enabled (default 30s)
        # - Buffer: + 15s for DDS operations
        # - Safety: + 60s additional buffer for daemon_timeout
        tile_budget = getattr(CFG.autoortho, 'tile_time_budget', 180.0)
        if isinstance(tile_budget, str):
            try:
                tile_budget = float(tile_budget)
            except ValueError:
                tile_budget = 180.0
        
        # Use max startup budget (10x multiplier, capped at 1800s)
        max_startup_budget = min(tile_budget * 10.0, 1800.0)
        
        # Add fallback timeout if enabled
        fallback_extends = getattr(CFG.autoortho, 'fallback_extends_budget', False)
        if isinstance(fallback_extends, str):
            fallback_extends = fallback_extends.lower().strip() in ('true', '1', 'yes', 'on')
        fallback_timeout = 0
        if fallback_extends:
            fallback_timeout = getattr(CFG.autoortho, 'fallback_timeout', 30)
            if isinstance(fallback_timeout, str):
                try:
                    fallback_timeout = float(fallback_timeout)
                except ValueError:
                    fallback_timeout = 30
        
        # daemon_timeout = max_build_timeout + 60s safety buffer
        # max_build_timeout = max_startup_budget + fallback_timeout + 15s buffer
        daemon_timeout = int(max_startup_budget + fallback_timeout + 15 + 60)
        
        options.update(dict(
            nothreads=nothreads,
            foreground=True,
            allow_other=True,
            volname=mount_name,
            daemon_timeout=daemon_timeout,
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
        
        # Register terrain index for this scenery
        # This indexes .ter files to discover actual tile zoom levels
        # Critical for predictive DDS: ensures we prefetch the exact tiles X-Plane will request
        terrain_folder = os.path.join(self.root, "terrain")
        scenery_name = os.path.basename(self.root)
        getortho.register_terrain_index(terrain_folder, scenery_name)
        
        # Start spatial prefetcher for proactive tile loading
        getortho.start_prefetcher(self.tc)
        
        # Start predictive DDS generation (pre-builds DDS in background)
        getortho.start_predictive_dds(self.tc)
    
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
        self._size_cache = OrderedDict()  # LRU cache for DDS sizes
        self._size_cache_max = 5000  # Max entries before LRU eviction
        self._ft_started = False
        self._ft_start_lock = threading.Lock()
        
        # Track redirected DSF file handles: fh -> redirect_path
        # Used when time exclusion redirects DSF reads to global scenery
        self._redirected_dsf_fhs = {}

        self.use_ns = kwargs.get("use_ns", False)
        
        # Initialize time exclusion manager with dataref tracker
        try:
            from autoortho.datareftrack import dt as datareftracker
        except ImportError:
            from datareftrack import dt as datareftracker
        time_exclusion_manager.set_dataref_tracker(datareftracker)

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

    def _calculate_build_timeout(self):
        """
        Calculate dynamic build_timeout based on tile_time_budget settings.
        
        This ensures the FUSE lock timeout is always sufficient for tile building:
        - Base: tile_time_budget (time allowed for chunk downloads)
        - Startup: applies same multiplier (3x, capped at 300s) when suspend_maxwait 
          is enabled and not yet connected to X-Plane
        - Extended: + fallback_timeout if fallback_extends_budget is enabled
        - Buffer: + 15 seconds for DDS operations and overhead
        
        Returns timeout in seconds (int).
        """
        # Get tile_time_budget (default 120s)
        tile_budget = getattr(CFG.autoortho, 'tile_time_budget', 120.0)
        if isinstance(tile_budget, str):
            try:
                tile_budget = float(tile_budget)
            except ValueError:
                tile_budget = 120.0
        
        # Account for startup multiplier - must match getortho.py logic
        # During initial startup (before first connection), budget is multiplied.
        # Uses has_ever_connected to distinguish true startup from temporary disconnects
        # caused by stuttering (which should NOT get extended timeouts).
        try:
            from datareftrack import dt as datareftracker
            suspend_maxwait = getattr(CFG.autoortho, 'suspend_maxwait', True)
            if isinstance(suspend_maxwait, str):
                suspend_maxwait = suspend_maxwait.lower().strip() in ('true', '1', 'yes', 'on')
            
            if suspend_maxwait and not getattr(datareftracker, 'has_ever_connected', False):
                # Match getortho.py: 10x multiplier for initial loading
                startup_multiplier = 10.0
                max_startup_budget = 1800.0
                tile_budget = min(tile_budget * startup_multiplier, max_startup_budget)
        except ImportError:
            pass  # datareftracker not available, use base budget
        
        # Check if fallback extends the budget
        fallback_extends = getattr(CFG.autoortho, 'fallback_extends_budget', False)
        if isinstance(fallback_extends, str):
            fallback_extends = fallback_extends.lower().strip() in ('true', '1', 'yes', 'on')
        
        # Get fallback_timeout if extended fallbacks are enabled (default 30s)
        fallback_timeout = 0
        if fallback_extends:
            fallback_timeout = getattr(CFG.autoortho, 'fallback_timeout', 30)
            if isinstance(fallback_timeout, str):
                try:
                    fallback_timeout = float(fallback_timeout)
                except ValueError:
                    fallback_timeout = 30
        
        # Calculate total: budget + extended fallback + 15s buffer
        # The 15s buffer accounts for DDS read/write, processing overhead, and safety margin
        build_timeout = int(tile_budget + fallback_timeout + 15)
        
        return build_timeout

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
        """Calculate the actual DDS file size based on tile parameters and current configuration.
        
        IMPORTANT: In dynamic zoom mode, the actual tile zoom can vary based on altitude prediction.
        To avoid truncated texture issues, we calculate size for the MAXIMUM possible zoom level
        that a tile could use, which is zoom + 1 (the X-Plane limit for tile imagery).
        This ensures FUSE always reports a size >= the actual DDS size.
        """
        try:
            # Convert parameters to the format expected by the tile system
            zoom = int(zoom)
            
            # Check if dynamic zoom mode is enabled
            max_zoom_mode = str(CFG.autoortho.max_zoom_mode).lower()
            
            if max_zoom_mode == "dynamic":
                # In dynamic zoom mode, the actual zoom can be up to zoom + 1
                # We MUST use the maximum possible size to avoid X-Plane seeing truncated textures
                # (the DDS header declares the full size, so the file must be at least that big)
                max_zoom = zoom + 1
            else:
                # Fixed mode - use the configured target zoom levels
                if CFG.autoortho.using_custom_tiles:
                    uncapped_target_zoom = self.tc.target_zoom_level
                else:
                    uncapped_target_zoom = self.tc.target_zoom_level_near_airports if zoom == 18 else self.tc.target_zoom_level
                max_zoom = min(zoom + 1, uncapped_target_zoom)
            
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


    def getattr(self, path, fh=None):
        log.debug(f"GETATTR {path}")
        
        # Check for DSF time exclusion redirect
        # Instead of hiding DSF files (which breaks X-Plane's indexing),
        # we redirect to X-Plane's global scenery DSF files
        if self.dsf_re.match(path):
            redirect_path = time_exclusion_manager.get_redirect_path(path)
            if redirect_path:
                log.debug(f"GETATTR: Redirecting DSF to global scenery: {path} -> {redirect_path}")
                return self._getattr_redirected_dsf(redirect_path)
        
        # Generate fresh timestamps for each call
        now = int(time.time())
        
        m = self.dds_re.match(path)
        if m:
            # DDS files are virtual - use cached size calculation but fresh timestamps
            return self._getattr_dds(path, m, now)
        elif path.endswith(".poison"):
            return self._getattr_poison(now)
        elif path.endswith("AOISWORKING"):
            return self._getattr_marker(now)
        else:
            # Real filesystem files - never cache, always get fresh stats
            return self._getattr_real_file(path)

    def _get_dds_size_cached(self, row, col, maptype, zoom):
        """Get DDS size from cache or compute it.
        
        Uses _size_cache OrderedDict with LRU eviction to prevent unbounded
        memory growth. Cache can be updated with actual tile sizes when tiles
        are opened (see open() method).
        """
        key = (row, col, maptype, zoom)
        dds_size = self._size_cache.get(key)
        if dds_size is not None:
            # Move to end for LRU
            self._size_cache.move_to_end(key)
            return dds_size
        
        dds_size = self._calculate_dds_size(str(zoom))
        # Evict oldest entries if at limit
        while len(self._size_cache) >= self._size_cache_max:
            self._size_cache.popitem(last=False)
        self._size_cache[key] = dds_size
        return dds_size
    
    def _set_dds_size_cached(self, row, col, maptype, zoom, size):
        """Set actual DDS size in cache with LRU eviction."""
        key = (row, col, maptype, zoom)
        # Evict oldest entries if at limit
        while len(self._size_cache) >= self._size_cache_max:
            self._size_cache.popitem(last=False)
        self._size_cache[key] = size
        self._size_cache.move_to_end(key)

    def _getattr_dds(self, path, match, now):
        """Get attributes for virtual DDS files."""
        self._ensure_flighttrack_started(reason_path=path)
        row, col, maptype, zoom = match.groups()
        dds_size = self._get_dds_size_cached(int(row), int(col), maptype, int(zoom))
        log.debug("GETATTR: Fetch for path: %s", path)
        
        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': dds_size,
        }
        log.debug(f"GETATTR: ATTRS: {attrs}")
        return attrs

    def _getattr_poison(self, now):
        """Handle poison pill for shutdown."""
        log.info("Poison pill.  Exiting!")
        fuse_ptr = ctypes.c_void_p(_libfuse.fuse_get_context().contents.fuse)
        do_fuse_exit(fuse_ptr=fuse_ptr)
        
        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': 0,
        }
        return attrs

    def _getattr_marker(self, now):
        """Get attributes for AOISWORKING marker file."""
        attrs = {
            'st_atime': now, 
            'st_ctime': now, 
            'st_mtime': now,
            'st_mode': 33206,
            'st_blksize': 32768,
            'st_nlink': 1,
            'st_uid': self.default_uid,
            'st_gid': self.default_gid,
            'st_size': 0,
        }
        return attrs

    def _getattr_real_file(self, path):
        """Get attributes for real filesystem files - never cached."""
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
    
    def _getattr_redirected_dsf(self, redirect_path):
        """Get attributes for a DSF file redirected to global scenery.
        
        When time exclusion is active, DSF reads are redirected to X-Plane's
        global scenery instead of serving empty/hidden files. This ensures
        X-Plane always has terrain data.
        
        Args:
            redirect_path: Absolute path to the global scenery DSF file
            
        Returns:
            dict: File attributes from the redirected DSF file
        """
        try:
            st = os.lstat(redirect_path)
            log.debug(f"GETATTR: Redirected DSF stat: {st}")
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
            log.debug(f"GETATTR: Redirected DSF ATTRS: {attrs}")
            return attrs
        except OSError as e:
            log.warning(f"GETATTR: Failed to stat redirected DSF {redirect_path}: {e}")
            raise FuseOSError(e.errno)

    def readdir(self, path, fh):
        """List directory contents.
        
        DSF files are ALWAYS shown regardless of time exclusion state.
        X-Plane indexes DSF files at flight load time - hiding them causes
        missing terrain. Instead, we redirect reads to global scenery DSF files.
        """
        if path in ["/textures", "/terrain"]:
            return ['.', '..', 'AOISWORKING']

        full_path = self._full_path(path)
        if os.path.isdir(full_path):
            entries = ['.', '..']
            for entry in os.listdir(full_path):
                # DSF files are always included - never filter them out
                # Time exclusion is handled by redirecting reads, not hiding files
                entries.append(entry)
            return entries
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

    @lru_cache(maxsize=128)
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

        # Handle DSF files with time exclusion redirect
        if self.dsf_re.match(path):
            # Check if DSF should be redirected to global scenery
            redirect_path = time_exclusion_manager.get_redirect_path(path)
            if redirect_path:
                log.info(f"OPEN: DSF [{path}] opened in GLOBAL SCENERY mode (time exclusion active) -> {redirect_path}")
                try:
                    if system_type == 'windows':
                        fh = os.open(redirect_path, flags | os.O_BINARY)
                    else:
                        fh = os.open(redirect_path, flags)
                    # Track this as a redirected DSF for release()
                    self._redirected_dsf_fhs[fh] = redirect_path
                    # Register this DSF as being in use (safe transition)
                    time_exclusion_manager.register_dsf_open(path)
                    return fh
                except OSError as e:
                    log.warning(f"OPEN: Failed to open redirected DSF {redirect_path}: {e}, falling back to ORTHO mode")
                    # Fall through to open the original file
            
            # Normal mode - serve AutoOrtho ortho scenery
            log.info(f"OPEN: DSF [{path}] opened in ORTHO mode (AutoOrtho scenery)")
            # Register this DSF as being in use (prevents redirect during active use)
            time_exclusion_manager.register_dsf_open(path)
        
        dds_match = self.dds_re.match(path)
        if dds_match:
            row, col, maptype, zoom = dds_match.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            
            # Register non-BI maptypes for terrain lookup (custom Ortho4XP tiles)
            # This allows the prefetcher to also check for custom tile maptypes
            if maptype != "BI":
                getortho.register_discovered_maptype(maptype)
            
            t = self.tc._open_tile(row, col, maptype, zoom)
            try:
                self._set_dds_size_cached(row, col, maptype, zoom, t.dds.total_size)
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
            
            # Calculate build_timeout dynamically based on tile_time_budget
            # This ensures the FUSE lock timeout is always >= the tile build time
            # Formula: tile_time_budget + fallback_timeout (if enabled) + 15s buffer
            #
            # The buffer accounts for:
            # - DDS read/write operations after tile is built
            # - Any processing overhead
            # - Margin of safety to prevent premature lock timeout
            build_timeout = self._calculate_build_timeout()
            
            if not lock.acquire(timeout=build_timeout):
                # CRITICAL FIX: Instead of raising EIO (which causes CTD on Windows
                # due to EXCEPTION_IN_PAGE_ERROR), return fallback placeholder data.
                # X-Plane will show a gray/missing texture, but won't crash.
                log.error(f"Tile build lock timeout for {key} after {build_timeout}s - returning fallback data")
                
                # ═══════════════════════════════════════════════════════════════
                # ENHANCED DIAGNOSTICS: Log tile state to help debug lock stalls
                # ═══════════════════════════════════════════════════════════════
                # When a lock timeout occurs, it's often difficult to determine
                # what caused the stall. This diagnostic block attempts to gather
                # tile state information (without acquiring locks) to help identify:
                # - Whether the tile exists and has valid DDS
                # - How many chunks have been processed
                # - Whether the tile's ready event is set
                # - Reference count (is something else holding it?)
                try:
                    # Attempt to get tile info WITHOUT acquiring locks (best-effort)
                    # Use _get_tile which may briefly acquire tc_lock, but won't
                    # block on the tile's internal lock
                    t = self.tc.tiles.get(self.tc._to_tile_id(row, col, maptype, zoom))
                    if t:
                        # Gather diagnostic info (all attribute reads are safe)
                        diag_refs = getattr(t, 'refs', 'N/A')
                        diag_ready = t.ready.is_set() if hasattr(t, 'ready') else 'N/A'
                        diag_dds = t.dds is not None if hasattr(t, 'dds') else 'N/A'
                        diag_chunks = len(t.chunks) if hasattr(t, 'chunks') else 'N/A'
                        diag_budget = None
                        if hasattr(t, '_tile_time_budget') and t._tile_time_budget:
                            budget = t._tile_time_budget
                            diag_budget = f"elapsed={budget.elapsed:.1f}s, exhausted={budget.exhausted}"
                        
                        log.error(f"  DIAGNOSTIC: tile={t}, refs={diag_refs}, ready={diag_ready}, "
                                 f"has_dds={diag_dds}, chunk_zooms={diag_chunks}, budget={diag_budget}")
                    else:
                        log.error(f"  DIAGNOSTIC: Tile not found in cache (may have been evicted)")
                except Exception as diag_err:
                    log.error(f"  DIAGNOSTIC: Failed to gather tile state: {diag_err}")
                # ═══════════════════════════════════════════════════════════════
                
                return _generate_fallback_dds_bytes(offset, length)
            
            try:
                t = self.tc._get_tile(row, col, maptype, zoom)
                data = t.read_dds_bytes(offset, length)
                if data is None:
                    # CRITICAL FIX: Return fallback data instead of EIO
                    log.error(f"Tile read returned None for {key} - returning fallback data")
                    return _generate_fallback_dds_bytes(offset, length)
                return data
            except FuseOSError:
                # CRITICAL FIX: Catch EIO and return fallback instead
                # This prevents Windows EXCEPTION_IN_PAGE_ERROR CTD
                log.error(f"FUSE error for tile {key} - returning fallback data to prevent CTD")
                return _generate_fallback_dds_bytes(offset, length)
            except Exception as e:
                # CRITICAL FIX: Return fallback data instead of EIO
                # This prevents Windows EXCEPTION_IN_PAGE_ERROR CTD
                log.error(f"Tile read/build failed for {key} - returning fallback data to prevent CTD")
                log.exception("cause:", exc_info=e)
                return _generate_fallback_dds_bytes(offset, length)
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
        
        # Unregister DSF close for time exclusion tracking
        if self.dsf_re.match(path):
            log.debug(f"RELEASE: DSF closed: {path}")
            time_exclusion_manager.register_dsf_close(path)
            
            # Clean up redirected DSF tracking
            if fh in self._redirected_dsf_fhs:
                log.debug(f"RELEASE: Cleaning up redirected DSF handle: {fh}")
                del self._redirected_dsf_fhs[fh]
        
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
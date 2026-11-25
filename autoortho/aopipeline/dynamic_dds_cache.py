"""
dynamic_dds_cache.py - Persistent DDS cache for AutoOrtho

Stores pre-built DDS textures on disk across sessions so that subsequent
loads skip the expensive JPEG-decode + DXT-compress pipeline entirely.

The DDS cache stores fully-built textures derived from JPEG tiles.

Key features:
- Persistent across sessions with configurable disk-budget enforcement
- Staleness detection via DDM metadata sidecar files
- ZL upgrade support with mipmap shifting (reuses existing mipmaps)
- LRU eviction when disk budget is exceeded
- Atomic file writes (temp + os.replace) for crash safety
- Thread-safe with minimal lock contention
"""

import json
import functools
import hashlib
import logging
import os
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Dict, List, NamedTuple, Optional, Tuple

try:
    from autoortho.dds_manifest import (
        MipmapSource,
        RowBuildManifest,
        manifests_from_dict,
        manifests_to_dict,
    )
except ImportError:
    from dds_manifest import (
        MipmapSource,
        RowBuildManifest,
        manifests_from_dict,
        manifests_to_dict,
    )

try:
    from autoortho.diagnostics import profiled_stage
except ImportError:
    from diagnostics import profiled_stage

try:
    from autoortho import chunk_store
except ImportError:
    import chunk_store

try:
    import zstandard
    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

log = logging.getLogger(__name__)

DDS_HEADER_BYTES = 128


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    _k32.SetFilePointerEx.restype = wintypes.BOOL
    _k32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    _k32.SetEndOfFile.restype = wintypes.BOOL
    _FSCTL_SET_SPARSE = 0x000900C4
    _FILE_BEGIN = 0


def _extend_sparse(f, size: int) -> None:
    """Grow an open file to ``size`` without writing the gap.

    ``truncate()`` is sparse on ext4 and APFS but not on Windows: the C
    runtime extends a file by writing zeros, so every 8192 px skeleton wrote
    about 45 MB and took tens of milliseconds. On Windows the file is marked
    sparse first (where the volume supports it) and its end moved with
    ``SetEndOfFile``, so unwritten rows cost no disk space and later writes
    deep in the file do not zero-fill everything before them.
    """
    if os.name != "nt":
        f.truncate(size)
        return
    f.flush()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno()))
    returned = wintypes.DWORD()
    # FAT32 and exFAT have no sparse files; the end still moves without
    # writing, they just zero-fill when a later write lands past it.
    _k32.DeviceIoControl(handle, _FSCTL_SET_SPARSE, None, 0, None, 0,
                         ctypes.byref(returned), None)
    position = ctypes.c_longlong()
    if (not _k32.SetFilePointerEx(handle, size, ctypes.byref(position), _FILE_BEGIN)
            or not _k32.SetEndOfFile(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


class _SizeShim:
    """The two tile fields ``_is_stale`` reads, for checks without a Tile."""

    class _DDS:
        def __init__(self, total_size):
            self.total_size = total_size

    def __init__(self, max_zoom, total_size):
        self.max_zoom = max_zoom
        self.dds = self._DDS(total_size)


class _DirectInfo(NamedTuple):
    """The parts of an entry's .ddm a direct open needs (see open_direct)."""

    sig: tuple  # (.ddm size, .ddm mtime_ns, .dds size, .dds mtime_ns)
    max_zl: object
    fmt: object
    comp: object
    disk_compression: object
    width: int
    height: int
    full: bool  # every level populated, mipmap 0 servable
    finest_run: Optional[int]
    needs_healing: bool
    dds_size: int
    dds_mtime_ns: int
    no_imagery_chunks: int  # lower-zoom chunks where the provider has none


def mm0_row_layout(width: int, height: int, fmt) -> Tuple[int, int, int]:
    """(start, row_bytes, rows) of mipmap 0's 256-pixel rows in a DDS file."""
    block = 16 if str(fmt).upper() in ("BC3", "DXT5") else 8
    length = max(1, (width * height) >> 4) * block
    rows = max(1, (height + 255) // 256)
    return DDS_HEADER_BYTES, length // rows, rows


def _rows_match(raw: bytes, manifests, width: int, height: int, fmt) -> bool:
    """Every mipmap-0 row of ``raw`` matches its row manifest."""
    start, row_bytes, rows = mm0_row_layout(width, height, fmt)
    if set(manifests) != set(range(rows)):
        return False
    for row in range(rows):
        begin = start + row * row_bytes
        if not manifests[row].validate_data(raw[begin:begin + row_bytes]):
            return False
    return True


def _writes_allowed_default() -> bool:
    return True


# Replaced by getortho with the disk brake's check: new DDS entries are
# skipped while the cache drive is nearly full.
writes_allowed = _writes_allowed_default

# Current DDM schema version. Bump when the metadata format changes
# in a backwards-incompatible way.
# v2 -> v3: added "populated_mipmaps" for incremental DDS persistence
# v3 -> v4: adds crash-safe, row-addressable partial mipmap-0 persistence.
# v4 -> v5: makes immutable, checksummed mipmap-zero row manifests mandatory.
DDM_VERSION = 5
_SUPPORTED_DDM_VERSIONS = frozenset((DDM_VERSION,))

# A row record is deliberately endian-explicit and contains no native-sized
# values, so caches can be moved between Windows and POSIX installations.
_ROW_RECORD_MAGIC = b"DDRR"
_ROW_RECORD_SCHEMA = 1
_ROW_RECORD_HEADER = struct.Struct(">4sBIQI32s")
_MAX_ROW_RECORD_SIZE = 128 * 1024 * 1024


# Predicate on a chunk JPEG file name: True for files kept for good (the
# low-zoom base, imagery of tiles downloaded on the map). Set by getortho.
protect_jpeg = None
# Predicate on a DDS path: True for built textures of tiles whose imagery
# is kept (kept_imagery.dds_protector). LRU eviction skips them.
protect_dds = None


def cleanup_source_jpegs(cache_dir: str, col: int, row: int,
                         tilename_zoom: int, max_zoom: int, min_zoom: int,
                         width: int, height: int, maptype: str,
                         budget_manager=None) -> int:
    """Delete source JPEG chunks after a complete DDS is stored.

    Enumerates chunk files at every zoom level used by the tile's mipmaps
    (max_zoom down to min_zoom) and deletes them.  Coordinate scaling
    mirrors ``_get_quick_zoom()``.

    Returns the number of files successfully deleted.
    """
    deleted = 0
    bytes_freed = 0
    store = chunk_store.store_for(cache_dir)
    legacy_files = store is None or store.legacy_pending()
    for zoom in range(max_zoom, min_zoom - 1, -1):
        zoom_diff = tilename_zoom - zoom
        if zoom_diff >= 0:
            scaled_col = col >> zoom_diff
            scaled_row = row >> zoom_diff
            scaled_width = max(1, width >> zoom_diff)
            scaled_height = max(1, height >> zoom_diff)
        else:
            shift = -zoom_diff
            scaled_col = col << shift
            scaled_row = row << shift
            scaled_width = width << shift
            scaled_height = height << shift

        if store is not None:
            keys = [
                (c, r)
                for r in range(scaled_row, scaled_row + scaled_height)
                for c in range(scaled_col, scaled_col + scaled_width)
                if protect_jpeg is None
                or not protect_jpeg(f"{c}_{r}_{zoom}_{maptype}.jpg")
            ]
            if keys:
                count, freed = store.delete_many(
                    maptype, zoom, keys, return_bytes=True
                )
                deleted += count
                bytes_freed += freed
            if not legacy_files:
                continue
        for r in range(scaled_row, scaled_row + scaled_height):
            for c in range(scaled_col, scaled_col + scaled_width):
                jpeg_name = f"{c}_{r}_{zoom}_{maptype}.jpg"
                if protect_jpeg is not None and protect_jpeg(jpeg_name):
                    continue  # the low-zoom base is kept for good
                jpeg_path = os.path.join(cache_dir, jpeg_name)
                for attempt in range(3):
                    try:
                        file_size = os.path.getsize(jpeg_path)
                        os.remove(jpeg_path)
                        deleted += 1
                        bytes_freed += file_size
                        break
                    except FileNotFoundError:
                        break
                    except PermissionError:
                        if attempt < 2:
                            time.sleep(0.01)
                    except OSError:
                        break

    if deleted > 0:
        log.debug(f"Cleaned up {deleted} source JPEGs for "
                  f"{col}_{row}_{maptype} z{max_zoom}")
        if budget_manager is not None and bytes_freed > 0:
            try:
                budget_manager.account_jpeg(-bytes_freed)
            except Exception:
                pass
    return deleted


class DynamicDDSCache:
    """
    Persistent disk cache for pre-built DDS textures.
    
    Sits between the FUSE layer and the build pipeline as a compiled-output
    cache. On a warm start, tiles are served from disk (~1-2ms) instead of
    being rebuilt from JPEGs (~390ms per tile).
    
    Thread Safety:
        A single ``threading.Lock`` protects the LRU metadata dict. File I/O
        is performed outside the lock when possible. Atomic writes (temp file
        + ``os.replace``) prevent corruption from concurrent access or crashes.
    """

    def __init__(
        self,
        cache_dir: str,
        max_size_mb: int = 4096,
        enabled: bool = True,
        *,
        cleanup_source_jpegs_after_store: bool = False,
        maintenance_workers: int = 2,
        partial_row_queue_bytes: int = 16 * 1024 * 1024,
        partial_row_shutdown_timeout: float = 2.0,
    ):
        """
        Args:
            cache_dir: Base cache directory (same as CFG.paths.cache_dir).
            max_size_mb: Maximum combined disk usage in MB for DDS data,
                         DDM metadata, and partial-row sidecars.
                         Set to 0 for unlimited cache size.
            enabled: Master enable flag. When False, all methods are no-ops.
        """
        self._cache_dir = cache_dir
        self._dds_root = os.path.join(cache_dir, "dds_cache")
        self._max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0  # 0 = unlimited
        self._enabled = enabled
        self._current_size = 0
        self._cleanup_source_jpegs_after_store = bool(
            cleanup_source_jpegs_after_store
        )
        # Persist mipmap 0 only when every row is real target imagery
        # (set from [streaming] real_from_index by getortho).
        self.real_only = False
        self._maintenance = ThreadPoolExecutor(
            max_workers=max(1, min(8, int(maintenance_workers))),
            thread_name_prefix="dds-cache",
        )
        self._maintenance_lock = threading.Lock()
        self._closed = False
        self._scan_complete = threading.Event()
        self._partial_row_queue_limit = max(0, int(partial_row_queue_bytes))
        self._partial_row_shutdown_timeout = max(0.0, partial_row_shutdown_timeout)
        self._partial_row_queue = {}
        self._partial_row_queue_bytes = 0
        self._partial_row_condition = threading.Condition()
        self._partial_row_stop = False
        self._partial_row_active = False
        self._partial_row_worker = None
        self._partial_row_coalesced = 0
        self._partial_row_dropped = 0
        self._partial_row_persisted = 0
        # Rows the cache did not take. Most are ordinary (the tile was
        # completed or superseded meanwhile); worker exceptions are not.
        self._partial_row_not_persisted = 0
        self._partial_row_worker_exceptions = 0
        self._tile_locks = {}
        self._tile_locks_guard = threading.Lock()

        # LRU tracking: tile_key -> (dds_path, ddm_path, size, last_access)
        # Ordered from oldest to newest access.
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

        # In-flight healing guard (prevents duplicate patch work)
        self._healing_in_progress: set = set()

        # Optional DiskBudgetManager reference, set by getortho after startup.
        self._budget_manager = None

        # Network healing callback: set by getortho.py to dispatch network
        # downloads for chunks that can't be healed from disk cache alone.
        # Signature: callback(tile, indices_to_heal: List[int]) -> None
        self._network_heal_callback = None

        # Guard to prevent duplicate network healing dispatches.
        # Protected by self._lock for thread safety.
        self._network_healing_in_progress: set = set()

        # Statistics
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._evictions = 0
        self._upgrades = 0

        # Disk compression settings (read once from config)
        self._compression, self._compression_level = self._get_compression_settings()
        if self._compression == "zstd" and not _HAS_ZSTD:
            log.warning("zstandard not installed - DDS cache compression disabled")
            self._compression = "none"
        # Direct reads (see open_direct): finished textures are served from
        # their cache file, which must be stored uncompressed.
        self.serve_from_disk = self._serve_from_disk_setting()
        if self.serve_from_disk:
            self._compression = "none"
        # Entries open for direct reads: key -> number of open handles.
        # Eviction skips them (guarded by self._lock).
        self._pins: Dict[str, int] = {}
        self._promoting: set = set()
        # What a direct open needs from each entry's .ddm, parsed once and
        # kept while the .ddm and the .dds keep their size and time
        # (see _direct_info). Without it every open read and parsed the
        # sidecar: half a millisecond of Python per open.
        self._direct_memo: OrderedDict = OrderedDict()
        self._direct_memo_lock = threading.Lock()

        if self._enabled:
            os.makedirs(self._dds_root, exist_ok=True)
            self._partial_row_worker = threading.Thread(
                target=self._partial_row_worker_main,
                name="dds-partial-rows",
                daemon=True,
            )
            self._partial_row_worker.start()
            log.info(f"DynamicDDSCache initialized: {self._dds_root} "
                     f"(max={max_size_mb}MB, compression={self._compression})")

    @staticmethod
    def _safe_file_size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    @classmethod
    def _entry_disk_size(cls, dds_path: str, ddm_path: str) -> int:
        """Return the complete logical size of one compiled-cache entry."""
        return sum(
            cls._safe_file_size(path)
            for path in (
                dds_path,
                ddm_path,
                cls._row_records_path(ddm_path),
            )
        )

    def _record_store(
        self,
        key,
        dds_path: str,
        ddm_path: str,
        *,
        count_store: bool = True,
    ) -> None:
        size = self._entry_disk_size(dds_path, ddm_path)
        old_size = 0
        with self._lock:
            if key in self._entries:
                old_size = self._entries[key][2]
                self._current_size -= old_size
            self._entries[key] = (dds_path, ddm_path, size, time.time())
            self._entries.move_to_end(key)
            self._current_size += size
            if count_store:
                self._stores += 1
            over_limit = (
                self._max_size > 0 and self._current_size > self._max_size
            )
        if self._budget_manager is not None:
            self._budget_manager.account_dds(size - old_size)
        if over_limit:
            self._evict_lru_async()

    def _remove_entry_files(
        self,
        key,
        dds_path: str,
        ddm_path: str,
    ) -> int:
        """Delete one entry and keep both accounting layers synchronized."""
        with self._lock:
            tracked = self._entries.pop(key, None)
            if tracked is not None:
                self._current_size = max(
                    0,
                    self._current_size - tracked[2],
                )

        before = self._entry_disk_size(dds_path, ddm_path)
        with self._tile_file_lock(ddm_path):
            self._delete_pair(dds_path, ddm_path)
        remaining = self._entry_disk_size(dds_path, ddm_path)
        freed = max(0, before - remaining)

        if remaining:
            with self._lock:
                self._entries[key] = (
                    dds_path,
                    ddm_path,
                    remaining,
                    time.time(),
                )
                self._entries.move_to_end(key, last=False)
                self._current_size += remaining

        if freed and self._budget_manager is not None:
            self._budget_manager.account_dds(-freed)
        return freed

    def wait_for_scan(self, timeout: Optional[float] = None) -> bool:
        return self._scan_complete.wait(timeout)

    def close(self) -> None:
        with self._maintenance_lock:
            if self._closed:
                return
            self._closed = True
        self.flush_partial_rows(self._partial_row_shutdown_timeout)
        with self._partial_row_condition:
            self._partial_row_stop = True
            self._partial_row_condition.notify_all()
        if self._partial_row_worker is not None:
            self._partial_row_worker.join(self._partial_row_shutdown_timeout)
        self._maintenance.shutdown(wait=True, cancel_futures=False)

    def _submit_maintenance(self, fn, *args, **kwargs) -> bool:
        with self._maintenance_lock:
            if self._closed:
                return False
            try:
                self._maintenance.submit(fn, *args, **kwargs)
            except RuntimeError:
                return False
        return True

    def enqueue_store(
        self,
        tile_id: str,
        max_zoom: int,
        dds_bytes: bytes,
        tile,
        mm0_missing_indices=None,
        mm0_fallback_indices=None,
        mm0_manifest=None,
    ) -> bool:
        """Transfer a DDS snapshot to the maintenance pool without blocking."""
        try:
            snapshot = bytes(dds_bytes)
        except (TypeError, ValueError):
            return False
        return self._submit_maintenance(
            self.store,
            tile_id,
            max_zoom,
            snapshot,
            tile,
            mm0_missing_indices,
            mm0_fallback_indices,
            mm0_manifest,
        )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _tile_key(self, tile_id: str, max_zoom: int) -> str:
        """Unique key for LRU tracking."""
        return f"{tile_id}_z{max_zoom}"

    def _paths_for(self, row: int, col: int, maptype: str,
                   tilename_zoom: int, max_zoom: int) -> Tuple[str, str]:
        """Return (dds_path, ddm_path) for a tile.
        
        Uses the same DSF-based directory hierarchy as cache_paths.py.
        """
        try:
            from autoortho.utils.cache_paths import get_dds_cache_path
        except ImportError:
            from utils.cache_paths import get_dds_cache_path

        base = get_dds_cache_path(
            self._cache_dir, row, col, maptype, tilename_zoom, max_zoom
        )
        return base + ".dds", base + ".ddm"

    @staticmethod
    def _row_records_path(ddm_path: str) -> str:
        """Return the append-only row-record path associated with a DDM."""
        return ddm_path + ".rows"

    @contextmanager
    def _tile_file_lock(self, ddm_path: str):
        """Serialize row data and metadata updates across processes."""
        lock_path = ddm_path + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with self._tile_locks_guard:
            thread_lock = self._tile_locks.setdefault(lock_path, threading.Lock())
        with thread_lock:
            with open(lock_path, "a+b") as lock_file:
                try:
                    if os.name == "nt":
                        import msvcrt
                        lock_file.seek(0)
                        if os.path.getsize(lock_path) == 0:
                            lock_file.write(b"\0")
                            lock_file.flush()
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    yield
                finally:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            lock_file.seek(0)
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass

    # ------------------------------------------------------------------
    # DDM metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_ddm(meta: object) -> bool:
        """Validate the DDM portion needed to safely consume a cache entry."""
        if not isinstance(meta, dict):
            return False
        version = meta.get("v")
        if not isinstance(version, int) or version not in _SUPPORTED_DDM_VERSIONS:
            return False

        populated = meta.get("populated_mipmaps", [])
        partial = meta.get("partial_mipmaps", {})
        if not isinstance(populated, list) or not isinstance(partial, dict):
            return False
        if any(not isinstance(mipmap, int) or mipmap < 0 for mipmap in populated):
            return False
        for mipmap, entry in partial.items():
            if (not isinstance(mipmap, str) or not mipmap.isdigit() or
                    not isinstance(entry, dict)):
                return False
            if entry.get("unit") != "chunk_row":
                return False
            total = entry.get("total")
            covered = entry.get("covered")
            if (not isinstance(total, int) or total <= 0 or
                    not isinstance(covered, list) or
                    any(not isinstance(row, int) or row < 0 or row >= total
                        for row in covered) or
                    len(set(covered)) != len(covered) or
                    not isinstance(entry.get("degraded"), list) or
                    any(
                        not isinstance(row, int)
                        or row < 0
                        or row >= total
                        for row in entry.get("degraded", [])
                    ) or
                    not isinstance(entry.get("revision"), int) or
                    entry["revision"] < 0):
                return False
            revisions = entry.get("row_revisions", {})
            if not isinstance(revisions, dict):
                return False
            if set(revisions) != {str(row) for row in covered}:
                return False
            if any(not isinstance(revision, int) or revision < 0
                   for revision in revisions.values()):
                return False
            if int(mipmap) in populated:
                return False
        try:
            tile_id = (
                f"{int(meta['tile_row'])}_{int(meta['tile_col'])}_"
                f"{meta['map']}_{int(meta['zl'])}"
            )
            manifests = manifests_from_dict(
                meta["mm0_manifest"],
                tile_id=tile_id,
            )
        except (KeyError, TypeError, ValueError):
            return False
        target_zoom = meta["mm0_manifest"].get("target_zoom")
        if target_zoom != meta.get("max_zl"):
            return False
        total_rows = max(1, (int(meta.get("h", 0)) + 255) // 256)
        if any(
            row < 0
            or row >= total_rows
            or len(manifest.sources) != max(
                1,
                (int(meta.get("w", 0)) + 255) // 256,
            )
            for row, manifest in manifests.items()
        ):
            return False
        if 0 in populated and set(manifests) != set(range(total_rows)):
            return False
        partial_mm0 = partial.get("0")
        if partial_mm0 is not None and set(manifests) != set(
            partial_mm0.get("covered", [])
        ):
            return False
        return True

    @staticmethod
    def _build_ddm(tile, max_zoom: int,
                   dds_format: str, compressor: str,
                   mm0_manifest,
                   disk_compression: str = "none") -> dict:
        """Build DDM metadata exclusively from immutable row manifests."""
        dds_ref = tile.dds
        width = dds_ref.width if dds_ref else 0
        height = dds_ref.height if dds_ref else 0
        mm_count = dds_ref.mipMapCount if dds_ref else 0
        manifests = dict(mm0_manifest or {})
        expected_rows = max(1, (height + 255) // 256)
        if set(manifests) != set(range(expected_rows)):
            raise ValueError("complete DDS requires every mipmap-zero row manifest")
        chunks_per_row = max(1, (width + 255) // 256)
        if any(
            manifest.tile_id != tile.id
            or manifest.target_zoom != max_zoom
            or manifest.mipmap != 0
            or len(manifest.sources) != chunks_per_row
            for manifest in manifests.values()
        ):
            raise ValueError("mipmap-zero manifest does not match the DDS")
        missing = []
        fallback = []
        for row_index, manifest in manifests.items():
            base = row_index * chunks_per_row
            missing.extend(base + index for index in manifest.missing_indices)
            fallback.extend(base + index for index in manifest.fallback_indices)
        total_chunks = chunks_per_row * expected_rows

        mipmaps = []
        for i in range(mm_count):
            mm_entry = {"zl": max_zoom - i if i < (max_zoom - 11) else 12, "complete": True}
            if i == 0 and total_chunks > 0:
                mm_entry["total"] = total_chunks
                mm_entry["valid"] = total_chunks - len(missing)
                mm_entry["complete"] = len(missing) == 0 and len(fallback) == 0
            mipmaps.append(mm_entry)

        return {
            "v": DDM_VERSION,
            "w": width,
            "h": height,
            "mm": mm_count,
            "zl": tile.tilename_zoom,
            "max_zl": max_zoom,
            "fmt": dds_format,
            "comp": compressor,
            "map": tile.maptype,
            "built": time.time(),
            "tile_row": tile.row,
            "tile_col": tile.col,
            "mipmaps": mipmaps,
            "populated_mipmaps": list(range(mm_count)),
            "partial_mipmaps": {},
            "needs_healing": len(missing) > 0 or len(fallback) > 0,
            "healing_chunks": len(missing) + len(fallback),
            "missing_indices": missing,
            "fallback_indices": fallback,
            "disk_compression": disk_compression,
            "mm0_manifest": manifests_to_dict(
                manifests,
                target_zoom=max_zoom,
            ),
        }

    @staticmethod
    def _build_ddm_incremental(row: int, col: int, maptype: str,
                               tilename_zoom: int, max_zoom: int,
                               width: int, height: int, mm_count: int,
                               dds_format: str, compressor: str,
                               populated_mipmaps: List[int],
                               disk_compression: str = "none",
                               partial_mipmaps: Optional[dict] = None,
                               mm0_manifest: Optional[dict] = None) -> dict:
        """Build a lightweight DDM v4 dict for incremental saves.

        Unlike ``_build_ddm``, this does not require the tile DDS object,
        making it safe to call outside the tile lock with captured values.
        """
        mipmaps = []
        for i in range(mm_count):
            mm_entry = {"zl": max_zoom - i if i < (max_zoom - 11) else 12,
                        "complete": i in populated_mipmaps}
            mipmaps.append(mm_entry)

        return {
            "v": DDM_VERSION,
            "w": width,
            "h": height,
            "mm": mm_count,
            "zl": tilename_zoom,
            "max_zl": max_zoom,
            "fmt": dds_format,
            "comp": compressor,
            "map": maptype,
            "built": time.time(),
            "tile_row": row,
            "tile_col": col,
            "mipmaps": mipmaps,
            "populated_mipmaps": sorted(populated_mipmaps),
            "partial_mipmaps": partial_mipmaps or {},
            "needs_healing": False,
            "healing_chunks": 0,
            "missing_indices": [],
            "fallback_indices": [],
            "disk_compression": disk_compression,
            "mm0_manifest": mm0_manifest or {
                "target_zoom": max_zoom,
                "rows": {},
            },
        }

    @staticmethod
    def _mm0_cache_complete(meta: dict) -> bool:
        """Return True only when cached mipmap 0 is safe to serve live."""
        if not meta:
            return False

        populated = meta.get("populated_mipmaps")
        if populated is not None and 0 not in populated:
            return False

        if meta.get("needs_healing"):
            return False
        if meta.get("missing_indices") or meta.get("fallback_indices"):
            return False

        try:
            tile_id = (
                f"{int(meta['tile_row'])}_{int(meta['tile_col'])}_"
                f"{meta['map']}_{int(meta['zl'])}"
            )
            manifests = manifests_from_dict(
                meta["mm0_manifest"],
                tile_id=tile_id,
            )
        except (KeyError, TypeError, ValueError):
            return False
        if not manifests or not all(
            manifest.is_exact for manifest in manifests.values()
        ):
            return False

        mipmaps = meta.get("mipmaps") or []
        if mipmaps:
            mm0 = mipmaps[0]
            if mm0.get("complete") is False:
                return False
            total = mm0.get("total", 0)
            valid = mm0.get("valid", total)
            if total > 0 and valid < total:
                return False

        return True

    @staticmethod
    def _validate_mm0_manifest_bytes(
        tile,
        max_zoom: int,
        dds_bytes: bytes,
        manifests,
    ) -> bool:
        try:
            manifest_map = dict(manifests or {})
            mm0 = tile.dds.mipmap_list[0]
            row_count = max(1, (tile.dds.height + 255) // 256)
            row_bytes = mm0.length // row_count
            if set(manifest_map) != set(range(row_count)):
                return False
            for row, manifest in manifest_map.items():
                if (
                    manifest.tile_id != tile.id
                    or manifest.target_zoom != max_zoom
                    or manifest.mipmap != 0
                    or manifest.row_index != row
                ):
                    return False
                start = mm0.startpos + row * row_bytes
                if not manifest.validate_data(
                    dds_bytes[start:start + row_bytes]
                ):
                    return False
            return True
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _write_ddm(ddm_path: str, meta: dict) -> None:
        """Write DDM metadata atomically."""
        tmp = ddm_path + f".tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ddm_path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _read_ddm(ddm_path: str) -> Optional[dict]:
        """Read and parse DDM metadata. Returns None on any error."""
        try:
            with open(ddm_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return meta if DynamicDDSCache._validate_ddm(meta) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Staleness detection
    # ------------------------------------------------------------------

    def _is_stale(self, meta: dict, tile, dds_path: str) -> bool:
        """Check if a cached DDS entry is stale and needs rebuilding.
        
        NOTE: Does NOT check ZL mismatch. ZL upgrades are handled separately
        in load() via find_upgrade_candidate() to enable mipmap shifting.
        
        Staleness rules:
        1. fmt != current DXT format -> config changed
        2. comp != current compressor -> config changed
        3. File size mismatch vs expected -> corruption
        """
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG  # type: ignore[no-redef]

        # Rule 1: DXT format changed
        current_fmt = CFG.pydds.format.upper()
        if current_fmt in ("DXT1",):
            current_fmt = "BC1"
        elif current_fmt in ("DXT5",):
            current_fmt = "BC3"
        if meta.get("fmt") != current_fmt:
            log.debug(f"DDS stale: format changed ({meta.get('fmt')} -> {current_fmt})")
            return True

        # Rule 2: Compressor changed
        current_comp = CFG.pydds.compressor.upper()
        if meta.get("comp") != current_comp:
            log.debug(f"DDS stale: compressor changed ({meta.get('comp')} -> {current_comp})")
            return True

        # Rule 3: File size validation (uncompressed files only)
        # Compressed files have variable on-disk sizes; corruption is caught
        # by zstd decompression failure in load() instead.
        if meta.get("disk_compression", "none") == "none":
            if meta.get("max_zl") == tile.max_zoom and tile.dds is not None:
                expected_size = tile.dds.total_size
                try:
                    actual_size = os.path.getsize(dds_path)
                    if actual_size != expected_size:
                        log.debug(f"DDS stale: size mismatch ({actual_size} vs {expected_size})")
                        return True
                except OSError:
                    return True

        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @profiled_stage("cache.dds_load", tile_arg=1)
    def load(self, tile_id: str, max_zoom: int, tile) -> Optional[bytes]:
        """
        Load a cached DDS from disk.
        
        Returns the complete DDS bytes (including 128-byte header) if the
        cache has a valid, non-stale entry. Returns None on miss or staleness.
        
        When a ZL mismatch is detected, this method sets hints on the tile:
        - ``tile._dds_upgrade_available``: cached ZL is one step lower (e.g., ZL16 -> ZL17)
        - ``tile._dds_downgrade_available``: cached ZL is one step higher (e.g., ZL17 -> ZL16)
        
        Args:
            tile_id: Tile identifier (e.g., "21728_34432_BI_12")
            max_zoom: Effective maximum zoom level
            tile: Tile object (used for staleness checks and path computation)
        
        Returns:
            DDS bytes on cache hit, None on miss
        """
        if not self._enabled:
            return None

        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom
            )
            key = self._tile_key(tile_id, max_zoom)

            # Check if DDM exists for the requested ZL
            meta = self._read_ddm(ddm_path)
            if meta is None:
                if os.path.exists(ddm_path):
                    self._remove_entry_files(key, dds_path, ddm_path)
                # No entry at requested ZL. Check for adjacent-ZL candidates.
                self._check_upgrade_candidate(tile_id, max_zoom, tile)
                self._check_downgrade_candidate(tile_id, max_zoom, tile)
                self._misses += 1
                return None

            # Staleness checks (excludes ZL mismatch, handled separately)
            if self._is_stale(meta, tile, dds_path):
                self._remove_entry_files(key, dds_path, ddm_path)
                self._misses += 1
                return None

            # Check ZL match
            cached_zl = meta.get("max_zl")
            if cached_zl != max_zoom:
                if cached_zl is not None and max_zoom - cached_zl == 1:
                    # Single-step upgrade available
                    tile._dds_upgrade_available = (dds_path, meta)
                    log.debug(f"DDS cache: ZL upgrade available {tile_id} "
                              f"z{cached_zl} -> z{max_zoom}")
                elif cached_zl is not None and cached_zl - max_zoom == 1:
                    # Single-step downgrade available
                    tile._dds_downgrade_available = (dds_path, meta)
                    log.debug(f"DDS cache: ZL downgrade available {tile_id} "
                              f"z{cached_zl} -> z{max_zoom}")
                else:
                    self._remove_entry_files(key, dds_path, ddm_path)
                self._misses += 1
                return None

            # Strict quality gate: do not serve prebuilt DDS files that still
            # contain missing-color or lower-ZL fallback chunks.  Returning None
            # lets the live path build a fresh tile instead of showing degraded
            # predictive-cache output for the rest of the flight.
            missing_indices = meta.get("missing_indices", []) or []
            fallback_indices = meta.get("fallback_indices", []) or []
            if not self._mm0_cache_complete(meta):
                tile._dds_needs_healing = True
                tile._dds_missing_indices = missing_indices
                tile._dds_fallback_indices = fallback_indices
                log.debug(f"DDS cache: strict miss for incomplete tile {tile_id} "
                          f"({len(missing_indices)} missing, "
                          f"{len(fallback_indices)} fallback chunks)")

                self._dispatch_healing_for_incomplete(tile_id, max_zoom, tile)
                self._misses += 1
                return None

            # Read the DDS file (possibly compressed on disk)
            try:
                with open(dds_path, "rb") as f:
                    raw_bytes = f.read()
            except (FileNotFoundError, OSError):
                self._remove_entry_files(key, dds_path, ddm_path)
                self._misses += 1
                return None

            # Decompress if the file was stored compressed
            try:
                dds_bytes = self._decompress_dds(raw_bytes, meta)
            except Exception:
                log.debug(f"DDS cache: decompression failed for {tile_id}, removing")
                self._remove_entry_files(key, dds_path, ddm_path)
                self._misses += 1
                return None

            # Validate size (against uncompressed DDS dimensions)
            if tile.dds is not None and len(dds_bytes) != tile.dds.total_size:
                log.debug(f"DDS cache: size mismatch for {tile_id} "
                          f"({len(dds_bytes)} vs {tile.dds.total_size})")
                self._remove_entry_files(key, dds_path, ddm_path)
                self._misses += 1
                return None

            try:
                manifests = manifests_from_dict(
                    meta["mm0_manifest"],
                    tile_id=tile_id,
                )
                mm0 = tile.dds.mipmap_list[0]
                row_count = max(1, (tile.dds.height + 255) // 256)
                row_bytes = mm0.length // row_count
                for row in range(row_count):
                    manifest = manifests[row]
                    start = mm0.startpos + row * row_bytes
                    row_data = dds_bytes[start:start + row_bytes]
                    if not manifest.validate_data(row_data):
                        raise ValueError("cached mipmap-zero row checksum mismatch")
            except (KeyError, TypeError, ValueError):
                log.debug(
                    "DDS cache: invalid mipmap-zero manifest for %s",
                    tile_id,
                )
                self._remove_entry_files(key, dds_path, ddm_path)
                self._misses += 1
                return None

            tile._dds_needs_healing = False
            tile._dds_missing_indices = []
            tile._dds_fallback_indices = []
            tile._loaded_mm0_manifests = manifests

            # DDM v3/v4: partial DDS awareness -- tell the tile which mipmaps
            # actually contain data so _populate_dds_from_prebuilt() can
            # skip unpopulated slots (avoids allocating zero-filled buffers).
            populated = meta.get("populated_mipmaps")
            if populated is not None:
                tile._dds_populated_mipmaps = set(populated)
            else:
                tile._dds_populated_mipmaps = None  # v2 compat: all populated

            # Update LRU tracking
            with self._lock:
                size = self._entry_disk_size(dds_path, ddm_path)
                if key in self._entries:
                    self._entries.move_to_end(key)
                    # Update access time
                    old = self._entries[key]
                    self._entries[key] = (dds_path, ddm_path, old[2], time.time())
                else:
                    # Entry loaded from disk but not yet tracked (startup)
                    self._entries[key] = (dds_path, ddm_path, size, time.time())
                    self._current_size += size
                self._hits += 1

            log.debug(f"DDS cache HIT: {tile_id} z{max_zoom} ({len(dds_bytes)} bytes)")
            return dds_bytes

        except Exception as e:
            log.debug(f"DDS cache load error for {tile_id}: {e}")
            self._misses += 1
            return None

    def metadata_signature(self, tile_id: str, max_zoom: int, tile):
        """(mtime_ns, size) of the tile's DDM file, or None when there is none.

        Lets a reader skip parsing the metadata again while the file has not
        changed: parsing it, with its row manifests, cost about 1.3 ms per
        read of a texture being built.
        """
        if not self._enabled:
            return None
        try:
            _, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype, tile.tilename_zoom, max_zoom)
            st = os.stat(ddm_path)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return (st.st_mtime_ns, st.st_size)

    @profiled_stage("cache.dds_metadata", tile_arg=1)
    def load_metadata(self, tile_id: str, max_zoom: int, tile) -> Optional[dict]:
        """Read DDM metadata without loading the DDS file.

        Returns the parsed DDM dict or None if no entry exists.
        Useful for checking ``needs_healing`` without the ~11 MB file read.
        """
        if not self._enabled:
            return None
        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom)
            meta = self._read_ddm(ddm_path)
            if meta is None and os.path.exists(ddm_path):
                self._remove_entry_files(
                    self._tile_key(tile_id, max_zoom),
                    dds_path,
                    ddm_path,
                )
            return meta
        except Exception:
            return None

    @staticmethod
    def _read_row_records(records_path: str) -> dict:
        """Read valid records, retaining only the newest revision for each row."""
        rows = {}
        try:
            with open(records_path, "rb") as records:
                while True:
                    header = records.read(_ROW_RECORD_HEADER.size)
                    if not header:
                        break
                    if len(header) != _ROW_RECORD_HEADER.size:
                        break
                    magic, schema, row, revision, length, checksum = (
                        _ROW_RECORD_HEADER.unpack(header))
                    if (magic != _ROW_RECORD_MAGIC or schema != _ROW_RECORD_SCHEMA or
                            length > _MAX_ROW_RECORD_SIZE):
                        break
                    data = records.read(length)
                    if len(data) != length:
                        break
                    if hashlib.sha256(data).digest() != checksum:
                        continue
                    previous = rows.get(row)
                    if previous is None or revision >= previous[0]:
                        rows[row] = (revision, data)
        except (FileNotFoundError, OSError):
            pass
        return rows

    @staticmethod
    def _append_row_record(records_path: str, row: int, revision: int,
                           data: bytes) -> None:
        checksum = hashlib.sha256(data).digest()
        header = _ROW_RECORD_HEADER.pack(
            _ROW_RECORD_MAGIC, _ROW_RECORD_SCHEMA, row, revision,
            len(data), checksum)
        with open(records_path, "ab") as records:
            records.write(header)
            records.write(data)
            # Data must reach disk before the DDM starts referencing it.
            records.flush()
            os.fsync(records.fileno())

    def _new_partial_ddm(self, tile, max_zoom: int) -> dict:
        dds_format, compressor = self._get_format_and_compressor()
        dds_ref = getattr(tile, "dds", None)
        return self._build_ddm_incremental(
            tile.row, tile.col, tile.maptype, tile.tilename_zoom, max_zoom,
            getattr(dds_ref, "width", 0), getattr(dds_ref, "height", 0),
            getattr(dds_ref, "mipMapCount", 0), dds_format, compressor, [])

    def append_partial_row(self, tile_id: str, max_zoom: int, tile,
                           row_index: int, data: bytes, total: int,
                           *, manifest: Optional[RowBuildManifest] = None,
                           degraded: bool = False,
                           revision: Optional[int] = None,
                           provenance: Optional[bytes] = None) -> bool:
        """Append one exact mipmap-zero row and atomically expose its manifest.

        ``total`` is the number of chunk rows in mipmap 0.  A row becomes
        visible only after its record has been flushed; mipmap 0 is added to
        ``populated_mipmaps`` only once all rows are covered.
        """
        if (not self._enabled or not isinstance(row_index, int) or
                not isinstance(total, int) or total <= 0 or
                row_index < 0 or row_index >= total):
            return False
        try:
            row_data = bytes(data)
        except (TypeError, ValueError):
            return False
        if (
            len(row_data) > _MAX_ROW_RECORD_SIZE
            or manifest is None
            or not manifest.is_exact
            or manifest.tile_id != tile_id
            or manifest.target_zoom != max_zoom
            or manifest.mipmap != 0
            or manifest.row_index != row_index
            or not manifest.validate_data(row_data)
        ):
            return False

        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype, tile.tilename_zoom, max_zoom)
            records_path = self._row_records_path(ddm_path)
            os.makedirs(os.path.dirname(ddm_path), exist_ok=True)
            with self._tile_file_lock(ddm_path):
                meta = self._read_ddm(ddm_path)
                if meta is None:
                    meta = self._new_partial_ddm(tile, max_zoom)
                elif meta.get("v") != DDM_VERSION or 0 in meta.get(
                        "populated_mipmaps", []):
                    return False

                partial = meta.setdefault("partial_mipmaps", {})
                entry = partial.get("0")
                if entry is None:
                    entry = {
                        "unit": "chunk_row",
                        "total": total,
                        "covered": [],
                        "degraded": [],
                        "revision": 0,
                        "row_revisions": {},
                    }
                    partial["0"] = entry
                if entry.get("total") != total:
                    return False

                current_revision = max(
                    [entry.get("revision", 0)] +
                    list(entry.get("row_revisions", {}).values()))
                row_revision = manifest.build_generation
                if not isinstance(row_revision, int) or row_revision < 0:
                    return False
                if row_revision < entry["row_revisions"].get(str(row_index), -1):
                    return True

                self._append_row_record(
                    records_path, row_index, row_revision, row_data)
                covered = set(entry["covered"])
                covered.add(row_index)
                entry["covered"] = sorted(covered)
                entry["degraded"] = []
                entry["revision"] = max(entry["revision"], row_revision)
                entry["row_revisions"][str(row_index)] = row_revision
                mm0_manifest = meta.setdefault(
                    "mm0_manifest",
                    {"target_zoom": max_zoom, "rows": {}},
                )
                if mm0_manifest.get("target_zoom") != max_zoom:
                    return False
                mm0_manifest.setdefault("rows", {})[
                    str(row_index)
                ] = manifest.to_dict()

                # Row records are not a normal DDS file. Keep mipmap 0 out of
                # populated_mipmaps until the live tile promotes and stores a
                # complete DDS, so older readers can never advertise partial
                # row storage as a complete texture.
                meta["populated_mipmaps"] = sorted(
                    mipmap for mipmap in meta.get("populated_mipmaps", [])
                    if mipmap != 0)
                if meta.get("mipmaps"):
                    meta["mipmaps"][0]["complete"] = False
                self._write_ddm(ddm_path, meta)
            key = self._tile_key(tile_id, max_zoom)
            self._record_store(
                key,
                dds_path,
                ddm_path,
                count_store=False,
            )
            return True
        except (OSError, ValueError, struct.error) as exc:
            log.debug("DDS partial-row append failed for %s: %s", tile_id, exc)
            return False

    def load_partial_rows(self, tile_id: str, max_zoom: int, tile) -> dict:
        """Return DDM-referenced mipmap-0 partial rows keyed by chunk-row."""
        if not self._enabled:
            return {}
        try:
            _, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype, tile.tilename_zoom, max_zoom)
            meta = self._read_ddm(ddm_path)
            if meta is None or meta.get("v") != DDM_VERSION:
                return {}
            entry = meta.get("partial_mipmaps", {}).get("0")
            if entry is None:
                return {}
            records = self._read_row_records(self._row_records_path(ddm_path))
            referenced = entry["row_revisions"]
            manifests = manifests_from_dict(
                meta["mm0_manifest"],
                tile_id=tile_id,
            )
            loaded = {}
            for row, record in records.items():
                manifest = manifests.get(row)
                if (
                    str(row) in referenced
                    and referenced[str(row)] == record[0]
                    and manifest is not None
                    and manifest.is_exact
                    and manifest.validate_data(record[1])
                ):
                    loaded[row] = record[1]
            return loaded
        except (OSError, ValueError, TypeError):
            return {}

    def enqueue_partial_row(self, tile_id: str, max_zoom: int, tile,
                            row_index: int, data: bytes, total: int,
                            *, manifest: Optional[RowBuildManifest] = None,
                            degraded: bool = False,
                            revision: Optional[int] = None,
                            provenance: Optional[bytes] = None,
                            completion_callback=None) -> bool:
        """Queue a row for non-blocking, coalesced background persistence."""
        if not self._enabled or self._partial_row_queue_limit <= 0:
            return False
        try:
            row_data = bytes(data)
        except (TypeError, ValueError):
            return False
        if (not isinstance(row_index, int) or not isinstance(total, int) or
                total <= 0 or row_index < 0 or row_index >= total or
                len(row_data) > _MAX_ROW_RECORD_SIZE or manifest is None or
                not manifest.is_exact or
                manifest.tile_id != tile_id or
                manifest.target_zoom != max_zoom or
                manifest.row_index != row_index or
                not manifest.validate_data(row_data)):
            return False
        key = (tile_id, max_zoom, tile.row, tile.col, tile.maptype,
               tile.tilename_zoom)
        with self._partial_row_condition:
            if self._partial_row_stop or self._closed:
                return False
            pending = self._partial_row_queue.setdefault(
                key, {"tile": tile, "rows": {}})
            old = pending["rows"].get(row_index)
            queued_bytes = self._partial_row_queue_bytes - (
                len(old["data"]) if old is not None else 0)
            if queued_bytes + len(row_data) > self._partial_row_queue_limit:
                self._partial_row_dropped += 1
                if not pending["rows"]:
                    self._partial_row_queue.pop(key, None)
                return False
            if old is not None:
                self._partial_row_coalesced += 1
            pending["rows"][row_index] = {
                "data": row_data, "total": total, "manifest": manifest,
                "completion_callback": completion_callback,
            }
            self._partial_row_queue_bytes = queued_bytes + len(row_data)
            self._partial_row_condition.notify()
        return True

    def _partial_row_worker_main(self) -> None:
        while True:
            with self._partial_row_condition:
                while not self._partial_row_queue and not self._partial_row_stop:
                    self._partial_row_condition.wait()
                if self._partial_row_stop and not self._partial_row_queue:
                    return
                _key, pending = self._partial_row_queue.popitem()
                self._partial_row_queue_bytes -= sum(
                    len(item["data"]) for item in pending["rows"].values())
                self._partial_row_active = True
            try:
                for row_index, item in pending["rows"].items():
                    success = self._persist_queued_row(
                        _key, pending["tile"], row_index, item)
                    with self._partial_row_condition:
                        if success:
                            self._partial_row_persisted += 1
                        else:
                            self._partial_row_not_persisted += 1
                    callback = item.get("completion_callback")
                    if callback is not None:
                        try:
                            callback(success, row_index)
                        except Exception:
                            log.exception("Partial-row completion callback failed")
            finally:
                # Left set, flush_partial_rows() and close() would wait on a
                # batch that is no longer running.
                with self._partial_row_condition:
                    self._partial_row_active = False
                    self._partial_row_condition.notify_all()

    def _persist_queued_row(self, key, tile, row_index: int, item: dict) -> bool:
        """Persist one queued row; an unexpected error fails only that row.

        ``append_partial_row()`` handles I/O errors itself. Anything else
        (a .ddm with missing or mistyped fields, say) used to end the worker
        thread: every later row then sat in the queue until it was dropped
        and no completion callback ran again for the rest of the session.
        """
        try:
            return bool(self.append_partial_row(
                key[0], key[1], tile, row_index,
                item["data"], item["total"],
                manifest=item["manifest"]))
        except Exception:
            with self._partial_row_condition:
                self._partial_row_worker_exceptions += 1
                failures = self._partial_row_worker_exceptions
            # The first one and every hundredth: a tile whose metadata is
            # broken fails once per row.
            if failures == 1 or failures % 100 == 0:
                log.exception(
                    "DDS partial-row persistence failed unexpectedly "
                    "(%d so far)", failures)
            return False

    def flush_partial_rows(self, timeout: Optional[float] = None) -> bool:
        """Wait for queued rows to persist, stopping after ``timeout`` seconds."""
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        with self._partial_row_condition:
            while self._partial_row_queue or getattr(self, "_partial_row_active", False):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._partial_row_condition.wait(remaining)
        return True

    # ------------------------------------------------------------------
    # Direct reads: finished textures served from their cache file
    # ------------------------------------------------------------------

    @staticmethod
    def _serve_from_disk_setting() -> bool:
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG  # type: ignore[no-redef]
        value = getattr(getattr(CFG, "pydds", None), "serve_from_disk", True)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    @staticmethod
    def _finest_populated_run(meta: dict, mm_count: int) -> Optional[int]:
        """The finest level from which every level to the last is populated.

        None when the entry does not say (a v2 entry) or holds no level.
        """
        populated = meta.get("populated_mipmaps")
        if populated is None or mm_count <= 0:
            return None
        levels = set(int(level) for level in populated)
        finest = None
        for level in range(mm_count - 1, -1, -1):
            if level not in levels:
                break
            finest = level
        return finest

    def open_direct(self, row: int, col: int, maptype: str,
                    tilename_zoom: int, max_zoom: int,
                    expected_size: int,
                    min_level: Optional[int] = None) -> Optional[dict]:
        """The cache entry of a texture that can be served as a plain file.

        Returns ``None`` unless the entry is at ``max_zoom``, not stale,
        uncompressed, exactly ``expected_size`` bytes, and either

        * complete and exact in mipmap 0 and populated at every level
          (``finest_level`` 0), or
        * when ``min_level`` is given (the level the paging model says
          X-Plane wants now), populated from some level ``k <= min_level``
          to the last (``finest_level`` k). A far texture is read at 512 px:
          its mipmap 0 is never downloaded, but the coarse levels
          ``store_incremental`` wrote into the sparse file are enough
          (performance plan, item 4). A read finer than ``finest_level``
          falls back to a live tile.

        A compressed entry that is otherwise servable is rewritten
        uncompressed in the background, so a later open can be direct.

        The returned entry is pinned (skipped by eviction) until
        :meth:`unpin` is called with its ``key``.
        """
        if not (self._enabled and self.serve_from_disk):
            return None
        tile_id = f"{row}_{col}_{maptype}_{tilename_zoom}"
        try:
            dds_path, ddm_path = self._paths_for(
                row, col, maptype, tilename_zoom, max_zoom
            )
            info = self._direct_info(tile_id, dds_path, ddm_path)
            # Why an open is not direct, counted per reason (STATS): what a
            # flight's ready share is made of.
            if info is None:
                self._bump("dds_direct_skip_no_entry")
                return None
            if info.max_zl != max_zoom:
                self._bump("dds_direct_skip_zoom")
                return None
            if self._direct_info_stale(info, expected_size):
                self._bump("dds_direct_skip_stale")
                return None
            full = info.full
            finest_level = 0
            if not full:
                if info.needs_healing:
                    self._bump("dds_direct_skip_healing")
                    return None
                if min_level is None or min_level <= 0:
                    # Partial entry, and the paging model has no level for
                    # it (no camera) or wants mipmap 0.
                    self._bump("dds_direct_skip_partial_no_level")
                    return None
                finest = info.finest_run
                if finest is None or finest > int(min_level):
                    self._bump("dds_direct_skip_partial_coarse")
                    return None
                # Mipmap 0 without its exact row manifest is never served
                # from the file: level 1 is the finest a partial entry gives.
                finest_level = max(1, finest)
                if finest_level > int(min_level):
                    return None
            if info.disk_compression != "none":
                self._schedule_promote(tile_id, max_zoom, dds_path, ddm_path,
                                       expected_size)
                self._bump("dds_direct_compressed_skip")
                return None
            if info.dds_size != expected_size:
                self._bump("dds_direct_skip_size")
                return None
            # Loaded only if a read needs a row checked: rows found good stay
            # good for the life of the file (disk_texture._verified).
            manifests = (
                functools.partial(
                    self._direct_manifests, tile_id, ddm_path, info.sig
                )
                if full else {}
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None
        except Exception as exc:
            log.debug(f"DDS direct open failed for {tile_id}: {exc}")
            return None

        key = self._tile_key(tile_id, max_zoom)
        with self._lock:
            self._pins[key] = self._pins.get(key, 0) + 1
            if key in self._entries:
                old = self._entries[key]
                self._entries[key] = (dds_path, ddm_path, old[2], time.time())
                self._entries.move_to_end(key)
            else:
                size = self._entry_disk_size(dds_path, ddm_path)
                self._entries[key] = (dds_path, ddm_path, size, time.time())
                self._current_size += size
            self._hits += 1
        return {
            "key": key,
            "tile_id": tile_id,
            "max_zoom": max_zoom,
            "dds_path": dds_path,
            "size": expected_size,
            "mtime_ns": info.dds_mtime_ns,
            "width": info.width,
            "height": info.height,
            "fmt": info.fmt,
            "manifests": manifests,
            "finest_level": finest_level,
            "no_imagery_chunks": info.no_imagery_chunks,
        }

    _DIRECT_MEMO_MAX = 8192

    @staticmethod
    def _remember_rows_checked(dds_path: str, manifests, size: int) -> None:
        """Tell direct reads that this file's mipmap-0 rows were checked."""
        try:
            if __package__ and __package__.startswith("autoortho"):
                from autoortho import disk_texture
            else:
                import disk_texture  # type: ignore[no-redef]
            disk_texture.remember_verified(dds_path, list(dict(manifests)), size)
        except Exception as exc:
            log.debug(f"Could not remember checked rows of {dds_path}: {exc}")

    def _direct_info(self, tile_id: str, dds_path: str,
                     ddm_path: str) -> Optional["_DirectInfo"]:
        """What :meth:`open_direct` needs from an entry, or None.

        Parsed from the .ddm once and kept while both files keep their size
        and modification time: any rewrite (a rebuild, healing, a promotion,
        another process storing the texture) changes one of them. Two
        ``stat`` calls instead of reading and parsing the sidecar.
        """
        try:
            ddm_stat = os.stat(ddm_path)
            dds_stat = os.stat(dds_path)
        except OSError:
            self._forget_direct_info(ddm_path)
            return None
        sig = (ddm_stat.st_size, ddm_stat.st_mtime_ns,
               dds_stat.st_size, dds_stat.st_mtime_ns)
        with self._direct_memo_lock:
            info = self._direct_memo.get(ddm_path)
            if info is not None and info.sig == sig:
                self._direct_memo.move_to_end(ddm_path)
                return info
        meta = self._read_ddm(ddm_path)
        if meta is None:
            self._forget_direct_info(ddm_path)
            return None
        info = self._parse_direct_info(tile_id, meta, sig, dds_stat)
        with self._direct_memo_lock:
            self._direct_memo[ddm_path] = info
            self._direct_memo.move_to_end(ddm_path)
            while len(self._direct_memo) > self._DIRECT_MEMO_MAX:
                self._direct_memo.popitem(last=False)
        return info

    def _forget_direct_info(self, ddm_path: str) -> None:
        with self._direct_memo_lock:
            self._direct_memo.pop(ddm_path, None)

    def _parse_direct_info(self, tile_id, meta, sig, dds_stat) -> "_DirectInfo":
        mm_count = int(meta.get("mm", 0) or 0)
        populated = meta.get("populated_mipmaps")
        all_levels = populated is None or set(range(mm_count)) <= set(populated)
        full = all_levels and self._mm0_cache_complete(meta)
        no_imagery = 0
        if all_levels and not full:
            holes = self._no_imagery_holes(meta)
            if holes:
                full = True
                no_imagery = holes
                self._bump("dds_direct_no_imagery_entries")
        return _DirectInfo(
            sig=sig,
            max_zl=meta.get("max_zl"),
            fmt=meta.get("fmt"),
            comp=meta.get("comp"),
            disk_compression=meta.get("disk_compression", "none"),
            width=int(meta.get("w", 0) or 0),
            height=int(meta.get("h", 0) or 0),
            full=bool(full),
            finest_run=self._finest_populated_run(meta, mm_count),
            needs_healing=bool(meta.get("needs_healing")),
            dds_size=dds_stat.st_size,
            dds_mtime_ns=dds_stat.st_mtime_ns,
            no_imagery_chunks=no_imagery,
        )

    @staticmethod
    def _direct_info_stale(info: "_DirectInfo", expected_size: int) -> bool:
        """:meth:`_is_stale` on a parsed entry, without touching the disk."""
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG  # type: ignore[no-redef]
        current_fmt = CFG.pydds.format.upper()
        if current_fmt in ("DXT1",):
            current_fmt = "BC1"
        elif current_fmt in ("DXT5",):
            current_fmt = "BC3"
        if info.fmt != current_fmt:
            return True
        if info.comp != CFG.pydds.compressor.upper():
            return True
        if info.disk_compression == "none" and info.dds_size != expected_size:
            return True
        return False

    def _direct_manifests(self, tile_id: str, ddm_path: str, sig: tuple) -> dict:
        """The row manifests of an entry opened for direct reads.

        Raises :class:`disk_texture.DiskTextureStale` when the .ddm was
        rewritten since the open: its manifests may describe other bytes.
        """
        try:
            from autoortho.disk_texture import DiskTextureStale
        except ImportError:
            from disk_texture import DiskTextureStale  # type: ignore[no-redef]
        try:
            st = os.stat(ddm_path)
        except OSError as exc:
            raise DiskTextureStale(f"{tile_id}: {exc}") from exc
        if (st.st_size, st.st_mtime_ns) != sig[:2]:
            raise DiskTextureStale(f"{tile_id}: entry rewritten since open")
        meta = self._read_ddm(ddm_path)
        if meta is None:
            raise DiskTextureStale(f"{tile_id}: entry unreadable")
        return manifests_from_dict(meta["mm0_manifest"], tile_id=tile_id)

    def _no_imagery_holes(self, meta: dict) -> int:
        """How many mipmap-0 chunks of an entry are filled from a lower zoom
        because the provider has no imagery there; 0 when any is not (or
        none are). See :meth:`_no_imagery_holes_for`."""
        if meta.get("missing_indices"):
            return 0
        fallback = meta.get("fallback_indices") or []
        if not fallback:
            return 0
        try:
            tile_id = (
                f"{int(meta['tile_row'])}_{int(meta['tile_col'])}_"
                f"{meta['map']}_{int(meta['zl'])}"
            )
            manifests = manifests_from_dict(meta["mm0_manifest"], tile_id=tile_id)
            holes = self._no_imagery_holes_for(
                manifests, int(meta["tile_row"]), int(meta["tile_col"]),
                int(meta["zl"]), int(meta["max_zl"]), str(meta["map"]),
                int(meta["w"]), int(meta["h"]),
            )
            recorded = set(int(i) for i in fallback)
        except (KeyError, TypeError, ValueError):
            return 0
        if not holes or holes != recorded:
            return 0
        return len(holes)

    def _no_imagery_holes_for(self, manifests, tile_row: int, tile_col: int,
                              zoom: int, max_zoom: int, maptype: str,
                              width: int, height: int) -> set:
        """The mipmap-0 chunk indices filled from a lower zoom because the
        provider has no imagery there, or an empty set when any non-target
        chunk is not that (or there is none).

        Such a texture is as good as it can get: the live path would build
        the same fallbacks again at every open. A chunk counts only if the
        chunk store remembers the provider answering "no imagery" for it; a
        download that failed or timed out is never recorded so, and leaves
        the texture live until it is healed. Missing-colour chunks (no
        fallback either) keep it live too.
        """
        manifests = dict(manifests or {})
        chunks_per_row = max(1, int(width) // 256)
        rows = max(1, (int(height) + 255) // 256)
        if set(manifests) != set(range(rows)):
            return set()
        target = int(MipmapSource.TARGET_PROVIDER)
        non_target = set()
        for row_index, manifest in manifests.items():
            if len(manifest.sources) != chunks_per_row or manifest.missing_indices:
                return set()
            fallback_columns = set(manifest.fallback_indices)
            base = row_index * chunks_per_row
            for index, value in enumerate(manifest.sources):
                if value == target:
                    continue
                if index not in fallback_columns:
                    return set()  # unknown or unrated source
                non_target.add(base + index)
        if not non_target:
            return set()
        diff = int(zoom) - int(max_zoom)
        col0 = tile_col >> diff if diff >= 0 else tile_col << (-diff)
        row0 = tile_row >> diff if diff >= 0 else tile_row << (-diff)
        paths = [
            os.path.join(
                self._cache_dir,
                f"{col0 + idx % chunks_per_row}_{row0 + idx // chunks_per_row}"
                f"_{max_zoom}_{maptype}.jpg",
            )
            for idx in sorted(non_target)
        ]
        try:
            nothing_there = chunk_store.no_imagery_paths(paths)
        except Exception as exc:
            log.debug(f"No-imagery lookup failed: {exc}")
            return set()
        return non_target if len(nothing_there) == len(paths) else set()

    def unpin(self, key: str) -> None:
        """Release one direct-read pin taken by :meth:`open_direct`."""
        with self._lock:
            count = self._pins.get(key, 0) - 1
            if count > 0:
                self._pins[key] = count
            else:
                self._pins.pop(key, None)

    def is_pinned(self, key: str) -> bool:
        with self._lock:
            return key in self._pins

    def _schedule_promote(self, tile_id, max_zoom, dds_path, ddm_path,
                          expected_size) -> None:
        key = self._tile_key(tile_id, max_zoom)
        with self._lock:
            if key in self._promoting:
                return
            self._promoting.add(key)
        if not self._submit_maintenance(
            self._promote_uncompressed, key, dds_path, ddm_path, expected_size
        ):
            with self._lock:
                self._promoting.discard(key)

    def _promote_uncompressed(self, key, dds_path, ddm_path,
                              expected_size) -> bool:
        """Rewrite a zstd entry uncompressed so it can be read directly."""
        try:
            if not writes_allowed():
                return False
            with self._tile_file_lock(ddm_path):
                meta = self._read_ddm(ddm_path)
                if meta is None or meta.get("disk_compression") != "zstd":
                    return False
                with open(dds_path, "rb") as f:
                    raw = self._decompress_dds(f.read(), meta)
                if len(raw) != expected_size:
                    return False
                tile_id = (
                    f"{int(meta['tile_row'])}_{int(meta['tile_col'])}_"
                    f"{meta['map']}_{int(meta['zl'])}"
                )
                manifests = manifests_from_dict(meta["mm0_manifest"],
                                                tile_id=tile_id)
                if not _rows_match(raw, manifests, int(meta.get("w", 0)),
                                   int(meta.get("h", 0)), meta.get("fmt")):
                    return False
                tmp = dds_path + f".tmp.{os.getpid()}.{threading.get_ident()}"
                with open(tmp, "wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, dds_path)
                self._remember_rows_checked(dds_path, manifests, len(raw))
                meta["disk_compression"] = "none"
                self._write_ddm(ddm_path, meta)
            self._record_store(key, dds_path, ddm_path, count_store=False)
            self._bump("dds_direct_promoted")
            return True
        except Exception as exc:
            log.debug(f"DDS cache promote failed for {key}: {exc}")
            try:
                os.remove(dds_path + f".tmp.{os.getpid()}.{threading.get_ident()}")
            except OSError:
                pass
            return False
        finally:
            with self._lock:
                self._promoting.discard(key)

    def contains(self, tile_id: str, max_zoom: int, tile) -> bool:
        """Check if a COMPLETE tile exists in the cache.

        Returns False for partial DDS entries (from store_incremental) where
        mipmap 0 is not populated.  This allows BackgroundDDSBuilder to build
        the complete DDS instead of skipping tiles that only have mm4-12.
        Without this check, store_incremental() creates partial entries that
        permanently block the builder, causing the 2.1.0 pixelation regression.
        """
        if not self._enabled:
            return False
        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom)
            if not os.path.exists(dds_path):
                return False

            # Read DDM sidecar to check if mm0 is populated and fully native.
            # If no DDM exists, assume complete (v2 compat / pre-DDM entries).
            meta = self._read_ddm(ddm_path)
            if meta is None:
                return not os.path.exists(ddm_path)
            return self._mm0_cache_complete(meta)
        except Exception:
            return False

    def get_staging_path(self, tile_id: str, max_zoom: int, tile) -> Optional[str]:
        """Get a temp file path for native direct-to-disk writes.

        After the native C code writes to this path, call
        ``store_from_file()`` to atomically move it into the cache.
        """
        if not self._enabled:
            return None
        dds_path, _ = self._paths_for(
            tile.row, tile.col, tile.maptype,
            tile.tilename_zoom, max_zoom)
        os.makedirs(os.path.dirname(dds_path), exist_ok=True)
        return dds_path + f".tmp.{os.getpid()}"

    def _check_upgrade_candidate(self, tile_id: str, max_zoom: int, tile) -> None:
        """Check if a lower-ZL DDS exists that can be upgraded via mipmap shift.
        
        Sets tile._dds_upgrade_available if a single-step upgrade candidate is found.
        """
        old_zoom = max_zoom - 1
        if old_zoom < 12:  # No point checking below ZL12
            return

        try:
            old_dds_path, old_ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, old_zoom
            )
            old_meta = self._read_ddm(old_ddm_path)
            if old_meta is not None and old_meta.get("max_zl") == old_zoom:
                tile._dds_upgrade_available = (old_dds_path, old_meta)
                log.debug(f"DDS cache: ZL upgrade candidate found {tile_id} "
                          f"z{old_zoom} -> z{max_zoom}")
        except Exception:
            pass

    def _check_downgrade_candidate(self, tile_id: str, max_zoom: int, tile) -> None:
        """Check if a higher-ZL DDS exists that can be downgraded via mipmap strip.
        
        Sets tile._dds_downgrade_available if a single-step downgrade candidate is found.
        """
        old_zoom = max_zoom + 1
        try:
            old_dds_path, old_ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, old_zoom
            )
            old_meta = self._read_ddm(old_ddm_path)
            if old_meta is not None and old_meta.get("max_zl") == old_zoom:
                tile._dds_downgrade_available = (old_dds_path, old_meta)
                log.debug(f"DDS cache: ZL downgrade candidate found {tile_id} "
                          f"z{old_zoom} -> z{max_zoom}")
        except Exception:
            pass

    def _get_format_and_compressor(self):
        """Return (dds_format, compressor) from current config."""
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG  # type: ignore[no-redef]

        dds_format = CFG.pydds.format.upper()
        if dds_format in ("DXT1",):
            dds_format = "BC1"
        elif dds_format in ("DXT5",):
            dds_format = "BC3"
        compressor = CFG.pydds.compressor.upper()
        return dds_format, compressor

    @staticmethod
    def _get_compression_settings():
        """Return (compression_type, level) from config."""
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG  # type: ignore[no-redef]
        comp = getattr(CFG.pydds, 'dds_compression', 'zstd').lower()
        if comp not in ('none', 'zstd'):
            comp = 'zstd'
        level = int(getattr(CFG.pydds, 'dds_compression_level', 3))
        level = max(1, min(19, level))
        return comp, level

    def _compress_dds(self, data: bytes) -> bytes:
        """Compress raw DDS bytes with zstd. Returns original data if compression disabled."""
        if self._compression != "zstd" or not _HAS_ZSTD:
            return data
        cctx = zstandard.ZstdCompressor(level=self._compression_level)
        return cctx.compress(data)

    def _decompress_dds(self, data: bytes, meta: dict) -> bytes:
        """Decompress DDS bytes based on DDM metadata. Returns data unchanged if uncompressed."""
        disk_comp = meta.get("disk_compression", "none")
        if disk_comp != "zstd":
            return data
        if not _HAS_ZSTD:
            raise RuntimeError("Compressed DDS but zstandard not installed")
        dctx = zstandard.ZstdDecompressor()
        return dctx.decompress(data)

    def _create_dds_skeleton(self, dds_path: str, header_bytes: bytes,
                             total_size: int) -> bool:
        """Create an empty DDS file with the correct header and total size.

        The file is extended to ``total_size`` without writing the unwritten
        region (see ``_extend_sparse``).

        Returns True on success.
        """
        tmp = dds_path + f".tmp.{os.getpid()}"
        try:
            with open(tmp, "wb") as f:
                f.write(header_bytes)
                _extend_sparse(f, total_size)
            os.replace(tmp, dds_path)
            return True
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _filter_healable_missing(missing_indices, tile, max_zoom):
        """Drop permanently unavailable chunks from the healing-needed list."""
        if not missing_indices:
            return missing_indices
        chunks = getattr(tile, 'chunks', {}).get(max_zoom, [])
        if not chunks:
            return missing_indices
        return [
            i for i in missing_indices
            if i >= len(chunks) or not getattr(chunks[i], 'permanent_failure', False)
        ]

    def _store_no_imagery_holes(self, tile, max_zoom, mm0_manifest) -> bool:
        """Whether a mipmap 0 that is not all target imagery may be stored
        all the same: its only holes are lower-zoom fallbacks for chunks the
        provider has no imagery for (hybrid read path plan, N4). Built again
        it would come out the same."""
        try:
            dds = tile.dds
            holes = self._no_imagery_holes_for(
                mm0_manifest, int(tile.row), int(tile.col),
                int(tile.tilename_zoom), int(max_zoom), str(tile.maptype),
                int(dds.width), int(dds.height),
            )
        except Exception:
            return False
        if holes:
            self._bump("dds_cache_store_no_imagery")
            return True
        return False

    @profiled_stage("cache.dds_store", tile_arg=1)
    def store(self, tile_id: str, max_zoom: int, dds_bytes: bytes,
              tile,
              mm0_missing_indices: Optional[List[int]] = None,
              mm0_fallback_indices: Optional[List[int]] = None,
              mm0_manifest=None) -> bool:
        """
        Store a DDS build result in the persistent cache.
        
        Writes the DDS file and its DDM metadata sidecar atomically.
        
        Args:
            tile_id: Tile identifier
            max_zoom: Effective maximum zoom level
            dds_bytes: Complete DDS file bytes (header + mipmaps)
            tile: Tile object
            mm0_missing_indices: Flat chunk indices missing at build time.
                None means "assume complete".
            mm0_fallback_indices: Flat chunk indices that used low-res fallback
                imagery instead of native-resolution data.
                None means "no fallbacks used".
        
        Returns:
            True on success, False on failure
        """
        if not self._enabled:
            return False
        if not writes_allowed():
            # The cache drive is nearly full (see ram_brake.DiskBrake).
            return False

        if not dds_bytes or len(dds_bytes) < 128:
            return False
        if not mm0_manifest:
            log.debug("DDS cache store rejected without a mipmap-zero manifest")
            return False
        if not self._validate_mm0_manifest_bytes(
            tile,
            max_zoom,
            dds_bytes,
            mm0_manifest,
        ):
            log.debug("DDS cache store rejected a mismatched row manifest")
            return False
        if self.real_only and not all(
            getattr(manifest, "is_exact", False)
            for manifest in dict(mm0_manifest).values()
        ) and not self._store_no_imagery_holes(tile, max_zoom, mm0_manifest):
            # Only real imagery is persisted: mipmap 0 with fallback or
            # missing rows is rebuilt next time instead of cached and healed.
            # Its coarser levels are real and are kept.
            self._bump("dds_cache_store_rejected_not_real")
            self._store_coarser_levels(tile, max_zoom, dds_bytes)
            return False

        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom
            )

            # Ensure directory exists
            os.makedirs(os.path.dirname(dds_path), exist_ok=True)

            dds_format, compressor = self._get_format_and_compressor()

            # Compress DDS data for disk storage
            disk_bytes = self._compress_dds(dds_bytes)
            disk_compression = self._compression if len(disk_bytes) < len(dds_bytes) else "none"
            if disk_compression == "none":
                disk_bytes = dds_bytes

            # Write DDS atomically
            tmp_dds = dds_path + f".tmp.{os.getpid()}"
            with open(tmp_dds, "wb") as f:
                f.write(disk_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_dds, dds_path)
            if disk_compression == "none":
                # The bytes were checked against their row manifests above.
                self._remember_rows_checked(dds_path, mm0_manifest, len(dds_bytes))

            # Build and write DDM metadata atomically
            meta = self._build_ddm(tile, max_zoom,
                                   dds_format, compressor,
                                   mm0_manifest=mm0_manifest,
                                   disk_compression=disk_compression)
            with self._tile_file_lock(ddm_path):
                self._write_ddm(ddm_path, meta)
                try:
                    os.remove(self._row_records_path(ddm_path))
                except FileNotFoundError:
                    pass
                except OSError:
                    log.debug(
                        "Could not remove promoted partial rows for %s",
                        tile_id,
                    )

            # Update LRU tracking using the DDS, metadata, and row sidecar.
            key = self._tile_key(tile_id, max_zoom)
            self._record_store(key, dds_path, ddm_path)

            log.debug(
                "DDS cache STORE: %s z%s (%d bytes)",
                tile_id,
                max_zoom,
                self._entry_disk_size(dds_path, ddm_path),
            )

            if self._mm0_cache_complete(meta):
                try:
                    self._cleanup_jpegs_async(tile)
                except Exception as e:
                    log.debug(f"DDS cache JPEG cleanup dispatch failed for {tile_id}: {e}")

            return True

        except Exception as e:
            log.debug(f"DDS cache store error for {tile_id}: {e}")
            # Clean up temp files on failure
            try:
                os.remove(dds_path + f".tmp.{os.getpid()}")
            except OSError:
                pass
            return False

    @profiled_stage("cache.dds_store_incremental", tile_arg=1)
    def _store_coarser_levels(self, tile, max_zoom, dds_bytes):
        """Persist mipmaps 1+ of a build whose mipmap 0 is not all real."""
        try:
            dds = tile.dds
            green = set(getattr(tile, "_green_mipmaps", ()) or ())
            data, offsets = {}, {}
            for mm in dds.mipmap_list[1:]:
                if mm.idx in green or mm.endpos > len(dds_bytes):
                    continue
                data[mm.idx] = bytes(dds_bytes[mm.startpos:mm.endpos])
                offsets[mm.idx] = (mm.startpos, mm.length)
            if not data:
                return False
            return self.store_incremental(
                tile.id, max_zoom, tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, bytes(dds_bytes[:128]), dds.total_size,
                dds.width, dds.height, dds.mipMapCount, data, offsets,
            )
        except Exception as exc:
            log.debug(f"Could not keep the coarser levels of {getattr(tile, 'id', '?')}: {exc}")
            return False

    @staticmethod
    def _bump(name):
        try:
            from autoortho.aostats import inc_stat
        except ImportError:
            try:
                from aostats import inc_stat
            except ImportError:
                return
        try:
            inc_stat(name)
        except Exception:
            pass

    def store_incremental(self, tile_id: str, max_zoom: int,
                          row: int, col: int, maptype: str,
                          tilename_zoom: int,
                          header_bytes: bytes, total_size: int,
                          width: int, height: int, mm_count: int,
                          mipmap_data: dict,
                          mipmap_offsets: dict) -> bool:
        """Persist one or more mipmap data buffers to the DDS cache file.

        Creates the DDS skeleton on first call, then seeks and writes each
        mipmap at its correct offset.  The DDM sidecar is updated *after*
        all data writes succeed (crash-safe ordering).

        Args:
            tile_id:        Tile identifier
            max_zoom:       Effective maximum zoom level
            row, col:       Tile grid coordinates
            maptype:        Map type string
            tilename_zoom:  Tile-name zoom level
            header_bytes:   DDS header (128 bytes)
            total_size:     Full DDS file size (header + all mipmaps)
            width, height:  Texture dimensions
            mm_count:       Total mipmap count
            mipmap_data:    ``{mipmap_idx: bytes}`` -- raw DXT data per mipmap
            mipmap_offsets: ``{mipmap_idx: (startpos, length)}`` -- file offsets

        Returns:
            True on success, False on failure
        """
        if not self._enabled:
            return False

        if not mipmap_data:
            return False
        if 0 in mipmap_data:
            log.debug(
                "Incremental DDS store rejected mipmap zero without manifests"
            )
            return False

        try:
            dds_path, ddm_path = self._paths_for(
                row, col, maptype, tilename_zoom, max_zoom
            )
            os.makedirs(os.path.dirname(dds_path), exist_ok=True)

            # 1. Read existing DDM to find already-populated mipmaps
            existing_meta = self._read_ddm(ddm_path)
            already_populated = set()
            if existing_meta is not None:
                already_populated = set(existing_meta.get("populated_mipmaps", []))

            # 2. Filter out mipmaps that are already saved
            new_mipmaps = {
                idx: data for idx, data in mipmap_data.items()
                if idx not in already_populated
            }
            if not new_mipmaps:
                return True

            # 3. Build the DDS content with new mipmaps, then compress
            use_compression = self._compression == "zstd" and _HAS_ZSTD
            merged_populated = sorted(already_populated | set(new_mipmaps.keys()))

            if use_compression:
                # Compressed path: work in memory, compress, write atomically.
                # Partial DDS files (mostly zeros) compress extremely well
                # (~43 MB skeleton → ~200 KB compressed).
                if os.path.isfile(dds_path):
                    with open(dds_path, "rb") as f:
                        raw = f.read()
                    was_compressed = (existing_meta or {}).get(
                        "disk_compression", "none") == "zstd"
                    if was_compressed:
                        try:
                            dds_data = bytearray(
                                self._decompress_dds(raw, existing_meta))
                        except Exception:
                            dds_data = bytearray(total_size)
                            dds_data[:len(header_bytes)] = header_bytes
                    else:
                        dds_data = bytearray(raw)
                        # Pad if file is shorter than expected (truncated)
                        if len(dds_data) < total_size:
                            dds_data.extend(b'\x00' * (total_size - len(dds_data)))
                else:
                    dds_data = bytearray(total_size)
                    dds_data[:len(header_bytes)] = header_bytes

                for idx, data in new_mipmaps.items():
                    startpos, length = mipmap_offsets[idx]
                    if len(data) != length:
                        log.debug(f"Incremental save: mipmap {idx} size mismatch "
                                  f"({len(data)} vs {length}), skipping")
                        continue
                    dds_data[startpos:startpos + length] = data

                compressed = self._compress_dds(bytes(dds_data))
                if len(compressed) < len(dds_data):
                    disk_compression = self._compression
                    disk_bytes = compressed
                else:
                    disk_compression = "none"
                    disk_bytes = bytes(dds_data)

                tmp = dds_path + f".tmp.{os.getpid()}"
                with open(tmp, "wb") as f:
                    f.write(disk_bytes)
                os.replace(tmp, dds_path)
                disk_size = len(disk_bytes)
            else:
                # Uncompressed path: seek-write in place (original behavior)
                if not os.path.isfile(dds_path):
                    self._create_dds_skeleton(dds_path, header_bytes, total_size)

                with open(dds_path, "r+b") as f:
                    for idx, data in new_mipmaps.items():
                        startpos, length = mipmap_offsets[idx]
                        if len(data) != length:
                            log.debug(f"Incremental save: mipmap {idx} size mismatch "
                                      f"({len(data)} vs {length}), skipping")
                            continue
                        f.seek(startpos)
                        f.write(data)

                disk_compression = "none"
                try:
                    disk_size = os.path.getsize(dds_path)
                except OSError:
                    disk_size = total_size

            # 4. Update DDM *after* data writes (crash-safe ordering)
            dds_format, compressor = self._get_format_and_compressor()
            partial_mipmaps = {}
            mm0_manifest = None
            if (existing_meta is not None and
                    existing_meta.get("v") == DDM_VERSION and
                    0 not in new_mipmaps):
                partial_mipmaps = existing_meta.get("partial_mipmaps", {})
                mm0_manifest = existing_meta.get("mm0_manifest")
            meta = self._build_ddm_incremental(
                row, col, maptype, tilename_zoom, max_zoom,
                width, height, mm_count, dds_format, compressor,
                merged_populated, disk_compression=disk_compression,
                partial_mipmaps=partial_mipmaps,
                mm0_manifest=mm0_manifest,
            )
            with self._tile_file_lock(ddm_path):
                latest_meta = self._read_ddm(ddm_path)
                if (
                    latest_meta is not None
                    and latest_meta.get("v") == DDM_VERSION
                    and 0 not in new_mipmaps
                ):
                    meta["partial_mipmaps"] = latest_meta.get(
                        "partial_mipmaps",
                        {},
                    )
                    meta["mm0_manifest"] = latest_meta.get(
                        "mm0_manifest",
                        meta["mm0_manifest"],
                    )
                self._write_ddm(ddm_path, meta)
                if 0 in merged_populated:
                    try:
                        os.remove(self._row_records_path(ddm_path))
                    except FileNotFoundError:
                        pass
                    except OSError:
                        log.debug(
                            "Could not remove promoted partial rows for %s",
                            tile_id,
                        )

            # 5. LRU tracking includes DDS metadata and partial-row records.
            key = self._tile_key(tile_id, max_zoom)
            self._record_store(key, dds_path, ddm_path)

            log.debug(f"DDS cache STORE_INCR: {tile_id} z{max_zoom} "
                      f"mipmaps={sorted(new_mipmaps.keys())} "
                      f"populated={merged_populated} "
                      f"disk={disk_size} compression={disk_compression}")

            return True

        except Exception as e:
            log.debug(f"DDS cache incremental store error for {tile_id}: {e}")
            return False

    def store_from_file(self, tile_id: str, max_zoom: int,
                        source_path: str, tile,
                        mm0_missing_indices: Optional[List[int]] = None,
                        mm0_fallback_indices: Optional[List[int]] = None,
                        mm0_manifest=None) -> bool:
        """
        Store a DDS build result from an existing file on disk.
        
        Used by native pipeline paths that write DDS directly to the
        ephemeral cache via ``finalize_to_file()`` or ``build_*_to_file()``.
        
        Attempts a hard-link first (zero-copy, instant) which works when
        both paths reside on the same filesystem.  Falls back to
        ``shutil.copy2()`` if linking fails (cross-filesystem, OS
        restrictions, etc.).
        
        Args:
            tile_id: Tile identifier
            max_zoom: Effective maximum zoom level
            source_path: Path to the DDS file already on disk
            tile: Tile object
            mm0_missing_indices: Flat chunk indices missing at build time.
            mm0_fallback_indices: Flat chunk indices that used low-res fallback.
        
        Returns:
            True on success, False on failure
        """
        if not self._enabled:
            return False
        stored = False
        try:
            stored = self._store_from_file(
                tile_id, max_zoom, source_path, tile,
                mm0_missing_indices, mm0_fallback_indices, mm0_manifest)
            return stored
        finally:
            if not stored and self._is_staging_path(source_path, max_zoom, tile):
                # The staging file is this cache's own temp file: a build
                # that was not stored must not leave it behind.
                try:
                    os.remove(source_path)
                except OSError:
                    pass

    def _is_staging_path(self, source_path: str, max_zoom: int, tile) -> bool:
        """``source_path`` is the file ``get_staging_path()`` hands out."""
        try:
            dds_path, _ = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom)
        except Exception:
            return False
        return self._same_path(source_path, dds_path + f".tmp.{os.getpid()}")

    @staticmethod
    def _same_path(first: str, second: str) -> bool:
        return (
            os.path.normcase(os.path.abspath(first))
            == os.path.normcase(os.path.abspath(second))
        )

    @staticmethod
    def _complete_mip_chain(tile, source_bytes) -> Optional[Tuple[bytes, bytes]]:
        """``(header, tail)`` that finish a native build's file, or None.

        The native builder (aodds.c) stops at the 4x4 level: 11 levels for a
        4096 px texture, its header saying so. The tile, and X-Plane, count
        levels to 1x1, and a live tile serves the levels below 4x4 as copies
        of the 4x4 one (``replace_mipmap_dense`` with the smallest level).
        Stored as it came, such a file is 16 bytes (BC1) short of the size an
        open expects and is never served from disk. Finished with the tile's
        own header and those copies, it holds the bytes the live tile serves.
        """
        dds = getattr(tile, "dds", None)
        mipmaps = getattr(dds, "mipmap_list", None)
        total = getattr(dds, "total_size", None)
        header_io = getattr(dds, "header", None)
        if not mipmaps or not total or header_io is None:
            return None
        size = len(source_bytes)
        if size >= total or size < 128 or bytes(source_bytes[:4]) != b"DDS ":
            return None
        try:
            count = struct.unpack_from("<I", source_bytes, 28)[0]
            header = bytes(header_io.getvalue())
        except Exception:
            return None
        if not 0 < count < len(mipmaps) or len(header) != 128:
            return None
        last = mipmaps[count - 1]
        if size != last.endpos or last.endpos > len(source_bytes):
            return None
        missing = mipmaps[count:]
        if not missing or any(m.length != last.length for m in missing):
            return None
        if missing[-1].endpos != total:
            return None
        block = bytes(source_bytes[last.startpos:last.endpos])
        return header, block * len(missing)

    def _store_from_file(self, tile_id: str, max_zoom: int,
                         source_path: str, tile,
                         mm0_missing_indices: Optional[List[int]] = None,
                         mm0_fallback_indices: Optional[List[int]] = None,
                         mm0_manifest=None) -> bool:
        try:
            source_size = os.path.getsize(source_path)
        except OSError:
            return False

        if source_size < 128:
            return False
        if not mm0_manifest:
            log.debug(
                "DDS cache file store rejected without a mipmap-zero manifest"
            )
            return False
        try:
            with open(source_path, "rb") as source_file:
                source_bytes = source_file.read()
        except OSError:
            return False
        if not self._validate_mm0_manifest_bytes(
            tile,
            max_zoom,
            source_bytes,
            mm0_manifest,
        ):
            log.debug("DDS cache file store rejected a mismatched row manifest")
            return False
        completed = self._complete_mip_chain(tile, source_bytes)

        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom
            )

            # Ensure directory exists
            os.makedirs(os.path.dirname(dds_path), exist_ok=True)

            # Atomic placement: create at a temp name, then os.replace().
            tmp_dds = dds_path + f".tmp.{os.getpid()}"
            # get_staging_path() hands out this same temp name, so a build
            # staged there is already in place. Removing it to make room for
            # a link, as for a source elsewhere, deleted the build itself and
            # every uncompressed store failed with "file not found".
            staged_in_place = self._same_path(source_path, tmp_dds)

            linked = False
            if completed is not None:
                header, tail = completed
                if staged_in_place:
                    # The cache's own temp file: finish it where it is.
                    with open(tmp_dds, "r+b") as staged:
                        staged.write(header)
                        staged.seek(0, os.SEEK_END)
                        staged.write(tail)
                source_bytes = header + source_bytes[len(header):] + tail
                source_size = len(source_bytes)
                self._bump("dds_cache_store_mip_chain_completed")
            if self._compression == "zstd" and _HAS_ZSTD:
                # Read source, compress, write compressed version
                raw_bytes = source_bytes
                disk_bytes = self._compress_dds(raw_bytes)
                disk_compression = self._compression if len(disk_bytes) < len(raw_bytes) else "none"
                if disk_compression == "none":
                    disk_bytes = raw_bytes
                with open(tmp_dds, "wb") as f:
                    f.write(disk_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                disk_size = len(disk_bytes)
            else:
                disk_compression = "none"
                if completed is not None and not staged_in_place:
                    # A link would share the source's bytes: write the
                    # completed file instead.
                    with open(tmp_dds, "wb") as f:
                        f.write(source_bytes)
                elif not staged_in_place:
                    # Try hard-link first -- zero-copy, instant, works when
                    # source and destination are on the same filesystem.
                    try:
                        try:
                            os.remove(tmp_dds)
                        except OSError:
                            pass
                        os.link(source_path, tmp_dds)
                        linked = True
                    except OSError:
                        pass

                    if not linked:
                        import shutil
                        shutil.copy2(source_path, tmp_dds)
                disk_size = source_size

                # Windows requires a writable handle for fsync/_commit.
                # The compression branch already synced its writable handle.
                with open(tmp_dds, "r+b") as committed_dds:
                    os.fsync(committed_dds.fileno())

            os.replace(tmp_dds, dds_path)
            if disk_compression == "none" and (staged_in_place or linked):
                # The same file whose bytes were checked above (a copy is
                # checked again at its first read).
                self._remember_rows_checked(dds_path, mm0_manifest, source_size)

            # Write DDM metadata and retire any superseded partial rows.
            dds_format, compressor = self._get_format_and_compressor()
            meta = self._build_ddm(tile, max_zoom,
                                   dds_format, compressor,
                                   mm0_manifest=mm0_manifest,
                                   disk_compression=disk_compression)
            with self._tile_file_lock(ddm_path):
                self._write_ddm(ddm_path, meta)
                try:
                    os.remove(self._row_records_path(ddm_path))
                except FileNotFoundError:
                    pass
                except OSError:
                    log.debug(
                        "Could not remove promoted partial rows for %s",
                        tile_id,
                    )

            # Update LRU tracking using the complete cache-entry footprint.
            key = self._tile_key(tile_id, max_zoom)
            self._record_store(key, dds_path, ddm_path)

            log.debug(f"DDS cache STORE (from file): {tile_id} z{max_zoom} "
                      f"({disk_size} bytes, compression={disk_compression})")

            if not mm0_missing_indices and not mm0_fallback_indices:
                try:
                    self._cleanup_jpegs_async(tile)
                except Exception as e:
                    log.debug(f"DDS cache JPEG cleanup dispatch failed for {tile_id}: {e}")

            return True

        except Exception as e:
            log.debug(f"DDS cache store_from_file error for {tile_id}: {e}")
            try:
                os.remove(dds_path + f".tmp.{os.getpid()}")
            except OSError:
                pass
            return False

    def upgrade_zl(self, tile_id: str, old_max_zoom: int, new_max_zoom: int,
                   new_mm0_bytes: bytes, tile,
                   new_mm0_manifest=None) -> Optional[bytes]:
        """
        Upgrade a cached DDS from one zoom level to the next.
        
        When max_zoom increases by 1 (e.g., ZL16 -> ZL17), the DDS dimensions
        double (e.g., 4096x4096 -> 8192x8192). Old mipmaps are "shifted" into
        the new file: old mm0 becomes new mm1, old mm1 becomes new mm2, etc.
        Only the new mm0 (highest resolution) needs to be built fresh.
        
        Args:
            tile_id: Tile identifier
            old_max_zoom: Previous zoom level of the cached DDS
            new_max_zoom: New (higher) zoom level
            new_mm0_bytes: Compressed mipmap 0 data for the new zoom level
            tile: Tile object (has the NEW dimensions in its DDS)
        
        Returns:
            Complete new DDS bytes on success, None on failure
        """
        if not self._enabled:
            return None

        if new_max_zoom - old_max_zoom != 1:
            log.debug(f"DDS upgrade: only single-step upgrades supported "
                      f"({old_max_zoom} -> {new_max_zoom})")
            return None
        if not new_mm0_manifest:
            log.debug(
                "DDS upgrade rejected without a new mipmap-zero manifest"
            )
            return None

        try:
            # Load old DDS
            old_dds_path, old_ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, old_max_zoom
            )

            # Read metadata first (needed for decompression)
            old_meta = self._read_ddm(old_ddm_path)
            if old_meta is None:
                return None

            try:
                with open(old_dds_path, "rb") as f:
                    old_raw = f.read()
            except (FileNotFoundError, OSError):
                log.debug(f"DDS upgrade: old file not found: {old_dds_path}")
                return None

            try:
                old_dds_bytes = self._decompress_dds(old_raw, old_meta)
            except Exception:
                log.debug(f"DDS upgrade: decompression failed for {old_dds_path}")
                old_key = self._tile_key(tile_id, old_max_zoom)
                self._remove_entry_files(
                    old_key,
                    old_dds_path,
                    old_ddm_path,
                )
                return None

            old_width = old_meta.get("w", 0)
            old_height = old_meta.get("h", 0)
            if old_width == 0 or old_height == 0:
                return None

            # Compute old and new mipmap layouts using pydds
            try:
                from autoortho import pydds
            except ImportError:
                import pydds  # type: ignore[no-redef]

            new_dds_ref = tile.dds
            if new_dds_ref is None:
                return None

            new_width = new_dds_ref.width
            new_height = new_dds_ref.height
            new_total_size = new_dds_ref.total_size
            new_mm_list = new_dds_ref.mipmap_list

            # Create a temporary DDS for computing old mipmap offsets
            dxt_format = old_meta.get("fmt", "BC1")
            old_dds_struct = pydds.DDS(old_width, old_height, dxt_format=dxt_format)
            old_mm_list = old_dds_struct.mipmap_list
            old_mm_count = len(old_mm_list)

            # Allocate new DDS buffer
            new_dds = bytearray(new_total_size)

            # Copy new header from tile's DDS (128 bytes with correct dimensions)
            new_dds_ref.header.seek(0)
            header_bytes = new_dds_ref.header.read()
            new_dds[:128] = header_bytes[:128]

            # Write new mm0 at offset 128
            if len(new_mm_list) > 0:
                mm0 = new_mm_list[0]
                end = min(mm0.startpos + len(new_mm0_bytes), mm0.endpos)
                new_dds[mm0.startpos:end] = new_mm0_bytes[:end - mm0.startpos]

            # Shift old mipmaps: old mm[i] -> new mm[i+1]
            for i, old_mm in enumerate(old_mm_list):
                new_idx = i + 1
                if new_idx >= len(new_mm_list):
                    break

                new_mm = new_mm_list[new_idx]
                # Extract old mipmap data
                old_data = old_dds_bytes[old_mm.startpos:old_mm.endpos]
                # Copy to new position (sizes should match)
                copy_len = min(len(old_data), new_mm.length)
                new_dds[new_mm.startpos:new_mm.startpos + copy_len] = old_data[:copy_len]

            new_dds_bytes = bytes(new_dds)

            # Store the upgraded DDS
            if self.store(
                tile_id,
                new_max_zoom,
                new_dds_bytes,
                tile,
                mm0_manifest=new_mm0_manifest,
            ):
                # Remove old entry
                old_key = self._tile_key(tile_id, old_max_zoom)
                self._remove_entry_files(
                    old_key,
                    old_dds_path,
                    old_ddm_path,
                )
                with self._lock:
                    self._upgrades += 1

                log.info(f"DDS upgrade: {tile_id} z{old_max_zoom} -> z{new_max_zoom} "
                         f"({old_width}x{old_height} -> {new_width}x{new_height})")
                return new_dds_bytes

            return None

        except Exception as e:
            log.debug(f"DDS upgrade error for {tile_id}: {e}")
            return None

    def downgrade_zl(self, tile_id: str, old_max_zoom: int, new_max_zoom: int,
                     tile, new_mm0_manifest=None) -> Optional[bytes]:
        """Downgrade a cached DDS from one zoom level to the next lower one.

        Inverse of ``upgrade_zl()``: strips mm0 and shifts remaining mipmaps
        up one level (old mm1 → new mm0, old mm2 → new mm1, etc.).

        Only single-step downgrades are supported.

        Returns complete new DDS bytes on success, None on failure.
        """
        if not self._enabled:
            return None

        if old_max_zoom - new_max_zoom != 1:
            log.debug(f"DDS downgrade: only single-step downgrades supported "
                      f"({old_max_zoom} -> {new_max_zoom})")
            return None
        if not new_mm0_manifest:
            log.debug(
                "DDS downgrade rejected because the shifted mipmap has no "
                "independently verified source manifest"
            )
            return None

        try:
            try:
                from autoortho import pydds
            except ImportError:
                import pydds  # type: ignore[no-redef]

            old_dds_path, old_ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, old_max_zoom)

            # Read metadata first (needed for decompression)
            old_meta = self._read_ddm(old_ddm_path)
            if old_meta is None:
                return None

            try:
                with open(old_dds_path, "rb") as f:
                    old_raw = f.read()
            except (FileNotFoundError, OSError):
                return None

            try:
                old_dds_bytes = self._decompress_dds(old_raw, old_meta)
            except Exception:
                log.debug(f"DDS downgrade: decompression failed for {old_dds_path}")
                old_key = self._tile_key(tile_id, old_max_zoom)
                self._remove_entry_files(
                    old_key,
                    old_dds_path,
                    old_ddm_path,
                )
                return None

            old_width = old_meta.get("w", 0)
            old_height = old_meta.get("h", 0)
            if old_width == 0 or old_height == 0:
                return None

            dxt_format = old_meta.get("fmt", "BC1")
            old_dds_struct = pydds.DDS(old_width, old_height, dxt_format=dxt_format)
            old_mm_list = old_dds_struct.mipmap_list

            new_width = old_width >> 1
            new_height = old_height >> 1
            if new_width < 4 or new_height < 4:
                return None

            new_dds_struct = pydds.DDS(new_width, new_height, dxt_format=dxt_format)
            new_mm_list = new_dds_struct.mipmap_list
            new_total_size = new_dds_struct.total_size

            new_dds = bytearray(new_total_size)

            # Copy header from new structure (correct dimensions)
            new_dds_struct.header.seek(0)
            header_bytes = new_dds_struct.header.read()
            new_dds[:128] = header_bytes[:128]

            # Shift: old mm[i+1] → new mm[i]  (skip old mm0)
            for i in range(1, len(old_mm_list)):
                new_idx = i - 1
                if new_idx >= len(new_mm_list):
                    break
                new_mm = new_mm_list[new_idx]
                old_data = old_dds_bytes[old_mm_list[i].startpos:old_mm_list[i].endpos]
                copy_len = min(len(old_data), new_mm.length)
                new_dds[new_mm.startpos:new_mm.startpos + copy_len] = old_data[:copy_len]

            new_dds_bytes = bytes(new_dds)

            if self.store(
                tile_id,
                new_max_zoom,
                new_dds_bytes,
                tile,
                mm0_manifest=new_mm0_manifest,
            ):
                old_key = self._tile_key(tile_id, old_max_zoom)
                self._remove_entry_files(
                    old_key,
                    old_dds_path,
                    old_ddm_path,
                )

                log.info(f"DDS downgrade: {tile_id} z{old_max_zoom} -> z{new_max_zoom} "
                         f"({old_width}x{old_height} -> {new_width}x{new_height})")
                return new_dds_bytes

            return None

        except Exception as e:
            log.debug(f"DDS downgrade error for {tile_id}: {e}")
            return None

    def invalidate(self, tile_id: str, max_zoom: int) -> bool:
        """Remove a specific entry from the cache.
        
        Args:
            tile_id: Tile identifier
            max_zoom: Zoom level of the cached entry
        
        Returns:
            True if an entry was removed
        """
        if not self._enabled:
            return False

        key = self._tile_key(tile_id, max_zoom)
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return False
        dds_path, ddm_path = entry[0], entry[1]
        self._remove_entry_files(key, dds_path, ddm_path)
        return self._entry_disk_size(dds_path, ddm_path) == 0

    def evict_lru(self, bytes_to_free: int) -> int:
        """
        Evict oldest entries until ``bytes_to_free`` bytes are reclaimed.
        
        Args:
            bytes_to_free: Number of bytes to free up
        
        Returns:
            Number of bytes actually freed
        """
        freed = 0
        to_delete = []

        with self._lock:
            # Entries open for direct reads are kept: on Windows their delete
            # would fail and orphan the file, elsewhere it wastes the work.
            skipped = []
            while freed < bytes_to_free and self._entries:
                key, entry = self._entries.popitem(last=False)
                if key in self._pins or (
                    protect_dds is not None and protect_dds(entry[0])
                ):
                    skipped.append((key, entry))
                    continue
                dds_path, ddm_path, size, _ = entry
                self._current_size -= size
                freed += size
                self._evictions += 1
                to_delete.append((dds_path, ddm_path))
            for key, entry in skipped:
                self._entries[key] = entry

        # Delete files outside the lock
        for dds_path, ddm_path in to_delete:
            self._delete_pair(dds_path, ddm_path)

        if freed > 0:
            log.debug(f"DDS cache evicted {len(to_delete)} entries, freed {freed / (1024*1024):.1f}MB")
            if self._budget_manager is not None:
                self._budget_manager.account_dds(-freed)
        return freed

    def get_disk_usage(self) -> int:
        """Return current tracked disk usage in bytes."""
        with self._lock:
            return self._current_size

    @property
    def stats(self) -> dict:
        """Return cache statistics."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0.0
        with self._lock:
            entries = len(self._entries)
            size = self._current_size
        with self._partial_row_condition:
            partial_queue_bytes = self._partial_row_queue_bytes
            partial_queue_rows = sum(
                len(item["rows"]) for item in self._partial_row_queue.values())
            partial_coalesced = self._partial_row_coalesced
            partial_dropped = self._partial_row_dropped
            partial_persisted = self._partial_row_persisted
            partial_not_persisted = self._partial_row_not_persisted
            partial_worker_exceptions = self._partial_row_worker_exceptions
        return {
            "hits": self._hits,
            "misses": self._misses,
            "stores": self._stores,
            "evictions": self._evictions,
            "upgrades": self._upgrades,
            "entries": entries,
            "disk_usage_mb": size / (1024 * 1024),
            "max_size_mb": self._max_size / (1024 * 1024),
            "hit_rate": hit_rate,
            "partial_row_queue_bytes": partial_queue_bytes,
            "partial_row_queue_rows": partial_queue_rows,
            "partial_row_coalesced": partial_coalesced,
            "partial_row_dropped": partial_dropped,
            "partial_row_persisted": partial_persisted,
            "partial_row_not_persisted": partial_not_persisted,
            "partial_row_worker_exceptions": partial_worker_exceptions,
        }

    # ------------------------------------------------------------------
    # Startup scan
    # ------------------------------------------------------------------

    def scan_existing(self) -> int:
        """
        Scan the DDS cache directory and populate the LRU tracking dict.
        
        Called once at startup in a background thread. Discovers entries
        written by previous sessions so they can be served immediately.
        
        Returns:
            Number of entries discovered
        """
        if not self._enabled:
            self._scan_complete.set()
            return 0

        count = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(self._dds_root):
                filename_set = set(filenames)
                for fname in filenames:
                    if fname.endswith(".dds"):
                        ddm_name = fname[:-4] + ".ddm"
                        if ddm_name not in filename_set:
                            try:
                                os.remove(os.path.join(dirpath, fname))
                            except OSError:
                                pass
                    elif fname.endswith(".ddm.rows"):
                        ddm_name = fname[:-5]
                        if ddm_name not in filename_set:
                            try:
                                os.remove(os.path.join(dirpath, fname))
                            except OSError:
                                pass

                for fname in filenames:
                    if not fname.endswith(".ddm"):
                        continue

                    ddm_path = os.path.join(dirpath, fname)
                    dds_path = ddm_path[:-4] + ".dds"
                    rows_path = self._row_records_path(ddm_path)
                    meta = self._read_ddm(ddm_path)
                    if meta is None:
                        self._delete_pair(dds_path, ddm_path)
                        continue

                    has_dds = os.path.isfile(dds_path)
                    if (
                        os.path.isfile(rows_path)
                        and not meta.get("partial_mipmaps")
                    ):
                        try:
                            os.remove(rows_path)
                        except OSError:
                            pass
                    has_partial_rows = (
                        os.path.isfile(rows_path)
                        and bool(meta.get("partial_mipmaps"))
                    )
                    if not has_dds and not has_partial_rows:
                        self._delete_pair(dds_path, ddm_path)
                        continue

                    # Reconstruct the tile key from metadata
                    row = meta.get("tile_row")
                    col = meta.get("tile_col")
                    maptype = meta.get("map", "")
                    zl = meta.get("zl", 0)
                    max_zl = meta.get("max_zl", 0)

                    if row is None or col is None:
                        continue

                    tile_id = f"{row}_{col}_{maptype}_{zl}"
                    key = self._tile_key(tile_id, max_zl)

                    size = self._entry_disk_size(dds_path, ddm_path)
                    if size <= 0:
                        continue

                    # Use file mtime as access time for initial ordering
                    mtimes = []
                    for path in (dds_path, ddm_path, rows_path):
                        try:
                            mtimes.append(os.path.getmtime(path))
                        except OSError:
                            pass
                    atime = max(mtimes) if mtimes else time.time()

                    with self._lock:
                        old = self._entries.get(key)
                        if old is None:
                            count += 1
                        else:
                            self._current_size -= old[2]
                        self._entries[key] = (
                            dds_path,
                            ddm_path,
                            size,
                            atime,
                        )
                        self._current_size += size

        except Exception as e:
            log.warning(f"DDS cache scan error: {e}")
        finally:
            self._scan_complete.set()

        if count > 0:
            # Sort by access time (oldest first for LRU)
            with self._lock:
                sorted_items = sorted(self._entries.items(), key=lambda x: x[1][3])
                self._entries.clear()
                for k, v in sorted_items:
                    self._entries[k] = v

            log.info(f"DDS cache scan: found {count} entries "
                     f"({self._current_size / (1024*1024):.1f}MB)")

        return count

    def migrate_uncompressed(self) -> int:
        """Re-compress existing uncompressed DDS files in-place.

        Walks the cache directory, reads each uncompressed DDS, compresses it
        with the current zstd settings, and atomically replaces the file.
        Updates DDM metadata and LRU size tracking.

        Designed to run once in a background thread after ``scan_existing()``.

        Returns the number of files migrated.
        """
        if not self._enabled or self._compression != "zstd" or not _HAS_ZSTD:
            return 0

        migrated = 0
        saved_bytes = 0
        start = time.monotonic()

        try:
            for dirpath, _dirnames, filenames in os.walk(self._dds_root):
                for fname in filenames:
                    if not fname.endswith(".ddm"):
                        continue

                    ddm_path = os.path.join(dirpath, fname)
                    meta = self._read_ddm(ddm_path)
                    if meta is None:
                        continue
                    if meta.get("disk_compression", "none") != "none":
                        continue

                    dds_path = ddm_path[:-4] + ".dds"
                    if not os.path.isfile(dds_path):
                        continue

                    try:
                        with open(dds_path, "rb") as f:
                            raw = f.read()
                        original_size = len(raw)

                        compressed = self._compress_dds(raw)
                        if len(compressed) >= original_size:
                            continue

                        tmp = dds_path + f".tmp.{os.getpid()}"
                        with open(tmp, "wb") as f:
                            f.write(compressed)
                        os.replace(tmp, dds_path)

                        meta["disk_compression"] = self._compression
                        self._write_ddm(ddm_path, meta)

                        # Update LRU size tracking
                        row = meta.get("tile_row")
                        col = meta.get("tile_col")
                        maptype = meta.get("map", "")
                        zl = meta.get("zl", 0)
                        max_zl = meta.get("max_zl", 0)
                        if row is not None and col is not None:
                            tile_id = f"{row}_{col}_{maptype}_{zl}"
                            key = self._tile_key(tile_id, max_zl)
                            self._record_store(
                                key,
                                dds_path,
                                ddm_path,
                                count_store=False,
                            )

                        saved_bytes += original_size - len(compressed)
                        migrated += 1

                    except Exception as e:
                        log.debug(f"DDS migration error for {dds_path}: {e}")
                        try:
                            os.remove(dds_path + f".tmp.{os.getpid()}")
                        except OSError:
                            pass
                        continue

        except Exception as e:
            log.warning(f"DDS cache migration error: {e}")

        elapsed = (time.monotonic() - start) * 1000
        if migrated > 0:
            log.info(f"DDS cache migration: compressed {migrated} files, "
                     f"saved {saved_bytes / (1024*1024):.1f}MB in {elapsed:.0f}ms")

        return migrated

    def find_upgrade_candidate(self, tile_id: str, max_zoom: int,
                               tile) -> Optional[Tuple[str, dict]]:
        """
        Check if there's a lower-ZL cached DDS that can be upgraded.
        
        Called when load() misses for the requested max_zoom. Looks for
        an entry at (max_zoom - 1) that can be shifted instead of rebuilt.
        
        Args:
            tile_id: Tile identifier
            max_zoom: The NEW (desired) zoom level
            tile: Tile object
        
        Returns:
            Tuple of (old_dds_path, old_metadata) if candidate found, else None
        """
        if not self._enabled:
            return None

        old_zoom = max_zoom - 1
        old_dds_path, old_ddm_path = self._paths_for(
            tile.row, tile.col, tile.maptype,
            tile.tilename_zoom, old_zoom
        )

        meta = self._read_ddm(old_ddm_path)
        if meta is None:
            return None

        if meta.get("max_zl") != old_zoom:
            return None

        return old_dds_path, meta

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def patch_missing_chunks(self, tile_id: str, max_zoom: int, tile,
                             chunk_jpegs: dict, sources: dict = None) -> bool:
        """In-place patch missing chunks into an existing DDS file and
        in-memory buffer.

        ``sources`` maps chunk index to the ``MipmapSource`` of its JPEG;
        chunks not listed are target-zoom provider imagery. Substitutes
        (upscaled lower zoom) stay listed for healing.

        Returns True if all chunks were patched successfully.
        """
        key = self._tile_key(tile_id, max_zoom)
        if key in self._healing_in_progress:
            return False
        self._healing_in_progress.add(key)
        try:
            return self._do_patch(
                tile_id, max_zoom, tile, chunk_jpegs, sources
            )
        finally:
            self._healing_in_progress.discard(key)

    def _do_patch(self, tile_id: str, max_zoom: int, tile,
                  chunk_jpegs: dict, sources: dict = None) -> bool:
        """Core in-place DXT block patching logic.

        For each missing chunk: decode JPEG → resize per mipmap → DXT compress
        → write strided blocks into the on-disk DDS and in-memory BytesIO buffers.
        After all chunks are patched, update the DDM sidecar to clear the
        ``needs_healing`` flag.

        Compressed files are decompressed into memory, patched, then
        recompressed and written back atomically.
        """
        dds_ref = tile.dds
        if dds_ref is None:
            return False

        chunks_per_row = getattr(tile, 'chunks_per_row', 0)
        if chunks_per_row == 0:
            return False

        dds_path, ddm_path = self._paths_for(
            tile.row, tile.col, tile.maptype,
            tile.tilename_zoom, max_zoom)

        blocksize = dds_ref.blocksize
        mm_list = dds_ref.mipmap_list
        patched = 0
        patched_ok = set()

        # Determine if the on-disk file is compressed
        meta = self._read_ddm(ddm_path)
        is_compressed = (meta or {}).get("disk_compression", "none") == "zstd"

        try:
            if is_compressed:
                # Decompress the entire file into a mutable bytearray
                with open(dds_path, "rb") as f:
                    raw = f.read()
                try:
                    dds_data = bytearray(self._decompress_dds(raw, meta))
                except Exception:
                    log.debug(f"Healing: decompression failed for {tile_id}")
                    return False

                for idx, jpeg_data in chunk_jpegs.items():
                    cx = idx % chunks_per_row
                    cy = idx // chunks_per_row

                    levels = self._patch_rgba_levels(jpeg_data)
                    if levels is None:
                        log.debug(f"Healing: JPEG decode failed for chunk {idx}")
                        continue

                    chunk_base_size = 256

                    for mm_idx, mm in enumerate(mm_list):
                        chunk_pixels = chunk_base_size >> mm_idx
                        if chunk_pixels < 4:
                            break

                        blocks_per_side = chunk_pixels // 4
                        mm_width = dds_ref.width >> mm_idx
                        if mm_width < 4:
                            break
                        mm_blocks_x = mm_width // 4
                        row_stride = mm_blocks_x * blocksize

                        block_x = cx * blocks_per_side
                        block_y = cy * blocks_per_side
                        stripe_bytes = blocks_per_side * blocksize

                        if mm_idx >= len(levels):
                            break
                        dxt_data = dds_ref.compress(chunk_pixels, chunk_pixels,
                                                    levels[mm_idx])
                        if dxt_data is None:
                            log.debug(f"Healing: DXT compress failed for chunk {idx} mm{mm_idx}")
                            continue

                        dxt_bytes_chunk = bytes(dxt_data)

                        for s in range(blocks_per_side):
                            file_offset = mm.startpos + (block_y + s) * row_stride + block_x * blocksize
                            stripe = dxt_bytes_chunk[s * stripe_bytes:(s + 1) * stripe_bytes]

                            # Write to in-memory bytearray (will be recompressed below)
                            dds_data[file_offset:file_offset + stripe_bytes] = stripe

                            # Write to in-memory mipmap buffer
                            if mm.buffer is not None:
                                mem_offset = file_offset - mm.startpos
                                mm.write_at(
                                    mem_offset,
                                    stripe,
                                    dds_ref.blocksize,
                                )

                    patched += 1
                    patched_ok.add(idx)

                read_row = lambda start, length: bytes(
                    dds_data[start:start + length]
                )

                # Recompress and write back atomically
                compressed = self._compress_dds(bytes(dds_data))
                tmp = dds_path + f".tmp.{os.getpid()}"
                with open(tmp, "wb") as f:
                    f.write(compressed)
                os.replace(tmp, dds_path)

            else:
                # Uncompressed: seek-write in place (original path)
                with open(dds_path, "r+b") as f:
                    for idx, jpeg_data in chunk_jpegs.items():
                        cx = idx % chunks_per_row
                        cy = idx // chunks_per_row

                        levels = self._patch_rgba_levels(jpeg_data)
                        if levels is None:
                            log.debug(f"Healing: JPEG decode failed for chunk {idx}")
                            continue

                        chunk_base_size = 256

                        for mm_idx, mm in enumerate(mm_list):
                            chunk_pixels = chunk_base_size >> mm_idx
                            if chunk_pixels < 4:
                                break

                            blocks_per_side = chunk_pixels // 4
                            mm_width = dds_ref.width >> mm_idx
                            if mm_width < 4:
                                break
                            mm_blocks_x = mm_width // 4
                            row_stride = mm_blocks_x * blocksize

                            block_x = cx * blocks_per_side
                            block_y = cy * blocks_per_side
                            stripe_bytes = blocks_per_side * blocksize

                            if mm_idx >= len(levels):
                                break
                            dxt_data = dds_ref.compress(chunk_pixels, chunk_pixels,
                                                        levels[mm_idx])
                            if dxt_data is None:
                                log.debug(f"Healing: DXT compress failed for chunk {idx} mm{mm_idx}")
                                continue

                            dxt_bytes_chunk = bytes(dxt_data)

                            for s in range(blocks_per_side):
                                file_offset = mm.startpos + (block_y + s) * row_stride + block_x * blocksize
                                stripe = dxt_bytes_chunk[s * stripe_bytes:(s + 1) * stripe_bytes]

                                f.seek(file_offset)
                                f.write(stripe)

                                if mm.buffer is not None:
                                    mem_offset = file_offset - mm.startpos
                                    mm.write_at(
                                        mem_offset,
                                        stripe,
                                        dds_ref.blocksize,
                                    )

                        patched += 1
                        patched_ok.add(idx)

                    f.flush()
                    with open(dds_path, "rb") as patched_file:
                        row_data = {}
                        for start, length in self._patched_row_ranges(
                            tile, patched_ok
                        ):
                            patched_file.seek(start)
                            row_data[start] = patched_file.read(length)
                    read_row = lambda start, length: row_data[start]

        except (FileNotFoundError, OSError) as e:
            log.debug(f"Healing: file I/O error for {tile_id}: {e}")
            return False

        target = int(MipmapSource.TARGET_PROVIDER)
        patched_sources = {
            idx: int((sources or {}).get(idx, target)) for idx in patched_ok
        }
        exact = {idx for idx, src in patched_sources.items() if src == target}
        remaining_missing = [i for i in (getattr(tile, '_dds_missing_indices', []) or [])
                             if i not in patched_ok]
        remaining_fallback = [i for i in (getattr(tile, '_dds_fallback_indices', []) or [])
                              if i not in exact]
        # Substitutes fill the hole but still need the real imagery.
        remaining_fallback.extend(
            sorted(set(patched_ok) - exact - set(remaining_fallback))
        )
        remaining_total = len(remaining_missing) + len(remaining_fallback)

        if patched == len(chunk_jpegs):
            # All requested chunks patched — check if anything remains
            meta = self._read_ddm(ddm_path)
            if meta is not None:
                self._rewrite_patched_manifests(
                    meta, tile, patched_sources, read_row
                )
                meta["needs_healing"] = remaining_total > 0
                meta["healing_chunks"] = remaining_total
                meta["missing_indices"] = remaining_missing
                meta["fallback_indices"] = remaining_fallback
                if meta.get("mipmaps"):
                    mm0 = meta["mipmaps"][0]
                    mm0["valid"] = mm0.get("total", 0) - len(remaining_missing)
                    mm0["complete"] = remaining_total == 0
                self._write_ddm(ddm_path, meta)

            tile._dds_needs_healing = remaining_total > 0
            tile._dds_missing_indices = remaining_missing
            tile._dds_fallback_indices = remaining_fallback
            log.info(f"Healing complete: {tile_id} ({patched} chunks patched)")

            # Clean up source JPEGs now that the DDS is fully healed
            if remaining_total == 0:
                self._cleanup_jpegs_async(tile)

            return True

        # Partial patch — update DDM with remaining indices
        meta = self._read_ddm(ddm_path)
        if meta is not None:
            self._rewrite_patched_manifests(
                meta, tile, patched_sources, read_row
            )
            meta["missing_indices"] = remaining_missing
            meta["fallback_indices"] = remaining_fallback
            meta["healing_chunks"] = remaining_total
            meta["needs_healing"] = remaining_total > 0
            if meta.get("mipmaps"):
                total = meta["mipmaps"][0].get("total", 0)
                meta["mipmaps"][0]["valid"] = total - len(remaining_missing)
                meta["mipmaps"][0]["complete"] = remaining_total == 0
            self._write_ddm(ddm_path, meta)

        tile._dds_missing_indices = remaining_missing
        tile._dds_fallback_indices = remaining_fallback
        tile._dds_needs_healing = remaining_total > 0
        log.info(f"Healing partial: {tile_id} ({patched}/{len(chunk_jpegs)} chunks patched, "
                 f"{remaining_total} remaining)")
        return False

    @staticmethod
    def _patch_rgba_levels(payload):
        """RGBA pixels of one 256 px chunk at every mipmap size down to 4 px.

        ``payload`` is JPEG bytes or raw 256x256 RGBA (healing substitutes
        are already decoded). Uses the bundled AoImage decoder and a 2x2 box
        filter, so healing does not depend on Pillow.
        """
        import numpy as np

        data = bytes(payload)
        if data[:3] == b"\xff\xd8\xff":
            try:
                from autoortho.aoimage import AoImage
            except ImportError:
                from aoimage import AoImage
            try:
                img = AoImage.load_from_memory(data)
            except Exception:
                img = None
            if img is None:
                return None
            try:
                if img.size != (256, 256):
                    return None
                data = img.tobytes()
            finally:
                img.close()
        if len(data) != 256 * 256 * 4:
            return None
        level = np.frombuffer(data, dtype=np.uint8).reshape(256, 256, 4)
        levels = []
        while level.shape[0] >= 4:
            levels.append(level.tobytes())
            half = level.shape[0] // 2
            level = (
                (
                    level.reshape(half, 2, half, 2, 4)
                    .astype(np.uint16)
                    .sum(axis=(1, 3))
                    + 2
                )
                // 4
            ).astype(np.uint8)
        return levels

    @staticmethod
    def _patched_row_ranges(tile, indices):
        """``(file_offset, length)`` of each mipmap-zero chunk row touched."""
        dds_ref = tile.dds
        rows_total = max(1, (dds_ref.height + 255) // 256)
        chunks_per_row = tile.chunks_per_row
        mm0 = dds_ref.mipmap_list[0]
        row_bytes = mm0.length // rows_total
        rows = sorted({idx // chunks_per_row for idx in indices})
        return [(mm0.startpos + row * row_bytes, row_bytes) for row in rows]

    def _rewrite_patched_manifests(self, meta, tile, patched_sources, read_row):
        """Record patched chunks in the mipmap-zero row manifests.

        Serving requires every row manifest to be exact, so without this a
        fully healed DDS would stay a cache miss forever. Checksums are
        recomputed from the patched bytes.
        """
        if not patched_sources:
            return
        try:
            tile_id = (
                f"{int(meta['tile_row'])}_{int(meta['tile_col'])}_"
                f"{meta['map']}_{int(meta['zl'])}"
            )
            manifests = manifests_from_dict(
                meta["mm0_manifest"], tile_id=tile_id
            )
            target_zoom = int(meta["mm0_manifest"]["target_zoom"])
        except (KeyError, TypeError, ValueError):
            return
        chunks_per_row = tile.chunks_per_row
        by_row = {}
        for idx, source in patched_sources.items():
            by_row.setdefault(idx // chunks_per_row, {})[
                idx % chunks_per_row
            ] = source
        ranges = dict(
            zip(
                sorted(by_row),
                self._patched_row_ranges(tile, patched_sources),
            )
        )
        for row, columns in by_row.items():
            old = manifests.get(row)
            if old is None:
                continue
            row_sources = bytearray(old.sources)
            for column, source in columns.items():
                if column < len(row_sources):
                    row_sources[column] = source
            start, length = ranges[row]
            manifests[row] = RowBuildManifest.create(
                tile_id=old.tile_id,
                target_zoom=old.target_zoom,
                mipmap=0,
                row_index=row,
                build_generation=old.build_generation,
                sources=row_sources,
                compressed_data=read_row(start, length),
            )
        meta["mm0_manifest"] = manifests_to_dict(
            manifests, target_zoom=target_zoom
        )

    @staticmethod
    def _chunk_coords(tile, idx: int, chunks_per_row: int):
        """Absolute max_zoom chunk coordinates of a mipmap-zero chunk index.

        ``tile.col``/``tile.row`` are in tile-name zoom units, which differ
        from max_zoom whenever the target zoom is capped or raised.
        """
        coords = getattr(tile, "_max_zoom_chunk_coords", None)
        if coords is not None:
            return coords(idx)
        return tile.col + idx % chunks_per_row, tile.row + idx // chunks_per_row

    @classmethod
    def _jpeg_exists_on_disk(cls, idx: int, tile, cache_dir: str,
                             chunks_per_row: int, max_zoom: int) -> bool:
        """Check whether a full-resolution JPEG exists on disk for a chunk index."""
        col, row = cls._chunk_coords(tile, idx, chunks_per_row)
        jpeg_path = os.path.join(
            cache_dir,
            f"{col}_{row}_{max_zoom}_{tile.maptype}.jpg")
        return chunk_store.chunk_path_exists(jpeg_path)

    def _try_heal_from_disk_cache(self, tile_id: str, max_zoom: int, tile) -> List[int]:
        """Check if healable chunk JPEGs exist on disk and dispatch healing.

        Missing chunks are checked first (all-or-nothing). Fallback chunks
        are checked individually (best-effort) so missing-chunk healing is
        never delayed by unavailable fallback JPEGs.

        Returns list of chunk indices that could NOT be healed from disk
        (candidates for network healing).
        """
        missing = getattr(tile, '_dds_missing_indices', [])
        fallback = getattr(tile, '_dds_fallback_indices', [])
        all_indices = list(missing) + list(fallback)
        if not all_indices:
            return []

        jpeg_cache_dir = getattr(tile, 'cache_dir', None)
        if not jpeg_cache_dir:
            return all_indices

        chunks_per_row = getattr(tile, 'chunks_per_row', 0)
        if not chunks_per_row:
            return all_indices

        healable = []

        # Missing chunks: best-effort (heal whatever JPEGs are available on disk).
        # Previously used all-or-nothing semantics which discarded ALL healable
        # chunks if even one JPEG was missing, causing green patches to persist
        # indefinitely even when most chunks could be healed.
        for idx in missing:
            if self._jpeg_exists_on_disk(idx, tile, jpeg_cache_dir, chunks_per_row, max_zoom):
                healable.append(idx)

        # Fallback chunks: best-effort (patch whichever have JPEGs on disk)
        for idx in fallback:
            if self._jpeg_exists_on_disk(idx, tile, jpeg_cache_dir, chunks_per_row, max_zoom):
                healable.append(idx)

        unhealable = [i for i in all_indices if i not in healable]

        if healable:
            log.debug(f"DDS cache: {len(healable)} healable JPEGs on disk for {tile_id}, "
                      f"dispatching cross-session healing")
            t = threading.Thread(
                target=self._heal_from_disk,
                args=(tile_id, max_zoom, tile, healable),
                daemon=True)
            t.start()

        return unhealable

    def _dispatch_healing_for_incomplete(self, tile_id: str, max_zoom: int, tile) -> None:
        """Start incomplete-DDS healing without delaying the cache miss path."""
        all_indices = list(getattr(tile, '_dds_missing_indices', []) or [])
        all_indices.extend(getattr(tile, '_dds_fallback_indices', []) or [])
        if not all_indices:
            return

        key = self._tile_key(tile_id, max_zoom)
        with self._lock:
            if key in self._network_healing_in_progress:
                return
            self._network_healing_in_progress.add(key)

        t = threading.Thread(
            target=self._heal_incomplete_async,
            args=(tile_id, max_zoom, tile),
            daemon=True,
            name=f"dds_heal_{tile_id}")
        t.start()

    def _heal_incomplete_async(self, tile_id: str, max_zoom: int, tile) -> None:
        """Background healing coordinator for strict cache misses."""
        network_dispatched = False
        try:
            unhealable = self._try_heal_from_disk_cache(tile_id, max_zoom, tile)
            if unhealable and self._network_heal_callback:
                log.debug(f"DDS cache: dispatching network healing for {tile_id} "
                          f"({len(unhealable)} chunks)")
                try:
                    self._network_heal_callback(tile, unhealable)
                    network_dispatched = True
                except Exception as e:
                    log.debug(f"DDS cache: network healing dispatch failed: {e}")
        finally:
            if not network_dispatched:
                key = self._tile_key(tile_id, max_zoom)
                with self._lock:
                    self._network_healing_in_progress.discard(key)

    def _heal_from_disk(self, tile_id: str, max_zoom: int, tile,
                        missing: List[int]) -> None:
        """Read missing JPEGs from disk cache and patch the DDS.

        Full patching logic implemented in Phase 3.
        """
        chunks_per_row = getattr(tile, 'chunks_per_row', 0)
        if not chunks_per_row:
            return
        chunk_jpegs = {}

        for idx in missing:
            col, row = self._chunk_coords(tile, idx, chunks_per_row)
            jpeg_path = os.path.join(
                tile.cache_dir,
                f"{col}_{row}_{max_zoom}_{tile.maptype}.jpg")
            data = chunk_store.read_chunk_path(jpeg_path)
            if not data:
                log.debug(f"Healing: JPEG missing for chunk {idx} of {tile_id}")
                return
            chunk_jpegs[idx] = data

        self.patch_missing_chunks(tile_id, max_zoom, tile, chunk_jpegs)

    def clear_network_healing(self, tile_id: str, max_zoom: int) -> None:
        """Remove a tile from the network healing in-progress set.

        Called by the healing thread in getortho.py when it finishes
        (success or failure) so the tile can be re-healed on next load.
        """
        key = self._tile_key(tile_id, max_zoom)
        with self._lock:
            self._network_healing_in_progress.discard(key)

    @staticmethod
    def _delete_pair(dds_path: str, ddm_path: str) -> None:
        """Delete a DDS entry and its partial-row records, if any."""
        for path in (dds_path, ddm_path,
                     DynamicDDSCache._row_records_path(ddm_path)):
            try:
                os.remove(path)
            except OSError:
                pass

    def _cleanup_jpegs_async(self, tile) -> None:
        """Schedule JPEG cleanup for a tile whose DDS is now complete."""
        if not self._cleanup_source_jpegs_after_store:
            return
        jpeg_cache_dir = getattr(tile, 'cache_dir', None)
        if not jpeg_cache_dir:
            return
        self._submit_maintenance(
            cleanup_source_jpegs,
            jpeg_cache_dir,
            tile.col,
            tile.row,
            tile.tilename_zoom,
            tile.max_zoom,
            getattr(tile, 'min_zoom', 12),
            getattr(tile, 'width', 1),
            getattr(tile, 'height', 1),
            tile.maptype,
            budget_manager=self._budget_manager,
        )

    def _evict_lru_async(self) -> None:
        """Schedule LRU eviction in a background thread."""
        # Evict down to 90% of budget (hysteresis)
        target = int(self._max_size * 0.9)
        excess = self._current_size - target
        if excess <= 0:
            return
        self._submit_maintenance(self.evict_lru, excess)

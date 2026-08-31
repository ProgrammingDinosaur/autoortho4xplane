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
import hashlib
import logging
import os
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import List, Optional, Tuple

try:
    from autoortho.diagnostics import profiled_stage
except ImportError:
    from diagnostics import profiled_stage

try:
    import zstandard
    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

log = logging.getLogger(__name__)

# Current DDM schema version. Bump when the metadata format changes
# in a backwards-incompatible way.
# v2 -> v3: added "populated_mipmaps" for incremental DDS persistence
# v3 -> v4: adds crash-safe, row-addressable partial mipmap-0 persistence.
DDM_VERSION = 4
_SUPPORTED_DDM_VERSIONS = frozenset((2, 3, DDM_VERSION))

# A row record is deliberately endian-explicit and contains no native-sized
# values, so caches can be moved between Windows and POSIX installations.
_ROW_RECORD_MAGIC = b"DDRR"
_ROW_RECORD_SCHEMA = 1
_ROW_RECORD_HEADER = struct.Struct(">4sBIQI32s")
_MAX_ROW_RECORD_SIZE = 128 * 1024 * 1024


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

        for r in range(scaled_row, scaled_row + scaled_height):
            for c in range(scaled_col, scaled_col + scaled_width):
                jpeg_path = os.path.join(cache_dir, f"{c}_{r}_{zoom}_{maptype}.jpg")
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
        if version != DDM_VERSION:
            return True

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
        return True

    @staticmethod
    def _build_ddm(tile, max_zoom: int,
                   dds_format: str, compressor: str,
                   mm0_missing_indices: Optional[List[int]] = None,
                   mm0_fallback_indices: Optional[List[int]] = None,
                   disk_compression: str = "none") -> dict:
        """Build a DDM v4 metadata dict from a tile and current config."""
        dds_ref = tile.dds
        width = dds_ref.width if dds_ref else 0
        height = dds_ref.height if dds_ref else 0
        mm_count = dds_ref.mipMapCount if dds_ref else 0

        missing = mm0_missing_indices or []
        fallback = mm0_fallback_indices or []
        total_chunks = getattr(tile, 'chunks_per_row', 0) ** 2

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
        }

    @staticmethod
    def _build_ddm_incremental(row: int, col: int, maptype: str,
                               tilename_zoom: int, max_zoom: int,
                               width: int, height: int, mm_count: int,
                               dds_format: str, compressor: str,
                               populated_mipmaps: List[int],
                               disk_compression: str = "none",
                               partial_mipmaps: Optional[dict] = None) -> dict:
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

            tile._dds_needs_healing = False
            tile._dds_missing_indices = []
            tile._dds_fallback_indices = []

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

    @profiled_stage("cache.dds_metadata", tile_arg=1)
    def load_metadata(self, tile_id: str, max_zoom: int, tile) -> Optional[dict]:
        """Read DDM metadata without loading the DDS file.

        Returns the parsed DDM dict or None if no entry exists.
        Useful for checking ``needs_healing`` without the ~11 MB file read.
        """
        if not self._enabled:
            return None
        try:
            _, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom)
            return self._read_ddm(ddm_path)
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
                           *, degraded: bool = False,
                           revision: Optional[int] = None) -> bool:
        """Append one mipmap-0 chunk row and atomically expose it in DDM v4.

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
        if len(row_data) > _MAX_ROW_RECORD_SIZE:
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
                row_revision = current_revision + 1 if revision is None else revision
                if not isinstance(row_revision, int) or row_revision < 0:
                    return False
                if row_revision < entry["row_revisions"].get(str(row_index), -1):
                    return True

                self._append_row_record(
                    records_path, row_index, row_revision, row_data)
                covered = set(entry["covered"])
                covered.add(row_index)
                entry["covered"] = sorted(covered)
                degraded_rows = set(entry["degraded"])
                if degraded:
                    degraded_rows.add(row_index)
                else:
                    degraded_rows.discard(row_index)
                entry["degraded"] = sorted(degraded_rows)
                entry["revision"] = max(entry["revision"], row_revision)
                entry["row_revisions"][str(row_index)] = row_revision

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
            return {
                row: record[1] for row, record in records.items()
                if (str(row) in referenced and
                    referenced[str(row)] == record[0])
            }
        except (OSError, ValueError, TypeError):
            return {}

    def enqueue_partial_row(self, tile_id: str, max_zoom: int, tile,
                            row_index: int, data: bytes, total: int,
                            *, degraded: bool = False,
                            revision: Optional[int] = None) -> bool:
        """Queue a row for non-blocking, coalesced background persistence."""
        if not self._enabled or self._partial_row_queue_limit <= 0:
            return False
        try:
            row_data = bytes(data)
        except (TypeError, ValueError):
            return False
        if (not isinstance(row_index, int) or not isinstance(total, int) or
                total <= 0 or row_index < 0 or row_index >= total or
                len(row_data) > _MAX_ROW_RECORD_SIZE):
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
                "data": row_data, "total": total, "degraded": degraded,
                "revision": revision,
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
            for row_index, item in pending["rows"].items():
                if self.append_partial_row(
                        _key[0], _key[1], pending["tile"], row_index,
                        item["data"], item["total"], degraded=item["degraded"],
                        revision=item["revision"]):
                    with self._partial_row_condition:
                        self._partial_row_persisted += 1
            with self._partial_row_condition:
                self._partial_row_active = False
                self._partial_row_condition.notify_all()

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

        The file is extended to ``total_size`` via ``truncate()``, which is a
        metadata-only operation on NTFS/ext4/APFS (sparse allocation -- no
        physical zero-writes for the unwritten region).

        Returns True on success.
        """
        tmp = dds_path + f".tmp.{os.getpid()}"
        try:
            with open(tmp, "wb") as f:
                f.write(header_bytes)
                f.truncate(total_size)
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

    @profiled_stage("cache.dds_store", tile_arg=1)
    def store(self, tile_id: str, max_zoom: int, dds_bytes: bytes,
              tile,
              mm0_missing_indices: Optional[List[int]] = None,
              mm0_fallback_indices: Optional[List[int]] = None) -> bool:
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

        if not dds_bytes or len(dds_bytes) < 128:
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
            os.replace(tmp_dds, dds_path)

            # Build and write DDM metadata atomically
            mm0_missing_indices = self._filter_healable_missing(
                mm0_missing_indices, tile, max_zoom)
            meta = self._build_ddm(tile, max_zoom,
                                   dds_format, compressor,
                                   mm0_missing_indices=mm0_missing_indices,
                                   mm0_fallback_indices=mm0_fallback_indices,
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

            if not mm0_missing_indices and not mm0_fallback_indices:
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
            if (existing_meta is not None and
                    existing_meta.get("v") == DDM_VERSION and
                    0 not in new_mipmaps):
                partial_mipmaps = existing_meta.get("partial_mipmaps", {})
            meta = self._build_ddm_incremental(
                row, col, maptype, tilename_zoom, max_zoom,
                width, height, mm_count, dds_format, compressor,
                merged_populated, disk_compression=disk_compression,
                partial_mipmaps=partial_mipmaps,
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
                        mm0_fallback_indices: Optional[List[int]] = None) -> bool:
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

        try:
            source_size = os.path.getsize(source_path)
        except OSError:
            return False

        if source_size < 128:
            return False

        try:
            dds_path, ddm_path = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom
            )

            # Ensure directory exists
            os.makedirs(os.path.dirname(dds_path), exist_ok=True)

            # Atomic placement: create at a temp name, then os.replace().
            tmp_dds = dds_path + f".tmp.{os.getpid()}"

            if self._compression == "zstd" and _HAS_ZSTD:
                # Read source, compress, write compressed version
                with open(source_path, "rb") as f:
                    raw_bytes = f.read()
                disk_bytes = self._compress_dds(raw_bytes)
                disk_compression = self._compression if len(disk_bytes) < len(raw_bytes) else "none"
                if disk_compression == "none":
                    disk_bytes = raw_bytes
                with open(tmp_dds, "wb") as f:
                    f.write(disk_bytes)
                disk_size = len(disk_bytes)
            else:
                disk_compression = "none"
                # Try hard-link first -- zero-copy, instant, works when source
                # and destination are on the same filesystem.
                linked = False
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

            os.replace(tmp_dds, dds_path)

            # Write DDM metadata and retire any superseded partial rows.
            dds_format, compressor = self._get_format_and_compressor()
            mm0_missing_indices = self._filter_healable_missing(
                mm0_missing_indices, tile, max_zoom)
            meta = self._build_ddm(tile, max_zoom,
                                   dds_format, compressor,
                                   mm0_missing_indices=mm0_missing_indices,
                                   mm0_fallback_indices=mm0_fallback_indices,
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
                   new_mm0_bytes: bytes, tile) -> Optional[bytes]:
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
            if self.store(tile_id, new_max_zoom, new_dds_bytes, tile):
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
                     tile) -> Optional[bytes]:
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

            if self.store(tile_id, new_max_zoom, new_dds_bytes, tile):
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
            while freed < bytes_to_free and self._entries:
                key, (dds_path, ddm_path, size, _) = self._entries.popitem(last=False)
                self._current_size -= size
                freed += size
                self._evictions += 1
                to_delete.append((dds_path, ddm_path))

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
                             chunk_jpegs: dict) -> bool:
        """In-place patch missing chunks into an existing DDS file and
        in-memory buffer.  Full implementation in Phase 3.

        Returns True if all chunks were patched successfully.
        """
        key = self._tile_key(tile_id, max_zoom)
        if key in self._healing_in_progress:
            return False
        self._healing_in_progress.add(key)
        try:
            return self._do_patch(tile_id, max_zoom, tile, chunk_jpegs)
        finally:
            self._healing_in_progress.discard(key)

    def _do_patch(self, tile_id: str, max_zoom: int, tile,
                  chunk_jpegs: dict) -> bool:
        """Core in-place DXT block patching logic.

        For each missing chunk: decode JPEG → resize per mipmap → DXT compress
        → write strided blocks into the on-disk DDS and in-memory BytesIO buffers.
        After all chunks are patched, update the DDM sidecar to clear the
        ``needs_healing`` flag.

        Compressed files are decompressed into memory, patched, then
        recompressed and written back atomically.
        """
        from io import BytesIO
        try:
            from PIL import Image
        except ImportError:
            log.debug(f"Healing: PIL not available, cannot patch {tile_id}")
            return False

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

                    try:
                        rgba_img = Image.open(BytesIO(jpeg_data)).convert("RGBA")
                    except Exception:
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

                        if mm_idx == 0:
                            resized = rgba_img.resize((chunk_pixels, chunk_pixels), Image.LANCZOS) \
                                if rgba_img.size != (chunk_pixels, chunk_pixels) else rgba_img
                        else:
                            resized = rgba_img.resize((chunk_pixels, chunk_pixels), Image.LANCZOS)

                        dxt_data = dds_ref.compress(chunk_pixels, chunk_pixels,
                                                    resized.tobytes())
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

                        try:
                            rgba_img = Image.open(BytesIO(jpeg_data)).convert("RGBA")
                        except Exception:
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

                            if mm_idx == 0:
                                resized = rgba_img.resize((chunk_pixels, chunk_pixels), Image.LANCZOS) \
                                    if rgba_img.size != (chunk_pixels, chunk_pixels) else rgba_img
                            else:
                                resized = rgba_img.resize((chunk_pixels, chunk_pixels), Image.LANCZOS)

                            dxt_data = dds_ref.compress(chunk_pixels, chunk_pixels,
                                                        resized.tobytes())
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

                    f.flush()

        except (FileNotFoundError, OSError) as e:
            log.debug(f"Healing: file I/O error for {tile_id}: {e}")
            return False

        patched_set = set(chunk_jpegs.keys())
        remaining_missing = [i for i in (getattr(tile, '_dds_missing_indices', []) or [])
                             if i not in patched_set]
        remaining_fallback = [i for i in (getattr(tile, '_dds_fallback_indices', []) or [])
                              if i not in patched_set]
        remaining_total = len(remaining_missing) + len(remaining_fallback)

        if patched == len(chunk_jpegs):
            # All requested chunks patched — check if anything remains
            meta = self._read_ddm(ddm_path)
            if meta is not None:
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
    def _jpeg_exists_on_disk(idx: int, tile, cache_dir: str,
                             chunks_per_row: int, max_zoom: int) -> bool:
        """Check whether a full-resolution JPEG exists on disk for a chunk index."""
        cx = idx % chunks_per_row
        cy = idx // chunks_per_row
        col = tile.col + cx
        row = tile.row + cy
        jpeg_path = os.path.join(
            cache_dir,
            f"{col}_{row}_{max_zoom}_{tile.maptype}.jpg")
        return os.path.exists(jpeg_path)

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
            cx = idx % chunks_per_row
            cy = idx // chunks_per_row
            col = tile.col + cx
            row = tile.row + cy
            jpeg_path = os.path.join(
                tile.cache_dir,
                f"{col}_{row}_{max_zoom}_{tile.maptype}.jpg")
            try:
                with open(jpeg_path, "rb") as f:
                    chunk_jpegs[idx] = f.read()
            except (FileNotFoundError, OSError):
                log.debug(f"Healing: JPEG missing for chunk {idx} of {tile_id}")
                return

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

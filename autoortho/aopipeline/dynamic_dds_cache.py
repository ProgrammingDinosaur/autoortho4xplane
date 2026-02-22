"""
dynamic_dds_cache.py - Persistent DDS cache for AutoOrtho

Stores pre-built DDS textures on disk across sessions so that subsequent
loads skip the expensive JPEG-decode + DXT-compress pipeline entirely.

Bundles remain the source of truth. The DDS cache is purely derived and
can always be regenerated from bundles.

Key features:
- Persistent across sessions (unlike EphemeralDDSCache)
- Staleness detection via DDM metadata sidecar files
- ZL upgrade support with mipmap shifting (reuses existing mipmaps)
- LRU eviction when disk budget is exceeded
- Atomic file writes (temp + os.replace) for crash safety
- Thread-safe with minimal lock contention
"""

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import List, Optional, Tuple

try:
    import zstandard
    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

log = logging.getLogger(__name__)

# Current DDM schema version. Bump when the metadata format changes
# in a backwards-incompatible way.
# v2 -> v3: added "populated_mipmaps" for incremental DDS persistence
DDM_VERSION = 3


def cleanup_source_jpegs(cache_dir: str, col: int, row: int,
                         tilename_zoom: int, max_zoom: int, min_zoom: int,
                         width: int, height: int, maptype: str) -> int:
    """Delete source JPEG chunks after a complete DDS is stored.

    Enumerates chunk files at every zoom level used by the tile's mipmaps
    (max_zoom down to min_zoom) and deletes them.  Coordinate scaling
    mirrors ``_get_quick_zoom()``.

    Returns the number of files successfully deleted.
    """
    deleted = 0
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
                        os.remove(jpeg_path)
                        deleted += 1
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
    return deleted


class DynamicDDSCache:
    """
    Persistent disk cache for pre-built DDS textures.
    
    Sits between the FUSE layer and the build pipeline as a compiled-output
    cache. On a warm start, tiles are served from disk (~1-2ms) instead of
    being rebuilt from bundles (~390ms per tile).
    
    Thread Safety:
        A single ``threading.Lock`` protects the LRU metadata dict. File I/O
        is performed outside the lock when possible. Atomic writes (temp file
        + ``os.replace``) prevent corruption from concurrent access or crashes.
    """

    def __init__(self, cache_dir: str, max_size_mb: int = 4096, enabled: bool = True):
        """
        Args:
            cache_dir: Base cache directory (same as CFG.paths.cache_dir).
            max_size_mb: Maximum disk usage in MB for the DDS cache.
                         Set to 0 to disable caching.
            enabled: Master enable flag. When False, all methods are no-ops.
        """
        self._cache_dir = cache_dir
        self._dds_root = os.path.join(cache_dir, "dds_cache")
        self._max_size = max_size_mb * 1024 * 1024  # bytes
        self._enabled = enabled and max_size_mb > 0
        self._current_size = 0

        # LRU tracking: tile_key -> (dds_path, ddm_path, size, last_access)
        # Ordered from oldest to newest access.
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

        # In-flight healing guard (prevents duplicate patch work)
        self._healing_in_progress: set = set()

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
            log.info(f"DynamicDDSCache initialized: {self._dds_root} "
                     f"(max={max_size_mb}MB, compression={self._compression})")

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _tile_key(self, tile_id: str, max_zoom: int) -> str:
        """Unique key for LRU tracking."""
        return f"{tile_id}_z{max_zoom}"

    def _paths_for(self, row: int, col: int, maptype: str,
                   tilename_zoom: int, max_zoom: int) -> Tuple[str, str]:
        """Return (dds_path, ddm_path) for a tile.
        
        Uses the same DSF-based directory hierarchy as bundle_paths.py.
        """
        try:
            from autoortho.utils.bundle_paths import get_dds_cache_path
        except ImportError:
            from utils.bundle_paths import get_dds_cache_path

        base = get_dds_cache_path(
            self._cache_dir, row, col, maptype, tilename_zoom, max_zoom
        )
        return base + ".dds", base + ".ddm"

    # ------------------------------------------------------------------
    # DDM metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_ddm(tile, max_zoom: int, bundle_mtime: Optional[float],
                   dds_format: str, compressor: str,
                   mm0_missing_indices: Optional[List[int]] = None,
                   disk_compression: str = "none") -> dict:
        """Build a DDM v3 metadata dict from a tile and current config."""
        dds_ref = tile.dds
        width = dds_ref.width if dds_ref else 0
        height = dds_ref.height if dds_ref else 0
        mm_count = dds_ref.mipMapCount if dds_ref else 0

        missing = mm0_missing_indices or []
        total_chunks = getattr(tile, 'chunks_per_row', 0) ** 2

        mipmaps = []
        for i in range(mm_count):
            mm_entry = {"zl": max_zoom - i if i < (max_zoom - 11) else 12, "complete": True}
            if i == 0 and total_chunks > 0:
                mm_entry["total"] = total_chunks
                mm_entry["valid"] = total_chunks - len(missing)
                mm_entry["complete"] = len(missing) == 0
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
            "bundle_mtime": bundle_mtime or 0.0,
            "built": time.time(),
            "tile_row": tile.row,
            "tile_col": tile.col,
            "mipmaps": mipmaps,
            "populated_mipmaps": list(range(mm_count)),
            "needs_healing": len(missing) > 0,
            "healing_chunks": len(missing),
            "missing_indices": missing,
            "disk_compression": disk_compression,
        }

    @staticmethod
    def _build_ddm_incremental(row: int, col: int, maptype: str,
                               tilename_zoom: int, max_zoom: int,
                               width: int, height: int, mm_count: int,
                               dds_format: str, compressor: str,
                               populated_mipmaps: List[int]) -> dict:
        """Build a lightweight DDM v3 dict for incremental saves.

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
            "bundle_mtime": 0.0,
            "built": time.time(),
            "tile_row": row,
            "tile_col": col,
            "mipmaps": mipmaps,
            "populated_mipmaps": sorted(populated_mipmaps),
            "needs_healing": False,
            "healing_chunks": 0,
            "missing_indices": [],
            "disk_compression": "none",
        }

    @staticmethod
    def _write_ddm(ddm_path: str, meta: dict) -> None:
        """Write DDM metadata atomically."""
        tmp = ddm_path + f".tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, separators=(",", ":"))
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
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    # ------------------------------------------------------------------
    # Staleness detection
    # ------------------------------------------------------------------

    def _is_stale(self, meta: dict, tile, dds_path: str) -> bool:
        """Check if a cached DDS entry is stale and needs rebuilding.
        
        NOTE: Does NOT check ZL mismatch. ZL upgrades are handled separately
        in load() via find_upgrade_candidate() to enable mipmap shifting.
        
        Staleness rules:
        1. bundle_mtime < actual bundle mtime -> bundle updated
        2. fmt != current DXT format -> config changed
        3. comp != current compressor -> config changed
        4. File size mismatch vs expected -> corruption
        """
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG  # type: ignore[no-redef]

        # Rule 2: DXT format changed
        current_fmt = CFG.pydds.format.upper()
        if current_fmt in ("DXT1",):
            current_fmt = "BC1"
        elif current_fmt in ("DXT5",):
            current_fmt = "BC3"
        if meta.get("fmt") != current_fmt:
            log.debug(f"DDS stale: format changed ({meta.get('fmt')} -> {current_fmt})")
            return True

        # Rule 3: Compressor changed
        current_comp = CFG.pydds.compressor.upper()
        if meta.get("comp") != current_comp:
            log.debug(f"DDS stale: compressor changed ({meta.get('comp')} -> {current_comp})")
            return True

        # Rule 4: File size validation (uncompressed files only)
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

        # Rule 1: Bundle mtime check
        bundle_mtime = meta.get("bundle_mtime", 0.0)
        if bundle_mtime > 0:
            try:
                from autoortho.utils.bundle_paths import get_bundle2_path
            except ImportError:
                from utils.bundle_paths import get_bundle2_path  # type: ignore[no-redef]
            bundle_path = get_bundle2_path(
                self._cache_dir, tile.row, tile.col,
                tile.maptype, tile.tilename_zoom
            )
            try:
                actual_mtime = os.path.getmtime(bundle_path)
                if actual_mtime > bundle_mtime:
                    log.debug(f"DDS stale: bundle updated ({bundle_mtime} < {actual_mtime})")
                    return True
            except OSError:
                # Bundle doesn't exist - DDS is orphaned but still usable
                # (the data was correct when it was built)
                pass

        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

            # Check if DDM exists for the requested ZL
            meta = self._read_ddm(ddm_path)
            if meta is None:
                # No entry at requested ZL. Check for a lower-ZL upgrade candidate.
                self._check_upgrade_candidate(tile_id, max_zoom, tile)
                self._misses += 1
                return None

            # Staleness checks (excludes ZL mismatch, handled separately)
            if self._is_stale(meta, tile, dds_path):
                self._delete_pair(dds_path, ddm_path)
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
                    self._delete_pair(dds_path, ddm_path)
                self._misses += 1
                return None

            # Read the DDS file (possibly compressed on disk)
            try:
                with open(dds_path, "rb") as f:
                    raw_bytes = f.read()
            except (FileNotFoundError, OSError):
                self._delete_pair(dds_path, ddm_path)
                self._misses += 1
                return None

            # Decompress if the file was stored compressed
            try:
                dds_bytes = self._decompress_dds(raw_bytes, meta)
            except Exception:
                log.debug(f"DDS cache: decompression failed for {tile_id}, removing")
                self._delete_pair(dds_path, ddm_path)
                self._misses += 1
                return None

            # Validate size (against uncompressed DDS dimensions)
            if tile.dds is not None and len(dds_bytes) != tile.dds.total_size:
                log.debug(f"DDS cache: size mismatch for {tile_id} "
                          f"({len(dds_bytes)} vs {tile.dds.total_size})")
                self._delete_pair(dds_path, ddm_path)
                self._misses += 1
                return None

            # DDM v2: healing detection (v1 DDMs lack this field → default False)
            if meta.get("needs_healing", False):
                tile._dds_needs_healing = True
                tile._dds_missing_indices = meta.get("missing_indices", [])
                log.debug(f"DDS cache: serving incomplete tile {tile_id} "
                          f"({len(tile._dds_missing_indices)} chunks need healing)")
                self._try_heal_from_disk_cache(tile_id, max_zoom, tile)

            # DDM v3: partial DDS awareness -- tell the tile which mipmaps
            # actually contain data so _populate_dds_from_prebuilt() can
            # skip unpopulated slots (avoids allocating zero-filled buffers).
            populated = meta.get("populated_mipmaps")
            if populated is not None:
                tile._dds_populated_mipmaps = set(populated)
            else:
                tile._dds_populated_mipmaps = None  # v2 compat: all populated

            # Update LRU tracking
            key = self._tile_key(tile_id, max_zoom)
            with self._lock:
                size = len(dds_bytes)
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

    def contains(self, tile_id: str, max_zoom: int, tile) -> bool:
        """Check if a tile exists in the cache (stat-only, no data load)."""
        if not self._enabled:
            return False
        try:
            dds_path, _ = self._paths_for(
                tile.row, tile.col, tile.maptype,
                tile.tilename_zoom, max_zoom)
            return os.path.exists(dds_path)
        except Exception:
            return False

    def get_staging_path(self, tile_id: str, max_zoom: int, tile) -> str:
        """Get a temp file path for native direct-to-disk writes.

        After the native C code writes to this path, call
        ``store_from_file()`` to atomically move it into the cache.
        """
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

    def store(self, tile_id: str, max_zoom: int, dds_bytes: bytes,
              tile, bundle_path: Optional[str] = None,
              mm0_missing_indices: Optional[List[int]] = None) -> bool:
        """
        Store a DDS build result in the persistent cache.
        
        Writes the DDS file and its DDM metadata sidecar atomically.
        
        Args:
            tile_id: Tile identifier
            max_zoom: Effective maximum zoom level
            dds_bytes: Complete DDS file bytes (header + mipmaps)
            tile: Tile object
            bundle_path: Optional path to the source bundle for mtime tracking
            mm0_missing_indices: Flat chunk indices missing at build time.
                None means "assume complete".
        
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

            # Get bundle mtime if path provided
            bundle_mtime = None
            if bundle_path:
                try:
                    bundle_mtime = os.path.getmtime(bundle_path)
                except OSError:
                    pass

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
            meta = self._build_ddm(tile, max_zoom, bundle_mtime,
                                   dds_format, compressor,
                                   mm0_missing_indices=mm0_missing_indices,
                                   disk_compression=disk_compression)
            self._write_ddm(ddm_path, meta)

            # Update LRU tracking (use on-disk size for accurate budget)
            key = self._tile_key(tile_id, max_zoom)
            size = len(disk_bytes)
            with self._lock:
                if key in self._entries:
                    old_size = self._entries[key][2]
                    self._current_size -= old_size
                self._entries[key] = (dds_path, ddm_path, size, time.time())
                self._entries.move_to_end(key)
                self._current_size += size
                self._stores += 1

            log.debug(f"DDS cache STORE: {tile_id} z{max_zoom} ({size} bytes)")

            if not mm0_missing_indices:
                self._cleanup_jpegs_async(tile)

            return True

        except Exception as e:
            log.debug(f"DDS cache store error for {tile_id}: {e}")
            # Clean up temp files on failure
            try:
                os.remove(dds_path + f".tmp.{os.getpid()}")
            except OSError:
                pass
            return False

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

            # 3. Create skeleton if DDS file doesn't exist
            if not os.path.isfile(dds_path):
                self._create_dds_skeleton(dds_path, header_bytes, total_size)

            # 4. Seek-write each mipmap's data
            with open(dds_path, "r+b") as f:
                for idx, data in new_mipmaps.items():
                    startpos, length = mipmap_offsets[idx]
                    if len(data) != length:
                        log.debug(f"Incremental save: mipmap {idx} size mismatch "
                                  f"({len(data)} vs {length}), skipping")
                        continue
                    f.seek(startpos)
                    f.write(data)

            # 5. Update DDM *after* data writes (crash-safe ordering)
            merged_populated = sorted(already_populated | set(new_mipmaps.keys()))

            dds_format, compressor = self._get_format_and_compressor()
            meta = self._build_ddm_incremental(
                row, col, maptype, tilename_zoom, max_zoom,
                width, height, mm_count, dds_format, compressor,
                merged_populated
            )
            self._write_ddm(ddm_path, meta)

            # 6. LRU tracking
            key = self._tile_key(tile_id, max_zoom)
            try:
                size = os.path.getsize(dds_path)
            except OSError:
                size = total_size
            with self._lock:
                if key in self._entries:
                    old_size = self._entries[key][2]
                    self._current_size -= old_size
                self._entries[key] = (dds_path, ddm_path, size, time.time())
                self._entries.move_to_end(key)
                self._current_size += size
                self._stores += 1

            log.debug(f"DDS cache STORE_INCR: {tile_id} z{max_zoom} "
                      f"mipmaps={sorted(new_mipmaps.keys())} "
                      f"populated={merged_populated}")

            return True

        except Exception as e:
            log.debug(f"DDS cache incremental store error for {tile_id}: {e}")
            return False

    def store_from_file(self, tile_id: str, max_zoom: int,
                        source_path: str, tile,
                        mm0_missing_indices: Optional[List[int]] = None) -> bool:
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

            # Write DDM metadata
            dds_format, compressor = self._get_format_and_compressor()
            meta = self._build_ddm(tile, max_zoom, None, dds_format, compressor,
                                   mm0_missing_indices=mm0_missing_indices,
                                   disk_compression=disk_compression)
            self._write_ddm(ddm_path, meta)

            # Update LRU tracking (use on-disk size for accurate budget)
            key = self._tile_key(tile_id, max_zoom)
            with self._lock:
                if key in self._entries:
                    old_size = self._entries[key][2]
                    self._current_size -= old_size
                self._entries[key] = (dds_path, ddm_path, disk_size, time.time())
                self._entries.move_to_end(key)
                self._current_size += disk_size
                self._stores += 1

            log.debug(f"DDS cache STORE (from file): {tile_id} z{max_zoom} "
                      f"({disk_size} bytes, compression={disk_compression})")

            if not mm0_missing_indices:
                self._cleanup_jpegs_async(tile)

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
                self._delete_pair(old_dds_path, old_ddm_path)
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
                self._delete_pair(old_dds_path, old_ddm_path)
                old_key = self._tile_key(tile_id, old_max_zoom)
                with self._lock:
                    if old_key in self._entries:
                        old_size = self._entries[old_key][2]
                        del self._entries[old_key]
                        self._current_size -= old_size
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
                self._delete_pair(old_dds_path, old_ddm_path)
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
                self._delete_pair(old_dds_path, old_ddm_path)
                old_key = self._tile_key(tile_id, old_max_zoom)
                with self._lock:
                    if old_key in self._entries:
                        old_size = self._entries[old_key][2]
                        del self._entries[old_key]
                        self._current_size -= old_size

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
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._current_size -= entry[2]
                dds_path, ddm_path = entry[0], entry[1]
            else:
                return False

        self._delete_pair(dds_path, ddm_path)
        return True

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
            return 0

        count = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(self._dds_root):
                for fname in filenames:
                    if not fname.endswith(".dds"):
                        continue

                    dds_path = os.path.join(dirpath, fname)
                    ddm_path = dds_path[:-4] + ".ddm"

                    # Need DDM to track properly
                    if not os.path.exists(ddm_path):
                        # Orphan DDS without metadata - remove it
                        try:
                            os.remove(dds_path)
                        except OSError:
                            pass
                        continue

                    meta = self._read_ddm(ddm_path)
                    if meta is None:
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

                    try:
                        size = os.path.getsize(dds_path)
                    except OSError:
                        continue

                    # Use file mtime as access time for initial ordering
                    try:
                        atime = os.path.getmtime(dds_path)
                    except OSError:
                        atime = time.time()

                    with self._lock:
                        if key not in self._entries:
                            self._entries[key] = (dds_path, ddm_path, size, atime)
                            self._current_size += size
                            count += 1

        except Exception as e:
            log.warning(f"DDS cache scan error: {e}")

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
                            if mm.databuffer is not None:
                                mem_offset = file_offset - mm.startpos
                                try:
                                    buf = mm.databuffer.getbuffer()
                                    buf[mem_offset:mem_offset + stripe_bytes] = stripe
                                except Exception:
                                    mm.databuffer.seek(mem_offset)
                                    mm.databuffer.write(stripe)

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

                                if mm.databuffer is not None:
                                    mem_offset = file_offset - mm.startpos
                                    try:
                                        buf = mm.databuffer.getbuffer()
                                        buf[mem_offset:mem_offset + stripe_bytes] = stripe
                                    except Exception:
                                        mm.databuffer.seek(mem_offset)
                                        mm.databuffer.write(stripe)

                        patched += 1

                    f.flush()

        except (FileNotFoundError, OSError) as e:
            log.debug(f"Healing: file I/O error for {tile_id}: {e}")
            return False

        if patched == len(chunk_jpegs):
            # All chunks patched → update DDM to mark tile complete
            meta = self._read_ddm(ddm_path)
            if meta is not None:
                meta["needs_healing"] = False
                meta["healing_chunks"] = 0
                meta["missing_indices"] = []
                if meta.get("mipmaps"):
                    mm0 = meta["mipmaps"][0]
                    mm0["valid"] = mm0.get("total", 0)
                    mm0["complete"] = True
                self._write_ddm(ddm_path, meta)

            tile._dds_needs_healing = False
            tile._dds_missing_indices = []
            log.info(f"Healing complete: {tile_id} ({patched} chunks patched)")
            return True

        # Partial patch — update DDM with remaining missing indices
        remaining = [i for i in (getattr(tile, '_dds_missing_indices', []) or [])
                     if i not in chunk_jpegs]
        meta = self._read_ddm(ddm_path)
        if meta is not None:
            meta["missing_indices"] = remaining
            meta["healing_chunks"] = len(remaining)
            meta["needs_healing"] = len(remaining) > 0
            if meta.get("mipmaps"):
                total = meta["mipmaps"][0].get("total", 0)
                meta["mipmaps"][0]["valid"] = total - len(remaining)
                meta["mipmaps"][0]["complete"] = len(remaining) == 0
            self._write_ddm(ddm_path, meta)

        tile._dds_missing_indices = remaining
        tile._dds_needs_healing = len(remaining) > 0
        log.info(f"Healing partial: {tile_id} ({patched}/{len(chunk_jpegs)} chunks patched, "
                 f"{len(remaining)} remaining)")
        return False

    def _try_heal_from_disk_cache(self, tile_id: str, max_zoom: int, tile) -> None:
        """Check if missing chunk JPEGs exist on disk and dispatch healing.

        Full implementation in Phase 3 (in-place patching).
        """
        missing = getattr(tile, '_dds_missing_indices', [])
        if not missing:
            return

        jpeg_cache_dir = getattr(tile, 'cache_dir', None)
        if not jpeg_cache_dir:
            return

        chunks_per_row = getattr(tile, 'chunks_per_row', 0)
        if not chunks_per_row:
            return

        all_exist = True
        for idx in missing:
            cx = idx % chunks_per_row
            cy = idx // chunks_per_row
            col = tile.col + cx
            row = tile.row + cy
            jpeg_path = os.path.join(
                jpeg_cache_dir,
                f"{col}_{row}_{max_zoom}_{tile.maptype}.jpg")
            if not os.path.exists(jpeg_path):
                all_exist = False
                break

        if all_exist:
            log.debug(f"DDS cache: all missing JPEGs on disk for {tile_id}, "
                      f"dispatching cross-session healing")
            t = threading.Thread(
                target=self._heal_from_disk,
                args=(tile_id, max_zoom, tile, missing),
                daemon=True)
            t.start()

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

    @staticmethod
    def _delete_pair(dds_path: str, ddm_path: str) -> None:
        """Delete DDS + DDM file pair, ignoring missing files."""
        for path in (dds_path, ddm_path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _cleanup_jpegs_async(self, tile) -> None:
        """Schedule JPEG cleanup for a tile whose DDS is now complete."""
        jpeg_cache_dir = getattr(tile, 'cache_dir', None)
        if not jpeg_cache_dir:
            return
        t = threading.Thread(
            target=cleanup_source_jpegs,
            args=(jpeg_cache_dir, tile.col, tile.row,
                  tile.tilename_zoom, tile.max_zoom,
                  getattr(tile, 'min_zoom', 12),
                  tile.width, tile.height, tile.maptype),
            daemon=True)
        t.start()

    def _evict_lru_async(self) -> None:
        """Schedule LRU eviction in a background thread."""
        # Evict down to 90% of budget (hysteresis)
        target = int(self._max_size * 0.9)
        excess = self._current_size - target
        if excess <= 0:
            return
        t = threading.Thread(target=self.evict_lru, args=(excess,), daemon=True)
        t.start()

"""
fallback_resolver.py - Unified fallback resolution for streaming DDS builder

Provides a reusable FallbackResolver class that encapsulates the fallback chain:
1. Disk cache search (find lower-zoom cached chunks and upscale)
2. Mipmap scaling (extract and scale from already-built mipmaps)
3. Network download (download lower-zoom chunks on-demand)

This module enables both live requests and prefetch builds to use the same
fallback logic, configured by fallback_level.

Usage:
    resolver = FallbackResolver(
        cache_dir="/path/to/cache",
        maptype="BI",
        tile_col=1234, tile_row=5678, tile_zoom=16,
        fallback_level=2
    )
    
    # Set available mipmap images for scaling fallback
    resolver.set_mipmap_images({0: mipmap0_image, 3: mipmap3_image})
    
    # Resolve a missing chunk
    rgba_bytes = resolver.resolve(chunk_col, chunk_row, chunk_zoom)
    
    if rgba_bytes:
        builder.add_fallback_image(chunk_index, rgba_bytes)
    else:
        builder.mark_missing(chunk_index)
"""

import logging
import math
import os
import threading
import time
from collections import OrderedDict
from typing import Optional, Dict, Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from autoortho.aoimage.AoImage import AoImage

log = logging.getLogger(__name__)

# Chunk dimensions
CHUNK_WIDTH = 256
CHUNK_HEIGHT = 256
CHUNK_BUFFER_SIZE = CHUNK_WIDTH * CHUNK_HEIGHT * 4

# Deepest lower zoom searched for a substitute: 256 >> 8 is one source pixel,
# still closer to the real terrain colour than missing_color.
MAX_FALLBACK_ZOOM_LEVELS = 8
# Decoded parent chunks kept per resolver. Neighbouring missing chunks share
# parents (2x2 one level down, 4x4 two levels down), so a row decodes each
# parent once.
_PARENT_CACHE_ENTRIES = 32

# (col, row, zoom) -> JPEG bytes or None
ChunkReader = Callable[[int, int, int], Optional[bytes]]


def _load_aoimage():
    try:
        from autoortho.aoimage import AoImage
    except ImportError:
        from aoimage import AoImage
    return AoImage


def _is_jpeg(data) -> bool:
    return bool(data) and len(data) >= 100 and data[:3] == b"\xff\xd8\xff"


def upscale_from_parent(parent_img, col: int, row: int, diff: int):
    """Crop child ``(col, row)`` out of a chunk ``diff`` zooms lower and
    scale it back to 256x256. Returns a new AoImage (caller closes it)."""
    scale = 1 << diff
    size = CHUNK_WIDTH >> diff
    return parent_img.crop_and_upscale(
        (col & (scale - 1)) * size,
        (row & (scale - 1)) * size,
        size,
        size,
        scale,
    )


class TimeBudget:
    """
    Simple time budget tracker for fallback operations.
    
    Compatible with the existing TimeBudget in getortho.py but simplified
    for use in the fallback resolver.
    """
    
    def __init__(self, timeout_seconds: float):
        self.timeout = timeout_seconds
        self.start_time = time.monotonic()
    
    @property
    def elapsed(self) -> float:
        """Seconds elapsed since budget started."""
        return time.monotonic() - self.start_time
    
    @property
    def remaining(self) -> float:
        """Seconds remaining in budget."""
        return max(0.0, self.timeout - self.elapsed)
    
    @property
    def exhausted(self) -> bool:
        """True if budget is exhausted."""
        return self.elapsed >= self.timeout


class FallbackResolver:
    """
    Resolves missing chunks using the fallback chain.
    
    Separated from Tile to enable reuse in streaming builder for both
    live requests and prefetch builds.
    
    Fallback Levels:
        0 - No fallbacks (use missing_color immediately)
        1 - Disk cache + mipmap scaling
        2 - Disk cache + mipmap scaling + network download
    """
    
    def __init__(self, cache_dir: str, maptype: str,
                 tile_col: int, tile_row: int, tile_zoom: int,
                 fallback_level: int = 2,
                 max_mipmap: int = 3,
                 downloader = None,
                 memory_reader: Optional[ChunkReader] = None,
                 secondary_reader: Optional[ChunkReader] = None,
                 grid_origin: Optional[tuple] = None):
        """
        Initialize fallback resolver.
        
        Args:
            cache_dir: Directory containing cached JPEG chunks
            maptype: Map source identifier (e.g., "BI", "EOX")
            tile_col: Tile column coordinate
            tile_row: Tile row coordinate
            tile_zoom: Tile zoom level (max zoom for chunks)
            fallback_level: 0-2, controls which fallbacks are enabled
            max_mipmap: Maximum mipmap level for this tile
            downloader: Optional chunk downloader for network fallback
            memory_reader: Returns JPEG bytes of a chunk already downloaded
                and held in memory (checked before the disk cache)
            secondary_reader: Returns JPEG bytes from slower storage such as
                the long-term cache (checked after the local disk cache)
            grid_origin: ``(col, row)`` of the tile's first chunk at
                ``tile_zoom``; enables upscaling lower-detail mipmap images
        """
        self.cache_dir = cache_dir
        self.maptype = maptype
        self.tile_col = tile_col
        self.tile_row = tile_row
        self.tile_zoom = tile_zoom
        self.fallback_level = fallback_level
        self.max_mipmap = max_mipmap
        self.downloader = downloader
        self.memory_reader = memory_reader
        self.secondary_reader = secondary_reader
        self.grid_origin = grid_origin
        
        # Reference to mipmap images for scaling fallback
        self._mipmap_images: Dict[int, Any] = {}
        # (col, row, zoom) -> decoded AoImage, or None for a known miss.
        # The lock also covers crops so eviction never frees an image in use.
        self._parents: "OrderedDict[tuple, Any]" = OrderedDict()
        self._parents_lock = threading.Lock()
        
        # Statistics
        self.stats = {
            'disk_cache_hits': 0,
            'disk_cache_misses': 0,
            'mipmap_scale_hits': 0,
            'mipmap_scale_misses': 0,
            'network_hits': 0,
            'network_misses': 0,
            'total_resolved': 0,
            'total_failed': 0,
        }
    
    def set_mipmap_images(self, images: Dict[int, Any]) -> None:
        """
        Set available mipmap images for scaling fallback.
        
        Args:
            images: Dict mapping mipmap level to AoImage
        """
        self._mipmap_images = images
    
    def resolve(self, chunk_col: int, chunk_row: int, chunk_zoom: int,
                target_mipmap: int = 0,
                time_budget: Optional[TimeBudget] = None) -> Optional[bytes]:
        result = self.resolve_with_source(
            chunk_col,
            chunk_row,
            chunk_zoom,
            target_mipmap=target_mipmap,
            time_budget=time_budget,
        )
        return result[0] if result is not None else None

    def resolve_with_source(
        self,
        chunk_col: int,
        chunk_row: int,
        chunk_zoom: int,
        target_mipmap: int = 0,
        time_budget: Optional[TimeBudget] = None,
    ) -> Optional[tuple[bytes, str]]:
        """
        Attempt to resolve a missing chunk using the fallback chain.
        
        Tries fallbacks in order based on fallback_level:
        1. Disk cache search (if fallback_level >= 1)
        2. Mipmap scaling (if fallback_level >= 1)
        3. Network download (if fallback_level >= 2)
        
        Args:
            chunk_col: Chunk column at target zoom
            chunk_row: Chunk row at target zoom
            chunk_zoom: Target zoom level
            target_mipmap: Target mipmap level (0 = highest detail)
            time_budget: Optional time budget (None = unbounded)
            
        Returns:
            RGBA bytes (256x256x4 = 262144 bytes) on success, None if all fallbacks fail
        """
        if self.fallback_level <= 0:
            self.stats['total_failed'] += 1
            return None
        
        # Fallback 1: Disk cache search
        if self.fallback_level >= 1:
            result = self._try_disk_cache_fallback(
                chunk_col, chunk_row, chunk_zoom, target_mipmap, time_budget
            )
            if result:
                self.stats['disk_cache_hits'] += 1
                self.stats['total_resolved'] += 1
                return result, "lower_zl_cache"
            self.stats['disk_cache_misses'] += 1
        
        # Check time budget
        if time_budget and time_budget.exhausted:
            self.stats['total_failed'] += 1
            return None
        
        # Fallback 2: Mipmap scaling
        if self.fallback_level >= 1:
            result = self._try_mipmap_scale_fallback(chunk_col, chunk_row, chunk_zoom, target_mipmap)
            if result:
                self.stats['mipmap_scale_hits'] += 1
                self.stats['total_resolved'] += 1
                return result, "lower_mipmap_memory"
            self.stats['mipmap_scale_misses'] += 1
        
        # Check time budget
        if time_budget and time_budget.exhausted:
            self.stats['total_failed'] += 1
            return None
        
        # Fallback 3: Network download (expensive)
        if self.fallback_level >= 2 and self.downloader:
            result = self._try_network_fallback(chunk_col, chunk_row, chunk_zoom, 
                                                 target_mipmap, time_budget)
            if result:
                self.stats['network_hits'] += 1
                self.stats['total_resolved'] += 1
                return result, "cascade_network_fallback"
            self.stats['network_misses'] += 1
        
        self.stats['total_failed'] += 1
        return None
    
    def forget_misses(self) -> None:
        """Drop cached lookups that found nothing, so chunks downloaded since
        are seen."""
        with self._parents_lock:
            for key in [k for k, img in self._parents.items() if img is None]:
                del self._parents[key]

    def close(self) -> None:
        """Free decoded parent chunks."""
        with self._parents_lock:
            images = [img for img in self._parents.values() if img is not None]
            self._parents.clear()
        for img in images:
            try:
                img.close()
            except Exception:
                pass

    def _try_disk_cache_fallback(self, col: int, row: int, zoom: int,
                                  target_mipmap: int,
                                  time_budget: Optional[TimeBudget] = None
                                  ) -> Optional[bytes]:
        """RGBA bytes for the chunk upscaled from the closest lower zoom."""
        img = self.lower_zoom_image(col, row, zoom, target_mipmap, time_budget)
        if img is None:
            return None
        try:
            return img.tobytes()
        finally:
            img.close()

    def lower_zoom_image(self, col: int, row: int, zoom: int,
                         target_mipmap: int = 0,
                         time_budget: Optional[TimeBudget] = None):
        """
        Upscale the closest available lower-zoom imagery covering a chunk.

        ``zoom`` is the chunk's own zoom. Each lower zoom is tried in order
        of quality; at every level an already built lower-detail mipmap
        image, the in-memory chunk, the local disk cache and the secondary
        reader are checked, cheapest first. The closest level always gets a
        full search; deeper levels stop once ``time_budget`` is exhausted.

        Returns a new 256x256 AoImage owned by the caller, or None.
        """
        try:
            AoImage = _load_aoimage()
        except ImportError:
            log.warning("FallbackResolver: Could not import AoImage for disk cache fallback")
            return None
        if 0 <= self.tile_zoom - zoom:
            # The chunk's zoom fixes its mipmap level; mipmap images and
            # their tile-local offsets are keyed by it.
            target_mipmap = self.tile_zoom - zoom

        levels = min(MAX_FALLBACK_ZOOM_LEVELS, zoom)
        for diff in range(1, levels + 1):
            if diff > 1 and time_budget is not None and time_budget.exhausted:
                return None
            upscaled = self._upscale_lower_mipmap(
                col, row, target_mipmap, target_mipmap + diff
            )
            if upscaled is not None:
                return upscaled
            with self._parents_lock:
                parent = self._parent_image(
                    AoImage, col >> diff, row >> diff, zoom - diff
                )
                if parent is not None:
                    try:
                        return upscale_from_parent(parent, col, row, diff)
                    except Exception as e:
                        log.debug(f"FallbackResolver: upscale failed: {e}")
        return None

    def _parent_image(self, AoImage, col: int, row: int, zoom: int):
        """Decoded chunk ``(col, row, zoom)``, or None. Caller holds the lock."""
        key = (col, row, zoom)
        if key in self._parents:
            self._parents.move_to_end(key)
            return self._parents[key]

        img = None
        for read in (
            self.memory_reader,
            self._read_local_cache,
            self.secondary_reader,
        ):
            if read is None:
                continue
            try:
                data = read(col, row, zoom)
            except Exception as e:
                log.debug(f"FallbackResolver: chunk read failed: {e}")
                data = None
            if not _is_jpeg(data):
                continue
            try:
                img = AoImage.load_from_memory(data)
            except Exception as e:
                log.debug(f"FallbackResolver: decode failed for {key}: {e}")
                img = None
            if img is not None:
                if img.size == (CHUNK_WIDTH, CHUNK_HEIGHT):
                    break
                img.close()
                img = None

        self._parents[key] = img
        while len(self._parents) > _PARENT_CACHE_ENTRIES:
            _old_key, old_img = self._parents.popitem(last=False)
            if old_img is not None:
                old_img.close()
        return img

    def _read_local_cache(self, col: int, row: int, zoom: int) -> Optional[bytes]:
        cache_path = os.path.join(
            self.cache_dir, f"{col}_{row}_{zoom}_{self.maptype}.jpg"
        )
        for attempt in range(1, 4):
            try:
                with open(cache_path, 'rb') as f:
                    data = f.read()
                try:
                    os.utime(cache_path, None)  # keep it fresh for cache LRU
                except OSError:
                    pass
                return data
            except FileNotFoundError:
                return None
            except OSError as e:
                # Windows: the cache writer may hold the file briefly.
                if attempt == 3:
                    log.debug(f"FallbackResolver: failed to read cache {cache_path}: {e}")
                    return None
                time.sleep(0.01 * attempt)
        return None

    def _upscale_lower_mipmap(self, col: int, row: int,
                              target_mipmap: int, source_mipmap: int):
        """Upscale the chunk's region out of a complete lower-detail mipmap
        image held in memory."""
        if self.grid_origin is None or not self._mipmap_images:
            return None
        entry = self._mipmap_images.get(source_mipmap)
        img = entry[0] if isinstance(entry, tuple) else entry
        if img is None or getattr(img, "_freed", False):
            return None
        if isinstance(entry, tuple) and len(entry) >= 4:
            if entry[3] != self.tile_zoom - source_mipmap:
                return None
        diff = source_mipmap - target_mipmap
        local_col = col - (self.grid_origin[0] >> target_mipmap)
        local_row = row - (self.grid_origin[1] >> target_mipmap)
        if local_col < 0 or local_row < 0:
            return None
        size = CHUNK_WIDTH >> diff
        x = (local_col * CHUNK_WIDTH) >> diff
        y = (local_row * CHUNK_HEIGHT) >> diff
        try:
            width, height = img.size
            if size < 1 or x + size > width or y + size > height:
                return None
            return img.crop_and_upscale(x, y, size, size, 1 << diff)
        except Exception as e:
            log.debug(f"FallbackResolver: mipmap upscale failed: {e}")
            return None
    
    def _try_mipmap_scale_fallback(self, col: int, row: int, zoom: int,
                                    target_mipmap: int) -> Optional[bytes]:
        """
        Extract and scale region from already-built mipmap.
        
        Looks at higher-detail mipmaps that have already been built
        and extracts/scales the region corresponding to this chunk.
        """
        if not self._mipmap_images or self.grid_origin is None:
            return None
        
        try:
            AoImage = _load_aoimage()
        except ImportError:
            return None
        # Mipmap images cover the whole tile, so offsets are tile-local.
        local_col = col - (self.grid_origin[0] >> target_mipmap)
        local_row = row - (self.grid_origin[1] >> target_mipmap)

        # Check higher-detail mipmaps (lower mipmap numbers = higher detail)
        for higher_mipmap in range(target_mipmap):
            if higher_mipmap not in self._mipmap_images:
                continue
            
            img_data = self._mipmap_images[higher_mipmap]
            if not img_data:
                continue
            
            # Handle tuple format (image, metadata) or plain image
            if isinstance(img_data, tuple):
                higher_img = img_data[0]
            else:
                higher_img = img_data
            
            if higher_img is None:
                continue
            
            try:
                scale_factor = 1 << (target_mipmap - higher_mipmap)
                
                chunk_offset_x = local_col * 256 * scale_factor
                chunk_offset_y = local_row * 256 * scale_factor
                
                # Validate bounds
                if chunk_offset_x < 0 or chunk_offset_y < 0:
                    continue
                
                higher_width, higher_height = higher_img.size
                crop_size = 256 * scale_factor
                
                if chunk_offset_x + crop_size > higher_width:
                    continue
                if chunk_offset_y + crop_size > higher_height:
                    continue
                
                temp_crop = AoImage.new('RGBA', (crop_size, crop_size), (0, 0, 0, 255))
                if not temp_crop:
                    continue

                try:
                    higher_img.crop(temp_crop, (chunk_offset_x, chunk_offset_y))

                    if scale_factor > 1:
                        final_img = temp_crop.reduce_2(int(math.log2(scale_factor)))
                        if not final_img:
                            continue
                    else:
                        final_img = temp_crop

                    try:
                        log.debug(f"FallbackResolver: mipmap scale hit from mipmap {higher_mipmap}")
                        return final_img.tobytes()
                    finally:
                        if final_img is not temp_crop:
                            final_img.close()
                finally:
                    temp_crop.close()
                
            except Exception as e:
                log.debug(f"FallbackResolver: mipmap scale failed: {e}")
                continue
        
        return None
    
    def _try_network_fallback(self, col: int, row: int, zoom: int,
                               target_mipmap: int, 
                               time_budget: Optional[TimeBudget]) -> Optional[bytes]:
        """
        Download lower-zoom chunk from network and upscale.
        
        This is the expensive fallback that makes network requests.
        Only used when fallback_level >= 2.
        """
        if not self.downloader:
            return None
        
        try:
            from autoortho.aoimage import AoImage
        except ImportError:
            try:
                from aoimage import AoImage
            except ImportError:
                return None

        # Try progressively lower-detail zoom levels
        for mipmap_diff in range(1, self.max_mipmap + 1 - target_mipmap):
            if time_budget and time_budget.exhausted:
                break
            
            diff = mipmap_diff
            col_p = col >> diff
            row_p = row >> diff
            zoom_p = zoom - mipmap_diff
            
            if zoom_p < 0:
                continue
            
            scale_factor = 1 << diff
            
            try:
                # Use downloader to fetch chunk
                wait_time = time_budget.remaining if time_budget else 5.0
                chunk_data = self.downloader.get_chunk(
                    col_p, row_p, zoom_p, self.maptype,
                    timeout=min(wait_time, 5.0)
                )
                
                if not chunk_data:
                    continue
                
                # Decode
                parent_img = AoImage.open(chunk_data)
                if not parent_img:
                    continue
                
                # Calculate crop region
                crop_offset_x = (col % scale_factor) * (256 // scale_factor)
                crop_offset_y = (row % scale_factor) * (256 // scale_factor)
                crop_size = 256 // scale_factor
                
                # Crop and upscale
                cropped = parent_img.crop((
                    crop_offset_x, crop_offset_y,
                    crop_offset_x + crop_size, crop_offset_y + crop_size
                ))
                
                if crop_size != 256:
                    cropped = cropped.resize((256, 256), resample=1)
                
                if cropped.mode != 'RGBA':
                    cropped = cropped.convert('RGBA')
                
                log.debug(f"FallbackResolver: network fallback hit at zoom {zoom_p}")
                return cropped.tobytes()
                
            except Exception as e:
                log.debug(f"FallbackResolver: network fallback failed: {e}")
                continue
        
        return None
    
    def get_stats(self) -> dict:
        """Get resolver statistics."""
        return dict(self.stats)
    
    def reset_stats(self) -> None:
        """Reset resolver statistics."""
        for key in self.stats:
            self.stats[key] = 0

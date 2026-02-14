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
import os
import time
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from autoortho.aoimage.AoImage import AoImage

log = logging.getLogger(__name__)

# Chunk dimensions
CHUNK_WIDTH = 256
CHUNK_HEIGHT = 256
CHUNK_BUFFER_SIZE = CHUNK_WIDTH * CHUNK_HEIGHT * 4


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
                 downloader = None):
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
        """
        self.cache_dir = cache_dir
        self.maptype = maptype
        self.tile_col = tile_col
        self.tile_row = tile_row
        self.tile_zoom = tile_zoom
        self.fallback_level = fallback_level
        self.max_mipmap = max_mipmap
        self.downloader = downloader
        
        # Reference to mipmap images for scaling fallback
        self._mipmap_images: Dict[int, Any] = {}
        
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
            result = self._try_disk_cache_fallback(chunk_col, chunk_row, chunk_zoom, target_mipmap)
            if result:
                self.stats['disk_cache_hits'] += 1
                self.stats['total_resolved'] += 1
                return result
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
                return result
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
                return result
            self.stats['network_misses'] += 1
        
        self.stats['total_failed'] += 1
        return None
    
    def _try_disk_cache_fallback(self, col: int, row: int, zoom: int,
                                  target_mipmap: int) -> Optional[bytes]:
        """
        Search disk cache for lower-zoom JPEG and upscale.
        
        Looks for cached chunks at lower zoom levels that cover the
        target chunk position, then crops and upscales.
        """
        try:
            # Import AoImage here to avoid circular imports
            from autoortho.aoimage.AoImage import AoImage
        except ImportError:
            log.warning("FallbackResolver: Could not import AoImage for disk cache fallback")
            return None
        
        max_search_zoom = self.tile_zoom
        
        for mipmap_diff in range(target_mipmap + 1, self.max_mipmap + 1):
            # Calculate equivalent position at lower zoom
            diff = mipmap_diff - target_mipmap
            col_p = col >> diff
            row_p = row >> diff
            zoom_p = zoom - mipmap_diff
            
            if zoom_p > max_search_zoom:
                continue
            
            scale_factor = min(1 << diff, 16)
            
            # Build cache path
            chunk_id = f"{col_p}_{row_p}_{zoom_p}_{self.maptype}"
            cache_path = os.path.join(self.cache_dir, f"{chunk_id}.jpg")
            
            if not os.path.isfile(cache_path):
                continue
            
            # Cache hit - read and upscale
            log.debug(f"FallbackResolver: disk cache hit at {cache_path}")
            
            try:
                data = None
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    try:
                        with open(cache_path, 'rb') as f:
                            data = f.read()
                        break
                    except (IOError, OSError) as e:
                        if attempt < max_attempts:
                            time.sleep(0.01 * attempt)
                        else:
                            log.warning(f"FallbackResolver: failed to read cache {cache_path}: {e}")
                            continue
                
                if not data or len(data) < 100:
                    continue
                
                # Decode JPEG using AoImage (loads from memory bytes)
                parent_img = AoImage.load_from_memory(data)
                if not parent_img:
                    continue
                
                cropped = None
                try:
                    # Calculate crop region for this chunk within parent
                    crop_offset_x = (col % scale_factor) * (256 // scale_factor)
                    crop_offset_y = (row % scale_factor) * (256 // scale_factor)
                    crop_size = 256 // scale_factor
                    
                    # Use crop_and_upscale for atomic crop+scale operation
                    # This is the proper AoImage API (not PIL-style crop/resize)
                    if crop_size != 256:
                        # Need to scale up - calculate scale factor to reach 256x256
                        upscale_factor = 256 // crop_size
                        cropped = parent_img.crop_and_upscale(
                            crop_offset_x, crop_offset_y,
                            crop_size, crop_size,
                            upscale_factor
                        )
                    else:
                        # No scaling needed - just crop
                        # Create destination image for crop
                        cropped = AoImage.new('RGBA', (256, 256), (0, 0, 0, 255))
                        parent_img.crop(cropped, (crop_offset_x, crop_offset_y))
                    
                    if not cropped:
                        continue
                    
                    # Return raw bytes (AoImage is always RGBA)
                    result = cropped.tobytes()
                    return result
                finally:
                    # Clean up images to free native memory
                    try:
                        parent_img.close()
                    except Exception:
                        pass
                    try:
                        if cropped is not None:
                            cropped.close()
                    except Exception:
                        pass
                
            except Exception as e:
                log.debug(f"FallbackResolver: disk cache fallback failed: {e}")
                continue
        
        return None
    
    def _try_mipmap_scale_fallback(self, col: int, row: int, zoom: int,
                                    target_mipmap: int) -> Optional[bytes]:
        """
        Extract and scale region from already-built mipmap.
        
        Looks at higher-detail mipmaps that have already been built
        and extracts/scales the region corresponding to this chunk.
        """
        if not self._mipmap_images:
            return None
        
        try:
            from autoortho.aoimage.AoImage import AoImage
        except ImportError:
            return None
        
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
                
                chunk_offset_x = (col % scale_factor) * 256
                chunk_offset_y = (row % scale_factor) * 256
                
                # Validate bounds
                if chunk_offset_x < 0 or chunk_offset_y < 0:
                    continue
                
                higher_width, higher_height = higher_img.size
                crop_size = 256 * scale_factor
                
                if chunk_offset_x + crop_size > higher_width:
                    continue
                if chunk_offset_y + crop_size > higher_height:
                    continue
                
                # Crop and downscale
                cropped = higher_img.crop((
                    chunk_offset_x, chunk_offset_y,
                    chunk_offset_x + crop_size, chunk_offset_y + crop_size
                ))
                
                if crop_size != 256:
                    cropped = cropped.resize((256, 256), resample=1)  # BILINEAR
                
                if cropped.mode != 'RGBA':
                    cropped = cropped.convert('RGBA')
                
                log.debug(f"FallbackResolver: mipmap scale hit from mipmap {higher_mipmap}")
                return cropped.tobytes()
                
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
            from autoortho.aoimage.AoImage import AoImage
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


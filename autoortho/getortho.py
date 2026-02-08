#!/usr/bin/env python3
import logging
import atexit
import ctypes
import gc
import os
import re
import sys
import time
import threading
import concurrent.futures
import uuid
import math
import tracemalloc
from typing import Optional, Dict, Tuple, List

from io import BytesIO
from urllib.request import urlopen, Request
from queue import Queue, PriorityQueue, Empty, Full
from functools import wraps, lru_cache
from pathlib import Path
from collections import OrderedDict

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho import pydds
except ImportError:
    import pydds

import requests
import psutil

try:
    from autoortho.aoimage import AoImage
except ImportError:
    from aoimage import AoImage

try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

try:
    from autoortho.aostats import STATS, StatTracker, StatsBatcher, get_stat, inc_many, inc_stat, set_stat, update_process_memory_stat, clear_process_memory_stat, update_decode_pool_stats
except ImportError:
    from aostats import STATS, StatTracker, StatsBatcher, get_stat, inc_many, inc_stat, set_stat, update_process_memory_stat, clear_process_memory_stat, update_decode_pool_stats

try:
    from autoortho.utils.constants import (
        system_type, 
        CURRENT_CPU_COUNT,
        EARTH_RADIUS_M,
        PRIORITY_DISTANCE_WEIGHT,
        PRIORITY_DIRECTION_WEIGHT,
        PRIORITY_MIPMAP_WEIGHT,
        LOOKAHEAD_TIME_SEC,
    )
except ImportError:
    from utils.constants import (
        system_type, 
        CURRENT_CPU_COUNT,
        EARTH_RADIUS_M,
        PRIORITY_DISTANCE_WEIGHT,
        PRIORITY_DIRECTION_WEIGHT,
        PRIORITY_MIPMAP_WEIGHT,
        LOOKAHEAD_TIME_SEC,
    )

try:
    from autoortho.utils.apple_token_service import apple_token_service
except ImportError:
    from utils.apple_token_service import apple_token_service

try:
    from autoortho.utils.dynamic_zoom import DynamicZoomManager
except ImportError:
    from utils.dynamic_zoom import DynamicZoomManager

try:
    from autoortho.utils.altitude_predictor import predict_altitude_at_closest_approach
except ImportError:
    from utils.altitude_predictor import predict_altitude_at_closest_approach

try:
    from autoortho.utils.simbrief_flight import simbrief_flight_manager, PathPoint
except ImportError:
    from utils.simbrief_flight import simbrief_flight_manager, PathPoint

try:
    from autoortho.datareftrack import dt as datareftracker
except ImportError:
    from datareftrack import dt as datareftracker

# ============================================================================
# NATIVE CACHE I/O (Optional)
# ============================================================================
# The aopipeline module provides native C implementations that bypass Python's
# GIL for true parallel I/O operations. This significantly improves performance
# when reading many cached JPEG files simultaneously.
# Falls back gracefully to Python implementation if native library not available.
# ============================================================================
_native_cache = None
_native_cache_checked = False

def _get_native_cache():
    """Lazily load and return native cache module, or None if unavailable."""
    global _native_cache, _native_cache_checked
    if _native_cache_checked:
        return _native_cache
    _native_cache_checked = True
    try:
        # Handle imports for both frozen (PyInstaller) and direct Python execution
        try:
            from autoortho.aopipeline import AoCache
        except ImportError:
            from aopipeline import AoCache
        if AoCache.is_available():
            _native_cache = AoCache
            log.info(f"Native cache I/O enabled: {AoCache.get_version()}")
        else:
            log.debug("Native cache library not available, using Python fallback")
    except ImportError as e:
        log.debug(f"Native cache import failed: {e}")
    except Exception as e:
        log.warning(f"Native cache initialization failed: {e}")
    return _native_cache

def _batch_read_cache_files(paths: list) -> dict:
    """
    Read multiple cache files in parallel.
    
    Uses native AoCache for parallel reads when available.
    Falls back to Python ThreadPoolExecutor when native unavailable.
    
    Args:
        paths: List of file paths to read
        
    Returns:
        Dict mapping path -> bytes for successfully read files.
        Missing/failed files are not included in the result.
    """
    if not paths:
        return {}
    
    native = _get_native_cache()
    
    # Try native batch read first (OpenMP parallel, fastest)
    if native is not None:
        try:
            results = native.batch_read_cache(paths, max_threads=0, validate_jpeg=True)
            output = {}
            for path, (data, success) in zip(paths, results):
                if success:
                    output[path] = data
            return output
        except Exception as e:
            log.debug(f"Native batch cache read failed: {e}")
            # Fall through to Python fallback
    
    # Python fallback: ThreadPoolExecutor for parallel file reads
    # This is slower than native but still much faster than sequential reads
    return _batch_read_cache_files_python(paths)


def _batch_read_cache_files_python(paths: list) -> dict:
    """
    Python fallback for batch cache file reading.
    
    Uses ThreadPoolExecutor for parallel file I/O when native library unavailable.
    Python's open()/read() releases the GIL during syscalls, enabling true parallelism.
    
    Args:
        paths: List of file paths to read
        
    Returns:
        Dict mapping path -> bytes for successfully read files.
    """
    def read_single_file(path: str) -> tuple:
        """Read a single cache file. Returns (path, data) or (path, None) on failure."""
        try:
            with open(path, 'rb') as f:
                data = f.read()
            # Validate JPEG magic bytes (same as native)
            if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
                return (path, data)
            return (path, None)  # Invalid JPEG
        except (FileNotFoundError, IOError, OSError):
            return (path, None)
    
    output = {}
    
    # Use limited workers to avoid resource exhaustion
    # 8 workers is a good balance: enough parallelism, not too many file handles
    max_workers = min(8, len(paths))
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(read_single_file, path): path for path in paths}
            
            for future in concurrent.futures.as_completed(futures, timeout=30.0):
                try:
                    path, data = future.result()
                    if data is not None:
                        output[path] = data
                except Exception:
                    pass  # Individual file failures are silent
    except Exception as e:
        log.debug(f"Python batch cache read failed: {e}")
        # Return whatever we got
    
    return output

# ============================================================================
# NATIVE DDS BUILDING (Optional)
# ============================================================================
# The aopipeline module provides native DDS building that bypasses the GIL
# for the entire pipeline: JPEG decoding, image composition, mipmap generation,
# and DXT compression - all in parallel native C code.
#
# PERFORMANCE INSIGHT (from benchmarks):
# - Native file I/O is SLOWER than Python for cached files (OpenMP overhead)
# - Native decode+compress is 3.4x FASTER (true parallelism)
# - OPTIMAL: Python reads files → Native decode+compress (HYBRID approach)
# ============================================================================
_native_dds = None
_native_dds_checked = False

def _get_native_dds():
    """Lazily load and return native DDS module, or None if unavailable."""
    global _native_dds, _native_dds_checked
    if _native_dds_checked:
        return _native_dds
    _native_dds_checked = True
    try:
        # Handle imports for both frozen (PyInstaller) and direct Python execution
        try:
            from autoortho.aopipeline import AoDDS
        except ImportError:
            from aopipeline import AoDDS
        if AoDDS.is_available():
            _native_dds = AoDDS
            
            # Respect user's compressor preference from config
            use_ispc = CFG.pydds.compressor.upper() == "ISPC"
            AoDDS.set_use_ispc(use_ispc)
            
            # Check if hybrid function is available (preferred)
            has_hybrid = hasattr(AoDDS, 'build_from_jpegs')
            log.info(f"Native DDS building enabled: {AoDDS.get_version()} "
                     f"[hybrid: {'yes' if has_hybrid else 'no'}]")
        else:
            log.debug("Native DDS library not available, using Python fallback")
    except ImportError as e:
        log.debug(f"Native DDS import failed: {e}")
    except Exception as e:
        log.warning(f"Native DDS initialization failed: {e}")
    return _native_dds


# ============================================================================
# PIPELINE MODE SELECTION
# ============================================================================
# Determines which pipeline to use based on config and platform.
# Modes: native (C I/O), hybrid (Python I/O + C decode), python (fallback)
# ============================================================================
_pipeline_mode = None
_pipeline_mode_determined = False

# Valid pipeline modes
PIPELINE_MODE_NATIVE = 'native'
PIPELINE_MODE_HYBRID = 'hybrid'
PIPELINE_MODE_PYTHON = 'python'


def get_pipeline_mode() -> str:
    """
    Determine the optimal pipeline mode based on config and platform.
    
    The result is cached after first call.
    
    Returns:
        One of: 'native', 'hybrid', 'python'
    
    Logic:
        - If config is 'auto': detect platform (Windows→native, others→hybrid)
        - If config specifies a mode: use it (with fallback if unavailable)
        - Always falls back to 'python' if native not available
    """
    global _pipeline_mode, _pipeline_mode_determined
    
    if _pipeline_mode_determined:
        return _pipeline_mode
    _pipeline_mode_determined = True
    
    # Get configured mode
    config_mode = getattr(CFG.autoortho, 'pipeline_mode', 'auto').lower().strip()
    
    # Check native availability
    native = _get_native_dds()
    native_available = native is not None
    hybrid_available = native_available and hasattr(native, 'build_from_jpegs')
    
    if config_mode == 'auto':
        # ═══════════════════════════════════════════════════════════════════════
        # AUTO PIPELINE SELECTION (Updated January 2026)
        # ═══════════════════════════════════════════════════════════════════════
        # 
        # With buffer pool optimization (Phase 1-3), the performance picture changed:
        # 
        # BENCHMARK RESULTS (4096x4096 tile):
        #   Without buffer pool:
        #     - Native (C I/O):     140ms
        #     - Hybrid (Python I/O): 149ms
        #     - Native wins by ~6%
        # 
        #   WITH buffer pool:
        #     - Buffer pool build:   55ms (2.54x faster!)
        #     - Allocation overhead: 83ms saved
        #     - Python file read:    10ms (OS cache optimized)
        #     - C file read:         21ms (OpenMP overhead)
        # 
        # OPTIMAL PATH:
        #   Hybrid + buffer pool = 10ms read + 55ms build = ~65ms
        #   Native + buffer pool = 21ms read + 55ms build = ~76ms
        # 
        # Both modes now have direct-to-disk optimization which is even faster
        # for predictive builds (~55ms, no copy overhead).
        # 
        # RECOMMENDATION: Prefer HYBRID on all platforms when buffer pool available
        # because Python file reads are faster than C file reads (OS cache).
        # ═══════════════════════════════════════════════════════════════════════
        
        if hybrid_available:
            # Hybrid is optimal when buffer pool available (all platforms)
            selected = PIPELINE_MODE_HYBRID
        elif native_available:
            selected = PIPELINE_MODE_NATIVE
        else:
            selected = PIPELINE_MODE_PYTHON
        
        log.info(f"Pipeline mode: {selected} (auto-detected for {sys.platform}, buffer pool optimized)")
    
    elif config_mode == PIPELINE_MODE_NATIVE:
        if native_available:
            selected = PIPELINE_MODE_NATIVE
            log.info(f"Pipeline mode: native (explicitly configured)")
        else:
            selected = PIPELINE_MODE_PYTHON
            log.warning(f"Pipeline mode 'native' requested but unavailable, using 'python'")
    
    elif config_mode == PIPELINE_MODE_HYBRID:
        if hybrid_available:
            selected = PIPELINE_MODE_HYBRID
            log.info(f"Pipeline mode: hybrid (explicitly configured)")
        elif native_available:
            selected = PIPELINE_MODE_NATIVE
            log.warning(f"Pipeline mode 'hybrid' requested but unavailable, using 'native'")
        else:
            selected = PIPELINE_MODE_PYTHON
            log.warning(f"Pipeline mode 'hybrid' requested but unavailable, using 'python'")
    
    elif config_mode == PIPELINE_MODE_PYTHON:
        selected = PIPELINE_MODE_PYTHON
        log.info("Pipeline mode: python (explicitly configured)")
    
    else:
        # Unknown mode - use auto logic
        log.warning(f"Unknown pipeline_mode '{config_mode}', using auto")
        if native_available:
            selected = PIPELINE_MODE_NATIVE if sys.platform == 'win32' else PIPELINE_MODE_HYBRID
        else:
            selected = PIPELINE_MODE_PYTHON
    
    _pipeline_mode = selected
    return _pipeline_mode


# ============================================================================
# BUNDLE DDS BUILDING (Mode Dispatcher)
# ============================================================================
# Builds DDS from AOB2 bundle files using the appropriate pipeline mode.
# ============================================================================

def _build_tile_dds_from_bundle(
    tile,
    bundle_path: str,
    target_zoom: int,
    dxt_format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    chunks_per_side: int = 16
) -> Optional[bytes]:
    """
    Build DDS from an AOB2 bundle file using the appropriate pipeline mode.
    
    This is the mode dispatcher that routes to native, hybrid, or pure Python
    implementations based on current pipeline mode and availability.
    
    Args:
        tile: Tile object (for metadata)
        bundle_path: Path to AOB2 bundle file
        target_zoom: Target zoom level
        dxt_format: "BC1" or "BC3"
        missing_color: RGB tuple for missing chunks
        chunks_per_side: Chunks per side (typically 16)
    
    Returns:
        DDS bytes if successful, None on failure
    """
    mode = get_pipeline_mode()
    
    # Try native mode (C reads bundle + builds DDS)
    if mode == PIPELINE_MODE_NATIVE:
        result = _build_dds_native_from_bundle(bundle_path, target_zoom, dxt_format, missing_color, chunks_per_side)
        if result is not None:
            return result
        # Fall through to hybrid
    
    # Try hybrid mode (Python reads bundle, C decodes + compresses)
    if mode in (PIPELINE_MODE_NATIVE, PIPELINE_MODE_HYBRID):
        result = _build_dds_hybrid_from_bundle(bundle_path, target_zoom, dxt_format, missing_color, chunks_per_side)
        if result is not None:
            return result
        # Fall through to pure Python
    
    # Pure Python mode
    return _build_dds_python_from_bundle(bundle_path, target_zoom, dxt_format, missing_color, chunks_per_side)


def _build_dds_native_from_bundle(
    bundle_path: str,
    target_zoom: int,
    dxt_format: str,
    missing_color: Tuple[int, int, int],
    chunks_per_side: int
) -> Optional[bytes]:
    """Build DDS using native C code for bundle I/O and DDS building."""
    try:
        try:
            from autoortho.aopipeline import AoBundle2
        except ImportError:
            from aopipeline import AoBundle2
        
        if not AoBundle2.is_available():
            return None
        
        return AoBundle2.build_dds(
            bundle_path=bundle_path,
            target_zoom=target_zoom,
            format=dxt_format,
            missing_color=missing_color,
            chunks_per_side=chunks_per_side
        )
    except Exception as e:
        log.debug(f"Native bundle DDS build failed: {e}")
        return None


def _build_dds_hybrid_from_bundle(
    bundle_path: str,
    target_zoom: int,
    dxt_format: str,
    missing_color: Tuple[int, int, int],
    chunks_per_side: int
) -> Optional[bytes]:
    """Build DDS: Python reads bundle, C decodes and compresses."""
    try:
        try:
            from autoortho.aopipeline import AoBundle2, AoDDS
        except ImportError:
            from aopipeline import AoBundle2, AoDDS
        
        # Python reads bundle - use context manager to release file immediately
        # Critical: On Windows, mmap holds file open and blocks consolidation updates
        with AoBundle2.Bundle2Python(bundle_path) as bundle:
            jpeg_datas = bundle.get_all_chunks(target_zoom)
        # Bundle is now closed - file handle released
        
        if not jpeg_datas:
            return None
        
        # Check if native DDS is available
        native = _get_native_dds()
        if native is None or not hasattr(native, 'build_from_jpegs'):
            return None
        
        # C builds DDS from JPEG array
        return native.build_from_jpegs(
            jpeg_datas=jpeg_datas,
            format=dxt_format,
            missing_color=missing_color
        )
    except Exception as e:
        log.debug(f"Hybrid bundle DDS build failed: {e}")
        return None


def _build_dds_python_from_bundle(
    bundle_path: str,
    target_zoom: int,
    dxt_format: str,
    missing_color: Tuple[int, int, int],
    chunks_per_side: int
) -> Optional[bytes]:
    """Build DDS using pure Python (no native dependencies)."""
    try:
        try:
            from autoortho.aopipeline import AoBundle2
        except ImportError:
            from aopipeline import AoBundle2
        
        # Read bundle with Python - use context manager to release file immediately
        # Critical: On Windows, mmap holds file open and blocks consolidation updates
        with AoBundle2.Bundle2Python(bundle_path) as bundle:
            jpeg_datas = bundle.get_all_chunks(target_zoom)
        # Bundle is now closed - file handle released
        
        if not jpeg_datas:
            return None
        
        # Compose image using AoImage
        tile_size = chunks_per_side * 256
        chunk_size = 256
        
        base_img = AoImage.new("RGBA", (tile_size, tile_size), missing_color + (255,))
        
        for i, jpeg_data in enumerate(jpeg_datas):
            if jpeg_data is not None:
                try:
                    chunk_img = AoImage.load_from_memory(jpeg_data)
                    if chunk_img:
                        x = (i % chunks_per_side) * chunk_size
                        y = (i // chunks_per_side) * chunk_size
                        base_img.paste(chunk_img, x, y)
                except Exception:
                    pass  # Use missing color for failed decodes
        
        # Build DDS using pydds
        dds_format = 0 if dxt_format == "BC1" else 1  # BC1=0, BC3=1
        dds = pydds.DDS(tile_size, tile_size, dxt_format=dds_format)
        dds.gen_mipmaps(base_img)
        return dds.read(dds.total_size)
        
    except Exception as e:
        log.debug(f"Python bundle DDS build failed: {e}")
        return None


# Global buffer pool for zero-copy DDS building (unified for live + prefetch)
# Uses priority queue: live tiles (PRIORITY_LIVE=0) are served first,
# prefetch tiles (PRIORITY_PREFETCH=100) are served when idle.
_dds_buffer_pool = None
_dds_buffer_pool_initialized = False

# Tile queue manager for bank-queue style processing
_tile_queue_manager = None
_tile_queue_initialized = False

# Priority constants for buffer pool acquisition
# Import from AoDDS when available, or use local fallbacks
PRIORITY_LIVE = 0       # Live tiles requested by X-Plane (premium, front of queue)
PRIORITY_PREFETCH = 100 # Prefetch/pre-built tiles (low priority, back of queue)


def reset_dds_buffer_pool():
    """
    Reset the DDS buffer pool so it will be recreated on next access.
    
    Call this when zoom-related settings change (max_zoom, max_zoom_near_airports,
    max_zoom_mode, dynamic_zoom_steps, or using_custom_tiles) to ensure the buffer
    pool is sized correctly for the new configuration.
    
    The pool will be lazily recreated with the new settings on next use.
    
    Note: The unified buffer pool serves both live and prefetch tiles using a
    priority queue system. Live tiles are always served first (premium clients).
    """
    global _dds_buffer_pool, _dds_buffer_pool_initialized
    global _tile_queue_manager, _tile_queue_initialized
    
    if _dds_buffer_pool is not None:
        log.info("Resetting DDS buffer pool (zoom settings changed)")
    
    _dds_buffer_pool = None
    _dds_buffer_pool_initialized = False
    
    # Shutdown tile queue if it was initialized
    if _tile_queue_manager is not None:
        try:
            _tile_queue_manager.shutdown()
        except Exception as e:
            log.debug(f"Error shutting down tile queue: {e}")
    _tile_queue_manager = None
    _tile_queue_initialized = False


def _calc_required_buffer_size():
    """
    Calculate the required DDS buffer size based on user configuration.
    
    For standard AutoOrtho scenery (using_custom_tiles=False):
        - Fixed mode: Check if max_zoom/max_zoom_near_airports require 8K
        - Dynamic mode: Parse dynamic_zoom_steps to find max configured zoom levels
    
    For custom tiles (using_custom_tiles=True):
        - Unknown tile zoom levels, assume worst case (8K)
    
    8K buffers (~43MB) are needed when:
        - max_zoom > 16 (ZL16 tiles building at ZL17)
        - max_zoom_near_airports > 18 (ZL18 tiles building at ZL19)
        - Any dynamic zoom step has zoom_level > 16 or zoom_level_airports > 18
        - Custom tiles are enabled
    
    Returns:
        Tuple of (buffer_size, size_name) where size_name is for logging
    """
    native = _get_native_dds()
    if native is None or not hasattr(native, 'DDSBufferPool'):
        return None, None
    
    # Default to 4K (16×16 chunks = 4096×4096)
    SIZE_4K = native.DDSBufferPool.SIZE_4096x4096_BC1
    SIZE_8K = native.DDSBufferPool.SIZE_8192x8192_BC1
    
    # Custom tiles: unknown zoom levels, assume worst case (8K)
    using_custom_tiles = getattr(CFG.autoortho, 'using_custom_tiles', False)
    if isinstance(using_custom_tiles, str):
        using_custom_tiles = using_custom_tiles.lower() in ('true', '1', 'yes', 'on')
    
    if using_custom_tiles:
        log.debug("Custom tiles enabled: using 8K buffer size (worst case)")
        return SIZE_8K, "8K (custom tiles)"
    
    # Standard AutoOrtho scenery: calculate based on config
    max_zoom_mode = str(getattr(CFG.autoortho, 'max_zoom_mode', 'fixed')).lower()
    
    if max_zoom_mode == "dynamic":
        # Dynamic zoom mode: check the actual configured zoom steps
        # Parse dynamic_zoom_steps to find maximum zoom levels
        max_step_zoom = 16  # Default if no steps configured
        max_step_zoom_airports = 18
        
        try:
            steps_config = getattr(CFG.autoortho, 'dynamic_zoom_steps', [])
            if steps_config and steps_config != "[]":
                # Parse steps if it's a list of dicts
                if isinstance(steps_config, list):
                    for step in steps_config:
                        if isinstance(step, dict):
                            zoom = int(step.get('zoom_level', 16))
                            zoom_airports = int(step.get('zoom_level_airports', 18))
                            max_step_zoom = max(max_step_zoom, zoom)
                            max_step_zoom_airports = max(max_step_zoom_airports, zoom_airports)
        except Exception as e:
            log.debug(f"Failed to parse dynamic_zoom_steps: {e}, assuming max ZL17")
            max_step_zoom = 17  # Safe default for dynamic mode
        
        # Check if any step requires 8K
        # ZL16 tiles can build at step zoom (up to ZL17 = 8K)
        # ZL18 airport tiles can build at step zoom_airports (up to ZL19 = 8K)
        needs_8k = (max_step_zoom > 16) or (max_step_zoom_airports > 18)
        
        if needs_8k:
            log.debug(f"Dynamic zoom requires 8K buffers (max_step_zoom={max_step_zoom}, max_step_zoom_airports={max_step_zoom_airports})")
            return SIZE_8K, f"8K (dynamic ZL{max(max_step_zoom, max_step_zoom_airports)})"
        else:
            log.debug(f"Dynamic zoom allows 4K buffers (max_step_zoom={max_step_zoom}, max_step_zoom_airports={max_step_zoom_airports})")
            return SIZE_4K, "4K (dynamic)"
    
    # Fixed mode: check configured zoom levels
    try:
        max_zoom = int(getattr(CFG.autoortho, 'max_zoom', 16))
        max_zoom_airports = int(getattr(CFG.autoortho, 'max_zoom_near_airports', 18))
    except (ValueError, TypeError):
        max_zoom = 16
        max_zoom_airports = 18
    
    # Standard AutoOrtho has:
    # - ZL16 tiles (regular scenery): can build at max_zoom (up to ZL17 = 8K)
    # - ZL18 tiles (near airports): can build at max_zoom_near_airports (up to ZL19 = 8K)
    #
    # If max_zoom > 16, ZL16 tiles will build at ZL17 → 8K needed
    # If max_zoom_airports > 18, ZL18 tiles will build at ZL19 → 8K needed
    needs_8k = (max_zoom > 16) or (max_zoom_airports > 18)
    
    if needs_8k:
        log.debug(f"Config requires 8K buffers (max_zoom={max_zoom}, max_zoom_airports={max_zoom_airports})")
        return SIZE_8K, f"8K (ZL{max(max_zoom, max_zoom_airports)})"
    else:
        log.debug(f"Config allows 4K buffers (max_zoom={max_zoom}, max_zoom_airports={max_zoom_airports})")
        return SIZE_4K, "4K"


def _get_dds_buffer_pool():
    """
    Get or create the unified DDS buffer pool with priority queue.
    
    This single pool serves both live tiles and prefetch tiles using a
    priority queue system:
    - Live tiles (PRIORITY_LIVE=0): Premium clients, always served first
    - Prefetch tiles (PRIORITY_PREFETCH=100): Low priority, served when idle
    
    Pool size is configured via CFG.autoortho.buffer_pool_size (2-64).
    Buffer size is calculated dynamically based on configuration:
        - 4K (~11MB) when max_zoom <= 16 and max_zoom_near_airports <= 18
        - 8K (~43MB) when higher zoom levels are configured or custom tiles enabled
    
    Only created if pipeline mode is 'native' or 'hybrid'.
    
    Returns:
        DDSBufferPool instance, or None if unavailable/disabled
    """
    global _dds_buffer_pool, _dds_buffer_pool_initialized
    if _dds_buffer_pool_initialized:
        return _dds_buffer_pool
    _dds_buffer_pool_initialized = True
    
    # Only create pool for native/hybrid modes
    mode = get_pipeline_mode()
    if mode == PIPELINE_MODE_PYTHON:
        log.debug("Buffer pool not needed for python pipeline mode")
        return None
    
    try:
        native = _get_native_dds()
        if native is not None and hasattr(native, 'DDSBufferPool'):
            # Calculate optimal pool size from worker counts
            # Maximum concurrent builds = prefetch_workers + live_builder_concurrency
            # More buffers than this would never be used simultaneously
            try:
                prefetch_workers = int(getattr(CFG.autoortho, 'background_builder_workers', 2))
                live_concurrency = int(getattr(CFG.autoortho, 'live_builder_concurrency', 8))
            except (ValueError, TypeError):
                prefetch_workers = 2
                live_concurrency = 8
            
            # Optimal pool size = total concurrent workers (cap)
            optimal_pool_size = prefetch_workers + live_concurrency
            
            # Get user-configured pool size (defaults to optimal)
            try:
                pool_size = int(getattr(CFG.autoortho, 'buffer_pool_size', optimal_pool_size))
            except (ValueError, TypeError):
                pool_size = optimal_pool_size
            
            # Clamp to valid range: minimum 2, maximum is optimal (no benefit exceeding workers)
            pool_size = max(2, min(optimal_pool_size, pool_size))
            
            log.debug(f"DDS buffer pool sizing: configured={getattr(CFG.autoortho, 'buffer_pool_size', 'default')}, "
                      f"optimal={optimal_pool_size} (prefetch={prefetch_workers} + live={live_concurrency}), "
                      f"final={pool_size}")
            
            # Calculate buffer size based on configuration
            buffer_size, size_name = _calc_required_buffer_size()
            if buffer_size is None:
                log.debug("Could not determine buffer size, using default 4K")
                buffer_size = native.DDSBufferPool.SIZE_4096x4096_BC1
                size_name = "4K (default)"
            
            _dds_buffer_pool = native.DDSBufferPool(
                buffer_size=buffer_size,
                pool_size=pool_size
            )
            
            total_mb = (pool_size * buffer_size) / (1024 * 1024)
            log.info(f"DDS buffer pool (unified): {pool_size} × {size_name} buffers "
                     f"({buffer_size/1024/1024:.1f}MB each) = {total_mb:.1f}MB total "
                     f"(priority queue enabled: live tiles first)")
    except Exception as e:
        log.debug(f"DDS buffer pool init failed: {e}")
    
    return _dds_buffer_pool




def _is_tile_queue_enabled() -> bool:
    """Check if tile queue system is enabled in config."""
    try:
        enabled = getattr(CFG.autoortho, 'tile_queue_enabled', True)
        if isinstance(enabled, str):
            return enabled.lower() in ('true', '1', 'yes', 'on')
        return bool(enabled)
    except Exception:
        return True  # Default to enabled


def _get_tile_queue_max_size() -> int:
    """Get the max queue size from config."""
    try:
        max_size = int(getattr(CFG.autoortho, 'tile_queue_max_size', 100))
        return max(10, min(500, max_size))
    except Exception:
        return 100


def _build_dds_hybrid(chunks: list, dxt_format: str,
                      missing_color: tuple) -> bytes:
    """
    Build DDS using HYBRID approach: chunks already in memory + native decode.
    
    This is the OPTIMAL approach based on benchmarks:
    - Avoids file I/O entirely (chunks already have .data)
    - Uses native parallel JPEG decode (3.4x faster)
    - Uses native ISPC/STB compression
    - Zero-copy output when buffer pool available (~80ms saved)
    
    Args:
        chunks: List of Chunk objects (must have .data attribute)
        dxt_format: "BC1" or "BC3"
        missing_color: RGB tuple for missing chunks
    
    Returns:
        DDS bytes on success, None on failure
    
    Performance: ~30-40ms with buffer pool vs ~112ms pure Python (3-4x faster)
    """
    native = _get_native_dds()
    if native is None or not hasattr(native, 'build_from_jpegs'):
        return None
    
    try:
        # Extract JPEG data from chunks
        # TOCTOU safety: capture data references atomically
        jpeg_datas = []
        valid_count = 0
        for chunk in chunks:
            # Capture local reference (atomic due to GIL)
            data = chunk.data
            if data and len(data) > 0:
                jpeg_datas.append(data)
                valid_count += 1
            else:
                jpeg_datas.append(None)
        
        if valid_count == 0:
            log.debug("Hybrid DDS build: no valid chunks")
            return None
        
        # Acquire buffer from pool (blocking with priority queue)
        # Live builds are high priority, prefetch is low priority
        pool = _get_dds_buffer_pool()
        if pool is None or not hasattr(native, 'build_from_jpegs_to_buffer'):
            log.debug("Hybrid DDS build: buffer pool not available")
            return None
        
        try:
            # Blocking acquire with LIVE priority (front of queue)
            # No fallback to allocation - wait for buffer to be available
            buffer, buffer_id = pool.acquire(timeout=30.0, priority=PRIORITY_LIVE)
        except TimeoutError:
            log.debug("Hybrid DDS build: buffer pool timeout (queue full)")
            bump('hybrid_buffer_pool_timeout')
            return None
        
        try:
            result = native.build_from_jpegs_to_buffer(
                buffer,
                jpeg_datas,
                format=dxt_format,
                missing_color=missing_color
            )
            
            if result.success and result.bytes_written >= 128:
                dds_bytes = result.to_bytes()
                log.debug(f"Hybrid DDS build: {valid_count}/{len(chunks)} chunks, "
                          f"{len(dds_bytes)} bytes")
                return dds_bytes
            else:
                log.debug("Hybrid DDS build: compression failed")
                return None
        finally:
            pool.release(buffer_id)
            
    except Exception as e:
        log.debug(f"Hybrid DDS build exception: {e}")
        return None


def _build_dds_native(cache_dir: str, tile_row: int, tile_col: int,
                       maptype: str, zoom: int, chunks_per_side: int,
                       dxt_format: str, missing_color: tuple) -> bytes:
    """
    Build a complete DDS tile using native code (reads from disk).
    
    NOTE: This is the LEGACY approach. For optimal performance, prefer
    _build_dds_hybrid() when chunk data is already in memory.
    
    This function uses the native aopipeline library for the entire
    DDS building pipeline. Falls back to None if native not available.
    
    Args:
        cache_dir: Directory containing cached JPEG chunks
        tile_row: Tile row coordinate
        tile_col: Tile column coordinate
        maptype: Map source identifier
        zoom: Zoom level
        chunks_per_side: Number of chunks per side
        dxt_format: "BC1" or "BC3"
        missing_color: RGB tuple for missing chunks
    
    Returns:
        DDS bytes on success, None on failure or if native unavailable
    """
    native = _get_native_dds()
    if native is None:
        return None
    
    try:
        result = native.build_tile_native_detailed(
            cache_dir=cache_dir,
            row=tile_row,
            col=tile_col,
            maptype=maptype,
            zoom=zoom,
            chunks_per_side=chunks_per_side,
            format=dxt_format,
            missing_color=missing_color
        )
        
        if result.success:
            log.debug(f"Native DDS build: {result.chunks_decoded}/{result.chunks_found} chunks, "
                      f"{result.mipmaps} mipmaps, {result.elapsed_ms:.1f}ms")
            return result.data
        else:
            log.debug(f"Native DDS build failed: {result.error}")
            return None
    except Exception as e:
        log.debug(f"Native DDS build exception: {e}")
        return None


MEMTRACE = False

log = logging.getLogger(__name__)


def create_http_session(pool_size=10):
    """
    Factory function to create an HTTP session with connection pooling.
    
    Returns a requests session configured with connection pooling.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=0,
        pool_block=True,
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    log.debug(f"Created requests session (pool_size={pool_size})")
    return session


# JPEG decode concurrency: auto-tuned for optimal performance
# Decode is memory-bound, not CPU-bound, so we can safely exceed CPU count
# Each decode uses ~256KB RAM, so even 64 concurrent = only ~16MB
_MAX_DECODE = min(CURRENT_CPU_COUNT * 4, 64)
_decode_sem = threading.Semaphore(_MAX_DECODE)


# Track average fetch times
tile_stats = StatTracker(20, 12)
mm_stats = StatTracker(0, 5)
partial_stats = StatTracker()

# Track FULL tile creation duration (download + compose + compress)
# This is the key metric for tuning tile_time_budget
tile_creation_stats = StatTracker(0, 5, default=0, maxlen=50)

stats_batcher = None


def _ensure_stats_batcher():
    global stats_batcher
    if stats_batcher is None:
        try:
            # Create when a remote store is bound (either via env or parent bind)
            if getattr(STATS, "_remote", None) is not None or os.getenv("AO_STATS_ADDR"):
                stats_batcher = StatsBatcher(flush_interval=0.05, max_items=200)
                atexit.register(stats_batcher.stop)
        except Exception:
            stats_batcher = None


def bump(key, n=1):
    _ensure_stats_batcher()
    if stats_batcher:
        stats_batcher.add(key, n)
    else:
        inc_stat(key, n)


def bump_many(d: dict):
    _ensure_stats_batcher()
    if stats_batcher:
        stats_batcher.add_many(d)
    else:
        inc_many(d)


def get_tile_creation_stats():
    """
    Get tile creation statistics for monitoring and tuning.
    
    Returns a dictionary with:
    - count: Number of tiles created
    - avg_time_s: Average creation time in seconds
    - avg_time_by_mipmap: Dict of mipmap level -> average time in seconds
    - averages: Rolling averages from tile_creation_stats (last 50 samples per mipmap)
    
    This is useful for tuning tile_time_budget - the average creation time
    gives a good baseline for how long tiles actually take to create.
    """
    result = {
        'count': 0,
        'avg_time_s': 0.0,
        'avg_time_by_mipmap': {},
        'averages': {},
    }
    
    try:
        # Get rolling averages from StatTracker (recent samples)
        result['averages'] = dict(tile_creation_stats.averages)
        
    except Exception as e:
        log.debug(f"get_tile_creation_stats error: {e}")
    
    return result


def log_tile_creation_summary():
    """Log a summary of tile creation statistics."""
    stats = get_tile_creation_stats()
    if stats['count'] > 0:
        log.info(f"TILE CREATION STATS: {stats['count']} tiles created, "
                f"avg time: {stats['avg_time_s']:.2f}s")
        if stats['avg_time_by_mipmap']:
            mm_str = ", ".join(f"MM{k}: {v:.2f}s" for k, v in sorted(stats['avg_time_by_mipmap'].items()))
            log.info(f"  Per-mipmap averages: {mm_str}")
        if stats['averages']:
            recent_str = ", ".join(f"MM{k}: {v:.2f}s" for k, v in sorted(stats['averages'].items()) if v > 0)
            if recent_str:
                log.info(f"  Recent averages (last 50): {recent_str}")


seasons_enabled = CFG.seasons.enabled

if seasons_enabled:
    try:
        from autoortho.aoseasons import AoSeasonCache
    except ImportError:
        from aoseasons import AoSeasonCache
    ao_seasons = AoSeasonCache(CFG.paths.cache_dir)

    # Per-DSF tile locks to serialize .si generation across threads
    _season_lock_map = {}
    _season_lock_map_lock = threading.Lock()

    def _season_tile_key_from_rc(row: int, col: int, zoom: int) -> str:
        # Compute DSF base name used by AoDsfSeason (e.g., +37-122)
        lon = col / pow(2, zoom) * 360 - 180
        n = math.pi - 2 * math.pi * row / pow(2, zoom)
        lat = 180 / math.pi * math.atan(0.5 * (math.exp(n) - math.exp(-n)))
        lat_i = math.floor(lat)
        lon_i = math.floor(lon)
        return f"{lat_i:+03d}{lon_i:+04d}"

    def _get_season_lock(key: str) -> threading.Lock:
        with _season_lock_map_lock:
            lock = _season_lock_map.get(key)
            if lock is None:
                lock = threading.Lock()
                _season_lock_map[key] = lock
            return lock

    def season_saturation_locked(row: int, col: int, zoom: int, day: Optional[int] = None) -> float:
        key = _season_tile_key_from_rc(row, col, zoom)
        lock = _get_season_lock(key)
        with lock:
            return ao_seasons.saturation(row, col, zoom, day)


def _is_jpeg(dataheader):
    # FFD8FF identifies image as a JPEG
    if dataheader[:3] == b'\xFF\xD8\xFF':
        return True
    else:
        return False


def _gtile_to_quadkey(til_x, til_y, zoomlevel):
    """
    Translates Google coding of tiles to Bing Quadkey coding. 
    """
    quadkey=""
    temp_x=til_x
    temp_y=til_y    
    for step in range(1,zoomlevel+1):
        size=2**(zoomlevel-step)
        a=temp_x//size
        b=temp_y//size
        temp_x=temp_x-a*size
        temp_y=temp_y-b*size
        quadkey=quadkey+str(a+2*b)
    return quadkey


def _chunk_to_latlon(row: int, col: int, zoom: int) -> tuple:
    """
    Convert tile row/col/zoom to center lat/lon coordinates.
    Returns (lat, lon) in degrees.
    
    Uses official OSM/Web Mercator formula:
    n = 2 ^ zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = arctan(sinh(π * (1 - 2 * ytile / n)))
    lat_deg = lat_rad * 180.0 / π
    
    Note: Adds 0.5 to row/col to get tile CENTER instead of top-left corner.
    """
    n = 2.0 ** zoom
    # Use tile center by adding 0.5 to both coordinates
    lon = (col + 0.5) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (row + 0.5) / n)))
    lat = math.degrees(lat_rad)
    return (lat, lon)


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate great-circle distance between two points in meters using Haversine formula.
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_M * c


def _calculate_spatial_priority(chunk_row: int, chunk_col: int, chunk_zoom: int, base_mipmap_priority: int) -> float:
    """
    Calculate priority based on:
    1. Distance from player
    2. Direction relative to movement vector (predictive)
    3. Base mipmap priority (detail level)
    
    Lower priority number = more urgent (fetched first).
    Returns priority as float.
    """
    # If no valid flight data, fall back to mipmap-only priority
    if not datareftracker.data_valid or not datareftracker.connected:
        return float(base_mipmap_priority)
    
    try:
        # Get player position and velocity
        player_lat = datareftracker.lat
        player_lon = datareftracker.lon
        player_hdg = datareftracker.hdg  # degrees, 0=N, 90=E, 180=S, 270=W
        player_spd = datareftracker.spd  # m/s

        chunk_lat, chunk_lon = _chunk_to_latlon(chunk_row, chunk_col, chunk_zoom)
        
        distance_m = _haversine_distance(player_lat, player_lon, chunk_lat, chunk_lon)
        
        distance_priority = min(100, distance_m / 100)  # 100m per priority point, max 100
        
        if player_spd > 5:  # Only use predictive if moving
            lat1_rad = math.radians(player_lat)
            lat2_rad = math.radians(chunk_lat)
            dlon_rad = math.radians(chunk_lon - player_lon)
            
            x = math.sin(dlon_rad) * math.cos(lat2_rad)
            y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
            bearing = math.degrees(math.atan2(x, y))
            bearing = (bearing + 360) % 360
            
            angle_diff = abs(player_hdg - bearing)
            if angle_diff > 180:
                angle_diff = 360 - angle_diff
            

            direction_priority = (angle_diff / 180) * 100 - 50
            
            predicted_distance = player_spd * LOOKAHEAD_TIME_SEC
            if distance_m < predicted_distance and angle_diff < 45:
                direction_priority -= 30
        else:
            direction_priority = 0
        

        total_priority = (
            base_mipmap_priority * PRIORITY_MIPMAP_WEIGHT +
            distance_priority * PRIORITY_DISTANCE_WEIGHT +
            direction_priority * PRIORITY_DIRECTION_WEIGHT
        )
        
        return max(0, total_priority)
        
    except Exception as e:
        # On any error, fall back to mipmap-only priority
        log.debug(f"Spatial priority calculation failed: {e}")
        return float(base_mipmap_priority)


def _safe_paste(dest_img, chunk_img, start_x, start_y, defer_cleanup=False):
    """
    Paste chunk_img into dest_img at (start_x, start_y) with coordinate validation.
    
    Args:
        dest_img: Destination AoImage
        chunk_img: Source chunk AoImage to paste
        start_x, start_y: Position to paste at
        defer_cleanup: If True, don't free chunk_img (caller will batch-free later)
    
    Returns True if paste succeeded, False if skipped due to invalid coordinates.
    """
    if start_x < 0 or start_y < 0:
        log.warning(f"GET_IMG: Skipping chunk with invalid coordinates ({start_x},{start_y})")
        if not defer_cleanup:
            try:
                chunk_img.close()
            except Exception:
                pass
        return False
    
    if not dest_img.paste(chunk_img, (start_x, start_y)):
        log.warning(f"GET_IMG: paste() failed for chunk at ({start_x},{start_y})")
        if not defer_cleanup:
            try:
                chunk_img.close()
            except Exception:
                pass
        return False
    
    # Free native buffer immediately unless deferred
    if not defer_cleanup:
        try:
            chunk_img.close()
        except Exception:
            pass
    
    return True


def _batch_close_images(images):
    """Batch-close multiple AoImage objects to free native memory."""
    for img in images:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def locked(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        #result = fn(self, *args, **kwargs)
        with self._lock:
            result = fn(self, *args, **kwargs)
        return result
    return wrapped


class TimeBudget:
    """
    Track elapsed wall-clock time for a single X-Plane tile request.
    
    This class solves the per-chunk vs per-request timeout problem:
    - Previously, each chunk had its own maxwait timeout, leading to
      serialized chunks taking N * maxwait total time
    - Now, all chunks share a single time budget, ensuring the total
      wall-clock time stays close to the configured limit
    
    Usage:
        budget = TimeBudget(max_seconds=2.0)
        while not budget.exhausted:
            chunk_ready = budget.wait_with_budget(chunk.ready)
            if not chunk_ready:
                break  # Budget exhausted or event not set
    
    Thread-safety: Uses time.monotonic() which is thread-safe and immune
    to system clock adjustments.
    """
    
    # Minimum wait granularity - how often we check if budget is exhausted
    # during a wait. Smaller = more responsive but slightly more CPU.
    # 50ms is a good balance: responsive enough for UI, not too chatty.
    WAIT_GRANULARITY_SEC = 0.05
    
    def __init__(self, max_seconds: float):
        """
        Initialize a time budget.
        
        Args:
            max_seconds: Maximum wall-clock time allowed for this request.
                        This should be the value the user expects X-Plane
                        to actually wait, not a per-chunk timeout.
        """
        self.max_seconds = max_seconds
        self.start_time = time.monotonic()
        self._exhausted = False
        self._chunks_processed = 0
        self._chunks_skipped = 0
    
    @property
    def remaining(self) -> float:
        """Return remaining time in seconds (never negative)."""
        return max(0.0, self.max_seconds - self.elapsed)
    
    @property 
    def elapsed(self) -> float:
        """Return elapsed time since budget creation in seconds."""
        return time.monotonic() - self.start_time
    
    @property
    def exhausted(self) -> bool:
        """
        Check if the time budget is exhausted.
        
        Once exhausted, always returns True (sticky flag for efficiency).
        """
        if self._exhausted:
            return True
        if self.elapsed >= self.max_seconds:
            self._exhausted = True
            return True
        return False
    
    def wait_with_budget(self, event: threading.Event, max_single_wait: float = None) -> bool:
        """
        Wait on an event while respecting both the time budget AND an optional per-chunk maxwait.
        
        This combines two timeout mechanisms:
        1. Time budget: Total wall-clock time for the entire tile
        2. Max single wait: Per-chunk timeout (like the old maxwait parameter)
        
        Args:
            event: A threading.Event to wait on (e.g., chunk.ready)
            max_single_wait: Optional per-chunk timeout in seconds. If provided,
                           the wait will not exceed this time even if budget remains.
                           This corresponds to the old "maxwait" config setting.
        
        Returns:
            True if the event was set (success)
            False if budget exhausted or max_single_wait exceeded before event was set
        
        Behavior:
            - If event is already set, returns immediately with True
            - If budget is already exhausted, returns event.is_set() immediately
            - Otherwise, waits up to min(remaining_budget, max_single_wait)
        """
        # Fast path: already set
        if event.is_set():
            return True
        
        # Fast path: budget already gone
        if self.exhausted:
            return event.is_set()
        
        # Track start time for max_single_wait
        single_wait_start = time.monotonic() if max_single_wait else None
        
        # Poll with granularity until event set or budget/maxwait exhausted
        while not self.exhausted:
            # Check max_single_wait limit
            if max_single_wait is not None:
                single_elapsed = time.monotonic() - single_wait_start
                if single_elapsed >= max_single_wait:
                    log.debug(f"max_single_wait ({max_single_wait:.2f}s) exceeded")
                    break
                single_remaining = max_single_wait - single_elapsed
            else:
                single_remaining = float('inf')
            
            # Wait the minimum of: budget remaining, single wait remaining, granularity
            wait_time = min(self.remaining, single_remaining, self.WAIT_GRANULARITY_SEC)
            if wait_time <= 0:
                break
            if event.wait(timeout=wait_time):
                return True
        
        # Final check after loop exits
        return event.is_set()
    
    def record_chunk_processed(self):
        """Record that a chunk was successfully processed."""
        self._chunks_processed += 1
    
    def record_chunk_skipped(self):
        """Record that a chunk was skipped due to budget exhaustion."""
        self._chunks_skipped += 1
    
    @property
    def chunks_processed(self) -> int:
        """Number of chunks successfully processed within budget."""
        return self._chunks_processed
    
    @property
    def chunks_skipped(self) -> int:
        """Number of chunks skipped due to budget exhaustion."""
        return self._chunks_skipped
    
    def __repr__(self):
        return (f"TimeBudget(max={self.max_seconds:.2f}s, "
                f"elapsed={self.elapsed:.2f}s, "
                f"remaining={self.remaining:.2f}s, "
                f"exhausted={self.exhausted})")


class Getter(object):
    queue = None
    workers = None
    WORKING = None
    session = None

    def __init__(self, num_workers):
        
        self.count = 0
        self.queue = PriorityQueue()
        self.workers = []
        self.WORKING = threading.Event()
        self.WORKING.set()
        self.localdata = threading.local()
        # Thread-local sessions created in worker() to avoid shared-state contention

        for i in range(num_workers):
            t = threading.Thread(target=self.worker, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)

        #self.stat_t = t = threading.Thread(target=self.show_stats, daemon=True)
        #self.stat_t.start()


    def stop(self):
        self.WORKING.clear()
        
        # Drain queue to unblock workers waiting on queue.get()
        try:
            while True:
                self.queue.get_nowait()
        except Empty:
            pass
        
        # Join workers with timeout to prevent hanging on shutdown
        for t in self.workers:
            t.join(timeout=5.0)
            if t.is_alive():
                log.warning(f"Worker thread {t.name} did not terminate within 5s")
        
        # If a stats thread was started, join it as well
        stat_thread = getattr(self, 'stat_t', None)
        if stat_thread is not None:
            stat_thread.join(timeout=2.0)

    def worker(self, idx):
        global STATS
        self.localdata.idx = idx
        
        # Create thread-local session with connection pooling
        try:
            pool_size = max(4, int(int(CFG.autoortho.fetch_threads) * 1.5))
            self.localdata.session = create_http_session(pool_size=pool_size)
        except Exception as _e:
            log.warning(f"Failed to initialize thread-local session: {_e}")
            self.localdata.session = requests.Session()
        
        while self.WORKING.is_set():
            try:
                obj, args, kwargs = self.queue.get(timeout=5)
                #log.debug(f"Got: {obj} {args} {kwargs}")
            except Empty:
                #log.debug(f"timeout, continue")
                #log.info(f"Got {self.counter}")
                continue

            #STATS.setdefault('count', 0) + 1
            bump('count', 1)

            try:
                # Mark chunk as in-flight (Chunk always has these attributes)
                obj.in_queue = False
                obj.in_flight = True
                
                if not self.get(obj, *args, **kwargs):
                    # Check if chunk is permanently failed before re-submitting
                    if obj.permanent_failure:
                        log.debug(f"Chunk {obj} permanently failed ({obj.failure_reason}), not re-submitting")
                        continue
                    log.warning(f"Failed getting: {obj} {args} {kwargs}, re-submit.")
                    # CRITICAL: Clear in_flight BEFORE re-submitting, otherwise submit()
                    # will see in_flight=True and silently drop the chunk!
                    obj.in_flight = False
                    self.submit(obj, *args, **kwargs)
            except Exception as err:
                log.error(f"ERROR {err} getting: {obj} {args} {kwargs}, re-submit.")
                # Don't re-submit if permanently failed
                if obj.permanent_failure:
                    log.debug(f"Chunk {obj} permanently failed during exception, not re-submitting")
                    continue
                # CRITICAL: Clear in_flight BEFORE re-submitting
                obj.in_flight = False
                self.submit(obj, *args, **kwargs)
            finally:
                obj.in_flight = False
        
        # Worker loop ended - cleanup thread-local HTTP session
        try:
            session = getattr(self.localdata, 'session', None)
            if session is not None:
                session.close()
                self.localdata.session = None
        except Exception:
            pass

    def get(obj, *args, **kwargs):
        raise NotImplementedError

    def submit(self, obj, *args, **kwargs):
        # Don't queue permanently failed chunks (Chunk always has this attribute)
        if obj.permanent_failure:
            bump('submit_skip_permanent_failure')
            return
        # Coalesce duplicate chunk submissions
        if obj.ready.is_set():
            bump('submit_skip_already_ready')
            return  # Already done
        if obj.in_queue:
            bump('submit_skip_already_queued')
            return  # Already queued
        if obj.in_flight:
            bump('submit_skip_in_flight')
            return  # Currently downloading
        obj.in_queue = True
        bump('submit_queued')
        self.queue.put((obj, args, kwargs))


class ChunkGetter(Getter):
    # Track in-progress chunk_ids GLOBALLY to prevent queueing duplicates
    _queued_chunk_ids = set()
    _queued_lock = threading.Lock()

    def submit(self, obj, *args, **kwargs):
        """Submit chunk for download with duplicate prevention."""
        if obj.permanent_failure:
            bump('submit_skip_permanent_failure')
            return
        
        chunk_id = getattr(obj, 'chunk_id', None)
        
        # Check if already queued (fast path)
        if chunk_id:
            with self._queued_lock:
                if chunk_id in self._queued_chunk_ids:
                    bump('submit_skip_id_already_queued')
                    return
        
        # Per-object coalescing checks
        if obj.ready.is_set():
            bump('submit_skip_already_ready')
            return
        if obj.in_queue:
            bump('submit_skip_already_queued')
            return
        if obj.in_flight:
            bump('submit_skip_in_flight')
            return
        
        obj.in_queue = True
        
        # Add to queue
        if chunk_id:
            with self._queued_lock:
                self._queued_chunk_ids.add(chunk_id)
        
        bump('submit_queued')
        self.queue.put((obj, args, kwargs))

    def show_stats(self):
        while self.WORKING.is_set():
            log.info(f"{self.__class__.__name__} got: {self.count}")
            time.sleep(10)
        log.info(f"Exiting {self.__class__.__name__} stat thread.  Got: {self.count} total")

    def get(self, obj, *args, **kwargs):
        if obj.ready.is_set():
            log.debug(f"{obj} already retrieved.  Exit")
            bump('chunk_get_already_ready')
            return True

        kwargs['idx'] = self.localdata.idx
        kwargs['session'] = getattr(self.localdata, 'session', None) or requests
        result = obj.get(*args, **kwargs)
        
        chunk_id = getattr(obj, 'chunk_id', None)
        if chunk_id:
            with self._queued_lock:
                self._queued_chunk_ids.discard(chunk_id)
                if result:
                    bump('chunk_download_completed')
        
        return result


def _create_chunk_getter(num_workers: int):
    """
    Factory function to create the chunk getter.
    
    Uses Python ChunkGetter with per-thread connection pooling via requests.Session.
    Each worker thread maintains its own HTTP session for connection reuse.
    
    Native aopipeline components (AoCache, AoDDS) are used for cache I/O and
    DDS building where parallel native processing provides clear benefits.
    """
    log.info(f"Using ChunkGetter ({num_workers} workers)")
    return ChunkGetter(num_workers)


chunk_getter = _create_chunk_getter(int(CFG.autoortho.fetch_threads))

#class TileGetter(Getter):
#    def get(self, obj, *args, **kwargs):
#        log.debug(f"{obj}, {args}, {kwargs}")
#        return obj.get(*args)
#
#tile_getter = TileGetter(8)

log.info(f"chunk_getter: {chunk_getter}")

# ============================================================================
# NATIVE PIPELINE WARMUP
# ============================================================================
# Pre-warm the native decode pipeline for optimal first-tile performance.
# This initializes persistent TurboJPEG handles, OpenMP thread pool, and
# pre-faults buffer pool memory pages to eliminate cold-start latency.
# ============================================================================

def _warmup_native_pipeline():
    """Pre-warm native pipeline for optimal first-tile performance."""
    try:
        try:
            from autoortho.aopipeline import AoDecode
        except ImportError:
            try:
                from aopipeline import AoDecode
            except ImportError:
                return  # Native not available
        
        if not AoDecode.is_available():
            return
        
        # Create a temporary pool for warmup (will be destroyed after warmup)
        pool = AoDecode.create_pool(64)
        if pool:
            try:
                AoDecode.warmup_full(pool)
                log.info("Native decode pipeline pre-warmed")
            finally:
                pool.destroy()
    except Exception as e:
        log.debug(f"Native warmup failed: {e}")

# Call warmup during module init if native pipeline is being used
if get_pipeline_mode() != PIPELINE_MODE_PYTHON:
    _warmup_native_pipeline()
#log.info(f"tile_getter: {tile_getter}")


# ============================================================================
# ASYNC CACHE WRITER
# ============================================================================
# A lightweight executor for background cache writes. This allows downloaded
# chunks to be marked as "ready" immediately after download, rather than
# waiting for disk I/O to complete. Cache writes are fire-and-forget since
# a failed write only affects future cache hits, not current processing.
#
# Using 2 workers: cache writes are I/O-bound, not CPU-bound, so a small pool
# is sufficient. More workers would just contend on disk I/O.
# ============================================================================
_cache_write_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, 
    thread_name_prefix="cache_writer"
)


def _async_cache_write(chunk):
    """Fire-and-forget cache write. Errors are logged but don't affect processing."""
    try:
        # Check if cache directory still exists (may have been cleaned up by temp directory)
        # This prevents errors when diagnose() temp directories are deleted before async write
        if not os.path.exists(chunk.cache_dir):
            log.debug(f"Cache dir gone for {chunk}, skipping async write")
            return
        chunk.save_cache()
    except (FileNotFoundError, OSError) as e:
        # Directory may have been deleted between check and write (race condition)
        # This is expected for temporary directories, so just log at debug level
        log.debug(f"Async cache write skipped for {chunk}: {e}")
    except Exception as e:
        log.debug(f"Async cache write failed for {chunk}: {e}")


def shutdown_cache_writer():
    """Shutdown the cache writer executor gracefully. Called during module cleanup.
    
    Uses wait=True with a timeout to ensure pending writes complete before shutdown,
    preventing potential file corruption or lost cache writes.
    """
    try:
        # Use wait=True to ensure pending writes complete
        # Python 3.9+ supports cancel_futures parameter for forceful cancellation
        _cache_write_executor.shutdown(wait=True)
    except Exception:
        pass


def flush_cache_writer(timeout=30.0):
    """Wait for all pending cache writes to complete.
    
    This is useful for tests that need to verify cache contents after
    get_img() returns. Since cache writes are asynchronous, you need to
    call this before checking for cached files.
    
    This function shuts down the executor and recreates it to ensure
    all pending tasks complete. This is the only reliable way to guarantee
    all writes are flushed on all platforms (especially Windows).
    
    Args:
        timeout: Maximum time to wait in seconds (default: 30.0)
    """
    global _cache_write_executor
    try:
        # Shutdown with wait=True ensures ALL pending tasks complete
        _cache_write_executor.shutdown(wait=True)
    except Exception:
        pass
    finally:
        # Recreate the executor for subsequent operations
        _cache_write_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="cache_writer"
        )


# ============================================================================
# TERRAIN TILE LOOKUP
# ============================================================================
# Provides on-demand lookup of .ter files to discover actual zoom levels.
# Uses lazy file existence checks instead of pre-indexing to handle
# sceneries with millions of .ter files efficiently.
#
# Critical for predictive DDS generation: Without this, the prefetcher might
# guess ZL16 based on altitude, but if the .ter file says ZL18 (airport tile),
# the prefetched DDS would be at the wrong zoom level - a complete miss!
#
# Design for scale:
# - NO pre-indexing (avoids 600MB+ RAM for 5M tiles)
# - NO startup delay (avoids 30-60s scan)
# - Lazy os.path.exists() checks (~0.1ms each, only when needed)
#
# Maptype handling:
# - .ter files are always "BI" in standard AutoOrtho sceneries
# - Custom Ortho4XP tiles may use other maptypes (EOX, Arc, etc.)
# - We default to checking only "BI" for efficiency
# - When X-Plane requests a DDS with a different maptype, we add it to
#   the known maptypes set and check it in future lookups
# ============================================================================

# Module-level set of discovered maptypes from actual X-Plane requests
# Starts with just "BI", grows if custom tiles use other providers
_discovered_maptypes: set = {"BI"}
_discovered_maptypes_lock = threading.Lock()


def register_discovered_maptype(maptype: str) -> None:
    """
    Register a maptype discovered from an actual X-Plane DDS request.
    
    Called from FUSE layer when X-Plane requests a DDS with a maptype
    other than "BI". This adapts the terrain lookup to also check
    for custom tile maptypes.
    """
    global _discovered_maptypes
    with _discovered_maptypes_lock:
        if maptype not in _discovered_maptypes:
            _discovered_maptypes.add(maptype)
            log.info(f"TerrainTileLookup: Discovered custom maptype '{maptype}', "
                    f"will now check for it in terrain lookups")


def get_discovered_maptypes() -> set:
    """Get the current set of discovered maptypes."""
    with _discovered_maptypes_lock:
        return _discovered_maptypes.copy()


class TerrainTileLookup:
    """
    Lazy lookup of .ter files in scenery terrain folders.
    
    Instead of pre-indexing millions of files (which would use 600MB+ RAM
    and take 30-60s at startup), this class performs on-demand file
    existence checks when tiles are needed.
    
    Maptype strategy:
    - Default: Check only "BI" (standard for AutoOrtho sceneries)
    - Adaptive: When X-Plane requests a DDS with a different maptype,
      it gets registered and future lookups will also check for it
    - This handles custom Ortho4XP tiles without upfront I/O penalty
    
    Cost per prefetch cycle:
    - 1 maptype × 6 zooms × os.path.exists() = ~0.6ms (uncached)
    - Additional maptypes only added when evidence of custom tiles
    
    File format checked:
    - {row}_{col}_{maptype}{zoom}.ter
    
    Example: 10880_10432_BI16.ter → row=10880, col=10432, maptype=BI, zoom=16
    """
    
    def __init__(self, terrain_folder: str, scenery_name: str):
        """
        Args:
            terrain_folder: Path to the terrain folder (e.g., .../z_ao_na/terrain)
            scenery_name: Human-readable name for logging (e.g., "z_ao_na")
        """
        self._terrain_folder = terrain_folder
        self._scenery_name = scenery_name
        self._folder_exists = os.path.isdir(terrain_folder)
        
        # Simple LRU cache for recent lookups to avoid repeated fs checks
        # Key: (row, col, maptype, zoom) → Value: bool (exists)
        self._cache: Dict[Tuple[int, int, str, int], bool] = {}
        self._cache_max_size = 10000  # ~1MB max
        self._cache_lock = threading.Lock()
        
        # Stats
        self._lookups = 0
        self._cache_hits = 0
        self._files_found = 0
        
        if self._folder_exists:
            log.info(f"TerrainTileLookup: Ready for {scenery_name} at {terrain_folder}")
        else:
            log.warning(f"TerrainTileLookup: Folder not found: {terrain_folder}")
    
    def get_tiles_for_position(self, lat: float, lon: float,
                               maptype_filter: Optional[str] = None,
                               zoom_range: Tuple[int, int] = (14, 19)
                               ) -> List[Tuple[int, int, str, int]]:
        """
        Find all .ter files that exist at a geographic position.
        
        Performs lazy file existence checks for each zoom level.
        
        Args:
            lat: Latitude in degrees
            lon: Longitude in degrees
            maptype_filter: Maptype to check (e.g., "BI"). If None, checks common types.
            zoom_range: (min_zoom, max_zoom) to check (default 14-19 for perf)
        
        Returns:
            List of (row, col, maptype, zoom) tuples for tiles that exist.
        """
        if not self._folder_exists:
            return []
        
        results = []
        
        # Get maptypes to check:
        # 1. If specific maptype requested, use only that
        # 2. Otherwise, use all discovered maptypes (starts with just "BI",
        #    grows if X-Plane requests DDS files with other maptypes)
        if maptype_filter:
            maptypes_to_check = [maptype_filter]
        else:
            maptypes_to_check = list(get_discovered_maptypes())
        
        for zoom in range(zoom_range[0], zoom_range[1] + 1):
            # Convert lat/lon to tile coords at this zoom
            row, col = self._latlon_to_tile(lat, lon, zoom)
            
            for maptype in maptypes_to_check:
                if self._tile_exists(row, col, maptype, zoom):
                    results.append((row, col, maptype, zoom))
                    # Found a tile at this zoom, no need to check other maptypes
                    break
        
        return results
    
    def _tile_exists(self, row: int, col: int, maptype: str, zoom: int) -> bool:
        """
        Check if a specific .ter file exists.
        
        Uses a small cache to avoid repeated filesystem checks for the same tile.
        """
        cache_key = (row, col, maptype, zoom)
        self._lookups += 1
        
        # Check cache first
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache_hits += 1
                return self._cache[cache_key]
        
        # Build path and check filesystem
        ter_filename = f"{row}_{col}_{maptype}{zoom}.ter"
        ter_path = os.path.join(self._terrain_folder, ter_filename)
        exists = os.path.exists(ter_path)
        
        if exists:
            self._files_found += 1
        
        # Cache the result
        with self._cache_lock:
            # Simple eviction: clear half when full
            if len(self._cache) >= self._cache_max_size:
                # Keep most recent half (arbitrary, but simple)
                keys_to_remove = list(self._cache.keys())[:self._cache_max_size // 2]
                for key in keys_to_remove:
                    del self._cache[key]
            self._cache[cache_key] = exists
        
        return exists
    
    def has_tile(self, row: int, col: int, maptype: str, zoom: int) -> bool:
        """Check if a specific tile exists."""
        return self._tile_exists(row, col, maptype, zoom)
    
    # Tile grid alignment: .ter files are placed every 16 slippy coordinates
    # because each tile covers a 16×16 chunk grid
    TILE_GRID_STEP = 16
    
    @staticmethod
    def _latlon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
        """
        Convert lat/lon to tile coordinates at a specific zoom level.
        
        Uses Web Mercator (Slippy Map) convention, then aligns to tile grid.
        
        IMPORTANT: .ter files are placed every 16 slippy coordinates because
        each tile covers a 16×16 chunk grid. Example:
        - 144224_260256_BI18.ter covers slippy coords 260256-260271
        - 144224_260272_BI18.ter covers slippy coords 260272-260287
        
        So we round DOWN to the nearest multiple of 16 to get the .ter filename.
        
        Returns:
            (row, col) tuple aligned to 16-tile grid - NOTE: row is Y, col is X
        """
        n = 2 ** zoom
        raw_col = int((lon + 180) / 360 * n)
        
        # Clamp latitude to valid Mercator range
        lat_clamped = max(-85.0511, min(85.0511, lat))
        lat_rad = math.radians(lat_clamped)
        raw_row = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
        
        # Align to tile grid (every 16 slippy coordinates)
        step = TerrainTileLookup.TILE_GRID_STEP
        row = (raw_row // step) * step
        col = (raw_col // step) * step
        
        return (row, col)
    
    def clear_cache(self) -> None:
        """Clear the lookup cache."""
        with self._cache_lock:
            self._cache.clear()
    
    @property
    def is_ready(self) -> bool:
        """Always ready (no async indexing needed)."""
        return self._folder_exists
    
    @property
    def stats(self) -> dict:
        """Return lookup statistics."""
        hit_rate = (self._cache_hits / self._lookups * 100) if self._lookups > 0 else 0
        return {
            'scenery': self._scenery_name,
            'lookups': self._lookups,
            'cache_hits': self._cache_hits,
            'hit_rate_pct': hit_rate,
            'files_found': self._files_found,
            'cache_size': len(self._cache),
            'folder_exists': self._folder_exists,
        }


# Module-level terrain lookup management
_terrain_lookups: List[TerrainTileLookup] = []
_terrain_lookups_lock = threading.Lock()


def register_terrain_index(terrain_folder: str, scenery_name: str) -> None:
    """
    Register a terrain lookup for a scenery.
    
    Called from autoortho_fuse.py when mounting a scenery.
    No async indexing needed - lookups are lazy.
    """
    global _terrain_lookups
    with _terrain_lookups_lock:
        lookup = TerrainTileLookup(terrain_folder, scenery_name)
        _terrain_lookups.append(lookup)
        log.info(f"Registered terrain lookup for {scenery_name}")


def get_all_tiles_for_position(lat: float, lon: float,
                               maptype_filter: Optional[str] = None) -> List[Tuple[int, int, str, int]]:
    """
    Query all terrain lookups for tiles at a position.
    
    Args:
        lat: Latitude in degrees
        lon: Longitude in degrees
        maptype_filter: Optional maptype to filter by
        
    Returns:
        List of (row, col, maptype, zoom) tuples from all sceneries
    """
    results = []
    with _terrain_lookups_lock:
        for lookup in _terrain_lookups:
            results.extend(lookup.get_tiles_for_position(lat, lon, maptype_filter))
    return results


def unregister_terrain_index(scenery_name: str) -> bool:
    """
    Unregister a terrain lookup for a specific scenery.
    
    Called when unmounting a scenery to free associated memory.
    
    Args:
        scenery_name: Name of the scenery to unregister
        
    Returns:
        True if the scenery was found and removed, False otherwise
    """
    global _terrain_lookups
    with _terrain_lookups_lock:
        for i, lookup in enumerate(_terrain_lookups):
            if lookup._scenery_name == scenery_name:
                lookup.clear_cache()
                _terrain_lookups.pop(i)
                log.info(f"Unregistered terrain lookup for {scenery_name}")
                return True
    log.debug(f"Terrain lookup for {scenery_name} not found")
    return False


def clear_terrain_indices() -> None:
    """Clear all terrain lookups (for shutdown)."""
    global _terrain_lookups
    with _terrain_lookups_lock:
        for lookup in _terrain_lookups:
            lookup.clear_cache()
        count = len(_terrain_lookups)
        _terrain_lookups.clear()
        log.info(f"Cleared {count} terrain lookups")


def get_terrain_index_stats() -> List[dict]:
    """Get statistics from all terrain lookups."""
    with _terrain_lookups_lock:
        return [lookup.stats for lookup in _terrain_lookups]


# ============================================================================
# SPATIAL PREFETCHER
# ============================================================================
# Proactively downloads tile chunks ahead of the aircraft based on position
# and heading. This reduces in-flight stutters by having tiles ready before
# X-Plane requests them.
#
# Key design principles:
# 1. Low priority: Prefetched chunks don't compete with immediate requests
# 2. Configurable: Users can enable/disable and tune parameters
# 3. Conservative: Doesn't overwhelm network or CPU resources
# 4. Non-blocking: Runs in background, never blocks main processing
# ============================================================================

class SpatialPrefetcher:
    """
    Background prefetcher that anticipates tile needs based on aircraft movement.
    
    The prefetcher periodically checks aircraft position and heading, calculates
    which tiles will be needed, and submits low-priority download requests.
    """
    
    # Priority offset for prefetched chunks (higher = lower priority)
    # This ensures prefetch doesn't compete with immediate X-Plane requests
    PREFETCH_PRIORITY_OFFSET = 100
    
    # Minimum speed (m/s) to trigger prefetching - don't prefetch when taxiing
    MIN_SPEED_MPS = 25  # ~50 knots
    
    def __init__(self):
        """Initialize the prefetcher with configuration from aoconfig."""
        self._thread = None
        self._stop_event = threading.Event()
        self._running = False
        self._prefetch_count = 0
        self._tile_cacher = None
        
        # Track recently prefetched to avoid duplicates
        # Use an LRU-like structure with max size
        self._recently_prefetched = set()
        self._max_recent = 500
        
        # Load configuration
        self._load_config()
        
    def _load_config(self):
        """Load prefetch configuration from aoconfig."""
        self.enabled = getattr(CFG.autoortho, 'prefetch_enabled', True)
        # Lookahead is now configured in MINUTES, convert to seconds
        # 0 = Unlimited (use very large lookahead, effectively infinite)
        lookahead_min = float(getattr(CFG.autoortho, 'prefetch_lookahead', 10))
        self.lookahead_unlimited = (lookahead_min <= 0)
        if self.lookahead_unlimited:
            # Use 24 hours as "unlimited" - effectively infinite for flight purposes
            self.lookahead_sec = 24 * 60 * 60  # 86400 seconds
        else:
            self.lookahead_sec = lookahead_min * 60  # Convert minutes to seconds
            # Clamp to reasonable range (1-60 minutes = 60-3600 seconds)
            self.lookahead_sec = max(60, min(3600, self.lookahead_sec))
        
        self.interval_sec = float(getattr(CFG.autoortho, 'prefetch_interval', 2.0))
        self.max_chunks = int(getattr(CFG.autoortho, 'prefetch_max_chunks', 24))
        
        # Unified prefetch radius (used by both velocity and SimBrief methods)
        # This replaces the old simbrief-specific route_prefetch_radius_nm
        self.prefetch_radius_nm = float(getattr(CFG.autoortho, 'prefetch_radius_nm', 40))
        self.prefetch_radius_nm = max(10, min(150, self.prefetch_radius_nm))
        
        self.interval_sec = max(1.0, min(10.0, self.interval_sec))
        self.max_chunks = max(8, min(512, self.max_chunks))
        
    def set_tile_cacher(self, tile_cacher):
        """Set the tile cacher reference for accessing tiles."""
        self._tile_cacher = tile_cacher
        
    def start(self):
        """Start the background prefetcher thread."""
        if self._running:
            return
            
        if not self.enabled:
            log.info("Spatial prefetcher disabled by configuration")
            return
            
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._prefetch_loop,
            name="SpatialPrefetcher",
            daemon=True
        )
        self._thread.start()
        lookahead_str = "Unlimited" if self.lookahead_unlimited else f"{self.lookahead_sec/60:.0f}min"
        log.info(f"Spatial prefetcher started (lookahead={lookahead_str}, "
                f"interval={self.interval_sec}s, max_chunks={self.max_chunks})")
        
    def stop(self):
        """Stop the background prefetcher thread."""
        if not self._running:
            return
            
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        log.info(f"Spatial prefetcher stopped (prefetched {self._prefetch_count} chunks total)")
        bump('prefetch_total', self._prefetch_count)
        
    def _prefetch_loop(self):
        """Main prefetch loop - runs in background thread."""
        while not self._stop_event.is_set():
            try:
                self._do_prefetch_cycle()
            except Exception as e:
                log.debug(f"Prefetch cycle error: {e}")
            
            # Wait before next cycle, but allow early exit on stop
            self._stop_event.wait(timeout=self.interval_sec)
    
    def _do_prefetch_cycle(self):
        """
        Execute one prefetch cycle.

        If SimBrief flight data is loaded and enabled, prefetches along the
        flight plan waypoints. Otherwise, uses velocity-based prediction.
        
        Falls back to velocity-based prefetching if aircraft deviates from route.
        """
        # Check if tile_cacher is available
        if self._tile_cacher is None:
            return

        # Always use instantaneous position (that's where we actually are)
        if not datareftracker.data_valid or not datareftracker.connected:
            return
            
        lat = datareftracker.lat
        lon = datareftracker.lon

        # Validate position data
        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            return

        # Check if SimBrief flight path prefetching should be used
        if self._should_use_simbrief_prefetch(lat, lon):
            chunks_submitted = self._prefetch_along_flight_plan(lat, lon)
            if chunks_submitted > 0:
                self._prefetch_count += chunks_submitted
                log.debug(f"Prefetched {chunks_submitted} chunks along flight plan (total: {self._prefetch_count})")
                bump('prefetch_chunk_count', chunks_submitted)
            return
        
        # Fall back to velocity-based prefetching
        self._do_velocity_prefetch_cycle(lat, lon)
    
    def _should_use_simbrief_prefetch(self, lat: float, lon: float) -> bool:
        """
        Check if SimBrief flight path prefetching should be used.
        
        Returns True if:
        - SimBrief flight data is loaded
        - use_flight_data toggle is enabled
        - Aircraft is on-route (within deviation threshold) OR
        - prefetch_while_parked is enabled AND aircraft is near origin airport
        """
        # Check if SimBrief integration is enabled
        if not hasattr(CFG, 'simbrief'):
            return False
        
        use_flight_data = getattr(CFG.simbrief, 'use_flight_data', False)
        if isinstance(use_flight_data, str):
            use_flight_data = use_flight_data.lower() in ('true', '1', 'yes', 'on')
        
        if not use_flight_data:
            return False
        
        # Check if flight data is loaded
        if not simbrief_flight_manager.is_loaded:
            return False
        
        # Get deviation threshold from config
        deviation_threshold = float(getattr(CFG.simbrief, 'route_deviation_threshold_nm', 40))
        
        # Check if aircraft is on-route
        if simbrief_flight_manager.is_on_route(lat, lon, deviation_threshold):
            return True
        
        # If prefetch_while_parked is enabled, allow prefetching when near origin
        # This lets us start prefetching the route even before takeoff
        prefetch_while_parked = getattr(CFG.simbrief, 'prefetch_while_parked', True)
        if isinstance(prefetch_while_parked, str):
            prefetch_while_parked = prefetch_while_parked.lower() in ('true', '1', 'yes', 'on')
        
        if prefetch_while_parked:
            # Check if aircraft is near the origin airport (within 50nm)
            # This allows prefetching to start while parked at the gate
            origin_distance = simbrief_flight_manager.get_distance_from_origin(lat, lon)
            if origin_distance is not None and origin_distance < 50:
                log.debug(f"SimBrief prefetch: Aircraft near origin ({origin_distance:.1f}nm), enabling parked prefetch")
                return True
        
        return False
    
    def _prefetch_along_flight_plan(self, lat: float, lon: float) -> int:
        """
        Prefetch tiles along the SimBrief flight plan path with time-based priority.
        
        This method interpolates points along the entire flight path (not just at
        waypoints) and prioritizes tiles by time-to-encounter. Tiles the aircraft
        will reach sooner are prefetched first.
        
        The path is interpolated at regular intervals to ensure uniform coverage
        even when waypoints are far apart.
        
        Uses the flight plan altitude at each position to determine the
        appropriate zoom level, matching what will actually be displayed.
        
        Skips tiles that are already opened by X-Plane (on-demand logic handles those).
        
        Returns number of chunks submitted.
        """
        chunks_submitted = 0
        
        # Use unified prefetch radius from config
        prefetch_radius_nm = self.prefetch_radius_nm
        
        # Get interpolated path points with time-to-encounter
        # Uses SimBrief's pre-calculated times (accounts for winds, climb/descent, etc.)
        # Spacing determines how frequently we sample the path
        # Smaller spacing = more uniform coverage, but more computation
        spacing_nm = min(prefetch_radius_nm / 2, 15.0)  # Sample at half the radius or 15nm
        
        path_points = simbrief_flight_manager.get_path_points_with_time(
            aircraft_lat=lat,
            aircraft_lon=lon,
            lookahead_sec=self.lookahead_sec,
            spacing_nm=spacing_nm
        )
        
        if not path_points:
            # Fall back to waypoint-based prefetching if path generation fails
            return self._prefetch_along_flight_plan_waypoints(lat, lon)
        
        # Get maptype for checking if tiles are opened
        maptype_filter = self._get_maptype_filter()
        default_maptype = maptype_filter or "EOX"
        
        # Collect tiles along the path with their time-to-encounter
        # Use a dict to track earliest time for each tile (key = (row, col, zoom))
        tile_times: Dict[Tuple[int, int, int], Tuple[float, int]] = {}  # key -> (time, altitude_agl)
        
        for point in path_points:
            # Get zoom level for this point's altitude
            zoom_level = self._get_zoom_for_altitude(point.altitude_agl_ft)
            
            # Get tiles within radius of this path point
            tiles = self._get_tiles_in_radius(
                point.lat, point.lon, prefetch_radius_nm, zoom_level
            )
            
            # Track each tile with its earliest encounter time
            for (row, col) in tiles:
                tile_key = (row, col, zoom_level)
                
                # Skip if already in recently prefetched
                if tile_key in self._recently_prefetched:
                    continue
                
                # Keep the earliest time for each tile
                if tile_key not in tile_times:
                    tile_times[tile_key] = (point.time_to_reach_sec, point.altitude_agl_ft)
                elif point.time_to_reach_sec < tile_times[tile_key][0]:
                    tile_times[tile_key] = (point.time_to_reach_sec, point.altitude_agl_ft)
        
        # Sort tiles by time-to-encounter (earliest first = nearest in time)
        sorted_tiles = sorted(tile_times.items(), key=lambda x: x[1][0])
        
        log.debug(f"Path prefetch: {len(path_points)} path points, {len(sorted_tiles)} unique tiles to prefetch")
        
        tiles_prefetched = 0
        
        # Prefetch tiles in order of encounter time
        for (row, col, zoom), (time_sec, alt_agl) in sorted_tiles:
            if chunks_submitted >= self.max_chunks:
                break
            
            tile_key = (row, col, zoom)
            
            # Skip if tile is already opened by X-Plane (on-demand logic handles it)
            if self._tile_cacher and self._tile_cacher.is_tile_opened_by_xplane(row, col, default_maptype, zoom):
                log.debug(f"Skipping prefetch for {row},{col}@ZL{zoom} - already opened by X-Plane")
                continue
            
            # Add to recently prefetched
            self._recently_prefetched.add(tile_key)
            if len(self._recently_prefetched) > self._max_recent:
                try:
                    self._recently_prefetched.pop()
                except KeyError:
                    pass
            
            # Prefetch this tile
            submitted = self._prefetch_tile(row, col, zoom)
            chunks_submitted += submitted
            
            if submitted > 0:
                tiles_prefetched += 1
                log.debug(f"Prefetch tile ({row},{col}) ZL{zoom}: ETA={time_sec/60:.1f}min, alt={alt_agl}ft AGL")
        
        return chunks_submitted
    
    def _prefetch_along_flight_plan_waypoints(self, lat: float, lon: float) -> int:
        """
        Fallback: Prefetch tiles around waypoints only (legacy behavior).
        
        Used when path interpolation fails or for compatibility.
        Skips tiles already opened by X-Plane.
        
        Returns number of chunks submitted.
        """
        chunks_submitted = 0
        
        # Use unified prefetch radius from config
        prefetch_radius_nm = self.prefetch_radius_nm
        
        # Get upcoming fixes (limited number to avoid overwhelming)
        upcoming_fixes = simbrief_flight_manager.get_upcoming_fixes(lat, lon, count=15)
        
        if not upcoming_fixes:
            return 0
        
        # Prefetch around each upcoming fix, stopping when we hit max chunks
        for fix in upcoming_fixes:
            if chunks_submitted >= self.max_chunks:
                break
            
            # Determine zoom level based on the AGL altitude at this waypoint
            zoom_level = self._get_zoom_for_altitude(fix.altitude_agl_ft)
            
            log.debug(f"Prefetch fix {fix.ident}: MSL={fix.altitude_ft}ft, "
                     f"GND={fix.ground_height_ft}ft, AGL={fix.altitude_agl_ft}ft -> ZL{zoom_level}")
            
            # Prefetch tiles around this waypoint at the appropriate zoom level
            submitted = self._prefetch_waypoint_area(
                fix.lat, fix.lon, prefetch_radius_nm, zoom_level
            )
            chunks_submitted += submitted
        
        return chunks_submitted
    
    # Tile grid alignment: .ter files are placed every 16 slippy coordinates
    TILE_GRID_STEP = 16
    
    def _get_tiles_in_radius(self, center_lat: float, center_lon: float,
                              radius_nm: float, zoom: int) -> List[Tuple[int, int]]:
        """
        Get all tile coordinates (row, col) within a radius of a center point.
        
        IMPORTANT: Tiles are aligned to a 16-coordinate grid (each .ter covers
        16×16 slippy tiles). We step through the grid in increments of 16.
        
        Args:
            center_lat, center_lon: Center point coordinates
            radius_nm: Radius in nautical miles
            zoom: Zoom level for tile coordinates
            
        Returns:
            List of (row, col) tuples for tiles within the radius (grid-aligned)
        """
        tiles = []
        step = self.TILE_GRID_STEP
        
        # Convert radius to degrees (approximate)
        # 1 nm ≈ 1/60 degree latitude
        radius_deg_lat = radius_nm / 60.0
        radius_deg_lon = radius_nm / (60.0 * max(0.1, math.cos(math.radians(center_lat))))
        
        # Calculate tile coordinates
        n = 2 ** zoom
        
        def latlon_to_tile(lat, lon):
            """Convert lat/lon to grid-aligned tile coordinates."""
            raw_x = int((lon + 180) / 360 * n)
            lat_clamped = max(-85.0511, min(85.0511, lat))
            lat_rad = math.radians(lat_clamped)
            raw_y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
            # Align to tile grid (every 16 slippy coordinates)
            return ((raw_x // step) * step, (raw_y // step) * step)
        
        # Get tile range for the area (already grid-aligned)
        col_center, row_center = latlon_to_tile(center_lat, center_lon)
        col_min, row_min = latlon_to_tile(
            center_lat + radius_deg_lat,
            center_lon - radius_deg_lon
        )
        col_max, row_max = latlon_to_tile(
            center_lat - radius_deg_lat,
            center_lon + radius_deg_lon
        )
        
        # Limit the area to prevent too many tiles
        # Note: max_tiles_per_dim now counts actual tiles (each covers 16×16 area)
        max_tiles_per_dim = 5
        tiles_in_row = (row_max - row_min) // step + 1
        tiles_in_col = (col_max - col_min) // step + 1
        
        if tiles_in_row > max_tiles_per_dim:
            half_tiles = (max_tiles_per_dim // 2) * step
            row_min = row_center - half_tiles
            row_max = row_center + half_tiles
        if tiles_in_col > max_tiles_per_dim:
            half_tiles = (max_tiles_per_dim // 2) * step
            col_min = col_center - half_tiles
            col_max = col_center + half_tiles
        
        # Collect tiles in the area, stepping by 16 (tile grid)
        for row in range(row_min, row_max + 1, step):
            for col in range(col_min, col_max + 1, step):
                tiles.append((row, col))
        
        return tiles
    
    def _prefetch_waypoint_area(self, waypoint_lat: float, waypoint_lon: float,
                                  radius_nm: float, zoom: int) -> int:
        """
        Prefetch tiles within a radius around a waypoint.
        Skips tiles already opened by X-Plane.
        
        Returns number of chunks submitted.
        """
        chunks_submitted = 0
        
        # Get maptype for checking if tiles are opened
        maptype_filter = self._get_maptype_filter()
        default_maptype = maptype_filter or "EOX"
        
        # Convert radius to degrees (approximate)
        # 1 nm ≈ 1/60 degree latitude
        radius_deg_lat = radius_nm / 60.0
        radius_deg_lon = radius_nm / (60.0 * math.cos(math.radians(waypoint_lat)))
        
        # Calculate tile coordinates
        n = 2 ** zoom
        
        def latlon_to_tile(lat, lon):
            """Convert lat/lon to tile coordinates."""
            x = int((lon + 180) / 360 * n)
            lat_clamped = max(-85.0511, min(85.0511, lat))
            lat_rad = math.radians(lat_clamped)
            y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
            return (x, y)
        
        # Get tile range for the area
        col_center, row_center = latlon_to_tile(waypoint_lat, waypoint_lon)
        col_min, row_min = latlon_to_tile(
            waypoint_lat + radius_deg_lat,
            waypoint_lon - radius_deg_lon
        )
        col_max, row_max = latlon_to_tile(
            waypoint_lat - radius_deg_lat,
            waypoint_lon + radius_deg_lon
        )
        
        # Limit the area to prevent too many tiles
        max_tiles_per_dim = 3
        if row_max - row_min > max_tiles_per_dim:
            row_min = row_center - max_tiles_per_dim // 2
            row_max = row_center + max_tiles_per_dim // 2
        if col_max - col_min > max_tiles_per_dim:
            col_min = col_center - max_tiles_per_dim // 2
            col_max = col_center + max_tiles_per_dim // 2
        
        # Prefetch tiles in the area
        for row in range(row_min, row_max + 1):
            for col in range(col_min, col_max + 1):
                if chunks_submitted >= self.max_chunks:
                    return chunks_submitted
                
                # Create unique key for this tile
                tile_key = (row, col, zoom)
                
                # Skip if recently prefetched
                if tile_key in self._recently_prefetched:
                    continue
                
                # Skip if tile is already opened by X-Plane
                if self._tile_cacher and self._tile_cacher.is_tile_opened_by_xplane(row, col, default_maptype, zoom):
                    log.debug(f"Skipping prefetch for {row},{col}@ZL{zoom} - already opened by X-Plane")
                    continue
                
                # Add to recently prefetched
                self._recently_prefetched.add(tile_key)
                if len(self._recently_prefetched) > self._max_recent:
                    try:
                        self._recently_prefetched.pop()
                    except KeyError:
                        pass
                
                # Prefetch this tile
                submitted = self._prefetch_tile(row, col, zoom)
                chunks_submitted += submitted
        
        return chunks_submitted
    
    def _get_zoom_for_altitude(self, altitude_ft: int) -> int:
        """
        Get the appropriate zoom level for a given altitude.
        
        Uses the TileCacher's dynamic zoom manager if available and in dynamic mode.
        Falls back to fixed max_zoom otherwise.
        
        Args:
            altitude_ft: Altitude in feet
            
        Returns:
            Appropriate zoom level for the altitude
        """
        # Check if we have access to the tile cacher and it's in dynamic mode
        if self._tile_cacher is not None:
            max_zoom_mode = getattr(CFG.autoortho, 'max_zoom_mode', 'fixed')
            if max_zoom_mode == 'dynamic' and hasattr(self._tile_cacher, 'dynamic_zoom_manager'):
                zoom = self._tile_cacher.dynamic_zoom_manager.get_zoom_for_altitude(altitude_ft)
                log.debug(f"Dynamic zoom for altitude {altitude_ft}ft: ZL{zoom}")
                return zoom
        
        # Fall back to fixed max_zoom
        return int(getattr(CFG.autoortho, 'max_zoom', 16))
    
    def _get_predicted_altitude(self, lat: float, lon: float, 
                                  hdg: float, spd: float, 
                                  target_lat: float, target_lon: float) -> int:
        """
        Predict altitude at a target position based on current flight parameters.
        
        Uses vertical speed to estimate altitude change over the distance.
        
        Returns:
            Predicted altitude in feet
        """
        # Get current altitude AGL and vertical speed
        with datareftracker._lock:
            if not datareftracker.data_valid:
                return int(getattr(CFG.autoortho, 'max_zoom', 16))
            current_alt = datareftracker.alt_agl_ft
        
        averages = datareftracker.get_flight_averages()
        if averages is None:
            return int(current_alt)
        
        vs_fpm = averages.get('vertical_speed_fpm', 0)
        
        # Calculate distance to target
        delta_lat = target_lat - lat
        delta_lon = target_lon - lon
        cos_lat = math.cos(math.radians(lat))
        distance_m = math.sqrt((delta_lat * 111320) ** 2 + (delta_lon * 111320 * cos_lat) ** 2)
        
        # Calculate time to reach target
        if spd > 0:
            time_sec = distance_m / spd
        else:
            time_sec = 0
        
        # Predict altitude change
        alt_change = vs_fpm * (time_sec / 60)  # Convert seconds to minutes
        predicted_alt = current_alt + alt_change
        
        # Clamp to reasonable values
        return max(0, int(predicted_alt))
    
    def _do_velocity_prefetch_cycle(self, lat: float, lon: float):
        """
        Execute velocity-based prefetch cycle using radius-based sampling.
        
        Samples points along the predicted flight path at regular intervals,
        then collects tiles within the prefetch radius at each sample point.
        Tiles are prioritized by distance from the aircraft (nearest first).
        
        ZOOM LEVEL DETERMINATION:
        1. PRIMARY: Query TerrainZoomIndex to find actual .ter files at each position
           - This ensures we prefetch the exact tiles X-Plane will request
           - Critical for airport tiles (ZL18) which would be missed by altitude guessing
        2. FALLBACK: Use altitude-based zoom guessing if no terrain index available
        """
        # Try to get 60-second averaged flight data for stable prediction
        averages = datareftracker.get_flight_averages()

        if averages is not None:
            hdg = averages['heading']
            spd = averages['ground_speed_mps']
        else:
            hdg = datareftracker.hdg
            spd = datareftracker.spd

        # Don't prefetch if moving slowly (taxiing, parked)
        if spd < self.MIN_SPEED_MPS:
            return

        # Calculate max distance based on lookahead time
        max_distance_m = spd * self.lookahead_sec
        
        # Sample spacing: half the radius or 15nm max (like SimBrief approach)
        spacing_nm = min(self.prefetch_radius_nm / 2, 15.0)
        spacing_m = spacing_nm * 1852  # Convert nm to meters
        
        # Generate sample points along the flight path (nearest to farthest)
        sample_points = []
        distance_m = 0
        hdg_rad = math.radians(hdg)
        
        while distance_m <= max_distance_m:
            # Calculate position at this distance
            delta_lat = (distance_m * math.cos(hdg_rad)) / 111320
            cos_lat = math.cos(math.radians(lat))
            if cos_lat > 0.01:
                delta_lon = (distance_m * math.sin(hdg_rad)) / (111320 * cos_lat)
            else:
                delta_lon = 0
            
            sample_lat = lat + delta_lat
            sample_lon = lon + delta_lon
            
            # Estimate altitude at this position
            predicted_alt = self._get_predicted_altitude(
                lat, lon, hdg, spd, sample_lat, sample_lon
            )
            
            sample_points.append((sample_lat, sample_lon, distance_m, predicted_alt))
            distance_m += spacing_m
        
        if not sample_points:
            return
        
        # Collect tiles from all sample points, tracking distance for prioritization
        # Key: (row, col, maptype, zoom), Value: distance_m (nearest wins)
        tile_distances: Dict[Tuple[int, int, str, int], float] = {}
        
        maptype_filter = self._get_maptype_filter()
        
        for sample_lat, sample_lon, distance_m, predicted_alt in sample_points:
            # PRIMARY: Query TerrainZoomIndex for actual tiles
            tiles_from_index = get_all_tiles_for_position(
                sample_lat, sample_lon, maptype_filter=maptype_filter
            )
            
            if tiles_from_index:
                # Use indexed tiles - get all within radius of this sample point
                for row, col, maptype, zoom in tiles_from_index:
                    tile_key = (row, col, maptype, zoom)
                    # Keep the nearest distance for each tile
                    if tile_key not in tile_distances or distance_m < tile_distances[tile_key]:
                        tile_distances[tile_key] = distance_m
            else:
                # FALLBACK: Use altitude-based zoom guessing with radius
                zoom_level = self._get_zoom_for_altitude(predicted_alt)
                fallback_maptype = maptype_filter or "EOX"
                
                tiles_in_radius = self._get_tiles_in_radius(
                    sample_lat, sample_lon, self.prefetch_radius_nm, zoom_level
                )
                
                for row, col in tiles_in_radius:
                    tile_key = (row, col, fallback_maptype, zoom_level)
                    if tile_key not in tile_distances or distance_m < tile_distances[tile_key]:
                        tile_distances[tile_key] = distance_m
        
        # Sort tiles by distance (nearest first - gradient from player outward)
        sorted_tiles = sorted(tile_distances.items(), key=lambda x: x[1])
        
        # Prefetch tiles in order of proximity
        chunks_submitted = 0
        tiles_prefetched = 0
        
        for (row, col, maptype, zoom), distance in sorted_tiles:
            if chunks_submitted >= self.max_chunks:
                break
            
            tile_key = (row, col, maptype, zoom)
            
            # Skip if recently prefetched
            if tile_key in self._recently_prefetched:
                continue
            
            # Skip if tile is already opened by X-Plane (on-demand logic handles it)
            if self._tile_cacher and self._tile_cacher.is_tile_opened_by_xplane(row, col, maptype, zoom):
                log.debug(f"Skipping prefetch for {row},{col}@ZL{zoom} - already opened by X-Plane")
                continue
            
            # Add to recently prefetched
            self._recently_prefetched.add(tile_key)
            if len(self._recently_prefetched) > self._max_recent:
                try:
                    self._recently_prefetched.pop()
                except KeyError:
                    pass
            
            submitted = self._prefetch_tile(row, col, zoom, maptype)
            chunks_submitted += submitted
            if submitted > 0:
                tiles_prefetched += 1
        
        if chunks_submitted > 0:
            self._prefetch_count += chunks_submitted
            log.debug(f"Velocity prefetch: {chunks_submitted} chunks from {tiles_prefetched} tiles "
                     f"(radius={self.prefetch_radius_nm}nm, total: {self._prefetch_count})")
            bump('prefetch_chunk_count', chunks_submitted)
    
    def _get_maptype_filter(self) -> Optional[str]:
        """
        Get the maptype to filter by when querying terrain index.
        
        Returns:
            Maptype string (e.g., "BI", "EOX") if override is set, None otherwise.
            When None, all maptypes are accepted from the terrain index.
        """
        if self._tile_cacher is not None:
            override = getattr(self._tile_cacher, 'maptype_override', None)
            if override and override != "Use tile default":
                return override
        return None  # Accept any maptype from terrain index
    
    # Maximum chunks to submit per tile per prefetch cycle
    # With 5 mipmaps (0-4), a ZL16 tile has 256+64+16+4+1 = 341 chunks total
    # We limit per-tile to avoid one tile dominating the queue
    MAX_CHUNKS_PER_TILE = 32
    
    def _prefetch_tile(self, row, col, zoom, maptype: Optional[str] = None):
        """
        Submit prefetch requests for a tile's chunks at ALL mipmap levels.
        
        Args:
            row: Tile row coordinate
            col: Tile column coordinate  
            zoom: Zoom level (max zoom for this tile)
            maptype: Optional maptype (e.g., "BI", "EOX"). If None, uses config.
        
        Returns number of chunks submitted.
        
        Downloads chunks for all mipmap levels (ZL16, ZL15, ZL14, etc.) to ensure
        the prebuilt DDS matches on-demand behavior where X-Plane may request
        any mipmap level first and get native-resolution chunks.
        
        IMPORTANT: Uses _open_tile()/_close_tile() pair to properly manage refs.
        This ensures prefetched tiles can be evicted when no longer needed.
        
        If X-Plane has also opened this tile (refs > 1 after our open), we drop
        it and let the on-demand tile build logic handle it instead.
        """
        # Get maptype from parameter or config
        if maptype is None:
            maptype = getattr(CFG.autoortho, 'maptype_override', None)
            if not maptype or maptype == "Use tile default":
                maptype = "EOX"
        
        tile = None
        try:
            # Use _open_tile() to properly increment refs (balanced with _close_tile below)
            # This ensures prefetched tiles don't accumulate refs and block eviction.
            tile = self._tile_cacher._open_tile(row, col, maptype, zoom)
            if not tile:
                return 0
            
            # If tile has refs > 1, X-Plane has also opened it - drop and let on-demand handle
            # Our _open_tile incremented refs to 1 (new) or +1 (existing). If refs > 1 now,
            # it means X-Plane is actively using this tile.
            if tile.refs > 1:
                log.debug(f"Tile {row},{col}@ZL{zoom} has refs={tile.refs}, X-Plane is using it - dropping prefetch")
                return 0
            
            submitted = 0
            
            # =========================================================================
            # Download chunks for ALL mipmap levels (not just mipmap 0)
            # This ensures prebuilt DDS matches on-demand behavior where X-Plane
            # may request any mipmap first and get native-resolution chunks.
            #
            # Mipmap levels and their zoom:
            #   mipmap 0 → max_zoom (ZL16) → 256 chunks (highest detail)
            #   mipmap 1 → max_zoom-1 (ZL15) → 64 chunks
            #   mipmap 2 → max_zoom-2 (ZL14) → 16 chunks
            #   mipmap 3 → max_zoom-3 (ZL13) → 4 chunks
            #   mipmap 4 → max_zoom-4 (ZL12) → 1 chunk (lowest detail)
            # =========================================================================
            
            for mipmap in range(tile.max_mipmap + 1):
                mipmap_zoom = tile.max_zoom - mipmap
                
                # Don't go below minimum zoom
                if mipmap_zoom < tile.min_zoom:
                    break
                
                # Wait for any pending consolidation OUTSIDE lock
                tile._wait_for_pending_consolidation_if_needed()
                
                # Create chunks for this zoom level if they don't exist
                tile._create_chunks(mipmap_zoom)
                
                mipmap_chunks = tile.chunks.get(mipmap_zoom, [])
                if not mipmap_chunks:
                    continue
                
                for chunk in mipmap_chunks:
                    # Skip if already ready, in flight, or failed
                    if chunk.ready.is_set():
                        continue
                    if chunk.in_queue or chunk.in_flight:
                        continue
                    if chunk.permanent_failure:
                        continue
                    
                    # Priority: mipmap 0 = most important, higher mipmaps = less important
                    # Lower priority number = more urgent
                    chunk.priority = self.PREFETCH_PRIORITY_OFFSET + mipmap
                    
                    # Submit to chunk getter
                    chunk_getter.submit(chunk)
                    submitted += 1
                    
                    # Limit chunks per tile to avoid flooding queue with one tile
                    if submitted >= self.MAX_CHUNKS_PER_TILE:
                        break
                
                if submitted >= self.MAX_CHUNKS_PER_TILE:
                    break
            
            # Register tile with completion tracker for predictive DDS generation
            # This must happen AFTER creating all chunks so the tracker can count them
            if tile_completion_tracker is not None and submitted > 0:
                tile_completion_tracker.start_tracking(tile, zoom)
            
            return submitted
            
        except Exception as e:
            log.debug(f"Prefetch error for tile {row},{col}: {e}")
            return 0
        finally:
            # Always close the tile to decrement refs - this balances _open_tile() above.
            # The tile stays in the cache (enable_cache=True) but with refs decremented,
            # allowing eviction to work properly when the tile is no longer needed.
            if tile is not None:
                try:
                    self._tile_cacher._close_tile(row, col, maptype, zoom)
                except Exception:
                    pass  # Don't let close errors mask the original exception


# Global prefetcher instance
spatial_prefetcher = SpatialPrefetcher()


def start_prefetcher(tile_cacher):
    """Start the spatial prefetcher with the given tile cacher."""
    spatial_prefetcher.set_tile_cacher(tile_cacher)
    spatial_prefetcher.start()


def stop_prefetcher():
    """Stop the spatial prefetcher."""
    spatial_prefetcher.stop()


# ============================================================================
# PREDICTIVE DDS GENERATION
# ============================================================================
# Pre-builds DDS textures in the background after tiles are prefetched.
# This eliminates the decode+compress stutter when X-Plane reads new tiles.
#
# Components:
# 1. TileCompletionTracker - Monitors chunk downloads, detects when all ready
# 2. PrebuiltDDSCache - Stores pre-built DDS for instant serving
# 3. BackgroundDDSBuilder - Builds DDS from completed tiles in background
# ============================================================================

class _TrackedTile:
    """Internal tracking state for a tile being prefetched."""
    __slots__ = ('tile', 'zoom', 'expected_chunks', 'completed_chunks', 
                 'start_time', 'completed_chunk_ids')
    
    def __init__(self, tile, zoom: int, expected_chunks: int):
        self.tile = tile
        self.zoom = zoom
        self.expected_chunks = expected_chunks
        self.completed_chunks = 0
        self.start_time = time.monotonic()
        self.completed_chunk_ids = set()  # Track which chunks reported complete


class TileCompletionTracker:
    """
    Tracks chunk downloads and notifies when all chunks for a tile are ready.
    
    Thread-safe: chunks complete on ChunkGetter threads, notifications are
    processed to avoid blocking download threads.
    
    Memory-bounded: Only tracks tiles that are actively being prefetched.
    Tiles are removed from tracking once build is triggered or on timeout.
    """
    
    def __init__(self, on_tile_complete=None):
        """
        Args:
            on_tile_complete: Callback when all chunks are ready.
                             Called with (tile_id, tile) from notification thread.
                             If None, no callback is made.
        """
        self._lock = threading.Lock()
        self._tracked_tiles: Dict[str, _TrackedTile] = {}
        self._on_tile_complete = on_tile_complete
        self._max_tracked = 200  # Limit memory usage
        self._timeout_sec = 600  # Stop tracking after 10 minutes
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 60  # Cleanup every 60 seconds
    
    def start_tracking(self, tile, zoom: int) -> None:
        """
        Begin tracking a tile's chunk completion for ALL mipmap levels.
        
        Called by SpatialPrefetcher when it starts prefetching a tile.
        If tile is already being tracked, this is a no-op.
        
        Counts chunks across all mipmap levels (not just mipmap 0) to ensure
        we wait for all native-resolution chunks before building the DDS.
        
        Args:
            tile: Tile object to track
            zoom: Max zoom level for this tile (tile.max_zoom)
        """
        if tile is None:
            return
        
        tile_id = tile.id
        
        with self._lock:
            # Already tracking this tile
            if tile_id in self._tracked_tiles:
                return
            
            # Calculate expected chunk count across ALL mipmap levels
            # Each mipmap has chunks at its corresponding zoom level:
            #   mipmap 0 → max_zoom (256 chunks typically)
            #   mipmap 1 → max_zoom-1 (64 chunks)
            #   mipmap 2 → max_zoom-2 (16 chunks)
            #   mipmap 3 → max_zoom-3 (4 chunks)
            #   mipmap 4 → max_zoom-4 (1 chunk)
            total_expected = 0
            
            for mipmap in range(tile.max_mipmap + 1):
                mipmap_zoom = tile.max_zoom - mipmap
                
                # Don't go below minimum zoom
                if mipmap_zoom < tile.min_zoom:
                    break
                
                chunks = tile.chunks.get(mipmap_zoom, [])
                if chunks:
                    total_expected += len(chunks)
                else:
                    # Chunks not created yet - estimate from tile dimensions
                    # Chunk count follows 4^mipmap pattern relative to mipmap 0
                    zoom_diff = tile.tilename_zoom - mipmap_zoom
                    if zoom_diff >= 0:
                        chunks_per_row = tile.width >> zoom_diff
                        chunks_per_col = tile.height >> zoom_diff
                    else:
                        chunks_per_row = tile.width << (-zoom_diff)
                        chunks_per_col = tile.height << (-zoom_diff)
                    total_expected += max(1, chunks_per_row) * max(1, chunks_per_col)
            
            # Enforce max tracked limit (evict oldest if needed)
            if len(self._tracked_tiles) >= self._max_tracked:
                self._evict_oldest_unlocked()
            
            self._tracked_tiles[tile_id] = _TrackedTile(tile, zoom, total_expected)
            log.debug(f"TileCompletionTracker: Started tracking {tile_id} "
                     f"(expecting {total_expected} chunks across all mipmaps)")
            
            # Periodic cleanup of stale entries
            self._maybe_cleanup_unlocked()
    
    def notify_chunk_ready(self, tile_id: str, chunk) -> None:
        """
        Called when a chunk download completes.
        
        Thread-safe: may be called from any ChunkGetter thread.
        Non-blocking: uses fire-and-forget pattern.
        
        Args:
            tile_id: ID of the parent tile
            chunk: The chunk that just completed
        """
        if not tile_id:
            return
        
        tile_to_callback = None
        
        with self._lock:
            tracked = self._tracked_tiles.get(tile_id)
            if tracked is None:
                # Not tracking this tile (not prefetched, or already completed)
                return
            
            # Avoid double-counting the same chunk
            chunk_key = chunk.chunk_id if chunk else None
            if chunk_key and chunk_key in tracked.completed_chunk_ids:
                return
            
            if chunk_key:
                tracked.completed_chunk_ids.add(chunk_key)
            tracked.completed_chunks += 1
            
            log.debug(f"TileCompletionTracker: {tile_id} chunk complete "
                     f"({tracked.completed_chunks}/{tracked.expected_chunks})")
            
            # Check if tile is complete
            if tracked.completed_chunks >= tracked.expected_chunks:
                tile_to_callback = tracked.tile
                # Remove from tracking (build will be triggered)
                del self._tracked_tiles[tile_id]
                log.debug(f"TileCompletionTracker: {tile_id} COMPLETE - all chunks ready")
        
        # Call callback OUTSIDE the lock to avoid deadlocks
        if tile_to_callback is not None and self._on_tile_complete is not None:
            try:
                self._on_tile_complete(tile_id, tile_to_callback)
            except Exception as e:
                log.warning(f"TileCompletionTracker: Callback error for {tile_id}: {e}")
    
    def stop_tracking(self, tile_id: str) -> None:
        """Stop tracking a tile (e.g., tile was evicted from cache)."""
        with self._lock:
            self._tracked_tiles.pop(tile_id, None)
    
    def _evict_oldest_unlocked(self) -> None:
        """Evict the oldest tracked tile. Must hold _lock."""
        if not self._tracked_tiles:
            return
        
        oldest_id = None
        oldest_time = float('inf')
        
        for tid, tracked in self._tracked_tiles.items():
            if tracked.start_time < oldest_time:
                oldest_time = tracked.start_time
                oldest_id = tid
        
        if oldest_id:
            del self._tracked_tiles[oldest_id]
            log.debug(f"TileCompletionTracker: Evicted oldest tile {oldest_id}")
    
    def _maybe_cleanup_unlocked(self) -> None:
        """Cleanup stale entries if enough time has passed. Must hold _lock."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        self._last_cleanup = now
        cutoff = now - self._timeout_sec
        
        stale = [tid for tid, tracked in self._tracked_tiles.items()
                 if tracked.start_time < cutoff]
        
        for tid in stale:
            del self._tracked_tiles[tid]
            log.debug(f"TileCompletionTracker: Cleaned up stale tile {tid}")
    
    @property
    def tracked_count(self) -> int:
        """Number of tiles currently being tracked."""
        with self._lock:
            return len(self._tracked_tiles)


class PrebuiltDDSCache:
    """
    Cache for pre-built DDS byte buffers.
    
    Stores completed DDS textures built by BackgroundDDSBuilder.
    These are served directly to X-Plane on cache hit, avoiding
    the decode+compress overhead.
    
    Memory Management:
    - Has its own memory limit (separate from tile cache)
    - Uses LRU eviction when limit is reached
    - Prebuilt tiles are evicted BEFORE active tiles
    
    Thread Safety:
    - Reads and writes are protected by RLock
    - Multiple FUSE threads may read concurrently
    - Single builder thread writes
    """
    
    def __init__(self, max_memory_bytes: int = 512 * 1024 * 1024):
        """
        Args:
            max_memory_bytes: Maximum memory for prebuilt DDS (default 512MB)
        """
        self._lock = threading.RLock()
        self._cache: OrderedDict = OrderedDict()
        self._memory_used = 0
        self._max_memory = max_memory_bytes
        self._hits = 0
        self._misses = 0
    
    def get(self, tile_id: str) -> Optional[bytes]:
        """
        Retrieve pre-built DDS bytes for a tile.
        
        Returns None if not in cache (cache miss).
        Updates LRU order on hit.
        
        Thread-safe: may be called from multiple FUSE threads.
        """
        with self._lock:
            if tile_id not in self._cache:
                self._misses += 1
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(tile_id)
            self._hits += 1
            return self._cache[tile_id]
    
    def store(self, tile_id: str, dds_bytes: bytes) -> None:
        """
        Store pre-built DDS bytes.
        
        Evicts oldest entries if memory limit exceeded.
        
        Thread-safe: called from builder thread.
        """
        if not dds_bytes:
            return
        
        size = len(dds_bytes)
        
        with self._lock:
            # Remove existing entry if present
            if tile_id in self._cache:
                old_size = len(self._cache[tile_id])
                del self._cache[tile_id]
                self._memory_used -= old_size
            
            # Evict until we have room
            while self._memory_used + size > self._max_memory and self._cache:
                oldest_id, oldest_bytes = self._cache.popitem(last=False)
                self._memory_used -= len(oldest_bytes)
                log.debug(f"PrebuiltDDSCache: Evicted {oldest_id} to make room")
            
            # Store new entry
            self._cache[tile_id] = dds_bytes
            self._memory_used += size
            log.debug(f"PrebuiltDDSCache: Stored {tile_id} ({size} bytes, "
                     f"total: {self._memory_used / (1024*1024):.1f}MB)")
    
    def remove(self, tile_id: str) -> None:
        """Remove a tile from the cache (e.g., when tile is evicted from main cache)."""
        with self._lock:
            if tile_id in self._cache:
                size = len(self._cache[tile_id])
                del self._cache[tile_id]
                self._memory_used -= size
    
    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            self._memory_used = 0
    
    def contains(self, tile_id: str) -> bool:
        """Check if tile is in cache without updating LRU."""
        with self._lock:
            return tile_id in self._cache
    
    @property
    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                'entries': len(self._cache),
                'memory_mb': self._memory_used / (1024 * 1024),
                'max_memory_mb': self._max_memory / (1024 * 1024),
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': hit_rate
            }


class EphemeralDDSCache:
    """
    Disk-based DDS cache for session overflow.
    
    Provides temporary disk storage for pre-built DDS textures that exceed
    memory limits. Key properties:
    
    - Uses OS temp directory (auto-cleaned on reboot)
    - Session-tagged to invalidate previous runs
    - LRU eviction when size limit reached
    - Automatically cleaned up on shutdown
    
    Unlike a persistent cache, this ephemeral cache:
    - Does NOT persist between sessions
    - Does NOT risk stale/corrupt textures
    - Does NOT increase long-term disk usage
    
    Thread Safety:
    - Reads and writes are protected by lock
    """
    
    def __init__(self, max_size_mb: int = 4096):
        """
        Args:
            max_size_mb: Maximum disk usage in MB (default 4GB)
        """
        import tempfile
        self._session_id = uuid.uuid4().hex[:8]
        self._cache_dir = os.path.join(
            tempfile.gettempdir(),
            'autoortho_dds_session'
        )
        os.makedirs(self._cache_dir, exist_ok=True)
        self._max_size = max_size_mb * 1024 * 1024
        self._current_size = 0
        self._entries: OrderedDict = OrderedDict()  # tile_id -> (path, size)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        
        # Clean stale entries from previous sessions
        self._cleanup_stale()
        
        log.info(f"EphemeralDDSCache initialized: {self._cache_dir} "
                 f"(session={self._session_id}, max={max_size_mb}MB)")
    
    def _cleanup_stale(self):
        """Remove files from previous sessions."""
        try:
            for filename in os.listdir(self._cache_dir):
                if not filename.startswith(self._session_id):
                    try:
                        os.remove(os.path.join(self._cache_dir, filename))
                    except OSError:
                        pass
        except OSError:
            pass
    
    def _path_for(self, tile_id: str) -> str:
        """Generate cache file path for a tile."""
        # Sanitize tile_id for filename
        safe_id = tile_id.replace('/', '_').replace('\\', '_')
        return os.path.join(self._cache_dir, f"{self._session_id}_{safe_id}.dds")
    
    def get(self, tile_id: str) -> Optional[bytes]:
        """
        Retrieve pre-built DDS bytes from disk cache.
        
        Returns None on miss. Updates LRU order on hit.
        """
        with self._lock:
            if tile_id not in self._entries:
                self._misses += 1
                return None
            path, _ = self._entries[tile_id]
            # Move to end (most recently used)
            self._entries.move_to_end(tile_id)
        
        try:
            data = Path(path).read_bytes()
            with self._lock:
                self._hits += 1
            return data
        except (FileNotFoundError, OSError):
            # File was deleted externally
            with self._lock:
                self._entries.pop(tile_id, None)
                self._misses += 1
            return None
    
    def store(self, tile_id: str, dds_bytes: bytes) -> bool:
        """
        Store pre-built DDS bytes to disk cache.
        
        Evicts oldest entries if size limit exceeded.
        Returns True on success.
        """
        if not dds_bytes:
            return False
        
        size = len(dds_bytes)
        path = self._path_for(tile_id)
        
        with self._lock:
            # Remove existing entry if present
            if tile_id in self._entries:
                old_path, old_size = self._entries.pop(tile_id)
                try:
                    os.remove(old_path)
                except OSError:
                    pass
                self._current_size -= old_size
            
            # Evict until we have room
            while self._current_size + size > self._max_size and self._entries:
                oldest_id, (oldest_path, oldest_size) = self._entries.popitem(last=False)
                try:
                    os.remove(oldest_path)
                except OSError:
                    pass
                self._current_size -= oldest_size
            
            # Write new entry
            try:
                Path(path).write_bytes(dds_bytes)
                self._entries[tile_id] = (path, size)
                self._current_size += size
                return True
            except OSError as e:
                log.debug(f"EphemeralDDSCache: Write failed for {tile_id}: {e}")
                return False
    
    def path_for(self, tile_id: str) -> str:
        """
        Get the cache file path for a tile (public API for direct-to-disk writes).
        
        Used when C code needs to write directly to the cache location.
        Call register_file() after successful write.
        
        Args:
            tile_id: Tile identifier
            
        Returns:
            Full path where the DDS file should be written
        """
        return self._path_for(tile_id)
    
    def register_file(self, tile_id: str, size: int) -> bool:
        """
        Register an externally-written file with the cache.
        
        CRITICAL for direct-to-disk optimization:
        When C code writes DDS files directly (via aodds_build_from_jpegs_to_file),
        this method registers the file with the cache without re-reading it.
        
        Flow for direct-to-disk:
        1. path = cache.path_for(tile_id)
        2. C writes DDS directly to path
        3. cache.register_file(tile_id, bytes_written)
        
        Handles eviction if size limit exceeded.
        
        Args:
            tile_id: Tile identifier
            size: File size in bytes (as reported by C code)
            
        Returns:
            True on success, False if file doesn't exist
        """
        path = self._path_for(tile_id)
        
        # Verify file exists (C should have written it)
        if not os.path.exists(path):
            log.debug(f"EphemeralDDSCache.register_file: File not found: {path}")
            return False
        
        with self._lock:
            # Remove existing entry if present
            if tile_id in self._entries:
                old_path, old_size = self._entries.pop(tile_id)
                # Don't delete - the new file is at the same path
                self._current_size -= old_size
            
            # Evict until we have room
            while self._current_size + size > self._max_size and self._entries:
                oldest_id, (oldest_path, oldest_size) = self._entries.popitem(last=False)
                try:
                    os.remove(oldest_path)
                except OSError:
                    pass
                self._current_size -= oldest_size
            
            # Register the file
            self._entries[tile_id] = (path, size)
            self._current_size += size
            return True
    
    def remove(self, tile_id: str) -> None:
        """Remove a tile from the cache."""
        with self._lock:
            if tile_id in self._entries:
                path, size = self._entries.pop(tile_id)
                try:
                    os.remove(path)
                except OSError:
                    pass
                self._current_size -= size
    
    def contains(self, tile_id: str) -> bool:
        """Check if tile is in cache."""
        with self._lock:
            return tile_id in self._entries
    
    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            for tile_id, (path, _) in list(self._entries.items()):
                try:
                    os.remove(path)
                except OSError:
                    pass
            self._entries.clear()
            self._current_size = 0
    
    def cleanup(self):
        """Clean all session files on shutdown."""
        self.clear()
        log.info(f"EphemeralDDSCache cleaned up (session={self._session_id})")
    
    @property
    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                'entries': len(self._entries),
                'size_mb': self._current_size / (1024 * 1024),
                'max_size_mb': self._max_size / (1024 * 1024),
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': hit_rate,
                'session_id': self._session_id
            }


class HybridDDSCache:
    """
    Two-tier DDS cache: fast memory + large disk overflow.
    
    Provides the best of both worlds:
    - Memory tier: Fast access for hot tiles (default 512MB)
    - Disk tier: Large capacity overflow (default 4GB)
    
    On store:
    - Tries memory first
    - Overflows to disk when memory full
    
    On get:
    - Checks memory first (faster)
    - Falls back to disk on miss
    
    Thread Safety:
    - Delegates to underlying caches which are thread-safe
    """
    
    def __init__(self, memory_mb: int = 512, disk_mb: int = 4096):
        """
        Args:
            memory_mb: Maximum memory cache size in MB
            disk_mb: Maximum disk cache size in MB
        """
        self._memory = PrebuiltDDSCache(max_memory_bytes=memory_mb * 1024 * 1024)
        self._disk = EphemeralDDSCache(max_size_mb=disk_mb)
        log.info(f"HybridDDSCache initialized: memory={memory_mb}MB, disk={disk_mb}MB")
    
    def get(self, tile_id: str) -> Optional[bytes]:
        """
        Retrieve DDS bytes, checking memory first then disk.
        """
        # Try memory first (faster)
        result = self._memory.get(tile_id)
        if result:
            return result
        
        # Try disk
        return self._disk.get(tile_id)
    
    def store(self, tile_id: str, dds_bytes: bytes) -> None:
        """
        Store DDS bytes, trying memory first then overflow to disk.
        """
        if not dds_bytes:
            return
        
        # Try memory first
        self._memory.store(tile_id, dds_bytes)
        
        # If memory is full and this entry wasn't stored, use disk
        if not self._memory.contains(tile_id):
            self._disk.store(tile_id, dds_bytes)
    
    def remove(self, tile_id: str) -> None:
        """Remove from both tiers."""
        self._memory.remove(tile_id)
        self._disk.remove(tile_id)
    
    def contains(self, tile_id: str) -> bool:
        """Check if tile is in either tier."""
        return self._memory.contains(tile_id) or self._disk.contains(tile_id)
    
    def clear(self) -> None:
        """Clear both tiers."""
        self._memory.clear()
        self._disk.clear()
    
    def cleanup(self):
        """Cleanup disk tier on shutdown."""
        self._disk.cleanup()
    
    @property
    def stats(self) -> dict:
        """Return combined statistics."""
        mem_stats = self._memory.stats
        disk_stats = self._disk.stats
        return {
            'memory': mem_stats,
            'disk': disk_stats,
            'total_entries': mem_stats['entries'] + disk_stats['entries'],
            'total_size_mb': mem_stats['memory_mb'] + disk_stats['size_mb']
            }


class BackgroundDDSBuilder:
    """
    Builds DDS textures from fully-downloaded tiles in the background.
    
    Design principles:
    1. Parallel workers: Configurable thread pool for throughput
    2. Rate-limited submission: Prevents queue flooding
    3. Priority queue: Build tiles in submission order (FIFO for fairness)
    4. Interruptible: Can pause/stop cleanly during shutdown
    """
    
    # Maximum queue depth (prevents unbounded memory growth)
    MAX_QUEUE_SIZE = 100
    
    def __init__(self, prebuilt_cache: PrebuiltDDSCache, 
                 build_interval_sec: float = 0.5,
                 max_workers: int = 2):
        """
        Args:
            prebuilt_cache: Cache to store completed DDS buffers
            build_interval_sec: Minimum time between submissions (rate limiting)
            max_workers: Number of parallel build workers
        """
        self._queue = Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._prebuilt_cache = prebuilt_cache
        self._build_interval = build_interval_sec
        self._max_workers = max_workers
        
        # Worker pool for parallel builds
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._coordinator_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Stats (thread-safe via atomic operations)
        self._builds_completed = 0
        self._builds_failed = 0
        self._active_builds = 0
        self._active_lock = threading.Lock()
        self._last_build_time = 0.0
    
    def start(self) -> None:
        """Start the background builder."""
        if self._coordinator_thread is not None and self._coordinator_thread.is_alive():
            return
        
        self._stop_event.clear()
        
        # Create worker pool
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="BackgroundDDS"
        )
        
        # Start coordinator thread
        self._coordinator_thread = threading.Thread(
            target=self._coordinator_loop,
            name="BackgroundDDSCoordinator",
            daemon=True
        )
        self._coordinator_thread.start()
        log.info(f"BackgroundDDSBuilder started "
                f"(workers={self._max_workers}, interval={self._build_interval*1000:.0f}ms)")
    
    def stop(self) -> None:
        """Stop the background builder gracefully."""
        self._stop_event.set()
        
        # Drain queue
        try:
            while True:
                self._queue.get_nowait()
        except Empty:
            pass
        
        # Stop coordinator
        if self._coordinator_thread is not None:
            try:
                self._queue.put_nowait(None)  # Sentinel
            except Full:
                pass
            self._coordinator_thread.join(timeout=2.0)
            self._coordinator_thread = None
        
        # Shutdown executor (wait for active builds)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None
        
        log.info(f"BackgroundDDSBuilder stopped "
                f"(built={self._builds_completed}, failed={self._builds_failed})")
    
    def submit(self, tile, priority: float = 0) -> bool:
        """
        Submit a tile for background DDS building.
        
        Args:
            tile: Tile with all chunks downloaded
            priority: Not used currently (FIFO order), reserved for future
            
        Returns:
            True if queued, False if queue is full or tile already cached
        """
        if tile is None:
            return False
        
        # Skip if already in prebuilt cache
        if self._prebuilt_cache.contains(tile.id):
            log.debug(f"BackgroundDDSBuilder: Skipping {tile.id} - already cached")
            return False
        
        try:
            self._queue.put_nowait((priority, tile))
            log.debug(f"BackgroundDDSBuilder: Queued {tile.id} "
                     f"(queue size: {self._queue.qsize()})")
            return True
        except Full:
            log.debug(f"BackgroundDDSBuilder: Queue full, skipping {tile.id}")
            return False
    
    def _coordinator_loop(self) -> None:
        """
        Coordinator loop - dispatches tiles to worker pool in batches.
        
        Submits multiple tiles per interval to better utilize worker pool.
        With 8 workers but only 1 submission/second, workers were underutilized.
        Now submits up to half the workers per interval to leave capacity.
        """
        while not self._stop_event.is_set():
            # Rate limiting: wait for interval before batch submission
            elapsed = time.monotonic() - self._last_build_time
            if elapsed < self._build_interval:
                sleep_time = self._build_interval - elapsed
                if self._stop_event.wait(timeout=sleep_time):
                    continue  # Stop requested during sleep
            
            # Submit batch of tiles to utilize all workers
            # Limit to half of workers to leave capacity for newly arriving tiles
            max_batch = max(1, self._max_workers // 2)
            batch_submitted = 0
            
            while batch_submitted < max_batch:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break  # Queue exhausted
                
                # Sentinel value signals shutdown
                if item is None:
                    continue
                
                priority, tile = item
                
                # Drop evicted tiles early to avoid holding
                # references longer than necessary
                if getattr(tile, '_closed', False):
                    bump('prebuilt_dds_skip_closed')
                    continue
                
                if self._executor is not None:
                    with self._active_lock:
                        self._active_builds += 1
                    self._executor.submit(
                        self._build_tile_wrapper, tile
                    )
                    batch_submitted += 1
            
            if batch_submitted > 0:
                self._last_build_time = time.monotonic()
            else:
                # Queue was empty - wait for new items
                # with blocking get
                try:
                    item = self._queue.get(timeout=1.0)
                    if item is not None:
                        priority, tile = item
                        # Drop evicted tiles early
                        if getattr(tile, '_closed', False):
                            bump('prebuilt_dds_skip_closed')
                        elif self._executor is not None:
                            with self._active_lock:
                                self._active_builds += 1
                            self._executor.submit(
                                self._build_tile_wrapper,
                                tile
                            )
                            self._last_build_time = (
                                time.monotonic()
                            )
                except Empty:
                    continue
    
    def _build_tile_wrapper(self, tile) -> None:
        """Wrapper for _build_tile_dds that handles exceptions and stats."""
        try:
            # Skip if tile was evicted while queued/waiting
            if getattr(tile, '_closed', False):
                log.debug(
                    "BackgroundDDSBuilder: Skipping closed tile "
                    f"{tile.id}"
                )
                bump('prebuilt_dds_skip_closed')
                return
            self._build_tile_dds(tile)
        finally:
            with self._active_lock:
                self._active_builds -= 1
    
    def _try_streaming_prefetch_build(self, tile, tile_id: str, build_start: float) -> bool:
        """
        Build DDS for prefetch using streaming builder with fallback support.
        
        Key difference from live: NO TIME BUDGET.
        Takes as long as needed to apply all fallbacks for quality.
        
        Priority: Bundle build (fastest) > Streaming build (with fallbacks)
        
        Args:
            tile: Tile to build
            tile_id: Tile ID string
            build_start: Monotonic time when build started
        
        Returns:
            True if build succeeded, False to fall back to other methods
        """
        # Try bundle-first path (much faster than streaming build)
        try:
            dds_bytes = self._try_build_from_bundle(tile, tile_id)
            if dds_bytes is not None:
                elapsed = (time.monotonic() - build_start) * 1000
                log.debug(f"BackgroundDDSBuilder: Built {tile_id} from bundle in {elapsed:.0f}ms")
                return self._store_dds_result(tile, tile_id, dds_bytes, build_start)
        except Exception as e:
            log.debug(f"BackgroundDDSBuilder: Bundle build failed for {tile_id}: {e}")
        
        # Fall through to streaming build
        try:
            from autoortho.aopipeline.AoDDS import get_default_builder_pool
            from autoortho.aopipeline.fallback_resolver import FallbackResolver
        except ImportError as e:
            log.debug(f"BackgroundDDSBuilder: Streaming builder imports failed: {e}")
            return False
        
        builder_pool = get_default_builder_pool()
        if builder_pool is None:
            log.debug(f"BackgroundDDSBuilder: Streaming builder pool not available")
            return False
        
        # Get configuration
        dxt_format = CFG.pydds.format.upper()
        if dxt_format in ("DXT1", "BC1"):
            dxt_format = "BC1"
        else:
            dxt_format = "BC3"
        
        missing_color = tuple(CFG.autoortho.missing_color[:3]) if hasattr(CFG.autoortho, 'missing_color') else (66, 77, 55)
        
        # Get fallback level - prefetch uses same settings as live
        use_fallbacks = getattr(CFG.autoortho, 'predictive_dds_use_fallbacks', True)
        if use_fallbacks:
            fallback_level_str = str(getattr(CFG.autoortho, 'fallback_level', 'cache')).lower()
            if fallback_level_str == 'none':
                fallback_level = 0
            elif fallback_level_str == 'full':
                fallback_level = 2
            else:
                fallback_level = 1
        else:
            fallback_level = 0  # No fallbacks for prefetch if disabled
        
        # Create fallback resolver
        resolver = FallbackResolver(
            cache_dir=tile.cache_dir,
            maptype=tile.maptype,
            tile_col=tile.col,
            tile_row=tile.row,
            tile_zoom=tile.max_zoom,
            fallback_level=fallback_level,
            max_mipmap=tile.max_mipmap,
            downloader=None  # Network fallback handled separately
        )
        
        # Set available mipmap images for scaling fallback
        resolver.set_mipmap_images(tile.imgs)
        
        # Acquire streaming builder (blocking OK for background thread)
        config = {
            'chunks_per_side': tile.chunks_per_row,
            'format': dxt_format,
            'missing_color': missing_color
        }
        
        builder = builder_pool.acquire(config=config, timeout=30.0)
        if not builder:
            log.warning(f"BackgroundDDSBuilder: Failed to acquire streaming builder for {tile_id}")
            return False
        
        # Setup transition tracking
        tile._live_transition_event = threading.Event()
        tile._active_streaming_builder = builder
        
        # Keep references alive for zero-copy mode (cleared after finalize)
        jpeg_refs_for_nocopy = []
        
        try:
            # Get chunks for max zoom
            chunks = tile.chunks.get(tile.max_zoom, [])
            if not chunks:
                log.debug(f"BackgroundDDSBuilder: No chunks for {tile_id}")
                return False
            
            # Import fallback TimeBudget for use during transition
            from autoortho.aopipeline.fallback_resolver import TimeBudget as FBTimeBudget
            
            # Phase 1: Batch add all ready chunks at once
            ready_chunks = []
            pending_indices = []
            for i, chunk in enumerate(chunks):
                if chunk.ready.is_set() and chunk.data:
                    ready_chunks.append((i, chunk.data))
                else:
                    pending_indices.append(i)
            
            # ZERO-COPY: Batch add chunks using nocopy mode
            # C stores pointers directly, we keep references in jpeg_refs_for_nocopy
            if ready_chunks:
                builder.add_chunks_batch_nocopy(ready_chunks, jpeg_refs_for_nocopy)
            
            # Phase 2: Process remaining chunks with transition handling
            # Key difference from live: NO initial time budget, but may get one on transition
            for i in pending_indices:
                chunk = chunks[i]
                
                # === TRANSITION CHECK ===
                # If tile became live, use its time budget for remaining work
                time_budget = tile._tile_time_budget if tile._is_live else None
                
                if tile._is_live and time_budget and time_budget.exhausted:
                    # Budget exhausted - mark remaining as missing
                    builder.mark_missing(i)
                    continue
                
                # Wait for chunk - but check for live transition periodically
                while not chunk.ready.is_set():
                    # Short wait to allow transition detection
                    chunk.ready.wait(timeout=0.1)
                    
                    # Check for live transition
                    if tile._is_live:
                        time_budget = tile._tile_time_budget
                        if time_budget and time_budget.exhausted:
                            break  # Budget exhausted, move on
                
                # Determine fallback budget (None if prefetching, from tile if live)
                fb_budget = None
                if tile._is_live and time_budget and not time_budget.exhausted:
                    remaining = time_budget.remaining
                    if remaining > 0:
                        fb_budget = FBTimeBudget(remaining)
                
                if chunk.data:
                    if not builder.add_chunk(i, chunk.data):
                        # Decode failed - try fallback
                        chunk_col = tile.col + (i % tile.chunks_per_row)
                        chunk_row = tile.row + (i // tile.chunks_per_row)
                        
                        fallback_rgba = resolver.resolve(
                            chunk_col, chunk_row, tile.max_zoom,
                            target_mipmap=0,
                            time_budget=fb_budget
                        )
                        
                        if fallback_rgba:
                            builder.add_fallback_image(i, fallback_rgba)
                        else:
                            builder.mark_missing(i)
                else:
                    # Chunk failed to download - apply full fallback chain
                    chunk_col = tile.col + (i % tile.chunks_per_row)
                    chunk_row = tile.row + (i // tile.chunks_per_row)
                    
                    fallback_rgba = resolver.resolve(
                        chunk_col, chunk_row, tile.max_zoom,
                        target_mipmap=0,
                        time_budget=fb_budget
                    )
                    
                    if fallback_rgba:
                        builder.add_fallback_image(i, fallback_rgba)
                    else:
                        builder.mark_missing(i)
            
            # Finalize directly to disk (optimal for prefetch cache)
            if hasattr(self._prebuilt_cache, 'path_for'):
                output_path = self._prebuilt_cache.path_for(tile_id)
                success, bytes_written = builder.finalize_to_file(output_path)
                
                if success and bytes_written >= 128:
                    if hasattr(self._prebuilt_cache, 'register_file'):
                        self._prebuilt_cache.register_file(tile_id, bytes_written)
                    
                    build_time = (time.monotonic() - build_start) * 1000
                    status = builder.get_status()
                    self._builds_completed += 1
                    log.debug(f"BackgroundDDSBuilder: Streaming built {tile_id} in {build_time:.0f}ms "
                              f"(decoded={status['chunks_decoded']}, fallback={status['chunks_fallback']}, "
                              f"missing={status['chunks_missing']})")
                    bump('prebuilt_dds_builds_streaming')
                    
                    # Schedule bundle consolidation for all zoom levels
                    # This ensures cached JPEGs are consolidated into bundles
                    consolidate_tile_if_ready(tile, "background_dds")
                    
                    return True
            
            log.debug(f"BackgroundDDSBuilder: Streaming finalize failed for {tile_id}")
            return False
            
        except Exception as e:
            log.debug(f"BackgroundDDSBuilder: Streaming build failed for {tile_id}: {e}")
            return False
        
        finally:
            # Clear JPEG refs to release memory held for zero-copy mode
            jpeg_refs_for_nocopy.clear()
            # Clear transition tracking
            tile._active_streaming_builder = None
            tile._live_transition_event = None
            builder.release()

    def _try_build_from_bundle(self, tile, tile_id: str) -> Optional[bytes]:
        """
        Try to build DDS directly from an AOB2 bundle file.
        
        This is the fastest path when a bundle exists:
        - Single file read instead of 256+
        - Memory-mapped for zero-copy access
        - Native parallel JPEG decode + DXT compress
        
        Args:
            tile: Tile to build
            tile_id: Tile ID string
        
        Returns:
            DDS bytes if successful, None if bundle not available
        """
        try:
            # Handle imports for both frozen (PyInstaller) and direct Python execution
            try:
                from autoortho.utils.bundle_paths import get_bundle2_path
                from autoortho.aopipeline import AoBundle2
            except ImportError:
                from utils.bundle_paths import get_bundle2_path
                from aopipeline import AoBundle2
            
            # CRITICAL: Use TILE ID coordinates (tile.row, tile.col at tilename_zoom) for bundle path!
            # Bundles are named after the tile ID, NOT the scaled chunk coordinates.
            bundle_path = get_bundle2_path(tile.cache_dir, tile.row, tile.col, tile.maptype, tile.tilename_zoom)
            
            if not os.path.exists(bundle_path):
                # Bundle doesn't exist - check if consolidation is pending or recently completed
                # CRITICAL: Pass max_zoom to is_pending() - only wait for OUR zoom!
                global _bundle_consolidator
                if _bundle_consolidator is not None:
                    bump('jpeg_to_bundle_fallback_entered')
                    waited_for_consolidation = False
                    if _bundle_consolidator.is_pending(tile.row, tile.col, tile.maptype, tile.max_zoom):
                        log.debug(f"Waiting for consolidation (zoom {tile.max_zoom}): {tile_id}")
                        bump('bundle_wait_started')
                        # Use 60s timeout - consolidation will complete, waiting is better than failing
                        # Pass zoom to wait for THIS specific zoom level's consolidation
                        wait_result = _bundle_consolidator.wait_for_pending(tile.row, tile.col, tile.maptype, timeout=60.0, zoom=tile.max_zoom)
                        waited_for_consolidation = True
                        if wait_result:
                            bump('bundle_wait_success')
                    
                    # ALWAYS re-check bundle - even if is_pending was False
                    # Consolidation may have JUST completed between os.path.exists() and is_pending()
                    if os.path.exists(bundle_path):
                        bump('jpeg_to_bundle_fallback_bundle_exists')
                        if waited_for_consolidation:
                            log.debug(f"Bundle appeared after consolidation wait: {tile_id}")
                        else:
                            log.debug(f"Bundle appeared (consolidation completed between checks): {tile_id}")
                            bump('jpeg_to_bundle_fallback_not_pending')
                    else:
                        # Still no bundle after all checks
                        return None
                else:
                    return None
            
            # Get chunk grid dimensions for max_zoom (used for DDS building)
            _, _, width, height, effective_zoom, _ = tile._get_quick_zoom(tile.max_zoom)
            
            # Get configuration
            dxt_format = CFG.pydds.format.upper()
            if dxt_format in ("DXT1", "BC1"):
                dxt_format = "BC1"
            else:
                dxt_format = "BC3"
            
            missing_color = tuple(CFG.autoortho.missing_color[:3]) if hasattr(CFG.autoortho, 'missing_color') else (66, 77, 55)
            
            # Build DDS from bundle using mode dispatcher
            return _build_tile_dds_from_bundle(
                tile=tile,
                bundle_path=bundle_path,
                target_zoom=effective_zoom,
                dxt_format=dxt_format,
                missing_color=missing_color,
                chunks_per_side=width  # Use scaled width (chunks_per_side for this zoom level)
            )
            
        except Exception as e:
            log.debug(f"BackgroundDDSBuilder: Bundle build failed: {e}")
            return None

    def _store_dds_result(self, tile, tile_id: str, dds_bytes: bytes, build_start: float) -> bool:
        """
        Store DDS build result in prebuilt cache.
        
        Args:
            tile: Tile that was built
            tile_id: Tile ID string
            dds_bytes: DDS data
            build_start: Build start time for metrics
        
        Returns:
            True if stored successfully
        """
        if not dds_bytes or len(dds_bytes) < 128:
            return False
        
        try:
            if hasattr(self._prebuilt_cache, 'path_for'):
                output_path = self._prebuilt_cache.path_for(tile_id)
                
                # Write DDS to disk atomically
                temp_path = output_path + f'.tmp.{os.getpid()}'
                with open(temp_path, 'wb') as f:
                    f.write(dds_bytes)
                os.rename(temp_path, output_path)
                
                if hasattr(self._prebuilt_cache, 'register_file'):
                    self._prebuilt_cache.register_file(tile_id, len(dds_bytes))
                
                build_time = (time.monotonic() - build_start) * 1000
                self._builds_completed += 1
                log.debug(f"BackgroundDDSBuilder: Bundle built {tile_id} in {build_time:.0f}ms")
                bump('prebuilt_dds_builds_bundle')
                return True
        except Exception as e:
            log.debug(f"BackgroundDDSBuilder: Failed to store DDS for {tile_id}: {e}")
        
        return False

    def _build_tile_dds(self, tile) -> None:
        """
        Build DDS for a single tile and store in prebuilt cache.
        
        Builds EACH mipmap from native-resolution chunks to match on-demand
        behavior. This ensures prebuilt tiles are visually identical to tiles
        that X-Plane requests directly.
        
        Uses the tile's existing infrastructure to get the composed image
        for each mipmap level, then compresses each into the DDS.
        
        Lock Contention Avoidance:
        - Attempts non-blocking lock acquisition before starting build
        - If tile is locked (X-Plane is building it), skips to avoid stalling
        - Holds lock for entire build to prevent concurrent builds
        
        Native Pipeline:
        - If native aopipeline is available, uses optimized C code for the
          entire pipeline (cache read, JPEG decode, compose, compress)
        - Falls back to Python implementation if native unavailable or fails
        """
        tile_id = tile.id
        build_start = time.monotonic()
        mipmap_images = []  # Track images for cleanup
        lock_acquired = False
        
        # ═══════════════════════════════════════════════════════════════════════
        # TRY STREAMING BUILDER FIRST (if enabled)
        # ═══════════════════════════════════════════════════════════════════════
        # Streaming builder allows:
        # - Incremental chunk processing as they download
        # - Full fallback chain support (disk cache, mipmap scaling)
        # - No time budget for prefetch (takes as long as needed for quality)
        # ═══════════════════════════════════════════════════════════════════════
        if getattr(CFG.autoortho, 'streaming_builder_enabled', True):
            if self._try_streaming_prefetch_build(tile, tile_id, build_start):
                return
        
        # ═══════════════════════════════════════════════════════════════════════
        # TRY OPTIMAL HYBRID DDS BUILDING FIRST
        # ═══════════════════════════════════════════════════════════════════════
        # PERFORMANCE INSIGHT (from benchmarks):
        # - Native file I/O is 3x SLOWER than Python for cached files
        # - Native decode+compress is 3.4x FASTER than Python
        # - OPTIMAL: Use chunk.data (already in memory) + native decode
        #
        # Pipeline mode determines which approach to use:
        # - native: C handles file I/O + decode + compress (fastest on Windows)
        # - hybrid: Python I/O + C decode + compress (fastest on macOS/Linux)
        # - python: Pure Python fallback (most compatible)
        # ═══════════════════════════════════════════════════════════════════════
        pipeline_mode = get_pipeline_mode()
        
        # Skip native attempts if explicitly in python mode
        if pipeline_mode != PIPELINE_MODE_PYTHON:
            native_dds = _get_native_dds()
            if native_dds is not None:
                # Get tile parameters
                dxt_format = CFG.pydds.format.upper()
                if dxt_format in ("DXT1", "BC1"):
                    dxt_format = "BC1"
                else:
                    dxt_format = "BC3"
                
                missing_color = tuple(CFG.autoortho.missing_color[:3]) if hasattr(CFG.autoortho, 'missing_color') else (66, 77, 55)
                
                # ───────────────────────────────────────────────────────────────────
                # HYBRID MODE: Python reads files, C does decode+compress
                # Best for macOS/Linux due to efficient Python I/O caching
                # ───────────────────────────────────────────────────────────────────
                if pipeline_mode == PIPELINE_MODE_HYBRID:
                    chunks_for_hybrid = tile.chunks.get(tile.max_zoom, [])
                    if chunks_for_hybrid:
                        # ═══════════════════════════════════════════════════════════════
                        # DIRECT-TO-DISK OPTIMIZATION (Phase 1: ~65ms copy eliminated)
                        # ═══════════════════════════════════════════════════════════════
                        # If cache supports register_file (EphemeralDDSCache), build
                        # directly to disk file - eliminates Python memory copy entirely.
                        # Flow: JPEG data → C decode → C compress → fwrite to disk
                        #
                        # This saves ~65ms per tile by avoiding:
                        # - Buffer → Python bytes copy
                        # - Python bytes → disk write
                        # ═══════════════════════════════════════════════════════════════
                        if (hasattr(self._prebuilt_cache, 'register_file') and 
                            hasattr(native_dds, 'build_from_jpegs_to_file')):
                            try:
                                # Extract JPEG data from chunks
                                jpeg_datas = []
                                valid_count = 0
                                for chunk in chunks_for_hybrid:
                                    data = chunk.data
                                    if data and len(data) > 0:
                                        jpeg_datas.append(data)
                                        valid_count += 1
                                    else:
                                        jpeg_datas.append(None)
                                
                                if valid_count > 0:
                                    # Get output path from cache
                                    output_path = self._prebuilt_cache.path_for(tile_id)
                                    
                                    # Build directly to file (zero-copy!)
                                    result = native_dds.build_from_jpegs_to_file(
                                        jpeg_datas,
                                        output_path,
                                        format=dxt_format,
                                        missing_color=missing_color
                                    )
                                    
                                    if result.success and result.bytes_written >= 128:
                                        # Register file with cache (no additional write!)
                                        self._prebuilt_cache.register_file(tile_id, result.bytes_written)
                                        build_time = (time.monotonic() - build_start) * 1000
                                        self._builds_completed += 1
                                        log.debug(f"BackgroundDDSBuilder: Direct-to-disk built {tile_id} "
                                                  f"in {build_time:.0f}ms ({result.bytes_written} bytes)")
                                        bump('prebuilt_dds_builds_direct')
                                        
                                        # Schedule bundle consolidation for all zoom levels
                                        consolidate_tile_if_ready(tile, "background_dds")
                                        
                                        return
                                    else:
                                        log.debug(f"BackgroundDDSBuilder: Direct-to-disk failed for "
                                                  f"{tile_id}: {result.error}, trying buffer path")
                            except Exception as e:
                                log.debug(f"BackgroundDDSBuilder: Direct-to-disk failed for {tile_id}: "
                                          f"{e}, trying buffer path")
                        
                        # ═══════════════════════════════════════════════════════════════
                        # BUFFER PATH FALLBACK
                        # Used when direct-to-disk unavailable or fails
                        # ═══════════════════════════════════════════════════════════════
                        try:
                            dds_bytes = _build_dds_hybrid(
                                chunks=chunks_for_hybrid,
                                dxt_format=dxt_format,
                                missing_color=missing_color
                            )
                            
                            if dds_bytes and len(dds_bytes) >= 128:
                                # Hybrid build succeeded - store and return
                                self._prebuilt_cache.store(tile_id, dds_bytes)
                                build_time = (time.monotonic() - build_start) * 1000
                                self._builds_completed += 1
                                log.debug(f"BackgroundDDSBuilder: Hybrid built {tile_id} in "
                                          f"{build_time:.0f}ms ({len(dds_bytes)} bytes)")
                                bump('prebuilt_dds_builds_hybrid')
                                
                                # Schedule bundle consolidation for all zoom levels
                                consolidate_tile_if_ready(tile, "background_dds")
                                
                                return
                            else:
                                log.debug(f"BackgroundDDSBuilder: Hybrid build returned no data "
                                          f"for {tile_id}, trying native file I/O")
                        except Exception as e:
                            log.debug(f"BackgroundDDSBuilder: Hybrid build failed for {tile_id}: "
                                      f"{e}, trying native file I/O")
                
                # ───────────────────────────────────────────────────────────────────
                # NATIVE MODE: C handles file I/O + decode + compress
                # Best for Windows, also serves as fallback for hybrid
                # ───────────────────────────────────────────────────────────────────
                
                # ═══════════════════════════════════════════════════════════════
                # NATIVE DIRECT-TO-DISK OPTIMIZATION (Phase 3)
                # ═══════════════════════════════════════════════════════════════
                # Same optimization as hybrid mode:
                # - C reads cache files + decodes + compresses + writes to disk
                # - Eliminates ~65ms Python copy overhead
                # ═══════════════════════════════════════════════════════════════
                if (hasattr(self._prebuilt_cache, 'register_file') and 
                    hasattr(native_dds, 'build_tile_to_file')):
                    try:
                        # Get output path from cache
                        output_path = self._prebuilt_cache.path_for(tile_id)
                        
                        # Build directly to file (zero-copy!)
                        result = native_dds.build_tile_to_file(
                            cache_dir=tile.cache_dir,
                            row=tile.row,
                            col=tile.col,
                            maptype=tile.maptype,
                            zoom=tile.max_zoom,
                            output_path=output_path,
                            chunks_per_side=tile.chunks_per_row,
                            format=dxt_format,
                            missing_color=missing_color
                        )
                        
                        if result.success and result.bytes_written >= 128:
                            # Register file with cache (no additional write!)
                            self._prebuilt_cache.register_file(tile_id, result.bytes_written)
                            build_time = (time.monotonic() - build_start) * 1000
                            self._builds_completed += 1
                            log.debug(f"BackgroundDDSBuilder: Native direct-to-disk built {tile_id} "
                                      f"in {build_time:.0f}ms ({result.bytes_written} bytes)")
                            bump('prebuilt_dds_builds_native_direct')
                            
                            # Schedule bundle consolidation for all zoom levels
                            consolidate_tile_if_ready(tile, "background_dds")
                            
                            return
                        else:
                            log.debug(f"BackgroundDDSBuilder: Native direct-to-disk failed for "
                                      f"{tile_id}: {result.error}, trying buffer path")
                    except Exception as e:
                        log.debug(f"BackgroundDDSBuilder: Native direct-to-disk failed for {tile_id}: "
                                  f"{e}, trying buffer path")
                
                # ═══════════════════════════════════════════════════════════════
                # NATIVE BUFFER POOL PATH (with priority queue)
                # ═══════════════════════════════════════════════════════════════
                # Uses unified buffer pool with PRIORITY_PREFETCH (low priority).
                # Prefetch tiles wait in the back of the queue and only get
                # buffers when all live tiles have been served (system is idle).
                # No fallback allocation - always wait for buffer (bank queue).
                # ═══════════════════════════════════════════════════════════════
                pool = _get_dds_buffer_pool()
                if pool is None or not hasattr(native_dds, 'build_tile_to_buffer'):
                    log.debug(f"BackgroundDDSBuilder: Buffer pool not available for {tile_id}")
                    bump('prefetch_pool_unavailable')
                    return
                
                # BLOCKING ACQUIRE: Wait for buffer (low priority - back of queue)
                # Prefetch tiles yield to live tiles automatically via priority queue
                wait_start = time.monotonic()
                try:
                    buffer, buffer_id = pool.acquire(timeout=60.0, priority=PRIORITY_PREFETCH)
                    wait_time_ms = (time.monotonic() - wait_start) * 1000
                    if wait_time_ms > 10:
                        bump('prefetch_queue_wait_count')
                except TimeoutError:
                    log.debug(f"BackgroundDDSBuilder: Prefetch queue timeout for {tile_id}")
                    bump('prefetch_queue_timeout')
                    return
                
                try:
                    result = native_dds.build_tile_to_buffer(
                        buffer,
                        cache_dir=tile.cache_dir,
                        row=tile.row,
                        col=tile.col,
                        maptype=tile.maptype,
                        zoom=tile.max_zoom,
                        chunks_per_side=tile.chunks_per_row,
                        format=dxt_format,
                        missing_color=missing_color
                    )
                    
                    if result.success and result.bytes_written >= 128:
                        # Copy from buffer and store
                        dds_bytes = result.to_bytes()
                        self._prebuilt_cache.store(tile_id, dds_bytes)
                        build_time = (time.monotonic() - build_start) * 1000
                        self._builds_completed += 1
                        log.debug(f"BackgroundDDSBuilder: Native built {tile_id} "
                                  f"in {build_time:.0f}ms ({len(dds_bytes)} bytes)")
                        bump('prebuilt_dds_builds_native_buffered')
                        
                        # Schedule bundle consolidation for all zoom levels
                        consolidate_tile_if_ready(tile, "background_dds")
                        
                        return
                    else:
                        log.debug(f"BackgroundDDSBuilder: Native build failed for {tile_id}")
                        bump('prefetch_native_build_failed')
                finally:
                    pool.release(buffer_id)
        
        # ═══════════════════════════════════════════════════════════════════════
        # PYTHON FALLBACK PATH
        # ═══════════════════════════════════════════════════════════════════════
        
        try:
            # ═══════════════════════════════════════════════════════════════════
            # LOCK CONTENTION AVOIDANCE
            # ═══════════════════════════════════════════════════════════════════
            # Try to acquire the tile's lock non-blocking. If X-Plane is currently
            # building this tile (holding the lock), we skip rather than stall the
            # background builder thread. The tile will be built by X-Plane anyway.
            #
            # Using non-blocking acquire prevents scenarios where:
            # 1. X-Plane requests tile -> FUSE acquires _tile_locks[key] -> get_mipmap() acquires tile._lock
            # 2. BackgroundDDSBuilder tries to build same tile -> blocks on tile._lock
            # 3. Builder thread stalls for potentially 300+ seconds
            #
            # By skipping locked tiles, we keep the background builder responsive
            # and avoid wasting thread time on tiles that are already being built.
            if hasattr(tile, '_lock'):
                if not tile._lock.acquire(blocking=False):
                    log.debug(f"BackgroundDDSBuilder: {tile_id} - tile is locked (in-use), skipping")
                    bump('prebuilt_dds_skipped_locked')
                    return
                lock_acquired = True
            # ═══════════════════════════════════════════════════════════════════
            
            # Verify tile is still valid
            if tile.dds is None:
                log.debug(f"BackgroundDDSBuilder: {tile_id} - tile.dds is None, skipping")
                return
            
            # Skip if already in cache (race condition check)
            if self._prebuilt_cache.contains(tile_id):
                log.debug(f"BackgroundDDSBuilder: {tile_id} - already in cache, skipping")
                return
            
            # Step 1: Verify chunks are ready for ALL mipmap levels
            # We need native chunks at each zoom level for proper mipmap building
            total_chunks = 0
            total_resolved = 0
            total_with_data = 0
            
            for mipmap in range(tile.max_mipmap + 1):
                mipmap_zoom = tile.max_zoom - mipmap
                if mipmap_zoom < tile.min_zoom:
                    break
                
                chunks = tile.chunks.get(mipmap_zoom, [])
                if not chunks:
                    # Chunks not created for this mipmap - can't build natively
                    log.debug(f"BackgroundDDSBuilder: {tile_id} - no chunks for mipmap {mipmap} (zoom {mipmap_zoom})")
                    continue
                
                total_chunks += len(chunks)
                resolved = sum(1 for c in chunks if c.ready.is_set())
                with_data = sum(1 for c in chunks if c.ready.is_set() and c.data)
                total_resolved += resolved
                total_with_data += with_data
            
            if total_resolved < total_chunks:
                # Some chunks still downloading - not ready for prebuild yet
                log.debug(f"BackgroundDDSBuilder: {tile_id} - only {total_resolved}/{total_chunks} chunks resolved")
                return
            
            if total_with_data == 0:
                # ALL chunks failed - nothing to build from
                log.debug(f"BackgroundDDSBuilder: {tile_id} - all chunks failed, skipping")
                return
            
            # Determine fallback behavior for prebuilds
            use_fallbacks = getattr(CFG.autoortho, 'predictive_dds_use_fallbacks', True)
            if isinstance(use_fallbacks, str):
                use_fallbacks = use_fallbacks.lower() in ('true', '1', 'yes', 'on')
            
            fallback_override = None if use_fallbacks else 0
            
            failed_count = total_chunks - total_with_data
            if failed_count > 0:
                if use_fallbacks:
                    log.debug(f"BackgroundDDSBuilder: {tile_id} - {failed_count}/{total_chunks} chunks failed, "
                             f"will apply fallbacks")
                else:
                    log.debug(f"BackgroundDDSBuilder: {tile_id} - {failed_count}/{total_chunks} chunks failed, "
                             f"will use missing color (fallbacks disabled)")
            
            # Step 2: Get mipmap 0 image to determine DDS dimensions
            img0 = tile.get_img(0, startrow=0, endrow=None, maxwait=30,
                               fallback_level_override=fallback_override)
            
            if img0 is None:
                log.debug(f"BackgroundDDSBuilder: {tile_id} - mipmap 0 get_img returned None")
                self._builds_failed += 1
                return
            
            mipmap_images.append(img0)
            width, height = img0.size
            
            # Step 3: Create DDS and compress mipmap 0
            use_ispc = CFG.pydds.compressor.upper() == "ISPC"
            dxt_format = CFG.pydds.format
            
            temp_dds = pydds.DDS(width, height, ispc=use_ispc, dxt_format=dxt_format)
            
            # Compress mipmap 0 only (not generating lower mipmaps from it)
            temp_dds.gen_mipmaps(img0, startmipmap=0, maxmipmaps=1)
            
            # Step 4: Build each subsequent mipmap from its native chunks
            # This matches on-demand behavior where X-Plane might request
            # any mipmap first and get native-resolution chunks
            for mipmap in range(1, tile.max_mipmap + 1):
                mipmap_zoom = tile.max_zoom - mipmap
                if mipmap_zoom < tile.min_zoom:
                    break
                
                # Get native image for this mipmap level
                img = tile.get_img(mipmap, startrow=0, endrow=None, maxwait=30,
                                  fallback_level_override=fallback_override)
                
                if img is None:
                    # Fall back to generating remaining mipmaps by downscaling
                    log.debug(f"BackgroundDDSBuilder: {tile_id} - mipmap {mipmap} get_img failed, "
                             f"will generate by downscaling")
                    # Generate remaining mipmaps from what we have
                    temp_dds.gen_mipmaps(img0, startmipmap=mipmap, maxmipmaps=99)
                    break
                
                mipmap_images.append(img)
                
                # Compress just this mipmap from native image
                temp_dds.gen_mipmaps(img, startmipmap=mipmap, maxmipmaps=1)
            
            # Step 5: Read out the complete DDS as bytes
            dds_bytes = temp_dds.read(temp_dds.total_size)
            
            if not dds_bytes or len(dds_bytes) < 128:
                log.debug(f"BackgroundDDSBuilder: {tile_id} - DDS read failed")
                self._builds_failed += 1
                return
            
            # Step 6: Store in prebuilt cache
            self._prebuilt_cache.store(tile_id, dds_bytes)
            
            build_time = (time.monotonic() - build_start) * 1000
            self._builds_completed += 1
            
            log.debug(f"BackgroundDDSBuilder: Built {tile_id} in {build_time:.0f}ms "
                     f"({len(dds_bytes)} bytes, {len(mipmap_images)} native mipmaps)")
            bump('prebuilt_dds_builds')
            
            # Schedule bundle consolidation for all zoom levels
            consolidate_tile_if_ready(tile, "background_dds")
            
        except Exception as e:
            log.warning(f"BackgroundDDSBuilder: Failed to build {tile_id}: {e}")
            self._builds_failed += 1
        finally:
            # Release tile lock if we acquired it
            # This MUST come before image cleanup to avoid holding the lock
            # longer than necessary
            if lock_acquired:
                try:
                    tile._lock.release()
                except Exception:
                    pass  # Lock may have been released elsewhere (shouldn't happen)
            
            # Clean up all mipmap images
            for img in mipmap_images:
                try:
                    img.close()
                except Exception:
                    pass
    
    @property
    def queue_size(self) -> int:
        """Current number of tiles waiting to be built."""
        return self._queue.qsize()
    
    @property
    def stats(self) -> dict:
        """Return builder statistics."""
        return {
            'queue_size': self._queue.qsize(),
            'builds_completed': self._builds_completed,
            'builds_failed': self._builds_failed,
            'interval_ms': self._build_interval * 1000
        }


# Global instances for predictive DDS generation (initialized in start_predictive_dds)
prebuilt_dds_cache: Optional[PrebuiltDDSCache] = None
background_dds_builder: Optional[BackgroundDDSBuilder] = None
tile_completion_tracker: Optional[TileCompletionTracker] = None

# Bundle consolidator for AOB2 format (consolidates JPEGs into bundles)
_bundle_consolidator = None


def schedule_bundle_consolidation(tile, priority: int = 0, jpeg_datas: list = None, zoom: int = None) -> bool:
    """
    Schedule bundle consolidation for a tile after successful build.
    
    This is called from ALL tile build paths (live, streaming, prefetch) to
    ensure JPEGs are consolidated into bundles for faster future access.
    
    CRITICAL: Bundle paths are based on TILE ID coordinates (tile.row, tile.col at tilename_zoom),
    NOT the scaled chunk coordinates! This ensures bundles can be found when looking up by tile ID.
    
    The bundle stores chunks at their respective zoom levels internally.
    
    PERFORMANCE OPTIMIZATION:
    When jpeg_datas is provided, the consolidator uses in-memory data directly,
    avoiding disk reads per tile.
    
    Args:
        tile: The tile that was successfully built
        priority: Consolidation priority (0=HIGH for live, 1=NORMAL for prefetch)
        jpeg_datas: Optional list of JPEG bytes for each chunk. If provided,
                    consolidator uses this data directly instead of reading from disk.
        zoom: Specific zoom level to consolidate. If None, consolidates max_zoom only.
    
    Returns:
        True if scheduled successfully, False otherwise
    """
    if _bundle_consolidator is None or tile is None:
        return False
    
    try:
        # Determine which zoom level to consolidate
        if zoom is None:
            target_zoom = tile.max_zoom
        else:
            target_zoom = zoom
        
        # Get chunk grid dimensions for the target zoom level
        # This gives us the scaled coordinates where chunks are stored
        chunk_col, chunk_row, width, height, effective_zoom, _ = tile._get_quick_zoom(target_zoom)
        
        # Calculate chunks_per_side from scaled dimensions
        chunks_per_side = width  # Assuming square tiles (width == height)
        
        # CRITICAL: Bundle path uses TILE ID coordinates (tile.row, tile.col at tilename_zoom)
        # NOT the scaled chunk coordinates! This ensures we can find the bundle when looking up by tile ID.
        # The consolidator needs:
        # - tile_row, tile_col: for bundle path (at tilename_zoom)
        # - tilename_zoom: for bundle path directory structure
        # - chunk_row, chunk_col: where chunks are stored (at effective_zoom)
        # - effective_zoom: the actual zoom level of the chunks
        _bundle_consolidator.schedule(
            row=chunk_row,  # Chunk grid origin row
            col=chunk_col,  # Chunk grid origin col
            maptype=tile.maptype,
            zoom=effective_zoom,
            priority=priority,
            chunks_per_side=chunks_per_side,
            jpeg_datas=jpeg_datas,
            # NEW: Pass tile ID coordinates for bundle path
            tile_row=tile.row,
            tile_col=tile.col,
            tile_zoom=tile.tilename_zoom
        )
        bump('bundle_consolidation_scheduled')
        if jpeg_datas:
            bump('bundle_consolidation_in_memory')
        
        # TEMP STAT: Sync consolidator queue stats to global stats periodically
        # (every 10 schedules to avoid overhead)
        scheduled_count = STATS.get('bundle_consolidation_scheduled', 0)
        if scheduled_count % 10 == 0:
            try:
                cstats = _bundle_consolidator.get_stats()
                STATS['consolidator_queue_current'] = cstats.get('queue_size_current', 0)
                STATS['consolidator_queue_max'] = cstats.get('queue_size_max', 0)
                STATS['consolidator_time_max_ms'] = int(cstats.get('tile_consolidation_max_ms', 0))
                # Add detailed bundle stats for diagnostics
                STATS['consolidator_bundles_created'] = cstats.get('bundles_created', 0)
                STATS['consolidator_bundles_updated'] = cstats.get('bundles_updated', 0)
                STATS['consolidator_bundles_merged'] = cstats.get('bundles_merged', 0)
                STATS['consolidator_bundles_skipped'] = cstats.get('bundles_skipped', 0)
            except Exception:
                pass
        
        return True
    except Exception as e:
        log.debug(f"Failed to schedule bundle consolidation for tile {tile.row},{tile.col} at zoom {zoom}: {e}")
        return False


def schedule_bundle_consolidation_all_zooms(tile, priority: int = 0) -> int:
    """
    Schedule bundle consolidation for ALL zoom levels of a tile.
    
    A DDS tile uses chunks at multiple zoom levels for different mipmaps:
    - Mipmap 0: max_zoom (e.g., ZL17) - 16×16 chunks
    - Mipmap 1: max_zoom-1 (e.g., ZL16) - 8×8 chunks
    - Mipmap 2: max_zoom-2 (e.g., ZL15) - 4×4 chunks
    - etc., down to min_zoom
    
    This function schedules consolidation for each zoom level that has cached chunks.
    
    Args:
        tile: The tile to consolidate
        priority: Consolidation priority
    
    Returns:
        Number of zoom levels scheduled for consolidation
    """
    if _bundle_consolidator is None or tile is None:
        return 0
    
    scheduled = 0
    
    # Consolidate all zoom levels from max_zoom down to min_zoom
    for mipmap in range(tile.max_mipmap + 1):
        zoom = tile.max_zoom - mipmap
        if zoom < tile.min_zoom:
            break
        
        # Get chunks for this zoom level if they exist
        zoom_chunks = tile.chunks.get(zoom, [])
        if zoom_chunks:
            # Collect JPEG data from in-memory chunks if available
            jpeg_datas = [chunk.data if hasattr(chunk, 'data') and chunk.data else None 
                          for chunk in zoom_chunks]
            
            if schedule_bundle_consolidation(tile, priority=priority, jpeg_datas=jpeg_datas, zoom=zoom):
                scheduled += 1
                log.debug(f"CONSOLIDATE_ALL: Scheduled ZL{zoom} for tile {tile.id}")
        else:
            log.debug(f"CONSOLIDATE_ALL: No chunks at ZL{zoom} for tile {tile.id}")
    
    if scheduled > 0:
        log.debug(f"Scheduled {scheduled} zoom levels for consolidation: {tile.id}")
    
    return scheduled


def consolidate_tile_if_ready(tile, source: str = "unknown") -> bool:
    """
    Single entry point for tile consolidation.
    
    Call this after any successful tile build to consolidate all zoom levels.
    The function handles deduplication internally.
    
    Args:
        tile: The tile to consolidate
        source: Debug identifier for logging (e.g., "background_dds", "aopipeline")
    
    Returns:
        True if consolidation was scheduled, False otherwise
    """
    if tile is None or _bundle_consolidator is None:
        return False
    
    # Priority: background builds use lower priority (1), live uses high (0)
    priority = 1 if source in ("background_dds", "prefetch") else 0
    
    try:
        scheduled = schedule_bundle_consolidation_all_zooms(tile, priority=priority)
        if scheduled > 0:
            log.debug(f"Consolidation scheduled ({source}): {scheduled} zooms for tile {tile.id}")
        return scheduled > 0
    except Exception as e:
        log.debug(f"Consolidation scheduling failed ({source}): {e}")
        return False


def _on_tile_complete_callback(tile_id: str, tile) -> None:
    """
    Callback invoked when all chunks for a tile have been downloaded.
    Submits the tile to the background DDS builder and schedules bundle consolidation.
    """
    if background_dds_builder is not None:
        background_dds_builder.submit(tile)
    
    # Schedule bundle consolidation for ALL zoom levels of this tile
    consolidate_tile_if_ready(tile, "prefetch")


def start_predictive_dds(tile_cacher=None) -> None:
    """
    Initialize and start the predictive DDS generation system.
    
    Pre-builds DDS textures in the background and stores them on disk.
    When X-Plane requests tiles, they're served from disk cache (~1-2ms)
    instead of being built on-demand (~100-500ms), eliminating stutters.
    
    Note: Uses disk-only caching because:
    - SSD read latency (1-2ms) is negligible compared to build time (100-500ms)
    - OS file cache naturally keeps hot files in RAM
    - Simplifies memory management and reduces RAM usage
    
    Should be called after start_prefetcher().
    
    Args:
        tile_cacher: TileCacher instance (for future use)
    """
    global prebuilt_dds_cache, background_dds_builder, tile_completion_tracker, _bundle_consolidator

    # Prevent duplicate initialization (Windows/Linux: all mounts share one process)
    if background_dds_builder is not None:
        log.debug("Predictive DDS already initialized, skipping")
        return

    # Check if enabled
    enabled = getattr(CFG.autoortho, 'predictive_dds_enabled', True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ('true', '1', 'yes', 'on')
    
    if not enabled:
        log.info("Predictive DDS generation disabled by configuration")
        return
    
    # Get configuration
    disk_cache_mb = int(getattr(CFG.autoortho, 'ephemeral_dds_cache_mb', 4096))
    disk_cache_mb = max(1024, min(16384, disk_cache_mb))  # Min 1GB, max 16GB
    
    build_interval_ms = int(getattr(CFG.autoortho, 'predictive_dds_build_interval_ms', 500))
    build_interval_ms = max(100, min(2000, build_interval_ms))
    build_interval_sec = build_interval_ms / 1000.0
    
    background_builder_workers = int(getattr(CFG.autoortho, 'background_builder_workers', 2))
    background_builder_workers = max(1, min(8, background_builder_workers))  # 1-8 workers
    
    live_builder_concurrency = int(getattr(CFG.autoortho, 'live_builder_concurrency', 4))
    live_builder_concurrency = max(1, min(32, live_builder_concurrency))  # 1-32 workers
    
    total_builders = background_builder_workers + live_builder_concurrency
    
    # Initialize decoder pool with size based on total_builders × CPU threads
    # This allows parallel JPEG decoding across all concurrent tile builds
    try:
        from autoortho.aopipeline import AoDDS
        decoder_pool_size = AoDDS.calculate_decoder_pool_size(total_builders)
        if AoDDS.init_decoder_pool(decoder_pool_size):
            log.info(f"Decoder pool initialized: {decoder_pool_size} decoders "
                    f"({total_builders} builders × {CURRENT_CPU_COUNT} threads)")
        else:
            log.debug("Decoder pool already initialized (using existing size)")
    except ImportError:
        log.debug("AoDDS not available, using default decoder pool")
    except Exception as e:
        log.warning(f"Failed to initialize decoder pool: {e}")
    
    # Initialize disk-only cache
    # Disk reads (~1-2ms) are fast enough - no need for RAM cache overhead
    # OS file cache naturally keeps hot files in memory
    prebuilt_dds_cache = EphemeralDDSCache(max_size_mb=disk_cache_mb)
    
    background_dds_builder = BackgroundDDSBuilder(
        prebuilt_cache=prebuilt_dds_cache,
        build_interval_sec=build_interval_sec,
        max_workers=background_builder_workers
    )
    
    tile_completion_tracker = TileCompletionTracker(
        on_tile_complete=_on_tile_complete_callback
    )
    
    # Initialize bundle consolidator for AOB2 format
    bundle_consolidation_enabled = getattr(CFG.autoortho, 'bundle_consolidation_enabled', True)
    if isinstance(bundle_consolidation_enabled, str):
        bundle_consolidation_enabled = bundle_consolidation_enabled.lower() in ('true', '1', 'yes', 'on')
    
    if bundle_consolidation_enabled:
        try:
            # Handle imports for both frozen (PyInstaller) and direct Python execution
            try:
                from autoortho.aopipeline.bundle_consolidator import BundleConsolidator
            except ImportError:
                from aopipeline.bundle_consolidator import BundleConsolidator
            
            cache_dir = CFG.paths.cache_dir
            _bundle_consolidator = BundleConsolidator(
                cache_dir=cache_dir,
                delete_jpegs=True,  # Remove individual JPEGs after consolidation
                max_workers=4,
                enabled=True
            )
            log.info(f"Bundle consolidation enabled (AOB2 format) - bundles saved to {cache_dir}/bundles/")
        except ImportError as e:
            log.info(f"Bundle consolidator not available (import failed): {e}")
        except Exception as e:
            log.warning(f"Failed to initialize bundle consolidator: {e}")
    else:
        log.info("Bundle consolidation disabled by config")
    
    # Start the builder thread
    background_dds_builder.start()
    
    log.info(f"Predictive DDS generation started "
            f"(disk_cache={disk_cache_mb}MB, interval={build_interval_ms}ms)")


def stop_predictive_dds() -> None:
    """Stop the predictive DDS generation system and cleanup disk cache."""
    global background_dds_builder, tile_completion_tracker, prebuilt_dds_cache, _bundle_consolidator
    
    # Shutdown bundle consolidator first (let pending consolidations complete)
    if _bundle_consolidator is not None:
        try:
            stats = _bundle_consolidator.get_stats()
            _bundle_consolidator.shutdown(wait=True)
            log.info(f"Bundle consolidator: {stats['bundles_created']} bundles created, "
                    f"{stats['jpegs_consolidated']} JPEGs consolidated")
            # TEMP STATS: Log queue and timing info for timeout tuning
            log.info(f"Bundle consolidator timing: "
                    f"avg={stats.get('tile_consolidation_avg_ms', 0):.1f}ms, "
                    f"max={stats.get('tile_consolidation_max_ms', 0):.1f}ms, "
                    f"min={stats.get('tile_consolidation_min_ms', 0):.1f}ms")
            log.info(f"Bundle consolidator queue: "
                    f"max_seen={stats.get('queue_size_max', 0)}, "
                    f"avg={stats.get('queue_size_avg', 0):.1f}")
            
            # Orphan JPEG cleanup (if enabled)
            cleanup_orphans = getattr(CFG.autoortho, 'cleanup_orphan_jpegs_on_exit', True)
            if isinstance(cleanup_orphans, str):
                cleanup_orphans = cleanup_orphans.lower() in ('true', '1', 'yes', 'on')
            
            if cleanup_orphans:
                try:
                    try:
                        from autoortho.aopipeline.bundle_consolidator import cleanup_orphan_jpegs
                    except ImportError:
                        from aopipeline.bundle_consolidator import cleanup_orphan_jpegs
                    
                    cache_dir = str(CFG.paths.cache_dir)
                    deleted, scanned = cleanup_orphan_jpegs(cache_dir)
                    if deleted > 0:
                        log.info(f"Orphan cleanup: deleted {deleted}/{scanned} JPEGs (data safe in bundles)")
                except Exception as e:
                    log.debug(f"Orphan cleanup failed: {e}")
        except Exception as e:
            log.warning(f"Error shutting down bundle consolidator: {e}")
        _bundle_consolidator = None
    
    if background_dds_builder is not None:
        stats = background_dds_builder.stats
        background_dds_builder.stop()
        log.info(f"BackgroundDDSBuilder: {stats['builds_completed']} tiles built, "
                f"{stats['builds_failed']} failed")
    
    if prebuilt_dds_cache is not None:
        stats = prebuilt_dds_cache.stats
        # EphemeralDDSCache uses different stat keys than PrebuiltDDSCache
        hits = stats.get('hits', 0)
        misses = stats.get('misses', 0)
        hit_rate = stats.get('hit_rate', 0)
        size_mb = stats.get('size_mb', stats.get('memory_mb', 0))
        log.info(f"DDS disk cache: {hits} hits, {misses} misses, "
                f"{hit_rate:.1f}% hit rate, {size_mb:.1f}MB used")
        
        # Clean up disk cache files
        prebuilt_dds_cache.cleanup()
    
    # Clear references
    background_dds_builder = None
    tile_completion_tracker = None
    prebuilt_dds_cache = None


# HTTP status codes that indicate permanent failure (no retry)
PERMANENT_FAILURE_CODES = {400, 401, 404, 405, 406, 410, 451}

# HTTP status codes that need special handling
TRANSIENT_FAILURE_CODES = {408, 429, 500, 502, 503, 504}

# Max retries for transient failures before giving up
MAX_TRANSIENT_RETRIES = {
    408: 3,   # Request timeout - network issue
    429: 10,  # Rate limit - needs backoff
    500: 15,  # Internal error - might recover
    502: 8,   # Bad gateway - infrastructure
    503: 8,   # Service unavailable - infrastructure  
    504: 5,   # Gateway timeout - infrastructure
}

# Maximum total attempts (including initial + all retries) before giving up
# This prevents infinite retry loops for persistent failures (e.g., network issues,
# invalid responses) that don't return specific HTTP error codes
MAX_TOTAL_ATTEMPTS = 15


class Chunk(object):
    col = -1
    row = -1
    source = None
    chunk_id = ""
    priority = 0
    width = 256
    height = 256
    cache_dir = 'cache'
    
    attempt = 0

    starttime = 0
    fetchtime = 0

    ready = None
    data = None
    img = None
    url = None
    
    # Parent tile ID for completion tracking (predictive DDS generation)
    tile_id = None

    serverlist=['a','b','c','d']

    def __init__(self, col, row, maptype, zoom, priority=0, cache_dir='.cache', tile_id=None,
                 skip_cache_check=False):
        self.col = col
        self.row = row
        self.zoom = zoom
        self.maptype = maptype
        self.cache_dir = cache_dir
        self.tile_id = tile_id  # Parent tile ID for completion tracking
        
        # Hack override maptype
        #self.maptype = "BI"

        # Set priority: use provided value or default to zoom level
        self.priority = priority if priority else zoom
        self.chunk_id = f"{col}_{row}_{zoom}_{maptype}"
        self.ready = threading.Event()
        self.ready.clear()
        self.download_started = threading.Event()  # Signal when download thread begins
        self.download_started.clear()
        if maptype == "Null":
            self.maptype = "EOX"

        # Failure tracking for circuit breaker
        self.permanent_failure = False
        self.failure_reason = None
        self.retry_count = 0

        # Coalescing flags to prevent duplicate submissions
        self.in_queue = False
        self.in_flight = False

        self.cache_path = os.path.join(self.cache_dir, f"{self.chunk_id}.jpg")
        
        # Check cache during initialization and set ready if found
        # skip_cache_check=True allows batch cache reading optimization
        if not skip_cache_check and self.get_cache():
            self.download_started.set()  # Cache hit = "download" done immediately
            self.ready.set()
            log.debug(f"Chunk {self} initialized with cached data")
    
    def set_cached_data(self, data: bytes):
        """
        Set chunk data from externally-read cache (e.g., native batch read).
        
        This method is used by the batch cache reading optimization to apply
        data that was read in parallel using native code.
        
        Args:
            data: JPEG bytes from cache file
        """
        if not data:
            return False
        self.data = data
        self.download_started.set()
        self.ready.set()
        bump('chunk_hit')
        return True

    def __lt__(self, other):
        return self.priority < other.priority

    def __repr__(self):
        return f"Chunk({self.col},{self.row},{self.maptype},{self.zoom},{self.priority})"

    def get_cache(self):
        if os.path.isfile(self.cache_path):
            bump('chunk_hit')
            cache_file = Path(self.cache_path)
            # Get data
            data = None
            # On Windows, the cache file can be briefly locked by AV or a concurrent writer.
            # Add a short retry/backoff loop to avoid spurious PermissionError / sharing violations.
            max_attempts = 5
            for attempt in range(1, max_attempts + 1):
                try:
                    data = cache_file.read_bytes()
                    break
                except PermissionError as e:
                    if attempt < max_attempts:
                        time.sleep(0.02 * attempt)
                        continue
                    log.warning(f"Permission denied reading cache {self}: {e}")
                    return False
                except FileNotFoundError:
                    # Raced with a concurrent replace/remove; treat as miss
                    return False
                except OSError as e:
                    winerr = getattr(e, 'winerror', None)
                    if winerr in (5, 32, 33) and attempt < max_attempts:
                        time.sleep(0.02 * attempt)
                        continue
                    log.debug(f"OSError reading cache {self}: {e}")
                    return False

            cache_file.touch()
            # Update modified data
            try:
                os.utime(self.cache_path, None)
            except (FileNotFoundError, PermissionError):
                pass 

            if _is_jpeg(data[:3]):
                #print(f"Found cache that is JPEG for {self}")
                self.data = data
                return True
            else:
                log.info(f"Loading file {self} not a JPEG! {data[:3]} path: {self.cache_path}")
                self.data = b''
                return False  # FIXED: Explicitly return False for corrupted cache
        else:
            bump('chunk_miss')
            return False

    def save_cache(self):
        # Snapshot data to avoid races with close() mutating self.data
        data = self.data
        if not data:
            return

        # Check if cache directory still exists (may have been deleted by temp cleanup)
        if not os.path.exists(self.cache_dir):
            log.debug(f"Cache directory gone for {self}, skipping save")
            return

        # Ensure cache directory exists
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except (FileNotFoundError, OSError):
            # Directory path is invalid or was deleted
            log.debug(f"Cannot create cache directory for {self}, skipping save")
            return
        except Exception:
            pass

        # Unique temp filename per writer to avoid collisions between threads/tiles
        temp_filename = os.path.join(self.cache_dir, f"{self.chunk_id}_{uuid.uuid4().hex}.tmp")

        # Write data to the unique temp file first
        try:
            with open(temp_filename, 'wb') as h:
                h.write(data)
        except FileNotFoundError as e:
            # Directory was deleted between check and write (race with temp cleanup)
            log.debug(f"Cache directory deleted during save for {self}: {e}")
            return
        except Exception as e:
            # Could not write temp file
            try:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
            except Exception:
                pass
            log.warning(f"Failed to save cache for {self}: {e}")
            return

        # Try to move into place atomically with a few retries for WinError 32
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if os.path.exists(self.cache_path):
                    # Another writer got there first; clean up our temp
                    os.remove(temp_filename)
                    log.debug(f"Cache file already exists for {self}, skipping save (race) on attempt {attempt}")
                    return
                os.replace(temp_filename, self.cache_path)
                return
            except FileExistsError:
                try:
                    os.remove(temp_filename)
                except Exception:
                    pass
                log.debug(f"Another thread saved cache for {self}, removed temp file")
                return
            except OSError as e:
                if getattr(e, 'winerror', None) in (5, 32, 33) and attempt < max_attempts:
                    time.sleep(0.05 * attempt)
                    if os.path.exists(self.cache_path):
                        try:
                            if os.path.exists(temp_filename):
                                os.remove(temp_filename)
                        except Exception:
                            pass
                        return
                    continue
                try:
                    if os.path.exists(temp_filename):
                        os.remove(temp_filename)
                except Exception:
                    pass
                log.warning(f"Failed to save cache for {self}: {e}")
                return

    def get(self, idx=0, session=requests):
        log.debug(f"Getting {self}")
        
        # Signal that download has started (not waiting in queue anymore)
        self.download_started.set()

        if self.get_cache():
            self.ready.set()
            return True

        # === TOTAL ATTEMPT LIMIT ===
        # Prevent infinite retries for persistent failures (network issues,
        # invalid responses, etc.) that don't return specific HTTP codes
        if self.attempt >= MAX_TOTAL_ATTEMPTS:
            log.warning(f"Chunk {self} exceeded {MAX_TOTAL_ATTEMPTS} total attempts, marking as permanently failed")
            self.permanent_failure = True
            self.failure_reason = "max_total_attempts"
            self.data = b''
            self.ready.set()
            bump('chunk_max_attempts_exhausted')
            # Notify tile completion tracker
            try:
                if tile_completion_tracker is not None and self.tile_id:
                    tile_completion_tracker.notify_chunk_ready(self.tile_id, self)
            except Exception:
                pass
            return True  # Return True to stop worker retries

        if not self.starttime:
            self.starttime = time.time()

        server_num = idx % (len(self.serverlist))
        server = self.serverlist[server_num]
        quadkey = _gtile_to_quadkey(self.col, self.row, self.zoom)

        # Hack override maptype
        #maptype = "ARC"

        MAPID = "s2cloudless-2023_3857"
        MATRIXSET = "g"
        MAPTYPES = {
            "EOX": f"https://{server}.tiles.maps.eox.at/wmts/?layer={MAPID}&style=default&tilematrixset={MATRIXSET}&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image%2Fjpeg&TileMatrix={self.zoom}&TileCol={self.col}&TileRow={self.row}",
            "BI": f"https://t.ssl.ak.tiles.virtualearth.net/tiles/a{quadkey}.jpeg?g=15312",
            "GO2": f"http://mts{server_num}.google.com/vt/lyrs=s&x={self.col}&y={self.row}&z={self.zoom}",
            "ARC": f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "NAIP": f"http://naip.maptiles.arcgis.com/arcgis/rest/services/NAIP/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "USGS": f"https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "FIREFLY": f"https://fly.maptiles.arcgis.com/arcgis/rest/services/World_Imagery_Firefly/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "YNDX": f"https://sat{server_num+1:02d}.maps.yandex.net/tiles?l=sat&v=3.1814.0&x={self.col}&y={self.row}&z={self.zoom}",
            "APPLE": f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v={apple_token_service.version}&accessKey={apple_token_service.apple_token}"
        }

        MAPTYPES_WITH_SERVER = ["YNDX", "EOX", "GO2"]

        self.url = MAPTYPES[self.maptype.upper()]
        #log.debug(f"{self} getting {url}")
        header = {
                "user-agent": "curl/7.68.0"
        }
        if self.maptype.upper() == "EOX":
            log.debug("EOX DETECTED")
            header.update({'referer': 'https://s2maps.eu/'})
       
        # Capped backoff: grows with attempts but maxes at 2 seconds
        # This prevents runaway delays when server is slow/throttling
        backoff_sleep = min(2.0, self.attempt / 10.0)
        time.sleep(backoff_sleep)
        self.attempt += 1

        log.debug(f"Requesting {self.url} ..")
        
        resp = None
        try:

            resp = session.get(self.url, headers=header, timeout=(5, 20))
            status_code = resp.status_code

            if self.maptype.upper() == "APPLE" and status_code in (403, 410):
                log.warning("APPLE tile got %s; rotating token and retrying", status_code)
                apple_token_service.reset_apple_maps_token()
                MAPTYPES["APPLE"] = f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v={apple_token_service.version}&accessKey={apple_token_service.apple_token}"
                self.url = MAPTYPES[self.maptype.upper()]
                if resp is not None:
                    resp.close()
                resp = session.get(self.url, headers=header, timeout=(5, 20))
                status_code = resp.status_code

            if status_code != 200:
                log.warning(f"Failed with status {status_code} to get chunk {self}" + (" on server " + server if self.maptype.upper() in MAPTYPES_WITH_SERVER else "") + ".")
                bump_many({f"http_{status_code}": 1, "req_err": 1})
                
                # Check if this is a permanent failure
                if status_code in PERMANENT_FAILURE_CODES:
                    log.info(f"Chunk {self} permanently failed with {status_code}, marking as failed")
                    self.permanent_failure = True
                    self.failure_reason = status_code
                    self.data = b''  # Empty data
                    self.ready.set()  # Mark as ready (with no data) to unblock waiters
                    bump(f'chunk_permanent_fail_{status_code}')
                    # Notify tile completion tracker even on failure
                    # This allows DDS prebuild to proceed with available chunks
                    # (fallbacks will be applied during build for failed chunks)
                    try:
                        if tile_completion_tracker is not None and self.tile_id:
                            tile_completion_tracker.notify_chunk_ready(self.tile_id, self)
                    except Exception:
                        pass
                    return True  # Return True to stop worker retries
                
                # Check if transient failure has exceeded max retries
                if status_code in TRANSIENT_FAILURE_CODES:
                    self.retry_count += 1
                    max_retries = MAX_TRANSIENT_RETRIES.get(status_code, 5)
                    if self.retry_count >= max_retries:
                        log.warning(f"Chunk {self} exceeded {max_retries} retries for {status_code}, giving up")
                        self.permanent_failure = True
                        self.failure_reason = f"{status_code}_max_retries"
                        self.data = b''
                        self.ready.set()
                        bump(f'chunk_transient_fail_{status_code}_exhausted')
                        # Notify tile completion tracker even on exhausted retries
                        try:
                            if tile_completion_tracker is not None and self.tile_id:
                                tile_completion_tracker.notify_chunk_ready(self.tile_id, self)
                        except Exception:
                            pass
                        return True
                    # Increase backoff for rate limiting
                    if status_code == 429:
                        backoff_time = min(5, self.retry_count * 0.5)
                        log.debug(f"Rate limited, backing off for {backoff_time}s (attempt {self.retry_count}/{max_retries})")
                        time.sleep(backoff_time)
                    bump(f'chunk_transient_fail_{status_code}_retry')

                err = get_stat("req_err")
                if err > 50:
                    ok = get_stat("req_ok")
                    error_rate = err / ( err + ok )
                    if error_rate >= 0.10:
                        log.error(f"Very high network error rate detected : {error_rate * 100 : .2f}%")
                        log.error(f"Check your network connection, DNS, maptype choice, and firewall settings.")
                        # Enhanced circuit breaker: reduce wait times on severe error rates
                        if error_rate >= 0.25:
                            log.warning("Severe error rate (≥25%) detected, consider checking configuration")
                return False

            bump("req_ok")

            data = resp.content

            if _is_jpeg(data[:3]):
                log.debug(f"Data for {self} is JPEG")
                self.data = data
            else:
                # FFD8FF identifies image as a JPEG
                log.debug(f"Loading file {self} not a JPEG! {data[:3]} URL: {self.url}")
            #    return False
                self.data = b''

            bump('bytes_dl', len(self.data))
                
        except Exception as err:
            log.warning(f"Failed to get chunk {self} on server {server}. Err: {err} URL: {self.url}")
            return False
        finally:
            if resp:
                resp.close()

        self.fetchtime = time.monotonic() - self.starttime

        # Signal ready IMMEDIATELY - data is in memory for consumers
        # Tile composition and bundle consolidation use chunk.data directly
        self.ready.set()
        
        # Track slow downloads for visibility in stats
        try:
            duration_ms = int((self.fetchtime or 0) * 1000)
            if duration_ms > 5000:  # >5s is noteworthy
                bump('chunk_slow_download')
            if duration_ms > 15000:  # >15s is very slow
                bump('chunk_very_slow_download')
        except Exception:
            pass
        
        # Notify tile completion tracker (for predictive DDS generation)
        # MUST happen before async cache write to ensure in-memory data capture
        # by bundle consolidation (which uses chunk.data directly)
        try:
            if tile_completion_tracker is not None and self.tile_id:
                tile_completion_tracker.notify_chunk_ready(self.tile_id, self)
        except Exception:
            pass  # Never block downloads
        
        # ASYNC CACHE WRITE: Fire-and-forget background disk write
        # The in-memory chunk.data is already captured by consumers above.
        # Disk write is only for future session restarts (cache persistence).
        # save_cache() snapshots self.data locally, so it's safe even if
        # chunk.close() clears self.data before the write completes.
        try:
            _cache_write_executor.submit(_async_cache_write, self)
        except Exception:
            # Executor full or shutdown - fall back to sync write
            self.save_cache()
        
        return True

    def close(self):
        """Release all references held by this Chunk so its memory can be reclaimed."""

        # Release image buffer if we created one
        if hasattr(self, 'img') and self.img is not None:
            try:
                # AoImage instances have a close() that frees underlying C memory
                if hasattr(self.img, "close"):
                    self.img.close()
            finally:
                self.img = None

        # Remove raw JPEG bytes
        self.data = None


class Tile(object):
    row = -1
    col = -1
    maptype = None
    zoom = -1
    min_zoom = 12
    width = 16
    height = 16
    baseline_zl = 12

    priority = -1
    #tile_condition = None
    _lock = None
    ready = None 

    chunks = None
    cache_file = None
    dds = None

    refs = None

    maxchunk_wait = float(CFG.autoortho.maxwait)
    imgs = None
    
    # Maximum cached images per tile to prevent unbounded memory growth
    # Each AoImage holds ~64MB of RGBA data for 4096x4096 textures
    # 4 images is sufficient for upscaling fallback logic (checks mipmap 1-4)
    _IMGS_MAX_SIZE = 4
    
    # Maximum fallback chunks per tile to prevent unbounded memory growth
    # Fallback chunks are shared parent chunks used when child chunks fail
    _FALLBACK_POOL_MAX_SIZE = 100

    def __init__(self, col, row, maptype, zoom, min_zoom=0, priority=0,
            cache_dir=None, max_zoom=None):
        self.row = int(row)
        self.col = int(col)
        self.maptype = maptype
        self.tilename_zoom = int(zoom)
        self.chunks = {}
        self.ready = threading.Event()
        self._lock = threading.RLock()
        self.refs = 0
        self.imgs = {}
        self._imgs_order = []  # Track insertion order for LRU eviction

        self.bytes_read = 0
        self.lowest_offset = 99999999
        
        # Track if tile was pre-populated by aopipeline (all mipmaps built at once)
        # When True, the "bytes_read" warning is not applicable because X-Plane
        # may only need small mipmaps even though we populated everything
        self._prepopulated = False
        
        # Track when this tile was first requested for accurate tile creation time stats
        self.first_request_time = None
        # Track if tile completion has been reported (to avoid double-counting)
        self._completion_reported = False
        # Tile-level time budget - shared across all mipmap builds for this tile
        # This ensures the budget limits the ENTIRE tile processing, not per-mipmap
        self._tile_time_budget = None
        
        # === SHARED FALLBACK CHUNK POOL ===
        # When multiple chunks fail and need the same parent chunk at a lower zoom,
        # we share the download instead of fetching it multiple times.
        # Key: (col, row, zoom) -> Chunk object
        # Example: 4 ZL16 chunks that all need (2,2,ZL15) share one download.
        self._fallback_chunk_pool = {}
        self._fallback_pool_lock = threading.Lock()
        
        # Track if lazy mipmap building has been triggered for this tile
        # This prevents repeated attempts after the first failure triggers a lazy build
        self._lazy_build_attempted = False
        
        # Track if aopipeline live build has been attempted for this tile
        # This prevents repeated attempts - if aopipeline fails once, use progressive path
        # Reset when tile is closed for potential reuse
        self._aopipeline_attempted = False
        
        # === PREFETCH-TO-LIVE TRANSITION TRACKING ===
        # When X-Plane requests a tile that's being prefetched, we transition
        # to "live" mode: apply time budget, boost chunk priorities
        self._is_live = False                           # True when X-Plane requests via FUSE
        self._live_transition_event = None              # Event to signal transition
        self._active_streaming_builder = None           # Reference for transition coordination
        
        # === BATCH-TO-STREAMING DATA REUSE ===
        # When batch aopipeline collects data but fails (ratio below threshold),
        # store the collected data for streaming builder to reuse
        self._last_collected_jpegs = None               # List of JPEG bytes (None for missing)
        self._last_collected_ratio = None               # Ratio of available chunks
        self._last_collected_missing = None             # List of missing chunk indices

        # Closed flag: set by close() so external holders
        # (BackgroundDDSBuilder, TileCompletionTracker) can detect
        # evicted tiles and skip stale work.
        self._closed = False

        #self.tile_condition = threading.Condition()
        if min_zoom:
            self.min_zoom = int(min_zoom)


        if cache_dir:
            self.cache_dir = cache_dir
        else:
            self.cache_dir = CFG.paths.cache_dir


        # Set max zoom level - if not specified, use original tile zoom (no capping)
        self.max_zoom = int(max_zoom) if max_zoom is not None else self.tilename_zoom
        # Hack override maptype
        #self.maptype = "BI"

        self.ready.clear()

        if not priority:
            self.priority = zoom

        if not os.path.isdir(self.cache_dir):
            os.makedirs(self.cache_dir)

        if CFG.pydds.compressor.upper() == "ISPC":
            use_ispc=True
        else:
            use_ispc=False

        # TODO VRAM usage optimization only getting the max ZL of the zone instead of the default max zoom
        # This is very time consuming while loading flight, left commented out for now
        # self.actual_max_zoom = self._detect_available_max_zoom()
        self.tilezoom_diff = self.tilename_zoom - self.max_zoom

        if self.tilezoom_diff >= 0:

            self.chunks_per_row = self.width >> self.tilezoom_diff
            self.chunks_per_col = self.height >> self.tilezoom_diff
        else:
            self.chunks_per_row = self.width << (-self.tilezoom_diff)
            self.chunks_per_col = self.height << (-self.tilezoom_diff)


        if self.tilezoom_diff < 0:
            if self.tilezoom_diff < -1:
                raise ValueError(f"Tilezoom_diff is {self.tilezoom_diff} which is less than -1, which is not supported by X-Plane.")
            self.max_mipmap = 5 # Enforce a maximum of 5 mipmaps (8192 -> 256 max)
        else:
            self.max_mipmap = 4 # Enforce a maximum of 4 mipmaps (4096 -> 256 max)

        self.chunks_per_row = max(1, self.chunks_per_row)
        self.chunks_per_col = max(1, self.chunks_per_col)

        dds_width = self.chunks_per_row * 256
        dds_height = self.chunks_per_col * 256
        log.debug(f"Creating DDS at original size: {dds_width}x{dds_height} (ZL{self.max_zoom})")
            
        self.dds = pydds.DDS(dds_width, dds_height, ispc=use_ispc,
                dxt_format=CFG.pydds.format)

        self.id = f"{row}_{col}_{maptype}_{self.tilename_zoom}"


    def __lt__(self, other):
        return self.priority < other.priority

    def __repr__(self):
        return f"Tile({self.col}, {self.row}, {self.maptype}, {self.tilename_zoom}, min_zoom={self.min_zoom}, max_zoom={self.max_zoom}, max_mm={self.max_mipmap})"

    def _cache_image(self, mipmap: int, img_data: tuple):
        """Cache an image with LRU eviction when limit reached.
        
        This prevents unbounded memory growth from accumulating AoImage objects.
        Each AoImage holds native RGBA pixel buffer (~64MB for 4096x4096).
        
        Args:
            mipmap: Mipmap level (0 = full resolution)
            img_data: Tuple of (image, col, row, zoom) metadata
        """
        # If already cached, just update (move to end for LRU)
        if mipmap in self.imgs:
            try:
                self._imgs_order.remove(mipmap)
            except ValueError:
                pass  # Not in order list, that's fine
            self._imgs_order.append(mipmap)
            # Close old image before replacing
            old_data = self.imgs.get(mipmap)
            if old_data is not None:
                if isinstance(old_data, tuple):
                    im = old_data[0]
                else:
                    im = old_data
                if im is not None and hasattr(im, 'close'):
                    try:
                        im.close()
                    except Exception:
                        pass
            self.imgs[mipmap] = img_data
            return
        
        # Evict oldest if at limit
        while len(self.imgs) >= self._IMGS_MAX_SIZE and self._imgs_order:
            oldest = self._imgs_order.pop(0)
            old_data = self.imgs.pop(oldest, None)
            if old_data is not None:
                # Close the old image to free native memory immediately
                if isinstance(old_data, tuple):
                    im = old_data[0]
                else:
                    im = old_data
                if im is not None and hasattr(im, 'close'):
                    try:
                        im.close()
                    except Exception:
                        pass
        
        # Insert new
        self.imgs[mipmap] = img_data
        self._imgs_order.append(mipmap)

    def _wait_for_pending_consolidation_if_needed(self, timeout: float = 5.0) -> bool:
        """
        Wait for pending consolidation OUTSIDE any locks.
        
        Must be called BEFORE _create_chunks() to avoid blocking other threads
        while waiting for consolidation. This prevents the scenario where thread A
        holds the tile lock for 30 seconds waiting for consolidation while thread B
        (wanting the same tile) is blocked.
        
        Args:
            timeout: Maximum time to wait in seconds (default 30s)
            
        Returns:
            True if consolidation completed (or wasn't pending)
            False if still pending after timeout
        """
        global _bundle_consolidator
        if _bundle_consolidator is None:
            return True
        
        if not _bundle_consolidator.is_pending(self.row, self.col, self.maptype):
            return True
        
        # Determine timeout based on time budget (if live request)
        if hasattr(self, '_tile_time_budget') and self._tile_time_budget is not None:
            wait_timeout = max(0.5, min(self._tile_time_budget.remaining, timeout))
        else:
            wait_timeout = timeout
        
        bump('bundle_wait_started')
        log.debug(f"Waiting for consolidation outside lock: {self.id}")
        
        if _bundle_consolidator.wait_for_pending(self.row, self.col, self.maptype, timeout=wait_timeout):
            bump('bundle_wait_success')
            return True
        
        bump('bundle_wait_timeout')
        return False

    def _schedule_immediate_consolidation(self, jpeg_datas: list, zoom: int) -> bool:
        """
        Schedule consolidation immediately while JPEG data is in memory.
        
        This ensures that zoom levels are consolidated as soon as their data
        is collected, before chunks can be garbage collected or evicted.
        Critical for ensuring lower zoom levels (mipmaps) are consolidated
        into the bundle, not just max_zoom.
        
        Args:
            jpeg_datas: List of JPEG bytes (None for missing chunks)
            zoom: Zoom level of the data
            
        Returns:
            True if scheduling succeeded, False otherwise
        """
        try:
            valid_count = sum(1 for d in jpeg_datas if d is not None)
            if valid_count == 0:
                return False
            
            # Use the existing schedule_bundle_consolidation function
            result = schedule_bundle_consolidation(
                self, 
                priority=1,  # Normal priority (not urgent)
                jpeg_datas=jpeg_datas, 
                zoom=zoom
            )
            
            if result:
                bump('consolidation_scheduled_immediate')
                log.debug(f"Scheduled immediate consolidation for {self.id} ZL{zoom} "
                         f"({valid_count}/{len(jpeg_datas)} chunks)")
            
            return result
        except Exception as e:
            log.debug(f"_schedule_immediate_consolidation failed: {e}")
            return False

    @locked
    def _create_chunks(self, quick_zoom=0, min_zoom=None):
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(quick_zoom, min_zoom)

        if not self.chunks.get(zoom):
            self.chunks[zoom] = []
            log.debug(f"CREATE_CHUNKS: Tile {self.id} creating chunks for zoom {zoom}: {width}x{height} grid starting at ({col},{row})")
            
            # Try bundle-first read (AOB2 format - much faster than individual files)
            bundle_loaded = False
            try:
                bundle_loaded = self._try_load_from_bundle(col, row, width, height, zoom)
            except Exception as e:
                log.warning(f"Bundle read error for {self.id}: {e}")
            
            if bundle_loaded:
                # Track bundle hits for monitoring cache effectiveness
                bump('bundle_chunks_loaded', len(self.chunks[zoom]))
                return
            else:
                # Track bundle misses - will fall through to individual file loading
                bump('bundle_cache_miss')

            # Check if native batch cache reading is available
            native_cache = _get_native_cache()
            use_batch_read = native_cache is not None
            
            # Create all chunks (skip individual cache checks if batch reading)
            for r in range(row, row+height):
                for c in range(col, col+width):
                    chunk = Chunk(c, r, self.maptype, zoom, cache_dir=self.cache_dir, 
                                  tile_id=self.id, skip_cache_check=use_batch_read)
                    self.chunks[zoom].append(chunk)
            
            # Native batch cache read: read all cache files in parallel using C code
            if use_batch_read:
                paths = [chunk.cache_path for chunk in self.chunks[zoom]]
                cached_data = _batch_read_cache_files(paths)
                
                if cached_data:
                    hits = 0
                    for chunk in self.chunks[zoom]:
                        if chunk.cache_path in cached_data:
                            chunk.set_cached_data(cached_data[chunk.cache_path])
                            hits += 1
                    if hits > 0:
                        log.debug(f"Native batch cache read: {hits}/{len(paths)} hits for zoom {zoom}")
                
                # SIMPLIFIED BUNDLE RECOVERY
                # If batch read missed files, try bundle once (no waiting)
                missing_count = len(paths) - len(cached_data)
                if missing_count > 0:
                    try:
                        try:
                            from autoortho.utils.bundle_paths import get_bundle2_path
                            from autoortho.aopipeline import AoBundle2
                        except ImportError:
                            from utils.bundle_paths import get_bundle2_path
                            from aopipeline import AoBundle2
                        
                        bundle_path = get_bundle2_path(
                            self.cache_dir, self.row, self.col, 
                            self.maptype, self.tilename_zoom
                        )
                        
                        if os.path.exists(bundle_path):
                            bump('bundle_fallback_attempted')
                            with AoBundle2.Bundle2Python(bundle_path) as bundle:
                                if bundle.has_zoom(zoom):
                                    jpeg_datas = bundle.get_all_chunks(zoom)
                                    if jpeg_datas:
                                        recovered = 0
                                        for i, chunk in enumerate(self.chunks[zoom]):
                                            if chunk.cache_path not in cached_data and i < len(jpeg_datas):
                                                if jpeg_datas[i]:
                                                    chunk.set_cached_data(jpeg_datas[i])
                                                    recovered += 1
                                        if recovered > 0:
                                            bump('bundle_fallback_success')
                                            log.debug(f"Bundle fallback: recovered {recovered}/{missing_count} chunks")
                    except Exception as e:
                        log.debug(f"Bundle fallback failed: {e}")
        else:
            log.debug(f"Reusing existing {len(self.chunks[zoom])} chunks for zoom {zoom}")

    def _try_load_from_bundle(self, col: int, row: int, width: int, height: int, zoom: int) -> bool:
        """
        Try to load chunks from an AOB2 bundle file.
        
        Bundle loading is much faster than individual file reads because:
        - Single file open instead of 256+ opens
        - Sequential read for optimal disk I/O
        - Memory-mappable for zero-copy access
        
        If the bundle doesn't exist but consolidation is pending, waits briefly
        for the consolidation to complete before falling back to individual files.
        
        Args:
            col, row: Starting chunk coordinates (SCALED for the target zoom level)
            width, height: Chunk grid dimensions
            zoom: Zoom level
        
        Returns:
            True if bundle was loaded successfully, False otherwise
        """
        global _bundle_consolidator
        
        try:
            # Handle imports for both frozen (PyInstaller) and direct Python execution
            try:
                from autoortho.utils.bundle_paths import get_bundle2_path, bundle_exists
                from autoortho.aopipeline import AoBundle2
            except ImportError:
                from utils.bundle_paths import get_bundle2_path, bundle_exists
                from aopipeline import AoBundle2
            
            # CRITICAL: Use TILE ID coordinates (self.row, self.col at tilename_zoom) for bundle path!
            # Bundles are named after the tile ID, NOT the scaled chunk coordinates.
            # The bundle stores chunks at their respective zoom levels internally.
            bundle_path = get_bundle2_path(self.cache_dir, self.row, self.col, self.maptype, self.tilename_zoom)
            
            if not os.path.exists(bundle_path):
                # Bundle doesn't exist - consolidation wait is now done OUTSIDE the lock
                # via _wait_for_pending_consolidation_if_needed() called before _create_chunks()
                # If bundle still doesn't exist here, fall back to individual files
                return False
            
            # Open bundle and read chunks - use context manager to release file immediately
            # Critical: On Windows, mmap holds file open and blocks consolidation updates
            max_attempts = 3
            retry_delay = 0.2
            jpeg_datas = None
            last_error = None
            for attempt in range(max_attempts):
                try:
                    with AoBundle2.Bundle2Python(bundle_path) as bundle:
                        # Check if bundle has this zoom level
                        if not bundle.has_zoom(zoom):
                            log.debug(f"Bundle missing zoom {zoom} for {self.id}")
                            return False
                        
                        # Get all chunk data from bundle (returns copies, safe after close)
                        jpeg_datas = bundle.get_all_chunks(zoom)
                    # Bundle is now closed - file handle released
                    break
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        time.sleep(retry_delay * (attempt + 1))
                        continue
                    log.warning(f"Failed to load bundle for {self.id} after {max_attempts} attempts: {e}")
                    return False

            if jpeg_datas is None:
                log.debug(f"Bundle read returned no data for {self.id}: {last_error}")
                return False
            
            expected_count = width * height
            
            if len(jpeg_datas) != expected_count:
                log.warning(f"Bundle chunk count mismatch: {len(jpeg_datas)} vs {expected_count} for {self.id}")
                return False
            
            # Count valid chunks
            valid_count = sum(1 for d in jpeg_datas if d is not None)
            
            # Create chunks with data from bundle
            chunk_idx = 0
            for r in range(row, row + height):
                for c in range(col, col + width):
                    chunk = Chunk(c, r, self.maptype, zoom, cache_dir=self.cache_dir,
                                  tile_id=self.id, skip_cache_check=True)
                    
                    # Set cached data if available
                    jpeg_data = jpeg_datas[chunk_idx]
                    if jpeg_data is not None:
                        chunk.set_cached_data(jpeg_data)
                    
                    self.chunks[zoom].append(chunk)
                    chunk_idx += 1
            
            bump('bundle_cache_hit')
            return True
            
        except ImportError:
            log.debug("Bundle modules not available")
            return False
        except Exception as e:
            log.warning(f"Failed to load bundle for {self.id}: {e}")
            return False

    def _try_build_dds_from_bundle(self, zoom: int, dxt_format: str = "BC1",
                                    missing_color: Tuple[int, int, int] = (66, 77, 55)) -> Optional[bytes]:
        """
        Try to build complete DDS from bundle using native C code.
        
        This is the fastest path when a bundle exists with all required chunks.
        Uses native aobundle2_build_dds which:
        - Reads bundle (single file open)
        - Decodes all JPEGs in parallel (OpenMP)
        - Builds DDS in one pass
        
        Args:
            zoom: Zoom level to build DDS for
            dxt_format: DXT compression format ("BC1" or "BC3")
            missing_color: RGB tuple for missing chunk fill color
            
        Returns:
            Complete DDS file as bytes, or None if bundle doesn't exist/have zoom level
            
        Thread Safety:
            - Uses context manager for Bundle2Python (releases mmap immediately)
            - Native build_dds opens/closes file internally
        """
        try:
            # Handle imports for both frozen (PyInstaller) and direct Python execution
            try:
                from autoortho.utils.bundle_paths import get_bundle2_path
                from autoortho.aopipeline import AoBundle2
            except ImportError:
                from utils.bundle_paths import get_bundle2_path
                from aopipeline import AoBundle2
            
            # Get bundle path for this tile
            bundle_path = get_bundle2_path(self.cache_dir, self.row, self.col, self.maptype, self.tilename_zoom)
            
            if not os.path.exists(bundle_path):
                return None
            
            # Check if bundle has this zoom level - use context manager to release file
            # immediately so native build_dds can open it
            with AoBundle2.Bundle2Python(bundle_path) as bundle:
                has_zoom = bundle.has_zoom(zoom)
            # Bundle is now closed - file handle released
            
            if not has_zoom:
                return None
            
            # Native DDS build (parallel JPEG decode + DXT compress)
            # This is ~3-4x faster than sequential Python processing
            try:
                dds_bytes = AoBundle2.build_dds(bundle_path, zoom, dxt_format, missing_color)
                bump('bundle_dds_build_success')
                return dds_bytes
            except Exception as e:
                log.debug(f"Native bundle DDS build failed: {e}")
                return None
                
        except ImportError:
            log.debug("Bundle modules not available for DDS build")
            return None
        except Exception as e:
            log.debug(f"_try_build_dds_from_bundle failed: {e}")
            return None

    def _try_get_chunks_from_bundle(self, zoom: int) -> Optional[List[Optional[bytes]]]:
        """
        Try to get all chunk JPEG data from bundle for a specific zoom level.
        
        This provides bulk access to chunk data from a bundle, which is faster
        than reading individual cache files when the bundle exists.
        
        Args:
            zoom: Zoom level to get chunks for
            
        Returns:
            List of JPEG bytes (None for missing chunks), or None if bundle unavailable
            
        Thread Safety:
            - Uses context manager for Bundle2Python (releases mmap immediately)
        """
        try:
            # Handle imports for both frozen (PyInstaller) and direct Python execution
            try:
                from autoortho.utils.bundle_paths import get_bundle2_path
                from autoortho.aopipeline import AoBundle2
            except ImportError:
                from utils.bundle_paths import get_bundle2_path
                from aopipeline import AoBundle2
            
            # Get bundle path for this tile
            bundle_path = get_bundle2_path(self.cache_dir, self.row, self.col, self.maptype, self.tilename_zoom)
            
            if not os.path.exists(bundle_path):
                return None
            
            # Read all chunks at once - use context manager to release file immediately
            with AoBundle2.Bundle2Python(bundle_path) as bundle:
                if not bundle.has_zoom(zoom):
                    return None
                jpeg_datas = bundle.get_all_chunks(zoom)
            # Bundle is now closed - file handle released
            
            return jpeg_datas
            
        except ImportError:
            log.debug("Bundle modules not available for chunk read")
            return None
        except Exception as e:
            log.debug(f"_try_get_chunks_from_bundle failed: {e}")
            return None

    def _collect_chunk_jpegs(self, zoom: int, time_budget=None,
                              min_available_ratio: float = 0.9,
                              return_partial: bool = False):
        """
        Collect JPEG data from chunks for aopipeline build.
        
        This method efficiently gathers JPEG data from chunks using a two-phase approach:
        1. INSTANT: Check already-ready chunks (in memory or disk cache)
        2. BUDGET-LIMITED: Download missing chunks using FULL remaining time_budget
        
        This is the data gathering phase for aopipeline integration. It separates
        the I/O concern from the build concern for cleaner architecture.
        
        Args:
            zoom: Zoom level to collect chunks for
            time_budget: TimeBudget for download phase - uses FULL remaining time
            min_available_ratio: Minimum ratio of chunks needed (0.0-1.0)
                                 Default 0.9 = 90% chunks required
            return_partial: If True, always return collected data even if below threshold
                           If False (default), return None when threshold not met
        
        Returns:
            List of JPEG bytes (None for missing chunks) if ratio met or return_partial=True
            None if insufficient chunks available and return_partial=False
        
        Thread Safety:
            - Chunk.data access uses GIL-protected reference copy
            - chunk_getter.submit() is thread-safe
            - No locks held during wait (allows concurrent operations)
        """
        # Wait for any pending consolidation OUTSIDE lock to avoid blocking other threads
        self._wait_for_pending_consolidation_if_needed()
        
        # Ensure chunks exist for this zoom level
        self._create_chunks(zoom)
        chunks = self.chunks.get(zoom, [])
        
        if not chunks:
            log.debug(f"_collect_chunk_jpegs: No chunks for zoom {zoom}")
            return None
        
        total_chunks = len(chunks)
        jpeg_datas = [None] * total_chunks
        available_count = 0
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 1: Collect already-ready chunks (INSTANT)
        # ═══════════════════════════════════════════════════════════════════════
        # Check chunks that are either:
        # - Already in memory (prefetched or previously downloaded)
        # - In disk cache (use BATCH reading for ~50x faster I/O)
        # This phase has zero network latency.
        
        # First pass: collect chunks already in memory
        need_cache_read_indices = []
        for i, chunk in enumerate(chunks):
            # TOCTOU safety: capture reference atomically (GIL protects this)
            chunk_data = chunk.data
            
            if chunk.ready.is_set() and chunk_data:
                # Already in memory
                jpeg_datas[i] = chunk_data
                available_count += 1
            elif not chunk.ready.is_set():
                # Not ready - need to check disk cache
                need_cache_read_indices.append(i)
        
        # BATCH CACHE READ: Read all missing chunks in parallel using native code
        # This is ~50x faster than individual get_cache() calls (1 batch vs 256 syscalls)
        if need_cache_read_indices:
            cache_paths = [chunks[i].cache_path for i in need_cache_read_indices]
            cached_data = _batch_read_cache_files(cache_paths)
            
            if cached_data:
                # Apply batch-read data to chunks
                for i in need_cache_read_indices:
                    chunk = chunks[i]
                    if chunk.cache_path in cached_data:
                        data = cached_data[chunk.cache_path]
                        # Update chunk state (same as get_cache() would do)
                        chunk.set_cached_data(data)
                        jpeg_datas[i] = data
                        available_count += 1
                        bump('chunk_hit')
                
                log.debug(f"_collect_chunk_jpegs: Batch cache read - "
                         f"{len(cached_data)}/{len(cache_paths)} hits")
                
                # ═══════════════════════════════════════════════════════════
                # JPEG-TO-BUNDLE FALLBACK: Handle race condition with consolidator
                # ═══════════════════════════════════════════════════════════
                # If batch read missed files, consolidator may have deleted them
                # while creating a bundle. Check if bundle now exists, or wait
                # for pending consolidation to avoid re-downloading.
                still_missing = [i for i in need_cache_read_indices 
                                if jpeg_datas[i] is None]
                if still_missing:
                    try:
                        try:
                            from autoortho.utils.bundle_paths import get_bundle2_path
                            from autoortho.aopipeline import AoBundle2
                        except ImportError:
                            from utils.bundle_paths import get_bundle2_path
                            from aopipeline import AoBundle2
                        
                        bundle_path = get_bundle2_path(self.cache_dir, self.row, self.col, self.maptype, self.tilename_zoom)
                        bundle_datas = None
                        bundle_has_data = False
                        
                        # SMART FALLBACK: Only attempt if bundle exists
                        bundle_exists = os.path.exists(bundle_path)
                        if not bundle_exists:
                            bump('jpeg_to_bundle_fallback_skipped')
                        
                        # First check: bundle exists?
                        if bundle_exists:
                            with AoBundle2.Bundle2Python(bundle_path) as bundle:
                                if bundle.has_zoom(zoom):
                                    bundle_datas = bundle.get_all_chunks(zoom)
                                    bundle_has_data = True
                        
                        # Second check: consolidation pending or recently completed?
                        # CRITICAL: Pass zoom to is_pending() - only wait for OUR zoom!
                        if not bundle_has_data and _bundle_consolidator is not None:
                            bump('jpeg_to_bundle_fallback_entered')
                            waited_for_consolidation = False
                            if _bundle_consolidator.is_pending(self.row, self.col, self.maptype, zoom):
                                log.debug(f"_collect_chunk_jpegs: waiting for pending consolidation (zoom {zoom})")
                                bump('jpeg_to_bundle_wait_started')
                                wait_start = time.monotonic()
                                # Pass zoom to wait for THIS specific zoom level's consolidation
                                wait_result = _bundle_consolidator.wait_for_pending(self.row, self.col, self.maptype, timeout=60.0, zoom=zoom)
                                wait_time = time.monotonic() - wait_start
                                waited_for_consolidation = True
                                
                                if wait_result:
                                    bump('jpeg_to_bundle_wait_success')
                                    if wait_time > 5.0:
                                        bump('jpeg_to_bundle_wait_long')
                                        log.debug(f"_collect_chunk_jpegs: waited {wait_time:.1f}s for consolidation")
                                elif wait_time > 30.0:
                                    bump('jpeg_to_bundle_wait_timeout')
                                    log.warning(f"_collect_chunk_jpegs: consolidation wait TIMEOUT after {wait_time:.1f}s - "
                                               f"tile {self.row}_{self.col}_{self.maptype} may have stuck consolidation")
                                else:
                                    bump('jpeg_to_bundle_wait_already_done')
                            
                            # ALWAYS try bundle - even if is_pending was False
                            # Consolidation may have completed between first check and now
                            if not bundle_has_data and os.path.exists(bundle_path):
                                bump('jpeg_to_bundle_fallback_bundle_exists')
                                with AoBundle2.Bundle2Python(bundle_path) as bundle:
                                    if bundle.has_zoom(zoom):
                                        bump('jpeg_to_bundle_fallback_has_zoom')
                                        bundle_datas = bundle.get_all_chunks(zoom)
                                        bundle_has_data = True
                                        if not waited_for_consolidation:
                                            bump('jpeg_to_bundle_fallback_not_pending')
                                    else:
                                        bump('jpeg_to_bundle_fallback_no_zoom')
                        
                        # Apply recovered data
                        if bundle_has_data and bundle_datas:
                            fallback_hits = 0
                            for i in still_missing:
                                if i < len(bundle_datas) and bundle_datas[i]:
                                    chunks[i].set_cached_data(bundle_datas[i])
                                    jpeg_datas[i] = bundle_datas[i]
                                    available_count += 1
                                    fallback_hits += 1
                            if fallback_hits > 0:
                                log.debug(f"_collect_chunk_jpegs: JPEG-to-bundle fallback - "
                                         f"recovered {fallback_hits}/{len(still_missing)} chunks")
                                bump('jpeg_to_bundle_fallback')
                    except Exception as e:
                        log.debug(f"_collect_chunk_jpegs: JPEG-to-bundle fallback failed: {e}")
                # ═══════════════════════════════════════════════════════════
            else:
                # Batch read failed or unavailable - fall back to individual reads
                for i in need_cache_read_indices:
                    chunk = chunks[i]
                    if chunk.get_cache():
                        chunk_data = chunk.data
                        if chunk_data:
                            jpeg_datas[i] = chunk_data
                            available_count += 1
        
        # Check if we already have enough from instant phase
        ratio = available_count / total_chunks
        if ratio >= min_available_ratio:
            log.debug(f"_collect_chunk_jpegs: Phase 1 sufficient - "
                      f"{available_count}/{total_chunks} chunks ({ratio*100:.0f}%)")
            # Schedule consolidation immediately while data is in memory
            # This ensures the zoom level gets consolidated before chunks are GC'd
            self._schedule_immediate_consolidation(jpeg_datas, zoom)
            return jpeg_datas
        
        log.debug(f"_collect_chunk_jpegs: Phase 1 - {available_count}/{total_chunks} "
                  f"chunks ({ratio*100:.0f}%), need {min_available_ratio*100:.0f}%")
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 2: Download missing chunks using FULL remaining budget
        # ═══════════════════════════════════════════════════════════════════════
        # For chunks not in cache, submit download requests and wait.
        # Uses the FULL remaining time_budget to maximize chunk collection.
        # This ensures batch aopipeline gets maximum opportunity to reach threshold.
        
        # Determine download wait time - use FULL remaining budget
        if time_budget and not time_budget.exhausted:
            wait_time = time_budget.remaining
        else:
            # No budget - use a reasonable default
            wait_time = 5.0
        
        if wait_time <= 0:
            log.debug(f"_collect_chunk_jpegs: No time for Phase 2 downloads")
            if return_partial:
                return jpeg_datas  # Return what we have
            return None
        
        # Find missing chunk indices
        missing_indices = [i for i, d in enumerate(jpeg_datas) if d is None]
        
        if not missing_indices:
            # Shouldn't happen, but handle gracefully
            return jpeg_datas
        
        # Submit all missing chunks for high-priority download
        for i in missing_indices:
            chunk = chunks[i]
            if not chunk.in_queue and not chunk.in_flight and not chunk.ready.is_set():
                chunk.priority = 0  # Highest priority for live requests
                chunk_getter.submit(chunk)
        
        # Wait for downloads with polling (allows early exit)
        deadline = time.monotonic() + wait_time
        poll_interval = 0.02  # 20ms polling for responsiveness
        
        while time.monotonic() < deadline:
            # Check newly ready chunks
            newly_available = 0
            for i in missing_indices:
                if jpeg_datas[i] is not None:
                    continue  # Already collected
                
                chunk = chunks[i]
                chunk_data = chunk.data  # TOCTOU-safe capture
                
                if chunk.ready.is_set() and chunk_data:
                    jpeg_datas[i] = chunk_data
                    available_count += 1
                    newly_available += 1
            
            # Check if we have enough now
            ratio = available_count / total_chunks
            if ratio >= min_available_ratio:
                log.debug(f"_collect_chunk_jpegs: Phase 2 success - "
                          f"{available_count}/{total_chunks} ({ratio*100:.0f}%)")
                return jpeg_datas
            
            # Brief sleep to avoid busy-wait
            time.sleep(poll_interval)
        
        # Final check after deadline
        ratio = available_count / total_chunks
        if ratio >= min_available_ratio:
            log.debug(f"_collect_chunk_jpegs: Phase 2 final success - "
                      f"{available_count}/{total_chunks} ({ratio*100:.0f}%)")
            # Schedule consolidation immediately while data is in memory
            self._schedule_immediate_consolidation(jpeg_datas, zoom)
            return jpeg_datas
        
        log.debug(f"_collect_chunk_jpegs: Threshold not met - "
                  f"{available_count}/{total_chunks} ({ratio*100:.0f}%), "
                  f"below {min_available_ratio*100:.0f}% threshold")
        
        # Store collected data for potential reuse by streaming builder
        # Even though ratio is below threshold, this data is still valuable
        self._last_collected_jpegs = jpeg_datas
        self._last_collected_ratio = ratio
        self._last_collected_missing = [i for i, d in enumerate(jpeg_datas) if d is None]
        
        # Schedule consolidation for partial data too - merge logic will fill in
        # missing chunks later when they're downloaded
        if available_count > 0:
            self._schedule_immediate_consolidation(jpeg_datas, zoom)
        
        # If return_partial requested, return what we collected (for build-as-is mode)
        if return_partial:
            return jpeg_datas
        
        return None

    def _try_aopipeline_build(self, time_budget=None, force_build_partial: bool = False) -> bool:
        """
        Attempt to build entire DDS using optimized aopipeline.
        
        This is the FAST PATH for live tile builds when chunks are available.
        Uses buffer pool and parallel native processing for ~5x speedup over
        the progressive pydds path.
        
        Strategy:
        1. Collect JPEG data using FULL remaining time_budget
        2. If threshold met OR force_build_partial=True: build with aopipeline
        3. Populate all mipmap buffers from result
        4. Return True on success, False to trigger streaming/fallback path
        
        Flow when fallbacks are disabled:
        - Collects chunks for full tile_time_budget
        - Builds with whatever is collected (fills missing with missing_color)
        - No handoff to streaming pipeline
        
        Flow when fallbacks are enabled:
        - Collects chunks for full tile_time_budget
        - If threshold met: builds directly
        - If threshold not met: returns False, streaming pipeline handles fallbacks
        
        Performance:
        - Success path: ~55-65ms (vs ~331ms for progressive)
        - Failure path: ~0-2ms overhead, then streaming path runs
        
        Thread Safety:
        - Caller should hold tile lock (or ensure single-threaded access)
        - Buffer pool has its own thread-safe acquire/release
        - Native build releases GIL during C execution
        
        Args:
            time_budget: TimeBudget - uses FULL remaining time for chunk collection
            force_build_partial: If True, build with whatever chunks are available
                                even if below threshold (for fallbacks-disabled mode)
        
        Returns:
            True if aopipeline build succeeded (all mipmaps populated)
            False if should fall back to streaming/progressive path
        """
        build_start = time.monotonic()
        
        # ═══════════════════════════════════════════════════════════════════════
        # CHECK 1: Is aopipeline enabled in config?
        # ═══════════════════════════════════════════════════════════════════════
        if not getattr(CFG.autoortho, 'live_aopipeline_enabled', True):
            return False
        
        # ═══════════════════════════════════════════════════════════════════════
        # CHECK 2: Is native DDS module available with required functions?
        # ═══════════════════════════════════════════════════════════════════════
        native_dds = _get_native_dds()
        if native_dds is None:
            log.debug(f"_try_aopipeline_build: Native DDS not available")
            return False
        
        if not hasattr(native_dds, 'build_from_jpegs_to_buffer'):
            log.debug(f"_try_aopipeline_build: build_from_jpegs_to_buffer not available")
            return False
        
        # ═══════════════════════════════════════════════════════════════════════
        # CHECK 3: Is buffer pool available?
        # ═══════════════════════════════════════════════════════════════════════
        pool = _get_dds_buffer_pool()
        if pool is None:
            log.debug(f"_try_aopipeline_build: Buffer pool not available")
            return False
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: Collect JPEG data from chunks using FULL remaining budget
        # ═══════════════════════════════════════════════════════════════════════
        # Uses the entire remaining time_budget to maximize chunk collection.
        # If force_build_partial is True, we'll build with whatever we get.
        min_ratio = float(getattr(CFG.autoortho, 'live_aopipeline_min_chunk_ratio', 0.9))
        
        jpeg_datas = self._collect_chunk_jpegs(
            self.max_zoom,
            time_budget=time_budget,
            min_available_ratio=min_ratio,
            return_partial=force_build_partial  # Return data even if below threshold
        )
        
        if jpeg_datas is None:
            # Threshold not met and not forcing partial build
            log.debug(f"_try_aopipeline_build: Threshold not met for {self.id}, "
                      f"deferring to streaming pipeline for fallbacks")
            return False
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: Acquire buffer from pool (PRIORITY_LIVE = front of queue)
        # ═══════════════════════════════════════════════════════════════════════
        # Live tiles are "premium clients" - they always go to the front of the
        # queue and are served before any prefetch tiles. Uses blocking acquire
        # with priority queue (bank-queue style) - no fallback allocation.
        # ═══════════════════════════════════════════════════════════════════════
        wait_start = time.monotonic()
        try:
            # Get timeout from remaining time budget or use default
            if time_budget is not None and hasattr(time_budget, 'remaining'):
                acquire_timeout = max(1.0, time_budget.remaining)
            else:
                acquire_timeout = 30.0  # Default 30s timeout
            
            buffer, buffer_id = pool.acquire(timeout=acquire_timeout, priority=PRIORITY_LIVE)
            
            # Track queue wait time
            wait_time_ms = (time.monotonic() - wait_start) * 1000
            if wait_time_ms > 10:  # Only track significant waits
                bump('live_queue_wait_count')
                log.debug(f"_try_aopipeline_build: Waited {wait_time_ms:.0f}ms for buffer for {self.id}")
            
        except TimeoutError:
            wait_time_ms = (time.monotonic() - wait_start) * 1000
            log.debug(f"_try_aopipeline_build: Queue timeout after {wait_time_ms:.0f}ms for {self.id}")
            bump('live_queue_timeout')
            return False
        
        build_success = False
        
        try:
            # ═══════════════════════════════════════════════════════════════
            # STEP 3: Get DDS format settings
            # ═══════════════════════════════════════════════════════════════
            dxt_format = CFG.pydds.format.upper()
            if dxt_format in ("DXT1", "BC1"):
                dxt_format = "BC1"
            else:
                dxt_format = "BC3"
            
            missing_color = tuple(CFG.autoortho.missing_color[:3]) if hasattr(CFG.autoortho, 'missing_color') else (66, 77, 55)
            
            # ═══════════════════════════════════════════════════════════════
            # STEP 4: Build DDS with native aopipeline
            # ═══════════════════════════════════════════════════════════════
            result = native_dds.build_from_jpegs_to_buffer(
                buffer,
                jpeg_datas,
                format=dxt_format,
                missing_color=missing_color
            )
            
            if not result.success:
                log.debug(f"_try_aopipeline_build: Native build failed for {self.id}: {result.error}")
                bump('live_aopipeline_build_failed')
                return False
            
            if result.bytes_written < 128:
                log.debug(f"_try_aopipeline_build: Build produced too few bytes: {result.bytes_written}")
                return False
            
            # ═══════════════════════════════════════════════════════════════
            # STEP 5: Copy DDS to bytes and populate tile's DDS structure
            # ═══════════════════════════════════════════════════════════════
            # Use to_bytes() to extract from buffer
            dds_bytes = result.to_bytes()
            
            # Reuse existing _populate_dds_from_prebuilt (proven, tested)
            # This populates all mipmap buffers and marks them as retrieved
            if not self._populate_dds_from_prebuilt(dds_bytes):
                log.debug(f"_try_aopipeline_build: Failed to populate DDS for {self.id}")
                bump('live_aopipeline_populate_failed')
                return False
            
            # Success!
            build_time = (time.monotonic() - build_start) * 1000
            log.debug(f"_try_aopipeline_build: SUCCESS for {self.id} - "
                      f"{result.bytes_written} bytes in {build_time:.0f}ms")
            build_success = True
            bump('live_aopipeline_success')
            
            # Schedule bundle consolidation for ALL zoom levels
            consolidate_tile_if_ready(self, "aopipeline")
            
        except Exception as e:
            log.debug(f"_try_aopipeline_build: Exception for {self.id}: {e}")
            bump('live_aopipeline_exception')
            build_success = False
        
        finally:
            # Always release buffer back to pool
            pool.release(buffer_id)
        
        return build_success

    def _try_streaming_aopipeline_build(self, time_budget=None) -> bool:
        """
        Build DDS using streaming aopipeline with fallback integration.
        
        This is the new streaming builder approach that:
        1. Accepts chunks incrementally as they download
        2. Applies user-configured fallbacks for missing chunks
        3. Uses the same logic for both live and prefetch paths
        
        Flow:
        1. Acquire streaming builder from pool
        2. Submit download requests for all chunks
        3. As chunks complete:
           a. If success: add_chunk with JPEG data
           b. If fail: resolve fallback, add_fallback_image or mark_missing
        4. When all chunks processed OR budget exhausted: finalize
        
        Args:
            time_budget: Optional TimeBudget to limit processing time
        
        Returns:
            True if build succeeded, False to fall back to progressive path
        """
        build_start = time.monotonic()
        
        # Check if streaming builder is enabled
        if not getattr(CFG.autoortho, 'streaming_builder_enabled', True):
            return False
        
        # Check if native DDS module is available
        try:
            from autoortho.aopipeline.AoDDS import get_default_builder_pool, get_default_pool
            from autoortho.aopipeline.fallback_resolver import FallbackResolver, TimeBudget as FBTimeBudget
        except ImportError as e:
            log.debug(f"_try_streaming_aopipeline_build: Imports not available: {e}")
            return False
        
        builder_pool = get_default_builder_pool()
        if builder_pool is None:
            log.debug(f"_try_streaming_aopipeline_build: Builder pool not available")
            return False
        
        # Get configuration
        fallback_level = self._get_fallback_level()
        dxt_format = CFG.pydds.format.upper()
        if dxt_format in ("DXT1", "BC1"):
            dxt_format = "BC1"
        else:
            dxt_format = "BC3"
        
        missing_color = tuple(CFG.autoortho.missing_color[:3]) if hasattr(CFG.autoortho, 'missing_color') else (66, 77, 55)
        
        # Create fallback resolver
        resolver = FallbackResolver(
            cache_dir=self.cache_dir,
            maptype=self.maptype,
            tile_col=self.col,
            tile_row=self.row,
            tile_zoom=self.max_zoom,
            fallback_level=fallback_level,
            max_mipmap=self.max_mipmap,
            downloader=None  # Network fallback handled separately
        )
        
        # Set available mipmap images for scaling fallback
        resolver.set_mipmap_images(self.imgs)
        
        # ═══════════════════════════════════════════════════════════════════════
        # OPTIMIZATION: Reuse data collected by batch aopipeline
        # ═══════════════════════════════════════════════════════════════════════
        # Check for pre-collected data BEFORE acquiring builder so we can set nocopy_mode
        pre_collected = getattr(self, '_last_collected_jpegs', None)
        pre_collected_missing = getattr(self, '_last_collected_missing', None)
        use_precollected = (pre_collected is not None and pre_collected_missing is not None)
        
        # Acquire streaming builder from pool
        # Enable nocopy_mode when using pre-collected data for zero-copy optimization
        config = {
            'chunks_per_side': self.chunks_per_row,
            'format': dxt_format,
            'missing_color': missing_color,
            'nocopy_mode': use_precollected,  # Zero-copy when we hold JPEG refs
        }
        
        # BLOCKING ACQUIRE: Wait for builder (bank-queue style)
        # Use remaining time budget as timeout, or default
        if time_budget is not None and hasattr(time_budget, 'remaining'):
            builder_timeout = max(1.0, time_budget.remaining)
        else:
            builder_timeout = 30.0
        
        wait_start = time.monotonic()
        builder = builder_pool.acquire(config=config, timeout=builder_timeout)
        if not builder:
            wait_time_ms = (time.monotonic() - wait_start) * 1000
            log.debug(f"_try_streaming_aopipeline_build: Builder pool timeout after {wait_time_ms:.0f}ms")
            bump('streaming_builder_queue_timeout')
            # Clear pre-collected data to free memory on early return
            self._last_collected_jpegs = None
            self._last_collected_missing = None
            self._last_collected_ratio = None
            return False
        
        wait_time_ms = (time.monotonic() - wait_start) * 1000
        if wait_time_ms > 10:
            bump('streaming_builder_queue_wait_count')
        
        # Keep references alive for zero-copy mode (cleared after finalize)
        jpeg_refs_for_nocopy = []
        
        try:
            # Wait for any pending consolidation OUTSIDE lock
            self._wait_for_pending_consolidation_if_needed()
            
            # Ensure chunks are created for target zoom
            self._create_chunks(self.max_zoom)
            chunks = self.chunks.get(self.max_zoom, [])
            
            if not chunks:
                log.debug(f"_try_streaming_aopipeline_build: No chunks for {self.id}")
                # Clear pre-collected data to free memory on early return
                self._last_collected_jpegs = None
                self._last_collected_missing = None
                self._last_collected_ratio = None
                return False
            
            # Streaming builder now uses time_budget for download waits (not deprecated max_download_wait)
            # Default to 5s if no budget provided
            if time_budget and not time_budget.exhausted:
                max_wait = time_budget.remaining
            else:
                max_wait = 5.0
            
            if use_precollected:
                # Use pre-collected data from batch attempt
                log.debug(f"_try_streaming_aopipeline_build: Reusing {len(pre_collected) - len(pre_collected_missing)} "
                          f"chunks from batch collection (missing: {len(pre_collected_missing)})")
                
                # ZERO-COPY: Batch add all ready chunks using nocopy mode
                # C stores pointers directly, we keep references in jpeg_refs_for_nocopy
                ready_chunks = [(i, data) for i, data in enumerate(pre_collected) if data is not None]
                if ready_chunks:
                    builder.add_chunks_batch_nocopy(ready_chunks, jpeg_refs_for_nocopy)
                
                # Use pre-computed missing indices
                pending_indices = list(pre_collected_missing)
                
                # Clear the cached data (used once)
                self._last_collected_jpegs = None
                self._last_collected_missing = None
                self._last_collected_ratio = None
            else:
                # No pre-collected data - collect from scratch
                pending_indices = []
                ready_chunks = []
                for i, chunk in enumerate(chunks):
                    if chunk.ready.is_set() and chunk.data:
                        ready_chunks.append((i, chunk.data))
                    else:
                        # Queue for download if not already
                        if not chunk.in_queue and not chunk.in_flight:
                            chunk.priority = 0  # High priority for live
                            chunk_getter.submit(chunk)
                        pending_indices.append(i)
                
                # ZERO-COPY: Batch add all ready chunks using nocopy mode
                # C stores pointers directly, we keep references in jpeg_refs_for_nocopy
                # CRITICAL: jpeg_refs_for_nocopy keeps bytes objects alive for zero-copy mode.
                # Do NOT clear this list until after finalize() returns.
                if ready_chunks:
                    builder.add_chunks_batch_nocopy(ready_chunks, jpeg_refs_for_nocopy)
            
            # Phase 2: Wait for pending downloads, then collect results
            # Single wait with reasonable timeout - much more efficient than iterating
            if pending_indices:
                # Wait for all pending chunks with a single timeout
                wait_deadline = time.monotonic() + max_wait
                for i in pending_indices:
                    remaining_wait = max(0.01, wait_deadline - time.monotonic())
                    chunks[i].ready.wait(timeout=remaining_wait)
                    if time_budget and time_budget.exhausted:
                        break  # Budget exhausted, stop waiting
            
            # Phase 3: Process all pending chunks - batch add successful, collect failures
            newly_ready = []
            failed_indices = []
            for i in pending_indices:
                chunk = chunks[i]
                if chunk.ready.is_set() and chunk.data:
                    newly_ready.append((i, chunk.data))
                else:
                    failed_indices.append(i)
            
            # ZERO-COPY: Batch add newly-ready chunks using nocopy mode
            # References kept alive in jpeg_refs_for_nocopy until finalize completes
            if newly_ready:
                builder.add_chunks_batch_nocopy(newly_ready, jpeg_refs_for_nocopy)
            
            # Phase 4: Resolve fallbacks for failed chunks
            # Use ThreadPoolExecutor for parallel disk I/O and image operations
            if failed_indices and not (time_budget and time_budget.exhausted):
                from concurrent.futures import ThreadPoolExecutor, as_completed
                
                def resolve_fallback(idx):
                    """Resolve fallback for a single chunk - can run in parallel."""
                    chunk_col = self.col + (idx % self.chunks_per_row)
                    chunk_row = self.row + (idx // self.chunks_per_row)
                    
                    # Create fallback time budget if main budget provided
                    fb_budget = None
                    if time_budget:
                        fb_remaining = time_budget.remaining
                        if fb_remaining > 0:
                            fb_budget = FBTimeBudget(fb_remaining)
                    
                    rgba = resolver.resolve(
                        chunk_col, chunk_row, self.max_zoom,
                        target_mipmap=0,
                        time_budget=fb_budget
                    )
                    return idx, rgba
                
                # Limit parallelism to avoid overwhelming disk I/O
                max_workers = min(8, len(failed_indices))
                fallback_results = []
                
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {executor.submit(resolve_fallback, i): i for i in failed_indices}
                        
                        # Collect results with timeout
                        deadline = time.monotonic() + max(1.0, max_wait)
                        for future in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
                            try:
                                idx, rgba = future.result(timeout=0.1)
                                fallback_results.append((idx, rgba))
                            except Exception:
                                # Fallback failed, will be marked missing
                                idx = futures[future]
                                fallback_results.append((idx, None))
                            
                            if time_budget and time_budget.exhausted:
                                break
                except Exception:
                    # Timeout or other error - mark remaining as missing
                    pass
                
                # Apply fallback results to builder
                resolved_indices = set()
                for idx, rgba in fallback_results:
                    resolved_indices.add(idx)
                    if rgba:
                        builder.add_fallback_image(idx, rgba)
                    else:
                        builder.mark_missing(idx)
                
                # Mark unresolved chunks as missing
                for i in failed_indices:
                    if i not in resolved_indices:
                        builder.mark_missing(i)
            elif failed_indices:
                # Budget exhausted - mark all failed as missing
                for i in failed_indices:
                    builder.mark_missing(i)
            
            # Finalize: acquire DDS buffer and build
            pool = _get_dds_buffer_pool()
            if pool is None:
                log.debug(f"_try_streaming_aopipeline_build: DDS buffer pool not available")
                return False
            
            # BLOCKING ACQUIRE: Wait for buffer (bank-queue style)
            # Live streaming builds use PRIORITY_LIVE (front of queue)
            wait_start = time.monotonic()
            try:
                # Use remaining time budget as timeout
                if time_budget is not None and hasattr(time_budget, 'remaining'):
                    acquire_timeout = max(1.0, time_budget.remaining)
                else:
                    acquire_timeout = 30.0
                
                buffer, buffer_id = pool.acquire(timeout=acquire_timeout, priority=PRIORITY_LIVE)
                
                wait_time_ms = (time.monotonic() - wait_start) * 1000
                if wait_time_ms > 10:
                    bump('streaming_queue_wait_count')
            
            except TimeoutError:
                log.debug(f"_try_streaming_aopipeline_build: Queue timeout for {self.id}")
                bump('streaming_queue_timeout')
                return False
            
            try:
                result = builder.finalize(buffer)
                if result.success and result.bytes_written >= 128:
                    dds_bytes = bytes(buffer[:result.bytes_written])
                    if self._populate_dds_from_prebuilt(dds_bytes):
                        build_time = (time.monotonic() - build_start) * 1000
                        status = builder.get_status()
                        log.debug(f"_try_streaming_aopipeline_build: SUCCESS for {self.id} - "
                                  f"{result.bytes_written} bytes in {build_time:.0f}ms "
                                  f"(decoded={status['chunks_decoded']}, fallback={status['chunks_fallback']}, "
                                  f"missing={status['chunks_missing']})")
                        bump('streaming_builder_success')
                        
                        # Schedule bundle consolidation for ALL zoom levels
                        consolidate_tile_if_ready(self, "aopipeline")
                        return True
                
                log.debug(f"_try_streaming_aopipeline_build: Finalize failed for {self.id}")
                bump('streaming_builder_finalize_failed')
                return False
                
            finally:
                pool.release(buffer_id)
        
        except Exception as e:
            log.debug(f"_try_streaming_aopipeline_build: Exception for {self.id}: {e}")
            bump('streaming_builder_exception')
            return False
        
        finally:
            # Clear JPEG refs to release memory held for zero-copy mode
            jpeg_refs_for_nocopy.clear()
            builder.release()

    def _get_fallback_level(self) -> int:
        """
        Get numeric fallback level from config.
        
        Returns:
            0 = none (no fallbacks)
            1 = cache (disk cache + mipmap scaling)  
            2 = full (all fallbacks including network)
        """
        level_str = str(getattr(CFG.autoortho, 'fallback_level', 'cache')).lower()
        if level_str == 'none':
            return 0
        elif level_str == 'full':
            return 2
        else:  # 'cache' or default
            return 1

    def mark_live(self, time_budget=None) -> None:
        """
        Mark tile as live (X-Plane requested via FUSE).
        
        Triggers transition from prefetch to live mode:
        - Sets _is_live flag
        - Applies time budget to remaining work (for streaming builder)
        - Boosts priority of any in-flight chunk downloads
        - Signals transition event for waiting prefetch thread
        
        Args:
            time_budget: Optional TimeBudget from the request (used by streaming builder)
        
        Note: For normal request flow, the budget is passed through the call chain
        (read_dds_bytes -> get_bytes -> get_mipmap -> get_img). The _tile_time_budget
        is stored here specifically for the streaming builder which runs in a separate
        context and needs access to the request's budget.
        """
        if self._is_live:
            return  # Already live
        
        self._is_live = True
        
        # Store time budget for streaming builder (which runs in separate context)
        if time_budget is not None:
            self._tile_time_budget = time_budget
        
        # Boost priority of any in-flight chunk downloads
        chunks = self.chunks.get(self.max_zoom, [])
        for chunk in chunks:
            if hasattr(chunk, 'priority') and (hasattr(chunk, 'in_flight') or hasattr(chunk, 'in_queue')):
                if getattr(chunk, 'in_flight', False) or getattr(chunk, 'in_queue', False):
                    chunk.priority = 0  # Highest priority
        
        # Signal transition to any waiting prefetch thread
        if self._live_transition_event is not None:
            self._live_transition_event.set()
        
        log.debug(f"Tile {self.id} transitioned to LIVE mode")

    def _get_quick_zoom(self, quick_zoom=0, min_zoom=None):
        """Calculate tile parameters for the given zoom level.
        
        Args:
            quick_zoom: Target zoom level (0 means use max_zoom)
            min_zoom: Minimum allowed zoom level
            
        Returns:
            Tuple of (col, row, width, height, zoom, zoom_diff)
            
        Note: Coordinates are ALWAYS scaled based on the difference between
        tilename_zoom and the effective zoom level. This ensures chunks are
        created at the correct geographic location regardless of zoom level.
        """
        # Handle simple case: no quick zoom specified (use max_zoom)
        if not quick_zoom:
            # Still need to scale coordinates if tilename_zoom != max_zoom
            # E.g., a ZL18 tile (.ter file) building at max_zoom=17 needs coords scaled by 2
            tilename_zoom_diff = self.tilename_zoom - self.max_zoom
            if tilename_zoom_diff == 0:
                # No scaling needed - common fast path
                return (self.col, self.row, self.width, self.height, self.max_zoom, 0)
            else:
                # Scale coordinates for zoom level difference
                def scale_by_zoom_diff(value, diff):
                    if diff >= 0:
                        return value >> diff
                    else:
                        return value << (-diff)
                scaled_col = scale_by_zoom_diff(self.col, tilename_zoom_diff)
                scaled_row = scale_by_zoom_diff(self.row, tilename_zoom_diff)
                scaled_width = max(1, scale_by_zoom_diff(self.width, tilename_zoom_diff))
                scaled_height = max(1, scale_by_zoom_diff(self.height, tilename_zoom_diff))
                return (scaled_col, scaled_row, scaled_width, scaled_height, self.max_zoom, 0)
        
        quick_zoom = int(quick_zoom)
        
        # Calculate the maximum zoom difference this tile can support
        max_supported_diff = min(self.max_zoom - quick_zoom, self.max_mipmap)
        
        # Determine minimum zoom level if not provided
        if min_zoom is None:
            min_zoom = max(self.max_zoom - max_supported_diff, self.min_zoom)
        
        # Clamp quick_zoom to minimum allowed value
        effective_zoom = max(quick_zoom, min_zoom)
        
        # Calculate actual zoom difference (limited by max_mipmap)
        zoom_diff = min(self.max_zoom - effective_zoom, self.max_mipmap)

        # Calculate tilename zoom difference
        tilename_zoom_diff = self.tilename_zoom - effective_zoom
        
        # Scale coordinates and dimensions based on zoom difference
        def scale_by_zoom_diff(value, diff):
            """Scale a value by 2^diff (positive diff scales down, negative scales up)"""
            if diff >= 0:
                return value >> diff  
            else:
                return value << (-diff)  
        
        scaled_col = scale_by_zoom_diff(self.col, tilename_zoom_diff)
        scaled_row = scale_by_zoom_diff(self.row, tilename_zoom_diff)
        scaled_width = max(1, scale_by_zoom_diff(self.width, tilename_zoom_diff))
        scaled_height = max(1, scale_by_zoom_diff(self.height, tilename_zoom_diff))
        
        return (scaled_col, scaled_row, scaled_width, scaled_height, effective_zoom, zoom_diff)


    def fetch(self, quick_zoom=0, background=False):
        # Wait for any pending consolidation OUTSIDE lock
        self._wait_for_pending_consolidation_if_needed()
        
        self._create_chunks(quick_zoom)
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(quick_zoom)

        for chunk in self.chunks[zoom]:
            chunk_getter.submit(chunk)

        for chunk in self.chunks[zoom]:
            ret = chunk.ready.wait()
            if not ret:
                log.error("Failed to get chunk.")

        return True
    
    def _populate_dds_from_prebuilt(self, prebuilt_bytes: bytes) -> bool:
        """
        Populate DDS mipmap buffers from prebuilt byte buffer.
        
        This copies the prebuilt DDS data into the tile's DDS structure
        so that subsequent reads work correctly without re-checking the cache.
        
        Note: Native aopipeline generates mipmaps down to 4×4 (the minimum DDS
        block size), while Python pydds.DDS creates structures down to 1×1.
        For an 8K tile, this means native produces 12 mipmaps but Python expects 14.
        We propagate the smallest mipmap's data to trailing mipmaps to match
        what pydds.gen_mipmaps() does.
        
        Args:
            prebuilt_bytes: Complete DDS file as bytes (including 128-byte header)
            
        Returns:
            True if successful, False if DDS structure is invalid
        """
        if self.dds is None:
            return False
        
        if not prebuilt_bytes or len(prebuilt_bytes) < 128:
            return False
        
        try:
            # The prebuilt bytes include the 128-byte DDS header followed by mipmap data
            # We need to populate each mipmap's databuffer
            last_valid_mm_data = None
            last_valid_mm_idx = -1
            
            for mm in self.dds.mipmap_list:
                if mm.startpos >= len(prebuilt_bytes):
                    # Prebuilt data doesn't include this mipmap
                    # Propagate last valid mipmap data to this and all remaining mipmaps
                    # This matches pydds.gen_mipmaps() behavior for trailing mipmaps
                    if last_valid_mm_data is not None:
                        for trailing_mm in self.dds.mipmap_list[mm.idx:]:
                            trailing_mm.databuffer = BytesIO(initial_bytes=last_valid_mm_data)
                            trailing_mm.retrieved = True
                    break
                
                # Extract this mipmap's data from the prebuilt buffer
                mm_end = min(mm.endpos, len(prebuilt_bytes))
                if mm_end <= mm.startpos:
                    # No data for this mipmap - propagate last valid
                    if last_valid_mm_data is not None:
                        for trailing_mm in self.dds.mipmap_list[mm.idx:]:
                            trailing_mm.databuffer = BytesIO(initial_bytes=last_valid_mm_data)
                            trailing_mm.retrieved = True
                    break
                    
                mm_data = prebuilt_bytes[mm.startpos:mm_end]
                
                # Store in the mipmap's databuffer
                mm.databuffer = BytesIO(initial_bytes=mm_data)
                mm.retrieved = True
                
                # Track last valid mipmap data for propagation to trailing mipmaps
                last_valid_mm_data = mm_data
                last_valid_mm_idx = mm.idx
            
            log.debug(f"Populated DDS from prebuilt cache for {self} (last_mm={last_valid_mm_idx})")
            # Mark as prepopulated so bytes_read warning doesn't trigger
            self._prepopulated = True
            return True
            
        except Exception as e:
            log.warning(f"Failed to populate DDS from prebuilt: {e}")
            return False
   
    def find_mipmap_pos(self, offset):
        for m in self.dds.mipmap_list:
            if offset < m.endpos:
                return m.idx
        return self.dds.mipmap_list[-1].idx

    def get_bytes(self, offset, length, time_budget=None):
        """
        Get bytes from DDS at specified offset.
        
        Args:
            offset: Byte offset in DDS file
            length: Number of bytes to read
            time_budget: TimeBudget for this request (created fresh in read_dds_bytes)
        """
        # Guard against races where tile is being closed and DDS is cleared
        if self.dds is None:
            log.debug(f"GET_BYTES: DDS is None for {self}, likely closing; skipping")
            return True
        
        # ═══════════════════════════════════════════════════════════════════
        # PREDICTIVE DDS: Check prebuilt cache first
        # ═══════════════════════════════════════════════════════════════════
        # If we have a prebuilt DDS for this tile, populate the DDS structure
        # from it and skip all the chunk download/decode/compress work.
        if prebuilt_dds_cache is not None:
            prebuilt_bytes = prebuilt_dds_cache.get(self.id)
            if prebuilt_bytes is not None:
                if self._populate_dds_from_prebuilt(prebuilt_bytes):
                    log.debug(f"GET_BYTES: Prebuilt cache HIT for {self.id}")
                    bump('prebuilt_cache_hit')
                    return True
                else:
                    # Population failed - fall through to normal path
                    log.debug(f"GET_BYTES: Prebuilt cache hit but populate failed for {self.id}")
                    bump('prebuilt_cache_populate_fail')
            else:
                bump('prebuilt_cache_miss')
        # ═══════════════════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════════════════
        # PREFETCH-TO-LIVE TRANSITION: Check if tile is being prebuilt
        # ═══════════════════════════════════════════════════════════════════
        # If BackgroundDDSBuilder is currently processing this tile, trigger
        # transition to live mode: apply time budget and boost priorities.
        if self._active_streaming_builder is not None and not self._is_live:
            # Use the passed-in request budget for live transition
            transition_budget = time_budget
            if transition_budget is None:
                # Fallback: create a budget if caller didn't provide one
                # Default 30s is responsive for flight sims - config can override for quality
                budget_seconds = float(getattr(CFG.autoortho, 'tile_time_budget', 30.0))
                transition_budget = TimeBudget(budget_seconds)
            
            # Trigger transition (boosts priorities, applies budget)
            self.mark_live(transition_budget)
            
            # Wait briefly for prefetch to complete with boosted priority
            if self._live_transition_event is not None:
                wait_time = min(transition_budget.remaining, 2.0)
                if self._live_transition_event.wait(timeout=wait_time):
                    # Prefetch completed - check if cache was populated
                    if prebuilt_dds_cache is not None:
                        prebuilt_bytes = prebuilt_dds_cache.get(self.id)
                        if prebuilt_bytes is not None:
                            if self._populate_dds_from_prebuilt(prebuilt_bytes):
                                log.debug(f"GET_BYTES: Prebuilt cache HIT after transition for {self.id}")
                                bump('prebuilt_cache_hit_after_transition')
                                return True
        # ═══════════════════════════════════════════════════════════════════

        mipmap = self.find_mipmap_pos(offset)
        log.debug(f"Get_bytes for mipmap {mipmap} ...")
        
        # ═══════════════════════════════════════════════════════════════════
        # LIVE AOPIPELINE: Try fast full-tile build when requesting mipmap 0
        # ═══════════════════════════════════════════════════════════════════
        # When X-Plane first requests the tile (typically header then mipmap 0),
        # attempt to build the entire DDS with the optimized aopipeline instead
        # of the progressive mipmap-by-mipmap path.
        #
        # Benefits:
        # - ~5x faster when chunks are cached/prefetched (~55ms vs ~331ms)
        # - Uses buffer pool (no allocation overhead)
        # - Parallel native decode + compress (better CPU utilization)
        #
        # Conditions:
        # - Only try once per tile (_aopipeline_attempted flag)
        # - Only for mipmap 0 (highest detail, means we'll need all mipmaps)
        # - Only if mipmap 0 not already retrieved
        # - Only if request actually extends into mipmap 0 data (past header at byte 128)
        #   This prevents triggering aopipeline when X-Plane is just reading the header
        #   to determine format/dimensions before requesting lower-detail mipmaps
        # - Falls back gracefully to progressive path on any failure
        #
        # FIX: Don't trigger aopipeline when just reading header (offset=0, length<=128).
        # X-Plane often reads the header first, then only requests mipmap 4 for distant tiles.
        # The old code would trigger full mipmap 0 downloads (1024 chunks) for every tile
        # even when X-Plane only needed mipmap 4 (1 chunk).
        mipmap_0_startpos = 128  # DDS header is 128 bytes, mipmap 0 starts after
        request_reaches_mipmap_0_data = (offset + length) > mipmap_0_startpos
        
        # FIX: Only trigger aopipeline for ACTUAL mipmap 0 requests, not header reads.
        # Header reads (offset=0) that bleed past byte 128 should use partial read logic,
        # not trigger full mipmap 0 aopipeline which downloads 1024 chunks.
        # We require offset > 0 (not a header read) to trigger aopipeline.
        is_pure_mipmap_request = offset > 0
        
        # DIAGNOSTIC: Track mipmap 0 trigger attempts
        if mipmap == 0:
            bump('get_bytes_mipmap_0_request')
            log.debug(f"GET_BYTES_DIAG: mipmap=0 offset={offset} length={length} "
                     f"reaches_data={request_reaches_mipmap_0_data} "
                     f"is_pure_mipmap={is_pure_mipmap_request} "
                     f"already_attempted={self._aopipeline_attempted} "
                     f"retrieved={self.dds.mipmap_list[0].retrieved if self.dds and self.dds.mipmap_list else 'N/A'}")
        
        if (mipmap == 0 and 
            request_reaches_mipmap_0_data and
            is_pure_mipmap_request and  # Don't trigger for header reads (offset=0)
            not self._aopipeline_attempted and
            self.dds is not None and
            len(self.dds.mipmap_list) > 0 and
            not self.dds.mipmap_list[0].retrieved):
            
            # DIAGNOSTIC: Track when we actually trigger aopipeline for mipmap 0
            bump('aopipeline_mipmap_0_triggered')
            log.info(f"AOPIPELINE_TRIGGER: {self.id} offset={offset} length={length}")
            
            self._aopipeline_attempted = True  # Prevent retry loops on failure
            
            try:
                # Use the per-request budget passed from read_dds_bytes()
                # This ensures each X-Plane read() gets its own independent budget
                aopipeline_budget = time_budget
                if aopipeline_budget is None:
                    # Fallback: create budget if caller didn't provide one (internal calls)
                    # Default 30s is responsive for flight sims - config can override for quality
                    budget_seconds = float(getattr(CFG.autoortho, 'tile_time_budget', 30.0))
                    aopipeline_budget = TimeBudget(budget_seconds)
                
                # ═══════════════════════════════════════════════════════════════
                # AOPIPELINE FLOW:
                # ═══════════════════════════════════════════════════════════════
                # 1. Batch aopipeline uses the request's time_budget for chunk collection
                # 2. If threshold met: build directly (fast path)
                # 3. If threshold NOT met:
                #    - Fallbacks DISABLED: build as-is with missing_color
                #    - Fallbacks ENABLED: hand to streaming with fallback_timeout budget
                # ═══════════════════════════════════════════════════════════════
                
                # Check fallback configuration
                fallback_level = self._get_fallback_level()
                fallbacks_enabled = fallback_level > 0  # 0 = none, 1 = cache, 2 = full
                
                if fallbacks_enabled:
                    # FALLBACKS ENABLED: Try batch, then streaming for fallback resolution
                    
                    # Try batch aopipeline (uses the request's time_budget)
                    if self._try_aopipeline_build(time_budget=aopipeline_budget, force_build_partial=False):
                        log.debug(f"GET_BYTES: batch aopipeline succeeded for {self.id}")
                        return True
                    
                    # Batch didn't reach threshold - use streaming as fallback route
                    # Give streaming a NEW budget based on fallback_timeout
                    if getattr(CFG.autoortho, 'streaming_builder_enabled', True):
                        fallback_timeout = float(getattr(CFG.autoortho, 'fallback_timeout', 30.0))
                        streaming_budget = TimeBudget(fallback_timeout)
                        log.debug(f"GET_BYTES: batch threshold not met, using streaming with "
                                  f"fallback_timeout={fallback_timeout}s for {self.id}")
                        
                        if self._try_streaming_aopipeline_build(time_budget=streaming_budget):
                            log.debug(f"GET_BYTES: streaming builder succeeded for {self.id}")
                            return True
                    
                    # Both failed - continue to progressive path
                    log.debug(f"GET_BYTES: aopipeline build failed, using progressive path for {self.id}")
                    bump('live_aopipeline_fallback')
                    
                else:
                    # FALLBACKS DISABLED: Build with whatever batch collects
                    # force_build_partial=True means build even if threshold not met
                    
                    if self._try_aopipeline_build(time_budget=aopipeline_budget, force_build_partial=True):
                        log.debug(f"GET_BYTES: batch aopipeline (no fallbacks) succeeded for {self.id}")
                        return True
                    
                    # Build failed (shouldn't happen with force_build_partial, but handle it)
                    log.debug(f"GET_BYTES: batch aopipeline failed even with force_build_partial for {self.id}")
                    bump('live_aopipeline_force_failed')
                    
            except Exception as e:
                log.debug(f"GET_BYTES: aopipeline exception: {e}, using progressive path")
                bump('live_aopipeline_exception')
        # ═══════════════════════════════════════════════════════════════════
        
        if mipmap > self.max_mipmap:
            # Just get the entire mipmap
            self.get_mipmap(self.max_mipmap, time_budget=time_budget)
            return True

        # Exit if already retrieved
        if self.dds.mipmap_list[mipmap].retrieved:
            log.debug(f"We already have mipmap {mipmap} for {self}")
            return True

        mm = self.dds.mipmap_list[mipmap]
        if length >= mm.length:
            self.get_mipmap(mipmap, time_budget=time_budget)
            return True
        
        log.debug(f"Retrieving {length} bytes from mipmap {mipmap} offset {offset}")

        # how deep are we in a mipmap
        mm_offset = max(0, offset - self.dds.mipmap_list[mipmap].startpos)
        log.debug(f"MM_offset: {mm_offset}  Offset {offset}.  Startpos {self.dds.mipmap_list[mipmap]}")

        # Dynamically compute bytes-per-chunk-row for this mip level based on actual DDS dimensions
        base_width_px = max(4, int(self.dds.width) >> mipmap)
        base_height_px = max(4, int(self.dds.height) >> mipmap)
        blocksize = 8 if CFG.pydds.format == "BC1" else 16
        blocks_per_row = max(1, base_width_px // 4)
        bytes_per_row = blocks_per_row * blocksize
        # Each chunk-row is 256 px tall -> 64 blocks vertically
        bytes_per_chunk_row = bytes_per_row * 64

        # Compute start/end chunk-rows touched by the requested byte range
        startrow = mm_offset // bytes_per_chunk_row
        endrow = (mm_offset + max(0, length - 1)) // bytes_per_chunk_row

        # Clamp to valid range of chunk rows for this mipmap
        chunk_rows_in_mm = base_height_px // 256
        if chunk_rows_in_mm == 0:
            log.error(f"Chunk rows in mipmap {mipmap} is 0!  Base height: {base_height_px}  Mipmap: {mipmap}")

        if startrow >= chunk_rows_in_mm:
            startrow = chunk_rows_in_mm - 1
        if endrow >= chunk_rows_in_mm:
            endrow = chunk_rows_in_mm - 1
        if endrow < startrow:
            endrow = startrow

        # Prefetch one extra chunk-row ahead to reduce subsequent stalls
        if endrow < (chunk_rows_in_mm - 1):
            endrow = min(endrow + 1, chunk_rows_in_mm - 1)

        log.debug(f"Startrow: {startrow} Endrow: {endrow} bytes_per_chunk_row: {bytes_per_chunk_row} width_px: {base_width_px} height_px: {base_height_px}")
        
        # ═══════════════════════════════════════════════════════════════════
        # NATIVE PARTIAL BUILD ATTEMPT
        # ═══════════════════════════════════════════════════════════════════
        # For mipmap 0, try native partial build first (10-20x faster).
        # This builds only the specific rows needed rather than the full mipmap.
        # Falls back to Python path if native build fails or is unavailable.
        if mipmap == 0:
            native_dds = _get_native_dds()
            if (native_dds is not None and 
                hasattr(native_dds, 'build_partial_mipmap') and
                self._try_native_partial_mipmap_build(
                    mipmap, startrow, endrow, bytes_per_chunk_row, time_budget)):
                # Native build succeeded - data written directly to DDS buffer
                self.ready.set()
                return True
        
        # ═══════════════════════════════════════════════════════════════════
        # PYTHON FALLBACK PATH
        # ═══════════════════════════════════════════════════════════════════
        # Pass the per-request budget to get_img (each read() gets its own budget)
        new_im = self.get_img(mipmap, startrow, endrow,
                maxwait=self.get_maxwait(), time_budget=time_budget)
        if not new_im:
            log.debug("No updates, so no image generated")
            return True

        # If tile is being closed concurrently, avoid touching DDS
        if self.dds is None:
            return True
        self.ready.clear()
        #log.info(new_im.size)
        
        start_time = time.time()

        # Only attempt partial compression from mipmap start
        if offset == 0:
            #compress_len = length
            compress_len = length - 128
        else:
            compress_len = 0

        try:
            self.dds.gen_mipmaps(new_im, mipmap, mipmap, compress_len)
        finally:
            # Close image if not cached in self.imgs to free native memory immediately
            # If cached, it will be closed when evicted by _cache_image() or in close()
            if mipmap not in self.imgs:
                try:
                    new_im.close()
                except Exception:
                    pass

        # We haven't fully retrieved so unset flag; guard against DDS being cleared
        log.debug(f"UNSETTING RETRIEVED! {self}")
        try:
            if self.dds is not None and self.dds.mipmap_list:
                self.dds.mipmap_list[mipmap].retrieved = False
        except Exception:
            pass
        end_time = time.time()
        self.ready.set()

        if compress_len:
            tile_time = end_time - start_time
            partial_stats.set(mipmap, tile_time)
            # Record partial mm stats via counters for aggregation
            try:
                bump_many({
                    f"partial_mm_count:{mipmap}": 1,
                    f"partial_mm_time_total_ms:{mipmap}": int(tile_time * 1000)
                })
            except Exception:
                pass

        return True

    def read_dds_bytes(self, offset, length):
        log.debug(f"READ DDS BYTES: {offset} {length}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # PER-REQUEST TIME BUDGET
        # ═══════════════════════════════════════════════════════════════════════
        # Create a fresh TimeBudget for THIS specific X-Plane read request.
        # Each read() call is independent - even multiple reads to the same tile
        # get their own budget. This ensures:
        # - No "budget starvation" for later reads to the same tile
        # - Consistent behavior regardless of read order
        # - Each request gets its full time budget for chunk collection
        # Default 30s is responsive for flight sims - config can override for quality
        budget_seconds = float(getattr(CFG.autoortho, 'tile_time_budget', 30.0))
        request_budget = TimeBudget(budget_seconds)
        
        # Track when this tile was first requested (for stats only)
        if self.first_request_time is None:
            self.first_request_time = time.monotonic()
            log.debug(f"READ_DDS_BYTES: First request for tile")
       
        if offset > 0 and offset < self.lowest_offset:
            self.lowest_offset = offset

        mm_idx = self.find_mipmap_pos(offset)
        mipmap = self.dds.mipmap_list[mm_idx]

        if offset == 0:
            # If offset = 0, read the header (and possibly some mipmap data)
            # The aopipeline trigger in get_bytes now correctly skips header reads
            # (offset=0) so this won't trigger unnecessary mipmap 0 builds
            log.debug("READ_DDS_BYTES: Read header")
            self.get_bytes(0, length, time_budget=request_budget)
        else:
            # Dynamically scale the early-read heuristic based on actual mip-0 bytes per chunk-row
            blocksize = 8 if CFG.pydds.format == "BC1" else 16
            width_px_m0 = max(4, int(self.dds.width))
            blocks_per_row_m0 = max(1, width_px_m0 // 4)
            bytes_per_row_m0 = blocks_per_row_m0 * blocksize
            bytes_per_chunk_row_m0 = bytes_per_row_m0 * 64

            # If we're still within the first chunk-row of mipmap 0, just fetch from the start
            early_threshold = bytes_per_chunk_row_m0
            if mm_idx == 0 and offset < early_threshold:
                log.debug("READ_DDS_BYTES: Early region of mipmap 0 - fetching from start")
                self.get_bytes(0, length + offset, time_budget=request_budget)
            elif (offset + length) < mipmap.endpos:
                # Total length is within this mipmap.  Make sure we have it.
                log.debug(f"READ_DDS_BYTES: Detected middle read for mipmap {mipmap.idx}")
                if not mipmap.retrieved:
                    log.debug(f"READ_DDS_BYTES: Retrieve {mipmap.idx}")
                    self.get_mipmap(mipmap.idx, time_budget=request_budget)
            else:
                log.debug(f"READ_DDS_BYTES: Start before this mipmap {mipmap.idx}")
                # We already know we start before the end of this mipmap
                # We must extend beyond the length.
                
                # Get bytes prior to this mipmap
                self.get_bytes(offset, length, time_budget=request_budget)

                # Get the entire next mipmap
                self.get_mipmap(mm_idx + 1, time_budget=request_budget)
        
        self.bytes_read += length
        # Seek and return data
        self.dds.seek(offset)
        return self.dds.read(length)

    def write(self):
        outfile = os.path.join(self.cache_dir, f"{self.row}_{self.col}_{self.maptype}_{self.tilename_zoom}_{self.tilename_zoom}.dds")
        self.ready.clear()
        self.dds.write(outfile)
        self.ready.set()
        return outfile

    def get_header(self):
        outfile = os.path.join(self.cache_dir, f"{self.row}_{self.col}_{self.maptype}_{self.tilename_zoom}_{self.tilename_zoom}.dds")
        
        self.ready.clear()
        self.dds.write(outfile)
        self.ready.set()
        return outfile

    @locked
    def get_img(self, mipmap, startrow=0, endrow=None, maxwait=5, min_zoom=None, time_budget=None,
                fallback_level_override=None):
        #
        # Get an image for a particular mipmap
        #
        # Args:
        #   fallback_level_override: If provided, overrides the config fallback_level.
        #       Used by BackgroundDDSBuilder when predictive_dds_use_fallbacks=False
        #       to force fallback_level=0 (missing color only, no extra I/O).
        #
        
        # === TIME BUDGET INITIALIZATION ===
        # Per-request budget: Each X-Plane read() call now gets its own independent budget
        # created in read_dds_bytes(). The budget is passed through the call chain.
        # This ensures:
        # - No "budget starvation" for later reads to the same tile
        # - Consistent behavior regardless of read order
        # - Each request gets its full time budget for chunk collection
        
        if time_budget is not None:
            # Use the provided per-request budget
            log.debug(f"GET_IMG: Using provided budget (elapsed={time_budget.elapsed:.2f}s, remaining={time_budget.remaining:.2f}s)")
        else:
            # Fallback: create budget if caller didn't provide one (internal calls, tests)
            use_time_budget = getattr(CFG.autoortho, 'use_time_budget', True)
            if isinstance(use_time_budget, str):
                use_time_budget = use_time_budget.lower() in ('true', '1', 'yes', 'on')
            
            if use_time_budget:
                base_budget = float(getattr(CFG.autoortho, 'tile_time_budget', 5.0))
                # Use has_ever_connected to distinguish true startup from temporary disconnects.
                # During stutters, X-Plane may briefly stop sending UDP packets, causing
                # connected=False. We don't want longer timeouts during stutters - only
                # during the actual initial scenery load (before first connection).
                if CFG.autoortho.suspend_maxwait and not datareftracker.has_ever_connected:
                    # Startup mode: increase budget but cap to reasonable maximum
                    # 10x multiplier for initial loading to allow more tiles to complete.
                    startup_multiplier = 10.0
                    max_startup_budget = 1800.0  # 30 minute absolute cap
                    effective_budget = min(base_budget * startup_multiplier, max_startup_budget)
                    log.debug(f"GET_IMG: Startup mode - creating fallback budget {effective_budget:.1f}s "
                              f"(base={base_budget:.1f}s × {startup_multiplier})")
                else:
                    effective_budget = base_budget
                    log.debug(f"GET_IMG: Creating fallback budget {effective_budget:.1f}s (no budget passed)")
                time_budget = TimeBudget(effective_budget)
            else:
                # Legacy mode: create budget from maxwait parameter
                effective_maxwait = self.get_maxwait() if maxwait == 5 else maxwait
                time_budget = TimeBudget(effective_maxwait)
                log.debug(f"GET_IMG: Legacy mode - fallback budget {effective_maxwait:.1f}s")

        # === FALLBACK CONFIGURATION ===
        # Get fallback settings for use by the fallback chain in process_chunk().
        # 
        # NOTE: Pre-building of lower mipmaps was removed due to severe diminishing returns:
        # - Cost: Paid on 100% of new tiles (85 extra downloads + 1-3s delay)
        # - Benefit: Only helped when mipmap 0 chunks fail (<5% of cases)
        # - Alternative: Fallback 3 (Cascading Download) handles failures on-demand
        #   with much lower overhead since it only activates when needed.
        #
        # fallback_level_override allows callers (e.g., BackgroundDDSBuilder) to
        # force a specific fallback behavior, bypassing user config.
        if fallback_level_override is not None:
            fallback_level = fallback_level_override
        else:
            fallback_level = self.get_fallback_level()
        fallback_extends_budget = self.get_fallback_extends_budget()
        
        # === FALLBACK BUDGET ===
        # When fallback_extends_budget is True, we create a SEPARATE budget for fallback operations.
        # This caps the TOTAL extra time spent on fallbacks (not per-chunk like before).
        # The fallback_budget is created lazily when the main budget exhausts.
        fallback_timeout = float(getattr(CFG.autoortho, 'fallback_timeout', 30.0))
        fallback_budget = None  # Created lazily when needed

        # Get effective zoom  
        zoom = min((self.max_zoom - mipmap), self.max_zoom)
        log.debug(f"GET_IMG: Default tile zoom: {self.tilename_zoom}, Requested Mipmap: {mipmap}, Requested mipmap zoom: {zoom}")
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(zoom, min_zoom)
        log.debug(f"Will use:  Zoom: {zoom},  Zoom_diff: {zoom_diff}")        
        
        log.debug(f"GET_IMG: Final zoom {zoom} for mipmap {mipmap}, coords: {col}x{row}, size: {width}x{height}")
        
        # Do we already have this img?
        if mipmap in self.imgs:
            img_data = self.imgs[mipmap]
            # Unpack tuple format (new) or return directly (old format, backward compat)
            if isinstance(img_data, tuple):
                img = img_data[0]  # Extract just the image
                log.debug(f"GET_IMG: Found saved image: {img}")
                return img
            else:
                # Old format: just the image without metadata
                log.debug(f"GET_IMG: Found saved image (old format): {img_data}")
                return img_data

        log.debug(f"GET_IMG: MM List before { {x.idx:x.retrieved for x in self.dds.mipmap_list} }")
        if mipmap < len(self.dds.mipmap_list) and self.dds.mipmap_list[mipmap].retrieved:
            log.debug(f"GET_IMG: We already have mipmap {mipmap} for {self}")
            return

        if startrow == 0 and endrow is None:
            complete_img = True
        else:
            complete_img = False

        startchunk = 0
        endchunk = None
        # Determine start and end chunk based on the actual zoom level we're using
        # Use the width/height calculated for the capped zoom level
        chunks_per_row = width  # width already accounts for capping
        if startrow:
            startchunk = startrow * chunks_per_row
        if endrow is not None:
            endchunk = (endrow * chunks_per_row) + chunks_per_row
            
        log.debug(f"GET_IMG: Chunk indices - start: {startchunk}, end: {endchunk}, chunks_per_row: {chunks_per_row}")

        # Wait for any pending consolidation - done here INSIDE get_img but the wait
        # itself doesn't hold any additional locks beyond the tile's RLock that get_img already holds.
        # Note: get_img has @locked, so this wait is within the tile lock.
        # TODO: For further optimization, this wait could be moved to callers of get_img.
        # For now, the per-tile event-based wait is much faster than notify_all().
        
        # Create chunks for the actual zoom level we'll download from
        self._create_chunks(zoom, min_zoom)
        chunks = self.chunks[zoom][startchunk:endchunk]
        log.debug(f"Start chunk: {startchunk}  End chunk: {endchunk}  Chunklen {len(self.chunks[zoom])} for zoom {zoom}")

        log.debug(f"GET_IMG: {self} : Retrieve mipmap for ZOOM: {zoom} MIPMAP: {mipmap}")
        data_updated = False
        log.debug(f"GET_IMG: {self} submitting chunks for zoom {zoom}.")
        for chunk in chunks:
            if not chunk.ready.is_set():
                log.debug(f"GET_IMG: Submitting chunk {chunk} for zoom {zoom}")
                # INVERTED: Lower detail (higher mipmap) = lower priority number (more urgent)
                # This ensures lower-detail tiles load first, minimizing missing tiles
                base_priority = self.max_mipmap - mipmap
                
                # During initial load (before first connection), further deprioritize high-detail
                # to ensure lower mipmaps load completely first.
                # Uses has_ever_connected to avoid applying during temporary stutters.
                if CFG.autoortho.suspend_maxwait and not datareftracker.has_ever_connected:
                    # Add penalty to high-detail mipmaps during initial load
                    # Mipmap 0 gets +20, mipmap 4 gets +0
                    initial_load_penalty = (self.max_mipmap - mipmap) * 5
                    base_priority = base_priority + initial_load_penalty
                
                # Apply spatial + predictive priority system
                # This considers distance from player and movement direction
                chunk.priority = _calculate_spatial_priority(
                    chunk.row, chunk.col, chunk.zoom, base_priority
                )
                
                chunk_getter.submit(chunk)
                data_updated = True
            else:
                log.debug(f"GET_IMG: Chunk {chunk} already ready - reusing for mipmap {mipmap}")

        # We've already determined this mipmap is not marked as 'retrieved' so we should create 
        # a new image, regardless here.
        #if not data_updated:
        #    log.info("No updates to chunks.  Exit.")
        #    return False

        # Calculate image dimensions based on the actual zoom level we're using for downloads
        # This creates smaller textures that save VRAM when zoom is capped
        img_width = 256 * width
        img_height = 256 * height
        
        log.debug(f"GET_IMG: Using download dimensions {width}x{height} chunks = {img_width}x{img_height} pixels")
        
        log.debug(f"GET_IMG: Create new image: Zoom: {zoom} | {(img_width, img_height)}")
        
        # === PRE-INITIALIZE BASE LAYER FOR MIPMAP 0 ===
        # When building mipmap 0 (highest detail), instead of starting with a blank 
        # missing_color image, we initialize with an upscaled lower mipmap.
        # This ensures any chunks that are skipped due to budget exhaustion still 
        # show a blurry-but-colored texture instead of visible holes.
        #
        # Why this works:
        # - Mipmaps are built 4->3->2->1->0, so lower mipmaps are usually available
        # - Chunks overwrite the base as they complete (progressive refinement)
        # - No hole detection needed - base layer guarantees coverage
        # - Single upscale operation (efficient) vs per-hole fills
        #
        # Fallback order: mipmap 1 (best quality) -> 2 -> 3 -> 4 -> missing_color
        
        new_im = None
        prefill_source_mm = None
        
        if mipmap == 0 and fallback_level >= 1:
            # Try to find the best available lower mipmap to use as base
            for source_mm in [1, 2, 3, 4]:
                if source_mm > self.max_mipmap:
                    continue
                    
                img_data = self.imgs.get(source_mm)
                if not img_data:
                    continue
                
                # Extract image from tuple format (image, col, row, zoom)
                if isinstance(img_data, tuple):
                    source_img = img_data[0]
                else:
                    source_img = img_data
                
                if source_img is None or getattr(source_img, '_freed', False):
                    continue
                
                # Calculate scale factor: mipmap 1->0 = 2x, mipmap 2->0 = 4x, etc.
                scale_factor = 1 << source_mm  # 2^source_mm
                
                # Verify source image dimensions match expected size
                expected_source_width = img_width >> source_mm
                expected_source_height = img_height >> source_mm
                
                if source_img._width != expected_source_width or source_img._height != expected_source_height:
                    log.debug(f"GET_IMG: Prefill skipping mipmap {source_mm} - size mismatch: "
                             f"got {source_img._width}x{source_img._height}, expected {expected_source_width}x{expected_source_height}")
                    continue
                
                # Upscale to mipmap 0 size
                log.debug(f"GET_IMG: Pre-initializing mipmap 0 base from mipmap {source_mm} "
                         f"({source_img._width}x{source_img._height} -> {img_width}x{img_height}, scale={scale_factor}x)")
                
                try:
                    new_im = source_img.scale(scale_factor)
                    if new_im is not None and new_im._width == img_width and new_im._height == img_height:
                        prefill_source_mm = source_mm
                        bump(f'mipmap0_prefill_from_mm{source_mm}')
                        log.debug(f"GET_IMG: Successfully pre-initialized base from mipmap {source_mm}")
                        break
                    else:
                        # Scale failed or wrong size - clean up and try next
                        if new_im is not None:
                            try:
                                new_im.close()
                            except Exception:
                                pass
                        new_im = None
                        log.debug(f"GET_IMG: Prefill scale from mipmap {source_mm} failed or wrong size")
                except Exception as e:
                    log.warning(f"GET_IMG: Prefill scale from mipmap {source_mm} exception: {e}")
                    new_im = None
        
        # Fall back to missing_color if no lower mipmap available or prefill disabled
        if new_im is None:
            new_im = AoImage.new(
                "RGBA",
                (img_width, img_height),
                (
                    CFG.autoortho.missing_color[0],
                    CFG.autoortho.missing_color[1],
                    CFG.autoortho.missing_color[2],
                ),
            )
            if mipmap == 0 and fallback_level >= 1:
                log.debug(f"GET_IMG: No lower mipmap available for prefill, using missing_color")
        else:
            log.debug(f"GET_IMG: Using prefilled base from mipmap {prefill_source_mm}")

        log.debug(f"GET_IMG: Will use image {new_im}")

        # Check if we have any chunks to process
        if len(chunks) == 0:
            log.warning(f"GET_IMG: No chunks created for zoom {zoom}, mipmap {mipmap}")
            # Return the missing_color filled image we already created
            # This ensures consistency - X-Plane sees missing_color, not arbitrary gray
            return new_im
        
        # Track if any executor thread needs a lazy build.
        # We defer lazy builds to after the executor completes to avoid lock contention:
        # calling get_img() from within an executor thread would block on self._lock.
        needs_lazy_build = False
            
        def process_chunk(chunk, skip_download_wait=False):
            """Process a single chunk and return (chunk, chunk_img, start_x, start_y)"""
            # Calculate position using native chunk size (no scaling)
            start_x = int(chunk.width * (chunk.col - col))
            start_y = int(chunk.height * (chunk.row - row))
            
            # PHASE 2 FIX #6 & #8: Validate coordinates before use
            # Negative coordinates indicate logic error or data corruption
            if start_x < 0 or start_y < 0:
                log.error(f"GET_IMG: Invalid negative coordinates: start_x={start_x}, start_y={start_y}, chunk.col={chunk.col}, chunk.row={chunk.row}, base col={col}, base row={row}")
                # Return placeholder to prevent crash
                return (chunk, None, 0, 0)
            
            # Check if coordinates would extend beyond image bounds
            if start_x + chunk.width > img_width or start_y + chunk.height > img_height:
                log.error(f"GET_IMG: Coordinates extend beyond image: pos=({start_x},{start_y}), size=({chunk.width}x{chunk.height}), image=({img_width}x{img_height})")
                # Return placeholder to prevent crash
                return (chunk, None, 0, 0)
            
            # === TIME BUDGET CHECK ===
            # Early exit if the time budget is exhausted. This is the key improvement:
            # instead of each chunk waiting maxwait seconds, we check the shared budget.
            if time_budget.exhausted:
                log.debug(f"Time budget exhausted (elapsed={time_budget.elapsed:.2f}s), skipping chunk {chunk}")
                time_budget.record_chunk_skipped()
                bump('chunk_budget_skipped')
                return (chunk, None, start_x, start_y)
            
            # Track if this chunk permanently failed
            is_permanent_failure = chunk.permanent_failure
            if is_permanent_failure:
                log.debug(f"Chunk {chunk} is permanently failed ({chunk.failure_reason}), will attempt fallbacks")
                bump(f'chunk_permanent_fail_{chunk.failure_reason}')
            
            # Smart timeout: only wait for download, not decode
            # If skip_download_wait is True, go straight to fallbacks (for chunks that never started)
            if skip_download_wait:
                chunk_ready = False
                log.debug(f"Chunk {chunk} never started downloading, skipping wait and going to fallbacks")
            elif chunk.ready.is_set():
                # Download already complete, just needs decode
                chunk_ready = True
                log.debug(f"Chunk {chunk} already downloaded, proceeding to decode")
            else:
                # Download in progress - use BOTH time budget AND per-chunk maxwait
                # This respects the total tile budget while also limiting per-chunk waits
                # The wait ends when either: budget exhausted, maxwait reached, or event set
                chunk_ready = time_budget.wait_with_budget(chunk.ready, max_single_wait=maxwait)
                if not chunk_ready:
                    log.debug(f"Chunk {chunk} wait ended (budget remaining={time_budget.remaining:.2f}s, maxwait={maxwait:.1f}s)")

            chunk_img = None
            decode_failed = False
            
            # TOCTOU FIX: Capture local reference to chunk.data before checking/using.
            # This prevents a race condition where another thread (e.g., Chunk.close())
            # could set chunk.data = None between our check and use. Python's GIL makes
            # reference assignment atomic, so once we have a local ref, the object won't
            # be garbage collected even if chunk.data is cleared elsewhere.
            # This pattern matches save_cache() which uses the same approach.
            chunk_data = chunk.data
            
            if chunk_ready and chunk_data:
                # We returned and have data!
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Ready and found chunk data.")
                try:
                    with _decode_sem:
                        chunk_img = AoImage.load_from_memory(chunk_data)
                        if chunk_img is None:
                            log.warning(f"GET_IMG: load_from_memory returned None for {chunk}")
                            decode_failed = True
                except Exception as _e:
                    log.error(f"GET_IMG: load_from_memory exception for {chunk}: {_e}")
                    chunk_img = None
                    decode_failed = True
            elif chunk_ready and not chunk_data:
                # Download "succeeded" but returned empty data
                log.debug(f"GET_IMG: Chunk {chunk} ready but has no data")
                decode_failed = True
            
            # Determine if we need fallbacks:
            # - chunk didn't download in time (not chunk_ready)
            # - chunk is permanently failed (404, etc)
            # - chunk downloaded but decode failed (decode_failed)
            needs_fallback = not chunk_ready or is_permanent_failure or decode_failed
            
            # FALLBACK CHAIN (in order of preference):
            # Each fallback only runs if previous ones failed and fallback_level allows it.
            # For permanent failures (404, etc), we ALWAYS try fallbacks to get lower-zoom alternatives.
            #
            # fallback_level controls which fallbacks are enabled:
            #   0 = None: Skip all fallbacks (fastest, may have missing tiles)
            #   1 = Cache-only: Fallback 1 (disk cache) + Fallback 2 (built mipmaps)
            #   2 = Full: All fallbacks including Fallback 3 (network downloads)
            
            # Fallback 1: Search disk cache for lower-zoom JPEGs (enabled if fallback_level >= 1)
            if not chunk_img and needs_fallback and fallback_level >= 1:
                log.debug(f"GET_IMG(process_chunk): Fallback 1 - searching disk cache for backup chunk.")
                chunk_img = self.get_best_chunk(chunk.col, chunk.row, mipmap, zoom)
                # get_best_chunk bumps 'upscaled_from_jpeg_count' if successful
            
            # === LAZY BUILD TRIGGER ===
            # NOTE: Lazy build is now deferred to AFTER the executor completes.
            # Calling get_img() from within an executor thread would cause lock contention:
            # the executor thread would block waiting for self._lock (held by the main thread),
            # while the main thread waits for executor futures - causing a 300s stall.
            # Instead, we mark that lazy build is needed and handle it after the main loop.
            # The nonlocal variable allows the outer scope to detect this need.
            if not chunk_img and mipmap == 0 and fallback_level >= 1:
                if not self._lazy_build_attempted:
                    log.debug(f"GET_IMG(process_chunk): Marking lazy build as needed (will run after executor)")
                    nonlocal needs_lazy_build
                    needs_lazy_build = True
            
            # Fallback 2: Scale from already-built mipmaps (enabled if fallback_level >= 1)
            # Now may have something to use thanks to lazy build above
            if not chunk_img and fallback_level >= 1:
                log.debug(f"GET_IMG(process_chunk): Fallback 2 - scaling from built mipmaps.")
                chunk_img = self.get_downscaled_from_higher_mipmap(mipmap, chunk.col, chunk.row, zoom)
                # Note: scaling function bumps its own counters (upscaled_chunk_count or downscaled_chunk_count)
            
            # Fallback 3: On-demand download of lower-detail chunks (enabled if fallback_level >= 2)
            # This is the expensive network fallback - only use when quality is prioritized
            if not chunk_img and needs_fallback and fallback_level >= 2:
                # Budget strategy for cascading fallback:
                # - Use main budget first (remaining time)
                # - If fallback_extends_budget is True AND main budget exhausts during fallback,
                #   switch to the extra fallback_budget
                # - This ensures max total time is exactly: main_budget + fallback_timeout
                nonlocal fallback_budget
                
                # Fallback budget is created lazily ONLY when main budget exhausts
                # This ensures it truly extends the time rather than running in parallel
                
                if time_budget.exhausted and not fallback_extends_budget:
                    # Main budget exhausted and no extension allowed - skip
                    log.debug(f"GET_IMG(process_chunk): Skipping Fallback 3 - budget exhausted and fallback_extends_budget=False")
                elif time_budget.exhausted and fallback_extends_budget:
                    # Main budget exhausted - create/use fallback budget
                    # Created NOW so the timer starts when main exhausts (true extension)
                    if fallback_budget is None:
                        fallback_budget = TimeBudget(fallback_timeout)
                        log.info(f"GET_IMG: Main budget exhausted, creating fallback budget {fallback_timeout:.1f}s")
                    
                    if fallback_budget.exhausted:
                        log.debug(f"GET_IMG(process_chunk): Skipping Fallback 3 - both budgets exhausted")
                    else:
                        log.debug(f"GET_IMG(process_chunk): Fallback 3 - main exhausted, using fallback budget "
                                 f"(remaining={fallback_budget.remaining:.2f}s)")
                        chunk_img = self.get_or_build_lower_mipmap_chunk(
                            mipmap, chunk.col, chunk.row, zoom,
                            main_budget=None,  # Main already exhausted
                            fallback_budget=fallback_budget
                        )
                else:
                    # Main budget still has time - use it first, may switch to fallback if main exhausts
                    log.debug(f"GET_IMG(process_chunk): Fallback 3 - using main budget "
                             f"(remaining={time_budget.remaining:.2f}s)" +
                             (f", fallback budget ready (remaining={fallback_budget.remaining:.2f}s)" 
                              if fallback_budget else ""))
                    chunk_img = self.get_or_build_lower_mipmap_chunk(
                        mipmap, chunk.col, chunk.row, zoom,
                        main_budget=time_budget,
                        fallback_budget=fallback_budget,  # May be None, will be created lazily
                        fallback_timeout=fallback_timeout if fallback_extends_budget else None
                    )

            if not chunk_ready and not chunk_img and not is_permanent_failure:
                # === FINAL RETRY (OPTIMIZED) ===
                # Only worth retrying if the chunk download is still pending.
                # Pending means: in queue, in flight, or already completed (ready).
                # If none of these are true, the download failed and won't be retried
                # by the worker - so waiting would be pointless.
                download_pending = chunk.in_flight or chunk.in_queue or chunk.ready.is_set()
                
                if time_budget.exhausted:
                    log.debug(f"GET_IMG: Skipping final retry for {chunk} - budget exhausted")
                    time_budget.record_chunk_skipped()
                    bump('chunk_budget_skipped')
                elif not download_pending:
                    # Download failed completely and won't be retried - skip waiting
                    log.debug(f"GET_IMG: Skipping retry for {chunk} - download not pending "
                             f"(in_flight={chunk.in_flight}, in_queue={chunk.in_queue})")
                    bump('chunk_retry_skipped_not_pending')
                else:
                    # Download still in progress or just completed - worth checking
                    log.debug(f"GET_IMG: Final retry for {chunk} "
                             f"(in_flight={chunk.in_flight}, in_queue={chunk.in_queue}, "
                             f"budget remaining={time_budget.remaining:.2f}s)")
                    bump('retry_chunk_count')
                    
                    if chunk.ready.is_set():
                        # Completed during fallback attempts
                        chunk_ready = True
                        log.debug(f"Chunk {chunk} completed during fallbacks, proceeding to decode")
                    else:
                        # Still in progress - wait with reduced timeout (1s max for retry)
                        chunk_ready = time_budget.wait_with_budget(chunk.ready, max_single_wait=1.0)
                
                # TOCTOU FIX: Capture local reference for retry path (same pattern as above)
                retry_chunk_data = chunk.data
                if chunk_ready and retry_chunk_data:
                    log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Final retry for {chunk}, SUCCESS!")
                    try:
                        with _decode_sem:
                            chunk_img = AoImage.load_from_memory(retry_chunk_data)
                            if chunk_img is None:
                                log.warning(f"GET_IMG: load_from_memory returned None on retry for {chunk}")
                    except Exception as _e:
                        log.error(f"GET_IMG: load_from_memory exception on retry for {chunk}: {_e}")
                        chunk_img = None

            if not chunk_img:
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Empty chunk data.  Skip.")
                bump('chunk_missing_count')
                if is_permanent_failure:
                    log.info(f"Chunk {chunk} permanently failed and all fallbacks exhausted")
            else:
                # Successfully processed this chunk within budget
                time_budget.record_chunk_processed()
                
            return (chunk, chunk_img, start_x, start_y)
        
        # OPTIMIZATION: Non-blocking chunk processing with bundle pre-check
        # 
        # Architecture:
        # 1. EARLY BUNDLE CHECK: Try to get all chunks from bundle first (fastest path)
        # 2. IMMEDIATE READY PROCESSING: Process ready chunks without waiting
        # 3. EXECUTOR FOR WAITING: Use executor only for chunks that need download wait
        # 4. ROUND-ROBIN RETRY: Poll non-ready chunks periodically instead of blocking all workers
        #
        # This prevents worker starvation where all 8 executor threads block on chunk.ready.wait()
        # when downloads are slow, leaving no workers to process chunks that become ready.
        
        max_pool_workers = min(CURRENT_CPU_COUNT, len(chunks), _MAX_DECODE)
        
        total_chunks = len(chunks)
        completed = 0
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_pool_workers)
        active_futures = {}
        chunks_with_images = set()  # Track which chunks have images for fallback sweep
        
        try:
            # === PHASE 0: EARLY BUNDLE CHECK ===
            # If bundle exists with this zoom level, populate chunk data immediately
            # This avoids submitting chunks to executor when data is already available
            bundle_preload_count = 0
            bundle_chunks = self._try_get_chunks_from_bundle(zoom)
            
            if bundle_chunks:
                # Calculate tile origin for this zoom level
                zoom_diff = self.max_zoom - zoom
                tile_col_at_zoom = self.col >> zoom_diff
                tile_row_at_zoom = self.row >> zoom_diff
                chunks_per_side = int(round((2 ** self.max_zoom) / (2 ** zoom)))
                
                for chunk in chunks:
                    if chunk.ready.is_set():
                        continue  # Already ready
                    
                    # Calculate chunk's index in the bundle
                    chunk_col_offset = chunk.col - tile_col_at_zoom
                    chunk_row_offset = chunk.row - tile_row_at_zoom
                    
                    if 0 <= chunk_col_offset < chunks_per_side and 0 <= chunk_row_offset < chunks_per_side:
                        bundle_idx = chunk_row_offset * chunks_per_side + chunk_col_offset
                        
                        if bundle_idx < len(bundle_chunks) and bundle_chunks[bundle_idx] is not None:
                            # Preload chunk data from bundle
                            chunk.set_cached_data(bundle_chunks[bundle_idx])
                            bundle_preload_count += 1
                
                if bundle_preload_count > 0:
                    log.debug(f"GET_IMG: Bundle preload populated {bundle_preload_count}/{len(chunks)} chunks")
                    bump('bundle_preload_chunks', bundle_preload_count)
            
            # === PHASE 1: IMMEDIATE READY PROCESSING ===
            # Process chunks that are already ready without submitting to executor
            # This is faster than executor overhead for chunks that don't need waiting
            ready_chunks = []
            pending_chunks = []
            
            for chunk in chunks:
                if chunk.permanent_failure:
                    bump('chunk_missing_count')
                    continue
                
                if chunk.ready.is_set() and chunk.data:
                    # Ready with data - process immediately in current thread
                    ready_chunks.append(chunk)
                else:
                    # Not ready or no data - needs waiting
                    pending_chunks.append(chunk)
            
            # Process ready chunks in parallel via executor (no waiting needed)
            if ready_chunks:
                log.debug(f"GET_IMG: Processing {len(ready_chunks)} ready chunks immediately")
                for chunk in ready_chunks:
                    # Use skip_download_wait=True since chunk is already ready
                    future = executor.submit(process_chunk, chunk, skip_download_wait=False)
                    active_futures[future] = chunk
            
            # === PHASE 2: SUBMIT PENDING CHUNKS ===
            # Submit chunks that need download waiting
            for chunk in pending_chunks:
                future = executor.submit(process_chunk, chunk)
                active_futures[future] = chunk
            
            log.debug(f"GET_IMG: Submitted {len(ready_chunks)} ready + {len(pending_chunks)} pending = {len(active_futures)} chunks to executor")
            
            # Collect results as they complete, respecting time budget
            while active_futures:
                # === TIME BUDGET CHECK ===
                if time_budget.exhausted:
                    remaining = len(active_futures)
                    if remaining > 0:
                        bump('chunk_budget_exhausted', remaining)
                    log.info(f"Time budget exhausted after {time_budget.elapsed:.2f}s for mipmap {mipmap}: "
                            f"processed {time_budget.chunks_processed}, skipped {time_budget.chunks_skipped}, "
                            f"remaining {remaining}/{total_chunks}")
                    break
                
                # Wait for completions with a short timeout to allow budget checks
                done, pending = concurrent.futures.wait(
                    active_futures.keys(), 
                    timeout=0.05,  # Check budget every 50ms
                    return_when=concurrent.futures.FIRST_COMPLETED
                )
                
                for future in done:
                    try:
                        chunk, chunk_img, start_x, start_y = future.result()
                        completed += 1
                        if chunk_img:
                            _safe_paste(new_im, chunk_img, start_x, start_y)
                            chunks_with_images.add(id(chunk))  # Track for fallback sweep
                    except Exception as exc:
                        log.error(f"Chunk processing failed: {exc}")
                    finally:
                        del active_futures[future]
                    # Progress logging
                    if total_chunks and (completed % max(1, total_chunks // 4) == 0):
                        log.debug(f"GET_IMG progress: {completed}/{total_chunks} chunks for mip {mipmap}")
            
            # Process any remaining active futures (when budget wasn't exhausted)
            # Budget exhaustion already logged above, so just drain remaining futures
            remaining_futures = list(active_futures.keys())
            if remaining_futures:
                # Use short timeout to avoid blocking forever
                timeout = max(0.5, time_budget.remaining) if not time_budget.exhausted else 0.5
                try:
                    for future in concurrent.futures.as_completed(remaining_futures, timeout=timeout):
                        try:
                            chunk, chunk_img, start_x, start_y = future.result()
                            if chunk_img:
                                _safe_paste(new_im, chunk_img, start_x, start_y)
                                chunks_with_images.add(id(active_futures.get(future)))
                        except Exception as exc:
                            log.error(f"Chunk processing failed: {exc}")
                        finally:
                            if future in active_futures:
                                del active_futures[future]
                except TimeoutError:
                    unfinished = len([f for f in remaining_futures if not f.done()])
                    log.debug(f"Timeout waiting for {unfinished} remaining chunks")
                    # Cancel remaining futures to free resources
                    for future in remaining_futures:
                        if not future.done():
                            future.cancel()
                    # Clear remaining references
                    active_futures.clear()
            
            # === FALLBACK SWEEP PHASE ===
            # After main budget expires, use fallback budget to fill in missing chunks
            # This maximizes image quality by systematically processing all gaps
            if fallback_extends_budget and time_budget.exhausted and fallback_level >= 2:
                # Identify chunks without images
                missing_chunks = [c for c in chunks if id(c) not in chunks_with_images 
                                  and not c.permanent_failure]
                
                if missing_chunks:
                    # Create fallback budget if not already created
                    if fallback_budget is None:
                        fallback_budget = TimeBudget(fallback_timeout)
                        log.info(f"GET_IMG: Starting fallback sweep for {len(missing_chunks)} missing chunks "
                                f"(budget={fallback_timeout:.1f}s)")
                    elif not fallback_budget.exhausted:
                        log.info(f"GET_IMG: Fallback sweep for {len(missing_chunks)} missing chunks "
                                f"(remaining={fallback_budget.remaining:.2f}s)")
                    
                    # === OPTIMIZATION: Try bundle-based bulk read FIRST ===
                    # This is MUCH faster than sequential chunk processing because:
                    # - Single file open instead of 256+ opens
                    # - All data read in one pass
                    # - Parallel JPEG decoding via OpenMP
                    bundle_recovery_attempted = False
                    bundle_jpegs = self._try_get_chunks_from_bundle(zoom)
                    
                    if bundle_jpegs:
                        bundle_recovery_attempted = True
                        bundle_recovered = 0
                        
                        # Build a mapping from (col, row) to bundle index
                        # Bundle stores chunks in row-major order starting from tile origin
                        # Calculate the tile origin for this zoom level
                        zoom_diff = self.max_zoom - zoom
                        tile_col_at_zoom = self.col >> zoom_diff
                        tile_row_at_zoom = self.row >> zoom_diff
                        chunks_per_side = int(round((2 ** self.max_zoom) / (2 ** zoom)))
                        
                        for chunk in missing_chunks:
                            # Calculate chunk's index in the bundle
                            chunk_col_offset = chunk.col - tile_col_at_zoom
                            chunk_row_offset = chunk.row - tile_row_at_zoom
                            
                            # Validate offsets are within bounds
                            if 0 <= chunk_col_offset < chunks_per_side and 0 <= chunk_row_offset < chunks_per_side:
                                bundle_idx = chunk_row_offset * chunks_per_side + chunk_col_offset
                                
                                if bundle_idx < len(bundle_jpegs) and bundle_jpegs[bundle_idx] is not None:
                                    jpeg_data = bundle_jpegs[bundle_idx]
                                    
                                    # Decode JPEG and paste into image
                                    try:
                                        with _decode_sem:
                                            chunk_img = AoImage.load_from_memory(jpeg_data)
                                        
                                        if chunk_img:
                                            start_x = int(chunk.width * (chunk.col - col))
                                            start_y = int(chunk.height * (chunk.row - row))
                                            _safe_paste(new_im, chunk_img, start_x, start_y)
                                            chunks_with_images.add(id(chunk))
                                            bundle_recovered += 1
                                            
                                            # Also update chunk state for future use
                                            chunk.set_cached_data(jpeg_data)
                                    except Exception as e:
                                        log.debug(f"Bundle chunk decode failed: {e}")
                        
                        if bundle_recovered > 0:
                            log.info(f"GET_IMG: Bundle sweep recovered {bundle_recovered}/{len(missing_chunks)} chunks")
                            bump('fallback_bundle_sweep_recovered', bundle_recovered)
                            
                            # Update missing chunks list for sequential fallback
                            missing_chunks = [c for c in missing_chunks if id(c) not in chunks_with_images]
                    
                    # === SEQUENTIAL FALLBACK: Process remaining missing chunks ===
                    # Only runs if bundle didn't recover all chunks
                    sweep_recovered = 0
                    for chunk in missing_chunks:
                        if fallback_budget.exhausted:
                            log.debug(f"Fallback sweep: budget exhausted after recovering {sweep_recovered} chunks")
                            break
                        
                        # Calculate position for this chunk
                        start_x = int(chunk.width * (chunk.col - col))
                        start_y = int(chunk.height * (chunk.row - row))
                        
                        # Try cascading fallback for this chunk
                        chunk_img = self.get_or_build_lower_mipmap_chunk(
                            mipmap, chunk.col, chunk.row, zoom,
                            main_budget=None,  # Main already exhausted
                            fallback_budget=fallback_budget
                        )
                        
                        if chunk_img:
                            _safe_paste(new_im, chunk_img, start_x, start_y)
                            chunks_with_images.add(id(chunk))
                            sweep_recovered += 1
                            bump('fallback_sweep_recovered')
                    
                    if sweep_recovered > 0:
                        log.info(f"GET_IMG: Fallback sweep recovered {sweep_recovered}/{len(missing_chunks)} chunks")
                    
                    # Update missing count for chunks we couldn't recover
                    still_missing = len([c for c in chunks if id(c) not in chunks_with_images 
                                        and not c.permanent_failure])
                    if still_missing > 0:
                        bump('chunk_missing_count', still_missing)
        finally:
            # Use wait=False to avoid blocking on cancelled/timed-out futures
            # The futures will complete in the background but we won't wait for them
            # Since process_chunk checks time_budget.exhausted, they should exit quickly
            executor.shutdown(wait=False, cancel_futures=True)
        
        # === DEFERRED LAZY BUILD ===
        # If any executor thread signaled that lazy build is needed, do it now.
        # This runs in the main thread which already holds self._lock, avoiding the
        # lock contention that caused 300s stalls when calling get_img() from executor threads.
        if needs_lazy_build and mipmap == 0 and not self._lazy_build_attempted:
            log.debug(f"GET_IMG: Running deferred lazy build (main thread, lock held)")
            self._try_lazy_build_fallback_mipmap(time_budget)
            
            # After lazy build, re-process any chunks that still need images
            # The lazy build created lower-detail mipmaps that Fallback 2 can use
            missing_after_lazy = [c for c in chunks if id(c) not in chunks_with_images 
                                  and not c.permanent_failure]
            if missing_after_lazy and len(self.imgs) > 0:
                log.debug(f"GET_IMG: Re-processing {len(missing_after_lazy)} chunks after lazy build")
                for chunk in missing_after_lazy:
                    if time_budget.exhausted:
                        break
                    # Try Fallback 2 now that we have built lower mipmaps
                    chunk_img = self.get_downscaled_from_higher_mipmap(mipmap, chunk.col, chunk.row, zoom)
                    if chunk_img:
                        start_x = int(chunk.width * (chunk.col - col))
                        start_y = int(chunk.height * (chunk.row - row))
                        _safe_paste(new_im, chunk_img, start_x, start_y)
                        chunks_with_images.add(id(chunk))
                        log.debug(f"GET_IMG: Recovered chunk via deferred lazy build fallback")

        # Determine if we need to cache this image for fallback/upscaling
        should_cache = complete_img and mipmap <= self.max_mipmap
        
        if should_cache:
            log.debug(f"GET_IMG: Save complete image for later...")
            # Store image with metadata (col, row, zoom) for coordinate mapping in upscaling
            # Use _cache_image for LRU eviction to prevent unbounded memory growth
            self._cache_image(mipmap, (new_im, col, row, zoom))

        # Log budget summary including fallback budget if used
        if fallback_budget is not None:
            log.info(f"GET_IMG: DONE! Main budget: {time_budget.elapsed:.2f}s, "
                    f"Fallback budget: {fallback_budget.elapsed:.2f}s/{fallback_timeout:.1f}s "
                    f"(exhausted={fallback_budget.exhausted})")
        else:
            log.debug(f"GET_IMG: DONE!  IMG created {new_im}")

        # OPTIMIZATION: In-place desaturation when image isn't cached
        # Only copy when image was saved to cache AND needs desaturation
        if seasons_enabled:
            saturation = 0.01 * season_saturation_locked(self.row, self.col, self.tilename_zoom)
            if saturation < 1.0:    # desaturation is expensive
                if should_cache:
                    # Must copy because original is cached for fallback use
                    new_im = new_im.copy().desaturate(saturation)
                else:
                    # In-place desaturation - assign result for error handling
                    new_im = new_im.desaturate(saturation)
        
        # Return image along with mipmap and zoom level this was created at
        return new_im

    def _try_lazy_build_fallback_mipmap(self, time_budget=None):
        """
        Lazy build a lower-detail mipmap when the first chunk failure occurs.
        
        This is triggered on-demand (not proactively like pre-building) to provide
        Fallback 2 support. When a mipmap 0 chunk fails, we quickly build mipmap 2
        (which has only 16 chunks at ZL14 vs 256 at ZL16), enabling subsequent
        failing chunks to upscale from the built image.
        
        Key differences from pre-building:
        - Only runs when a failure actually occurs (not on every tile)
        - Only runs once per tile (tracked by _lazy_build_attempted)
        - Uses reduced time budget to avoid blocking
        - Builds mipmap 2 (faster than mipmap 1, still useful for upscaling)
        
        Returns:
            True if a mipmap was successfully built, False otherwise
        """
        # Only attempt once per tile
        if self._lazy_build_attempted:
            return False
        
        self._lazy_build_attempted = True
        
        # Choose which mipmap to build:
        # - Mipmap 2 (ZL14): 16 chunks, fast to build, 4x upscale to mipmap 0
        # - Mipmap 3 (ZL13): 4 chunks, very fast, 8x upscale (lower quality)
        # We use mipmap 2 as a balance between speed and quality
        target_mipmap = min(2, self.max_mipmap)
        
        # Skip if target_mipmap is 0 - we're already building mipmap 0,
        # so building it again as a "fallback" is redundant
        if target_mipmap == 0:
            log.debug("Lazy build: max_mipmap is 0, no lower-detail mipmap available")
            return False
        
        # Skip if we somehow already have this mipmap
        if target_mipmap in self.imgs:
            log.debug(f"Lazy build: mipmap {target_mipmap} already exists")
            return True
        
        # Calculate available budget for lazy build
        # Use at most 1.5 seconds or 30% of remaining budget
        if time_budget and not time_budget.exhausted:
            max_lazy_budget = min(1.5, time_budget.remaining * 0.3)
            if max_lazy_budget < 0.3:
                log.debug(f"Lazy build: insufficient budget ({max_lazy_budget:.2f}s), skipping")
                return False
            lazy_budget = TimeBudget(max_lazy_budget)
        else:
            # No budget or exhausted - use a small fixed budget
            lazy_budget = TimeBudget(1.0)
        
        log.debug(f"Lazy build: triggering build of mipmap {target_mipmap} "
                 f"(budget={lazy_budget.max_seconds:.2f}s)")
        bump('lazy_build_triggered')
        
        try:
            # Build the lower mipmap
            # This will populate self.imgs[target_mipmap] for Fallback 2 to use
            result = self.get_img(
                target_mipmap, 
                startrow=0, 
                endrow=None, 
                maxwait=1.0,
                time_budget=lazy_budget
            )
            
            if result and target_mipmap in self.imgs:
                log.debug(f"Lazy build: successfully built mipmap {target_mipmap}")
                bump('lazy_build_success')
                return True
            else:
                log.debug(f"Lazy build: mipmap {target_mipmap} build returned but not in imgs")
                return False
                
        except Exception as e:
            log.debug(f"Lazy build: failed to build mipmap {target_mipmap}: {e}")
            bump('lazy_build_failed')
            return False

    def _get_shared_fallback_chunk(self, col, row, zoom):
        """
        Get or create a fallback chunk from the shared pool.
        
        This enables chunk sharing: when multiple high-zoom chunks fail and need
        the same parent chunk, they share a single download instead of each
        downloading it independently.
        
        Thread-safe: Uses a lock to prevent race conditions.
        
        Args:
            col, row, zoom: Coordinates of the fallback chunk to get/create
            
        Returns:
            Chunk object (may already be ready, in-flight, or newly created)
        """
        key = (col, row, zoom)
        
        with self._fallback_pool_lock:
            if key in self._fallback_chunk_pool:
                chunk = self._fallback_chunk_pool[key]
                log.debug(f"Reusing shared fallback chunk {chunk} from pool")
                bump('fallback_chunk_pool_hit')
                return chunk
            
            # Evict oldest chunks if at limit to prevent unbounded memory growth
            while len(self._fallback_chunk_pool) >= self._FALLBACK_POOL_MAX_SIZE:
                oldest_key = next(iter(self._fallback_chunk_pool))
                old_chunk = self._fallback_chunk_pool.pop(oldest_key)
                try:
                    old_chunk.close()
                except Exception:
                    pass
                bump('fallback_chunk_pool_evict')
            
            # Create new chunk and add to pool
            # Use HIGH priority (low number) for fallback chunks since they're blocking
            # tile completion. Regular mipmap 0 chunks have priority ~5-10, so we use
            # priority 0 to ensure fallback chunks get processed urgently.
            # Pass tile_id for completion tracking (predictive DDS generation)
            chunk = Chunk(col, row, self.maptype, zoom, priority=0, cache_dir=self.cache_dir, tile_id=self.id)
            self._fallback_chunk_pool[key] = chunk
            bump('fallback_chunk_pool_miss')
            
            # Check cache - if hit, it's ready immediately
            if chunk.ready.is_set():
                log.debug(f"Created shared fallback chunk {chunk} - already cached")
            else:
                log.debug(f"Created shared fallback chunk {chunk} - needs download")
            
            return chunk

    def get_or_build_lower_mipmap_chunk(self, target_mipmap, col, row, zoom, 
                                         main_budget=None, fallback_budget=None,
                                         fallback_timeout=None):
        """
        Cascading fallback: Try to get/build progressively lower-detail mipmaps.
        Only downloads chunks on-demand when needed (lazy evaluation).
        
        OPTIMIZED: Uses shared chunk pool to prevent duplicate downloads.
        When multiple chunks fail and need the same parent, they share one download.
        
        Budget strategy:
        - Uses main_budget first (remaining time from tile's main budget)
        - When main_budget exhausts, creates/switches to fallback_budget (extra time)
        - This ensures max total time is: main_budget + fallback_timeout
        
        Args:
            target_mipmap: The mipmap level we need (e.g., 0)
            col, row, zoom: Chunk coordinates at target zoom
            main_budget: Primary TimeBudget (tile's main budget)
            fallback_budget: Extra TimeBudget for fallback extension (may be None, created lazily)
            fallback_timeout: Seconds for fallback budget (used to create it lazily when main exhausts)
        
        Returns:
            Upscaled AoImage or None
        """
        # Track the fallback budget - may be created lazily when main exhausts
        _fallback_budget = fallback_budget
        _using_fallback_budget = False
        
        def get_active_budget():
            """Return the currently active budget, switching from main to fallback when needed."""
            nonlocal _fallback_budget, _using_fallback_budget
            if main_budget and not main_budget.exhausted:
                return main_budget
            # Main exhausted - try fallback budget
            if _fallback_budget is None and fallback_timeout:
                # Create fallback budget NOW (timer starts when main exhausts)
                _fallback_budget = TimeBudget(fallback_timeout)
                log.info(f"Cascading fallback: main budget exhausted, creating fallback budget {fallback_timeout:.1f}s")
            if _fallback_budget and not _fallback_budget.exhausted:
                if not _using_fallback_budget:
                    log.debug(f"Cascading fallback: switching to fallback budget "
                             f"(remaining={_fallback_budget.remaining:.2f}s)")
                    _using_fallback_budget = True
                return _fallback_budget
            return None
        
        # Early exit if both budgets exhausted
        active_budget = get_active_budget()
        if main_budget is None and fallback_budget is None:
            pass  # No budget constraints
        elif active_budget is None:
            log.debug(f"Cascading fallback: skipping - all budgets exhausted")
            return None
        
        # Try each progressively lower-detail mipmap
        for fallback_mipmap in range(target_mipmap + 1, self.max_mipmap + 1):
            # Check budget at each iteration (may switch from main to fallback)
            active_budget = get_active_budget()
            if (main_budget is not None or fallback_budget is not None) and active_budget is None:
                log.debug(f"Cascading fallback: stopping at mipmap {fallback_mipmap} - all budgets exhausted")
                break
            
            log.debug(f"Cascading fallback: trying mipmap {fallback_mipmap} for failed mipmap {target_mipmap}")
            
            # Calculate coordinates at fallback mipmap level
            mipmap_diff = fallback_mipmap - target_mipmap
            fallback_col = col >> mipmap_diff
            fallback_row = row >> mipmap_diff
            fallback_zoom = zoom - mipmap_diff
            
            # OPTIMIZED: Use shared pool to prevent duplicate downloads
            # Multiple failing chunks that need the same parent share one download
            fallback_chunk = self._get_shared_fallback_chunk(fallback_col, fallback_row, fallback_zoom)
            
            # If not ready, submit for download and wait
            if not fallback_chunk.ready.is_set():
                # Submit if not already in flight or queue
                if not fallback_chunk.in_flight and not fallback_chunk.in_queue:
                    chunk_getter.submit(fallback_chunk)
                
                # Wait for download with budget awareness
                # Uses active_budget which may switch from main to fallback mid-wait
                per_chunk_timeout = self.get_maxwait()
                active_budget = get_active_budget()
                if active_budget:
                    fallback_chunk_ready = active_budget.wait_with_budget(
                        fallback_chunk.ready, max_single_wait=per_chunk_timeout
                    )
                else:
                    # No budget constraints - use simple timeout
                    fallback_chunk_ready = fallback_chunk.ready.wait(timeout=per_chunk_timeout)
                
                if not fallback_chunk_ready:
                    # Check if we can switch to fallback budget and continue
                    active_budget = get_active_budget()
                    if active_budget:
                        log.debug(f"Cascading fallback: mipmap {fallback_mipmap} timed out, "
                                 f"continuing with budget (remaining={active_budget.remaining:.2f}s)")
                    else:
                        log.debug(f"Cascading fallback: mipmap {fallback_mipmap} timed out, all budgets exhausted")
                    # Don't close - shared pool manages lifecycle
                    continue
            
            # TOCTOU FIX: Capture local reference before checking/using.
            # Same pattern as process_chunk() - prevents race where another thread
            # could clear fallback_chunk.data between our check and decode.
            fallback_data = fallback_chunk.data
            
            # Chunk is ready - check if we have valid data
            if not fallback_data:
                log.debug(f"Cascading fallback: chunk {fallback_chunk} ready but no data")
                continue
            
            # Decode and upscale
            try:
                with _decode_sem:
                    fallback_img = AoImage.load_from_memory(fallback_data)
                    if fallback_img is None:
                        log.warning(f"Cascading fallback: load_from_memory returned None for {fallback_chunk}")
                        continue
                
                # Calculate which portion to extract and upscale
                scale_factor = 1 << mipmap_diff
                offset_col = col % scale_factor
                offset_row = row % scale_factor
                
                log.debug(f"CASCADE DEBUG: target=({col},{row}), fallback_chunk=({fallback_col},{fallback_row}), "
                         f"offset=({offset_col},{offset_row}), scale={scale_factor}")
                
                # Pixel position in fallback image
                pixel_x = offset_col * (256 // scale_factor)
                pixel_y = offset_row * (256 // scale_factor)
                crop_size = 256 // scale_factor
                
                # Upscale to 256x256
                upscaled = fallback_img.crop_and_upscale(
                    pixel_x, pixel_y, crop_size, crop_size, scale_factor
                )
                
                log.debug(f"Cascading fallback SUCCESS: upscaled mipmap {fallback_mipmap} -> {target_mipmap} "
                         f"at {col}x{row} (scale {scale_factor}x)")
                bump('upscaled_chunk_count')
                bump('chunk_from_cascade_fallback')
                
                # Don't close fallback_chunk - shared pool manages lifecycle
                # Other threads may still be using this chunk
                return upscaled
                
            except Exception as e:
                log.warning(f"Cascading fallback: failed to upscale from mipmap {fallback_mipmap}: {e}")
                continue
        
        log.debug(f"Cascading fallback: all mipmaps failed for {col}x{row} at mipmap {target_mipmap}")
        return None

    def get_downscaled_from_higher_mipmap(self, target_mipmap, col, row, zoom):
        """
        Try to scale from already-built mipmaps to fill missing chunk.
        Checks both downscaling (from higher-detail) and upscaling (from lower-detail).
        
        Args:
            target_mipmap: The mipmap level we need (e.g., 0 or 3)
            col, row, zoom: Chunk coordinates at target zoom
        
        Returns:
            Scaled AoImage or None
        """
        for higher_mipmap in range(target_mipmap):
            if higher_mipmap not in self.imgs:
                continue  # Haven't built this mipmap yet
            
            img_data = self.imgs[higher_mipmap]
            if not img_data:
                continue
            
            # Unpack metadata (or use old format for backward compat)
            if isinstance(img_data, tuple):
                higher_img = img_data[0]  # Extract image from tuple
            else:
                higher_img = img_data  # Old format: just the image
            
            if higher_img is None:
                continue
            
            # Calculate scale factor
            scale_factor = 1 << (target_mipmap - higher_mipmap)
            
            chunk_offset_x = (col % scale_factor) * 256
            chunk_offset_y = (row % scale_factor) * 256
            
            try:
                # Crop scale_factor*256 region, then downscale to 256
                crop_size = 256 * scale_factor
                cropped = AoImage.new('RGBA', (crop_size, crop_size), (0,0,0,0))
                
                # PHASE 2 FIX #6 & #8: Validate crop coordinates
                if chunk_offset_x < 0 or chunk_offset_y < 0:
                    log.warning(f"GET_IMG: Negative crop offset: ({chunk_offset_x},{chunk_offset_y}), skipping fallback")
                    continue
                
                # Check bounds
                higher_width, higher_height = higher_img.size
                if chunk_offset_x + crop_size > higher_width or chunk_offset_y + crop_size > higher_height:
                    log.warning(f"GET_IMG: Crop extends beyond image: pos=({chunk_offset_x},{chunk_offset_y}), size={crop_size}, image=({higher_width}x{higher_height})")
                    continue
                
                if not higher_img.crop(cropped, (chunk_offset_x, chunk_offset_y)):
                    log.warning(f"GET_IMG: crop() failed for fallback at ({chunk_offset_x},{chunk_offset_y})")
                    continue
                
                downscale_steps = int(math.log2(scale_factor))
                downscaled = cropped.reduce_2(downscale_steps)
                
                log.debug(f"Downscaled mipmap {higher_mipmap} to fill missing mipmap {target_mipmap} chunk at {col}x{row}")
                bump('downscaled_chunk_count')
                return downscaled
            except Exception as e:
                log.debug(f"Failed to downscale from mipmap {higher_mipmap}: {e}")
                continue
        
        # Search for lower-detail mipmaps (higher mipmap numbers) to upscale
        for lower_mipmap in range(target_mipmap + 1, self.max_mipmap + 1):
            if lower_mipmap not in self.imgs:
                continue
            
            img_data = self.imgs[lower_mipmap]
            if not img_data:
                continue
            
            # Unpack metadata
            if isinstance(img_data, tuple):
                lower_img, base_col, base_row, base_zoom = img_data
            else:
                continue  # Old format without metadata, skip
            
            # Calculate scale factor and relative position
            scale_factor = 1 << (lower_mipmap - target_mipmap)
            
            # Map requested chunk to position in lower-detail image
            # lower_img was built from chunks starting at (base_col, base_row) at base_zoom
            # We need chunk (col, row) at zoom
            
            # Convert requested chunk coords to the zoom level of the lower image
            zoom_diff = zoom - base_zoom
            if zoom_diff >= 0:
                # Requested chunk is at higher or same zoom as image base
                # Calculate which parent chunk at base_zoom level
                parent_col = col >> zoom_diff
                parent_row = row >> zoom_diff
                
                # Relative to base position (which parent chunk in the image)
                rel_col = parent_col - base_col
                rel_row = parent_row - base_row
                
                # Calculate sub-chunk offset within that parent (0 to 2^zoom_diff-1)
                sub_col = col & ((1 << zoom_diff) - 1)  # Equivalent to col % (1 << zoom_diff)
                sub_row = row & ((1 << zoom_diff) - 1)
            else:
                # Requested chunk is at lower zoom (shouldn't happen but handle it)
                parent_col = col << (-zoom_diff)
                parent_row = row << (-zoom_diff)
                rel_col = parent_col - base_col
                rel_row = parent_row - base_row
                sub_col = 0
                sub_row = 0
            
            # Size to crop from lower image (will be upscaled by scale_factor)
            crop_size = 256 // scale_factor
            
            # Each parent chunk in the image is 256px wide
            # We need to find the sub-chunk within that 256x256 parent
            # Each sub-chunk occupies (256 // scale_factor) pixels in the parent
            sub_chunk_size_in_parent = crop_size  # Same as 256 // scale_factor
            pixel_x = rel_col * 256 + sub_col * sub_chunk_size_in_parent
            pixel_y = rel_row * 256 + sub_row * sub_chunk_size_in_parent
            
            log.debug(f"MIPMAP UPSCALE DEBUG: target=({col},{row},z{zoom}) base=({base_col},{base_row},z{base_zoom}) parent=({parent_col},{parent_row}) sub=({sub_col},{sub_row}) pixel=({pixel_x},{pixel_y}) crop={crop_size}")
            
            # Bounds check
            img_width, img_height = lower_img.size
            if (pixel_x < 0 or pixel_y < 0 or 
                pixel_x + crop_size > img_width or 
                pixel_y + crop_size > img_height):
                log.debug(f"Upscale bounds check failed: pos ({pixel_x},{pixel_y}) crop {crop_size} img ({img_width}x{img_height})")
                continue
            
            try:
                upscaled = lower_img.crop_and_upscale(
                    pixel_x, pixel_y, crop_size, crop_size, scale_factor
                )
                log.debug(f"Upscaled mipmap {lower_mipmap} (zoom {base_zoom}) to fill mipmap {target_mipmap} chunk at {col}x{row}")
                bump('upscaled_chunk_count')
                return upscaled
            except Exception as e:
                log.debug(f"Failed to upscale from mipmap {lower_mipmap}: {e}")
                continue
        
        return None

    def get_best_chunk(self, col, row, mm, zoom):
        """
        Search disk cache for lower-zoom JPEG chunks and upscale to fill missing chunk.
        
        OPTIMIZED: Uses direct filesystem checks instead of creating Chunk objects.
        This avoids the overhead of creating threading primitives (Event objects)
        for each cache lookup. Only reads file data when cache hit is confirmed.
        
        Args:
            col, row: Chunk coordinates at target zoom
            mm: Target mipmap level
            zoom: Target zoom level
            
        Returns:
            Upscaled AoImage or None if no cached fallback found
        """
        max_search_zoom = self.max_zoom

        for i in range(mm + 1, self.max_mipmap + 1):
            # Difference between requested mm and found image mm level
            diff = i - mm
            
            # Equivalent col, row, zl at lower zoom
            col_p = col >> diff
            row_p = row >> diff
            zoom_p = zoom - i
            
            # Don't search beyond our detected actual_max_zoom
            if zoom_p > max_search_zoom:
                continue

            scalefactor = min(1 << diff, 16)

            # OPTIMIZED: Direct cache path check without Chunk object creation
            # This avoids creating threading.Event objects for each lookup
            chunk_id = f"{col_p}_{row_p}_{zoom_p}_{self.maptype}"
            cache_path = os.path.join(self.cache_dir, f"{chunk_id}.jpg")
            
            # Fast existence check - avoids Chunk object overhead
            if not os.path.isfile(cache_path):
                bump('chunk_miss')
                continue
            
            # Cache file exists - read and validate
            bump('chunk_hit')
            log.debug(f"Found cache file for {chunk_id}, reading...")
            
            # Read file data with retry for Windows file locking
            data = None
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    data = Path(cache_path).read_bytes()
                    break
                except PermissionError:
                    if attempt < max_attempts:
                        time.sleep(0.02 * attempt)
                        continue
                    log.debug(f"Permission denied reading cache {cache_path}")
                    break
                except FileNotFoundError:
                    # File was deleted between check and read (race condition)
                    break
                except OSError as e:
                    winerr = getattr(e, 'winerror', None)
                    if winerr in (5, 32, 33) and attempt < max_attempts:
                        time.sleep(0.02 * attempt)
                        continue
                    log.debug(f"OSError reading cache {cache_path}: {e}")
                    break
            
            if not data:
                continue
            
            # Validate JPEG header
            if not _is_jpeg(data[:3]):
                log.debug(f"Cache file {cache_path} not a valid JPEG")
                continue
            
            # Touch the file to update mtime for LRU cache management
            try:
                os.utime(cache_path, None)
            except (FileNotFoundError, PermissionError):
                pass
        
            log.debug(f"Found cached JPEG for {col}x{row}x{zoom} (mm{mm}) at {col_p}x{row_p}x{zoom_p} (mm{i}), upscaling {scalefactor}x")
            
            # Offset into chunk
            col_offset = col % scalefactor
            row_offset = row % scalefactor

            log.debug(f"UPSCALE DEBUG: col={col}, row={row}, col_p={col_p}, row_p={row_p}, col_offset={col_offset}, row_offset={row_offset}, scalefactor={scalefactor}")

            # Pixel dimensions to extract from source
            w_p = max(1, 256 >> diff)
            h_p = max(1, 256 >> diff)

            log.debug(f"Pixel Size: {w_p}x{h_p}")

            # Load image to crop
            try:
                img_p = AoImage.load_from_memory(data)
            except Exception as e:
                log.error(f"Exception loading cached JPEG {chunk_id} into memory: {e}")
                continue
            
            if not img_p:
                log.warning(f"Failed to load cached JPEG {chunk_id} into memory (returned None).")
                continue

            # Crop the relevant portion
            crop_img = AoImage.new('RGBA', (w_p, h_p), (0, 255, 0))
            img_p.crop(crop_img, (col_offset * w_p, row_offset * h_p))
            chunk_img = crop_img.scale(scalefactor)

            # Free source image memory
            try:
                img_p.close()
            except Exception:
                pass
            
            # Track upscaling from cached JPEGs
            bump('upscaled_from_jpeg_count')
            return chunk_img

        log.debug(f"No best chunk found for {col}x{row}x{zoom}!")
        return None

    def get_maxwait(self):
        effective_maxwait = self.maxchunk_wait
        # Only extend maxwait during true initial loading, not during temporary disconnects
        if CFG.autoortho.suspend_maxwait and not datareftracker.has_ever_connected:
            effective_maxwait = 20
        return effective_maxwait

    def get_fallback_level(self):
        """Get the fallback level as an integer.
        
        Converts string config values to integer:
        - 'none' -> 0 (skip all fallbacks)
        - 'cache' -> 1 (disk cache and pre-built mipmaps only)
        - 'full' -> 2 (all fallbacks including network downloads)
        
        For backwards compatibility, also accepts integer strings '0', '1', '2'
        and boolean True/False (legacy config parsing artifacts).
        """
        fb_value = getattr(CFG.autoortho, 'fallback_level', 'cache')
        
        # Handle string values
        if isinstance(fb_value, str):
            fb_lower = fb_value.lower().strip()
            if fb_lower == 'none':
                return 0
            elif fb_lower == 'cache':
                return 1
            elif fb_lower == 'full':
                return 2
            else:
                # Try parsing as integer for backwards compatibility
                try:
                    # Clamp to valid range 0-2
                    return max(0, min(2, int(fb_value)))
                except ValueError:
                    return 1  # Default to cache
        # Handle boolean (from SectionParser when value was '0' or '1')
        elif isinstance(fb_value, bool):
            return 2 if fb_value else 0
        # Handle integer
        elif isinstance(fb_value, int):
            return max(0, min(2, fb_value))
        else:
            return 1  # Default to cache

    def get_fallback_extends_budget(self):
        """Check if fallbacks should extend beyond the time budget.
        
        When True and fallback_level is 'full', network fallbacks will continue
        even after the tile time budget is exhausted (prioritizing quality over timing).
        
        When False, fallbacks respect the time budget strictly (prioritizing timing over quality).
        
        Returns:
            bool: True if fallbacks should ignore budget exhaustion
        """
        fb_value = getattr(CFG.autoortho, 'fallback_extends_budget', False)
        
        # Handle string values from config
        if isinstance(fb_value, str):
            return fb_value.lower().strip() in ('true', '1', 'yes', 'on')
        # Handle boolean directly
        elif isinstance(fb_value, bool):
            return fb_value
        else:
            return False  # Default: respect budget

    def _try_native_mipmap_build(self, mipmap: int, time_budget=None) -> bool:
        """
        Try to build a single mipmap level using native aopipeline.
        
        This is ~3-4x faster than Python for mipmaps with 16+ chunks:
        - Mipmap 1 (256 chunks): ~4x faster
        - Mipmap 2 (64 chunks): ~3.3x faster
        - Mipmap 3 (16 chunks): ~3x faster
        - Mipmap 4 (4 chunks): Minimal benefit, but still faster
        
        Uses build_single_mipmap() which does parallel JPEG decode + DXT compress.
        Returns raw DXT bytes that are written directly to DDS mipmap buffer.
        
        Optimized data loading:
        1. Try bundle (AOB2) loading first - fastest path
        2. Batch cache read with _batch_read_cache_files() - parallel I/O
        3. Download missing chunks if below threshold - respects time budget
        
        Args:
            mipmap: Mipmap level to build (1-4 typically, 0 uses full aopipeline)
            time_budget: Optional TimeBudget for chunk collection and downloads
            
        Returns:
            True if native build succeeded
            False if should fall back to Python path
        """
        build_start = time.monotonic()
        
        # ═══════════════════════════════════════════════════════════════════════
        # EARLY CHECKS
        # ═══════════════════════════════════════════════════════════════════════
        
        # Skip for mipmap 0 - use full aopipeline path instead
        if mipmap == 0:
            return False
        
        # Guard against race where tile is being closed
        if self.dds is None:
            log.debug(f"_try_native_mipmap_build: DDS is None for {self.id}")
            return False
        
        # Check if native mipmap build is enabled
        if not getattr(CFG.autoortho, 'native_mipmap_enabled', True):
            return False
        
        # Get native DDS module
        native_dds = _get_native_dds()
        if native_dds is None or not hasattr(native_dds, 'build_single_mipmap'):
            return False
        
        # Calculate zoom level for this mipmap
        zoom = self.max_zoom - mipmap
        if zoom < self.min_zoom:
            return False
        
        # Get threshold config with validation
        # UNIFIED: Use the same threshold as live_aopipeline for consistency
        # This removes the separate native_mipmap_min_ratio config
        min_ratio = float(getattr(CFG.autoortho, 'live_aopipeline_min_chunk_ratio', 0.9))
        min_ratio = max(0.0, min(1.0, min_ratio))  # Clamp to valid range
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 1: Create chunks and try bundle loading (fastest path)
        # ═══════════════════════════════════════════════════════════════════════
        
        # Wait for any pending consolidation OUTSIDE lock
        self._wait_for_pending_consolidation_if_needed()
        
        # Ensure chunks exist for this zoom level
        # Note: _create_chunks already tries bundle loading internally
        self._create_chunks(zoom)
        chunks = self.chunks.get(zoom, [])
        
        if not chunks:
            log.debug(f"_try_native_mipmap_build: No chunks for mipmap {mipmap} zoom {zoom}")
            return False
        
        total_chunks = len(chunks)
        jpeg_datas = [None] * total_chunks
        ready_count = 0
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 2: Collect memory-resident chunks (instant)
        # ═══════════════════════════════════════════════════════════════════════
        
        need_cache_read = []
        for i, chunk in enumerate(chunks):
            # TOCTOU safety: capture reference atomically (GIL protects this)
            chunk_data = chunk.data
            if chunk.ready.is_set() and chunk_data:
                jpeg_datas[i] = chunk_data
                ready_count += 1
            else:
                need_cache_read.append(i)
        
        # Check if bundle loaded everything
        if ready_count == total_chunks:
            bump('native_mipmap_bundle_hit')
            log.debug(f"_try_native_mipmap_build: All {total_chunks} chunks from bundle/memory")
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 3: Batch cache read for missing chunks (parallel I/O)
        # ═══════════════════════════════════════════════════════════════════════
        
        if need_cache_read and ready_count < total_chunks:
            cache_paths = [chunks[i].cache_path for i in need_cache_read]
            cached_data = _batch_read_cache_files(cache_paths)
            
            if cached_data:
                for i in need_cache_read:
                    chunk = chunks[i]
                    if chunk.cache_path in cached_data:
                        data = cached_data[chunk.cache_path]
                        chunk.set_cached_data(data)
                        jpeg_datas[i] = data
                        ready_count += 1
                        bump('chunk_hit')
                
                log.debug(f"_try_native_mipmap_build: Batch cache read - "
                         f"{len(cached_data)}/{len(cache_paths)} hits for mipmap {mipmap}")
            
            # Update need list for potential download phase
            need_cache_read = [i for i in need_cache_read if jpeg_datas[i] is None]
            
            # ═══════════════════════════════════════════════════════════════
            # JPEG-TO-BUNDLE FALLBACK: Handle race condition with consolidator
            # ═══════════════════════════════════════════════════════════════
            # If batch read missed files, consolidator may have deleted them
            # while creating a bundle. Check if bundle now exists, or wait
            # for pending consolidation to avoid re-downloading.
            if need_cache_read:
                try:
                    try:
                        from autoortho.utils.bundle_paths import get_bundle2_path
                        from autoortho.aopipeline import AoBundle2
                    except ImportError:
                        from utils.bundle_paths import get_bundle2_path
                        from aopipeline import AoBundle2
                    
                    bundle_path = get_bundle2_path(self.cache_dir, self.row, self.col, self.maptype, self.tilename_zoom)
                    bundle_datas = None
                    bundle_has_data = False
                    
                    # SMART FALLBACK: Only attempt if bundle exists
                    bundle_exists = os.path.exists(bundle_path)
                    if not bundle_exists:
                        bump('jpeg_to_bundle_fallback_skipped')
                    
                    # First check: bundle exists?
                    if bundle_exists:
                        with AoBundle2.Bundle2Python(bundle_path) as bundle:
                            if bundle.has_zoom(zoom):
                                bundle_datas = bundle.get_all_chunks(zoom)
                                bundle_has_data = True
                    
                    # Second check: consolidation pending or recently completed?
                    # CRITICAL: Pass zoom to is_pending() - only wait for OUR zoom!
                    if not bundle_has_data and _bundle_consolidator is not None:
                        bump('jpeg_to_bundle_fallback_entered')
                        waited_for_consolidation = False
                        if _bundle_consolidator.is_pending(self.row, self.col, self.maptype, zoom):
                            log.debug(f"_try_native_mipmap_build: waiting for pending consolidation (zoom {zoom})")
                            bump('jpeg_to_bundle_wait_started')
                            wait_start = time.monotonic()
                            # Pass zoom to wait for THIS specific zoom level's consolidation
                            wait_result = _bundle_consolidator.wait_for_pending(self.row, self.col, self.maptype, timeout=60.0, zoom=zoom)
                            wait_time = time.monotonic() - wait_start
                            waited_for_consolidation = True
                            
                            if wait_result:
                                bump('jpeg_to_bundle_wait_success')
                                if wait_time > 5.0:
                                    bump('jpeg_to_bundle_wait_long')
                                    log.debug(f"_try_native_mipmap_build: waited {wait_time:.1f}s for consolidation")
                            elif wait_time > 30.0:
                                bump('jpeg_to_bundle_wait_timeout')
                                log.warning(f"_try_native_mipmap_build: consolidation wait TIMEOUT after {wait_time:.1f}s - "
                                           f"tile {self.row}_{self.col}_{self.maptype} may have stuck consolidation")
                            else:
                                bump('jpeg_to_bundle_wait_already_done')
                        
                        # ALWAYS try bundle - even if is_pending was False
                        # Consolidation may have completed between first check and now
                        if not bundle_has_data and os.path.exists(bundle_path):
                            bump('jpeg_to_bundle_fallback_bundle_exists')
                            with AoBundle2.Bundle2Python(bundle_path) as bundle:
                                if bundle.has_zoom(zoom):
                                    bump('jpeg_to_bundle_fallback_has_zoom')
                                    bundle_datas = bundle.get_all_chunks(zoom)
                                    bundle_has_data = True
                                    if not waited_for_consolidation:
                                        bump('jpeg_to_bundle_fallback_not_pending')
                                else:
                                    bump('jpeg_to_bundle_fallback_no_zoom')
                    
                    # Apply recovered data
                    if bundle_has_data and bundle_datas:
                        fallback_hits = 0
                        for i in need_cache_read:
                            if i < len(bundle_datas) and bundle_datas[i]:
                                chunks[i].set_cached_data(bundle_datas[i])
                                jpeg_datas[i] = bundle_datas[i]
                                ready_count += 1
                                fallback_hits += 1
                        if fallback_hits > 0:
                            log.debug(f"_try_native_mipmap_build: JPEG-to-bundle fallback - "
                                     f"recovered {fallback_hits}/{len(need_cache_read)} for mipmap {mipmap}")
                            bump('jpeg_to_bundle_fallback')
                        # Update need list
                        need_cache_read = [i for i in need_cache_read if jpeg_datas[i] is None]
                except Exception as e:
                    log.debug(f"_try_native_mipmap_build: JPEG-to-bundle fallback failed: {e}")
            # ═══════════════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 4: Download missing chunks (same as aopipeline)
        # ═══════════════════════════════════════════════════════════════════════
        # Trigger downloads for missing chunks and wait for the full budget.
        # This ensures native mipmap path works like aopipeline:
        # trigger -> fetch from network or cache -> threshold check -> build
        
        if ready_count < total_chunks * min_ratio and need_cache_read:
            # Check if we have time budget for downloads
            if time_budget and not time_budget.exhausted:
                # Submit download requests for missing chunks
                missing_chunks_indices = []
                for i in need_cache_read:
                    chunk = chunks[i]
                    if jpeg_datas[i] is None and not chunk.ready.is_set():
                        # Submit for download if not already in queue
                        if not getattr(chunk, 'in_queue', False) and not getattr(chunk, 'in_flight', False):
                            chunk.priority = 0  # High priority
                            chunk_getter.submit(chunk)
                        missing_chunks_indices.append(i)
                
                if missing_chunks_indices:
                    # Wait for downloads with full remaining budget
                    wait_time = time_budget.remaining
                    if wait_time > 0:
                        wait_deadline = time.monotonic() + wait_time
                        
                        while time.monotonic() < wait_deadline:
                            # Count ready chunks (including newly completed ones)
                            current_ready = sum(1 for i in range(total_chunks) 
                                               if jpeg_datas[i] is not None or 
                                               (chunks[i].ready.is_set() and chunks[i].data))
                            if current_ready >= total_chunks * min_ratio:
                                break
                            time.sleep(0.01)
                        
                        # Collect newly downloaded chunks
                        for i in missing_chunks_indices:
                            chunk = chunks[i]
                            if jpeg_datas[i] is None and chunk.ready.is_set():
                                chunk_data = chunk.data
                                if chunk_data:
                                    jpeg_datas[i] = chunk_data
                                    ready_count += 1
        
        # ═══════════════════════════════════════════════════════════════════════
        # THRESHOLD CHECK
        # ═══════════════════════════════════════════════════════════════════════
        
        if ready_count < total_chunks * min_ratio:
            log.debug(f"_try_native_mipmap_build: Only {ready_count}/{total_chunks} chunks ready "
                     f"for mipmap {mipmap} ({ready_count/total_chunks*100:.0f}%), "
                     f"threshold {min_ratio*100:.0f}%, falling back to Python")
            bump('native_mipmap_threshold_miss')
            # Still schedule consolidation for partial data - merge will fill in later
            if ready_count > 0:
                self._schedule_immediate_consolidation(jpeg_datas, zoom)
            return False
        
        # ═══════════════════════════════════════════════════════════════════════
        # SCHEDULE CONSOLIDATION IMMEDIATELY (before build)
        # ═══════════════════════════════════════════════════════════════════════
        # Schedule consolidation NOW while we have the JPEG data in memory.
        # This ensures lower zoom levels get consolidated into the bundle, not
        # just max_zoom. The merge logic will combine with any existing data.
        self._schedule_immediate_consolidation(jpeg_datas, zoom)
        
        # ═══════════════════════════════════════════════════════════════════════
        # BUILD MIPMAPS WITH NATIVE CHUNKS (QUALITY OPTIMIZED)
        # ═══════════════════════════════════════════════════════════════════════
        # QUALITY OPTIMIZATION: Build each mipmap from its native zoom level:
        #   - Mipmap N: ZL(max_zoom - N) chunks
        #   - Mipmap N+1: ZL(max_zoom - N - 1) chunks
        #   - etc.
        # This uses map provider's properly-filtered lower zoom tiles instead
        # of deriving them via reduce_half, which produces better quality.
        #
        # FALLBACK: If native chunks unavailable, reduce_half is used in C code.
        
        try:
            native_build_start = time.monotonic()
            
            # Get compression format and missing color
            dxt_format = CFG.pydds.format.upper()
            missing_color = (
                CFG.autoortho.missing_color[0],
                CFG.autoortho.missing_color[1],
                CFG.autoortho.missing_color[2],
            )
            
            # Calculate max mipmaps to generate (from current to smallest_mm)
            # smallest_mm is the 4×4 mipmap level
            max_mipmaps = self.dds.smallest_mm - mipmap + 1
            
            # Try to use build_all_mipmaps_native if available (best quality)
            # This builds each mipmap from its native zoom level chunks
            if hasattr(native_dds, 'build_all_mipmaps_native'):
                # Collect JPEG data for ALL mipmap levels from current to smallest
                jpeg_datas_per_zoom = []
                for mm in range(mipmap, self.dds.smallest_mm + 1):
                    mm_zoom = self.max_zoom - mm
                    if mm_zoom < self.min_zoom:
                        # No more zoom levels available
                        jpeg_datas_per_zoom.append([])
                        continue
                    
                    if mm == mipmap:
                        # We already collected the first mipmap's chunks above
                        jpeg_datas_per_zoom.append(jpeg_datas)
                    else:
                        # Collect chunks for this zoom level
                        mm_jpeg_datas = self._collect_chunks_for_zoom(mm_zoom)
                        jpeg_datas_per_zoom.append(mm_jpeg_datas)
                        # Schedule consolidation for this zoom level too
                        if mm_jpeg_datas and any(d for d in mm_jpeg_datas):
                            self._schedule_immediate_consolidation(mm_jpeg_datas, mm_zoom)
                
                result = native_dds.build_all_mipmaps_native(
                    jpeg_datas_per_zoom,
                    format=dxt_format,
                    missing_color=missing_color
                )
            elif hasattr(native_dds, 'build_mipmap_chain'):
                # Fallback to build_mipmap_chain (uses reduce_half for smaller mipmaps)
                result = native_dds.build_mipmap_chain(
                    jpeg_datas,
                    format=dxt_format,
                    missing_color=missing_color,
                    max_mipmaps=max_mipmaps
                )
            else:
                # Fallback to build_single_mipmap if chain function not available
                result = native_dds.build_single_mipmap(
                    jpeg_datas,
                    format=dxt_format,
                    missing_color=missing_color
                )
            
            if not result.success:
                log.debug(f"_try_native_mipmap_build: Build failed for mipmap {mipmap}: {result.error}")
                return False
            
            if not result.data or len(result.data) < 16:
                log.debug(f"_try_native_mipmap_build: Build produced too few bytes for mipmap {mipmap}")
                return False
            
            # Guard against DDS being cleared during build
            if self.dds is None:
                log.debug(f"_try_native_mipmap_build: DDS cleared during build for {self.id}")
                return False
            
            # Write mipmap data to DDS buffers
            if hasattr(result, 'mipmap_count') and result.mipmap_count > 0:
                # MipmapChainResult: write each mipmap to its DDS buffer
                for i in range(result.mipmap_count):
                    target_mipmap = mipmap + i
                    if target_mipmap < len(self.dds.mipmap_list):
                        mip_data = result.get_mipmap_data(i)
                        if mip_data:
                            self.dds.mipmap_list[target_mipmap].databuffer = BytesIO(initial_bytes=mip_data)
                            self.dds.mipmap_list[target_mipmap].retrieved = True
                
                # For mipmaps beyond smallest_mm, copy the 4×4 block
                # (This matches Python gen_mipmaps behavior)
                smallest_mm = self.dds.smallest_mm
                if mipmap + result.mipmap_count - 1 >= smallest_mm:
                    smallest_data = result.get_mipmap_data(result.mipmap_count - 1)
                    if smallest_data:
                        for mm in self.dds.mipmap_list[smallest_mm + 1:]:
                            mm.databuffer = BytesIO(initial_bytes=smallest_data)
                            mm.retrieved = True
                
                log.debug(f"_try_native_mipmap_build: Built {result.mipmap_count} mipmaps "
                         f"({mipmap} to {mipmap + result.mipmap_count - 1})")
            else:
                # SingleMipmapResult: only write the one mipmap
                self.dds.mipmap_list[mipmap].databuffer = BytesIO(initial_bytes=result.data)
                self.dds.mipmap_list[mipmap].retrieved = True
            
            # Record timing stats
            total_time = time.monotonic() - build_start
            native_time = time.monotonic() - native_build_start
            
            mm_stats.set(mipmap, total_time)
            tile_creation_stats.set(mipmap, total_time)
            
            mipmaps_built = getattr(result, 'mipmap_count', 1)
            log.debug(f"_try_native_mipmap_build: SUCCESS mipmap {mipmap} - "
                     f"{len(result.data)} bytes ({mipmaps_built} mipmaps) in {total_time*1000:.0f}ms "
                     f"(native: {native_time*1000:.0f}ms, {ready_count}/{total_chunks} chunks)")
            
            bump('native_mipmap_success')
            
            # Consolidation was already scheduled before the build
            # (see SCHEDULE CONSOLIDATION IMMEDIATELY section above)
            
            return True
            
        except Exception as e:
            log.debug(f"_try_native_mipmap_build: Exception for mipmap {mipmap}: {e}")
            bump('native_mipmap_exception')
            return False

    def _collect_chunks_for_zoom(self, zoom: int) -> list:
        """
        Collect JPEG data for all chunks at a given zoom level.
        
        This is a fast collection method that:
        1. Checks if chunks are already in memory
        2. Falls back to disk cache for missing chunks
        3. Does NOT trigger downloads (for speed)
        
        Used by build_all_mipmaps_native to collect chunks at multiple zoom levels.
        
        Args:
            zoom: Zoom level to collect chunks for
            
        Returns:
            List of JPEG bytes (None for missing chunks)
        """
        # Ensure chunks exist for this zoom level
        self._wait_for_pending_consolidation_if_needed()
        self._create_chunks(zoom)
        chunks = self.chunks.get(zoom, [])
        
        if not chunks:
            return []
        
        total_chunks = len(chunks)
        jpeg_datas = [None] * total_chunks
        need_cache_read = []
        
        # Phase 1: Collect memory-resident chunks (instant)
        for i, chunk in enumerate(chunks):
            chunk_data = chunk.data
            if chunk.ready.is_set() and chunk_data:
                jpeg_datas[i] = chunk_data
            else:
                need_cache_read.append(i)
        
        # Phase 2: Batch cache read for missing chunks
        if need_cache_read:
            cache_paths = [chunks[i].cache_path for i in need_cache_read]
            cached_data = _batch_read_cache_files(cache_paths)
            
            if cached_data:
                for i in need_cache_read:
                    chunk = chunks[i]
                    if chunk.cache_path in cached_data:
                        data = cached_data[chunk.cache_path]
                        chunk.set_cached_data(data)
                        jpeg_datas[i] = data
        
        return jpeg_datas

    def _collect_row_chunk_jpegs(
        self,
        zoom: int,
        startrow: int,
        endrow: int,
        time_budget=None
    ):
        """
        Collect JPEG data for specific chunk rows.
        
        Unlike _collect_chunk_jpegs() which collects all chunks for a zoom level,
        this method collects only the chunks for specific rows, used for partial
        mipmap builds (e.g., building only 1-2 rows of mipmap 0).
        
        Args:
            zoom: Chunk zoom level
            startrow: First row index (0-based, inclusive)
            endrow: Last row index (inclusive)
            time_budget: Optional TimeBudget for download waiting
        
        Returns:
            Tuple of (jpeg_datas, chunks_width, chunks_height):
            - jpeg_datas: List of JPEG bytes in row-major order (None for missing)
            - chunks_width: Number of chunks horizontally
            - chunks_height: Number of rows (endrow - startrow + 1)
        
        Thread Safety:
            - chunk.ready.wait() is thread-safe
            - chunk.data access uses GIL-protected reference copy
        """
        # Wait for any pending consolidation OUTSIDE lock
        self._wait_for_pending_consolidation_if_needed()
        
        # Ensure chunks exist for this zoom level
        self._create_chunks(zoom)
        
        if zoom not in self.chunks:
            return [], 0, 0
        
        all_chunks = self.chunks[zoom]
        total_count = len(all_chunks)
        
        if total_count == 0:
            return [], 0, 0
        
        # Calculate chunks per row (assumes square layout for mipmap 0)
        import math
        chunks_per_side = int(math.sqrt(total_count))
        if chunks_per_side * chunks_per_side != total_count:
            log.debug(f"_collect_row_chunk_jpegs: Non-square chunk count {total_count}")
            return [], 0, 0
        
        chunks_width = chunks_per_side
        chunks_height = endrow - startrow + 1
        
        # Validate row range
        if startrow < 0 or endrow >= chunks_per_side or startrow > endrow:
            log.debug(f"_collect_row_chunk_jpegs: Invalid row range {startrow}-{endrow} "
                     f"for {chunks_per_side} rows")
            return [], 0, 0
        
        # Get chunk indices for the requested rows
        start_idx = startrow * chunks_width
        end_idx = (endrow + 1) * chunks_width
        row_chunks = all_chunks[start_idx:end_idx]
        chunk_count = len(row_chunks)
        
        # Submit downloads for any chunks not ready
        for chunk in row_chunks:
            if not chunk.ready.is_set():
                if not getattr(chunk, 'in_queue', False) and not getattr(chunk, 'in_flight', False):
                    chunk.priority = 0  # High priority for partial builds
                    chunk_getter.submit(chunk)
        
        # Collect JPEG data, waiting for downloads within budget
        jpeg_datas = []
        max_wait = time_budget.remaining if time_budget else 2.0
        
        for i, chunk in enumerate(row_chunks):
            if chunk.ready.is_set():
                # Already ready
                jpeg_datas.append(chunk.data if chunk.data else None)
            elif max_wait > 0:
                # Wait for download
                if chunk.ready.wait(timeout=max_wait):
                    jpeg_datas.append(chunk.data if chunk.data else None)
                else:
                    jpeg_datas.append(None)
                # Update remaining time
                if time_budget:
                    max_wait = max(0.01, time_budget.remaining)
            else:
                # No time left
                jpeg_datas.append(None)
        
        return jpeg_datas, chunks_width, chunks_height

    def _try_native_partial_mipmap_build(
        self,
        mipmap: int,
        startrow: int,
        endrow: int,
        bytes_per_chunk_row: int,
        time_budget=None
    ) -> bool:
        """
        Try to build specific mipmap rows using native aopipeline.
        
        This provides significant speedup for partial reads (header reads that
        extend into mipmap data). Instead of building via Python PIL + DXT
        compression (~46ms), uses native JPEG decode + ISPC compression (~3-5ms).
        
        Use case: When X-Plane reads offset=0 length=65536, it extends past the
        128-byte header into mipmap 0 data. Rather than building the full 2048×2048
        mipmap (64 chunks), we build only the rows actually needed (8 chunks for
        1 row), achieving 10-20x speedup.
        
        Args:
            mipmap: Mipmap level to build (currently only 0 supported)
            startrow: First chunk row to build (0-based)
            endrow: Last chunk row to build (inclusive)
            bytes_per_chunk_row: Bytes per chunk-row in compressed DXT format
            time_budget: Optional TimeBudget for chunk collection
        
        Returns:
            True if native build succeeded and data written to DDS buffer
            False if should fall back to Python path
        
        Thread Safety:
            - Uses _collect_row_chunk_jpegs() which is thread-safe
            - DDS buffer write protected by caller's lock
        """
        # Only implemented for mipmap 0 (biggest benefit)
        if mipmap != 0:
            return False
        
        # Check native library availability
        native_dds = _get_native_dds()
        if native_dds is None:
            return False
        
        # Check for build_partial_mipmap function
        if not hasattr(native_dds, 'build_partial_mipmap'):
            return False
        
        # Calculate zoom for this mipmap
        zoom = self.max_zoom - mipmap
        
        # Collect JPEG data for requested rows
        jpeg_datas, chunks_width, chunks_height = self._collect_row_chunk_jpegs(
            zoom, startrow, endrow, time_budget
        )
        
        chunk_count = chunks_width * chunks_height
        if chunk_count == 0:
            log.debug(f"_try_native_partial_mipmap_build: No chunks for rows {startrow}-{endrow}")
            return False
        
        # Check threshold - need 90% of chunks
        min_ratio = float(getattr(CFG.autoortho, 'live_aopipeline_min_chunk_ratio', 0.9))
        valid_count = sum(1 for d in jpeg_datas if d is not None)
        
        if valid_count < chunk_count * min_ratio:
            log.debug(f"_try_native_partial_mipmap_build: threshold not met "
                     f"({valid_count}/{chunk_count} = {valid_count/chunk_count*100:.0f}%, "
                     f"need {min_ratio*100:.0f}%)")
            bump('native_partial_mipmap_threshold_miss')
            return False
        
        # Get compression format and missing color
        dxt_format = CFG.pydds.format.upper()
        missing_color = (
            CFG.autoortho.missing_color[0],
            CFG.autoortho.missing_color[1],
            CFG.autoortho.missing_color[2],
        )
        
        try:
            build_start = time.monotonic()
            
            # Build partial mipmap using native code
            result = native_dds.build_partial_mipmap(
                jpeg_datas=jpeg_datas,
                chunks_width=chunks_width,
                chunks_height=chunks_height,
                format=dxt_format,
                missing_color=missing_color
            )
            
            if not result.success:
                log.debug(f"_try_native_partial_mipmap_build: Build failed: {result.error}")
                return False
            
            if not result.data or len(result.data) < 16:
                log.debug(f"_try_native_partial_mipmap_build: Too few bytes")
                return False
            
            # Guard against DDS being cleared during build
            if self.dds is None or len(self.dds.mipmap_list) == 0:
                log.debug(f"_try_native_partial_mipmap_build: DDS cleared during build")
                return False
            
            # Write DXT data to correct offset in DDS buffer
            mm = self.dds.mipmap_list[mipmap]
            
            # Ensure buffer exists
            if mm.databuffer is None:
                mm.databuffer = BytesIO()
                mm.databuffer.write(b'\x00' * mm.length)
                mm.databuffer.seek(0)
            
            # Calculate offset for this row in the DXT buffer
            row_offset = startrow * bytes_per_chunk_row
            
            # Write data at correct offset
            mm.databuffer.seek(row_offset)
            mm.databuffer.write(result.data)
            
            # Note: We intentionally do NOT set mm.retrieved = True here
            # because this is a partial build - more rows may be built later
            
            build_time = time.monotonic() - build_start
            log.debug(f"_try_native_partial_mipmap_build: SUCCESS rows {startrow}-{endrow} "
                     f"({result.bytes_written} bytes at offset {row_offset}, "
                     f"{result.elapsed_ms:.1f}ms native, {build_time*1000:.1f}ms total)")
            
            bump('native_partial_mipmap_success')
            
            # Schedule bundle consolidation for this zoom level
            # Track which zoom levels we've scheduled to avoid duplicate scheduling
            # (partial builds happen row-by-row, we only need to schedule once)
            if not hasattr(self, '_partial_build_consolidation_scheduled'):
                self._partial_build_consolidation_scheduled = set()
            
            if zoom not in self._partial_build_consolidation_scheduled:
                self._partial_build_consolidation_scheduled.add(zoom)
                try:
                    # Get ALL chunks for this zoom (not just partial rows) for consolidation
                    all_chunks = self.chunks.get(zoom, [])
                    if all_chunks:
                        all_jpeg_datas = [chunk.data if hasattr(chunk, 'data') and chunk.data else None 
                                          for chunk in all_chunks]
                        schedule_bundle_consolidation(self, priority=1, jpeg_datas=all_jpeg_datas, zoom=zoom)
                        log.debug(f"_try_native_partial_mipmap_build: Scheduled consolidation for ZL{zoom}")
                except Exception as e:
                    log.debug(f"_try_native_partial_mipmap_build: Consolidation scheduling failed: {e}")
            
            return True
            
        except Exception as e:
            log.debug(f"_try_native_partial_mipmap_build: Exception: {e}")
            bump('native_partial_mipmap_exception')
            return False

    #@profile
    @locked
    def get_mipmap(self, mipmap=0, time_budget=None):
        """
        Build a specific mipmap level.
        
        Args:
            mipmap: Mipmap level to build (0=highest detail, 7=lowest)
            time_budget: TimeBudget for this request (created fresh in read_dds_bytes)
        """
        #
        # Protect this method to avoid simultaneous threads attempting mm builds at the same time.
        # Otherwise we risk contention such as waiting get_img call attempting to build an image as 
        # another thread closes chunks.
        #
        
        # Start timing FULL tile creation (download + compose + compress)
        # Use monotonic() for consistent interval measurement (immune to clock adjustments)
        tile_creation_start = time.monotonic()

        log.debug(f"GET_MIPMAP: {self}")

        if mipmap > self.max_mipmap:
            mipmap = self.max_mipmap
        
        # === BUDGET TIMING ===
        # The budget is passed in from read_dds_bytes() - each read() gets its own budget.
        #
        # NOTE: We intentionally do NOT skip get_img/gen_mipmaps when budget is exhausted.
        # Even if the budget is exhausted, we must still:
        # 1. Call get_img() to create an image filled with missing_color
        # 2. Call gen_mipmaps() to compress it into the DDS buffer
        # This ensures X-Plane receives a valid DDS with missing_color tiles instead of
        # black/uninitialized data. Once X-Plane reads a DDS, it's cached and won't refresh.
        # The get_img() method handles budget exhaustion gracefully by skipping chunk downloads
        # but still returning a valid (missing_color filled) image for compression.
        
        if time_budget and time_budget.exhausted:
            log.debug(f"GET_MIPMAP: Budget exhausted, will build mipmap {mipmap} with missing_color (no new downloads)")

        # ═══════════════════════════════════════════════════════════════════════
        # NATIVE MIPMAP BUILD: Try fast native path for mipmaps 1-4
        # ═══════════════════════════════════════════════════════════════════════
        # For mipmaps with 16-256 chunks, native aopipeline is 3-4x faster than Python.
        # This uses build_single_mipmap() for parallel JPEG decode + DXT compress.
        # Falls back to Python path if:
        # - Mipmap 0 (use full aopipeline instead)
        # - Not enough chunks ready (need fallback handling)
        # - Native build fails for any reason
        if mipmap > 0 and self._try_native_mipmap_build(mipmap, time_budget):
            # Native build succeeded - mipmap buffer is populated
            # Note: Timing stats are recorded inside _try_native_mipmap_build
            log.debug(f"GET_MIPMAP: Native build succeeded for mipmap {mipmap}")
            
            try:
                bump_many({f"mm_count:{mipmap}": 1})
            except Exception:
                pass
            
            return True
        # ═══════════════════════════════════════════════════════════════════════

        # We can have multiple threads wait on get_img ...
        log.debug(f"GET_MIPMAP: Next call is get_img which may block!.............")
        # Pass the per-request budget to get_img (each read() gets its own budget)
        new_im = self.get_img(mipmap, maxwait=self.get_maxwait(), time_budget=time_budget)
        if not new_im:
            log.debug("GET_MIPMAP: No updates, so no image generated")
            return True

        self.ready.clear()
        compress_start_time = time.monotonic()
        try:
            if mipmap == 0:
                self.dds.gen_mipmaps(new_im, mipmap, 0) 
            else:
                self.dds.gen_mipmaps(new_im, mipmap) 
        finally:
            # Close image if not cached in self.imgs to free native memory immediately
            # If cached, it will be closed when evicted by _cache_image() or in close()
            if mipmap not in self.imgs:
                try:
                    new_im.close()
                except Exception:
                    pass

        compress_end_time = time.monotonic()
        self.ready.set()

        # Calculate timing metrics
        zoom = self.max_zoom - mipmap
        compress_time = compress_end_time - compress_start_time
        total_creation_time = compress_end_time - tile_creation_start
        
        # Track compression time (legacy stat)
        mm_stats.set(mipmap, compress_time)
        
        # Track FULL tile creation time (new stat for tuning tile_time_budget)
        tile_creation_stats.set(mipmap, total_creation_time)

        # Record per-mipmap count via counters for aggregation
        try:
            bump_many({
                f"mm_count:{mipmap}": 1,
            })
        except Exception:
            pass
        
        # Log tile completion when mipmap 0 is done (full tile delivered to X-Plane)
        if mipmap == 0 and not self._completion_reported:
            self._completion_reported = True
            # Calculate time from first X-Plane request to completion
            if self.first_request_time is not None:
                tile_completion_time = time.monotonic() - self.first_request_time
            else:
                # Fallback: use the mipmap creation time if first_request_time wasn't set
                tile_completion_time = total_creation_time
            
            log.debug(f"GET_MIPMAP: Tile {self} COMPLETED in {tile_completion_time:.2f}s "
                     f"(mipmap 0 done, time from first request)")
        
        # Log per-mipmap creation time for visibility
        log.debug(f"GET_MIPMAP: Tile {self} mipmap {mipmap} created in {total_creation_time:.2f}s "
                 f"(download+compose: {total_creation_time - compress_time:.2f}s, compress: {compress_time:.2f}s)")

        # Schedule bundle consolidation for this zoom level BEFORE closing chunks
        # This uses in-memory JPEG data when available (avoids disk reads)
        mipmap_zoom = self.max_zoom - mipmap
        if mipmap_zoom in self.chunks:
            try:
                zoom_chunks = self.chunks[mipmap_zoom]
                jpeg_datas = [chunk.data if hasattr(chunk, 'data') and chunk.data else None 
                              for chunk in zoom_chunks]
                schedule_bundle_consolidation(self, priority=1, jpeg_datas=jpeg_datas, zoom=mipmap_zoom)
            except Exception as e:
                log.debug(f"GET_MIPMAP: Consolidation scheduling failed: {e}")

        # Close only chunks for THIS mipmap's zoom level (not all zooms!)
        # FIX: Previously closing ALL chunks caused double-downloads when mipmap 0
        # completed but mipmap 4 was still being accessed by X-Plane.
        # Each mipmap uses a different zoom level: mipmap N uses (max_zoom - N).
        if mipmap_zoom in self.chunks:
            log.debug(f"GET_MIPMAP: Closing chunks for mipmap {mipmap} (zoom {mipmap_zoom}).")
            for chunk in self.chunks[mipmap_zoom]:
                chunk.close()
            del self.chunks[mipmap_zoom]
                    #del(chunk.data)
                    #del(chunk.img)
        #return outfile
        log.debug("Results:")
        log.debug(self.dds.mipmap_list)
        return True


    def should_close(self):
        if self.dds.mipmap_list[0].retrieved:
            # Skip bytes_read check for prepopulated tiles - X-Plane may only need small mipmaps
            # even though we populated all mipmaps via aopipeline
            if self._prepopulated:
                return True
            if self.bytes_read < self.dds.mipmap_list[0].length:
                log.warning(f"TILE: {self} retrieved mipmap 0, but only read {self.bytes_read}. Lowest offset: {self.lowest_offset}")
                return False
            else:
                #log.info(f"TILE: {self} retrieved mipmap 0, full read of mipmap! {self.bytes_read}.")
                return True
        else:
            return True


    def close(self):
        log.debug(f"Closing {self}")

        # Check refs first - if still referenced, don't close yet
        if self.refs > 0:
            log.warning(f"TILE: Trying to close, but has refs: {self.refs}")
            return

        # Log mipmap retrieval status (safely check dds first)
        # Skip for prepopulated tiles - X-Plane may only need small mipmaps
        if self.dds is not None and not self._prepopulated:
            try:
                if self.dds.mipmap_list and self.dds.mipmap_list[0].retrieved:
                    if self.bytes_read < self.dds.mipmap_list[0].length:
                        log.warning(f"TILE: {self} retrieved mipmap 0, but only read {self.bytes_read}. Lowest offset: {self.lowest_offset}")
                    else:
                        log.debug(f"TILE: {self} retrieved mipmap 0, full read of mipmap! {self.bytes_read}.")
            except (AttributeError, IndexError):
                pass  # DDS structure incomplete, continue with cleanup

        # ------------------------------------------------------------------
        # Memory-reclamation additions
        # ------------------------------------------------------------------

        # 1) Free any cached AoImage instances (RGBA pixel buffers)
        try:
            for img_data in list(self.imgs.values()):
                # Handle both tuple format (new) and plain image (old)
                if isinstance(img_data, tuple):
                    im = img_data[0]  # Extract image from tuple
                else:
                    im = img_data
                
                if im is not None and hasattr(im, "close"):
                    try:
                        im.close()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.imgs.clear()
            self._imgs_order = []  # Clear LRU tracking list

        # 2) Release DDS mip-map ByteIO buffers so the underlying bytes
        #    are no longer referenced from Python.
        if self.dds is not None:
            try:
                # Use DDS.close() method for proper cleanup
                self.dds.close()
            except Exception:
                pass
            # Drop the DDS object reference itself
            self.dds = None

        # 3) Close all chunks
        try:
            for chunks in self.chunks.values():
                for chunk in chunks:
                    try:
                        chunk.close()
                    except Exception:
                        pass
        except Exception:
            pass
        self.chunks = {}
        
        # 4) Close all fallback chunks in the shared pool
        try:
            with self._fallback_pool_lock:
                for chunk in self._fallback_chunk_pool.values():
                    try:
                        chunk.close()
                    except Exception:
                        pass
                self._fallback_chunk_pool.clear()
        except Exception:
            pass
        
        # 5) Free batch-to-streaming JPEG data cache
        # _last_collected_jpegs can hold ~25MB of JPEG bytes per tile.
        # Without clearing, this data stays attached to the tile object
        # and can't be GC'd if any external reference keeps the tile alive
        # (e.g. TileCompletionTracker, BackgroundDDSBuilder queue).
        self._last_collected_jpegs = None
        self._last_collected_ratio = None
        self._last_collected_missing = None

        # 6) Reset state flags for potential tile reuse
        self._lazy_build_attempted = False
        self._aopipeline_attempted = False
        self._tile_time_budget = None
        self.first_request_time = None
        self._completion_reported = False
        self._is_live = False
        self._live_transition_event = None
        self._active_streaming_builder = None

        # 7) Mark tile as closed so external holders (e.g.
        # BackgroundDDSBuilder) can skip building from stale data
        self._closed = True


def _release_memory_to_os():
    """
    Force the OS to reclaim physical pages from freed Python/C allocations.

    After tile eviction, Python frees objects but the C runtime heap retains
    the pages. This causes RSS to stay high even though the memory is logically
    free. Platform-specific calls return those pages to the OS.
    """
    gc.collect()
    if sys.platform == 'linux':
        try:
            _libc = ctypes.CDLL("libc.so.6")
            _libc.malloc_trim(0)
        except Exception:
            pass
    elif sys.platform == 'darwin':
        try:
            _libc = ctypes.CDLL("libSystem.B.dylib")
            _libc.malloc_zone_pressure_relief(None, 0)
        except Exception:
            pass
    elif sys.platform == 'win32':
        try:
            ctypes.cdll.msvcrt._heapmin()
        except Exception:
            pass


class TileCacher(object):
    hits = 0
    misses = 0

    enable_cache = True
    cache_mem_lim = pow(2,30) * float(CFG.cache.cache_mem_limit)
    cache_tile_lim = 25
    
    # Maximum entries in open_count dict to prevent unbounded memory growth
    _open_count_max = 2000

    def __init__(self, cache_dir='.cache'):
        if MEMTRACE:
            tracemalloc.start()

        self.tiles = OrderedDict()
        self.open_count = OrderedDict()  # Use OrderedDict for LRU-style eviction

        self.maptype_override = CFG.autoortho.maptype_override
        if self.maptype_override:
            log.info(f"Maptype override set to {self.maptype_override}")
            if self.maptype_override == "APPLE":
                apple_token_service.reset_apple_maps_token()
        else:
            log.info(f"Maptype override not set, will use default.")
        log.info(f"Will use Compressor: {CFG.pydds.compressor}")
        self.tc_lock = threading.RLock()
        self._pid = os.getpid()
        # Eviction behavior controls
        self.evict_hysteresis_frac = 0.10  # keep ~10% headroom below limit
        self.evict_headroom_min_bytes = 256 * 1048576  # at least 256MB headroom
        self.evict_leader_ttl_sec = 5  # seconds
        
        self.cache_dir = CFG.paths.cache_dir
        log.info(f"Cache dir: {self.cache_dir}")
        self.min_zoom = int(CFG.autoortho.min_zoom)
        # Set target zoom level directly - much simpler than offset calculations
        self.target_zoom_level = int(CFG.autoortho.max_zoom)  # Direct zoom level target, regardless of tile name
        self.target_zoom_level_near_airports = int(CFG.autoortho.max_zoom_near_airports)
        log.info(f"Target zoom level set to ZL{self.target_zoom_level}")

        # Dynamic zoom configuration
        # When "dynamic", zoom level is computed based on predicted altitude at tile
        # When "fixed" (default), uses the configured max_zoom value
        self.max_zoom_mode = str(CFG.autoortho.max_zoom_mode).lower()
        self.dynamic_zoom_manager = DynamicZoomManager()
        self._last_logged_dynamic_zoom = None  # Track last logged zoom to reduce spam
        if self.max_zoom_mode == "dynamic":
            self.dynamic_zoom_manager.load_from_config(
                CFG.autoortho.dynamic_zoom_steps
            )
            step_count = len(self.dynamic_zoom_manager.get_steps())
            log.info("=" * 60)
            log.info("DYNAMIC ZOOM MODE ACTIVATED")
            log.info(f"  Quality steps configured: {step_count}")
            if step_count > 0:
                log.info(f"  Steps: {self.dynamic_zoom_manager.get_summary()}")
                log.info("  Zoom levels will be adjusted based on predicted altitude")
            else:
                log.warning("  No quality steps configured - using default zoom")
            log.info("=" * 60)
        else:
            log.info("Using fixed max zoom level (ZL%d)", self.target_zoom_level)

        self.clean_t = threading.Thread(target=self.clean, daemon=True)
        self.clean_t.start()

        if system_type == 'windows':
            # Windows doesn't handle FS cache the same way so enable here.
            self.enable_cache = True
            self.cache_tile_lim = 50
    
    def _compute_dynamic_zoom(self, row: int, col: int, tile_zoom: int) -> int:
        """
        Compute dynamic zoom level based on predicted altitude at tile.

        Uses the aircraft's averaged flight data (heading, speed, vertical speed)
        to predict what altitude the aircraft will be at when it reaches the
        closest point to this tile. Then returns the appropriate zoom level
        from the configured quality steps.
        
        If SimBrief flight data is loaded and the "use_flight_data" toggle is enabled,
        and the aircraft is on-route, uses the flight plan altitude for that position
        instead of DataRef-based prediction.

        Args:
            row: Tile row coordinate
            col: Tile column coordinate
            tile_zoom: Default zoom level for this tile

        Returns:
            The computed max zoom level for this tile
        """
        # Get tile center coordinates (needed for both methods)
        tile_lat, tile_lon = _chunk_to_latlon(row, col, tile_zoom)
        
        # Check if SimBrief flight data should be used
        simbrief_altitude_agl = self._get_simbrief_altitude_for_tile(tile_lat, tile_lon)
        if simbrief_altitude_agl is not None:
            # Use SimBrief flight plan AGL altitude for zoom calculation
            if tile_zoom == 18 and not CFG.autoortho.using_custom_tiles:
                zoom = self.dynamic_zoom_manager.get_airport_zoom_for_altitude(simbrief_altitude_agl)
                log.debug(f"Dynamic zoom (SimBrief): {simbrief_altitude_agl}ft AGL -> ZL{zoom} (airport tile)")
                return zoom
            zoom = self.dynamic_zoom_manager.get_zoom_for_altitude(simbrief_altitude_agl)
            log.debug(f"Dynamic zoom (SimBrief): {simbrief_altitude_agl}ft AGL -> ZL{zoom}")
            return zoom
        
        # Fall back to DataRef-based calculation
        return self._compute_dynamic_zoom_from_datarefs(row, col, tile_zoom, tile_lat, tile_lon)
    
    def _get_simbrief_altitude_for_tile(self, tile_lat: float, tile_lon: float) -> Optional[int]:
        """
        Get conservative AGL altitude from SimBrief flight plan for a tile position.
        
        Returns the planned altitude Above Ground Level (AGL) if:
        - SimBrief flight data is loaded
        - The "use_flight_data" toggle is enabled
        - The aircraft is currently on-route (within deviation threshold)
        
        When multiple waypoints are within the consideration radius:
        - Uses the LOWEST flight altitude (MSL) - accounts for descent
        - Uses the HIGHEST ground elevation - accounts for mountains
        - Conservative AGL = lowest_MSL - highest_ground
        
        This ensures maximum detail when flying over areas with varied terrain
        (e.g., descending over mountains).
        
        AGL Calculation:
            AGL = MSL altitude - terrain elevation (ground_height from SimBrief)
            
            AGL is used because it represents the actual height above the terrain
            being viewed. This is more relevant for imagery quality:
            - 10,000 ft MSL over 5,000 ft mountains = 5,000 ft AGL (needs higher zoom)
            - 10,000 ft MSL over the ocean = 10,000 ft AGL (can use lower zoom)
        
        Args:
            tile_lat: Tile center latitude
            tile_lon: Tile center longitude
            
        Returns:
            Conservative AGL altitude in feet if SimBrief data should be used, None otherwise
        """
        # Check if SimBrief integration is enabled
        if not hasattr(CFG, 'simbrief'):
            return None
        
        use_flight_data = getattr(CFG.simbrief, 'use_flight_data', False)
        if isinstance(use_flight_data, str):
            use_flight_data = use_flight_data.lower() in ('true', '1', 'yes', 'on')
        
        if not use_flight_data:
            return None
        
        # Check if flight data is loaded
        if not simbrief_flight_manager.is_loaded:
            return None
        
        # Get aircraft position to check if on-route
        with datareftracker._lock:
            if not datareftracker.data_valid:
                return None
            aircraft_lat = datareftracker.lat
            aircraft_lon = datareftracker.lon
        
        # Get deviation threshold from config
        deviation_threshold = float(getattr(CFG.simbrief, 'route_deviation_threshold_nm', 40))
        
        # Check if aircraft is on-route
        if not simbrief_flight_manager.is_on_route(aircraft_lat, aircraft_lon, deviation_threshold):
            log.debug(f"Aircraft deviated from route, using DataRef-based zoom calculation")
            return None
        
        # Get consideration radius from config
        consideration_radius = float(getattr(CFG.simbrief, 'route_consideration_radius_nm', 50))
        
        # Get AGL altitude at tile position (uses lowest AGL of fixes within radius)
        # use_agl=True returns Above Ground Level altitude for better terrain awareness
        altitude_agl = simbrief_flight_manager.get_altitude_at_position(
            tile_lat, tile_lon, consideration_radius, use_agl=True
        )
        
        return altitude_agl
    
    def _compute_dynamic_zoom_from_datarefs(self, row: int, col: int, tile_zoom: int,
                                             tile_lat: float, tile_lon: float) -> int:
        """
        Compute dynamic zoom level using DataRef-based altitude prediction.
        
        This is the fallback method when SimBrief data is not available or
        when the aircraft has deviated from the planned route.
        """
        # Get flight averages for prediction
        averages = datareftracker.get_flight_averages()

        # Helper to determine if this is an airport tile
        is_airport_tile = tile_zoom == 18 and not CFG.autoortho.using_custom_tiles

        # If no valid averages, try to use current altitude (AGL)
        if averages is None:
            with datareftracker._lock:
                if datareftracker.data_valid and datareftracker.alt_agl_ft > 0:
                    if is_airport_tile:
                        zoom = self.dynamic_zoom_manager.get_airport_zoom_for_altitude(
                            datareftracker.alt_agl_ft
                        )
                        log.debug(f"Dynamic zoom (current AGL): {datareftracker.alt_agl_ft:.0f}ft -> ZL{zoom} (airport tile)")
                    else:
                        zoom = self.dynamic_zoom_manager.get_zoom_for_altitude(
                            datareftracker.alt_agl_ft
                        )
                        log.debug(f"Dynamic zoom (current AGL): {datareftracker.alt_agl_ft:.0f}ft -> ZL{zoom}")
                    return zoom
            # Fall back to base step or fixed zoom
            base = self.dynamic_zoom_manager.get_base_step()
            if is_airport_tile:
                fallback_zoom = base.zoom_level_airports if base else 18
            else:
                fallback_zoom = base.zoom_level if base else self.target_zoom_level
            log.debug(f"Dynamic zoom: no flight data, using fallback ZL{fallback_zoom}{' (airport tile)' if is_airport_tile else ''}")
            return fallback_zoom

        # Get current position (with lock for thread safety)
        with datareftracker._lock:
            if not datareftracker.data_valid:
                base = self.dynamic_zoom_manager.get_base_step()
                if is_airport_tile:
                    fallback_zoom = base.zoom_level_airports if base else 18
                else:
                    fallback_zoom = base.zoom_level if base else self.target_zoom_level
                log.debug(f"Dynamic zoom: no valid datarefs, using fallback ZL{fallback_zoom}{' (airport tile)' if is_airport_tile else ''}")
                return fallback_zoom

            aircraft_lat = datareftracker.lat
            aircraft_lon = datareftracker.lon
            aircraft_alt_ft = datareftracker.alt_agl_ft

        # Predict altitude at closest approach (using AGL for terrain-aware calculations)
        predicted_alt, will_approach = predict_altitude_at_closest_approach(
            aircraft_lat=aircraft_lat,
            aircraft_lon=aircraft_lon,
            aircraft_alt_ft=aircraft_alt_ft,
            aircraft_hdg=averages['heading'],
            aircraft_speed_mps=averages['ground_speed_mps'],
            vertical_speed_fpm=averages['vertical_speed_fpm'],
            tile_lat=tile_lat,
            tile_lon=tile_lon
        )

        # Get zoom level for predicted altitude
        # Use airport zoom level when near airports (tile_zoom == 18)
        if tile_zoom == 18 and not CFG.autoortho.using_custom_tiles:
            zoom = self.dynamic_zoom_manager.get_airport_zoom_for_altitude(predicted_alt)
            log.debug(f"Dynamic zoom (predicted): {predicted_alt:.0f}ft AGL -> ZL{zoom} (airport tile)")
            return zoom
        zoom = self.dynamic_zoom_manager.get_zoom_for_altitude(predicted_alt)
        log.debug(f"Dynamic zoom (predicted): {predicted_alt:.0f}ft AGL -> ZL{zoom}")
        return zoom

    def _get_target_zoom_level(self, default_zoom: int, row: int = None, col: int = None) -> int:
        """
        Get target zoom level for a tile.

        In fixed mode: Uses the configured max_zoom (current behavior)
        In dynamic mode: Computes zoom based on predicted altitude at tile

        Args:
            default_zoom: The default/base zoom level for this tile
            row: Tile row coordinate (required for dynamic mode)
            col: Tile column coordinate (required for dynamic mode)

        Returns:
            The target zoom level, capped to tile's max supported zoom
        """
        # Dynamic mode - compute based on altitude prediction
        if self.max_zoom_mode == "dynamic" and row is not None and col is not None:
            dynamic_zoom = self._compute_dynamic_zoom(row, col, default_zoom)
            # Still cap to tile's max supported zoom (default + 1 is X-Plane's limit)
            final_zoom = min(default_zoom + 1, dynamic_zoom)
            
            # Log when dynamic zoom changes from the fixed config value (DEBUG level to reduce spam)
            # Only log once per unique (final_zoom, fixed_zoom) pair to avoid flooding logs
            fixed_zoom = self.target_zoom_level
            if default_zoom == 18 and not CFG.autoortho.using_custom_tiles:
                fixed_zoom = self.target_zoom_level_near_airports
            
            log_key = (final_zoom, fixed_zoom, default_zoom)
            if log_key != self._last_logged_dynamic_zoom and final_zoom != fixed_zoom:
                log.debug(f"Dynamic zoom: ZL{final_zoom} (was fixed ZL{fixed_zoom}) - altitude-based adjustment")
                self._last_logged_dynamic_zoom = log_key
            
            return final_zoom

        # Fixed mode - existing behavior
        if CFG.autoortho.using_custom_tiles:
            uncapped_target_zoom = self.target_zoom_level
        else:
            uncapped_target_zoom = (
                self.target_zoom_level_near_airports
                if default_zoom == 18
                else self.target_zoom_level
            )
        return min(default_zoom + 1, uncapped_target_zoom)

    def _to_tile_id(self, row, col, map_type, zoom):
        if self.maptype_override:
            map_type = self.maptype_override
        tile_id = f"{row}_{col}_{map_type}_{zoom}"
        return tile_id

    def show_stats(self):
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        cur_mem = mem_info.rss
        
        # Log detailed memory info for debugging discrepancies with Activity Monitor
        vms = getattr(mem_info, 'vms', 0)
        log.debug(f"Memory detail: RSS={cur_mem//(1024**2)}MB, VMS={vms//(1024**2)}MB, PID={os.getpid()}")
        
        # Report per-process memory to shared store; parent will aggregate
        update_process_memory_stat()
        # Report decode pool stats for native buffer monitoring
        update_decode_pool_stats()
        #set_stat('tile_mem_open', len(self.tiles))
        if self.enable_cache:
            #set_stat('tile_mem_miss', self.misses)
            #set_stat('tile_mem_hits', self.hits)
            log.debug(f"TILE CACHE:  MISS: {self.misses}  HIT: {self.hits}")
        log.debug(f"NUM OPEN TILES: {len(self.tiles)}.  TOTAL MEM: {cur_mem//1048576} MB")

    # -----------------------------
    # LRU helpers and leader logic
    # -----------------------------
    def _touch_tile(self, idx, tile):
        try:
            # Move to MRU position
            self.tiles.move_to_end(idx, last=True)
        except Exception:
            pass

    def _lru_candidates(self):
        try:
            # Keys iterate from LRU -> MRU after move_to_end
            return list(self.tiles.keys())
        except Exception:
            return list(self.tiles.keys())

    def _has_shared_store(self) -> bool:
        return bool(getattr(STATS, "_remote", None) or os.getenv("AO_STATS_ADDR"))

    def _try_acquire_evict_leader(self) -> bool:
        if not self._has_shared_store():
            return True
        now = int(time.time())
        try:
            leader_until = get_stat('evict_leader_until') or 0
            leader_pid = get_stat('evict_leader_pid') or 0
        except Exception:
            leader_until = 0
            leader_pid = 0

        if int(leader_until) < now:
            # Try to become leader
            try:
                set_stat('evict_leader_pid', self._pid)
                set_stat('evict_leader_until', now + self.evict_leader_ttl_sec)
                return True
            except Exception:
                return False

        # Renew if we are already leader
        if int(leader_pid) == self._pid:
            try:
                set_stat('evict_leader_until', now + self.evict_leader_ttl_sec)
                return True
            except Exception:
                return False

        return False

    def _renew_evict_leader(self) -> None:
        if not self._has_shared_store():
            return
        try:
            now = int(time.time())
            if int(get_stat('evict_leader_pid') or 0) == self._pid:
                set_stat('evict_leader_until', now + self.evict_leader_ttl_sec)
        except Exception:
            pass

    def _evict_batch(self, max_to_evict: int) -> int:
        """
        Evict tiles with minimal lock holding time.
        
        Splits eviction into two phases:
        1. PHASE 1: Pop tiles from dict under lock (fast dict ops only)
        2. PHASE 2: Close tiles OUTSIDE lock (slow I/O operations)
        
        This prevents blocking _get_tile() and _open_tile() during mass evictions.
        
        Also releases external references (TileCompletionTracker) so evicted
        tiles can be garbage-collected immediately.
        """
        evicted = 0
        BATCH_SIZE = 20  # Balance between lock overhead and responsiveness
        
        while evicted < max_to_evict:
            # PHASE 1: Collect batch to evict (under lock - fast dict ops only)
            # Store (tile_id, tile) pairs so we can release external refs
            batch = []
            with self.tc_lock:
                for idx in list(self._lru_candidates()):
                    if len(batch) >= BATCH_SIZE:
                        break
                    if evicted + len(batch) >= max_to_evict:
                        break
                    t = self.tiles.get(idx)
                    if not t:
                        continue
                    if t.refs > 0:
                        continue
                    # Pop from dict immediately - tile is now "orphaned"
                    try:
                        batch.append((idx, self.tiles.pop(idx)))
                    except KeyError:
                        continue
            
            if not batch:
                break
            
            # PHASE 2: Close tiles OUTSIDE lock (slow I/O operations)
            # This is safe because tiles are already removed from self.tiles
            for tile_id, t in batch:
                try:
                    # Release external references BEFORE close() so the
                    # tile object can be garbage-collected once close()
                    # drops the last internal references.
                    # TileCompletionTracker may hold a strong ref to
                    # the tile, preventing GC for up to 10 minutes.
                    if tile_completion_tracker is not None:
                        try:
                            tile_completion_tracker.stop_tracking(
                                tile_id
                            )
                        except Exception:
                            pass
                    t.close()
                except Exception:
                    pass
                finally:
                    t = None
                evicted += 1
        
        return evicted

    def clean(self):
        log.info(f"Started tile clean thread.  Mem limit {self.cache_mem_lim}")
        # Faster cadence when a shared stats store is present (macOS parent)
        fast_mode = self._has_shared_store()
        # Base poll intervals — used when memory is well below limit.
        # When memory approaches the limit (>70%), the interval drops
        # to poll_interval_fast to catch growth before it overshoots.
        poll_interval_normal = 3 if fast_mode else 10
        poll_interval_fast = 1
        poll_interval = poll_interval_normal
        
        # Maximum tile count before forced eviction (prevents memory bloat from tile object overhead)
        # Each tile object costs ~10-50KB in overhead even without loaded data
        max_tile_count = 5000
        
        # Staleness threshold for global memory stats (seconds)
        # If stats are older than this, they're considered unreliable
        global_stat_max_age_sec = 30

        while True:
            process = psutil.Process(os.getpid())
            cur_mem = process.memory_info().rss
            total_evicted = 0
            rss_before_eviction = cur_mem  # Always set so eviction-log block never sees UnboundLocalError

            # Publish this process heartbeat + RSS so the parent can aggregate
            try:
                update_process_memory_stat()
            except Exception:
                pass

            self.show_stats()

            if not self.enable_cache:
                time.sleep(poll_interval)
                continue

            # Use aggregated memory across all workers when available; otherwise local RSS
            # Also check for staleness to avoid using outdated stats
            global_mem_bytes = 0
            global_stat_stale = True
            
            try:
                global_mem_mb = get_stat('cur_mem_mb')
                if isinstance(global_mem_mb, (int, float)) and global_mem_mb > 0:
                    global_mem_bytes = int(global_mem_mb) * 1048576
                    
                    # Check staleness via last update timestamp
                    try:
                        last_update = get_stat('cur_mem_mb_ts')
                        if isinstance(last_update, (int, float)):
                            age = int(time.time()) - int(last_update)
                            global_stat_stale = age > global_stat_max_age_sec
                            if global_stat_stale:
                                log.debug(f"Global memory stat is stale (age: {age}s > {global_stat_max_age_sec}s)")
                        else:
                            # No timestamp available - check if we're in single-process mode
                            global_stat_stale = fast_mode  # Only stale if we expect multi-process
                    except Exception:
                        # No timestamp available
                        global_stat_stale = fast_mode
            except Exception:
                pass
            
            # If global stats are stale or unavailable, don't trust them
            if global_stat_stale:
                global_mem_bytes = 0
            
            # Determine effective memory and limit based on aggregation availability
            if global_mem_bytes > 0:
                # Global aggregation working - use global memory vs global limit
                effective_mem = global_mem_bytes
                effective_limit = self.cache_mem_lim
            else:
                # Fallback: use local memory vs cache limit
                effective_mem = cur_mem
                effective_limit = self.cache_mem_lim
                if cur_mem > self.cache_mem_lim:
                    log.warning(
                        "Global memory stats unavailable/stale. "
                        f"Local RSS: {cur_mem // (1024**2)}MB, "
                        f"limit: "
                        f"{self.cache_mem_lim // (1024**2)}MB"
                    )

            # Hysteresis target: evict down to limit - headroom
            headroom = max(int(effective_limit * self.evict_hysteresis_frac), self.evict_headroom_min_bytes)
            target_bytes = max(0, int(effective_limit) - headroom)
            
            # Check if we need to evict based on tile count (prevents unbounded tile accumulation)
            tile_count = len(self.tiles)
            need_tile_count_eviction = tile_count > max_tile_count

            # Leader election: only one worker performs eviction when shared store is present
            need_mem_eviction = effective_mem > effective_limit
            if need_mem_eviction or need_tile_count_eviction:
                if not self._try_acquire_evict_leader():
                    time.sleep(poll_interval_fast)
                    continue
                rss_before_eviction = process.memory_info().rss

            # Evict if too many tiles (regardless of memory)
            if need_tile_count_eviction:
                target_tile_count = int(max_tile_count * 0.8)  # Evict down to 80% of max
                tiles_to_evict = tile_count - target_tile_count
                if tiles_to_evict > 0:
                    log.info(f"Tile count eviction: {tile_count} tiles, evicting {tiles_to_evict} to reach {target_tile_count}")
                    # _evict_batch handles its own locking internally
                    evicted = self._evict_batch(tiles_to_evict)
                    total_evicted += evicted

            # Evict while above target using adaptive batch sizing
            while self.tiles and effective_mem > target_bytes:
                over_bytes = max(0, effective_mem - target_bytes)
                ratio = min(1.0, over_bytes / max(1, effective_limit))
                adaptive = max(20, int(len(self.tiles) * min(0.10, ratio)))
                # _evict_batch handles its own locking internally
                evicted = self._evict_batch(adaptive)
                total_evicted += evicted
                if evicted == 0:
                    break

                # Recompute local RSS and, if available, the aggregated RSS
                # Use same staleness-aware logic as main loop
                cur_mem = process.memory_info().rss
                global_mem_bytes = 0
                try:
                    global_mem_mb = get_stat('cur_mem_mb')
                    if isinstance(global_mem_mb, (int, float)) and global_mem_mb > 0:
                        # Quick staleness check
                        try:
                            last_update = get_stat('cur_mem_mb_ts')
                            if isinstance(last_update, (int, float)):
                                age = int(time.time()) - int(last_update)
                                if age <= global_stat_max_age_sec:
                                    global_mem_bytes = int(global_mem_mb) * 1048576
                        except Exception:
                            pass
                except Exception:
                    pass
                
                # Update effective_mem and effective_limit consistently
                if global_mem_bytes > 0:
                    effective_mem = global_mem_bytes
                    effective_limit = self.cache_mem_lim
                else:
                    effective_mem = cur_mem
                    effective_limit = self.cache_mem_lim
                
                # Recalculate target with potentially new limit
                headroom = max(int(effective_limit * self.evict_hysteresis_frac), self.evict_headroom_min_bytes)
                target_bytes = max(0, int(effective_limit) - headroom)

                # Renew leadership and publish heartbeat after an eviction batch
                try:
                    self._renew_evict_leader()
                    update_process_memory_stat()
                except Exception:
                    pass

            if total_evicted > 0:
                _release_memory_to_os()
                rss_after_release = process.memory_info().rss
                log.info(
                    f"Eviction complete: evicted={total_evicted} tiles, "
                    f"RSS before={rss_before_eviction // (1024**2)}MB, "
                    f"RSS after release={rss_after_release // (1024**2)}MB, "
                    f"freed={max(0, rss_before_eviction - rss_after_release) // (1024**2)}MB"
                )

            if MEMTRACE:
                snapshot = tracemalloc.take_snapshot()
                top_stats = snapshot.statistics('lineno')

                log.info("[ Top 10 ]")
                for stat in top_stats[:10]:
                        log.info(stat)

            # Adaptive poll interval: check more frequently when
            # memory is above 70% of the limit so eviction can
            # react before the limit is significantly exceeded.
            # During active tile creation, memory can grow at
            # ~200-300 MB/s, so a 3-second sleep allows ~700 MB
            # of overshoot.  A 1-second sleep caps it to ~250 MB.
            mem_pressure = (
                effective_mem / max(1, effective_limit)
            )
            if mem_pressure > 0.7:
                poll_interval = poll_interval_fast
            else:
                poll_interval = poll_interval_normal
            time.sleep(poll_interval)

    def _get_tile(self, row, col, map_type, zoom):
        
        idx = self._to_tile_id(row, col, map_type, zoom)
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                tile = self._open_tile(row, col, map_type, zoom)
            else:
                # Touch LRU on hit
                self._touch_tile(idx, tile)
        return tile

    def _open_tile(self, row, col, map_type, zoom):
        if self.maptype_override and self.maptype_override != "Use tile default":
            map_type = self.maptype_override
        idx = self._to_tile_id(row, col, map_type, zoom)

        log.debug(f"Get_tile: {idx}")
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                self.misses += 1
                bump('tile_mem_miss')
                # Use target zoom level - supports both fixed and dynamic modes
                # Pass row/col for dynamic zoom computation based on predicted altitude
                tile = Tile(
                    col, row, map_type, zoom, 
                    cache_dir=self.cache_dir,
                    min_zoom=self.min_zoom,
                    max_zoom=self._get_target_zoom_level(zoom, row=row, col=col),
                )
                self.tiles[idx] = tile
                # New tile becomes MRU
                self._touch_tile(idx, tile)
                self.open_count[idx] = self.open_count.get(idx, 0) + 1
                if self.open_count[idx] > 1:
                    log.debug(f"Tile: {idx} opened for the {self.open_count[idx]} time.")
                # Limit open_count size to prevent unbounded memory growth
                while len(self.open_count) > self._open_count_max:
                    try:
                        self.open_count.popitem(last=False)  # Remove oldest entry
                    except KeyError:
                        break
            elif tile.refs <= 0:
                # Only in this case would this cache have made a difference
                self.hits += 1
                bump('tile_mem_hits')
                # Reset time budget when tile is re-opened from cache
                # This ensures returning to an area gets a fresh budget, not the
                # exhausted budget from a previous (possibly failed) request.
                tile._tile_time_budget = None

            tile.refs += 1
        return tile

    
    def _close_tile(self, row, col, map_type, zoom):
        tile_id = self._to_tile_id(row, col, map_type, zoom)
        with self.tc_lock:
            t = self.tiles.get(tile_id)
            if not t:
                log.warning(f"Attmpted to close unknown tile {tile_id}!")
                return False

            t.refs -= 1

            if self.enable_cache: # and not t.should_close():
                log.debug(f"Cache enabled.  Delay tile close for {tile_id}")
                return True

            if t.refs <= 0:
                log.debug(f"No more refs for {tile_id} closing...")
                t = self.tiles.pop(tile_id)
                t.close()
                t = None
                del(t)
            else:
                log.debug(f"Still have {t.refs} refs for {tile_id}")

        return True
    
    def is_tile_opened_by_xplane(self, row: int, col: int, map_type: str, zoom: int) -> bool:
        """
        Check if a tile is currently opened by X-Plane (has refs > 0).
        
        This is used by the prefetcher to skip tiles that X-Plane is already
        loading - the on-demand tile build logic will handle those.
        
        Returns:
            True if tile exists and has refs > 0 (being used by X-Plane)
            False if tile doesn't exist or has refs <= 0
        """
        tile_id = self._to_tile_id(row, col, map_type, zoom)
        with self.tc_lock:
            t = self.tiles.get(tile_id)
            if t and t.refs > 0:
                return True
        return False

# ============================================================
# Module-level cleanup helpers
# ============================================================

def shutdown():
    """Free network pools, worker threads and cached tiles to minimise RSS
    just before interpreter exit. Safe to call multiple times.
    
    Shutdown order:
    1. Spatial prefetcher (stop new prefetch requests)
    2. Predictive DDS (bundle consolidator, DDS builder, cache)
    3. ChunkGetter (background download threads)
    4. Cache writer executor
    5. Terrain indices
    6. TileCacher instances
    7. Stats batcher
    8. Process memory stats
    """
    global chunk_getter

    # 1. Stop spatial prefetcher first (stop new prefetch requests)
    try:
        stop_prefetcher()
        log.debug("Spatial prefetcher stopped")
    except Exception as _err:
        log.debug(f"Prefetcher stop error: {_err}")

    # 2. Stop predictive DDS (includes bundle consolidator, DDS builder)
    try:
        stop_predictive_dds()
        log.debug("Predictive DDS stopped")
    except Exception as _err:
        log.debug(f"Predictive DDS stop error: {_err}")

    # 3. Stop background download threads
    try:
        if chunk_getter is not None:
            chunk_getter.stop()
            chunk_getter = None
            log.debug("ChunkGetter stopped")
    except Exception as _err:
        log.debug(f"ChunkGetter stop error: {_err}")

    # 4. Shutdown cache writer executor
    try:
        shutdown_cache_writer()
        log.debug("Cache writer shutdown")
    except Exception as _err:
        log.debug(f"Cache writer shutdown error: {_err}")

    # 5. Clear terrain indices
    try:
        clear_terrain_indices()
        log.debug("Terrain indices cleared")
    except Exception as _err:
        log.debug(f"Clear terrain indices error: {_err}")

    # 6. Iterate over every TileCacher instance still alive and flush
    #    its caches.  We avoid importing autoortho_fuse to prevent cycles; instead
    #    we search the GC list.
    import gc
    for obj in gc.get_objects():
        try:
            if isinstance(obj, TileCacher):
                with obj.tc_lock:
                    for tile in list(obj.tiles.values()):
                        tile.close()
                    obj.tiles.clear()
        except Exception:
            # Ignore any edge-case failures during shutdown
            pass

    # 7. Stop stats batcher
    try:
        if stats_batcher:
            stats_batcher.stop()
            log.debug("Stats batcher stopped")
    except Exception:
        pass

    # 8. Clear process memory stats
    try:
        clear_process_memory_stat()
    except Exception:
        pass

    log.info("autoortho.getortho shutdown complete")

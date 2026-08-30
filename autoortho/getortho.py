#!/usr/bin/env python3
import logging
import atexit
import ctypes
import gc
import glob as glob_mod
import hashlib
import heapq
import itertools
import json
import errno
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
import queue
from queue import PriorityQueue, Empty, Full
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
    from autoortho.http2_broker import (
        HTTP2Broker,
        RequestTimeout as BrokerRequestTimeout,
        BrokerCancelledError,
        BrokerCapacityError,
        BrokerTimeoutError,
        BrokerShutdownError,
        BrokerError,
    )
except ImportError:
    try:
        from http2_broker import (
            HTTP2Broker,
            RequestTimeout as BrokerRequestTimeout,
            BrokerCancelledError,
            BrokerCapacityError,
            BrokerTimeoutError,
            BrokerShutdownError,
            BrokerError,
        )
    except ImportError:
        HTTP2Broker = None
        BrokerRequestTimeout = None

        class BrokerError(Exception):
            """Placeholder used when the broker package is unavailable."""

        class BrokerTimeoutError(BrokerError):
            """Placeholder used when the broker package is unavailable."""

        class BrokerCancelledError(BrokerError):
            """Placeholder used when the broker package is unavailable."""

        class BrokerCapacityError(BrokerError):
            """Placeholder used when the broker package is unavailable."""

        class BrokerShutdownError(BrokerError):
            """Placeholder used when the broker package is unavailable."""

try:
    from autoortho.diagnostics import (
        profiled_stage,
        profile_gauge,
        profile_span,
        record_stage,
    )
except ImportError:
    from diagnostics import profiled_stage, profile_gauge, profile_span, record_stage

try:
    from autoortho.aoimage import AoImage
except ImportError:
    from aoimage import AoImage

try:
    from autoortho.aoconfig import CFG, resolve_provider_setting
except ImportError:
    from aoconfig import CFG, resolve_provider_setting

try:
    from autoortho.aostats import STATS, StatTracker, StatsBatcher, get_stat, inc_many, inc_stat, set_stat, update_process_memory_stat, clear_process_memory_stat, update_decode_pool_stats, _get_macos_phys_footprint
except ImportError:
    from aostats import STATS, StatTracker, StatsBatcher, get_stat, inc_many, inc_stat, set_stat, update_process_memory_stat, clear_process_memory_stat, update_decode_pool_stats, _get_macos_phys_footprint

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
    from autoortho.utils.custom_map import get_custom_map_config
except ImportError:
    from utils.custom_map import get_custom_map_config

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

@profiled_stage("cache.jpeg_batch_read")
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
PRIORITY_BACKGROUND_DDS = PRIORITY_PREFETCH
PRIORITY_CACHE_REPAIR = 500
PRIORITY_NETWORK_HEALING = 600


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
                prefetch_workers = int(getattr(CFG.autoortho, 'background_builder_workers', 4))
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
                pool_size=pool_size,
                queue_enabled=_is_tile_queue_enabled(),
                max_waiters=_get_tile_queue_max_size(),
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
                      missing_color: tuple,
                      buffer_priority: int = PRIORITY_LIVE) -> bytes:
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
            # No fallback to allocation - wait for buffer to be available.
            buffer, buffer_id = pool.acquire(timeout=30.0, priority=buffer_priority)
        except TimeoutError:
            log.debug("Hybrid DDS build: buffer pool timeout (queue full)")
            bump('hybrid_buffer_pool_timeout')
            return None
        
        try:
            with _native_build_context() as threads:
                result = native.build_from_jpegs_to_buffer(
                    buffer,
                    jpeg_datas,
                    format=dxt_format,
                    missing_color=missing_color,
                    max_threads=threads
                )

            if result.success and result.bytes_written >= 128:
                dds_bytes = result.to_bytes()
                if log.isEnabledFor(logging.DEBUG):
                    active, total_threads = _native_thread_load()
                    log.debug(f"Hybrid DDS build: {valid_count}/{len(chunks)} chunks, "
                              f"{len(dds_bytes)} bytes, {threads} threads "
                              f"({active} concurrent, {total_threads}/{CURRENT_CPU_COUNT} threads in flight)")
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
            if log.isEnabledFor(logging.DEBUG):
                active, total_threads = _native_thread_load()
                log.debug(f"Native DDS build: {result.chunks_decoded}/{result.chunks_found} chunks, "
                          f"{result.mipmaps} mipmaps, {result.elapsed_ms:.1f}ms "
                          f"({active} concurrent, {total_threads}/{CURRENT_CPU_COUNT} threads in flight)")
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


_http2_client = None
_http2_client_lock = threading.Lock()
_http2_client_failed = False


def _get_http2_client():
    global _http2_client, _http2_client_failed
    if _http2_client is not None:
        return _http2_client
    if _http2_client_failed or HTTP2Broker is None:
        return None
    address = os.getenv("AO_HTTP2_BROKER_ADDR")
    token = os.getenv("AO_HTTP2_BROKER_TOKEN")
    if not address or not token:
        return None
    with _http2_client_lock:
        if _http2_client is not None:
            return _http2_client
        try:
            _http2_client = HTTP2Broker.connect(
                address,
                token,
                max_pending=resolve_provider_setting("provider_max_in_flight"),
                queue_timeout=resolve_provider_setting(
                    "provider_queue_timeout"
                ),
            )
        except Exception as exc:
            _http2_client_failed = True
            log.error("Could not connect to shared HTTP/2 broker: %s", exc)
            return None
        return _http2_client


def _close_http2_client():
    global _http2_client
    with _http2_client_lock:
        client, _http2_client = _http2_client, None
    if client is not None:
        client.stop()


def _reset_http2_client(failed_client=None):
    """Discard a dead attached client so the next request can reconnect."""
    global _http2_client, _http2_client_failed
    with _http2_client_lock:
        if failed_client is not None and _http2_client is not failed_client:
            return
        client, _http2_client = _http2_client, None
        _http2_client_failed = False
    if client is not None:
        try:
            client.stop()
        except Exception:
            log.debug("Failed closing dead HTTP/2 broker client", exc_info=True)


def _broker_env_configured():
    """True when this process is meant to download through the shared broker.

    Checked without connecting: it stays true while the client is being
    (re)established, which is what the worker-pool sizing needs to know.
    """

    if HTTP2Broker is None:
        return False
    return bool(
        os.getenv("AO_HTTP2_BROKER_ADDR") and os.getenv("AO_HTTP2_BROKER_TOKEN")
    )


def _broker_timeout(timeout):
    """Convert a requests-style timeout into a broker RequestTimeout."""

    if isinstance(timeout, tuple):
        connect_timeout = float(timeout[0])
        read_timeout = float(timeout[1])
    else:
        connect_timeout = read_timeout = float(timeout)
    return BrokerRequestTimeout(
        connect=connect_timeout,
        read=read_timeout,
        write=connect_timeout,
        pool=connect_timeout,
    )


def _http_span_details(chunk):
    return {
        "maptype": getattr(chunk, "maptype", None),
        "zoom": getattr(chunk, "zoom", None),
        "attempt": getattr(chunk, "attempt", None),
        "prefetch": bool(getattr(chunk, "prefetch", False)),
    }


def _profiled_http_get(session, url, headers, timeout, chunk):
    tile_id = getattr(chunk, "tile_id", None) or getattr(chunk, "chunk_id", None)
    with profile_span(
        "network.http_request",
        tile_id=tile_id,
        details=_http_span_details(chunk),
    ) as span:
        broker = _get_http2_client()
        if broker is not None:
            request_id = uuid.uuid4().hex
            chunk._broker_request_id = request_id
            try:
                response = broker.get(
                    url,
                    headers=headers,
                    priority=int(getattr(chunk, "priority", 0)),
                    timeout=_broker_timeout(timeout),
                    request_id=request_id,
                )
            except BrokerCancelledError:
                # Cancellation is an intentional outcome, not a transport
                # failure: keep the typed error so callers skip retry/backoff.
                raise
            except BrokerTimeoutError as exc:
                raise requests.exceptions.Timeout(str(exc)) from exc
            except BrokerError as exc:
                raise requests.exceptions.ConnectionError(str(exc)) from exc
            finally:
                chunk._broker_request_id = None
        else:
            response = session.get(url, headers=headers, timeout=timeout)
        span.annotate(
            status_code=response.status_code,
            response_bytes=len(response.content or b""),
        )
        if response.status_code >= 400:
            span.outcome = "failed"
        return response


# JPEG decode concurrency: auto-tuned for optimal performance
# Decode is memory-bound, not CPU-bound, so we can safely exceed CPU count
# Each decode uses ~256KB RAM, so even 64 concurrent = only ~16MB
_MAX_DECODE = min(CURRENT_CPU_COUNT * 4, 64)
_decode_sem = threading.Semaphore(_MAX_DECODE)

# Persistent thread pool for progressive tile building (avoids per-tile executor overhead)
_progressive_executor = None
_progressive_executor_lock = threading.Lock()

# Track concurrent native builds for OpenMP thread budget coordination.
# Shared across background builder, live builder, and streaming builder.
# `_active_native_thread_count` is the SUM of per-build OpenMP thread budgets
# currently in flight; comparing it against CURRENT_CPU_COUNT measures actual
# CPU oversubscription (peak captured separately for periodic reporting).
_active_native_builds = 0
_active_native_thread_count = 0
_peak_native_thread_count = 0
_active_native_builds_lock = threading.Lock()


def _native_thread_load() -> tuple:
    """Snapshot of (active_builds, total_threads_in_flight) under the lock."""
    with _active_native_builds_lock:
        return (_active_native_builds, _active_native_thread_count)


def native_thread_load_peak_and_reset() -> tuple:
    """Return ``(active_builds, total_threads, peak_threads)`` and reset peak.

    Intended for periodic reporting (e.g. stats loop). Resetting on read gives
    a windowed peak between calls.
    """
    global _peak_native_thread_count
    with _active_native_builds_lock:
        snap = (_active_native_builds, _active_native_thread_count, _peak_native_thread_count)
        _peak_native_thread_count = _active_native_thread_count
    return snap

# Track live (FUSE-requested) tile reads in progress.
# When threshold exceeded, prefetching pauses to give network resources
# to live requests. Background DDS building does NOT pause — instrumentation
# (native_semaphore_wait_ms_live) confirmed zero contention; gating the
# builder leaves it idle for the entire flight (the symptom in the bug logs:
# `BackgroundDDSBuilder: 0 tiles built`).
_live_reads_in_progress = 0
_live_reads_lock = threading.Lock()
try:
    _live_tile_admission_limit = max(
        1,
        min(
            128,
            int(getattr(CFG.autoortho, "live_tile_admission", 16)),
        ),
    )
except (TypeError, ValueError):
    _live_tile_admission_limit = 16
_live_tile_admission = threading.BoundedSemaphore(
    _live_tile_admission_limit
)

# Bounded repair queue for partial DDS entries that are missing mipmap 0.
_partial_mm0_promotions = OrderedDict()
_partial_mm0_promotions_lock = threading.Lock()
_read_ahead_executor = None
_read_ahead_executor_lock = threading.Lock()
_read_ahead_capacity = threading.BoundedSemaphore(2)

_shutdown_requested = threading.Event()


def begin_shutdown(reason="shutdown"):
    """Signal shutdown and unblock queued/waiting chunk work immediately."""
    _shutdown_requested.set()
    try:
        chunk_getter.cancel_all_work(reason)
    except Exception:
        pass


def clear_shutdown_request():
    """Clear shutdown state for a subsequent run in the same process."""
    _shutdown_requested.clear()


def is_shutdown_requested() -> bool:
    return _shutdown_requested.is_set()


def _live_read_start():
    """Signal that a live tile read has started."""
    global _live_reads_in_progress
    with _live_reads_lock:
        _live_reads_in_progress += 1


def _live_read_end():
    """Signal that a live tile read has finished."""
    global _live_reads_in_progress
    with _live_reads_lock:
        _live_reads_in_progress -= 1


def acquire_live_tile_slot(timeout: float) -> bool:
    """Bound complete live tile work before expensive tile state is created."""
    started = time.monotonic()
    acquired = _live_tile_admission.acquire(timeout=max(0.0, timeout))
    record_stage(
        "tile.admission_wait",
        (time.monotonic() - started) * 1000.0,
        outcome="ok" if acquired else "timeout",
        details={"limit": _live_tile_admission_limit},
    )
    return acquired


def release_live_tile_slot() -> None:
    try:
        _live_tile_admission.release()
    except ValueError:
        log.error("Live tile admission released without a matching acquire")


def is_live_building() -> bool:
    """Return True if live read pressure is high enough to pause prefetching."""
    with _live_reads_lock:
        return _live_reads_in_progress >= 6


_shared_flight_state_lock = threading.Lock()
_shared_flight_state_last_poll = 0.0
_shared_flight_state_allowed = False
_shared_flight_state_timestamp = 0.0


def _sync_shared_flight_state() -> bool:
    """Refresh the worker-local tracker from the parent stats snapshot."""
    global _shared_flight_state_last_poll
    global _shared_flight_state_allowed
    global _shared_flight_state_timestamp

    if (
        getattr(datareftracker, "running", False)
        and
        getattr(datareftracker, "has_ever_connected", False)
        and getattr(datareftracker, "connected", False)
        and getattr(datareftracker, "data_valid", False)
    ):
        return True

    now = time.monotonic()
    with _shared_flight_state_lock:
        if now - _shared_flight_state_last_poll < 0.5:
            return _shared_flight_state_allowed
        _shared_flight_state_last_poll = now
        state = get_stat("flight_state")
        if not isinstance(state, dict):
            _shared_flight_state_allowed = False
            return False
        try:
            state_timestamp = float(state.get("timestamp", 0.0))
            fresh = time.time() - state_timestamp <= 3.0
            connected = bool(state.get("connected")) and fresh
            data_valid = bool(state.get("data_valid")) and fresh
            has_connected = bool(state.get("has_ever_connected"))
            with datareftracker._lock:
                datareftracker.has_ever_connected = (
                    datareftracker.has_ever_connected or has_connected
                )
                datareftracker.connected = connected
                datareftracker.data_valid = data_valid
                if connected and data_valid:
                    for name in (
                        "lat",
                        "lon",
                        "alt",
                        "hdg",
                        "spd",
                        "local_time_sec",
                        "pressure_alt",
                        "sun_pitch",
                    ):
                        setattr(datareftracker, name, float(state[name]))
            if (
                connected
                and data_valid
                and state_timestamp > _shared_flight_state_timestamp
            ):
                datareftracker.flight_averager.add_sample(
                    lat=float(state["lat"]),
                    lon=float(state["lon"]),
                    alt_ft=float(state["alt"]) * 3.28084,
                    hdg=float(state["hdg"]),
                    spd=float(state["spd"]),
                )
                _shared_flight_state_timestamp = state_timestamp
            _shared_flight_state_allowed = bool(
                connected and data_valid and has_connected
            )
        except (KeyError, TypeError, ValueError):
            _shared_flight_state_allowed = False
        return _shared_flight_state_allowed


def is_prefetch_runtime_allowed() -> bool:
    """Allow speculative work only after X-Plane has entered the flight."""
    return bool(_sync_shared_flight_state() and not is_live_building())


def _thread_budget_for(active: int) -> int:
    """Per-build OpenMP thread count given the number of active builds (including self).

    Divides CPU cores across active concurrent builds so total threads ~ cpu_count.
    Returns at least 2 to ensure each build makes progress.
    If native_pipeline_threads config is > 0, uses that as a fixed override.
    """
    try:
        explicit = int(getattr(CFG.autoortho, 'native_pipeline_threads', 0))
        if explicit > 0:
            return explicit
    except (ValueError, TypeError, AttributeError):
        pass

    return max(2, CURRENT_CPU_COUNT // max(1, active))


class _BackgroundBuildDeferred(Exception):
    """Raised when speculative DDS work should yield to live X-Plane requests."""


class _native_build_context:
    """``__enter__`` returns the per-build OpenMP thread budget under the same lock
    that increments the active-build counter, so each build sees itself in the count."""
    def __enter__(self):
        global _active_native_builds, _active_native_thread_count, _peak_native_thread_count
        with _active_native_builds_lock:
            _active_native_builds += 1
            self._threads = _thread_budget_for(_active_native_builds)
            _active_native_thread_count += self._threads
            if _active_native_thread_count > _peak_native_thread_count:
                _peak_native_thread_count = _active_native_thread_count
        return self._threads

    def __exit__(self, *exc):
        global _active_native_builds, _active_native_thread_count
        with _active_native_builds_lock:
            _active_native_builds -= 1
            _active_native_thread_count -= self._threads
        return False


def _defer_background_build_if_live(tile=None):
    if is_live_building() and not getattr(tile, '_is_live', False):
        raise _BackgroundBuildDeferred("live read active")


def _get_progressive_executor(max_workers=None):
    """Get or create the shared progressive tile executor.

    Pool size is based on system capabilities (CPU count and decode limit),
    not on the first caller's chunk count. The max_workers parameter is
    accepted for API compatibility but only used as a fallback.
    """
    global _progressive_executor, _read_ahead_executor
    if _progressive_executor is None:
        with _progressive_executor_lock:
            if _progressive_executor is None:
                workers = min(CURRENT_CPU_COUNT, _MAX_DECODE)
                _progressive_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="ao-progressive"
                )
    return _progressive_executor


def _get_read_ahead_executor():
    global _read_ahead_executor
    if _read_ahead_executor is None:
        with _read_ahead_executor_lock:
            if _read_ahead_executor is None:
                _read_ahead_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=2,
                    thread_name_prefix="ao-row-ahead",
                )
    return _read_ahead_executor


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


def _get_bool_config(section, name: str, default: bool) -> bool:
    """Read a boolean config value that may be stored as a string."""
    value = getattr(section, name, default)
    if isinstance(value, str):
        return value.lower().strip() in ('true', '1', 'yes', 'on')
    return bool(value)


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


class _AsyncDownload(object):
    """Bookkeeping for one chunk download owned by :class:`_BrokerDownloadStage`."""

    __slots__ = ("obj", "args", "kwargs", "idx", "request", "request_id",
                 "started_at")

    def __init__(self, obj, args, kwargs, idx, request):
        self.obj = obj
        self.args = args
        self.kwargs = kwargs
        self.idx = idx
        self.request = request
        self.request_id = None
        self.started_at = 0.0


class _DeferredDispatch(object):
    """A chunk accepted by the stage but still waiting for an admission slot.

    Holds no network state at all: `begin_network_attempt` has not run yet, so
    a deferred chunk has not consumed one of its retry attempts and can be
    handed back to the work queue unchanged.
    """

    __slots__ = ("obj", "args", "kwargs", "idx")

    def __init__(self, obj, args, kwargs, idx):
        self.obj = obj
        self.args = args
        self.kwargs = kwargs
        self.idx = idx


class _BrokerDownloadStage(object):
    """Bounded asynchronous network stage backed by the shared HTTP/2 broker.

    Worker threads hand a chunk to :meth:`dispatch` and go straight back to the
    queue, so the number of outstanding provider requests is bounded by the
    broker's pending budget instead of by the size of the downloader pool.

    Three cooperating pieces, none of which ever sleeps on a backoff:

    * the broker dispatcher thread settles futures and only pushes them onto
      ``_completions`` (it must never run chunk logic inline),
    * a small pool of completion threads applies responses to chunks,
    * one scheduler thread owns every timed action (pre-request backoff, retry
      backoff, capacity re-admission).

    Admission mirrors the broker: background/prefetch work may only use
    ``max_outstanding - reserved_live`` slots, so prefetch can never starve
    live requests.  A chunk that finds the stage full is **not** handed back
    to the worker for a synchronous download -- that would silently exceed
    ``provider_max_in_flight`` and burn the reserved live capacity.  It is
    parked in a bounded, priority-ordered deferred queue instead and admitted
    by :meth:`_release` as soon as a slot frees.  Only when that queue is also
    full does the calling worker block (bounded by
    ``_BACKPRESSURE_TIMEOUT``); if capacity still has not appeared the chunk is
    quietly requeued, which throttles the feeders without ever bypassing the
    admission bound.

    :meth:`dispatch` therefore returns False only when this stage cannot serve
    the request at all (no broker, shutting down, or a legacy call signature).
    """

    _CAPACITY_RETRY_DELAY = 0.05
    # How long a worker thread may block when even the deferred queue is full.
    _BACKPRESSURE_TIMEOUT = 0.25
    _BACKPRESSURE_POLL = 0.05

    def __init__(self, getter, max_outstanding, workers, live_priority_threshold=PRIORITY_PREFETCH,
                 max_deferred=None):
        self._getter = getter
        self._max_outstanding = max(1, int(max_outstanding))
        self._reserved_live = max(1, self._max_outstanding // 4)
        self._live_priority_threshold = live_priority_threshold
        self._lock = threading.Lock()
        self._admit_cv = threading.Condition(self._lock)
        self._outstanding = 0
        self._background_outstanding = 0
        self._peak_outstanding = 0
        self._active = {}
        self._stopping = threading.Event()

        # Deferred admission queue: bounded so a stalled provider cannot grow
        # the backlog without limit, ordered so live work overtakes prefetch.
        if max_deferred is None:
            max_deferred = min(4096, max(64, 4 * self._max_outstanding))
        self._max_deferred = max(1, int(max_deferred))
        self._deferred = []
        self._deferred_seq = itertools.count()
        self._drain_scheduled = False

        self._completions = queue.Queue()
        self._sched_cv = threading.Condition()
        self._sched_heap = []
        self._sched_seq = itertools.count()

        self._threads = []
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            name="ao-dl-sched",
            daemon=True,
        )
        self._scheduler.start()
        self._threads.append(self._scheduler)
        for i in range(max(1, int(workers))):
            t = threading.Thread(
                target=self._completion_loop,
                name=f"ao-dl-complete-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    # -- admission ---------------------------------------------------------

    def _is_background(self, obj):
        if getattr(obj, "prefetch", False):
            return True
        return int(getattr(obj, "priority", 0)) >= self._live_priority_threshold

    def _try_admit(self, obj):
        background = self._is_background(obj)
        with self._lock:
            return self._try_admit_locked(background)

    def _try_admit_locked(self, background):
        if self._stopping.is_set():
            return False
        limit = self._max_outstanding
        if background:
            limit -= self._reserved_live
        if self._outstanding >= limit:
            bump(
                "download_stage_full_background"
                if background
                else "download_stage_full_live"
            )
            return False
        self._outstanding += 1
        if self._outstanding > self._peak_outstanding:
            self._peak_outstanding = self._outstanding
        if background:
            self._background_outstanding += 1
        return True

    def _release(self, obj):
        background = self._is_background(obj)
        with self._admit_cv:
            self._outstanding = max(0, self._outstanding - 1)
            if background:
                self._background_outstanding = max(
                    0, self._background_outstanding - 1
                )
            has_deferred = bool(self._deferred)
            self._admit_cv.notify()
        if has_deferred:
            self._request_drain()

    def outstanding(self):
        with self._lock:
            return self._outstanding

    def peak_outstanding(self):
        with self._lock:
            return self._peak_outstanding

    def deferred_depth(self):
        with self._lock:
            return len(self._deferred)

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, obj, args, kwargs, idx):
        """Take ownership of ``obj``'s download, or return False.

        Returning True means this stage is responsible for eventually calling
        ``Getter._finalize_attempt`` for the chunk (possibly after deferring
        or requeueing it); the worker must not touch it again.  False is
        returned only when the stage cannot serve this request at all, never
        merely because it is at capacity.
        """

        if self._stopping.is_set():
            return False
        if args:
            # Unknown positional arguments belong to the legacy sync path.
            return False
        if set(kwargs) - {"timeout", "max_attempts"}:
            return False
        if _get_http2_client() is None:
            return False
        if self._try_admit(obj):
            self._start(obj, args, kwargs, idx)
            return True
        return self._defer(_DeferredDispatch(obj, args, kwargs, idx))

    def _defer(self, pending):
        """Park a chunk until a slot frees, applying backpressure if needed."""

        deadline = time.monotonic() + self._BACKPRESSURE_TIMEOUT
        pending_is_background = self._is_background(pending.obj)
        while True:
            stopping = False
            evicted_background = None
            with self._admit_cv:
                if self._stopping.is_set():
                    stopping = True
                    queued = False
                    expired = False
                elif len(self._deferred) < self._max_deferred:
                    heapq.heappush(
                        self._deferred,
                        (
                            int(getattr(pending.obj, "priority", 0)),
                            next(self._deferred_seq),
                            pending,
                        ),
                    )
                    bump("download_stage_deferred")
                    queued = True
                elif not pending_is_background:
                    background_entries = [
                        (index, item)
                        for index, item in enumerate(self._deferred)
                        if self._is_background(item[2].obj)
                    ]
                    if background_entries:
                        evict_index, evicted_item = max(
                            background_entries,
                            key=lambda pair: pair[1][0],
                        )
                        evicted_background = evicted_item[2]
                        self._deferred.pop(evict_index)
                        heapq.heapify(self._deferred)
                        heapq.heappush(
                            self._deferred,
                            (
                                int(getattr(pending.obj, "priority", 0)),
                                next(self._deferred_seq),
                                pending,
                            ),
                        )
                        bump("download_stage_live_displaced_prefetch")
                        queued = True
                    else:
                        queued = False
                        expired = time.monotonic() >= deadline
                        if not expired:
                            self._admit_cv.wait(self._BACKPRESSURE_POLL)
                else:
                    # Deferred queue is full too: hold this worker briefly so
                    # the feeders slow down instead of the bound being ignored.
                    queued = False
                    bump("download_stage_backpressure")
                    expired = time.monotonic() >= deadline
                    if not expired:
                        self._admit_cv.wait(self._BACKPRESSURE_POLL)
            if stopping:
                # Shutdown raced this dispatch.  Retire the chunk instead of
                # letting the worker open a synchronous connection while the
                # process is tearing down.
                self._retire_deferred(pending)
                return True
            if evicted_background is not None:
                self._requeue_deferred(evicted_background)
            if queued:
                # A slot may have been freed between the failed admission and
                # the push above, so make sure someone looks at the queue.
                self._request_drain()
                return True
            if expired:
                self._requeue_deferred(pending)
                return True
            if _get_http2_client() is None:
                # The broker went away: the legacy synchronous path is now the
                # only way this chunk can still be fetched.
                return False

    def _start(self, obj, args, kwargs, idx):
        """Begin one network attempt for an already-admitted chunk."""

        try:
            prepared = obj.begin_network_attempt(
                idx=idx,
                timeout=kwargs.get("timeout"),
                max_attempts=kwargs.get("max_attempts"),
            )
        except Exception as err:
            self._release(obj)
            self._getter._settle_attempt(obj, args, kwargs, False, err)
            return

        if isinstance(prepared, _AttemptOutcome):
            self._release(obj)
            if prepared.resolved:
                self._record_chunk_resolve(obj)
            self._getter._settle_attempt(obj, args, kwargs, prepared.resolved)
            return

        state = _AsyncDownload(obj, args, kwargs, idx, prepared)
        try:
            if prepared.delay > 0:
                delay, prepared.delay = prepared.delay, 0.0
                self._schedule(delay, self._send, state)
            else:
                self._send(state)
        except Exception as err:
            self._release(obj)
            self._getter._settle_attempt(
                obj,
                args,
                kwargs,
                False,
                err,
            )

    # -- deferred admission ------------------------------------------------

    def _request_drain(self):
        """Ask the scheduler thread to admit whatever deferred work fits.

        Draining happens on the single scheduler thread so completion threads
        never recurse into `_start`, and a flag keeps at most one drain
        callback queued -- the scheduler heap cannot accumulate one entry per
        completed download.
        """

        with self._lock:
            if self._drain_scheduled or not self._deferred:
                return
            self._drain_scheduled = True
        self._schedule(0.0, self._drain_deferred)

    def _drain_deferred(self):
        while True:
            with self._admit_cv:
                self._drain_scheduled = False
                pending = self._take_admissible_locked()
                if pending is None:
                    return
                self._admit_cv.notify()
            if self._stopping.is_set() or getattr(pending.obj, "cancelled", False):
                self._release(pending.obj)
                self._retire_deferred(pending)
                continue
            self._start(pending.obj, pending.args, pending.kwargs, pending.idx)

    def _take_admissible_locked(self):
        """Pop and admit the highest-priority deferred chunk that fits.

        The caller must hold ``_admit_cv``.  When the head of the queue is
        background work that no longer fits its (smaller) budget, the queue is
        scanned once for live work: the reserved live slots must stay usable
        even while prefetch saturates the stage.
        """

        if not self._deferred:
            return None
        head_background = self._is_background(self._deferred[0][2].obj)
        if self._try_admit_locked(head_background):
            return heapq.heappop(self._deferred)[2]
        if not head_background:
            return None
        for index, item in enumerate(self._deferred):
            if self._is_background(item[2].obj):
                continue
            if not self._try_admit_locked(False):
                return None
            self._deferred.pop(index)
            heapq.heapify(self._deferred)
            return item[2]
        return None

    def _requeue_deferred(self, pending):
        """Hand a still-unstarted chunk back to the work queue, quietly."""

        bump("download_stage_requeued")
        self._getter._settle_attempt(
            pending.obj, pending.args, pending.kwargs, False, quiet=True
        )

    def _retire_deferred(self, pending):
        """Settle a deferred chunk that will never run (shutdown/cancel)."""

        abandon = getattr(self._getter, "_abandon_duplicate_waiters", None)
        if abandon is not None:
            abandon(pending.obj)
        else:
            cancel = getattr(pending.obj, "cancel", None)
            if cancel is not None:
                cancel()
        self._getter._settle_attempt(
            pending.obj, pending.args, pending.kwargs, False, quiet=True
        )

    def _send(self, state):
        broker = _get_http2_client()
        if broker is None:
            self._finish(state, _AttemptOutcome(requeue_delay=0.0))
            return
        if self._stopping.is_set() or getattr(state.obj, "cancelled", False):
            self._finish(state, _AttemptOutcome(resolved=True))
            return

        request = state.request
        request_id = uuid.uuid4().hex
        state.request_id = request_id
        state.obj._broker_request_id = request_id
        state.started_at = time.monotonic()
        try:
            future = broker.submit_async(
                request.url,
                headers=request.headers,
                priority=int(getattr(state.obj, "priority", 0)),
                timeout=_broker_timeout(request.timeout),
                request_id=request_id,
            )
        except BrokerCapacityError:
            # Another client filled the shared pending budget; re-offer soon
            # instead of failing the chunk. Our own outstanding counter caps
            # how many states can ever be waiting here.
            state.obj._broker_request_id = None
            bump("download_broker_capacity_deferred")
            self._schedule(self._CAPACITY_RETRY_DELAY, self._send, state)
            return
        except BrokerError as err:
            state.obj._broker_request_id = None
            self._finish(state, state.obj.finish_network_attempt(request, None, err))
            return

        with self._lock:
            self._active[request_id] = state
        future.add_done_callback(self._on_future_done)

    def _on_future_done(self, future):
        # Runs on the broker dispatcher thread: never do chunk work here.
        with self._lock:
            state = self._active.pop(future.request_id, None)
        if state is None:
            return
        self._completions.put((state, future))

    # -- completion --------------------------------------------------------

    def _completion_loop(self):
        while True:
            item = self._completions.get()
            try:
                if item is None:
                    return
                state, future = item
                self._apply_result(state, future)
            except Exception:
                log.exception("Unhandled error in download completion worker")
            finally:
                self._completions.task_done()

    def _apply_result(self, state, future):
        obj = state.obj
        obj._broker_request_id = None
        error = future.exception()
        response = None if error is not None else future.result()

        duration_ms = (time.monotonic() - state.started_at) * 1000.0
        details = _http_span_details(obj)
        if error is None:
            details["status_code"] = response.status_code
            details["response_bytes"] = len(response.content or b"")
            outcome_label = "ok" if response.status_code < 400 else "failed"
        else:
            outcome_label = (
                "cancelled"
                if isinstance(error, BrokerCancelledError)
                else "failed"
            )
        record_stage(
            "network.http_request",
            duration_ms,
            tile_id=getattr(obj, "tile_id", None) or getattr(obj, "chunk_id", None),
            outcome=outcome_label,
            details=details,
        )

        try:
            try:
                outcome = obj.finish_network_attempt(
                    state.request,
                    response,
                    error,
                )
            finally:
                if response is not None:
                    response.close()
        except Exception as err:
            self._release(obj)
            self._getter._settle_attempt(
                obj,
                state.args,
                state.kwargs,
                False,
                err,
            )
            return

        if outcome.retry_request is not None:
            state.request = outcome.retry_request
            try:
                self._send(state)
            except Exception as err:
                self._release(obj)
                self._getter._settle_attempt(
                    obj,
                    state.args,
                    state.kwargs,
                    False,
                    err,
                )
            return
        self._finish(state, outcome)

    def _finish(self, state, outcome):
        if outcome.requeue_delay > 0:
            # Backoff must not consume network admission while no request is
            # active. Resubmission will pass through normal priority admission.
            self._release(state.obj)
            if self._stopping.is_set():
                state.obj.cancel()
                self._getter._settle_attempt(
                    state.obj,
                    state.args,
                    state.kwargs,
                    True,
                    quiet=True,
                )
                return
            self._schedule(
                outcome.requeue_delay,
                self._getter._settle_attempt,
                state.obj,
                state.args,
                state.kwargs,
                outcome.resolved,
                None,
                True,
            )
            return
        self._release(state.obj)
        state.obj._broker_request_id = None
        if outcome.resolved:
            self._record_chunk_resolve(state.obj)
        self._getter._settle_attempt(
            state.obj, state.args, state.kwargs, outcome.resolved
        )

    def _record_chunk_resolve(self, obj):
        started = getattr(obj, "starttime", None)
        if not started:
            return
        record_stage(
            "chunk.resolve",
            max(0.0, time.time() - started) * 1000.0,
            tile_id=getattr(obj, "tile_id", None)
            or getattr(obj, "chunk_id", None),
            outcome=(
                "cancelled"
                if getattr(obj, "cancelled", False)
                else "failed"
                if getattr(obj, "permanent_failure", False)
                else "ok"
            ),
            details={
                "attempts": getattr(obj, "attempt", 0),
                "prefetch": bool(getattr(obj, "prefetch", False)),
            },
        )

    # -- scheduler ---------------------------------------------------------

    def _schedule(self, delay, fn, *args):
        when = time.monotonic() + max(0.0, float(delay))
        with self._sched_cv:
            heapq.heappush(
                self._sched_heap, (when, next(self._sched_seq), fn, args)
            )
            self._sched_cv.notify()

    def _scheduler_loop(self):
        while True:
            with self._sched_cv:
                while True:
                    if self._stopping.is_set() and not self._sched_heap:
                        return
                    if not self._sched_heap:
                        self._sched_cv.wait(0.25)
                        continue
                    when = self._sched_heap[0][0]
                    remaining = when - time.monotonic()
                    if remaining <= 0:
                        _, _, fn, args = heapq.heappop(self._sched_heap)
                        break
                    self._sched_cv.wait(min(remaining, 0.25))
            try:
                fn(*args)
            except Exception:
                log.exception("Unhandled error in download scheduler")

    # -- lifecycle ---------------------------------------------------------

    def stop(self, timeout=5.0):
        self._stopping.set()
        broker = _get_http2_client()
        with self._admit_cv:
            active = list(self._active.keys())
            deferred = [item[2] for item in self._deferred]
            self._deferred = []
            self._admit_cv.notify_all()
        if broker is not None:
            for request_id in active:
                try:
                    broker.cancel(request_id)
                except BrokerError:
                    log.debug("Broker cancel failed during stage shutdown",
                              exc_info=True)
        # Deferred chunks never started an attempt, so nothing else will ever
        # finalize them: retire them here or their waiters block forever.
        for pending in deferred:
            try:
                self._retire_deferred(pending)
            except Exception:
                log.exception("Failed to retire deferred chunk during shutdown")
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                active_count = len(self._active)
            if active_count == 0 and self._completions.unfinished_tasks == 0:
                break
            time.sleep(0.01)
        with self._sched_cv:
            self._sched_cv.notify_all()
        for _ in range(len(self._threads) - 1):
            self._completions.put(None)
        for t in self._threads:
            t.join(timeout=timeout)
            if t.is_alive():
                log.warning("Download stage thread %s did not stop in time", t.name)
        with self._lock:
            remaining_active = list(self._active.values())
            self._active.clear()
        for state in remaining_active:
            self._release(state.obj)
            state.obj.cancel()
            self._getter._settle_attempt(
                state.obj,
                state.args,
                state.kwargs,
                True,
                quiet=True,
            )

    def cancel_deferred(self, prefetch_only=True):
        """Remove deferred work before it reaches provider admission."""
        removed = []
        with self._admit_cv:
            kept = []
            for item in self._deferred:
                pending = item[2]
                if (not prefetch_only) or self._is_background(pending.obj):
                    removed.append(pending)
                else:
                    kept.append(item)
            self._deferred = kept
            heapq.heapify(self._deferred)
            self._admit_cv.notify_all()
        for pending in removed:
            self._retire_deferred(pending)
        return len(removed)


class Getter(object):
    queue = None
    workers = None
    WORKING = None
    session = None

    def __init__(self, num_workers):
        
        self.count = 0
        # Live work is never capacity-rejected. Prefetch has a separate,
        # bounded lane and cannot occupy or block live admission.
        self.live_queue = PriorityQueue()
        self.prefetch_queue = PriorityQueue(
            maxsize=max(
                32,
                min(
                    4096,
                    int(
                        getattr(
                            CFG.autoortho, "prefetch_max_chunks", 512
                        )
                    ),
                ),
            )
        )
        # Compatibility alias for diagnostics that inspect the live lane.
        self.queue = self.live_queue
        self.workers = []
        self.WORKING = threading.Event()
        self.WORKING.set()
        self.localdata = threading.local()
        self._inflight_objs = set()
        self._inflight_objs_lock = threading.Lock()
        # Broker-backed downloads are dispatched here instead of blocking a
        # worker thread for the whole request/response round trip.
        self._async_stage = self._create_async_stage()
        # Thread-local sessions created in worker() to avoid shared-state contention

        for i in range(num_workers):
            t = threading.Thread(target=self.worker, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)

        #self.stat_t = t = threading.Thread(target=self.show_stats, daemon=True)
        #self.stat_t.start()

    def _create_async_stage(self):
        """Subclasses return a `_BrokerDownloadStage`, or None to stay sync."""
        return None

    def stop(self):
        self.WORKING.clear()

        stage, self._async_stage = self._async_stage, None
        if stage is not None:
            stage.stop()

        for work_queue in (self.live_queue, self.prefetch_queue):
            try:
                while True:
                    work_queue.get_nowait()
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
        
        # A session is retained as an explicit HTTP/1.1 fallback. Each worker
        # issues at most one request at a time, so a huge per-thread pool only
        # wastes metadata and fragments connection reuse.
        try:
            self.localdata.session = create_http_session(pool_size=2)
        except Exception as _e:
            log.warning(f"Failed to initialize thread-local session: {_e}")
            self.localdata.session = requests.Session()
        
        while self.WORKING.is_set():
            work_queue, work_item = self._get_next_work()
            if work_item is None:
                continue
            obj, args, kwargs = work_item

            try:
                queued_at = getattr(obj, '_profile_queued_at', None)
                if queued_at is not None:
                    record_stage(
                        "chunk.queue_wait",
                        (time.monotonic() - queued_at) * 1000.0,
                        tile_id=getattr(obj, 'tile_id', None) or getattr(obj, 'chunk_id', None),
                        details={
                            "priority": getattr(obj, 'priority', None),
                            "prefetch": bool(getattr(obj, 'prefetch', False)),
                        },
                    )
                    obj._profile_queued_at = None

                # Mark chunk as in-flight (Chunk always has these attributes)
                obj.in_queue = False
                obj.in_flight = True
                with self._inflight_objs_lock:
                    self._inflight_objs.add(obj)

                if self._dispatch_async(obj, args, kwargs, idx):
                    # Ownership moved to the async stage: it now owns the
                    # in-flight bookkeeping and the eventual finalisation.
                    continue

                try:
                    resolved = self.get(obj, *args, **kwargs)
                    error = None
                except Exception as err:
                    resolved = False
                    error = err
                self._finalize_attempt(obj, args, kwargs, resolved, error)
            except Exception as err:
                log.exception("Unhandled chunk dispatch failure for %s", obj)
                self._finalize_attempt(
                    obj,
                    args,
                    kwargs,
                    False,
                    err,
                )
            finally:
                work_queue.task_done()
        
        # Worker loop ended - cleanup thread-local HTTP session
        try:
            session = getattr(self.localdata, 'session', None)
            if session is not None:
                session.close()
                self.localdata.session = None
        except Exception:
            pass

    def _dispatch_async(self, obj, args, kwargs, idx) -> bool:
        """Hand the download to the async stage; True means ownership moved."""
        return False

    def _settle_chunk(self, obj, resolved):
        """Release per-chunk coalescing state. Overridden by ChunkGetter."""
        return resolved

    def _settle_attempt(self, obj, args, kwargs, resolved, error=None, quiet=False):
        """Terminal bookkeeping for an asynchronously dispatched attempt.

        Mirrors what the synchronous worker path does after `self.get()`:
        release coalescing state first (so a resubmit is not mistaken for a
        duplicate), then resubmit/finalize.  ``quiet`` marks an unstarted
        attempt being handed back (capacity backpressure or shutdown), which
        is expected flow control rather than a download failure.
        """
        try:
            self._settle_chunk(obj, resolved)
        except Exception:
            log.exception("Failed to settle chunk state for %s", obj)
        return self._finalize_attempt(obj, args, kwargs, resolved, error, quiet=quiet)

    def _finalize_attempt(self, obj, args, kwargs, resolved, error=None, quiet=False):
        """Resubmit or retire one finished attempt and clear in-flight state."""
        did_resubmit = False
        try:
            if error is not None:
                log.error(f"ERROR {error} getting: {obj} {args} {kwargs}, re-submit.")
            if not resolved:
                if obj.permanent_failure:
                    log.debug(f"Chunk {obj} permanently failed ({obj.failure_reason}), not re-submitting")
                elif getattr(obj, 'cancelled', False):
                    log.debug(f"Chunk {obj} cancelled, not re-submitting")
                else:
                    if error is None and not quiet:
                        log.warning(f"Failed getting: {obj} {args} {kwargs}, re-submit.")
                    # CRITICAL: Clear in_flight BEFORE re-submitting, otherwise
                    # submit() sees in_flight=True and silently drops the chunk!
                    obj.in_flight = False
                    bump('worker_resubmit')
                    did_resubmit = bool(self.submit(obj, *args, **kwargs))
                    if not did_resubmit:
                        bump("worker_resubmit_dropped")
                        abandon = getattr(
                            self, "_abandon_duplicate_waiters", None
                        )
                        if abandon is not None:
                            abandon(obj)
        finally:
            if not did_resubmit:
                # Normal completion — we still own in_flight and _inflight_objs
                obj.in_flight = False
                with self._inflight_objs_lock:
                    self._inflight_objs.discard(obj)
            # If did_resubmit: in_flight was already cleared before submit(), and
            # _inflight_objs will be managed by whichever worker picks it up next.
        return did_resubmit

    def _get_next_work(self):
        """Always service live work first; never spin on paused prefetch."""
        try:
            return self.live_queue, self.live_queue.get_nowait()
        except Empty:
            pass

        if is_prefetch_runtime_allowed():
            try:
                return self.prefetch_queue, self.prefetch_queue.get_nowait()
            except Empty:
                pass

        try:
            return self.live_queue, self.live_queue.get(timeout=0.05)
        except Empty:
            if is_prefetch_runtime_allowed():
                try:
                    return (
                        self.prefetch_queue,
                        self.prefetch_queue.get(timeout=0.05),
                    )
                except Empty:
                    pass
        return None, None

    def _enqueue(self, obj, args, kwargs) -> bool:
        if getattr(obj, "prefetch", False):
            if not is_prefetch_runtime_allowed():
                bump("prefetch_skipped_until_flight")
                return False
            target = self.prefetch_queue
        else:
            target = self.live_queue
        try:
            target.put_nowait((obj, args, kwargs))
            return True
        except Full:
            # Only the prefetch lane is bounded.
            bump("prefetch_queue_full")
            return False

    def queue_depths(self):
        return {
            "live": self.live_queue.qsize(),
            "prefetch": self.prefetch_queue.qsize(),
        }

    def get(obj, *args, **kwargs):
        raise NotImplementedError

    def submit(self, obj, *args, **kwargs):
        # Don't queue permanently failed or cancelled chunks
        if obj.permanent_failure:
            bump('submit_skip_permanent_failure')
            return
        if getattr(obj, 'cancelled', False):
            bump('submit_skip_cancelled')
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
        if self._enqueue(obj, args, kwargs):
            return True
        else:
            obj.in_queue = False
            return False

class ChunkGetter(Getter):
    # Track in-progress chunk_ids GLOBALLY to prevent queueing duplicates
    _queued_chunk_ids = set()
    _queued_chunk_objs = {}
    _queued_chunk_waiters = {}
    _queued_lock = threading.Lock()

    def reprioritize_queue(self) -> None:
        """Rebuild both heaps and move promoted prefetch into the live lane."""
        promoted = []
        try:
            with self.prefetch_queue.mutex:
                kept = []
                for item in self.prefetch_queue.queue:
                    if getattr(item[0], "prefetch", False):
                        kept.append(item)
                    else:
                        promoted.append(item)
                self.prefetch_queue.queue[:] = kept
                heapq.heapify(self.prefetch_queue.queue)
                self.prefetch_queue.not_full.notify_all()
            for item in promoted:
                self.live_queue.put_nowait(item)
            with self.live_queue.mutex:
                heapq.heapify(self.live_queue.queue)
                self.live_queue.not_empty.notify_all()
        except Exception:
            pass

    def _cancel_work(self, reason="", prefetch_only=True,
                     include_inflight=False) -> int:
        """Cancel queued downloads so live/shutdown work can drain.

        In-flight HTTP requests cannot be interrupted safely, but removing the
        queued tail prevents prefetch from keeping the worker pool busy after a
        flight stops or while AutoOrtho is unmounting.  During shutdown we also
        mark in-flight chunks cancelled so any thread waiting on their ready
        event unblocks immediately.
        """
        cancelled = []
        stage = self._async_stage
        if stage is not None:
            stage.cancel_deferred(prefetch_only=prefetch_only)

        queues = (
            (self.prefetch_queue,)
            if prefetch_only
            else (self.live_queue, self.prefetch_queue)
        )
        for work_queue in queues:
            with work_queue.mutex:
                kept = []
                removed = []
                for item in list(work_queue.queue):
                    obj = item[0]
                    if (not prefetch_only) or getattr(
                        obj, 'prefetch', False
                    ):
                        removed.append(obj)
                    else:
                        kept.append(item)
                if removed:
                    work_queue.queue[:] = kept
                    heapq.heapify(work_queue.queue)
                    work_queue.not_full.notify_all()
                    cancelled.extend(removed)

        for obj in cancelled:
            try:
                obj.in_queue = False
                chunk_id = getattr(obj, 'chunk_id', None)
                waiters = []
                if chunk_id:
                    with self._queued_lock:
                        self._queued_chunk_ids.discard(chunk_id)
                        self._queued_chunk_objs.pop(chunk_id, None)
                        waiters = self._queued_chunk_waiters.pop(
                            chunk_id, [])
                obj.cancel()
                for waiter in waiters:
                    try:
                        waiter.in_queue = False
                        waiter.cancel()
                    except Exception:
                        pass
            except Exception:
                pass

        if include_inflight:
            try:
                with self._inflight_objs_lock:
                    inflight = list(self._inflight_objs)
                for obj in inflight:
                    if prefetch_only and not getattr(obj, 'prefetch', False):
                        continue
                    try:
                        obj.cancel()
                        cancelled.append(obj)
                    except Exception:
                        pass
            except Exception:
                pass

        if cancelled:
            msg = f" ({reason})" if reason else ""
            work_type = "prefetch" if prefetch_only else "chunk"
            log.info(f"Cancelled {len(cancelled)} {work_type} work items{msg}")
            bump(f'{work_type}_cancelled', len(cancelled))
        return len(cancelled)

    def cancel_prefetch_work(self, reason="") -> int:
        return self._cancel_work(reason, prefetch_only=True,
                                 include_inflight=False)

    def cancel_all_work(self, reason="") -> int:
        return self._cancel_work(reason, prefetch_only=False,
                                 include_inflight=True)

    def submit(self, obj, *args, **kwargs):
        """Submit chunk for download with duplicate prevention."""
        became_foreground = False
        if getattr(obj, 'prefetch', False) and getattr(obj, 'priority', 0) < PRIORITY_PREFETCH:
            obj.prefetch = False
            became_foreground = True
        if obj.permanent_failure:
            bump('submit_skip_permanent_failure')
            return False
        if getattr(obj, 'cancelled', False):
            bump('submit_skip_cancelled')
            return False
        
        # Per-object coalescing checks
        if obj.ready.is_set():
            bump('submit_skip_already_ready')
            return False
        if obj.in_queue:
            if became_foreground:
                self.reprioritize_queue()
            bump('submit_skip_already_queued')
            return False
        if obj.in_flight:
            bump('submit_skip_in_flight')
            return False

        chunk_id = getattr(obj, 'chunk_id', None)

        # Check if an equivalent chunk is already queued or in flight.  Keep this
        # chunk as a waiter so it receives the downloaded bytes and completion
        # notification instead of waiting forever on a skipped duplicate.
        if chunk_id:
            reprioritize = False
            duplicate_queued = False
            with self._queued_lock:
                if chunk_id in self._queued_chunk_ids:
                    duplicate_queued = True
                    queued_obj = self._queued_chunk_objs.get(chunk_id)
                    if queued_obj is not None:
                        incoming_priority = getattr(obj, 'priority', PRIORITY_PREFETCH)
                        if incoming_priority < getattr(queued_obj, 'priority', incoming_priority):
                            queued_obj.priority = incoming_priority
                            reprioritize = True
                        if not getattr(obj, 'prefetch', False) and getattr(queued_obj, 'prefetch', False):
                            queued_obj.prefetch = False
                            reprioritize = True
                            bump('background_chunk_promoted_live')
                    obj.in_queue = True
                    self._queued_chunk_waiters.setdefault(chunk_id, []).append(obj)
                    bump('submit_skip_id_already_queued')
            if duplicate_queued:
                if reprioritize:
                    self.reprioritize_queue()
                return True
        
        obj.in_queue = True
        
        # Add to queue
        if chunk_id:
            with self._queued_lock:
                self._queued_chunk_ids.add(chunk_id)
                self._queued_chunk_objs[chunk_id] = obj
        
        obj._profile_queued_at = time.monotonic()
        if not self._enqueue(obj, args, kwargs):
            obj.in_queue = False
            obj._profile_queued_at = None
            if chunk_id:
                with self._queued_lock:
                    self._queued_chunk_ids.discard(chunk_id)
                    self._queued_chunk_objs.pop(chunk_id, None)
            return False
        return True

    def _complete_duplicate_waiters(self, obj, waiters):
        """Fan out one downloaded chunk result to duplicate Chunk objects."""
        if not waiters:
            return
        for waiter in waiters:
            if waiter is obj or waiter.ready.is_set():
                continue
            waiter.data = obj.data
            waiter.permanent_failure = obj.permanent_failure
            waiter.failure_reason = obj.failure_reason
            waiter.fetchtime = obj.fetchtime
            waiter.url = getattr(obj, 'url', None)
            waiter.in_queue = False
            waiter.in_flight = False
            waiter.ready.set()
            try:
                if tile_completion_tracker is not None and waiter.tile_id:
                    tile_completion_tracker.notify_chunk_ready(waiter.tile_id, waiter)
            except Exception:
                pass

    def _abandon_duplicate_waiters(self, obj):
        chunk_id = getattr(obj, "chunk_id", None)
        if not chunk_id:
            return
        with self._queued_lock:
            waiters = self._queued_chunk_waiters.pop(chunk_id, [])
        obj.cancel()
        for waiter in waiters:
            try:
                waiter.in_queue = False
                waiter.in_flight = False
                waiter.cancel()
            except Exception:
                pass

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
        self._settle_chunk(obj, result)
        return result

    def _settle_chunk(self, obj, result):
        """Release chunk_id coalescing state and fan out to duplicate waiters."""
        chunk_id = getattr(obj, 'chunk_id', None)
        waiters = []
        if chunk_id:
            with self._queued_lock:
                self._queued_chunk_ids.discard(chunk_id)
                if self._queued_chunk_objs.get(chunk_id) is obj:
                    self._queued_chunk_objs.pop(chunk_id, None)
                if result:
                    waiters = self._queued_chunk_waiters.pop(chunk_id, [])
                    bump('chunk_download_completed')

        if result:
            self._complete_duplicate_waiters(obj, waiters)

        return result

    def _create_async_stage(self):
        if HTTP2Broker is None:
            return None
        try:
            max_outstanding = resolve_provider_setting("provider_max_in_flight")
            workers = resolve_provider_setting("download_dispatch_workers")
        except Exception:
            log.exception("Falling back to synchronous downloads")
            return None
        return _BrokerDownloadStage(self, max_outstanding, workers)

    def _dispatch_async(self, obj, args, kwargs, idx) -> bool:
        stage = self._async_stage
        if stage is None:
            return False
        if obj.ready.is_set():
            # Matches the fast path in get(); cheap enough to run inline.
            return False
        return stage.dispatch(obj, args, kwargs, idx)


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


def _resolve_getter_workers():
    """Size the chunk worker pool for this process.

    With the broker configured the workers no longer own a socket for the
    whole round trip: they only hand chunks to the async stage, which caps
    real network concurrency at ``provider_max_in_flight``.  A handful of
    coordination threads (``download_dispatch_workers``) is therefore enough,
    and a large ``fetch_threads`` value would only add context switching.
    Without a broker the legacy sizing still applies, because each worker
    then blocks on its own synchronous request.
    """

    if _broker_env_configured():
        try:
            workers = max(1, int(resolve_provider_setting("download_dispatch_workers")))
        except Exception:
            log.exception(
                "Could not resolve download_dispatch_workers; using legacy sizing"
            )
        else:
            log.info(
                "Broker configured: using %d coordination workers "
                "(network concurrency is provider_max_in_flight)",
                workers,
            )
            return workers

    requested = max(1, int(getattr(CFG.autoortho, "fetch_threads", 32)))
    max_concurrent = resolve_provider_setting("provider_max_in_flight")
    if os.getenv("AO_RUN_MODE") not in ("mount_worker", "macfuse_worker"):
        max_concurrent = min(max_concurrent, 8)
    workers = min(
        requested,
        max_concurrent,
        max(8, min(64, CURRENT_CPU_COUNT * 2)),
    )
    if workers != requested:
        log.warning(
            "Clamping fetch_threads from %d to %d for this process "
            "(in-flight budget: %d)",
            requested,
            workers,
            max_concurrent,
        )
    return workers


class LazyChunkGetter:
    def __init__(self):
        self._instance = None
        self._lock = threading.Lock()

    @property
    def initialized(self):
        return self._instance is not None

    def _get(self):
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = _create_chunk_getter(
                        _resolve_getter_workers()
                    )
        return self._instance

    def submit(self, *args, **kwargs):
        return self._get().submit(*args, **kwargs)

    def reprioritize_queue(self):
        if self._instance is not None:
            return self._instance.reprioritize_queue()

    def cancel_all_work(self, *args, **kwargs):
        if self._instance is not None:
            return self._instance.cancel_all_work(*args, **kwargs)

    def cancel_prefetch_work(self, *args, **kwargs):
        if self._instance is not None:
            return self._instance.cancel_prefetch_work(*args, **kwargs)

    def stop(self):
        if self._instance is not None:
            self._instance.stop()
            self._instance = None

    def __getattr__(self, name):
        return getattr(self._get(), name)


chunk_getter = LazyChunkGetter()

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
    """Pre-warm native pipeline for optimal first-tile performance.

    Early init: creates persistent decoder handles and warms the OpenMP
    thread pool.  If a real buffer pool is available later (via
    ``start_predictive_dds``), ``warmup_full`` is called again with it
    to pre-fault the pool's memory pages.
    """
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

        # Initialize persistent decoders (one per OpenMP thread) and warm
        # the OpenMP thread pool.  No buffer pool needed at this stage.
        AoDecode.init_persistent_decoders()
        AoDecode.warmup_full()  # pool=None — just warms decoders + OpenMP
        log.info("Native decode pipeline pre-warmed (decoders + OpenMP)")
    except Exception as e:
        log.debug(f"Native decode warmup failed: {e}")

    # Also warm the native cache I/O thread pool
    try:
        try:
            from autoortho.aopipeline import AoCache
        except ImportError:
            try:
                from aopipeline import AoCache
            except ImportError:
                return

        if AoCache.is_available():
            AoCache.warmup_threads()
            log.info("Native cache I/O thread pool pre-warmed")
    except Exception as e:
        log.debug(f"Native cache warmup failed: {e}")

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

_cache_write_pending_bytes = 0
_cache_write_pending_lock = threading.Lock()
_lt_cache_write_pending_bytes = 0
_lt_cache_write_pending_lock = threading.Lock()


def _cache_write_limit_bytes() -> int:
    try:
        value = int(
            float(getattr(CFG.autoortho, "cache_write_buffer_mb", 256))
            * 1048576
        )
    except (TypeError, ValueError):
        value = 256 * 1048576
    return max(16 * 1048576, value)


def _lt_cache_write_limit_bytes() -> int:
    try:
        value = int(
            float(
                getattr(
                    CFG.autoortho,
                    "long_term_cache_write_buffer_mb",
                    128,
                )
            )
            * 1048576
        )
    except (TypeError, ValueError):
        value = 128 * 1048576
    return max(16 * 1048576, value)


def _release_pending_bytes(kind: str, byte_count: int) -> None:
    global _cache_write_pending_bytes, _lt_cache_write_pending_bytes
    if kind == "local":
        with _cache_write_pending_lock:
            _cache_write_pending_bytes = max(
                0, _cache_write_pending_bytes - byte_count
            )
            profile_gauge(
                "cache_write.pending_bytes",
                _cache_write_pending_bytes,
            )
    else:
        with _lt_cache_write_pending_lock:
            _lt_cache_write_pending_bytes = max(
                0, _lt_cache_write_pending_bytes - byte_count
            )
            profile_gauge(
                "lt_cache_write.pending_bytes",
                _lt_cache_write_pending_bytes,
            )


def _submit_bounded_cache_write(
    executor,
    function,
    *args,
    byte_count: int,
    kind: str,
) -> bool:
    global _cache_write_pending_bytes, _lt_cache_write_pending_bytes
    byte_count = max(0, int(byte_count))
    if kind == "local":
        lock = _cache_write_pending_lock
        limit = _cache_write_limit_bytes()
    else:
        lock = _lt_cache_write_pending_lock
        limit = _lt_cache_write_limit_bytes()

    with lock:
        pending = (
            _cache_write_pending_bytes
            if kind == "local"
            else _lt_cache_write_pending_bytes
        )
        if pending + byte_count > limit:
            bump(f"{kind}_cache_write_backpressure")
            return False
        if kind == "local":
            _cache_write_pending_bytes += byte_count
            current = _cache_write_pending_bytes
        else:
            _lt_cache_write_pending_bytes += byte_count
            current = _lt_cache_write_pending_bytes
        gauge_prefix = "cache_write" if kind == "local" else "lt_cache_write"
        profile_gauge(f"{gauge_prefix}.pending_bytes", current)

    try:
        future = executor.submit(function, *args)
    except Exception:
        _release_pending_bytes(kind, byte_count)
        return False
    future.add_done_callback(
        lambda _future: _release_pending_bytes(kind, byte_count)
    )
    return True

_lt_cache_write_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="lt_cache_writer"
)

# LT cache I/O is offloaded to a small bounded pool so a hung backing FS
# (e.g. NFS) can't block FUSE threads or stack up unbounded probe threads.
_lt_io_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="lt_io"
)

_lt_cache_available = True
_lt_cache_retry_after = 0.0
_lt_cache_state_lock = threading.Lock()
_LT_CACHE_TIMEOUT = 3.0
_LT_CACHE_RETRY_INTERVAL = 30.0


def _lt_chunk_shard(chunk_id: str) -> str:
    """1-byte shard key (256 buckets) for LT cache fan-out."""
    return hashlib.blake2b(chunk_id.encode(), digest_size=1).hexdigest()


def _lt_chunk_path(lt_dir: str, chunk_id: str) -> str:
    return os.path.join(lt_dir, _lt_chunk_shard(chunk_id), f"{chunk_id}.jpg")


_LT_CACHE_DIR_RESOLVED: tuple = (False, None)  # (initialized, value)


def _resolve_lt_cache_dir() -> str | None:
    """Read ``CFG.paths.long_term_cache_dir`` once and cache the normalized value.

    Per-chunk Chunk.__init__ used to repeat this getattr+str+strip; cache it so
    chunk creation stays cheap when LT cache is disabled (the common case).
    """
    global _LT_CACHE_DIR_RESOLVED
    initialized, value = _LT_CACHE_DIR_RESOLVED
    if initialized:
        return value
    raw = str(getattr(CFG.paths, 'long_term_cache_dir', '') or '').strip()
    value = raw if raw else None
    _LT_CACHE_DIR_RESOLVED = (True, value)
    return value


def _lt_call_with_timeout(fn, *args, on_timeout=None, default=False):
    """Run ``fn(*args)`` on the LT executor and return its result, or ``default``
    on timeout/error. Calls ``on_timeout()`` (if given) when the deadline fires."""
    try:
        return _lt_io_executor.submit(fn, *args).result(timeout=_LT_CACHE_TIMEOUT)
    except concurrent.futures.TimeoutError:
        if on_timeout:
            on_timeout()
        return default
    except Exception:
        return default


def _lt_probe_dir(dir_path: str) -> bool:
    return _lt_call_with_timeout(os.path.isdir, dir_path)


def _lt_available(dir_path: str | None = None) -> bool:
    global _lt_cache_available, _lt_cache_retry_after
    with _lt_cache_state_lock:
        if _lt_cache_available:
            return True
        if time.monotonic() < _lt_cache_retry_after:
            return False
    # Probe outside the lock — _lt_probe_dir blocks on the executor.
    if dir_path and not _lt_probe_dir(dir_path):
        with _lt_cache_state_lock:
            _lt_cache_retry_after = time.monotonic() + _LT_CACHE_RETRY_INTERVAL
        log.debug("LT cache dir still unreachable, retrying in 30s")
        return False
    with _lt_cache_state_lock:
        _lt_cache_available = True
    return True


def _lt_mark_unavailable(retry_interval: float = _LT_CACHE_RETRY_INTERVAL):
    global _lt_cache_available, _lt_cache_retry_after
    with _lt_cache_state_lock:
        _lt_cache_available = False
        _lt_cache_retry_after = time.monotonic() + retry_interval
    log.warning(f"LT cache unavailable, skipping for {retry_interval:.0f}s")


def _lt_read_bytes(path: str):
    """Read a file via the LT executor with timeout. Returns bytes or None."""
    return _lt_call_with_timeout(
        Path(path).read_bytes,
        on_timeout=_lt_mark_unavailable,
        default=None,
    )


def _cleanup_temp(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        log.debug(f"_cleanup_temp({path}): {e}")


# Tracks the errno from the most recent failed _atomic_write per write directory.
# Keyed by parent dir (not full path) to bound size to O(dirs) not O(chunks).
# Consulted by _save_to_dir to choose the right LT cache backoff interval.
_atomic_write_last_errno: dict[str, int] = {}


def _atomic_write(target_path: str, data: bytes) -> bool:
    """Atomically write ``data`` to ``target_path`` via tempfile + ``os.replace``.

    Creates the parent dir, retries on transient Windows file-lock errors
    (WinError 5/32/33). Returns True on success or already-exists; False on
    failure. Bookkeeping (disk budget, stats) is the caller's responsibility.
    """
    write_dir = os.path.dirname(target_path)
    try:
        os.makedirs(write_dir, exist_ok=True)
    except OSError as e:
        log.debug(f"_atomic_write: makedirs failed for {write_dir}: {e}")
        return False

    if os.path.exists(target_path):
        try:
            _chunk_cache_index.mark_present(target_path)
        except NameError:
            pass
        return True

    temp_filename = os.path.join(
        write_dir, f".{os.path.basename(target_path)}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(temp_filename, 'wb') as h:
            h.write(data)
    except FileNotFoundError:
        try:
            _chunk_cache_index.invalidate(target_path)
        except NameError:
            pass
        return False
    except OSError as e:
        _atomic_write_last_errno[write_dir] = e.errno
        _cleanup_temp(temp_filename)
        log.warning(f"_atomic_write: failed to write temp for {target_path}: {e}")
        return False
    except Exception as e:
        _cleanup_temp(temp_filename)
        log.warning(f"_atomic_write: failed to write temp for {target_path}: {e}")
        return False

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            if os.path.exists(target_path):
                _cleanup_temp(temp_filename)
                try:
                    _chunk_cache_index.mark_present(target_path)
                except NameError:
                    pass
                return True
            os.replace(temp_filename, target_path)
            try:
                _chunk_cache_index.mark_present(target_path)
            except NameError:
                pass
            return True
        except FileExistsError:
            _cleanup_temp(temp_filename)
            try:
                _chunk_cache_index.mark_present(target_path)
            except NameError:
                pass
            return True
        except OSError as e:
            if getattr(e, 'winerror', None) in (5, 32, 33) and attempt < max_attempts:
                time.sleep(0.05 * attempt)
                continue
            _cleanup_temp(temp_filename)
            log.warning(f"_atomic_write: failed to replace {target_path}: {e}")
            return False
    return False


def _async_cache_write(chunk, data):
    """Fire-and-forget cache write. Errors are logged but don't affect processing."""
    try:
        # Check if cache directory still exists (may have been cleaned up by temp directory)
        # This prevents errors when diagnose() temp directories are deleted before async write
        if not os.path.exists(chunk.cache_dir):
            log.debug(f"Cache dir gone for {chunk}, skipping async write")
            return
        chunk.save_cache(data=data)
    except (FileNotFoundError, OSError) as e:
        # Directory may have been deleted between check and write (race condition)
        # This is expected for temporary directories, so just log at debug level
        log.debug(f"Async cache write skipped for {chunk}: {e}")
    except Exception as e:
        log.debug(f"Async cache write failed for {chunk}: {e}")


def shutdown_cache_writer():
    """Shut down the cache write executors. Local writes block to flush; LT writes
    drop in flight (the LT path may be on a slow/hung backing FS at exit time)."""
    try:
        _cache_write_executor.shutdown(wait=True)
    except Exception as e:
        log.debug(f"shutdown_cache_writer: local executor shutdown failed: {e}")
    try:
        _lt_cache_write_executor.shutdown(wait=False)
    except Exception as e:
        log.debug(f"shutdown_cache_writer: LT executor shutdown failed: {e}")
    try:
        _lt_io_executor.shutdown(wait=False)
    except Exception as e:
        log.debug(f"shutdown_cache_writer: LT IO executor shutdown failed: {e}")


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
    
    INDEX_SCHEMA_VERSION = 1

    def __init__(
        self,
        terrain_folder: str,
        scenery_name: str,
        *,
        build_async: bool = True,
        index_cache_dir: Optional[str] = None,
    ):
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

        # Sparse spatial index for high-zoom tiles (ZL18, ZL19)
        # These are airport tiles — sparse but important for prefetching.
        # Bucketed by 1° lat/lon cells for O(1) lookup.
        # Built once at mount time via glob (typically <50ms for a few hundred files).
        self._highzoom_index: Dict[Tuple[int, int], List[Tuple[int, int, str, int]]] = {}
        self._highzoom_count = 0
        self._index_lock = threading.Lock()
        self._index_ready = threading.Event()
        self._index_cancel = threading.Event()
        self._index_error = None
        self._index_thread = None
        cache_root = index_cache_dir or os.path.join(
            str(CFG.paths.cache_dir), "terrain_index"
        )
        cache_key = hashlib.sha256(
            os.path.normcase(os.path.abspath(terrain_folder)).encode("utf-8")
        ).hexdigest()
        self._index_cache_path = os.path.join(cache_root, f"{cache_key}.json")

        if self._folder_exists:
            if build_async:
                self._index_thread = threading.Thread(
                    target=self._load_or_build_highzoom_index,
                    name=f"TerrainIndex-{scenery_name}",
                    daemon=True,
                )
                self._index_thread.start()
            else:
                self._load_or_build_highzoom_index()
        else:
            log.warning(f"TerrainTileLookup: Folder not found: {terrain_folder}")
            self._index_ready.set()
    
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

    def get_tiles_for_bounds(
        self,
        lat_min: float,
        lon_min: float,
        lat_max: float,
        lon_max: float,
        *,
        maptype_filter: Optional[str] = None,
        tile_zooms=(16, 18),
    ) -> List[Tuple[int, int, str, int]]:
        """Return terrain tiles intersecting geographic bounds."""
        if not self._folder_exists:
            return []
        maptypes = (
            [maptype_filter]
            if maptype_filter
            else list(get_discovered_maptypes())
        )
        results = []
        for zoom in tile_zooms:
            north_row, west_col = self._latlon_to_tile(
                lat_max, lon_min, zoom
            )
            south_row, east_col = self._latlon_to_tile(
                lat_min, lon_max, zoom
            )
            row_start, row_end = sorted((north_row, south_row))
            col_start, col_end = sorted((west_col, east_col))
            for row in range(row_start, row_end + 1, self.TILE_GRID_STEP):
                for col in range(
                    col_start, col_end + 1, self.TILE_GRID_STEP
                ):
                    for maptype in maptypes:
                        if self._tile_exists(row, col, maptype, zoom):
                            results.append((row, col, maptype, zoom))
                            break
        return results
    
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

    @staticmethod
    def _tile_to_latlon(row: int, col: int, zoom: int) -> Tuple[float, float]:
        """
        Convert tile grid coordinates back to approximate center lat/lon.

        Inverse of _latlon_to_tile. Used to bucket high-zoom tiles into
        1° lat/lon cells for the sparse spatial index.

        Returns:
            (lat, lon) in degrees for the center of the 16×16 tile block.
        """
        n = 2 ** zoom
        step = TerrainTileLookup.TILE_GRID_STEP
        center_col = col + step / 2
        center_row = row + step / 2
        lon = center_col / n * 360 - 180
        lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * center_row / n)))
        lat = math.degrees(lat_rad)
        return (lat, lon)

    def _terrain_signature(self) -> int:
        return int(os.stat(self._terrain_folder).st_mtime_ns)

    def _load_or_build_highzoom_index(self) -> None:
        t0 = time.time()
        try:
            if not self._load_cached_highzoom_index():
                index, count = self._scan_highzoom_index()
                if self._index_cancel.is_set():
                    return
                with self._index_lock:
                    self._highzoom_index = index
                    self._highzoom_count = count
                self._write_cached_highzoom_index(index, count)
            elapsed_ms = (time.time() - t0) * 1000
            log.info(
                "TerrainTileLookup: Ready for %s at %s "
                "(%d high-zoom tiles, %.0fms)",
                self._scenery_name,
                self._terrain_folder,
                self._highzoom_count,
                elapsed_ms,
            )
        except Exception as exc:
            self._index_error = exc
            log.error(
                "TerrainTileLookup: failed to index %s: %s",
                self._scenery_name,
                exc,
            )
        finally:
            self._index_ready.set()

    def _scan_highzoom_index(self):
        index: Dict[
            Tuple[int, int], List[Tuple[int, int, str, int]]
        ] = {}
        count = 0
        with os.scandir(self._terrain_folder) as entries:
            for entry in entries:
                if self._index_cancel.is_set():
                    break
                if not entry.is_file(follow_symlinks=False):
                    continue
                basename = entry.name
                if not basename.endswith(".ter"):
                    continue
                name = basename[:-4]
                try:
                    tokens = name.split("_", 2)
                    row_val = int(tokens[0])
                    col_val = int(tokens[1])
                    maptype_zoom = tokens[2]
                    maptype = maptype_zoom[:-2]
                    file_zoom = int(maptype_zoom[-2:])
                except (ValueError, IndexError):
                    continue
                if file_zoom not in (18, 19) or maptype.upper() in ("ZL", ""):
                    continue
                if (
                    row_val % self.TILE_GRID_STEP != 0
                    or col_val % self.TILE_GRID_STEP != 0
                ):
                    continue
                lat, lon = self._tile_to_latlon(row_val, col_val, file_zoom)
                bucket_key = (int(math.floor(lat)), int(math.floor(lon)))
                index.setdefault(bucket_key, []).append(
                    (row_val, col_val, maptype, file_zoom)
                )
                count += 1
        return index, count

    def _load_cached_highzoom_index(self) -> bool:
        try:
            with open(self._index_cache_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if (
                payload.get("schema_version") != self.INDEX_SCHEMA_VERSION
                or payload.get("terrain_folder")
                != os.path.normcase(os.path.abspath(self._terrain_folder))
                or int(payload.get("terrain_signature", -1))
                != self._terrain_signature()
            ):
                return False
            index = {}
            count = 0
            for cell in payload.get("cells", []):
                if not isinstance(cell, list) or len(cell) != 3:
                    return False
                entries = [
                    (int(row), int(col), str(maptype), int(zoom))
                    for row, col, maptype, zoom in cell[2]
                ]
                index[(int(cell[0]), int(cell[1]))] = entries
                count += len(entries)
            if count != int(payload.get("tile_count", -1)):
                return False
            with self._index_lock:
                self._highzoom_index = index
                self._highzoom_count = count
            return True
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _write_cached_highzoom_index(self, index, count) -> None:
        payload = {
            "schema_version": self.INDEX_SCHEMA_VERSION,
            "terrain_folder": os.path.normcase(
                os.path.abspath(self._terrain_folder)
            ),
            "terrain_signature": self._terrain_signature(),
            "tile_count": count,
            "cells": [
                [lat, lon, entries]
                for (lat, lon), entries in sorted(index.items())
            ],
        }
        data = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        os.makedirs(os.path.dirname(self._index_cache_path), exist_ok=True)
        if not _atomic_write(self._index_cache_path, data):
            log.warning(
                "TerrainTileLookup: failed to persist index for %s",
                self._scenery_name,
            )

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        if not self._index_ready.wait(timeout):
            return False
        return self._index_error is None

    def get_highzoom_tiles_near(self, lat: float, lon: float,
                                 radius_nm: float = 40.0,
                                 maptype_filter: Optional[str] = None
                                 ) -> List[Tuple[int, int, str, int]]:
        """
        Find all indexed high-zoom (ZL18/19) tiles within radius of a position.

        Uses the bucketed spatial index for O(1) cell lookup, then filters
        by approximate distance. No filesystem calls — purely in-memory.

        Args:
            lat, lon: Query position in degrees
            radius_nm: Search radius in nautical miles
            maptype_filter: Optional maptype to filter by (e.g., "BI")

        Returns:
            List of (row, col, maptype, zoom) tuples for matching tiles
        """
        if not self._index_ready.is_set():
            return []
        with self._index_lock:
            highzoom_index = self._highzoom_index
        if not highzoom_index:
            return []

        results = []
        # 1° latitude ≈ 60nm
        radius_deg = radius_nm / 60.0
        lon_deg = radius_nm / (60.0 * max(0.1, math.cos(math.radians(lat))))

        # Check all 1° cells that could contain tiles within radius
        lat_min = int(math.floor(lat - radius_deg))
        lat_max = int(math.floor(lat + radius_deg))
        lon_min = int(math.floor(lon - lon_deg))
        lon_max = int(math.floor(lon + lon_deg))

        for blat in range(lat_min, lat_max + 1):
            for blon in range(lon_min, lon_max + 1):
                bucket = highzoom_index.get((blat, blon))
                if not bucket:
                    continue
                for entry in bucket:
                    row_val, col_val, maptype, zoom = entry
                    if maptype_filter and maptype != maptype_filter:
                        continue
                    # Approximate distance check using tile center
                    tile_lat, tile_lon = self._tile_to_latlon(row_val, col_val, zoom)
                    dlat = (tile_lat - lat) * 60  # convert degrees to nm
                    dlon = (tile_lon - lon) * 60 * math.cos(math.radians(lat))
                    dist_sq = dlat * dlat + dlon * dlon
                    if dist_sq <= radius_nm * radius_nm:
                        results.append(entry)

        return results

    def clear_cache(self) -> None:
        """Clear the lookup cache and spatial index."""
        self._index_cancel.set()
        if self._index_thread and self._index_thread.is_alive():
            self._index_thread.join(timeout=1.0)
        with self._cache_lock:
            self._cache.clear()
        with self._index_lock:
            self._highzoom_index.clear()
            self._highzoom_count = 0

    @property
    def is_ready(self) -> bool:
        return self._folder_exists and self._index_ready.is_set()
    
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
            'highzoom_tiles_indexed': self._highzoom_count,
            'highzoom_cells': len(self._highzoom_index),
            'index_ready': self._index_ready.is_set(),
            'index_error': str(self._index_error) if self._index_error else None,
        }


# Module-level terrain lookup management
_terrain_lookups: List[TerrainTileLookup] = []
_terrain_lookups_lock = threading.Lock()


def register_terrain_index(
    terrain_folder: str, scenery_name: str
) -> TerrainTileLookup:
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
        return lookup


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


def get_highzoom_tiles_near(lat: float, lon: float,
                            radius_nm: float = 40.0,
                            maptype_filter: Optional[str] = None
                            ) -> List[Tuple[int, int, str, int]]:
    """
    Query all terrain lookups for high-zoom (ZL18/19) tiles near a position.

    This uses the pre-built sparse spatial index — no filesystem calls.

    Args:
        lat, lon: Position in degrees
        radius_nm: Search radius in nautical miles
        maptype_filter: Optional maptype to filter by

    Returns:
        List of (row, col, maptype, zoom) tuples from all sceneries
    """
    results = []
    with _terrain_lookups_lock:
        for lookup in _terrain_lookups:
            results.extend(lookup.get_highzoom_tiles_near(
                lat, lon, radius_nm, maptype_filter
            ))
    return results


def get_tiles_for_dsf(
    dsf_path: str,
    maptype_filter: Optional[str] = None,
) -> List[Tuple[int, int, str, int]]:
    match = re.search(r"([+-]\d{2,3})([+-]\d{3})\.dsf$", dsf_path)
    if not match:
        return []
    lat = int(match.group(1))
    lon = int(match.group(2))
    results = []
    seen = set()
    with _terrain_lookups_lock:
        lookups = list(_terrain_lookups)
    for lookup in lookups:
        for tile in lookup.get_tiles_for_bounds(
            lat,
            lon,
            lat + 1.0,
            lon + 1.0,
            maptype_filter=maptype_filter,
        ):
            if tile not in seen:
                seen.add(tile)
                results.append(tile)
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
        
        self.interval_sec = float(getattr(CFG.autoortho, 'prefetch_interval', 1.0))
        self.max_chunks = int(getattr(CFG.autoortho, 'prefetch_max_chunks', 512))

        # Unified prefetch radius (used by both velocity and SimBrief methods)
        # This replaces the old simbrief-specific route_prefetch_radius_nm
        self.prefetch_radius_nm = float(getattr(CFG.autoortho, 'prefetch_radius_nm', 40))
        self.prefetch_radius_nm = max(10, min(150, self.prefetch_radius_nm))

        self.interval_sec = max(0.5, min(10.0, self.interval_sec))
        self.max_chunks = max(32, min(4096, self.max_chunks))
        
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
        try:
            chunk_getter.cancel_prefetch_work("prefetcher stopped")
        except Exception:
            pass
        log.info(f"Spatial prefetcher stopped (prefetched {self._prefetch_count} chunks total)")
        bump('prefetch_total', self._prefetch_count)
        
    def _prefetch_loop(self):
        """Main prefetch loop - runs in background thread."""
        while not self._stop_event.is_set() and not is_shutdown_requested():
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
        # Yield all resources to live tile reads when X-Plane is active
        if is_live_building():
            return

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

        # ADDITIONAL: Query sparse index for high-zoom airport tiles (ZL18/19)
        # along the flight path. Point-sampling misses these because ZL18 tiles
        # are ~1.4nm wide but sample points are ~15-20nm apart.
        for point in path_points:
            highzoom_tiles = get_highzoom_tiles_near(
                point.lat, point.lon,
                radius_nm=prefetch_radius_nm,
                maptype_filter=maptype_filter
            )
            for row, col, _maptype, hz_zoom in highzoom_tiles:
                tile_key = (row, col, hz_zoom)
                if tile_key in self._recently_prefetched:
                    continue
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
            
            # Prefetch this tile
            submitted, complete = self._prefetch_tile(row, col, zoom)
            chunks_submitted += submitted

            if submitted > 0:
                tiles_prefetched += 1
                log.debug(f"Prefetch tile ({row},{col}) ZL{zoom}: ETA={time_sec/60:.1f}min, alt={alt_agl}ft AGL")

            # Only mark as recently prefetched if ALL chunks were submitted
            if complete:
                self._recently_prefetched.add(tile_key)
                if len(self._recently_prefetched) > self._max_recent:
                    try:
                        self._recently_prefetched.pop()
                    except KeyError:
                        pass

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

        Uses _get_tiles_in_radius for correct 16-tile grid alignment.

        Returns number of chunks submitted.
        """
        chunks_submitted = 0

        # Get maptype for checking if tiles are opened
        maptype_filter = self._get_maptype_filter()
        default_maptype = maptype_filter or "EOX"

        # Get grid-aligned tiles within radius
        tiles = self._get_tiles_in_radius(waypoint_lat, waypoint_lon, radius_nm, zoom)

        for row, col in tiles:
            if chunks_submitted >= self.max_chunks:
                return chunks_submitted

            tile_key = (row, col, zoom)

            # Skip if recently prefetched
            if tile_key in self._recently_prefetched:
                continue

            # Skip if tile is already opened by X-Plane
            if self._tile_cacher and self._tile_cacher.is_tile_opened_by_xplane(row, col, default_maptype, zoom):
                log.debug(f"Skipping prefetch for {row},{col}@ZL{zoom} - already opened by X-Plane")
                continue

            # Prefetch this tile
            submitted, complete = self._prefetch_tile(row, col, zoom)
            chunks_submitted += submitted

            # Only mark as recently prefetched if ALL chunks were submitted
            if complete:
                self._recently_prefetched.add(tile_key)
                if len(self._recently_prefetched) > self._max_recent:
                    try:
                        self._recently_prefetched.pop()
                    except KeyError:
                        pass

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

        # ADDITIONAL: Query sparse index for high-zoom airport tiles (ZL18/19)
        # Point-sampling misses these because ZL18 tiles are ~1.4nm wide but
        # sample points are ~15-20nm apart. The spatial index finds all ZL18/19
        # tiles within the prefetch radius in O(1) — no filesystem calls.
        highzoom_tiles = get_highzoom_tiles_near(
            lat, lon,
            radius_nm=self.prefetch_radius_nm,
            maptype_filter=maptype_filter
        )
        for row, col, maptype, zoom in highzoom_tiles:
            tile_key = (row, col, maptype, zoom)
            if tile_key not in tile_distances:
                # Estimate distance from aircraft to this tile's center
                tile_lat, tile_lon = TerrainTileLookup._tile_to_latlon(row, col, zoom)
                dlat = (tile_lat - lat) * 111320
                cos_lat = math.cos(math.radians(lat))
                dlon = (tile_lon - lon) * 111320 * cos_lat
                dist_m = math.sqrt(dlat * dlat + dlon * dlon)
                tile_distances[tile_key] = dist_m

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
            
            submitted, complete = self._prefetch_tile(row, col, zoom, maptype)
            chunks_submitted += submitted
            if submitted > 0:
                tiles_prefetched += 1

            # Only mark as recently prefetched if ALL chunks were submitted
            if complete:
                self._recently_prefetched.add(tile_key)
                if len(self._recently_prefetched) > self._max_recent:
                    try:
                        self._recently_prefetched.pop()
                    except KeyError:
                        pass
        
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
            Returns None for "Custom Map" since different cells may use different providers.
        """
        if self._tile_cacher is not None:
            override = getattr(self._tile_cacher, 'maptype_override', None)
            if override and override != "Use tile default" and override != "Custom Map":
                return override
        return None  # Accept any maptype from terrain index
    
    def _prefetch_tile(
        self,
        row,
        col,
        zoom,
        maptype: Optional[str] = None,
        max_chunks: Optional[int] = None,
    ):
        """
        Submit prefetch requests for a tile's chunks at ALL mipmap levels.

        Args:
            row: Tile row coordinate
            col: Tile column coordinate
            zoom: Zoom level (max zoom for this tile)
            maptype: Optional maptype (e.g., "BI", "EOX"). If None, uses config.

        Returns (submitted, complete) tuple:
            submitted: Number of chunks submitted to the queue
            complete: True if all submittable chunks were submitted (or none needed)

        Downloads chunks for all mipmap levels (ZL16, ZL15, ZL14, etc.) to ensure
        the prebuilt DDS matches on-demand behavior where X-Plane may request
        any mipmap level first and get native-resolution chunks.

        IMPORTANT: Uses _open_tile()/_close_tile() pair to properly manage refs.
        This ensures prefetched tiles can be evicted when no longer needed.

        If X-Plane has also opened this tile (refs > 1 after our open), we drop
        it and let the on-demand tile build logic handle it instead.
        """
        if not is_prefetch_runtime_allowed():
            return 0, False

        # Get maptype from parameter or config
        if maptype is None:
            maptype = getattr(CFG.autoortho, 'maptype_override', None)
            if not maptype or maptype in ("Use tile default", "Custom Map"):
                maptype = "EOX"
        
        tile = None
        try:
            # Use _open_tile() to properly increment refs (balanced with _close_tile below)
            # This ensures prefetched tiles don't accumulate refs and block eviction.
            tile = self._tile_cacher._open_tile(row, col, maptype, zoom)
            if not tile:
                return 0, True
            
            # If tile has refs > 1, X-Plane has also opened it - drop and let on-demand handle
            # Our _open_tile incremented refs to 1 (new) or +1 (existing). If refs > 1 now,
            # it means X-Plane is actively using this tile.
            if tile.refs > 1:
                log.debug(f"Tile {row},{col}@ZL{zoom} has refs={tile.refs}, X-Plane is using it - dropping prefetch")
                return 0, True
            
            submitted = 0
            total_submittable = 0
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

                # Create chunks for this zoom level if they don't exist
                tile._create_chunks(mipmap_zoom)

                mipmap_chunks = tile.chunks.get(mipmap_zoom, [])
                if not mipmap_chunks:
                    continue

                if mipmap == 0 and tile_completion_tracker is not None:
                    tile_completion_tracker.start_tracking(tile, zoom)

                for chunk in mipmap_chunks:
                    if max_chunks is not None and submitted >= max_chunks:
                        return submitted, False
                    # Skip if already ready, in flight, or failed
                    if chunk.ready.is_set():
                        continue
                    if chunk.in_queue or chunk.in_flight:
                        continue
                    if chunk.permanent_failure:
                        continue

                    total_submittable += 1

                    # Priority: mipmap 0 = most important, higher mipmaps = less important
                    # Lower priority number = more urgent
                    chunk.priority = self.PREFETCH_PRIORITY_OFFSET + mipmap
                    chunk.prefetch = True

                    # Submit to chunk getter with shorter prefetch timeouts
                    # Prefetch chunks should fail fast to keep the pipeline flowing;
                    # the healing system handles any permanently failed chunks later
                    if chunk_getter.submit(chunk, timeout=(5, 10), max_attempts=8):
                        submitted += 1

            complete = (total_submittable == 0) or (submitted == total_submittable)
            return submitted, complete
            
        except Exception as e:
            log.debug(f"Prefetch error for tile {row},{col}: {e}")
            return 0, False
        finally:
            # Always close the tile to decrement refs - this balances _open_tile() above.
            # The tile stays in the cache (enable_cache=True) but with refs decremented,
            # allowing eviction to work properly when the tile is no longer needed.
            if tile is not None:
                try:
                    self._tile_cacher._close_tile(row, col, maptype, zoom)
                except Exception:
                    pass  # Don't let close errors mask the original exception

    def prefetch_tiles(
        self,
        tiles: List[Tuple[int, int, str, int]],
        max_chunks: Optional[int] = None,
    ) -> int:
        """Queue an explicit ordered set of terrain tiles."""
        if not is_prefetch_runtime_allowed():
            return 0
        limit = self.max_chunks if max_chunks is None else max(1, max_chunks)
        submitted_total = 0
        for row, col, maptype, zoom in tiles:
            if submitted_total >= limit or is_shutdown_requested():
                break
            key = (row, col, maptype, zoom)
            if key in self._recently_prefetched:
                continue
            submitted, complete = self._prefetch_tile(
                row,
                col,
                zoom,
                maptype,
                max_chunks=limit - submitted_total,
            )
            submitted_total += submitted
            if complete:
                self._recently_prefetched.add(key)
                if len(self._recently_prefetched) > self._max_recent:
                    self._recently_prefetched.pop()
        if submitted_total:
            self._prefetch_count += submitted_total
            bump("dsf_prefetch_chunk_count", submitted_total)
        return submitted_total


# Global prefetcher instance
spatial_prefetcher = SpatialPrefetcher()


def start_prefetcher(tile_cacher):
    """Start the spatial prefetcher with the given tile cacher."""
    clear_shutdown_request()
    spatial_prefetcher.set_tile_cacher(tile_cacher)
    spatial_prefetcher.start()


def prefetch_dsf(dsf_path: str, max_chunks: Optional[int] = None) -> int:
    if not is_prefetch_runtime_allowed():
        return 0
    tiles = get_tiles_for_dsf(dsf_path)
    maptype_override = spatial_prefetcher._get_maptype_filter()
    if maptype_override:
        tiles = [
            (row, col, maptype_override, zoom)
            for row, col, _terrain_maptype, zoom in tiles
        ]
    return spatial_prefetcher.prefetch_tiles(
        tiles, max_chunks=max_chunks
    )


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
# 2. DynamicDDSCache - Stores pre-built DDS for instant serving
# 3. BackgroundDDSBuilder - Builds DDS from completed tiles in background
# ============================================================================

class _TrackedTile:
    """Internal tracking state for a tile being prefetched."""
    __slots__ = ('tile', 'zoom', 'expected_chunks', 'completed_chunks',
                 'start_time', 'completed_chunk_ids', 'build_triggered')

    def __init__(self, tile, zoom: int, expected_chunks: int):
        self.tile = tile
        self.zoom = zoom
        self.expected_chunks = expected_chunks
        self.completed_chunks = 0
        self.start_time = time.monotonic()
        self.completed_chunk_ids = set()  # Track which chunks reported complete
        self.build_triggered = False  # True once threshold build was fired


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
            on_tile_complete: Callback when chunks are ready.
                             Called with (tile_id, tile, partial, healing).
                             partial=True means threshold reached but not 100%.
                             healing=True means remaining chunks arrived after partial build.
                             If None, no callback is made.
        """
        self._lock = threading.Lock()
        self._tracked_tiles: Dict[str, _TrackedTile] = {}
        self._on_tile_complete = on_tile_complete
        self._max_tracked = 400  # Limit memory usage
        self._timeout_sec = 600  # Stop tracking after 10 minutes
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 60  # Cleanup every 60 seconds
        # Build threshold: trigger DDS build when this fraction of chunks is ready.
        # build_from_jpegs handles None entries for missing chunks; healing fills gaps later.
        self._build_threshold = 1.0  # Always require all chunks for quality
    
    def start_tracking(self, tile, zoom: int, submitted_count: int = 0) -> None:
        """
        Begin tracking a tile's native mipmap-0 chunk resolution.

        Called by SpatialPrefetcher when it starts prefetching a tile.
        If tile is already being tracked, this is a no-op.

        Args:
            tile: Tile object to track
            zoom: Max zoom level for this tile (tile.max_zoom)
            submitted_count: Deprecated compatibility argument. Completion is
                             now based on all native mipmap-0 chunks resolving,
                             not on a submitted subset.
        """
        if tile is None:
            return

        tile_id = tile.id
        tile_to_callback = None

        required_chunks = list(tile.chunks.get(tile.max_zoom, []))
        if not required_chunks:
            return

        with self._lock:
            # Already tracking this tile
            if tile_id in self._tracked_tiles:
                return

            total_expected = len(required_chunks)
            if total_expected == 0:
                return  # Nothing to track

            completed_ids = {
                chunk.chunk_id
                for chunk in required_chunks
                if chunk.ready.is_set()
            }

            if len(completed_ids) >= total_expected:
                tile_to_callback = tile
            else:
                # Enforce max tracked limit (evict oldest if needed)
                if len(self._tracked_tiles) >= self._max_tracked:
                    self._evict_oldest_unlocked()

                tracked = _TrackedTile(tile, zoom, total_expected)
                tracked.completed_chunk_ids = completed_ids
                tracked.completed_chunks = len(completed_ids)
                self._tracked_tiles[tile_id] = tracked
                log.debug(f"TileCompletionTracker: Started tracking {tile_id} "
                         f"(expecting {total_expected} native chunks, "
                         f"{len(completed_ids)} already ready)")

            # Periodic cleanup of stale entries
            self._maybe_cleanup_unlocked()

        if tile_to_callback is not None and self._on_tile_complete is not None:
            try:
                self._on_tile_complete(tile_id, tile_to_callback,
                                       partial=False,
                                       healing=False)
            except Exception as e:
                log.warning(f"TileCompletionTracker: Callback error for {tile_id}: {e}")
    
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
        callback_partial = False
        callback_healing = False

        with self._lock:
            tracked = self._tracked_tiles.get(tile_id)
            if tracked is None:
                # Not tracking this tile (not prefetched, or already completed)
                return

            # Avoid double-counting the same chunk
            chunk_key = chunk.chunk_id if chunk else None
            if chunk is None:
                return
            if chunk_key and chunk_key in tracked.completed_chunk_ids:
                return

            if chunk_key:
                tracked.completed_chunk_ids.add(chunk_key)
            tracked.completed_chunks += 1

            log.debug(f"TileCompletionTracker: {tile_id} chunk complete "
                     f"({tracked.completed_chunks}/{tracked.expected_chunks})")

            # Threshold-based triggering: fire early build at _build_threshold,
            # then fire healing when remaining chunks arrive.
            ratio = tracked.completed_chunks / tracked.expected_chunks if tracked.expected_chunks > 0 else 0
            if tracked.completed_chunks >= tracked.expected_chunks:
                # 100% complete
                tile_to_callback = tracked.tile
                del self._tracked_tiles[tile_id]
                if tracked.build_triggered:
                    # Already built at threshold — remaining chunks trigger healing
                    callback_healing = True
                    log.debug(f"TileCompletionTracker: {tile_id} COMPLETE - healing pass")
                else:
                    log.debug(f"TileCompletionTracker: {tile_id} COMPLETE - all chunks ready")
            elif ratio >= self._build_threshold and not tracked.build_triggered:
                # Threshold reached — trigger early build
                tracked.build_triggered = True
                tile_to_callback = tracked.tile
                callback_partial = True
                log.debug(f"TileCompletionTracker: {tile_id} THRESHOLD ({ratio:.0%}) - early build")
                # Keep tracking for healing when remaining chunks arrive

        # Call callback OUTSIDE the lock to avoid deadlocks
        if tile_to_callback is not None and self._on_tile_complete is not None:
            try:
                self._on_tile_complete(tile_id, tile_to_callback,
                                       partial=callback_partial,
                                       healing=callback_healing)
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



class BackgroundDDSBuilder:
    
    # Maximum queue depth (prevents unbounded memory growth)
    MAX_QUEUE_SIZE = 500
    
    def __init__(self, dds_cache,
                 build_interval_sec: float = 0.5,
                 max_workers: int = 2):
        """
        Args:
            dds_cache: DynamicDDSCache instance for persistent DDS storage
            build_interval_sec: Minimum time between submissions (rate limiting)
            max_workers: Number of parallel build workers
        """
        self._queue = PriorityQueue(maxsize=self.MAX_QUEUE_SIZE)
        self._queue_sequence = itertools.count()
        self._dds_cache = dds_cache
        self._build_interval = build_interval_sec
        self._max_workers = max_workers
        
        # Worker pool for parallel builds
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._coordinator_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Event to wake coordinator when work is available or a slot frees up
        self._work_event = threading.Event()

        # Stats (thread-safe via atomic operations)
        self._builds_completed = 0
        self._builds_failed = 0
        self._active_builds = 0
        self._active_lock = threading.Lock()
    
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
        self._work_event.set()  # Wake coordinator so it sees stop_event

        # Drain queue
        try:
            while True:
                self._queue.get_nowait()
        except Empty:
            pass

        # Stop coordinator
        if self._coordinator_thread is not None:
            try:
                self._queue.put_nowait((float('inf'), next(self._queue_sequence), None))  # Sentinel
            except Full:
                pass
            self._coordinator_thread.join(timeout=2.0)
            self._coordinator_thread = None

        # Shutdown executor — don't wait for in-flight builds since they are
        # speculative background work.  cancel_futures=True prevents queued
        # futures from starting; wait=False avoids blocking on builds that
        # may be stuck on network I/O or holding the native build semaphore.
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        
        log.info(f"BackgroundDDSBuilder stopped "
                f"(built={self._builds_completed}, failed={self._builds_failed})")
    
    def submit(self, tile, priority: float = PRIORITY_BACKGROUND_DDS) -> bool:
        """
        Submit a tile for background DDS building.
        
        Args:
            tile: Tile with all chunks downloaded
            priority: Queue priority (lower runs first; values here are
                background priority and yield to live builds)
            
        Returns:
            True if queued, False if queue is full or tile already cached
        """
        if tile is None:
            return False
        
        # Skip if already in DDS cache (allow through if healing needed)
        if self._dds_cache is not None and self._dds_cache.contains(tile.id, tile.max_zoom, tile):
            if not getattr(tile, '_dds_needs_healing', False):
                log.debug(f"BackgroundDDSBuilder: Skipping {tile.id} - already cached")
                return False
            log.debug(f"BackgroundDDSBuilder: healing tile {tile.id} passed through")
        
        try:
            self._queue.put_nowait((priority, next(self._queue_sequence), tile))
            self._work_event.set()  # Wake coordinator immediately
            log.debug(f"BackgroundDDSBuilder: Queued {tile.id} "
                     f"(queue size: {self._queue.qsize()})")
            return True
        except Full:
            log.debug(f"BackgroundDDSBuilder: Queue full, skipping {tile.id}")
            return False
    
    def _coordinator_loop(self) -> None:
        """
        Event-driven coordinator loop - dispatches tiles to worker pool.

        Wakes immediately when:
        - A tile is submitted to the queue (submit() signals _work_event)
        - A worker finishes and frees a slot (_build_tile_wrapper signals _work_event)

        Fills ALL available worker slots each cycle instead of rate-limiting
        to half the workers, maximizing CPU utilization for background builds.
        """
        while not self._stop_event.is_set() and not is_shutdown_requested():
            # Wait for work signal or timeout as fallback
            self._work_event.wait(timeout=self._build_interval)
            self._work_event.clear()

            if self._stop_event.is_set() or is_shutdown_requested():
                break

            # Fill all available worker slots
            with self._active_lock:
                available = self._max_workers - self._active_builds
            if available <= 0:
                continue

            batch_submitted = 0
            while batch_submitted < available:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break  # Queue exhausted

                # Sentinel value signals shutdown
                if item is None:
                    continue

                priority, _seq, tile = item
                if tile is None:
                    continue

                # Drop evicted tiles early to avoid holding
                # references longer than necessary
                if getattr(tile, '_closed', False):
                    bump('prebuilt_dds_skip_closed')
                    continue

                if self._executor is not None:
                    with self._active_lock:
                        self._active_builds += 1
                    self._executor.submit(
                        self._build_tile_wrapper, priority, tile
                    )
                    batch_submitted += 1
    
    def _build_tile_wrapper(self, priority, tile) -> None:
        """Wrapper for _build_tile_dds that handles exceptions and stats."""
        deferred = False
        try:
            if is_shutdown_requested():
                bump('prebuilt_dds_skip_shutdown')
                return

            # Skip if tile was evicted while queued/waiting
            if getattr(tile, '_closed', False):
                log.debug(
                    "BackgroundDDSBuilder: Skipping closed tile "
                    f"{tile.id}"
                )
                bump('prebuilt_dds_skip_closed')
                return
            _defer_background_build_if_live(tile)
            self._build_tile_dds(tile)
        except _BackgroundBuildDeferred:
            bump('background_build_deferred_live')
            deferred = self.submit(tile, priority=priority)
        finally:
            if not deferred:
                try:
                    tile._clear_mm0_promotion_pin()
                except Exception:
                    pass
            with self._active_lock:
                self._active_builds -= 1
            self._work_event.set()  # Signal coordinator a slot freed up
    
    def _try_streaming_prefetch_build(self, tile, tile_id: str, build_start: float) -> bool:
        """
        Build DDS for prefetch using streaming builder with fallback support.
        
        Key difference from live: NO TIME BUDGET.
        Takes as long as needed to apply all fallbacks for quality.
        
        Args:
            tile: Tile to build
            tile_id: Tile ID string
            build_start: Monotonic time when build started
        
        Returns:
            True if build succeeded, False to fall back to other methods
        """
        # Handle imports for both frozen (PyInstaller) and direct Python execution
        try:
            from autoortho.aopipeline.AoDDS import get_default_builder_pool
            from autoortho.aopipeline.fallback_resolver import FallbackResolver
        except ImportError:
            try:
                from aopipeline.AoDDS import get_default_builder_pool
                from aopipeline.fallback_resolver import FallbackResolver
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
            _defer_background_build_if_live(tile)

            # Get chunks for max zoom
            chunks = tile.chunks.get(tile.max_zoom, [])
            if not chunks:
                log.debug(f"BackgroundDDSBuilder: No chunks for {tile_id}")
                return False
            
            # Import fallback TimeBudget for use during transition
            # Handle imports for both frozen (PyInstaller) and direct Python execution
            try:
                from autoortho.aopipeline.fallback_resolver import TimeBudget as FBTimeBudget
            except ImportError:
                from aopipeline.fallback_resolver import TimeBudget as FBTimeBudget
            
            # Phase 1: Batch add all ready chunks at once
            ready_chunks = []
            pending_indices = []
            prefetch_mm0_missing = []
            prefetch_mm0_fallback = []
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
                _defer_background_build_if_live(tile)
                
                # === TRANSITION CHECK ===
                # If tile became live, use its time budget for remaining work
                time_budget = tile._tile_time_budget if tile._is_live else None
                
                if tile._is_live and time_budget and time_budget.exhausted:
                    builder.mark_missing(i)
                    prefetch_mm0_missing.append(i)
                    continue
                
                # Wait for chunk - but check for live transition periodically
                while not chunk.ready.is_set():
                    # Short wait to allow transition detection
                    chunk.ready.wait(timeout=0.1)
                    _defer_background_build_if_live(tile)
                    
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
                            prefetch_mm0_fallback.append(i)
                        else:
                            builder.mark_missing(i)
                            prefetch_mm0_missing.append(i)
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
                        prefetch_mm0_fallback.append(i)
                    else:
                        builder.mark_missing(i)
                        prefetch_mm0_missing.append(i)
            
            # Finalize directly to disk via DynamicDDSCache staging path
            if self._dds_cache is not None:
                _defer_background_build_if_live(tile)
                staging_path = self._dds_cache.get_staging_path(tile_id, tile.max_zoom, tile)
                if not staging_path:
                    return False
                with _native_build_context() as threads:
                    success, bytes_written = builder.finalize_to_file(
                        staging_path, max_threads=threads
                    )
                
                if success and bytes_written >= 128:
                    self._dds_cache.store_from_file(
                        tile_id, tile.max_zoom, staging_path, tile,
                        mm0_missing_indices=prefetch_mm0_missing or None,
                        mm0_fallback_indices=prefetch_mm0_fallback or None)
                    
                    build_time = (time.monotonic() - build_start) * 1000
                    status = builder.get_status()
                    self._builds_completed += 1
                    log.debug(f"BackgroundDDSBuilder: Streaming built {tile_id} in {build_time:.0f}ms "
                              f"(decoded={status['chunks_decoded']}, fallback={status['chunks_fallback']}, "
                              f"missing={status['chunks_missing']})")
                    bump('prebuilt_dds_builds_streaming')
                    
                    return True
            
            log.debug(f"BackgroundDDSBuilder: Streaming finalize failed for {tile_id}")
            return False

        except _BackgroundBuildDeferred:
            raise
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
        _defer_background_build_if_live(tile)
        
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
            _defer_background_build_if_live(tile)
        
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
        _defer_background_build_if_live(tile)
        
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
                    hybrid_mm0_missing = [
                        i for i, c in enumerate(chunks_for_hybrid)
                        if not getattr(c, 'data', None)
                    ]
                    if chunks_for_hybrid:
                        # ═══════════════════════════════════════════════════════════════
                        # DIRECT-TO-DISK OPTIMIZATION (Phase 1: ~65ms copy eliminated)
                        # ═══════════════════════════════════════════════════════════════
                        # Build directly to disk file via DynamicDDSCache staging path.
                        # Eliminates Python memory copy entirely.
                        # Flow: JPEG data → C decode → C compress → fwrite to disk
                        #
                        # This saves ~65ms per tile by avoiding:
                        # - Buffer → Python bytes copy
                        # - Python bytes → disk write
                        # ═══════════════════════════════════════════════════════════════
                        if (self._dds_cache is not None and
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
                                    _defer_background_build_if_live(tile)
                                    staging_path = self._dds_cache.get_staging_path(
                                        tile_id, tile.max_zoom, tile)
                                    if not staging_path:
                                        return
                                    
                                    with _native_build_context() as threads:
                                        result = native_dds.build_from_jpegs_to_file(
                                            jpeg_datas,
                                            staging_path,
                                            format=dxt_format,
                                            missing_color=missing_color,
                                            max_threads=threads
                                        )

                                    if result.success and result.bytes_written >= 128:
                                        self._dds_cache.store_from_file(
                                            tile_id, tile.max_zoom, staging_path, tile,
                                            mm0_missing_indices=hybrid_mm0_missing or None)
                                        build_time = (time.monotonic() - build_start) * 1000
                                        self._builds_completed += 1
                                        log.debug(f"BackgroundDDSBuilder: Direct-to-disk built {tile_id} "
                                                  f"in {build_time:.0f}ms ({result.bytes_written} bytes)")
                                        bump('prebuilt_dds_builds_direct')

                                        return
                                    else:
                                        log.debug(f"BackgroundDDSBuilder: Direct-to-disk failed for "
                                                  f"{tile_id}: {result.error}, trying buffer path")
                            except _BackgroundBuildDeferred:
                                raise
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
                                missing_color=missing_color,
                                buffer_priority=PRIORITY_PREFETCH,
                            )

                            if dds_bytes and len(dds_bytes) >= 128:
                                # Hybrid build succeeded - store in DDS cache
                                if self._dds_cache is not None:
                                    try:
                                        self._dds_cache.store(
                                            tile_id, tile.max_zoom, dds_bytes, tile,
                                            mm0_missing_indices=hybrid_mm0_missing or None)
                                    except Exception:
                                        pass
                                build_time = (time.monotonic() - build_start) * 1000
                                self._builds_completed += 1
                                log.debug(f"BackgroundDDSBuilder: Hybrid built {tile_id} in "
                                          f"{build_time:.0f}ms ({len(dds_bytes)} bytes)")
                                bump('prebuilt_dds_builds_hybrid')

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
                if (self._dds_cache is not None and
                    hasattr(native_dds, 'build_tile_to_file')):
                    try:
                        _defer_background_build_if_live(tile)
                        staging_path = self._dds_cache.get_staging_path(
                            tile_id, tile.max_zoom, tile)
                        if not staging_path:
                            return

                        with _native_build_context():
                            result = native_dds.build_tile_to_file(
                                cache_dir=tile.cache_dir,
                                row=tile.row,
                                col=tile.col,
                                maptype=tile.maptype,
                                zoom=tile.max_zoom,
                                output_path=staging_path,
                                chunks_per_side=tile.chunks_per_row,
                                format=dxt_format,
                                missing_color=missing_color
                            )

                        if result.success and result.bytes_written >= 128:
                            # Check which chunks were missing from cache (filled with missing_color by native build)
                            native_mm0_missing = None
                            if hasattr(tile, 'chunks') and tile.max_zoom in tile.chunks:
                                mm0_chunks = tile.chunks[tile.max_zoom]
                                missing = [i for i, c in enumerate(mm0_chunks)
                                           if not (c.ready.is_set() and c.data)]
                                if missing:
                                    native_mm0_missing = missing
                            self._dds_cache.store_from_file(
                                tile_id, tile.max_zoom, staging_path, tile,
                                mm0_missing_indices=native_mm0_missing)
                            build_time = (time.monotonic() - build_start) * 1000
                            self._builds_completed += 1
                            log.debug(f"BackgroundDDSBuilder: Native direct-to-disk built {tile_id} "
                                      f"in {build_time:.0f}ms ({result.bytes_written} bytes)")
                            bump('prebuilt_dds_builds_native_direct')

                            return
                        else:
                            log.debug(f"BackgroundDDSBuilder: Native direct-to-disk failed for "
                                      f"{tile_id}: {result.error}, trying buffer path")
                    except _BackgroundBuildDeferred:
                        raise
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
                _defer_background_build_if_live(tile)
                
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
                    _defer_background_build_if_live(tile)
                    with _native_build_context():
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
                        dds_bytes = result.to_bytes()
                        if self._dds_cache is not None:
                            try:
                                native_mm0_missing = None
                                if hasattr(tile, 'chunks') and tile.max_zoom in tile.chunks:
                                    mm0_chunks = tile.chunks[tile.max_zoom]
                                    missing = [i for i, c in enumerate(mm0_chunks)
                                               if not (c.ready.is_set() and c.data)]
                                    if missing:
                                        native_mm0_missing = missing
                                self._dds_cache.store(
                                    tile_id, tile.max_zoom, dds_bytes, tile,
                                    mm0_missing_indices=native_mm0_missing)
                            except Exception:
                                pass
                        build_time = (time.monotonic() - build_start) * 1000
                        self._builds_completed += 1
                        log.debug(f"BackgroundDDSBuilder: Native built {tile_id} "
                                  f"in {build_time:.0f}ms ({len(dds_bytes)} bytes)")
                        bump('prebuilt_dds_builds_native_buffered')
                        
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
            _defer_background_build_if_live(tile)
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
            
            # Skip if already in cache (race condition check, allow healing tiles)
            if self._dds_cache is not None and self._dds_cache.contains(tile_id, tile.max_zoom, tile):
                if not getattr(tile, '_dds_needs_healing', False):
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
            _defer_background_build_if_live(tile)
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
                _defer_background_build_if_live(tile)
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
            _defer_background_build_if_live(tile)
            dds_bytes = temp_dds.read(temp_dds.total_size)
            
            if not dds_bytes or len(dds_bytes) < 128:
                log.debug(f"BackgroundDDSBuilder: {tile_id} - DDS read failed")
                self._builds_failed += 1
                return
            
            # Step 6: Store in DDS cache
            if self._dds_cache is not None:
                try:
                    mm0_grid = tile.chunks.get(tile.max_zoom)
                    mm0_chunks = (
                        mm0_grid.ensure_all()
                        if mm0_grid is not None
                        else []
                    )
                    python_mm0_missing = [i for i, c in enumerate(mm0_chunks)
                                          if not (c.ready.is_set() and c.data)]
                    self._dds_cache.store(tile_id, tile.max_zoom, dds_bytes, tile,
                                         mm0_missing_indices=python_mm0_missing or None)
                except Exception:
                    pass
            
            build_time = (time.monotonic() - build_start) * 1000
            self._builds_completed += 1
            
            log.debug(f"BackgroundDDSBuilder: Built {tile_id} in {build_time:.0f}ms "
                     f"({len(dds_bytes)} bytes, {len(mipmap_images)} native mipmaps)")
            bump('prebuilt_dds_builds')
            
        except _BackgroundBuildDeferred:
            raise
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
background_dds_builder: Optional[BackgroundDDSBuilder] = None
tile_completion_tracker: Optional[TileCompletionTracker] = None

# Persistent DDS cache (cross-session) and disk budget manager
dynamic_dds_cache = None       # type: ignore[assignment]  # DynamicDDSCache instance
disk_budget_manager = None     # type: ignore[assignment]  # DiskBudgetManager instance
_cache_init_thread = None
_persist_partial_dds: bool = False


def _collect_healing_jpegs(tile, missing_indices):
    """Collect JPEG bytes for missing chunk indices from memory or disk cache.

    Returns dict mapping chunk index → JPEG bytes, or None if not all available.
    """
    max_zoom = getattr(tile, 'max_zoom', None)
    if max_zoom is None:
        return None
    grid = tile.chunks.get(max_zoom)
    chunks = grid if grid is not None else ()
    chunk_jpegs = {}
    for idx in missing_indices:
        if idx < len(chunks) and chunks[idx].data:
            chunk_jpegs[idx] = chunks[idx].data
        elif idx < len(chunks):
            if chunks[idx].get_cache() and chunks[idx].data:
                chunk_jpegs[idx] = chunks[idx].data
            else:
                return None
        else:
            return None
    return chunk_jpegs


def _dispatch_healing(tile):
    """Dispatch in-place healing for a tile with missing or fallback chunks.

    Missing chunks (showing missing_color) are collected first so they are
    patched even when fallback-chunk JPEGs are not yet available.
    """
    missing = getattr(tile, '_dds_missing_indices', [])
    fallback = getattr(tile, '_dds_fallback_indices', [])
    if (not missing and not fallback) or dynamic_dds_cache is None:
        return

    chunk_jpegs = {}

    # Phase 1: missing chunks (high priority -- these show missing_color)
    if missing:
        missing_jpegs = _collect_healing_jpegs(tile, missing)
        if missing_jpegs is not None:
            chunk_jpegs.update(missing_jpegs)
        else:
            log.debug(f"Healing: not all missing-chunk JPEGs available for {tile.id}, deferring missing")

    # Phase 2: fallback chunks (lower priority -- these have low-res data)
    if fallback:
        fallback_jpegs = _collect_healing_jpegs(tile, fallback)
        if fallback_jpegs is not None:
            chunk_jpegs.update(fallback_jpegs)
        else:
            log.debug(f"Healing: not all fallback-chunk JPEGs available for {tile.id}, skipping fallback healing")

    if not chunk_jpegs:
        return

    t = threading.Thread(
        target=dynamic_dds_cache.patch_missing_chunks,
        args=(tile.id, tile.max_zoom, tile, chunk_jpegs),
        daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════════════
# Network Healing: re-download missing/fallback chunks on tile reopen
# ═══════════════════════════════════════════════════════════════════

# Semaphore to limit concurrent network healing threads
_network_healing_semaphore = threading.Semaphore(2)


def _dispatch_network_healing(tile, indices_to_heal):
    """Dispatch background network healing for specific chunk indices.

    Called by DynamicDDSCache.load() when disk-only healing cannot cover
    all missing/fallback chunks.  Spawns a daemon thread that downloads
    the chunks through the normal pipeline (download → fallback → patch).
    """
    if not indices_to_heal or dynamic_dds_cache is None:
        # Clean up the in-progress guard since we won't actually heal
        if dynamic_dds_cache is not None:
            dynamic_dds_cache.clear_network_healing(tile.id, tile.max_zoom)
        return
    if getattr(tile, '_closed', False):
        dynamic_dds_cache.clear_network_healing(tile.id, tile.max_zoom)
        return
    if chunk_getter is None:
        dynamic_dds_cache.clear_network_healing(tile.id, tile.max_zoom)
        return

    t = threading.Thread(
        target=_do_network_healing,
        args=(tile, list(indices_to_heal)),
        daemon=True,
        name=f"net_heal_{tile.id}")
    t.start()


def _do_network_healing(tile, indices_to_heal):
    """Background thread: download missing chunks and patch the cached DDS.

    Follows the same flow as the streaming pipeline:
    1. Create Chunk objects for each missing index
    2. Submit to chunk_getter for download
    3. Wait with generous timeout (background — no urgency)
    4. Run fallback resolver for any that failed
    5. Patch the DDS via dynamic_dds_cache.patch_missing_chunks()
    """
    tile_id = tile.id
    max_zoom = tile.max_zoom

    if not _network_healing_semaphore.acquire(timeout=10.0):
        log.debug(f"Network healing: semaphore timeout for {tile_id}, deferring")
        if dynamic_dds_cache is not None:
            dynamic_dds_cache.clear_network_healing(tile_id, max_zoom)
        return

    try:
        if is_live_building():
            bump('network_healing_deferred_live')
            return

        if getattr(tile, '_closed', False):
            return

        chunks_per_row = getattr(tile, 'chunks_per_row', 0)
        if not chunks_per_row:
            return

        # Background healing uses a generous timeout — no frame deadline
        healing_timeout = max(10.0, float(getattr(CFG.autoortho, 'maxwait', 2.0)) * 3)

        # ── Step 1: Create Chunk objects and submit for download ──
        heal_chunks = {}  # idx -> Chunk
        for idx in indices_to_heal:
            if getattr(tile, '_closed', False):
                return
            cx = idx % chunks_per_row
            cy = idx // chunks_per_row
            col = tile.col + cx
            row = tile.row + cy
            chunk = Chunk(col, row, tile.maptype, max_zoom,
                          priority=PRIORITY_NETWORK_HEALING,
                          cache_dir=tile.cache_dir,
                          tile_id=None)  # No completion tracking for healing chunks
            chunk.prefetch = True
            heal_chunks[idx] = chunk

        # Submit chunks that aren't already cached
        if chunk_getter is None:
            return
        for idx, chunk in heal_chunks.items():
            if not chunk.ready.is_set():
                chunk_getter.submit(chunk)

        # ── Step 2: Wait for downloads ──
        deadline = time.monotonic() + healing_timeout
        for idx, chunk in heal_chunks.items():
            if getattr(tile, '_closed', False):
                for c in heal_chunks.values():
                    c.cancelled = True
                return
            remaining = max(0.05, deadline - time.monotonic())
            chunk.ready.wait(timeout=remaining)

        # ── Step 3: Collect results, run fallback for failures ──
        chunk_jpegs = {}
        failed_indices = []

        for idx, chunk in heal_chunks.items():
            if chunk.ready.is_set() and chunk.data:
                chunk_jpegs[idx] = chunk.data
            else:
                failed_indices.append(idx)

        if failed_indices and not getattr(tile, '_closed', False):
            try:
                from autoortho.aopipeline.fallback_resolver import FallbackResolver, TimeBudget as FBTimeBudget
            except ImportError:
                try:
                    from aopipeline.fallback_resolver import FallbackResolver, TimeBudget as FBTimeBudget
                except ImportError:
                    FallbackResolver = None

            if FallbackResolver is not None:
                fallback_level_str = str(getattr(CFG.autoortho, 'fallback_level', 'cache')).lower()
                if fallback_level_str == 'none':
                    fb_level = 0
                elif fallback_level_str == 'full':
                    fb_level = 2
                else:
                    fb_level = 1

                if fb_level > 0:
                    resolver = FallbackResolver(
                        cache_dir=tile.cache_dir,
                        maptype=tile.maptype,
                        tile_col=tile.col,
                        tile_row=tile.row,
                        tile_zoom=max_zoom,
                        fallback_level=fb_level,
                        max_mipmap=getattr(tile, 'max_mipmap', 4),
                        downloader=None)

                    fallback_timeout = float(getattr(CFG.autoortho, 'fallback_timeout', 30.0))
                    from concurrent.futures import ThreadPoolExecutor, as_completed

                    def _resolve_one(idx):
                        chunk_col = tile.col + (idx % chunks_per_row)
                        chunk_row = tile.row + (idx // chunks_per_row)
                        rgba = resolver.resolve(
                            chunk_col, chunk_row, max_zoom,
                            target_mipmap=0,
                            time_budget=FBTimeBudget(fallback_timeout))
                        return idx, rgba

                    max_workers = min(4, len(failed_indices))
                    try:
                        with ThreadPoolExecutor(max_workers=max_workers) as executor:
                            futures = {executor.submit(_resolve_one, i): i for i in failed_indices}
                            for future in as_completed(futures, timeout=fallback_timeout):
                                try:
                                    idx, rgba = future.result(timeout=1.0)
                                    if rgba:
                                        # Fallback returns RGBA bytes — encode to JPEG
                                        # so patch_missing_chunks can decode uniformly.
                                        # Minor quality loss acceptable for already-lower-res
                                        # fallback data.
                                        try:
                                            from PIL import Image
                                            img = Image.frombytes("RGBA", (256, 256), rgba)
                                            buf = BytesIO()
                                            img.convert("RGB").save(buf, format="JPEG", quality=90)
                                            chunk_jpegs[idx] = buf.getvalue()
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                    except Exception:
                        pass

        if getattr(tile, '_closed', False):
            return

        # ── Step 4: Patch the DDS ──
        if chunk_jpegs and dynamic_dds_cache is not None:
            healed_count = len(chunk_jpegs)
            total_count = len(indices_to_heal)
            log.info(f"Network healing: patching {healed_count}/{total_count} chunks for {tile_id}")
            dynamic_dds_cache.patch_missing_chunks(tile_id, max_zoom, tile, chunk_jpegs)
        else:
            log.debug(f"Network healing: no chunks recovered for {tile_id}")

    except Exception as e:
        log.debug(f"Network healing: failed for {tile_id}: {e}")
    finally:
        _network_healing_semaphore.release()
        if dynamic_dds_cache is not None:
            dynamic_dds_cache.clear_network_healing(tile_id, max_zoom)


def _on_tile_complete_callback(tile_id: str, tile,
                               partial: bool = False,
                               healing: bool = False) -> None:
    """
    Callback invoked when chunks reach the build threshold or all complete.

    Args:
        tile_id: Tile identifier
        tile: Tile object
        partial: True when threshold reached but not 100% — build with available chunks
        healing: True when remaining chunks arrived after a partial build — patch the DDS
    """
    if healing:
        # Remaining chunks arrived after partial build — dispatch healing
        _dispatch_healing(tile)
    elif getattr(tile, '_dds_needs_healing', False) and dynamic_dds_cache is not None:
        _dispatch_healing(tile)
    elif background_dds_builder is not None:
        if getattr(tile, '_mm0_promotion_queued', False):
            tile._pin_mm0_promotion()
        background_dds_builder.submit(tile)


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
    global background_dds_builder, tile_completion_tracker
    global dynamic_dds_cache, disk_budget_manager
    global _cache_init_thread

    # Prevent duplicate initialization (Windows/Linux: all mounts share one process)
    if background_dds_builder is not None:
        log.debug("Predictive DDS already initialized, skipping")
        return

    # Check if enabled. Predictive DDS is fed by spatial prefetch; keep the
    # runtime dependency explicit so a disabled prefetch setting really means
    # no speculative DDS work.
    prefetch_enabled = getattr(CFG.autoortho, 'prefetch_enabled', True)
    if isinstance(prefetch_enabled, str):
        prefetch_enabled = prefetch_enabled.lower() in ('true', '1', 'yes', 'on')
    if not prefetch_enabled:
        log.info("Predictive DDS generation disabled because spatial prefetching is disabled")
        return

    enabled = getattr(CFG.autoortho, 'predictive_dds_enabled', True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ('true', '1', 'yes', 'on')
    
    if not enabled:
        log.info("Predictive DDS generation disabled by configuration")
        return
    
    build_interval_ms = int(getattr(CFG.autoortho, 'predictive_dds_build_interval_ms', 250))
    build_interval_ms = max(50, min(2000, build_interval_ms))
    build_interval_sec = build_interval_ms / 1000.0

    background_builder_workers = int(getattr(CFG.autoortho, 'background_builder_workers', 8))
    background_builder_workers = max(1, min(16, background_builder_workers))  # 1-16 workers
    
    live_builder_concurrency = int(getattr(CFG.autoortho, 'live_builder_concurrency', 8))
    live_builder_concurrency = max(1, min(32, live_builder_concurrency))  # 1-32 workers
    
    total_builders = background_builder_workers + live_builder_concurrency
    
    # Initialize decoder pool sized for coordinated thread budget
    # With dynamic thread coordination, total OpenMP threads ~ cpu_count
    try:
        from autoortho.aopipeline import AoDDS
        decoder_pool_size = AoDDS.calculate_decoder_pool_size(total_builders)
        if AoDDS.init_decoder_pool(decoder_pool_size):
            log.info(f"Decoder pool initialized: {decoder_pool_size} decoders "
                    f"(cpu×1.5={CURRENT_CPU_COUNT}×1.5, min={total_builders} builders)")
        else:
            log.debug("Decoder pool already initialized (using existing size)")
    except ImportError:
        log.debug("AoDDS not available, using default decoder pool")
    except Exception as e:
        log.warning(f"Failed to initialize decoder pool: {e}")
    
    # ═══════════════════════════════════════════════════════════════════
    # Initialize persistent Dynamic DDS Cache (cross-session)
    # Must be created before BackgroundDDSBuilder which depends on it.
    # ═══════════════════════════════════════════════════════════════════
    persistent_dds_mb = int(getattr(CFG.autoortho, 'persistent_dds_cache_mb', 0))
    try:
        try:
            from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        except ImportError:
            from aopipeline.dynamic_dds_cache import DynamicDDSCache

        cache_dir = str(CFG.paths.cache_dir)
        dynamic_dds_cache = DynamicDDSCache(
            cache_dir=cache_dir,
            max_size_mb=persistent_dds_mb,
            enabled=True,
            cleanup_source_jpegs_after_store=_get_bool_config(
                CFG.autoortho, "cleanup_source_jpegs_after_dds", False
            ),
            maintenance_workers=int(
                getattr(CFG.autoortho, "dds_cache_maintenance_workers", 2)
            ),
        )

        # Wire network healing callback so cache can trigger downloads
        dynamic_dds_cache._network_heal_callback = _dispatch_network_healing

        size_desc = f"max={persistent_dds_mb}MB" if persistent_dds_mb > 0 else "unlimited"
        log.info(f"Dynamic DDS cache initialized ({size_desc})")
    except Exception as e:
        log.warning(f"Failed to initialize Dynamic DDS cache: {e}")
    
    background_dds_builder = BackgroundDDSBuilder(
        dds_cache=dynamic_dds_cache,
        build_interval_sec=build_interval_sec,
        max_workers=background_builder_workers
    )
    
    tile_completion_tracker = TileCompletionTracker(
        on_tile_complete=_on_tile_complete_callback
    )
    
    # ═══════════════════════════════════════════════════════════════════
    # Initialize Disk Budget Manager
    # ═══════════════════════════════════════════════════════════════════
    budget_enabled = getattr(CFG.autoortho, 'disk_budget_enabled', True)
    if isinstance(budget_enabled, str):
        budget_enabled = budget_enabled.lower() in ('true', '1', 'yes', 'on')
    
    if budget_enabled:
        try:
            try:
                from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
            except ImportError:
                from aopipeline.disk_budget_manager import DiskBudgetManager
            
            total_budget_gb = int(getattr(CFG.cache, 'file_cache_size', 30))
            total_budget_mb = total_budget_gb * 1024  # GB -> MB
            dds_pct = int(getattr(CFG.autoortho, 'dds_budget_pct', 40))
            
            cache_dir = str(CFG.paths.cache_dir)
            disk_budget_manager = DiskBudgetManager(
                cache_dir=cache_dir,
                total_budget_mb=total_budget_mb,
                dds_budget_pct=dds_pct,
                dds_cache=dynamic_dds_cache
            )
            
            if dynamic_dds_cache is not None:
                dynamic_dds_cache._budget_manager = disk_budget_manager
            log.info(f"Disk budget manager initialized (total={total_budget_gb}GB, dds_pct={dds_pct}%)")
        except Exception as e:
            log.warning(f"Failed to initialize Disk Budget Manager: {e}")
            disk_budget_manager = None
    else:
        log.info("Disk budget enforcement disabled by config")

    def _initialize_disk_caches():
        if dynamic_dds_cache is not None:
            dynamic_dds_cache.scan_existing()
            dynamic_dds_cache.migrate_uncompressed()
        if disk_budget_manager is not None:
            disk_budget_manager.initial_scan()

    _cache_init_thread = threading.Thread(
        target=_initialize_disk_caches,
        daemon=True,
        name="dds-cache-init",
    )
    _cache_init_thread.start()

    global _persist_partial_dds
    _persist_partial_dds = _get_bool_config(
        CFG.autoortho, 'persist_partial_dds_cache', False)

    # Start the builder thread
    background_dds_builder.start()
    
    size_desc = (
        f"{persistent_dds_mb}MB"
        if persistent_dds_mb > 0
        else "disk-budget managed"
    )
    log.info(
        "Predictive DDS generation started "
        f"(persistent_cache={size_desc}, interval={build_interval_ms}ms)"
    )


def stop_predictive_dds() -> None:
    """Stop the predictive DDS generation system and cleanup disk cache."""
    global background_dds_builder, tile_completion_tracker
    global dynamic_dds_cache, disk_budget_manager
    global _cache_init_thread
    
    if background_dds_builder is not None:
        stats = background_dds_builder.stats
        background_dds_builder.stop()
        log.info(f"BackgroundDDSBuilder: {stats['builds_completed']} tiles built, "
                f"{stats['builds_failed']} failed")

    if _cache_init_thread is not None and _cache_init_thread.is_alive():
        _cache_init_thread.join(timeout=10.0)
        if _cache_init_thread.is_alive():
            log.warning("DDS cache initialization did not stop within 10 seconds")
    
    # Log dynamic DDS cache stats on shutdown
    if dynamic_dds_cache is not None:
        try:
            stats = dynamic_dds_cache.stats
            log.info(f"Dynamic DDS cache: {stats['hits']} hits, {stats['misses']} misses, "
                    f"{stats['hit_rate']:.1f}% hit rate, {stats['disk_usage_mb']:.1f}MB used, "
                    f"{stats['entries']} entries, {stats['upgrades']} ZL upgrades")
        except Exception:
            pass
        try:
            dynamic_dds_cache.close()
        except Exception as exc:
            log.warning("Dynamic DDS cache shutdown failed: %s", exc)
    
    # Log disk budget stats on shutdown
    if disk_budget_manager is not None:
        try:
            report = disk_budget_manager.usage_report
            log.info(f"Disk budget: DDS={report['dds_usage_mb']:.0f}/{report['dds_budget_mb']:.0f}MB, "
                    f"JPEGs={report['jpeg_usage_mb']:.0f}/{report['jpeg_budget_mb']:.0f}MB")
        except Exception:
            pass
    
    # Clear references
    background_dds_builder = None
    tile_completion_tracker = None
    dynamic_dds_cache = None
    disk_budget_manager = None
    _cache_init_thread = None


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
MAX_BROKER_TIMEOUT_ATTEMPTS = 3

# Maptypes whose URL embeds a rotating server number; used only for logging.
MAPTYPES_WITH_SERVER = ("YNDX", "EOX", "GO2")


class _NetworkRequest(object):
    """A single provider HTTP request prepared by :meth:`Chunk.begin_network_attempt`.

    ``delay`` is the backoff the caller must observe *before* issuing the
    request.  It is reported instead of slept so async dispatchers can schedule
    it without occupying a thread.
    """

    __slots__ = (
        "url", "headers", "timeout", "delay", "server", "idx",
        "apple_token_generation", "apple_retried",
    )

    def __init__(self, url, headers, timeout, delay, server, idx,
                 apple_token_generation, apple_retried=False):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.delay = delay
        self.server = server
        self.idx = idx
        self.apple_token_generation = apple_token_generation
        self.apple_retried = apple_retried


class _AttemptOutcome(object):
    """Result of applying one HTTP result to a chunk.

    ``resolved`` mirrors the legacy ``Chunk.get()`` return value: True means
    the chunk reached a terminal state and the worker must stop retrying.
    """

    __slots__ = ("resolved", "retry_request", "requeue_delay")

    def __init__(self, resolved=False, retry_request=None, requeue_delay=0.0):
        self.resolved = resolved
        self.retry_request = retry_request
        self.requeue_delay = requeue_delay


class CacheProbeCoordinator:
    """Bounded process-local hint index for cache path probes."""

    UNKNOWN = "unknown"
    PRESENT = "present"
    MISSING = "missing"
    WRITE_PENDING = "write_pending"

    def __init__(self, max_entries=16384, negative_ttl=2.0):
        self._max_entries = max(128, int(max_entries))
        self._negative_ttl = max(0.05, float(negative_ttl))
        self._entries = OrderedDict()
        self._probing = {}
        self._recent_results = OrderedDict()
        self._recent_result_ttl = 0.25
        self._recent_result_limit = 64
        self._lock = threading.Lock()

    def _set(self, path, state, expires=0.0):
        self._entries[path] = (state, expires)
        self._entries.move_to_end(path)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def mark_present(self, path):
        with self._lock:
            self._set(path, self.PRESENT)

    def mark_write_pending(self, path):
        with self._lock:
            self._set(path, self.WRITE_PENDING)

    def mark_missing(self, path):
        with self._lock:
            self._set(
                path,
                self.MISSING,
                time.monotonic() + self._negative_ttl,
            )

    def invalidate(self, path):
        with self._lock:
            self._entries.pop(path, None)

    def probe(self, path, reader):
        """Return reader bytes or ``None`` while coalescing concurrent misses."""
        waited = False
        with self._lock:
            while path in self._probing:
                waited = True
                self._probing[path].wait()
            if waited:
                bump("cache_probe_coalesced")
                recent = self._recent_results.get(path)
                if (
                    recent is not None
                    and recent[1] > time.monotonic()
                ):
                    self._recent_results.move_to_end(path)
                    return recent[0]

            now = time.monotonic()
            state, expires = self._entries.get(
                path,
                (self.UNKNOWN, 0.0),
            )
            if state == self.MISSING and expires > now:
                self._entries.move_to_end(path)
                bump("cache_index_negative_hit")
                return None
            if state == self.PRESENT:
                bump("cache_index_positive_hit")
            elif state == self.WRITE_PENDING:
                bump("cache_index_write_pending_hit")

            condition = threading.Condition(self._lock)
            self._probing[path] = condition

        data = None
        error = None
        try:
            bump("cache_paths_probed")
            data = reader()
        except Exception as exc:
            error = exc

        with self._lock:
            self._recent_results[path] = (
                data,
                time.monotonic() + self._recent_result_ttl,
            )
            self._recent_results.move_to_end(path)
            while len(self._recent_results) > self._recent_result_limit:
                self._recent_results.popitem(last=False)
            if data:
                self._set(path, self.PRESENT)
            else:
                self._set(
                    path,
                    self.MISSING,
                    time.monotonic() + self._negative_ttl,
                )
            condition = self._probing.pop(path)
            condition.notify_all()
        if error is not None:
            raise error
        return data


_chunk_cache_index = CacheProbeCoordinator()


class _ChunkReadyEvent(threading.Event):
    def __init__(self, condition=None):
        super().__init__()
        self._condition = condition

    def set(self):
        super().set()
        if self._condition is not None:
            with self._condition:
                self._condition.notify_all()


class ChunkGrid:
    """Sparse canonical chunk grid with stable logical indices."""

    def __init__(
        self,
        *,
        col,
        row,
        width,
        height,
        zoom,
        maptype,
        cache_dir,
        tile_id,
    ):
        self.col = int(col)
        self.row = int(row)
        self.width = int(width)
        self.height = int(height)
        self.zoom = int(zoom)
        self.maptype = maptype
        self.cache_dir = cache_dir
        self.tile_id = tile_id
        self.logical_length = self.width * self.height
        self._slots = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition()

    def _new_chunk(self, index):
        row_offset, col_offset = divmod(index, self.width)
        return Chunk(
            self.col + col_offset,
            self.row + row_offset,
            self.maptype,
            self.zoom,
            cache_dir=self.cache_dir,
            tile_id=self.tile_id,
            skip_cache_check=True,
            completion_condition=self._condition,
        )

    @staticmethod
    def _probe_chunks(chunks):
        if not chunks:
            return
        bump("cache_paths_probed", len(chunks))
        cached_data = _batch_read_cache_files(
            [chunk.cache_path for chunk in chunks]
        )
        for chunk in chunks:
            data = cached_data.get(chunk.cache_path) if cached_data else None
            if data:
                chunk.set_cached_data(data)
                _chunk_cache_index.mark_present(chunk.cache_path)
            else:
                _chunk_cache_index.mark_missing(chunk.cache_path)
                chunk.get_cache()

    def ensure_range(self, start, end):
        start = max(0, int(start))
        end = min(self.logical_length, max(start, int(end)))
        created = []
        with self._lock:
            for index in range(start, end):
                if index not in self._slots:
                    chunk = self._new_chunk(index)
                    self._slots[index] = chunk
                    created.append(chunk)
        if created:
            bump("logical_chunks_materialized", len(created))
            profile_gauge("chunk_grid.materialized", len(self._slots))
            self._probe_chunks(created)
        return [self._slots[index] for index in range(start, end)]

    def ensure_rows(self, start_row, end_row):
        start_row = max(0, int(start_row))
        end_row = min(self.height - 1, int(end_row))
        if end_row < start_row:
            return []
        return self.ensure_range(
            start_row * self.width,
            (end_row + 1) * self.width,
        )

    def ensure_all(self):
        return self.ensure_range(0, self.logical_length)

    def get(self, index):
        index = int(index)
        if index < 0:
            index += self.logical_length
        if not 0 <= index < self.logical_length:
            raise IndexError(index)
        return self.ensure_range(index, index + 1)[0]

    def materialized(self):
        with self._lock:
            return tuple(
                self._slots[index]
                for index in sorted(self._slots)
            )

    def materialized_items(self):
        with self._lock:
            return tuple(sorted(self._slots.items()))

    def wait_for(self, chunks, deadline):
        with self._condition:
            while True:
                pending = [chunk for chunk in chunks if not chunk.ready.is_set()]
                if not pending:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

    def close(self):
        for chunk in self.materialized():
            chunk.close()
        with self._lock:
            self._slots.clear()

    def __len__(self):
        return self.logical_length

    def __iter__(self):
        return iter(self.materialized())

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self.logical_length)
            if step != 1:
                return [self.get(index) for index in range(start, stop, step)]
            return self.ensure_range(start, stop)
        return self.get(key)


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

    def __init__(self, col, row, maptype, zoom, priority=None, cache_dir='.cache', tile_id=None,
                 skip_cache_check=False, completion_condition=None):
        self.col = col
        self.row = row
        self.zoom = zoom
        self.maptype = maptype
        self.cache_dir = cache_dir
        self.tile_id = tile_id  # Parent tile ID for completion tracking
        
        # Hack override maptype
        #self.maptype = "BI"

        # Set priority: use provided value or default to zoom level.
        # Priority 0 is valid and means live/foreground work.
        self.priority = zoom if priority is None else priority
        self.chunk_id = f"{col}_{row}_{zoom}_{maptype}"
        self.ready = _ChunkReadyEvent(completion_condition)
        self.ready.clear()
        self.download_started = threading.Event()  # Signal when download thread begins
        self.download_started.clear()
        if maptype == "Null":
            self.maptype = "EOX"

        # Failure tracking for circuit breaker
        self.permanent_failure = False
        self.failure_reason = None
        self.retry_count = 0
        self.broker_timeout_count = 0

        # Coalescing flags to prevent duplicate submissions
        self.in_queue = False
        self.in_flight = False

        # Cancellation flag: set by caller when it gives up waiting.
        # Worker checks this before each retry and abandons the download,
        # freeing the worker slot for other chunks.
        self.cancelled = False
        self.prefetch = False
        self._broker_request_id = None

        self.cache_path = os.path.join(self.cache_dir, f"{self.chunk_id}.jpg")

        self.lt_cache_dir = _resolve_lt_cache_dir()
        self.lt_cache_path = (
            _lt_chunk_path(self.lt_cache_dir, self.chunk_id)
            if self.lt_cache_dir else None
        )
        
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

    def _read_local_cache(self):
        cache_file = Path(self.cache_path)
        data = None
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
                return None
            except FileNotFoundError:
                return None
            except OSError as e:
                winerr = getattr(e, 'winerror', None)
                if winerr in (5, 32, 33) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                log.debug(f"OSError reading cache {self}: {e}")
                return None
        if not data or not _is_jpeg(data[:3]):
            if data:
                log.info(
                    f"Loading file {self} not a JPEG! {data[:3]} "
                    f"path: {self.cache_path}"
                )
            return None
        try:
            os.utime(self.cache_path, None)
        except (FileNotFoundError, PermissionError):
            pass
        return data

    def get_cache(self):
        data = _chunk_cache_index.probe(
            self.cache_path,
            self._read_local_cache,
        )
        if data:
            bump('chunk_hit')
            self.data = data
            return True

        bump('chunk_miss')
        self.data = None
        if self.lt_cache_path and _lt_available(self.lt_cache_dir):
            data = _lt_read_bytes(self.lt_cache_path)
            if data and _is_jpeg(data[:3]):
                self.data = data
                bump('chunk_lt_hit')
                log.debug(f"LT cache hit for {self}")
                _chunk_cache_index.mark_write_pending(self.cache_path)
                if not _submit_bounded_cache_write(
                    _cache_write_executor,
                    self._save_to_dir,
                    self.cache_dir,
                    data,
                    byte_count=len(data),
                    kind="local",
                ):
                    self._save_to_dir(self.cache_dir, data)
                return True
        return False

    def _save_to_dir(self, target_dir, data=None):
        data = self.data if data is None else data
        if not data:
            return

        if target_dir == self.lt_cache_dir:
            target_path = self.lt_cache_path
            if not target_path:
                return
        else:
            target_path = os.path.join(target_dir, f"{self.chunk_id}.jpg")

        if not _atomic_write(target_path, data):
            if target_dir == self.lt_cache_dir:
                # EPERM = mount is read-only (NFS, NTFS, TCC). Back off 5 min.
                # Other failures = transient; use standard 30s interval.
                last_err = _atomic_write_last_errno.get(os.path.dirname(target_path))
                retry = 300.0 if last_err == errno.EPERM else _LT_CACHE_RETRY_INTERVAL
                if last_err == errno.EPERM:
                    log.error(
                        f"LT cache write denied (EPERM) at {target_dir} — "
                        "check NFS export options or filesystem permissions. "
                        f"Disabling LT writes for {retry:.0f}s."
                    )
                _lt_mark_unavailable(retry)
            return

        if target_dir == self.lt_cache_dir:
            bump('chunk_lt_write')
        elif target_dir == self.cache_dir and disk_budget_manager is not None:
            disk_budget_manager.account_jpeg(len(data))

    def save_cache(self, data=None):
        data = self.data if data is None else data
        if not data:
            return

        # Skip if the parent cache dir was nuked (e.g. diagnose temp cleanup).
        if not os.path.exists(self.cache_dir):
            log.debug(f"Cache directory gone for {self}, skipping save")
            return

        _chunk_cache_index.mark_write_pending(self.cache_path)
        if not _atomic_write(self.cache_path, data):
            return

        if disk_budget_manager is not None:
            disk_budget_manager.account_jpeg(len(data))
        if self.lt_cache_dir:
            if not _submit_bounded_cache_write(
                _lt_cache_write_executor,
                self._save_to_dir,
                self.lt_cache_dir,
                data,
                byte_count=len(data),
                kind="lt",
            ):
                bump("lt_cache_write_dropped")

    def _notify_ready(self):
        try:
            if tile_completion_tracker is not None and self.tile_id:
                tile_completion_tracker.notify_chunk_ready(self.tile_id, self)
        except Exception:
            pass  # Never block downloads

    def _build_tile_url(self, idx):
        """Build the provider URL/headers for attempt ``idx``.

        Returns ``(url, headers, server, apple_token_generation)``.  The Apple
        token generation is captured *before* the URL is rendered so a 403/410
        retry can rotate exactly the token it used.
        """

        server_num = idx % (len(self.serverlist))
        server = self.serverlist[server_num]
        quadkey = _gtile_to_quadkey(self.col, self.row, self.zoom)

        MAPID = "s2cloudless-2024_3857"
        MATRIXSET = "g"
        apple_token_generation = apple_token_service.generation
        MAPTYPES = {
            "EOX": f"https://s2maps-tiles.eu/wmts?layer={MAPID}&style=default&tilematrixset={MATRIXSET}&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image%2Fjpeg&TileMatrix={self.zoom}&TileCol={self.col}&TileRow={self.row}",
            "BI": f"https://t.ssl.ak.tiles.virtualearth.net/tiles/a{quadkey}.jpeg?g=15312",
            "GO2": f"http://mts{server_num}.google.com/vt/lyrs=s&x={self.col}&y={self.row}&z={self.zoom}",
            "ARC": f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "NAIP": f"http://naip.maptiles.arcgis.com/arcgis/rest/services/NAIP/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "USGS": f"https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "FIREFLY": f"https://fly.maptiles.arcgis.com/arcgis/rest/services/World_Imagery_Firefly/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "YNDX": f"https://sat{server_num+1:02d}.maps.yandex.net/tiles?l=sat&v=3.1814.0&x={self.col}&y={self.row}&z={self.zoom}",
            "APPLE": f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v={apple_token_service.version}&accessKey={apple_token_service.apple_token}"
        }

        url = MAPTYPES[self.maptype.upper()]
        header = {
                "user-agent": "curl/7.68.0"
        }
        if self.maptype.upper() == "EOX":
            log.debug("EOX DETECTED")
            header.update({'referer': 'https://s2maps.eu/'})
        return url, header, server, apple_token_generation

    def begin_network_attempt(self, idx=0, timeout=None, max_attempts=None):
        """Prepare the next network attempt for this chunk.

        Returns a :class:`_NetworkRequest` describing the HTTP call to issue,
        or an :class:`_AttemptOutcome` when the chunk resolved without any
        network I/O (cache hit, cancellation, attempt budget exhausted).

        This performs no blocking I/O and no sleeping: any required backoff is
        reported through ``_NetworkRequest.delay`` so async callers can
        schedule it instead of occupying a thread.
        """

        log.debug(f"Getting {self}")

        # Signal that download has started (not waiting in queue anymore)
        self.download_started.set()

        if is_shutdown_requested():
            self.cancel()
            return _AttemptOutcome(resolved=True)

        if self.get_cache():
            self.ready.set()
            self._notify_ready()
            return _AttemptOutcome(resolved=True)

        _max_attempts = max_attempts or MAX_TOTAL_ATTEMPTS

        # === TOTAL ATTEMPT LIMIT ===
        # Prevent infinite retries for persistent failures (network issues,
        # invalid responses, etc.) that don't return specific HTTP codes
        if self.attempt >= _max_attempts:
            log.warning(f"Chunk {self} exceeded {_max_attempts} total attempts, marking as permanently failed")
            self.permanent_failure = True
            self.failure_reason = "max_total_attempts"
            self.data = b''
            self.ready.set()
            bump('chunk_max_attempts_exhausted')
            self._notify_ready()
            return _AttemptOutcome(resolved=True)  # Stop worker retries

        if not self.starttime:
            self.starttime = time.time()

        url, header, server, apple_token_generation = self._build_tile_url(idx)
        self.url = url

        # === CANCELLATION CHECK ===
        # If the caller gave up waiting (Pass 2 timeout, budget exhausted, etc.),
        # abandon this download immediately to free the worker slot.
        if self.cancelled or is_shutdown_requested():
            log.debug(f"Chunk {self} cancelled before download attempt {self.attempt}")
            bump('chunk_cancelled_before_download')
            self.cancel()
            return _AttemptOutcome(resolved=True)  # Stop worker retries

        # Capped backoff: grows with attempts but maxes at 2 seconds
        # This prevents runaway delays when server is slow/throttling
        backoff_sleep = min(2.0, self.attempt / 10.0)
        self.attempt += 1

        # Read timeout = maxwait + 1s safety margin. No point keeping a
        # download alive longer than the caller is willing to wait.
        try:
            _maxwait = float(CFG.autoortho.maxwait)
        except Exception:
            _maxwait = 2.0
        _http_timeout = timeout or (5, _maxwait + 1.0)

        log.debug(f"Requesting {self.url} ..")

        return _NetworkRequest(
            url=url,
            headers=header,
            timeout=_http_timeout,
            delay=backoff_sleep,
            server=server,
            idx=idx,
            apple_token_generation=apple_token_generation,
        )

    def finish_network_attempt(self, request, response=None, error=None):
        """Apply one HTTP result to this chunk.

        Exactly one of ``response``/``error`` is expected.  Returns an
        :class:`_AttemptOutcome`; the caller owns closing ``response`` and
        honouring ``retry_request``/``requeue_delay``.
        """

        if error is not None:
            return self._handle_attempt_error(request, error)

        status_code = response.status_code

        if self.maptype.upper() == "APPLE" and status_code in (403, 410):
            if not request.apple_retried:
                log.warning("APPLE tile got %s; rotating token and retrying", status_code)
                apple_token_service.reset_apple_maps_token(
                    expected_generation=request.apple_token_generation,
                )
                url, header, server, generation = self._build_tile_url(request.idx)
                self.url = url
                return _AttemptOutcome(
                    retry_request=_NetworkRequest(
                        url=url,
                        headers=header,
                        timeout=request.timeout,
                        delay=0.0,
                        server=server,
                        idx=request.idx,
                        apple_token_generation=generation,
                        apple_retried=True,
                    ),
                )

        if status_code != 200:
            return self._handle_failed_status(request, response, status_code)

        try:
            data = response.content
        except Exception as err:
            return self._handle_attempt_error(request, err)

        if data and _is_jpeg(data[:3]):
            log.debug(f"Data for {self} is JPEG")
            self.data = data
        else:
            log.debug(f"Invalid JPEG for {self} (HTTP {status_code} "
                      f"content-type={response.headers.get('content-type', '?')} "
                      f"size={len(data) if data else 0})")
            bump('chunk_invalid_jpeg')
            self.data = b''

        bump('bytes_dl', len(self.data))

        self.fetchtime = time.monotonic() - self.starttime

        # Cache writers own an immutable bytes reference before consumers are
        # released, so completed tiles can safely drop Chunk.data immediately.
        cache_data = self.data
        if cache_data:
            if not _submit_bounded_cache_write(
                _cache_write_executor,
                _async_cache_write,
                self,
                cache_data,
                byte_count=len(cache_data),
                kind="local",
            ):
                self.save_cache(data=cache_data)

        # Signal ready after persistence owns its snapshot.
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
        self._notify_ready()

        return _AttemptOutcome(resolved=True)

    def _handle_failed_status(self, request, response, status_code):
        log.warning(
            f"Failed with status {status_code} to get chunk {self}"
            + (" on server " + request.server
               if self.maptype.upper() in MAPTYPES_WITH_SERVER else "")
            + "."
        )
        bump_many({f"http_{status_code}": 1, "req_err": 1})

        # Check if this is a permanent failure
        if status_code in PERMANENT_FAILURE_CODES:
            log.info(f"Chunk {self} permanently failed with {status_code}, marking as failed")
            self.permanent_failure = True
            self.failure_reason = str(status_code)
            self.data = b''  # Empty data
            self.ready.set()  # Mark as ready (with no data) to unblock waiters
            bump(f'chunk_permanent_fail_{status_code}')
            # Notify tile completion tracker even on failure
            # This allows DDS prebuild to proceed with available chunks
            # (fallbacks will be applied during build for failed chunks)
            self._notify_ready()
            return _AttemptOutcome(resolved=True)  # Stop worker retries

        requeue_delay = 0.0

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
                self._notify_ready()
                return _AttemptOutcome(resolved=True)
            # Back off for all transient failures, not only rate limits.
            requeue_delay = min(10.0, 0.5 * (2 ** min(self.retry_count, 5)))
            if status_code == 429:
                log.debug(f"Rate limited, backing off for {requeue_delay}s (attempt {self.retry_count}/{max_retries})")
                bump('chunk_rate_limited')
            else:
                log.debug(f"Transient error {status_code}, backing off for {requeue_delay}s (attempt {self.retry_count}/{max_retries})")
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
                    log.warning("Severe error rate (>=25%%) detected, consider checking configuration")
        return _AttemptOutcome(requeue_delay=requeue_delay)

    def _handle_attempt_error(self, request, error):
        # Cancellation is a deliberate outcome, never a transport failure:
        # no warning, no backoff and no resubmission.
        if isinstance(error, BrokerCancelledError) or self.cancelled:
            log.debug(f"Chunk {self} download cancelled: {error}")
            self.cancel()
            return _AttemptOutcome(resolved=True)

        if isinstance(error, BrokerShutdownError):
            _reset_http2_client()
            backoff = min(5.0, 0.5 * (2 ** min(self.attempt, 4)))
            log.warning(
                "Shared HTTP/2 broker became unavailable for %s; "
                "reconnecting after %.1fs",
                self,
                backoff,
            )
            return _AttemptOutcome(requeue_delay=backoff)

        if isinstance(error, (requests.exceptions.ConnectionError,
                              requests.exceptions.Timeout,
                              BrokerTimeoutError)):
            if (
                isinstance(error, BrokerTimeoutError)
            ):
                self.broker_timeout_count += 1
            if (
                isinstance(error, BrokerTimeoutError)
                and self.broker_timeout_count
                >= MAX_BROKER_TIMEOUT_ATTEMPTS
            ):
                self.permanent_failure = True
                self.failure_reason = "broker_timeout"
                self.data = b''
                self.ready.set()
                self._notify_ready()
                bump("chunk_broker_timeout_exhausted")
                log.warning(
                    "%s exceeded %d broker timeout attempts; using fallback",
                    self,
                    MAX_BROKER_TIMEOUT_ATTEMPTS,
                )
                return _AttemptOutcome(resolved=True)
            backoff = min(10.0, 0.5 * (2 ** min(self.attempt, 5)))
            log.warning(f"{self} connection/timeout error (attempt {self.attempt}), "
                        f"backoff {backoff:.1f}s: {error}")
            return _AttemptOutcome(requeue_delay=backoff)

        log.warning(f"Failed to get chunk {self} on server {request.server}. "
                    f"Err: {error} URL: {self.url}")
        return _AttemptOutcome()

    @profiled_stage("chunk.resolve")
    def get(self, idx=0, session=requests, timeout=None, max_attempts=None):
        prepared = self.begin_network_attempt(
            idx=idx, timeout=timeout, max_attempts=max_attempts
        )
        if isinstance(prepared, _AttemptOutcome):
            return prepared.resolved

        request = prepared
        while True:
            if request.delay > 0:
                time.sleep(request.delay)
                request.delay = 0.0

            resp = None
            error = None
            try:
                resp = _profiled_http_get(
                    session, request.url, request.headers, request.timeout, self
                )
            except Exception as err:
                error = err

            try:
                outcome = self.finish_network_attempt(request, resp, error)
            finally:
                if resp is not None:
                    resp.close()

            if outcome.retry_request is not None:
                request = outcome.retry_request
                continue
            if outcome.requeue_delay > 0:
                time.sleep(outcome.requeue_delay)
            return outcome.resolved

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

    def cancel(self):
        """Cancel this chunk's download.

        Sets the cancelled flag so the worker abandons retries and frees
        its slot.  Also sets ready (with empty data) so any other waiter
        unblocks immediately instead of hanging.
        """
        if self.cancelled:
            return  # Already cancelled
        self.cancelled = True
        request_id = self._broker_request_id
        if request_id:
            broker = _get_http2_client()
            if broker is not None:
                broker.cancel(request_id)
        # Unblock any thread waiting on ready (e.g. other mipmap builders)
        if not self.ready.is_set():
            if not self.data:
                self.data = b''
            self.ready.set()
        log.debug(f"Chunk {self} cancelled")
        bump('chunk_cancelled')


class MipmapBuildCoordinator:
    """Coordinate full and row-scoped mipmap builds without holding I/O locks."""

    IDLE = "idle"
    PARTIAL_BUILDING = "partial_building"
    FULL_BUILDING = "full_building"
    COMPLETE = "complete"
    CLOSED = "closed"

    def __init__(self, total_rows):
        self.total_rows = max(1, int(total_rows))
        self.state = self.IDLE
        self.covered_rows = set()
        self._inflight_rows = set()
        self._revision = 0
        self._condition = threading.Condition()

    def begin_partial(self, start_row, end_row, deadline):
        requested = set(range(start_row, end_row + 1))
        with self._condition:
            while True:
                if self.state == self.CLOSED:
                    return "closed", None
                if requested <= self.covered_rows:
                    return "covered", self._revision
                if (
                    self.state != self.FULL_BUILDING
                    and not requested.intersection(self._inflight_rows)
                ):
                    self._inflight_rows.update(requested)
                    self.state = self.PARTIAL_BUILDING
                    return "build", self._revision
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", None
                self._condition.wait(timeout=remaining)

    def can_commit_partial(self, revision):
        with self._condition:
            return (
                self.state != self.CLOSED
                and self.state != self.COMPLETE
                and revision == self._revision
            )

    def finish_partial(self, start_row, end_row, revision, success):
        rows = set(range(start_row, end_row + 1))
        with self._condition:
            self._inflight_rows.difference_update(rows)
            if success and revision == self._revision:
                self.covered_rows.update(rows)
            if self.state == self.PARTIAL_BUILDING:
                if len(self.covered_rows) >= self.total_rows:
                    self.state = self.COMPLETE
                elif self._inflight_rows:
                    self.state = self.PARTIAL_BUILDING
                else:
                    self.state = self.IDLE
            self._condition.notify_all()

    def begin_full(self, deadline):
        with self._condition:
            while self.state == self.FULL_BUILDING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", None
                self._condition.wait(timeout=remaining)
            if self.state == self.CLOSED:
                return "closed", None
            if self.state == self.COMPLETE:
                return "complete", self._revision
            self._revision += 1
            self.state = self.FULL_BUILDING
            return "build", self._revision

    def finish_full(self, revision, success):
        with self._condition:
            if revision == self._revision:
                if success:
                    self.covered_rows = set(range(self.total_rows))
                    self.state = self.COMPLETE
                else:
                    self.state = (
                        self.PARTIAL_BUILDING
                        if self._inflight_rows
                        else self.IDLE
                    )
            self._condition.notify_all()

    def restore_coverage(self, rows):
        with self._condition:
            if self.state == self.CLOSED:
                return
            self.covered_rows.update(
                row
                for row in rows
                if 0 <= int(row) < self.total_rows
            )
            if len(self.covered_rows) >= self.total_rows:
                self.state = self.COMPLETE
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._revision += 1
            self.state = self.CLOSED
            self._inflight_rows.clear()
            self._condition.notify_all()


class SourceLease:
    """Own immutable source bytes while a native or Python builder consumes them."""

    def __init__(self, tile, sources):
        self._tile = tile
        self.sources = tuple(source for source in sources if source)
        self._closed = False
        with tile._source_lease_lock:
            tile._source_lease_count += 1
            profile_gauge(
                "tile.source_leases",
                tile._source_lease_count,
            )

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.sources = ()
        tile = self._tile
        self._tile = None
        with tile._source_lease_lock:
            tile._source_lease_count = max(
                0,
                tile._source_lease_count - 1,
            )
            remaining = tile._source_lease_count
            profile_gauge("tile.source_leases", remaining)
        if remaining == 0:
            tile._release_completed_sources()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


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
        self._dds_write_lock = threading.Lock()  # Protects DDS buffer writes (gen_mipmaps, databuffer)
        self._mipmap_build_locks = {}  # Per-mipmap build serialization
        self._mipmap_build_locks_guard = threading.Lock()  # Protects the dict above
        self._mipmap_builds = {}
        self._mipmap_builds_guard = threading.Lock()
        self._source_lease_lock = threading.Lock()
        self._source_lease_count = 0
        self.refs = 0
        self.imgs = {}
        self._imgs_order = []  # Track insertion order for LRU eviction
        self._img_sizes = {}
        self._cached_image_bytes = 0
        try:
            self._image_cache_limit_bytes = max(
                0,
                int(
                    float(
                        getattr(
                            CFG.autoortho,
                            "tile_image_cache_mb",
                            96,
                        )
                    )
                    * 1048576
                ),
            )
        except (TypeError, ValueError):
            self._image_cache_limit_bytes = 96 * 1048576

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
        self._fallback_time_budget = None
        self._time_budget_lock = threading.Lock()
        
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
        
        # === DYNAMIC DDS CACHE UPGRADE HINT ===
        # Set by DynamicDDSCache.load() when a lower-ZL cached DDS exists that
        # can be upgraded via mipmap shifting instead of a full rebuild.
        # Tuple of (old_dds_path, old_metadata) or None.
        self._dds_upgrade_available = None
        
        # === DDS HEALING STATE ===
        # Set by DynamicDDSCache.load() when a cached DDS has missing or fallback chunks.
        self._dds_needs_healing = False
        self._dds_missing_indices = []
        self._dds_fallback_indices = []
        self._dds_coverage_revision = 0
        
        # === DDS ZL DOWNGRADE HINT ===
        # Set by DynamicDDSCache.load() when a higher-ZL cached DDS exists.
        self._dds_downgrade_available = None

        # === DDS INCREMENTAL PERSISTENCE HINT ===
        # Set by DynamicDDSCache.load() when loading a partially-built DDS.
        # Contains a set of mipmap indices that have real data on disk, or
        # None when all mipmaps are populated (v2 compat / full DDS).
        self._dds_populated_mipmaps = None
        self._dds_persisted = False
        self._dds_persistence_accepted = False
        
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
        self._read_ahead_state = {}
        self._read_ahead_lock = threading.Lock()

        # Bounded repair state for partial DDS cache entries missing mipmap 0.
        self._mm0_promotion_queued = False
        self._mm0_promotion_pin_until = 0.0

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

    def _get_mipmap_build_lock(self, mipmap):
        """Get or create a per-mipmap build lock for serialization."""
        with self._mipmap_build_locks_guard:
            if mipmap not in self._mipmap_build_locks:
                self._mipmap_build_locks[mipmap] = threading.Lock()
            return self._mipmap_build_locks[mipmap]

    def _get_mipmap_build_coordinator(self, mipmap):
        with self._mipmap_builds_guard:
            coordinator = self._mipmap_builds.get(mipmap)
            if coordinator is None:
                height_px = max(4, int(self.dds.height) >> mipmap)
                coordinator = MipmapBuildCoordinator(
                    max(1, height_px // 256)
                )
                self._mipmap_builds[mipmap] = coordinator
            return coordinator

    def _source_lease(self, sources):
        return SourceLease(self, sources)

    def _load_partial_dds_rows(self, metadata):
        if (
            dynamic_dds_cache is None
            or self.dds is None
            or not _get_bool_config(
                CFG.autoortho,
                "persist_partial_dds_cache",
                _persist_partial_dds,
            )
        ):
            return False
        entry = (metadata or {}).get("partial_mipmaps", {}).get("0")
        if not entry or entry.get("unit") != "chunk_row":
            return False
        total_rows = int(entry.get("total", 0))
        if total_rows <= 0:
            return False
        mm = self.dds.mipmap_list[0]
        bytes_per_chunk_row = mm.length // total_rows
        rows = dynamic_dds_cache.load_partial_rows(
            self.id,
            self.max_zoom,
            self,
        )
        valid_rows = {
            int(row): data
            for row, data in rows.items()
            if 0 <= int(row) < total_rows
            and len(data) == bytes_per_chunk_row
        }
        if not valid_rows:
            return False
        with self._dds_write_lock:
            sparse = self.dds.ensure_sparse_mipmap(
                0,
                bytes_per_chunk_row,
                total_rows,
            )
            for row, data in valid_rows.items():
                sparse.write_at(row * bytes_per_chunk_row, data)
            self._get_mipmap_build_coordinator(0).restore_coverage(
                valid_rows
            )
            self._dds_coverage_revision = int(entry.get("revision", 0))
            degraded_rows = set(entry.get("degraded", []))
            if degraded_rows:
                degraded_indices = {
                    row * self.chunks_per_row + column
                    for row in degraded_rows
                    for column in range(self.chunks_per_row)
                }
                self._dds_fallback_indices = sorted(
                    set(self._dds_fallback_indices).union(
                        degraded_indices
                    )
                )
                self._dds_needs_healing = True
            if sparse.is_complete() and not self._dds_needs_healing:
                dense = sparse.read_at(0, mm.length)
                self.dds.replace_mipmap_dense(0, dense, complete=True)
                sparse = self.dds.mipmap_list[0].buffer
                bump("sparse_to_dense_promotions")
        bump("partial_dds_cache_rows_loaded", len(valid_rows))
        profile_gauge(
            "dds.sparse_allocated_bytes",
            sparse.allocated_bytes(),
        )
        return True

    def _queue_partial_rows(
        self,
        mipmap,
        startrow,
        endrow,
        bytes_per_chunk_row,
        data,
        degraded_rows=None,
    ):
        if (
            mipmap != 0
            or dynamic_dds_cache is None
            or not hasattr(dynamic_dds_cache, "enqueue_partial_row")
            or not _get_bool_config(
                CFG.autoortho,
                "persist_partial_dds_cache",
                _persist_partial_dds,
            )
        ):
            return
        degraded_rows = set(degraded_rows or ())
        total_rows = self._get_mipmap_build_coordinator(0).total_rows
        for relative_row, row_index in enumerate(
            range(startrow, endrow + 1)
        ):
            row_start = relative_row * bytes_per_chunk_row
            row_data = data[row_start:row_start + bytes_per_chunk_row]
            if len(row_data) != bytes_per_chunk_row:
                bump("partial_dds_persistence_dropped")
                continue
            if dynamic_dds_cache.enqueue_partial_row(
                self.id,
                self.max_zoom,
                self,
                row_index,
                row_data,
                total_rows,
                degraded=row_index in degraded_rows,
            ):
                bump(
                    "partial_dds_persistence_queue_bytes",
                    len(row_data),
                )
            else:
                bump("partial_dds_persistence_dropped")

    def _maybe_persist_complete_dds(self):
        if (
            dynamic_dds_cache is None
            or self.dds is None
            or self._dds_persisted
            or self._dds_persistence_accepted
            or self._dds_needs_healing
        ):
            return False
        if not all(mm.retrieved for mm in self.dds.mipmap_list):
            return False
        dds_bytes = self.dds.read_at(0, self.dds.total_size)
        if len(dds_bytes) != self.dds.total_size:
            return False
        self._dds_persistence_accepted = self._enqueue_dds_persistence(
            dds_bytes,
        )
        if self._dds_persistence_accepted:
            self._release_completed_sources()
        return self._dds_persistence_accepted

    def _enqueue_dds_persistence(
        self,
        dds_bytes,
        missing_indices=None,
        fallback_indices=None,
    ):
        if dynamic_dds_cache is None:
            return False
        try:
            if hasattr(dynamic_dds_cache, "enqueue_store"):
                accepted = dynamic_dds_cache.enqueue_store(
                    self.id,
                    self.max_zoom,
                    dds_bytes,
                    self,
                    missing_indices,
                    fallback_indices,
                )
            else:
                accepted = dynamic_dds_cache.store(
                    self.id,
                    self.max_zoom,
                    dds_bytes,
                    self,
                    mm0_missing_indices=missing_indices,
                    mm0_fallback_indices=fallback_indices,
                )
        except Exception:
            accepted = False
        self._dds_persistence_accepted = bool(accepted)
        return self._dds_persistence_accepted

    def _finalize_build(
        self,
        *,
        dds_bytes=None,
        missing_indices=None,
        fallback_indices=None,
        persisted=False,
    ):
        missing_indices = list(missing_indices or ())
        fallback_indices = list(fallback_indices or ())
        with self._lock:
            self._dds_missing_indices = missing_indices
            self._dds_fallback_indices = fallback_indices
            self._dds_needs_healing = bool(
                missing_indices or fallback_indices
            )
            if persisted:
                self._dds_persisted = True
                self._dds_persistence_accepted = True
        if dds_bytes is not None and not persisted:
            self._enqueue_dds_persistence(
                dds_bytes,
                missing_indices or None,
                fallback_indices or None,
            )
        if not self._dds_needs_healing:
            self._release_completed_sources()

    def _maybe_schedule_read_ahead(
        self,
        mipmap,
        startrow,
        endrow,
        bytes_per_chunk_row,
    ):
        if self._closed or self.refs <= 0 or mipmap != 0:
            return
        now = time.monotonic()
        key = int(mipmap)
        candidate = None
        consumed = 0
        with self._read_ahead_lock:
            state = self._read_ahead_state.get(key)
            if state is None or now - state["timestamp"] > 2.0:
                sequential = 1
                direction = 0
                scheduled = set()
            else:
                scheduled = state["scheduled"]
                requested_rows = set(range(startrow, endrow + 1))
                consumed = len(scheduled.intersection(requested_rows))
                scheduled.difference_update(requested_rows)
                if startrow == state["end"] + 1:
                    direction = 1
                elif endrow + 1 == state["start"]:
                    direction = -1
                else:
                    direction = 0
                sequential = (
                    state["sequential"] + 1
                    if direction and direction == state["direction"]
                    else 2 if direction else 1
                )
            if sequential >= 2 and direction:
                candidate = endrow + 1 if direction > 0 else startrow - 1
                total_rows = self._get_mipmap_build_coordinator(
                    mipmap
                ).total_rows
                if not 0 <= candidate < total_rows or candidate in scheduled:
                    candidate = None
                else:
                    scheduled.add(candidate)
            self._read_ahead_state[key] = {
                "start": startrow,
                "end": endrow,
                "direction": direction,
                "timestamp": now,
                "sequential": sequential,
                "scheduled": scheduled,
            }

        if consumed:
            bump("partial_read_ahead_rows_consumed", consumed)
        if candidate is None:
            return
        if not is_prefetch_runtime_allowed():
            bump("partial_read_ahead_skipped_capacity")
            with self._read_ahead_lock:
                self._read_ahead_state[key]["scheduled"].discard(candidate)
            return
        try:
            depths = chunk_getter.queue_depths()
        except Exception:
            depths = {"live": 0, "prefetch": 0}
        if depths["live"] > 0:
            bump("partial_read_ahead_skipped_live")
            with self._read_ahead_lock:
                self._read_ahead_state[key]["scheduled"].discard(candidate)
            return
        if not _read_ahead_capacity.acquire(blocking=False):
            bump("partial_read_ahead_skipped_capacity")
            with self._read_ahead_lock:
                self._read_ahead_state[key]["scheduled"].discard(candidate)
            return

        bump("partial_rows_extra_scheduled")
        try:
            _get_read_ahead_executor().submit(
                self._run_read_ahead,
                mipmap,
                candidate,
                bytes_per_chunk_row,
            )
        except Exception:
            _read_ahead_capacity.release()
            with self._read_ahead_lock:
                self._read_ahead_state[key]["scheduled"].discard(candidate)

    def _run_read_ahead(self, mipmap, row, bytes_per_chunk_row):
        try:
            if self._closed or self.refs <= 0 or is_live_building():
                return
            self._try_native_partial_mipmap_build(
                mipmap,
                row,
                row,
                bytes_per_chunk_row,
                priority=PRIORITY_PREFETCH,
            )
        finally:
            _read_ahead_capacity.release()

    def _cache_image(self, mipmap: int, img_data: tuple) -> bool:
        """Retain fallback imagery within a byte budget, not an image count."""
        image = img_data[0] if isinstance(img_data, tuple) else img_data
        image_bytes = max(
            0,
            int(getattr(image, "_width", 0))
            * int(getattr(image, "_height", 0))
            * 4,
        )
        if (
            image_bytes <= 0
            or image_bytes > self._image_cache_limit_bytes
        ):
            bump("tile_image_cache_oversize_skip")
            return False

        # If already cached, just update (move to end for LRU)
        if mipmap in self.imgs:
            # Another build already installed a fallback source. Keep the
            # existing object alive until the tile is unreferenced; replacing
            # and closing it here can free native memory still used by a
            # concurrent mipmap compressor.
            return False

        if (
            self._cached_image_bytes + image_bytes
            > self._image_cache_limit_bytes
        ):
            bump("tile_image_cache_budget_skip")
            return False

        # Insert new
        self.imgs[mipmap] = img_data
        self._img_sizes[mipmap] = image_bytes
        self._cached_image_bytes += image_bytes
        self._imgs_order.append(mipmap)
        profile_gauge(
            "tile.cached_image_bytes",
            self._cached_image_bytes,
        )
        return True

    def _release_composition_images(self) -> None:
        with self._lock:
            images = list(self.imgs.values())
            self.imgs.clear()
            self._imgs_order.clear()
            self._img_sizes.clear()
            self._cached_image_bytes = 0
        for img_data in images:
            image = img_data[0] if isinstance(img_data, tuple) else img_data
            if image is not None and hasattr(image, "close"):
                try:
                    image.close()
                except Exception:
                    pass

    def _release_completed_sources(self) -> bool:
        """Release source buffers only when no healing path can consume them."""
        with self._lock:
            grid = self.chunks.get(self.max_zoom)
            if isinstance(grid, ChunkGrid):
                chunks = list(grid.materialized())
            else:
                chunks = list(grid or ())
            complete = (
                not self._dds_needs_healing
                and not self._dds_missing_indices
                and not self._dds_fallback_indices
                and self.dds is not None
                and bool(self.dds.mipmap_list)
                and self.dds.mipmap_list[0].retrieved
                and self.refs <= 0
                and (
                    dynamic_dds_cache is None
                    or self._dds_persisted
                    or self._dds_persistence_accepted
                )
                and getattr(self, "_source_lease_count", 0) == 0
                and bool(chunks)
                and all(
                    chunk.ready.is_set() and bool(chunk.data)
                    for chunk in chunks
                )
            )
            if not complete:
                return False
            self._last_collected_jpegs = None
            self._last_collected_ratio = None
            self._last_collected_missing = None
            released_bytes = sum(
                len(chunk.data)
                for chunk in chunks
                if chunk.data
            )
            for chunk in chunks:
                chunk.data = None
            images = list(self.imgs.values())
            self.imgs.clear()
            self._imgs_order.clear()
            self._img_sizes.clear()
            self._cached_image_bytes = 0
        for img_data in images:
            image = img_data[0] if isinstance(img_data, tuple) else img_data
            if image is not None and hasattr(image, "close"):
                try:
                    image.close()
                except Exception:
                    pass
        if released_bytes:
            bump("jpeg_bytes_released_after_build", released_bytes)
        return True

    def _get_chunk_grid(self, quick_zoom=0, min_zoom=None):
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(quick_zoom, min_zoom)

        with self._lock:
            grid = self.chunks.get(zoom)
            if grid is None:
                grid = ChunkGrid(
                    col=col,
                    row=row,
                    width=width,
                    height=height,
                    zoom=zoom,
                    maptype=self.maptype,
                    cache_dir=self.cache_dir,
                    tile_id=self.id,
                )
                self.chunks[zoom] = grid
                bump("logical_chunks", grid.logical_length)
                log.debug(
                    f"CREATE_CHUNKS: Tile {self.id} created logical grid "
                    f"for zoom {zoom}: {width}x{height} at ({col},{row})"
                )
            return grid

    def _create_chunks(self, quick_zoom=0, min_zoom=None):
        """Compatibility helper for full-build consumers."""
        grid = self._get_chunk_grid(quick_zoom, min_zoom)
        grid.ensure_all()
        return grid

    def _probe_chunk_cache_ratio(self, zoom: int) -> float:
        """
        Fast probe to check what fraction of chunks are already available
        in memory or disk cache. Does NOT trigger any downloads.

        Used by get_bytes() to route warm cache to batch aopipeline
        and cold cache directly to progressive path.

        Cost: ~1-5ms (memory scan + batch cache read)

        Returns:
            float: Ratio of available chunks (0.0 to 1.0)
        """
        self._create_chunks(zoom)
        chunks = self.chunks.get(zoom, [])

        if not chunks:
            return 0.0

        total_chunks = len(chunks)
        available_count = 0

        need_cache_read = []
        for chunk in chunks:
            chunk_data = chunk.data
            if chunk.ready.is_set() and chunk_data:
                available_count += 1
            elif not chunk.ready.is_set():
                need_cache_read.append(chunk)

        if need_cache_read:
            cache_paths = [c.cache_path for c in need_cache_read]
            cached_data = _batch_read_cache_files(cache_paths)

            if cached_data:
                for chunk in need_cache_read:
                    if chunk.cache_path in cached_data:
                        chunk.set_cached_data(cached_data[chunk.cache_path])
                        available_count += 1
                        bump('chunk_hit')
            else:
                for chunk in need_cache_read:
                    if chunk.get_cache():
                        if chunk.data:
                            available_count += 1

        return available_count / total_chunks

    @profiled_stage("tile.collect_chunks")
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
            return jpeg_datas
        
        log.debug(f"_collect_chunk_jpegs: Phase 1 - {available_count}/{total_chunks} "
                  f"chunks ({ratio*100:.0f}%), need {min_available_ratio*100:.0f}%")
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 2: Download missing chunks using FULL remaining budget
        # ═══════════════════════════════════════════════════════════════════════
        # For chunks not in cache, submit download requests and wait.
        # Uses the FULL remaining time_budget to maximize chunk collection.
        # This ensures batch aopipeline gets maximum opportunity to reach threshold.
        
        # Determine download wait time, capped by the user's per-chunk wait.
        maxwait_cap = self.get_maxwait()
        if time_budget and not time_budget.exhausted:
            wait_time = min(time_budget.remaining, maxwait_cap)
        else:
            # No budget - use a reasonable default
            wait_time = min(5.0, maxwait_cap)
        
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
        
        # Wait for downloads using chunk.ready events (avoids busy-wait polling).
        # Each chunk's ready Event is set when its download completes, so we
        # wake immediately instead of wasting up to 20ms per poll cycle.
        deadline = time.monotonic() + wait_time

        # Build list of still-pending chunk indices for event-driven wait
        still_pending = [i for i in missing_indices if jpeg_datas[i] is None]

        while still_pending and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            # Wait on the first pending chunk's ready event (wakes on any completion)
            # Use short timeout so we can cycle through and check all chunks
            wait_timeout = min(0.1, remaining)
            chunks[still_pending[0]].ready.wait(timeout=wait_timeout)

            # Check all pending chunks for newly available data
            new_pending = []
            for i in still_pending:
                if jpeg_datas[i] is not None:
                    continue  # Already collected

                chunk = chunks[i]
                chunk_data = chunk.data  # TOCTOU-safe capture

                if chunk.ready.is_set() and chunk_data:
                    jpeg_datas[i] = chunk_data
                    available_count += 1
                else:
                    new_pending.append(i)

            still_pending = new_pending

            # Check if we have enough now
            ratio = available_count / total_chunks
            if ratio >= min_available_ratio:
                log.debug(f"_collect_chunk_jpegs: Phase 2 success - "
                          f"{available_count}/{total_chunks} ({ratio*100:.0f}%)")
                return jpeg_datas
        
        # Final check after deadline
        ratio = available_count / total_chunks
        if ratio >= min_available_ratio:
            log.debug(f"_collect_chunk_jpegs: Phase 2 final success - "
                      f"{available_count}/{total_chunks} ({ratio*100:.0f}%)")
            return jpeg_datas
        
        log.debug(f"_collect_chunk_jpegs: Threshold not met - "
                  f"{available_count}/{total_chunks} ({ratio*100:.0f}%), "
                  f"below {min_available_ratio*100:.0f}% threshold")
        
        # Store collected data for potential reuse by streaming builder
        # Even though ratio is below threshold, this data is still valuable
        self._last_collected_jpegs = jpeg_datas
        self._last_collected_ratio = ratio
        self._last_collected_missing = [i for i, d in enumerate(jpeg_datas) if d is None]
        
        # If return_partial requested, return what we collected (for build-as-is mode)
        if return_partial:
            return jpeg_datas
        
        return None

    @profiled_stage("dds.batch_build")
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
        jpeg_datas = self._collect_chunk_jpegs(
            self.max_zoom,
            time_budget=time_budget,
            min_available_ratio=1.0,
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
            record_stage(
                "dds.buffer_pool_wait",
                wait_time_ms,
                tile_id=self.id,
                details={"path": "batch"},
            )
            if wait_time_ms > 10:  # Only track significant waits
                bump('live_queue_wait_count')
                log.debug(f"_try_aopipeline_build: Waited {wait_time_ms:.0f}ms for buffer for {self.id}")
            
        except TimeoutError:
            wait_time_ms = (time.monotonic() - wait_start) * 1000
            record_stage(
                "dds.buffer_pool_wait",
                wait_time_ms,
                tile_id=self.id,
                outcome="timeout",
                details={"path": "batch"},
            )
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
            native_start = time.monotonic()
            with self._source_lease(jpeg_datas):
                with _native_build_context() as threads:
                    result = native_dds.build_from_jpegs_to_buffer(
                        buffer,
                        jpeg_datas,
                        format=dxt_format,
                        missing_color=missing_color,
                        max_threads=threads
                    )
            record_stage(
                "dds.native_compute",
                (time.monotonic() - native_start) * 1000.0,
                tile_id=self.id,
                outcome="ok" if result.success else "failed",
                details={"path": "batch", "threads": threads},
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
            
            mm0_missing = [i for i, d in enumerate(jpeg_datas) if d is None]
            self._finalize_build(
                dds_bytes=dds_bytes,
                missing_indices=mm0_missing,
            )
            
        except Exception as e:
            log.debug(f"_try_aopipeline_build: Exception for {self.id}: {e}")
            bump('live_aopipeline_exception')
            build_success = False
        
        finally:
            # Always release buffer back to pool
            pool.release(buffer_id)
        
        return build_success

    @profiled_stage("dds.streaming_build")
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
        # Handle imports for both frozen (PyInstaller) and direct Python execution
        try:
            from autoortho.aopipeline.AoDDS import get_default_builder_pool, get_default_pool
            from autoortho.aopipeline.fallback_resolver import FallbackResolver, TimeBudget as FBTimeBudget
        except ImportError:
            try:
                from aopipeline.AoDDS import get_default_builder_pool, get_default_pool
                from aopipeline.fallback_resolver import FallbackResolver, TimeBudget as FBTimeBudget
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
        
        # Keep references alive for zero-copy mode (cleared after finalize).
        # Builder acquisition is delayed until data/fallback collection is done
        # so scarce native builder slots are not held during network/disk waits.
        jpeg_refs_for_nocopy = []
        source_lease = None
        final_ready_chunks = []
        builder = None
        
        try:
            
            # Ensure chunks are created for target zoom
            self._create_chunks(self.max_zoom)
            grid = self.chunks.get(self.max_zoom)
            chunks = grid.materialized() if grid is not None else ()
            
            if not chunks:
                log.debug(f"_try_streaming_aopipeline_build: No chunks for {self.id}")
                # Clear pre-collected data to free memory on early return
                self._last_collected_jpegs = None
                self._last_collected_missing = None
                self._last_collected_ratio = None
                return False
            
            # Streaming builder now uses time_budget for download waits (not deprecated max_download_wait)
            # Default to 5s if no budget provided
            maxwait_cap = self.get_maxwait()
            if time_budget and not time_budget.exhausted:
                max_wait = min(time_budget.remaining, maxwait_cap)
            else:
                max_wait = min(5.0, maxwait_cap)
            
            if use_precollected:
                # Use pre-collected data from batch attempt
                log.debug(f"_try_streaming_aopipeline_build: Reusing {len(pre_collected) - len(pre_collected_missing)} "
                          f"chunks from batch collection (missing: {len(pre_collected_missing)})")
                
                ready_chunks = [(i, data) for i, data in enumerate(pre_collected) if data is not None]
                if ready_chunks:
                    final_ready_chunks.extend(ready_chunks)
                
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
                
                if ready_chunks:
                    final_ready_chunks.extend(ready_chunks)
            
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
            streaming_mm0_missing = []
            streaming_mm0_fallback = []
            newly_ready = []
            failed_indices = []
            for i in pending_indices:
                chunk = chunks[i]
                if chunk.ready.is_set() and chunk.data:
                    newly_ready.append((i, chunk.data))
                else:
                    failed_indices.append(i)
            
            if newly_ready:
                final_ready_chunks.extend(newly_ready)
            
            # Phase 4: Resolve fallbacks for failed chunks.
            # Always attempt fallback resolution even if the main time budget is
            # exhausted: the resolver only does disk cache lookups and mipmap
            # scaling (downloader=None) which are fast, sub-second operations.
            fallback_results = []
            if failed_indices:
                # When main budget is exhausted, give fallback resolution a
                # fresh budget for disk-only operations (no network), using the
                # user-configured fallback_timeout.
                fallback_resolve_budget = float(getattr(CFG.autoortho, 'fallback_timeout', 30.0))
                resolve_deadline_secs = max(1.0, max_wait) if not (time_budget and time_budget.exhausted) else fallback_resolve_budget
                shared_fb_budget = FBTimeBudget(resolve_deadline_secs)

                # Resolve serially because AoImage/aodds native paths are not
                # consistently thread-safe under fallback scaling.
                for idx in failed_indices:
                    if shared_fb_budget.exhausted:
                        log.debug(f"_try_streaming_aopipeline_build: Fallback budget exhausted for {self.id}")
                        break

                    chunk_col = self.col + (idx % self.chunks_per_row)
                    chunk_row = self.row + (idx // self.chunks_per_row)
                    try:
                        with profile_span(
                            "image.fallback_resolve",
                            tile_id=self.id,
                            details={
                                "chunk_index": idx,
                                "zoom": self.max_zoom,
                            },
                        ) as fallback_span:
                            rgba = resolver.resolve(
                                chunk_col, chunk_row, self.max_zoom,
                                target_mipmap=0,
                                time_budget=shared_fb_budget
                            )
                            if rgba is None:
                                fallback_span.outcome = "miss"
                        fallback_results.append((idx, rgba))
                    except Exception as e:
                        log.debug(f"_try_streaming_aopipeline_build: Fallback failed for {self.id} chunk {idx}: {e}")
                        fallback_results.append((idx, None))

            config = {
                'chunks_per_side': self.chunks_per_row,
                'format': dxt_format,
                'missing_color': missing_color,
                'nocopy_mode': use_precollected,
            }
            if time_budget is not None and hasattr(time_budget, 'remaining'):
                builder_timeout = max(1.0, time_budget.remaining)
            else:
                builder_timeout = 30.0

            wait_start = time.monotonic()
            builder = builder_pool.acquire(config=config, timeout=builder_timeout)
            if not builder:
                wait_time_ms = (time.monotonic() - wait_start) * 1000
                record_stage(
                    "dds.builder_pool_wait",
                    wait_time_ms,
                    tile_id=self.id,
                    outcome="timeout",
                )
                log.debug(f"_try_streaming_aopipeline_build: Builder pool timeout after {wait_time_ms:.0f}ms")
                bump('streaming_builder_queue_timeout')
                return False

            wait_time_ms = (time.monotonic() - wait_start) * 1000
            record_stage(
                "dds.builder_pool_wait",
                wait_time_ms,
                tile_id=self.id,
            )
            if wait_time_ms > 10:
                bump('streaming_builder_queue_wait_count')

            if final_ready_chunks:
                source_lease = self._source_lease(
                    data for _index, data in final_ready_chunks
                )
                builder.add_chunks_batch_nocopy(final_ready_chunks, jpeg_refs_for_nocopy)

            if failed_indices:
                # Apply fallback results to builder
                resolved_indices = set()
                for idx, rgba in fallback_results:
                    resolved_indices.add(idx)
                    if rgba:
                        builder.add_fallback_image(idx, rgba)
                        streaming_mm0_fallback.append(idx)
                    else:
                        builder.mark_missing(idx)
                        streaming_mm0_missing.append(idx)
                
                # Mark unresolved chunks as missing
                for i in failed_indices:
                    if i not in resolved_indices:
                        builder.mark_missing(i)
                        streaming_mm0_missing.append(i)
            
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
                record_stage(
                    "dds.buffer_pool_wait",
                    wait_time_ms,
                    tile_id=self.id,
                    details={"path": "streaming"},
                )
                if wait_time_ms > 10:
                    bump('streaming_queue_wait_count')
            
            except TimeoutError:
                record_stage(
                    "dds.buffer_pool_wait",
                    (time.monotonic() - wait_start) * 1000.0,
                    tile_id=self.id,
                    outcome="timeout",
                    details={"path": "streaming"},
                )
                log.debug(f"_try_streaming_aopipeline_build: Queue timeout for {self.id}")
                bump('streaming_queue_timeout')
                return False
            
            try:
                native_start = time.monotonic()
                with _native_build_context() as threads:
                    result = builder.finalize(buffer, max_threads=threads)
                record_stage(
                    "dds.native_compute",
                    (time.monotonic() - native_start) * 1000.0,
                    tile_id=self.id,
                    outcome="ok" if result.success else "failed",
                    details={"path": "streaming", "threads": threads},
                )
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
                        
                        self._finalize_build(
                            dds_bytes=dds_bytes,
                            missing_indices=streaming_mm0_missing,
                            fallback_indices=streaming_mm0_fallback,
                        )
                        
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
            if source_lease is not None:
                source_lease.close()
            if builder is not None:
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
        
        # Boost priority of any queued/in-flight background downloads.
        chunks = self.chunks.get(self.max_zoom, [])
        reprioritize = False
        for chunk in chunks:
            if hasattr(chunk, 'priority') and (hasattr(chunk, 'in_flight') or hasattr(chunk, 'in_queue')):
                if getattr(chunk, 'in_flight', False) or getattr(chunk, 'in_queue', False):
                    chunk.priority = PRIORITY_LIVE
                    chunk.prefetch = False
                    reprioritize = True

        if reprioritize and chunk_getter is not None:
            chunk_getter.reprioritize_queue()
        
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
        
        self._create_chunks(quick_zoom)
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(quick_zoom)

        grid = self.chunks[zoom]
        for chunk in grid.ensure_all():
            chunk_getter.submit(chunk)

        for chunk in grid.ensure_all():
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

            populated = getattr(self, '_dds_populated_mipmaps', None)

            for mm in self.dds.mipmap_list:
                if mm.startpos >= len(prebuilt_bytes):
                    # Prebuilt data doesn't include this mipmap
                    # Propagate last valid mipmap data to this and all remaining mipmaps
                    # This matches pydds.gen_mipmaps() behavior for trailing mipmaps
                    if last_valid_mm_data is not None:
                        for trailing_mm in self.dds.mipmap_list[mm.idx:]:
                            if populated is not None and trailing_mm.idx not in populated:
                                continue
                            self.dds.replace_mipmap_dense(
                                trailing_mm.idx,
                                last_valid_mm_data,
                            )
                    break

                # Skip unpopulated mipmaps (partial DDS from incremental save)
                if populated is not None and mm.idx not in populated:
                    continue

                # Extract this mipmap's data from the prebuilt buffer
                mm_end = min(mm.endpos, len(prebuilt_bytes))
                if mm_end <= mm.startpos:
                    # No data for this mipmap - propagate last valid
                    if last_valid_mm_data is not None:
                        for trailing_mm in self.dds.mipmap_list[mm.idx:]:
                            if populated is not None and trailing_mm.idx not in populated:
                                continue
                            self.dds.replace_mipmap_dense(
                                trailing_mm.idx,
                                last_valid_mm_data,
                            )
                    break
                    
                mm_data = prebuilt_bytes[mm.startpos:mm_end]
                
                # Store in the mipmap's databuffer
                self.dds.replace_mipmap_dense(mm.idx, mm_data)
                
                # Track last valid mipmap data for propagation to trailing mipmaps
                last_valid_mm_data = mm_data
                last_valid_mm_idx = mm.idx
            
            log.debug(f"Populated DDS from prebuilt cache for {self} "
                      f"(last_mm={last_valid_mm_idx}, "
                      f"populated={sorted(populated) if populated else 'all'})")
            # Mark as prepopulated so bytes_read warning doesn't trigger
            self._prepopulated = True
            self._finalize_build(persisted=True)
            return True
            
        except Exception as e:
            log.warning(f"Failed to populate DDS from prebuilt: {e}")
            return False
   
    def find_mipmap_pos(self, offset):
        for m in self.dds.mipmap_list:
            if offset < m.endpos:
                return m.idx
        return self.dds.mipmap_list[-1].idx

    def _partial_row_range(self, mipmap, offset, length):
        mm = self.dds.mipmap_list[mipmap]
        height_px = max(4, int(self.dds.height) >> mipmap)
        total_rows = max(1, math.ceil(height_px / 256))
        bytes_per_chunk_row = math.ceil(mm.length / total_rows)
        mm_offset = max(0, int(offset) - mm.startpos)
        startrow = min(total_rows - 1, mm_offset // bytes_per_chunk_row)
        end_byte = mm_offset + max(0, int(length) - 1)
        endrow = min(total_rows - 1, end_byte // bytes_per_chunk_row)
        return startrow, max(startrow, endrow), bytes_per_chunk_row, total_rows

    def _pin_mm0_promotion(self) -> None:
        try:
            ttl_sec = float(getattr(CFG.autoortho, 'partial_cache_promote_pin_sec', 180.0))
        except Exception:
            ttl_sec = 180.0
        ttl_sec = max(30.0, min(600.0, ttl_sec))
        self._mm0_promotion_pin_until = time.monotonic() + ttl_sec

    def _clear_mm0_promotion_pin(self) -> None:
        self._mm0_promotion_pin_until = 0.0

    def _mm0_promotion_is_pinned(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.monotonic()
        return self._mm0_promotion_pin_until > now

    def _release_mm0_promotion_claim(self) -> None:
        self._mm0_promotion_queued = False
        self._clear_mm0_promotion_pin()
        with _partial_mm0_promotions_lock:
            _partial_mm0_promotions.pop(self.id, None)

    def _maybe_promote_partial_cache_to_mm0(self, requested_mipmap: int) -> bool:
        """Queue bounded full-detail repair for a partial DDS cache entry."""
        if self._mm0_promotion_queued:
            bump('partial_mm0_promote_duplicate')
            return False
        if requested_mipmap <= 0:
            return False
        if background_dds_builder is None or tile_completion_tracker is None:
            bump('partial_mm0_promote_no_builder')
            return False
        if not _get_bool_config(CFG.autoortho, 'partial_cache_promote_mm0', True):
            bump('partial_mm0_promote_disabled')
            return False
        if self.dds is None or self.dds.mipmap_list[0].retrieved:
            return False

        have_position = bool(datareftracker.data_valid and datareftracker.connected)

        try:
            radius_nm = float(getattr(CFG.autoortho, 'partial_cache_promote_radius_nm', 12.0))
        except Exception:
            radius_nm = 12.0
        radius_nm = max(1.0, min(80.0, radius_nm))

        try:
            max_promotions = int(getattr(CFG.autoortho, 'partial_cache_promote_max_tiles', 500))
        except Exception:
            max_promotions = 500
        max_promotions = max(0, min(1000, max_promotions))
        if max_promotions <= 0:
            bump('partial_mm0_promote_cap_zero')
            return False

        try:
            promotion_window_sec = float(getattr(CFG.autoortho, 'partial_cache_promote_window_sec', 90.0))
        except Exception:
            promotion_window_sec = 90.0
        promotion_window_sec = max(15.0, min(600.0, promotion_window_sec))

        distance_nm = None
        if have_position:
            try:
                with datareftracker._lock:
                    player_lat = datareftracker.lat
                    player_lon = datareftracker.lon

                center_row = self.row + (self.height / 2.0) - 0.5
                center_col = self.col + (self.width / 2.0) - 0.5
                tile_lat, tile_lon = _chunk_to_latlon(center_row, center_col, self.tilename_zoom)
                distance_nm = _haversine_distance(player_lat, player_lon, tile_lat, tile_lon) / 1852.0
            except Exception as e:
                log.debug(f"Partial mm0 promotion distance check failed for {self.id}: {e}")
                bump('partial_mm0_promote_distance_error')
                return False

            if distance_nm > radius_nm:
                bump('partial_mm0_promote_too_far')
                return False
        else:
            try:
                startup_cap = int(getattr(CFG.autoortho, 'partial_cache_promote_startup_max_tiles', 500))
            except Exception:
                startup_cap = 96
            startup_cap = max(0, min(max_promotions, startup_cap))
            if startup_cap <= 0:
                bump('partial_mm0_promote_no_position')
                return False
            max_promotions = startup_cap

        with _partial_mm0_promotions_lock:
            cutoff = time.monotonic() - promotion_window_sec
            while _partial_mm0_promotions:
                _old_id, old_ts = next(iter(_partial_mm0_promotions.items()))
                if old_ts >= cutoff:
                    break
                _partial_mm0_promotions.popitem(last=False)

            if self.id in _partial_mm0_promotions:
                self._mm0_promotion_queued = True
                bump('partial_mm0_promote_duplicate')
                return False
            if len(_partial_mm0_promotions) >= max_promotions:
                bump('partial_mm0_promote_cap_hit')
                return False
            _partial_mm0_promotions[self.id] = time.monotonic()

        try:
            chunks = self._create_chunks(self.max_zoom).ensure_all()
            if not chunks:
                bump('partial_mm0_promote_no_chunks')
                self._release_mm0_promotion_claim()
                return False

            not_ready = [c for c in chunks if not c.ready.is_set()]
            self._mm0_promotion_queued = True

            if not not_ready:
                self._pin_mm0_promotion()
                if background_dds_builder.submit(self, priority=PRIORITY_CACHE_REPAIR):
                    bump('partial_mm0_promote_builder_ready')
                    log.info(
                        f"PARTIAL_MM0_PROMOTE: {self.id} queued DDS build "
                        f"(distance={distance_nm:.1f}nm, chunks=cached)"
                        if distance_nm is not None else
                        f"PARTIAL_MM0_PROMOTE: {self.id} queued DDS build "
                        f"(distance=unknown, chunks=cached)"
                    )
                    return True
                bump('partial_mm0_promote_builder_rejected')
                self._release_mm0_promotion_claim()
                return False

            self._pin_mm0_promotion()
            tile_completion_tracker.start_tracking(self, self.max_zoom)

            submitted = 0
            for chunk in not_ready:
                if chunk.ready.is_set():
                    continue
                if not getattr(chunk, 'in_queue', False) and not getattr(chunk, 'in_flight', False):
                    chunk.priority = (
                        PRIORITY_CACHE_REPAIR +
                        _calculate_spatial_priority(chunk.row, chunk.col, chunk.zoom, 0)
                    )
                    chunk.prefetch = True
                    chunk_getter.submit(chunk)
                    submitted += 1

            bump('partial_mm0_promote_queued')
            if submitted:
                bump('partial_mm0_promote_chunks_submitted', submitted)
            log.info(
                f"PARTIAL_MM0_PROMOTE: {self.id} queued mm0 repair "
                f"(distance={distance_nm:.1f}nm, submitted={submitted}, "
                f"pending={len(not_ready)})"
                if distance_nm is not None else
                f"PARTIAL_MM0_PROMOTE: {self.id} queued mm0 repair "
                f"(distance=unknown, submitted={submitted}, pending={len(not_ready)})"
            )
            return True
        except Exception as e:
            self._release_mm0_promotion_claim()
            log.debug(f"Partial mm0 promotion failed for {self.id}: {e}")
            bump('partial_mm0_promote_error')
            return False

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

        requested_mipmap = self.find_mipmap_pos(offset)

        # ═══════════════════════════════════════════════════════════════════
        # PERSISTENT DDS CACHE: Check Dynamic DDS Cache first (disk, cross-session)
        # ═══════════════════════════════════════════════════════════════════
        # The persistent cache survives across sessions. On a warm start this
        # provides ~1-2ms per tile vs ~390ms for a full rebuild.
        if dynamic_dds_cache is not None and not self._prepopulated:
            cache_has_requested_mipmap = True
            try:
                meta = dynamic_dds_cache.load_metadata(self.id, self.max_zoom, self)
                populated = meta.get("populated_mipmaps") if meta else None
                self._load_partial_dds_rows(meta)
                if populated is not None and 0 not in populated:
                    cache_has_requested_mipmap = False
                    log.debug(
                        f"GET_BYTES: DDS cache entry for {self.id} is partial "
                        f"and missing mipmap 0; ignoring partial cache"
                    )
                    bump("dynamic_dds_cache_skip_partial_missing_mm0")
                    self._maybe_promote_partial_cache_to_mm0(requested_mipmap)
                elif populated is not None and requested_mipmap not in populated:
                    cache_has_requested_mipmap = False
                    log.debug(
                        f"GET_BYTES: DDS cache entry for {self.id} is partial "
                        f"and missing mipmap {requested_mipmap}; using live build"
                    )
                    bump("dynamic_dds_cache_skip_missing_mipmap")
            except Exception:
                cache_has_requested_mipmap = False

            cached_bytes = (
                dynamic_dds_cache.load(self.id, self.max_zoom, self)
                if cache_has_requested_mipmap else None
            )
            if cached_bytes is not None:
                if self._populate_dds_from_prebuilt(cached_bytes):
                    # FIX: Only return early if mm0 was actually populated.
                    # Partial DDS entries (from store_incremental) contain mm4-12 but
                    # NOT mm0. Returning True here would serve empty mm0 data to X-Plane
                    # and set _prepopulated=True, permanently blocking the complete DDS
                    # from being loaded later. This is the root cause of the 2.1.0
                    # pixelation regression: partial cache hit → empty mm0 served →
                    # complete DDS from BackgroundDDSBuilder never loaded.
                    if self.dds and self.dds.mipmap_list and self.dds.mipmap_list[0].retrieved:
                        log.debug(f"GET_BYTES: Dynamic DDS cache HIT (complete) for {self.id}")
                        bump('dynamic_dds_cache_hit')
                        return True
                    else:
                        log.debug(f"GET_BYTES: Dynamic DDS cache HIT (partial, mm0 missing) for {self.id} "
                                  f"— falling through to build paths")
                        bump('dynamic_dds_cache_hit_partial')
                else:
                    log.debug(f"GET_BYTES: Dynamic DDS cache hit but populate failed for {self.id}")
                    bump('dynamic_dds_cache_populate_fail')
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
                    if dynamic_dds_cache is not None:
                        cached_bytes = dynamic_dds_cache.load(self.id, self.max_zoom, self)
                        if cached_bytes is not None:
                            if self._populate_dds_from_prebuilt(cached_bytes):
                                # Only return if mm0 was populated (same guard as primary cache path)
                                if self.dds and self.dds.mipmap_list and self.dds.mipmap_list[0].retrieved:
                                    log.debug(f"GET_BYTES: DDS cache HIT after transition for {self.id}")
                                    bump('dds_cache_hit_after_transition')
                                    return True
        # ═══════════════════════════════════════════════════════════════════

        mipmap = requested_mipmap
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
        request_covers_mipmap_0 = (
            offset <= self.dds.mipmap_list[0].startpos
            and offset + length >= self.dds.mipmap_list[0].endpos
        )
        
        if mipmap == 0:
            log.debug(f"GET_BYTES_DIAG: mipmap=0 offset={offset} length={length} "
                     f"reaches_data={request_reaches_mipmap_0_data} "
                     f"covers_mipmap={request_covers_mipmap_0} "
                     f"already_attempted={self._aopipeline_attempted} "
                     f"retrieved={self.dds.mipmap_list[0].retrieved if self.dds and self.dds.mipmap_list else 'N/A'}")
        
        if (mipmap == 0 and 
            request_reaches_mipmap_0_data and
            request_covers_mipmap_0 and
            not self._aopipeline_attempted and
            self.dds is not None and
            len(self.dds.mipmap_list) > 0 and
            not self.dds.mipmap_list[0].retrieved):
            
            # DIAGNOSTIC: Track when we actually trigger aopipeline for mipmap 0
            bump('aopipeline_mipmap_0_triggered')
            log.info(f"AOPIPELINE_TRIGGER: {self.id} offset={offset} length={length}")
            
            self._aopipeline_attempted = True  # Prevent retry loops on failure
            
            try:
                aopipeline_budget = (
                    time_budget or self._get_or_create_tile_time_budget()
                )

                fallback_level = self._get_fallback_level()
                fallbacks_enabled = fallback_level > 0

                # ═══════════════════════════════════════════════════════════
                # FAST CACHE PROBE: Determine warm vs cold cache (~1-5ms)
                # ═══════════════════════════════════════════════════════════
                # Check how many chunks are already available in memory or
                # disk cache WITHOUT triggering any downloads.  This avoids
                # the batch aopipeline wasting the full tile_time_budget
                # downloading chunks on cold cache only to fail the
                # threshold check.
                cache_ratio = self._probe_chunk_cache_ratio(self.max_zoom)
                cache_is_warm = cache_ratio >= 1.0

                log.debug(f"GET_BYTES: Cache probe for {self.id}: "
                          f"{cache_ratio*100:.0f}% available, "
                          f"threshold=100%, "
                          f"warm={cache_is_warm}")

                if cache_is_warm:
                    # ═══════════════════════════════════════════════════════
                    # WARM CACHE: Batch aopipeline with main budget
                    # ═══════════════════════════════════════════════════════
                    # All/most chunks are cached, so _collect_chunk_jpegs
                    # Phase 1 will succeed instantly (~55ms build).  The
                    # full budget is passed so buffer pool contention or
                    # other edge cases use the user's configured budget.

                    if fallbacks_enabled:
                        if self._try_aopipeline_build(time_budget=aopipeline_budget, force_build_partial=False):
                            log.debug(f"GET_BYTES: warm-cache batch aopipeline succeeded for {self.id}")
                            return True

                        # Warm probe said ready but batch failed (race condition:
                        # chunk evicted between probe and collect).
                        # Retry streaming within the same tile-wide budget. A
                        # separate fallback extension is only created after the
                        # main budget is actually exhausted.
                        if getattr(CFG.autoortho, 'streaming_builder_enabled', True):
                            log.debug(f"GET_BYTES: warm-cache batch failed, trying streaming for {self.id}")

                            if self._try_streaming_aopipeline_build(time_budget=aopipeline_budget):
                                log.debug(f"GET_BYTES: streaming builder succeeded for {self.id}")
                                return True

                    else:
                        # FALLBACKS DISABLED: build with whatever batch collects
                        if self._try_aopipeline_build(time_budget=aopipeline_budget, force_build_partial=True):
                            log.debug(f"GET_BYTES: warm-cache batch (no fallbacks) succeeded for {self.id}")
                            return True

                    # Warm-cache paths all failed — fall through to progressive
                    log.debug(f"GET_BYTES: warm-cache paths failed, using progressive for {self.id}")
                    bump('live_aopipeline_fallback')

                else:
                    # ═══════════════════════════════════════════════════════
                    # COLD CACHE: Skip batch/streaming entirely
                    # ═══════════════════════════════════════════════════════
                    # Batch would waste the entire budget downloading then
                    # fail the threshold check.  Progressive handles
                    # downloads with per-chunk fallbacks and native C
                    # decode/compress.  The original time_budget is passed
                    # through untouched.
                    log.debug(f"GET_BYTES: cold cache ({cache_ratio*100:.0f}%), "
                              f"skipping batch/streaming for {self.id}")
                    bump('cold_cache_skip_batch')

                    # Clear stale pre-collected data (batch was skipped)
                    self._last_collected_jpegs = None
                    self._last_collected_missing = None
                    self._last_collected_ratio = None

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

        mm_offset = max(0, offset - self.dds.mipmap_list[mipmap].startpos)
        log.debug(f"MM_offset: {mm_offset}  Offset {offset}.  Startpos {self.dds.mipmap_list[mipmap]}")

        base_width_px = max(4, int(self.dds.width) >> mipmap)
        base_height_px = max(4, int(self.dds.height) >> mipmap)
        (
            startrow,
            endrow,
            bytes_per_chunk_row,
            chunk_rows_in_mm,
        ) = self._partial_row_range(mipmap, offset, length)

        log.debug(f"Startrow: {startrow} Endrow: {endrow} bytes_per_chunk_row: {bytes_per_chunk_row} width_px: {base_width_px} height_px: {base_height_px}")
        bump("partial_rows_requested", endrow - startrow + 1)
        
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
                # (ready.set() already called inside _try_native_partial_mipmap_build)
                self._maybe_schedule_read_ahead(
                    mipmap,
                    startrow,
                    endrow,
                    bytes_per_chunk_row,
                )
                return True
        
        # ═══════════════════════════════════════════════════════════════════
        # PYTHON FALLBACK PATH
        # ═══════════════════════════════════════════════════════════════════
        wait_seconds = self.get_maxwait()
        if time_budget is not None:
            wait_seconds = min(
                wait_seconds,
                max(0.0, time_budget.remaining),
            )
        coordinator = self._get_mipmap_build_coordinator(mipmap)
        action, revision = coordinator.begin_partial(
            startrow,
            endrow,
            time.monotonic() + wait_seconds,
        )
        if action == "covered":
            return True
        if action != "build":
            return False

        new_im = None
        build_succeeded = False
        start_time = time.monotonic()
        try:
            new_im = self.get_img(
                mipmap,
                startrow,
                endrow,
                maxwait=self.get_maxwait(),
                time_budget=time_budget,
            )
            if not new_im or self.dds is None:
                return False

            img_data = new_im.data_ptr()
            pixel_width, pixel_height = new_im.size
            dxtdata = self.dds.compress(
                pixel_width,
                pixel_height,
                img_data,
            )
            if dxtdata is None:
                return False
            dxtdata = bytes(dxtdata)
            expected_length = (
                (endrow - startrow + 1) * bytes_per_chunk_row
            )
            if len(dxtdata) != expected_length:
                log.error(
                    "Python partial build length mismatch for %s mm%d: "
                    "%d != %d",
                    self.id,
                    mipmap,
                    len(dxtdata),
                    expected_length,
                )
                return False
            if not coordinator.can_commit_partial(revision):
                return True

            with self._dds_write_lock:
                self.ready.clear()
                try:
                    mm = self.dds.mipmap_list[mipmap]
                    sparse = self.dds.ensure_sparse_mipmap(
                        mipmap,
                        bytes_per_chunk_row,
                        coordinator.total_rows,
                    )
                    self.dds.write_mipmap_at(
                        mipmap,
                        startrow * bytes_per_chunk_row,
                        dxtdata,
                    )
                    mm.retrieved = False
                    profile_gauge(
                        "dds.sparse_allocated_bytes",
                        sparse.allocated_bytes(),
                    )
                finally:
                    self.ready.set()

            degraded_rows = set()
            grid = self.chunks.get(self.max_zoom - mipmap)
            if grid is not None:
                for row_index in range(startrow, endrow + 1):
                    row_chunks = grid.ensure_rows(row_index, row_index)
                    if any(not chunk.data for chunk in row_chunks):
                        degraded_rows.add(row_index)
            self._queue_partial_rows(
                mipmap,
                startrow,
                endrow,
                bytes_per_chunk_row,
                dxtdata,
                degraded_rows,
            )
            build_succeeded = True
            self._dds_coverage_revision += 1
            return True
        finally:
            if new_im is not None and mipmap not in self.imgs:
                try:
                    new_im.close()
                except Exception:
                    pass
            coordinator.finish_partial(
                startrow,
                endrow,
                revision,
                build_succeeded,
            )
            if build_succeeded and self.dds is not None:
                with self._dds_write_lock:
                    mm = self.dds.mipmap_list[mipmap]
                    if (
                        mm.buffer is not None
                        and mm.buffer.is_complete()
                        and not mm.retrieved
                    ):
                        dense = mm.buffer.read_at(0, mm.length)
                        self.dds.replace_mipmap_dense(
                            mipmap,
                            dense,
                            complete=True,
                        )
                        bump("sparse_to_dense_promotions")
                self._maybe_persist_complete_dds()
            tile_time = time.monotonic() - start_time
            partial_stats.set(mipmap, tile_time)
            bump_many({
                f"partial_mm_count:{mipmap}": 1,
                f"partial_mm_time_total_ms:{mipmap}": int(tile_time * 1000),
            })
            if build_succeeded:
                self._maybe_schedule_read_ahead(
                    mipmap,
                    startrow,
                    endrow,
                    bytes_per_chunk_row,
                )

    def read_dds_bytes(self, offset, length):
        log.debug(f"READ DDS BYTES: {offset} {length}")

        # Signal that a live tile read is in progress.
        # While any live reads are active, prefetching and background DDS
        # building pause so all chunk download and build resources serve
        # X-Plane's requests.
        _live_read_start()
        try:
            return self._read_dds_bytes_inner(offset, length)
        finally:
            _live_read_end()

    @profiled_stage("tile.dds_read")
    def _read_dds_bytes_inner(self, offset, length):
        # Every read for the same open tile shares one wall-clock budget. This
        # matches the user-facing "complete tile" contract and prevents X-Plane's
        # block-oriented reads from multiplying the configured timeout.
        request_budget = self._get_or_create_tile_time_budget()

        # Track when this tile was first requested (for stats only)
        if self.first_request_time is None:
            self.first_request_time = time.monotonic()
            log.debug(f"READ_DDS_BYTES: First request for tile")
       
        if offset > 0 and offset < self.lowest_offset:
            self.lowest_offset = offset

        request_end = offset + length
        for mipmap in self.dds.mipmap_list:
            overlap_start = max(offset, mipmap.startpos)
            overlap_end = min(request_end, mipmap.endpos)
            if overlap_start >= overlap_end:
                continue
            overlap_length = overlap_end - overlap_start
            if (
                overlap_start == mipmap.startpos
                and overlap_length >= mipmap.length
            ):
                self.get_mipmap(
                    mipmap.idx,
                    time_budget=request_budget,
                )
            else:
                self.get_bytes(
                    overlap_start,
                    overlap_length,
                    time_budget=request_budget,
                )
        
        self.bytes_read += length
        return self.dds.read_at(offset, length)

    def _get_or_create_tile_time_budget(self):
        use_time_budget = getattr(CFG.autoortho, "use_time_budget", True)
        if isinstance(use_time_budget, str):
            use_time_budget = use_time_budget.lower().strip() in (
                "true", "1", "yes", "on"
            )
        if not use_time_budget:
            return None

        with self._time_budget_lock:
            if self._tile_time_budget is None:
                budget_seconds = max(
                    0.1,
                    float(getattr(CFG.autoortho, "tile_time_budget", 60.0)),
                )
                suspend_maxwait = getattr(
                    CFG.autoortho, "suspend_maxwait", False
                )
                if isinstance(suspend_maxwait, str):
                    suspend_maxwait = suspend_maxwait.lower().strip() in (
                        "true", "1", "yes", "on"
                    )
                if (
                    suspend_maxwait
                    and not getattr(datareftracker, "has_ever_connected", False)
                ):
                    budget_seconds = min(budget_seconds * 10.0, 1800.0)
                self._tile_time_budget = TimeBudget(budget_seconds)
            return self._tile_time_budget

    def _get_or_create_fallback_time_budget(self):
        if self.get_fallback_level() < 2 or not self.get_fallback_extends_budget():
            return None
        with self._time_budget_lock:
            if self._fallback_time_budget is None:
                timeout = max(
                    0.1,
                    float(getattr(CFG.autoortho, "fallback_timeout", 30.0)),
                )
                self._fallback_time_budget = TimeBudget(timeout)
            return self._fallback_time_budget

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

    @profiled_stage("image.compose")
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
        with self._lock:
            cached_img_data = self.imgs.get(mipmap)
        if cached_img_data is not None:
            # Unpack tuple format (new) or return directly (old format, backward compat)
            if isinstance(cached_img_data, tuple):
                img = cached_img_data[0]  # Extract just the image
                log.debug(f"GET_IMG: Found saved image: {img}")
                return img
            else:
                # Old format: just the image without metadata
                log.debug(f"GET_IMG: Found saved image (old format): {cached_img_data}")
                return cached_img_data

        log.debug(f"GET_IMG: MM List before { {x.idx:x.retrieved for x in self.dds.mipmap_list} }")
        with self._lock:
            already_retrieved = mipmap < len(self.dds.mipmap_list) and self.dds.mipmap_list[mipmap].retrieved
        if already_retrieved:
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


        # TODO: For further optimization, this wait could be moved to callers of get_img.
        # For now, the per-tile event-based wait is much faster than notify_all().
        
        grid = self._get_chunk_grid(zoom, min_zoom)
        if complete_img:
            chunks = grid.ensure_all()
        else:
            chunks = grid.ensure_range(
                startchunk,
                endchunk if endchunk is not None else grid.logical_length,
            )
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
        partial_row_offset = 0
        if complete_img:
            img_height = 256 * height
        else:
            requested_rows = max(1, endrow - startrow + 1)
            img_height = 256 * requested_rows
            partial_row_offset = startrow * 256
        
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
        
        if complete_img and mipmap == 0 and fallback_level >= 1:
            # Try to find the best available lower mipmap to use as base
            for source_mm in [1, 2, 3, 4]:
                if source_mm > self.max_mipmap:
                    continue
                    
                with self._lock:
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
                
                # Calculate expected dimensions using _get_quick_zoom which
                # correctly accounts for min_zoom capping. Without this, the
                # bit-shift calculation (img_width >> source_mm) gives wrong
                # expected sizes when min_zoom > max_zoom - source_mm.
                source_zoom = self.max_zoom - source_mm
                _, _, sw_chunks, sh_chunks, _, _ = self._get_quick_zoom(source_zoom, self.min_zoom)
                expected_source_width = 256 * sw_chunks
                expected_source_height = 256 * sh_chunks

                if source_img._width != expected_source_width or source_img._height != expected_source_height:
                    log.debug(f"GET_IMG: Prefill skipping mipmap {source_mm} - size mismatch: "
                             f"got {source_img._width}x{source_img._height}, expected {expected_source_width}x{expected_source_height}")
                    continue

                # Calculate scale factor from actual dimensions
                scale_factor_w = img_width // expected_source_width
                scale_factor_h = img_height // expected_source_height
                if scale_factor_w != scale_factor_h or scale_factor_w < 1:
                    log.debug(f"GET_IMG: Prefill skipping mipmap {source_mm} - non-uniform scale: "
                              f"{scale_factor_w}x{scale_factor_h}")
                    continue
                scale_factor = scale_factor_w
                
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
        
        # Track if any pass needs a lazy build
        needs_lazy_build = False

        # THREE-PASS CHUNK COLLECTION
        #
        # Architecture:
        # 1. QUICK PASS: Process all already-ready chunks immediately (non-blocking)
        # 2. POLL PASS: Poll deferred chunks as they arrive, processing each immediately
        # 3. FALLBACK PASS: Apply disk cache / mipmap scaling / network fallbacks to still-missing chunks
        #
        # This prevents a single slow chunk from blocking the entire tile build.
        # Ready chunks are processed immediately regardless of submission order.

        total_chunks = len(chunks)
        chunks_with_images = set()  # Track which chunks have images for fallback sweep

        # === PASS 1: QUICK COLLECT (non-blocking) ===
        # Process all already-ready chunks in one fast sweep (~0ms wait)
        deferred_chunks = []  # [(chunk, start_x, start_y)]

        for chunk in chunks:
            start_x = int(chunk.width * (chunk.col - col))
            start_y = int(chunk.height * (chunk.row - row)) - partial_row_offset

            # Validate coordinates
            if start_x < 0 or start_y < 0:
                log.error(f"GET_IMG: Invalid negative coordinates: start_x={start_x}, start_y={start_y}")
                continue
            if start_x + chunk.width > img_width or start_y + chunk.height > img_height:
                log.error(f"GET_IMG: Coordinates extend beyond image: pos=({start_x},{start_y})")
                continue

            if chunk.permanent_failure:
                bump('chunk_missing_count')
                # Still add to deferred for fallback processing
                deferred_chunks.append((chunk, start_x, start_y))
                continue

            chunk_data = chunk.data  # TOCTOU-safe snapshot
            if chunk.ready.is_set() and chunk_data:
                # Decode immediately
                try:
                    with _decode_sem:
                        chunk_img = AoImage.load_from_memory(chunk_data)
                    if chunk_img:
                        _safe_paste(new_im, chunk_img, start_x, start_y)
                        chunks_with_images.add(id(chunk))
                        time_budget.record_chunk_processed()
                    else:
                        deferred_chunks.append((chunk, start_x, start_y))
                except Exception as e:
                    log.error(f"GET_IMG: Pass 1 decode exception for {chunk}: {e}")
                    deferred_chunks.append((chunk, start_x, start_y))
            else:
                deferred_chunks.append((chunk, start_x, start_y))

        pass1_ready = len(chunks_with_images)
        log.debug(f"GET_IMG: Pass 1 (quick): {pass1_ready}/{total_chunks} chunks ready, "
                 f"{len(deferred_chunks)} deferred")

        # === PASS 2: POLL DEFERRED CHUNKS (time-capped) ===
        # Poll all deferred chunks, processing any that become ready.
        # Each chunk gets at most `maxwait` seconds total before being moved to
        # fallback (Pass 3). The pass itself is capped at `maxwait` wall-clock
        # time — once that expires, ALL remaining chunks move to fallback.
        # This prevents a single slow chunk from consuming the entire budget.
        if deferred_chunks and not time_budget.exhausted:
            still_waiting = [(chunk, sx, sy) for chunk, sx, sy in deferred_chunks
                             if id(chunk) not in chunks_with_images and not chunk.permanent_failure]

            # Per-chunk deadline tracking: each chunk gets maxwait seconds from now
            pass2_start = time.monotonic()
            pass2_deadline = pass2_start + maxwait  # Cap entire pass at maxwait
            chunk_first_seen = {}  # chunk id -> monotonic time first polled

            while still_waiting and not time_budget.exhausted:
                now = time.monotonic()
                if now >= pass2_deadline:
                    log.debug(f"GET_IMG: Pass 2 deadline reached after {now - pass2_start:.2f}s, "
                             f"{len(still_waiting)} chunks moving to fallback")
                    # Cancel all remaining chunks to free worker slots
                    for chunk, _, _ in still_waiting:
                        chunk.cancel()
                    break

                newly_ready = []
                timed_out = []
                remaining = []

                for item in still_waiting:
                    chunk, sx, sy = item
                    chunk_id = id(chunk)

                    # Track first-seen time for per-chunk timeout
                    if chunk_id not in chunk_first_seen:
                        chunk_first_seen[chunk_id] = now

                    if chunk.ready.is_set():
                        newly_ready.append(item)
                    elif now - chunk_first_seen[chunk_id] >= maxwait:
                        # This chunk has exceeded its per-chunk maxwait — give up
                        timed_out.append(item)
                    else:
                        remaining.append(item)

                # Process all newly ready chunks
                for chunk, sx, sy in newly_ready:
                    chunk_data = chunk.data
                    if chunk_data:
                        try:
                            with _decode_sem:
                                chunk_img = AoImage.load_from_memory(chunk_data)
                            if chunk_img:
                                _safe_paste(new_im, chunk_img, sx, sy)
                                chunks_with_images.add(id(chunk))
                                time_budget.record_chunk_processed()
                        except Exception as e:
                            log.error(f"GET_IMG: Pass 2 decode exception for {chunk}: {e}")

                if timed_out:
                    log.debug(f"GET_IMG: Pass 2: {len(timed_out)} chunks timed out after maxwait={maxwait:.1f}s")
                    bump('chunk_pass2_timeout', len(timed_out))
                    # Cancel timed-out chunks to free their worker slots
                    for chunk, _, _ in timed_out:
                        chunk.cancel()

                still_waiting = remaining
                if not still_waiting:
                    break

                # Wait briefly for ANY chunk to arrive (250ms poll interval)
                wait_cap = min(0.25, pass2_deadline - time.monotonic())
                if wait_cap <= 0:
                    break
                chunk_to_wait = still_waiting[0][0]
                time_budget.wait_with_budget(chunk_to_wait.ready, max_single_wait=wait_cap)

            # Update deferred_chunks for pass 3
            deferred_chunks = [(chunk, sx, sy) for chunk, sx, sy in deferred_chunks
                               if id(chunk) not in chunks_with_images]

        pass2_ready = len(chunks_with_images) - pass1_ready
        log.debug(f"GET_IMG: Pass 2 (poll): {pass2_ready} additional chunks, "
                 f"{len(deferred_chunks)} still deferred")

        # === PASS 3: FALLBACK RESOLUTION ===
        # Apply fallback chain to still-missing chunks: disk cache, mipmap scaling, network
        if deferred_chunks and fallback_level >= 1:
            for chunk, sx, sy in deferred_chunks:
                if id(chunk) in chunks_with_images:
                    continue
                if time_budget.exhausted and not fallback_extends_budget:
                    chunk.cancel()  # Free worker slot
                    time_budget.record_chunk_skipped()
                    bump('chunk_budget_skipped')
                    continue

                chunk_img = None
                is_permanent_failure = chunk.permanent_failure

                # Budget exhaustion: attempt local fallbacks only (fast, sub-ms)
                budget_exhausted_at_entry = time_budget.exhausted
                if budget_exhausted_at_entry:
                    bump('chunk_budget_exhausted_local_fallback')

                # Fallback 1: disk cache (fast, ~1ms)
                chunk_img = self.get_best_chunk(chunk.col, chunk.row, mipmap, zoom)

                # Track lazy build need
                if not chunk_img and mipmap == 0:
                    with self._lock:
                        lazy_attempted = self._lazy_build_attempted
                    if not lazy_attempted:
                        needs_lazy_build = True

                # Fallback 2: mipmap scaling (fast, in-memory)
                if not chunk_img:
                    chunk_img = self.get_downscaled_from_higher_mipmap(mipmap, chunk.col, chunk.row, zoom)

                # Fallback 3: network (only if budget allows and fallback_level >= 2)
                if not chunk_img and (not is_permanent_failure or True) and fallback_level >= 2:
                    if time_budget.exhausted and not fallback_extends_budget:
                        pass  # Skip network fallback
                    elif time_budget.exhausted and fallback_extends_budget:
                        if fallback_budget is None:
                            fallback_budget = self._get_or_create_fallback_time_budget()
                            if fallback_budget is not None:
                                log.info(
                                    "GET_IMG: Main budget exhausted, using shared "
                                    f"fallback budget {fallback_timeout:.1f}s"
                                )
                        if fallback_budget is not None and not fallback_budget.exhausted:
                            chunk_img = self.get_or_build_lower_mipmap_chunk(
                                mipmap, chunk.col, chunk.row, zoom,
                                main_budget=None,
                                fallback_budget=fallback_budget
                            )
                    elif not budget_exhausted_at_entry:
                        chunk_img = self.get_or_build_lower_mipmap_chunk(
                            mipmap, chunk.col, chunk.row, zoom,
                            main_budget=time_budget,
                            fallback_budget=fallback_budget,
                            fallback_timeout=fallback_timeout if fallback_extends_budget else None
                        )

                # Final retry: check if chunk completed during fallback attempts
                if not chunk_img and not is_permanent_failure:
                    chunk_data = chunk.data
                    if chunk.ready.is_set() and chunk_data:
                        try:
                            with _decode_sem:
                                chunk_img = AoImage.load_from_memory(chunk_data)
                        except Exception:
                            chunk_img = None

                if chunk_img:
                    _safe_paste(new_im, chunk_img, sx, sy)
                    chunks_with_images.add(id(chunk))
                    time_budget.record_chunk_processed()
                else:
                    # All fallbacks exhausted — cancel chunk to free worker slot
                    chunk.cancel()
                    bump('chunk_missing_count')
                    if is_permanent_failure:
                        log.info(f"Chunk {chunk} permanently failed and all fallbacks exhausted")

        pass3_ready = len(chunks_with_images) - pass1_ready - pass2_ready
        if pass3_ready > 0:
            log.debug(f"GET_IMG: Pass 3 (fallback): {pass3_ready} additional chunks recovered")

        # Cancel any remaining unprocessed chunks to free worker slots.
        # This catches chunks that were never processed (e.g. fallback_level=0)
        # or that fell through all passes without getting an image.
        for chunk in chunks:
            if id(chunk) not in chunks_with_images and not chunk.ready.is_set():
                chunk.cancel()

        # === DEFERRED LAZY BUILD ===
        # If any executor thread signaled that lazy build is needed, do it now.
        # This runs in the main thread, avoiding the lock contention that caused
        # 300s stalls when calling get_img() from executor threads.
        with self._lock:
            lazy_attempted = self._lazy_build_attempted
        if needs_lazy_build and mipmap == 0 and not lazy_attempted:
            log.debug(f"GET_IMG: Running deferred lazy build (main thread, lock held)")
            self._try_lazy_build_fallback_mipmap(time_budget)
            
            # After lazy build, re-process any chunks that still need images
            # The lazy build created lower-detail mipmaps that Fallback 2 can use
            missing_after_lazy = [c for c in chunks if id(c) not in chunks_with_images 
                                  and not c.permanent_failure]
            if missing_after_lazy and len(self.imgs) > 0:
                log.debug(f"GET_IMG: Re-processing {len(missing_after_lazy)} chunks after lazy build")
                for chunk in missing_after_lazy:
                    # get_downscaled_from_higher_mipmap is pure in-memory
                    # scaling -- always worth attempting regardless of budget.
                    chunk_img = self.get_downscaled_from_higher_mipmap(mipmap, chunk.col, chunk.row, zoom)
                    if chunk_img:
                        start_x = int(chunk.width * (chunk.col - col))
                        start_y = (
                            int(chunk.height * (chunk.row - row))
                            - partial_row_offset
                        )
                        _safe_paste(new_im, chunk_img, start_x, start_y)
                        chunks_with_images.add(id(chunk))
                        log.debug(f"GET_IMG: Recovered chunk via deferred lazy build fallback")

        # Determine if we need to cache this image for fallback/upscaling
        should_cache = complete_img and mipmap <= self.max_mipmap
        
        if should_cache:
            log.debug(f"GET_IMG: Save complete image for later...")
            # Store image with metadata (col, row, zoom) for coordinate mapping in upscaling
            # Use _cache_image for LRU eviction to prevent unbounded memory growth
            with self._lock:
                should_cache = self._cache_image(
                    mipmap,
                    (new_im, col, row, zoom),
                )

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

    def _try_incremental_dds_store(self, mipmap):
        """Persist intermediate mipmaps (1+) incrementally to DDS cache."""
        if mipmap == 0 or dynamic_dds_cache is None or self.dds is None:
            return
        if not _persist_partial_dds:
            return
        _partially_cached = getattr(self, '_dds_populated_mipmaps', None) is not None
        if self._prepopulated and not _partially_cached:
            return
        if self._completion_reported:
            return
        try:
            mm_data = {}
            mm_offsets = {}
            for i in range(mipmap, len(self.dds.mipmap_list)):
                mm = self.dds.mipmap_list[i]
                if mm.retrieved and mm.buffer is not None:
                    data = mm.buffer.read_at(0, mm.length)
                    if data:
                        mm_data[i] = data
                        mm_offsets[i] = (mm.startpos, mm.length)
            if mm_data:
                dynamic_dds_cache.store_incremental(
                    self.id, self.max_zoom,
                    self.row, self.col, self.maptype,
                    self.tilename_zoom,
                    self.dds.header.getvalue(),
                    self.dds.total_size,
                    self.dds.width, self.dds.height,
                    self.dds.mipMapCount,
                    mm_data, mm_offsets
                )
        except Exception as e:
            log.warning(f"Incremental DDS cache store failed for {self.id} mm{mipmap}: {e}")

    @profiled_stage("dds.native_mipmap_build")
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
        1. Batch cache read with _batch_read_cache_files() - parallel I/O
        2. Download missing chunks if below threshold - respects time budget
        
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
        
        # Always require all chunks for native build — if any are missing,
        # fall through to the Python path which handles per-chunk fallbacks.
        min_ratio = 1.0
        
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 1: Create chunks and collect data from cache
        # ═══════════════════════════════════════════════════════════════════════
        
        # Ensure chunks exist for this zoom level
        chunks = self._create_chunks(zoom).ensure_all()
        
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
        
        if ready_count == total_chunks:
            log.debug(f"_try_native_mipmap_build: All {total_chunks} chunks from memory")
        
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
                    # Wait for downloads capped at maxwait (per-chunk timeout).
                    # Previously used time_budget.remaining (up to 180s), which
                    # caused the native path to spin for minutes when a single
                    # chunk was unreachable, then fall through to get_img() for
                    # another 180s — total 360s stall.
                    maxwait_cap = self.get_maxwait()
                    wait_time = min(time_budget.remaining, maxwait_cap)
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
            return False
        
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
                # Collect JPEG data for ALL mipmap levels from current to smallest.
                # CRITICAL: _collect_chunks_for_zoom does NOT trigger downloads,
                # so lower zoom levels may have mostly-None data if chunks haven't
                # been fetched yet. Passing that incomplete data to the native
                # builder would "poison" those mipmaps with missing_color AND mark
                # them retrieved=True, preventing later proper builds.
                # Fix: once a zoom level falls below the availability threshold,
                # pass empty arrays for it and all subsequent levels so the native
                # builder derives them via reduce_half from the last good level.
                jpeg_datas_per_zoom = []
                chain_truncated = False
                for mm in range(mipmap, self.dds.smallest_mm + 1):
                    mm_zoom = self.max_zoom - mm
                    if mm_zoom < self.min_zoom:
                        jpeg_datas_per_zoom.append([])
                        continue
                    
                    if mm == mipmap:
                        jpeg_datas_per_zoom.append(jpeg_datas)
                    elif chain_truncated:
                        jpeg_datas_per_zoom.append([])
                    else:
                        mm_jpeg_datas = self._collect_chunks_for_zoom(mm_zoom)
                        available = sum(1 for d in mm_jpeg_datas if d is not None) if mm_jpeg_datas else 0
                        total = len(mm_jpeg_datas) if mm_jpeg_datas else 0
                        if total > 0 and (available / total) >= min_ratio:
                            jpeg_datas_per_zoom.append(mm_jpeg_datas)
                        else:
                            log.debug(f"_try_native_mipmap_build: ZL{mm_zoom} has "
                                     f"{available}/{total} chunks "
                                     f"({(available/total*100) if total else 0:.0f}% vs "
                                     f"{min_ratio*100:.0f}% threshold), "
                                     f"truncating chain at mm{mm}")
                            jpeg_datas_per_zoom.append([])
                            chain_truncated = True
                
                build_fn = native_dds.build_all_mipmaps_native
                build_args = (jpeg_datas_per_zoom,)
                build_kwargs = {'format': dxt_format, 'missing_color': missing_color}
            elif hasattr(native_dds, 'build_mipmap_chain'):
                build_fn = native_dds.build_mipmap_chain
                build_args = (jpeg_datas,)
                build_kwargs = {'format': dxt_format, 'missing_color': missing_color,
                                'max_mipmaps': max_mipmaps}
            else:
                build_fn = native_dds.build_single_mipmap
                build_args = (jpeg_datas,)
                build_kwargs = {'format': dxt_format, 'missing_color': missing_color}

            lease_sources = [
                data
                for zoom_datas in jpeg_datas_per_zoom
                for data in zoom_datas
                if data
            ] if "jpeg_datas_per_zoom" in locals() else jpeg_datas
            with self._source_lease(lease_sources):
                with _native_build_context() as threads:
                    result = build_fn(
                        *build_args,
                        max_threads=threads,
                        **build_kwargs,
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
            
            # Write mipmap data to DDS buffers — short critical section
            with self._dds_write_lock:
                self.ready.clear()
                if hasattr(result, 'mipmap_count') and result.mipmap_count > 0:
                    # MipmapChainResult: write each mipmap to its DDS buffer
                    for i in range(result.mipmap_count):
                        target_mipmap = mipmap + i
                        if target_mipmap < len(self.dds.mipmap_list):
                            mip_data = result.get_mipmap_data(i)
                            if mip_data:
                                self.dds.replace_mipmap_dense(
                                    target_mipmap,
                                    mip_data,
                                )

                    # For mipmaps beyond smallest_mm, copy the 4×4 block
                    # (This matches Python gen_mipmaps behavior)
                    smallest_mm = self.dds.smallest_mm
                    if mipmap + result.mipmap_count - 1 >= smallest_mm:
                        smallest_data = result.get_mipmap_data(result.mipmap_count - 1)
                        if smallest_data:
                            for mm in self.dds.mipmap_list[smallest_mm + 1:]:
                                self.dds.replace_mipmap_dense(
                                    mm.idx,
                                    smallest_data,
                                )

                    log.debug(f"_try_native_mipmap_build: Built {result.mipmap_count} mipmaps "
                             f"({mipmap} to {mipmap + result.mipmap_count - 1})")
                else:
                    # SingleMipmapResult: only write the one mipmap
                    self.dds.replace_mipmap_dense(mipmap, result.data)
                self.ready.set()
            
            # Record timing stats
            total_time = time.monotonic() - build_start
            native_time = time.monotonic() - native_build_start
            record_stage(
                "dds.native_compute",
                native_time * 1000.0,
                tile_id=self.id,
                details={
                    "path": "mipmap",
                    "mipmap": mipmap,
                    "threads": threads,
                },
            )
            
            mm_stats.set(mipmap, total_time)
            tile_creation_stats.set(mipmap, total_time)
            
            if log.isEnabledFor(logging.DEBUG):
                mipmaps_built = getattr(result, 'mipmap_count', 1)
                active, total_threads = _native_thread_load()
                log.debug(f"_try_native_mipmap_build: SUCCESS mipmap {mipmap} - "
                         f"{len(result.data)} bytes ({mipmaps_built} mipmaps) in {total_time*1000:.0f}ms "
                         f"(native: {native_time*1000:.0f}ms, {ready_count}/{total_chunks} chunks, "
                         f"{threads} threads, {active} concurrent, "
                         f"{total_threads}/{CURRENT_CPU_COUNT} threads in flight)")
            
            self._maybe_persist_complete_dds()
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
        chunks = self._create_chunks(zoom).ensure_all()
        
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
        time_budget=None,
        deadline=None,
        priority=PRIORITY_LIVE,
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
        grid = self._get_chunk_grid(zoom)
        chunks_width = grid.width
        chunks_height = endrow - startrow + 1

        if startrow < 0 or endrow >= grid.height or startrow > endrow:
            log.debug(f"_collect_row_chunk_jpegs: Invalid row range {startrow}-{endrow} "
                     f"for {grid.height} rows")
            return [], 0, 0

        row_chunks = grid.ensure_rows(startrow, endrow)
        # Submit downloads for any chunks not ready
        for chunk in row_chunks:
            if not chunk.ready.is_set():
                if not getattr(chunk, 'in_queue', False) and not getattr(chunk, 'in_flight', False):
                    chunk.priority = priority
                    chunk.prefetch = priority != PRIORITY_LIVE
                    chunk_getter.submit(chunk)

        if deadline is None:
            maxwait_cap = self.get_maxwait()
            max_wait = (
                min(time_budget.remaining, maxwait_cap)
                if time_budget
                else min(2.0, maxwait_cap)
            )
            deadline = time.monotonic() + max(0.0, max_wait)
        if not grid.wait_for(row_chunks, deadline):
            bump("row_batch_deadline_expired")

        jpeg_datas = [
            chunk.data if chunk.ready.is_set() and chunk.data else None
            for chunk in row_chunks
        ]
        return jpeg_datas, chunks_width, chunks_height

    def _resolve_partial_sources(
        self,
        jpeg_datas,
        startrow,
        chunks_width,
        zoom,
        deadline,
    ):
        """Resolve missing row slots into target-sized prepared RGBA sources."""
        sources = list(jpeg_datas)
        fallback_indices = []
        missing_indices = []
        fallback_level = self.get_fallback_level()
        grid = self._get_chunk_grid(zoom)
        row_chunks = grid.ensure_range(
            startrow * chunks_width,
            startrow * chunks_width + len(jpeg_datas),
        )
        resolver = None
        budget = None
        if fallback_level > 0:
            try:
                try:
                    from autoortho.aopipeline.fallback_resolver import (
                        FallbackResolver,
                        TimeBudget as FBTimeBudget,
                    )
                except ImportError:
                    from aopipeline.fallback_resolver import (
                        FallbackResolver,
                        TimeBudget as FBTimeBudget,
                    )
                resolver = FallbackResolver(
                    cache_dir=self.cache_dir,
                    maptype=self.maptype,
                    tile_col=self.col,
                    tile_row=self.row,
                    tile_zoom=self.max_zoom,
                    fallback_level=fallback_level,
                    max_mipmap=self.max_mipmap,
                    downloader=None,
                )
                resolver.set_mipmap_images(self.imgs)
                budget = FBTimeBudget(
                    max(0.01, deadline - time.monotonic())
                )
            except Exception:
                resolver = None

        for local_index, source in enumerate(sources):
            if source is not None:
                continue
            global_index = startrow * chunks_width + local_index
            chunk = row_chunks[local_index]
            rgba = None
            if resolver is not None and not budget.exhausted:
                try:
                    rgba = resolver.resolve(
                        chunk.col,
                        chunk.row,
                        zoom,
                        target_mipmap=0,
                        time_budget=budget,
                    )
                except Exception:
                    rgba = None
            if rgba:
                sources[local_index] = {
                    "pixels": rgba,
                    "mode": "RGBA",
                    "width": 256,
                    "height": 256,
                }
                fallback_indices.append(global_index)
                bump("partial_lower_zl_fallback_chunks")
            else:
                missing_indices.append(global_index)
                bump("partial_missing_color_chunks")
        return sources, fallback_indices, missing_indices

    @profiled_stage("dds.native_partial_build")
    def _try_native_partial_mipmap_build(
        self,
        mipmap: int,
        startrow: int,
        endrow: int,
        bytes_per_chunk_row: int,
        time_budget=None,
        priority=PRIORITY_LIVE,
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
        
        max_wait = self.get_maxwait()
        if time_budget is not None:
            max_wait = min(max_wait, max(0.0, time_budget.remaining))
        deadline = time.monotonic() + max_wait
        coordinator = self._get_mipmap_build_coordinator(mipmap)
        action, revision = coordinator.begin_partial(
            startrow,
            endrow,
            deadline,
        )
        if action == "covered":
            return True
        if action != "build":
            if action == "timeout":
                bump("partial_build_join_timeout")
            return False

        build_succeeded = False
        try:
            jpeg_datas, chunks_width, chunks_height = self._collect_row_chunk_jpegs(
                zoom,
                startrow,
                endrow,
                time_budget,
                deadline=deadline,
                priority=priority,
            )

            chunk_count = chunks_width * chunks_height
            if chunk_count == 0:
                log.debug(f"_try_native_partial_mipmap_build: No chunks for rows {startrow}-{endrow}")
                return False

        # Check threshold - need ALL chunks for native partial build
            valid_count = sum(1 for d in jpeg_datas if d is not None)
            allow_incomplete = _get_bool_config(
                CFG.autoortho,
                "native_partial_allow_incomplete",
                False,
            )

            if valid_count < chunk_count and not allow_incomplete:
                log.debug(f"_try_native_partial_mipmap_build: threshold not met "
                         f"({valid_count}/{chunk_count} = {valid_count/chunk_count*100:.0f}%, "
                         f"need 100%)")
                bump('native_partial_mipmap_threshold_miss')
                bump("native_partial_strict_build")
                return False
            partial_sources = jpeg_datas
            fallback_indices = []
            missing_indices = []
            if valid_count < chunk_count:
                bump("native_partial_incomplete_build")
                (
                    partial_sources,
                    fallback_indices,
                    missing_indices,
                ) = self._resolve_partial_sources(
                    jpeg_datas,
                    startrow,
                    chunks_width,
                    zoom,
                    deadline,
                )
                with self._lock:
                    self._dds_missing_indices = sorted(
                        set(self._dds_missing_indices).union(missing_indices)
                    )
                    self._dds_fallback_indices = sorted(
                        set(self._dds_fallback_indices).union(
                            fallback_indices
                        )
                    )
                    self._dds_needs_healing = bool(
                        self._dds_missing_indices
                        or self._dds_fallback_indices
                    )
            else:
                bump("native_partial_strict_build")
        
            dxt_format = CFG.pydds.format.upper()
            missing_color = (
                CFG.autoortho.missing_color[0],
                CFG.autoortho.missing_color[1],
                CFG.autoortho.missing_color[2],
            )

            build_start = time.monotonic()
            
            # Build partial mipmap using native code
            with self._source_lease(
                source
                for source in partial_sources
                if isinstance(source, (bytes, bytearray))
            ):
                if (
                    allow_incomplete
                    and hasattr(native_dds, "build_partial_mipmap_v2")
                ):
                    result = native_dds.build_partial_mipmap_v2(
                        sources=partial_sources,
                        chunks_width=chunks_width,
                        chunks_height=chunks_height,
                        format=dxt_format,
                        missing_color=missing_color,
                    )
                else:
                    result = native_dds.build_partial_mipmap(
                        jpeg_datas=[
                            source
                            if isinstance(source, (bytes, bytearray))
                            else None
                            for source in partial_sources
                        ],
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
            
            if not coordinator.can_commit_partial(revision):
                return True

            with self._dds_write_lock:
                self.ready.clear()
                mm = self.dds.mipmap_list[mipmap]
                row_offset = startrow * bytes_per_chunk_row
                sparse = self.dds.ensure_sparse_mipmap(
                    mipmap,
                    bytes_per_chunk_row,
                    coordinator.total_rows,
                )
                self.dds.write_mipmap_at(mipmap, row_offset, result.data)
                allocated = sparse.allocated_bytes()
                bump("sparse_dds_allocated_bytes", len(result.data))
                bump(
                    "sparse_dds_avoided_dense_bytes",
                    max(0, mm.length - allocated),
                )
                profile_gauge("dds.sparse_allocated_bytes", allocated)
                self.ready.set()

            degraded_rows = {
                index // chunks_width
                for index in fallback_indices + missing_indices
            }
            self._queue_partial_rows(
                mipmap,
                startrow,
                endrow,
                bytes_per_chunk_row,
                result.data,
                degraded_rows,
            )

            build_succeeded = True
            build_time = time.monotonic() - build_start
            log.debug(f"_try_native_partial_mipmap_build: SUCCESS rows {startrow}-{endrow} "
                     f"({result.bytes_written} bytes at offset {row_offset}, "
                     f"{result.elapsed_ms:.1f}ms native, {build_time*1000:.1f}ms total)")
            
            
            return True
        except Exception as e:
            log.debug(f"_try_native_partial_mipmap_build: Exception: {e}")
            bump('native_partial_mipmap_exception')
            return False
        finally:
            coordinator.finish_partial(
                startrow,
                endrow,
                revision,
                build_succeeded,
            )
            if build_succeeded and self.dds is not None:
                with self._dds_write_lock:
                    mm = self.dds.mipmap_list[mipmap]
                    if (
                        mm.buffer is not None
                        and mm.buffer.is_complete()
                        and not mm.retrieved
                    ):
                        dense = mm.buffer.read_at(0, mm.length)
                        self.dds.replace_mipmap_dense(
                            mipmap,
                            dense,
                            complete=True,
                        )
                        bump("sparse_to_dense_promotions")
                self._maybe_persist_complete_dds()

    #@profile
    def get_mipmap(self, mipmap=0, time_budget=None):
        """
        Build a specific mipmap level.

        Per-mipmap serialization prevents duplicate builds of the same mipmap.
        IMPORTANT: The lock is NOT held across blocking I/O (chunk downloads).
        Only the DDS buffer write is protected by _dds_write_lock (~10-50ms).

        Args:
            mipmap: Mipmap level to build (0=highest detail, 7=lowest)
            time_budget: TimeBudget for this request (created fresh in read_dds_bytes)
        """
        if mipmap > self.max_mipmap:
            mipmap = self.max_mipmap

        wait_seconds = self.get_maxwait()
        if time_budget is not None:
            wait_seconds = max(0.0, time_budget.remaining)
        deadline = time.monotonic() + wait_seconds
        coordinator = self._get_mipmap_build_coordinator(mipmap)
        action, revision = coordinator.begin_full(deadline)
        if action == "complete":
            return True
        if action != "build":
            if action == "timeout":
                bump("mipmap_build_join_timeout")
            return False

        success = False
        try:
            result = self._get_mipmap_inner(mipmap, time_budget)
            success = bool(
                result
                and self.dds is not None
                and self.dds.mipmap_list[mipmap].retrieved
            )
            return result
        finally:
            coordinator.finish_full(revision, success)

    @profiled_stage("tile.mipmap_build")
    def _get_mipmap_inner(self, mipmap, time_budget):
        """Inner mipmap build logic.  No per-mipmap lock is held here."""
        # Start timing FULL tile creation (download + compose + compress)
        tile_creation_start = time.monotonic()

        log.debug(f"GET_MIPMAP: {self}")

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
        # NATIVE MIPMAP BUILD: Try fast native path for all mipmaps (0-4)
        # ═══════════════════════════════════════════════════════════════════════
        if self._try_native_mipmap_build(mipmap, time_budget):
            log.debug(f"GET_MIPMAP: Native build succeeded for mipmap {mipmap}")

            try:
                bump_many({f"mm_count:{mipmap}": 1})
            except Exception:
                pass

            self._try_incremental_dds_store(mipmap)
            self._maybe_persist_complete_dds()
            return True
        # ═══════════════════════════════════════════════════════════════════════

        # Python path: get_img runs WITHOUT tile lock held
        log.debug(f"GET_MIPMAP: Next call is get_img which may block!.............")
        new_im = self.get_img(mipmap, maxwait=self.get_maxwait(), time_budget=time_budget)
        if not new_im:
            log.debug("GET_MIPMAP: No updates, so no image generated")
            return True

        # DDS WRITE — short critical section (~10-50ms)
        compress_start_time = time.monotonic()
        with self._dds_write_lock:
            self.ready.clear()
            try:
                if mipmap == 0:
                    self.dds.gen_mipmaps(new_im, mipmap, 0)
                else:
                    self.dds.gen_mipmaps(new_im, mipmap)
            finally:
                self.ready.set()

        compress_end_time = time.monotonic()

        # Image cleanup (no lock needed)
        if mipmap not in self.imgs:
            try:
                new_im.close()
            except Exception:
                pass

        # Calculate timing metrics
        compress_time = compress_end_time - compress_start_time
        total_creation_time = compress_end_time - tile_creation_start
        record_stage(
            "dds.python_compress",
            compress_time * 1000.0,
            tile_id=self.id,
            details={"mipmap": mipmap},
        )

        mm_stats.set(mipmap, compress_time)
        tile_creation_stats.set(mipmap, total_creation_time)

        try:
            bump_many({
                f"mm_count:{mipmap}": 1,
            })
        except Exception:
            pass

        # Log tile completion when mipmap 0 is done (full tile delivered to X-Plane)
        if mipmap == 0 and not self._completion_reported:
            self._completion_reported = True
            mm0_missing = None
            if self.first_request_time is not None:
                tile_completion_time = time.monotonic() - self.first_request_time
            else:
                tile_completion_time = total_creation_time

            log.debug(f"GET_MIPMAP: Tile {self} COMPLETED in {tile_completion_time:.2f}s "
                     f"(mipmap 0 done, time from first request)")

            _partially_cached = getattr(self, '_dds_populated_mipmaps', None) is not None
            if dynamic_dds_cache is not None and (not self._prepopulated or _partially_cached):
                try:
                    dds_bytes = self.dds.read_at(0, self.dds.total_size)
                    if dds_bytes and len(dds_bytes) >= 128:
                        mm0_missing = None
                        with self._lock:
                            mm0_grid = self.chunks.get(self.max_zoom)
                            mm0_chunks = (
                                mm0_grid.ensure_all()
                                if mm0_grid is not None
                                else []
                            )
                        if mm0_chunks:
                            missing = [i for i, c in enumerate(mm0_chunks)
                                       if not (c.ready.is_set() and c.data)]
                            if missing:
                                mm0_missing = missing
                                log.debug(f"GET_MIPMAP: Progressive store for {self.id} "
                                          f"recording {len(missing)} missing chunks for healing")
                        self._finalize_build(
                            dds_bytes=dds_bytes,
                            missing_indices=mm0_missing,
                        )
                except Exception:
                    pass
            else:
                self._finalize_build(missing_indices=mm0_missing)

        log.debug(f"GET_MIPMAP: Tile {self} mipmap {mipmap} created in {total_creation_time:.2f}s "
                 f"(download+compose: {total_creation_time - compress_time:.2f}s, compress: {compress_time:.2f}s)")

        self._try_incremental_dds_store(mipmap)

        # Chunk cleanup — lock only for dict mutation
        mipmap_zoom = self.max_zoom - mipmap
        chunks_to_close = []
        with self._lock:
            keep_for_healing = (
                mipmap == 0
                and self._dds_needs_healing
            )
            if mipmap_zoom in self.chunks and not keep_for_healing:
                log.debug(f"GET_MIPMAP: Closing chunks for mipmap {mipmap} (zoom {mipmap_zoom}).")
                chunks_to_close = self.chunks.pop(mipmap_zoom)
        if isinstance(chunks_to_close, ChunkGrid):
            chunks_to_close.close()
        else:
            for chunk in chunks_to_close:
                chunk.close()

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
        self._release_composition_images()

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

        with self._mipmap_builds_guard:
            coordinators = list(self._mipmap_builds.values())
            self._mipmap_builds.clear()
        for coordinator in coordinators:
            coordinator.close()

        # 3) Close all chunks
        try:
            for chunks in self.chunks.values():
                if isinstance(chunks, ChunkGrid):
                    chunks.close()
                else:
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
        self._fallback_time_budget = None
        self.first_request_time = None
        self._completion_reported = False
        self._is_live = False
        self._dds_populated_mipmaps = None
        self._dds_persisted = False
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

    Note: on macOS we deliberately skip ``malloc_zone_pressure_relief`` because
    the kernel's memory compressor already reclaims pages aggressively, and
    calling it triggers page decompression/recompression churn that inflates
    ``phys_footprint`` rather than reducing it.
    """
    gc.collect()
    if sys.platform == 'linux':
        try:
            _libc = ctypes.CDLL("libc.so.6")
            _libc.malloc_trim(0)
        except Exception:
            pass
    elif sys.platform == 'win32':
        try:
            ctypes.cdll.msvcrt._heapmin()
        except Exception:
            pass


def _get_process_mem_bytes(process):
    """Best available memory metric: phys_footprint on macOS, RSS elsewhere.

    This is a lightweight call used inside the eviction loop to get the
    current process's true memory pressure without going through the
    full stats/IPC machinery.
    """
    if sys.platform == 'darwin':
        fp = _get_macos_phys_footprint()
        if fp > 0:
            return fp
    if sys.platform == 'linux':
        try:
            rss = 0
            swap = 0
            with open(f"/proc/{process.pid}/status", "rb") as fh:
                for line in fh:
                    if line.startswith(b"VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                    elif line.startswith(b"VmSwap:"):
                        swap = int(line.split()[1]) * 1024
                        break  # VmSwap follows VmRSS in /proc status
            if rss:
                return rss + swap
        except Exception:
            pass
    return process.memory_info().rss


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
        self.custom_map = None
        if self.maptype_override:
            log.info(f"Maptype override set to {self.maptype_override}")
            if self.maptype_override == "Custom Map":
                self.custom_map = get_custom_map_config()
                log.info(f"Custom map loaded with {len(self.custom_map.get_all_cells())} cells")
            elif self.maptype_override == "APPLE":
                apple_token_service.reset_apple_maps_token()
        else:
            log.info(f"Maptype override not set, will use default.")
        log.info(f"Will use Compressor: {CFG.pydds.compressor}")
        self.tc_lock = threading.RLock()
        self._pid = os.getpid()
        # Eviction behavior controls
        self.evict_hysteresis_frac = 0.10  # keep ~10% headroom below limit
        self.evict_headroom_min_bytes = 256 * 1048576  # at least 256MB headroom
        # Track last tile access time for activity-aware proportional eviction.
        # On macOS multi-process, "cold" workers (no recent access) evict
        # more aggressively than "hot" workers with fresh tiles.
        self._last_access_ts = time.monotonic()
        # Bounded ring of recently-evicted tile ids for thrash detection.
        # Maps idx -> eviction monotonic time; capped at 500 entries (OrderedDict FIFO).
        self._recently_evicted: dict = OrderedDict()
        
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

    def _resolve_maptype(self, row, col, map_type, zoom):
        """Resolve the effective maptype, accounting for Custom Map per-cell overrides.

        Never returns the sentinel string ``"Custom Map"`` — if the custom map
        has no entry for a position, the caller's ``map_type`` is returned
        (but only when it is itself a real imagery source).
        """
        if not self.maptype_override or self.maptype_override == "Use tile default":
            return map_type
        if self.maptype_override == "Custom Map":
            if self.custom_map:
                lat, lon = _chunk_to_latlon(row, col, zoom)
                resolved = self.custom_map.get_maptype(lat, lon)
                if resolved:
                    return resolved
            # Fallback: use the caller's maptype only if it is a real source,
            # otherwise default to "BI" to avoid passing "Custom Map" downstream.
            return map_type if map_type != "Custom Map" else "BI"
        return self.maptype_override

    def _to_tile_id(self, row, col, map_type, zoom):
        map_type = self._resolve_maptype(row, col, map_type, zoom)
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
        with self.tc_lock:
            profiled_tiles = list(self.tiles.values())
        profile_gauge("tile_cache.tiles", len(profiled_tiles))
        profile_gauge(
            "tile_cache.open_references",
            sum(max(0, getattr(tile, "refs", 0)) for tile in profiled_tiles),
        )
        if chunk_getter is not None:
            if chunk_getter.initialized:
                depths = chunk_getter.queue_depths()
                profile_gauge("chunk_queue.live_depth", depths["live"])
                profile_gauge(
                    "chunk_queue.prefetch_depth", depths["prefetch"]
                )
                profile_gauge(
                    "chunk_queue.depth",
                    depths["live"] + depths["prefetch"],
                )
        if _dds_buffer_pool is not None:
            try:
                for name, value in _dds_buffer_pool.get_stats().items():
                    profile_gauge(f"dds_buffer_pool.{name}", value)
            except Exception:
                pass
        # Publish activity stats for proportional eviction (macOS multi-process only)
        if self._has_shared_store():
            try:
                pid = os.getpid()
                set_stat(f"tile_count:{pid}", len(self.tiles))
            except Exception:
                pass
        #set_stat('tile_mem_open', len(self.tiles))
        if self.enable_cache:
            #set_stat('tile_mem_miss', self.misses)
            #set_stat('tile_mem_hits', self.hits)
            log.debug(f"TILE CACHE:  MISS: {self.misses}  HIT: {self.hits}")
        log.debug(f"NUM OPEN TILES: {len(self.tiles)}.  TOTAL MEM: {cur_mem//1048576} MB")
        active_now, total_now, peak_threads = native_thread_load_peak_and_reset()
        if peak_threads > 0:
            over = "OVERSUBSCRIBED" if peak_threads > CURRENT_CPU_COUNT else "ok"
            log.debug(f"NATIVE BUILDS: now={active_now} ({total_now} threads), "
                      f"peak_threads_since_last={peak_threads}/{CURRENT_CPU_COUNT} [{over}]")

    # -----------------------------
    # LRU helpers and leader logic
    # -----------------------------
    def _touch_tile(self, idx, tile):
        try:
            # Move to MRU position
            self.tiles.move_to_end(idx, last=True)
            self._last_access_ts = time.monotonic()
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

    def _compute_proportional_target(self, cur_mem, effective_mem, global_target):
        """Compute this worker's proportional share of the global eviction target.

        Workers holding more memory carry a bigger eviction burden.
        Workers with stale tiles (no recent access) carry extra burden.

        Platform behavior:
          - Windows/Linux (single process): returns global_target unchanged.
            No proportional logic runs. Zero overhead.
          - macOS (multi-process): computes proportional share weighted by
            memory and staleness.
        """
        if not self._has_shared_store() or effective_mem <= 0:
            return global_target  # Single process: use global target as-is

        # Base share: proportional to this worker's fraction of total memory
        my_share = cur_mem / max(1, effective_mem)
        my_target = int(global_target * my_share)

        # Staleness penalty: cold workers get a lower target (evict more)
        try:
            now = time.monotonic()
            my_idle_sec = now - self._last_access_ts
            # Workers idle for >30s are considered "cold"
            if my_idle_sec > 30:
                # Reduce target by up to 50% based on idle time (caps at 5 min)
                penalty = min(0.5, my_idle_sec / 600)
                my_target = int(my_target * (1.0 - penalty))
        except Exception:
            pass

        # Floor: never set target below 0
        return max(0, my_target)

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
            now = time.monotonic()
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
                    if (
                        hasattr(t, '_mm0_promotion_is_pinned') and
                        t._mm0_promotion_is_pinned(now)
                    ):
                        bump('partial_mm0_promote_pin_evict_skip')
                        continue
                    # Pop from dict immediately - tile is now "orphaned"
                    try:
                        batch.append((idx, self.tiles.pop(idx)))
                        self._recently_evicted[idx] = now
                        if len(self._recently_evicted) > 500:
                            self._recently_evicted.popitem(last=False)
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

        # Prompt garbage collection after large eviction batches.
        # DDS buffers are multi-MB objects; Python's generational GC may
        # not collect them promptly without a hint after bulk deletion.
        if evicted > 50:
            gc.collect()
        elif evicted > 10:
            gc.collect(generation=1)  # Partial collection — fast

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
        max_tile_count = 3000
        
        # Staleness threshold for global memory stats (seconds)
        # If stats are older than this, they're considered unreliable
        global_stat_max_age_sec = 30

        while True:
            process = psutil.Process(os.getpid())
            cur_mem = _get_process_mem_bytes(process)
            total_evicted = 0
            rss_before_eviction = cur_mem  # Always set so eviction-log block never sees UnboundLocalError

            # Publish this process heartbeat + RSS so the parent can aggregate
            try:
                stat_mem = update_process_memory_stat()
                if stat_mem > 0:
                    cur_mem = stat_mem  # prefer the full-featured metric
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
                # Floor at local cur_mem so we never under-count if the global
                # stat hasn't caught up with this worker's recent growth.
                effective_mem = max(global_mem_bytes, cur_mem)
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
            # Default proportional target = global target (overridden below
            # when eviction is needed and proportional logic kicks in)
            my_target = target_bytes
            
            # Check if we need to evict based on tile count (prevents unbounded tile accumulation)
            tile_count = len(self.tiles)
            need_tile_count_eviction = tile_count > max_tile_count

            # Proportional eviction: each worker independently decides how much
            # to evict based on its memory share and access recency.
            need_mem_eviction = effective_mem > effective_limit
            if need_mem_eviction or need_tile_count_eviction:
                if not self.tiles:
                    time.sleep(poll_interval_normal)
                    continue

                # Compute proportional eviction target.
                # Each worker's share of the global target is weighted by:
                #   (a) its fraction of total memory (bigger = evict more)
                #   (b) staleness penalty (colder = evict more)
                my_target = self._compute_proportional_target(
                    cur_mem, effective_mem, target_bytes
                )

                # Only evict if our LOCAL memory exceeds our proportional target
                if cur_mem <= my_target and not need_tile_count_eviction:
                    time.sleep(poll_interval_fast)
                    continue

                # Self-preservation safety net: if proportional target
                # computation fails for any reason, fall back to evicting
                # locally when cur_mem exceeds the global limit.
                if my_target <= 0 and cur_mem > self.cache_mem_lim:
                    my_target = int(self.cache_mem_lim * 0.9)
                    log.info(
                        f"Self-preservation eviction: local mem "
                        f"{cur_mem // (1024**2)}MB exceeds limit "
                        f"{self.cache_mem_lim // (1024**2)}MB, "
                        f"target set to {my_target // (1024**2)}MB"
                    )

                rss_before_eviction = _get_process_mem_bytes(process)

            # Evict if too many tiles (regardless of memory)
            if need_tile_count_eviction:
                target_tile_count = int(max_tile_count * 0.8)  # Evict down to 80% of max
                tiles_to_evict = tile_count - target_tile_count
                if tiles_to_evict > 0:
                    log.info(f"Tile count eviction: {tile_count} tiles, evicting {tiles_to_evict} to reach {target_tile_count}")
                    # _evict_batch handles its own locking internally
                    evicted = self._evict_batch(tiles_to_evict)
                    total_evicted += evicted

            # Evict while above proportional target using adaptive batch sizing
            while self.tiles and cur_mem > my_target:
                over_bytes = max(0, cur_mem - my_target)
                ratio = min(1.0, over_bytes / max(1, effective_limit))
                adaptive = max(20, int(len(self.tiles) * min(0.10, ratio)))
                # _evict_batch handles its own locking internally
                evicted = self._evict_batch(adaptive)
                total_evicted += evicted
                if evicted == 0:
                    break

                # Recompute local memory and, if available, the aggregated total.
                # Use same staleness-aware logic as main loop.
                cur_mem = _get_process_mem_bytes(process)
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
                    effective_mem = max(global_mem_bytes, cur_mem)
                    effective_limit = self.cache_mem_lim
                else:
                    effective_mem = cur_mem
                    effective_limit = self.cache_mem_lim
                
                # Recalculate global target and proportional target
                headroom = max(int(effective_limit * self.evict_hysteresis_frac), self.evict_headroom_min_bytes)
                target_bytes = max(0, int(effective_limit) - headroom)
                my_target = self._compute_proportional_target(
                    cur_mem, effective_mem, target_bytes
                )

                # Publish heartbeat after an eviction batch
                try:
                    update_process_memory_stat()
                except Exception:
                    pass
                # The parent refreshes the aggregate once per second. Limit
                # each pass to one adaptive batch so the next target is based
                # on fresh post-eviction memory rather than a stale-high total.
                break

            if total_evicted > 0:
                _release_memory_to_os()
                rss_after_release = _get_process_mem_bytes(process)
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
        map_type = self._resolve_maptype(row, col, map_type, zoom)
        idx = self._to_tile_id(row, col, map_type, zoom)

        log.debug(f"Get_tile: {idx}")
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                self.misses += 1
                bump('tile_mem_miss')
                evicted_at = self._recently_evicted.pop(idx, None)
                if evicted_at is not None:
                    bump('evict_reload_after_evict')
                    log.debug(f"Thrash: {idx} reloaded {time.monotonic() - evicted_at:.1f}s after eviction")
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
                tile._fallback_time_budget = None

            tile.refs += 1
        return tile

    
    def _close_tile(self, row, col, map_type, zoom):
        tile_id = self._to_tile_id(row, col, map_type, zoom)
        release_completed_sources = None
        with self.tc_lock:
            t = self.tiles.get(tile_id)
            if not t:
                log.warning(f"Attmpted to close unknown tile {tile_id}!")
                return False

            t.refs -= 1

            if self.enable_cache: # and not t.should_close():
                log.debug(f"Cache enabled.  Delay tile close for {tile_id}")
                if t.refs <= 0:
                    release_completed_sources = t
            elif t.refs <= 0:
                log.debug(f"No more refs for {tile_id} closing...")
                t = self.tiles.pop(tile_id)
                t.close()
                t = None
                del(t)
            else:
                log.debug(f"Still have {t.refs} refs for {tile_id}")

        if release_completed_sources is not None:
            release_completed_sources._release_completed_sources()

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
    2. Predictive DDS (DDS builder, cache)
    3. ChunkGetter (background download threads)
    4. Cache writer executor
    5. Terrain indices
    6. TileCacher instances
    7. Stats batcher
    8. Process memory stats
    """
    global chunk_getter

    try:
        begin_shutdown("module shutdown")
    except Exception as _err:
        log.debug(f"Begin shutdown error: {_err}")

    # 1. Stop spatial prefetcher first (stop new prefetch requests)
    try:
        stop_prefetcher()
        log.debug("Spatial prefetcher stopped")
    except Exception as _err:
        log.debug(f"Prefetcher stop error: {_err}")

    # 2. Stop predictive DDS (DDS builder, cache)
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

    try:
        _close_http2_client()
    except Exception as _err:
        log.debug(f"HTTP/2 broker client stop error: {_err}")

    # 4. Shutdown cache writer executor
    try:
        shutdown_cache_writer()
        log.debug("Cache writer shutdown")
    except Exception as _err:
        log.debug(f"Cache writer shutdown error: {_err}")

    # 4b. Shutdown persistent progressive executor
    global _progressive_executor, _read_ahead_executor
    try:
        if _progressive_executor is not None:
            _progressive_executor.shutdown(wait=False)
            _progressive_executor = None
            log.debug("Progressive executor shutdown")
    except Exception as _err:
        log.debug(f"Progressive executor shutdown error: {_err}")

    try:
        if _read_ahead_executor is not None:
            _read_ahead_executor.shutdown(wait=False)
            _read_ahead_executor = None
            log.debug("Read-ahead executor shutdown")
    except Exception as _err:
        log.debug(f"Read-ahead executor shutdown error: {_err}")

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

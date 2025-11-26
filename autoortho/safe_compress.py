#!/usr/bin/env python3
"""
Safe DDS compression with process isolation.

Runs DDS compression in a subprocess so that if the compression library
crashes (segfault, access violation), the main process survives and
returns a placeholder texture instead.

This ensures AutoOrtho NEVER crashes due to compression failures.
"""

import os
import sys
import logging
import multiprocessing as mp
from multiprocessing import Process, Queue
from queue import Empty
import time

log = logging.getLogger(__name__)

# Timeout for compression operations (seconds)
COMPRESS_TIMEOUT = 30

# Retry configuration
RETRY_COUNT = 2  # Number of retries before returning placeholder
RETRY_DELAY = 0.1  # Seconds between retries

# Placeholder DDS data (4x4 magenta texture - visible error indicator)
# This is a minimal valid BC1 DDS file
_PLACEHOLDER_DDS_HEADER = bytes([
    0x44, 0x44, 0x53, 0x20,  # "DDS "
    0x7C, 0x00, 0x00, 0x00,  # Header size (124)
    0x07, 0x10, 0x08, 0x00,  # Flags
    0x04, 0x00, 0x00, 0x00,  # Height (4)
    0x04, 0x00, 0x00, 0x00,  # Width (4)
    0x08, 0x00, 0x00, 0x00,  # Pitch/LinearSize
    0x00, 0x00, 0x00, 0x00,  # Depth
    0x01, 0x00, 0x00, 0x00,  # MipMapCount
] + [0x00] * 44 + [  # Reserved
    0x20, 0x00, 0x00, 0x00,  # Pixel format size
    0x04, 0x00, 0x00, 0x00,  # Pixel format flags (FOURCC)
    0x44, 0x58, 0x54, 0x31,  # "DXT1"
] + [0x00] * 20 + [  # Rest of pixel format
    0x08, 0x10, 0x40, 0x00,  # Caps
] + [0x00] * 16)  # Caps2, Reserved

# Magenta BC1 block (4x4 pixels)
_PLACEHOLDER_BC1_BLOCK = bytes([
    0x1F, 0xF8,  # Color0 (magenta)
    0x1F, 0xF8,  # Color1 (magenta)
    0x00, 0x00, 0x00, 0x00  # All pixels use color0
])


def get_placeholder_dds(width, height, dxt_format="BC1"):
    """
    Generate a placeholder DDS texture (magenta - visible error).
    
    This is returned when compression fails, ensuring the main process
    never crashes but users can see something went wrong.
    """
    # Calculate number of 4x4 blocks
    blocks_x = (width + 3) // 4
    blocks_y = (height + 3) // 4
    num_blocks = blocks_x * blocks_y
    
    # Block size depends on format
    if dxt_format == "BC3":
        block_size = 16
        # BC3 block: 8 bytes alpha + 8 bytes color
        block = bytes([0xFF] * 8) + _PLACEHOLDER_BC1_BLOCK
    else:  # BC1
        block_size = 8
        block = _PLACEHOLDER_BC1_BLOCK
    
    # Build DDS header
    header = bytearray(_PLACEHOLDER_DDS_HEADER)
    # Update dimensions
    header[12:16] = height.to_bytes(4, 'little')
    header[16:20] = width.to_bytes(4, 'little')
    # Update linear size
    linear_size = num_blocks * block_size
    header[20:24] = linear_size.to_bytes(4, 'little')
    
    # Update format
    if dxt_format == "BC3":
        header[84:88] = b'DXT5'
    
    # Build data
    data = bytes(header) + (block * num_blocks)
    return data


def _compress_worker(queue_in, queue_out):
    """
    Worker process that performs DDS compression.
    
    Runs in a separate process so crashes don't affect the main process.
    """
    # Import compression libraries in the worker
    try:
        from pydds import DDS
        log.debug("Compression worker started")
    except Exception as e:
        log.error(f"Compression worker failed to import pydds: {e}")
        queue_out.put(("error", str(e)))
        return
    
    while True:
        try:
            # Get work item
            item = queue_in.get(timeout=60)
            
            if item is None:  # Shutdown signal
                break
            
            task_id, width, height, data, dxt_format, ispc = item
            
            try:
                # Create DDS object and compress
                dds = DDS(width, height, ispc=ispc, dxt_format=dxt_format)
                result = dds.compress(width, height, data)
                
                if result is None:
                    queue_out.put((task_id, "error", "compress returned None"))
                else:
                    queue_out.put((task_id, "ok", bytes(result)))
                    
            except Exception as e:
                log.error(f"Compression failed: {e}")
                queue_out.put((task_id, "error", str(e)))
                
        except Empty:
            continue
        except Exception as e:
            log.error(f"Worker error: {e}")


class SafeCompressor:
    """
    Safe DDS compressor that isolates compression in subprocess.
    
    If compression crashes, returns a placeholder texture instead
    of crashing the main process.
    """
    
    def __init__(self, num_workers=2):
        self.num_workers = num_workers
        self.workers = []
        self.queue_in = None
        self.queue_out = None
        self.task_counter = 0
        self.started = False
        self._lock = None
        
    def start(self):
        """Start worker processes."""
        if self.started:
            return
            
        try:
            import threading
            self._lock = threading.Lock()
            
            # Use spawn method for clean process isolation
            ctx = mp.get_context('spawn')
            self.queue_in = ctx.Queue()
            self.queue_out = ctx.Queue()
            
            for i in range(self.num_workers):
                p = ctx.Process(
                    target=_compress_worker,
                    args=(self.queue_in, self.queue_out),
                    daemon=True
                )
                p.start()
                self.workers.append(p)
            
            self.started = True
            log.info(f"SafeCompressor started with {self.num_workers} workers")
            
        except Exception as e:
            log.error(f"Failed to start SafeCompressor: {e}")
            self.started = False
    
    def stop(self):
        """Stop worker processes."""
        if not self.started:
            return
            
        # Send shutdown signals
        for _ in self.workers:
            try:
                self.queue_in.put(None)
            except:
                pass
        
        # Wait for workers to finish
        for p in self.workers:
            try:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
            except:
                pass
        
        self.workers = []
        self.started = False
        log.info("SafeCompressor stopped")
    
    def compress(self, width, height, data, dxt_format="BC1", ispc=True,
                 timeout=COMPRESS_TIMEOUT, retries=RETRY_COUNT):
        """
        Compress image data to DDS format safely.
        
        Retries on failure before returning placeholder.
        If all retries fail, returns a placeholder texture.
        NEVER crashes the main process.
        
        Args:
            width: Image width (must be multiple of 4)
            height: Image height (must be multiple of 4)
            data: RGBA pixel data
            dxt_format: "BC1" or "BC3"
            ispc: Use ISPC compressor
            timeout: Timeout in seconds
            retries: Number of retry attempts
            
        Returns:
            Compressed DDS data (or placeholder on failure)
        """
        # Validate inputs
        if not data:
            log.warning("SafeCompressor: No data provided, returning placeholder")
            return get_placeholder_dds(width, height, dxt_format)
        
        if width < 4 or height < 4:
            log.warning(f"SafeCompressor: Invalid dimensions {width}x{height}")
            return get_placeholder_dds(max(4, width), max(4, height), dxt_format)
        
        # If workers not started, try direct compression with fallback
        if not self.started:
            return self._compress_direct_with_fallback(
                width, height, data, dxt_format, ispc
            )
        
        last_error = None
        
        for attempt in range(retries + 1):
            if attempt > 0:
                log.info(f"DDS compression retry {attempt}/{retries} (previous: {last_error})")
                time.sleep(RETRY_DELAY)
            
            # Submit to worker
            with self._lock:
                task_id = self.task_counter
                self.task_counter += 1
            
            try:
                self.queue_in.put((task_id, width, height, data, dxt_format, ispc))
                
                # Wait for result
                start_time = time.time()
                while time.time() - start_time < timeout:
                    try:
                        result = self.queue_out.get(timeout=1)
                        if result[0] == task_id:
                            if result[1] == "ok":
                                return result[2]
                            else:
                                last_error = result[2]
                                log.info(f"DDS compress attempt {attempt+1} failed: {last_error}")
                                break  # Try retry
                    except Empty:
                        continue
                else:
                    # Timeout
                    last_error = "timeout"
                    log.info(f"DDS compress attempt {attempt+1} timed out")
                    
            except Exception as e:
                last_error = str(e)
                log.info(f"DDS compress attempt {attempt+1} exception: {e}")
        
        # All retries exhausted
        log.warning(f"DDS compression FAILED after {retries+1} attempts: {last_error}")
        return get_placeholder_dds(width, height, dxt_format)
    
    def _compress_direct_with_fallback(self, width, height, data, 
                                        dxt_format, ispc):
        """
        Try direct compression with fallback to placeholder.
        
        Used when worker processes aren't available.
        """
        try:
            from pydds import DDS
            dds = DDS(width, height, ispc=ispc, dxt_format=dxt_format)
            result = dds.compress(width, height, data)
            if result:
                return bytes(result)
        except Exception as e:
            log.error(f"Direct compression failed: {e}")
        
        return get_placeholder_dds(width, height, dxt_format)


# Global instance (lazy initialized)
_safe_compressor = None


def get_safe_compressor():
    """Get the global SafeCompressor instance."""
    global _safe_compressor
    if _safe_compressor is None:
        _safe_compressor = SafeCompressor(num_workers=2)
        _safe_compressor.start()
    return _safe_compressor


def safe_compress(width, height, data, dxt_format="BC1", ispc=True):
    """
    Safely compress image data to DDS format.
    
    This is the main entry point. NEVER crashes - returns placeholder on failure.
    """
    compressor = get_safe_compressor()
    return compressor.compress(width, height, data, dxt_format, ispc)


def shutdown_safe_compressor():
    """Shutdown the global compressor (call on app exit)."""
    global _safe_compressor
    if _safe_compressor:
        _safe_compressor.stop()
        _safe_compressor = None


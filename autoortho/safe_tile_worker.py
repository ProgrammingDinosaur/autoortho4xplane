#!/usr/bin/env python3
"""
Safe Tile Worker - Complete process isolation for tile building.

This module runs ALL C library operations (aoimage, libjpeg-turbo, ispc_texcomp)
in isolated worker processes. If any C code crashes, the worker dies but the
main AutoOrtho process survives.

Architecture:
    Main Process (AutoOrtho)
         │
         ├── TileWorkerPool (manages workers)
         │       │
         │       ├── Worker 1 (subprocess) ─── aoimage, libjpeg-turbo, ispc
         │       ├── Worker 2 (subprocess) ─── aoimage, libjpeg-turbo, ispc
         │       └── Worker N (subprocess) ─── aoimage, libjpeg-turbo, ispc
         │
         └── On worker crash: restart worker, return placeholder

Key guarantee: Main process NEVER crashes due to C library failures.
"""

import os
import sys
import logging
import multiprocessing as mp
from multiprocessing import Process, Queue
from queue import Empty
import time
import traceback

log = logging.getLogger(__name__)

# Configuration
WORKER_COUNT = max(2, (os.cpu_count() or 4) // 2)  # Half of CPU cores
TASK_TIMEOUT = 45  # seconds
WORKER_RESTART_DELAY = 0.5  # seconds before restarting crashed worker
RETRY_COUNT = 2  # Number of retries before returning placeholder
RETRY_DELAY = 0.1  # Seconds between retries

# Placeholder magenta color (RGB) - visible error indicator
PLACEHOLDER_COLOR = (255, 0, 255)


def _create_placeholder_rgba(width, height, color=PLACEHOLDER_COLOR):
    """Create a placeholder RGBA buffer (magenta)."""
    r, g, b = color
    pixel = bytes([r, g, b, 255])
    return pixel * (width * height)


def _worker_main(task_queue, result_queue, worker_id):
    """
    Worker process main loop.
    
    Handles all C library operations in isolation. If this process crashes,
    the main process is unaffected.
    """
    # Import C libraries inside worker (isolation)
    try:
        from aoimage import AoImage
        from pydds import DDS
        log.debug(f"Worker {worker_id}: Libraries loaded successfully")
    except Exception as e:
        log.error(f"Worker {worker_id}: Failed to load libraries: {e}")
        result_queue.put(("init_error", worker_id, str(e)))
        return

    while True:
        try:
            # Get task with timeout (allows clean shutdown)
            try:
                task = task_queue.get(timeout=30)
            except Empty:
                continue
            
            if task is None:  # Shutdown signal
                log.debug(f"Worker {worker_id}: Received shutdown signal")
                break
            
            task_id, task_type, task_data = task
            
            try:
                if task_type == "load_jpeg":
                    # Load JPEG from memory
                    jpeg_data = task_data
                    img = AoImage.load_from_memory(jpeg_data)
                    if img is None:
                        result_queue.put((task_id, "error", "Failed to decode JPEG"))
                    else:
                        # Return image dimensions and raw RGBA data
                        width, height = img.size
                        rgba_data = img.tobytes()
                        result_queue.put((task_id, "ok", (width, height, rgba_data)))
                        
                elif task_type == "build_tile":
                    # Build complete tile from chunks
                    tile_width, tile_height, chunks_data = task_data
                    # chunks_data: list of (jpeg_bytes, paste_x, paste_y, chunk_w, chunk_h)
                    
                    # Create base image
                    base_img = AoImage.new('RGBA', (tile_width, tile_height), (128, 128, 128))
                    if base_img is None:
                        result_queue.put((task_id, "error", "Failed to create base image"))
                        continue
                    
                    # Paste each chunk
                    for chunk_jpeg, paste_x, paste_y, chunk_w, chunk_h in chunks_data:
                        chunk_img = AoImage.load_from_memory(chunk_jpeg)
                        if chunk_img:
                            base_img.paste(chunk_img, paste_x, paste_y)
                    
                    # Return assembled tile
                    width, height = base_img.size
                    rgba_data = base_img.tobytes()
                    result_queue.put((task_id, "ok", (width, height, rgba_data)))
                    
                elif task_type == "compress_dds":
                    # Compress RGBA to DDS
                    width, height, rgba_data, dxt_format, ispc = task_data
                    
                    dds = DDS(width, height, ispc=ispc, dxt_format=dxt_format)
                    result = dds.compress(width, height, rgba_data)
                    
                    if result is None:
                        result_queue.put((task_id, "error", "DDS compression failed"))
                    else:
                        result_queue.put((task_id, "ok", bytes(result)))
                        
                elif task_type == "full_pipeline":
                    # Complete pipeline: JPEG decode -> assemble -> DDS compress
                    tile_width, tile_height, chunks_data, dxt_format, ispc, mipmap_start, mipmap_max = task_data
                    
                    # Create base image
                    base_img = AoImage.new('RGBA', (tile_width, tile_height), (128, 128, 128))
                    if base_img is None:
                        result_queue.put((task_id, "error", "Failed to create base image"))
                        continue
                    
                    # Paste chunks
                    for chunk_jpeg, paste_x, paste_y in chunks_data:
                        chunk_img = AoImage.load_from_memory(chunk_jpeg)
                        if chunk_img:
                            base_img.paste(chunk_img, paste_x, paste_y)
                    
                    # Create DDS and generate mipmaps
                    dds = DDS(tile_width, tile_height, ispc=ispc, dxt_format=dxt_format)
                    dds.gen_mipmaps(base_img, mipmap_start, mipmap_max)
                    
                    # Return DDS data
                    result_queue.put((task_id, "ok", dds))
                    
                else:
                    result_queue.put((task_id, "error", f"Unknown task type: {task_type}"))
                    
            except Exception as e:
                log.error(f"Worker {worker_id}: Task {task_id} failed: {e}")
                result_queue.put((task_id, "error", str(e)))
                
        except Exception as e:
            log.error(f"Worker {worker_id}: Loop error: {e}")
            # Continue running - don't let one error kill the worker


class TileWorkerPool:
    """
    Pool of worker processes for safe tile building.
    
    Manages worker lifecycle, restarts crashed workers, and provides
    task submission with timeout and fallback.
    """
    
    def __init__(self, num_workers=WORKER_COUNT):
        self.num_workers = num_workers
        self.workers = []
        self.task_queue = None
        self.result_queue = None
        self.task_counter = 0
        self.pending_tasks = {}  # task_id -> (start_time, task_type)
        self.started = False
        self._lock = None
        
    def start(self):
        """Start worker processes."""
        if self.started:
            return True
            
        try:
            import threading
            self._lock = threading.Lock()
            
            # Use spawn for clean isolation (especially important on Windows)
            ctx = mp.get_context('spawn')
            self.task_queue = ctx.Queue()
            self.result_queue = ctx.Queue()
            
            for i in range(self.num_workers):
                p = ctx.Process(
                    target=_worker_main,
                    args=(self.task_queue, self.result_queue, i),
                    daemon=True
                )
                p.start()
                self.workers.append(p)
            
            self.started = True
            log.info(f"TileWorkerPool started with {self.num_workers} workers")
            return True
            
        except Exception as e:
            log.error(f"Failed to start TileWorkerPool: {e}")
            self.started = False
            return False
    
    def stop(self):
        """Stop all worker processes."""
        if not self.started:
            return
            
        # Send shutdown signals
        for _ in self.workers:
            try:
                self.task_queue.put(None)
            except:
                pass
        
        # Wait for workers
        for p in self.workers:
            try:
                p.join(timeout=3)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=1)
            except:
                pass
        
        self.workers = []
        self.started = False
        log.info("TileWorkerPool stopped")
    
    def _check_workers(self):
        """Check for and restart crashed workers."""
        for i, p in enumerate(self.workers):
            if not p.is_alive():
                log.warning(f"Worker {i} crashed, restarting...")
                try:
                    ctx = mp.get_context('spawn')
                    new_worker = ctx.Process(
                        target=_worker_main,
                        args=(self.task_queue, self.result_queue, i),
                        daemon=True
                    )
                    new_worker.start()
                    self.workers[i] = new_worker
                    log.info(f"Worker {i} restarted successfully")
                except Exception as e:
                    log.error(f"Failed to restart worker {i}: {e}")
    
    def submit(self, task_type, task_data, timeout=TASK_TIMEOUT):
        """
        Submit a task to the worker pool.
        
        Returns result or None on failure. NEVER raises exceptions.
        """
        if not self.started:
            if not self.start():
                return None
        
        with self._lock:
            task_id = self.task_counter
            self.task_counter += 1
        
        try:
            self.task_queue.put((task_id, task_type, task_data))
            self.pending_tasks[task_id] = (time.time(), task_type)
            
            # Wait for result
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    result = self.result_queue.get(timeout=1)
                    
                    result_id = result[0]
                    if result_id == "init_error":
                        # Worker init failed
                        log.error(f"Worker init error: {result[2]}")
                        continue
                        
                    if result_id == task_id:
                        del self.pending_tasks[task_id]
                        if result[1] == "ok":
                            return result[2]
                        else:
                            log.warning(f"Task failed: {result[2]}")
                            return None
                    else:
                        # Result for different task (out of order), put back
                        # This shouldn't happen often with proper task tracking
                        pass
                        
                except Empty:
                    # Check for crashed workers
                    self._check_workers()
                    continue
            
            # Timeout
            log.warning(f"Task {task_id} ({task_type}) timed out after {timeout}s")
            del self.pending_tasks[task_id]
            return None
            
        except Exception as e:
            log.error(f"Submit error: {e}")
            return None
    
    def load_jpeg_safe(self, jpeg_data, placeholder_size=(256, 256), retries=RETRY_COUNT):
        """
        Load JPEG safely with retries, returns (width, height, rgba_data) or placeholder.
        
        Retries on failure before returning placeholder.
        NEVER crashes, NEVER returns None.
        """
        last_error = None
        
        for attempt in range(retries + 1):
            if attempt > 0:
                log.debug(f"JPEG load retry {attempt}/{retries}")
                time.sleep(RETRY_DELAY)
                self._check_workers()
            
            result = self.submit("load_jpeg", jpeg_data)
            if result:
                return result
            
            last_error = "submit failed"
        
        # All retries exhausted
        log.warning(f"JPEG load failed after {retries+1} attempts")
        w, h = placeholder_size
        return (w, h, _create_placeholder_rgba(w, h))
    
    def compress_dds_safe(self, width, height, rgba_data, dxt_format="BC1", ispc=True,
                          retries=RETRY_COUNT):
        """
        Compress to DDS safely with retries, returns DDS data or placeholder.
        
        Retries on failure before returning placeholder.
        NEVER crashes, NEVER returns None.
        """
        from safe_compress import get_placeholder_dds
        
        last_error = None
        
        for attempt in range(retries + 1):
            if attempt > 0:
                log.debug(f"DDS compress retry {attempt}/{retries}")
                time.sleep(RETRY_DELAY)
                self._check_workers()
            
            result = self.submit("compress_dds", (width, height, rgba_data, dxt_format, ispc))
            if result:
                return result
            
            last_error = "submit failed"
        
        # All retries exhausted
        log.warning(f"DDS compress failed after {retries+1} attempts")
        return get_placeholder_dds(width, height, dxt_format)


# Global pool instance (lazy initialized)
_worker_pool = None
_pool_lock = None


def get_worker_pool():
    """Get the global worker pool instance."""
    global _worker_pool, _pool_lock
    
    if _pool_lock is None:
        import threading
        _pool_lock = threading.Lock()
    
    with _pool_lock:
        if _worker_pool is None:
            _worker_pool = TileWorkerPool()
            _worker_pool.start()
        return _worker_pool


def shutdown_worker_pool():
    """Shutdown the global worker pool."""
    global _worker_pool
    if _worker_pool:
        _worker_pool.stop()
        _worker_pool = None


# Convenience functions
def safe_load_jpeg(jpeg_data, placeholder_size=(256, 256)):
    """
    Safely load JPEG data.
    
    Returns (width, height, rgba_bytes). NEVER crashes, NEVER returns None.
    """
    pool = get_worker_pool()
    return pool.load_jpeg_safe(jpeg_data, placeholder_size)


def safe_compress_dds(width, height, rgba_data, dxt_format="BC1", ispc=True):
    """
    Safely compress RGBA to DDS.
    
    Returns DDS bytes. NEVER crashes, NEVER returns None.
    """
    pool = get_worker_pool()
    return pool.compress_dds_safe(width, height, rgba_data, dxt_format, ispc)


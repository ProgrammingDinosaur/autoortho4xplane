"""
AoDDS.py - Python wrapper for native DDS texture building

Provides high-performance DDS generation that bypasses Python's GIL
by delegating the entire build pipeline to native C code:
- Cache reading + JPEG decoding + image composition
- Mipmap generation + DXT compression
- All in a single native call

Usage:
    from autoortho.aopipeline import AoDDS
    
    # Build a complete DDS tile from cached JPEGs
    dds_bytes = AoDDS.build_tile_native(
        cache_dir="/path/to/cache",
        row=1234, col=5678,
        maptype="BI", zoom=16,
        chunks_per_side=16,
        format="BC1"
    )
"""

from ctypes import (
    CDLL, POINTER, Structure, c_void_p,
    c_char, c_char_p, c_int32, c_uint8, c_uint32, c_double, c_size_t,
    byref, cast
)
import logging
import os
import sys
import threading
import time
from typing import Optional, Tuple, NamedTuple, Union, List

import numpy as np

# Import AoDecode for decode pool management
from . import AoDecode

log = logging.getLogger(__name__)

# Format constants (match dds_format_t in aodds.h)
FORMAT_BC1 = 0  # DXT1
FORMAT_BC3 = 1  # DXT5


# ============================================================================
# Buffer Pool Priority Constants
# ============================================================================
# Lower number = higher priority (served first)
# Live tiles are "premium clients" and always go to the front of the queue
# Prefetch tiles are low priority and only processed when system is idle

PRIORITY_LIVE = 0       # Live tiles requested by X-Plane (premium, front of queue)
PRIORITY_PREFETCH = 100 # Prefetch/pre-built tiles (low priority, back of queue)


# ============================================================================
# Buffer Pool for Zero-Copy DDS Building
# ============================================================================

class DDSBufferPool:
    """
    Thread-safe pool of reusable numpy buffers for DDS building with priority queue.
    
    Eliminates per-call allocation overhead (~15ms) and avoids copying
    data back to Python (~65ms) by reusing pre-allocated numpy arrays.
    
    Priority Queue ("Bank Queue") System:
    - Live tiles (PRIORITY_LIVE=0): Premium clients, served first
    - Prefetch tiles (PRIORITY_PREFETCH=100): Low priority, served when idle
    
    When all buffers are in use, waiters are queued by priority. When a buffer
    is released, the highest-priority (lowest number) waiter is served first.
    This ensures live tiles are never blocked by prefetch work.
    
    Usage:
        pool = DDSBufferPool(max_dds_size=11_200_000)  # ~10.7 MB for 4096x4096
        
        # Acquire buffer with priority
        buffer, buffer_id = pool.acquire(priority=PRIORITY_LIVE)
        try:
            result = build_tile_to_buffer(buffer, ...)
            dds_bytes = bytes(buffer[:result.bytes_written])
        finally:
            pool.release(buffer_id)
    
    For even better performance, use the buffer directly without copying:
        memoryview(buffer[:result.bytes_written])
    """
    
    # Standard DDS sizes for common tile configurations
    # These include DDS header (128 bytes) + all mipmaps + safety margin
    # Values calculated from aodds_calc_dds_size() + 1KB padding for safety
    SIZE_8192x8192_BC1 = 44_740_000  # 32x32 chunks, BC1 format with mipmaps (~42.67 MB)
    SIZE_4096x4096_BC1 = 11_186_000  # 16x16 chunks, BC1 format with mipmaps (~10.67 MB)
    SIZE_2048x2048_BC1 = 2_797_500   # 8x8 chunks, BC1 format with mipmaps (~2.67 MB)
    SIZE_1024x1024_BC1 = 700_000     # 4x4 chunks, BC1 format with mipmaps (~0.67 MB)
    
    def __init__(self, buffer_size: int = SIZE_4096x4096_BC1, pool_size: int = 4):
        """
        Create a buffer pool.
        
        Args:
            buffer_size: Size of each buffer in bytes (default: 4096x4096 BC1)
            pool_size: Number of buffers to pre-allocate
        """
        import heapq
        
        self._buffer_size = buffer_size
        self._pool_size = pool_size
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        
        # Pre-allocate numpy arrays
        self._buffers = [
            np.zeros(buffer_size, dtype=np.uint8)
            for _ in range(pool_size)
        ]
        self._available = list(range(pool_size))
        self._in_use = set()
        
        # Priority queue for waiters: (priority, sequence_number, event)
        # sequence_number breaks ties (FIFO within same priority)
        self._waiters = []  # heap queue
        self._waiter_sequence = 0
        
        # Stats for monitoring
        self._live_acquires = 0
        self._prefetch_acquires = 0
        self._live_waits = 0
        self._prefetch_waits = 0
        
        log.debug(f"DDSBufferPool: created {pool_size} buffers of {buffer_size:,} bytes each (priority queue enabled)")
    
    def acquire(self, timeout: float = 5.0, priority: int = PRIORITY_LIVE) -> Tuple[np.ndarray, int]:
        """
        Acquire a buffer from the pool with priority queuing.
        
        Args:
            timeout: Maximum time to wait for a buffer (seconds)
            priority: Queue priority (lower = higher priority)
                      PRIORITY_LIVE (0) = front of queue
                      PRIORITY_PREFETCH (100) = back of queue
        
        Returns:
            Tuple of (buffer, buffer_id)
            
        Raises:
            TimeoutError: If no buffer available within timeout
        """
        import time
        import heapq
        
        start = time.monotonic()
        
        with self._condition:
            # Fast path: buffer available and no higher-priority waiters
            if self._available and not self._waiters:
                buffer_id = self._available.pop()
                self._in_use.add(buffer_id)
                if priority == PRIORITY_LIVE:
                    self._live_acquires += 1
                else:
                    self._prefetch_acquires += 1
                return self._buffers[buffer_id], buffer_id
            
            # Check if there are only lower-priority waiters (we can skip ahead)
            if self._available and self._waiters:
                # Peek at highest-priority waiter
                top_priority = self._waiters[0][0]
                if priority < top_priority:
                    # We have higher priority, take buffer immediately
                    buffer_id = self._available.pop()
                    self._in_use.add(buffer_id)
                    if priority == PRIORITY_LIVE:
                        self._live_acquires += 1
                    else:
                        self._prefetch_acquires += 1
                    return self._buffers[buffer_id], buffer_id
            
            # Slow path: need to wait in queue
            my_event = threading.Event()
            my_sequence = self._waiter_sequence
            self._waiter_sequence += 1
            
            # Add to priority queue: (priority, sequence, event, buffer_id_holder)
            # buffer_id_holder is a mutable list so we can receive the assigned buffer
            buffer_id_holder = [None]
            waiter_entry = (priority, my_sequence, my_event, buffer_id_holder)
            heapq.heappush(self._waiters, waiter_entry)
            
            if priority == PRIORITY_LIVE:
                self._live_waits += 1
            else:
                self._prefetch_waits += 1
        
        # Wait outside lock for our turn
        remaining = timeout - (time.monotonic() - start)
        if remaining > 0:
            got_buffer = my_event.wait(timeout=remaining)
        else:
            got_buffer = my_event.is_set()
        
        if got_buffer and buffer_id_holder[0] is not None:
            return self._buffers[buffer_id_holder[0]], buffer_id_holder[0]
        
        # Timeout - remove ourselves from queue if still there
        with self._condition:
            try:
                self._waiters = [w for w in self._waiters if w[1] != my_sequence]
                heapq.heapify(self._waiters)
            except Exception as e:
                log.debug(f"DDSBufferPool: waiter cleanup exception (harmless): {e}")
        
        raise TimeoutError(
            f"DDSBufferPool: no buffer available after {timeout}s "
            f"({len(self._in_use)}/{self._pool_size} in use, "
            f"priority={priority}, waiters={len(self._waiters)})"
        )
    
    def release(self, buffer_id: int) -> None:
        """
        Release a buffer back to the pool.
        
        If there are waiters, the highest-priority waiter gets the buffer.
        
        Args:
            buffer_id: The buffer ID returned from acquire()
        """
        import heapq
        
        with self._condition:
            if buffer_id not in self._in_use:
                log.warning(f"DDSBufferPool: releasing buffer {buffer_id} that wasn't in use")
                return
            
            self._in_use.remove(buffer_id)
            
            # Check for waiters - give buffer to highest priority (lowest number)
            while self._waiters:
                priority, sequence, event, buffer_id_holder = heapq.heappop(self._waiters)
                
                # Check if waiter is still waiting (not timed out)
                if not event.is_set():
                    # Assign buffer to this waiter
                    buffer_id_holder[0] = buffer_id
                    self._in_use.add(buffer_id)
                    event.set()
                    return
            
            # No waiters - return buffer to available pool
            self._available.append(buffer_id)
    
    def try_acquire(self, priority: int = PRIORITY_LIVE) -> Optional[Tuple[np.ndarray, int]]:
        """
        Try to acquire a buffer without blocking.
        
        Only succeeds if a buffer is immediately available AND there are no
        higher-priority waiters ahead.
        
        Args:
            priority: Queue priority (for checking against waiters)
        
        Returns:
            Tuple of (buffer, buffer_id) if available, None otherwise
        """
        with self._lock:
            if self._available:
                # Check if there are higher-priority waiters
                if self._waiters:
                    top_priority = self._waiters[0][0]
                    if priority >= top_priority:
                        # Can't skip ahead of equal or higher priority waiters
                        return None
                
                buffer_id = self._available.pop()
                self._in_use.add(buffer_id)
                if priority == PRIORITY_LIVE:
                    self._live_acquires += 1
                else:
                    self._prefetch_acquires += 1
                return self._buffers[buffer_id], buffer_id
            return None
    
    @property
    def buffer_size(self) -> int:
        """Size of each buffer in bytes."""
        return self._buffer_size
    
    @property
    def pool_size(self) -> int:
        """Total number of buffers in pool."""
        return self._pool_size
    
    @property
    def available_count(self) -> int:
        """Number of buffers currently available."""
        with self._lock:
            return len(self._available)
    
    @property
    def in_use_count(self) -> int:
        """Number of buffers currently in use."""
        with self._lock:
            return len(self._in_use)
    
    @property
    def waiter_count(self) -> int:
        """Number of waiters in queue."""
        with self._lock:
            return len(self._waiters)
    
    def get_stats(self) -> dict:
        """Get pool statistics for monitoring."""
        with self._lock:
            return {
                'pool_size': self._pool_size,
                'available': len(self._available),
                'in_use': len(self._in_use),
                'waiters': len(self._waiters),
                'live_acquires': self._live_acquires,
                'prefetch_acquires': self._prefetch_acquires,
                'live_waits': self._live_waits,
                'prefetch_waits': self._prefetch_waits,
            }
    
    def get_ctypes_ptr(self, buffer: np.ndarray) -> POINTER(c_uint8):
        """
        Get a ctypes pointer to a numpy buffer for passing to C.
        
        Args:
            buffer: A numpy array from this pool
            
        Returns:
            ctypes pointer suitable for passing to native functions
        """
        return buffer.ctypes.data_as(POINTER(c_uint8))


# Global default buffer pool (lazily initialized)
_default_pool: Optional[DDSBufferPool] = None
_default_pool_lock = threading.Lock()


def get_default_pool() -> DDSBufferPool:
    """
    Get or create the default global buffer pool.
    
    Returns:
        The default DDSBufferPool instance
    """
    global _default_pool
    if _default_pool is None:
        with _default_pool_lock:
            if _default_pool is None:
                _default_pool = DDSBufferPool()
    return _default_pool

# ============================================================================
# Streaming Builder Structures
# ============================================================================

class BuilderConfig(Structure):
    """
    Builder configuration structure.
    Maps to aodds_builder_config_t in C.
    """
    _fields_ = [
        ('chunks_per_side', c_int32),
        ('format', c_int32),
        ('missing_r', c_uint8),
        ('missing_g', c_uint8),
        ('missing_b', c_uint8),
        ('nocopy_mode', c_uint8),  # 1 = zero-copy mode (Python owns JPEG memory)
    ]


class BuilderStatus(Structure):
    """
    Builder status structure for monitoring progress.
    Maps to aodds_builder_status_t in C.
    """
    _fields_ = [
        ('chunks_total', c_int32),
        ('chunks_received', c_int32),
        ('chunks_decoded', c_int32),
        ('chunks_failed', c_int32),
        ('chunks_fallback', c_int32),
        ('chunks_missing', c_int32),
    ]


class StreamingBuilderResult(NamedTuple):
    """Result from StreamingBuilder.finalize()."""
    success: bool
    bytes_written: int
    error: str = ""


class StreamingBuilder:
    """
    Wrapper around native aodds_builder_t.
    
    Manages incremental chunk feeding and finalization for streaming DDS builds.
    Chunks can be added as they become available (JPEG data, fallback images,
    or marked as missing), and the final DDS is generated when finalize() is called.
    
    Usage:
        # Create builder via pool
        builder = builder_pool.acquire(config)
        
        # Add chunks as they arrive
        for idx, jpeg_bytes in chunks:
            builder.add_chunk(idx, jpeg_bytes)
        
        # Add fallback images for missing chunks
        for idx, rgba_data in fallbacks:
            builder.add_fallback_image(idx, rgba_data)
        
        # Mark remaining chunks as missing
        for idx in missing_indices:
            builder.mark_missing(idx)
        
        # Finalize and get DDS
        buffer = pool.acquire_buffer()
        result = builder.finalize(buffer)
        
        # Return builder to pool
        builder.release()
    """
    
    def __init__(self, handle: c_void_p, pool_ref: 'StreamingBuilderPool', 
                 lib, config: BuilderConfig):
        self._handle = handle
        self._pool_ref = pool_ref
        self._lib = lib
        self._config = config
        self._finalized = False
        self._released = False
        self._jpeg_refs = []  # Keep JPEG references alive for zero-copy mode
    
    def add_chunk(self, index: int, jpeg_data: bytes) -> bool:
        """
        Add JPEG data for a chunk.
        
        The JPEG data is stored for deferred parallel decode at finalize time.
        This enables faster throughput by decoding all chunks in parallel with OpenMP.
        
        Args:
            index: Chunk index in row-major order (0 to chunks_per_side^2 - 1)
            jpeg_data: JPEG bytes
            
        Returns:
            True if chunk was stored, False if failed or chunk already set
            
        Thread Safety: Thread-safe - multiple threads can add chunks.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        if not jpeg_data:
            return False
        
        jpeg_ptr = cast(jpeg_data, POINTER(c_uint8))
        result = self._lib.aodds_builder_add_chunk(
            self._handle,
            c_int32(index),
            jpeg_ptr,
            c_uint32(len(jpeg_data))
        )
        return bool(result)
    
    def add_chunks_batch(self, chunks: List[Tuple[int, bytes]]) -> int:
        """
        Add multiple JPEG chunks in a single native call.
        
        More efficient than calling add_chunk() repeatedly due to reduced
        Python/C crossing overhead. Chunks are stored for deferred parallel
        decode at finalize time.
        
        Args:
            chunks: List of (index, jpeg_bytes) tuples
            
        Returns:
            Number of chunks successfully added
            
        Thread Safety: Thread-safe.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        if not chunks:
            return 0
        
        count = len(chunks)
        
        # Build arrays for C call
        indices = (c_int32 * count)()
        jpeg_ptrs = (c_void_p * count)()
        jpeg_sizes = (c_uint32 * count)()
        
        # Keep references to bytes objects to prevent GC during call
        # (No copy needed - cast directly to pointer, bytes are immutable
        # and CPython doesn't move objects in memory)
        jpeg_refs = []
        
        for i, (idx, jpeg_data) in enumerate(chunks):
            indices[i] = idx
            jpeg_sizes[i] = len(jpeg_data) if jpeg_data else 0
            
            if jpeg_data:
                # Keep reference alive and cast directly - NO COPY
                jpeg_refs.append(jpeg_data)
                jpeg_ptrs[i] = cast(jpeg_data, c_void_p).value
            else:
                jpeg_ptrs[i] = None
        
        result = self._lib.aodds_builder_add_chunks_batch(
            self._handle,
            c_int32(count),
            indices,
            cast(jpeg_ptrs, POINTER(c_void_p)),
            jpeg_sizes
        )
        
        return result
    
    def add_chunks_batch_nocopy(self, chunks: list, jpeg_refs_out: list = None) -> int:
        """
        Add multiple JPEG chunks in a single call using ZERO-COPY mode.
        
        ZERO-COPY: C stores pointers directly without copying. The caller MUST
        ensure that the JPEG bytes objects remain alive until finalize() completes.
        
        This is faster than add_chunks_batch when Python already holds references
        that outlive the builder (e.g., chunk.data held by Tile objects).
        
        Args:
            chunks: List of (index, jpeg_bytes) tuples
            jpeg_refs_out: Optional list to append JPEG byte references to
                           (for caller to maintain references until finalize)
            
        Returns:
            Number of chunks successfully added
            
        Thread Safety: Thread-safe.
        
        WARNING: Memory safety is caller's responsibility! If any bytes object
        is garbage collected before finalize(), C will read freed memory.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        if not chunks:
            return 0
        
        count = len(chunks)
        
        # Build arrays for C call
        indices = (c_int32 * count)()
        jpeg_ptrs = (c_void_p * count)()
        jpeg_sizes = (c_uint32 * count)()
        
        # CRITICAL: Keep references alive! Either caller provides list,
        # or we store internally
        refs = jpeg_refs_out if jpeg_refs_out is not None else []
        
        for i, (idx, jpeg_data) in enumerate(chunks):
            indices[i] = idx
            jpeg_sizes[i] = len(jpeg_data) if jpeg_data else 0
            
            if jpeg_data:
                # Keep reference and cast directly to pointer
                refs.append(jpeg_data)
                jpeg_ptrs[i] = cast(jpeg_data, c_void_p).value
            else:
                jpeg_ptrs[i] = None
        
        # Call the nocopy version - C stores pointers without copying
        result = self._lib.aodds_builder_add_chunks_batch_nocopy(
            self._handle,
            c_int32(count),
            indices,
            cast(jpeg_ptrs, POINTER(c_void_p)),
            jpeg_sizes
        )
        
        # If no external list provided, store internally to keep alive
        if jpeg_refs_out is None and refs:
            if not hasattr(self, '_jpeg_refs'):
                self._jpeg_refs = []
            self._jpeg_refs.extend(refs)
        
        return result
    
    def add_fallback_image(self, index: int, rgba_data: bytes,
                           width: int = 256, height: int = 256) -> bool:
        """
        Add a pre-decoded fallback image for a chunk.
        
        Used when Python has resolved a fallback (disk cache, mipmap scale, network).
        
        Args:
            index: Chunk index in row-major order
            rgba_data: RGBA pixel data (width * height * 4 bytes)
            width: Image width (must be 256)
            height: Image height (must be 256)
            
        Returns:
            True on success, False on failure
            
        Thread Safety: Thread-safe.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        if not rgba_data or len(rgba_data) != width * height * 4:
            return False
        
        rgba_ptr = cast(rgba_data, POINTER(c_uint8))
        result = self._lib.aodds_builder_add_fallback_image(
            self._handle,
            c_int32(index),
            rgba_ptr,
            c_int32(width),
            c_int32(height)
        )
        return bool(result)
    
    def mark_missing(self, index: int) -> None:
        """
        Mark a chunk as permanently missing.
        
        Call this when all fallbacks have been exhausted. The chunk position
        will be filled with missing_color during finalization.
        
        Args:
            index: Chunk index in row-major order
            
        Thread Safety: Thread-safe.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        self._lib.aodds_builder_mark_missing(self._handle, c_int32(index))
    
    def get_status(self) -> dict:
        """
        Get current builder status.
        
        Returns:
            Dictionary with status fields:
            - chunks_total: Total chunks expected
            - chunks_received: Chunks added or marked
            - chunks_decoded: Successfully decoded JPEGs
            - chunks_failed: JPEG decode failures
            - chunks_fallback: Chunks using fallback images
            - chunks_missing: Chunks marked as missing
            
        Thread Safety: Thread-safe.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        status = BuilderStatus()
        self._lib.aodds_builder_get_status(self._handle, byref(status))
        return {
            'chunks_total': status.chunks_total,
            'chunks_received': status.chunks_received,
            'chunks_decoded': status.chunks_decoded,
            'chunks_failed': status.chunks_failed,
            'chunks_fallback': status.chunks_fallback,
            'chunks_missing': status.chunks_missing,
        }
    
    def is_complete(self) -> bool:
        """
        Check if all chunks have been processed.
        
        Returns:
            True if all chunks are added/marked, False otherwise
            
        Thread Safety: Thread-safe.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        
        return bool(self._lib.aodds_builder_is_complete(self._handle))
    
    def finalize(self, buffer: np.ndarray) -> StreamingBuilderResult:
        """
        Finalize and write DDS to buffer.
        
        Performs:
        1. Fill missing chunks with missing_color
        2. Compose all chunks into tile
        3. Generate all mipmap levels
        4. Compress with BC1/BC3
        5. Write complete DDS to buffer
        
        Args:
            buffer: Pre-allocated numpy buffer (use DDSBufferPool)
            
        Returns:
            StreamingBuilderResult with success status and bytes_written
            
        Thread Safety: NOT thread-safe with add_chunk. Ensure all adding complete.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        if self._finalized:
            raise RuntimeError("StreamingBuilder already finalized")
        
        bytes_written = c_uint32()
        buffer_ptr = buffer.ctypes.data_as(POINTER(c_uint8))
        
        result = self._lib.aodds_builder_finalize(
            self._handle,
            buffer_ptr,
            c_uint32(len(buffer)),
            byref(bytes_written)
        )
        
        self._finalized = True
        
        if result:
            return StreamingBuilderResult(
                success=True,
                bytes_written=bytes_written.value
            )
        else:
            return StreamingBuilderResult(
                success=False,
                bytes_written=0,
                error="Failed to finalize DDS"
            )
    
    def finalize_to_file(self, output_path: str) -> Tuple[bool, int]:
        """
        Finalize and write DDS directly to file.
        
        Zero-copy optimization for prefetch cache.
        
        Args:
            output_path: Path to output DDS file
            
        Returns:
            Tuple of (success, bytes_written)
            
        Thread Safety: NOT thread-safe with add_chunk.
        """
        if self._released:
            raise RuntimeError("StreamingBuilder has been released")
        if self._finalized:
            raise RuntimeError("StreamingBuilder already finalized")
        
        bytes_written = c_uint32()
        output_path_bytes = output_path.encode('utf-8')
        
        result = self._lib.aodds_builder_finalize_to_file(
            self._handle,
            output_path_bytes,
            byref(bytes_written)
        )
        
        self._finalized = True
        
        return bool(result), bytes_written.value
    
    def release(self) -> None:
        """
        Return builder to pool for reuse.
        
        Must be called when done with the builder (use try/finally).
        """
        if not self._released and self._pool_ref is not None:
            self._pool_ref._return_builder(self._handle)
            self._released = True
            self._handle = None
            # Clear JPEG refs now that finalize has completed
            self._jpeg_refs.clear()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def __del__(self):
        """Safety net - release builder if not explicitly released."""
        try:
            if not self._released and self._handle is not None:
                log.warning("StreamingBuilder.__del__: Builder was not released - "
                           "possible resource leak. Use try/finally or context manager.")
                self.release()
        except Exception:
            pass  # Don't raise in __del__


class StreamingBuilderPool:
    """
    Pool of reusable StreamingBuilder instances.
    
    Reduces allocation overhead for high-frequency tile builds by reusing
    native builder instances.
    
    Usage:
        pool = StreamingBuilderPool(pool_size=4)
        
        # Acquire builder
        builder = pool.acquire(config={'chunks_per_side': 16, 'format': 'BC1'})
        
        try:
            # Use builder...
            pass
        finally:
            builder.release()
        
        # Or use context manager
        with pool.acquire(config) as builder:
            # Use builder...
            pass
    """
    
    def __init__(self, pool_size: int = 4, decode_pool: c_void_p = None):
        """
        Create a streaming builder pool.
        
        Args:
            pool_size: Number of builders to pre-allocate
            decode_pool: Optional native decode buffer pool (aodecode_pool_t*)
        """
        self._pool_size = pool_size
        self._decode_pool = decode_pool
        self._lock = threading.Lock()
        self._available: list = []
        self._in_use: set = set()  # Track handles currently in use
        self._total_created = 0  # Total builders ever created
        self._lib = None
        self._initialized = False
        
        log.debug(f"StreamingBuilderPool: created with pool_size={pool_size}")
    
    def _ensure_initialized(self):
        """Lazy initialization of native library and function signatures."""
        if self._initialized:
            return
        
        with self._lock:
            if self._initialized:
                return
            
            self._lib = _load_library()
            
            # Setup builder function signatures
            self._lib.aodds_builder_create.argtypes = [
                POINTER(BuilderConfig), c_void_p
            ]
            self._lib.aodds_builder_create.restype = c_void_p
            
            self._lib.aodds_builder_reset.argtypes = [
                c_void_p, POINTER(BuilderConfig)
            ]
            self._lib.aodds_builder_reset.restype = None
            
            self._lib.aodds_builder_destroy.argtypes = [c_void_p]
            self._lib.aodds_builder_destroy.restype = None
            
            self._lib.aodds_builder_add_chunk.argtypes = [
                c_void_p, c_int32, POINTER(c_uint8), c_uint32
            ]
            self._lib.aodds_builder_add_chunk.restype = c_int32
            
            # Batch API for adding multiple chunks in one call
            self._lib.aodds_builder_add_chunks_batch.argtypes = [
                c_void_p,           # builder
                c_int32,            # count
                POINTER(c_int32),   # indices array
                POINTER(c_void_p),  # jpeg_data array (array of pointers)
                POINTER(c_uint32),  # jpeg_sizes array
            ]
            self._lib.aodds_builder_add_chunks_batch.restype = c_int32
            
            # Zero-copy batch API - C stores pointers only, caller owns memory
            self._lib.aodds_builder_add_chunks_batch_nocopy.argtypes = [
                c_void_p,           # builder
                c_int32,            # count
                POINTER(c_int32),   # indices array
                POINTER(c_void_p),  # jpeg_data array (array of pointers)
                POINTER(c_uint32),  # jpeg_sizes array
            ]
            self._lib.aodds_builder_add_chunks_batch_nocopy.restype = c_int32
            
            self._lib.aodds_builder_add_fallback_image.argtypes = [
                c_void_p, c_int32, POINTER(c_uint8), c_int32, c_int32
            ]
            self._lib.aodds_builder_add_fallback_image.restype = c_int32
            
            self._lib.aodds_builder_mark_missing.argtypes = [c_void_p, c_int32]
            self._lib.aodds_builder_mark_missing.restype = None
            
            self._lib.aodds_builder_get_status.argtypes = [
                c_void_p, POINTER(BuilderStatus)
            ]
            self._lib.aodds_builder_get_status.restype = None
            
            self._lib.aodds_builder_is_complete.argtypes = [c_void_p]
            self._lib.aodds_builder_is_complete.restype = c_int32
            
            self._lib.aodds_builder_finalize.argtypes = [
                c_void_p, POINTER(c_uint8), c_uint32, POINTER(c_uint32)
            ]
            self._lib.aodds_builder_finalize.restype = c_int32
            
            self._lib.aodds_builder_finalize_to_file.argtypes = [
                c_void_p, c_char_p, POINTER(c_uint32)
            ]
            self._lib.aodds_builder_finalize_to_file.restype = c_int32
            
            self._initialized = True
    
    def acquire(self, config: dict, timeout: float = 5.0) -> Optional[StreamingBuilder]:
        """
        Acquire a builder from the pool.
        
        Args:
            config: Builder configuration dict:
                - chunks_per_side: int (typically 16)
                - format: str ("BC1" or "BC3")
                - missing_color: tuple of (r, g, b) bytes
            timeout: Maximum wait time in seconds
            
        Returns:
            StreamingBuilder instance, or None if pool exhausted
        """
        self._ensure_initialized()
        
        # Build config structure
        fmt = FORMAT_BC1 if config.get('format', 'BC1').upper() in ('BC1', 'DXT1') else FORMAT_BC3
        missing_color = config.get('missing_color', (128, 128, 128))
        nocopy_mode = 1 if config.get('nocopy_mode', False) else 0
        
        c_config = BuilderConfig(
            chunks_per_side=config.get('chunks_per_side', 16),
            format=fmt,
            missing_r=missing_color[0],
            missing_g=missing_color[1],
            missing_b=missing_color[2],
            nocopy_mode=nocopy_mode,
        )
        
        import time
        start = time.monotonic()
        
        while True:
            with self._lock:
                if self._available:
                    handle = self._available.pop()
                    self._in_use.add(id(handle))  # Track in-use handle
                    # Reset builder with new config
                    self._lib.aodds_builder_reset(handle, byref(c_config))
                    try:
                        return StreamingBuilder(handle, self, self._lib, c_config)
                    except Exception:
                        # Constructor failed - return handle to available pool
                        self._in_use.discard(id(handle))
                        self._available.append(handle)
                        raise
            
            # Pool exhausted - try to create new if under limit
            with self._lock:
                # Fix: Use actual count of created builders, not buggy calculation
                current_count = self._total_created
                if current_count < self._pool_size * 2:
                    # Create new builder
                    handle = self._lib.aodds_builder_create(
                        byref(c_config), self._decode_pool
                    )
                    if handle:
                        self._total_created += 1
                        self._in_use.add(id(handle))  # Track in-use handle
                        try:
                            return StreamingBuilder(handle, self, self._lib, c_config)
                        except Exception:
                            # Constructor failed - destroy the handle
                            self._in_use.discard(id(handle))
                            self._total_created -= 1
                            self._lib.aodds_builder_destroy(handle)
                            raise
            
            if time.monotonic() - start > timeout:
                log.warning(f"StreamingBuilderPool: timeout waiting for builder")
                return None
            
            time.sleep(0.001)
    
    def _return_builder(self, handle: c_void_p) -> None:
        """Return a builder handle to the pool."""
        with self._lock:
            self._in_use.discard(id(handle))
            self._available.append(handle)
    
    def close(self) -> None:
        """Destroy all builders in the pool."""
        with self._lock:
            # Destroy available builders
            for handle in self._available:
                try:
                    self._lib.aodds_builder_destroy(handle)
                except Exception:
                    pass
            self._available.clear()
            
            # Note: in-use builders will be cleaned up when released
            # Log warning if any builders are still in use
            if self._in_use:
                log.warning(f"StreamingBuilderPool.close(): {len(self._in_use)} builders "
                           f"still in use - they will leak if not released")
            
            self._total_created = 0
    
    @property
    def available_count(self) -> int:
        """Number of builders currently available."""
        with self._lock:
            return len(self._available)


# ============================================================================
# Global Decode Pool (for JPEG decoding with memory limits)
# ============================================================================
#
# Memory Management Strategy:
# 1. FIXED POOL: Pre-allocated buffers for optimal performance (no malloc per decode)
#    - Size = 2 * cpu_count (allows concurrent decode across all OpenMP threads)
# 2. DYNAMIC OVERFLOW: If fixed pool busy, malloc new buffers IF within memory limit
#    - Memory limit = 25% of cache_mem_limit (decode buffers are transient)
# 3. WAIT QUEUE: If limit reached, block until buffer is released
# 4. AUTO-SHRINK: Overflow buffers freed immediately on release (returns to fixed pool size)
#
# This ensures:
# - No memory leaks from unbounded buffer allocation
# - Performance under normal load (fixed pool is fast, no malloc)
# - Graceful handling of burst load (overflow + wait-queue)
# - Automatic memory reclamation (auto-shrink)

_default_decode_pool: Optional[AoDecode.BufferPool] = None
_default_decode_pool_lock = threading.Lock()


def _calculate_decode_pool_size() -> int:
    """
    Calculate optimal fixed pool size for JPEG decode buffers.
    
    Uses 2 * cpu_count to allow concurrent decoding across all OpenMP threads
    while having headroom for burst requests.
    
    Returns:
        Optimal fixed pool size (minimum 8, maximum 256)
    """
    import os
    cpu_count = os.cpu_count() or 4
    # 2x CPU count for headroom during burst loads
    pool_size = cpu_count * 2
    # Clamp to valid range
    pool_size = max(8, min(256, pool_size))
    log.debug(f"Decode pool size: {pool_size} (based on cpu_count={cpu_count})")
    return pool_size


def _calculate_decode_memory_limit() -> int:
    """
    Calculate memory limit for decode pool based on cache_mem_limit.
    
    Decode buffers are transient (used only during DDS build), so we allocate
    25% of the total cache_mem_limit for them. This is conservative since
    most buffers are recycled quickly.
    
    Each 256x256 RGBA buffer = 256KB, so 1GB limit = ~4000 potential buffers.
    
    Returns:
        Memory limit in bytes (0 if config unavailable)
    """
    try:
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG
        
        # cache_mem_limit is in GB
        cache_limit_gb = float(getattr(CFG.autoortho, 'cache_mem_lim', 4))
        # Use 25% for decode buffers (they're transient and recycle fast)
        decode_limit_gb = cache_limit_gb * 0.25
        decode_limit_bytes = int(decode_limit_gb * 1024 * 1024 * 1024)
        
        log.debug(f"Decode pool memory limit: {decode_limit_bytes // (1024*1024)} MB "
                 f"(25% of cache_mem_limit={cache_limit_gb} GB)")
        return decode_limit_bytes
    except Exception as e:
        log.warning(f"Failed to get cache_mem_limit for decode pool: {e}, using 512MB default")
        return 512 * 1024 * 1024  # 512 MB default


def get_default_decode_pool() -> Optional[AoDecode.BufferPool]:
    """
    Get or create the default global decode buffer pool.
    
    The decode pool is used by all native JPEG decoding operations to:
    - Avoid per-decode malloc calls (performance)
    - Enforce memory limits (resource control)
    - Provide wait-queue for burst load handling (stability)
    
    Pool Configuration:
    - Fixed pool size: 2 * cpu_count
    - Memory limit: 25% of cache_mem_limit
    - Auto-shrink: Overflow buffers freed on release
    
    Returns:
        BufferPool instance, or None if native library unavailable
    """
    global _default_decode_pool
    
    if _default_decode_pool is None:
        with _default_decode_pool_lock:
            if _default_decode_pool is None:
                try:
                    pool_size = _calculate_decode_pool_size()
                    memory_limit = _calculate_decode_memory_limit()
                    
                    _default_decode_pool = AoDecode.BufferPool(
                        count=pool_size,
                        memory_limit=memory_limit
                    )
                    
                    log.info(f"Global decode pool initialized: "
                            f"{pool_size} fixed buffers, "
                            f"{memory_limit // (1024*1024)} MB limit")
                except Exception as e:
                    log.warning(f"Failed to create decode pool: {e} - decoding will use malloc fallback")
                    return None
    
    return _default_decode_pool


def get_decode_pool_stats() -> Optional[dict]:
    """
    Get statistics from the global decode pool.
    
    Returns:
        Dict with pool stats, or None if pool not initialized
    """
    pool = get_default_decode_pool()
    if pool:
        return pool.stats_ex()
    return None


def shutdown_decode_pool():
    """
    Shutdown the global decode pool and free all resources.
    
    Should be called during application shutdown.
    """
    global _default_decode_pool
    
    with _default_decode_pool_lock:
        if _default_decode_pool is not None:
            try:
                stats = _default_decode_pool.stats_ex()
                if stats['acquired'] > 0 or stats['overflow_count'] > 0:
                    log.warning(f"Shutting down decode pool with active buffers: "
                               f"acquired={stats['acquired']}, overflow={stats['overflow_count']}")
                _default_decode_pool.destroy()
                log.info("Global decode pool destroyed")
            except Exception as e:
                log.warning(f"Error destroying decode pool: {e}")
            finally:
                _default_decode_pool = None


# Global streaming builder pool (lazily initialized)
_default_builder_pool: Optional[StreamingBuilderPool] = None
_default_builder_pool_lock = threading.Lock()


def _calculate_builder_pool_size() -> int:
    """
    Calculate the streaming builder pool size from config.
    
    Pool size = prefetch_workers + live_concurrency
    
    This ensures:
    - All prefetch workers can build tiles simultaneously
    - Live X-Plane requests have dedicated builders and won't be starved
    
    Returns:
        Calculated pool size (minimum 2, maximum 64)
    """
    prefetch_workers = 2  # Default
    live_concurrency = 4  # Default
    
    try:
        # Try to import config - may fail during early initialization
        try:
            from autoortho.aoconfig import CFG
        except ImportError:
            from aoconfig import CFG
        
        prefetch_workers = int(getattr(CFG.autoortho, 'background_builder_workers', 2))
        live_concurrency = int(getattr(CFG.autoortho, 'live_builder_concurrency', 4))
    except Exception:
        # Config not available yet - use defaults
        pass
    
    # Calculate pool size and clamp to valid range
    pool_size = prefetch_workers + live_concurrency
    pool_size = max(2, min(64, pool_size))
    
    log.debug(f"Builder pool size: {pool_size} (prefetch={prefetch_workers} + live={live_concurrency})")
    return pool_size


def get_default_builder_pool() -> StreamingBuilderPool:
    """
    Get or create the default global streaming builder pool.
    
    Pool size is automatically calculated from config:
        pool_size = background_builder_workers + live_builder_concurrency
    
    The pool is initialized with the global decode pool for memory-managed
    JPEG decoding.
    
    This ensures prefetch workers and live requests each have dedicated
    builders and won't starve each other.
    
    Returns:
        The default StreamingBuilderPool instance
    """
    global _default_builder_pool
    if _default_builder_pool is None:
        with _default_builder_pool_lock:
            if _default_builder_pool is None:
                pool_size = _calculate_builder_pool_size()
                
                # Get or create the global decode pool for memory-limited JPEG decoding
                decode_pool = get_default_decode_pool()
                decode_pool_handle = decode_pool.handle if decode_pool else None
                
                _default_builder_pool = StreamingBuilderPool(
                    pool_size=pool_size,
                    decode_pool=decode_pool_handle
                )
                log.info(f"Streaming builder pool initialized: {pool_size} builders, "
                        f"decode_pool={'enabled' if decode_pool_handle else 'disabled'}")
    return _default_builder_pool


def shutdown_builder_pool():
    """
    Shutdown the global streaming builder pool.
    
    Should be called during application shutdown.
    """
    global _default_builder_pool
    
    with _default_builder_pool_lock:
        if _default_builder_pool is not None:
            try:
                _default_builder_pool.close()
                log.info("Global builder pool destroyed")
            except Exception as e:
                log.warning(f"Error destroying builder pool: {e}")
            finally:
                _default_builder_pool = None


def shutdown_all_pools():
    """
    Shutdown all global pools (decode pool and builder pool).
    
    Should be called during application shutdown. Shuts down in correct order:
    1. Builder pool first (stops using decode pool)
    2. Decode pool last (frees decode buffers)
    """
    log.info("Shutting down all native pools...")
    shutdown_builder_pool()
    shutdown_decode_pool()
    log.info("All native pools shut down")


def get_all_pool_stats() -> dict:
    """
    Get combined statistics from all global pools.
    
    Returns dict with:
        - decode_pool: dict with decode pool stats or None
        - builder_pool: dict with builder pool info or None
    """
    stats = {
        'decode_pool': None,
        'builder_pool': None,
    }
    
    # Get decode pool stats
    decode_stats = get_decode_pool_stats()
    if decode_stats:
        stats['decode_pool'] = decode_stats
    
    # Get builder pool info
    with _default_builder_pool_lock:
        if _default_builder_pool is not None:
            stats['builder_pool'] = {
                'pool_size': _default_builder_pool._pool_size,
                'available': _default_builder_pool.available_count,
                'in_use': len(_default_builder_pool._in_use),
                'total_created': _default_builder_pool._total_created,
            }
    
    return stats


# ============================================================================
# Library Loading
# ============================================================================

_aodds = None
_load_error = None


def _get_lib_path() -> str:
    """Get the path to the native library for the current platform."""
    if sys.platform == 'darwin':
        lib_subdir = 'macos'
        lib_name = 'libaopipeline.dylib'
    elif sys.platform == 'win32':
        lib_subdir = 'windows'
        lib_name = 'aopipeline.dll'
    else:
        lib_subdir = 'linux'
        lib_name = 'libaopipeline.so'
    
    # Check if running as PyInstaller frozen executable
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # PyInstaller: library is in _MEIPASS/autoortho/aopipeline/lib/<platform>/
        lib_path = os.path.join(sys._MEIPASS, 'autoortho', 'aopipeline', 'lib', lib_subdir, lib_name)
        if os.path.exists(lib_path):
            return lib_path
        # Fallback: check without autoortho prefix
        lib_path = os.path.join(sys._MEIPASS, 'aopipeline', 'lib', lib_subdir, lib_name)
        if os.path.exists(lib_path):
            return lib_path
    
    # Development mode: library is relative to this file
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Try platform-specific lib directory first
    lib_path = os.path.join(base_dir, 'lib', lib_subdir, lib_name)
    if os.path.exists(lib_path):
        return lib_path
    
    # Fall back to same directory (for development/testing)
    alt_path = os.path.join(base_dir, lib_name)
    if os.path.exists(alt_path):
        return alt_path
    
    raise FileNotFoundError(
        f"Native library not found. Expected at: {lib_path} or {alt_path}"
    )


def _load_library():
    """Load the native library and set up function signatures."""
    global _aodds, _load_error
    
    if _aodds is not None:
        return _aodds
    
    if _load_error is not None:
        raise _load_error
    
    try:
        lib_path = _get_lib_path()
        log.debug(f"Loading aodds native library from: {lib_path}")
        
        # Windows: Add DLL directory to search path (Python 3.8+)
        if sys.platform == 'win32':
            lib_dir = os.path.dirname(lib_path)
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(lib_dir)
        
        _aodds = CDLL(lib_path)
        
        # Configure function signatures
        _setup_signatures(_aodds)
        
        # Initialize ISPC
        _aodds.aodds_init_ispc()
        
        # Log version info
        version = _aodds.aodds_version()
        log.info(f"Loaded native DDS library: {version.decode()}")
        
        return _aodds
        
    except OSError as e:
        error_str = str(e)
        
        # Provide platform-specific help
        if sys.platform == 'linux' and 'libgomp' in error_str:
            _load_error = ImportError(
                f"OpenMP runtime library not found.\n"
                f"Install with:\n"
                f"  Ubuntu/Debian: sudo apt install libgomp1\n"
                f"  Fedora/RHEL:   sudo dnf install libgomp\n"
                f"  Arch Linux:    sudo pacman -S gcc-libs\n"
                f"Original error: {e}"
            )
        elif sys.platform == 'win32':
            _load_error = ImportError(
                f"Failed to load native library. Ensure all DLLs are present:\n"
                f"  - aopipeline.dll\n"
                f"  - libgomp-1.dll\n"
                f"  - libturbojpeg.dll\n"
                f"  - libgcc_s_seh-1.dll\n"
                f"  - libwinpthread-1.dll\n"
                f"Original error: {e}"
            )
        else:
            _load_error = ImportError(f"Failed to load aodds native library: {e}")
        
        log.warning(f"Native DDS library not available: {e}")
        raise _load_error
        
    except Exception as e:
        _load_error = ImportError(f"Failed to load aodds native library: {e}")
        log.warning(f"Native DDS library not available: {e}")
        raise _load_error


def _setup_signatures(lib):
    """Configure ctypes function signatures for type safety."""
    
    # aodds_build_tile
    lib.aodds_build_tile.argtypes = [POINTER(DDSTileRequest), c_void_p]
    lib.aodds_build_tile.restype = c_int32
    
    # aodds_calc_dds_size - returns size_t for large buffer safety
    lib.aodds_calc_dds_size.argtypes = [c_int32, c_int32, c_int32, c_int32]
    lib.aodds_calc_dds_size.restype = c_size_t
    
    # aodds_calc_mipmap_count
    lib.aodds_calc_mipmap_count.argtypes = [c_int32, c_int32]
    lib.aodds_calc_mipmap_count.restype = c_int32
    
    # aodds_write_header
    lib.aodds_write_header.argtypes = [
        POINTER(c_uint8), c_int32, c_int32, c_int32, c_int32
    ]
    lib.aodds_write_header.restype = c_int32
    
    # aodds_init_ispc
    lib.aodds_init_ispc.argtypes = []
    lib.aodds_init_ispc.restype = c_int32
    
    # aodds_set_use_ispc
    lib.aodds_set_use_ispc.argtypes = [c_int32]
    lib.aodds_set_use_ispc.restype = None
    
    # aodds_get_use_ispc
    lib.aodds_get_use_ispc.argtypes = []
    lib.aodds_get_use_ispc.restype = c_int32
    
    # aodds_using_fallback_compressor - check if using lower-quality fallback
    lib.aodds_using_fallback_compressor.argtypes = []
    lib.aodds_using_fallback_compressor.restype = c_int32
    
    # aodds_version
    lib.aodds_version.argtypes = []
    lib.aodds_version.restype = c_char_p
    
    # Decoder pool configuration
    lib.aodds_init_decoder_pool.argtypes = [c_int32]
    lib.aodds_init_decoder_pool.restype = c_int32
    
    lib.aodds_get_decoder_pool_size.argtypes = []
    lib.aodds_get_decoder_pool_size.restype = c_int32
    
    lib.aodds_get_decoder_pool_stats.argtypes = [
        POINTER(c_int32), POINTER(c_int32), POINTER(c_int32)
    ]
    lib.aodds_get_decoder_pool_stats.restype = None


# ============================================================================
# Data Structures
# ============================================================================

class DDSStats(Structure):
    """Statistics from DDS building."""
    _fields_ = [
        ('chunks_found', c_int32),
        ('chunks_decoded', c_int32),
        ('chunks_failed', c_int32),
        ('mipmaps_generated', c_int32),
        ('elapsed_ms', c_double),
    ]


class DDSTileRequest(Structure):
    """
    Tile build request structure.
    Maps to dds_tile_request_t in C.
    """
    _fields_ = [
        ('cache_dir', c_char_p),
        ('tile_row', c_int32),
        ('tile_col', c_int32),
        ('maptype', c_char_p),
        ('zoom', c_int32),
        ('chunks_per_side', c_int32),
        ('format', c_int32),
        ('missing_r', c_uint8),
        ('missing_g', c_uint8),
        ('missing_b', c_uint8),
        ('dds_buffer', POINTER(c_uint8)),
        ('dds_buffer_size', c_uint32),
        ('dds_written', c_uint32),
        ('stats', DDSStats),
        ('success', c_int32),
        ('error', c_char * 256),
    ]


class BuildResult(NamedTuple):
    """Result from DDS tile building."""
    data: bytes
    success: bool
    chunks_found: int
    chunks_decoded: int
    chunks_failed: int
    mipmaps: int
    elapsed_ms: float
    error: str = ''


class BufferBuildResult(NamedTuple):
    """
    Result from zero-copy DDS tile building.
    
    Unlike BuildResult, this doesn't copy data - it returns a view into
    the provided buffer. The caller must ensure the buffer remains valid
    while using the data.
    
    Use `get_view()` for zero-copy access, or `to_bytes()` if you need
    to copy the data (e.g., to store it after releasing the buffer).
    """
    buffer: np.ndarray      # The buffer containing DDS data
    bytes_written: int      # Number of valid bytes in buffer
    success: bool
    chunks_found: int
    chunks_decoded: int
    chunks_failed: int
    mipmaps: int
    elapsed_ms: float
    error: str = ''
    
    def get_view(self) -> memoryview:
        """
        Get a zero-copy view of the DDS data.
        
        This is the fastest way to access the data but the view becomes
        invalid when the buffer is released back to the pool.
        """
        return memoryview(self.buffer[:self.bytes_written])
    
    def to_bytes(self) -> bytes:
        """
        Copy the DDS data to a new bytes object.
        
        Use this if you need to keep the data after releasing the buffer.
        This incurs a copy (~65ms for 10MB) but the data is then independent.
        """
        return bytes(self.buffer[:self.bytes_written])


# ============================================================================
# Public API
# ============================================================================

def build_tile_native(
    cache_dir: str,
    row: int,
    col: int,
    maptype: str,
    zoom: int,
    chunks_per_side: int = 16,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    pool: Optional[c_void_p] = None
) -> bytes:
    """
    Build a complete DDS tile from cached JPEGs using native code.
    
    This is the main entry point for native DDS building. It performs
    the entire pipeline in native code without Python GIL involvement:
    1. Batch read all chunk cache files
    2. Parallel decode all JPEGs
    3. Compose chunks into full tile image
    4. Generate all mipmap levels
    5. Compress each mipmap with ISPC texcomp
    6. Return complete DDS file as bytes
    
    Args:
        cache_dir: Directory containing cached JPEGs
        row: Tile row coordinate
        col: Tile column coordinate
        maptype: Map source identifier (e.g., "BI", "EOX")
        zoom: Zoom level for chunk fetching
        chunks_per_side: Number of chunks per side (default 16)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        pool: Optional buffer pool handle from AoDecode
    
    Returns:
        Complete DDS file as bytes
    
    Raises:
        RuntimeError: If DDS build fails
        ImportError: If native library not available
    """
    lib = _load_library()
    
    # Calculate buffer size
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    dds_size = lib.aodds_calc_dds_size(tile_size, tile_size, 0, fmt)
    
    # Allocate output buffer
    buffer = (c_uint8 * dds_size)()
    
    # Build request
    request = DDSTileRequest()
    request.cache_dir = cache_dir.encode('utf-8')
    request.tile_row = row
    request.tile_col = col
    request.maptype = maptype.encode('utf-8')
    request.zoom = zoom
    request.chunks_per_side = chunks_per_side
    request.format = fmt
    request.missing_r = missing_color[0]
    request.missing_g = missing_color[1]
    request.missing_b = missing_color[2]
    request.dds_buffer = cast(buffer, POINTER(c_uint8))
    request.dds_buffer_size = dds_size
    
    # Call native function
    pool_handle = pool if pool else None
    if not lib.aodds_build_tile(byref(request), pool_handle):
        error = request.error.decode('utf-8', errors='replace').rstrip('\x00')
        raise RuntimeError(f"DDS build failed: {error}")
    
    # Return DDS bytes
    return bytes(buffer[:request.dds_written])


def build_tile_native_detailed(
    cache_dir: str,
    row: int,
    col: int,
    maptype: str,
    zoom: int,
    chunks_per_side: int = 16,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    pool: Optional[c_void_p] = None
) -> BuildResult:
    """
    Build a DDS tile with detailed result information.
    
    Same as build_tile_native but returns a BuildResult with statistics.
    """
    lib = _load_library()
    
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    dds_size = lib.aodds_calc_dds_size(tile_size, tile_size, 0, fmt)
    
    buffer = (c_uint8 * dds_size)()
    
    request = DDSTileRequest()
    request.cache_dir = cache_dir.encode('utf-8')
    request.tile_row = row
    request.tile_col = col
    request.maptype = maptype.encode('utf-8')
    request.zoom = zoom
    request.chunks_per_side = chunks_per_side
    request.format = fmt
    request.missing_r = missing_color[0]
    request.missing_g = missing_color[1]
    request.missing_b = missing_color[2]
    request.dds_buffer = cast(buffer, POINTER(c_uint8))
    request.dds_buffer_size = dds_size
    
    pool_handle = pool if pool else None
    success = lib.aodds_build_tile(byref(request), pool_handle)
    
    return BuildResult(
        data=bytes(buffer[:request.dds_written]) if success else b'',
        success=bool(success),
        chunks_found=request.stats.chunks_found,
        chunks_decoded=request.stats.chunks_decoded,
        chunks_failed=request.stats.chunks_failed,
        mipmaps=request.stats.mipmaps_generated,
        elapsed_ms=request.stats.elapsed_ms,
        error=request.error.decode('utf-8', errors='replace').rstrip('\x00') if not success else ''
    )


def build_tile_to_buffer(
    buffer: np.ndarray,
    cache_dir: str,
    row: int,
    col: int,
    maptype: str,
    zoom: int,
    chunks_per_side: int = 16,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    decode_pool: Optional[c_void_p] = None
) -> BufferBuildResult:
    """
    Build a DDS tile directly into a pre-allocated numpy buffer (ZERO-COPY).
    
    This is the highest-performance path for DDS building:
    - No allocation overhead (buffer already exists)
    - No copy overhead (data stays in provided buffer)
    - Use with DDSBufferPool for optimal performance
    
    Args:
        buffer: Pre-allocated numpy array (must be large enough for DDS output).
                Use DDSBufferPool.SIZE_4096x4096_BC1 for sizing.
        cache_dir: Directory containing cached JPEGs
        row: Tile row coordinate
        col: Tile column coordinate
        maptype: Map source identifier (e.g., "BI", "EOX")
        zoom: Zoom level for chunk fetching
        chunks_per_side: Number of chunks per side (default 16)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        decode_pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        BufferBuildResult with success status and metadata.
        Use result.get_view() for zero-copy access to DDS data.
    
    Example:
        pool = DDSBufferPool()
        buffer, buffer_id = pool.acquire()
        try:
            result = build_tile_to_buffer(
                buffer, cache_dir, row, col, maptype, zoom
            )
            if result.success:
                # Zero-copy access to DDS data
                dds_view = result.get_view()
                # ... use dds_view ...
        finally:
            pool.release(buffer_id)
    """
    lib = _load_library()
    
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    required_size = lib.aodds_calc_dds_size(tile_size, tile_size, 0, fmt)
    
    if len(buffer) < required_size:
        return BufferBuildResult(
            buffer=buffer,
            bytes_written=0,
            success=False,
            chunks_found=0,
            chunks_decoded=0,
            chunks_failed=0,
            mipmaps=0,
            elapsed_ms=0.0,
            error=f"Buffer too small: {len(buffer)} < {required_size} bytes needed"
        )
    
    request = DDSTileRequest()
    request.cache_dir = cache_dir.encode('utf-8')
    request.tile_row = row
    request.tile_col = col
    request.maptype = maptype.encode('utf-8')
    request.zoom = zoom
    request.chunks_per_side = chunks_per_side
    request.format = fmt
    request.missing_r = missing_color[0]
    request.missing_g = missing_color[1]
    request.missing_b = missing_color[2]
    request.dds_buffer = buffer.ctypes.data_as(POINTER(c_uint8))
    request.dds_buffer_size = len(buffer)
    
    pool_handle = decode_pool if decode_pool else None
    success = lib.aodds_build_tile(byref(request), pool_handle)
    
    return BufferBuildResult(
        buffer=buffer,
        bytes_written=request.dds_written if success else 0,
        success=bool(success),
        chunks_found=request.stats.chunks_found,
        chunks_decoded=request.stats.chunks_decoded,
        chunks_failed=request.stats.chunks_failed,
        mipmaps=request.stats.mipmaps_generated,
        elapsed_ms=request.stats.elapsed_ms,
        error=request.error.decode('utf-8', errors='replace').rstrip('\x00') if not success else ''
    )


def calc_dds_size(
    width: int,
    height: int,
    mipmap_count: int = 0,
    format: str = "BC1"
) -> int:
    """
    Calculate required DDS buffer size.
    
    Args:
        width: Image width in pixels
        height: Image height in pixels
        mipmap_count: Number of mipmaps (0 = auto-calculate)
        format: "BC1" or "BC3"
    
    Returns:
        Required buffer size in bytes
    """
    lib = _load_library()
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    return lib.aodds_calc_dds_size(width, height, mipmap_count, fmt)


def calc_mipmap_count(width: int, height: int) -> int:
    """
    Calculate number of mipmap levels for given dimensions.
    
    Args:
        width: Image width
        height: Image height
    
    Returns:
        Number of mipmap levels
    """
    lib = _load_library()
    return lib.aodds_calc_mipmap_count(width, height)


def init_ispc() -> bool:
    """
    Initialize the ISPC compression library.
    
    Returns:
        True if ISPC available, False if using fallback compression
    """
    lib = _load_library()
    return bool(lib.aodds_init_ispc())


def set_use_ispc(use_ispc: bool) -> None:
    """
    Set whether to use ISPC compression or fallback (STB).
    
    This allows respecting user configuration for compressor preference.
    When use_ispc=False, the fallback compressor is used even if ISPC is available.
    
    Args:
        use_ispc: True to use ISPC (if available), False to force STB fallback
    """
    lib = _load_library()
    lib.aodds_set_use_ispc(1 if use_ispc else 0)


def get_use_ispc() -> bool:
    """
    Get whether ISPC compression is currently active.
    
    Returns:
        True if ISPC will be used, False if fallback will be used
    """
    lib = _load_library()
    return bool(lib.aodds_get_use_ispc())


def get_version() -> str:
    """Get version information for the native DDS library."""
    lib = _load_library()
    return lib.aodds_version().decode('utf-8')


def is_available() -> bool:
    """Check if the native DDS library is available."""
    try:
        _load_library()
        return True
    except (ImportError, FileNotFoundError):
        return False


# Track whether we've issued the fallback warning
_fallback_warning_issued = False


def using_fallback_compressor() -> bool:
    """
    Check if the fallback (lower-quality) compressor is being used.
    
    Returns True if ISPC is unavailable or force_fallback is set.
    
    Returns:
        True if fallback compressor will be used, False if ISPC is active
    """
    lib = _load_library()
    return bool(lib.aodds_using_fallback_compressor())


# ============================================================================
# Decoder Pool Configuration
# ============================================================================

def init_decoder_pool(pool_size: int) -> bool:
    """
    Initialize the JPEG decoder pool with a specific size.
    
    The decoder pool provides thread-safe JPEG decoding for parallel tile
    building. Pool size should be calculated as:
        pool_size = max_builders * cpu_threads
    
    For example: 4 background builders on 32-thread CPU = 128 decoders
    
    Must be called before any decoding operations (typically at startup).
    If not called, a default pool size of 64 is used.
    
    Memory usage: ~2KB per pooled decoder (idle), ~350KB per active decode.
    
    Args:
        pool_size: Number of decoders to pool (minimum 1, no upper limit)
        
    Returns:
        True on success, False if pool already in use (too late to resize)
    """
    lib = _load_library()
    return bool(lib.aodds_init_decoder_pool(c_int32(pool_size)))


def get_decoder_pool_size() -> int:
    """
    Get the current decoder pool size.
    
    Returns:
        Current pool size, or default (64) if not explicitly initialized
    """
    lib = _load_library()
    return lib.aodds_get_decoder_pool_size()


class DecoderPoolStats(NamedTuple):
    """Statistics from the decoder pool."""
    pool_size: int      # Total pool capacity
    in_use: int         # Decoders currently in use
    allocated: int      # Decoders actually created (lazy allocation)
    
    @property
    def available(self) -> int:
        """Number of decoders available for use."""
        return self.pool_size - self.in_use
    
    @property
    def memory_mb_idle(self) -> float:
        """Estimated memory usage when idle (all decoders created but not in use)."""
        return self.allocated * 2 / 1024  # ~2KB per idle decoder
    
    @property
    def memory_mb_active(self) -> float:
        """Estimated memory usage with all in_use decoders active."""
        return self.in_use * 0.35  # ~350KB per active decode


def get_decoder_pool_stats() -> DecoderPoolStats:
    """
    Get decoder pool statistics for monitoring.
    
    Returns:
        DecoderPoolStats with pool_size, in_use, and allocated counts
    """
    lib = _load_library()
    pool_size = c_int32()
    in_use = c_int32()
    allocated = c_int32()
    lib.aodds_get_decoder_pool_stats(byref(pool_size), byref(in_use), byref(allocated))
    return DecoderPoolStats(
        pool_size=pool_size.value,
        in_use=in_use.value,
        allocated=allocated.value
    )


def calculate_decoder_pool_size(max_builders: int, cpu_threads: int = 0) -> int:
    """
    Calculate the recommended decoder pool size based on configuration.
    
    Formula: max_builders * cpu_threads
    
    Args:
        max_builders: Maximum number of parallel DDS builders (background_builder_workers)
        cpu_threads: Number of CPU threads (0 = auto-detect from os.cpu_count())
        
    Returns:
        Recommended pool size (minimum 1, no upper limit)
    """
    import os
    if cpu_threads <= 0:
        cpu_threads = os.cpu_count() or 1
    
    pool_size = max_builders * cpu_threads
    
    # Minimum of 1
    return max(1, pool_size)


def _check_compressor_warning():
    """
    Log a warning if using fallback compressor.
    
    Called internally on first DDS build to inform users that
    quality may be slightly reduced without ISPC.
    """
    global _fallback_warning_issued
    if _fallback_warning_issued:
        return
    
    try:
        if using_fallback_compressor():
            log.warning(
                "Using fallback DXT compressor (ISPC unavailable). "
                "Quality may be slightly reduced. Install ISPC library for optimal compression."
            )
            _fallback_warning_issued = True
    except Exception:
        pass  # Don't fail builds due to warning check


# ============================================================================
# Hybrid Pipeline: Python reads files, Native decodes + compresses
# ============================================================================

def build_from_jpegs(
    jpeg_datas: list,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    pool: Optional[c_void_p] = None
) -> bytes:
    """
    Build DDS from pre-read JPEG data (HYBRID APPROACH).
    
    This is the optimal entry point for high-performance tile building:
    - Call this AFTER reading cache files in Python (which is fast)
    - Native code handles decode + compose + compress (where parallelism helps)
    
    This avoids:
    - File I/O overhead in native code
    - ctypes path string overhead
    - Thread overhead for small file reads
    
    Args:
        jpeg_datas: List of JPEG bytes (None or b'' for missing chunks)
                    Length must be a perfect square (16, 64, 256, etc.)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        pool: Optional buffer pool handle from AoDecode
    
    Returns:
        Complete DDS file as bytes
    
    Raises:
        RuntimeError: If DDS build fails
        ValueError: If chunk_count is not a perfect square
    
    Example:
        # Python reads files (fast for cached files)
        jpeg_datas = []
        for chunk in chunks:
            try:
                jpeg_datas.append(Path(chunk.cache_path).read_bytes())
            except FileNotFoundError:
                jpeg_datas.append(None)
        
        # Native builds DDS (parallel decode + compress)
        dds_bytes = build_from_jpegs(jpeg_datas)
    """
    import math
    
    # Check and warn about fallback compressor on first use
    _check_compressor_warning()
    
    lib = _load_library()
    chunk_count = len(jpeg_datas)
    
    if chunk_count == 0:
        raise ValueError("No JPEG data provided")
    
    # Verify perfect square
    chunks_per_side = int(math.sqrt(chunk_count))
    if chunks_per_side * chunks_per_side != chunk_count:
        raise ValueError(
            f"chunk_count must be a perfect square, got {chunk_count}"
        )
    
    # Calculate buffer size
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    dds_size = lib.aodds_calc_dds_size(tile_size, tile_size, 0, fmt)
    
    # Allocate output buffer
    buffer = (c_uint8 * dds_size)()
    
    # Build arrays for C
    # Create array of pointers to JPEG data
    jpeg_ptrs = (POINTER(c_uint8) * chunk_count)()
    jpeg_sizes = (c_uint32 * chunk_count)()
    
    # Keep references to bytes objects to prevent GC during call
    # (No copy needed - cast directly to pointer, bytes are immutable
    # and CPython doesn't move objects in memory)
    jpeg_refs = []
    
    for i, data in enumerate(jpeg_datas):
        if data and len(data) > 0:
            # Keep reference alive and cast directly - NO COPY
            jpeg_refs.append(data)
            jpeg_ptrs[i] = cast(data, POINTER(c_uint8))
            jpeg_sizes[i] = len(data)
        else:
            jpeg_ptrs[i] = None
            jpeg_sizes[i] = 0
    
    bytes_written = c_uint32()
    pool_handle = pool if pool else None
    
    # Setup function signature if not already done
    if not hasattr(lib, '_hybrid_setup_done'):
        lib.aodds_build_from_jpegs.argtypes = [
            POINTER(POINTER(c_uint8)),  # jpeg_data
            POINTER(c_uint32),          # jpeg_sizes
            c_int32,                    # chunk_count
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color
            POINTER(c_uint8),           # dds_output
            c_uint32,                   # output_size
            POINTER(c_uint32),          # bytes_written
            c_void_p                    # pool
        ]
        lib.aodds_build_from_jpegs.restype = c_int32
        lib._hybrid_setup_done = True
    
    # Call native function
    success = lib.aodds_build_from_jpegs(
        jpeg_ptrs,
        jpeg_sizes,
        chunk_count,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        cast(buffer, POINTER(c_uint8)),
        dds_size,
        byref(bytes_written),
        pool_handle
    )
    
    if not success:
        raise RuntimeError("Failed to build DDS from JPEGs")
    
    return bytes(buffer[:bytes_written.value])


def build_from_jpegs_to_buffer(
    buffer: np.ndarray,
    jpeg_datas: list,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    decode_pool: Optional[c_void_p] = None
) -> BufferBuildResult:
    """
    Build DDS from pre-read JPEG data into a pre-allocated buffer (ZERO-COPY).
    
    This is the OPTIMAL path combining:
    - HYBRID approach (Python reads files)
    - ZERO-COPY output (pre-allocated buffer)
    
    Performance improvement over build_from_jpegs():
    - No allocation overhead (~15ms saved)
    - No copy overhead (~65ms saved)
    - Total: ~80ms faster for 4096x4096 tiles
    
    Args:
        buffer: Pre-allocated numpy array (must be large enough for DDS output)
        jpeg_datas: List of JPEG bytes (None or b'' for missing chunks)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        decode_pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        BufferBuildResult with success status and metadata.
        Use result.get_view() for zero-copy access to DDS data.
    
    Example:
        pool = DDSBufferPool()
        buffer, buffer_id = pool.acquire()
        try:
            # Python reads files (fast)
            jpeg_datas = [Path(p).read_bytes() for p in chunk_paths]
            
            # Build DDS with zero-copy output
            result = build_from_jpegs_to_buffer(buffer, jpeg_datas)
            if result.success:
                dds_view = result.get_view()
                # ... use dds_view ...
        finally:
            pool.release(buffer_id)
    """
    import math
    
    lib = _load_library()
    chunk_count = len(jpeg_datas)
    
    if chunk_count == 0:
        return BufferBuildResult(
            buffer=buffer, bytes_written=0, success=False,
            chunks_found=0, chunks_decoded=0, chunks_failed=0,
            mipmaps=0, elapsed_ms=0.0, error="No JPEG data provided"
        )
    
    # Verify perfect square
    chunks_per_side = int(math.sqrt(chunk_count))
    if chunks_per_side * chunks_per_side != chunk_count:
        return BufferBuildResult(
            buffer=buffer, bytes_written=0, success=False,
            chunks_found=0, chunks_decoded=0, chunks_failed=0,
            mipmaps=0, elapsed_ms=0.0,
            error=f"chunk_count must be a perfect square, got {chunk_count}"
        )
    
    # Calculate required buffer size
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    required_size = lib.aodds_calc_dds_size(tile_size, tile_size, 0, fmt)
    
    if len(buffer) < required_size:
        return BufferBuildResult(
            buffer=buffer, bytes_written=0, success=False,
            chunks_found=0, chunks_decoded=0, chunks_failed=0,
            mipmaps=0, elapsed_ms=0.0,
            error=f"Buffer too small: {len(buffer)} < {required_size} bytes needed"
        )
    
    # Build arrays for C
    jpeg_ptrs = (POINTER(c_uint8) * chunk_count)()
    jpeg_sizes = (c_uint32 * chunk_count)()
    # Keep references to bytes objects to prevent GC during call
    # (No copy needed - cast directly to pointer, bytes are immutable
    # and CPython doesn't move objects in memory)
    jpeg_refs = []
    
    for i, data in enumerate(jpeg_datas):
        if data and len(data) > 0:
            # Keep reference alive and cast directly - NO COPY
            jpeg_refs.append(data)
            jpeg_ptrs[i] = cast(data, POINTER(c_uint8))
            jpeg_sizes[i] = len(data)
        else:
            jpeg_ptrs[i] = None
            jpeg_sizes[i] = 0
    
    bytes_written = c_uint32()
    pool_handle = decode_pool if decode_pool else None
    
    # Setup function signature if not already done
    if not hasattr(lib, '_hybrid_setup_done'):
        lib.aodds_build_from_jpegs.argtypes = [
            POINTER(POINTER(c_uint8)),  # jpeg_data
            POINTER(c_uint32),          # jpeg_sizes
            c_int32,                    # chunk_count
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color
            POINTER(c_uint8),           # dds_output
            c_uint32,                   # output_size
            POINTER(c_uint32),          # bytes_written
            c_void_p                    # pool
        ]
        lib.aodds_build_from_jpegs.restype = c_int32
        lib._hybrid_setup_done = True
    
    # Call native function with numpy buffer
    success = lib.aodds_build_from_jpegs(
        jpeg_ptrs,
        jpeg_sizes,
        chunk_count,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        buffer.ctypes.data_as(POINTER(c_uint8)),
        len(buffer),
        byref(bytes_written),
        pool_handle
    )
    
    return BufferBuildResult(
        buffer=buffer,
        bytes_written=bytes_written.value if success else 0,
        success=bool(success),
        chunks_found=chunk_count,  # All chunks were provided
        chunks_decoded=chunk_count if success else 0,
        chunks_failed=0,
        mipmaps=int(math.log2(tile_size)) + 1 if success else 0,
        elapsed_ms=0.0,  # Not tracked in this API
        error="" if success else "Failed to build DDS from JPEGs"
    )


# ============================================================================
# Single Mipmap Build: Build one mipmap level from JPEG data
# ============================================================================

class SingleMipmapResult(NamedTuple):
    """
    Result from single mipmap building.
    
    Returns raw DXT-compressed bytes (no DDS header) that can be
    directly written to pydds.DDS.mipmap_list[n].databuffer.
    """
    success: bool
    bytes_written: int
    data: Optional[bytes]  # Raw DXT bytes (no header)
    elapsed_ms: float
    error: str = ''


def calc_mipmap_size(width: int, height: int, format: str = "BC1") -> int:
    """
    Calculate compressed size for a single mipmap level.
    
    Args:
        width: Image width in pixels
        height: Image height in pixels
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
    
    Returns:
        Compressed size in bytes (no header included)
    """
    block_size = 8 if format.upper() in ("BC1", "DXT1") else 16
    blocks_x = (width + 3) // 4
    blocks_y = (height + 3) // 4
    return blocks_x * blocks_y * block_size


def build_single_mipmap(
    jpeg_datas: List[Optional[bytes]],
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    pool: Optional[c_void_p] = None
) -> SingleMipmapResult:
    """
    Build a single mipmap level from JPEG data.
    
    This function builds ONLY one mipmap level, not the entire mipmap chain.
    Returns raw DXT-compressed bytes without a DDS header.
    
    Use this for on-demand mipmap building where X-Plane requests a specific
    mipmap level (e.g., mipmap 2 with 64 chunks). Much faster than Python path
    for 16+ chunks due to parallel JPEG decoding and ISPC compression.
    
    Performance:
    - 16 chunks (4x4): ~3x faster than Python
    - 64 chunks (8x8): ~3.3x faster than Python  
    - 256 chunks (16x16): ~4x faster than Python
    
    Args:
        jpeg_datas: List of JPEG bytes (None or b'' for missing chunks)
                    Length must be a perfect square (4, 16, 64, 256)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        SingleMipmapResult with raw DXT bytes (no DDS header).
        The data can be written directly to pydds.DDS.mipmap_list[n].databuffer.
    
    Example:
        # Build mipmap 2 (64 chunks for a ZL16 tile)
        jpeg_datas = [chunk.data for chunk in chunks]
        result = build_single_mipmap(jpeg_datas)
        if result.success:
            # Write raw DXT bytes to DDS mipmap buffer
            tile.dds.mipmap_list[2].databuffer.write(result.data)
    """
    import math
    import time
    
    start_time = time.monotonic()
    lib = _load_library()
    chunk_count = len(jpeg_datas)
    
    if chunk_count == 0:
        return SingleMipmapResult(
            success=False, bytes_written=0, data=None, elapsed_ms=0.0,
            error="No JPEG data provided"
        )
    
    # Verify perfect square
    chunks_per_side = int(math.sqrt(chunk_count))
    if chunks_per_side * chunks_per_side != chunk_count:
        return SingleMipmapResult(
            success=False, bytes_written=0, data=None, elapsed_ms=0.0,
            error=f"chunk_count must be a perfect square, got {chunk_count}"
        )
    
    # Calculate required buffer size
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    output_size = calc_mipmap_size(tile_size, tile_size, format)
    
    # Allocate output buffer
    output_buffer = np.zeros(output_size, dtype=np.uint8)
    
    # Build arrays for C
    jpeg_ptrs = (POINTER(c_uint8) * chunk_count)()
    jpeg_sizes = (c_uint32 * chunk_count)()
    # Keep references to bytes objects to prevent GC during call
    jpeg_refs = []
    
    for i, data in enumerate(jpeg_datas):
        if data and len(data) > 0:
            jpeg_refs.append(data)
            jpeg_ptrs[i] = cast(data, POINTER(c_uint8))
            jpeg_sizes[i] = len(data)
        else:
            jpeg_ptrs[i] = None
            jpeg_sizes[i] = 0
    
    bytes_written = c_uint32()
    pool_handle = pool if pool else None
    
    # Setup function signature if not already done
    if not hasattr(lib, '_single_mipmap_setup_done'):
        lib.aodds_build_single_mipmap.argtypes = [
            POINTER(POINTER(c_uint8)),  # jpeg_data
            POINTER(c_uint32),          # jpeg_sizes
            c_int32,                    # chunk_count
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color
            POINTER(c_uint8),           # output
            c_uint32,                   # output_size
            POINTER(c_uint32),          # bytes_written
            c_void_p                    # pool
        ]
        lib.aodds_build_single_mipmap.restype = c_int32
        
        lib.aodds_calc_mipmap_size.argtypes = [c_int32, c_int32, c_int32]
        lib.aodds_calc_mipmap_size.restype = c_uint32
        
        lib._single_mipmap_setup_done = True
    
    # Call native function
    success = lib.aodds_build_single_mipmap(
        jpeg_ptrs,
        jpeg_sizes,
        chunk_count,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        output_buffer.ctypes.data_as(POINTER(c_uint8)),
        output_size,
        byref(bytes_written),
        pool_handle
    )
    
    elapsed_ms = (time.monotonic() - start_time) * 1000
    
    if success and bytes_written.value > 0:
        # Extract bytes from numpy buffer
        result_data = bytes(output_buffer[:bytes_written.value])
        return SingleMipmapResult(
            success=True,
            bytes_written=bytes_written.value,
            data=result_data,
            elapsed_ms=elapsed_ms,
            error=""
        )
    else:
        return SingleMipmapResult(
            success=False,
            bytes_written=0,
            data=None,
            elapsed_ms=elapsed_ms,
            error="Failed to build single mipmap"
        )


# ============================================================================
# Partial Mipmap Building (Rectangular Chunk Layouts)
# ============================================================================

class PartialMipmapResult(NamedTuple):
    """
    Result from partial mipmap building (rectangular chunk layouts).
    
    Unlike SingleMipmapResult which requires square chunk grids,
    this supports arbitrary width × height chunk layouts for building
    specific rows of a mipmap.
    
    Returns raw DXT-compressed bytes that can be written directly
    to the correct offset in pydds.DDS.mipmap_list[n].databuffer.
    """
    success: bool
    bytes_written: int
    data: Optional[bytes]       # Raw DXT bytes (no header)
    pixel_width: int            # Width in pixels (e.g., 2048)
    pixel_height: int           # Height in pixels (e.g., 256 for 1 row)
    elapsed_ms: float
    error: str = ''


def build_partial_mipmap(
    jpeg_datas: List[Optional[bytes]],
    chunks_width: int,
    chunks_height: int,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    pool: Optional[c_void_p] = None
) -> PartialMipmapResult:
    """
    Build a rectangular partial mipmap from JPEG chunks.
    
    Unlike build_single_mipmap() which requires square chunk grids (e.g., 8×8),
    this function supports arbitrary width × height layouts (e.g., 8×1 for
    a single row of chunks).
    
    Use case: Building specific rows of mipmap 0 for partial reads,
    providing 10-20x speedup over Python PIL + DXT compression path.
    
    Args:
        jpeg_datas: List of JPEG bytes in row-major order (None for missing chunks)
        chunks_width: Number of chunks horizontally (e.g., 8)
        chunks_height: Number of chunks vertically (e.g., 1 for single row)
        format: "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        pool: Optional buffer pool (c_void_p from create_buffer_pool)
    
    Returns:
        PartialMipmapResult with compressed DXT data
    
    Example:
        # Build single row (8 chunks × 1 row = 2048×256 pixels)
        result = build_partial_mipmap(
            jpeg_datas=row_jpegs,  # 8 JPEG bytes
            chunks_width=8,
            chunks_height=1
        )
        if result.success:
            # Write to mipmap buffer at correct offset
            mm.data[row_offset:row_offset + result.bytes_written] = result.data
    """
    lib = _load_library()
    if lib is None:
        return PartialMipmapResult(
            success=False, bytes_written=0, data=None,
            pixel_width=0, pixel_height=0, elapsed_ms=0.0,
            error="Native library not available"
        )
    
    start_time = time.monotonic()
    
    chunk_count = chunks_width * chunks_height
    if len(jpeg_datas) != chunk_count:
        return PartialMipmapResult(
            success=False, bytes_written=0, data=None,
            pixel_width=0, pixel_height=0, elapsed_ms=0.0,
            error=f"Expected {chunk_count} chunks ({chunks_width}×{chunks_height}), got {len(jpeg_datas)}"
        )
    
    # Calculate output dimensions
    pixel_width = chunks_width * 256
    pixel_height = chunks_height * 256
    
    # Calculate output size based on format
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    blocksize = 8 if format == "BC1" else 16
    blocks_x = pixel_width // 4
    blocks_y = pixel_height // 4
    output_size = blocks_x * blocks_y * blocksize
    
    # Prepare JPEG data arrays
    # Keep references to prevent garbage collection
    jpeg_refs = []
    jpeg_ptrs = (c_void_p * chunk_count)()
    jpeg_sizes = (c_uint32 * chunk_count)()
    
    for i, data in enumerate(jpeg_datas):
        if data:
            jpeg_refs.append(data)
            jpeg_ptrs[i] = cast(c_char_p(data), c_void_p)
            jpeg_sizes[i] = len(data)
        else:
            jpeg_ptrs[i] = None
            jpeg_sizes[i] = 0
    
    # Allocate output buffer using numpy for efficiency
    output_buffer = np.zeros(output_size, dtype=np.uint8)
    bytes_written = c_uint32(0)
    
    # Get pool handle if provided
    pool_handle = pool if pool else None

    # Setup function signature if not already done
    if not hasattr(lib, '_partial_mipmap_setup_done'):
        lib.aodds_build_partial_mipmap.argtypes = [
            POINTER(c_void_p),          # jpeg_data (array of pointers)
            POINTER(c_uint32),          # jpeg_sizes
            c_int32,                    # chunks_width
            c_int32,                    # chunks_height
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color RGB
            POINTER(c_uint8),           # output
            c_uint32,                   # output_size
            POINTER(c_uint32),          # bytes_written
            c_void_p                    # pool
        ]
        lib.aodds_build_partial_mipmap.restype = c_int32
        lib._partial_mipmap_setup_done = True
    
    # Call native function
    success = lib.aodds_build_partial_mipmap(
        jpeg_ptrs,
        jpeg_sizes,
        c_int32(chunks_width),
        c_int32(chunks_height),
        c_int32(fmt),
        c_uint8(missing_color[0]),
        c_uint8(missing_color[1]),
        c_uint8(missing_color[2]),
        output_buffer.ctypes.data_as(POINTER(c_uint8)),
        c_uint32(output_size),
        byref(bytes_written),
        pool_handle
    )
    
    elapsed_ms = (time.monotonic() - start_time) * 1000
    
    if success and bytes_written.value > 0:
        result_data = bytes(output_buffer[:bytes_written.value])
        return PartialMipmapResult(
            success=True,
            bytes_written=bytes_written.value,
            data=result_data,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            elapsed_ms=elapsed_ms,
            error=""
        )
    else:
        return PartialMipmapResult(
            success=False,
            bytes_written=0,
            data=None,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            elapsed_ms=elapsed_ms,
            error="Failed to build partial mipmap"
        )


class MipmapChainResult(NamedTuple):
    """
    Result from mipmap chain building.
    
    Contains raw DXT bytes for multiple mipmap levels, from the starting
    level down to 4×4. Each mipmap's data can be extracted using the
    offsets and sizes arrays.
    """
    success: bool
    bytes_written: int
    mipmap_count: int
    data: Optional[bytes]            # All mipmaps concatenated
    mipmap_offsets: List[int]        # Offset of each mipmap in data
    mipmap_sizes: List[int]          # Size of each mipmap
    elapsed_ms: float
    error: str = ''
    
    def get_mipmap_data(self, mipmap_index: int) -> Optional[bytes]:
        """Extract raw DXT bytes for a specific mipmap level."""
        if not self.success or not self.data or mipmap_index >= self.mipmap_count:
            return None
        offset = self.mipmap_offsets[mipmap_index]
        size = self.mipmap_sizes[mipmap_index]
        return self.data[offset:offset + size]


def build_mipmap_chain(
    jpeg_datas: List[Optional[bytes]],
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    max_mipmaps: int = 0,
    pool: Optional[c_void_p] = None
) -> MipmapChainResult:
    """
    Build a mipmap chain from JPEG data: starting level + all smaller mipmaps.
    
    This function builds the mipmap level corresponding to the chunk count
    AND all smaller mipmaps down to 4×4, matching Python's gen_mipmaps() behavior.
    
    Use this for on-demand mipmap building to ensure smaller mipmaps are
    also populated, preventing NULL buffer warnings when X-Plane reads
    into smaller mipmap positions.
    
    Args:
        jpeg_datas: List of JPEG bytes (None or b'' for missing chunks)
                    Length must be a perfect square (4, 16, 64, 256)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        max_mipmaps: Maximum mipmaps to generate (0 = all down to 4×4)
        pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        MipmapChainResult with all mipmap data and offsets.
        
    Example:
        # Build mipmap 2 + all smaller mipmaps (64 chunks for a ZL16 tile)
        jpeg_datas = [chunk.data for chunk in chunks]
        result = build_mipmap_chain(jpeg_datas)
        if result.success:
            # Write each mipmap to its DDS buffer
            for i in range(result.mipmap_count):
                mip_data = result.get_mipmap_data(i)
                tile.dds.mipmap_list[start_mipmap + i].databuffer = BytesIO(mip_data)
    """
    import math
    import time
    
    start_time = time.monotonic()
    lib = _load_library()
    chunk_count = len(jpeg_datas)
    
    if chunk_count == 0:
        return MipmapChainResult(
            success=False, bytes_written=0, mipmap_count=0, data=None,
            mipmap_offsets=[], mipmap_sizes=[], elapsed_ms=0.0,
            error="No JPEG data provided"
        )
    
    # Verify perfect square
    chunks_per_side = int(math.sqrt(chunk_count))
    if chunks_per_side * chunks_per_side != chunk_count:
        return MipmapChainResult(
            success=False, bytes_written=0, mipmap_count=0, data=None,
            mipmap_offsets=[], mipmap_sizes=[], elapsed_ms=0.0,
            error=f"chunk_count must be a perfect square, got {chunk_count}"
        )
    
    # Calculate required buffer size for all mipmaps
    tile_size = chunks_per_side * 256
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    
    # Calculate total size for all mipmaps down to 4×4
    total_mipmaps = 0
    size = tile_size
    while size >= 4:
        total_mipmaps += 1
        size //= 2
    
    if max_mipmaps > 0:
        total_mipmaps = min(total_mipmaps, max_mipmaps)
    
    # Calculate output buffer size
    output_size = 0
    size = tile_size
    for _ in range(total_mipmaps):
        output_size += calc_mipmap_size(size, size, format)
        size //= 2
        if size < 4:
            size = 4
    
    # Allocate buffers
    output_buffer = np.zeros(output_size, dtype=np.uint8)
    mipmap_offsets_arr = (c_uint32 * total_mipmaps)()
    mipmap_sizes_arr = (c_uint32 * total_mipmaps)()
    
    # Build arrays for C
    jpeg_ptrs = (POINTER(c_uint8) * chunk_count)()
    jpeg_sizes = (c_uint32 * chunk_count)()
    jpeg_refs = []
    
    for i, data in enumerate(jpeg_datas):
        if data and len(data) > 0:
            jpeg_refs.append(data)
            jpeg_ptrs[i] = cast(data, POINTER(c_uint8))
            jpeg_sizes[i] = len(data)
        else:
            jpeg_ptrs[i] = None
            jpeg_sizes[i] = 0
    
    bytes_written = c_uint32()
    mipmap_count_out = c_int32()
    pool_handle = pool if pool else None
    
    # Setup function signature if not already done
    if not hasattr(lib, '_mipmap_chain_setup_done'):
        lib.aodds_build_mipmap_chain.argtypes = [
            POINTER(POINTER(c_uint8)),  # jpeg_data
            POINTER(c_uint32),          # jpeg_sizes
            c_int32,                    # chunk_count
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color
            POINTER(c_uint8),           # output
            c_uint32,                   # output_size
            POINTER(c_uint32),          # bytes_written
            POINTER(c_int32),           # mipmap_count_out
            POINTER(c_uint32),          # mipmap_offsets
            POINTER(c_uint32),          # mipmap_sizes
            c_int32,                    # max_mipmaps
            c_void_p                    # pool
        ]
        lib.aodds_build_mipmap_chain.restype = c_int32
        lib._mipmap_chain_setup_done = True
    
    # Call native function
    success = lib.aodds_build_mipmap_chain(
        jpeg_ptrs,
        jpeg_sizes,
        chunk_count,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        output_buffer.ctypes.data_as(POINTER(c_uint8)),
        output_size,
        byref(bytes_written),
        byref(mipmap_count_out),
        mipmap_offsets_arr,
        mipmap_sizes_arr,
        max_mipmaps if max_mipmaps > 0 else 0,
        pool_handle
    )
    
    elapsed_ms = (time.monotonic() - start_time) * 1000
    
    if success and bytes_written.value > 0:
        result_data = bytes(output_buffer[:bytes_written.value])
        offsets = [mipmap_offsets_arr[i] for i in range(mipmap_count_out.value)]
        sizes = [mipmap_sizes_arr[i] for i in range(mipmap_count_out.value)]
        
        return MipmapChainResult(
            success=True,
            bytes_written=bytes_written.value,
            mipmap_count=mipmap_count_out.value,
            data=result_data,
            mipmap_offsets=offsets,
            mipmap_sizes=sizes,
            elapsed_ms=elapsed_ms,
            error=""
        )
    else:
        return MipmapChainResult(
            success=False,
            bytes_written=0,
            mipmap_count=0,
            data=None,
            mipmap_offsets=[],
            mipmap_sizes=[],
            elapsed_ms=elapsed_ms,
            error="Failed to build mipmap chain"
        )


# ============================================================================
# Native Multi-Zoom Mipmap Building
# ============================================================================

class NativeMipmapResult(NamedTuple):
    """
    Result from building all mipmaps from native zoom level chunks.
    
    Each mipmap is built from its native zoom level's JPEG chunks:
    - Mipmap 0: ZL16 chunks (best quality)
    - Mipmap 1: ZL15 chunks
    - Mipmap 2: ZL14 chunks
    - etc.
    
    Compatible with MipmapChainResult for uniform handling in getortho.py.
    """
    success: bool
    bytes_written: int
    mipmap_count: int                    # Number of mipmaps in data
    data: Optional[bytes]                # Complete DDS file (header + all mipmaps)
    mipmap_offsets: List[int]            # Offset of each mipmap in data (after 128-byte header)
    mipmap_sizes: List[int]              # Size of each mipmap
    elapsed_ms: float
    error: str = ''
    
    def get_mipmap_data(self, mipmap_index: int) -> Optional[bytes]:
        """Extract raw DXT bytes for a specific mipmap level (excludes DDS header)."""
        if not self.success or not self.data or mipmap_index >= self.mipmap_count:
            return None
        offset = self.mipmap_offsets[mipmap_index]
        size = self.mipmap_sizes[mipmap_index]
        return self.data[offset:offset + size]


def build_all_mipmaps_native(
    jpeg_datas_per_zoom: List[List[Optional[bytes]]],
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    pool: Optional[c_void_p] = None
) -> NativeMipmapResult:
    """
    Build ALL mipmaps from native zoom level chunks.
    
    QUALITY OPTIMIZATION:
    Instead of building mipmap 0 and deriving smaller mipmaps via reduce_half,
    this function builds EACH mipmap from its native zoom level's JPEG chunks:
    - Mipmap 0: ZL16 chunks (256 chunks, 4096x4096)
    - Mipmap 1: ZL15 chunks (64 chunks, 2048x2048)
    - Mipmap 2: ZL14 chunks (16 chunks, 1024x1024)
    - Mipmap 3: ZL13 chunks (4 chunks, 512x512)
    - Mipmap 4: ZL12 chunks (1 chunk, 256x256)
    
    FALLBACK:
    If a zoom level has no chunks (all None/empty), falls back to reduce_half
    from the previous mipmap level. This maintains quality while handling
    network failures gracefully.
    
    Args:
        jpeg_datas_per_zoom: List of lists - outer list is per zoom level,
                             inner list is JPEG bytes for that zoom.
                             e.g., [[256 ZL16 chunks], [64 ZL15 chunks], ...]
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        NativeMipmapResult with complete DDS data (including header).
        
    Example:
        # Build DDS with native chunks at each zoom level
        jpeg_datas_per_zoom = [
            [chunk.data for chunk in tile.chunks[max_zoom]],      # ZL16
            [chunk.data for chunk in tile.chunks[max_zoom - 1]],  # ZL15
            [chunk.data for chunk in tile.chunks[max_zoom - 2]],  # ZL14
            [chunk.data for chunk in tile.chunks[max_zoom - 3]],  # ZL13
            [chunk.data for chunk in tile.chunks[max_zoom - 4]],  # ZL12
        ]
        result = build_all_mipmaps_native(jpeg_datas_per_zoom)
        if result.success:
            with open(dds_path, 'wb') as f:
                f.write(result.data)
    """
    start_time = time.monotonic()
    
    lib = _load_library()
    if lib is None:
        return NativeMipmapResult(
            success=False,
            bytes_written=0,
            mipmap_count=0,
            data=None,
            mipmap_offsets=[],
            mipmap_sizes=[],
            elapsed_ms=0,
            error="Native library not available"
        )
    
    if not jpeg_datas_per_zoom or len(jpeg_datas_per_zoom) == 0:
        return NativeMipmapResult(
            success=False,
            bytes_written=0,
            mipmap_count=0,
            data=None,
            mipmap_offsets=[],
            mipmap_sizes=[],
            elapsed_ms=0,
            error="No zoom level data provided"
        )
    
    # Validate first zoom level
    first_zoom_data = jpeg_datas_per_zoom[0]
    if not first_zoom_data or len(first_zoom_data) == 0:
        return NativeMipmapResult(
            success=False,
            bytes_written=0,
            mipmap_count=0,
            data=None,
            mipmap_offsets=[],
            mipmap_sizes=[],
            elapsed_ms=0,
            error="First zoom level has no chunks"
        )
    
    zoom_count = len(jpeg_datas_per_zoom)
    
    # Calculate tile size from first zoom level
    chunk_count_0 = len(first_zoom_data)
    chunks_per_side_0 = int(chunk_count_0 ** 0.5)
    if chunks_per_side_0 * chunks_per_side_0 != chunk_count_0:
        return NativeMipmapResult(
            success=False,
            bytes_written=0,
            mipmap_count=0,
            data=None,
            mipmap_offsets=[],
            mipmap_sizes=[],
            elapsed_ms=0,
            error=f"First zoom level chunk count {chunk_count_0} is not a perfect square"
        )
    
    tile_size = chunks_per_side_0 * 256  # CHUNK_SIZE = 256
    
    # Parse format
    fmt = 0  # BC1
    if format.upper() in ("BC3", "DXT5"):
        fmt = 1
    
    # Calculate output size and track mipmap offsets/sizes
    block_size = 8 if fmt == 0 else 16
    mipmap_count = 0
    size = tile_size
    output_size = 128  # DDS header
    mipmap_offsets = []
    mipmap_sizes = []
    while size >= 4:
        mipmap_offsets.append(output_size)  # Offset in complete DDS file (after header for first)
        mip_size = ((size + 3) // 4) * ((size + 3) // 4) * block_size
        mipmap_sizes.append(mip_size)
        output_size += mip_size
        mipmap_count += 1
        size //= 2
    
    # Allocate output buffer
    output_buffer = np.zeros(output_size, dtype=np.uint8)
    
    # Build chunk count array
    chunk_counts = (c_int32 * zoom_count)()
    for z in range(zoom_count):
        chunk_counts[z] = len(jpeg_datas_per_zoom[z]) if jpeg_datas_per_zoom[z] else 0
    
    # Build JPEG data arrays (array of arrays)
    # We need to keep references to prevent garbage collection
    jpeg_refs = []
    jpeg_ptr_arrays = []
    jpeg_size_arrays = []
    
    for z in range(zoom_count):
        zoom_data = jpeg_datas_per_zoom[z] if jpeg_datas_per_zoom[z] else []
        chunk_count = len(zoom_data)
        
        if chunk_count > 0:
            ptr_arr = (POINTER(c_uint8) * chunk_count)()
            size_arr = (c_uint32 * chunk_count)()
            
            for i, data in enumerate(zoom_data):
                if data and len(data) > 0:
                    jpeg_refs.append(data)
                    ptr_arr[i] = cast(data, POINTER(c_uint8))
                    size_arr[i] = len(data)
                else:
                    ptr_arr[i] = None
                    size_arr[i] = 0
            
            jpeg_ptr_arrays.append(ptr_arr)
            jpeg_size_arrays.append(size_arr)
        else:
            jpeg_ptr_arrays.append(None)
            jpeg_size_arrays.append(None)
    
    # Build the outer pointer arrays
    jpeg_data_per_zoom_ptrs = (POINTER(POINTER(c_uint8)) * zoom_count)()
    jpeg_sizes_per_zoom_ptrs = (POINTER(c_uint32) * zoom_count)()
    
    for z in range(zoom_count):
        if jpeg_ptr_arrays[z] is not None:
            jpeg_data_per_zoom_ptrs[z] = cast(jpeg_ptr_arrays[z], POINTER(POINTER(c_uint8)))
            jpeg_sizes_per_zoom_ptrs[z] = cast(jpeg_size_arrays[z], POINTER(c_uint32))
        else:
            jpeg_data_per_zoom_ptrs[z] = None
            jpeg_sizes_per_zoom_ptrs[z] = None
    
    bytes_written = c_uint32()
    pool_handle = pool if pool else None
    
    # Setup function signature if not already done
    if not hasattr(lib, '_all_mipmaps_native_setup_done'):
        lib.aodds_build_all_mipmaps_native.argtypes = [
            POINTER(POINTER(POINTER(c_uint8))),  # jpeg_data_per_zoom
            POINTER(POINTER(c_uint32)),          # jpeg_sizes_per_zoom
            POINTER(c_int32),                    # chunk_counts_per_zoom
            c_int32,                             # zoom_count
            c_int32,                             # format
            c_uint8, c_uint8, c_uint8,           # missing color
            POINTER(c_uint8),                    # output
            c_uint32,                            # output_size
            POINTER(c_uint32),                   # bytes_written
            c_void_p                             # pool
        ]
        lib.aodds_build_all_mipmaps_native.restype = c_int32
        lib._all_mipmaps_native_setup_done = True
    
    # Call native function
    success = lib.aodds_build_all_mipmaps_native(
        cast(jpeg_data_per_zoom_ptrs, POINTER(POINTER(POINTER(c_uint8)))),
        cast(jpeg_sizes_per_zoom_ptrs, POINTER(POINTER(c_uint32))),
        chunk_counts,
        zoom_count,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        output_buffer.ctypes.data_as(POINTER(c_uint8)),
        output_size,
        byref(bytes_written),
        pool_handle
    )
    
    elapsed_ms = (time.monotonic() - start_time) * 1000
    
    if success and bytes_written.value > 0:
        result_data = bytes(output_buffer[:bytes_written.value])
        return NativeMipmapResult(
            success=True,
            bytes_written=bytes_written.value,
            mipmap_count=mipmap_count,
            data=result_data,
            mipmap_offsets=mipmap_offsets,
            mipmap_sizes=mipmap_sizes,
            elapsed_ms=elapsed_ms,
            error=""
        )
    else:
        return NativeMipmapResult(
            success=False,
            bytes_written=0,
            mipmap_count=0,
            data=None,
            mipmap_offsets=[],
            mipmap_sizes=[],
            elapsed_ms=elapsed_ms,
            error="Failed to build mipmaps from native chunks"
        )


# ============================================================================
# Direct-to-File Pipeline: Zero-copy DDS building to disk
# ============================================================================

class FileBuildResult(NamedTuple):
    """
    Result from direct-to-file DDS building.
    
    This is the OPTIMAL path for predictive DDS - no Python memory involvement
    after the initial JPEG data is passed to C.
    """
    success: bool
    bytes_written: int
    output_path: str
    error: str = ''


def build_from_jpegs_to_file(
    jpeg_datas: list,
    output_path: str,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    decode_pool: Optional[c_void_p] = None
) -> FileBuildResult:
    """
    Build DDS from pre-read JPEG data and write directly to disk (ZERO-COPY).
    
    This is the OPTIMAL path for predictive DDS caching:
    - No allocation overhead (writes incrementally to disk)
    - No copy overhead (data goes straight to file)
    - No Python memory involvement after JPEG data is passed
    - Perfect integration with EphemeralDDSCache
    
    Performance improvement over build_from_jpegs():
    - ~65ms copy overhead eliminated (no buffer → bytes conversion)
    - No GIL contention during file write (done in C)
    - Streaming write (lower peak memory)
    
    Atomicity:
    - Uses temp file + rename pattern
    - No corrupt files on crash
    
    Args:
        jpeg_datas: List of JPEG bytes (None or b'' for missing chunks)
                    Length must be a perfect square (16, 64, 256, etc.)
        output_path: Path to output DDS file (will be created/overwritten)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        decode_pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        FileBuildResult with success status and metadata.
    
    Example:
        # Python reads files (fast)
        jpeg_datas = [Path(p).read_bytes() for p in chunk_paths]
        
        # Build DDS directly to disk (no Python memory for output)
        result = build_from_jpegs_to_file(
            jpeg_datas,
            "/path/to/cache/tile_12345.dds"
        )
        if result.success:
            print(f"Wrote {result.bytes_written} bytes to {result.output_path}")
    """
    import math
    
    lib = _load_library()
    chunk_count = len(jpeg_datas)
    
    if chunk_count == 0:
        return FileBuildResult(
            success=False, bytes_written=0, output_path=output_path,
            error="No JPEG data provided"
        )
    
    # Verify perfect square
    chunks_per_side = int(math.sqrt(chunk_count))
    if chunks_per_side * chunks_per_side != chunk_count:
        return FileBuildResult(
            success=False, bytes_written=0, output_path=output_path,
            error=f"chunk_count must be a perfect square, got {chunk_count}"
        )
    
    # Build arrays for C
    jpeg_ptrs = (POINTER(c_uint8) * chunk_count)()
    jpeg_sizes = (c_uint32 * chunk_count)()
    # Keep references to bytes objects to prevent GC during call
    # (No copy needed - cast directly to pointer, bytes are immutable
    # and CPython doesn't move objects in memory)
    jpeg_refs = []
    
    for i, data in enumerate(jpeg_datas):
        if data and len(data) > 0:
            # Keep reference alive and cast directly - NO COPY
            jpeg_refs.append(data)
            jpeg_ptrs[i] = cast(data, POINTER(c_uint8))
            jpeg_sizes[i] = len(data)
        else:
            jpeg_ptrs[i] = None
            jpeg_sizes[i] = 0
    
    bytes_written = c_uint32()
    pool_handle = decode_pool if decode_pool else None
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    
    # Setup function signature if not already done
    if not hasattr(lib, '_file_output_setup_done'):
        lib.aodds_build_from_jpegs_to_file.argtypes = [
            POINTER(POINTER(c_uint8)),  # jpeg_data
            POINTER(c_uint32),          # jpeg_sizes
            c_int32,                    # chunk_count
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color
            c_char_p,                   # output_path
            POINTER(c_uint32),          # bytes_written
            c_void_p                    # pool
        ]
        lib.aodds_build_from_jpegs_to_file.restype = c_int32
        lib._file_output_setup_done = True
    
    # Encode path for cross-platform compatibility
    # On Windows, use UTF-8 which works with most file systems
    path_bytes = output_path.encode('utf-8')
    
    # Call native function
    success = lib.aodds_build_from_jpegs_to_file(
        jpeg_ptrs,
        jpeg_sizes,
        chunk_count,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        path_bytes,
        byref(bytes_written),
        pool_handle
    )
    
    return FileBuildResult(
        success=bool(success),
        bytes_written=bytes_written.value if success else 0,
        output_path=output_path,
        error="" if success else "Failed to build DDS to file"
    )


def build_tile_to_file(
    cache_dir: str,
    row: int,
    col: int,
    maptype: str,
    zoom: int,
    output_path: str,
    chunks_per_side: int = 16,
    format: str = "BC1",
    missing_color: Tuple[int, int, int] = (66, 77, 55),
    decode_pool: Optional[c_void_p] = None
) -> FileBuildResult:
    """
    Build DDS from cache files and write directly to disk (NATIVE ZERO-COPY).
    
    This is the OPTIMAL path for NATIVE MODE predictive DDS caching:
    - C reads cache files directly
    - C decodes + compresses in parallel
    - C writes directly to disk (no Python involvement)
    - Perfect integration with EphemeralDDSCache
    
    This provides the same optimization as build_from_jpegs_to_file() but for
    native mode where C handles file I/O.
    
    Performance improvement over build_tile_native():
    - ~65ms copy overhead eliminated (no buffer → bytes conversion)
    - Streaming write (lower peak memory)
    
    Atomicity:
    - Uses temp file + rename pattern
    - No corrupt files on crash
    
    Args:
        cache_dir: Directory containing cached JPEGs
        row: Tile row coordinate
        col: Tile column coordinate
        maptype: Map source identifier (e.g., "BI", "EOX")
        zoom: Zoom level for chunk fetching
        output_path: Path to output DDS file
        chunks_per_side: Number of chunks per side (default 16)
        format: Compression format - "BC1" (DXT1) or "BC3" (DXT5)
        missing_color: RGB tuple for missing chunks
        decode_pool: Optional decode buffer pool handle from AoDecode
    
    Returns:
        FileBuildResult with success status and metadata.
    
    Example:
        result = build_tile_to_file(
            cache_dir="/path/to/cache",
            row=1234, col=5678,
            maptype="BI", zoom=16,
            output_path="/path/to/output.dds"
        )
        if result.success:
            print(f"Wrote {result.bytes_written} bytes")
    """
    lib = _load_library()
    
    bytes_written = c_uint32()
    pool_handle = decode_pool if decode_pool else None
    fmt = FORMAT_BC1 if format.upper() in ("BC1", "DXT1") else FORMAT_BC3
    
    # Setup function signature if not already done
    if not hasattr(lib, '_tile_to_file_setup_done'):
        lib.aodds_build_tile_to_file.argtypes = [
            c_char_p,                   # cache_dir
            c_int32,                    # tile_row
            c_int32,                    # tile_col
            c_char_p,                   # maptype
            c_int32,                    # zoom
            c_int32,                    # chunks_per_side
            c_int32,                    # format
            c_uint8, c_uint8, c_uint8,  # missing color
            c_char_p,                   # output_path
            POINTER(c_uint32),          # bytes_written
            c_void_p                    # pool
        ]
        lib.aodds_build_tile_to_file.restype = c_int32
        lib._tile_to_file_setup_done = True
    
    # Encode paths for cross-platform compatibility
    cache_dir_bytes = cache_dir.encode('utf-8')
    maptype_bytes = maptype.encode('utf-8')
    output_path_bytes = output_path.encode('utf-8')
    
    # Call native function
    success = lib.aodds_build_tile_to_file(
        cache_dir_bytes,
        row,
        col,
        maptype_bytes,
        zoom,
        chunks_per_side,
        fmt,
        missing_color[0],
        missing_color[1],
        missing_color[2],
        output_path_bytes,
        byref(bytes_written),
        pool_handle
    )
    
    return FileBuildResult(
        success=bool(success),
        bytes_written=bytes_written.value if success else 0,
        output_path=output_path,
        error="" if success else "Failed to build DDS to file"
    )


# ============================================================================
# Module initialization
# ============================================================================

# Try to load library on import to fail fast
try:
    _load_library()
except Exception:
    pass  # Will raise on first use if not available


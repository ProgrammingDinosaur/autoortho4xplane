"""
bundle_consolidator.py - Background worker for consolidating JPEGs into AOB2 bundles.

This module provides background processing to consolidate individual JPEG
cache files into AOB2 bundles, significantly improving read performance.

Features:
- Background consolidation (doesn't block downloads)
- Priority queue for user-visible vs prefetch tiles
- Debouncing to batch consolidation work
- Verification before deleting source JPEGs
- Compaction worker for bundle maintenance
- Clean shutdown with proper thread termination

Usage:
    from autoortho.aopipeline.bundle_consolidator import BundleConsolidator
    
    consolidator = BundleConsolidator(cache_dir)
    consolidator.schedule(row=456, col=123, maptype="BI", zoom=16)
    
    # ... later ...
    consolidator.shutdown()
"""

import atexit
import logging
import os
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# Native batch cache I/O for parallel file reads
try:
    from autoortho.aopipeline import AoCache
    _AOCACHE_AVAILABLE = AoCache.is_available()
except ImportError:
    _AOCACHE_AVAILABLE = False


class ConsolidationPriority(IntEnum):
    """Priority levels for consolidation tasks."""
    HIGH = 0       # User-visible tiles (immediate need)
    NORMAL = 1     # Prefetch tiles
    LOW = 2        # Background consolidation
    COMPACTION = 3 # Compaction tasks (lowest priority)


@dataclass(order=True)
class ConsolidationTask:
    """A consolidation task with priority ordering."""
    priority: int
    row: int = field(compare=False)  # Chunk grid origin row (at zoom level)
    col: int = field(compare=False)  # Chunk grid origin col (at zoom level)
    maptype: str = field(compare=False)
    zoom: int = field(compare=False)  # Chunk zoom level (effective_zoom)
    timestamp: float = field(default_factory=time.time, compare=False)
    chunks_per_side: int = field(default=16, compare=False)
    # Optional: JPEG data passed directly (avoids disk read)
    # List of bytes or None for each chunk position
    jpeg_datas: Optional[List[Optional[bytes]]] = field(default=None, compare=False)
    # Tile ID coordinates for bundle path (at tilename_zoom)
    # If None, falls back to using row/col/zoom (legacy behavior)
    tile_row: Optional[int] = field(default=None, compare=False)
    tile_col: Optional[int] = field(default=None, compare=False)
    tile_zoom: Optional[int] = field(default=None, compare=False)
    # Sentinel value to signal worker shutdown
    is_shutdown_sentinel: bool = field(default=False, compare=False)


# Sentinel task to signal worker threads to exit
_SHUTDOWN_SENTINEL = ConsolidationTask(
    priority=-1,  # Highest priority to be processed immediately
    row=0, col=0, maptype="", zoom=0,
    is_shutdown_sentinel=True
)


# Track all active consolidators for cleanup on exit
_active_consolidators: List[weakref.ref] = []
_active_consolidators_lock = threading.Lock()


def _cleanup_all_consolidators():
    """atexit handler to ensure all consolidators are shut down."""
    with _active_consolidators_lock:
        for ref in _active_consolidators:
            consolidator = ref()
            if consolidator is not None:
                try:
                    consolidator.shutdown(wait=False, timeout=1.0)
                except Exception:
                    pass
        _active_consolidators.clear()


# Register cleanup handler
atexit.register(_cleanup_all_consolidators)


class BundleConsolidator:
    """
    Background worker that consolidates JPEGs into bundles.
    
    Runs in dedicated daemon threads to avoid blocking download threads
    and to ensure clean shutdown when the application exits.
    Uses priority queue to process user-visible tiles before prefetch tiles.
    
    Thread Safety: All public methods are thread-safe.
    """
    
    # Default configuration
    DEBOUNCE_DELAY_MS = 100  # Minimal delay - cache writes are synchronous, no need to wait long
    RETRY_DELAY_MS = 500  # Retry delay if no files found (fallback for edge cases)
    MAX_RETRIES = 1  # Single retry for edge cases (sync writes should make this rare)
    COMPACTION_CHECK_INTERVAL = 300  # Check for compaction every 5 minutes
    COMPACTION_THRESHOLD = 0.30  # 30% garbage triggers compaction
    
    def __init__(
        self,
        cache_dir: str,
        delete_jpegs: bool = True,
        max_workers: int = 1,
        enabled: bool = True
    ):
        """
        Initialize the consolidator.
        
        Args:
            cache_dir: Base cache directory
            delete_jpegs: If True, delete source JPEGs after successful bundle creation
            max_workers: Number of worker threads
            enabled: If False, consolidation is disabled (no-op)
        """
        self.cache_dir = cache_dir
        self.delete_jpegs = delete_jpegs
        self.enabled = enabled
        self._max_workers = max_workers
        
        # Task queue and tracking
        self._task_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._pending: Dict[str, ConsolidationTask] = {}  # tile_key -> task
        self._pending_lock = threading.Lock()
        
        # Completion notification (replaces polling in wait_for_pending)
        self._completion_condition = threading.Condition()
        self._recently_completed: set = set()
        self._completion_cleanup_threshold = 1000  # Cleanup after this many entries
        
        # Debouncing: track last chunk arrival per tile
        self._last_chunk_time: Dict[str, float] = {}
        self._debounce_lock = threading.Lock()
        self._last_chunk_time_max = 2000  # Max entries before cleanup
        
        # Per-bundle locks to serialize updates to the same bundle file
        # This prevents race conditions when multiple zoom levels update simultaneously
        self._bundle_locks: Dict[str, threading.Lock] = {}
        self._bundle_locks_lock = threading.Lock()  # Protects _bundle_locks dict
        self._bundle_locks_max = 500  # Max entries before cleanup
        self._bundle_locks_cleanup_counter = 0  # Cleanup every N calls
        
        # Per-tile completion events (replaces thundering herd notify_all)
        # Maps tile_prefix (row_col_maptype_) -> Event
        # Using per-tile events means only threads waiting for a specific tile
        # are woken when that tile completes, avoiding thundering herd
        self._tile_events: Dict[str, threading.Event] = {}
        self._tile_events_lock = threading.Lock()
        self._tile_events_max = 500  # Max entries before cleanup
        
        # Worker threads (daemon threads for clean shutdown)
        self._workers: List[threading.Thread] = []
        self._shutdown_event = threading.Event()
        self._workers_started = False
        
        if enabled and max_workers > 0:
            self._start_workers()
        
        # Statistics
        self._stats = {
            'bundles_created': 0,
            'bundles_updated': 0,
            'bundles_merged': 0,  # New chunks merged into existing zoom level
            'bundles_skipped': 0,  # Skipped because zoom exists and no new data
            'jpegs_consolidated': 0,
            'bytes_saved': 0,
            'errors': 0,
            'in_memory_consolidations': 0,  # Used in-memory data (no disk read)
            'disk_read_consolidations': 0,  # Had to read from disk
        }
        self._stats_lock = threading.Lock()
        
        # Callbacks
        self._on_bundle_created: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        
        # Register for cleanup on exit
        with _active_consolidators_lock:
            _active_consolidators.append(weakref.ref(self))
    
    def _start_workers(self):
        """Start background worker threads as daemon threads."""
        if self._workers_started:
            return
        
        self._workers_started = True
        
        # Start multiple consolidation workers for parallel processing
        # Using daemon=True ensures threads are killed when main program exits
        for i in range(self._max_workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"bundle_consolidator_{i}",
                daemon=True  # Critical: daemon threads exit when main program exits
            )
            thread.start()
            self._workers.append(thread)
    
    def _get_bundle_lock(self, bundle_path: str) -> threading.Lock:
        """
        Get or create a lock for a specific bundle file.
        
        This ensures that only one worker at a time can update a given bundle,
        preventing race conditions where multiple zoom levels try to update
        simultaneously and lose each other's data.
        """
        with self._bundle_locks_lock:
            if bundle_path not in self._bundle_locks:
                self._bundle_locks[bundle_path] = threading.Lock()
                # Periodic cleanup to prevent unbounded growth
                self._bundle_locks_cleanup_counter += 1
                if self._bundle_locks_cleanup_counter >= 100:
                    self._bundle_locks_cleanup_counter = 0
                    self._cleanup_bundle_locks_unlocked()
            return self._bundle_locks[bundle_path]
    
    def _cleanup_bundle_locks_unlocked(self):
        """
        Clean up old bundle locks that are no longer in use.
        Must be called with _bundle_locks_lock held.
        """
        if len(self._bundle_locks) <= self._bundle_locks_max:
            return
        
        # Remove locks that are not currently held
        # Start from oldest entries (arbitrary order in dict)
        to_remove = []
        for path, lock in self._bundle_locks.items():
            if not lock.locked():
                to_remove.append(path)
            if len(self._bundle_locks) - len(to_remove) <= self._bundle_locks_max // 2:
                break
        
        for path in to_remove:
            del self._bundle_locks[path]
        
        if to_remove:
            log.debug(f"Cleaned up {len(to_remove)} bundle locks")
    
    def _worker_loop(self):
        """Main worker loop - processes consolidation tasks."""
        log.debug("Bundle consolidator worker started")
        
        # Track retry counts per tile
        retry_counts: Dict[str, int] = {}
        
        while not self._shutdown_event.is_set():
            try:
                # Get task with short timeout to allow responsive shutdown
                try:
                    task = self._task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                
                # TEMP STAT: Track queue size when task is picked up
                current_queue_size = self._task_queue.qsize()
                with self._stats_lock:
                    self._stats['queue_size_samples'] = self._stats.get('queue_size_samples', 0) + 1
                    self._stats['queue_size_total'] = self._stats.get('queue_size_total', 0) + current_queue_size
                    if current_queue_size > self._stats.get('queue_size_max', 0):
                        self._stats['queue_size_max'] = current_queue_size
                
                # Check for shutdown sentinel
                if task.is_shutdown_sentinel:
                    log.debug("Bundle consolidator worker received shutdown sentinel")
                    break
                
                # Double-check shutdown after getting task
                if self._shutdown_event.is_set():
                    # Put task back for other workers or discard
                    break
                
                # CRITICAL: Use tile ID coordinates AND zoom for tile_key!
                # This must match what schedule() uses, or pending tracking breaks.
                path_row = task.tile_row if task.tile_row is not None else task.row
                path_col = task.tile_col if task.tile_col is not None else task.col
                tile_key = self._get_tile_key(path_row, path_col, task.maptype, task.zoom)
                
                # Debounce disabled for performance - consolidation uses in-memory data
                # so there's no benefit to waiting for more chunks. The task already
                # has all available JPEG data when scheduled.
                
                # Get retry count for this tile
                retry_count = retry_counts.get(tile_key, 0)
                
                # Process the task
                try:
                    # TEMP STAT: Track per-tile consolidation time
                    tile_start_time = time.monotonic()
                    success = self._consolidate_tile(task, retry_count)
                    tile_elapsed_ms = (time.monotonic() - tile_start_time) * 1000
                    
                    # Record timing stats
                    with self._stats_lock:
                        self._stats['tile_consolidation_count'] = self._stats.get('tile_consolidation_count', 0) + 1
                        self._stats['tile_consolidation_total_ms'] = self._stats.get('tile_consolidation_total_ms', 0) + tile_elapsed_ms
                        if tile_elapsed_ms > self._stats.get('tile_consolidation_max_ms', 0):
                            self._stats['tile_consolidation_max_ms'] = tile_elapsed_ms
                        if tile_elapsed_ms < self._stats.get('tile_consolidation_min_ms', float('inf')):
                            self._stats['tile_consolidation_min_ms'] = tile_elapsed_ms
                    
                    if not success and retry_count < self.MAX_RETRIES:
                        # Retry - wait for async writes to complete
                        # IMPORTANT: Clear jpeg_datas before retry to avoid memory bloat
                        # (retry will read from disk anyway since in-memory failed)
                        task.jpeg_datas = None
                        retry_counts[tile_key] = retry_count + 1
                        # Check shutdown before sleeping
                        if self._shutdown_event.wait(timeout=self.RETRY_DELAY_MS / 1000.0):
                            break  # Shutdown requested during retry wait
                        self._task_queue.put(task)  # Requeue for retry
                        continue  # Don't remove from pending yet
                    
                    # Done with this tile (success or max retries reached)
                    retry_counts.pop(tile_key, None)
                    
                except Exception as e:
                    log.error(
                        f"Error consolidating tile {tile_key}: {e}"
                    )
                    with self._stats_lock:
                        self._stats['errors'] += 1
                    if self._on_error:
                        try:
                            self._on_error(tile_key, e)
                        except Exception:
                            pass
                    retry_counts.pop(tile_key, None)
                finally:
                    # CRITICAL: Free memory immediately after
                    # processing.  Using finally guarantees cleanup
                    # even if _on_error or stats code raises.
                    # Safe on retry path: jpeg_datas is already
                    # None before requeue (line 331).
                    task.jpeg_datas = None
                
                # Remove from pending and notify waiters
                with self._pending_lock:
                    self._pending.pop(tile_key, None)
                
                # Signal ONLY threads waiting for this tile (no thundering herd)
                # This is O(1) vs O(N) for notify_all() with N waiting threads
                self._signal_tile_completion(tile_key)
                
                # Also update recently_completed for backward compatibility
                # (some code may still use the old polling-based check)
                with self._completion_condition:
                    self._recently_completed.add(tile_key)
                    # Periodic cleanup of old completions to prevent unbounded growth
                    if len(self._recently_completed) > self._completion_cleanup_threshold:
                        # Keep only last 100 entries (arbitrary - could tune)
                        excess = len(self._recently_completed) - 100
                        for _ in range(excess):
                            self._recently_completed.pop()
                    
            except Exception as e:
                log.exception(f"Unexpected error in consolidator worker: {e}")
                # Check shutdown even after exception
                if self._shutdown_event.is_set():
                    break
        
        log.debug("Bundle consolidator worker stopped")
    
    def _get_tile_key(self, row: int, col: int, maptype: str, zoom: int = None) -> str:
        """Generate unique key for a tile at a specific zoom level.
        
        CRITICAL: Include zoom in key! A tile has chunks at multiple zoom levels
        (for different mipmaps). Each zoom level needs separate consolidation.
        Without zoom, only the first zoom level gets scheduled, others rejected!
        """
        if zoom is not None:
            return f"{row}_{col}_{maptype}_{zoom}"
        else:
            # Fallback for is_pending() checks that don't know the specific zoom
            return f"{row}_{col}_{maptype}"
    
    def _get_tile_key_prefix(self, row: int, col: int, maptype: str) -> str:
        """Get the prefix for matching any zoom level of a tile."""
        return f"{row}_{col}_{maptype}_"
    
    def _get_or_create_event(self, tile_prefix: str) -> threading.Event:
        """
        Get or create completion event for a tile prefix.
        
        Thread-safe. Used to avoid thundering herd when waiting for completion.
        """
        with self._tile_events_lock:
            if tile_prefix not in self._tile_events:
                self._tile_events[tile_prefix] = threading.Event()
                # Periodic cleanup of old events
                if len(self._tile_events) > self._tile_events_max:
                    self._cleanup_tile_events_unlocked()
            return self._tile_events[tile_prefix]
    
    def _cleanup_tile_events_unlocked(self):
        """
        Clean up old tile events that have been signaled.
        Must be called with _tile_events_lock held.
        """
        # Remove events that are set (completed) - they won't be waited on again
        to_remove = [k for k, v in self._tile_events.items() if v.is_set()]
        for k in to_remove[:len(to_remove) // 2]:  # Remove half to avoid thrashing
            del self._tile_events[k]
        if to_remove:
            log.debug(f"Cleaned up {len(to_remove) // 2} tile events")
    
    def _signal_tile_completion(self, tile_key: str):
        """
        Signal completion for a specific tile.
        
        Wakes only threads waiting for this tile (no thundering herd).
        Signals BOTH the full tile_key event (for zoom-specific waits)
        AND the prefix event (for any-zoom waits).
        """
        with self._tile_events_lock:
            # Signal full tile_key event (for zoom-specific waits)
            if tile_key in self._tile_events:
                self._tile_events[tile_key].set()
            
            # Also signal prefix event (for any-zoom waits)
            # Extract prefix (row_col_maptype_) from full key (row_col_maptype_zoom)
            parts = tile_key.split("_")
            if len(parts) >= 3:
                prefix = f"{parts[0]}_{parts[1]}_{parts[2]}_"
                if prefix in self._tile_events:
                    self._tile_events[prefix].set()
    
    def _cleanup_tile_event(self, tile_prefix: str):
        """
        Remove event after successful wait.
        
        Called by waiter after wait completes to free memory.
        """
        with self._tile_events_lock:
            self._tile_events.pop(tile_prefix, None)
    
    def schedule(
        self,
        row: int,
        col: int,
        maptype: str,
        zoom: int,
        priority: ConsolidationPriority = ConsolidationPriority.NORMAL,
        chunks_per_side: int = 16,
        jpeg_datas: Optional[List[Optional[bytes]]] = None,
        tile_row: Optional[int] = None,
        tile_col: Optional[int] = None,
        tile_zoom: Optional[int] = None
    ) -> bool:
        """
        Schedule tile for bundle consolidation.
        
        Args:
            row: Chunk grid origin row (at zoom level)
            col: Chunk grid origin col (at zoom level)
            maptype: Map type identifier
            zoom: Zoom level of the chunks
            priority: Task priority
            chunks_per_side: Chunks per side
            jpeg_datas: Optional list of JPEG bytes for each chunk position.
                        If provided, consolidator uses this data directly (no disk read).
                        If None, consolidator reads from cache files on disk.
                        Length must be chunks_per_side * chunks_per_side.
            tile_row: Tile ID row at tilename_zoom (for bundle path)
            tile_col: Tile ID col at tilename_zoom (for bundle path)
            tile_zoom: Tile's tilename_zoom (for bundle path)
        
        Returns:
            True if scheduled, False if consolidation disabled or already pending
        """
        if not self.enabled:
            return False
        
        # Use tile_row/tile_col for the tile key if provided, otherwise use chunk row/col
        # CRITICAL: Include zoom in key! Different zoom levels need separate consolidation tasks.
        key_row = tile_row if tile_row is not None else row
        key_col = tile_col if tile_col is not None else col
        tile_key = self._get_tile_key(key_row, key_col, maptype, zoom)
        
        with self._pending_lock:
            # Skip if already pending for this specific zoom level
            if tile_key in self._pending:
                # Update priority if higher
                existing = self._pending[tile_key]
                if priority < existing.priority:
                    existing.priority = priority
                # Don't replace jpeg_datas - first submission wins
                log.debug(f"CONSOLIDATE_SCHEDULE: {tile_key} already pending, skipping")
                return False
            
            # Create task with optional in-memory JPEG data
            task = ConsolidationTask(
                priority=priority,
                row=row,
                col=col,
                maptype=maptype,
                zoom=zoom,
                chunks_per_side=chunks_per_side,
                jpeg_datas=jpeg_datas,
                tile_row=tile_row,
                tile_col=tile_col,
                tile_zoom=tile_zoom
            )
            
            self._pending[tile_key] = task
            self._task_queue.put(task)
            log.debug(f"Scheduled consolidation: {tile_key} ZL{zoom}")
        
        return True
    
    def notify_chunk_complete(self, row: int, col: int, maptype: str):
        """
        Notify that a chunk has been downloaded (for debouncing).
        
        Call this when a chunk download completes to reset the debounce timer.
        """
        tile_key = self._get_tile_key(row, col, maptype)
        with self._debounce_lock:
            self._last_chunk_time[tile_key] = time.time()
            # Cleanup old entries to prevent unbounded growth
            if len(self._last_chunk_time) > self._last_chunk_time_max:
                # Remove entries older than 5 minutes
                cutoff = time.time() - 300
                old_keys = [k for k, v in self._last_chunk_time.items() if v < cutoff]
                for k in old_keys:
                    del self._last_chunk_time[k]
    
    def _batch_read_jpeg_files(self, paths: List[str]) -> List[Optional[bytes]]:
        """
        Read multiple JPEG files in parallel using native batch I/O.
        
        Falls back to sequential Python reads if native library unavailable.
        
        Args:
            paths: List of file paths to read
            
        Returns:
            List of bytes or None for each path (same order as input)
        """
        if _AOCACHE_AVAILABLE and len(paths) >= 16:
            # Use native parallel batch read (OpenMP)
            try:
                results = AoCache.batch_read_cache(paths, validate_jpeg=False)
                return [data if success else None for data, success in results]
            except Exception as e:
                log.debug(f"Batch read failed, falling back to sequential: {e}")
        
        # Fallback: Sequential Python reads
        jpeg_datas = []
        for path in paths:
            try:
                with open(path, 'rb') as f:
                    jpeg_datas.append(f.read())
            except (FileNotFoundError, IOError, OSError):
                jpeg_datas.append(None)
        return jpeg_datas
    
    def _delete_jpegs_for_skipped_task(self, task: ConsolidationTask, chunk_count: int):
        """
        Delete source JPEGs when consolidation is skipped because bundle already has data.
        
        This prevents JPEGs from piling up in the cache folder when tiles are
        re-requested but the bundle already has the zoom level.
        """
        # Build JPEG paths and delete immediately
        deleted = 0
        for i in range(chunk_count):
            chunk_row = i // task.chunks_per_side
            chunk_col = i % task.chunks_per_side
            abs_col = task.col + chunk_col
            abs_row = task.row + chunk_row
            path = os.path.join(
                self.cache_dir,
                f"{abs_col}_{abs_row}_{task.zoom}_{task.maptype}.jpg"
            )
            try:
                os.remove(path)
                deleted += 1
            except FileNotFoundError:
                pass  # Already deleted
            except (PermissionError, OSError):
                pass  # Locked - orphan cleanup will handle
        if deleted > 0:
            log.debug(f"Deleted {deleted} JPEGs for skipped consolidation")
    
    def _consolidate_tile(self, task: ConsolidationTask, retry_count: int = 0) -> bool:
        """
        Actual consolidation work.
        
        1. Use in-memory JPEG data if provided, otherwise read from disk
        2. Create/update bundle atomically
        3. Verify bundle integrity
        4. Delete source JPEGs if configured
        
        CRITICAL: Bundle paths are based on TILE ID coordinates (tile_row, tile_col at tile_zoom),
        NOT the chunk grid coordinates (task.row, task.col at task.zoom)!
        This ensures bundles can be found when looking up by tile ID.
        
        Returns:
            True if consolidation succeeded or should not retry
            False if no JPEGs found and should retry
        """
        # Handle imports for both frozen (PyInstaller) and direct Python execution
        try:
            from autoortho.utils.bundle_paths import get_bundle2_path, ensure_bundle2_dir
            from autoortho.aopipeline import AoBundle2
        except ImportError:
            from utils.bundle_paths import get_bundle2_path, ensure_bundle2_dir
            from aopipeline import AoBundle2
        
        # Use tile ID coordinates for bundle path if provided, otherwise fall back to chunk coords
        path_row = task.tile_row if task.tile_row is not None else task.row
        path_col = task.tile_col if task.tile_col is not None else task.col
        path_zoom = task.tile_zoom if task.tile_zoom is not None else task.zoom
        
        tile_key = self._get_tile_key(path_row, path_col, task.maptype, task.zoom)
        chunk_count = task.chunks_per_side * task.chunks_per_side
        start_time = time.time()
        
        # EARLY CHECK: Check if bundle already has this zoom level
        # This uses has_zoom_quick() which reads only the header (~200 bytes)
        # instead of loading the entire bundle file, making it ~1000x faster
        bundle_path = get_bundle2_path(self.cache_dir, path_row, path_col, task.maptype, path_zoom)
        bundle_has_zoom = False
        if os.path.exists(bundle_path):
            try:
                bundle_has_zoom = AoBundle2.has_zoom_quick(bundle_path, task.zoom)
            except Exception as e:
                log.debug(f"Quick zoom check failed, will attempt full load: {e}")
                # Continue with normal flow - bundle might be corrupted
        
        # If bundle already has this zoom level AND we have in-memory data,
        # we may need to MERGE new chunks into existing bundle (e.g., filling
        # missing chunks when user increases min_chunk_ratio from 0.9 to 1.0)
        if bundle_has_zoom and task.jpeg_datas is not None:
            new_chunk_count = sum(1 for d in task.jpeg_datas if d is not None)
            if new_chunk_count > 0:
                # MERGE new data into existing bundle
                log.debug(f"Merging {new_chunk_count} new chunks into {tile_key} ZL{task.zoom}")
                try:
                    import math
                    chunks_per_side = int(math.sqrt(len(task.jpeg_datas)))
                    
                    # Acquire per-bundle lock for merge
                    bundle_lock = self._get_bundle_lock(bundle_path)
                    with bundle_lock:
                        AoBundle2.merge_bundle_zoom_data(bundle_path, task.zoom, task.jpeg_datas, chunks_per_side)
                    
                    with self._stats_lock:
                        self._stats['bundles_merged'] = self._stats.get('bundles_merged', 0) + 1
                    
                    # Delete source JPEGs (data is now in bundle)
                    if self.delete_jpegs:
                        self._delete_jpegs_for_skipped_task(task, chunk_count)
                    
                    return True  # Merge complete
                except Exception as e:
                    log.warning(f"Merge failed for {tile_key} ZL{task.zoom}: {e}, will recreate bundle")
                    # Fall through to normal consolidation path
            else:
                # No new data to merge - skip
                log.debug(f"Bundle {tile_key} already has ZL{task.zoom}, no new data to merge")
                with self._stats_lock:
                    self._stats['bundles_skipped'] = self._stats.get('bundles_skipped', 0) + 1
                
                # Still need to delete source JPEGs
                if self.delete_jpegs:
                    self._delete_jpegs_for_skipped_task(task, chunk_count)
                
                return True  # Already done
        elif bundle_has_zoom and task.jpeg_datas is None:
            # Bundle has zoom but no in-memory data - need to check if there are
            # new JPEGs on disk to merge. We'll handle this in the normal flow
            # after reading disk data.
            pass  # Continue to disk read section
        
        # Determine if we have in-memory data or need to read from disk
        use_memory_data = (task.jpeg_datas is not None and 
                          len(task.jpeg_datas) == chunk_count)
        
        if use_memory_data:
            # FAST PATH: Use in-memory JPEG data (avoids 256 disk reads per tile)
            jpeg_datas = task.jpeg_datas
            log.debug(f"Consolidating tile {tile_key} at ZL{task.zoom} (in-memory data)")
            with self._stats_lock:
                self._stats['in_memory_consolidations'] += 1
        else:
            # FALLBACK PATH: Read from disk cache files using batch I/O
            log.debug(f"Consolidating tile {tile_key} at ZL{task.zoom} (disk read, attempt {retry_count + 1})")
            with self._stats_lock:
                self._stats['disk_read_consolidations'] += 1
            
            # Build path list for batch read
            paths = []
            for i in range(chunk_count):
                chunk_row = i // task.chunks_per_side
                chunk_col = i % task.chunks_per_side
                # task.col/row ARE the starting coordinates of the chunk grid (scaled coords)
                abs_col = task.col + chunk_col
                abs_row = task.row + chunk_row
                paths.append(os.path.join(
                    self.cache_dir, 
                    f"{abs_col}_{abs_row}_{task.zoom}_{task.maptype}.jpg"
                ))
            
            # Batch read all files (parallel if native available)
            jpeg_datas = self._batch_read_jpeg_files(paths)
        
        # Build paths list for deletion (always needed, even with in-memory data)
        jpeg_paths: List[Optional[str]] = []
        for i in range(chunk_count):
            chunk_row = i // task.chunks_per_side
            chunk_col = i % task.chunks_per_side
            abs_col = task.col + chunk_col
            abs_row = task.row + chunk_row
            path = os.path.join(
                self.cache_dir,
                f"{abs_col}_{abs_row}_{task.zoom}_{task.maptype}.jpg"
            )
            jpeg_paths.append(path if jpeg_datas[i] is not None else None)
        
        # Count valid chunks
        valid_count = sum(1 for d in jpeg_datas if d is not None)
        if valid_count == 0:
            if not use_memory_data and retry_count < self.MAX_RETRIES:
                log.debug(f"No JPEGs found for tile {tile_key} at ZL{task.zoom}, will retry. "
                         f"First path: {self.cache_dir}/{task.col}_{task.row}_{task.zoom}_{task.maptype}.jpg")
                return False  # Signal to retry
            else:
                log.debug(f"No JPEGs found for tile {tile_key} (in-memory={use_memory_data}), giving up.")
                return True  # Don't retry anymore
        
        log.debug(f"Consolidating {tile_key} ZL{task.zoom}: {valid_count}/{chunk_count} JPEGs")
        
        # CRITICAL: Use tile ID coordinates (path_row, path_col, path_zoom) for bundle path,
        # NOT chunk grid coordinates! This ensures bundles match tile ID naming.
        # bundle_path was already computed in early check above
        bundle_dir = ensure_bundle2_dir(self.cache_dir, path_row, path_col, path_zoom, task.maptype)
        
        # Acquire per-bundle lock to serialize updates to the same bundle file
        # This prevents race conditions when multiple zoom levels update simultaneously
        bundle_lock = self._get_bundle_lock(bundle_path)
        
        with bundle_lock:
            # Check if bundle exists (update vs create)
            # Note: Early check already handled "bundle has this zoom" case, so if we get here
            # with an existing bundle, it means we need to ADD this zoom level
            bundle_exists = os.path.exists(bundle_path)
            
            try:
                if bundle_exists:
                    # Bundle exists but doesn't have this zoom level (early check confirmed)
                    # Add new zoom level to existing bundle
                    try:
                        # Check if zoom already exists, then CLOSE before update
                        # Critical: On Windows, mmap holds the file open and prevents os.replace()
                        zoom_already_exists = False
                        with AoBundle2.Bundle2Python(bundle_path) as existing:
                            zoom_already_exists = existing.has_zoom(task.zoom)
                        # Bundle is now closed - safe to update
                        
                        if zoom_already_exists:
                            # Zoom exists - MERGE new data into existing instead of skipping
                            # This handles the case where we read data from disk and bundle
                            # already has partial data for this zoom level
                            new_chunk_count = sum(1 for d in jpeg_datas if d is not None)
                            if new_chunk_count > 0:
                                log.debug(f"BUNDLE_SAVE: Merging {new_chunk_count} chunks into existing ZL{task.zoom}")
                                import math
                                chunks_per_side = int(math.sqrt(len(jpeg_datas)))
                                AoBundle2.merge_bundle_zoom_data(bundle_path, task.zoom, jpeg_datas, chunks_per_side)
                                with self._stats_lock:
                                    self._stats['bundles_merged'] = self._stats.get('bundles_merged', 0) + 1
                            else:
                                log.debug(f"BUNDLE_SAVE: ZL{task.zoom} already present, no new data")
                                with self._stats_lock:
                                    self._stats['bundles_skipped'] = self._stats.get('bundles_skipped', 0) + 1
                        else:
                            log.debug(f"Updating bundle {tile_key} with ZL{task.zoom}")
                            import math
                            chunks_per_side = int(math.sqrt(len(jpeg_datas)))
                            AoBundle2.update_bundle_with_zoom(bundle_path, task.zoom, jpeg_datas, chunks_per_side)
                    except Exception as e:
                        # If reading existing bundle fails, overwrite it
                        log.warning(f"Could not update bundle {tile_key}: {e}, recreating")
                        if AoBundle2.is_available():
                            AoBundle2.create_bundle_from_data(
                                task.row, task.col, task.maptype, task.zoom,
                                jpeg_datas, bundle_path
                            )
                        else:
                            AoBundle2.create_bundle_from_data_python(
                                task.row, task.col, task.maptype, task.zoom,
                                jpeg_datas, bundle_path
                            )
                else:
                    # Create new bundle
                    if AoBundle2.is_available():
                        # Use native implementation
                        AoBundle2.create_bundle_from_data(
                            task.row, task.col, task.maptype, task.zoom,
                            jpeg_datas, bundle_path
                        )
                    else:
                        # Use pure Python fallback
                        AoBundle2.create_bundle_from_data_python(
                            task.row, task.col, task.maptype, task.zoom,
                            jpeg_datas, bundle_path
                        )
            except Exception as e:
                log.error(f"Failed to create bundle {tile_key}: {e}")
                raise
        
        # Skip validation in production - it reads the entire bundle back from disk
        # which is very slow for large bundles (15MB+ for ZL16 tiles).
        # The atomic write (temp file + rename) ensures integrity.
        # Enable BUNDLE_VALIDATE_ENABLED=1 environment variable for debugging.
        if os.environ.get('BUNDLE_VALIDATE_ENABLED', '0') == '1':
            try:
                if AoBundle2.is_available():
                    if not AoBundle2.validate(bundle_path):
                        raise RuntimeError("Bundle validation failed")
                else:
                    # Python validation - just try to parse it
                    with AoBundle2.Bundle2Python(bundle_path) as bundle:
                        if bundle.get_chunk_count(task.zoom) != chunk_count:
                            raise RuntimeError("Bundle chunk count mismatch")
            except Exception as e:
                log.error(f"Bundle verification failed for {tile_key}: {e}")
                # Try to remove corrupted bundle
                try:
                    os.remove(bundle_path)
                except:
                    pass
                raise
        
        # Delete source JPEGs if configured
        # Failed deletions are fine - orphan cleanup on exit will handle them
        if self.delete_jpegs:
            deleted = 0
            for path in jpeg_paths:
                if path:
                    try:
                        os.remove(path)
                        deleted += 1
                    except FileNotFoundError:
                        deleted += 1  # Already deleted, counts as success
                    except (PermissionError, OSError):
                        # Locked - orphan cleanup will handle
                        pass
            log.debug(f"Deleted {deleted}/{len(jpeg_paths)} JPEGs for {tile_key}")
        
        # Update stats
        with self._stats_lock:
            if bundle_exists:
                self._stats['bundles_updated'] += 1
            else:
                self._stats['bundles_created'] += 1
            self._stats['jpegs_consolidated'] += valid_count
        
        # Callback
        if self._on_bundle_created:
            self._on_bundle_created(tile_key, bundle_path)
        
        elapsed_ms = (time.time() - start_time) * 1000
        log.debug(f"Consolidated {valid_count} JPEGs into bundle for {tile_key} in {elapsed_ms:.0f}ms")
        
        # Track consolidation time for monitoring
        with self._stats_lock:
            total_time = self._stats.get('consolidation_time_ms', 0)
            self._stats['consolidation_time_ms'] = total_time + elapsed_ms
            count = self._stats.get('bundles_created', 0) + self._stats.get('bundles_updated', 0)
            if count > 0:
                self._stats['avg_consolidation_ms'] = self._stats['consolidation_time_ms'] / count
        
        return True  # Success
    
    def get_stats(self) -> dict:
        """Get consolidation statistics including computed averages."""
        with self._stats_lock:
            stats = self._stats.copy()
        
        # Add current queue size (live value)
        stats['queue_size_current'] = self._task_queue.qsize()
        
        # Add pending count
        with self._pending_lock:
            stats['pending_count'] = len(self._pending)
        
        # Compute averages for temp stats
        samples = stats.get('queue_size_samples', 0)
        if samples > 0:
            stats['queue_size_avg'] = stats.get('queue_size_total', 0) / samples
        
        count = stats.get('tile_consolidation_count', 0)
        if count > 0:
            stats['tile_consolidation_avg_ms'] = stats.get('tile_consolidation_total_ms', 0) / count
        
        return stats
    
    def get_pending_count(self) -> int:
        """Get number of pending consolidation tasks."""
        with self._pending_lock:
            return len(self._pending)
    
    def is_pending(self, row: int, col: int, maptype: str, zoom: int = None) -> bool:
        """Check if a tile is pending consolidation.
        
        Args:
            row: Tile row (tile ID coordinates)
            col: Tile col (tile ID coordinates) 
            maptype: Map type
            zoom: If provided, check for specific zoom level. If None, check for ANY zoom level.
        
        Returns:
            True if any matching consolidation is pending
        """
        with self._pending_lock:
            if zoom is not None:
                # Check for specific zoom level
                tile_key = self._get_tile_key(row, col, maptype, zoom)
                return tile_key in self._pending
            else:
                # Check if ANY zoom level of this tile is pending (prefix match)
                prefix = self._get_tile_key_prefix(row, col, maptype)
                return any(key.startswith(prefix) for key in self._pending)
    
    def wait_for_pending(self, row: int, col: int, maptype: str, 
                         timeout: float = None, zoom: int = None) -> bool:
        """
        Wait for pending consolidation of a tile to complete.
        
        Uses per-tile Events for efficient waiting (no thundering herd).
        Only the thread waiting for this specific tile is woken when it completes.
        
        Args:
            row: Tile row (tile ID coordinates)
            col: Tile col (tile ID coordinates)
            maptype: Map type
            timeout: Maximum time to wait in seconds. None = wait up to 60s.
            zoom: If provided, wait for this specific zoom level. If None, wait for ANY zoom.
            
        Returns:
            True if consolidation completed (bundle should now have the zoom level)
            False if not pending, timeout, or still pending after timeout
        """
        if zoom is not None:
            # Wait for specific zoom level
            tile_key = self._get_tile_key(row, col, maptype, zoom)
            
            # Check if this specific zoom is pending
            with self._pending_lock:
                if tile_key not in self._pending:
                    # Check if recently completed
                    if tile_key in self._recently_completed:
                        log.debug(f"BUNDLE_WAIT: ZL{zoom} already completed for tile")
                        return True
                    return False  # Not pending, nothing to wait for
            
            # Use per-tile-zoom event for efficient waiting
            event = self._get_or_create_event(tile_key)
            
            effective_timeout = timeout if timeout is not None else 60.0
            start = time.time()
            try:
                result = event.wait(timeout=effective_timeout)
                if result:
                    log.debug(f"BUNDLE_WAIT: ZL{zoom} completed after {time.time()-start:.3f}s")
                else:
                    log.debug(f"BUNDLE_WAIT: ZL{zoom} timeout after {effective_timeout}s")
                return result
            finally:
                self._cleanup_tile_event(tile_key)
        else:
            # Wait for ANY zoom level (original behavior)
            prefix = self._get_tile_key_prefix(row, col, maptype)
            
            # Check if any zoom level is pending
            with self._pending_lock:
                pending_keys = [k for k in self._pending if k.startswith(prefix)]
                if not pending_keys:
                    return False  # Not pending, nothing to wait for
            
            # Check if already completed (quick path)
            # Snapshot the set to avoid RuntimeError from concurrent modification
            if any(k.startswith(prefix) for k in set(self._recently_completed)):
                log.debug(f"BUNDLE_WAIT: Already completed for {prefix}*")
                return True
            
            # Use per-tile event for efficient waiting (no thundering herd)
            # When this tile completes, _signal_tile_completion sets our event
            event = self._get_or_create_event(prefix)
            
            # Use reasonable default timeout to prevent infinite blocks
            effective_timeout = timeout if timeout is not None else 60.0
            
            start = time.time()
            try:
                result = event.wait(timeout=effective_timeout)
                if result:
                    log.debug(f"BUNDLE_WAIT: Completed for {prefix}* after {time.time()-start:.3f}s")
                else:
                    log.debug(f"BUNDLE_WAIT: Timeout waiting for {prefix}* after {effective_timeout}s")
                return result
            finally:
                # Cleanup event after use to prevent memory leaks
                self._cleanup_tile_event(prefix)
    
    def set_callbacks(
        self,
        on_bundle_created: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str, Exception], None]] = None
    ):
        """Set callback functions for events."""
        self._on_bundle_created = on_bundle_created
        self._on_error = on_error
    
    def shutdown(self, wait: bool = True, timeout: float = 5.0):
        """
        Shutdown the consolidator.
        
        Args:
            wait: If True, wait for worker threads to exit
            timeout: Maximum time to wait for each worker thread (only if wait=True)
        """
        if self._shutdown_event.is_set():
            return  # Already shutting down
            
        log.debug("Shutting down bundle consolidator")
        self._shutdown_event.set()
        
        # Drain the queue to unblock workers waiting on get()
        # This prevents workers from processing more tasks during shutdown
        drained_count = 0
        try:
            while True:
                self._task_queue.get_nowait()
                drained_count += 1
        except queue.Empty:
            pass
        
        if drained_count > 0:
            log.debug(f"Drained {drained_count} pending tasks from queue during shutdown")
        
        # Send shutdown sentinels to wake up all workers
        # Workers blocked on queue.get() will receive these and exit
        for _ in range(len(self._workers)):
            try:
                self._task_queue.put_nowait(_SHUTDOWN_SENTINEL)
            except queue.Full:
                pass  # Queue full, but workers should still see shutdown event
        
        # Wait for worker threads to exit
        if wait and self._workers:
            for thread in self._workers:
                if thread.is_alive():
                    thread.join(timeout=timeout)
                    if thread.is_alive():
                        log.warning(f"Worker thread {thread.name} did not exit within {timeout}s")
        
        # Clear worker list
        self._workers.clear()
        self._workers_started = False
        
        # Clear pending tracking
        with self._pending_lock:
            self._pending.clear()
        
        log.debug("Bundle consolidator shutdown complete")


class CompactionWorker:
    """
    Periodic compaction worker for fragmented bundles.
    
    Runs during idle time (no active downloads) to reclaim space
    from garbage data in bundles.
    """
    
    FRAGMENTATION_THRESHOLD = 0.30  # 30% garbage triggers compaction
    CHECK_INTERVAL = 300  # Check every 5 minutes
    
    def __init__(
        self,
        cache_dir: str,
        threshold: float = FRAGMENTATION_THRESHOLD,
        enabled: bool = True
    ):
        """
        Initialize compaction worker.
        
        Args:
            cache_dir: Base cache directory
            threshold: Fragmentation threshold for compaction
            enabled: If False, compaction is disabled
        """
        self.cache_dir = cache_dir
        self.threshold = threshold
        self.enabled = enabled
        
        self._shutdown_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._idle_event = threading.Event()
        self._idle_event.set()  # Start as idle
        
        # Stats
        self._stats = {
            'bundles_checked': 0,
            'bundles_compacted': 0,
            'bytes_reclaimed': 0,
        }
        self._stats_lock = threading.Lock()
    
    def start(self):
        """Start the compaction worker."""
        if not self.enabled or self._thread is not None:
            return
        
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="bundle_compaction",
            daemon=True
        )
        self._thread.start()
    
    def stop(self, wait: bool = True):
        """Stop the compaction worker."""
        self._shutdown_event.set()
        if self._thread and wait:
            self._thread.join(timeout=5.0)
        self._thread = None
    
    def set_busy(self):
        """Signal that the system is busy (don't compact)."""
        self._idle_event.clear()
    
    def set_idle(self):
        """Signal that the system is idle (can compact)."""
        self._idle_event.set()
    
    def _worker_loop(self):
        """Main compaction loop."""
        log.debug("Compaction worker started")
        
        while not self._shutdown_event.is_set():
            # Wait for idle state
            if not self._idle_event.wait(timeout=60.0):
                continue
            
            # Check shutdown
            if self._shutdown_event.is_set():
                break
            
            # Run compaction check
            try:
                self._check_and_compact()
            except Exception as e:
                log.error(f"Error during compaction check: {e}")
            
            # Sleep until next check
            self._shutdown_event.wait(timeout=self.CHECK_INTERVAL)
        
        log.debug("Compaction worker stopped")
    
    def _check_and_compact(self):
        """Check all bundles and compact if needed."""
        # Handle imports for both frozen (PyInstaller) and direct Python execution
        try:
            from autoortho.utils.bundle_paths import enumerate_bundles
            from autoortho.aopipeline import AoBundle2
        except ImportError:
            from utils.bundle_paths import enumerate_bundles
            from aopipeline import AoBundle2
        
        bundles = enumerate_bundles(self.cache_dir)
        
        for bundle_info in bundles:
            if self._shutdown_event.is_set() or not self._idle_event.is_set():
                break
            
            path = bundle_info['path']
            
            with self._stats_lock:
                self._stats['bundles_checked'] += 1
            
            try:
                if AoBundle2.is_available():
                    if AoBundle2.needs_compaction(path, self.threshold):
                        bytes_reclaimed = AoBundle2.compact(path)
                        if bytes_reclaimed > 0:
                            with self._stats_lock:
                                self._stats['bundles_compacted'] += 1
                                self._stats['bytes_reclaimed'] += bytes_reclaimed
                            log.info(f"Compacted {path}, reclaimed {bytes_reclaimed} bytes")
            except Exception as e:
                log.warning(f"Failed to compact {path}: {e}")
    
    def get_stats(self) -> dict:
        """Get compaction statistics."""
        with self._stats_lock:
            return self._stats.copy()


# ============================================================================
# Convenience Functions
# ============================================================================

_default_consolidator: Optional[BundleConsolidator] = None
_consolidator_lock = threading.Lock()


def get_consolidator(cache_dir: str = None) -> Optional[BundleConsolidator]:
    """Get or create the default consolidator."""
    global _default_consolidator
    
    with _consolidator_lock:
        if _default_consolidator is None and cache_dir:
            _default_consolidator = BundleConsolidator(cache_dir)
        return _default_consolidator


def schedule_consolidation(
    row: int,
    col: int,
    maptype: str,
    zoom: int,
    cache_dir: str = None,
    priority: ConsolidationPriority = ConsolidationPriority.NORMAL
) -> bool:
    """
    Convenience function to schedule consolidation.
    
    Uses the default consolidator if cache_dir matches.
    """
    consolidator = get_consolidator(cache_dir)
    if consolidator:
        return consolidator.schedule(row, col, maptype, zoom, priority)
    return False


def shutdown_consolidator():
    """Shutdown the default consolidator."""
    global _default_consolidator
    
    with _consolidator_lock:
        if _default_consolidator:
            _default_consolidator.shutdown()
            _default_consolidator = None


# ============================================================================
# Orphan JPEG Cleanup
# ============================================================================

def cleanup_orphan_jpegs(cache_dir: str) -> Tuple[int, int]:
    """
    Delete JPEG files whose data is already safely stored in bundles.
    
    Safe to call on program exit when no file locking concerns exist.
    Scans for JPEGs matching the chunk naming pattern and checks if their
    corresponding bundle contains the zoom level.
    
    Args:
        cache_dir: Path to the cache directory to scan
        
    Returns:
        Tuple of (deleted_count, scanned_count)
    """
    import glob
    import re
    
    try:
        from autoortho.utils.bundle_paths import get_bundle2_path
        from autoortho.aopipeline.AoBundle2 import has_zoom_quick
    except ImportError:
        from utils.bundle_paths import get_bundle2_path
        from aopipeline.AoBundle2 import has_zoom_quick
    
    deleted = 0
    scanned = 0
    
    # Pattern: {col}_{row}_{zoom}_{maptype}.jpg
    jpeg_pattern = os.path.join(cache_dir, "*_*_*_*.jpg")
    jpeg_regex = re.compile(r"(\d+)_(\d+)_(\d+)_(\w+)\.jpg$")
    
    for jpeg_path in glob.glob(jpeg_pattern):
        scanned += 1
        filename = os.path.basename(jpeg_path)
        match = jpeg_regex.match(filename)
        if not match:
            continue
        
        col, row, zoom, maptype = match.groups()
        col, row, zoom = int(col), int(row), int(zoom)
        
        # Calculate tile coordinates from chunk coordinates
        tile_row, tile_col, tilename_zoom = _chunk_to_tile_coords(row, col, zoom)
        
        bundle_path = get_bundle2_path(cache_dir, tile_row, tile_col, maptype, tilename_zoom)
        
        if os.path.exists(bundle_path):
            # Check if bundle has this zoom level
            if has_zoom_quick(bundle_path, zoom):
                # Safe to delete - data is in bundle
                try:
                    os.remove(jpeg_path)
                    deleted += 1
                except (PermissionError, OSError):
                    pass  # Still locked somehow, skip
    
    return deleted, scanned


def _chunk_to_tile_coords(chunk_row: int, chunk_col: int, zoom: int) -> Tuple[int, int, int]:
    """
    Convert chunk coordinates back to tile coordinates.
    
    This is the inverse of Tile._get_quick_zoom().
    The tilename_zoom is typically zoom - 4 (for 16x16 chunk grids).
    
    Args:
        chunk_row: Chunk row coordinate
        chunk_col: Chunk column coordinate  
        zoom: Zoom level of the chunk
        
    Returns:
        Tuple of (tile_row, tile_col, tilename_zoom)
    """
    # For a 16x16 chunk grid, tilename_zoom = zoom - 4
    # tile_row = chunk_row // 16, tile_col = chunk_col // 16
    chunks_per_side = 16
    tilename_zoom = zoom - 4  # 16x16 grid = 4 zoom levels difference
    
    tile_row = chunk_row // chunks_per_side
    tile_col = chunk_col // chunks_per_side
    
    return tile_row, tile_col, tilename_zoom

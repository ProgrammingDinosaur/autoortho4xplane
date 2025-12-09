#!/usr/bin/env python3
"""
Chunk Fetch Worker Pool - Global Shared Pool Architecture

Process-isolated HTTP fetching with global shared pool for efficiency.
On Mac, multiple FUSE mounts share a single worker pool to avoid resource waste.

Architecture:
    Main Process (owns the pool)
         │
         ├── FetchManager (BaseManager on localhost:PORT)
         │     └── SharedFetchPool
         │           ├── chunk_queue (to workers)
         │           ├── result_queue (from workers)
         │           └── _results (per-worker buffers)
         │
         └── ChunkWorker Processes (N = configurable)
                 ├── Pull chunk from shared queue
                 ├── HTTP GET the URL
                 ├── Push result with worker_id for routing
                 └── Immediately loop for next chunk (Bank Queue!)

    MacFUSE Workers (connect as clients)
         │
         └── FetchClient
               ├── Connects to FetchManager
               ├── submit(chunk, url, headers) - non-blocking
               ├── Dispatcher thread polls for results
               └── Routes results to local Chunk objects
"""

import os
import logging
import atexit
import multiprocessing as mp
from multiprocessing import Queue
from multiprocessing.managers import BaseManager
from queue import Empty
import time
import threading
from typing import NamedTuple, Optional, Dict, Tuple, List
from collections import defaultdict

log = logging.getLogger(__name__)

# Configuration
DEFAULT_WORKER_COUNT = 4
CONNECTION_POOL_SIZE = 100  # connections per worker
CHUNK_TIMEOUT = 60.0  # Max time for any chunk before considered lost
TIMEOUT_CHECK_INTERVAL = 1.0  # How often to check for stale chunks
POLL_INTERVAL = 0.05  # How often clients poll for results (50ms)


# =============================================================================
# Data Structures
# =============================================================================

class ChunkRequest(NamedTuple):
    """Request to fetch a chunk - sent to worker processes."""
    request_id: int
    url: str
    headers: Dict[str, str]
    timeout: Tuple[float, float]  # (connect_timeout, read_timeout)
    worker_id: str  # Which worker/mount sent this (for routing back)
    chunk_id: str   # Chunk identifier (for matching result to chunk)


class ChunkResult(NamedTuple):
    """Result from a chunk fetch - returned from worker processes."""
    request_id: int
    status: str             # "ok", "http_error", "timeout", "error"
    status_code: int        # HTTP status (200, 404, etc.) or 0 for errors
    data: Optional[bytes]   # JPEG bytes or None
    error_msg: Optional[str]
    worker_id: str          # Route back to this worker
    chunk_id: str           # Match to this chunk


def _get_worker_count() -> int:
    """Get fetch worker count from config."""
    try:
        from aoconfig import CFG
        count = int(getattr(CFG.pydds, 'fetch_workers', DEFAULT_WORKER_COUNT))
        return max(1, min(count, 16))  # Clamp between 1 and 16
    except Exception:
        return DEFAULT_WORKER_COUNT


# =============================================================================
# Worker Process
# =============================================================================

def _chunk_worker(chunk_queue: Queue, result_queue: Queue, 
                  worker_id: int, stop_event) -> None:
    """
    Chunk fetch worker process main loop.
    
    Bank Queue Pattern:
    - Pull one chunk from queue (blocks until available)
    - Download the JPEG
    - Push result with routing info (worker_id, chunk_id)
    - Immediately loop to next chunk
    """
    import requests
    from requests.adapters import HTTPAdapter
    
    # Configure logging for worker
    logging.basicConfig(
        level=logging.INFO,
        format=f'[ChunkWorker-{worker_id}] %(levelname)s: %(message)s'
    )
    worker_log = logging.getLogger(f"chunk_worker_{worker_id}")
    
    session = None
    try:
        # Create session with connection pooling
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=CONNECTION_POOL_SIZE,
            pool_maxsize=CONNECTION_POOL_SIZE,
            max_retries=0  # We handle retries at higher level
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        worker_log.info(f"Worker {worker_id} started with {CONNECTION_POOL_SIZE} connections")
        
    except Exception as e:
        worker_log.error(f"Worker {worker_id} failed to initialize: {e}")
        return
    
    # Main loop - BANK QUEUE PATTERN
    while True:
        try:
            # Check for stop signal
            if stop_event.is_set():
                worker_log.info(f"Worker {worker_id} received stop signal")
                break
            
            # Pull next chunk (blocks until available, with timeout to check stop)
            try:
                request = chunk_queue.get(timeout=1.0)
            except Empty:
                continue  # Check stop signal and try again
            
            if request is None:  # Shutdown signal
                worker_log.info(f"Worker {worker_id} received shutdown signal")
                break
            
            if not isinstance(request, ChunkRequest):
                worker_log.warning(f"Worker {worker_id} received invalid request type")
                continue
            
            # Do HTTP request
            try:
                resp = session.get(
                    request.url, 
                    headers=request.headers, 
                    timeout=request.timeout
                )
                
                if resp.status_code == 200:
                    result = ChunkResult(
                        request_id=request.request_id,
                        status="ok",
                        status_code=200,
                        data=resp.content,
                        error_msg=None,
                        worker_id=request.worker_id,
                        chunk_id=request.chunk_id
                    )
                else:
                    result = ChunkResult(
                        request_id=request.request_id,
                        status="http_error",
                        status_code=resp.status_code,
                        data=None,
                        error_msg=f"HTTP {resp.status_code}",
                        worker_id=request.worker_id,
                        chunk_id=request.chunk_id
                    )
                
                resp.close()
                
            except Exception as e:
                error_type = "timeout" if "timeout" in str(e).lower() else "error"
                result = ChunkResult(
                    request_id=request.request_id,
                    status=error_type,
                    status_code=0,
                    data=None,
                    error_msg=str(e),
                    worker_id=request.worker_id,
                    chunk_id=request.chunk_id
                )
            
            # Push result (non-blocking)
            try:
                result_queue.put(result)
            except Exception as e:
                worker_log.error(f"Failed to put result: {e}")
            
            # Immediately loop to next chunk (Bank Queue!)
            
        except Exception as e:
            worker_log.error(f"Worker {worker_id} error: {e}")
    
    # Cleanup
    if session:
        try:
            session.close()
        except Exception:
            pass
    
    worker_log.info(f"Worker {worker_id} exiting")


# =============================================================================
# SharedFetchPool (Main Process)
# =============================================================================

class SharedFetchPool:
    """
    Shared fetch pool that routes results by worker_id.
    
    Used in main process to serve all MacFUSE workers.
    Results are buffered per-worker and polled by clients.
    """
    
    def __init__(self, num_workers: Optional[int] = None):
        self.num_workers = num_workers or _get_worker_count()
        self.workers: List[mp.Process] = []
        self.chunk_queue: Optional[Queue] = None
        self.result_queue: Optional[Queue] = None
        
        # Per-worker result buffers: worker_id -> [(chunk_id, result), ...]
        self._results: Dict[str, List[Tuple[str, ChunkResult]]] = defaultdict(list)
        self._results_lock = threading.Lock()
        
        # Request tracking for timeouts
        self._pending: Dict[int, Tuple[str, str, float]] = {}  # request_id -> (worker_id, chunk_id, submit_time)
        self._pending_lock = threading.Lock()
        self._next_request_id = 0
        
        # Control
        self._stop_event = threading.Event()
        self._mp_stop_event = None
        self._dispatcher_thread: Optional[threading.Thread] = None
        self._started = False
        
        # Stats
        self._stats = {
            'submitted': 0,
            'completed': 0,
            'timeouts': 0,
            'errors': 0
        }
    
    def start(self) -> bool:
        """Start the shared fetch pool."""
        if self._started:
            return True
        
        try:
            # Use spawn context for clean process isolation
            ctx = mp.get_context('spawn')
            
            # Create queues
            self.chunk_queue = ctx.Queue()
            self.result_queue = ctx.Queue()
            
            # Create multiprocessing stop event
            self._mp_stop_event = ctx.Event()
            
            # Start workers
            for i in range(self.num_workers):
                p = ctx.Process(
                    target=_chunk_worker,
                    args=(self.chunk_queue, self.result_queue, i, self._mp_stop_event),
                    name=f"SharedChunkWorker-{i}",
                    daemon=True
                )
                p.start()
                self.workers.append(p)
            
            # Start dispatcher thread
            self._stop_event.clear()
            self._dispatcher_thread = threading.Thread(
                target=self._dispatcher_loop,
                name="SharedFetchDispatcher",
                daemon=True
            )
            self._dispatcher_thread.start()
            
            self._started = True
            log.info(f"SharedFetchPool started with {self.num_workers} workers")
            return True
            
        except Exception as e:
            log.error(f"Failed to start SharedFetchPool: {e}")
            self.stop()
            return False
    
    def _dispatcher_loop(self) -> None:
        """
        Dispatcher thread - routes results to per-worker buffers.
        Also checks for stale requests (worker process died).
        """
        last_timeout_check = time.time()
        last_health_check = time.time()
        
        while not self._stop_event.is_set():
            try:
                # Pull results from workers
                try:
                    result = self.result_queue.get(timeout=0.1)
                except Empty:
                    result = None
                
                if result and isinstance(result, ChunkResult):
                    # Route to per-worker buffer
                    with self._results_lock:
                        self._results[result.worker_id].append(
                            (result.chunk_id, result)
                        )
                    
                    # Update stats
                    if result.status == "ok":
                        self._stats['completed'] += 1
                    elif result.status == "timeout":
                        self._stats['timeouts'] += 1
                    else:
                        self._stats['errors'] += 1
                    
                    # Remove from pending
                    with self._pending_lock:
                        self._pending.pop(result.request_id, None)
                
                # Periodic checks
                now = time.time()
                
                # Check for stale requests
                if now - last_timeout_check > TIMEOUT_CHECK_INTERVAL:
                    self._check_timeouts()
                    last_timeout_check = now
                
                # Check worker health
                if now - last_health_check > 5.0:
                    self._check_and_restart_workers()
                    last_health_check = now
                    
            except Exception as e:
                log.error(f"SharedFetchPool dispatcher error: {e}")
        
        log.debug("SharedFetchPool dispatcher exiting")
    
    def _check_timeouts(self) -> None:
        """Check for requests that have been pending too long."""
        now = time.time()
        timed_out = []
        
        with self._pending_lock:
            for request_id, (worker_id, chunk_id, submit_time) in list(self._pending.items()):
                if now - submit_time > CHUNK_TIMEOUT:
                    timed_out.append((request_id, worker_id, chunk_id))
        
        # Create timeout results
        for request_id, worker_id, chunk_id in timed_out:
            log.warning(f"Chunk {chunk_id} timed out after {CHUNK_TIMEOUT}s")
            result = ChunkResult(
                request_id=request_id,
                status="timeout",
                status_code=0,
                data=None,
                error_msg="Request timed out (worker may have died)",
                worker_id=worker_id,
                chunk_id=chunk_id
            )
            
            with self._results_lock:
                self._results[worker_id].append((chunk_id, result))
            
            with self._pending_lock:
                self._pending.pop(request_id, None)
            
            self._stats['timeouts'] += 1
    
    def _check_and_restart_workers(self) -> None:
        """Check for crashed workers and restart them."""
        if not self._started:
            return
        
        for i, p in enumerate(self.workers):
            if not p.is_alive():
                exit_code = p.exitcode
                log.warning(f"Shared chunk worker {i} crashed (exit code: {exit_code}), restarting...")
                try:
                    ctx = mp.get_context('spawn')
                    new_p = ctx.Process(
                        target=_chunk_worker,
                        args=(self.chunk_queue, self.result_queue, i, self._mp_stop_event),
                        name=f"SharedChunkWorker-{i}",
                        daemon=True
                    )
                    new_p.start()
                    self.workers[i] = new_p
                    log.info(f"Shared chunk worker {i} restarted")
                except Exception as e:
                    log.error(f"Failed to restart shared chunk worker {i}: {e}")
    
    def submit(self, url: str, headers: Dict[str, str], worker_id: str,
               chunk_id: str, timeout: Tuple[float, float] = (5, 20)) -> int:
        """
        Submit a chunk for fetching.
        
        Args:
            url: URL to fetch
            headers: HTTP headers
            worker_id: Which worker/mount is requesting (for routing back)
            chunk_id: Chunk identifier (for matching result)
            timeout: (connect_timeout, read_timeout)
            
        Returns:
            request_id
        """
        if not self._started:
            if not self.start():
                return -1
        
        with self._pending_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            self._pending[request_id] = (worker_id, chunk_id, time.time())
            self._stats['submitted'] += 1
        
        request = ChunkRequest(
            request_id=request_id,
            url=url,
            headers=headers,
            timeout=timeout,
            worker_id=worker_id,
            chunk_id=chunk_id
        )
        
        try:
            self.chunk_queue.put(request)
        except Exception as e:
            log.error(f"Failed to submit chunk: {e}")
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return -1
        
        return request_id
    
    def poll_results(self, worker_id: str) -> List[Tuple[str, ChunkResult]]:
        """
        Poll for results belonging to a specific worker.
        
        Args:
            worker_id: Worker to get results for
            
        Returns:
            List of (chunk_id, ChunkResult) tuples
        """
        with self._results_lock:
            results = self._results.pop(worker_id, [])
        return results
    
    def stop(self) -> None:
        """Stop the shared fetch pool."""
        if not self._started:
            return
        
        log.info("Stopping SharedFetchPool...")
        
        # Signal stop
        self._stop_event.set()
        if self._mp_stop_event:
            self._mp_stop_event.set()
        
        # Send shutdown signals to workers
        if self.chunk_queue:
            for _ in self.workers:
                try:
                    self.chunk_queue.put_nowait(None)
                except Exception:
                    pass
        
        # Wait for dispatcher
        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=2.0)
        
        # Give workers a moment to exit gracefully
        time.sleep(0.2)
        
        # Terminate workers
        for i, p in enumerate(self.workers):
            try:
                if p.is_alive():
                    log.debug(f"Terminating shared chunk worker {i}")
                    p.terminate()
                    p.join(timeout=0.5)
                    if p.is_alive():
                        log.debug(f"Force killing shared chunk worker {i}")
                        p.kill()
                        p.join(timeout=0.3)
            except Exception as e:
                log.debug(f"Error stopping shared chunk worker {i}: {e}")
        
        # Clean up queues
        try:
            if self.chunk_queue:
                self.chunk_queue.close()
                self.chunk_queue.join_thread()
        except Exception:
            pass
        
        try:
            if self.result_queue:
                self.result_queue.close()
                self.result_queue.join_thread()
        except Exception:
            pass
        
        self.workers = []
        self._started = False
        log.info(f"SharedFetchPool stopped. Stats: {self._stats}")
    
    def get_stats(self) -> Dict[str, int]:
        """Get pool statistics."""
        return dict(self._stats)


# =============================================================================
# FetchManager (BaseManager for cross-process access)
# =============================================================================

# Global shared pool instance (for Manager)
_shared_pool: Optional[SharedFetchPool] = None
_shared_pool_lock = threading.Lock()


def _get_or_create_shared_pool() -> SharedFetchPool:
    """Factory for Manager - creates singleton SharedFetchPool."""
    global _shared_pool
    with _shared_pool_lock:
        if _shared_pool is None:
            _shared_pool = SharedFetchPool()
            _shared_pool.start()
        return _shared_pool


class FetchManager(BaseManager):
    """Manager to expose SharedFetchPool across processes."""
    pass


# Register the pool with exposed methods
FetchManager.register(
    'get_pool',
    callable=_get_or_create_shared_pool,
    exposed=['submit', 'poll_results', 'stop', 'get_stats']
)


def start_fetch_manager(authkey: bytes = b'AOFETCH') -> Tuple[FetchManager, str]:
    """
    Start the FetchManager server.
    
    Returns:
        (manager, address_string) tuple
    """
    mgr = FetchManager(address=('127.0.0.1', 0), authkey=authkey)
    mgr.start()
    host, port = mgr.address
    addr = f"{host}:{port}"
    log.info(f"FetchManager listening on {addr}")
    return mgr, addr


def stop_fetch_manager(mgr: Optional[FetchManager]) -> None:
    """Stop the FetchManager server."""
    global _shared_pool
    
    # Stop the shared pool first
    with _shared_pool_lock:
        if _shared_pool is not None:
            _shared_pool.stop()
            _shared_pool = None
    
    # Then shutdown the manager
    if mgr:
        try:
            mgr.shutdown()
        except Exception as e:
            log.debug(f"FetchManager shutdown error: {e}")


# =============================================================================
# FetchClient (For MacFUSE Workers)
# =============================================================================

class FetchClient:
    """
    Client for connecting to shared FetchManager from MacFUSE workers.
    
    Provides same interface as ChunkFetchPool but routes requests
    through the shared pool in the main process.
    """
    
    def __init__(self, address: str, authkey: bytes, worker_id: str):
        self._worker_id = worker_id
        self._address = address
        self._authkey = authkey
        
        # Local chunk registry: chunk_id -> Chunk object
        self._chunks: Dict[str, object] = {}
        self._lock = threading.Lock()
        
        # Connect to manager
        host, port_str = address.split(':')
        port = int(port_str)
        
        # Register client-side (must match server registration)
        FetchManager.register('get_pool')
        
        self._manager = FetchManager(address=(host, port), authkey=authkey)
        self._manager.connect()
        self._pool = self._manager.get_pool()
        
        # Start dispatcher thread
        self._running = True
        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_loop,
            name=f"FetchClient-{worker_id}",
            daemon=True
        )
        self._dispatcher_thread.start()
        
        log.info(f"FetchClient connected to {address} as worker '{worker_id}'")
    
    def submit(self, chunk, url: str, headers: Dict[str, str],
               timeout: Tuple[float, float] = (5, 20)) -> None:
        """
        Submit chunk for fetching (non-blocking).
        
        Args:
            chunk: Chunk object (has ready, download_started, data attributes)
            url: URL to fetch
            headers: HTTP headers
            timeout: (connect_timeout, read_timeout)
        """
        chunk_id = chunk.chunk_id
        
        # Register chunk locally
        with self._lock:
            self._chunks[chunk_id] = chunk
        
        # Mark as started
        try:
            chunk.download_started.set()
        except Exception:
            pass
        
        # Submit to shared pool
        try:
            self._pool.submit(url, headers, self._worker_id, chunk_id, timeout)
        except Exception as e:
            log.error(f"FetchClient submit failed: {e}")
            # Mark chunk as failed
            with self._lock:
                self._chunks.pop(chunk_id, None)
            chunk.data = None
            try:
                chunk.ready.set()
            except Exception:
                pass
    
    def _dispatcher_loop(self) -> None:
        """Poll for results and route to local chunks."""
        while self._running:
            try:
                # Poll for our results
                results = self._pool.poll_results(self._worker_id)
                
                for chunk_id, result in results:
                    # Find local chunk
                    with self._lock:
                        chunk = self._chunks.pop(chunk_id, None)
                    
                    if chunk:
                        # Update chunk state
                        if result.status == "ok":
                            chunk.data = result.data
                        else:
                            chunk.data = None
                            if result.status_code in [404, 403, 410]:
                                try:
                                    chunk.permanent_failure = True
                                    chunk.failure_reason = f"HTTP {result.status_code}"
                                except Exception:
                                    pass
                        
                        # Store HTTP status code for sync fetch support
                        try:
                            chunk._http_status_code = result.status_code
                        except Exception:
                            pass
                        
                        # Signal ready - wakes up waiters!
                        try:
                            chunk.ready.set()
                        except Exception:
                            pass
                    else:
                        log.debug(f"Received result for unknown chunk: {chunk_id}")
                        
            except Exception as e:
                log.debug(f"FetchClient dispatcher error: {e}")
            
            time.sleep(POLL_INTERVAL)
    
    def fetch(self, url: str, headers: Dict[str, str],
              timeout: Tuple[float, float] = (5, 20)) -> 'ChunkResult':
        """
        Synchronous fetch - blocks until result is ready.
        
        This is a convenience method for simple fetch operations that don't
        need the full async chunk model.
        
        Args:
            url: URL to fetch
            headers: HTTP headers
            timeout: (connect_timeout, read_timeout)
            
        Returns:
            ChunkResult with status, data, etc.
        """
        # Create a minimal chunk-like object for the fetch
        class SyncChunk:
            def __init__(self):
                self.chunk_id = f"sync_{id(self)}_{time.time()}"
                self.ready = threading.Event()
                self.download_started = threading.Event()
                self.data = None
                self.permanent_failure = False
                self.failure_reason = None
                self._http_status_code = 0  # Will be set by dispatcher
        
        chunk = SyncChunk()
        
        # Submit and wait
        self.submit(chunk, url, headers, timeout)
        
        # Wait for result with timeout (connect + read + buffer)
        wait_timeout = timeout[0] + timeout[1] + 5
        if chunk.ready.wait(timeout=wait_timeout):
            status_code = getattr(chunk, '_http_status_code', 0) or 0
            if chunk.data:
                return ChunkResult(
                    request_id=0,
                    status="ok",
                    status_code=status_code or 200,
                    data=chunk.data,
                    error_msg=None,
                    worker_id=self._worker_id,
                    chunk_id=chunk.chunk_id
                )
            elif chunk.permanent_failure:
                return ChunkResult(
                    request_id=0,
                    status="permanent_failure",
                    status_code=status_code,
                    data=None,
                    error_msg=chunk.failure_reason or "Permanent failure",
                    worker_id=self._worker_id,
                    chunk_id=chunk.chunk_id
                )
            else:
                return ChunkResult(
                    request_id=0,
                    status="error",
                    status_code=status_code,
                    data=None,
                    error_msg="No data received",
                    worker_id=self._worker_id,
                    chunk_id=chunk.chunk_id
                )
        else:
            # Timeout
            return ChunkResult(
                request_id=0,
                status="timeout",
                status_code=0,
                data=None,
                error_msg=f"Timeout after {wait_timeout}s",
                worker_id=self._worker_id,
                chunk_id=chunk.chunk_id
            )
    
    def stop(self) -> None:
        """Stop the client."""
        self._running = False
        
        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=2.0)
        
        # Wake up any waiting chunks
        with self._lock:
            for chunk in self._chunks.values():
                try:
                    chunk.data = None
                    chunk.ready.set()
                except Exception:
                    pass
            self._chunks.clear()
        
        log.info(f"FetchClient '{self._worker_id}' stopped")


# =============================================================================
# ChunkFetchPool (Local pool for Windows/Linux single-process mode)
# =============================================================================

class ChunkFetchPool:
    """
    Process-isolated chunk fetch worker pool for local (non-shared) use.
    
    Used on Windows/Linux where there's only one mount per process.
    Same interface as FetchClient for consistency.
    """
    
    def __init__(self, num_workers: Optional[int] = None):
        self.num_workers = num_workers or _get_worker_count()
        self.workers: List[mp.Process] = []
        self.chunk_queue: Optional[Queue] = None
        self.result_queue: Optional[Queue] = None
        
        # Chunk registry: chunk_id -> Chunk object
        self._chunks: Dict[str, object] = {}
        self._submit_times: Dict[str, float] = {}
        self._lock = threading.Lock()
        
        # Dispatcher
        self._dispatcher_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._mp_stop_event = None
        
        self._started = False
        self._stats = {'submitted': 0, 'completed': 0, 'timeouts': 0, 'errors': 0}
    
    def start(self) -> bool:
        """Start the fetch worker pool."""
        if self._started:
            return True
        
        try:
            ctx = mp.get_context('spawn')
            self.chunk_queue = ctx.Queue()
            self.result_queue = ctx.Queue()
            self._mp_stop_event = ctx.Event()
            
            for i in range(self.num_workers):
                p = ctx.Process(
                    target=_chunk_worker,
                    args=(self.chunk_queue, self.result_queue, i, self._mp_stop_event),
                    name=f"ChunkWorker-{i}",
                    daemon=True
                )
                p.start()
                self.workers.append(p)
            
            self._stop_event.clear()
            self._dispatcher_thread = threading.Thread(
                target=self._dispatcher_loop,
                name="ChunkDispatcher",
                daemon=True
            )
            self._dispatcher_thread.start()
            
            self._started = True
            log.info(f"ChunkFetchPool started with {self.num_workers} workers")
            return True
            
        except Exception as e:
            log.error(f"Failed to start ChunkFetchPool: {e}")
            self.stop()
            return False
    
    def _dispatcher_loop(self) -> None:
        """Dispatch results to chunks."""
        last_timeout_check = time.time()
        last_health_check = time.time()
        
        # Batch size for draining results - process multiple per iteration
        BATCH_SIZE = 50
        
        while not self._stop_event.is_set():
            try:
                # Drain up to BATCH_SIZE results from queue
                results = []
                try:
                    # First one can block briefly
                    result = self.result_queue.get(timeout=0.1)
                    if result:
                        results.append(result)
                    
                    # Drain more without blocking
                    for _ in range(BATCH_SIZE - 1):
                        try:
                            result = self.result_queue.get_nowait()
                            if result:
                                results.append(result)
                        except Empty:
                            break
                except Empty:
                    pass
                
                # Process batch with single lock acquisition
                if results:
                    chunks_to_update = []
                    
                    with self._lock:
                        for result in results:
                            if not isinstance(result, ChunkResult):
                                continue
                            chunk_id = result.chunk_id
                            chunk = self._chunks.pop(chunk_id, None)
                            self._submit_times.pop(chunk_id, None)
                            if chunk:
                                chunks_to_update.append((chunk, result))
                    
                    # Update chunks outside the lock
                    for chunk, result in chunks_to_update:
                        if result.status == "ok":
                            chunk.data = result.data
                            self._stats['completed'] += 1
                        else:
                            chunk.data = None
                            if result.status == "timeout":
                                self._stats['timeouts'] += 1
                            else:
                                self._stats['errors'] += 1
                            if result.status_code in [404, 403, 410]:
                                try:
                                    chunk.permanent_failure = True
                                    chunk.failure_reason = f"HTTP {result.status_code}"
                                except Exception:
                                    pass
                        
                        # Store HTTP status code for sync fetch support
                        try:
                            chunk._http_status_code = result.status_code
                        except Exception:
                            pass
                        
                        try:
                            chunk.download_started.set()
                        except Exception:
                            pass
                        try:
                            chunk.ready.set()
                        except Exception:
                            pass
                
                now = time.time()
                if now - last_timeout_check > TIMEOUT_CHECK_INTERVAL:
                    self._check_timeouts()
                    last_timeout_check = now
                
                if now - last_health_check > 5.0:
                    self._check_and_restart_workers()
                    last_health_check = now
                    
            except Exception as e:
                log.error(f"ChunkFetchPool dispatcher error: {e}")
    
    def _check_timeouts(self) -> None:
        """Check for chunks that have been pending too long."""
        now = time.time()
        timed_out = []
        
        with self._lock:
            for chunk_id, submit_time in list(self._submit_times.items()):
                if now - submit_time > CHUNK_TIMEOUT:
                    chunk = self._chunks.get(chunk_id)
                    if chunk:
                        try:
                            if chunk.ready.is_set():
                                del self._chunks[chunk_id]
                                del self._submit_times[chunk_id]
                                continue
                        except Exception:
                            pass
                        timed_out.append((chunk_id, chunk))
        
        for chunk_id, chunk in timed_out:
            log.warning(f"Chunk {chunk_id} timed out after {CHUNK_TIMEOUT}s")
            chunk.data = None
            try:
                chunk.download_started.set()
            except Exception:
                pass
            try:
                chunk.ready.set()
            except Exception:
                pass
            self._stats['timeouts'] += 1
            with self._lock:
                self._chunks.pop(chunk_id, None)
                self._submit_times.pop(chunk_id, None)
    
    def _check_and_restart_workers(self) -> None:
        """Check for crashed workers and restart them."""
        if not self._started:
            return
        
        for i, p in enumerate(self.workers):
            if not p.is_alive():
                exit_code = p.exitcode
                log.warning(f"Chunk worker {i} crashed (exit code: {exit_code}), restarting...")
                try:
                    ctx = mp.get_context('spawn')
                    new_p = ctx.Process(
                        target=_chunk_worker,
                        args=(self.chunk_queue, self.result_queue, i, self._mp_stop_event),
                        name=f"ChunkWorker-{i}",
                        daemon=True
                    )
                    new_p.start()
                    self.workers[i] = new_p
                except Exception as e:
                    log.error(f"Failed to restart chunk worker {i}: {e}")
    
    def submit(self, chunk, url: str, headers: Dict[str, str],
               timeout: Tuple[float, float] = (5, 20)) -> int:
        """Submit a chunk for fetching (non-blocking)."""
        if not self._started:
            if not self.start():
                chunk.data = None
                chunk.ready.set()
                return -1
        
        chunk_id = chunk.chunk_id
        
        with self._lock:
            self._chunks[chunk_id] = chunk
            self._submit_times[chunk_id] = time.time()
            self._stats['submitted'] += 1
        
        # Use a dummy worker_id since we're local
        request = ChunkRequest(
            request_id=id(chunk),
            url=url,
            headers=headers,
            timeout=timeout,
            worker_id="local",
            chunk_id=chunk_id
        )
        
        try:
            self.chunk_queue.put(request)
        except Exception as e:
            log.error(f"Failed to submit chunk: {e}")
            with self._lock:
                self._chunks.pop(chunk_id, None)
                self._submit_times.pop(chunk_id, None)
            chunk.data = None
            chunk.ready.set()
            return -1
        
        return id(chunk)
    
    def fetch(self, url: str, headers: Dict[str, str],
              timeout: Tuple[float, float] = (5, 20)) -> 'ChunkResult':
        """
        Synchronous fetch - blocks until result is ready.
        
        This is a convenience method for simple fetch operations that don't
        need the full async chunk model.
        
        Args:
            url: URL to fetch
            headers: HTTP headers
            timeout: (connect_timeout, read_timeout)
            
        Returns:
            ChunkResult with status, data, etc.
        """
        import threading
        
        # Create a minimal chunk-like object for the fetch
        class SyncChunk:
            def __init__(self):
                self.chunk_id = f"sync_{id(self)}_{time.time()}"
                self.ready = threading.Event()
                self.download_started = threading.Event()
                self.data = None
                self.permanent_failure = False
                self.failure_reason = None
                self._http_status_code = 0  # Will be set by dispatcher
        
        chunk = SyncChunk()
        
        # Submit and wait
        self.submit(chunk, url, headers, timeout)
        
        # Wait for result with timeout (connect + read + buffer)
        wait_timeout = timeout[0] + timeout[1] + 5
        if chunk.ready.wait(timeout=wait_timeout):
            status_code = getattr(chunk, '_http_status_code', 0) or 0
            if chunk.data:
                return ChunkResult(
                    request_id=0,
                    status="ok",
                    status_code=status_code or 200,
                    data=chunk.data,
                    error_msg=None,
                    worker_id="local",
                    chunk_id=chunk.chunk_id
                )
            elif chunk.permanent_failure:
                return ChunkResult(
                    request_id=0,
                    status="permanent_failure",
                    status_code=status_code,
                    data=None,
                    error_msg=chunk.failure_reason or "Permanent failure",
                    worker_id="local",
                    chunk_id=chunk.chunk_id
                )
            else:
                return ChunkResult(
                    request_id=0,
                    status="error",
                    status_code=status_code,
                    data=None,
                    error_msg="No data received",
                    worker_id="local",
                    chunk_id=chunk.chunk_id
                )
        else:
            # Timeout
            return ChunkResult(
                request_id=0,
                status="timeout",
                status_code=0,
                data=None,
                error_msg=f"Timeout after {wait_timeout}s",
                worker_id="local",
                chunk_id=chunk.chunk_id
            )
    
    def stop(self) -> None:
        """Stop the worker pool."""
        if not self._started:
            return
        
        log.info("Stopping ChunkFetchPool...")
        
        self._stop_event.set()
        if self._mp_stop_event:
            self._mp_stop_event.set()
        
        if self.chunk_queue:
            for _ in self.workers:
                try:
                    self.chunk_queue.put_nowait(None)
                except Exception:
                    pass
        
        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=2.0)
        
        time.sleep(0.2)
        
        for i, p in enumerate(self.workers):
            try:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=0.5)
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=0.3)
            except Exception as e:
                log.debug(f"Error stopping chunk worker {i}: {e}")
        
        try:
            if self.chunk_queue:
                self.chunk_queue.close()
                self.chunk_queue.join_thread()
        except Exception:
            pass
        
        try:
            if self.result_queue:
                self.result_queue.close()
                self.result_queue.join_thread()
        except Exception:
            pass
        
        with self._lock:
            for chunk in self._chunks.values():
                try:
                    chunk.data = None
                    chunk.ready.set()
                except Exception:
                    pass
            self._chunks.clear()
            self._submit_times.clear()
        
        self.workers = []
        self._started = False
        log.info(f"ChunkFetchPool stopped. Stats: {self._stats}")


# =============================================================================
# Global pool instances and accessors
# =============================================================================

_chunk_pool: Optional[ChunkFetchPool] = None
_fetch_client: Optional[FetchClient] = None
_pool_lock = threading.Lock()


def get_chunk_fetch_pool() -> ChunkFetchPool:
    """Get or create the local chunk fetch pool."""
    global _chunk_pool
    with _pool_lock:
        if _chunk_pool is None:
            _chunk_pool = ChunkFetchPool()
        return _chunk_pool


def get_fetch_client() -> Optional[FetchClient]:
    """Get the fetch client (only available in MacFUSE worker mode)."""
    global _fetch_client
    with _pool_lock:
        if _fetch_client is None:
            # Try to connect to shared pool
            addr = os.environ.get('AO_FETCH_ADDR')
            if addr:
                auth = os.environ.get('AO_FETCH_AUTH', 'AOFETCH').encode()
                worker_id = os.environ.get('AO_WORKER_ID', str(os.getpid()))
                try:
                    _fetch_client = FetchClient(addr, auth, worker_id)
                except Exception as e:
                    log.error(f"Failed to connect to FetchManager: {e}")
                    return None
        return _fetch_client


def shutdown_chunk_fetch_pool() -> None:
    """Shutdown the local chunk fetch pool."""
    global _chunk_pool
    with _pool_lock:
        if _chunk_pool is not None:
            _chunk_pool.stop()
            _chunk_pool = None


def shutdown_fetch_client() -> None:
    """Shutdown the fetch client."""
    global _fetch_client
    with _pool_lock:
        if _fetch_client is not None:
            _fetch_client.stop()
            _fetch_client = None


def _cleanup_on_exit() -> None:
    """Cleanup handler for program exit."""
    try:
        shutdown_fetch_client()
        shutdown_chunk_fetch_pool()
    except Exception:
        pass


# Register cleanup handler
atexit.register(_cleanup_on_exit)


# =============================================================================
# Legacy compatibility
# =============================================================================

# Legacy functions for backward compatibility
def get_fetch_worker_pool():
    """Legacy: Get fetch worker pool."""
    return get_chunk_fetch_pool()


def shutdown_fetch_worker_pool():
    """Legacy: Shutdown fetch worker pool."""
    shutdown_chunk_fetch_pool()

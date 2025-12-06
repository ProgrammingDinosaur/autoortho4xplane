#!/usr/bin/env python3
"""
Fetch Worker Pool - Process-isolated HTTP fetching for crash safety.

Runs HTTP requests in separate processes so that if urllib3/SSL crashes,
the main process survives and the worker is automatically restarted.

Architecture:
    Main Process
         │
         ├── FetchWorkerPool (manages workers)
         │
         └── Fetch Workers (N = configurable)
                 ├── Each has own requests.Session
                 ├── Persistent connection pool
                 └── Crash isolation
"""

import os
import logging
import atexit
import multiprocessing as mp
from multiprocessing import Queue
from queue import Empty
import time
import threading
from typing import NamedTuple, Optional, Dict, List, Tuple, Any

log = logging.getLogger(__name__)

# Configuration
DEFAULT_WORKER_COUNT = 4
TASK_TIMEOUT = 30  # seconds per request
RETRY_COUNT = 2
RETRY_DELAY = 0.1
CONNECTION_POOL_SIZE = 50  # connections per worker


class FetchRequest(NamedTuple):
    """Request to fetch a URL."""
    request_id: int
    url: str
    headers: Dict[str, str]
    timeout: Tuple[float, float]  # (connect, read)


class FetchResult(NamedTuple):
    """Result from a fetch operation."""
    request_id: int
    status: str  # "ok", "error", "timeout"
    status_code: int  # HTTP status code (0 if error)
    data: Optional[bytes]  # Response body
    error_msg: Optional[str]


class InitError(NamedTuple):
    """Initialization error from worker."""
    worker_id: int
    error: str


def _get_worker_count() -> int:
    """Get fetch worker count from config."""
    try:
        from aoconfig import CFG
        count = int(getattr(CFG.pydds, 'fetch_workers', DEFAULT_WORKER_COUNT))
        return max(1, min(count, 16))  # Clamp between 1 and 16
    except Exception:
        return DEFAULT_WORKER_COUNT


def _worker_process(task_queue: Queue, result_queue: Queue, 
                    worker_id: int) -> None:
    """
    Fetch worker process main loop.
    
    Each worker has its own requests.Session with connection pooling.
    Crashes here don't affect the main process.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    
    # Configure logging for worker
    logging.basicConfig(
        level=logging.INFO,
        format=f'[FetchWorker-{worker_id}] %(message)s'
    )
    worker_log = logging.getLogger(f"fetch_worker_{worker_id}")
    
    try:
        # Create session with connection pooling
        session = requests.Session()
        
        # Configure connection pool and retries at transport level
        retry_strategy = Retry(
            total=0,  # We handle retries at higher level
            backoff_factor=0,
        )
        adapter = HTTPAdapter(
            pool_connections=CONNECTION_POOL_SIZE,
            pool_maxsize=CONNECTION_POOL_SIZE,
            max_retries=retry_strategy
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        worker_log.info(f"Worker {worker_id} started with {CONNECTION_POOL_SIZE} connections")
        
    except Exception as e:
        result_queue.put(InitError(worker_id, str(e)))
        return
    
    while True:
        try:
            # Get task with timeout
            try:
                task = task_queue.get(timeout=30)
            except Empty:
                continue
            
            if task is None:  # Shutdown signal
                worker_log.info(f"Worker {worker_id} shutting down")
                break
            
            if not isinstance(task, FetchRequest):
                continue
            
            request_id = task.request_id
            url = task.url
            headers = task.headers
            timeout = task.timeout
            
            try:
                # Perform the HTTP request
                resp = session.get(url, headers=headers, timeout=timeout)
                
                result_queue.put(FetchResult(
                    request_id=request_id,
                    status="ok",
                    status_code=resp.status_code,
                    data=resp.content,
                    error_msg=None
                ))
                
                resp.close()
                
            except requests.Timeout as e:
                result_queue.put(FetchResult(
                    request_id=request_id,
                    status="timeout",
                    status_code=0,
                    data=None,
                    error_msg=str(e)
                ))
            except Exception as e:
                result_queue.put(FetchResult(
                    request_id=request_id,
                    status="error",
                    status_code=0,
                    data=None,
                    error_msg=str(e)
                ))
                
        except Exception as e:
            worker_log.error(f"Worker {worker_id} error: {e}")
    
    # Cleanup
    try:
        session.close()
    except Exception:
        pass


class FetchWorkerPool:
    """
    Process-isolated HTTP fetch worker pool.
    
    Features:
    - Crash isolation (worker crash doesn't affect main)
    - Auto-restart of crashed workers
    - Persistent connection pools per worker
    - Thread-safe request dispatch
    """
    
    def __init__(self, num_workers: Optional[int] = None):
        self.num_workers = num_workers or _get_worker_count()
        self.workers: List[mp.Process] = []
        self.task_queue: Optional[Queue] = None
        self.result_queue: Optional[Queue] = None
        self.request_counter = 0
        self._lock = threading.Lock()
        self._started = False
        
        # Result dispatch system
        self._pending: Dict[int, Tuple[threading.Event, List]] = {}
        self._dispatcher_thread: Optional[threading.Thread] = None
        self._dispatcher_stop = threading.Event()
    
    def start(self) -> bool:
        """Start the fetch worker pool."""
        if self._started:
            return True
        
        try:
            # Create queues using spawn context for clean isolation
            ctx = mp.get_context('spawn')
            self.task_queue = ctx.Queue()
            self.result_queue = ctx.Queue()
            
            # Start workers
            for i in range(self.num_workers):
                p = ctx.Process(
                    target=_worker_process,
                    args=(self.task_queue, self.result_queue, i),
                    daemon=True
                )
                p.start()
                self.workers.append(p)
            
            # Start result dispatcher thread
            self._dispatcher_stop.clear()
            self._dispatcher_thread = threading.Thread(
                target=self._result_dispatcher,
                name="Fetch-ResultDispatcher",
                daemon=True
            )
            self._dispatcher_thread.start()
            
            self._started = True
            log.info(f"FetchWorkerPool started with {self.num_workers} workers")
            return True
            
        except Exception as e:
            log.error(f"Failed to start FetchWorkerPool: {e}")
            self.stop()
            return False
    
    def _result_dispatcher(self) -> None:
        """Dispatch results to waiting callers."""
        while not self._dispatcher_stop.is_set():
            try:
                result = self.result_queue.get(timeout=0.5)
                
                # Handle InitError
                if isinstance(result, InitError):
                    log.error(f"Fetch worker {result.worker_id} init failed: {result.error}")
                    continue
                
                if isinstance(result, FetchResult):
                    request_id = result.request_id
                    
                    with self._lock:
                        if request_id in self._pending:
                            event, result_list = self._pending[request_id]
                            result_list.append(result)
                            event.set()
                            
            except Empty:
                # Check worker health periodically
                self._check_and_restart_workers()
            except Exception as e:
                log.error(f"Fetch result dispatcher error: {e}")
    
    def stop(self) -> None:
        """Stop the worker pool."""
        if not self._started:
            return
        
        log.debug("Stopping FetchWorkerPool...")
        
        # Stop dispatcher first
        self._dispatcher_stop.set()
        if self._dispatcher_thread:
            self._dispatcher_thread.join(timeout=1)
        
        # Send shutdown signal to workers via queue
        if self.task_queue:
            for _ in self.workers:
                try:
                    self.task_queue.put_nowait(None)
                except Exception:
                    pass
        
        # Give workers a moment to finish gracefully
        time.sleep(0.1)
        
        # Terminate workers - be aggressive to avoid hanging
        for i, p in enumerate(self.workers):
            try:
                if p.is_alive():
                    log.debug(f"Terminating fetch worker {i}")
                    p.terminate()
                    p.join(timeout=0.5)
                    if p.is_alive():
                        log.debug(f"Force killing fetch worker {i}")
                        p.kill()
                        p.join(timeout=0.5)
            except Exception as e:
                log.debug(f"Error stopping fetch worker {i}: {e}")
        
        # Clean up queues
        try:
            if self.task_queue:
                self.task_queue.close()
                self.task_queue.join_thread()
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
        log.info("FetchWorkerPool stopped")
    
    def _check_and_restart_workers(self) -> None:
        """Check for crashed workers and restart them."""
        if not self._started:
            return
        
        for i, p in enumerate(self.workers):
            if not p.is_alive():
                exit_code = p.exitcode
                log.warning(f"Fetch worker {i} crashed (exit code: {exit_code}), restarting...")
                try:
                    ctx = mp.get_context('spawn')
                    new_p = ctx.Process(
                        target=_worker_process,
                        args=(self.task_queue, self.result_queue, i),
                        daemon=True
                    )
                    new_p.start()
                    self.workers[i] = new_p
                except Exception as e:
                    log.error(f"Failed to restart fetch worker {i}: {e}")
    
    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None,
              timeout: Tuple[float, float] = (5, 20),
              request_timeout: float = TASK_TIMEOUT) -> FetchResult:
        """
        Fetch a URL using a worker process.
        
        Args:
            url: URL to fetch
            headers: Optional HTTP headers
            timeout: (connect_timeout, read_timeout) for the HTTP request
            request_timeout: Overall timeout for the operation
            
        Returns:
            FetchResult with status, data, etc.
        """
        if not self._started:
            if not self.start():
                return FetchResult(
                    request_id=0,
                    status="error",
                    status_code=0,
                    data=None,
                    error_msg="Worker pool failed to start"
                )
        
        # Generate request ID
        with self._lock:
            request_id = self.request_counter
            self.request_counter += 1
            
            # Create event for waiting
            event = threading.Event()
            result_list: List[FetchResult] = []
            self._pending[request_id] = (event, result_list)
        
        try:
            # Submit request
            request = FetchRequest(
                request_id=request_id,
                url=url,
                headers=headers or {},
                timeout=timeout
            )
            self.task_queue.put(request)
            
            # Wait for result
            if event.wait(timeout=request_timeout):
                if result_list:
                    return result_list[0]
            
            # Timeout
            return FetchResult(
                request_id=request_id,
                status="timeout",
                status_code=0,
                data=None,
                error_msg="Request timed out"
            )
            
        finally:
            # Cleanup pending entry
            with self._lock:
                self._pending.pop(request_id, None)
    
    def fetch_batch(self, requests_list: List[Tuple[str, Dict[str, str]]],
                    timeout: Tuple[float, float] = (5, 20),
                    request_timeout: float = TASK_TIMEOUT
                    ) -> List[FetchResult]:
        """
        Fetch multiple URLs in parallel.
        
        Args:
            requests_list: List of (url, headers) tuples
            timeout: (connect_timeout, read_timeout) for each HTTP request
            request_timeout: Overall timeout for all operations
            
        Returns:
            List of FetchResult in same order as requests
        """
        if not requests_list:
            return []
        
        if not self._started:
            if not self.start():
                return [FetchResult(
                    request_id=i,
                    status="error",
                    status_code=0,
                    data=None,
                    error_msg="Worker pool failed to start"
                ) for i in range(len(requests_list))]
        
        # Submit all requests
        request_ids = []
        events_and_results = []
        
        with self._lock:
            for url, headers in requests_list:
                request_id = self.request_counter
                self.request_counter += 1
                request_ids.append(request_id)
                
                event = threading.Event()
                result_list: List[FetchResult] = []
                self._pending[request_id] = (event, result_list)
                events_and_results.append((event, result_list))
                
                request = FetchRequest(
                    request_id=request_id,
                    url=url,
                    headers=headers or {},
                    timeout=timeout
                )
                self.task_queue.put(request)
        
        # Wait for all results
        deadline = time.time() + request_timeout
        results = []
        
        try:
            for i, (event, result_list) in enumerate(events_and_results):
                remaining = deadline - time.time()
                if remaining <= 0:
                    results.append(FetchResult(
                        request_id=request_ids[i],
                        status="timeout",
                        status_code=0,
                        data=None,
                        error_msg="Batch timeout"
                    ))
                    continue
                
                if event.wait(timeout=remaining):
                    if result_list:
                        results.append(result_list[0])
                    else:
                        results.append(FetchResult(
                            request_id=request_ids[i],
                            status="error",
                            status_code=0,
                            data=None,
                            error_msg="No result received"
                        ))
                else:
                    results.append(FetchResult(
                        request_id=request_ids[i],
                        status="timeout",
                        status_code=0,
                        data=None,
                        error_msg="Request timed out"
                    ))
            
            return results
            
        finally:
            # Cleanup all pending entries
            with self._lock:
                for request_id in request_ids:
                    self._pending.pop(request_id, None)


# Global instance management
_fetch_pool: Optional[FetchWorkerPool] = None
_fetch_lock = threading.Lock()


def get_fetch_worker_pool() -> FetchWorkerPool:
    """Get the global fetch worker pool (lazy initialized)."""
    global _fetch_pool
    
    with _fetch_lock:
        if _fetch_pool is None:
            _fetch_pool = FetchWorkerPool()
            _fetch_pool.start()
        return _fetch_pool


def shutdown_fetch_worker_pool() -> None:
    """Shutdown the global fetch worker pool."""
    global _fetch_pool
    
    with _fetch_lock:
        if _fetch_pool is not None:
            log.info("Shutting down fetch worker pool...")
            _fetch_pool.stop()
            _fetch_pool = None
            log.info("Fetch worker pool shutdown complete")


def _cleanup_on_exit() -> None:
    """Cleanup handler called on process exit."""
    try:
        shutdown_fetch_worker_pool()
    except Exception:
        pass


# Register cleanup handler to ensure workers are stopped on exit
atexit.register(_cleanup_on_exit)


# Convenience function
def fetch_url(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: Tuple[float, float] = (5, 20)) -> FetchResult:
    """
    Fetch a URL using the global worker pool.
    
    Crash-safe: if urllib3/SSL crashes, only the worker dies.
    """
    pool = get_fetch_worker_pool()
    return pool.fetch(url, headers, timeout)


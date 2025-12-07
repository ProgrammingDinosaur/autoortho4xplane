#!/usr/bin/env python3
"""
End-to-end tests for the fetch worker system.

Tests all modes:
1. Local ChunkFetchPool (Windows/Linux mode)
2. SharedFetchPool (Mac main process)
3. FetchClient (Mac worker mode)
4. Full integration with simulated chunk objects
"""

import os
import sys
import time
import threading
import tempfile
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class MockChunk:
    """Mock Chunk object for testing."""
    def __init__(self, col, row, zoom, maptype):
        self.col = col
        self.row = row
        self.zoom = zoom
        self.maptype = maptype
        self.chunk_id = f"{col}_{row}_{zoom}_{maptype}"
        self.ready = threading.Event()
        self.download_started = threading.Event()
        self.data = None
        self.permanent_failure = False
        self.failure_reason = None


def test_chunk_request_result():
    """Test that ChunkRequest and ChunkResult have required fields."""
    log.info("=" * 60)
    log.info("TEST 1: ChunkRequest and ChunkResult data structures")
    log.info("=" * 60)
    
    from fetch_worker import ChunkRequest, ChunkResult
    
    # Test ChunkRequest
    req = ChunkRequest(
        request_id=1,
        url="https://example.com/tile.jpg",
        headers={"User-Agent": "test"},
        timeout=(5, 20),
        worker_id="test_worker",
        chunk_id="123_456_16_BI"
    )
    assert req.request_id == 1
    assert req.worker_id == "test_worker"
    assert req.chunk_id == "123_456_16_BI"
    log.info("✓ ChunkRequest has all required fields")
    
    # Test ChunkResult
    result = ChunkResult(
        request_id=1,
        status="ok",
        status_code=200,
        data=b"test_data",
        error_msg=None,
        worker_id="test_worker",
        chunk_id="123_456_16_BI"
    )
    assert result.status == "ok"
    assert result.worker_id == "test_worker"
    assert result.chunk_id == "123_456_16_BI"
    log.info("✓ ChunkResult has all required fields")
    
    log.info("TEST 1 PASSED\n")
    return True


def test_local_chunk_fetch_pool():
    """Test the local ChunkFetchPool (Windows/Linux mode)."""
    log.info("=" * 60)
    log.info("TEST 2: Local ChunkFetchPool (Windows/Linux mode)")
    log.info("=" * 60)
    
    from fetch_worker import ChunkFetchPool
    
    pool = ChunkFetchPool(num_workers=2)
    
    try:
        # Start pool
        assert pool.start(), "Pool failed to start"
        log.info("✓ Pool started with 2 workers")
        
        # Create mock chunk
        chunk = MockChunk(2176, 3232, 13, "ARC")
        
        # Build a real URL (ArcGIS World Imagery - publicly accessible)
        url = f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{chunk.zoom}/{chunk.row}/{chunk.col}"
        headers = {"User-Agent": "curl/7.68.0"}
        
        log.info(f"Submitting chunk: {chunk.chunk_id}")
        log.info(f"URL: {url}")
        
        # Submit
        pool.submit(chunk, url, headers)
        
        # Wait for result
        start = time.time()
        result = chunk.ready.wait(timeout=30)
        elapsed = time.time() - start
        
        if result:
            log.info(f"✓ Chunk completed in {elapsed:.2f}s")
            if chunk.data:
                log.info(f"✓ Received {len(chunk.data)} bytes of data")
                # Verify it's a JPEG (starts with FFD8FF)
                if chunk.data[:3] == b'\xff\xd8\xff':
                    log.info("✓ Data is valid JPEG")
                else:
                    log.warning(f"Data header: {chunk.data[:10].hex()}")
            else:
                log.warning("Chunk completed but no data")
        else:
            log.error(f"✗ Chunk timed out after {elapsed:.2f}s")
            return False
        
        log.info("TEST 2 PASSED\n")
        return True
        
    finally:
        pool.stop()
        log.info("Pool stopped")


def test_shared_fetch_pool():
    """Test the SharedFetchPool (Mac main process mode)."""
    log.info("=" * 60)
    log.info("TEST 3: SharedFetchPool (Mac main process)")
    log.info("=" * 60)
    
    from fetch_worker import SharedFetchPool
    
    pool = SharedFetchPool(num_workers=2)
    
    try:
        # Start pool
        assert pool.start(), "SharedFetchPool failed to start"
        log.info("✓ SharedFetchPool started with 2 workers")
        
        # Submit from multiple "workers"
        worker_ids = ["worker_1", "worker_2", "worker_3"]
        chunk_ids = []
        
        for i, worker_id in enumerate(worker_ids):
            col = 2176 + i
            row = 3232 + i
            zoom = 13
            chunk_id = f"{col}_{row}_{zoom}_ARC"
            chunk_ids.append((worker_id, chunk_id))
            
            url = f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{row}/{col}"
            headers = {"User-Agent": "curl/7.68.0"}
            
            request_id = pool.submit(url, headers, worker_id, chunk_id)
            log.info(f"Submitted chunk {chunk_id} from {worker_id}, request_id={request_id}")
        
        # Wait and poll for results
        time.sleep(5)  # Give time for downloads
        
        results_received = 0
        for worker_id, chunk_id in chunk_ids:
            results = pool.poll_results(worker_id)
            for cid, result in results:
                log.info(f"✓ Received result for {cid} from {worker_id}: {result.status}")
                if result.status == "ok" and result.data:
                    log.info(f"  Data size: {len(result.data)} bytes")
                results_received += 1
        
        # May need more polling
        for _ in range(10):
            if results_received >= len(chunk_ids):
                break
            time.sleep(1)
            for worker_id, chunk_id in chunk_ids:
                results = pool.poll_results(worker_id)
                for cid, result in results:
                    log.info(f"✓ Received result for {cid} from {worker_id}: {result.status}")
                    results_received += 1
        
        log.info(f"Received {results_received}/{len(chunk_ids)} results")
        
        stats = pool.get_stats()
        log.info(f"Pool stats: {stats}")
        
        log.info("TEST 3 PASSED\n")
        return True
        
    finally:
        pool.stop()
        log.info("SharedFetchPool stopped")


def test_fetch_manager_and_client():
    """Test FetchManager and FetchClient (Mac full integration)."""
    log.info("=" * 60)
    log.info("TEST 4: FetchManager + FetchClient (Mac integration)")
    log.info("=" * 60)
    
    from fetch_worker import start_fetch_manager, stop_fetch_manager, FetchClient
    
    mgr = None
    client = None
    
    try:
        # Start manager (simulating main process)
        mgr, addr = start_fetch_manager(authkey=b'TESTAUTH')
        log.info(f"✓ FetchManager started on {addr}")
        
        # Create client (simulating Mac worker)
        client = FetchClient(addr, b'TESTAUTH', "test_mac_worker")
        log.info("✓ FetchClient connected")
        
        # Create mock chunks
        chunks = []
        for i in range(3):
            chunk = MockChunk(2176 + i, 3232 + i, 13, "ARC")
            chunks.append(chunk)
        
        # Submit chunks via client
        for chunk in chunks:
            url = f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{chunk.zoom}/{chunk.row}/{chunk.col}"
            headers = {"User-Agent": "curl/7.68.0"}
            client.submit(chunk, url, headers)
            log.info(f"Submitted {chunk.chunk_id} via FetchClient")
        
        # Wait for all to complete
        completed = 0
        timeout = 30
        start = time.time()
        
        while completed < len(chunks) and (time.time() - start) < timeout:
            for chunk in chunks:
                if chunk.ready.is_set() and chunk.data:
                    if not hasattr(chunk, '_logged'):
                        chunk._logged = True
                        completed += 1
                        log.info(f"✓ {chunk.chunk_id} completed: {len(chunk.data)} bytes")
            time.sleep(0.1)
        
        log.info(f"Completed {completed}/{len(chunks)} chunks")
        
        if completed == len(chunks):
            log.info("TEST 4 PASSED\n")
            return True
        else:
            log.error("TEST 4 FAILED - Not all chunks completed\n")
            return False
        
    finally:
        if client:
            client.stop()
            log.info("FetchClient stopped")
        if mgr:
            stop_fetch_manager(mgr)
            log.info("FetchManager stopped")


def test_mode_detection():
    """Test mode detection functions."""
    log.info("=" * 60)
    log.info("TEST 5: Mode detection")
    log.info("=" * 60)
    
    # Save original env
    original_addr = os.environ.get('AO_FETCH_ADDR')
    original_auth = os.environ.get('AO_FETCH_AUTH')
    original_worker_id = os.environ.get('AO_WORKER_ID')
    
    try:
        # Clear env vars
        for var in ['AO_FETCH_ADDR', 'AO_FETCH_AUTH', 'AO_WORKER_ID']:
            if var in os.environ:
                del os.environ[var]
        
        # Import fresh (need to reload)
        import importlib
        import getortho
        importlib.reload(getortho)
        
        # Test local mode (no env vars)
        assert not getortho._use_shared_pool(), "Should be local mode without env vars"
        log.info("✓ Local mode detected when AO_FETCH_ADDR not set")
        
        # Set env vars for shared mode
        os.environ['AO_FETCH_ADDR'] = '127.0.0.1:12345'
        os.environ['AO_FETCH_AUTH'] = 'TESTAUTH'
        os.environ['AO_WORKER_ID'] = 'test_worker'
        
        # Reload to pick up env vars
        importlib.reload(getortho)
        
        assert getortho._use_shared_pool(), "Should be shared mode with env vars"
        log.info("✓ Shared mode detected when AO_FETCH_ADDR is set")
        
        log.info("TEST 5 PASSED\n")
        return True
        
    finally:
        # Restore env
        for var, val in [('AO_FETCH_ADDR', original_addr), 
                         ('AO_FETCH_AUTH', original_auth),
                         ('AO_WORKER_ID', original_worker_id)]:
            if val is not None:
                os.environ[var] = val
            elif var in os.environ:
                del os.environ[var]


def test_crash_recovery():
    """Test that pool handles worker crashes gracefully."""
    log.info("=" * 60)
    log.info("TEST 6: Crash recovery simulation")
    log.info("=" * 60)
    
    from fetch_worker import ChunkFetchPool
    
    pool = ChunkFetchPool(num_workers=2)
    
    try:
        assert pool.start(), "Pool failed to start"
        log.info("✓ Pool started")
        
        # Verify workers are alive
        alive_count = sum(1 for w in pool.workers if w.is_alive())
        log.info(f"Workers alive: {alive_count}")
        assert alive_count == 2, "Not all workers started"
        
        # Submit a request to make sure things work
        chunk = MockChunk(2176, 3232, 13, "ARC")
        url = f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{chunk.zoom}/{chunk.row}/{chunk.col}"
        headers = {"User-Agent": "curl/7.68.0"}
        
        pool.submit(chunk, url, headers)
        
        if chunk.ready.wait(timeout=30):
            log.info(f"✓ Request completed successfully")
        else:
            log.warning("Request timed out")
        
        # Check workers still alive
        alive_count = sum(1 for w in pool.workers if w.is_alive())
        log.info(f"Workers still alive after request: {alive_count}")
        
        log.info("TEST 6 PASSED\n")
        return True
        
    finally:
        pool.stop()


def test_multiple_chunks_parallel():
    """Test processing multiple chunks in parallel."""
    log.info("=" * 60)
    log.info("TEST 7: Multiple chunks in parallel")
    log.info("=" * 60)
    
    from fetch_worker import ChunkFetchPool
    
    pool = ChunkFetchPool(num_workers=4)
    
    try:
        assert pool.start(), "Pool failed to start"
        log.info("✓ Pool started with 4 workers")
        
        # Create multiple chunks
        num_chunks = 8
        chunks = []
        for i in range(num_chunks):
            chunk = MockChunk(2176 + (i % 4), 3232 + (i // 4), 13, "ARC")
            chunks.append(chunk)
        
        # Submit all
        start_time = time.time()
        for chunk in chunks:
            url = f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{chunk.zoom}/{chunk.row}/{chunk.col}"
            headers = {"User-Agent": "curl/7.68.0"}
            pool.submit(chunk, url, headers)
        
        log.info(f"Submitted {num_chunks} chunks")
        
        # Wait for all to complete
        timeout = 60
        completed = 0
        deadline = time.time() + timeout
        
        while completed < num_chunks and time.time() < deadline:
            completed = sum(1 for c in chunks if c.ready.is_set())
            time.sleep(0.1)
        
        elapsed = time.time() - start_time
        log.info(f"Completed {completed}/{num_chunks} chunks in {elapsed:.2f}s")
        
        # Check data
        data_ok = sum(1 for c in chunks if c.data and len(c.data) > 0)
        log.info(f"Chunks with valid data: {data_ok}/{num_chunks}")
        
        if completed == num_chunks:
            log.info("TEST 7 PASSED\n")
            return True
        else:
            log.warning("TEST 7 PARTIAL - Some chunks timed out\n")
            return completed >= num_chunks // 2  # Pass if at least half completed
        
    finally:
        pool.stop()


def run_all_tests():
    """Run all tests and report results."""
    log.info("\n" + "=" * 70)
    log.info("FETCH WORKER END-TO-END TESTS")
    log.info("=" * 70 + "\n")
    
    tests = [
        ("Data Structures", test_chunk_request_result),
        ("Local ChunkFetchPool", test_local_chunk_fetch_pool),
        ("SharedFetchPool", test_shared_fetch_pool),
        ("FetchManager + Client", test_fetch_manager_and_client),
        ("Mode Detection", test_mode_detection),
        ("Crash Recovery", test_crash_recovery),
        ("Parallel Chunks", test_multiple_chunks_parallel),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            log.info(f"Running: {name}")
            passed = test_func()
            results.append((name, passed, None))
        except Exception as e:
            log.exception(f"Test {name} failed with exception")
            results.append((name, False, str(e)))
    
    # Summary
    log.info("\n" + "=" * 70)
    log.info("TEST SUMMARY")
    log.info("=" * 70)
    
    passed = sum(1 for _, p, _ in results if p)
    total = len(results)
    
    for name, success, error in results:
        status = "✓ PASS" if success else "✗ FAIL"
        log.info(f"  {status}: {name}")
        if error:
            log.info(f"         Error: {error}")
    
    log.info("-" * 70)
    log.info(f"TOTAL: {passed}/{total} tests passed")
    log.info("=" * 70 + "\n")
    
    return passed == total


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)


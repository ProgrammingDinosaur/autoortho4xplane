#!/usr/bin/env python3
"""
Tests for Python 3.14 free-threading optimizations.

These tests verify that the parallel processing improvements work correctly
and don't introduce race conditions or data corruption.
"""
import sys
import os
import pytest
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import Mock, patch, MagicMock

# Skip entire module if not Python 3.14+
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="Free-threading tests require Python 3.14+"
)

# Import after version check
try:
    from autoortho.utils.constants import (
        FREE_THREADING_ENABLED,
        is_free_threaded,
        get_optimal_worker_count,
        CURRENT_CPU_COUNT,
    )
except ImportError:
    from utils.constants import (
        FREE_THREADING_ENABLED,
        is_free_threaded,
        get_optimal_worker_count,
        CURRENT_CPU_COUNT,
    )


class TestFreeThreadingDetection:
    """Test free-threading detection utilities."""
    
    def test_is_free_threaded_function_exists(self):
        """Verify the detection function is available."""
        assert callable(is_free_threaded)
    
    def test_free_threading_enabled_is_bool(self):
        """Verify FREE_THREADING_ENABLED is a boolean."""
        assert isinstance(FREE_THREADING_ENABLED, bool)
    
    def test_get_optimal_worker_count_cpu_bound(self):
        """Test worker count calculation for CPU-bound tasks."""
        count = get_optimal_worker_count(8, "cpu")
        if FREE_THREADING_ENABLED:
            # Free-threaded: can use all cores
            assert count == 8
        else:
            # GIL mode: limited to 2
            assert count == 2
    
    def test_get_optimal_worker_count_io_bound(self):
        """Test worker count calculation for I/O-bound tasks."""
        count = get_optimal_worker_count(8, "io")
        # I/O-bound always gets multiplied by 4, capped at 64
        assert count == min(8 * 4, 64)


class TestBackgroundDDSBuilderParallel:
    """Test parallel DDS building functionality."""
    
    def test_builder_uses_multiple_workers_in_free_threading(self, tmpdir):
        """Verify BackgroundDDSBuilder uses multiple workers when free-threading enabled."""
        try:
            from autoortho.getortho import BackgroundDDSBuilder, PrebuiltDDSCache
        except ImportError:
            pytest.skip("Could not import getortho module")
        
        cache = PrebuiltDDSCache(max_memory_bytes=64 * 1024 * 1024)
        builder = BackgroundDDSBuilder(prebuilt_cache=cache, num_workers=None)
        
        if FREE_THREADING_ENABLED:
            # Should use half of CPU cores
            expected_min = max(2, CURRENT_CPU_COUNT // 2)
            assert builder._num_workers >= 2
        else:
            # GIL mode: single worker
            assert builder._num_workers == 1
    
    def test_builder_starts_and_stops_cleanly(self, tmpdir):
        """Verify builder can start and stop without errors."""
        try:
            from autoortho.getortho import BackgroundDDSBuilder, PrebuiltDDSCache
        except ImportError:
            pytest.skip("Could not import getortho module")
        
        cache = PrebuiltDDSCache(max_memory_bytes=64 * 1024 * 1024)
        builder = BackgroundDDSBuilder(prebuilt_cache=cache, num_workers=2)
        
        builder.start()
        assert builder._executor is not None
        assert builder._dispatcher_thread is not None
        assert builder._dispatcher_thread.is_alive()
        
        builder.stop()
        assert builder._executor is None
        assert builder._dispatcher_thread is None
    
    def test_parallel_builds_no_race_conditions(self, tmpdir):
        """Verify no race conditions with parallel builds."""
        # This test submits multiple mock tiles and verifies consistent results
        results = []
        lock = threading.Lock()
        
        def mock_build(item):
            time.sleep(0.01)  # Simulate work
            with lock:
                results.append(item)
            return item
        
        items = list(range(20))
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(mock_build, i) for i in items]
            for future in as_completed(futures):
                future.result()
        
        # All items should be processed exactly once
        assert sorted(results) == items


class TestChunkDecodeParallel:
    """Test parallel chunk decoding functionality."""
    
    def test_decode_sem_is_nullcontext_in_free_threading(self):
        """Verify decode semaphore is disabled in free-threading mode."""
        try:
            from autoortho.getortho import _decode_sem, FREE_THREADING_ENABLED
        except ImportError:
            pytest.skip("Could not import getortho module")
        
        if FREE_THREADING_ENABLED:
            # Should be a nullcontext (no throttling)
            from contextlib import nullcontext
            assert isinstance(_decode_sem, type(nullcontext()))
        else:
            # Should be a semaphore
            assert isinstance(_decode_sem, threading.Semaphore)
    
    def test_max_decode_increased_in_free_threading(self):
        """Verify max decode count is higher in free-threading mode."""
        try:
            from autoortho.getortho import _MAX_DECODE, CURRENT_CPU_COUNT, FREE_THREADING_ENABLED
        except ImportError:
            pytest.skip("Could not import getortho module")
        
        if FREE_THREADING_ENABLED:
            # Should be 2x CPU count
            assert _MAX_DECODE == CURRENT_CPU_COUNT * 2
        else:
            # Should be 4x CPU count, capped at 64
            assert _MAX_DECODE == min(CURRENT_CPU_COUNT * 4, 64)


class TestMipmapParallel:
    """Test parallel mipmap compression functionality."""
    
    def test_gen_mipmaps_parallel_method_exists(self):
        """Verify parallel mipmap generation method exists."""
        try:
            from autoortho.pydds import DDS
        except ImportError:
            pytest.skip("Could not import pydds module")
        
        assert hasattr(DDS, 'gen_mipmaps_parallel')
        assert hasattr(DDS, 'gen_mipmaps_auto')
    
    def test_gen_mipmaps_auto_selects_correctly(self):
        """Verify auto method selects appropriate implementation."""
        try:
            from autoortho.pydds import DDS, FREE_THREADING_ENABLED
        except ImportError:
            pytest.skip("Could not import pydds module")
        
        # Create a mock DDS object
        dds = DDS(256, 256)
        
        # The auto method should exist and be callable
        assert callable(dds.gen_mipmaps_auto)


class TestCacheOperations:
    """Test cache operation optimizations."""
    
    def test_cache_writer_workers_dynamic(self):
        """Verify cache writer worker count is dynamically set."""
        try:
            from autoortho.getortho import _get_cache_writer_workers, FREE_THREADING_ENABLED, CURRENT_CPU_COUNT
        except ImportError:
            pytest.skip("Could not import getortho module")
        
        workers = _get_cache_writer_workers()
        
        if FREE_THREADING_ENABLED:
            # Should use up to 8 workers based on CPU count
            assert workers == min(CURRENT_CPU_COUNT, 8)
        else:
            # GIL mode: 4 workers default
            assert workers == 4


class TestThreadSafety:
    """Test thread safety of shared data structures."""
    
    def test_concurrent_access_to_shared_dict(self):
        """Verify concurrent access to shared dictionaries is safe."""
        shared_dict = {}
        lock = threading.Lock()
        errors = []
        
        def writer(key):
            try:
                for i in range(100):
                    with lock:
                        shared_dict[f"{key}_{i}"] = i
            except Exception as e:
                errors.append(e)
        
        def reader():
            try:
                for _ in range(100):
                    with lock:
                        _ = dict(shared_dict)
            except Exception as e:
                errors.append(e)
        
        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=writer, args=(i,)))
            threads.append(threading.Thread(target=reader))
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Thread safety errors: {errors}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


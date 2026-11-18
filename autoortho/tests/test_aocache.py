"""
test_aocache.py - Unit tests for native cache I/O module

Tests the AoCache Python wrapper and validates:
- Batch file reading correctness
- JPEG signature validation
- Error handling for missing/corrupt files
- Performance comparison with Python fallback
"""

import os
import sys
import tempfile
import time
import pytest
from pathlib import Path
from typing import List

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_cache_result_copies_native_bytes_exactly():
    # get_bytes used to slice the ctypes pointer, which builds a list of ints
    # per file; string_at must return the same bytes.
    import ctypes
    from aopipeline.AoCache import CacheResult

    payload = bytes(range(256)) * 60
    buffer = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
    result = CacheResult()
    result.data = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))
    result.length = len(payload)
    result.success = 1
    assert result.get_bytes() == payload

    result.success = 0
    assert result.get_bytes() == b''


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def test_cache_dir():
    """Create a temporary cache directory with test JPEG files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def jpeg_files(test_cache_dir):
    """Create multiple test JPEG files in the cache directory."""
    # Minimal valid JPEG file (smallest valid JPEG)
    # This is a 1x1 pixel red JPEG
    MINIMAL_JPEG = bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
        0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
        0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD2, 0x8A, 0x28, 0x03, 0xFF, 0xD9
    ])
    
    paths = []
    for i in range(10):
        path = os.path.join(test_cache_dir, f"chunk_{i}.jpg")
        with open(path, 'wb') as f:
            f.write(MINIMAL_JPEG)
        paths.append(path)
    
    return paths


@pytest.fixture
def mixed_files(test_cache_dir):
    """Create a mix of valid, invalid, and missing files."""
    MINIMAL_JPEG = bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0xFF, 0xD9
    ])
    
    paths = []
    
    # Valid JPEG files
    for i in range(5):
        path = os.path.join(test_cache_dir, f"valid_{i}.jpg")
        with open(path, 'wb') as f:
            f.write(MINIMAL_JPEG)
        paths.append(path)
    
    # Invalid files (not JPEG)
    for i in range(3):
        path = os.path.join(test_cache_dir, f"invalid_{i}.jpg")
        with open(path, 'wb') as f:
            f.write(b"NOT A JPEG FILE")
        paths.append(path)
    
    # Missing files
    for i in range(2):
        path = os.path.join(test_cache_dir, f"missing_{i}.jpg")
        paths.append(path)  # Don't create the file
    
    return paths


# ============================================================================
# Python Fallback Implementation (for comparison)
# ============================================================================

def python_batch_read(paths: List[str]) -> List[tuple]:
    """Python-only batch file read for performance comparison."""
    results = []
    for path in paths:
        try:
            with open(path, 'rb') as f:
                data = f.read()
            # Validate JPEG signature
            if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
                results.append((data, True))
            else:
                results.append((b'', False))
        except (FileNotFoundError, PermissionError, OSError):
            results.append((b'', False))
    return results


# ============================================================================
# Test Cases
# ============================================================================

class TestAoCacheAvailability:
    """Test native library availability and fallback."""
    
    def test_import_aocache(self):
        """Test that AoCache module can be imported."""
        try:
            from autoortho.aopipeline import AoCache
            # Module loaded, check if native is available
            is_avail = AoCache.is_available()
            print(f"AoCache native library available: {is_avail}")
        except ImportError as e:
            pytest.skip(f"AoCache not available: {e}")
    
    def test_fallback_graceful(self):
        """Test that missing native library doesn't crash."""
        from autoortho.getortho import _get_native_cache, _batch_read_cache_files
        
        # Should not raise even if native not available
        native = _get_native_cache()
        # Either returns module or None
        assert native is None or hasattr(native, 'batch_read_cache')
        
        # Batch read should return empty dict if native not available
        result = _batch_read_cache_files([])
        assert result == {}


class TestBatchCacheRead:
    """Test batch cache reading functionality."""
    
    def test_batch_read_all_valid(self, jpeg_files):
        """Test reading multiple valid JPEG files."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            results = AoCache.batch_read_cache(jpeg_files)
            
            assert len(results) == len(jpeg_files)
            for data, success in results:
                assert success is True
                assert len(data) > 0
                # Verify JPEG signature
                assert data[0] == 0xFF
                assert data[1] == 0xD8
                assert data[2] == 0xFF
                
        except ImportError:
            pytest.skip("AoCache module not available")
    
    def test_batch_read_mixed(self, mixed_files):
        """Test reading a mix of valid, invalid, and missing files."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            results = AoCache.batch_read_cache(mixed_files)
            
            assert len(results) == len(mixed_files)
            
            # Count successes (should be 5 valid JPEGs)
            successes = sum(1 for _, success in results if success)
            assert successes == 5
            
            # Verify first 5 are valid
            for i in range(5):
                assert results[i][1] is True
            
            # Verify next 3 are invalid (not JPEG)
            for i in range(5, 8):
                assert results[i][1] is False
            
            # Verify last 2 are missing
            for i in range(8, 10):
                assert results[i][1] is False
                
        except ImportError:
            pytest.skip("AoCache module not available")
    
    def test_batch_read_detailed(self, mixed_files):
        """Test detailed batch read with error messages."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            results = AoCache.batch_read_cache_detailed(mixed_files)
            
            assert len(results) == len(mixed_files)
            
            # Check structure of results
            for result in results:
                assert hasattr(result, 'data')
                assert hasattr(result, 'success')
                assert hasattr(result, 'error')
            
            # Invalid/missing files should have error messages
            for i in range(5, 10):
                if not results[i].success:
                    assert len(results[i].error) > 0
                    
        except ImportError:
            pytest.skip("AoCache module not available")
    
    def test_batch_read_empty(self):
        """Test reading empty path list."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            results = AoCache.batch_read_cache([])
            assert results == []
            
        except ImportError:
            pytest.skip("AoCache module not available")


class TestJpegValidation:
    """Test JPEG signature validation."""
    
    def test_validate_jpegs(self, mixed_files):
        """Test fast JPEG header validation."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            valid_flags = AoCache.validate_jpegs(mixed_files)
            
            assert len(valid_flags) == len(mixed_files)
            
            # First 5 should be valid
            for i in range(5):
                assert valid_flags[i] is True
            
            # Rest should be invalid
            for i in range(5, 10):
                assert valid_flags[i] is False
                
        except ImportError:
            pytest.skip("AoCache module not available")


class TestFileOperations:
    """Test single-file operations."""
    
    def test_file_exists(self, jpeg_files):
        """Test file existence check."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            # Existing file
            assert AoCache.file_exists(jpeg_files[0]) is True
            
            # Non-existent file
            assert AoCache.file_exists("/nonexistent/path.jpg") is False
            
        except ImportError:
            pytest.skip("AoCache module not available")
    
    def test_file_size(self, jpeg_files):
        """Test file size retrieval."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            size = AoCache.file_size(jpeg_files[0])
            assert size > 0
            
            # Verify matches actual size
            actual_size = os.path.getsize(jpeg_files[0])
            assert size == actual_size
            
            # Non-existent file
            assert AoCache.file_size("/nonexistent/path.jpg") == -1
            
        except ImportError:
            pytest.skip("AoCache module not available")
    
    def test_write_file_atomic(self, test_cache_dir):
        """Test atomic file writing."""
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                pytest.skip("Native library not available")
            
            path = os.path.join(test_cache_dir, "test_write.bin")
            data = b"Test data for atomic write"
            
            result = AoCache.write_file_atomic(path, data)
            assert result is True
            
            # Verify file contents
            with open(path, 'rb') as f:
                written_data = f.read()
            assert written_data == data
            
        except ImportError:
            pytest.skip("AoCache module not available")


class TestPerformance:
    """Performance benchmarks comparing native vs Python."""
    
    def test_benchmark_batch_read(self, test_cache_dir):
        """Benchmark native vs Python batch reading."""
        # Create more files for meaningful benchmark
        MINIMAL_JPEG = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0xFF, 0xD9
        ])
        
        NUM_FILES = 256
        paths = []
        for i in range(NUM_FILES):
            path = os.path.join(test_cache_dir, f"bench_{i}.jpg")
            with open(path, 'wb') as f:
                f.write(MINIMAL_JPEG)
            paths.append(path)
        
        # Python benchmark
        start = time.perf_counter()
        python_results = python_batch_read(paths)
        python_time = time.perf_counter() - start
        
        # Native benchmark (if available)
        try:
            from autoortho.aopipeline import AoCache
            if not AoCache.is_available():
                print(f"\nPython batch read: {python_time*1000:.2f}ms for {NUM_FILES} files")
                pytest.skip("Native library not available for comparison")
            
            # Warmup
            AoCache.batch_read_cache(paths[:10])
            
            start = time.perf_counter()
            native_results = AoCache.batch_read_cache(paths)
            native_time = time.perf_counter() - start
            
            # Verify correctness
            assert len(native_results) == len(python_results)
            for (n_data, n_success), (p_data, p_success) in zip(native_results, python_results):
                assert n_success == p_success
                if n_success:
                    assert n_data == p_data
            
            speedup = python_time / native_time if native_time > 0 else float('inf')
            
            print(f"\n{'='*60}")
            print(f"Batch Read Benchmark ({NUM_FILES} files)")
            print(f"{'='*60}")
            print(f"Python:  {python_time*1000:.2f}ms")
            print(f"Native:  {native_time*1000:.2f}ms")
            print(f"Speedup: {speedup:.1f}x")
            print(f"{'='*60}")
            
            # Native should be faster (allow some margin for small file tests)
            # In real scenarios with larger files and more I/O, speedup is higher
            assert native_time <= python_time * 2, "Native should not be significantly slower"
            
        except ImportError:
            print(f"\nPython batch read: {python_time*1000:.2f}ms for {NUM_FILES} files")
            pytest.skip("AoCache module not available")


class TestIntegration:
    """Integration tests with getortho module."""
    
    def test_getortho_batch_read_helper(self, jpeg_files):
        """Test the _batch_read_cache_files helper in getortho."""
        from autoortho.getortho import _batch_read_cache_files
        
        result = _batch_read_cache_files(jpeg_files)
        
        # If native is available, should return dict of cached data
        # If not available, should return empty dict
        assert isinstance(result, dict)
        
        if result:  # Native available
            for path in jpeg_files:
                if path in result:
                    data = result[path]
                    assert len(data) > 0
                    assert data[0] == 0xFF  # JPEG signature
    
    def test_getortho_native_cache_getter(self):
        """Test the _get_native_cache lazy loader."""
        from autoortho.getortho import _get_native_cache
        
        # Should not raise
        native = _get_native_cache()
        
        # Should be consistent (cached)
        native2 = _get_native_cache()
        assert native is native2


# ============================================================================
# Main
# ============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])


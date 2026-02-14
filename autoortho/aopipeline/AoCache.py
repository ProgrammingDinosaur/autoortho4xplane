"""
AoCache.py - Python wrapper for native parallel cache I/O

Provides high-performance batch file reading that bypasses Python's GIL
by delegating to native C code with OpenMP parallelism.

Usage:
    from autoortho.aopipeline import AoCache
    
    # Batch read multiple cache files
    results = AoCache.batch_read_cache(["/path/to/a.jpg", "/path/to/b.jpg"])
    for data, success in results:
        if success:
            # data is bytes containing JPEG data
            process(data)
    
    # Or with error details:
    results = AoCache.batch_read_cache_detailed(paths)
    for result in results:
        if result.success:
            jpeg_bytes = result.data
"""

from ctypes import (
    CDLL, POINTER, Structure,
    c_char, c_char_p, c_int32, c_int64, c_uint8, c_uint32,
    cast
)
import logging
import os
import sys
from typing import List, Tuple, Optional, NamedTuple

log = logging.getLogger(__name__)

# ============================================================================
# Library Loading
# ============================================================================

_aocache = None
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
    global _aocache, _load_error
    
    if _aocache is not None:
        return _aocache
    
    if _load_error is not None:
        raise _load_error
    
    try:
        lib_path = _get_lib_path()
        log.debug(f"Loading aocache native library from: {lib_path}")
        
        # Windows: Add DLL directory to search path (Python 3.8+)
        if sys.platform == 'win32':
            lib_dir = os.path.dirname(lib_path)
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(lib_dir)
        
        _aocache = CDLL(lib_path)
        
        # Configure function signatures
        _setup_signatures(_aocache)
        
        # Log version info
        version = _aocache.aocache_version()
        log.info(f"Loaded native cache library: {version.decode()}")
        
        return _aocache
        
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
            _load_error = ImportError(f"Failed to load aocache native library: {e}")
        
        log.warning(f"Native cache library not available: {e}")
        raise _load_error
        
    except Exception as e:
        _load_error = ImportError(f"Failed to load aocache native library: {e}")
        log.warning(f"Native cache library not available: {e}")
        raise _load_error


def _setup_signatures(lib):
    """Configure ctypes function signatures for type safety."""
    
    # aocache_batch_read
    lib.aocache_batch_read.argtypes = [
        POINTER(c_char_p),  # paths
        c_int32,            # count
        POINTER(CacheResult),  # results
        c_int32             # max_threads
    ]
    lib.aocache_batch_read.restype = c_int32
    
    # aocache_batch_read_raw
    lib.aocache_batch_read_raw.argtypes = [
        POINTER(c_char_p),
        c_int32,
        POINTER(CacheResult),
        c_int32
    ]
    lib.aocache_batch_read_raw.restype = c_int32
    
    # aocache_batch_free
    lib.aocache_batch_free.argtypes = [POINTER(CacheResult), c_int32]
    lib.aocache_batch_free.restype = None
    
    # aocache_validate_jpegs
    lib.aocache_validate_jpegs.argtypes = [
        POINTER(c_char_p),
        c_int32,
        POINTER(c_int32),
        c_int32
    ]
    lib.aocache_validate_jpegs.restype = c_int32
    
    # aocache_file_exists
    lib.aocache_file_exists.argtypes = [c_char_p]
    lib.aocache_file_exists.restype = c_int32
    
    # aocache_file_size
    lib.aocache_file_size.argtypes = [c_char_p]
    lib.aocache_file_size.restype = c_int64
    
    # aocache_read_file
    lib.aocache_read_file.argtypes = [
        c_char_p,
        POINTER(POINTER(c_uint8)),
        POINTER(c_uint32)
    ]
    lib.aocache_read_file.restype = c_int32
    
    # aocache_write_file_atomic
    lib.aocache_write_file_atomic.argtypes = [
        c_char_p,
        POINTER(c_uint8),
        c_uint32
    ]
    lib.aocache_write_file_atomic.restype = c_int32
    
    # aocache_version
    lib.aocache_version.argtypes = []
    lib.aocache_version.restype = c_char_p
    
    # aocache_warmup_threads
    lib.aocache_warmup_threads.argtypes = [c_int32]
    lib.aocache_warmup_threads.restype = None


# ============================================================================
# Data Structures
# ============================================================================

class CacheResult(Structure):
    """
    Result structure for batch cache read operations.
    Maps to aocache_result_t in C.
    """
    _fields_ = [
        ('data', POINTER(c_uint8)),
        ('length', c_uint32),
        ('success', c_int32),
        ('error', c_char * 64),
    ]
    
    def get_bytes(self) -> bytes:
        """Get data as Python bytes (copies data)."""
        if not self.success or not self.data:
            return b''
        return bytes(self.data[:self.length])
    
    def get_error(self) -> str:
        """Get error message if failed."""
        if self.success:
            return ''
        return self.error.decode('utf-8', errors='replace').rstrip('\x00')


class CacheReadResult(NamedTuple):
    """Python-friendly result from batch cache read."""
    data: bytes
    success: bool
    error: str = ''


# ============================================================================
# Public API
# ============================================================================

def batch_read_cache(
    paths: List[str], 
    max_threads: int = 0,
    validate_jpeg: bool = True
) -> List[Tuple[bytes, bool]]:
    """
    Read multiple cache files in parallel using native code.
    
    This function reads all specified files concurrently using OpenMP,
    bypassing Python's GIL for true parallelism.
    
    Args:
        paths: List of file paths to read
        max_threads: Maximum parallel threads (0 = auto-detect from CPU cores)
        validate_jpeg: If True, validate JPEG signature (FFD8FF)
    
    Returns:
        List of (data_bytes, success) tuples in same order as input paths.
        - data_bytes: File contents as bytes (empty if failed)
        - success: True if file was read successfully
    
    Example:
        paths = ["/cache/a.jpg", "/cache/b.jpg", "/cache/c.jpg"]
        results = batch_read_cache(paths)
        for path, (data, ok) in zip(paths, results):
            if ok:
                print(f"Read {len(data)} bytes from {path}")
            else:
                print(f"Failed to read {path}")
    """
    if not paths:
        return []
    
    lib = _load_library()
    count = len(paths)
    
    # Create path array for C
    path_array = (c_char_p * count)()
    for i, path in enumerate(paths):
        if isinstance(path, str):
            path_array[i] = path.encode('utf-8')
        else:
            path_array[i] = path
    
    # Allocate results array
    results = (CacheResult * count)()
    
    # Call native function
    if validate_jpeg:
        lib.aocache_batch_read(path_array, count, results, max_threads)
    else:
        lib.aocache_batch_read_raw(path_array, count, results, max_threads)
    
    # Extract results to Python objects
    output = []
    for i in range(count):
        if results[i].success:
            data = results[i].get_bytes()
            output.append((data, True))
        else:
            output.append((b'', False))
    
    # Free native memory
    lib.aocache_batch_free(results, count)
    
    return output


def batch_read_cache_detailed(
    paths: List[str],
    max_threads: int = 0,
    validate_jpeg: bool = True
) -> List[CacheReadResult]:
    """
    Read multiple cache files with detailed error information.
    
    Same as batch_read_cache but includes error messages for failed reads.
    
    Args:
        paths: List of file paths to read
        max_threads: Maximum parallel threads (0 = auto)
        validate_jpeg: If True, validate JPEG signature
    
    Returns:
        List of CacheReadResult namedtuples with data, success, and error fields.
    """
    if not paths:
        return []
    
    lib = _load_library()
    count = len(paths)
    
    path_array = (c_char_p * count)()
    for i, path in enumerate(paths):
        if isinstance(path, str):
            path_array[i] = path.encode('utf-8')
        else:
            path_array[i] = path
    
    results = (CacheResult * count)()
    
    if validate_jpeg:
        lib.aocache_batch_read(path_array, count, results, max_threads)
    else:
        lib.aocache_batch_read_raw(path_array, count, results, max_threads)
    
    output = []
    for i in range(count):
        result = CacheReadResult(
            data=results[i].get_bytes(),
            success=bool(results[i].success),
            error=results[i].get_error()
        )
        output.append(result)
    
    lib.aocache_batch_free(results, count)
    return output


def validate_jpegs(
    paths: List[str],
    max_threads: int = 0
) -> List[bool]:
    """
    Fast validation of JPEG headers without reading full files.
    
    This reads only the first 3 bytes of each file to check for the
    JPEG signature (FFD8FF). Much faster than full file reads for
    pre-filtering cache entries.
    
    Args:
        paths: List of file paths to validate
        max_threads: Maximum parallel threads (0 = auto)
    
    Returns:
        List of booleans indicating whether each file is a valid JPEG.
    """
    if not paths:
        return []
    
    lib = _load_library()
    count = len(paths)
    
    path_array = (c_char_p * count)()
    for i, path in enumerate(paths):
        if isinstance(path, str):
            path_array[i] = path.encode('utf-8')
        else:
            path_array[i] = path
    
    valid_flags = (c_int32 * count)()
    lib.aocache_validate_jpegs(path_array, count, valid_flags, max_threads)
    
    return [bool(valid_flags[i]) for i in range(count)]


def file_exists(path: str) -> bool:
    """
    Check if a cache file exists and is readable.
    
    Args:
        path: File path to check
    
    Returns:
        True if file exists and is a regular file.
    """
    lib = _load_library()
    if isinstance(path, str):
        path = path.encode('utf-8')
    return bool(lib.aocache_file_exists(path))


def file_size(path: str) -> int:
    """
    Get the size of a cache file in bytes.
    
    Args:
        path: File path
    
    Returns:
        File size in bytes, or -1 if file doesn't exist.
    """
    lib = _load_library()
    if isinstance(path, str):
        path = path.encode('utf-8')
    return lib.aocache_file_size(path)


def read_file(path: str) -> Optional[bytes]:
    """
    Read a single cache file.
    
    Convenience function for reading one file when batch operations
    aren't needed.
    
    Note: This uses the batch API internally to ensure proper memory
    management (no memory leaks).
    
    Args:
        path: File path to read
    
    Returns:
        File contents as bytes, or None if read failed.
    """
    # Use batch_read_cache for proper memory management
    # This avoids the memory leak in the single-file native API
    results = batch_read_cache([path], validate_jpeg=False)
    if results and results[0][1]:  # (data, success)
        return results[0][0]
    return None


def write_file_atomic(path: str, data: bytes) -> bool:
    """
    Write data to a cache file atomically.
    
    Uses atomic write pattern (write to temp, then rename) to prevent
    partial/corrupt files.
    
    Args:
        path: Destination file path
        data: Data to write
    
    Returns:
        True on success, False on failure.
    """
    lib = _load_library()
    
    if isinstance(path, str):
        path = path.encode('utf-8')
    
    data_array = (c_uint8 * len(data)).from_buffer_copy(data)
    return bool(lib.aocache_write_file_atomic(
        path, 
        cast(data_array, POINTER(c_uint8)), 
        len(data)
    ))


def get_version() -> str:
    """Get version information for the native cache library."""
    lib = _load_library()
    return lib.aocache_version().decode('utf-8')


def warmup_threads(num_threads: int = 0) -> None:
    """
    Warm up the thread pool to avoid creation overhead on first use.
    
    Call this early in application startup to pre-create the OpenMP
    thread pool. This eliminates the ~2ms thread creation penalty
    on the first batch read.
    
    Args:
        num_threads: Number of threads (0 = auto-detect from CPU cores)
    """
    lib = _load_library()
    lib.aocache_warmup_threads(num_threads)


def is_available() -> bool:
    """Check if the native cache library is available."""
    try:
        _load_library()
        return True
    except (ImportError, FileNotFoundError):
        return False


# ============================================================================
# Module initialization
# ============================================================================

# Try to load library on import and warm up thread pool
try:
    _load_library()
    # Warm up thread pool immediately to avoid penalty on first use
    warmup_threads(0)
except Exception:
    pass  # Will raise on first use if not available


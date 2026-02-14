"""
AoDecode.py - Python wrapper for native parallel JPEG decoding

Provides high-performance batch JPEG decoding that bypasses Python's GIL
by delegating to native C code with OpenMP parallelism and libturbojpeg.

Usage:
    from autoortho.aopipeline import AoDecode
    
    # Create buffer pool for efficient memory reuse
    pool = AoDecode.create_pool(256)
    
    # Batch decode multiple JPEGs
    jpeg_datas = [...]  # List of JPEG bytes
    images = AoDecode.batch_decode(jpeg_datas, pool)
    
    for image in images:
        if image.is_valid:
            # Access raw RGBA data
            rgba_data = image.data
            width, height = image.width, image.height
"""

from ctypes import (
    CDLL, POINTER, Structure, c_void_p,
    c_char, c_char_p, c_int32, c_int64, c_uint8, c_uint32,
    byref, cast
)
import logging
import os
import sys
from typing import List, Optional, NamedTuple

log = logging.getLogger(__name__)

# ============================================================================
# Library Loading
# ============================================================================

_aodecode = None
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
    global _aodecode, _load_error
    
    if _aodecode is not None:
        return _aodecode
    
    if _load_error is not None:
        raise _load_error
    
    try:
        lib_path = _get_lib_path()
        log.debug(f"Loading aodecode native library from: {lib_path}")
        
        # Windows: Add DLL directory to search path (Python 3.8+)
        if sys.platform == 'win32':
            lib_dir = os.path.dirname(lib_path)
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(lib_dir)
        
        _aodecode = CDLL(lib_path)
        
        # Configure function signatures
        _setup_signatures(_aodecode)
        
        # Log version info
        version = _aodecode.aodecode_version()
        log.info(f"Loaded native decode library: {version.decode()}")
        
        return _aodecode
        
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
            _load_error = ImportError(f"Failed to load aodecode native library: {e}")
        
        log.warning(f"Native decode library not available: {e}")
        raise _load_error
        
    except Exception as e:
        _load_error = ImportError(f"Failed to load aodecode native library: {e}")
        log.warning(f"Native decode library not available: {e}")
        raise _load_error


def _setup_signatures(lib):
    """Configure ctypes function signatures for type safety."""
    
    # Pool functions
    lib.aodecode_create_pool.argtypes = [c_int32]
    lib.aodecode_create_pool.restype = c_void_p
    
    lib.aodecode_destroy_pool.argtypes = [c_void_p]
    lib.aodecode_destroy_pool.restype = None
    
    lib.aodecode_acquire_buffer.argtypes = [c_void_p]
    lib.aodecode_acquire_buffer.restype = POINTER(c_uint8)
    
    lib.aodecode_release_buffer.argtypes = [c_void_p, POINTER(c_uint8)]
    lib.aodecode_release_buffer.restype = None
    
    lib.aodecode_pool_stats.argtypes = [
        c_void_p, POINTER(c_int32), POINTER(c_int32), POINTER(c_int32)
    ]
    lib.aodecode_pool_stats.restype = None
    
    # Extended pool functions
    lib.aodecode_create_pool_ex.argtypes = [c_int32, c_int64]
    lib.aodecode_create_pool_ex.restype = c_void_p
    
    lib.aodecode_pool_set_limit.argtypes = [c_void_p, c_int64]
    lib.aodecode_pool_set_limit.restype = None
    
    lib.aodecode_pool_get_limit.argtypes = [c_void_p]
    lib.aodecode_pool_get_limit.restype = c_int64
    
    lib.aodecode_pool_get_usage.argtypes = [c_void_p]
    lib.aodecode_pool_get_usage.restype = c_int64
    
    lib.aodecode_pool_stats_ex.argtypes = [
        c_void_p, POINTER(c_int32), POINTER(c_int32), POINTER(c_int32),
        POINTER(c_int32), POINTER(c_int64), POINTER(c_int64)
    ]
    lib.aodecode_pool_stats_ex.restype = None
    
    # Batch decode
    lib.aodecode_batch.argtypes = [
        POINTER(DecodeRequest),
        c_int32,
        c_void_p,
        c_int32
    ]
    lib.aodecode_batch.restype = c_int32
    
    # From cache
    lib.aodecode_from_cache.argtypes = [
        POINTER(c_char_p),
        c_int32,
        POINTER(DecodedImage),
        c_void_p,
        c_int32
    ]
    lib.aodecode_from_cache.restype = c_int32
    
    # Single decode
    lib.aodecode_single.argtypes = [
        POINTER(c_uint8),
        c_uint32,
        POINTER(DecodedImage),
        c_void_p
    ]
    lib.aodecode_single.restype = c_int32
    
    # Free image
    lib.aodecode_free_image.argtypes = [POINTER(DecodedImage), c_void_p]
    lib.aodecode_free_image.restype = None
    
    # Version
    lib.aodecode_version.argtypes = []
    lib.aodecode_version.restype = c_char_p
    
    # Persistent decoder management
    lib.aodecode_init_persistent_decoders.argtypes = []
    lib.aodecode_init_persistent_decoders.restype = None
    
    lib.aodecode_cleanup_persistent_decoders.argtypes = []
    lib.aodecode_cleanup_persistent_decoders.restype = None
    
    # Full warmup
    lib.aodecode_warmup_full.argtypes = [c_void_p]
    lib.aodecode_warmup_full.restype = None


# ============================================================================
# Data Structures
# ============================================================================

class DecodedImage(Structure):
    """
    Decoded image structure.
    Maps to aodecode_image_t in C.
    """
    _fields_ = [
        ('data', POINTER(c_uint8)),
        ('width', c_int32),
        ('height', c_int32),
        ('stride', c_int32),
        ('channels', c_int32),
        ('from_pool', c_int32),
    ]
    
    @property
    def is_valid(self) -> bool:
        """Check if image was successfully decoded."""
        return self.data is not None and self.width > 0 and self.height > 0
    
    def get_bytes(self) -> bytes:
        """Get RGBA data as Python bytes (copies data)."""
        if not self.is_valid:
            return b''
        size = self.height * self.stride
        return bytes(self.data[:size])


class DecodeRequest(Structure):
    """
    Decode request structure for batch operations.
    Maps to aodecode_request_t in C.
    """
    _fields_ = [
        ('jpeg_data', POINTER(c_uint8)),
        ('jpeg_length', c_uint32),
        ('image', DecodedImage),
        ('success', c_int32),
        ('error', c_char * 64),
    ]
    
    def get_error(self) -> str:
        """Get error message if failed."""
        if self.success:
            return ''
        return self.error.decode('utf-8', errors='replace').rstrip('\x00')


class PoolStats(NamedTuple):
    """Statistics for a buffer pool."""
    total: int
    available: int
    acquired: int


# ============================================================================
# Buffer Pool
# ============================================================================

class BufferPool:
    """
    Wrapper for native buffer pool.
    
    Pre-allocates RGBA buffers for efficient chunk decoding.
    Supports memory limits with overflow buffers and wait-queue.
    """
    
    def __init__(self, count: int, memory_limit: int = 0):
        """
        Create a buffer pool.
        
        Args:
            count: Number of 256x256 RGBA buffers to pre-allocate
            memory_limit: Maximum memory usage in bytes (0 = unlimited)
        """
        lib = _load_library()
        if memory_limit > 0:
            self._handle = lib.aodecode_create_pool_ex(count, memory_limit)
        else:
            self._handle = lib.aodecode_create_pool(count)
        if not self._handle:
            raise MemoryError(f"Failed to create buffer pool with {count} buffers")
        self._lib = lib
    
    def __del__(self):
        self.destroy()
    
    def destroy(self):
        """Destroy the pool and free all memory."""
        if hasattr(self, '_handle') and self._handle:
            self._lib.aodecode_destroy_pool(self._handle)
            self._handle = None
    
    @property
    def handle(self) -> c_void_p:
        """Get native pool handle."""
        return self._handle
    
    def set_memory_limit(self, limit_bytes: int):
        """Set the memory limit for overflow buffers."""
        self._lib.aodecode_pool_set_limit(self._handle, limit_bytes)
    
    def get_memory_limit(self) -> int:
        """Get the current memory limit."""
        return self._lib.aodecode_pool_get_limit(self._handle)
    
    def get_memory_usage(self) -> int:
        """Get current memory usage (fixed + overflow)."""
        return self._lib.aodecode_pool_get_usage(self._handle)
    
    def stats(self) -> PoolStats:
        """Get pool statistics."""
        total = c_int32()
        available = c_int32()
        acquired = c_int32()
        self._lib.aodecode_pool_stats(
            self._handle, byref(total), byref(available), byref(acquired)
        )
        return PoolStats(total.value, available.value, acquired.value)
    
    def stats_ex(self) -> dict:
        """Get extended pool statistics including overflow tracking."""
        total = c_int32()
        available = c_int32()
        acquired = c_int32()
        overflow_count = c_int32()
        overflow_bytes = c_int64()
        memory_limit = c_int64()
        self._lib.aodecode_pool_stats_ex(
            self._handle, byref(total), byref(available), byref(acquired),
            byref(overflow_count), byref(overflow_bytes), byref(memory_limit)
        )
        return {
            'total': total.value,
            'available': available.value,
            'acquired': acquired.value,
            'overflow_count': overflow_count.value,
            'overflow_bytes': overflow_bytes.value,
            'memory_limit': memory_limit.value,
        }
    
    def __repr__(self) -> str:
        s = self.stats()
        return f"BufferPool(total={s.total}, available={s.available}, acquired={s.acquired})"


# ============================================================================
# Public API
# ============================================================================

def create_pool(count: int = 256, memory_limit: int = 0) -> BufferPool:
    """
    Create a buffer pool for efficient memory reuse.
    
    Args:
        count: Number of 256x256 RGBA buffers to pre-allocate
        memory_limit: Maximum memory usage in bytes (0 = unlimited)
    
    Returns:
        BufferPool instance
    """
    return BufferPool(count, memory_limit)


def batch_decode(
    jpeg_datas: List[bytes],
    pool: Optional[BufferPool] = None,
    max_threads: int = 0
) -> List[DecodedImage]:
    """
    Decode multiple JPEGs in parallel using native code.
    
    Args:
        jpeg_datas: List of JPEG data as bytes
        pool: Optional buffer pool for output images
        max_threads: Maximum parallel threads (0 = auto)
    
    Returns:
        List of DecodedImage structures. Check .is_valid to determine success.
    
    Note: Caller is responsible for freeing images via free_images().
    """
    if not jpeg_datas:
        return []
    
    lib = _load_library()
    count = len(jpeg_datas)
    
    # Create request array
    requests = (DecodeRequest * count)()
    
    # Keep references to data arrays to prevent garbage collection
    data_arrays = []
    
    for i, jpeg_bytes in enumerate(jpeg_datas):
        if jpeg_bytes:
            data_array = (c_uint8 * len(jpeg_bytes)).from_buffer_copy(jpeg_bytes)
            data_arrays.append(data_array)
            requests[i].jpeg_data = cast(data_array, POINTER(c_uint8))
            requests[i].jpeg_length = len(jpeg_bytes)
        else:
            requests[i].jpeg_data = None
            requests[i].jpeg_length = 0
    
    # Call native batch decode
    pool_handle = pool.handle if pool else None
    lib.aodecode_batch(requests, count, pool_handle, max_threads)
    
    # Extract results
    return [requests[i].image for i in range(count)]


def decode_from_cache(
    cache_paths: List[str],
    pool: Optional[BufferPool] = None,
    max_threads: int = 0
) -> List[DecodedImage]:
    """
    Read cache files and decode JPEGs in one native call.
    
    This is the optimal path - all I/O and decoding happens in native
    code without Python involvement.
    
    Args:
        cache_paths: List of cache file paths
        pool: Optional buffer pool
        max_threads: Maximum parallel threads (0 = auto)
    
    Returns:
        List of DecodedImage structures. Check .is_valid to determine success.
    """
    if not cache_paths:
        return []
    
    lib = _load_library()
    count = len(cache_paths)
    
    # Create path array
    path_array = (c_char_p * count)()
    for i, path in enumerate(cache_paths):
        if isinstance(path, str):
            path_array[i] = path.encode('utf-8')
        else:
            path_array[i] = path
    
    # Create output array
    images = (DecodedImage * count)()
    
    # Call native function
    pool_handle = pool.handle if pool else None
    lib.aodecode_from_cache(path_array, count, images, pool_handle, max_threads)
    
    return list(images)


def decode_single(
    jpeg_data: bytes,
    pool: Optional[BufferPool] = None
) -> Optional[DecodedImage]:
    """
    Decode a single JPEG.
    
    Args:
        jpeg_data: JPEG data as bytes
        pool: Optional buffer pool
    
    Returns:
        DecodedImage if successful, None otherwise
    """
    if not jpeg_data:
        return None
    
    lib = _load_library()
    
    data_array = (c_uint8 * len(jpeg_data)).from_buffer_copy(jpeg_data)
    image = DecodedImage()
    
    pool_handle = pool.handle if pool else None
    if lib.aodecode_single(
        cast(data_array, POINTER(c_uint8)),
        len(jpeg_data),
        byref(image),
        pool_handle
    ):
        return image
    
    return None


def free_image(image: DecodedImage, pool: Optional[BufferPool] = None):
    """
    Free a decoded image.
    
    Args:
        image: Image to free
        pool: Pool the image may have come from
    """
    lib = _load_library()
    pool_handle = pool.handle if pool else None
    lib.aodecode_free_image(byref(image), pool_handle)


def free_images(images: List[DecodedImage], pool: Optional[BufferPool] = None):
    """
    Free multiple decoded images.
    
    Args:
        images: List of images to free
        pool: Pool the images may have come from
    """
    lib = _load_library()
    pool_handle = pool.handle if pool else None
    for image in images:
        lib.aodecode_free_image(byref(image), pool_handle)


def get_version() -> str:
    """Get version information for the native decode library."""
    lib = _load_library()
    return lib.aodecode_version().decode('utf-8')


def is_available() -> bool:
    """Check if the native decode library is available."""
    try:
        _load_library()
        return True
    except (ImportError, FileNotFoundError):
        return False


def init_persistent_decoders() -> bool:
    """
    Initialize persistent TurboJPEG decoder handles.
    
    Creates one decompressor per OpenMP thread for reuse across decode calls,
    eliminating the ~0.15ms overhead of handle creation in each parallel loop.
    
    Returns:
        True if initialization succeeded, False otherwise
    """
    try:
        lib = _load_library()
        lib.aodecode_init_persistent_decoders()
        return True
    except Exception as e:
        log.debug(f"Failed to init persistent decoders: {e}")
        return False


def cleanup_persistent_decoders() -> bool:
    """
    Cleanup persistent TurboJPEG decoder handles.
    
    Call during application shutdown to release resources.
    
    Returns:
        True if cleanup succeeded, False otherwise
    """
    try:
        lib = _load_library()
        lib.aodecode_cleanup_persistent_decoders()
        return True
    except Exception as e:
        log.debug(f"Failed to cleanup persistent decoders: {e}")
        return False


def warmup_full(pool: Optional[BufferPool] = None) -> bool:
    """
    Pre-warm the decode pipeline for optimal first-tile performance.
    
    Initializes:
    - Persistent TurboJPEG decoder handles (one per OpenMP thread)
    - OpenMP thread pool
    - Memory page faults for buffer pool (if provided)
    
    Call this once during application startup before any decode operations.
    
    Args:
        pool: Optional buffer pool to pre-warm (pre-fault memory pages)
    
    Returns:
        True if warmup succeeded, False otherwise
    """
    try:
        lib = _load_library()
        pool_handle = pool.handle if pool else None
        lib.aodecode_warmup_full(pool_handle)
        log.debug("Native decode pipeline pre-warmed")
        return True
    except Exception as e:
        log.debug(f"Native decode warmup failed: {e}")
        return False


# ============================================================================
# Module initialization
# ============================================================================

# Try to load library on import to fail fast
try:
    _load_library()
except Exception:
    pass  # Will raise on first use if not available


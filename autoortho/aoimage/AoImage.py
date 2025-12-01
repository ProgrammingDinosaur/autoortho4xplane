#!/usr/bin/env python
"""
AoImage - Python wrapper for aoimage C library.

This module provides a safe Python interface to the aoimage C library,
with comprehensive error handling to prevent crashes.
"""

import os
import sys
from ctypes import (
    Structure, CDLL, POINTER,
    c_uint64, c_uint32, c_int32, c_float, c_char, c_char_p,
    create_string_buffer, memmove
)

from utils.constants import system_type
import logging
log = logging.getLogger(__name__)


# Optional breadcrumb support for crash debugging
def _breadcrumb(msg):
    """Write breadcrumb for crash debugging (if available)."""
    try:
        from crash_handler import breadcrumb
        breadcrumb(msg)
    except Exception:
        pass


class AOImageException(Exception):
    """Exception for AoImage errors."""
    pass


class AoImage(Structure):
    """
    Python wrapper for aoimage_t C structure.
    
    Provides safe access to C image processing functions with:
    - Automatic memory management (prevent double-free)
    - Input validation before C calls
    - Exception handling for C call failures
    """
    _fields_ = [
        ('_data', c_uint64),
        ('_width', c_uint32),
        ('_height', c_uint32),
        ('_stride', c_uint32),
        ('_channels', c_uint32),
        ('_errmsg', c_char * 80)
    ]

    def __init__(self):
        self._data = 0
        self._width = 0
        self._height = 0
        self._stride = 0
        self._channels = 0
        self._errmsg = b''
        self._freed = False  # Prevent double-free crashes

    def __del__(self):
        """Clean up C memory. Protected against double-free."""
        if not self._freed and self._data != 0:
            try:
                _aoi.aoimage_delete(self)
                self._freed = True
            except Exception as e:
                log.debug(f"Error in AoImage.__del__: {e}")

    def __repr__(self):
        return (f"AoImage(width={self._width}, height={self._height}, "
                f"stride={self._stride}, channels={self._channels})")

    def close(self):
        """Explicitly free C memory. Safe to call multiple times."""
        if not self._freed and self._data != 0:
            try:
                _aoi.aoimage_delete(self)
                self._freed = True
            except Exception as e:
                log.error(f"Error in AoImage.close: {e}")

    def convert(self, mode):
        """Convert image to specified mode (only RGBA supported)."""
        if mode != "RGBA":
            log.error(f"convert: Only RGBA mode supported, got {mode}")
            return None
        
        if self._data == 0:
            log.error("convert: Source image has no data")
            return None
        
        new_img = AoImage()
        try:
            _breadcrumb(f"convert {self._width}x{self._height}")
            if not _aoi.aoimage_2_rgba(self, new_img):
                log.error(f"convert error: {new_img._errmsg.decode()}")
                return None
            return new_img
        except Exception as e:
            log.error(f"convert exception: {e}")
            return None

    def reduce_2(self, steps=1):
        """Reduce image by factor of 2, repeated 'steps' times."""
        if steps < 1:
            log.error(f"reduce_2: Invalid steps {steps}")
            return None
        
        if self._data == 0:
            log.error("reduce_2: Source image has no data")
            return None

        half = self
        for i in range(steps):
            orig = half
            half = AoImage()
            try:
                _breadcrumb(f"reduce_2 step {i+1}/{steps}")
                if not _aoi.aoimage_reduce_2(orig, half):
                    err = half._errmsg.decode()
                    log.error(f"reduce_2 error at step {i+1}: {err}")
                    raise AOImageException(f"reduce_2 error: {err}")
            except AOImageException:
                raise
            except Exception as e:
                log.error(f"reduce_2 exception at step {i+1}: {e}")
                raise AOImageException(f"reduce_2 exception: {e}")

        return half

    def scale(self, factor=2):
        """Scale image by given factor."""
        # Validate factor
        if not isinstance(factor, (int, float)) or factor <= 0:
            log.error(f"scale: Invalid factor {factor}")
            return None
        
        if factor > 1000:
            log.error(f"scale: Factor {factor} too large (max 1000)")
            return None
        
        if self._data == 0:
            log.error("scale: Source image has no data")
            return None
        
        scaled = AoImage()
        try:
            _breadcrumb(f"scale {self._width}x{self._height} by {factor}")
            if not _aoi.aoimage_scale(self, scaled, int(factor)):
                log.error(f"scale error: {scaled._errmsg.decode()}")
                return None
            return scaled
        except Exception as e:
            log.error(f"scale exception: {e}")
            return None

    def write_jpg(self, filename, quality=90):
        """Write image to JPEG file."""
        if self._data == 0:
            log.error("write_jpg: Image has no data")
            return False
        
        try:
            _breadcrumb(f"write_jpg {filename}")
            if not _aoi.aoimage_write_jpg(filename.encode(), self, quality):
                log.error(f"write_jpg error: {self._errmsg.decode()}")
                return False
            return True
        except Exception as e:
            log.error(f"write_jpg exception: {e}")
            return False

    def tobytes(self):
        """Return image data as bytes. High overhead - use data_ptr() instead."""
        if self._data == 0:
            log.error("tobytes: Image has no data")
            return None
        
        try:
            size = self._width * self._height * self._channels
            buf = create_string_buffer(size)
            _aoi.aoimage_tobytes(self, buf)
            return buf.raw
        except Exception as e:
            log.error(f"tobytes exception: {e}")
            return None

    def data_ptr(self):
        """Return pointer to image data. Valid only while object is alive."""
        return self._data

    def _set_data(self, rgba_bytes):
        """
        Set image data from raw RGBA bytes.
        
        Used for reconstructing image from subprocess results.
        WARNING: Internal use only. Caller must ensure correct size.
        """
        if self._data == 0:
            log.error("_set_data: Image has no buffer")
            return False
        
        expected_size = self._width * self._height * 4
        if len(rgba_bytes) != expected_size:
            log.error(f"_set_data: Size mismatch {len(rgba_bytes)} != {expected_size}")
            return False
        
        try:
            # Copy bytes into existing buffer
            memmove(self._data, rgba_bytes, expected_size)
            return True
        except Exception as e:
            log.error(f"_set_data exception: {e}")
            return False

    def paste(self, p_img, pos):
        """Paste another image onto this image at position (x, y)."""
        # Validate parameters
        if not p_img or not hasattr(p_img, '_width'):
            log.error("paste: Invalid source image")
            return False
        
        if self._data == 0:
            log.error("paste: Destination image has no data")
            return False
        
        if p_img._data == 0:
            log.error("paste: Source image has no data")
            return False
        
        x, y = pos
        if x < 0 or y < 0:
            log.error(f"paste: Negative position ({x}, {y})")
            return False
        
        if x + p_img._width > self._width or y + p_img._height > self._height:
            log.error(f"paste: Out of bounds: ({x},{y}) + "
                      f"({p_img._width}x{p_img._height}) > "
                      f"({self._width}x{self._height})")
            return False
        
        try:
            _breadcrumb(f"paste {p_img._width}x{p_img._height} at ({x},{y})")
            _aoi.aoimage_paste(self, p_img, x, y)
            return True
        except Exception as e:
            log.error(f"paste exception: {e}")
            return False

    def crop(self, c_img, pos):
        """Crop region from this image into c_img at position (x, y)."""
        # Validate parameters
        if not c_img or not hasattr(c_img, '_width'):
            log.error("crop: Invalid destination image")
            return False
        
        if self._data == 0:
            log.error("crop: Source image has no data")
            return False
        
        x, y = pos
        if x < 0 or y < 0:
            log.error(f"crop: Negative position ({x}, {y})")
            return False
        
        if x + c_img._width > self._width or y + c_img._height > self._height:
            log.error(f"crop: Out of bounds: ({x},{y}) + "
                      f"({c_img._width}x{c_img._height}) > "
                      f"({self._width}x{self._height})")
            return False
        
        try:
            _breadcrumb(f"crop {c_img._width}x{c_img._height} from ({x},{y})")
            _aoi.aoimage_crop(self, c_img, x, y)
            return True
        except Exception as e:
            log.error(f"crop exception: {e}")
            return False

    def copy(self, height_only=0):
        """Create a copy of this image (optionally only first height_only rows)."""
        if self._data == 0:
            log.error("copy: Source image has no data")
            return None
        
        new_img = AoImage()
        try:
            _breadcrumb(f"copy {self._width}x{self._height}")
            if not _aoi.aoimage_copy(self, new_img, height_only):
                log.error(f"copy error: {new_img._errmsg.decode()}")
                return None
            return new_img
        except Exception as e:
            log.error(f"copy exception: {e}")
            return None

    def desaturate(self, saturation=1.0):
        """Desaturate image (0.0 = grayscale, 1.0 = full color)."""
        if saturation < 0.0 or saturation > 1.0:
            log.error(f"desaturate: Invalid saturation {saturation}")
            return None
        
        if saturation == 1.0:
            return self
        
        if self._data == 0:
            log.error("desaturate: Image has no data")
            return None
        
        try:
            _breadcrumb(f"desaturate {saturation}")
            if not _aoi.aoimage_desaturate(self, saturation):
                log.error(f"desaturate error: {self._errmsg.decode()}")
                return None
            return self
        except Exception as e:
            log.error(f"desaturate exception: {e}")
            return None

    def crop_and_upscale(self, x, y, width, height, scale_factor):
        """Crop region and upscale atomically (high performance)."""
        if self._data == 0:
            raise AOImageException("crop_and_upscale: Source has no data")
        
        # Validate bounds
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise AOImageException(f"crop_and_upscale: Invalid params "
                                   f"x={x} y={y} w={width} h={height}")
        
        if x + width > self._width or y + height > self._height:
            raise AOImageException(f"crop_and_upscale: Out of bounds")
        
        if scale_factor <= 0 or scale_factor > 64:
            raise AOImageException(f"crop_and_upscale: Invalid scale {scale_factor}")
        
        result = AoImage()
        try:
            _breadcrumb(f"crop_upscale ({x},{y}) {width}x{height} *{scale_factor}")
            if not _aoi.aoimage_crop_and_upscale(
                    self, result, x, y, width, height, scale_factor):
                raise AOImageException(f"crop_and_upscale: {result._errmsg.decode()}")
            return result
        except AOImageException:
            raise
        except Exception as e:
            raise AOImageException(f"crop_and_upscale exception: {e}")

    @property
    def size(self):
        """Return (width, height) tuple."""
        return self._width, self._height


# Factory functions
def new(mode, wh, color):
    """Create new image with given dimensions and fill color."""
    if mode != "RGBA":
        log.error(f"new: Only RGBA mode supported, got {mode}")
        return None
    
    width, height = wh
    if width <= 0 or height <= 0:
        log.error(f"new: Invalid dimensions {width}x{height}")
        return None
    
    if width > 65536 or height > 65536:
        log.error(f"new: Dimensions too large {width}x{height}")
        return None
    
    img = AoImage()
    try:
        _breadcrumb(f"new {width}x{height}")
        if not _aoi.aoimage_create(img, width, height, color[0], color[1], color[2]):
            log.error(f"new error: {img._errmsg.decode()}")
            return None
        return img
    except Exception as e:
        log.error(f"new exception: {e}")
        return None


def load_from_memory(mem, datalen=None, use_safe_mode=None):
    """
    Load image from memory buffer (JPEG data).
    
    Args:
        mem: JPEG data bytes
        datalen: Optional length (defaults to len(mem))
        use_safe_mode: None=use config, True=force safe, False=force direct
    """
    if not mem:
        log.error("load_from_memory: Empty buffer")
        return None
    
    if datalen is None:
        datalen = len(mem)
    
    if datalen < 4:
        log.error(f"load_from_memory: Data too short ({datalen} bytes)")
        return None
    
    # Check if we should use safe mode (subprocess isolation)
    if use_safe_mode is None:
        try:
            from aoconfig import CFG
            safe_mode = getattr(CFG.pydds, 'safe_mode', 'off')
            use_safe_mode = (safe_mode == 'full')
        except Exception:
            use_safe_mode = False
    
    if use_safe_mode:
        # SAFE MODE: All C operations in subprocess with shared memory
        # Returns None on failure to allow caller to use fallbacks (e.g., higher mipmap)
        try:
            # Use shared memory worker (optimized)
            from shared_memory_worker import shm_load_jpeg
            result = shm_load_jpeg(mem)
            
            if result is None:
                # Worker failed - return None to allow fallback
                log.debug("Safe mode: Worker returned None, allowing fallback")
                return None
            
            width, height, rgba_data = result
            
            # Create AoImage from raw RGBA data
            img = new('RGBA', (width, height), (0, 0, 0))
            if img and rgba_data:
                # Copy RGBA data into image
                img._set_data(rgba_data)
                return img
            else:
                log.debug("Safe mode: Failed to create image from worker result")
                return None  # Allow fallback
        except Exception as e:
            log.warning(f"Safe mode load exception: {e}")
            return None  # Return None to allow fallback
    
    # Direct load (only used when safe_mode is OFF)
    img = AoImage()
    try:
        # Keep strong reference to prevent GC during C call
        mem_ref = mem
        _breadcrumb(f"load_from_memory {datalen} bytes")
        if not _aoi.aoimage_from_memory(img, mem_ref, datalen):
            log.error(f"load_from_memory error: {img._errmsg.decode()}")
            return None
        # Use mem_ref after C call to prevent early GC
        _ = len(mem_ref)
        return img
    except Exception as e:
        log.error(f"load_from_memory exception: {e}")
        return None


def open(filename):
    """Open JPEG file and return AoImage."""
    if not filename:
        log.error("open: No filename provided")
        return None
    
    img = AoImage()
    try:
        _breadcrumb(f"open {filename}")
        if not _aoi.aoimage_read_jpg(filename.encode(), img):
            log.debug(f"open error for {filename}: {img._errmsg.decode()}")
            return None
        return img
    except Exception as e:
        log.error(f"open exception for {filename}: {e}")
        return None


# Library initialization
if system_type == 'linux':
    _aoi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'aoimage.so')
elif system_type == 'windows':
    _aoi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'aoimage.dll')
elif system_type == 'darwin':
    _aoi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'aoimage.dylib')
else:
    log.error(f"Unsupported system: {system_type}")
    raise RuntimeError(f"Unsupported system: {system_type}")

try:
    if not os.path.exists(_aoi_path):
        raise FileNotFoundError(f"aoimage library not found at: {_aoi_path}")
    _aoi = CDLL(_aoi_path)
    log.debug(f"Loaded aoimage library from {_aoi_path}")
except Exception as e:
    log.error(f"FATAL: Failed to load aoimage library: {e}")
    raise

# Set argtypes for type safety
_aoi.aoimage_read_jpg.argtypes = (c_char_p, POINTER(AoImage))
_aoi.aoimage_write_jpg.argtypes = (c_char_p, POINTER(AoImage), c_int32)
_aoi.aoimage_2_rgba.argtypes = (POINTER(AoImage), POINTER(AoImage))
_aoi.aoimage_reduce_2.argtypes = (POINTER(AoImage), POINTER(AoImage))
_aoi.aoimage_scale.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32)
_aoi.aoimage_delete.argtypes = (POINTER(AoImage),)
_aoi.aoimage_create.argtypes = (
    POINTER(AoImage), c_uint32, c_uint32, c_uint32, c_uint32, c_uint32
)
_aoi.aoimage_tobytes.argtypes = (POINTER(AoImage), c_char_p)
_aoi.aoimage_from_memory.argtypes = (POINTER(AoImage), c_char_p, c_uint32)
_aoi.aoimage_paste.argtypes = (
    POINTER(AoImage), POINTER(AoImage), c_uint32, c_uint32
)
_aoi.aoimage_crop.argtypes = (
    POINTER(AoImage), POINTER(AoImage), c_uint32, c_uint32
)
_aoi.aoimage_copy.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32)
_aoi.aoimage_desaturate.argtypes = (POINTER(AoImage), c_float)
_aoi.aoimage_crop_and_upscale.argtypes = (
    POINTER(AoImage), POINTER(AoImage),
    c_uint32, c_uint32, c_uint32, c_uint32, c_uint32
)


# =============================================================================
# Placeholder/Fallback Functions - NEVER crash, always return usable image
# =============================================================================

def new_or_placeholder(mode, wh, color, placeholder_color=(255, 0, 255)):
    """
    Create new image, or return placeholder if creation fails.
    
    NEVER returns None - always returns a usable image.
    Placeholder is magenta by default (visible error indicator).
    """
    result = new(mode, wh, color)
    if result is not None:
        return result
    
    # Try creating a placeholder
    log.warning(f"Creating placeholder image {wh[0]}x{wh[1]}")
    result = new(mode, wh, placeholder_color)
    if result is not None:
        return result
    
    # Last resort: try smaller size
    safe_size = (max(4, min(wh[0], 256)), max(4, min(wh[1], 256)))
    log.warning(f"Creating minimal placeholder {safe_size[0]}x{safe_size[1]}")
    return new(mode, safe_size, placeholder_color)


def load_from_memory_or_placeholder(mem, datalen=None, 
                                     placeholder_size=(256, 256),
                                     placeholder_color=(255, 0, 255)):
    """
    Load image from memory, or return placeholder if loading fails.
    
    NEVER returns None - always returns a usable image.
    Placeholder is magenta by default (visible error indicator).
    """
    if mem and (datalen is None or datalen >= 4):
        result = load_from_memory(mem, datalen)
        if result is not None:
            return result
    
    # Return placeholder
    log.warning(f"Creating placeholder for failed load")
    return new('RGBA', placeholder_size, placeholder_color)


def open_or_placeholder(filename, placeholder_size=(256, 256),
                        placeholder_color=(255, 0, 255)):
    """
    Open file, or return placeholder if open fails.
    
    NEVER returns None - always returns a usable image.
    """
    if filename:
        result = open(filename)
        if result is not None:
            return result
    
    log.warning(f"Creating placeholder for failed open: {filename}")
    return new('RGBA', placeholder_size, placeholder_color)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("AoImage module test")
    
    # Basic test
    img = new('RGBA', (256, 256), (128, 128, 128))
    if img:
        print(f"Created: {img}")
        img.write_jpg("/tmp/test.jpg")
    else:
        print("Failed to create image")

#!/usr/bin/env python

import os
import sys
from ctypes import *

from utils.constants import system_type
import logging
log = logging.getLogger(__name__)

class AOImageException(Exception):
    pass

class AoImage(Structure):
    _fields_ = [
        ('_data', c_uint64),    # ctypes pointers are tricky when changed under the hud so we treat it as number
        #('_data', POINTER(c_uint64)),    # ctypes pointers are tricky when changed under the hud so we treat it as number
        ('_width', c_uint32),
        ('_height', c_uint32),
        ('_stride', c_uint32),
        ('_channels', c_uint32),
        ('_errmsg', c_char*80)  #possible error message to be filled by the C routines
    ]

    def __init__(self):
        self._data = 0
        #self._data = cast('\x00', POINTER(c_uint64))
        self._width = 0
        self._height = 0
        self._stride = 0
        self._channels = 0
        self._errmsg = b'';
        self._freed = False  # Prevent double-free crashes

    def __del__(self):
        # Only delete if not already freed (prevents double-free crash)
        if not self._freed:
            try:
                _aoi.aoimage_delete(self)
                self._freed = True
            except Exception as e:
                # Log but don't raise in __del__ (causes issues)
                log.debug(f"Error in AoImage.__del__: {e}")

    def __repr__(self):
        return f"ptr:  width: {self._width} height: {self._height} stride: {self._stride} channels: {self._channels}"

    def close(self):
        # Only delete if not already freed (prevents double-free crash)
        if not self._freed:
            try:
                _aoi.aoimage_delete(self)
                self._freed = True
            except Exception as e:
                log.error(f"Error in AoImage.close: {e}")
        
    def convert(self, mode):
        """
        Not really needed as AoImage always loads as RGBA
        """
        assert mode == "RGBA", "Sorry, only conversion to RGBA supported"
        new = AoImage()
        if not _aoi.aoimage_2_rgba(self, new):
            log.debug(f"AoImage.reduce_2 error: {new._errmsg.decode()}")
            return None

        return new

    def reduce_2(self, steps = 1):
        """
        Reduce image by factor 2.
        """
        assert steps >= 1, "useless reduce_2" # otherwise we must do a useless copy

        half = self
        while steps >= 1:
            orig = half
            half = AoImage()
            if not _aoi.aoimage_reduce_2(orig, half):
                log.debug(f"AoImage.reduce_2 error: {half._errmsg.decode()}")
                raise AOImageException(f"AoImage.reduce_2 error: {half._errmsg.decode()}")
                #return None

            steps -= 1

        return half

    def scale(self, factor=2):
        # CRITICAL FIX #10: Validate scale factor
        if not isinstance(factor, (int, float)) or factor <= 0:
            log.error(f"scale: Invalid factor {factor} - must be positive number")
            return None
        
        if factor > 1000:  # Sanity check
            log.error(f"scale: Factor {factor} too large (max 1000)")
            return None
        
        scaled = AoImage()
        orig = self
        
        try:
            log.debug(f"AoImage.scale: Scaling {self._width}x{self._height} by {factor}")
            if not _aoi.aoimage_scale(orig, scaled, factor):
                log.error(f"AoImage.scale error: {scaled._errmsg.decode()}")
                return None
            log.debug(f"AoImage.scale: Success, created {scaled._width}x{scaled._height}")
            return scaled
        except Exception as e:
            log.error(f"scale: Exception: {e}")
            return None

    def write_jpg(self, filename, quality = 90):
        """
        Convenience function to write jpeg.
        """   
        if not _aoi.aoimage_write_jpg(filename.encode(), self, quality):
            log.debug(f"AoImage.new error: {new._errmsg.decode()}")
    
    def tobytes(self):
        """
        Not really needed, high overhead. Use data_ptr instead.
        """      
        buf = create_string_buffer(self._width * self._height * self._channels)
        _aoi.aoimage_tobytes(self, buf)
        return buf.raw

    def data_ptr(self):
        """
        Return ptr to image data. Valid only as long as the object lives.
        """
        return self._data

    def paste(self, p_img, pos):
        # CRITICAL FIX #4: Validate parameters before C call
        if not p_img or not hasattr(p_img, '_width'):
            log.error("paste: Invalid image object")
            return False
        
        x, y = pos
        if x < 0 or y < 0:
            log.error(f"paste: Invalid position ({x}, {y}) - cannot be negative")
            return False
        
        if x + p_img._width > self._width or y + p_img._height > self._height:
            log.error(f"paste: Image extends beyond bounds: pos=({x},{y}), size=({p_img._width}x{p_img._height}), dest=({self._width}x{self._height})")
            return False
        
        try:
            log.debug(f"AoImage.paste: Pasting {p_img._width}x{p_img._height} at ({x},{y})")
            _aoi.aoimage_paste(self, p_img, pos[0], pos[1])
            return True
        except Exception as e:
            log.error(f"paste: C call failed: {e}")
            return False

    def crop(self, c_img, pos):
        # CRITICAL FIX #4: Validate parameters before C call
        if not c_img or not hasattr(c_img, '_width'):
            log.error("crop: Invalid destination image object")
            return False
        
        x, y = pos
        if x < 0 or y < 0:
            log.error(f"crop: Invalid position ({x}, {y}) - cannot be negative")
            return False
        
        if x + c_img._width > self._width or y + c_img._height > self._height:
            log.error(f"crop: Crop region extends beyond bounds: pos=({x},{y}), size=({c_img._width}x{c_img._height}), source=({self._width}x{self._height})")
            return False
        
        try:
            log.debug(f"AoImage.crop: Cropping {c_img._width}x{c_img._height} from ({x},{y})")
            _aoi.aoimage_crop(self, c_img, pos[0], pos[1])
            return True
        except Exception as e:
            log.error(f"crop: C call failed: {e}")
            return False

    def copy(self, height_only = 0):
        new = AoImage()
        if not _aoi.aoimage_copy(self, new, height_only):
            log.error(f"AoImage.copy error: {self._errmsg.decode()}")
            return None

        return new

    
    def desaturate(self, saturation = 1.0):
        assert 0.0 <= saturation and saturation <= 1.0
        if saturation == 1.0 or saturation is None:
            return self

        if not _aoi.aoimage_desaturate(self, saturation):
            log.error(f"AoImage.desaturate error: {self._errmsg.decode()}")
            return None
        return self

    def crop_and_upscale(self, x, y, width, height, scale_factor):
        """
        Crop a region and upscale it atomically in C (high performance).
        This is optimized for fallback imagery - single allocation, no intermediate buffers.
        
        Args:
            x, y: Crop region start position
            width, height: Crop region dimensions
            scale_factor: Upscale factor (must be power of 2: 2, 4, 8, 16)
        
        Returns:
            New AoImage with dimensions (width * scale_factor, height * scale_factor)
        """
        result = AoImage()
        if not _aoi.aoimage_crop_and_upscale(self, result, x, y, width, height, scale_factor):
            raise AOImageException(f"crop_and_upscale failed: {result._errmsg.decode()}")
        return result

    @property
    def size(self):
        return self._width, self._height

## factories
def new(mode, wh, color):
    #print(f"{mode}, {wh}, {color}")
    assert(mode == "RGBA")
    new = AoImage()
    if not _aoi.aoimage_create(new, wh[0], wh[1], color[0], color[1], color[2]):
        log.debug(f"AoImage.new error: {new._errmsg.decode()}")
        return None

    return new


def load_from_memory(mem, datalen=None):
    # Validate input before passing to C code
    if not mem:
        log.error("AoImage.load_from_memory: mem is None or empty")
        return None
    
    if not datalen:
        datalen = len(mem)
    
    if datalen < 4:
        log.error(f"AoImage.load_from_memory: data too short ({datalen} bytes)")
        return None
    
    new = AoImage()
    try:
        # Breadcrumb: Log BEFORE entering C code (helps debug crashes)
        log.debug(f"AoImage: Calling C aoimage_from_memory with {datalen} bytes")
        
        # Keep strong reference to mem to prevent GC during C call
        mem_ref = mem
        if not _aoi.aoimage_from_memory(new, mem_ref, datalen):
            log.error(f"AoImage.load_from_memory error: {new._errmsg.decode()}")
            return None
        
        # Breadcrumb: Made it through C code successfully
        log.debug(f"AoImage: C call succeeded, created {new._width}x{new._height} image")
    except Exception as e:
        log.error(f"AoImage.load_from_memory exception: {e}")
        return None

    return new

def open(filename):
    new = AoImage()
    if not _aoi.aoimage_read_jpg(filename.encode(), new):
        log.debug(f"AoImage.open error for {filename}: {new._errmsg.decode()}")
        return None

    return new

# init code
if system_type == 'linux':
    _aoi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'aoimage.so')
elif system_type == 'windows':
    _aoi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'aoimage.dll')
elif system_type == 'darwin':
    _aoi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'aoimage.dylib')
else:
    log.error("System is not supported")
    exit()

# Load C library with error handling to prevent silent crashes
try:
    if not os.path.exists(_aoi_path):
        raise FileNotFoundError(f"aoimage library not found at: {_aoi_path}")
    _aoi = CDLL(_aoi_path)
except Exception as e:
    log.error(f"FATAL: Failed to load aoimage library from {_aoi_path}")
    log.error(f"Error: {e}")
    log.error("AutoOrtho cannot continue without this library.")
    log.error("Please verify installation and that all DLL dependencies are present.")
    raise
_aoi.aoimage_read_jpg.argtypes = (c_char_p, POINTER(AoImage))
_aoi.aoimage_write_jpg.argtypes = (c_char_p, POINTER(AoImage), c_int32)
_aoi.aoimage_2_rgba.argtypes = (POINTER(AoImage), POINTER(AoImage))
_aoi.aoimage_reduce_2.argtypes = (POINTER(AoImage), POINTER(AoImage))
_aoi.aoimage_scale.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32)
_aoi.aoimage_delete.argtypes = (POINTER(AoImage),)
_aoi.aoimage_create.argtypes = (POINTER(AoImage), c_uint32, c_uint32, c_uint32, c_uint32, c_uint32)
_aoi.aoimage_tobytes.argtypes = (POINTER(AoImage), c_char_p)
_aoi.aoimage_from_memory.argtypes = (POINTER(AoImage), c_char_p, c_uint32)
_aoi.aoimage_paste.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32, c_uint32)
_aoi.aoimage_crop.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32, c_uint32)
_aoi.aoimage_copy.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32)
_aoi.aoimage_desaturate.argtypes = (POINTER(AoImage), c_float)
_aoi.aoimage_crop_and_upscale.argtypes = (POINTER(AoImage), POINTER(AoImage), c_uint32, c_uint32, c_uint32, c_uint32, c_uint32)

def main():
    logging.basicConfig(level = logging.DEBUG)
    width = 16
    height = 16
    black = new('RGBA', (256*width,256*height), (0,0,0))
    log.info(f"{black}")
    log.info(f"black._data: {black._data}")
    log.info(f"black.data_ptr(): {black.data_ptr()}")
    black.write_jpg("black.jpg")
    w, h = black.size
    black = None
    log.info(f"black done, {w} {h}")

    green = new('RGBA', (256*width,256*height), (0,230,0))
    log.info(f"green {green}")
    green.write_jpg("green.jpg")

    log.info("Trying nonexistent jpg")
    img = open("../testfiles/non_exitent.jpg")

    log.info("Trying non jpg")
    img = open("main.c")

    img = open("../testfiles/test_tile2.jpg")
    log.info(f"AoImage.open {img}")

    img2 = img.reduce_2()
    log.info(f"img2: {img2}")

    img2.write_jpg("test_tile_2.jpg")

    img3 = open("../testfiles/test_tile_small.jpg")
    big = img3.scale(16)
    big.write_jpg('test_tile_big.jpg')

    cropimg = new('RGBA', (256,256), (0,0,0))
    img.crop(cropimg, (256,256))
    cropimg.write_jpg("crop.jpg")

    green.paste(img2, (1024, 1024))
    green.write_jpg("test_tile_p.jpg")

    img4 = img.reduce_2(2)
    log.info(f"img4 {img4}")


    img.paste(img4, (0, 2048))
    img.write_jpg("test_tile_p2.jpg")





if __name__ == "__main__":
    main()

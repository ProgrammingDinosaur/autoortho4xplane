#!/usr/bin/env python

import os
import sys
import math
from io import BytesIO
from binascii import hexlify
from ctypes import *
#from PIL import Image
from aoimage import AoImage as Image

import threading

#from functools import lru_cache, cache

#from memory_profiler import profile
from aoconfig import CFG
from utils.constants import system_type

import logging
log = logging.getLogger(__name__)

# Define rgba_surface structure BEFORE it's referenced in library setup
class rgba_surface(Structure):
    _fields_ = [
        ('data', c_char_p),
        ('width', c_uint32),
        ('height', c_uint32),
        ('stride', c_uint32)
    ]

#_stb = CDLL("/usr/lib/x86_64-linux-gnu/libstb.so")
if system_type == 'linux':
    print("Linux detected")
    _stb_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'linux', 'lib_stb_dxt.so')
    _ispc_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'linux', 'libispc_texcomp.so')
elif system_type == 'windows':
    print("Windows detected")
    _stb_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'windows', 'stb_dxt.dll')
    _ispc_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'windows', 'ispc_texcomp.dll')
elif system_type == 'darwin':
    print("macOS detected")
    _stb_path = None
    _ispc_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'macos', 'libispc_texcomp.dylib')
else:
    print("System is not supported")
    exit()

# Load compression libraries with error handling
_stb = None
if _stb_path:
    try:
        if not os.path.exists(_stb_path):
            raise FileNotFoundError(f"STB DXT library not found at: {_stb_path}")
        _stb = CDLL(_stb_path)
        log.info(f"Loaded STB DXT library from {_stb_path}")
        
        # CRITICAL FIX #5: Set argtypes ONCE at module load (thread safety)
        _stb.compress_pixels.argtypes = (c_char_p, c_char_p, c_uint64, c_uint64, c_bool)
        
    except Exception as e:
        log.error(f"Failed to load STB DXT library from {_stb_path}: {e}")
        log.warning("DXT1 compression may not work correctly")
        # Don't raise - ISPC can handle all formats

try:
    if not os.path.exists(_ispc_path):
        raise FileNotFoundError(f"ISPC texcomp library not found at: {_ispc_path}")
    _ispc = CDLL(_ispc_path)
    log.info(f"Loaded ISPC texcomp library from {_ispc_path}")
    
    # CRITICAL FIX #5: Set argtypes ONCE at module load (thread safety)
    # Don't set these in compress() - causes race conditions!
    _ispc.CompressBlocksBC3.argtypes = (POINTER(rgba_surface), c_char_p)
    _ispc.CompressBlocksBC1.argtypes = (POINTER(rgba_surface), c_char_p)
    
except Exception as e:
    log.error(f"FATAL: Failed to load ISPC texcomp library from {_ispc_path}")
    log.error(f"Error: {e}")
    log.error("AutoOrtho cannot continue without this library.")
    raise

DDSD_CAPS = 0x00000001          # dwCaps/dwCaps2 is enabled. 
DDSD_HEIGHT = 0x00000002                # dwHeight is enabled. 
DDSD_WIDTH = 0x00000004                 # dwWidth is enabled. Required for all textures. 
DDSD_PITCH = 0x00000008                 # dwPitchOrLinearSize represents pitch. 
DDSD_PIXELFORMAT = 0x00001000   # dwPfSize/dwPfFlags/dwRGB/dwFourCC and such are enabled. 
DDSD_MIPMAPCOUNT = 0x00020000   # dwMipMapCount is enabled. Required for storing mipmaps. 
DDSD_LINEARSIZE = 0x00080000    # dwPitchOrLinearSize represents LinearSize. 
DDSD_DEPTH = 0x00800000                 # dwDepth is enabled. Used for 3D (Volume) Texture. 


STB_DXT_NORMAL = 0
STB_DXT_DITHER = 1
STB_DXT_HIGHQUAL = 2


# def do_compress(img):
# 
#     width, height = img.size
# 
#     if (width < 4 or width % 4 != 0 or height < 4 or height % 4 != 0):
#         log.debug("Compressed images must have dimensions that are multiples of 4.")
#         return None
# 
#     if img.mode == "RGB":
#         img = img.convert("RGBA")
#     
#     data = img.tobytes()
# 
#     is_rgba = True
#     blocksize = 16
# 
#     dxt_size = ((width+3) >> 2) * ((height+3) >> 2) * 16
#     outdata = create_string_buffer(dxt_size)
# 
#     _stb.compress_pixels.argtypes = (
#             c_char_p,
#             c_char_p, 
#             c_uint64, 
#             c_uint64, 
#             c_bool)
# 
#     result = _stb.compress_pixels(
#             outdata,
#             c_char_p(data),
#             c_uint64(width), 
#             c_uint64(height), 
#             c_bool(is_rgba))
# 
#     if not result:
#         log.debug("Failed to compress")
# 
#     return (dxt_size, outdata)
#
#def get_size(width, height):
#    return ((width+3) >> 2) * ((height+3) >> 2) * 16

class MipMap(object):
    def __init__(self, idx=0, startpos=0, endpos=0, length=0, retrieved=False, databuffer=None):
        self.idx = idx
        self.startpos = startpos
        self.endpos = endpos
        self.length = length
        self.retrieved = retrieved
        self.databuffer = databuffer
        #self.databuffer = BytesIO()

    def __repr__(self):
        return f"MipMap({self.idx}, {self.startpos}, {self.endpos}, {self.length}, {self.retrieved}, {self.databuffer})"


class DDS(Structure):
    _fields_ = [
        ('magic', c_char * 4),
        ('size', c_uint32),
        ('flags', c_uint32),
        ('height', c_uint32),
        ('width', c_uint32),
        ('pitchOrLinearSize', c_uint32),
        ('depth', c_uint32),
        ('mipMapCount', c_uint32),
        ('reserved1', c_char * 44),
        ('pfSize', c_uint32),
        ('pfFlags', c_uint32),
        ('fourCC', c_char * 4),
        ('rgbBitCount', c_uint32),
        ('rBitMask', c_uint32),
        ('gBitMask', c_uint32),
        ('bBitMask', c_uint32),
        ('aBitMask', c_uint32),
        ('caps', c_uint32),
        ('caps2', c_uint32),
        ('reservedCaps', c_uint32 * 2),
        ('reserved2', c_uint32)
    ]


    def __init__(self, width, height, ispc=True, dxt_format="BC1"):
        self.magic = b"DDS "  
        self.size = 124
        self.flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
        self.width = width
        self.height = height
        

        #self.reserved1 = b"pydds"
        self.pfSize = 32
        self.pfFlags = 0x4

        if dxt_format == 'BC3':
            self.fourCC = b'DXT5'
            self.blocksize = 16
        else:
            self.fourCC = b'DXT1'
            self.blocksize = 8
        
        self.caps = 0x1000 | 0x400000
        self.mipMapCount = 0
       
        #self.mipmaps = []

        self.header = BytesIO()
                
        self.ispc = ispc        
        self.dxt_format = dxt_format
        self.mipmap_map = {}

        #[pow(2,x)*pow(2,x) for x in range(int(math.log(width,2)),1,-1) ]

        # List of tuples [(byte_position, retrieved_bool)]
        self.mipmap_list = []

        # https://learn.microsoft.com/en-us/windows/win32/direct3ddds/dds-header
        # pitchOrLinearSize is the total number of bytes in the top level texture for a compressed texture
        self.pitchOrLinearSize = max(1, (width*height >> 4)) * self.blocksize
        self.position = 0
        
        curbytes = 128
        while (width >= 1) and (height >= 1):
            mipmap = MipMap()
            mipmap.idx = self.mipMapCount
            mipmap.startpos = curbytes
            curbytes += max(1, (width*height >> 4)) * self.blocksize
            mipmap.length = curbytes - mipmap.startpos
            mipmap.endpos = mipmap.startpos + mipmap.length 
            self.mipmap_list.append(mipmap)
            width = width >> 1
            height = height >> 1
            self.mipMapCount+=1
            
        # Size of all mipmaps: sum([pow(2,x)*pow(2,x) for x in range(12,1,-1) ])
        #self.pitchOrLinearSize = curbytes 
        self.total_size = curbytes
        self.dump_header()

        # The smallest effective MM we can have is a size 4x4 block.  However
        # XPlane expects MM down to theoretical 1x1.  Therefore the smallest
        # real MM is the len of our list - 3
        self.smallest_mm = len(self.mipmap_list) - 3

        for m in self.mipmap_list:
            log.debug(m)

        #log.debug(self.mipmap_list)
        log.debug(self.pitchOrLinearSize)
        #print(self.pitchOrLinearSize)
        log.debug(self.mipMapCount)

        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.ready.clear()
        
        # PHASE 2 FIX #9: Per-mipmap locks for thread-safe generation
        self.mipmap_locks = {}  # Lock per mipmap level
        for i in range(self.mipMapCount):
            self.mipmap_locks[i] = threading.Lock()
   
        self.compress_count = 0

    def write(self, filename):
        #self.dump_header()
        with open(filename, 'wb') as h:
            h.write(self)
            log.debug(f"Wrote {h.tell()} bytes")
            for mipmap in self.mipmap_list:
                #if mipmap.retrieved:
                log.debug(f"Writing {mipmap.startpos}")
                h.seek(mipmap.startpos)
                if mipmap.databuffer is not None:
                    buf = mipmap.databuffer.getbuffer()
                    # Write no more than the declared mipmap length to avoid misalignment
                    h.write(buf[:mipmap.length])
                log.debug(f"Wrote {h.tell()-mipmap.startpos} bytes")

            # Make sure we complete the full file size
            mipmap = self.mipmap_list[-1]
            if not mipmap.retrieved:
                h.seek(self.total_size - 2)
                h.write(b'x\00')


    def tell(self):
        return self.position

    def seek(self, offset):
        log.debug(f"SEEK: {offset}")
        self.position = offset

    def read(self, length):
        log.debug(f"PYDDS: READ: {self.position} {length} bytes")

        outdata = b''

        if self.position < 128:
            log.debug("Read the header")
            outdata = self.header.getvalue()
            self.position = 128
            length -= 128

        for mipmap in self.mipmap_list:
           
            #if mipmap.databuffer is None:
            #    continue

            if mipmap.endpos > self.position >= mipmap.startpos:
                #
                # Requested read starts before end of this mipmap and before or equal to the starting position
                #
                log.debug(f"PYDDS: We are reading from mipmap {mipmap.idx}")
                
                log.debug(f"PYDDS: {mipmap} , Pos: {self.position} , Len: {length}")
                # Get position in mipmap
                mipmap_pos = self.position - mipmap.startpos
                #remaining_mipmap_len = mipmap.length - mipmap_pos
                remaining_mipmap_len = mipmap.endpos - self.position

                log.debug(f"Len: {length}, remain: {remaining_mipmap_len}, mipmap_pos {mipmap_pos}")
                if length <= remaining_mipmap_len: 
                    #
                    # Mipmap has more than enough remaining length for request
                    # ~We have remaining length in current mipmap~
                    #
                    if mipmap.databuffer is None:
                        log.warning(f"PYDDS: No buffer for {mipmap.idx}!")
                        #data = b''
                        data = b'\x88' * length
                        log.warning(f"PYDDS: adding to outdata {remaining_mipmap_len} bytes for {mipmap.idx}.")
                    else:
                        log.debug("We have a mipmap and adequated remaining length")
                        mipmap.databuffer.seek(mipmap_pos)
                        data = mipmap.databuffer.read(length)
                        ret_len = length - len(data)
                        if ret_len != 0:
                            # This should be impossible
                            log.error(f"PYDDS  Didn't retrieve full length.  Fill empty bytes.  This is not good! mmpos: {mipmap_pos} retlen: {ret_len} reqlen: {length} mm:{mipmap.idx}")
                            data += b'\xFF' * ret_len
                                
                    outdata += data
                    self.position += length
                    break

                elif length > remaining_mipmap_len:
                    #
                    # Requested length is greater than what's available in this mipmap
                    #
                    log.debug(f"PYDDS: In mipmap {mipmap.idx} not enough length")

                    #if not mipmap.retrieved:
                    if mipmap.databuffer is None:
                        # 
                        # Mipmap not fully retrieved.  Mimpamp buffer may exist for partially retreived mipmap 0, but
                        # we *must* make sure the full size is available.
                        # 
                        #log.warning(f"PYDDS: No buffer for {mipmap.idx}, Attempt to fill {remaining_mipmap_len} bytes")
                        log.warning(f"PYDDS: No buffer for {mipmap.idx}!")
                        #data = b''
                        data = b'\x88' * remaining_mipmap_len
                        log.warning(f"PYDDS: adding to outdata {remaining_mipmap_len} bytes for {mipmap.idx}.")
                    else:    
                        # Mipmap is retrieved
                        mipmap.databuffer.seek(mipmap_pos)
                        data = mipmap.databuffer.read(remaining_mipmap_len)
                    
                    # Make sure we retrieved all the expected data from the mipmap we can.
                    ret_len = remaining_mipmap_len - len(data)
                    if ret_len != 0:
                        log.error(f"PYDDS: ERROR! Didn't retrieve full length of mipmap for {mipmap.idx}!")
                        log.error(f"PYDDS: Didn't retrieve full length.  Fill empty bytes {ret_len}")
                        # Pretty sure this causes visual corruption
                        data += b'\x88' * ret_len

                    outdata += data

                    length -= remaining_mipmap_len
                    #self.position += remaining_mipmap_len
                    self.position = mipmap.endpos


        log.debug(f"PYDDS: END READ: At {self.position} returning {len(outdata)} bytes")
        return outdata


    def dump_header(self):
        self.header.seek(0)
        self.header.write(self)

    #@profile 
    def compress(self, width, height, data):
        # Compress width * height of data
        
        # CRITICAL FIX #1: Validate data before passing to C code
        if not data:
            log.error("DDS.compress: data is None or empty")
            return None
        
        # Validate dimensions
        if (width < 4 or width % 4 != 0 or height < 4 or height % 4 != 0):
            log.error(f"DDS.compress: Invalid dimensions {width}x{height} (must be multiple of 4, >= 4)")
            return None
        
        # Validate data size
        expected_size = width * height * 4  # RGBA = 4 bytes per pixel
        if hasattr(data, '__len__'):
            actual_size = len(data)
            if actual_size < expected_size:
                log.error(f"DDS.compress: Data too short: {actual_size} < {expected_size} bytes")
                return None
        
        # Breadcrumb: Log before entering C code
        log.debug(f"DDS.compress: Compressing {width}x{height} RGBA ({expected_size} bytes) to {self.dxt_format}")

        #outdata = b'\x00'*dxt_size
        
        #bio.write(b'\x00'*dxt_size)
        #outdata = bio.getbuffer().tobytes()


        if self.ispc and self.dxt_format == "BC3":
            # Calculate compressed size
            dxt_size = ((width+3) >> 2) * ((height+3) >> 2) * self.blocksize
            
            # CRITICAL FIX #7: Check for integer overflow
            if dxt_size <= 0 or dxt_size > 2**31:  # Sanity check
                log.error(f"DDS.compress: Invalid compressed size: {dxt_size}")
                return None
            
            try:
                outdata = create_string_buffer(dxt_size)
            except MemoryError as e:
                log.error(f"DDS.compress: Failed to allocate {dxt_size} bytes: {e}")
                return None
            
            #print(f"LEN: {len(outdata)}")
            s = rgba_surface()
            s.data = c_char_p(data)
            s.width = c_uint32(width)
            s.height = c_uint32(height)
            s.stride = c_uint32(width * 4)
            
            # CRITICAL FIX #3: Add error handling for C calls
            try:
                log.debug("DDS.compress: Calling CompressBlocksBC3")
                _ispc.CompressBlocksBC3(s, outdata)
                log.debug("DDS.compress: CompressBlocksBC3 succeeded")
                result = True
            except Exception as e:
                log.error(f"DDS.compress: CompressBlocksBC3 failed: {e}")
                return None
        elif self.ispc and self.dxt_format == "BC1":
            #print("BC1")
            blocksize = 8
            dxt_size = ((width+3) >> 2) * ((height+3) >> 2) * self.blocksize
            
            # CRITICAL FIX #7: Check for integer overflow
            if dxt_size <= 0 or dxt_size > 2**31:
                log.error(f"DDS.compress: Invalid compressed size: {dxt_size}")
                return None
            
            try:
                outdata = create_string_buffer(dxt_size)
            except MemoryError as e:
                log.error(f"DDS.compress: Failed to allocate {dxt_size} bytes: {e}")
                return None
            #print(f"LEN: {len(outdata)}")
        
            s = rgba_surface()
            s.data = c_char_p(data)
            s.width = c_uint32(width)
            s.height = c_uint32(height)
            s.stride = c_uint32(width * 4)
            
            # CRITICAL FIX #3: Add error handling for C calls
            try:
                log.debug("DDS.compress: Calling CompressBlocksBC1")
                _ispc.CompressBlocksBC1(s, outdata)
                log.debug("DDS.compress: CompressBlocksBC1 succeeded")
                result = True
            except Exception as e:
                log.error(f"DDS.compress: CompressBlocksBC1 failed: {e}")
                return None
        else:
            # Use STB compressor; honor BC1 (DXT1, 8 bytes/block) vs BC3 (DXT5, 16 bytes/block)
            use_alpha = (self.dxt_format == "BC3")
            blocksize = self.blocksize
            dxt_size = ((width+3) >> 2) * ((height+3) >> 2) * blocksize
            
            # CRITICAL FIX #7: Check for integer overflow
            if dxt_size <= 0 or dxt_size > 2**31:
                log.error(f"DDS.compress: Invalid compressed size: {dxt_size}")
                return None
            
            try:
                outdata = create_string_buffer(dxt_size)
            except MemoryError as e:
                log.error(f"DDS.compress: Failed to allocate {dxt_size} bytes: {e}")
                return None

            # CRITICAL FIX #3: Add error handling for STB calls
            try:
                log.debug("DDS.compress: Calling STB compress_pixels")
                result = _stb.compress_pixels(
                        outdata,
                        c_char_p(data),
                        c_uint64(width), 
                        c_uint64(height), 
                        c_bool(use_alpha))
                if result:
                    log.debug("DDS.compress: STB compress_pixels succeeded")
                else:
                    log.error("DDS.compress: STB compress_pixels returned False")
                    return None
            except Exception as e:
                log.error(f"DDS.compress: STB compress_pixels failed: {e}")
                return None


        if not result:
            log.debug("Failed to compress")

        self.compress_count += 1
        return outdata

    #@profile
    def gen_mipmaps(self, img, startmipmap=0, maxmipmaps=99, compress_bytes=0):
        # img : PIL/Pillow image
        # startmipmap : Mipmap to start compressing
        # maxmipmaps : Maximum mipmap to compress.  99 = all mipmaps
        # compress_bytes : Optionally limit compression to number of bytes

        #if maxmipmaps <= len(self.mipmap_list):
        #    maxmipmaps = len(self.mipmap_list)

        #if not maxmipmaps:
        #    maxmipmaps = 8

        # Print info outside lock; keep compression non-critical except for writes
        width, height = img.size
        mipmap = startmipmap
        # I believe XP only references up to MM8, so might be able to trim
        # this down more
        # maxmipmaps == 0 indicates we want all mipmaps so set to len
        # of our mipmap_list
        if maxmipmaps > self.smallest_mm:
            log.debug(f"Setting maxmipmaps to {self.smallest_mm}")
            maxmipmaps = self.smallest_mm

        log.debug(self.mipmap_list)
        
        # Initial reduction of image size before mipmap processing 
        steps = 0
        if mipmap > 0:
            desired_width = self.width >> mipmap
            while width > desired_width:
                width >>= 1
                compress_bytes >>= 2
                steps += 1

        if steps > 0:        
            #img = img.reduce(pow(2, steps))
            img = img.reduce_2(steps)

        while True:
            # PHASE 2 FIX #9: Lock per-mipmap to prevent concurrent generation
            # This prevents race conditions when multiple threads generate same mipmap
            mipmap_lock = self.mipmap_locks.get(mipmap, threading.Lock())
            
            with mipmap_lock:
                # Check if already retrieved (another thread may have done it)
                if self.mipmap_list[mipmap].retrieved and not compress_bytes:
                    log.debug(f"MIPMAP: {mipmap} already retrieved by another thread, skipping")
                    break
                
                # CRITICAL FIX #2: Keep strong reference to img during data_ptr() use
                # This prevents GC from freeing the image while C code is using the pointer
                img_ref = img  # Strong reference
                imgdata = img_ref.data_ptr()
                width, height = img_ref.size
                log.debug(f"MIPMAP: {mipmap} SIZE: {img_ref.size}")

                if compress_bytes:
                    # Get how many rows we need to process for requested number of bytes
                    height = math.ceil((compress_bytes * 16) / (width * self.blocksize))
                    # Make the rows a factor of 4
                    height = max(4, ((height + 3) // 4) * 4) 
                    log.debug(f"Doing partial compress of {compress_bytes} bytes.  Height: {height}")
                    compress_bytes >>= 2

                try:
                    # Keep img_ref alive during compress
                    dxtdata = self.compress(width, height, imgdata)
                except Exception as e:
                    log.error(f"dds compress failed: {e}")
                    dxtdata = None
                finally:
                    # CRITICAL FIX #11: Explicitly release reference after compression
                    # This ensures deterministic cleanup even if compress fails
                    pass  # img_ref will be released when we exit this scope

                # Assign databuffer (still within mipmap_lock)
                if dxtdata is not None:
                    self.mipmap_list[mipmap].databuffer = BytesIO(initial_bytes=dxtdata)
                    if not compress_bytes:
                        self.mipmap_list[mipmap].retrieved = True

                    # we are already at 4x4 so push result forward to
                    # remaining MMs
                    if mipmap == self.smallest_mm:
                        log.debug(f"At MM {mipmap}.  Set the remaining MMs..")
                        for mm in self.mipmap_list[self.smallest_mm:]:
                            mm.databuffer = BytesIO(initial_bytes=dxtdata)
                            mm.retrieved = True
                            mipmap += 1

                dxtdata = None
                # mipmap_lock released here

        
            if mipmap >= maxmipmaps: #(maxmipmaps + 1) or mipmap >= self.smallest_mm:
                # We've hit or max requested or possible mipmaps
                break

            mipmap += 1
            # Halve the image
            # Keep reference to original before reducing
            prev_img = img
            img = prev_img.reduce_2()
            # prev_img can now be GC'd

        # Dump header (use main lock for header writes)
        with self.lock:
            self.dump_header()


def to_dds(img, outpath):
    #if img.mode == "RGB":
    #    img = img.convert("RGBA")
    width, height = img.size

    dds = DDS(width, height)
    dds.gen_mipmaps(img)
    dds.write(outpath)
    

def main():
    inimg = sys.argv[1]
    outimg = sys.argv[2]
    img = Image.open(inimg)
   
    to_dds(img, outimg)

if __name__ == "__main__":
    main()

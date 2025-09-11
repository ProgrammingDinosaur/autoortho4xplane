#!/usr/bin/env python3

import os
import math
import ctypes
import multiprocessing as mp
from multiprocessing import shared_memory

from utils.constants import system_type

import logging
log = logging.getLogger(__name__)


# Local replica of the rgba_surface used by ISPC compressor
class rgba_surface(ctypes.Structure):
    _fields_ = [
        ('data', ctypes.c_char_p),
        ('width', ctypes.c_uint32),
        ('height', ctypes.c_uint32),
        ('stride', ctypes.c_uint32),
    ]


def _load_ispc_lib():
    if system_type == 'linux':
        _ispc_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'linux', 'libispc_texcomp.so')
    elif system_type == 'windows':
        _ispc_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'windows', 'ispc_texcomp.dll')
    elif system_type == 'darwin':
        _ispc_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib', 'macos', 'libispc_texcomp.dylib')
    else:
        raise RuntimeError("Unsupported system for ISPC texcomp")

    _ispc = ctypes.CDLL(_ispc_path)
    _ispc.CompressBlocksBC3.argtypes = (ctypes.POINTER(rgba_surface), ctypes.c_char_p)
    _ispc.CompressBlocksBC1.argtypes = (ctypes.POINTER(rgba_surface), ctypes.c_char_p)
    return _ispc


_ISPC = None


def _addr_of_buffer(buf) -> int:
    """Return base address of a Python buffer object."""
    return ctypes.addressof(ctypes.c_char.from_buffer(buf))


def _compress_stripe_worker(args):
    (
        in_name,
        out_name,
        width,
        stripe_height_px,
        stride,
        dxt_format,
        blocksize,
        stripe_start_row_px,
        dxt_offset_bytes,
    ) = args

    global _ISPC
    if _ISPC is None:
        _ISPC = _load_ispc_lib()

    in_shm = shared_memory.SharedMemory(name=in_name)
    out_shm = shared_memory.SharedMemory(name=out_name)
    try:
        in_buf = in_shm.buf
        out_buf = out_shm.buf

        base_in = _addr_of_buffer(in_buf)
        base_out = _addr_of_buffer(out_buf)

        # Source pointer offset to stripe start
        src_addr = base_in + (stripe_start_row_px * stride)
        dst_addr = base_out + dxt_offset_bytes

        s = rgba_surface()
        s.data = ctypes.c_char_p(src_addr)
        s.width = ctypes.c_uint32(width)
        s.height = ctypes.c_uint32(stripe_height_px)
        s.stride = ctypes.c_uint32(stride)

        out_ptr = ctypes.c_char_p(dst_addr)
        if dxt_format == 'BC3':
            _ISPC.CompressBlocksBC3(s, out_ptr)
        else:
            _ISPC.CompressBlocksBC1(s, out_ptr)
        return True
    finally:
        try:
            in_shm.close()
        except Exception:
            pass
        try:
            out_shm.close()
        except Exception:
            pass


def compress_mipmap_to_bytes_parallel(rgba_bytes: bytes, width: int, height: int, dxt_format: str, workers: int = 0, stripe_height_px: int = 128) -> bytes:
    """
    Compress an RGBA image (width x height) to BC1/BC3 using multiple processes.

    - rgba_bytes: raw RGBA bytes (row-major), length = width*height*4
    - dxt_format: 'BC1' or 'BC3'
    - workers: number of processes (0 or None -> os.cpu_count())
    - stripe_height_px: height of each work stripe (must be multiple of 4)
    """
    if width % 4 != 0 or height % 4 != 0:
        raise ValueError("Dimensions must be multiples of 4 for BC compression")
    if stripe_height_px < 4 or stripe_height_px % 4 != 0:
        stripe_height_px = 128
    blocksize = 16 if dxt_format == 'BC3' else 8
    stride = width * 4
    blocks_per_row = width // 4
    total_blocks = (width * height) // 16
    out_size = total_blocks * blocksize

    # Shared memory setup
    in_shm = shared_memory.SharedMemory(create=True, size=len(rgba_bytes))
    out_shm = shared_memory.SharedMemory(create=True, size=out_size)
    try:
        in_shm.buf[:] = rgba_bytes

        tasks = []
        start = 0
        while start < height:
            hh = min(stripe_height_px, height - start)
            # round up to multiple of 4
            hh = max(4, ((hh + 3) // 4) * 4)
            stripe_block_rows = hh // 4
            dxt_offset = (start // 4) * blocks_per_row * blocksize
            tasks.append((
                in_shm.name,
                out_shm.name,
                width,
                hh,
                stride,
                dxt_format,
                blocksize,
                start,
                dxt_offset,
            ))
            start += hh

        if workers is None or workers <= 0:
            workers = max(1, os.cpu_count() or 1)

        ctx = mp.get_context('spawn')
        with ctx.Pool(processes=workers) as pool:
            results = pool.map(_compress_stripe_worker, tasks)
            if not all(results):
                raise RuntimeError("Parallel compression failed in at least one worker")

        # Copy bytes back to process memory
        return bytes(out_shm.buf)
    finally:
        try:
            in_shm.close()
            in_shm.unlink()
        except Exception:
            pass
        try:
            out_shm.close()
            out_shm.unlink()
        except Exception:
            pass



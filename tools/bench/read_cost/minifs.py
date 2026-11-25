"""A minimal read-only passthrough file system, for the read-cost benchmark.

It mounts one folder through the same binding AutoOrtho uses (mfusepy over
WinFsp or Dokan) and does nothing else, so a read through it costs the kernel
round trip plus the least a callback can cost:

``--read python``
    The read callback is Python: take a lock, seek, read (what AutoOrtho's
    ``DiskTexture`` does on Windows, with none of its checks).
``--read native``
    The read callback is ``aoprobe.dll``: the ``read`` field of mfusepy's
    ``fuse_operations`` struct is replaced before the mount starts, and reads
    never enter Python. Open, getattr and release stay Python.

``--spin N`` starts N threads that run Python in a loop, to show what a read
costs while other Python threads hold the GIL.

Stop it by opening ``<mount>/.poison``.

    python minifs.py --root D:/cache/dds --mountpoint D:/tmp/m --read native --lib aoprobe.dll

On Linux and macOS ``--fuse-lib`` may be left out (libfuse / macFUSE as
mfusepy finds them) and the native read uses the file descriptor with pread.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import stat
import sys
import threading
import time


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--read", choices=("python", "native"), default="python")
    parser.add_argument("--lib", help="aoprobe.dll, for --read native")
    parser.add_argument("--fuse-lib", required=(os.name == "nt"),
                        help="winfsp-x64.dll or dokanfuse2.dll (Windows)")
    parser.add_argument("--spin", type=int, default=0)
    parser.add_argument("--stats", help="JSON written once a second")
    args = parser.parse_args(argv)

    if args.fuse_lib:
        os.environ["FUSE_LIBRARY_PATH"] = args.fuse_lib
    import mfusepy
    from mfusepy import FUSE, FuseOSError, Operations

    root = os.path.abspath(args.root)
    native = args.read == "native"
    counts = {"getattr": 0, "open": 0, "read": 0, "release": 0, "read_ns": 0}

    windows = os.name == "nt"
    if windows:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    class Mini(Operations):
        def __init__(self):
            self.locks = {}

        def _full(self, path):
            return os.path.join(root, path.lstrip("/\\"))

        def getattr(self, path, fh=None):
            counts["getattr"] += 1
            if path.endswith(".poison"):
                fuse_ptr = ctypes.c_void_p(mfusepy._libfuse.fuse_get_context().contents.fuse)
                mfusepy._libfuse.fuse_exit(fuse_ptr)
                return {"st_mode": stat.S_IFREG | 0o444, "st_size": 0, "st_nlink": 1}
            try:
                st = os.stat(self._full(path))
            except OSError:
                raise FuseOSError(errno.ENOENT)
            return {
                "st_mode": (stat.S_IFDIR | 0o555) if stat.S_ISDIR(st.st_mode) else (stat.S_IFREG | 0o444),
                "st_size": st.st_size, "st_nlink": 1,
                "st_atime": st.st_atime, "st_mtime": st.st_mtime, "st_ctime": st.st_ctime,
            }

        def readdir(self, path, fh):
            return [".", ".."] + os.listdir(self._full(path))

        def open(self, path, flags):
            counts["open"] += 1
            full = self._full(path)
            if native and windows:
                handle = kernel32.CreateFileW(full, 0x80000000, 7, None, 3, 0x80, None)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise FuseOSError(errno.ENOENT)
                return handle
            try:
                fd = os.open(full, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            except OSError:
                raise FuseOSError(errno.ENOENT)
            if native:
                return fd  # aoprobe reads it with pread
            self.locks[fd] = threading.Lock()
            return fd

        def read(self, path, length, offset, fh):
            started = time.perf_counter_ns()
            with self.locks[fh]:
                os.lseek(fh, offset, os.SEEK_SET)
                data = os.read(fh, length)
            counts["read"] += 1
            counts["read_ns"] += time.perf_counter_ns() - started
            return data

        def release(self, path, fh):
            counts["release"] += 1
            if native and windows:
                kernel32.CloseHandle(fh)
            elif native:
                os.close(fh)
            else:
                self.locks.pop(fh, None)
                os.close(fh)
            return 0

    lib = None
    if native:
        lib = ctypes.CDLL(os.path.abspath(args.lib))
        lib.aoprobe_init(mfusepy.fuse_file_info.fh.offset)
        lib.aoprobe_reads.restype = ctypes.c_int64
        real_main = mfusepy.fuse_main_real

        def patched_main(argc, argv_, ops_pointer, size, ctx):
            ops = ops_pointer.contents
            prototype = type(ops.read)
            ops.read = ctypes.cast(lib.aoprobe_read, prototype)
            return real_main(argc, argv_, ops_pointer, size, ctx)

        mfusepy.fuse_main_real = patched_main

    def spin():
        x = 0
        while True:
            for i in range(10000):
                x += i * i

    for _ in range(args.spin):
        threading.Thread(target=spin, daemon=True).start()

    def write_stats():
        while True:
            time.sleep(1.0)
            out = dict(counts)
            if lib is not None:
                out["native_reads"] = int(lib.aoprobe_reads())
            try:
                tmp = args.stats + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(out, f)
                os.replace(tmp, args.stats)
            except OSError:
                pass

    if args.stats:
        threading.Thread(target=write_stats, daemon=True).start()

    name = "aominifs"
    FUSE(Mini(), os.path.abspath(args.mountpoint), foreground=True, allow_other=True,
         VolumeName=name, FileSystemName=name, uid=-1, gid=-1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

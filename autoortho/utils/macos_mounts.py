"""Inspect Darwin's cached mount table without making filesystem requests."""

import ctypes
import os
from functools import lru_cache
from typing import NamedTuple


class MountEntry(NamedTuple):
    mountpoint: str
    source: str
    fstype: str


class _StatFS(ctypes.Structure):
    # Darwin's 64-bit struct statfs (sys/mount.h), on arm64 and x86_64.
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


@lru_cache(maxsize=1)
def _getfsstat():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    # Use the explicit 64-bit ABI, rather than the legacy getfsstat symbol.
    fn = lib.getfsstat64
    fn.argtypes = [ctypes.POINTER(_StatFS), ctypes.c_int, ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn


def read_mount_table():
    """Return cached entries using MNT_NOWAIT; never stat any mounted path.

    Errors propagate: an unavailable table is not proof of an unmounted path.
    A private buffer also avoids getmntinfo's process-global buffer races.
    """
    fn = _getfsstat()
    count = fn(None, 0, 2)  # MNT_NOWAIT, not MNT_WAIT (used by `mount`).
    if count < 0:
        raise _mount_error()
    for _ in range(3):
        capacity = count + 8
        entries = (_StatFS * capacity)()
        count = fn(entries, ctypes.sizeof(entries), 2)
        if count < 0:
            raise _mount_error()
        if count < capacity:
            return [
                MountEntry(
                    os.fsdecode(entry.f_mntonname),
                    os.fsdecode(entry.f_mntfromname),
                    os.fsdecode(entry.f_fstypename).lower(),
                )
                for entry in entries[:count]
            ]
    raise OSError("Mount table kept growing during inspection")


def _mount_error():
    code = ctypes.get_errno()
    return OSError(code, os.strerror(code))


def mount_path(path):
    """Resolve parent aliases, without resolving or statting the mount itself."""
    path = os.path.abspath(os.path.expanduser(os.fsdecode(path)))
    if path == os.path.sep:
        return path
    parent, leaf = os.path.split(path)
    return os.path.join(os.path.realpath(parent), leaf)


def find_mount(path):
    target = mount_path(path)
    return next(
        (entry for entry in read_mount_table()
         if os.path.normpath(entry.mountpoint) == target),
        None,
    )

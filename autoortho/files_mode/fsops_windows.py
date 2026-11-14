"""Windows file system calls through ctypes (sparse files, renames, holes).

Measured on Windows 11 / NTFS while building files mode: a name another
process holds mapped cannot be replaced, even with POSIX-semantics rename
(ERROR_SHARING_VIOLATION); a sibling hard link of the same mapped file can;
and in-place writes into a mapped file succeed. See ``store.py`` for how
that shapes the writer.
"""

import ctypes
import msvcrt
import os
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_SHARE_ALL = 0x7
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

FSCTL_SET_SPARSE = 0x000900C4
FSCTL_SET_ZERO_DATA = 0x000980C8
FILE_RENAME_INFO_EX = 22  # FileRenameInfoEx
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x1
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x2
MOVEFILE_REPLACE_EXISTING = 0x1

ERROR_SHARING_VIOLATION = 32
ERROR_ACCESS_DENIED = 5
ERROR_TOO_MANY_LINKS = 1142
ERROR_INVALID_PARAMETER = 87

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.SetFileInformationByHandle.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
    wintypes.DWORD,
]
kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]


def _long(path):
    path = os.path.abspath(path)
    if not path.startswith("\\\\?\\") and len(path) > 240:
        return "\\\\?\\" + path
    return path


def set_sparse_fd(fd):
    """Mark an open file sparse. Must come before extending it."""
    handle = msvcrt.get_osfhandle(fd)
    returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle, FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(returned), None
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


class _ZeroData(ctypes.Structure):
    _fields_ = [("FileOffset", ctypes.c_longlong), ("BeyondFinalZero", ctypes.c_longlong)]


def zero_range_fd(fd, offset, length):
    """Deallocate ``[offset, offset+length)`` of a sparse file (a hole)."""
    handle = msvcrt.get_osfhandle(fd)
    data = _ZeroData(int(offset), int(offset + length))
    returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle, FSCTL_SET_ZERO_DATA, ctypes.byref(data), ctypes.sizeof(data),
        None, 0, ctypes.byref(returned), None,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


FILE_END_OF_FILE_INFO = 6   # FileEndOfFileInfo
FILE_STANDARD_INFO = 1      # FileStandardInfo
kernel32.GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]


class _StandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


def set_size_fd(fd, size):
    """Set the end of file without writing zeros.

    ``os.ftruncate`` on Windows extends through the C runtime, which writes
    zeros up to the new size: a sparse file then becomes fully allocated.
    """
    handle = msvcrt.get_osfhandle(fd)
    value = ctypes.c_longlong(int(size))
    if not kernel32.SetFileInformationByHandle(
        handle, FILE_END_OF_FILE_INFO, ctypes.byref(value), ctypes.sizeof(value)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _standard_info_fd(fd):
    handle = msvcrt.get_osfhandle(fd)
    info = _StandardInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle, FILE_STANDARD_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def link_count_fd(fd):
    return int(_standard_info_fd(fd).NumberOfLinks)


FSCTL_QUERY_ALLOCATED_RANGES = 0x000940CF
ERROR_MORE_DATA = 234
ALLOCATION_UNIT = 65536


class _Range(ctypes.Structure):
    _fields_ = [("FileOffset", ctypes.c_longlong), ("Length", ctypes.c_longlong)]


def allocated_ranges_fd(fd, size):
    """Allocated ``(offset, length)`` ranges of a file (as ``fsutil sparse``)."""
    handle = msvcrt.get_osfhandle(fd)
    ranges = []
    query = _Range(0, int(size))
    out = (_Range * 64)()
    while query.Length > 0:
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle, FSCTL_QUERY_ALLOCATED_RANGES, ctypes.byref(query),
            ctypes.sizeof(query), out, ctypes.sizeof(out), ctypes.byref(returned), None,
        )
        err = 0 if ok else ctypes.get_last_error()
        if not ok and err != ERROR_MORE_DATA:
            raise ctypes.WinError(err)
        count = returned.value // ctypes.sizeof(_Range)
        for i in range(count):
            ranges.append((int(out[i].FileOffset), int(out[i].Length)))
        if ok or count == 0:
            break
        last_end = out[count - 1].FileOffset + out[count - 1].Length
        query = _Range(last_end, int(size) - last_end)
    return ranges


def allocated_size(path):
    """Allocated bytes, from the allocated ranges.

    ``GetCompressedFileSizeW`` and ``FileStandardInfo.AllocationSize`` both
    misreport sparse files on NTFS (measured: a stale 64 KB and 0), so the
    ranges are summed instead, rounded to NTFS's 64 KB sparse unit.
    """
    fd = os.open(_long(path), os.O_RDONLY | os.O_BINARY)
    try:
        size = os.fstat(fd).st_size
        total = 0
        for offset, length in allocated_ranges_fd(fd, size):
            start = offset // ALLOCATION_UNIT * ALLOCATION_UNIT
            end = -(-(offset + length) // ALLOCATION_UNIT) * ALLOCATION_UNIT
            total += end - start
        return total
    finally:
        os.close(fd)


def _rename_info(dst):
    dst = _long(dst)
    size = ctypes.sizeof(wintypes.DWORD) * 2 + ctypes.sizeof(wintypes.HANDLE) + (len(dst) + 1) * 2

    class _Info(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (len(dst) + 1)),
        ]

    info = _Info()
    info.Flags = FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
    info.RootDirectory = None
    info.FileNameLength = len(dst) * 2
    info.FileName = dst
    return info, max(size, ctypes.sizeof(info))


def posix_rename(src, dst):
    """Replace ``dst`` with ``src`` using POSIX semantics. Returns error code (0: ok)."""
    handle = kernel32.CreateFileW(
        _long(src), DELETE | GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED
    try:
        info, size = _rename_info(dst)
        ok = kernel32.SetFileInformationByHandle(
            handle, FILE_RENAME_INFO_EX, ctypes.byref(info), size
        )
        return 0 if ok else (ctypes.get_last_error() or ERROR_ACCESS_DENIED)
    finally:
        kernel32.CloseHandle(handle)


def move_replace(src, dst, retry_s=2.0, step_s=0.05):
    """``MoveFileExW`` replace, retried. Returns the last error code (0: ok)."""
    deadline = time.monotonic() + max(0.0, retry_s)
    while True:
        if kernel32.MoveFileExW(_long(src), _long(dst), MOVEFILE_REPLACE_EXISTING):
            return 0
        err = ctypes.get_last_error()
        if err not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED) or time.monotonic() >= deadline:
            return err
        time.sleep(step_s)


def replace(src, dst, retry_s=2.0):
    """Best replace on Windows: POSIX rename first, then a retried move."""
    err = posix_rename(src, dst)
    if err == 0:
        return 0
    if err in (ERROR_INVALID_PARAMETER, 1):  # FileRenameInfoEx unsupported
        return move_replace(src, dst, retry_s=retry_s)
    if err in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED):
        return move_replace(src, dst, retry_s=retry_s)
    return err


def volume_fs_type(path):
    buf = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(os.path.abspath(path), buf, 1024):
        return ""
    name = ctypes.create_unicode_buffer(64)
    serial = wintypes.DWORD()
    maxlen = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationW(
        buf.value, None, 0, ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), name, 64,
    ):
        return ""
    return name.value


def create_junction(target, link):
    import _winapi
    _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))

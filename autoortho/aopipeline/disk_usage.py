"""
disk_usage.py - The disk space a file really uses

A partial DDS cache entry is a sparse file: its size is the whole texture
(44.7 MB at 8,192 px) while only the coarse levels are written, about
0.1 MB. A budget that adds up ``st_size`` counts 22.6 GB for a cache that
uses 1.5 GB and evicts to stay under a limit it never reached
(docs/developer/ready-share-plan.md, R1). Everything that sizes the cache
uses :func:`allocated_size` instead.
"""

import os

if os.name == "nt":
    import ctypes
    import stat as _stat
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetCompressedFileSizeW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    _INVALID_FILE_SIZE = 0xFFFFFFFF
    _SPARSE = _stat.FILE_ATTRIBUTE_SPARSE_FILE

    def _extended(path: str) -> str:
        """``path`` in the form that has no 260 character limit, if it needs it."""
        path = os.fspath(path)
        if len(path) < 248 or path.startswith("\\\\"):
            return path
        return "\\\\?\\" + os.path.abspath(path)


def allocated_size(path, st=None) -> int:
    """Bytes of disk the file at ``path`` uses.

    ``st`` is the file's ``os.stat_result`` when the caller has one (from
    ``os.stat`` or ``DirEntry.stat``); it is read here otherwise, and an
    ``OSError`` from that is the caller's to handle.

    A file that is not sparse counts as its size, as before, at no extra
    cost: on Windows the attributes in ``st`` say whether it is sparse, and
    only then is the system asked what is allocated. Elsewhere ``st_blocks``
    answers. Never more than the file's size, so a small file does not count
    as a whole cluster. On a volume without sparse files (FAT32, exFAT) the
    answer is the size.
    """
    if st is None:
        st = os.stat(path)
    size = st.st_size
    if os.name == "nt":
        if not getattr(st, "st_file_attributes", 0) & _SPARSE:
            return size
        high = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        low = _k32.GetCompressedFileSizeW(_extended(path), ctypes.byref(high))
        if low == _INVALID_FILE_SIZE and ctypes.get_last_error() != 0:
            return size
        return min(size, (high.value << 32) | low)
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return size
    return min(size, blocks * 512)

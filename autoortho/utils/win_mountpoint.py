"""Remove volume mount points whose volume no longer exists (Windows).

A Dokan directory mount is an NTFS mount-point reparse point on the folder
that targets ``\\??\\Volume{GUID}\\``. When the filesystem stops, Dokan removes
that reparse point through the Mount Manager, which needs administrator
rights. Without them the reparse point stays behind: the folder still reports
as a mount point, every access fails with ERROR_NO_SUCH_DEVICE (433), and
X-Plane and AutoOrtho both treat the scenery as still mounted.

The folder's owner can delete the reparse point directly, which leaves an
ordinary empty folder. We only do that once the target volume device is gone;
the check never opens the folder or a file on the volume.
"""
import logging
import os
import re
import sys

log = logging.getLogger(__name__)

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_FSCTL_DELETE_REPARSE_POINT = 0x000900AC
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
# ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, ERROR_NO_SUCH_DEVICE
_VOLUME_GONE_ERRORS = (2, 3, 433)
_ERROR_NOT_A_REPARSE_POINT = 4390

_VOLUME_TARGET = re.compile(r"^\\\?\?\\(Volume\{[0-9A-Fa-f-]+\})\\?$")

_kernel32 = None


def _k32():
    global _kernel32
    if _kernel32 is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.DeviceIoControl.restype = wintypes.BOOL
        k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32 = k32
    return _kernel32


def _open_reparse_point(path, access):
    import ctypes
    from ctypes import wintypes

    k32 = _k32()
    handle = k32.CreateFileW(
        path, access, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def mount_point_volume(path):
    """Return the ``Volume{GUID}`` a mount-point reparse point targets.

    None when the path is not a volume mount point (a plain folder, a junction
    to another folder, a symlink, missing).
    """
    try:
        if os.lstat(path).st_reparse_tag != IO_REPARSE_TAG_MOUNT_POINT:
            return None
    except (OSError, AttributeError, ValueError, TypeError):
        return None

    import ctypes
    from ctypes import wintypes

    k32 = _k32()
    handle = _open_reparse_point(path, 0)
    try:
        buf = ctypes.create_string_buffer(16 * 1024)
        size = wintypes.DWORD()
        if not k32.DeviceIoControl(
            handle, _FSCTL_GET_REPARSE_POINT, None, 0, buf, len(buf),
            ctypes.byref(size), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(handle)

    raw = buf.raw[:size.value]
    if len(raw) < 16 or int.from_bytes(raw[0:4], "little") != IO_REPARSE_TAG_MOUNT_POINT:
        return None
    offset = int.from_bytes(raw[8:10], "little")
    length = int.from_bytes(raw[10:12], "little")
    target = raw[16 + offset:16 + offset + length].decode("utf-16-le", "replace")
    match = _VOLUME_TARGET.match(target)
    return match.group(1) if match else None


def volume_exists(volume):
    """False only when ``Volume{GUID}`` provably no longer exists.

    The name link can outlive the volume, so open the volume device itself.
    With no access rights requested this opens the device, not a file on it:
    it succeeds at once on a live Dokan or WinFsp volume and fails with
    ERROR_NO_SUCH_DEVICE once the filesystem has gone.
    """
    import ctypes
    from ctypes import wintypes

    k32 = _k32()
    handle = k32.CreateFileW(
        "\\\\.\\" + volume, 0, _FILE_SHARE_ALL, None, _OPEN_EXISTING, 0, None,
    )
    if handle != wintypes.HANDLE(-1).value:
        k32.CloseHandle(handle)
        return True
    # Any other error (access denied, ...) does not prove the volume is gone.
    return ctypes.get_last_error() not in _VOLUME_GONE_ERRORS


def remove_stale_mount_point(path):
    """Delete a mount point whose volume is gone. True if one was removed.

    Leaves live mounts, junctions to folders and plain folders untouched.
    """
    if sys.platform != "win32" or not path:
        return False
    try:
        volume = mount_point_volume(path)
        if volume is None or volume_exists(volume):
            return False

        import ctypes
        from ctypes import wintypes

        k32 = _k32()
        handle = _open_reparse_point(path, _GENERIC_WRITE)
        try:
            # REPARSE_GUID_DATA_BUFFER header with no data: tag, length 0.
            header = IO_REPARSE_TAG_MOUNT_POINT.to_bytes(4, "little") + bytes(4)
            returned = wintypes.DWORD()
            if not k32.DeviceIoControl(
                handle, _FSCTL_DELETE_REPARSE_POINT, header, len(header),
                None, 0, ctypes.byref(returned), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k32.CloseHandle(handle)
    except OSError as exc:
        if getattr(exc, "winerror", None) == _ERROR_NOT_A_REPARSE_POINT:
            # The worker and the parent both clean up; the other one won.
            log.debug("Stale mount point already removed: %s", path)
            return True
        log.warning("Could not remove stale mount point %s: %s", path, exc)
        return False
    log.info("Removed stale mount point left after unmount: %s (%s)", path, volume)
    return True

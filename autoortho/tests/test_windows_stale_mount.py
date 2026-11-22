"""Mount points Dokan leaves behind when AutoOrtho runs without admin rights.

Dokan removes its directory mount point through the Mount Manager, which needs
administrator rights. Without them the folder keeps a mount-point reparse point
to a volume that no longer exists, so it still reports as mounted and
"AutoOrtho could not stop streaming cleanly" (3.4.1 user report).

These tests build that state with a plain reparse point, no Dokan needed.
"""
import ctypes
import importlib
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import mount_utils, win_mountpoint  # noqa: E402

_FSCTL_SET_REPARSE_POINT = 0x000900A4


def _set_mount_point(path, target):
    """Make ``path`` an NTFS mount point (junction) to ``target`` (NT path)."""
    from ctypes import wintypes

    k32 = win_mountpoint._k32()
    sub = target.encode("utf-16-le")
    names = sub + b"\0\0" + b"\0\0"  # substitute name, empty print name
    data = (
        (0).to_bytes(2, "little") + len(sub).to_bytes(2, "little")
        + (len(sub) + 2).to_bytes(2, "little") + (0).to_bytes(2, "little")
        + names
    )
    buf = (
        win_mountpoint.IO_REPARSE_TAG_MOUNT_POINT.to_bytes(4, "little")
        + len(data).to_bytes(2, "little") + bytes(2) + data
    )
    handle = win_mountpoint._open_reparse_point(str(path), win_mountpoint._GENERIC_WRITE)
    try:
        returned = wintypes.DWORD()
        if not k32.DeviceIoControl(
            handle, _FSCTL_SET_REPARSE_POINT, buf, len(buf), None, 0,
            ctypes.byref(returned), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(handle)


def _clear_mount_point(path):
    from ctypes import wintypes

    k32 = win_mountpoint._k32()
    handle = win_mountpoint._open_reparse_point(str(path), win_mountpoint._GENERIC_WRITE)
    try:
        header = win_mountpoint.IO_REPARSE_TAG_MOUNT_POINT.to_bytes(4, "little") + bytes(4)
        returned = wintypes.DWORD()
        k32.DeviceIoControl(
            handle, win_mountpoint._FSCTL_DELETE_REPARSE_POINT, header,
            len(header), None, 0, ctypes.byref(returned), None,
        )
    finally:
        k32.CloseHandle(handle)


@pytest.fixture
def dead_mount(tmp_path):
    """A scenery folder still pointing at a volume that no longer exists."""
    mountpoint = tmp_path / "z_ao_stream_xp12"
    mountpoint.mkdir()
    _set_mount_point(mountpoint, f"\\??\\Volume{{{uuid.uuid4()}}}\\")
    yield str(mountpoint)
    if os.lstat(mountpoint).st_reparse_tag:
        _clear_mount_point(mountpoint)


def test_dead_mount_point_reads_as_mounted_and_is_removed(dead_mount):
    assert mount_utils.safe_ismount(dead_mount)
    with pytest.raises(OSError):
        os.listdir(dead_mount)

    assert win_mountpoint.remove_stale_mount_point(dead_mount)

    assert not mount_utils.safe_ismount(dead_mount)
    assert os.listdir(dead_mount) == []


def test_mount_point_to_live_volume_is_left_alone(tmp_path):
    from ctypes import wintypes

    k32 = win_mountpoint._k32()
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    ]
    name = ctypes.create_unicode_buffer(64)
    drive = os.path.splitdrive(str(tmp_path))[0] + "\\"
    if not k32.GetVolumeNameForVolumeMountPointW(drive, name, len(name)):
        pytest.skip("temp folder is not on a volume with a GUID name")
    live = name.value.replace("\\\\?\\", "\\??\\", 1)

    mountpoint = tmp_path / "z_ao_live"
    mountpoint.mkdir()
    _set_mount_point(mountpoint, live)
    try:
        assert win_mountpoint.mount_point_volume(str(mountpoint)) is not None
        assert not win_mountpoint.remove_stale_mount_point(str(mountpoint))
        assert os.lstat(mountpoint).st_reparse_tag == win_mountpoint.IO_REPARSE_TAG_MOUNT_POINT
    finally:
        _clear_mount_point(mountpoint)


def test_junction_to_a_folder_is_left_alone(tmp_path):
    target = tmp_path / "real_scenery"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    junction = tmp_path / "z_ao_junction"
    junction.mkdir()
    _set_mount_point(junction, "\\??\\" + str(target))
    try:
        assert win_mountpoint.mount_point_volume(str(junction)) is None
        assert not win_mountpoint.remove_stale_mount_point(str(junction))
        assert os.listdir(junction) == ["keep.txt"]
    finally:
        _clear_mount_point(junction)


def test_plain_and_missing_folders_are_ignored(tmp_path):
    assert not win_mountpoint.remove_stale_mount_point(str(tmp_path))
    assert not win_mountpoint.remove_stale_mount_point(str(tmp_path / "missing"))
    assert not win_mountpoint.remove_stale_mount_point(None)


def test_already_removed_by_the_other_process_counts_as_removed(dead_mount, monkeypatch):
    # The worker and the parent both clean up; whoever loses must not warn.
    real_open = win_mountpoint._open_reparse_point

    def open_after_other_process_won(path, access):
        if access == win_mountpoint._GENERIC_WRITE:
            monkeypatch.setattr(win_mountpoint, "_open_reparse_point", real_open)
            _clear_mount_point(path)
        return real_open(path, access)

    monkeypatch.setattr(win_mountpoint, "_open_reparse_point", open_after_other_process_won)
    assert win_mountpoint.remove_stale_mount_point(dead_mount)


def test_worker_cleanup_restores_the_placeholder(dead_mount):
    assert mount_utils.cleanup_mountpoint(dead_mount)
    assert sorted(os.listdir(dead_mount)) == sorted(
        [".AO_PLACEHOLDER", "Earth nav data", "terrain", "textures"]
    )


def test_force_unmount_needs_no_admin_tools(dead_mount, monkeypatch):
    winsetup = importlib.import_module("winsetup")

    def no_tools(*args, **kwargs):
        raise AssertionError("dokanctl/fsptool should not be needed")

    monkeypatch.setattr(winsetup.subprocess, "run", no_tools)
    winsetup.force_unmount(dead_mount)
    assert not mount_utils.safe_ismount(dead_mount)


def test_parent_unmount_succeeds_without_waiting(dead_mount, monkeypatch):
    module = importlib.import_module("autoortho")
    monkeypatch.setattr(module, "system_type", "windows")

    started = time.monotonic()
    assert module.AOMount.unmount(None, dead_mount, wait_timeout=8.0)
    assert time.monotonic() - started < 2.0

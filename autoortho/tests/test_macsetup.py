"""Mount detection must support macFUSE and FUSE-T's local NFS backend."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macsetup


@pytest.mark.parametrize("fstype", ["macfuse", "osxfuse", "fusefs", "fuse-t"])
def test_native_fuse_mount(monkeypatch, tmp_path, fstype):
    mountpoint = tmp_path / "Custom Scenery" / "z_ao"
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw:
                        f"autoortho on {mountpoint} ({fstype}, local, nodev)\n")
    assert macsetup.is_macfuse_mount(str(mountpoint))


@pytest.mark.parametrize("host", ["fuse-t", "localhost", "127.0.0.1", "[::1]"])
def test_fuse_t_local_nfs(monkeypatch, tmp_path, host):
    mountpoint = tmp_path / "Custom Scenery" / "z_ao"
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw:
                        f"{host}:/autoortho on {mountpoint} (nfs, nodev, nosuid, mounted by user)\n")
    assert macsetup.is_macfuse_mount(str(mountpoint))


@pytest.mark.parametrize("source,fstype,suffix", [
    ("server:/export", "nfs", ""),
    ("fuse-t.example.com:/export", "nfs", ""),
    ("fuse-t:/export", "apfs", ""),
    ("fuse-t:/export", "nfs", "_other"),
    ("localhost:/export", "apfs", ""),
    ("localhost:/export", "nfs", "_other"),
    ("/dev/disk3s5", "apfs", ""),
])
def test_unrelated_mounts_are_rejected(monkeypatch, tmp_path, source, fstype, suffix):
    mountpoint = tmp_path / "z_ao"
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw:
                        f"{source} on {mountpoint}{suffix} ({fstype}, local)\n")
    assert not macsetup.is_macfuse_mount(str(mountpoint))


def test_symlink_parent_and_trailing_slash(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw:
                        f"localhost:/ao on {real}/z_ao (nfs, nodev)\n")
    assert macsetup.is_macfuse_mount(str(alias / "z_ao") + os.sep)


def test_mount_command_failure(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "mount")
    monkeypatch.setattr(subprocess, "check_output", fail)
    assert not macsetup.is_macfuse_mount(str(tmp_path))


def test_multiple_mountpoints_are_checked_independently(monkeypatch, tmp_path):
    # Format captured from a real FUSE-T mount, including spaces in the path.
    scenery = tmp_path / "X-Plane 12" / "Custom Scenery"
    eur, na, missing = [scenery / name for name in ("z_ao_eur", "z_ao_na", "z_ao_asi")]
    table = (
        f"fuse-t:/z_ao_eur on {eur} (nfs, nodev, nosuid, mounted by user)\n"
        f"fuse-t:/z_ao_na on {na} (nfs, nodev, nosuid, mounted by user)\n"
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: table)
    assert macsetup.is_macfuse_mount(str(eur))
    assert macsetup.is_macfuse_mount(str(na))
    assert not macsetup.is_macfuse_mount(str(missing))


def test_empty_mount_table(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "")
    assert not macsetup.is_macfuse_mount(str(tmp_path))

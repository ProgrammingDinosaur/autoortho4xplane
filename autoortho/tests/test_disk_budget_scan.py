"""
test_disk_budget_scan.py - The disk budget's walk of the cache folder

The scan that sizes the cache and picks imagery files to evict: it finds the
same files a plain walk does, leaves out the DDS cache where asked, and never
goes through a linked directory (a junction or a symlink) to files outside
the cache folder.
"""

import os
import subprocess
import sys

import pytest

# Add repository root to path for package-style imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from autoortho.aopipeline import disk_budget_manager  # noqa: E402
from autoortho.aopipeline.disk_budget_manager import (  # noqa: E402
    DiskBudgetManager,
    _iter_file_entries,
)


def _write(path, size=10, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _names(root, skip=None):
    return sorted(entry.name for entry in _iter_file_entries(str(root), skip))


def _link_directory(link, target):
    """Make ``link`` point at ``target``; skips the test where that is not allowed."""
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("cannot create a junction here")
    else:
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except OSError:
            pytest.skip("cannot create a symlink here")


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "cache"
    _write(str(root / "top.jpg"), 10)
    _write(str(root / "BI" / "16" / "a.jpg"), 20)
    _write(str(root / "BI" / "16" / "b.jpeg"), 30)
    _write(str(root / "BI" / "17" / "notes.txt"), 40)
    _write(str(root / "dds_cache" / "BI" / "t.dds"), 500)
    _write(str(root / "dds_cache" / "BI" / "inside.jpg"), 50)
    return root


def test_scan_finds_what_a_plain_walk_finds(cache):
    walked = sorted(
        name for _dir, _subdirs, files in os.walk(str(cache)) for name in files
    )
    assert _names(cache) == walked
    assert len(walked) == 6


def test_scan_entries_carry_path_and_size(cache):
    sizes = {
        os.path.relpath(entry.path, str(cache)).replace(os.sep, "/"): entry.stat().st_size
        for entry in _iter_file_entries(str(cache))
    }
    assert sizes["BI/16/a.jpg"] == 20
    assert sizes["dds_cache/BI/t.dds"] == 500


def test_scan_leaves_out_a_named_subtree(cache):
    assert _names(cache, "dds_cache") == ["a.jpg", "b.jpeg", "notes.txt", "top.jpg"]


def test_scan_of_a_missing_folder_is_empty(tmp_path):
    assert _names(tmp_path / "nowhere") == []


def test_scan_does_not_follow_a_linked_directory(cache, tmp_path):
    outside = tmp_path / "outside"
    _write(str(outside / "elsewhere.jpg"), 70)
    _link_directory(cache / "BI" / "link", outside)
    # The link is there and a plain walk would go through it on Windows.
    assert os.path.isfile(str(cache / "BI" / "link" / "elsewhere.jpg"))

    assert "elsewhere.jpg" not in _names(cache)


def test_dir_size_counts_only_the_extensions_asked_for(cache):
    assert DiskBudgetManager._scan_dir_size(str(cache / "dds_cache"), ".dds") == 500
    assert DiskBudgetManager._scan_dir_size(str(cache), (".jpg", ".jpeg")) == 110
    assert DiskBudgetManager._scan_dir_size(str(cache / "nowhere"), ".dds") == 0


def test_jpeg_size_skips_the_dds_cache_and_protected_files(cache):
    manager = DiskBudgetManager(cache_dir=str(cache), total_budget_mb=1000)
    assert manager._scan_jpeg_files_size() == 60  # top + a + b, not inside.jpg

    manager.protect_jpeg = lambda name: name == "a.jpg"
    assert manager._scan_jpeg_files_size() == 40


def test_jpeg_eviction_removes_the_oldest_first(tmp_path):
    root = tmp_path / "cache"
    old = _write(str(root / "BI" / "old.jpg"), 100, mtime=1_000_000)
    mid = _write(str(root / "BI" / "mid.jpg"), 100, mtime=2_000_000)
    new = _write(str(root / "new.jpg"), 100, mtime=3_000_000)
    kept = _write(str(root / "dds_cache" / "kept.jpg"), 100, mtime=1)
    manager = DiskBudgetManager(cache_dir=str(root), total_budget_mb=1000)

    assert manager._evict_oldest_jpeg_files(150) == 200

    assert not os.path.exists(old) and not os.path.exists(mid)
    assert os.path.exists(new) and os.path.exists(kept)


def test_jpeg_eviction_never_deletes_through_a_linked_directory(tmp_path):
    root = tmp_path / "cache"
    inside = _write(str(root / "BI" / "inside.jpg"), 100, mtime=2_000_000)
    outside = tmp_path / "photos"
    theirs = _write(str(outside / "holiday.jpg"), 100, mtime=1_000_000)
    _link_directory(root / "BI" / "link", outside)
    manager = DiskBudgetManager(cache_dir=str(root), total_budget_mb=1000)

    assert manager._scan_jpeg_files_size() == 100
    assert manager._evict_oldest_jpeg_files(10_000) == 100

    assert not os.path.exists(inside)
    assert os.path.exists(theirs)


def test_scan_survives_an_entry_that_cannot_be_examined(cache, monkeypatch):
    real_scandir = os.scandir

    class _Broken:
        """A directory entry whose type cannot be read."""

        name = "broken"
        path = os.path.join(str(cache), "broken")

        def is_dir(self):
            raise OSError("unreadable")

    class _Listing:
        def __init__(self, inner, extra):
            self._inner = inner
            self._extra = list(extra)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

        def __next__(self):
            if self._extra:
                return self._extra.pop()
            return next(self._inner)

    def scandir(path):
        extra = [_Broken()] if path == str(cache) else []
        return _Listing(real_scandir(path), extra)

    monkeypatch.setattr(disk_budget_manager.os, "scandir", scandir)

    names = _names(cache)

    # Treated as a file, like the rest; nothing else is lost.
    assert "broken" in names and "top.jpg" in names and "a.jpg" in names

"""Item 2: low-zoom chunks are kept on disk for good."""

import os

from autoortho import low_zoom_base
from autoortho.aopipeline import disk_budget_manager as dbm
from autoortho.aopipeline import dynamic_dds_cache as ddc


def test_chunk_names_parse():
    assert low_zoom_base.chunk_zoom("1100_2200_12_BI.jpg") == 12
    assert low_zoom_base.chunk_zoom("1100_2200_16_GO2.jpeg") == 16
    assert low_zoom_base.chunk_zoom("notes.jpg") is None
    assert low_zoom_base.is_base_chunk("1_2_12_BI.jpg", 12)
    assert not low_zoom_base.is_base_chunk("1_2_13_BI.jpg", 12)


def test_protector_off_returns_none():
    assert low_zoom_base.protector(False, 12) is None


def _write(path, size):
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff" + b"0" * (size - 3))


def test_eviction_never_removes_base_chunks(tmp_path):
    base = tmp_path / "10_20_12_BI.jpg"
    detail = tmp_path / "160_320_16_BI.jpg"
    _write(base, 4000)
    _write(detail, 4000)
    os.utime(base, (1, 1))  # oldest: first in line for LRU eviction
    manager = dbm.DiskBudgetManager(str(tmp_path), total_budget_mb=1)
    manager.protect_jpeg = low_zoom_base.protector(True, 12)

    assert manager._scan_jpegs_size() == 4000
    freed = manager._evict_oldest_jpegs(10_000)

    assert freed == 4000
    assert base.exists()
    assert not detail.exists()


def test_dds_store_cleanup_keeps_base_chunks(tmp_path, monkeypatch):
    base = tmp_path / "0_0_12_BI.jpg"
    detail = tmp_path / "0_0_16_BI.jpg"
    _write(base, 100)
    _write(detail, 100)
    monkeypatch.setattr(ddc, "protect_jpeg", low_zoom_base.protector(True, 12))

    ddc.cleanup_source_jpegs(
        str(tmp_path), 0, 0, tilename_zoom=16, max_zoom=16, min_zoom=12,
        width=1, height=1, maptype="BI",
    )

    assert base.exists()
    assert not detail.exists()

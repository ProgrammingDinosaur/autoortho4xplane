"""Item 5: one number per tile, and only real imagery is persisted."""

import types

import pytest

from autoortho import getortho
from autoortho.aopipeline import dynamic_dds_cache as ddc


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    yield t
    t.close()


def _built(tile, *levels):
    for level in levels:
        tile.dds.mipmap_list[level].retrieved = True


def test_nothing_built_means_no_real_level(tile):
    assert tile.real_from() == len(tile.dds.mipmap_list)


def test_real_from_is_the_finest_contiguous_real_level(tile):
    last = len(tile.dds.mipmap_list) - 1
    _built(tile, *range(2, last + 1))
    assert tile.real_from() == 2
    # A gap: level 3 missing, so only the levels below it count.
    tile.dds.mipmap_list[3].retrieved = False
    assert tile.real_from() == 4


def test_green_level_is_not_real(tile):
    last = len(tile.dds.mipmap_list) - 1
    _built(tile, *range(1, last + 1))
    tile._green_mipmaps.add(2)
    assert tile.real_from() == 3


def test_level_zero_counts_only_when_every_row_is_exact(tile, monkeypatch):
    _built(tile, *range(len(tile.dds.mipmap_list)))
    exact = types.SimpleNamespace(is_exact=True)
    degraded = types.SimpleNamespace(is_exact=False)
    rows = {r: exact for r in range(tile.chunks_per_col)}
    monkeypatch.setattr(tile, "_mm0_manifest_rows", lambda: rows)
    assert tile.real_from() == 0
    rows[3] = degraded
    assert tile.real_from() == 1


def test_coarser_levels_are_derived_from_level_zero(tile, monkeypatch):
    exact = types.SimpleNamespace(is_exact=True)
    monkeypatch.setattr(
        tile, "_mm0_manifest_rows",
        lambda: {r: exact for r in range(tile.chunks_per_col)},
    )
    tile.dds.mipmap_list[0].retrieved = True
    monkeypatch.setattr(
        tile, "_collect_chunks_for_zoom",
        lambda zoom: [b"jpeg"] * (tile.chunks_per_row * tile.chunks_per_col),
    )
    levels = tile.dds.mipmap_list

    def build_mipmap_chain(jpegs, **kwargs):
        assert len(jpegs) == tile.chunks_per_row * tile.chunks_per_col
        datas = [bytes([i + 1]) * levels[i].length for i in range(len(levels))]
        return types.SimpleNamespace(
            success=True,
            mipmap_count=len(datas),
            get_mipmap_data=lambda i: datas[i],
        )

    native = types.SimpleNamespace(build_mipmap_chain=build_mipmap_chain)
    monkeypatch.setattr(tile, "_try_incremental_dds_store", lambda m: None)
    assert tile._derive_levels_from_mm0(1, native)
    assert all(mm.retrieved for mm in levels)
    assert tile.dds.read_at(levels[1].startpos, 4) == b"\x02" * 4
    assert tile.real_from() == 0


def test_no_derivation_without_exact_level_zero(tile):
    native = types.SimpleNamespace(build_mipmap_chain=lambda *a, **k: pytest.fail())
    assert not tile._derive_levels_from_mm0(1, native)


# ---------------------------------------------------------------- the cache


def _cache(tmp_path, real_only):
    cache = ddc.DynamicDDSCache(cache_dir=str(tmp_path), max_size_mb=0, enabled=True)
    cache.real_only = real_only
    return cache


def test_store_rejects_level_zero_that_is_not_all_real(tmp_path, tile, monkeypatch):
    cache = _cache(tmp_path, real_only=True)
    monkeypatch.setattr(cache, "_validate_mm0_manifest_bytes", lambda *a: True)
    kept = []
    monkeypatch.setattr(cache, "store_incremental", lambda *a, **k: kept.append(a) or True)
    manifests = {0: types.SimpleNamespace(is_exact=True), 1: types.SimpleNamespace(is_exact=False)}
    dds_bytes = b"\x00" * tile.dds.total_size
    assert not cache.store(tile.id, 16, dds_bytes, tile, mm0_manifest=manifests)
    # Its coarser levels are real and kept.
    assert kept and 0 not in kept[0][11]


def test_store_unchanged_when_flag_off(tmp_path, tile, monkeypatch):
    cache = _cache(tmp_path, real_only=False)
    monkeypatch.setattr(cache, "_validate_mm0_manifest_bytes", lambda *a: True)
    calls = []
    monkeypatch.setattr(cache, "_store_coarser_levels", lambda *a: calls.append(a))
    manifests = {0: types.SimpleNamespace(is_exact=False)}
    cache.store(tile.id, 16, b"\x00" * tile.dds.total_size, tile, mm0_manifest=manifests)
    assert calls == []

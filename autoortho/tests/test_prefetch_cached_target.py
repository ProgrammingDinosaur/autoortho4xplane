"""The prefetcher asks the tile cache whether a texture is already finished.

A texture that is complete and exact in the DDS cache needs no chunk read and
no build. Asking before opening the tile is what keeps a texture too large
for one prefetch pass (8192 px) from being reread and rebuilt for good.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autoortho import getortho


class FakeDDSCache:
    def __init__(self, cached):
        self.cached = cached
        self.asked = []

    def contains(self, tile_id, max_zoom, tile):
        self.asked.append(
            (
                tile_id,
                max_zoom,
                tile.row,
                tile.col,
                tile.maptype,
                tile.tilename_zoom,
            )
        )
        return self.cached


def _cacher(override=None):
    return SimpleNamespace(
        _resolve_maptype=lambda row, col, maptype, zoom: override or maptype,
        _to_tile_id=lambda row, col, maptype, zoom: (
            f"{row}_{col}_{override or maptype}_{zoom}"
        ),
    )


def test_cached_target_is_asked_at_the_target_zoom(monkeypatch):
    cache = FakeDDSCache(True)
    monkeypatch.setattr(getortho, "dynamic_dds_cache", cache)

    assert getortho.TileCacher.prefetch_target_cached(
        _cacher(), 18240, 31440, "BI", 16, 17
    ) is True

    # The tile is named for its terrain zoom, its texture for the target's.
    assert cache.asked == [("18240_31440_BI_16", 17, 18240, 31440, "BI", 16)]


def test_cached_target_uses_the_resolved_imagery_source(monkeypatch):
    cache = FakeDDSCache(True)
    monkeypatch.setattr(getortho, "dynamic_dds_cache", cache)

    getortho.TileCacher.prefetch_target_cached(
        _cacher(override="EOX"), 37696, 66128, "BI", 17, 17
    )

    assert cache.asked == [("37696_66128_EOX_17", 17, 37696, 66128, "EOX", 17)]


def test_target_not_in_the_cache(monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", FakeDDSCache(False))

    assert getortho.TileCacher.prefetch_target_cached(
        _cacher(), 1, 2, "BI", 16, 17
    ) is False


def test_no_dds_cache_means_not_cached(monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)

    assert getortho.TileCacher.prefetch_target_cached(
        _cacher(), 1, 2, "BI", 16, 17
    ) is False


def test_a_build_the_cache_refused_is_counted(monkeypatch):
    bumps = []
    monkeypatch.setattr(getortho, "bump", lambda name, *a: bumps.append(name))

    getortho.BackgroundDDSBuilder._note_build_not_stored(
        "18240_31440_BI_16", "Streaming"
    )

    assert bumps == ["prebuilt_dds_store_failed"]

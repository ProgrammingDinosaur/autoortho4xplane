"""Item 3: prefetch each tile only to the level X-Plane will want."""

import math
import types

import pytest

from autoortho import getortho, paging_model


# ---------------------------------------------------------------- the model


def test_mip_doubles_with_distance():
    size_m, px, k = 2400.0, 4096, 8000.0
    edge = lambda x: x * size_m / px + size_m / 2  # centre distance for x px
    assert paging_model.mip_for_k(k, edge(k * 0.9), size_m, px) == 0
    assert paging_model.mip_for_k(k, edge(k * 1.5), size_m, px) == 1
    assert paging_model.mip_for_k(k, edge(k * 3), size_m, px) == 2
    assert paging_model.mip_for_k(k, edge(k * 6), size_m, px) == 3


def test_screen_guess_matches_measurement():
    # OrthoStreamXP measured k ~ 7500-8200 with a 3840 px screen at 62 deg.
    assert 7500 <= paging_model.guess_k(3840, 62) <= 8300


def test_fit_recovers_k_from_reads():
    k, size_m, px = 6000.0, 2400.0, 4096
    samples = []
    for i in range(400):
        x = 100 + i * 150.0
        samples.append((x, paging_model.mip_for_k(k, x * size_m / px + size_m / 2, size_m, px)))
    result = paging_model.fit(samples)
    assert result is not None
    assert result.agreement == pytest.approx(1.0)
    assert result.low <= k <= result.high


def test_model_adopts_fitted_k_after_enough_reads():
    model = paging_model.PagingModel()
    true_k, size_m, px = 5000.0, 2400.0, 4096
    for i in range(paging_model.MIN_FIT_SAMPLES):
        d = 1500 + i * 120.0
        model.note_read(paging_model.mip_for_k(true_k, d, size_m, px), d, size_m, px)
    assert model.fitted is not None
    assert abs(model.k - true_k) / true_k < 0.25


def test_user_k_is_never_replaced():
    model = paging_model.PagingModel(12000)
    model.use_screen(1920, 60)
    assert model.k == 12000


def test_closing_shortens_distance():
    lat, lon = 47.0, 8.0
    ahead_lat = lat + 0.2  # north
    d = paging_model.ground_distance_m(lat, lon, ahead_lat, lon)
    soon = paging_model.soon_distance_m(d, lat, lon, ahead_lat, lon, 0.0, 100.0, 60)
    assert soon == pytest.approx(d - 6000, rel=1e-3)
    behind = paging_model.soon_distance_m(d, lat, lon, ahead_lat, lon, 180.0, 100.0, 60)
    assert behind == d


def test_tile_geometry_zl16():
    lat, lon = paging_model.tile_centre(22880, 34320, 16)
    assert -90 < lat < 90 and -180 < lon < 180
    assert 1500 < paging_model.tile_size_m(lat, 16) < 10000


# ---------------------------------------------------------------- the split


class _Cacher:
    def __init__(self):
        self.opened = set()

    def _get_target_zoom_level(self, zoom, row=None, col=None):
        return zoom

    def is_tile_opened_by_xplane(self, row, col, maptype, zoom):
        return (row, col) in self.opened


@pytest.fixture
def prefetcher(monkeypatch):
    p = getortho.SpatialPrefetcher()
    p._tile_cacher = _Cacher()
    calls = []

    def fake_prefetch_tile(row, col, zoom, maptype=None, max_chunks=None, first_mip=0,
                           source="velocity"):
        calls.append((row, col, first_mip))
        p.sources.append(source)
        return 1, True

    p.sources = []

    monkeypatch.setattr(p, "_prefetch_tile", fake_prefetch_tile)
    p.calls = calls
    return p


def _tile_at(lat, lon, zoom=16):
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return (y // 16 * 16, x // 16 * 16, "BI", zoom)


def test_split_keeps_near_and_warms_far(prefetcher, monkeypatch):
    monkeypatch.setattr(getortho, "current_camera", lambda: (47.0, 8.0, 300.0, 0.0, 0.0))
    near = _tile_at(47.0, 8.0)
    far = _tile_at(47.0, 9.5)  # ~110 km east
    kept = prefetcher.paging_split([(near, 0.0), (far, 0.0)])
    assert [t for t, _d in kept] == [near]
    assert len(prefetcher.calls) == 1
    row, col, first_mip = prefetcher.calls[0]
    assert (row, col) == far[:2] and first_mip >= 2
    assert prefetcher.sources == ["paging"]


def test_split_without_position_changes_nothing(prefetcher, monkeypatch):
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    tiles = [(_tile_at(47.0, 9.5), 0.0)]
    assert prefetcher.paging_split(tiles) == tiles
    assert prefetcher.calls == []


def test_split_does_not_warm_same_level_twice(prefetcher, monkeypatch):
    monkeypatch.setattr(getortho, "current_camera", lambda: (47.0, 8.0, 300.0, 0.0, 0.0))
    far = _tile_at(47.0, 9.5)
    prefetcher.paging_split([(far, 0.0)])
    prefetcher.paging_split([(far, 0.0)])
    assert len(prefetcher.calls) == 1

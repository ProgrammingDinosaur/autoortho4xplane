"""Files mode: texture geometry, rings (A3-A6) and X-Plane's window (A8)."""

import math

import pytest

import files_mode_helpers  # noqa: F401  (sets sys.path)
from files_mode import geo, rings, window


def test_texture_centre_matches_load_center():
    # 100000_42224_BI18.ter in z_ao_na: LOAD_CENTER 39.18969 -122.00317 1895 4096
    lat, lon = geo.texture_centre(100000, 42224, 18)
    assert lat == pytest.approx(39.18969, abs=1e-4)
    assert lon == pytest.approx(-122.00317, abs=1e-4)
    assert geo.texture_size_m(100000, 42224, 18) == pytest.approx(1895, abs=2)


def test_parse_names():
    assert geo.parse_ter_name("100000_42240_BI18_water_overlay.ter") == (100000, 42240, "BI", 18)
    assert geo.parse_ter_name("10096_36736_BI16_sea_overlay.ter") == (10096, 36736, "BI", 16)
    assert geo.parse_ter_name("1_2_GO216.ter") == (1, 2, "GO2", 16)
    assert geo.parse_ter_name("1_2_ZL18.ter") == (1, 2, "ZL", 18)
    assert geo.parse_ter_name("1_2_16.ter") == (1, 2, "", 16)
    assert geo.parse_ter_name("readme.txt") is None
    assert geo.texture_name(1, 2, "GO2", 16) == "1_2_GO216.dds"


def test_squares_of_edge_texture():
    # A texture straddling the prime meridian touches two squares.
    row, col = files_mode_helpers.tile_rc(50.5, 0.0, 16)
    sq = geo.squares_of(row, col, 16)
    assert all(s[0] == 50 for s in sq)
    assert {s[1] for s in sq} <= {-1, 0}


def test_ring_levels_zl16():
    model = rings.RingModel(margin=1.0, size_scaling=False)
    assert model.level_needed(10, 16) == 0
    assert model.level_needed(20, 16) == 1
    assert model.level_needed(40, 16) == 2
    assert model.level_needed(80, 16) == 3
    assert model.level_needed(90, 16) == rings.LOAD_LEVEL
    # ZL18: four times closer.
    assert model.level_needed(13.6 / 4 - 0.1, 18) == 0
    assert model.level_needed(13.6 / 4 + 0.1, 18) == 1


def test_ring_finest_and_scale():
    model = rings.RingModel(margin=1.0, size_scaling=False)
    assert model.level_needed(1, 16, finest=1) == 1
    assert rings.finest_for_quality(5) == 0
    assert rings.finest_for_quality(4) == 1
    assert rings.finest_for_quality(None) == 0
    assert rings.screen_scale(2704, 65) == pytest.approx(1.0)
    assert rings.screen_scale(5408, 65) == pytest.approx(2.0)
    assert rings.screen_scale(1000, 90) == 1.0      # never below 1
    assert rings.screen_scale(20000, 20) == 4.0     # capped
    assert model.level_needed(20, 16, scale=2.0) == 0


def test_size_scaling_takes_larger_ring():
    model = rings.RingModel(margin=1.0, size_scaling=True)
    assert model.ring_km(0, 16, size_m=14200) == pytest.approx(27.2)
    assert model.ring_km(0, 16, size_m=3000) == pytest.approx(13.6)


def test_top_px():
    assert rings.top_px_for(16, 16) == 4096
    assert rings.top_px_for(16, 17) == 8192
    assert rings.top_px_for(18, 17) == 2048
    assert rings.top_px_for(16, 20) == 8192  # X-Plane's limit: one zoom above


def test_rings_json_override(tmp_path):
    path = tmp_path / "rings.json"
    path.write_text('{"rings_zl16_km": {"0": 10, "1": 20, "2": 40, "3": 80}, "margin": 1.0}')
    model = rings.RingModel.load(str(path))
    assert model.rings[0] == 10
    assert model.margin == 1.0


def test_window_moves_with_hysteresis():
    w = window.XPlaneWindow()
    assert w.follow(47.5, 8.3)
    tiles = w.tiles()
    assert len(tiles) == 12
    assert (47, 8) in tiles
    # Small moves inside the hysteresis do not move it.
    assert not w.follow(47.5, 8.6)
    assert not w.follow(47.5, 8.4)
    # Past the column's middle + 0.15 degrees: moves east.
    assert w.follow(47.5, 8.66)
    assert w.west == tiles_west(tiles) + 1
    # Back west only past the other threshold.
    assert not w.follow(47.5, 8.4)
    assert w.follow(47.5, 8.3)


def tiles_west(tiles):
    return min(t[1] for t in tiles)


def test_window_rows_and_jump():
    w = window.XPlaneWindow()
    w.follow(47.5, 8.3)
    south = w.south
    assert not w.follow(48.05, 8.3)
    assert w.follow(48.15, 8.3)
    assert w.south == south + 1
    assert w.follow(10.0, 100.0)  # jump: reset
    assert (10, 100) in w.tiles()


def test_window_wraps_dateline():
    w = window.XPlaneWindow()
    w.follow(0.5, 179.7)
    tiles = w.tiles()
    assert all(-180 <= t[1] < 180 for t in tiles)
    assert (0, -180) in tiles or (0, 179) in tiles
    ring = w.with_ring(1)
    assert len(ring) == 30


def test_xplane_tiles_include_lag():
    tiles = window.xplane_tiles(47.5, 8.3)
    assert (47, 8) in tiles
    assert len(tiles) >= 12

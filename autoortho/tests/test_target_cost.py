"""The zoom target cost model."""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import target_cost as tc  # noqa: E402


def test_texture_size_follows_zoom_capped_one_level_above_the_tile():
    assert tc.texture_px(16, 16) == 4096
    assert tc.texture_px(17, 16) == 8192
    assert tc.texture_px(18, 16) == 8192      # X-Plane's limit: tile + 1
    assert tc.texture_px(15, 16) == 2048
    assert tc.texture_px(19, 18) == 8192


def test_texture_bytes_bc1_and_bc3():
    assert tc.texture_bytes(4096) == int(4096 * 4096 * 0.5 * 4 / 3)
    assert tc.texture_bytes(4096, "BC3") == int(4096 * 4096 * 4 / 3)


def _phase(cost, label):
    return next(p for p in cost.phases if p.scenario.label == label)


def test_each_zoom_level_costs_more_low_down():
    costs = [
        _phase(tc.target_cost("fixed", z, 18), "10,000 ft").vram_bytes
        for z in (14, 15, 16, 17)
    ]
    assert costs == sorted(costs)
    assert costs[-1] > 1.5 * costs[-2]


def test_cruise_gains_nothing_from_zl17():
    # From 35,000 ft X-Plane never wants the extra level (paging model).
    zl16 = _phase(tc.target_cost("fixed", 16, 18), "Cruise")
    zl17 = _phase(tc.target_cost("fixed", 17, 18), "Cruise")
    assert zl17.vram_bytes == zl16.vram_bytes


def test_airport_zoom_adds_to_the_departure():
    low = _phase(tc.target_cost("fixed", 16, 17), "Departure and approach")
    high = _phase(tc.target_cost("fixed", 16, 19), "Departure and approach")
    assert high.vram_bytes > low.vram_bytes


def test_standing_still_downloads_only_the_start_area():
    ground = _phase(tc.target_cost("fixed", 16, 18), "At the airport")
    assert ground.rate_bytes_per_s == 0
    assert ground.initial_bytes > 0


def test_plausible_magnitudes_for_zl16():
    cost = tc.target_cost("fixed", 16, 18)
    assert 1.5 * tc.GB < cost.peak_vram.vram_bytes < 4 * tc.GB
    assert 1 < cost.peak_rate.mbit_per_s < 20


def test_dynamic_steps_become_phases():
    steps = [
        {"altitude_ft": 0, "zoom_level": 17, "zoom_level_airports": 19},
        {"altitude_ft": 20000, "zoom_level": 15, "zoom_level_airports": 17},
    ]
    phases = tc.scenarios_for("dynamic", 16, 18, steps)
    assert [p.label for p in phases] == [
        "At the airport",
        "Departure and approach",
        "From 20,000 ft",
    ]
    assert phases[0].zoom == 17 and phases[0].airport_zoom == 19
    assert phases[1].airport_zoom == 19
    assert phases[2].zoom == 15 and phases[2].airport_zoom is None


def test_dynamic_without_steps_falls_back_to_fixed():
    assert [p.label for p in tc.scenarios_for("dynamic", 16, 18, [])] == [
        p.label for p in tc.scenarios_for("fixed", 16, 18)
    ]


def test_cost_from_config_reads_steps_text_and_format():
    cfg = SimpleNamespace(
        autoortho=SimpleNamespace(
            max_zoom_mode="dynamic",
            max_zoom=16,
            max_zoom_near_airports=18,
            dynamic_zoom_steps=(
                '[{"altitude_ft": 0, "zoom_level": 16, '
                '"zoom_level_airports": 18}]'
            ),
        ),
        streaming=SimpleNamespace(paging_k=0),
        pydds=SimpleNamespace(format="BC3"),
    )
    cost = tc.target_cost_from_config(cfg)
    expected = tc.target_cost(
        "dynamic", 16, 18,
        [{"altitude_ft": 0, "zoom_level": 16, "zoom_level_airports": 18}],
        fmt="BC3",
    )
    assert cost == expected


@pytest.mark.parametrize("k", [4000, 16000])
def test_larger_k_holds_more(k):
    base = tc.target_cost("fixed", 16, 18).peak_vram.vram_bytes
    other = tc.target_cost("fixed", 16, 18, k=k).peak_vram.vram_bytes
    assert (other > base) == (k > tc.DEFAULT_K)

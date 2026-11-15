"""Guards: graphics memory, RAM and disk checks with suggested fixes."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guards  # noqa: E402
import target_cost as tc  # noqa: E402
from guards import GB, Level  # noqa: E402
from hardware import HardwareInfo  # noqa: E402


def _pc(vram_gb=None, ram_gb=32):
    return HardwareInfo(16, 8, int(ram_gb * GB),
                        int(vram_gb * GB) if vram_gb else None)


def test_vram_levels():
    cost = tc.target_cost("fixed", 17, 18)
    assert guards.check_vram(cost, _pc(24)).level is Level.OK
    assert guards.check_vram(cost, _pc(8)).level is Level.BAD
    textures = cost.peak_vram.vram_bytes
    # Just above 90 % of the card.
    card = (textures + 5 * GB) / 0.95
    assert guards.check_vram(
        cost, HardwareInfo(16, 8, 32 * GB, int(card))
    ).level is Level.WARN


def test_unknown_vram_is_information_only():
    result = guards.check_vram(tc.target_cost("fixed", 18, 19), _pc(None))
    assert result.level is Level.INFO
    assert "unknown" in result.message


def test_reserve_counts():
    cost = tc.target_cost("fixed", 16, 18)
    assert guards.check_vram(cost, _pc(12), reserve_gb=5).level is Level.OK
    assert guards.check_vram(cost, _pc(12), reserve_gb=10).level is Level.BAD


def test_fixed_fix_fits_the_card():
    fix = guards.suggest_target("fixed", 17, 19, [], _pc(8))
    assert fix is not None
    zoom = int(fix.changes["autoortho.max_zoom"])
    airport = int(fix.changes["autoortho.max_zoom_near_airports"])
    assert zoom < 17
    cost = tc.target_cost("fixed", zoom, airport)
    assert guards.check_vram(cost, _pc(8)).level is Level.OK


def test_no_fix_when_it_fits_or_card_unknown():
    assert guards.suggest_target("fixed", 16, 18, [], _pc(24)) is None
    assert guards.suggest_target("fixed", 17, 19, [], _pc(None)) is None


def test_dynamic_fix_lowers_steps_only():
    steps = [
        {"altitude_ft": 0, "zoom_level": 17, "zoom_level_airports": 19},
        {"altitude_ft": 10000, "zoom_level": 16, "zoom_level_airports": 18},
    ]
    fix = guards.suggest_target("dynamic", 16, 18, steps, _pc(8))
    assert fix is not None
    lowered = fix.changes["autoortho.dynamic_zoom_steps"]
    assert [s["altitude_ft"] for s in lowered] == [0, 10000]
    for old, new in zip(steps, lowered):
        assert new["zoom_level"] <= old["zoom_level"]
        assert new["zoom_level_airports"] <= old["zoom_level_airports"]
    cost = tc.target_cost("dynamic", 16, 18, lowered)
    assert guards.check_vram(cost, _pc(8)).level is Level.OK


def test_ram_levels_and_fix():
    assert guards.check_ram(4, _pc(ram_gb=16)).level is Level.OK
    warn = guards.check_ram(8, _pc(ram_gb=16))
    assert warn.level is Level.WARN
    assert warn.fix.changes == {"cache.cache_mem_limit": "4"}
    assert guards.check_ram(12, _pc(ram_gb=16)).level is Level.BAD
    assert guards.check_ram(4, HardwareInfo(1, 1, 0)).level is Level.INFO


def test_disk_levels(monkeypatch):
    cost = tc.target_cost("fixed", 16, 18)
    for free, level in (
        (500 * GB, Level.OK),
        (8 * GB, Level.WARN),
        (2 * GB, Level.BAD),
        (None, Level.INFO),
    ):
        monkeypatch.setattr(guards, "free_bytes", lambda _path, f=free: f)
        assert guards.check_disk("C:/cache", cost).level is level


def test_free_bytes_walks_up_to_an_existing_folder(tmp_path):
    assert guards.free_bytes(tmp_path / "not" / "there") > 0
    assert guards.free_bytes("") is None


def _cfg(**autoortho):
    values = dict(max_zoom_mode="fixed", max_zoom=17,
                  max_zoom_near_airports=18, dynamic_zoom_steps=[])
    values.update(autoortho)
    return SimpleNamespace(
        autoortho=SimpleNamespace(**values),
        cache=SimpleNamespace(cache_mem_limit=4),
        paths=SimpleNamespace(cache_dir=""),
        streaming=SimpleNamespace(paging_k=0),
        pydds=SimpleNamespace(format="BC1"),
        guards=SimpleNamespace(warnings=True, xplane_vram_reserve_gb=5),
    )


def test_check_config_worst_first_with_fix():
    results = guards.check_config(_cfg(), _pc(8))
    assert results[0].resource == "vram"
    assert results[0].level is Level.BAD
    assert results[0].fix is not None
    assert {r.resource for r in results} == {"vram", "ram", "disk", "download"}


def test_check_config_without_disk():
    results = guards.check_config(_cfg(), _pc(24), include_disk=False)
    assert "disk" not in {r.resource for r in results}


def test_warnings_switch_reads_text():
    cfg = _cfg()
    cfg.guards.warnings = "False"
    assert guards.warnings_enabled(cfg) is False
    assert guards.warnings_enabled(SimpleNamespace()) is True

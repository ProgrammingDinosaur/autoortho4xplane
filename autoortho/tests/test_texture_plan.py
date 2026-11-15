"""Phase 8: the texture size cap and the graphics memory estimate."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import governor as gv  # noqa: E402
import texture_plan as tp  # noqa: E402
from governor import Governor, Sample  # noqa: E402

GB = 1024 ** 3


def test_texture_sizes_follow_the_build_zoom():
    assert tp.texture_px(16, 16) == 4096
    assert tp.texture_px(16, 17) == 8192
    assert tp.texture_px(18, 19) == 8192
    assert tp.texture_px(16, 15) == 2048


def test_cap_lowers_only_oversized_textures():
    assert tp.capped_zoom(16, 17, 4096) == 16
    assert tp.capped_zoom(18, 19, 4096) == 18
    assert tp.capped_zoom(16, 16, 4096) == 16      # already fits
    assert tp.capped_zoom(16, 15, 4096) == 15      # never raised
    assert tp.capped_zoom(16, 17, 0) == 17         # no cap
    assert tp.capped_zoom(16, 17, 2048) == 15


def test_texture_bytes_bc1_and_bc3():
    # 4096 px BC1: 8 MiB, a third more with the smaller levels.
    assert tp.texture_bytes(4096) == int(4096 * 4096 * 0.5 * 4 / 3)
    assert tp.texture_bytes(4096, "BC3") == pytest.approx(2 * tp.texture_bytes(4096), abs=2)
    assert tp.texture_bytes(8192) == pytest.approx(4 * tp.texture_bytes(4096), abs=4)


def test_held_textures_keep_the_finest_level_for_twenty_minutes():
    now = [0.0]
    held = tp.HeldTextures(clock=lambda: now[0])
    held.note("a", 1024)
    held.note("a", 4096)
    held.note("a", 512)          # a coarser read later keeps the finest
    held.note("b", 2048)
    assert held.count() == 2
    assert held.bytes() == tp.texture_bytes(4096) + tp.texture_bytes(2048)
    now[0] = tp.HELD_FOR_S + 1
    held.note("b", 2048)
    assert held.count() == 1     # "a" expired
    assert held.bytes() == tp.texture_bytes(2048)


def test_cap_from_config():
    cfg = SimpleNamespace(governor=SimpleNamespace(texture_cap_px="4096"))
    assert tp.cap_from_config(cfg) == 4096
    assert tp.cap_from_config(SimpleNamespace()) == 0
    assert tp.cap_from_config(SimpleNamespace(governor=SimpleNamespace(texture_cap_px="x"))) == 0


# -- governor --------------------------------------------------------------


def _cfg(allow):
    return SimpleNamespace(governor=SimpleNamespace(allow_texture_cap=allow))


def test_cap_lever_only_when_allowed():
    assert gv.TEXTURE_CAP_LEVER in gv.levers_for(_cfg("True"))
    assert gv.TEXTURE_CAP_LEVER not in gv.levers_for(_cfg("False"))


def _flight(gov, n, vram, t0=0.0):
    decisions = []
    c = dict(near_real=0, near_standin=0, req_ok=0)
    for i in range(n):
        c = {k: v + (50 if k == "near_real" else 100 if k == "req_ok" else 0)
             for k, v in c.items()}
        sample = Sample(t=t0 + 2.0 * (i + 1), cpu_percent=60, free_ram_bytes=16 * GB,
                        total_ram_bytes=32 * GB, connected=True, speed_kt=250,
                        altitude_ft=10000, graphics_memory_bytes=vram, **c)
        decision = gov.observe(sample, starting=False)
        if decision is not None:
            decisions.append(decision)
    return decisions


def test_governor_caps_textures_when_graphics_memory_is_full():
    levers = gv.levers_for(_cfg(True))
    values = {name: spec.minimum for name, spec in levers.items()}
    gov = Governor(levers, values, mode="auto", vram_budget_bytes=7 * GB)
    decisions = _flight(gov, 10, int(6.9 * GB))
    cap = [d for d in decisions if d.lever == gv.TEXTURE_CAP_LEVER]
    assert cap and cap[0].new == 4096 and cap[0].state == "vram"
    assert cap[0].text().startswith("cap textures at 4096 px")
    assert gov.values[gv.TEXTURE_CAP_LEVER] == 4096
    # Kept for the session: no revert, not eased off in headroom.
    later = _flight(gov, 60, int(2 * GB), t0=100.0)
    assert not [d for d in later if d.lever == gv.TEXTURE_CAP_LEVER]
    assert gov.values[gv.TEXTURE_CAP_LEVER] == 4096
    assert gov.summary()["texture_cap_px"] == 4096


def test_without_permission_it_only_tells_the_user():
    levers = gv.levers_for(_cfg(False))
    values = {name: spec.minimum for name, spec in levers.items()}
    gov = Governor(levers, values, mode="auto", vram_budget_bytes=7 * GB)
    decisions = _flight(gov, 10, int(6.9 * GB))
    assert not [d for d in decisions if d.lever == gv.TEXTURE_CAP_LEVER]
    assert "graphics memory limit" in (gov.notice or "")
    assert "allow auto-tuning to cap texture size" in gov.notice


def test_pinned_cap_is_left_alone():
    levers = gv.levers_for(_cfg(True))
    values = {name: spec.minimum for name, spec in levers.items()}
    gov = Governor(levers, values, mode="auto", vram_budget_bytes=7 * GB,
                   pinned={gv.TEXTURE_CAP_LEVER})
    assert not [d for d in _flight(gov, 10, int(6.9 * GB))
                if d.lever == gv.TEXTURE_CAP_LEVER]


def test_service_sums_worker_estimates_and_toggles_permission():
    levers = gv.levers_for(_cfg(False))
    values = {name: spec.minimum for name, spec in levers.items()}
    published = []
    service = gv.GovernorService(
        Governor(levers, values, mode="auto"), read_stat=lambda k: 0,
        publish=published.append, sum_stat=lambda prefix: 3 * GB,
    )
    assert service.sample().graphics_memory_bytes == 3 * GB
    service.set_texture_cap_allowed(True)
    assert gv.TEXTURE_CAP_LEVER in service.governor.levers
    service.governor.values[gv.TEXTURE_CAP_LEVER] = 4096
    service.set_texture_cap_allowed(False)
    assert gv.TEXTURE_CAP_LEVER not in service.governor.levers
    assert published == [{gv.TEXTURE_CAP_LEVER: 0}]


# -- getortho: the tile zoom choice ------------------------------------------


def test_tile_cacher_applies_the_cap(monkeypatch):
    import getortho

    cacher = getortho.TileCacher.__new__(getortho.TileCacher)
    cacher.max_zoom_mode = "fixed"
    cacher.target_zoom_level = 17
    cacher.target_zoom_level_near_airports = 19
    monkeypatch.setattr(getortho.CFG.autoortho, "using_custom_tiles", False, raising=False)
    monkeypatch.setattr(getortho.CFG.governor, "texture_cap_px", 0, raising=False)
    assert cacher._get_target_zoom_level(16) == 17
    assert cacher._get_target_zoom_level(18) == 19
    monkeypatch.setattr(getortho.CFG.governor, "texture_cap_px", 4096, raising=False)
    counted = []
    monkeypatch.setattr(getortho, "bump", lambda name, *a: counted.append(name))
    assert cacher._get_target_zoom_level(16, count=True) == 16
    assert cacher._get_target_zoom_level(18) == 18
    assert counted == ["texture_capped_tiles"]


def test_reads_feed_the_graphics_memory_estimate(monkeypatch):
    import getortho

    held = tp.HeldTextures()
    monkeypatch.setattr(getortho, "held_textures", held)
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    tile = getortho.Tile.__new__(getortho.Tile)
    tile.id = "1_2_BI_16"
    tile.dds = SimpleNamespace(width=8192)
    tile._note_wanted_level(1)
    assert held.bytes() == tp.texture_bytes(4096)


# -- phase 9 worker gauges ---------------------------------------------------


def test_radar_publishes_nearby_tiles_by_state(monkeypatch):
    import threading

    import getortho

    published = {}
    monkeypatch.setattr(getortho, "set_stat", lambda k, v: published.__setitem__(k, v))
    monkeypatch.setattr(getortho, "datareftracker", SimpleNamespace(
        data_valid=True, connected=True, lat=47.5, lon=11.0))

    def tile(row, col, real_from, refs):
        dds = SimpleNamespace(mipmap_list=[0] * 5)
        return SimpleNamespace(row=row, col=col, tilename_zoom=16, chunks_per_row=16,
                               dds=dds, refs=refs, real_from=lambda: real_from)

    lat_row = int((1 - __import__("math").asinh(__import__("math").tan(
        __import__("math").radians(47.5))) / __import__("math").pi) / 2 * 2 ** 16)
    col = int((11.0 + 180) / 360 * 2 ** 16)
    cacher = getortho.TileCacher.__new__(getortho.TileCacher)
    cacher.tc_lock = threading.RLock()
    cacher.tiles = {
        "a": tile(lat_row - 8, col - 8, 0, 0),     # sharp
        "b": tile(lat_row + 8, col + 8, 2, 1),     # being read
        "c": tile(lat_row, col + 40, 2, 0),        # below target
        "far": tile(lat_row, col + 5000, 0, 0),    # beyond 60 nm
    }
    cacher._publish_radar()
    rows = next(v for k, v in published.items() if k.startswith("tile_radar:"))
    assert sorted(code for _dx, _dy, code in rows) == [0, 1, 2]
    assert all(abs(dx) <= 60 and abs(dy) <= 60 for dx, dy, _code in rows)


def test_horizon_is_the_last_ready_tile_before_a_gap(monkeypatch):
    import getortho

    published = {}
    monkeypatch.setattr(getortho, "set_stat", lambda k, v: published.__setitem__(k, v))
    prefetcher = getortho.SpatialPrefetcher.__new__(getortho.SpatialPrefetcher)
    ready = {(1, 1), (2, 2)}
    prefetcher._tile_ready = lambda row, col, maptype, zoom: (row, col) in ready
    prefetcher._note_horizon([
        ((1, 1, "BI", 16), 1000.0), ((2, 2, "BI", 16), 9000.0),
        ((3, 3, "BI", 16), 15000.0), ((4, 4, "BI", 16), 20000.0),
    ])
    value = next(v for k, v in published.items() if k.startswith("prefetch_horizon_m:"))
    assert value == 9000

"""Files mode: route feed, time exclusion swaps, season rebuilds, relocation
pause, live levers, the files-mode governor and Home's wording."""

import os
import time

import pytest

import files_mode_helpers as h
from files_mode import dsf_swap, fsops, geo, pack, planner as planner_mod, rings
from files_mode import governor as fg
from files_mode.route import RouteFeed
from files_mode.status import files_mode_status

# --------------------------------------------------------------- route


class _Fix:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon


class _Manager:
    def __init__(self, fixes):
        self.fixes = fixes
        self.is_loaded = False
        self.loaded = []

    def load_flight_data(self, snap):
        self.loaded.append(snap)
        self.is_loaded = True
        return True

    def clear(self):
        self.is_loaded = False

    def is_on_route(self, lat, lon, threshold):
        return True

    def get_upcoming_fixes(self, lat, lon, count):
        return self.fixes[:count]


class _Cfg:
    class simbrief:
        use_flight_data = False
        route_deviation_threshold_nm = 40


def test_route_feed_follows_shared_snapshot():
    stats = {"simbrief_route_generation": 1, "simbrief_route_snapshot": {"x": 1},
             "simbrief_route_enabled": True}
    mgr = _Manager([_Fix(47.0, 8.0), _Fix(47.5, 8.5)])
    feed = RouteFeed(stats.get, manager=mgr, cfg=_Cfg, clock=lambda: 1000.0)
    assert feed.route(47.0, 8.0) == [(47.0, 8.0), (47.5, 8.5)]
    assert mgr.loaded == [{"x": 1}]
    stats["simbrief_route_enabled"] = False
    feed2 = RouteFeed(stats.get, manager=mgr, cfg=_Cfg, clock=lambda: 1000.0)
    assert feed2.route(47.0, 8.0) is None


def test_route_points_cut_at_lookahead():
    route = [(47.0, 8.0), (47.0, 9.0), (47.0, 10.0)]
    pts = planner_mod.route_points(route, 47.0, 8.0, 90.0, 100.0, lookahead_s=90.0)
    assert pts and pts[-1][2] == 90.0
    assert geo.haversine_km(47.0, 8.0, pts[-1][0], pts[-1][1]) == pytest.approx(9.0, abs=0.2)


def test_route_points_ignored_when_flying_away():
    route = [(47.0, 8.5), (47.0, 9.0)]
    assert planner_mod.route_points(route, 47.0, 8.0, 270.0, 100.0) == []


# -------------------------------------------------------- time exclusion

def _pack_with_dsfs(tmp_path):
    textures = h.textures_around(47.5, 8.5, radius=1)
    root = h.make_region(str(tmp_path / "scenery"), "z_ao_eur", textures)
    custom = tmp_path / "Custom Scenery"
    custom.mkdir()
    p = pack.Pack(str(tmp_path / "files"), str(custom), {"z_ao_eur": root})
    p.setup()
    xp = tmp_path / "X-Plane 12"
    for lat, lon in {s for t in textures for s in geo.squares_of(t[0], t[1], t[3])}:
        folder = xp / "Global Scenery" / "X-Plane 12 Global Scenery" / "Earth nav data" / \
            geo.square_folder(lat, lon)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (geo.square_name(lat, lon) + ".dsf")).write_bytes(b"GLOBAL")
    return p, str(xp)


def test_dsf_swap_day_night_and_skips_loaded(tmp_path):
    p, xp = _pack_with_dsfs(tmp_path)
    night = {"on": True}
    swapper = dsf_swap.DsfSwapper(p, xp, lambda: night["on"])
    squares = {(47, 8)}
    dst = os.path.join(p.region_dir("z_ao_eur"), "Earth nav data", "+40+000", "+47+008.dsf")
    # Held by X-Plane: never swapped under it.
    assert swapper.sync(squares, xp_tiles={(47, 8)}) == 0
    assert open(dst, "rb").read() == b"XPLNEDSF"
    assert swapper.sync(squares, xp_tiles=set()) == 1
    assert open(dst, "rb").read() == b"GLOBAL"
    night["on"] = False
    assert swapper.sync(squares, xp_tiles=set()) == 1
    assert open(dst, "rb").read() == b"XPLNEDSF"
    # The region's own DSF is untouched throughout.
    src = os.path.join(p.regions["z_ao_eur"], "Earth nav data", "+40+000", "+47+008.dsf")
    assert open(src, "rb").read() == b"XPLNEDSF"


def test_dsf_swap_off_when_disabled(tmp_path):
    p, xp = _pack_with_dsfs(tmp_path)
    swapper = dsf_swap.DsfSwapper(p, xp, lambda: True, enabled=lambda: False)
    assert swapper.sync({(47, 8)}, set()) == 0


# ------------------------------------------------------- session features

from files_mode.session import FilesConfig, FilesSession  # noqa: E402

LAT, LON = 47.45, 8.55


def _wait_idle(s, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not s.scheduler.idle():
        time.sleep(0.02)


@pytest.fixture
def session(tmp_path):
    root = h.make_region(str(tmp_path / "scenery"), "z_ao_eur",
                         h.textures_around(LAT, LON, radius=4))
    custom = tmp_path / "Custom Scenery"
    custom.mkdir()
    flight = h.FakeFlight(LAT, LON)
    season = {"key": None}
    cfg = FilesConfig(files_root=str(tmp_path / "files"), custom_scenery=str(custom),
                      regions={"z_ao_eur": root}, disk_gb=10, gate_port=0,
                      install_plugin=False)
    season_fn = lambda r, c, z: season["key"]  # noqa: E731
    s = FilesSession(cfg, flight, service=h.FakeService(season_fn=season_fn),
                     size_of=lambda cat, i: (1024, "BC1"), xplane_running=lambda: False,
                     season_key_fn=season_fn)
    s.flight = flight
    s.season = season
    s.start()
    s._stop.set()          # drive step() by hand
    s._thread.join(5)
    yield s
    s.stop()


def test_season_change_requeues_built_textures(session):
    session.step()
    _wait_idle(session)
    built = [st for st in session.index.all() if st.built_level != 99]
    assert built and all(st.season_key is None for st in built)
    session.season["key"] = "0.500"
    queued = session._season_sweep(session.feed.current, session.window.with_ring(2))
    assert queued > 0
    _wait_idle(session)
    assert any(st.season_key == "0.500" for st in session.index.all())


def test_relocation_pauses_until_built(session):
    session.step()
    _wait_idle(session)
    session.gate.last_hello = time.time()   # the plugin is there
    assert session.gate.handle(b"AO1 POLL 47.45 8.55") == "AO1 RESUME"
    from files_mode import geo as g
    lat, lon = g.destination(LAT, LON, 90.0, 40.0)
    session.flight.move(lat, lon)
    session.step()
    assert session.relocating
    assert session.gate.handle(f"AO1 POLL {lat} {lon}".encode()) == "AO1 PAUSE"
    for _ in range(20):
        _wait_idle(session)
        session.step()
        if not session.relocating:
            break
    assert not session.relocating
    assert session.gate.handle(f"AO1 POLL {lat} {lon}".encode()) == "AO1 RESUME"
    assert session.counters["files_relocations"] == 1


def test_relocation_gives_up_after_cap(session):
    session.begin_relocation(LAT, LON)
    session._relocate_started -= session.RELOCATE_MAX_S + 1
    assert not session.pause_wanted()
    assert not session.starting


def test_apply_levers(session):
    session.apply_levers({"files.builders": 3, "files.lookahead_s": 150,
                          "files.margin": 1.2, "other.x": 1})
    assert session.scheduler.workers == 3
    assert session.planner.lookahead_s == 150
    assert session.rings.margin == 1.2
    assert session.planner.rings is session.rings


# --------------------------------------------------------------- governor

def _governor(mode="auto", **kw):
    levers = fg.levers_for(limits=None, network_spec=fg.LeverSpec(
        fg.CONCURRENCY, "Parallel downloads", 16, 64, 320, group="network"))
    values = {fg.BUILDERS: 2, fg.LOOKAHEAD: 90, fg.MARGIN: 1.11, fg.CONCURRENCY: 128}
    return fg.FilesGovernor(levers, values, mode=mode, **kw)


def _feed(gov, seconds, step=2.0, start=0.0, **per_step):
    """Feed samples; per_step maps counter -> increment per sample."""
    t = start
    totals = dict(late_never=0, late_queued=0, late_building=0, builds=0)
    totals.update(getattr(gov, "_test_totals", {}))
    out = []
    gauges = {k: v for k, v in per_step.items() if k not in totals}
    while t < start + seconds:
        for k in ("late_never", "late_queued", "late_building", "builds"):
            totals[k] += per_step.get(k, 0)
        d = gov.observe(fg.FilesSample(t=t, **totals, **gauges))
        if d is not None:
            out.append(d)
        t += step
    gov._test_totals = totals
    return out, t


def test_governor_plans_further_ahead_when_late_textures_were_never_queued():
    gov = _governor()
    decisions, _ = _feed(gov, 40, late_never=2, builds=5, builders=2, in_flight=2,
                         cpu_percent=30)
    assert decisions and decisions[0].lever == fg.LOOKAHEAD and decisions[0].new > 90
    assert gov.values[fg.LOOKAHEAD] > 90


def test_governor_adds_downloads_when_builders_idle_and_textures_wait():
    gov = _governor()
    decisions, _ = _feed(gov, 40, late_queued=2, builds=5, builders=4, in_flight=1,
                         cpu_percent=30)
    assert decisions[0].lever == fg.CONCURRENCY


def test_governor_adds_builders_when_they_are_busy():
    gov = _governor()
    decisions, _ = _feed(gov, 40, late_queued=2, builds=5, builders=2, in_flight=2,
                         cpu_percent=30)
    assert decisions[0].lever == fg.BUILDERS and decisions[0].new == 3


def test_governor_protects_frame_rate():
    gov = _governor()
    _, t = _feed(gov, 40, builds=5, frame_ms=16.0, cpu_percent=30)
    decisions, _ = _feed(gov, 40, start=t, builds=5, frame_ms=40.0, cpu_percent=30)
    assert decisions[0].lever == fg.BUILDERS and decisions[0].new == 1
    assert decisions[0].state == "sim_fps"


def test_governor_reverts_a_move_that_made_things_worse():
    gov = _governor()
    decisions, t = _feed(gov, 32, late_queued=1, builds=5, builders=2, in_flight=2,
                         cpu_percent=30)
    assert decisions and decisions[0].lever == fg.BUILDERS
    decisions, _ = _feed(gov, 40, start=t, late_queued=6, builds=1, builders=3,
                         in_flight=3, cpu_percent=30)
    reverts = [d for d in decisions if d.outcome == "revert"]
    assert reverts and gov.values[fg.BUILDERS] == 2


def test_governor_advise_and_starting():
    gov = _governor(mode="advise")
    decisions, _ = _feed(gov, 40, late_never=2, builds=5, cpu_percent=30)
    assert decisions and not decisions[0].applied
    assert gov.values[fg.LOOKAHEAD] == 90
    gov2 = _governor()
    decisions, _ = _feed(gov2, 40, late_never=2, builds=5, starting=True, cpu_percent=30)
    assert decisions == [] and gov2.state == "starting"


def test_governor_eases_off_with_headroom():
    gov = _governor()
    gov.values[fg.LOOKAHEAD] = 150
    decisions, _ = _feed(gov, 300, builds=5, cpu_percent=20)
    assert any(d.state == "headroom" and d.lever == fg.LOOKAHEAD for d in decisions)


def test_service_snapshot_shape():
    gov = _governor()
    stats = {"files_stats": {"files_builds": 3, "files_builders": 2}}
    svc = fg.FilesGovernorService(gov, read_stat=stats.get, clock=lambda: 1.0)
    svc.tick()
    snap = svc.snapshot()
    for key in ("mode", "state", "state_text", "fidelity", "values", "levers", "decisions",
                "timeline", "home"):
        assert key in snap
    assert svc.flight_summary()["backend"] == "files"


# ------------------------------------------------------------------ status

def test_status_words():
    assert files_mode_status(error="no disk")["state"] == "error"
    assert "Ready" in files_mode_status()["headline"]
    s = files_mode_status({"files_starting": 1, "files_start_built": 3,
                           "files_start_total": 9, "files_plugin_seen": 1}, running=True)
    assert "3 of 9" in s["headline"]
    s = files_mode_status({"files_late": 4, "files_disk_used_bytes": 2 << 30,
                           "files_disk_budget_bytes": 20 << 30, "files_plugin_seen": 1},
                          running=True)
    assert s["state"] == "warning" and "2.0 of 20 GB" in s["detail"]
    assert files_mode_status({"files_plugin_seen": 1}, running=True)["state"] == "ok"


# --------------------------------------------------------------------- UI

def test_essentials_backend_selector(qt_app):
    from ui.pages.essentials_page import EssentialsPage
    page = EssentialsPage()
    seen = []
    page.backend_changed.connect(seen.append)
    page.set_backend("files")
    assert page.backend() == "files" and seen == []      # programmatic: no signal
    assert "plugin" in page.backend_label.text()
    page.backend_combo.setCurrentIndex(page.backend_combo.findData("fuse"))
    assert seen == ["fuse"] and page.backend() == "fuse"
    page.set_backend("nonsense")
    assert page.backend() == "fuse"


def test_home_files_card(qt_app):
    from ui.pages.home_page import HomePage
    home = HomePage()
    home.set_files_mode(None)
    assert home.files_card.isHidden()
    home.set_files_mode(files_mode_status({"files_late": 2, "files_plugin_seen": 1},
                                          running=True))
    assert not home.files_card.isHidden()
    assert "late" in home.files_label.text()

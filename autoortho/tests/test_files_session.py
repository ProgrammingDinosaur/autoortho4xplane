"""Files mode end to end: a synthetic region, the fake texture service and
the stand-in X-Plane (tools/files_mode/fake_xplane.py)."""

import importlib.util
import os
import time

import pytest

import files_mode_helpers as h
from files_mode import fsops, planner as planner_mod, scheduler as scheduler_mod
from files_mode.flightfeed import from_snapshot
from files_mode.session import FilesConfig, FilesSession, builder_counts

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "tools", "files_mode", "fake_xplane.py")
spec = importlib.util.spec_from_file_location("fake_xplane", TOOLS)
fake_xplane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_xplane)

LAT, LON = 47.45, 8.55
TOP = 1024


def _wait_idle(session, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.scheduler.idle():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def session(tmp_path):
    scenery = tmp_path / "scenery"
    root = h.make_region(str(scenery), "z_ao_eur", h.textures_around(LAT, LON, radius=8))
    custom = tmp_path / "Custom Scenery"
    custom.mkdir()
    flight = h.FakeFlight(LAT, LON)
    cfg = FilesConfig(files_root=str(tmp_path / "files"), custom_scenery=str(custom),
                      regions={"z_ao_eur": root}, disk_gb=10, gate_port=0,
                      install_plugin=False)
    s = FilesSession(cfg, flight, service=h.FakeService(),
                     size_of=lambda cat, i: (TOP, "BC1"), xplane_running=lambda: False)
    s.flight = flight
    s.start()
    yield s
    s.stop()


def test_start_hold_builds_the_area_then_flight_stays_ahead(session):
    custom = session.cfg.custom_scenery
    assert fsops.is_link_or_junction(os.path.join(custom, "z_ao_eur"))
    session.on_flight_load(LAT, LON)
    assert not session.start_status().ready
    for _ in range(20):
        session.step()
        _wait_idle(session)
        if session.start_status().ready:
            break
    status = session.start_status()
    assert status.ready, status
    assert status.total > 0
    xp = fake_xplane.FakeXPlane(session)
    xp.start(LAT, LON)
    report = xp.report()
    assert report["reads"].get("real", 0) > 0
    assert not [b for b in report["bad"] if b[0] in ("hole", "missing")], report["bad"][:5]
    assert not report["bad"], report["bad"][:5]
    # Fly east at 150 m/s, 2 s per pass.
    lat, lon = LAT, LON
    session.flight.state["spd"] = 150.0
    session.flight.state["hdg"] = session.flight.state["hpath"] = 90.0
    from files_mode import geo
    for _ in range(15):
        lat, lon = geo.destination(lat, lon, 90.0, 0.3)
        session.flight.move(lat, lon)
        session.step()
        _wait_idle(session)
        xp.move(lat, lon)
    report = xp.report()
    assert not [b for b in report["bad"] if b[0] in ("hole", "missing")], report["bad"][:5]
    stats = session.stats()
    assert stats["files_builds"] > 0 and stats["files_writes"] > 0


def test_stop_removes_links_when_xplane_is_not_running(session):
    link = os.path.join(session.cfg.custom_scenery, "z_ao_eur")
    assert fsops.is_link_or_junction(link)
    session.stop()
    assert not os.path.lexists(link)
    assert os.path.isdir(session.pack.region_dir("z_ao_eur"))


def test_flightfeed_snapshot_and_jump():
    now = time.time()
    snap = {"connected": True, "data_valid": True, "lat": 47.0, "lon": 8.0, "alt": 100,
            "hdg": 10, "hpath": 20, "spd": 50, "timestamp": now, "async_scenery_load": 1.0,
            "view_x": 1000.0, "view_y": 0.0, "view_z": 0.0, "local_x": 0.0, "local_y": 0.0,
            "local_z": 0.0}
    fs = from_snapshot(snap, now=now)
    assert fs.valid and fs.track_deg == 20 and fs.scenery_loading
    assert fs.camera_lon > 8.0
    stale = dict(snap, timestamp=now - 10)
    assert not from_snapshot(stale, now=now).valid
    far = dict(snap, lat=48.0)
    assert from_snapshot(far, fs, now=now).jumped


def test_builder_counts():
    flight, start = builder_counts(cpu_count=16, ram_gb=32)
    assert flight == 4 and start == 10
    flight, start = builder_counts(cpu_count=4, ram_gb=8)
    assert flight == 1 and start >= 1


class _D:
    def __init__(self, name, level, urgency, dist=1.0):
        self.name, self.level, self.urgency_s, self.distance_km = name, level, urgency, dist


def test_scheduler_orders_by_urgency_and_replaces_plan():
    done = []
    sched = scheduler_mod.Scheduler(lambda d: done.append(d.name) or True, workers=1,
                                    paused=lambda: True)
    sched.start()
    sched.replace_plan([_D("late", 2, 30), _D("now", 1, 0), _D("soon", 0, 5)])
    assert sched.urgent_depth() == 1
    sched.replace_plan([_D("soon", 0, 5), _D("now", 1, 0)])   # "late" dropped
    assert sched.depth() == 2
    sched.paused = lambda: False
    deadline = time.monotonic() + 5
    while not sched.idle() and time.monotonic() < deadline:
        time.sleep(0.01)
    sched.stop()
    assert done == ["now", "soon"]


def test_scheduler_backs_off_failures():
    class R:
        ok = False
        refused = True
    sched = scheduler_mod.Scheduler(lambda d: R(), workers=1)
    sched.start()
    sched.replace_plan([_D("x", 0, 0)])
    deadline = time.monotonic() + 5
    while not sched.idle() and time.monotonic() < deadline:
        time.sleep(0.01)
    sched.replace_plan([_D("x", 0, 0)])  # within backoff: not queued again
    assert sched.depth() == 0
    sched.stop()


def test_planner_cruise_rule():
    p = planner_mod.Planner(None, None, None)
    assert not p.update_cruise(10000 * 0.3048, 0)
    assert p.update_cruise(19000 * 0.3048, 0)
    assert p.update_cruise(18000 * 0.3048, 0)          # hysteresis
    assert not p.update_cruise(17000 * 0.3048, 0)
    p.update_cruise(19000 * 0.3048, 0)
    # Descending at 2000 ft/min from 18,000 ft: 13,000 ft within 3 minutes.
    assert not p.update_cruise(18000 * 0.3048, -2000 * 0.3048 / 60)


def test_path_points():
    pts = planner_mod.path_points(47.0, 8.0, 90.0, 100.0)
    assert len(pts) == 10 and pts[-1][2] == 90.0
    assert pts[-1][1] > 8.0
    assert planner_mod.path_points(47.0, 8.0, 90.0, 1.0) == [(47.0, 8.0, 0.0)]

"""The tuner end to end: plan (target, line, PC), governor fixes, sync."""

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import governor as gv  # noqa: E402
import target_cost as tc  # noqa: E402
import tuning_plan  # noqa: E402
from governor import Governor, LeverSpec, Sample  # noqa: E402
from test_governor import VALUES, Flight, _levers, _run  # noqa: E402

GB = 1024 ** 3
CONC = "autoortho.provider_origin_max_concurrency"


# --------------------------------------------------------------- fake config


class FakeCfg:
    """value / automatic_text / is_custom, like AOConfig."""

    def __init__(self, values, automatic=None, custom=()):
        self.values = dict(values)
        self.automatic = dict(automatic or values)
        self.custom = set(custom)

    def value(self, section, key):
        return self.values.get(f"{section}.{key}")

    def automatic_text(self, section, key):
        return self.automatic.get(f"{section}.{key}")

    def is_custom(self, section, key):
        return f"{section}.{key}" in self.custom


# ------------------------------------------------- bug 1: concurrency 0


def test_zero_concurrency_is_the_ceiling_not_a_number():
    levers = _levers()
    cfg = FakeCfg(dict(VALUES, **{CONC: 0}))
    values, baseline, pinned = gv.lever_state(cfg, levers)
    assert values[CONC] == 320 and baseline[CONC] == 320
    assert not pinned


def test_network_room_from_a_zero_ceiling_never_lowers_it():
    # Before the fix, 0 was a number: "raise" made it 0 + 16, clamped up to
    # the 64 floor, cutting the real ceiling (320) to 64.
    levers = _levers()
    values, baseline, pinned = gv.lever_state(FakeCfg(dict(VALUES, **{CONC: 0})), levers)
    gov = Governor(levers, values, baseline=baseline, pinned=pinned, mode="auto",
                   line_mbps=500)
    decisions = _run(gov, Flight(), 12, active=320, limit=320, backlog=20, mbps=50)
    assert not [d for d in decisions if d.lever == CONC and d.new < d.old]
    assert gov.values[CONC] == 320


def test_network_room_raises_a_ceiling_below_the_maximum():
    levers = _levers()
    values, baseline, pinned = gv.lever_state(FakeCfg(dict(VALUES, **{CONC: 128})), levers)
    gov = Governor(levers, values, baseline=baseline, pinned=pinned, mode="auto",
                   line_mbps=500)
    decisions = _run(gov, Flight(), 6, active=128, limit=128, backlog=20, mbps=50)
    move = [d for d in decisions if d.lever == CONC][0]
    assert (move.old, move.new) == (128, 144)


def test_values_outside_the_range_are_clamped_unless_pinned():
    levers = _levers()
    cfg = FakeCfg(dict(VALUES, **{"autoortho.live_tile_admission": 200,
                                  "cache.cache_mem_limit": 50}),
                  custom={"cache.cache_mem_limit"})
    values, _baseline, pinned = gv.lever_state(cfg, levers)
    assert values["autoortho.live_tile_admission"] == 64
    assert values["cache.cache_mem_limit"] == 50 and "cache.cache_mem_limit" in pinned


# ------------------------------------------- bug 2: degradation is published


def test_degradation_is_a_published_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(gv, "UNREACHABLE_AFTER_S", 20.0)
    values = dict(VALUES, **{CONC: 320})
    gov = Governor(_levers(), values, mode="auto", line_mbps=500)
    counters = {}
    now = [0.0]
    published = []
    flight = Flight()

    class Feed(gv.GovernorService):
        def sample(self):
            return flight.step(active=320, limit=320, backlog=5, real=30, standin=20)

    service = Feed(gov, read_stat=counters.get, publish=published.append,
                   clock=lambda: now[0], ao_cpu=lambda: None)
    for _ in range(30):
        service.tick()
    degrade = [d for d in gov.history if d.state == "unreachable"]
    assert degrade and degrade[0].applied and degrade[0].outcome == "kept"
    assert {degrade[0].lever: degrade[0].new} in published
    assert gov.values["autoortho.prefetch_lookahead"] == 8


def test_degradation_in_advise_is_only_listed(monkeypatch):
    monkeypatch.setattr(gv, "UNREACHABLE_AFTER_S", 20.0)
    gov = Governor(_levers(), dict(VALUES, **{CONC: 320}), mode="advise", line_mbps=500)
    _run(gov, Flight(), 30, active=320, limit=320, backlog=5, real=30, standin=20)
    degrade = [d for d in gov.history if d.state == "unreachable"]
    assert degrade and not degrade[0].applied
    assert gov.values["autoortho.prefetch_lookahead"] == 10


# ------------------------------------------------------- AutoOrtho's own CPU


def _with_ao_cpu(flight, ao, **step):
    sample = flight.step(**step)
    return Sample(**{**sample.__dict__, "ao_cpu_percent": ao})


def _window(gov, ao, n=6, **step):
    flight = Flight()
    return gv.window_of([_with_ao_cpu(flight, ao, **step) for _ in range(n)])


def test_compute_limit_follows_autoorthos_share_not_the_whole_pc():
    gov = Governor(_levers(), VALUES, ao_cpu_budget_percent=50.0)
    gov.starting = False
    # The PC is only 60 % busy, but AutoOrtho uses all it may.
    assert gov.classify(_window(gov, 48, waits=10, admissions=20, cpu=60)) == "compute_limit"
    # The PC is busy because of X-Plane... still no CPU to give.
    assert gov.classify(_window(gov, 10, waits=10, admissions=20, cpu=95)) == "compute_limit"
    assert gov.classify(_window(gov, 20, waits=10, admissions=20, cpu=60)) == "compute"


def test_headroom_uses_autoorthos_share():
    gov = Governor(_levers(), VALUES, ao_cpu_budget_percent=50.0)
    gov.starting = False
    # X-Plane keeps the PC at 70 %; AutoOrtho idles: room to ease off.
    assert gov.classify(_window(gov, 10, cpu=70)) == "headroom"
    assert gov.classify(_window(gov, 30, cpu=70)) == "ok"


def test_process_tree_cpu_reads_a_share():
    reader = gv.ProcessTreeCpu()
    first = reader()
    assert first is None or 0.0 <= first <= 100.0
    second = reader()
    assert second is None or 0.0 <= second <= 100.0


# ------------------------------------------------- server pacing vs ceiling


def test_server_pacing_is_not_raised_against_the_ceiling():
    gov = Governor(_levers(), VALUES, line_mbps=500)
    gov.starting = False
    flight = Flight()
    samples = []
    for _ in range(6):
        sample = flight.step(active=60, limit=64, backlog=20, mbps=40, real=45, standin=5)
        samples.append(Sample(**{**sample.__dict__, "broker_ceiling": 320}))
    window = gv.window_of(samples)
    assert not window.broker_ceiling_bound
    assert gov.classify(window) == "network_paced"
    assert gov._candidates("network_paced") == [("autoortho.prefetch_radius_nm", -1)]


def test_broker_ceiling_is_sampled():
    gov = Governor(_levers(), VALUES)
    stats = {"origins": {"a": {"active": 10, "limit": 64, "ceiling": 320}},
             "backlog_depth": 0}
    service = gv.GovernorService(gov, read_stat=lambda _k: 0, broker_stats=lambda: stats,
                                 ao_cpu=lambda: 12.5)
    sample = service.sample()
    assert sample.broker_limit == 64 and sample.broker_ceiling == 320
    assert sample.ao_cpu_percent == 12.5


# ------------------------------------------------------------ keep or revert


def _window_of(fidelity, reads):
    real = int(round(fidelity * reads))
    return gv.Window(30, reads, real / reads, 0, 0, 0, 0, 0, 0, 0, False, None)


def test_a_small_noisy_dip_is_not_blamed_on_the_change():
    # 95 % on 400 reads, then 90 % on 40: within chance.
    assert not Governor.fidelity_worse(_window_of(0.95, 400), _window_of(0.90, 40))
    # The same drop on plenty of reads is real.
    assert Governor.fidelity_worse(_window_of(0.95, 2000), _window_of(0.90, 2000))
    # Tiny drops never count.
    assert not Governor.fidelity_worse(_window_of(0.99, 5000), _window_of(0.98, 5000))


def test_a_phase_change_waits_then_keeps():
    gov = Governor(_levers(), VALUES, mode="auto")
    flight = Flight()
    first = _run(gov, flight, 6, waits=10, admissions=20, cpu=60)[0]
    # The aircraft lands right after the change: a different phase, and far
    # worse tiles. Not the change's fault: no revert, kept after the wait.
    later = _run(gov, flight, 40, waits=18, admissions=20, cpu=60, real=20,
                 standin=30, speed=20)
    assert not [d for d in later if d.outcome == "reverted"]
    marked = [d for d in gov.history if d.t == first.t]
    assert marked[0].outcome == "kept"


def test_judged_sooner_with_plenty_of_reads():
    gov = Governor(_levers(), VALUES, mode="auto")
    flight = Flight()
    first = _run(gov, flight, 6, waits=10, admissions=20, cpu=60)[0]
    _run(gov, flight, 6, waits=0, admissions=20, cpu=60, real=100)
    marked = [d for d in gov.history if d.t == first.t]
    assert marked[0].outcome == "kept"           # within 12 s, not 20


def test_headroom_does_not_undo_a_change_it_just_kept():
    gov = Governor(_levers(), VALUES, mode="auto")
    flight = Flight()
    first = _run(gov, flight, 6, waits=10, admissions=20, cpu=60)[0]
    # The bottleneck is gone and the CPU idles: headroom, but not for the
    # lever that just fixed it.
    later = _run(gov, flight, 30, waits=0, admissions=20, cpu=10)
    assert not [d for d in later if d.lever == first.lever]
    assert gov.values[first.lever] == first.new


# ------------------------------------------------------- mid-flight changes


def test_reconfigure_takes_new_limits_pins_and_adopted_values():
    levers = _levers()
    gov = Governor(levers, VALUES, mode="auto")
    gov.values["cache.cache_mem_limit"] = 8
    gov.values["autoortho.prefetch_radius_nm"] = 50
    gov.values["autoortho.live_tile_admission"] = 32
    gov._pending = (gv.Decision(0, "x", "x", 0, 1, "compute", ""), None, "cruise")
    smaller = dict(levers)
    smaller["cache.cache_mem_limit"] = LeverSpec("cache.cache_mem_limit", "Memory cache",
                                                 1, 2, 4, group="memory")
    configured = dict(VALUES, **{"autoortho.prefetch_radius_nm": 30,
                                 "autoortho.live_tile_admission": 12,
                                 "autoortho.background_builder_workers": 3})
    out = gov.reconfigure(
        smaller, configured, configured,
        pinned={"autoortho.live_tile_admission"},
        adopt={"autoortho.background_builder_workers"},
        ram_limit_bytes=3 * GB,
    )
    assert gov.values["cache.cache_mem_limit"] == 4        # inside the new range
    assert gov.values["autoortho.prefetch_radius_nm"] == 50  # where it was tuned
    assert gov.values["autoortho.live_tile_admission"] == 12  # the user's now
    assert gov.values["autoortho.background_builder_workers"] == 3  # adopted
    assert "autoortho.live_tile_admission" not in out
    assert out["cache.cache_mem_limit"] == 4
    assert gov.ram_limit_bytes == 3 * GB
    assert gov._pending is None


def test_service_reconfigure_publishes_only_in_auto():
    published = []
    gov = Governor(_levers(), VALUES, mode="advise")
    service = gv.GovernorService(gov, read_stat=lambda _k: 0, publish=published.append,
                                 ao_cpu=lambda: None)
    assert service.reconfigure(_levers(), VALUES, VALUES, set()) == {}
    assert published == []
    service.set_mode("auto")
    out = service.reconfigure(_levers(), VALUES, VALUES, set())
    assert out and published == [out]


def test_unpinned_lever_returns_to_the_configured_value():
    gov = Governor(_levers(), VALUES, mode="auto", pinned={"cache.cache_mem_limit"})
    gov.values["cache.cache_mem_limit"] = 7
    gov.reconfigure(_levers(), dict(VALUES, **{"cache.cache_mem_limit": 5}), VALUES, set())
    assert gov.values["cache.cache_mem_limit"] == 5


# ------------------------------------------------------------- the plan


def _cost(zoom=17, airport=18):
    return tc.target_cost("fixed", zoom, airport)


def test_plan_without_a_line_sizes_prefetch_to_the_target_only():
    plan = tuning_plan.plan(_cost())
    assert plan.capacity_mbps is None and plan.ratio is None
    # About 12 Mbit/s x 3 = 36 Mbit/s of chunks per 1 s cycle.
    assert int(plan.values["autoortho.prefetch_max_chunks"]) == 320
    for name in ("autoortho.prefetch_radius_nm", "autoortho.prefetch_lookahead",
                 "streaming.paging_lead_seconds", "streaming.band_max_wait_seconds"):
        assert name not in plan.values


def test_plan_on_a_fast_line_widens_the_corridor_and_keeps_waits():
    plan = tuning_plan.plan(_cost(), line_mbps=200, chunk_seconds=0.1)
    assert plan.ratio > 10
    assert plan.values["autoortho.prefetch_radius_nm"] == "60"
    assert plan.values["autoortho.prefetch_lookahead"] == "10"
    assert plan.values["streaming.paging_lead_seconds"] == "60"
    assert "streaming.band_max_wait_seconds" not in plan.values
    assert "streaming.near_wait_seconds" not in plan.values


def test_plan_on_a_tight_line_starts_prefetch_earlier_and_waits_longer():
    plan = tuning_plan.plan(_cost(), line_mbps=18, chunk_seconds=0.3)
    assert 1.0 <= plan.ratio < 2.0
    assert int(plan.values["autoortho.prefetch_radius_nm"]) < 30
    assert int(plan.values["autoortho.prefetch_lookahead"]) > 10
    assert int(plan.values["streaming.paging_lead_seconds"]) > 60
    assert float(plan.values["streaming.band_max_wait_seconds"]) > 6
    assert float(plan.values["streaming.near_wait_seconds"]) > 60
    # Prefetch never asks for more than the line carries.
    chunks = int(plan.values["autoortho.prefetch_max_chunks"])
    assert chunks * tc.CHUNK_BYTES * 8 / 1e6 <= 18 + 64 * tc.CHUNK_BYTES * 8 / 1e6


def test_plan_below_the_need_does_not_lengthen_lookahead():
    plan = tuning_plan.plan(_cost(), line_mbps=6)
    assert plan.ratio < 1
    assert "autoortho.prefetch_lookahead" not in plan.values
    assert plan.values["autoortho.prefetch_radius_nm"] == "15"


def test_bandwidth_limit_caps_the_line():
    plan = tuning_plan.plan(_cost(), line_mbps=500, bandwidth_mbps=18)
    assert plan.capacity_mbps == 18


def test_plan_raises_predictive_memory_for_many_builders_only():
    plan = tuning_plan.plan(_cost(), background_workers=4, ram_limit_gb=16.5,
                            current={"autoortho.predictive_dds_memory_mb": "512"})
    assert plan.values["autoortho.predictive_dds_memory_mb"] == "1024"   # 4 x 256 MB
    plan = tuning_plan.plan(_cost(), background_workers=2,
                            current={"autoortho.predictive_dds_memory_mb": "768"})
    assert "autoortho.predictive_dds_memory_mb" not in plan.values
    capped = tuning_plan.plan(_cost(), background_workers=4, ram_limit_gb=2.5,
                              current={"autoortho.predictive_dds_memory_mb": "256"})
    # A quarter of a 2.5 GB RAM limit, in 128 MB steps.
    assert capped.values["autoortho.predictive_dds_memory_mb"] == "640"


def test_planned_values_fit_the_governor_ranges():
    levers = gv.levers_for(SimpleNamespace())
    for line in (None, 6, 18, 50, 200, 1000):
        for zoom, airport in ((15, 17), (16, 18), (17, 19)):
            plan = tuning_plan.plan(_cost(zoom, airport), line_mbps=line)
            for name, text in plan.values.items():
                spec = levers.get(name)
                if spec is None:
                    continue
                assert spec.minimum <= float(text) <= spec.maximum, (name, text, line)


# ------------------------------------------------------- config integration


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    import aoconfig
    import connection_test

    monkeypatch.setattr(connection_test, "RESULT_PATH", str(tmp_path / "connection.json"))
    from hardware import HardwareInfo

    aoconfig.hardware.set_for_tests(HardwareInfo(16, 8, 32 * GB))
    yield aoconfig, connection_test
    aoconfig.hardware.set_for_tests(None)


def test_config_plans_from_the_target_and_the_line(isolated, tmp_path):
    aoconfig, ct = isolated
    path = tmp_path / ".autoortho"
    path.write_text("[general]\nconfig_revision = 2\n[autoortho]\nmax_zoom = 17\n",
                    encoding="utf-8")
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.source("autoortho", "prefetch_max_chunks") == "planned"
    assert int(cfg.autoortho.prefetch_max_chunks) == 320
    assert cfg.source("autoortho", "prefetch_radius_nm") == "default"

    ct.save_result(ct.Result("2026-11-12T10:00:00+00:00", "BI",
                             [ct.Round(128, 1024, 0, 16_000_000, 4.0, 0.3, 18.0)], 128))
    cfg = aoconfig.AOConfig(str(path))
    assert cfg.source("autoortho", "prefetch_radius_nm") == "planned"
    assert int(cfg.autoortho.prefetch_radius_nm) < 30
    assert cfg.source("autoortho", "provider_origin_max_concurrency") == "planned"
    assert int(cfg.autoortho.provider_origin_max_concurrency) > 0
    assert not cfg.is_custom("autoortho", "prefetch_radius_nm")
    # Planned values are never written as the user's own.
    text = path.read_text(encoding="utf-8")
    assert "\nprefetch_radius_nm" not in text


def test_users_own_value_beats_the_plan(isolated, tmp_path):
    aoconfig, _ct = isolated
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[autoortho]\nmax_zoom = 17\n"
        "prefetch_max_chunks = 100\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert int(cfg.autoortho.prefetch_max_chunks) == 100
    assert cfg.source("autoortho", "prefetch_max_chunks") == "custom"


def test_turning_the_baseline_off_turns_the_plan_off(isolated, tmp_path):
    aoconfig, _ct = isolated
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[tuning]\nhardware_baseline = False\n",
        encoding="utf-8",
    )
    cfg = aoconfig.AOConfig(str(path))
    assert int(cfg.autoortho.prefetch_max_chunks) == 64


def test_auto_is_the_default_mode(isolated, tmp_path):
    aoconfig, _ct = isolated
    cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    assert str(cfg.governor.mode) == "auto"


# ------------------------------------------------------- mount wiring


@pytest.fixture
def mount(isolated, monkeypatch, tmp_path):
    import autoortho as ao_module

    aoconfig, _ct = isolated
    monkeypatch.setattr(gv, "DATA_DIR", str(tmp_path / "gov"))
    m = ao_module.AOMount.__new__(ao_module.AOMount)
    m.cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    m.cfg.set_value("governor", "record_samples", "False")
    m._download_broker = None
    m.published = []
    m.publish_levers = lambda levers: m.published.append(dict(levers)) or dict(levers)
    yield m
    if getattr(m, "governor_service", None) is not None:
        m.governor_service.stop()
        m.governor_service = None


def test_mount_gives_the_governor_its_cpu_budget_and_real_ceiling(mount):
    service = mount.start_governor()
    gov = service.governor
    assert gov.mode == "auto"                       # the default now
    # 8 cores, 2 kept for X-Plane: 6 cores x 2 threads of 16.
    assert gov.ao_cpu_budget_percent == pytest.approx(75.0)
    assert gov.values[CONC] == gov.levers[CONC].maximum > 64


def test_new_limits_mid_flight_reach_the_running_governor(mount, monkeypatch):
    service = mount.start_governor()
    gov = service.governor
    assert gov.levers["cache.cache_mem_limit"].maximum >= 8
    gov.values["autoortho.prefetch_radius_nm"] = 50.0      # tuned in flight
    mount.published.clear()
    mount.cfg.set_value("limits", "ram_gb", "3")
    mount.apply_live_levers()
    assert gov.levers["cache.cache_mem_limit"].maximum == 2
    assert gov.values["cache.cache_mem_limit"] == 2
    assert gov.values["autoortho.prefetch_radius_nm"] == 50   # kept
    assert gov.ram_limit_bytes == int(2.5 * GB)
    # The workers get the governor's values back, not only the new limits.
    merged = {}
    for levers in mount.published:
        merged.update(levers)
    assert merged["autoortho.prefetch_radius_nm"] == 50


def test_a_value_set_by_hand_mid_flight_is_pinned(mount):
    service = mount.start_governor()
    mount.cfg.set_value("autoortho", "live_tile_admission", "10")
    mount.sync_governor()
    gov = service.governor
    assert "autoortho.live_tile_admission" in gov.pinned
    assert gov.values["autoortho.live_tile_admission"] == 10


def test_only_levers_that_moved_are_learned(mount, monkeypatch):
    import aostats

    monkeypatch.setattr(aostats, "set_stat", lambda k, v: None)
    service = mount.start_governor()
    gov = service.governor
    gov.values["autoortho.prefetch_radius_nm"] = gov.baseline["autoortho.prefetch_radius_nm"] + 10
    mount.stop_governor()
    learned = gv.LearnedProfiles().get(mount._governor_key)
    assert set(learned) == {"autoortho.prefetch_radius_nm"}

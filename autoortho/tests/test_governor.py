"""Phases 5-6: the governor (classifier, decisions, keep/revert, learning)."""

import json
import os
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import governor as gv  # noqa: E402
from governor import Governor, LeverSpec, Sample  # noqa: E402

GB = 1024 ** 3


def _levers():
    return {
        "autoortho.provider_origin_max_concurrency": LeverSpec(
            "autoortho.provider_origin_max_concurrency", "Parallel downloads",
            16, 64, 320, group="network"),
        "autoortho.live_tile_admission": LeverSpec(
            "autoortho.live_tile_admission", "Live tiles at once", 4, 4, 64, group="compute"),
        "autoortho.background_builder_workers": LeverSpec(
            "autoortho.background_builder_workers", "Background builders", 1, 1, 8,
            group="compute"),
        "autoortho.predictive_dds_build_interval_ms": LeverSpec(
            "autoortho.predictive_dds_build_interval_ms", "Background build pause",
            100, 100, 1000, unit="ms", group="compute"),
        "cache.cache_mem_limit": LeverSpec("cache.cache_mem_limit", "Memory cache", 1, 2, 8,
                                           unit="GB", group="memory"),
        "autoortho.tile_image_cache_mb": LeverSpec(
            "autoortho.tile_image_cache_mb", "Tile image cache", 32, 32, 256, group="memory"),
        "autoortho.predictive_dds_memory_mb": LeverSpec(
            "autoortho.predictive_dds_memory_mb", "Predictive build memory", 128, 128, 2048,
            group="memory"),
        "streaming.band_max_wait_seconds": LeverSpec(
            "streaming.band_max_wait_seconds", "Wait for near imagery", 1, 2, 12,
            integer=False, group="waits"),
        "streaming.near_wait_seconds": LeverSpec(
            "streaming.near_wait_seconds", "Wait while starting", 10, 20, 120,
            integer=False, group="waits"),
        "autoortho.prefetch_lookahead": LeverSpec(
            "autoortho.prefetch_lookahead", "Prefetch lookahead", 2, 4, 30, group="prefetch"),
        "autoortho.prefetch_radius_nm": LeverSpec(
            "autoortho.prefetch_radius_nm", "Prefetch radius", 10, 10, 80, group="prefetch"),
    }


VALUES = {
    "autoortho.provider_origin_max_concurrency": 128,
    "autoortho.live_tile_admission": 16,
    "autoortho.background_builder_workers": 2,
    "autoortho.predictive_dds_build_interval_ms": 250,
    "cache.cache_mem_limit": 4,
    "autoortho.tile_image_cache_mb": 96,
    "autoortho.predictive_dds_memory_mb": 512,
    "streaming.band_max_wait_seconds": 6,
    "streaming.near_wait_seconds": 60,
    "autoortho.prefetch_lookahead": 10,
    "autoortho.prefetch_radius_nm": 30,
}


class Flight:
    """Builds cumulative samples every 2 s with per-step rates."""

    def __init__(self):
        self.t = 0.0
        self.c = dict(near_real=0, near_standin=0, req_ok=0, req_err=0,
                      bytes_dl=0, admission_waits=0, admissions=0)

    def step(self, *, real=50, standin=0, ok=100, err=0, mbps=50.0, waits=0,
             admissions=20, cpu=40.0, free=16 * GB, ao=3 * GB, active=10, limit=128,
             backlog=0, connected=True, speed=250, alt=10000, paused=False,
             frame=None, late_flight=(), late_start=()):
        self.t += 2.0
        c = self.c
        c["near_real"] += real
        c["near_standin"] += standin
        c["req_ok"] += ok
        c["req_err"] += err
        c["bytes_dl"] += int(mbps * 1e6 / 8 * 2)
        c["admission_waits"] += waits
        c["admissions"] += admissions
        return Sample(t=self.t, cpu_percent=cpu, free_ram_bytes=free,
                      total_ram_bytes=32 * GB, ao_memory_bytes=ao,
                      broker_active=active, broker_limit=limit,
                      broker_waiting=backlog, connected=connected,
                      speed_kt=speed, altitude_ft=alt, paused=paused,
                      frame_time_ms=frame, late_flight=tuple(late_flight),
                      late_start=tuple(late_start), **c)


def _late(seconds, count):
    """Late-arrival bucket counts with ``count`` arrivals after ``seconds``."""
    from wait_bands import LATE_BUCKETS_S, late_bucket

    counts = [0] * (len(LATE_BUCKETS_S) + 1)
    counts[late_bucket(seconds)] = count
    return tuple(counts)


def _run(gov, flight, n, **step):
    out = []
    for _ in range(n):
        decision = gov.observe(flight.step(**step), starting=False)
        if decision is not None:
            out.append(decision)
    return out


def test_window_rates():
    flight = Flight()
    samples = [flight.step(real=40, standin=10, ok=97, err=3, mbps=80) for _ in range(5)]
    w = gv.window_of(samples)
    assert w.fidelity == pytest.approx(0.8)
    assert w.error_share == pytest.approx(0.03)
    assert w.mbps == pytest.approx(80, rel=0.01)
    assert gv.window_of(samples[:1]) is None


def test_classifier_states():
    gov = Governor(_levers(), VALUES, line_mbps=200, ram_limit_bytes=8 * GB,
                   brake_free_bytes=2 * GB)
    gov.starting = False
    flight = Flight()
    cases = [
        (dict(free=int(2.2 * GB)), "memory"),
        (dict(ao=int(7.5 * GB)), "memory"),
        (dict(ok=90, err=10), "network_pushback"),
        (dict(waits=10, admissions=20, cpu=50), "compute"),
        (dict(waits=10, admissions=20, cpu=95), "compute_limit"),
        (dict(active=128, limit=128, backlog=20, mbps=50), "network_room"),
        (dict(active=128, limit=128, backlog=20, mbps=190), "network_full"),
        (dict(cpu=20), "headroom"),
        # Blurry, and the imagery came soon after the 6 s wait: wait longer.
        (dict(real=40, standin=10, late_flight=_late(8, 30)), "wait"),
        # Blurry, but most of it took longer than any wait allows.
        (dict(real=40, standin=10, late_flight=_late(30, 30)), "slow_imagery"),
        # Blurry with no evidence that waiting helps: no wait raise.
        (dict(real=40, standin=10), "behind"),
    ]
    for step, expected in cases:
        samples = [flight.step(**step) for _ in range(6)]
        assert gov.classify(gv.window_of(samples)) == expected, expected


def test_too_few_near_reads_is_warming_up():
    gov = Governor(_levers(), VALUES)
    flight = Flight()
    samples = [flight.step(real=1) for _ in range(4)]
    assert gov.classify(gv.window_of(samples)) == "warming_up"


def test_advise_suggests_without_changing_values():
    gov = Governor(_levers(), VALUES, mode="advise", line_mbps=500)
    decisions = _run(gov, Flight(), 6, active=128, limit=128, backlog=10)
    assert decisions and decisions[0].lever == "autoortho.provider_origin_max_concurrency"
    assert decisions[0].new == 144 and decisions[0].applied is False
    assert gov.values["autoortho.provider_origin_max_concurrency"] == 128
    assert "queue full" in decisions[0].reason


def test_auto_applies_one_lever_per_tick_with_cooldown():
    gov = Governor(_levers(), VALUES, mode="auto", line_mbps=500)
    decisions = _run(gov, Flight(), 6, waits=10, admissions=20, cpu=40)
    assert len(decisions) == 1
    assert decisions[0].lever == "autoortho.live_tile_admission"
    assert gov.values["autoortho.live_tile_admission"] == 20
    # Within the cooldown the next compute lever moves instead.
    more = _run(gov, Flight(), 4, waits=10, admissions=20, cpu=40)
    levers = [d.lever for d in more if not d.reason.startswith("undoing")]
    assert "autoortho.live_tile_admission" not in levers[:1]


def test_starting_uses_bigger_steps():
    gov = Governor(_levers(), VALUES, mode="advise")
    flight = Flight()
    decision = None
    for _ in range(6):
        decision = gov.observe(
            flight.step(real=40, standin=10, late_start=_late(90, 10)), starting=True
        ) or decision
    assert decision.lever == "streaming.near_wait_seconds"
    assert decision.new == 80     # step 10, doubled while starting


def test_keep_or_revert():
    gov = Governor(_levers(), VALUES, mode="auto")
    flight = Flight()
    first = _run(gov, flight, 6, waits=10, admissions=20, cpu=40)[0]
    # After the change, tiles wait more: revert.
    later = _run(gov, flight, 12, waits=18, admissions=20, cpu=40, real=40, standin=10)
    reverts = [d for d in later if d.outcome == "reverted"]
    assert reverts and reverts[0].lever == first.lever
    assert gov.values[first.lever] == first.old
    assert any(d.outcome == "reverted" for d in gov.history if d.t == first.t)


def test_kept_when_it_helps():
    gov = Governor(_levers(), VALUES, mode="auto")
    flight = Flight()
    first = _run(gov, flight, 6, waits=10, admissions=20, cpu=40)[0]
    _run(gov, flight, 12, waits=0, admissions=20, cpu=40)
    marked = [d for d in gov.history if d.t == first.t]
    assert marked[0].outcome == "kept"
    assert gov.values[first.lever] == first.new


def test_pinned_levers_are_never_moved():
    gov = Governor(_levers(), VALUES, mode="auto",
                   pinned={"autoortho.live_tile_admission"})
    decisions = _run(gov, Flight(), 6, waits=10, admissions=20, cpu=40)
    assert decisions[0].lever == "autoortho.background_builder_workers"
    assert gov.values["autoortho.live_tile_admission"] == 16


def test_headroom_eases_toward_baseline():
    values = dict(VALUES)
    values["autoortho.provider_origin_max_concurrency"] = 192
    gov = Governor(_levers(), values, baseline=VALUES, mode="auto")
    decisions = _run(gov, Flight(), 6, cpu=10)
    assert decisions[0].state == "headroom"
    assert decisions[0].new == 176


def test_limits_bound_the_levers():
    gov = Governor(_levers(), dict(VALUES, **{"autoortho.provider_origin_max_concurrency": 320}),
                   mode="auto")
    decisions = _run(gov, Flight(), 6, active=320, limit=320, backlog=5)
    assert not [d for d in decisions if d.lever == "autoortho.provider_origin_max_concurrency"]


def test_unreachable_degrades_far_work_and_notifies(monkeypatch):
    monkeypatch.setattr(gv, "UNREACHABLE_AFTER_S", 20.0)
    values = dict(VALUES, **{"autoortho.provider_origin_max_concurrency": 320})
    gov = Governor(_levers(), values, mode="auto", line_mbps=500)
    _run(gov, Flight(), 30, active=320, limit=320, backlog=5, real=30, standin=20)
    assert gov.state == "unreachable"
    assert "autoortho.prefetch_lookahead" in gov.degraded
    assert gov.values["autoortho.prefetch_lookahead"] == 8
    assert "your connection" in gov.notice
    # The target itself is never touched.
    assert set(gov.values) == set(VALUES)


def test_off_mode_decides_nothing_but_measures():
    gov = Governor(_levers(), VALUES, mode="off")
    decisions = _run(gov, Flight(), 6, waits=10, admissions=20)
    assert decisions == []
    assert gov.window is not None and gov.state == "compute"


def test_paused_freezes():
    gov = Governor(_levers(), VALUES, mode="auto")
    flight = Flight()
    _run(gov, flight, 3)
    assert gov.observe(flight.step(paused=True)) is None
    assert gov.state == "paused"


def test_summary_and_phases():
    gov = Governor(_levers(), VALUES, mode="advise")
    flight = Flight()
    _run(gov, flight, 10, speed=250, alt=30000)
    _run(gov, flight, 10, speed=150, alt=1000, real=40, standin=10)
    summary = gov.summary()
    assert summary["fidelity_by_phase"]["cruise"] == 1.0
    assert summary["fidelity_by_phase"]["departure_arrival"] == pytest.approx(0.8)
    assert summary["verdict"] in ("A few blurry moments", "Often blurry")
    assert summary["mode"] == "advise"


def test_replay_is_deterministic(tmp_path):
    flight = Flight()
    samples = [flight.step(active=128, limit=128, backlog=10) for _ in range(12)]
    path = tmp_path / "samples.jsonl"
    path.write_text("\n".join(json.dumps(asdict(s)) for s in samples), encoding="utf-8")
    loaded = gv.load_samples(path)
    assert loaded == samples
    first = gv.replay(loaded, _levers(), VALUES, mode="advise", line_mbps=500)
    second = gv.replay(loaded, _levers(), VALUES, mode="advise", line_mbps=500)
    assert first == second and first


def test_levers_follow_limits():
    import limits
    from hardware import HardwareInfo

    cfg = SimpleNamespace()
    pc = HardwareInfo(16, 8, 32 * GB, 12 * GB)
    specs = gv.levers_for(cfg, limits.resolve({"bandwidth_mbps": "20", "ram_gb": "4"}, pc))
    assert specs["autoortho.provider_origin_max_concurrency"].maximum == 64
    assert specs["cache.cache_mem_limit"].maximum == 3
    assert specs["autoortho.background_builder_workers"].maximum == 3


def test_learned_profiles(tmp_path):
    store = gv.LearnedProfiles(str(tmp_path / "learned.json"))
    assert store.get("k") == {}
    store.put("k", {"a.b": 3}, {"peak_mbps": 90})
    assert store.get("k") == {"a.b": 3}
    store.clear()
    assert store.get("k") == {}


def test_fingerprint_is_salted_and_stable(tmp_path):
    one = gv.network_fingerprint(str(tmp_path / "a"))
    assert one == gv.network_fingerprint(str(tmp_path / "a"))
    assert one != gv.network_fingerprint(str(tmp_path / "b"))
    key = gv.profile_key({"zoom": 16}, "BI", one)
    assert key.endswith(f":BI:{one}")


def test_service_tick_applies_in_auto(tmp_path):
    counters = {"near_reads_real": 0, "near_reads_standin": 0, "live_admissions": 0,
                "live_admission_waits": 0, "req_ok": 0, "bytes_dl": 0}
    now = [0.0]
    published = []

    def read(key):
        return counters.get(key, 0)

    gov = Governor(_levers(), VALUES, mode="auto")
    service = gv.GovernorService(
        gov, read_stat=read, publish=published.append,
        flight=lambda: {"alt": 3000, "spd": 120, "lat": 50.0, "lon": 8.0},
        record_path=str(tmp_path / "rec.jsonl"), clock=lambda: now[0],
    )
    decision = None
    for _ in range(6):
        now[0] += 2
        counters["near_reads_real"] += 50
        counters["live_admissions"] += 20
        counters["live_admission_waits"] += 10
        counters["req_ok"] += 100
        decision = service.tick() or decision
    assert decision is not None and decision.applied
    assert published == [{decision.lever: decision.new}]
    snap = service.snapshot()
    assert snap["mode"] == "auto" and snap["timeline"]
    assert snap["timeline"][-1]["fidelity"] == 1.0
    assert len(gv.load_samples(tmp_path / "rec.jsonl")) == 6
    service.set_mode("advise")
    assert service.snapshot()["mode"] == "advise"


def test_protect_sim_fps_eases_background_work():
    gov = Governor(_levers(), VALUES, mode="auto", protect_sim_fps=True)
    flight = Flight()
    _run(gov, flight, 20, cpu=60, frame=16.0)        # 62 fps sets the best
    assert gov.frame_best_ms == pytest.approx(16.0)
    decisions = _run(gov, flight, 14, cpu=60, frame=30.0)   # 33 fps
    assert gov.state == "sim_fps" or decisions
    first = decisions[0]
    assert first.state == "sim_fps"
    assert first.lever == "autoortho.background_builder_workers" and first.new == 1
    assert "fps" in first.reason and "was 62" in first.reason
    # Frames recover and stay sharp: the change is kept.
    decisions = _run(gov, flight, 12, cpu=60, frame=17.0)
    assert not [d for d in decisions if d.outcome == "reverted"]
    assert gov.values["autoortho.background_builder_workers"] == 1


def test_protect_sim_fps_off_or_unknown_frames_change_nothing():
    flight = Flight()
    off = Governor(_levers(), VALUES, protect_sim_fps=False)
    _run(off, flight, 20, frame=16.0)
    samples = [flight.step(frame=30.0) for _ in range(6)]
    assert off.classify(gv.window_of(samples)) != "sim_fps"
    on = Governor(_levers(), VALUES, protect_sim_fps=True)
    samples = [flight.step() for _ in range(6)]     # no frame data
    assert on.classify(gv.window_of(samples)) != "sim_fps"
    # High fps that dips but stays above 40 fps is fine.
    _run(on, flight, 20, frame=8.0)
    samples = [flight.step(frame=12.0) for _ in range(6)]
    assert on.classify(gv.window_of(samples)) != "sim_fps"


def test_service_reads_frame_time_and_pause():
    governor = Governor(_levers(), VALUES)
    service = gv.GovernorService(
        governor, read_stat=lambda k: 0,
        flight=lambda: {"alt": 1000, "spd": 100, "lat": 47.0, "lon": 11.0,
                        "frame_period": 0.025, "paused": True},
    )
    sample = service.sample()
    assert sample.frame_time_ms == pytest.approx(25.0)
    assert sample.paused is True
    service.tick()
    assert governor.state == "paused"
    assert service.snapshot()["fps"] is None   # one sample is not a window


def test_datareftrack_decodes_frame_period_and_pause():
    import struct
    import datareftrack

    tracker = datareftrack.DatarefTracker.__new__(datareftrack.DatarefTracker)
    values = [47.0, 11.0, 300.0, 90.0, 120.0, 36000.0, 5000.0, 20.0, 2560.0, 80.0,
              0.02, 1.0]
    payload = b"".join(struct.pack("<if", i, v) for i, v in enumerate(values))
    packet = tracker.RREF_HEADER + b"\x00" + payload
    decoded = tracker._decode_packet(packet)
    assert decoded[10] == pytest.approx(0.02) and decoded[11] == 1.0


# ------------------------------------------------- waits need evidence


def test_wait_is_raised_no_further_than_the_imagery_needed():
    values = dict(VALUES, **{"streaming.near_wait_seconds": 50})
    gov = Governor(_levers(), values, mode="advise")
    flight = Flight()
    decision = None
    for _ in range(6):
        decision = gov.observe(
            flight.step(real=40, standin=10, late_start=_late(60, 10)), starting=True
        ) or decision
    assert decision.lever == "streaming.near_wait_seconds"
    assert decision.new == 60     # 50 + 20 would overshoot the 60 s needed
    assert "most late imagery within 60 s" in decision.reason


def test_too_few_late_reads_raise_no_wait():
    gov = Governor(_levers(), VALUES)
    gov.starting = False
    flight = Flight()
    samples = [flight.step(real=40, standin=10, late_flight=_late(8, 5)) for _ in range(6)]
    assert gov.wait_evidence(gv.window_of(samples)) == ("unknown", 8.0)
    assert gov.classify(gv.window_of(samples)) == "behind"


def test_wait_already_long_enough_is_not_raised():
    gov = Governor(_levers(), VALUES)
    gov.starting = False
    flight = Flight()
    samples = [flight.step(real=40, standin=10, late_flight=_late(5, 40)) for _ in range(6)]
    assert gov.wait_evidence(gv.window_of(samples)) == ("enough", 5.0)
    assert gov.classify(gv.window_of(samples)) == "behind"


def test_slow_imagery_asks_for_it_earlier_and_never_raises_the_wait():
    gov = Governor(_levers(), VALUES, mode="advise")
    decisions = _run(gov, Flight(), 8, real=40, standin=10, late_flight=_late(45, 40))
    assert decisions
    assert all(d.lever == "autoortho.prefetch_lookahead" for d in decisions)
    assert "most late imagery within 45 s" in decisions[0].reason


def test_late_samples_survive_a_replay_file(tmp_path):
    flight = Flight()
    samples = [flight.step(late_flight=_late(8, 3)) for _ in range(3)]
    path = tmp_path / "samples.jsonl"
    path.write_text("\n".join(json.dumps(asdict(s)) for s in samples), encoding="utf-8")
    assert gv.load_samples(path) == samples


def test_accepted_suggestions_are_kept_beside_learned_levers(tmp_path):
    store = gv.LearnedProfiles(str(tmp_path / "learned.json"))
    store.put_applied("k", "autoortho.live_tile_admission", 20)
    store.put("k", {"autoortho.provider_origin_max_concurrency": 160})
    assert store.applied("k") == {"autoortho.live_tile_admission": 20.0}
    assert store.warm_start("k", "advise") == {"autoortho.live_tile_admission": 20.0}
    assert store.warm_start("k", "auto") == {
        "autoortho.live_tile_admission": 20.0,
        "autoortho.provider_origin_max_concurrency": 160,
    }
    assert store.warm_start("k", "off") == {}
    # What Auto settled on wins over an older suggestion.
    store.put("k", {"autoortho.live_tile_admission": 24})
    assert store.warm_start("k", "auto")["autoortho.live_tile_admission"] == 24
    store.clear()
    assert store.warm_start("k", "auto") == {}

"""Performance plan, item 2: diagnostics and statistics that cost little."""

import json
import time

import pytest

from autoortho import diagnostics
from autoortho.prefetch import PrefetchCoordinator


def _profiler(tmp_path, role="mount-worker:test"):
    profiler = diagnostics.PerformanceProfiler(
        session_dir=tmp_path,
        session_id="session-1",
        role=role,
        checkpoint_interval=10.0,
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    return profiler


def _fill(profiler, count):
    for i in range(count):
        sample = (
            1_700_000_000.0 + i, float(i), 1_000_000 + i, 900_000 + i,
            5_000_000, 0, 12.5, 40, 10 * i, 5 * i,
        )
        diagnostics._append_bounded_sample(
            profiler._memory_samples, sample, diagnostics._MAX_SELF_MEMORY_SAMPLES
        )
        profiler._unsaved_samples.append(sample)
    for i in range(50):
        profiler.record(f"stage.{i % 7}", float(i), tile_id=f"t{i}")
    profiler.set_gauge("g", 3)


def _checkpoint(profiler):
    profiler._last_checkpoint = -1e9
    profiler._write_checkpoint_if_due()


def test_checkpoint_is_small_and_samples_are_appended(tmp_path):
    profiler = _profiler(tmp_path)
    _fill(profiler, 5_000)
    _checkpoint(profiler)

    process_path = profiler._process_path()
    samples_path = diagnostics._samples_path_for(process_path)
    assert process_path.stat().st_size < 200 * 1024
    assert len(samples_path.read_text(encoding="utf-8").splitlines()) == 5_000
    assert json.loads(process_path.read_text(encoding="utf-8"))["memory_samples"] == []

    # A second checkpoint appends only what is new, and the checkpoint JSON
    # does not grow with the flight.
    size_before = process_path.stat().st_size
    _fill(profiler, 100)
    _checkpoint(profiler)
    assert len(samples_path.read_text(encoding="utf-8").splitlines()) == 5_100
    assert process_path.stat().st_size < size_before * 1.5


def test_finalized_report_matches_the_inline_layout(tmp_path):
    new_dir = tmp_path / "new"
    old_dir = tmp_path / "old"
    profiler = _profiler(new_dir)
    _fill(profiler, 5_000)
    _checkpoint(profiler)

    # What the old code wrote for the same input: one JSON with every sample.
    old_dir.mkdir()
    old_profile = profiler._build_process_profile([], profile_status="checkpoint")
    old_profile["ended_wall_time"] = json.loads(
        profiler._process_path().read_text(encoding="utf-8")
    )["ended_wall_time"]
    for key in ("ended_at", "duration_seconds"):
        old_profile[key] = json.loads(
            profiler._process_path().read_text(encoding="utf-8")
        )[key]
    (old_dir / profiler._process_path().name).write_text(
        json.dumps(old_profile, indent=2, sort_keys=True), encoding="utf-8"
    )

    diagnostics.finalize_session_report(new_dir, "session-1")
    diagnostics.finalize_session_report(old_dir, "session-1")
    new_report = json.loads((new_dir / "report.json").read_text(encoding="utf-8"))
    old_report = json.loads((old_dir / "report.json").read_text(encoding="utf-8"))
    new_report.pop("generated_at")
    old_report.pop("generated_at")
    assert new_report == old_report
    assert len(new_report["process_profiles"][0]["memory_samples"]) == 5_000


def test_stop_writes_samples_inline_and_drops_the_samples_file(tmp_path):
    profiler = _profiler(tmp_path)
    _fill(profiler, 300)
    _checkpoint(profiler)
    samples_path = diagnostics._samples_path_for(profiler._process_path())
    assert samples_path.exists()
    profiler.stop()
    assert not samples_path.exists()
    profile = diagnostics.load_process_profile(profiler._process_path())
    assert profile["profile_status"] == "complete"
    assert len(profile["memory_samples"]) >= 300


def test_samples_file_survives_a_line_cut_short(tmp_path):
    profiler = _profiler(tmp_path)
    _fill(profiler, 10)
    _checkpoint(profiler)
    samples_path = diagnostics._samples_path_for(profiler._process_path())
    with samples_path.open("a", encoding="utf-8") as handle:
        handle.write("[1700000000.0,1.0,12")
    profile = diagnostics.load_process_profile(profiler._process_path())
    assert len(profile["memory_samples"]) == 10
    assert "samples_file" not in profile


def test_helper_processes_sample_every_five_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("AO_PROFILE_ENABLED", "1")
    monkeypatch.setenv("AO_PROFILE_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("AO_PROFILE_SESSION_ID", "s")
    monkeypatch.setenv("AO_PROFILE_INTERVAL", "1.0")
    started = []
    monkeypatch.setattr(
        diagnostics, "_activate_profiler", lambda p: started.append(p) or p
    )
    diagnostics.start_worker_profiler_from_env("worker")
    diagnostics.start_worker_profiler_from_env("http2-broker", helper=True)
    assert started[0].sample_interval == 1.0
    assert started[1].sample_interval == diagnostics._HELPER_SAMPLE_SECONDS


def test_parent_samples_slowly_but_tells_workers_the_configured_interval(
    tmp_path, monkeypatch
):
    class Section:
        performance_profiling = True
        report_dir = str(tmp_path)
        sample_interval_seconds = 1.0

    class Cfg:
        diagnostics = Section()

    started = []
    monkeypatch.setattr(
        diagnostics, "_activate_profiler", lambda p: started.append(p) or p
    )
    profiler = diagnostics.start_parent_profiler(Cfg())
    assert profiler.sample_interval == diagnostics._HELPER_SAMPLE_SECONDS
    assert profiler.child_environment()["AO_PROFILE_INTERVAL"] == "1.0"


# ------------------------------------------------- prefetch gauge publication


class _Getter:
    def cancel_prefetch_work(self, reason):
        return 0


class _Cacher:
    def _resolve_maptype(self, row, col, maptype, zoom):
        return maptype


def test_coordinator_publishes_gauges_a_few_times_a_second():
    calls = []
    coordinator = PrefetchCoordinator(
        tile_cacher=_Cacher(),
        chunk_getter=_Getter(),
        scenery_id="test",
        gauge=lambda name, value: calls.append((name, value)),
    )
    coordinator.start()
    try:
        before = len(calls)
        for i in range(10_000):
            coordinator._materialized_chunks = i
            coordinator._report()
        published = [c for c in calls[before:] if c[0] == "prefetch_materialized_chunks"]
        assert len(calls) - before < 100
        # The last state is published shortly after by the coordinator thread.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            published = [
                c for c in calls if c[0] == "prefetch_materialized_chunks"
            ]
            if published and published[-1][1] == 9_999:
                break
            time.sleep(0.02)
        assert published[-1][1] == 9_999
    finally:
        coordinator.stop()


def test_forced_report_publishes_at_once():
    calls = []
    coordinator = PrefetchCoordinator(
        tile_cacher=_Cacher(),
        chunk_getter=_Getter(),
        scenery_id="test",
        gauge=lambda name, value: calls.append((name, value)),
    )
    coordinator._report()
    coordinator._materialized_chunks = 7
    coordinator._report()  # throttled
    assert ("prefetch_materialized_chunks", 7) not in calls
    coordinator._report(force=True)
    assert ("prefetch_materialized_chunks", 7) in calls

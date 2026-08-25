import json
import subprocess
import sys
import time

from autoortho.diagnostics import (
    PerformanceProfiler,
    StageAggregate,
    _append_bounded_sample,
    _aggregate_memory_timeline,
)


def test_stage_aggregate_tracks_distribution_and_failures():
    aggregate = StageAggregate()
    for duration in (1.0, 5.0, 25.0, 500.0):
        aggregate.observe(duration, "ok")
    aggregate.observe(2_000.0, "timeout")

    result = aggregate.to_dict()

    assert result["count"] == 5
    assert result["avg_ms"] == 506.2
    assert result["p50_ms"] == 25.0
    assert result["p95_ms"] == 2_500.0
    assert result["max_ms"] == 2_000.0
    assert result["error_count"] == 1
    assert result["outcomes"] == {"ok": 4, "timeout": 1}


def test_session_report_combines_process_timing_and_memory(tmp_path):
    session_dir = tmp_path / "performance-test-session"
    worker = PerformanceProfiler(
        session_dir=session_dir,
        session_id="test-session",
        role="mount-worker:test",
        sample_interval=0.05,
        slow_operation_ms=5.0,
        max_slow_operations=10,
    ).start()
    worker.record(
        "network.http_request",
        1_500.0,
        tile_id="1_2_BI_16",
        details={"status_code": 200},
    )
    worker.set_gauge("tile_cache.tiles", 12)
    time.sleep(0.06)
    worker.stop()

    parent = PerformanceProfiler(
        session_dir=session_dir,
        session_id="test-session",
        role="parent",
        sample_interval=0.05,
        slow_operation_ms=5.0,
        max_slow_operations=10,
        metadata={
            "config": {
                "cache": {"cache_mem_limit": 4},
                "diagnostics": {"performance_profiling": True},
            }
        },
    ).start()
    parent.record("fuse.dds_read", 1_700.0, tile_id="1_2_BI_16")
    report_path = parent.stop(
        stats_snapshot={"chunk_hit": 20, "chunk_miss": 4},
        finalize_session=True,
    )

    assert report_path == session_dir / "report.md"
    assert report_path.exists()
    markdown = report_path.read_text(encoding="utf-8")
    assert "mount-worker:test" in markdown
    assert "network.http_request" in markdown
    assert "HTTP request p95 exceeds 1 second" in markdown

    report = json.loads((session_dir / "report.json").read_text(encoding="utf-8"))
    assert report["process_count"] == 2
    assert report["stats_snapshot"]["chunk_hit"] == 20
    assert report["peak_total_rss_bytes"] > 0
    assert any(
        row["stage"] == "fuse.dds_read"
        for row in report["stages"]
    )
    assert any(
        row["name"] == "tile_cache.tiles" and row["max"] == 12
        for row in report["gauges"]
    )


def test_parent_preserves_samples_for_ungraceful_worker(tmp_path):
    session_dir = tmp_path / "performance-observed-worker"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.15)"]
    )
    parent = PerformanceProfiler(
        session_dir=session_dir,
        session_id="observed-worker",
        role="parent",
        sample_interval=0.05,
    ).start()
    parent.register_process(child.pid, "mount-worker:crashed")
    child.wait(timeout=2)
    report_path = parent.stop(finalize_session=True)

    report = json.loads(
        report_path.with_name("report.json").read_text(encoding="utf-8")
    )
    worker = next(
        row for row in report["processes"]
        if row["role"] == "mount-worker:crashed"
    )
    assert worker["peak_rss_bytes"] > 0
    assert worker["sample_count"] >= 1
    assert any(
        "flight-stage latency and worker gauges are incomplete" in flag
        for flag in report["diagnostic_flags"]
    )


def test_parent_startup_probe_does_not_trigger_flight_http_flag(tmp_path):
    session_dir = tmp_path / "performance-parent-probe"
    parent = PerformanceProfiler(
        session_dir=session_dir,
        session_id="parent-probe",
        role="parent",
        sample_interval=0.05,
    ).start()
    parent.record(
        "network.http_request",
        2_000.0,
        tile_id="probe",
        details={"status_code": 200},
    )

    report_path = parent.stop(finalize_session=True)
    report = json.loads(report_path.with_name("report.json").read_text("utf-8"))

    assert not any(
        "HTTP request p95 exceeds" in flag
        for flag in report["diagnostic_flags"]
    )


def test_memory_timeline_preserves_subsecond_samples():
    profile = {
        "pid": 10,
        "role": "worker",
        "ended_wall_time": 101.0,
        "memory_samples": [
            {
                "timestamp": 100.1,
                "rss_bytes": 10,
                "uss_bytes": 5,
                "physical_footprint_bytes": 0,
            },
            {
                "timestamp": 100.8,
                "rss_bytes": 25,
                "uss_bytes": 15,
                "physical_footprint_bytes": 0,
            },
        ],
    }

    timeline = _aggregate_memory_timeline([profile])

    assert [sample["timestamp"] for sample in timeline] == [100.1, 100.8]
    assert max(sample["total_rss_bytes"] for sample in timeline) == 25


def test_memory_samples_are_compacted_without_losing_peaks():
    samples = []
    for value in (10, 100, 20, 30, 40):
        sample = (value, value, value, value, value, 0, value, value, value, value)
        _append_bounded_sample(samples, sample, limit=4)

    assert len(samples) <= 4
    assert max(sample[2] for sample in samples) == 100
    assert samples[-1][0] == 40

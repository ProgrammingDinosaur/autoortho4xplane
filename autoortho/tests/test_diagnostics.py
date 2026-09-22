import json
import subprocess
import sys
import time

import pytest

from autoortho.diagnostics import (
    PerformanceProfiler,
    StageAggregate,
    _append_bounded_sample,
    _aggregate_memory_timeline,
    export_imagery_comparison,
    finalize_session_report,
)
from autoortho import pydds
from autoortho.dds_manifest import (
    MipmapSource,
    RowBuildManifest,
    manifests_to_dict,
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
                "autoortho": {
                    "prefetch_lookahead": 0,
                    "prefetch_max_chunks": 256,
                    "prefetch_radius_nm": 50,
                },
            }
        },
    ).start()
    parent.record("fuse.dds_read", 1_700.0, tile_id="1_2_BI_16")
    report_path = parent.stop(
        stats_snapshot={
            "chunk_hit": 20,
            "chunk_miss": 4,
            "effective_target_zoom:17": 2,
            "mm0_served_exact_bytes": 900,
            "mm0_served_lower_zl_bytes": 100,
            "prefetch_live_exact_coverage_pct:90": 3,
            "prefetch_promotion_eta_seconds:60": 3,
            "mm0_rows_served_before_predictive_complete": 4,
            "mm0_rows_served_after_predictive_complete": 8,
        },
        finalize_session=True,
    )

    assert report_path == session_dir / "report.md"
    assert report_path.exists()
    markdown = report_path.read_text(encoding="utf-8")
    assert "mount-worker:test" in markdown
    assert "network.http_request" in markdown
    assert "HTTP request p95 exceeds 1 second" in markdown
    assert "Mipmap-zero delivery" in markdown
    assert "| Target-ZL provider response | 900 | 90.0% |" in markdown
    assert "Legacy prefetch settings combine unlimited lookahead" in markdown

    report = json.loads((session_dir / "report.json").read_text(encoding="utf-8"))
    assert report["process_count"] == 2
    assert report["stats_snapshot"]["chunk_hit"] == 20
    assert report["peak_total_rss_bytes"] > 0
    assert report["delivery"]["effective_target_zoom"] == [
        {"value": "17", "count": 2}
    ]
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


def test_checkpoint_preserves_worker_stages_before_graceful_stop(tmp_path):
    session_dir = tmp_path / "performance-checkpoint"
    worker = PerformanceProfiler(
        session_dir=session_dir,
        session_id="checkpoint-session",
        role="mount-worker:test",
        sample_interval=60.0,
        checkpoint_interval=10.0,
    ).start()
    try:
        worker.record("fuse.dds_read", 1_250.0, tile_id="1_2_BI_16")
        worker.set_gauge("chunk_queue.live_depth", 42)
        worker._last_checkpoint = 0.0
        worker._write_checkpoint_if_due()

        process_path = worker._process_path()
        checkpoint = json.loads(process_path.read_text(encoding="utf-8"))
        assert checkpoint["profile_status"] == "checkpoint"
        assert checkpoint["stages"]["fuse.dds_read"]["count"] == 1
        assert checkpoint["gauges"]["chunk_queue.live_depth"]["max"] == 42

        report_path = finalize_session_report(
            session_dir,
            "checkpoint-session",
        )
        report = json.loads(
            report_path.with_name("report.json").read_text(encoding="utf-8")
        )
        process = next(
            row for row in report["processes"]
            if row["role"] == "mount-worker:test"
        )
        assert process["profile_status"] == "checkpoint"
        assert any(
            row["stage"] == "fuse.dds_read"
            for row in report["stages"]
        )
        assert any(
            "recovered from periodic checkpoints" in flag
            for flag in report["diagnostic_flags"]
        )
    finally:
        worker.stop()


def test_report_prefers_checkpoint_over_observed_profile(tmp_path):
    session_dir = tmp_path / "performance-profile-precedence"
    session_dir.mkdir()
    common = {
        "schema_version": 1,
        "session_id": "precedence",
        "role": "mount-worker:test",
        "pid": 42,
        "started_wall_time": 1.0,
        "ended_wall_time": 2.0,
        "duration_seconds": 1.0,
        "host": {},
        "gauges": {},
        "slow_operations": [],
        "memory_samples": [],
        "python_allocations": [],
        "settings": {},
        "metadata": {},
    }
    observed = {
        **common,
        "profile_status": "resource-only",
        "stages": {},
    }
    checkpoint = {
        **common,
        "profile_status": "checkpoint",
        "stages": {
            "fuse.dds_read": StageAggregate(
                count=1,
                total_ms=10.0,
                min_ms=10.0,
                max_ms=10.0,
            ).to_dict(),
        },
    }
    (session_dir / "process-42-worker-observed.json").write_text(
        json.dumps(observed),
        encoding="utf-8",
    )
    (session_dir / "process-42-worker.json").write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )

    report_path = finalize_session_report(session_dir, "precedence")
    report = json.loads(
        report_path.with_name("report.json").read_text(encoding="utf-8")
    )

    assert report["process_count"] == 1
    assert report["processes"][0]["profile_status"] == "checkpoint"
    assert [row["stage"] for row in report["stages"]] == ["fuse.dds_read"]


def test_export_imagery_comparison_writes_local_bundle(
    tmp_path,
    monkeypatch,
):
    dds = pydds.DDS(256, 256, dxt_format="BC1")
    dds_bytes = dds.read_at(0, dds.total_size)
    mm0 = dds.mipmap_list[0]
    row_data = dds_bytes[mm0.startpos:mm0.endpos]
    manifest = RowBuildManifest.create(
        tile_id="0_0_BI_16",
        target_zoom=16,
        mipmap=0,
        row_index=0,
        build_generation=1,
        sources=[MipmapSource.TARGET_PROVIDER],
        compressed_data=row_data,
    )
    cache_dir = tmp_path / "cache"
    metadata_dir = cache_dir / "dds_cache" / "tiles"
    metadata_dir.mkdir(parents=True)
    ddm_path = metadata_dir / "tile.ddm"
    ddm_path.with_suffix(".dds").write_bytes(dds_bytes)
    ddm_path.write_text(
        json.dumps(
            {
                "v": 5,
                "tile_row": 0,
                "tile_col": 0,
                "zl": 16,
                "max_zl": 16,
                "map": "BI",
                "w": 256,
                "h": 256,
                "built": 1.0,
                "disk_compression": "none",
                "mm0_manifest": manifests_to_dict(
                    {0: manifest},
                    target_zoom=16,
                ),
            }
        ),
        encoding="utf-8",
    )

    def tiny_mosaic(_cache_root, output_path, **_kwargs):
        output_path.write_bytes(b"jpeg")
        return {"present": 0, "expected": 1}

    monkeypatch.setattr(
        "autoortho.diagnostics._write_jpeg_mosaic",
        tiny_mosaic,
    )

    output = export_imagery_comparison(ddm_path, tmp_path / "exports")

    assert (output / "target-zl-mosaic.jpg").is_file()
    assert (output / "lower-zl-parent-mosaic.jpg").is_file()
    assert (output / "generated-dds-mipmap-zero.ppm").is_file()
    assert (output / "row-manifests.json").is_file()
    assert (output / "ddm-metadata.json").is_file()
    assert (output / "timing-summary.json").is_file()
    assert (output / "checksums.json").is_file()

    unsafe = json.loads(ddm_path.read_text(encoding="utf-8"))
    unsafe["map"] = "../../escape"
    ddm_path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        export_imagery_comparison(ddm_path, tmp_path / "safe-root")

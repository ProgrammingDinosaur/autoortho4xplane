"""Parent diagnostics and OS report matching, with no system configuration edits."""

import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import posix_crash as pc


@pytest.mark.parametrize("code, expected", [(0, False), (-signal.SIGTERM, True), (143, True), (-getattr(signal, "SIGKILL", 9), True)])
def test_normal_and_requested_stops_are_not_crashes(tmp_path, code, expected):
    collector = pc.PosixCrashCollector(tmp_path, collect_system=False)
    Path(str(collector.prefix) + ".fault.log").write_text("header only")
    collector.start(123, None)
    collector.record_exit(code, expected=expected)
    collector.close()
    assert list(tmp_path.iterdir()) == []


def test_native_fault_during_shutdown_is_still_retained(tmp_path):
    collector = pc.PosixCrashCollector(tmp_path, collect_system=False)
    collector.start(123, None)
    collector.record_exit(-signal.SIGSEGV, expected=True)
    collector.close()
    summary = json.loads(Path(str(collector.prefix) + ".crash.json").read_text())
    assert summary["native_fault"] and summary["shutdown_requested"]


def test_os_report_matching_requires_pid_and_fresh_timestamp(tmp_path):
    stale = tmp_path / "stale.ips"
    stale.write_text('{"app_name":"AutoOrtho"}\n{"pid":123,"threads":[]}')
    os.utime(stale, (1, 1))
    unrelated = tmp_path / "other.ips"
    unrelated.write_text('{"pid":999}')
    assert pc._find_macos_report(123, time.time() - 2, [tmp_path]) is None
    report = tmp_path / "matching.ips"
    report.write_text('{"app_name":"AutoOrtho"}\n{\n"pid":123,"threads":[]}')
    suffix, data = pc._find_macos_report(123, time.time() - 2, [tmp_path])
    assert suffix == ".ips" and '"pid":123' in data


def test_classic_macos_crash_report_requires_exact_process_pid():
    assert pc._report_matches_pid("Process: AutoOrtho [123]\nPath: /app", ".crash", 123)
    assert not pc._report_matches_pid("Process: Other [1234]\n", ".crash", 123)


def test_missing_linux_core_collector_is_explicit(tmp_path, monkeypatch):
    collector = pc.PosixCrashCollector(tmp_path)
    collector.start(123, None)
    collector.record_exit(-signal.SIGSEGV)
    monkeypatch.setattr(pc.sys, "platform", "linux")
    original_is_file = pc.Path.is_file
    monkeypatch.setattr(pc.Path, "is_file", lambda path: False if path.name == "coredumpctl" else original_is_file(path))
    collector.close()
    collector.thread.join(timeout=5)
    status = json.loads(Path(str(collector.prefix) + ".system.json").read_text())
    assert status["status"] == "unavailable"
    assert "not installed" in status["reason"]
    assert Path(str(collector.prefix) + ".complete").exists()


def test_linux_report_query_is_scoped_to_worker_and_user(tmp_path, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX user IDs")
    collector = pc.PosixCrashCollector(tmp_path)
    collector.start(123, None)
    collector.record_exit(-signal.SIGSEGV)
    monkeypatch.setattr(pc.sys, "platform", "linux")
    original_is_file = pc.Path.is_file
    monkeypatch.setattr(pc.Path, "is_file", lambda path: True if path.name == "coredumpctl" else original_is_file(path))
    captured = {}
    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        kwargs["stdout"].write(b"Stack trace of thread 123: native frames\n")
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(pc.subprocess, "run", run)
    collector.close()
    collector.thread.join(timeout=5)
    assert "COREDUMP_PID=123" in captured["command"]
    assert "COREDUMP_UID=" + str(os.getuid()) in captured["command"]
    assert "COREDUMP_EXE=" + os.path.realpath(sys.executable) in captured["command"]
    assert "info" in captured["command"] and "dump" not in captured["command"]
    assert captured["timeout"] <= 3
    status = json.loads(Path(str(collector.prefix) + ".system.json").read_text())
    assert status["status"] == "collected"


def test_retention_does_not_remove_windows_or_active_reports(tmp_path):
    for index in range(5):
        for suffix in pc.REPORT_SUFFIXES:
            path = tmp_path / ("posix-" + str(index) + suffix)
            path.touch()
            os.utime(path, (index + 1, index + 1))
    active = tmp_path / "posix-active.fault.log"
    active.write_text("active")
    windows = tmp_path / "worker-123.complete"
    windows.touch()
    pc.prune_reports(tmp_path)
    assert len(list(tmp_path.glob("posix-*.complete"))) == 3
    assert active.exists() and windows.exists()


def test_posix_metadata_file_is_private(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode bits")
    path = tmp_path / "report.json"
    pc._write_json(path, {"pid": 123})
    assert path.stat().st_mode & 0o777 == 0o600

"""Performance plan, item 0: the replay benchmark's tools (tools/bench)."""

import json
import os
import socket
import struct
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools", "bench"))

import compare  # noqa: E402
import fake_xplane  # noqa: E402
import make_scenario  # noqa: E402
import replay  # noqa: E402
from sampler import ProcessTreeSampler  # noqa: E402

from autoortho import read_trace  # noqa: E402
from autoortho.datareftrack import DatarefTracker  # noqa: E402


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_fake_xplane_answers_rref_subscriptions():
    port = _free_udp_port()
    position = {"lat": 62.06, "lon": -7.27, "agl_m": 950.0, "hdg": 88.0, "gs": 120.0}
    fake = fake_xplane.FakeXPlane(port, lambda: position, rate_hz=20).start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(3.0)
    try:
        for index, entry in enumerate(DatarefTracker.datarefs):
            client.sendto(
                struct.pack("<5sii400s", b"RREF\x00", 1, index, entry[0].encode()),
                ("127.0.0.1", port),
            )
        data, _ = client.recvfrom(4096)
    finally:
        client.close()
        fake.stop()
    tracker = DatarefTracker.__new__(DatarefTracker)
    values = DatarefTracker._decode_packet(tracker, data)
    assert values[0] == pytest.approx(62.06, abs=1e-4)
    assert values[1] == pytest.approx(-7.27, abs=1e-4)
    assert values[4] == pytest.approx(120.0)


def test_read_trace_round_trip(tmp_path):
    path = str(tmp_path / "t.trace")
    trace = read_trace.ReadTrace(path)
    trace.event("open", "/textures/1_2_BI16.dds")
    trace.event("read", "/textures/1_2_BI16.dds", offset=0, length=65536, ms=1.5)
    trace.event("release", "/textures/1_2_BI16.dds")
    trace.close()
    events = read_trace.load(path)
    assert [e["op"] for e in events] == ["open", "read", "release"]
    assert events[1]["length"] == 65536


def test_trace_from_environment_is_off_by_default(monkeypatch):
    monkeypatch.delenv(read_trace.ENV, raising=False)
    assert read_trace.from_environment() is None


def test_replay_against_plain_files(tmp_path):
    textures = tmp_path / "textures"
    textures.mkdir()
    (textures / "1_2_BI16.dds").write_bytes(b"x" * 200_000)
    events = [
        {"t": 0.0, "op": "open", "path": "/textures/1_2_BI16.dds"},
        {"t": 0.01, "op": "read", "path": "/textures/1_2_BI16.dds", "offset": 0, "length": 65536},
        {"t": 0.02, "op": "read", "path": "/textures/1_2_BI16.dds", "offset": 65536, "length": 65536},
        {"t": 0.03, "op": "release", "path": "/textures/1_2_BI16.dds"},
    ]
    replayer = replay.Replayer(str(tmp_path), events, threads=2)
    replayer.run()
    summary = replayer.summary()
    assert summary["reads"] == 2 and summary["errors"] == 0
    assert summary["bytes_read"] == 131072
    assert summary["opens"] == 1


def test_sampler_measures_this_process():
    sampler = ProcessTreeSampler(os.getpid(), interval=0.05).start()
    sum(i * i for i in range(300_000))
    time.sleep(0.15)
    sampler.stop()
    summary = sampler.summary()
    assert summary["total_cpu_s"] > 0
    assert summary["processes"][0]["role"] == "parent"


def test_make_scenario_writes_opens_reads_and_positions(tmp_path):
    textures = tmp_path / "textures"
    textures.mkdir()
    # Two ZL16 textures near Vagar.
    for name in ("18240_31440_BI16.dds", "18240_31456_BI16.dds"):
        (textures / name).write_bytes(b"")
    out = tmp_path / "s.trace"
    assert make_scenario.main([
        "--mountpoint", str(tmp_path), "--lat", "62.06", "--lon", "-7.27",
        "--minutes", "1", "--load-s", "5", "--radius-km", "500", "--out", str(out),
    ]) == 0
    events = read_trace.load(str(out))
    ops = {e["op"] for e in events}
    assert {"open", "read", "pos"} <= ops
    first_pos = min(e["t"] for e in events if e["op"] == "pos")
    assert first_pos >= 5.0  # silent while loading


def test_compare_runs_with_gates(tmp_path, capsys):
    def run(label, cpu, teams):
        return {
            "label": label, "scenario": "cruise-cold", "wall_s": 100.0,
            "reads": {"opens": 10, "reads": 100, "read_ms_p50": 1, "read_ms_p95": 2, "read_ms_max": 3},
            "resources": {
                "total_cpu_s": cpu, "total_kernel_s": cpu / 2,
                "total_working_set_median": 1e9, "total_working_set_peak": 2e9,
                "process_count_peak": 4, "threads_total_median": 120,
                "processes": [
                    {"role": "mount-worker", "cpu_s": cpu * 0.8, "kernel_s": cpu * 0.4,
                     "working_set_median": 8e8, "read_bytes": 0, "write_bytes": 0},
                    {"role": "parent", "cpu_s": cpu * 0.1, "kernel_s": 0,
                     "working_set_median": 1e8, "read_bytes": 0, "write_bytes": 0},
                    {"role": "broker", "cpu_s": cpu * 0.1, "kernel_s": 0,
                     "working_set_median": 6e7, "read_bytes": 0, "write_bytes": 0},
                ],
            },
            "counters": {"native_teams_released": teams},
        }

    paths = []
    for label, cpu, teams in (("B0", 100.0, 100), ("new", 60.0, 0)):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(run(label, cpu, teams)), encoding="utf-8")
        paths.append(str(path))
    compare.main(paths)
    out = capsys.readouterr().out
    assert "close the item" in out  # 100 x 0.65 ms of 80 s
    assert "total CPU down 40.0 %" in out

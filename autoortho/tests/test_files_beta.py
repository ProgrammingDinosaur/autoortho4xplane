"""Files mode beta: Phase 0 telemetry, ring fit, download priority, Windows
integration, plugin digest, and the flight report section."""

import importlib.util
import json
import os
import random
import sys

import pytest

import files_mode_helpers as h
from files_mode import geo, ring_fit, rings, telemetry, windows_integration

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ----------------------------------------------------------- telemetry

def test_telemetry_keeps_first_read_per_level(tmp_path):
    rec = telemetry.ReadTelemetry(directory=str(tmp_path), clock=lambda: 1.0)
    assert rec.note(1, 2, "BI", 16, 4, 4096, 50000, 300, 7100, 2704, 65, 1)
    assert not rec.note(1, 2, "BI", 16, 4, 4096, 40000, 300, 7100, 2704, 65, 1)
    assert rec.note(1, 2, "BI", 16, 2, 4096, 30000, 300, 7100, 2704, 65, 2)
    rec.flush()
    lines = [json.loads(line) for line in open(rec.path)]
    assert [line["mip"] for line in lines] == [4, 2]
    assert lines[0]["first"] and not lines[1]["first"]
    assert lines[1]["opens"] == 2


def test_telemetry_cap_and_off(tmp_path):
    rec = telemetry.ReadTelemetry(directory=str(tmp_path), max_samples=2)
    for i in range(5):
        rec.note(i, 0, "BI", 16, 0, 4096, 1000, 0, 7100)
    assert rec.count == 2
    off = telemetry.ReadTelemetry(directory=str(tmp_path), enabled=False)
    assert not off.note(1, 1, "BI", 16, 0, 4096, 1, 0, 1)


# -------------------------------------------------------------- ring fit

def _synthetic(true_rings, n=3000, seed=1):
    """First reads where X-Plane takes each level near its (zoom-scaled) ring."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        zoom = rnd.choice([16, 18])
        level = rnd.choice([0, 1, 2, 3])
        outer = true_rings[level] * 2.0 ** (16 - zoom)
        inner = true_rings.get(level - 1, 0) * 2.0 ** (16 - zoom) if level else 0
        d = outer * rnd.uniform(0.9, 1.0) if rnd.random() < 0.7 else rnd.uniform(inner, outer)
        out.append({"row": i, "col": i, "mt": "BI", "z": zoom, "mip": level, "px": 4096,
                    "ground_m": d * 1000, "agl_m": 300, "size_m": rnd.uniform(4000, 9000),
                    "width": 2704, "fov": 65, "tex_res": 5, "opens": level + 1,
                    "first": False})
        out.append({"row": i, "col": i, "mt": "BI", "z": zoom, "mip": 4, "px": 4096,
                    "ground_m": 200000, "agl_m": 300, "size_m": 7100, "width": 2704,
                    "fov": 65, "tex_res": 5, "opens": 1, "first": True})
    return out


def test_ring_fit_recovers_rings_and_scaling():
    truth = {0: 12.0, 1: 22.0, 2: 40.0, 3: 75.0}
    rings_json, report = ring_fit.fit(_synthetic(truth))
    fitted = {int(k): v for k, v in rings_json["rings_zl16_km"].items()}
    for level, value in truth.items():
        assert fitted[level] == pytest.approx(value, rel=0.05)
    assert rings_json["size_scaling"] is False
    assert report["A2_first_read_levels"] == {4: 3000}
    model = rings.RingModel(rings=fitted, margin=1.0, size_scaling=False)
    assert model.level_needed(11, 16) == 0


def test_ring_fit_cli(tmp_path):
    path = tmp_path / "reads-1.jsonl"
    path.write_text("\n".join(json.dumps(s) for s in _synthetic(
        {0: 13.6, 1: 23.7, 2: 42.9, 3: 81.4}, n=800)))
    spec = importlib.util.spec_from_file_location(
        "fit_rings", os.path.join(ROOT, "tools", "files_mode", "fit_rings.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "rings.json"
    assert mod.main([str(path), "-o", str(out), "--report", str(tmp_path / "r.json")]) == 0
    loaded = rings.RingModel.load(str(out))
    assert loaded.rings[0] == pytest.approx(13.6, rel=0.05)


# ------------------------------------------------------ download priority

class _Chunk:
    def __init__(self, priority=16):
        self.priority = priority
        self.prefetch = False
        self.in_queue = False
        self.in_flight = False


def test_files_priority_hint_maps_urgency():
    import getortho
    assert getortho.files_priority_for(0) == getortho.PRIORITY_LIVE
    assert getortho.files_priority_for(30) == getortho.PRIORITY_IMMINENT
    assert getortho.files_priority_for(300) == getortho.PRIORITY_PREFETCH
    c = _Chunk()
    getortho._apply_files_priority(c)     # no hint (FUSE mode): untouched
    assert c.priority == 16 and not c.prefetch
    with getortho.files_priority(getortho.PRIORITY_PREFETCH):
        getortho._apply_files_priority(c)
    assert c.priority == getortho.PRIORITY_PREFETCH and c.prefetch
    with getortho.files_priority(getortho.PRIORITY_LIVE):
        getortho._apply_files_priority(c)
        queued = _Chunk(200)
        queued.in_queue = True
        getortho._apply_files_priority(queued)
    assert c.priority == getortho.PRIORITY_LIVE and not c.prefetch
    assert queued.priority == 200          # already queued: left alone
    assert getattr(getortho._files_priority_local, "value", None) is None


def test_session_build_passes_priority(tmp_path):
    from files_mode.session import FilesConfig, FilesSession
    from files_mode.planner import PlannedDemand
    root = h.make_region(str(tmp_path / "s"), "z_ao_eur",
                         h.textures_around(47.4, 8.5, radius=0))
    (tmp_path / "cs").mkdir()
    svc = h.FakeService()
    cfg = FilesConfig(files_root=str(tmp_path / "f"), custom_scenery=str(tmp_path / "cs"),
                      regions={"z_ao_eur": root}, gate_port=0, install_plugin=False)
    s = FilesSession(cfg, h.FakeFlight(47.4, 8.5), service=svc,
                     size_of=lambda c, i: (1024, "BC1"), xplane_running=lambda: False)
    s.setup()
    row, col, mt, z = h.textures_around(47.4, 8.5, radius=0)[0]
    for urgency in (0.0, 300.0):
        s._build(PlannedDemand("z_ao_eur", 0, geo.texture_name(row, col, mt, z), row, col,
                               mt, z, 3, urgency, 1.0, 99, 1024))
    import getortho
    assert [p.priority for p in svc.policies] == [getortho.PRIORITY_LIVE,
                                                   getortho.PRIORITY_PREFETCH]
    s.index.close()


# ---------------------------------------------------- Windows integration

def test_defender_command_quotes_path():
    cmd = windows_integration.defender_command("D:\\It's here\\files")
    assert "It''s here" in cmd and cmd.startswith("Add-MpPreference -ExclusionPath '")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Search")
def test_search_scope_query_is_read_only(tmp_path):
    # Read-only: asks whether a folder is indexed; changes nothing.
    assert windows_integration.search_indexes(str(tmp_path)) in (True, False, None)


def test_exclude_skips_when_not_indexed(monkeypatch):
    calls = []

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def included(self, path):
            return False

        def exclude(self, path):
            calls.append(path)

    monkeypatch.setattr(windows_integration, "_CrawlScope", Scope)
    monkeypatch.setattr(windows_integration.sys, "platform", "win32")
    assert windows_integration.exclude_from_search("X:\\files")
    assert calls == []


# ------------------------------------------------------------ plugin digest

def test_plugin_binaries_match_source():
    """ao_gate.c changed without rebuilding: run build_win.bat / make (stamp.py)."""
    plugin = os.path.join(ROOT, "autoortho", "xplane_plugin")
    spec = importlib.util.spec_from_file_location("stamp", os.path.join(plugin, "stamp.py"))
    stamp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stamp)
    recorded = open(os.path.join(plugin, "bin", "SOURCE_SHA256")).read().strip()
    assert recorded == stamp.source_digest()
    assert os.path.isfile(os.path.join(plugin, "bin", "win_x64", "AutoOrtho.xpl"))


# ---------------------------------------------------------------- report

def test_report_has_files_mode_section():
    import diagnostics
    lines = diagnostics._render_files_mode({"files_builds": 1200, "files_late_total": 3,
                                            "files_start_s": 41.5,
                                            "files_levers": {"files.builders": 3}})
    text = "\n".join(lines)
    assert "## Files mode" in text and "1,200" in text and "41.5" in text
    assert "files.builders" in text
    assert diagnostics._render_files_mode({}) == []

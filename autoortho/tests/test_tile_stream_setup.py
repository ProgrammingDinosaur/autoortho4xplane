#!/usr/bin/env python3
"""Tests for streaming package setup, prefetching, disk limits and overlays."""

import gzip
import io
import json
import os
import sys
import threading
import time
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
TESTS_DIR = os.path.dirname(__file__)
for directory in (ROOT, AUTOORTHO_DIR, os.path.join(ROOT, "scripts"), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import tile_stream as ts  # noqa: E402
from test_tile_stream import (  # noqa: E402
    FakeHost, FakeResponse, build_release_zip, index_for, make_dsf,
)


def test_dsf_rel_uses_ten_degree_folders():
    assert ts.dsf_rel(45, -75) == "Earth nav data/+40-080/+45-075.dsf"
    assert ts.dsf_rel(-12, -1) == "Earth nav data/-20-010/-12-001.dsf"
    assert ts.dsf_rel(0, 0) == "Earth nav data/+00+000/+00+000.dsf"


def test_empty_overlay_dsf_is_a_valid_overlay():
    data = ts.empty_overlay_dsf(45, -75)
    assert data.startswith(b"XPLNEDSF")
    assert ts.dsf_terrain_names(data) == []
    assert b"sim/overlay\x001\x00" in data
    import hashlib
    assert hashlib.md5(data[:-16]).digest() == data[-16:]


# ---------------------------------------------------------------------------
# A release with a 3x3 block of tiles
# ---------------------------------------------------------------------------

URL = "https://github.com/o/r/releases/download/t/z_grid_00.zip"


@pytest.fixture
def grid_release():
    members = []
    for lat in (39, 40, 41):
        for lon in (-10, -9, -8):
            rel = ts.dsf_rel(lat, lon)
            members.append((f"z_grid_00/{rel}", make_dsf([]) + os.urandom(2000),
                            zipfile.ZIP_DEFLATED))
    blob = build_release_zip(members)
    return {"blob": blob, "index": index_for({URL: blob})}


class SlowHost(FakeHost):
    """Delays ranges for chosen byte offsets so queue order can be observed."""

    def __init__(self, files, slow_offsets=(), delay=0.5):
        super().__init__(files)
        self.slow_offsets = set(slow_offsets)
        self.delay = delay

    def get(self, url, headers=None, allow_redirects=True, timeout=None):
        if headers and "Range" in headers and url.startswith("https://storage"):
            start = int(headers["Range"].split("=")[1].split("-")[0])
            if start in self.slow_offsets:
                time.sleep(self.delay)
        return super().get(url, headers, allow_redirects, timeout)


def make_pkg(tmp_path, release, host, **marker):
    root = tmp_path / "z_ao_stream_xp11"
    root.mkdir(exist_ok=True)
    with gzip.open(root / ts.INDEX_NAME, "wt") as fh:
        json.dump(release["index"], fh)
    marker = dict({"index": ts.INDEX_NAME}, **marker)
    ts.write_marker(str(root), marker)
    return ts.StreamPackage(str(root), ts.read_marker(str(root)),
                            remote=ts.RemoteAssets(session=host)), root


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_neighbors_are_prefetched_in_background(tmp_path, grid_release):
    host = FakeHost({URL: grid_release["blob"]})
    pkg, root = make_pkg(tmp_path, grid_release, host)
    try:
        center = ts.dsf_rel(40, -9)
        assert pkg.ensure_local(center, timeout=5)
        assert pkg.prefetch_neighbors(center) == 8
        neighbours = [ts.dsf_rel(lat, lon) for lat in (39, 40, 41)
                      for lon in (-10, -9, -8) if (lat, lon) != (40, -9)]
        assert wait_for(lambda: all(os.path.exists(pkg.local_path(r)) for r in neighbours))
        assert pkg.stats["prefetched"] == 8
        assert pkg.prefetch_neighbors(center) == 0  # all local now
    finally:
        pkg.shutdown()


def test_foreground_open_does_not_wait_behind_prefetches(tmp_path, grid_release):
    files = grid_release["index"]["files"]
    slow = {e[1] for rel, e in files.items() if rel != ts.dsf_rel(41, -8)}
    host = SlowHost({URL: grid_release["blob"]}, slow_offsets=slow, delay=1.0)
    pkg, _ = make_pkg(tmp_path, grid_release, host, prefetch_workers=1)
    try:
        for rel in files:
            if rel != ts.dsf_rel(41, -8):
                pkg.prefetch(rel)  # 8 slow prefetches on one worker
        started = time.monotonic()
        assert pkg.ensure_local(ts.dsf_rel(41, -8), timeout=5)
        assert time.monotonic() - started < 0.8
    finally:
        pkg.shutdown()


def test_foreground_request_for_queued_prefetch_is_served_now(tmp_path, grid_release):
    files = grid_release["index"]["files"]
    target = ts.dsf_rel(39, -10)
    blocker = ts.dsf_rel(41, -8)
    host = SlowHost({URL: grid_release["blob"]}, slow_offsets={files[blocker][1]}, delay=1.5)
    pkg, _ = make_pkg(tmp_path, grid_release, host, prefetch_workers=1)
    try:
        pkg.prefetch(blocker)   # occupies the only prefetch worker
        pkg.prefetch(target)    # queued behind it
        started = time.monotonic()
        assert pkg.ensure_local(target, timeout=5)
        assert time.monotonic() - started < 1.0
    finally:
        pkg.shutdown()


def test_eviction_keeps_recent_files_and_removes_oldest(tmp_path, grid_release):
    host = FakeHost({URL: grid_release["blob"]})
    pkg, root = make_pkg(tmp_path, grid_release, host, disk_limit_gb=0.000005)  # 5 kB
    try:
        rels = sorted(grid_release["index"]["files"])
        for rel in rels:
            assert pkg.ensure_local(rel, timeout=5)
        now = time.time()
        old = now - 3 * 3600
        for i, rel in enumerate(rels[:6]):
            os.utime(pkg.local_path(rel), (old + i, old + i))
        removed = pkg.evict()
        assert removed > 0
        # Oldest go first; files used in the last two hours stay.
        assert not os.path.exists(pkg.local_path(rels[0]))
        for rel in rels[6:]:
            assert os.path.exists(pkg.local_path(rel))
    finally:
        pkg.shutdown()


def test_usage_counts_dsfs_and_masks(tmp_path, grid_release):
    host = FakeHost({URL: grid_release["blob"]})
    pkg, root = make_pkg(tmp_path, grid_release, host)
    try:
        rel = ts.dsf_rel(40, -9)
        pkg.ensure_local(rel, timeout=5)
        (root / "textures" / "1_2_ZL16.png").write_bytes(b"x" * 100)
        total, count = pkg.usage()
        assert count == 1
        assert total == os.path.getsize(pkg.local_path(rel)) + 100
    finally:
        pkg.shutdown()


# ---------------------------------------------------------------------------
# Enabling, disabling, index refresh
# ---------------------------------------------------------------------------

class IndexHost:
    def __init__(self, payloads, last_modified="Wed, 18 Nov 2026 00:00:00 GMT"):
        self.payloads = payloads
        self.last_modified = last_modified
        self.requests = []

    def get(self, url, timeout=None, headers=None, **_):
        self.requests.append((url, dict(headers or {})))
        if headers and headers.get("If-Modified-Since") == self.last_modified:
            return FakeResponse(304)
        if url not in self.payloads:
            return FakeResponse(404)
        return FakeResponse(200, self.payloads[url], {"Last-Modified": self.last_modified})


def gz_index(release):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
        fh.write(json.dumps(release["index"]).encode())
    return buf.getvalue()


def test_enable_and_disable_streaming(tmp_path, grid_release):
    spec = ts.SOURCES["xp11"]
    host = IndexHost({spec["ortho_index"]: gz_index(grid_release),
                      spec["overlay_index"]: gz_index(grid_release)})
    scenery = tmp_path / "scenery"
    (scenery / "z_ao_eur").mkdir(parents=True)  # a downloaded package
    created = ts.enable_streaming(str(scenery), "xp11", overlays=True,
                                  disk_limit_gb=40, session=host)
    assert created == ["z_ao_stream_xp11", "yAO_stream_xp11_overlays"]
    packages = ts.stream_packages(str(scenery))
    assert packages["z_ao_stream_xp11"]["disk_limit_gb"] == 40
    assert packages["yAO_stream_xp11_overlays"]["kind"] == "overlay"
    assert packages["yAO_stream_xp11_overlays"]["disk_limit_gb"] == 10
    ts.update_stream_settings(str(scenery), 8)
    assert ts.stream_packages(str(scenery))["yAO_stream_xp11_overlays"]["disk_limit_gb"] == 2
    # Turning overlays off removes only that package.
    ts.enable_streaming(str(scenery), "xp11", overlays=False, session=host)
    assert list(ts.stream_packages(str(scenery))) == ["z_ao_stream_xp11"]
    assert ts.disable_streaming(str(scenery)) == ["z_ao_stream_xp11"]
    assert (scenery / "z_ao_eur").is_dir()  # never touched
    with pytest.raises(ValueError):
        ts.enable_streaming(str(scenery), "no-such-set", session=host)


def test_overlays_can_be_added_and_removed_while_streaming(tmp_path, grid_release):
    spec = ts.SOURCES["xp11"]
    host = IndexHost({spec["ortho_index"]: gz_index(grid_release),
                      spec["overlay_index"]: gz_index(grid_release)})
    scenery = str(tmp_path)
    with pytest.raises(ValueError):  # streaming is off
        ts.set_stream_overlays(scenery, True, session=host)
    ts.enable_streaming(scenery, "xp11", overlays=False, disk_limit_gb=12, session=host)
    assert ts.set_stream_overlays(scenery, True, session=host) == "yAO_stream_xp11_overlays"
    overlay = ts.stream_packages(scenery)["yAO_stream_xp11_overlays"]
    assert overlay["kind"] == "overlay" and overlay["disk_limit_gb"] == 3
    stored = tmp_path / "yAO_stream_xp11_overlays" / "Earth nav data" / "+40-010"
    stored.mkdir(parents=True)
    (stored / "+40-009.dsf").write_bytes(b"x" * 50)
    summary = ts.stream_summary(scenery)
    assert summary["enabled"] and summary["overlays"] and summary["source"] == "xp11"
    assert summary["overlay"] == (50, 1) and summary["ortho"] == (0, 0)
    assert summary["disk_limit_gb"] == 12
    assert ts.set_stream_overlays(scenery, False) == "yAO_stream_xp11_overlays"
    assert list(ts.stream_packages(scenery)) == ["z_ao_stream_xp11"]
    assert ts.set_stream_overlays(scenery, False) is None


def test_change_source_keeps_settings_and_old_tiles_until_ready(tmp_path, grid_release,
                                                               monkeypatch):
    spec = ts.SOURCES["xp11"]
    other = {"label": "Other set", "available": True,
             "ortho_index": "https://example.invalid/other.json.gz", "overlay_index": None}
    monkeypatch.setitem(ts.SOURCES, "other", other)
    host = IndexHost({spec["ortho_index"]: gz_index(grid_release),
                      spec["overlay_index"]: gz_index(grid_release)})
    scenery = str(tmp_path)
    ts.enable_streaming(scenery, "xp11", overlays=True, disk_limit_gb=30,
                        roughness=0.8, session=host)
    # The new set's index can't be downloaded: streaming stays as it was.
    with pytest.raises(IOError):
        ts.change_stream_source(scenery, "other", session=host)
    assert set(ts.stream_packages(scenery)) == {"z_ao_stream_xp11", "yAO_stream_xp11_overlays"}
    host.payloads[other["ortho_index"]] = gz_index(grid_release)
    assert ts.change_stream_source(scenery, "other", session=host) == ["z_ao_stream_other"]
    packages = ts.stream_packages(scenery)
    assert list(packages) == ["z_ao_stream_other"]  # it has no overlays
    assert packages["z_ao_stream_other"]["disk_limit_gb"] == 30
    assert packages["z_ao_stream_other"]["roughness"] == 0.8
    assert ts.change_stream_source(scenery, "other", session=host) == []


def test_terrain_reflectivity_of_streamed_tiles(tmp_path, grid_release):
    spec = ts.SOURCES["xp11"]
    host = IndexHost({spec["ortho_index"]: gz_index(grid_release)})
    scenery = str(tmp_path)
    with pytest.raises(ValueError):  # streaming is off
        ts.set_stream_roughness(scenery, 0.5)
    ts.enable_streaming(scenery, "xp11", overlays=False, session=host)
    assert ts.stream_summary(scenery)["roughness"] is None
    with pytest.raises(ValueError):
        ts.set_stream_roughness(scenery, 1.5)

    assert ts.set_stream_roughness(scenery, 0.7) == "z_ao_stream_xp11"
    root = tmp_path / "z_ao_stream_xp11"
    marker = ts.read_marker(str(root))
    assert marker["roughness"] == 0.7 and ts.stream_summary(scenery)["roughness"] == 0.7
    # Generated .ter files carry it; the package reads it when mounted.
    package = ts.StreamPackage(str(root), marker, remote=host)
    try:
        ter = package.ter_bytes("25000_31296_BI16.ter").decode()
        assert ter.endswith("SUPER_ROUGHNESS 0.7\n")
    finally:
        package.shutdown()

    ts.set_stream_roughness(scenery, None)
    assert "roughness" not in ts.read_marker(str(root))
    package = ts.StreamPackage(str(root), ts.read_marker(str(root)), remote=host)
    try:
        assert "SUPER_ROUGHNESS" not in package.ter_bytes("25000_31296_BI16.ter").decode()
    finally:
        package.shutdown()


def test_stream_summary_when_off(tmp_path):
    summary = ts.stream_summary(str(tmp_path / "missing"))
    assert summary["enabled"] is False and summary["ortho"] == (0, 0)


def test_enable_streaming_rejects_bad_index(tmp_path):
    spec = ts.SOURCES["xp11"]
    host = IndexHost({spec["ortho_index"]: gzip.compress(b'{"format": 99}')})
    with pytest.raises(ValueError):
        ts.enable_streaming(str(tmp_path), "xp11", overlays=False, session=host)
    assert not (tmp_path / "z_ao_stream_xp11").exists()


def test_refresh_index_uses_if_modified_since(tmp_path, grid_release):
    spec = ts.SOURCES["xp11"]
    host = IndexHost({spec["ortho_index"]: gz_index(grid_release)})
    ts.enable_streaming(str(tmp_path), "xp11", overlays=False, session=host)
    root = tmp_path / "z_ao_stream_xp11"
    marker = ts.read_marker(str(root))
    assert ts.refresh_index(str(root), marker, session=host) is False  # fresh
    old = time.time() - 30 * 86400
    os.utime(root / ts.INDEX_NAME, (old, old))
    assert ts.refresh_index(str(root), marker, session=host) is False  # 304
    host.last_modified = "Thu, 19 Nov 2026 00:00:00 GMT"
    os.utime(root / ts.INDEX_NAME, (old, old))
    assert ts.refresh_index(str(root), marker, session=host) is True
    assert ts.read_marker(str(root))["index_last_modified"] == host.last_modified


def test_overlay_hidden_dsfs_reads_classic_overlay_folder(tmp_path):
    d = tmp_path / "yAutoOrtho_Overlays" / "Earth nav data" / "+40-080"
    d.mkdir(parents=True)
    (d / "+45-075.dsf").write_bytes(b"x")
    assert ts.overlay_hidden_dsfs(str(tmp_path)) == {"Earth nav data/+40-080/+45-075.dsf"}


# ---------------------------------------------------------------------------
# scenery_packs.ini
# ---------------------------------------------------------------------------

INI_HEAD = ["I", "1000 Version", "SCENERY", ""]


def write_ini(tmp_path, packs):
    lines = INI_HEAD + [f"SCENERY_PACK Custom Scenery/{p}/" for p in packs]
    (tmp_path / "scenery_packs.ini").write_text("\n".join(lines) + "\n")


def read_packs(tmp_path):
    return [line.split("Custom Scenery/")[1].rstrip("/")
            for line in (tmp_path / "scenery_packs.ini").read_text().splitlines()
            if "Custom Scenery/" in line]


def test_pack_order_inserts_missing_entries(tmp_path):
    write_ini(tmp_path, ["KSEA Demo", "z_ao_na"])
    assert ts.ensure_pack_order(str(tmp_path), "z_ao_stream_xp11", "yAO_stream_xp11_overlays")
    assert read_packs(tmp_path) == ["KSEA Demo", "yAO_stream_xp11_overlays", "z_ao_na",
                                    "z_ao_stream_xp11"]
    assert (tmp_path / "scenery_packs.ini.ao_backup").exists()
    assert not ts.ensure_pack_order(str(tmp_path), "z_ao_stream_xp11", "yAO_stream_xp11_overlays")


def test_pack_order_moves_overlay_above_orthos(tmp_path):
    write_ini(tmp_path, ["KSEA Demo", "z_ao_stream_xp11", "yAO_stream_xp11_overlays"])
    assert ts.ensure_pack_order(str(tmp_path), "z_ao_stream_xp11", "yAO_stream_xp11_overlays")
    assert read_packs(tmp_path) == ["KSEA Demo", "yAO_stream_xp11_overlays", "z_ao_stream_xp11"]


def test_pack_order_sits_next_to_classic_overlays(tmp_path):
    write_ini(tmp_path, ["simHeaven", "yAutoOrtho_Overlays", "z_ao_eur", "z_ao_stream_xp11"])
    ts.ensure_pack_order(str(tmp_path), "z_ao_stream_xp11", "yAO_stream_xp11_overlays")
    assert read_packs(tmp_path) == ["simHeaven", "yAutoOrtho_Overlays",
                                    "yAO_stream_xp11_overlays", "z_ao_eur", "z_ao_stream_xp11"]


def test_pack_order_without_ini_does_nothing(tmp_path):
    assert ts.ensure_pack_order(str(tmp_path), "z_ao_stream_xp11", None) is False


# ---------------------------------------------------------------------------
# UI panel
# ---------------------------------------------------------------------------

def test_stream_panel_states(qt_app):
    from ui.widgets.stream_panel import StreamPanel
    panel = StreamPanel()
    from ui.widgets.stream_panel import NO_OVERLAY_HELP, OVERLAY_HELP
    events = []
    for name in ("enable_requested", "disable_requested", "source_change_requested",
                 "overlays_change_requested", "limit_changed",
                 "roughness_change_requested", "refresh_requested"):
        getattr(panel, name).connect(lambda *a, n=name: events.append((n,) + a))

    # XP12 is the default; it has no overlays, so that option is off.
    assert panel.source_combo.currentData() == "xp12"
    assert all(panel.source_combo.model().item(i).isEnabled()
               for i in range(panel.source_combo.count()))
    assert not panel.overlays_check.isEnabled()
    assert panel.overlays_help.text() == NO_OVERLAY_HELP

    # Off: settings can be picked before turning streaming on, without requests.
    xp11 = panel.source_combo.findData("xp11")
    panel.source_combo.setCurrentIndex(xp11)
    panel.source_combo.activated.emit(xp11)  # what a user's pick sends
    assert panel.overlays_check.isEnabled()
    assert panel.overlays_help.text() == OVERLAY_HELP
    panel.overlays_check.click()
    assert panel.overlays_check.isEnabled() and events == []
    assert panel.usage_bar.isHidden() and panel.status_label.isHidden()
    panel.enable_check.click()
    assert events == [("enable_requested", "xp11", True, 20)]

    # On: each change is requested right away.
    events.clear()
    panel.set_state({"enabled": True, "source": "xp11", "overlays": False,
                     "disk_limit_gb": 30, "ortho": (3_000_000_000, 120),
                     "overlay": (0, 0)})
    assert panel.enabled and panel.enable_check.isChecked()
    assert panel.overlays_check.isEnabled() and not panel.overlays_check.isChecked()
    assert panel.limit_spin.value() == 30
    assert "120 tiles" in panel.usage_label.text() and not panel.usage_bar.isHidden()
    assert "next time AutoOrtho starts" in panel.status_label.text()
    panel.overlays_check.click()
    assert events == [("overlays_change_requested", True)]
    panel.restore()  # e.g. the user cancelled
    assert not panel.overlays_check.isChecked()
    panel.limit_spin.setValue(45)
    panel.limit_spin.editingFinished.emit()
    assert events[-1] == ("limit_changed", 45)

    # Terrain reflectivity: unset by default, applied right away while on.
    assert panel.roughness is None and not panel.roughness_spin.isEnabled()
    panel.roughness_check.click()
    assert events[-1] == ("roughness_change_requested", 1.0)
    assert panel.roughness_spin.isEnabled()
    panel.roughness_spin.setValue(0.6)
    panel.roughness_spin.editingFinished.emit()
    assert events[-1] == ("roughness_change_requested", 0.6)
    panel.set_state(dict(panel.state, roughness=0.6))
    count = len(events)
    panel.roughness_spin.editingFinished.emit()  # nothing changed
    assert len(events) == count
    panel.roughness_check.click()
    assert events[-1] == ("roughness_change_requested", None)
    panel.set_state(dict(panel.state, roughness=None))
    assert not panel.roughness_check.isChecked()

    # Busy and running lock everything; unlocking asks for fresh usage.
    panel.set_busy(True, "Downloading the overlay index…")
    assert not panel.enable_check.isEnabled() and not panel.overlays_check.isEnabled()
    assert panel.status_label.text() == "Downloading the overlay index…"
    panel.restore()
    panel.set_runtime_locked(True)
    assert not panel.enable_check.isEnabled() and not panel.source_combo.isEnabled()
    assert "locked while AutoOrtho" in panel.status_label.text()
    panel.set_runtime_locked(False)
    assert events[-1] == ("refresh_requested",)
    panel.enable_check.click()
    assert events[-1] == ("disable_requested",)
    panel.set_state({"enabled": False})
    assert not panel.enabled and not panel.enable_check.isChecked()
    assert panel.usage_bar.isHidden()

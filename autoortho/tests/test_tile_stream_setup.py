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
        ts.enable_streaming(str(scenery), "xp12", session=host)


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
    xp12 = panel.source_combo.findData("xp12")
    assert not panel.source_combo.model().item(xp12).isEnabled()
    assert "Soon" in panel.source_combo.itemText(xp12)
    requests = []
    panel.enable_requested.connect(lambda *a: requests.append(a))
    panel.toggle_button.click()
    assert requests == [("xp11", True, 20)]
    panel.set_state({"z_ao_stream_xp11": {"source": "xp11", "kind": "ortho",
                                          "disk_limit_gb": 30}}, 3_000_000_000, 120)
    assert panel.enabled and panel.toggle_button.text() == "Turn off streaming"
    assert not panel.overlays_check.isChecked()
    assert panel.limit_spin.value() == 30
    assert "120 tiles" in panel.status_label.text()
    panel.set_runtime_locked(True)
    assert not panel.toggle_button.isEnabled()
    panel.set_runtime_locked(False)
    off = []
    panel.disable_requested.connect(lambda: off.append(True))
    panel.toggle_button.click()
    assert off == [True]
    panel.set_state({})
    assert not panel.enabled and panel.status_label.text() == "Streaming is off."

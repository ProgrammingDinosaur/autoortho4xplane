"""Tests for the per-tile cache inventory (scan, delete, pin, keep offline)."""

import gzip
import json
import math
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

import cache_inventory as ci  # noqa: E402
import tile_stream as ts  # noqa: E402
from test_tile_stream import (  # noqa: E402
    FakeHost, LAND_TER, build_release_zip, index_for, make_dsf)

OLD = time.time() - 30 * 86400


def chunk_name(lat, lon, zoom, maptype="BI"):
    n = 1 << zoom
    col = int((lon + 180.0) / 360.0 * n)
    row = int(ci.lat_row(lat, zoom))
    return f"{col}_{row}_{zoom}_{maptype}.jpg"


def write(path, size, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def make_stream_package(ao_dir, name, kind="ortho", limit=20.0):
    root = os.path.join(ao_dir, name)
    os.makedirs(root, exist_ok=True)
    ts.write_marker(root, {"source": "xp11", "kind": kind,
                           "index": ts.INDEX_NAME, "disk_limit_gb": limit})
    return root


def dsf_path(root, lat, lon):
    return os.path.join(root, *ts.dsf_rel(lat, lon).split("/"))


# Mask 100000_148176_ZL18 is centred at 39.19N 23.50E.
MASK = "100000_148176_ZL18.png"


@pytest.fixture
def stores(tmp_path):
    ao_dir = str(tmp_path / "scenery")
    cache = str(tmp_path / "cache")
    ortho = make_stream_package(ao_dir, "z_ao_stream_xp11")
    overlay = make_stream_package(ao_dir, "yAO_stream_xp11_overlays", kind="overlay")
    write(dsf_path(ortho, 39, 23), 1000, OLD)
    write(os.path.join(ortho, "textures", MASK), 50, OLD)
    write(dsf_path(overlay, 39, 23), 300, OLD)
    write(dsf_path(ortho, 47, -123), 2000)
    write(os.path.join(ortho, "textures", "water_transition.png"), 7)  # not a mask
    ts.write_pins(ortho, {(47, -123)})
    # Imagery: two zooms in 47,-123, one chunk in 39,23, junk that must stay.
    write(os.path.join(cache, chunk_name(47.5, -122.5, 16)), 100)
    write(os.path.join(cache, chunk_name(47.6, -122.4, 17)), 40)
    write(os.path.join(cache, chunk_name(39.5, 23.5, 16, "EOX")), 10)
    write(os.path.join(cache, "notes.jpg"), 5)
    write(os.path.join(cache, "terrain_index", "abc.json"), 5)
    dds = os.path.join(cache, ci.DDS_DIR, "+40-130", "+47-123", "BI")
    write(os.path.join(dds, "100_200_z18.dds"), 500)
    write(os.path.join(dds, "100_200_z18.ddm"), 20)
    return ao_dir, cache, ortho, overlay


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

def test_file_names_map_to_tiles():
    assert ts.dsf_cell("+39-009.dsf") == (39, -9)
    assert ts.dsf_cell("junk.dsf") is None
    assert ts.mask_cell(MASK) == (39, 23)
    assert ts.mask_cell("water_transition.png") is None
    assert ci.chunk_cell(*map(int, chunk_name(47.5, -122.5, 16).split("_")[:3])) == (47, -123)
    assert ci.chunk_cell(*map(int, chunk_name(-33.9, 151.2, 17).split("_")[:3])) == (-34, 151)


def test_chunks_in_cell_follows_mercator():
    equator = ci.chunks_in_cell((0, 10), 16)
    # 65536 / 360 = 182 tiles a degree each way at the equator, plus edges.
    assert 182 * 182 <= equator <= 184 * 184
    north = ci.chunks_in_cell((60, 10), 16)
    assert north == pytest.approx(equator / math.cos(math.radians(60.5)), rel=0.03)
    assert ci.chunks_in_cell((0, 10), 17) == pytest.approx(equator * 4, rel=0.02)


def test_estimate_uses_cached_chunk_sizes():
    inv = ci.CacheInventory(chunk_zooms={16: [100, 1_500_000]})
    est = ci.estimate_imagery([(0, 10), (1, 10)], [16, 17], inv)
    count16 = ci.chunks_in_cell((0, 10), 16) + ci.chunks_in_cell((1, 10), 16)
    assert est[16] == (count16, count16 * 15_000)
    # No ZL17 chunks cached: falls back to the overall average.
    assert est[17][1] == est[17][0] * 15_000
    assert ci.estimate_imagery([(0, 10)], [16])[16][1] == \
        ci.chunks_in_cell((0, 10), 16) * ci.DEFAULT_CHUNK_BYTES


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def test_scan_groups_mesh_and_imagery_per_tile(stores):
    ao_dir, cache, _, _ = stores
    inv = ci.scan(ao_dir, cache)

    greece = inv.cells[(39, 23)]
    assert (greece.dsf_bytes, greece.overlay_bytes, greece.mask_bytes) == (1000, 300, 50)
    assert greece.chunk_bytes == 10 and greece.maptypes == {"EOX"}
    assert greece.state == "both" and not greece.pinned
    assert greece.mesh_used == pytest.approx(OLD, abs=2)

    seattle = inv.cells[(47, -123)]
    assert seattle.dsf_bytes == 2000 and seattle.pinned
    assert seattle.chunk_bytes == 140 and seattle.chunk_files == 2
    assert seattle.dds_bytes == 520 and seattle.dds_files == 2
    assert seattle.zooms == {16, 17, 18}
    assert seattle.imagery_bytes == 660

    assert set(inv.cells) == {(39, 23), (47, -123)}
    assert inv.mesh_bytes == 3350 and inv.imagery_bytes == 670
    assert (inv.mesh_cells, inv.imagery_cells, inv.pinned_cells) == (2, 2, 1)
    assert sorted(inv.packages) == [("yAO_stream_xp11_overlays", "overlay"),
                                    ("z_ao_stream_xp11", "ortho")]
    assert inv.chunk_zooms[16] == [2, 110]


def test_scan_handles_missing_folders(tmp_path):
    inv = ci.scan(str(tmp_path / "nope"), "")
    assert inv.cells == {} and not inv.streaming


def test_pinned_tile_with_nothing_stored(tmp_path):
    ao_dir = str(tmp_path)
    root = make_stream_package(ao_dir, "z_ao_stream_xp11")
    ts.write_pins(root, {(10, 20)})
    inv = ci.scan(ao_dir, "")
    assert inv.cells[(10, 20)].pinned and inv.cells[(10, 20)].state == "none"


# ---------------------------------------------------------------------------
# Deleting and pinning
# ---------------------------------------------------------------------------

def test_delete_mesh_only(stores):
    ao_dir, cache, ortho, overlay = stores
    result = ci.delete_cells(ao_dir, cache, [(39, 23), (47, -123)], mesh=True, imagery=False)

    assert result.bytes == 3350 and result.files == 4 and not result.errors
    assert not os.path.exists(dsf_path(overlay, 39, 23))
    assert os.path.exists(os.path.join(ortho, "textures", "water_transition.png"))
    assert ts.read_pins(ortho) == set()  # deleting stops keeping it
    inv = ci.scan(ao_dir, cache)
    assert inv.mesh_bytes == 0 and inv.imagery_bytes == 670


def test_delete_imagery_only_leaves_other_tiles_and_junk(stores):
    ao_dir, cache, ortho, _ = stores
    result = ci.delete_cells(ao_dir, cache, [(47, -123)], mesh=False, imagery=True)

    assert result.bytes == 660 and result.files == 4
    assert not os.path.exists(os.path.join(cache, ci.DDS_DIR, "+40-130"))
    assert os.path.exists(os.path.join(cache, "notes.jpg"))
    assert os.path.exists(os.path.join(cache, "terrain_index", "abc.json"))
    inv = ci.scan(ao_dir, cache)
    assert inv.cells[(39, 23)].chunk_bytes == 10
    assert inv.cells[(47, -123)].imagery_bytes == 0
    assert inv.cells[(47, -123)].dsf_bytes == 2000
    assert ts.read_pins(ortho) == {(47, -123)}


def test_pin_cells_writes_every_package(stores):
    ao_dir, _, ortho, overlay = stores
    assert ci.pin_cells(ao_dir, [(39, 23)]) == 2
    assert ts.read_pins(ortho) == {(47, -123), (39, 23)}
    assert ts.read_pins(overlay) == {(39, 23)}
    assert ci.pin_cells(ao_dir, [(39, 23), (47, -123)], pinned=False) == 2
    assert ts.read_pins(ortho) == set()
    assert not os.path.exists(os.path.join(ortho, ts.PIN_NAME))


def test_eviction_skips_pinned_tiles(tmp_path, release_for_keep):
    root = make_stream_package(str(tmp_path), "z_ao_stream_xp11", limit=1e-6)  # 1 KB
    with gzip.open(os.path.join(root, ts.INDEX_NAME), "wt") as fh:
        json.dump(release_for_keep["index"], fh)
    write(dsf_path(root, 39, 23), 4000, OLD)
    write(os.path.join(root, "textures", MASK), 400, OLD)
    write(dsf_path(root, 40, 23), 4000, OLD)
    write(dsf_path(root, 41, 23), 600, OLD + 100)
    ts.write_pins(root, {(39, 23)})
    pkg = ts.StreamPackage(root, ts.read_marker(root))
    try:
        pkg.evict()
    finally:
        pkg.shutdown()
    assert os.path.exists(dsf_path(root, 39, 23))
    assert os.path.exists(os.path.join(root, "textures", MASK))
    assert not os.path.exists(dsf_path(root, 40, 23))
    # The pinned 4.4 KB don't count, so the last 600 bytes fit under 1 KB.
    assert os.path.exists(dsf_path(root, 41, 23))


# ---------------------------------------------------------------------------
# Keeping tiles for offline use
# ---------------------------------------------------------------------------

SEA_DSF_REL = "Earth nav data/+30+020/+39+023.dsf"


@pytest.fixture
def release_for_keep():
    dsf = make_dsf(["terrain/100000_125184_BI18.ter",
                    "terrain/100000_148176_BI18_sea_overlay.ter"]) + os.urandom(3000)
    mask = b"\x89PNG" + os.urandom(200)
    blob = build_release_zip([
        ("z_test_00/" + SEA_DSF_REL, dsf, zipfile.ZIP_DEFLATED),
        ("z_test_00/terrain/100000_125184_BI18.ter", LAND_TER, zipfile.ZIP_DEFLATED),
        ("z_test_00/textures/" + MASK, mask, zipfile.ZIP_STORED),
    ])
    url = "https://github.com/o/r/releases/download/t/z_test_00.zip"
    return {"url": url, "blob": blob, "dsf": dsf, "mask": mask,
            "index": index_for({url: blob})}


def test_keep_offline_pins_and_downloads_dsf_and_masks(tmp_path, release_for_keep):
    ao_dir = str(tmp_path / "scenery")
    root = make_stream_package(ao_dir, "z_ao_stream_xp11")
    with gzip.open(os.path.join(root, ts.INDEX_NAME), "wt") as fh:
        json.dump(release_for_keep["index"], fh)
    host = FakeHost({release_for_keep["url"]: release_for_keep["blob"]})

    def factory(root, marker, hidden=()):
        return ts.StreamPackage(root, marker, remote=ts.RemoteAssets(session=host),
                                hidden=hidden)

    steps = []
    result = ci.keep_offline(ao_dir, [(39, 23), (0, 0)], package_factory=factory,
                             progress=lambda *a: steps.append(a))

    assert not result.errors
    assert result.missing == [(0, 0)]
    assert result.files == 2
    assert result.bytes == len(release_for_keep["dsf"]) + len(release_for_keep["mask"])
    assert open(dsf_path(root, 39, 23), "rb").read() == release_for_keep["dsf"]
    assert open(os.path.join(root, "textures", MASK), "rb").read() == release_for_keep["mask"]
    assert ts.read_pins(root) == {(39, 23)}
    assert steps[-1] == (2, 2, result.bytes)

    # Second run: everything is on disk, nothing is downloaded.
    ranges = host.ranges
    again = ci.keep_offline(ao_dir, [(39, 23)], package_factory=factory)
    assert again.bytes == 0 and host.ranges == ranges


def test_keep_offline_needs_streaming(tmp_path):
    with pytest.raises(ValueError):
        ci.keep_offline(str(tmp_path), [(1, 1)])


def test_keep_offline_stops_when_cancelled(tmp_path, release_for_keep):
    ao_dir = str(tmp_path / "scenery")
    root = make_stream_package(ao_dir, "z_ao_stream_xp11")
    with gzip.open(os.path.join(root, ts.INDEX_NAME), "wt") as fh:
        json.dump(release_for_keep["index"], fh)
    host = FakeHost({release_for_keep["url"]: release_for_keep["blob"]})
    result = ci.keep_offline(
        ao_dir, [(39, 23)], cancelled=lambda: True,
        package_factory=lambda r, m, hidden=(): ts.StreamPackage(
            r, m, remote=ts.RemoteAssets(session=host), hidden=hidden))
    assert result.cancelled and result.files == 0 and host.ranges == 0
    assert ts.read_pins(root) == {(39, 23)}


class FakeKeepPackage:
    """Every tile is in the index; download_tile is scripted per tile."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.started = []
        self.lock = threading.Lock()

    def entry(self, rel):
        return [0, 0, 1, 1, 0, 0]

    def download_tile(self, lat, lon, cancelled=lambda: False):
        with self.lock:
            self.started.append((lat, lon))
        return self.behaviour(lat, lon)

    def shutdown(self):
        pass


def test_keep_offline_downloads_tiles_in_parallel(tmp_path):
    ao_dir = str(tmp_path / "scenery")
    make_stream_package(ao_dir, "z_ao_stream_xp11")
    cells = [(40, c) for c in range(8)]
    barrier = threading.Barrier(ci.KEEP_WORKERS, timeout=5)

    def behaviour(lat, lon):
        barrier.wait()  # only passes if KEEP_WORKERS tiles run at once
        return 10, 2

    pkg = FakeKeepPackage(behaviour)
    steps = []
    result = ci.keep_offline(ao_dir, cells, package_factory=lambda *a, **k: pkg,
                             progress=lambda *a: steps.append(a))
    assert not result.errors and not result.cancelled
    assert (result.bytes, result.files) == (80, 16)
    assert sorted(pkg.started) == cells
    assert [s[0] for s in steps] == list(range(1, 9)) and steps[-1][2] == 80


def test_keep_offline_stops_starting_tiles_when_throttled(tmp_path, monkeypatch):
    ao_dir = str(tmp_path / "scenery")
    root = make_stream_package(ao_dir, "z_ao_stream_xp11")
    monkeypatch.setattr(ci, "KEEP_WORKERS", 1)
    cells = [(40, c) for c in range(5)]

    def behaviour(lat, lon):
        if lon == 1:
            raise ts.ThrottledError("429")
        return 10, 1

    pkg = FakeKeepPackage(behaviour)
    result = ci.keep_offline(ao_dir, cells, package_factory=lambda *a, **k: pkg)
    assert result.cancelled
    assert pkg.started == [(40, 0), (40, 1)]
    assert len(result.errors) == 1 and "429" in result.errors[0]
    assert (result.bytes, result.files) == (10, 1)
    assert ts.read_pins(root) == set(cells)  # pins stay; a later run resumes

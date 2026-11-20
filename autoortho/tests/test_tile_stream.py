#!/usr/bin/env python3
"""Tests for streaming scenery tiles out of release zips."""

import gzip
import io
import json
import os
import struct
import sys
import threading
import time
import zipfile
import zlib

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
for directory in (ROOT, AUTOORTHO_DIR, os.path.join(ROOT, "scripts")):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import tile_stream as ts  # noqa: E402


# Copied byte for byte from the kubilus1 XP11 Europe package.
LAND_TER = (
    "A\n800\nTERRAIN\n\n"
    "LOAD_CENTER 39.18969 -8.07495 1895 4096\n"
    "BASE_TEX_NOWRAP ../textures/100000_125184_BI18.dds\n"
    "DECAL_LIB lib/g10/decals/maquify_2_green_key.dcl\n"
    "NO_ALPHA\n"
)
WATER_TER = (
    "A\n800\nTERRAIN\n\n"
    "LOAD_CENTER 39.18969 -8.07495 1895 4096\n"
    "BASE_TEX_NOWRAP ../textures/100000_125184_BI18.dds\n"
    "BORDER_TEX ../textures/water_transition.png\n"
    "WET\nNO_SHADOW\n"
)
SEA_TER = (
    "A\n800\nTERRAIN\n\n"
    "LOAD_CENTER 39.18969 23.49976 1895 4096\n"
    "BASE_TEX_NOWRAP ../textures/100000_148176_BI18.dds\n"
    "LOAD_CENTER_BORDER 39.18969 23.49976 1895 256\n"
    "BORDER_TEX ../textures/100000_148176_ZL18.png\n"
    "WET\nNO_SHADOW\n"
)


# ---------------------------------------------------------------------------
# .ter generation
# ---------------------------------------------------------------------------

def test_generated_ter_files_match_published_ones():
    assert ts.synthesize_ter("100000_125184_BI18.ter").decode() == LAND_TER
    assert ts.synthesize_ter("100000_125184_BI18_water_overlay.ter").decode() == WATER_TER
    assert ts.synthesize_ter("100000_148176_BI18_sea_overlay.ter").decode() == SEA_TER


def test_ter_mask_size_follows_zoom():
    # Mask is 4096 / 2**(zoom - 14) pixels. (Near the poles the published
    # files round the centre differently; the index stores those verbatim.)
    text = ts.synthesize_ter("10000_35504_BI16_sea_overlay.ter").decode()
    assert text.splitlines()[6].endswith(" 1024")


def test_ter_exceptions_and_roughness():
    exceptions = {"1_2_BI16.ter": "A\n800\nTERRAIN\n\nSPECIAL\nSUPER_ROUGHNESS 0.2\n"}
    text = ts.synthesize_ter("1_2_BI16.ter", exceptions=exceptions, roughness=0.8).decode()
    assert "SPECIAL" in text
    assert text.count("SUPER_ROUGHNESS") == 1
    assert text.endswith("SUPER_ROUGHNESS 0.8\n")
    assert ts.synthesize_ter("terrain_Water") is None


def test_border_textures_reads_mask_names():
    assert ts.border_textures(SEA_TER.encode()) == ["textures/100000_148176_ZL18.png"]
    assert ts.border_textures(LAND_TER.encode()) == []


# ---------------------------------------------------------------------------
# DSF terrain list
# ---------------------------------------------------------------------------

def _atom(atom_id, payload):
    return atom_id + struct.pack("<I", len(payload) + 8) + payload


def make_dsf(terrain_names):
    tert = _atom(b"TRET", b"".join(n.encode() + b"\0" for n in terrain_names))
    defn = _atom(b"NFED", tert + _atom(b"TJBO", b""))
    head = _atom(b"DAEH", b"")
    return b"XPLNEDSF" + struct.pack("<I", 1) + head + defn + b"\0" * 16


def test_dsf_terrain_names_and_tiles():
    names = ["terrain_Water", "terrain/100000_125184_BI18.ter",
             "terrain/100000_125184_BI18_sea_overlay.ter", "terrain/16_32_BI16.ter"]
    dsf = make_dsf(names)
    assert ts.dsf_terrain_names(dsf) == names
    assert ts.terrain_tiles(names) == [(16, 32, "BI", 16), (100000, 125184, "BI", 18)]
    assert ts.dsf_terrain_names(b"7z\xbc\xaf'\x1c" + b"\0" * 40) is None


def make_raster_dsf(rasters):
    nmed = _atom(b"NMED", b"".join(n.encode() + b"\0" for n in rasters))
    tert = _atom(b"TRET", b"terrain/1_2_BI16.ter\0")
    return (b"XPLNEDSF" + struct.pack("<I", 1) + _atom(b"DAEH", b"")
            + _atom(b"NFED", tert + nmed) + b"\0" * 16)


def make_installed_package(root, dsf_bytes):
    folder = root / "Earth nav data" / "+40-010"
    folder.mkdir(parents=True)
    (folder / "+40-010.dsf").write_bytes(dsf_bytes)
    return str(root)


XP12_RASTERS = ["elevation", "sea_level", "soundscape", "spr1", "spr2",
                "sum1", "sum2", "fal1", "fal2", "win1", "win2"]


def test_dsf_raster_names_and_native_seasons(tmp_path):
    dsf = make_raster_dsf(XP12_RASTERS)
    assert ts.dsf_raster_names(dsf) == XP12_RASTERS
    assert ts.dsf_terrain_names(dsf) == ["terrain/1_2_BI16.ter"]
    assert ts.dsf_raster_names(make_dsf([])) == []

    assert ts.package_has_native_seasons(make_installed_package(tmp_path / "xp12", dsf))
    assert not ts.package_has_native_seasons(
        make_installed_package(tmp_path / "xp11", make_dsf(["terrain/1_2_BI16.ter"])))
    assert not ts.package_has_native_seasons(
        make_installed_package(tmp_path / "elev", make_raster_dsf(["elevation"])))
    assert not ts.package_has_native_seasons(str(tmp_path / "missing"))

    # The answer is kept next to the package while its first DSF is unchanged.
    cache = tmp_path / "xp11" / ts.NATIVE_SEASONS_CACHE
    stored = json.loads(cache.read_text())
    assert stored["seasons"] is False and stored["dsf"] == "+40-010/+40-010.dsf"
    cache.write_text(json.dumps(dict(stored, seasons=True)))
    assert ts.package_has_native_seasons(str(tmp_path / "xp11"))  # read, not rechecked
    cache.write_text(json.dumps(dict(stored, seasons=True, size=1)))
    assert not ts.package_has_native_seasons(str(tmp_path / "xp11"))  # stale: rechecked


def test_installed_package_with_native_seasons_shows_included(tmp_path):
    import downloader
    scenery = tmp_path / "Custom Scenery"
    info = scenery / "z_autoortho" / "xp12area_info.json"
    info.parent.mkdir(parents=True)
    info.write_text('{"name": "xp12area"}')

    def status():
        release = downloader.Release("xp12area", install_dir=str(scenery))
        release.load(str(info))
        return release.seasons_apply_status

    package = scenery / "z_autoortho" / "scenery" / "z_ao_xp12area"
    make_installed_package(package, make_dsf(["terrain/1_2_BI16.ter"]))
    assert status() == downloader.SeasonsApplyStatus.NOT_APPLIED
    dsf = package / "Earth nav data" / "+40-010" / "+40-010.dsf"
    dsf.write_bytes(make_raster_dsf(XP12_RASTERS) + b"x")  # new size: not cached
    assert status() == downloader.SeasonsApplyStatus.INCLUDED
    # Seasons applied by the patch keep their own status.
    info.write_text('{"name": "xp12area", "processed_dsf_seasons": {"+40-010": ["+40-010.dsf"]},'
                    ' "dsf_folders_files": {"+40-010": ["+40-010.dsf"]}}')
    assert status() == downloader.SeasonsApplyStatus.APPLIED


# ---------------------------------------------------------------------------
# Fake release host
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeHost:
    """GitHub-like host: download URLs redirect to signed storage URLs."""

    def __init__(self, files, expires_in=3600):
        self.files = files  # download url -> bytes
        self.headers = {}
        self.resolves = 0
        self.ranges = 0
        self.fail_next = []
        self.expires_in = expires_in
        self.lock = threading.Lock()

    def get(self, url, headers=None, allow_redirects=True, timeout=None):
        with self.lock:
            if self.fail_next:
                return self.fail_next.pop(0)
            if url in self.files:
                self.resolves += 1
                exp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() + self.expires_in))
                return FakeResponse(302, headers={
                    "Location": f"https://storage.example/{url.rsplit('/', 1)[1]}?se={exp}&sig=x"})
            name = url.split("?")[0].rsplit("/", 1)[1]
            blob = next(v for k, v in self.files.items() if k.endswith("/" + name))
            start, end = headers["Range"].split("=")[1].split("-")
            self.ranges += 1
            return FakeResponse(206, blob[int(start):int(end) + 1])


def build_release_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, method in members:
            zf.writestr(name, data, compress_type=method)
    return buf.getvalue()


def index_for(blobs, top="z_test_00"):
    """Index the way scripts/build_tile_index.py does, from local bytes."""
    import build_tile_index as bti
    assets, files = [], {}
    for url, blob in blobs.items():
        idx = len(assets)
        assets.append({"name": url.rsplit("/", 1)[1], "url": url, "size": len(blob)})
        for info in zipfile.ZipFile(io.BytesIO(blob)).infolist():
            rel = bti.member_rel(info.filename, "ortho")
            if rel:
                files[rel] = [idx, info.header_offset, info.compress_size,
                              info.file_size, info.compress_type, info.CRC]
    return {"format": 1, "name": "test", "variant": "xp11", "built": 1700000000,
            "assets": assets, "files": files, "ter": {"params": {}, "exceptions": {}}}


DSF_REL = "Earth nav data/+30+000/+39-009.dsf"
MASK_REL = "textures/100000_148176_ZL18.png"


@pytest.fixture
def release():
    dsf = make_dsf(["terrain/100000_125184_BI18.ter",
                    "terrain/100000_148176_BI18_sea_overlay.ter"]) + os.urandom(5000)
    mask = b"\x89PNG" + os.urandom(300)
    blob = build_release_zip([
        ("z_test_00/Earth nav data/+30+000/+39-009.dsf", dsf, zipfile.ZIP_DEFLATED),
        ("z_test_00/terrain/100000_125184_BI18.ter", LAND_TER, zipfile.ZIP_DEFLATED),
        ("z_test_00/textures/100000_148176_ZL18.png", mask, zipfile.ZIP_STORED),
        ("z_test_00/*/junk.dsf", b"x", zipfile.ZIP_STORED),
    ])
    url = "https://github.com/o/r/releases/download/t/z_test_00.zip"
    return {"url": url, "blob": blob, "dsf": dsf, "mask": mask,
            "index": index_for({url: blob})}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def test_index_skips_junk_and_terrain(release):
    files = release["index"]["files"]
    assert set(files) == {DSF_REL, MASK_REL}


def test_fetch_member_reuses_signed_url(release):
    host = FakeHost({release["url"]: release["blob"]})
    remote = ts.RemoteAssets(session=host)
    index = ts.TileIndex(release["index"])
    assert ts.fetch_member(remote, index, index.lookup(DSF_REL)) == release["dsf"]
    assert ts.fetch_member(remote, index, index.lookup(MASK_REL)) == release["mask"]
    assert host.resolves == 1
    assert host.ranges == 2


def test_fetch_member_rejects_corrupt_data(release):
    index = ts.TileIndex(release["index"])
    entry = list(index.lookup(MASK_REL))
    entry[5] ^= 1  # wrong CRC
    remote = ts.RemoteAssets(session=FakeHost({release["url"]: release["blob"]}))
    with pytest.raises(IOError, match="CRC"):
        ts.fetch_member(remote, index, entry)


def test_split_asset_reads_across_parts(release):
    blob = release["blob"]
    cut = len(blob) // 2
    p0 = "https://github.com/o/r/releases/download/t/y_test.zip.00"
    p1 = "https://github.com/o/r/releases/download/t/y_test.zip.01"
    host = FakeHost({p0: blob[:cut], p1: blob[cut:]})
    asset = {"size": len(blob), "parts": [{"url": p0, "size": cut},
                                          {"url": p1, "size": len(blob) - cut}]}
    remote = ts.RemoteAssets(session=host)
    assert ts.asset_read(remote, asset, cut - 10, cut + 9) == blob[cut - 10:cut + 10]
    assert ts.asset_read(remote, asset, 0, 9) == blob[:10]


def test_throttled_host_pauses_fetches(release):
    host = FakeHost({release["url"]: release["blob"]})
    host.fail_next = [FakeResponse(429, headers={"Retry-After": "120"})]
    now = [1000.0]
    remote = ts.RemoteAssets(session=host, clock=lambda: now[0])
    index = ts.TileIndex(release["index"])
    with pytest.raises(ts.ThrottledError):
        ts.fetch_member(remote, index, index.lookup(DSF_REL))
    assert remote.in_cooldown()
    with pytest.raises(ts.ThrottledError):
        ts.fetch_member(remote, index, index.lookup(DSF_REL))
    assert host.resolves == 0
    now[0] += 121
    assert ts.fetch_member(remote, index, index.lookup(DSF_REL)) == release["dsf"]


def test_expired_signed_url_is_resolved_again(release):
    host = FakeHost({release["url"]: release["blob"]})
    remote = ts.RemoteAssets(session=host)
    index = ts.TileIndex(release["index"])
    ts.fetch_member(remote, index, index.lookup(MASK_REL))
    host.fail_next = [FakeResponse(403)]  # storage says the signature expired
    assert ts.fetch_member(remote, index, index.lookup(MASK_REL)) == release["mask"]
    assert host.resolves == 2


# ---------------------------------------------------------------------------
# Package runtime
# ---------------------------------------------------------------------------

class DroppingHost(FakeHost):
    """Drops connections the way GitHub's CDN sometimes does.

    ``stall_offsets``: the first read at each offset hangs until ``release``
    is set, then fails with a connection reset. ``drop_first``: that many
    reads fail with a reset straight away.
    """

    def __init__(self, files):
        super().__init__(files)
        self.stall_offsets = set()
        self.drop_first = 0
        self.release = threading.Event()
        self.stalled = threading.Event()
        self.range_calls = 0

    def get(self, url, headers=None, allow_redirects=True, timeout=None):
        if headers and "Range" in headers and url.startswith("https://storage"):
            start = int(headers["Range"].split("=")[1].split("-")[0])
            with self.lock:
                self.range_calls += 1
                stall = start in self.stall_offsets
                self.stall_offsets.discard(start)
                drop = self.drop_first > 0
                self.drop_first -= drop
            if stall:
                self.stalled.set()
                self.release.wait(10)
            if stall or drop:
                raise requests.exceptions.ConnectionError(
                    "An existing connection was forcibly closed by the remote host")
        return super().get(url, headers, allow_redirects, timeout)


def make_package(tmp_path, release, hidden=(), host=None):
    root = tmp_path / "z_ao_stream"
    root.mkdir()
    with gzip.open(root / "tile_index.json.gz", "wt") as fh:
        json.dump(release["index"], fh)
    (root / ts.MARKER_NAME).write_text(json.dumps({"index": "tile_index.json.gz"}))
    host = host or FakeHost({release["url"]: release["blob"]})
    seen = []
    pkg = ts.StreamPackage(str(root), ts.read_marker(str(root)),
                           remote=ts.RemoteAssets(session=host),
                           on_terrain=seen.extend, hidden=hidden)
    return pkg, host, seen, root


def test_ensure_local_fetches_dsf_and_queues_masks(tmp_path, release):
    pkg, host, seen, root = make_package(tmp_path, release)
    try:
        assert pkg.dsf_names("+30+000") == ["+39-009.dsf"]
        local = pkg.ensure_local(DSF_REL, timeout=5)
        assert local and open(local, "rb").read() == release["dsf"]
        mask = root / "textures" / "100000_148176_ZL18.png"
        deadline = time.time() + 5
        while not mask.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert mask.read_bytes() == release["mask"]
        assert (100000, 125184, "BI", 18) in seen
        assert not list(root.rglob("*.part*"))
        # Already on disk: no further requests.
        before = host.ranges
        assert pkg.ensure_local(DSF_REL) == local
        assert host.ranges == before
    finally:
        pkg.shutdown()


def test_hidden_and_unknown_files_are_not_fetched(tmp_path, release):
    pkg, host, _, _ = make_package(tmp_path, release, hidden={DSF_REL})
    try:
        assert pkg.dsf_names("+30+000") == []
        assert pkg.ensure_local(DSF_REL, timeout=1) is None
        assert pkg.ensure_local("Earth nav data/+50+000/+51+000.dsf", timeout=1) is None
        assert host.ranges == 0
    finally:
        pkg.shutdown()


def test_failed_fetch_returns_none_and_backs_off(tmp_path, release):
    pkg, host, _, _ = make_package(tmp_path, release)
    try:
        host.fail_next = [FakeResponse(429)]
        assert pkg.ensure_local(DSF_REL, timeout=5) is None
        assert pkg.unavailable(DSF_REL)
    finally:
        pkg.shutdown()


MASK_REL = "textures/100000_148176_ZL18.png"


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    return predicate()


def test_open_does_not_wait_for_a_stalled_prefetch(tmp_path, release):
    # The X-Plane crash of 2026-11-19: a mask prefetch hung on a dropped CDN
    # connection, the open waited behind it, was woken by its failure and
    # reported the mask missing.
    host = DroppingHost({release["url"]: release["blob"]})
    pkg, host, _, root = make_package(tmp_path, release, host=host)
    host.stall_offsets = {pkg.entry(MASK_REL)[1]}
    try:
        assert pkg.prefetch(MASK_REL)
        assert host.stalled.wait(5)
        started = time.monotonic()
        local = pkg.ensure_local(MASK_REL, timeout=5)
        assert local and open(local, "rb").read() == release["mask"]
        assert time.monotonic() - started < 2
        host.release.set()  # now the prefetch's connection drops
        assert wait_until(lambda: not pkg._inflight_bg)
        assert open(local, "rb").read() == release["mask"]
        assert not list(root.rglob("*.part*"))
    finally:
        host.release.set()
        pkg.shutdown()


def test_dropped_connection_is_retried(release):
    host = DroppingHost({release["url"]: release["blob"]})
    host.drop_first = 1
    remote = ts.RemoteAssets(session=host)
    index = ts.TileIndex(release["index"])
    assert ts.fetch_member(remote, index, index.lookup(DSF_REL)) == release["dsf"]
    assert host.range_calls == 2


def test_open_retries_and_then_remembers_a_failure(tmp_path, release):
    host = DroppingHost({release["url"]: release["blob"]})
    pkg, host, _, _ = make_package(tmp_path, release, host=host)
    try:
        # A file that can't be fetched at all: the open gives up, and the
        # next open of it falls back at once instead of waiting again.
        host.drop_first = 1000
        assert pkg.ensure_local(MASK_REL, timeout=5) is None
        calls = host.range_calls
        assert pkg.ensure_local(MASK_REL, timeout=5) is None
        assert host.range_calls == calls
        # Every read of the first download fails; the open tries again.
        host.drop_first = ts.RemoteAssets.ATTEMPTS
        assert pkg.ensure_local(DSF_REL, timeout=5)
    finally:
        pkg.shutdown()


def test_masks_are_not_dropped_when_the_dsf_queue_is_full(tmp_path, release):
    # In the 2026-11-19 flight a burst of neighbour DSFs filled the queue and
    # the masks of prefetched tiles were silently skipped.
    pkg, _, _, root = make_package(tmp_path, release)
    try:
        pkg._background_pending = pkg.MAX_BACKGROUND
        assert not pkg.prefetch("Earth nav data/+30+000/+39-009.dsf")
        assert pkg.prefetch(MASK_REL)
        assert wait_until(lambda: (root / MASK_REL).exists())
        assert (root / MASK_REL).read_bytes() == release["mask"]
        assert pkg._background_pending == pkg.MAX_BACKGROUND
        assert not pkg._mask_priority
    finally:
        pkg.shutdown()


def test_priority_workers_run_urgent_jobs_first():
    workers = ts.PriorityWorkers(1, "test")
    gate, ran = threading.Event(), []
    try:
        workers.submit(0, gate.wait, 5)  # occupies the only worker
        workers.submit(ts.StreamPackage.MASK_AHEAD, ran.append, "ahead-1")
        workers.submit(ts.StreamPackage.MASK_AHEAD, ran.append, "ahead-2")
        workers.submit(ts.StreamPackage.MASK_URGENT, ran.append, "urgent")
        gate.set()
        assert wait_until(lambda: len(ran) == 3)
        assert ran == ["urgent", "ahead-1", "ahead-2"]
    finally:
        workers.shutdown()
    with pytest.raises(RuntimeError):
        workers.submit(0, ran.append, "late")


class ClosingHost(FakeHost):
    def __init__(self, files):
        super().__init__(files)
        self.closes = 0

    def close(self):
        self.closes += 1


def test_idle_connections_are_dropped_before_the_next_request(release):
    host = ClosingHost({release["url"]: release["blob"]})
    remote = ts.RemoteAssets(session=host)
    index = ts.TileIndex(release["index"])
    entry = index.lookup(DSF_REL)
    ts.fetch_member(remote, index, entry)
    ts.fetch_member(remote, index, entry)
    assert host.closes == 0  # busy: keep the connections
    remote._last_used -= ts.RemoteAssets.IDLE_RESET + 1  # idle for a while
    assert ts.fetch_member(remote, index, entry) == release["dsf"]
    assert host.closes == 1 and remote.stats["idle_resets"] == 1


@pytest.fixture
def fuse_module(monkeypatch):
    """autoortho_fuse with the FUSE library stubbed out (callbacks only)."""
    import importlib.util
    from types import SimpleNamespace
    stub = SimpleNamespace(
        FUSE=object, Operations=object, FuseOSError=OSError,
        fuse_get_context=lambda: (0, 0, 0), _libfuse=SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "autoortho.mfusepy", stub)
    path = os.path.join(AUTOORTHO_DIR, "autoortho_fuse.py")
    spec = importlib.util.spec_from_file_location("_test_fuse_stream", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unfetchable_mask_is_served_as_a_blank_stand_in(tmp_path, release, fuse_module):
    import itertools
    import re
    host = DroppingHost({release["url"]: release["blob"]})
    host.drop_first = 1000  # the CDN keeps dropping the connection
    pkg, host, _, root = make_package(tmp_path, release, host=host)
    ao = fuse_module.AutoOrtho.__new__(fuse_module.AutoOrtho)
    never = re.compile(r"(?!)")
    ao.root, ao.dsf_re, ao.dds_re, ao.ter_tile_re = str(root), never, never, never
    ao.default_uid = ao.default_gid = 0
    ao._stream = pkg
    ao._virtual_fh = itertools.count(1 << 40)
    ao._virtual_lock = threading.Lock()
    ao._virtual_files = {}
    ao._stream_placeholders = set()
    path = "/" + MASK_REL
    try:
        fh = ao.open(path, os.O_RDONLY)  # used to raise ENOENT and crash X-Plane
        data = ao.read(path, 1 << 20, 0, fh)
        assert data == pkg.placeholder_png(MASK_REL)
        assert ao._getattr_stream(path)["st_size"] == len(data)
        # Once the real mask arrives, it is served from disk again.
        (root / MASK_REL).write_bytes(release["mask"])
        assert ao._getattr_stream(path) is None
    finally:
        pkg.shutdown()


def test_placeholder_mask_is_a_blank_png_of_the_mask_size(tmp_path, release):
    import struct
    pkg, _, _, _ = make_package(tmp_path, release)
    try:
        data = pkg.placeholder_png(MASK_REL)
    finally:
        pkg.shutdown()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, color) == (256, 256, 8, 0)  # ZL18, mask_zl 14
    idat_len = struct.unpack(">I", data[33:37])[0]
    assert data[37:41] == b"IDAT"
    pixels = zlib.decompress(data[41:41 + idat_len])
    assert pixels == b"\x00" * (257 * 256)


def test_installed_dsfs_lists_other_packages(tmp_path):
    scenery = tmp_path / "scenery"
    (scenery / "z_ao_na" / "Earth nav data" / "+40-080").mkdir(parents=True)
    (scenery / "z_ao_na" / "Earth nav data" / "+40-080" / "+40-075.dsf").write_bytes(b"x")
    (scenery / "z_ao_stream" / "Earth nav data" / "+40-080").mkdir(parents=True)
    (scenery / "z_ao_stream" / "Earth nav data" / "+40-080" / "+41-075.dsf").write_bytes(b"x")
    found = ts.installed_dsfs(str(scenery), str(scenery / "z_ao_stream"))
    assert found == {"Earth nav data/+40-080/+40-075.dsf"}


# ---------------------------------------------------------------------------
# Index builder helpers
# ---------------------------------------------------------------------------

def test_builder_joins_split_parts_in_numeric_order():
    import build_tile_index as bti
    release_dict = {"assets": [
        {"name": f"y_eur_overlays.zip.{i:02d}", "size": 10,
         "browser_download_url": f"u{i}"} for i in (10, 2, 0, 1)
    ] + [{"name": "z_eur_00.zip", "size": 5, "browser_download_url": "z"}]}
    overlay = bti.scenery_assets(release_dict, "overlay")
    assert [p["url"] for p in overlay[0]["parts"]] == ["u0", "u1", "u2", "u10"]
    ortho = bti.scenery_assets(release_dict, "ortho")
    assert ortho == [{"name": "z_eur_00.zip", "url": "z", "size": 5}]


def test_builder_member_paths():
    import build_tile_index as bti
    assert bti.member_rel("z_eur_00/Earth nav data/+40+000/+40+000.dsf", "ortho") == \
        "Earth nav data/+40+000/+40+000.dsf"
    assert bti.member_rel("z_eur_00/terrain/1_2_BI16.ter", "ortho") is None
    assert bti.member_rel("y_eur/yOrtho4XP_Overlays/Earth nav data/+40+000/+40+000.dsf",
                          "overlay") == "Earth nav data/+40+000/+40+000.dsf"
    assert bti.member_rel("y_eur/Earth nav data/+40+000/+40+000.dsf", "overlay") is None


def test_ter_checker_learns_settings_and_keeps_odd_files():
    import build_tile_index as bti
    checker = bti.TerChecker()
    checker.check("100000_148176_BI18_sea_overlay.ter", SEA_TER)
    checker.check("100000_125184_BI18.ter", LAND_TER)
    odd = SEA_TER.replace("_ZL18.png", "_BI18_ZL18.png").replace("148176", "148192")
    checker.check("100000_148192_BI18_sea_overlay.ter", odd)
    assert checker.params["mask_zl"] == 14
    assert checker.params["mask_name_by_zoom"] == {"18": "{row}_{col}_ZL{zoom}.png"}
    assert list(checker.exceptions) == ["100000_148192_BI18_sea_overlay.ter"]


# ---------------------------------------------------------------------------
# Terrain lookup
# ---------------------------------------------------------------------------

def test_terrain_lookup_accepts_streamed_tiles(tmp_path):
    import getortho
    lookup = getortho.TerrainTileLookup(str(tmp_path / "missing"), "z_ao_stream",
                                        build_async=False,
                                        index_cache_dir=str(tmp_path / "idx"))
    assert not lookup.has_tile(100000, 125184, "BI", 18)
    lookup.add_virtual_tiles([(100000, 125184, "BI", 18), (25008, 31296, "BI", 16)])
    assert lookup.has_tile(100000, 125184, "BI", 18)
    lat, lon = lookup._tile_to_latlon(100000, 125184, 18)
    near = lookup.get_highzoom_tiles_near(lat, lon, radius_nm=5)
    assert (100000, 125184, "BI", 18) in near
    assert lookup.get_tiles_for_position(lat, lon, maptype_filter="BI")


# ---------------------------------------------------------------------------
# XP12 (Ortho4XP 1.40) templates
# ---------------------------------------------------------------------------

XP12_PARAMS = {"decal": None, "mask_zl": 16, "decal_on_sea": True, "land_shadow": True}
XP12_HEAD = (
    "A\n800\nTERRAIN\n\n"
    "LOAD_CENTER 39.18969 -8.07495 1895 4096\n"
    "BASE_TEX_NOWRAP ../textures/100000_125184_BI18.dds\n"
)


def test_xp12_templates_follow_ortho4xp_140():
    make = lambda n, **p: ts.synthesize_ter(n, dict(XP12_PARAMS, **p)).decode()
    assert make("100000_125184_BI18.ter") == XP12_HEAD + "NO_ALPHA\n"
    assert make("100000_125184_BI18_sea.ter") == XP12_HEAD + "WATER_COLOR_MASK\nWET\nNO_SHADOW\n"
    assert make("100000_125184_BI18_water.ter") == XP12_HEAD + "WATER_COLOR_MASK\nWET\nNO_SHADOW\n"
    assert make("100000_125184_BI18_water_overlay.ter") == (
        XP12_HEAD + "BORDER_TEX ../textures/water_transition.png\nWET\nNO_SHADOW\n")
    assert make("100000_125184_BI18_sea_overlay.ter") == (
        XP12_HEAD + "LOAD_CENTER_BORDER 39.18969 -8.07495 1895 1024\n"
        "BORDER_TEX ../textures/100000_125184_ZL18.png\nWET\nNO_SHADOW\n")
    with_decal = make("100000_125184_BI18_sea_overlay.ter", decal="lib/x.dcl")
    assert "BORDER_TEX ../textures/100000_125184_ZL18.png\nDECAL_LIB lib/x.dcl\nWET" in with_decal
    assert make("100000_125184_BI18.ter", land_shadow=False).endswith("NO_ALPHA\nNO_SHADOW\n")


def test_ter_checker_learns_xp12_settings():
    import build_tile_index as bti
    checker = bti.TerChecker()
    land = XP12_HEAD + "NO_ALPHA\nNO_SHADOW\n"
    sea = (XP12_HEAD.replace("125184", "125200").replace("-8.07495", "-8.05298")
           + "LOAD_CENTER_BORDER 39.18969 -8.05298 1895 1024\n"
           "BORDER_TEX ../textures/100000_125200_ZL18.png\nDECAL_LIB lib/x.dcl\nWET\nNO_SHADOW\n")
    checker.check("100000_125184_BI18.ter", land)
    checker.check("100000_125200_BI18_sea_overlay.ter", sea)
    assert checker.params["decal_on_sea"] is True
    assert checker.params["land_shadow"] is False
    assert checker.params["mask_zl"] == 16
    assert checker.exceptions == {}, checker.exceptions


# ---------------------------------------------------------------------------
# Repacking
# ---------------------------------------------------------------------------

def make_package_tree(base):
    root = base / "z_ao_test_zl16_v1"
    tiles = {"+39-009": "+30-010", "+39-008": "+30-010"}
    for tile, folder in tiles.items():
        d = root / "Earth nav data" / folder
        d.mkdir(parents=True, exist_ok=True)
    (root / "Earth nav data" / "+30-010" / "+39-009.dsf").write_bytes(ts.SEVENZIP_MAGIC + os.urandom(4000))
    (root / "Earth nav data" / "+30-010" / "+39-008.dsf").write_bytes(make_dsf([]) + os.urandom(4000))
    (root / "terrain").mkdir()
    (root / "textures").mkdir()
    # 100000_125184 @ZL18 is in tile +39-009; 100000_125552 in +39-008.
    for base_name in ("100000_125184_BI18", "100000_125552_BI18"):
        (root / "terrain" / f"{base_name}.ter").write_text("x" * 200)
        (root / "terrain" / f"{base_name}_sea_overlay.ter").write_text("y" * 200)
    (root / "textures" / "100000_125184_ZL18.png").write_bytes(b"\x89PNG" + os.urandom(2000))
    (root / "textures" / "water_transition.png").write_bytes(b"\x89PNG" + os.urandom(100))
    (root / "README.txt").write_text("attributions")
    (root / "_docs").mkdir()
    (root / "_docs" / "cfg.txt").write_text("cfg")
    return base


def test_repack_groups_whole_tiles(tmp_path):
    import repack_scenery as rp
    import build_tile_index as bti
    src = make_package_tree(tmp_path / "src")
    out = tmp_path / "out"
    rp.main([str(src), "--area", "test", "--out", str(out), "--max-mb", "0",
             "--no-compress-dsf"])
    # --max-mb 0 forces one tile per zip.
    assert sorted(p.name for p in out.iterdir()) == [
        "test_info.json", "z_test_00.zip", "z_test_00.zip.sha256",
        "z_test_01.zip", "z_test_01.zip.sha256"]
    info = json.loads((out / "test_info.json").read_text())
    assert info["id"] == "test" and info["ortho_prefix"] == "z_test_"
    z0 = zipfile.ZipFile(out / "z_test_00.zip")
    z1 = zipfile.ZipFile(out / "z_test_01.zip")
    n0, n1 = z0.namelist(), z1.namelist()
    assert {n.split("/")[0] for n in n0} == {"z_test_00"}
    assert {n.split("/")[0] for n in n1} == {"z_test_01"}
    # The tile order is sorted, so +39-009 (lon -9) comes first.
    assert n0[0] == "z_test_00/Earth nav data/+30-010/+39-009.dsf"
    assert "z_test_00/terrain/100000_125184_BI18_sea_overlay.ter" in n0
    assert "z_test_00/textures/100000_125184_ZL18.png" in n0
    assert "z_test_01/terrain/100000_125552_BI18.ter" in n1
    for shared in ("README.txt", "_docs/cfg.txt", "textures/water_transition.png"):
        assert f"z_test_00/{shared}" in n0
    assert z0.getinfo("z_test_00/Earth nav data/+30-010/+39-009.dsf").compress_type == zipfile.ZIP_STORED
    assert z1.getinfo("z_test_01/Earth nav data/+30-010/+39-008.dsf").compress_type == zipfile.ZIP_DEFLATED
    kinds = [n.split("/")[1] for n in n0 if not n.endswith("/")]
    assert kinds.index("terrain") > kinds.index("Earth nav data")
    sha_line = (out / "z_test_00.zip.sha256").read_text()
    import hashlib
    assert sha_line == f"{hashlib.sha256((out / 'z_test_00.zip').read_bytes()).hexdigest()}  z_test_00.zip\n"
    assert bti.member_rel(n0[0], "ortho") == "Earth nav data/+30-010/+39-009.dsf"


@pytest.mark.skipif(sys.platform != "win32", reason="uses the bundled Windows 7-Zip")
def test_repack_7z_compresses_dsfs(tmp_path):
    import repack_scenery as rp
    src = make_package_tree(tmp_path / "src")
    raw_dsf = src / "z_ao_test_zl16_v1" / "Earth nav data" / "+30-010" / "+39-008.dsf"
    original = raw_dsf.read_bytes()
    out = tmp_path / "out"
    rp.main([str(src), "--area", "test", "--out", str(out), "--workers", "2"])
    member = "z_test_00/Earth nav data/+30-010/+39-008.dsf"
    with zipfile.ZipFile(out / "z_test_00.zip") as zf:
        info = zf.getinfo(member)
        packed = zf.read(member)
    assert info.compress_type == zipfile.ZIP_STORED
    assert packed.startswith(ts.SEVENZIP_MAGIC)
    unpacked = tmp_path / "check.dsf"
    unpacked.write_bytes(packed)
    assert ts.unpack_7z_dsf(str(unpacked)) == original
    assert (tmp_path / "out_work" / "Earth nav data" / "+30-010" / "+39-008.dsf").exists()


def test_builder_indexes_local_release_with_final_urls(tmp_path):
    import repack_scenery as rp
    import build_tile_index as bti
    src = make_package_tree(tmp_path / "src")
    root = src / "z_ao_test_zl16_v1"
    # Real .ter content so the template check has something to verify.
    for f in (root / "terrain").iterdir():
        f.write_bytes(ts.synthesize_ter(f.name))
    out = tmp_path / "release"
    rp.main([str(src), "--area", "test", "--out", str(out), "--no-compress-dsf"])
    index_path = tmp_path / "idx.json.gz"
    bti.main(["--local-dir", str(out), "--out", str(index_path), "--variant", "xp12",
              "--url-base", "https://github.com/o/r/releases/download/{area}_v1/"])
    index = ts.TileIndex.load(str(index_path))
    assert index.variant == "xp12"
    assert index.asset_url(0) == "https://github.com/o/r/releases/download/test_v1/z_test_00.zip"
    assert index.lookup("Earth nav data/+30-010/+39-009.dsf") is not None
    assert index.lookup("textures/100000_125184_ZL18.png") is not None
    data = json.load(gzip.open(index_path, "rt"))
    assert data["format"] == ts.INDEX_FORMAT == 2
    assert data["ter"]["verified"] == 4 and data["ter"]["exceptions"] == {}

    # An index in the old format converts without reading the releases.
    old = dict(data, format=1, files={rel: index.lookup(rel) for rel in (
        "Earth nav data/+30-010/+39-009.dsf", "textures/100000_125184_ZL18.png")})
    old_path, new_path = tmp_path / "old.json.gz", tmp_path / "new.json.gz"
    with gzip.open(old_path, "wt") as fh:
        json.dump(old, fh)
    bti.main(["--convert", str(old_path), "--out", str(new_path)])
    converted = ts.TileIndex.load(str(new_path))
    assert json.load(gzip.open(new_path, "rt"))["format"] == 2
    assert len(converted) == 2 and converted.name == old["name"]
    assert converted.lookup("textures/100000_125184_ZL18.png") ==         index.lookup("textures/100000_125184_ZL18.png")


def test_packed_index_finds_every_file_and_nothing_else():
    files = {
        "Earth nav data/+30-010/+39-009.dsf": [0, 10, 20, 30, 93, 0xFFFFFFFF],
        "Earth nav data/+30-010/+38-009.dsf": [1, 2**40, 5, 6, 0, 7],
        "textures/100000_125184_ZL18.png": [2, 99, 8, 9, 8, 123],
        "textures/25000_31296_ZL16.png": [131, 1, 2, 3, 0, 4],
        "textures/0_0_ZL0.png": [3, 4, 5, 6, 0, 7],
        "textures/0100_5_ZL16.png": [4, 5, 6, 7, 0, 8],   # not a mask name
        "textures/water_overlay.png": [5, 6, 7, 8, 0, 9],
    }
    data = {"format": 1, "assets": [], "files": files}
    old = ts.TileIndex(data)
    new = ts.TileIndex(dict(data, format=2, files=ts.pack_files(files)))
    for index in (old, new):
        assert len(index) == len(files)
        assert all(index.lookup(rel) == entry for rel, entry in files.items())
        assert index.lookup("textures/100_5_ZL16.png") is None
        assert index.lookup("textures/100000_125184_ZL17.png") is None
        assert index.lookup("Earth nav data/+30-010/+30-010.dsf") is None
        assert index.dsf_count == 2
        assert index.dsf_names("+30-010") == ["+38-009.dsf", "+39-009.dsf"]
    packed = ts.pack_files(files)
    assert len(packed["names"]) == 4  # DSFs and non-mask textures keep names
    with pytest.raises(ValueError):
        ts.TileIndex(dict(data, format=2, files=dict(packed, names=packed["names"][:-1])))
    with pytest.raises(ValueError):
        ts.TileIndex(dict(data, format=3))


# ---------------------------------------------------------------------------
# Coalesced reads (Keep Offline)
# ---------------------------------------------------------------------------

def test_coalesce_runs_groups_neighbours_of_one_asset():
    def e(asset, offset, csize=100):
        return [asset, offset, csize, csize, 0, 0]
    entries = [e(0, 5000), e(0, 0), e(1, 0), e(0, 10_000_000), e(0, 1000)]
    runs = ts.coalesce_runs(entries, max_gap=64 * 1024)
    assert runs == [[1, 4, 0], [3], [2]]
    # A span limit splits a run that would read too much at once.
    assert ts.coalesce_runs(entries[:2] + [entries[4]], max_gap=64 * 1024,
                            max_span=1700) == [[1, 2], [0]]


MASK_COLS = (148176, 148192, 148208, 148224, 148240)


def mask_rel(col):
    return f"textures/100000_{col}_ZL18.png"


@pytest.fixture
def many_masks_release():
    dsf = make_dsf([f"terrain/100000_{c}_BI18_sea_overlay.ter" for c in MASK_COLS]
                   + ["terrain/100000_125184_BI18.ter"]) + os.urandom(5000)
    masks = {mask_rel(c): b"\x89PNG" + os.urandom(300 + i) for i, c in enumerate(MASK_COLS)}
    members = [("z_test_00/" + ts.dsf_rel(39, -9), dsf, zipfile.ZIP_DEFLATED)]
    for rel, data in masks.items():
        members.append(("z_test_00/" + rel, data, zipfile.ZIP_STORED))
    blob = build_release_zip(members)
    url = "https://github.com/o/r/releases/download/t/z_test_00.zip"
    return {"url": url, "blob": blob, "dsf": dsf, "masks": masks,
            "index": index_for({url: blob})}


def test_download_tile_reads_all_masks_with_one_request(tmp_path, many_masks_release):
    rel = many_masks_release
    pkg, host, _seen, root = make_package(tmp_path, rel)
    try:
        got, files = pkg.download_tile(39, -9)
    finally:
        pkg.shutdown()
    assert files == 1 + len(MASK_COLS)
    assert got == len(rel["dsf"]) + sum(len(m) for m in rel["masks"].values())
    assert host.ranges == 2  # the DSF, then every mask in one read
    for m, data in rel["masks"].items():
        assert (root / m).read_bytes() == data


def test_fetch_many_skips_stored_and_unknown_files(tmp_path, many_masks_release):
    rel = many_masks_release
    pkg, host, _seen, root = make_package(tmp_path, rel)
    first = mask_rel(MASK_COLS[0])
    (root / first).write_bytes(rel["masks"][first])
    try:
        got, files = pkg.fetch_many([first, "textures/1_1_ZL16.png"]
                                    + [mask_rel(c) for c in MASK_COLS[1:]])
    finally:
        pkg.shutdown()
    assert files == len(MASK_COLS) - 1
    assert got == sum(len(rel["masks"][mask_rel(c)]) for c in MASK_COLS[1:])
    assert host.ranges == 1


def test_fetch_run_falls_back_when_an_extra_field_is_long():
    big_extra = struct.pack("<HH", 0xCAFE, 2000) + b"\0" * 2000
    members = []
    buf = io.BytesIO()
    datas = [b"\x89PNG" + os.urandom(200) for _ in range(3)]
    with zipfile.ZipFile(buf, "w") as zf:
        for i, data in enumerate(datas):
            info = zipfile.ZipInfo(f"z_test_00/textures/1_{i}_ZL16.png")
            if i == 1:
                info.extra = big_extra
            zf.writestr(info, data)
    blob = buf.getvalue()
    url = "https://github.com/o/r/releases/download/t/z_test_00.zip"
    index = ts.TileIndex(index_for({url: blob}))
    host = FakeHost({url: blob})
    remote = ts.RemoteAssets(session=host)
    entries = [index.lookup(f"textures/1_{i}_ZL16.png") for i in range(3)]
    assert ts.fetch_run(remote, index, entries) == datas

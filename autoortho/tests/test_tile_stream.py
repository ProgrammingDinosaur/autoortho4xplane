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

import pytest

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

def make_package(tmp_path, release, hidden=()):
    root = tmp_path / "z_ao_stream"
    root.mkdir()
    with gzip.open(root / "tile_index.json.gz", "wt") as fh:
        json.dump(release["index"], fh)
    (root / ts.MARKER_NAME).write_text(json.dumps({"index": "tile_index.json.gz"}))
    host = FakeHost({release["url"]: release["blob"]})
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
    assert "Earth nav data/+30-010/+39-009.dsf" in index.files
    assert "textures/100000_125184_ZL18.png" in index.files
    data = json.load(gzip.open(index_path, "rt"))
    assert data["ter"]["verified"] == 4 and data["ter"]["exceptions"] == {}

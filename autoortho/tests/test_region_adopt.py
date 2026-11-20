"""Tests for moving installed regions into the streaming package."""
import gzip
import json
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
TESTS_DIR = os.path.dirname(__file__)
for directory in (ROOT, AUTOORTHO_DIR, os.path.join(ROOT, "scripts"), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import region_adopt as ra  # noqa: E402
import tile_stream as ts  # noqa: E402
from test_tile_stream import build_release_zip, index_for, make_dsf  # noqa: E402

URL = "https://github.com/o/r/releases/download/t/z_test_00.zip"
TILES = [(39, 23), (39, 24), (40, 23)]
MASK = "textures/100000_148176_ZL18.png"


def dsf_bytes(cell):
    return make_dsf(["terrain/100000_125184_BI18.ter"]) + bytes(f"{cell}", "ascii")


def make_streaming(ao_dir, source="xp11", kind="ortho", tiles=TILES, extra=()):
    """A streaming package whose index has ``tiles`` (and ``extra`` files)."""
    members = [("z_test_00/" + ts.dsf_rel(*c), dsf_bytes(c), zipfile.ZIP_STORED)
               for c in tiles]
    members += [("z_test_00/" + rel, data, zipfile.ZIP_STORED) for rel, data in extra]
    blob = build_release_zip(members)
    index = index_for({URL: blob})
    index["variant"] = source
    name = (ts.ORTHO_PACKAGE if kind == "ortho" else ts.OVERLAY_PACKAGE).format(source=source)
    root = os.path.join(ao_dir, name)
    os.makedirs(root)
    with gzip.open(os.path.join(root, ts.INDEX_NAME), "wt") as fh:
        json.dump(index, fh)
    ts.write_marker(root, {"source": source, "kind": kind, "index": ts.INDEX_NAME,
                           "disk_limit_gb": 20})
    return root


def make_region(ao_dir, region_id, tiles, masks=(MASK,), info=None, backups=False):
    root = os.path.join(ao_dir, f"z_ao_{region_id}")
    for cell in tiles:
        path = os.path.join(root, *ts.dsf_rel(*cell).split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(dsf_bytes(cell))
    os.makedirs(os.path.join(root, "textures"), exist_ok=True)
    os.makedirs(os.path.join(root, "terrain"), exist_ok=True)
    with open(os.path.join(root, "terrain", "100000_125184_BI18.ter"), "w") as fh:
        fh.write("A\n800\nTERRAIN\n")
    for rel in masks:
        with open(os.path.join(root, *rel.split("/")), "wb") as fh:
            fh.write(b"\x89PNG" + rel.encode())
    if backups:
        os.makedirs(os.path.join(root, ra.SEASON_BACKUPS, "+30+020"), exist_ok=True)
        with open(os.path.join(root, ra.SEASON_BACKUPS, "+30+020", "+39+023.dsf.bak"), "wb") as fh:
            fh.write(b"original")
    info_path = os.path.join(os.path.dirname(ao_dir), f"{region_id}_info.json")
    with open(info_path, "w") as fh:
        json.dump(info or {"id": region_id, "ver": "0.0.51"}, fh)
    return root, info_path


@pytest.fixture
def ao_dir(tmp_path):
    path = tmp_path / "z_autoortho" / "scenery"
    path.mkdir(parents=True)
    return str(path)


def test_region_moves_into_the_package_and_is_pinned(ao_dir):
    pkg = make_streaming(ao_dir)
    region, info = make_region(ao_dir, "eur", TILES[:2])
    checks = ra.check_regions(ao_dir)
    assert [(c.region_id, c.movable, c.known, c.dsfs) for c in checks] == [("eur", True, 2, 2)]
    steps = []
    result = ra.adopt_regions(ao_dir, progress=lambda *a: steps.append(a))
    assert result.regions == ["eur"] and not result.errors
    assert (result.tiles, result.files) == (2, 3)
    assert not os.path.exists(region) and not os.path.exists(info)
    for cell in TILES[:2]:
        with open(os.path.join(pkg, *ts.dsf_rel(*cell).split("/")), "rb") as fh:
            assert fh.read() == dsf_bytes(cell)
    assert os.path.exists(os.path.join(pkg, *MASK.split("/")))
    assert ts.read_pins(pkg) == set(TILES[:2])
    assert steps[-1] == ("eur", 3, 3)
    assert ra.installed_regions(ao_dir) == []


def test_patched_overlapping_and_unindexed_tiles_move_as_they_are(ao_dir):
    """Seasons-patched DSFs keep their seasons; tiles the index lacks still
    move (the mount lists stored DSFs), as long as most are known."""
    pkg = make_streaming(ao_dir)
    region, _ = make_region(ao_dir, "afr", TILES + [(41, 23)], backups=True)
    patched = os.path.join(region, *ts.dsf_rel(39, 23).split("/"))
    with open(patched, "wb") as fh:
        fh.write(ts.SEVENZIP_MAGIC + b"patched")
    check = ra.check_regions(ao_dir)[0]
    assert check.movable and check.seasons and check.known == 3
    result = ra.adopt_regions(ao_dir)
    assert result.regions == ["afr"]
    with open(os.path.join(pkg, *ts.dsf_rel(39, 23).split("/")), "rb") as fh:
        assert fh.read() == ts.SEVENZIP_MAGIC + b"patched"
    assert os.path.exists(os.path.join(pkg, *ts.dsf_rel(41, 23).split("/")))
    assert (41, 23) in ts.read_pins(pkg)


def test_region_stays_when_the_streamed_set_is_not_xp11(ao_dir):
    make_streaming(ao_dir, source="xp12")
    region, _ = make_region(ao_dir, "eur", TILES)
    check = ra.check_regions(ao_dir)[0]
    assert not check.movable and "XP11" in check.reason
    result = ra.adopt_regions(ao_dir)
    assert result.regions == [] and "eur" in result.kept
    assert os.path.isdir(region)


def test_region_stays_when_the_index_lacks_most_tiles(ao_dir):
    make_streaming(ao_dir, tiles=TILES[:1])
    region, _ = make_region(ao_dir, "eur", TILES)
    result = ra.adopt_regions(ao_dir)
    assert "only 1 of its 3" in result.kept["eur"]
    assert os.path.isdir(region)


def test_files_already_streamed_are_not_duplicated(ao_dir):
    pkg = make_streaming(ao_dir)
    dst = os.path.join(pkg, *ts.dsf_rel(*TILES[0]).split("/"))
    os.makedirs(os.path.dirname(dst))
    with open(dst, "wb") as fh:
        fh.write(dsf_bytes(TILES[0]))
    make_region(ao_dir, "eur", TILES[:2])
    result = ra.adopt_regions(ao_dir)
    assert result.files == 2  # the second DSF and the mask


def test_cancel_midway_keeps_the_region_and_pins(ao_dir):
    pkg = make_streaming(ao_dir)
    region, _ = make_region(ao_dir, "eur", TILES)
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 3  # region loop check, then two files move

    result = ra.adopt_regions(ao_dir, cancelled=cancelled)
    assert result.cancelled and result.regions == []
    assert os.path.isdir(region)
    assert ts.read_pins(pkg) == set(TILES)
    # A second run finishes the job.
    again = ra.adopt_regions(ao_dir)
    assert again.regions == ["eur"] and not os.path.exists(region)


def test_patched_reflectivity_carries_over(ao_dir):
    pkg = make_streaming(ao_dir)
    make_region(ao_dir, "eur", TILES, info={
        "roughness_value": 0.4, "processed_ter_roughness": {"z_eur_00": {"a.ter": 1}}})
    ra.adopt_regions(ao_dir)
    assert ts.read_marker(pkg)["roughness"] == 0.4


def test_streaming_off_raises(ao_dir):
    make_region(ao_dir, "eur", TILES)
    with pytest.raises(ValueError):
        ra.adopt_regions(ao_dir)


def test_installed_overlays_move_into_the_overlay_package(ao_dir, tmp_path):
    make_streaming(ao_dir)
    over = make_streaming(ao_dir, kind="overlay", tiles=TILES[:1])
    custom = tmp_path / "Custom Scenery"
    legacy = custom / ts.LEGACY_OVERLAY_FOLDER
    for cell in TILES[:2]:
        path = legacy.joinpath(*ts.dsf_rel(*cell).split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"overlay")
    result = ra.adopt_regions(ao_dir, custom_scenery_dir=str(custom))
    assert result.overlays == 1
    assert os.path.exists(os.path.join(over, *ts.dsf_rel(*TILES[0]).split("/")))
    # The overlay index doesn't have the second tile: it stays installed.
    assert legacy.joinpath(*ts.dsf_rel(*TILES[1]).split("/")).exists()
    assert ts.read_pins(over) == {TILES[0]}

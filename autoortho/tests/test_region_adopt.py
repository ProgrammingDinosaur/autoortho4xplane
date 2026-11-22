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


class _Clock:
    def __init__(self, step):
        self.now, self.step = 100.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def _progress_steps(ao_dir, monkeypatch, clock_step):
    """Moves a region of 3 DSFs and 20 masks with a clock that advances
    ``clock_step`` seconds per reading; returns the progress reports."""
    make_streaming(ao_dir)
    masks = [f"textures/{100000 + i}_148176_ZL18.png" for i in range(20)]
    make_region(ao_dir, "eur", TILES, masks=masks)
    monkeypatch.setattr(ra.time, "monotonic", _Clock(clock_step))
    steps = []
    result = ra.adopt_regions(ao_dir, progress=lambda *a: steps.append(a))
    assert result.regions == ["eur"] and not result.errors
    return steps


def test_fast_moves_report_progress_by_time_not_per_file(ao_dir, monkeypatch):
    """Links take microseconds: reporting every few files flooded the UI
    thread with updates and froze the window during the start-up move."""
    steps = _progress_steps(ao_dir, monkeypatch, clock_step=0.0)
    assert steps == [("eur", 1, 23), ("eur", 23, 23)]


def test_slow_moves_report_each_interval(ao_dir, monkeypatch):
    steps = _progress_steps(ao_dir, monkeypatch,
                            clock_step=ra.PROGRESS_INTERVAL)
    assert [done for _, done, _ in steps] == list(range(1, 24))


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


def test_region_moves_into_another_set_with_its_own_terrain(ao_dir):
    """An XP11 region in an XP12 package keeps its terrain files, which the
    package serves ahead of the XP12 ones it would generate."""
    pkg = make_streaming(ao_dir, source="xp12")
    region, info = make_region(ao_dir, "eur", TILES, info={
        "roughness_value": 0.4, "processed_ter_roughness": {"z": {"a.ter": 1}}})
    check = ra.check_regions(ao_dir)[0]
    assert check.movable
    result = ra.adopt_regions(ao_dir)
    assert result.regions == ["eur"] and not os.path.exists(region)
    ter = os.path.join(pkg, "terrain", "100000_125184_BI18.ter")
    region_ter = "A\n800\nTERRAIN\n"
    assert open(ter).read() == region_ter
    assert "roughness" not in ts.read_marker(pkg)   # kept in its own files
    package = ts.StreamPackage(pkg, ts.read_marker(pkg), remote=object())
    try:
        with open(ter, "rb") as fh:
            assert package.ter_bytes("100000_125184_BI18.ter") == fh.read()
        # Other terrain is still generated.
        assert b"LOAD_CENTER" in package.ter_bytes("100000_125200_BI18.ter")
    finally:
        package.shutdown()


def test_same_set_regions_leave_terrain_to_the_package(ao_dir):
    pkg = make_streaming(ao_dir)
    make_region(ao_dir, "eur", TILES)
    ra.adopt_regions(ao_dir)
    assert not os.path.exists(os.path.join(pkg, "terrain", "100000_125184_BI18.ter"))


def test_moved_masks_are_in_the_package_before_any_dsf_moves(ao_dir):
    pkg = make_streaming(ao_dir, source="xp12")
    region, _ = make_region(ao_dir, "eur", TILES)
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 4  # region check, terrain file, mask, one DSF

    ra.adopt_regions(ao_dir, cancelled=cancelled)
    moved = [c for c in TILES
             if os.path.exists(os.path.join(pkg, *ts.dsf_rel(*c).split("/")))]
    assert len(moved) == 1
    assert os.path.exists(os.path.join(pkg, *MASK.split("/")))
    assert os.path.exists(os.path.join(pkg, "terrain", "100000_125184_BI18.ter"))
    # The region still has everything its remaining DSFs need.
    assert os.path.exists(os.path.join(region, *MASK.split("/")))
    assert os.path.exists(os.path.join(region, "terrain", "100000_125184_BI18.ter"))


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


def test_region_left_without_dsfs_is_removed(ao_dir):
    """An earlier move took every DSF but didn't remove the folder (the app
    quit, or the delete failed): the next run removes the leftover instead
    of keeping an empty region installed for ever."""
    pkg = make_streaming(ao_dir)
    region, info = make_region(ao_dir, "asi", [])
    os.makedirs(os.path.join(region, ts.DSF_DIR, "+30+100"))
    assert ra.installed_regions(ao_dir) == ["z_ao_asi"]
    check = ra.check_regions(ao_dir)[0]
    assert check.movable and check.leftover
    result = ra.adopt_regions(ao_dir)
    assert result.cleaned == ["asi"] and result.regions == [] and not result.kept
    assert not os.path.exists(region) and not os.path.exists(info)
    assert os.path.exists(os.path.join(pkg, *MASK.split("/")))
    assert ts.read_pins(pkg) == set()
    assert ra.installed_regions(ao_dir) == []
    assert "removed 1 emptied" in str(result)


def test_leftover_region_goes_even_when_turned_off(ao_dir, tmp_path):
    make_streaming(ao_dir)
    region, _ = make_region(ao_dir, "eur", [])
    os.makedirs(os.path.join(region, ts.DSF_DIR))
    custom = tmp_path / "Custom Scenery"
    custom.mkdir()
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 Version\nSCENERY\n\n"
        "SCENERY_PACK_DISABLED Custom Scenery/z_ao_eur/\n")
    result = ra.adopt_regions(ao_dir, custom_scenery_dir=str(custom))
    assert result.cleaned == ["eur"] and not os.path.exists(region)


def test_move_counts_when_the_folder_cant_be_removed(ao_dir, monkeypatch):
    """The tiles are in the package; a failed delete leaves a DSF-less
    folder, which the next run removes."""
    pkg = make_streaming(ao_dir)
    region, info = make_region(ao_dir, "eur", TILES)
    real_remove = ra._remove_region

    def fail(_root):
        raise OSError("Directory not empty")

    monkeypatch.setattr(ra, "_remove_region", fail)
    result = ra.adopt_regions(ao_dir)
    assert result.regions == ["eur"] and not result.errors
    assert os.path.isdir(region) and os.path.exists(info)
    for cell in TILES:
        assert os.path.exists(os.path.join(pkg, *ts.dsf_rel(*cell).split("/")))
    monkeypatch.setattr(ra, "_remove_region", real_remove)
    again = ra.adopt_regions(ao_dir)
    assert again.cleaned == ["eur"] and not again.kept
    assert not os.path.exists(region) and not os.path.exists(info)
    assert ts.read_pins(pkg) == set(TILES)


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


def test_installed_overlays_are_left_where_they_are(ao_dir, tmp_path):
    """yAutoOrtho_Overlays works with any set; moving it into an XP11
    overlay package would lose it when switching to XP12 (no overlays)."""
    make_streaming(ao_dir)
    make_streaming(ao_dir, kind="overlay")
    make_region(ao_dir, "eur", TILES)
    custom = tmp_path / "Custom Scenery"
    path = custom.joinpath(ts.LEGACY_OVERLAY_FOLDER, *ts.dsf_rel(*TILES[0]).split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"overlay")
    result = ra.adopt_regions(ao_dir, custom_scenery_dir=str(custom))
    assert result.regions == ["eur"] and path.exists()


def write_packs(custom, lines):
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 Version\nSCENERY\n\n" + "".join(l + "\n" for l in lines))


def test_region_turned_off_in_xplane_stays_installed(ao_dir, tmp_path):
    make_streaming(ao_dir)
    off, _ = make_region(ao_dir, "eur", TILES[:2])
    make_region(ao_dir, "afr", TILES[2:])
    custom = tmp_path / "Custom Scenery"
    write_packs(custom, ["SCENERY_PACK_DISABLED Custom Scenery/z_ao_eur/",
                         "SCENERY_PACK Custom Scenery/z_ao_afr/"])
    assert ra.disabled_packs(str(custom)) == {"z_ao_eur"}
    checks = {c.region_id: c for c in ra.check_regions(ao_dir, str(custom))}
    assert "turned off" in checks["eur"].reason and checks["afr"].movable
    result = ra.adopt_regions(ao_dir, custom_scenery_dir=str(custom))
    assert result.regions == ["afr"] and "eur" in result.kept
    assert os.path.isdir(off)


def test_region_on_another_drive_is_copied_when_there_is_room(ao_dir, monkeypatch):
    pkg = make_streaming(ao_dir)
    region, _ = make_region(ao_dir, "eur", TILES)
    monkeypatch.setattr(ra, "_same_drive", lambda a, b: False)
    real_replace = os.replace

    def replace(src, dst):
        if src.startswith(region):          # a move off the "other drive"
            raise OSError(17, "not the same device")
        real_replace(src, dst)

    monkeypatch.setattr(ra.os, "replace", replace)
    result = ra.adopt_regions(ao_dir)
    assert result.regions == ["eur"] and not os.path.exists(region)
    for cell in TILES:
        assert os.path.exists(os.path.join(pkg, *ts.dsf_rel(*cell).split("/")))
    assert not any(n.endswith(".part") for _d, _s, files in os.walk(pkg) for n in files)


def test_region_on_another_drive_stays_without_room(ao_dir, monkeypatch):
    make_streaming(ao_dir)
    region, _ = make_region(ao_dir, "eur", TILES)
    monkeypatch.setattr(ra, "_same_drive", lambda a, b: False)
    monkeypatch.setattr(ra.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 1024 ** 3})())
    result = ra.adopt_regions(ao_dir)
    assert "another drive" in result.kept["eur"] and os.path.isdir(region)


def test_a_linked_region_is_unlinked_not_emptied(ao_dir, tmp_path, monkeypatch):
    make_streaming(ao_dir)
    region, _ = make_region(ao_dir, "eur", TILES)
    monkeypatch.setattr(ra, "_is_link", lambda p: p == region)
    removed = []
    monkeypatch.setattr(ra.os, "unlink", lambda p: removed.append(p))
    monkeypatch.setattr(ra.shutil, "rmtree",
                        lambda p: pytest.fail("rmtree on a link target"))
    ra.adopt_regions(ao_dir)
    assert removed == [region]


# ---------------------------------------------------------------------------
# Migration on first start
# ---------------------------------------------------------------------------

def _index_host(source_tiles=TILES):
    from test_tile_stream_setup import IndexHost
    import gzip as _gzip
    import io
    members = [("z_test_00/" + ts.dsf_rel(*c), dsf_bytes(c), zipfile.ZIP_STORED)
               for c in source_tiles]
    index = index_for({URL: build_release_zip(members)})
    index["variant"] = "xp11"
    buf = io.BytesIO()
    with _gzip.GzipFile(fileobj=buf, mode="wb") as fh:
        fh.write(json.dumps(index).encode())
    spec = ts.SOURCES["xp11"]
    return IndexHost({spec["ortho_index"]: buf.getvalue(),
                      spec["overlay_index"]: buf.getvalue()})


def test_migration_sets_up_the_xp11_set_offline_only_and_moves_everything(ao_dir, tmp_path):
    make_region(ao_dir, "eur", TILES[:2])
    make_region(ao_dir, "afr", TILES[2:])
    custom = tmp_path / "Custom Scenery"
    legacy = custom / ts.LEGACY_OVERLAY_FOLDER
    path = legacy.joinpath(*ts.dsf_rel(*TILES[0]).split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"overlay")
    assert ra.needs_migration(ao_dir)

    result = ra.migrate(ao_dir, str(custom), session=_index_host())

    # No overlay package: the installed overlays keep working where they are.
    assert result.created == ["z_ao_stream_xp11"] and path.exists()
    assert sorted(result.regions) == ["afr", "eur"]
    assert "seasons" not in ts.read_marker(os.path.join(ao_dir, "z_ao_stream_xp11"))
    assert not ra.needs_migration(ao_dir)
    packages = ts.stream_packages(ao_dir)
    # Behaves as before: only the tiles that were installed are offered.
    assert all(m.get("fetch_missing") is False for m in packages.values())
    pkg = os.path.join(ao_dir, "z_ao_stream_xp11")
    assert ts.read_pins(pkg) == set(TILES)
    assert ra.migrate(ao_dir, str(custom), session=_index_host()).regions == []


def test_migration_into_the_set_already_in_use(ao_dir):
    pkg = make_streaming(ao_dir, source="xp12")
    make_region(ao_dir, "eur", TILES)
    result = ra.migrate(ao_dir, session=object())   # no download needed
    assert result.created == [] and result.regions == ["eur"]
    assert os.path.exists(os.path.join(pkg, "terrain", "100000_125184_BI18.ter"))


def test_migration_waits_when_the_index_cannot_be_downloaded(ao_dir):
    from test_tile_stream_setup import IndexHost
    region, _ = make_region(ao_dir, "eur", TILES)
    with pytest.raises(Exception):
        ra.migrate(ao_dir, session=IndexHost({}))
    assert os.path.isdir(region) and ts.stream_packages(ao_dir) == {}
    assert ra.needs_migration(ao_dir)


def test_shared_textures_move_with_the_region(ao_dir):
    """XP11 water edges use textures/water_transition.png, which the XP12
    set's index doesn't have: it must come along, or X-Plane stops."""
    pkg = make_streaming(ao_dir, source="xp12")
    region, _ = make_region(ao_dir, "eur", TILES,
                            masks=(MASK, "textures/water_transition.png"))
    ra.adopt_regions(ao_dir)
    assert os.path.exists(os.path.join(pkg, "textures", "water_transition.png"))
    assert not os.path.exists(region)


def test_migration_keeps_seasons_on_for_regions_that_had_them(ao_dir):
    make_region(ao_dir, "eur", TILES, backups=True)
    ra.migrate(ao_dir, session=_index_host())
    assert ts.read_marker(os.path.join(ao_dir, "z_ao_stream_xp11"))["seasons"] is True

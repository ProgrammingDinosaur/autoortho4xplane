"""Which scenery set each stored tile is from, and switching tiles to the set
in use: tiles of another set bring their own terrain files, which must go
with them so the set in use gets its own again."""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
TESTS_DIR = os.path.dirname(__file__)
for directory in (ROOT, AUTOORTHO_DIR, os.path.join(ROOT, "scripts"), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import pytest  # noqa: E402

import cache_inventory as ci  # noqa: E402
import region_adopt as ra  # noqa: E402
import tile_stream as ts  # noqa: E402
from test_cache_inventory import write_index  # noqa: E402
from test_region_adopt import TILES, make_region, make_streaming  # noqa: E402

Z = 16
N = 1 << Z


def col_at(lon):
    return int((lon + 180.0) / 360.0 * N)


def row_at(lat):
    return int(ci.lat_row(lat, Z))


def ter_inside(cell):
    """A texture well inside a cell."""
    lat, lon = cell
    return f"{row_at(lat + 0.5)}_{col_at(lon + 0.5)}_BI{Z}.ter"


def ter_across(west, east):
    """A texture straddling the line between two cells side by side."""
    lat, lon = east
    return f"{row_at(lat + 0.5)}_{col_at(lon) - 8}_BI{Z}.ter"


def mask_for(ter):
    row, col, _ = ter.split("_")
    return f"{row}_{col}_ZL{Z}.png"


def test_texture_cells_follow_the_texture_area():
    a, b = (47, -123), (47, -122)
    name = ter_inside(a)
    row, col = map(int, name.split("_")[:2])
    assert ts.texture_cells(row, col, Z) == {a}
    row, col = map(int, ter_across(a, b).split("_")[:2])
    assert ts.texture_cells(row, col, Z) == {a, b}


def make_pkg(tmp_path, source="xp12"):
    root = tmp_path / "scenery" / f"z_ao_stream_{source}"
    for sub in ("terrain", "textures"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    ts.write_marker(str(root), {"source": source, "kind": "ortho", "index": ts.INDEX_NAME})
    return root


def put_dsf(root, cell, data=b"XPLNEDSF"):
    path = root.joinpath(*ts.dsf_rel(*cell).split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_forgetting_a_tile_of_another_set_takes_only_its_own_files(tmp_path):
    a, b, own = (47, -123), (47, -122), (40, 10)
    root = make_pkg(tmp_path)
    for cell in (a, b, own):
        put_dsf(root, cell)
    ts.write_tile_sources(str(root), {a: "xp11", b: "xp11"})
    only_a, shared, own_ter = ter_inside(a), ter_across(a, b), ter_inside(own)
    for name in (only_a, shared, own_ter):
        (root / "terrain" / name).write_text("A\n800\nTERRAIN\n")
    (root / "textures" / mask_for(only_a)).write_bytes(b"png")
    (root / "textures" / mask_for(shared)).write_bytes(b"png")

    files, size = ts.forget_foreign_tiles(str(root), [a, own])

    assert not root.joinpath(*ts.dsf_rel(*a).split("/")).exists()
    assert root.joinpath(*ts.dsf_rel(*own).split("/")).exists()   # its own set: not "foreign"
    assert not (root / "terrain" / only_a).exists()
    assert not (root / "textures" / mask_for(only_a)).exists()
    assert (root / "terrain" / shared).exists()                    # b still needs it
    assert (root / "textures" / mask_for(shared)).exists()
    assert (root / "terrain" / own_ter).exists()                   # not near a
    assert ts.read_tile_sources(str(root)) == {b: "xp11"}
    assert files == 3

    ts.forget_foreign_tiles(str(root), [b])
    assert not (root / "terrain" / shared).exists()
    assert not (root / ts.SOURCES_NAME).exists()


def test_tiles_moved_in_before_the_record_are_found_by_size(tmp_path):
    a, own = (47, -123), (40, 10)
    root = make_pkg(tmp_path)
    put_dsf(root, a, b"XPLNEDSF from another set")
    put_dsf(root, own, b"X" * 9)
    write_index(str(root), [ts.dsf_rel(*a), ts.dsf_rel(*own)])
    # The placeholder index says 1 byte; make the own tile match it.
    put_dsf(root, own, b"X")
    ts.write_pins(str(root), {a, own})
    assert ts.backfill_tile_sources(str(root), ts.read_marker(str(root))) == 0  # no terrain
    (root / "terrain" / ter_inside(a)).write_text("A\n")
    assert ts.backfill_tile_sources(str(root), ts.read_marker(str(root))) == 1
    assert ts.read_tile_sources(str(root)) == {a: "xp11"}
    # Once recorded it isn't guessed again.
    assert ts.backfill_tile_sources(str(root), ts.read_marker(str(root))) == 0


def test_regions_moved_into_another_set_are_recorded(tmp_path):
    ao_dir = str(tmp_path / "z_autoortho" / "scenery")
    os.makedirs(ao_dir)
    pkg = make_streaming(ao_dir, source="xp12")
    make_region(ao_dir, "eur", TILES)
    ra.adopt_regions(ao_dir)
    assert ts.read_tile_sources(pkg) == {c: "xp11" for c in TILES}
    same = str(tmp_path / "other" / "z_autoortho" / "scenery")
    os.makedirs(same)
    pkg11 = make_streaming(same, source="xp11")
    make_region(same, "eur", TILES)
    ra.adopt_regions(same)
    assert ts.read_tile_sources(pkg11) == {}


def test_the_map_knows_each_tiles_set_and_can_switch_it(tmp_path):
    a, own = (47, -123), (40, 10)
    root = make_pkg(tmp_path)
    ao_dir = str(root.parent)
    put_dsf(root, a)
    put_dsf(root, own)
    ts.write_pins(str(root), {a})
    ts.write_tile_sources(str(root), {a: "xp11"})
    (root / "terrain" / ter_inside(a)).write_text("A\n")

    inv = ci.scan(ao_dir, "")
    assert inv.ortho_source == "xp12"
    assert inv.cells[a].mesh_set == "xp11" and inv.cells[own].mesh_set == "xp12"
    assert ci.other_set_cells(inv, [a, own, (1, 1)]) == [a]

    ci.switch_to_current_set(ao_dir, [a])
    assert not root.joinpath(*ts.dsf_rel(*a).split("/")).exists()
    assert not (root / "terrain" / ter_inside(a)).exists()
    assert ts.read_pins(str(root)) == {a}          # still kept: downloads anew
    assert ts.read_tile_sources(str(root)) == {}


def test_deleting_a_tile_of_another_set_takes_its_terrain(tmp_path):
    a = (47, -123)
    root = make_pkg(tmp_path)
    put_dsf(root, a)
    ts.write_tile_sources(str(root), {a: "xp11"})
    (root / "terrain" / ter_inside(a)).write_text("A\n")
    ci.delete_cells(str(root.parent), "", [a], mesh=True, imagery=False)
    assert not (root / "terrain" / ter_inside(a)).exists()
    assert ts.read_tile_sources(str(root)) == {}

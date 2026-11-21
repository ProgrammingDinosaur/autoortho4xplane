"""Tests for seasons on downloaded tiles of the XP11 set."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
TESTS_DIR = os.path.dirname(__file__)
for directory in (ROOT, AUTOORTHO_DIR, os.path.join(ROOT, "scripts"), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import cache_inventory as ci  # noqa: E402
import stream_seasons as ss  # noqa: E402
import tile_stream as ts  # noqa: E402


class FakeResult:
    ADDED = "added"
    XP_TILE_MISSING = "tile_missing"
    FAILED = "failed"


class FakeUtils:
    """Stands in for DsfUtils: appends a marker instead of running DSFTool."""

    def __init__(self, tmp_path, fail=()):
        self.dsf_dir = str(tmp_path / "dsf_cache")
        self.fail = set(fail)
        self.calls = []

    def add_seasons_to_dsf_file(self, path, cache_dir, backup_dir=None):
        from utils.dsf_utils import SeasonsAddResult
        self.calls.append((os.path.basename(path), backup_dir))
        if os.path.basename(path) in self.fail:
            return SeasonsAddResult.FAILED
        with open(path, "ab") as fh:
            fh.write(b"RASTERS")
        return SeasonsAddResult.ADDED


def make_pkg(tmp_path, cells, pins=None, source="xp11"):
    root = tmp_path / "z_autoortho" / "scenery" / f"z_ao_stream_{source}"
    root.mkdir(parents=True)
    ts.write_marker(str(root), {"source": source, "kind": "ortho",
                                "index": ts.INDEX_NAME, "seasons": True})
    for cell in cells:
        path = root.joinpath(*ts.dsf_rel(*cell).split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"XPLNEDSF" + bytes(str(cell), "ascii"))
    ts.write_pins(str(root), set(cells if pins is None else pins))
    return str(root)


def test_pending_tiles_are_kept_and_stored_but_not_patched(tmp_path):
    root = make_pkg(tmp_path, [(40, 1), (40, 2), (41, 1)], pins=[(40, 1), (40, 2), (50, 5)])
    assert ss.pending_tiles(root) == [(40, 1), (40, 2)]  # (50, 5) isn't stored
    utils = FakeUtils(tmp_path)
    assert ss.patch_tile(root, (40, 1), utils) is True
    assert utils.calls == [("+40+001.dsf", None)]  # no backup for streamed tiles
    assert ss.pending_tiles(root) == [(40, 2)]
    assert ss.is_patched(root, (40, 1))


def test_a_tile_downloaded_again_is_pending_again(tmp_path):
    root = make_pkg(tmp_path, [(40, 1)])
    ss.patch_tile(root, (40, 1), FakeUtils(tmp_path))
    path = os.path.join(root, *ts.dsf_rel(40, 1).split("/"))
    with open(path, "wb") as fh:
        fh.write(b"XPLNEDSF fresh download")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert ss.pending_tiles(root) == [(40, 1)]


def test_patch_tiles_counts_failures_and_stops_when_cancelled(tmp_path):
    cells = [(40, 1), (40, 2), (40, 3)]
    root = make_pkg(tmp_path, cells)
    steps = []
    ok, failed = ss.patch_tiles(root, cells, workers=1, utils=FakeUtils(
        tmp_path, fail={"+40+002.dsf"}), progress=lambda *a: steps.append(a))
    assert (ok, failed) == (2, 1)
    assert steps[-1] == (3, 3)
    assert ss.pending_tiles(root) == [(40, 2)]
    utils = FakeUtils(tmp_path)
    assert ss.patch_tiles(root, [(40, 2)], utils=utils, cancelled=lambda: True) == (0, 0)
    assert utils.calls == []


def test_only_the_xp11_set_needs_seasons():
    assert ss.applies({"source": "xp11", "kind": "ortho"})
    assert not ss.applies({"source": "xp12", "kind": "ortho"})
    assert not ss.applies({"source": "xp11", "kind": "overlay"})


class FakeKeepPackage:
    def __init__(self):
        self.downloaded = []

    def entry(self, rel):
        return [0, 0, 1, 1, 0, 0]

    def download_tile(self, lat, lon, cancelled=lambda: False):
        self.downloaded.append((lat, lon))
        return 10, 1

    def shutdown(self):
        pass


@pytest.mark.parametrize("source,seasons,patched", [
    ("xp11", True, True), ("xp11", False, False), ("xp12", True, False)])
def test_download_patches_tiles_when_seasons_is_on(tmp_path, source, seasons, patched):
    ao_dir = str(tmp_path / "scenery")
    root = os.path.join(ao_dir, f"z_ao_stream_{source}")
    os.makedirs(root)
    ts.write_marker(root, {"source": source, "kind": "ortho", "index": ts.INDEX_NAME,
                           **({"seasons": True} if seasons else {})})
    calls = []
    pkg = FakeKeepPackage()
    result = ci.keep_offline(ao_dir, [(40, 1), (40, 2)],
                             package_factory=lambda *a, **k: pkg,
                             seasons_patcher=lambda r, c: calls.append((r, c)) or True)
    assert not result.errors
    assert sorted(c for _r, c in calls) == ([(40, 1), (40, 2)] if patched else [])


def test_a_failing_patch_leaves_the_tile_downloaded(tmp_path):
    ao_dir = str(tmp_path / "scenery")
    root = os.path.join(ao_dir, "z_ao_stream_xp11")
    os.makedirs(root)
    ts.write_marker(root, {"source": "xp11", "kind": "ortho", "index": ts.INDEX_NAME,
                           "seasons": True})

    def boom(_root, _cell):
        raise RuntimeError("DSFTool missing")

    result = ci.keep_offline(ao_dir, [(40, 1)], package_factory=lambda *a, **k: FakeKeepPackage(),
                             seasons_patcher=boom)
    assert not result.errors and result.files == 1


def test_seasons_setting_is_kept_on_refresh(tmp_path, monkeypatch):
    from test_tile_stream_setup import IndexHost, gz_index
    from test_tile_stream import build_release_zip, index_for, make_dsf
    import zipfile
    blob = build_release_zip([("z_g_00/" + ts.dsf_rel(40, 1), make_dsf([]), zipfile.ZIP_STORED)])
    url = "https://github.com/o/r/releases/download/t/z_g_00.zip"
    release = {"index": index_for({url: blob})}
    spec = ts.SOURCES["xp11"]
    host = IndexHost({spec["ortho_index"]: gz_index(release),
                      spec["overlay_index"]: gz_index(release)})
    scenery = str(tmp_path)
    ts.enable_streaming(scenery, "xp11", overlays=False, session=host)
    assert ts.stream_summary(scenery)["seasons"] is False
    assert ts.set_stream_seasons(scenery, True) == "z_ao_stream_xp11"
    ts.enable_streaming(scenery, "xp11", overlays=True, session=host)  # rewrites markers
    assert ts.stream_packages(scenery)["z_ao_stream_xp11"]["seasons"] is True
    assert ts.stream_summary(scenery)["seasons"] is True
    ts.set_stream_seasons(scenery, False)
    assert "seasons" not in ts.stream_packages(scenery)["z_ao_stream_xp11"]


def test_stream_panel_offers_seasons_only_for_the_xp11_set(qt_app):
    from ui.widgets.stream_panel import StreamPanel
    panel = StreamPanel()
    events = []
    panel.seasons_change_requested.connect(events.append)
    assert panel.seasons_check.isHidden()
    base = {"enabled": True, "ortho": (0, 0), "overlay": (0, 0)}
    panel.set_state(dict(base, source="xp12"))
    assert panel.seasons_check.isHidden()
    panel.set_state(dict(base, source="xp11", seasons=False))
    assert not panel.seasons_check.isHidden() and panel.seasons_check.isEnabled()
    panel.seasons_check.click()
    assert events == [True]
    panel.set_state(dict(base, source="xp11", seasons=True))
    assert panel.seasons_check.isChecked()


GLOBAL_RASTERS = (
    "RASTER_DEF elevation\nRASTER_DEF sea_level\nRASTER_DEF soundscape\n"
    "RASTER_DEF spr1\nRASTER_DEF win2\n"
    "RASTER_DATA version=1 bpp=2 g.txt.elevation.raw\n"
    "RASTER_DATA version=1 bpp=2 g.txt.sea_level.raw\n"
    "RASTER_DATA version=1 bpp=1 g.txt.soundscape.raw\n"
    "RASTER_DATA version=1 bpp=1 g.txt.spr1.raw\n"
    "RASTER_DATA version=1 bpp=1 g.txt.win2.raw\n"
)


def test_season_rasters_are_picked_by_name_past_sea_level():
    """X-Plane 12.1+ lists sea_level between elevation and the seasons; the
    old patch stopped there and copied only the elevation definition."""
    from utils.dsf_utils import DsfUtils
    lines = DsfUtils.season_raster_lines(GLOBAL_RASTERS.splitlines(keepends=True))
    defs = [line.split()[1] for line in lines if line.startswith("RASTER_DEF")]
    datas = [line.rsplit(".", 2)[-2] for line in lines if line.startswith("RASTER_DATA")]
    assert defs == ["elevation", "soundscape", "spr1", "win2"]
    assert datas == defs  # each definition keeps its own data


def test_dsf_file_patch_skips_the_backup_when_asked(tmp_path, monkeypatch):
    from utils import dsf_utils
    monkeypatch.setattr(dsf_utils.CFG.seasons, "compress_dsf", False, raising=False)
    utils = object.__new__(dsf_utils.DsfUtils)
    dsf = tmp_path / "pkg" / "Earth nav data" / "+40+000" / "+40+001.dsf"
    dsf.parent.mkdir(parents=True)
    dsf.write_bytes(b"original")
    global_dsf = tmp_path / "global.dsf"
    global_dsf.write_bytes(b"global")
    monkeypatch.setattr(utils, "get_dsf_folder_location", lambda f, n: str(global_dsf),
                        raising=False)

    def to_txt(src, dst):
        with open(dst, "w") as fh:
            fh.write("PROPERTY a b\n" if src == str(dsf) else GLOBAL_RASTERS)
        return True

    def to_dsf(src, dst):
        with open(src) as fh, open(dst, "wb") as out:
            out.write(fh.read().encode())
        return True

    monkeypatch.setattr(utils, "convert_dsf_to_txt", to_txt, raising=False)
    monkeypatch.setattr(utils, "convert_txt_to_dsf", to_dsf, raising=False)
    result = utils.add_seasons_to_dsf_file(str(dsf), str(tmp_path / "cache"), backup_dir=None)
    assert result == dsf_utils.SeasonsAddResult.ADDED
    patched = dsf.read_bytes()
    assert b"RASTER_DEF spr1" in patched and b"g.txt.win2.raw" in patched
    assert b"sea_level" not in patched
    assert not (tmp_path / "pkg" / "dsf_backups").exists()
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("existing,expect_patch", [
    ("RASTER_DEF elevation\n", True),          # left by the old patch: repaired
    ("RASTER_DEF spr1\nRASTER_DATA x.spr1.raw\n", False),  # already has seasons
])
def test_dsf_file_patch_repairs_or_skips(tmp_path, monkeypatch, existing, expect_patch):
    from utils import dsf_utils
    monkeypatch.setattr(dsf_utils.CFG.seasons, "compress_dsf", False, raising=False)
    utils = object.__new__(dsf_utils.DsfUtils)
    dsf = tmp_path / "pkg" / "Earth nav data" / "+40+000" / "+40+001.dsf"
    dsf.parent.mkdir(parents=True)
    dsf.write_bytes(b"original")
    monkeypatch.setattr(utils, "get_dsf_folder_location", lambda f, n: "global",
                        raising=False)

    def to_txt(src, dst):
        with open(dst, "w") as fh:
            fh.write("PROPERTY a b\n" + existing if src == str(dsf) else GLOBAL_RASTERS)
        return True

    def to_dsf(src, dst):
        with open(src) as fh, open(dst, "wb") as out:
            out.write(fh.read().encode())
        return True

    monkeypatch.setattr(utils, "convert_dsf_to_txt", to_txt, raising=False)
    monkeypatch.setattr(utils, "convert_txt_to_dsf", to_dsf, raising=False)
    result = utils.add_seasons_to_dsf_file(str(dsf), str(tmp_path / "cache"))
    assert result == dsf_utils.SeasonsAddResult.ADDED
    patched = dsf.read_bytes()
    if expect_patch:
        assert patched.count(b"RASTER_DEF elevation") == 1 and b"RASTER_DEF spr1" in patched
    else:
        assert patched == b"original"

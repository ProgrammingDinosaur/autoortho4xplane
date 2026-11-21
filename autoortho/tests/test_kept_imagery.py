"""Tests for downloading imagery ahead for kept tiles and keeping it."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
TESTS_DIR = os.path.dirname(__file__)
for directory in (ROOT, AUTOORTHO_DIR, os.path.join(ROOT, "scripts"), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import cache_inventory as ci  # noqa: E402
import kept_imagery as ki  # noqa: E402
import tile_stream as ts  # noqa: E402
from test_tile_stream import make_dsf  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100


def chunk_name(cell, zoom, maptype="BI", dx=0, dy=0):
    """A chunk well inside ``cell`` at ``zoom``."""
    lat, lon = cell
    n = 1 << zoom
    col = int((lon + 0.5 + 180.0) / 360.0 * n) + dx
    row = int(ci.lat_row(lat + 0.5, zoom)) + dy
    return f"{col}_{row}_{zoom}_{maptype}.jpg"


def test_kept_tiles_are_recorded_with_their_highest_zoom(tmp_path):
    cache = str(tmp_path)
    assert ki.read_kept(cache) == {}
    ki.set_kept(cache, [(47, -123), (-34, 18)], 16)
    ki.set_kept(cache, [(47, -123)], 17)
    ki.set_kept(cache, [(-34, 18)], 15)  # a lower zoom doesn't shrink it
    assert ki.read_kept(cache) == {(47, -123): 17, (-34, 18): 16}
    ki.set_kept(cache, [(47, -123)], None)
    assert ki.read_kept(cache) == {(-34, 18): 16}


def test_protector_covers_kept_tiles_up_to_their_zoom(tmp_path):
    cache = str(tmp_path)
    assert ki.protector(cache) is None
    base = lambda name: name.endswith("_12_BI.jpg")  # noqa: E731
    assert ki.protector(cache, base) is base
    ki.set_kept(cache, [(47, -123)], 17)
    protect = ki.protector(cache, base)
    assert protect(chunk_name((47, -123), 16))
    assert protect(chunk_name((47, -123), 17, "EOX"))
    assert not protect(chunk_name((47, -123), 18))
    assert not protect(chunk_name((48, -123), 16))
    assert protect("1_1_12_BI.jpg")           # the base still applies
    assert not protect("notes.txt")


def test_protector_puts_chunks_on_a_degree_line_in_the_right_tile(tmp_path):
    cache = str(tmp_path)
    ki.set_kept(cache, [(47, -123)], 16)
    protect = ki.protector(cache)
    n = 1 << 16
    edge = int((-122.0 + 180.0) / 360.0 * n)   # first column east of the tile
    row = int(ci.lat_row(47.5, 16))
    assert protect(f"{edge - 1}_{row}_16_BI.jpg")
    assert not protect(f"{edge + 1}_{row}_16_BI.jpg")
    assert ki.chunk_cell(edge - 1, row, 16) == ci.chunk_cell(edge - 1, row, 16)


def test_dds_protector_reads_the_tile_folder(tmp_path):
    cache = str(tmp_path)
    assert ki.dds_protector(cache) is None
    ki.set_kept(cache, [(47, -123)], 16)
    protect = ki.dds_protector(cache)
    assert protect(os.path.join(cache, "dds_cache", "+40-130", "+47-123", "BI", "x_z16.dds"))
    assert not protect(os.path.join(cache, "dds_cache", "+40-130", "+48-123", "BI", "x.dds"))


@pytest.mark.parametrize("zoom,count", [(16, 256), (17, 1024), (15, 64), (14, 16)])
def test_texture_chunks_cover_the_texture_at_any_zoom(zoom, count):
    chunks = list(ki.texture_chunks(row=22880, col=10512, tex_zoom=16, zoom=zoom))
    assert len(chunks) == len(set(chunks)) == count
    if zoom == 17:
        assert min(chunks) == (21024, 45760) and max(chunks) == (21055, 45791)


def test_maptype_follows_override_and_custom_map():
    class CustomMap:
        def get_maptype(self, lat, lon):
            return "EOX" if lon > 0 else None

    assert ki.maptype_resolver()(1, 1, "BI", 16) == "BI"
    assert ki.maptype_resolver("Use tile default")(1, 1, "BI", 16) == "BI"
    assert ki.maptype_resolver("ARC")(1, 1, "BI", 16) == "ARC"
    resolve = ki.maptype_resolver("Custom Map", CustomMap())
    east = (100, 70000, "BI", 17)   # col past the meridian at ZL17
    west = (100, 100, "BI", 17)
    assert resolve(*east) == "EOX" and resolve(*west) == "BI"


def make_pkg(tmp_path, cell, terrain):
    root = tmp_path / "z_ao_stream_xp11"
    path = root.joinpath(*ts.dsf_rel(*cell).split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(make_dsf(terrain))
    return str(root)


def test_plan_lists_missing_chunks_of_stored_tiles(tmp_path):
    root = make_pkg(tmp_path, (39, 23), [
        "terrain/100000_125184_BI16.ter",
        "terrain/100000_125184_BI16_sea_overlay.ter",   # same texture
        "terrain/100016_125184_APPLE16.ter",
    ])
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "125184_100000_16_BI.jpg").write_bytes(JPEG)
    plan = ki.plan(root, str(cache), [(39, 23), (10, 10)], 16)
    assert plan.tiles == [(39, 23)]          # (10, 10) isn't stored
    assert plan.cached == 1 and len(plan.chunks) == 255
    assert plan.unsupported == {"APPLE"}
    assert all(z == 16 and m == "BI" for _c, _r, z, m in plan.chunks)


class FakeResponse:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def close(self):
        pass


class FakeSession:
    def __init__(self, answer):
        self.answer = answer
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return self.answer(url)


def test_download_writes_chunks_and_counts_missing_imagery(tmp_path):
    cache = str(tmp_path / "cache")
    chunks = [(1, 1, 16, "BI"), (2, 1, 16, "BI"), (3, 1, 16, "EOX")]

    def answer(url):
        # EOX has no imagery here; Bing has it.
        return FakeResponse(404) if "s2maps" in url else FakeResponse(200, JPEG)

    session = FakeSession(answer)
    steps = []
    result = ki.download(chunks, cache, workers=2, session_factory=lambda: session,
                         progress=lambda *a: steps.append(a))
    assert (result.files, result.no_imagery, result.failed) == (2, 1, 0)
    assert result.bytes == 2 * len(JPEG)
    assert sorted(os.listdir(cache)) == ["1_1_16_BI.jpg", "2_1_16_BI.jpg"]
    assert steps[-1][:2] == (3, 3)


def test_download_rejects_bing_no_tile_and_stops_when_refused(tmp_path):
    cache = str(tmp_path / "cache")
    no_tile = FakeSession(lambda url: FakeResponse(
        200, b"\x89PNG", {"X-VE-Tile-Info": "no-tile"}))
    result = ki.download([(1, 1, 19, "BI")], cache, session_factory=lambda: no_tile)
    assert result.no_imagery == 1 and not os.listdir(cache)

    refused = FakeSession(lambda url: FakeResponse(429))
    result = ki.download([(c, 1, 16, "BI") for c in range(20)], cache, workers=1,
                         session_factory=lambda: refused)
    assert "429" in result.throttled and result.files == 0
    assert len(refused.urls) == 1   # nothing more was asked


def test_deleting_imagery_stops_keeping_it(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / chunk_name((47, -123), 16)).write_bytes(JPEG)
    ki.set_kept(str(cache), [(47, -123), (10, 10)], 16)
    ci.delete_cells("", str(cache), [(47, -123)], mesh=False, imagery=True)
    assert ki.read_kept(str(cache)) == {(10, 10): 16}
    inv = ci.scan("", str(cache))
    assert inv.cells[(10, 10)].imagery_kept == 16


def test_download_imagery_keeps_tiles_only_when_complete(tmp_path, monkeypatch):
    from ui.widgets import cache_map_panel as cmp
    ao_dir = tmp_path / "scenery"
    root = ao_dir / "z_ao_stream_xp11"
    path = root.joinpath(*ts.dsf_rel(39, 23).split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(make_dsf(["terrain/100000_125184_BI16.ter"]))
    ts.write_marker(str(root), {"source": "xp11", "kind": "ortho", "index": ts.INDEX_NAME})
    cache = tmp_path / "cache"
    calls = []

    def fake_download(chunks, cache_dir, progress=None, cancelled=None):
        calls.append(len(chunks))
        return ki.DownloadResult(files=len(chunks), bytes=len(chunks))

    monkeypatch.setattr(ki, "download", fake_download)
    result = cmp.download_imagery(str(ao_dir), str(cache), [(39, 23)], 17, {"override": "EOX"})
    assert calls == [1024] and result.files == 1024
    assert ki.read_kept(str(cache)) == {(39, 23): 17}

    monkeypatch.setattr(ki, "download", lambda *a, **k: ki.DownloadResult(throttled="429"))
    ki.set_kept(str(cache), [(39, 23)], None)
    cmp.download_imagery(str(ao_dir), str(cache), [(39, 23)], 18, {})
    assert ki.read_kept(str(cache)) == {}

"""Phase 1 seam: texture_service (files mode) over getortho's tiles."""

import os
import subprocess
import sys
import textwrap

import pytest

import files_mode_helpers  # noqa: F401
import texture_service as ts

AUTOORTHO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_resolve_source_maptype():
    assert ts.resolve_source_maptype("ZL") == "BI"
    assert ts.resolve_source_maptype("") == "BI"
    assert ts.resolve_source_maptype("zl", "EOX") == "EOX"
    assert ts.resolve_source_maptype("ZL", "Use tile default") == "BI"
    assert ts.resolve_source_maptype("GO2", "EOX") == "GO2"


def test_key_name():
    assert ts.TextureKey(1, 2, "ZL", 18).name == "1_2_ZL18.dds"


@pytest.mark.parametrize("module", ["texture_service", "files_mode.worker", "files_mode.session", "getortho"])
def test_files_mode_never_imports_fuse(module):
    """The files worker must start without WinFsp or macFUSE installed."""
    code = textwrap.dedent(f"""
        import sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split('.')[0] in ('mfusepy', 'refuse', 'fuse', 'autoortho_fuse'):
                    raise ImportError('blocked ' + name)
                if name.endswith('autoortho_fuse') or name.endswith('mfusepy'):
                    raise ImportError('blocked ' + name)
                return None
        sys.meta_path.insert(0, Block())
        sys.path.insert(0, {AUTOORTHO!r})
        import {module}
        bad = [m for m in sys.modules if 'fuse' in m.lower()]
        assert not bad, bad
        print('ok')
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, cwd=AUTOORTHO)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-2000:]


class _FakeTile:
    """A tile whose DDS buffers are filled by get_mipmap, like getortho's."""

    def __init__(self, width, max_zoom):
        import pydds
        self.dds = pydds.DDS(width, width, dxt_format="BC1")
        self.max_mipmap = 4
        self.max_zoom = max_zoom
        self.built = []

    def get_mipmap(self, mip, time_budget=None):
        assert time_budget is not None  # never FUSE's wait band
        self.built.append(mip)
        for mm in self.dds.mipmap_list:
            if mm.idx >= mip and not mm.retrieved:
                mm.write_at(0, bytes([0x20 + mm.idx]) * mm.length, self.dds.blocksize) \
                    if hasattr(mm, "write_at") else None
                mm.retrieved = True
        return True

    def read_dds_bytes(self, offset, length):
        return self.dds.read_at(offset, length)

    def real_from(self):
        return min(self.built) if self.built else 99

    def _recolour(self, data, offset):
        return data


class _FakeCacher:
    max_zoom_mode = "fixed"
    target_zoom_level = 16
    target_zoom_level_near_airports = 18

    def __init__(self):
        self.opened = []
        self.closed = []
        self.tile = None

    def open_tile(self, row, col, maptype, zoom, max_zoom=None):
        self.opened.append((row, col, maptype, zoom, max_zoom))
        width = int(4096 * 2 ** (max_zoom - zoom))
        self.tile = _FakeTile(width, max_zoom)
        return self.tile

    def close_tile(self, row, col, maptype, zoom):
        self.closed.append((row, col, maptype, zoom))


class _Cfg:
    class autoortho:
        maptype_override = ""
        using_custom_tiles = False

    class governor:
        allow_texture_cap = False
        texture_cap_px = 0


def test_build_returns_the_bytes_the_tile_serves(monkeypatch):
    monkeypatch.setattr(ts, "season_key_for", lambda *a: None)
    cacher = _FakeCacher()
    service = ts.TextureService(cacher, cfg=_Cfg)
    demand = ts.Demand(ts.TextureKey(100, 200, "ZL", 16), 2, 0.0, ts.DemandSource.PLANNER,
                       max_zoom=14)
    built = service.build(demand)
    assert cacher.opened == [(100, 200, "BI", 16, 14)]
    assert cacher.closed == [(100, 200, "BI", 16)]
    tile = cacher.tile
    assert built.width == 1024 and min(built.levels) == 2
    assert built.header == tile.dds.read_at(0, 128)
    for mip, data in built.levels.items():
        start, length = built.offsets[mip]
        assert data == tile.read_dds_bytes(start, length)
    assert tile.built == [2, 3, 4]
    assert not built.patchy


def test_target_zoom_rules():
    tc = _FakeCacher()
    assert ts.target_zoom(tc, 16, _Cfg) == 16
    assert ts.target_zoom(tc, 18, _Cfg) == 18
    tc.target_zoom_level = 19
    assert ts.target_zoom(tc, 16, _Cfg) == 17  # never above zoom + 1


@pytest.mark.skipif(not os.environ.get("AO_NETWORK_TESTS"), reason="needs imagery downloads")
def test_equivalence_with_fuse_reads(tmp_path):
    """With real imagery: the service's bytes equal Tile.read_dds_bytes."""
    import getortho
    tc = getortho.TileCacher(str(tmp_path))
    service = ts.TextureService(tc)
    key = ts.TextureKey(3232, 2176, "BI", 13)
    built = service.build(ts.Demand(key, 0, 0.0, ts.DemandSource.PLANNER, max_zoom=13))
    tile = tc.open_tile(3232, 2176, "BI", 13, max_zoom=13)
    try:
        for mip, data in built.levels.items():
            start, length = built.offsets[mip]
            assert data == tile.read_dds_bytes(start, length)
    finally:
        tc.close_tile(3232, 2176, "BI", 13)

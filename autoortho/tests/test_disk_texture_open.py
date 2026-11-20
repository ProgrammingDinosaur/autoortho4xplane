"""getortho.open_disk_texture: finished textures served from the DDS cache."""
import threading
from types import SimpleNamespace

import pytest

from autoortho import getortho
from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
from autoortho.dds_manifest import MipmapSource, RowBuildManifest
from autoortho.disk_texture import mip_layout

ROW, COL, ZOOM = 21728, 34432, 16


class _Cacher:
    """The TileCacher fields open_disk_texture reads."""

    def __init__(self, target_zoom=16):
        self.tc_lock = threading.RLock()
        self.tiles = {}
        self._target = target_zoom

    def _resolve_maptype(self, row, col, map_type, zoom):
        return map_type

    def _get_target_zoom_level(self, zoom, row=None, col=None, count=False):
        return self._target


def _dds_bytes(size_px=4096):
    total = getortho._expected_dds_size(size_px, size_px)
    data = bytearray(total)
    data[:4] = b"DDS "
    for i in range(128, total, 4093):
        data[i] = i & 0xFF
    return bytes(data)


def _manifests(tile_id, dds, size_px=4096):
    rows = size_px // 256
    row_bytes = (size_px * size_px // 16 * 8) // rows
    return {
        row: RowBuildManifest.create(
            tile_id=tile_id, target_zoom=ZOOM, mipmap=0, row_index=row,
            build_generation=1,
            sources=bytes([int(MipmapSource.TARGET_PROVIDER)] * (size_px // 256)),
            compressed_data=dds[128 + row * row_bytes:128 + (row + 1) * row_bytes],
        )
        for row in range(rows)
    }


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho.CFG.pydds, "format", "BC1")
    monkeypatch.setattr(getortho.CFG.pydds, "serve_from_disk", True, raising=False)
    monkeypatch.setattr(getortho, "season_colour_table", lambda *a: None)
    c = DynamicDDSCache(cache_dir=str(tmp_path), max_size_mb=0, enabled=True)
    monkeypatch.setattr(getortho, "dynamic_dds_cache", c)
    yield c
    c.close()


def _store(cache, dds):
    tile_id = f"{ROW}_{COL}_BI_{ZOOM}"
    tile = SimpleNamespace(
        row=ROW, col=COL, maptype="BI", tilename_zoom=ZOOM, max_zoom=ZOOM,
        id=tile_id, cache_dir="",
        dds=SimpleNamespace(
            width=4096, height=4096, mipMapCount=13, total_size=len(dds),
            mipmap_list=list(mip_layout(4096, 4096, "BC1")),
        ),
    )
    assert cache.store(tile_id, ZOOM, dds, tile, mm0_manifest=_manifests(tile_id, dds))


def test_finished_texture_is_served_from_disk(cache):
    dds = _dds_bytes()
    _store(cache, dds)
    texture = getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM)
    assert texture is not None
    try:
        assert texture.size == len(dds)
        assert texture.read(0, 65536) == dds[:65536]
        assert texture.read(128 + 600_000, 300_000) == dds[128 + 600_000:128 + 900_000]
    finally:
        texture.close()


def test_other_zoom_or_open_live_tile_stays_live(cache):
    _store(cache, _dds_bytes())
    assert getortho.open_disk_texture(_Cacher(target_zoom=17), ROW, COL, "BI", ZOOM) is None
    cacher = _Cacher()
    cacher.tiles[f"{ROW}_{COL}_BI_{ZOOM}"] = SimpleNamespace(refs=1)
    assert getortho.open_disk_texture(cacher, ROW, COL, "BI", ZOOM) is None


def test_nothing_cached_is_not_served(cache):
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is None


def test_off_when_serving_from_disk_is_off(cache):
    _store(cache, _dds_bytes())
    cache.serve_from_disk = False
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is None

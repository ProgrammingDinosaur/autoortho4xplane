"""Performance plan, item 4: direct reads of textures finished at the level X-Plane wants."""

import threading
from types import SimpleNamespace

import pytest

from autoortho import disk_texture, getortho
from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
from autoortho.disk_texture import mip_layout

ROW, COL, ZOOM = 21728, 34432, 16
SIZE_PX = 4096


class _Cacher:
    def __init__(self, target_zoom=ZOOM):
        self.tc_lock = threading.RLock()
        self.tiles = {}
        self._target = target_zoom

    def _resolve_maptype(self, row, col, map_type, zoom):
        return map_type

    def _get_target_zoom_level(self, zoom, row=None, col=None, count=False):
        return self._target


def _levels():
    return mip_layout(SIZE_PX, SIZE_PX, "BC1")


def _full_dds():
    levels = _levels()
    data = bytearray(levels[-1].endpos)
    data[:4] = b"DDS "
    data[4:128] = bytes(range(124))
    for level in levels:
        # A distinct byte per level, so a test can tell where bytes came from.
        data[level.startpos:level.endpos] = bytes([0x10 + level.idx]) * level.length
    return bytes(data)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho.CFG.pydds, "format", "BC1")
    monkeypatch.setattr(getortho.CFG.pydds, "serve_from_disk", True, raising=False)
    monkeypatch.setattr(getortho, "season_colour_table", lambda *a: None)
    getortho._partial_direct_stats = getortho._PartialDirectStats()
    c = DynamicDDSCache(cache_dir=str(tmp_path), max_size_mb=0, enabled=True)
    monkeypatch.setattr(getortho, "dynamic_dds_cache", c)
    yield c
    c.close()


def _store_partial(cache, dds, first_level):
    """Levels ``first_level`` to the last, as store_incremental writes them."""
    levels = _levels()
    tile_id = f"{ROW}_{COL}_BI_{ZOOM}"
    data = {lv.idx: dds[lv.startpos:lv.endpos] for lv in levels[first_level:]}
    offsets = {lv.idx: (lv.startpos, lv.length) for lv in levels[first_level:]}
    assert cache.store_incremental(
        tile_id, ZOOM, ROW, COL, "BI", ZOOM, dds[:128], len(dds),
        SIZE_PX, SIZE_PX, len(levels), data, offsets,
    )


def _wants(monkeypatch, level):
    monkeypatch.setattr(getortho, "paging_target_mip", lambda *a, **k: level)


def test_far_texture_is_served_from_its_coarse_levels(cache, monkeypatch):
    dds = _full_dds()
    _store_partial(cache, dds, 3)
    _wants(monkeypatch, 3)
    opened = []
    monkeypatch.setattr(getortho.TileCacher, "_open_tile", lambda *a: opened.append(a), raising=False)
    texture = getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM)
    assert texture is not None
    assert texture.finest_level == 3
    levels = _levels()
    try:
        # X-Plane's header chunk: the real header, stand-in bytes for mm0.
        head = texture.read(0, 65536)
        assert head[:128] == dds[:128]
        assert len(head) == 65536
        assert set(head[128:]) == set(dds[levels[-1].startpos:levels[-1].startpos + 8])
        # Then from the 64 KB chunk holding the start of level 3 to the end.
        start = levels[3].startpos // 65536 * 65536
        tail = texture.read(start, len(dds) - start)
        assert tail[levels[3].startpos - start:] == dds[levels[3].startpos:]
    finally:
        texture.close()
    assert opened == []  # no tile was built


def test_read_of_a_finer_level_falls_back_without_condemning_the_file(cache, monkeypatch):
    dds = _full_dds()
    _store_partial(cache, dds, 3)
    _wants(monkeypatch, 3)
    texture = getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM)
    levels = _levels()
    with pytest.raises(disk_texture.DiskTextureLevelMissing):
        texture.read(levels[2].startpos, levels[2].length)
    assert not texture._file.bad
    texture.close()
    # The entry is still there for the next open.
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is not None


def test_not_served_when_the_wanted_level_is_not_stored(cache, monkeypatch):
    _store_partial(cache, _full_dds(), 4)
    _wants(monkeypatch, 3)
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is None
    _wants(monkeypatch, None)  # no aircraft position: full entries only
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is None


def test_off_switch(cache, monkeypatch):
    _store_partial(cache, _full_dds(), 3)
    _wants(monkeypatch, 3)
    monkeypatch.setattr(getortho.streaming_options, "flag",
                        lambda name: False if name == "partial_direct_reads" else True)
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is None


def test_frequent_fallbacks_ask_for_one_level_finer(cache, monkeypatch):
    stats = getortho._partial_direct_stats
    for _ in range(60):
        stats.note_open()
    for _ in range(10):
        stats.note_fallback()
    _wants(monkeypatch, 3)
    assert getortho._partial_direct_level(ROW, COL, ZOOM, ZOOM) == 2
    _store_partial(cache, _full_dds(), 3)
    assert getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM) is None


def test_partial_counters(cache, monkeypatch):
    bumps = []
    real = getortho.bump
    monkeypatch.setattr(getortho, "bump", lambda name, *a, **k: (bumps.append(name), real(name, *a, **k)))
    _store_partial(cache, _full_dds(), 3)
    _wants(monkeypatch, 3)
    texture = getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM)
    with pytest.raises(disk_texture.DiskTextureFailed):
        texture.read(_levels()[1].startpos, 1000)
    texture.close()
    assert "dds_direct_partial_opens" in bumps
    assert "dds_direct_partial_fallbacks" in bumps


def test_full_entry_still_served_whole(cache, monkeypatch):
    """The full rule is unchanged: a complete entry with level 0 qualifies."""
    from autoortho.tests.test_disk_texture_open import _store as store_full, _dds_bytes

    dds = _dds_bytes()
    store_full(cache, dds)
    _wants(monkeypatch, 3)
    texture = getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM)
    try:
        assert texture.finest_level == 0
        assert texture.read(128, 4096) == dds[128:4224]
    finally:
        texture.close()

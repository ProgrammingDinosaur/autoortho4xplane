"""Direct opens without repeated work (hybrid read path plan, N2 to N4).

* N2: mipmap-0 rows checked once per file, not once per open.
* N3: the cache entry parsed once, kept while its files keep size and time.
* N4: textures whose only holes are chunks the provider has no imagery for
  are served from disk.
"""
import os
import threading
import time
from types import SimpleNamespace

import pytest

from autoortho import chunk_store, disk_texture, getortho
from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
from autoortho.dds_manifest import MipmapSource, RowBuildManifest
from autoortho.disk_texture import mip_layout

ROW, COL, ZOOM = 21728, 34432, 16
TILE_ID = f"{ROW}_{COL}_BI_{ZOOM}"
PX = 4096
ROWS = PX // 256
ROW_BYTES = (PX * PX // 16 * 8) // ROWS


class _Cacher:
    def __init__(self):
        self.tc_lock = threading.RLock()
        self.tiles = {}

    def _resolve_maptype(self, row, col, map_type, zoom):
        return map_type

    def _get_target_zoom_level(self, zoom, row=None, col=None, count=False):
        return ZOOM


def _dds_bytes(seed=1):
    total = getortho._expected_dds_size(PX, PX)
    data = bytearray(total)
    data[:4] = b"DDS "
    for i in range(128, total, 4093):
        data[i] = (i * seed) & 0xFF
    return bytes(data)


def _manifests(dds, fallback=(), missing=()):
    fallback, missing = set(fallback), set(missing)
    out = {}
    for row in range(ROWS):
        sources = []
        for column in range(ROWS):
            index = row * ROWS + column
            if index in missing:
                sources.append(int(MipmapSource.MISSING_COLOR))
            elif index in fallback:
                sources.append(int(MipmapSource.LOWER_ZL_CACHE))
            else:
                sources.append(int(MipmapSource.TARGET_PROVIDER))
        out[row] = RowBuildManifest.create(
            tile_id=TILE_ID, target_zoom=ZOOM, mipmap=0, row_index=row,
            build_generation=1, sources=bytes(sources),
            compressed_data=dds[128 + row * ROW_BYTES:128 + (row + 1) * ROW_BYTES],
        )
    return out


def _tile():
    return SimpleNamespace(
        row=ROW, col=COL, maptype="BI", tilename_zoom=ZOOM, max_zoom=ZOOM,
        id=TILE_ID, cache_dir="",
        dds=SimpleNamespace(
            width=PX, height=PX, mipMapCount=13,
            total_size=getortho._expected_dds_size(PX, PX),
            mipmap_list=list(mip_layout(PX, PX, "BC1")),
        ),
    )


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho.CFG.pydds, "format", "BC1")
    monkeypatch.setattr(getortho.CFG.pydds, "serve_from_disk", True, raising=False)
    monkeypatch.setattr(getortho, "season_colour_table", lambda *a: None)
    counts = {}

    def bump(name, n=1):
        counts[name] = counts.get(name, 0) + n

    monkeypatch.setattr(getortho, "bump", bump)
    disk_texture.forget_verified()
    c = DynamicDDSCache(cache_dir=str(tmp_path), max_size_mb=0, enabled=True)
    c.counts = counts
    monkeypatch.setattr(getortho, "dynamic_dds_cache", c)
    yield c
    c.close()
    disk_texture.forget_verified()


def _open():
    return getortho.open_disk_texture(_Cacher(), ROW, COL, "BI", ZOOM)


def _read_level0(texture):
    total = texture.size
    pos = 0
    out = []
    while pos < total:
        out.append(texture.read(pos, min(65536, total - pos)))
        pos += 65536
    return b"".join(out)


# ---------------------------------------------------------------- N2

def test_rows_are_checked_once_per_file(cache):
    dds = _dds_bytes()
    assert cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    disk_texture.forget_verified()  # as in a later session
    texture = _open()
    assert _read_level0(texture) == dds
    texture.close()
    assert cache.counts["dds_direct_rows_checked"] == ROWS
    assert disk_texture.open_files() == 0  # the file handle was closed

    texture = _open()
    assert _read_level0(texture) == dds
    texture.close()
    assert cache.counts["dds_direct_rows_checked"] == ROWS, "not checked again"


def test_a_rewritten_file_is_checked_again(cache):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    disk_texture.forget_verified()  # as in a later session
    texture = _open()
    _read_level0(texture)
    texture.close()
    time.sleep(0.02)
    other = _dds_bytes(seed=3)
    cache.store(TILE_ID, ZOOM, other, _tile(), mm0_manifest=_manifests(other))
    disk_texture.forget_verified()  # as in a later session
    texture = _open()
    assert _read_level0(texture) == other
    texture.close()
    assert cache.counts["dds_direct_rows_checked"] == 2 * ROWS


def test_a_failed_check_is_not_remembered_as_good(cache):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    disk_texture.forget_verified()  # as in a later session
    texture = _open()
    path = texture._file.path
    stat = os.stat(path)
    with open(path, "r+b") as f:  # same size, same time: only the check sees it
        f.seek(128 + 5 * ROW_BYTES + 10)
        f.write(b"\xff\xff")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    texture.read(0, 65536)  # row 0 is fine
    with pytest.raises(disk_texture.DiskTextureFailed):
        texture.read(128 + 5 * ROW_BYTES, 65536)
    texture.close()
    assert not disk_texture._verified
    assert not os.path.exists(path), "the bad entry is dropped"


# ---------------------------------------------------------------- N3

def test_open_reads_the_sidecar_once(cache, monkeypatch):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    reads = []
    real = DynamicDDSCache._read_ddm
    monkeypatch.setattr(DynamicDDSCache, "_read_ddm",
                        staticmethod(lambda p: reads.append(p) or real(p)))
    for _ in range(5):
        texture = _open()
        assert texture is not None
        texture.read(texture.size - 4096, 4096)  # a far read: no row check
        texture.close()
    assert len(reads) == 1


def test_a_rewritten_sidecar_is_read_again(cache):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    _open().close()
    _dds_path, ddm_path = cache._paths_for(ROW, COL, "BI", ZOOM, ZOOM)
    meta = cache._read_ddm(ddm_path)
    meta["populated_mipmaps"] = [m for m in meta["populated_mipmaps"] if m != 0]
    meta["mipmaps"][0]["complete"] = False
    time.sleep(0.02)
    cache._write_ddm(ddm_path, meta)
    # Mipmap 0 is no longer servable. With no aircraft position the entry is
    # still served, from the levels it has left (ready share plan, K3).
    texture = _open()
    try:
        assert texture.finest_level == 1
    finally:
        texture.close()


def test_sidecar_rewritten_during_an_open_hands_the_read_to_the_live_path(cache):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    disk_texture.forget_verified()  # as in a later session
    texture = _open()
    _dds_path, ddm_path = cache._paths_for(ROW, COL, "BI", ZOOM, ZOOM)
    meta = cache._read_ddm(ddm_path)
    meta["built"] = meta.get("built", 0) + 1
    time.sleep(0.02)
    cache._write_ddm(ddm_path, meta)
    with pytest.raises(disk_texture.DiskTextureStale):
        texture.read(0, 65536)
    path = texture._file.path
    texture.close()
    assert os.path.exists(path), "not a fault: the entry stays"


def test_deleted_entry_is_not_served_from_memory(cache):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    _open().close()
    assert cache.invalidate(TILE_ID, ZOOM)
    assert _open() is None


# ---------------------------------------------------------------- N4

@pytest.fixture
def store_on(monkeypatch):
    previous = chunk_store.enabled()
    chunk_store.close_all()
    chunk_store.configure(True)
    yield
    chunk_store.close_all()
    chunk_store.configure(previous)


def _chunk_path(cache, index):
    return os.path.join(
        cache._cache_dir,
        f"{COL + index % ROWS}_{ROW + index // ROWS}_{ZOOM}_BI.jpg",
    )


def _remember_no_imagery(cache, indices):
    for index in indices:
        assert chunk_store.mark_no_imagery_path(_chunk_path(cache, index))
    chunk_store.flush_all()


def test_no_imagery_holes_are_served_from_disk(cache, store_on):
    sea = [3, 4, 40, 255]
    dds = _dds_bytes()
    assert cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds, fallback=sea))
    assert _open() is None, "nothing remembered: a failed download looks the same"
    _remember_no_imagery(cache, sea)
    # The sidecar is unchanged: drop the parsed entry as a new session would.
    cache._direct_memo.clear()
    texture = _open()
    assert texture is not None
    try:
        assert _read_level0(texture) == dds
    finally:
        texture.close()
    assert cache.counts["dds_direct_no_imagery_opens"] == 1


def test_one_unexplained_hole_keeps_the_texture_live(cache, store_on):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds, fallback=[3, 4, 5]))
    _remember_no_imagery(cache, [3, 4])
    assert _open() is None


def test_missing_colour_keeps_the_texture_live(cache, store_on):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(),
                mm0_manifest=_manifests(dds, fallback=[3], missing=[4]))
    _remember_no_imagery(cache, [3, 4])
    assert _open() is None


def test_without_the_chunk_store_holes_stay_live(cache):
    dds = _dds_bytes()
    cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds, fallback=[3]))
    assert _open() is None


def test_real_only_store_keeps_no_imagery_holes(cache, store_on):
    """With only real imagery persisted, a mipmap 0 whose holes are all
    no-imagery fallbacks is stored all the same, and served from disk."""
    cache.real_only = True
    sea = [7, 8]
    dds = _dds_bytes()
    assert not cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds, fallback=sea))
    _remember_no_imagery(cache, sea)
    assert cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds, fallback=sea))
    texture = _open()
    assert texture is not None
    try:
        assert _read_level0(texture) == dds
    finally:
        texture.close()
    # A hole the provider may still have imagery for is not stored.
    assert not cache.store(TILE_ID, ZOOM, dds, _tile(),
                           mm0_manifest=_manifests(dds, fallback=sea + [9]))


def test_a_live_tile_with_only_no_imagery_holes_may_persist(tmp_path, store_on, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    tile = getortho.Tile(COL, ROW, "BI", ZOOM, cache_dir=str(tmp_path), max_zoom=ZOOM)
    try:
        tile._dds_missing_indices = []
        tile._dds_fallback_indices = [3, 20]
        assert not tile._holes_are_no_imagery()
        for index in (3, 20):
            col, row = tile._max_zoom_chunk_coords(index)
            assert chunk_store.mark_no_imagery_path(
                os.path.join(str(tmp_path), f"{col}_{row}_{ZOOM}_BI.jpg"))
        chunk_store.flush_all()
        tile._no_imagery_check = None  # skip the 5 s retry wait
        assert tile._holes_are_no_imagery()
        tile._dds_missing_indices = [4]
        assert not tile._holes_are_no_imagery()
    finally:
        tile.close()


# ---------------------------------------------------------------- S3: rows

def test_rows_stored_by_this_process_are_not_checked_again(cache):
    """The cache checked the bytes against their manifests when it stored
    them: the reads of that file need no check (hybrid read path plan, S3)."""
    dds = _dds_bytes()
    assert cache.store(TILE_ID, ZOOM, dds, _tile(), mm0_manifest=_manifests(dds))
    texture = _open()
    assert _read_level0(texture) == dds
    texture.close()
    assert cache.counts.get("dds_direct_rows_checked", 0) == 0

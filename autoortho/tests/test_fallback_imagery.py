"""A read that can't wait gets the tile's own bytes (Tile.standin_bytes).

X-Plane keeps what it reads first, so a fallback serves the tile's real
header and its coarser built levels upscaled, not flat missing_color.
"""

import threading

import pytest

from autoortho import getortho

RED = (0xF800).to_bytes(2, "little") * 2 + b"\0" * 4


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    yield t
    t.close()


def test_header_is_the_tiles_real_header(tile):
    assert tile.standin_bytes(0, 128) == tile.dds.read_at(0, 128)
    assert tile.standin_bytes(0, 4) == b"DDS "


def test_unbuilt_level_is_upscaled_from_a_built_coarser_one(tile):
    dds = tile.dds
    m4 = dds.mipmap_list[4]
    dds.write_mipmap_at(4, 0, RED * (m4.length // len(RED)))
    m3 = dds.mipmap_list[3]
    served = tile.standin_bytes(m3.startpos, 64)
    flat = dds.read_at(m3.startpos, 64)
    assert served != flat                  # not missing color
    assert served[:4] == RED[:4]           # the coarser level's colour


def test_nothing_built_gives_missing_color_with_a_valid_header(tile):
    data = tile.standin_bytes(0, 4096)
    assert data[:4] == b"DDS "
    assert data == tile.dds.read_at(0, 4096)


def test_does_not_wait_behind_a_busy_tile_lock(tile, monkeypatch):
    monkeypatch.setattr(getortho, "STANDIN_LOCK_WAIT_S", 0.05)
    held = threading.Event()
    release = threading.Event()

    def hold():
        with tile._lock:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    try:
        assert tile.standin_bytes(0, 128) is None
    finally:
        release.set()
        thread.join()

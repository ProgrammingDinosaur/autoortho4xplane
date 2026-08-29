import concurrent.futures
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import getortho  # noqa: E402


class FakeImage:
    def __init__(self, width, height):
        self._width = width
        self._height = height
        self.close_count = 0

    def close(self):
        self.close_count += 1


def make_tile(image_limit_mb=96):
    tile = getortho.Tile.__new__(getortho.Tile)
    tile.imgs = {}
    tile._lock = threading.RLock()
    tile._imgs_order = []
    tile._img_sizes = {}
    tile._cached_image_bytes = 0
    tile._image_cache_limit_bytes = image_limit_mb * 1048576
    tile._last_collected_jpegs = None
    tile._last_collected_ratio = None
    tile._last_collected_missing = None
    tile._dds_needs_healing = False
    tile._dds_missing_indices = []
    tile._dds_fallback_indices = []
    tile._dds_persisted = True
    tile.max_zoom = 17
    tile.refs = 0
    tile.chunks = {}
    tile.dds = SimpleNamespace(
        mipmap_list=[SimpleNamespace(retrieved=True)]
    )
    return tile


def test_zl17_mipmap_zero_is_not_retained():
    tile = make_tile()
    image = FakeImage(8192, 8192)

    retained = tile._cache_image(0, (image, 0, 0, 17))

    assert retained is False
    assert tile.imgs == {}
    assert tile._cached_image_bytes == 0
    assert image.close_count == 0


def test_fallback_images_are_bounded_without_closing_live_entries():
    tile = make_tile(image_limit_mb=80)
    large = FakeImage(4096, 4096)  # 64 MiB
    small = FakeImage(2048, 2048)  # 16 MiB
    replacement = FakeImage(4096, 4096)

    assert tile._cache_image(1, (large, 0, 0, 16))
    assert tile._cache_image(2, (small, 0, 0, 15))
    assert tile._cached_image_bytes == 80 * 1048576

    assert not tile._cache_image(3, (replacement, 0, 0, 14))
    assert tile._cached_image_bytes <= 80 * 1048576
    assert large.close_count == 0
    assert small.close_count == 0
    assert list(tile.imgs) == [1, 2]


def test_completed_tile_releases_images_and_jpegs():
    tile = make_tile()
    image = FakeImage(2048, 2048)
    chunk = SimpleNamespace(data=b"jpeg", ready=threading.Event())
    chunk.ready.set()
    tile.imgs = {2: (image, 0, 0, 15)}
    tile._imgs_order = [2]
    tile._img_sizes = {2: 16 * 1048576}
    tile._cached_image_bytes = 16 * 1048576
    tile._last_collected_jpegs = [b"jpeg"]
    tile._last_collected_ratio = 1.0
    tile._last_collected_missing = []
    tile.chunks = {17: [chunk]}

    tile._release_completed_sources()

    assert image.close_count == 1
    assert tile.imgs == {}
    assert tile._cached_image_bytes == 0
    assert tile._last_collected_jpegs is None
    assert chunk.data is None


def test_referenced_completed_tile_defers_image_release():
    tile = make_tile()
    image = FakeImage(2048, 2048)
    chunk = SimpleNamespace(data=b"jpeg", ready=threading.Event())
    chunk.ready.set()
    tile.refs = 1
    tile.imgs = {2: (image, 0, 0, 15)}
    tile._imgs_order = [2]
    tile._img_sizes = {2: 16 * 1048576}
    tile._cached_image_bytes = 16 * 1048576
    tile.chunks = {17: [chunk]}

    assert tile._release_completed_sources() is False
    assert image.close_count == 0
    assert tile.imgs


def test_healing_tile_keeps_source_data():
    tile = make_tile()
    image = FakeImage(2048, 2048)
    chunk = SimpleNamespace(data=b"jpeg", ready=threading.Event())
    chunk.ready.set()
    tile.imgs = {2: (image, 0, 0, 15)}
    tile._imgs_order = [2]
    tile._img_sizes = {2: 16 * 1048576}
    tile._cached_image_bytes = 16 * 1048576
    tile.chunks = {17: [chunk]}
    tile._dds_needs_healing = True

    tile._release_completed_sources()

    assert image.close_count == 0
    assert chunk.data == b"jpeg"


def test_cache_write_queue_is_bounded_by_bytes(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def blocked_write(_payload):
        started.set()
        release.wait(timeout=5.0)

    monkeypatch.setattr(getortho, "_cache_write_limit_bytes", lambda: 10)
    with getortho._cache_write_pending_lock:
        getortho._cache_write_pending_bytes = 0

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        assert getortho._submit_bounded_cache_write(
            executor,
            blocked_write,
            b"12345678",
            byte_count=8,
            kind="local",
        )
        assert started.wait(timeout=2.0)
        assert not getortho._submit_bounded_cache_write(
            executor,
            blocked_write,
            b"abcdefgh",
            byte_count=8,
            kind="local",
        )
    finally:
        release.set()
        executor.shutdown(wait=True)

    deadline = time.monotonic() + 2.0
    while getortho._cache_write_pending_bytes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert getortho._cache_write_pending_bytes == 0

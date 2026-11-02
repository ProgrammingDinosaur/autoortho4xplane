"""Lower-zoom fallback lookups: zoom, crop position and source order."""

import pytest

from autoortho.aoimage import AoImage
from autoortho.aopipeline.fallback_resolver import FallbackResolver

RED = (220, 20, 20)
BLUE = (20, 20, 220)


@pytest.fixture
def split_jpeg(tmp_path_factory):
    """256x256 JPEG: left half red, right half blue."""
    img = AoImage.new("RGBA", (256, 256), RED)
    img.paste(AoImage.new("RGBA", (128, 256), BLUE), (128, 0))
    target = tmp_path_factory.mktemp("src") / "split.jpg"
    img.write_jpg(str(target), quality=95)
    return target.read_bytes()


def _centre_colour(img):
    data = img.tobytes()
    offset = (128 * 256 + 128) * 4
    return tuple(data[offset:offset + 3])


def _close_to(colour, expected, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(colour, expected))


def _resolver(tmp_path, **kwargs):
    return FallbackResolver(
        cache_dir=str(tmp_path),
        maptype="BI",
        tile_col=0,
        tile_row=0,
        tile_zoom=17,
        fallback_level=1,
        max_mipmap=5,
        **kwargs,
    )


@pytest.mark.parametrize("diff", [1, 2, 5])
def test_lower_zoom_crop_matches_child_position(tmp_path, diff, split_jpeg):
    col, row, zoom = 1000, 2000, 17
    parent = (col >> diff, row >> diff, zoom - diff)
    (tmp_path / f"{parent[0]}_{parent[1]}_{parent[2]}_BI.jpg").write_bytes(
        split_jpeg
    )
    resolver = _resolver(tmp_path)
    scale = 1 << diff
    left_child = (col >> diff) << diff  # first child column of the parent
    right_child = left_child + scale - 1

    left = resolver.lower_zoom_image(left_child, row, zoom)
    right = resolver.lower_zoom_image(right_child, row, zoom)
    try:
        assert left.size == right.size == (256, 256)
        assert _close_to(_centre_colour(left), RED)
        assert _close_to(_centre_colour(right), BLUE)
    finally:
        left.close()
        right.close()
        resolver.close()


def test_lower_zoom_for_lower_mipmap_uses_parent_zoom(tmp_path, split_jpeg):
    # A mipmap-1 chunk at ZL16: its parent one level down is ZL15, not ZL14.
    col, row = 500, 900
    (tmp_path / f"{col >> 1}_{row >> 1}_15_BI.jpg").write_bytes(split_jpeg)
    resolver = _resolver(tmp_path)
    img = resolver.lower_zoom_image(col, row, 16, target_mipmap=1)
    try:
        assert img is not None
    finally:
        img.close()
        resolver.close()


def test_memory_chunk_is_used_before_disk(tmp_path, split_jpeg):
    jpeg = split_jpeg
    requested = []

    def memory(col, row, zoom):
        requested.append((col, row, zoom))
        return jpeg

    resolver = _resolver(tmp_path, memory_reader=memory)
    img = resolver.lower_zoom_image(1000, 2000, 17)
    try:
        assert img is not None
        assert requested == [(500, 1000, 16)]
    finally:
        img.close()
        resolver.close()


def test_secondary_reader_used_when_local_cache_misses(tmp_path, split_jpeg):
    jpeg = split_jpeg
    resolver = _resolver(
        tmp_path,
        secondary_reader=lambda col, row, zoom: jpeg if zoom == 14 else None,
    )
    img = resolver.lower_zoom_image(1000, 2000, 17)
    try:
        assert img is not None
    finally:
        img.close()
        resolver.close()


def test_neighbouring_chunks_decode_parent_once(tmp_path, split_jpeg):
    jpeg = split_jpeg
    reads = []

    def memory(col, row, zoom):
        reads.append((col, row, zoom))
        return jpeg

    resolver = _resolver(tmp_path, memory_reader=memory)
    images = [resolver.lower_zoom_image(1000 + i, 2000, 17) for i in range(2)]
    try:
        assert all(img is not None for img in images)
        assert reads == [(500, 1000, 16)]
    finally:
        for img in images:
            img.close()
        resolver.close()


def test_non_jpeg_cache_file_is_ignored(tmp_path, split_jpeg):
    (tmp_path / "500_1000_16_BI.jpg").write_bytes(b"\x89PNG" + b"\x00" * 200)
    resolver = _resolver(tmp_path)
    assert resolver.lower_zoom_image(1000, 2000, 17) is None
    resolver.close()


def test_lower_mipmap_image_is_upscaled(tmp_path, split_jpeg):
    # Tile of 4x4 chunks at ZL17 starting at (1000, 2000). Its mipmap 2 is a
    # 256x256 image: left half red, right half blue.
    mm2 = AoImage.new("RGBA", (256, 256), RED)
    mm2.paste(AoImage.new("RGBA", (128, 256), BLUE), (128, 0))
    resolver = _resolver(tmp_path, grid_origin=(1000, 2000))
    resolver.set_mipmap_images({2: (mm2, 250, 500, 15)})

    left = resolver.lower_zoom_image(1000, 2000, 17)
    right = resolver.lower_zoom_image(1003, 2000, 17)
    try:
        assert _close_to(_centre_colour(left), RED, tolerance=5)
        assert _close_to(_centre_colour(right), BLUE, tolerance=5)
    finally:
        left.close()
        right.close()
        resolver.close()

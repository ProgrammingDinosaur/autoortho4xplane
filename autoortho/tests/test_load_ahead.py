"""Item 4: textures are warmed as X-Plane opens their .ter files."""

import math
import re

from autoortho import getortho, load_ahead


def _rc(lat, lon, zoom=16):
    n = 2 ** zoom
    col = int((lon + 180) / 360 * n) // 16 * 16
    row = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n) // 16 * 16
    return row, col


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_nearest_to_latest_request_goes_first():
    warmed = []
    clock = Clock()
    queue = load_ahead.LoadAhead(
        lambda *t: warmed.append(t) or True, aircraft=lambda: (0.0, 0.0), clock=clock
    )
    far = _rc(47.0, 9.0)
    near = _rc(47.0, 8.2)
    queue.note_terrain(*far, "BI", 16)
    queue.note_terrain(*near, "BI", 16)
    queue.note_texture_request(*_rc(47.0, 8.0), 16)

    queue.run_once()
    assert warmed[0][:2] == near


def test_aircraft_is_focus_when_no_recent_request():
    warmed = []
    clock = Clock()
    queue = load_ahead.LoadAhead(
        lambda *t: warmed.append(t) or True, aircraft=lambda: (47.0, 9.0), clock=clock
    )
    queue.note_texture_request(*_rc(47.0, 8.0), 16)
    clock.now += load_ahead.REQUEST_FOCUS_SECONDS + 1
    queue.note_terrain(*_rc(47.0, 8.05), "BI", 16)
    queue.note_terrain(*_rc(47.0, 9.0), "BI", 16)

    queue.run_once()
    assert warmed[0][:2] == _rc(47.0, 9.0)


def test_requested_texture_leaves_the_queue():
    queue = load_ahead.LoadAhead(lambda *t: True)
    rc = _rc(47.0, 8.0)
    queue.note_terrain(*rc, "BI", 16)
    queue.note_texture_request(*rc, 16)
    assert queue.waiting() == 0


def test_no_capacity_keeps_texture_waiting():
    queue = load_ahead.LoadAhead(lambda *t: False)
    queue.note_terrain(*_rc(47.0, 8.0), "BI", 16)
    assert queue.run_once() is False
    assert queue.waiting() == 1


def test_recently_warmed_texture_is_not_queued_again():
    clock = Clock()
    queue = load_ahead.LoadAhead(lambda *t: True, clock=clock)
    rc = _rc(47.0, 8.0)
    queue.note_terrain(*rc, "BI", 16)
    queue.run_once()
    queue.note_terrain(*rc, "BI", 16)
    assert queue.waiting() == 0
    clock.now += load_ahead.REWARM_SECONDS + 1
    queue.note_terrain(*rc, "BI", 16)
    assert queue.waiting() == 1


def test_ter_names_parse_like_fuse():
    pattern = re.compile(
        r".*/(\d+)[-_](\d+)[-_](\S*?)(\d{2})(?:_(?:sea|water)_overlay)?\.ter$"
    )
    assert pattern.match("/terrain/22880_34320_BI16.ter").groups() == ("22880", "34320", "BI", "16")
    assert pattern.match("/terrain/22880_34320_GO216.ter").groups()[2:] == ("GO2", "16")
    assert pattern.match("/terrain/22880_34320_BI16_sea_overlay.ter").groups()[2:] == ("BI", "16")


def test_warm_uses_base_level_without_position(monkeypatch):
    calls = []

    class Cacher:
        def is_tile_opened_by_xplane(self, *a):
            return False

        def _get_target_zoom_level(self, zoom, row=None, col=None):
            return zoom

    monkeypatch.setattr(getortho, "is_prefetch_runtime_allowed", lambda: True)
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    monkeypatch.setattr(getortho.spatial_prefetcher, "_tile_cacher", Cacher())
    monkeypatch.setattr(
        getortho.spatial_prefetcher,
        "_prefetch_tile",
        lambda row, col, zoom, maptype=None, max_chunks=None, first_mip=0, source="": (
            calls.append((first_mip, source)) or (1, True)
        ),
    )
    assert getortho.load_ahead_warm(0, 0, "BI", 16)
    assert calls == [(4, "ter")]


def test_tile_index_follows_the_queue():
    """A texture request drops its waiting entries through the tile index,
    which stays in step with queue, take and requeue."""
    queue = load_ahead.LoadAhead(lambda *a: True)
    queue.note_terrain(10, 20, "BI", 16)
    queue.note_terrain(10, 20, "GO2", 16)
    queue.note_terrain(10, 36, "BI", 16)
    queue.note_terrain(10, 20, "BI", 17)
    queue.note_texture_request(10, 20, 16)
    assert set(queue._waiting) == {(10, 36, "BI", 16), (10, 20, "BI", 17)}
    entry = queue.take_next()
    queue.requeue(entry)
    queue.requeue(entry)  # twice: still one entry
    assert queue.waiting() == 2
    while queue.take_next() is not None:
        pass
    assert queue._by_tile == {} and queue._cells == {}
    queue.note_terrain(1, 2, "BI", 16)
    queue.note_texture_request(1, 2, 16)
    assert queue.waiting() == 0 and queue._by_tile == {} and queue._cells == {}
    assert queue.max_waiting == 4


def test_grid_search_takes_the_nearest_like_a_full_scan():
    """take_next through the grid returns a texture as near as a scan of the
    whole queue would, wherever the focus is."""
    import random

    rng = random.Random(7)
    focus = [None]
    queue = load_ahead.LoadAhead(lambda *a: True, aircraft=lambda: focus[0])
    for _ in range(1500):
        queue.note_terrain(
            rng.randrange(17000, 19000) // 16 * 16,
            rng.randrange(30000, 33000) // 16 * 16,
            rng.choice(("BI", "GO2")), rng.choice((15, 16, 17)),
        )

    def distance(entry, f):
        cos_lat = math.cos(math.radians(f[0]))
        return (entry[4] - f[0]) ** 2 + ((entry[5] - f[1]) * cos_lat) ** 2

    for i in range(300):
        if i % 50 == 0:  # sometimes far from everything waiting
            focus[0] = (rng.uniform(-60, 80), rng.uniform(-170, 170))
        else:
            focus[0] = (rng.uniform(55, 70), rng.uniform(-20, 5))
        expected = min(distance(e, focus[0]) for e in queue._waiting.values())
        entry = queue.take_next()
        assert distance(entry, focus[0]) == expected
        if i % 3 == 0:
            queue.requeue(entry)
    assert sum(len(keys) for keys in queue._cells.values()) == queue.waiting()

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

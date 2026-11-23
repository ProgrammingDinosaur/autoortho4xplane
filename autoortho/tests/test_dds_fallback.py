#!/usr/bin/env python3
"""Placeholder textures served when a tile read fails (dds_fallback.py).

X-Plane rejects a DDS with an invalid header and keeps that terrain white
until the scenery reloads, so every placeholder must start with a header
X-Plane accepts, for the size the file was reported to have.
"""

import struct
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTOORTHO_DIR = ROOT / "autoortho"
for path in (str(ROOT), str(AUTOORTHO_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import dds_fallback  # noqa: E402
import debug_faults  # noqa: E402
import pydds  # noqa: E402
from aoconfig import CFG  # noqa: E402

H = dds_fallback.DDS_HEADER_SIZE
SIZE_4K_BC1 = 11184952
SIZE_8K_BC1 = 44739384
OLIVE = (66, 77, 55)


def parse_header(data):
    (magic, size, flags, height, width, linear, _depth, mips) = struct.unpack_from("<4s7I", data)
    four_cc = data[84:88]
    return dict(magic=magic, size=size, flags=flags, height=height, width=width,
                linear=linear, mips=mips, four_cc=four_cc)


# ---------------------------------------------------------------- header


@pytest.mark.parametrize("fmt", ["BC1", "BC3"])
@pytest.mark.parametrize("side", [256, 1024, 4096, 8192, 16384])
def test_header_matches_the_one_pydds_writes(fmt, side):
    real = pydds.DDS(side, side, dxt_format=fmt)
    assert dds_fallback.dds_header(side, side, fmt) == bytes(real)[:H]
    assert dds_fallback.dds_size(side, side, fmt) == real.total_size


@pytest.mark.parametrize("fmt,size,side", [
    ("BC1", SIZE_4K_BC1, 4096),
    ("BC1", SIZE_8K_BC1, 8192),
    ("BC3", 2 * SIZE_4K_BC1 - 128 + 16, None),  # not a real DDS size
])
def test_side_for_size(fmt, size, side):
    assert dds_fallback.side_for_size(size, fmt) == side


@pytest.mark.parametrize("fmt", ["BC1", "BC3"])
def test_header_for_size_names_the_reported_size(fmt):
    size = dds_fallback.dds_size(8192, 8192, fmt)
    header = parse_header(dds_fallback.header_for_size(size, fmt))
    assert header["magic"] == b"DDS "
    assert header["size"] == 124
    assert (header["width"], header["height"]) == (8192, 8192)
    assert header["mips"] == 14
    assert header["four_cc"] == (b"DXT5" if fmt == "BC3" else b"DXT1")


def test_header_never_promises_more_than_the_file_has():
    odd = SIZE_8K_BC1 - 1  # fits no square texture
    header = parse_header(dds_fallback.header_for_size(odd, "BC1"))
    assert header["magic"] == b"DDS "
    assert dds_fallback.dds_size(header["width"], header["height"], "BC1") <= odd
    assert header["width"] == 4096


def test_header_without_a_size_is_still_valid():
    header = parse_header(dds_fallback.header_for_size(None, "BC1"))
    assert header["magic"] == b"DDS " and header["width"] == 4096


# ---------------------------------------------------------------- bytes


def _whole(fmt, side=1024):
    size = dds_fallback.dds_size(side, side, fmt)
    header = dds_fallback.dds_header(side, side, fmt)
    return dds_fallback.fallback_bytes(0, size, header, fmt, color=OLIVE), size


@pytest.mark.parametrize("fmt", ["BC1", "BC3"])
def test_whole_placeholder_is_header_then_solid_blocks(fmt):
    data, size = _whole(fmt)
    assert len(data) == size
    assert data[:4] == b"DDS "
    block = dds_fallback.missing_block(fmt, OLIVE)
    body = data[H:]
    assert len(body) % len(block) == 0
    assert body == block * (len(body) // len(block))


def test_missing_color_blocks():
    c = ((66 >> 3) << 11 | (77 >> 2) << 5 | (55 >> 3)).to_bytes(2, "little")
    assert dds_fallback.missing_block("BC1", OLIVE) == c + c + b"\0\0\0\0"
    bc3 = dds_fallback.missing_block("BC3", OLIVE)
    assert bc3[:2] == b"\xff\xff" and bc3[8:] == c + c + b"\0\0\0\0"


@pytest.mark.parametrize("fmt", ["BC1", "BC3"])
@pytest.mark.parametrize("offset,length", [
    (0, 4), (3, 50), (100, 60), (127, 2), (128, 8), (131, 13), (5000, 77),
])
def test_any_slice_matches_the_whole_placeholder(fmt, offset, length):
    whole, _size = _whole(fmt)
    header = whole[:H]
    piece = dds_fallback.fallback_bytes(offset, length, header, fmt, color=OLIVE)
    assert piece == whole[offset:offset + length]


def test_empty_read():
    assert dds_fallback.fallback_bytes(10, 0, b"\0" * H, "BC1") == b""


# ---------------------------------------------------------------- FUSE read


def _load_fuse(monkeypatch, tile):
    flighttrack = types.ModuleType("autoortho.flighttrack")
    monkeypatch.setitem(sys.modules, "flighttrack", flighttrack)
    monkeypatch.setitem(sys.modules, "autoortho.flighttrack", flighttrack)

    stub = types.ModuleType("autoortho.getortho")
    stub.clear_shutdown_request = lambda: None
    stub.TileCacher = lambda cache_dir: None
    stub.register_terrain_index = lambda *a, **k: None
    stub.start_prefetcher = lambda *a, **k: None
    stub.start_predictive_dds = lambda *a, **k: None
    stub.register_discovered_maptype = lambda *a, **k: None
    stub.is_shutdown_requested = lambda: False
    stub.begin_shutdown = lambda *a, **k: None
    stub.acquire_live_tile_slot = lambda timeout, weight=0: True
    stub.release_live_tile_slot = lambda weight=0: None
    BUMPS.clear()
    stub.bump = lambda name, value=1: BUMPS.append(name)
    monkeypatch.setitem(sys.modules, "getortho", stub)
    monkeypatch.setitem(sys.modules, "autoortho.getortho", stub)

    mfusepy = types.ModuleType("autoortho.mfusepy")

    class FuseOSError(OSError):
        pass

    mfusepy.FUSE = object
    mfusepy.FuseOSError = FuseOSError
    mfusepy.Operations = type("Operations", (), {})
    mfusepy.fuse_get_context = lambda: (0, 0, 0)
    mfusepy._libfuse = types.SimpleNamespace(
        fuse_get_context=lambda: types.SimpleNamespace(
            contents=types.SimpleNamespace(fuse=None)
        )
    )
    monkeypatch.setitem(sys.modules, "mfusepy", mfusepy)
    monkeypatch.setitem(sys.modules, "autoortho.mfusepy", mfusepy)

    package = types.ModuleType("autoortho")
    package.flighttrack = flighttrack
    package.getortho = stub
    monkeypatch.setitem(sys.modules, "autoortho", package)
    monkeypatch.delitem(sys.modules, "autoortho_fuse", raising=False)
    monkeypatch.delitem(sys.modules, "autoortho.autoortho_fuse", raising=False)
    import importlib

    module = importlib.import_module("autoortho_fuse")
    return module, FuseOSError


class _Tile:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def needs_live_slot(self, offset, length):
        return False

    def read_dds_bytes(self, offset, length):
        if self.behaviour == "none":
            return None
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return b"r" * length


PATH = "/textures/24832_12416_BI16.dds"
BUMPS = []


def _fuse(monkeypatch, tmp_path, behaviour, size=SIZE_8K_BC1):
    module, fuse_error = _load_fuse(monkeypatch, None)
    monkeypatch.setattr(CFG.pydds, "format", "BC1", raising=False)
    if behaviour == "fuse_error":
        behaviour = fuse_error(5, "boom")
    root = tmp_path / "scenery"
    root.mkdir()
    ao = module.AutoOrtho(str(root), cache_dir=str(tmp_path / "cache"))
    ao.tc = types.SimpleNamespace(_get_tile=lambda *args: _Tile(behaviour))
    ao._set_dds_size_cached(24832, 12416, "BI", 16, size)
    return module, ao


@pytest.mark.parametrize("behaviour", ["none", "fuse_error", RuntimeError("boom")])
def test_failed_header_read_serves_a_valid_header(monkeypatch, tmp_path, behaviour):
    _module, ao = _fuse(monkeypatch, tmp_path, behaviour)
    data = ao.read(PATH, 65536, 0, 0)
    header = parse_header(data)
    assert header["magic"] == b"DDS "
    assert (header["width"], header["height"]) == (8192, 8192)
    assert len(data) == 65536


def test_lock_timeout_serves_a_valid_header(monkeypatch, tmp_path):
    _module, ao = _fuse(monkeypatch, tmp_path, "ok")
    ao._calculate_build_timeout = lambda: 0.0
    key = ao._tile_key(24832, 12416, "BI", 16)
    lock = ao._tile_locks[key]
    lock.acquire()
    try:
        data = ao.read(PATH, 256, 0, 0)
    finally:
        lock.release()
    assert data[:4] == b"DDS "


def test_shutdown_serves_a_valid_header(monkeypatch, tmp_path):
    module, ao = _fuse(monkeypatch, tmp_path, "ok")
    module.getortho.is_shutdown_requested = lambda: True
    assert ao.read(PATH, 128, 0, 0)[:4] == b"DDS "


def test_header_follows_the_size_x_plane_was_given(monkeypatch, tmp_path):
    _module, ao = _fuse(monkeypatch, tmp_path, "none", size=SIZE_4K_BC1)
    header = parse_header(ao.read(PATH, 128, 0, 0))
    assert header["width"] == 4096


def test_successful_read_is_untouched(monkeypatch, tmp_path):
    _module, ao = _fuse(monkeypatch, tmp_path, "ok")
    assert ao.read(PATH, 64, 0, 0) == b"r" * 64


def test_injected_failure_serves_the_placeholder(monkeypatch, tmp_path):
    module, ao = _fuse(monkeypatch, tmp_path, "ok")
    monkeypatch.setattr(module, "_DDS_FAULTS", debug_faults.DdsFaults(every=True))
    data = ao.read(PATH, 256, 0, 0)
    assert data[:4] == b"DDS " and data[H:] != b"r" * (256 - H)


def test_zero_header_mode_reproduces_the_old_placeholder(monkeypatch, tmp_path):
    module, ao = _fuse(monkeypatch, tmp_path, "none")
    monkeypatch.setattr(module, "_DDS_FAULTS", debug_faults.DdsFaults(zero_header=True))
    assert ao.read(PATH, 128, 0, 0) == b"\0" * 128


# ---------------------------------------------------------------- debug switch


VAGAR = (18240, 31440, 16)   # a ZL16 texture inside +62-008


def test_switch_is_off_by_default():
    faults = debug_faults.DdsFaults.from_env({})
    assert not faults.active and not faults.zero_header
    assert not faults.should_fail(*VAGAR)


def test_parse_cells():
    every, cells = debug_faults.parse_cells(" +62-008, -27+141.dsf ;bad, ")
    assert not every
    assert cells == {(62, -8), (-27, 141)}
    assert debug_faults.parse_cells("all")[0]


def test_fails_only_inside_the_named_tiles():
    faults = debug_faults.DdsFaults.from_env({"AO_DEBUG_FAIL_DDS": "+62-008"})
    assert faults.should_fail(*VAGAR)
    assert not faults.should_fail(24832, 12416, 16)


def test_window_starts_at_each_tiles_first_read():
    now = [100.0]
    faults = debug_faults.DdsFaults(every=True, seconds=30, clock=lambda: now[0])
    assert faults.should_fail(*VAGAR)
    now[0] = 129.0
    assert faults.should_fail(*VAGAR)
    assert faults.should_fail(24832, 12416, 16)   # first read of another tile
    now[0] = 131.0
    assert not faults.should_fail(*VAGAR)
    assert faults.should_fail(24832, 12416, 16)


def test_env_options():
    faults = debug_faults.DdsFaults.from_env({
        "AO_DEBUG_FAIL_DDS": "all",
        "AO_DEBUG_FAIL_DDS_SECONDS": "45",
        "AO_DEBUG_FAIL_DDS_HEADER": "zero",
    })
    assert faults.every and faults.seconds == 45 and faults.zero_header


# ---------------------------------------------------------------- reasons and tile imagery


class _MemoryTile:
    """A tile in memory whose own bytes a fallback can serve."""

    def __init__(self, size, data=b"DDS " + b"t" * 124, fail=False):
        self.dds = types.SimpleNamespace(total_size=size)
        self.data = data
        self.fail = fail
        self.calls = []

    def standin_bytes(self, offset, length):
        self.calls.append((offset, length))
        if self.fail:
            raise RuntimeError("busy")
        return (self.data * (1 + (offset + length) // len(self.data)))[offset:offset + length]


def _with_memory_tile(ao, tile):
    ao.tc.tiles = {"24832_12416_BI_16": tile}
    ao.tc._to_tile_id = lambda row, col, maptype, zoom: f"{row}_{col}_{maptype}_{zoom}"


@pytest.mark.parametrize("behaviour,reason", [
    ("none", "read_none"),
    ("fuse_error", "fuse_error"),
    (RuntimeError("boom"), "exception"),
])
def test_fallbacks_are_counted_by_reason(monkeypatch, tmp_path, behaviour, reason):
    _module, ao = _fuse(monkeypatch, tmp_path, behaviour)
    ao.read(PATH, 128, 0, 0)
    assert f"fuse_fallback_{reason}" in BUMPS


def test_lock_timeout_and_shutdown_are_counted(monkeypatch, tmp_path):
    module, ao = _fuse(monkeypatch, tmp_path, "ok")
    ao._calculate_build_timeout = lambda: 0.0
    lock = ao._tile_locks[ao._tile_key(24832, 12416, "BI", 16)]
    lock.acquire()
    try:
        ao.read(PATH, 128, 0, 0)
    finally:
        lock.release()
    module.getortho.is_shutdown_requested = lambda: True
    ao.read(PATH, 128, 0, 0)
    assert "fuse_fallback_lock_timeout" in BUMPS
    assert "fuse_fallback_shutdown" in BUMPS


def test_failed_read_serves_the_tiles_own_bytes(monkeypatch, tmp_path):
    _module, ao = _fuse(monkeypatch, tmp_path, RuntimeError("boom"))
    tile = _MemoryTile(SIZE_8K_BC1)
    ao.tc._get_tile = lambda *args: tile_with_failing_read(tile)
    data = ao.read(PATH, 256, 0, 0)
    assert data == tile.standin_bytes(0, 256)
    assert "fuse_fallback_imagery" in BUMPS


def tile_with_failing_read(tile):
    def fail(offset, length):
        raise RuntimeError("boom")
    tile.needs_live_slot = lambda offset, length: False
    tile.read_dds_bytes = fail
    return tile


def test_lock_timeout_finds_the_tile_in_memory(monkeypatch, tmp_path):
    _module, ao = _fuse(monkeypatch, tmp_path, "ok")
    tile = _MemoryTile(SIZE_8K_BC1)
    _with_memory_tile(ao, tile)
    ao._calculate_build_timeout = lambda: 0.0
    lock = ao._tile_locks[ao._tile_key(24832, 12416, "BI", 16)]
    lock.acquire()
    try:
        data = ao.read(PATH, 128, 0, 0)
    finally:
        lock.release()
    assert tile.calls and data == tile.standin_bytes(0, 128)


@pytest.mark.parametrize("tile", [
    _MemoryTile(SIZE_4K_BC1),                    # not the size X-Plane was told
    _MemoryTile(SIZE_8K_BC1, fail=True),         # stand-in raised
    _MemoryTile(SIZE_8K_BC1, data=b"XXXX" * 32),  # no DDS header
])
def test_unusable_tile_bytes_fall_back_to_the_placeholder(monkeypatch, tmp_path, tile):
    _module, ao = _fuse(monkeypatch, tmp_path, "none")
    _with_memory_tile(ao, tile)
    data = ao.read(PATH, 256, 0, 0)
    header = parse_header(data)
    assert header["magic"] == b"DDS " and header["width"] == 8192
    assert data[H:] == dds_fallback.missing_block("BC1", tuple(dds_fallback.configured_missing_color())) * ((256 - H) // 8)
    assert "fuse_fallback_imagery" not in BUMPS


def test_shutdown_does_not_touch_tiles(monkeypatch, tmp_path):
    module, ao = _fuse(monkeypatch, tmp_path, "ok")
    tile = _MemoryTile(SIZE_8K_BC1)
    _with_memory_tile(ao, tile)
    module.getortho.is_shutdown_requested = lambda: True
    assert ao.read(PATH, 128, 0, 0)[:4] == b"DDS "
    assert tile.calls == []

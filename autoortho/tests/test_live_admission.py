"""Live tile admission is for detailed builds only.

Header chunks and base-level reads take no build memory, so they must not
queue behind near reads that hold every slot while they wait for imagery.
"""

import sys
import types
from pathlib import Path

import pytest

from autoortho import getortho, read_pattern, streaming_options

# autoortho_fuse is imported as a top-level module below. Appended, not
# prepended, so ``autoortho`` keeps meaning the package for other tests.
AUTOORTHO_DIR = str(Path(__file__).resolve().parents[1])
if AUTOORTHO_DIR not in sys.path:
    sys.path.append(AUTOORTHO_DIR)

HEADER = read_pattern.DDS_HEADER_SIZE
CHUNK = read_pattern.XPLANE_CHUNK


def _layout(size=4096, block=8):
    mipmaps, pos, idx = [], HEADER, 0
    while size >= 4:
        length = (size // 4) * (size // 4) * block
        mipmaps.append(
            types.SimpleNamespace(idx=idx, startpos=pos, endpos=pos + length, length=length)
        )
        pos += length
        size //= 2
        idx += 1
    return mipmaps


def _aligned(mipmaps, level):
    return mipmaps[level].startpos // CHUNK * CHUNK, CHUNK


BASE = 4


@pytest.mark.parametrize(
    "offset,length",
    [
        (0, HEADER),            # the DDS header alone
        (0, CHUNK),             # X-Plane's header chunk
        (0, CHUNK // 2),        # macFUSE's first piece of it
        (CHUNK // 2, CHUNK // 2),  # and its second piece
    ],
)
def test_header_reads_skip_admission(offset, length):
    assert not read_pattern.needs_build_slot(_layout(), offset, length, BASE)


def test_base_level_read_skips_admission():
    mm = _layout()
    assert not read_pattern.needs_build_slot(mm, *_aligned(mm, BASE), BASE)
    assert not read_pattern.needs_build_slot(mm, mm[BASE + 1].startpos, 1024, BASE)


def test_detailed_reads_need_admission():
    mm = _layout()
    # Reading on into mipmap 0 past the header chunk.
    assert read_pattern.needs_build_slot(mm, CHUNK, CHUNK, BASE)
    # The aligned read for a middle level (starts in the finer level's tail).
    assert read_pattern.needs_build_slot(mm, *_aligned(mm, 2), BASE)
    # The read for the level just finer than the base.
    assert read_pattern.needs_build_slot(mm, *_aligned(mm, BASE - 1), BASE)


def test_read_inside_one_level_stays_conservative():
    mm = _layout()
    assert read_pattern.needs_build_slot(mm, mm[0].startpos + 3 * CHUNK, CHUNK, BASE)


def test_whole_mipmap_zero_read_needs_admission():
    mm = _layout(256)
    assert read_pattern.needs_build_slot(mm, 0, mm[0].endpos, 1)


# ---------------------------------------------------------------- the tile


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    yield t
    t.close()


def test_tile_skips_admission_for_header_and_base(tile):
    mm = tile.dds.mipmap_list
    assert tile.needs_live_slot(0, CHUNK) is False
    assert tile.needs_live_slot(*_aligned(mm, tile.max_mipmap)) is False
    assert tile.needs_live_slot(CHUNK, CHUNK) is True


def test_tile_admits_everything_with_the_classifier_off(tile, monkeypatch):
    original = streaming_options.flag
    monkeypatch.setattr(
        streaming_options, "flag",
        lambda name: False if name == "read_classifier" else original(name),
    )
    assert tile.needs_live_slot(0, CHUNK) is True


# ---------------------------------------------------------------- FUSE read


def _load_fuse(monkeypatch, acquired):
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

    def acquire(timeout):
        acquired.append(timeout)
        return True

    stub.acquire_live_tile_slot = acquire
    stub.release_live_tile_slot = lambda: acquired.append("released")
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

    return importlib.import_module("autoortho_fuse")


class _FakeTile:
    def __init__(self, needs):
        self.needs = needs
        self.reads = []

    def needs_live_slot(self, offset, length):
        return self.needs

    def read_dds_bytes(self, offset, length):
        self.reads.append((offset, length))
        return b"\0" * length


@pytest.mark.parametrize("needs,slots", [(False, 0), (True, 1)])
def test_fuse_read_takes_a_slot_only_when_needed(monkeypatch, tmp_path, needs, slots):
    acquired = []
    module = _load_fuse(monkeypatch, acquired)
    root = tmp_path / "scenery"
    root.mkdir()
    ao = module.AutoOrtho(str(root), cache_dir=str(tmp_path / "cache"))
    fake = _FakeTile(needs)
    ao.tc = types.SimpleNamespace(_get_tile=lambda *args: fake)

    data = ao.read("/textures/24832_12416_BI16.dds", CHUNK, 0, 0)

    assert len(data) == CHUNK
    assert fake.reads == [(0, CHUNK)]
    assert len([a for a in acquired if a != "released"]) == slots
    assert acquired.count("released") == slots

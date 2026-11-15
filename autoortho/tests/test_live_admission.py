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

    def acquire(timeout, weight=0):
        acquired.append(timeout)
        return True

    stub.acquire_live_tile_slot = acquire
    stub.release_live_tile_slot = lambda weight=0: acquired.append("released")
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


# ------------------------------------------------------------ weighted gate


def test_gate_admits_by_memory_within_the_count_limit():
    from autoortho.live_settings import ResizableGate

    gate = ResizableGate(8, byte_budget=100)
    assert gate.acquire(blocking=False, weight=60)
    assert not gate.acquire(blocking=False, weight=50)  # over the budget
    assert gate.acquire(blocking=False, weight=40)
    assert gate.weight_held == 100
    gate.release(weight=60)
    assert gate.acquire(blocking=False, weight=50)
    assert gate.held == 2


def test_gate_admits_an_oversized_request_alone():
    from autoortho.live_settings import ResizableGate

    gate = ResizableGate(4, byte_budget=100)
    assert gate.acquire(blocking=False, weight=500)
    assert not gate.acquire(blocking=False, weight=1)
    gate.release(weight=500)
    assert gate.acquire(blocking=False, weight=1)


def test_gate_serves_waiters_in_order():
    import threading
    import time
    from autoortho.live_settings import ResizableGate

    gate = ResizableGate(8, byte_budget=100)
    assert gate.acquire(weight=80)
    order = []

    def heavy():
        assert gate.acquire(timeout=5, weight=90)
        order.append("heavy")

    waiter = threading.Thread(target=heavy)
    waiter.start()
    time.sleep(0.1)
    # A light request would fit, but the heavy waiter came first.
    assert not gate.acquire(blocking=False, weight=10)
    gate.release(weight=80)
    waiter.join(5)
    assert order == ["heavy"]


def test_gate_without_budget_counts_only():
    from autoortho.live_settings import ResizableGate

    gate = ResizableGate(2)
    assert gate.acquire(blocking=False, weight=10 ** 9)
    assert gate.acquire(blocking=False, weight=10 ** 9)
    assert not gate.acquire(blocking=False)


def test_read_weight_follows_the_rows_it_builds(tile):
    mm0 = tile.dds.mipmap_list[0]
    full = getortho.live_build_weight(tile.dds.width, tile.dds.height, 0)
    assert tile.live_slot_weight(mm0.startpos, mm0.length) == full
    rows = tile.dds.height // 256
    one_row = tile.live_slot_weight(mm0.startpos, 1024)
    assert one_row == -(-full // rows)
    mm2 = tile.dds.mipmap_list[2]
    assert tile.live_slot_weight(mm2.startpos, mm2.length) == full // 16


def test_build_memory_budget_is_derived_from_the_cache_limit(monkeypatch):
    monkeypatch.setattr(getortho.CFG.autoortho, "live_build_memory_mb", 0, raising=False)
    assert getortho._live_build_memory_bytes(4) == 1024 ** 3
    assert getortho._live_build_memory_bytes(0.5) == 256 * 1048576
    assert getortho._live_build_memory_bytes(64) == 2 * 1024 ** 3
    monkeypatch.setattr(getortho.CFG.autoortho, "live_build_memory_mb", 300, raising=False)
    assert getortho._live_build_memory_bytes(4) == 300 * 1048576


# ------------------------------------------------------------ direct reads


def test_fuse_serves_a_finished_texture_from_disk_then_falls_back(monkeypatch, tmp_path):
    acquired = []
    module = _load_fuse(monkeypatch, acquired)
    failed = module.disk_texture.DiskTextureFailed
    stub = module.getortho

    class FakeTexture:
        size = 11184952

        def __init__(self):
            self.reads = 0
            self.closed = False

        def read(self, offset, length):
            self.reads += 1
            if self.reads > 1:
                raise failed("checksum")
            return b"d" * length

        def close(self):
            self.closed = True

    texture = FakeTexture()
    stub.note_texture_requested = lambda *a: None
    stub.open_disk_texture = lambda *a: texture
    stub.bump = lambda *a, **k: None
    root = tmp_path / "scenery"
    root.mkdir()
    ao = module.AutoOrtho(str(root), cache_dir=str(tmp_path / "cache"))
    live = _FakeTile(False)
    opened, closed = [], []
    ao.tc = types.SimpleNamespace(
        _get_tile=lambda *args: live,
        _open_tile=lambda *args: opened.append(args) or live,
        _close_tile=lambda *args: closed.append(args),
    )
    path = "/textures/24832_12416_BI16.dds"

    fh = ao.open(path, 0)
    assert fh >= 1 << 32
    assert ao.read(path, 100, 0, fh) == b"d" * 100
    assert opened == []  # no tile in memory
    # The file fails a check: the rest of this open is served live.
    assert ao.read(path, 100, 200, fh) == b"\0" * 100
    assert len(opened) == 1 and live.reads == [(200, 100)]
    ao.release(path, fh)
    assert texture.closed
    assert len(closed) == 1

"""Native reads of textures served from disk (hybrid read path plan, N7 / S2).

Needs the aofast code in the aopipeline library. A library built before it
(or a machine without one) skips these tests; set AO_TEST_AOFAST_LIB to a
build of ``aopipeline/aofast.c`` to run them against it.
"""
import ctypes
import os
import random
import threading

import pytest

from autoortho import read_pattern
from autoortho.aopipeline import AoFast
from autoortho.disk_texture import mip_layout


def _native():
    path = os.environ.get(AoFast.ENV_LIB) or None
    native = AoFast.load(path) if path else AoFast.load()
    if native is None:
        pytest.skip("aopipeline library without native reads (aofast)")
    return native


@pytest.fixture(scope="module")
def native():
    return _native()


FH_OFFSET = 8


class FileInfo(ctypes.Structure):
    """Enough of fuse_file_info: fh at FH_OFFSET."""
    _fields_ = [("flags", ctypes.c_int64), ("fh", ctypes.c_uint64), ("pad", ctypes.c_uint64 * 4)]


class Mount:
    """One attached mount whose Python side records what it is handed."""

    PY_BYTE = 0x5A

    def __init__(self, native):
        self.native = native
        self.calls = []
        self.lock = threading.Lock()

        def python_read(path, buf, size, offset, fi):
            with self.lock:
                self.calls.append((offset, size))
            ctypes.memset(buf, self.PY_BYTE, size)
            return size

        native.set_fh_offset(FH_OFFSET)
        native.set_enabled(True)
        self.index = native.attach_callback(python_read)
        assert self.index >= 0

    def read(self, handle, offset, size):
        fi = FileInfo(fh=handle)
        buf = ctypes.create_string_buffer(size)
        n = self.native.read_mount(self.index, b"/textures/x.dds", buf, size, offset,
                                   ctypes.addressof(fi))
        return buf.raw[:n]

    def close(self):
        self.native.uninstall(self.index)


@pytest.fixture
def mount(native):
    m = Mount(native)
    yield m
    m.close()


def _texture_file(tmp_path, px=1024, fmt="BC1", seed=7, name="t.dds"):
    levels = mip_layout(px, px, fmt)
    size = levels[-1].endpos
    rng = random.Random(seed)
    data = bytearray(rng.getrandbits(8) for _ in range(size))
    data[:4] = b"DDS "
    path = tmp_path / name
    path.write_bytes(bytes(data))
    rows = max(1, px // 256)
    row_bytes = levels[0].length // rows
    return str(path), bytes(data), levels, rows, row_bytes


def _register(native, path, data, levels, rows, row_bytes, verified=None, **kwargs):
    st = os.stat(path)
    handle, _table = native.register(
        path, len(data), st.st_mtime_ns, levels, levels[0].startpos, row_bytes, rows,
        range(rows) if verified is None else verified, **kwargs,
    )
    return handle


def _xplane_reads(levels, size, level):
    reads = [(0, 65536)]
    pos = (levels[level].startpos // 65536) * 65536
    while pos < size:
        reads.append((pos, min(65536, size - pos)))
        pos += 65536
    return reads


# ------------------------------------------------------------ classify parity

@pytest.mark.parametrize("px,fmt", [(4096, "BC1"), (8192, "BC3"), (1024, "BC1"), (256, "BC3")])
def test_classify_matches_python(native, px, fmt):
    levels = mip_layout(px, px, fmt)
    size = levels[-1].endpos
    rng = random.Random(px)
    sequences = [
        _xplane_reads(levels, size, level) for level in range(len(levels))
    ] + [
        [(rng.randrange(0, size), rng.choice((1, 128, 32768, 65536))) for _ in range(40)]
        for _ in range(30)
    ] + [
        # macFUSE splits each 64 KB read in two.
        [(o + h, 32768) for o, _ in _xplane_reads(levels, size, 0) for h in (0, 32768)],
    ]
    for reads in sequences:
        py_cursor = read_pattern.ReadCursor()
        c_cursor = [-1, -1]
        for offset, length in reads:
            expected = read_pattern.classify(levels, py_cursor, offset, length)
            got = native.classify(levels, c_cursor, offset, length)
            assert got == expected, (px, fmt, offset, length)


# ------------------------------------------------------------ byte equality

@pytest.mark.parametrize("px,fmt", [(1024, "BC1"), (2048, "BC3")])
def test_native_bytes_equal_the_file(native, mount, tmp_path, px, fmt):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, px, fmt)
    handle = _register(native, path, data, levels, rows, row_bytes)
    assert handle & AoFast.TAG
    try:
        for level in range(len(levels)):
            for offset, size in _xplane_reads(levels, len(data), level):
                assert mount.read(handle, offset, size) == data[offset:offset + size]
        assert mount.calls == [], "nothing handed to Python"
        # Past the end: nothing; straddling: cut at the end.
        assert mount.read(handle, len(data), 65536) == b""
        assert mount.read(handle, len(data) - 10, 65536) == data[-10:]
    finally:
        native.unregister(handle)


def test_unchecked_rows_go_to_python_until_marked(native, mount, tmp_path):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024)
    handle = _register(native, path, data, levels, rows, row_bytes, verified=[0])
    try:
        row2 = levels[0].startpos + 2 * row_bytes
        assert mount.read(handle, row2, 4096) == bytes([Mount.PY_BYTE]) * 4096
        assert mount.calls == [(row2, 4096)]
        # Levels below mipmap 0 never need a check.
        tail = levels[1].startpos
        assert mount.read(handle, tail, 4096) == data[tail:tail + 4096]
        assert native.mark_verified(handle, [2])
        assert mount.read(handle, row2, 4096) == data[row2:row2 + 4096]
        assert len(mount.calls) == 1
    finally:
        native.unregister(handle)


def test_untagged_released_and_disabled_handles_go_to_python(native, mount, tmp_path):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024)
    assert mount.read(1234, 0, 16) == bytes([Mount.PY_BYTE]) * 16
    handle = _register(native, path, data, levels, rows, row_bytes)
    assert native.disable(handle)
    assert mount.read(handle, 0, 16) == bytes([Mount.PY_BYTE]) * 16
    native.unregister(handle)
    assert mount.read(handle, 0, 16) == bytes([Mount.PY_BYTE]) * 16
    assert native.unregister(handle) == []  # twice: harmless


def test_a_changed_file_is_not_registered(native, tmp_path):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024)
    st = os.stat(path)
    args = (levels[0].startpos, row_bytes, rows)
    assert native.register(path, len(data) + 8, st.st_mtime_ns, levels, *args)[0] == 0
    assert native.register(path, len(data), st.st_mtime_ns + 1, levels, *args)[0] == 0
    assert native.register(path + ".missing", len(data), st.st_mtime_ns, levels, *args)[0] == 0
    bad_levels = mip_layout(2048, 2048, "BC1")  # does not match the size
    assert native.register(path, len(data), st.st_mtime_ns, bad_levels, *args)[0] == 0
    # A finest level outside the texture, or an odd block size.
    assert native.register(path, len(data), st.st_mtime_ns, levels, *args,
                           finest_level=len(levels))[0] == 0
    assert native.register(path, len(data), st.st_mtime_ns, levels, *args,
                           block_size=12)[0] == 0


def test_a_file_truncated_under_the_slot_goes_to_python(native, mount, tmp_path):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024)
    handle = _register(native, path, data, levels, rows, row_bytes)
    try:
        with open(path, "r+b") as f:
            f.truncate(1000)
        assert mount.read(handle, 900, 4096) == bytes([Mount.PY_BYTE]) * 4096
        assert native.stats()["deferred_io"] >= 1
    finally:
        native.unregister(handle)


def test_wanted_levels_are_drained_once(native, mount, tmp_path):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024)
    handle = _register(native, path, data, levels, rows, row_bytes)
    try:
        native.drain()
        for offset, size in _xplane_reads(levels, len(data), 2):
            mount.read(handle, offset, size)
        samples = [s for s in native.drain() if s[0] == handle]
        assert samples == [(handle, 2, 1)]
        assert [s for s in native.drain() if s[0] == handle] == []
        for offset, size in _xplane_reads(levels, len(data), 1):
            mount.read(handle, offset, size)
    finally:
        assert native.unregister(handle) == [(1, 1)]


def test_concurrent_reads_while_released_never_cross_textures(native, mount, tmp_path):
    files = [_texture_file(tmp_path, 1024, seed=s, name=f"t{s}.dds") for s in (1, 2)]
    py_block = bytes([Mount.PY_BYTE]) * 4096
    errors = []
    for _round in range(20):
        handles = [_register(native, *f) for f in files]
        start = threading.Barrier(17)

        def reader(handle=handles[0], data=files[0][1]):
            start.wait()
            for _ in range(200):
                offset = random.randrange(0, len(data) - 4096)
                got = mount.read(handle, offset, 4096)
                if got != data[offset:offset + 4096] and got != py_block:
                    errors.append(offset)

        threads = [threading.Thread(target=reader) for _ in range(16)]
        for t in threads:
            t.start()
        start.wait()
        native.unregister(handles[0])  # while the readers run
        for t in threads:
            t.join()
        native.unregister(handles[1])
    assert not errors
    assert native.stats()["active_slots"] == 0


def test_install_swaps_a_struct_read_and_keeps_the_python_one(native):
    seen = []
    prototype = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_int64, ctypes.c_void_p)

    class Ops(ctypes.Structure):
        _fields_ = [("read", prototype)]

    ops = Ops()
    ops.read = prototype(lambda path, buf, size, offset, fi: seen.append(offset) or 0)
    native.set_fh_offset(FH_OFFSET)
    mount = native.install(ops)
    assert mount >= 0
    try:
        fi = FileInfo(fh=42)  # untagged: Python answers
        buf = ctypes.create_string_buffer(16)
        assert ops.read(b"/x", buf, 16, 7, ctypes.addressof(fi)) == 0
        assert seen == [7]
    finally:
        native.uninstall(mount)


# ------------------------------------------------------------ S3: partial entries

def _python_texture(path, data, px, fmt, finest_level, recolour=None):
    """The Python direct read (tier 2) of the same file."""
    from autoortho.disk_texture import DiskTexture
    texture = DiskTexture(
        {"key": "k", "tile_id": "t", "max_zoom": 16, "size": len(data), "width": px,
         "height": px, "fmt": fmt, "manifests": {}, "finest_level": finest_level,
         "dds_path": path, "mtime_ns": os.stat(path).st_mtime_ns},
        unpin=lambda key: None, invalidate=lambda *a: None, recolour=recolour,
    )
    texture._file.verified_rows.update(range(64))  # the native side was told they are
    return texture


def _python_answer(texture, offset, size):
    """What tier 2 answers, or None where it hands the open to a live tile."""
    from autoortho.disk_texture import DiskTextureLevelMissing
    try:
        return texture.read(offset, size)
    except DiskTextureLevelMissing:
        return None


@pytest.mark.parametrize("px,fmt,finest", [(1024, "BC1", 2), (2048, "BC3", 1), (1024, "BC1", 4)])
def test_partial_entries_answer_like_python(native, mount, tmp_path, px, fmt, finest):
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, px, fmt)
    block = 16 if fmt == "BC3" else 8
    for level in range(len(levels)):
        python = _python_texture(path, data, px, fmt, finest)
        handle = _register(native, path, data, levels, rows, row_bytes, verified=[],
                           finest_level=finest, block_size=block)
        assert handle
        try:
            for offset, size in _xplane_reads(levels, len(data), level):
                expected = _python_answer(python, offset, size)
                calls = len(mount.calls)
                got = mount.read(handle, offset, size)
                if expected is None:
                    # A finer level than the file holds: Python decides.
                    assert len(mount.calls) == calls + 1
                    break
                assert got == expected, (level, offset)
                assert len(mount.calls) == calls, "answered natively"
        finally:
            native.unregister(handle)
            python.close()
    assert native.stats()["deferred_level"] >= 1


# ------------------------------------------------------------ S3: season colours

def _table(saturation):
    np = pytest.importorskip("numpy")
    from autoortho import colour_table
    return colour_table.saturation_table(saturation), np


@pytest.mark.parametrize("block", [8, 16])
def test_recolour_matches_python(native, block):
    table, np = _table(0.35)
    from autoortho import colour_table
    rng = random.Random(block)
    for _ in range(200):
        offset = rng.randrange(0, 4096)
        data = bytes(rng.getrandbits(8) for _ in range(rng.randrange(1, 700)))
        expected = colour_table.apply(data, offset, block, table)
        assert native.recolour(table, data, offset, block) == expected, offset


@pytest.mark.parametrize("finest", [0, 2])
def test_season_colours_answer_like_python(native, mount, tmp_path, finest):
    table, _np = _table(0.6)
    from autoortho import colour_table
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024, "BC1")
    python = _python_texture(
        path, data, 1024, "BC1", finest,
        recolour=lambda d, off: colour_table.apply(d, off, 8, table),
    )
    handle, entry = native.register(
        path, len(data), os.stat(path).st_mtime_ns, levels, levels[0].startpos, row_bytes,
        rows, range(rows), finest_level=finest, block_size=8, table=table,
    )
    assert handle and entry >= 0
    try:
        for offset, size in _xplane_reads(levels, len(data), 3):
            assert mount.read(handle, offset, size) == _python_answer(python, offset, size)
        assert mount.calls == []
    finally:
        native.unregister(handle)
        native.release_table(entry)
        python.close()


def test_table_entries_are_reused_only_when_idle(native, tmp_path):
    np = pytest.importorskip("numpy")
    path, data, levels, rows, row_bytes = _texture_file(tmp_path, 1024)
    tables = [np.full(65536, i, dtype=np.uint16) for i in range(AoFast.TABLES + 1)]
    held = [native.acquire_table(t) for t in tables[:AoFast.TABLES]]
    assert sorted(held) == list(range(AoFast.TABLES)) or len(set(held)) == AoFast.TABLES
    assert native.acquire_table(tables[-1]) == -1, "every entry in use"
    native.release_table(held[0])
    assert native.acquire_table(tables[-1]) == held[0]
    for entry in held[1:]:
        native.release_table(entry)
    native.release_table(held[0])


def test_native_reads_stay_on_with_a_read_trace(monkeypatch):
    """AO_READ_TRACE no longer turns native reads off: a flight can be
    traced (opens, for gate C) with native reads answering."""
    import sys
    import types

    from autoortho import autoortho_fuse

    fake_module = types.ModuleType("fake_fuse_for_native_test")
    fake_module.fuse_main_real = lambda *a: 0
    fake_module.fuse_file_info = FileInfo
    sys.modules[fake_module.__name__] = fake_module

    class FakeFUSE:
        pass

    FakeFUSE.__module__ = fake_module.__name__

    class FakeNative:
        def set_fh_offset(self, offset):
            self.offset = offset

    fake_native = FakeNative()
    monkeypatch.setattr(autoortho_fuse, "_native_reads_setting", lambda: True)
    monkeypatch.setattr(
        autoortho_fuse, "_aofast", lambda: types.SimpleNamespace(load=lambda: fake_native)
    )
    ao = types.SimpleNamespace(_read_trace=object())
    try:
        with autoortho_fuse.native_reads_for(ao, FakeFUSE):
            assert autoortho_fuse._native_pending.ao is ao
        assert ao._native_candidate is fake_native
        assert fake_native.offset == FileInfo.fh.offset
        assert autoortho_fuse._native_pending.ao is None
    finally:
        sys.modules.pop(fake_module.__name__, None)

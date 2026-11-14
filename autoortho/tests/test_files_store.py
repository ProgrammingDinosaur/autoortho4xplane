"""Files mode: neutral pool, texture store (writes, upgrades, eviction) and gate."""

import os
import socket
import sys
import time

import pytest

import files_mode_helpers as h
from files_mode import dds_layout, fsops, gate, neutral, store
from texture_service import Demand, DemandSource, TextureKey

TOP = 1024  # small textures keep the tests fast


def _store(tmp_path, strategy="link"):
    root = tmp_path / "files"
    (root / "z_ao_eur" / "textures").mkdir(parents=True)
    pool = neutral.NeutralPool(str(root / "_ao" / "neutral"))
    index = store.TextureIndex(str(root / "_ao" / "textures.sqlite"))
    return store.TextureStore(str(root), pool, index, strategy=strategy), pool, index


def _built(level, zoom=16, row=22880, col=34256):
    svc = h.FakeService()
    return svc.build(Demand(TextureKey(row, col, "BI", zoom), level, 0, DemandSource.PLANNER,
                            max_zoom=zoom - 2))


def _read_level(path, mip, top=TOP):
    lay = dds_layout.layout(top, top, "BC1")
    start, length = lay.offsets[mip]
    with open(path, "rb") as handle:
        handle.seek(start)
        return handle.read(length)


def test_neutral_pool_content_and_links(tmp_path):
    pool = neutral.NeutralPool(str(tmp_path / "n"))
    dst = str(tmp_path / "a.dds")
    assert pool.link(TOP, "BC1", dst)
    assert pool.is_neutral(dst)
    data = open(dst, "rb").read()
    lay = dds_layout.layout(TOP, TOP, "BC1")
    assert data[:128] == lay.header and len(data) == lay.total_size
    assert data[128:136] == dds_layout.neutral_block("BC1")


def test_neutral_pool_rolls_over_at_link_limit(tmp_path, monkeypatch):
    pool = neutral.NeutralPool(str(tmp_path / "n"))
    real = fsops.hard_link
    counts = {}

    def limited(src, dst):
        counts[src] = counts.get(src, 0) + 1
        if counts[src] > 2:
            return neutral.ERROR_TOO_MANY_LINKS
        return real(src, dst)

    monkeypatch.setattr(fsops, "hard_link", limited)
    for i in range(5):
        assert pool.link(TOP, "BC1", str(tmp_path / f"t{i}.dds"))
    assert len([n for n in os.listdir(tmp_path / "n") if n.endswith(".dds")]) == 3


def test_first_build_replaces_neutral_link(tmp_path):
    st, pool, index = _store(tmp_path)
    b = _built(3)
    name = b.key.name
    assert st.make_unbuilt("z_ao_eur", name, TOP, "BC1")
    path = st.path("z_ao_eur", name)
    assert pool.is_neutral(path)
    res = st.write("z_ao_eur", b)
    assert res.ok and not res.in_place
    assert not pool.is_neutral(path)
    assert _read_level(path, 3) == bytes([h.level_byte(3)]) * len(_read_level(path, 3))
    assert _read_level(path, 0) == b"\0" * len(_read_level(path, 0))  # a hole
    assert index.get(name).built_level == 3
    # The neutral pool itself is untouched.
    other = str(tmp_path / "other.dds")
    pool.link(TOP, "BC1", other)
    assert _read_level(other, 3)[:8] == dds_layout.neutral_block("BC1")


def test_upgrade_writes_in_place_only_new_levels(tmp_path):
    st, pool, index = _store(tmp_path)
    st.write("z_ao_eur", _built(3))
    path = st.path("z_ao_eur", _built(3).key.name)
    ino = os.stat(path).st_ino
    res = st.write("z_ao_eur", _built(1))
    assert res.ok and res.in_place
    assert os.stat(path).st_ino == ino
    lay = dds_layout.layout(TOP, TOP, "BC1")
    assert res.bytes_written == lay.offsets[1][1] + lay.offsets[2][1]
    assert index.get(_built(1).key.name).built_level == 1
    assert _read_level(path, 1)[:4] == bytes([h.level_byte(1)]) * 4


def test_never_writes_in_place_into_a_shared_file(tmp_path):
    st, pool, index = _store(tmp_path)
    b = _built(3)
    st.write("z_ao_eur", b)
    path = st.path("z_ao_eur", b.key.name)
    os.link(path, str(tmp_path / "second-name.dds"))
    res = st.write("z_ao_eur", _built(2))
    assert res.ok and not res.in_place   # replaced, the other name keeps its data
    assert _read_level(str(tmp_path / "second-name.dds"), 2) == b"\0" * len(_read_level(path, 2))


def test_sparse_strategy_unbuilt_and_in_place(tmp_path):
    st, pool, index = _store(tmp_path, strategy="sparse")
    b = _built(2)
    st.make_unbuilt("z_ao_eur", b.key.name, TOP, "BC1")
    path = st.path("z_ao_eur", b.key.name)
    assert not pool.is_neutral(path)
    assert _read_level(path, 4)[:8] == dds_layout.neutral_block("BC1")
    res = st.write("z_ao_eur", b)
    assert res.ok and res.in_place


def test_index_persists_and_validates(tmp_path):
    st, pool, index = _store(tmp_path)
    b = _built(3)
    st.write("z_ao_eur", b)
    index.close()
    st2, _p, index2 = _store_reopen(tmp_path)
    assert st2.is_built("z_ao_eur", b.key.name)
    # A file that no longer matches its row is treated as unbuilt.
    st3, _p, index3 = _store_reopen(tmp_path)
    with open(st3.path("z_ao_eur", b.key.name), "r+b") as handle:
        handle.write(b"XXXX")
    assert not st3.is_built("z_ao_eur", b.key.name)


def _store_reopen(tmp_path):
    root = tmp_path / "files"
    pool = neutral.NeutralPool(str(root / "_ao" / "neutral"))
    index = store.TextureIndex(str(root / "_ao" / "textures.sqlite"))
    return store.TextureStore(str(root), pool, index), pool, index


def test_coarsen_and_delete(tmp_path):
    st, pool, index = _store(tmp_path)
    b = _built(0)
    st.write("z_ao_eur", b)
    path = st.path("z_ao_eur", b.key.name)
    st.coarsen("z_ao_eur", b.key.name, 4)
    assert index.get(b.key.name).built_level == 4
    assert _read_level(path, 0)[:16] == b"\0" * 16
    assert _read_level(path, 4)[:4] == bytes([h.level_byte(4)]) * 4
    st.delete("z_ao_eur", b.key.name)
    assert not os.path.exists(path) and index.get(b.key.name) is None


def test_fill_standin_copies_up(tmp_path):
    st, pool, index = _store(tmp_path)
    b = _built(2)
    st.write("z_ao_eur", b)
    assert st.fill_standin("z_ao_eur", b.key.name, 0)
    path = st.path("z_ao_eur", b.key.name)
    assert _read_level(path, 0)[:8] != b"\0" * 8
    state = index.get(b.key.name)
    assert state.built_level == 2 and state.copied_level == 0
    # A real build still replaces the stand-in.
    st.write("z_ao_eur", _built(0))
    assert _read_level(path, 0)[:4] == bytes([h.level_byte(0)]) * 4


def test_remove_if_unbuilt_keeps_built(tmp_path):
    st, pool, index = _store(tmp_path)
    b = _built(3)
    st.make_unbuilt("z_ao_eur", "1_1_BI16.dds", TOP, "BC1")
    st.write("z_ao_eur", b)
    assert st.remove_if_unbuilt("z_ao_eur", "1_1_BI16.dds")
    assert not st.remove_if_unbuilt("z_ao_eur", b.key.name)
    assert os.path.exists(st.path("z_ao_eur", b.key.name))


# --------------------------------------------------------------------- gate

def test_gate_parse_rejects_garbage():
    assert gate.parse(b"AO1 HOLD 47.5 8.5 3") == ("HOLD", [47.5, 8.5, 3])
    for bad in (b"", b"AO1", b"AO2 HOLD 1 2 3", b"AO1 HOLD 91 0 1", b"AO1 LOAD x y",
                b"\xff\xfe", b"AO1 " + b"A" * 600, b"AO1 NOPE"):
        assert gate.parse(bad) is None


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_gate_wait_then_go():
    clock = _Clock()
    status = gate.StartStatus(ready=False, built=1, total=10, updated=clock.t)
    g = gate.Gate(lambda: status, clock=clock)
    assert g.handle(b"AO1 HOLD 47.5 8.5 1").startswith("AO1 WAIT 1 10 1")
    status.ready = True
    assert g.handle(b"AO1 HOLD 47.5 8.5 2").startswith("AO1 GO 2")


def test_gate_releases_on_stall_and_silent_loop():
    clock = _Clock()
    status = gate.StartStatus(ready=False, built=3, total=10, updated=clock.t)
    g = gate.Gate(lambda: status, clock=clock)
    g.handle(b"AO1 HOLD 0 0 1")
    clock.t += gate.STALL_S + 1
    status.updated = clock.t
    assert g.handle(b"AO1 HOLD 0 0 2").startswith("AO1 GO")
    g2 = gate.Gate(lambda: status, clock=clock)
    clock.t += gate.LOOP_SILENT_S + 1
    assert g2.handle(b"AO1 HOLD 0 0 3").startswith("AO1 GO")


def test_gate_release_and_position_callback():
    seen = []
    g = gate.Gate(lambda: gate.StartStatus(updated=time.monotonic()),
                  on_position=lambda la, lo: seen.append((la, lo)))
    g.release("user stop")
    assert g.handle(b"AO1 HOLD 47.5 8.5 1").startswith("AO1 GO")
    assert seen == [(47.5, 8.5)]


def test_gate_udp_roundtrip():
    loaded = []
    g = gate.Gate(lambda: gate.StartStatus(ready=True, updated=time.monotonic()),
                  on_load=lambda la, lo: loaded.append((la, lo)), port=0)
    port = g.start()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.sendto(b"AO1 HELLO 12100 1", ("127.0.0.1", port))
        assert sock.recv(64) == b"AO1 ACK"
        sock.sendto(b"AO1 LOAD 47.5 8.5", ("127.0.0.1", port))
        assert sock.recv(64) == b"AO1 ACK"
        sock.sendto(b"AO1 HOLD 47.5 8.5 9", ("127.0.0.1", port))
        assert sock.recv(64).startswith(b"AO1 GO 9")
        sock.close()
    finally:
        g.stop()
    assert loaded == [(47.5, 8.5)]

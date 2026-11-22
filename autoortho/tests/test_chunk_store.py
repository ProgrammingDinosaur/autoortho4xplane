"""Performance plan, item 3: the packed chunk store."""

import multiprocessing
import os
import sqlite3
import time

import pytest

from autoortho import chunk_store
from autoortho.chunk_store import ChunkStore

JPEG = b"\xff\xd8\xff" + b"x" * 100


def _jpeg(tag) -> bytes:
    return b"\xff\xd8\xff" + str(tag).encode() * 20


@pytest.fixture
def store(tmp_path):
    s = ChunkStore(str(tmp_path), batch_seconds=0.01)
    yield s
    s.close()


def test_morton_round_trip():
    for col, row in [(0, 0), (1, 0), (0, 1), (125744, 72992), (2**19 - 1, 2**18 + 5)]:
        assert chunk_store.unmorton(chunk_store.morton(col, row)) == (col, row)


def test_aligned_square_is_one_morton_range():
    keys = [(64 + dx, 32 + dy) for dy in range(16) for dx in range(16)]
    codes = sorted(chunk_store.morton(c, r) for c, r in keys)
    assert codes[-1] - codes[0] + 1 == len(keys)


def test_parse_chunk_name():
    assert chunk_store.parse_chunk_name("/x/125744_72992_18_BI.jpg") == (125744, 72992, 18, "BI")
    assert chunk_store.parse_chunk_name(".125744_72992_18_BI.jpg.tmp") is None
    assert chunk_store.parse_chunk_name("a_b_18_BI.jpg") is None


def test_put_is_readable_before_and_after_commit(store):
    assert store.put(10, 20, 16, "BI", JPEG)
    assert store.get(10, 20, 16, "BI") == JPEG  # from the pending map
    assert store.flush()
    assert store.pending_bytes == 0
    assert store.get(10, 20, 16, "BI") == JPEG
    assert store.get(11, 20, 16, "BI") is None
    assert store.get(10, 20, 16, "EOX") is None


def test_range_read_of_a_texture(store):
    keys = [(160 + dx, 96 + dy) for dy in range(16) for dx in range(16)]
    for col, row in keys:
        store.put(col, row, 17, "BI", _jpeg((col, row)))
    # A neighbour inside the Morton range of another zoom or map is not read.
    store.put(160, 96, 17, "EOX", _jpeg("eox"))
    store.flush()
    found = store.get_many("BI", 17, keys)
    assert len(found) == 256
    assert found[(161, 97)] == _jpeg((161, 97))
    # Sparse keys go through IN batches.
    sparse = [(0, 0), (161, 97), (100000, 70000)]
    assert set(store.get_many("BI", 17, sparse)) == {(161, 97)}
    assert store.contains_many("BI", 17, keys[:10] + [(0, 0)]) == set(keys[:10])


def test_no_imagery_rows(store):
    store.mark_no_imagery(5, 6, 15, "BI")
    store.flush()
    assert store.get(5, 6, 15, "BI") is None
    assert store.status(5, 6, 15, "BI") == chunk_store.STATUS_NO_IMAGERY
    seen = set()
    store.get_many("BI", 15, [(5, 6), (7, 7)], no_imagery=seen)
    assert seen == {(5, 6)}
    # Imagery that arrives later replaces the marker.
    store.put(5, 6, 15, "BI", JPEG)
    store.flush()
    assert store.status(5, 6, 15, "BI") == chunk_store.STATUS_DATA


def test_eviction_drops_oldest_and_skips_kept_rows(tmp_path):
    now = [1_000_000.0]
    s = ChunkStore(str(tmp_path), batch_seconds=0.01, clock=lambda: now[0])
    try:
        for i in range(10):
            now[0] += 10
            s.put(i, 0, 16, "BI", b"\xff\xd8\xff" + b"a" * 997, keep=(i == 0))
        s.flush()
        freed = s.evict_oldest(3000)
        assert freed >= 3000
        remaining = {col for col, *_ in s.iter_chunks()}
        # Row 0 is kept though oldest; rows 1, 2, 3 went.
        assert 0 in remaining
        assert remaining == {0, 4, 5, 6, 7, 8, 9}
        assert s.set_keep("BI", 16, [(0, 0)], keep=False) == 1
        s.evict_oldest(1)
        assert 0 not in {col for col, *_ in s.iter_chunks()}
    finally:
        s.close()


def test_put_keeps_an_existing_keep_flag(store):
    store.put(1, 1, 12, "BI", JPEG, keep=True)
    store.flush()
    store.put(1, 1, 12, "BI", _jpeg("new"))
    store.flush()
    rows = list(store.iter_chunks())
    assert rows[0][4] & chunk_store.FLAG_KEEP


def test_delete_many(store):
    for i in range(5):
        store.put(i, 0, 16, "BI", JPEG)
    store.flush()
    assert store.delete_many("BI", 16, [(0, 0), (1, 0)]) == 2
    assert set(store.get_many("BI", 16, [(i, 0) for i in range(5)])) == {(2, 0), (3, 0), (4, 0)}


def test_bands_split_by_region(store):
    # Vagar (62N 7W) and the Alps (46N 8E) at ZL16.
    store.put(31490, 17460, 16, "BI", JPEG)
    store.put(34300, 23200, 16, "BI", JPEG)
    store.flush()
    names = sorted(f for f in os.listdir(store.directory) if f.endswith(".sqlite"))
    assert len(names) == 2


def test_full_queue_drops_writes(tmp_path):
    s = ChunkStore(str(tmp_path), max_pending_bytes=150, batch_seconds=5.0)
    try:
        assert s.put(1, 1, 16, "BI", JPEG)
        assert not s.put(2, 1, 16, "BI", JPEG)
        assert s.stats["writes_dropped"] == 1
    finally:
        s.close()


def test_damaged_database_is_recreated(tmp_path):
    s = ChunkStore(str(tmp_path), batch_seconds=0.01)
    for i in range(400):
        s.put(i, 0, 16, "BI", _jpeg(i) * 30)
    s.flush()
    path = next(
        os.path.join(s.directory, f) for f in os.listdir(s.directory) if f.endswith(".sqlite")
    )
    s.close()
    # Checkpoint the WAL into the file, then cut the file in half.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    size = os.path.getsize(path)
    with open(path, "r+b") as handle:
        handle.truncate(size // 2)

    s = ChunkStore(str(tmp_path), batch_seconds=0.01)
    try:
        # Reads cost misses, never an exception.
        for _ in range(3):
            s.get_many("BI", 16, [(i, 0) for i in range(400)])
        s.get(399, 0, 16, "BI")
        # The band was set aside and a new one takes writes.
        s.put(1, 0, 16, "BI", JPEG)
        s.flush()
        assert s.get(1, 0, 16, "BI") == JPEG
        assert any(".corrupt-" in f for f in os.listdir(s.directory))
    finally:
        s.close()


def _writer_process(cache_dir, offset, count):
    s = ChunkStore(cache_dir, batch_seconds=0.01)
    for i in range(count):
        s.put(offset + i, 7, 16, "BI", b"\xff\xd8\xff" + bytes([i % 256]) * 50)
    s.flush()
    s.close()


def test_two_processes_write_the_same_store(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_writer_process, args=(str(tmp_path), offset, 300))
        for offset in (0, 1000)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    s = ChunkStore(str(tmp_path))
    try:
        keys = [(i, 7) for i in range(300)] + [(1000 + i, 7) for i in range(300)]
        assert len(s.get_many("BI", 16, keys)) == 600
    finally:
        s.close()


def test_old_rows_are_touched_on_read(tmp_path):
    now = [1_000_000.0]
    s = ChunkStore(str(tmp_path), batch_seconds=0.01, clock=lambda: now[0])
    try:
        s.put(1, 1, 16, "BI", JPEG)
        s.flush()
        now[0] += chunk_store._TOUCH_AFTER_SECONDS + 100
        assert s.get(1, 1, 16, "BI") == JPEG
        s.flush()
        fetched = [row[6] for row in s.iter_chunks()]
        assert fetched == [int(now[0])]
    finally:
        s.close()


def test_open_store_marks_a_folder_with_old_files(tmp_path):
    (tmp_path / "1_2_16_BI.jpg").write_bytes(JPEG)
    s = chunk_store.open_store(str(tmp_path))
    try:
        assert s.legacy_pending()
        assert chunk_store.open_store(str(tmp_path)) is s
    finally:
        chunk_store.close_all()


def test_on_commit_runs_after_the_batch_lands(store):
    landed = []
    store.put(3, 3, 16, "BI", JPEG, on_commit=lambda: landed.append(store.get(3, 3, 16, "BI")))
    store.flush()
    deadline = time.monotonic() + 2
    while not landed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert landed == [JPEG]

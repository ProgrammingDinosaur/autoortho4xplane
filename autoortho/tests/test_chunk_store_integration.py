"""Performance plan, item 3: the chunk store behind the old chunk paths.

Migration (read-through and the background converter), the disk budget,
cleanup after a texture is complete, and the imagery pipeline's chunks.
"""

import os
import threading
import time
import types

import pytest

from autoortho import chunk_migration, chunk_store


def _jpeg(tag) -> bytes:
    return b"\xff\xd8\xff" + str(tag).encode() * 40


@pytest.fixture
def store_on():
    previous = chunk_store.enabled()
    chunk_store.close_all()
    chunk_store.configure(True)
    yield
    chunk_store.close_all()
    chunk_store.configure(previous)


def _legacy_files(folder, count, zoom=16, maptype="BI"):
    paths = []
    for i in range(count):
        path = os.path.join(str(folder), chunk_store.chunk_file_name(1000 + i, 2000, zoom, maptype))
        with open(path, "wb") as handle:
            handle.write(_jpeg(i))
        paths.append(path)
    return paths


# ------------------------------------------------------------- read-through


def test_miss_reads_the_old_file_and_moves_it(tmp_path, store_on):
    paths = _legacy_files(tmp_path, 3)
    store = chunk_store.open_store(str(tmp_path))
    assert store.legacy_pending()
    assert chunk_store.read_chunk_path(paths[0]) == _jpeg(0)
    store.flush()
    deadline = time.monotonic() + 2
    while os.path.exists(paths[0]) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not os.path.exists(paths[0])  # removed once committed
    assert store.get(1000, 2000, 16, "BI") == _jpeg(0)
    # Batch reads move the rest.
    found = chunk_store.read_chunk_paths(paths)
    assert set(found) == set(paths)
    store.flush()
    assert store.contains_many("BI", 16, [(1000, 2000), (1001, 2000), (1002, 2000)]) == {
        (1000, 2000), (1001, 2000), (1002, 2000)
    }


def test_no_read_through_once_the_folder_is_converted(tmp_path, store_on):
    store = chunk_store.open_store(str(tmp_path))  # an empty folder: no marker
    assert not store.legacy_pending()
    path = _legacy_files(tmp_path, 1)[0]  # appears later (not ours)
    assert chunk_store.read_chunk_path(path) is None


def test_store_off_uses_files(tmp_path):
    previous = chunk_store.enabled()
    chunk_store.configure(False)
    try:
        path = _legacy_files(tmp_path, 1)[0]
        assert chunk_store.read_chunk_path(path) == _jpeg(0)
        assert chunk_store.chunk_path_exists(path)
        assert not os.path.isdir(os.path.join(str(tmp_path), "chunks"))
    finally:
        chunk_store.configure(previous)


def test_lookup_reports_remembered_no_imagery(tmp_path, store_on):
    path = os.path.join(str(tmp_path), chunk_store.chunk_file_name(5, 5, 14, "BI"))
    assert chunk_store.lookup_chunk_path(path) == (None, False)
    assert chunk_store.mark_no_imagery_path(path)
    assert chunk_store.lookup_chunk_path(path) == (None, True)
    seen = set()
    chunk_store.read_chunk_paths([path], no_imagery_out=seen)
    assert seen == {path}


# --------------------------------------------------------------- converter


def test_converter_moves_everything_and_drops_the_marker(tmp_path, store_on):
    paths = _legacy_files(tmp_path, 50)
    (tmp_path / "1_1_16_BI.jpg").write_bytes(b"not a jpeg")
    store = chunk_store.open_store(str(tmp_path))
    kept = []
    converter = chunk_migration.LegacyChunkConverter(
        str(tmp_path), keep=lambda name: kept.append(name) or name.startswith("1000_"), batch=7
    )
    progress = converter.run()
    assert progress.finished
    assert progress.moved == 50 and progress.skipped == 1
    assert not store.legacy_pending(max_age=0)
    assert not any(os.path.exists(p) for p in paths)
    assert len(store.get_many("BI", 16, [(1000 + i, 2000) for i in range(50)])) == 50
    rows = {col: status for col, _r, _z, _m, status, _s, _f in store.iter_chunks()}
    assert rows[1000] & chunk_store.FLAG_KEEP
    assert not rows[1001] & chunk_store.FLAG_KEEP


def test_converter_resumes_after_a_stop(tmp_path, store_on):
    _legacy_files(tmp_path, 200)
    store = chunk_store.open_store(str(tmp_path))
    converter = chunk_migration.LegacyChunkConverter(str(tmp_path), batch=10)
    moved_before_stop = threading.Event()
    original = store.import_legacy

    def slow_import(path, data, keep=False):
        if converter.progress.moved >= 30:
            converter._stop.set()
            moved_before_stop.set()
        return original(path, data, keep=keep)

    store.import_legacy = slow_import
    converter.run()
    assert moved_before_stop.is_set()
    assert not converter.progress.finished
    assert store.legacy_pending(max_age=0)
    store.import_legacy = original
    remaining = len([n for n in os.listdir(tmp_path) if n.endswith(".jpg")])
    assert 0 < remaining < 200

    converter2 = chunk_migration.LegacyChunkConverter(str(tmp_path))
    assert converter2.run().finished
    assert len(store.get_many("BI", 16, [(1000 + i, 2000) for i in range(200)])) == 200


def test_progress_text():
    progress = chunk_migration.Progress(total=1000, moved=250)
    assert "25 %" in progress.text()
    progress.finished = True
    assert progress.text() == ""


# ------------------------------------------------------------- disk budget


def test_disk_budget_counts_and_evicts_store_rows(tmp_path, store_on):
    from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager

    store = chunk_store.open_store(str(tmp_path))
    for i in range(20):
        store.put(i, 0, 16, "BI", b"\xff\xd8\xff" + b"z" * 997)
    store.put(0, 0, 12, "BI", b"\xff\xd8\xff" + b"b" * 997, keep=True)  # base
    store.flush()
    manager = DiskBudgetManager(cache_dir=str(tmp_path), total_budget_mb=1, dds_budget_pct=40)
    # Kept rows are outside the budget.
    assert manager._scan_jpegs_size() == 20 * 1000
    manager.protect_jpeg = lambda name: name.startswith("3_0_16_")
    freed = manager._evict_oldest_jpegs(5000)
    assert freed >= 5000
    left = {(c, z) for c, _r, z, *_ in store.iter_chunks()}
    assert (0, 12) in left and (3, 16) in left


# ------------------------------------------------------------ texture cleanup


def test_cleanup_source_jpegs_deletes_store_rows(tmp_path, store_on):
    from autoortho.aopipeline import dynamic_dds_cache as ddc

    store = chunk_store.open_store(str(tmp_path))
    for c in range(16):
        for r in range(16):
            store.put(c, r, 16, "BI", _jpeg((c, r)))
    store.flush()
    previous = ddc.protect_jpeg
    ddc.protect_jpeg = lambda name: name == "0_0_16_BI.jpg"
    try:
        deleted = ddc.cleanup_source_jpegs(
            str(tmp_path), 0, 0, 16, 16, 16, 16, 16, "BI"
        )
    finally:
        ddc.protect_jpeg = previous
    assert deleted == 255
    assert [(c, r) for c, r, *_ in store.iter_chunks()] == [(0, 0)]


# ---------------------------------------------------------------- kept imagery


def test_kept_imagery_plan_and_download_use_the_store(tmp_path, store_on, monkeypatch):
    from autoortho import kept_imagery

    store = chunk_store.open_store(str(tmp_path))
    store.put(10, 10, 16, "BI", _jpeg("old"))
    store.flush()
    assert "10_10_16_BI.jpg" in chunk_store.present_chunk_names(str(tmp_path))

    class Response:
        status_code = 200
        headers = {}
        content = _jpeg("new")

        def close(self):
            pass

    session = types.SimpleNamespace(get=lambda url, headers=None, timeout=None: Response())
    monkeypatch.setattr(kept_imagery.provider_urls, "tile_url", lambda *a, **k: ("u", {}))
    status, size = kept_imagery.fetch_chunk(session, (11, 10, 16, "BI"), str(tmp_path))
    assert status == "ok"
    store.flush()
    rows = {c: s for c, _r, _z, _m, s, _sz, _f in store.iter_chunks()}
    assert rows[11] & chunk_store.FLAG_KEEP
    assert not os.path.exists(os.path.join(str(tmp_path), "11_10_16_BI.jpg"))


def test_cache_inventory_counts_store_rows(tmp_path, store_on):
    from autoortho import cache_inventory

    store = chunk_store.open_store(str(tmp_path))
    store.put(34300, 23200, 16, "BI", _jpeg("a"))
    store.put(34301, 23200, 16, "BI", _jpeg("b"))
    store.flush()
    inv = cache_inventory.CacheInventory()
    cache_inventory._scan_chunks(inv, str(tmp_path), lambda: False)
    assert inv.files == 2
    assert inv.chunk_zooms[16][0] == 2

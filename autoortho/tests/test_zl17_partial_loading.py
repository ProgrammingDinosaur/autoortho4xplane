import threading
import time

from autoortho import getortho, pydds
from autoortho.dds_manifest import RowBuildManifest, manifests_to_dict


def _grid(monkeypatch, tmp_path, width=32, height=32):
    monkeypatch.setattr(getortho, "_batch_read_cache_files", lambda paths: {})
    monkeypatch.setattr(getortho.Chunk, "get_cache", lambda self: False)
    return getortho.ChunkGrid(
        col=100,
        row=200,
        width=width,
        height=height,
        zoom=17,
        maptype="BI",
        cache_dir=str(tmp_path),
        tile_id="tile",
    )


def test_chunk_grid_materializes_only_requested_rows(monkeypatch, tmp_path):
    grid = _grid(monkeypatch, tmp_path)

    row = grid.ensure_rows(7, 7)

    assert len(grid) == 1024
    assert len(row) == 32
    assert len(grid.materialized()) == 32
    assert grid.get(7 * 32 + 4) is row[4]


def test_chunk_grid_concurrent_materialization_is_canonical(
    monkeypatch,
    tmp_path,
):
    grid = _grid(monkeypatch, tmp_path, width=8, height=8)
    results = []

    threads = [
        threading.Thread(
            target=lambda: results.append(grid.ensure_rows(2, 2))
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(grid.materialized()) == 8
    assert all(
        result[index] is results[0][index]
        for result in results
        for index in range(8)
    )


def test_cache_probe_coalesces_and_negative_entries_expire():
    coordinator = getortho.CacheProbeCoordinator(
        max_entries=128,
        negative_ttl=0.05,
    )
    calls = []
    release = threading.Event()
    results = []

    def reader():
        calls.append(1)
        release.wait(1)
        return b"jpeg"

    threads = [
        threading.Thread(
            target=lambda: results.append(
                coordinator.probe("/cache/chunk.jpg", reader)
            )
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.02)
    release.set()
    for thread in threads:
        thread.join()

    assert calls == [1]
    assert results == [b"jpeg"] * 6

    missing_calls = []
    assert coordinator.probe(
        "/cache/missing.jpg",
        lambda: missing_calls.append(1),
    ) is None
    assert coordinator.probe(
        "/cache/missing.jpg",
        lambda: missing_calls.append(1),
    ) is None
    assert len(missing_calls) == 1
    time.sleep(0.06)
    coordinator.probe(
        "/cache/missing.jpg",
        lambda: missing_calls.append(1),
    )
    assert len(missing_calls) == 2


def test_sparse_buffer_reads_real_and_fallback_segments():
    fallback = pydds.get_fallback_bytes(32, 8)
    sparse = pydds.SparseMipmapBuffer(
        32,
        blocksize=8,
        unit_size=8,
        total_units=4,
    )
    sparse.write_at(0, b"A" * 8)
    sparse.write_at(16, b"C" * 8)

    assert sparse.read_at(0, 32) == (
        b"A" * 8 + fallback[8:16] + b"C" * 8 + fallback[24:32]
    )
    assert sparse.coverage() == {0, 2}
    assert sparse.allocated_bytes() == 16
    assert not sparse.is_complete()


def test_sparse_buffer_preserves_bc3_alignment_across_gap():
    sparse = pydds.SparseMipmapBuffer(
        48,
        blocksize=16,
        unit_size=16,
        total_units=3,
    )
    sparse.write_at(16, b"R" * 16)

    expected = pydds.get_fallback_bytes(48, 16)
    assert sparse.read_at(8, 32) == (
        expected[8:16] + b"R" * 16 + expected[32:40]
    )


def test_mipmap_provenance_reports_mixed_row_bytes_exactly():
    grid = pydds.MipmapProvenanceGrid(4, 1)
    grid.set_row(
        0,
        [
            pydds.MipmapProvenance.EXACT_TARGET,
            pydds.MipmapProvenance.EXACT_TARGET,
            pydds.MipmapProvenance.LOWER_ZL_CACHE,
            pydds.MipmapProvenance.MISSING_COLOR,
        ],
    )

    summary = grid.summarize_bytes(0, 16, 16)

    assert summary == {
        pydds.MipmapProvenance.EXACT_TARGET: 8,
        pydds.MipmapProvenance.LOWER_ZL_CACHE: 4,
        pydds.MipmapProvenance.MISSING_COLOR: 4,
    }


def test_mipmap_provenance_partial_read_preserves_requested_length():
    grid = pydds.MipmapProvenanceGrid(3, 1)
    grid.set_indices(range(3), pydds.MipmapProvenance.EXACT_TARGET)

    summary = grid.summarize_bytes(2, 5, 10)

    assert sum(summary.values()) == 5
    assert summary[pydds.MipmapProvenance.EXACT_TARGET] == 5


def test_dds_read_at_is_stateless_and_exact():
    dds = pydds.DDS(512, 512, dxt_format="BC1")
    mm = dds.mipmap_list[0]
    row_size = mm.length // 2
    dds.ensure_sparse_mipmap(0, row_size, 2)
    dds.write_mipmap_at(0, 0, b"X" * row_size)

    reads = []
    threads = [
        threading.Thread(
            target=lambda offset=offset: reads.append(
                dds.read_at(offset, 4096)
            )
        )
        for offset in (0, 128, mm.startpos + row_size, dds.total_size - 4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(reads) == 4
    assert all(len(data) == 4096 for data in reads)
    assert dds.tell() == 0


def test_partial_row_calculation_does_not_expand_read(monkeypatch, tmp_path):
    tile = getortho.Tile(
        0,
        0,
        "BI",
        16,
        cache_dir=str(tmp_path),
        max_zoom=17,
    )
    mm = tile.dds.mipmap_list[0]
    bytes_per_chunk_row = mm.length // 32
    captured = []

    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    native = type("Native", (), {"build_partial_mipmap": object()})()
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: native)
    monkeypatch.setattr(
        tile,
        "_try_native_partial_mipmap_build",
        lambda mipmap, startrow, endrow, row_bytes, time_budget: (
            captured.append((mipmap, startrow, endrow, row_bytes)) or True
        ),
    )

    assert tile.get_bytes(
        mm.startpos + 7 * bytes_per_chunk_row + 32,
        4096,
    )

    assert captured == [(0, 7, 7, bytes_per_chunk_row)]
    assert tile.chunks == {}


def test_tile_serving_batches_mipmap_zero_provenance(monkeypatch, tmp_path):
    tile = getortho.Tile(
        0,
        0,
        "BI",
        16,
        cache_dir=str(tmp_path),
        max_zoom=17,
    )
    mm = tile.dds.mipmap_list[0]
    row_size = mm.length // tile.chunks_per_col
    tile._mm0_provenance.set_row(
        0,
        [
            pydds.MipmapProvenance.EXACT_TARGET
        ] * (tile.chunks_per_row // 2)
        + [
            pydds.MipmapProvenance.LOWER_ZL_CACHE
        ] * (tile.chunks_per_row // 2),
    )
    captured = {}
    monkeypatch.setattr(
        getortho,
        "bump_many",
        lambda counters: captured.update(counters),
    )

    tile._record_mipmap_provenance(mm.startpos, row_size)

    assert captured["mm0_served_exact_bytes"] == row_size // 2
    assert captured["mm0_served_lower_zl_bytes"] == row_size // 2
    assert captured["mm0_served_missing_bytes"] == 0
    assert captured["mm0_reads_before_predictive_complete"] == 1


def test_partial_row_ranges_cover_first_and_last_rows(tmp_path):
    tile = getortho.Tile(
        0,
        0,
        "BI",
        16,
        cache_dir=str(tmp_path),
        max_zoom=17,
    )

    for mipmap in range(tile.max_mipmap + 1):
        mm = tile.dds.mipmap_list[mipmap]
        first = tile._partial_row_range(
            mipmap,
            mm.startpos,
            1,
        )
        last = tile._partial_row_range(
            mipmap,
            mm.endpos - 1,
            1,
        )

        assert first[:2] == (0, 0)
        assert last[:2] == (last[3] - 1, last[3] - 1)
        assert first[2] * first[3] >= mm.length


def test_build_coordinator_allows_disjoint_rows_and_joins_overlap():
    coordinator = getortho.MipmapBuildCoordinator(32)
    deadline = time.monotonic() + 1

    assert coordinator.begin_partial(1, 1, deadline)[0] == "build"
    assert coordinator.begin_partial(3, 3, deadline)[0] == "build"

    result = []
    waiter = threading.Thread(
        target=lambda: result.append(
            coordinator.begin_partial(
                1,
                1,
                time.monotonic() + 1,
            )[0]
        )
    )
    waiter.start()
    time.sleep(0.02)
    coordinator.finish_partial(1, 1, 0, True)
    waiter.join()

    assert result == ["covered"]


def test_stale_partial_completion_does_not_clobber_full_build():
    coordinator = getortho.MipmapBuildCoordinator(32)
    deadline = time.monotonic() + 1

    partial_action, partial_revision = coordinator.begin_partial(
        1,
        1,
        deadline,
    )
    full_action, _full_revision = coordinator.begin_full(deadline)
    coordinator.finish_partial(
        1,
        1,
        partial_revision,
        partial_action == "build",
    )

    assert full_action == "build"
    assert coordinator.state == coordinator.FULL_BUILDING


def test_source_lease_tracks_builder_ownership(tmp_path):
    tile = getortho.Tile(
        0,
        0,
        "BI",
        16,
        cache_dir=str(tmp_path),
        max_zoom=17,
    )

    with tile._source_lease([b"jpeg", None]) as lease:
        assert lease.sources == (b"jpeg",)
        assert tile._source_lease_count == 1

    assert tile._source_lease_count == 0


def test_partial_cache_load_populates_only_persisted_rows(
    monkeypatch,
    tmp_path,
):
    tile = getortho.Tile(
        0,
        0,
        "BI",
        16,
        cache_dir=str(tmp_path),
        max_zoom=17,
    )
    row_size = tile.dds.mipmap_list[0].length // 32

    class Cache:
        @staticmethod
        def load_partial_rows(tile_id, max_zoom, current_tile):
            return {5: b"P" * row_size}

    monkeypatch.setattr(getortho, "dynamic_dds_cache", Cache())
    monkeypatch.setattr(
        getortho.CFG.autoortho,
        "persist_partial_dds_cache",
        True,
    )

    row_manifest = RowBuildManifest.create(
        tile_id=tile.id,
        target_zoom=tile.max_zoom,
        mipmap=0,
        row_index=5,
        build_generation=3,
        sources=bytes(
            [pydds.MipmapProvenance.EXACT_TARGET]
            * tile.chunks_per_row
        ),
        compressed_data=b"P" * row_size,
    )
    metadata = {
        "mm0_manifest": manifests_to_dict(
            {5: row_manifest},
            target_zoom=tile.max_zoom,
        ),
        "partial_mipmaps": {
            "0": {
                "unit": "chunk_row",
                "total": 32,
                "covered": [5],
                "degraded": [],
                "revision": 3,
            }
        }
    }
    tile._restore_provenance_from_metadata(metadata)

    assert tile._load_partial_dds_rows(metadata)

    mm = tile.dds.mipmap_list[0]
    assert isinstance(mm.buffer, pydds.SparseMipmapBuffer)
    assert mm.buffer.allocated_bytes() == row_size
    assert mm.read_at(5 * row_size, row_size, tile.dds.blocksize) == (
        b"P" * row_size
    )
    assert not mm.retrieved
    assert tile.chunks == {}
    assert tile._persisted_exact_rows == {5}
    assert set(tile._mm0_provenance.row(5)) == {
        int(pydds.MipmapProvenance.EXACT_TARGET)
    }


def test_partial_cache_reload_skipped_when_rows_already_held(
    monkeypatch,
    tmp_path,
):
    # get_bytes calls the loader on every read until the texture is complete;
    # re-reading and re-hashing rows the tile already holds made each read
    # slower than the last.
    tile = getortho.Tile(
        0,
        0,
        "BI",
        16,
        cache_dir=str(tmp_path),
        max_zoom=17,
    )
    loads = []

    class Cache:
        @staticmethod
        def load_partial_rows(tile_id, max_zoom, current_tile):
            loads.append(tile_id)
            return {}

    monkeypatch.setattr(getortho, "dynamic_dds_cache", Cache())
    monkeypatch.setattr(
        getortho.CFG.autoortho,
        "persist_partial_dds_cache",
        True,
    )
    tile._persisted_exact_rows.update({5, 6})

    def metadata(rows):
        return {
            "partial_mipmaps": {
                "0": {
                    "unit": "chunk_row",
                    "total": 32,
                    "row_revisions": {str(row): 1 for row in rows},
                }
            }
        }

    assert not tile._load_partial_dds_rows(metadata([5, 6]))
    assert loads == []

    # A row persisted elsewhere (not held by this tile) is still loaded.
    tile._load_partial_dds_rows(metadata([5, 6, 7]))
    assert loads == [tile.id]


def test_partial_v2_accepts_prepared_target_pixels(monkeypatch):
    from autoortho.aopipeline import AoDDS

    captured = {}

    def build_partial_mipmap(**kwargs):
        captured.update(kwargs)
        data = b"\x00" * (3 * 64 * 64 * 8)
        return AoDDS.PartialMipmapResult(
            success=True,
            bytes_written=len(data),
            data=data,
            pixel_width=3 * 256,
            pixel_height=256,
            elapsed_ms=0,
        )

    monkeypatch.setattr(AoDDS, "build_partial_mipmap", build_partial_mipmap)

    result = AoDDS.build_partial_mipmap_v2(
        [
            b"\xff\xd8jpeg",
            {
                "pixels": bytes([10, 20, 30, 255]) * (256 * 256),
                "mode": "RGBA",
                "width": 256,
                "height": 256,
            },
            None,
        ],
        chunks_width=3,
        chunks_height=1,
    )

    assert result.success
    assert captured["jpeg_datas"][0] == b"\xff\xd8jpeg"
    assert captured["jpeg_datas"][1] is None
    assert captured["jpeg_datas"][2] is None
    assert result.data != b"\x00" * len(result.data)


def test_live_row_read_queues_rest_of_level_once(monkeypatch, tmp_path):
    # X-Plane reads from the level it wants to the end of the file: the rows
    # after the one being read are queued at once, behind it, and only once.
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=17)
    submitted = []

    class Getter:
        @staticmethod
        def submit(chunk, *args, **kwargs):
            submitted.append((chunk.row, chunk.priority, chunk.prefetch))
            chunk.in_queue = True
            return True

    monkeypatch.setattr(getortho, "chunk_getter", Getter())
    monkeypatch.setattr(getortho.Tile, "_warm_fallback_mipmap_chunks", lambda self: None)
    grid = tile._get_chunk_grid(17)
    deadline = time.monotonic()  # nothing arrives: return at once

    tile._collect_row_chunk_jpegs(17, 0, 0, deadline=deadline)

    first_row = [s for s in submitted if s[0] == grid.row]
    ahead = [s for s in submitted if s[0] != grid.row]
    assert len(first_row) == grid.width
    assert all(p == getortho.PRIORITY_LIVE for _, p, _ in first_row)
    assert len(ahead) == grid.width * (grid.height - 1)
    assert all(p == getortho.PRIORITY_READ_AHEAD and not pf for _, p, pf in ahead)

    submitted.clear()
    promoted = []
    Getter.promote = staticmethod(lambda chunk, priority: promoted.append((chunk.row, priority)) or True)
    tile._collect_row_chunk_jpegs(17, 1, 1, deadline=deadline)
    assert submitted == []  # already queued
    # The row now being read is raised from read-ahead to live.
    assert promoted == [(grid.row + 1, getortho.PRIORITY_LIVE)] * grid.width


def test_prefetch_row_reads_do_not_read_ahead(monkeypatch, tmp_path):
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=17)
    submitted = []

    class Getter:
        @staticmethod
        def submit(chunk, *args, **kwargs):
            submitted.append(chunk.row)
            return True

    monkeypatch.setattr(getortho, "chunk_getter", Getter())
    grid = tile._get_chunk_grid(17)
    tile._collect_row_chunk_jpegs(
        17, 0, 0, deadline=time.monotonic(), priority=getortho.PRIORITY_PREFETCH
    )
    assert len(submitted) == grid.width


def test_chunk_getter_promote_moves_queued_chunk_ahead(monkeypatch, tmp_path):
    getter = getortho.ChunkGetter.__new__(getortho.ChunkGetter)
    from autoortho.prefetch import IndexedPriorityQueue
    getter.live_queue = IndexedPriorityQueue()
    getter.prefetch_queue = IndexedPriorityQueue()
    getter._async_stage = None
    chunks = [getortho.Chunk(i, 0, "BI", 17, cache_dir=str(tmp_path), skip_cache_check=True) for i in range(3)]
    for chunk, priority in zip(chunks, (getortho.PRIORITY_READ_AHEAD, 30, 10)):
        chunk.priority = priority
        getter.live_queue.put_nowait((chunk, (), {}), item_key=chunk.chunk_id, item_priority=priority)

    assert getter.promote(chunks[0], getortho.PRIORITY_LIVE) is True
    assert chunks[0].priority == getortho.PRIORITY_LIVE
    assert getter.live_queue.get_nowait()[0] is chunks[0]
    # Never lowers a chunk's priority.
    assert getter.promote(chunks[2], 40) is False
    assert chunks[2].priority == 10


def test_partial_build_threads_share_the_budget(monkeypatch):
    monkeypatch.setattr(getortho, "_native_cpu_budget", lambda: 32)
    monkeypatch.delenv("AO_PARTIAL_BUILD_THREADS", raising=False)
    # one 8192 px chunk row: 32 chunks, about eight per thread
    assert getortho._partial_build_threads(32) == 4
    assert getortho._partial_build_threads(3) == 1
    # never more than half the budget, so a second build always fits
    assert getortho._partial_build_threads(10_000) == 16
    monkeypatch.setenv("AO_PARTIAL_BUILD_THREADS", "64")
    assert getortho._partial_build_threads(32) == 32


def test_bounded_partial_builds_enter_the_gate_together(monkeypatch):
    """Four builds of two threads fit an eight-thread budget at once; a
    reservation of the whole budget used to admit only one."""
    monkeypatch.setattr(getortho, "_native_cpu_budget", lambda: 8)
    barrier = threading.Barrier(4, timeout=5)
    granted = []
    errors = []

    def build():
        try:
            with getortho._native_build_context(threads=2) as threads:
                granted.append(threads)
                barrier.wait()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    workers = [threading.Thread(target=build) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert not errors
    assert granted == [2, 2, 2, 2]


def test_partial_v2_forwards_thread_bound(monkeypatch):
    from autoortho.aopipeline import AoDDS

    captured = {}

    def build_partial_mipmap(**kwargs):
        captured.update(kwargs)
        data = b"\x00" * (64 * 64 * 8)
        return AoDDS.PartialMipmapResult(
            success=True, bytes_written=len(data), data=data,
            pixel_width=256, pixel_height=256, elapsed_ms=0,
        )

    monkeypatch.setattr(AoDDS, "build_partial_mipmap", build_partial_mipmap)
    AoDDS.build_partial_mipmap_v2([b"\xff\xd8jpeg"], 1, 1, max_threads=3)
    assert captured["max_threads"] == 3


def test_bounded_native_partial_build_matches_default():
    import os
    import pytest
    from autoortho.aopipeline import AoDDS

    if not AoDDS.partial_mipmap_supports_threads():
        pytest.skip("native library without aodds_build_partial_mipmap_mt")
    sample = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "testfiles", "test_tile_small.jpg",
    )
    jpeg = open(sample, "rb").read()
    sources = [jpeg] * 8 + [None] * 4  # 4 x 3 band with a missing row
    default = AoDDS.build_partial_mipmap(sources, 4, 3)
    assert default.success
    for threads in (1, 3):
        bounded = AoDDS.build_partial_mipmap(sources, 4, 3, max_threads=threads)
        assert bounded.success
        assert bounded.data == default.data

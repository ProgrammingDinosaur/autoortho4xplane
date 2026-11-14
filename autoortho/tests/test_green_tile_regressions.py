"""Regression tests for live mipmap-zero rows served as missing color.

Each test pins one defect that turned recoverable imagery into the configured
missing color (the "green tiles" reports against 3.0.0).
"""

import logging
import types

import pytest

from autoortho import getortho, pydds
from autoortho.aoimage import AoImage


@pytest.fixture
def no_dynamic_cache(monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)


def _tile(tmp_path, tile_zoom, max_zoom):
    return getortho.Tile(
        0,
        0,
        "BI",
        tile_zoom,
        cache_dir=str(tmp_path),
        max_zoom=max_zoom,
    )


# ---------------------------------------------------------------------------
# Cached complete images and partial row requests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tile_zoom, max_zoom, expected_full, expected_row",
    [
        (18, 17, 131072, 65536),     # airport ZL18 capped to ZL17 (2048 DDS)
        (16, 17, 2097152, 262144),   # ZL16 tile raised to ZL17 (8192 DDS)
    ],
)
def test_cached_complete_image_serves_partial_rows(
    no_dynamic_cache,
    tmp_path,
    caplog,
    tile_zoom,
    max_zoom,
    expected_full,
    expected_row,
):
    tile = _tile(tmp_path, tile_zoom, max_zoom)
    mipmap = 2
    mm = tile.dds.mipmap_list[mipmap]
    assert mm.length == expected_full
    width = tile.dds.width >> mipmap
    height = tile.dds.height >> mipmap

    img = AoImage.new("RGBA", (width, height), (10, 200, 10))
    with tile._lock:
        assert tile._cache_image(mipmap, (img, 0, 0, max_zoom - mipmap))

    with caplog.at_level(logging.ERROR):
        assert tile.get_bytes(mm.startpos, expected_row)

    assert not any(
        "length mismatch" in record.getMessage() for record in caplog.records
    )
    served = tile.dds.read_at(mm.startpos, expected_row)
    assert served != pydds.get_fallback_bytes(expected_row, tile.dds.blocksize)
    # The cached source image must survive the partial build.
    assert not img._freed


def test_cached_image_crop_returns_requested_rows_only(
    no_dynamic_cache,
    tmp_path,
):
    tile = _tile(tmp_path, 16, 16)
    mipmap = 1
    width = tile.dds.width >> mipmap
    height = tile.dds.height >> mipmap
    img = AoImage.new("RGBA", (width, height), (1, 2, 3))
    zoom = tile.max_zoom - mipmap

    cropped = tile._cached_image_for_request(
        mipmap,
        (img, 0, 0, zoom),
        1,
        2,
        zoom,
        False,
    )
    assert cropped is not img
    assert cropped.size == (width, 512)

    complete = tile._cached_image_for_request(
        mipmap,
        (img, 0, 0, zoom),
        0,
        None,
        zoom,
        False,
    )
    assert complete is img

    # A cached image composed at another zoom cannot serve row ranges.
    assert tile._cached_image_for_request(
        mipmap,
        (img, 0, 0, zoom - 1),
        1,
        2,
        zoom,
        False,
    ) is None


# ---------------------------------------------------------------------------
# Deadline cancellation must not leave permanent holes
# ---------------------------------------------------------------------------

def _chunk(tmp_path):
    return getortho.Chunk(
        10,
        20,
        "BI",
        17,
        cache_dir=str(tmp_path),
        skip_cache_check=True,
    )


def test_deadline_keeps_download_on_the_wire(tmp_path):
    chunk = _chunk(tmp_path)
    chunk.priority = getortho.PRIORITY_LIVE
    chunk.download_started.set()
    chunk._broker_request_id = "request"

    chunk.release_after_deadline()

    assert not chunk.cancelled
    assert not chunk.ready.is_set()
    # A retry must not compete with the next live read.
    assert chunk.priority >= getortho.PRIORITY_PREFETCH
    assert chunk.prefetch


def test_deadline_cancels_download_waiting_in_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "_get_http2_client", lambda: object())
    chunk = _chunk(tmp_path)
    chunk.download_started.set()
    chunk.in_flight = True  # backoff between broker attempts

    chunk.release_after_deadline()

    assert chunk.cancelled


def test_deadline_cancels_queued_download(tmp_path):
    chunk = _chunk(tmp_path)

    chunk.release_after_deadline()

    assert chunk.cancelled
    assert chunk.ready.is_set()


def test_cancelled_chunk_is_revived_for_a_later_request(tmp_path):
    chunk = _chunk(tmp_path)
    chunk.cancel()
    assert chunk.ready.is_set() and chunk.data == b""

    assert getortho._revive_for_request(chunk)

    assert not chunk.cancelled
    assert not chunk.ready.is_set()
    assert chunk.data is None


def test_permanent_failure_is_not_revived(tmp_path):
    chunk = _chunk(tmp_path)
    chunk.permanent_failure = True
    chunk.cancel()

    assert not getortho._revive_for_request(chunk)
    assert chunk.cancelled


def test_in_flight_cancelled_chunk_is_not_revived(tmp_path):
    chunk = _chunk(tmp_path)
    chunk.cancel()
    chunk.in_flight = True

    assert not getortho._revive_for_request(chunk)


# ---------------------------------------------------------------------------
# Native partial build success path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("startrow,row_count", [(0, 1), (1, 1), (2, 2), (15, 1)])
@pytest.mark.parametrize("fail_success_log", [False, True])
def test_native_partial_build_success_returns_true(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
    startrow,
    row_count,
    fail_success_log,
):
    tile = _tile(tmp_path, 16, 16)
    mm = tile.dds.mipmap_list[0]
    rows = tile.chunks_per_col
    bytes_per_chunk_row = mm.length // rows
    width = tile.chunks_per_row

    monkeypatch.setattr(
        tile,
        "_collect_row_chunk_jpegs",
        lambda *args, **kwargs: ([b"jpeg"] * width * row_count, width, row_count),
    )
    monkeypatch.setattr(tile, "_queue_partial_rows", lambda results: None)

    data = b"\x11" * bytes_per_chunk_row * row_count
    fake_native = types.SimpleNamespace(
        build_partial_mipmap=lambda **kwargs: types.SimpleNamespace(
            success=True,
            data=data,
            bytes_written=len(data),
            elapsed_ms=1.0,
            error=None,
        )
    )
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: fake_native)
    if fail_success_log:
        original_debug = getortho.log.debug
        def debug(message, *args, **kwargs):
            if "_try_native_partial_mipmap_build: SUCCESS" in message:
                raise RuntimeError("Simulated diagnostic failure after commit")
            return original_debug(message, *args, **kwargs)
        monkeypatch.setattr(getortho.log, "debug", debug)

    assert tile._try_native_partial_mipmap_build(
        0,
        startrow,
        startrow + row_count - 1,
        bytes_per_chunk_row,
        None,
    )
    assert tile.dds.read_at(
        mm.startpos + startrow * bytes_per_chunk_row, len(data)
    ) == data


# ---------------------------------------------------------------------------
# Rows served missing are rebuilt when the tile is opened again
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("native", [True, False], ids=["native", "python"])
def test_kernel_read_crossing_served_row_builds_new_row(
    no_dynamic_cache, tmp_path, monkeypatch, native
):
    """macFUSE reads 32 KiB across the 128-byte-offset DDS row boundary."""
    from autoortho import streaming_options
    from autoortho.dds_manifest import ComposedImageResult, MipmapSource, RowState

    # This pins row sealing across kernel reads. Its first read is a header
    # chunk, which the read classifier (test_read_classifier) answers without
    # building row zero, so the classifier is off here.
    original_flag = streaming_options.flag
    monkeypatch.setattr(
        streaming_options,
        "flag",
        lambda name: False if name == "read_classifier" else original_flag(name),
    )
    tile = _tile(tmp_path, 16, 16)
    mm = tile.dds.mipmap_list[0]
    row_bytes = mm.length // tile.chunks_per_col
    calls = []
    queued = []
    monkeypatch.setattr(tile, "_maybe_schedule_read_ahead", lambda *a: None)
    monkeypatch.setattr(tile, "_queue_partial_rows", lambda rows: queued.extend(rows))

    def compressed(row_count):
        calls.append(row_count)
        return b"\x11" * row_bytes if row_count == 1 else (
            b"\x99" * row_bytes + b"\x22" * row_bytes
        )

    if native:
        monkeypatch.setattr(
            tile, "_collect_row_chunk_jpegs",
            lambda zoom, start, end, *a, **kw: (
                [b"jpeg"] * tile.chunks_per_row * (end - start + 1),
                tile.chunks_per_row, end - start + 1,
            ),
        )
        def build(**kwargs):
            data = compressed(kwargs["chunks_height"])
            return types.SimpleNamespace(success=True, data=data,
                                         bytes_written=len(data), elapsed_ms=1.0)
        monkeypatch.setattr(getortho, "_get_native_dds", lambda: types.SimpleNamespace(
            build_partial_mipmap=build
        ))
    else:
        monkeypatch.setattr(getortho, "_get_native_dds", lambda: None)
        def image(mipmap, start, end, **kwargs):
            rows = end - start + 1
            img = types.SimpleNamespace(
                size=(tile.dds.width, 256 * rows), data_ptr=lambda: None,
                close=lambda: None,
            )
            return ComposedImageResult(
                img, bytes([MipmapSource.TARGET_PROVIDER]) * tile.chunks_per_row * rows
            )
        monkeypatch.setattr(tile, "get_img", image)
        monkeypatch.setattr(tile.dds, "compress", lambda w, h, ptr: compressed(h // 256))

    assert tile.read_dds_bytes(0, 16384)[128:] == b"\x11" * (16384 - 128)
    first_manifest = tile._mm0_rows.manifest(0)
    assert tile._mm0_rows.state(0) == RowState.SERVED_EXACT
    # Rebuilding row zero would change data already served to X-Plane.
    # The next kernel read must preserve it while installing the new row.
    offset = 507904
    old_bytes = mm.startpos + row_bytes - offset
    assert tile.read_dds_bytes(offset, 32768) == (
        b"\x11" * old_bytes + b"\x22" * (32768 - old_bytes)
    )
    assert tile._mm0_rows.manifest(0) == first_manifest
    assert tile._mm0_rows.state(1) == RowState.SERVED_EXACT
    assert tile.dds.read_at(mm.startpos + row_bytes, row_bytes) == b"\x22" * row_bytes
    assert calls == [1, 2]
    assert [r.manifest.row_index for r in queued] == [0, 1]


def _serve_missing_row(tile, row):
    mm = tile.dds.mipmap_list[0]
    row_bytes = mm.length // tile.chunks_per_col
    tile._read_with_mm0_sealing(mm.startpos + row * row_bytes, row_bytes)


def test_reopen_unseals_rows_served_as_missing(no_dynamic_cache, tmp_path):
    from autoortho.dds_manifest import RowState

    tile = _tile(tmp_path, 16, 16)
    coordinator = tile._get_mipmap_build_coordinator(0)
    coordinator.restore_coverage([0, 1])
    _serve_missing_row(tile, 0)
    assert tile._mm0_rows.state(0) == RowState.SERVED_MISSING

    assert tile.prepare_for_reopen() == 1

    assert tile._mm0_rows.state(0) == RowState.UNREQUESTED
    assert tile._mm0_rows.manifest(0) is None
    assert 0 not in coordinator.covered_rows
    assert 1 in coordinator.covered_rows
    assert not tile.dds.mipmap_list[0].retrieved


def test_row_coordinator_keeps_exact_rows_on_reopen():
    from autoortho.dds_manifest import RowStateCoordinator, RowState

    rows = RowStateCoordinator(3)
    rows._states[0] = RowState.SERVED_EXACT
    rows._states[1] = RowState.SERVED_DEGRADED
    rows._states[2] = RowState.READY_EXACT

    assert rows.unseal_for_reopen() == (1,)
    assert rows.state(0) == RowState.READY_EXACT
    assert rows.state(1) == RowState.READY_DEGRADED
    assert rows.state(2) == RowState.READY_EXACT


def test_build_coordinator_uncover_reopens_complete_mipmap():
    coordinator = getortho.MipmapBuildCoordinator(4)
    coordinator.restore_coverage(range(4))
    assert coordinator.state == coordinator.COMPLETE

    coordinator.uncover([2])

    assert coordinator.state == coordinator.IDLE
    action, _revision = coordinator.begin_partial(2, 2, 0)
    assert action == "build"


# ---------------------------------------------------------------------------
# Tile budget only advances while X-Plane is reading
# ---------------------------------------------------------------------------

def test_read_active_budget_pauses_between_reads(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(getortho.time, "monotonic", lambda: now[0])
    budget = getortho.ReadActiveTimeBudget(10.0)

    now[0] += 50.0  # header parsed, X-Plane busy elsewhere
    assert budget.elapsed == 0.0

    budget.begin_read()
    now[0] += 4.0
    budget.begin_read()  # overlapping read counts once
    now[0] += 2.0
    budget.end_read()
    budget.end_read()
    assert budget.elapsed == pytest.approx(6.0)

    now[0] += 120.0
    assert budget.elapsed == pytest.approx(6.0)
    assert not budget.exhausted

    budget.begin_read()
    now[0] += 5.0
    assert budget.exhausted
    budget.end_read()


def test_reopen_keeps_mipmap_unretrieved_until_rows_rebuilt(
    no_dynamic_cache,
    tmp_path,
):
    tile = _tile(tmp_path, 16, 16)
    coordinator = tile._get_mipmap_build_coordinator(0)
    coordinator.restore_coverage(range(coordinator.total_rows))
    _serve_missing_row(tile, 3)
    assert tile._mipmap_rows_all_covered(0)

    tile.prepare_for_reopen()

    # Bytes for every row may exist, but row 3 still needs its rebuild, so
    # the sparse buffer must not be promoted to a "retrieved" mipmap.
    assert not tile._mipmap_rows_all_covered(0)
    coordinator.restore_coverage([3])
    assert tile._mipmap_rows_all_covered(0)


# ---------------------------------------------------------------------------
# Lower-ZL fallback after a row deadline expired
# ---------------------------------------------------------------------------

def test_partial_fallback_resolves_after_row_deadline_expired(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
):
    budgets = []

    class Resolver:
        def resolve_with_source(self, *args, time_budget=None, **kwargs):
            budgets.append(time_budget.remaining)
            getortho.time.sleep(0.02)  # disk read and upscale
            return b"\x00" * (256 * 256 * 4), "lower_zl_cache"

        def close(self):
            pass

    tile = _tile(tmp_path, 16, 16)
    monkeypatch.setattr(tile, "_new_fallback_resolver", lambda level: Resolver())
    monkeypatch.setattr(tile, "get_fallback_level", lambda: 1)
    width = tile.chunks_per_row

    # The wait for target chunks spent the whole row deadline.
    deadline = getortho.time.monotonic() - 5.0
    sources, fallback, missing, provenance = tile._resolve_partial_sources(
        [None] * width,
        0,
        width,
        tile.max_zoom,
        deadline,
    )

    assert missing == []
    assert len(fallback) == width
    assert all(source is not None for source in sources)
    assert budgets[0] > 0.5
    assert set(provenance.values()) == {
        pydds.MipmapProvenance.LOWER_ZL_CACHE
    }


# ---------------------------------------------------------------------------
# Strict quality mode during X-Plane's initial scenery load
# ---------------------------------------------------------------------------

def test_strict_tile_budget_keeps_initial_load_extension(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
):
    from autoortho.prefetch import PrefetchQualityMode

    monkeypatch.setattr(
        getortho,
        "_prefetch_quality_mode",
        lambda: PrefetchQualityMode.STRICT_TARGET,
    )
    monkeypatch.setattr(
        getortho.CFG.autoortho, "strict_target_deadline_sec", 120.0
    )
    monkeypatch.setattr(
        getortho.datareftracker, "has_ever_connected", False, raising=False
    )

    budget = _tile(tmp_path, 16, 16)._get_or_create_tile_time_budget()
    assert budget.max_seconds == pytest.approx(1200.0)

    monkeypatch.setattr(
        getortho.datareftracker, "has_ever_connected", True, raising=False
    )
    budget = _tile(tmp_path, 16, 16)._get_or_create_tile_time_budget()
    assert budget.max_seconds == pytest.approx(120.0)


def test_arcgis_requests_use_https(tmp_path):
    chunk = getortho.Chunk(
        10, 20, "ARC", 17, cache_dir=str(tmp_path), skip_cache_check=True
    )
    url, _headers, _server, _generation = chunk._build_tile_url(0)
    assert url.startswith("https://services.arcgisonline.com/")


# ---------------------------------------------------------------------------
# Fallback coordinates and local fallbacks after the budget is spent
# ---------------------------------------------------------------------------

def test_chunk_coords_use_max_zoom_units(tmp_path):
    # ZL16 tile raised to ZL17: tile-name coordinates double at max_zoom.
    tile = getortho.Tile(100, 200, "BI", 16, cache_dir=str(tmp_path), max_zoom=17)
    width = tile.chunks_per_row
    assert width == 32
    assert tile._max_zoom_chunk_coords(0) == (200, 400)
    assert tile._max_zoom_chunk_coords(width + 3) == (203, 401)


def _write_parent_jpegs(tmp_path, cols, rows, zoom, colour):
    img = AoImage.new("RGBA", (256, 256), colour)
    source = tmp_path / "parent_source.jpg"
    img.write_jpg(str(source), quality=95)
    data = source.read_bytes()
    for col in cols:
        for row in rows:
            (tmp_path / f"{col}_{row}_{zoom}_BI.jpg").write_bytes(data)


def test_best_chunk_uses_lower_zoom_chunk_held_in_memory(tmp_path):
    tile = _tile(tmp_path, 16, 16)
    img = AoImage.new("RGBA", (256, 256), (200, 30, 30))
    source = tmp_path / "memory_source.jpg"
    img.write_jpg(str(source), quality=95)
    grid = tile._get_chunk_grid(15)
    parent = grid.get(0)
    parent.data = source.read_bytes()
    parent.ready.set()
    source.unlink()

    upscaled = tile.get_best_chunk(grid.col * 2 + 1, grid.row * 2, 0, 16)

    assert upscaled is not None
    assert upscaled.size == (256, 256)


def test_get_img_uses_local_fallbacks_after_budget_exhausted(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
):
    tile = _tile(tmp_path, 16, 16)
    col, row, width, height, _zoom, _diff = tile._get_quick_zoom()
    colour = (30, 160, 200)
    _write_parent_jpegs(
        tmp_path,
        range(col >> 1, (col + width) >> 1),
        range(row >> 1, (row + height) >> 1),
        15,
        colour,
    )
    monkeypatch.setattr(
        getortho.chunk_getter, "submit", lambda *args, **kwargs: False
    )
    budget = getortho.TimeBudget(0.01)
    getortho.time.sleep(0.02)
    assert budget.exhausted

    result = tile.get_img(0, time_budget=budget, with_source_manifest=True)

    sources = result.source_manifest
    assert len(sources) == width * height
    assert int(getortho.MipmapSource.MISSING_COLOR) not in set(sources)


def test_image_with_missing_chunks_is_not_cached_as_fallback(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
):
    tile = _tile(tmp_path, 16, 16)
    monkeypatch.setattr(
        getortho.chunk_getter, "submit", lambda *args, **kwargs: False
    )
    budget = getortho.TimeBudget(0.01)
    getortho.time.sleep(0.02)
    missing = []

    tile.get_img(2, time_budget=budget, missing_out=missing)

    # Nothing arrived and nothing local exists: the composed mm2 is all
    # missing color and must not become an upscale source for mm0.
    assert 2 not in tile.imgs
    assert missing == [True]
    # Composing alone does not mark the tile; installing the pixels does.
    assert 2 not in tile._green_mipmaps


def test_speculative_partial_build_skips_incomplete_rows(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
):
    tile = _tile(tmp_path, 16, 16)
    mm = tile.dds.mipmap_list[0]
    bytes_per_chunk_row = mm.length // tile.chunks_per_col
    width = tile.chunks_per_row
    monkeypatch.setattr(
        tile,
        "_collect_row_chunk_jpegs",
        lambda *args, **kwargs: ([b"jpeg"] * (width - 1) + [None], width, 1),
    )
    built = []
    fake_native = types.SimpleNamespace(
        build_partial_mipmap=lambda **kwargs: built.append(kwargs),
        build_partial_mipmap_v2=lambda **kwargs: built.append(kwargs),
    )
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: fake_native)

    assert not tile._try_native_partial_mipmap_build(
        0,
        0,
        0,
        bytes_per_chunk_row,
        None,
        priority=getortho.PRIORITY_PREFETCH,
    )
    assert built == []
    coordinator = tile._get_mipmap_build_coordinator(0)
    assert 0 not in coordinator.covered_rows


# ---------------------------------------------------------------------------
# Lower mipmaps holding missing color are neither persisted nor kept
# ---------------------------------------------------------------------------

class _RecordingCache:
    def __init__(self):
        self.stores = []
        self.incremental = []

    def enqueue_store(self, *args, **kwargs):
        self.stores.append(args)
        return True

    def store_incremental(self, *args):
        self.incremental.append(args)


def test_green_lower_mipmap_blocks_complete_dds_persistence(
    tmp_path, monkeypatch
):
    cache = _RecordingCache()
    monkeypatch.setattr(getortho, "dynamic_dds_cache", cache)
    tile = _tile(tmp_path, 16, 16)
    for mm in tile.dds.mipmap_list:
        mm.retrieved = True
    tile._note_mipmaps_built([3], {3})

    assert not tile._maybe_persist_complete_dds()
    assert cache.stores == []


def test_incremental_store_skips_green_lower_mipmaps(tmp_path, monkeypatch):
    cache = _RecordingCache()
    monkeypatch.setattr(getortho, "dynamic_dds_cache", cache)
    monkeypatch.setattr(getortho, "_persist_partial_dds", True)
    tile = _tile(tmp_path, 16, 16)
    for mm in tile.dds.mipmap_list[1:]:
        tile.dds.replace_mipmap_dense(mm.idx, b"\x01" * mm.length)
    tile._note_mipmaps_built([2], {2})

    tile._try_incremental_dds_store(1)

    stored = cache.incremental[0][-2]
    assert 1 in stored and 3 in stored
    assert 2 not in stored


def test_clean_rebuild_clears_green_mark(tmp_path):
    tile = _tile(tmp_path, 16, 16)
    tile._note_mipmaps_built([2, 3], {2, 3})
    tile._note_mipmaps_built([2], set())
    assert tile._green_mipmaps == {3}


def test_reopen_rebuilds_green_lower_mipmaps(no_dynamic_cache, tmp_path):
    tile = _tile(tmp_path, 16, 16)
    tile.dds.mipmap_list[2].retrieved = True
    tile.dds.mipmap_list[3].retrieved = True
    tile._note_mipmaps_built([2], {2})

    assert tile.prepare_for_reopen() == 1

    assert not tile.dds.mipmap_list[2].retrieved
    assert tile.dds.mipmap_list[3].retrieved


# ---------------------------------------------------------------------------
# fallback_level=full downloads lower-zoom parents for the live row path
# ---------------------------------------------------------------------------

def _parent_jpeg(tmp_path):
    source = tmp_path / "parent.jpg"
    AoImage.new("RGBA", (256, 256), (70, 140, 90)).write_jpg(str(source))
    data = source.read_bytes()
    source.unlink()
    return data


def _network_fallback_tile(tmp_path, monkeypatch, level, submitted):
    tile = _tile(tmp_path, 16, 16)
    monkeypatch.setattr(tile, "get_fallback_level", lambda: level)
    jpeg = _parent_jpeg(tmp_path)

    def submit(chunk, *args, **kwargs):
        submitted.append((chunk.col, chunk.row, chunk.zoom, chunk.priority))
        chunk.data = jpeg
        chunk.ready.set()
        return True

    monkeypatch.setattr(getortho.chunk_getter, "submit", submit)
    return tile


def test_full_fallback_downloads_parent_for_live_row(
    no_dynamic_cache, tmp_path, monkeypatch
):
    submitted = []
    tile = _network_fallback_tile(tmp_path, monkeypatch, 2, submitted)
    width = tile.chunks_per_row
    jpegs = [b"jpeg"] * width
    jpegs[3] = None

    sources, fallback, missing, provenance = tile._resolve_partial_sources(
        jpegs, 0, width, 16, getortho.time.monotonic() - 1.0
    )

    grid = tile._get_chunk_grid(16)
    child = grid.get(3)
    assert submitted == [
        (child.col >> 1, child.row >> 1, 15, getortho.PRIORITY_LIVE)
    ]
    assert missing == [] and fallback == [3]
    assert provenance[3] == pydds.MipmapProvenance.CASCADE_NETWORK_FALLBACK


@pytest.mark.parametrize("level, exhausted", [(1, False), (2, True)])
def test_network_fallback_needs_full_level_and_budget(
    no_dynamic_cache, tmp_path, monkeypatch, level, exhausted
):
    submitted = []
    tile = _network_fallback_tile(tmp_path, monkeypatch, level, submitted)
    monkeypatch.setattr(tile, "get_fallback_extends_budget", lambda: False)
    budget = getortho.TimeBudget(0.0 if exhausted else 60.0)
    width = tile.chunks_per_row
    jpegs = [b"jpeg"] * width
    jpegs[3] = None

    _sources, _fallback, missing, _provenance = tile._resolve_partial_sources(
        jpegs, 0, width, 16, getortho.time.monotonic() - 1.0, budget
    )

    assert submitted == []
    assert missing == [3]

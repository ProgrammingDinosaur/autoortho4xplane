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

def test_native_partial_build_success_returns_true(
    no_dynamic_cache,
    tmp_path,
    monkeypatch,
):
    tile = _tile(tmp_path, 16, 16)
    mm = tile.dds.mipmap_list[0]
    rows = tile.chunks_per_col
    bytes_per_chunk_row = mm.length // rows
    width = tile.chunks_per_row

    monkeypatch.setattr(
        tile,
        "_collect_row_chunk_jpegs",
        lambda *args, **kwargs: ([b"jpeg"] * width, width, 1),
    )
    monkeypatch.setattr(tile, "_queue_partial_rows", lambda results: None)

    data = b"\x11" * bytes_per_chunk_row
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

    assert tile._try_native_partial_mipmap_build(
        0,
        0,
        0,
        bytes_per_chunk_row,
        None,
    )
    assert tile.dds.read_at(mm.startpos, bytes_per_chunk_row) == data


# ---------------------------------------------------------------------------
# Rows served missing are rebuilt when the tile is opened again
# ---------------------------------------------------------------------------

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

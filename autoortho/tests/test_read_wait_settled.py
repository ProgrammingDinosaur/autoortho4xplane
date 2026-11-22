"""Performance plan, item 1: reads stop waiting for chunks that have settled."""

import threading
import time
import types

import pytest

from autoortho import getortho


# ------------------------------------------------------------- the helper


def _chunk(tmp_path, col=0, row=0, zoom=13):
    return getortho.Chunk(
        col, row, "BI", zoom, cache_dir=str(tmp_path), skip_cache_check=True
    )


def test_returns_at_once_when_every_chunk_already_settled(tmp_path):
    chunks = [_chunk(tmp_path, col=i) for i in range(3)]
    for c in chunks:
        c.ready.set()
    start = time.monotonic()
    assert getortho.wait_for_chunks_settled(chunks, time.monotonic() + 60)
    assert time.monotonic() - start < 0.05


def test_wakes_when_last_chunk_settles_without_data(tmp_path):
    chunks = [_chunk(tmp_path, col=i) for i in range(2)]
    chunks[0].data = b"jpeg"
    chunks[0].ready.set()

    def settle():
        chunks[1].data = b""
        chunks[1].permanent_failure = True
        chunks[1].ready.set()

    threading.Timer(0.03, settle).start()
    start = time.monotonic()
    assert getortho.wait_for_chunks_settled(chunks, time.monotonic() + 60)
    assert time.monotonic() - start < 0.5


def test_gives_up_at_the_deadline(tmp_path):
    chunk = _chunk(tmp_path)
    start = time.monotonic()
    assert not getortho.wait_for_chunks_settled([chunk], time.monotonic() + 0.1)
    assert 0.09 <= time.monotonic() - start < 0.5


def test_plain_events_still_settle_through_the_backstop():
    chunk = types.SimpleNamespace(ready=threading.Event())
    threading.Timer(0.02, chunk.ready.set).start()
    start = time.monotonic()
    assert getortho.wait_for_chunks_settled([chunk], time.monotonic() + 5)
    assert time.monotonic() - start < 0.5


# ------------------------------------------- through the native level build


@pytest.fixture
def tile(tmp_path, monkeypatch):
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    t = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    yield t
    t.close()


def _wire_level_build(tile, tmp_path, monkeypatch, settle):
    """Set up a mipmap 3 build (four ZL13 chunks), three already on hand.

    ``settle(chunk)`` runs when the fourth chunk is submitted for download.
    Returns (chunks, builds, bumps).
    """
    mipmap = 3
    zoom = tile.max_zoom - mipmap
    chunks = [_chunk(tmp_path, col=i % 2, row=i // 2, zoom=zoom) for i in range(4)]
    for c in chunks[:3]:
        c.data = b"jpeg-%d" % id(c)
        c.ready.set()
    builds = []

    def build_single_mipmap(jpegs, **kwargs):
        builds.append(list(jpegs))
        return types.SimpleNamespace(success=False, error="test stops here")

    monkeypatch.setattr(
        getortho, "_get_native_dds",
        lambda: types.SimpleNamespace(build_single_mipmap=build_single_mipmap),
    )
    monkeypatch.setattr(
        tile, "_create_chunks",
        lambda z: types.SimpleNamespace(ensure_all=lambda: chunks),
    )
    monkeypatch.setattr(tile, "_probe_chunk_cache_ratio", lambda z: 1.0)
    monkeypatch.setattr(getortho, "_batch_read_cache_files", lambda paths: {})
    monkeypatch.setattr(tile, "_row_wait_seconds", lambda *a, **k: 60.0)
    monkeypatch.setattr(
        getortho, "chunk_getter",
        types.SimpleNamespace(submit=lambda c: threading.Timer(0.03, settle, (c,)).start()),
    )
    bumps = []
    real_bump = getortho.bump
    monkeypatch.setattr(
        getortho, "bump", lambda name, *a, **k: (bumps.append(name), real_bump(name, *a, **k))
    )
    return mipmap, chunks, builds, bumps


def test_chunk_with_no_imagery_ends_the_wait(tile, tmp_path, monkeypatch):
    def no_imagery(chunk):
        chunk.data = b""
        chunk.permanent_failure = True
        chunk.failure_reason = "no_imagery"
        chunk.ready.set()

    mipmap, _chunks, builds, bumps = _wire_level_build(
        tile, tmp_path, monkeypatch, no_imagery
    )
    budget = types.SimpleNamespace(exhausted=False, remaining=60.0)
    start = time.monotonic()
    assert tile._try_native_mipmap_build(mipmap, time_budget=budget) is False
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"waited {elapsed:.2f}s for a settled chunk"
    assert "native_mipmap_threshold_miss" in bumps
    assert "read_wait_ended_all_settled" in bumps
    assert builds == []


def test_chunk_that_settles_with_data_is_used(tile, tmp_path, monkeypatch):
    def arrives(chunk):
        chunk.data = b"late-jpeg"
        chunk.ready.set()

    mipmap, chunks, builds, bumps = _wire_level_build(
        tile, tmp_path, monkeypatch, arrives
    )
    budget = types.SimpleNamespace(exhausted=False, remaining=60.0)
    start = time.monotonic()
    tile._try_native_mipmap_build(mipmap, time_budget=budget)
    assert time.monotonic() - start < 0.5
    assert len(builds) == 1
    assert builds[0][3] == b"late-jpeg"
    assert "native_mipmap_threshold_miss" not in bumps
    assert "read_wait_ended_all_settled" not in bumps

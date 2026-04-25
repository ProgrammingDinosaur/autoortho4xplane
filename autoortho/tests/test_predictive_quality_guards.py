import threading
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autoortho import getortho


class FakeChunk:
    def __init__(self, chunk_id, data=None):
        self.chunk_id = chunk_id
        self.tile_id = "tile"
        self.ready = threading.Event()
        self.data = data
        self.permanent_failure = False
        self.failure_reason = None
        self.fetchtime = None
        self.url = None
        self.in_queue = False
        self.in_flight = False
        self.cancelled = False

    def __lt__(self, other):
        return self.chunk_id < other.chunk_id


class FakeTile:
    id = "tile"
    max_zoom = 16

    def __init__(self, chunks):
        self.chunks = {self.max_zoom: chunks}


def test_completion_tracker_requires_all_native_chunks_resolved():
    calls = []
    chunks = [FakeChunk("a"), FakeChunk("b")]
    tile = FakeTile(chunks)
    tracker = getortho.TileCompletionTracker(
        on_tile_complete=lambda *args, **kwargs: calls.append((args, kwargs))
    )

    tracker.start_tracking(tile, tile.max_zoom)

    chunks[0].data = b"jpeg-a"
    chunks[0].ready.set()
    tracker.notify_chunk_ready(tile.id, chunks[0])
    assert calls == []

    chunks[1].ready.set()
    tracker.notify_chunk_ready(tile.id, chunks[1])
    assert len(calls) == 1


def test_collect_healing_jpegs_uses_cache_side_effect_bytes():
    jpeg_bytes = b"\xff\xd8\xffcached"

    class CacheChunk(FakeChunk):
        def get_cache(self):
            self.data = jpeg_bytes
            return True

    tile = FakeTile([CacheChunk("a")])

    assert getortho._collect_healing_jpegs(tile, [0]) == {0: jpeg_bytes}


def test_chunk_getter_fans_out_duplicate_download_results():
    getortho.ChunkGetter._queued_chunk_ids.clear()
    getortho.ChunkGetter._queued_chunk_waiters.clear()

    getter = getortho.ChunkGetter(0)
    original = FakeChunk("same")
    duplicate = FakeChunk("same")

    getter.submit(original)
    getter.submit(duplicate)

    waiters = getortho.ChunkGetter._queued_chunk_waiters.pop("same")
    original.data = b"\xff\xd8\xffnative"
    original.ready.set()
    getter._complete_duplicate_waiters(original, waiters)

    assert duplicate.ready.is_set()
    assert duplicate.data == original.data
    assert duplicate.in_queue is False

    getortho.ChunkGetter._queued_chunk_ids.clear()
    getortho.ChunkGetter._queued_chunk_waiters.clear()

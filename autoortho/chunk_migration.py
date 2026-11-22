"""Move chunk files of the old layout into the packed chunk store.

Performance plan, item 3. A cache folder of the old layout can hold a million
``{col}_{row}_{zoom}_{maptype}.jpg`` files. Nothing waits for them: while the
store's legacy marker is present, a chunk the store misses is read from its
old file by exact name and moved over (``chunk_store.read_chunk_path``). This
converter moves the rest in the background while no flight is mounted, and
removes the marker once the folder holds no chunk files.

Stopping is cheap and safe: a file is deleted only after its row is committed,
so the next run picks up exactly what is left.

No Qt and no getortho import here.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

if __package__:
    from . import chunk_store
else:  # running from inside the autoortho folder
    import chunk_store

log = logging.getLogger(__name__)


@dataclass
class Progress:
    total: int = 0          # chunk files found when the run started
    moved: int = 0          # moved into the store
    skipped: int = 0        # unreadable or not JPEG (removed)
    running: bool = False
    finished: bool = False

    @property
    def share(self) -> float:
        return min(1.0, (self.moved + self.skipped) / self.total) if self.total else 0.0

    def text(self) -> str:
        if self.finished:
            return ""
        if not self.total:
            return "Preparing the imagery cache for the new format"
        return (
            "Moving the imagery cache to the new format: "
            f"{self.share * 100:.0f} % ({self.moved + self.skipped:,} of "
            f"{self.total:,} chunks)"
        )


class LegacyChunkConverter:
    """Converts one cache folder; ``start`` and ``stop`` may be called repeatedly."""

    def __init__(
        self,
        cache_dir: str,
        *,
        keep: Optional[Callable[[str], bool]] = None,
        batch: int = 256,
        pause_seconds: float = 0.0,
        store: Optional[chunk_store.ChunkStore] = None,
    ):
        self.cache_dir = str(cache_dir)
        self.keep = keep
        self.batch = max(1, int(batch))
        self.pause_seconds = max(0.0, float(pause_seconds))
        self._store = store
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.progress = Progress()

    @property
    def store(self) -> chunk_store.ChunkStore:
        if self._store is None:
            self._store = chunk_store.open_store(self.cache_dir)
        return self._store

    def needed(self) -> bool:
        return self.store.legacy_pending(max_age=0.0)

    def start(self) -> bool:
        """Run in a background thread when old files remain. True if started."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            if not self.needed():
                self.progress.finished = True
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self.run, name="ChunkStoreConverter", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 30.0) -> None:
        """Stop and wait until the store has committed what was read."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _names(self):
        try:
            with os.scandir(self.cache_dir) as entries:
                for entry in entries:
                    if chunk_store.parse_chunk_name(entry.name) is not None:
                        yield entry.name
        except OSError as exc:
            log.warning("Chunk converter could not list %s: %s", self.cache_dir, exc)

    def run(self) -> Progress:
        """Convert in the calling thread until done or stopped."""
        store = self.store
        progress = self.progress
        progress.running = True
        progress.finished = False
        progress.moved = progress.skipped = 0
        started = time.monotonic()
        try:
            progress.total = sum(1 for _ in self._names())
            log.info(
                "Moving %d chunk files of %s into the chunk store",
                progress.total,
                self.cache_dir,
            )
            pending: list[str] = []
            for name in self._names():
                if self._stop.is_set():
                    break
                pending.append(name)
                if len(pending) >= self.batch:
                    self._move(store, pending)
                    pending = []
                    if self.pause_seconds:
                        self._stop.wait(self.pause_seconds)
            if pending and not self._stop.is_set():
                self._move(store, pending)
            store.flush(timeout=120.0)
            if not self._stop.is_set() and not chunk_store.folder_has_legacy_chunks(
                self.cache_dir
            ):
                store.set_legacy_pending(False)
                progress.finished = True
                log.info(
                    "Chunk store conversion finished: %d moved, %d skipped in %.0f s",
                    progress.moved,
                    progress.skipped,
                    time.monotonic() - started,
                )
        except Exception:
            log.exception("Chunk store conversion failed; it resumes next time")
        finally:
            progress.running = False
        return progress

    def _move(self, store: chunk_store.ChunkStore, names: list[str]) -> None:
        for name in names:
            if self._stop.is_set():
                return
            path = os.path.join(self.cache_dir, name)
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if not chunk_store.is_jpeg(data):
                # A truncated or foreign file: the old reader rejected it too.
                try:
                    os.remove(path)
                except OSError:
                    pass
                self.progress.skipped += 1
                continue
            keep = False
            if self.keep is not None:
                try:
                    keep = bool(self.keep(name))
                except Exception:
                    keep = False
            while not store.import_legacy(path, data, keep=keep):
                # The write queue is full: let the writer catch up.
                if self._stop.is_set() or store.closed:
                    return
                store.flush(timeout=5.0)
            self.progress.moved += 1

"""Texture names for the squares X-Plane holds or will load next.

X-Plane opens every texture a DSF's ``.ter`` files name when it loads that
DSF, so every one must exist as a file. Creating them for a whole region is
too slow (Europe: 420,000 textures), so names exist only for X-Plane's
window plus one ring (``window.RING``). Built textures always stay: they
are the warm cache, and the disk budget handles them.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

RETRY_FIRST_S = 10.0
RETRY_MAX_S = 300.0


class Materializer:
    def __init__(self, catalogue, store, size_of):
        """``size_of(catalogue, index) -> (top_px, fmt)``."""
        self.catalogue = catalogue
        self.store = store
        self.size_of = size_of
        self.present = set()        # squares whose names all exist
        self._retry = {}            # square -> (next_try, delay)
        self._lock = threading.Lock()
        self.counters = {"files_names_made": 0, "files_names_removed": 0,
                         "files_names_failed": 0}

    def make_square(self, square):
        made = failed = 0
        for cat, indices in self.catalogue.textures_in(square):
            for i in indices:
                name = cat.name(int(i))
                if self.store.is_built(cat.region, name):
                    continue
                top_px, fmt = self.size_of(cat, int(i))
                if self.store.make_unbuilt(cat.region, name, top_px, fmt):
                    made += 1
                else:
                    failed += 1
        return made, failed

    def release_square(self, square):
        removed = 0
        for cat, indices in self.catalogue.textures_in(square):
            for i in indices:
                if self.store.remove_if_unbuilt(cat.region, cat.name(int(i))):
                    removed += 1
        return removed

    def sync(self, wanted, release=(), allow_release=True, deadline=None, now=None):
        """Make names for ``wanted`` (ordered, most urgent first).

        Releases names of ``release`` squares (only unbuilt ones) when
        allowed. Returns the squares of ``wanted`` still missing.
        """
        now = time.monotonic() if now is None else now
        missing = []
        for square in wanted:
            with self._lock:
                if square in self.present:
                    continue
                retry = self._retry.get(square)
            if retry and retry[0] > now:
                missing.append(square)
                continue
            if deadline is not None and time.monotonic() > deadline:
                missing.append(square)
                continue
            made, failed = self.make_square(square)
            self.counters["files_names_made"] += made
            if failed:
                self.counters["files_names_failed"] += failed
                delay = RETRY_FIRST_S if not retry else min(RETRY_MAX_S, retry[1] * 2)
                with self._lock:
                    self._retry[square] = (now + delay, delay)
                log.warning("Square %s: %d texture names could not be made; retry in %.0fs",
                            square, failed, delay)
                missing.append(square)
            else:
                with self._lock:
                    self.present.add(square)
                    self._retry.pop(square, None)
        if allow_release:
            for square in release:
                with self._lock:
                    if square not in self.present:
                        continue
                    self.present.discard(square)
                self.counters["files_names_removed"] += self.release_square(square)
        return missing

    def is_present(self, square):
        with self._lock:
            return square in self.present

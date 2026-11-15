"""Emergency brake for very low free RAM.

When the system's free RAM drops below max(1.5 GB, 8 %), or AutoOrtho's own
memory goes over the RAM limit (``[limits] ram_gb``), AutoOrtho stops adding
to memory pressure until it recovers: prefetch and background builds pause,
and the memory cache shrinks to half its limit. Live reads from X-Plane are
always served. The brake releases once free RAM has stayed above
max(2.5 GB, 12 %), and AutoOrtho's memory under 90 % of its limit, for 20
seconds, so it does not flap.

Each mount worker runs its own brake (no cross-process round trip), sampling
at most once a second from whichever thread asks first.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

GB = 1024 ** 3

ENGAGE_FREE_BYTES = int(1.5 * GB)
ENGAGE_FREE_SHARE = 0.08
RELEASE_FREE_BYTES = int(2.5 * GB)
RELEASE_FREE_SHARE = 0.12
RELEASE_AFTER_S = 20.0
OWN_RELEASE_SHARE = 0.9
SAMPLE_EVERY_S = 1.0
# While braking, the memory cache is held to this share of its limit.
CACHE_SHARE_WHILE_ENGAGED = 0.5


def _psutil_memory():
    import psutil

    memory = psutil.virtual_memory()
    return int(memory.available), int(memory.total)


class RamBrake:
    def __init__(
        self,
        *,
        enabled: bool = True,
        reader: Optional[Callable[[], tuple]] = None,
        clock: Callable[[], float] = time.monotonic,
        on_change: Optional[Callable[[bool, int, int], None]] = None,
    ):
        self.enabled = bool(enabled)
        self._reader = reader or _psutil_memory
        self._clock = clock
        self._on_change = on_change
        self._lock = threading.Lock()
        self._engaged = False
        self._last_sample = None
        self._clear_since = None
        self.activations = 0
        self.last_free = None
        self._own_used = None
        self._own_limit = None
        self.reason = ""

    def note_own_memory(self, used_bytes, limit_bytes) -> None:
        """AutoOrtho's own memory and the RAM limit (None: no limit)."""
        with self._lock:
            self._own_used = int(used_bytes) if used_bytes else None
            self._own_limit = int(limit_bytes) if limit_bytes else None

    @staticmethod
    def thresholds(total: int) -> tuple:
        """(engage below, release above) in bytes for this much RAM."""
        engage = max(ENGAGE_FREE_BYTES, int(total * ENGAGE_FREE_SHARE))
        release = max(RELEASE_FREE_BYTES, int(total * RELEASE_FREE_SHARE))
        return engage, max(release, engage)

    def sample(self, force: bool = False) -> bool:
        """Read free RAM (at most once a second) and update the state."""
        if not self.enabled:
            return False
        now = self._clock()
        with self._lock:
            if (
                not force
                and self._last_sample is not None
                and now - self._last_sample < SAMPLE_EVERY_S
            ):
                return self._engaged
            self._last_sample = now
        try:
            free, total = self._reader()
        except Exception as exc:
            log.debug("RAM brake: cannot read memory: %s", exc)
            return self._engaged
        engage_below, release_above = self.thresholds(total)
        changed = None
        with self._lock:
            self.last_free = free
            used, limit = self._own_used, self._own_limit
            own_over = bool(limit and used and used > limit)
            own_clear = not (limit and used and used > limit * OWN_RELEASE_SHARE)
            if not self._engaged:
                if free < engage_below or own_over:
                    self._engaged = True
                    self._clear_since = None
                    self.activations += 1
                    self.reason = "free" if free < engage_below else "limit"
                    changed = True
            elif free > release_above and own_clear:
                if self._clear_since is None:
                    self._clear_since = now
                elif now - self._clear_since >= RELEASE_AFTER_S:
                    self._engaged = False
                    self._clear_since = None
                    changed = False
            else:
                self._clear_since = None
            engaged = self._engaged
        if changed is not None:
            if changed and self.reason == "limit":
                log.warning(
                    "AutoOrtho uses %.1f GB, above its RAM limit of %.1f GB. "
                    "Pausing prefetch and background builds and shrinking the "
                    "memory cache until it is back under.",
                    (used or 0) / GB,
                    (limit or 0) / GB,
                )
            elif changed:
                log.warning(
                    "Low memory: %.1f GB free of %.0f GB. Pausing prefetch "
                    "and background builds and shrinking the memory cache "
                    "until it recovers.",
                    free / GB,
                    total / GB,
                )
            else:
                log.info(
                    "Memory recovered (%.1f GB free); prefetch and background "
                    "builds resume.",
                    free / GB,
                )
            if self._on_change is not None:
                try:
                    self._on_change(changed, free, total)
                except Exception:
                    log.debug("RAM brake listener failed", exc_info=True)
        return engaged

    @property
    def engaged(self) -> bool:
        return self.sample()

    def cache_limit(self, limit_bytes: int) -> int:
        """The memory cache limit to enforce now."""
        if self.sample():
            return max(512 * 1024 * 1024, int(limit_bytes * CACHE_SHARE_WHILE_ENGAGED))
        return limit_bytes


# ---------------------------------------------------------------- disk


DISK_SAMPLE_EVERY_S = 5.0
DISK_RELEASE_EXTRA = 1 * GB


def _disk_free(path):
    import shutil
    from pathlib import Path

    current = Path(str(path)).expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return shutil.disk_usage(current).free


class DiskBrake:
    """Stops new cache writes while the cache drive is nearly full.

    Reads keep being served (from memory, the cache already on disk and the
    network); only new cache entries are skipped. Engages below the safety
    margin and releases 1 GB above it, checking at most every 5 seconds.
    """

    def __init__(self, path, margin_bytes, *, enabled=True, reader=None,
                 clock=time.monotonic, on_change=None):
        self.path = path
        self.margin = int(margin_bytes)
        self.enabled = bool(enabled) and bool(path)
        self._reader = reader or _disk_free
        self._clock = clock
        self._on_change = on_change
        self._lock = threading.Lock()
        self._engaged = False
        self._last_sample = None
        self.activations = 0
        self.last_free = None

    def writes_allowed(self) -> bool:
        if not self.enabled:
            return True
        now = self._clock()
        with self._lock:
            if (
                self._last_sample is not None
                and now - self._last_sample < DISK_SAMPLE_EVERY_S
            ):
                return not self._engaged
            self._last_sample = now
        try:
            free = int(self._reader(self.path))
        except Exception as exc:
            log.debug("Disk brake: cannot read free space: %s", exc)
            return not self._engaged
        changed = None
        with self._lock:
            self.last_free = free
            if not self._engaged and free < self.margin:
                self._engaged = True
                self.activations += 1
                changed = True
            elif self._engaged and free > self.margin + DISK_RELEASE_EXTRA:
                self._engaged = False
                changed = False
            engaged = self._engaged
        if changed is True:
            log.warning(
                "Cache drive almost full (%.1f GB free). Not writing new cache "
                "entries until space is freed; scenery keeps streaming.",
                free / GB,
            )
        elif changed is False:
            log.info("Cache drive has room again (%.1f GB free).", free / GB)
        if changed is not None and self._on_change is not None:
            try:
                self._on_change(changed, free)
            except Exception:
                log.debug("Disk brake listener failed", exc_info=True)
        return not engaged

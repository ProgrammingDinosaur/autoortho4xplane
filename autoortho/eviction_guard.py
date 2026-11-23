"""Stops memory-driven tile eviction when it frees nothing.

The tile cache evicts by the process's memory (RSS) against the cache limit.
Much of that memory is not tiles (native pools, buffers, the interpreter,
freed memory the allocator keeps), so with a low limit the target can sit
below what dropping tiles can ever reach. Eviction then never converges:
seen in a user's flight as 20 tiles dropped every 1.4 s, 0 MB freed each
time, and 1,250 tiles X-Plane reopened right after having to be rebuilt.

After ``STALL_PASSES`` passes in a row that free less than ``MIN_FREED``,
memory eviction pauses for a back-off (30 s, doubling to 5 min while it keeps
stalling). It resumes early if memory grows ``REGROW_SHARE`` above where it
stalled, so real growth is still met. A pass that frees memory resets it.
Eviction by tile count is not affected.
"""

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

MB = 1024 * 1024
STALL_PASSES = 3
MIN_FREED = 16 * MB
BASE_BACKOFF_S = 30.0
MAX_BACKOFF_S = 300.0
REGROW_SHARE = 0.05


class EvictionStallGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 metric: Optional[Callable[..., None]] = None):
        self._clock = clock
        self._metric = metric or (lambda *_a, **_k: None)
        self._lock = threading.Lock()
        self._weak = 0
        self._backoff = BASE_BACKOFF_S
        self._until: Optional[float] = None
        self._mem_at_stall = 0
        self._warned = False
        self.stalls = 0

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._until is not None

    def allow(self, mem_bytes: int) -> bool:
        """Whether a memory eviction pass may run now."""
        with self._lock:
            if self._until is None:
                return True
            now = self._clock()
            regrown = mem_bytes > self._mem_at_stall * (1.0 + REGROW_SHARE)
            if now < self._until and not regrown:
                return False
            self._until = None
            log.info(
                "Memory eviction resumes (%s; memory %d MB)",
                "memory grew" if regrown else "back-off over",
                mem_bytes // MB,
            )
            return True

    def note_pass(self, freed_bytes: int, mem_bytes: int,
                  target_bytes: int = 0, tiles: int = 0) -> None:
        """Record what one memory eviction pass freed."""
        with self._lock:
            if freed_bytes >= MIN_FREED:
                self._weak = 0
                self._backoff = BASE_BACKOFF_S
                return
            self._weak += 1
            if self._weak < STALL_PASSES:
                return
            self._weak = 0
            backoff = self._backoff
            self._until = self._clock() + backoff
            self._mem_at_stall = int(mem_bytes)
            self._backoff = min(MAX_BACKOFF_S, backoff * 2)
            self.stalls += 1
            first = not self._warned
            self._warned = True
        self._metric("evict_stalled")
        text = (
            f"Memory eviction paused for {backoff:.0f}s: the last "
            f"{STALL_PASSES} passes freed under {MIN_FREED // MB} MB "
            f"(memory {mem_bytes // MB} MB, target {target_bytes // MB} MB, "
            f"{tiles} tiles). Dropping more tiles would only make X-Plane "
            "wait for them again."
        )
        if first:
            log.warning(text + " Consider a higher cache_mem_limit or a lower zoom level.")
        else:
            log.info(text)

"""When a mount worker that died mid-flight may be started again.

A worker can die while X-Plane is flying, for example when macFUSE drops the
mount ("fuse: reading channel: Invalid argument"). Without a restart every
texture in that region stays missing until streaming is stopped and started
by hand. One region is remounted a few times; a worker that keeps dying is a
real fault, and streaming stops with an error as before.
"""

import threading
import time

MAX_RESTARTS = 3
WINDOW_SECONDS = 600.0


class RestartPolicy:
    """Allow ``max_restarts`` restarts per mount within ``window_seconds``."""

    def __init__(self, max_restarts=MAX_RESTARTS, window_seconds=WINDOW_SECONDS,
                 clock=time.monotonic):
        self.max_restarts = max(0, int(max_restarts))
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._restarts = {}

    def allow(self, key):
        """Record one restart of ``key`` and say whether it may go ahead."""
        now = self._clock()
        with self._lock:
            recent = [
                t for t in self._restarts.get(key, ())
                if now - t < self.window_seconds
            ]
            if len(recent) >= self.max_restarts:
                self._restarts[key] = recent
                return False
            recent.append(now)
            self._restarts[key] = recent
            return True

    def count(self, key):
        now = self._clock()
        with self._lock:
            return sum(
                1 for t in self._restarts.get(key, ())
                if now - t < self.window_seconds
            )

    def reset(self):
        with self._lock:
            self._restarts.clear()

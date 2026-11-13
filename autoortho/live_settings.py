"""Live levers: setting values a running mount picks up without remounting.

The parent process (the governor, or Apply while streaming) publishes lever
values into the shared stats store under ``governor/levers``; each mount
worker polls it every 1.5 s and applies what changed. No new IPC: the stats
store is already shared and authenticated between the parent and its
workers, and in single-process mode it is simply local.

A lever's value lands in the worker's ``CFG`` namespace (so code that reads
the setting at call time sees it) and in any hook registered for it (so
pools, gates and budgets created at start resize). See
``docs/developer/settings-refactor-proposal.md`` section 8.

:class:`ResizableGate` is the semaphore the hooks resize: a drop-in for
``threading.BoundedSemaphore`` whose permit count can change while threads
hold permits.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

LEVERS_KEY = "governor/levers"
GENERATION_KEY = "governor/levers_generation"
POLL_EVERY_S = 1.5


class ResizableGate:
    """A bounded semaphore whose limit can change.

    Shrinking never takes permits away from holders: new acquirers wait until
    enough holders have released to get under the new limit. Releasing more
    than acquired raises ``ValueError``, like ``BoundedSemaphore``.
    """

    def __init__(self, limit: int):
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._held = 0

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def held(self) -> int:
        with self._cond:
            return self._held

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        with self._cond:
            if not blocking:
                if self._held < self._limit:
                    self._held += 1
                    return True
                return False
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while self._held >= self._limit:
                if deadline is None:
                    self._cond.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
            self._held += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._held <= 0:
                raise ValueError("ResizableGate released too many times")
            self._held -= 1
            self._cond.notify()

    def resize(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class LiveSettings:
    """Lever values shared from the parent to every mount worker.

    ``reader``/``writer`` default to the stats store (``aostats``); tests
    pass a dict. ``cfg`` is the process's config, whose namespaces receive
    each value.
    """

    def __init__(
        self,
        cfg=None,
        *,
        reader: Optional[Callable] = None,
        writer: Optional[Callable] = None,
        clock: Callable[[], float] = time.monotonic,
        poll_every: float = POLL_EVERY_S,
    ):
        if reader is None or writer is None:
            try:
                from autoortho.aostats import get_stat, set_stat
            except ImportError:
                from aostats import get_stat, set_stat
            reader = reader or get_stat
            writer = writer or set_stat
        self._cfg = cfg
        self._read = reader
        self._write = writer
        self._clock = clock
        self._poll_every = float(poll_every)
        self._lock = threading.Lock()
        self._values: Dict[str, object] = {}
        self._generation = None
        self._last_poll = None
        self._hooks: Dict[str, list] = {}
        self._thread = None
        self._stop = threading.Event()

    # -- worker side -----------------------------------------------------

    def on_change(self, name: str, hook: Callable[[object], None]) -> None:
        """Call ``hook(value)`` whenever lever ``name`` changes."""
        with self._lock:
            self._hooks.setdefault(name, []).append(hook)

    def get(self, name: str, default=None):
        """The live value of ``name``: published, else the config's."""
        self.refresh()
        with self._lock:
            if name in self._values:
                return self._values[name]
        if self._cfg is not None and "." in name:
            section, key = name.split(".", 1)
            namespace = getattr(self._cfg, section, None)
            if namespace is not None and hasattr(namespace, key):
                return getattr(namespace, key)
        return default

    def refresh(self, force: bool = False) -> Dict[str, object]:
        """Poll the shared store (at most every 1.5 s); apply changes.

        Returns the levers that changed in this poll.
        """
        now = self._clock()
        with self._lock:
            if (
                not force
                and self._last_poll is not None
                and now - self._last_poll < self._poll_every
            ):
                return {}
            self._last_poll = now
        try:
            generation = self._read(GENERATION_KEY)
        except Exception:
            return {}
        if not generation or generation == self._generation:
            return {}
        try:
            published = self._read(LEVERS_KEY) or {}
        except Exception:
            return {}
        if not isinstance(published, dict):
            return {}
        with self._lock:
            changed = {
                name: value for name, value in published.items()
                if self._values.get(name, object()) != value
            }
            self._values = dict(published)
            self._generation = generation
            hooks = {name: list(self._hooks.get(name, ())) for name in changed}
        for name, value in changed.items():
            self._apply_to_cfg(name, value)
            for hook in hooks.get(name, ()):
                try:
                    hook(value)
                except Exception:
                    log.exception("Live setting hook for %s failed", name)
        if changed:
            log.info(
                "Live settings: %s",
                ", ".join(f"{k}={v}" for k, v in sorted(changed.items())),
            )
        return changed

    def _apply_to_cfg(self, name, value):
        if self._cfg is None or "." not in name:
            return
        section, key = name.split(".", 1)
        namespace = getattr(self._cfg, section, None)
        if namespace is not None:
            try:
                setattr(namespace, key, value)
            except Exception:
                pass

    def start(self) -> None:
        """Poll in the background so hooks run even when nothing reads."""
        if self._thread is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self._poll_every):
                self.refresh(force=True)

        self._thread = threading.Thread(target=loop, name="LiveSettings", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    # -- parent side -----------------------------------------------------

    def publish(self, values: Dict[str, object]) -> None:
        """Set lever values for every worker (merged with earlier ones)."""
        if not values:
            return
        try:
            current = self._read(LEVERS_KEY) or {}
        except Exception:
            current = {}
        if not isinstance(current, dict):
            current = {}
        merged = dict(current)
        merged.update(values)
        self._write(LEVERS_KEY, merged)
        self._write(GENERATION_KEY, f"{time.time():.6f}-{len(merged)}")

    def clear(self) -> None:
        """Forget every published lever (at unmount)."""
        self._write(LEVERS_KEY, {})
        self._write(GENERATION_KEY, f"{time.time():.6f}-clear")

    def published(self) -> Dict[str, object]:
        try:
            current = self._read(LEVERS_KEY) or {}
        except Exception:
            return {}
        return dict(current) if isinstance(current, dict) else {}

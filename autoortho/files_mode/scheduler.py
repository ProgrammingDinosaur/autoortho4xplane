"""Build queue ordered by urgency (plan section 5.7).

The planner hands in a complete set of demands every pass; the queue is
replaced by it, so queued work is always ranked by the newest urgency and
textures no longer needed drop out. Ties go to the sharper level, then to
the nearer texture. A texture already building is not queued again unless
a sharper level is wanted.
"""

import heapq
import itertools
import logging
import threading
import time

log = logging.getLogger(__name__)

BACKOFF_FIRST_S = 5.0
BACKOFF_MAX_S = 60.0


class Scheduler:
    def __init__(self, build_fn, workers=2, paused=lambda: False,
                 under_pressure=lambda: False, name="AO-FilesBuild"):
        """``build_fn(demand) -> result`` with ``.ok`` and ``.refused``."""
        self.build_fn = build_fn
        self.paused = paused
        self.under_pressure = under_pressure
        self._heap = []
        self._queued = {}       # name -> demand
        self._in_flight = {}    # name -> level being built
        self._backoff = {}      # name -> (retry_at, delay)
        self._cond = threading.Condition()
        self._stop = False
        self._threads = []
        self._target_workers = max(1, int(workers))
        self._seq = itertools.count()
        self._name = name
        self.counters = {
            "files_builds": 0, "files_build_failures": 0, "files_build_s_total": 0.0,
        }
        self._recent = []   # completion timestamps for the build rate

    # -- workers -------------------------------------------------------
    def start(self):
        with self._cond:
            self._stop = False
            self._grow_locked()

    def _grow_locked(self):
        self._threads = [t for t in self._threads if t.is_alive()]
        used = {getattr(t, "ao_slot", -1) for t in self._threads}
        for slot in range(self._target_workers):
            if slot in used:
                continue
            t = threading.Thread(target=self._run, name=f"{self._name}-{slot}", daemon=True)
            t.ao_slot = slot
            self._threads.append(t)
            t.start()

    def set_workers(self, count):
        with self._cond:
            self._target_workers = max(1, int(count))
            if not self._stop:
                self._grow_locked()
            self._cond.notify_all()

    @property
    def workers(self):
        return self._target_workers

    def stop(self, timeout=5.0):
        with self._cond:
            self._stop = True
            self._heap.clear()
            self._queued.clear()
            self._cond.notify_all()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads = []

    # -- queue -----------------------------------------------------------
    def replace_plan(self, demands, now=None):
        """Make ``demands`` the queue (most urgent first)."""
        now = time.monotonic() if now is None else now
        heap = []
        queued = {}
        with self._cond:
            for d in demands:
                building = self._in_flight.get(d.name)
                if building is not None and building <= d.level:
                    continue
                back = self._backoff.get(d.name)
                if back is not None and back[0] > now:
                    continue
                prev = queued.get(d.name)
                if prev is not None and prev.level <= d.level and prev.urgency_s <= d.urgency_s:
                    continue
                queued[d.name] = d
            for d in queued.values():
                heap.append((d.urgency_s, d.level, d.distance_km, next(self._seq), d))
            heapq.heapify(heap)
            self._heap = heap
            self._queued = queued
            self._cond.notify_all()

    def add(self, demand):
        """Queue one demand on top of the current plan (start, refresh)."""
        with self._cond:
            building = self._in_flight.get(demand.name)
            if building is not None and building <= demand.level:
                return False
            self._queued[demand.name] = demand
            heapq.heappush(self._heap, (demand.urgency_s, demand.level, demand.distance_km,
                                        next(self._seq), demand))
            self._cond.notify_all()
            return True

    def _next_locked(self):
        pressure = False
        try:
            pressure = bool(self.under_pressure())
        except Exception:
            pressure = False
        while self._heap:
            urgency, level, _dist, _seq, d = self._heap[0]
            if self._queued.get(d.name) is not d:
                heapq.heappop(self._heap)  # superseded
                continue
            if pressure and (urgency > 0 or len(self._in_flight) >= 1):
                return None
            heapq.heappop(self._heap)
            del self._queued[d.name]
            return d
        return None

    def _run(self):
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    me = threading.current_thread()
                    if getattr(me, "ao_slot", 0) >= self._target_workers:
                        if me in self._threads:
                            self._threads.remove(me)
                        return
                    paused = False
                    try:
                        paused = bool(self.paused())
                    except Exception:
                        paused = False
                    demand = None if paused else self._next_locked()
                    if demand is not None:
                        self._in_flight[demand.name] = demand.level
                        break
                    self._cond.wait(0.25)
            started = time.monotonic()
            ok = False
            refused = False
            try:
                result = self.build_fn(demand)
                ok = bool(getattr(result, "ok", result))
                refused = bool(getattr(result, "refused", False))
            except Exception as exc:
                log.warning("Build of %s (level %s) failed: %s", demand.name, demand.level, exc)
            elapsed = time.monotonic() - started
            with self._cond:
                self._in_flight.pop(demand.name, None)
                if ok:
                    self._backoff.pop(demand.name, None)
                    self.counters["files_builds"] += 1
                    self.counters["files_build_s_total"] += elapsed
                    self._recent.append(time.monotonic())
                else:
                    self.counters["files_build_failures"] += 1
                    prev = self._backoff.get(demand.name)
                    delay = BACKOFF_FIRST_S if prev is None else min(BACKOFF_MAX_S, prev[1] * 2)
                    if refused:
                        delay = BACKOFF_FIRST_S if prev is None else delay
                    self._backoff[demand.name] = (time.monotonic() + delay, delay)
                self._cond.notify_all()

    # -- stats -----------------------------------------------------------
    def depth(self):
        with self._cond:
            return len(self._queued)

    def urgent_depth(self):
        with self._cond:
            return sum(1 for d in self._queued.values() if d.urgency_s <= 0)

    def in_flight(self):
        with self._cond:
            return dict(self._in_flight)

    def is_pending(self, name):
        with self._cond:
            return name in self._queued or name in self._in_flight

    def build_rate(self, window_s=60.0):
        now = time.monotonic()
        with self._cond:
            self._recent = [t for t in self._recent if now - t <= window_s]
            return len(self._recent) / window_s

    def idle(self):
        with self._cond:
            return not self._queued and not self._in_flight

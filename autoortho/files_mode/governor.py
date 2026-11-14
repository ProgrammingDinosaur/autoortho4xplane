"""The governor in files mode: tunes files-mode levers live.

FUSE mode's governor (``governor.py``) reads X-Plane's reads: near reads
answered real or with a stand-in, prefetch misses, live-tile waits. In files
mode X-Plane reads plain files and AutoOrtho never sees a read, so those
signals stay zero. This governor follows the same rules (one lever per
decision, a cooldown per lever, keep or revert, Auto / Advise / Off, never
past the limits, protection before quality) on the files-mode signals:

* **late textures**: needed at a level now (urgency <= 0) and built coarser,
  split by what the queue had done for them: *never queued* (planned too
  late: look further ahead, build wider), *queued* (waited behind other work:
  more builders or downloads), *building* (the build itself too slow: more
  downloads, then builders);
* X-Plane's frame time (``protect_sim_fps``), CPU against the CPU limit, RAM
  against the RAM limit and the emergency brake.

Levers: builders in flight, the planner's lookahead, the ring margin, and
the provider's parallel downloads (the same broker lever as FUSE mode).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Deque, Dict, Iterable, List, Optional

if __package__ and __package__.startswith("autoortho"):
    from autoortho.governor import MODES, Decision, LeverSpec
else:
    from governor import MODES, Decision, LeverSpec

log = logging.getLogger(__name__)

SAMPLE_EVERY_S = 2.0
WINDOW_S = 30.0
DECIDE_EVERY_S = 6.0
JUDGE_AFTER_S = 20.0
COOLDOWN_S = 30.0
REVERT_COOLDOWN_FACTOR = 4.0
MIN_LATE = 5              # late textures in a window before a quality move
DOMINANT_SHARE = 0.5
HEADROOM_WINDOWS = 3      # quiet windows in a row before easing off
FRAME_SLOWER = 1.2
FRAME_SLOW_MS = 25.0
WORSE_FACTOR = 1.2        # a move that made late textures this much worse is undone

BUILDERS = "files.builders"
LOOKAHEAD = "files.lookahead_s"
MARGIN = "files.margin"
CONCURRENCY = "autoortho.provider_origin_max_concurrency"

STATE_TEXT = {
    "warming_up": "Measuring",
    "ok": "Keeping ahead of X-Plane",
    "headroom": "Headroom: easing off",
    "memory": "Memory pressure",
    "sim_fps": "X-Plane's frame rate dropped",
    "compute_limit": "Busy at the CPU limit",
    "late_unplanned": "Textures planned too late",
    "late_queued": "Textures waiting in the queue",
    "late_slow": "Builds too slow (downloads)",
    "starting": "Building the start area",
    "paused": "Paused",
    "unreachable": "Late textures, every lever at its limit",
}


def levers_for(cfg=None, limits=None, network_spec: Optional[LeverSpec] = None) -> Dict[str, LeverSpec]:
    import os
    threads = getattr(limits, "cpu_threads", None) or (os.cpu_count() or 8)
    specs = [
        LeverSpec(BUILDERS, "Texture builders", 1, 1, max(1, min(16, threads - 2)),
                  group="compute"),
        LeverSpec(LOOKAHEAD, "Planning lookahead", 15, 60, 240, unit="s", group="prefetch"),
        LeverSpec(MARGIN, "Build margin beyond X-Plane's rings", 0.05, 1.05, 1.4,
                  integer=False, group="prefetch"),
    ]
    if network_spec is not None:
        specs.append(network_spec)
    return {s.name: s for s in specs}


@dataclass(frozen=True)
class FilesSample:
    t: float
    late_never: int = 0         # cumulative counters from files_stats
    late_queued: int = 0
    late_building: int = 0
    builds: int = 0
    urgent_depth: int = 0       # gauges
    queue_depth: int = 0
    in_flight: int = 0
    builders: int = 0
    starting: bool = False
    paused: bool = False
    frame_ms: float = 0.0
    cpu_percent: float = 0.0
    free_ram_bytes: int = 0
    ao_memory_bytes: int = 0
    broker_saturated: bool = False
    mbps: float = 0.0


@dataclass
class FilesWindow:
    seconds: float
    late_never: int
    late_queued: int
    late_building: int
    builds: int
    urgent_depth: float
    in_flight: float
    builders: float
    frame_ms: float
    cpu_percent: float
    free_ram_bytes: int
    ao_memory_bytes: int
    broker_saturated: bool
    mbps: float

    @property
    def late(self):
        return self.late_never + self.late_queued + self.late_building

    @property
    def late_per_min(self):
        return self.late * 60.0 / self.seconds if self.seconds > 0 else 0.0

    @property
    def on_time(self) -> Optional[float]:
        total = self.builds + self.late
        return None if total == 0 else self.builds / total


def window_of(samples: List[FilesSample]) -> Optional[FilesWindow]:
    if len(samples) < 2:
        return None
    a, b = samples[0], samples[-1]
    seconds = b.t - a.t
    if seconds <= 0:
        return None
    n = len(samples)
    frames = [s.frame_ms for s in samples if s.frame_ms > 0]
    return FilesWindow(
        seconds=seconds,
        late_never=max(0, b.late_never - a.late_never),
        late_queued=max(0, b.late_queued - a.late_queued),
        late_building=max(0, b.late_building - a.late_building),
        builds=max(0, b.builds - a.builds),
        urgent_depth=sum(s.urgent_depth for s in samples) / n,
        in_flight=sum(s.in_flight for s in samples) / n,
        builders=sum(s.builders for s in samples) / n,
        frame_ms=sum(frames) / len(frames) if frames else 0.0,
        cpu_percent=sum(s.cpu_percent for s in samples) / n,
        free_ram_bytes=min(s.free_ram_bytes for s in samples),
        ao_memory_bytes=max(s.ao_memory_bytes for s in samples),
        broker_saturated=any(s.broker_saturated for s in samples),
        mbps=sum(s.mbps for s in samples) / n,
    )


class FilesGovernor:
    """Decides lever moves from samples. Pure: no I/O, no clock of its own."""

    _PLAN = {
        "memory": [(BUILDERS, -1)],
        "sim_fps": [(BUILDERS, -1)],
        "compute_limit": [(BUILDERS, -1)],
        "late_unplanned": [(LOOKAHEAD, +1), (MARGIN, +1)],
        "late_queued": [(BUILDERS, +1), (CONCURRENCY, +1)],
        "late_slow": [(CONCURRENCY, +1), (BUILDERS, +1)],
    }
    # Protection moves are never undone by the quality judge.
    PROTECT = ("memory", "sim_fps", "compute_limit")

    def __init__(self, levers: Dict[str, LeverSpec], values: Dict[str, float], *,
                 baseline: Optional[Dict[str, float]] = None, mode: str = "advise",
                 pinned: Iterable[str] = (), cpu_limit_percent: float = 90.0,
                 ram_limit_bytes: Optional[int] = None,
                 brake_free_bytes: Optional[int] = None, protect_sim_fps: bool = True,
                 line_mbps: Optional[float] = None):
        self.levers = dict(levers)
        self.values = {n: float(values[n]) for n in levers if n in values}
        self.baseline = dict(baseline or self.values)
        self.mode = mode if mode in MODES else "advise"
        self.pinned = set(pinned)
        self.cpu_limit_percent = float(cpu_limit_percent)
        self.ram_limit_bytes = ram_limit_bytes
        self.brake_free_bytes = brake_free_bytes
        self.protect_sim_fps = bool(protect_sim_fps)
        self.line_mbps = line_mbps
        self.samples: Deque[FilesSample] = deque()
        self.window: Optional[FilesWindow] = None
        self.state = "warming_up"
        self.history: List[Decision] = []
        self.frame_best_ms: Optional[float] = None
        self.starting = False
        self.notice: Optional[str] = None
        self._quiet = 0
        self._cooldown_until: Dict[str, float] = {}
        self._last_decision_t = -math.inf
        self._pending: Optional[tuple] = None   # (decision, window before)
        self.late_total = 0
        self.builds_total = 0

    # -- feeding -------------------------------------------------------
    def observe(self, sample: FilesSample) -> Optional[Decision]:
        prev = self.samples[-1] if self.samples else None
        self.samples.append(sample)
        while self.samples and sample.t - self.samples[0].t > WINDOW_S:
            self.samples.popleft()
        if prev is not None:
            self.late_total += max(0, (sample.late_never + sample.late_queued
                                       + sample.late_building)
                                   - (prev.late_never + prev.late_queued + prev.late_building))
            self.builds_total += max(0, sample.builds - prev.builds)
        self.starting = sample.starting
        self.window = window_of(list(self.samples))
        w = self.window
        if w is not None and w.frame_ms and not sample.starting and not sample.paused and (
            w.seconds >= WINDOW_S * 0.8
        ):
            if self.frame_best_ms is None or w.frame_ms < self.frame_best_ms:
                self.frame_best_ms = w.frame_ms
        if sample.paused:
            self.state = "paused"
            return None
        if w is None or w.seconds < WINDOW_S * 0.8:
            self.state = "warming_up"
            return None
        self.state = self.classify(w)
        if self.mode == "off":
            return None
        outcome = self._judge(sample.t)
        if outcome is not None:
            return outcome
        if self._pending is not None or sample.t - self._last_decision_t < DECIDE_EVERY_S:
            return None
        decision = self._decide(sample.t)
        if decision is not None:
            self._last_decision_t = sample.t
        return decision

    # -- classification --------------------------------------------------
    def frames_slow(self, w):
        if not self.protect_sim_fps or not w.frame_ms or self.frame_best_ms is None:
            return False
        return w.frame_ms > self.frame_best_ms * FRAME_SLOWER and w.frame_ms > FRAME_SLOW_MS

    def classify(self, w: FilesWindow) -> str:
        if self.brake_free_bytes and w.free_ram_bytes and (
            w.free_ram_bytes < 1.3 * self.brake_free_bytes
        ):
            return "memory"
        if self.ram_limit_bytes and w.ao_memory_bytes > 0.9 * self.ram_limit_bytes:
            return "memory"
        if self.frames_slow(w):
            return "sim_fps"
        if w.cpu_percent > self.cpu_limit_percent:
            return "compute_limit"
        if self.starting:
            # Everything is urgent at a start; judge in flight.
            return "starting"
        if w.late >= MIN_LATE:
            self._quiet = 0
            shares = {
                "late_unplanned": w.late_never,
                "late_queued": w.late_queued,
                "late_slow": w.late_building,
            }
            state, count = max(shares.items(), key=lambda kv: kv[1])
            if count >= DOMINANT_SHARE * w.late:
                return state
            return "late_queued"
        if w.late == 0 and w.urgent_depth < 1:
            self._quiet += 1
            if self._quiet >= HEADROOM_WINDOWS * (WINDOW_S / SAMPLE_EVERY_S):
                return "headroom"
        return "ok"

    # -- moves -----------------------------------------------------------
    def _candidates(self, state):
        if state == "headroom":
            moves = []
            for name, value in self.values.items():
                base = self.baseline.get(name)
                if base is not None and value != base:
                    moves.append((name, +1 if base > value else -1))
            return moves
        moves = list(self._PLAN.get(state, ()))
        w = self.window
        if state == "late_queued" and w is not None and w.in_flight < 0.75 * max(1.0, w.builders):
            # Builders idle while textures wait: they wait on downloads.
            moves = [(CONCURRENCY, +1), (BUILDERS, +1)]
        if w is not None and self.line_mbps and w.mbps >= 0.9 * self.line_mbps:
            moves = [m for m in moves if not (m[0] == CONCURRENCY and m[1] > 0)]
        if w is not None and w.broker_saturated:
            moves = [m for m in moves if not (m[0] == CONCURRENCY and m[1] > 0)]
        if state.startswith("late") and w is not None and (
            w.cpu_percent > 0.85 * self.cpu_limit_percent
        ):
            moves = [m for m in moves if not (m[0] == BUILDERS and m[1] > 0)]
        return moves

    def _decide(self, now) -> Optional[Decision]:
        state = self.state
        moves = self._candidates(state)
        for name, direction in moves:
            spec = self.levers.get(name)
            if spec is None or name in self.pinned or name not in self.values:
                continue
            if now < self._cooldown_until.get(name, -math.inf):
                continue
            old = self.values[name]
            target = old + direction * spec.step
            if state == "headroom":
                base = self.baseline[name]
                target = max(target, base) if direction < 0 else min(target, base)
            new = spec.clamp(target)
            if new == old:
                continue
            decision = Decision(now, name, spec.label, old, new, state, self._reason(state))
            self._cooldown_until[name] = now + COOLDOWN_S
            if self.mode == "auto":
                decision = replace(decision, applied=True)
                self.values[name] = new
                if state not in self.PROTECT:
                    self._pending = (decision, self.window)
            self.history.append(decision)
            self.notice = None
            return decision
        if state.startswith("late") and moves:
            self.notice = ("Textures arrive late and every lever that could help is at its "
                           "limit: raise the CPU or bandwidth limits, or lower the zoom.")
            self.state = "unreachable"
        return None

    def _reason(self, state):
        w = self.window
        if state.startswith("late") and w is not None:
            return (f"{w.late} late textures in {w.seconds:.0f}s: {w.late_never} planned late, "
                    f"{w.late_queued} queued, {w.late_building} building")
        if state == "sim_fps" and w is not None:
            return f"frames {w.frame_ms:.0f} ms vs best {self.frame_best_ms:.0f} ms"
        if state == "compute_limit" and w is not None:
            return f"CPU {w.cpu_percent:.0f} % over the {self.cpu_limit_percent:.0f} % limit"
        if state == "memory":
            return "RAM close to the limit"
        if state == "headroom":
            return "no late textures for a while"
        return STATE_TEXT.get(state, state)

    def _judge(self, now) -> Optional[Decision]:
        if self._pending is None:
            return None
        decision, before = self._pending
        if now - decision.t < JUDGE_AFTER_S:
            return None
        self._pending = None
        after = self.window
        # Compared as rates: the windows may differ in length.
        worse = (
            before is not None and after is not None
            and after.late >= MIN_LATE
            and after.late_per_min > before.late_per_min * WORSE_FACTOR
        )
        self._mark(decision, "reverted" if worse else "kept")
        if not worse:
            return None
        self.values[decision.lever] = decision.old
        self._cooldown_until[decision.lever] = now + COOLDOWN_S * REVERT_COOLDOWN_FACTOR
        undo = Decision(now, decision.lever, decision.label, decision.new, decision.old,
                        decision.state, "made late textures worse", applied=True,
                        outcome="revert")
        self.history.append(undo)
        return undo

    def _mark(self, decision, outcome):
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i] is decision:
                self.history[i] = replace(decision, outcome=outcome)
                return

    def fidelity(self) -> Optional[float]:
        return self.window.on_time if self.window else None

    def summary(self) -> dict:
        total = self.builds_total + self.late_total
        return {
            "backend": "files",
            "mode": self.mode,
            "fidelity": (self.builds_total / total) if total else None,
            "late_textures": self.late_total,
            "builds": self.builds_total,
            "values": dict(self.values),
            "decisions": len(self.history),
            "verdict": (
                "No late textures" if self.late_total == 0
                else f"{self.late_total} late textures, {self.builds_total} built"
            ),
        }


class FilesGovernorService:
    """Samples ``files_stats`` every 2 s in the parent; publishes in Auto.

    ``snapshot()`` has the same keys as ``governor.GovernorService`` so the
    UI shows either backend's governor.
    """

    def __init__(self, governor: FilesGovernor, *, read_stat: Callable[[str], object],
                 publish: Optional[Callable[[dict], object]] = None,
                 flight: Optional[Callable[[], Optional[dict]]] = None,
                 broker_stats: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.governor = governor
        self._read = read_stat
        self._publish = publish
        self._flight = flight
        self._broker_stats = broker_stats
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.decisions: Deque[Decision] = deque(maxlen=200)
        self._timeline: Deque[dict] = deque(maxlen=1800)
        self.home: dict = {}

    def sample(self) -> FilesSample:
        stats = self._read("files_stats") or {}
        if not isinstance(stats, dict):
            stats = {}
        cpu = free = mem = 0
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=None)
            free = psutil.virtual_memory().available
        except Exception:
            pass
        try:
            mem = int(self._read("cur_mem_mb") or 0) * 1024 * 1024
        except (TypeError, ValueError):
            mem = 0
        frame_ms = 0.0
        paused = False
        if self._flight is not None:
            try:
                data = self._flight() or {}
                period = float(data.get("frame_period") or 0)
                frame_ms = period * 1000.0 if 0 < period < 5 else 0.0
                paused = bool(data.get("paused"))
            except Exception:
                pass
        saturated = False
        mbps = 0.0
        if self._broker_stats is not None:
            try:
                b = self._broker_stats() or {}
                saturated = bool(b.get("saturated") or b.get("queue_full"))
                mbps = float(b.get("mbps") or 0.0)
            except Exception:
                pass

        def n(key):
            try:
                return int(stats.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return FilesSample(
            t=self._clock(), late_never=n("files_late_never_queued"),
            late_queued=n("files_late_queued"), late_building=n("files_late_building"),
            builds=n("files_builds"), urgent_depth=n("files_urgent_depth"),
            queue_depth=n("files_queue_depth"), in_flight=n("files_in_flight"),
            builders=n("files_builders"),
            starting=bool(stats.get("files_starting") or stats.get("files_relocating")),
            paused=paused, frame_ms=frame_ms, cpu_percent=float(cpu),
            free_ram_bytes=int(free), ao_memory_bytes=mem, broker_saturated=saturated,
            mbps=mbps,
        )

    def tick(self) -> Optional[Decision]:
        sample = self.sample()
        with self._lock:
            decision = self.governor.observe(sample)
            w = self.governor.window
            self._timeline.append({
                "t": sample.t, "fidelity": w.on_time if w else None,
                "mbps": w.mbps if w else 0.0, "cpu": sample.cpu_percent,
                "ram_gb": sample.ao_memory_bytes / 1024 ** 3,
                "late": w.late if w else 0,
                "decision": decision.text() if decision else "",
            })
            if decision is not None:
                self.decisions.append(decision)
        if decision is not None:
            log.info("Files governor: %s%s", decision.text(self.governor.fidelity()),
                     "" if decision.applied else " (advice)")
            if decision.applied and self._publish is not None:
                try:
                    self._publish({decision.lever: decision.new})
                except Exception:
                    log.exception("Files governor could not apply %s", decision.lever)
        return decision

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(SAMPLE_EVERY_S):
                try:
                    self.tick()
                except Exception:
                    log.exception("Files governor tick failed")

        self._thread = threading.Thread(target=loop, name="FilesGovernor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    def set_mode(self, mode):
        with self._lock:
            self.governor.mode = mode if mode in MODES else "advise"

    def set_texture_cap_allowed(self, allowed, cap_px=4096):
        return None  # files mode sizes textures per file; no cap lever

    def flight_summary(self) -> dict:
        with self._lock:
            return self.governor.summary()

    def snapshot(self) -> dict:
        with self._lock:
            g = self.governor
            w = g.window
            return {
                "backend": "files",
                "mode": g.mode, "state": g.state,
                "state_text": STATE_TEXT.get(g.state, g.state),
                "fidelity": w.on_time if w else None,
                "mbps": w.mbps if w else 0.0,
                "error_share": 0.0,
                "cpu": w.cpu_percent if w else 0.0,
                "time_to_sharp_s": None,
                "fps": (1000.0 / w.frame_ms) if w and w.frame_ms else None,
                "protect_sim_fps": g.protect_sim_fps,
                "graphics_memory_bytes": 0, "vram_budget_bytes": None,
                "line_mbps": g.line_mbps, "bandwidth_mbps": None,
                "ram_limit_bytes": g.ram_limit_bytes,
                "home": dict(self.home),
                "values": dict(g.values), "baseline": dict(g.baseline),
                "pinned": sorted(g.pinned), "levers": dict(g.levers),
                "decisions": list(self.decisions),
                "timeline": list(self._timeline),
                "notice": g.notice, "starting": g.starting,
                "late": w.late if w else 0,
            }

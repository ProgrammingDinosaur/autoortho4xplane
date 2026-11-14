"""The governor: tunes engine settings live to reach the quality target.

It runs in the parent process. Every 2 seconds a sampler collects signals
(target fidelity, downloads, errors, live-tile admission waits, CPU, RAM,
flight phase); every few seconds the governor looks at the last 30 seconds,
finds what holds fidelity back (the bottleneck), and moves one lever one
step. In **Auto** mode it publishes the change (``live_settings``), checks
the result over the next window, and keeps or reverts it. In **Advise**
mode it only lists its decisions. **Off** leaves every value as configured.

Rules that keep it stable (proposal section 4.4):

* one lever per decision, a cooldown per lever, bigger steps while a flight
  is starting;
* keep or revert: a change that made fidelity or its bottleneck worse is
  undone and that lever waits four times longer;
* levers stay inside the ranges the limits allow; settings the user set
  (pinned) are never touched;
* the target is never lowered: when every lever is at its limit and
  fidelity stays low, the governor spends less on far work and tells the
  user what limits it.

At the end of a flight it stores where the levers settled (learned
profiles, keyed by target, provider and a salted network fingerprint) so the
next flight starts there. Everything is decided from :class:`Sample` values,
so recorded flights replay offline (:func:`replay`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Deque, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)


# Chosen by __package__, not try/except: with the autoortho folder on
# sys.path, "autoortho" can resolve to autoortho.py (see _wait_bands below).
# prefetch_ledger itself imports nothing of the app.
if __package__:
    from autoortho.prefetch_ledger import MISS_KINDS, PAUSE_REASONS
else:
    from prefetch_ledger import MISS_KINDS, PAUSE_REASONS


def _wait_bands():
    """wait_bands, imported on first use.

    At import time it would pull in streaming_options and aoconfig, and with
    the autoortho folder on sys.path that chain reaches autoortho.py and the
    UI while ui.main_window is still importing this module.
    """
    if __package__:
        from autoortho import wait_bands
    else:
        import wait_bands
    return wait_bands

MODES = ("auto", "advise", "off")
MODE_LABELS = {
    "auto": "Auto: tune live within the limits",
    "advise": "Advise: suggest changes, apply nothing",
    "off": "Off: use the configured values",
}

SAMPLE_EVERY_S = 2.0
WINDOW_S = 30.0
DECIDE_EVERY_S = 6.0
JUDGE_AFTER_S = 20.0
MIN_NEAR_READS = 20
COOLDOWN_S = 30.0
REVERT_COOLDOWN_FACTOR = 4.0
UNREACHABLE_AFTER_S = 300.0
FIDELITY_GOOD = 0.98
FIDELITY_LOW = 0.95
# protect_sim_fps: X-Plane's frames count as slowed when they take this much
# longer than the session's best 30-second average, and are under 40 fps.
FRAME_SLOWER = 1.2
FRAME_SLOW_MS = 25.0
# Graphics memory: the photo textures X-Plane holds (texture_plan.HeldTextures)
# against the budget; at this share the texture cap engages (when allowed).
VRAM_FULL = 0.95
TEXTURE_CAP_LEVER = "governor.texture_cap_px"
# Waits are raised only on evidence (OrthoStreamXP's tuner): enough reads
# that stopped waiting, and their imagery arriving within reach of the
# lever. In flight 80 % of them, while a flight starts 90 %.
LATE_SHARE_FLIGHT = 0.8
LATE_SHARE_START = 0.9
MIN_LATE_FLIGHT = 20
MIN_LATE_START = 5
NEAR_WAIT_LEVER = "streaming.near_wait_seconds"
BAND_WAIT_LEVER = "streaming.band_max_wait_seconds"
PAGING_LEAD_LEVER = "streaming.paging_lead_seconds"
PREFETCH_CHUNKS_LEVER = "autoortho.prefetch_max_chunks"
# Behind the aircraft, by evidence (governor-behind-detection-plan.md): each
# blurry near read in flight is classified by what prefetch had done for the
# tile (prefetch_ledger.py). A prefetch lever moves only with at least
# MIN_MISSES classified misses in the window and one cause behind at least
# MISS_DOMINANT_SHARE of them. Starting values, to be set from flight reports.
MIN_MISSES = 10
MISS_DOMINANT_SHARE = 0.5
# A bigger memory cache only while AutoOrtho uses under this share of its RAM limit.
EVICTED_RAM_SHARE = 0.8
# Dominant miss cause -> state. Turns and tiles that were complete have
# nothing to tune.
MISS_STATE = {
    "never_wide": "behind_wide",
    "never_inside": "behind_short",
    "never_paused": "prefetch_paused",
    "never_turn": "ok",
    "coarse": "paging_coarse",
    "late": "prefetch_late",
    "evicted": "memory_evicted",
    "done": "ok",
}
STATE_MISS = {state: kind for kind, state in MISS_STATE.items() if state != "ok"}
# States without moves that tell the user once they persist.
NOTICE_STATES = ("prefetch_paused", "unexplained")
PAUSE_WORDS = {
    "ram": "low memory",
    "live": "X-Plane's own downloads",
    "gate": "the wait for X-Plane to enter the flight",
}
DATA_DIR = os.path.join(os.path.expanduser("~"), ".autoortho-data", "governor")


# --------------------------------------------------------------- levers


@dataclass(frozen=True)
class LeverSpec:
    name: str              # "section.key"
    label: str
    step: float
    minimum: float
    maximum: float
    unit: str = ""
    integer: bool = True
    group: str = ""        # "network", "compute", "memory", "waits", "prefetch"

    def clamp(self, value):
        value = max(self.minimum, min(self.maximum, float(value)))
        return int(round(value)) if self.integer else round(value, 2)


def _number(cfg, section, key, default):
    try:
        return float(getattr(getattr(cfg, section), key))
    except (AttributeError, TypeError, ValueError):
        return float(default)


def levers_for(cfg, limits=None) -> Dict[str, LeverSpec]:
    """The levers and their ranges under the current limits."""
    try:
        if __package__:
            from autoortho.aoconfig import resolve_provider_setting
        else:
            from aoconfig import resolve_provider_setting
        min_conc = int(resolve_provider_setting("provider_origin_min_concurrency", cfg))
        max_in_flight = int(resolve_provider_setting("provider_max_in_flight", cfg))
    except Exception:
        min_conc, max_in_flight = 64, 320
    net_ceiling = max_in_flight
    if limits is not None and getattr(limits, "bytes_per_second", 0):
        net_ceiling = max(
            min_conc,
            min(max_in_flight, int(limits.bytes_per_second / (16_000 / 0.13) * 1.5) + 1),
        )
    threads = getattr(limits, "cpu_threads", None) or (os.cpu_count() or 8)
    cores = getattr(limits, "cpu_cores", None) or max(1, threads // 2)
    cache_ceiling = getattr(limits, "cache_ceiling_gb", None) or 8
    ram_gb = getattr(limits, "ram_gb", None) or 8.0
    specs = [
        LeverSpec("autoortho.provider_origin_max_concurrency", "Parallel downloads",
                  16, min_conc, net_ceiling, group="network"),
        LeverSpec("autoortho.live_tile_admission", "Live tiles at once",
                  4, 4, max(8, min(64, threads * 2)), group="compute"),
        LeverSpec("autoortho.background_builder_workers", "Background builders",
                  1, 1, max(1, min(16, cores // 2)), group="compute"),
        LeverSpec("autoortho.predictive_dds_build_interval_ms", "Background build pause",
                  100, 100, 1000, unit="ms", group="compute"),
        LeverSpec("cache.cache_mem_limit", "Memory cache",
                  1, 2, max(2, cache_ceiling), unit="GB", group="memory"),
        LeverSpec("autoortho.predictive_dds_memory_mb", "Predictive build memory",
                  128, 128, max(256, int(ram_gb * 1024 * 0.25) // 128 * 128),
                  unit="MB", group="memory"),
        LeverSpec("autoortho.tile_image_cache_mb", "Tile image cache",
                  32, 32, 256, unit="MB", group="memory"),
        LeverSpec("streaming.band_max_wait_seconds", "Wait for near imagery",
                  1, 2, 12, unit="s", integer=False, group="waits"),
        LeverSpec("streaming.near_wait_seconds", "Wait while starting",
                  10, 20, 120, unit="s", integer=False, group="waits"),
        LeverSpec("autoortho.prefetch_lookahead", "Prefetch lookahead",
                  2, 4, 30, unit="min", group="prefetch"),
        LeverSpec("autoortho.prefetch_radius_nm", "Prefetch radius",
                  10, 10, 80, unit="nm", group="prefetch"),
        # Read at call time (streaming_options), so live with no hook.
        LeverSpec(PAGING_LEAD_LEVER, "Prefetch lead time",
                  15, 30, 180, unit="s", group="prefetch"),
        # The template's 64 is the floor: never lowered below the default.
        LeverSpec(PREFETCH_CHUNKS_LEVER, "Prefetch chunks per cycle",
                  64, 64, 2048, group="prefetch"),
    ]
    if texture_cap_allowed(cfg):
        try:
            if __package__:
                from autoortho.texture_plan import CAP_PX
            else:
                from texture_plan import CAP_PX
        except ImportError:
            CAP_PX = 4096
        # 0 (no cap) or the cap; engaged once for the session (phase 8).
        specs.append(LeverSpec(TEXTURE_CAP_LEVER, "Texture size cap",
                               CAP_PX, 0, CAP_PX, unit="px", group="memory"))
    return {spec.name: spec for spec in specs}


def texture_cap_allowed(cfg) -> bool:
    value = getattr(getattr(cfg, "governor", None), "allow_texture_cap", False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


# --------------------------------------------------------------- samples


@dataclass(frozen=True)
class Sample:
    """Signals at one moment. Counters are cumulative; the rest are gauges."""

    t: float
    near_real: int = 0
    near_standin: int = 0
    req_ok: int = 0
    req_err: int = 0
    bytes_dl: int = 0
    admission_waits: int = 0
    admissions: int = 0
    time_to_sharp_ms_total: int = 0
    time_to_sharp_count: int = 0
    ram_brake_activations: int = 0
    disk_brake_activations: int = 0
    cpu_percent: float = 0.0
    ao_memory_bytes: int = 0
    free_ram_bytes: int = 0
    total_ram_bytes: int = 0
    broker_active: int = 0
    broker_limit: int = 0
    broker_waiting: int = 0
    altitude_ft: Optional[float] = None
    speed_kt: Optional[float] = None
    connected: bool = False
    paused: bool = False
    frame_time_ms: Optional[float] = None
    graphics_memory_bytes: int = 0
    # Cumulative counts per wait_bands.LATE_BUCKETS_S bucket (plus overflow):
    # how long imagery took after a near or middle read stopped waiting.
    late_flight: tuple = ()
    late_start: tuple = ()
    # Cumulative blurry near reads in flight by what prefetch had done for
    # the tile (prefetch_ledger.MISS_KINDS), and the never-queued ones missed
    # during a prefetch pause by its reason.
    near_miss_never_wide: int = 0
    near_miss_never_inside: int = 0
    near_miss_never_paused: int = 0
    near_miss_never_turn: int = 0
    near_miss_coarse: int = 0
    near_miss_late: int = 0
    near_miss_evicted: int = 0
    near_miss_done: int = 0
    near_miss_paused_ram: int = 0
    near_miss_paused_live: int = 0
    near_miss_paused_gate: int = 0
    # Whether the flight was starting (the service's view), so a replay
    # judges each sample as the live governor did. None in old recordings.
    starting: Optional[bool] = None

    def misses(self) -> Dict[str, int]:
        return {kind: int(getattr(self, f"near_miss_{kind}", 0) or 0) for kind in MISS_KINDS}

    def pauses(self) -> Dict[str, int]:
        return {r: int(getattr(self, f"near_miss_paused_{r}", 0) or 0) for r in PAUSE_REASONS}


def _delta(after: Dict[str, int], before: Dict[str, int]) -> Dict[str, int]:
    return {k: max(0, after.get(k, 0) - before.get(k, 0)) for k in after}


@dataclass(frozen=True)
class Window:
    seconds: float
    near_reads: int
    fidelity: Optional[float]
    mbps: float
    error_share: float
    admission_wait_share: float
    cpu_percent: float
    free_ram_bytes: int
    total_ram_bytes: int
    ao_memory_bytes: int
    broker_saturated: bool
    time_to_sharp_s: Optional[float]
    frame_time_ms: Optional[float] = None
    graphics_memory_bytes: int = 0
    late_flight: tuple = ()
    late_start: tuple = ()
    miss_counts: Dict[str, int] = field(default_factory=dict)
    pause_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def misses_total(self) -> int:
        return sum(self.miss_counts.values())


def window_of(samples: List[Sample]) -> Optional[Window]:
    if len(samples) < 2:
        return None
    first, last = samples[0], samples[-1]
    dt = max(0.001, last.t - first.t)
    real = last.near_real - first.near_real
    standin = last.near_standin - first.near_standin
    near = max(0, real + standin)
    ok = last.req_ok - first.req_ok
    err = last.req_err - first.req_err
    adm = last.admissions - first.admissions
    waits = last.admission_waits - first.admission_waits
    tts_n = last.time_to_sharp_count - first.time_to_sharp_count
    tts_ms = last.time_to_sharp_ms_total - first.time_to_sharp_ms_total
    recent = samples[-min(len(samples), 5):]
    saturated = sum(
        1 for s in recent
        if s.broker_limit and (s.broker_active >= 0.9 * s.broker_limit or s.broker_waiting > 0)
    ) >= max(1, len(recent) // 2 + 1)
    frames = [s.frame_time_ms for s in samples if s.frame_time_ms]
    return Window(
        seconds=dt,
        near_reads=near,
        fidelity=(real / near) if near >= MIN_NEAR_READS else None,
        mbps=max(0, last.bytes_dl - first.bytes_dl) * 8 / dt / 1e6,
        error_share=(err / (ok + err)) if (ok + err) > 0 else 0.0,
        admission_wait_share=(waits / adm) if adm > 0 else 0.0,
        cpu_percent=sum(s.cpu_percent for s in recent) / len(recent),
        free_ram_bytes=last.free_ram_bytes,
        total_ram_bytes=last.total_ram_bytes,
        ao_memory_bytes=last.ao_memory_bytes,
        broker_saturated=saturated,
        time_to_sharp_s=(tts_ms / tts_n / 1000.0) if tts_n > 0 else None,
        frame_time_ms=(sum(frames) / len(frames)) if len(frames) >= 3 else None,
        graphics_memory_bytes=last.graphics_memory_bytes,
        late_flight=tuple(last.late_flight or ()),
        late_start=tuple(last.late_start or ()),
        miss_counts=_delta(last.misses(), first.misses()),
        pause_counts=_delta(last.pauses(), first.pauses()),
    )


def phase_of(sample: Sample, starting: bool) -> str:
    if not sample.connected:
        return "starting" if starting else "idle"
    if starting:
        return "starting"
    speed = sample.speed_kt or 0
    alt = sample.altitude_ft or 0
    if speed < 40:
        return "taxi"
    if alt < 3000:
        return "departure_arrival"
    return "cruise"


# --------------------------------------------------------------- decisions


@dataclass(frozen=True)
class Decision:
    t: float
    lever: str
    label: str
    old: float
    new: float
    state: str
    reason: str
    applied: bool = False
    outcome: str = ""          # "", "kept", "reverted"

    @property
    def verb(self):
        return "raise" if self.new > self.old else "lower"

    def text(self, fidelity=None):
        fid = f"; fidelity {fidelity * 100:.0f} %" if fidelity is not None else ""
        if self.lever == TEXTURE_CAP_LEVER:
            what = (f"cap textures at {_fmt(self.new)} px" if self.new
                    else "lift the texture size cap")
            return f"{what} ({self.reason}){fid}"
        return (
            f"{self.verb} {self.label.lower()} {_fmt(self.old)}→{_fmt(self.new)} "
            f"({self.reason}){fid}"
        )


def _fmt(value):
    return f"{value:g}" if isinstance(value, float) and not value.is_integer() else str(int(value))


STATE_TEXT = {
    "warming_up": "Measuring",
    "ok": "Keeping up",
    "headroom": "Headroom: easing off",
    "memory": "Memory pressure",
    "network_pushback": "Server pushing back",
    "network_room": "Network-bound, line has room",
    "network_full": "Network-bound, line full",
    "compute": "Compute-bound",
    "compute_limit": "Compute-bound at the CPU limit",
    "wait": "Waiting too briefly",
    "slow_imagery": "Imagery arrives late",
    "behind_wide": "Prefetch corridor too narrow",
    "behind_short": "Prefetch corridor too short",
    "paging_coarse": "Prefetch asks for too little detail",
    "prefetch_late": "Prefetch starts too late",
    "prefetch_paused": "Prefetch paused",
    "memory_evicted": "Memory cache too small",
    "unexplained": "Blurry, cause unclear",
    "sim_fps": "X-Plane's frame rate dropped",
    "vram": "Graphics memory nearly full",
    "unreachable": "Target out of reach",
    "paused": "Paused",
}


class Governor:
    """Decides lever moves from samples. Pure: no I/O, no clocks of its own."""

    def __init__(
        self,
        levers: Dict[str, LeverSpec],
        values: Dict[str, float],
        *,
        baseline: Optional[Dict[str, float]] = None,
        mode: str = "advise",
        pinned: Iterable[str] = (),
        line_mbps: Optional[float] = None,
        bandwidth_mbps: Optional[float] = None,
        cpu_limit_percent: float = 90.0,
        ram_limit_bytes: Optional[int] = None,
        brake_free_bytes: Optional[int] = None,
        protect_sim_fps: bool = False,
        vram_budget_bytes: Optional[int] = None,
    ):
        self.levers = dict(levers)
        self.values = {name: float(values[name]) for name in levers if name in values}
        self.baseline = dict(baseline or self.values)
        self.mode = mode if mode in MODES else "advise"
        self.pinned = set(pinned)
        self.line_mbps = line_mbps
        self.bandwidth_mbps = bandwidth_mbps
        self.cpu_limit_percent = float(cpu_limit_percent)
        self.ram_limit_bytes = ram_limit_bytes
        self.brake_free_bytes = brake_free_bytes
        self.protect_sim_fps = bool(protect_sim_fps)
        self.frame_best_ms: Optional[float] = None
        self.vram_budget_bytes = vram_budget_bytes
        self.peak_graphics_memory = 0
        self.samples: Deque[Sample] = deque()
        self.history: List[Decision] = []
        self.state = "warming_up"
        self.window: Optional[Window] = None
        self.starting = True
        self._cooldown_until: Dict[str, float] = {}
        self._last_decision_t = -math.inf
        self._pending: Optional[tuple] = None    # (decision, window before)
        self._limited_since: Optional[float] = None
        self.degraded: List[str] = []
        self.notice: Optional[str] = None
        self.phase_fidelity: Dict[str, List[int]] = {}   # phase -> [real, total]
        self.phase_misses: Dict[str, Dict[str, int]] = {}  # phase -> kind -> count
        self._persist: Dict[str, tuple] = {}     # notice state -> (first, last seen)
        self.peak_mbps = 0.0

    # -- feeding ---------------------------------------------------------

    def observe(self, sample: Sample, starting: Optional[bool] = None) -> Optional[Decision]:
        if starting is None:
            starting = sample.starting
        if starting is not None:
            self.starting = bool(starting)
        previous = self.samples[-1] if self.samples else None
        self.samples.append(sample)
        while self.samples and sample.t - self.samples[0].t > WINDOW_S:
            self.samples.popleft()
        if previous is not None:
            phase = phase_of(sample, self.starting)
            real = sample.near_real - previous.near_real
            stand = sample.near_standin - previous.near_standin
            bucket = self.phase_fidelity.setdefault(phase, [0, 0])
            bucket[0] += max(0, real)
            bucket[1] += max(0, real + stand)
            misses = _delta(sample.misses(), previous.misses())
            if any(misses.values()):
                by_kind = self.phase_misses.setdefault(phase, {})
                for kind, count in misses.items():
                    if count:
                        by_kind[kind] = by_kind.get(kind, 0) + count
        self.window = window_of(list(self.samples))
        if self.window is not None:
            self.peak_mbps = max(self.peak_mbps, self.window.mbps)
            self.peak_graphics_memory = max(self.peak_graphics_memory,
                                            self.window.graphics_memory_bytes)
            self._note_vram(self.window)
            frame = self.window.frame_time_ms
            # The best 30-second average once flying (loading frames are slow).
            if frame and not self.starting and not sample.paused and (
                self.window.seconds >= WINDOW_S * 0.8
            ):
                if self.frame_best_ms is None or frame < self.frame_best_ms:
                    self.frame_best_ms = frame
        if sample.paused:
            self.state = "paused"
            return None
        if self.mode == "off" or self.window is None:
            self.state = "warming_up" if self.window is None else self.classify(self.window)
            return None
        outcome = self._judge_pending(sample.t)
        if outcome is not None:
            return outcome
        self.state = self.classify(self.window)
        self._note_persistent(sample.t)
        if self._pending is not None:
            # One change at a time: wait for the last one's verdict.
            return None
        if sample.t - self._last_decision_t < DECIDE_EVERY_S:
            return None
        decision = self._decide(sample.t)
        if decision is not None:
            self._last_decision_t = sample.t
        return decision

    # -- classification --------------------------------------------------

    def classify(self, w: Window) -> str:
        if self.brake_free_bytes and w.free_ram_bytes and (
            w.free_ram_bytes < 1.3 * self.brake_free_bytes
        ):
            return "memory"
        if self.ram_limit_bytes and w.ao_memory_bytes > 0.9 * self.ram_limit_bytes:
            return "memory"
        if self.vram_full(w) and self._can_cap():
            return "vram"
        if w.error_share > 0.03:
            return "network_pushback"
        frames_slow = self.frames_slow(w)
        if frames_slow and not self.starting:
            return "sim_fps"
        capacity = [c for c in (self.line_mbps, self.bandwidth_mbps) if c]
        line_full = bool(capacity) and w.mbps >= 0.9 * min(capacity)
        if w.fidelity is None:
            return "warming_up"
        if w.admission_wait_share > 0.2:
            if w.cpu_percent >= self.cpu_limit_percent:
                return "compute_limit"
            return "compute"
        if w.broker_saturated and w.error_share < 0.01:
            return "network_full" if line_full else "network_room"
        if w.fidelity >= FIDELITY_GOOD:
            if (w.cpu_percent < 0.5 * self.cpu_limit_percent and not w.broker_saturated
                    and not frames_slow):
                return "headroom"
            return "ok"
        if w.fidelity < FIDELITY_LOW:
            verdict, _need = self.wait_evidence(w)
            if verdict == "wait":
                return "wait"
            if verdict == "too_slow":
                return "slow_imagery"
            return self.miss_state(w)
        return "ok"

    def miss_cause(self, w: Optional[Window] = None):
        """The dominant miss kind as ``(kind, count, total)``, or None.

        None with fewer than MIN_MISSES classified misses in the window or no
        kind behind MISS_DOMINANT_SHARE of them.
        """
        w = w if w is not None else self.window
        if w is None:
            return None
        total = w.misses_total
        if total < MIN_MISSES:
            return None
        kind, count = max(w.miss_counts.items(), key=lambda kv: kv[1])
        if count < MISS_DOMINANT_SHARE * total:
            return None
        return kind, count, total

    def miss_state(self, w: Window) -> str:
        """Low fidelity with no network, compute or wait cause: ask the ledger.

        Without evidence nothing moves (``unexplained``). Misses are not
        counted while a flight starts (prefetch can't know where it begins).
        """
        if self.starting:
            return "ok"
        cause = self.miss_cause(w)
        if cause is None:
            return "unexplained"
        return MISS_STATE.get(cause[0], "ok")

    def wait_evidence(self, w: Optional[Window] = None):
        """Whether a longer wait would bring real imagery, from the samples.

        Returns ``(verdict, seconds)``. ``seconds`` is the share (80 % in
        flight, 90 % while starting) of the imagery reads stopped waiting
        for, timed from the start of the read. Verdicts:

        * ``"wait"``: it arrived within the lever's reach but after the
          current wait;
        * ``"too_slow"``: most of it took longer than any wait allowed;
        * ``"enough"``: the current wait already covers it;
        * ``"unknown"``: too few such reads to tell.
        """
        w = w if w is not None else self.window
        name = NEAR_WAIT_LEVER if self.starting else BAND_WAIT_LEVER
        spec = self.levers.get(name)
        if w is None or spec is None:
            return "unknown", None
        counts = w.late_start if self.starting else w.late_flight
        share = LATE_SHARE_START if self.starting else LATE_SHARE_FLIGHT
        minimum = MIN_LATE_START if self.starting else MIN_LATE_FLIGHT
        need, total = _wait_bands().late_percentile(counts, share)
        if need is None or total < minimum:
            return "unknown", need
        if need > spec.maximum:
            return "too_slow", need
        if need > self.values.get(name, spec.maximum):
            return "wait", need
        return "enough", need

    def vram_full(self, w: Optional[Window]) -> bool:
        """The photo textures X-Plane holds are near the graphics memory budget."""
        if w is None or not self.vram_budget_bytes or not w.graphics_memory_bytes:
            return False
        return w.graphics_memory_bytes >= VRAM_FULL * self.vram_budget_bytes

    def _can_cap(self) -> bool:
        spec = self.levers.get(TEXTURE_CAP_LEVER)
        return (spec is not None and TEXTURE_CAP_LEVER not in self.pinned
                and not self.values.get(TEXTURE_CAP_LEVER))

    def _note_vram(self, w: Window) -> None:
        """Near the budget with no cap to apply: tell the user (once)."""
        if self.notice or not self.vram_full(w) or self._can_cap():
            return
        if self.values.get(TEXTURE_CAP_LEVER):
            return   # already capped: nothing more the governor can do
        used = w.graphics_memory_bytes / 1024 ** 3
        budget = self.vram_budget_bytes / 1024 ** 3
        self.notice = (
            f"Photo textures are near the graphics memory limit ({used:.1f} of "
            f"{budget:.1f} GB). Lower the zoom target, or allow auto-tuning "
            "to cap texture size."
        )

    def frames_slow(self, w: Optional[Window]) -> bool:
        """X-Plane's frames are clearly slower than earlier this flight."""
        if not self.protect_sim_fps or w is None or not w.frame_time_ms:
            return False
        if self.frame_best_ms is None:
            return False
        return (w.frame_time_ms > FRAME_SLOWER * self.frame_best_ms
                and w.frame_time_ms > FRAME_SLOW_MS)

    def _at_max(self, name):
        spec = self.levers.get(name)
        return spec is not None and self.values.get(name, spec.maximum) >= spec.maximum

    def _at_min(self, name):
        spec = self.levers.get(name)
        return spec is not None and self.values.get(name, spec.minimum) <= spec.minimum

    # -- choosing a move -------------------------------------------------

    _PLAN = {
        "network_room": [("autoortho.provider_origin_max_concurrency", +1)],
        "network_pushback": [
            ("autoortho.provider_origin_max_concurrency", -1),
            ("autoortho.prefetch_radius_nm", -1),
        ],
        "network_full": [("autoortho.prefetch_radius_nm", -1)],
        "compute": [
            ("autoortho.live_tile_admission", +1),
            ("autoortho.background_builder_workers", +1),
        ],
        "compute_limit": [
            ("autoortho.background_builder_workers", -1),
            ("autoortho.predictive_dds_build_interval_ms", +1),
        ],
        "memory": [
            ("autoortho.tile_image_cache_mb", -1),
            ("autoortho.predictive_dds_memory_mb", -1),
            ("cache.cache_mem_limit", -1),
        ],
        "vram": [(TEXTURE_CAP_LEVER, +1)],
        "sim_fps": [
            ("autoortho.background_builder_workers", -1),
            ("autoortho.predictive_dds_build_interval_ms", +1),
            ("autoortho.prefetch_radius_nm", -1),
        ],
        # The line has room but each picture comes late: ask for it earlier.
        "slow_imagery": [("autoortho.prefetch_lookahead", +1)],
        # Behind the aircraft, by the dominant miss cause.
        "behind_wide": [("autoortho.prefetch_radius_nm", +1)],
        "behind_short": [("autoortho.prefetch_lookahead", +1)],
        "paging_coarse": [(PAGING_LEAD_LEVER, +1)],
        "prefetch_late": [(PAGING_LEAD_LEVER, +1), (PREFETCH_CHUNKS_LEVER, +1)],
        "memory_evicted": [("cache.cache_mem_limit", +1)],
    }

    def _candidates(self, state):
        if state == "wait":
            name = NEAR_WAIT_LEVER if self.starting else BAND_WAIT_LEVER
            return [(name, +1)]
        if state == "headroom":
            # Ease off toward the baseline, one lever at a time.
            moves = []
            for name, value in self.values.items():
                base = self.baseline.get(name)
                if base is None or value == base or name == TEXTURE_CAP_LEVER:
                    continue
                moves.append((name, +1 if base > value else -1))
            return moves
        w = self.window
        if state == "prefetch_late" and w is not None and w.broker_saturated:
            return []     # more prefetch would only queue behind a full line
        if state == "memory_evicted" and w is not None and self.ram_limit_bytes and (
            w.ao_memory_bytes >= EVICTED_RAM_SHARE * self.ram_limit_bytes
        ):
            return []     # no RAM to give the cache
        return list(self._PLAN.get(state, ()))

    def _decide(self, now: float) -> Optional[Decision]:
        state = self.state
        moves = self._candidates(state)
        w = self.window
        for name, direction in moves:
            spec = self.levers.get(name)
            if spec is None or name in self.pinned or name not in self.values:
                continue
            if now < self._cooldown_until.get(name, -math.inf):
                continue
            old = self.values[name]
            step = spec.step * (2 if self.starting else 1)
            target = old + direction * step
            if state == "headroom":
                base = self.baseline[name]
                target = max(target, base) if direction < 0 else min(target, base)
            if state == "wait":
                # No further than the imagery actually needed.
                _verdict, need = self.wait_evidence()
                if need is not None and math.isfinite(need):
                    target = min(target, max(need, old))
            new = spec.clamp(target)
            if new == old:
                continue
            reason = self._reason(state, w)
            decision = Decision(now, name, spec.label, old, new, state, reason)
            self._cooldown_until[name] = now + COOLDOWN_S * (0.5 if self.starting else 1)
            self._limited_since = None
            if self.mode == "auto":
                decision = replace(decision, applied=True)
                self.values[name] = new
                self._pending = (decision, w)
            self.history.append(decision)
            return decision
        if state not in ("ok", "headroom", "warming_up", "paused") and (
            moves or state in STATE_MISS
        ) and state not in NOTICE_STATES:
            self._note_limited(now, state)
        return None

    def _note_persistent(self, now) -> None:
        """A state without moves that keeps coming back reaches the user once."""
        state = self.state
        if state not in NOTICE_STATES:
            return
        first, last = self._persist.get(state, (now, now))
        if now - last > 2 * WINDOW_S:
            first = now           # it went away for a while: start again
        self._persist[state] = (first, now)
        if self.notice or now - first < UNREACHABLE_AFTER_S:
            return
        if state == "prefetch_paused":
            w = self.window
            pauses = w.pause_counts if w is not None else {}
            reason = max(pauses.items(), key=lambda kv: kv[1])[0] if any(
                pauses.values()) else "gate"
            self.notice = (
                "Scenery was often blurry because prefetch was paused by "
                f"{PAUSE_WORDS.get(reason, reason)}. "
                + {
                    "ram": "Close other programs or raise the RAM limit.",
                    "live": "A faster connection or a lower zoom target helps.",
                }.get(reason, "")
            ).strip()
        else:
            self.notice = (
                "Scenery was often blurry and AutoOrtho could not tell why; "
                "the flight report lists the blurry tiles by cause."
            )

    def _note_limited(self, now, state):
        """Every lever for this bottleneck is at its limit."""
        if self._limited_since is None:
            self._limited_since = now
            return
        if now - self._limited_since < UNREACHABLE_AFTER_S:
            return
        if self.window is None or self.window.fidelity is None:
            return
        if self.window.fidelity >= FIDELITY_LOW:
            return
        self.state = "unreachable"
        # Graceful degradation: spend less on far work first (never lower
        # the target itself).
        for name in ("autoortho.prefetch_lookahead", "autoortho.prefetch_radius_nm"):
            spec = self.levers.get(name)
            if spec is None or name in self.pinned or self._at_min(name):
                continue
            if name in self.degraded:
                continue
            self.degraded.append(name)
            if self.mode == "auto":
                self.values[name] = spec.clamp(self.values[name] - spec.step)
        limiting = {
            "network_room": "your connection",
            "network_full": "your connection",
            "network_pushback": "the imagery server",
            "compute": "the CPU",
            "compute_limit": "the CPU limit",
            "memory": "memory",
            "wait": "waiting time",
            "slow_imagery": "how fast the imagery server answers",
            "behind_wide": "prefetching",
            "behind_short": "prefetching",
            "paging_coarse": "how much detail prefetch asks for",
            "prefetch_late": "how early prefetch starts",
            "memory_evicted": "memory",
            "sim_fps": "X-Plane's frame rate",
            "vram": "graphics memory",
        }.get(state, "this PC")
        self.notice = (
            f"The target reached {self.window.fidelity * 100:.0f} % sharp; the "
            f"limit is {limiting}. Lower the zoom for this phase, raise the "
            "relevant limit, or keep it."
        )

    def _reason(self, state, w: Optional[Window]) -> str:
        if w is None:
            return STATE_TEXT.get(state, state)
        parts = [STATE_TEXT.get(state, state).lower()]
        if state.startswith("network"):
            parts.append(f"errors {w.error_share * 100:.1f} %")
            parts.append(f"{w.mbps:.0f} Mbit/s")
            if w.broker_saturated:
                parts.append("queue full")
        elif state.startswith("compute"):
            parts.append(f"{w.admission_wait_share * 100:.0f} % of tiles waited")
            parts.append(f"CPU {w.cpu_percent:.0f} %")
        elif state == "memory":
            parts.append(f"{w.free_ram_bytes / 1024 ** 3:.1f} GB free")
        elif state == "vram" and self.vram_budget_bytes:
            parts.append(f"{w.graphics_memory_bytes / 1024 ** 3:.1f} of "
                         f"{self.vram_budget_bytes / 1024 ** 3:.1f} GB")
        elif state == "sim_fps" and w.frame_time_ms:
            parts.append(f"{1000 / w.frame_time_ms:.0f} fps")
            if self.frame_best_ms:
                parts.append(f"was {1000 / self.frame_best_ms:.0f}")
        elif state in STATE_MISS or state == "unexplained":
            evidence = self._miss_evidence(state, w)
            if evidence:
                parts.append(evidence)
            if w.fidelity is not None:
                parts.append(f"{w.fidelity * 100:.0f} % sharp")
        elif state in ("wait", "slow_imagery") and w.fidelity is not None:
            parts.append(f"{w.fidelity * 100:.0f} % sharp")
            if state in ("wait", "slow_imagery"):
                _verdict, need = self.wait_evidence()
                if need is not None:
                    parts.append(
                        f"most late imagery within {need:g} s" if math.isfinite(need)
                        else f"most late imagery over {_wait_bands().LATE_GIVE_UP_S} s"
                    )
        return ": ".join([parts[0], ", ".join(parts[1:])]) if len(parts) > 1 else parts[0]

    _MISS_TEXT = {
        "never_wide": "lay outside the prefetch corridor",
        "never_inside": "were never queued, inside the corridor",
        "never_paused": "were missed while prefetch was paused",
        "coarse": "were queued only at a coarser level",
        "late": "were queued but not ready",
        "evicted": "had left the memory cache",
    }

    def _miss_evidence(self, state, w: Window) -> str:
        total = w.misses_total
        kind = STATE_MISS.get(state)
        if kind is None:
            return f"{total} blurry tiles, no single cause" if total else ""
        count = w.miss_counts.get(kind, 0)
        return f"{count} of {total} blurry tiles {self._MISS_TEXT.get(kind, kind)}"

    # -- keep or revert --------------------------------------------------

    def _metric(self, state, w: Window) -> float:
        """Bigger is better: the bottleneck's own measure."""
        kind = STATE_MISS.get(state)
        if kind is not None:
            # The share of near reads blurry for this cause (lower is better).
            # Per near read rather than per miss, so a change that cut every
            # cause is not judged worse.
            return -(w.miss_counts.get(kind, 0) / max(1, w.near_reads))
        if state.startswith("network"):
            return w.mbps * (1 - w.error_share)
        if state.startswith("compute"):
            return -w.admission_wait_share
        if state == "memory":
            return float(w.free_ram_bytes)
        if state == "sim_fps":
            return -(w.frame_time_ms or 0.0)
        return w.fidelity or 0.0

    def _judge_pending(self, now: float) -> Optional[Decision]:
        if self._pending is None:
            return None
        decision, before = self._pending
        if decision.state == "vram":
            # The estimate covers 20 minutes of textures, so it falls slowly:
            # the cap stays for the session rather than being judged.
            self._pending = None
            self._mark(decision, "kept")
            return None
        if now - decision.t < JUDGE_AFTER_S:
            return None
        self._pending = None
        # Judge on what happened since the change only.
        after = window_of([s for s in self.samples if s.t >= decision.t])
        if before is None or after is None:
            return None
        worse_fidelity = (
            before.fidelity is not None and after.fidelity is not None
            and after.fidelity < before.fidelity - 0.02
        )
        before_metric = self._metric(decision.state, before)
        worse_metric = (
            self._metric(decision.state, after)
            < before_metric - 0.05 * abs(before_metric) - 1e-9
        )
        if decision.state == "headroom":
            worse_metric = False   # easing off may cost a little, by design
        if decision.state == "sim_fps" and (after.fidelity or 0.0) >= FIDELITY_LOW:
            worse_fidelity = False  # X-Plane's frames first, while still sharp
        if worse_fidelity or worse_metric:
            self.values[decision.lever] = decision.old
            self._cooldown_until[decision.lever] = now + COOLDOWN_S * REVERT_COOLDOWN_FACTOR
            reverted = replace(decision, t=now, old=decision.new, new=decision.old,
                               outcome="reverted", applied=True,
                               reason=f"undoing: {'fidelity' if worse_fidelity else 'it'} got worse")
            self._mark(decision, "reverted")
            self.history.append(reverted)
            return reverted
        self._mark(decision, "kept")
        return None

    def _mark(self, decision, outcome):
        if outcome == "kept":
            log.info("Governor: kept %s", decision.text())
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index] is decision:
                self.history[index] = replace(decision, outcome=outcome)
                return

    # -- reporting -------------------------------------------------------

    def fidelity(self) -> Optional[float]:
        return self.window.fidelity if self.window else None

    def summary(self) -> dict:
        """End-of-flight report section (the flight report's verdict)."""
        phases = {
            phase: round(real / total, 3)
            for phase, (real, total) in self.phase_fidelity.items()
            if total >= MIN_NEAR_READS
        }
        total_real = sum(r for r, _t in self.phase_fidelity.values())
        total = sum(t for _r, t in self.phase_fidelity.values())
        overall = total_real / total if total else None
        if overall is None:
            verdict = "Not enough nearby scenery read to judge"
        elif overall >= FIDELITY_GOOD:
            verdict = "Smooth flight"
        elif overall >= 0.85:
            verdict = "A few blurry moments"
        else:
            verdict = "Often blurry"
        last = self.samples[-1] if self.samples else None
        return {
            "verdict": verdict,
            "fidelity": round(overall, 3) if overall is not None else None,
            "fidelity_by_phase": phases,
            "mode": self.mode,
            "decisions": len([
                d for d in self.history if not d.reason.startswith("undoing")
            ]),
            "kept": len([d for d in self.history if d.outcome == "kept"]),
            "reverted": len([d for d in self.history if d.outcome == "reverted"]),
            "peak_mbps": round(self.peak_mbps, 1),
            "peak_graphics_memory_gb": round(self.peak_graphics_memory / 1024 ** 3, 2),
            "texture_cap_px": int(self.values.get(TEXTURE_CAP_LEVER, 0) or 0),
            "ram_brake_activations": last.ram_brake_activations if last else 0,
            "disk_brake_activations": last.disk_brake_activations if last else 0,
            "learned": {k: v for k, v in self.values.items() if v != self.baseline.get(k)},
            "learned_labels": {
                k: self.levers[k].label for k, v in self.values.items()
                if v != self.baseline.get(k) and k in self.levers
            },
            "notice": self.notice,
            "degraded": list(self.degraded),
            "misses": {
                kind: sum(phase.get(kind, 0) for phase in self.phase_misses.values())
                for kind in MISS_KINDS
            },
            "misses_by_phase": {
                phase: dict(kinds) for phase, kinds in self.phase_misses.items()
            },
        }


# --------------------------------------------------------------- replay


def replay(samples: Iterable[Sample], levers, values, **kwargs) -> List[Decision]:
    """Run the governor over recorded samples; returns its decisions."""
    governor = Governor(levers, values, **kwargs)
    decisions = []
    for sample in samples:
        decision = governor.observe(sample)
        if decision is not None:
            decisions.append(decision)
    return decisions


def load_samples(path) -> List[Sample]:
    samples = []
    names = set(Sample.__dataclass_fields__)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            samples.append(Sample(**{
                k: tuple(v) if isinstance(v, list) else v
                for k, v in data.items() if k in names
            }))
    return samples


# --------------------------------------------------------------- learning


def _salt(data_dir) -> str:
    path = os.path.join(data_dir, "salt")
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
            if value:
                return value
    except OSError:
        pass
    value = secrets.token_hex(16)
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "w", encoding="ascii") as handle:
            handle.write(value)
    except OSError:
        pass
    return value


def network_fingerprint(data_dir=DATA_DIR) -> str:
    """A salted hash of this PC's local networks (never leaves the PC)."""
    nets = []
    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                text = getattr(addr, "address", "") or ""
                if getattr(addr, "family", None) is not None and text.count(".") == 3:
                    if text.startswith("127.") or text.startswith("169.254."):
                        continue
                    nets.append(".".join(text.split(".")[:3]))
    except Exception:
        pass
    raw = _salt(data_dir) + "|" + ",".join(sorted(set(nets)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def profile_key(target: dict, provider: str, fingerprint: str) -> str:
    target_text = json.dumps(target, sort_keys=True)
    target_hash = hashlib.sha256(target_text.encode("utf-8")).hexdigest()[:12]
    return f"{target_hash}:{provider}:{fingerprint}"


def _last_flight_path() -> str:
    return os.path.join(DATA_DIR, "last_flight.json")


def save_last_flight(summary: dict) -> None:
    """Keep the last flight's summary for Home's last-flight card."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _last_flight_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)
        os.replace(tmp, _last_flight_path())
    except OSError as exc:
        log.debug("Could not save the last flight: %s", exc)


def load_last_flight() -> Optional[dict]:
    try:
        with open(_last_flight_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class LearnedProfiles:
    """Where the levers settled, per target/provider/network.

    Each entry has the ``levers`` Auto settled on and the ``applied``
    suggestions the user accepted in Advise mode. Both are AutoOrtho's
    values, not the user's: they never pin a setting, the governor may move
    them again, and Reset learned settings clears them (OrthoStreamXP's
    tuned store).
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "learned.json")

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key) -> dict:
        return dict(self._load().get(key, {}).get("levers", {}))

    def applied(self, key) -> dict:
        """The Advise suggestions accepted for this profile."""
        return dict(self._load().get(key, {}).get("applied", {}))

    def warm_start(self, key, mode) -> dict:
        """The values a flight in ``mode`` starts from (Off: none).

        Accepted suggestions apply in Advise and Auto; what Auto settled on
        applies in Auto only, and wins over an older suggestion.
        """
        if mode not in ("auto", "advise"):
            return {}
        values = self.applied(key)
        if mode == "auto":
            values.update(self.get(key))
        return values

    def put(self, key, levers: dict, extra: Optional[dict] = None) -> None:
        data = self._load()
        previous = data.get(key, {})
        entry = {"levers": dict(levers), "saved": time.time()}
        if previous.get("applied"):
            entry["applied"] = dict(previous["applied"])
        entry.update(extra or {})
        data[key] = entry
        self._write(data)

    def put_applied(self, key, lever: str, value: float) -> None:
        """Remember an accepted Advise suggestion for the next flights."""
        data = self._load()
        entry = data.setdefault(key, {"levers": {}})
        entry.setdefault("applied", {})[lever] = float(value)
        entry["saved"] = time.time()
        self._write(data)

    def _write(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save learned settings: %s", exc)

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


# --------------------------------------------------------------- service


def _distance_m(a, b) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    d_lat = (lat2 - lat1) * 111320.0
    d_lon = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(d_lat, d_lon)



class GovernorService:
    """Samples every 2 s in the parent, runs the governor, applies in Auto.

    ``read_stat`` reads the shared stats store; ``publish`` sends levers to
    the workers and broker (``AOMount.publish_levers``); ``broker_stats``
    returns the broker's server counters; ``flight`` returns X-Plane's
    flight data. All optional, for tests.
    """

    def __init__(
        self,
        governor: Governor,
        *,
        read_stat: Callable[[str], object],
        publish: Optional[Callable[[dict], object]] = None,
        broker_stats: Optional[Callable[[], dict]] = None,
        flight: Optional[Callable[[], Optional[dict]]] = None,
        record_path: Optional[str] = None,
        on_decision: Optional[Callable[[Decision], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sum_stat: Optional[Callable[[str], float]] = None,
        max_stat: Optional[Callable[[str], float]] = None,
        collect_stat: Optional[Callable[[str], list]] = None,
    ):
        self.governor = governor
        self._read = read_stat
        self._sum = sum_stat
        self._max = max_stat
        self._collect = collect_stat
        # Session totals for Home and the last-flight card.
        self._session_start: Optional[float] = None
        self._first_counts: Optional[dict] = None
        self._distance_m = 0.0
        self._last_fix = None            # (lat, lon) while connected
        self._last_flight: Optional[dict] = None
        self._altitudes: Deque[tuple] = deque(maxlen=30)   # (t, ft)
        self.home: dict = {}
        self._publish = publish
        self._broker_stats = broker_stats
        self._flight = flight
        self._record_path = record_path
        self._on_decision = on_decision
        self._clock = clock
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._timeline: Deque[dict] = deque(maxlen=1800)   # an hour at 2 s
        self._starting = True
        self._last_pos = None
        self.decisions: Deque[Decision] = deque(maxlen=200)

    # -- one tick ----------------------------------------------------------

    def _counter(self, key) -> int:
        try:
            return int(self._read(key) or 0)
        except (TypeError, ValueError):
            return 0

    def sample(self) -> Sample:
        now = self._clock()
        cpu = 0.0
        free = total = 0
        try:
            import psutil

            cpu = float(psutil.cpu_percent(interval=None))
            memory = psutil.virtual_memory()
            free, total = int(memory.available), int(memory.total)
        except Exception:
            pass
        try:
            ao_mb = float(self._read("cur_mem_mb") or 0)
        except (TypeError, ValueError):
            ao_mb = 0.0
        active = limit = waiting = 0
        if self._broker_stats is not None:
            try:
                stats = self._broker_stats() or {}
                origins = stats.get("origins") or {}
                for row in (origins.values() if isinstance(origins, dict) else origins):
                    active += int(row.get("active", 0) or 0)
                    limit += int(min(row.get("limit", 0) or 0, row.get("ceiling", 1 << 30) or 0))
                # Requests parked waiting for an origin permit: the queue is full.
                waiting = int(stats.get("backlog_depth", 0) or 0)
            except Exception:
                pass
        altitude = speed = frame_ms = None
        connected = paused = False
        if self._flight is not None:
            try:
                data = self._flight()
            except Exception:
                data = None
            self._last_flight = data or None
            if data:
                connected = True
                altitude = float(data.get("alt", 0) or 0) / 0.3048
                speed = float(data.get("spd", 0) or 0) * 3600 / 1852
                period = float(data.get("frame_period", 0) or 0)
                frame_ms = period * 1000.0 if 0 < period < 5 else None
                paused = bool(data.get("paused", False))
                self._update_starting(data, now)
        return Sample(
            t=now,
            near_real=self._counter("near_reads_real"),
            near_standin=self._counter("near_reads_standin"),
            req_ok=self._counter("req_ok"),
            req_err=self._counter("req_err"),
            bytes_dl=self._counter("bytes_dl"),
            admission_waits=self._counter("live_admission_waits"),
            admissions=self._counter("live_admissions"),
            time_to_sharp_ms_total=self._counter("time_to_sharp_ms_total"),
            time_to_sharp_count=self._counter("time_to_sharp_count"),
            ram_brake_activations=self._counter("ram_brake_activations"),
            disk_brake_activations=self._counter("disk_brake_activations"),
            cpu_percent=cpu,
            ao_memory_bytes=int(ao_mb * 1024 * 1024),
            free_ram_bytes=free,
            total_ram_bytes=total,
            broker_active=active,
            broker_limit=limit,
            broker_waiting=waiting,
            altitude_ft=altitude,
            speed_kt=speed,
            connected=connected,
            paused=paused,
            frame_time_ms=frame_ms,
            graphics_memory_bytes=self._summed("graphics_memory_bytes"),
            late_flight=self._late_counts(starting=False),
            late_start=self._late_counts(starting=True),
            **{f"near_miss_{kind}": self._counter(f"near_miss_{kind}")
               for kind in MISS_KINDS},
            **{f"near_miss_paused_{reason}": self._counter(f"near_miss_paused_{reason}")
               for reason in PAUSE_REASONS},
            starting=self._starting,
        )

    def _late_counts(self, starting) -> tuple:
        bands = _wait_bands()
        return tuple(
            self._counter(bands.late_stat_key(starting, index))
            for index in range(len(bands.LATE_BUCKETS_S) + 1)
        )

    def _largest(self, prefix) -> int:
        """A per-process gauge's largest value over the workers."""
        if self._max is None:
            return self._summed(prefix)
        try:
            return int(self._max(prefix) or 0)
        except Exception:
            return 0

    _COUNTS = ("tile_mem_miss", "tile_mem_hits", "chunk_hit", "chunk_miss", "bytes_dl")

    def _note_session(self, sample: Sample) -> None:
        """Totals since streaming started, and the extras Home shows."""
        counts = {name: self._counter(name) for name in self._COUNTS}
        if self._session_start is None:
            self._session_start = sample.t
            self._first_counts = counts
        data = self._last_flight
        if data and sample.connected:
            lat, lon = data.get("lat"), data.get("lon")
            if lat is not None and lon is not None:
                if self._last_fix is not None:
                    step = _distance_m(self._last_fix, (lat, lon))
                    if step < 50_000:          # not a jump to another place
                        self._distance_m += step
                self._last_fix = (lat, lon)
            if sample.altitude_ft is not None:
                self._altitudes.append((sample.t, sample.altitude_ft))
        else:
            self._last_fix = None
        first = self._first_counts or counts
        delta = {name: max(0, counts[name] - first.get(name, 0)) for name in counts}
        hits, misses = delta["chunk_hit"], delta["chunk_miss"]
        self.home = {
            "textures": delta["tile_mem_miss"] + delta["tile_mem_hits"],
            "cache_share": hits / (hits + misses) if hits + misses else None,
            "downloaded_bytes": delta["bytes_dl"],
            "building_now": self._summed("building_now"),
            "horizon_m": self._largest("prefetch_horizon_m"),
            "phase": phase_of(sample, self._starting),
            "vertical": self._vertical(),
            "altitude_ft": sample.altitude_ft,
            "speed_kt": sample.speed_kt,
            "connected": sample.connected,
            "paused": sample.paused,
            "ao_memory_bytes": sample.ao_memory_bytes,
            "free_ram_bytes": sample.free_ram_bytes,
            "total_ram_bytes": sample.total_ram_bytes,
            "duration_s": sample.t - self._session_start,
            "distance_nm": self._distance_m / 1852.0,
            "heading": float((data or {}).get("hdg", 0) or 0) if data else None,
            "radar": self._collected("tile_radar"),
        }

    def _collected(self, prefix) -> list:
        """A per-process list (``prefix:<pid>``) joined over the workers."""
        if self._collect is None:
            return []
        try:
            return list(self._collect(prefix) or [])
        except Exception:
            return []

    def _vertical(self) -> str:
        """climbing / descending / level, over the last minute."""
        if len(self._altitudes) < 2:
            return ""
        (t0, a0), (t1, a1) = self._altitudes[0], self._altitudes[-1]
        if t1 - t0 < 10:
            return ""
        rate = (a1 - a0) / (t1 - t0) * 60.0     # ft per minute
        if rate > 300:
            return "climbing"
        if rate < -300:
            return "descending"
        return "level"

    def flight_summary(self) -> dict:
        """The governor's verdict plus the session totals (the last-flight card)."""
        with self._lock:
            summary = self.governor.summary()
            home = dict(self.home)
        summary.update({
            "duration_min": round(home.get("duration_s", 0.0) / 60.0, 1),
            "distance_nm": round(home.get("distance_nm", 0.0), 1),
            "downloaded_gb": round(home.get("downloaded_bytes", 0) / 1024 ** 3, 2),
            "cache_share": (round(home["cache_share"], 3)
                            if home.get("cache_share") is not None else None),
            "textures": home.get("textures", 0),
            "ended": time.time(),
        })
        return summary

    def _summed(self, prefix) -> int:
        """A per-process gauge (``prefix:<pid>``) summed over the workers."""
        if self._sum is None:
            return self._counter(prefix)
        try:
            return int(self._sum(prefix) or 0)
        except Exception:
            return 0

    def _update_starting(self, data, now):
        speed = float(data.get("spd", 0) or 0)
        lat, lon = data.get("lat"), data.get("lon")
        if self._last_pos is not None and lat is not None:
            last_lat, last_lon, last_t = self._last_pos
            dt = max(0.05, now - last_t)
            d = math.hypot((lat - last_lat) * 111320.0,
                           (lon - last_lon) * 111320.0 * math.cos(math.radians(lat)))
            if d / dt > 400.0:
                self._starting = True
        if lat is not None:
            self._last_pos = (lat, lon, now)
        if speed >= 10.0:
            self._starting = False

    def tick(self) -> Optional[Decision]:
        sample = self.sample()
        with self._lock:
            decision = self.governor.observe(sample, starting=self._starting)
            window = self.governor.window
            self._timeline.append({
                "t": sample.t,
                "fidelity": window.fidelity if window else None,
                "mbps": window.mbps if window else 0.0,
                "cpu": sample.cpu_percent,
                "ram_gb": sample.ao_memory_bytes / 1024 ** 3,
                "misses": dict(window.miss_counts) if window else {},
                "decision": decision.text() if decision else "",
            })
            if decision is not None:
                self.decisions.append(decision)
        self._record(sample)
        try:
            self._note_session(sample)
        except Exception:
            log.debug("Governor session totals failed", exc_info=True)
        if decision is not None:
            log.info("Governor: %s%s", decision.text(self.governor.fidelity()),
                     "" if decision.applied else " (advice)")
            if decision.applied and self._publish is not None:
                try:
                    self._publish({decision.lever: decision.new})
                except Exception:
                    log.exception("Governor could not apply %s", decision.lever)
            if self._on_decision is not None:
                try:
                    self._on_decision(decision)
                except Exception:
                    log.debug("Governor decision listener failed", exc_info=True)
        return decision

    def _record(self, sample):
        if not self._record_path:
            return
        try:
            os.makedirs(os.path.dirname(self._record_path), exist_ok=True)
            with open(self._record_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(sample)) + "\n")
        except OSError:
            self._record_path = None

    # -- thread ------------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(SAMPLE_EVERY_S):
                try:
                    self.tick()
                except Exception:
                    log.exception("Governor tick failed")

        self._thread = threading.Thread(target=loop, name="Governor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    # -- for the UI --------------------------------------------------------

    def set_texture_cap_allowed(self, allowed: bool, cap_px: int = 4096) -> None:
        """The user allowed (or no longer allows) the texture cap mid-flight."""
        lift = False
        with self._lock:
            governor = self.governor
            if allowed and TEXTURE_CAP_LEVER not in governor.levers:
                governor.levers[TEXTURE_CAP_LEVER] = LeverSpec(
                    TEXTURE_CAP_LEVER, "Texture size cap", cap_px, 0, cap_px,
                    unit="px", group="memory",
                )
                governor.values[TEXTURE_CAP_LEVER] = 0.0
                governor.baseline[TEXTURE_CAP_LEVER] = 0.0
            elif not allowed and TEXTURE_CAP_LEVER in governor.levers:
                lift = bool(governor.values.pop(TEXTURE_CAP_LEVER, 0))
                governor.levers.pop(TEXTURE_CAP_LEVER, None)
                governor.baseline.pop(TEXTURE_CAP_LEVER, None)
        if lift and self._publish is not None:
            try:
                self._publish({TEXTURE_CAP_LEVER: 0})
            except Exception:
                log.exception("Could not lift the texture size cap")

    def set_mode(self, mode):
        with self._lock:
            self.governor.mode = mode if mode in MODES else "advise"

    def snapshot(self) -> dict:
        with self._lock:
            governor = self.governor
            window = governor.window
            return {
                "mode": governor.mode,
                "state": governor.state,
                "state_text": STATE_TEXT.get(governor.state, governor.state),
                "fidelity": window.fidelity if window else None,
                "mbps": window.mbps if window else 0.0,
                "error_share": window.error_share if window else 0.0,
                "cpu": window.cpu_percent if window else 0.0,
                "time_to_sharp_s": window.time_to_sharp_s if window else None,
                "fps": (1000.0 / window.frame_time_ms
                        if window and window.frame_time_ms else None),
                "protect_sim_fps": governor.protect_sim_fps,
                "graphics_memory_bytes": window.graphics_memory_bytes if window else 0,
                "vram_budget_bytes": governor.vram_budget_bytes,
                "line_mbps": governor.line_mbps,
                "bandwidth_mbps": governor.bandwidth_mbps,
                "ram_limit_bytes": governor.ram_limit_bytes,
                "home": dict(self.home),
                "values": dict(governor.values),
                "baseline": dict(governor.baseline),
                "pinned": sorted(governor.pinned),
                "levers": dict(governor.levers),
                "decisions": list(self.decisions),
                "timeline": list(self._timeline),
                "notice": governor.notice,
                "starting": governor.starting,
            }

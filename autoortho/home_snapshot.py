"""What Home shows, worked out once a second (proposal section 13).

:func:`build` turns the governor's snapshot (its sampler collects every
signal each 2 s and smooths them over 30 s) and the app's own state into a
:class:`HomeSnapshot` in plain units: % sharp, Mbit/s, GB, minutes and nm.
``HomePage`` only formats it. Pure: no Qt, no I/O, so it is tested directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

GB = 1024 ** 3
SHARP_GOOD = 0.95          # below this a trend bucket counts as blurry
TREND_BUCKET_S = 5.0       # trend lines use 5-second buckets
TREND_POINTS = 120         # ten minutes of buckets
RADAR_RADIUS_NM = 60.0
RADAR_TEXT = ("sharp at target", "being read now", "below target", "not loaded")

STOPPED, STARTING, FLYING, PAUSED, AFTER_FLIGHT = (
    "stopped", "starting", "flying", "paused", "after_flight",
)

STATE_TEXT = {
    STOPPED: "Stopped",
    STARTING: "Starting",
    FLYING: "Flying with AutoOrtho",
    PAUSED: "Paused",
    AFTER_FLIGHT: "Stopped",
}

PHASE_TEXT = {
    "starting": "loading",
    "taxi": "on the ground",
    "departure_arrival": "departure or arrival",
    "cruise": "cruise",
    "idle": "",
}

# Governor bottleneck states -> the gauge that gets the chip.
BOTTLENECK_GAUGE = {
    "network_room": "network",
    "network_full": "network",
    "network_pushback": "network",
    "slow_imagery": "network",
    "compute": "computer",
    "compute_limit": "computer",
    "sim_fps": "computer",
    "memory": "computer",
    "vram": "graphics",
}


@dataclass
class Gauge:
    """One resource gauge: a headline, a detail line, a share and a level."""

    title: str
    value: str
    detail: str = ""
    share: Optional[float] = None       # used / limit, 0..1 (None: no bar)
    level: str = "success"              # success / warning / error / info
    bottleneck: bool = False

    @property
    def sentence(self) -> str:
        parts = [f"{self.title}: {self.value}"]
        if self.detail:
            parts.append(self.detail)
        if self.bottleneck:
            parts.append("this is what limits the scenery now")
        return ". ".join(parts) + "."


@dataclass
class HomeSnapshot:
    state: str = STOPPED
    # status strip
    pill: str = "Stopped"
    strip_detail: str = ""
    flight_line: str = ""
    # hero
    fidelity: Optional[float] = None
    hero_value: str = ""
    target_text: str = ""
    sharp_after: str = ""
    trend: List[Optional[float]] = field(default_factory=list)
    # starting
    loading_lines: List[str] = field(default_factory=list)
    # gauges
    gauges: List[Gauge] = field(default_factory=list)
    # activity
    activity_lines: List[str] = field(default_factory=list)
    # auto-tuning
    tuning_visible: bool = False
    tuning_lines: List[str] = field(default_factory=list)
    # tile radar: (east nm, north nm, code) around the aircraft
    radar: List[list] = field(default_factory=list)
    heading: Optional[float] = None
    corridor_nm: float = 0.0
    # tray / taskbar
    summary: str = "AutoOrtho"
    # after flight
    last_flight: Optional[dict] = None


def _gb(value: float) -> str:
    return f"{value / GB:.1f} GB"


def _level(share: Optional[float]) -> str:
    if share is None:
        return "info"
    if share >= 0.95:
        return "error"
    if share >= 0.8:
        return "warning"
    return "success"


def trend_of(timeline, now: Optional[float] = None) -> List[Optional[float]]:
    """Session fidelity in 5-second buckets (the last ten minutes)."""
    points = [p for p in timeline or () if p.get("t") is not None]
    if not points:
        return []
    end = now if now is not None else points[-1]["t"]
    start = end - TREND_BUCKET_S * TREND_POINTS
    buckets: List[List[float]] = [[] for _ in range(TREND_POINTS)]
    for point in points:
        if point["t"] < start or point.get("fidelity") is None:
            continue
        index = min(TREND_POINTS - 1, int((point["t"] - start) / TREND_BUCKET_S))
        buckets[index].append(point["fidelity"])
    first = next((i for i, b in enumerate(buckets) if b), None)
    if first is None:
        return []
    return [sum(b) / len(b) if b else None for b in buckets[first:]]


def _network_gauge(snap) -> Gauge:
    mbps = snap.get("mbps") or 0.0
    capacities = [c for c in (snap.get("line_mbps"), snap.get("bandwidth_mbps")) if c]
    capacity = min(capacities) if capacities else None
    errors = snap.get("error_share") or 0.0
    detail = f"{errors * 100:.1f} % retries"
    if capacity:
        value = f"{mbps:.0f} Mbit/s of ~{capacity:.0f}"
        share = min(1.0, mbps / capacity) if capacity else None
    else:
        value = f"{mbps:.0f} Mbit/s"
        share = None
    level = "error" if errors > 0.03 else "warning" if errors > 0.01 else _level(share)
    return Gauge("Network", value, detail, share, level)


def _computer_gauge(snap, cpu_limit_cores) -> Gauge:
    home = snap.get("home") or {}
    cpu = snap.get("cpu") or 0.0
    ao = home.get("ao_memory_bytes") or 0
    limit = snap.get("ram_limit_bytes")
    value = f"CPU {cpu:.0f} %"
    if cpu_limit_cores:
        value += f" · limit {cpu_limit_cores} cores"
    if limit:
        ram = f"RAM {_gb(ao)} of {_gb(limit)} (AutoOrtho)"
        share = min(1.0, ao / limit)
    else:
        ram = f"RAM {_gb(ao)} (AutoOrtho)"
        share = None
    free = home.get("free_ram_bytes") or 0
    total = home.get("total_ram_bytes") or 0
    lines = [ram]
    # A second line only when the system itself is low on memory.
    if total and free and (free < 1.5 * GB or free < 0.08 * total):
        lines.append(f"system free RAM low: {_gb(free)}")
    level = _level(max(share or 0.0, cpu / 100.0))
    if len(lines) > 1:
        level = "error"
    return Gauge("Computer", value, " · ".join(lines), share, level)


def _graphics_gauge(snap, card_bytes) -> Gauge:
    used = snap.get("graphics_memory_bytes") or 0
    budget = snap.get("vram_budget_bytes")
    card = f" ({_gb(card_bytes)} card)" if card_bytes else ""
    if budget:
        share = min(1.0, used / budget) if budget else None
        detail = f"of {_gb(budget)} budget{card}"
    else:
        share = None
        detail = f"graphics memory unknown{card}" if not card_bytes else f"of the card{card}"
    return Gauge("Graphics memory", f"~{_gb(used)} photo textures", detail, share,
                 _level(share))


def _flight_line(home, provider, route) -> str:
    parts = []
    if route:
        parts.append(route)
    phase = home.get("vertical") or ""
    if home.get("phase") in ("taxi",):
        phase = "on the ground"
    alt = home.get("altitude_ft")
    if alt is not None and home.get("connected"):
        parts.append(f"{phase + ' ' if phase else ''}{alt:,.0f} ft AGL".strip())
    speed = home.get("speed_kt")
    if speed is not None and home.get("connected"):
        parts.append(f"{speed:.0f} kt")
    if provider:
        parts.append(provider)
    return " · ".join(parts)


def _tuning_lines(snap, now) -> List[str]:
    decisions = snap.get("decisions") or []
    applied = [d for d in decisions if getattr(d, "applied", False)]
    lines = []
    if decisions:
        last = decisions[-1]
        age = max(0.0, now - last.t) if now is not None else None
        when = f" · {age:.0f} s ago" if age is not None and age < 3600 else ""
        prefix = "" if last.applied else "Suggests: "
        lines.append(f"{prefix}{last.text()}{when}")
    else:
        lines.append("No changes yet: "
                     + str(snap.get("state_text") or "measuring").lower())
    pinned = len(snap.get("pinned") or ())
    count = len(applied) if snap.get("mode") == "auto" else len(decisions)
    noun = "changes" if snap.get("mode") == "auto" else "suggestions"
    lines.append(f"{count} {noun} this flight · {pinned} pinned value"
                 f"{'' if pinned == 1 else 's'}")
    return lines


def build(
    *,
    runtime: str,
    governor: Optional[dict] = None,
    packs: int = 0,
    xplane_connected: bool = False,
    provider: str = "",
    route: str = "",
    target_text: str = "",
    cpu_limit_cores: Optional[int] = None,
    card_bytes: Optional[int] = None,
    last_flight: Optional[dict] = None,
    show_last_flight: bool = False,
    mode_label: str = "",
    now: Optional[float] = None,
) -> HomeSnapshot:
    """``runtime`` is the app's state: stopped, starting, running or error."""
    snap = governor or {}
    home = snap.get("home") or {}
    connection = "X-Plane connected" if xplane_connected else "X-Plane not connected"
    pack_text = f"{packs} scenery pack{'' if packs == 1 else 's'}"
    out = HomeSnapshot(target_text=target_text)

    if runtime in ("stopped", "error"):
        out.state = AFTER_FLIGHT if (show_last_flight and last_flight) else STOPPED
        out.pill = "Stopped" if runtime == "stopped" else "Error"
        out.strip_detail = f"{pack_text} configured"
        out.last_flight = last_flight
        out.summary = f"AutoOrtho · {out.pill}"
        return out
    if runtime == "starting" or not snap:
        out.state = STARTING
    elif home.get("paused"):
        out.state = PAUSED
    elif not (home.get("connected") or xplane_connected):
        # Mounted, X-Plane loading (it sends no positions on its loading screen).
        out.state = STARTING
    else:
        out.state = FLYING

    out.pill = STATE_TEXT[out.state]
    out.strip_detail = f"{pack_text} · {connection}"
    out.flight_line = _flight_line(home, provider, route)

    fidelity = snap.get("fidelity")
    out.fidelity = fidelity
    out.hero_value = (f"{fidelity * 100:.0f} % sharp at target" if fidelity is not None
                      else "Measuring sharpness")
    tts = snap.get("time_to_sharp_s")
    out.sharp_after = f"Sharp {tts:.0f} s after the last area load" if tts else ""
    out.trend = trend_of(snap.get("timeline"), now=None)

    if out.state == STARTING:
        lines = [f"Mounting {pack_text} ✓" if runtime != "starting"
                 else f"Mounting {pack_text}…"]
        lines.append("Waiting for X-Plane ✓" if xplane_connected
                     else "Waiting for X-Plane…")
        if fidelity is not None:
            lines.append(f"Loading area: {fidelity * 100:.0f} % of nearby scenery sharp")
        else:
            lines.append("Loading area: measuring")
        out.loading_lines = lines

    gauges = [
        _network_gauge(snap),
        _computer_gauge(snap, cpu_limit_cores),
        _graphics_gauge(snap, card_bytes),
    ]
    chip = BOTTLENECK_GAUGE.get(snap.get("state") or "")
    for gauge in gauges:
        gauge.bottleneck = {
            "network": "Network", "computer": "Computer", "graphics": "Graphics memory",
        }.get(chip) == gauge.title
    out.gauges = gauges

    textures = home.get("textures") or 0
    cache = home.get("cache_share")
    downloaded = home.get("downloaded_bytes") or 0
    first = f"{textures:,} textures served"
    if cache is not None:
        first += f" · {cache * 100:.0f} % from cache"
    first += f" · {_gb(downloaded)} downloaded"
    second = f"Building {home.get('building_now') or 0} now"
    horizon_m = home.get("horizon_m") or 0
    speed_kt = home.get("speed_kt") or 0
    if horizon_m and speed_kt and speed_kt > 40:
        minutes = horizon_m / (speed_kt * 1852 / 60)
        second += f" · Ahead of you: next ~{minutes:.0f} min / {horizon_m / 1852:.0f} nm ready"
    elif horizon_m:
        second += f" · Ready around you: {horizon_m / 1852:.0f} nm"
    out.activity_lines = [first, second]

    out.radar = list(home.get("radar") or [])
    out.heading = home.get("heading")
    lookahead = (snap.get("values") or {}).get("autoortho.prefetch_lookahead")
    if lookahead and speed_kt:
        out.corridor_nm = min(RADAR_RADIUS_NM, speed_kt / 60.0 * float(lookahead))

    mode = snap.get("mode") or ""
    out.tuning_visible = bool(snap) and mode in ("auto", "advise")
    if out.tuning_visible:
        out.tuning_lines = _tuning_lines(snap, now)

    parts = ["AutoOrtho"]
    if fidelity is not None:
        parts.append(f"{fidelity * 100:.0f} % sharp")
    parts.append(f"{snap.get('mbps') or 0:.0f} Mbit/s")
    parts.append(out.pill.replace(" with AutoOrtho", ""))
    out.summary = " · ".join(parts)
    out.last_flight = last_flight
    return out


def radar_text(radar) -> str:
    """The radar in words, for screen readers."""
    if not radar:
        return "No tiles loaded near the aircraft yet"
    counts = [0, 0, 0, 0]
    for _dx, _dy, code in radar:
        if 0 <= int(code) < 4:
            counts[int(code)] += 1
    parts = [f"{n} {RADAR_TEXT[i]}" for i, n in enumerate(counts) if n]
    return f"{len(radar)} tiles within {RADAR_RADIUS_NM:.0f} nm: " + ", ".join(parts)


def verdict_level(summary: Optional[dict]) -> str:
    verdict = (summary or {}).get("verdict", "")
    if verdict.startswith("Smooth"):
        return "success"
    if verdict.startswith("A few"):
        return "warning"
    if verdict.startswith("Often"):
        return "error"
    return "info"


def last_flight_lines(summary: Optional[dict]) -> List[str]:
    """The last-flight card in plain words."""
    if not summary:
        return []
    lines = []
    phases = summary.get("fidelity_by_phase") or {}
    names = {"starting": "Loading", "taxi": "Ground",
             "departure_arrival": "Departure and arrival", "cruise": "Cruise"}
    if phases:
        lines.append(" · ".join(
            f"{names.get(k, k.title())} {v * 100:.0f} %" for k, v in phases.items()
        ))
    trip = []
    if summary.get("distance_nm"):
        trip.append(f"{summary['distance_nm']:.0f} nm")
    if summary.get("duration_min"):
        trip.append(f"{summary['duration_min']:.0f} min")
    if summary.get("downloaded_gb") is not None:
        trip.append(f"{summary['downloaded_gb']:.1f} GB downloaded")
    if summary.get("cache_share") is not None:
        trip.append(f"{summary['cache_share'] * 100:.0f} % from cache")
    if trip:
        lines.append(" · ".join(trip))
    brakes = (summary.get("ram_brake_activations") or 0) + (
        summary.get("disk_brake_activations") or 0)
    if brakes:
        lines.append(f"Guards stepped in {brakes} time{'' if brakes == 1 else 's'}")
    learned = summary.get("learned") or {}
    labels = summary.get("learned_labels") or {}
    if learned:
        shown = ", ".join(
            f"{v:g} {labels[k].lower()}" if k in labels else f"{k.split('.', 1)[-1]} {v:g}"
            for k, v in list(learned.items())[:3]
        )
        lines.append(f"Learned for next time: {shown}")
    return lines


def ended_text(summary: Optional[dict], now: Optional[float] = None) -> str:
    ended = (summary or {}).get("ended")
    if not ended:
        return ""
    now = now if now is not None else time.time()
    minutes = max(0, int((now - float(ended)) / 60))
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return time.strftime("%d %b", time.localtime(float(ended)))

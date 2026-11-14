"""The aircraft as files mode sees it, from the parent's flight snapshot.

The parent's dataref tracker publishes ``flight_state`` to the shared stats
store once a second (``AOMount.start_reporter``). Files mode reads it and
derives what X-Plane does not send: ground track and speed from the last
positions when the aircraft was moved from outside, and jumps.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from . import geo

STALE_S = 3.0
JUMP_KM = 20.0


@dataclass
class FlightState:
    valid: bool = False
    lat: float = 0.0
    lon: float = 0.0
    agl_m: float = 0.0
    track_deg: float = 0.0
    gs_mps: float = 0.0
    vs_mps: float = 0.0
    width_px: float = 0.0
    fov_deg: float = 0.0
    paused: bool = False
    scenery_loading: bool = False
    camera_lat: Optional[float] = None
    camera_lon: Optional[float] = None
    camera_agl_m: Optional[float] = None
    timestamp: float = 0.0
    jumped: bool = False


def _f(state, key, default=0.0):
    try:
        value = state.get(key, default)
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def from_snapshot(state, previous=None, now=None):
    """A ``FlightState`` from the parent's ``flight_state`` dict."""
    now = time.time() if now is None else now
    if not isinstance(state, dict):
        return FlightState()
    stamp = _f(state, "timestamp")
    if not (state.get("connected") and state.get("data_valid")) or now - stamp > STALE_S:
        return FlightState(timestamp=stamp)
    fs = FlightState(
        valid=True, lat=_f(state, "lat"), lon=_f(state, "lon"),
        agl_m=max(0.0, _f(state, "alt")), track_deg=_f(state, "hpath", _f(state, "hdg")),
        gs_mps=max(0.0, _f(state, "spd")), vs_mps=_f(state, "vh_ind"),
        width_px=_f(state, "window_width"), fov_deg=_f(state, "fov_deg"),
        paused=bool(state.get("paused")),
        scenery_loading=bool(_f(state, "async_scenery_load") >= 0.5),
        timestamp=stamp,
    )
    # Camera from view_x/z vs local_x/z (OpenGL frame: +x east, -z north).
    try:
        if all(k in state for k in ("view_x", "view_z", "local_x", "local_z")):
            east = _f(state, "view_x") - _f(state, "local_x")
            north = -(_f(state, "view_z") - _f(state, "local_z"))
            dist_km = math.hypot(east, north) / 1000.0
            if dist_km > 0.5:
                bearing = math.degrees(math.atan2(east, north)) % 360.0
                fs.camera_lat, fs.camera_lon = geo.destination(fs.lat, fs.lon, bearing, dist_km)
                fs.camera_agl_m = max(0.0, fs.agl_m + _f(state, "view_y") - _f(state, "local_y"))
    except Exception:
        pass
    if previous is not None and previous.valid:
        moved_km = geo.haversine_km(previous.lat, previous.lon, fs.lat, fs.lon)
        dt = max(1e-3, stamp - previous.timestamp)
        if moved_km > JUMP_KM:
            fs.jumped = True
        elif fs.gs_mps < 1.0 and moved_km > 0.05 and dt < 10:
            # Position set from outside (no ground speed): derive motion.
            fs.gs_mps = moved_km * 1000.0 / dt
            fs.track_deg = _bearing(previous.lat, previous.lon, fs.lat, fs.lon)
    return fs


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


class FlightFeed:
    """Polls a reader (``get_stat('flight_state')``) into ``FlightState``."""

    def __init__(self, reader):
        self.reader = reader
        self.current = FlightState()
        self.override = None   # set by the gate from the plugin's LOAD / HOLD

    OVERRIDE_FRESH_S = 30.0

    def poll(self):
        """The newest flight state: X-Plane's datarefs, else the plugin's
        position (X-Plane sends no datarefs while the plugin holds it)."""
        try:
            snapshot = self.reader()
        except Exception:
            snapshot = None
        previous = self.current if not getattr(self.current, "_from_plugin", False) else None
        state = from_snapshot(snapshot, previous)
        if not state.valid and self.override is not None:
            lat, lon, stamp = self.override
            if time.time() - stamp < self.OVERRIDE_FRESH_S:
                state = FlightState(valid=True, lat=lat, lon=lon, timestamp=stamp)
                state._from_plugin = True
        self.current = state
        return state

    def set_position(self, lat, lon):
        self.override = (float(lat), float(lon), time.time())

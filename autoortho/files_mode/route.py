"""The SimBrief route, for the planner (plan section 5.6.1).

The parent loads the flight plan and shares it through the stats store
(``simbrief_route_snapshot``, ``simbrief_route_generation``,
``simbrief_route_enabled``), as it does for the FUSE workers. Files mode
plans along the route ahead when the aircraft is on it; the planner itself
checks that the aircraft flies its way (``planner.route_points``).
"""

import logging
import time

log = logging.getLogger(__name__)

REFRESH_S = 5.0
UPCOMING_FIXES = 40


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


class RouteFeed:
    def __init__(self, get_stat, manager=None, cfg=None, clock=time.monotonic):
        self.get_stat = get_stat
        self._manager = manager
        self._cfg = cfg
        self.clock = clock
        self._generation = None
        self._last = 0.0
        self._cached = None

    @property
    def manager(self):
        if self._manager is None:
            try:
                from autoortho.utils.simbrief_flight import simbrief_flight_manager
            except ImportError:
                from utils.simbrief_flight import simbrief_flight_manager
            self._manager = simbrief_flight_manager
        return self._manager

    def _config(self):
        if self._cfg is None:
            try:
                from autoortho.aoconfig import CFG
            except ImportError:
                from aoconfig import CFG
            self._cfg = CFG
        return self._cfg

    def _sync(self):
        generation = self.get_stat("simbrief_route_generation")
        if not generation or generation == self._generation:
            return
        self._generation = generation
        snapshot = self.get_stat("simbrief_route_snapshot")
        if snapshot:
            if not self.manager.load_flight_data(snapshot):
                log.warning("Files mode could not read the shared SimBrief route")
            else:
                log.info("Files mode follows the SimBrief route")
        else:
            self.manager.clear()

    def enabled(self):
        shared = self.get_stat("simbrief_route_enabled")
        if shared in (True, False):
            return shared
        cfg = self._config()
        return _truthy(getattr(getattr(cfg, "simbrief", None), "use_flight_data", False))

    def route(self, lat, lon):
        """``[(lat, lon), ...]`` of the fixes ahead, or None when off route."""
        now = self.clock()
        if now - self._last < REFRESH_S:
            return self._cached
        self._last = now
        self._cached = None
        try:
            self._sync()
            if not self.enabled() or not self.manager.is_loaded:
                return None
            cfg = self._config()
            threshold = float(getattr(getattr(cfg, "simbrief", None),
                                      "route_deviation_threshold_nm", 40))
            if not self.manager.is_on_route(lat, lon, threshold):
                return None
            fixes = self.manager.get_upcoming_fixes(lat, lon, UPCOMING_FIXES)
            self._cached = [(float(f.lat), float(f.lon)) for f in fixes] or None
        except Exception as exc:
            log.debug("SimBrief route unavailable: %s", exc)
            self._cached = None
        return self._cached

"""FilesSession: wires every files-mode component and runs its loop.

Loop (every ``PASS_S``): read the flight, follow X-Plane's window, make
texture names for the window plus a ring (release far ones), plan levels,
hand the plan to the scheduler, fill stand-ins for late textures, keep the
disk budget (every ``EVICT_S``), publish stats and the start status the
gate answers the plugin with.
"""

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field

from . import (
    catalogue as catalogue_mod, evict, fsops, gate as gate_mod, materializer,
    neutral, pack as pack_mod, planner as planner_mod, plugin_install, preflight,
    rings, scheduler as scheduler_mod, store as store_mod, window as window_mod,
    xpprefs,
)
from .flightfeed import FlightFeed

log = logging.getLogger(__name__)

PASS_S = 2.0
EVICT_S = 10.0
SCENERY_PAUSE_TAIL_S = 5.0
SCENERY_PAUSE_MAX_S = 120.0
RING_ON_EXIT = 3
STANDIN_PER_PASS = 20
START_NAMES_MAX_S = 60.0
SEASON_SWEEP_S = 60.0
SEASON_REFRESH_MAX = 200
SEASON_URGENCY_S = 900.0
DSF_SWEEP_S = 10.0
GB = 1024 ** 3


@dataclass
class FilesConfig:
    files_root: str
    custom_scenery: str
    regions: dict                      # {name: region_root}
    xplane_path: str = ""
    disk_gb: float = 20.0
    camera_reach_km: float = 10.0
    strategy: str = "link"             # link | sparse
    gate_port: int = gate_mod.DEFAULT_PORT
    dxt_format: str = "BC1"
    missing_rgb: tuple = (66, 77, 55)
    imagery_floor_zl: int = 13
    install_plugin: bool = True
    exclude_from_search: bool = False


def builder_counts(cpu_count=None, ram_gb=None):
    """``(in_flight, at_start)`` builders: a third of the cores (max 4, one
    per 8 GB) in flight; all but two (one per 3 GB) while X-Plane is held."""
    import os as _os
    cpus = cpu_count or _os.cpu_count() or 4
    if ram_gb is None:
        try:
            import psutil
            ram_gb = psutil.virtual_memory().total / GB
        except Exception:
            ram_gb = 16
    flight = max(1, min(4, cpus // 3, int(ram_gb // 8) or 1))
    start = max(flight, min(cpus - 2, int(ram_gb // 3) or 1))
    return flight, start


class FilesSession:
    def __init__(self, config: FilesConfig, flight_reader, service=None,
                 size_of=None, xplane_running=None, clock=time.monotonic,
                 route_fn=None, season_key_fn=None, dsf_swapper_factory=None):
        self.cfg = config
        self.flight_reader = flight_reader
        self.service = service
        # route_fn(lat, lon) -> [(lat, lon), ...] ahead, or None (SimBrief).
        self.route_fn = route_fn
        # season_key_fn(row, col, zoom) -> key of the colours to bake.
        self.season_key_fn = season_key_fn
        # dsf_swapper_factory(pack) -> DsfSwapper (time exclusion), or None.
        self.dsf_swapper_factory = dsf_swapper_factory
        self.dsf_swapper = None
        self.relocating = False
        self.search_indexed = None
        self._relocate_started = None
        self._last_season = 0.0
        self._last_dsf = 0.0
        self._size_of_override = size_of
        self._xplane_running = xplane_running or xpprefs.xplane_running
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._loop_heartbeat = clock()
        self.ready = False
        self.error = None
        self.starting = False
        self._start_total = 0
        self._start_at = None
        self._start_names_deadline = None
        self._pause_until = 0.0
        self._pause_started = None
        self._last_evict = 0.0
        self.window = window_mod.XPlaneWindow()
        self.status = gate_mod.StartStatus()
        self.last_plan = None
        self.route = None
        self._zoom_cache = {}
        self.counters = {"files_pause_s": 0.0, "files_late": 0, "files_patchy": 0,
                         "files_plan_ms": 0.0, "files_candidates": 0,
                         "files_late_never_queued": 0, "files_late_queued": 0,
                         "files_late_building": 0, "files_late_total": 0,
                         "files_season_refresh": 0, "files_relocations": 0,
                         "files_route_points": 0}

    # -- setup -----------------------------------------------------------
    def setup(self):
        cfg = self.cfg
        preflight.check(cfg.files_root, cfg.disk_gb)
        self.pack = pack_mod.Pack(cfg.files_root, cfg.custom_scenery, cfg.regions)
        running = bool(self._xplane_running())
        self.pack.setup(xplane_running=running)
        meta = self.pack.meta_dir
        self.neutral = neutral.NeutralPool(os.path.join(meta, "neutral"), cfg.missing_rgb)
        self.index = store_mod.TextureIndex(os.path.join(meta, "textures.sqlite"))
        self.index.start()
        self.store = store_mod.TextureStore(cfg.files_root, self.neutral, self.index,
                                            strategy=cfg.strategy, rgb=cfg.missing_rgb)
        for region in cfg.regions:
            self.store.cleanup_temp_files(region)
        self.catalogue = catalogue_mod.Catalogue(os.path.join(meta, "catalogue"))
        self._load_catalogues()
        self.rings = rings.RingModel.load(os.path.join(meta, "rings.json"))
        self.prefs = xpprefs.XPlanePrefs(cfg.xplane_path).refresh()
        self.materializer = materializer.Materializer(self.catalogue, self.store, self.size_of)
        self.planner = planner_mod.Planner(self.catalogue, self.store, self.size_of,
                                           self.rings, cfg.camera_reach_km)
        self.flight_builders, self.start_builders = builder_counts()
        self.scheduler = scheduler_mod.Scheduler(
            self._build, workers=self.flight_builders, paused=self._paused,
            under_pressure=self._under_memory_pressure,
        )
        self.evictor = evict.Evictor(self.store, int(cfg.disk_gb * GB), self.rings,
                                     cfg.camera_reach_km)
        self.feed = FlightFeed(self.flight_reader)
        self.gate = gate_mod.Gate(self.start_status, on_load=self.on_flight_load,
                                  on_frame=self._on_frame, on_position=self._on_hold_position,
                                  pause_fn=self.pause_wanted, port=cfg.gate_port)
        if self.dsf_swapper_factory is not None:
            try:
                self.dsf_swapper = self.dsf_swapper_factory(self.pack)
            except Exception as exc:
                log.warning("Time exclusion unavailable in files mode: %s", exc)

    def _load_catalogues(self):
        threads = []
        errors = []

        def load(name, root):
            try:
                self.catalogue.load_or_scan(name, root, stop=self._stop)
            except Exception as exc:
                errors.append((name, exc))

        for name, root in self.cfg.regions.items():
            t = threading.Thread(target=load, args=(name, root), name=f"AO-Catalogue-{name}",
                                 daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        for name, exc in errors:
            log.error("Catalogue of %s unavailable: %s", name, exc)

    def start(self):
        """Set up, install links, open the gate and run the loop."""
        self.setup()
        port = self.gate.start()
        self.pack.install_links()
        self.pack.write_markers()
        if self.cfg.install_plugin and self.cfg.xplane_path:
            ok, msg = plugin_install.install(self.cfg.xplane_path, self.cfg.files_root, port,
                                             self.cfg.custom_scenery)
            log.info("X-Plane plugin: %s", msg)
        self._windows_integration()
        self.scheduler.start()
        self._thread = threading.Thread(target=self._run, name="AO-FilesLoop", daemon=True)
        self._thread.start()
        self.ready = True
        return port

    def _windows_integration(self):
        """Windows Search: check (read-only) and exclude when asked."""
        import sys as _sys
        if _sys.platform != "win32":
            return
        try:
            from . import windows_integration
            indexed = windows_integration.search_indexes(self.cfg.files_root)
            self.search_indexed = indexed
            if indexed and self.cfg.exclude_from_search:
                windows_integration.exclude_from_search_async(self.cfg.files_root)
                self.search_indexed = None  # being excluded
            elif indexed:
                log.warning("Windows Search indexes the files folder (%s); it costs CPU in "
                            "flight. Set [files] exclude_from_search = True to stop it.",
                            self.cfg.files_root)
        except Exception as exc:
            log.debug("Windows Search check skipped: %s", exc)

    # -- sizes -----------------------------------------------------------
    def size_of(self, cat, i):
        if self._size_of_override is not None:
            return self._size_of_override(cat, i)
        zoom = int(cat.zooms[i])
        top = self._zoom_cache.get(zoom)
        if top is None:
            max_zoom = self._target_zoom(zoom)
            top = rings.top_px_for(zoom, max_zoom)
            self._zoom_cache[zoom] = top
        return top, self.cfg.dxt_format

    def _target_zoom(self, zoom):
        try:
            try:
                from autoortho.texture_service import target_zoom
            except ImportError:
                from texture_service import target_zoom
            return target_zoom(self.service.tc, zoom)
        except Exception:
            return zoom

    # -- building ----------------------------------------------------------
    def _build(self, demand):
        try:
            from autoortho.texture_service import Demand, DemandSource, TextureKey, BuildPolicy
        except ImportError:
            from texture_service import Demand, DemandSource, TextureKey, BuildPolicy
        key = TextureKey(demand.row, demand.col, demand.maptype, demand.zoom)
        max_zoom = demand.zoom + int(round(math.log2(max(1, demand.top_px) / 4096.0)))
        source = DemandSource.START if self.starting else DemandSource.PLANNER
        built = self.service.build(
            Demand(key, demand.level, demand.urgency_s, source, max_zoom=max_zoom),
            BuildPolicy(priority=_download_priority(demand.urgency_s)),
        )
        result = self.store.write(demand.region, built)
        if result.ok and built.patchy:
            self._schedule_patchy(demand.region, key.name)
        return result

    def _schedule_patchy(self, region, name):
        st = self.index.get(name)
        if st is None:
            return
        delay = 60.0
        if st.retry_at and st.built_at:
            delay = min(1800.0, max(60.0, (st.retry_at - st.built_at) * 2))
        st.retry_at = time.time() + delay
        self.index.put(st)

    # -- pauses ------------------------------------------------------------
    def _paused(self):
        if self.starting:
            return False  # X-Plane is waiting for us: never pause at a start
        return self.clock() < self._pause_until

    def _under_memory_pressure(self):
        """getortho's RAM brake (one per process): urgent work only."""
        try:
            try:
                from autoortho import getortho
            except ImportError:
                import getortho
            brake = getattr(getortho, "ram_brake", None)
            return bool(brake.engaged) if brake is not None else False
        except Exception:
            return False

    def _on_frame(self, ms):
        log.info("X-Plane frame took %.0f ms (files mode: %d queued, %d building)",
                 ms, self.scheduler.depth(), len(self.scheduler.in_flight()))

    # -- the start ---------------------------------------------------------
    def on_flight_load(self, lat, lon):
        """The plugin says a flight starts at (lat, lon)."""
        log.info("Flight start at %.4f %.4f: building the start area", lat, lon)
        self.feed.set_position(lat, lon)
        self.window.reset(lat, lon)
        self.starting = True
        self._start_total = 0
        self._start_at = self.clock()
        self._start_names_deadline = self.clock() + START_NAMES_MAX_S
        self.scheduler.set_workers(self.start_builders)
        self.status = gate_mod.StartStatus(ready=False, updated=self.clock())

    # -- moving the aircraft in flight (no plugin hold) ---------------------
    RELOCATE_MAX_S = 120.0

    def begin_relocation(self, lat, lon):
        """The aircraft was moved far in flight: build the new area first.

        With the plugin present, X-Plane is paused (``AO1 PAUSE`` in answer to
        its polls) until the area is built, at most ``RELOCATE_MAX_S``.
        """
        if self.starting:
            return
        log.info("Aircraft moved to %.4f %.4f: building the area before it flies on", lat, lon)
        self.counters["files_relocations"] += 1
        self.window.reset(lat, lon)
        self.starting = True
        self.relocating = True
        self._start_total = 0
        self._start_at = self._relocate_started = self.clock()
        self._start_names_deadline = self.clock() + START_NAMES_MAX_S
        self.scheduler.set_workers(self.start_builders)

    def pause_wanted(self):
        """True while X-Plane should stay paused for a relocation."""
        if not self.relocating:
            return False
        if self._relocate_started is not None and (
            self.clock() - self._relocate_started > self.RELOCATE_MAX_S
        ):
            log.info("Relocation took over %.0fs: X-Plane goes on", self.RELOCATE_MAX_S)
            self._end_start()
            return False
        return True

    def _end_start(self):
        self.starting = False
        self.relocating = False
        self._relocate_started = None
        self.scheduler.set_workers(self.flight_builders)

    # -- live levers (files-mode governor) --------------------------------
    LEVER_BUILDERS = "files.builders"
    LEVER_LOOKAHEAD = "files.lookahead_s"
    LEVER_MARGIN = "files.margin"

    def apply_levers(self, levers):
        """Apply governor levers published by the parent."""
        applied = {}
        for name, value in (levers or {}).items():
            try:
                if name == self.LEVER_BUILDERS:
                    self.flight_builders = max(1, int(value))
                    self.start_builders = max(self.start_builders, self.flight_builders)
                    if not self.starting:
                        self.scheduler.set_workers(self.flight_builders)
                elif name == self.LEVER_LOOKAHEAD:
                    self.planner.lookahead_s = max(30.0, float(value))
                elif name == self.LEVER_MARGIN:
                    self.rings.margin = max(1.0, float(value))
                else:
                    continue
                applied[name] = value
            except (TypeError, ValueError):
                continue
        if applied:
            log.info("Files mode levers: %s", applied)
        return applied

    def lever_values(self):
        return {
            self.LEVER_BUILDERS: self.flight_builders,
            self.LEVER_LOOKAHEAD: self.planner.lookahead_s,
            self.LEVER_MARGIN: self.rings.margin,
        }

    def _on_hold_position(self, lat, lon):
        if not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            self.feed.set_position(lat, lon)

    def start_status(self):
        return self.status

    def _update_start_status(self, missing_window, plan):
        if not self.starting:
            self.status = gate_mod.StartStatus(ready=True, reason="not starting",
                                               updated=self.clock())
            return
        urgent = sum(1 for d in plan.demands if d.urgency_s <= 0) if plan else 0
        urgent = max(urgent, self.scheduler.urgent_depth())
        self._start_total = max(self._start_total, urgent)
        total = self._start_total
        built = max(0, total - urgent)
        names_ok = not missing_window or (
            self._start_names_deadline is not None and self.clock() > self._start_names_deadline
        )
        ready = names_ok and urgent == 0 and plan is not None and plan.candidates > 0
        if not names_ok:
            reason = "making texture names"
        else:
            reason = "start area built" if ready else "building"
        self.status = gate_mod.StartStatus(ready=ready, built=built, total=total,
                                           reason=reason, updated=self.clock())
        if ready:
            held = self.clock() - (self._start_at or self.clock())
            log.info("%s area ready after %.1fs (%d textures)",
                     "New" if self.relocating else "Start", held, total)
            self._end_start()

    # -- loop --------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.step()
            except Exception:
                log.exception("Files mode pass failed")
            self._loop_heartbeat = self.clock()
            self._stop.wait(max(0.05, PASS_S - (self.clock() - started)))

    def step(self):
        flight = self.feed.poll()
        self.prefs.refresh()
        if not flight.valid:
            self.status = gate_mod.StartStatus(ready=False, reason="waiting for X-Plane",
                                               updated=self.clock())
            return None
        if flight.jumped:
            if self.gate.last_hello and not self.starting:
                self.begin_relocation(flight.lat, flight.lon)
            else:
                log.info("The aircraft moved more than 20 km: planning the new place")
                self.window.reset(flight.lat, flight.lon)
        else:
            self.window.follow(flight.lat, flight.lon)
        now = self.clock()
        if flight.scenery_loading and not self.starting:
            if self._pause_started is None:
                self._pause_started = now
            if now - self._pause_started < SCENERY_PAUSE_MAX_S:
                self._pause_until = now + SCENERY_PAUSE_TAIL_S
        else:
            if self._pause_started is not None:
                self.counters["files_pause_s"] += now - self._pause_started
            self._pause_started = None
        window_sq = self.window.tiles()
        ring_sq = self.window.with_ring(window_mod.RING)
        keep_sq = self.window.with_ring(window_mod.KEEP)
        wanted = sorted(ring_sq, key=lambda s: (s not in window_sq, _sq_dist(s, flight)))
        release = self.materializer.present - keep_sq
        deadline = None if self.starting else now + 1.5
        missing = self.materializer.sync(
            wanted, release, allow_release=not flight.scenery_loading, deadline=deadline,
        )
        missing_window = [s for s in missing if s in window_sq]
        finest = rings.finest_for_quality(self.prefs.tex_res)
        route = self.route
        if route is None and self.route_fn is not None:
            try:
                route = self.route_fn(flight.lat, flight.lon)
            except Exception as exc:
                log.debug("Route unavailable: %s", exc)
                route = None
        plan = self.planner.plan(flight, window_sq, ring_sq, finest=finest, route=route,
                                 starting=self.starting)
        self.last_plan = plan
        self.counters["files_late"] = 0 if self.starting else plan.late
        self.counters["files_plan_ms"] = plan.duration_s * 1000.0
        self.counters["files_candidates"] = plan.candidates
        self.counters["files_route_points"] = plan.route_points
        if not self.starting:
            self._count_late_causes(plan)
        self.scheduler.replace_plan(plan.demands)
        self._add_patchy_refresh(flight)
        if now - self._last_season >= SEASON_SWEEP_S:
            self._last_season = now
            self._season_sweep(flight, keep_sq)
        if self.dsf_swapper is not None and not flight.scenery_loading and (
            now - self._last_dsf >= DSF_SWEEP_S
        ):
            self._last_dsf = now
            try:
                self.dsf_swapper.sync(self.window.with_ring(3),
                                      window_mod.xplane_tiles(flight.lat, flight.lon))
            except Exception:
                log.exception("Time exclusion pass failed")
        if not self.starting:
            self._fill_standins(plan)
        self._update_start_status(missing_window, plan)
        if not flight.scenery_loading and now - self._last_evict >= EVICT_S:
            self._last_evict = now
            try:
                self.evictor.run_round(flight, window_mod.xplane_tiles(flight.lat, flight.lon),
                                       self.materializer.present)
            except Exception:
                log.exception("Disk budget round failed")
        return plan

    def _count_late_causes(self, plan):
        """Late textures (needed now, built coarser) by what the queue had
        done for them before this pass: the governor's evidence."""
        in_flight = self.scheduler.in_flight()
        never = queued = building = 0
        for d in plan.demands:
            if d.urgency_s > 0:
                continue
            if d.name in in_flight:
                building += 1
            elif self.scheduler.is_pending(d.name):
                queued += 1
            else:
                never += 1
        self.counters["files_late_never_queued"] += never
        self.counters["files_late_queued"] += queued
        self.counters["files_late_building"] += building
        self.counters["files_late_total"] += never + queued + building

    def _season_sweep(self, flight, squares, limit=SEASON_REFRESH_MAX):
        """Requeue built textures near the aircraft whose baked season colours
        are out of date (date or settings changed). Low priority."""
        if self.season_key_fn is None:
            return 0
        names = set()
        for square in squares:
            for cat, idx in self.catalogue.textures_in(square):
                for i in idx:
                    names.add((cat.region, cat.name(int(i))))
        queued = 0
        for region, name in names:
            if queued >= limit:
                break
            st = self.index.get(name)
            if st is None or st.built_level == store_mod.NEUTRAL:
                continue
            try:
                key = self.season_key_fn(st.row, st.col, st.zoom)
            except Exception:
                continue
            if key == st.season_key or self.scheduler.is_pending(name):
                continue
            lat, lon = _texture_centre(st)
            self.scheduler.add(planner_mod.PlannedDemand(
                region=region, index=-1, name=name, row=st.row, col=st.col,
                maptype=st.maptype, zoom=st.zoom, level=st.built_level,
                urgency_s=SEASON_URGENCY_S, distance_km=_km(flight.lat, flight.lon, lat, lon),
                built_level=st.built_level, top_px=st.top_px,
            ))
            queued += 1
        if queued:
            self.counters["files_season_refresh"] += queued
            log.info("Season colours changed: %d built textures queued for a rebuild", queued)
        return queued

    def _fill_standins(self, plan):
        done = 0
        for d in plan.demands:
            if done >= STANDIN_PER_PASS:
                break
            if d.urgency_s > 0 or d.built_level == store_mod.NEUTRAL:
                continue
            if self.scheduler.is_pending(d.name) and d.name in self.scheduler.in_flight():
                continue
            try:
                if self.store.fill_standin(d.region, d.name, d.level):
                    done += 1
            except Exception as exc:
                log.debug("Stand-in for %s failed: %s", d.name, exc)

    def _add_patchy_refresh(self, flight):
        now = time.time()
        patchy = 0
        for st in self.index.all():
            if not st.patchy:
                continue
            patchy += 1
            if st.retry_at and st.retry_at > now:
                continue
            lat, lon = _texture_centre(st)
            dist = _km(flight.lat, flight.lon, lat, lon)
            if dist > 100:
                continue
            self.scheduler.add(planner_mod.PlannedDemand(
                region=st.region, index=-1, name=st.name, row=st.row, col=st.col,
                maptype=st.maptype, zoom=st.zoom, level=st.built_level, urgency_s=600.0,
                distance_km=dist, built_level=st.built_level, top_px=st.top_px,
            ))
            st.retry_at = now + 60.0
            self.index.put(st)
        self.counters["files_patchy"] = patchy

    # -- stop ---------------------------------------------------------------
    def stop(self, timeout=5.0):
        if hasattr(self, "gate"):
            self.gate.release("AutoOrtho stopped")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if hasattr(self, "scheduler"):
            self.scheduler.stop(timeout=timeout)
        running = bool(self._xplane_running())
        if hasattr(self, "materializer") and running:
            # X-Plane keeps flying over the files: names for a few more rings.
            try:
                more = self.window.with_ring(RING_ON_EXIT)
                self.materializer.sync(sorted(more), (), allow_release=False,
                                       deadline=self.clock() + 10.0)
            except Exception as exc:
                log.warning("Could not make names beyond the window at exit: %s", exc)
        if hasattr(self, "gate"):
            self.gate.stop()
        if hasattr(self, "index"):
            self.index.close()
        if hasattr(self, "pack"):
            self.pack.remove_markers()
            if not running:
                removed = self.pack.remove_links()
                log.info("Removed files-mode links: %s", removed)
            else:
                log.info("X-Plane is running: files-mode links stay (it keeps flying over "
                         "what is on disk)")
        self.ready = False

    # -- stats ----------------------------------------------------------------
    def stats(self):
        out = dict(self.counters)
        if hasattr(self, "store"):
            out.update(self.store.counters)
            out.update(self.materializer.counters)
            out.update(self.scheduler.counters)
            out.update(self.evictor.counters)
            out.update(self.gate.counters)
            out["files_queue_depth"] = self.scheduler.depth()
            out["files_urgent_depth"] = self.scheduler.urgent_depth()
            out["files_build_rate"] = round(self.scheduler.build_rate(), 3)
            out["files_disk_used_bytes"] = self.index.total_alloc()
            out["files_disk_budget_bytes"] = self.evictor.budget_bytes
            out["files_built_textures"] = len(self.index)
            out["files_starting"] = int(self.starting)
            out["files_start_built"] = self.status.built
            out["files_start_total"] = self.status.total
            out["files_plugin_seen"] = int(bool(self.gate.last_hello))
            out["files_cruise"] = int(self.planner.cruise)
            out["files_relocating"] = int(self.relocating)
            out["files_search_indexed"] = int(bool(self.search_indexed))
            out["files_builders"] = self.scheduler.workers
            out["files_in_flight"] = len(self.scheduler.in_flight())
            out["files_levers"] = self.lever_values()
            if self.dsf_swapper is not None:
                out.update(self.dsf_swapper.counters)
                out["files_night"] = int(self.dsf_swapper.night)
        return out


def _download_priority(urgency_s):
    """getortho's download priority for a build (None without getortho)."""
    try:
        try:
            from autoortho import getortho
        except ImportError:
            import getortho
        return getortho.files_priority_for(urgency_s)
    except Exception:
        return None


def _sq_dist(square, flight):
    return abs(square[0] + 0.5 - flight.lat) + abs(square[1] + 0.5 - flight.lon)


def _texture_centre(st):
    from . import geo
    return geo.texture_centre(st.row, st.col, st.zoom)


def _km(lat1, lon1, lat2, lon2):
    from . import geo
    return geo.haversine_km(lat1, lon1, lat2, lon2)

"""What level each texture needs, and how soon (plan section 5.6).

Every pass (2 s): the aircraft now plus points along its ground track up to
``LOOKAHEAD_S`` ahead (and along the SimBrief route when on it). For every
texture of the candidate squares: its closest approach to that path gives
the sharpest level X-Plane may read (``rings``), and the time until the
aircraft reaches that level's ring gives the urgency. A demand is emitted
when the texture is built coarser than needed.

Distances to the path are ground distances (never more than the 3-D
distance X-Plane uses), so levels are built early rather than late; level 0
of an 8192 px file is the exception: it is costly, so it is measured in 3-D
from the height above ground, and skipped in cruise.
"""

import math
import time
from dataclasses import dataclass, field

import numpy as np

from . import geo, rings, window
from .store import NEUTRAL

LOOKAHEAD_S = 90.0
PATH_STEP_S = 10.0
CANDIDATE_KM = 300.0
STATIONARY_MPS = 5.0
NOMINAL_MPS = 50.0
# Cruise rule for 8192 px level 0 (plan 5.6.5).
CRUISE_ON_FT = 18500.0
CRUISE_OFF_FT = 17500.0
DESCENT_TARGET_FT = 13000.0
DESCENT_LEAD_S = 180.0
FT = 0.3048
ROUTE_NEAR_KM = 15.0
ROUTE_HEADING_DEG = 100.0
ROUTE_MIN_MPS = 25.0
WINDOW_LOAD_URGENCY_S = 0.0
RING_LOAD_URGENCY_S = 60.0

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQ = 111.320


@dataclass
class PlannedDemand:
    region: str
    index: int
    name: str
    row: int
    col: int
    maptype: str
    zoom: int
    level: int
    urgency_s: float
    distance_km: float
    built_level: int
    top_px: int


@dataclass
class PlanResult:
    demands: list = field(default_factory=list)
    late: int = 0           # needed now (urgency <= 0) and built coarser
    candidates: int = 0
    route_points: int = 0
    duration_s: float = 0.0
    cruise: bool = False


def path_points(lat, lon, track_deg, gs_mps, lookahead_s=LOOKAHEAD_S, step_s=PATH_STEP_S):
    """``[(lat, lon, t_s)]`` along the ground track (just ``now`` when parked)."""
    points = [(lat, lon, 0.0)]
    if gs_mps < STATIONARY_MPS:
        return points
    t = step_s
    while t <= lookahead_s + 1e-6:
        plat, plon = geo.destination(lat, lon, track_deg, gs_mps * t / 1000.0)
        points.append((plat, plon, t))
        t += step_s
    return points


def route_points(route, lat, lon, track_deg, gs_mps, lookahead_s=LOOKAHEAD_S):
    """Route fixes ahead within the lookahead, when flying along the route.

    ``route``: ``[(lat, lon), ...]`` in flight order.
    """
    if not route or gs_mps < ROUTE_MIN_MPS:
        return []
    dists = [geo.haversine_km(lat, lon, p[0], p[1]) for p in route]
    nearest = min(range(len(route)), key=lambda i: dists[i])
    if dists[nearest] > ROUTE_NEAR_KM * 4:
        return []
    out = []
    reach_km = gs_mps * lookahead_s / 1000.0
    travelled = 0.0
    prev = (lat, lon)
    for p in route[nearest:]:
        seg = geo.haversine_km(prev[0], prev[1], p[0], p[1])
        if seg < 1e-6:
            continue
        if travelled + seg > reach_km:
            # Cut the leg where the lookahead ends.
            frac = (reach_km - travelled) / seg
            cut = (prev[0] + (p[0] - prev[0]) * frac, prev[1] + (p[1] - prev[1]) * frac)
            out.append((cut[0], cut[1], lookahead_s))
            break
        travelled += seg
        out.append((p[0], p[1], travelled * 1000.0 / gs_mps))
        prev = p
    if out:
        brg = _bearing(lat, lon, out[0][0], out[0][1])
        if abs(((brg - track_deg) + 540) % 360 - 180) > ROUTE_HEADING_DEG and dists[nearest] > 1:
            return []
    return out


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def squares_within(lat, lon, radius_km):
    out = set()
    dlat = radius_km / KM_PER_DEG_LAT
    coslat = max(0.05, math.cos(math.radians(lat)))
    dlon = min(180.0, radius_km / (KM_PER_DEG_LON_EQ * coslat))
    for la in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
        if not -90 <= la < 90:
            continue
        for lo in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
            out.add((la, ((lo + 180) % 360) - 180))
    return out


class Planner:
    def __init__(self, catalogue, store, size_of, ring_model=None,
                 camera_reach_km=10.0):
        self.catalogue = catalogue
        self.store = store
        self.size_of = size_of
        self.rings = ring_model or rings.RingModel()
        self.camera_reach_km = float(camera_reach_km)
        self.lookahead_s = LOOKAHEAD_S   # a governor lever
        self.cruise = False
        self._cache_key = None
        self._cache = None

    def _candidates(self, squares):
        key = frozenset(squares)
        if key == self._cache_key and self._cache is not None:
            return self._cache
        parts = []
        for square in squares:
            for cat, idx in self.catalogue.textures_in(square):
                parts.append((cat, idx, square))
        regions, cats, indices, lats, lons, zooms, sizes, sqs = [], [], [], [], [], [], [], []
        seen = set()
        for cat, idx, square in parts:
            for i in idx:
                key2 = (cat.region, int(i))
                if key2 in seen:
                    continue
                seen.add(key2)
                cats.append(cat)
                indices.append(int(i))
                sqs.append(square)
        if cats:
            lats = np.fromiter((c.lats[i] for c, i in zip(cats, indices)), dtype=np.float64, count=len(cats))
            lons = np.fromiter((c.lons[i] for c, i in zip(cats, indices)), dtype=np.float64, count=len(cats))
            zooms = np.fromiter((c.zooms[i] for c, i in zip(cats, indices)), dtype=np.int32, count=len(cats))
            sizes = np.fromiter((c.size_m[i] for c, i in zip(cats, indices)), dtype=np.float64, count=len(cats))
        else:
            lats = lons = sizes = np.empty(0)
            zooms = np.empty(0, dtype=np.int32)
        top = np.fromiter((self.size_of(c, i)[0] for c, i in zip(cats, indices)), dtype=np.int32,
                          count=len(cats)) if cats else np.empty(0, dtype=np.int32)
        names = [c.name(i) for c, i in zip(cats, indices)]
        self._cache_key = key
        self._cache = (cats, indices, lats, lons, zooms, sizes, top, names, sqs)
        return self._cache

    def update_cruise(self, agl_m, vs_mps):
        agl_ft = agl_m / FT
        if self.cruise:
            descending_soon = (
                vs_mps < 0
                and (agl_ft - DESCENT_TARGET_FT) / max(1.0, -vs_mps / FT) <= DESCENT_LEAD_S
            )
            if agl_ft < CRUISE_OFF_FT or descending_soon:
                self.cruise = False
        elif agl_ft > CRUISE_ON_FT:
            self.cruise = True
        return self.cruise

    def plan(self, flight, window_squares, ring_squares, finest=0, route=None,
             starting=False, now=None):
        """Demands for one pass. ``window_squares``: what X-Plane holds now."""
        started = time.perf_counter()
        now = time.time() if now is None else now
        result = PlanResult()
        if not flight.valid:
            return result
        self.update_cruise(flight.agl_m, flight.vs_mps)
        result.cruise = self.cruise
        scale = rings.screen_scale(flight.width_px, flight.fov_deg)
        pts = path_points(flight.lat, flight.lon, flight.track_deg, flight.gs_mps,
                          lookahead_s=self.lookahead_s)
        # Polylines: the ground track, and the route ahead (from the aircraft).
        lines = [(0, len(pts))]
        if route:
            ahead = route_points(route, flight.lat, flight.lon, flight.track_deg, flight.gs_mps,
                                     lookahead_s=self.lookahead_s)
            if ahead:
                start = len(pts)
                pts = pts + [(flight.lat, flight.lon, 0.0)] + ahead
                lines.append((start, len(pts)))
        result.route_points = len(pts) - lines[0][1]
        max_ring = max(self.rings.ring_km(3, 16, None), 1.0) * 4.0 * scale * self.rings.margin
        squares = set(window_squares) | set(ring_squares)
        squares |= squares_within(flight.lat, flight.lon, min(CANDIDATE_KM, max_ring + 20))
        cats, indices, lats, lons, zooms, sizes, top, names, sqs = self._candidates(squares)
        n = len(cats)
        result.candidates = n
        if n == 0:
            result.duration_s = time.perf_counter() - started
            return result
        # Local equirectangular frame around the aircraft (fine below 300 km).
        coslat = math.cos(math.radians(flight.lat))
        def to_xy(la, lo):
            dlon = ((np.asarray(lo) - flight.lon + 540.0) % 360.0) - 180.0
            return dlon * KM_PER_DEG_LON_EQ * coslat, (np.asarray(la) - flight.lat) * KM_PER_DEG_LAT
        tx, ty = to_xy(lats, lons)
        px, py = to_xy([p[0] for p in pts], [p[1] for p in pts])
        pt = np.asarray([p[2] for p in pts], dtype=np.float64)
        # Distance to each path point, and to each segment.
        d_points = np.hypot(tx[:, None] - px[None, :], ty[:, None] - py[None, :])
        d_min = d_points.min(axis=1)
        seg_a = np.asarray([i for a, b in lines for i in range(a, b - 1)], dtype=np.int64)
        if len(seg_a):
            ax, ay = px[seg_a], py[seg_a]
            bx, by = px[seg_a + 1], py[seg_a + 1]
            vx, vy = bx - ax, by - ay
            seg_len2 = np.maximum(vx * vx + vy * vy, 1e-9)
            t = ((tx[:, None] - ax[None, :]) * vx[None, :] + (ty[:, None] - ay[None, :]) * vy[None, :]) / seg_len2[None, :]
            t = np.clip(t, 0.0, 1.0)
            cx = ax[None, :] + t * vx[None, :]
            cy = ay[None, :] + t * vy[None, :]
            d_seg = np.hypot(tx[:, None] - cx, ty[:, None] - cy).min(axis=1)
            d_min = np.minimum(d_min, d_seg)
        d_now = d_points[:, 0]
        agl_km = flight.agl_m / 1000.0
        d_now_3d = np.hypot(d_now, agl_km)
        # Camera: an external view near the ground needs levels there too.
        if flight.camera_lat is not None:
            cx_, cy_ = to_xy([flight.camera_lat], [flight.camera_lon])
            d_cam = np.hypot(tx - cx_[0], ty - cy_[0])
            d_cam = np.hypot(d_cam, (flight.camera_agl_m or 0.0) / 1000.0)
        else:
            d_cam = None
        # Ring radii per level, per texture (with margin and screen scale).
        zoom_f = np.power(2.0, 16.0 - zooms.astype(np.float64))
        radius = {}
        for level, base in self.rings.rings.items():
            r = base * zoom_f
            if self.rings.size_scaling:
                r = np.maximum(r, base * sizes / rings.ZL16_SIZE_M)
            radius[level] = r * scale
        margin = self.rings.margin
        reach = self.camera_reach_km
        need = np.full(n, rings.LOAD_LEVEL, dtype=np.int32)
        for level in sorted(self.rings.rings, reverse=True):  # 3, 2, 1, 0
            if level < finest:
                continue
            within = d_min <= radius[level] * margin + reach
            if d_cam is not None:
                within |= d_cam <= radius[level] * margin
            need = np.where(within, level, need)
        # 8192 px level 0: 3-D distance, no camera reach, never in cruise.
        big = top >= 8192
        if big.any():
            ok0 = np.hypot(d_min, agl_km) <= radius[0] * margin
            if d_cam is not None:
                ok0 |= d_cam <= radius[0] * margin
            if self.cruise:
                ok0[:] = False
            need = np.where(big & (need == 0) & ~ok0, 1, need)
        # Only squares X-Plane holds or loads next get the load level.
        window_set = set(window_squares)
        ring_set = set(ring_squares) | window_set
        demands = []
        late = 0
        speed = flight.gs_mps if flight.gs_mps >= STATIONARY_MPS else 0.0
        index_get = self.store.index.get
        for k in range(n):
            lvl = int(need[k])
            sq = sqs[k]
            if lvl >= rings.LOAD_LEVEL and sq not in ring_set:
                continue
            st = index_get(names[k])
            built = st.built_level if st is not None else NEUTRAL
            if st is not None and st.top_px != int(top[k]):
                built = NEUTRAL  # size changed (settings): rebuild
            if lvl >= built:
                continue
            # Urgency: time until the aircraft reaches the ring of ``lvl``.
            if lvl >= rings.LOAD_LEVEL:
                urgency = WINDOW_LOAD_URGENCY_S if sq in window_set else RING_LOAD_URGENCY_S
                if starting:
                    urgency = 0.0
            else:
                r = radius[lvl][k] * margin + reach
                if d_now[k] <= r:
                    urgency = 0.0
                elif speed > 0:
                    inside = np.nonzero(d_points[k] <= r)[0]
                    if len(inside):
                        urgency = float(pt[inside[0]])
                    else:
                        urgency = (float(d_min[k]) - r) * 1000.0 / speed + self.lookahead_s
                else:
                    urgency = (float(d_now[k]) - r) * 1000.0 / NOMINAL_MPS
                if d_cam is not None and d_cam[k] <= radius[lvl][k] * margin:
                    urgency = min(urgency, 0.0)
            if urgency <= 0:
                late += 1
            cat = cats[k]
            i = indices[k]
            demands.append(PlannedDemand(
                region=cat.region, index=i, name=names[k], row=int(cat.rows[i]),
                col=int(cat.cols[i]), maptype=cat.maptypes[int(cat.maptype_ids[i])],
                zoom=int(cat.zooms[i]), level=lvl, urgency_s=float(urgency),
                distance_km=float(d_now[k]), built_level=int(built), top_px=int(top[k]),
            ))
        result.demands = demands
        result.late = late
        result.duration_s = time.perf_counter() - started
        return result

#!/usr/bin/env python3
import logging
import atexit
import os
import time
import threading
import concurrent.futures
import uuid
import math
import tracemalloc
from typing import Optional

from io import BytesIO
from urllib.request import urlopen, Request
from queue import Queue, PriorityQueue, Empty
from functools import wraps, lru_cache
from pathlib import Path
from collections import OrderedDict

import pydds

import requests
import psutil
from aoimage import AoImage

from aoconfig import CFG
from aostats import STATS, StatTracker, StatsBatcher, get_stat, inc_many, inc_stat, set_stat, update_process_memory_stat, clear_process_memory_stat
from utils.constants import (
    system_type, 
    CURRENT_CPU_COUNT,
    EARTH_RADIUS_M,
    PRIORITY_DISTANCE_WEIGHT,
    PRIORITY_DIRECTION_WEIGHT,
    PRIORITY_MIPMAP_WEIGHT,
    LOOKAHEAD_TIME_SEC,
)
from utils.apple_token_service import apple_token_service

from datareftrack import dt as datareftracker

MEMTRACE = False

log = logging.getLogger(__name__)


# Bound global JPEG decode concurrency to reduce memory spikes and failures
try:
    _MAX_DECODE = int(getattr(CFG.pydds, 'max_decode_concurrency', CURRENT_CPU_COUNT))
except Exception:
    _MAX_DECODE = CURRENT_CPU_COUNT
_decode_sem = threading.Semaphore(_MAX_DECODE)


# Track average fetch times
tile_stats = StatTracker(20, 12)
mm_stats = StatTracker(0, 5)
partial_stats = StatTracker()

stats_batcher = None


def _ensure_stats_batcher():
    global stats_batcher
    if stats_batcher is None:
        try:
            # Create when a remote store is bound (either via env or parent bind)
            if getattr(STATS, "_remote", None) is not None or os.getenv("AO_STATS_ADDR"):
                stats_batcher = StatsBatcher(flush_interval=0.05, max_items=200)
                atexit.register(stats_batcher.stop)
        except Exception:
            stats_batcher = None


def bump(key, n=1):
    _ensure_stats_batcher()
    if stats_batcher:
        stats_batcher.add(key, n)
    else:
        inc_stat(key, n)


def bump_many(d: dict):
    _ensure_stats_batcher()
    if stats_batcher:
        stats_batcher.add_many(d)
    else:
        inc_many(d)


seasons_enabled = CFG.seasons.enabled

if seasons_enabled:
    from aoseasons import AoSeasonCache
    ao_seasons = AoSeasonCache(CFG.paths.cache_dir)

    # Per-DSF tile locks to serialize .si generation across threads
    _season_lock_map = {}
    _season_lock_map_lock = threading.Lock()

    def _season_tile_key_from_rc(row: int, col: int, zoom: int) -> str:
        # Compute DSF base name used by AoDsfSeason (e.g., +37-122)
        lon = col / pow(2, zoom) * 360 - 180
        n = math.pi - 2 * math.pi * row / pow(2, zoom)
        lat = 180 / math.pi * math.atan(0.5 * (math.exp(n) - math.exp(-n)))
        lat_i = math.floor(lat)
        lon_i = math.floor(lon)
        return f"{lat_i:+03d}{lon_i:+04d}"

    def _get_season_lock(key: str) -> threading.Lock:
        with _season_lock_map_lock:
            lock = _season_lock_map.get(key)
            if lock is None:
                lock = threading.Lock()
                _season_lock_map[key] = lock
            return lock

    def season_saturation_locked(row: int, col: int, zoom: int, day: Optional[int] = None) -> float:
        key = _season_tile_key_from_rc(row, col, zoom)
        lock = _get_season_lock(key)
        with lock:
            return ao_seasons.saturation(row, col, zoom, day)


def _is_jpeg(dataheader):
    # FFD8FF identifies image as a JPEG
    if dataheader[:3] == b'\xFF\xD8\xFF':
        return True
    else:
        return False


def _gtile_to_quadkey(til_x, til_y, zoomlevel):
    """
    Translates Google coding of tiles to Bing Quadkey coding. 
    """
    quadkey=""
    temp_x=til_x
    temp_y=til_y    
    for step in range(1,zoomlevel+1):
        size=2**(zoomlevel-step)
        a=temp_x//size
        b=temp_y//size
        temp_x=temp_x-a*size
        temp_y=temp_y-b*size
        quadkey=quadkey+str(a+2*b)
    return quadkey


def _chunk_to_latlon(row: int, col: int, zoom: int) -> tuple:
    """
    Convert tile row/col/zoom to center lat/lon coordinates.
    Returns (lat, lon) in degrees.
    
    Uses official OSM/Web Mercator formula:
    n = 2 ^ zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = arctan(sinh(π * (1 - 2 * ytile / n)))
    lat_deg = lat_rad * 180.0 / π
    
    Note: Adds 0.5 to row/col to get tile CENTER instead of top-left corner.
    """
    n = 2.0 ** zoom
    # Use tile center by adding 0.5 to both coordinates
    lon = (col + 0.5) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (row + 0.5) / n)))
    lat = math.degrees(lat_rad)
    return (lat, lon)


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate great-circle distance between two points in meters using Haversine formula.
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_M * c


def _calculate_spatial_priority(chunk_row: int, chunk_col: int, chunk_zoom: int, base_mipmap_priority: int) -> float:
    """
    Calculate priority based on:
    1. Distance from player
    2. Direction relative to movement vector (predictive)
    3. Base mipmap priority (detail level)
    
    Lower priority number = more urgent (fetched first).
    Returns priority as float.
    """
    # If no valid flight data, fall back to mipmap-only priority
    if not datareftracker.data_valid or not datareftracker.connected:
        return float(base_mipmap_priority)
    
    try:
        # Get player position and velocity
        player_lat = datareftracker.lat
        player_lon = datareftracker.lon
        player_hdg = datareftracker.hdg  # degrees, 0=N, 90=E, 180=S, 270=W
        player_spd = datareftracker.spd  # m/s

        chunk_lat, chunk_lon = _chunk_to_latlon(chunk_row, chunk_col, chunk_zoom)
        
        distance_m = _haversine_distance(player_lat, player_lon, chunk_lat, chunk_lon)
        
        distance_priority = min(100, distance_m / 100)  # 100m per priority point, max 100
        
        if player_spd > 5:  # Only use predictive if moving
            lat1_rad = math.radians(player_lat)
            lat2_rad = math.radians(chunk_lat)
            dlon_rad = math.radians(chunk_lon - player_lon)
            
            x = math.sin(dlon_rad) * math.cos(lat2_rad)
            y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
            bearing = math.degrees(math.atan2(x, y))
            bearing = (bearing + 360) % 360
            
            angle_diff = abs(player_hdg - bearing)
            if angle_diff > 180:
                angle_diff = 360 - angle_diff
            

            direction_priority = (angle_diff / 180) * 100 - 50
            
            predicted_distance = player_spd * LOOKAHEAD_TIME_SEC
            if distance_m < predicted_distance and angle_diff < 45:
                direction_priority -= 30
        else:
            direction_priority = 0
        

        total_priority = (
            base_mipmap_priority * PRIORITY_MIPMAP_WEIGHT +
            distance_priority * PRIORITY_DISTANCE_WEIGHT +
            direction_priority * PRIORITY_DIRECTION_WEIGHT
        )
        
        return max(0, total_priority)
        
    except Exception as e:
        # On any error, fall back to mipmap-only priority
        log.debug(f"Spatial priority calculation failed: {e}")
        return float(base_mipmap_priority)


def locked(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        #result = fn(self, *args, **kwargs)
        with self._lock:
            result = fn(self, *args, **kwargs)
        return result
    return wrapped


class Getter(object):
    queue = None
    workers = None
    WORKING = None
    session = None

    def __init__(self, num_workers):
        
        self.count = 0
        self.queue = PriorityQueue()
        self.workers = []
        self.WORKING = threading.Event()
        self.WORKING.set()
        self.localdata = threading.local()
        # Thread-local sessions created in worker() to avoid shared-state contention

        for i in range(num_workers):
            t = threading.Thread(target=self.worker, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)

        #self.stat_t = t = threading.Thread(target=self.show_stats, daemon=True)
        #self.stat_t.start()


    def stop(self):
        self.WORKING.clear()
        for t in self.workers:
            t.join()
        # If a stats thread was started, join it as well
        stat_thread = getattr(self, 'stat_t', None)
        if stat_thread is not None:
            stat_thread.join()

    def worker(self, idx):
        global STATS
        self.localdata.idx = idx
        
        # Create thread-local session with optimized pool and NO retries
        # (Circuit breaker handles retries, not session)
        try:
            session = requests.Session()
            # Larger pool to avoid connection stalls
            pool_size = max(4, int(int(CFG.autoortho.fetch_threads) * 1.5))
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=pool_size,
                pool_maxsize=pool_size,
                max_retries=0,  # Circuit breaker handles retries
                pool_block=True,
            )
            session.mount('https://', adapter)
            session.mount('http://', adapter)
            self.localdata.session = session
        except Exception as _e:
            log.warning(f"Failed to initialize thread-local session: {_e}")
            self.localdata.session = requests.Session()
        
        while self.WORKING.is_set():
            try:
                obj, args, kwargs = self.queue.get(timeout=5)
                #log.debug(f"Got: {obj} {args} {kwargs}")
            except Empty:
                #log.debug(f"timeout, continue")
                #log.info(f"Got {self.counter}")
                continue

            #STATS.setdefault('count', 0) + 1
            bump('count', 1)

            try:
                # Mark object as in-flight
                try:
                    if hasattr(obj, 'in_queue'):
                        obj.in_queue = False
                    if hasattr(obj, 'in_flight'):
                        obj.in_flight = True
                except Exception:
                    pass
                
                if not self.get(obj, *args, **kwargs):
                    # Check if chunk is permanently failed before re-submitting
                    if hasattr(obj, 'permanent_failure') and obj.permanent_failure:
                        log.debug(f"Chunk {obj} permanently failed ({obj.failure_reason}), not re-submitting")
                        continue
                    log.warning(f"Failed getting: {obj} {args} {kwargs}, re-submit.")
                    self.submit(obj, *args, **kwargs)
            except Exception as err:
                log.error(f"ERROR {err} getting: {obj} {args} {kwargs}, re-submit.")
                # Don't re-submit if permanently failed
                if hasattr(obj, 'permanent_failure') and obj.permanent_failure:
                    log.debug(f"Chunk {obj} permanently failed during exception, not re-submitting")
                    continue
                self.submit(obj, *args, **kwargs)
            finally:
                try:
                    if hasattr(obj, 'in_flight'):
                        obj.in_flight = False
                except Exception:
                    pass

    def get(obj, *args, **kwargs):
        raise NotImplementedError

    def submit(self, obj, *args, **kwargs):
        # Don't queue permanently failed chunks
        if hasattr(obj, 'permanent_failure') and obj.permanent_failure:
            return
        # Coalesce duplicate chunk submissions
        try:
            if hasattr(obj, 'ready') and obj.ready.is_set():
                return  # Already done
            if hasattr(obj, 'in_queue') and obj.in_queue:
                return  # Already queued
            if hasattr(obj, 'in_flight') and obj.in_flight:
                return  # Currently downloading
            if hasattr(obj, 'in_queue'):
                obj.in_queue = True
        except Exception:
            pass
        self.queue.put((obj, args, kwargs))

    def show_stats(self):
        while self.WORKING.is_set():
            log.info(f"{self.__class__.__name__} got: {self.count}")
            time.sleep(10)
        log.info(f"Exiting {self.__class__.__name__} stat thread.  Got: {self.count} total")


class ChunkGetter(Getter):
    def get(self, obj, *args, **kwargs):
        if obj.ready.is_set():
            log.info(f"{obj} already retrieved.  Exit")
            return True

        kwargs['idx'] = self.localdata.idx
        # Use thread-local session
        kwargs['session'] = getattr(self.localdata, 'session', None) or requests
        #log.debug(f"{obj}, {args}, {kwargs}")
        return obj.get(*args, **kwargs)

chunk_getter = ChunkGetter(int(CFG.autoortho.fetch_threads))

#class TileGetter(Getter):
#    def get(self, obj, *args, **kwargs):
#        log.debug(f"{obj}, {args}, {kwargs}")
#        return obj.get(*args)
#
#tile_getter = TileGetter(8)

log.info(f"chunk_getter: {chunk_getter}")
#log.info(f"tile_getter: {tile_getter}")

# HTTP status codes that indicate permanent failure (no retry)
PERMANENT_FAILURE_CODES = {400, 401, 404, 405, 406, 410, 451}

# HTTP status codes that need special handling
TRANSIENT_FAILURE_CODES = {408, 429, 500, 502, 503, 504}

# Max retries for transient failures before giving up
MAX_TRANSIENT_RETRIES = {
    408: 3,   # Request timeout - network issue
    429: 10,  # Rate limit - needs backoff
    500: 15,  # Internal error - might recover
    502: 8,   # Bad gateway - infrastructure
    503: 8,   # Service unavailable - infrastructure  
    504: 5,   # Gateway timeout - infrastructure
}


class Chunk(object):
    col = -1
    row = -1
    source = None
    chunk_id = ""
    priority = 0
    width = 256
    height = 256
    cache_dir = 'cache'
    
    attempt = 0

    starttime = 0
    fetchtime = 0

    ready = None
    data = None
    img = None
    url = None

    serverlist=['a','b','c','d']

    def __init__(self, col, row, maptype, zoom, priority=0, cache_dir='.cache'):
        self.col = col
        self.row = row
        self.zoom = zoom
        self.maptype = maptype
        self.cache_dir = cache_dir
        
        # Hack override maptype
        #self.maptype = "BI"

        # Set priority: use provided value or default to zoom level
        self.priority = priority if priority else zoom
        self.chunk_id = f"{col}_{row}_{zoom}_{maptype}"
        self.ready = threading.Event()
        self.ready.clear()
        self.download_started = threading.Event()  # Signal when download thread begins
        self.download_started.clear()
        if maptype == "Null":
            self.maptype = "EOX"

        # Failure tracking for circuit breaker
        self.permanent_failure = False
        self.failure_reason = None
        self.retry_count = 0

        # Coalescing flags to prevent duplicate submissions
        self.in_queue = False
        self.in_flight = False

        self.cache_path = os.path.join(self.cache_dir, f"{self.chunk_id}.jpg")
        
        # FIXED: Check cache during initialization and set ready if found
        if self.get_cache():
            self.download_started.set()  # Cache hit = "download" done immediately
            self.ready.set()
            log.debug(f"Chunk {self} initialized with cached data")

    def __lt__(self, other):
        return self.priority < other.priority

    def __repr__(self):
        return f"Chunk({self.col},{self.row},{self.maptype},{self.zoom},{self.priority})"

    def get_cache(self):
        if os.path.isfile(self.cache_path):
            bump('chunk_hit')
            cache_file = Path(self.cache_path)
            # Get data
            data = None
            # On Windows, the cache file can be briefly locked by AV or a concurrent writer.
            # Add a short retry/backoff loop to avoid spurious PermissionError / sharing violations.
            max_attempts = 5
            for attempt in range(1, max_attempts + 1):
                try:
                    data = cache_file.read_bytes()
                    break
                except PermissionError as e:
                    if attempt < max_attempts:
                        time.sleep(0.02 * attempt)
                        continue
                    log.warning(f"Permission denied reading cache {self}: {e}")
                    return False
                except FileNotFoundError:
                    # Raced with a concurrent replace/remove; treat as miss
                    return False
                except OSError as e:
                    winerr = getattr(e, 'winerror', None)
                    if winerr in (5, 32, 33) and attempt < max_attempts:
                        time.sleep(0.02 * attempt)
                        continue
                    log.debug(f"OSError reading cache {self}: {e}")
                    return False

            cache_file.touch()
            # Update modified data
            try:
                os.utime(self.cache_path, None)
            except (FileNotFoundError, PermissionError):
                pass 

            if _is_jpeg(data[:3]):
                #print(f"Found cache that is JPEG for {self}")
                self.data = data
                return True
            else:
                log.info(f"Loading file {self} not a JPEG! {data[:3]} path: {self.cache_path}")
                self.data = b''
                return False  # FIXED: Explicitly return False for corrupted cache
        else:
            bump('chunk_miss')
            return False

    def save_cache(self):
        # Snapshot data to avoid races with close() mutating self.data
        data = self.data
        if not data:
            return

        # Ensure cache directory exists
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except Exception:
            pass

        # Unique temp filename per writer to avoid collisions between threads/tiles
        temp_filename = os.path.join(self.cache_dir, f"{self.chunk_id}_{uuid.uuid4().hex}.tmp")

        # Write data to the unique temp file first
        try:
            with open(temp_filename, 'wb') as h:
                h.write(data)
        except Exception as e:
            # Could not write temp file
            try:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
            except Exception:
                pass
            log.warning(f"Failed to save cache for {self}: {e}")
            return

        # Try to move into place atomically with a few retries for WinError 32
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if os.path.exists(self.cache_path):
                    # Another writer got there first; clean up our temp
                    os.remove(temp_filename)
                    log.debug(f"Cache file already exists for {self}, skipping save (race) on attempt {attempt}")
                    return
                os.replace(temp_filename, self.cache_path)
                return
            except FileExistsError:
                try:
                    os.remove(temp_filename)
                except Exception:
                    pass
                log.debug(f"Another thread saved cache for {self}, removed temp file")
                return
            except OSError as e:
                if getattr(e, 'winerror', None) in (5, 32, 33) and attempt < max_attempts:
                    time.sleep(0.05 * attempt)
                    if os.path.exists(self.cache_path):
                        try:
                            if os.path.exists(temp_filename):
                                os.remove(temp_filename)
                        except Exception:
                            pass
                        return
                    continue
                try:
                    if os.path.exists(temp_filename):
                        os.remove(temp_filename)
                except Exception:
                    pass
                log.warning(f"Failed to save cache for {self}: {e}")
                return

    def get(self, idx=0, session=requests):
        log.debug(f"Getting {self}")
        
        # Signal that download has started (not waiting in queue anymore)
        self.download_started.set()

        if self.get_cache():
            self.ready.set()
            return True

        if not self.starttime:
            self.starttime = time.time()

        server_num = idx % (len(self.serverlist))
        server = self.serverlist[server_num]
        quadkey = _gtile_to_quadkey(self.col, self.row, self.zoom)

        # Hack override maptype
        #maptype = "ARC"

        MAPID = "s2cloudless-2023_3857"
        MATRIXSET = "g"
        MAPTYPES = {
            "EOX": f"https://{server}.tiles.maps.eox.at/wmts/?layer={MAPID}&style=default&tilematrixset={MATRIXSET}&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image%2Fjpeg&TileMatrix={self.zoom}&TileCol={self.col}&TileRow={self.row}",
            "BI": f"https://t.ssl.ak.tiles.virtualearth.net/tiles/a{quadkey}.jpeg?g=15312",
            "GO2": f"http://mts{server_num}.google.com/vt/lyrs=s&x={self.col}&y={self.row}&z={self.zoom}",
            "ARC": f"http://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "NAIP": f"http://naip.maptiles.arcgis.com/arcgis/rest/services/NAIP/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "USGS": f"https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "FIREFLY": f"https://fly.maptiles.arcgis.com/arcgis/rest/services/World_Imagery_Firefly/MapServer/tile/{self.zoom}/{self.row}/{self.col}",
            "YNDX": f"https://sat{server_num+1:02d}.maps.yandex.net/tiles?l=sat&v=3.1814.0&x={self.col}&y={self.row}&z={self.zoom}",
            "APPLE": f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v={apple_token_service.version}&accessKey={apple_token_service.apple_token}"
        }

        MAPTYPES_WITH_SERVER = ["YNDX", "EOX", "GO2"]

        self.url = MAPTYPES[self.maptype.upper()]
        #log.debug(f"{self} getting {url}")
        header = {
                "user-agent": "curl/7.68.0"
        }
        if self.maptype.upper() == "EOX":
            log.debug("EOX DETECTED")
            header.update({'referer': 'https://s2maps.eu/'})
       
        time.sleep((self.attempt/10))
        self.attempt += 1

        log.debug(f"Requesting {self.url} ..")
        
        resp = None
        try:

            resp = session.get(self.url, headers=header, timeout=(5, 20))
            status_code = resp.status_code

            if self.maptype.upper() == "APPLE" and status_code in (403, 410):
                log.warning("APPLE tile got %s; rotating token and retrying", status_code)
                apple_token_service.reset_apple_maps_token()
                MAPTYPES["APPLE"] = f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v={apple_token_service.version}&accessKey={apple_token_service.apple_token}"
                self.url = MAPTYPES[self.maptype.upper()]
                if resp is not None:
                    resp.close()
                resp = session.get(self.url, headers=header, timeout=(5, 20))
                status_code = resp.status_code

            if status_code != 200:
                log.warning(f"Failed with status {status_code} to get chunk {self}" + (" on server " + server if self.maptype.upper() in MAPTYPES_WITH_SERVER else "") + ".")
                bump_many({f"http_{status_code}": 1, "req_err": 1})
                
                # Check if this is a permanent failure
                if status_code in PERMANENT_FAILURE_CODES:
                    log.info(f"Chunk {self} permanently failed with {status_code}, marking as failed")
                    self.permanent_failure = True
                    self.failure_reason = status_code
                    self.data = b''  # Empty data
                    self.ready.set()  # Mark as ready (with no data) to unblock waiters
                    bump(f'chunk_permanent_fail_{status_code}')
                    return True  # Return True to stop worker retries
                
                # Check if transient failure has exceeded max retries
                if status_code in TRANSIENT_FAILURE_CODES:
                    self.retry_count += 1
                    max_retries = MAX_TRANSIENT_RETRIES.get(status_code, 5)
                    if self.retry_count >= max_retries:
                        log.warning(f"Chunk {self} exceeded {max_retries} retries for {status_code}, giving up")
                        self.permanent_failure = True
                        self.failure_reason = f"{status_code}_max_retries"
                        self.data = b''
                        self.ready.set()
                        bump(f'chunk_transient_fail_{status_code}_exhausted')
                        return True
                    # Increase backoff for rate limiting
                    if status_code == 429:
                        backoff_time = min(5, self.retry_count * 0.5)
                        log.debug(f"Rate limited, backing off for {backoff_time}s (attempt {self.retry_count}/{max_retries})")
                        time.sleep(backoff_time)
                    bump(f'chunk_transient_fail_{status_code}_retry')

                err = get_stat("req_err")
                if err > 50:
                    ok = get_stat("req_ok")
                    error_rate = err / ( err + ok )
                    if error_rate >= 0.10:
                        log.error(f"Very high network error rate detected : {error_rate * 100 : .2f}%")
                        log.error(f"Check your network connection, DNS, maptype choice, and firewall settings.")
                        # Enhanced circuit breaker: reduce wait times on severe error rates
                        if error_rate >= 0.25:
                            log.warning("Severe error rate (≥25%) detected, consider checking configuration")
                return False

            bump("req_ok")

            data = resp.content

            if _is_jpeg(data[:3]):
                log.debug(f"Data for {self} is JPEG")
                self.data = data
            else:
                # FFD8FF identifies image as a JPEG
                log.debug(f"Loading file {self} not a JPEG! {data[:3]} URL: {self.url}")
            #    return False
                self.data = b''

            bump('bytes_dl', len(self.data))
                
        except Exception as err:
            log.warning(f"Failed to get chunk {self} on server {server}. Err: {err} URL: {self.url}")
            return False
        finally:
            if resp:
                resp.close()

        self.fetchtime = time.monotonic() - self.starttime

        self.save_cache()
        self.ready.set()
        return True

    def close(self):
        """Release all references held by this Chunk so its memory can be reclaimed."""

        # Release image buffer if we created one
        if hasattr(self, 'img') and self.img is not None:
            try:
                # AoImage instances have a close() that frees underlying C memory
                if hasattr(self.img, "close"):
                    self.img.close()
            finally:
                self.img = None

        # Remove raw JPEG bytes
        self.data = None


class Tile(object):
    row = -1
    col = -1
    maptype = None
    zoom = -1
    min_zoom = 12
    width = 16
    height = 16
    baseline_zl = 12

    priority = -1
    #tile_condition = None
    _lock = None
    ready = None 

    chunks = None
    cache_file = None
    dds = None

    refs = None

    maxchunk_wait = float(CFG.autoortho.maxwait)
    imgs = None

    def __init__(self, col, row, maptype, zoom, min_zoom=0, priority=0,
            cache_dir=None, max_zoom=None):
        self.row = int(row)
        self.col = int(col)
        self.maptype = maptype
        self.tilename_zoom = int(zoom)
        self.chunks = {}
        self.cache_file = (-1, None)
        self.ready = threading.Event()
        self._lock = threading.RLock()
        self.refs = 0
        self.imgs = {}

        self.bytes_read = 0
        self.lowest_offset = 99999999


        #self.tile_condition = threading.Condition()
        if min_zoom:
            self.min_zoom = int(min_zoom)


        if cache_dir:
            self.cache_dir = cache_dir
        else:
            self.cache_dir = CFG.paths.cache_dir


        # Set max zoom level - if not specified, use original tile zoom (no capping)
        self.max_zoom = int(max_zoom) if max_zoom is not None else self.tilename_zoom
        # Hack override maptype
        #self.maptype = "BI"

        #self._find_cached_tiles()
        self.ready.clear()
        
        #self._find_cache_file()

        if not priority:
            self.priority = zoom

        if not os.path.isdir(self.cache_dir):
            os.makedirs(self.cache_dir)

        if CFG.pydds.compressor.upper() == "ISPC":
            use_ispc=True
        else:
            use_ispc=False

        # TODO VRAM usage optimization only getting the max ZL of the zone instead of the default max zoom
        # This is very time consuming while loading flight, left commented out for now
        # self.actual_max_zoom = self._detect_available_max_zoom()
        self.tilezoom_diff = self.tilename_zoom - self.max_zoom

        if self.tilezoom_diff >= 0:

            self.chunks_per_row = self.width >> self.tilezoom_diff
            self.chunks_per_col = self.height >> self.tilezoom_diff
        else:
            self.chunks_per_row = self.width << (-self.tilezoom_diff)
            self.chunks_per_col = self.height << (-self.tilezoom_diff)


        if self.tilezoom_diff < 0:
            if self.tilezoom_diff < -1:
                raise ValueError(f"Tilezoom_diff is {self.tilezoom_diff} which is less than -1, which is not supported by X-Plane.")
            self.max_mipmap = 5 # Enforce a maximum of 5 mipmaps (8192 -> 256 max)
        else:
            self.max_mipmap = 4 # Enforce a maximum of 4 mipmaps (4096 -> 256 max)

        self.chunks_per_row = max(1, self.chunks_per_row)
        self.chunks_per_col = max(1, self.chunks_per_col)

        dds_width = self.chunks_per_row * 256
        dds_height = self.chunks_per_col * 256
        log.debug(f"Creating DDS at original size: {dds_width}x{dds_height} (ZL{self.max_zoom})")
            
        self.dds = pydds.DDS(dds_width, dds_height, ispc=use_ispc,
                dxt_format=CFG.pydds.format)

        self.id = f"{row}_{col}_{maptype}_{self.tilename_zoom}"


    def __lt__(self, other):
        return self.priority < other.priority

    def __repr__(self):
        return f"Tile({self.col}, {self.row}, {self.maptype}, {self.tilename_zoom}, min_zoom={self.min_zoom}, max_zoom={self.max_zoom}, max_mm={self.max_mipmap})"

    @locked
    def _create_chunks(self, quick_zoom=0, min_zoom=None):
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(quick_zoom, min_zoom)

        if not self.chunks.get(zoom):
            self.chunks[zoom] = []
            log.debug(f"Creating chunks for zoom {zoom}: {width}x{height} grid starting at ({col},{row})")

            for r in range(row, row+height):
                for c in range(col, col+width):
                    # FIXED: Create chunk and let it check cache during initialization
                    chunk = Chunk(c, r, self.maptype, zoom, cache_dir=self.cache_dir)
                    self.chunks[zoom].append(chunk)
        else:
            log.debug(f"Reusing existing {len(self.chunks[zoom])} chunks for zoom {zoom}")

    def _find_cache_file(self):
        #with self.tile_condition:
        with self.tile_lock:
            for z in range(self.max_zoom, (self.min_zoom-1), -1):
                cache_file = os.path.join(self.cache_dir, f"{self.row}_{self.col}_{self.maptype}_{self.tilename_zoom}_{z}.dds")
                if os.path.exists(cache_file):
                    log.info(f"Found cache for {cache_file}...")
                    self.cache_file = (z, cache_file)
                    self.ready.set()
                    return

        #log.info(f"No cache found for {self}!")


    def _get_quick_zoom(self, quick_zoom=0, min_zoom=None):
        """Calculate tile parameters for the given zoom level.
        
        Args:
            quick_zoom: Target zoom level (0 means use max_zoom)
            min_zoom: Minimum allowed zoom level
            
        Returns:
            Tuple of (col, row, width, height, zoom, zoom_diff)
        """
        # Handle simple case: no quick zoom specified
        if not quick_zoom:
            return (self.col, self.row, self.width, self.height, self.max_zoom, 0)
        
        quick_zoom = int(quick_zoom)
        
        # Calculate the maximum zoom difference this tile can support
        max_supported_diff = min(self.max_zoom - quick_zoom, self.max_mipmap)
        
        # Determine minimum zoom level if not provided
        if min_zoom is None:
            min_zoom = max(self.max_zoom - max_supported_diff, self.min_zoom)
        
        # Clamp quick_zoom to minimum allowed value
        effective_zoom = max(quick_zoom, min_zoom)
        
        # Calculate actual zoom difference (limited by max_mipmap)
        zoom_diff = min(self.max_zoom - effective_zoom, self.max_mipmap)

        # Calculate tilename zoom difference
        tilename_zoom_diff = self.tilename_zoom - effective_zoom
        
        # Scale coordinates and dimensions based on zoom difference
        def scale_by_zoom_diff(value, diff):
            """Scale a value by 2^diff (positive diff scales down, negative scales up)"""
            if diff >= 0:
                return value >> diff  
            else:
                return value << (-diff)  
        
        scaled_col = scale_by_zoom_diff(self.col, tilename_zoom_diff)
        scaled_row = scale_by_zoom_diff(self.row, tilename_zoom_diff)
        scaled_width = max(1, scale_by_zoom_diff(self.width, tilename_zoom_diff))
        scaled_height = max(1, scale_by_zoom_diff(self.height, tilename_zoom_diff))
        
        return (scaled_col, scaled_row, scaled_width, scaled_height, effective_zoom, zoom_diff)


    def fetch(self, quick_zoom=0, background=False):
        self._create_chunks(quick_zoom)
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(quick_zoom)

        for chunk in self.chunks[zoom]:
            chunk_getter.submit(chunk)

        for chunk in self.chunks[zoom]:
            ret = chunk.ready.wait()
            if not ret:
                log.error("Failed to get chunk.")

        return True
   
    def find_mipmap_pos(self, offset):
        for m in self.dds.mipmap_list:
            if offset < m.endpos:
                return m.idx
        return self.dds.mipmap_list[-1].idx

    def get_bytes(self, offset, length):
        
        # Guard against races where tile is being closed and DDS is cleared
        if self.dds is None:
            log.debug(f"GET_BYTES: DDS is None for {self}, likely closing; skipping")
            return True

        mipmap = self.find_mipmap_pos(offset)
        log.debug(f"Get_bytes for mipmap {mipmap} ...")
        if mipmap > self.max_mipmap:
            # Just get the entire mipmap
            self.get_mipmap(self.max_mipmap)
            return True

        # Exit if already retrieved
        if self.dds.mipmap_list[mipmap].retrieved:
            log.debug(f"We already have mipmap {mipmap} for {self}")
            return True

        mm = self.dds.mipmap_list[mipmap]
        if length >= mm.length:
            self.get_mipmap(mipmap)
            return True
        
        log.debug(f"Retrieving {length} bytes from mipmap {mipmap} offset {offset}")

        # how deep are we in a mipmap
        mm_offset = max(0, offset - self.dds.mipmap_list[mipmap].startpos)
        log.debug(f"MM_offset: {mm_offset}  Offset {offset}.  Startpos {self.dds.mipmap_list[mipmap]}")

        # Dynamically compute bytes-per-chunk-row for this mip level based on actual DDS dimensions
        base_width_px = max(4, int(self.dds.width) >> mipmap)
        base_height_px = max(4, int(self.dds.height) >> mipmap)
        blocksize = 8 if CFG.pydds.format == "BC1" else 16
        blocks_per_row = max(1, base_width_px // 4)
        bytes_per_row = blocks_per_row * blocksize
        # Each chunk-row is 256 px tall -> 64 blocks vertically
        bytes_per_chunk_row = bytes_per_row * 64

        # Compute start/end chunk-rows touched by the requested byte range
        startrow = mm_offset // bytes_per_chunk_row
        endrow = (mm_offset + max(0, length - 1)) // bytes_per_chunk_row

        # Clamp to valid range of chunk rows for this mipmap
        chunk_rows_in_mm = base_height_px // 256
        if chunk_rows_in_mm == 0:
            log.error(f"Chunk rows in mipmap {mipmap} is 0!  Base height: {base_height_px}  Mipmap: {mipmap}")

        if startrow >= chunk_rows_in_mm:
            startrow = chunk_rows_in_mm - 1
        if endrow >= chunk_rows_in_mm:
            endrow = chunk_rows_in_mm - 1
        if endrow < startrow:
            endrow = startrow

        # Prefetch one extra chunk-row ahead to reduce subsequent stalls
        if endrow < (chunk_rows_in_mm - 1):
            endrow = min(endrow + 1, chunk_rows_in_mm - 1)

        log.debug(f"Startrow: {startrow} Endrow: {endrow} bytes_per_chunk_row: {bytes_per_chunk_row} width_px: {base_width_px} height_px: {base_height_px}")
        
        new_im = self.get_img(mipmap, startrow, endrow,
                maxwait=self.get_maxwait())
        if not new_im:
            log.debug("No updates, so no image generated")
            return True

        # If tile is being closed concurrently, avoid touching DDS
        if self.dds is None:
            return True
        self.ready.clear()
        #log.info(new_im.size)
        
        start_time = time.time()

        # Only attempt partial compression from mipmap start
        if offset == 0:
            #compress_len = length
            compress_len = length - 128
        else:
            compress_len = 0

        try:
            self.dds.gen_mipmaps(new_im, mipmap, mipmap, compress_len)
        finally:
            pass
            # We may have retrieved a full image that could be saved for later
            # usage.  Don't close here.
            #new_im.close()

        # We haven't fully retrieved so unset flag; guard against DDS being cleared
        log.debug(f"UNSETTING RETRIEVED! {self}")
        try:
            if self.dds is not None and self.dds.mipmap_list:
                self.dds.mipmap_list[mipmap].retrieved = False
        except Exception:
            pass
        end_time = time.time()
        self.ready.set()

        if compress_len:
            tile_time = end_time - start_time
            partial_stats.set(mipmap, tile_time)
            # Record partial mm stats via counters for aggregation
            try:
                bump_many({
                    f"partial_mm_count:{mipmap}": 1,
                    f"partial_mm_time_total_ms:{mipmap}": int(tile_time * 1000)
                })
            except Exception:
                pass

        return True

    def read_dds_bytes(self, offset, length):
        log.debug(f"READ DDS BYTES: {offset} {length}")
       
        if offset > 0 and offset < self.lowest_offset:
            self.lowest_offset = offset

        mm_idx = self.find_mipmap_pos(offset)
        mipmap = self.dds.mipmap_list[mm_idx]

        if offset == 0:
            # If offset = 0, read the header
            log.debug("READ_DDS_BYTES: Read header")
            self.get_bytes(0, length)
        else:
            # Dynamically scale the early-read heuristic based on actual mip-0 bytes per chunk-row
            blocksize = 8 if CFG.pydds.format == "BC1" else 16
            width_px_m0 = max(4, int(self.dds.width))
            blocks_per_row_m0 = max(1, width_px_m0 // 4)
            bytes_per_row_m0 = blocks_per_row_m0 * blocksize
            bytes_per_chunk_row_m0 = bytes_per_row_m0 * 64

            # If we're still within the first chunk-row of mipmap 0, just fetch from the start
            early_threshold = bytes_per_chunk_row_m0
            if mm_idx == 0 and offset < early_threshold:
                log.debug("READ_DDS_BYTES: Early region of mipmap 0 - fetching from start")
                self.get_bytes(0, length + offset)
            elif (offset + length) < mipmap.endpos:
                # Total length is within this mipmap.  Make sure we have it.
                log.debug(f"READ_DDS_BYTES: Detected middle read for mipmap {mipmap.idx}")
                if not mipmap.retrieved:
                    log.debug(f"READ_DDS_BYTES: Retrieve {mipmap.idx}")
                    self.get_mipmap(mipmap.idx)
            else:
                log.debug(f"READ_DDS_BYTES: Start before this mipmap {mipmap.idx}")
                # We already know we start before the end of this mipmap
                # We must extend beyond the length.
                
                # Get bytes prior to this mipmap
                self.get_bytes(offset, length)

                # Get the entire next mipmap
                self.get_mipmap(mm_idx + 1)
        
        self.bytes_read += length
        # Seek and return data
        self.dds.seek(offset)
        return self.dds.read(length)

    def write(self):
        outfile = os.path.join(self.cache_dir, f"{self.row}_{self.col}_{self.maptype}_{self.tilename_zoom}_{self.tilename_zoom}.dds")
        self.ready.clear()
        self.dds.write(outfile)
        self.ready.set()
        return outfile

    def get_header(self):
        outfile = os.path.join(self.cache_dir, f"{self.row}_{self.col}_{self.maptype}_{self.tilename_zoom}_{self.tilename_zoom}.dds")
        
        self.ready.clear()
        self.dds.write(outfile)
        self.ready.set()
        return outfile

    @locked
    def get_img(self, mipmap, startrow=0, endrow=None, maxwait=5, min_zoom=None):
        #
        # Get an image for a particular mipmap
        #

        # CRITICAL: If building mipmap 0, ensure lower mipmaps (1-4) are fully built first
        # This ensures they're available in self.imgs for fallback upscaling
        if mipmap == 0 and (startrow == 0 and endrow is None):
            log.debug(f"GET_IMG: Building mipmap 0, ensuring lower mipmaps are available for fallback")
            for lower_mm in range(1, min(self.max_mipmap + 1, 5)):
                if lower_mm not in self.imgs:
                    log.debug(f"GET_IMG: Pre-building mipmap {lower_mm} for fallback support")
                    try:
                        # Build the complete lower mipmap
                        self.get_img(lower_mm, startrow=0, endrow=None, maxwait=maxwait, min_zoom=min_zoom)
                    except Exception as e:
                        log.debug(f"GET_IMG: Failed to pre-build mipmap {lower_mm}: {e}")

        # Get effective zoom  
        zoom = min((self.max_zoom - mipmap), self.max_zoom)
        log.debug(f"GET_IMG: Default tile zoom: {self.tilename_zoom}, Requested Mipmap: {mipmap}, Requested mipmap zoom: {zoom}")
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(zoom, min_zoom)
        log.debug(f"Will use:  Zoom: {zoom},  Zoom_diff: {zoom_diff}")        
        
        log.debug(f"GET_IMG: Final zoom {zoom} for mipmap {mipmap}, coords: {col}x{row}, size: {width}x{height}")
        
        # Do we already have this img?
        if mipmap in self.imgs:
            img_data = self.imgs[mipmap]
            # Unpack tuple format (new) or return directly (old format, backward compat)
            if isinstance(img_data, tuple):
                img = img_data[0]  # Extract just the image
                log.debug(f"GET_IMG: Found saved image: {img}")
                return img
            else:
                # Old format: just the image without metadata
                log.debug(f"GET_IMG: Found saved image (old format): {img_data}")
                return img_data

        log.debug(f"GET_IMG: MM List before { {x.idx:x.retrieved for x in self.dds.mipmap_list} }")
        if mipmap < len(self.dds.mipmap_list) and self.dds.mipmap_list[mipmap].retrieved:
            log.debug(f"GET_IMG: We already have mipmap {mipmap} for {self}")
            return

        if startrow == 0 and endrow is None:
            complete_img = True
        else:
            complete_img = False

        startchunk = 0
        endchunk = None
        # Determine start and end chunk based on the actual zoom level we're using
        # Use the width/height calculated for the capped zoom level
        chunks_per_row = width  # width already accounts for capping
        if startrow:
            startchunk = startrow * chunks_per_row
        if endrow is not None:
            endchunk = (endrow * chunks_per_row) + chunks_per_row
            
        log.debug(f"GET_IMG: Chunk indices - start: {startchunk}, end: {endchunk}, chunks_per_row: {chunks_per_row}")

        # Create chunks for the actual zoom level we'll download from
        self._create_chunks(zoom, min_zoom)
        chunks = self.chunks[zoom][startchunk:endchunk]
        log.debug(f"Start chunk: {startchunk}  End chunk: {endchunk}  Chunklen {len(self.chunks[zoom])} for zoom {zoom}")

        log.debug(f"GET_IMG: {self} : Retrieve mipmap for ZOOM: {zoom} MIPMAP: {mipmap}")
        data_updated = False
        log.debug(f"GET_IMG: {self} submitting chunks for zoom {zoom}.")
        for chunk in chunks:
            if not chunk.ready.is_set():
                log.debug(f"GET_IMG: Submitting chunk {chunk} for zoom {zoom}")
                # INVERTED: Lower detail (higher mipmap) = lower priority number (more urgent)
                # This ensures lower-detail tiles load first, minimizing missing tiles
                base_priority = self.max_mipmap - mipmap
                
                # During initial load (before flight starts), further deprioritize high-detail
                # to ensure lower mipmaps load completely first
                if CFG.autoortho.suspend_maxwait and not datareftracker.connected:
                    # Add penalty to high-detail mipmaps during initial load
                    # Mipmap 0 gets +20, mipmap 4 gets +0
                    initial_load_penalty = (self.max_mipmap - mipmap) * 5
                    base_priority = base_priority + initial_load_penalty
                
                # Apply spatial + predictive priority system
                # This considers distance from player and movement direction
                chunk.priority = _calculate_spatial_priority(
                    chunk.row, chunk.col, chunk.zoom, base_priority
                )
                
                chunk_getter.submit(chunk)
                data_updated = True
            else:
                log.debug(f"GET_IMG: Chunk {chunk} already ready - reusing for mipmap {mipmap}")

        # We've already determined this mipmap is not marked as 'retrieved' so we should create 
        # a new image, regardless here.
        #if not data_updated:
        #    log.info("No updates to chunks.  Exit.")
        #    return False

        # Calculate image dimensions based on the actual zoom level we're using for downloads
        # This creates smaller textures that save VRAM when zoom is capped
        img_width = 256 * width
        img_height = 256 * height
        
        log.debug(f"GET_IMG: Using download dimensions {width}x{height} chunks = {img_width}x{img_height} pixels")
        
        log.debug(f"GET_IMG: Create new image: Zoom: {zoom} | {(img_width, img_height)}")
        
        new_im = AoImage.new(
            "RGBA",
            (img_width, img_height),
            (
                CFG.autoortho.missing_color[0],
                CFG.autoortho.missing_color[1],
                CFG.autoortho.missing_color[2],
            ),
        )

        log.debug(f"GET_IMG: Will use image {new_im}")

        # Check if we have any chunks to process
        if len(chunks) == 0:
            log.warning(f"GET_IMG: No chunks created for zoom {zoom}, mipmap {mipmap}")
            # Create a placeholder image with the expected dimensions
            placeholder_img = AoImage.new('RGBA', (img_width, img_height), (128, 128, 128))
            return placeholder_img
            
        def process_chunk(chunk, skip_download_wait=False):
            """Process a single chunk and return (chunk, chunk_img, start_x, start_y)"""
            # Calculate position using native chunk size (no scaling)
            start_x = int(chunk.width * (chunk.col - col))
            start_y = int(chunk.height * (chunk.row - row))
            
            # Track if this chunk permanently failed
            is_permanent_failure = chunk.permanent_failure
            if is_permanent_failure:
                log.debug(f"Chunk {chunk} is permanently failed ({chunk.failure_reason}), will attempt fallbacks")
                bump(f'chunk_permanent_fail_{chunk.failure_reason}')
            
            # Smart timeout: only wait for download, not decode
            # If skip_download_wait is True, go straight to fallbacks (for chunks that never started)
            if skip_download_wait:
                chunk_ready = False
                log.debug(f"Chunk {chunk} never started downloading, skipping wait and going to fallbacks")
            elif chunk.ready.is_set():
                # Download already complete, just needs decode
                chunk_ready = True
                log.debug(f"Chunk {chunk} already downloaded, proceeding to decode")
            else:
                # Download in progress, apply maxwait
                chunk_ready = chunk.ready.wait(maxwait)
                if not chunk_ready:
                    log.debug(f"Chunk {chunk} download timed out after {maxwait}s")

            chunk_img = None
            if chunk_ready and chunk.data:
                # We returned and have data!
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Ready and found chunk data.")
                try:
                    with _decode_sem:
                        chunk_img = AoImage.load_from_memory(chunk.data)
                except Exception as _e:
                    log.debug(f"GET_IMG: load_from_memory failed for {chunk}: {_e}")
            
            # FALLBACK CHAIN (in order of preference):
            # Each fallback only runs if previous ones failed
            # For permanent failures (404, etc), we ALWAYS try fallbacks to get lower-zoom alternatives
            
            # Fallback 1: Search disk cache for lower-zoom JPEGs
            if not chunk_img and (not chunk_ready or is_permanent_failure):
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Searching disk cache for backup chunk.")
                chunk_img = self.get_best_chunk(chunk.col, chunk.row, mipmap, zoom)
                # get_best_chunk bumps 'upscaled_from_jpeg_count' if successful
            
            # Fallback 2: Scale from already-built mipmaps (if they exist)
            if not chunk_img:
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Attempting scaling from built mipmaps.")
                chunk_img = self.get_downscaled_from_higher_mipmap(mipmap, chunk.col, chunk.row, zoom)
                # Note: scaling function bumps its own counters (upscaled_chunk_count or downscaled_chunk_count)
            
            # Fallback 3: On-demand download of lower-detail chunks
            if not chunk_img and (not chunk_ready or is_permanent_failure):
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Attempting cascading fallback (download).")
                chunk_img = self.get_or_build_lower_mipmap_chunk(mipmap, chunk.col, chunk.row, zoom)

            if not chunk_ready and not chunk_img and not is_permanent_failure:
                # Ran out of time, lower mipmap.  Retry...
                # Don't retry permanent failures (404, etc) - they won't succeed
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Final retry for {chunk}, WAITING!")
                bump('retry_chunk_count')
                # Smart timeout for retry: check if already downloaded
                if chunk.ready.is_set():
                    chunk_ready = True
                    log.debug(f"Chunk {chunk} downloaded during retry, proceeding to decode")
                else:
                    chunk_ready = chunk.ready.wait(maxwait)
                if chunk_ready and chunk.data:
                    log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Final retry for {chunk}, SUCCESS!")
                    try:
                        with _decode_sem:
                            chunk_img = AoImage.load_from_memory(chunk.data)
                    except Exception as _e:
                        log.debug(f"GET_IMG: load_from_memory failed on retry for {chunk}: {_e}")

            if not chunk_img:
                log.debug(f"GET_IMG(process_chunk(tid={threading.get_ident()})): Empty chunk data.  Skip.")
                bump('chunk_missing_count')
                if is_permanent_failure:
                    log.info(f"Chunk {chunk} permanently failed and all fallbacks exhausted")
                
            return (chunk, chunk_img, start_x, start_y)
        
        # Process chunks lazily - only after their download has started
        max_pool_workers = min(CURRENT_CPU_COUNT, len(chunks), _MAX_DECODE)
        
        processed_chunks = set()
        total_chunks = len(chunks)
        completed = 0
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_pool_workers)
        active_futures = {}
        
        start_time = time.time()
        max_total_wait = max(10.0, self.maxchunk_wait * 10)  # At least 10s, or 10x maxwait
        
        try:
            while len(processed_chunks) < len(chunks):
                # Safety timeout check
                if time.time() - start_time > max_total_wait:
                    chunks_abandoned = len(chunks) - len(processed_chunks)
                    if chunks_abandoned > 0:
                        bump('chunk_missing_count', chunks_abandoned)
                    log.warning(f"Lazy submission timeout after {max_total_wait}s, "
                               f"processed {len(processed_chunks)}/{len(chunks)} chunks, abandoned {chunks_abandoned}")
                    break
                
                # Smart early exit - if all started downloads are done, exit
                # Only use early exit when spatial priorities are active (during flight)
                # to avoid incomplete mipmaps during initial load or tests
                if datareftracker.data_valid and datareftracker.connected:
                    chunks_started = sum(1 for c in chunks if c.download_started.is_set())
                    if chunks_started > 0 and len(processed_chunks) >= chunks_started:
                        chunks_never_started = len(chunks) - chunks_started
                        if chunks_never_started > 0:
                            log.debug(f"Early exit: {chunks_never_started} chunks never started")
                        break
                
                # Find chunks whose downloads have started but haven't been submitted for processing yet
                for chunk in chunks:
                    chunk_id = id(chunk)
                    if chunk_id in processed_chunks:
                        continue
                    
                    # Skip permanently failed chunks immediately
                    if chunk.permanent_failure:
                        processed_chunks.add(chunk_id)
                        bump('chunk_missing_count')
                        continue
                    
                    # Submit when download has started
                    if chunk.download_started.is_set():
                        future = executor.submit(process_chunk, chunk)
                        active_futures[future] = chunk
                        processed_chunks.add(chunk_id)
                
                # Process any completed futures
                if active_futures:
                    done, pending = concurrent.futures.wait(
                        active_futures.keys(), 
                        timeout=0.025,  # Check 4x more frequently
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    
                    for future in done:
                        try:
                            chunk, chunk_img, start_x, start_y = future.result()
                            completed += 1
                            if chunk_img:
                                new_im.paste(chunk_img, (start_x, start_y))
                                # Free native buffer immediately after paste
                                try:
                                    chunk_img.close()
                                except Exception:
                                    pass
                        except Exception as exc:
                            log.error(f"Chunk processing failed: {exc}")
                        finally:
                            del active_futures[future]
                        # Progress logging
                        if total_chunks and (completed % max(1, total_chunks // 4) == 0):
                            log.debug(f"GET_IMG progress: {completed}/{total_chunks} chunks for mip {mipmap}")
                else:
                    # No active futures yet, wait a bit before checking for started downloads
                    time.sleep(0.01)  # Check 5x more frequently
            
            # Wait for any remaining futures to complete
            for future in concurrent.futures.as_completed(active_futures.keys()):
                try:
                    chunk, chunk_img, start_x, start_y = future.result()
                    if chunk_img:
                        new_im.paste(chunk_img, (start_x, start_y))
                except Exception as exc:
                    log.error(f"Chunk processing failed: {exc}")
            
            # CRITICAL: Process any chunks that never started downloading
            # These would otherwise be left as missing color patches
            unprocessed_chunks = [c for c in chunks if id(c) not in processed_chunks and not c.permanent_failure]
            if unprocessed_chunks:
                log.info(f"Processing {len(unprocessed_chunks)} chunks that never started downloading (fallback only)")
                
                # Submit ALL unprocessed chunks in parallel (not serial!)
                unprocessed_futures = {}
                for chunk in unprocessed_chunks:
                    # Use skip_download_wait=True to go straight to fallbacks
                    future = executor.submit(process_chunk, chunk, skip_download_wait=True)
                    unprocessed_futures[future] = chunk
                    processed_chunks.add(id(chunk))
                
                # Process results as they complete (parallel, not serial)
                for future in concurrent.futures.as_completed(unprocessed_futures.keys(), timeout=10):
                    try:
                        chunk, chunk_img, start_x, start_y = future.result()
                        if chunk_img:
                            new_im.paste(chunk_img, (start_x, start_y))
                        else:
                            bump('chunk_missing_count')
                    except Exception as exc:
                        chunk = unprocessed_futures.get(future, "unknown")
                        log.debug(f"Fallback failed for unprocessed chunk {chunk}: {exc}")
                        bump('chunk_missing_count')
                    
        finally:
            executor.shutdown(wait=True)

        if complete_img and mipmap <= self.max_mipmap:
            log.debug(f"GET_IMG: Save complete image for later...")
            # Store image with metadata (col, row, zoom) for coordinate mapping in upscaling
            self.imgs[mipmap] = (new_im, col, row, zoom)

        log.debug(f"GET_IMG: DONE!  IMG created {new_im}")

        if seasons_enabled:
            saturation = 0.01 * season_saturation_locked(self.row, self.col, self.tilename_zoom)
            if saturation < 1.0:    # desaturation is expensive
                new_im = new_im.copy().desaturate(saturation)
        # Return image along with mipmap and zoom level this was created at
        return new_im

    def get_or_build_lower_mipmap_chunk(self, target_mipmap, col, row, zoom):
        """
        Cascading fallback: Try to get/build progressively lower-detail mipmaps.
        Only downloads chunks on-demand when needed (lazy evaluation).
        
        Args:
            target_mipmap: The mipmap level we need (e.g., 0)
            col, row, zoom: Chunk coordinates at target zoom
        
        Returns:
            Upscaled AoImage or None
        """
        # Try each progressively lower-detail mipmap
        for fallback_mipmap in range(target_mipmap + 1, self.max_mipmap + 1):
            log.debug(f"Cascading fallback: trying mipmap {fallback_mipmap} for failed mipmap {target_mipmap}")
            
            # Calculate coordinates at fallback mipmap level
            mipmap_diff = fallback_mipmap - target_mipmap
            fallback_col = col >> mipmap_diff
            fallback_row = row >> mipmap_diff
            fallback_zoom = zoom - mipmap_diff
            
            # Try to get the chunk at this fallback level
            fallback_chunk = Chunk(fallback_col, fallback_row, self.maptype, fallback_zoom, cache_dir=self.cache_dir)
            
            # Check cache first
            if not fallback_chunk.ready.is_set():
                if fallback_chunk.get_cache():
                    fallback_chunk.ready.set()
                    log.debug(f"Cascading fallback: found cached chunk at mipmap {fallback_mipmap}")
                else:
                    # Not in cache, try to download it (with shorter timeout)
                    chunk_getter.submit(fallback_chunk)
                    # Wait with reduced timeout (don't want to cascade delays)
                    fallback_chunk_ready = fallback_chunk.ready.wait(timeout=min(3.0, self.get_maxwait() / 2))
                    if not fallback_chunk_ready:
                        log.debug(f"Cascading fallback: mipmap {fallback_mipmap} also timed out, trying next level")
                        fallback_chunk.close()
                        continue
            
            # We have the chunk data, decode and upscale it
            if fallback_chunk.data:
                try:
                    with _decode_sem:
                        fallback_img = AoImage.load_from_memory(fallback_chunk.data)
                    
                    # Calculate which portion to extract and upscale
                    scale_factor = 1 << mipmap_diff
                    offset_col = col % scale_factor
                    offset_row = row % scale_factor
                    
                    log.debug(f"CASCADE DEBUG: target=({col},{row}), fallback_chunk=({fallback_col},{fallback_row}), offset=({offset_col},{offset_row}), scale={scale_factor}")
                    
                    # Pixel position in fallback image
                    pixel_x = offset_col * (256 // scale_factor)
                    pixel_y = offset_row * (256 // scale_factor)
                    crop_size = 256 // scale_factor
                    
                    # Upscale to 256x256
                    upscaled = fallback_img.crop_and_upscale(
                        pixel_x, pixel_y, crop_size, crop_size, scale_factor
                    )
                    
                    log.debug(f"Cascading fallback SUCCESS: upscaled mipmap {fallback_mipmap} -> {target_mipmap} at {col}x{row} (scale {scale_factor}x)")
                    bump('upscaled_chunk_count')
                    bump('chunk_from_cascade_fallback')
                    
                    fallback_chunk.close()
                    return upscaled
                    
                except Exception as e:
                    log.warning(f"Cascading fallback: failed to upscale from mipmap {fallback_mipmap}: {e}")
                    fallback_chunk.close()
                    continue
            
            fallback_chunk.close()
        
        log.debug(f"Cascading fallback: all mipmaps failed for {col}x{row} at mipmap {target_mipmap}")
        return None

    def get_downscaled_from_higher_mipmap(self, target_mipmap, col, row, zoom):
        """
        Try to scale from already-built mipmaps to fill missing chunk.
        Checks both downscaling (from higher-detail) and upscaling (from lower-detail).
        
        Args:
            target_mipmap: The mipmap level we need (e.g., 0 or 3)
            col, row, zoom: Chunk coordinates at target zoom
        
        Returns:
            Scaled AoImage or None
        """
        for higher_mipmap in range(target_mipmap):
            if higher_mipmap not in self.imgs:
                continue  # Haven't built this mipmap yet
            
            img_data = self.imgs[higher_mipmap]
            if not img_data:
                continue
            
            # Unpack metadata (or use old format for backward compat)
            if isinstance(img_data, tuple):
                higher_img = img_data[0]  # Extract image from tuple
            else:
                higher_img = img_data  # Old format: just the image
            
            if higher_img is None:
                continue
            
            # Calculate scale factor
            scale_factor = 1 << (target_mipmap - higher_mipmap)
            
            chunk_offset_x = (col % scale_factor) * 256
            chunk_offset_y = (row % scale_factor) * 256
            
            try:
                # Crop scale_factor*256 region, then downscale to 256
                crop_size = 256 * scale_factor
                cropped = AoImage.new('RGBA', (crop_size, crop_size), (0,0,0,0))
                higher_img.crop(cropped, (chunk_offset_x, chunk_offset_y))
                
                downscale_steps = int(math.log2(scale_factor))
                downscaled = cropped.reduce_2(downscale_steps)
                
                log.debug(f"Downscaled mipmap {higher_mipmap} to fill missing mipmap {target_mipmap} chunk at {col}x{row}")
                bump('downscaled_chunk_count')
                return downscaled
            except Exception as e:
                log.debug(f"Failed to downscale from mipmap {higher_mipmap}: {e}")
                continue
        
        # Search for lower-detail mipmaps (higher mipmap numbers) to upscale
        for lower_mipmap in range(target_mipmap + 1, self.max_mipmap + 1):
            if lower_mipmap not in self.imgs:
                continue
            
            img_data = self.imgs[lower_mipmap]
            if not img_data:
                continue
            
            # Unpack metadata
            if isinstance(img_data, tuple):
                lower_img, base_col, base_row, base_zoom = img_data
            else:
                continue  # Old format without metadata, skip
            
            # Calculate scale factor and relative position
            scale_factor = 1 << (lower_mipmap - target_mipmap)
            
            # Map requested chunk to position in lower-detail image
            # lower_img was built from chunks starting at (base_col, base_row) at base_zoom
            # We need chunk (col, row) at zoom
            
            # Convert requested chunk coords to the zoom level of the lower image
            zoom_diff = zoom - base_zoom
            if zoom_diff >= 0:
                # Requested chunk is at higher or same zoom as image base
                # Calculate which parent chunk at base_zoom level
                parent_col = col >> zoom_diff
                parent_row = row >> zoom_diff
                
                # Relative to base position (which parent chunk in the image)
                rel_col = parent_col - base_col
                rel_row = parent_row - base_row
                
                # Calculate sub-chunk offset within that parent (0 to 2^zoom_diff-1)
                sub_col = col & ((1 << zoom_diff) - 1)  # Equivalent to col % (1 << zoom_diff)
                sub_row = row & ((1 << zoom_diff) - 1)
            else:
                # Requested chunk is at lower zoom (shouldn't happen but handle it)
                parent_col = col << (-zoom_diff)
                parent_row = row << (-zoom_diff)
                rel_col = parent_col - base_col
                rel_row = parent_row - base_row
                sub_col = 0
                sub_row = 0
            
            # Size to crop from lower image (will be upscaled by scale_factor)
            crop_size = 256 // scale_factor
            
            # Each parent chunk in the image is 256px wide
            # We need to find the sub-chunk within that 256x256 parent
            # Each sub-chunk occupies (256 // scale_factor) pixels in the parent
            sub_chunk_size_in_parent = crop_size  # Same as 256 // scale_factor
            pixel_x = rel_col * 256 + sub_col * sub_chunk_size_in_parent
            pixel_y = rel_row * 256 + sub_row * sub_chunk_size_in_parent
            
            log.debug(f"MIPMAP UPSCALE DEBUG: target=({col},{row},z{zoom}) base=({base_col},{base_row},z{base_zoom}) parent=({parent_col},{parent_row}) sub=({sub_col},{sub_row}) pixel=({pixel_x},{pixel_y}) crop={crop_size}")
            
            # Bounds check
            img_width, img_height = lower_img.size
            if (pixel_x < 0 or pixel_y < 0 or 
                pixel_x + crop_size > img_width or 
                pixel_y + crop_size > img_height):
                log.debug(f"Upscale bounds check failed: pos ({pixel_x},{pixel_y}) crop {crop_size} img ({img_width}x{img_height})")
                continue
            
            try:
                upscaled = lower_img.crop_and_upscale(
                    pixel_x, pixel_y, crop_size, crop_size, scale_factor
                )
                log.debug(f"Upscaled mipmap {lower_mipmap} (zoom {base_zoom}) to fill mipmap {target_mipmap} chunk at {col}x{row}")
                bump('upscaled_chunk_count')
                return upscaled
            except Exception as e:
                log.debug(f"Failed to upscale from mipmap {lower_mipmap}: {e}")
                continue
        
        return None

    def get_best_chunk(self, col, row, mm, zoom):
        # For adaptive system, search up to actual_max_zoom level
        max_search_zoom = self.max_zoom

        for i in range(mm + 1, self.max_mipmap + 1):
            # Difference between requested mm and found image mm level
            diff = i - mm
            
            # Equivalent col, row, zl
            col_p = col >> diff
            row_p = row >> diff
            zoom_p = zoom - i
            
            # Don't search beyond our detected actual_max_zoom
            if zoom_p > max_search_zoom:
                continue

            scalefactor = min(1 << diff, 16)

            # Check if we have a cached chunk
            c = Chunk(col_p, row_p, self.maptype, zoom_p, cache_dir=self.cache_dir)
            log.debug(f"Check cache for {c}")
            cached = c.get_cache()
            if not cached:
                c.close()
                continue
        
            log.debug(f"Found cached JPEG for {col}x{row}x{zoom} (mm{mm}) at {col_p}x{row_p}x{zoom_p} (mm{i}), upscaling {scalefactor}x")
            # Offset into chunk
            col_offset = col % scalefactor
            row_offset = row % scalefactor

            log.debug(f"UPSCALE DEBUG: col={col}, row={row}, col_p={col_p}, row_p={row_p}, col_offset={col_offset}, row_offset={row_offset}, scalefactor={scalefactor}")

            # Pixel width
            w_p = max(1, 256 >> diff)
            h_p = max(1, 256 >> diff)

            log.debug(f"Pixel Size: {w_p}x{h_p}")

            # Load image to crop
            img_p = AoImage.load_from_memory(c.data)
            if not img_p:
                log.warning(f"Failed to load chunk {c} into memory.")
                c.close()
                continue

            # Crop
            crop_img = AoImage.new('RGBA', (w_p, h_p), (0,255,0))
            img_p.crop(crop_img, (col_offset * w_p, row_offset * h_p))
            chunk_img = crop_img.scale(scalefactor)

            # Close the cache chunk to free memory before returning
            c.close()
            
            # Track upscaling from cached JPEGs separately
            bump('upscaled_from_jpeg_count')
            return chunk_img

        log.debug(f"No best chunk found for {col}x{row}x{zoom}!")
        return None

    def get_maxwait(self):
        effective_maxwait = self.maxchunk_wait
        if CFG.autoortho.suspend_maxwait and not datareftracker.connected:
            effective_maxwait = 20
        return effective_maxwait

    #@profile
    @locked
    def get_mipmap(self, mipmap=0):
        #
        # Protect this method to avoid simultaneous threads attempting mm builds at the same time.
        # Otherwise we risk contention such as waiting get_img call attempting to build an image as 
        # another thread closes chunks.
        #

        log.debug(f"GET_MIPMAP: {self}")

        if mipmap > self.max_mipmap:
            mipmap = self.max_mipmap

        # We can have multiple threads wait on get_img ...
        log.debug(f"GET_MIPMAP: Next call is get_img which may block!.............")
        new_im = self.get_img(mipmap, maxwait=self.get_maxwait())
        if not new_im:
            log.debug("GET_MIPMAP: No updates, so no image generated")
            return True

        self.ready.clear()
        start_time = time.time()
        try:
            if mipmap == 0:
                self.dds.gen_mipmaps(new_im, mipmap, 0) 
            else:
                self.dds.gen_mipmaps(new_im, mipmap) 
        finally:
            pass
            #new_im.close()

        end_time = time.time()
        self.ready.set()

        zoom = self.max_zoom - mipmap
        tile_time = end_time - start_time
        mm_stats.set(mipmap, tile_time)

        # Record mm stats via counters for aggregation
        try:
            bump_many({
                f"mm_count:{mipmap}": 1,
                f"mm_time_total_ms:{mipmap}": int(tile_time * 1000)
            })
        except Exception:
            pass

        # Don't close all chunks since we don't gen all mipmaps 
        if mipmap == 0:
            log.debug("GET_MIPMAP: Will close all chunks.")
            for z,chunks in self.chunks.items():
                for chunk in chunks:
                    chunk.close()
            self.chunks = {}
                    #del(chunk.data)
                    #del(chunk.img)
        #return outfile
        log.debug("Results:")
        log.debug(self.dds.mipmap_list)
        return True


    def should_close(self):
        if self.dds.mipmap_list[0].retrieved:
            if self.bytes_read < self.dds.mipmap_list[0].length:
                log.warning(f"TILE: {self} retrieved mipmap 0, but only read {self.bytes_read}. Lowest offset: {self.lowest_offset}")
                return False
            else:
                #log.info(f"TILE: {self} retrieved mipmap 0, full read of mipmap! {self.bytes_read}.")
                return True
        else:
            return True


    def close(self):
        log.debug(f"Closing {self}")

        if self.dds.mipmap_list[0].retrieved:
            if self.bytes_read < self.dds.mipmap_list[0].length:
                log.warning(f"TILE: {self} retrieved mipmap 0, but only read {self.bytes_read}. Lowest offset: {self.lowest_offset}")
            else:
                log.debug(f"TILE: {self} retrieved mipmap 0, full read of mipmap! {self.bytes_read}.")


        if self.refs > 0:
            log.warning(f"TILE: Trying to close, but has refs: {self.refs}")
            return

        # ------------------------------------------------------------------
        # Memory-reclamation additions
        # ------------------------------------------------------------------

        # 1) Free any cached AoImage instances (RGBA pixel buffers)
        try:
            for img_data in list(self.imgs.values()):
                # Handle both tuple format (new) and plain image (old)
                if isinstance(img_data, tuple):
                    im = img_data[0]  # Extract image from tuple
                else:
                    im = img_data
                
                if im is not None and hasattr(im, "close"):
                    im.close()
        finally:
            self.imgs.clear()

        # 2) Release DDS mip-map ByteIO buffers so the underlying bytes
        #    are no longer referenced from Python.
        if self.dds is not None:
            for mm in getattr(self.dds, "mipmap_list", []):
                mm.databuffer = None
            # Drop the DDS object reference itself
            self.dds = None

        for chunks in self.chunks.values():
            for chunk in chunks:
                chunk.close()
        self.chunks = {}
        

class TileCacher(object):
    hits = 0
    misses = 0

    enable_cache = True
    cache_mem_lim = pow(2,30) * float(CFG.cache.cache_mem_limit)
    cache_tile_lim = 25

    def __init__(self, cache_dir='.cache'):
        if MEMTRACE:
            tracemalloc.start()

        self.tiles = OrderedDict()
        self.open_count = {}

        self.maptype_override = CFG.autoortho.maptype_override
        if self.maptype_override:
            log.info(f"Maptype override set to {self.maptype_override}")
            if self.maptype_override == "APPLE":
                apple_token_service.reset_apple_maps_token()
        else:
            log.info(f"Maptype override not set, will use default.")
        log.info(f"Will use Compressor: {CFG.pydds.compressor}")
        self.tc_lock = threading.RLock()
        self._pid = os.getpid()
        # Eviction behavior controls
        self.evict_hysteresis_frac = 0.10  # keep ~10% headroom below limit
        self.evict_headroom_min_bytes = 256 * 1048576  # at least 256MB headroom
        self.evict_leader_ttl_sec = 5  # seconds
        
        self.cache_dir = CFG.paths.cache_dir
        log.info(f"Cache dir: {self.cache_dir}")
        self.min_zoom = int(CFG.autoortho.min_zoom)
        # Set target zoom level directly - much simpler than offset calculations
        self.target_zoom_level = int(CFG.autoortho.max_zoom)  # Direct zoom level target, regardless of tile name
        self.target_zoom_level_near_airports = int(CFG.autoortho.max_zoom_near_airports)
        log.info(f"Target zoom level set to ZL{self.target_zoom_level}")

        self.clean_t = threading.Thread(target=self.clean, daemon=True)
        self.clean_t.start()

        if system_type == 'windows':
            # Windows doesn't handle FS cache the same way so enable here.
            self.enable_cache = True
            self.cache_tile_lim = 50
    
    def _get_target_zoom_level(self, default_zoom: int) -> int:
        if CFG.autoortho.using_custom_tiles:
            uncapped_target_zoom = self.target_zoom_level
        else:
            uncapped_target_zoom = self.target_zoom_level_near_airports if default_zoom == 18 else self.target_zoom_level
        return min(default_zoom + 1, uncapped_target_zoom)

    def _to_tile_id(self, row, col, map_type, zoom):
        if self.maptype_override:
            map_type = self.maptype_override
        tile_id = f"{row}_{col}_{map_type}_{zoom}"
        return tile_id

    def show_stats(self):
        process = psutil.Process(os.getpid())
        cur_mem = process.memory_info().rss
        # Report per-process memory to shared store; parent will aggregate
        update_process_memory_stat()
        #set_stat('tile_mem_open', len(self.tiles))
        if self.enable_cache:
            #set_stat('tile_mem_miss', self.misses)
            #set_stat('tile_mem_hits', self.hits)
            log.debug(f"TILE CACHE:  MISS: {self.misses}  HIT: {self.hits}")
        log.debug(f"NUM OPEN TILES: {len(self.tiles)}.  TOTAL MEM: {cur_mem//1048576} MB")

    # -----------------------------
    # LRU helpers and leader logic
    # -----------------------------
    def _touch_tile(self, idx, tile):
        try:
            # Move to MRU position
            self.tiles.move_to_end(idx, last=True)
        except Exception:
            pass

    def _lru_candidates(self):
        try:
            # Keys iterate from LRU -> MRU after move_to_end
            return list(self.tiles.keys())
        except Exception:
            return list(self.tiles.keys())

    def _has_shared_store(self) -> bool:
        return bool(getattr(STATS, "_remote", None) or os.getenv("AO_STATS_ADDR"))

    def _try_acquire_evict_leader(self) -> bool:
        if not self._has_shared_store():
            return True
        now = int(time.time())
        try:
            leader_until = get_stat('evict_leader_until') or 0
            leader_pid = get_stat('evict_leader_pid') or 0
        except Exception:
            leader_until = 0
            leader_pid = 0

        if int(leader_until) < now:
            # Try to become leader
            try:
                set_stat('evict_leader_pid', self._pid)
                set_stat('evict_leader_until', now + self.evict_leader_ttl_sec)
                return True
            except Exception:
                return False

        # Renew if we are already leader
        if int(leader_pid) == self._pid:
            try:
                set_stat('evict_leader_until', now + self.evict_leader_ttl_sec)
                return True
            except Exception:
                return False

        return False

    def _renew_evict_leader(self) -> None:
        if not self._has_shared_store():
            return
        try:
            now = int(time.time())
            if int(get_stat('evict_leader_pid') or 0) == self._pid:
                set_stat('evict_leader_until', now + self.evict_leader_ttl_sec)
        except Exception:
            pass

    def _evict_batch(self, max_to_evict: int) -> int:
        evicted = 0
        # Prefer strict LRU order: left to right
        for idx in list(self._lru_candidates()):
            if evicted >= max_to_evict:
                break
            t = self.tiles.get(idx)
            if not t:
                continue
            if t.refs > 0:
                continue
            # Evict this tile
            try:
                t = self.tiles.pop(idx)
            except KeyError:
                continue
            try:
                t.close()
            except Exception:
                pass
            finally:
                t = None
                evicted += 1
        return evicted

    def clean(self):
        log.info(f"Started tile clean thread.  Mem limit {self.cache_mem_lim}")
        # Faster cadence when a shared stats store is present (macOS parent)
        fast_mode = self._has_shared_store()
        poll_interval = 3 if fast_mode else 15

        while True:
            process = psutil.Process(os.getpid())
            cur_mem = process.memory_info().rss

            # Publish this process heartbeat + RSS so the parent can aggregate
            try:
                update_process_memory_stat()
            except Exception:
                pass

            self.show_stats()

            if not self.enable_cache:
                time.sleep(poll_interval)
                continue

            # Use aggregated memory across all workers when available; otherwise local RSS
            try:
                global_mem_mb = get_stat('cur_mem_mb')
                global_mem_bytes = int(global_mem_mb) * 1048576 if isinstance(global_mem_mb, (int, float)) else 0
            except Exception:
                global_mem_bytes = 0

            effective_mem = global_mem_bytes or cur_mem

            # Hysteresis target: evict down to limit - headroom
            headroom = max(int(self.cache_mem_lim * self.evict_hysteresis_frac), self.evict_headroom_min_bytes)
            target_bytes = max(0, int(self.cache_mem_lim) - headroom)

            # Leader election: only one worker performs eviction when shared store is present
            if effective_mem > self.cache_mem_lim:
                if not self._try_acquire_evict_leader():
                    time.sleep(poll_interval)
                    continue

            # Evict while above target using adaptive batch sizing
            while self.tiles and effective_mem > target_bytes:
                over_bytes = max(0, effective_mem - target_bytes)
                ratio = min(1.0, over_bytes / max(1, self.cache_mem_lim))
                adaptive = max(20, int(len(self.tiles) * min(0.10, ratio)))
                with self.tc_lock:
                    evicted = self._evict_batch(adaptive)
                if evicted == 0:
                    break

                # Recompute local RSS and, if available, the aggregated RSS
                cur_mem = process.memory_info().rss
                try:
                    global_mem_mb = get_stat('cur_mem_mb')
                    global_mem_bytes = int(global_mem_mb) * 1048576 if isinstance(global_mem_mb, (int, float)) else 0
                except Exception:
                    global_mem_bytes = 0
                effective_mem = global_mem_bytes or cur_mem

                # Renew leadership and publish heartbeat after an eviction batch
                try:
                    self._renew_evict_leader()
                    update_process_memory_stat()
                except Exception:
                    pass

            if MEMTRACE:
                snapshot = tracemalloc.take_snapshot()
                top_stats = snapshot.statistics('lineno')

                log.info("[ Top 10 ]")
                for stat in top_stats[:10]:
                        log.info(stat)

            time.sleep(poll_interval)

    def _get_tile(self, row, col, map_type, zoom):
        
        idx = self._to_tile_id(row, col, map_type, zoom)
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                tile = self._open_tile(row, col, map_type, zoom)
            else:
                # Touch LRU on hit
                self._touch_tile(idx, tile)
        return tile

    def _open_tile(self, row, col, map_type, zoom):
        if self.maptype_override and self.maptype_override != "Use tile default":
            map_type = self.maptype_override
        idx = self._to_tile_id(row, col, map_type, zoom)

        log.debug(f"Get_tile: {idx}")
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                self.misses += 1
                bump('tile_mem_miss')
                # Use target zoom level directly - much cleaner than offset calculations
                tile = Tile(
                    col, row, map_type, zoom, 
                    cache_dir=self.cache_dir,
                    min_zoom=self.min_zoom,
                    max_zoom=self._get_target_zoom_level(zoom),
                )
                self.tiles[idx] = tile
                # New tile becomes MRU
                self._touch_tile(idx, tile)
                self.open_count[idx] = self.open_count.get(idx, 0) + 1
                if self.open_count[idx] > 1:
                    log.debug(f"Tile: {idx} opened for the {self.open_count[idx]} time.")
            elif tile.refs <= 0:
                # Only in this case would this cache have made a difference
                self.hits += 1
                bump('tile_mem_hits')

            tile.refs += 1
        return tile

    
    def _close_tile(self, row, col, map_type, zoom):
        tile_id = self._to_tile_id(row, col, map_type, zoom)
        with self.tc_lock:
            t = self.tiles.get(tile_id)
            if not t:
                log.warning(f"Attmpted to close unknown tile {tile_id}!")
                return False

            t.refs -= 1

            if self.enable_cache: # and not t.should_close():
                log.debug(f"Cache enabled.  Delay tile close for {tile_id}")
                return True

            if t.refs <= 0:
                log.debug(f"No more refs for {tile_id} closing...")
                t = self.tiles.pop(tile_id)
                t.close()
                t = None
                del(t)
            else:
                log.debug(f"Still have {t.refs} refs for {tile_id}")

        return True

# ============================================================
# Module-level cleanup helpers
# ============================================================

def shutdown():
    """Free network pools, worker threads and cached tiles to minimise RSS
    just before interpreter exit. Safe to call multiple times."""

    global chunk_getter

    # 1. Stop background download threads
    try:
        if chunk_getter is not None:
            chunk_getter.stop()
            chunk_getter = None
    except Exception as _err:
        log.debug(f"ChunkGetter stop error: {_err}")

    # 2. Iterate over every TileCacher instance still alive and flush
    #    its caches.  We avoid importing autoortho_fuse to prevent cycles; instead
    #    we search the GC list.
    import gc
    for obj in gc.get_objects():
        try:
            if isinstance(obj, TileCacher):
                with obj.tc_lock:
                    for tile in list(obj.tiles.values()):
                        tile.close()
                    obj.tiles.clear()
        except Exception:
            # Ignore any edge-case failures during shutdown
            pass

    try:
        if stats_batcher:
            stats_batcher.stop()
    except Exception:
        pass

    try:
        clear_process_memory_stat()
    except Exception:
        pass

    log.info("autoortho.getortho shutdown complete")

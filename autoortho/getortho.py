#!/usr/bin/env python3

import os
import sys
import time
import math
import tempfile
import threading
import concurrent.futures

import subprocess
import collections
import uuid

from io import BytesIO
from urllib.request import urlopen, Request
from queue import Queue, PriorityQueue, Empty
from functools import wraps, lru_cache
from pathlib import Path

import pydds

import requests
import psutil
from aoimage import AoImage

from aoconfig import CFG
from aostats import STATS, StatTracker, set_stat, inc_stat, get_stat
from utils.constants import system_type
from utils.apple_token_service import apple_token_service
from utils.utils import coord_from_sleepy_tilename
from utils.tile_db_service import tile_db_service


MEMTRACE = False

import logging
log = logging.getLogger(__name__)

import tracemalloc
#from memory_profiler import profile


# Track average fetch times
tile_stats = StatTracker(20, 12)
mm_stats = StatTracker(0, 5)
partial_stats = StatTracker()


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
    WORKING = False
    session = None

    def __init__(self, num_workers):
        
        self.count = 0
        self.queue = PriorityQueue()
        self.workers = []
        self.WORKING = True
        self.localdata = threading.local()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections = int(CFG.autoortho.fetch_threads),
            pool_maxsize = int(CFG.autoortho.fetch_threads)
        )
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        

        for i in range(num_workers):
            t = threading.Thread(target=self.worker, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)

        #self.stat_t = t = threading.Thread(target=self.show_stats, daemon=True)
        #self.stat_t.start()


    def stop(self):
        self.WORKING = False
        for t in self.workers:
            t.join()
        # If a stats thread was started, join it as well
        stat_thread = getattr(self, 'stat_t', None)
        if stat_thread is not None:
            stat_thread.join()

    def worker(self, idx):
        global STATS
        self.localdata.idx = idx
        while self.WORKING:
            try:
                obj, args, kwargs = self.queue.get(timeout=5)
                #log.debug(f"Got: {obj} {args} {kwargs}")
            except Empty:
                #log.debug(f"timeout, continue")
                #log.info(f"Got {self.counter}")
                continue

            #STATS.setdefault('count', 0) + 1
            STATS['count'] = STATS.get('count', 0) + 1


            try:
                if not self.get(obj, *args, **kwargs):
                    log.warning(f"Failed getting: {obj} {args} {kwargs}, re-submit.")
                    self.submit(obj, *args, **kwargs)
            except Exception as err:
                log.error(f"ERROR {err} getting: {obj} {args} {kwargs}, re-submit.")
                self.submit(obj, *args, **kwargs)

    def get(obj, *args, **kwargs):
        raise NotImplementedError

    def submit(self, obj, *args, **kwargs):
        self.queue.put((obj, args, kwargs))

    def show_stats(self):
        while self.WORKING:
            log.info(f"{self.__class__.__name__} got: {self.count}")
            time.sleep(10)
        log.info(f"Exiting {self.__class__.__name__} stat thread.  Got: {self.count} total")


class ChunkGetter(Getter):
    def get(self, obj, *args, **kwargs):
        if obj.ready.is_set():
            log.info(f"{obj} already retrieved.  Exit")
            return True

        kwargs['idx'] = self.localdata.idx
        kwargs['session'] = self.session
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

        if not priority:
            self.priority = zoom
        self.chunk_id = f"{col}_{row}_{zoom}_{maptype}"
        self.ready = threading.Event()
        self.ready.clear()
        if maptype == "Null":
            self.maptype = "EOX"

        self.cache_path = os.path.join(self.cache_dir, f"{self.chunk_id}.jpg")
        
        # FIXED: Check cache during initialization and set ready if found
        if self.get_cache():
            self.ready.set()
            log.debug(f"Chunk {self} initialized with cached data")

    def __lt__(self, other):
        return self.priority < other.priority

    def __repr__(self):
        return f"Chunk({self.col},{self.row},{self.maptype},{self.zoom},{self.priority})"

    def get_cache(self):
        if os.path.isfile(self.cache_path):
            inc_stat('chunk_hit')
            cache_file = Path(self.cache_path)
            # Get data
            data = cache_file.read_bytes()

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
            inc_stat('chunk_miss')
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
        temp_filename = os.path.join(self.cache_dir, f"XX_{self.chunk_id}_{uuid.uuid4().hex}.tmp")

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

        if self.get_cache():
            self.ready.set()
            return True

        if not self.starttime:
            self.startime = time.time()

        server_num = idx%(len(self.serverlist))
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
            "APPLE": f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v=10181&accessKey={apple_token_service.apple_token}"
        }

        self.url = MAPTYPES[self.maptype.upper()]
        #log.debug(f"{self} getting {url}")
        header = {
                "user-agent": "curl/7.68.0"
        }
        if self.maptype.upper() == "EOX":
            log.info("EOX DETECTED")
            header.update({'referer': 'https://s2maps.eu/'})
       
        time.sleep((self.attempt/10))
        self.attempt += 1

        log.debug(f"Requesting {self.url} ..")

        use_requests = True
        
        resp = 0
        try:
            if use_requests:
                # FIXME: Not the best way to set headers
                session.headers = header
                #resp = session.get(self.url, stream=True)
                resp = session.get(self.url)
                status_code = resp.status_code

                if self.maptype.upper() == "APPLE" and status_code == 403:
                    log.warning(f"Failed with status {status_code} to get chunk {self} on server {server}.  Retrying with new Apple Maps token.")
                    apple_token_service.reset_apple_maps_token()
                    MAPTYPES["APPLE"] = f"https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1&z={self.zoom}&x={self.col}&y={self.row}&v=10181&accessKey={apple_token_service.apple_token}"
                    self.url = MAPTYPES[self.maptype.upper()]
                    resp = session.get(self.url)
                    status_code = resp.status_code

            else:
                req = Request(self.url, headers=header)
                resp = urlopen(req, timeout=5)
                status_code = resp.status

                if self.maptype.upper() == "APPLE" and status_code == 403:
                    log.warning(f"Failed with status {status_code} to get chunk {self} on server {server}.  Retrying with new Apple Maps token.")
                    apple_token_service.reset_apple_maps_token()
                    self.url = MAPTYPES[self.maptype.upper()]
                    resp = session.get(self.url)
                    status_code = resp.status_code

            if status_code != 200:
                log.warning(f"Failed with status {status_code} to get chunk {self} on server {server}.")
                inc_stat(f"http_{status_code}")
                inc_stat("req_err")

                err = get_stat("req_err")
                if err > 50:
                    ok = get_stat("req_ok")
                    error_rate = err / ( err + ok )
                    if error_rate >= 0.10:
                        log.error(f"Very high network error rate detected : {error_rate * 100 : .2f}%")
                        log.error(f"Check your network connection, DNS, maptype choice, and firewall settings.")
                return False

            inc_stat("req_ok")

            if use_requests:
                data = resp.content
                #data = resp.raw.read()
            else:
                data = resp.read()

            if _is_jpeg(data[:3]):
                log.debug(f"Data for {self} is JPEG")
                self.data = data
            else:
                # FFD8FF identifies image as a JPEG
                log.debug(f"Loading file {self} not a JPEG! {data[:3]} URL: {self.url}")
            #    return False
                self.data = b''

            inc_stat('bytes_dl', len(self.data))
                
        except Exception as err:
            log.warning(f"Failed to get chunk {self} on server {server}. Err: {err} URL: {self.url}")
            return False
        finally:
            if resp:
                resp.close()

        self.fetchtime = time.time() - self.starttime

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
        # Each chunk-row is 256 px tall → 64 blocks vertically
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

        log.debug(f"Startrow: {startrow} Endrow: {endrow} bytes_per_chunk_row: {bytes_per_chunk_row} width_px: {base_width_px} height_px: {base_height_px}")
        
        new_im = self.get_img(mipmap, startrow, endrow,
                maxwait=self.maxchunk_wait)
        if not new_im:
            log.debug("No updates, so no image generated")
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

        # We haven't fully retrieved so unset flag
        log.debug(f"UNSETTING RETRIEVED! {self}")
        self.dds.mipmap_list[mipmap].retrieved = False
        end_time = time.time()
        self.ready.set()

        if compress_len:
            #STATS['partial_mm'] = STATS.get('partial_mm', 0) + 1
            tile_time = end_time - start_time
            partial_stats.set(mipmap, tile_time)
            STATS['partial_mm_averages'] = partial_stats.averages
            STATS['partial_mm_counts'] = partial_stats.counts

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

        # Get effective zoom  
        zoom = min((self.max_zoom - mipmap), self.max_zoom)
        log.debug(f"GET_IMG: Default tile zoom: {self.zoom}, Requested Mipmap: {mipmap}, Requested mipmap zoom: {zoom}")
        col, row, width, height, zoom, zoom_diff = self._get_quick_zoom(zoom, min_zoom)
        log.debug(f"Will use:  Zoom: {zoom},  Zoom_diff: {zoom_diff}")        
        
        log.debug(f"GET_IMG: Final zoom {zoom} for mipmap {mipmap}, coords: {col}x{row}, size: {width}x{height}")
        
        # Do we already have this img?
        if mipmap in self.imgs:
            log.debug(f"GET_IMG: Found saved image: {self.imgs[mipmap]}")
            return self.imgs.get(mipmap)

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
                chunk.priority = self.min_zoom - mipmap 
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
        
        new_im = AoImage.new('RGBA', (img_width, img_height), (66,77,55))

        log.debug(f"GET_IMG: Will use image {new_im}")

        # Check if we have any chunks to process
        if len(chunks) == 0:
            log.warning(f"GET_IMG: No chunks created for zoom {zoom}, mipmap {mipmap}")
            # Create a placeholder image with the expected dimensions
            placeholder_img = AoImage.new('RGBA', (img_width, img_height), (128, 128, 128))
            return placeholder_img
            
        def process_chunk(chunk):
            """Process a single chunk and return (chunk, chunk_img, start_x, start_y)"""
            chunk_ready = chunk.ready.wait(maxwait)
            
            # Calculate position using native chunk size (no scaling)
            start_x = int(chunk.width * (chunk.col - col))
            start_y = int(chunk.height * (chunk.row - row))

            chunk_img = None
            if chunk_ready and chunk.data:
                # We returned and have data!
                log.debug(f"GET_IMG: Ready and found chunk data.")
                chunk_img = AoImage.load_from_memory(chunk.data)
            elif mipmap < self.max_mipmap and not chunk_ready:
                # Ran out of time, requesting mm below max.  Search for backup...
                log.debug(f"GET_IMG: Tile {self} not ready.  Try to find backup chunk.")
                chunk_img = self.get_best_chunk(chunk.col, chunk.row, mipmap, zoom)
                if chunk_img:
                    inc_stat('backup_chunk_count')

            if not chunk_ready and not chunk_img:
                # Ran out of time, lower mipmap.  Retry...
                log.debug(f"GET_IMG: Final retry for {chunk}")
                inc_stat('retry_chunk_count')
                chunk_ready = chunk.ready.wait(maxwait)
                if chunk_ready and chunk.data:
                    log.debug(f"GET_IMG: Final retry for {chunk}, SUCCESS!")
                    chunk_img = AoImage.load_from_memory(chunk.data)

            if not chunk_img and not chunk.data:
                log.debug(f"GET_IMG: Empty chunk data.  Skip.")
                STATS['chunk_missing_count'] = STATS.get('chunk_missing_count', 0) + 1
            elif not chunk_img and chunk.data:
                log.warning(f"GET_IMG: FAILED! {chunk}:  LEN: {len(chunk.data)}  HEADER: {chunk.data[:8]}")
                
            return (chunk, chunk_img, start_x, start_y)
        
        # Process all chunks in parallel instead of sequentially
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(chunks))) as executor:
            # Submit all chunk processing tasks
            future_to_chunk = {executor.submit(process_chunk, chunk): chunk for chunk in chunks}
            
            # Collect results and paste into image as they complete
            for future in concurrent.futures.as_completed(future_to_chunk):
                try:
                    chunk, chunk_img, start_x, start_y = future.result()
                    if chunk_img:
                        # For adaptive system, chunks are already downloaded at the optimal zoom level
                        # or retrieved from backup at appropriate resolution. Paste directly.
                        new_im.paste(chunk_img, (start_x, start_y))
                except Exception as exc:
                    log.error(f"Chunk processing failed: {exc}")

        if complete_img and mipmap <= self.max_mipmap:
            log.debug(f"GET_IMG: Save complete image for later...")
            self.imgs[mipmap] = new_im

        log.debug(f"GET_IMG: DONE!  IMG created {new_im}")
        # Return image along with mipmap and zoom level this was created at
        return new_im

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
        
            log.debug(f"Found best chunk for {col}x{row}x{zoom} at {col_p}x{row_p}x{zoom_p}")
            # Offset into chunk
            col_offset = col % scalefactor
            row_offset = row % scalefactor

            log.debug(f"Col_Offset: {col_offset}, Row_Offset: {row_offset}, Scale_Factor: {scalefactor}")

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
            return chunk_img

        log.debug(f"No best chunk found for {col}x{row}x{zoom}!")
        return False


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
        new_im = self.get_img(mipmap, maxwait=self.maxchunk_wait)
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

        #log.info(f"Compress MM {mipmap} for ZL {zoom} in {tile_time} seconds")
        #log.info(f"Average compress times: {mm_averages}")
        #log.info(f"MM counts: {mm_counts}")
        STATS['mm_counts'] = mm_stats.counts
        STATS['mm_averages'] = mm_stats.averages

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
            for im in list(self.imgs.values()):
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

        self.tiles = {}
        self.open_count = {}
        self.open_tiles_by_dsf = {}

        self.maptype_override = CFG.autoortho.maptype_override
        if self.maptype_override:
            log.info(f"Maptype override set to {self.maptype_override}")
            if self.maptype_override == "APPLE":
                apple_token_service.reset_apple_maps_token()
        else:
            log.info(f"Maptype override not set, will use default.")
        log.info(f"Will use Compressor: {CFG.pydds.compressor}")
        self.tc_lock = threading.RLock()
        
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

        log.info("Initialized global TileCacher")

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
        set_stat('cur_mem_mb', cur_mem//1048576)
        #set_stat('tile_mem_open', len(self.tiles))
        if self.enable_cache:
            #set_stat('tile_mem_miss', self.misses)
            #set_stat('tile_mem_hits', self.hits)
            log.debug(f"TILE CACHE:  MISS: {self.misses}  HIT: {self.hits}")
        log.debug(f"NUM OPEN TILES: {len(self.tiles)}.  TOTAL MEM: {cur_mem//1048576} MB")

    def clean(self):
        log.info(f"Started tile clean thread.  Mem limit {self.cache_mem_lim}")
        while True:
            process = psutil.Process(os.getpid())
            cur_mem = process.memory_info().rss

            self.show_stats()
            time.sleep(15)
            
            if not self.enable_cache:
                continue

            while len(self.tiles) >= self.cache_tile_lim and cur_mem > self.cache_mem_lim:
                log.debug("Hit cache limit.  Remove oldest 20")
                with self.tc_lock:
                    for i in list(self.tiles.keys())[:20]:
                        t = self.tiles.get(i)
                        if not t:
                            continue
                        if t.refs <= 0:
                            t = self.tiles.pop(i)
                            try:
                                lat, lon = coord_from_sleepy_tilename(t.row, t.col, t.tilename_zoom)
                            except Exception:
                                lat, lon = (None, None)
                            try:
                                t.close()
                            finally:
                                pass
                            # Only update open_tiles_by_dsf if we could compute coords
                            if lat is not None:
                                self.open_tiles_by_dsf[(lat, lon)] -= 1
                                if self.open_tiles_by_dsf[(lat, lon)] <= 0:
                                    del self.open_tiles_by_dsf[(lat, lon)]
                            del t
                cur_mem = process.memory_info().rss


            if MEMTRACE:
                snapshot = tracemalloc.take_snapshot()
                top_stats = snapshot.statistics('lineno')

                log.info("[ Top 10 ]")
                for stat in top_stats[:10]:
                        log.info(stat)

            time.sleep(15)

    def _get_tile(self, row, col, map_type, zoom):
        
        idx = self._to_tile_id(row, col, map_type, zoom)
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                tile = self._open_tile(row, col, map_type, zoom)
        return tile

    def _open_tile(self, row, col, map_type, zoom):
        if self.maptype_override and self.maptype_override != "Use tile default":
            if self.maptype_override == "Use tile settings":
                lat, lon = coord_from_sleepy_tilename(row, col, zoom)
                map_type = tile_db_service.get_tile_maptype(lat, lon)
            else:
                map_type = self.maptype_override
        idx = self._to_tile_id(row, col, map_type, zoom)

        log.debug(f"Get_tile: {idx}")
        with self.tc_lock:
            tile = self.tiles.get(idx)
            if not tile:
                self.misses += 1
                inc_stat('tile_mem_miss')
                # Use target zoom level directly - much cleaner than offset calculations
                tile = Tile(
                    col, row, map_type, zoom, 
                    cache_dir=self.cache_dir,
                    min_zoom=self.min_zoom,
                    max_zoom=self._get_target_zoom_level(zoom),
                )
                self.tiles[idx] = tile
                self.open_count[idx] = self.open_count.get(idx, 0) + 1
                if self.open_count[idx] > 1:
                    log.debug(f"Tile: {idx} opened for the {self.open_count[idx]} time.")
            elif tile.refs <= 0:
                # Only in this case would this cache have made a difference
                self.hits += 1
                inc_stat('tile_mem_hits')

            tile.refs += 1
        lat, lon = coord_from_sleepy_tilename(row, col, zoom)
        if self.open_tiles_by_dsf.get((lat, lon)) is None:
            self.open_tiles_by_dsf[(lat, lon)] = 1
        else:
            self.open_tiles_by_dsf[(lat, lon)] += 1
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
                del t
                lat, lon = coord_from_sleepy_tilename(row, col, zoom)
                self.open_tiles_by_dsf[(lat, lon)] -= 1
                if self.open_tiles_by_dsf[(lat, lon)] <= 0:
                    del self.open_tiles_by_dsf[(lat, lon)]
            else:
                log.debug(f"Still have {t.refs} refs for {tile_id}")

        return True

# ============================================================
# Module-level cleanup helpers
# ============================================================

"""Global TileCacher singleton used by all mounts"""
tile_cacher = TileCacher()

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

    log.info("autoortho.getortho shutdown complete")

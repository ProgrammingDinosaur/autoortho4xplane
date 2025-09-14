""" module to hold utility functions for cache """

import os
import threading
import logging

from aoconfig import CFG
from utils.cache_db_service import cache_db_service

log = logging.getLogger(__name__)

class CacheUtils:
    def __init__(self):
        self.delete_lock = threading.Lock()

    def delete_cache_for_tile_id(self, tile_id: str):
        with self.delete_lock:
            filenames = cache_db_service.get_files_for_tile_id(tile_id)
            for path in filenames:
                try:
                    os.remove(path)
                except Exception as e:
                    log.warning(f"Failed to delete cache file {path}: {e}")
            log.info(f"Deleted {len(filenames)} cache files for tile {tile_id}")
            cache_db_service.delete_cache_files_for_tile_id(tile_id)

    def delete_cache_for_lat_lon_maptype(self, lat: int, lon: int, maptype: str):
        with self.delete_lock:
            filenames = cache_db_service.get_files_for_lat_lon_maptype(lat, lon, maptype)
            for path in filenames:
                try:
                    os.remove(path)
                except Exception as e:
                    log.warning(f"Failed to delete cache file {path}: {e}")
            log.info(f"Deleted {len(filenames)} cache files for lat {lat}, lon {lon}, maptype {maptype}")
            cache_db_service.delete_cache_files_for_lat_lon_maptype(lat, lon, maptype)

    def delete_cache_for_lat_lon(self, lat: int, lon: int):
        with self.delete_lock:
            filenames = cache_db_service.get_files_for_lat_lon(lat, lon)
            for path in filenames:
                try:
                    os.remove(path)
                except Exception as e:
                    log.warning(f"Failed to delete cache file {path}: {e}")
            log.info(f"Deleted {len(filenames)} cache files for lat {lat}, lon {lon}")
            cache_db_service.delete_cache_files_for_lat_lon(lat, lon)

                    
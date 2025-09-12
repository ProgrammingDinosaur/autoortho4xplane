import sqlite3
import os
import threading
from aoconfig import CFG


class CacheDBService:
    def __init__(self):
        if not os.path.isdir(CFG.paths.cache_dir):
            os.makedirs(CFG.paths.cache_dir)
        self.cache_dir = os.path.join(CFG.paths.cache_dir, 'cache.ao')
        self.conn = sqlite3.connect(self.cache_dir, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute('''CREATE TABLE IF NOT EXISTS cache (
            tile_id TEXT NOT NULL,
            lat INTEGER NOT NULL,
            lon INTEGER NOT NULL,
            maptype TEXT NOT NULL,
            max_zoom INTEGER NOT NULL,
            is_cached BOOLEAN NOT NULL,
            PRIMARY KEY (tile_id, lat, lon, maptype, max_zoom)
        )''')

        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS cache_files (
                filename TEXT NOT NULL,
                maptype TEXT NOT NULL,
                lat INTEGER NOT NULL,
                lon INTEGER NOT NULL,
                parent_max_zoom INTEGER NOT NULL,
                parent_tile_id TEXT NOT NULL,
                size_in_bytes INTEGER NOT NULL,
                is_cached BOOLEAN NOT NULL,
                PRIMARY KEY (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id)
        )''')
        self.conn.commit()

    def set_tile_cache_state(self, tile_id: str, lat: int, lon: int, maptype: str, max_zoom: int, is_cached: bool):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (tile_id, lat, lon, maptype, max_zoom, is_cached) VALUES (?, ?, ?, ?, ?, ?)",
                (tile_id, lat, lon, maptype, max_zoom, is_cached)
            )
            self.conn.commit()

    def get_tile_cache_state(self, tile_id: str, lat: int, lon: int, maptype: str, max_zoom: int) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "SELECT is_cached FROM cache WHERE tile_id = ? AND lat = ? AND lon = ? AND maptype = ? AND max_zoom = ?",
                (tile_id, lat, lon, maptype, max_zoom)
            )
            return cursor.fetchone()[0] if cursor.fetchone() else False

    def set_cache_file_cache_state(self, filename: str, maptype: str, lat: int, lon: int, parent_max_zoom: int, parent_tile_id: str, size_in_bytes: int, is_cached: bool):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache_files (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id, size_in_bytes, is_cached) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id, size_in_bytes, is_cached)
            )
            self.conn.commit()
    
    def get_cache_file_cache_state(self, filename: str, maptype: str, lat: int, lon: int, parent_max_zoom: int, parent_tile_id: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "SELECT is_cached FROM cache_files WHERE filename = ? AND maptype = ? AND lat = ? AND lon = ? AND parent_max_zoom = ? AND parent_tile_id = ?",
                (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id)
            )
            return cursor.fetchone()[0] if cursor.fetchone() else False
    
    def delete_cache_file(self, filename: str):
        with self._lock:
            self.conn.execute(
                "DELETE FROM cache_files WHERE filename = ?",
                (filename,)
            )
            self.conn.commit()

    def get_cache_size_mb_total(self):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT SUM(size_in_bytes) FROM cache_files WHERE is_cached = 1"
            )
            return cursor.fetchone()[0] if cursor.fetchone() else 0
    
    def get_cache_size_mb_for_lat_lon(self, lat: int, lon: int):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT SUM(size_in_bytes) FROM cache_files WHERE lat = ? AND lon = ? AND is_cached = 1",
                (lat, lon)
            )
            return cursor.fetchone()[0] if cursor.fetchone() else 0

    def get_files_for_lat_lon(self, lat: int, lon: int):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT filename FROM cache_files WHERE lat = ? AND lon = ? AND is_cached = 1",
                (lat, lon)
            )
            return cursor.fetchall()
    
    def get_files_for_lat_lon_maptype(self, lat: int, lon: int, maptype: str):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT filename FROM cache_files WHERE lat = ? AND lon = ? AND maptype = ? AND is_cached = 1",
                (lat, lon, maptype)
            )
            return cursor.fetchall()
    

cache_db_service = CacheDBService()
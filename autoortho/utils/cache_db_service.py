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
            lat INTEGER NOT NULL,
            lon INTEGER NOT NULL,
            maptype TEXT NOT NULL,
            default_zoom INTEGER NOT NULL,
            max_zoom INTEGER NOT NULL,
            is_cached BOOLEAN NOT NULL,
            PRIMARY KEY (lat, lon, maptype, default_zoom, max_zoom)
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
                PRIMARY KEY (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id)
        )''')
        self.conn.commit()

    def create_tile_cache_state(self, tile_id: str, lat: int, lon: int, maptype: str, max_zoom: int, is_cached: bool):
        with self._lock:
            # Create a new entry for a tile only if it doesn't exist
            self.conn.execute(
                "INSERT INTO cache (tile_id, lat, lon, maptype, max_zoom, is_cached) VALUES (?, ?, ?, ?, ?, ?)",
                (tile_id, lat, lon, maptype, max_zoom, is_cached)
            )
            self.conn.commit()
            
    def update_tile_cache_state(self, tile_id: str, lat: int, lon: int, maptype: str, max_zoom: int, is_cached: bool):
        with self._lock:
            self.conn.execute(
                "UPDATE cache SET is_cached = ? WHERE tile_id = ? AND lat = ? AND lon = ? AND maptype = ? AND max_zoom = ?",
                (is_cached, tile_id, lat, lon, maptype, max_zoom)
            )
            self.conn.commit()

    def get_tile_cache_state(self, tile_id: str, lat: int, lon: int, maptype: str, max_zoom: int) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "SELECT is_cached FROM cache WHERE tile_id = ? AND lat = ? AND lon = ? AND maptype = ? AND max_zoom = ?",
                (tile_id, lat, lon, maptype, max_zoom)
            )
            row = cursor.fetchone()
            return bool(row[0]) if row is not None else False

    def set_cache_file_cache_state(self, filename: str, maptype: str, lat: int, lon: int, parent_max_zoom: int, parent_tile_id: str, size_in_bytes: int, is_cached: bool):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache_files (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id, size_in_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (filename, maptype, lat, lon, parent_max_zoom, parent_tile_id, size_in_bytes)
            )
            self.conn.commit()
    
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
                "SELECT SUM(size_in_bytes) FROM cache_files WHERE is_cached = TRUE"
            )
            row = cursor.fetchone()
            byte_size = row[0] if (row is not None and row[0] is not None) else 0
            return byte_size / 1024 / 1024
    
    def get_cache_size_mb_for_lat_lon(self, lat: int, lon: int):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT SUM(size_in_bytes) FROM cache_files WHERE lat = ? AND lon = ? AND is_cached = 1",
                (lat, lon)
            )
            row = cursor.fetchone()
            return row[0] if (row is not None and row[0] is not None) else 0

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

    def get_files_for_tile_id(self, tile_id: str):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT filename FROM cache_files WHERE parent_tile_id = ?",
                (tile_id,)
            )
            return cursor.fetchall()

    def delete_cache_files_for_lat_lon_maptype(self, lat: int, lon: int, maptype: str):
        with self._lock:
            self.conn.execute(
                "DELETE FROM cache_files WHERE lat = ? AND lon = ? AND maptype = ?",
                (lat, lon, maptype)
            )
            self.conn.commit()
    
    def delete_cache_files_for_tile_id(self, tile_id: str):
        with self._lock:
            self.conn.execute(
                "DELETE FROM cache_files WHERE parent_tile_id = ?",
                (tile_id,)
            )
            self.conn.commit()
    
    def delete_cache_files_for_lat_lon(self, lat: int, lon: int):
        with self._lock:
            self.conn.execute(
                "DELETE FROM cache_files WHERE lat = ? AND lon = ?",
                (lat, lon)
            )
            self.conn.commit()

    def get_latlon_maptype_cache_aggregate(self, lat: int, lon: int, maptype: str):
        """Return total rows and number cached for the given lat/lon and maptype.

        Returns a dict: {"total": int, "cached": int}
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(is_cached), 0) AS cached FROM cache WHERE lat = ? AND lon = ? AND maptype = ?",
                (lat, lon, maptype)
            )
            row = cursor.fetchone()
            total = int(row[0]) if row and row[0] is not None else -1
            cached = int(row[1]) if row and row[1] is not None else -1
            return {"total": total, "cached": cached}

cache_db_service = CacheDBService()

import sqlite3
import os
from aoconfig import CFG


class CacheDBService:
    def __init__(self):
        self.cache_dir = os.path.join(CFG.paths.cache_dir, 'cache.ao')
        self.conn = sqlite3.connect(self.cache_dir)
        self.conn.execute('''CREATE TABLE IF NOT EXISTS cache (
            row INTEGER NOT NULL,
            col INTEGER NOT NULL,
            zoom INTEGER NOT NULL,
            lat INTEGER NOT NULL,
            lon INTEGER NOT NULL,
            maptype TEXT NOT NULL,
            isCached BOOLEAN NOT NULL,
            PRIMARY KEY (row, col, zoom, lat, lon, maptype)
        )''')
        self.conn.commit()

    def set_tile_cache_state(self, row: int, col: int, zoom: int, lat: int, lon: int, maptype: str, isCached: bool):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (row, col, zoom, lat, lon, maptype, isCached) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row, col, zoom, lat, lon, maptype, isCached)
            )
            self.conn.commit()

    def get_tile_cache_state(self, row, col, zoom, lat, lon, maptype) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "SELECT isCached FROM cache WHERE row = ? AND col = ? AND zoom = ? AND lat = ? AND lon = ? AND maptype = ?",
                (row, col, zoom, lat, lon, maptype)
            )
            return cursor.fetchone()[0] if cursor.fetchone() else False
        

cache_db_service = CacheDBService()
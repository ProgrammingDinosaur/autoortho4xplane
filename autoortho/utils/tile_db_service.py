import sqlite3
import os
import threading

from aoconfig import CFG


DB_NAME = "tiles.ao"


class TileDBService:
    def __init__(self):
        self.default_maptype = "DFLT"
        self._lock = threading.Lock()

        db_path = os.path.join(CFG.paths.tile_db_dir)
        if not os.path.exists(db_path):
            os.makedirs(db_path)
        db_path_full = os.path.join(db_path, DB_NAME)
        # Open DB connection and ensure schema exists
        self.conn = sqlite3.connect(db_path_full, check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tiles (
                lat INTEGER NOT NULL,
                lon INTEGER NOT NULL,
                maptype TEXT NOT NULL,
                PRIMARY KEY (lat, lon)
            )
            """
        )
        self.conn.commit()

    def change_maptype(self, lat, lon, maptype):
        with self._lock:
            # Upsert row to ensure tile exists
            self.conn.execute(
                """
                INSERT INTO tiles (lat, lon, maptype)
                VALUES (?, ?, ?)
                ON CONFLICT(lat, lon) DO UPDATE SET maptype=excluded.maptype
                """,
                (lat, lon, maptype),
            )
            self.conn.commit()

    def get_tile_maptype(self, lat, lon):
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT maptype FROM tiles WHERE lat = ? AND lon = ?
                """,
                (lat, lon),
            )
            res = cursor.fetchone()
            if res:
                return res[0]
            else:
                return self.default_maptype


tile_db_service = TileDBService()

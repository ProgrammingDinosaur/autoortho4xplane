import sqlite3

from aoconfig import CFG

class TileDBService:
    def __init__(self):
        self.conn = sqlite3.connect(CFG.paths.tile_db_dir)
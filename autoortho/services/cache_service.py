""" module to hold utility functions for cache """

import os
import logging

from functools import lru_cache

from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, Text, Boolean, Index,
    select, insert, update, func
)
from sqlalchemy.pool import StaticPool

from aoconfig import CFG
from utils.utils import (
    coord_from_sleepy_tilename,
    get_main_dsf_folder_from_coord,
    get_dsf_name_from_coord
)

log = logging.getLogger(__name__)


class CacheDBService:

    db_folder = "cache_data"
    db_name = "cache.ao"

    def __init__(self):
        if not os.path.isdir(CFG.paths.cache_dir):
            os.makedirs(CFG.paths.cache_dir)

        db_path = os.path.join(
            CFG.paths.cache_dir, self.db_folder, self.db_name
        )

        # Ensure the db_folder directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # Create SQLAlchemy engine with connection pooling
        # pool_pre_ping=True ensures stale connections are handled
        # StaticPool for SQLite to handle thread-safety efficiently
        self.engine = create_engine(
            f'sqlite:///{db_path}',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
            pool_pre_ping=True,
            echo=False  # Set to True for SQL debugging
        )

        # Define metadata and table schema
        self.metadata = MetaData()
        self.cache_table = Table(
            'cache',
            self.metadata,
            Column('lat_tile', Integer, nullable=False,
                   primary_key=True),
            Column('lon_tile', Integer, nullable=False,
                   primary_key=True),
            Column('row', Integer, nullable=False, primary_key=True),
            Column('col', Integer, nullable=False, primary_key=True),
            Column('maptype', Text, nullable=False, primary_key=True),
            Column('cached_max_zoom', Integer, nullable=False,
                   primary_key=True),
            Column('cache_size_in_bytes', Integer, nullable=False),
            Column('is_cached', Boolean, nullable=False),
            # Add index for common query pattern
            Index('idx_tile_lookup', 'lat_tile', 'lon_tile', 'maptype')
        )

        # Create tables if they don't exist
        self.metadata.create_all(self.engine)

    def create_tile_cache_state(
        self, row: int, col: int, lat_tile: int, lon_tile: int,
        maptype: str, cached_max_zoom: int
    ):
        """Create a new cache entry for a tile."""
        stmt = insert(self.cache_table).values(
            row=row,
            col=col,
            lat_tile=lat_tile,
            lon_tile=lon_tile,
            maptype=maptype,
            cached_max_zoom=cached_max_zoom,
            cache_size_in_bytes=0,
            is_cached=False
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def update_tile_cache_state(
        self, row: int, col: int, lat_tile: int, lon_tile: int,
        maptype: str, cached_max_zoom: int, is_cached: bool
    ):
        """Update the cached state of a tile."""
        stmt = (
            update(self.cache_table)
            .where(
                (self.cache_table.c.row == row) &
                (self.cache_table.c.col == col) &
                (self.cache_table.c.lat_tile == lat_tile) &
                (self.cache_table.c.lon_tile == lon_tile) &
                (self.cache_table.c.maptype == maptype) &
                (self.cache_table.c.cached_max_zoom == cached_max_zoom)
            )
            .values(is_cached=is_cached)
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_tile_cache_state(
        self, row: int, col: int, lat_tile: int, lon_tile: int,
        maptype: str, cached_max_zoom: int
    ) -> bool:
        """Get the cached state of a tile.

        Returns True if cached, False otherwise.
        """
        stmt = (
            select(self.cache_table.c.is_cached)
            .where(
                (self.cache_table.c.row == row) &
                (self.cache_table.c.col == col) &
                (self.cache_table.c.lat_tile == lat_tile) &
                (self.cache_table.c.lon_tile == lon_tile) &
                (self.cache_table.c.maptype == maptype) &
                (self.cache_table.c.cached_max_zoom == cached_max_zoom)
            )
        )
        with self.engine.connect() as conn:
            result = conn.execute(stmt).fetchone()
            return bool(result[0]) if result is not None else False

    def get_rowcol_maptype_cache_aggregate(
        self, lat_tile: int, lon_tile: int, maptype: str
    ):
        """Return total rows and number cached for lat/lon and maptype.

        Returns a dict: {"total": int, "cached": int}
        """
        stmt = (
            select(
                func.count().label('total'),
                func.coalesce(
                    func.sum(self.cache_table.c.is_cached), 0
                ).label('cached')
            )
            .where(
                (self.cache_table.c.lat_tile == lat_tile) &
                (self.cache_table.c.lon_tile == lon_tile) &
                (self.cache_table.c.maptype == maptype)
            )
        )
        with self.engine.connect() as conn:
            result = conn.execute(stmt).fetchone()
            total = int(result[0]) if result and result[0] else -1
            cached = int(result[1]) if result and result[1] else -1
            return {"total": total, "cached": cached}

    def increment_cache_size_in_bytes(self, row: int, col: int, maptype: str, size: int):
        """Increment the cache size for a tile by bytes."""
        stmt = (
            update(self.cache_table)
            .where(
                (self.cache_table.c.row == row) &
                (self.cache_table.c.col == col) &
                (self.cache_table.c.maptype == maptype)

            )
            .values(
                cache_size_in_bytes=(
                    self.cache_table.c.cache_size_in_bytes + size
                )
            )
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_dsf_cache_size_in_bytes(self, lat: int, lon: int):
        """Get the cache size for a given lat/lon."""
        stmt = (
            select(func.sum(self.cache_table.c.cache_size_in_bytes))
            .where(
                (self.cache_table.c.lat_tile == lat) &
                (self.cache_table.c.lon_tile == lon)
            )
        )

        with self.engine.connect() as conn:
            result = conn.execute(stmt).fetchone()
            return int(result[0]) if result and result[0] else 0


class CacheManager:
    """Manages cache operations and file storage."""

    def __init__(self):
        self.cache_db_service = CacheDBService()

    @lru_cache(maxsize=2048)
    def get_cache_folder_for_tile(
        self, row: int, col: int, zoom: int, maptype: str
    ) -> str:
        """Get the cache folder path for a given tile."""
        tile_lat, tile_lon = coord_from_sleepy_tilename(row, col, zoom)
        main_dsf_folder = get_main_dsf_folder_from_coord(
            tile_lat, tile_lon
        )
        dsf_name = get_dsf_name_from_coord(tile_lat, tile_lon)
        return os.path.join(
            CFG.paths.cache_dir, main_dsf_folder, dsf_name, maptype
        )


cache_manager = CacheManager()

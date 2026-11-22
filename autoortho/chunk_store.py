"""Packed chunk store: imagery chunks in SQLite instead of one file each.

Performance plan, item 3. Every downloaded chunk used to be its own file,
``{col}_{row}_{zoom}_{maptype}.jpg``, all in one folder (close to a million on
a well-used PC). Each write cost a temp file and a rename, mostly kernel time,
before the antivirus looked at the new file; each texture read was 16 to 1,024
opens; NTFS rounded every file up to whole clusters; and cache maintenance
walked a folder of a million entries.

Layout
------
``<cache_dir>/chunks/<band>.sqlite``, one database per 10 degree square, the
same bands the DDS cache folders use (``+60-010``). A band keeps each file to a
few gigabytes, lets eviction and a damaged file affect one region, and a flight
touches one to four of them. Each holds::

    chunks(id INTEGER PRIMARY KEY, status INTEGER, data BLOB, fetched INTEGER)
    maptypes(id INTEGER PRIMARY KEY, name TEXT UNIQUE)

``id`` packs the map type's number in the band, the zoom and the Morton code of
the chunk's column and row (``row_id``). A texture's chunks are an aligned
square, so they are one contiguous id range and reading them is one range scan
of the table itself.

The plan proposed ``PRIMARY KEY (maptype, z, m) WITHOUT ROWID`` with 16 KB
pages. Measured on 100,000 real chunks (9 KB on average), that layout spilled
every chunk above about 4 KB into a mostly empty overflow page: 1.46 GB on disk
for 0.9 GB of imagery. A WITHOUT ROWID table is an index b-tree, whose rows may
hold only about a quarter of a page before overflowing. A rowid table with 4 KB
pages packs overflow pages end to end: 1.09 times the imagery (see
``_PAGE_SIZE``).

``status`` holds what the row is (``STATUS_DATA``, ``STATUS_NO_IMAGERY``: the
provider said it has nothing there, kept so the sea is not asked for again)
and ``FLAG_KEEP`` for rows eviction must skip (the low-zoom base, kept
imagery). Triggers keep exact byte totals in ``meta``.

Writes are queued and committed by one writer thread in batches (up to
``batch_rows`` chunks or ``batch_seconds``, whichever comes first). A queued
chunk is readable from the pending map until its batch commits.

The store is safe to share between processes (WAL journal, busy timeout): on
macOS each mount runs its own worker. It is not meant for network shares
(SQLite's WAL needs shared memory), which is why the long-term cache, often on
a NAS, stays one file per chunk.

Path API
--------
Much of AutoOrtho names chunks by their old file path. ``read_chunk_path``,
``read_chunk_paths``, ``chunk_path_exists``, ``write_chunk_path`` and
``remove_chunk_path`` take such a path and use the store when it is enabled
(``configure``), else the file. While a folder still holds files of the old
layout (``LEGACY_MARKER``), a miss reads the old file and moves it into the
store.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Callable, Iterable, Iterator, Optional

log = logging.getLogger(__name__)

STATUS_DATA = 1
STATUS_NO_IMAGERY = 2
_STATUS_KIND = 3
FLAG_KEEP = 4

STORE_DIRNAME = "chunks"
# Present while ``<cache_dir>`` may still hold chunk files of the old layout.
LEGACY_MARKER = "legacy-files-pending"

# 4 KB pages: measured on 30,000 real chunks (9 KB on average), the files took
# 1.09x the imagery (8 KB: 1.16x, 16 KB: 1.45x, 64 KB: 1.21x; one file per chunk
# on NTFS about 1.4x) and a 256-chunk texture read in 2.5 ms (16 KB: 1.7 ms).
# Rows larger than a page go to overflow pages packed end to end.
_PAGE_SIZE = 4096
_BUSY_TIMEOUT_MS = 10_000
# Rows read again after this long have their ``fetched`` time refreshed, so
# eviction drops what has not been used, not what was downloaded first.
_TOUCH_AFTER_SECONDS = 6 * 3600
# A provider's "no imagery here" is trusted this long, then asked again:
# providers add coverage.
NO_IMAGERY_TTL_SECONDS = 30 * 24 * 3600
_IN_BATCH = 400
_BAND_ZOOM = 12

# Row id layout: | maptype number (10 bits) | zoom (5 bits) | Morton (48 bits) |
_MORTON_BITS = 48
_ZOOM_BITS = 5
_MAPTYPE_BITS = 10
_MORTON_MASK = (1 << _MORTON_BITS) - 1
_ZOOM_MASK = (1 << _ZOOM_BITS) - 1
_MAX_ZOOM = 24

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS chunks ("
    " id INTEGER PRIMARY KEY,"
    " status INTEGER NOT NULL,"
    " data BLOB,"
    " fetched INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS chunks_fetched ON chunks(fetched)",
    "CREATE TABLE IF NOT EXISTS maptypes (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
    # Exact byte totals without reading every row: the disk budget asks for
    # them at start, and a band can hold gigabytes.
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_bytes', 0), ('kept_bytes', 0)",
    "CREATE TRIGGER IF NOT EXISTS chunks_bytes_insert AFTER INSERT ON chunks BEGIN"
    " UPDATE meta SET value = value + COALESCE(length(new.data), 0) WHERE key = 'data_bytes';"
    " UPDATE meta SET value = value + COALESCE(length(new.data), 0)"
    "  WHERE key = 'kept_bytes' AND (new.status & 4) != 0;"
    " END",
    "CREATE TRIGGER IF NOT EXISTS chunks_bytes_delete AFTER DELETE ON chunks BEGIN"
    " UPDATE meta SET value = value - COALESCE(length(old.data), 0) WHERE key = 'data_bytes';"
    " UPDATE meta SET value = value - COALESCE(length(old.data), 0)"
    "  WHERE key = 'kept_bytes' AND (old.status & 4) != 0;"
    " END",
    "CREATE TRIGGER IF NOT EXISTS chunks_bytes_update AFTER UPDATE OF data, status ON chunks BEGIN"
    " UPDATE meta SET value = value - COALESCE(length(old.data), 0)"
    "  + COALESCE(length(new.data), 0) WHERE key = 'data_bytes';"
    " UPDATE meta SET value = value"
    "  - (CASE WHEN (old.status & 4) != 0 THEN COALESCE(length(old.data), 0) ELSE 0 END)"
    "  + (CASE WHEN (new.status & 4) != 0 THEN COALESCE(length(new.data), 0) ELSE 0 END)"
    "  WHERE key = 'kept_bytes';"
    " END",
)

_UPSERT = (
    "INSERT INTO chunks(id, status, data, fetched) VALUES (?, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET"
    " status = (excluded.status & 3) | ((chunks.status | excluded.status) & 4),"
    " data = excluded.data,"
    " fetched = excluded.fetched"
)


# ---------------------------------------------------------------- key helpers


def _spread(value: int) -> int:
    """Spread the low 32 bits of ``value`` to the even bits of a 64-bit int."""
    value &= 0xFFFFFFFF
    value = (value | (value << 16)) & 0x0000FFFF0000FFFF
    value = (value | (value << 8)) & 0x00FF00FF00FF00FF
    value = (value | (value << 4)) & 0x0F0F0F0F0F0F0F0F
    value = (value | (value << 2)) & 0x3333333333333333
    value = (value | (value << 1)) & 0x5555555555555555
    return value


def _compact(value: int) -> int:
    value &= 0x5555555555555555
    value = (value | (value >> 1)) & 0x3333333333333333
    value = (value | (value >> 2)) & 0x0F0F0F0F0F0F0F0F
    value = (value | (value >> 4)) & 0x00FF00FF00FF00FF
    value = (value | (value >> 8)) & 0x0000FFFF0000FFFF
    value = (value | (value >> 16)) & 0x00000000FFFFFFFF
    return value


def morton(col: int, row: int) -> int:
    """Morton (Z-order) code of a chunk: column bits even, row bits odd."""
    return _spread(int(col)) | (_spread(int(row)) << 1)


def unmorton(code: int) -> tuple[int, int]:
    return _compact(code), _compact(code >> 1)


def row_id(maptype_number: int, zoom: int, col: int, row: int) -> int:
    return (
        (int(maptype_number) << (_MORTON_BITS + _ZOOM_BITS))
        | ((int(zoom) & _ZOOM_MASK) << _MORTON_BITS)
        | (morton(col, row) & _MORTON_MASK)
    )


def split_row_id(value: int) -> tuple[int, int, int, int]:
    """``row_id`` -> ``(maptype_number, zoom, col, row)``."""
    value = int(value)
    col, row = unmorton(value & _MORTON_MASK)
    return (
        value >> (_MORTON_BITS + _ZOOM_BITS),
        (value >> _MORTON_BITS) & _ZOOM_MASK,
        col,
        row,
    )


@lru_cache(maxsize=65_536)
def _band_of_zl12(col12: int, row12: int, zoom: int) -> str:
    n = 2 ** zoom
    lon = col12 / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row12 / n))))
    lat_band = int(math.floor(lat / 10) * 10)
    lon_band = int(math.floor(lon / 10) * 10)
    return f"{lat_band:+03d}{lon_band:+04d}"


def band_for(col: int, row: int, zoom: int) -> str:
    """The 10 degree band a chunk is stored in.

    Taken from the chunk's ZL12 ancestor, so all the chunks of one texture
    (a ZL12 tile holds whole textures at every usual zoom) share a database.
    """
    zoom = int(zoom)
    if zoom > _BAND_ZOOM:
        shift = zoom - _BAND_ZOOM
        return _band_of_zl12(int(col) >> shift, int(row) >> shift, _BAND_ZOOM)
    return _band_of_zl12(int(col), int(row), zoom)


def chunk_file_name(col: int, row: int, zoom: int, maptype: str) -> str:
    """File name of a chunk in the old one-file-per-chunk layout."""
    return f"{int(col)}_{int(row)}_{int(zoom)}_{maptype}.jpg"


def parse_chunk_name(name: str) -> Optional[tuple[int, int, int, str]]:
    """``"{col}_{row}_{zoom}_{maptype}.jpg"`` -> ``(col, row, zoom, maptype)``."""
    base = os.path.basename(str(name))
    if not base.endswith(".jpg") or base.startswith("."):
        return None
    parts = base[:-4].split("_")
    if len(parts) != 4:
        return None
    col, row, zoom, maptype = parts
    if not (col.isdigit() and row.isdigit() and zoom.isdigit()) or not maptype:
        return None
    return int(col), int(row), int(zoom), maptype


def is_jpeg(data) -> bool:
    return (
        bool(data)
        and len(data) >= 3
        and data[0] == 0xFF
        and data[1] == 0xD8
        and data[2] == 0xFF
    )


def _is_corruption(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(
        exc, sqlite3.OperationalError
    ):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("malformed", "not a database", "file is encrypted", "disk image", "corrupt")
    )


# ------------------------------------------------------------------- the band


class _Band:
    """One band's database: a small pool of connections, opened lazily.

    A damaged file is set aside and recreated once no connection uses it, so
    a corrupt band costs misses, never an error to the caller.
    """

    def __init__(self, store: "ChunkStore", name: str):
        self.store = store
        self.name = name
        self.path = os.path.join(store.directory, f"{name}.sqlite")
        self._lock = threading.Lock()
        self._idle: list[sqlite3.Connection] = []
        self._in_use = 0
        self._broken = False
        self._initialized = False
        self._maptypes: dict[str, int] = {}
        self._maptype_names: dict[int, str] = {}
        self.recreated = 0

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(self.store.directory, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            if not self._initialized:
                # Page size and auto-vacuum only take effect on a new file,
                # before the journal is switched to WAL.
                conn.execute(f"PRAGMA page_size={_PAGE_SIZE}")
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("PRAGMA journal_mode=WAL")
                for statement in _SCHEMA:
                    conn.execute(statement)
                self._initialized = True
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
        except BaseException:
            # A connection left open keeps a damaged file from being moved.
            conn.close()
            raise
        return conn

    def checkout(self) -> Optional[sqlite3.Connection]:
        with self._lock:
            if self._broken:
                return None
            self._in_use += 1
            conn = self._idle.pop() if self._idle else None
        if conn is not None:
            return conn
        try:
            return self._connect()
        except sqlite3.Error as exc:
            self.checkin(None, exc)
            return None

    def checkin(self, conn, error: Optional[BaseException] = None) -> None:
        to_close = []
        quarantine = False
        with self._lock:
            self._in_use = max(0, self._in_use - 1)
            if error is not None and _is_corruption(error):
                if not self._broken:
                    log.warning(
                        "Chunk store band %s is damaged (%s); recreating it",
                        self.name,
                        error,
                    )
                self._broken = True
            if conn is not None:
                if self._broken:
                    to_close.append(conn)
                else:
                    self._idle.append(conn)
            if self._broken and self._in_use == 0:
                to_close.extend(self._idle)
                self._idle.clear()
                quarantine = True
        for item in to_close:
            try:
                item.close()
            except sqlite3.Error:
                pass
        if quarantine:
            self._set_aside()

    def _set_aside(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        moved = True
        for suffix in ("", "-wal", "-shm"):
            source = self.path + suffix
            if not os.path.exists(source):
                continue
            try:
                if suffix:
                    os.remove(source)
                else:
                    os.replace(source, f"{self.path}.corrupt-{stamp}")
            except OSError as exc:
                moved = False
                log.warning("Could not set aside damaged %s: %s", source, exc)
        with self._lock:
            if moved:
                self._broken = False
                self._initialized = False
                self._maptypes.clear()
                self._maptype_names.clear()
                self.recreated += 1

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def run(self, function: Callable[[sqlite3.Connection], object], default=None):
        """Run ``function(conn)``; a database error costs ``default``."""
        conn = self.checkout()
        if conn is None:
            return default
        error = None
        try:
            return function(conn)
        except sqlite3.Error as exc:
            error = exc
            if not _is_corruption(exc):
                log.debug("Chunk store band %s: %s", self.name, exc)
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return default
        finally:
            self.checkin(conn, error)

    # Map types are numbered per band so the row id stays one integer.

    def _load_maptypes(self, conn) -> None:
        for number, name in conn.execute("SELECT id, name FROM maptypes"):
            self._maptypes[str(name)] = int(number)
            self._maptype_names[int(number)] = str(name)

    def maptype_number(self, conn, name: str, create: bool) -> Optional[int]:
        name = str(name)
        number = self._maptypes.get(name)
        if number is not None:
            return number
        self._load_maptypes(conn)
        number = self._maptypes.get(name)
        if number is not None or not create:
            return number
        conn.execute("INSERT OR IGNORE INTO maptypes(name) VALUES (?)", (name,))
        self._load_maptypes(conn)
        number = self._maptypes.get(name)
        if number is not None and number >= (1 << _MAPTYPE_BITS):
            raise sqlite3.DataError("too many map types in one band")
        return number

    def maptype_name(self, conn, number: int) -> Optional[str]:
        name = self._maptype_names.get(int(number))
        if name is None:
            self._load_maptypes(conn)
            name = self._maptype_names.get(int(number))
        return name


# ------------------------------------------------------------------ the store


class _Write:
    __slots__ = ("key", "status", "data", "fetched", "on_commit", "touch_only")

    def __init__(self, key, status, data, fetched, on_commit=None, touch_only=False):
        self.key = key
        self.status = status
        self.data = data
        self.fetched = fetched
        self.on_commit = on_commit
        self.touch_only = touch_only


class ChunkStore:
    """Imagery chunks of one cache folder, packed in per-band SQLite files."""

    def __init__(
        self,
        cache_dir: str,
        *,
        batch_rows: int = 64,
        batch_seconds: float = 0.1,
        max_pending_bytes: int = 256 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ):
        self.cache_dir = str(cache_dir)
        self.directory = os.path.join(self.cache_dir, STORE_DIRNAME)
        self.batch_rows = max(1, int(batch_rows))
        self.batch_seconds = max(0.001, float(batch_seconds))
        self.max_pending_bytes = max(1, int(max_pending_bytes))
        self._clock = clock
        self._bands: dict[str, _Band] = {}
        self._bands_lock = threading.Lock()
        # key -> _Write waiting for its batch; reads see it.
        self._pending: dict[tuple, _Write] = {}
        self._pending_bytes = 0
        self._pending_lock = threading.Lock()
        self._queue: "queue.Queue[Optional[_Write]]" = queue.Queue()
        self._writer: Optional[threading.Thread] = None
        self._writer_lock = threading.Lock()
        self._closed = False
        # Recently touched keys, so a hot chunk is not touched on every read.
        self._touched: OrderedDict = OrderedDict()
        self._legacy_state = (0.0, False)
        self.stats = {
            "rows_written": 0,
            "batches": 0,
            "writes_dropped": 0,
            "reads": 0,
            "hits": 0,
            "legacy_imported": 0,
        }
        os.makedirs(self.directory, exist_ok=True)

    # ----------------------------------------------------------- plumbing

    def _band(self, name: str) -> _Band:
        band = self._bands.get(name)
        if band is not None:
            return band
        with self._bands_lock:
            band = self._bands.get(name)
            if band is None:
                band = _Band(self, name)
                self._bands[name] = band
            return band

    def _existing_bands(self) -> list[_Band]:
        try:
            names = [
                entry.name[: -len(".sqlite")]
                for entry in os.scandir(self.directory)
                if entry.name.endswith(".sqlite") and entry.is_file()
            ]
        except OSError:
            names = []
        for name in names:
            self._band(name)
        with self._bands_lock:
            return list(self._bands.values())

    @staticmethod
    def _key(col, row, zoom, maptype) -> tuple:
        zoom = int(zoom)
        if not 0 <= zoom <= _MAX_ZOOM:
            raise ValueError(f"zoom {zoom} outside 0..{_MAX_ZOOM}")
        return (str(maptype), zoom, int(col), int(row))

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        with self._writer_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(
                target=self._write_loop, name="ChunkStoreWriter", daemon=True
            )
            self._writer.start()

    # -------------------------------------------------------------- reads

    def get(self, col: int, row: int, zoom: int, maptype: str) -> Optional[bytes]:
        """The chunk's bytes, or None (not stored, or stored without data)."""
        return self.get_many(maptype, zoom, [(col, row)]).get((int(col), int(row)))

    def status(self, col: int, row: int, zoom: int, maptype: str) -> int:
        """``STATUS_DATA``, ``STATUS_NO_IMAGERY`` (while trusted) or 0."""
        seen: set = set()
        if self.get_many(maptype, zoom, [(col, row)], no_imagery=seen):
            return STATUS_DATA
        return STATUS_NO_IMAGERY if seen else 0

    def get_many(
        self,
        maptype: str,
        zoom: int,
        keys: Iterable[tuple[int, int]],
        *,
        no_imagery: Optional[set] = None,
    ) -> dict[tuple[int, int], bytes]:
        """Bytes of the stored chunks among ``keys`` ((col, row) pairs).

        A texture's chunks are one aligned square, so a band's keys usually
        form one id range and are read with a single range scan.
        ``no_imagery``, when given, receives the keys stored as having none
        (within ``NO_IMAGERY_TTL_SECONDS``).
        """
        maptype = str(maptype)
        zoom = int(zoom)
        wanted = {(int(col), int(row)) for col, row in keys}
        if not wanted:
            return {}
        self.stats["reads"] += len(wanted)
        found: dict[tuple[int, int], bytes] = {}
        with self._pending_lock:
            if self._pending:
                for col, row in list(wanted):
                    pending = self._pending.get((maptype, zoom, col, row))
                    if pending is None or pending.touch_only:
                        continue
                    if pending.status & _STATUS_KIND == STATUS_DATA:
                        found[(col, row)] = pending.data
                    elif no_imagery is not None:
                        no_imagery.add((col, row))
                    wanted.discard((col, row))
        by_band: dict[str, list[tuple[int, int]]] = {}
        for col, row in wanted:
            by_band.setdefault(band_for(col, row, zoom), []).append((col, row))
        now = int(self._clock())
        stale_before = now - _TOUCH_AFTER_SECONDS
        trusted_since = now - NO_IMAGERY_TTL_SECONDS
        to_touch = []
        for band_name, positions in by_band.items():
            band = self._band(band_name)

            def query(conn, band=band, positions=positions):
                number = band.maptype_number(conn, maptype, create=False)
                if number is None:
                    return {}, []
                ids = {row_id(number, zoom, c, r): (c, r) for c, r in positions}
                low, high = min(ids), max(ids)
                if high - low + 1 <= 4 * len(ids):
                    rows = conn.execute(
                        "SELECT id, status, data, fetched FROM chunks"
                        " WHERE id BETWEEN ? AND ?",
                        (low, high),
                    ).fetchall()
                else:
                    rows = []
                    ordered = sorted(ids)
                    for start in range(0, len(ordered), _IN_BATCH):
                        part = ordered[start : start + _IN_BATCH]
                        marks = ",".join("?" * len(part))
                        rows.extend(
                            conn.execute(
                                "SELECT id, status, data, fetched FROM chunks"
                                f" WHERE id IN ({marks})",
                                part,
                            ).fetchall()
                        )
                return ids, rows

            ids, rows = band.run(query, ({}, []))
            for identifier, status, data, fetched in rows:
                position = ids.get(identifier)
                if position is None:
                    continue
                kind = int(status) & _STATUS_KIND
                if kind == STATUS_DATA and data:
                    found[position] = bytes(data)
                    if int(fetched) < stale_before:
                        to_touch.append(position)
                elif (
                    kind == STATUS_NO_IMAGERY
                    and no_imagery is not None
                    and int(fetched) >= trusted_since
                ):
                    no_imagery.add(position)
        self.stats["hits"] += len(found)
        for col, row in to_touch:
            self._touch(col, row, zoom, maptype, now)
        return found

    def contains_many(
        self, maptype: str, zoom: int, keys: Iterable[tuple[int, int]]
    ) -> set[tuple[int, int]]:
        """The keys among ``keys`` stored with data, without reading the data."""
        maptype = str(maptype)
        zoom = int(zoom)
        wanted = {(int(col), int(row)) for col, row in keys}
        present = set()
        with self._pending_lock:
            for col, row in list(wanted):
                pending = self._pending.get((maptype, zoom, col, row))
                if pending is None or pending.touch_only:
                    continue
                if pending.status & _STATUS_KIND == STATUS_DATA:
                    present.add((col, row))
                wanted.discard((col, row))
        by_band: dict[str, list[tuple[int, int]]] = {}
        for col, row in wanted:
            by_band.setdefault(band_for(col, row, zoom), []).append((col, row))
        for band_name, positions in by_band.items():
            band = self._band(band_name)

            def query(conn, band=band, positions=positions):
                number = band.maptype_number(conn, maptype, create=False)
                if number is None:
                    return {}, []
                ids = {row_id(number, zoom, c, r): (c, r) for c, r in positions}
                return ids, conn.execute(
                    "SELECT id FROM chunks WHERE id BETWEEN ? AND ? AND (status & 3) = 1",
                    (min(ids), max(ids)),
                ).fetchall()

            ids, rows = band.run(query, ({}, []))
            for (identifier,) in rows:
                position = ids.get(identifier)
                if position is not None:
                    present.add(position)
        return present

    def iter_chunks(
        self, maptype: Optional[str] = None, zoom: Optional[int] = None
    ) -> Iterator[tuple[int, int, int, str, int, int, int]]:
        """Yield ``(col, row, zoom, maptype, status, size, fetched)`` for every row."""
        self.flush()
        for band in self._existing_bands():

            def query(conn, band=band):
                rows = conn.execute(
                    "SELECT id, status, length(data), fetched FROM chunks"
                ).fetchall()
                band._load_maptypes(conn)
                return rows

            rows = band.run(query, [])
            names = dict(band._maptype_names)
            for identifier, status, size, fetched in rows:
                number, z, col, row = split_row_id(identifier)
                name = names.get(number)
                if name is None:
                    continue
                if maptype is not None and name != maptype:
                    continue
                if zoom is not None and z != int(zoom):
                    continue
                yield col, row, z, name, int(status), int(size or 0), int(fetched)

    # ------------------------------------------------------------- writes

    def put(
        self,
        col: int,
        row: int,
        zoom: int,
        maptype: str,
        data: bytes,
        *,
        keep: bool = False,
        on_commit: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Queue a chunk for the next batch. False when the queue is full."""
        if not data:
            return False
        status = STATUS_DATA | (FLAG_KEEP if keep else 0)
        return self._enqueue(
            _Write(
                self._key(col, row, zoom, maptype),
                status,
                bytes(data),
                int(self._clock()),
                on_commit,
            )
        )

    def put_many(self, rows: Iterable[tuple]) -> int:
        """Queue ``(col, row, zoom, maptype, data[, keep])`` rows; returns how many."""
        queued = 0
        for item in rows:
            col, row, zoom, maptype, data = item[:5]
            keep = bool(item[5]) if len(item) > 5 else False
            if self.put(col, row, zoom, maptype, data, keep=keep):
                queued += 1
        return queued

    def mark_no_imagery(self, col: int, row: int, zoom: int, maptype: str) -> bool:
        """Remember that the provider has no imagery for this chunk."""
        return self._enqueue(
            _Write(
                self._key(col, row, zoom, maptype),
                STATUS_NO_IMAGERY,
                None,
                int(self._clock()),
            )
        )

    def _touch(self, col, row, zoom, maptype, now) -> None:
        key = self._key(col, row, zoom, maptype)
        with self._pending_lock:
            if self._closed or key in self._touched or key in self._pending:
                return
            self._touched[key] = True
            while len(self._touched) > 8192:
                self._touched.popitem(last=False)
        self._queue.put(_Write(key, 0, None, now, touch_only=True))
        self._ensure_writer()

    def _enqueue(self, write: _Write) -> bool:
        size = len(write.data or b"")
        with self._pending_lock:
            if self._closed:
                return False
            previous = self._pending.get(write.key)
            previous_size = len(previous.data or b"") if previous is not None else 0
            if size and self._pending_bytes - previous_size + size > self.max_pending_bytes:
                self.stats["writes_dropped"] += 1
                return False
            self._pending[write.key] = write
            self._pending_bytes += size - previous_size
        self._queue.put(write)
        self._ensure_writer()
        return True

    def _update_rows(self, maptype, zoom, keys, sql_for_ids) -> int:
        """Run ``sql_for_ids(conn, ids)`` per band; returns rows changed."""
        maptype = str(maptype)
        zoom = int(zoom)
        by_band: dict[str, list[tuple[int, int]]] = {}
        for col, row in keys:
            by_band.setdefault(band_for(col, row, zoom), []).append((int(col), int(row)))
        changed = 0
        for band_name, positions in by_band.items():
            band = self._band(band_name)

            def apply(conn, band=band, positions=positions):
                number = band.maptype_number(conn, maptype, create=False)
                if number is None:
                    return 0
                ids = sorted(row_id(number, zoom, c, r) for c, r in positions)
                count = 0
                conn.execute("BEGIN IMMEDIATE")
                for start in range(0, len(ids), _IN_BATCH):
                    count += sql_for_ids(conn, ids[start : start + _IN_BATCH])
                conn.execute("COMMIT")
                return count

            changed += int(band.run(apply, 0) or 0)
        return changed

    def delete_many(
        self,
        maptype: str,
        zoom: int,
        keys: Iterable[tuple[int, int]],
        *,
        return_bytes: bool = False,
    ):
        """Remove stored chunks; returns the number of rows deleted.

        With ``return_bytes``, ``(rows, bytes of imagery freed)``.
        """
        keys = [(int(c), int(r)) for c, r in keys]
        freed = [0]
        with self._pending_lock:
            for col, row in keys:
                pending = self._pending.pop((str(maptype), int(zoom), col, row), None)
                if pending is not None:
                    self._pending_bytes -= len(pending.data or b"")

        def remove(conn, ids):
            marks = ",".join("?" * len(ids))
            if return_bytes:
                freed[0] += int(
                    conn.execute(
                        f"SELECT COALESCE(SUM(length(data)), 0) FROM chunks WHERE id IN ({marks})",
                        ids,
                    ).fetchone()[0]
                    or 0
                )
            return conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", ids).rowcount

        count = self._update_rows(maptype, zoom, keys, remove)
        return (count, freed[0]) if return_bytes else count

    def delete(self, col: int, row: int, zoom: int, maptype: str) -> bool:
        return self.delete_many(maptype, zoom, [(col, row)]) > 0

    def set_keep(
        self, maptype: str, zoom: int, keys: Iterable[tuple[int, int]], keep: bool = True
    ) -> int:
        """Mark stored rows as kept (skipped by eviction), or clear the mark."""
        self.flush()
        expression = "status | 4" if keep else "status & ~4"

        def update(conn, ids):
            marks = ",".join("?" * len(ids))
            return conn.execute(
                f"UPDATE chunks SET status = {expression} WHERE id IN ({marks})", ids
            ).rowcount

        return self._update_rows(maptype, zoom, keys, update)

    def clear_keep_where(self, predicate: Callable[[int, int, int, str], bool]) -> int:
        """Clear the keep mark of kept rows for which ``predicate`` is true."""
        groups: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for col, row, z, name, status, _size, _fetched in self.iter_chunks():
            if status & FLAG_KEEP and predicate(col, row, z, name):
                groups.setdefault((name, z), []).append((col, row))
        return sum(
            self.set_keep(name, z, keys, keep=False) for (name, z), keys in groups.items()
        )

    # ----------------------------------------------------------- the writer

    def _write_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=1.0)
            except queue.Empty:
                with self._pending_lock:
                    if self._closed:
                        return
                continue
            if first is None:
                self._queue.task_done()
                return
            batch = [first]
            deadline = time.monotonic() + self.batch_seconds
            stop = False
            while len(batch) < self.batch_rows:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is None:
                    self._queue.task_done()
                    stop = True
                    break
                batch.append(item)
            try:
                self._commit(batch)
            except Exception:
                log.exception("Chunk store batch commit failed")
            finally:
                for _ in batch:
                    self._queue.task_done()
            if stop:
                return

    def _commit(self, batch: list[_Write]) -> None:
        by_band: dict[str, list[_Write]] = {}
        for write in batch:
            _maptype, zoom, col, row = write.key
            by_band.setdefault(band_for(col, row, zoom), []).append(write)
        committed: list[_Write] = []
        for band_name, writes in by_band.items():
            band = self._band(band_name)

            def apply(conn, band=band, writes=writes):
                conn.execute("BEGIN IMMEDIATE")
                rows = []
                touches = []
                for write in writes:
                    maptype, zoom, col, row = write.key
                    number = band.maptype_number(
                        conn, maptype, create=not write.touch_only
                    )
                    if number is None:
                        continue
                    identifier = row_id(number, zoom, col, row)
                    if write.touch_only:
                        touches.append((write.fetched, identifier))
                    else:
                        rows.append(
                            (
                                identifier,
                                write.status,
                                sqlite3.Binary(write.data) if write.data else None,
                                write.fetched,
                            )
                        )
                if rows:
                    conn.executemany(_UPSERT, rows)
                if touches:
                    conn.executemany("UPDATE chunks SET fetched=? WHERE id=?", touches)
                conn.execute("COMMIT")
                return len(rows)

            written = band.run(apply, None)
            if written is not None:
                committed.extend(writes)
                self.stats["rows_written"] += written
        self.stats["batches"] += 1
        # Committed or not (a damaged band), the pending entries go: a write
        # that could not land is a cache miss later, never stale data.
        with self._pending_lock:
            for write in batch:
                if write.touch_only:
                    self._touched.pop(write.key, None)
                    continue
                if self._pending.get(write.key) is write:
                    del self._pending[write.key]
                    self._pending_bytes -= len(write.data or b"")
        for write in committed:
            if write.on_commit is not None:
                try:
                    write.on_commit()
                except Exception:
                    log.debug("Chunk store commit callback failed", exc_info=True)

    def flush(self, timeout: float = 30.0) -> bool:
        """Wait until every queued write has been committed."""
        if self._writer is None:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    @property
    def closed(self) -> bool:
        return self._closed

    # ----------------------------------------------------------- maintenance

    def size_bytes(self) -> int:
        """Bytes the store's files take on disk."""
        total = 0
        try:
            for entry in os.scandir(self.directory):
                if entry.name.endswith((".sqlite", ".sqlite-wal")) and entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
        except OSError:
            return 0
        return total

    def _meta_total(self, key: str) -> int:
        total = 0
        for band in self._existing_bands():
            total += int(
                band.run(
                    lambda conn: (
                        conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                        or (0,)
                    )[0],
                    0,
                )
                or 0
            )
        return total

    def data_bytes(self) -> int:
        """Bytes of imagery held, kept rows included."""
        self.flush()
        return self._meta_total("data_bytes")

    def kept_bytes(self) -> int:
        """Bytes of imagery in kept rows (outside the budget)."""
        self.flush()
        return self._meta_total("kept_bytes")

    def evictable_bytes(self) -> int:
        """Bytes of imagery the disk budget counts: everything not kept."""
        self.flush()
        return max(0, self._meta_total("data_bytes") - self._meta_total("kept_bytes"))

    def evict_oldest(
        self,
        nbytes: int,
        *,
        batch: int = 2000,
        protect: Optional[Callable[[int, int, int, str], bool]] = None,
    ) -> int:
        """Drop the least recently used rows until ``nbytes`` of imagery is freed.

        Kept rows (``FLAG_KEEP``) are skipped. ``protect(col, row, zoom,
        maptype)``, when given, names more rows to keep: they are marked kept
        on the way, so the next eviction skips them in SQL. Returns the bytes
        freed.
        """
        self.flush()
        target = max(0, int(nbytes))
        freed = 0
        bands = self._existing_bands()
        while freed < target and bands:
            heads = []
            for band in bands:
                oldest = band.run(
                    lambda conn: conn.execute(
                        "SELECT MIN(fetched) FROM chunks WHERE (status & 4) = 0"
                    ).fetchone()[0],
                    None,
                )
                if oldest is not None:
                    heads.append((int(oldest), band))
            if not heads:
                break
            heads.sort(key=lambda item: item[0])
            band = heads[0][1]

            def evict(conn, band=band):
                rows = conn.execute(
                    "SELECT id, length(data) FROM chunks"
                    " WHERE (status & 4) = 0 ORDER BY fetched LIMIT ?",
                    (batch,),
                ).fetchall()
                if not rows:
                    return 0, 0
                band._load_maptypes(conn)
                gained = 0
                victims = []
                kept = []
                for identifier, size in rows:
                    if protect is not None:
                        number, z, col, row = split_row_id(identifier)
                        name = band._maptype_names.get(number, "")
                        if protect(col, row, z, name):
                            kept.append((identifier,))
                            continue
                    victims.append((identifier,))
                    gained += int(size or 0)
                    if freed + gained >= target:
                        break
                conn.execute("BEGIN IMMEDIATE")
                if kept:
                    conn.executemany("UPDATE chunks SET status = status | 4 WHERE id=?", kept)
                if victims:
                    conn.executemany("DELETE FROM chunks WHERE id=?", victims)
                conn.execute("COMMIT")
                if victims:
                    conn.execute("PRAGMA incremental_vacuum")
                return gained, len(victims) + len(kept)

            gained, count = band.run(evict, (0, 0))
            if not count:
                bands.remove(band)
                continue
            freed += gained
        return freed

    def clear(self) -> None:
        """Delete every row (the user cleared the cache)."""
        self.flush()
        with self._pending_lock:
            self._pending.clear()
            self._pending_bytes = 0
        for band in self._existing_bands():

            def wipe(conn):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM chunks")
                conn.execute("COMMIT")
                conn.execute("PRAGMA incremental_vacuum")
                return True

            band.run(wipe, False)

    def close(self) -> None:
        with self._pending_lock:
            self._closed = True
        writer = self._writer
        if writer is not None and writer.is_alive():
            self._queue.put(None)
            writer.join(timeout=30.0)
        with self._bands_lock:
            bands = list(self._bands.values())
        for band in bands:
            band.close()

    # ------------------------------------------------------------ migration

    @property
    def legacy_marker_path(self) -> str:
        return os.path.join(self.directory, LEGACY_MARKER)

    def legacy_pending(self, max_age: float = 30.0) -> bool:
        """Whether old-layout files may remain (checked at most every ``max_age`` s)."""
        checked, pending = self._legacy_state
        now = time.monotonic()
        if checked and now - checked < max_age:
            return pending
        pending = os.path.exists(self.legacy_marker_path)
        self._legacy_state = (now, pending)
        return pending

    def set_legacy_pending(self, pending: bool) -> None:
        path = self.legacy_marker_path
        try:
            if pending:
                with open(path, "a", encoding="utf-8"):
                    pass
            elif os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log.debug("Could not update %s: %s", path, exc)
        self._legacy_state = (time.monotonic(), bool(pending))

    def import_legacy(
        self, path: str, data: bytes, *, keep: bool = False
    ) -> bool:
        """Move an old-layout file into the store; the file goes once committed."""
        parsed = parse_chunk_name(path)
        if parsed is None or not is_jpeg(data):
            return False
        col, row, zoom, maptype = parsed

        def remove_file():
            try:
                os.remove(path)
            except OSError:
                pass

        if self.put(col, row, zoom, maptype, data, keep=keep, on_commit=remove_file):
            self.stats["legacy_imported"] += 1
            return True
        return False


# ----------------------------------------------------------- shared instances

_enabled = False
_stores: dict[str, ChunkStore] = {}
_stores_lock = threading.Lock()
_store_kwargs: dict = {}
# Whether imported legacy chunks are kept: set by the worker from the
# low-zoom base and kept-imagery predicates (name -> bool).
_legacy_keep: Optional[Callable[[str], bool]] = None


def configure(enabled: bool, **store_kwargs) -> None:
    """Turn the store on or off for this process (``[cache] chunk_store``)."""
    global _enabled
    _enabled = bool(enabled)
    _store_kwargs.update(store_kwargs)


def enabled() -> bool:
    return _enabled


def configure_from_config(cfg) -> bool:
    """``configure`` from a loaded AutoOrtho config (``[cache] chunk_store``)."""
    value = getattr(getattr(cfg, "cache", None), "chunk_store", True)
    if isinstance(value, str):
        value = value.strip().lower() in ("true", "1", "yes", "on")
    configure(bool(value))
    return bool(value)


def set_legacy_keep(predicate: Optional[Callable[[str], bool]]) -> None:
    global _legacy_keep
    _legacy_keep = predicate


def _normalize(cache_dir: str) -> str:
    return os.path.normcase(os.path.abspath(str(cache_dir)))


def open_store(cache_dir: str, **kwargs) -> ChunkStore:
    """The process's store for ``cache_dir``, created on first use.

    A new store whose folder still holds chunk files of the old layout gets
    the legacy marker, so misses read those files through until they are
    converted.
    """
    key = _normalize(cache_dir)
    store = _stores.get(key)
    if store is not None:
        return store
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            new_folder = not os.path.isdir(os.path.join(str(cache_dir), STORE_DIRNAME))
            options = dict(_store_kwargs)
            options.update(kwargs)
            store = ChunkStore(cache_dir, **options)
            if new_folder and folder_has_legacy_chunks(cache_dir):
                store.set_legacy_pending(True)
            _stores[key] = store
        return store


def store_for(cache_dir: Optional[str]) -> Optional[ChunkStore]:
    """The store for ``cache_dir`` when the store is enabled, else None."""
    if not _enabled or not cache_dir:
        return None
    return open_store(cache_dir)


def close_all() -> None:
    with _stores_lock:
        stores = list(_stores.values())
        _stores.clear()
    for store in stores:
        try:
            store.close()
        except Exception:
            log.debug("Closing chunk store failed", exc_info=True)


def flush_all(timeout: float = 30.0) -> None:
    for store in list(_stores.values()):
        store.flush(timeout)


def folder_has_legacy_chunks(cache_dir: str) -> bool:
    """True when ``cache_dir`` holds at least one old-layout chunk file."""
    try:
        with os.scandir(cache_dir) as entries:
            for entry in entries:
                if parse_chunk_name(entry.name) is not None:
                    return True
    except OSError:
        return False
    return False


# -------------------------------------------------------------- the path API


def _read_file(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return data if is_jpeg(data) else None


def _store_and_key(path: str):
    parsed = parse_chunk_name(path)
    if parsed is None:
        return None, None
    return store_for(os.path.dirname(str(path))), parsed


def _keep_legacy(path: str) -> bool:
    predicate = _legacy_keep
    if predicate is None:
        return False
    try:
        return bool(predicate(os.path.basename(path)))
    except Exception:
        return False


def read_chunk_path(path: str) -> Optional[bytes]:
    """A chunk's bytes, by its old file path."""
    store, parsed = _store_and_key(path)
    if store is None:
        return _read_file(path)
    col, row, zoom, maptype = parsed
    data = store.get(col, row, zoom, maptype)
    if data:
        return data
    if store.legacy_pending():
        data = _read_file(path)
        if data:
            store.import_legacy(path, data, keep=_keep_legacy(path))
            return data
    return None


def read_chunk_paths(
    paths: Iterable[str],
    legacy_reader: Optional[Callable[[list], dict]] = None,
    no_imagery_out: Optional[set] = None,
) -> dict:
    """``{path: bytes}`` for the chunks among ``paths`` that are stored.

    Paths of one texture are read with one range scan per band. Misses fall
    back to ``legacy_reader(paths)`` (default: plain reads) while old-layout
    files remain, and what it finds moves into the store. ``no_imagery_out``
    receives the paths the provider is remembered to have nothing for.
    """
    paths = list(paths)
    result: dict = {}
    groups: dict[tuple, list[tuple[str, int, int]]] = {}
    plain: list[str] = []
    for path in paths:
        store, parsed = _store_and_key(path)
        if store is None:
            plain.append(path)
            continue
        col, row, zoom, maptype = parsed
        groups.setdefault((id(store), maptype, zoom), []).append((path, col, row))
    stores = {}
    for path in paths:
        store, _parsed = _store_and_key(path)
        if store is not None:
            stores[id(store)] = store
    legacy_misses: list[str] = []
    for (store_id, maptype, zoom), items in groups.items():
        store = stores[store_id]
        seen: set = set()
        found = store.get_many(
            maptype, zoom, [(col, row) for _path, col, row in items], no_imagery=seen
        )
        legacy = store.legacy_pending()
        for path, col, row in items:
            data = found.get((col, row))
            if data:
                result[path] = data
            elif (col, row) in seen:
                if no_imagery_out is not None:
                    no_imagery_out.add(path)
            elif legacy:
                legacy_misses.append(path)
    reader = legacy_reader or (
        lambda items: {p: d for p in items if (d := _read_file(p)) is not None}
    )
    if plain:
        result.update(reader(plain))
    if legacy_misses:
        recovered = reader(legacy_misses)
        for path, data in recovered.items():
            store, _parsed = _store_and_key(path)
            if store is not None:
                store.import_legacy(path, data, keep=_keep_legacy(path))
            result[path] = data
    return result


def lookup_chunk_path(path: str) -> tuple[Optional[bytes], bool]:
    """``(bytes or None, provider has no imagery)`` for a chunk, by its old path.

    One query answers both, so a miss costs no second look.
    """
    store, parsed = _store_and_key(path)
    if store is None:
        return _read_file(path), False
    col, row, zoom, maptype = parsed
    seen: set = set()
    data = store.get_many(maptype, zoom, [(col, row)], no_imagery=seen).get((col, row))
    if data:
        return data, False
    if seen:
        return None, True
    if store.legacy_pending():
        data = _read_file(path)
        if data:
            store.import_legacy(path, data, keep=_keep_legacy(path))
            return data, False
    return None, False


def chunk_path_exists(path: str) -> bool:
    store, parsed = _store_and_key(path)
    if store is None:
        return os.path.isfile(path)
    col, row, zoom, maptype = parsed
    if store.contains_many(maptype, zoom, [(col, row)]):
        return True
    return store.legacy_pending() and os.path.isfile(path)


def chunk_paths_present(paths: Iterable[str]) -> set:
    """The paths among ``paths`` whose chunk is stored (or still a file)."""
    paths = list(paths)
    present = set()
    groups: dict[tuple, list[tuple[str, int, int]]] = {}
    stores = {}
    for path in paths:
        store, parsed = _store_and_key(path)
        if store is None:
            if os.path.isfile(path):
                present.add(path)
            continue
        col, row, zoom, maptype = parsed
        stores[id(store)] = store
        groups.setdefault((id(store), maptype, zoom), []).append((path, col, row))
    for (store_id, maptype, zoom), items in groups.items():
        store = stores[store_id]
        found = store.contains_many(maptype, zoom, [(c, r) for _p, c, r in items])
        legacy = store.legacy_pending()
        for path, col, row in items:
            if (col, row) in found or (legacy and os.path.isfile(path)):
                present.add(path)
    return present


def write_chunk_path(
    path: str,
    data: bytes,
    *,
    keep: bool = False,
    file_writer: Optional[Callable[[str, bytes], bool]] = None,
) -> bool:
    """Store a chunk by its old file path (or write the file, store off)."""
    store, parsed = _store_and_key(path)
    if store is None:
        if file_writer is not None:
            return bool(file_writer(path, data))
        directory = os.path.dirname(str(path))
        try:
            os.makedirs(directory, exist_ok=True)
            temp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(temp, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
            return True
        except OSError:
            return False
    col, row, zoom, maptype = parsed
    return store.put(col, row, zoom, maptype, data, keep=keep)


def remove_chunk_path(path: str) -> None:
    store, parsed = _store_and_key(path)
    if store is not None:
        col, row, zoom, maptype = parsed
        store.delete(col, row, zoom, maptype)
    try:
        os.remove(path)
    except OSError:
        pass


def mark_no_imagery_path(path: str) -> bool:
    store, parsed = _store_and_key(path)
    if store is None:
        return False
    col, row, zoom, maptype = parsed
    return store.mark_no_imagery(col, row, zoom, maptype)


def no_imagery_paths(paths: Iterable[str]) -> set:
    """The paths among ``paths`` the provider is remembered to have nothing for."""
    found = set()
    groups: dict[tuple, list[tuple[str, int, int]]] = {}
    stores = {}
    for path in paths:
        store, parsed = _store_and_key(path)
        if store is None:
            continue
        col, row, zoom, maptype = parsed
        stores[id(store)] = store
        groups.setdefault((id(store), maptype, zoom), []).append((path, col, row))
    for (store_id, maptype, zoom), items in groups.items():
        seen: set = set()
        stores[store_id].get_many(
            maptype, zoom, [(c, r) for _p, c, r in items], no_imagery=seen
        )
        found.update(path for path, col, row in items if (col, row) in seen)
    return found


def no_imagery_path(path: str) -> bool:
    """Whether the store remembers the provider having nothing for this chunk."""
    store, parsed = _store_and_key(path)
    if store is None:
        return False
    col, row, zoom, maptype = parsed
    seen: set = set()
    store.get_many(maptype, zoom, [(col, row)], no_imagery=seen)
    return bool(seen)


def present_chunk_names(cache_dir: str) -> set:
    """Old-layout names of every chunk held with data under ``cache_dir``."""
    names = set()
    store = store_for(cache_dir)
    if store is not None:
        for col, row, zoom, maptype, status, _size, _fetched in store.iter_chunks():
            if status & _STATUS_KIND == STATUS_DATA:
                names.add(chunk_file_name(col, row, zoom, maptype))
        if not store.legacy_pending():
            return names
    try:
        with os.scandir(cache_dir) as entries:
            for entry in entries:
                if parse_chunk_name(entry.name) is not None:
                    names.add(entry.name)
    except OSError:
        pass
    return names

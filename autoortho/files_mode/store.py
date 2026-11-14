"""TextureStore: the files X-Plane reads, and an index of what each holds.

How a texture name changes over its life (plan sections 5.3, 5.5):

1. **Unbuilt**: a hard link to the neutral pool (``link`` strategy, the
   default) or its own sparse file whose levels of 256 px and smaller hold
   the neutral colour (``sparse`` strategy).
2. **First build**: a new sparse file is written beside it (``.ao-tmp-*``)
   and replaces the name atomically. Unbuilt names are never written in
   place: a hard link shares its data with every other unbuilt texture.
3. **Upgrades**: written **in place** into the texture's own file, only the
   new levels. Measured on Windows/NTFS: a name another process holds
   mapped cannot be replaced (sharing violation, even with POSIX-semantics
   rename), but in-place writes into it succeed. On macOS and Linux both
   work; in place also writes less (only the new levels).

Every write is guarded: a file with more than one link is never written in
place. A crash leaves at most a temp file (removed at the next start) or a
level whose index row was not updated (rebuilt later).
"""

import logging
import os
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass, field, replace as dc_replace
from typing import Optional

from . import dds_layout, fsops, geo

log = logging.getLogger(__name__)

NEUTRAL = 99
TMP_PREFIX = ".ao-tmp-"
FLUSH_INTERVAL_S = 2.0


@dataclass
class TextureState:
    name: str
    region: str
    row: int
    col: int
    maptype: str
    zoom: int
    max_zoom: int
    top_px: int
    built_level: int = NEUTRAL
    copied_level: Optional[int] = None
    patchy: bool = False
    retry_at: Optional[float] = None
    season_key: Optional[str] = None
    provider: Optional[str] = None
    built_at: Optional[float] = None
    last_needed_at: Optional[float] = None
    alloc_bytes: int = 0
    fmt: str = "BC1"


@dataclass
class WriteResult:
    ok: bool
    refused: bool = False
    in_place: bool = False
    bytes_written: int = 0
    error: str = ""


_COLUMNS = (
    "name", "region", "row", "col", "maptype", "zoom", "max_zoom", "top_px",
    "built_level", "copied_level", "patchy", "retry_at", "season_key",
    "provider", "built_at", "last_needed_at", "alloc_bytes", "fmt",
)


class TextureIndex:
    """In-memory states, persisted to sqlite by one writer thread."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._states = {}
        self._dirty = set()
        self._deleted = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._load()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS textures ("
            "name TEXT PRIMARY KEY, region TEXT NOT NULL, row INTEGER, col INTEGER,"
            " maptype TEXT, zoom INTEGER, max_zoom INTEGER NOT NULL,"
            " top_px INTEGER NOT NULL, built_level INTEGER NOT NULL,"
            " copied_level INTEGER, patchy INTEGER NOT NULL DEFAULT 0, retry_at REAL,"
            " season_key TEXT, provider TEXT, built_at REAL, last_needed_at REAL,"
            " alloc_bytes INTEGER, fmt TEXT)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS textures_region_rc ON textures(region, row, col)"
        )
        return conn

    def _load(self):
        conn = self._connect()
        try:
            for values in conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM textures"):
                data = dict(zip(_COLUMNS, values))
                data["patchy"] = bool(data["patchy"])
                data["fmt"] = data["fmt"] or "BC1"
                state = TextureState(**data)
                self._states[state.name] = state
        finally:
            conn.close()
        log.info("Texture index: %d built textures", len(self._states))

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="AO-FilesIndex", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.wait(FLUSH_INTERVAL_S):
            self.flush()

    def flush(self):
        with self._lock:
            dirty = [self._states[n] for n in self._dirty if n in self._states]
            deleted = list(self._deleted)
            self._dirty.clear()
            self._deleted.clear()
        if not dirty and not deleted:
            return
        try:
            conn = self._connect()
            try:
                with conn:
                    if deleted:
                        conn.executemany("DELETE FROM textures WHERE name = ?",
                                         [(n,) for n in deleted])
                    if dirty:
                        conn.executemany(
                            f"INSERT OR REPLACE INTO textures ({', '.join(_COLUMNS)}) "
                            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                            [tuple(int(getattr(s, c)) if c == "patchy" else getattr(s, c)
                                   for c in _COLUMNS) for s in dirty],
                        )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.error("Texture index flush failed: %s", exc)
            with self._lock:
                self._dirty.update(s.name for s in dirty)
                self._deleted.update(deleted)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.flush()

    def get(self, name):
        with self._lock:
            return self._states.get(name)

    def put(self, state):
        with self._lock:
            self._states[state.name] = state
            self._dirty.add(state.name)
            self._deleted.discard(state.name)

    def delete(self, name):
        with self._lock:
            self._states.pop(name, None)
            self._dirty.discard(name)
            self._deleted.add(name)

    def touch(self, name, when):
        with self._lock:
            state = self._states.get(name)
            if state is not None:
                state.last_needed_at = when
                self._dirty.add(name)

    def all(self):
        with self._lock:
            return list(self._states.values())

    def total_alloc(self):
        with self._lock:
            return sum(int(s.alloc_bytes or 0) for s in self._states.values())

    def __len__(self):
        with self._lock:
            return len(self._states)


class _StripedLocks:
    def __init__(self, count=256):
        self._locks = [threading.Lock() for _ in range(count)]

    def __call__(self, name):
        return self._locks[zlib.crc32(name.encode()) % len(self._locks)]


class TextureStore:
    def __init__(self, files_root, neutral_pool, index, strategy="link",
                 replace_retry_s=2.0, rgb=(66, 77, 55)):
        self.files_root = files_root
        self.neutral = neutral_pool
        self.index = index
        self.strategy = strategy
        self.replace_retry_s = replace_retry_s
        self.rgb = rgb
        self._locks = _StripedLocks()
        self._validated = set()
        self._validated_lock = threading.Lock()
        self.counters = {
            "files_bytes_written": 0, "files_replace_refused": 0,
            "files_writes": 0, "files_in_place": 0, "files_invalid_reset": 0,
        }

    # -- paths ---------------------------------------------------------
    def textures_dir(self, region):
        return os.path.join(self.files_root, region, "textures")

    def path(self, region, name):
        return os.path.join(self.textures_dir(region), name)

    # -- states --------------------------------------------------------
    def state(self, region, name, top_px=None, fmt=None):
        """The index row, validated against the file the first time."""
        st = self.index.get(name)
        if st is None:
            return None
        with self._validated_lock:
            seen = name in self._validated
        if seen:
            return st
        ok = self._validate(st)
        with self._validated_lock:
            self._validated.add(name)
        if not ok:
            self.counters["files_invalid_reset"] += 1
            log.info("Texture %s does not match its index row; treated as unbuilt", name)
            self.index.delete(name)
            return None
        return st

    def _validate(self, st):
        path = self.path(st.region, st.name)
        try:
            stat = os.stat(path)
        except OSError:
            return False
        if self.neutral.is_neutral(path):
            return False
        lay = dds_layout.layout(st.top_px, st.top_px, st.fmt)
        if stat.st_size != lay.total_size:
            return False
        try:
            fd = fsops.open_ro(path)
            try:
                head = fsops.pread(fd, dds_layout.HEADER_SIZE, 0)
            finally:
                os.close(fd)
        except OSError:
            return False
        return dds_layout.is_valid_header(head, lay)

    def is_built(self, region, name):
        st = self.state(region, name)
        return st is not None and st.built_level != NEUTRAL

    # -- names ---------------------------------------------------------
    def make_unbuilt(self, region, name, top_px, fmt):
        """Create the texture's name if absent. True when it now exists."""
        path = self.path(region, name)
        lock = self._locks(name)
        with lock:
            if os.path.lexists(path):
                return True
            if self.strategy == "sparse":
                return self._create_sparse_unbuilt(path, top_px, fmt)
            return self.neutral.link(top_px, fmt, path)

    def _create_sparse_unbuilt(self, path, top_px, fmt):
        lay = dds_layout.layout(top_px, top_px, fmt)
        tmp = self._tmp_path(path)
        try:
            fd = fsops.open_new(tmp)
            try:
                fsops.make_sparse(fd, lay.total_size)
                fsops.pwrite(fd, lay.header, 0)
                start, end = lay.level_range(min(4, lay.mip_count - 1))
                fsops.pwrite(fd, self.neutral.neutral_bytes(top_px, fmt, start, end - start), start)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.warning("Could not create %s: %s", path, exc)
            self._remove_quietly(tmp)
            return False

    def remove_if_unbuilt(self, region, name):
        """Remove the name when it holds nothing built. True when removed."""
        path = self.path(region, name)
        with self._locks(name):
            st = self.index.get(name)
            if st is not None and st.built_level != NEUTRAL:
                return False
            if not os.path.lexists(path):
                return False
            if self.strategy == "link" and not self.neutral.is_neutral(path):
                return False
            try:
                os.remove(path)
                if st is not None:
                    self.index.delete(name)
                return True
            except OSError:
                return False

    # -- writing -------------------------------------------------------
    def _tmp_path(self, path):
        folder, name = os.path.split(path)
        return os.path.join(folder, f"{TMP_PREFIX}{os.getpid()}-{threading.get_ident()}-{name}")

    @staticmethod
    def _remove_quietly(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _own_file(self, path):
        """True when ``path`` is a file only this name uses (safe in place)."""
        try:
            stat = os.stat(path)
        except OSError:
            return False
        if stat.st_nlink != 1:
            return False
        return not self.neutral.is_neutral(path)

    @staticmethod
    def _header_matches(path, lay):
        try:
            if os.path.getsize(path) != lay.total_size:
                return False
            fd = fsops.open_ro(path)
            try:
                return dds_layout.is_valid_header(fsops.pread(fd, dds_layout.HEADER_SIZE, 0), lay)
            finally:
                os.close(fd)
        except OSError:
            return False

    def write(self, region, built, now=None):
        """Write the levels of ``built`` (a ``texture_service.BuiltLevels``)."""
        now = time.time() if now is None else now
        key = built.key
        name = key.name
        path = self.path(region, name)
        lay = dds_layout.layout(built.width, built.width, built.dxt_format)
        if not built.levels:
            return WriteResult(False, error="nothing built")
        first = min(built.levels)
        with self._locks(name):
            st = self.index.get(name)
            same_shape = st is not None and st.top_px == built.width and st.fmt == lay.dxt_format
            own = self._own_file(path)
            refresh = bool(st and (st.patchy or st.season_key != built.season_key))
            if (st is None and own and self.strategy == "sparse"
                    and self._header_matches(path, lay)):
                # An unbuilt sparse name (no row yet): its own file, so the
                # first build goes in place too (survives an X-Plane mapping).
                st = TextureState(name=name, region=region, row=key.row, col=key.col,
                                  maptype=key.maptype, zoom=key.zoom, max_zoom=built.max_zoom,
                                  top_px=built.width, fmt=lay.dxt_format)
                same_shape = True
            if own and same_shape and (st.built_level != NEUTRAL or self.strategy == "sparse"):
                written = self._write_in_place(path, lay, built, st, refresh)
                result = WriteResult(True, in_place=True, bytes_written=written)
                self.counters["files_in_place"] += 1
            else:
                result = self._write_new(path, lay, built, st if (own and same_shape) else None)
                if not result.ok:
                    if result.refused:
                        self.counters["files_replace_refused"] += 1
                    return result
            built_level = first
            if st is not None and same_shape and st.built_level != NEUTRAL and not refresh:
                built_level = min(first, st.built_level)
            try:
                alloc = fsops.allocated_size(path)
            except OSError:
                alloc = lay.total_size
            new_state = TextureState(
                name=name, region=region, row=key.row, col=key.col,
                maptype=key.maptype, zoom=key.zoom, max_zoom=built.max_zoom,
                top_px=built.width, built_level=built_level, copied_level=None,
                patchy=bool(built.patchy),
                retry_at=(st.retry_at if (st and built.patchy) else None),
                season_key=built.season_key, provider=built.provider,
                built_at=now, last_needed_at=now, alloc_bytes=alloc, fmt=lay.dxt_format,
            )
            if st is not None and st.copied_level is not None and st.copied_level < built_level:
                new_state.copied_level = st.copied_level
            self.index.put(new_state)
            with self._validated_lock:
                self._validated.add(name)
        self.counters["files_writes"] += 1
        self.counters["files_bytes_written"] += result.bytes_written
        return result

    def _write_in_place(self, path, lay, built, st, refresh):
        fd = fsops.open_rw(path)
        written = 0
        try:
            for mip in sorted(built.levels):
                if not refresh and st.built_level != NEUTRAL and mip >= st.built_level:
                    continue  # already on disk, from its own zoom
                start, length = lay.offsets[mip]
                data = built.levels[mip]
                written += fsops.pwrite(fd, data[:length], start)
        finally:
            os.close(fd)
        return written

    def _write_new(self, path, lay, built, carry_from):
        tmp = self._tmp_path(path)
        written = 0
        try:
            fd = fsops.open_new(tmp)
        except OSError as exc:
            return WriteResult(False, error=str(exc))
        try:
            fsops.make_sparse(fd, lay.total_size)
            written += fsops.pwrite(fd, lay.header, 0)
            for mip in sorted(built.levels):
                start, length = lay.offsets[mip]
                written += fsops.pwrite(fd, built.levels[mip][:length], start)
            if carry_from is not None and carry_from.built_level != NEUTRAL:
                # Coarser levels already built stay as they were.
                written += self._carry_over(path, fd, lay, built, carry_from)
        except OSError as exc:
            os.close(fd)
            self._remove_quietly(tmp)
            return WriteResult(False, error=str(exc))
        os.close(fd)
        res = fsops.replace(tmp, path, retry_s=self.replace_retry_s)
        if not res.ok:
            self._remove_quietly(tmp)
            return WriteResult(False, refused=True, error=f"replace refused ({res.error})")
        return WriteResult(True, bytes_written=written)

    def _carry_over(self, path, fd, lay, built, st):
        missing = [m for m in range(st.built_level, lay.mip_count) if m not in built.levels]
        if not missing:
            return 0
        written = 0
        src = fsops.open_ro(path)
        try:
            for mip in missing:
                start, length = lay.offsets[mip]
                written += fsops.pwrite(fd, fsops.pread(src, length, start), start)
        finally:
            os.close(src)
        return written

    # -- stand-ins and eviction ---------------------------------------
    def fill_standin(self, region, name, level):
        """Copy up the best built level into ``level``..below it (own files).

        A late texture then shows its coarser imagery upscaled, never the
        neutral colour or a hole. Returns True when written.
        """
        try:
            from autoortho import dxt_upscale
        except ImportError:
            import dxt_upscale
        path = self.path(region, name)
        with self._locks(name):
            st = self.index.get(name)
            if st is None or st.built_level == NEUTRAL or level >= st.built_level:
                return False
            if st.copied_level is not None and st.copied_level <= level:
                return False
            if not self._own_file(path):
                return False
            lay = dds_layout.layout(st.top_px, st.top_px, st.fmt)
            src_mip = st.built_level
            fd = fsops.open_rw(path)
            try:
                s_start, s_len = lay.offsets[src_mip]
                source = fsops.pread(fd, s_len, s_start)
                src_px = max(1, st.top_px >> src_mip)
                src_blocks = max(1, src_px // 4)
                upto = st.copied_level if st.copied_level is not None else src_mip
                for mip in range(upto - 1, level - 1, -1):
                    out_px = max(1, st.top_px >> mip)
                    out_blocks = max(1, out_px // 4)
                    data, _src = dxt_upscale.upscale_blocks(
                        source, src_blocks, src_blocks,
                        range(out_blocks * out_blocks), out_blocks,
                        src_mip - mip, lay.blocksize,
                    )
                    if not data:
                        return False
                    start, length = lay.offsets[mip]
                    fsops.pwrite(fd, data[:length], start)
            finally:
                os.close(fd)
            st = dc_replace(st, copied_level=level)
            try:
                st.alloc_bytes = fsops.allocated_size(path)
            except OSError:
                pass
            self.index.put(st)
            return True

    def coarsen(self, region, name, keep_from_level):
        """Free levels sharper than ``keep_from_level``. Returns bytes freed."""
        path = self.path(region, name)
        with self._locks(name):
            st = self.index.get(name)
            if st is None or st.built_level == NEUTRAL:
                return 0
            if st.built_level >= keep_from_level and (
                st.copied_level is None or st.copied_level >= keep_from_level
            ):
                return 0
            if not self._own_file(path):
                return 0
            lay = dds_layout.layout(st.top_px, st.top_px, st.fmt)
            before = int(st.alloc_bytes or 0)
            start, _ = lay.offsets[0]
            end, _ = lay.offsets[keep_from_level]
            fd = fsops.open_rw(path)
            try:
                punched = fsops.punch_hole(fd, start, end - start)
            finally:
                os.close(fd)
            if not punched:
                return 0
            try:
                alloc = fsops.allocated_size(path)
            except OSError:
                alloc = before
            st = dc_replace(st, built_level=max(st.built_level, keep_from_level),
                            copied_level=None, alloc_bytes=alloc)
            self.index.put(st)
            return max(0, before - alloc)

    def delete(self, region, name):
        """Remove a built texture and its row. Returns bytes freed."""
        path = self.path(region, name)
        with self._locks(name):
            st = self.index.get(name)
            freed = int(st.alloc_bytes or 0) if st else 0
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.debug("Could not delete %s: %s", path, exc)
                return 0
            self.index.delete(name)
            return freed

    def cleanup_temp_files(self, region):
        folder = self.textures_dir(region)
        removed = 0
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith(TMP_PREFIX):
                        self._remove_quietly(entry.path)
                        removed += 1
        except OSError:
            pass
        if removed:
            log.info("Removed %d leftover temp files in %s", removed, folder)
        return removed

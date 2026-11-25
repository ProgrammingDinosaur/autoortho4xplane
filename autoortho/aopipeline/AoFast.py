"""ctypes wrapper of aofast.c: native reads of textures served from disk.

Hybrid read path plan (docs/developer/hybrid-read-path-plan.md), stage S2.
The C code is part of the aopipeline library. A library built before it was
added has no ``aofast_*`` symbols: :func:`load` then returns None and the
mount reads through Python as before.

Typical use, in the mount (see autoortho_fuse.py):

* :meth:`NativeReads.install` swaps the ``read`` callback of a mount's
  ``fuse_operations`` for this mount's native entry point, keeping the
  Python callback it replaced for every read the C code does not answer.
* ``open`` registers a complete texture (:meth:`register`) and gives FUSE the
  tagged handle it returns.
* ``release`` unregisters it, which waits for reads in flight.
* Once a second :meth:`drain` hands over the wanted-level samples the C code
  gathered, for the paging model.
"""
import ctypes
import logging
import os
import sys
import threading
from ctypes import (
    POINTER, Structure, c_char_p, c_int, c_int32, c_int64, c_size_t, c_uint16,
    c_uint32, c_uint64, c_void_p, c_wchar_p,
)

log = logging.getLogger(__name__)

TAG = 1 << 62
SLOTS = 4096
MAX_LEVELS = 24
MAX_ROWS = 64
MOUNTS = 32
TABLES = 32
ENV_LIB = "AO_TEST_AOFAST_LIB"


class Sample(Structure):
    _fields_ = [
        ("slot", c_int32),
        ("generation", c_uint32),
        ("level", c_int32),
        ("count", c_int32),
    ]


class Stats(Structure):
    _fields_ = [
        ("reads", c_int64),
        ("bytes", c_int64),
        ("deferred", c_int64),
        ("deferred_rows", c_int64),
        ("deferred_io", c_int64),
        ("deferred_closed", c_int64),
        ("past_end", c_int64),
        ("active_slots", c_int64),
        ("deferred_level", c_int64),
    ]


# int read(const char *path, char *buf, size_t size, int64_t offset, void *fi)
READ_FN = ctypes.CFUNCTYPE(c_int, c_char_p, c_void_p, c_size_t, c_int64, c_void_p)


def is_tagged(fh) -> bool:
    try:
        return bool(int(fh) & TAG)
    except (TypeError, ValueError):
        return False


def _library_path():
    override = os.environ.get(ENV_LIB, "").strip()
    if override:
        return override
    if __package__ and __package__.startswith("autoortho."):
        from autoortho.aopipeline import AoDDS
    else:
        from aopipeline import AoDDS  # type: ignore[no-redef]
    return AoDDS._get_lib_path()


class NativeReads:
    """One process's native read table (it is global in the library)."""

    def __init__(self, lib):
        self._lib = lib
        lib.aofast_version.restype = c_char_p
        lib.aofast_set_fh_offset.argtypes = [c_int32]
        lib.aofast_set_fh_offset.restype = None
        lib.aofast_set_enabled.argtypes = [c_int32]
        lib.aofast_set_enabled.restype = None
        lib.aofast_attach.argtypes = [c_void_p]
        lib.aofast_attach.restype = c_int32
        lib.aofast_entry.argtypes = [c_int32]
        lib.aofast_entry.restype = c_void_p
        lib.aofast_detach.argtypes = [c_int32]
        lib.aofast_detach.restype = None
        register_args = [
            c_int64, c_int64, POINTER(c_int64), c_int32, c_int64, c_int64,
            c_int32, c_uint64, c_int32, c_int32, c_int32,
        ]
        if sys.platform == "win32":
            lib.aofast_register_w.argtypes = [c_wchar_p] + register_args
            lib.aofast_register_w.restype = c_uint64
            self._register = lib.aofast_register_w
            self._encode = str
        else:
            lib.aofast_register.argtypes = [c_char_p] + register_args
            lib.aofast_register.restype = c_uint64
            self._register = lib.aofast_register
            self._encode = os.fsencode
        lib.aofast_mark_verified.argtypes = [c_uint64, c_uint64]
        lib.aofast_mark_verified.restype = c_int32
        lib.aofast_disable.argtypes = [c_uint64]
        lib.aofast_disable.restype = c_int32
        lib.aofast_unregister.argtypes = [c_uint64, POINTER(Sample), c_int32]
        lib.aofast_unregister.restype = c_int32
        lib.aofast_drain.argtypes = [POINTER(Sample), c_int32]
        lib.aofast_drain.restype = c_int32
        lib.aofast_get_stats.argtypes = [POINTER(Stats)]
        lib.aofast_get_stats.restype = None
        lib.aofast_read_mount.argtypes = [
            c_int32, c_char_p, c_void_p, c_size_t, c_int64, c_void_p,
        ]
        lib.aofast_read_mount.restype = c_int
        lib.aofast_classify.argtypes = [
            POINTER(c_int64), c_int32, POINTER(c_int64), c_int64, c_int64,
            POINTER(c_int32),
        ]
        lib.aofast_classify.restype = None
        lib.aofast_table_set.argtypes = [c_int32, POINTER(c_uint16)]
        lib.aofast_table_set.restype = c_int32
        lib.aofast_recolour.argtypes = [POINTER(c_uint16), c_void_p, c_int64, c_int64, c_int32]
        lib.aofast_recolour.restype = None
        # Season tables in the library's entries: id(table) -> [entry, opens
        # using it, the table (kept so its id stays its own)].
        self._tables = {}
        self._table_lock = threading.Lock()
        self.version = lib.aofast_version().decode()
        # The Python callbacks each mount's entry point falls back to: kept
        # alive here, since the struct no longer references them.
        self._keep = {}
        self._lock = threading.Lock()
        self._levels_cache = {}

    # -- install ----------------------------------------------------------

    def set_fh_offset(self, offset: int) -> None:
        self._lib.aofast_set_fh_offset(int(offset))

    def set_enabled(self, enabled: bool) -> None:
        self._lib.aofast_set_enabled(1 if enabled else 0)

    def install(self, ops) -> int:
        """Swap ``ops.read`` (a ``fuse_operations`` struct) for a native
        entry point. Returns the mount index, or -1 (nothing changed)."""
        original = ops.read
        address = ctypes.cast(original, c_void_p).value
        if not address:
            return -1
        mount = self._lib.aofast_attach(address)
        if mount < 0:
            return -1
        entry = self._lib.aofast_entry(mount)
        with self._lock:
            # The struct's own reference to the Python thunk goes away when
            # the field is replaced: keep the thunk (and its owner) alive.
            self._keep[mount] = (original, getattr(ops, "_objects", None) and dict(ops._objects))
        ops.read = ctypes.cast(c_void_p(entry), type(original))
        return mount

    def uninstall(self, mount: int) -> None:
        """After the mount's FUSE loop returned."""
        if mount is None or mount < 0:
            return
        self._lib.aofast_detach(int(mount))
        with self._lock:
            self._keep.pop(mount, None)

    def attach_callback(self, callback) -> int:
        """Attach a plain Python read callback (tests): returns the mount."""
        thunk = READ_FN(callback)
        mount = self._lib.aofast_attach(ctypes.cast(thunk, c_void_p).value)
        if mount >= 0:
            with self._lock:
                self._keep[mount] = (thunk, None)
        return mount

    def read_mount(self, mount, path, buf, size, offset, fi) -> int:
        """Call a mount's native read as FUSE would (tests)."""
        return self._lib.aofast_read_mount(mount, path, buf, size, offset, fi)

    # -- per open ---------------------------------------------------------

    def _levels(self, levels):
        key = tuple((lv.startpos, lv.endpos) for lv in levels)
        array = self._levels_cache.get(key)
        if array is None:
            starts = [lv.startpos for lv in levels] + [levels[-1].endpos]
            array = (c_int64 * len(starts))(*starts)
            if len(self._levels_cache) > 64:
                self._levels_cache.clear()
            self._levels_cache[key] = array
        return array

    @staticmethod
    def _table_pointer(table):
        import numpy as np
        array = np.ascontiguousarray(table, dtype=np.uint16)
        if array.shape != (65536,):
            raise ValueError("a colour table has 65,536 entries")
        return array, array.ctypes.data_as(POINTER(c_uint16))

    def acquire_table(self, table) -> int:
        """The library entry holding ``table`` for one more open, or -1.

        Entries are reused once no open uses them. Release with
        :meth:`release_table`.
        """
        if table is None:
            return -1
        key = id(table)
        with self._table_lock:
            known = self._tables.get(key)
            if known is not None:
                known[1] += 1
                return known[0]
            used = {entry for entry, _n, _t in self._tables.values()}
            free = [e for e in range(TABLES) if e not in used]
            if not free:
                idle = [k for k, v in self._tables.items() if v[1] == 0]
                if not idle:
                    return -1
                free = [self._tables.pop(idle[0])[0]]
            entry = free[0]
            try:
                _array, pointer = self._table_pointer(table)
            except Exception:
                return -1
            if not self._lib.aofast_table_set(entry, pointer):
                return -1
            self._tables[key] = [entry, 1, table]
            return entry

    def release_table(self, entry) -> None:
        if entry is None or entry < 0:
            return
        with self._table_lock:
            for value in self._tables.values():
                if value[0] == entry:
                    value[1] = max(0, value[1] - 1)
                    return

    def recolour(self, table, data, offset, block_size) -> bytes:
        """colour_table.apply in C (tests)."""
        _array, pointer = self._table_pointer(table)
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        self._lib.aofast_recolour(pointer, buf, int(offset), len(data), int(block_size))
        return buf.raw

    def register(self, path, size, mtime_ns, levels, row_start, row_bytes,
                 rows, verified_rows=(), finest_level=0, block_size=8,
                 table=None):
        """``(handle, table entry)`` of one open; handle 0 keeps it in Python.

        ``table`` is the season colour table to apply, or None. The entry
        must be given back with :meth:`release_table` after unregister.
        """
        if not levels or len(levels) > MAX_LEVELS or rows > MAX_ROWS:
            return 0, -1
        bits = 0
        for row in verified_rows:
            if 0 <= int(row) < MAX_ROWS:
                bits |= 1 << int(row)
        entry = -1
        if table is not None:
            entry = self.acquire_table(table)
            if entry < 0:
                return 0, -1
        handle = int(self._register(
            self._encode(path), int(size), int(mtime_ns), self._levels(levels),
            len(levels), int(row_start), int(row_bytes), int(rows), bits,
            int(finest_level), int(block_size), int(entry),
        ))
        if not handle:
            self.release_table(entry)
            return 0, -1
        return handle, entry

    def mark_verified(self, handle, rows) -> bool:
        bits = 0
        for row in rows:
            if 0 <= int(row) < MAX_ROWS:
                bits |= 1 << int(row)
        return bool(self._lib.aofast_mark_verified(int(handle), bits))

    def disable(self, handle) -> bool:
        return bool(self._lib.aofast_disable(int(handle)))

    def unregister(self, handle):
        """Close the slot; returns its pending ``[(level, count)]``."""
        out = (Sample * MAX_LEVELS)()
        n = self._lib.aofast_unregister(int(handle), out, MAX_LEVELS)
        return [(out[i].level, out[i].count) for i in range(max(0, n))]

    def drain(self, cap=1024):
        """Pending samples of every slot: ``[(handle, level, count)]``."""
        out = (Sample * cap)()
        n = self._lib.aofast_drain(out, cap)
        return [
            (TAG | (out[i].generation << 16) | out[i].slot, out[i].level, out[i].count)
            for i in range(max(0, n))
        ]

    def stats(self) -> dict:
        out = Stats()
        self._lib.aofast_get_stats(ctypes.byref(out))
        return {name: int(getattr(out, name)) for name, _t in Stats._fields_}

    def classify(self, levels, cursor, offset, length):
        """read_pattern.classify in C; ``cursor`` is [last_end, header_end]."""
        starts = self._levels(levels)
        cur = (c_int64 * 2)(*cursor)
        out = (c_int32 * 3)()
        self._lib.aofast_classify(starts, len(levels), cur, int(offset), int(length), out)
        cursor[0], cursor[1] = cur[0], cur[1]
        wanted = None if out[1] < 0 else int(out[1])
        return bool(out[0]), wanted, int(out[2])


_native = None
_load_error = None
_load_lock = threading.Lock()


def load(path=None):
    """The process's :class:`NativeReads`, or None when the library has none."""
    global _native, _load_error
    with _load_lock:
        if _native is not None and path is None:
            return _native
        if _load_error is not None and path is None:
            return None
        try:
            lib_path = path or _library_path()
            if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(os.path.dirname(os.path.abspath(lib_path)))
                except OSError:
                    pass
            lib = ctypes.CDLL(lib_path)
            if not hasattr(lib, "aofast_entry"):
                raise OSError(f"{lib_path} has no native read support (aofast)")
            native = NativeReads(lib)
        except Exception as exc:
            if path is None:
                _load_error = exc
            log.info("Native texture reads unavailable: %s", exc)
            return None
        if path is None:
            _native = native
        log.info("Native texture reads available: %s", native.version)
        return native

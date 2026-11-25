"""Finished textures served straight from their DDS cache file.

A texture whose complete, exact DDS is in the disk cache does not need a
``Tile`` in memory: X-Plane's reads are answered from the file, and the OS
file cache keeps the hot ones (shared, reclaimed under pressure, not charged
to AutoOrtho). See docs/developer/resource-usage-plan.md.

Mipmap-0 rows are checked against their SHA-256 row manifest the first time
each row is read, once per file per process: the rows found good outlive the
open (``_verified``), keyed by the file's path, size, modification time and
identity, so a texture X-Plane opens again is not checked again. Any short
read, I/O error or checksum mismatch raises :class:`DiskTextureFailed`; the
caller then serves the rest of that open from a live tile.
"""
import functools
import logging
import os
import threading
from collections import OrderedDict, namedtuple
from typing import Callable, Dict, Optional, Tuple

try:
    from autoortho import read_pattern
    from autoortho.aopipeline.dynamic_dds_cache import mm0_row_layout
except ImportError:
    import read_pattern
    from aopipeline.dynamic_dds_cache import mm0_row_layout

log = logging.getLogger(__name__)

DDS_HEADER_BYTES = 128
_HAS_PREAD = hasattr(os, "pread")
_O_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)

MipLevel = namedtuple("MipLevel", "idx startpos endpos length")


class DiskTextureFailed(Exception):
    """The cache file cannot serve this read; serve it live instead."""


class DiskTextureLevelMissing(DiskTextureFailed):
    """A partial entry does not hold the level this read wants.

    Not a fault of the file: it is not marked bad, and the entry stays.
    """


class DiskTextureStale(DiskTextureFailed):
    """The entry changed on disk since this open (rebuilt, healed).

    Not a fault of the file: the rest of the open is served live and the
    next open sees the new entry.
    """


@functools.lru_cache(maxsize=64)
def mip_layout(width: int, height: int, fmt) -> Tuple[MipLevel, ...]:
    """The byte range of every level, as ``pydds.DDS`` lays them out."""
    block = 16 if str(fmt).upper() in ("BC3", "DXT5") else 8
    levels = []
    pos = DDS_HEADER_BYTES
    w, h = int(width), int(height)
    idx = 0
    while w >= 1 and h >= 1:
        length = max(1, (w * h) >> 4) * block
        levels.append(MipLevel(idx, pos, pos + length, length))
        pos += length
        w >>= 1
        h >>= 1
        idx += 1
    return tuple(levels)


class _SharedFile:
    """One open handle per cache file, shared by every open of the texture."""

    def __init__(self, key, path, size):
        self.key = key
        self.path = path
        self.size = size
        self.fd = os.open(path, _O_FLAGS)
        self.ident = None  # the file's identity (inode, file index)
        self.refs = 0
        self.lock = threading.Lock()  # seek+read on platforms without pread
        self.verified_rows = set()
        self.verify_lock = threading.Lock()
        self.manifests = None  # loaded on the first row check
        self.bad = False

    def pread(self, length: int, offset: int) -> bytes:
        if _HAS_PREAD:
            return os.pread(self.fd, length, offset)
        with self.lock:
            os.lseek(self.fd, offset, os.SEEK_SET)
            return os.read(self.fd, length)

    def read_exact(self, length: int, offset: int) -> bytes:
        parts = []
        got = 0
        while got < length:
            piece = self.pread(length - got, offset + got)
            if not piece:
                break
            parts.append(piece)
            got += len(piece)
        data = parts[0] if len(parts) == 1 else b"".join(parts)
        if len(data) != length:
            raise DiskTextureFailed(
                f"short read {len(data)}/{length} at {offset} in {self.path}"
            )
        return data

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


_files: Dict[tuple, _SharedFile] = {}
_files_lock = threading.Lock()

# Rows found good in files no longer open: file key -> (identity, row bits).
# The key holds the size and modification time, so a rebuilt or healed file
# is a new key; the identity guards against a file replaced by another with
# the same size and time. A few dozen bytes per texture.
_VERIFIED_MAX = 16384
_verified: "OrderedDict[tuple, Tuple[object, int]]" = OrderedDict()


def _rows_to_bits(rows) -> int:
    bits = 0
    for row in rows:
        bits |= 1 << int(row)
    return bits


def _bits_to_rows(bits: int) -> set:
    rows = set()
    row = 0
    while bits:
        if bits & 1:
            rows.add(row)
        bits >>= 1
        row += 1
    return rows


def _acquire_file(path: str, size: int, mtime_ns: int) -> _SharedFile:
    key = (os.path.normcase(path), size, mtime_ns)
    with _files_lock:
        shared = _files.get(key)
        if shared is None or shared.bad:
            shared = _SharedFile(key, path, size)
            st = os.fstat(shared.fd)
            if st.st_size != size or st.st_mtime_ns != mtime_ns:
                # Replaced between the metadata check and the open.
                shared.close()
                raise DiskTextureFailed(f"{path} changed while opening")
            shared.ident = (st.st_dev, st.st_ino)
            known = _verified.get(key)
            if known is not None and known[0] == shared.ident:
                shared.verified_rows = _bits_to_rows(known[1])
                _verified.move_to_end(key)
            _files[key] = shared
        shared.refs += 1
        return shared


def _release_file(shared: _SharedFile) -> bool:
    """Drop one reference; True when the handle was closed."""
    with _files_lock:
        shared.refs -= 1
        if shared.refs > 0:
            return False
        if _files.get(shared.key) is shared:
            del _files[shared.key]
        if shared.bad:
            _verified.pop(shared.key, None)
        elif shared.verified_rows and shared.ident is not None:
            _verified[shared.key] = (shared.ident, _rows_to_bits(shared.verified_rows))
            _verified.move_to_end(shared.key)
            while len(_verified) > _VERIFIED_MAX:
                _verified.popitem(last=False)
    shared.close()
    return True


def remember_verified(path: str, rows, size: int) -> bool:
    """Rows of a cache file this process just wrote, from bytes it had
    checked against their manifests: no read needs to check them again
    (hybrid read path plan, S3; the check moves off the read path for every
    texture built in this session).

    Keyed like the record :func:`_release_file` keeps, by the file as it is
    now; a file that is not ``size`` bytes is left alone.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size != size:
        return False
    key = (os.path.normcase(path), st.st_size, st.st_mtime_ns)
    ident = (st.st_dev, st.st_ino)
    bits = _rows_to_bits(rows)
    with _files_lock:
        known = _verified.get(key)
        if known is not None and known[0] == ident:
            bits |= known[1]
        _verified[key] = (ident, bits)
        _verified.move_to_end(key)
        while len(_verified) > _VERIFIED_MAX:
            _verified.popitem(last=False)
        shared = _files.get(key)
        if shared is not None and shared.ident == ident and not shared.bad:
            shared.verified_rows.update(_bits_to_rows(bits))
    return True


def forget_verified() -> None:
    """Drop every remembered row check (tests; a cache cleared by hand)."""
    with _files_lock:
        _verified.clear()


def open_files() -> int:
    """Cache files currently held open for direct reads."""
    with _files_lock:
        return len(_files)


class DiskTexture:
    """One X-Plane open of a texture served from its cache file."""

    def __init__(
        self,
        entry: dict,
        *,
        unpin: Callable[[str], None],
        invalidate: Callable[[str, int], object],
        recolour: Optional[Callable[[bytes, int], bytes]] = None,
        note_wanted: Optional[Callable[[int, int], None]] = None,
        metric: Optional[Callable[..., None]] = None,
    ):
        self.key = entry["key"]
        self.tile_id = entry["tile_id"]
        self.max_zoom = entry["max_zoom"]
        self.size = int(entry["size"])
        self.width = int(entry["width"])
        self.height = int(entry["height"])
        self.fmt = entry["fmt"]
        # The row manifests, or a function that loads them: most opens find
        # every row they read already checked and never need them.
        self._manifests = entry["manifests"]
        self._unpin = unpin
        self._invalidate = invalidate
        self._recolour = recolour
        self._note_wanted = note_wanted
        self._metric = metric or (lambda *_a, **_k: None)
        self._levels = mip_layout(self.width, self.height, self.fmt)
        if not self._levels or self._levels[-1].endpos != self.size:
            unpin(self.key)
            raise DiskTextureFailed(f"layout of {self.tile_id} does not match its size")
        self._row_start, self._row_bytes, self._rows = mm0_row_layout(
            self.width, self.height, self.fmt
        )
        # Levels finer than this are not in the file (a partial entry).
        self.finest_level = int(entry.get("finest_level", 0) or 0)
        if not 0 <= self.finest_level < len(self._levels):
            unpin(self.key)
            raise DiskTextureFailed(f"{self.tile_id}: bad finest level {self.finest_level}")
        self._standin_block: Optional[bytes] = None
        # Set by the mount when native code answers this open's reads
        # (aofast): it classifies every read itself, and is told which rows
        # this side checks (see native_spec).
        self.classified_natively = False
        self.on_rows_verified: Optional[Callable[[set], None]] = None
        # The season colour table that applies to this texture now (or
        # None), and the block size recolour() uses with it.
        self.colour_table_now: Optional[Callable[[], object]] = None
        self.colour_block_bytes = 0
        self._cursor = read_pattern.ReadCursor()
        self._closed = False
        # Reads served, counted once at close: a counter per read cost more
        # than the read itself.
        self.reads = 0
        try:
            self._file = _acquire_file(entry["dds_path"], self.size, entry["mtime_ns"])
        except Exception:
            unpin(self.key)
            raise

    # ------------------------------------------------------------------
    def read(self, offset: int, length: int) -> bytes:
        if self._closed:
            raise DiskTextureFailed("texture closed")
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        end = offset + length
        first_needed = self._note_read(offset, length)
        try:
            if self.finest_level:
                data = self._read_partial(offset, end, first_needed)
            else:
                self._verify_rows(offset, end)
                data = self._file.read_exact(length, offset)
        except DiskTextureLevelMissing:
            self._metric("dds_direct_partial_fallbacks")
            raise
        except DiskTextureStale:
            self._metric("dds_direct_stale_fallbacks")
            raise
        except DiskTextureFailed:
            self._file.bad = True
            raise
        except OSError as exc:
            self._file.bad = True
            raise DiskTextureFailed(f"{self.tile_id}: {exc}") from exc
        self.reads += 1
        if self._recolour is not None:
            data = self._recolour(data, offset)
        return data

    def _read_partial(self, offset: int, end: int, first_needed: int) -> bytes:
        """A read of an entry that holds levels ``finest_level`` and coarser.

        Bytes of finer levels that the read only touches incidentally (the
        header chunk's piece of mipmap 0, the tail of the level in front of
        the wanted one) are answered with stand-in bytes, as the live path
        answers them from the base level. A read that needs a finer level
        raises :class:`DiskTextureLevelMissing`.
        """
        boundary = self._levels[self.finest_level].startpos
        needed_start = (
            self._levels[first_needed].startpos
            if first_needed < len(self._levels)
            else end
        )
        needed_start = max(offset, needed_start)
        if needed_start < min(end, boundary):
            raise DiskTextureLevelMissing(
                f"{self.tile_id}: level {first_needed} wanted, "
                f"file holds {self.finest_level} and coarser"
            )
        parts = []
        if offset < DDS_HEADER_BYTES:
            # The real header, from the file.
            parts.append(self._file.read_exact(min(end, DDS_HEADER_BYTES) - offset, offset))
        standin_start = max(offset, DDS_HEADER_BYTES)
        standin_end = min(end, boundary)
        if standin_start < standin_end:
            parts.append(self._standin(standin_start, standin_end))
        file_start = max(offset, boundary)
        if file_start < end:
            parts.append(self._file.read_exact(end - file_start, file_start))
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def _standin(self, start: int, end: int) -> bytes:
        """Stand-in bytes for ``[start, end)`` of the levels not in the file.

        One compressed block repeated: the texture's coarsest stored block,
        so the stand-in has its average colour. Built once per texture.
        """
        block_size = 16 if str(self.fmt).upper() in ("BC3", "DXT5") else 8
        if self._standin_block is None:
            coarsest = self._levels[-1]
            block = self._file.read_exact(min(block_size, coarsest.length), coarsest.startpos)
            if len(block) < block_size:
                block = block.ljust(block_size, b"\x00")
            self._standin_block = block
        # Blocks are aligned to the start of each level, and every level
        # starts on a block boundary: align to the data start (byte 128).
        phase = (start - DDS_HEADER_BYTES) % block_size
        count = (end - start + phase) // block_size + 2
        tiled = self._standin_block * count
        return tiled[phase:phase + (end - start)]

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.reads:
            self._metric("dds_direct_reads", self.reads)
        bad = self._file.bad
        closed = _release_file(self._file)
        self._unpin(self.key)
        if bad and closed:
            # The file failed a check: drop the entry so it is rebuilt.
            try:
                self._invalidate(self.tile_id, self.max_zoom)
            except Exception as exc:
                log.debug(f"Could not invalidate {self.tile_id}: {exc}")

    # ------------------------------------------------------------------
    def _note_read(self, offset, length) -> int:
        """Classify the read, feed the paging model; return ``first_needed``."""
        try:
            _header, wanted, first_needed = read_pattern.classify(
                self._levels, self._cursor, offset, length
            )
        except Exception:
            return 0
        if wanted is not None and self._note_wanted is not None:
            try:
                self._note_wanted(wanted, max(1, self.width >> int(wanted)))
            except Exception:  # statistics only
                pass
        return int(first_needed)

    def _verify_rows(self, offset, end):
        mm0_end = self._row_start + self._row_bytes * self._rows
        if end <= self._row_start or offset >= mm0_end:
            return
        first = max(0, (offset - self._row_start) // self._row_bytes)
        last = min(self._rows - 1, (end - 1 - self._row_start) // self._row_bytes)
        shared = self._file
        for row in range(first, last + 1):
            if row in shared.verified_rows:
                continue
            with shared.verify_lock:
                if row in shared.verified_rows:
                    continue
                start = self._row_start + row * self._row_bytes
                data = shared.read_exact(self._row_bytes, start)
                manifest = self._row_manifests(shared).get(row)
                if manifest is None or not manifest.validate_data(data):
                    self._metric("dds_direct_checksum_failures")
                    raise DiskTextureFailed(
                        f"{self.tile_id}: mipmap-0 row {row} does not match its manifest"
                    )
                shared.verified_rows.add(row)
                self._metric("dds_direct_rows_checked")
                if self.on_rows_verified is not None:
                    try:
                        self.on_rows_verified({row})
                    except Exception:  # the native side then asks again
                        pass

    def _row_manifests(self, shared) -> dict:
        """The row manifests, loaded once per open file (``verify_lock`` held)."""
        if shared.manifests is None:
            manifests = self._manifests
            if callable(manifests):
                # Raises DiskTextureStale when the entry changed since open.
                manifests = manifests()
            shared.manifests = manifests or {}
        return shared.manifests

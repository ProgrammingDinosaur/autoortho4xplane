"""Finished textures served straight from their DDS cache file.

A texture whose complete, exact DDS is in the disk cache does not need a
``Tile`` in memory: X-Plane's reads are answered from the file, and the OS
file cache keeps the hot ones (shared, reclaimed under pressure, not charged
to AutoOrtho). See docs/developer/resource-usage-plan.md.

Mipmap-0 rows are checked against their SHA-256 row manifest the first time
each row is read (once per file per process). Any short read, I/O error or
checksum mismatch raises :class:`DiskTextureFailed`; the caller then serves
the rest of that open from a live tile.
"""
import logging
import os
import threading
from collections import namedtuple
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
        self.refs = 0
        self.lock = threading.Lock()  # seek+read on platforms without pread
        self.verified_rows = set()
        self.verify_lock = threading.Lock()
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
    shared.close()
    return True


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
        self._cursor = read_pattern.ReadCursor()
        self._closed = False
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
        except DiskTextureFailed:
            self._file.bad = True
            raise
        except OSError as exc:
            self._file.bad = True
            raise DiskTextureFailed(f"{self.tile_id}: {exc}") from exc
        self._metric("dds_direct_reads")
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
                manifest = self._manifests.get(row)
                if manifest is None or not manifest.validate_data(data):
                    self._metric("dds_direct_checksum_failures")
                    raise DiskTextureFailed(
                        f"{self.tile_id}: mipmap-0 row {row} does not match its manifest"
                    )
                shared.verified_rows.add(row)

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
        self._note_read(offset, length)
        try:
            self._verify_rows(offset, end)
            data = self._file.read_exact(length, offset)
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
    def _note_read(self, offset, length):
        if self._note_wanted is None:
            return
        try:
            _header, wanted, _first = read_pattern.classify(
                self._levels, self._cursor, offset, length
            )
            if wanted is not None:
                self._note_wanted(wanted, max(1, self.width >> int(wanted)))
        except Exception:  # statistics only
            pass

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

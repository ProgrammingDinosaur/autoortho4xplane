"""Neutral textures: what X-Plane reads of a texture not built yet.

One fully written DDS per size class and format, every level the missing
colour (the same as FUSE's fallback), shared through hard links. NTFS
allows 1,023 names per file, so the pool keeps numbered copies and links to
the first with room.
"""

import errno
import logging
import os
import threading

from . import dds_layout, fsops

log = logging.getLogger(__name__)

ERROR_TOO_MANY_LINKS = 1142
NTFS_MAX_LINKS = 1023


class NeutralPool:
    def __init__(self, directory, rgb=(66, 77, 55)):
        self.directory = directory
        self.rgb = tuple(rgb)
        self._lock = threading.Lock()
        self._current = {}   # (px, fmt) -> copy index
        self._ids = set()    # (st_dev, st_ino) of every pool file
        os.makedirs(directory, exist_ok=True)
        self._scan()

    def _scan(self):
        for name in os.listdir(self.directory):
            if name.startswith("neutral_") and name.endswith(".dds"):
                try:
                    self._ids.add(fsops.file_id(os.path.join(self.directory, name)))
                except OSError:
                    pass

    def path(self, px, fmt, index):
        return os.path.join(self.directory, f"neutral_{fmt}_{px}_{index:03d}.dds")

    def _create(self, px, fmt, index):
        path = self.path(px, fmt, index)
        if os.path.exists(path):
            return path
        lay = dds_layout.layout(px, px, fmt)
        block = dds_layout.neutral_block(fmt, self.rgb)
        tmp = f"{path}.ao-tmp-{os.getpid()}"
        with open(tmp, "wb") as handle:
            handle.write(lay.header)
            remaining = lay.total_size - dds_layout.HEADER_SIZE
            chunk = block * (1 << 16)  # 512 KB / 1 MB
            while remaining > 0:
                take = min(remaining, len(chunk))
                handle.write(chunk[:take])
                remaining -= take
        os.replace(tmp, path)
        self._ids.add(fsops.file_id(path))
        log.info("Neutral texture %s created (%d bytes)", path, lay.total_size)
        return path

    def link(self, px, fmt, dst):
        """Hard link a neutral of ``px``/``fmt`` at ``dst``. True when done."""
        key = (int(px), str(fmt))
        with self._lock:
            index = self._current.get(key, 0)
            for _attempt in range(10000):
                src = self._create(key[0], key[1], index)
                err = fsops.hard_link(src, dst)
                if err == 0:
                    self._current[key] = index
                    return True
                if err in (errno.EEXIST,):
                    return False
                if err in (ERROR_TOO_MANY_LINKS, errno.EMLINK):
                    index += 1
                    continue
                log.warning("Neutral link %s -> %s failed: %s", src, dst, err)
                return False
        return False

    def is_neutral(self, path):
        try:
            return fsops.file_id(path) in self._ids
        except OSError:
            return False

    def neutral_bytes(self, px, fmt, offset, length):
        """Neutral content of a byte range (for stand-ins in sparse files)."""
        block = dds_layout.neutral_block(fmt, self.rgb)
        bs = len(block)
        prefix = (offset - dds_layout.HEADER_SIZE) % bs
        count = (prefix + length + bs - 1) // bs
        return (block * count)[prefix:prefix + length]

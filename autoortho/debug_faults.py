"""Debug switch that makes chosen texture reads fail on purpose.

Off unless ``AO_DEBUG_FAIL_DDS`` is set. It exists to test, in X-Plane, what
a failed read looks like (the placeholder texture, see dds_fallback.py):

``AO_DEBUG_FAIL_DDS``
    ``all``, or 1x1 degree tiles named like their DSF, comma separated:
    ``+62-008,+55+012``. Reads of textures touching them get the placeholder.
``AO_DEBUG_FAIL_DDS_SECONDS``
    Fail only for this many seconds after the first read in each tile, like
    a failure while X-Plane loads it. Reload the scenery afterwards to check
    real imagery replaces the placeholder. Default: fail for the whole run.
``AO_DEBUG_FAIL_DDS_HEADER=zero``
    Serve the all-zero header of AutoOrtho 3.4.2 and earlier instead of a
    valid one, to reproduce the white tiles it caused.
"""

import logging
import os
import re
import threading
import time
from typing import Callable, Dict, Mapping, Optional, Set, Tuple

try:
    from autoortho import tile_stream
except ImportError:
    import tile_stream

log = logging.getLogger(__name__)

_CELL_RE = re.compile(r"^([+-]\d{1,2})([+-]\d{1,3})$")

Cell = Tuple[int, int]


def parse_cells(spec: str) -> Tuple[bool, Set[Cell]]:
    """``(all, cells)`` from an ``AO_DEBUG_FAIL_DDS`` value."""
    every, cells = False, set()
    for part in (spec or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lower().endswith(".dsf"):
            part = part[:-4]
        if not part:
            continue
        if part.lower() == "all":
            every = True
            continue
        m = _CELL_RE.match(part)
        if not m:
            log.warning("AO_DEBUG_FAIL_DDS: ignoring %r (expected e.g. +62-008)", part)
            continue
        cells.add((int(m.group(1)), int(m.group(2))))
    return every, cells


class DdsFaults:
    def __init__(self, every: bool = False, cells=(), seconds: Optional[float] = None,
                 zero_header: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.every = bool(every)
        self.cells = set(cells)
        self.seconds = seconds if seconds and seconds > 0 else None
        self.zero_header = bool(zero_header)
        self._clock = clock
        self._first: Dict[Cell, float] = {}
        self._announced: Set[Cell] = set()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "DdsFaults":
        env = os.environ if env is None else env
        every, cells = parse_cells(env.get("AO_DEBUG_FAIL_DDS", ""))
        seconds = None
        raw = env.get("AO_DEBUG_FAIL_DDS_SECONDS", "").strip()
        if raw:
            try:
                seconds = float(raw)
            except ValueError:
                log.warning("AO_DEBUG_FAIL_DDS_SECONDS: ignoring %r", raw)
        zero = env.get("AO_DEBUG_FAIL_DDS_HEADER", "").strip().lower() == "zero"
        faults = cls(every, cells, seconds, zero)
        if faults.active:
            where = "every tile" if every else ", ".join(
                f"{la:+03d}{lo:+04d}" for la, lo in sorted(cells))
            log.warning(
                "DEBUG: texture reads will fail on purpose in %s%s%s",
                where,
                f" for {faults.seconds:g}s after each tile's first read" if faults.seconds else "",
                "; placeholders use the old all-zero header" if zero else "",
            )
        elif zero:
            log.warning("DEBUG: placeholder textures use the old all-zero header")
        return faults

    @property
    def active(self) -> bool:
        return self.every or bool(self.cells)

    def should_fail(self, row: int, col: int, zoom: int) -> bool:
        if not self.active:
            return False
        touched = tile_stream.texture_cells(row, col, zoom)
        matched = touched if self.every else touched & self.cells
        if not matched:
            return False
        now = self._clock()
        failing = False
        with self._lock:
            for cell in matched:
                first = self._first.setdefault(cell, now)
                if self.seconds is None or now - first < self.seconds:
                    failing = True
                    if cell not in self._announced:
                        self._announced.add(cell)
                        log.warning("DEBUG: failing texture reads in %+03d%+04d on purpose",
                                    *cell)
        return failing

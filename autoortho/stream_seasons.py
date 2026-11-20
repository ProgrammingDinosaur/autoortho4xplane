"""X-Plane 12 seasons for downloaded tiles of the XP11 set.

The XP11 set's DSFs have no season rasters; the Seasons patch copies them
from X-Plane 12's Global Scenery into each DSF with DSFTool (see
:class:`utils.dsf_utils.DsfUtils`). It takes a few seconds per tile, so it
runs only for tiles downloaded on the map (kept tiles), never for tiles
X-Plane is waiting for. The XP12 set has the rasters built in.

Which tiles are patched is recorded in ``seasons_tiles.json`` in the
package, with each DSF's size and modification time after patching: a tile
deleted and downloaded again no longer matches and counts as unpatched.
No backup is kept; an original can be downloaded again.

The option itself is ``"seasons": true`` in the ortho package's marker
(:func:`tile_stream.set_stream_seasons`).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Iterable, List, Optional, Tuple

if __package__:
    from . import tile_stream
else:  # running from inside the autoortho folder
    import tile_stream

log = logging.getLogger(__name__)

Cell = Tuple[int, int]
SEASONS_FILE = "seasons_tiles.json"
SEASONS_SET = "xp11"
_lock = threading.Lock()


def _cell_key(cell: Cell) -> str:
    return os.path.basename(tile_stream.dsf_rel(*cell))[:-4]


def _stamp(path: str) -> Optional[list]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, int(st.st_mtime)]


def read_patched(root: str) -> Dict[str, list]:
    try:
        with open(os.path.join(root, SEASONS_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: v for k, v in data.get("tiles", {}).items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):
        return {}


def _write(root: str, tiles: Dict[str, list]) -> None:
    path = os.path.join(root, SEASONS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"tiles": tiles}, fh)
    os.replace(tmp, path)


def _record(root: str, cell: Cell, stamp: Optional[list]) -> None:
    with _lock:
        tiles = read_patched(root)
        if stamp is None:
            tiles.pop(_cell_key(cell), None)
        else:
            tiles[_cell_key(cell)] = stamp
        _write(root, tiles)


def is_patched(root: str, cell: Cell, patched: Optional[Dict[str, list]] = None) -> bool:
    patched = read_patched(root) if patched is None else patched
    stamp = _stamp(os.path.join(root, *tile_stream.dsf_rel(*cell).split("/")))
    return stamp is not None and patched.get(_cell_key(cell)) == stamp


def applies(marker: dict) -> bool:
    """Whether the package's set needs the patch (the XP11 set)."""
    return marker.get("kind", "ortho") == "ortho" and marker.get("source") == SEASONS_SET


def pending_tiles(root: str) -> List[Cell]:
    """Kept tiles whose stored DSF hasn't been patched."""
    patched = read_patched(root)
    out = []
    for cell in sorted(tile_stream.read_pins(root)):
        path = os.path.join(root, *tile_stream.dsf_rel(*cell).split("/"))
        if os.path.exists(path) and not is_patched(root, cell, patched):
            out.append(cell)
    return out


def _default_utils():
    if __package__:
        from .utils.dsf_utils import DsfUtils
    else:
        from utils.dsf_utils import DsfUtils
    return DsfUtils()


def patch_tile(root: str, cell: Cell, utils=None) -> bool:
    """Add seasons to one stored tile. Returns True if it now has them (or
    Global Scenery has none to copy for it)."""
    if __package__:
        from .utils.dsf_utils import SeasonsAddResult
    else:
        from utils.dsf_utils import SeasonsAddResult
    path = os.path.join(root, *tile_stream.dsf_rel(*cell).split("/"))
    if not os.path.exists(path):
        return False
    utils = utils or _default_utils()
    cache_dir = os.path.join(utils.dsf_dir, str(uuid.uuid4()))
    result = utils.add_seasons_to_dsf_file(path, cache_dir, backup_dir=None)
    if result in (SeasonsAddResult.ADDED, SeasonsAddResult.XP_TILE_MISSING):
        _record(root, cell, _stamp(path))
        return True
    log.warning("Seasons: could not patch %s", path)
    return False


def patch_tiles(root: str, cells: Iterable[Cell], workers: int = 2,
                progress: Optional[Callable[[int, int], None]] = None,
                cancelled: Callable[[], bool] = lambda: False,
                utils=None) -> Tuple[int, int]:
    """Patch several tiles. Returns (patched, failed)."""
    cells = list(cells)
    utils = utils or _default_utils()
    done = ok = failed = 0

    def one(cell):
        if cancelled():
            return None
        return patch_tile(root, cell, utils)

    with ThreadPoolExecutor(max_workers=max(1, workers),
                            thread_name_prefix="stream-seasons") as pool:
        for future in as_completed([pool.submit(one, c) for c in cells]):
            try:
                result = future.result()
            except Exception as exc:
                log.warning("Seasons: patching failed: %s", exc)
                result = False
            if result is None:
                continue
            done += 1
            ok += bool(result)
            failed += not result
            if progress:
                progress(done, len(cells))
    return ok, failed

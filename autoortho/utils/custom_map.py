"""Custom Map configuration: per-cell maptype assignments for 1-degree lat/lon grid."""
import glob
import json
import math
import os
import re
import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".autoortho-data", "custom_map.json")


def _cell_key(lat: float, lon: float) -> str:
    """Convert lat/lon to cell key using floor to get the 1-degree cell."""
    return f"{math.floor(lat)},{math.floor(lon)}"


class CustomMapConfig:
    """Manages per-cell maptype assignments stored in a JSON file."""

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._cells: dict[str, str] = {}
        self._load()

    def _load(self):
        """Load cells from disk if the file exists."""
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and "cells" in data:
                self._cells = dict(data["cells"])
            log.info(f"Loaded {len(self._cells)} custom map cells from {self._path}")
        except Exception:
            log.warning(f"Failed to load custom map config from {self._path}", exc_info=True)

    def _save(self):
        """Persist current cells to disk."""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {"version": 1, "cells": self._cells}
        try:
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            log.warning(f"Failed to save custom map config to {self._path}", exc_info=True)

    def get_maptype(self, lat: float, lon: float) -> Optional[str]:
        """Look up the maptype for the cell containing (lat, lon). Returns None if unassigned."""
        key = _cell_key(lat, lon)
        return self._cells.get(key)

    def set_cells(self, assignments: dict[str, str]):
        """Bulk set cells and save. assignments maps cell keys to maptypes."""
        with self._lock:
            self._cells.update(assignments)
            self._save()

    def remove_cells(self, keys: list[str]):
        """Remove cells by key and save."""
        with self._lock:
            for k in keys:
                self._cells.pop(k, None)
            self._save()

    def clear(self):
        """Remove all cell assignments."""
        with self._lock:
            self._cells.clear()
            self._save()

    def get_all_cells(self) -> dict[str, str]:
        """Return a copy of all cell assignments."""
        return dict(self._cells)

    def export_json(self) -> str:
        """Export current config as a JSON string."""
        return json.dumps({"version": 1, "cells": self._cells}, indent=2)

    def import_json(self, json_str: str, merge: bool = False):
        """Import cells from a JSON string. If merge=True, overlay on existing; otherwise replace."""
        data = json.loads(json_str)
        if not isinstance(data, dict) or "cells" not in data:
            raise ValueError("Invalid custom map JSON: missing 'cells' key")
        with self._lock:
            if not merge:
                self._cells.clear()
            self._cells.update(data["cells"])
            self._save()


DSF_FILENAME_RE = re.compile(r'^([+-]\d{2})([+-]\d{3})\.dsf$')


def discover_dsf_tiles(ao_scenery_path: str) -> list[str]:
    """Scan scenery packages for DSF files and return available cell keys.

    Each DSF file like +35+012.dsf corresponds to a 1-degree tile at lat=35, lon=12.
    The DSFs live under: ao_scenery_path/<package>/Earth nav data/<10-deg-grid>/<dsf>.
    """
    available = set()
    if not os.path.isdir(ao_scenery_path):
        log.warning(f"Scenery path does not exist: {ao_scenery_path}")
        return []

    for dsf_path in glob.iglob(os.path.join(ao_scenery_path, "*", "Earth nav data", "*", "*.dsf")):
        filename = os.path.basename(dsf_path)
        m = DSF_FILENAME_RE.match(filename)
        if m:
            lat = int(m.group(1))
            lon = int(m.group(2))
            available.add(f"{lat},{lon}")

    log.info(f"Discovered {len(available)} available DSF tiles from {ao_scenery_path}")
    return sorted(available)


_singleton: Optional[CustomMapConfig] = None
_singleton_lock = threading.Lock()


def get_custom_map_config() -> CustomMapConfig:
    """Get the module-level singleton CustomMapConfig instance."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CustomMapConfig()
    return _singleton


def reload_custom_map_config() -> CustomMapConfig:
    """Force-reload the singleton from disk. Returns the refreshed instance."""
    global _singleton
    with _singleton_lock:
        _singleton = CustomMapConfig()
    return _singleton

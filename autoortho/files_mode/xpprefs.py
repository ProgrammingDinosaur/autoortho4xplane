"""What files mode reads from X-Plane's own files and processes."""

import json
import logging
import os
import re

log = logging.getLogger(__name__)

# X-Plane 12 on Windows writes both; the higher notch is used (building more
# is never wrong).
TEX_RES_KEYS = ("_tex_res", "renopt_tex_res_04")
_LINE_RE = re.compile(r"^(\S+)\s+(.*)$")


def prefs_dir(xplane_path):
    return os.path.join(xplane_path, "Output", "preferences")


def _read_prf(path):
    values = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _LINE_RE.match(line.strip())
                if match:
                    values[match.group(1)] = match.group(2)
    except OSError:
        return None
    return values


class XPlanePrefs:
    """Texture quality and the saved start, re-read when the files change."""

    def __init__(self, xplane_path):
        self.xplane_path = xplane_path or ""
        self._mtimes = {}
        self.tex_res = None
        self.last_start = None
        self.last_start_changed = 0

    def _changed(self, path):
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return False
        if self._mtimes.get(path) == mtime:
            return False
        self._mtimes[path] = mtime
        return True

    def refresh(self):
        folder = prefs_dir(self.xplane_path)
        xp = os.path.join(folder, "X-Plane.prf")
        if self._changed(xp):
            values = _read_prf(xp) or {}
            notches = []
            for key in TEX_RES_KEYS:
                try:
                    notches.append(int(values[key]))
                except (KeyError, ValueError):
                    pass
            self.tex_res = max(notches) if notches else None
            log.info("X-Plane texture quality notch: %s", self.tex_res)
        ff = os.path.join(folder, "Freeflight.prf")
        if self._changed(ff):
            values = _read_prf(ff) or {}
            start = values.get("_last_start")
            if start != self.last_start:
                if self.last_start is not None:
                    self.last_start_changed += 1
                self.last_start = start
        return self

    def last_start_info(self):
        try:
            return json.loads(self.last_start) if self.last_start else None
        except ValueError:
            return None


def xplane_running():
    """True when an X-Plane process is running on this machine."""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if name.startswith("x-plane") and "installer" not in name:
            return True
    return False

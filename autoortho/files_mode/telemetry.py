"""Phase 0: what X-Plane reads, recorded in FUSE mode (plan section 10).

FUSE mode sees every real read X-Plane makes. For each texture and level,
the first read X-Plane makes of it is written to a JSON-lines file with what
files mode needs to check its assumptions A1-A6: the distance from the
aircraft to the texture's centre (ground and height separately), the
texture's size, the screen width and field of view, X-Plane's texture
quality, and how many times the texture was opened (A1: a new open per
sharper level).

Files: ``~/.autoortho-data/telemetry/reads-<date>-<pid>.jsonl``, local only,
at most ``MAX_SAMPLES`` lines per session. ``tools/files_mode/fit_rings.py``
turns them into ``rings.json``. Turned off with
``[diagnostics] read_telemetry = False``.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

MAX_SAMPLES = 200_000
MAX_FILES = 30
FLUSH_EVERY = 200
DIR = os.path.join(os.path.expanduser("~"), ".autoortho-data", "telemetry")


class ReadTelemetry:
    def __init__(self, directory=DIR, enabled=True, prefs=None, clock=time.time,
                 max_samples=MAX_SAMPLES):
        self.directory = directory
        self.enabled = bool(enabled)
        self.prefs = prefs
        self.clock = clock
        self.max_samples = int(max_samples)
        self._seen = set()
        self._buffer = []
        self._count = 0
        self._lock = threading.Lock()
        self._path = None
        self._prefs_checked = 0.0

    @property
    def path(self):
        if self._path is None:
            os.makedirs(self.directory, exist_ok=True)
            self._prune()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._path = os.path.join(self.directory, f"reads-{stamp}-{os.getpid()}.jsonl")
        return self._path

    def _prune(self):
        try:
            files = sorted(
                (f for f in os.listdir(self.directory) if f.startswith("reads-")),
                key=lambda f: os.path.getmtime(os.path.join(self.directory, f)),
            )
            for name in files[:-MAX_FILES]:
                os.remove(os.path.join(self.directory, name))
        except OSError:
            pass

    def _tex_res(self):
        if self.prefs is None:
            return None
        now = self.clock()
        if now - self._prefs_checked > 30:
            self._prefs_checked = now
            try:
                self.prefs.refresh()
            except Exception:
                pass
        return getattr(self.prefs, "tex_res", None)

    def note(self, row, col, maptype, zoom, mip, top_px, ground_m, height_m, size_m,
             width_px=0.0, fov_deg=0.0, opens=0, aircraft=None):
        """A read of level ``mip`` (the first per texture and level is kept)."""
        if not self.enabled:
            return False
        key = (row, col, maptype, zoom, mip)
        with self._lock:
            if key in self._seen or self._count >= self.max_samples:
                return False
            self._seen.add(key)
            first_of_texture = not any(
                (row, col, maptype, zoom, m) in self._seen for m in range(16) if m != mip
            )
            sample = {
                "t": round(self.clock(), 2), "row": row, "col": col, "mt": maptype,
                "z": zoom, "mip": int(mip), "px": int(top_px),
                "ground_m": round(float(ground_m), 1), "agl_m": round(float(height_m), 1),
                "size_m": round(float(size_m), 1), "width": round(float(width_px or 0)),
                "fov": round(float(fov_deg or 0), 1), "tex_res": self._tex_res(),
                "opens": int(opens), "first": first_of_texture,
            }
            if aircraft is not None:
                sample["lat"], sample["lon"] = round(aircraft[0], 5), round(aircraft[1], 5)
            self._buffer.append(sample)
            self._count += 1
            if len(self._buffer) >= FLUSH_EVERY:
                self._flush_locked()
        return True

    def _flush_locked(self):
        if not self._buffer:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                for sample in self._buffer:
                    handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.debug("Read telemetry not written: %s", exc)
            self.enabled = False
        self._buffer.clear()

    def flush(self):
        with self._lock:
            self._flush_locked()

    @property
    def count(self):
        return self._count


_instance = None
_instance_lock = threading.Lock()


def recorder(cfg=None):
    """The process's recorder (created on first use, from the config)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            enabled = True
            prefs = None
            try:
                if cfg is None:
                    try:
                        from autoortho.aoconfig import CFG as cfg
                    except ImportError:
                        from aoconfig import CFG as cfg
                value = getattr(getattr(cfg, "diagnostics", None), "read_telemetry", True)
                enabled = str(value).strip().lower() in ("true", "1", "yes", "on")
                from .xpprefs import XPlanePrefs
                prefs = XPlanePrefs(getattr(cfg.paths, "xplane_path", ""))
            except Exception:
                pass
            _instance = ReadTelemetry(enabled=enabled, prefs=prefs)
            import atexit
            atexit.register(_instance.flush)
        return _instance

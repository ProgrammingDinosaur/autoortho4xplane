"""Start AutoOrtho with counters on its file-system callbacks, for the read-cost benchmark.

Runs ``python -m autoortho`` with the arguments given, after arranging for
three things to be wrapped as their modules load. Nothing in AutoOrtho is
changed on disk.

* ``mfusepy.FUSE._wrapper``: every callback the file-system library makes
  (getattr, open, read, release...) passes through it. Counted and timed per
  callback, textures (``.dds``) apart from everything else. The time is what
  the call spends in Python once it holds the GIL: the client's latency minus
  this is the kernel round trip plus the wait for the GIL.
* ``DiskTexture.read``: the direct read itself.
* ``getortho.bump``: AutoOrtho's counters (a lone mount worker logs no STATS).

The totals are written as JSON once a second to the file named by
``AO_PROBE_OUT``.

    AO_RUN_MODE=mount_worker AO_PROBE_OUT=probe.json python ao_probe.py --root ... --mountpoint ...
"""

from __future__ import annotations

import importlib.abc
import json
import os
import runpy
import sys
import threading
import time
from collections import defaultdict

OUT = os.environ.get("AO_PROBE_OUT")
calls = defaultdict(lambda: [0, 0])  # name -> [count, ns]
bumps = defaultdict(int)
_patched = set()


def _patch_mfusepy(module):
    original = module.FUSE._wrapper
    clock = time.perf_counter_ns

    def wrapper(self, func, *args, **kwargs):
        started = clock()
        try:
            return original(self, func, *args, **kwargs)
        finally:
            spent = clock() - started
            path = args[0] if args else None
            kind = "dds" if isinstance(path, bytes) and path.endswith(b".dds") else "other"
            entry = calls[f"{func.__name__}.{kind}"]
            entry[0] += 1
            entry[1] += spent

    module.FUSE._wrapper = wrapper


def _patch_disk_texture(module):
    original = module.DiskTexture.read
    clock = time.perf_counter_ns

    def read(self, offset, length):
        started = clock()
        try:
            return original(self, offset, length)
        finally:
            entry = calls["DiskTexture.read"]
            entry[0] += 1
            entry[1] += clock() - started

    module.DiskTexture.read = read


def _patch_getortho(module):
    original = module.bump

    def bump(name, value=1, *args, **kwargs):
        try:
            bumps[name] += value
        except Exception:
            pass
        return original(name, value, *args, **kwargs)

    module.bump = bump


PATCHES = {
    "mfusepy": _patch_mfusepy,
    "disk_texture": _patch_disk_texture,
    "getortho": _patch_getortho,
}


class _Hook(importlib.abc.MetaPathFinder):
    """Patch a module right after it has executed, whichever name it loads under."""

    def find_spec(self, fullname, path, target=None):
        short = fullname.rsplit(".", 1)[-1]
        if short not in PATCHES or fullname in _patched:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None or spec.loader is None:
                continue
            loader = spec.loader
            exec_module = loader.exec_module

            def patched_exec(module, _exec=exec_module, _short=short, _name=fullname):
                _exec(module)
                if _name not in _patched:
                    _patched.add(_name)
                    try:
                        PATCHES[_short](module)
                    except Exception as exc:
                        sys.stderr.write(f"ao_probe: could not patch {_name}: {exc}\n")

            loader.exec_module = patched_exec
            return spec
        return None


def _writer():
    while True:
        time.sleep(1.0)
        data = {
            "pid": os.getpid(),
            "patched": sorted(_patched),
            "calls": {name: {"count": v[0], "ns": v[1]} for name, v in list(calls.items())},
            "bumps": dict(bumps),
        }
        try:
            with open(OUT + ".tmp", "w") as handle:
                json.dump(data, handle)
            os.replace(OUT + ".tmp", OUT)
        except OSError:
            pass


def main() -> None:
    # ``python -m autoortho`` needs the repository root on the path, and
    # AutoOrtho's own modules also import each other by their short names.
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    sys.path[0] = repo
    sys.meta_path.insert(0, _Hook())
    if OUT:
        threading.Thread(target=_writer, daemon=True, name="ao-probe").start()
    sys.argv = ["autoortho"] + sys.argv[1:]
    runpy.run_module("autoortho", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()

"""Replay a flight's texture reads against an AutoOrtho mount and measure it.

Performance plan, item 0. Two ways to run:

``--attach``
    AutoOrtho is already running and streaming (started normally, X-Plane
    closed): its parent, mount worker, brokers and stats all count, exactly as
    in a flight. Give its scenery mount with ``--mountpoint``; the parent is
    found by process name, or pass ``--pid``.

``--launch-worker``
    Start one mount worker alone (``AO_RUN_MODE=mount_worker``) for a quick
    worker-only A/B. No brokers or stats server run then; downloads go direct.

The trace is a file recorded with ``AO_READ_TRACE`` (autoortho/read_trace.py)
or made by ``make_scenario.py``. Its ``pos`` events are fed to AutoOrtho by a
fake X-Plane on the dataref UDP port, so ``flight_is_starting()`` goes false
after the load as in a real flight.

Before timing, orphaned broker processes of earlier runs are killed, the run
refuses to start while other processes use more than one core (``--force``
overrides), and the log level the mount runs at is printed (the shipped
default is DEBUG).

    python tools/bench/replay.py --attach --mountpoint "X:/.../z_ao_eur" \\
        --trace tools/bench/scenarios/cruise-cold.trace --label B0 --out runs/

Output: one JSON per run, read by ``compare.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import queue
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import psutil  # noqa: E402

from autoortho import read_trace  # noqa: E402
from fake_xplane import FakeXPlane, TracePositions, wait_for_subscriber  # noqa: E402
from sampler import ProcessTreeSampler  # noqa: E402

# Counters the plan judges items by (STATS lines of the AutoOrtho log).
COUNTERS = (
    "native_teams_released", "native_partial_dds_cleared", "native_partial_strict_build",
    "native_mipmap_threshold_miss", "read_wait_ended_all_settled",
    "dds_direct_opens", "dds_direct_partial_opens", "dds_direct_partial_fallbacks",
    "dds_direct_fallbacks", "mm0_served_exact_bytes", "mm0_served_lower_zl_bytes",
    "mm0_served_missing_bytes", "near_reads_real", "near_reads_standin",
    "prefetch_admission_no_capacity", "chunk_hit", "chunk_miss", "chunk_no_imagery",
    "chunk_no_imagery_remembered", "bytes_dl", "local_cache_write_backpressure",
    "prefetch_lease_held_for_rows", "prefetch_lease_row_hold_expired",
    # Hybrid read path plan: native reads, why opens are not direct, policies.
    "dds_native_opens", "dds_native_reads", "dds_native_deferred",
    "dds_native_deferred_rows", "dds_native_deferred_level", "dds_direct_no_imagery_opens",
    "dds_direct_skip_no_entry", "dds_direct_skip_zoom", "dds_direct_skip_stale",
    "dds_direct_skip_healing", "dds_direct_skip_partial_no_level",
    "dds_direct_skip_partial_coarse", "dds_direct_skip_size", "dds_direct_skip_live_tile",
    "reads_wanted_real", "reads_wanted_standin",
    # Ready share plan: the cache's evictions, partial entries while loading.
    "dds_cache_evicted", "dds_direct_loading_opens", "dds_direct_loading_fallbacks",
    *(f"wait_policy_{p}_{k}" for p in ("quality", "balanced", "smooth")
      for k in ("reads", "read_ms_total", "near_standins")),
)


# --------------------------------------------------------------- prechecks


def kill_orphaned_brokers() -> int:
    """Kill multiprocessing children of this Python whose parent is gone."""
    killed = 0
    me = os.path.normcase(os.path.abspath(sys.executable))
    for proc in psutil.process_iter(["pid", "cmdline", "exe"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "spawn_main" not in cmdline:
                continue
            exe = os.path.normcase(os.path.abspath(proc.info.get("exe") or ""))
            if exe != me:
                continue
            match = re.search(r"parent_pid=(\d+)", cmdline)
            if match and psutil.pid_exists(int(match.group(1))):
                continue
            proc.kill()
            killed += 1
        except (psutil.Error, OSError):
            continue
    return killed


def busy_cores(exclude=(), seconds: float = 3.0) -> float:
    """Cores used by processes outside ``exclude`` (pids), over ``seconds``."""
    exclude = set(exclude) | {os.getpid(), 0}
    procs = []
    for proc in psutil.process_iter(["name"]):
        if proc.pid in exclude:
            continue
        # Windows reports its idle time as the "System Idle Process"'s CPU.
        if (proc.info.get("name") or "").lower() in ("system idle process", "idle"):
            continue
        try:
            proc.cpu_percent(None)
            procs.append(proc)
        except (psutil.Error, OSError):
            continue
    time.sleep(seconds)
    total = 0.0
    for proc in procs:
        try:
            total += proc.cpu_percent(None)
        except (psutil.Error, OSError):
            continue
    return total / 100.0


def find_autoortho_parent() -> int | None:
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "autoortho" not in cmdline.lower() or "spawn_main" in cmdline:
                continue
            if proc.environ().get("AO_RUN_MODE"):
                continue
            return proc.pid
        except (psutil.Error, OSError):
            continue
    return None


def config_log_level(home: str | None = None) -> str:
    path = os.path.join(home or os.path.expanduser("~"), ".autoortho")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip().startswith("file_log_level"):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "DEBUG (default)"


def last_stats(log_path: str | None) -> dict:
    """The last ``STATS: {...}`` line of an AutoOrtho log, as a dict."""
    if not log_path or not os.path.exists(log_path):
        return {}
    last = None
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "STATS: {" in line:
                last = line
    if last is None:
        return {}
    try:
        return ast.literal_eval(last.split("STATS: ", 1)[1].strip())
    except (ValueError, SyntaxError):
        return {}


# ------------------------------------------------------------------ replay


class Replayer:
    def __init__(self, mountpoint: str, events: list, speed: float = 1.0, threads: int = 16):
        self.mountpoint = mountpoint
        self.events = [e for e in events if e.get("op") in ("open", "read", "release")]
        self.speed = max(0.01, float(speed))
        self.started = None
        self.latencies = []
        self.slowest = []
        self.errors = 0
        self.bytes_read = 0
        self._lanes = defaultdict(queue.Queue)
        self._handles = {}
        self._pool = queue.Queue()
        self._threads = [
            threading.Thread(target=self._work, daemon=True, name=f"replay-{i}")
            for i in range(threads)
        ]
        self._lane_busy = set()
        self._lock = threading.Lock()

    def _path(self, trace_path: str) -> str:
        return os.path.join(self.mountpoint, trace_path.lstrip("/\\"))

    def clock(self) -> float:
        if self.started is None:
            return 0.0
        return (time.monotonic() - self.started) * self.speed

    def _run_event(self, event: dict) -> None:
        path = self._path(event["path"])
        op = event["op"]
        if op == "open":
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                self._handles.setdefault(event["path"], []).append(fd)
            except OSError:
                self.errors += 1
        elif op == "read":
            fds = self._handles.get(event["path"])
            fd = fds[-1] if fds else None
            opened_here = False
            if fd is None:
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                    opened_here = True
                except OSError:
                    self.errors += 1
                    return
            started = time.monotonic()
            try:
                os.lseek(fd, int(event["offset"]), os.SEEK_SET)
                data = os.read(fd, int(event["length"]))
                self.bytes_read += len(data)
            except OSError:
                self.errors += 1
                data = b""
            ms = (time.monotonic() - started) * 1000.0
            with self._lock:
                self.latencies.append(ms)
                self.slowest.append((ms, event["path"], event["offset"], event["t"]))
                if len(self.slowest) > 400:
                    self.slowest.sort(reverse=True)
                    del self.slowest[100:]
            if opened_here:
                os.close(fd)
        elif op == "release":
            fds = self._handles.get(event["path"])
            if fds:
                try:
                    os.close(fds.pop())
                except OSError:
                    pass

    def _work(self) -> None:
        while True:
            key = self._pool.get()
            if key is None:
                return
            lane = self._lanes[key]
            while True:
                try:
                    event = lane.get_nowait()
                except queue.Empty:
                    break
                self._run_event(event)
            with self._lock:
                self._lane_busy.discard(key)
                if not lane.empty() and key not in self._lane_busy:
                    self._lane_busy.add(key)
                    self._pool.put(key)

    def run(self) -> float:
        """Replay on the recorded timeline (scaled by ``speed``)."""
        for thread in self._threads:
            thread.start()
        self.started = time.monotonic()
        for event in self.events:
            delay = float(event["t"]) / self.speed - (time.monotonic() - self.started)
            if delay > 0:
                time.sleep(delay)
            key = event["path"]
            self._lanes[key].put(event)
            with self._lock:
                if key not in self._lane_busy:
                    self._lane_busy.add(key)
                    self._pool.put(key)
        # Let every lane drain.
        while True:
            with self._lock:
                busy = bool(self._lane_busy)
            if not busy:
                break
            time.sleep(0.05)
        for _ in self._threads:
            self._pool.put(None)
        for fds in self._handles.values():
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return time.monotonic() - self.started

    def summary(self) -> dict:
        lat = sorted(self.latencies) or [0.0]

        def pct(p):
            return round(lat[min(len(lat) - 1, int(len(lat) * p))], 2)

        self.slowest.sort(reverse=True)
        return {
            "opens": sum(1 for e in self.events if e["op"] == "open"),
            "reads": len(self.latencies),
            "errors": self.errors,
            "bytes_read": self.bytes_read,
            "read_ms_p50": pct(0.50),
            "read_ms_p95": pct(0.95),
            "read_ms_p99": pct(0.99),
            "read_ms_max": round(lat[-1], 2),
            "read_ms_total": round(sum(self.latencies), 1),
            "slowest_reads": [
                {"ms": round(ms, 1), "path": path, "offset": offset, "t": t}
                for ms, path, offset, t in self.slowest[:20]
            ],
        }


def launch_worker(root: str, mountpoint: str, home: str | None, log_file: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["AO_RUN_MODE"] = "mount_worker"
    if home:
        env["USERPROFILE"] = home
        env["HOME"] = home
    handle = open(log_file, "a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "autoortho", "--root", root, "--mountpoint", mountpoint],
        cwd=REPO, env=env, stdout=handle, stderr=subprocess.STDOUT,
    )


def wait_for_mount(mountpoint: str, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(os.path.join(mountpoint, "textures")):
            return True
        time.sleep(0.5)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--attach", action="store_true")
    mode.add_argument("--launch-worker", action="store_true")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--root", help="scenery root for --launch-worker")
    parser.add_argument("--home", help="isolated home (config) for --launch-worker")
    parser.add_argument("--pid", type=int, help="AutoOrtho parent pid for --attach")
    parser.add_argument("--udp-port", type=int, default=49000)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--log", default=os.path.expanduser("~/.autoortho-data/logs/autoortho.log"))
    parser.add_argument("--label", default="run")
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--out", default=os.path.join(HERE, "runs"))
    parser.add_argument("--force", action="store_true", help="run on a busy PC")
    parser.add_argument("--no-feed", action="store_true", help="no fake X-Plane")
    args = parser.parse_args(argv)

    events = read_trace.load(args.trace)
    if not events:
        print(f"{args.trace} holds no events", file=sys.stderr)
        return 2
    killed = kill_orphaned_brokers()
    if killed:
        print(f"Killed {killed} orphaned broker process(es) of earlier runs")

    worker = None
    if args.launch_worker:
        if not args.root:
            parser.error("--launch-worker needs --root")
        worker = launch_worker(args.root, args.mountpoint, args.home, os.path.join(args.out, "worker.log"))
        root_pid = worker.pid
    else:
        root_pid = args.pid or find_autoortho_parent()
        if root_pid is None:
            print("No running AutoOrtho found: start it, or pass --pid", file=sys.stderr)
            return 2

    tree = {root_pid}
    try:
        tree |= {p.pid for p in psutil.Process(root_pid).children(recursive=True)}
    except psutil.Error:
        pass
    others = busy_cores(exclude=tree)
    print(f"Other processes use {others:.2f} cores; mount log level: {config_log_level(args.home)}")
    if others > 1.0 and not args.force:
        print("Refusing to time a busy PC (more than one core in use); --force overrides",
              file=sys.stderr)
        if worker is not None:
            worker.terminate()
        return 3

    replayer = Replayer(args.mountpoint, events, speed=args.speed, threads=args.threads)
    fake = None
    if not args.no_feed and any(e.get("op") == "pos" for e in events):
        fake = FakeXPlane(args.udp_port, TracePositions(events, replayer.clock)).start()

    if args.launch_worker and not wait_for_mount(args.mountpoint):
        print("The mount did not appear", file=sys.stderr)
        worker.terminate()
        return 4
    if fake is not None and not wait_for_subscriber(fake, timeout=30.0):
        print("Warning: AutoOrtho did not subscribe to the fake X-Plane feed;"
              " reads run in loading-screen mode", file=sys.stderr)

    stats_before = last_stats(args.log)
    sampler = ProcessTreeSampler(root_pid).start()
    wall = replayer.run()
    time.sleep(2.0)  # let in-flight background work show up in the samples
    sampler.stop()
    if fake is not None:
        fake.stop()
    stats_after = last_stats(args.log)

    counters = {
        name: (stats_after.get(name, 0) or 0) - (stats_before.get(name, 0) or 0)
        for name in COUNTERS
        if isinstance(stats_after.get(name, 0), (int, float))
    }
    result = {
        "label": args.label,
        "scenario": args.scenario or os.path.splitext(os.path.basename(args.trace))[0],
        "trace": os.path.abspath(args.trace),
        "mode": "attach" if args.attach else "worker",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wall_s": round(wall, 2),
        "speed": args.speed,
        "other_cores": round(others, 2),
        "log_level": config_log_level(args.home),
        "feed_packets": fake.packets_sent if fake is not None else 0,
        "reads": replayer.summary(),
        "resources": sampler.summary(),
        "counters": counters,
    }
    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(
        args.out, f"{result['scenario']}-{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    print(f"Wrote {out}")
    print(
        f"reads {result['reads']['reads']}  p50 {result['reads']['read_ms_p50']} ms  "
        f"p95 {result['reads']['read_ms_p95']} ms  max {result['reads']['read_ms_max']} ms  "
        f"CPU {result['resources']['total_cpu_s']} s"
    )
    if worker is not None:
        worker.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

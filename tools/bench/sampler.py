"""Per-process resource samples for the replay benchmark.

Samples a process and every descendant once a second: CPU seconds (user and
kernel), working set, private bytes, threads, and bytes read and written.
Processes that exit keep their last sample, so short-lived brokers count.
The roles are guessed from command lines: parent, mount worker, broker.
"""

from __future__ import annotations

import statistics
import threading
import time
from typing import Dict, List, Optional

import psutil


def _role(proc: psutil.Process, root_pid: int) -> str:
    if proc.pid == root_pid:
        return "parent"
    try:
        cmdline = " ".join(proc.cmdline())
        environ = {}
        try:
            environ = proc.environ()
        except (psutil.Error, OSError):
            pass
    except (psutil.Error, OSError):
        return "unknown"
    if environ.get("AO_RUN_MODE") in ("mount_worker", "macfuse_worker") or "mount_worker" in cmdline:
        return "mount-worker"
    if "multiprocessing" in cmdline or "spawn_main" in cmdline:
        return "broker"
    return "child"


class ProcessTreeSampler:
    def __init__(self, root_pid: int, interval: float = 1.0):
        self.root_pid = int(root_pid)
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._procs: Dict[int, psutil.Process] = {}
        self._roles: Dict[int, str] = {}
        self.samples: Dict[int, List[dict]] = {}
        self.started = time.monotonic()

    def _tree(self) -> List[psutil.Process]:
        try:
            root = psutil.Process(self.root_pid)
        except psutil.Error:
            return []
        procs = [root]
        try:
            procs.extend(root.children(recursive=True))
        except psutil.Error:
            pass
        return procs

    def sample_once(self) -> None:
        now = round(time.monotonic() - self.started, 3)
        for proc in self._tree():
            pid = proc.pid
            if pid not in self._procs:
                self._procs[pid] = proc
                self._roles[pid] = _role(proc, self.root_pid)
            proc = self._procs[pid]
            try:
                with proc.oneshot():
                    cpu = proc.cpu_times()
                    mem = proc.memory_info()
                    threads = proc.num_threads()
                    try:
                        io = proc.io_counters()
                        read_bytes, write_bytes = io.read_bytes, io.write_bytes
                    except (psutil.Error, AttributeError, NotImplementedError):
                        read_bytes = write_bytes = 0
            except (psutil.Error, OSError):
                continue
            self.samples.setdefault(pid, []).append({
                "t": now,
                "user_s": cpu.user,
                "kernel_s": cpu.system,
                "working_set": int(getattr(mem, "wset", mem.rss)),
                "private": int(getattr(mem, "private", getattr(mem, "vms", 0))),
                "threads": int(threads),
                "read_bytes": int(read_bytes),
                "write_bytes": int(write_bytes),
            })

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(self.interval)

    def start(self) -> "ProcessTreeSampler":
        self.sample_once()
        self._thread = threading.Thread(target=self._run, name="BenchSampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.sample_once()

    def summary(self, since: float = 0.0) -> dict:
        """Totals per process and for the tree, counting from ``since`` seconds."""
        processes = []
        for pid, samples in self.samples.items():
            window = [s for s in samples if s["t"] >= since] or samples[-1:]
            first = next((s for s in samples if s["t"] >= since), samples[0])
            if since and samples[0]["t"] < since:
                # CPU and I/O counted from the window start.
                base = max((s for s in samples if s["t"] < since), key=lambda s: s["t"])
            else:
                base = {"user_s": 0.0, "kernel_s": 0.0, "read_bytes": 0, "write_bytes": 0}
            last = samples[-1]
            ws = [s["working_set"] for s in window]
            processes.append({
                "pid": pid,
                "role": self._roles.get(pid, "unknown"),
                "user_s": round(last["user_s"] - base["user_s"], 3),
                "kernel_s": round(last["kernel_s"] - base["kernel_s"], 3),
                "cpu_s": round(
                    last["user_s"] + last["kernel_s"] - base["user_s"] - base["kernel_s"], 3
                ),
                "working_set_median": int(statistics.median(ws)),
                "working_set_peak": int(max(ws)),
                "private_peak": int(max(s["private"] for s in window)),
                "threads_median": int(statistics.median(s["threads"] for s in window)),
                "threads_peak": int(max(s["threads"] for s in window)),
                "read_bytes": int(last["read_bytes"] - base["read_bytes"]),
                "write_bytes": int(last["write_bytes"] - base["write_bytes"]),
                "alive_at_end": last is samples[-1] and first is not None,
            })
        # The tree's memory over time: sum the processes alive at each second.
        timeline: Dict[float, int] = {}
        for samples in self.samples.values():
            for s in samples:
                if s["t"] >= since:
                    key = round(s["t"])
                    timeline[key] = timeline.get(key, 0) + s["working_set"]
        totals = list(timeline.values()) or [0]
        return {
            "processes": sorted(processes, key=lambda p: -p["cpu_s"]),
            "total_cpu_s": round(sum(p["cpu_s"] for p in processes), 3),
            "total_kernel_s": round(sum(p["kernel_s"] for p in processes), 3),
            "total_working_set_median": int(statistics.median(totals)),
            "total_working_set_peak": int(max(totals)),
            "process_count_peak": len(processes),
            "threads_total_median": sum(p["threads_median"] for p in processes),
        }

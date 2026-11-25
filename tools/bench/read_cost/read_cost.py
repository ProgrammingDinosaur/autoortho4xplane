"""What a read of a ready texture costs, and where the time goes.

Stage M0 of docs/developer/hybrid-read-path-plan.md. The same reads (X-Plane's
pattern: the header piece, then from a level's start to the end of the file in
64 KB pieces) are made by a C client (``readbench.c``) against:

1. the DDS cache files themselves, on NTFS: the floor;
2. the same files behind a minimal file system (``minifs.py``) on the same
   kernel driver, with its read callback in Python or in C (``aoprobe.c``):
   the kernel round trip, and the least a Python callback can cost;
3. AutoOrtho's mount with the textures ready (started through ``ao_probe.py``,
   which times its callbacks from the inside).

Subcommands:

``build``   compile the client (``readbench``) and the probe (``aoprobe``) with
            gcc or clang, for this system
``lists``   write the list files a run reads (one per level)
``run``     run the client N times against one target, write one JSON
``report``  print runs side by side

The servers (AutoOrtho's worker, minifs) are started by hand; see README.md.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics
import subprocess
import sys
import time

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
PIECE = 65536
HEADER = 128


def level_start(width: int, level: int, block: int = 8) -> int:
    """Byte offset where ``level`` starts in a square DDS of ``width`` px."""
    pos, w = HEADER, width
    for _ in range(level):
        pos += max(1, (w * w) >> 4) * block
        w >>= 1
    return pos


def cmd_build(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    gcc = args.gcc
    if sys.platform == "win32":
        client, probe, flags = "readbench.exe", "aoprobe.dll", ["-shared", "-static-libgcc"]
        threads = []
    else:
        client = "readbench"
        probe = "aoprobe.dylib" if sys.platform == "darwin" else "aoprobe.so"
        flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
        threads = ["-pthread"]
    subprocess.check_call([gcc, "-O2", "-Wall", *threads, "-o", os.path.join(args.out, client),
                           os.path.join(HERE, "readbench.c")])
    subprocess.check_call([gcc, "-O2", "-Wall", *flags, "-o",
                           os.path.join(args.out, probe), os.path.join(HERE, "aoprobe.c")])
    return 0


def cmd_lists(args) -> int:
    """One list per level: ``<prefix>-L<level>.txt`` (X-Plane's 64 KB aligned start)."""
    names = [line.strip() for line in open(args.names) if line.strip()]
    for level in args.levels:
        start = -1 if level < 0 else (level_start(args.width, level) // PIECE) * PIECE
        path = f"{args.prefix}-L{'hdr' if level < 0 else level}.txt"
        with open(path, "w") as out:
            for name in names:
                out.write(f"{os.path.join(args.dir, name)}\t{start}\n")
        print(path, len(names), "textures, start", start)
    return 0


def _pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]


def _summary(values_ns):
    us = [v / 1000.0 for v in values_ns]
    if not us:
        return {"n": 0}
    return {
        "n": len(us), "mean": round(statistics.fmean(us), 2), "p50": round(_pct(us, 0.50), 2),
        "p95": round(_pct(us, 0.95), 2), "p99": round(_pct(us, 0.99), 2), "max": round(max(us), 1),
    }


def _cpu(pids):
    out = {}
    for pid in pids:
        try:
            t = psutil.Process(pid).cpu_times()
            out[str(pid)] = (t.user, t.system)
        except psutil.Error:
            pass
    return out


def _read_probe(path):
    if not path:
        return None
    for _ in range(20):
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            time.sleep(0.1)
    return None


def _probe_delta(before, after):
    if not before or not after:
        return None
    out = {"calls": {}, "bumps": {}}
    for kind in ("calls",):
        for name, value in after.get(kind, {}).items():
            old = before.get(kind, {}).get(name, {"count": 0, "ns": 0})
            count = value["count"] - old["count"]
            if count:
                ns = value["ns"] - old["ns"]
                out["calls"][name] = {"count": count, "us_each": round(ns / count / 1000.0, 2),
                                      "ms_total": round(ns / 1e6, 1)}
    for name, value in after.get("bumps", {}).items():
        delta = value - before.get("bumps", {}).get(name, 0)
        if delta:
            out["bumps"][name] = delta
    return out


def cmd_run(args) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    pids = [int(p) for p in args.pid]
    system_pid = 4
    runs = []
    for index in range(args.repeat):
        csv_path = args.out + f".{index}.csv"
        probe_before = _read_probe(args.probe)
        stats_before = _read_probe(args.minifs_stats)
        cpu_before = _cpu(pids + [system_pid])
        machine_before = psutil.cpu_times()
        started = time.perf_counter()
        rc = subprocess.call([args.readbench, args.list, str(args.threads), csv_path])
        wall = time.perf_counter() - started
        machine_after = psutil.cpu_times()
        cpu_after = _cpu(pids + [system_pid])
        if args.probe or args.minifs_stats:
            time.sleep(1.3)  # the servers write their counters once a second
        probe_after = _read_probe(args.probe)
        stats_after = _read_probe(args.minifs_stats)

        opens, reads, closes, textures, hashes = [], [], [], {}, {}
        first_reads, body_reads = [], []
        per_texture = {}
        client = {}
        total_bytes = 0
        with open(csv_path) as handle:
            for row in csv.reader(handle):
                kind = row[0]
                if kind == "O":
                    opens.append(int(row[2]))
                    per_texture[row[1]] = per_texture.get(row[1], 0) + int(row[2])
                elif kind == "R":
                    ns = int(row[4])
                    reads.append(ns)
                    (first_reads if row[2] == "0" else body_reads).append(ns)
                    total_bytes += int(row[3])
                    per_texture[row[1]] = per_texture.get(row[1], 0) + ns
                elif kind == "C":
                    closes.append(int(row[2]))
                    per_texture[row[1]] = per_texture.get(row[1], 0) + int(row[2])
                elif kind == "T":
                    hashes[row[1]] = row[4]
                elif kind == "P":
                    client = {"wall_s": int(row[1]) / 1e9, "user_s": int(row[2]) / 1e9,
                              "kernel_s": int(row[3]) / 1e9}
        if not args.keep_csv:
            os.remove(csv_path)

        def delta(pid):
            a, b = cpu_before.get(str(pid)), cpu_after.get(str(pid))
            if not a or not b:
                return None
            return {"user_s": round(b[0] - a[0], 3), "kernel_s": round(b[1] - a[1], 3)}

        busy = lambda t: t.user + t.system
        run = {
            "rc": rc, "wall_s": round(wall, 3), "textures": len(per_texture),
            "reads": len(reads), "bytes": total_bytes,
            "open_us": _summary(opens), "read_us": _summary(reads),
            "header_read_us": _summary(first_reads), "body_read_us": _summary(body_reads),
            "close_us": _summary(closes),
            "texture_ms": {k: round(v / 1000.0, 3) for k, v in
                           _summary(list(per_texture.values())).items()},
            "client_cpu": client,
            "server_cpu": {str(pid): delta(pid) for pid in pids},
            "system_process_cpu": delta(system_pid),
            "machine_busy_s": round(busy(machine_after) - busy(machine_before), 3),
            "probe": _probe_delta(probe_before, probe_after),
            "minifs": ({k: stats_after[k] - stats_before.get(k, 0) for k in stats_after}
                       if stats_before and stats_after else None),
            "hashes": hashes,
        }
        runs.append(run)
        r = run["read_us"]
        print(f"{args.label} run {index}: {run['textures']} textures, {run['reads']} reads, "
              f"read p50 {r.get('p50')} p95 {r.get('p95')} p99 {r.get('p99')} max {r.get('max')} us, "
              f"texture mean {run['texture_ms'].get('mean')} ms, rc {rc}")
        time.sleep(args.pause)
    result = {
        "label": args.label, "target": args.target, "list": args.list, "threads": args.threads,
        "note": args.note, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": runs,
    }
    with open(args.out, "w") as handle:
        json.dump(result, handle, indent=1)
    return 0


def cmd_report(args) -> int:
    files = []
    for pattern in args.files:
        files.extend(sorted(glob.glob(pattern)) or [pattern])
    rows = []
    for path in files:
        data = json.load(open(path))
        runs = data["runs"][args.skip:] or data["runs"]
        med = lambda get: statistics.median([get(r) for r in runs])
        server = lambda r: sum((v["user_s"] + v["kernel_s"]) for v in r["server_cpu"].values() if v)
        reads = med(lambda r: r["reads"]) or 1
        rows.append((
            data["label"], data["threads"], int(reads),
            med(lambda r: r["read_us"]["p50"]), med(lambda r: r["read_us"]["p95"]),
            med(lambda r: r["read_us"]["p99"]), med(lambda r: r["read_us"]["max"]),
            med(lambda r: r["open_us"]["p50"]), med(lambda r: r["texture_ms"]["mean"]),
            med(lambda r: (r["client_cpu"]["user_s"] + r["client_cpu"]["kernel_s"])) / reads * 1e6,
            med(server) / reads * 1e6,
            med(lambda r: r["machine_busy_s"]) / reads * 1e6,
        ))
    head = ("label", "thr", "reads", "rd p50", "rd p95", "rd p99", "rd max", "open p50",
            "tex ms", "client us/rd", "server us/rd", "machine us/rd")
    print(" | ".join(head))
    for row in rows:
        print(" | ".join(f"{v:.1f}" if isinstance(v, float) else str(v) for v in row))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--gcc", default="gcc")
    b.set_defaults(func=cmd_build)

    l = sub.add_parser("lists")
    l.add_argument("--names", required=True, help="file with one texture file name per line")
    l.add_argument("--dir", required=True, help="folder the names are read from")
    l.add_argument("--prefix", required=True)
    l.add_argument("--width", type=int, default=4096)
    l.add_argument("--levels", type=int, nargs="+", default=[0, 1, 3], help="-1: header piece only")
    l.set_defaults(func=cmd_lists)

    r = sub.add_parser("run")
    r.add_argument("--readbench", required=True)
    r.add_argument("--list", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--target", default="")
    r.add_argument("--threads", type=int, default=1)
    r.add_argument("--repeat", type=int, default=3)
    r.add_argument("--pause", type=float, default=1.0)
    r.add_argument("--pid", action="append", default=[], help="server process to charge CPU to")
    r.add_argument("--probe", help="ao_probe.py's JSON")
    r.add_argument("--minifs-stats", help="minifs.py's JSON")
    r.add_argument("--note", default="")
    r.add_argument("--keep-csv", action="store_true")
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    p.add_argument("--skip", type=int, default=0, help="leave out the first N runs of each file")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

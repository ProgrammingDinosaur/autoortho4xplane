"""Print replay runs side by side, with the performance plan's gates.

    python tools/bench/compare.py runs/cruise-cold-B0-*.json runs/cruise-cold-new-*.json

The first file is the baseline. With two runs of the same scenario and label
it also checks step 0's repeatability (total CPU within 5 %).

Gates and checks (docs/developer/performance-gap-plan.md):

* step 6: ``native_teams_released`` x 0.65 ms below 1 % of the worker's CPU
  seconds closes the item;
* step 2: diagnostics on costs at most 1 % more CPU than off (label the runs
  ``diag-on`` and ``diag-off``);
* definition of done 5 to 8 against the baseline, per scenario.
"""

from __future__ import annotations

import argparse
import json
import sys

TEAM_REBUILD_MS = 0.65


def load(path):
    with open(path, encoding="utf-8") as handle:
        run = json.load(handle)
    run["_path"] = path
    return run


def role_total(run, role, key):
    return sum(p.get(key, 0) for p in run["resources"]["processes"] if p["role"] == role)


def metrics(run):
    res = run["resources"]
    reads = run["reads"]
    counters = run.get("counters", {})
    opens = counters.get("dds_direct_opens", 0)
    helpers = sum(
        p["working_set_median"] for p in res["processes"]
        if p["role"] not in ("mount-worker", "parent")
    )
    return {
        "wall s": run["wall_s"],
        "total CPU s": res["total_cpu_s"],
        "kernel CPU s": res["total_kernel_s"],
        "worker CPU s": role_total(run, "mount-worker", "cpu_s"),
        "worker kernel s": role_total(run, "mount-worker", "kernel_s"),
        "parent CPU s": role_total(run, "parent", "cpu_s"),
        "broker CPU s": role_total(run, "broker", "cpu_s"),
        "memory median MB": round(res["total_working_set_median"] / 1e6, 1),
        "memory peak MB": round(res["total_working_set_peak"] / 1e6, 1),
        "worker WS median MB": round(role_total(run, "mount-worker", "working_set_median") / 1e6, 1),
        "helpers WS MB": round(helpers / 1e6, 1),
        "processes": res["process_count_peak"],
        "threads": res["threads_total_median"],
        "worker read MB": round(role_total(run, "mount-worker", "read_bytes") / 1e6, 1),
        "worker write MB": round(role_total(run, "mount-worker", "write_bytes") / 1e6, 1),
        "reads": reads["reads"],
        "read p50 ms": reads["read_ms_p50"],
        "read p95 ms": reads["read_ms_p95"],
        "read max ms": reads["read_ms_max"],
        "direct opens": opens,
        "direct partial opens": counters.get("dds_direct_partial_opens", 0),
        "teams released": counters.get("native_teams_released", 0),
    }


def table(runs):
    rows = [metrics(r) for r in runs]
    names = list(rows[0])
    headers = [f"{r['scenario']}/{r['label']}" for r in runs]
    width = max(len(n) for n in names) + 2
    col = max(14, max(len(h) for h in headers) + 2)
    print("".ljust(width) + "".join(h.rjust(col) for h in headers) + "   change".rjust(10))
    for name in names:
        values = [row[name] for row in rows]
        line = name.ljust(width) + "".join(f"{v:>{col}}" for v in values)
        base, last = values[0], values[-1]
        if isinstance(base, (int, float)) and base:
            line += f"{(last - base) / base * 100:+9.1f}%"
        print(line)


def verdict(ok):
    return "PASS" if ok else "FAIL"


def gates(runs):
    base, cand = runs[0], runs[-1]
    mb, mc = metrics(base), metrics(cand)
    print("\nGates and checks")
    # Step 6 gate, on the baseline.
    worker_cpu = mb["worker CPU s"] or 1e-9
    share = mb["teams released"] * TEAM_REBUILD_MS / 1000.0 / worker_cpu
    print(f"  step 6 gate: team rebuilds {share * 100:.2f} % of worker CPU "
          f"-> {'close the item' if share < 0.01 else 'build the stable lanes'}")
    # Step 0: repeatability.
    if base["scenario"] == cand["scenario"] and base["label"] == cand["label"]:
        diff = abs(mc["total CPU s"] - mb["total CPU s"]) / max(1e-9, mb["total CPU s"])
        print(f"  step 0 repeatability: {diff * 100:.1f} % apart  {verdict(diff < 0.05)}")
    # Step 2: diagnostics overhead.
    labels = {r["label"]: metrics(r) for r in runs}
    if "diag-on" in labels and "diag-off" in labels:
        on, off = labels["diag-on"]["total CPU s"], labels["diag-off"]["total CPU s"]
        cost = (on - off) / max(1e-9, off)
        print(f"  step 2 diagnostics cost: {cost * 100:+.2f} %  {verdict(cost <= 0.01)}")
    scenario = cand["scenario"]
    if scenario.startswith("cruise-cold"):
        cpu = (mb["total CPU s"] - mc["total CPU s"]) / max(1e-9, mb["total CPU s"])
        kernel = (mb["worker kernel s"] - mc["worker kernel s"]) / max(1e-9, mb["worker kernel s"])
        print(f"  DoD 5: total CPU down {cpu * 100:.1f} % (>= 30)  {verdict(cpu >= 0.30)}")
        print(f"  DoD 5: worker kernel down {kernel * 100:.1f} % (>= 50)  {verdict(kernel >= 0.50)}")
    if scenario.startswith("cruise-cached"):
        opens = cand.get("counters", {}).get("dds_direct_opens", 0)
        total_opens = cand["reads"].get("opens") or 0
        share = opens / total_opens if total_opens else 0.0
        ws = (mb["worker WS median MB"] - mc["worker WS median MB"]) / max(1e-9, mb["worker WS median MB"])
        print(f"  DoD 6: direct opens {share * 100:.0f} % of {total_opens} (>= 70)  {verdict(share >= 0.70)}")
        print(f"  DoD 6: worker median working set down {ws * 100:.1f} % (>= 25)  {verdict(ws >= 0.25)}")
    if scenario.startswith("load"):
        print(f"  DoD 7: slowest read {mc['read max ms']} ms (baseline {mb['read max ms']} ms); "
              f"time to the last read {mc['wall s']} s vs {mb['wall s']} s  "
              f"{verdict(mc['wall s'] <= mb['wall s'] * 1.0)}")
    print(f"  DoD 8: processes {mc['processes']} (<= 4) {verdict(mc['processes'] <= 4)}, "
          f"threads {mc['threads']} (< 150) {verdict(mc['threads'] < 150)}, "
          f"helpers {mc['helpers WS MB']} MB (< 400) {verdict(mc['helpers WS MB'] < 400)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+")
    args = parser.parse_args(argv)
    runs = [load(path) for path in args.runs]
    table(runs)
    gates(runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())

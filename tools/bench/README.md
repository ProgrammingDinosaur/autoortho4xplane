# Replay benchmark

The tools the performance plan ([docs/developer/performance-gap-plan.md](../../docs/developer/performance-gap-plan.md))
judges its items by. Every figure there should come from these, run on an idle PC.

| File | What it does |
|---|---|
| `replay.py` | Replays a trace of X-Plane's texture opens and reads against a mount, feeds positions through a fake X-Plane, samples every AutoOrtho process, writes one JSON per run |
| `compare.py` | Prints runs side by side and evaluates the plan's gates and definition-of-done checks against the first (baseline) run |
| `make_scenario.py` | Makes a synthetic trace along a straight route from a mount's texture list, until a recorded one exists |
| `fake_xplane.py` | Answers AutoOrtho's dataref (RREF) subscriptions with the trace's positions |
| `sampler.py` | Per-process CPU seconds (user, kernel), working set, private bytes, threads, read and write bytes |
| `chunk_store_gate.py` | Step 3's decision gate: chunk writes and texture reads, one file per chunk against the packed store |

## 1. Record a trace

A recorded trace is X-Plane's real reading. Start AutoOrtho with `AO_READ_TRACE`
naming the trace file; the mount worker inherits the variable and appends every
DDS open, read and release, and the aircraft position once a second:

```powershell
$env:AO_READ_TRACE = "D:\ao-traces\vagar-east.trace"; python -m autoortho
```

Fly the route, then quit. Cut the trace into the plan's three scenarios (the
first five minutes for `load`, twenty minutes of cruise for the other two) with
any text tool: one JSON object per line, `t` in seconds. Shift `t` so each
scenario starts at 0.

Without a recording, `make_scenario.py` imitates X-Plane's reading pattern
(read_pattern.py) along a straight route; say so when quoting its results:

```powershell
python tools/bench/make_scenario.py --mountpoint "X:\Custom Scenery\z_ao_eur" --lat 62.06 --lon -7.27 --heading 90 --minutes 20 --out tools/bench/scenarios/cruise.trace
```

## 2. Run

Start AutoOrtho normally with the scenery mounted and X-Plane closed (the fake
X-Plane takes its UDP port, 49000 by default). For a cold run, clear the cache
first (Settings, Cache, Delete cache); for a warm run, replay the same trace
again.

```powershell
python tools/bench/replay.py --attach --mountpoint "X:\Custom Scenery\z_ao_eur" --trace tools/bench/scenarios/cruise.trace --scenario cruise-cold --label B0
```

The driver kills orphaned broker processes of earlier runs, refuses to start
while other processes use more than one core (`--force` overrides), and prints
the mount's log level (the shipped default, DEBUG, is what users run).

`--launch-worker --root ... --home ...` starts a mount worker alone instead
(no brokers, no stats server): quick, but not a whole-app measurement. The
position feed needs the parent too (it runs the dataref tracker and shares the
position with workers through the stats store), so a lone worker reads in
loading-screen mode.

Run each scenario three times. Profile with py-spy during `cruise-cold`:

```powershell
py-spy record --gil --duration 600 --pid <worker pid> -o worker.svg
```

## 3. Compare

```powershell
python tools/bench/compare.py tools/bench/runs/cruise-cold-B0-*.json tools/bench/runs/cruise-cold-new-*.json
```

The first file is the baseline. The output includes:

* step 0: two runs of one scenario within 5 % of each other's total CPU;
* step 2: diagnostics on against off (label the runs `diag-on` and `diag-off`);
* step 6: `native_teams_released` x 0.65 ms against 1 % of the worker's CPU;
* definition of done 5 to 8 for the scenario of the last run.

Counters come from the `STATS:` lines of the AutoOrtho log (`--log`, the
default log of the current user).

## 4. Against OrthoStudio XP Live

A replay cannot compare the two (OrthoStudio has no virtual disk to replay
reads against). Use OrthoStudio's own flight tooling, which flies X-Plane along
a route with either program: `tools/live/flights/fly.py` with its test-pilot
plugin and `sampler_windows.py` in the OrthoStudio checkout.

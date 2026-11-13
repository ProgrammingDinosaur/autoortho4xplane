# Plan: detecting "behind the aircraft" from prefetch misses

Status: planned, not implemented
Related: [settings-refactor-proposal.md](settings-refactor-proposal.md) §4.4 (the *Behind the aircraft* row of the bottleneck classifier) and its phases 5-6 notes

---

## 1. Problem

The governor's `behind` state is a fallback today. `Governor.classify()`
returns it when fewer than 95 % of near reads are sharp and nothing else
explains it: the network is not saturated, the CPU is not the limit, and
the late-arrival evidence (`wait_evidence()`) does not ask for a longer
wait. In that state the governor lengthens prefetch lookahead and widens the
radius.

That move is a guess. Your proposal's §4.4 defines the state by evidence
("near reads hit tiles that prefetch never queued, line and CPU have room"),
but nothing records which tiles prefetch queued. So the governor can't tell
apart four cases that need different fixes:

| What happened to the tile X-Plane read blurry | Real cause | Right move |
|---|---|---|
| No prefetch path ever queued it | The corridor was too short or too narrow, or prefetch was paused | Longer lookahead or a wider radius, or find the pause |
| It was queued, but not finished when X-Plane read it | Prefetch started too late, or had too little capacity | A longer paging lead, or the existing network/compute rules |
| It was queued only to a coarser level than X-Plane wanted | The paging model under-estimated the detail | A longer paging lead, or report a k mismatch |
| It was finished, then dropped from memory before the read | Memory cache too small | A bigger memory cache, if RAM allows |

Widening prefetch only helps in the first case. In the other three it adds
downloads that compete with X-Plane's own reads.

## 2. Goal

- Classify every blurry near read in flight by what prefetch had done for
  that tile beforehand.
- Base the `behind` state on that classification, and give each cause its
  own move.
- Without evidence, move nothing: today's blind prefetch increase goes away.
- Ship the measurement and the flight-report table first, with no governor
  rule, so the thresholds come from real flights (as Advise did).

Out of scope: changing how the prefetcher chooses tiles. This plan only
measures and tunes the existing knobs.

## 3. Where prefetch queues tiles today

Every path ends in one of two places, so two hooks cover them all:

| Path | Code | Ends in |
|---|---|---|
| Velocity corridor | `SpatialPrefetcher._do_velocity_prefetch_cycle()` | `_prefetch_tile()` |
| SimBrief route / waypoints | `_prefetch_along_flight_plan*()`, `_prefetch_waypoint_area()` | `_prefetch_tile()` |
| Paging warm (coarse levels) | `SpatialPrefetcher.paging_split()` | `_prefetch_tile(first_mip=…)` |
| `.ter` load-ahead | `load_ahead_warm()` | `_prefetch_tile(first_mip=…)` |
| DSF prefetch, paging-coarse tiles | `prefetch_dsf()` (the camera branch) | `_prefetch_tile(first_mip=…)` |
| DSF prefetch, corridor tiles | `prefetch_dsf()` | `PrefetchCoordinator.publish()` |
| `prefetch_tiles()` fallback | `SpatialPrefetcher.prefetch_tiles()` | `_prefetch_tile()` |

When a tile is done:

- `PrefetchCoordinator._finish_candidate(candidate, CandidateState.COMPLETE)` (prefetch.py)
- `BackgroundDDSBuilder` storing a predictive DDS (`dynamic_dds_cache.store`)
- the chunks queued by `_prefetch_tile()` arriving: `TileCompletionTracker` already watches these for predictive DDS

When a tile leaves memory: `TileCacher` eviction (it already records
`_recently_evicted[idx] = now`, getortho.py near line 17952).

When X-Plane reads a tile blurry: `Tile._read_dds_bytes_budgeted()`, in the
branch that bumps `near_reads_standin` (getortho.py).

## 4. Design

### 4.1 Prefetch ledger (per worker process)

Each mount worker runs its own prefetcher and serves its own reads, so the
ledger stays in the worker and no cross-process channel is needed.

New module `autoortho/prefetch_ledger.py`. It must be pure: no getortho
import, and the clock is injected, so tests run without a tile cacher.

```python
@dataclass
class LedgerEntry:
    first_queued: float            # monotonic time of the first queue
    finest_mip: int                # finest level any path queued (0 = full)
    sources: frozenset[str]        # "velocity", "simbrief", "paging", "ter", "dsf"
    done_at: Optional[float] = None
    done_mip: Optional[int] = None # finest level known complete
    evicted_at: Optional[float] = None

class PrefetchLedger:
    MAX_ENTRIES = 8192             # ~100 B each: under 1 MB
    TTL_S = 1200.0                 # 20 min, as the graphics memory meter

    def note_queued(self, key, mip, source, now): ...
    def note_done(self, key, mip, now): ...
    def note_evicted(self, key, now): ...
    def classify_miss(self, key, wanted_mip, now) -> str: ...
```

- **Key:** `(row, col, zoom)`, without the maptype. DSF prefetch may rewrite
  the maptype (`maptype_override`) while reads use the resolved one, so a
  key that includes it would split one tile into two entries. Different
  maptypes for one tile in one session are rare, and merging them only
  makes a miss look covered.
- **Bounded:** an `OrderedDict` used as an LRU, with entries older than
  `TTL_S` dropped on access. One lock, held only for dictionary work.
- `note_queued` keeps the **finest** `mip` and the earliest time, and adds
  the source.
- `note_done` sets `done_at` and `done_mip`; `note_evicted` sets
  `evicted_at`.

`classify_miss(key, wanted_mip, now)` returns, checking in this order:

1. `"never"`: no entry, or the first queue was less than `MIN_LEAD_S`
   (2 s) before the read. A queue that came that late is a reaction to
   X-Plane, not foresight.
2. `"evicted"`: `done_mip <= wanted_mip` and `evicted_at > done_at`.
3. `"coarse"`: `finest_mip > wanted_mip` (only a coarser level was queued).
4. `"late"`: queued at `finest_mip <= wanted_mip`, but not done
   (`done_at is None` or `done_mip > wanted_mip`).
5. `"done"`: complete at the wanted level and not evicted. The read was
   blurry for another reason (a failed chunk, or a level marked as missing
   colour). Counted, not acted on.

### 4.2 Hooks

All hooks are best effort: wrapped in `try`/`except` and logged at debug
level, like `_note_wanted_level()`.

| Hook | Call |
|---|---|
| `SpatialPrefetcher._prefetch_tile()`, after it submitted at least one chunk or found the tile complete | `ledger.note_queued((row, col, zoom), first_mip, source, now)`. `source` is a new keyword argument, passed by each caller from the table in §3 (default `"velocity"`). |
| `prefetch_dsf()`, after `prefetch_coordinator.publish()` returned `created` or the key was already known | `ledger.note_queued(key, 0, "dsf", now)` |
| `PrefetchCoordinator._finish_candidate(…, COMPLETE)` | a new optional `on_complete(key)` callback set by getortho, which calls `ledger.note_done(key, 0, now)` |
| `TileCompletionTracker`, when all of a tile's queued chunks are ready | `ledger.note_done(key, first_mip_of_that_submission, now)` |
| `TileCacher` eviction (`_recently_evicted[idx] = now`) | `ledger.note_evicted(key, now)` |
| `Tile._read_dds_bytes_budgeted()`, near band, not real, **not starting**, X-Plane connected | `kind = ledger.classify_miss(key, wanted, now)`, then `bump(f"near_miss_{kind}")` |

**Geometry for "never" misses.** A tile prefetch never queued either lay
outside the corridor (fix: radius) or inside it while prefetch did not run
(fix: find the pause). To tell which, the read hook also computes the
tile's **cross-track offset** from the aircraft track at the time of the
read, using `current_camera()` and `paging_model.bearing_deg()` /
`ground_distance_m()`, which already exist:

- `|cross_track| > 0.8 × prefetch_radius` → `near_miss_never_wide`
- otherwise → `near_miss_never_inside`

`never_inside` while prefetch was paused points at the pauses: the RAM brake
(`ram_brake_activations`), live pressure
(`LivePressureController`), `prefetch_during_live_reads = False`, or the
runtime gate (`is_prefetch_runtime_allowed()`). Also bump
`near_miss_never_paused` when any of these was active in the last
`prefetch_interval` seconds. `SpatialPrefetcher` records the time of its
last skipped cycle as `self._last_skip_at`.

**Turns.** In a turn, tiles on the outside of the turn are never in a
straight-ahead corridor. The read hook runs in the worker, so the ledger
keeps its own small deque of `(time, track_deg)` from `current_camera()`
(its fourth field), appended at most once a second. When the track changed
by more than 30° in the last 60 s, count the miss as
`near_miss_never_turn` instead of `wide`. A wider radius is the wrong fix
for a turn; SimBrief prefetch already covers planned turns.

### 4.3 Governor

New `Sample` fields (cumulative counters, all workers summed, read like
`near_reads_standin`):

```
near_miss_never_wide, near_miss_never_inside, near_miss_never_paused,
near_miss_never_turn, near_miss_coarse, near_miss_late,
near_miss_evicted, near_miss_done
```

`Window` gets their deltas over the 30 s window.

Classification replaces today's `return "behind"` fallback. It runs only
when fidelity is under `FIDELITY_LOW` and `wait_evidence()` did not return
`"wait"` or `"too_slow"`:

```python
misses = w.miss_counts            # dict kind -> count in the window
total = sum(misses.values())
if total < MIN_MISSES:            # 10 in 30 s
    return "ok"                   # no evidence: move nothing
kind, count = max(misses.items(), key=lambda kv: kv[1])
if count < 0.5 * total:
    return "ok"                   # no single cause
return {
    "never_wide": "behind_wide",
    "never_inside": "behind_short",
    "never_paused": "prefetch_paused",
    "never_turn": "ok",           # turns: nothing to tune
    "coarse": "paging_coarse",
    "late": "prefetch_late",
    "evicted": "memory_evicted",
    "done": "ok",
}[kind]
```

Moves (`_PLAN`):

| State | Move | Note |
|---|---|---|
| `behind_wide` | `prefetch_radius_nm` +1 step | |
| `behind_short` | `prefetch_lookahead` +1 step | Inside the corridor yet never queued: usually the corridor ended too early for the speed |
| `prefetch_paused` | none | Notice naming the pause ("prefetch paused by low memory 40 % of the last 5 minutes"). The RAM brake is the right owner. |
| `paging_coarse` | new lever `streaming.paging_lead_seconds` +1 step | Already read at call time through `streaming_options`, so it is live with no engine change. Range 30-180 s, step 15 s. |
| `prefetch_late` | `streaming.paging_lead_seconds` +1, then `autoortho.prefetch_max_chunks` +1 | Starts prefetch earlier first, then lets it do more per cycle. Skipped when the network is saturated (already a network state). |
| `memory_evicted` | `cache.cache_mem_limit` +1 | Only when not in the `memory` state and RSS is under 80 % of `ram_gb` |

Keep/revert metric for these states: the share of near misses of that kind
(lower is better). `_metric()` returns `-share` for them.

Headroom easing (`state == "headroom"`) already brings
`prefetch_lookahead`, `prefetch_radius_nm` and the new lever back toward
baseline, so no new decay code is needed.

`levers_for()` gains:

```python
LeverSpec("streaming.paging_lead_seconds", "Prefetch lead time",
          15, 30, 180, unit="s", group="prefetch"),
LeverSpec("autoortho.prefetch_max_chunks", "Prefetch chunks per cycle",
          64, 128, 2048, group="prefetch"),
```

Both are already handled live: `prefetch_max_chunks` has a
`live_settings` hook (`_on_live_prefetch`), and `paging_lead_seconds` is
read at call time. Add `paging_lead_seconds` to the registry with
`owner="governed"`, `live=True`.

`STATE_TEXT` and the unreachable notice's "limiting" map gain the new states
in plain words: "Prefetch corridor too narrow", "Prefetch starts too late",
"Prefetch asks for too little detail", "Prefetch paused", "Memory cache
too small". `home_snapshot.BOTTLENECK_GAUGE` maps `memory_evicted` to
`computer` and the others to no gauge, since they are prefetch rather than
a resource.

### 4.4 Transparency

- **Flight report** (`diagnostics._render_flight`): a *Blurry moments by
  cause* table with the share of each miss kind, by phase. It ships in
  step 1, before any governor rule.
- **Tuning page:** the decision reason gives the evidence, for example
  "prefetch corridor too narrow: 14 of 18 blurry tiles lay 6-9 nm off the
  track".
- **Home:** no new block. A `prefetch_paused` notice reaches Home only when
  it repeats (§13.5: raw counters stay off Home).

## 5. Work plan

Each step can ship on its own.

| Step | Content | Files | Tests |
|---|---|---|---|
| 1. Ledger | `PrefetchLedger`, `LedgerEntry`, `classify_miss()` | new `prefetch_ledger.py` | new `tests/test_prefetch_ledger.py`: queue → read (`never` / `late` / `coarse` / `done`), done → evict → read (`evicted`), finest-mip merge, the LRU bound, TTL expiry, `MIN_LEAD_S` |
| 2. Hooks | the `source` argument on `_prefetch_tile()` and its callers; DSF publish; coordinator `on_complete`; completion tracker; eviction; read-side `classify_miss` + geometry + turn + pause tagging | `getortho.py`, `prefetch.py` | extend `tests/test_paging_prefetch.py` and `tests/test_load_ahead.py` (each path records its source and mip); a tile read test like `test_late_arrivals.py::test_tile_watches_…` for the miss counters; coordinator test for `on_complete` |
| 3. Report | *Blurry moments by cause* in the flight report; counters in the timeline JSON | `diagnostics.py`, `governor.py` (summary) | `tests/test_tuning_ui.py::test_report_renders_flight_verdict` extended |
| — | **Fly 5-10 flights with step 3, read the reports, set `MIN_MISSES`, the 50 % dominance share and the 0.8 × radius threshold from them** | | |
| 4. Governor rules | `Sample`/`Window` fields; new states and moves; `paging_lead_seconds` and `prefetch_max_chunks` levers; `_metric` for the new states; remove the blind `behind` fallback | `governor.py`, `settings_registry.py`, `aoconfig.py` (template comment), `home_snapshot.py` | `tests/test_governor.py`: one classifier case per kind, no move without evidence, keep/revert on the miss share, and a replay test over a recorded `samples-*.jsonl` from step 3 |
| 5. Docs | `docs/config.md` governor section; proposal implementation notes | docs | — |

Rough size: step 1 about 150 lines plus tests. Step 2 is the risky one,
since `_prefetch_tile()` has seven callers; about 120 lines. Step 4 is about
120 lines in `governor.py`.

## 6. Risks and decisions

- **The starting phase.** While X-Plane loads an area, every near read is a
  "never" miss, because prefetch can't know where a flight starts. Starting
  is excluded with the same `flight_is_starting()` the wait bands use, plus
  the jump rule in `wait_bands.StartTracker`.
- **Teleports and replays.** Same as starting: the jump detection covers
  them.
- **Lock contention.** `note_queued` runs once per tile submission (at most
  a few hundred per prefetch cycle), and `classify_miss` once per blurry
  near read. One dictionary lookup under a lock: negligible next to the
  chunk work around it.
- **Key mismatch between prefetch and reads.** Prefetch uses the `.ter` zoom
  (`zoom` in `_prefetch_tile(row, col, zoom)`), and reads use
  `tilename_zoom`. Both are the file's zoom, but step 2's tests must assert
  that one tile gives the same key on both sides, including a DSF tile with
  `maptype_override`.
- **Several workers.** Each worker's ledger sees only its own prefetches and
  reads. A tile belongs to one scenery package, so to one worker. The
  governor sums the counters, as it already does for `near_reads_*`.
- **Paging k.** `paging_coarse` often means k is wrong rather than the lead
  too short. Once the fitted k is persisted (the deferred "keep the measured
  paging k" item), a coarse miss share that stays high after the lead
  reached its maximum should become a notice suggesting a k check.
- **Behaviour change.** Removing the blind `behind` fallback means a low
  fidelity with no evidence moves nothing. That is intended, but
  `_note_limited()` only runs for states with moves, so the unreachable
  notice would no longer fire in that case. Add a separate state,
  `unexplained`, for fidelity under `FIDELITY_LOW` with no dominant miss
  cause. It has no moves; after `UNREACHABLE_AFTER_S` it sets the notice
  "Scenery was often blurry and AutoOrtho could not tell why; the flight
  report lists the blurry tiles by cause". The classifier above returns
  `unexplained` instead of `ok` in its two "no evidence" branches whenever
  fidelity is low.

## 7. Done when

- The flight report shows blurry near reads by cause for every flight.
- The governor's `behind_*`, `paging_coarse`, `prefetch_late` and
  `memory_evicted` decisions cite the miss evidence in their reason, and
  replay tests over recorded flights reproduce them.
- No prefetch lever moves without at least `MIN_MISSES` classified misses
  in the window.

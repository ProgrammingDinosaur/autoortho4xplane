# Performance: closing the gap with OrthoStudio XP Live

Date: 2026-11-22. Status: implemented in code on 2026-11-22 (items 0 to 5 and 7;
item 6 waits on its gate). Measured so far: item 3's decision gate and a smoke
test of a real mount worker. Not measured yet: the baseline B0, the other gates,
and the definition of done. They need flights and replays on this PC; see
"Implementation status" at the end.

The goal is for AutoOrtho to use less CPU and RAM than OrthoStudio XP Live on the
same flight, and to make X-Plane wait less. This plan covers what was left out
when the two bugs found on the flight of 2026-11-22 were fixed (commits
`4cbe69ea`, background builds not saved, and `f315eab8`, the prefetch loop). It
follows on from [resource-usage-plan.md](resource-usage-plan.md).

## Where we stand

One flight, 73 minutes, Vágar eastbound, EOX imagery, maximum zoom 17, Windows,
streaming scenery, before the two fixes. Source: the session report
(`performance-20261122-131415-ac2f625b`), the `STATS` lines and process counters.

| | Measured | Note |
|---|---|---|
| CPU, all AutoOrtho processes | 0.37 core average | worker 1,204 s, four brokers 196 s, parent 176 s, stats manager 15 s |
| of which the prefetch loop | about 0.25 core | parked with nothing downloading, the worker used 24 to 30 % of a core |
| Worker CPU spent in the kernel | 44 % | file I/O above all |
| Worker disk reads / writes | 41.5 GB / 8.2 GB | the reads are the loop rereading chunks |
| Memory (working set) | 2.2 GB | worker 1.35 GB, parent 0.26 GB, brokers 4 x 0.115 GB, stats manager 0.09 GB |
| Processes / threads | 7 / 264 | worker 124 threads, parent 92 |
| Chunk downloads | 100,000 in 71 min | 24 a second on average, 260 MB |
| Time X-Plane spent in texture reads | 325 s over 3,257 reads | 240 s of it are four reads that waited 60 s each while the flight loaded |
| Texture opens served straight from disk | 476 of 4,330 (11 %) | the rest built a `Tile` in memory |
| Chunk cache on this PC | 969,934 JPEG files in one folder | about 6.3 KB each (sampled) |

OrthoStudio publishes 0.32 core and 2.9 GB median for its own flight (a Mac,
another route, an older AutoOrtho as the comparison). Without the loop AutoOrtho
would already be near 0.12 to 0.15 core on this flight. That is an estimate from
the parked minutes, not a measurement: step 0 replaces it with one.

Two things said in the first analysis need correcting, and the plan below uses the
corrected versions:

- **"40 % of the worker's CPU was in threads that have exited".** Those are OpenMP
  worker threads that did real decode and compress work before their team was
  freed. The waste is only the cost of making a new team (0.3 to 1 ms a build),
  so the item is much smaller than it looked (section 6).
- **"Seven processes sample at 1 Hz".** The expensive fields (USS, thread count)
  are already read every 10 s, and the broker watchdog no longer lists every
  process on Windows. What is left is how the reports are written (section 2).

## Order of work

| # | Item | What it buys | Size | Risk |
|---|---|---|---|---|
| 0 | A benchmark in the repo, and a fresh baseline | every later decision | M | low |
| 1 | Reads stop waiting for chunks that have settled | up to 60 s off a flight's loading screen, 2 s off in-flight reads | S | low |
| 2 | Diagnostics and statistics that cost little | CPU and disk writes in all seven processes | S | low |
| 3 | A packed chunk store instead of one file per chunk | kernel time, antivirus load, disk space, cache maintenance | L | medium |
| 4 | Direct reads for textures finished at the level X-Plane wants | worker RAM and CPU, shorter reads | M | medium |
| 5 | Fewer, leaner processes | about 0.4 GB of RAM, idle CPU | M | medium |
| 6 | Stable native build threads | a few percent of worker CPU, if step 0 confirms it | S | low |
| 7 | Prefetch passes that let their own row builds finish | wasted builds on textures cached chunk by chunk | S | medium |

Do 0 first, then 1 and 2 (small, safe, and they clean up the numbers the larger
items are judged by), then 3 and 4, then 5 to 7 as step 0's fresh numbers justify.
Each item is its own branch and pull request.

## 0. A benchmark in the repo, and a fresh baseline

**Why.** Every figure above comes from one flight with two bugs in it. The loop
inflated the kernel time, the disk reads, the cache probes (2.39 million), the
thread-team churn (49,858 teams freed) and the gauge updates (2.3 million per
gauge). The replay harness used for earlier measurements lives outside the
repository, so nobody else can rerun it and its traps are known only from notes.

**Changes.**

- Add `tools/bench/` with the read replay: a recorded list of X-Plane's opens and
  reads with their times (one flight's `fuse.dds_open` / `fuse.dds_read`
  sequence), a driver that mounts a scenery through the real mount worker and
  replays them, and a sampler that records per process CPU seconds (user and
  kernel), working set, private bytes, threads, read and write bytes.
- Three scenarios, each with its recorded reads checked in: `load` (the first
  five minutes of a flight, cold cache), `cruise-cold` (20 minutes, cold cache),
  `cruise-cached` (the same 20 minutes again, warm cache).
- The driver connects a fake simulator position feed so `flight_is_starting()`
  goes false after the load, as in a real flight. Without it every read runs in
  loading mode (60 s near waits, ten times the tile budget) and the results
  describe a loading screen.
- Before timing, the driver kills orphaned broker processes of earlier runs,
  refuses to start when other processes use more than one core, and prints the
  log level it runs at (the shipped default, DEBUG).
- Output: one JSON per run, and a `compare.py` that prints two runs side by side
  with the counters this plan names.
- For the head-to-head with OrthoStudio, use its own flight tooling
  (`tools/live/flights/fly.py` with its test-pilot plugin and
  `sampler_windows.py` in the OrthoStudio checkout): it flies X-Plane along a
  route with either program. A replay cannot compare the two, since OrthoStudio
  has no virtual disk to replay reads against.

**Then measure**, on develop with the two fixes: the three scenarios three times
each, and one real flight of the Vágar route. Record the results in this file
under "Baseline B0". Run `py-spy record --gil` on the worker, one broker and the
parent during `cruise-cold` and keep the three flame graphs with the baseline.

**Done when** two runs of the same scenario on an idle PC differ by less than 5 %
in total CPU seconds, and B0 is written down.

## 1. Reads stop waiting for chunks that have settled

**Why.** At 13:18:24 four textures near Vágar each needed sixteen ZL14 chunks for
their 1024 px level. Bing has no imagery for some of those chunks (open sea) and
said so within 30 ms. The read still waited the full near-band wait, 60 s while a
flight starts, then built the level in 2 ms from the fallback. X-Plane's loading
screen stood still for a minute for nothing. In flight the same case costs the
normal wait (2 s) on every such read. It hits any coast with a provider that has
gaps.

The cause is the wait loop in `Tile._try_native_mipmap_build`
([getortho.py](../../autoortho/getortho.py), "Wait for downloads capped at
maxwait"). It leaves early only when the count of chunks *with data* reaches the
threshold (100 %). A chunk that is ready without data, because the provider has
none or the download failed for good, never counts, so the loop always runs to
its deadline. It also polls with `time.sleep(0.01)`, a hundred wake-ups a second
per waiting read.

**Changes.**

- Leave the loop as soon as no chunk in `missing_chunks_indices` is still
  outstanding (`chunk.ready.is_set()` for all of them), whether or not they have
  data. The threshold check that follows already sends the build to the fallback
  path when chunks are missing.
- Replace the poll with a wait on the chunks' ready events: one
  `threading.Event` set by a settled callback on each outstanding chunk
  (`add_settled_callback`, as the prefetch coordinator uses), waited on with the
  remaining time. This is the only such poll loop in the read path.
- Count the case: `read_wait_ended_all_settled`, so a flight shows how often it
  helps.

**Tests.** A tile whose level needs four chunks, one settled without data and
marked permanent: the build returns in under 100 ms with a wait budget of 60 s,
and reports the threshold miss. A second test where the last chunk settles with
data after 50 ms: the build uses it.

**Done when** the `load` replay over the Vágar area shows no read longer than the
slowest download, and `native_mipmap_threshold_miss` reads no longer take the
whole wait.

## 2. Diagnostics and statistics that cost little

**Why.** Reports are worth keeping on by default: this analysis depended on them.
But their cost grows with the length of the flight and is paid in every process.

- Each process rewrites its whole report every 10 s
  (`PerformanceProfiler._write_checkpoint_if_due`,
  [diagnostics.py](../../autoortho/diagnostics.py)): every memory sample since the
  start, through `json.dump(indent=2, sort_keys=True)`. After 73 minutes each
  file was 1.4 MB. The parent had written 600 MB of reports and each broker
  312 MB. The sample list is capped at 36,000 entries, so a ten-hour flight
  rewrites about 12 MB every 10 s in seven processes.
- The prefetch coordinator publishes nine gauges, two of them sums over every
  candidate, on every chunk event (`PrefetchCoordinator._report_locked`,
  [prefetch.py](../../autoortho/prefetch.py)): 2.3 million updates per gauge on
  this flight.
- Counters go to a separate stats-manager process through `StatsBatcher`
  ([aostats.py](../../autoortho/aostats.py)), which wakes twenty times a second
  in every process to send them.

**Changes.**

- Samples become append-only: each checkpoint appends the new samples to
  `process-<pid>-<role>.samples.jsonl` and writes a checkpoint JSON that holds
  only the stages, gauges and slow operations (bounded, tens of kilobytes,
  compact separators). `finalize_session_report` merges the two when the
  scenery unmounts, so the final report keeps its layout. The report reader used
  by the Diagnostics page accepts both layouts while a session runs.
- Sample the brokers, the stats manager and the parent every 5 s. The worker
  keeps 1 s: its timeline is the one that explains stutters.
- `_report_locked` sets a dirty flag. The gauges are published at most four times
  a second and once when a candidate reaches a final state.
- `StatsBatcher` flushes once a second (`flush_interval=1.0`), and still at once
  when its buffer reaches `max_items`. First check the readers that want fresh
  counters (the governor's `near_miss_*` signals, the Home page): if one needs
  less than a second, give it a `flush()` call instead of a faster timer.

**Tests.** A profiler run with 5,000 samples: the checkpoint file stays under
200 KB, the samples file has 5,000 lines, the finalized report equals the one the
old code produced for the same input. A coordinator fed 10,000 chunk events
calls the gauge callback fewer than 100 times.

**Done when** a `cruise-cold` replay with `performance_profiling` on uses at most
1 % more CPU seconds than with it off, and the bytes each helper process writes
stay flat over time.

## 3. A packed chunk store instead of one file per chunk

**Why.** Every downloaded chunk is its own file,
`{col}_{row}_{zoom}_{maptype}.jpg`, all in one folder: 969,934 of them here.

- Writing one costs about 0.59 ms of CPU (`_atomic_write`: an existence check, a
  temp file, a rename), mostly in the kernel. At 100,000 chunks a flight that is
  about a minute of CPU, before the antivirus looks at each new file. Microsoft
  Defender used about 118 CPU seconds during one 15-minute replay.
- Reading a texture's imagery is 16 to 1,024 opens. `_chunk_cache_index` exists
  only to avoid asking the file system about files that are not there.
- On NTFS each file takes whole 4 KB clusters and a file record: an estimated
  8.7 GB on disk for 6.1 GB of imagery.
- Cache maintenance (the size scan, eviction, `cache_inventory`, kept imagery)
  walks a folder of a million entries.

OrthoStudio keeps every chunk in one SQLite file for these reasons.

**Design.**

- A `ChunkStore` class (new module `autoortho/chunk_store.py`) with
  `get_many(maptype, zoom, keys)`, `put_many(rows)`, `mark_no_imagery(...)`,
  `delete_many(...)`, `size_bytes()` and `evict_oldest(nbytes)`.
- Storage is SQLite, one database per 10 degree band, in
  `<cache_dir>/chunks/<band>.sqlite`, the same bands the DDS cache folders use.
  A band keeps each file to a few gigabytes, lets eviction and a damaged file
  affect one region, and a flight touches one to four of them.
- Table `chunks(maptype TEXT, z INTEGER, m INTEGER, status INTEGER, data BLOB,
  fetched INTEGER, PRIMARY KEY (maptype, z, m)) WITHOUT ROWID`, where `m` is the
  Morton code of the chunk's column and row. A texture's chunks are an aligned
  square, so they are one contiguous key range and `get_many` is one range scan.
- A provider's "no imagery here" is stored as a row with no data, as OrthoStudio
  does, so the sea is not asked for again in the next session. Today it is kept
  on the chunk in memory (`failure_reason = "no_imagery"`); I found nothing that
  writes it to disk, which is worth confirming when this is built.
- Page size 16 KB (most chunks fit a page without overflow), WAL journal,
  `synchronous=NORMAL`. WAL is needed because macOS runs one worker per mount, so
  several processes write the same store. Windows and Linux have one worker.
- Writes go through the existing bounded cache-writer executor, which now
  collects completed chunks and commits them in batches (up to 64 chunks or
  100 ms, whichever comes first). A chunk is readable from memory until its batch
  commits, as `WRITE_PENDING` is today.
- `Chunk.get_cache` / `Chunk.save_cache`, `_batch_read_cache_files` and
  `_chunk_cache_index` are rewritten on top of the store. The index shrinks to
  the pending-write map.
- Native code stops reading chunk files. The builders that take imagery as bytes
  (`build_from_jpegs_to_file`, `build_partial_mipmap`, `build_all_mipmaps_native`)
  already exist and are the default ("hybrid") path. The path-based entry points
  (`build_tile_to_file`, `build_tile_to_buffer`, `aocache_batch_read`) are kept
  only for reading the old folder during migration, then removed.
- Everything else that knows chunk file names moves to the store's API:
  `disk_budget_manager` (JPEG accounting and eviction), `_cleanup_jpegs_async`
  (dropping imagery once its texture is complete), `cache_inventory`,
  `kept_imagery`, `low_zoom_base` (base chunks become rows the eviction skips),
  `fallback_resolver`, `utils/cache_cleanup`, and the long-term cache copy.

**Migration.**

- On a store miss, the old file is still tried by its exact name while a marker
  file says the old folder is not drained. A hit is imported into the store and
  the file deleted.
- A background converter moves the remaining files when no flight is mounted,
  with progress on the Scenery or Home page, and removes the marker at the end.
  Nothing blocks the first start on a million files.
- A user who goes back to an older version finds an empty chunk cache and the
  DDS cache intact. Say so in the release notes.

**Decision gate, before any of the above is merged.** A microbenchmark with
100,000 real chunks from this cache: CPU per chunk written, time and CPU to read
256 and 1,024 chunks cold and warm, against today's files, with Defender on.
Go ahead only if writes cost at least five times less CPU and reads are no
slower. If SQLite misses the gate, test one pack file per texture and zoom before
dropping the item.

**Tests.** Store unit tests (range reads, no-imagery rows, eviction order, two
processes writing, a database truncated mid-file is recreated, not fatal).
Migration tests (read-through, converter resumes after a stop). The existing
chunk and tile tests run against the store.

**Done when** the gate numbers hold in the `cruise-cold` replay (worker kernel
CPU down by the share file I/O had in B0), a second session over open sea
downloads nothing it was already told does not exist, and a cache of a million
files converts in the background without a flight noticing.

## 4. Direct reads for textures finished at the level X-Plane wants

**Why.** A texture served straight from its cache file costs no `Tile`, no
decode and no RAM in the worker (resource-usage-plan, section 1). Only 11 % of
opens were direct on this flight, for two reasons.

- Background builds never reached the cache. Fixed in `4cbe69ea`, so this share
  rises by itself. Step 0 says by how much.
- A direct read needs a complete, exact mipmap 0. But X-Plane mostly does not
  want mipmap 0: of the reads that asked for a level, 433 wanted level 0 and
  2,273 wanted level 1 to 5. A texture 30 km away is read at 512 px, its
  mipmap 0 is never downloaded, and so it can never be direct, however often it
  is opened. Its coarse levels are already on disk: `store_incremental` writes
  them into a sparse file of the full size.

**Changes.**

- `DynamicDDSCache.open_direct` gains a second way to qualify: the entry is at
  the target zoom, not stale, uncompressed, of the expected size, and levels
  `k` to the last are populated, where `k` is the level the paging model says
  X-Plane wants now (`paging_target_mip`). The returned entry carries
  `finest_level`.
- `DiskTexture.read` ([disk_texture.py](../../autoortho/disk_texture.py)) serves
  a read from the file when it lies within the populated levels. The header
  chunk (the DDS header and the first bytes of mipmap 0, which X-Plane reads for
  every texture and does not use for a far one) is answered as the live path
  answers an incidental read: the real header and stand-in bytes, built once per
  texture.
- A read that reaches a level the file does not hold switches that handle to a
  live `Tile`, through the fallback direct reads already have for I/O errors.
  A wrong guess of `k` therefore costs what an open costs today, never an error
  or a wrong texture.
- Each read is still classified (`read_pattern.classify`), so the paging model
  and the held-texture estimate keep their samples.
- Counters: `dds_direct_partial_opens`, `dds_direct_partial_fallbacks`.

**Risks.** A fallback rate that eats the gain: if more than one partial open in
ten falls back, widen the rule to "populated from `k - 1`". The season colour
table must be applied as the live path applies it. Levels below mipmap 0 have no
row manifest and are trusted, as they are in direct reads today.

**Tests.** A partial entry (levels 3 to 13) opened for a far texture is served
from the file and never builds a tile. A read into level 2 falls back and
returns the same bytes the live path gives. The header chunk matches the live
path's answer for the same texture.

**Done when** at least 70 % of opens are direct on `cruise-cached`, the worker's
median working set on that scenario drops by at least 25 % against B0, and no
read returns bytes that differ from the live path's.

## 5. Fewer, leaner processes

**Why.** Six helper processes held about 0.8 GB and a quarter of the CPU on this
flight, for work that is mostly small.

- **Stats manager.** A `multiprocessing` manager process (94 MB, 15 CPU seconds)
  that exists to hold a dictionary of counters.
- **Brokers.** Four of them, 115 MB each. The count is one per four CPU threads,
  up to four, sized for the burst while a flight loads. In flight they handled
  24 requests a second together. One broker is good for about 200.
- **Parent.** 263 MB of working set, 1.1 GB committed, 92 threads, and 176 CPU
  seconds (4 % of a core) while it does nothing the flight needs.
- **Worker.** 124 threads, several pools sized from 32 logical CPUs.

**Changes.**

- *Counters without a manager process.* The store is a `BaseManager` that
  `autoortho.py` starts as a process of its own, reached over a local socket.
  Serve the same store from a thread of the parent instead
  (`manager.get_server().serve_forever()` on a daemon thread): the workers and
  brokers keep their client code and the socket protocol, and the process goes
  away. With section 2's one-second flush the parent handles a few calls a
  second. On macOS, with one worker per mount, the parent is still the one
  place that sums. If the parent's flame graph later shows the server thread,
  the next step is sending increments once a second on a channel the parent
  reads in one batch.
- *Brokers on demand.* Start one broker. Start another when requests wait at the
  client side (`broker.client_pending` above 64 for two seconds, or a broker
  event loop running late), up to the configured maximum. Stop the extra ones
  after five minutes without that pressure. Requests are spread by URL, so a new
  broker only takes new requests. `provider_broker_processes` keeps its meaning
  as the maximum; 0 stays automatic.
- *Lighter brokers.* Measure a broker's imports (`python -X importtime`) and load
  lazily what it does not need to serve requests. Target 60 MB.
- *A quiet parent.* Profile it (step 0's flame graph), then stop what a flight
  does not need while the window is hidden or minimized: page refresh timers,
  per-second sampling of other processes. Cap its thread pools: nothing in the
  parent needs a pool sized by CPU count.
- *Worker pools.* List every executor and thread started in the worker with its
  size and what bounds it, and cap those sized by logical CPUs where the work is
  I/O or coordination.

**Decision gates.** Brokers on demand goes ahead only if the `load` replay with
one broker plus on-demand start is within 5 % of four brokers on time to finish.
If not, the default becomes two, which costs nothing to implement.

**Tests.** Counter totals with and without the manager match on a replay. A
broker burst test: pending requests above the threshold start a second broker
within three seconds and no request is lost when a broker is retired.

**Done when** a flight in cruise runs four processes (parent, worker, one broker,
and no stats manager), helper processes hold under 0.4 GB together, and the
parent uses under 1 % of a core with its window hidden.

## 6. Stable native build threads

**Why.** Native builds run OpenMP parallel regions on whichever Python thread
asked: read threads, the exact-row executor, the background builder, prefetch.
libgomp keeps a team of worker threads per calling thread, which once grew the
worker to 634 threads, so `_settle_native_team` frees a thread's team after its
build unless it is one of the few "keepers". On this flight 49,858 teams were
freed, each rebuilt at the next build for 0.3 to 1 ms: 15 to 50 CPU seconds.

Most of those builds came from the loop (it ran 437 partial builds a minute
while the aircraft was parked, out of 25,932 in the whole flight), so this may
already be small.

**Decision gate.** In B0, `native_teams_released` times 0.65 ms. Below 1 % of the
worker's CPU seconds, close this item.

**Changes, if it stays.** Run native builds on a small fixed set of build
threads, each keeping its team for good: `_native_build_context` hands the build
to a lane instead of running it on the caller's thread, and the caller waits for
the result. Lanes: one wide (half the CPU budget) for whole-texture builds, the
rest narrow (the eight-chunks-a-thread rule of `_partial_build_threads`) for row
builds. Priority order stays as `_native_build_context` has it, live before
speculative. `_settle_native_team` and the keeper table are deleted.

**Done when** `native_teams_released` stays at zero through a flight, the
worker's thread count is flat after the load, and build latency (the
`dds.native_partial_build` and `dds.native_mipmap_build` stages) is no worse than
B0 at p50 and p95.

## 7. Prefetch passes that let their own row builds finish

**Why.** A texture with more chunks than one pass may hold (an 8192 px texture)
is finished over several passes. When its chunks are all on disk a pass covers
512 of them in milliseconds, runs out of budget and drops its lease at once.
Dropping the lease closes the tile, which kills the row builds the pass had just
queued ("DDS cleared during build", 4,797 times on this flight). After
`f315eab8` this no longer loops, but each such texture still throws away a few
builds per pass and needs more passes than necessary.

**Changes.**

- `Tile.exact_rows_in_flight()`: the count of rows in the states `build_queued`
  and `building` (built or building, not yet saved).
- `PrefetchCoordinator._defer_candidate` keeps the lease while that count is
  above zero, up to a deadline of ten seconds from the pass's last scan. Past the
  deadline it drops the lease as today, so a row stuck in a state cannot hold the
  chunk budget and starve other candidates.
- `Tile._run_exact_prefetch_row` resets a row's state when it returns early on a
  closed tile. Today it leaves the row in `build_queued`.

**Risk.** The lease holds up to 512 chunks of the shared budget for longer.
The ten-second cap bounds it. Watch `prefetch_admission_no_capacity` in the
replay: it must not rise.

**Tests.** A pass whose rows are in flight keeps its lease, then drops it when
they are saved. A row that never leaves `building` loses the lease at the
deadline.

**Done when** `native_partial_dds_cleared` is under 1 % of
`native_partial_strict_build` on `cruise-cached`, and an 8192 px texture with all
chunks on disk completes in at most three passes.

## Not planned

- **Building textures to plain files with no virtual disk** (OrthoStudio's
  model). Still out of scope, as resource-usage-plan decided: sections 3 and 4
  get most of its CPU and RAM benefit while keeping one code path for reads.
- **Turning diagnostics off by default.** Section 2 makes them cheap instead.
- **Lowering the default log level.** About 56,000 lines in 80 minutes: too
  little to matter. Worth a separate look only if a flame graph shows log
  formatting.
- **A different imagery provider default or fewer downloads.** EOX answered in
  300 ms at the median and up to 1 s at p95. That is the provider, and the
  paging prefetch already limits what is fetched.

## Definition of done

The whole plan is done when all of the following hold. "B0" is the baseline of
step 0. Every figure is the median of three runs on an idle PC.

**Against OrthoStudio XP Live** (the current release, the same route flown by
its `fly.py` tooling with each program in turn on the same day, empty caches,
the same imagery source and zoom):

1. AutoOrtho's total CPU seconds in flight, all processes, are lower.
2. AutoOrtho's memory, all processes, is lower at the median and at the peak.
3. X-Plane's frame rate is not lower, and its scenery loads in flight are not
   longer.
4. The time from starting the flight to flying is not longer.

**Against B0**, on the replay scenarios:

5. `cruise-cold`: total CPU seconds down by at least 30 %, worker kernel CPU
   seconds down by at least 50 %.
6. `cruise-cached`: at least 70 % of texture opens direct, worker median working
   set down by at least 25 %.
7. `load`: no read waits longer than the slowest download it depended on; time
   to the last read no longer than B0.
8. In cruise: four processes or fewer, under 150 threads in total, helper
   processes under 0.4 GB together.
9. Diagnostics on costs at most 1 % more CPU seconds than off.

**Quality and safety:**

10. Imagery quality is not lower: the share of mipmap-0 bytes served exact
    (`mm0_served_exact_bytes` against the lower-zoom and missing counters) is at
    least B0's, and `near_reads_real` against stand-ins is no worse.
11. Every test file passes on its own, with no failure that clean develop does
    not have, and each item ships the tests its section lists.
12. A real flight of at least an hour on Windows with each merged item, and one
    on macOS and Linux before release, with no crash, no stuck read and a clean
    unmount.
13. An existing chunk cache of a million files is taken over without blocking a
    start, and going back to the previous version loses nothing but the chunk
    cache.

**Housekeeping:**

14. New settings are in `aoconfig.py`, the settings registry and
    `docs/config.md`; removed ones are migrated or ignored with a log line.
15. This file's status line says "implemented", each section says what was
    measured after it, and items closed by a decision gate say so with the
    number that closed them.

## Implementation status (2026-11-22)

All changes are in the working tree of `fix/prefetch-loop-and-dds-staging`,
uncommitted, one item per area so they can still be split into the branches
this plan asks for.

### 0. Benchmark

- `tools/bench/`: `replay.py` (attach to a running AutoOrtho or start a lone
  mount worker, replay a trace on its timeline, sample every process, write
  one JSON), `sampler.py`, `fake_xplane.py` (answers AutoOrtho's RREF
  subscriptions with the trace's positions), `compare.py` (side by side, the
  gates of items 0, 2 and 6, definition of done 5 to 8), `make_scenario.py`
  (synthetic trace along a route), `chunk_store_gate.py` (item 3's gate),
  `README.md`.
- Recording: `autoortho/read_trace.py`, switched on by `AO_READ_TRACE=<file>`.
  The FUSE layer appends every DDS open, read (with its latency) and release,
  and the position once a second. Checked on a real mount worker: 833 events
  for nine textures.
- Prechecks as specified: orphaned brokers killed, a busy PC refused (the
  Windows idle process is not counted), the log level printed.
- Differences from the plan: the position feed needs the parent (it owns the
  dataref tracker and shares the position through the stats store), so
  `--launch-worker` replays read in loading-screen mode. No scenario traces
  are checked in yet: they must be recorded on a flight (`README.md`, step 1),
  or made with `make_scenario.py` and labelled synthetic.
- **Not done: B0.** Three runs of three scenarios and the Vágar flight are
  still to do, on an idle PC.

### 1. Reads stop waiting for settled chunks

- `getortho.wait_for_chunks_settled`: leaves when every outstanding chunk is
  ready, with or without data, and wakes on the chunks' ready callbacks
  instead of polling every 10 ms (a 250 ms backstop only for test doubles
  without callbacks). New counter `read_wait_ended_all_settled`.
- Tests: `test_read_wait_settled.py`. A chunk with no imagery ends a 60 s wait
  in well under 0.5 s and the build reports the threshold miss; a chunk that
  settles with data 30 ms later is used.
- Not measured: the `load` replay over Vágar.

### 2. Diagnostics and statistics

- Checkpoints append new samples to `process-<pid>-<role>.samples.jsonl` and
  rewrite only a light JSON (compact separators, host info built once); the
  complete profile at stop holds the samples inline and the samples file is
  removed. `diagnostics.load_process_profile` reads both layouts;
  `finalize_session_report` uses it (the Diagnostics page reads only
  finalized reports).
- The parent and the brokers sample every 5 s; the worker keeps the configured
  interval (`child_sample_interval` passes it on).
- The prefetch coordinator publishes its gauges at most four times a second,
  and at once when a candidate reaches a final state or the coordinator stops.
- `StatsBatcher` flushes once a second (the governor reads every 2 s, the
  STATS log every 10 s).
- Tests: `test_cheap_diagnostics.py`. 5,000 samples: checkpoint well under
  200 KB, 5,000 lines in the samples file, finalized report equal to the inline
  layout's; 10,000 coordinator reports cause fewer than 100 gauge calls.
- Not measured: diagnostics on against off on `cruise-cold`.

### 3. Packed chunk store

- `autoortho/chunk_store.py`, on by default (`[cache] chunk_store = True`).
  Per-band SQLite files in `<cache_dir>/chunks/`, WAL, `synchronous=NORMAL`,
  incremental auto-vacuum, batched writes (64 rows or 100 ms) from one writer
  thread with a pending map that reads see, bounded by
  `cache_write_buffer_mb`. "No imagery" is stored (trusted 30 days), kept rows
  (`FLAG_KEEP`: the low-zoom base, kept imagery) are skipped by eviction, exact
  byte totals are kept by triggers, and a damaged band is set aside and
  recreated without an error reaching the caller.
- **Layout changed from the plan, by measurement.** `WITHOUT ROWID` with
  16 KB pages put every chunk above about 4 KB into a mostly empty overflow
  page: 1.46 GB on disk for 0.9 GB of imagery. The table is now a rowid table
  whose integer key packs (map type number, zoom, Morton code), so a texture is
  still one range scan; with 4 KB pages the files take 1.09 times the imagery
  (8 KB: 1.16, 16 KB: 1.45, 64 KB: 1.21; one file per chunk on NTFS about 1.4).
- **Decision gate: passed.** 100,000 real chunks from this PC's cache
  (895 MB), Defender on, scratch folder on C:. Writes: 459 µs CPU a chunk as
  files against 44 µs in the store (10.4 times less; 24 times less counting
  Defender, which used 89 CPU seconds on the files and under 1 on the store).
  Reads of 20 textures each, files against store: 256 chunks 698 against 3 ms
  cold and 42 against 4 ms warm; 1,024 chunks 2,467 against 7 ms cold and 42
  against 6 ms warm. ("Cold" is a fresh store and files not opened since they
  were written; the OS file cache was not dropped.)
- Wired in: `getortho` (batch and single reads, writes straight from the
  download path, the background builder's hybrid mode reads the store and the
  file-reading native modes are skipped, corrupt-chunk removal, provenance
  checks, remembered "no imagery" settles a chunk without a download),
  `dynamic_dds_cache` (cleanup after a texture is complete, healing reads),
  `fallback_resolver`, `disk_budget_manager` (O(1) size, eviction with the
  keep predicate), `kept_imagery` (downloads write kept rows, un-keeping
  releases them), `cache_inventory`, `diagnostics` (comparison export),
  `utils/cache_cleanup` and the cache cleanup in Settings.
- Migration: a new store over a folder with chunk files gets the marker
  `chunks/legacy-files-pending`; misses read the old file by exact name and
  move it in (deleted after its batch commits). `chunk_migration.py` moves
  the rest in the background while no flight is mounted (paused at mount,
  resumed after unmount, progress in Home's status strip) and removes the
  marker when no chunk file is left.
- Kept as files, against the plan: the long-term cache. It is meant for slow
  disks and network shares, where SQLite's WAL is unsafe.
- Native path-based entry points (`build_tile_to_file`, `build_tile_to_buffer`,
  `aocache_batch_read`) stay for the store-off setting and for reading old
  files during migration.
- Smoke test on a real mount worker (isolated home, Bing): nine textures
  near Vágar read through WinFsp, one band file `+60-010.sqlite` with 1,533
  chunks (17.2 MB, trigger totals exact, 163 KB of base chunks kept), no
  `.jpg` written, clean unmount.
- Tests: `test_chunk_store.py`, `test_chunk_store_integration.py`,
  `test_chunk_store_getortho.py` (range reads, no-imagery rows, eviction order
  and keep, two processes writing, a truncated database recreated,
  read-through, the converter resuming after a stop, the budget, cleanup,
  kept imagery, inventory).
- Not measured: `cruise-cold` kernel CPU, a second session over open sea, a
  million-file conversion during a flight.

### 4. Partial direct reads

- `DynamicDDSCache.open_direct(..., min_level=k)` also qualifies an entry
  populated from some level `<= k` to the last (never level 0 without its
  exact row manifest); the entry carries `finest_level`. `DiskTexture` serves
  levels from `finest_level` from the file, the header from the file, and the
  incidental bytes of finer levels as stand-in bytes (the texture's coarsest
  block repeated, built once). A read that needs a finer level raises
  `DiskTextureLevelMissing`: the open switches to a live tile, the file is not
  marked bad. `getortho.open_disk_texture` passes `paging_target_mip`; with
  more than one partial open in ten falling back (after 50), entries must start
  one level finer. Setting `[streaming] partial_direct_reads`. Counters
  `dds_direct_partial_opens`, `dds_direct_partial_fallbacks`.
- Difference: the stand-in bytes are not the live path's (which stretches the
  base level into them). X-Plane does not use those bytes for a texture it
  wants coarser; the bytes it wants are the file's, as in the live path.
- Tests: `test_partial_direct_reads.py`.
- Not measured: the share of direct opens and the worker's working set on
  `cruise-cached`.

### 5. Processes

- The stats store is served from threads of the parent
  (`autoortho._ThreadedStatsServer`): same protocol for workers and brokers,
  no manager process, the parent uses the store directly. Test with two client
  processes: `test_stats_server_thread.py`.
- Brokers on demand (`[autoortho] provider_broker_autoscale = True`): one
  broker starts; `http2_broker.BrokerAutoscaler` asks each broker for its
  counters every second and starts another (up to `provider_broker_processes`)
  after 2 s of more than 64 held requests or an answer slower than 250 ms, and
  retires the newest extra one after five calm minutes (draining first: the
  routing table marks it, workers stop sending to it, it is stopped once
  empty). Workers follow the table the parent publishes in the stats store
  (`MultiBrokerClient.watch_routing`). Difference: the pressure signal is the
  brokers' own counters, not the workers' `broker.client_pending` (a profiler
  gauge the parent cannot read). Tests: `test_broker_autoscale.py`.
  **Gate not run** (the `load` replay with on-demand brokers against four).
- Lighter brokers: `httpx` is imported only by a broker that uses it (curl is
  used when installed); and a "spawn" child no longer imports the app, its
  config and Qt through `__main__` (guarded by `__name__ == "__mp_main__"`).
  Measured imports: the child starts at about 40 MB instead of about 115 MB.
  This applies when running from source and to frozen macOS and Linux builds;
  frozen Windows builds never re-ran `__main__` in children.
- Quiet parent: hidden or minimized, its two 1 s page timers run every 5 s;
  Qt's shared thread pool is capped at 4.
- Worker pools, as listed: `ao-progressive` (decode, up to the CPU count,
  CPU-bound: left), `BackgroundDDS` (16 ceiling, CPU-bound: left), exact rows
  (2), cache writers (2, unused with the store), long-term cache (2 + 4),
  read-ahead (2), DDS cache maintenance (2), fallback resolve (4, per call).
  None is I/O or coordination sized by logical CPUs, so none was capped; the
  worker's thread count is mostly OpenMP teams (item 6) and FUSE threads.
- Not measured: processes, threads and helper memory in cruise.

### 6. Stable native build threads

- **Waiting on its gate.** The only team count available is the 2026-11-22
  flight with the prefetch loop in it: 49,858 teams released x 0.65 ms =
  32 s, 2.7 % of the worker's 1,204 CPU seconds. Most of those builds came
  from the loop, which is fixed. `compare.py` computes the gate from B0; build
  the lanes only if it is above 1 %.

### 7. Prefetch passes keep their row builds

- `Tile.exact_rows_in_flight()`; `_defer_candidate` keeps the lease while rows
  are queued, building or built and unsaved, up to 10 s from the first
  deferral that held it (counters `prefetch_lease_held_for_rows`,
  `prefetch_lease_row_hold_expired`); `_run_exact_prefetch_row` resets a row
  on a closed tile. Tests in `test_prefetch_coordinator.py` and
  `test_exact_rows_in_flight.py`.
- Not measured: `native_partial_dds_cleared` on `cruise-cached`.

### Definition of done

| # | Status |
|---|---|
| 1 to 4 (against OrthoStudio) | Not measured: needs flights with its `fly.py` tooling |
| 5 to 9 (against B0) | Not measured: needs B0 and the replays (`compare.py` checks them) |
| 10 (imagery quality) | Not measured |
| 11 (tests) | Every test file run on its own; no failure that clean HEAD does not have |
| 12 (hour-long flights) | Not done |
| 13 (million-file cache taken over) | Converter and read-through built and tested on small folders; not run on the real cache |
| 14 (settings) | `chunk_store`, `partial_direct_reads`, `provider_broker_autoscale` in `aoconfig.py` (so the registry), documented in `docs/config.md` |
| 15 (this file) | This section; to be updated with B0 and the measured results |

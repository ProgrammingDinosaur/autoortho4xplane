# Hybrid read path: plain-file speed for ready textures, a choice for the others

Date: 2026-11-24. **Status: closed on 2026-11-25.** Every stage that was to
be built is built, measured and committed on
`fix/prefetch-loop-and-dds-staging` (M0, N1 to N8, S1 to S3); S6 was closed
by gate B, S5 is not built (gate C, see "Flights and closure"), S4 is
deferred. What was built and measured is in "Implementation status" right
below; what the flights showed and what carries over is in "Flights and
closure". The work continues in
[ready-share-plan.md](ready-share-plan.md).

## Implementation status (2026-11-25)

| # | What was built | Where | Checked by |
|---|---|---|---|
| N1 | A DDS read at or past the end returns nothing; one that straddles the end is cut there (`fuse_reads_past_end`). The tile clamps too. | `AutoOrtho._read`, `Tile.read_dds_bytes` | `test_dds_fallback.py` |
| N2 | Rows found good outlive the open: `disk_texture._verified`, keyed by path, size and modification time, guarded by the file's identity; a failed check forgets the file. | `disk_texture.py` | `test_direct_open_cost.py` |
| N3 | The parsed `.ddm` is kept (`DynamicDDSCache._direct_info`) while both files keep size and time: two `stat` calls per open instead of reading and parsing it. Row manifests are loaded only if a read needs a row checked, and a `.ddm` rewritten since the open hands the open to a live tile (`DiskTextureStale`, not a fault). Direct reads are counted once per open. | `dynamic_dds_cache.py`, `disk_texture.py` | `test_direct_open_cost.py` |
| N4 | An entry whose only holes are lower-zoom fallbacks for chunks the chunk store remembers as "no imagery" is servable (`dds_direct_no_imagery_opens`). A failed download is never recorded so; missing-colour chunks keep a texture live. | `DynamicDDSCache._no_imagery_holes` | `test_direct_open_cost.py` |
| N5 | `[streaming] texture_wait_policy` = `quality` / `balanced` / `smooth`, as Part 3 says; hand-set means written in the user's file, so the governor's run-time changes do not count. "When scenery is not ready" in Settings > Streaming and on Essentials; counters `wait_policy_<policy>_reads`, `_read_ms_total`, `_near_standins`. HTTP and live-queue timeouts keep the configured wait. | `wait_policy.py`, `wait_bands.py`, `getortho._prefetch_quality_mode`, UI, `docs/config.md` | `test_wait_policy.py`, `test_essentials.py` |
| N6 | Code only: a read event in the trace has `tier` (direct, partial, live, ..._to_live), `tid` and `got`; `python -m autoortho.read_trace <file>` prints the share of ready opens and how many reads overlap. The recorded flight is still to fly. | `autoortho_fuse.read`, `read_trace.summarize` | `test_bench_tools.py` |
| N7 | Stage S2: `aopipeline/aofast.c` in the aopipeline library (all three Makefiles), `AoFast.py`, the hook in mfusepy's `fuse_main_real` (`autoortho_fuse.native_reads_for`, used by `run` and the mount worker), one native entry point per mount (32), `[streaming] native_direct_reads`, default off. Complete entries without season colours only; unchecked rows, short reads, disabled or stale handles go to Python; the C classifier is `read_pattern.classify` line for line. | `aofast.c/.h`, `AoFast.py`, `autoortho_fuse.py`, `mount_worker.py` | `test_native_reads.py` (classifier parity, byte equality, fallbacks, 16 readers racing a release), `test_dds_fallback.py::test_native_reads_through_the_mount` |
| N8 | `readbench.c`, `aoprobe.c`, `minifs.py` and `read_cost.py build` build on Linux and macOS (POSIX calls). | `tools/bench/read_cost/` | built and smoke-tested on Linux (WSL); no recorded Linux or macOS run |
| S3 | Native reads of **partial entries** (the header from the file, bytes of levels the file lacks as the coarsest block repeated, as `DiskTexture._standin`; a read that needs such a level goes to Python, which serves the open live) and of textures with **season colours** (`colour_table.apply` ported line for line; the table is taken at open and held in one of 32 library entries, reused once no open uses it). **Row checks** moved off the read path, the plan's second option: rows of a file this process stored, from bytes it had just checked against their manifests, are remembered as checked (`disk_texture.remember_verified`, from `store`, a hard-linked or staged `store_file` and the zstd promotion), so a texture built ahead in this session is never checked again; files from earlier sessions or other processes are checked at first read as before. SHA-256 in C was not done: a portable C implementation is several times slower than Python's OpenSSL one. | `aofast.c`, `AoFast.py`, `disk_texture.py`, `dynamic_dds_cache.py`, `autoortho_fuse.py` | `test_native_reads.py` (partial and season byte equality against tier 2, recolour parity, table reuse), `test_direct_open_cost.py` |

| Extras | Found while measuring (see "Busy worker, spin and replays"): `store_from_file` finishes the native builder's 11-level files to the 13 levels the tile serves (they were never servable); `dds_direct_skip_*` counters, one per reason an open is not direct; a read trace keeps native reads on (`native_opens`); the load-ahead queue searched through a grid (`load_ahead_waiting`, `_max`). | `dynamic_dds_cache.py`, `getortho.py`, `autoortho_fuse.py`, `read_trace.py`, `load_ahead.py`, `tools/bench/replay.py` | `test_dynamic_dds_cache.py`, `test_native_reads.py`, `test_bench_tools.py`, `test_load_ahead.py` |

The committed libraries were built before `aofast.c`: until CI rebuilds them,
`AoFast.load()` finds no `aofast_*` symbols, the native tests skip and the
mount reads through Python as before. `AO_TEST_AOFAST_LIB` points the tests
at a local build. On Linux (WSL, gcc 13) the native tests pass with the
library built plain and with AddressSanitizer and UBSan. On 2026-11-25 a
Windows `aopipeline.dll` with `aofast.c` was built locally (MinGW gcc 15,
the Makefile's flags) and put in `lib/windows/` for a flight test; it is not
a CI build and must not be committed.

In-process cost of a direct open (no file system; open, the reads, close;
4096 px BC1, medians, `getortho.open_disk_texture` against a temporary cache):

| | Before N2/N3 | After |
|---|---|---|
| Open and close | 508 µs | 100 µs |
| Level 3 (header piece and 4 reads) | 819 µs | 145 µs |
| Level 1 (44 reads) | 1,645 µs | 603 µs |
| Level 0 (171 reads) | 6,600 µs | 2,279 µs |

What is left of an open is two `stat` calls (about 36 µs each on this PC) and
opening the file.

N4 needed two more changes to have any effect, found on the real mount: the
cache refused to store a mipmap 0 that was not all target imagery
(`real_only`), and a live tile with any fallback never asked to be stored
(`_dds_needs_healing`). Both now let through a texture whose only holes are
remembered no-imagery chunks (`DynamicDDSCache._store_no_imagery_holes`,
`Tile._holes_are_no_imagery`, asking the chunk store; a tile's own chunk
objects do not know, as its rows may come from row records).

### Re-measured on the mount (2026-11-25)

`read_cost` as in M0, on the same PC: a lone worker in an isolated home, Bing
ZL16 around Vágar, 25 textures of 4096 px BC1 (two of them coastal, with
no-imagery chunks, now served from disk), medians of runs 2 to 5, the files
in the system's file cache. "Python" is this branch with
`native_direct_reads` off (N1 to N4), "native" with it on (a standalone build
of `aofast.c`). Whole textures in milliseconds; M0 is the table above.

| | Level 0, 1 thread | 8 | 16 | Level 1, 1 | 8 | 16 | Level 3, 1 | 8 | 16 |
|---|---|---|---|---|---|---|---|---|---|
| Plain file (M0) | 1.07 | 1.27 | 1.95 | 0.28 | 0.35 | 0.49 | 0.04 | 0.07 | 0.11 |
| Minimal FS, read in C (M0) | 3.14 | 5.76 | 10.5 | 1.09 | 2.02 | 3.71 | 0.46 | 1.08 | 1.99 |
| AutoOrtho at M0 | 10.0 | 41.8 | 95.6 | 2.79 | 14.1 | 30.5 | 1.23 | 5.87 | 11.2 |
| Python (N1-N4) | 9.4 | 60.7 | 113 | 3.3 | 17.3 | 35.1 | 0.7 | 4.6 | 8.0 |
| Native | **4.0** | **8.4** | **13.0** | **1.5** | **3.7** | **6.7** | **0.7** | **3.3** | **5.6** |

Single reads, native: p50 17 µs and p95 34 µs with one thread (minimal FS in
C: 14.3 µs at M0), 33 and 60 µs with eight (25.7 µs), 47 and 94 µs with
sixteen. Every read of every texture was answered natively
(`dds_native_reads` = the reads made), and every run returned the same bytes
(by hash) with native reads off and on. Opens still go through Python: 0.55 to
0.6 ms with one thread, 1.9 to 4.5 ms with eight and sixteen, now most of a
far texture's time.

What this says:

- Native reads take AutoOrtho to within about 25 % of the minimal file system
  with the read in C (whole textures 4.0 against 3.14 ms, 13.0 against
  10.5 ms at 16 threads), and 2.5 to 7 times below M0. S2's "done when"
  (within 20 % at p50 and p95 per read) is about met with one thread and not
  quite with eight; the busy-worker runs are still to do.
- Without native reads, the Python path with N1 to N4 is faster at level 3
  (the cheap open: 1.23 to 0.7 ms) but not at level 0 or with many threads.
  The row check is gone (N2), yet a level-0 texture with one thread still
  takes 9.4 ms: this run's worker had other Python work at the time (its
  start, load-ahead), which the native path does not feel. The in-process
  numbers above show what N2 and N3 save.
- The open is now the cost that is left (gate C, S5): 0.6 ms at one thread,
  several milliseconds when sixteen readers take turns for the GIL.

Run summary: `tools/bench/runs/read-cost-20261125.json`.

### S3 measured (2026-11-25)

Same setup. Whole textures in milliseconds at 1 / 8 / 16 reader threads,
medians of runs 2 to 5; Python is `native_direct_reads` off.

| | Python | Native |
|---|---|---|
| Far, partial entries (levels 4+), read at level 4 | 0.7 / 3.3 / 5.5 | 0.7 / 2.8 / 4.5 |
| Seasons on, complete, level 1 | 10.0 / 83.0 / 154 | 1.8 / 3.8 / 5.8 |
| Seasons on, complete, level 3 | 1.5 / 12.9 / 18.8 | 0.7 / 2.6 / 4.9 |

- **Seasons** gain most: Python recolours every read with numpy (about
  0.2 ms a read with one thread, milliseconds with many), the library does it
  inline. Every run returned the same bytes with native reads off and on.
- **Partial entries**: a far read takes 32 µs instead of 104 (one thread) and
  31 instead of 315 (eight), but a far texture is two reads, so its time is
  the open's (0.5 to 3.7 ms, Python). The open (S5) is what is left there.
  A lone worker has no camera, so the benchmark pinned the partial level the
  paging model would give (`AO_BENCH_PARTIAL_LEVEL` in a scratch probe).
- **Row checks** were not timed on the mount: unit tests show a texture this
  process stored is read with no check. In flight that covers the textures
  built ahead by prefetch and predictive builds.

Run summary: `tools/bench/runs/read-cost-s3-20261125.json`.

### Safe changes made without a flight (2026-11-25)

- **The end-of-file cut uses only sizes an open recorded.** N1 first cut at
  the size cache, which also holds getattr's estimate from the configured
  zoom. Under dynamic or airport zoom that estimate can be smaller than the
  texture, and a read cut there would hand X-Plane short data. Now
  `AutoOrtho._opened_sizes` holds real sizes only; without one nothing is
  cut in the mount (the tile and the cache file cut at their own size).
- **Opens are timed in the read trace** (`ms`, `tier`, `native`, `tid`), and
  `read_trace.summarize` reports `ready_open_ms` and
  `ready_open_time_share`, the two numbers the new gate C reads.
- **A texture open no longer scans the load-ahead queue.** Every open called
  `LoadAhead.note_texture_request`, which walked the whole queue of waiting
  textures under the lock the load-ahead worker also takes. With 3,000
  waiting (X-Plane loading a few DSFs) that was 165 µs per open; with a tile
  index it is 1.4 µs, the same entries removed.
- **The load-ahead worker no longer scans its queue for the nearest
  texture.** `LoadAhead.take_next` looked at every waiting texture, in
  Python, holding the queue's lock and the GIL, once per texture warmed:
  0.17 ms with 1,200 waiting (2×2 DSFs at ZL16), 0.5 ms with 2,600, 2 ms
  with 10,800, 3.3 ms with 18,000 (2×2 DSFs at ZL18), and every Python
  open of the mount waited behind it. A grid of 0.1° cells, searched in
  growing windows around the focus and stopped at the window's edge
  distance, takes 65 to 165 µs at every one of those sizes and returns a
  texture exactly as near (a randomised test checks it against a full
  scan). A focus far from everything waiting falls back to the scan.
  `load_ahead_waiting` and `load_ahead_waiting_max` (per worker, in the
  STATS lines) show how large the queue gets in flight.

The rest of the Python open was profiled and left alone: of about 100 µs in
`open_disk_texture`, two `stat` calls (21 µs each) and opening and closing
the file are most of it, and the second `stat` (the `.ddm`) is what keeps a
rewritten sidecar from being served stale.

### Busy worker, spin and replays (2026-11-25)

**Read cost with the worker busy (S2's "done when").** `read_cost` as above,
with the `aopipeline.dll` built from this branch. Busy: the same worker
downloads and builds uncached textures (Munich) through a second reader
meanwhile. Spin: two pure-Python threads spin in the worker (M0's worst
case). Later reads in µs, p50 / p95; the minimal C file system from M0 in
brackets; whole textures in ms.

| | Python, idle | Native, idle | Python, busy | Native, busy | Minimal C |
|---|---|---|---|---|---|
| Level 0, 1 thread | 28 / 71 | 14 / 31 | 30 / 308 | 14 / 35 | (14 / 30) |
| Level 0, 8 | 173 / 426 | 25 / 46 | 373 / 1,358 | 25 / 49 | (26 / 47) |
| Level 0, 16 | 297 / 854 | 43 / 84 | 780 / 2,208 | 42 / 108 | (49 / 88) |
| Level 1, 8 | 171 / 409 | 26 / 47 | 387 / 1,303 | 23 / 46 | (24 / 44) |
| Level 0 texture, 1 / 8 / 16 | 6.7 / 36 / 66 | 3.4 / 6.6 / 11.2 | 14 / 93 / 168 | 4.3 / 7.4 / 14.5 | (3.1 / 5.8 / 10.5) |

| Spin (two busy Python threads) | Python | Native | Minimal C, spin |
|---|---|---|---|
| Level 1 read, 1 thread | 158 ms / 412 ms | 14 / 30 µs | (15 / 31 µs) |
| Level 1 texture, 1 thread | 8.5 s | 0.66 s (the open) | |

- **S2's "done when" is met at levels 0 and 1**: native reads are within
  20 % of the minimal C file system at p50 and mostly at p95 (0.8 to 1.4
  times), idle and busy, at 1, 8 and 16 threads. A busy worker leaves them
  as they were; the Python path gets 2 to 16 times slower and its slowest
  reads reach 12 ms. Under spin a Python read waits 158 ms, a native one
  14 µs.
- **Level 3** (far textures, 4 reads each) at 8 and 16 threads with the
  worker busy: p95 0.1 to 1 ms. Those reads queue behind slow Python opens
  for the file system's dispatch threads. Under spin a texture is its open:
  0.6 to 2 s. That is S5's case, and the open is what gate C weighs.
- 81,725 of 82,125 reads were answered natively (idle and busy); the other
  400 are first reads of level-0 rows not checked yet in this process,
  answered by Python on purpose (S3).

Summary: `tools/bench/runs/read-cost-busy-20261125.json`.

**Replays (S1 and S3).** A synthetic route (`make_scenario.py`): the
Faroes, Vágar eastward at 250 kt and 3,000 m, 60 s of loading then 8
minutes of flight, ZL16 textures within 25 km: 239 opens, 6,820 reads. The
whole app from source (parent, worker, brokers) in an isolated home, Bing,
positions from the fake X-Plane; cold runs on a new cache.

- **`smooth`**: no read in flight above 100 ms (the slowest 26 ms; the
  worker's own trace). The loading screen still waits (up to 380 ms here),
  as Part 3 leaves the start wait alone. S1's check holds for `smooth`.
- **`quality`**: no level-0 bytes served from a lower zoom, as asked, but
  65 MB of level 0 served in `missing_color`: the chunks Bing has no
  imagery for at ZL16 (the sea). `quality` sets `strict_target`, which turns
  every fallback off (`_get_fallback_level` returns 0), so a chunk that
  will never arrive is painted flat where `balanced` upscales the lower
  zoom. **Decided and fixed (2026-11-25):** under `quality` a chunk the
  provider has no imagery for (`failure_reason == "no_imagery"`) gets the
  configured `fallback_level` (`Tile._no_imagery_fallback_level`, passed to
  `FallbackResolver` as `no_imagery_level`); every other chunk stays
  strict. A `fallback_level = none` the user set is kept.
- **Warm cache**: only 16 of 239 opens were direct. Counting why (new
  `dds_direct_skip_*` counters) found:
  1. *Native builds stored 16 bytes short.* The native builder (aodds.c)
     stops at the 4×4 level: 11 levels for a 4,096 px texture, while the
     tile, the cache and X-Plane count 13. `store_from_file` stored those
     files as they came, and every open then refused them (size). In the
     replay caches 133 of 152 complete entries were short: most complete
     textures could never be served from disk. **Fixed**: the stored file
     is finished with the tile's header and the 4×4 block copied into the
     2×2 and 1×1 levels, the bytes a live tile serves there. Entries
     already short stay refused until rebuilt and stored again.
  2. *Partial entries too coarse* (106 opens): the cold flight read these
     textures from level 1, but what was stored starts at level 4 (the
     ZL12 imagery load-ahead warms). The level-1 data the live tiles built
     is not kept as a partial entry.
  3. *No level during the load* (61 opens): partial entries are served
     only when the paging model names a level, and it has no camera on
     the loading screen.

  After the fix, the warm replay with native reads: 70 direct opens (all
  native) instead of 16, ready share 29 % instead of 7 %, read p50 0.11
  instead of 0.29 ms, the app's CPU 39 instead of 55 s. Items 2 and 3 are
  where the rest of the ready share is; they belong with the prediction
  plan (what to store and when), not here.
- **S3's 95 %**: of the reads of direct textures in the warm replay, 3,421
  were native and 315 went to Python, 92 %: all 315 are first reads of
  level-0 rows of a cache written by an earlier process (checked once per
  process, by design). Within one process it is 99.5 % (above). Keeping
  checked rows across sessions would reach 95 % here, but would trust a
  file the process has not read; not done.
- **A read trace no longer turns native reads off.** With both on, the
  trace keeps opens (marked `native`) and releases, and `summarize` counts a
  native open as ready (`native_opens`); its reads are not in the trace.
  So gate C can be read from a flight with native reads on. A trace meant
  for replaying needs every read: record it with native reads off.
- Gate C on this synthetic route (record only, the gate is for a real
  flight): ready share 0.29, open p95 4.3 ms: closed by criterion 1.

Summary: `tools/bench/runs/replay-faroes-20261125.json`.

**Found on the way, outside this plan:** `ChunkGrid.ensure_range` reads its
slots back after releasing its lock, so a tile closed meanwhile raises
`KeyError` and the prefetch coordinator logs "admission failed" (2 to 10
times per replay). The candidate was for a closed tile, so nothing is lost
but the log line; still a race to fix.

### Flights and closure (2026-11-25)

Three flights over the same route (central Mexico, max zoom 17, WinFsp),
with `AO_READ_TRACE` and `native_direct_reads` on, the third under `smooth`.

| | Flight 1 | Flight 2 | Flight 3 |
|---|---|---|---|
| Traced minutes | 26 | 25 | 7 |
| Texture opens / textures | 31,326 / 3,583 | 48,316 / 3,562 | 18,984 / 2,245 |
| Opens served from the cache file | 0 | 4 | 16, all native (147 reads, 101 MB) |
| Worker had the aircraft position | no | no | yes |
| Errors from the mount or native reads | none | none | none |

- **Native reads are stable** in real flights: installed in every session
  ("Native texture reads on"), no crash, no fallback, and the opens they
  served took 0.8 ms (p50).
- **They had almost nothing to serve.** Two causes, neither in the read
  path. (1) *The mount worker lost the parent's statistics store* 30 to
  40 s into flights 1 and 2 and never got it back, so it had no aircraft
  position: no prefetch ahead, no partial entries. Found with new log lines
  (`14c21f52`) and fixed (`447c5a59`: WinError 10048, one connection per
  short-lived thread; the worker now shares one and reconnects). (2) *The
  cache does not hold the route*: in flight 3, with the position back,
  89 % of the opens found no cache entry for a route flown twice an hour
  before. That is the subject of
  [ready-share-plan.md](ready-share-plan.md).
- **Gate C: closed on its numbers, but they do not mean what the gate
  meant.** Ready share 0.08 % (criterion 1 needs 50 %), so by the gate S5
  is not built. The flights showed the gate asks the wrong question twice
  over: the ready share is low because of the cache, not because few
  textures could be ready; and the opens that cost X-Plane time are not
  the ready ones but the 85 % of opens that read nothing (5 to 7 ms each,
  112 to 254 s per flight). The new plan measures those and decides there.

**Definition of done, as closed:**

1. Done: `texture_wait_policy` in the UI and docs; `smooth` has no
   in-flight read above 100 ms on the replay; `quality` fills no-imagery
   chunks from a lower zoom (decided 2026-11-25).
2. Done at levels 0 and 1, idle and busy (within 20 % of the C file
   system). Level 3 with many readers is bound by the Python open.
3. 92 % on the warm replay, 99.5 % within one process: the rest are first
   reads of rows checked once per process, by design. Accepted.
4. Done: byte equality on every run, native off and on.
5. Done: with the library missing or the setting off, tests and behaviour
   are as before.
6. Done: flights flown, no crash, no unexpected fallback. Gate C read, see
   above.
7. Done: gate B closed (M0), gate C closed (above).

**Carried over to [ready-share-plan.md](ready-share-plan.md):** the cache
budget, the opens that read nothing, partial entries on the loading
screen and live-built levels (prediction plan F11, F12), turning
`native_direct_reads` on by default (needs CI's libraries and a flight on
them), recorded runs on Linux and macOS, and S5 under a new gate. S4 (the
OrthoStudio comparison) stays deferred.

This follows [performance-gap-plan.md](performance-gap-plan.md) and
[resource-usage-plan.md](resource-usage-plan.md), and uses the terms of
[read-fallbacks.md](read-fallbacks.md).

## The idea in short

AutoOrtho and OrthoStudio XP Live both predict what X-Plane will read and build
it ahead. They differ in what a read costs and in what happens when the
prediction was late.

| | AutoOrtho today | OrthoStudio XP Live | This plan |
|---|---|---|---|
| Texture ready | read passes through the virtual disk and Python | X-Plane reads an ordinary file | read passes through the virtual disk and a few lines of C, no Python |
| Texture not ready | waits a bounded time, then serves coarser imagery | serves the coarser file at once | the user chooses: wait for full quality, today's bounded wait, or never wait |
| Prediction wrong (teleport, turn) | the read starts the build | neutral colour until X-Plane loads the place again | as AutoOrtho today |

So: keep the virtual disk, because it is the only thing that can make X-Plane
wait and the only safety net for a wrong prediction, and make it cost almost
nothing when it has nothing to decide.

## M0 results (2026-11-24) and what they change

Measured with [tools/bench/read_cost](../../tools/bench/read_cost/README.md) on
this PC (Ryzen 9 9950X3D, Windows 11, Python 3.14.7 with the GIL), idle, on the
working tree of `fix/prefetch-loop-and-dds-staging` (`48a2fb14` plus
uncommitted changes), with a lone mount worker in an isolated home: Bing
imagery, zoom 16, default settings otherwise. 34 textures of 4096 px (BC1,
11.2 MB) that AutoOrtho serves straight from its cache, read by a C client the
way X-Plane reads (the header piece, then from a level's start to the end in
64 KB pieces). Every target read the same files, already in the system's file
cache, and returned the same bytes (checked by hash). Medians of four runs. The
summary is in `tools/bench/runs/read-cost-20261124.json`.

The targets:

- **Plain file**: the cache files on NTFS. The floor.
- **Minimal file system, read in C**: a passthrough over the cache folder
  through mfusepy, whose `read` is a C function swapped into the
  `fuse_operations` struct (the hook Part 1 proposes). Open stays Python.
- **Minimal file system, read in Python**: the same with the least Python a
  read can be (lock, seek, read).
- **AutoOrtho**: its mount, textures ready.

### One reader thread, worker idle (WinFsp)

Times in microseconds, whole textures in milliseconds.

| | Open | First read | Later reads | Level 0 (171 reads) | Level 1 (44) | Level 3 (4) |
|---|---|---|---|---|---|---|
| Plain file | 23 | 8 | 5.2 | 1.07 ms | 0.28 ms | 0.04 ms |
| Minimal, read in C | 350 | 40 | 14.3 | 3.14 ms | 1.09 ms | 0.46 ms |
| Minimal, read in Python | 350 | 75 | 22.5 | 4.83 ms | 1.50 ms | 0.54 ms |
| AutoOrtho | 710 to 780 | 330 | 27.6 (mean 52 at level 0) | 10.0 ms | 2.79 ms | 1.23 ms |

Inside AutoOrtho's Python (the probe, per call): open 490 to 510, read 30 to 37
(of which `DiskTexture.read` 24 to 33), release 23 to 33, getattr 14 to 16,
twice per open.

### Eight reader threads, worker idle

| | Open | Later reads | Level 0 | Level 1 | Level 3 |
|---|---|---|---|---|---|
| Plain file | 27 to 47 | 5.8 | 1.27 ms | 0.35 ms | 0.07 ms |
| Minimal, read in C | 820 to 965 | 25.7 | 5.76 ms | 2.02 ms | 1.08 ms |
| Minimal, read in Python | 710 to 820 | 88.8 | 17.8 ms | 5.56 ms | 1.64 ms |
| AutoOrtho | 2,830 to 3,680 | 173 to 386 | 41.8 ms | 14.1 ms | 5.87 ms |

### How many threads X-Plane reads with

From the session reports of past flights on this PC
(`~/.autoortho-data/reports`). Each report keeps the 200 slowest operations
with their start, length and texture. Reads that were in flight at the same
moment on different textures are different X-Plane threads, each blocked on its
own read.

| Session | Reads in flight at once | On different textures |
|---|---|---|
| 2026-11-19 22:10 | 17 | 17 |
| 2026-11-17 11:56 | 16 | 16 |
| 2026-11-22 23:10 | 11 | 11 |
| 2026-11-21 13:06 | 9 | 9 |

The most textures X-Plane held open at once (`tile_cache.open_references`) was
23 to 32 in every flight of 15 minutes or more, and never above 32. This PC has
32 logical cores. So X-Plane reads with at least 17 threads here, and most
likely with one per logical core. This shows the size of its pool, from reads
that were waiting; how often that many fast reads overlap is not recorded.

### Sixteen and thirty-two reader threads, worker idle

Whole textures, in milliseconds.

| | Level 0, 16 threads | 32 threads | Level 1, 16 | 32 | Level 3, 16 | 32 |
|---|---|---|---|---|---|---|
| Plain file | 1.95 | 2.91 | 0.49 | 0.77 | 0.11 | 0.12 |
| Minimal, read in C | 10.5 | 17.9 | 3.71 | 6.47 | 1.99 | 3.38 |
| Minimal, read in Python | 51.9 | 108 | 15.0 | 30.7 | 3.39 | 6.30 |
| AutoOrtho | 95.6 | 191 | 30.5 | 58.7 | 11.2 | 21.1 |

AutoOrtho's time per texture grows in step with the number of readers (10,
41.8, 95.6, 191 ms at 1, 8, 16, 32 threads): the readers take turns, so the
whole mount serves about 140 level-0 textures a second however many threads
ask. The 34 textures took 0.24 s through AutoOrtho, 0.05 s through the read in
C and 0.03 s from plain files. With 32 readers an AutoOrtho open takes 14 ms
(median), two thirds of a far texture's 21 ms.

### Worker busy

AutoOrtho's ready textures, read while the same worker downloaded and built 80
other textures:

| | Idle | Busy |
|---|---|---|
| Level 1, one thread | 2.79 ms | 5.32 ms |
| Level 1, eight threads | 14.1 ms | 32.8 ms |
| Level 3, one thread | 1.23 ms | 1.67 ms |
| Level 3, eight threads | 5.87 ms | 7.89 ms |

The slowest single ready read in any busy run took 4.6 ms; the 95th percentile
stayed under 2 ms. Real builds hold the GIL little, because decode and
compression are native code that releases it.

The worst case, made on purpose: two threads running pure Python without pause
in the minimal file system's process.

| | Idle | Two busy Python threads |
|---|---|---|
| Read in Python | 22 µs | 64 ms |
| Read in C | 14.3 µs | 14.6 µs |
| Open (Python in both) | 0.35 ms | 190 to 300 ms |

### Dokan

The minimal file system on Dokan 2.3.0 gives the same picture: a later read
takes 16.4 µs in C and 24.8 µs in Python (one thread), 27 and 93 µs (eight
threads). The C read hook works unchanged on both drivers.

### Where AutoOrtho's time goes

For one level-0 texture, one thread: 10.0 ms against a floor of 1.07 ms. Of the
8.9 ms above the floor:

| Part | Time | How it was derived |
|---|---|---|
| Kernel round trip | 2.1 ms | minimal read-in-C minus plain file |
| The least a Python callback costs | 1.7 ms | minimal read-in-Python minus read-in-C |
| The row check, repeated at every open | about 4.2 ms | 9 % of reads take 270 µs; later reads average 52 µs against a median of 28 |
| The rest of AutoOrtho's Python (open, classify, counters) | about 0.9 ms | remainder |

For a far texture (level 3) the open is most of it: 0.71 of 1.23 ms, then the
first read with its row check, 0.33 ms.

CPU is not the issue. The worker spent about 9 ms of CPU per level-0 texture
and 1 to 2.5 ms per far one. If every one of a flight's 4,330 opens were ready
that is about 10 CPU seconds, under 1 % of the 1,204 the worker used on the
flight of 2026-11-22. What ready textures cost is X-Plane's time: about 11 s of
its loader threads over that flight with one thread reading, four to five
times that with eight, against under 1 s from plain files. For comparison, X-Plane spent 325 s
in reads of textures that were not ready on that flight.

### Findings

1. **The row check runs at every open, not once per file.** The record of
   verified rows lives on the shared file object, which
   `disk_texture._release_file` drops when the last open closes. The first pass
   in a fresh process cost the same as every later pass (9.9 and 10.0 ms). This
   is about 4 ms of each level-0 texture and 0.3 ms of every open of a complete
   texture.
2. **An open costs 0.5 ms of Python.** It reads and parses the `.ddm` sidecar
   and checks the file each time. With eight threads it takes 2.2 to 2.8 ms,
   because the threads take turns.
3. **Python makes concurrent readers take turns.** Eight threads read a
   texture four to five times slower than one. The read in C keeps its speed
   (26 µs) and is untouched by busy Python threads.
4. **Textures with chunks the provider has no imagery for are never served
   from disk.** 14 of the 48 textures cached for this test (open sea around
   Vágar) had complete cache files and still went through a live tile at every
   open, because their level-0 rows do not count as exact.
5. **A live texture answers reads past its end with data.** On a texture
   served by the live path, a read at or beyond the file size returns a full
   64 KB every time, even 50 MB past the end, and a read that straddles the end
   returns 64 KB where 100 bytes remain. A ready texture answers correctly.
   X-Plane never reads past the end, but any program that reads until
   end-of-file (an antivirus, an indexer, a copy) never stops: the first
   version of the test client made 10 million reads of four textures and used
   608 s of the worker's Python time before it was stopped.
6. **The file-system library's open path costs about 0.33 ms** whatever the
   file system does (350 µs against 23 µs for a plain file). No design that
   keeps the virtual disk gets a far texture below about 0.45 ms.

### The gates

- **A (build the native read, S2): open.** Python accounts for 65 to 77 % of
  what AutoOrtho adds to a plain-file read, above the 50 % the gate asked for.
  The second criterion is not met: a ready read with the worker busy stayed
  under 2 ms at the 95th percentile, not above 5 ms. The decision is to
  build it, but not first: findings 1 and 2 take out about two thirds of
  AutoOrtho's own cost in Python alone, on every platform, at far lower risk.
  What is left for S2 is concurrency, and the session reports settle that:
  X-Plane reads with 17 or more threads, where the read in C is five to ten
  times faster per texture than AutoOrtho today.
- **B (reads that leave the virtual disk, S6): closed.** By its second
  criterion the gate is open: the round trip adds 190 % to a level-0 texture.
  That criterion was badly set, relative to a floor too small to matter. In
  absolute terms the round trip costs 0.4 to 2 ms per texture, about 3 s of
  loader time over a flight, and about 0.3 % of the worker's CPU, for mechanisms
  that differ on every platform and give up seasons, the paging model's samples
  and the wait. Not worth building on these numbers.
- **C (native open and getattr, S5): not evaluated as written** (it needs the
  `cruise-cached` replay; on 2026-11-25 the gate was rewritten: see "Gates"
  under "M0. Measure a ready read" below). The evidence points at the open: it is most of a
  far texture's cost, and with 32 readers it takes 14 ms because the opens
  take turns. The cheaper answer comes first (finding 2, in Python); S5 is
  built after S2 if opens still take turns for milliseconds then.

### Order of work after M0

Ranked for optimization, quality, speed, stability and all platforms alike.
Each is its own branch.

| # | Item | Why now | Size | Risk |
|---|---|---|---|---|
| N1 | Reads past the end of a live texture return nothing (finding 5) | stability: a third program reading a texture can pin the worker | S | low |
| N2 | Verified rows survive a close (finding 1) | level-0 textures 10.0 to about 5.8 ms; every complete open 0.3 ms shorter; pure Python, all platforms | S | low |
| N3 | A cheap open: the parsed cache entry kept in memory, checked against the file's size and time (finding 2) | far textures 1.23 to about 0.6 ms; less turn-taking with many readers | S to M | low |
| N4 | Textures with no-imagery chunks served from disk (finding 4) | 29 % of this test's textures; coasts and islands | M | medium |
| N5 | `texture_wait_policy` (S1) | the user's choice between quality and smoothness | S | low |
| N6 | A recorded flight: the share of opens that are ready, and how many reads overlap when they are fast (thread ids added to the read trace) | the share of ready opens is the number that matters most, and it is still unknown | M | low |
| N7 | Native reads (S2, then S3) | X-Plane reads with 17 or more threads: with 16 readers, level 0 from 95.6 to about 16 ms; immune to busy Python | M | medium |
| N8 | `read_cost` on Linux and macOS (the client and the probe with `pread`) | the other platforms measured, not assumed, before N7 ships there | S | low |

N1 to N3 are to be re-measured with `read_cost` before and after. After N2 and
N3 the expected figures for one thread are about 5.5 ms (level 0), 2.0 ms
(level 1) and 0.6 ms (level 3). N4 needs care: "the provider has no imagery
here" must be told apart from a download that failed, or a texture would be
frozen with a hole in it.

Not done in M0: the busy and idle runs were made three to five times, not on a
replayed flight; nothing was measured on Linux or macOS; the share of opens
that are ready in a real flight is still the figure from before the fixes
(11 %).

## Terms

- **Texture**: one `.dds` file X-Plane opens, named `row_col_maptype_zoom.dds`.
- **Level**: one mipmap of a texture. Level 0 is the sharpest (4096 px, or
  8192 px one zoom above the mesh).
- **Ready**: the DDS cache file of the texture holds, on disk and valid, every
  level X-Plane wants at this open.
- **Direct read**: a read answered from the cache file
  ([disk_texture.py](../../autoortho/disk_texture.py)). Today it still runs
  Python.
- **Live read**: a read answered by a `Tile` in memory, which downloads and
  builds what is missing.
- **Stand-in**: coarser imagery served in place of a level that is not built.

## Where AutoOrtho is now

### The mount

`autoortho_fuse.AutoOrtho` is an `mfusepy.Operations` object. mfusepy is a
ctypes binding: it fills a C `fuse_operations` struct with Python callbacks and
calls `fuse_main_real`. The kernel side is WinFsp or Dokan on Windows, macFUSE
on a Mac, libfuse on Linux. The whole scenery pack is mounted (DSF, `.ter`,
`.png` and textures), not only the textures.

Every call X-Plane makes on the pack therefore becomes a Python call on a
thread the file-system library owns. That thread must take the GIL first. The
worker's Python is a GIL build (checked on the `ao314` environment: Python
3.14.7, `sys._is_gil_enabled()` is true, switch interval 5 ms).

### What one texture costs today

X-Plane reads a texture in 64 KB pieces
([read_pattern.py](../../autoortho/read_pattern.py)): first the header piece
(the 128-byte header and the start of level 0), then from the piece that holds
the start of the level it wants to the end of the file. A 4096 px BC1 texture
is 11.2 MB:

| X-Plane wants | Bytes read | Reads |
|---|---|---|
| level 0 (4096 px, next to the camera) | 11.2 MB | about 171 |
| level 1 (2048 px) | 2.8 MB | about 44 |
| level 3 (512 px) | 0.2 MB | about 4 |

macFUSE splits each of these into 32 KB pieces, so twice as many calls there.

For each open of a texture, in Python
([autoortho_fuse.py](../../autoortho/autoortho_fuse.py), `open`):

1. Match the path, resolve the map type, note the request for load-ahead.
2. `getortho.open_disk_texture`: check no live tile holds the texture, work out
   the target zoom, ask the paging model which level X-Plane wants, then
   `DynamicDDSCache.open_direct`: read the `.ddm` sidecar (JSON, with the row
   manifests), check it is not stale, `stat` the file, pin the entry against
   eviction.
3. If that gives an entry, make a `DiskTexture`. Otherwise open a live `Tile`.

For each read of a direct texture, in Python (`_read`, then
`DiskTexture.read`):

1. mfusepy's wrapper: path bytes to `str`, the result copied into the buffer.
2. Match the path again, resolve the map type, check for shutdown and injected
   faults, find the handle.
3. Classify the read (header piece, wanted level) and feed the paging model.
4. The first time each level-0 row is read: read the row and check its SHA-256
   against the manifest. The code means this to happen once per file; M0 found
   it happens at every open (finding 1).
5. `pread` from the cache file. For a partial entry, build the answer from the
   file's header, stand-in bytes and the file's levels.
6. Apply the season colour table, if one applies
   ([colour_table.py](../../autoortho/colour_table.py), numpy).
7. Count the read.

### Three ways a read is answered

| Way | When | Cost |
|---|---|---|
| Direct, full | cache file complete and exact at level 0 | the Python above; no tile, no decode |
| Direct, partial | cache file holds the level the paging model expects and coarser (`partial_direct_reads`) | the same; a read that wants a finer level switches the open to a live tile |
| Live | anything else | a `Tile` in memory, downloads, decode, compress, waits by band |

### What already predicts

All on by default ([aoconfig.py](../../autoortho/aoconfig.py)):

- The path ahead: velocity and the SimBrief route, `prefetch_lookahead` 10
  minutes, `prefetch_radius_nm` 30.
- X-Plane's own hints: a DSF open queues its neighbours; a `.ter` open queues
  its texture 20 to 45 s before X-Plane reads it (`ter_load_ahead`).
- The level by distance: `paging_prefetch` fetches each texture only to the
  level X-Plane will want `paging_lead_seconds` (60) from now. The paging
  constant is measured in flight from the reads themselves.
- Building ahead: `predictive_dds_enabled` builds the textures in the
  background and stores them in the DDS cache.

### What happens when a texture is not ready

- **Wait bands** ([wait_bands.py](../../autoortho/wait_bands.py)): a near or
  middle read waits up to `band_max_wait_seconds` (6 s) in flight and
  `near_wait_seconds` (60 s) while a flight loads; a far read waits
  `far_wait_seconds` (0.5 s).
- **Quality mode** (`prefetch_quality_mode`): `responsive` (bounded wait, then
  lower-zoom imagery), `prefer_target` (a short grace period for exact
  imagery), `strict_target` (never lower-zoom imagery in level 0, up to
  `strict_target_deadline_sec`, 120 s).
- **After the wait** ([read-fallbacks.md](read-fallbacks.md)): the tile's own
  bytes, with unbuilt levels upscaled from the nearest coarser built level.
  `low_zoom_base` keeps the ZL12 chunks on disk for good, so this is real
  imagery almost everywhere after a first visit.

X-Plane reads each texture once and keeps what it got until it opens the
texture again. Whatever a read is answered with stays on screen until then.

### What is measured, and what is not

| Fact | Value | Source |
|---|---|---|
| Opens served direct | 476 of 4,330 (11 %) | flight of 2026-11-22, before the fix that saves background builds and before partial direct reads |
| Events in a read trace | 833 for nine textures | smoke test of `AO_READ_TRACE` |
| Worker CPU in the kernel | 44 % | same flight, inflated by the prefetch loop since fixed |
| GIL | enabled, 5 ms switch interval | `ao314` environment |

Not measured:

- The share of direct opens since the fixes. The plan's baseline B0 has not
  been run.
- What a direct read costs, and how that splits between the kernel round trip,
  the wait for the GIL and the Python work.
- How many `getattr` and passthrough calls (DSF, `.ter`, `.png`) a flight
  makes.

The second of these is now measured ("M0 results" above). The first and third
still need a flight. The rough count made before the measurement, kept here
because it turned out right about CPU and about latency: 4,330 opens at 44 to 171 reads each is
200,000 to 700,000 reads a flight. At some tens of microseconds of Python each
that is 10 to 30 CPU seconds in a worker that used 1,204. If that holds, the
CPU gain of a native read path is small, and its value is latency: a read that
has to wait for the GIL while other Python threads work can wait milliseconds,
many times per texture.

## What OrthoStudio does for the same cases

From its sources (`D:\Programs\orthostudio-xp`, v2.4.0,
`src/orthostudio/live/`):

- Textures are real files in the scenery pack. A planner runs every 2 s and
  builds, for every texture within 300 km, the level X-Plane will read of it
  (`rings.py`: distances measured on probe flights). A finished file replaces
  the old one with `rename`.
- X-Plane's read is an NTFS read. OrthoStudio is not involved and cannot delay
  it.
- At a flight's start its X-Plane plugin holds the loading screen until
  everything X-Plane will read there is built (`gate.py`).
- In flight, a texture built late is read coarse. It copies the level below up
  into the missing one so the result is blur and not the neutral colour. After
  a jump it did not foresee, what X-Plane already read stays neutral until
  X-Plane loads the place again.
- On Windows it cannot replace a file X-Plane holds open; it logs that and
  leaves the file.
- It has no way to know which level X-Plane read. It relies on its measured
  rings.

## What we want

1. **A ready texture costs no Python.** Its reads are answered by native code
   from the cache file, on the file-system library's own thread, without the
   GIL.
2. **Later, a ready texture costs no Python at open either.**
3. **When a texture is not ready, the user decides**, with one setting:
   - *Full quality*: the read waits until the texture is built at the zoom the
     user set. X-Plane may stutter or hold its loading screen longer.
   - *Balanced*: today's behaviour.
   - *Never wait in flight*: the read is answered at once with the best
     imagery at hand, as OrthoStudio does. The build goes on behind it.
4. **Never the wrong bytes.** The native path serves exactly what the Python
   direct path serves today, byte for byte. On any doubt it hands the read to
   Python. No I/O error ever reaches X-Plane (it crashes on Windows).
5. **Every platform keeps working.** Without the native library, or with it
   switched off, the mount behaves as today.

Not goals:

- Removing the virtual disk. See "Alternatives".
- Rewriting the file system in another language. The logic that decides what a
  texture is (target zoom, staleness, seasons, streaming, time exclusion) stays
  in Python; only the case with nothing left to decide moves.

## Constraints

These come from the code and from earlier incidents. Each one rules out
something that looks simpler.

1. **A plain file cannot wait.** Holding a read until a build ends needs
   something between X-Plane and the file. *Full quality* therefore needs the
   virtual disk (or another interception) for every texture that is not ready.
2. **X-Plane reads once.** A coarse answer stays until X-Plane opens the
   texture again, which it does only for a sharper level or a scenery load.
   *Never wait* cannot promise that the sharp texture appears as soon as it is
   built.
3. **The reads are AutoOrtho's measurement of X-Plane.** Each read says which
   level X-Plane wanted; the paging model is fitted to that. A path that hides
   reads from AutoOrtho must still report the wanted level, or the model drifts.
4. **Season colours are applied as bytes are served** (`read_time_colours`).
   The cache file holds the photos as downloaded. Bytes X-Plane reads straight
   from the cache file would have no season.
5. **One texture name maps to several cache files.** The cache path includes
   the target zoom, entries may be compressed (zstd), stale, or evicted, and an
   open pins its entry.
6. **Level-0 rows are checked against their manifest on first read.**
7. **Windows cannot replace a file X-Plane holds open.**
8. **Three operating systems, two Windows drivers, no administrator rights.**

## Design

### Overview

A read goes to the first tier that can answer it:

```
X-Plane read
   |
   v
kernel driver (WinFsp, Dokan, macFUSE, Linux FUSE)
   |
   v
tier 1  native   handle is in the native table and the bytes are in the file
   |             -> pread into the reply buffer, count, return        (no GIL)
   v
tier 2  Python direct   today's DiskTexture.read (row check, seasons, partial)
   |
   v
tier 3  Python live     Tile: download, build, wait by policy, stand-in
```

Tier 1 is new. Tiers 2 and 3 are today's code, unchanged. Tier 1 never decides
anything: it answers only what Python declared answerable when the texture was
opened.

### Part 1. A native read path

**The library.** `aofast`, in C, built next to `aopipeline` with the same three
Makefiles and loaded the same way. C and not Rust because the toolchain, the
build scripts and the packaging for three systems already exist for C; a Rust
library would add a toolchain to every build for about 500 lines of code. The
code is small enough that C's risks are manageable with the tests below.

**The hook.** mfusepy builds the `fuse_operations` struct in `FUSE.__init__`
and passes it to `fuse_main_real`. A subclass replaces the struct's `read`
field with `aofast_read` before the mount starts and gives the library the
Python callback it replaced. `aofast_read` has the standard signature, the same
in the FUSE 2 API (Windows, Mac) and FUSE 3 (Linux). The offset of `fh` inside
`fuse_file_info` differs by platform; Python reads it from mfusepy's ctypes
struct and passes it at start, so the C code includes no FUSE header.

**The handle table.** A fixed array of slots (4,096 to start). When Python's
`open` gets a `DiskTexture`, it registers it:

```
aofast_register(os_handle, file_size, level_table, finest_level,
                standin_block, flags) -> slot
```

and returns to FUSE a file handle with a tag bit set and the slot in the low
bits. Untagged handles (live tiles, DSF, `.ter`) go straight to the Python
callback, at the cost of one comparison.

**A read, in `aofast_read`:**

1. `fh` not tagged, or the fast path switched off: call the Python callback.
2. Take the slot (reference count up; a slot being released refuses).
3. Clip the range to the file size. Classify the read with the same rules as
   `read_pattern.classify` (header piece, wanted level), kept in the slot's
   cursor.
4. Decide whether the file can answer:
   - the range lies in levels the slot holds, or is the incidental part of a
     finer level (then stand-in bytes, as `DiskTexture._standin` builds them);
   - every level-0 row in the range is marked verified;
   - no season table applies to the slot.
   If not: call the Python callback. It answers through tier 2 and updates the
   slot (marks rows verified, or switches the open to a live tile and clears
   the tag's slot).
5. `pread` (Windows: `ReadFile` with an `OVERLAPPED` offset) straight into the
   reply buffer. A short read or an error: call the Python callback, which
   marks the file bad as today.
6. Add to the slot's counters: reads, bytes, and the wanted level if this read
   revealed it.

**Statistics without Python.** Python drains the counters of every slot once a
second and at release, and feeds `dds_direct_reads` and the paging model
(`note_texture_wanted`) from them. The model gets the same samples as today, a
second later.

**The read trace.** With `AO_READ_TRACE` set, the fast path is off, so a trace
still holds every read with its latency. A trace records behaviour, not this
path's speed.

**Lifetime.** `release` in Python unregisters the slot, waits for its reference
count to reach zero, then closes the `DiskTexture`, which unpins the cache
entry. Eviction cannot remove a file a slot uses, because the entry is pinned
for as long as the `DiskTexture` lives, as today.

**Switch.** `[streaming] native_direct_reads`, default off until stage S2's
gate passes, then on. Off, or the library missing, is exactly today.

### Part 2. Three things the native path must learn to keep

Part 1 alone hands many reads back to Python. Three additions close that:

- **Row checks (S3).** Today the first read of each level-0 row in a process
  goes to Python for its SHA-256. For a near texture opened for the first time
  in a session that is every level-0 read. Either the library checks the row
  itself (one small SHA-256 implementation, the digests passed at register), or
  the check moves off the read path: verified once when the file is stored by
  this process, and in the background at first open otherwise. M0's numbers
  choose between them.
- **Season colours (S3).** `colour_table.apply` maps the two RGB565 colours of
  each block through a 65,536-entry table. In C that is a loop over the reply
  buffer. The table is passed at register and replaced when the season
  changes.
- **Open and getattr (S5).** A ready table in the library, keyed by texture
  name, filled by Python when a cache entry becomes servable and cleared when
  it is evicted, goes stale, or the zoom or season settings change. A native
  `open` that finds the name answers without Python and queues a note for
  Python (the request, the pin). Built only if M0 shows that opens and
  `getattr` calls cost enough to matter.

### Part 3. One setting for what happens when a texture is not ready

A new setting, shown in the UI as **"When scenery is not ready"**, that sets
the existing knobs together. No new mechanism.

`[streaming] texture_wait_policy`:

| Value | In flight | While a flight loads | Level 0 may hold lower-zoom imagery |
|---|---|---|---|
| `quality` | waits up to `strict_target_deadline_sec` (120 s) | waits (60 s per read, as today) | no (`strict_target`) |
| `balanced` (default) | near and middle 6 s, far 0.5 s | 60 s | yes, after the wait |
| `smooth` | no wait in any band | 60 s | yes, at once |

Notes on each:

- `quality` keeps a deadline. Without one, a provider that stops answering
  would freeze X-Plane for good. After the deadline the read is answered as a
  fallback is today and counted.
- `smooth` still waits on the loading screen. X-Plane is not drawing then, so a
  wait costs no stutter, and OrthoStudio holds the loading screen too. In
  flight a read is answered with the tile's own bytes: built levels as they
  are, unbuilt levels upscaled from the nearest coarser one, never a neutral
  colour where any imagery exists. The build X-Plane's read started keeps the
  reader's priority, so the texture is ready for X-Plane's next open.
- In `smooth`, constraint 2 applies: the coarse texture stays until X-Plane
  opens it again. The doc and the UI text must say so.
- A user who sets the individual knobs keeps them: the policy only supplies
  defaults for values not set by hand, as auto-tuning does.

This part is independent of the native path and is the part users see. It can
ship first.

### Part 4. Reads that leave the virtual disk (only if M0 asks for it)

After Part 1 a ready read still makes the kernel round trip: X-Plane to the
driver, to the worker, and back. If M0 shows that this round trip, and not
Python, is most of a ready read's cost, three mechanisms can remove it. Each
decides per open, not per read, so each needs the file to be right for the
whole open.

| Mechanism | System | How | Not verified |
|---|---|---|---|
| FUSE passthrough | Linux 6.9+, libfuse 3.17+ | at open, the worker hands the kernel a descriptor of the cache file; the kernel serves the reads | privileges it needs; mfusepy support |
| Projected File System | Windows 10 1809+ | the pack becomes a projected folder; a file is filled on first read by a callback that may wait, and is an ordinary file afterwards | whether a file can be filled level by level; the optional Windows feature must be enabled once with administrator rights |
| Symbolic link answer | all | the mount reports a ready texture as a link to its cache file; the system opens the cache file itself | WinFsp and Dokan support for links that leave the mount; attribute caching when a texture changes from link to file |

What every one of them has to solve (the constraints above):

- **Seasons (4).** The file must already hold the season's colours, or the
  mechanism is used only where no table applies.
- **The paging model (3).** AutoOrtho sees the open, not the reads. The model
  would be fitted on the textures that were not ready only.
- **A wrong level (1, 2).** If the file holds levels 3 and coarser and X-Plane
  reads level 1, nothing can intercept. So in `quality` only complete files
  qualify; in `smooth` a partial file must have its missing levels filled with
  upscaled imagery first.
- **Replacing a file X-Plane holds (7).**

None of this is designed further here. Stage S6 is a spike per mechanism, run
only if the gate in M0 opens.

## Alternatives considered

| Alternative | Why not |
|---|---|
| No virtual disk, real files only (OrthoStudio's model) | Cannot wait in flight (constraint 1), so no *full quality* mode, and a wrong prediction shows until the next scenery load. |
| Rewrite the whole file system in C or Rust | The decisions live in nearly 30,000 lines of Python that change every release. The hot case is one function. |
| Free-threaded Python (3.14t) | Would remove the wait for the GIL without native code. Every C extension in the worker would have to support it (numpy, zstandard, the ctypes libraries are fine; the rest is unknown), and per-call Python cost stays. Worth one run in M0 if it starts; not a plan. |
| Serve the file through memory mapping in Python | Saves the `pread`, not the callback or the GIL. |
| Do nothing; partial direct reads are enough | Possible. M0 decides. |

## Implementation plan

| # | Stage | What it buys | Size | Risk | Depends on |
|---|---|---|---|---|---|
| M0 | Measure a ready read | the decision for every stage below | S | low | B0 scenarios of the performance plan |
| S1 | `texture_wait_policy` | the user's choice between quality and smoothness | S | low | nothing |
| S2 | Native reads for complete textures | ready reads without Python or GIL | M | medium | M0 gate A |
| S3 | Partial entries, row checks, seasons in the native path | the same for far textures, first opens and season users | M | medium | S2 |
| S4 | Flights against OrthoStudio (deferred) | proof, or the next gap | M | low | S1 to S3 |
| S5 | Native open and getattr | opens without Python | M | medium | M0 gate C |
| S6 | Passthrough spikes | reads without the kernel round trip | L | high | M0 gate B |

S1 can be done at any time. S2 and S3 are one branch each. S5 and S6 are built
only if their gate opens.

M0 is done. It added four items ahead of S2 and closed S6: the order to follow
is the table "Order of work after M0" near the top of this file.

### M0. Measure a ready read

**Why.** The design assumes Python and the GIL are what a ready read costs. If
it is the kernel round trip instead, Part 1 buys little and Part 4 is the work.
If neither is large, this plan stops at S1.

**Changes.**

- `tools/bench/read_cost.py`: replays one recorded read pattern (opens, offsets,
  lengths, in order, as fast as they return and then on the trace's timeline)
  against three targets and reports per-read and per-texture latency (p50, p95,
  p99) and the CPU seconds of every process involved:
  1. the cache files themselves, on NTFS: the floor;
  2. the same files behind a passthrough file system written in C (WinFsp
     ships one as a sample): the kernel round trip alone;
  3. AutoOrtho's mount with every texture ready: the round trip plus Python.
- The same replay with the worker busy: a second scenario prefetching and
  building elsewhere at the same time. This shows the wait for the GIL.
- Counters in the mount, per call type: `getattr`, `open`, `read`, `release`,
  split into texture and passthrough (DSF, `.ter`, `.png`), with total time.
  They stay in after M0: they are cheap and every later gate reads them.
- The read trace's `read` event gains the tier that answered (direct, partial,
  live).

**Gates.**

- **A (build S2):** Python and the GIL account for at least half of the
  difference between targets 3 and 1, or the p95 of a ready read with the
  worker busy is above 5 ms.
- **B (run S6):** after subtracting what S2 removes, the round trip (target 2
  minus target 1) times the reads of a flight is more than 5 % of the worker's
  CPU, or adds more than 20 % to the time X-Plane takes to load a ready
  texture.
- **C (build S5):** ~~`open` plus `getattr` on textures take more than 5 % of
  the worker's CPU on `cruise-cached`~~. Replaced on 2026-11-25: like gate B's
  second criterion, it measured the wrong thing. An open costs X-Plane time,
  not the worker's CPU: 4,330 opens of a flight at the old 0.5 ms are about
  2 s, near 0.2 % of the 1,204 CPU seconds of the flight of 2026-11-22, so
  the gate could never open, while sixteen readers waited 3.7 to 4.5 ms per
  open for their turn. The new gate is read from a **recorded flight** with
  `AO_READ_TRACE` (`python -m autoortho.read_trace <file>`) and opens when
  both hold:
  1. at least half of the texture opens are ready (`ready_share` >= 0.5):
     a native open only helps those;
  2. the open is a large part of a ready texture's time at the flight's own
     concurrency: `ready_open_time_share` > 0.25, or the p95 of
     `ready_open_ms` > 2 ms.

  S5 also has a cost the old gate did not weigh: the open feeds load-ahead
  and the paging model and decides the zoom and the partial level, which a
  native open would have to defer to Python. Late notes mean later
  prediction, and prediction misses cost X-Plane far more than opens do.

**Done when** the three targets and both worker states are measured three times
each on an idle PC, and this file records the numbers and which gates opened.

### S1. `texture_wait_policy`

**Changes.**

- The setting in [aoconfig.py](../../autoortho/aoconfig.py) and the settings
  registry, with the table of Part 3 as its presets. It supplies
  `prefetch_quality_mode`, `band_max_wait_seconds`, `far_wait_seconds` and the
  strict deadline where the user has not set them.
- `wait_bands.wait_seconds` returns 0 for every band in flight under `smooth`.
- The UI: one choice of three with a sentence each on what the pilot will see.
  `docs/config.md`.
- Counters per policy: reads that waited, total wait, reads answered with a
  stand-in in level 0 or 1.

**To check while implementing.** How `strict_target` and the wait bands combine
today inside `Tile.read_dds_bytes`, and what a read is answered with when the
strict deadline passes.

**Tests.** A near read of an unbuilt texture, with downloads held back: under
`smooth` it returns in under 50 ms with upscaled imagery and the build is still
queued at the reader's priority; under `quality` it returns only when the build
ends, with exact bytes; under `balanced` as today. A hand-set
`band_max_wait_seconds` survives a policy change.

**Done when** on the `load` and `cruise-cold` replays `smooth` shows no
in-flight read above 100 ms, and `quality` shows no level-0 read answered with
lower-zoom imagery before the deadline.

### S2. Native reads for complete textures

**Changes.**

- `autoortho/aopipeline/aofast.c` and `.h`: the slot table, `aofast_read`,
  register, unregister, drain; `AoFast.py`, the ctypes wrapper. Added to the
  three Makefiles and to the packaging.
- [autoortho_fuse.py](../../autoortho/autoortho_fuse.py): the `FUSE` subclass
  that swaps `read`; `open` registers a complete `DiskTexture` with no season
  table and returns a tagged handle; `release` unregisters; a one-second drain
  feeds the counters and the paging model.
- In this stage a slot answers only what needs no check: levels 1 and coarser,
  and level-0 rows already verified. Everything else goes to tier 2, which
  marks rows verified in the slot as it checks them.
- `[streaming] native_direct_reads`.

**Risks.** A fault in C takes the worker down, and the mount with it. The code
keeps to fixed arrays, bounds-checks every offset against the slot's file size,
and holds no pointer into Python memory after register. Handle reuse: a slot
carries a generation number in the tagged handle, so a late read on a released
handle goes to Python and not to another texture's file.

**Tests.**

- Byte equality: for a set of cached textures (4096 and 8192 px, BC1 and BC3),
  every read of a recorded pattern returns the same bytes through tier 1 and
  tier 2.
- Concurrency: sixteen threads reading one slot while another releases it; no
  read returns another texture's bytes, none crashes.
- Fallbacks: a truncated file, a file deleted under the slot, a read past the
  end, an untagged handle. Each ends in tier 2's answer.
- The library absent: the mount starts and serves as today.
- An address-sanitizer build of the library run under the same tests on Linux.

**Done when** on `read_cost.py` a ready read through the mount is within 20 %
of the C passthrough file system (target 2) at p50 and p95, idle and with the
worker busy, and byte equality holds on every scenario.

### S3. Partial entries, row checks and seasons

**Changes.**

- Partial entries: the slot holds `finest_level` and the stand-in block; the
  incidental bytes of finer levels are tiled from it, as `_standin` does. A
  read that wants a finer level goes to Python, which switches the open to a
  live tile.
- Row checks: the choice made from M0's numbers (in the library, or off the
  read path).
- Seasons: the table passed at register, applied in the library; a season
  change replaces the table of every slot.

**Tests.** S2's byte-equality suite on partial entries and with a season table.
A corrupted level-0 row is still caught and the entry dropped. A season change
during a read returns a buffer coloured wholly by one table.

**Done when** on `cruise-cached` at least 95 % of the reads of direct textures
are answered by tier 1, with and without seasons.

### S4. Flights against OrthoStudio

**Deferred (2026-11-25), outside this plan's definition of done.** No
decision in this plan waits on it: turning `native_direct_reads` on by
default needs CI's libraries and one AutoOrtho flight with it on; S5 needs
gate C, read from that flight's trace; the wait policy needs the replays
and a flight under `balanced` and `smooth`. S4 measures the wider goal,
AutoOrtho against OrthoStudio XP, and is kept as its own comparison.

The comparison the performance plan's definition of done asks for (items 1 to
4), flown with OrthoStudio's test pilot on the same scripted paths, the two
programs interleaved in one session, cold and warm. AutoOrtho is flown under
`balanced` and under `smooth`. Reported: each program's CPU and memory, the
rest of the machine's CPU (antivirus, kernel), disk writes, X-Plane's loading
times and long frames, and sharpness of the test pilot's screenshots.

**Done when** the table is in this file.

### S5. Native open and getattr (gate C)

As Part 2 describes. The difficult part is keeping the ready table true: every
event that makes a cache entry unservable (eviction, staleness, a zoom or
season setting, a live tile taking the texture) must clear its row before the
change takes effect. The table is therefore cleared in the same critical
section that pins and unpins entries in `DynamicDDSCache`.

### S6. Passthrough spikes (gate B)

One spike of two to three days per mechanism, each answering the "not
verified" column of Part 4 on a real mount with X-Plane reading. A spike ends
with a go or no-go and a measured read cost, not with shipped code.

## Risks across the plan

- **The gain is small.** The rough count above says the CPU saved may be 1 to
  3 % of the worker. M0 exists to find that out for two days of work and not
  after three weeks.
- **Two paths to keep equal.** Tier 1 and tier 2 must return the same bytes
  for good. The byte-equality suite runs in CI on every platform that builds
  the library.
- **Dokan and WinFsp differ** in how they call `read` (threads, buffer
  sizes). S2's tests run on both.
- **Fewer samples for the paging model, later.** Tier 1 keeps them (drained
  counters). Part 4 would lose them; that is one of the reasons it is last.

## Definition of done

1. `texture_wait_policy` exists with three values, is in the UI and the docs,
   and each value does what Part 3's table says on the replays.
2. With `native_direct_reads` on, a ready read costs within 20 % of a C
   passthrough file system, idle and with the worker busy.
3. At least 95 % of the reads of direct textures are answered without Python
   on `cruise-cached`.
4. No read returns bytes that differ from today's direct path.
5. With the library missing or the setting off, behaviour and tests are as
   before this plan.
6. ~~The flights of S4 are recorded here, AutoOrtho under `balanced` and
   `smooth`.~~ S4 is deferred (see S4). Instead: one AutoOrtho flight with
   `AO_READ_TRACE` and `native_direct_reads` on, partly under `smooth`, ends
   with no crash and no native fallback beyond the expected ones, and its
   trace decides gate C.
7. Gates B and C are recorded as open or closed, with their numbers.

## Open questions

- What a ready read costs today and where (M0).
- How X-Plane's reads arrive through Dokan and WinFsp: the same 64 KB pieces,
  or merged or split.
- Whether the first-read row check should stay on the read path at all.
- Whether `smooth` should also shorten the loading-screen wait for users who
  prefer a fast start to a sharp one. Left at 60 s here.
- The "not verified" column of Part 4.

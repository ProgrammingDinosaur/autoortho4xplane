# Ready share plan: keep what was flown, and make an open cost nothing

Date: 2026-11-25. **Status (2026-11-26): everything that can be built
and measured without X-Plane is built, replayed and committed. What is
left waits for flights; the list is "What waits for flights" at the
end.** K1, K3, K4, the no-flight parts of K0 and the side fixes are in;
on a replay of flight 3 a second pass serves 96 % of the textures from
the cache file (0.6 % in the flight). K2 has its likely fix in (R13)
and needs a flight to confirm it; K5 and K6 are decided on a flight.
Findings R7 to R13 and "Implementation status" at the end record what
changed from the plan as first written.

Follows [hybrid-read-path-plan.md](hybrid-read-path-plan.md), which is
closed: a texture that is ready is now served at close to the speed of a
plain file. Comes before the building stages of
[prediction-plan.md](prediction-plan.md): that plan decides what to build
ahead; this one makes sure what was already built is still there the next
time, and that asking for it is cheap.

## The idea in short

Three flights over the same route on 2026-11-25 showed the fast read path
working and almost unused: 16 of 18,984 opens were served from the cache.
The reads are no longer the problem. Three things in front of them are:

| | Today | This plan |
|---|---|---|
| A route flown an hour ago | 89 % of its opens find nothing in the cache: the cache thinks it is full and evicts what the flight just stored | the cache counts the disk space it really uses, and holds whole routes |
| An open that reads nothing (85 % of all opens) | 5 to 7 ms of Python each, 2 to 4 minutes of X-Plane's loader threads per flight | well under a millisecond |
| A texture on the loading screen, or one a live read built | built again, though its levels are or were at hand | served from the cache file |

## What the flights showed

Central Mexico, max zoom 17 (8,192 px textures), WinFsp, read traces in
`D:\ao-traces\flight1..3.trace`. All 2,245 textures of flight 3 were also
opened in flight 1.

| | Flight 1 | Flight 2 | Flight 3 |
|---|---|---|---|
| Traced minutes | 26 | 25 | 7 |
| Texture opens | 31,326 | 48,316 | 18,984 |
| Different textures | 3,583 | 3,562 | 2,245 |
| Opens with at least one read | 4,543 (15 %) | 7,057 (15 %) | 2,578 (14 %) |
| Opens that read nothing | 26,783 | 41,259 | 16,406 |
| Time in opens that read nothing | 121 s | 254 s | 112 s |
| Opens served from the cache file | 0 | 4 | 16 |
| Cache entries evicted during the flight | 3,164 | 3,520 | 4,197 |
| Worker had the aircraft position | no | no | yes |

Why an open was not served from the cache in flight 3
(`dds_direct_skip_*`): no entry 16,812; partial entry but no level named
(loading screen) 645; live tile already open 360; partial entry too coarse
99.

## Findings

Each names its evidence. R1 and R2 are new; R3 and R4 are F12 and F11 of
the prediction plan, measured here on a real flight.

**R1. The cache budget counts sizes that are not on disk.** A partial
entry is a sparse file: its nominal size is the whole texture (44.7 MB at
8,192 px), but only the coarse levels are written.
`DynamicDDSCache._entry_disk_size` and
`DiskBudgetManager._scan_dir_size` both add up `st_size`. After flight 3
the cache held 581 entries: 22.6 GB by that count, against a DDS budget of
24 GB (80 % of `file_cache_size` = 30), and **1.5 GB on disk**. The 462
partial ZL17 entries alone count as 19.25 GB and use 0.05 GB. So the cache
is always "full", and every store evicts about 60 entries to "free"
2.5 GB that were never used: 4,197 entries in the seven traced minutes of
flight 3, more than the flight has textures. A route cannot stay.

**R2. 85 % of texture opens read nothing, and each costs 5 to 7 ms.**
X-Plane (or Windows on its behalf) opens every texture about seven times
without reading, each held about 1 ms, beside the one open that reads.
Each of them runs AutoOrtho's whole open: the load-ahead note, the cache
lookup, and for a texture not in the cache `TileCacher._open_tile`, which
creates a live tile, all in Python with X-Plane's other loader threads
taking turns. Median 4.6 ms in flight 1 and 6.7 ms in flight 3; the opens
that do read took 0.4 and 4.8 ms. Per texture that is about 40 ms of
waiting before its first byte: as much as all the reads of a ready
level-0 texture took through Python, with eight readers, before the
hybrid plan (41.8 ms). Not known yet: what makes these
opens (an attribute query that WinFsp turns into open and release, or
X-Plane's own probing), and whether Linux and macOS see them.

This is what gate C of the hybrid plan was reaching for, measured on the
wrong opens: it timed the opens of ready textures and never looked at the
ones that read nothing.

**R3 (prediction F12). Partial entries are not served on the loading
screen.** A partial entry is served only at the level the paging model
names, and with no camera it names none. In flight 3, 2,142 of the 2,578
opens that read happened in the first 75 s, before the first position:
the loading screen is where most textures are read, and there every
partial entry is refused (645 opens, and far more once R1 lets entries
stay).

**R4 (prediction F11). Levels a live read builds are often not kept.** On
the synthetic replay the warm pass found entries starting at level 4
where the cold pass had read level 1 (106 of 239 opens). 99 opens in
flight 3. Likely cause: `Tile._try_incremental_dds_store` keeps only
levels whose whole buffer was built, and level-1 reads are answered by
row-range builds that never complete a level. To confirm before fixing.

**R5. The ready share was measured over the wrong opens.**
`read_trace.summarize` divides ready opens by all opens. With 85 % of
opens reading nothing, a flight where every texture came from the cache
would still show about 15 %. The share must be over opens that read, and
given for the loading screen and the flight apart.

**R6. A complete texture at zoom 17 is 44.7 MB.** The 24 GB budget holds
about 550 of them, a quarter of this route. That is real disk space, not
an accounting error, and R1 does not change it. X-Plane reads most
textures only at coarse levels, so a route is mostly partial entries of
about 0.1 MB; the complete ones are the textures under the aircraft. What
to evict first when the real budget is reached (level 0 of far-away
textures before any coarse level) is item 9 of the prediction plan; here
only the count is fixed.

### Added by the review of 2026-11-26

**R7. An open that reads nothing waits; it does not work.** The same
traces, with the opens that read nothing split by how many other opens
were in flight when each began (median time):

| | Flight 1 (worker idle) | Flight 3 (worker building) |
|---|---|---|
| Alone, tile already in memory | 0.24 ms (6,275) | 3.7 ms (1,266) |
| Alone, creates the tile | 1.0 ms (785) | 6.0 ms (101) |
| 2 to 4 opens in flight | 2.4 ms | 6.4 ms |
| 5 to 16 opens in flight | 6.1 to 6.7 ms | 6.9 ms |

The open's own work is a quarter of a millisecond, one millisecond when
it creates the tile. 87 % of the time is in opens of a texture whose tile
already exists (104 of 121 s in flight 1, 98 of 112 s in flight 3), so
the tile's constructor is not the cost. The rest is waiting for a turn:
the open is a Python callback, and with sixteen file system threads, or
with one busy builder thread, each waits about one interpreter switch
interval (5 ms by default; nothing in the repository sets it). That it is
the interpreter lock and not `tc_lock` is an inference from these
numbers: flight 3's opens wait 3.7 ms with no other open in flight. To
confirm (K0).

What follows: removing work from the open (both designs of K2 as first
written) cannot bring it under 0.5 ms while the open is still a Python
callback. See K2.

*Corrected the same day by the replays ("Implementation status", K2):
the opens do wait rather than work, but not for the worker's
interpreter. The same opens cost 0.02 ms in a replay with the builders
busy, and a shorter switch interval changes nothing. What is left is
R13.*

In flight 3, 14,637 of the 16,412 opens that read nothing (102 of 112 s)
came before the first position: this is loading-screen time.

**R8. The cost in R2 is a lower bound.** Only `open` is timed. `release`
is recorded without a duration and `getattr` is not recorded at all, and
each is a Python callback that waits the same way; the file system calls
`getattr` at least once for every open. The 121 to 254 s per flight count
one of three or more callbacks.

**R9. The trace cannot tell which open a read belongs to.**
`read_trace.summarize` pairs by path: a read counts for the last open of
its path, and the first release of that path ends the pairing. Several
opens of one path overlap in the traces (three within 6 ms at the start
of flight 3), and every live open returns handle 0, so `release` cannot
say which open it ends. The 85 % is therefore approximate (it cannot be
far off: 8 to 10 opens per texture against about 1.2 that read). The
trace has no process id either, so X-Plane's opens cannot be told from an
antivirus's or the indexer's.

**R10. Eviction by real size would delete what costs nothing.**
`evict_lru` deletes entries oldest first until it has freed enough bytes.
Once sizes are real (K1) a partial entry frees about 0.1 MB, so a pass
that must free 2.4 GB (10 % of the budget) deletes every partial entry
older than about the 54th-oldest complete texture: thousands of entries,
for nothing. The route would be lost again every time the cache really
fills, which at zoom 17 is after about 550 complete textures.

**R11. Every mount worker keeps its own count.** Each worker builds its
own `DynamicDDSCache` and `DiskBudgetManager` over the same folder
(`getortho.start_predictive_dds`), counts what it found at start and then
only what it stores itself, each against the whole budget. With nominal
sizes this was hidden by constant eviction. With real sizes, N workers
can together hold more than the budget, and one worker evicts by an order
that knows nothing of what the others read. On Windows and Linux the
parent serves several mounts from one shared worker ("Serving N mounts
from one shared worker"), and this PC has one mount, so this is a
question for macOS, where each mount has its own worker. Not checked
yet: whether anything scans again.

**R12. The cache will hold a hundred times more entries.** 581 today;
about 3,500 per route once they stay, three files each. The start scan
reads every `.ddm` in every worker, K1 adds one system call per sparse
file, and Windows Defender looks at each file. Nothing here has been
measured above a few hundred entries.

**R13. Every open with no cache entry called the parent process.**
`DynamicDDSCache._bump` counted with `inc_stat`, which in a mount worker
is a call to the parent's statistics store, and since `447c5a59` all of
a worker's calls share one connection, one at a time. `open_direct`
counts why it refused on every open: 16,812 such calls in flight 3, each
made from a file system thread that then waits for its turn again. This
is at least part of why an open alone took 3.7 ms in flight 3 and 0.24 ms
in flight 1, where the connection was lost and counts stayed local. Fixed
with K1 (the cache now counts through getortho's batched counter). Not
measured yet how much of R7 it explains; other calls to the store on the
open path, if any, are to be looked for with K0's timings.

**Smaller.** *Corrected 2026-11-26:* the default `dds_budget_pct` is 80,
as on the PC of these flights (the config template in `aoconfig.py`). The
40 first reported here was only the literal `getortho` falls back to when
the setting is missing, which it never is; that literal is now 80 too.
K1's "done when" does not
need two flights at first: replaying `flight3.trace` twice against a
running mount shows whether entries stay.

**Fixed on the way, for the record.** The worker lost the parent's
statistics store, and with it the aircraft position, in every flight
(`447c5a59`). Native builds were stored 16 bytes short and never served
(`fff86acd`); the 45 complete entries now in the cache are whole.
Entries stored short before that fix are refused until evicted; no repair
is planned.

## The plan

| # | Stage | What it buys | Size | Risk | Depends on |
|---|---|---|---|---|---|
| K0 | Measure the right things | the numbers every stage below is judged by | S | low | nothing |
| K1 | The budget counts disk space really used | routes stay in the cache | S | low | K0 |
| K2 | An open that reads nothing costs nothing | 2 to 4 minutes of loader time per flight | M | medium | K0 |
| K3 | Partial entries on the loading screen | the loading screen served from the cache | S | low | K1 |
| K4 | Keep the levels live reads build | second passes need no build | S to M | medium | K1 |
| K5 | Native reads on by default | the hybrid plan's gain for every user | S | low | CI libraries, K1 |
| K6 | Decide the native open (S5) | closes or opens the last stage of the hybrid plan | - | - | K1 to K3 |

K1 first: without it nothing else can be measured on a second flight. K2
is independent of the cache and can run beside it. K3 and K4 only show
once K1 lets entries stay.

### K0. Measure the right things

- `read_trace.summarize`: opens with reads, opens that read nothing and
  their total and median time; the ready share over opens with reads, for
  the loading screen and the flight apart (R5).
- The read trace's `open` event gains the open flags the file system
  passes, to tell an attribute probe from a real open if the driver lets
  them be told apart (R2). One short session with WinFsp's own debug log
  to see what X-Plane asks for.
- STATS gauges of the DDS cache: entries, counted bytes, bytes on disk,
  entries evicted. Today eviction is only in DEBUG lines.
- A profile of `open` for a texture not in the cache, with sixteen
  threads opening at once (py-spy `--gil`, and the in-process timing used
  for N3), to see where the 5 ms go: the tile's constructor, `tc_lock`,
  or waiting for a turn. *Answered in part by R7: it is waiting. What is
  left is to confirm what for.*
- `release` and `getattr` of textures timed in the trace like `open`
  (R8), and every trace event of an open carrying an id of that open and
  the process id of the caller (R9). Live opens get a handle of their own
  for this, which K2 needs anyway.
- **The switch-interval experiment** (R7): the same replay with
  `sys.setswitchinterval` at 5 ms (today), 1 ms and 0.5 ms in the mount
  worker, read from an environment variable so no code path changes. If
  the opens that read nothing fall with it, the wait is the interpreter
  lock; the run also shows what a shorter interval costs the builders
  (CPU, build time).

**Done when** the three flight traces are summarized with the new
numbers in this file, the profile names the parts of the 5 ms, and the
switch-interval runs are in `tools/bench/runs`.

### K1. The budget counts disk space really used

- One helper, `allocated_size(path, stat)`: on Windows
  `GetCompressedFileSizeW`, called only for files whose attributes carry
  `FILE_ATTRIBUTE_SPARSE_FILE` (the `stat` already has them, so other
  files cost no extra call); on Linux and macOS `st_blocks * 512`; where
  that is unavailable, `st_size`.
- Used by `DynamicDDSCache._entry_disk_size` (and so `_current_size`, the
  LRU eviction and the startup scan) and by
  `DiskBudgetManager._scan_dir_size`. An entry is measured again when
  `store_incremental` adds levels to it, since it grows.
- Eviction frees what the entry really used; the log line and the Home
  and Settings figures show disk space, as the user's file manager does.
- On a volume without sparse files (FAT32, exFAT) the two sizes are the
  same and nothing changes.
- **Eviction keeps what frees nothing (R10).** A pass first deletes only
  entries that use at least 1 MB, oldest first, and stops when it has
  freed enough. Smaller entries are deleted, oldest first, only if that
  was not enough. A partial entry of a far texture then outlives the
  complete textures around it, which is the order item 9 of the
  prediction plan wants; the rest of item 9 (drop level 0 and keep the
  coarse levels) stays there.
- **More than one worker (R11).** To settle before the second flight:
  count the workers of a usual setup. If more than one, the smallest fix
  is that a worker scans the folder again before it evicts (the scan is
  the truth; its own count is a guess), and evicts by file time rather
  than by its own order.
- **Many entries (R12).** Time the start scan on a cache of 50,000
  entries (made by a script) before the stage is called done; if it
  takes more than a few seconds, the count moves into one index file
  written at shutdown.

**To check while implementing.** What `_extend_sparse` leaves allocated on
NTFS after rows are written in the middle of a file (allocation is in
64 KB units); that the JPEG side of the budget has no sparse files; that
a cache which was "full" at 24 GB and is found to use 1.5 GB does not
trigger anything at start.

**Tests.** A sparse entry of nominal 44 MB with 100 KB written counts as
about 100 KB on every platform that has sparse files; storing 1,000 such
entries under a 1 GB budget evicts nothing; a complete entry counts in
full; the budget manager's scan agrees with the cache's own count.

**Done when** the route is flown twice in a row and, on the second
flight, fewer than 10 % of the opens that read find no entry (89 % of all
opens today), with evictions only when the disk space used reaches the
budget.

### K2. An open that reads nothing costs nothing

**Changed by R7.** The open's work is not what costs; its turn is. The
two designs below, as first written, still answer every open from
Python, and neither can reach the target under load. The choice is now
among three, to be made on K0's switch-interval runs:

- **A shorter switch interval** in the mount worker. One line. Enough on
  its own only if the runs show the opens falling near 0.5 ms without
  the builders paying for it.
- **Answer the empty callbacks in native code.** `aofast.c` intercepts
  only `read` today. It would also answer a texture's `open`, `release`
  and `getattr` without Python: `open` hands out a handle and notes the
  path; the first `read` calls Python once to do the real open ("open
  late", below, with the lateness in C); a `release` without a read ends
  there. `getattr` needs the texture's size in native code: a table
  Python fills, or the size rule itself. This is stage S5 of the hybrid
  plan in a smaller shape, so K6 is decided here and not after.
- **Both**, if the first helps the opens that read and the builders
  tolerate it.

The two designs as first written remain what the Python side does at the
first read:

1. **Open late.** `open` answers at once with a handle and does the real
   work (cache lookup, native registration, or creating the live tile) at
   the first read. An open that is released without a read never did
   anything. The load-ahead note and the "X-Plane is loading here" focus
   move to the first read too, which is when X-Plane really loads the
   texture.
2. **Open cheaply.** Keep the order of work and remove what the profile
   finds: for example a tile created at open but its chunk grid and
   buffers only at the first read.

**To check before choosing.** What `getattr` must answer between such an
open and the first read: today the open records the texture's real size
(`_set_dds_size_cached`), and X-Plane sizes its reads from `getattr`. A
late open must not make `getattr` answer a different size than the read
will serve; under dynamic or airport zoom the estimate and the real size
differ (see N1 of the hybrid plan). Whether WinFsp calls `getattr` after
every open (it fills the file information of the create from it).
Whether the first read of a late open may run on a different thread than
the open (it may; the handle table already allows it).

**Risks.** The open is where one texture is kept to one source (direct
or live) and where a live tile's reference is counted; doing that at the
first read must keep `release` balanced for opens that never read. Tests
for every order: open and release; open, read, release; two opens of one
texture, one reading.

**Done when** in a recorded flight an open that reads nothing takes
under 0.5 ms at the median and the time in such opens is under 15 s per
25 minutes of flight (121 to 254 s today), with byte equality and the
fallback tests unchanged.

### K3. Partial entries on the loading screen (R3)

With no camera, `open_direct` is given no level and refuses every partial
entry. Serve it from its finest stored level instead; a read of a finer
level already hands that open to a live tile (`_switch_direct_to_live`),
which costs one extra open in the worst case. Such fallbacks must not
count against `_PartialDirectStats` (the margin that makes in-flight
partial opens ask for one level finer). Once the start position is known
(prediction plan, item 8) the level can come from the distance to it;
not in this plan.

**Done when** `dds_direct_skip_partial_no_level` is near zero on a second
flight and fewer than one in ten of those opens falls back to a live
tile.

### K4. Keep the levels live reads build (R4)

First confirm the cause on the replay: which levels a cold pass builds by
row ranges, and what `_try_incremental_dds_store` then stores. Then
persist a level when its last row is built, as a whole level is today.
Levels 1 and coarser only; level 0 keeps its own path with row
manifests.

**Done when** on a second pass over a route fewer than 5 % of the opens
that read are refused as "partial entry too coarse" (44 % on the replay,
4 % in flight 3, where few entries survived to be refused).

### K5. Native reads on by default

Push the branch so CI rebuilds the libraries on the three platforms with
`aofast.c` (before pulling CI's commit, discard the local Windows build:
`git checkout -- autoortho/aopipeline/lib/windows/aopipeline.dll`). One
flight on CI's Windows library after K1, when the cache has something to
serve. The `read_cost` run on Linux and on macOS, recorded as N8 asked.
Then `native_direct_reads = True` in `streaming_options.DEFAULTS` and the
config template, with the setting kept as the way to turn it off.

**Done when** a flight with a ready share above 50 % ends with
`dds_native_reads` far above `dds_native_deferred`, no error, on CI's
library. (The Linux and macOS runs were dropped on 2026-11-26: those
platforms are beta, see "Decisions".)

### K6. Decide the native open (S5 of the hybrid plan)

After K1 to K3, from one recorded flight. Build S5 only if both hold:

1. at least half of the opens that read are ready (R5's share, not the
   old one);
2. the open is still a large part of a ready texture's time:
   `ready_open_time_share` above 0.25, or the p95 of `ready_open_ms`
   above 2 ms.

If K2 opens late, S5 shrinks to the lookup at the first read and may not
be worth a stage at all. Record the decision here either way.

*After R7:* if K2 answers `open`, `release` and `getattr` in native code,
that is S5, and this stage only records it. The two conditions above then
judge what is left: whether the first read's call into Python is still a
large part of a ready texture's time.

## Found on the way, outside this plan

Small, separate fixes; none blocks a stage above.

- **The window fetches the scenery list on its main thread.**
  `_render_scenery_list` calls `downloader.parse`, which downloads from
  GitHub: 297 requests during flight 1, and a read timeout that went
  uncaught and wrote a crash report (`crash_20261125_164614.log`). Move
  it to a worker thread, cache the result for the session, catch the
  timeout.
- **Loading the config saves it.** `AOConfig.__init__` calls `save()` in
  every process that is not a mount worker, so running the tests or any
  tool that imports `aoconfig` rewrites the user's `~/.autoortho` (same
  values). Save only when a default was added or a value migrated.
- **`python -m autoortho` from the repository root fails**
  (`import flighttrack` in `autoortho.main`), while `python autoortho`
  works and the mount worker is started with `-m autoortho`. One import
  style for both.
- **Watch for `Shared stats` warnings** in flight logs: the connection
  fix (`447c5a59`) should leave none.

## Not in this plan

- What to build ahead and when: the prediction plan (its P0 should be
  run again after K1, since its baseline was taken with a cache that
  could not hold a route).
- What to evict first once the real budget is reached (R6): prediction
  plan, item 9.
- The comparison flights against OrthoStudio XP (S4 of the hybrid plan):
  deferred, worth doing after K1 to K3, when a second flight is served
  from the cache.

## Risks across the plan

- **A larger cache in real terms.** After K1 the cache will fill to its
  true budget for the first time. Users who sized `file_cache_size`
  against what they saw used on disk will see it grow to the number they
  set. The release note should say so.
- **K2 touches the one place every texture passes.** It ships behind a
  setting for one release, like `native_direct_reads`.
- **Numbers from one route and one PC.** K0's summaries and each
  stage's "done when" are read on at least two routes, one of them at
  zoom 16, before a stage is called done.

## Definition of done

1. On a second flight over a route, at least 90 % of the opens that read
   find a cache entry, and at least 60 % are served from the cache file
   (0.6 % today). The rest is the prediction plan's.
2. Evictions happen only when the disk space really used reaches the
   budget.
3. An open that reads nothing costs under 0.5 ms at the median; the time
   in such opens is under 15 s per 25 minutes of flight.
4. No read returns bytes that differ from today's.
5. `native_direct_reads` is on by default on Windows, on libraries built
   by CI (Linux and macOS: beta, see "Decisions").
6. The native open (S5) is decided, with its numbers.

## Open questions

- What makes the opens that read nothing, and do Linux and macOS have
  them (K0)?
- Does anything rely on a live tile existing between `open` and the first
  read (the tile radar on Home, the held-textures count the governor
  reads as graphics memory)?
- Should the budget's default grow now that it will be real? 24 GB holds
  about 550 complete zoom-17 textures. *Decided, see below.*
- Is the wait in R7 the interpreter lock (K0's switch-interval runs)?

## Decisions (2026-11-26)

- **Linux and macOS are beta for this plan.** The Windows results decide
  each stage. The recorded `read_cost` runs on Linux and macOS are no
  longer part of K5 or of the definition of done, and the question of
  one count per mount worker (R11, macOS only) is a known limit there,
  not a blocker. Whether `native_direct_reads` is on by default on those
  platforms is settled at K5, with the flight on Windows in hand; the
  release note says the fast read path is not measured there.
- **The texture share of the cache stays at 80 %.** It was asked to pick
  the value best for performance and stability. The share cannot go up:
  the imagery side (20 %, 6 GB of the default 30) held 4.6 GB after the
  three flights of one route, so at 90 % (3 GB) the imagery a flight is
  building from would be evicted during that flight and downloaded again.
  It should not go down: a built texture is what a read is served from
  with no work, and the plan's whole gain is in keeping those. To
  revisit with the first flights after K1, when the texture side fills
  for real: if evictions of complete textures start within one flight at
  zoom 17, the answer is a larger `file_cache_size`, not another split.

## Implementation status

Updated as stages land. Nothing below is committed yet.

**2026-11-26: K1 built, not yet flown or replayed.**

- `aopipeline/disk_usage.py`, `allocated_size(path, stat)`: as planned,
  and never more than the file's size, so files that are not sparse count
  exactly as before. Used by `DynamicDDSCache._entry_disk_size`,
  `DiskBudgetManager._scan_dir_size` and the cache inventory the window
  shows (`cache_inventory._scan_dds`).
- **Found while implementing: NTFS reports late.** Right after a write
  to a sparse file, `GetCompressedFileSizeW` says 0, and the real figure
  only once the data has left the system's write cache: more than 8 s in
  one measurement, 6 s in another, at once after a flush (0.8 ms on this
  SSD). The store path does not flush. Instead every stored entry is
  measured again for a minute (`remeasure_recent`, called from the tile
  cacher's housekeeping loop every 1 to 10 s), and the count, the budget
  manager and the eviction check follow. The start scan sees the truth in
  any case. If a flight shows the count lagging more than that, the
  alternative is a flush at each store.
- **Eviction takes small entries last (R10):** `evict_lru` deletes
  entries of 1 MB or more first, oldest first, and smaller ones only if
  that was not enough.
- **Counts are batched (R13).**
- **K0, the cache part:** gauges `dds_cache_entries:<pid>` and
  `dds_cache_bytes:<pid>` per worker, and the counter
  `dds_cache_evicted`.
- **On this PC's cache** (read only, nothing changed in it): 581
  textures, 536 of them sparse; 22.57 GB by file size, **1.55 GB by the
  new count**. The budget manager's scan of its 1,163 files took 77 ms,
  which at 50,000 entries would be several seconds (R12, still to
  measure).
- Tests: `test_dynamic_dds_cache.py` (ten new), all green on Windows
  (NTFS), with the other cache, inventory, direct-read and native-read
  test files.

- **R12 measured** on 20,000 synthetic partial entries (8,192 px, coarse
  levels only, made and deleted in a scratch folder on the same SSD):
  `scan_existing` took **62 s**, 3.1 ms an entry; the budget manager's
  scan of the same 40,000 files took 0.7 s, so the new size call is not
  the cost: reading each `.ddm` is (the files were minutes old, so an
  antivirus's first look is in that figure; an older cache may read
  faster). The scan runs in the background and reads are served without
  it, but at 50,000 entries it is minutes of disk work at every start
  with the count wrong meanwhile. An eviction pass over the 20,000
  entries took 0.03 s. Each partial entry used about 0.12 MB: NTFS
  allocates a sparse file in 64 KB pieces, so that is the floor however
  little is written.

- **R12 fixed without an index.** The scan now takes an entry's key,
  size and age from the directory listing and its path: the file name
  gives row, column and target zoom, the folder the map type and the
  1 degree cell, and only one zoom of the tile's name puts that row and
  column in that cell. The `.ddm` is read only for entries with row
  records or whose path does not settle the key. The same 20,000
  entries: **0.9 s** (was 62 s). No file to keep in step with the cache.
  A `.ddm` that cannot be parsed is no longer deleted by the scan; the
  open that finds it unusable stores over it.

Not done in K1: the check with more than one worker (R11, macOS: beta),
the replay of flight 3 twice, the flight.
Not checked: `st_blocks` on macOS (APFS) and on ZFS, which may report
late as NTFS does; the re-measure covers that case too.

**2026-11-26: K0, the parts that need no flight.**

- The trace times `release` and `getattr` of textures and records the
  caller's process id on opens and getattrs (R8, R9). Not done: an id per
  open, which needs live opens to have their own handle (with K2).
- `read_trace.summarize` gives the opens with and without reads, the
  ready share over the opens that read, loading screen and flight apart,
  and the cost of empty opens, getattrs and releases (R5):

  | | Flight 1 | Flight 2 | Flight 3 |
  |---|---|---|---|
  | Opens that read | 4,541 | 6,705 | 2,588 |
  | Opens that read nothing | 26,785 | 41,611 | 16,396 |
  | Their time, median | 121 s, 4.6 ms | 256 s, 5.7 ms | 112 s, 6.7 ms |
  | Ready share of the opens that read | 0 | 0.06 % | 0.62 % |
  | The same, loading screen / flight | no positions | 0 / 0.14 % | 0 / 3.6 % |

  (A few opens differ from the first table: an open served by native
  reads now counts as one that read.)
- `AO_SWITCH_INTERVAL_MS` in the mount worker's environment sets the
  interpreter's switch interval, for the experiment of R7. Off unless
  set.

K0 still open: the switch-interval runs, a trace with the new timings
(any flight or replay from now on has them), the id per open.

**2026-11-26: K3 built.** With no aircraft position a partial entry is
served from the levels it holds (`open_direct(any_level=True)`); a read of
a finer level moves that open to a live tile as before. Counted as
`dds_direct_loading_opens` and `dds_direct_loading_fallbacks`, and kept
out of `_PartialDirectStats`. Its "done when" is read on a flight.

**2026-11-26: K4, cause confirmed in the code and fixed.** A read of
level 1 or coarser that is shorter than the level (X-Plane reads in
pieces) is built by `Tile.get_bytes` a few rows at a time into a sparse
buffer. When the rows make the whole level it is promoted to a dense,
retrieved level, and then only `_maybe_persist_complete_dds` ran, which
stores complete textures. `_try_incremental_dds_store`, the only thing
that keeps single levels, was called from `get_mipmap` and from the
derive-from-level-0 path alone. So a level read in pieces was never kept.
It is now stored when its last row lands (`partial_levels_kept`). The
store runs on the read that completed the level: one write of the level
(8 MB for level 1 of an 8,192 px texture), once per texture and level.

**2026-11-26: flight 3 replayed, cold and warm.** The whole app from
this checkout (parent, one worker, brokers, CI's library), an isolated
home with a copy of the PC's config and an empty scratch cache, Bing,
the fake X-Plane feeding the trace's positions; `flight3.trace` (7 min,
18,984 opens of 2,140 textures that were read, 12,834 reads) at its own
pace. Loading-screen behaviour for the first 75 s, as in the flight.

| Pass | Cache before | Textures served from the cache file alone | Reads p50 / p95 / max | CPU | Entries after, on disk | Evicted |
|---|---|---|---|---|---|---|
| cold | empty | 0 % | 8.2 / 152 ms / 39 s | 236 s | 294, 1.3 GB | 0 |
| warm 1 | imagery only | 8.4 % | 5.3 / 43 / 472 ms | 156 s | 2,265, 3.5 GB | 0 |
| **warm 2** | the route | **95.8 %** (2,050 of 2,140; 3.6 % partly; 0.6 % live) | **0.05 / 1.7 / 143 ms** | **37 s** | 2,270, 3.5 GB | 0 |

- **K1 holds.** The 2,270 entries are 81.7 GB by file size and 3.5 GB
  on disk. By the old count they are 3.4 times the 24 GB budget; by the
  new one nothing was evicted in any pass.
- **A cold pass keeps little, rightly.** With no imagery on disk the
  reads of the cold pass came before the downloads and were answered
  with stand-ins (`reads_wanted_standin` 2,396 against 216 real), which
  are never stored. The first pass with imagery at hand (warm 1) built
  the real levels and stored them; the pass after it is the one served
  from the cache. In a flight the prefetch does the work of warm 1.
- **K4 was most of "no entry".** Warm 1 kept 2,227 levels that reads
  had completed in pieces (`partial_levels_kept`); before the fix those
  2,227 would have been built and dropped. After warm 1, 1,941 of the
  1,954 textures whose level could be told held the level they were read
  at. In warm 2, 201 opens were refused as too coarse (1.1 % of opens;
  the stage asks for under 5 %).
- **K3.** Warm 2: 16,420 partial entries opened on the loading screen
  with no level named, none fell back to a live tile;
  `dds_direct_skip_partial_no_level` 28 (755 in the cold pass).
- **Native reads** (K5's numbers, on the replay): 18,188 of 18,984
  opens registered natively, `dds_native_reads` 11,218 against
  `dds_native_deferred` 910, of which 863 are first reads of rows checked
  once per process. No error. 87 % of all reads never reached Python.
- **The definition of done, on the replay:** point 1 asks for 90 % of
  the opens that read to find an entry and 60 % served from the cache
  file; warm 2 has 99 % and 96 % (per texture). Point 2 holds. These are
  to be confirmed on a flight, where X-Plane sets the pace and the
  prefetch replaces warm 1.
- `read_trace.summarize` now also answers per texture
  (`ready_texture_share`). With native reads on, an open answered
  natively counts as one that read, so X-Plane's probes of a ready
  texture lift the per-open share (99.5 % in warm 2); the per-texture
  number is the one to quote.

**2026-11-26: K2 and the experiments of K0. The replay does not show
the cost; a flight has to.** Four more passes over the same trace (all
in `tools/bench/runs/replay-flight3-20261126.json`):

| Pass | Opens that read nothing, median | Open of a ready texture, p50 / p95 | getattr / release, median |
|---|---|---|---|
| cold | 0.022 ms (2.0 s in all) | | 0.004 / 0.006 ms |
| cold, counters not batched (before the R13 fix) | 0.022 ms (4.5 s in all) | | 0.004 / 0.006 ms |
| warm 2 | 0.023 ms | 0.23 / 0.57 ms | 0.004 / 0.041 ms |
| warm, switch interval 1 ms | 0.023 ms | 0.23 / 0.55 ms | 0.004 / 0.041 ms |
| warm, every CPU busy (31.5 cores of other work) | 0.25 ms | 0.36 / 1.76 ms | 0.005 / 0.067 ms |
| the same, switch interval 1 ms | 0.52 ms | 0.37 / 1.62 ms | 0.005 / 0.071 ms |

In flight 3 the same opens took 6.7 ms. What the passes say:

- **R7's inference was wrong.** The worker's interpreter is not what the
  opens wait for: with the builders at work (cold pass) an open that
  reads nothing costs 0.02 ms, and a 1 ms switch interval changes
  nothing, idle or under load. `AO_SWITCH_INTERVAL_MS` stays as a switch
  for the next flight and is to be removed when K2 closes.
- **CPU pressure is not it either.** With every logical CPU taken by
  other processes the opens cost ten times more, 0.25 ms, not 300 times.
- **R13 is the explanation left standing, not proven here.** Before the
  fix every open with no entry and no open tile made a call to the
  parent's statistics store. In the replay the parent has no window and
  answers in about 0.3 ms (the cold pass with the fix removed: 2.5 s
  more over 8,646 such calls). In a flight the parent runs the window,
  and a call waits for the parent's interpreter, about one switch
  interval: the 5 ms the flights show. It also fits flight 1, where the
  opens that cost 0.24 ms came after the worker had lost its connection
  and counted locally. The replay cannot show this: its parent is
  headless.
- **So K2 has no more code to write until a flight is read.** The fix
  is in. The next trace times `open`, `getattr` and `release` and names
  the calling process, with the window open and X-Plane loading. If the
  opens that read nothing are under 0.5 ms there, K2 is closed by R13.
  If not, that trace shows which of the three calls waits, and the
  native answer described under K2 is the fallback.

**K6, on the replay: do not build the native open (S5).** Condition 1
holds (96 % of textures ready on a second pass). Condition 2 does not:
the open of a ready texture is 0.23 ms at the median and 0.57 ms at p95
(1.76 ms with every CPU busy), under the 2 ms the stage asks for, and
5 s in all for the 18,187 such opens of the pass.
`ready_open_time_share` cannot be read with native reads on (the reads
are not in the trace). To be confirmed with the flight's
`ready_open_ms`; if it holds, S5 is closed for good.

**2026-11-26: the side fixes.**

- The config file is written only when its text changes. One load saved
  twice with two different texts (before and after the hardware values
  were applied), which is why the file was rewritten at every start; the
  save in the middle of the load is gone.
- `python -m autoortho` starts the app (`flighttrack` and `datareftrack`
  are imported both ways in `main`, as the rest of that file does).
- The catalog worker parses the latest releases; drawing the scenery
  list does no I/O; a release's info file is downloaded once per
  process. A release that cannot be read is listed with its name and
  version.

**2026-11-26: CI's libraries.** The branch was pushed and the library
build run on it (`workflow_dispatch`; a push alone does not start it, it
runs for pull requests to `develop` that touch the C sources). Its commit
`ad397b51` is merged here and the local Windows build discarded; the
native-read tests pass on CI's `aopipeline.dll`.

## What waits for flights

Everything below needs X-Plane. Nothing else is open.

**The flights.** The central Mexico route of 2026-11-25 twice in a row
(about 25 minutes each), AutoOrtho started from this checkout with
`AO_READ_TRACE` set to a new file for each flight. Then, for the rule
that numbers come from two routes, a second route twice at max zoom 16.
The first pair is enough to close or reopen every stage; the second
pair makes the result general.

**What is read from them, and what follows.**

| Stage | Read from the second flight of a pair | Then |
|---|---|---|
| K1 | under 10 % of the opens that read find no entry; `dds_cache_evicted` stays 0 until `dds_cache_bytes` reaches the budget | closed, or the count is wrong somewhere a replay did not reach |
| K2 | opens that read nothing under 0.5 ms at the median and 15 s per 25 minutes; `getattr_ms`, `release_ms`; the `pid` and `flags` of those opens | closed by R13 and `AO_SWITCH_INTERVAL_MS` removed, or the native answer is built |
| K3 | `dds_direct_skip_partial_no_level` near zero; `dds_direct_loading_fallbacks` under a tenth of `dds_direct_loading_opens` | closed |
| K4 | under 5 % of the opens that read refused as too coarse | closed |
| K5 | `ready_texture_share` above 50 %, `dds_native_reads` far above `dds_native_deferred`, no error, on CI's library (already in this checkout) | `native_direct_reads = True` by default on Windows |
| K6 | `ready_open_ms` | S5 closed (expected) or built |
| Budget | whether complete textures are evicted within one flight at zoom 17 | keep `file_cache_size`, or raise its default |

Also from a flight, not blocking: what the opens that read nothing are
(K0's question; the trace now names the process and the open flags), and
`Shared stats` warnings in the log, of which there should be none.

After that, outside this plan: P0 of the prediction plan again, on a
cache that now holds a route, and the comparison flights against
OrthoStudio XP.

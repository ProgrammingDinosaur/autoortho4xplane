# Prediction plan: every texture ready before X-Plane asks

Status: proposal (2026-11-24). Nothing here is implemented.
Related: [hybrid-read-path-plan.md](hybrid-read-path-plan.md) (how cheap a ready
texture is to serve), [governor-behind-detection-plan.md](governor-behind-detection-plan.md)
(the miss causes the governor reads today), [performance-gap-plan.md](performance-gap-plan.md),
[ready-share-plan.md](ready-share-plan.md) (2026-11-25: the cache could not hold a route and most
opens read nothing; do its K1 before P0, and F11 and F12 below are its K4 and K3).

The hybrid read-path plan makes a ready texture cost almost nothing to serve.
This plan decides how many textures are ready. If prediction is wrong, the fast
path has nothing to serve and X-Plane waits for a build, so this plan matters
more than the other.

---

## The idea in short

X-Plane does not ask for textures at random. It asks for one detail level of
each texture when the aircraft comes within a fixed distance of it, and for the
next sharper level at the next, shorter distance. Given the list of textures
X-Plane has loaded and where the aircraft will be in the next minute or two,
its requests can be listed in advance, each with the moment it will arrive.

AutoOrtho does not use this today. It samples points on a straight line ahead
of the aircraft and queues the texture under each point at full detail. It
fetches the rest once, at whatever level looked right when X-Plane loaded the
area, and never revisits them. The result on your flights: about half of the
near reads are blurry, and very few opens find a finished file.

The plan replaces that with one planner:

1. Keep a table of every texture X-Plane has loaded, with the level that is
   ready on disk and the level X-Plane last read.
2. Every second, work out the next level X-Plane will read of each texture
   and when (the deadline), from the aircraft's position, track, turn rate and
   climb rate.
3. Build exactly that level as a finished file, earliest deadline first,
   early enough that it is done before the deadline.
4. Measure how long building takes and how often the plan was wrong or
   wasteful, and let the governor set the few numbers that remain.

A texture the plan misses is still built on demand at full quality, as today.
The pull path stays as the safety net. That is the advantage over OrthoStudio,
which shows a neutral colour when its prediction is late.

---

## Terms

- **Texture**: one `.dds` file X-Plane reads, 4096 px or 8192 px wide.
- **Level**: one mipmap of a texture. Level 0 is full size, each next level is
  half the width. A 4096 px texture needs 256 imagery chunks for level 0, then
  64, 16, 4 and 1 for levels 1 to 4.
- **Load level**: the coarse level X-Plane reads of every texture when it
  loads an area (level 4).
- **Ring**: the distance from the aircraft within which X-Plane reads a given
  level. One ring per level.
- **Deadline**: the moment a texture will cross its next ring, so the moment
  X-Plane will read its next level.
- **Lead**: how long before the deadline the plan wants the level finished.
- **Slack**: deadline minus the expected finish time. Negative slack means the
  level will be late.
- **Ready**: the level is in the DDS cache as a file `open` can serve directly,
  with no build.
- **Ready share**: the share of X-Plane's opens that found the wanted level
  ready. The number this plan exists to raise.
- **Waste**: levels built that X-Plane never read.

---

## What your flights show

Source: the governor's samples (`~/.autoortho-data/governor/samples-*.jsonl`),
the session reports and the `STATS` lines of the flights of 17 to 22 November.

### Blurry near reads, by what prefetch had done for the texture

"Near" is a read that wants level 0 or 1. "Sharp" is the share answered with
real imagery at that level, in flight (starts excluded).

| Flight | In flight | Sharp | Blurry reads | never predicted | coarse | late | ready yet blurry |
|---|---|---|---|---|---|---|---|
| 17 Nov 22:25 | 21 min | 48 % | 243 | 2 | 192 | 40 | 9 |
| 17 Nov 23:03 | 14 min | 68 % | 80 | 1 | 64 | 14 | 1 |
| 17 Nov 23:22 | 12 min | 71 % | 44 | 0 | 29 | 12 | 3 |
| 19 Nov 22:10 | 17 min | 65 % | 115 | 0 | 103 | 12 | 0 |
| 20 Nov 15:48 | 17 min | 62 % | 173 | 3 | 120 | 43 | 7 |
| 21 Nov 21:20 | 121 min | 51 % | 377 | 71 | 208 | 52 | 46 |
| 22 Nov 13:14 | 136 min | 56 % | 1,286 | 19 | 853 | 316 | 98 |

"Coarse" means prefetch had fetched the texture, but only at a coarser level
than X-Plane then asked for. It is the largest cause in every flight, 55 to
90 % of the misses. "Never predicted" is nearly absent: prefetch knows which
textures exist. It asks for too little of them, or too late.

### The flight of 22 November in detail

165 minutes, 748 nm, maximum zoom 17 (18 near airports), Bing imagery.

| | Measured |
|---|---|
| Texture reads by X-Plane | 67,086, taking 4,757 s in total (71 ms each on average) |
| Reads over 100 ms / over 1 s | 10.9 % / 0.7 % |
| Read calls that start in level 0 / in levels 1 to 5 | 9,052 / 46,316 (16 % / 84 %) |
| Opens served from a finished file | 488 of 96,632 |
| Full-detail textures prefetch chose | 2,671 |
| of which finished | 1,280 |
| of which dropped as no longer on the path | 1,145 |
| of which X-Plane opened first | 238, when prefetch still expected them 30 to 210 s later |
| Prefetch passes deferred because X-Plane was reading | 4,112 |
| Prefetch passes refused for lack of a free slot | 47,393 |
| "Ready ahead" distance published for Home | 2 km |
| Downloaded | 3.8 GB, 3.1 Mbit/s on average; the line measured 36 Mbit/s |
| Governor | pushed "prefetch lead time" to its maximum (180 s), then reported "the limit is how much detail prefetch asks for" |

The 4,757 s are thread time: X-Plane reads with 17 or more threads, so this is
not 79 minutes of frozen screen. It is the time its loader threads spent
waiting on AutoOrtho. The flight of 21 November shows the same: 4,875 s over
152 minutes. With finished files the same reads cost a few seconds in total.

### The line was not the limit

Rough demand at 450 kt and low altitude, zoom 16, a texture 7.5 km wide
(an estimate from the rings, not a measurement):

| Level | Ring | Textures crossing it per hour | Chunks per hour |
|---|---|---|---|
| 0 | 13.6 km | 400 | 104,000 (four times that at zoom 17) |
| 1 | 23.7 km | 700 | 45,000 |
| 2 | 42.9 km | 1,300 | 20,000 |
| 3 | 81.4 km | 2,400 | 10,000 |
| 4 (load) | every texture loaded | 5,000 | 5,000 |

That is about 1.5 Mbit/s for level 0 at zoom 16, 6 Mbit/s at zoom 17, and about
1 Mbit/s for all the other levels together. Your line carries 36. At cruise
altitude the rings reach less ground and demand drops further. The misses on
these flights are a prediction and scheduling problem, not a capacity problem.

Two more things follow from the table:

- Making every coarse level ready costs less than level 0 alone, yet levels 1
  to 5 are 84 % of X-Plane's reads.
- AutoOrtho's whole "predictive DDS" machinery aims at level 0.

### Limits of these numbers

- The flights ran before two fixes of 22 November: background builds are saved
  to the DDS cache again (`4cbe69ea`), and far textures can be served from a
  partly filled cache file (`3d91866d`). No flight has been flown since. The
  ready share of the current code is unknown and is the first thing to measure.
- Opens include ones X-Plane makes without reading a level, so "488 of 96,632"
  understates the share. The performance plan counted 476 of 4,330 opens that
  asked for a level in the first 73 minutes of the same session (11 %). Either
  way most opens were built on demand.
- "Sharp" understates level 0 slightly: level 0 counts as real only once every
  row is exact.
- One PC, one line, one provider.

### What was not checked

The findings below show the current prefetch logic is not optimal. They do not
show how far it is from optimal, or what the cheapest fix is.

- No experiment was run. The findings come from reading the code and from
  counters of past flights. No test lists which textures prefetch queued
  against which X-Plane read.
- Not read in depth: the coordinator's admission, priority and retry rules
  (`prefetch.py`), the background builder, the live-pressure controller and
  the SimBrief route path. The route path does collect textures within the
  radius, so F1 applies to flights without a route, as all of yours were.
- The alternative of patching the current prefetcher (F1, F2, F4, F8) and
  keeping its structure was not tested against the planner. Stage P0 does
  that, and Gate 0 stops the refactor if the patches are enough.

---

## Why prediction falls short today

Each finding names the code. None needs a new measurement to be believed,
except F7.

**F1. The "corridor" is a line of points.** `_do_velocity_prefetch_cycle`
(getortho.py) walks points along the heading and calls
`get_all_tiles_for_position(lat, lon)`, which returns the textures under that
one point. `prefetch_radius_nm` is used only as the spacing between points
(half the radius, at most 15 nm) and in the fallback without a terrain index.
With the default radius of 40 nm the points are 28 km apart: about one texture
in four on the centre line is queued at full detail, and none beside it. Your
governor shrank the radius to its minimum of 10 nm, which made the points
denser.

**F2. Textures beside the track are fetched once and never upgraded.** The
`.ter` load-ahead and the DSF prefetch fetch each texture at the level the
paging model gives at that moment, when the area loads and the texture is far
away. Nothing looks at it again as the aircraft approaches. X-Plane then asks
for level 3, 2, 1 and 0 in turn, and each is built on demand. This is the
"coarse" column above.

**F3. A predicted coarse level is chunks, not a file.**
`_prefetch_tile(first_mip=k)` downloads the JPEG chunks of level k and
coarser. No DDS is built. A coarse level becomes a servable file only after a
read has built it live once (`store_incremental`). On a first pass over an
area, every level-1-to-4 read is a live build in Python, even when prediction
was right.

**F4. Urgency is measured to the wrong place.** A candidate's ETA is its
distance divided by speed: the time until the aircraft is over it. X-Plane
reads it one ring earlier, 14 to 80 km before. The 238 textures X-Plane opened
while prefetch still held them had ETAs of 30 to 210 s. The order of work is
off by one to three minutes.

**F5. Full-detail work is chosen too far ahead and thrown away.** Lookahead is
10 to 30 minutes. 43 % of the full-detail candidates went stale before they
were finished. OrthoStudio measured the same and settled on two minutes.

**F6. Prefetch stops when X-Plane reads.** Live pressure defers every
prefetch pass. But X-Plane reads because prefetch was late, so prefetch is
stopped exactly when it is behind, and the next wave of reads is live too.

**F7. The level rule may be one level off for 8192 px textures (to verify).**
`paging_model` measures distance in the texture's own pixels, so it expects an
8192 px texture's level 0 at half the distance of a 4096 px one. OrthoStudio
traced X-Plane and found it counts levels from the top of the file whatever
its size: an 8192 px file is read whole at the same ring as a 4096 px file.
If that holds here, every zoom-17 texture is predicted one level too coarse
until the in-flight fit corrects it, and the fit is thrown away at the end of
each session (`paging_k` is not saved).

**F8. The path is extrapolated from magnetic heading.** The dataref read is
`mag_psi`, averaged over 60 s. The ground track differs by the magnetic
variation (over 15 degrees in Alaska or Greenland) and by wind drift, and the
average lags a turn by half a minute. Vertical speed is not read at all.

**F9. Nothing runs below 50 kt.** `MIN_SPEED_MPS = 25`. A helicopter, a slow
climb or a taxi gets no velocity prefetch.

**F10. The governor steers levers that do not control the outcome.** Radius
is a spacing (F1). Lead time changes a level guess that is made once (F2). And
there is no measure of waste, so evidence only ever pushes one way and the
levers end at their limits: lead 180 s, chunks per cycle 704, radius 10.


**F11. A live build of a coarse level is often not kept either (found
2026-11-25, hybrid read path replays).** F3 assumes a level becomes a file
once a read has built it live. On a synthetic cruise (Faroes, 239 textures
read from level 1), the warm pass found the stored entries starting at
level 4, the ZL12 imagery the `.ter` load-ahead warms, not at the level 1
the cold pass had built: 106 of 239 opens were refused as "partial entry
too coarse" (`dds_direct_skip_partial_coarse`). The likely cause:
`Tile._try_incremental_dds_store` persists only levels whose whole buffer
was built and skips prepopulated tiles, while those level-1 reads were
answered by row-range builds (`native_partial_strict_build`, about 800 per
pass) that never complete a whole level. To confirm in P0; P3's level
builder makes it moot for predicted textures, and live row builds that do
complete a level should be kept too.

**F12. Partial entries are not served while X-Plane loads.** A partial
entry is served only at the level the paging model names, and on the
loading screen there is no camera: `open_direct` gets no level and refuses
every partial entry (61 of 239 opens in the same replay,
`dds_direct_skip_partial_no_level`). Those textures are built live although
their coarse levels are on disk. Once the start position is known (item 8,
open question 2), the level can come from the distance to it; until then,
serving the entry from its finest level and letting a finer read fall back
to a live tile costs one extra open in the worst case.

---

## How X-Plane asks for textures

This is the rule the plan rests on. It comes from OrthoStudio's probe flights
(`live/rings.py`, `live/streamer.py`), from AutoOrtho's own read pattern
(`read_pattern.py`, `load_ahead.py`) and from the flights above.

- When X-Plane loads an area (130 to 230 km ahead), it opens every `.ter`
  file of it, then 20 to 45 s later reads the load level of every texture.
- As the aircraft approaches, X-Plane opens a texture again for each sharper
  level, at that level's ring. It reads from the start of that level to the
  end of the file, once, and keeps the result.
- Measured rings for a zoom-16 texture: level 3 within 81 km, level 2 within
  43 km, level 1 within 24 km, level 0 within 14 km. A zoom-18 texture follows
  the same rule four times closer.
- Distance is from the aircraft, in three dimensions. Camera views do not
  matter. At cruise a texture straight below is already 10 to 12 km away.
- The rings scale with the screen: a wider image or a narrower field of view
  moves every ring out.

So for each texture the next read is known: one level sharper than the last
one X-Plane read, at the moment the texture crosses that level's ring. The
only uncertainty is where the aircraft will be, and only over the next minute
or two, because nothing needs to be finished earlier than that.

---

## What OrthoStudio does, and what to take from it

| OrthoStudio | Verdict | Why |
|---|---|---|
| A planner that compares, for every texture within 300 km, the level needed soon with the level built, every 2 s | **Take** | This is the missing piece (F1, F2). AutoOrtho has producers that each queue a texture once. |
| Builds exactly the level that will be read, as a finished file | **Take** | F3. AutoOrtho already has the file format for it (partly filled cache files, served directly). |
| Urgency = distance left to the ring | **Take**, as time | F4. In seconds it works at any speed. |
| Every texture in the loaded area has at least its load level | **Take** | Up to 22,000 of the read calls on 22 November start at that level. |
| Rings by level index, scaled by screen width and field of view | **Check, then take** | F7. AutoOrtho's one-constant model is close; the file-size rule differs. |
| Two minutes of path, not ten | **Take** as the starting point | F5. AutoOrtho derives the lead from measured build times instead of fixing it. |
| Distance from the aircraft in 3-D, climb and descent anticipated | **Take** | F8. Add true track and vertical speed. |
| The airport's whole block at full detail before the flight starts | **Take** | Departure and arrival are the worst phase here (44 % sharp). |
| Pause work ahead while X-Plane loads scenery or its worker threads are all busy | **Take** | Costs one dataref. AutoOrtho only watches frame time today. |
| No full detail above 18,500 ft, where the screen cannot show it | **Take as an option** | Half of OrthoStudio's downloads at cruise. A quality choice, so the user or the governor decides. |
| Scripted test flights and probes | **Take** | The plan needs traces to tune and to prove itself. |
| Fixed constants (2 min, 4 encoders, 11 % margin) tuned on one Mac | **Leave** | The governor and measured times replace them. |
| Neutral colour when late; a plugin to hold the loading screen | **Leave** | AutoOrtho's pull path already waits or serves a stand-in, on every platform, with no plugin. |
| Real files swapped by rename | **Leave** | Decided in the hybrid plan. |

AutoOrtho keeps three things OrthoStudio lacks: a miss is repaired on demand
at full quality, the ring model calibrates itself to each user's screen, and
the governor tunes the rest per PC and line.

---

## Design

The parts below are pure Python on top of builders and caches that exist.
Nothing is specific to an operating system.

### 1. The texture table

One row per texture X-Plane may read.

- **Where rows come from.** The `.ter` files X-Plane opens (an exact list, the
  hook exists: `note_terrain_opened`), and the terrain index for the 1-degree
  squares within load distance, so rows exist before X-Plane opens them.
- **What a row holds.** Centre position, ground size, file width, target zoom;
  `ready_level`, the sharpest level servable from disk (kept current by the DDS
  cache's store and evict hooks); `held_level`, the level X-Plane last read
  (from the read hook, forgotten when the area unloads); `building_level`.
- **Size.** About 3,000 zoom-16 textures lie within 230 km, plus airports:
  under 10,000 rows held as flat arrays. One pass over them takes a few
  milliseconds, so the planner runs every second at well under 1 % of a core.
  The prefetch loop it replaces used a quarter of a core while parked.

`ready_level` in memory is also what the hybrid plan needs for a cheap `open`
(its step N3) and later for a native `open`. Build it once and use it for both.

### 2. The ring model

- Start from one rule: the ring of level n is twice the ring of level n-1, and
  all rings scale with screen width and field of view. This is what
  `paging_model` holds today as the constant k.
- Learn it per user. Every open gives a sample (level, distance, file width).
  The fit that exists is kept, with two changes: file width is handled as the
  trace says it must be (F7), and the result is saved between sessions, keyed
  by window width and field of view. A flight then starts from what the last
  one measured, not from a guess.
- Be generous when unsure. Until a fit exists, use the larger of the guess and
  the shipped default. Building a level slightly early costs a little;
  building it late costs a wait.
- A margin on top (start 1.12, governed) covers what the fit cannot.

### 3. Where the aircraft will be

Inputs, all from X-Plane's datarefs over UDP: position, true ground track
(`hpath`, or derived from successive positions when X-Plane reports none),
ground speed, height above ground, vertical speed, and the turn rate taken
from the track's change over the last five seconds.

- **Central path.** Hold the current turn rate for the lead time, then fly
  straight. With a SimBrief route loaded and the aircraft on it, follow the
  route's legs instead. Height along the path follows the vertical speed, so
  a descent brings the rings down to the ground ahead of time.
- **What the aircraft could do.** In t seconds it cannot move more than
  speed times t, whatever the pilot does. Every texture whose next ring lies
  within that distance is "reachable". Reachable textures off the central
  path get a later, lower-priority deadline. If capacity covers them all, no
  manoeuvre can make X-Plane wait. A governed number (`plan_turn_cover`)
  sets how much of this set is built at the finest levels.
- **Slow or stopped.** Below 10 m/s there is no path, only the rings around
  the aircraft. That covers taxi and hover (F9).

### 4. The plan

For each row, every second:

1. The level needed is the sharpest level whose ring the texture is inside at
   any point of the path within the lead.
2. If `ready_level` is already that sharp, nothing to do.
3. Otherwise a job: build to the needed level, with the deadline of the first
   missing level (the moment the path first enters its ring).

Jobs whose deadline lies beyond the lead are not started. A job that has
started is finished unless the texture is by then two rings away, so a small
change of heading does not throw work away (F5).

### 5. The level builder

Prediction must end in a file, not in chunks (F3).

- **Levels 1 and coarser.** Download the chunks of that level's zoom (64 or
  fewer), encode the level, derive the coarser levels from it by halving when
  they are missing, and write them into the texture's cache file in the format
  `store_incremental` already writes. `open_direct(min_level=...)` then serves
  the texture with no build. Partly filled files and their direct reads exist
  since `3d91866d`; what is new is that a background job fills them, not only
  a live read.
- **Level 0.** The existing full-quality path stays: `PrefetchCoordinator`
  and `BackgroundDDSBuilder`, with exact-row manifests and the
  `prefetch_quality_mode` rules. The plan feeds it; the velocity samples no
  longer do.
- **Hand-over.** If X-Plane opens a texture whose level is still being built,
  the open takes over the chunks already fetched, as promotion does today.
- **Cost.** Levels 3 and 4 together are five chunks and a few milliseconds of
  encoding. Level 1 is 64 chunks.

### 6. Order of work, and what happens under load

Three classes:

| Class | What | Runs |
|---|---|---|
| Due | slack at or below zero, or X-Plane is waiting on it | always, at live priority |
| Planned | deadline within the lead | earliest deadline first |
| Ahead | load levels of the next squares, the route beyond the lead | spare capacity only |

- **When the work due exceeds what the line or the CPU delivers**, the plan
  trims by level, not by area: every texture gets its coarse levels on time
  before any gets its finest. Under the `quality` wait policy X-Plane then
  waits only for the finest levels. Under `smooth` it never waits and shows
  one level less. No lever is needed for this; it is the order of the queue.
- **A floor for planned work.** While X-Plane reads, the plan keeps a small
  share of the download slots and one builder (F6). The governor sets the
  share.
- **Yielding.** While X-Plane loads scenery (one more dataref,
  `async_scenery_load_in_progress`), while frames are slow, or while the RAM
  brake is on: "ahead" stops, "planned" slows, "due" continues.

### 7. The load-level floor

- When X-Plane opens a square's `.ter` files, build the load level of all its
  textures at once. X-Plane reads them 20 to 45 s later. A square has about
  170 textures at one chunk each.
- Better, build it before that. X-Plane loads squares as the aircraft crosses
  degree lines, so the next row or column is known from the path. The terrain
  index lists its textures.
- Keep these coarse levels on disk longest. A second flight anywhere you have
  flown starts with every load-level read ready.

These reads come in bursts of hundreds across all of X-Plane's loader threads,
which is where AutoOrtho's serialised read path hurts most.

### 8. Start, pre-flight and arrival

- **Start.** X-Plane's loading screen already waits on AutoOrtho's reads. The
  plan runs as soon as the position is known: from the first dataref, or
  before that from the SimBrief origin or the last session's final position,
  confirmed when the first `.ter` opens fall in the same square. It builds the
  rings around the start and the whole block of zoom-18 textures of the
  airport at full detail.
- **Pre-flight.** With a SimBrief plan loaded and X-Plane not yet flying,
  AutoOrtho can prepare the origin block, the destination block and the load
  level along the route. Bounded by the disk budget and a polite download
  rate, with a setting to turn it off.
- **Arrival.** From the top of descent the destination's block is planned
  work, not spare-capacity work.

### 9. Eviction that knows the plan

The cache never evicts a level the plan lists within its horizon. It evicts
what lies behind the aircraft first and the coarse base levels last.

### 10. What is retired

The velocity point sampling, the `paging_split` warm-up, and the one-shot
level choices of DSF prefetch and `.ter` load-ahead. Their hooks stay as
sources of table rows. `SpatialPrefetcher` shrinks to the SimBrief route
provider. `PrefetchCoordinator` stays as the executor for level 0.

---

## The governor

You asked that the new settings work with the governor so each PC finds its
own best values. Three rules make that possible.

1. **Everything is in seconds or ratios.** No distances, no chunk counts. The
   same value means the same thing at 100 kt and at 500 kt, on 10 Mbit/s and
   on 1 Gbit/s.
2. **What can be measured is not tuned.** The lead is computed from how long
   this PC and line take to make a level ready. A slow line gets a longer lead
   by itself. The governor only trims a factor on top.
3. **Every lever has evidence in both directions.** Misses push a lever up,
   waste pushes it down. Today there is no waste signal, so levers ratchet to
   their limits (F10).

### New signals (in `governor.Sample`)

| Signal | Meaning |
|---|---|
| `ready_opens`, `built_opens` | opens served from a finished file / built on demand, by level class (0, 1 to 2, 3 and coarser) |
| `plan_slack_p10_s` | slack of the tightest tenth of planned jobs; negative means behind |
| `time_to_ready_p95_s` | measured time from job start to ready file, by level class |
| `plan_built_bytes`, `plan_unread_bytes` | what the plan built / what X-Plane never read before the texture fell behind |
| miss counters (below) | why an open was not ready |

### Miss causes

They replace `never_*`, `coarse` and `late` in `prefetch_ledger`. Each has one
cause and one owner.

| Cause | Meaning | Move |
|---|---|---|
| `ring` | the plan's level for the texture was coarser than X-Plane read | ring margin up; refit the model |
| `turn` | outside the central path because the aircraft manoeuvred | turn cover up |
| `late_lead` | planned and started, but started too close to the deadline | lead factor up |
| `late_capacity` | planned in time, then waited in the queue | the existing network and compute states; plan share up if it was starved by live reads |
| `paused` | the plan was paused (RAM, gate, yielding) | none; a notice names the pause |
| `evicted` | ready, then evicted | the existing memory rule |
| `not_served` | ready on disk, yet the open built live | none; a defect counter |
| `unknown` | the texture was not in the table | none; a defect counter |

Reads while a flight starts are excluded, as today.

### Levers

| Setting (`[streaming]`) | Meaning | Range | Start | Up on | Down on |
|---|---|---|---|---|---|
| `plan_lead_factor` | lead = factor x `time_to_ready_p95_s` + one planner period | 1.2 to 4 | 2 | `late_lead` | waste |
| `plan_ring_margin` | rings widened by this | 1.00 to 1.40 | 1.12 | `ring` | waste |
| `plan_turn_cover` | share of the reachable set built at the finest levels | 0 to 1 | 0.5 | `turn` | waste |
| `plan_share` | least share of download slots the plan keeps while X-Plane reads | 0.1 to 0.5 | 0.25 | `late_capacity` during live reads | near reads waiting longer |
| `plan_horizon_min` | how far along the path or route spare capacity is spent | 2 to 30 min | from the tuning plan | headroom | any pressure state |

Parallel downloads, builders and the memory levers stay as they are.

### Rules

- The plan states run only when the ready share is below target and no
  resource state (memory, network, compute, frame time) explains it, as the
  miss states do today.
- One lever, one step, keep or revert, as today. The judged metric is the
  ready share, and for downward moves the waste share.
- With the ready share at target and waste above 25 %, the governor eases the
  lever with the most unread bytes behind it. This is today's "headroom"
  easing, given a direction.
- When the line or CPU is the limit, no plan lever moves. The queue already
  trims by level (design part 6), and the governor reports what limits it,
  as it does now.
- Start values come from the tuning plan (line speed, cores, target zoom) and
  from the learned profile of earlier flights. The ring model is saved per
  screen.

### Retired settings

`prefetch_lookahead`, `prefetch_radius_nm`, `paging_lead_seconds` and
`prefetch_max_chunks` stop being governed. They are read for one more release:
a pinned lookahead maps to `plan_horizon_min`; the others are ignored with a
line in the log. `prefetch_enabled` and `predictive_dds_enabled` keep working
as the off switches.

---

## Proving it without flying every time

- **Trace.** Extend the opt-in read trace (`AO_READ_TRACE`) with, per open:
  time, texture, level wanted, how it was served (ready, partly ready, built),
  the wait, and the aircraft's position, track, speed and height. Add the
  aircraft's state once a second.
- **Simulator.** `tools/bench/predict_sim.py` replays a trace through a
  planner and a capacity model (downloads per second, builds per second) and
  prints the ready share by level, the lead each open would have needed, and
  the waste. Old and new planner run on the same traces. No network, no
  X-Plane, seconds per run.
- **Matrix.** The simulator runs each trace across line speeds (5, 20,
  100 Mbit/s), core counts (4, 8, 16) and zooms (16, 17, 18), with the
  governor in the loop. This is the check that the levers settle for every
  kind of user and not only on this PC.
- **Scorecard.** The flight report gains ready share by level and phase, miss
  causes, waste share and the measured rings.
- **Reference flights.** Three scripted flights (a jet leg with a turn, a low
  GA circuit, a helicopter hop), flown cold and warm. OrthoStudio's test pilot
  can fly them; its AutoOrtho runner is Mac-only today.

---

## Implementation plan

Each stage ships alone. Sizes: S under 200 lines, M under 600, L more.

| Stage | Content | Main files | Size | Gate or exit |
|---|---|---|---|---|
| P0 Measure | Trace fields, simulator, scorecard, three reference traces. Re-baseline the current branch. Settle F7 from the trace. Run the simulator on two candidates: the current prefetcher, and the current prefetcher with F1, F2, F4 and F8 patched. Both are scored with predicted levels counted as finished files, because the level builder (P3, which fixes F3) is needed whichever candidate wins. | `read_pattern.py`, `diagnostics.py`, new `tools/bench/predict_sim.py` | M | The baseline ready share and ring fit are recorded. **Gate 0:** if the patched prefetcher reaches the definition of done on the traces, ship the patches and P3, and skip P2 and P4. Otherwise its score is the bar the planner must beat at Gate A. |
| P1 Inputs | True track, vertical speed, turn rate, scenery-loading flag. Save the ring model per screen. Waste accounting in the ledger. | `datareftrack.py`, `paging_model.py`, `prefetch_ledger.py`, `governor.py` (profiles) | S | Path error on the traces under 5 degrees. |
| P2 Planner in shadow | Texture table and a pure planner module (no `getortho` import, injected clock, like `prefetch_ledger`). It acts on nothing; it logs what it would have had ready. | new `texture_table.py`, new `level_plan.py`, hooks in `getortho.py` | L | **Gate A:** on the reference traces the shadow plan names at least 95 % of X-Plane's opens (texture and level) a full lead ahead, with waste under 30 %. If not, fix the model before going on. |
| P3 Level builder | Background jobs that write levels 1 and coarser into cache files. Keep the levels live row builds complete (F11). | `getortho.py` (`BackgroundDDSBuilder`), `dynamic_dds_cache.py` | M | A level built in the background is served directly and returns the same bytes as a live build. |
| P4 Planner on | Setting `[streaming] planner = off / shadow / on`. The plan feeds the level builder and the level-0 coordinator. The old producers stop when it is on. | `getortho.py`, `prefetch.py`, `aoconfig.py`, `settings_registry.py` | L | **Gate B:** ready share at least 80 % on a cold reference flight, CPU not above the baseline. The old path stays the default until this passes. |
| P5 Floor | Load level at `.ter` open and for the next squares; base levels evicted last. | `load_ahead.py`, `disk_budget_manager.py` | S | Load-level reads ready: at least 95 %. |
| P6 Governor | Signals, miss causes, levers, rules, start values, texts for Home and Tuning, retired settings. | `governor.py`, `tuning_plan.py`, `settings_registry.py`, `home_snapshot.py` | M | Simulator matrix: the levers settle within range in every cell, and no cell ends with a lever at its limit while waste is above 25 %. |
| P7 Start and route | Start position, airport block, pre-flight preparation, arrival. A partial-entry level on the loading screen (F12). | `getortho.py`, `flighttrack.py`, UI | M | A start at a known airport waits for no near read after the loading screen. |
| P8 Yield and caps | Yield rules; the height-aware detail cap as a governed option. | `level_plan.py`, `governor.py` | S | No frame-time regression on the reference flights. |
| P9 Platforms | Reference flights on Linux and macOS. | none | S | The definition of done on all three. |

Order: P0 and P1 first. P2 and P3 can run side by side. P4 needs both. P5 to
P8 in any order after P4. The hybrid plan's step N3 (the in-memory index for a
cheap `open`) and P2's texture table are the same structure: whoever builds
one should build it for both.

---

## Risks

| Risk | What limits it |
|---|---|
| The ring rule is wrong for some screen, setting or X-Plane version | The model is fitted per user and saved; `ring` misses raise the margin; a miss is still built on demand. |
| More background work costs frames | Planned work has a share, not the whole machine; yielding on scenery load and slow frames; the frame-time state already cuts builders. Gate B checks CPU. |
| Waste: levels built and never read | Jobs start only within the lead; waste is measured and pushes the levers down; target under 25 %. |
| A level file written while X-Plane holds it open | A finer level is written into its own range of the sparse file and the metadata is switched atomically, as `store_incremental` does now. Covered by a byte-identity test in P3. |
| Two planners during the transition | One setting chooses; the old producers are off when the plan is on. |
| Imagery servers see more requests | Total chunks drop (less far-ahead level-0 work thrown away, F5). Pre-flight preparation has its own rate cap. |
| The start position is guessed wrong | The guess must be confirmed by the first `.ter` opens before anything sharp is built. |
| Line slower than demand (fast jet, zoom 18) | No prediction fixes that. The queue trims by level and the governor says so. |

---

## Definition of done

On the reference flights, on Windows, Linux and macOS:

- At least 95 % of X-Plane's opens in flight find the wanted level ready, cold
  cache. At least 99 % warm.
- No texture read over 1 s in flight, starts excluded. (0.7 % of reads today.)
- With the hybrid read path in place, X-Plane's time in texture reads is under
  one minute per flight hour. (About 29 minutes per flight hour today.)
- Waste under 25 % of the bytes the plan built.
- Planner CPU under 1 % of a core. AutoOrtho's total CPU and download volume
  not above the P0 baseline.
- On a 10 Mbit/s line with four cores: under `smooth`, no read waits and at
  least 90 % of opens are ready; under `quality`, only level 0 and 1 reads
  wait, and the governor names the limit.
- In the simulator matrix the governor settles every plan lever inside its
  range.

---

## Open questions

1. Does X-Plane count levels from the top of an 8192 px file here, as
   OrthoStudio found on a Mac (F7)? P0 answers it.
2. Does X-Plane send position datarefs while the loading screen is up? If so
   the start needs no guess.
3. How many degree squares does X-Plane load ahead with your scenery-distance
   setting? The trace of `.ter` opens against position answers it.
4. Should pre-flight preparation run by default, or only when asked?
5. The height-aware detail cap lowers what is shown at cruise to what the
   screen can resolve. Is that acceptable as a default under `balanced`, or
   only under `smooth`?

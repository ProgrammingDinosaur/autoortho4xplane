# Configuration

Setup and configuration for most use cases should be pretty simple. The important settings are:

* Scenery install path
* X-Plane install path
* Downloader directory

## First-run setup

On a new installation, AutoOrtho opens a guided setup wizard that:

1. **Welcome and checks:** the required FUSE backend for this platform.
2. **X-Plane:** detected, or browse for it.
3. **Scenery:** the scenery folder, and regions to install (or use scenery
   you already have).
4. **Image cache:** one folder, its free space, and a disk cache limit
   (with a suggestion for that drive). The long-term cache and download
   folders are under **More folders…**.
5. **Quality target:** one zoom level everywhere, or an altitude profile
   (Airliner, General Aviation, Low VRAM). It shows what the target needs on
   this PC, can test your connection, and can suggest a target.
6. **Limits and ready:** **Use what's available (recommended)** or your own
   limits, a last readiness check, and **Start AutoOrtho when setup
   finishes** (it waits if scenery is still being installed).

Existing installations with valid paths and dependencies are detected
automatically and are not forced through the wizard. The wizard remains
available from the header overflow menu or **Settings → Setup & Storage**.
If setup is interrupted, reopen the wizard from that menu; unaccepted wizard
changes are discarded and the last applied configuration remains active.

## Application navigation

The application is organized around five primary destinations:

- **Home** — what AutoOrtho is doing now (see [Home](#home) below).
- **Scenery Library** — scenery search, filters, installation, updates, and
  package details.
- **Flight Plan & Map** — SimBrief controls, loaded-flight details, and the
  local Custom Map Editor.
- **Settings** — Essentials first, every setting with **Show all settings**,
  search, exact numeric inputs, and Apply/Revert.
- **Diagnostics** — health checks, searchable logs, export actions, and
  performance-report history.

The last destination and window geometry are restored on the next launch.

### Keyboard and accessibility

All primary workflows are keyboard accessible. Use `Ctrl+1` through `Ctrl+5`
to open Home, Scenery Library, Flight Plan & Map, Settings, and Diagnostics.
Use `Ctrl+S` to apply settings, `Ctrl+Shift+R` to revert pending settings, and
`F1` to open documentation. Statuses combine text, symbols, and color so they
remain understandable without color perception.

AutoOrtho follows the operating system UI font and supports Qt display scaling,
including 200% scaling. Search fields, tables, task actions, settings controls,
and custom editors expose accessible names for assistive technologies.

## Home

Home answers four questions in order: is it working, are you getting the
quality you asked for, what is AutoOrtho using, and do you need to do
anything. What it shows follows the state:

- **Stopped:** the status strip with **Start streaming**, *Ready to fly*
  (the checklist appears only when something is wrong), the quality target
  with the lights from the cost preview and an **Edit** link, the last
  flight, storage, and scenery.
- **Starting:** mounting, waiting for X-Plane, and how much of the nearby
  scenery is already sharp while X-Plane loads.
- **Flying:**
  - **Scenery quality:** % sharp at target over the last 30 seconds, the target, the time to sharp scenery after an area loads, and a ten-minute trend with blurry moments in red. Click it for the Auto-Tuning timeline.
  - **Network, Computer and Graphics memory** gauges: use against the capacity or your limit, coloured by headroom. The one auto-tuning finds is holding the scenery back gets a ◆ chip.
  - **Activity:** textures served, the share from cache, data downloaded, builds in progress, and how far ahead of the aircraft everything is already sharp (in minutes and nm). It also has a small radar of the tiles around the aircraft (green sharp, blue being read, amber below target, grey not loaded) with the prefetch corridor; click it for the map.
  - **Auto-tuning:** the last change and why, the number of changes and pinned values.
- **Paused:** the same, dimmed, and the numbers stop.
- **After a flight:** the last flight in full: the verdict, sharpness by
  phase, distance, duration, data and cache share, guard activations and what
  auto-tuning learned. **Dismiss** returns to the stopped view.

Notices (at most three, the most serious first) appear between the strip and
the rest only when there is something to do: a target out of reach,
auto-tuning advice, values kept from an older config, or a failed scenery
install. Scenery installs show as a badge on **Scenery library**. **Compact**
keeps only the strip, quality and gauges (and hides the navigation) for a
second monitor. While streaming, the window title reads like Home's headline,
for example `AutoOrtho · 96 % sharp · 142 Mbit/s · Flying`. With **Minimize
to the system tray when streaming starts**, the window goes to a tray icon
with the same text as its tooltip, and comes back when streaming stops.

## Essentials and all settings

**Settings** opens on **Essentials**: the quality target with what it needs
on this PC, the limits, the folders and disk cache, the imagery source,
seasons, night exclusion, SimBrief and whether the window hides while
streaming. AutoOrtho sizes everything else for this PC. **Show all settings**
(top right) shows every category; searching always looks through all of
them, and opening a category from elsewhere in the app shows them too.

The four performance profiles are gone: the quality target (with its
altitude profiles), the limits and the automatic sizing cover what they
did. The minimum zoom level is now an Expert setting (All Settings), since
the low-zoom base covers far scenery.

When an older config is converted and it set engine settings by hand,
those values are kept, and Home says so: **Review** opens them in the
Expert table, and **Use automatic values** puts AutoOrtho's own values back
(press **Apply** to keep them).

## Applying and reverting settings

Configuration edits remain pending until **Apply** is selected. **Revert**
restores the last applied values. If streaming is started or the application is
closed with pending edits, AutoOrtho asks whether to apply or discard them.
Settings that affect mount workers display a restart notice when applied during
streaming.

Long-running operations appear in the persistent **Activity** panel. Completed
and failed tasks remain available until dismissed. Only operations that can be
stopped without corrupting files expose a Cancel action.

Use the header's **Start Streaming** action after readiness checks pass. The
same action becomes **Stop Streaming** while AutoOrtho is running. Start and
stop progress remains visible as an Activity task, and the application waits
for mounts to stop cleanly before it exits.

## Scenery install path
This is the location that scenery will be installed to.  Previously this defaulted to a user's existing X-Plane Custom Scenery directory, but that is no longer the case.  

It should be possible to set this to a convenient location with enough room to install scenery packages.  Each scenery package can take around 20-30GB.

This can be an external NAS or separate drive, but I have not tried all drive combinations.  Speed of external storage will naturally impact performance to a certain degree.  Plan accordingly.

## X-Plane install path
This is the X-Plane install location.  Under this directory should be X-Plane's `Custom Scenery` directory. 
*IT IS IMPORTANT THIS IS THE CORRECT LOCATION*

From this directory AutoOrtho will create mount points and run the program.

*IF THIS IS NOT CORRECT THINGS WILL NOT WORK RIGHT*

## Download directory
This is the path that will be used to temporarily store zip files and other fetched files for scenery setup.  By default this will be in under the user's home dir under `.autoortho-data/downloads`

For Windows users, it is highly recommended to set a Windows Defender exception to this directory otherwise expect *VERY* slow setup of scenery.

This folder can be set to any convenient location with enough space for scenery downloads (20-30GB per).

## User config file location

The configuration file `.autoortho` is located in the user's home directory.

### Only changed settings are stored

The file keeps every setting with its comment, but only the settings you
changed are active lines. Every other setting is written as a commented
default:

```ini
# Maximum zoom level to allow. ...
# max_zoom = 16
fallback_timeout = 45.0
```

Here `max_zoom` follows AutoOrtho's default, so it picks up any future change
to that default, while `fallback_timeout` keeps your own value. To override a default
by hand, delete the `# ` and change the value. A line you write yourself stays
active even when it equals the default, until you change it in the app.

The file is written as UTF-8. Older files written in another encoding are
still read, and the first save converts them: values equal to the defaults
become commented lines, and retired settings (`live_aopipeline_min_chunk_ratio`,
`ephemeral_dds_cache_mb`, `max_concurrent_downloads`, `http2_max_connections`,
and the old waits `maxwait`, `suspend_maxwait`, `tile_time_budget` and
`use_time_budget`) are removed after their values are migrated. The old waits
have nothing to migrate to: the wait bands replaced them (see the
[Performance Tuning Guide](performance.md#wait-policy-wait-bands)).

### All Settings (Expert)

**Settings → All Settings (Expert)** lists every setting with its value, its
default and whether you changed it. Search by name or description, filter by
group (Essential, Tuning, Expert), or show only changed settings. Double-click
a value to edit it, or select rows and choose **Reset Selected to Default**.
Settings that already have a control on another settings page are shown
greyed and are changed there. Edits are applied with **Apply** and discarded
with **Revert**, like any other setting. The Source column shows **This PC**
for values scaled to your computer (below); **Reset** returns a setting to
that value.

### Defaults scaled to this PC

A few defaults suit a mid-range PC but are wrong at either end, so AutoOrtho
scales them to your cores and RAM when you have not set them yourself:

| Setting | Rule | 8 GB, 4 cores | 16 GB, 8 cores | 32 GB, 12 cores |
|---|---|---|---|---|
| `cache_mem_limit` | a quarter of RAM, 2 to 8 GB | 2 | 4 | 8 |
| `predictive_dds_memory_mb` | by RAM, 256 to 1024 | 256 | 512 | 768 |
| `live_builder_concurrency` | one per physical core, 4 to 12 | 4 | 8 | 12 |
| `background_builder_workers` | one per three cores beyond two, 1 to 4 | 1 | 2 | 3 |

These values are not written to the config file, which shows them as
`# (set for this PC; the packaged default is 4)`, so the file still suits
another PC. A value you set yourself always wins. Set
`[tuning] hardware_baseline = False` to use the packaged defaults instead.

After a connection test, AutoOrtho also starts each flight's download
concurrency (`provider_origin_initial_concurrency`) at the number the test
suggested, when the test measured the imagery source in use; the adaptive
controller still moves from there. The Expert table shows that value as
**Measured**. A download limit also caps how many downloads run at once
(`provider_origin_max_concurrency`): about what the limit needs, with half
again to spare.

## Resource limits

**Settings → Limits** sets what AutoOrtho may use of this PC. Each limit is
**Auto** (worked out from the hardware) or your own ceiling:

| Limit | Auto | What it holds back |
|---|---|---|
| RAM (`ram_gb`) | a quarter of RAM plus 0.5 GB, leaving X-Plane at least 4 GB or 20 % | the memory cache and predictive build memory AutoOrtho picks; the low-memory brake engages when AutoOrtho goes over it |
| Graphics memory (`vram_gb`) | the card's memory minus `xplane_vram_reserve_gb` | the budget the graphics memory guard checks the target against |
| Download speed (`bandwidth_mbps`) | no limit | every download: the broker reads no faster than this, so other apps keep the rest |
| CPU cores (`cpu_cores`) | all physical cores but two (all but one on four cores or fewer) | native texture-build threads, and the builder counts AutoOrtho picks |
| Protect X-Plane's frame rate (`protect_sim_fps`) | on | in Auto mode, auto-tuning eases off background builds and prefetch when X-Plane's frames get clearly slower (see below) |

Limits lower only the values AutoOrtho picks itself. A value you set by hand
elsewhere is kept, and the guards warn when it is over a limit. The Expert
table shows a lowered value as **Your limits**. Changing a limit while
streaming takes effect at once: AutoOrtho resizes its memory cache, build
workers, download rate and download concurrency without remounting. (Raising
the CPU limit above what streaming started with takes full effect at the
next start.)

Edits in the Expert table to settings a running mount can pick up (prefetch
distance, live admission, background builders, memory budgets and the like)
also apply at once; only the others ask for a restart.

### Connection test

**Test my connection** on the Limits page (and in the setup wizard)
downloads about 30 MB of real imagery from your imagery source, in rounds of
16 to 256 downloads at once, and reports the speed and the fewest downloads
at once that reach 90 % of it. A typical run takes a few seconds. The result
is kept in `~/.autoortho-data/connection.json`; the download guard compares
it with what the zoom target needs, and **Suggest a target for this PC** uses
it. The test is disabled while streaming, because it would share the
connection. Apple Maps and custom maps are tested with Bing, since they need
a session.

## Guards: what your settings need from this PC

Under the zoom controls in **Settings → Imagery**, a panel shows what the
zoom target needs on this PC, with a light for each resource:

- **Graphics memory:** the photo textures X-Plane holds for the target,
  against the card's memory minus what X-Plane needs for itself
  (`[guards] xplane_vram_reserve_gb`, 5 GB), or against your graphics memory
  limit when you set one. Red when it does not fit, amber when close.
- **RAM:** the memory cache plus about 1 GB for the app, against your RAM.
  Red when less than 6 GB is left for X-Plane, amber above 35 % of RAM or
  above the RAM limit.
- **Disk:** free space on the cache drive against what the cache may still
  take (its limit minus what it holds now, plus the safety margin), and how
  much new imagery an hour of flying adds.
- **Download:** the speed the target needs to keep up, against the last
  connection test and your download limit. Amber when it needs more than
  80 % of the slower of the two.

**Suggest a target for this PC** fills in the sharpest fixed zoom level that
both the graphics memory budget and the connection carry.

The estimate follows X-Plane's texture paging: full detail only near the
aircraft, less further away, and textures stay loaded for about 20 minutes.
It works out each flight phase (on the ground, departure, climb, cruise, or
each altitude step in Dynamic mode) and shows the busiest one. For example,
from cruise altitude ZL17 needs no more graphics memory than ZL16, because
X-Plane never asks for the extra detail from that height. Treat the figures
as estimates: wide screens, a wide field of view and turning flight hold
more.

When a light is amber or red, **Apply suggestion** fills in the nearest
setting that fits: one zoom level lower, a smaller memory or disk cache, or
the automatic graphics memory or a higher download limit when your own
limit is the only problem. Nothing changes until you press **Apply**. Guards never block
anything. When the saved settings do not fit, Home's Readiness card lists
**PC resources** with the reason. Set `[guards] warnings = False` to turn the
panel and that check off.

Graphics memory is read from Windows' display driver information, from
`nvidia-smi` or the AMD driver on Linux, and on Macs with Apple silicon is
taken as about two thirds of RAM. When it cannot be read, the panel says so
and does not warn.

### Low-disk brake

When the cache drive has less free space than the safety margin
(`[scenery] storage_safety_margin_gb`, at least 2 GB), AutoOrtho stops
writing new cache entries until 1 GB more is free. Scenery keeps
streaming; only caching pauses. Counted as `disk_brake_activations`.
`[guards] ram_brake = False` turns this brake off too.

### Low-memory brake

While the system has very little free RAM (below 1.5 GB or 8 % of RAM), or
AutoOrtho uses more than its RAM limit, AutoOrtho pauses prefetch and
background builds and holds its memory cache to half its limit. X-Plane's
own requests are always served. The brake releases once free RAM has stayed
above 2.5 GB (or 12 %), and AutoOrtho under 90 % of its limit, for 20
seconds. Each activation is logged and counted in the performance report
(`ram_brake_activations`). Set `[guards] ram_brake = False` to turn it off.

## Auto-tuning

### Starting values: the tuning plan

Before a flight, AutoOrtho picks starting values for the settings you have
not set yourself (`[tuning] hardware_baseline`, on by default):

* from the PC: memory cache, predictive build memory and builder counts
  (cores and RAM);
* from the quality target and the connection: what the zoom target needs to
  keep up at its busiest flight phase (the same estimate as the Download
  line of the target cost panel), against the last connection test of this
  imagery source, capped by your download limit.

| Setting | Planned from |
|---|---|
| Prefetch chunks per cycle | Up to three times what the target needs per second, never more than the line carries (at least 64) |
| Predictive build memory | One texture of the target per background builder, within a quarter of the RAM limit (only raised) |
| Parallel downloads ceiling | What the line (or the download limit) carries, with the connection test's speed per download, and half again to spare |
| Prefetch radius | 30 nm scaled by how much room the line has over the need: 15 nm on a tight line, up to 60 nm with plenty |
| Prefetch lookahead, prefetch lead time | 10 min and 60 s with room to spare; up to twice (lead 2.5x) as long as the line gets tight, so prefetch starts earlier |
| Wait for near imagery | 1.5x the time one full near texture of the target takes on the line, 6 to 12 s (only raised) |
| Wait while starting | 1.2x the time to load a new area, 60 to 120 s (only raised) |

Without a connection test only the first two are planned. Settings
(**Expert**) shows these values with the source *Target and line*. Auto-tuning
then fine-tunes from there in flight; a value you set yourself always wins.

### In flight

While streaming, AutoOrtho measures every 2 seconds how much of the nearby
scenery X-Plane gets at the zoom you asked for (**% sharp at target**),
along with download speed, errors, CPU, RAM and the flight phase. Every few
seconds it looks at the last 30 seconds, works out what holds the
sharpness back (the connection, the imagery server, the CPU, memory, too
short a wait, or prefetch falling behind) and can move one setting one
step. **Settings → Auto-Tuning** (`[governor] mode`, also on Essentials)
picks what it does with that:

| Mode | What happens |
|---|---|
| **Auto** *(default)* | Changes the setting while streaming, checks the result (after 10 to 20 seconds, up to a minute when too few nearby reads or a change of flight phase leave nothing to compare), and undoes the change if sharpness or the bottleneck got worse by more than chance explains. |
| **Advise** | Works out the same changes but applies none. Home shows the latest suggestion with **Apply**, which uses it at once and keeps it for the next flights as a learned value (not a pinned one): auto-tuning may suggest moving it again, and **Reset learned settings** clears it. |
| **Off** | Uses the configured values. Sharpness is still measured. |

It waits longer only on evidence. When a nearby read stops waiting,
AutoOrtho times how long that imagery still took to arrive. The wait is
raised only when most of it (80 % in flight, 90 % while a flight loads)
arrived within the longest wait allowed, and never beyond what it needed.
When most of it took longer, waiting would only hold X-Plane up, so the
state is *Imagery arrives late* and prefetch looks further ahead instead.

It tunes prefetch only on evidence too. AutoOrtho remembers, per tile, what
prefetch did for it: when it was queued, to which detail level, when its
imagery was in, and whether it later left the memory cache. When X-Plane
reads a nearby tile blurry in flight (not while a flight loads or after a
jump), AutoOrtho records why:

| What prefetch had done | State | What auto-tuning moves |
|---|---|---|
| Never queued it, and the tile lay off to the side | *Prefetch corridor too narrow* | Prefetch radius |
| Never queued it, though it lay ahead | *Prefetch corridor too short* | Prefetch lookahead |
| Never queued it, while prefetch was paused | *Prefetch paused* | Nothing; Home names the pause (low memory, X-Plane's own downloads) when it keeps happening |
| Never queued it, in a turn | — | Nothing (SimBrief routes cover planned turns) |
| Queued only a coarser level than X-Plane wanted | *Prefetch asks for too little detail* | Prefetch lead time (`[streaming] paging_lead_seconds`, 30-180 s) |
| Queued it, but it was not ready in time | *Prefetch starts too late* | Prefetch lead time, then prefetch chunks per cycle (not while the line is full) |
| It was ready, then left memory | *Memory cache too small* | Memory cache (only while AutoOrtho uses under 80 % of its RAM limit) |
| It was ready and in memory | — | Nothing (a failed download, for example) |

A setting moves only with at least 10 such blurry tiles in the last 30
seconds and one cause behind at least half of them; the change is kept when
that cause's share of nearby reads falls. When the scenery stays blurry with
no clear cause, the state is *Blurry, cause unclear*: nothing moves, and
after five minutes Home says so and points to the flight report.

The rules that keep it steady: one change at a time, a 30-second wait per
setting (two minutes after an undo), bigger steps while a flight is loading
and while the scenery is far below the target (under 85 % sharp), a
two-minute settling time before a change that fixed a bottleneck can be
eased back, and nothing above your limits. When live tiles queue for build
slots (the DDS buffers and streaming builders) rather than for admission,
auto-tuning raises **Live builders** (`live_builder_concurrency`) and resizes
those pools while you fly, within your CPU-core limit and a quarter of your
RAM limit for the buffers. Compute-bound means AutoOrtho's
own processes use what your CPU-core limit allows them (or the whole PC is
busy); X-Plane's CPU use alone does not count against AutoOrtho. A full
download queue raises the downloads ceiling only when the adaptive limit
has reached it; when the imagery server is setting the pace instead
(*Network-bound, server pacing downloads*), prefetch narrows to give the
nearby scenery what the server sends. Limits or settings you change while
streaming reach auto-tuning at once: a value you set is pinned from then
on, and new limits narrow the ranges it may use. Settings you set yourself are
**pinned** and never moved. It never lowers your zoom target: when every
setting it may move is at its limit for five minutes and sharpness stays
under 95 %, it shortens prefetch one step at a time (listed like any other
change, and eased back once the scenery is sharp again) and tells you what
limits it (for example
"the limit is your connection"), with links to the target and the limits.

**Settings you set yourself, in Auto mode.** A value you set by hand (now or
in an older config file) pins that setting: the tuning plan and auto-tuning
leave it as it is. While auto-tuning is on, Home lists the pinned settings
AutoOrtho would otherwise manage, with **Let AutoOrtho manage them** (back to
the automatic values, saved at once and taken over mid-flight) and
**Review** (the Expert table, showing only your own values). Dismiss the
notice to keep them; it comes back only when another setting gets pinned.
Pinning a setting on the Auto-Tuning page counts as keeping it. There is no
need to delete your config file.

The Auto-Tuning page lists the settings it may move, with the current
value, the range your limits allow, where the value comes from (*Baseline*,
*Learned*, *Governor* or *Pinned*), the last change and its reason, and a
**Pin** button. A timeline shows sharpness, Mbit/s, CPU and RAM for the
session; click a red marker to read that decision. **Export tuning report**
writes the same as JSON to `~/.autoortho-data/governor/`.

**Protect X-Plane's frame rate** (`[limits] protect_sim_fps`, on the
Limits page): when X-Plane's frames take 20 % longer than the best
30-second average of this flight and it runs under 40 fps, Auto mode
lowers background builders, lengthens the pause between background builds
and shortens prefetch. Such a change is kept as long as the scenery stays
at least 95 % sharp. The frame time comes from X-Plane's UDP data output,
like the flight data; without it the option does nothing.

**Smaller textures when graphics memory is short** (`[governor]
allow_texture_cap`, off by default, on the Auto-Tuning page). AutoOrtho
estimates the graphics memory of the photo textures X-Plane holds: each
texture at the finest detail X-Plane read in the last 20 minutes. The page's
status line shows it against the graphics memory budget (your limit, or the
card minus `xplane_vram_reserve_gb`). When it reaches 95 % of the budget and
you allowed it, Auto mode builds textures above 4096 px (ZL17 on ZL16
scenery, ZL19 near airports) at 4096 px for the rest of the session: a
quarter of the memory, with the detail of one zoom level lower. Your zoom
target is not changed, and textures X-Plane already holds keep their size.
Without the permission, Home tells you instead.

**Learning across flights.** In Auto mode, the settings that ended up away
from their planned values are saved at unmount in `~/.autoortho-data/governor/learned.json`, keyed by the
zoom target, the imagery source and a salted hash of your local network, so
the next flight with the same key starts there. Suggestions you applied in
Advise mode are kept in the same file and used in Advise and Auto. **Reset
learned settings** clears both.

**Flight verdict.** The performance report gains a *Flight verdict* section:
*Smooth flight*, *A few blurry moments* or *Often blurry*, sharpness by
phase (loading, taxi, departure and arrival, cruise), the number of changes
kept and undone, peak download speed, brake activations, what was
learned for next time, and a *Tuning plan* table: the target's need, your
line, and for each planned setting the planned value beside where
auto-tuning left it. Its *Blurry moments by cause* table gives, per phase,
the share of blurry nearby tiles by what prefetch had done for them (the
causes in the table above).

With `[governor] record_samples = True` (the default) each session's
samples are written to `~/.autoortho-data/governor/samples-*.jsonl`
(counters only, no positions). Developers can replay them offline with
`governor.load_samples()` and `governor.replay()` to check a change to the
rules against real flights.

## Performance Tuning

AutoOrtho includes advanced performance settings that allow you to balance image quality against loading times and stuttering. These settings are available under **Settings → Streaming**.

Key settings include:
- **Wait for nearby imagery / Wait while a flight loads** - How long X-Plane's reads of nearby textures wait for real imagery (the wait bands; distant textures never wait)
- **Fallback Level** - How aggressively to find replacement imagery for failed chunks
- **Spatial Prefetching** - Proactively download tiles ahead of your aircraft
- **Dynamic Zoom Levels** - Automatically adjust imagery quality based on altitude

For detailed configuration options and recommended settings for different use cases, see the [Performance Tuning Guide](performance.md).

### Provider transport and working memory

The shared provider transport uses asynchronous HTTP/2 requests. Network and
tile-build concurrency are bounded independently:

```ini
provider_max_in_flight = 320
provider_max_connections = 160
download_dispatch_workers = 4
provider_queue_timeout = 60
provider_adaptive_concurrency = True
provider_adaptive_controller_v2 = False
provider_origin_initial_concurrency = 128
provider_origin_min_concurrency = 64
provider_transport_trace = False
live_tile_admission = 16
tile_image_cache_mb = 96
cache_write_buffer_mb = 256
long_term_cache_write_buffer_mb = 128
native_partial_allow_incomplete = False
persist_partial_dds_cache = True
partial_cache_promote_startup_max_tiles = 0
```

- `provider_max_in_flight` is the strict global pending-request limit. A
  reserved slice remains available for live X-Plane requests.
- `provider_max_connections` bounds physical connections. HTTP/2 can multiplex
  multiple requests over each connection.
- `download_dispatch_workers` applies completed responses; it does not limit
  network concurrency.
- Adaptive concurrency raises a provider's limit after sustained successes and
  reduces it after overload responses or timeouts. Windowed v2 additionally
  requires utilized, backlogged windows before latency-based adjustments.
- `live_tile_admission` bounds expensive concurrent tile construction while
  allowing JPEG retrieval to continue independently. Only reads that may
  build a detailed level take a slot: X-Plane's header chunk (read for every
  texture it loads) and reads of the base level skip it, so they are never
  stuck behind nearby reads that wait for imagery.
- `tile_image_cache_mb` is a per-tile byte budget for temporary fallback RGBA
  images. Oversized ZL17 mipmap-zero images are compressed and released.
- Cache-write byte budgets prevent slow storage from retaining an unbounded
  number of downloaded JPEGs.
- `native_partial_allow_incomplete` trades strict row quality for shorter
  deadline stalls by using child-cropped lower-ZL imagery and `missing_color`.
- `persist_partial_dds_cache` stores only exact, checksummed compressed rows
  asynchronously for faster repeat loads. DDM v5 binds each row's immutable
  source manifest to the bytes that were compressed.

### Bounded prefetch pipeline

```ini
prefetch_pipeline_v2 = True
prefetch_lookahead = 10
prefetch_max_chunks = 64
prefetch_admission_burst = 64
prefetch_radius_nm = 30
background_builder_workers = 2
predictive_dds_memory_mb = 512
persist_partial_dds_cache = True
prefetch_quality_mode = prefer_target
prefetch_quality_grace_sec = 5
strict_target_deadline_sec = 120
partial_cache_promote_startup_max_tiles = 0
```

`prefetch_max_chunks` bounds work published per producer cycle, while
`prefetch_admission_burst` bounds one candidate's coordinator pass. Queue,
candidate, tile, JPEG, and predictive-build limits are derived independently
from provider and memory capacity. A lookahead of `0` permits unlimited route
discovery, while
retained candidates and admitted work remain bounded.

Quality modes are `responsive` (lower-resolution fallback after the normal
wait), `prefer_target` (a short exact-quality grace period), and
`strict_target` (no lower-ZL substitution in mipmap zero, with a hard deadline).
Rows are sealed on their first X-Plane read. Late target-ZL arrivals can improve
only a future cache generation, never the bytes or provenance already served.

### Performance Diagnostics

The Settings tab can create an end-of-flight performance report containing
X-Plane DDS read latency, cache/network/build stage distributions, the slowest
tile operations, and per-process memory/CPU timelines. Reports are written to
`~/.autoortho-data/reports` by default. See
[Flight Performance Reports](performance.md#flight-performance-reports) for
the metric reference and analysis workflow.

`checkpoint_interval_seconds` controls periodic atomic worker snapshots
(10 seconds by default). If a worker is force-stopped, the report recovers its
most recent stage histograms and gauges instead of retaining resource samples
only.

For troubleshooting missing tiles or stuttering issues, see the [FAQ](faq.md#missing-color-tiles).

## Native Pipeline Settings

AutoOrtho includes a high-performance native pipeline that significantly improves DDS texture building speed. These settings control the native components.

### Native Pipeline Threads (`native_pipeline_threads`)
- **Type:** Integer
- **Default:** 0 (auto-detect)
- **Config file:** `native_pipeline_threads = 0`

Controls how many CPU threads the native pipeline uses for parallel operations (cache I/O, JPEG decoding, DDS compression).

| Value | Behavior |
|-------|----------|
| **0** | Auto-detect CPU cores, use all available (recommended) |
| **1** | Single-threaded mode (lowest CPU usage, slowest builds) |
| **2-N** | Limit to N threads (balance performance vs other apps) |

**Recommendation:** Leave at 0 unless you need to limit CPU usage for other applications.

### Persistent Predictive DDS Cache (`persistent_dds_cache_mb`)
- **Type:** Integer (megabytes)
- **Default:** 0 (managed by the shared disk budget)
- **Config file:** `persistent_dds_cache_mb = 0`

Size of the cross-session cache for compiled DDS textures. The deprecated
`ephemeral_dds_cache_mb` key is accepted for compatibility but no longer
creates a separate cache.

Set this to `0` to remove the cache's independent DDS-only cap and delegate
retention to the shared `file_cache_size` budget. With disk-budget enforcement
enabled, complete DDS files, DDM metadata, and partial-row sidecars all count
toward the DDS percentage; JPEG source files receive the remaining percentage.
If disk-budget enforcement is disabled, `0` is genuinely unlimited.

| Value | Behavior |
|-------|----------|
| **0** | Use the shared disk budget and DDS allocation percentage |
| **1024-4096** | Fixed, bounded compiled-texture cache |
| **8192+** | Larger fixed cache for repeat long-haul routes |

**Why disk-only caching?**

AutoOrtho uses disk-only caching (no dedicated RAM cache) because:
- **SSD reads are fast enough:** Reading a pre-built DDS from disk takes ~1-2ms, which is negligible compared to the ~100-500ms build time it saves
- **OS file cache handles hot files:** Your operating system automatically keeps frequently accessed files in RAM
- **Simpler memory management:** No need to configure RAM limits - the OS handles it naturally
- **Cross-session:** Valid compiled textures eliminate repeat downloads and builds

**Key properties:**
- Uses atomic DDS and metadata sidecar files
- Validates provider/build metadata before reuse
- Enforces the configured DDS and JPEG disk budgets
- OS file cache keeps hot files in RAM automatically

**Recommendation:** Leave at 0 so the shared disk budget controls retention.

### Apple Maps Caveat

When using Apple Maps (`APPLE`) as your imagery source, downloads **always use the Python HTTP client** instead of the native libcurl client. This is because Apple Maps requires:
- Dynamic authentication tokens
- Token refresh on 403/410 errors
- Special header handling

Other imagery sources (BI, EOX, ARC, NAIP, USGS, FIREFLY, YNDX, GO2) use the faster native HTTP client when available.

See the [Performance Tuning Guide](performance.md#native-pipeline-architecture) for detailed architecture information.

## Dynamic Zoom Levels

AutoOrtho can automatically adjust imagery zoom levels based on your altitude Above Ground Level (AGL). This provides:
- Higher detail imagery when flying low
- Lower detail (faster loading) imagery at high altitudes
- Terrain-aware calculations — flying at 10,000ft MSL over 5,000ft mountains uses higher quality than 10,000ft over ocean

Configure quality steps in **Settings → Imagery → Altitude-Based Quality**.

See the [Performance Tuning Guide](performance.md#dynamic-zoom-levels) for detailed configuration.

## SimBrief Integration

AutoOrtho can integrate with your SimBrief account to use your flight plan data for:
- **Dynamic Zoom**: Use planned altitudes at waypoints instead of velocity predictions
- **Prefetching**: Download tiles along your actual flight path, prioritized by time-to-encounter using SimBrief's calculated flight times

To set up SimBrief integration:
1. Go to **Flight Plan & Map**
2. Enter your **SimBrief User ID**
3. Click **Fetch Flight Data** after filing your flight plan
4. Enable the toggle to use flight data for calculations
5. Optionally adjust the **Route Calculation Settings** that appear below the toggle

### Route Calculation Settings

When flight data is loaded and the "Use Flight Data" toggle is enabled, additional settings become available:

| Setting | Description |
|---------|-------------|
| **Route Consideration Radius** | How far (nm) to look for waypoints when calculating tile altitude. Larger values are more conservative. |
| **Route Deviation Threshold** | Maximum distance (nm) off-route before falling back to DataRef-based calculations. |
| **Route Prefetch Radius** | How far (nm) perpendicular to your route to prefetch tiles. The path is sampled uniformly along the route; this controls the width of coverage. |

> **ℹ Real-time Changes:** These settings take effect immediately when modified — no restart required. Use **Apply** to persist your values for future sessions.

## Storage safety

**Settings → Scenery** includes a configurable free-space safety margin. AutoOrtho
shows cache usage and available space, estimates temporary and final package
requirements, and blocks scenery installation when either destination is too
small or not writable.

See the [Performance Tuning Guide](performance.md#simbrief-integration) for detailed information and limitations.

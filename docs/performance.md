# Performance Tuning Guide

AutoOrtho includes advanced performance tuning options that allow you to balance image quality against loading times and in-flight stuttering. This guide explains each setting and provides recommendations for different use cases.

## Loading Architecture

AutoOrtho keeps the selected detail level, including ZL17, while reducing
startup latency through:

- one loopback-only HTTP/2 broker shared by all mounted regions;
- bounded global request concurrency and per-request cancellation;
- coalescing of identical imagery requests across workers;
- DSF- and trajectory-driven prefetch only after X-Plane reports a valid
  flight connection;
- a persistent compiled DDS cache for repeat routes; and
- lazy regional download/build services so inactive regions remain lightweight.

If the optional HTTP/2 dependencies cannot start, AutoOrtho reports the error
and falls back to the existing HTTP/1.1 request path.

## Download Concurrency

Broker-backed downloads are dispatched asynchronously: a single dispatcher
thread owns the broker client socket and keeps many requests in flight at once.
Downloader threads no longer block one-per-request, so real concurrency is set
by the broker budget rather than by the size of the worker pool.

```ini
[autoortho]
# Maximum imagery requests in flight against the broker at any moment.
provider_max_in_flight = 128
# Upstream HTTP/2 connections the broker keeps open per provider host.
provider_max_connections = 64
# Threads used to settle completed downloads (coordination only).
download_dispatch_workers = 4
# Queue watchdog; HTTP timeouts begin only after the broker sends STARTED.
provider_queue_timeout = 60
```

| Setting | Default | Range | Notes |
|---------|---------|-------|-------|
| `provider_max_in_flight` | `128` | 8-1024 | Strict cap. A reserved slice (25%) is held for live, X-Plane-visible tiles so prefetch and healing can never starve them. |
| `provider_max_connections` | `64` | 1-256 | Effective value is `min(provider_max_in_flight, provider_max_connections)`. This preserves throughput when a provider negotiates HTTP/1.1 rather than multiplexed HTTP/2. |
| `download_dispatch_workers` | `4` | 1-16 | Small fixed pool that finalises completed requests; increasing it does not increase download concurrency. |
| `provider_queue_timeout` | `60` | 5-600 seconds | Maximum broker/adaptive queue wait. It is separate from provider connect/read/pool timeouts. |

### Strict admission

The dispatch stage never falls back to a synchronous HTTP request just because
it is busy. When `provider_max_in_flight` is exhausted, work is parked in a
bounded, priority-ordered deferred queue and admitted as soon as a slot frees
up; live tiles jump ahead of queued prefetch work. If the deferred queue itself
is full, the chunk is quietly requeued onto the normal work queue rather than
downloaded inline, so the configured concurrency is a hard ceiling on real
provider load.

Because downloader threads only coordinate (they no longer block on the
network), the pool is sized by `download_dispatch_workers` whenever a broker is
configured. `fetch_threads` still sizes the pool when no broker is available.

### Adaptive per-provider concurrency

The broker measures every upstream request, so it enforces an additional
per-origin (`scheme://host`) limit on top of the global budget. The controller
is AIMD:

* **Additive increase** — after at least one full concurrency window of fast,
  successful responses (and never fewer than
  `provider_origin_success_threshold`) the limit grows by
  `provider_origin_increase_step`.
* **Multiplicative decrease** — a `429`, any `5xx`, or a timeout/connection
  error multiplies the limit by `provider_origin_decrease_factor` (clamped at
  `provider_origin_min_concurrency`) and starts a cooldown of
  `provider_origin_cooldown_seconds` during which further failures do not
  compound the reduction.
* `403` and `410` are treated as neutral: they are per-tile answers, not
  overload signals, so they neither raise nor lower the limit.
* Slow successful responses do not raise the limit, and ramp-up pauses during
  the overload cooldown. This prevents the controller from oscillating back
  into saturation immediately after reducing concurrency.

Requests that cannot get an origin permit are parked in the broker's per-origin
backlog and resumed in priority order, so a throttled origin never blocks
another one and low-volume origins keep their own floor
(`provider_origin_min_concurrency`).

After the first response identifies the negotiated protocol, HTTP/1.x origins
are capped at `provider_max_connections`. This prevents hundreds of broker tasks
from timing out while waiting for a smaller HTTP/1.1 connection pool. HTTP/2
origins may continue ramping toward `provider_max_in_flight`.

```ini
[autoortho]
# Adaptive per-origin concurrency (broker-side).
provider_adaptive_concurrency = True
# 0 => start at provider_max_connections and ramp toward the ceiling.
provider_origin_initial_concurrency = 0
provider_origin_min_concurrency = 2
# 0 => use provider_max_in_flight as the per-origin ceiling.
provider_origin_max_concurrency = 0
provider_origin_increase_step = 1
provider_origin_success_threshold = 8
provider_origin_decrease_factor = 0.5
provider_origin_cooldown_seconds = 5.0
```

| Setting | Default | Range | Notes |
|---------|---------|-------|-------|
| `provider_adaptive_concurrency` | `True` | bool | Disable to use a fixed per-origin limit equal to the ceiling. |
| `provider_origin_initial_concurrency` | `0` | 0-1024 | `0` starts at the effective connection budget, preventing an initial request flood while still ramping after successful responses. |
| `provider_origin_min_concurrency` | `2` | 1-256 | Floor the controller will never drop below. Also clamps the initial value upwards. |
| `provider_origin_max_concurrency` | `0` | 0-1024 | `0` means `provider_max_in_flight`. Always clamped to the global `max_concurrency`. |
| `provider_origin_increase_step` | `1` | 1-64 | Permits added per successful ramp step. |
| `provider_origin_success_threshold` | `8` | 1-4096 | Consecutive successes required before a ramp step. |
| `provider_origin_decrease_factor` | `0.5` | 0.1-0.95 | Multiplier applied on overload. |
| `provider_origin_cooldown_seconds` | `5.0` | 0-60 | Suppresses repeated cuts while a reduction settles. |

The three most useful expert controls—initial concurrency, decrease factor, and
cooldown—are available in **Settings → Performance Tuning → Provider Download
Transport → Advanced adaptive tuning**. The section is collapsed by default to
keep unsafe tuning away from normal users.

Current limits, active requests and throttle counts are exposed through the
broker stats endpoint (`HTTP2Broker.server_stats()`) and, when profiling is
enabled, as the `broker.origin_limit.<origin>` gauge plus
`broker.origin_limit_change` stage records.

### Transport

Broker replies are multipart: one MessagePack frame with the response metadata
and a second frame carrying the raw JPEG bytes. The image body is therefore
never copied into or out of a MessagePack buffer. Requests and control messages
remain single-frame, and clients still accept a legacy embedded body for
compatibility.

### Legacy settings

`max_concurrent_downloads` and `http2_max_connections` continue to work as
aliases for `provider_max_in_flight` and `provider_max_connections`. A legacy
key is only honoured when the matching modern key is still at its packaged
default, so an explicit `provider_max_in_flight` is never silently overridden by
an old `max_concurrent_downloads` value.

`fetch_threads` still sizes the downloader thread pool, but those threads now
only feed the asynchronous dispatch stage (and serve the direct HTTP/1.1
fallback path). It no longer caps how many downloads can be in flight.

## Quick Start: Performance Presets

For most users, the easiest way to configure performance is using the **Performance Preset** dropdown in Settings → Performance Tuning:

| Preset | Best For | Trade-off |
|--------|----------|-----------|
| **Fast** | Weak CPUs, slow internet, stutter-free experience | May have occasional missing/low-res tiles |
| **Balanced** | Most users | Good balance of quality and performance |
| **Quality** | Fast CPUs, fast internet, maximum image quality | May have longer loading times |
| **Custom** | Advanced users who want fine-grained control | Manual configuration required |

---

## Flight Performance Reports

AutoOrtho creates a profiling report for every mounted flight session. Profiling
starts immediately before the first scenery worker launches and finishes after
all scenery workers stop, so the report covers X-Plane-visible tile requests,
background prefetch work, and shutdown.

Reports are written to:

```text
~/.autoortho-data/reports/performance-<timestamp>-<session-id>/
├── report.md
├── report.json
└── process-<pid>-<role>.json
```

`report.md` is the human-readable starting point. `report.json` contains the
complete one-second resource timeline, latency histograms, slow-operation
records, final counters, relevant configuration, gauges, and individual process
profiles.

The **Diagnostics** destination lists these reports newest-first and loads the
selected report without blocking the application. Use **Open Report** for the
full file or **Open Folder** to inspect the accompanying JSON and per-process
profiles.

### What is measured

| Stage | What it identifies |
|-------|--------------------|
| `fuse.dds_read` | Total time from an X-Plane DDS read until bytes are returned |
| `fuse.tile_lock_wait` | Concurrent reads blocked behind work on the same tile |
| `tile.cache_lookup` | In-memory tile lookup/open overhead |
| `cache.dds_load` | Persistent compiled DDS disk read and decompression |
| `cache.jpeg_batch_read` | Source JPEG cache I/O |
| `chunk.queue_wait` | Time a download waits for a fetch worker |
| `network.http_request` | Map provider/network request latency |
| `tile.collect_chunks` | Cache collection plus waiting for missing chunks |
| `image.fallback_resolve` | Disk/mipmap fallback work for missing chunks |
| `image.compose` | Progressive image assembly and fallback processing |
| `dds.builder_pool_wait` | Wait for a streaming DDS builder |
| `dds.buffer_pool_wait` | Wait for a preallocated DDS output buffer |
| `dds.native_compute` | Native JPEG decode and DDS compression |
| `dds.python_compress` | Python-path DDS compression |
| `tile.mipmap_build` | Complete mipmap construction |

Stage durations are **inclusive**. Nested stages overlap, so totals must not be
added together. Start with `fuse.dds_read`, then use the nested stage with the
largest p95/max latency to locate the bottleneck.

Every AutoOrtho process samples RSS, USS, virtual memory, CPU, thread count, and
I/O once per second. On macOS, each worker also records physical footprint so
compressed/swapped native memory is visible. The report ranks processes by peak
physical footprint on macOS or RSS elsewhere, and shows retained RSS growth.
Native decode/buffer pools, tile counts,
open references, and queue depth are tracked as resource gauges. Long or
sub-second sessions use peak-preserving adaptive downsampling, keeping report
memory bounded while retaining spikes and full-session coverage.

### Configuration

The **Settings → Performance Diagnostics** section controls the normal profiling
options:

```ini
[diagnostics]
performance_profiling = True
sample_interval_seconds = 1.0
slow_operation_ms = 250.0
max_slow_operations = 200
python_allocation_tracing = False
report_dir = ~/.autoortho-data/reports
max_reports = 20
```

Normal profiling uses bounded histograms and slow-operation heaps to avoid
growing with the number of tile requests. `python_allocation_tracing` is
different: it adds file-and-line Python allocation growth, but materially
changes performance and cannot see native C image/DDS buffers. Enable it only
for a dedicated memory diagnostic flight, then disable it before comparing
normal latency.

### Reading a report

1. Check **Diagnostic flags** for queue saturation, network latency, pool
   contention, native build cost, or retained memory.
2. In **Memory by process**, identify the worker with the largest peak and RSS
   growth. Correlate its peak with tile/cache/pool gauges and the raw timeline.
3. In **Slowest stages**, compare p95 and max, not only averages. A low average
   with a very high p99 is a stutter source.
4. In **Slowest individual operations**, follow repeated tile IDs and outcomes
   to distinguish a single bad provider response from systemic contention.
5. Use `report.json` for plotting or comparing sessions after changing one
   tuning parameter.

---

## Detailed Settings Reference

### Zoom Level (Critical Performance Factor)

The **Max Zoom Level** setting has the most significant impact on loading times and resource usage. Understanding this is crucial for optimizing performance.

#### How Zoom Levels Work

Each zoom level doubles the resolution in both dimensions, meaning **each zoom level increase requires 4× the resources** of the previous level:

| Zoom Level | Chunks per Tile | Relative Resources | Typical Use |
|------------|-----------------|-------------------|-------------|
| ZL14 | 16 (4×4) | 1× (baseline) | Low detail, fast loading |
| ZL15 | 64 (8×8) | 4× | Medium detail |
| ZL16 | 256 (16×16) | 16× | High detail (default) |
| ZL17 | 1024 (32×32) | 64× | Very high detail |
| ZL18 | 4096 (64×64) | 256× | Maximum detail, slowest |

#### Impact on Performance

**Downloads:** At ZL17, you need to download 4× more image chunks than ZL16. At ZL18, that's 16× more than ZL16.

**Processing:** Each chunk must be decoded (JPEG→RGB) and compressed (RGB→DDS). More chunks = more CPU time.

**Memory:** Higher zoom levels require more RAM to hold all the image data during processing.

**Storage:** Cache size grows exponentially with zoom level.

#### Recommendations by System

| System Type | Recommended Max Zoom | Notes |
|-------------|---------------------|-------|
| Low-end / Slow internet | ZL15 | Fastest loading, acceptable quality |
| Mid-range | ZL16 | Good balance (default) |
| High-end / Fast internet | ZL17 | High quality, longer loading |
| Enthusiast | ZL18 | Maximum quality, expect long loads |

#### Example: Loading Time Impact

Assuming 50ms average per chunk download:

| Zoom Level | Chunks | Theoretical Min Time |
|------------|--------|---------------------|
| ZL15 | 64 | ~3.2 seconds |
| ZL16 | 256 | ~12.8 seconds |
| ZL17 | 1024 | ~51.2 seconds |
| ZL18 | 4096 | ~3.4 minutes |

**Note:** Actual times vary based on parallelism, caching, and network conditions. These are theoretical minimums to illustrate the exponential scaling.

#### Config File Setting

```ini
# Maximum zoom level for imagery (14-18)
# Higher = better quality but exponentially longer loading times
# Each level increase = 4× more resources needed
maptype_override_zoom = 16
```

**Tip:** If you're experiencing long loading times or frequent missing tiles, consider lowering your max zoom level before adjusting other settings.

---

### Time Budget System

The Time Budget system controls how long AutoOrtho spends loading each tile before returning a result to X-Plane.

#### Enable Time Budget (`use_time_budget`)
- **Type:** Boolean (True/False)
- **Default:** True
- **Config file:** `use_time_budget = True`

When enabled, AutoOrtho uses a wall-clock time limit for tile requests instead of the legacy per-chunk timeout system. This provides more predictable performance and reduces stuttering.

**Recommendation:** Keep enabled unless you experience issues.

---

#### Tile Time Budget (`tile_time_budget`)
- **Type:** Float (seconds)
- **Default:** 60
- **Range:** 60 - 600 seconds
- **Config file:** `tile_time_budget = 60.0`

The maximum wall-clock time for a **complete tile** across every block read and
mipmap request. A tile receives one shared budget when X-Plane first reads it;
subsequent reads do not restart the timer.

| Value | Use Case | Effect |
|-------|----------|--------|
| 60 - 120.0s | Fast/smooth experience | Quicker loading, but more partial tiles |
| 120.0 - 300.0s | Balanced | Good quality with reasonable loading times |
| 300 - 600.0s | Maximum quality | Complete tiles, but longer loading times |

**How it works:**
1. X-Plane requests a tile from AutoOrtho (tile enters processing queue)
2. **Budget timer starts** on the tile's first DDS read
3. AutoOrtho builds all mipmaps (4 → 3 → 2 → 1 → 0) sharing this budget
4. After `tile_time_budget` seconds of active processing, AutoOrtho builds the DDS with whatever is complete
5. Any incomplete areas use the configured `missing_color`

Queue and resource-pool waits count against the tile-wide deadline so a busy
system cannot multiply loading time through repeated reads.

**Note:** Each tile covers a large geographic area (approximately 1 square degree of latitude/longitude at zoom 16). Higher budgets allow more time for all chunks to download and process.

**Note:** The effectiveness of this setting will also depend on your configured max zoom level. Higher zoom levels with lower budget times will result in faster loading but lots of missing tiles.

---

### Fallback System

When chunks fail to download in time, the fallback system provides alternative imagery to prevent missing tiles.

#### Fallback Level (`fallback_level`)
- **Type:** String (none, cache, full)
- **Default:** cache
- **Config file:** `fallback_level = cache`

Controls which fallback mechanisms are enabled:

| Level | Fallback 1 (Disk Cache) | Fallback 2 (Scale Mipmaps) | Fallback 3 (Network) | Use Case |
|-------|------------------------|---------------------------|---------------------|----------|
| **none** | ❌ Disabled | ❌ Disabled | ❌ Disabled | Fastest, may have green/missing tiles |
| **cache** | ✅ Enabled | ✅ Enabled | ❌ Disabled | Balanced - uses cached data only |
| **full** | ✅ Enabled | ✅ Enabled | ✅ Enabled | Best quality - can download lower-detail alternatives |

**Fallback Chain Explained:**

1. **Fallback 1 - Disk Cache:** Searches your local cache for a lower-zoom version of the same imagery. Fast and free.

2. **Fallback 2 - Scale from Mipmaps:** Scales imagery from already-built lower-detail mipmap levels. Very fast.

3. **Fallback 3 - Network Download:** Downloads lower-detail imagery on-demand from the server. Slowest but provides the best fill-in quality.

**Recommendation:** Use `cache` for most users. Use `full` if you have fast internet and prefer complete imagery over speed.

---

#### Fallback Extends Budget (`fallback_extends_budget`)
- **Type:** Boolean (True/False)
- **Default:** False
- **Config file:** `fallback_extends_budget = False`

**Only applies when `fallback_level = full`.**

When enabled, network fallbacks (Fallback 3) will continue even after the tile time budget is exhausted. This prioritizes image quality over strict timing.

| Setting | Behavior | Effect |
|---------|----------|--------|
| **False** (default) | Fallbacks respect budget | Faster loading, may have some missing tiles |
| **True** | Fallbacks ignore budget | Better quality, may cause longer load times |

**When to enable:**
- You prioritize having complete imagery over loading speed
- You have a fast, reliable internet connection
- You don't mind occasional longer loading times

**When to keep disabled:**
- You want predictable, stutter-free performance
- Your internet is slow or unreliable
- Loading speed is more important than perfect imagery

---

#### Extended Fallback Timeout (`fallback_timeout`)
- **Type:** Float (seconds)
- **Default:** 30
- **Range:** 10 - 120 seconds
- **Config file:** `fallback_timeout = 30`

**Only applies when `fallback_extends_budget = True`.**

When extended fallbacks are enabled, this is one shared extension for the
entire tile. Multiple block reads and fallback levels cannot restart it.

| Value | Total Extra Time | Use Case |
|-------|------------------|----------|
| 10s | Up to 10 seconds | Fast - minimize extra wait |
| 30s | Up to 30 seconds | Balanced default |
| 60s | Up to 60 seconds | Quality on slower providers |
| 120s | Up to 120 seconds | Explicit maximum-quality choice |

**Example calculation:**
- `tile_time_budget = 60s`
- `fallback_timeout = 30s`
- **Total worst-case tile time:** 60s + 30s = **90 seconds**

**Recommendation:** Start with 30 seconds. Increase it only when complete
initial imagery is more important than a predictable loading deadline.

---

### Startup Loading Behavior

#### Suspend Maxwait (`suspend_maxwait`)
- **Type:** Boolean (True/False)
- **Default:** True
- **Config file:** `suspend_maxwait = True`

When enabled, AutoOrtho extends timeout values during initial scenery loading (before the first flight begins). This allows more time for downloads to complete during the initial load, resulting in fewer missing tiles when the flight starts.

**How it works:**
- AutoOrtho detects the "loading" phase by tracking if X-Plane has **ever** sent flight data via UDP
- During initial loading (before first connection), the tile time budget is multiplied by 10×
- Once the flight begins (X-Plane starts sending position data), normal timeouts resume permanently
- **Important:** Temporary disconnects (e.g., from stuttering) do NOT re-enable extended timeouts

| Phase | Time Budget Behavior |
|-------|---------------------|
| Initial loading (never connected) | `tile_time_budget × 10` |
| Flying (UDP connected) | `tile_time_budget` (normal) |
| Temporary disconnect (stutter) | `tile_time_budget` (normal - no penalty) |

**Stall Detection:**
AutoOrtho monitors download progress and will log warnings if downloads appear stalled:
- After 60 seconds with no successful downloads: `"Downloads appear slow..."` warning
- After 180 seconds with no successful downloads: `"⚠️ DOWNLOADS STALLED..."` warning

These warnings help identify server-side throttling vs client-side issues.

**Note:** If you experience very long loading times (>5 minutes stuck at "Reading scenery files"), this may indicate:
- Server throttling (especially with BI/Bing imagery during high-traffic periods)
- Network connectivity issues
- Very high zoom level with slow internet connection

**Troubleshooting Long Loading Times:**
1. Check AutoOrtho logs for "Downloads appear slow" or "DOWNLOADS STALLED" warnings - these indicate server throttling
2. Try a different imagery source (GO2, EOX) to rule out server-specific issues
3. Lower your max zoom level temporarily to reduce download volume
4. Ensure your file cache is enabled - subsequent flights load much faster
5. If issues persist, set `suspend_maxwait = False` for stricter timeouts during loading (may result in more missing tiles initially)

---

### Dynamic Zoom Levels

The Dynamic Zoom system automatically adjusts imagery quality based on your altitude Above Ground Level (AGL). This provides higher detail when flying low and saves resources at high altitudes where detail matters less.

#### Why AGL Instead of MSL?

AutoOrtho uses **AGL (Above Ground Level)** altitude from X-Plane's `y_agl` dataref rather than MSL (Mean Sea Level) pressure altitude. This provides more accurate terrain-aware imagery quality:

| Scenario | MSL Altitude | Terrain Elevation | AGL Altitude | Zoom Decision |
|----------|--------------|-------------------|--------------|---------------|
| Flying over ocean | 10,000ft | 0ft | 10,000ft AGL | Lower zoom OK |
| Flying over mountains | 10,000ft | 5,000ft | 5,000ft AGL | Higher zoom needed |
| Approaching mountain airport | 8,000ft | 7,000ft | 1,000ft AGL | Maximum zoom |

With AGL, you automatically get higher quality imagery when flying low over terrain, regardless of the terrain's MSL elevation.

#### Enable Dynamic Zoom (`max_zoom_mode`)
- **Type:** String (fixed, dynamic)
- **Default:** fixed
- **Config file:** `max_zoom_mode = fixed`

| Mode | Description |
|------|-------------|
| **fixed** | Use the same zoom level everywhere (traditional behavior) |
| **dynamic** | Automatically adjust zoom based on AGL altitude |

#### Configuring Quality Steps

Quality Steps define zoom levels for different altitude ranges. Each step specifies:
- **Altitude (AGL)**: The altitude threshold in feet above ground
- **Zoom Level**: The maximum zoom level for normal tiles
- **Airports Zoom Level**: The maximum zoom level near airports (can be higher for detail)

The inline editor under **Settings → Dynamic Zoom** shows the resulting altitude
ranges and preview chart, includes Airliner/General Aviation/Low VRAM presets,
and supports undo, redo, reset, and keyboard row removal. Changes remain
transactional until **Apply** is selected.

**Example configuration:**

| Altitude (AGL) | Normal ZL | Airports ZL | Use Case |
|----------------|-----------|-------------|----------|
| 0ft+ | ZL17 | ZL18 | On ground / very low |
| 5,000ft+ | ZL16 | ZL17 | Low altitude flight |
| 15,000ft+ | ZL15 | ZL16 | Medium altitude |
| 30,000ft+ | ZL14 | ZL15 | High altitude cruise |

**How altitude prediction works:**

When a tile is requested, AutoOrtho predicts your altitude when you'll be closest to that tile:

1. Gets your current position, heading, speed, and vertical speed
2. Calculates when you'll be closest to the tile
3. Predicts your AGL altitude at that time
4. Selects the appropriate zoom level for that predicted altitude

This means tiles ahead of you during a descent will load at higher detail than tiles behind you.

#### Fallback Behavior

When X-Plane DataRefs are not available (before flight starts, loading screens):
- Dynamic zoom falls back to the **base step** (0ft AGL quality level)
- This ensures tiles load at maximum configured quality during scenery loading

---

### Spatial Prefetching

The prefetching system proactively downloads tiles ahead of your aircraft to reduce in-flight stuttering.

#### Enable Prefetching (`prefetch_enabled`)
- **Type:** Boolean (True/False)
- **Default:** True
- **Config file:** `prefetch_enabled = True`

When enabled, AutoOrtho monitors your aircraft's position, heading, and speed to predict which tiles you'll need next and downloads them in advance.

Prefetch is intentionally disabled while X-Plane displays the initial
"Reading scenery files" screen. Live DDS requests receive all downloader and
builder capacity until the parent process publishes a valid flight connection.
After the flight starts, prefetch uses a separate bounded queue; live requests
remain unbounded and always take priority.

**How it works:**

*With SimBrief flight data loaded:*
1. AutoOrtho follows your actual flight plan, interpolating between waypoints
2. Uses SimBrief's calculated times (accounting for winds and climb/descent)
3. Prioritizes tiles by time-to-encounter (closest tiles first)
4. Downloads tiles uniformly along your entire route

*Without SimBrief (velocity-based):*
1. AutoOrtho tracks your aircraft's heading and ground speed
2. It calculates which tiles are in your flight path ahead
3. It downloads those tiles in the background before you reach them

**Prefetching with Dynamic Zoom:**

When Dynamic Zoom is enabled, the prefetcher uses your predicted AGL altitude at each prefetch location to determine the appropriate zoom level. This means:
- Tiles prefetched for a descent will be at higher zoom levels
- Tiles prefetched for cruise will be at lower zoom levels
- Each prefetched tile matches what you'll actually need when you get there

**Recommendation:** Keep enabled for the smoothest in-flight experience.

---

#### Lookahead Time (`prefetch_lookahead`)
- **Type:** Integer (minutes)
- **Default:** 10
- **Range:** 1 - 60 minutes
- **Config file:** `prefetch_lookahead = 10`

How far ahead (in minutes of flight time) to prefetch tiles.

| Value | At 150 kts | At 300 kts | At 500 kts | Use Case |
|-------|-----------|-----------|-----------|----------|
| 5 min | ~12nm | ~25nm | ~42nm | Conservative, less bandwidth |
| 10 min | ~25nm | ~50nm | ~83nm | Balanced (default) |
| 20 min | ~50nm | ~100nm | ~166nm | Longer flights, faster aircraft |
| 30 min | ~75nm | ~150nm | ~250nm | Cross-country flights |
| 60 min | ~150nm | ~300nm | ~500nm | Maximum prefetch coverage |

**Example:** At 300 knots ground speed with 10 minute lookahead:
- Distance: 300 kts × 10 min = 50 nautical miles ahead
- AutoOrtho will prefetch tiles up to 50nm in front of you

**Recommendation:** 
- Short flights / GA: 5-10 minutes
- Medium flights / Jets: 10-20 minutes  
- Long haul / Fast jets: 20-30 minutes

---

### Per-Chunk Timeout Settings

These settings work **alongside** the tile time budget to control individual chunk download behavior.

#### Per-Chunk Max Wait (`maxwait`)
- **Type:** Float (seconds)
- **Default:** 5.0
- **Range:** 0.1 - 10.0
- **Config file:** `maxwait = 5.0`
- **UI:** Settings → Performance → Per-chunk max wait

Maximum time to wait for a **single chunk** to download. This works in combination with the tile time budget:

- **Tile Time Budget:** Total time for the entire tile (all 256 chunks)
- **Per-Chunk Max Wait:** Maximum time for each individual chunk download

A chunk will stop waiting when **either** limit is reached, whichever comes first. This prevents a single slow chunk from consuming the entire tile budget.

**How it works:**
```
For each chunk:
  wait_time = min(remaining_tile_budget, maxwait)
  wait for chunk up to wait_time
```

**Recommended values:**
| Network Speed | Recommended `maxwait` |
|--------------|----------------------|
| Fast (fiber) | 2.0 seconds |
| Normal (cable/DSL) | 5.0 seconds (default) |
| Slow/unreliable | 10.0 seconds |

**When time budget is disabled:** This becomes the sole timeout mechanism — each chunk waits up to `maxwait` seconds independently, which can result in much longer total tile times (up to 256 × maxwait for a full tile).

#### Extended Loading at Startup (`suspend_maxwait`)
- **Type:** Boolean
- **Default:** True
- **Config file:** `suspend_maxwait = True`
- **UI:** Settings → Performance → "Allow extra loading time during startup"

When enabled, AutoOrtho uses significantly longer timeouts during X-Plane's initial scenery load (before the flight starts). This ensures tiles load at full quality before you begin flying.

**Startup behavior when enabled:**

| Setting | Normal Flight | During Startup |
|---------|--------------|----------------|
| Tile Time Budget | As configured | **10× the configured value** |
| Per-Chunk Max Wait | As configured | **20 seconds** |

**Example:** With `tile_time_budget = 60` and `maxwait = 5.0`:
- During startup: up to 600s tile budget when startup extension is enabled
- During flight: 60s tile budget, 5s per-chunk wait

**How startup is detected:** AutoOrtho considers you to be in "startup mode" until X-Plane's DataRef connection is established, which happens when the flight becomes active (after the "Reading new scenery files" splash screen).

**Trade-offs:**
- ✅ Better initial scenery quality (fewer blurry/missing tiles at flight start)
- ✅ Reduces low-resolution and placeholder tiles
- ⚠️ May increase initial scenery loading time

> **⚠️ Tip: Long X-Plane Loading Times?**  
> If you're experiencing significantly longer X-Plane loading times, try setting **"Allow extra loading time during startup"** to **Off**. This option can dramatically increase scenery loading times, especially when combined with higher zoom levels or slower internet connections. Disabling it will use the normal time budgets during startup, resulting in faster loads at the cost of potentially lower initial scenery quality.

---

## Recommended Configurations

### Stutter-Free Flying (Prioritize Performance)
```ini
use_time_budget = True
tile_time_budget = 120.0
fallback_level = cache
fallback_extends_budget = False
prefetch_enabled = True
prefetch_lookahead = 30
max_zoom_level = 16
```

### Maximum Quality (Prioritize Imagery)
```ini
use_time_budget = True
tile_time_budget = 300
fallback_level = full
fallback_extends_budget = True
prefetch_enabled = True
prefetch_lookahead = 60
max_zoom_level = 17
```

### Slow Internet Connection
```ini
use_time_budget = True
tile_time_budget = 180
fallback_level = cache
fallback_extends_budget = False
prefetch_enabled = True
prefetch_lookahead = 90
max_zoom_level = 16
```

### Weak CPU / Limited System
```ini
use_time_budget = True
tile_time_budget = 180
fallback_level = none
fallback_extends_budget = False
prefetch_enabled = False
max_zoom_level = 15
```

---

## Understanding the Statistics

AutoOrtho logs performance statistics that can help you tune your settings:

```
STATS: {'mm_count:0': 12, 'mm_count:1': 26, 'chunk_budget_skipped': 8782, 'chunk_missing_count': 394}
```

### Key Statistics

| Statistic | Meaning |
|-----------|---------|
| `mm_count:N` | Successful mipmap builds at level N (0=highest detail) |
| `chunk_budget_skipped` | Chunks skipped because time budget ran out |
| `chunk_missing_count` | Chunks that ended up with missing color (no fallback worked) |

**Healthy indicators:**
- High `mm_count:0` values = high-detail tiles completing successfully
- Low `chunk_missing_count` = fallbacks working well

**Warning signs:**
- High `chunk_budget_skipped` = many chunks timing out, increase `tile_time_budget`
- High `chunk_missing_count` = fallbacks not covering gaps, enable more fallbacks

### Breaking Down Tile Creation Time

Each tile creation has two main phases:

1. **Download + Compose** (typically 60-80% of time)
   - Downloading 64-4096 image chunks (depending on zoom level)
   - Decoding JPEG data
   - Compositing into a single image

2. **Compression** (typically 20-40% of time)
   - Converting RGBA to DDS format
   - Generating mipmaps

The stats show both the total time and compression-only time, letting you identify bottlenecks:
- If compression time is high → CPU-bound, consider lowering zoom level
- If download time is high → Network-bound, check internet speed

---

## Time-Based Exclusion

AutoOrtho includes a time-based exclusion feature that allows you to automatically disable AutoOrtho scenery during specific time ranges in the simulator. This is useful for night flying when satellite imagery provides little benefit.

### How It Works

When time exclusion is active:

1. AutoOrtho monitors the simulator's local time via the `sim/time/local_time_sec` dataref
2. During the exclusion period, DSF file reads are **redirected** to X-Plane's global scenery
3. X-Plane uses its default scenery (which often has better night lighting)
4. When the exclusion period ends, AutoOrtho scenery becomes available again

**Important:** DSF files are never hidden from X-Plane. X-Plane indexes DSF files at flight load time, so hiding them would cause missing terrain. Instead, AutoOrtho redirects reads to the corresponding global scenery DSF files, ensuring terrain data is always available.

### Safety Features

The time exclusion system includes important safety features:

- **Active DSF Protection:** DSF files that are currently in use by X-Plane will NOT be redirected, even if the exclusion period starts. This prevents crashes or graphical glitches.
- **Gradual Transition:** Only new DSF requests are redirected during exclusion. Previously loaded scenery continues to work until X-Plane naturally releases it.
- **Global Scenery Fallback:** When redirecting, AutoOrtho looks for the equivalent DSF in X-Plane's Global Scenery folder. If not found, the original AutoOrtho file is served.

### Configuration

#### Enable Time Exclusion (`enabled`)
- **Type:** Boolean (True/False)
- **Default:** False
- **Config file:** `[time_exclusion]` section, `enabled = True`

Enable or disable the time-based exclusion feature.

#### Start Time (`start_time`)
- **Type:** String (HH:MM format)
- **Default:** 22:00
- **Config file:** `start_time = 22:00`

The time when the exclusion period begins (24-hour format). For example, "22:00" for 10 PM.

#### End Time (`end_time`)
- **Type:** String (HH:MM format)
- **Default:** 06:00
- **Config file:** `end_time = 06:00`

The time when the exclusion period ends (24-hour format). For example, "06:00" for 6 AM.

#### Default to Exclusion (`default_to_exclusion`)
- **Type:** Boolean (True/False)
- **Default:** False
- **Config file:** `default_to_exclusion = False`

Controls behavior when sim time is not yet available (before flight starts):

| Setting | Behavior |
|---------|----------|
| **False** (default) | AutoOrtho works normally until sim time confirms exclusion |
| **True** | Assume exclusion is active until sim time proves otherwise |

**When to enable:**
- You want night flights to start with default scenery from the very beginning
- You don't want any AutoOrtho scenery loaded before sim time is available

**When to keep disabled:**
- You prefer AutoOrtho to work normally during loading screens
- You only want exclusion to apply when sim time is confirmed

### Decision Preservation During Scenery Reload

Once the simulator time becomes available and AutoOrtho determines the correct exclusion state, this decision is **preserved** during temporary disconnections such as when you trigger "Reload Scenery" in X-Plane.

**Why this matters:**

Without decision preservation, the following problem would occur:
1. You start a flight with "Default to Exclusion" enabled
2. Time exclusion activates initially (no sim time available yet)
3. Sim time becomes available (e.g., 15:00 / 3 PM) → exclusion deactivates correctly
4. You trigger "Reload Scenery" in X-Plane
5. During reload, sim time temporarily becomes unavailable
6. ❌ Without preservation: exclusion would incorrectly re-activate
7. ✅ With preservation: exclusion stays inactive (correct behavior)

**How it works:**

- When sim time is received, AutoOrtho records both the time and the exclusion decision
- If sim time becomes temporarily unavailable (during reload), the last decision is preserved
- The preserved decision is updated whenever new sim time data is received
- Normal time-based transitions still work (crossing into/out of exclusion hours)

**Limitations:**

| Limitation | Description |
|------------|-------------|
| **Persists until restart** | The preserved decision remains until AutoOrtho is fully restarted |
| **To reset behavior** | Quit and restart AutoOrtho to return to the `default_to_exclusion` initial behavior |
| **Updates on new time** | If sim time indicates a state change (e.g., crossing into night), it will update when time becomes available again |

> **💡 Tip:** If you notice the exclusion state is "stuck" after multiple scenery reloads, simply restart AutoOrtho to reset to the configured default behavior.

### Example Configuration

To disable AutoOrtho between 10 PM and 6 AM (night hours):

```ini
[time_exclusion]
enabled = True
start_time = 22:00
end_time = 06:00
default_to_exclusion = False
```

To ensure exclusion is active from the moment AutoOrtho starts (before sim time is available):

```ini
[time_exclusion]
enabled = True
start_time = 22:00
end_time = 06:00
default_to_exclusion = True
```

### Overnight Ranges

The system correctly handles overnight time ranges. For example, if you set:
- Start: 22:00 (10 PM)
- End: 06:00 (6 AM)

AutoOrtho will be disabled from 10 PM until 6 AM the next morning.

### UI Configuration

You can configure time exclusion in the AutoOrtho Settings tab:

1. Go to **Settings** tab
2. Find the **Time Exclusion Settings** group
3. Check **Enable time-based exclusion**
4. Set the **Start time** and **End time** in HH:MM format

### Use Cases

- **Night Flying:** Satellite imagery is often dark or less useful at night. Default X-Plane scenery may have better night lighting.
- **Performance Optimization:** Reduce network usage and CPU load during night hours when visual quality matters less.
- **Dawn/Dusk Flying:** Exclude twilight hours when satellite imagery transitions may look unrealistic.

---

---

## SimBrief Integration

AutoOrtho can integrate with SimBrief to enhance Dynamic Zoom and Prefetching using your actual flight plan data. This provides more accurate predictions than velocity-based calculations alone.

### Setting Up SimBrief Integration

1. Go to **Settings** → **Setup** tab
2. Find the **SimBrief Integration** section
3. Enter your **SimBrief User ID** (found in your SimBrief account settings)
4. Click **Fetch Flight Data** after filing your flight plan in SimBrief
5. Enable **Use Flight Data for Dynamic Zoom Level and Pre-fetching Calculations**

> **Note:** You can load SimBrief flight data at any time — before or after clicking **Start Streaming**. The toggle takes effect immediately, so you don't need to restart AutoOrtho or save the config when loading a flight plan mid-session.

### How It Works

When SimBrief integration is enabled and flight data is loaded:

#### Dynamic Zoom with SimBrief

Instead of predicting altitude based on current vertical speed, AutoOrtho uses your planned altitudes from SimBrief:

1. For each tile, finds the closest waypoint(s) in your flight plan
2. Uses the planned AGL altitude at those waypoints
3. If multiple waypoints are within the consideration radius (default 50nm), uses the **lowest AGL altitude** for conservative quality

**Conservative AGL Calculation:**
- Uses the **lowest** flight altitude (MSL) among nearby waypoints — accounts for descents
- Uses the **highest** ground elevation — accounts for mountains
- AGL = lowest_MSL - highest_ground = most conservative (highest quality) result

#### Prefetching with SimBrief

Instead of prefetching based on velocity vector prediction, AutoOrtho follows your actual flight path with time-based prioritization:

1. **Projects your position onto the route** — finds exactly where you are on the flight plan
2. **Calculates your "current time"** — interpolates between waypoint times based on position
3. **Walks forward along the entire path** — interpolating between waypoints at regular intervals (not just around waypoints)
4. **Uses SimBrief's planned times** — accounts for winds, climb/descent speeds, and holds
5. **Calculates time-to-encounter** for each point along the path
6. **Prioritizes tiles by time** — tiles you'll reach sooner are prefetched first
7. **Stops at the configured lookahead time** — doesn't waste bandwidth on distant tiles

**Key advantage:** The path is uniformly sampled between waypoints, so even long oceanic legs or direct routes with distant waypoints get full coverage. Tiles are downloaded in the order you'll actually encounter them.

**Example:** If your next waypoint is 200nm away but you'll pass near a tile in 5 minutes, that tile is prefetched before tiles near the waypoint itself.

### Configuration Options

These settings are available in **Settings** → **Setup** → **SimBrief Integration** when flight data is loaded and the "Use Flight Data" toggle is enabled.

| Setting | Default | Range | Description |
|---------|---------|-------|-------------|
| Route Consideration Radius | 50 nm | 10-200 nm | Radius around a tile to consider waypoints for altitude calculation. Uses the lowest altitude among nearby waypoints for conservative zoom level selection. |
| Route Deviation Threshold | 40 nm | 5-100 nm | Maximum distance off-route before falling back to DataRef-based calculations. Accounts for ATC vectors or weather avoidance. |
| Route Prefetch Radius | 40 nm | 10-150 nm | Radius around path points for pre-fetching tiles. Larger values prefetch more tiles perpendicular to your route. |

> **ℹ Real-time Changes:** All route settings take effect immediately when modified — no restart required. However, use **Apply** to persist your values for future AutoOrtho sessions.

### Fallback Behavior

SimBrief integration gracefully falls back to DataRef-based calculations when:

1. **No flight data loaded**: Use velocity-based prediction from X-Plane DataRefs
2. **Aircraft off-route**: If you deviate more than 40nm from the planned route, AutoOrtho assumes you're no longer following the plan and falls back to DataRef calculations
3. **DataRefs unavailable**: If X-Plane isn't sending data yet, uses the base quality step (0ft AGL = maximum quality)

### Example Workflows

#### Option A: Load flight plan before starting

1. **File flight plan** in SimBrief (e.g., KJFK → KLAX)
2. **Start AutoOrtho** and go to **Flight Plan & Map**
3. **Enter SimBrief User ID** and click "Fetch Flight Data"
4. **Verify flight info** displays correctly (route, cruise altitude, aircraft)
5. **Enable toggle** "Use Flight Data for Dynamic Zoom..."
6. Click **Start Streaming** — AutoOrtho starts and begins prefetching along your route
7. **Start X-Plane** and fly your route

#### Option B: Load flight plan after starting (mid-session)

1. **Start AutoOrtho** and click **Start Streaming** (with your SimBrief User ID already saved)
2. **Start X-Plane** and begin your flight
3. **File flight plan** in SimBrief when ready
4. Go to **Flight Plan & Map** and click **Fetch Flight Data**
5. **Enable toggle** — takes effect immediately, no restart needed
6. AutoOrtho will immediately start using your flight plan for prefetching and dynamic zoom

---

## Known Limitations

### Zoom Level Transitions

When flying through areas where the zoom level changes (e.g., from ZL16 to ZL15 during climb), you may notice:

- **Hard visual transitions**: Adjacent tiles at different zoom levels may have visible seams due to:
  - Different imagery capture dates
  - Different color processing/calibration
  - Resolution differences at tile boundaries

- **Color mismatches**: Satellite imagery from different zoom levels is often captured at different times, so colors, seasons, and lighting may not match perfectly.

**Mitigation strategies:**
- Configure fewer, larger altitude steps to reduce the number of transitions
- Accept some quality variation for the performance benefits
- Use similar zoom levels for adjacent altitude ranges (e.g., ZL16/ZL16 instead of ZL16/ZL14)

### SimBrief Integration Limitations

| Limitation | Description |
|------------|-------------|
| **Single flight plan** | Only the most recently fetched SimBrief flight plan is used. Multi-leg flights require re-fetching between legs. |
| **Static ground heights** | Ground elevation data comes from SimBrief's database, which may differ slightly from X-Plane's terrain. |
| **Route deviation detection** | The 40nm off-route threshold is a straight-line distance, not a cross-track distance. Complex routes near waypoints may trigger false positives. |
| **No automatic refresh** | Flight data is fetched once when you click the button. Changes to your SimBrief flight plan require manual re-fetch. |
| **Holding patterns** | SimBrief fixes don't include holding patterns. If you hold, AutoOrtho may use incorrect altitude predictions. |

### DataRef-Based Prediction Limitations

When not using SimBrief (or when off-route), altitude prediction uses X-Plane DataRefs:

| Limitation | Description |
|------------|-------------|
| **Vertical speed extrapolation** | Assumes current vertical speed will continue, which may not be accurate for complex climb/descent profiles. |
| **60-second averaging window** | Predictions are based on a 60-second rolling average, so rapid changes in flight path take time to reflect. |
| **No terrain awareness** | DataRef-based predictions use current AGL but don't know about upcoming terrain changes. |

### Prefetching Limitations

| Limitation | Description |
|------------|-------------|
| **Priority system** | Prefetched tiles are always lower priority than tiles X-Plane directly requests. During rapid maneuvering, prefetching pauses. |
| **Network-dependent** | Prefetching requires available network bandwidth. On slow connections, prefetching may not keep up with fast aircraft. |
| **Cache eviction** | Prefetched tiles can be evicted from cache if memory limits are reached before you reach those tiles. |

### General Limitations

| Limitation | Description |
|------------|-------------|
| **Imagery availability** | Not all zoom levels are available in all areas. Some regions only have imagery up to ZL15 or ZL16. |
| **Server-side rate limiting** | Excessive requests may be throttled by imagery providers, affecting both real-time and prefetch downloads. |
| **Memory usage** | Higher zoom levels and extensive prefetching increase memory usage. Monitor system RAM on limited systems. |

---

## Native Pipeline Architecture

AutoOrtho includes a high-performance native pipeline (`aopipeline`) written in C that bypasses Python's Global Interpreter Lock (GIL) for CPU-intensive operations. This provides **10-20x faster DDS texture building** compared to the Python-only path.

### Why Native Code?

Python's GIL (Global Interpreter Lock) prevents true multi-threading for CPU-bound work. Even with multiple Python threads, only one can execute Python bytecode at a time. This caused stutters when multiple DDS textures needed to be built simultaneously.

The native pipeline solves this by:
1. Moving CPU-intensive work entirely to C code
2. Using **OpenMP** for true parallel execution across all CPU cores
3. Calling into Python only for orchestration, not computation

### Components

The native pipeline consists of four modules:

| Module | Purpose | Parallelism |
|--------|---------|-------------|
| **AoCache** | Batch file I/O for cached JPEGs | OpenMP parallel reads |
| **AoDecode** | JPEG decoding via TurboJPEG | OpenMP parallel decodes |
| **AoDDS** | DDS texture building with ISPC compression | OpenMP parallel compression |
| **AoHttp** | HTTP downloads via libcurl | Connection pooling, HTTP/2 |

### How It Works

When X-Plane requests a tile, AutoOrtho's native pipeline:

1. **Batch reads** all cached JPEG chunks in parallel (256 files for ZL16)
2. **Batch decodes** all JPEGs using thread-local TurboJPEG handles
3. **Composes** the full tile image using SIMD-optimized operations
4. **Compresses** each DDS mipmap level in parallel using ISPC
5. Returns the complete DDS to Python for serving to X-Plane

All steps happen in native C threads, completely bypassing the Python GIL.

### Performance Impact

| Metric | Python-Only | Native Pipeline | Improvement |
|--------|-------------|-----------------|-------------|
| Cache read (256 files) | 500ms | 50ms | **10x** |
| JPEG decode (256 chunks) | 800ms | 100ms | **8x** |
| DDS compression | 1000ms | 80ms | **12x** |
| **Total tile build** | **2.5s** | **~260ms** | **~10x** |

### Configuration

```ini
[autoortho]
# Maximum threads per native build (0 = auto, divided across concurrent builds)
# Set to 1 for single-threaded mode (lowest CPU, slowest builds)
native_pipeline_threads = 0

# Early-build threshold: start DDS build when this fraction of chunks are ready (0.5-1.0)
# 1.0 = wait for all chunks (single build, no placeholders)
# 0.9 = build at 90%, automatically rebuild when remaining 10% arrive
live_aopipeline_min_chunk_ratio = 1.0

# Persistent compiled DDS cache size in MB.
# 0 delegates retention to the shared disk budget.
persistent_dds_cache_mb = 0
```

#### Thread Configuration

| Value | Behavior |
|-------|----------|
| **0** (default) | Auto: divides CPU cores across concurrent builds to avoid oversubscription |
| **1** | Single-threaded (useful for debugging or very low-end CPUs) |
| **N** | Fixed N threads per build (regardless of concurrent build count) |

#### Early-Build Threshold (`live_aopipeline_min_chunk_ratio`)

Controls a **two-phase build strategy** that trades brief placeholder artifacts for faster first texture appearance:

**Phase 1 — Early build**: fires as soon as this ratio of chunks is available. Any missing chunks are filled with the missing color.

**Phase 2 — Healing pass**: fires automatically when the remaining chunks arrive, replacing placeholder areas with real imagery.

| Value | Behavior |
|-------|----------|
| **1.0** (default) | Wait for all chunks — single build, no placeholders, no healing needed |
| **0.9** | Build at 90% — texture appears ~10% sooner, healed silently after |
| **0.8** | Build at 80% — texture appears earlier, more placeholder area to heal |

At `1.0` there is no behavioral difference from before — the tile only builds once all chunks are present. Values below `1.0` are useful on slow connections where waiting for the last few chunks causes noticeable delay before a tile becomes visible.

### Ephemeral DDS Cache

The native pipeline includes an **ephemeral disk cache** for pre-built DDS textures:

- **Memory tier**: Fast access for recently used tiles (configurable, default 512MB)
- **Disk tier**: Overflow storage in temp directory (configurable, default 4GB)
- **Auto-cleanup**: Disk cache is deleted when AutoOrtho exits

This hybrid approach provides:
- ✅ Large cache capacity without permanent disk usage
- ✅ Fresh tiles every session (no stale/corrupted cache)
- ✅ Settings changes take effect immediately (no cache invalidation needed)

### Native HTTP Client

The native HTTP client uses **libcurl's multi-interface** for high-performance chunk downloads:

| Feature | Benefit |
|---------|---------|
| **Connection pooling** | Reuses TCP connections across requests |
| **HTTP/2 multiplexing** | Multiple requests over single connection |
| **Batch processing** | Amortizes Python overhead across 64 chunks |
| **Parallel downloads** | True concurrent I/O, not GIL-limited |

This is especially impactful during **initial loading** when 100,000+ chunk requests are queued.

### Important Caveats

#### Apple Maps Fallback

**Apple Maps (`APPLE` imagery source) uses the shared Python HTTP/2 broker**,
not the native libcurl client. This preserves:

1. **Dynamic token**: Apple requires a session-specific access token obtained via DuckDuckGo proxy
2. **Token rotation**: On 403/410 errors, the token must be refreshed and the request retried
3. **Complex logic**: The Python path handles all this special authentication flow

**Impact**: Apple Maps downloads may be slightly slower than other sources, but all retry logic and token handling works correctly.

```
# Native HTTP path (fast):
BI, EOX, ARC, NAIP, USGS, FIREFLY, YNDX, GO2 → libcurl → parallel downloads

# Shared broker path (full features):
APPLE → asynchronous HTTP/2 broker → token rotation on 403/410
```

#### Platform Support

The native pipeline requires compiled libraries for each platform:

| Platform | Library | Status |
|----------|---------|--------|
| macOS (ARM64) | `libaopipeline.dylib` | ✅ Included |
| macOS (x86_64) | `libaopipeline.dylib` | Build from source |
| Linux (x64) | `libaopipeline.so` | Build from source |
| Windows (x64) | `aopipeline.dll` | Build from source |

If the native library is not available for your platform, AutoOrtho automatically falls back to the Python implementation.

#### Fallback Behavior

The native pipeline gracefully falls back to Python when:
- Native library is not available or fails to load
- Apple Maps source is used (token handling)
- Transient HTTP errors need sophisticated retry logic
- Chunk downloads fail and need server rotation

You'll see log messages indicating which path is used:
```
INFO: Using NativeChunkGetter (32 connections)
INFO: Native HTTP client available: 1.0.0
```

Or for fallback:
```
INFO: Using Python ChunkGetter (32 workers)
DEBUG: Native HTTP client library not available, using Python requests fallback
```

---

## Troubleshooting

### Long X-Plane Loading Times

If X-Plane takes significantly longer to load scenery with AutoOrtho enabled, the most common cause is the **"Allow extra loading time during startup"** setting (`suspend_maxwait`). This option extends timeout values by 10× during initial scenery loading, which can add substantial time to X-Plane's startup.

**To reduce loading times:**

1. Go to **Settings** → **Performance**
2. Set **"Allow extra loading time during startup"** to **Off**
3. This will use normal time budgets during startup, resulting in faster loads

**Note:** X-Plane does not automatically refresh a texture after consuming it.
Choose the startup budget and fallback policy according to the quality required
for the initial load; background healing improves only future cache reads.

### Other Common Issues

See the [FAQ](faq.md#missing-color-tiles) for common issues related to:
- Missing color (green) tiles
- Long loading times
- In-flight stuttering

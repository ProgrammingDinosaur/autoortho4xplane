# Configuration

Setup and configuration for most use cases should be pretty simple. The important settings are:

* Scenery install path
* X-Plane install path
* Downloader directory

## First-run setup

On a new installation, AutoOrtho opens a guided setup wizard that:

1. Detects or asks for the X-Plane installation.
2. Validates scenery, cache, archive, and temporary-download folders.
3. Checks the required FUSE backend for the current platform.
4. Lets you select scenery regions and reviews disk-space requirements.

Existing installations with valid paths and dependencies are detected
automatically and are not forced through the wizard. The wizard remains
available from the header overflow menu or **Settings → Paths & Storage**.
If setup is interrupted, reopen the wizard from that menu; unaccepted wizard
changes are discarded and the last applied configuration remains active.

## Application navigation

The application is organized around five primary destinations:

- **Home** — runtime, readiness, X-Plane, cache, task, and failure summaries.
- **Scenery Library** — scenery search, filters, installation, updates, and
  package details.
- **Flight Plan & Map** — SimBrief controls, loaded-flight details, and the
  local Custom Map Editor.
- **Settings** — searchable categories, presets, exact numeric
  inputs, and Apply/Revert.
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

## Performance Tuning

AutoOrtho includes advanced performance settings that allow you to balance image quality against loading times and stuttering. These settings are available under **Settings → Performance** and **Settings → Prefetching**.

Key settings include:
- **Tile Time Budget** - Maximum time to wait for a tile before returning results
- **Fallback Level** - How aggressively to find replacement imagery for failed chunks
- **Spatial Prefetching** - Proactively download tiles ahead of your aircraft
- **Dynamic Zoom Levels** - Automatically adjust imagery quality based on altitude

For detailed configuration options and recommended settings for different use cases, see the [Performance Tuning Guide](performance.md).

### Provider transport and working memory

The shared provider transport uses asynchronous HTTP/2 requests. Network and
tile-build concurrency are bounded independently:

```ini
provider_max_in_flight = 128
provider_max_connections = 64
download_dispatch_workers = 4
provider_queue_timeout = 60
provider_adaptive_concurrency = True
live_tile_admission = 16
tile_image_cache_mb = 96
cache_write_buffer_mb = 256
long_term_cache_write_buffer_mb = 128
native_partial_allow_incomplete = False
persist_partial_dds_cache = False
```

- `provider_max_in_flight` is the strict global pending-request limit. A
  reserved slice remains available for live X-Plane requests.
- `provider_max_connections` bounds physical connections. HTTP/2 can multiplex
  multiple requests over each connection.
- `download_dispatch_workers` applies completed responses; it does not limit
  network concurrency.
- Adaptive concurrency raises a provider's limit after sustained successes and
  reduces it after overload responses or timeouts.
- `live_tile_admission` bounds expensive concurrent tile construction while
  allowing JPEG retrieval to continue independently.
- `tile_image_cache_mb` is a per-tile byte budget for temporary fallback RGBA
  images. Oversized ZL17 mipmap-zero images are compressed and released.
- Cache-write byte budgets prevent slow storage from retaining an unbounded
  number of downloaded JPEGs.
- `native_partial_allow_incomplete` trades strict row quality for shorter
  deadline stalls by using child-cropped lower-ZL imagery and `missing_color`.
- `persist_partial_dds_cache` stores completed compressed rows asynchronously
  for faster repeat loads. It uses additional space inside the DDS disk budget.

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

Configure quality steps in **Settings → Dynamic Zoom**.

See the [Performance Tuning Guide](performance.md#dynamic-zoom-levels) for detailed configuration.

## SimBrief Integration

AutoOrtho can integrate with your SimBrief account to use your flight plan data for:
- **Dynamic Zoom**: Use planned altitudes at waypoints instead of velocity predictions
- **Prefetching**: Download tiles along your actual flight path, prioritized by time-to-encounter using SimBrief's calculated flight times

To set up SimBrief integration:
1. Go to **Settings** → **Setup** tab
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

**Settings → Paths & Storage** includes a configurable free-space safety margin. AutoOrtho
shows cache usage and available space, estimates temporary and final package
requirements, and blocks scenery installation when either destination is too
small or not writable.

See the [Performance Tuning Guide](performance.md#simbrief-integration) for detailed information and limitations.

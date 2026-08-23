# Configuration

Setup and configuration for most use cases should be pretty simple. The important settings are:

* Scenery install path
* X-Plane install path
* Downloader directory

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

AutoOrtho includes advanced performance settings that allow you to balance image quality against loading times and stuttering. These settings are available in the Settings tab under "Performance Tuning".

Key settings include:
- **Tile Time Budget** - Maximum time to wait for a tile before returning results
- **Fallback Level** - How aggressively to find replacement imagery for failed chunks
- **Spatial Prefetching** - Proactively download tiles ahead of your aircraft
- **Dynamic Zoom Levels** - Automatically adjust imagery quality based on altitude

For detailed configuration options and recommended settings for different use cases, see the [Performance Tuning Guide](performance.md).

### Performance Diagnostics

The Settings tab can create an end-of-flight performance report containing
X-Plane DDS read latency, cache/network/build stage distributions, the slowest
tile operations, and per-process memory/CPU timelines. Reports are written to
`~/.autoortho-data/reports` by default. See
[Flight Performance Reports](performance.md#flight-performance-reports) for
the metric reference and analysis workflow.

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

### Predictive DDS Disk Cache (`ephemeral_dds_cache_mb`)
- **Type:** Integer (megabytes)
- **Default:** 4096
- **Range:** 1024 - 16384
- **Config file:** `ephemeral_dds_cache_mb = 4096`

Size of the disk cache for pre-built DDS textures. The predictive DDS system builds tiles in the background and stores them on disk for instant serving when X-Plane requests them.

| Value | Behavior |
|-------|----------|
| **1024-4096** | Balanced - good for most systems |
| **8192** | Large flights - covers more waypoints |
| **16384** | Maximum capacity - long-haul flights |

**Why disk-only caching?**

AutoOrtho uses disk-only caching (no dedicated RAM cache) because:
- **SSD reads are fast enough:** Reading a pre-built DDS from disk takes ~1-2ms, which is negligible compared to the ~100-500ms build time it saves
- **OS file cache handles hot files:** Your operating system automatically keeps frequently accessed files in RAM
- **Simpler memory management:** No need to configure RAM limits - the OS handles it naturally
- **Session-based:** Cache is fresh every session, avoiding stale texture issues

**Key properties:**
- Uses OS temp directory (not your JPEG cache)
- Automatically cleaned when AutoOrtho exits
- Fresh cache every session (no stale tiles)
- OS file cache keeps hot files in RAM automatically

**Recommendation:** 4096MB for most users. Increase to 8192-16384MB for long-haul flights covering many tiles.

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

Configure quality steps in **Settings** → **Imagery** → **Dynamic Zoom Mode**.

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

> **ℹ Real-time Changes:** These settings take effect immediately when modified — no restart required. Use **Save Config** to persist your values for future sessions.

See the [Performance Tuning Guide](performance.md#simbrief-integration) for detailed information and limitations.

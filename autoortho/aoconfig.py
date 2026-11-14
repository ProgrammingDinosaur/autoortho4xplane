#!/usr/bin/env python3

import os
import ast
import pprint
import configparser
from types import SimpleNamespace

# Handle imports for both package and direct Python execution.
if __package__:
    from autoortho import hardware
    from autoortho import limits as limits_module
    from autoortho.utils.constants import system_type
    from autoortho.worker_modes import is_mount_worker_mode
    from autoortho.settings_registry import (
        Tier,
        build_registry,
        parse_value,
    )
else:
    import hardware
    import limits as limits_module
    from utils.constants import system_type
    from worker_modes import is_mount_worker_mode
    from settings_registry import Tier, build_registry, parse_value

import logging
log = logging.getLogger(__name__)

class SectionParser(object):
    # Note: '0' and '1' are NOT treated as booleans to avoid corrupting numeric settings
    # Settings like native_pipeline_threads=0 should stay as integers, not become False
    true = ['true', 'yes', 'on']
    false = ['false', 'no', 'off']

    def __init__(self, /, **kwargs):
        # Lists first, then booleans (text only), integers and floats; see
        # settings_registry.parse_value.
        for k, v in kwargs.items():
            self.__dict__[k] = parse_value(v)

    def __repr__(self):
        items = (f"{k}={v!r}" for k, v in self.__dict__.items())
        return "{}({})".format(type(self).__name__, ", ".join(items))

    def __eq__(self, other):
        if isinstance(self, SimpleNamespace) and isinstance(other, SimpleNamespace):
           return self.__dict__ == other.__dict__
        return NotImplemented


# Bump when a release changes a default that existing configs must pick up,
# and add the step to _migrate_config_revision.
CONFIG_REVISION = 2

# From this revision the file holds only the settings the user changed; every
# other setting is a commented "# key = default" line. Active lines in such a
# file are the user's explicit choices, even when equal to the default.
SPARSE_REVISION = 2

# Shown at the top of [general] in the written file. Comment lines are read as
# option keys (comment_prefixes='/') and dropped on load, so text before the
# first section header would break parsing.
_FILE_NOTE = (
    "# Only the settings you changed are active. A line such as",
    "# \"# max_zoom = 16\" is a default in use: delete the \"# \" and change",
    "# the value to override it.",
)

_MISSING = object()


def _normalized(section, value):
    """A value in comparable form: parsed, and paths in canonical form."""
    parsed = parse_value(value)
    if section == "paths" and isinstance(parsed, str) and parsed:
        return os.path.normcase(os.path.normpath(os.path.expanduser(parsed)))
    return parsed


def _same(a, b):
    """Equal values of the same kind (True is not the integer 1 here)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _new_parser():
    return configparser.ConfigParser(
        strict=False,
        allow_no_value=True,
        comment_prefixes='/',
    )


def _read_config_file(path):
    """Read a config file as UTF-8, falling back to the locale encoding.

    Releases before the sparse format wrote the file in the locale encoding
    (cp1252 on Windows), which UTF-8 cannot read when a comment holds a dash.
    Latin-1 is the last resort: it decodes any byte, and only comments hold
    non-ASCII text in practice.
    """
    for encoding in ("utf-8", None, "latin-1"):
        parser = _new_parser()
        try:
            parser.read(path, encoding=encoding)
            return parser
        except UnicodeDecodeError:
            continue
    return parser


_REGISTRY = None


def settings_registry():
    """The registry of every packaged setting (built once)."""
    global _REGISTRY
    if _REGISTRY is None:
        ranges = {
            f"autoortho.{name}": spec["range"]
            for name, spec in PROVIDER_SETTINGS.items()
            if "range" in spec
        }
        _REGISTRY = build_registry(AOConfig._defaults, ranges)
    return _REGISTRY


class AOConfig(object):
    config = configparser.ConfigParser(strict=False, allow_no_value=True, comment_prefixes='/')


    _defaults = f"""
[general]
# Use GUI config at startup
gui = True
# Show config setup at startup everytime
showconfig = True
# First-run setup was completed or inferred from an existing valid config
setup_complete = False
# Internal: schema revision used for one-time default migrations. Do not edit.
config_revision = {CONFIG_REVISION}
# Application shell state
window_width = 1100
window_height = 760
window_x = 100
window_y = 100
last_page = Home
# Settings shows every category (True) or only the essential ones (False).
show_all_settings = False
# Engine settings an older config set by hand, kept when AutoOrtho started
# sizing them itself. Home offers to review them until dismissed.
kept_values =
dismissed_update_version =
# Hide when running
hide = True
# Console/UI log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
console_log_level = INFO
# File log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
file_log_level = DEBUG

[paths]
# X-Plane install path
xplane_path =
# Scenery install path (X-Plane Custom Scenery or other.)
scenery_path =
# Directory where satellite images are cached
cache_dir = {os.path.join(os.path.expanduser("~"), ".autoortho-data", "cache")}
# Optional long-term cache directory (e.g. large slow disk or network share).
# Leave empty to disable. Chunks are stored here permanently and promoted to
# local cache on access, avoiding re-downloads across reinstalls or machines.
long_term_cache_dir =
# Set directory for temporary downloading of scenery and other support files
download_dir = {os.path.join(os.path.expanduser("~"), ".autoortho-data", "downloads")}
# Changing log_file dir is currently not supported
log_file = {os.path.join(os.path.expanduser("~"), ".autoortho-data", "logs", "autoortho.log")}
# Directory where dsf files are cached
dsf_dir = {os.path.join(os.path.expanduser("~"), ".autoortho-data", "dsf")}

[autoortho]
# Override map type with a different source
maptype_override = Use tile default
# Minimum zoom level to allow.  THIS WILL NOT INCREASE THE MAX QUALITY OF SATELLITE IMAGERY
min_zoom = 12
# Maximum zoom level to allow.  Higher values = more detail but larger downloads and more VRAM usage.
# Optimal: 16 for most cases. Keep in mind that every extra ZL increases VRAM and potential network usage by 4x.
max_zoom = 16
# Maximum zoom level to allow near airports. Zoom level around airports used by default is 18.
max_zoom_near_airports = 18
# Dynamic zoom mode: "fixed" (current behavior) or "dynamic" (altitude-based quality steps)
# Fixed: Uses max_zoom slider value for all tiles
# Dynamic: Uses altitude-based quality steps defined in dynamic_zoom_steps
max_zoom_mode = fixed
# Quality steps for dynamic zoom mode. List of altitude/zoom pairs.
# Format: [{{"altitude_ft": <altitude>, "zoom_level": <zoom>}}, ...]
# Example: [{{"altitude_ft": 0, "zoom_level": 17}}, {{"altitude_ft": 20000, "zoom_level": 15}}]
# Altitude is "at or above" - tiles are rendered at the zoom level of the highest matching altitude.
# The base step (altitude_ft: 0) cannot be removed and serves as the ground-level default.
dynamic_zoom_steps = []
# RETIRED: the [streaming] wait bands are the only wait policy. These four are
# read for old files only and ignored: maxwait is now band_max_wait_seconds
# (near_wait_seconds while a flight starts), tile_time_budget the longest of
# those, and the startup extension (suspend_maxwait) and the time budget
# (use_time_budget) are always on.
maxwait = 2.0
suspend_maxwait = True
use_time_budget = True
tile_time_budget = 60.0
# Fallback level when chunks fail to download in time:
# none = Skip all fallbacks (fastest, may have missing tiles)
# cache = Use disk cache and already-built mipmaps, no network (balanced)
# full = All fallbacks including on-demand network downloads (best quality, slowest)
# Recommended: cache for most users, full if you have fast internet and CPU
fallback_level = cache
# When enabled with fallback_level=full, network fallbacks will continue even after
# the tile time budget is exhausted. This prioritizes quality over strict timing.
# True = Better quality, may cause longer load times when network is slow
# False = Strict timing, fallbacks respect budget (may have more missing tiles)
# Recommended: False for stutter-free experience, True for maximum quality
fallback_extends_budget = False
# Timeout per mipmap level when using extended fallbacks (in seconds)
# When fallback_extends_budget is True, each lower-detail mipmap level
# gets this much time to download. Total extra time = this * number of levels tried.
# Example: 10 seconds * 4 levels = 40 seconds max additional time
# Range: 10 - 120 seconds
# Recommended: 3.0 (balanced), 5.0 (quality), 1.5 (fast)
fallback_timeout = 30.0
# Allow a native row build to finish after its shared deadline by using
# cache/lower-ZL fallbacks and finally missing_color for unresolved positions.
# Enabled by default: a strict build rejects the whole row when one chunk is
# late, and the row is then rebuilt slowly or served as missing color. Rows
# served degraded are rebuilt when X-Plane opens the tile again.
native_partial_allow_incomplete = True
# Spatial prefetching - proactively downloads tiles ahead of aircraft
# Enable/disable prefetching (True/False)
prefetch_enabled = True
# Unified bounded candidate/admission pipeline. Disable only as a temporary
# rollback while diagnosing a provider- or scenery-specific regression.
prefetch_pipeline_v2 = True
# How far ahead to prefetch in minutes of flight time (1-60)
# Higher = more tiles prefetched ahead, uses more bandwidth and memory
# Lower = fewer tiles prefetched, less resource usage
# Set to 0 for unlimited route discovery. Retained candidates and admitted work
# remain bounded even in unlimited mode.
prefetch_lookahead = 10
# How often to check for prefetch opportunities in seconds (0.5-10)
prefetch_interval = 1.0
# Maximum chunks admitted in one coordinator cycle. This does not size queues.
prefetch_max_chunks = 64
# Maximum chunks materialized by one candidate admission pass.
prefetch_admission_burst = 64
# Prefetch radius in nautical miles (10-150)
# Tiles within this radius of the flight path are prefetched
# Used by both velocity-based and SimBrief prefetching
prefetch_radius_nm = 30
# Live imagery quality policy:
# responsive = bounded wait with lower-ZL fallback (existing behavior)
# prefer_target = pause distant work and grant a short exact-quality grace period
# strict_target = never substitute lower-ZL data into mipmap zero
prefetch_quality_mode = responsive
prefetch_quality_grace_sec = 5.0
strict_target_deadline_sec = 120.0
# Predictive DDS generation - pre-build DDS textures in background after prefetch
# When enabled, tiles are compressed to DDS in the background, eliminating stutters
# when X-Plane loads new scenery areas. Falls back gracefully on cache miss.
predictive_dds_enabled = True
# Minimum interval between DDS builds in milliseconds (50-2000)
# Rate limits background builds to prevent CPU spikes during flight
# Lower = faster building, higher CPU usage
# Higher = slower building, lower CPU usage
# Recommended: 250 (fast CPU), 500 (balanced), 1000 (low-end CPU)
predictive_dds_build_interval_ms = 250
# Number of parallel prefetch workers for predictive DDS builds (1-16)
# These run in the background to pre-build tiles ahead of where you're flying
# Higher values = faster prefetch throughput, but more CPU usage during flight
# Lower values = less CPU impact, slower prefetch
# This is a maximum; the predictive byte budget may admit fewer concurrently.
background_builder_workers = 2
# Estimated decoded/native working-set budget for predictive builds.
predictive_dds_memory_mb = 512
# Number of concurrent tile build workers (1-32)
# Controls how many tiles can be built simultaneously by the native pipeline
# Higher values = faster tile processing, more CPU/RAM usage
# Lower values = less resource usage, potential stutters
# Recommended: 4 (low-end), 8 (mid-range, default), 16-32 (high-end CPU)
live_builder_concurrency = 8
# End-to-end live tile requests admitted before allocating composition state.
# Keep this high enough to saturate downloads while bounding ZL17 working memory.
live_tile_admission = 16
# Maximum retained RGBA fallback imagery per tile. ZL17 mipmap 0 alone is
# 256MB and is therefore composed, compressed, and released instead of cached.
tile_image_cache_mb = 96
# Bound immutable JPEG bytes waiting for local and optional long-term cache I/O.
cache_write_buffer_mb = 256
long_term_cache_write_buffer_mb = 128
# Apply fallbacks when pre-building DDS (True/False)
# True (default): Apply same fallback logic as live requests (cache search, lower zoom, etc.)
#   - Pro: Prebuilt tiles have best possible quality with fallbacks for failed chunks
#   - Con: Prebuilds may do extra disk/network I/O to find fallback data
# False: Use missing color for failed chunks (no fallbacks, fastest)
#   - Pro: Faster prebuilds, no extra I/O
#   - Con: Failed chunks show missing color instead of fallback data
predictive_dds_use_fallbacks = True
# Deprecated compatibility alias for older configurations. The runtime uses the
# persistent DynamicDDSCache and the shared disk budget below.
ephemeral_dds_cache_mb = 4096
# Persistent DDS cache - stores pre-built DDS textures across sessions
# Eliminates JPEG decode + DXT compress on subsequent loads (~1-2ms read vs ~390ms rebuild)
# Set to 0 for unlimited cache (recommended). Uses disk budget manager for cleanup.
persistent_dds_cache_mb = 0
# Persist compressed mipmap-0 chunk rows asynchronously for repeat ZL17 loads.
# Exact compressed rows are safe to reuse across sessions.
persist_partial_dds_cache = True
partial_cache_promote_startup_max_tiles = 0
# Delete source JPEG chunks only after a complete DDS is durably stored. Keeping
# sources improves rebuild resilience when compiled DDS entries are evicted.
cleanup_source_jpegs_after_dds = False
# Bounded workers for DDS eviction and optional JPEG cleanup.
dds_cache_maintenance_workers = 2
# Disk budget enforcement - automatically cleans up old cache data
# When total cache exceeds file_cache_size, oldest data is evicted
# Categories: DDS cache (compiled textures), JPEGs (source images)
disk_budget_enabled = True
# Literal percentage of file_cache_size allocated to persistent DDS cache
# (10-90). JPEG source files receive the remainder.
dds_budget_pct = 80
# Maximum threads for native pipeline operations (per concurrent build)
# 0 = auto (dynamically divides CPU cores across concurrent builds)
# >0 = fixed thread count per build (e.g., 4 means each build uses 4 threads)
native_pipeline_threads = 0
# Pipeline mode for DDS texture building
# auto: Automatically select best mode for your platform (recommended)
#       With buffer pool optimization, hybrid is fastest on all platforms
#       (Python file reads are OS-cache optimized, 10ms vs 21ms for C)
# native: Full native pipeline (C handles file I/O + decode + compress)
#         Uses more threads, may be better for cold cache scenarios
# hybrid: Python reads files, native decode + compress (recommended)
#         Fastest with buffer pool, lower thread overhead
# python: Pure Python fallback (slowest but most compatible)
#         Use if native pipeline causes issues
pipeline_mode = auto
# Number of pre-allocated buffers for zero-copy DDS building (2-64)
# Buffer size is calculated dynamically based on your zoom settings:
#   - ~11MB per buffer when max_zoom <= 16 and max_zoom_near_airports <= 18 (4K textures)
#   - ~43MB per buffer when higher zoom levels are configured (8K textures)
#   - ~43MB per buffer when using custom tiles (assumes worst case)
# 
# Default: Automatically calculated as (prefetch_workers + live_builder_concurrency)
# This ensures optimal parallelism: each worker can have its own buffer.
# Maximum is capped to (prefetch_workers + live_builder_concurrency) since
# more buffers than workers would never be used simultaneously.
# 
# The value here (10) is a reasonable fallback = 2 prefetch + 8 live (defaults)
buffer_pool_size = 10
# === LIVE AOPIPELINE SETTINGS ===
# These control the optimized pipeline for live (on-demand) DDS builds when
# X-Plane requests tiles that aren't in the predictive cache.
#
# Enable optimized aopipeline for live DDS builds (True/False)
# When enabled, attempts to build entire tile with native pipeline (~5x faster)
# when sufficient chunks are available. Falls back to progressive path on failure.
# Recommended: True (enables fast path when chunks are cached/prefetched)
live_aopipeline_enabled = True
# Threshold ratio of chunks that triggers an early DDS build (0.5-1.0)
# Controls a two-phase build strategy:
#   1. Early build: fires when this ratio is reached, missing chunks filled with missing_color
#   2. Healing pass: fires automatically when the remaining chunks arrive, replacing placeholders
# 1.0 = wait for all chunks before building (single pass, no healing needed)
# 0.9 = build at 90%%, heal remaining 10%% when they arrive (faster first texture, then corrected)
# 0.5 = build at 50%% (fastest first texture, more healing work)
# Recommended: 1.0 (quality, no artifacts), 0.9 (balanced), 0.8 (faster)
# DEPRECATED: min_chunk_ratio is now always 1.0 (require all chunks).
# Kept for config file backwards compatibility — value is ignored.
live_aopipeline_min_chunk_ratio = 1.0
# === STREAMING BUILDER SETTINGS ===
# Enable streaming builder for incremental DDS generation (True/False)
# When enabled, chunks are processed as they arrive rather than in batch.
# This allows fallbacks to be applied during the build process.
# Recommended: True (better fallback support), False for legacy behavior
streaming_builder_enabled = True
# Size of the streaming builder pool (2-64)
# Each builder handles one tile at a time. Pool size determines
# how many tiles can be built concurrently.
# Recommended: 4 (default), increase to 8-16 for faster CPUs, up to 64 for high-end systems
streaming_builder_pool_size = 4
# === TILE QUEUE SETTINGS ===
# When all buffer pool slots are busy, tiles wait in a queue instead of
# falling back to the slower Python pipeline. This ensures consistent
# performance and prevents resource contention.
#
# Enable tile queue system with priority (True/False)
# When enabled, tiles wait for pool slots using a priority queue:
# - Live tiles (X-Plane requests) = "premium clients", served first
# - Prefetch tiles = low priority, served when system is idle
# Recommended: True for consistent performance
tile_queue_enabled = True
# Maximum tiles that can wait in queue (10-500)
# If queue is full, additional tiles are rejected
# Higher = more tiles can wait, but uses more memory for tracking
# Recommended: 100 (default)
tile_queue_max_size = 100
# Shared HTTP/2 transport. The parent owns one broker used by all mount workers.
# Set to False to try plain HTTP/1.1, as OrthoStreamXP does: no broker, each
# of the fetch_threads download workers makes one request at a time on its
# own connection. Raise fetch_threads at the same time (to 64, its ceiling;
# it is also capped at twice the CPU cores), since that is then the number
# of downloads in flight.
http2_enabled = True
# Maximum broker requests that may be in flight (pending a reply) at once.
# This is the real download-concurrency knob: it is not bounded by the number
# of downloader threads because broker requests are dispatched asynchronously.
provider_max_in_flight = 320
# Maximum reusable HTTP connections owned by the broker. HTTP/2 multiplexes
# many requests per connection, so this stays well below provider_max_in_flight.
provider_max_connections = 160
# Threads used to apply broker responses (cache write, decode hand-off).
# This is a coordination pool, not a download pool; keep it small.
download_dispatch_workers = 4
# Maximum time a request may wait for broker/provider admission before its
# network timeout begins. Queue time is deliberately separate from HTTP time.
provider_queue_timeout = 60.0
# Adaptive per-provider concurrency. The broker tracks each origin
# (scheme://host) separately: it adds provider_origin_increase_step permits
# after provider_origin_success_threshold consecutive good responses and
# multiplies the limit by provider_origin_decrease_factor when the origin
# reports overload (429, 5xx, timeouts). 403/410 are provider policy answers
# and never shrink the limit.
provider_adaptive_concurrency = True
# Windowed v2 observes two-second throughput/latency/error windows. Disable
# temporarily to restore the legacy per-response AIMD controller.
provider_adaptive_controller_v2 = False
# Aggregate supported httpcore connection lifecycle events. Detailed request
# tracing remains disabled and no request URLs or tokens are retained.
provider_transport_trace = False
# Starting per-origin limit. 0 starts at provider_max_connections and ramps
# toward provider_origin_max_concurrency after successful responses.
provider_origin_initial_concurrency = 128
# Floor for a throttled origin, so a bad patch cannot starve it completely.
provider_origin_min_concurrency = 64
# Ceiling for one origin. 0 means "use provider_max_in_flight".
provider_origin_max_concurrency = 0
provider_origin_increase_step = 1
provider_origin_success_threshold = 8
provider_origin_decrease_factor = 0.5
# Consecutive overload signals inside this window cause a single decrease.
provider_origin_cooldown_seconds = 5.0
# Legacy aliases for the two settings above. They are honoured only when the
# modern setting is still at its packaged default.
max_concurrent_downloads = 256
http2_max_connections = 64
fetch_threads = 32
# Simheaven compatibility mode.
simheaven_compat = False
# Using custom generated Ortho4XP tiles along with AutoOrtho.
using_custom_tiles = False
# Color used for missing textures. an
missing_color = [66, 77, 55]
# How X-Plane gets the textures. fuse: a virtual file system (WinFsp,
# macFUSE or FUSE) builds each texture while X-Plane reads it. files
# (experimental): no virtual file system; AutoOrtho writes textures to real
# files ahead of X-Plane, and an X-Plane plugin holds a flight's loading
# screen until the start area is built. Late textures show grey or blurry
# until X-Plane's next read. Settings in [files].
streaming_backend = fuse

[streaming]
# Streaming techniques adopted from OrthoStreamXP. Each one can be turned off
# on its own to compare with the previous behavior.
#
# Only the mipmap X-Plane actually wants triggers downloads. X-Plane reads the
# start of every texture it loads (the "header chunk"), and the read for each
# level starts with the tail of the finer level before it. Those incidental
# bytes are answered from what is at hand instead of downloading max-zoom rows.
read_classifier = True
# Keep the low-zoom chunks (this zoom and coarser) on disk for good and never
# evict them. One ZL12 chunk covers a whole ZL16 tile, so far scenery and
# placeholders always have real imagery after the first visit.
low_zoom_base = True
low_zoom_base_max_zoom = 12
# Prefetch each tile only to the mipmap X-Plane will want at its distance,
# instead of every mipmap. Far tiles then cost one low-zoom chunk.
paging_prefetch = True
# Paging constant k: X-Plane wants mipmap n while the distance to the tile's
# near edge, in the tile's own pixels, is below 2^n * k. 0 guesses it from
# X-Plane's window and field of view, then measures it in flight.
paging_k = 0
# Fetch the level the aircraft will need this many seconds from now.
# Auto-tuning may raise it (30-180 s) when blurry tiles were prefetched only
# at a coarser level or too late; a value set here is never changed.
paging_lead_seconds = 60
# X-Plane opens all terrain (.ter) files of a scenery tile 20-45 s before it
# reads their textures. Queue those textures as soon as their .ter is opened.
ter_load_ahead = True
# Wait policy by distance band. Near (mipmap 0-1) waits for real imagery,
# middle waits briefly, far (the low-zoom base level) never waits: it gets the
# best data at hand and the real data is fetched behind it.
wait_bands = True
# Seconds a near or middle read may wait while a flight starts or X-Plane
# loads a new place (X-Plane will not read those textures again while parked).
near_wait_seconds = 60.0
# Seconds a near or middle read may wait in flight.
band_max_wait_seconds = 6.0
# Seconds a far read (the base level) may wait. OrthoStreamXP uses 0; a short
# wait lets a base chunk that is already downloading arrive.
far_wait_seconds = 0.5
# Downloads are ordered by band (near reads first, then middle reads, then
# work done ahead of X-Plane). A queued build X-Plane starts reading is raised
# to the reader's priority instead of being started again.
band_priorities = True
# Keep prefetch running (at lower priority) while X-Plane reads tiles,
# instead of pausing it. False restores the pause.
prefetch_during_live_reads = True
# Apply season colors when bytes are served, by recoloring the DXT block
# endpoints, instead of baking them into built and cached textures. Changing
# season settings then never invalidates the cache.
read_time_colours = True
# Track one number per tile: the finest mipmap with real imagery. Only real
# imagery is persisted, and a level implies every coarser one.
real_from_index = True

[pydds]
# ISPC or STB for dds file compression
compressor = ISPC
# BC1 or BC3 for dxt1 or dxt5 respectively
format = BC1
# Disk compression for cached DDS files (none or zstd)
# zstd reduces cache disk usage by 30-60% with ~2-5ms added read latency
# none stores raw DDS data (fastest reads, largest disk usage)
dds_compression = zstd
# Zstd compression level (1-19, default 3)
# Higher = smaller files, slower writes (reads are always fast)
# Background builds can afford higher levels since writes are async
# Recommended: 3 (balanced), 6 (smaller files), 1 (fastest writes)
dds_compression_level = 3

[scenery]
# Don't cleanup downloads
noclean = False
# Maximum parallel download workers for scenery packages (1-8)
# Higher values download more files simultaneously, saturating bandwidth faster
# Recommended: 4 (default), 2 (slow connection), 8 (fast connection)
max_download_workers = 4
# Minimum free-space reserve used by setup and scenery installation checks
storage_safety_margin_gb = 2

[files]
# Files mode (streaming_backend = files). Folder holding the built textures.
# Empty: <scenery_path>/z_autoortho/files. Must be NTFS, APFS, ext4, btrfs or
# xfs (hard links and sparse files), best on the same disk as the scenery.
root =
# Disk budget in GB for built textures (at least 10).
disk_gb = 20
# Sharp textures are built this many km beyond what X-Plane reads around the
# aircraft, so external views close to it look sharp too.
camera_reach_km = 10
# Unbuilt textures: link (hard links to one neutral texture; cheapest) or
# sparse (a small file of their own each).
materialize = link
# UDP port on 127.0.0.1 where the X-Plane plugin asks whether to go on.
gate_port = 49317
# No level is built from imagery coarser than this zoom (Bing's coarser
# imagery has clashing colours).
imagery_floor_zl = 13
# Install and update the AutoOrtho X-Plane plugin when files mode starts.
install_plugin = True
# Windows: exclude the files folder from Windows Search for this user (it only
# acts when the folder is indexed; Home says so when it is).
exclude_from_search = False

[fuse]
# Enable or disable multi-threading when using FUSE
threading = True
# NOTE: build_timeout (FUSE lock timeout) is calculated dynamically from the
# tile budget (the longest [streaming] wait). Formula: that budget +
# fallback_timeout (if enabled) + 15s.
# This ensures the lock timeout always exceeds the maximum possible tile build time.

[flightdata]
# Local port for map and stats (default 5847 to avoid macOS AirPlay on 5000)
webui_port = 5847
# UDP port XPlane listens on
xplane_udp_port = 49000

[cache]
# Max size of the image disk cache in GB. Minimum of 10GB
file_cache_size = 30
# Max size of memory cache in GB. Minimum of 2GB.
cache_mem_limit = 4
# Auto clean cache on AutoOrtho exit
auto_clean_cache = False

[diagnostics]
# Record per-stage tile latency and per-process memory/CPU usage for each mounted
# flight session. Reports are written when AutoOrtho unmounts the scenery.
performance_profiling = True
# Process resource sampling interval. One second provides useful flight timelines
# with low overhead.
sample_interval_seconds = 1.0
# Persist worker stage/gauge checkpoints so useful profiles survive forced exits.
checkpoint_interval_seconds = 10.0
# Keep the slowest operations whose duration exceeds this threshold.
slow_operation_ms = 250.0
# Maximum slow operations retained per process. Histograms still include all calls.
max_slow_operations = 200
# Track Python allocation sites. This adds material overhead and does not include
# native image/DDS buffers, so enable only for a dedicated diagnostic flight.
python_allocation_tracing = False
# Output directory for Markdown and raw JSON flight reports.
report_dir = {os.path.join(os.path.expanduser("~"), ".autoortho-data", "reports")}
# Number of completed profiling sessions to retain.
max_reports = 20
# Record which texture level X-Plane reads at what distance (FUSE mode), to
# calibrate files mode. Local files in ~/.autoortho-data/telemetry, never sent.
read_telemetry = True

[seasons]
seasons_convert_workers = 4
enabled = False
spr_saturation = 70.0
sum_saturation = 100.0
fal_saturation = 80.0
win_saturation = 55.0
compress_dsf = True

[terrain]
# Default SUPER_ROUGHNESS value for .ter files (0.0 to 1.0)
# Higher values make terrain less reflective/shiny at sunset/sunrise
default_roughness = 1.0
# Number of workers for .ter file patching
ter_patch_workers = 8

[windows]
prefer_winfsp = True

[guards]
# Warn when the zoom target or the memory cache looks too big for this PC
# (graphics memory, RAM, disk). Warnings never block anything.
warnings = True
# Graphics memory X-Plane needs for itself beside AutoOrtho's photo textures,
# in GB. Raise it for heavy aircraft or high X-Plane texture settings.
xplane_vram_reserve_gb = 5
# Emergency brake: while free RAM is very low, pause prefetch and background
# builds and shrink the memory cache until it recovers.
ram_brake = True

[tuning]
# Scale the memory cache, predictive build memory and builder counts to this
# PC's cores and RAM. A value you set yourself always wins.
hardware_baseline = True

[governor]
# Auto-tuning while streaming. auto: AutoOrtho adjusts engine settings live,
# within the limits, to reach the quality target, and remembers what worked
# for the next flight. advise: it only suggests changes (shown in Settings >
# Auto-Tuning and the log). off: the configured values, as before.
mode = advise
# Keep a record of each flight's signals for replaying the governor offline.
record_samples = True
# Let auto-tuning cap photo textures at 4096 px when the textures X-Plane
# holds near the graphics memory limit. A capped texture (8192 px at ZL17 on
# ZL16 scenery, ZL19 near airports) takes a quarter of the graphics memory and
# shows the detail of one zoom level lower. Your zoom target is not changed.
allow_texture_cap = False
# Set by auto-tuning: the largest texture built, in px (0: no cap). Applies to
# textures opened after it changes.
texture_cap_px = 0

[limits]
# What AutoOrtho may use of this PC. "auto" works it out from the hardware;
# a number sets your own ceiling. AutoOrtho keeps the values it picks itself
# under these limits; a value you set by hand is kept, with a warning.
# RAM for AutoOrtho in GB (memory cache, build buffers, the app). auto: a
# quarter of RAM plus 0.5 GB, leaving X-Plane at least 4 GB or 20 percent.
ram_gb = auto
# Graphics memory for photo textures in GB. auto: the card's memory minus
# xplane_vram_reserve_gb in [guards].
vram_gb = auto
# Download speed in Mbit/s. auto: no limit.
bandwidth_mbps = auto
# Physical CPU cores for building textures. auto: all but two (all but one
# on PCs with four cores or fewer).
cpu_cores = auto
# Protect X-Plane's frame rate: when X-Plane's frames get slower while
# streaming, auto-tuning (Auto mode) eases off background work first.
protect_sim_fps = True

[time_exclusion]
# Enable sun-position-based AutoOrtho exclusion.
# When active, AutoOrtho scenery is disabled at night and X-Plane uses its default scenery.
# The sun elevation angle (from sim/graphics/scenery/sun_pitch_degrees) determines day/night.
# This is accurate across seasons, latitudes, and with time acceleration.
enabled = False
# When enabled, assume exclusion is active until sun position data is available.
# Useful to ensure night flights start with default scenery from the beginning.
# When disabled, AutoOrtho works normally until sun data confirms exclusion.
default_to_exclusion = False
# Sun elevation threshold to switch to night mode (degrees).
# Nautical twilight (-12) is when artificial lights dominate the landscape.
# Range: -18 (astronomical twilight) to 0 (horizon).
sun_night_threshold = -12.0
# Sun elevation threshold to switch to day mode (degrees).
# Should be higher than night threshold to provide hysteresis and prevent rapid toggling.
sun_day_threshold = -10.0

[simbrief]
# SimBrief user ID for flight plan integration
userid = 
# Use SimBrief flight data for dynamic zoom level and pre-fetching calculations
use_flight_data = False
# Radius in nautical miles to consider fixes when calculating altitude for a tile
# All fixes within this radius are considered, and the lowest altitude is used
route_consideration_radius_nm = 50
# If the aircraft deviates more than this distance (nm) from the route, fall back to DataRef-based calculations
route_deviation_threshold_nm = 40
# Allow prefetching while parked when SimBrief flight plan is loaded
# With a known route, we don't need to wait for aircraft movement to start prefetching
prefetch_while_parked = True
# Radius in nautical miles around waypoints to prefetch tiles (now in Advanced Settings)
route_prefetch_radius_nm = 40
"""

    def __init__(self, conf_file=None):
        self.config = configparser.ConfigParser(
            strict=False,
            allow_no_value=True,
            comment_prefixes='/',
        )
        if not conf_file:
            self.conf_file = os.path.join(os.path.expanduser("~"), ".autoortho")
        else:
            self.conf_file = conf_file

        # Always load initially
        self.ready = self.load()
        
        # Save to update new defaults, but ONLY in the main process.
        # Mount worker subprocesses must NOT save config to avoid race conditions
        # that can overwrite the main process's configuration and reset user settings.
        # Workers are identified by the AO_RUN_MODE environment variable set during launch.
        if not is_mount_worker_mode():
            self.save()


    @staticmethod
    def registry():
        return settings_registry()

    def load(self):
        self._baseline = {}
        self.config.read_string(self._defaults)
        file_exists = os.path.isfile(self.conf_file)
        if file_exists:
            log.info(f"Config file found {self.conf_file} reading...")
            user_config = _read_config_file(self.conf_file)
        else:
            log.info("No config file found. Using defaults...")
            user_config = _new_parser()

        # Comment lines are parsed as option keys; the commented defaults of a
        # sparse file ("# key = value") must not reach the live config.
        for section in user_config.sections():
            if not self.config.has_section(section):
                self.config.add_section(section)
            for key, value in user_config.items(section, raw=True):
                if not key.startswith('#'):
                    self.config.set(section, key, value)

        self._explicit_options = {
            section: {
                option for option in user_config.options(section)
                if not option.startswith('#')
            }
            for section in user_config.sections()
        }
        self._loaded_explicit = {}
        if file_exists and _file_revision(user_config) >= SPARSE_REVISION:
            self._loaded_explicit = self._explicit_values(user_config)
        _migrate_legacy_provider_options(self, user_config)
        if file_exists:
            _migrate_config_revision(self, user_config)
        self.get_config()
        self._apply_hardware_baseline()
        if file_exists and _file_revision(user_config) < SPARSE_REVISION:
            kept = self.kept_engine_values()
            if kept:
                self.set_value("general", "kept_values", ",".join(kept))
        return True

    # Engine sections whose hand-set values Home offers to review after an
    # upgrade, and the choices there that are the user's intent, not tuning.
    _ENGINE_SECTIONS = ("autoortho", "streaming", "pydds", "fuse", "cache")
    _NOT_ENGINE = frozenset({
        "autoortho.using_custom_tiles",
        "autoortho.simheaven_compat",
        "autoortho.missing_color",
    })

    def kept_engine_values(self):
        """Engine settings holding the user's own value, by "section.key"."""
        kept = []
        for spec in self.registry():
            if spec.tier not in (Tier.TUNING, Tier.EXPERT):
                continue
            if spec.section not in self._ENGINE_SECTIONS:
                continue
            if spec.name in self._NOT_ENGINE:
                continue
            if self.is_custom(spec.section, spec.key):
                kept.append(spec.name)
        return sorted(kept)

    def _apply_hardware_baseline(self, custom=None):
        """Pick values for settings the user has not set (never written).

        Three sources, in order, each only for settings still at their
        packaged default:

        * the hardware baseline (hardware.baseline_values), when on;
        * the connection test's warm start for download concurrency;
        * the limits in [limits], which lower any of the above.

        Whether the baseline is on, and the limits, are fixed at load, so
        changing them takes effect at the next start without pinning the old
        values.
        """
        self._baseline = {}
        self._automatic_sources = {}
        registry = self.registry()
        enabled = parse_value(
            str(self._current_value("tuning", "hardware_baseline"))
        ) is True
        try:
            info = hardware.basic()
        except Exception as exc:
            log.debug(f"Hardware unavailable: {exc}")
            return
        chosen = {}
        sources = {}
        if enabled:
            for name, text in hardware.baseline_values(info).items():
                chosen[name] = text
                sources[name] = "hardware"
        warm = _connection_warm_start(self)
        if warm is not None:
            chosen["autoortho.provider_origin_initial_concurrency"] = warm
            sources["autoortho.provider_origin_initial_concurrency"] = "measured"
        try:
            active = limits_module.from_config(self, info)
        except Exception as exc:
            log.debug(f"Limits unavailable: {exc}")
            active = None
        if active is not None:
            governed = {}
            for name in limits_module.GOVERNED:
                section, key = name.split(".", 1)
                spec = registry.get(section, key)
                if spec is not None:
                    governed[name] = chosen.get(name, spec.default_text)
            clamped = limits_module.clamp_automatic(governed, active)
            for name, text in clamped.items():
                if text != governed[name]:
                    chosen[name] = text
                    sources[name] = "limit"
            ceiling = _bandwidth_concurrency_ceiling(self, active.bytes_per_second)
            if ceiling is not None:
                name = "autoortho.provider_origin_max_concurrency"
                chosen[name] = ceiling
                sources[name] = "limit"
        for name, text in chosen.items():
            section, key = name.split(".", 1)
            spec = registry.get(section, key)
            if spec is None or spec.tier is Tier.RETIRED:
                continue
            if _same(
                _normalized(section, text),
                _normalized(section, spec.default_text),
            ):
                continue
            is_own = (
                name in custom if custom is not None
                else self.is_custom(section, spec.key)
            )
            self._baseline[(section, spec.key)] = text
            self._automatic_sources[(section, spec.key)] = sources[name]
            if not is_own:
                self.set_value(section, spec.key, text)

    def recompute_automatic(self):
        """Work the automatic values out again (limits changed while running).

        Settings holding the user's own value keep it; the others move to
        the new automatic value, or back to the packaged default when no
        rule applies any more. Returns {"section.key": new value} for what
        changed, ready to publish as live levers.
        """
        registry = self.registry()
        names = {f"{s}.{k}" for s, k in self._baseline}
        names.update(limits_module.GOVERNED)
        names.update({
            "autoortho.provider_origin_max_concurrency",
            "autoortho.provider_origin_initial_concurrency",
        })
        before = {}
        custom = set()
        for name in names:
            section, key = name.split(".", 1)
            if registry.get(section, key) is None:
                continue
            before[name] = self._current_value(section, key)
            if self.is_custom(section, key):
                custom.add(name)
        self._apply_hardware_baseline(custom=custom)
        changed = {}
        for name, old in before.items():
            section, key = name.split(".", 1)
            if name not in custom and (section, key) not in self._baseline:
                spec = registry.get(section, key)
                self.set_value(section, key, spec.default_text)
            new = self._current_value(section, key)
            if not _same(_normalized(section, new), _normalized(section, old)):
                changed[name] = new
        return changed

    def _explicit_values(self, user_config):
        """The registered settings a sparse file sets, in comparable form."""
        registry = self.registry()
        values = {}
        for section in user_config.sections():
            for key, value in user_config.items(section, raw=True):
                spec = registry.get(section, key)
                if spec is None or spec.tier is Tier.RETIRED:
                    continue
                values[(section, spec.key)] = _normalized(section, value)
        return values

    # -- Value sources -----------------------------------------------------

    def _current_value(self, section, key):
        namespace = self.__dict__.get(section)
        if namespace is not None and key in namespace.__dict__:
            return namespace.__dict__[key]
        return self.config.get(section, key, raw=True, fallback=None)

    def value(self, section, key):
        """The live value of a setting (None when unknown)."""
        return self._current_value(section, key)

    def automatic_text(self, section, key):
        """The value a setting has when the user has not set it: the
        hardware baseline for this PC, else the packaged default."""
        baseline = getattr(self, "_baseline", {}).get((section, key))
        if baseline is not None:
            return baseline
        spec = self.registry().get(section, key)
        return None if spec is None else spec.default_text

    def is_custom(self, section, key):
        """Whether a setting holds the user's own value rather than the default.

        A value differing from the default is custom. In a sparse file, an
        active line equal to the default stays custom until the value changes,
        so a deliberate choice is not dropped. Settings the registry does not
        know (plugins, hand-added keys) are always custom; retired ones never.
        """
        spec = self.registry().get(section, key)
        if spec is None:
            return True
        if spec.tier is Tier.RETIRED:
            return False
        current = _normalized(section, self._current_value(section, spec.key))
        automatic = self.automatic_text(section, spec.key)
        if not _same(current, _normalized(section, automatic)):
            return True
        loaded = self._loaded_explicit.get((section, spec.key), _MISSING)
        return loaded is not _MISSING and _same(loaded, current)

    def source(self, section, key):
        """Where a setting's value comes from, for the settings UI.

        'custom' (the user's), 'limit' (lowered to fit [limits]),
        'hardware' (scaled to this PC), 'measured' (from the connection
        test) or 'default'.
        """
        if self.is_custom(section, key):
            return "custom"
        if (section, key) in getattr(self, "_baseline", {}):
            return getattr(self, "_automatic_sources", {}).get(
                (section, key), "hardware"
            )
        return "default"

    def _should_write(self, section, key):
        spec = self.registry().get(section, key)
        if spec is not None and spec.persist_always:
            return True
        return self.is_custom(section, key)

    def set_value(self, section, key, value):
        """Set one setting from its text form (as the config file holds it)."""
        text = '' if value is None else str(value)
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, key, text)
        namespace = self.__dict__.get(section)
        if namespace is None:
            namespace = SectionParser()
            self.__dict__[section] = namespace
        namespace.__dict__[key] = parse_value(text)

    def pin(self, section, key):
        """Keep the current value as the user's own: written to the file and
        never moved by auto-tuning, even when it equals the automatic one."""
        spec = self.registry().get(section, key)
        if spec is None or spec.tier is Tier.RETIRED:
            return
        self._loaded_explicit[(section, spec.key)] = _normalized(
            section, self._current_value(section, spec.key)
        )

    def reset_to_default(self, section, key):
        """Return a registered setting to its default; it is then not written."""
        spec = self.registry().get(section, key)
        if spec is None:
            raise KeyError(f"{section}.{key} is not a known setting")
        self.set_value(section, spec.key, self.automatic_text(section, spec.key))
        self._loaded_explicit.pop((section, spec.key), None)


    def _load_defaults_parser(self):
        """Create a ConfigParser loaded with internal defaults."""
        defaults_cp = configparser.ConfigParser(strict=False, allow_no_value=True, comment_prefixes='/')
        defaults_cp.read_string(self._defaults)
        return defaults_cp

    def _is_value_valid_for_default(self, current_value, default_value):
        """Validate current_value against the type implied by default_value.

        Returns True if current_value looks valid for the default's type; False otherwise.
        """
        s = '' if current_value is None else str(current_value).strip()
        d = '' if default_value is None else str(default_value).strip()

        # If default is an empty string, accept any value (including empty)
        if d == '':
            return True

        # Boolean
        if d.lower() in (SectionParser.true + SectionParser.false):
            return s.lower() in (SectionParser.true + SectionParser.false)

        # List (simple heuristic)
        if d.startswith('[') and d.endswith(']'):
            if s == '':
                return False
            try:
                parsed = ast.literal_eval(s)
                return isinstance(parsed, list)
            except Exception:
                return False

        # Integer
        try:
            int(d)
            try:
                int(s)
                return True
            except Exception:
                return False
        except Exception:
            pass

        # Float
        try:
            float(d)
            try:
                float(s)
                return True
            except Exception:
                return False
        except Exception:
            pass

        # String (non-empty required to be considered valid)
        return s != ''

    def _sanitize_and_patch_config(self):
        """Ensure all values exist and are valid; fill with defaults and mark for patching if needed."""
        defaults_cp = self._load_defaults_parser()
        patched = False

        for sect in defaults_cp.sections():
            if not self.config.has_section(sect):
                self.config.add_section(sect)
                patched = True

            for key, def_val in defaults_cp.items(sect):
                has_opt = self.config.has_option(sect, key)
                cur_val = self.config.get(sect, key, fallback=None) if has_opt else None

                needs_default = (not has_opt) or (cur_val is None) or (str(cur_val).strip() == '')
                if not needs_default:
                    # Validate type/format vs default
                    if not self._is_value_valid_for_default(cur_val, def_val):
                        needs_default = True

                if needs_default:
                    if key.startswith('#'):
                        # Avoid trailing '=' in comment lines
                        self.config.set(sect, key, None)
                    else:
                        self.config.set(sect, key, str(def_val))
                    patched = True

        # Flag for persistence after object dict is constructed
        self._patched_during_load = patched

    def get_config(self):
        # Pull info from ConfigParser object into AOConfig

        # First, sanitize and patch missing/invalid values
        self._sanitize_and_patch_config()

        config_dict = {sect: SectionParser(**dict(self.config.items(sect))) for sect in
                self.config.sections()}
        #pprint.pprint(config_dict)
        self.__dict__.update(**config_dict)
        self.refresh_derived_paths(create_missing=True)

        # If we patched any values during load, persist them now so next run is stable.
        # Only save in the main process - workers must not write config files.
        if getattr(self, "_patched_during_load", False):
            if not is_mount_worker_mode():
                try:
                    self.save()
                except Exception as e:
                    log.error(f"Failed to persist patched config defaults: {e}")
            self._patched_during_load = False
        return

    def refresh_derived_paths(self, create_missing=True):
        """Refresh mount definitions without necessarily creating directories."""
        self.ao_scenery_path = os.path.join(
                self.paths.scenery_path,
                "z_autoortho",
                "scenery"
        )

        self.xplane_custom_scenery_path = os.path.abspath(os.path.join(
                self.paths.xplane_path,
                "Custom Scenery"
        ))

        sceneries = []
        if os.path.exists(self.ao_scenery_path):
            sceneries = os.listdir(self.ao_scenery_path)
            log.info(f"Found sceneries: {sceneries}")
        
        if system_type == "darwin":
            try:
                if ".DS_Store" in sceneries:
                    sceneries.remove(".DS_Store")
            except Exception as e:
                log.error(f"Error removing .DS_Store from sceneries: {e}")

        self.scenery_mounts = [{
            "root": os.path.join(self.ao_scenery_path, s),
            "mount": os.path.join(self.xplane_custom_scenery_path, s),
        } for s in sceneries]


        if (
            create_missing
            and self.paths.scenery_path
            and not os.path.exists(self.ao_scenery_path)
        ):
            try:
                log.info(f"Creating dir {self.ao_scenery_path}")
                os.makedirs(self.ao_scenery_path)
            except OSError as e:
                log.warning(f"Could not create scenery dir {self.ao_scenery_path}: {e}")

    def save(self):
        log.info("Saving config ... ")
        self.set_config()

        text = self.render_config_text()
        with open(self.conf_file, 'w', encoding='utf-8') as h:
            h.write(text)
        # What was written is now the user's explicit choice.
        self._loaded_explicit = {
            (spec.section, spec.key): _normalized(
                spec.section,
                self._current_value(spec.section, spec.key),
            )
            for spec in self.registry()
            if spec.tier is not Tier.RETIRED
            and self.is_custom(spec.section, spec.key)
        }
        log.info(f"Wrote config file: {self.conf_file}")

    def render_config_text(self):
        """Render the config using the packaged template's layout.

        Comments are parsed as option keys (``comment_prefixes='/'``), so a
        comment containing ``:`` or ``=`` collapses into one shared key per
        section and old comments survive forever in their original position.
        Writing from the template keeps every comment verbatim next to the
        setting it describes and drops stale ones.

        Only custom settings (see :meth:`is_custom`) are active lines; every
        other setting is written as ``# key = default`` so the file still
        documents it. Retired settings and their comments are left out.
        Settings the template does not know are kept at the end of their
        section.
        """
        registry = self.registry()
        lines = []
        written = {}
        section = None
        pending_comments = []

        def flush_comments():
            lines.extend(pending_comments)
            pending_comments.clear()

        def flush_extras(sect):
            if sect is None or not self.config.has_section(sect):
                return
            seen = written.get(sect, set())
            for key in self.config.options(sect):
                if key.startswith('#') or key in seen:
                    continue
                lines.append(self._format_option(sect, key))
                seen.add(key)

        for raw_line in self._defaults.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                flush_comments()
                flush_extras(section)
                if lines and lines[-1] != '':
                    lines.append('')
                first_section = section is None
                section = stripped[1:-1].strip()
                written.setdefault(section, set())
                lines.append(stripped)
                if first_section:
                    lines.extend(_FILE_NOTE)
                continue
            if not stripped:
                flush_comments()
                continue
            if stripped.startswith('#') or section is None:
                pending_comments.append(stripped)
                continue
            key = self.config.optionxform(
                stripped.split('=', 1)[0].split(':', 1)[0].strip()
            )
            written[section].add(key)
            spec = registry.get(section, key)
            if spec is not None and spec.tier is Tier.RETIRED:
                pending_comments.clear()
                continue
            flush_comments()
            if not (
                self.config.has_section(section)
                and self.config.has_option(section, key)
            ):
                lines.append(stripped)
            elif spec is None or self._should_write(section, key):
                lines.append(self._format_option(section, key))
            elif (section, key) in self._baseline:
                reason = {
                    "limit": "lowered to fit [limits]",
                    "measured": "from the connection test",
                }.get(
                    self._automatic_sources.get((section, key)),
                    "set for this PC",
                )
                lines.append(
                    f"# ({reason}; the packaged default is "
                    f"{spec.default_text})"
                )
                lines.append(f"# {key} = {self._baseline[(section, key)]}")
            else:
                lines.append(f"# {key} = {spec.default_text}")
        flush_comments()
        flush_extras(section)

        for sect in self.config.sections():
            if sect in written:
                continue
            if lines and lines[-1] != '':
                lines.append('')
            lines.append(f"[{sect}]")
            written[sect] = set()
            flush_extras(sect)
        return "\n".join(lines) + "\n"

    def _format_option(self, section, key):
        value = self.config.get(section, key, raw=True, fallback=None)
        if value is None:
            return key
        value = str(value).replace('\n', '\n\t')
        return f"{key} = {value}"


    def set_config(self):
        # Push info from AOConfig into ConfigParser object

        for sect in self.config.sections():
            foo = self.__dict__.get(sect)
            for k,v in foo.__dict__.items():
                if k.startswith('#'):
                    continue
                self.config[sect][k] = str(v)

# ---------------------------------------------------------------------------
# Provider throughput settings
# ---------------------------------------------------------------------------
# Each entry maps a modern setting to its packaged default, its clamp range and
# the legacy key that used to control the same behaviour.  Legacy keys stay
# authoritative only while the modern key is untouched, so an explicit new
# value is never silently overridden by a stale legacy one.
PROVIDER_SETTINGS = {
    "provider_max_in_flight": {
        "default": 320,
        "range": (8, 1024),
        "legacy": ("max_concurrent_downloads", 256),
    },
    "provider_max_connections": {
        "default": 160,
        "range": (1, 256),
        "legacy": ("http2_max_connections", 64),
    },
    "download_dispatch_workers": {
        "default": 4,
        "range": (1, 16),
        "legacy": None,
    },
    "provider_queue_timeout": {
        "type": float,
        "default": 60.0,
        "range": (5.0, 600.0),
        "legacy": None,
    },
    # Adaptive per-origin concurrency (enforced by the broker server).
    "provider_adaptive_concurrency": {
        "type": bool,
        "default": True,
        "legacy": None,
    },
    "provider_adaptive_controller_v2": {
        "type": bool,
        "default": False,
        "legacy": None,
    },
    "provider_transport_trace": {
        "type": bool,
        "default": False,
        "legacy": None,
    },
    "provider_origin_initial_concurrency": {
        "default": 128,
        "range": (0, 1024),
        "legacy": None,
    },
    "provider_origin_min_concurrency": {
        "default": 64,
        "range": (1, 256),
        "legacy": None,
    },
    "provider_origin_max_concurrency": {
        "default": 0,  # 0 => use provider_max_in_flight as the ceiling
        "range": (0, 1024),
        "legacy": None,
    },
    "provider_origin_increase_step": {
        "default": 1,
        "range": (1, 64),
        "legacy": None,
    },
    "provider_origin_success_threshold": {
        "default": 8,
        "range": (1, 4096),
        "legacy": None,
    },
    "provider_origin_decrease_factor": {
        "type": float,
        "default": 0.5,
        "range": (0.1, 0.95),
        "legacy": None,
    },
    "provider_origin_cooldown_seconds": {
        "type": float,
        "default": 5.0,
        "range": (0.0, 60.0),
        "legacy": None,
    },
}


_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n"})


def _coerce_setting(value, kind, key):
    """Parse one configured value, or raise ValueError for a bad one."""

    if kind is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
        raise ValueError(f"{value!r} is not a boolean")
    if kind is float:
        return float(str(value).strip())
    return int(str(value).strip())


def _read_setting(section, key, default, kind=int):
    if section is None:
        return default
    value = getattr(section, key, None)
    if value is None:
        return default
    try:
        return _coerce_setting(value, kind, key)
    except (TypeError, ValueError):
        log.warning(
            "Ignoring invalid value for autoortho.%s: %r", key, value
        )
        return default


def _read_int_setting(section, key, default):
    return _read_setting(section, key, default, int)


def resolve_provider_setting(name, cfg=None):
    """Resolve a provider throughput setting, honouring legacy aliases.

    ``cfg`` may be any object exposing an ``autoortho`` attribute; it defaults
    to the process-wide :data:`CFG`.  Unknown or malformed values fall back to
    the packaged default instead of raising, because this is called from hot
    startup paths where a bad config must not abort the download stack.
    """

    spec = PROVIDER_SETTINGS[name]
    default = spec["default"]
    kind = spec.get("type", int)
    config_object = cfg if cfg is not None else CFG
    section = getattr(config_object, "autoortho", None)
    explicit_options = getattr(
        config_object,
        "_explicit_options",
        {},
    ).get("autoortho", set())

    if kind is bool:
        return _read_setting(section, name, default, bool)

    low, high = spec["range"]
    value = _read_setting(section, name, default, kind)
    if name in explicit_options or value != default:
        return max(low, min(high, value))

    legacy = spec["legacy"]
    if legacy is not None:
        legacy_key, legacy_default = legacy
        legacy_value = _read_setting(section, legacy_key, legacy_default, kind)
        if legacy_value != legacy_default:
            if name == "provider_max_in_flight" and legacy_value > 256:
                log.warning(
                    "Clamping legacy autoortho.%s=%s to 256 while migrating "
                    "to %s; set the modern key explicitly to opt into a "
                    "higher tested limit",
                    legacy_key,
                    legacy_value,
                    name,
                )
                legacy_value = 256
            log.info(
                "Using legacy setting autoortho.%s=%s for %s",
                legacy_key,
                legacy_value,
                name,
            )
            return max(low, min(high, legacy_value))

    return max(low, min(high, default))


def _connection_module():
    if __package__:
        from autoortho import connection_test
    else:
        import connection_test
    return connection_test


def _connection_for(cfg):
    """The last connection test, when it measured this provider."""
    try:
        module = _connection_module()
        result = module.load_result()
    except Exception:
        return None
    if result is None:
        return None
    provider = module.provider_for(
        getattr(getattr(cfg, "autoortho", None), "maptype_override", "")
    )
    return result if result.provider == provider else None


def _connection_warm_start(cfg):
    """Starting download concurrency from the last connection test, as text.

    The adaptive controller still moves from there; this only spares it the
    climb (or the overload) at the start of every flight. A test of another
    provider says nothing about this one's servers, so it is not used.
    """
    result = _connection_for(cfg)
    if result is None or result.suggested <= 0:
        return None
    low, high = PROVIDER_SETTINGS["provider_origin_initial_concurrency"]["range"]
    try:
        floor = int(resolve_provider_setting("provider_origin_min_concurrency", cfg))
        ceiling = int(resolve_provider_setting("provider_max_in_flight", cfg))
    except Exception:
        floor, ceiling = 1, high
    value = max(floor, min(ceiling, int(result.suggested)))
    return str(max(max(1, low), min(high, value)))


# A chunk's typical round trip when no connection test says otherwise:
# 16 KB in about 0.13 s.
_DEFAULT_REQUEST_BYTES_PER_S = 16_000 / 0.13


def _bandwidth_concurrency_ceiling(cfg, bytes_per_second):
    """Downloads at once a bandwidth limit needs, with half again to spare.

    More at once than that only queues inside the rate limiter. Text, or
    None when no limit applies.
    """
    if not bytes_per_second:
        return None
    per_request = _DEFAULT_REQUEST_BYTES_PER_S
    result = _connection_for(cfg)
    if result is not None and result.suggested > 0:
        chosen = next(
            (r for r in result.rounds if r.at_once == result.suggested), None
        )
        if chosen is not None and chosen.chunks and chosen.mean_chunk_seconds > 0:
            per_request = (chosen.bytes / chosen.chunks) / chosen.mean_chunk_seconds
    needed = int(bytes_per_second / max(1.0, per_request) * 1.5) + 1
    try:
        floor = int(resolve_provider_setting("provider_origin_min_concurrency", cfg))
        ceiling = int(resolve_provider_setting("provider_max_in_flight", cfg))
    except Exception:
        floor, ceiling = 1, 1024
    return str(max(floor, min(ceiling, needed)))


def _migrate_config_revision(cfg, user_config):
    """Apply one-time default changes to configs saved by older releases.

    Before revision 2 every save wrote every setting, so a saved value cannot
    tell a user's choice from an old default. The revision marks which changes
    a file has already seen; each step runs once and the next save records the
    new revision. From revision 2 the file is sparse (only custom settings are
    active), so a changed default reaches every user who never set it and new
    steps are rarely needed.
    """
    try:
        revision = int(
            user_config.get("general", "config_revision", fallback="0")
        )
    except (TypeError, ValueError):
        revision = 0
    if revision >= CONFIG_REVISION:
        return
    if revision < 1:
        # 3.0.1: strict native partial builds rejected a whole row when one
        # chunk was late, which served large areas as missing color.
        current = cfg.config.get(
            "autoortho",
            "native_partial_allow_incomplete",
            fallback="True",
        )
        if str(current).strip().lower() in SectionParser.false:
            cfg.config.set(
                "autoortho",
                "native_partial_allow_incomplete",
                "True",
            )
            log.info(
                "Enabled autoortho.native_partial_allow_incomplete "
                "(new default; rows that miss their deadline now use "
                "lower-resolution imagery instead of the missing color)"
            )
    if revision < 2:
        # Sparse format: settings equal to their default become commented
        # "# key = default" lines at the next save (the loaded file had no
        # explicit choices recorded, see AOConfig.load), and retired settings
        # are dropped.
        log.info(
            "Config will now store only changed settings; values equal to "
            "their defaults follow future default changes"
        )
    cfg.config.set("general", "config_revision", str(CONFIG_REVISION))


def _file_revision(user_config):
    try:
        return int(user_config.get("general", "config_revision", fallback="0"))
    except (TypeError, ValueError):
        return 0


def _migrate_legacy_provider_options(cfg, user_config):
    """Persist deprecated provider aliases once instead of resolving per call."""
    section_name = "autoortho"
    explicit = cfg._explicit_options.setdefault(section_name, set())
    if not user_config.has_section(section_name):
        return

    for modern_name, spec in PROVIDER_SETTINGS.items():
        legacy = spec.get("legacy")
        if legacy is None:
            continue
        legacy_name, legacy_default = legacy
        if legacy_name not in explicit:
            continue

        kind = spec.get("type", int)
        parsed_section = SectionParser(
            **dict(cfg.config.items(section_name))
        )
        legacy_value = _read_setting(
            parsed_section,
            legacy_name,
            legacy_default,
            kind,
        )
        if legacy_value == legacy_default:
            continue

        if modern_name in explicit:
            cfg.config.set(
                section_name,
                legacy_name,
                str(legacy_default),
            )
            log.debug(
                "Normalized deprecated autoortho.%s because modern %s is "
                "present",
                legacy_name,
                modern_name,
            )
            continue

        low, high = spec["range"]
        migrated_value = max(low, min(high, legacy_value))
        if modern_name == "provider_max_in_flight":
            migrated_value = min(256, migrated_value)
        cfg.config.set(
            section_name,
            modern_name,
            str(migrated_value),
        )
        cfg.config.set(
            section_name,
            legacy_name,
            str(legacy_default),
        )
        explicit.add(modern_name)
        log.warning(
            "Migrated deprecated autoortho.%s=%s to %s=%s",
            legacy_name,
            legacy_value,
            modern_name,
            migrated_value,
        )


CFG = AOConfig()


# Note: The test code below is obsolete and has been commented out
# if __name__ == "__main__":
#     aoc = AOConfig()
#     cfgui = ConfigUI(aoc)  # ConfigUI is not imported in this module
#     cfgui.setup()
#     cfgui.verify()

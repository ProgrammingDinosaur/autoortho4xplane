#!/usr/bin/env python3

import os
import ast
import pprint
import configparser
from types import SimpleNamespace

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.utils.constants import system_type
except ImportError:
    from utils.constants import system_type

import logging
log = logging.getLogger(__name__)

class SectionParser(object):
    true = ['true','1', 'yes', 'on']
    false = ['false', '0', 'no', 'off']

    def __init__(self, /, **kwargs):
        for k, v in kwargs.items():
            # Normalize to string for parsing while tolerating None
            sv = '' if v is None else str(v)
            s = sv.strip()

            # Detect booleans
            if s.lower() in self.true:
                parsed_val = True
            elif s.lower() in self.false:
                parsed_val = False
            # Detect list
            elif s.startswith('[') and s.endswith(']'):
                try:
                    parsed_val = ast.literal_eval(s)
                except Exception:
                    parsed_val = s
            else:
                parsed_val = s

            self.__dict__.update({k: parsed_val})

    def __repr__(self):
        items = (f"{k}={v!r}" for k, v in self.__dict__.items())
        return "{}({})".format(type(self).__name__, ", ".join(items))

    def __eq__(self, other):
        if isinstance(self, SimpleNamespace) and isinstance(other, SimpleNamespace):
           return self.__dict__ == other.__dict__
        return NotImplemented


class AOConfig(object):
    config = configparser.ConfigParser(strict=False, allow_no_value=True, comment_prefixes='/')


    _defaults = f"""
[general]
# Use GUI config at startup
gui = True
# Show config setup at startup everytime
showconfig = True
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
# Per-chunk maximum wait time in seconds. This limits how long to wait for a SINGLE
# chunk download before moving on. Works in combination with tile_time_budget:
# - tile_time_budget: Total time for entire tile (all chunks combined)
# - maxwait: Maximum time per individual chunk download
# A chunk download will end when EITHER limit is reached, whichever comes first.
# This prevents a single slow chunk from consuming the entire tile budget.
# Recommended: 2.0 (fast networks), 5.0 (normal), 10.0 (slow networks)
maxwait = 5.0
# Temporarily increase maxwait to an effectively infinite value while X-Plane is
# loading scenery data prior to starting the flight.  This allows more downloads to
# succeed and reduce the use of backup chunks and missing chunks at the start of flight.
suspend_maxwait = True
# Use time budget system for tile requests. When enabled, the tile_time_budget
# value represents the total wall-clock time for an ENTIRE tile (all mipmaps),
# providing more predictable loading times. When disabled, falls back to legacy
# per-chunk maxwait timing.
use_time_budget = True
# Maximum wall-clock time in seconds for a COMPLETE tile (all 5 mipmap levels).
# IMPORTANT: This measures ACTIVE PROCESSING time only - queue wait time doesn't count.
# The budget starts when chunks actually begin downloading, not when the tile is first requested.
# This ensures fair time allocation when many tiles are requested simultaneously.
# After this time, the tile is built with whatever has been downloaded.
# Lower = faster loading, but may have more partial/blurry tiles
# Higher = better quality, but longer initial load times
# Recommended: 60.0 (fast), 120.0 (balanced), 300.0 (quality)
tile_time_budget = 180.0
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
# Spatial prefetching - proactively downloads tiles ahead of aircraft
# Enable/disable prefetching (True/False)
prefetch_enabled = True
# How far ahead to prefetch in minutes of flight time (1-60)
# Higher = more tiles prefetched ahead, uses more bandwidth and memory
# Lower = fewer tiles prefetched, less resource usage
# Recommended: 15 (fast aircraft), 30 (balanced), 60 (slow internet or long haul)
# Set to 0 for Unlimited lookahead (continues until max_chunks or other limits)
prefetch_lookahead = 30
# How often to check for prefetch opportunities in seconds (1-10)
prefetch_interval = 2.0
# Maximum chunks to prefetch per cycle (8-512)
prefetch_max_chunks = 32
# Prefetch radius in nautical miles (10-150)
# Tiles within this radius of the flight path are prefetched
# Used by both velocity-based and SimBrief prefetching
prefetch_radius_nm = 40
# Predictive DDS generation - pre-build DDS textures in background after prefetch
# When enabled, tiles are compressed to DDS in the background, eliminating stutters
# when X-Plane loads new scenery areas. Falls back gracefully on cache miss.
predictive_dds_enabled = True
# Maximum memory for pre-built DDS cache in MB (128-2048)
# Higher = more tiles cached, fewer stutters, more RAM used
# Lower = fewer tiles cached, more potential stutters, less RAM used
# Recommended: 256 (low RAM), 512 (balanced), 1024 (high RAM)
predictive_dds_cache_mb = 512
# Minimum interval between DDS builds in milliseconds (100-2000)
# Rate limits background builds to prevent CPU spikes during flight
# Lower = faster building, higher CPU usage
# Higher = slower building, lower CPU usage
# Recommended: 250 (fast CPU), 500 (balanced), 1000 (low-end CPU)
predictive_dds_build_interval_ms = 500
# Apply fallbacks when pre-building DDS (True/False)
# True (default): Apply same fallback logic as live requests (cache search, lower zoom, etc.)
#   - Pro: Prebuilt tiles have best possible quality with fallbacks for failed chunks
#   - Con: Prebuilds may do extra disk/network I/O to find fallback data
# False: Use missing color for failed chunks (no fallbacks, fastest)
#   - Pro: Faster prebuilds, no extra I/O
#   - Con: Failed chunks show missing color instead of fallback data
predictive_dds_use_fallbacks = True
# DDS builder worker count (0 = auto-detect based on CPU cores)
# With Python 3.14 free-threading: uses half of CPU cores
# With Python <= 3.13 (GIL): uses 1 worker (single-threaded)
dds_builder_workers = 0
# Maximum concurrent decode operations (0 = auto-detect)
# Higher values benefit from Python 3.14 free-threading
max_decode_concurrency = 0
# Cache writer worker count (0 = auto-detect, typically 2-8)
cache_writer_workers = 0
fetch_threads = 32
# Simheaven compatibility mode.
simheaven_compat = False
# Using custom generated Ortho4XP tiles along with AutoOrtho.
using_custom_tiles = False
# Color used for missing textures.
missing_color = [66, 77, 55]

[pydds]
# ISPC or STB for dds file compression
compressor = ISPC
# BC1 or BC3 for dxt1 or dxt5 respectively
format = BC1

[scenery]
# Don't cleanup downloads
noclean = False

[fuse]
# Enable or disable multi-threading when using FUSE
threading = True
# NOTE: build_timeout (FUSE lock timeout) is calculated dynamically based on
# tile_time_budget. Formula: tile_time_budget + fallback_timeout (if enabled) + 15s.
# This ensures the lock timeout always exceeds the maximum possible tile build time.

[flightdata]
# Local port for map and stats
webui_port = 5000
# UDP port XPlane listens on
xplane_udp_port = 49000

[cache]
# Max size of the image disk cache in GB. Minimum of 10GB
file_cache_size = 30
# Max size of memory cache in GB. Minimum of 2GB.
cache_mem_limit = 4
# Auto clean cache on AutoOrtho exit
auto_clean_cache = False

[seasons]
seasons_convert_workers = 4
enabled = False
spr_saturation = 70.0
sum_saturation = 100.0
fal_saturation = 80.0
win_saturation = 55.0
compress_dsf = True

[windows]
prefer_winfsp = True

[time_exclusion]
# Enable time-based AutoOrtho exclusion. When active during the specified time range,
# AutoOrtho's scenery will be hidden and X-Plane will use its default scenery instead.
enabled = False
# Start time for exclusion in 24-hour format (HH:MM), e.g. "22:00" for 10 PM
start_time = 23:00
# End time for exclusion in 24-hour format (HH:MM), e.g. "06:00" for 6 AM
end_time = 05:00
# When enabled, assume exclusion is active until sim time is available.
# Useful to ensure night flights start with default scenery from the beginning.
# When disabled, AutoOrtho works normally until sim time confirms exclusion.
default_to_exclusion = False

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
        if not conf_file:
            self.conf_file = os.path.join(os.path.expanduser("~"), ".autoortho")
        else:
            self.conf_file = conf_file

        # Always load initially
        self.ready = self.load()
        
        # Save to update new defaults, but ONLY in the main process.
        # macfuse worker subprocesses must NOT save config to avoid race conditions
        # that can overwrite the main process's configuration and reset user settings.
        # Workers are identified by the AO_RUN_MODE environment variable set during launch.
        if os.environ.get("AO_RUN_MODE") != "macfuse_worker":
            self.save()


    def load(self):
        self.config.read_string(self._defaults)
        if os.path.isfile(self.conf_file):
            log.info(f"Config file found {self.conf_file} reading...")
            self.config.read(self.conf_file)
        else:
            log.info("No config file found. Using defaults...")

        self.get_config()
        return True


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


        if not os.path.exists(self.ao_scenery_path):
            log.info(f"Creating dir {self.ao_scenery_path}")
            os.makedirs(self.ao_scenery_path)

        # If we patched any values during load, persist them now so next run is stable.
        # Only save in the main process - workers must not write config files.
        if getattr(self, "_patched_during_load", False):
            if os.environ.get("AO_RUN_MODE") != "macfuse_worker":
                try:
                    self.save()
                except Exception as e:
                    log.error(f"Failed to persist patched config defaults: {e}")
            self._patched_during_load = False
        return


    def save(self):
        log.info("Saving config ... ")
        self.set_config()

        with open(self.conf_file, 'w') as h:
            self.config.write(h)
        log.info(f"Wrote config file: {self.conf_file}")


    def set_config(self):
        # Push info from AOConfig into ConfigParser object

        for sect in self.config.sections():
            foo = self.__dict__.get(sect)
            for k,v in foo.__dict__.items():
                if k.startswith('#'):
                    continue
                self.config[sect][k] = str(v)

CFG = AOConfig()

# Note: The test code below is obsolete and has been commented out
# if __name__ == "__main__":
#     aoc = AOConfig()
#     cfgui = ConfigUI(aoc)  # ConfigUI is not imported in this module
#     cfgui.setup()
#     cfgui.verify()

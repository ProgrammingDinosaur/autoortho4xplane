#!/usr/bin/env python3

import os
import ast
import pprint
import configparser
from types import SimpleNamespace
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
# Max time to wait for images.  Higher numbers mean better quality, but more
# stutters.  Lower numbers will be more responsive at the expense of
# ocassional low quality tiles.
maxwait = 0.5
# Temporarily increase maxwait to an effectively infinite value while X-Plane is
# loading scenery data prior to starting the flight.  This allows more downloads to
# succeed and reduce the use of backup chunks and missing chunks at the start of flight.
suspend_maxwait = True
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
# Maximum number of concurrent JPEG decode threads (default: CPU count)
max_decode_concurrency = {os.cpu_count() or 1}

[scenery]
# Don't cleanup downloads
noclean = False

[fuse]
# Enable or disable multi-threading when using FUSE
threading = {False if system_type == "darwin" else True}

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
"""

    def __init__(self, conf_file=None):
        if not conf_file:
            self.conf_file = os.path.join(os.path.expanduser("~"), ".autoortho")
        else:
            self.conf_file = conf_file

        # Always load initially
        self.ready = self.load()
        # Save to update new defaults
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

        # If we patched any values during load, persist them now so next run is stable
        if getattr(self, "_patched_during_load", False):
            try:
                self.save()
            except Exception as e:
                log.error(f"Failed to persist patched config defaults: {e}")
            finally:
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

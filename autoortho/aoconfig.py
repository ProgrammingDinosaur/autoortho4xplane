#!/usr/bin/env python3

import os
import ast
import pprint
import configparser
import platform

import logging
log = logging.getLogger(__name__)

class SectionParser(object):
    true = ['true','1', 'yes', 'on']
    false = ['false', '0', 'no', 'off']

    def __init__(self, /, **kwargs):
        for k,v in kwargs.items():
            # Detect booleans
            if v.lower() in self.true:
                v = True
            elif v.lower() in self.false:
                v = False
            # Detect list
            elif v.startswith('[') and v.endswith(']'):
                v = ast.literal_eval(v)

            self.__dict__.update({k:v})

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
# Debug mode
debug = False

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
fetch_threads = 32
# Simheaven compatibility mode.
simheaven_compat = False
# Using custom generated Ortho4XP tiles along with AutoOrtho.
using_custom_tiles = False
# Shows the downloaded tiles info before loading into a flight.
show_downloaded_tiles = False

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
# Max size of memory cache in GB. Minimmum of 2GB.
cache_mem_limit = 4
# Auto clean cache on AutoOrtho exit
auto_clean_cache = False

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


    def get_config(self):
        # Pull info from ConfigParser object into AOConfig

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
        
        if platform.system() == "Darwin":
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

if __name__ == "__main__":
    aoc = AOConfig()
    cfgui = ConfigUI(aoc)
    cfgui.setup()
    cfgui.verify()

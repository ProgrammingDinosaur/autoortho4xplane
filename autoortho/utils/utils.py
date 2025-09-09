""" module to hold utility functions used throughout the project """
import math
import psutil
from functools import lru_cache


def is_xplane_running():
    """ check if xplane is running """
    # Process is called "X-Plane"
    for proc in psutil.process_iter():
        if "X-Plane" in proc.name():
            return True
    return False


@lru_cache(maxsize=2048)
def coord_from_sleepy_tilename(row, col, zoom):
    """ get coordinate from gtile """
    n = pow(2, zoom)
    lon_deg = row / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * col / n)))
    lat_deg = lat_rad * 180.0 / math.pi
    return math.floor(lat_deg), math.floor(lon_deg)

""" module to hold utility functions used throughout the project """
import math
import os
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


def scan_existing_tiles(search_path: str) -> dict[str, set[str]]:
    """ scan existing tiles """
    print("SCANNING EXISTING TILES")
    base_ao_sceneries_path = os.path.join(search_path, "z_autoortho", "scenery")
    existing_tiles = {}
    if not os.path.exists(base_ao_sceneries_path):
        return existing_tiles
    for base_package_path in os.listdir(base_ao_sceneries_path):
        package_path = os.path.join(base_ao_sceneries_path, base_package_path)
        if not os.path.exists(package_path) or not os.path.isdir(package_path):
            continue
        base_dsf_paths = os.path.join(package_path, "Earth Nav Data")
        if not os.path.exists(base_dsf_paths) or not os.path.isdir(base_dsf_paths):
            continue
        for base_dsf_dir_path in os.listdir(base_dsf_paths):
            dsf_path = os.path.join(base_dsf_paths, base_dsf_dir_path)
            if not os.path.exists(dsf_path) or not os.path.isdir(dsf_path):
                continue
            for dsf_file in os.listdir(dsf_path):
                category = "Base AO Package" if base_package_path.startswith("z_ao_") else "Custom Tile"
                coord = dsf_file.split(".")[0]
                if coord not in existing_tiles or category == "Custom Tile":
                    existing_tiles[coord] = {
                        "type": category,
                        "package": base_package_path,
                    }

    return existing_tiles


def get_main_dsf_folder_from_coord(latitude: int, longitude: int) -> str:
    """ get the main dsf folder from a coordinate """
    latitude_10 = math.ceil(latitude / 10) * 10
    longitude_10 = math.ceil(longitude / 10) * 10
    return f"{latitude_10:+03d}{longitude_10:+04d}"


def get_dsf_name_from_coord(latitude: int, longitude: int) -> str:
    """ get the dsf name from a coordinate """
    latitude_10 = math.floor(latitude)
    longitude_10 = math.floor(longitude)
    return f"{latitude_10:+03d}{longitude_10:+04d}.dsf"
"""
cache_paths.py - DSF-based path utilities for AutoOrtho cache files

Provides utilities for generating DDS cache file paths organized by DSF tile coordinates.
Cache files are stored in a hierarchy matching X-Plane's scenery structure:
    {cache_dir}/dds_cache/{10°_band}/{1°_tile}/{maptype}/{row}_{col}_z{max_zoom}.dds
"""

import os
import math
from typing import Tuple


def tile_to_lat_lon(row: int, col: int, zoom: int) -> Tuple[float, float]:
    """
    Convert slippy tile coordinates to lat/lon.
    
    Uses Web Mercator projection (EPSG:3857) conversion.
    Returns the northwest corner of the tile.
    
    Args:
        row: Tile row (Y coordinate in slippy tile system)
        col: Tile column (X coordinate in slippy tile system)
        zoom: Zoom level
    
    Returns:
        Tuple of (latitude, longitude) in degrees
    """
    n = 2 ** zoom
    lon = col / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * row / n)))
    lat = math.degrees(lat_rad)
    return lat, lon


def tile_to_dsf_coords(row: int, col: int, zoom: int) -> Tuple[int, int]:
    """
    Convert slippy tile coordinates to DSF 1-degree coordinates.
    
    DSF files are named by their southwest corner in 1-degree increments.
    
    Args:
        row: Tile row (Y coordinate in slippy tile system)
        col: Tile column (X coordinate in slippy tile system)
        zoom: Zoom level
    
    Returns:
        Tuple of (lat_floor, lon_floor) - southwest corner of 1-degree DSF tile
    """
    lat, lon = tile_to_lat_lon(row, col, zoom)
    return int(math.floor(lat)), int(math.floor(lon))


def tile_to_dsf_10deg_band(row: int, col: int, zoom: int) -> Tuple[int, int]:
    """
    Convert slippy tile coordinates to 10-degree band coordinates.
    
    X-Plane organizes Earth nav data in 10-degree bands (e.g., +30-130).
    
    Args:
        row: Tile row
        col: Tile column
        zoom: Zoom level
    
    Returns:
        Tuple of (lat_band, lon_band) - 10-degree band coordinates
    """
    lat, lon = tile_to_lat_lon(row, col, zoom)
    lat_band = int(math.floor(lat / 10) * 10)
    lon_band = int(math.floor(lon / 10) * 10)
    return lat_band, lon_band


def format_dsf_name(lat: int, lon: int) -> str:
    """
    Format DSF 1-degree tile name.
    
    Args:
        lat: Latitude (integer, floor of actual lat)
        lon: Longitude (integer, floor of actual lon)
    
    Returns:
        Formatted DSF name like "+37-122"
    """
    return f"{lat:+03d}{lon:+04d}"


def format_dsf_band(lat_band: int, lon_band: int) -> str:
    """
    Format 10-degree band name.
    
    Args:
        lat_band: Latitude band (multiple of 10)
        lon_band: Longitude band (multiple of 10)
    
    Returns:
        Formatted band name like "+30-130"
    """
    return f"{lat_band:+03d}{lon_band:+04d}"


def get_dds_cache_dir(cache_dir: str, row: int, col: int, zoom: int, maptype: str) -> str:
    """
    Get the directory path for a cached DDS file based on DSF coordinates.
    
    Directory structure:
        {cache_dir}/dds_cache/{10°_band}/{1°_tile}/{maptype}/
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        zoom: Zoom level (used for coordinate conversion)
        maptype: Map source identifier (e.g., "BI", "APPLE")
    
    Returns:
        Directory path for the DDS cache file
    """
    lat_band, lon_band = tile_to_dsf_10deg_band(row, col, zoom)
    lat_1deg, lon_1deg = tile_to_dsf_coords(row, col, zoom)
    
    band_name = format_dsf_band(lat_band, lon_band)
    tile_name = format_dsf_name(lat_1deg, lon_1deg)
    
    return os.path.join(cache_dir, "dds_cache", band_name, tile_name, maptype)


def get_dds_cache_path(cache_dir: str, row: int, col: int,
                       maptype: str, zoom: int, max_zoom: int) -> str:
    """
    Get the full path for a cached DDS file.
    
    Path format:
        {cache_dir}/dds_cache/{10°_band}/{1°_tile}/{maptype}/{row}_{col}_z{max_zoom}.dds
    
    The _z{max_zoom} suffix distinguishes DDS files for different effective
    zoom levels of the same tile (e.g., 4096x4096 at ZL16 vs 8192x8192 at ZL17).
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        maptype: Map source identifier
        zoom: Zoom level (used for coordinate conversion to find DSF tile)
        max_zoom: Effective maximum zoom level for DDS dimensions
    
    Returns:
        Full path to the DDS cache file (without extension - caller appends .dds or .ddm)
    """
    dds_dir = get_dds_cache_dir(cache_dir, row, col, zoom, maptype)
    return os.path.join(dds_dir, f"{row}_{col}_z{max_zoom}")


def tile_row_col_to_dsf_key(row: int, col: int, zoom: int) -> str:
    """
    Convert tile row/col to DSF key string.
    
    This matches the format used in getortho.py for season file keys.
    
    Args:
        row: Tile row
        col: Tile column
        zoom: Zoom level
    
    Returns:
        DSF key string like "+37-122"
    """
    lat, lon = tile_to_lat_lon(row, col, zoom)
    lat_i = int(math.floor(lat))
    lon_i = int(math.floor(lon))
    return f"{lat_i:+03d}{lon_i:+04d}"

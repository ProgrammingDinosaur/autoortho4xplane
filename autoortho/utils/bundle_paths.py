"""
bundle_paths.py - DSF-based path utilities for AOB2 bundle files

Provides utilities for generating bundle file paths organized by DSF tile coordinates.
Bundle files are stored in a hierarchy matching X-Plane's scenery structure:
    {cache_dir}/bundles/{10°_band}/{1°_tile}/{maptype}/{row}_{col}.aob2

Example:
    cache/bundles/+30-130/+37-122/APPLE/10880_10432.aob2
"""

import os
import math
from typing import Tuple, Optional


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
    # Floor to nearest 10 degrees
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


def get_bundle2_dir(cache_dir: str, row: int, col: int, zoom: int, maptype: str) -> str:
    """
    Get the directory path for a bundle file based on DSF coordinates.
    
    Directory structure:
        {cache_dir}/bundles/{10°_band}/{1°_tile}/{maptype}/
    
    Example:
        cache/bundles/+30-130/+37-122/APPLE/
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        zoom: Zoom level (used for coordinate conversion)
        maptype: Map source identifier (e.g., "BI", "APPLE")
    
    Returns:
        Directory path for the bundle file
    """
    lat_band, lon_band = tile_to_dsf_10deg_band(row, col, zoom)
    lat_1deg, lon_1deg = tile_to_dsf_coords(row, col, zoom)
    
    band_name = format_dsf_band(lat_band, lon_band)
    tile_name = format_dsf_name(lat_1deg, lon_1deg)
    
    return os.path.join(cache_dir, "bundles", band_name, tile_name, maptype)


def get_bundle2_filename(row: int, col: int) -> str:
    """
    Get the bundle filename (without directory).
    
    Format: {row}_{col}.aob2
    
    Note: Zoom level is NOT included in the filename since bundles
    can contain multiple zoom levels. Maptype is encoded in the
    parent directory structure.
    
    Args:
        row: Tile row
        col: Tile column
    
    Returns:
        Bundle filename like "10880_10432.aob2"
    """
    return f"{row}_{col}.aob2"


def get_bundle2_path(cache_dir: str, row: int, col: int, 
                     maptype: str, zoom: int) -> str:
    """
    Get the full path for a bundle file.
    
    Path format:
        {cache_dir}/bundles/{10°_band}/{1°_tile}/{maptype}/{row}_{col}.aob2
    
    Example:
        cache/bundles/+30-130/+37-122/APPLE/10880_10432.aob2
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        maptype: Map source identifier
        zoom: Zoom level (used for coordinate conversion to find DSF tile)
    
    Returns:
        Full path to the bundle file
    """
    bundle_dir = get_bundle2_dir(cache_dir, row, col, zoom, maptype)
    filename = get_bundle2_filename(row, col)
    return os.path.join(bundle_dir, filename)


def ensure_bundle2_dir(cache_dir: str, row: int, col: int, zoom: int, maptype: str) -> str:
    """
    Ensure the bundle directory exists and return its path.
    
    Creates the directory hierarchy if it doesn't exist.
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        zoom: Zoom level
        maptype: Map source identifier (e.g., "BI", "APPLE")
    
    Returns:
        Path to the bundle directory (now guaranteed to exist)
    """
    bundle_dir = get_bundle2_dir(cache_dir, row, col, zoom, maptype)
    os.makedirs(bundle_dir, exist_ok=True)
    return bundle_dir


def bundle_exists(cache_dir: str, row: int, col: int, 
                  maptype: str, zoom: int) -> bool:
    """
    Check if a bundle file exists.
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        maptype: Map source identifier
        zoom: Zoom level
    
    Returns:
        True if the bundle file exists
    """
    path = get_bundle2_path(cache_dir, row, col, maptype, zoom)
    return os.path.exists(path)


def get_bundle2_dsf_dir(cache_dir: str, row: int, col: int, zoom: int) -> str:
    """
    Get the DSF tile directory path (without maptype subfolder).
    
    This returns the 1-degree tile directory that contains maptype subfolders.
    
    Directory structure:
        {cache_dir}/bundles/{10°_band}/{1°_tile}/
    
    Example:
        cache/bundles/+30-130/+37-122/
    
    Args:
        cache_dir: Base cache directory
        row: Tile row
        col: Tile column
        zoom: Zoom level (used for coordinate conversion)
    
    Returns:
        DSF tile directory path
    """
    lat_band, lon_band = tile_to_dsf_10deg_band(row, col, zoom)
    lat_1deg, lon_1deg = tile_to_dsf_coords(row, col, zoom)
    
    band_name = format_dsf_band(lat_band, lon_band)
    tile_name = format_dsf_name(lat_1deg, lon_1deg)
    
    return os.path.join(cache_dir, "bundles", band_name, tile_name)


def get_bundle2_path_from_tile_id(cache_dir: str, tile_id: str, zoom: int) -> str:
    """
    Get bundle path from a tile ID string.
    
    Tile ID format: "{row}_{col}_{maptype}" (e.g., "10880_10432_BI")
    
    Note: The tile_id still contains maptype for compatibility with existing code,
    but the resulting path uses maptype as a directory, not in the filename.
    
    Args:
        cache_dir: Base cache directory
        tile_id: Tile identifier string
        zoom: Zoom level
    
    Returns:
        Full path to the bundle file
    
    Raises:
        ValueError: If tile_id format is invalid
    """
    parts = tile_id.rsplit('_', 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid tile_id format: {tile_id}")
    
    try:
        row = int(parts[0])
        col = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid tile_id format (non-integer row/col): {tile_id}")
    
    maptype = parts[2]
    return get_bundle2_path(cache_dir, row, col, maptype, zoom)


def parse_bundle_filename(filename: str) -> Optional[Tuple[int, int]]:
    """
    Parse a bundle filename to extract row and col.
    
    Note: maptype is no longer in the filename; it's in the parent directory.
    
    Args:
        filename: Bundle filename (with or without .aob2 extension)
    
    Returns:
        Tuple of (row, col), or None if parsing fails
    """
    # Remove extension if present
    name = filename
    if name.endswith('.aob2'):
        name = name[:-5]
    
    parts = name.rsplit('_', 1)
    if len(parts) != 2:
        return None
    
    try:
        row = int(parts[0])
        col = int(parts[1])
        return row, col
    except ValueError:
        return None


def enumerate_bundles(cache_dir: str) -> list:
    """
    Enumerate all bundle files in the cache directory.
    
    Walks the bundle directory hierarchy and returns info about each bundle.
    
    Directory structure:
        {cache_dir}/bundles/{10°_band}/{1°_tile}/{maptype}/{row}_{col}.aob2
    
    Args:
        cache_dir: Base cache directory
    
    Returns:
        List of dicts with keys: path, row, col, maptype, band, dsf_tile
    """
    bundles = []
    bundles_root = os.path.join(cache_dir, "bundles")
    
    if not os.path.isdir(bundles_root):
        return bundles
    
    for band_name in os.listdir(bundles_root):
        band_path = os.path.join(bundles_root, band_name)
        if not os.path.isdir(band_path):
            continue
        
        for tile_name in os.listdir(band_path):
            tile_path = os.path.join(band_path, tile_name)
            if not os.path.isdir(tile_path):
                continue
            
            # Maptype is now a subdirectory
            for maptype in os.listdir(tile_path):
                maptype_path = os.path.join(tile_path, maptype)
                if not os.path.isdir(maptype_path):
                    continue
                
                for filename in os.listdir(maptype_path):
                    if not filename.endswith('.aob2'):
                        continue
                    
                    parsed = parse_bundle_filename(filename)
                    if parsed is None:
                        continue
                    
                    row, col = parsed
                    bundles.append({
                        'path': os.path.join(maptype_path, filename),
                        'row': row,
                        'col': col,
                        'maptype': maptype,
                        'band': band_name,
                        'dsf_tile': tile_name,
                    })
    
    return bundles


def get_dds_cache_dir(cache_dir: str, row: int, col: int, zoom: int, maptype: str) -> str:
    """
    Get the directory path for a cached DDS file based on DSF coordinates.
    
    Mirrors the bundle directory hierarchy for consistency.
    
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


# For compatibility with existing code that uses row/col convention
def tile_row_col_to_dsf_key(row: int, col: int, zoom: int) -> str:
    """
    Convert tile row/col to DSF key string (same as _season_tile_key_from_rc).
    
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

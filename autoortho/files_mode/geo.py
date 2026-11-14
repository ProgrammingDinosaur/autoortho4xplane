"""Texture geometry from texture names.

An AutoOrtho or Ortho4XP texture ``<row>_<col>_<maptype><zoom>.dds`` covers
16 x 16 slippy-map chunks at ``zoom``, with its top-left chunk at
(``col``, ``row``). Its centre and ground size therefore follow from the
name alone; they equal the ``LOAD_CENTER`` the ``.ter`` file states (checked
in the tests), so the catalogue never has to open a ``.ter`` file.
"""

import math
import re

EARTH_CIRCUMFERENCE_M = 40075016.686
EARTH_RADIUS_KM = 6371.0088
CHUNKS_PER_TEXTURE = 16

# <row>_<col>_<maptype><zoom>[_water_overlay|_sea_overlay].ter
# The maptype is matched lazily, so "GO216" reads as maptype GO2, zoom 16.
TER_NAME_RE = re.compile(
    r"^(\d+)_(\d+)_(.*?)(\d{2})(?:_(?:water|sea)_overlay)?\.ter$",
    re.IGNORECASE,
)
DDS_NAME_RE = re.compile(r"^(\d+)_(\d+)_(.*?)(\d{2})\.dds$", re.IGNORECASE)


def texture_name(row, col, maptype, zoom):
    return f"{int(row)}_{int(col)}_{maptype}{int(zoom)}.dds"


def parse_texture_name(name):
    """``(row, col, maptype, zoom)`` of a ``.dds`` name, or None."""
    match = DDS_NAME_RE.match(name)
    if not match:
        return None
    row, col, maptype, zoom = match.groups()
    return int(row), int(col), maptype, int(zoom)


def parse_ter_name(name):
    """``(row, col, maptype, zoom)`` of the texture a ``.ter`` name implies."""
    match = TER_NAME_RE.match(name)
    if not match:
        return None
    row, col, maptype, zoom = match.groups()
    return int(row), int(col), maptype, int(zoom)


def tile_y_to_lat(y, zoom):
    n = math.pi - 2.0 * math.pi * y / (1 << zoom)
    return math.degrees(math.atan(math.sinh(n)))


def tile_x_to_lon(x, zoom):
    return x / (1 << zoom) * 360.0 - 180.0


def texture_centre(row, col, zoom):
    """Latitude and longitude of the texture's centre."""
    half = CHUNKS_PER_TEXTURE / 2
    return tile_y_to_lat(row + half, zoom), tile_x_to_lon(col + half, zoom)


def texture_bounds(row, col, zoom):
    """``(lat_south, lat_north, lon_west, lon_east)`` of the texture."""
    north = tile_y_to_lat(row, zoom)
    south = tile_y_to_lat(row + CHUNKS_PER_TEXTURE, zoom)
    west = tile_x_to_lon(col, zoom)
    east = tile_x_to_lon(col + CHUNKS_PER_TEXTURE, zoom)
    return south, north, west, east


def texture_size_m(row, col, zoom):
    """East-west ground size of the texture at its centre (``LOAD_CENTER``'s)."""
    lat, _lon = texture_centre(row, col, zoom)
    return (
        EARTH_CIRCUMFERENCE_M
        * math.cos(math.radians(lat))
        / (1 << zoom)
        * CHUNKS_PER_TEXTURE
    )


def squares_of(row, col, zoom):
    """The 1x1 degree DSF squares (south-west corner) the texture touches."""
    south, north, west, east = texture_bounds(row, col, zoom)
    eps = 1e-9
    squares = []
    for lat in range(math.floor(south + eps), math.floor(north - eps) + 1):
        for lon in range(math.floor(west + eps), math.floor(east - eps) + 1):
            squares.append((lat, lon))
    return squares


def haversine_km(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def destination(lat, lon, bearing_deg, distance_km):
    """The point ``distance_km`` from (lat, lon) on ``bearing_deg``."""
    d = distance_km / EARTH_RADIUS_KM
    b = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    lon2 = (math.degrees(l2) + 540.0) % 360.0 - 180.0
    return math.degrees(p2), lon2


def square_name(lat, lon):
    """``+40-120`` style name of a DSF square."""
    return f"{int(lat):+03d}{int(lon):+04d}"


def square_folder(lat, lon):
    """The 10-degree ``Earth nav data`` folder holding a square's DSF."""
    return square_name(math.floor(lat / 10) * 10, math.floor(lon / 10) * 10)

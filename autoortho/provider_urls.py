"""Imagery tile URLs per provider (map type).

Shared by the downloader (``getortho.Chunk``) and the connection test, so a
test measures the same servers streaming uses.
"""

from __future__ import annotations

EOX_MAPID = "s2cloudless-2024_3857"
EOX_MATRIXSET = "g"
USER_AGENT = "curl/7.68.0"

# Providers the connection test can use without a session token.
TESTABLE = ("BI", "GO2", "ARC", "EOX", "NAIP", "USGS", "FIREFLY", "YNDX")
# Providers that only cover the United States.
US_ONLY = ("NAIP", "USGS")


def quadkey(col: int, row: int, zoom: int) -> str:
    digits = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if col & mask:
            digit += 1
        if row & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def tile_url(
    maptype: str,
    col: int,
    row: int,
    zoom: int,
    server_num: int = 0,
    apple_token: str = "",
    apple_version: str = "",
):
    """``(url, headers)`` for one 256 px imagery chunk."""
    kind = str(maptype).upper()
    if kind == "EOX":
        url = (
            f"https://s2maps-tiles.eu/wmts?layer={EOX_MAPID}&style=default"
            f"&tilematrixset={EOX_MATRIXSET}&Service=WMTS&Request=GetTile"
            f"&Version=1.0.0&Format=image%2Fjpeg&TileMatrix={zoom}"
            f"&TileCol={col}&TileRow={row}"
        )
    elif kind == "BI":
        url = (
            "https://t.ssl.ak.tiles.virtualearth.net/tiles/"
            f"a{quadkey(col, row, zoom)}.jpeg?g=15312"
        )
    elif kind == "GO2":
        url = f"http://mts{server_num}.google.com/vt/lyrs=s&x={col}&y={row}&z={zoom}"
    elif kind == "ARC":
        # The http:// endpoint answers every tile with a 301 to https://.
        url = (
            "https://services.arcgisonline.com/ArcGIS/rest/services/"
            f"World_Imagery/MapServer/tile/{zoom}/{row}/{col}"
        )
    elif kind == "NAIP":
        url = (
            "http://naip.maptiles.arcgis.com/arcgis/rest/services/NAIP/"
            f"MapServer/tile/{zoom}/{row}/{col}"
        )
    elif kind == "USGS":
        url = (
            "https://basemap.nationalmap.gov/arcgis/rest/services/"
            f"USGSImageryOnly/MapServer/tile/{zoom}/{row}/{col}"
        )
    elif kind == "FIREFLY":
        url = (
            "https://fly.maptiles.arcgis.com/arcgis/rest/services/"
            f"World_Imagery_Firefly/MapServer/tile/{zoom}/{row}/{col}"
        )
    elif kind == "YNDX":
        url = (
            f"https://sat{server_num + 1:02d}.maps.yandex.net/tiles?l=sat"
            f"&v=3.1814.0&x={col}&y={row}&z={zoom}"
        )
    elif kind == "APPLE":
        url = (
            "https://sat-cdn.apple-mapkit.com/tile?style=7&size=1&scale=1"
            f"&z={zoom}&x={col}&y={row}&v={apple_version}"
            f"&accessKey={apple_token}"
        )
    else:
        raise KeyError(maptype)
    headers = {"user-agent": USER_AGENT}
    if kind == "EOX":
        headers["referer"] = "https://s2maps.eu/"
    return url, headers

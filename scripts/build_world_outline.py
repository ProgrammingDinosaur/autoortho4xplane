"""Pack Natural Earth GeoJSON layers into the compact world outline used by
the in-app Custom Map editor (autoortho/data/world_outline.json.gz).

Natural Earth data is public domain: https://www.naturalearthdata.com/

Usage:
    python scripts/build_world_outline.py LAND.geojson LAKES.geojson \
        BORDERS.geojson [OUTPUT]

Inputs are the 1:50m layers from
https://github.com/nvkelso/natural-earth-vector/tree/master/geojson:
    ne_50m_land.geojson
    ne_50m_lakes.geojson
    ne_50m_admin_0_boundary_lines_land.geojson

Output format (gzip-compressed JSON):
    {"version": 1, "scale": Q,
     "land": [ring, ...], "lakes": [ring, ...], "borders": [line, ...]}
Each ring/line is a flat list of integers: the first point as absolute
(lon*Q, lat*Q), then each later point as a delta from the previous one.
"""

import gzip
import json
import os
import sys

SCALE = 200  # 1/200 degree (~550 m) is finer than the 1:50m source data.


def _encode(coords):
    out = []
    prev_x = prev_y = None
    for lon, lat in coords:
        x = round(lon * SCALE)
        y = round(lat * SCALE)
        if prev_x is None:
            out.extend((x, y))
        elif x != prev_x or y != prev_y:
            out.extend((x - prev_x, y - prev_y))
        else:
            continue
        prev_x, prev_y = x, y
    return out


def _rings(path, polygons):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    result = []
    for feature in data["features"]:
        geom = feature["geometry"]
        if geom is None:
            continue
        kind = geom["type"]
        coords = geom["coordinates"]
        if kind == "Polygon":
            parts = coords
        elif kind == "MultiPolygon":
            parts = [ring for poly in coords for ring in poly]
        elif kind == "LineString":
            parts = [coords]
        elif kind == "MultiLineString":
            parts = coords
        else:
            continue
        for part in parts:
            encoded = _encode(part)
            if len(encoded) >= (6 if polygons else 4):
                result.append(encoded)
    return result


def main(argv):
    if len(argv) not in (4, 5):
        print(__doc__)
        return 2
    land, lakes, borders = argv[1:4]
    output = argv[4] if len(argv) == 5 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "autoortho", "data", "world_outline.json.gz",
    )
    payload = {
        "version": 1,
        "scale": SCALE,
        "source": "Natural Earth 1:50m (public domain)",
        "land": _rings(land, True),
        "lakes": _rings(lakes, True),
        "borders": _rings(borders, False),
    }
    os.makedirs(os.path.dirname(output), exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
    with gzip.GzipFile(output, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(raw)
    points = sum(len(r) // 2 for k in ("land", "lakes", "borders")
                 for r in payload[k])
    print(f"wrote {output}: {os.path.getsize(output):,} bytes, {points:,} points")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Make a synthetic read trace along a straight route, for the replay benchmark.

A recorded trace (``AO_READ_TRACE``, autoortho/read_trace.py) is the real
thing; this makes a stand-in from a mounted scenery's texture names until one
is recorded. X-Plane's reading is imitated as read_pattern.py describes it:

* a texture within ``--radius-km`` of the aircraft is opened once;
* the header chunk is read (the first 64 KB);
* then, from the 64 KB chunk holding the start of the level the paging model
  says X-Plane wants at that distance, every chunk to the end of the file;
* the texture is released when it falls out of range.

The aircraft starts at ``--lat``/``--lon`` and flies ``--heading`` at
``--speed-kt`` and ``--agl-m``, after ``--load-s`` seconds parked (the
loading screen, no position sent). Positions are written once a second.

    python tools/bench/make_scenario.py --mountpoint "X:/.../z_ao_eur" \\
        --lat 62.06 --lon -7.27 --heading 90 --minutes 20 --out cruise.trace
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from autoortho import paging_model  # noqa: E402
from autoortho.disk_texture import mip_layout  # noqa: E402

DDS_RE = re.compile(r"^(\d+)[-_](\d+)[-_]((?!ZL)\D*)(\d{2})\.dds$")
CHUNK = 65536
EARTH_M = 6_371_000.0


def move(lat, lon, heading_deg, distance_m):
    """The point ``distance_m`` from (lat, lon) along ``heading_deg``."""
    d = distance_m / EARTH_M
    h = math.radians(heading_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(h))
    l2 = l1 + math.atan2(math.sin(h) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def textures(textures_dir):
    out = []
    for name in sorted(os.listdir(textures_dir)):
        m = DDS_RE.match(name)
        if not m:
            continue
        row, col, maptype, zoom = int(m[1]), int(m[2]), m[3], int(m[4])
        lat, lon = paging_model.tile_centre(row, col, zoom)
        out.append({
            "path": f"/textures/{name}",
            "lat": lat,
            "lon": lon,
            "size_m": paging_model.tile_size_m(lat, zoom),
            "zoom": zoom,
        })
    return out


def reads_for(size_px, mip, fmt="BC1"):
    """X-Plane's reads of a texture it wants at level ``mip``: (offset, length)."""
    levels = mip_layout(size_px, size_px, fmt)
    end = levels[-1].endpos
    reads = [(0, min(CHUNK, end))]
    mip = max(0, min(len(levels) - 1, int(mip)))
    start = levels[mip].startpos // CHUNK * CHUNK
    if mip == 0:
        start = CHUNK  # the header chunk was already read
    for offset in range(max(start, CHUNK if mip == 0 else start), end, CHUNK):
        reads.append((offset, min(CHUNK, end - offset)))
    return reads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--heading", type=float, default=90.0)
    parser.add_argument("--speed-kt", type=float, default=250.0)
    parser.add_argument("--agl-m", type=float, default=3000.0)
    parser.add_argument("--minutes", type=float, default=20.0)
    parser.add_argument("--load-s", type=float, default=60.0)
    parser.add_argument("--radius-km", type=float, default=60.0)
    parser.add_argument("--size-px", type=int, default=4096)
    parser.add_argument("--k", type=float, default=0.0, help="paging k (0: X-Plane's default screen)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    textures_dir = os.path.join(args.mountpoint, "textures")
    known = textures(textures_dir)
    if not known:
        print(f"No textures in {textures_dir}", file=sys.stderr)
        return 2
    model = paging_model.PagingModel(args.k if args.k > 0 else None)
    events = []
    open_now = {}
    radius = args.radius_km * 1000.0
    speed = args.speed_kt * 1852.0 / 3600.0
    total = args.load_s + args.minutes * 60.0
    second = 0
    while second <= total:
        flying = second >= args.load_s
        moved = speed * (second - args.load_s) if flying else 0.0
        lat, lon = move(args.lat, args.lon, args.heading, moved)
        if flying:
            events.append({
                "t": float(second), "op": "pos", "lat": round(lat, 6), "lon": round(lon, 6),
                "agl_m": args.agl_m, "hdg": args.heading, "gs": round(speed, 1),
            })
        in_range = []
        for tex in known:
            distance = paging_model.camera_distance_m(lat, lon, args.agl_m, tex["lat"], tex["lon"])
            if distance <= radius:
                in_range.append((distance, tex))
        in_range.sort(key=lambda item: item[0])
        t = float(second)
        for distance, tex in in_range:
            if tex["path"] in open_now:
                continue
            mip = model.mip_for(distance, tex["size_m"], args.size_px)
            open_now[tex["path"]] = mip
            events.append({"t": round(t, 3), "op": "open", "path": tex["path"]})
            for offset, length in reads_for(args.size_px, mip):
                t += 0.002
                events.append({"t": round(t, 3), "op": "read", "path": tex["path"],
                               "offset": offset, "length": length})
        names = {tex["path"] for _d, tex in in_range}
        for path in [p for p in open_now if p not in names]:
            open_now.pop(path)
            events.append({"t": float(second) + 0.5, "op": "release", "path": path})
        second += 1
    events.sort(key=lambda e: e["t"])
    with open(args.out, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    opens = sum(1 for e in events if e["op"] == "open")
    reads = sum(1 for e in events if e["op"] == "read")
    print(f"Wrote {args.out}: {opens} opens, {reads} reads over {total:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

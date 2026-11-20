#!/usr/bin/env python3
"""Repack an extracted Ortho4XP scenery package into release zips.

Produces the same layout as kubilus1/autoortho-scenery releases, so the
packages work both for streaming (scripts/build_tile_index.py reads them
remotely) and for AutoOrtho's full-download installer:

    <area>_info.json
    z_<area>_00.zip  z_<area>_00.zip.sha256
    z_<area>_01.zip  ...

Each zip holds whole 1x1 degree tiles: the DSF plus the .ter files and
water masks whose texture centre lies in that tile, under a single top
folder ``z_<area>_NN/``. Zips stay under --max-mb (GitHub's limit is 2 GiB
per file). Files that belong to no tile (README, water_transition.png,
docs) go into the first zip. Inside each zip the DSFs come first, then the
contiguous terrain/ block, then textures/, which keeps remote reads short.

Already-compressed files (7z DSFs, PNGs) are stored; text is deflated.

Example:
    7z x z_ao_greenland_zl16_o4xp1.40.13+je_v1.4.7z -oD:/xp12/extracted
    python scripts/repack_scenery.py D:/xp12/extracted --area greenland \\
        --name "Greenland XP12 (flightwusel v1.4)" --out D:/xp12/release_greenland
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import zipfile
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

DSF_DIR = "Earth nav data"
GITHUB_LIMIT = 2 * 1024 ** 3 - 1
# Left behind by AutoOrtho itself (seasons backups, streaming state).
SKIP_DIRS = {"dsf_backups"}
SKIP_FILES = {"ao_stream.json", "tile_index.json.gz", ".AO_PLACEHOLDER"}
SKIP_SUFFIXES = (".bak", ".tmp")
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
TEX_RE = re.compile(r"^(?P<row>\d+)_(?P<col>\d+)_")
ZOOM_RE = re.compile(r"(?:ZL|[A-Za-z])(?P<zoom>\d{2})(?:[_.]|$)")
DSF_RE = re.compile(r"^(?P<lat>[+-]\d{2})(?P<lon>[+-]\d{3})\.dsf$")

Tile = Tuple[int, int]
DEFAULT_7Z = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "autoortho",
                          "lib", "windows", "7zip", "7za.exe")

# rel path -> file to read instead of root/rel (7z-compressed DSFs).
SOURCE_OVERRIDES: Dict[str, str] = {}


def source_path(root: str, rel: str) -> str:
    return SOURCE_OVERRIDES.get(rel) or os.path.join(root, *rel.split("/"))


def compress_dsfs(root: str, rels: List[str], workdir: str, sevenzip: str,
                  workers: int) -> Tuple[int, int]:
    """7z-compress uncompressed DSFs into ``workdir`` (reused on reruns).

    X-Plane reads 7z-compressed DSFs natively; this makes downloads about a
    fifth smaller than zip deflate and DSFs on disk about a quarter of
    their raw size. Returns (raw bytes, compressed bytes).
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    def one(rel):
        src = os.path.join(root, *rel.split("/"))
        with open(src, "rb") as fh:
            if fh.read(6) == SEVENZIP_MAGIC:
                return os.path.getsize(src), os.path.getsize(src)
        dst = os.path.join(workdir, *rel.split("/"))
        if not (os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".tmp.7z"
            if os.path.exists(tmp):
                os.remove(tmp)
            result = subprocess.run(
                [sevenzip, "a", "-t7z", "-m0=lzma", "-mx=7", "-mmt=1", "-bso0", "-bsp0", tmp, src],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                raise RuntimeError(f"7-Zip failed on {rel}: {result.stderr.decode(errors='replace')}")
            os.replace(tmp, dst)
        SOURCE_OVERRIDES[rel] = dst
        return os.path.getsize(src), os.path.getsize(dst)

    raw = packed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (r, p) in enumerate(pool.map(one, rels), 1):
            raw += r
            packed += p
            if i % 100 == 0 or i == len(rels):
                print(f"  compressed {i}/{len(rels)} DSFs: {raw/1e9:.2f} -> {packed/1e9:.2f} GB",
                      flush=True)
    return raw, packed


def find_package_root(path: str) -> str:
    """The folder holding Earth nav data (descends one level if needed)."""
    if os.path.isdir(os.path.join(path, DSF_DIR)):
        return path
    subs = [os.path.join(path, d) for d in os.listdir(path)
            if os.path.isdir(os.path.join(path, d, DSF_DIR))]
    if len(subs) == 1:
        return subs[0]
    sys.exit(f"No single scenery package with '{DSF_DIR}' found under {path}")


def texture_tile(name: str) -> Optional[Tile]:
    """1-degree tile containing the centre of a texture's 16x16 block."""
    m = TEX_RE.match(name)
    z = None
    for zm in ZOOM_RE.finditer(name[m.end():] if m else ""):
        z = int(zm["zoom"])
    if not m or z is None:
        return None
    row, col = int(m["row"]) + 8, int(m["col"]) + 8
    lon = col / 2 ** z * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / 2 ** z))))
    return math.floor(lat), math.floor(lon)


def scan(root: str):
    """Group every file in the package by tile."""
    dsfs: Dict[Tile, str] = {}
    for folder in sorted(os.listdir(os.path.join(root, DSF_DIR))):
        fdir = os.path.join(root, DSF_DIR, folder)
        if not os.path.isdir(fdir):
            continue
        for f in sorted(os.listdir(fdir)):
            m = DSF_RE.match(f)
            if m:
                dsfs[(int(m["lat"]), int(m["lon"]))] = f"{DSF_DIR}/{folder}/{f}"
    groups: Dict[Tile, Dict[str, List[str]]] = OrderedDict(
        (t, {"dsf": [rel], "ter": [], "tex": []}) for t, rel in sorted(dsfs.items()))
    shared: List[str] = []
    orphans = 0
    for sub, kind in (("terrain", "ter"), ("textures", "tex")):
        sdir = os.path.join(root, sub)
        if not os.path.isdir(sdir):
            continue
        for f in sorted(os.listdir(sdir)):
            rel = f"{sub}/{f}"
            tile = texture_tile(f)
            if tile in groups:
                groups[tile][kind].append(rel)
            else:
                if tile is not None:
                    orphans += 1
                shared.append(rel)
    for dirpath, _, files in os.walk(root):
        reldir = os.path.relpath(dirpath, root).replace("\\", "/")
        top = reldir.split("/")[0]
        if top in (DSF_DIR, "terrain", "textures") or top in SKIP_DIRS:
            continue
        for f in sorted(files):
            if f in SKIP_FILES or f.endswith(SKIP_SUFFIXES):
                continue
            shared.append(f if reldir == "." else f"{reldir}/{f}")
    return groups, shared, orphans


def file_size(root: str, rel: str) -> int:
    return os.path.getsize(source_path(root, rel))


def zipped_size(root: str, rel: str) -> int:
    """Raw size plus the zip's per-entry headers (local + central)."""
    return file_size(root, rel) + 2 * (len(rel) + 32) + 60


def plan_zips(root: str, groups, shared, max_bytes: int) -> List[List[str]]:
    """Whole tiles per zip, in tile order, each under max_bytes (raw size)."""
    zips: List[List[str]] = []
    current: List[List[str]] = [[], [], []]  # dsf, ter, tex
    size = sum(zipped_size(root, r) for r in shared)
    if size > GITHUB_LIMIT // 2:  # extras this large are leftovers, not scenery
        sys.exit(f"Files outside any tile total {size/1e9:.2f} GB; "
                 f"remove extras such as {shared[:3]}")
    pending_shared = list(shared)

    def flush():
        nonlocal current, size, pending_shared
        if any(current) or pending_shared:
            # DSFs, then the terrain block, then textures; shared files
            # (first zip only) last.
            zips.append(current[0] + current[1] + current[2] + pending_shared)
        current, size, pending_shared = [[], [], []], 0, []

    for tile, files in groups.items():
        tile_size = sum(zipped_size(root, r) for k in files for r in files[k])
        if tile_size > GITHUB_LIMIT:
            sys.exit(f"Tile {tile} alone is {tile_size/1e9:.2f} GB, over GitHub's 2 GiB limit")
        if any(current) and size + tile_size > max_bytes:
            flush()
        current[0] += files["dsf"]
        current[1] += files["ter"]
        current[2] += files["tex"]
        size += tile_size
    flush()
    return zips


def compress_type(root: str, rel: str) -> int:
    if rel.endswith((".png", ".dds", ".jpg", ".zip", ".7z")):
        return zipfile.ZIP_STORED
    if rel.endswith(".dsf"):
        with open(source_path(root, rel), "rb") as fh:
            if fh.read(6) == SEVENZIP_MAGIC:
                return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def write_zip(root: str, files: List[str], path: str, top: str) -> str:
    """Write one zip and its .sha256 sidecar; returns the hash."""
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for rel in files:
            zf.write(source_path(root, rel), f"{top}/{rel}",
                     compress_type=compress_type(root, rel), compresslevel=6)
    sha = hashlib.sha256()
    with open(tmp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    os.replace(tmp, path)
    digest = sha.hexdigest()
    with open(path + ".sha256", "w", newline="\n") as fh:
        fh.write(f"{digest}  {os.path.basename(path)}\n")
    return digest


def verify_zip(root: str, path: str, top: str, files: List[str]) -> None:
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad:
            sys.exit(f"Corrupt member {bad} in {path}")
        names = set(zf.namelist())
    missing = [r for r in files if f"{top}/{r}" not in names]
    if missing:
        sys.exit(f"{path} is missing {len(missing)} files, e.g. {missing[:3]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="Extracted package folder (or its parent)")
    ap.add_argument("--area", required=True, help="Region id, e.g. eur-w")
    ap.add_argument("--name", help="Human-readable set name for <area>_info.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-mb", type=int, default=1900, help="Zip size cap (MB, default 1900)")
    ap.add_argument("--dry-run", action="store_true", help="Only print the plan")
    ap.add_argument("--no-verify", dest="verify", action="store_false")
    ap.add_argument("--no-compress-dsf", dest="compress_dsf", action="store_false",
                    help="Keep DSFs as they are instead of 7z-compressing them")
    ap.add_argument("--sevenzip", default=DEFAULT_7Z, help="7-Zip executable")
    ap.add_argument("--workdir", help="Where compressed DSFs are kept (default: <out>_work)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args(argv)

    if not re.match(r"^[A-Za-z0-9-]+$", args.area):
        sys.exit("--area may only contain letters, digits and '-'")
    root = find_package_root(args.src)
    groups, shared, orphans = scan(root)
    SOURCE_OVERRIDES.clear()
    if args.compress_dsf and not args.dry_run:
        if not os.path.isfile(args.sevenzip):
            sys.exit(f"7-Zip not found at {args.sevenzip}; pass --sevenzip or --no-compress-dsf")
        workdir = args.workdir or os.path.normpath(args.out) + "_work"
        dsfs = [r for g in groups.values() for r in g["dsf"]]
        compress_dsfs(root, dsfs, workdir, args.sevenzip, args.workers)
    files_total = sum(len(v) for g in groups.values() for v in g.values()) + len(shared)
    raw = sum(file_size(root, r) for g in groups.values() for v in g.values() for r in v)
    print(f"{root}\n  {len(groups)} tiles, {files_total} files, {raw/1e9:.2f} GB; "
          f"{len(shared)} shared files ({orphans} textures outside any DSF tile)")
    zips = plan_zips(root, groups, shared, args.max_mb * 1_000_000)
    prefix = f"z_{args.area}_"
    for i, files in enumerate(zips):
        size = sum(file_size(root, r) for r in files)
        dsf = sum(1 for r in files if r.endswith(".dsf"))
        print(f"  {prefix}{i:02d}.zip: {dsf} DSFs, {len(files)} files, ~{size/1e6:.0f} MB raw")
    if args.dry_run:
        return

    os.makedirs(args.out, exist_ok=True)
    for i, files in enumerate(zips):
        top = f"{prefix}{i:02d}"
        path = os.path.join(args.out, f"{top}.zip")
        digest = write_zip(root, files, path, top)
        if args.verify:
            verify_zip(root, path, top, files)
        print(f"  wrote {path} ({os.path.getsize(path)/1e6:.0f} MB) sha256 {digest[:12]}...")
    info = OrderedDict([("id", args.area), ("name", args.name or f"{args.area} XP12 Ortho Set"),
                        ("ortho_prefix", prefix), ("overlay_prefix", "")])
    with open(os.path.join(args.out, f"{args.area}_info.json"), "w", newline="\n") as fh:
        json.dump(info, fh, indent=4)
        fh.write("\n")
    print(f"\nDone. Upload with (one release per area):\n"
          f"  gh release create <tag> --repo <owner>/<repo> --title \"<tag>\" --notes-file <notes>\n"
          f"  gh release upload <tag> --repo <owner>/<repo> {args.out}/*")


if __name__ == "__main__":
    main()

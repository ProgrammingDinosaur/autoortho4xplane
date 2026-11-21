#!/usr/bin/env python3
"""Build a tile index for streaming scenery straight from release zips.

Reads the table of contents of every scenery zip in a GitHub repository's
releases with HTTP Range requests (a few MB per zip, nothing else is
downloaded) and writes a gzipped JSON index mapping each DSF and water mask
to its byte range. AutoOrtho's streaming packages use the index to fetch one
tile at a time (see autoortho/tile_stream.py).

With --verify-ter (the default) it also downloads each zip's terrain/ block,
checks every .ter against the template AutoOrtho generates, and stores the
ones that differ in the index so the generated files match exactly.

Examples:
    python scripts/build_tile_index.py --out tile_index_xp11.json.gz
    python scripts/build_tile_index.py --regions eur --max-zips 1 --out test.json.gz
    python scripts/build_tile_index.py --kind overlay --out overlay_index.json.gz
"""

from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import os
import re
import sys
import time
import zipfile
from typing import Dict, List, Optional

import requests

_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _REPO)
try:
    from autoortho import tile_stream as ts  # noqa: E402
except ImportError:  # autoortho/ itself is on sys.path (tests)
    sys.path.insert(0, os.path.join(_REPO, "autoortho"))
    import tile_stream as ts  # noqa: E402

DEFAULT_REPO = "kubilus1/autoortho-scenery"
MASK_TEMPLATES = ("{row}_{col}_ZL{zoom}.png", "{row}_{col}_{maptype}{zoom}_ZL{zoom}.png")
TER_CHUNK = 16 * 1024 * 1024


class RemoteFile(io.RawIOBase):
    """Seekable read-only view of a remote asset for zipfile."""

    def __init__(self, remote: ts.RemoteAssets, asset: dict, readahead: int = 1 << 20):
        self.remote = remote
        self.asset = asset
        self.size = asset["size"]
        self.pos = 0
        self.readahead = readahead
        self._buf_start = -1
        self._buf = b""

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        end = min(self.pos + n, self.size)
        if end <= self.pos:
            return b""
        b0 = self._buf_start
        if not (b0 <= self.pos and end <= b0 + len(self._buf)):
            want_end = min(max(end, self.pos + self.readahead), self.size)
            self._buf = ts.asset_read(self.remote, self.asset, self.pos, want_end - 1)
            self._buf_start = self.pos
        out = self._buf[self.pos - self._buf_start:end - self._buf_start]
        self.pos += len(out)
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


class LocalAssets:
    """read_range over files on disk, keyed by the URL they will have."""

    def __init__(self, url_to_path: Dict[str, str]):
        self.paths = url_to_path
        self.stats = {"resolves": 0, "ranges": 0, "bytes": 0, "throttled": 0}

    def in_cooldown(self) -> bool:
        return False

    def read_range(self, url: str, start: int, end: int) -> bytes:
        with open(self.paths[url], "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        self.stats["ranges"] += 1
        self.stats["bytes"] += len(data)
        return data


def local_release(folder: str, url_base: str):
    """A release-like dict for zips written by repack_scenery.py."""
    infos = [f for f in os.listdir(folder) if f.endswith("_info.json")]
    if len(infos) != 1:
        sys.exit(f"Expected one <area>_info.json in {folder}, found {infos}")
    region = infos[0][:-len("_info.json")]
    base = url_base.format(area=region)
    if not base.endswith("/"):
        base += "/"
    assets, paths = [], {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            url = base + requests.utils.quote(name)
            assets.append({"name": name, "size": os.path.getsize(path),
                           "browser_download_url": url})
            paths[url] = path
    tag = base.rstrip("/").rsplit("/", 1)[-1]
    rel = {"tag_name": requests.utils.unquote(tag), "published_at": time.strftime("%Y-%m-%d"),
           "assets": assets}
    return region, rel, paths


def list_releases(session: requests.Session, repo: str) -> List[dict]:
    out, page = [], 1
    while True:
        r = session.get(f"https://api.github.com/repos/{repo}/releases",
                        params={"per_page": 100, "page": page},
                        headers={"Accept": "application/vnd.github+json"}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def latest_per_region(releases: List[dict], include_pre: bool) -> Dict[str, dict]:
    """Newest release per region, picked the way OrthoManager picks them."""
    chosen = {}
    for rel in sorted(releases, key=lambda r: r["published_at"], reverse=True):
        if rel.get("prerelease") and not include_pre:
            continue
        regions = [a["name"][:-len("_info.json")] for a in rel["assets"]
                   if a["name"].endswith("_info.json")]
        if regions and regions[0] not in chosen:
            chosen[regions[0]] = rel
    return chosen


def scenery_assets(release: dict, kind: str) -> List[dict]:
    """Zip assets of one release, joining split parts into one asset."""
    prefix = "z_" if kind == "ortho" else "y_"
    groups = collections.OrderedDict()
    for a in release["assets"]:
        m = re.match(r"^(?P<zip>[yz]_.+?\.zip)(?:\.(?P<part>\d+))?$", a["name"])
        if not m or not a["name"].startswith(prefix):
            continue
        groups.setdefault(m["zip"], []).append((int(m["part"]) if m["part"] else -1, a))
    assets = []
    for name, parts in groups.items():
        parts.sort(key=lambda p: p[0])  # numeric, so .10 follows .09
        if len(parts) == 1 and parts[0][0] == -1:
            a = parts[0][1]
            assets.append({"name": name, "url": a["browser_download_url"], "size": a["size"]})
        else:
            assets.append({
                "name": name,
                "size": sum(p[1]["size"] for p in parts),
                "parts": [{"url": p[1]["browser_download_url"], "size": p[1]["size"]}
                          for p in parts],
            })
    return assets


def member_rel(name: str, kind: str) -> Optional[str]:
    """Path inside the package for a zip member, or None to skip it."""
    head, _, rest = name.partition("/")
    if not rest:
        return None
    if kind == "overlay":
        sub = "yOrtho4XP_Overlays/"
        if not rest.startswith(sub):
            return None
        rest = rest[len(sub):]
    if rest.startswith("*/") or rest.endswith("/"):
        return None
    if rest.startswith(ts.DSF_DIR + "/") and rest.endswith(".dsf"):
        return rest
    if kind == "ortho" and rest.startswith("textures/") and rest.endswith(".png"):
        return rest
    return None


class TerChecker:
    """Learns the .ter template settings and collects files that differ."""

    def __init__(self):
        self.params = dict(ts.DEFAULT_TER_PARAMS)
        self.params["mask_name_by_zoom"] = {}
        self.exceptions: Dict[str, str] = {}
        self.checked = 0
        self._learned_decal = False
        self._learned_zl = False

    def _learn(self, name: str, text: str):
        m = ts.TER_NAME_RE.match(name)
        if not m:
            return
        kind = m["kind"] or ""
        decal = re.search(r"(?m)^DECAL_LIB (\S+)$", text)
        if kind == "" and not self._learned_decal:
            self.params["decal"] = decal.group(1) if decal else None
            self.params["land_shadow"] = "\nNO_SHADOW\n" not in text
            self._learned_decal = True
        if kind.startswith("_sea") and decal and not self.params.get("decal_on_sea"):
            self.params["decal_on_sea"] = True
            self.params["decal"] = self.params.get("decal") or decal.group(1)
        if kind == "_sea_overlay":
            zoom = int(m["zoom"])
            bm = re.search(r"(?m)^LOAD_CENTER_BORDER \S+ \S+ \S+ (\d+)$", text)
            if bm and not self._learned_zl:
                px = int(bm.group(1))
                self.params["mask_zl"] = zoom - (4096 // px).bit_length() + 1
                self._learned_zl = True
            by_zoom = self.params["mask_name_by_zoom"]
            if str(zoom) not in by_zoom:
                tm = re.search(r"(?m)^BORDER_TEX \.\./textures/(\S+)$", text)
                if tm:
                    for tpl in MASK_TEMPLATES:
                        if tpl.format(row=int(m["row"]), col=int(m["col"]),
                                      maptype=m["maptype"], zoom=zoom) == tm.group(1):
                            by_zoom[str(zoom)] = tpl
                            break

    def check(self, name: str, text: str):
        self._learn(name, text)
        self.checked += 1
        made = ts.synthesize_ter(name, self.params)
        if made is None or made.decode("ascii") != text:
            self.exceptions[name] = text


def verify_ter_block(remote, asset, infos, checker: TerChecker, kind: str):
    ters = sorted((i for i in infos if i.filename.endswith(".ter")
                   and "/terrain/" in i.filename), key=lambda i: i.header_offset)
    if not ters:
        return
    i = 0
    while i < len(ters):
        start = ters[i].header_offset
        j = i
        while j + 1 < len(ters) and ters[j + 1].header_offset - start < TER_CHUNK:
            j += 1
        last = ters[j]
        end = min(last.header_offset + 30 + 1024 + last.compress_size, asset["size"]) - 1
        raw = ts.asset_read(remote, asset, start, end)
        for info in ters[i:j + 1]:
            entry = [0, info.header_offset, info.compress_size, info.file_size,
                     info.compress_type, info.CRC]
            data, _ = ts.extract_member(raw[info.header_offset - start:], entry)
            if data is None:
                raise IOError(f"Short terrain block read at {info.filename}")
            checker.check(info.filename.rsplit("/", 1)[-1], data.decode("ascii", "replace"))
        i = j + 1


def convert(src: str, out: str) -> None:
    """Rewrite index ``src`` in the current format, keeping everything else."""
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    index = ts.TileIndex(data)  # validates
    if data["format"] == 1:
        data["files"] = ts.pack_files(data["files"])
    data["format"] = ts.INDEX_FORMAT
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"{src} -> {out}: {len(index)} files, {index.dsf_count} DSFs "
          f"({os.path.getsize(out)/1e6:.2f} MB)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--kind", choices=("ortho", "overlay"), default="ortho")
    ap.add_argument("--regions", nargs="*", help="Only these region ids (e.g. eur na)")
    ap.add_argument("--tags", nargs="*", help="Use these release tags instead of the latest per region")
    ap.add_argument("--prerelease", action="store_true", help="Allow prereleases")
    ap.add_argument("--max-zips", type=int, default=0, help="Stop after N zips (testing)")
    ap.add_argument("--no-verify-ter", dest="verify_ter", action="store_false")
    ap.add_argument("--name", default="AutoOrtho XP11 legacy set")
    ap.add_argument("--variant", default="xp11")
    ap.add_argument("--out", required=True)
    ap.add_argument("--convert", metavar="INDEX",
                    help="Rewrite an existing index in the current format (no downloads)")
    ap.add_argument("--local-dir", action="append",
                    help="Index release files on disk instead (repeat per area); "
                         "needs --url-base")
    ap.add_argument("--url-base", help="Download URL prefix the files will have once "
                    "uploaded, e.g. https://github.com/o/r/releases/download/<tag>/ "
                    "(use {area} for one release per area)")
    args = ap.parse_args(argv)
    if args.convert:
        convert(args.convert, args.out)
        return

    session = requests.Session()
    session.headers["User-Agent"] = "autoortho-tile-index-builder"
    remote = ts.RemoteAssets(session=session, timeout=60)

    if args.local_dir:
        if not args.url_base:
            sys.exit("--local-dir needs --url-base")
        picked, local_map = {}, {}
        for d in args.local_dir:
            region, rel, paths = local_release(d, args.url_base)
            picked[region] = rel
            local_map.update(paths)
        remote = LocalAssets(local_map)
        releases = []
    else:
        releases = list_releases(session, args.repo)
    if args.local_dir:
        pass
    elif args.tags:
        picked = {}
        for rel in releases:
            if rel["tag_name"] in args.tags:
                regions = [a["name"][:-len("_info.json")] for a in rel["assets"]
                           if a["name"].endswith("_info.json")] or [rel["tag_name"]]
                picked[regions[0]] = rel
    else:
        picked = latest_per_region(releases, args.prerelease)
    if args.regions:
        picked = {k: v for k, v in picked.items() if k in args.regions}
    if not picked:
        sys.exit("No matching releases")
    for region, rel in picked.items():
        print(f"{region}: release {rel['tag_name']} ({rel['published_at'][:10]})")

    assets: List[dict] = []
    files: Dict[str, list] = {}
    checker = TerChecker()
    started = time.time()
    zips = 0
    for region, rel in sorted(picked.items()):
        for asset in scenery_assets(rel, args.kind):
            if args.max_zips and zips >= args.max_zips:
                break
            zips += 1
            t0 = time.time()
            asset_idx = len(assets)
            assets.append(dict(asset, region=region, release=rel["tag_name"]))
            zf = zipfile.ZipFile(RemoteFile(remote, asset))
            infos = zf.infolist()
            counts = collections.Counter()
            for info in infos:
                rel_path = member_rel(info.filename, args.kind)
                if rel_path is None:
                    continue
                if rel_path in files:
                    counts["duplicate"] += 1
                files[rel_path] = [asset_idx, info.header_offset, info.compress_size,
                                   info.file_size, info.compress_type, info.CRC]
                counts[rel_path.rsplit(".", 1)[-1]] += 1
            if args.verify_ter and args.kind == "ortho":
                verify_ter_block(remote, asset, infos, checker, args.kind)
            print(f"  {asset['name']:28s} {asset['size']/1e6:8.0f} MB  "
                  f"{dict(counts)}  {time.time()-t0:.1f}s")

    if not checker.params["mask_name_by_zoom"]:
        checker.params.pop("mask_name_by_zoom")
    index = {
        "format": ts.INDEX_FORMAT,
        "name": args.name,
        "variant": args.variant,
        "kind": args.kind,
        "source": args.repo,
        "built": int(time.time()),
        "assets": assets,
        "files": ts.pack_files(files),
    }
    if args.kind == "ortho":
        index["ter"] = {"params": checker.params, "exceptions": checker.exceptions,
                        "verified": checker.checked if args.verify_ter else 0}
    with gzip.open(args.out, "wt", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))

    dsfs = sum(1 for f in files if f.endswith(".dsf"))
    pngs = sum(1 for f in files if f.endswith(".png"))
    print(f"\n{len(assets)} zips, {dsfs} DSFs, {pngs} masks -> {args.out} "
          f"({os.path.getsize(args.out)/1e6:.2f} MB) in {time.time()-started:.0f}s")
    print(f"HTTP: {remote.stats}")
    if args.kind == "ortho" and args.verify_ter:
        pct = 100.0 * len(checker.exceptions) / max(1, checker.checked)
        print(f".ter: {checker.checked} checked, {len(checker.exceptions)} stored as "
              f"exceptions ({pct:.3f}%), params {checker.params}")


if __name__ == "__main__":
    main()

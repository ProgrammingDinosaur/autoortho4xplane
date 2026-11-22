"""Decision gate for the packed chunk store (performance plan, item 3).

Writes and reads real chunks with the one-file-per-chunk layout and with
``autoortho.chunk_store.ChunkStore``, and reports CPU per chunk written and
the time and CPU to read 256 and 1,024 chunk textures, cold (a fresh store
or a first open of each file) and warm.

    python tools/bench/chunk_store_gate.py --source <cache_dir> --count 100000

The source folder is only read. Output goes to ``--work`` (a temporary
folder by default), which is deleted at the end unless ``--keep``.

The gate passes when writes cost at least five times less CPU and reads are
no slower. Windows Defender's CPU (MsMpEng) is measured alongside: it scans
each new file of the old layout.

"Cold" cannot drop the operating system's file cache without admin rights:
it means a fresh store (new connections, empty SQLite page cache) or files
not opened since they were written.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from autoortho import chunk_store  # noqa: E402

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def _defender_cpu() -> float:
    if psutil is None:
        return 0.0
    total = 0.0
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() == "msmpeng.exe":
            try:
                times = proc.cpu_times()
                total += times.user + times.system
            except (psutil.Error, OSError):
                pass
    return total


class Meter:
    def __init__(self, label):
        self.label = label

    def __enter__(self):
        self.wall = time.perf_counter()
        self.cpu = time.process_time()
        self.defender = _defender_cpu()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.wall
        self.cpu = time.process_time() - self.cpu
        self.defender = _defender_cpu() - self.defender
        return False

    def as_dict(self):
        return {
            "wall_s": round(self.wall, 4),
            "cpu_s": round(self.cpu, 4),
            "defender_cpu_s": round(self.defender, 4),
        }


def load_chunks(source: str, count: int):
    chunks = []
    with os.scandir(source) as entries:
        for entry in entries:
            parsed = chunk_store.parse_chunk_name(entry.name)
            if parsed is None:
                continue
            try:
                with open(entry.path, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if not chunk_store.is_jpeg(data):
                continue
            chunks.append((*parsed, data))
            if len(chunks) >= count:
                break
    return chunks


def atomic_write(target_path: str, data: bytes) -> None:
    """The old layout's write, as getortho._atomic_write does it."""
    if os.path.exists(target_path):
        return
    write_dir = os.path.dirname(target_path)
    temp = os.path.join(
        write_dir, f".{os.path.basename(target_path)}.{uuid.uuid4().hex}.tmp"
    )
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, target_path)


def textures(chunks, side):
    """Aligned ``side`` x ``side`` squares fully present in ``chunks``."""
    groups = defaultdict(set)
    for col, row, zoom, maptype, _data in chunks:
        groups[(zoom, maptype, col // side, row // side)].add((col, row))
    return [
        (zoom, maptype, [(c0 * side + dx, r0 * side + dy) for dy in range(side) for dx in range(side)])
        for (zoom, maptype, c0, r0), present in groups.items()
        if len(present) == side * side
    ]


def read_files(folder, zoom, maptype, keys):
    found = 0
    for col, row in keys:
        try:
            with open(os.path.join(folder, chunk_store.chunk_file_name(col, row, zoom, maptype)), "rb") as handle:
                if handle.read():
                    found += 1
        except OSError:
            pass
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="a chunk cache folder (read only)")
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--work", default=None)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--json", default=None, help="write the results here")
    args = parser.parse_args(argv)

    work = args.work or tempfile.mkdtemp(prefix="ao-chunk-gate-")
    files_dir = os.path.join(work, "files")
    store_dir = os.path.join(work, "store")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(store_dir, exist_ok=True)
    results = {"work": work}
    try:
        print(f"Loading up to {args.count} chunks from {args.source} ...")
        chunks = load_chunks(args.source, args.count)
        n = len(chunks)
        total_bytes = sum(len(c[4]) for c in chunks)
        results["chunks"] = n
        results["bytes"] = total_bytes
        print(f"  {n} chunks, {total_bytes / 1e6:.1f} MB")

        with Meter("files write") as m_files:
            for col, row, zoom, maptype, data in chunks:
                atomic_write(os.path.join(files_dir, chunk_store.chunk_file_name(col, row, zoom, maptype)), data)
        results["write_files"] = m_files.as_dict()

        store = chunk_store.ChunkStore(store_dir, max_pending_bytes=4 * 1024 ** 3)
        with Meter("store write") as m_store:
            for col, row, zoom, maptype, data in chunks:
                while not store.put(col, row, zoom, maptype, data):
                    time.sleep(0.001)
            store.flush(timeout=600)
        results["write_store"] = m_store.as_dict()
        store.close()

        results["write_cpu_us_per_chunk_files"] = round(m_files.cpu / n * 1e6, 2)
        results["write_cpu_us_per_chunk_store"] = round(m_store.cpu / n * 1e6, 2)
        ratio = m_files.cpu / m_store.cpu if m_store.cpu else float("inf")
        results["write_cpu_ratio"] = round(ratio, 2)
        with_def = (m_files.cpu + m_files.defender) / max(1e-9, m_store.cpu + m_store.defender)
        results["write_cpu_ratio_with_defender"] = round(with_def, 2)

        disk_files = sum(os.path.getsize(os.path.join(files_dir, f)) for f in os.listdir(files_dir))
        results["disk_bytes_files_logical"] = disk_files
        results["disk_bytes_store"] = chunk_store.ChunkStore(store_dir).size_bytes()

        for side in (16, 32):
            squares = textures(chunks, side)[:20]
            label = f"read_{side * side}"
            if not squares:
                results[label] = "no complete textures of this size in the sample"
                continue
            # cold: files never opened since written; a fresh store.
            with Meter("files cold") as m_fc:
                for zoom, maptype, keys in squares:
                    read_files(files_dir, zoom, maptype, keys)
            fresh = chunk_store.ChunkStore(store_dir)
            with Meter("store cold") as m_sc:
                for zoom, maptype, keys in squares:
                    fresh.get_many(maptype, zoom, keys)
            with Meter("files warm") as m_fw:
                for zoom, maptype, keys in squares:
                    read_files(files_dir, zoom, maptype, keys)
            with Meter("store warm") as m_sw:
                for zoom, maptype, keys in squares:
                    fresh.get_many(maptype, zoom, keys)
            fresh.close()
            per = len(squares)
            results[label] = {
                "textures": per,
                "files_cold_ms": round(m_fc.wall / per * 1000, 3),
                "store_cold_ms": round(m_sc.wall / per * 1000, 3),
                "files_warm_ms": round(m_fw.wall / per * 1000, 3),
                "store_warm_ms": round(m_sw.wall / per * 1000, 3),
                "files_cold_cpu_ms": round(m_fc.cpu / per * 1000, 3),
                "store_cold_cpu_ms": round(m_sc.cpu / per * 1000, 3),
                "files_warm_cpu_ms": round(m_fw.cpu / per * 1000, 3),
                "store_warm_cpu_ms": round(m_sw.cpu / per * 1000, 3),
            }

        reads_ok = all(
            isinstance(results.get(f"read_{s}"), dict)
            and results[f"read_{s}"]["store_cold_ms"] <= results[f"read_{s}"]["files_cold_ms"] * 1.05
            and results[f"read_{s}"]["store_warm_ms"] <= results[f"read_{s}"]["files_warm_ms"] * 1.05
            for s in (256, 1024)
            if isinstance(results.get(f"read_{s}"), dict)
        )
        results["gate_writes_5x"] = ratio >= 5.0
        results["gate_reads_not_slower"] = reads_ok
        results["gate_pass"] = bool(ratio >= 5.0 and reads_ok)
        print(json.dumps(results, indent=2))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump(results, handle, indent=2)
        return 0 if results["gate_pass"] else 1
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

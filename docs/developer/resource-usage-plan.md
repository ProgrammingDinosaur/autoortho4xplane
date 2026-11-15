# Resource usage: design

Date: 2026-11-15. Status: implemented on `fix/3.1.0-bugs`.

## Why

AutoOrtho's RAM and CPU go mostly to its own design, not to Python. OrthoStudio XP
Live (also Python) used 0.32 cores and 2.9 GB in flight against 0.57 cores and 3.8 GB
for AutoOrtho in OrthoStudio's own comparison. OrthoStreamXP (C#) is still a virtual
file system but serves finished textures as plain files. Three things cost AutoOrtho
the most:

1. **Every texture X-Plane opens lives in AutoOrtho's RAM.** A disk-cache hit reads
   the whole DDS (11 MB BC1, 22 MB BC3 at 4096 px), decompresses it (zstd) and copies
   it into a `Tile`. Tiles stay until RSS-driven eviction removes them.
2. **One Python process per scenery mount**, each with its own interpreter,
   thread pools, prefetcher, tile cache and memory limit, coordinated through a
   shared stat that can go stale.
3. **Live admission counts builds, not bytes.** Sixteen mipmap-0 builds and
   sixteen 256 px builds cost the same "slot", so the gate is either too loose
   (a burst of mm0 builds) or too tight (many far reads).

Measured on cached imagery (Apple, ZL16/17, ISPC BC1, 4096 px): zstd level 3 saves
**9-13 %** of a DDS (not the 30-60 % the old config comment claimed), costs ~40 ms of
CPU per store and ~9 ms per load, and forces the whole file through RAM.

## 1. Finished textures served from disk ("direct reads")

`[pydds] serve_from_disk = True` (default).

- **Open.** Before creating a `Tile`, `autoortho_fuse.open()` asks
  `getortho.open_disk_texture()` for the texture. It answers with a
  `DiskTexture` only when the DDS cache holds an entry that is: at the tile's
  current target zoom, not stale (format, compressor), uncompressed on disk,
  complete and exact in mipmap 0 (every row real target imagery, nothing missing
  or fallback), every level populated, and exactly the expected size. Otherwise
  the live `Tile` path runs as before. A texture X-Plane already has open as a
  live `Tile` stays on that path so one texture is never served two ways.
- **Read.** Positional reads from one shared file handle per cache file.
  Mipmap-0 rows are checked against their SHA-256 row manifest the first time
  each row is read (once per file per process); later levels have no manifest
  and are trusted (atomic writes). The season colour table is applied exactly as
  the live path does. Each read is still classified (`read_pattern.classify`)
  so the paging model and the held-texture statistics keep their samples.
- **Fallback.** A short read, I/O error or checksum mismatch invalidates the
  cache entry and switches that handle to a live `Tile` for the rest of its
  reads. X-Plane never sees an error.
- **Pins.** Entries open for direct reads are skipped by LRU eviction (on
  Windows the delete would fail and orphan the file; on POSIX it would waste
  the work).
- **Storage.** With direct reads on, new entries are written uncompressed. A
  compressed entry that is otherwise servable is rewritten uncompressed in the
  background on its first hit, so the next open is direct.
- **Dropping RAM copies.** A live `Tile` that X-Plane has closed and whose DDS
  is on disk is dropped by the cleaner on its next pass instead of waiting for
  memory pressure: every later open is a direct read.

The OS file cache now holds hot textures: shared, reclaimed under pressure, not
charged to AutoOrtho.

## 2. One process for every mount

`[fuse] shared_worker = True` (Windows and Linux only). macOS always keeps one
worker per mount: that was the reason for per-mount workers in the first
place, since macFUSE cannot run several mounts from one process through the
Python FUSE binding. The setting is ignored there, and the worker itself
refuses a multi-mount launch on macOS.

The parent starts one mount worker with every scenery (`--mount root|mountpoint|volname`
repeated). The worker runs each FUSE loop on its own thread and shares one
`TileCacher` (one memory limit, one prefetcher, one DDS builder, one download
pool). The worker CPU share (`AO_MOUNT_WORKER_COUNT`, OpenMP budget) becomes 1,
so native code gets the whole CPU budget instead of 1/N each. The parent keeps
one handle per mount so status, unmount and restart code is unchanged; all
handles of a shared worker share its process.

Crash isolation is not lost in practice: X-Plane crashes when any mount it is
reading disappears, so a dead worker per mount never kept a flight alive.

## 3. Memory-weighted live admission

`_live_tile_admission` becomes a `WeightedGate`: the governor's count limit
stays, and a byte budget is added (`[autoortho] live_build_memory_mb`, default
derived from `cache_mem_limit`: 1/4, between 256 MB and 2 GB). Each read is
weighted by the working set of the level it builds (RGBA + BC data, about
size² × 4.5 bytes: 75 MB at 4096 px, 1.2 MB at 512 px). A request heavier than the
whole budget is admitted when nothing else holds the gate, so nothing starves.

Buffer pooling already exists (`DDSBufferPool`, `StreamingBuilderPool`), so no
new pool is added.

Direct reads need the DDS cache, so they are off when predictive DDS (or
spatial prefetch, which feeds it) is disabled.

## 4. Startup downloads

Audited, not changed. OrthoStudio's comparison ran upstream AutoOrtho. This
branch already has the OrthoStreamXP techniques that cut start-up downloads,
all on by default (`streaming_options.DEFAULTS`): the read classifier
(header chunks served from one base chunk), the low-zoom base, paging
prefetch (DSF prefetch fetches only the level X-Plane will want), terrain
load-ahead and wait bands. Any further trimming needs a measured flight
(the `dsf_paging_coarse_chunks`, `read_header_chunks` and download byte
counters) rather than a guess.

## Not done

Building textures ahead to plain files with no virtual file system (OrthoStudio
Live's model): deliberately out of scope (delicate, and heavy on disk writes).

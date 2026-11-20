# Scenery streaming: how it works and how to maintain it

User-facing behaviour is described in
[Streaming scenery instead of installing regions](../scenery.md#streaming-scenery-instead-of-installing-regions).
This page covers the design and the maintainer tasks: building tile indexes,
repackaging scenery for streaming, and adding a scenery set.

## Design

Scenery releases are zip files on GitHub Releases. Every member of a zip is
compressed on its own and the zip's central directory records where each
one starts, so a single DSF or water mask can be read with an HTTP Range
request without downloading the rest. GitHub release assets support Range
requests and have no bandwidth limit, so streaming needs no paid hosting.

| Piece | Where |
|---|---|
| Index format, remote reads, streaming package, `.ter` generation | `autoortho/tile_stream.py` |
| FUSE hooks (virtual DSF listing, fetch on open, fallbacks) | `autoortho/autoortho_fuse.py` (`_getattr_stream`, `_open_stream_dsf`) |
| Streamed tiles visible to the imagery pipeline | `TerrainTileLookup.add_virtual_tiles` in `autoortho/getortho.py` |
| `scenery_packs.ini` ordering at mount time | `tile_stream.ensure_all_pack_orders`, called from `AOMount.mount_sceneries` |
| Scenery Library panel | `autoortho/ui/widgets/stream_panel.py`, handlers in `autoortho/ui/main_window.py` (`_on_stream_*`) |
| Cache view (scan, delete, pin, keep offline) | `autoortho/cache_inventory.py` |
| Cache view UI | `autoortho/ui/widgets/cache_map_panel.py` on the shared `MapCanvas` in `autoortho/ui/widgets/custom_map_editor.py` |
| Index builder | `scripts/build_tile_index.py` |
| Repackager for solid archives | `scripts/repack_scenery.py` |

### Streaming packages

A streaming package is a folder in `z_autoortho/scenery/` holding
`ao_stream.json` (the marker) and `tile_index.json.gz`. It is mounted like
an installed region:

- `z_ao_stream_<set>`: base mesh (`kind: ortho`).
- `yAO_stream_<set>_overlays`: overlays (`kind: overlay`), optional.

The marker records the set, the index URL and its `Last-Modified`, the
disk limit (overlays get a quarter of the ortho limit) and, on the ortho
package, the terrain reflectivity (`roughness`), which `synthesize_ter`
appends to every generated `.ter` as `SUPER_ROUGHNESS`
(`set_stream_roughness`).

Installed packages built with X-Plane 12's seasonal rasters (`spr*`,
`sum*`, `fal*`, `win*` in the DSF's DEMN table, as in the XP12 set) show
Seasons as **Included** (`SeasonsApplyStatus.INCLUDED`), and the Seasons patch
isn't offered. `package_has_native_seasons` checks the package's first DSF
and caches the answer in `.ao_native_seasons.json` next to it, because
unpacking a 7z DSF takes seconds. Code only ever
deletes folders that contain a marker.

### What happens on open

1. `readdir` lists every DSF in the index; installed regions' DSFs are
   hidden, so installed tiles always win.
2. When X-Plane opens a DSF that isn't stored yet, it is fetched (zip local
   header + member) and stored; 7z-compressed DSFs are unpacked. The DSF's
   terrain list registers its imagery tiles with `TerrainTileLookup` and
   queues its water masks. The 8 neighbouring tiles are prefetched, and
   their masks are queued as soon as each one arrives.

   Downloads run in three queues so what X-Plane needs soonest goes first:
   opens in the foreground pool; water masks in their own pool
   (`PriorityWorkers`, never capped), masks of tiles X-Plane opened ahead of
   masks of prefetched tiles; neighbour DSFs in the background pool, at most
   `MAX_BACKGROUND` (32) queued. X-Plane reads a tile's masks seconds after
   its DSF, so masks must never wait behind, or be dropped for, 15 MB DSFs.
3. `.ter` files are generated from their names with the Ortho4XP template
   (`synthesize_ter`). Index builds check every published `.ter` against
   it; the few that differ are stored in the index as exceptions.
4. If a fetch fails, an ortho DSF is served from X-Plane's Global Scenery,
   an overlay DSF as a valid empty overlay, and a water mask as a blank
   (all-sea) PNG of the mask's size. X-Plane must never see a missing file:
   a `.ter` whose mask is missing stops it with a fatal error.
5. An open (X-Plane waiting) always gets its own download and retries it
   until `open_timeout` (30 s) runs out. It never waits behind a prefetch of
   the same file, which may be stuck on a dropped connection; whichever
   download finishes first stores the file. An open that failed is
   remembered for 5 minutes so an offline sim falls back immediately.

### GitHub access

`releases/download/...` redirects to a signed URL valid for about an hour.
`RemoteAssets` reuses the signed URL until shortly before it expires, so
normal flying makes about one request to github.com per zip per hour; the
rest go to the asset CDN. On 403, 429 or 5xx, fetches pause (honouring
`Retry-After`, 60 s doubling up to 15 min) and opens fall back meanwhile.

The CDN and home routers drop idle connections without telling either end;
reusing one stalls until Windows reports the reset, about 20 s later. So
pooled connections idle for more than 30 s are closed before the next
request (`RemoteAssets.IDLE_RESET`; a new TLS connection costs about
0.1 s), requests use a 5 s connect and 10 s read timeout (the read timeout
is the longest silence between bytes, not the whole download), and a
dropped or stalled connection is retried up to 3 times.

### Disk limit

After fetches, and at most once a minute, the least recently used tiles are
removed until the package is under 90% of its limit. Files used in the last
two hours are kept. Use is tracked by file mtime (touched on open).

Pinned tiles (kept for offline flying) are listed in `pinned_tiles.json` in
each package (`{"tiles": ["+47-123", ...]}`; `read_pins`/`write_pins`).
`evict` skips their DSFs and masks and leaves them out of the total, so
pinned tiles don't count toward the limit. A mask belongs to the tile that
holds its centre (`mask_cell`). The pins live in their own file rather than
the marker because `_create_package` rewrites the marker.

## Cache view

The map on **Flight Plan & Map** has a Cache view (`CachePanel`) that shows,
deletes and pins what is stored per 1x1 degree tile. The file work is in
`cache_inventory.py`, which has no Qt dependency:

- `scan(ao_scenery_dir, cache_dir)` builds a `CacheInventory` of
  `CellUsage` per tile with `os.scandir` only. On Windows the directory
  entries carry size and mtime, so about 200k chunk files take about 1.2 s.
  The UI runs it on a thread and drops results from older scans.
  - Streamed mesh: DSFs (`dsf_cell`) and masks (`mask_cell`) of every
    package with a marker, plus the pins.
  - Chunk JPEGs in `cache_dir`: the name is `<col>_<row>_<zoom>_<maptype>.jpg`
    (column first, see `Chunk.chunk_id` in `getortho.py`). A chunk belongs
    to the tile holding its centre. Row and column halves are memoised.
  - Built textures: `dds_cache/<10 degree band>/<1 degree tile>/<maptype>/`;
    the tile folder name is the tile, and the zoom comes from `_z<zl>.dds`.
  - The long-term cache (`long_term_cache_dir`) isn't scanned.
- `delete_cells(..., mesh, imagery)` removes only files AutoOrtho names
  itself: DSFs and masks inside marked packages, chunk files matching the
  chunk naming, and the tiles' `dds_cache` folders (empty band folders go
  too). Deleting a tile's mesh also unpins it. A mask on a degree line may
  also be used by the neighbouring DSF; that DSF fetches it again on its
  next open (`note_local_dsf`).
- `keep_offline(...)` creates a `StreamPackage` per package (with the same
  `hidden` set the mount uses, so installed tiles aren't fetched), pins the
  tiles its index has, and calls `StreamPackage.download_tile` for each:
  `fetch_now` (synchronous fetch) of the DSF, then of the masks its terrain
  list names (`tile_masks` / `mask_rels`). A throttled host stops the run;
  what was fetched stays pinned. Progress goes to the Activity panel as a
  `TaskType.CACHE` task, cancellable between files.
- `mesh_download_estimate` sums the index sizes of DSFs not on disk yet, and
  `estimate_imagery` counts web-mercator tiles per tile and zoom
  (`chunks_in_cell`) times the average chunk size already in the cache
  (16 KB if there's none). Both feed the Keep Offline confirmation.

Deleting and keeping are locked while AutoOrtho or X-Plane runs:
`MainWindow._set_runtime_locked` passes the same flag to the stream panel
and to `CustomMapEditor.set_runtime_locked`. After a change the panel
rescans and emits `cacheChanged`, which refreshes the stream panel's usage.

### Next step: pre-caching imagery

Keep Offline downloads only the mesh. Pre-caching imagery is the larger
half (a 1x1 degree tile at 40°N is about 0.6 GB at ZL16, 2.6 GB at ZL17 and
10 GB at ZL18 with Bing), and needs:

1. The exact chunk set rather than a box: after the DSF is stored, its
   terrain list (`terrain_tiles`) names every texture as (row, col,
   maptype, zoom); each texture is 16x16 chunks at that zoom. Water-only
   textures can be skipped. This also gives a precise size estimate per ZL
   once the mesh is on disk.
2. A downloader that reuses the imagery pipeline's providers and rate
   limits (`getortho` chunk fetch and the broker), writes to `cache_dir`
   with the normal naming, skips files already there, and reports to the
   Activity panel with a byte total.
3. Protection from cache cleanup: the imagery cache limit
   (`CacheCleanupWorker`, `disk_budget_manager`) would delete pinned
   imagery. It needs the pins (per tile) and to leave those chunks out of
   its budget, the same way `evict` does for the mesh.
4. A UI choice of zoom levels (capped at the scenery's own ZL) with the
   estimate shown per ZL before starting.

## Building and publishing indexes

Indexes live in the `tile-index` pre-release of this repository
(`tile_stream.INDEX_BASE_URL`). Installed packages check them for updates
once a week with `If-Modified-Since`.

XP11 legacy set, from kubilus1/autoortho-scenery (reads only zip
directories remotely, plus the `terrain/` block of each zip for the `.ter`
check):

```bash
python scripts/build_tile_index.py --out dist/tile-index/tile_index_xp11.json.gz
python scripts/build_tile_index.py --kind overlay --out dist/tile-index/overlay_index_xp11.json.gz
gh release upload tile-index dist/tile-index/*.json.gz --clobber
```

A set hosted as one release per area, from local copies of the release
files (repeat `--local-dir` per area; `{area}` is filled from each folder's
`<area>_info.json`). The XP12 set is hosted at
`ProgrammingDinosaur/autoortho-scenery-xp12`, one release per area tagged
`<area>_v1.4`: `afr-n`, `afr-s`, `asi-c`, `asi-n`, `asi-s`, `aus-pac`,
`eur-e`, `eur-w`, `greenland`, `na-n`, `na-s` and `sa` (18,695 tiles,
238 GB). Each build replaces the index, so pass every area:

```bash
python scripts/build_tile_index.py \
    --local-dir D:/xp12/afr-n/release --local-dir D:/xp12/afr-s/release ... \
    --url-base "https://github.com/ProgrammingDinosaur/autoortho-scenery-xp12/releases/download/{area}_v1.4/" \
    --variant xp12 --name "XP12 set (flightwusel v1.4)" \
    --out dist/tile-index/tile_index_xp12.json.gz
```

Index entries are `[asset, header_offset, compressed_size, size, method,
crc]`; the format version is `tile_stream.INDEX_FORMAT`. Bump it only for
incompatible changes, since installed copies refuse unknown formats.

- **Format 2** (written by the builder) packs the entries, because parsing
  a million JSON lists costs about 500 MB that Python keeps afterwards.
  `files` holds `names` (DSFs and anything else, sorted), `masks` (base64
  of the water masks' sorted little-endian uint64 keys,
  `row << 36 | col << 8 | zoom`) and `entries` (base64 of 24-byte records,
  `<HQIIHI`, names first, then masks). See `tile_stream.pack_files`. The
  full XP12 index (1.35 million files) loads in about 0.2 s and uses about
  50 MB.
- **Format 1** stores `files` as `{path: entry}`. It's still read, and
  AutoOrtho 3.3.0 reads only format 1. So the XP11 indexes stay format 1
  under their current names. Publish a format 2 index only for a source that
  3.3.0 shows as **Soon**, or under a new file name.
- `--convert INDEX --out NEW` rewrites an index in the current format
  without reading the releases.

## Repackaging a solid archive

Packages shipped as one solid `.7z` (like the XP12 flightwusel set) can't be
streamed. `scripts/repack_scenery.py` turns an extracted package into
kubilus-style release zips: whole 1x1 degree tiles per zip, each under
1900 MB, DSFs 7z-compressed inside (X-Plane reads them natively), and an
`<area>_info.json`. The result also works with the classic region installer.
Release notes credit the original package and name the zip that holds its
`README.txt` and `_docs/` (`asi-n` ships no `README.txt`).

```bash
7z x z_ao_greenland_zl16_o4xp1.40.13+je_v1.4.7z -oD:/xp12/greenland/extracted
python scripts/repack_scenery.py D:/xp12/greenland/extracted --area greenland \
    --name "Greenland XP12 (flightwusel v1.4)" --out D:/xp12/greenland/release
gh release create greenland_v1.4 D:/xp12/greenland/release/* \
    --repo ProgrammingDinosaur/autoortho-scenery-xp12
```

Then rebuild the set's index (above) with every area uploaded so far.

## Adding a scenery set

Add an entry to `tile_stream.SOURCES` with its label, `ortho_index` and
`overlay_index` (or `None`) URLs. The first available entry is the default
in the panel and the setup wizard (currently `xp12`). Set
`available: False` to show it as **Soon** until its index is published. The panel turns
**Include overlays** off for a set without overlays.

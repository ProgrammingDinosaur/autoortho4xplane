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
| Index builder | `scripts/build_tile_index.py` |
| Repackager for solid archives | `scripts/repack_scenery.py` |

### Streaming packages

A streaming package is a folder in `z_autoortho/scenery/` holding
`ao_stream.json` (the marker) and `tile_index.json.gz`. It is mounted like
an installed region:

- `z_ao_stream_<set>`: base mesh (`kind: ortho`).
- `yAO_stream_<set>_overlays`: overlays (`kind: overlay`), optional.

The marker records the set, the index URL and its `Last-Modified`, and the
disk limit; overlays get a quarter of the ortho limit. Code only ever
deletes folders that contain a marker.

### What happens on open

1. `readdir` lists every DSF in the index; installed regions' DSFs are
   hidden, so installed tiles always win.
2. When X-Plane opens a DSF that isn't stored yet, it is fetched (zip local
   header + member) and stored; 7z-compressed DSFs are unpacked. The DSF's
   terrain list registers its imagery tiles with `TerrainTileLookup` and
   queues its water masks. The 8 neighbouring tiles are prefetched in a
   background pool that never delays foreground requests.
3. `.ter` files are generated from their names with the Ortho4XP template
   (`synthesize_ter`). Index builds check every published `.ter` against
   it; the few that differ are stored in the index as exceptions.
4. If a fetch fails, an ortho DSF is served from X-Plane's Global Scenery and
   an overlay DSF as a valid empty overlay, so X-Plane never sees an error.

### GitHub access

`releases/download/...` redirects to a signed URL valid for about an hour.
`RemoteAssets` reuses the signed URL until shortly before it expires, so
normal flying makes about one request to github.com per zip per hour; the
rest go to the asset CDN. On 403, 429 or 5xx, fetches pause (honouring
`Retry-After`, 60 s doubling up to 15 min) and opens fall back meanwhile.

### Disk limit

After fetches, and at most once a minute, the least recently used tiles are
removed until the package is under 90% of its limit. Files used in the last
two hours are kept. Use is tracked by file mtime (touched on open).

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
`<area>_info.json`):

```bash
python scripts/build_tile_index.py \
    --local-dir D:/xp12/greenland/release --local-dir D:/xp12/eur/release \
    --url-base "https://github.com/ProgrammingDinosaur/autoortho-scenery-xp12/releases/download/{area}_v1.4/" \
    --variant xp12 --name "XP12 set (flightwusel v1.4)" \
    --out dist/tile-index/tile_index_xp12.json.gz
```

Index entries are `[asset, header_offset, compressed_size, size, method,
crc]`; the format version is `tile_stream.INDEX_FORMAT`. Bump it only for
incompatible changes, since installed copies refuse unknown formats.

## Repackaging a solid archive

Packages shipped as one solid `.7z` (like the XP12 flightwusel set) can't be
streamed. `scripts/repack_scenery.py` turns an extracted package into
kubilus-style release zips: whole 1x1 degree tiles per zip, each under
1900 MB, DSFs 7z-compressed inside (X-Plane reads them natively), and an
`<area>_info.json`. The result also works with the classic region installer.

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
`overlay_index` (or `None`) URLs. Set `available: False` to show it in the
panel as **Soon** until its index is published. The panel turns
**Include overlays** off for a set without overlays.

# Plan: Files mode, streaming without FUSE

Status: **beta: everything that needs no real-flight data is built; not yet flown** (see §13 for what was built,
what was measured on Windows and where the implementation departs from this plan).
Related: [settings-refactor-proposal.md](settings-refactor-proposal.md) (settings registry, governor),
[governor-behind-detection-plan.md](governor-behind-detection-plan.md) (read-based signals that files mode replaces).
Prior art: OrthoStudio XP Live (`D:\Programs\orthostudio-xp`, `docs/specs/live.md` and `docs/specs/live/*.md`).
It is GPLv3 and AutoOrtho is Apache 2.0, so **no code is copied**. We reuse its published measurements
(facts) and the general technique, and reimplement everything (see §12).

---

## 1. Summary

Today AutoOrtho mounts each `z_ao_*` region through FUSE (WinFsp on Windows, macFUSE on macOS). X-Plane
opens a `.dds`, and AutoOrtho downloads, builds and returns the bytes while X-Plane waits on the read.

**Files mode** removes the virtual file system. X-Plane reads ordinary files from a real scenery folder.
AutoOrtho runs *beside* X-Plane: it predicts which mip level X-Plane will read of each texture, and when,
and writes the texture to disk before that happens. X-Plane picks up the new file by itself, because it
reopens a texture file every time it wants a sharper level.

What changes for users:

- No WinFsp, macFUSE or Dokan to install. No kernel extension, no driver.
- X-Plane never blocks on AutoOrtho in flight. If AutoOrtho crashes, X-Plane keeps flying over what is
  already on disk.
- At a flight's start, a small X-Plane plugin holds the loading screen until the area around the
  aircraft is built.
- The cost: a texture that is late shows a neutral grey or a blurrier level until X-Plane's next read.
  Disk writes go up, roughly 3× in OrthoStudio's measurements.

Files mode ships as an **opt-in second backend**. FUSE stays the default until files mode has proved
itself in real flights (§10, Phase 8).

---

## 2. How X-Plane reads ortho textures (the facts this design relies on)

These were measured by OrthoStudio's probe flights (X-Plane 12.4.4, macOS) and partly confirmed in its
later releases. Each one is an **assumption** for us, numbered so code and tests can refer to it. The
phase that checks it on Windows is given for each.

| # | Assumption | Source | Checked on Windows in |
|---|---|---|---|
| A1 | X-Plane opens a texture file again for every sharper level it wants, and never reuses an old mapping. 1,276 of 1,276 upgrades in the probe were a new `open()` + `mmap`. A file replaced by rename is therefore seen at its next upgrade. | probe-flight-1.md | Phase 0 (FUSE telemetry), Phase 3 |
| A2 | When X-Plane loads a DSF it reads only the header and the levels of 256 px and smaller (about 45 KB of an 11 MB file). | probe-flight-1.md | Phase 0 |
| A3 | The level X-Plane wants depends on the distance from the **aircraft** (not the camera) to the texture's centre, in fixed rings. For a ZL16 texture (about 7 km across) the corrected rings are: level 3 (512 px) within 81.4 km, level 2 within 42.9 km, level 1 within 23.7 km, level 0 (4096 px) within 13.6 km. A ZL18 texture uses the same law 4× closer. | rings.py docstring, levels-test.md | Phase 0 |
| A4 | Levels are counted from the **top of the file**, whatever its size. An 8192 px file (imagery one zoom above the mesh) is read whole where a 4096 px file's level 0 would be. | live.md "The 8192 px level" | Phase 0 |
| A5 | The rings grow with screen width and as the field of view narrows: `scale = clamp((width / 2704) × tan(65°/2) / tan(fov/2), 1, 4)`. | probe-flight-2.md | Phase 0 |
| A6 | Below texture quality "Maximum", level 0 is never read. Each lower notch is assumed to drop one more level. | probe-flight-1.md | Phase 0 |
| A7 | At a flight's start X-Plane saves `_last_start` in `Freeflight.prf`, loads the aircraft (plugins get `XPLM_MSG_PLANE_LOADED`, then `LIVERY_LOADED`), and only then reads the scenery. A plugin that does not return from that message holds the whole load. | hold-test.md | Phase 5 |
| A8 | X-Plane keeps a window of 4 × 3 DSF tiles around the aircraft. It moves a column when the aircraft is 0.15° past a column's middle, and a row 0.1° past a row's edge. | evict.py constants | Phase 2 |
| A9 | A built file can be replaced while X-Plane runs. On Windows a file X-Plane has open or mapped may refuse replacement; OrthoStudio retries for 2 s. | filesystem.py | **Phase 3, decisive** |
| A10 | X-Plane never reads a coarser level again once it has a sharper one. A texture made coarser behind the aircraft is never shown coarser than X-Plane already holds. | live.md "Every 10 s" | Phase 6 |

AutoOrtho already has a different but related model in [paging_model.py](../../autoortho/paging_model.py).
It uses a single constant `k` and the camera's distance to the tile's near edge, and is fitted from FUSE
reads. Files mode uses the ring table above as its default and calibrates it with FUSE telemetry (Phase 0).

**AutoOrtho has an advantage here.** Its FUSE mode sees every real read on every user's machine, Windows
included. Phase 0 turns that into calibration data before files mode even exists. OrthoStudio never had
that.

---

## 3. Goals and non-goals

**Goals**

1. Stream ortho with no virtual file system on Windows, macOS and Linux.
2. Reuse AutoOrtho's existing pipeline: chunk download, the HTTP/2 broker, the native `aopipeline`
   encoder, seasons, providers, prefetch inputs, SimBrief.
3. Keep FUSE mode working and unchanged in behaviour throughout. Switching modes is a setting.
4. X-Plane is never blocked in flight. At a start it is held only until the start area is built, and
   only by the plugin.
5. Built textures persist across sessions. A second flight in the same area starts warm.

**Non-goals (first release)**

- Replacing the mesh. Files mode reads the installed `z_ao_*` regions as they are.
- Night-light textures and decal changes to `.ter` (OrthoStudio does these; out of scope).
- Sharp textures for external cameras far from the aircraft (A3: X-Plane doesn't read them either).
- Removing FUSE mode.

---

## 4. Architecture overview

```
 ┌──────────────────────────── AutoOrtho parent process (UI, today's AOMount) ────────────────────────────┐
 │  settings · governor (files-mode signals) · download broker · stats/log servers · process supervisor   │
 └───────────────▲───────────────────────────────────────────────────────────────────────────▲────────────┘
                 │ stats / control (existing channels)                                         │ spawn/stop
 ┌───────────────┴──────────────────────────── files worker process ──────────────────────────┴────────────┐
 │                                                                                                         │
 │  FlightFeed ──► Planner ──► Scheduler (urgency heap) ──► TextureService.build() ──► TextureStore.write() │
 │  (UDP datarefs,   │  ▲          │                          (getortho Tile,            (sparse DDS,     │
 │   SimBrief,       │  │          │                           ChunkGetter, broker,       temp+rename,    │
 │   X-Plane prefs)  │  │          │                           aopipeline encoder)        sqlite index)   │
 │                   ▼  │          ▼                                                                       │
 │             Catalogue   Materializer (squares window: neutral links, mask links)                        │
 │             (.ter LOAD_CENTER                         │                                                 │
 │              per square)                              ▼                                                 │
 │                                    Evictor (disk budget: coarsen behind the aircraft)                   │
 │  Gate (UDP 127.0.0.1) ◄────────────────────────────────────────────────────► X-Plane plugin (C, XPLM)  │
 └─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                 │ writes                                                     ▲ reads plain files
                 ▼                                                            │
   <files root>/z_ao_eur/{Earth nav data, terrain, textures}  ◄── junction ── Custom Scenery/z_ao_eur
```

### 4.1 Process model

- **One files worker process** for all regions, started by the parent the way mount workers are today
  (`AOMount.launch_mount_worker` / `process_supervisor`). It runs with `AO_RUN_MODE=files_worker`.
  FUSE mode runs one worker per region because each worker owns a mount; files mode has no mount, and
  `getortho`'s module-level state (TileCacher, getters, caches) is easier to keep in one process.
- **Encoding stays on threads.** `aopipeline` is C called through ctypes, so it releases the GIL.
  (OrthoStudio needed process pools because its encoder is Python/numpy.) Builds run at below-normal
  thread priority through the existing `_native_build_context`, so X-Plane's threads come first.
- **File operations** (links, renames, deletions) go through a batch API (§5.3). They start on one
  dedicated thread. If profiling shows GIL contention (OrthoStudio measured a start going from 4.4 s
  to 8.1 s with a busy sibling thread), the same batches move to a `spawn` subprocess with no API change.

### 4.2 New package layout

```
autoortho/files_mode/
  __init__.py
  worker.py          # process entry point (mirrors mount_worker.py): start/stop, control channel
  session.py         # FilesSession: wires all components, lifecycle, health
  pack.py            # on-disk layout per region, setup/teardown, junctions in Custom Scenery
  fsops.py           # portable file ops: replace, sparse, hard link, junction, allocated size
  fsops_windows.py   # ctypes: FSCTL_SET_SPARSE, FileRenameInfoEx, junction create/remove, Search scope
  catalogue.py       # textures per DSF square, from .ter LOAD_CENTER; cached index per region
  window.py          # X-Plane's DSF window model (A8) and the squares to materialize
  materializer.py    # makes/removes texture names per square: neutral links, mask links
  neutral.py         # neutral texture pool per file size, NTFS 1,023-link limit
  rings.py           # level model (A3-A6), calibratable
  planner.py         # every 2 s: what level each texture needs, and its urgency
  scheduler.py       # urgency heap, admission, pauses; feeds TextureService
  store.py           # TextureStore: sqlite index + DDS writer (sparse, temp+rename)
  evict.py           # disk budget: coarsen/remove textures X-Plane can't be showing
  gate.py            # UDP server for the plugin protocol (§5.8)
  xpprefs.py         # X-Plane.prf (texture quality), Freeflight.prf (_last_start)
  stats.py           # files-mode counters published to the parent

autoortho/texture_service.py   # Phase 1: the seam between demand and getortho's tile building
autoortho/xplane_plugin/       # C plugin sources, build scripts, prebuilt binaries per platform
tools/files_mode/fake_xplane.py  # stand-in X-Plane that reads files the way A1-A4 say (tests, CI)
```

---

## 5. Component design

### 5.1 Mode selection and lifecycle

**Setting.** Add `autoortho.streaming_backend` to the settings registry
([settings_registry.py](../../autoortho/settings_registry.py)), tier `ESSENTIAL`, values `fuse` (default)
and `files`. Add `files.root`, `files.disk_gb` (default 20, floor 10; floor 20 when any zoom setting
produces 8192 px textures) and `files.camera_reach_km` (default 10), all tier `EXPERT`. Everything else in
files mode is internal (§5.6 explains why the prefetch and wait settings do not apply).

**Parent side** ([autoortho.py](../../autoortho/autoortho.py)). `AOMount.mount_sceneries()` branches on
the backend:

```python
def mount_sceneries(self, blocking=True):
    if self.cfg.autoortho.streaming_backend == "files":
        return self.start_files_mode(blocking)
    ...  # today's FUSE path, unchanged
```

`start_files_mode()`:

1. `_ensure_parent_services()` (log server, stats manager, broker): unchanged.
2. Preflight (§5.2.4). On failure, show the reason in the UI and do **not** fall back to FUSE silently.
3. Launch the files worker through the existing supervisor with
   `--mode files --roots <region roots...> --custom-scenery <path>`.
4. Wait for the worker's `READY` (pack set up, junctions in place, gate listening), with a timeout.
5. `start_governor()` with the files-mode profile (§5.11).

`unmount_sceneries()` sends `shutdown` on the control channel. On shutdown the worker:

1. Answers any pending plugin `HOLD` with `GO`.
2. Stops the planner and scheduler, lets in-flight writes finish (bounded at 5 s), flushes the index.
3. **If X-Plane is running**, leaves the junctions in place (X-Plane keeps flying over what is on disk).
   Before leaving, it materializes neutral names for squares up to `RING_ON_EXIT = 3` rings beyond the
   window, so X-Plane doesn't hit missing textures soon after. This is metadata only, a few seconds.
4. **If X-Plane is not running**, removes the junctions (§5.2.3).

Detecting whether X-Plane is running: look for an `X-Plane` process (psutil, already a dependency through
the stats code), and confirm with the plugin's last `HELLO` (§5.8).

**`diagnose()`** in files mode checks: every expected junction exists and resolves to the files root,
a known texture opens and reads a valid DDS header, the gate answers on loopback, and the plugin is
installed at the expected version.

**Mode switch.** Changing the backend needs the session stopped. Switching to FUSE removes files-mode
junctions first. FUSE needs the mount point absent (WinFsp) or empty (macFUSE), and both modes use the
same `Custom Scenery/z_ao_<region>` names. Switching to files removes stale FUSE mount folders through
the existing `cleanup_stale_mount_folders`.

### 5.2 On-disk layout (the pack)

#### 5.2.1 Where things live

```
<files root>                          default: <paths.scenery_path>/z_autoortho/files
  _ao/
    textures.sqlite                   TextureStore index (WAL)
    catalogue/<region>.v1.bin         Catalogue cache per region (§5.4)
    pack.json                         regions, fingerprints, layout version
    neutral/                          neutral pool (§5.3.3)
  z_ao_eur/
    Earth nav data/                   real folder: hard links to each region DSF (same volume),
                                      or a junction to the region's folder (other volume)
    terrain                           junction -> <region root>/terrain   (read-only, never written)
    textures/                         real folder: managed by files mode
      100000_125184_BI18.dds          neutral link | built sparse DDS
      100000_148176_ZL18.png          hard link to the region's mask (or a copy on another volume)
  z_ao_na/ ...
Custom Scenery/
  z_ao_eur  -> junction -> <files root>/z_ao_eur
```

Why this shape:

- **Same pack names as FUSE.** `Custom Scenery/z_ao_eur` exists in both modes, so `scenery_packs.ini`
  order and the overlays (`yAutoOrtho_Overlays`) don't change. When AutoOrtho isn't running, the
  junctions are removed, exactly as FUSE mount points disappear today. X-Plane sees the same thing.
- **`terrain` as a junction, not copied per square.** AutoOrtho's `.ter` files are used unmodified
  (OrthoStudio copies them because it edits them for decals and night lights). The default root sits next
  to the regions on the same volume. One junction replaces copying tens of thousands of `.ter` files per
  start; OrthoStudio measured 36,677 files in 8.1 s at a Frankfurt start versus 12,906 textures in 2.7 s.
- **DSFs as per-file hard links** (same volume). They cost nothing, and they let time exclusion swap
  single DSFs (§5.10). On another volume, the whole `Earth nav data` becomes a junction and time
  exclusion is unavailable in files mode (the UI says so).
- **`textures` is the only folder files mode writes.** A built texture *is* the cached artifact: there is
  no second copy in `DynamicDDSCache` (§5.5.5).

`.ter` files reference textures as `../textures/<row>_<col>_<maptype><zoom>.dds`, and also carry
`LOAD_CENTER <lat> <lon> <size_m> <px>`. That is the paging hint X-Plane itself uses, and the catalogue
reads it (§5.4).

#### 5.2.2 Setup (first start, or a region fingerprint changed)

For each region root found under `z_autoortho/scenery` (same discovery as
`AOConfig.refresh_derived_paths`):

1. Fingerprint the region: mtime and identity of `terrain/` and `Earth nav data/`, plus the count of DSFs.
   Compare with `pack.json`.
2. If unchanged, done. Otherwise **never while X-Plane runs**: a DSF of before next to a `.ter` of now
   must not happen. If X-Plane is running, refuse with a clear message.
3. Create `z_ao_<r>/Earth nav data/<+40+000>/*.dsf` as hard links, or a junction if cross-volume.
4. Create the `terrain` junction.
5. Hard-link every `*.png` from the region's `textures/` into the pack's `textures/`. These are water
   masks, a few thousand per region; copy them on another volume.
6. Remove leftover `.ao-tmp-*` files from a crash.
7. Write `pack.json` last, so a region counts as set up only once it's complete.

Regions added later are set up at the next start. A removed region gets its pack folder removed.
Built textures of the remaining regions are kept.

#### 5.2.3 Junctions: creation and safe removal

Directory junctions need no admin rights or Developer Mode on Windows. Use symlinks on macOS and Linux.

**Safety rule (must be enforced in code and covered by tests): never call a recursive delete on any path
inside or through Custom Scenery.** Removal is:

```python
def remove_pack_link(link: Path, files_root: Path) -> None:
    if not is_link_or_junction(link):          # os.path.isjunction / is_symlink (Python 3.12+)
        raise RefuseToRemove(link)             # a real folder: never touched
    if not resolves_under(link, files_root):   # someone else's link: never touched
        raise RefuseToRemove(link)
    os.rmdir(link) if is_junction(link) else os.unlink(link)   # removes the link, not the target
```

Before creating a junction, the target name must be absent, or be our own junction, or be an empty
stale FUSE mount folder (reuse `cleanup_stale_mount_folders`). Anything else aborts start with a message.

#### 5.2.4 Preflight checks

- The files root's file system is NTFS, APFS, ext4, btrfs or xfs: it must support **hard links and
  sparse files or clones**. Refuse exFAT, FAT32 and HFS+: every texture would take its full 11 or 45 MB.
  Probe it by creating a sparse 64 MB file and checking its allocated size.
- Free space ≥ `files.disk_gb` + 2 GB.
- The files root is not on a disk that is missing (an external drive unplugged): refuse, never create the
  folder on the system disk instead.
- Windows: add a Windows Search crawl-scope exclusion for the files root (§5.12).

### 5.3 Materializer: texture names per square

X-Plane opens every texture named by the `.ter` files of a DSF when it loads that DSF. So every texture
of every square in X-Plane's window must exist as a file. Creating all of them for a whole region is too
expensive: Europe alone has about 400,000 textures, which took OrthoStudio 4 minutes and needed APFS
clones. So names exist only for **X-Plane's window plus one ring** around it.

#### 5.3.1 Window model (`window.py`)

Port the behaviour of A8 (reimplemented from the measured constants):

- `XPLANE_COLUMNS = 4`, `XPLANE_ROWS = 3`, `COLUMN_MOVE_DEG = 0.15`, `ROW_MOVE_DEG = 0.1`,
  `HYSTERESIS_DEG = 0.25`.
- `XPlaneWindow.follow(lat, lon) -> moved: bool`. It tracks the column boundary and the row the window is
  centred on, moving them only past those thresholds. A jump of more than 2° resets it.
- `xplane_tiles(lat, lon) -> set[(lat_sw, lon_sw)]`: the tiles X-Plane may hold, given its lag.
- `squares_to_materialize = window ∪ ring(RING=1)`, plus a ring around SimBrief route points within the
  next 10 minutes when a plan is active.
- `squares_to_release = present − (window ∪ ring(KEEP=2))`, never while X-Plane is loading scenery.

#### 5.3.2 Operations

For each square to make, for each texture of that square (from the catalogue):

- Built in `TextureStore` → nothing to do (its file is already there).
- Not built → hard link to the neutral pool file of the right size class (§5.3.3).

For each square to release: remove texture names that are neutral links. **Built textures stay**: they
are the warm cache, and the disk budget handles them. Mask links stay (small, and shared with other
squares).

API (batchable, so it can move to a process later):

```python
@dataclass(frozen=True)
class FileOp:
    kind: Literal["link_neutral", "unlink_if_neutral", "link_mask"]
    dst: str            # absolute path under <files root>
    size_class: int     # file top px for neutral (4096, 8192, 2048...)

def run_ops(ops: Sequence[FileOp]) -> OpsResult   # per-op errors, never raises for one failure
```

- Order squares by distance to the aircraft, the window first.
- A square whose ops fail (disk full, file held open) is retried after 10 s, doubling up to 300 s. It
  holds a start for at most 60 s, then the log says so and the start proceeds.
- Measured scale (OrthoStudio, macOS SSD): about 12,900 names for the densest European start in 2.7 s, and
  1,680 names for the heaviest new column in flight in 0.4 s. On Windows, measure the same in Phase 2.
  Target: under 10 s for a start window on an NVMe disk.

**Race with the builder.** The builder may finish a texture while the materializer links a neutral name
over it, or the reverse. All writes to a texture name go through `TextureStore`, which holds a per-name
lock. The materializer asks the store `link_neutral_if_absent(name)`, never creates names directly.

#### 5.3.3 Neutral pool (`neutral.py`)

- One neutral DDS per **size class** (file top px: 2048, 4096, 8192, 16384). The header must match the
  real texture's dimensions, so X-Plane's level arithmetic is the same before and after the swap (A4).
- Content: a uniform dull grey-green BC1 block at **every** level, fully written, not sparse. It is shared
  through links, so the size doesn't matter. Use the colour of `pydds._get_missing_color_bc1_block`, so
  FUSE fallbacks and files-mode neutrals look the same.
- NTFS allows 1,023 names per file. The pool keeps copies `neutral_4096_000.dds`, `_001`, … and links to
  the first with room (catch `ERROR_TOO_MANY_LINKS` (1142) and move on). APFS and HFS+: 32,767. ext4: 65,000.
- `is_neutral(path)` compares `(st_dev, st_ino)` with the pool's inodes. On Windows, compare the file ID
  via `os.stat` `st_ino`, which Python fills from the file index on NTFS.

### 5.4 Catalogue: which textures exist, and where (`catalogue.py`)

Built once per region and cached in `_ao/catalogue/<region>.v1.bin`; rebuilt when the region fingerprint
changes.

- Scan `terrain/*.ter`. For each texture name, record `(row, col, maptype, zoom)` from the name, and
  `(lat, lon, size_m, px)` from `LOAD_CENTER`. Group them by DSF square (the floor of the centre's lat/lon;
  a texture crossing a square edge is added to every square it touches).
- Kinds of `.ter` per texture (`""`, `_water_overlay`, `_sea_overlay`) all name the same `.dds`; dedupe.
- Storage: one compact binary per region (struct array plus a square → offset index) or a sqlite table.
  Loading must be under 1 s for the whole planet: about 3.7 M textures, which OrthoStudio reads per square
  in 0.7 s.
- Reuse the scanning code of `getortho.TerrainTileLookup` where it helps. The catalogue replaces the
  "discover the maptype when X-Plane opens a file" logic (`register_discovered_maptype`): every maptype
  in the `.ter` names is known up front, including custom Ortho4XP tiles placed in `z_autoortho/scenery`.
- The `ZL` and empty maptypes keep the file name the `.ter` gives. Only the imagery source is resolved,
  through `_resolve_dds_maptype`'s rule (maptype override, else BI).

Listing Europe's 684,000 `.ter` took OrthoStudio 0.9 to 2.4 s on a Mac and 57 s on a slow cloud Windows
disk. Build the catalogue **in the background as soon as the UI opens**, and at the end of a region
install, never on the start path.

### 5.5 Building and writing textures

#### 5.5.1 The seam: `texture_service.py` (built in Phase 1)

```python
@dataclass(frozen=True)
class TextureKey:
    row: int
    col: int
    maptype: str          # as named in the .ter (may be "ZL" or custom)
    zoom: int             # the texture's own zoom (from its name)
    @property
    def name(self) -> str: return f"{self.row}_{self.col}_{self.maptype}{self.zoom}.dds"

class DemandSource(str, Enum):
    FUSE_READ = "fuse_read"; PLANNER = "planner"; START = "start"; PREFETCH = "prefetch"

@dataclass(frozen=True)
class Demand:
    key: TextureKey
    level: int            # mip index from the top of the file: 0 = full size
    urgency_s: float      # seconds until X-Plane reads that level; <= 0: X-Plane may be reading it now
    source: DemandSource

@dataclass(frozen=True)
class BuildPolicy:
    max_seconds: float | None      # None: no deadline (files mode); FUSE passes its wait-band budget
    stuck_piece_s: float           # give up on one chunk after this, build it from the parent zoom
    allow_parent_fallback: bool
    priority: int                  # maps onto PRIORITY_* in getortho

@dataclass
class BuiltLevels:
    key: TextureKey
    max_zoom: int                  # imagery zoom of level 0 (> key.zoom for an 8192 px file)
    header: bytes                  # DDS header for the full file
    total_size: int
    levels: dict[int, memoryview]  # mip index -> compressed bytes, for each level built
    offsets: dict[int, tuple[int, int]]   # mip index -> (offset, length) in the file
    patchy: bool                   # some chunks missing, built from a parent zoom
    season_key: str | None         # identity of the colour table baked in
    provider: str

class TextureService:
    def __init__(self, tile_cacher: "getortho.TileCacher", executor): ...
    def build(self, demand: Demand, policy: BuildPolicy) -> BuiltLevels: ...   # blocking
    def submit(self, demand, policy, on_done: Callable[[BuiltLevels | Exception], None]) -> Future: ...
    def cancel(self, key: TextureKey) -> bool: ...
```

`build()` uses today's machinery, no new image code:

1. `tile = tile_cacher.open_tile(row, col, resolved_maptype, zoom)`. Promote `_open_tile` /
   `_close_tile` to public names, keeping the old names as aliases.
2. Build levels `demand.level … tile.max_mipmap` with
   `tile.get_mipmap(m, time_budget=TimeBudget(policy.max_seconds or FILES_MODE_MAX_S))`.
   Each level comes from its own zoom's chunks, as FUSE builds them, so files mode looks identical.
   **Important:** `get_mipmap` falls back to `get_maxwait()` when no budget is given. That is a FUSE-era
   wait band and must not apply here, so always pass an explicit budget (`FILES_MODE_MAX_S = 120`).
3. Copy each level out of `tile.dds.mipmap_list[m]` (verify the buffer attribute in `pydds.MipMap`;
   dense and sparse buffers differ) into `BuiltLevels.levels`.
4. Seasons: the season recolour (`Tile._recolour`, `season_colour_table`) runs at read time in FUSE mode.
   In files mode apply it at build time to the level bytes, and record `season_key`.
5. `tile_cacher.close_tile(...)`.

FUSE mode keeps calling `Tile.read_dds_bytes` exactly as today. Phase 1 changes no FUSE behaviour.

#### 5.5.2 File format

A built texture is a **full-size DDS** with exactly the header FUSE would serve (`pydds.DDS` for the
same tile: same dimensions, format BC1/BC3, same mip count). This keeps the files interchangeable with
what X-Plane sees through FUSE.

- Levels built: written.
- Levels finer than the sharpest built (not needed yet): **holes** in a sparse file. They read as zeros,
  which is black in BC1. So a hole must never be in a level X-Plane may read before the next build.
  The planner guarantees that with the margin (§5.6.3). The exception is the "copy up" rule (§5.6.5),
  which writes the BC1 2× upscale of the level below (`dxt_upscale.upscale_blocks`).
- Levels coarser than this build's levels: carried over from the current file if it was built, else
  written from the new build. All levels of a build come from their own zoom (as AutoOrtho builds them),
  never derived from the sharper level.

Disk per texture is counted in the file system's allocation unit: 64 KB for NTFS sparse data, 4 KB on
APFS and ext4. A far texture with only small levels still costs 64 KB on NTFS plus the header unit.

#### 5.5.3 Writer (`store.py`)

```python
class TextureStore:
    def write(self, built: BuiltLevels) -> WriteResult
    def link_neutral_if_absent(self, name: str, size_class: int) -> bool
    def coarsen(self, name: str, keep_from_level: int) -> int      # bytes freed
    def state(self, name: str) -> TextureState | None              # from the index
```

`write()` algorithm, all under the per-name lock:

1. `tmp = textures/.ao-tmp-<pid>-<name>`, opened with `O_CREAT | O_EXCL | O_WRONLY | O_BINARY`.
2. Make it sparse: on Windows `DeviceIoControl(FSCTL_SET_SPARSE)`, which must come **before** extending.
   Today's `DynamicDDSCache._create_dds_skeleton` uses `truncate()`, which is *not* sparse on NTFS without
   this flag: NTFS zero-fills up to the valid data length on the first write past it. Then set the end of
   file to `total_size`. On APFS, ext4 and xfs, truncate is already sparse.
3. Write the header and each level at its offset. POSIX: `os.pwrite`. Windows has no `os.pwrite`:
   use `os.lseek` + `os.write` on the temp file's private descriptor (nothing else touches it), or
   `WriteFile` with an `OVERLAPPED` offset through ctypes if a shared descriptor is ever needed.
4. Carry over coarser levels from the current file when it is a built one (`pread` of a few hundred KB).
5. No `fsync` (performance; a crash leaves only a temp file, which is removed at the next start). The
   rename is atomic.
6. `replace(tmp, final)`: see §5.5.4.
7. Update the index row in the same critical section.

On clone-capable file systems (APFS `clonefile`, btrfs/xfs `FICLONE`), an upgrade may clone the current
file and write only the new levels. This is an optimisation for Phase 6, not needed for correctness.

#### 5.5.4 Replacing a file X-Plane may have open (A9, the Windows risk)

Order of attempts on Windows:

1. `SetFileInformationByHandle(FileRenameInfoEx, FILE_RENAME_FLAG_REPLACE_IF_EXISTS |
   FILE_RENAME_FLAG_POSIX_SEMANTICS)` (Windows 10 1709+). With POSIX semantics the rename succeeds even if
   the destination is open by a process that allowed `FILE_SHARE_DELETE`. That process keeps the old file
   until it closes it, which is exactly what A1 needs.
2. Fallback: `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`, retried every 50 ms for 2 s.
3. Still refused (a mapped section without `FILE_SHARE_DELETE`): keep the temp file, requeue the
   texture with a 5 s backoff, and count `files_replace_refused`. **Never block the scheduler.**

**Fallback B, if Phase 3 shows replacement is refused during normal flight:** write **in place** into the
holes of the live file instead of renaming. Only levels X-Plane hasn't read yet are written. Because of
A1, X-Plane reopens and maps the file again before reading them, and it sees the new data. This needs
X-Plane's open to allow `FILE_SHARE_WRITE`, which Phase 3 records with Process Monitor. Keep `write()`
behind an interface so either strategy can be selected per platform.

macOS and Linux: `rename(2)` always succeeds over an open or mapped file.

#### 5.5.5 Index (`_ao/textures.sqlite`, WAL)

```sql
CREATE TABLE textures (
  name          TEXT PRIMARY KEY,     -- 100000_125184_BI18.dds
  region        TEXT NOT NULL,
  row INTEGER, col INTEGER, maptype TEXT, zoom INTEGER,
  max_zoom      INTEGER NOT NULL,     -- imagery zoom of level 0
  top_px        INTEGER NOT NULL,     -- size class
  built_level   INTEGER NOT NULL,     -- sharpest mip index built (0 = full); 99 = neutral
  copied_level  INTEGER,              -- a level holding a 2x copy of the level below, if any
  patchy        INTEGER NOT NULL DEFAULT 0,
  retry_at      REAL,                 -- next rebuild attempt for a patchy texture (backoff)
  season_key    TEXT,
  provider      TEXT,
  built_at      REAL, last_needed_at REAL,
  alloc_bytes   INTEGER               -- allocated size, for the disk budget
);
CREATE INDEX textures_region_rc ON textures(region, row, col);
```

- Writes are batched every 2 s from a single writer thread.
- At start, validate rows lazily: a texture is checked (magic `DDS `, size equal to `total_size`, not a
  neutral inode) the first time the planner touches it. A mismatch resets it to neutral.
- **Relation to `DynamicDDSCache`.** In files mode, the pack is the persistent DDS cache. Disable
  `DynamicDDSCache` writes in the files worker. Optionally, the first time a texture is built,
  seed it from a matching `DynamicDDSCache` entry (zstd-decompress, write), so users switching from FUSE
  start warm. JPEG chunk caches (`cache_dir`) are shared by both modes unchanged.

### 5.6 Planner (`planner.py`, `rings.py`)

#### 5.6.1 Inputs (`FlightFeed`)

From the UDP dataref tracker ([datareftrack.py](../../autoortho/datareftrack.py)), which already
subscribes to latitude, longitude, `y_agl`, `mag_psi`, groundspeed, `window_width`, `field_of_view_deg`
and `paused`. **Add:**

| Dataref | Use |
|---|---|
| `sim/flightmodel/position/hpath` | ground track (heading is wrong in crosswinds and on the ground) |
| `sim/flightmodel/position/vh_ind` | vertical speed: cruise rule on descent (§5.6.5) |
| `sim/graphics/scenery/async_scenery_load_in_progress` | pause while X-Plane loads scenery (§5.6.4) |
| `sim/graphics/view/view_x`, `view_y`, `view_z` | camera position (external views) |
| `sim/flightmodel/position/local_x`, `local_y`, `local_z` | aircraft in the same frame as the view |

Ground speed missing while the position moves (a position set from outside): derive motion from the last
two positions.

From [flighttrack.py](../../autoortho/flighttrack.py) / SimBrief: the active route, when the aircraft is
within 15 km of it and flying its way (within 100°, faster than 25 m/s).

From `xpprefs.py`: texture quality from `Output/preferences/X-Plane.prf` (key `_tex_res`; verify the
key on X-Plane 12.x on Windows), re-read when the file's mtime changes. Unknown means assume Maximum
(builds the most, never wrong).

#### 5.6.2 Level model (`rings.py`)

```python
RINGS_ZL16_KM = {3: 81.4, 2: 42.9, 1: 23.7, 0: 13.6}   # A3, corrected (+13 %)
LOAD_LEVEL = 4            # 256 px: what X-Plane reads at DSF load
MARGIN = 1.11             # build this much wider than X-Plane reads
REF_WIDTH_PX, REF_FOV_DEG, MAX_SCALE = 2704, 65.0, 4.0

def ring_km(level, zoom, size_m=None, lat=None) -> float
def screen_scale(width_px, fov_deg) -> float               # A5, never < 1
def finest_for_quality(tex_res) -> int                     # A6
def level_needed(distance_km, key, scale, finest) -> int   # level index counted from the 4096 px top
def file_level(level_4096, top_px) -> int                  # A4: map to the file's own mip index
```

- **Size scaling.** OrthoStudio scales rings by zoom only (`2^(16−zoom)`). X-Plane probably scales by the
  texture's ground size from `LOAD_CENTER`, and textures shrink with latitude. Until Phase 0 settles it,
  use the **larger** of the zoom-scaled ring and the size-scaled ring
  (`RINGS_ZL16_KM[L] × size_m / 7100`). Building early is safe; building late shows grey.
- **Calibration.** `rings.py` loads overrides from `_ao/rings.json` when present. Phase 0's FUSE
  telemetry produces it. It also feeds `paging_model.PagingMeasurements`, which can be kept as the fitter.
- 8192 px files (A4): their level 0 is read where a 4096 px file's level 0 would be; their level 1 at a
  4096 px file's level-1 ring, and so on. Build the 4096-and-smaller levels by the 4096 rings; level 0
  (8192) gets its own rule (§5.6.5).

#### 5.6.3 Pass every 2 s

1. Read the feed. Compute the path points: the aircraft now, plus points along the track up to
   `LOOKAHEAD_S = 90` s ahead, and along the SimBrief route over the same time when on route.
2. Candidate textures: those of squares within 300 km (catalogue by square).
3. For each texture:
   - `d_min` = closest approach of its centre to the path (point-to-polyline, great-circle approximated
     with local equirectangular coordinates; fine below 300 km).
   - `need` = `level_needed(d_min, ...)`, widened by `files.camera_reach_km` for all rings except the
     256 px one and level 0 of 8192 px files.
   - Also from the camera's own position in 3D, without the reach: an external view near the ground
     needs levels there.
   - `built` = from the index. If `need < built` (sharper wanted), emit a demand.
   - `urgency_s` = time until the aircraft reaches the ring of `need`, from distance along the path and
     ground speed. It is ≤ 0 if already inside the ring (X-Plane may be reading it now). Use urgency
     by distance (distance left before the ring) when stationary.
4. Hand the demands to the scheduler, which re-ranks queued work by the newest urgency (a texture queued
   earlier is ranked by this pass, not by the one that queued it).
5. Cost: a European start has about 13,000 textures in the window and about 50,000 within 300 km. Keep
   the pass under 50 ms: precompute centres in a numpy array per square, vectorise distance and ring
   tests, and only walk textures whose `need` changed.

#### 5.6.4 Pauses and holds

- **While X-Plane loads scenery in flight** (`async_scenery_load_in_progress == 1`) and for 5 s after:
  download, encode and file operations pause, except names for squares X-Plane is loading. Cap each pause
  at 120 s, counted again whenever X-Plane's window moves. OrthoStudio measured a third DSF column
  arriving 1 to 7.5 minutes late when it kept working during X-Plane's loads, and none later than 24 s
  when paused. **Never pause at a start**: X-Plane is waiting for us.
- **Paused sim** (`sim/time/paused`): keep building (the aircraft isn't moving, so it costs X-Plane
  nothing).
- **Memory pressure**: reuse [ram_brake.py](../../autoortho/ram_brake.py). Under pressure, only urgency
  ≤ 0 work, one build at a time, and no 8192 px builds ahead of time.

#### 5.6.5 Special rules

- **8192 px level 0** (ZL17 imagery on ZL16 terrain, ZL19 near airports): gets imagery only within
  `ring_km(0) × MARGIN` of the path, measured in 3D from height above ground. Urgency is the distance
  left before ring 0.
- **Cruise:** above 18,500 ft AGL, no 8192 px level-0 imagery; resume below 17,500 ft, or 3 minutes
  before the descent reaches 13,000 ft AGL (from `vh_ind`). At those heights the screen can't resolve it,
  and it was half of OrthoStudio's downloads at FL300.
- **Copy up:** an 8192 px texture within 1.1 × ring 0 whose level-0 imagery is still missing, but whose
  level 1 is built, gets level 1 BC1-upscaled into level 0 first (`dxt_upscale`). A late texture then
  looks like ZL16, never grey.
- **Imagery floor:** never use imagery coarser than ZL13 for any level. A ZL16 texture's 256 px level is
  built from shrunk ZL13 chunks. Bing's ZL12 and coarser come from different imagery whose colours clash
  (OrthoStudio measured mean colour differences of 21 to 31). Make the floor per provider
  (`min_photo_zl`, 14 for providers that change imagery below ZL14). This matters for files mode only,
  because FUSE builds the low levels only when X-Plane reads them, from what is cached.

### 5.7 Scheduler and downloads (`scheduler.py`)

- An urgency heap keyed by `TextureKey`, with the latest demand winning. Ties go to lower `need`
  (sharper is more visible) and then to distance.
- **Admission.** At most `MAX_IN_FLIGHT_CHUNKS = 1024` chunks awaited across textures, and
  `MAX_HELD_CHUNKS = 8192` held in memory. Past that, only urgency ≤ 0 work is admitted.
- **Builders in flight:** a third of the cores, max 4 (fewer with less RAM: 1 per 8 GB). At a start, while
  the plugin holds X-Plane, all cores but 2 (1 per 3 GB). Feed these as levers to the existing live
  settings (`_on_live_background_workers`, `BackgroundDDSBuilder.set_max_workers`).
- **Chunks** go through the existing `ChunkGetter` and broker. Map urgency onto chunk priority:
  urgency ≤ 0 → `PRIORITY_LIVE`, ≤ 60 s → imminent, else prefetch. The broker's live and prefetch split
  then keeps working unchanged.
- **Stuck chunks:** for urgency ≤ 0 work, give up on one chunk after `STUCK_S = 10` s and build it from
  its parent zoom (`allow_parent_fallback`). Mark the texture patchy, and rebuild it after 60 s, doubling
  up to 30 min. A patchy rebuild is never what a start waits for.
- **Live pressure.** Today `LivePressureController.live_read_start/end` is driven by FUSE reads. In files
  mode, the scheduler calls it for urgency ≤ 0 builds, so the existing background-yield logic
  (`_defer_background_build_if_live`) keeps meaning "X-Plane is about to need this".

### 5.8 X-Plane plugin (C, XPLM)

Sources in `autoortho/xplane_plugin/`: `ao_gate.c` (all platforms) plus a small native window for the
"held by AutoOrtho" message (optional in v1: X-Plane's own loading screen shows; log only).

**Lifecycle**

- `XPluginStart`: send `AO1 HELLO <xp_version> <pid>` to `127.0.0.1:<gate_port>`. If nothing answers
  within 300 ms (Windows: the socket read fails with `WSAECONNRESET` after ICMP port unreachable; disable
  that with `SIO_UDP_CONNRESET` so reads behave as on POSIX), AutoOrtho isn't running. **Remove stale
  AutoOrtho junctions** in `Custom Scenery` (only links resolving under the files root recorded in
  `Resources/plugins/AutoOrtho/files_root.txt`; never folders). X-Plane starts plugins before it scans
  scenery, so it never sees a pack with no texture names. This covers AutoOrtho crashing or the PC
  restarting with files mode active.
- `XPLM_MSG_PLANE_LOADED` (user aircraft, index 0): send `AO1 LOAD <lat> <lon>`.
- `XPLM_MSG_LIVERY_LOADED` (user aircraft): enter the **hold loop**: every 250 ms send
  `AO1 HOLD <lat> <lon> <seq>` and wait for a reply. Return from the message handler only on:
  - `AO1 GO`;
  - no reply to the first `HOLD` within 1 s (no AutoOrtho);
  - no reply for 5 s (AutoOrtho stuck or gone).

  There is no other time limit. A fixed 5-minute limit let X-Plane load with 211 textures unbuilt at LFPG.
- On Windows, while holding, call `DisableProcessWindowsGhosting()` once at start, so Windows doesn't
  grey out X-Plane as "Not Responding".
- Optional: a flight-loop callback that measures frame time and sends `AO1 FRAME <ms>` for frames
  ≥ 100 ms, so AutoOrtho can log stutters next to what it was doing.

**Gate (`gate.py`) answers**

- `AO1 WAIT <built> <total>` while the start area is not ready. Ready means: names exist for the window
  squares, and every texture with urgency ≤ 0 at the start position is built to its needed level,
  including the airport's ZL18 textures at full size.
- `AO1 GO` when ready; or when no chunk has arrived for 60 s (`STALL_S`: no internet, or the provider
  refusing; the UI and log say which); or when the user clicks Stop.
- The gate thread answers on its own if the planner loop is busy for more than 0.5 s, repeating the last
  `WAIT`, but never for a loop silent for more than 60 s. A busy loop must not release X-Plane early.

**Moving the aircraft in flight** (no aircraft load, so no plugin message). The planner sees a jump
of more than 20 km between two samples, or `Freeflight.prf` `_last_start` changes. It sends
`AO1 PAUSE` to the plugin, which runs `sim/operation/pause_on`, until the new area is ready, then
`AO1 RESUME`. This is v1.1 (Phase 7) if time is short.

**Build and ship**

- Use the official X-Plane SDK headers (XPLM 4.x) fetched at build time; link against `XPLM_64.dll`
  (Windows import library), the XPLM framework (macOS) or none (Linux, resolved at load).
- Build with MinGW-w64 (`win_x64/AutoOrtho.xpl`), clang universal (`mac_x64/AutoOrtho.xpl`, arm64 +
  x86_64) and gcc against an old glibc (`lin_x64/AutoOrtho.xpl`). Add jobs to CI next to the `aopipeline`
  builds ([aopipeline/Makefile.mingw64](../../autoortho/aopipeline/Makefile.mingw64) shows the pattern).
  Commit prebuilt binaries with a digest of their sources; a test fails if the sources changed without
  a rebuild.
- Install: the files worker copies the plugin into `X-Plane 12/Resources/plugins/AutoOrtho/` at start if
  the installed version differs, and writes `files_root.txt`. It is never removed automatically (it's
  inert when AutoOrtho isn't running, apart from the stale-junction cleanup). The UI offers "Remove
  X-Plane plugin".
- macOS: the `.xpl` must be signed or the user must allow it. Ad-hoc sign it in the build, and document
  Gatekeeper.

### 5.9 Disk budget and eviction (`evict.py`)

Every 10 s, never while X-Plane loads scenery, at most 50 textures per round:

- **Budget:** `files.disk_gb`, split between JPEG chunks and textures. Textures may use the room chunks
  leave unused. Allocated bytes come from the index (`alloc_bytes`), measured in allocation units.
- **Above budget:** evict down to 90 % of it.
  1. Textures **behind** the aircraft (relative to the track) first, then least recently needed at their
     built level.
  2. Only textures X-Plane **can't be showing sharper than what we leave**: outside `xplane_tiles()` (its
     window, with lag), and beyond the 512 px ring plus camera reach.
  3. **Coarsen** in place: punch holes over levels above 256 px (`FSCTL_SET_ZERO_DATA` on Windows,
     `fallocate(PUNCH_HOLE)` on Linux, `F_PUNCHHOLE` on macOS), or rewrite through `write()`. The file
     stays valid at 256 px. A10 says X-Plane never reads it coarser later.
  4. Beyond 300 km and outside materialized squares: delete the file and its row.
- JPEG chunks: oldest first. Reuse `disk_budget_manager.py` for the chunk side.

### 5.10 Features that depend on FUSE today

| Feature (where) | FUSE mechanism | Files mode |
|---|---|---|
| DDS serving (`autoortho_fuse.open/read`) | virtual file, built on read | pre-built files (§5.5) |
| Time exclusion (`autoortho_fuse.getattr/open` → `time_exclusion.get_redirect_path`) | redirects DSF opens to global scenery | swap the DSF **hard link** in `Earth nav data` for a link to the global-scenery DSF (same volume), only for squares outside `xplane_tiles()`; swap back the same way. Unavailable when `Earth nav data` is a cross-volume junction. |
| Maptype discovery (`register_discovered_maptype` on open) | learns custom maptypes from X-Plane's opens | not needed: the catalogue lists all `.ter` names |
| `ZL` / empty maptype (`_resolve_dds_maptype`) | resolved per request | the same rule at build time; file name unchanged |
| DSF-open prefetch (`_schedule_dsf_prefetch`) | prefetch triggered by X-Plane opening a DSF | replaced by the window model and the planner |
| Terrain load-ahead (`note_terrain_opened`, `load_ahead.py`) | `.ter` opens as early warning | replaced by the planner |
| Seasons (`Tile._recolour` at read) | colour applied per read | baked at build (`season_key`). When the key changes (date change, settings), requeue built textures in the window at low priority. |
| `AOISWORKING` marker and `.poison` | virtual files | `AOISWORKING` written as a real file in each pack root while running; `.poison` not needed |
| Paging telemetry (`PagingMeasurements.note_read`) | from reads | no reads in files mode. Calibration comes from FUSE-mode users (Phase 0). In files mode, record **late** textures instead (§5.11). |
| Governor "near reads sharp %" ([governor-behind-detection-plan.md](governor-behind-detection-plan.md)) | from reads | replaced by files-mode signals (§5.11) |
| Custom Ortho4XP tiles in `z_autoortho/scenery` | served like regions | handled like a region: its own pack folder |

### 5.11 Governor, stats and UI

- **Signals** (published by `files_mode/stats.py`):
  - `files_late`: textures whose needed level at urgency ≤ 0 is sharper than built. This is the files-mode
    equivalent of a blurry read. Split it into "never queued", "queued too late" and "building", as
    in the governor-behind plan.
  - `files_queue_depth`, `files_urgent_depth`, `files_build_rate`, `files_bytes_written`,
    `files_replace_refused`, `files_pause_s`, `files_patchy`.
  - Start: `files_start_s` (time held), and textures built and total at the start.
- **Governor** in files mode: levers are builders in flight, chunk concurrency, lookahead and margin.
  Ship v1 with the governor in **advise-only** mode for files mode (logs suggested moves), and switch it on
  once flight reports set the thresholds, as was done for FUSE.
- **UI:** a backend selector with a short explanation of the trade-off; a files-mode status card (start:
  held / building N of M; flight: late textures, queue, disk used of budget); preflight errors in plain
  words; plugin installed or version.

### 5.12 Windows specifics (checklist)

- Open every texture file with `O_BINARY`.
- Sparse: `FSCTL_SET_SPARSE` before extending. Allocation unit for sparse data: 64 KB.
- Replace: `FileRenameInfoEx` with POSIX semantics, then retry, then requeue (§5.5.4).
- Hard links max 1,023 per file (`ERROR_TOO_MANY_LINKS` = 1142).
- Junctions for folders (no Developer Mode needed). Never `shutil.rmtree` through them.
- **Windows Search:** hundreds of thousands of new files cost half a core in OrthoStudio's tests.
  Add a crawl-scope exclusion for the files root at each start (`ISearchCrawlScopeManager` through
  `comtypes`; no admin needed). File attributes don't keep files out of the index.
- **Microsoft Defender:** real-time scanning of each new DDS adds latency and CPU. The NSIS installer
  ([installer.nsi](../../installer.nsi)) runs elevated: offer an opt-in checkbox that adds a Defender
  exclusion for the files root (`Add-MpPreference -ExclusionPath`). Never do it silently.
- UDP: `SIO_UDP_CONNRESET` off on the gate and plugin sockets.

---

## 6. Phase 1 in detail: the `getortho` refactor

Scope it narrowly: only what files mode needs. No general cleanup of `getortho.py` (18,600 lines).

| Change | Where | Why |
|---|---|---|
| Add `texture_service.py` with the types in §5.5.1 and `TextureService.build()` over `Tile` | new | the seam |
| Public `TileCacher.open_tile` / `close_tile` (keep `_open_tile` / `_close_tile` as aliases) | getortho.py ~18387 | used by the service |
| `get_mipmap` callers in the service always pass an explicit `TimeBudget` | texture_service.py | avoid FUSE wait bands |
| Add a `season_recolour(level_bytes, mip, row, col, zoom)` helper extracted from `Tile._recolour` | getortho.py ~13878 | bake seasons at build |
| `AO_RUN_MODE` gate: accept `files_worker` wherever `mount_worker` / `macfuse_worker` are accepted | getortho.py ~4219, others via grep | worker-only code paths |
| `_active_mount_worker_count()` → `active_worker_count()`, returning 1 in files mode | getortho.py ~5429 and its 3 callers | per-worker budgets |
| `register_terrain_index` usable without a FUSE object; init moved out of `AutoOrtho.__init__` into a helper both modes call | autoortho_fuse.py ~790, getortho.py ~5260 | catalogue and prefetch inputs |
| `note_texture_requested` / `note_terrain_opened` behind a `DemandSink` callback list, so FUSE registers and files mode doesn't | getortho.py ~7133 | decouple demand |
| Make sure `getortho` imports nothing from FUSE modules (it doesn't today; add an import test) | tests | the files worker must start without WinFsp or macFUSE installed |

**Tests (exit criteria for Phase 1)**

1. **Equivalence:** for a fixture tile with cached chunks, `TextureService.build(level=0)` bytes equal the
   bytes `Tile.read_dds_bytes(offset, length)` returns over the whole file, for BC1 and BC3, with and
   without seasons.
2. **No FUSE import:** `python -I -c "import autoortho.texture_service"` with `mfusepy` / `refuse`
   blocked via a meta-path finder.
3. Existing test suite green. FUSE flight smoke test unchanged (startup time, read latencies within noise).

---

## 7. Data flows

**Start (files mode)**

1. The user clicks Run. The parent preflights and launches the worker. The worker sets up the pack (fast
   if unchanged), creates the junctions, installs the plugin, opens the gate, loads catalogues, and
   reports `READY`.
2. The user loads a flight. X-Plane writes `_last_start`, and the plugin sends `LOAD lat lon`. The worker
   resets the window model at that point, and the materializer creates names for the window plus ring
   (neutral links). The planner computes needs from the start position (no motion) with all cores.
3. The plugin sends `HOLD` every 250 ms; the gate answers `WAIT n/m`. When names exist and all urgency
   ≤ 0 textures are built, the gate answers `GO`.
4. X-Plane scans scenery, loads the DSFs and opens textures: they are real, sharp files.

**In flight**

Every 2 s: planner → scheduler → builds → writes. On a window move: materializer makes the new
column and row, releases far ones. On async scenery load: pause. Every 10 s: evictor.

**Stop / crash**

Stop: the shutdown described in §5.1. Crash: X-Plane keeps flying over the files. At the next X-Plane start without AutoOrtho,
the plugin removes the junctions. At AutoOrtho's next start, temp files are removed and index rows are validated lazily.

---

## 8. Testing strategy

- **Unit:** `rings` (table, scaling, quality, 8192 mapping), `window` (moves, hysteresis, wrap at ±180°),
  `catalogue` (parse `LOAD_CENTER`, square edges, dedupe kinds), `store` (sparse layout, carry-over,
  holes vs copies, per-name lock), `neutral` (link limit rollover), `fsops` junction safety
  (refuses real folders, foreign links), gate protocol (busy loop stand-in, stall `GO`).
- **Stand-in X-Plane** (`tools/files_mode/fake_xplane.py`): replays a recorded flight (flighttrack logs or a
  synthetic path) and reads files the way A1-A4 describe. On DSF load it opens the header plus the 256 px
  level; for each ring crossed it opens and maps the level. It reports reads that hit neutral, hole or
  copy, with how late they were. It runs in CI on Windows and Linux runners with a small fixture region
  and a fake imagery server. This is the main regression gate for the planner.
- **Windows file-system checks** (Phase 3, scripted): replacement while a process holds the file open with
  X-Plane's observed share flags; while mapped; link-limit rollover; sparse allocation numbers; junction
  removal; Windows Search exclusion effect.
- **Real flights** (Phase 4 onwards): a fixed set, each run cold and warm: a low VFR leg at 230 kt, a
  climb to FL300, a descent into a large airport, a fast jet at 480 kt, external-view sweeps, and moving the aircraft
  mid-flight. Record late textures, X-Plane stutters (plugin `FRAME`), CPU and memory, bytes written, and
  start-hold time. Compare with FUSE mode on the same machine and route.

---

## 9. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A9: Windows refuses to replace files X-Plane has mapped | files never update in flight | POSIX-semantics rename; fallback B (in-place writes to holes); measured in Phase 3, before the planner work |
| A1/A3 behave differently on Windows or in a future X-Plane | late or unseen upgrades | Phase 0 telemetry from FUSE users; rings in a data file; the stand-in harness encodes the assumptions so a change is caught by one recorded flight |
| X-Plane starts preferring the `.ktx2` twin it already checks for | files ignored | none needed now; monitor X-Plane release notes; `catalogue` can write `.ktx2` names later |
| Disk writes about 3× FUSE | SSD wear concern, slow disks | carry-over of levels, clone upgrades where available, disk budget, cruise rule; show bytes written in stats |
| Late textures visible (grey) | quality regressions vs FUSE | margin, camera reach, copy-up, start hold; `files_late` signal and governor; keep FUSE available |
| Slow small-file operations on Windows (many names per start) | long start holds | terrain as a junction (no `.ter` copies), batching, process move if GIL-bound, Search and Defender exclusions |
| Recursive deletion through a junction | user data loss | the `remove_pack_link` rule with tests; code review checklist item |
| Plugin crashes X-Plane | very visible | tiny C surface, no allocations in the hold loop, fuzz the protocol parser, run X-Plane in CI with the plugin loaded (headless start where possible) |
| Two backends double the maintenance | slower development | Phase 1 seam keeps one building pipeline; only demand and output differ |

---

## 10. Phases

Effort is for one developer familiar with the codebase. Each phase ends in something that can be run.

### Phase 0: Calibration telemetry in FUSE mode (0.5–1 week)
- In `autoortho_fuse.open/read`, for DDS reads record: texture key, mip level read (from the offset), open
  count per level per texture (checks A1), distance from the aircraft to the `.ter` `LOAD_CENTER`, its
  `size_m`, `window_width`, `fov`, texture quality, AGL.
- Write a session summary to the log and stats: per level, distance quantiles; reopen-per-level ratio;
  levels read at DSF load (A2).
- A tool `tools/files_mode/fit_rings.py` produces `rings.json` from collected logs.
- **Exit:** a few beta users' Windows flights confirm or correct A1-A6.

### Phase 1: The seam (1.5–2 weeks)
- §6 in full. **Exit:** §6 tests pass; FUSE behaviour unchanged.

### Phase 2: Pack and materializer (2 weeks)
- `pack.py`, `fsops*.py`, `neutral.py`, `catalogue.py`, `window.py`, `materializer.py`, preflight, junction
  safety, a files worker skeleton with `READY` / `shutdown`, and the setting (hidden behind `EXPERT`).
- **Exit (first X-Plane milestone):** X-Plane loads AutoOrtho's mesh through files mode with no FUSE, all
  neutral grey; flying across DSF columns keeps names ahead of X-Plane (no missing-texture errors in
  `Log.txt`); timing of start names measured on Windows NVMe and SATA.

### Phase 3: Store and writer (1.5 weeks)
- `store.py` (sparse writer, replace strategies, index), `TextureService` wired to it, a `prebuild` CLI
  command that builds every texture around an airport to full size.
- Windows file-system check script (§8).
- **Exit (second milestone):** prebuild around one airport, fly there, see sharp ortho; Process Monitor
  confirms A1 and the replace strategy on Windows; if refused, fallback B implemented and selected.

### Phase 4: Planner and scheduler (3 weeks)
- `FlightFeed` datarefs, `rings.py`, `planner.py`, `scheduler.py`, pauses, special rules, live-pressure
  mapping, stand-in X-Plane harness in CI.
- **Exit (first real flights):** cold-cache flights of the §8 set with no FUSE; late textures counted and
  explained; no X-Plane stutters attributable to AutoOrtho above FUSE mode's.

### Phase 5: Plugin and start hold (1.5 weeks)
- C plugin, builds in CI, install and update, gate, start readiness, stale-junction cleanup at X-Plane start.
- **Exit:** starts at dense airports (ZL17/19) are held until built and then load sharp; killing AutoOrtho
  mid-hold releases X-Plane within 5 s; X-Plane started without AutoOrtho shows no AutoOrtho packs.

### Phase 6: Disk budget and eviction (1 week)
- `evict.py`, hole punching per platform, chunk-side budget reuse, clone upgrades on APFS/btrfs/xfs.
- **Exit:** a 2-hour flight stays within budget; nothing evicted is read sharper than kept (stand-in
  harness).

### Phase 7: Parity and polish (2–3 weeks)
- Time exclusion via DSF link swaps, seasons rebuild on key change, moving the aircraft mid-flight
  (`PAUSE` / `RESUME`), files-mode stats, governor advise-only, UI card and selector, diagnostics,
  installer Defender option, Windows Search exclusion, docs for users.
- **Exit:** feature-parity table (§5.10) all handled or explicitly documented.

### Phase 8: Opt-in beta, then default decision
- Release with FUSE as default and files mode as a selectable backend. Collect flight reports (files-mode
  stats in the existing report).
- Decide on the default when: late textures per hour, start time, CPU and stutters are at or better than
  FUSE on the §8 routes across Windows, macOS and Linux reports.

Total: about 13–16 developer-weeks to the opt-in beta.

---

## 11. Open questions

1. Does X-Plane scale rings by texture ground size (`LOAD_CENTER` `size_m`) or by zoom? Phase 0 answers
   it; until then, use the larger ring.
2. Does lowering texture quality drop levels from the file's own top or from 4096 px? Build the sharper
   of the two until Phase 0 shows which.
3. X-Plane on Windows: share flags and mapping lifetime of texture files (Phase 3, Process Monitor).
4. Should files mode support the user's own Ortho4XP tiles outside `z_autoortho/scenery`? Not in v1.
5. Long term: once files mode is the default, can the FUSE-specific parts of `getortho` (wait bands,
   tile-lock timeouts, partial-row serving) be retired, or kept for users who prefer blocking reads?

---

## 12. Licensing

OrthoStudio XP is GPLv3 with additional terms; AutoOrtho is Apache 2.0. Copying its code would require
relicensing AutoOrtho under the GPL. Rules for this work:

- Don't copy or translate code, comments or file structures line by line from OrthoStudio XP.
- Measurements, constants that describe X-Plane's behaviour, and the general technique (pre-built files,
  rename on upgrade, start hold through a plugin) are facts and ideas, and may be reused. Cite the
  OrthoStudio document they come from in our docstrings, as this plan does.
- Developers implementing a component should work from this plan and our own measurements, not with
  OrthoStudio's source open side by side.

---

## 13. Implementation status (2026-11-13)

### What exists

| Plan item | Where | State |
|---|---|---|
| Phase 1 seam | `autoortho/texture_service.py`, `TileCacher.open_tile/close_tile(max_zoom=)` | done; equivalence with `Tile.read_dds_bytes` checked against real imagery (`AO_NETWORK_TESTS=1`) |
| `files_worker` run mode | `worker_modes.py`, `__main__.py`, `files_mode/worker.py`, `process_supervisor.start_mount_worker(run_mode=, worker_args=)` | done; a test proves the worker, session and `getortho` import no FUSE module |
| Pack, junctions, safety rule | `files_mode/pack.py`, `fsops.py`, `fsops_windows.py` | done; tests cover refusing real folders and foreign links, and removing a pack without following its `terrain` junction |
| Catalogue | `files_mode/catalogue.py`, `geo.py` | done (from `.ter` **names**, see below) |
| Window, materializer, neutral pool | `window.py`, `materializer.py`, `neutral.py` | done, with NTFS link-limit rollover |
| Store and writer | `store.py`, `dds_layout.py` | done: sparse files, first build by rename, upgrades in place, stand-in copy-up, coarsen by hole punching |
| Rings, planner, scheduler | `rings.py`, `planner.py`, `scheduler.py`, `flightfeed.py` | done (vectorised; under 1 ms for small packs) |
| Gate and plugin | `gate.py`, `autoortho/xplane_plugin/ao_gate.c` | done; Windows binary built with MSVC (`build_win.bat`); macOS and Linux via `Makefile` (not built here) |
| Disk budget | `evict.py` | done |
| Parent integration | `AOMount.start_files_mode/stop_files_mode`, `[files]` settings, `autoortho.streaming_backend` | done; FUSE mode unchanged |
| Stand-in X-Plane | `tools/files_mode/fake_xplane.py`, `tests/test_files_session.py` | done (synthetic region and flight) |
| Diagnose | `files_mode/diagnose.py` | done |

### Added in the second round

| Plan item | Where | State |
|---|---|---|
| Season rebuild when the season key changes | `session._season_sweep` | every 60 s, built textures near the aircraft whose baked colours are out of date are requeued at low priority (rebuilt in place) |
| SimBrief route input | `files_mode/route.py`, `planner.route_points` | the shared route snapshot (same stats keys as FUSE workers); the route ahead is its own polyline from the aircraft, cut at the lookahead |
| Time exclusion | `files_mode/dsf_swap.py` | per-DSF hard links pointed at the region's DSF (day) or X-Plane's global scenery (night), only for squares X-Plane cannot be holding; off with a message when the files folder is on another disk |
| Moving the aircraft in flight | `session.begin_relocation`, gate `POLL`, plugin flight loop | a jump of more than 20 km with the plugin present: the plugin's 1 s `POLL` is answered `PAUSE` until the new area is built (at most 120 s), then `RESUME`. The plugin only resumes a pause it started, and resumes on its own if AutoOrtho is silent for 10 s |
| Governor | `files_mode/governor.py`, `AOMount.start_files_governor` | see below |
| UI | Essentials "How X-Plane Gets the Textures", Home "Files mode" card (`files_mode/status.py`) | backend selector saved with the other settings; card shows start progress, late textures, queue, build rate, disk use, night swaps, plugin seen |

**Governor in files mode.** FUSE's governor reads X-Plane's reads (near reads real or stand-in,
prefetch misses, live-tile waits); none happen in files mode. `files_mode/governor.py` keeps its
rules (one lever per decision, cooldowns, keep or revert, Auto / Advise / Off, protection first,
pinned settings untouched) on files-mode signals: late textures split into *never queued*
(planned late → lookahead, then ring margin), *queued* (→ builders, or downloads when builders sit
idle), *building* (→ downloads, then builders); frame time, CPU and RAM lower builders. A move is
judged by late textures per minute after 20 s and undone when they got 20 % worse. Levers reach the
worker through `live_settings` (`files.builders`, `files.lookahead_s`, `files.margin`) and the
broker (`autoortho.provider_origin_max_concurrency`). Thresholds are starting values, to be set
from flight reports.

### Added for the beta (third round)

| Item | Where | State |
|---|---|---|
| Phase 0 telemetry | `files_mode/telemetry.py`, hook in `Tile._note_wanted_level`, `[diagnostics] read_telemetry` | FUSE mode writes the first read of each texture level: ground distance and height, size, screen, FOV, texture quality, opens (A1). Local JSON lines, capped per session |
| Ring fitting | `files_mode/ring_fit.py`, `tools/files_mode/fit_rings.py` | fits rings per level (98th percentile, normalised to ZL16 at 2704 px / 65°), picks zoom vs size scaling and ground vs 3-D distance by spread, reports A1, A2, A6; writes `rings.json` |
| Download priority by urgency | `getortho.files_priority`, `files_priority_for`, `LazyChunkGetter.submit` | a thread-local hint set per files-mode build: urgent → live, ≤ 60 s → imminent (50), later → prefetch. Unset in FUSE mode (no change there). Chunks already queued keep their priority |
| Windows Search | `files_mode/windows_integration.py`, `[files] exclude_from_search` (off) | read-only check at every start (verified on Windows 11); Home and the log say when the folder is indexed. The exclusion call (`AddUserScopeRule` + `SaveAll`) is opt-in: it could not be exercised without changing this machine's settings |
| Defender | Essentials button (UAC prompt), installer section `sec_defender` (unchecked) | the installer reads the folder from an existing `~/.autoortho`. The installer was not compiled here (no makensis) |
| CI | `.github/workflows/files-mode.yml` | files-mode tests on Windows, Linux and macOS; plugin builds on all three, uploaded as artifacts |
| Plugin digest guard | `xplane_plugin/stamp.py`, `bin/SOURCE_SHA256`, a test | fails when `ao_gate.c` changed without rebuilding |
| Beta feedback | flight report *Files mode* section (`diagnostics._render_files_mode`), profiler started in files mode, `docs/files-mode.md` | user guide, settings, and what to send |

### Left for real usage data

- Ring table: from beta users' telemetry (`fit_rings.py`), then shipped as the default table.
- Governor thresholds (`MIN_LATE`, `WORSE_FACTOR`, frame thresholds) from flight reports.
- `materialize = link` vs `sparse` on Windows: decided by `files_replace_refused` in reports and a
  Process Monitor capture of X-Plane's file sharing.
- Disk budget default, builder counts, the cruise rule's heights.
- Whether files mode can become the default (Phase 8).

### Unverified until someone runs it

- The plugin inside X-Plane (Windows binary built; macOS and Linux built by CI).
- Windows Search exclusion's write path; the installer's Defender section.
- Files mode on macOS and Linux (code paths written; tests run in CI only).

### Measured on Windows 11 / NTFS while building it

1. **A9 is real, and narrower than feared.** A name another process holds *mapped* cannot be
   replaced (error 32), even with `FileRenameInfoEx` + POSIX semantics. Replacing a *sibling* hard
   link of the same mapped file works, and **in-place writes into a mapped file work**. The store is
   therefore: neutral link → first build by rename (before X-Plane maps it: the start hold and the
   ring of squares ahead give the lead time) → every upgrade in place. In-place upgrades also write
   only the new levels, which cuts the "3× disk writes" risk. A file with more than one link is
   never written in place. A `sparse` strategy (`[files] materialize = sparse`) gives every name its
   own file from the start, for the case real flights show X-Plane keeping mappings.
2. **`os.ftruncate` is not sparse on Windows**: the C runtime extends by writing zeros. Files mode
   sets the end of file with `SetFileInformationByHandle(FileEndOfFileInfo)` after
   `FSCTL_SET_SPARSE`.
3. **`GetCompressedFileSizeW` and `FileStandardInfo.AllocationSize` misreport sparse files**
   (a stale 64 KB and 0). Allocation is summed from `FSCTL_QUERY_ALLOCATED_RANGES`, as `fsutil`
   does. An unbuilt-level texture with its header and ≤256 px levels costs 128 KB.
4. **The catalogue does not need to read `.ter` files.** A texture's centre follows from its name and
   equals `LOAD_CENTER` to 1e-5° (3,000 random Europe textures checked). Europe: 420,709 textures in
   2,187 squares, listed in 1.8 s, cached load in 0.2 s. `LOAD_CENTER`'s size is smaller than the
   name-derived size only for partly covered high-latitude textures, so rings stay conservative.
5. `X-Plane.prf` on X-Plane 12 / Windows has both `_tex_res` and `renopt_tex_res_04`; the higher
   notch is used.

### Departures from the plan (and why)

- **Held X-Plane sends no UDP datarefs** (its main thread is blocked), so the parent's
  `flight_state` goes stale during a start hold. Every `HOLD` carries the position, and the flight
  feed uses it while the snapshot is stale.
- **The plugin needs no SDK**: it looks up the XPLM functions in the already-loaded XPLM library at
  run time. It holds on `LIVERY_LOADED` at the first flight and on message 115 (param 1) for a new
  flight started from within a flight (OrthoStudio's hold test: no aircraft load, so no
  `LIVERY_LOADED`).
- **Dynamic zoom** (`max_zoom_mode = dynamic`) does not change a texture's file size in files mode:
  the file takes the sharpest zoom the steps allow, and the planner builds only the levels each
  distance needs. A file's size is fixed once materialized; a size change rebuilds it.
- **Shutdown** sends `shutdown` on the control channel and waits before the supervisor's terminate
  (TerminateProcess would skip link removal and the index flush); the parent removes leftover
  links itself when X-Plane is not running.

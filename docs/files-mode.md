# Files mode (beta): streaming without WinFsp, macFUSE or FUSE

Files mode is a second way for AutoOrtho to give X-Plane its ortho textures. It is
**experimental**: the default stays the virtual file system (FUSE mode) until files mode has
proved itself in real flights. Your feedback decides that.

## How it differs

| | FUSE mode (default) | Files mode (beta) |
|---|---|---|
| Driver needed | WinFsp, macFUSE or FUSE | none |
| How textures reach X-Plane | built while X-Plane reads them | written to real files *ahead* of X-Plane |
| Flight start | X-Plane waits on each read | a small X-Plane plugin holds the loading screen until the area is built |
| In flight | X-Plane may wait on AutoOrtho | X-Plane never waits; a late texture shows grey or blurry until X-Plane reads it again |
| If AutoOrtho crashes | X-Plane may hang or crash | X-Plane keeps flying over what is already on disk |
| Disk | image cache | image cache plus built textures (20 GB budget by default) |

## Turning it on

1. Run AutoOrtho once in FUSE mode, or at least finish the setup, so your folders are known.
2. **Essentials → How X-Plane Gets the Textures → Files, no virtual file system (experimental)**,
   then Save.
3. Click **Run**. The first start sets up the files folder (links to your installed regions;
   a minute or two for big regions, seconds afterwards).
4. Start X-Plane **after** AutoOrtho is running, as in FUSE mode.

AutoOrtho installs its plugin in `X-Plane 12/Resources/plugins/AutoOrtho` when files mode
starts. It does nothing when AutoOrtho isn't running, apart from removing AutoOrtho's leftover
scenery links before X-Plane scans scenery.

## What to expect

- **At a flight's start** X-Plane's loading screen waits while AutoOrtho builds the area
  (typically 30–90 s on a first visit, much less when you return). Home shows "building N of M".
  The X-Plane log shows lines starting with `AutoOrtho:`.
- **In flight** Home's *Files mode* card shows whether AutoOrtho keeps ahead of X-Plane. "Late
  textures" are the ones that may show grey or blurry for a while.
- **Moving the aircraft far** (map, teleport) pauses X-Plane until the new area is built
  (at most two minutes).
- **Night scenery** (*Use X-Plane's own scenery at night*) works when the files folder is on the
  same disk as your scenery.
- **Second flights in the same area start faster**: built textures stay on disk within the
  disk budget.

## Settings (Settings → Files, Expert)

| Setting | Default | What it does |
|---|---|---|
| `root` | `<scenery folder>/z_autoortho/files` | where built textures live. Must be NTFS, APFS, ext4, btrfs or xfs; best on the same disk as the scenery |
| `disk_gb` | 20 | disk budget for built textures (minimum 10) |
| `camera_reach_km` | 10 | sharp textures built this far beyond what X-Plane needs, for outside views |
| `materialize` | `link` | `sparse` if your feedback shows many "replacements refused" on Windows |
| `exclude_from_search` | False | Windows: keep Windows Search off the files folder (Home tells you when it indexes it) |
| `install_plugin` | True | install and update the X-Plane plugin |

**Auto-Tuning** works in files mode too. It tunes texture builders, how far ahead AutoOrtho
plans, and parallel downloads, from late textures, CPU, memory and X-Plane's frame rate.

**Microsoft Defender** scans every new file. *Essentials → Exclude the files folder from
Microsoft Defender…* asks Windows (with its approval prompt) to stop scanning that one folder.
The installer offers the same as an unchecked option.

## Switching back

Choose the virtual file system in Essentials, Save, and Run. AutoOrtho removes the files-mode
links first. Built textures stay in the files folder; delete it to reclaim the space.

## Sending feedback

Please report every beta flight, good or bad, with:

1. **The flight report**: `~/.autoortho-data/reports/<session>/report.md` (it has a *Files
   mode* section) and `report.json`.
2. **The worker log**: `~/.autoortho-data/logs/worker-files.log` and `autoortho.log`.
3. **X-Plane's `Log.txt`** (lines starting with `AutoOrtho:`).
4. What you saw: grey or blurry areas (where, at what altitude and speed), stutters, how long the
   start took, your screen width and field of view, texture quality.

**FUSE-mode users help too.** In FUSE mode, AutoOrtho records which texture level X-Plane reads
at which distance (`~/.autoortho-data/telemetry/reads-*.jsonl`, local only, never sent). Sending
those files lets us calibrate files mode for everyone (`[diagnostics] read_telemetry`).

## Known limits of the beta

- The distances at which X-Plane reads each texture level come from measurements on macOS;
  Windows calibration comes from your telemetry.
- Auto-Tuning's thresholds are first guesses.
- The X-Plane plugin for macOS must be allowed by Gatekeeper the first time.

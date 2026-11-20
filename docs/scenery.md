# Scenery Setup

## Using provided sceneries

*IMPORTANT*

Use **Scenery Library** to search, filter, install, update, and remove regions.
AutoOrtho checks the destination and temporary-download folders before an
installation begins, shows the expected storage requirement, and blocks the
operation when the destination is not writable or space is clearly
insufficient.

Installations and patch operations remain visible in the **Activity** panel
while you navigate elsewhere. A cancelled operation stops at a safe boundary;
AutoOrtho either preserves resumable download data or cleans incomplete output.
Completed and failed operations remain visible until dismissed, and failures
offer the recovery actions that are safe for that operation.

The scenery packs provided with this tool that you download should be setup via the configuration utility.  This does more than simply download and extract the files.

If you do wish to manually manipulate these scenery packs, you should review the source code (downloader.py in particular) and understand how symlinks and/or junctions work and the limitations of each.

If you manually move things around outside of the configuration utility, things very well might break!

*IMPORTANT*

## Streaming scenery instead of installing regions

You don't have to download whole regions before you fly. With scenery
streaming on, AutoOrtho downloads each 1x1 degree tile the first time
X-Plane needs it, from the same GitHub releases the region installer uses.

Only the tile's base mesh is downloaded: its DSF file and water masks,
typically about 3 MB per tile and up to about 15 MB for detailed ones. The terrain files are generated on the fly, and satellite
imagery is fetched as usual.

### Turning it on

1. Stop AutoOrtho if it's running. Streaming settings are locked while
   AutoOrtho or X-Plane is running.
2. Open **Scenery Library**. The **Stream scenery** panel is at the top.
3. Pick the **Scenery set**:
   - **XP11 legacy set (kubilus1/autoortho-scenery):** the same meshes as
     the regions offered in the Scenery Library.
   - **XP12 set:** coming soon. It's marked **Soon** until its packages are
     ready for streaming.
4. Choose whether to **Include overlays** (roads, forests, buildings). See
   [Overlays and simHeaven](#overlays-and-simheaven).
5. Set the **Disk limit** (20 GB by default).
6. Tick **Stream scenery you haven't installed**. AutoOrtho downloads the
   tile index, a few megabytes, and sets up the streaming folders.
7. Start AutoOrtho, then X-Plane.

You can change the scenery set, overlays and disk limit at any time while
AutoOrtho is stopped. Changes are applied right away, whether streaming is
on or off, and take effect the next time AutoOrtho starts. AutoOrtho asks
first when a change would delete tiles you've already streamed.

### Installed regions always come first

Streaming only covers tiles you haven't installed. If a region is installed,
X-Plane uses its tiles and streaming fills in the rest of the world. That
means you can:

- **Stream everything** and install nothing.
- **Install the regions you fly most** and stream the rest. Installed tiles
  load without waiting for a download, and you can fly them offline.

To stream a region you have installed, uninstall it from the Scenery
Library.

### Overlays and simHeaven

**If you use simHeaven X-World or another overlay package, turn
Include overlays off.** simHeaven already provides roads, forests and
buildings, so streaming AutoOrtho's overlays as well would duplicate them.

With overlays on, AutoOrtho streams the overlays that come with the scenery
set. If you still have the classic `Custom Scenery/yAutoOrtho_Overlays`
folder, its tiles are used instead of streamed ones, even when it's disabled
in `scenery_packs.ini`. To stream overlays instead, move that folder out of
`Custom Scenery`.

### Disk use

Streamed tiles stay on disk, so each tile is downloaded only once. When the
stored tiles grow past the disk limit, AutoOrtho removes the tiles you
haven't used for the longest time, down to 90% of the limit.

- Tiles used in the last two hours are never removed.
- Overlays get a quarter of the limit on top of it. With a 20 GB limit,
  overlays can use another 5 GB.
- The panel shows how many tiles are stored and how much space they use.
  The disk limit covers only the base mesh. Satellite imagery has its own
  cache limit in **Settings**.

### When you fly

- When X-Plane opens a tile, AutoOrtho downloads it and the tiles around
  it. The first flight in a new area takes longer to load. Later flights
  there load at normal speed.
- If a tile can't be downloaded (no connection, or GitHub is unavailable),
  X-Plane gets the tile from its default Global Scenery instead, so you see
  default terrain there rather than an error. A missing overlay tile is
  served empty.
- If GitHub throttles downloads, AutoOrtho pauses downloads (from one minute
  up to 15 minutes) and then tries again. Tiles needed during the pause
  come from Global Scenery.
- Downloaded tile indexes are checked for updates once a week.

### Where the files are

Streaming creates these folders next to your installed regions in
`Custom Scenery/z_autoortho/scenery/`:

- `z_ao_stream_xp11`: the streamed base mesh.
- `yAO_stream_xp11_overlays`: the streamed overlays, if you included them.

Like installed regions, each one appears in `Custom Scenery` while AutoOrtho
is running. Don't edit or move these folders yourself; use the Stream
scenery panel.

When AutoOrtho starts, it adds the streaming folders to `scenery_packs.ini`
and keeps the streamed overlays above the AutoOrtho base meshes. The first
time it changes the file, it keeps your original as
`scenery_packs.ini.ao_backup`.

### Turning it off

Untick **Stream scenery you haven't installed**. AutoOrtho asks before
deleting the tiles streamed so far. Installed regions aren't affected.

### Limitations

- **Scenery Patches** (Seasons, Terrain reflectivity) are applied only to
  installed regions, not to streamed tiles yet.
- You can't keep a streamed area for offline flying. To fly an area
  offline, install its region.
- The setup wizard doesn't offer streaming yet. Turn it on in the Scenery
  Library after setup.

## Configuring scenery_packs.ini

Any scenery added to XPlane requires being setup in the scenery_packs.ini file
located in your installs 'Custom Scenery' directory.

This file is order dependent where higher priority layers are towards the top
of the file and lower priority items are towards the bottom.

On initial run of XPlane after installing new scenery, these layers will
automatically be added to your scenery_packs.ini file, but likely in an
incorrect location.

This can result in strange issues, such as having no roads, buildings, or
trees when flying with XPlane.  If this is the case, double check the order of
files in scenery_packs.ini.

This tool will download and setup numerous directories in your 'Custom
Scenery' folder of the form:  `z_ao_<short name of scenery pack>` and a
directory named `yAutoOrtho_Overlays`.  Typically these should be listed
towards the bottom of your scenery_packs.ini file:

```
Landmarks, airports, etc
simHeaven, special layers, etc
yAutoOrtho_Overlays
yAO_stream_xp11_overlays
z_ao_aus_pac
z_ao_eur
z_ao_na
z_ao_stream_xp11
z_autoortho
zzz_global_scenery
```

`yAO_stream_xp11_overlays` and `z_ao_stream_xp11` appear only if
[scenery streaming](#streaming-scenery-instead-of-installing-regions) is on.
AutoOrtho places them for you when it starts.

## Installing in custom directories

It is possible to install scenery into directories other than X-Plane's Custom
Scenery directory.  

You should have a good understanding of how X-Plane handles custom scenery
before attempting this!

1. This will require that you configure AutoOrtho to point the 'Custom Scenery' setting to this alternative directory.
2. Setup and install scenery as per normal.  Run the program at least once to
   create the mount points.  You should see folder that correspond to the
   installed packages now in the alternative directory that was setup.  The
   format will look like `z_ao_XXXX`
3. You will then need to manually link each of the resulting mount directories to X-Planes's origin Custom Scenery dir.
   On Linux you can use a symlink.  On Windows you can create a shortcut.
4. Make sure Autoortho is running and start X-Plane.  Double check your
   `scenery_packs.ini` file to assure the directories are seen.


# Scenery Setup

## AutoOrtho scenery

AutoOrtho's base mesh comes from one scenery set, stored per 1x1 degree tile.
You get tiles in two ways, and can use both:

- **Download them on the map**, one by one, by box or a whole region at a
  time. Downloaded tiles are kept: they work offline and are never removed
  to make room. This replaces installing regions.
- **Let them download while you fly.** Each tile downloads the first time
  X-Plane needs it.

Only the base mesh is downloaded for a tile: its DSF file and water masks.
The terrain files are generated on the fly. Satellite imagery is fetched as
you fly, or can be [downloaded ahead](#downloading-imagery-ahead) with the
tiles. How big a tile's mesh is depends on the scenery set and the area:

- **XP12 set:** about 12 MB per tile on average, from about 4 MB in Africa
  to about 27 MB in western Europe. Detailed tiles with a lot of coastline or
  lakes, such as in the Alps, can reach about 55 MB.
- **XP11 legacy set:** typically about 3 MB per tile and up to about 15 MB
  for detailed ones.

The scenery comes from releases on GitHub, which have no download limit.

### Setting it up

On a new installation, the setup wizard's **Scenery** step has
**Use AutoOrtho scenery** ticked: pick the settings below and finish. See
[First-run setup](config.md#first-run-setup).

To set it up or change it later:

1. Stop AutoOrtho if it's running. The settings are locked while AutoOrtho
   or X-Plane is running.
2. Open **Scenery Library**. The **AutoOrtho scenery** panel is at the top.
3. Pick the **Scenery set**:
   - **XP12 set (flightwusel v1.4, by jonaseberle):** the default. A base
     mesh made for X-Plane 12, covering the whole world (all 12 areas of the
     flightwusel set). It already includes X-Plane 12's seasons. It has no
     overlays, so use simHeaven X-World for roads, forests and buildings.
   - **XP11 legacy set (kubilus1/autoortho-scenery):** the meshes the
     region installer used to install, with overlays.
4. Choose whether to **Download tiles while flying**. See
   [Using only downloaded tiles](#using-only-downloaded-tiles).
5. Choose whether to **Include overlays** (roads, forests, buildings). See
   [Overlays and simHeaven](#overlays-and-simheaven). The option is off for
   the XP12 set, which has none.
6. Set the **Disk limit** for tiles downloaded while flying (20 GB by
   default). Optionally tick **Set terrain reflectivity** and pick a value
   ([Terrain reflectivity](#terrain-reflectivity)), and for the XP11 set,
   **Add seasons to downloaded tiles** ([Seasons](#seasons-for-the-xp11-set)).
7. Tick **Use AutoOrtho scenery**. AutoOrtho downloads the tile index (about
   30 MB for the XP12 set, 6 MB for the XP11 set) and sets up the folders.
8. Click **Download Tiles on the Map…** to download the areas you fly, or
   just start AutoOrtho, then X-Plane.

You can change these settings at any time while AutoOrtho is stopped.
Changes are applied right away and take effect the next time AutoOrtho
starts. AutoOrtho asks first when a change would delete stored tiles.

### Downloading tiles and regions on the map

**Flight Plan & Map** has a map with two views; switch with the buttons
above it. The **Scenery & Cache** view shows what's stored per tile and is
where you download scenery.

1. Select tiles: click one or drag a box, with Ctrl or Shift to add to the
   selection. To take a whole area, pick it in **Select a region…** (for the
   XP12 set: Africa North, Europe West, North America South and so on),
   which selects every tile of it and zooms the map there. Right-drag pans
   the map and the wheel zooms; **Clear** or Esc clears the selection.
2. Click **Download N tiles…**. AutoOrtho shows how much scenery it will
   download and lets you add imagery (next section).
3. Progress shows in the **Activity** panel, where you can cancel it.
   Downloading runs four tiles at a time; a tile takes about a second on a
   fast connection. If GitHub throttles the download, it stops; run it again
   later and it continues where it stopped.

Downloaded tiles are never removed to make room and don't count toward the
disk limit. To stop keeping tiles, select them and click **Stop Keeping**
(shown when the selection has kept tiles); their files stay on disk until
the disk limit needs the space. Tiles with no scenery, such as open water, are skipped.

Downloading and deleting only work while AutoOrtho and X-Plane are stopped.

### Downloading imagery ahead

The Download dialog's **Imagery** choice downloads the satellite imagery of
the selected tiles too, up to the zoom you pick, with the size for each
zoom. AutoOrtho downloads exactly the imagery the tiles' textures use, from
the source it would use when flying (your map type, or the Custom Map's
choice per tile). Imagery already in the cache isn't downloaded again.

- Downloaded imagery is kept: the imagery cache limit in **Settings**
  doesn't remove it, and the texture files AutoOrtho builds from it aren't
  removed either. Deleting a tile's imagery or **Stop Keeping** ends that.
- Pick the zoom you fly with (Settings, Max zoom). Each zoom level is four
  times larger than the one below: a 1x1 degree tile at 45°N is about
  0.9 GB at ZL16 with Bing.
- Apple Maps imagery can't be downloaded ahead.

### When you fly

With **Download tiles while flying** on:

- When X-Plane opens a tile you haven't downloaded, AutoOrtho downloads it
  and the tiles around it. The first flight in a new area takes longer to
  load. Later flights there load at normal speed.
- If a tile can't be downloaded (no connection, or GitHub is unavailable),
  X-Plane gets the tile from its default Global Scenery instead, so you see
  default terrain there rather than an error. A missing overlay tile is
  served empty, and a missing water mask is served blank, which shows
  X-Plane's default water along that stretch of coast until the scenery
  reloads.
- Dropped connections are retried straight away, so a hiccup costs a
  second or two rather than a fallback.
- If GitHub throttles downloads, AutoOrtho pauses downloads (from one minute
  up to 15 minutes) and then tries again. Tiles needed during the pause
  come from Global Scenery.
- Downloaded tile indexes are checked for updates once a week.

Tiles downloaded while flying stay on disk, so each is downloaded only once.
When they grow past the disk limit, AutoOrtho removes the ones you haven't
used for the longest time, down to 90% of the limit. Tiles used in the last
two hours, and tiles you downloaded on the map, are never removed. Overlays
get a quarter of the limit on top of it. The panel shows how many tiles are
stored and how much space they use.

### Using only downloaded tiles

Untick **Download tiles while flying** to use only the tiles you downloaded
on the map, as installed regions used to work. X-Plane then shows its
default scenery everywhere else, and nothing is downloaded while you fly.
If a downloaded tile is missing a water mask, AutoOrtho still fetches it,
since X-Plane stops with an error on a missing mask.

### Regions installed with earlier versions

Regions installed with the old region installer keep working, and their
tiles are used before AutoOrtho scenery where both have one. The Scenery
Library lists them under **Installed regions**, each with its Seasons and
terrain reflectivity status, **Scenery Options** and **Remove**.

**Move into AutoOrtho Scenery** moves them into the AutoOrtho scenery, so
the map shows and manages them like downloaded tiles. Their files are
moved, not downloaded again, and the region folders are removed afterwards.
Seasons and terrain reflectivity they had are kept.

All installed regions are from the XP11 legacy set, so they can move only
while you use the XP11 set; each region says why if it can't. To use the
XP12 set where you have an XP11 region installed, remove the region.

### Overlays and simHeaven

**If you use simHeaven X-World or another overlay package, turn
Include overlays off.** simHeaven already provides roads, forests and
buildings, so AutoOrtho's overlays as well would duplicate them.

With overlays on, AutoOrtho downloads the overlays that come with the
scenery set. If you still have the classic `Custom Scenery/yAutoOrtho_Overlays`
folder, its tiles are used instead, even when it's disabled in
`scenery_packs.ini`. **Move into AutoOrtho Scenery** also moves those
overlay tiles when you use the XP11 set with overlays.

### Terrain reflectivity

Terrain reflectivity is X-Plane's `SUPER_ROUGHNESS` setting. Higher values
make terrain less shiny at sunrise and sunset; 1.0 is matte. Untick
**Set terrain reflectivity** to use X-Plane's default.

The value applies to every tile, including tiles already stored, because
their terrain files are generated as X-Plane reads them. It takes effect the
next time AutoOrtho starts. Installed regions keep their own value, set in
**Scenery Patches**.

### Seasons for the XP11 set

The XP12 set already includes X-Plane 12's seasons. For the XP11 set, tick
**Add seasons to downloaded tiles**: each tile you download on the map then
gets X-Plane 12's season data from Global Scenery, a few seconds per tile.
Turning it on offers to add seasons to the tiles you already downloaded.
Tiles downloaded while flying stay as they are.

The Seasons patch on X-Plane 12.1 and later only added an elevation
reference and no seasons. That's fixed; a region patched that way can be
patched again (Scenery Options > Restore defaults, then apply Seasons), and
its tiles get seasons when moved into AutoOrtho scenery with the option on.

### Where the files are

AutoOrtho scenery lives in `Custom Scenery/z_autoortho/scenery/`:

- `z_ao_stream_xp12` or `z_ao_stream_xp11`: the base mesh of the set you
  picked.
- `yAO_stream_xp11_overlays`: the overlays, if you included them (XP11
  legacy set only).

Like installed regions, each one appears in `Custom Scenery` while AutoOrtho
is running. Don't edit or move these folders yourself; use the Scenery
Library and the map.

When AutoOrtho starts, it adds these folders to `scenery_packs.ini` and keeps
the overlays above the AutoOrtho base meshes. The first time it changes the
file, it keeps your original as `scenery_packs.ini.ao_backup`.

### Seeing and managing stored tiles on the map

In the **Scenery & Cache** view, tiles are coloured by what's stored:
**Scenery** (base mesh, overlays and water masks), **Imagery** (cached
satellite imagery), or **Both**. Choose **Colour by: Size** to colour them by
how much space they use instead. A white outline marks downloaded (kept)
tiles. Hover over a tile to see its sizes, imagery zoom levels and sources,
kept imagery, and when it was last used. The line under the map shows the
totals. **Show Stored** zooms the map to the tiles that are stored.

To delete, select tiles and click **Delete N tiles…**. Tick **Scenery**,
**Imagery** or both; each shows how much it would free, and nothing is
deleted until you click **Delete**.

- Deleted scenery is downloaded again the next time you fly there if
  downloading while flying is on. Deleting it also stops keeping the tile.
- Deleted imagery is downloaded again when X-Plane needs it.
- Installed regions aren't affected.

### Turning it off

Untick **Use AutoOrtho scenery**. AutoOrtho asks before deleting the stored
tiles. Installed regions aren't affected.

### Limitations

- Apple Maps imagery can't be downloaded ahead.
- Seasons are added only to XP11 tiles downloaded on the map, not to tiles
  downloaded while flying.

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
z_ao_aus_pac
z_ao_eur
z_ao_na
z_ao_stream_xp12
z_autoortho
zzz_global_scenery
```

The `z_ao_stream_...` and `yAO_stream_..._overlays` folders appear only if
[scenery streaming](#streaming-scenery-instead-of-installing-regions) is on;
the overlays folder only with the XP11 legacy set.
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


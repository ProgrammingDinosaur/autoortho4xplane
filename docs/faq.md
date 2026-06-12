# FAQ and Troubleshooting

## Application File Structure

### Important: Do NOT delete the `ao_files` folder!

When you extract AutoOrtho (Windows zip or macOS app), you'll see a folder structure like this:

**Windows (zip extraction):**
```
autoortho_release/
├── autoortho.exe          ← Main executable
├── ao_files/              ← REQUIRED - Contains Python runtime and dependencies
│   ├── (many files...)
│   └── ...
└── ...
```

**macOS (app bundle):**
```
AutoOrtho.app/
└── Contents/
    ├── MacOS/
    │   ├── autoortho      ← Main executable
    │   └── ao_files/      ← REQUIRED - Contains Python runtime and dependencies
    └── ...
```

The `ao_files` folder contains the bundled Python interpreter and all required libraries. **If you delete this folder, AutoOrtho will not work!**

Common mistakes:
- Moving only the `.exe` file to another location (without `ao_files`)
- "Cleaning up" by deleting folders that look like temporary files
- Anti-virus software quarantining files from this folder

If AutoOrtho suddenly stops working after it previously worked, check that the `ao_files` folder is intact and in the same directory as the executable.

---

## General Issues

<a name="missing-color-tiles"></a>
### I see occasional blurry and/or green (missing color) tiles

Missing color tiles (typically green) occur when AutoOrtho cannot retrieve imagery within the time budget. This section explains why this happens and how to minimize it.

#### Why do missing color tiles happen?

1. **Time Budget Exhaustion:** Each tile request has a time limit. If chunks don't download in time, they may appear as missing color.
2. **Network Issues:** Slow or unreliable internet connections cause downloads to timeout.
3. **Server Issues:** The imagery servers may be slow or temporarily unavailable.
4. **CPU Limitations:** If your CPU can't decode images fast enough, chunks may not complete in time.

#### How to minimize missing color tiles

**Option 1 - Lower Zoom Level (Most Effective):**
Each zoom level requires 4× more resources. Lowering your max zoom dramatically reduces missing tiles:
1. Open AutoOrtho Settings
2. Lower "Max Zoom Level" from ZL17/18 to ZL16 or ZL15
3. This reduces chunks per tile from 1024/4096 down to 256/64

**Option 2 - Increase Time Budget:**
1. Open AutoOrtho Settings
2. Go to the Settings tab → Performance Tuning section
3. Increase "Tile Time Budget" to 15-20 seconds
4. This gives more time for chunks to download

**Option 3 - Enable Full Fallbacks:**
1. Set "Fallback Level" to "Full (Best Quality)"
2. Enable "Allow fallbacks to extend time budget"
3. This allows AutoOrtho to download lower-detail alternatives when high-detail fails

**Recommended settings for minimal missing tiles:**
```ini
maptype_override_zoom = 16
use_time_budget = True
tile_time_budget = 300.0
fallback_level = full
fallback_extends_budget = True
```

**Trade-off Warning:** Higher time budgets and extended fallbacks may cause longer loading times and occasional stuttering. See the [Performance Tuning Guide](performance.md) for a detailed explanation of the quality vs. speed trade-off.

---

### Why are there so many missing tiles even with "Full" fallback enabled?

If you're seeing many missing tiles with `fallback_level = full`, check these:

1. **Budget still limiting fallbacks:** By default, even with "full" fallbacks, network fallbacks respect the time budget. Enable "Allow fallbacks to extend time budget" (`fallback_extends_budget = True`) to let fallbacks continue after the budget is exhausted.

2. **New area without cache:** The first time you fly over an area, there's no cached data for fallbacks to use. Fly the same route again and you should see fewer missing tiles.

3. **All fallbacks failing:** In rare cases (server issues, network problems), all fallback levels may fail. Check the AutoOrtho logs for error messages.

---

### How do I get the smoothest flying experience with minimal stutters?

Stuttering occurs when X-Plane waits for AutoOrtho to provide imagery. To minimize stutters:

1. **Use Time Budget System:** Keep `use_time_budget = True` (default)
2. **Use reasonable budget:** Set `tile_time_budget` to 180-300 seconds
3. **Use cache-only fallbacks:** Set `fallback_level = cache` to avoid network delays during fallbacks
4. **Enable prefetching:** Keep `prefetch_enabled = True` to download tiles ahead of your aircraft
5. **Increase prefetch lookahead:** Set `prefetch_lookahead = 30` or higher for faster aircraft

**Recommended settings for stutter-free flying:**
```ini
use_time_budget = True
tile_time_budget = 180
fallback_level = cache
fallback_extends_budget = False
prefetch_enabled = True
prefetch_lookahead = 30
max_zoom_level = 16
```

**Trade-off:** You may see occasional blurry or missing tiles, but your flight will be smoother.

---

### Why does loading take so long at startup?

At startup, X-Plane requests the initial scenery tiles. Loading time depends on:

1. **Zoom level (biggest factor):** Each zoom level increase requires **4× more resources**
2. **Number of tiles:** More tiles = more total work
3. **Network speed:** Slower connections = longer downloads
4. **CPU speed:** Slower CPUs = longer decode/compress times
5. **Time budget:** Higher budgets = more time spent per tile

**To speed up startup:**
- **Lower your Max Zoom Level** - This has the biggest impact! ZL16→ZL15 is 4× faster
- Use a lower `tile_time_budget` (180-300 seconds)
- Enable `suspend_maxwait = True` to use extended timeouts only during startup
- The second time you load the same area will be faster (cached data)

**Zoom Level Resource Scaling:**
| Zoom | Chunks/Tile | Relative Load Time |
|------|-------------|-------------------|
| ZL15 | 64 | 1× (baseline) |
| ZL16 | 256 | 4× |
| ZL17 | 1024 | 16× |
| ZL18 | 4096 | 64× |

See the [Performance Tuning Guide](performance.md#zoom-level-critical-performance-factor) for detailed zoom level recommendations.

---

### What do the Performance Tuning settings mean?

| Setting | What it controls |
|---------|-----------------|
| **Tile Time Budget** | Total seconds to wait for a tile before returning partial results |
| **Fallback Level** | How aggressively to find replacement imagery for failed chunks |
| **Fallback Extends Budget** | Whether to continue network fallbacks after budget is exhausted |
| **Prefetch Enabled** | Whether to download tiles ahead of your aircraft |
| **Prefetch Lookahead** | How many seconds of flight time to prefetch ahead |

For detailed explanations, see the [Performance Tuning Guide](performance.md).

---

### What is the Native Pipeline and do I need it?

The **native pipeline** (`aopipeline`) is an optional high-performance component that dramatically speeds up DDS texture building (10-20x faster). It's written in C and uses true multi-threading via OpenMP.

**Do you need it?** 
- If it's available for your platform (included in releases), it's used automatically
- If not available, AutoOrtho falls back to the Python implementation
- All features work either way; native is just faster

**Benefit:** Significantly fewer stutters, especially during initial scenery load or when flying fast/low.

See the [Native Pipeline Architecture](performance.md#native-pipeline-architecture) for technical details.

---

### Why are Apple Maps downloads slower than other sources?

**Apple Maps always uses the Python HTTP client**, not the native libcurl client. This is intentional because Apple Maps requires:

1. **Dynamic authentication**: Tokens must be fetched via DuckDuckGo proxy
2. **Token rotation**: On 403/410 errors, the token must be refreshed
3. **Special headers**: Requires `Authorization: Bearer` headers

The Python path handles all this authentication flow correctly. Other imagery sources (BI, EOX, ARC, NAIP, USGS, FIREFLY, YNDX, GO2) use the faster native HTTP client.

**Impact:** Apple Maps initial loading may be 2-3x slower than other sources. Once cached, performance is identical.

**Workaround:** Consider using an alternative imagery source if download speed is critical.

---

### I changed the settings but nothing seems different

1. **Restart AutoOrtho:** Some settings require a restart to take effect
2. **Clear cache:** Old cached data may affect results. Try clearing the cache
3. **Fly a new route:** Cached areas will use existing data. Try an area you haven't visited
4. **Check config file:** Verify your `~/.autoortho` file has the expected values

---

### What's the difference between maxwait and tile_time_budget?

| Setting | Scope | Behavior |
|---------|-------|----------|
| `maxwait` (legacy) | Per-chunk timeout | Each of 256 chunks waits up to this long |
| `tile_time_budget` (new) | Whole-tile budget | Total wall-clock time for entire tile request |

**Example:** With `maxwait = 1.5s` and 256 chunks:
- Worst case: 256 × 1.5s = 384 seconds (due to serial execution)
- Actual time varies widely based on parallelism

**Example:** With `tile_time_budget = 180s`:
- Always release a tile to X-Plane within 180 seconds (predictable)
- May have some missing chunks if network is slow or zoom level is high

**Recommendation:** Keep `use_time_budget = True` to use the new predictable system.

### Time exclusion re-activated after scenery reload

**Symptom:** You have "Default to Exclusion" enabled, the exclusion correctly deactivated when sim time showed daytime, but after triggering "Reload Scenery" the exclusion incorrectly re-activated and you see global scenery instead of orthos.

**Solution:** This issue has been fixed. AutoOrtho now preserves the exclusion decision made based on actual sim time during temporary disconnections (like scenery reload).

If you're still experiencing this issue:
1. Make sure you're running the latest version of AutoOrtho
2. If the state seems "stuck", restart AutoOrtho to reset to the default behavior

**Technical details:** When X-Plane reloads scenery, the UDP connection temporarily disconnects, making sim time unavailable. AutoOrtho now remembers the last decision made with real sim time and uses it during these brief disconnections, rather than falling back to the `default_to_exclusion` setting.

See [Time Exclusion - Decision Preservation](performance.md#decision-preservation-during-scenery-reload) for more details.

### I see a messge in the logs, but otherwise things work fine.
The log will log various things, typically info and warnings can be ignored
unless other issues are seen.

### In XPlane I get an error like `Failed to find resource '../textures/22304_34976_BI16.dds', referenced from file 'Custom Scenery/z_eur_11/terrain/'.`

What's happening is that X-Plane has found a terrain file, but is not finding a linked texture.  This could be caused by a few issues:

  * AutoOrtho isn't running
  * You may have broken the links from your texture directories to the AutoOrtho mount location. Perhaps you manually moved around directories after downloading these from the configuration utility.
  * The directory AutoOrtho is configured to run from is now different from the directory links the scenery packs point to.
  * On Windows 11 try uninstalling Dokan (and reboot) and install WinFSP
    instead. Or vice versa.

First verify that AutoOrtho is running and there are no obvious errors shown in a log.  If it is running then verify that all the directory links are correct, and consider simply cleaning up and reinstalling scenery from scratch, keeping a consistent 'Custom Scenery' directory configured.

If in doubt, re-install the scenery packs.

### Something went wrong with scenery setup and I'd like to start again.  How do I reinstall?
AutoOrtho checks for metadata for installed scenery packs in `Custom Scenery/z_autoorth/**_info.json`  Where '**' is a shortname for each scenery pack installed.  You can delete the corresponding .json file, and re-run the configuration utility and should be able to reinstall the scenery pack

### I installed scenery, setup Custom Scenery, and clicked 'Run' but X-Plane did not automatically startup
You have to start X-Plane separately from this tool.  It's also best to start X-Plane _after_ starting autoortho so that all new files and directories are picked up.

---

## Linux Issues

### IOError: [Errno 24] Too Many Open Files
On some Linux distributions they set the minimum open files limits. This is 1024. Since AutoOrtho is modifying and creating a large number of files at the same time, it can easily hit this limit.

*To confirm*
Run the following command and see if the result is in the 4,096 at a minimum.
'ulimit -n'

While doing this you can also check the hard limit thats system wide. This should be in the 100,000s or millions in most cases.
'ulimit -Hn'

*Fixing by setting a higher limit temporarily*
Open a new terminal window.

This command will set a filesystem max file limit just in case we are accidentally hitting  that limit too...
'sudo sysctl -w fs.inotify.max_user_watches=100000'

Now we will set our user/process limit to something more workable with generating ortho dynamically...
'ulimit -S -n 8192'

When you check your 'ulimit -n' you should now see it at 8k (8192). Now, in the same terminal window, we can launch our autoortho binary:
'./autoortho_lin_1.4.2.bin'

Note: when you close your terminal window that you entered these commands into (or start another) it will no longer have this raised limit. This is a python safeguard, not a flaw. It helps protect you against scenarios where touching a large number of files can indicate issues (e.g. resource exhaustion, ransomware attacks, bad code, etc).

### On Linux this does not start/gives a FUSE error
Make sure that your `/etc/fuse.conf` files is set to `user_allow_other`.  You may need to uncomment a line.

### WARNING - FallbackResolver: Could not import AoImage for disk cache fallback
This can happen when the prebuilt AoImage shared library provided in the installation package is incompatible with your Linux distribution.
The most likely incompatibility is the C runtime library and the easiest fix is to install autoortho from source and rebuild the AoImage library on your system.

You'll need to install basic tools like a C compiler and `make`.
On Debian-based systems, this may be something like:
```bash
sudo apt install build-essential make
```
On Arch/CachyOS:
```bash
sudo pacman -S --needed base-devel
```
Then:
```bash
git clone https://github.com/ProgrammingDinosaur/autoortho4xplane.git
cd autoortho4xplane
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make aopipeline
```

Quick sanity check:
```bash
python -c "from autoortho.aoimage import AoImage; print('OK')"
```
Suggested start script:
```bash
#!/usr/bin/env bash
ulimit -n 524288
cd your_autoortho4xplane_directory
source .venv/bin/activate
PYTHONPATH=/your_autoortho4xplane_directory/autoortho python -m autoortho &
```

Thanks to "clumsynick" for this fix!

### The program crashed and now I get an error when AutoOrtho attempts to mount
You can clear mounts manually with the command `sudo umount -f AutoOrtho`.
You may need to run this for each remaining mount.  The `mount` command will
list your current mount points.

---

## Windows Issues

### When using Windows I see an error using the run.bat file such as ` note: This error originates from a subprocess, and is likely not a problem with pip. error: legacy-install-failure`

This is likely due to having a very new version of Python and a package dependency that does not have a pre-built 'wheel' binary.  Further in that error you will see a link to visual studio build tools that can be installed.  You could also try downgrading your version of Python.  For instance try uninstalling Python 3.11 and install Python 3.10.

### Downloading and extracting files takes a very long time on Windows

This may be due to Windows Defender real time scanning be enabled.  You can
temporarily disable this, which should make a difference, but it will be
re-enabled automatically.

You can exclude directories, such as `C:\Users\.autoortho-data' or wherever
else you installed your download and cache directories.

### On Windows the executable/zip is detected as a malware/virus by Windows Defender
That is a false positive.  Unfortunately, Windows is very dev and opensource unfriendly.  You can choose to ignore this false positive or not, it's your computer.  Alternatively, you can run this directly via source.

### I get an error when running with scenery installed on a non-local drive or non-NTFS formatted drive when using Windows
This is not supported. Use a local NTFS formatted drive.

---

## MacOS issues

### Flight crashes during loading, X-Plane logs show no detailed errors.
AutoOrtho Tile Setup and imagery download can take up lots of time while loading the flight and X-Plane freezes in the meantime. MacOS doesn't like the app being frozen and if you click on it or change apps often the system might force close it. Crashing the Simulator. Disabling multithreaded FUSE helps in these cases.
Solution: 
1. Set FUSE Multithreading to disabled in the settings.
2. Retry loading and stay on the loading screen (be patient)

## I have an issue that is not here.

Please check the [issues page of the repository](https://github.com/ProgrammingDinosaur/autoortho4xplane/issues). 
*Before submitting an issue please search for already existing issues for yopur problem, as it may be duplicated.*
If your issue has not been reported please open a new one and include as much information as possible, for example:
- Screenshots
- Logs
- OS 
- Version of AO and X-Plane you are using

With more information the debugging process will be easier.

After the issue is opened I'll gladly check it out as soon as my time allows. Keep in mind that this is a side project for me, so support might not be immediate. 


## Base Mesh Packages Problems

### I have an issue with some part of the mesh offered in the scenery packages
Currently this fork still uses kubilus1 base mesh, as such support is not offered for it right now, this will change with the new custom mesh. For now refer to the [issues page of the kubilus scenery](https://github.com/kubilus1/autoortho-scenery/issues) to see if your issue has been reported and hopefully a community fix has been shared.

## About this fork

### What is the objective of this fork?

As the AutoOrtho Continued name suggests the main idea of this fork is to continue building upon kubilus1's codebase and to add more features while keeping the program updated with latest technologies, and offer active support to issues that may arise.

### What has been changed?

This fork currently adds a revamped new UI, more autoortho options such as overriding max zoom, and quality of life features such as new imagery servers and download retries.
For more detailed information of the changes of every release please refer to the [releases](https://github.com/ProgrammingDinosaur/autoortho4xplane/releases)  page as detailed changelongs of every release are detailed there.

### What's next?

While the scope of this fork may change over time and soem features might never see the light of day the main things in development right now are:
- MacOs Support 
- Completely rebuild the Base Mesh packages with newer Otho4XP versions to have newest mesh info and take advantage of new XP12 technologies such as 3D water.

Things in backlog that are planned be implemented later on (order does not matter):
- Predictive Tile caching with Simbrief integration: Have the program monitor your flight or via simbrief flightplan infer where you are heading to and start caching imagery from further tiles to speed up loading times and decrease stutters when you reach a new area.
- Tile caching monitoring: See what tiles have been loaded and cached by the software.
- Merge [hotbso changes](https://github.com/hotbso/autoortho) to this fork to support seasons with AutoOrtho

### When will those features be released?

There is no ETA. And there is also no guarantee of this changes being released at all.
Keep in mind this is a side project that is mostly fueled by my free time, autism and hyperfixation. So development times are not guaranteed and are subject to change. 

### Can I contribute to help?

Sure! This is a community open-source project after all, and the more contributions the better.
You can create your own fork and open a PR into this repo with code you would want implemented! I will review it and hopefully merge it to keep this project growing.

### Want some idea that you want implemented but can't code it?

You can still contribute!
Open a discussion with your feature suggestion and maybe someone or me will look at it and implement it if we find it useful or interesting!

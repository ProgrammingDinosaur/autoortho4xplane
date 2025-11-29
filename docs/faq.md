# FAQ and Troubleshooting

## General Issues

### I see occasional blurry and/or green tiles
There is a timeout for how long the system waits for individual satellite
images.  You can adjust how long the system waits for high resolution
images by adjusting the 'max_wait' setting (in seconds) in your configuration
file.  Lower resolution tiles are used when available as a fall back.  The
green tile is used as a last resort.

By making this too high you risk introducing lag, stuttering, and delays.
However this may need to be increased for users that are far from source
servers or have slow internet connections.

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
On some Linux distrubtions they set the minimum open files limits. This is 1024. Since AutoOrtho is modifying and creating a large number of files at the same time, it can easily hit this limit.

*To confirm*
Run the following command and see if the result is in the 4,096 at a minumum.
'ulimit -n'

While doing this you can also check the hard limit thats system wide. This should be in the 100,000s or millions in most cases.
'ulimit -Hn'

*Fixing by setting a higher limit temporarely*
Open a new terminal window.

This command will set a filesystem max file lmit just in case we are accidently hitting into that limit too...
'sudo sysctl -w fs.inotify.max_user_watches=100000'

Now we will set our user/process limit to something more workable with generating ortho dynamically...
'ulimit -S -n 8192'

When you check your 'ulimit -n' you should now see it at 8k (8192). Now, in the same terminal window, we can launch our autoortho binary:
'./autoortho_lin_1.4.2.bin'

Note: when you close your terminal window that you entered these commands into (or start another) it will no longer have this raised limit. This is a python safegaurd, not a flaw. It helps protect you against scenerios where touching a large number of files can indicate issues (e.g. resource exhaustion, ransomeware attacks, bad code, etc).

### On Linux this does not start/gives a FUSE error
Make sure that your `/etc/fuse.conf` files is set to `user_allow_other`.  You may need to uncomment a line.

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

# Instructions

## Linux Setup
Assumptions:

* You have installed X-Plane 11.50+
* You have a reasonably fast CPU and broadband internet 
* FUSE is installed and setup (Enable `user_allow_other` in /etc/fuse.conf, if not already the case.) 

### Steps:

1. Download the most recent packaged release (autortho_lin_####.bin) [from here](https://github.com/ProgrammingDinosaur/autoortho/releases/latest)
3. Make executable if needed `chmod +x autoortho_lin.bin` 
4. Run the program `./autoortho_lin.bin`
5. Configure your settings and paths for your system 
6. Download and setup an ortho set from the 'Scenery' tab.
7. Click **Start Streaming**
8. Configure your scenery_packs.ini file appropriately
9. Run X-Plane and choose a location for an ortho set you have downloaded

## Windows Setup

Assumptions:

* You are running 64bit Windows 10+
* You have install X-Plane 11.50+
* You have setup and configured either: [Dokan](https://github.com/dokan-dev/dokany/releases/latest) or [WinFSP](https://github.com/winfsp/winfsp)
* * If you have trouble with one, try the other! 
    Some users on Windows 11 have better luck
    with WinFSP for some reason.  *Windows users should always attempt both before asking questions on forums!!!* 
* * If both Dokan and WinFSP are installed, Dokan will be preferred. 
    Users can force preferring WinFSP by setting prefer_winfsp=True in their config file.
* You install all components, including scenery, on a local NTFS drive.

### Zip File Steps:

1. Download the most recent packaged release (autoortho_win_####.zip) [from here](https://github.com/ProgrammingDinosaur/autoortho/releases/latest)
2. Extract the downloaded directory.
4. Run `autoortho_win.exe` from within the extracted dir.
5. Configure your settings and paths for your system 
6. Download and setup an ortho set from the 'Scenery' tab.
7. Click **Start Streaming**
8. Configure your scenery_packs.ini file appropriately 
9. Run X-Plane and choose a location for an ortho set you have downloaded

### Experimental Installer Steps:

Alternatively, there is now an installer that can be used.  This is
experimental ATM:

1. Download the most recent packaged release (AutoOrtho_####.exe) [from here](https://github.com/ProgrammingDinosaur/autoortho/releases/latest)
2. Run the program.
3. If there is a prior install you will be prompted to uninstall first.  Do
   this.
4. Run AutoOrtho from your system start menu.
5. Configure your settings and paths for your system 
6. Download and setup an ortho set from the 'Scenery' tab.
7. Click **Start Streaming**
8. Configure your scenery_packs.ini file appropriately 
9. Run X-Plane and choose a location for an ortho set you have downloaded

## MacOS Setup
Assumptions:

* You have installed X-Plane 11.50+
* You have an Apple Silicon Computer (AutoOrtho Mac only supports arm64)
* FUSE is installed, MacFuse and Fuse-T are supported. 

### Steps:

1. If you haven't install MacFuse or Fuse-T. Fuse-T was used for testing the mac binary but both should work.
2. Download the most recent packaged release (autortho_mac_####.zip) [from here](https://github.com/ProgrammingDinosaur/autoortho/releases/latest)
3. Unzip on the location you want.
4. Run the program `AutoOrtho.app`
5. **Important: macOS Quarantine Warning** - This program is currently unsigned. On first run, macOS will block it from opening. You have two options to fix this:

    **Option A (Recommended):** Remove the quarantine attribute using Terminal:
    ```bash
    xattr -d com.apple.quarantine /path/to/AutoOrtho.app
    ```
    *Tip: You can drag the AutoOrtho.app onto the Terminal window to auto-fill the path.*

    **Option B:** Use System Settings:
    - Go to **Settings → Privacy and Security**
    - Scroll down to find the message about `AutoOrtho.app` being blocked
    - Click **Open Anyway**

6. Configure your settings and paths for your system 
7. Download and setup an ortho set from the 'Scenery' tab.
8. Click **Start Streaming**
9. Configure your scenery_packs.ini file appropriately
10. Run X-Plane and choose a location for an ortho set you have downloaded

---


## Configuring your scenery_packs.ini

Use this image as a guide on how to order your X-Plane packages within the scenery_packs.ini file.

[scenery_packs.ini_config](imgs/scenery_ini_guide.png)

---

## Requirements and Compatibility

To run from source, this project requires python3.x and all pre-requisites in the
`requirements.txt` file.  Also, a copy of X-Plane 11.50+ of course.

Most testing is done on Ubuntu 22.04 using FUSE.  Other Linux flavors ought to work as
well.  MacOS very likely *should* work, but I have no way of testing it.

I have done testing on Windows 10 with the
[Dokan](https://github.com/dokan-dev/dokany/releases/latest) project.
This does appear to work fine, but this is not how I use this project, so it's not my primary concern.

## Performance Features

AutoOrtho includes advanced performance optimizations to minimize stuttering and reduce loading times:

### Native Pipeline (High Performance Mode)

AutoOrtho includes a **native C pipeline** that dramatically improves DDS texture building performance by bypassing Python's Global Interpreter Lock:

- **10-20x faster** DDS texture building
- **True multi-core parallelism** via OpenMP
- **Native HTTP client** with connection pooling for faster downloads

The native pipeline is automatically used when available. See the [Performance Tuning Guide](performance.md#native-pipeline-architecture) for details.

> **Note:** Apple Maps downloads always use the Python HTTP client due to its special authentication requirements. Other imagery sources use the faster native client.

### Additional Features

- **Spatial Prefetching**: Downloads tiles ahead of your aircraft
- **SimBrief Integration**: Uses your flight plan for smarter prefetching
- **Dynamic Zoom**: Automatically adjusts quality based on altitude
- **Time Budget System**: Predictable tile loading times

See the [Performance Tuning Guide](performance.md) for configuration options.

---

## Known issues and limits
* Currently I try to limit the memory usage, though it's possible it
  will exceed this in certain scenarios.
* Not really possible to post-process the satellite photos.  You get what you
  get.
* Will add a couple minutes to initial flight time for new areas.  That's to
  be expected though.
* Will require and use a lot of internet requests (surprise!) and writes a lot of small files locally.  
  There are many parts along your path that may need tuning or may not like this (ISP, DNS, Wifi, drive speed, CPU speed, etc).  
  User's should be ready to diagnose their specific setup and determine if this project fits withing their own personal computing requirements.
* Windows has too many variations with how filesystems are handled, links are
  done, intrusive malware detection systems, etc and will likely always be
  slower and more difficult to use with this project.
* **Apple Maps** always uses Python HTTP client (not native) due to special authentication requirements.

## TODOS

* ~See if this will work on Windows with WinFSP~ 
* ~Allow for overriding the satellite source type.  Since this pulls on the fly, we aren't stuck with what was setup initially with Ortho4XP.~
* ~Package a set of DSF files to get started with~
* Attempt to make a workable OSX version

## Warnings, Warranty, etc

This project is provided free of charge and is not warrantied in any way.  Use
at your own risk.

Accessing map data from 3rd parties may or may not be allowed and is up to you
the end user to determine and use appropriately.

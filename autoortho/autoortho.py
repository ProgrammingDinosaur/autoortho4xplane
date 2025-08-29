#!/usr/bin/env python

import os
import sys
import time
import ctypes
import shutil
import signal
import tempfile
import platform
import argparse
import threading

from pathlib import Path
from contextlib import contextmanager

import aoconfig
import aostats
import winsetup
import macsetup
import flighttrack

from utils.constants import MAPTYPES
from version import __version__

import logging
log = logging.getLogger(__name__)

import geocoder

# Import PyQt6 modules

from PySide6.QtWidgets import QApplication
import config_ui_qt as config_ui
USE_QT = True
class MountError(Exception):
    pass

class AutoOrthoError(Exception):
    pass

@contextmanager
def setupmount(mountpoint, systemtype):
    mountpoint = os.path.expanduser(mountpoint)

    placeholder_path = os.path.join(mountpoint, ".AO_PLACEHOLDER")

    # Stash placeholder
    if os.path.isdir(mountpoint):
        log.info(f"Existing mountpoint detected: {mountpoint}")
        if os.path.lexists(placeholder_path):
            log.info(f"Detected placeholder dir.  Removing: {mountpoint}")
            shutil.rmtree(mountpoint)

    # Setup
    log.info(f"Setting up mountpoint: {mountpoint}")
    if systemtype == "Linux-FUSE":
        if not os.path.exists(mountpoint):
            os.makedirs(mountpoint)
        if not os.path.isdir(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")

    elif systemtype == "dokan-FUSE":
        ret = winsetup.setup_dokan_mount(mountpoint)
        if not ret:
            raise MountError(f"Failed to setup mount point {mountpoint}!")

    elif systemtype == "winfsp-FUSE":
        ret = winsetup.setup_winfsp_mount(mountpoint)
        if not ret:
            raise MountError(f"Failed to setup mount point {mountpoint}!")

    elif systemtype == "macOS":
        ret = macsetup.setup_macfuse_mount(mountpoint)
        if not ret:
            raise MountError(f"Failed to setup mount point {mountpoint}!")

    else:
        log.error(f"Unknown mount type of {systemtype}!")
        time.sleep(5)
        raise MountError(f"Unknown system type: {systemtype} for mount {mountpoint}")

    yield mountpoint

    # Cleanup
    if os.path.lexists(mountpoint):
        log.info(f"Cleaning up mountpoint: {mountpoint}")
        os.rmdir(mountpoint)

    # Restore placeholder
    log.info(f"Restoring placeholder for mountpoint: {mountpoint}")
    structure = [
        os.path.join(mountpoint, 'Earth nav data'),
        os.path.join(mountpoint, 'terrain'),
        os.path.join(mountpoint, 'textures'),
    ]

    for d in structure:
        os.makedirs(d)

    Path(placeholder_path).touch()
    log.info(f"Mount point {mountpoint} exiting.")


def diagnose(CFG):
    if platform.system() == 'Windows':
        systemtype, libpath = winsetup.find_win_libs()
    else:
        systemtype = "Linux-FUSE"

    location = geocoder.ip("me")

    log.info("Waiting for mounts...")
    for scenery in CFG.scenery_mounts:
        mount = scenery.get('mount')
        ret = False
        for i in range(5):
            time.sleep(i)
            ret = os.path.isdir(os.path.join(mount, 'textures'))
            if ret:
                break
            log.info('.')

    failed = False
    log.info("\n\n")
    log.info("------------------------------------")
    log.info(" Diagnostic check ...")
    log.info("------------------------------------")
    log.info(f"Detected system: {platform.uname()}")
    log.info(f"Detected location {location.address}")
    log.info(f"Detected installed scenery:")
    for scenery in CFG.scenery_mounts:
        root = scenery.get('root')
        mount = scenery.get('mount')
        log.info(f"    {root}")
        ret = os.path.isdir(os.path.join(mount, 'textures'))
        log.info(f"        Mounted? {ret}")
        if not ret:
            failed = True

    log.info(f"Checking maptypes:")
    import getortho
    for maptype in MAPTYPES:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = getortho.Chunk(2176, 3232, maptype, 13, cache_dir=tmpdir)
            ret = c.get()
            if ret:
                log.info(f"    Maptype: {maptype} OK!")
            else:
                log.warning(f"    Maptype: {maptype} FAILED!")
                failed = True

    log.info("------------------------------------")
    if failed:
        log.warning("***************")
        log.warning("***************")
        log.warning("FAILURES DETECTED!!")
        log.warning("Please review logs and setup.")
        log.warning("***************")
        log.warning("***************")
        return False
    else:
        log.info(" Diagnostics done.  All checks passed")
        return True
    log.info("------------------------------------\n\n")



class AOMount:
    mounts_running = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.mount_threads = []
        self.active_mounts = {}
        self._mount_lock = threading.RLock()



    def mount_sceneries(self, blocking=True):
        if not self.cfg.scenery_mounts:
            log.warning(f"No installed sceneries detected.  Exiting.")
            return

        self.mounts_running = True
        for scenery in self.cfg.scenery_mounts:
            t = threading.Thread(
                target=self.domount,
                daemon=True if platform.system() == 'Darwin' else False,
                args=(
                    scenery.get('root'),
                    scenery.get('mount'),
                    self.cfg.fuse.threading
                )
            )
            t.start()
            self.mount_threads.append(t)

        if not blocking:
            log.info("Running mounts in non-blocking mode.")
            time.sleep(1)
            diagnose(self.cfg)
            return

        try:
            def handle_sigterm(sig, frame):
                raise(SystemExit)

            signal.signal(signal.SIGTERM, handle_sigterm)

            time.sleep(1)
            # Check things out
            diagnose(self.cfg)

            while self.mounts_running:
                time.sleep(1)

        except (KeyboardInterrupt, SystemExit) as err:
            self.running = False
            log.info(f"Exiting due to {err}")
        finally:
            log.info("Shutting down ...")
            self.unmount_sceneries()


    def unmount_sceneries(self):
        log.info("Unmounting ...")
        self.mounts_running = False
        for scenery in self.cfg.scenery_mounts:
            self.unmount(scenery.get('mount'))

        log.info("Wait on threads...")
        for t in self.mount_threads:
            t.join(5)
            log.info(f"Thread {t.ident} exited.")
        log.info("Unmount complete")


    def domount(self, root, mountpoint, threading=True):

        if threading:
            log.info("Running in multi-threaded mode.")
            nothreads = False
        else:
            log.info("Running in single-threaded mode.")
            nothreads = True

        root = os.path.expanduser(root)

        try:
            if platform.system() == 'Windows':
                systemtype, libpath = winsetup.find_win_libs()
                with setupmount(mountpoint, systemtype) as mount:
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import autoortho_fuse
                    import mfusepy
                    mfusepy._libfuse = ctypes.CDLL(libpath)
                    ao = autoortho_fuse.AutoOrtho(root)
                    with self._mount_lock:
                        self.active_mounts[mount] = ao
                    try:
                        autoortho_fuse.run(
                                ao,
                                mount,
                                nothreads
                        )
                    finally:
                        with self._mount_lock:
                            self.active_mounts.pop(mount, None)
            elif platform.system() == 'Darwin':
                # systemtype, libpath = macsetup.find_mac_libs()
                systemtype = "macOS"
                with setupmount(mountpoint, systemtype) as mount:
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import autoortho_fuse
                    import mfusepy
                    # mfusepy._libfuse = ctypes.CDLL(libpath)
                    ao = autoortho_fuse.AutoOrtho(root)
                    with self._mount_lock:
                        self.active_mounts[mount] = ao
                    try:
                        autoortho_fuse.run(
                                ao,
                                mount,
                                nothreads
                        )
                    finally:
                        with self._mount_lock:
                            self.active_mounts.pop(mount, None)
            else:
                # Linux
                with setupmount(mountpoint, "Linux-FUSE") as mount:
                    log.info("Running in FUSE mode.")
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import autoortho_fuse
                    ao = autoortho_fuse.AutoOrtho(root)
                    with self._mount_lock:
                        self.active_mounts[mount] = ao
                    try:
                        autoortho_fuse.run(
                                ao,
                                mount,
                                nothreads
                        )
                    finally:
                        with self._mount_lock:
                            self.active_mounts.pop(mount, None)

        except Exception as err:
            log.error(f"Exception detected when running FUSE mount: {err}.  Exiting...")
            time.sleep(5)

    def switch_map_provider(self, provider: str) -> bool:
        """Switch map provider across all active mounts safely.

        Returns True if at least one mount accepted the change.
        """
        accepted = False
        with self._mount_lock:
            mounts = list(self.active_mounts.values())
        for ao in mounts:
            try:
                ok = ao.switch_provider(provider)
                accepted = accepted or bool(ok)
            except Exception:
                continue
        if accepted:
            log.info(f"Switched provider to {provider} across {len(mounts)} mounts (where supported)")
        else:
            log.warning("Provider switch requested but no mounts accepted the change")
        return accepted

    def unmount(self, mountpoint):
        log.info(f"Shutting down {mountpoint}")
        poison_path = os.path.join(mountpoint, ".poison")

        # Trigger getattr('.poison') to invoke fuse_exit inside the FUSE thread
        try:
            os.lstat(poison_path)
        except FileNotFoundError:
            # The file doesn't need to exist; getattr handler keys on the suffix
            pass
        except Exception as exc:
            log.debug(f"Poison trigger stat failed: {exc}")

        # Wait briefly for the mount to go away naturally
        deadline = time.time() + 10
        while time.time() < deadline:
            if not os.path.ismount(mountpoint):
                break
            time.sleep(0.5)

        # macOS can linger; force unmount if still mounted
        if platform.system() == 'Darwin' and os.path.ismount(mountpoint):
            try:
                import subprocess
                log.info(f"Force unmounting {mountpoint} via diskutil")
                subprocess.run(
                    ["diskutil", "unmount", "force", mountpoint],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                log.warning(f"Force unmount attempt failed: {exc}")


class AOMountUI(AOMount, config_ui.ConfigUI):
    def __init__(self, *args, **kwargs):
        AOMount.__init__(self, *args, **kwargs)
        config_ui.ConfigUI.__init__(self, *args, **kwargs)
    
    def mount_sceneries(self, blocking=True):
        """Mount sceneries using AOMount functionality"""
        return AOMount.mount_sceneries(self, blocking)
    
    def unmount_sceneries(self):
        """Unmount sceneries using AOMount functionality"""
        return AOMount.unmount_sceneries(self)

    # Bridge for UI -> mount manager provider switch
    def switch_map_provider(self, provider: str) -> bool:
        return AOMount.switch_map_provider(self, provider)


def main():
    log.info(f"AutoOrtho version: {__version__}")

    parser = argparse.ArgumentParser(
        description="AutoOrtho: X-Plane scenery streamer"
    )
    parser.add_argument(
        "root",
        help = "Root directory of orthophotos",
        nargs="?"
    )
    parser.add_argument(
        "mountpoint",
        help = "Directory within X-Plane 11 custom scenery folder to mount",
        nargs="?"
    )
    parser.add_argument(
        "-c",
        "--configure",
        default=False,
        action="store_true",
        help = "Run the configuration setup again."
    )
    parser.add_argument(
        "-H",
        "--headless",
        default=False,
        action="store_true",
        help = "Run in headless mode."
    )

    args = parser.parse_args()

    CFG = aoconfig.CFG
    if args.configure or (CFG.general.showconfig and not args.headless):
        # Show cfgui at start
        run_headless = False
    else:
        # Don't show cfgui
        run_headless = True

    stats = aostats.AOStats()


    import flighttrack
    ftrack = threading.Thread(
        target=flighttrack.run,
        daemon=True
    )

    # Start helper threads
    ftrack.start()
    stats.start()

    # Run things
    if args.root and args.mountpoint:
        # Just mount specific requested dirs
        root = args.root
        mountpoint = args.mountpoint
        print("root:", root)
        print("mountpoint:", mountpoint)
        aom = AOMount(CFG)
        aom.domount(
            root,
            mountpoint,
            CFG.fuse.threading
        )
    elif run_headless:
        log.info("Running headless")
        aom = AOMount(CFG)
        aom.mount_sceneries()
    else:
        log.info("Running CFG UI")
        if USE_QT:
            app = QApplication(sys.argv)
            cfgui = AOMountUI(CFG)
            cfgui.show()
            app.exec()
        else:
            cfgui = AOMountUI(CFG)
            cfgui.setup()

    stats.stop()
    flighttrack.ft.stop()

    log.info("AutoOrtho exit.")


if __name__ == '__main__':
    main()

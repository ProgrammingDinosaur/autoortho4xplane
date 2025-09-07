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

from utils.constants import MAPTYPES, system_type
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
    created_mount_dir = False
    had_placeholder = False

    # Preflight: ensure mount dir is a directory and not currently mounted
    if os.path.ismount(mountpoint):
        raise MountError(f"{mountpoint} is already mounted")
    # For WinFsp, the directory must NOT exist; let winsetup handle removal of placeholders.
    if systemtype != "winfsp-FUSE":
        if not os.path.exists(mountpoint):
            os.makedirs(mountpoint, exist_ok=True)
            created_mount_dir = True
        elif not os.path.isdir(mountpoint):
            raise MountError(f"{mountpoint} exists but is not a directory")

        # If it's not empty and doesn't look like our placeholder, refuse
        if os.listdir(mountpoint):
            if os.path.exists(placeholder_path):
                had_placeholder = True
                # Remove our placeholder content to ensure an empty dir for FUSE
                try:
                    for name in ('Earth nav data', 'terrain', 'textures'):
                        p = os.path.join(mountpoint, name)
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                        elif os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    # Remove the placeholder marker file/dir last
                    try:
                        if os.path.isdir(placeholder_path) and not os.path.islink(placeholder_path):
                            shutil.rmtree(placeholder_path, ignore_errors=True)
                        elif os.path.exists(placeholder_path):
                            os.remove(placeholder_path)
                    except Exception:
                        pass
                except Exception as e:
                    log.warning(f"Failed to cleanup placeholder content at {mountpoint}: {e}")
                # After cleanup, verify directory is now empty
                if os.listdir(mountpoint):
                    raise MountError(f"Mount point {mountpoint} is not empty after cleanup")
            else:
                raise MountError(f"Mount point {mountpoint} exists and is not empty")

    # Platform-specific setup
    if systemtype == "Linux-FUSE":
        pass
    elif systemtype == "dokan-FUSE":
        if not winsetup.setup_dokan_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    elif systemtype == "winfsp-FUSE":
        if not winsetup.setup_winfsp_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    elif systemtype == "macOS":
        if not macsetup.setup_macfuse_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    else:
        raise MountError(f"Unknown system type: {systemtype} for mount {mountpoint}")

    try:
        yield mountpoint
    finally:
        # Do not remove if still mounted; just try to present placeholder content.
        try:
            if os.path.ismount(mountpoint):
                log.debug(f"Skipping cleanup: still mounted: {mountpoint}")
            else:
                for d in ('Earth nav data', 'terrain', 'textures'):
                    os.makedirs(os.path.join(mountpoint, d), exist_ok=True)
                Path(placeholder_path).touch()
        except Exception as e:
            log.warning(f"Placeholder restore failed for {mountpoint}: {e}")


def diagnose(CFG):

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
        if maptype == "Use tile default":
            continue
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



    def mount_sceneries(self, blocking=True):
        if not self.cfg.scenery_mounts:
            log.warning(f"No installed sceneries detected.  Exiting.")
            return

        self.mounts_running = True
        for scenery in self.cfg.scenery_mounts:
            t = threading.Thread(
                target=self.domount,
                daemon=False,
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
                for t in list(self.mount_threads):
                    if not t.is_alive():
                        log.error(f"Mount thread {t.name or t.ident} died; failing all mounts.")
                        self.mounts_running = False
                        break
                time.sleep(0.5)

        except (KeyboardInterrupt, SystemExit) as err:
            self.mounts_running = False
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
            if system_type == 'windows':
                systemtype, libpath = winsetup.find_win_libs()
                with setupmount(mountpoint, systemtype) as mount:
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import mfusepy
                    import autoortho_fuse
                    mfusepy._libfuse = ctypes.CDLL(libpath)
                    autoortho_fuse.run(
                            autoortho_fuse.AutoOrtho(root),
                            mount,
                            mount.split('/')[-1],
                            nothreads
                    )
            elif system_type == 'darwin':
                # systemtype, libpath = macsetup.find_mac_libs()
                systemtype = "macOS"
                with setupmount(mountpoint, systemtype) as mount:
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import autoortho_fuse
                    import mfusepy
                    # mfusepy._libfuse = ctypes.CDLL(libpath)
                    autoortho_fuse.run(
                            autoortho_fuse.AutoOrtho(root),
                            mount,
                            mount.split('/')[-1],
                            nothreads
                    )
            else:
                # Linux
                with setupmount(mountpoint, "Linux-FUSE") as mount:
                    log.info("Running in FUSE mode.")
                    log.info(f"AutoOrtho:  root: {root}  mountpoint: {mount}")
                    import autoortho_fuse
                    autoortho_fuse.run(
                            autoortho_fuse.AutoOrtho(root),
                            mount,
                            mount.split('/')[-1],
                            nothreads
                    )

        except Exception as err:
            log.exception(f"Exception in FUSE mount: {err}")
            # Per your spec, a failure while X-Plane is running should terminate everything.
            time.sleep(5)
            os._exit(2)

    def unmount(self, mountpoint):
        log.info(f"Shutting down {mountpoint}")
        poison_path = os.path.join(mountpoint, ".poison")

        try:
            os.lstat(poison_path)  # triggers getattr('.poison') -> fuse_exit
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug(f"Poison trigger stat failed: {exc}")

        deadline = time.time() + 10
        while time.time() < deadline:
            if not os.path.ismount(mountpoint):
                break
            time.sleep(0.5)

        if os.path.ismount(mountpoint):
            try:
                import subprocess
                if system_type == 'darwin':
                    log.info(f"Force unmounting {mountpoint} via diskutil")
                    subprocess.run(["diskutil", "unmount", "force", mountpoint],
                                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif system_type == 'linux':
                    log.info(f"Force unmounting {mountpoint} via fusermount -u -z")
                    if shutil.which("fusermount"):
                        subprocess.run(["fusermount", "-u", "-z", mountpoint],
                                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.run(["umount", "-l", mountpoint],
                                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif system_type == 'windows':
                    log.info(f"Force unmounting {mountpoint} via winsetup.force_unmount")
                    try:
                        winsetup.force_unmount(mountpoint)  # implement this in winsetup for both backends
                    except Exception as exc:
                        log.warning(f"Windows force unmount failed: {exc}")
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
        log.info("root:", root)
        log.info("mountpoint:", mountpoint)
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

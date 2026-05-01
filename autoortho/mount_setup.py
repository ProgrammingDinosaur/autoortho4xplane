import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager

try:
    from autoortho.utils.mount_utils import cleanup_mountpoint, safe_ismount
except ImportError:
    from utils.mount_utils import cleanup_mountpoint, safe_ismount

log = logging.getLogger(__name__)


class MountError(Exception):
    pass


def _winsetup():
    try:
        from autoortho import winsetup
    except ImportError:
        import winsetup
    return winsetup


def _macsetup():
    try:
        from autoortho import macsetup
    except ImportError:
        import macsetup
    return macsetup


@contextmanager
def setupmount(mountpoint, systemtype):
    mountpoint = os.path.expanduser(mountpoint)
    placeholder_path = os.path.join(mountpoint, ".AO_PLACEHOLDER")

    if safe_ismount(mountpoint):
        log.warning("%s is already mounted; attempting to unmount", mountpoint)
        try:
            if systemtype in ("winfsp-FUSE", "dokan-FUSE"):
                try:
                    _winsetup().force_unmount(mountpoint)
                except Exception as exc:
                    log.debug("Windows force_unmount preflight failed: %s", exc)
            elif systemtype == "macOS":
                try:
                    subprocess.run(
                        ["diskutil", "unmount", "force", mountpoint],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as exc:
                    log.debug("macOS preflight unmount failed: %s", exc)
            elif systemtype == "Linux-FUSE":
                try:
                    if shutil.which("fusermount"):
                        subprocess.run(
                            ["fusermount", "-u", "-z", mountpoint],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        subprocess.run(
                            ["umount", "-l", mountpoint],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                except Exception as exc:
                    log.debug("Linux preflight unmount failed: %s", exc)
        except Exception as exc:
            log.debug("Preflight unmount exception ignored: %s", exc)

        deadline = time.time() + 10
        while time.time() < deadline:
            if not safe_ismount(mountpoint):
                break
            time.sleep(0.5)
        if safe_ismount(mountpoint):
            raise MountError(f"{mountpoint} is already mounted")

    if systemtype != "winfsp-FUSE":
        if not os.path.exists(mountpoint):
            os.makedirs(mountpoint, exist_ok=True)
        elif not os.path.isdir(mountpoint):
            raise MountError(f"{mountpoint} exists but is not a directory")

        if os.listdir(mountpoint):
            try:
                entries = [
                    e
                    for e in os.listdir(mountpoint)
                    if e not in (".DS_Store", ".metadata_never_index", ".poison")
                ]
            except Exception:
                entries = os.listdir(mountpoint)

            if not entries:
                try:
                    poison_fp = os.path.join(mountpoint, ".poison")
                    if os.path.exists(poison_fp):
                        os.remove(poison_fp)
                except Exception:
                    pass
            elif os.path.exists(placeholder_path):
                try:
                    for name in ("Earth nav data", "terrain", "textures"):
                        p = os.path.join(mountpoint, name)
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                        elif os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    try:
                        if os.path.isdir(placeholder_path) and not os.path.islink(placeholder_path):
                            shutil.rmtree(placeholder_path, ignore_errors=True)
                        elif os.path.exists(placeholder_path):
                            os.remove(placeholder_path)
                    except Exception:
                        pass
                except Exception as exc:
                    log.warning("Failed to cleanup placeholder content at %s: %s", mountpoint, exc)
                if any(e for e in os.listdir(mountpoint) if e not in (".DS_Store", ".metadata_never_index")):
                    raise MountError(f"Mount point {mountpoint} is not empty after cleanup")
            else:
                raise MountError(f"Mount point {mountpoint} exists and is not empty")

    if systemtype == "Linux-FUSE":
        pass
    elif systemtype == "dokan-FUSE":
        if not _winsetup().setup_dokan_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    elif systemtype == "winfsp-FUSE":
        if not _winsetup().setup_winfsp_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    elif systemtype == "macOS":
        if not _macsetup().setup_macfuse_mount(mountpoint):
            raise MountError(f"Failed to setup mount point {mountpoint}!")
    else:
        raise MountError(f"Unknown system type: {systemtype} for mount {mountpoint}")

    try:
        yield mountpoint
    finally:
        try:
            cleanup_mountpoint(mountpoint)
        except Exception as exc:
            log.warning("Failed to cleanup mountpoint %s: %s", mountpoint, exc)

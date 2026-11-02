import os
import logging
import subprocess
import threading
import time

try:
    from autoortho.utils.macos_mounts import find_mount, mount_path
except ImportError:
    from utils.macos_mounts import find_mount, mount_path

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.utils.mount_utils import safe_ismount
except ImportError:
    from utils.mount_utils import safe_ismount

log = logging.getLogger(__name__)

_IGNORE_FILES = {".DS_Store", ".metadata_never_index"}
_AO_PLACEHOLDER_ITEMS = {"Earth nav data", "terrain", "textures", ".AO_PLACEHOLDER"}
_pending_unmounts = {}
_unmount_lock = threading.Lock()


def unmount_macfuse(path, force=False, timeout=8.0):
    """Detach while the FUSE worker can still answer kernel requests.

    A helper is essential: an unmount syscall can itself stick in the kernel.
    Do not use subprocess.run or a Popen context manager here: after a timeout
    both wait without a deadline for a killed helper, which may never exit.
    """
    path = mount_path(path)
    with _unmount_lock:
        previous = _pending_unmounts.get(path)
        if previous is not None and previous.poll() is None:
            log.error("Unmount helper still blocked for %s; leaving worker alive", path)
            return False
    if not safe_ismount(path):
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    for forced in ([True] if force else [False, True]):
        with _unmount_lock:
            previous = _pending_unmounts.get(path)
            if previous is not None and previous.poll() is None:
                log.error("Unmount helper still blocked for %s; leaving worker alive", path)
                return False
            command = ["/sbin/umount"] + (["-f"] if forced else []) + [path]
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                log.error("Cannot start unmount helper for %s: %s", path, exc)
                return False
            _pending_unmounts[path] = process
        try:
            result = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log.error("Unmount timed out for %s; keeping the FUSE worker alive", path)
            # A signal cannot recover a kernel deadlock. Never wait unboundedly
            # here, or launch more helpers against the same stuck filesystem.
            try:
                process.kill()
            except OSError:
                pass
            return False
        if result == 0:
            with _unmount_lock:
                _pending_unmounts.pop(path, None)
            return not safe_ismount(path)
        if time.monotonic() >= deadline:
            break
    log.error("macOS could not unmount %s", path)
    return False



def _is_only_ao_placeholder(dirpath):
    try:
        entries = [e for e in os.listdir(dirpath) if e not in _IGNORE_FILES]
    except Exception:
        return False
    return set(entries).issubset(_AO_PLACEHOLDER_ITEMS)

def _clear_ao_placeholder(dirpath):
    import shutil, os
    for name in _AO_PLACEHOLDER_ITEMS:
        p = os.path.join(dirpath, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.lexists(p):
            try: os.remove(p)
            except Exception: pass


def _is_effectively_empty(dirpath):
    try:
        entries = [e for e in os.listdir(dirpath) if e not in _IGNORE_FILES]
        return len(entries) == 0
    except Exception as e:
        log.debug(f"Listdir failed for {dirpath}: {e}")
        return False


def setup_macfuse_mount(path):
    """Setup macFUSE/FUSE-T mount point safely."""
    path = os.path.expanduser(path)
    real = os.path.realpath(path)

    # Reject symlink mountpoints; less surprises
    if os.path.islink(path):
        log.error(f"Mount point {path} is a symlink; refusing.")
        return False

    if os.path.lexists(real):
        if safe_ismount(real):
            log.warning(f"Mount point {path} is already mounted")
            return False
        if not os.path.isdir(real):
            log.error(f"Mount point {path} exists but is not a directory")
            return False
        if not _is_effectively_empty(real):
            # Accept directories that contain only our placeholder; clear them.
            if _is_only_ao_placeholder(real):
                _clear_ao_placeholder(real)
            else:
                log.warning(f"Mount point {path} exists and is not empty")
                return False
        # OK: empty directory; proceed
    else:
        try:
            os.makedirs(real, exist_ok=True)
            log.info(f"Created mount point directory: {real}")
        except OSError as e:
            log.error(f"Could not create mount point {real}: {e}")
            return False

    # Permission sanity: need write/execute on the directory to mount
    if not os.access(real, os.W_OK | os.X_OK):
        log.error(f"Mount point {real} is not writable/searchable by current user")
        return False

    # Finder/Volumes hint (debug level – not an error)
    if not real.startswith("/Volumes/"):
        log.debug("Not mounted in volumes, will not be visible in Finder (use volname option).")

    return True


def is_macfuse_mount(path: str) -> bool:
    """Recognize macFUSE and FUSE-T's local NFS backend at this mountpoint.

    FUSE-T's NFS mounts have no 'fuse' marker in their filesystem type.
    Accept the 'fuse-t:' alias or loopback NFS at the exact path, not remote
    NFS volumes. This is a readiness check, not proof of server identity.
    """
    try:
        entry = find_mount(path)
        if entry is None:
            return False
        if entry.fstype.startswith("fuse") or entry.fstype in {"osxfuse", "macfuse"}:
            return True
        if entry.fstype == "nfs":
            return entry.source.lower().startswith(
                ("fuse-t:", "localhost:", "127.0.0.1:", "[::1]:", "::1:")
            )
        return False
    except Exception as e:
        log.debug(f"is_macfuse_mount failed for {path}: {e}")
        return False

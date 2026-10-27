import os
import logging

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.utils.mount_utils import safe_ismount
except ImportError:
    from utils.mount_utils import safe_ismount

log = logging.getLogger(__name__)

_IGNORE_FILES = {".DS_Store", ".metadata_never_index"}
_AO_PLACEHOLDER_ITEMS = {"Earth nav data", "terrain", "textures", ".AO_PLACEHOLDER"}



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
        import subprocess
        abs_path = os.path.realpath(os.path.expanduser(path))
        output = subprocess.check_output(
            ["mount"], text=True, errors="ignore", timeout=5,
        )

        for line in output.splitlines():
            # Split from the right: mountpoint names may contain spaces or '('.
            location, separator, options = line.rpartition(" (")
            if not separator or not options.endswith(")"):
                continue
            source, separator, mountpoint = location.partition(" on ")
            if not separator or os.path.realpath(mountpoint) != abs_path:
                continue
            fstype = options[:-1].split(",", 1)[0].strip().lower()
            if (fstype.startswith("fuse") or fstype in {"osxfuse", "macfuse"}):
                return True
            if fstype == "nfs":
                # FUSE-T serves NFS on loopback; a remote NFS share is not FUSE-T.
                return source.lower().startswith(
                    ("fuse-t:", "localhost:", "127.0.0.1:", "[::1]:", "::1:")
                )
            return False
        return False
    except Exception as e:
        log.debug(f"is_macfuse_mount failed for {path}: {e}")
        return False

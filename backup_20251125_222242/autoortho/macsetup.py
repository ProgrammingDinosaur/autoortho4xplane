import os
import logging
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
        if os.path.ismount(real):
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
    """Return True if the given path is mounted via macFUSE/osxfuse.

    Parses the output of the macOS 'mount' command and verifies the FS type
    at the specific mount point contains a FUSE indicator (fuse, fusefs,
    osxfuse, macfuse).
    """
    try:
        import subprocess
        abs_path = os.path.abspath(os.path.expanduser(path))
        try:
            output = subprocess.check_output(["mount"], text=True, errors="ignore")
        except Exception:
            return False

        needle = f" on {abs_path} "
        for line in output.splitlines():
            if needle in line:
                # Example line:
                #   dev on /Volumes/MyMount (osxfuse, local, nodev, nosuid)
                if "(" in line and ")" in line:
                    inside = line[line.find("(") + 1: line.rfind(")")]
                    parts = [p.strip().lower() for p in inside.split(",")]
                    return any(
                        p.startswith("fuse") or "osxfuse" in p or "macfuse" in p or "fusefs" in p
                        for p in parts
                    )
                # If no parentheses, fall back to a substring heuristic
                ln = line.lower()
                return ("fuse" in ln) or ("osxfuse" in ln) or ("macfuse" in ln) or ("fusefs" in ln)
        return False
    except Exception as e:
        log.debug(f"is_macfuse_mount failed for {path}: {e}")
        return False

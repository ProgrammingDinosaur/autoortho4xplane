import os
import sys
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


def safe_ismount(path) -> bool:
    """
    Safe wrapper for os.path.ismount() that handles exceptions gracefully.
    
    On Windows, os.path.ismount() can raise OSError [WinError 123] for paths that
    don't exist yet or contain certain characters (e.g., paths with spaces).
    This function also handles TypeError for None or invalid path types.
    
    Args:
        path: The path to check for mount status. Can be str, bytes, or path-like.
              None or invalid types are handled gracefully.
        
    Returns:
        True if the path is a mount point, False otherwise (including error cases).
    """
    # Handle None or empty paths before calling os.path.ismount
    if path is None:
        return False
    
    try:
        if sys.platform == "darwin":
            from .macos_mounts import find_mount
            try:
                return find_mount(path) is not None
            except OSError as exc:
                # Fail closed: never inspect/clean a possibly live mount.
                log.warning("Cannot read cached mount table: %s", exc)
                return True
        return os.path.ismount(path)
    except (OSError, TypeError, ValueError) as e:
        # OSError: WinError 123 "The filename, directory name, or volume label syntax is incorrect"
        #          Can happen on Windows for paths that don't exist or have unusual formats
        # TypeError: Raised if path is not a valid path type (e.g., int, object)
        # ValueError: Raised for embedded null characters or other invalid path values
        log.debug(f"safe_ismount: os.path.ismount({path!r}) raised {type(e).__name__}: {e}")
        return False


_IGNORE_FILES = {".DS_Store", ".metadata_never_index"}
_AO_PLACEHOLDER_ITEMS = {"Earth nav data", "terrain", "textures", ".AO_PLACEHOLDER"}

def cleanup_mountpoint(mountpoint):
    # Checking lexists/rmdir first can deadlock against a stopped FUSE server.
    if safe_ismount(mountpoint):
        log.debug("Skipping cleanup: still mounted: %s", mountpoint)
        return False
    if os.path.islink(mountpoint) or not is_only_ao_placeholder(mountpoint):
        # Never label unrelated scenery as disposable placeholder content.
        log.warning("Skipping cleanup of non-placeholder directory: %s", mountpoint)
        return False
    placeholder_path = os.path.join(mountpoint, ".AO_PLACEHOLDER")
    log.info("Cleaning up mountpoint: %s", mountpoint)
    # Reusing the directory is idempotent and preserves filesystem metadata.
    for d in ('Earth nav data', 'terrain', 'textures'):
        os.makedirs(os.path.join(mountpoint, d), exist_ok=True)
    Path(placeholder_path).touch()
    return True


def _is_frozen() -> bool:
    """
    Check if running as a frozen/compiled application (PyInstaller).
    
    PyInstaller sets sys.frozen = True when running as a bundled executable.
    This is used to determine how to launch subprocess workers.
    """
    return getattr(sys, 'frozen', False)


def is_only_ao_placeholder(mountpoint: str) -> bool:
    """True if the directory contains only our known placeholder structure."""
    try:
        entries = [e for e in os.listdir(mountpoint) if e not in _IGNORE_FILES]
    except FileNotFoundError:
        return True  # treat missing dir as 'empty'
    except Exception as e:
        log.debug(f"is_only_ao_placeholder listdir failed: {e}")
        return False
    return set(entries).issubset(_AO_PLACEHOLDER_ITEMS)


def clear_ao_placeholder(mountpoint: str) -> None:
    """Remove our placeholder structure (and only that)."""
    try:
        for name in _AO_PLACEHOLDER_ITEMS:
            p = os.path.join(mountpoint, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.lexists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        log.info(f"Cleared AO placeholder from: {mountpoint}")
    except Exception as e:
        log.warning(f"clear_ao_placeholder failed for {mountpoint}: {e}")


def cleanup_stale_mount_folders(custom_scenery_path: str) -> int:
    """
    Clean up stale z_ao_* mount folders left behind from a crash.
    
    When AutoOrtho crashes during flight, it can leave lingering mount folders
    in the Custom Scenery directory. These folders cause errors on subsequent
    mounts because AutoOrtho detects them as already mounted.
    
    This function finds and removes any z_ao_* folders that:
    - Are not currently mounted
    - Are empty or contain only AutoOrtho placeholder content
    
    Args:
        custom_scenery_path: Path to the X-Plane Custom Scenery folder
        
    Returns:
        Number of stale folders cleaned up
    """
    if not custom_scenery_path or not os.path.isdir(custom_scenery_path):
        log.debug(f"cleanup_stale_mount_folders: invalid path: {custom_scenery_path}")
        return 0
    
    cleaned_count = 0
    
    try:
        entries = os.listdir(custom_scenery_path)
    except Exception as e:
        log.warning(f"cleanup_stale_mount_folders: failed to list {custom_scenery_path}: {e}")
        return 0
    
    for entry in entries:
        # Only process z_ao_* folders
        if not entry.startswith("z_ao_"):
            continue
        
        folder_path = os.path.join(custom_scenery_path, entry)
        
        # Skip if currently mounted
        if safe_ismount(folder_path):
            log.debug(f"cleanup_stale_mount_folders: skipping mounted folder: {folder_path}")
            continue

        if not os.path.isdir(folder_path) or os.path.islink(folder_path):
            continue
        
        # Check if folder is empty or contains only placeholder content
        if is_only_ao_placeholder(folder_path):
            try:
                # First clear the placeholder content
                clear_ao_placeholder(folder_path)
                
                # Remove any remaining ignorable files
                for ignore_file in _IGNORE_FILES:
                    ignore_path = os.path.join(folder_path, ignore_file)
                    if os.path.exists(ignore_path):
                        try:
                            os.remove(ignore_path)
                        except Exception:
                            pass
                
                # Now try to remove the empty directory
                if os.path.exists(folder_path):
                    os.rmdir(folder_path)
                
                log.info(f"Cleaned up stale mount folder: {folder_path}")
                cleaned_count += 1
            except OSError as e:
                # Directory not empty or other error - skip it
                log.warning(f"cleanup_stale_mount_folders: could not remove {folder_path}: {e}")
            except Exception as e:
                log.warning(f"cleanup_stale_mount_folders: unexpected error for {folder_path}: {e}")
        else:
            log.debug(f"cleanup_stale_mount_folders: skipping non-placeholder folder: {folder_path}")
    
    if cleaned_count > 0:
        log.info(f"Cleaned up {cleaned_count} stale mount folder(s)")
    
    return cleaned_count

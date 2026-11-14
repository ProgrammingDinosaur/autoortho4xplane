"""Portable file operations for files mode.

Safety rule (plan section 5.2.3): nothing here ever deletes recursively
through Custom Scenery. Pack links are removed only when they are links or
junctions that resolve under the files root; real folders and foreign links
are refused.
"""

import ctypes
import errno
import logging
import os
import shutil
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
O_BINARY = getattr(os, "O_BINARY", 0)

if IS_WINDOWS:
    from . import fsops_windows as _win
else:
    _win = None

# Allocation unit of sparse data: NTFS allocates sparse files in 64 KB
# compression units; APFS and ext4 in 4 KB blocks.
SPARSE_UNIT = 65536 if IS_WINDOWS else 4096
SUPPORTED_FS = {"ntfs", "refs", "apfs", "ext4", "btrfs", "xfs", "zfs", "f2fs"}
REFUSED_FS = {"exfat", "fat32", "fat", "vfat", "msdos", "hfs", "hfs+", "fat16"}


class RefuseToRemove(Exception):
    """A path that is not ours to remove."""


@dataclass
class ReplaceResult:
    ok: bool
    error: int = 0


def is_junction(path):
    try:
        return os.path.isjunction(path)
    except AttributeError:  # Python < 3.12
        return False


def is_link_or_junction(path):
    return os.path.islink(path) or is_junction(path)


def link_target(path):
    """Where a symlink or junction points (absolute), or None."""
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if target.startswith("\\\\?\\"):
        target = target[4:]
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(path), target)
    return os.path.normpath(target)


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def resolves_under(link, root):
    target = link_target(link)
    if target is None:
        return False
    target_n = _norm(target)
    root_n = _norm(root)
    return target_n == root_n or target_n.startswith(root_n + os.sep)


def remove_pack_link(link, files_root):
    """Remove a pack link (never its target). Refuses anything else."""
    if not is_link_or_junction(link):
        raise RefuseToRemove(f"{link} is not a link or junction")
    if not resolves_under(link, files_root):
        raise RefuseToRemove(f"{link} does not point under {files_root}")
    if is_junction(link):
        os.rmdir(link)
    else:
        os.unlink(link)


def create_dir_link(target, link):
    """A directory junction (Windows) or symlink (elsewhere) at ``link``."""
    if IS_WINDOWS:
        _win.create_junction(target, link)
    else:
        os.symlink(os.path.abspath(target), link, target_is_directory=True)


def ensure_dir_link(target, link, files_root=None):
    """Make ``link`` point at ``target``; replaces only our own stale link.

    Returns True when created or already correct. Raises RefuseToRemove when
    something else is in the way.
    """
    if is_link_or_junction(link):
        current = link_target(link)
        if current is not None and _norm(current) == _norm(target):
            return True
        if files_root is not None and resolves_under(link, files_root):
            remove_pack_link(link, files_root)
        elif files_root is None:
            raise RefuseToRemove(f"{link} points elsewhere ({current})")
        else:
            raise RefuseToRemove(f"{link} is a foreign link to {current}")
    elif os.path.lexists(link):
        if os.path.isdir(link) and not os.listdir(link):
            os.rmdir(link)  # an empty folder (stale mount point): safe
        else:
            raise RefuseToRemove(f"{link} exists and is not empty")
    create_dir_link(target, link)
    return True


def hard_link(src, dst):
    """Hard link; returns 0 or the errno/winerror (1142: too many links)."""
    try:
        os.link(src, dst)
        return 0
    except FileExistsError:
        return errno.EEXIST
    except OSError as exc:
        return getattr(exc, "winerror", None) or exc.errno or -1


def link_or_copy(src, dst):
    """Hard link where possible (same volume), else copy. True when done."""
    if os.path.exists(dst):
        return True
    err = hard_link(src, dst)
    if err == 0:
        return True
    try:
        shutil.copyfile(src, dst)
        return True
    except OSError as exc:
        log.warning("Could not link or copy %s -> %s: %s", src, dst, exc)
        return False


def same_volume(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def open_new(path):
    """Create a file exclusively for writing (binary)."""
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | O_BINARY, 0o644)


def open_rw(path):
    return os.open(path, os.O_RDWR | O_BINARY)


def open_ro(path):
    return os.open(path, os.O_RDONLY | O_BINARY)


def make_sparse(fd, size):
    """Mark sparse (Windows needs it before extending) and set the size."""
    if IS_WINDOWS:
        try:
            _win.set_sparse_fd(fd)
        except OSError as exc:
            log.debug("FSCTL_SET_SPARSE failed: %s", exc)
        _win.set_size_fd(fd, int(size))
        return
    os.ftruncate(fd, int(size))


def pwrite(fd, data, offset):
    if hasattr(os, "pwrite"):
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.pwrite(fd, view[written:], offset + written)
        return written
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    written = 0
    while written < len(view):
        written += os.write(fd, view[written:])
    return written


def pread(fd, length, offset):
    if hasattr(os, "pread"):
        return os.pread(fd, length, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    out = bytearray()
    while len(out) < length:
        chunk = os.read(fd, length - len(out))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


_libc = None


def _linux_punch(fd, offset, length):
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
    FALLOC_FL_KEEP_SIZE = 0x01
    FALLOC_FL_PUNCH_HOLE = 0x02
    if _libc.fallocate(fd, FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE, offset, length) != 0:
        raise OSError(ctypes.get_errno(), "fallocate punch hole failed")


def _mac_punch(fd, offset, length):
    import fcntl
    F_PUNCHHOLE = 99

    class _Punch(ctypes.Structure):
        _fields_ = [("fp_flags", ctypes.c_uint), ("reserved", ctypes.c_uint),
                    ("fp_offset", ctypes.c_longlong), ("fp_length", ctypes.c_longlong)]

    # Offsets must be block aligned on APFS.
    start = (offset + 4095) // 4096 * 4096
    end = (offset + length) // 4096 * 4096
    if end <= start:
        return
    arg = _Punch(0, 0, start, end - start)
    fcntl.fcntl(fd, F_PUNCHHOLE, bytes(arg))


def punch_hole(fd, offset, length):
    """Deallocate a byte range (reads back zeros). False when unsupported."""
    if length <= 0:
        return True
    try:
        if IS_WINDOWS:
            _win.zero_range_fd(fd, offset, length)
        elif IS_MAC:
            _mac_punch(fd, offset, length)
        else:
            _linux_punch(fd, offset, length)
        return True
    except (OSError, AttributeError) as exc:
        log.debug("Hole punching unavailable: %s", exc)
        return False


def allocated_size(path):
    """Bytes the file actually occupies on disk."""
    if IS_WINDOWS:
        try:
            return _win.allocated_size(path)
        except OSError:
            return os.stat(path).st_size
    st = os.stat(path)
    blocks = getattr(st, "st_blocks", None)
    return int(blocks) * 512 if blocks is not None else st.st_size


def replace(src, dst, retry_s=2.0):
    """Atomically replace ``dst`` with ``src``."""
    if IS_WINDOWS:
        err = _win.replace(src, dst, retry_s=retry_s)
        return ReplaceResult(err == 0, err)
    try:
        os.replace(src, dst)
        return ReplaceResult(True, 0)
    except OSError as exc:
        return ReplaceResult(False, exc.errno or -1)


def fs_type(path):
    """File system name of the volume holding ``path`` (lower case)."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if IS_WINDOWS:
        try:
            return _win.volume_fs_type(probe).lower()
        except Exception:
            return ""
    try:
        import psutil
        best = ("", "")
        real = os.path.realpath(probe)
        for part in psutil.disk_partitions(all=True):
            mp = part.mountpoint
            if real == mp or real.startswith(mp.rstrip("/") + "/"):
                if len(mp) > len(best[0]):
                    best = (mp, part.fstype)
        return best[1].lower()
    except Exception:
        return ""


def probe_sparse(directory, size=64 * 1024 * 1024):
    """True when a sparse file of ``size`` takes a small fraction of it."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f".ao-sparse-probe-{os.getpid()}")
    try:
        fd = open_new(path)
        try:
            make_sparse(fd, size)
            pwrite(fd, b"DDS ", 0)
            pwrite(fd, b"x", size - 1)
        finally:
            os.close(fd)
        return allocated_size(path) < size // 8
    except OSError as exc:
        log.warning("Sparse probe in %s failed: %s", directory, exc)
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def probe_hard_links(directory):
    os.makedirs(directory, exist_ok=True)
    a = os.path.join(directory, f".ao-link-probe-{os.getpid()}")
    b = a + "-2"
    try:
        with open(a, "wb") as handle:
            handle.write(b"x")
        return hard_link(a, b) == 0
    finally:
        for p in (a, b):
            try:
                os.remove(p)
            except OSError:
                pass


def free_bytes(path):
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def file_id(path):
    st = os.stat(path)
    return st.st_dev, st.st_ino

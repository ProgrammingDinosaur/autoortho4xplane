"""Unpack a downloaded release and check it is a complete AutoOrtho build."""

import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from .assets import KIND_APP, KIND_TAR, KIND_ZIP
from .install_info import BUILD_INFO_NAME, RUNTIME_DIR, executable_name
from .versions import parse_release_version

log = logging.getLogger(__name__)


class StageError(Exception):
    pass


def _check_member_name(name):
    """Reject absolute paths, drive letters and '..' (zip slip)."""
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or (len(normalized) > 1 and normalized[1] == ":")
        or ".." in path.parts
    ):
        raise StageError(f"The archive contains an unsafe path: {name!r}")


def _extract_zip(archive, destination, platform):
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        for member in members:
            _check_member_name(member.filename)
        if platform == "macos":
            # zipfile drops symlinks and the executable bit, both of which an
            # app bundle needs (Qt frameworks are symlink trees).
            result = subprocess.run(
                ["/usr/bin/ditto", "-x", "-k", str(archive), str(destination)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise StageError(
                    f"Could not unpack the app: {result.stderr.strip()}"
                )
            return
        for member in members:
            target = bundle.extract(member, destination)
            mode = (member.external_attr >> 16) & 0o777
            if mode and os.name != "nt":
                os.chmod(target, mode)


def _extract_tar(archive, destination):
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                _check_member_name(member.name)
            # The "data" filter refuses links that leave the folder, device
            # files and absolute paths, and drops set-uid bits.
            bundle.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise StageError(f"Could not unpack the archive: {exc}") from exc


def extract(archive, destination, kind, platform):
    """Unpack ``archive`` into a fresh ``destination`` folder."""
    destination = Path(destination)
    if destination.exists():
        remove_tree(destination)
    destination.mkdir(parents=True)
    try:
        if kind in (KIND_ZIP, KIND_APP):
            _extract_zip(archive, destination, platform)
        elif kind == KIND_TAR:
            _extract_tar(archive, destination)
        else:
            raise StageError(f"Cannot unpack a {kind!r} release.")
    except zipfile.BadZipFile as exc:
        raise StageError(f"The archive is damaged: {exc}") from exc


def _is_bundle_root(folder, platform):
    if platform == "macos":
        return (
            folder.suffix == ".app"
            and (folder / "Contents" / "MacOS" / "autoortho").is_file()
        )
    return (folder / executable_name(platform)).is_file() and (
        folder / RUNTIME_DIR
    ).is_dir()


def find_bundle_root(staged, platform):
    """The one folder in ``staged`` (at most two levels down) that is a build."""
    staged = Path(staged)
    candidates = [staged]
    for child in sorted(staged.iterdir()):
        if child.is_dir() and not child.is_symlink():
            candidates.append(child)
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and not grandchild.is_symlink():
                    candidates.append(grandchild)
    roots = [folder for folder in candidates if _is_bundle_root(folder, platform)]
    if len(roots) != 1:
        raise StageError(
            "The download does not contain exactly one AutoOrtho build."
        )
    return roots[0]


def _data_file(root, platform, name):
    """Where the build keeps ``autoortho/<name>`` (see autoortho.spec)."""
    if platform == "macos":
        candidates = [
            root / "Contents" / "Resources" / "autoortho" / name,
            root / "Contents" / "Frameworks" / "autoortho" / name,
            root / "Contents" / "MacOS" / "autoortho" / name,
        ]
    else:
        candidates = [root / RUNTIME_DIR / "autoortho" / name]
    for path in candidates:
        if path.is_file():
            return path
    return None


def bundle_version_text(root, platform):
    path = _data_file(root, platform, ".version")
    if path is None:
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def bundle_build_info(root, platform):
    path = _data_file(root, platform, BUILD_INFO_NAME)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_bundle(root, platform, expected_version, linux_variant=""):
    """Check the unpacked build is the expected version for this machine."""
    root = Path(root)
    if not _is_bundle_root(root, platform):
        raise StageError("The download is not a complete AutoOrtho build.")
    text = bundle_version_text(root, platform)
    version = parse_release_version(text)
    if version is None or version != expected_version:
        raise StageError(
            f"The download reports version {text or 'unknown'}, not "
            f"{expected_version}."
        )
    info = bundle_build_info(root, platform)
    if info:
        if info.get("platform") and info["platform"] != platform:
            raise StageError("The download was built for another platform.")
        variant = info.get("linux_variant") or ""
        if platform == "linux" and linux_variant and variant and variant != linux_variant:
            raise StageError(
                f"The download was built for {variant}, not {linux_variant}."
            )
    if platform != "windows":
        exe = (
            root / "Contents" / "MacOS" / "autoortho"
            if platform == "macos"
            else root / "autoortho"
        )
        mode = exe.stat().st_mode
        if not mode & stat.S_IXUSR:
            os.chmod(exe, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def tree_size(folder):
    total = 0
    for base, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def _make_writable(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def remove_tree(path):
    """Delete a folder, clearing read-only flags; quiet on failure."""
    path = Path(path)
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
        return
    if path.exists():
        shutil.rmtree(path, onexc=_make_writable)

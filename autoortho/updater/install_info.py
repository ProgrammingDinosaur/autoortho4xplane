"""How this copy of AutoOrtho was installed, and whether it can update itself."""

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .assets import KIND_APP, KIND_NSIS, KIND_TAR, KIND_ZIP
from .versions import parse_release_version

log = logging.getLogger(__name__)

KIND_SOURCE = "source"
KIND_UNKNOWN = "unknown"

BUILD_INFO_NAME = ".build-info.json"
NSIS_UNINSTALLER = "uninstall.exe"
NSIS_USER_MARKER = "_user_install_marker"
RUNTIME_DIR = "ao_files"  # PyInstaller contents_directory (autoortho.spec)


def platform_name(sys_platform=None):
    value = sys_platform or sys.platform
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "macos"
    return "linux"


def executable_name(platform):
    return "autoortho.exe" if platform == "windows" else "autoortho"


@dataclass(frozen=True)
class InstallInfo:
    platform: str
    kind: str
    version_text: str
    version: object  # packaging Version, or None for non-release builds
    root: Path | None  # the folder (or .app bundle) an update replaces
    executable: Path | None
    arch: str = ""
    linux_variant: str = ""
    all_users: bool = False  # NSIS: installed for all users (needs admin)
    unsupported_reason: str = ""

    @property
    def can_self_update(self):
        return not self.unsupported_reason

    @property
    def app_executable(self):
        """What to start to run this install."""
        if self.kind == KIND_APP and self.root is not None:
            return self.root / "Contents" / "MacOS" / "autoortho"
        return self.executable


def load_build_info(meipass=None):
    """The build info the spec bundled, or {} (source runs, old builds)."""
    base = meipass if meipass is not None else getattr(sys, "_MEIPASS", None)
    if not base:
        return {}
    path = Path(base) / "autoortho" / BUILD_INFO_NAME
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def probe_writable(folder):
    """True when a file can really be created in ``folder``.

    ``os.access`` ignores Windows ACLs and read-only volumes, so try it.
    """
    try:
        handle, path = tempfile.mkstemp(prefix=".ao-update-probe-", dir=folder)
    except OSError:
        return False
    try:
        os.close(handle)
        os.unlink(path)
    except OSError:
        pass
    return True


def _app_bundle_for(executable):
    """``X.app`` for ``X.app/Contents/MacOS/<exe>``, else None."""
    parents = executable.parents
    if (
        len(parents) >= 3
        and parents[0].name == "MacOS"
        and parents[1].name == "Contents"
        and parents[2].suffix == ".app"
    ):
        return parents[2]
    return None


def detect(
    executable=None,
    frozen=None,
    sys_platform=None,
    version_text=None,
    build_info=None,
    writable=probe_writable,
):
    """Describe the running install. Arguments exist for tests."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    platform = platform_name(sys_platform)
    if version_text is None:
        try:
            from autoortho.version import __version__ as version_text
        except ImportError:
            from version import __version__ as version_text
    version_text = str(version_text or "").strip()
    version = parse_release_version(version_text)
    if build_info is None:
        build_info = load_build_info() if frozen else {}

    base = dict(
        platform=platform,
        version_text=version_text,
        version=version,
        arch=str(build_info.get("arch") or ""),
        linux_variant=str(build_info.get("linux_variant") or ""),
    )

    if not frozen:
        return InstallInfo(
            kind=KIND_SOURCE,
            root=None,
            executable=None,
            unsupported_reason=(
                "AutoOrtho is running from source code. Update it with git "
                "instead."
            ),
            **base,
        )

    exe = Path(executable or sys.executable).resolve()

    def unsupported(kind, root, reason, **extra):
        return InstallInfo(
            kind=kind,
            root=root,
            executable=exe,
            unsupported_reason=reason,
            **base,
            **extra,
        )

    if platform == "windows":
        root = exe.parent
        if (root / NSIS_UNINSTALLER).is_file():
            kind = KIND_NSIS
        else:
            kind = KIND_ZIP
        all_users = kind == KIND_NSIS and not (root / NSIS_USER_MARKER).exists()
        extra = {"all_users": all_users}
    elif platform == "macos":
        root = _app_bundle_for(exe)
        if root is None:
            return unsupported(
                KIND_UNKNOWN,
                exe.parent,
                "AutoOrtho is not running from AutoOrtho.app.",
            )
        kind = KIND_APP
        extra = {}
    else:
        root = exe.parent
        kind = KIND_TAR
        extra = {}

    if version is None:
        return unsupported(
            kind,
            root,
            f"This build ({version_text or 'unknown version'}) is not a "
            "release, so it cannot update itself.",
            **extra,
        )

    if kind != KIND_APP and not (root / RUNTIME_DIR).is_dir():
        return unsupported(
            kind,
            root,
            f"The install folder does not look like a release build "
            f"(no {RUNTIME_DIR} folder next to the program).",
            **extra,
        )

    if kind == KIND_APP and "AppTranslocation" in root.parts:
        return unsupported(
            kind,
            root,
            "macOS is running AutoOrtho from a temporary read-only copy. "
            "Move AutoOrtho.app to another folder (for example "
            "Applications), open it from there, and try again.",
            **extra,
        )

    if kind == KIND_TAR and not base["linux_variant"]:
        return unsupported(
            kind,
            root,
            "This build does not record which Linux release it was built "
            "for.",
            **extra,
        )

    # The installer asks for administrator rights itself; every other kind
    # renames the install folder aside and creates the new one beside it,
    # which needs write access to the folder that holds it.
    if kind != KIND_NSIS and not writable(root.parent):
        return unsupported(
            kind,
            root,
            f"AutoOrtho cannot write to {root.parent}. Move it to a folder "
            "you own, or update it by hand.",
            **extra,
        )

    return InstallInfo(
        kind=kind,
        root=root,
        executable=exe,
        **base,
        **extra,
    )

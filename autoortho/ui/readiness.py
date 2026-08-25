"""Readiness checks and storage helpers for the setup wizard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import platform
import shutil
import ctypes.util
from typing import Any, Iterable


if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.config_validation import ConfigurationInput, has_installed_scenery, validate_configuration
else:
    from ui.config_validation import ConfigurationInput, has_installed_scenery, validate_configuration


class ReadinessStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ReadinessCheck:
    id: str
    title: str
    status: ReadinessStatus
    message: str
    fix_action: str = ""
    last_checked: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.status == ReadinessStatus.SUCCESS

    @property
    def is_blocking(self) -> bool:
        return self.status in (
            ReadinessStatus.PENDING,
            ReadinessStatus.ERROR,
        )


@dataclass(frozen=True)
class SceneryChoice:
    region_id: str
    title: str
    selected: bool = False
    installed: bool = False
    description: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class SetupReadiness:
    checks: tuple[ReadinessCheck, ...]
    installed_scenery_present: bool
    selected_region_ids: tuple[str, ...]
    can_finish: bool

    def by_id(self, check_id: str) -> ReadinessCheck | None:
        return next((check for check in self.checks if check.id == check_id), None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def format_bytes(num_bytes: int | float | None) -> str:
    if num_bytes is None:
        return "0 B"
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(value)} B"


def nearest_existing_parent(path: str | os.PathLike[str]) -> Path:
    current = Path(path).expanduser()
    if not current.is_absolute():
        current = current.resolve(strict=False)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_space_bytes(path: str | os.PathLike[str]) -> int:
    existing = nearest_existing_parent(path)
    try:
        return shutil.disk_usage(existing).free
    except OSError:
        return 0


def recursive_directory_usage_bytes(
    path: str | os.PathLike[str],
    cancel_callback=None,
) -> int:
    root = Path(path).expanduser()
    if not root.exists():
        return 0

    total = 0
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0

    for dirpath, dirnames, filenames in os.walk(root):
        if cancel_callback is not None and cancel_callback():
            return total
        dirnames[:] = [name for name in dirnames if not Path(dirpath, name).is_symlink()]
        for filename in filenames:
            if cancel_callback is not None and cancel_callback():
                return total
            file_path = Path(dirpath, filename)
            if file_path.is_symlink():
                continue
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _looks_like_xplane_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    custom = path / "Custom Scenery"
    return custom.is_dir()


def _parse_install_txt(path: Path) -> Path | None:
    if not path.is_file():
        return None

    try:
        raw = path.read_text(errors="ignore")
    except OSError:
        return None

    candidates: list[str] = []
    for line in raw.splitlines():
        text = line.strip().strip('"').strip("'")
        if not text:
            continue
        if "=" in text:
            text = text.split("=", 1)[1].strip().strip('"').strip("'")
        candidates.append(text)

    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if expanded.is_dir() and _looks_like_xplane_root(expanded):
            return expanded.resolve(strict=False)

    parent = path.parent
    if _looks_like_xplane_root(parent):
        return parent.resolve(strict=False)
    return None


def _common_xplane_roots() -> list[Path]:
    home = Path.home()
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "x-plane_install.txt",
        home / "Library" / "Preferences" / "x-plane_install.txt",
        home / ".x-plane_install.txt",
        Path("/Applications/X-Plane 12"),
        Path("/Applications/X-Plane 11"),
        home / "Applications" / "X-Plane 12",
        home / "Applications" / "X-Plane 11",
        home / "X-Plane 12",
        home / "X-Plane 11",
        home / "Steam" / "steamapps" / "common" / "X-Plane 12",
        home / "Steam" / "steamapps" / "common" / "X-Plane 11",
        home / ".steam" / "steam" / "steamapps" / "common" / "X-Plane 12",
        home / ".steam" / "steam" / "steamapps" / "common" / "X-Plane 11",
        home / ".local" / "share" / "Steam" / "steamapps" / "common" / "X-Plane 12",
        home / ".local" / "share" / "Steam" / "steamapps" / "common" / "X-Plane 11",
        Path("C:/X-Plane 12"),
        Path("C:/X-Plane 11"),
        Path("C:/Program Files/X-Plane 12"),
        Path("C:/Program Files/X-Plane 11"),
        Path("C:/SteamLibrary/steamapps/common/X-Plane 12"),
        Path("C:/SteamLibrary/steamapps/common/X-Plane 11"),
        Path("D:/SteamLibrary/steamapps/common/X-Plane 12"),
        Path("D:/SteamLibrary/steamapps/common/X-Plane 11"),
        Path("C:/Program Files (x86)/Steam/steamapps/common/X-Plane 12"),
        Path("C:/Program Files (x86)/Steam/steamapps/common/X-Plane 11"),
        home / "Library" / "Application Support" / "Steam" / "steamapps" / "common" / "X-Plane 12",
        home / "Library" / "Application Support" / "Steam" / "steamapps" / "common" / "X-Plane 11",
    ]
    return roots


def detect_xplane_installation(search_roots: Iterable[str | os.PathLike[str]] | None = None) -> Path | None:
    roots: list[Path] = [Path(root).expanduser() for root in _common_xplane_roots()]
    if search_roots:
        for root in search_roots:
            roots.append(Path(root).expanduser())

    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)

        if root.is_file() and root.name.lower() == "x-plane_install.txt":
            candidate = _parse_install_txt(root)
            if candidate:
                return candidate

        if root.is_dir():
            if _looks_like_xplane_root(root):
                return root.resolve(strict=False)
            install_txt = root / "x-plane_install.txt"
            candidate = _parse_install_txt(install_txt)
            if candidate:
                return candidate

    if search_roots:
        for root in search_roots:
            root_path = Path(root).expanduser()
            if not root_path.exists() or not root_path.is_dir():
                continue
            for install_txt in root_path.rglob("x-plane_install.txt"):
                candidate = _parse_install_txt(install_txt)
                if candidate:
                    return candidate
    return None


def _normalize_scenery_choice(choice: Any) -> SceneryChoice:
    if isinstance(choice, SceneryChoice):
        return choice
    if isinstance(choice, dict):
        return SceneryChoice(
            region_id=str(choice.get("region_id") or choice.get("id") or choice.get("value") or ""),
            title=str(choice.get("title") or choice.get("name") or choice.get("label") or choice.get("region_id") or choice.get("id") or ""),
            selected=bool(choice.get("selected", False)),
            installed=bool(choice.get("installed", False)),
            description=str(choice.get("description") or ""),
            size_bytes=int(choice.get("size_bytes") or choice.get("size") or 0),
        )
    return SceneryChoice(
        region_id=str(getattr(choice, "region_id", getattr(choice, "id", getattr(choice, "value", "")))),
        title=str(getattr(choice, "title", getattr(choice, "name", getattr(choice, "label", getattr(choice, "region_id", ""))))),
        selected=bool(getattr(choice, "selected", False)),
        installed=bool(getattr(choice, "installed", False)),
        description=str(getattr(choice, "description", "")),
        size_bytes=int(getattr(choice, "size_bytes", getattr(choice, "size", 0)) or 0),
    )


def _path_value(values: Any, key: str, default: str = "") -> str:
    if isinstance(values, dict):
        return str(values.get(key, default) or "")
    if hasattr(values, key):
        return str(getattr(values, key) or "")
    paths = getattr(values, "paths", None)
    if paths is not None and hasattr(paths, key):
        return str(getattr(paths, key) or "")
    return default


def _config_input_from(values: Any) -> ConfigurationInput:
    webui_port = _path_value(values, "webui_port", "5847")
    xplane_udp_port = _path_value(values, "xplane_udp_port", "49000")
    return ConfigurationInput(
        xplane_path=_path_value(values, "xplane_path"),
        scenery_path=_path_value(values, "scenery_path"),
        cache_dir=_path_value(values, "cache_dir"),
        long_term_cache_dir=_path_value(values, "long_term_cache_dir"),
        download_dir=_path_value(values, "download_dir"),
        webui_port=webui_port or "5847",
        xplane_udp_port=xplane_udp_port or "49000",
    )


def _check_xplane(values: Any, search_roots: Iterable[str | os.PathLike[str]] | None = None) -> ReadinessCheck:
    configured = _path_value(values, "xplane_path").strip()
    detected = detect_xplane_installation(search_roots)
    candidate = Path(configured).expanduser() if configured else detected

    if not candidate:
        return ReadinessCheck(
            id="setup-xplane",
            title="X-Plane install",
            status=ReadinessStatus.PENDING,
            message="Choose an X-Plane install folder.",
            fix_action="Pick the folder that contains Custom Scenery.",
            last_checked=_now(),
        )

    config_input = _config_input_from(values)
    if not configured and detected:
        config_input = ConfigurationInput(
            **{
                **config_input.__dict__,
                "xplane_path": str(detected),
            }
        )

    issues = validate_configuration(
        config_input,
        require_installed_scenery=False,
    )
    errors = [issue.message for issue in issues if issue.field == "xplane_path"]
    if errors:
        return ReadinessCheck(
            id="setup-xplane",
            title="X-Plane install",
            status=ReadinessStatus.ERROR,
            message=errors[0],
            fix_action="Select the X-Plane folder that contains Custom Scenery.",
            last_checked=_now(),
            details={"detected": str(detected) if detected else ""},
        )

    return ReadinessCheck(
        id="setup-xplane",
        title="X-Plane install",
        status=ReadinessStatus.SUCCESS,
        message=f"Found X-Plane at {candidate}",
        fix_action="",
        last_checked=_now(),
        details={"detected": str(detected) if detected else str(candidate)},
    )


def _check_paths(values: Any) -> ReadinessCheck:
    relevant_fields = {"scenery_path", "cache_dir", "download_dir"}
    blank_fields = [field for field in relevant_fields if not _path_value(values, field).strip()]
    if blank_fields:
        return ReadinessCheck(
            id="setup-storage",
            title="Storage paths",
            status=ReadinessStatus.PENDING,
            message="Choose scenery, cache, and download folders.",
            fix_action="Select writable folders for scenery, cache, and downloads.",
            last_checked=_now(),
            details={"blank_fields": blank_fields},
        )

    issues = validate_configuration(
        _config_input_from(values),
        require_installed_scenery=False,
    )
    errors = [issue for issue in issues if issue.field in relevant_fields and issue.severity.value == "error"]
    warnings = [issue for issue in issues if issue.field in relevant_fields and issue.severity.value == "warning"]

    if errors:
        issue = errors[0]
        return ReadinessCheck(
            id="setup-storage",
            title="Storage paths",
            status=ReadinessStatus.ERROR,
            message=issue.message,
            fix_action="Choose writable folders with enough disk space.",
            last_checked=_now(),
            details={"issues": [item.message for item in errors + warnings]},
        )
    if warnings:
        issue = warnings[0]
        return ReadinessCheck(
            id="setup-storage",
            title="Storage paths",
            status=ReadinessStatus.WARNING,
            message=issue.message,
            fix_action="Review the storage paths before continuing.",
            last_checked=_now(),
            details={"issues": [item.message for item in warnings]},
        )
    try:
        safety_margin_gb = float(
            _path_value(values, "storage_safety_margin_gb", "2")
        )
    except ValueError:
        safety_margin_gb = 2.0
    safety_margin = max(0, int(safety_margin_gb * 1024 ** 3))
    low_space = [
        field for field in relevant_fields
        if free_space_bytes(_path_value(values, field)) < safety_margin
    ]
    if low_space:
        return ReadinessCheck(
            id="setup-storage",
            title="Storage paths",
            status=ReadinessStatus.WARNING,
            message=(
                "Free space is below the configured "
                f"{format_bytes(safety_margin)} reserve."
            ),
            fix_action="Choose a drive with more free space.",
            last_checked=_now(),
            details={"low_space_fields": low_space},
        )
    return ReadinessCheck(
        id="setup-storage",
        title="Storage paths",
        status=ReadinessStatus.SUCCESS,
        message="Storage paths are writable.",
        fix_action="",
        last_checked=_now(),
    )


def _mac_dependency_state() -> ReadinessCheck:
    candidates = [
        shutil.which("mount_macfuse"),
        shutil.which("mount_fusefs"),
        shutil.which("fuse-t"),
        "/usr/local/bin/fuse-t",
        "/Library/Filesystems/macfuse.fs/Contents/Resources/mount_macfuse",
        "/Library/Filesystems/macfuse.fs/Contents/Resources/mount_fusefs",
        "/Library/Filesystems/osxfuse.fs",
        "/Library/Filesystems/macfuse.fs",
        ctypes.util.find_library("osxfuse"),
        ctypes.util.find_library("macfuse"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return ReadinessCheck(
                id="setup-dependencies",
                title="FUSE dependencies",
                status=ReadinessStatus.SUCCESS,
                message=f"Found macFUSE/FUSE-T support at {candidate}.",
                fix_action="",
                last_checked=_now(),
                details={"backend": candidate},
            )
    return ReadinessCheck(
        id="setup-dependencies",
        title="FUSE dependencies",
        status=ReadinessStatus.ERROR,
        message="macFUSE or FUSE-T was not detected.",
        fix_action="Install macFUSE or FUSE-T and reopen the wizard.",
        last_checked=_now(),
    )


def _linux_dependency_state() -> ReadinessCheck:
    fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
    fuse_dev = Path("/dev/fuse")
    fuse_accessible = (
        fuse_dev.exists()
        and os.access(fuse_dev, os.R_OK | os.W_OK)
    )
    if fusermount and fuse_accessible:
        return ReadinessCheck(
            id="setup-dependencies",
            title="FUSE dependencies",
            status=ReadinessStatus.SUCCESS,
            message=f"Found {Path(fusermount).name} and /dev/fuse.",
            fix_action="",
            last_checked=_now(),
            details={"fusermount": fusermount, "dev_fuse": str(fuse_dev)},
        )
    missing = []
    if not fusermount:
        missing.append("fusermount3/fusermount")
    if not fuse_accessible:
        missing.append("/dev/fuse read/write access")
    return ReadinessCheck(
        id="setup-dependencies",
        title="FUSE dependencies",
        status=ReadinessStatus.ERROR,
        message="Missing Linux FUSE support: " + ", ".join(missing) + ".",
        fix_action="Install FUSE support and ensure /dev/fuse is available.",
        last_checked=_now(),
        details={"missing": missing},
    )


def _windows_dependency_state() -> ReadinessCheck:
    try:
        try:
            from autoortho import winsetup
        except ImportError:
            import winsetup
    except Exception as exc:
        return ReadinessCheck(
            id="setup-dependencies",
            title="FUSE dependencies",
            status=ReadinessStatus.ERROR,
            message=f"Unable to load Windows FUSE helpers: {exc}",
            fix_action="Install WinFSP or Dokan and reopen the wizard.",
            last_checked=_now(),
        )

    mode, libpath = winsetup.find_win_libs()
    if mode and libpath:
        return ReadinessCheck(
            id="setup-dependencies",
            title="FUSE dependencies",
            status=ReadinessStatus.SUCCESS,
            message=f"Detected {mode} at {libpath}.",
            fix_action="",
            last_checked=_now(),
            details={"mode": mode, "library": libpath},
        )
    return ReadinessCheck(
        id="setup-dependencies",
        title="FUSE dependencies",
        status=ReadinessStatus.ERROR,
        message="WinFSP or Dokan was not detected.",
        fix_action="Install WinFSP or Dokan and reopen the wizard.",
        last_checked=_now(),
    )


def _check_dependencies() -> ReadinessCheck:
    system_name = platform.system()
    if system_name == "Windows":
        return _windows_dependency_state()
    if system_name == "Darwin":
        return _mac_dependency_state()
    return _linux_dependency_state()


def _check_scenery(values: Any, scenery_choices: Iterable[Any]) -> tuple[ReadinessCheck, tuple[str, ...], bool]:
    choices = tuple(_normalize_scenery_choice(choice) for choice in scenery_choices)
    selected_region_ids = tuple(choice.region_id for choice in choices if choice.selected)
    scenery_path = _path_value(values, "scenery_path")
    installed_scenery_present = has_installed_scenery(scenery_path)

    if selected_region_ids:
        selected_size = sum(
            choice.size_bytes for choice in choices
            if choice.selected and not choice.installed
        )
        try:
            margin_gb = float(
                _path_value(values, "storage_safety_margin_gb", "2")
            )
        except ValueError:
            margin_gb = 2.0
        temporary, final = package_storage_requirements(
            selected_size,
            safety_margin_gb=margin_gb,
        )
        if (
            free_space_bytes(_path_value(values, "download_dir")) < temporary
            or free_space_bytes(_path_value(values, "scenery_path")) < final
        ):
            return (
                ReadinessCheck(
                    id="setup-scenery",
                    title="Scenery",
                    status=ReadinessStatus.ERROR,
                    message="Selected regions need more free disk space.",
                    fix_action="Select fewer regions or choose larger drives.",
                    last_checked=_now(),
                    details={
                        "temporary_required": temporary,
                        "final_required": final,
                    },
                ),
                selected_region_ids,
                installed_scenery_present,
            )
        return (
            ReadinessCheck(
                id="setup-scenery",
                title="Scenery",
                status=ReadinessStatus.SUCCESS,
                message=(
                    f"{len(selected_region_ids)} region(s) selected for installation."
                    if not installed_scenery_present
                    else (
                        "Existing scenery found and "
                        f"{len(selected_region_ids)} additional region(s) selected."
                    )
                ),
                fix_action="",
                last_checked=_now(),
                details={
                    "installed": installed_scenery_present,
                    "selected": selected_region_ids,
                },
            ),
            selected_region_ids,
            installed_scenery_present,
        )

    if installed_scenery_present:
        return (
            ReadinessCheck(
                id="setup-scenery",
                title="Scenery",
                status=ReadinessStatus.SUCCESS,
                message="Existing AutoOrtho scenery was found.",
                fix_action="",
                last_checked=_now(),
                details={"installed": True, "selected": selected_region_ids},
            ),
            selected_region_ids,
            True,
        )

    return (
        ReadinessCheck(
            id="setup-scenery",
            title="Scenery",
            status=ReadinessStatus.WARNING,
            message="No scenery is selected yet.",
            fix_action="Select at least one region or point to an existing install.",
            last_checked=_now(),
            details={"installed": False, "selected": selected_region_ids},
        ),
        selected_region_ids,
        False,
    )


def build_readiness(values: Any, scenery_choices: Iterable[Any] = (), search_roots: Iterable[str | os.PathLike[str]] | None = None) -> SetupReadiness:
    xplane = _check_xplane(values, search_roots=search_roots)
    storage = _check_paths(values)
    dependencies = _check_dependencies()
    scenery, selected_region_ids, installed_scenery_present = _check_scenery(values, scenery_choices)
    can_finish = (
        not xplane.is_blocking
        and not storage.is_blocking
        and not dependencies.is_blocking
        and not scenery.is_blocking
        and (installed_scenery_present or bool(selected_region_ids))
    )
    return SetupReadiness(
        checks=(xplane, storage, dependencies, scenery),
        installed_scenery_present=installed_scenery_present,
        selected_region_ids=selected_region_ids,
        can_finish=can_finish,
    )


def infer_setup_complete(
    values: Any,
    *,
    search_roots: Iterable[str | os.PathLike[str]] | None = None,
) -> bool:
    """Infer whether an existing user can skip first-run setup."""
    xplane = _check_xplane(values, search_roots=search_roots)
    storage = _check_paths(values)
    dependencies = _check_dependencies()
    return xplane.is_ready and storage.is_ready and dependencies.is_ready


def package_storage_requirements(
    download_size: int,
    safety_margin_gb: float = 2.0,
) -> tuple[int, int]:
    """Return conservative temporary and final-space requirements."""
    size = max(0, int(download_size))
    safety_margin = max(
        int(max(0.0, safety_margin_gb) * 1024 ** 3),
        int(size * 0.10),
    )
    temporary = size + safety_margin
    final = int(size * 1.5) + safety_margin
    return temporary, final

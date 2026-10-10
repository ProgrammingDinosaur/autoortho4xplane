"""Structured validation for values entered in the configuration UI."""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from typing import Iterable


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    severity: ValidationSeverity
    message: str


@dataclass(frozen=True)
class ConfigurationInput:
    xplane_path: str
    scenery_path: str
    cache_dir: str
    long_term_cache_dir: str
    download_dir: str
    webui_port: str
    xplane_udp_port: str


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _validate_directory(
    field: str,
    label: str,
    value: str,
    *,
    required: bool = True,
    must_exist: bool = False,
) -> list[ValidationIssue]:
    path_text = str(value or "").strip()
    if not path_text:
        if required:
            return [
                ValidationIssue(
                    field,
                    ValidationSeverity.ERROR,
                    f"{label} is required.",
                )
            ]
        return []

    path = Path(os.path.expanduser(path_text))
    if path.exists():
        if not path.is_dir():
            return [
                ValidationIssue(
                    field,
                    ValidationSeverity.ERROR,
                    f"{label} must be a directory.",
                )
            ]
        if not os.access(path, os.W_OK):
            return [
                ValidationIssue(
                    field,
                    ValidationSeverity.ERROR,
                    f"{label} is not writable.",
                )
            ]
        return []

    if must_exist:
        return [
            ValidationIssue(
                field,
                ValidationSeverity.ERROR,
                f"{label} does not exist.",
            )
        ]

    parent = _nearest_existing_parent(path)
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        return [
            ValidationIssue(
                field,
                ValidationSeverity.ERROR,
                f"{label} cannot be created at this location.",
            )
        ]
    return []


def _validate_port(field: str, label: str, value: str) -> list[ValidationIssue]:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return [
            ValidationIssue(
                field,
                ValidationSeverity.ERROR,
                f"{label} must be a whole number.",
            )
        ]

    if not 1024 <= port <= 65535:
        return [
            ValidationIssue(
                field,
                ValidationSeverity.ERROR,
                f"{label} must be between 1024 and 65535.",
            )
        ]
    return []


def has_installed_scenery(
    scenery_path: str,
    scenery_mounts: Iterable[dict] = (),
) -> bool:
    root = Path(os.path.expanduser(str(scenery_path or ""))) / "z_autoortho" / "scenery"
    try:
        return root.is_dir() and any(item.is_dir() for item in root.iterdir())
    except OSError:
        return False


def validate_configuration(
    values: ConfigurationInput,
    *,
    scenery_mounts: Iterable[dict] = (),
    require_installed_scenery: bool = True,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(
        _validate_directory(
            "xplane_path",
            "X-Plane install folder",
            values.xplane_path,
            must_exist=True,
        )
    )

    xplane_path = Path(os.path.expanduser(str(values.xplane_path or "")))
    if xplane_path.is_dir() and not (xplane_path / "Custom Scenery").is_dir():
        issues.append(
            ValidationIssue(
                "xplane_path",
                ValidationSeverity.ERROR,
                "X-Plane install folder must contain a Custom Scenery directory.",
            )
        )

    issues.extend(
        _validate_directory(
            "scenery_path",
            "Scenery install folder",
            values.scenery_path,
        )
    )
    issues.extend(
        _validate_directory(
            "cache_dir",
            "Image cache folder",
            values.cache_dir,
        )
    )
    issues.extend(
        _validate_directory(
            "long_term_cache_dir",
            "Long-term cache folder",
            values.long_term_cache_dir,
            required=False,
        )
    )
    issues.extend(
        _validate_directory(
            "download_dir",
            "Temporary download folder",
            values.download_dir,
        )
    )
    issues.extend(_validate_port("webui_port", "Web UI port", values.webui_port))
    issues.extend(
        _validate_port(
            "xplane_udp_port",
            "X-Plane UDP port",
            values.xplane_udp_port,
        )
    )

    if require_installed_scenery and not has_installed_scenery(
        values.scenery_path,
        scenery_mounts,
    ):
        issues.append(
            ValidationIssue(
                "scenery",
                ValidationSeverity.ERROR,
                "Install at least one scenery region before starting streaming.",
            )
        )

    selected_scenery_path = Path(
        os.path.expanduser(str(values.scenery_path or ""))
    ).resolve(strict=False)
    for mount in scenery_mounts:
        root_value = mount.get("root")
        if not root_value:
            continue
        root = Path(os.path.expanduser(str(root_value))).resolve(strict=False)
        try:
            root.relative_to(selected_scenery_path)
        except ValueError:
            continue
        try:
            has_dsf = any(root.glob("Earth nav data/*/*.dsf"))
        except OSError:
            has_dsf = False
        if not has_dsf:
            issues.append(
                ValidationIssue(
                    "scenery",
                    ValidationSeverity.WARNING,
                    f"Scenery folder appears incomplete: {root}",
                )
            )

    return issues

"""Which release file belongs to which kind of install.

The release workflows publish, per version:

* ``autoortho_win_<v>.zip``: Windows folder build (kind ``zip``)
* ``AutoOrtho_win_<v>.exe``: Windows NSIS installer (kind ``nsis``)
* ``AutoOrtho_mac_<v>.zip``: macOS app bundle (kind ``app``)
* ``autoortho_linux_<v>_<codename>.tar.gz``: Linux, one per Ubuntu release
  it was built on (kind ``tar``, variant ``<codename>``)

``<v>`` is the tag with unsafe characters replaced (Makefile SAFE_VERSION).
The manifest script and the client fallback both classify files here, so the
two can never disagree.
"""

import re
from dataclasses import dataclass

KIND_ZIP = "zip"
KIND_NSIS = "nsis"
KIND_APP = "app"
KIND_TAR = "tar"

_PATTERNS = (
    (re.compile(r"^autoortho_win_(?P<version>.+)\.zip$"), "windows", KIND_ZIP, "x86_64"),
    (re.compile(r"^AutoOrtho_win_(?P<version>.+)\.exe$"), "windows", KIND_NSIS, "x86_64"),
    (re.compile(r"^AutoOrtho_mac_(?P<version>.+)\.zip$"), "macos", KIND_APP, "arm64"),
    (
        re.compile(
            r"^autoortho_linux_(?P<version>.+)_(?P<variant>[A-Za-z0-9]+)\.tar\.gz$"
        ),
        "linux",
        KIND_TAR,
        "x86_64",
    ),
)


@dataclass(frozen=True)
class AssetSpec:
    name: str
    platform: str
    kind: str
    arch: str
    variant: str
    version_text: str


def safe_version(text):
    """The Makefile's SAFE_VERSION: anything outside [A-Za-z0-9._-] -> '-'."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(text))


def classify_asset(name):
    """The :class:`AssetSpec` for a release file name, or None."""
    for pattern, platform, kind, arch in _PATTERNS:
        match = pattern.match(name)
        if match:
            groups = match.groupdict()
            return AssetSpec(
                name=name,
                platform=platform,
                kind=kind,
                arch=arch,
                variant=groups.get("variant") or "",
                version_text=groups["version"],
            )
    return None

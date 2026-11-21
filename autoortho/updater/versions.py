"""Version parsing shared by the update check and the updater."""

import re

from packaging.version import InvalidVersion, Version

# Any version-like text, for example "AutoOrtho 3.4.0-rc-1" (lenient).
_EMBEDDED_VERSION = re.compile(
    r"\d+(?:\.\d+){1,3}(?:[-._]rc[-._]?\d+)?",
    re.IGNORECASE,
)
# Exactly a release tag as the release workflows create them: x.y.z or
# x.y.z-rc-N (an optional leading "v" is accepted).
_RELEASE_TAG = re.compile(
    r"^v?(?P<base>\d+\.\d+\.\d+)(?:-rc-(?P<rc>\d+))?$",
    re.IGNORECASE,
)


def parse_version(text):
    """Find and parse a version anywhere in ``text``; None when there is none.

    Lenient on purpose: it is what decides whether to show the update banner,
    and has always accepted text such as "v3.4.0" or "3.4.0-rc-1".
    """
    if not text:
        return None
    match = _EMBEDDED_VERSION.search(str(text))
    if not match:
        return None
    normalized = re.sub(
        r"[-._]rc[-._]?(\d+)",
        r"rc\1",
        match.group(0),
        flags=re.IGNORECASE,
    )
    try:
        return Version(normalized)
    except InvalidVersion:
        return None


def parse_release_version(text):
    """Parse a release tag exactly (x.y.z or x.y.z-rc-N); None otherwise.

    The updater only installs releases and only runs from release builds, so
    "unstable", date-based development versions and other tags (for example
    the scenery "tile-index" release) are rejected here.
    """
    if not text:
        return None
    match = _RELEASE_TAG.match(str(text).strip())
    if not match:
        return None
    normalized = match.group("base")
    if match.group("rc") is not None:
        normalized += f"rc{int(match.group('rc'))}"
    return Version(normalized)


def is_release_tag(text):
    return parse_release_version(text) is not None

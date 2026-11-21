"""The release manifest: one entry per release file, with its checksum.

CI writes ``update-manifest.json`` after all builds finish
(``scripts/make_update_manifest.py``)::

    {
      "schema": 1,
      "tag": "3.5.0",
      "min_client_api": 1,
      "assets": [
        {"name": "autoortho_win_3.5.0.zip", "platform": "windows",
         "kind": "zip", "arch": "x86_64", "variant": "",
         "size": 105262114, "sha256": "c777..."}
      ]
    }

``min_client_api`` lets a future release tell older updaters it needs a
manual install (for example after a packaging change they cannot handle):
an updater whose :data:`CLIENT_API` is lower only offers the release page.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass

from .assets import classify_asset

MANIFEST_NAME = "update-manifest.json"
SIGNATURE_NAME = "update-manifest.json.sig"
SCHEMA = 1
CLIENT_API = 1
MAX_MANIFEST_BYTES = 256 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    platform: str
    kind: str
    arch: str
    variant: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    tag: str
    min_client_api: int
    entries: tuple

    def entry(self, name):
        for item in self.entries:
            if item.name == name:
                return item
        return None


def _text(item, key, required=True):
    value = item.get(key, "")
    if not isinstance(value, str) or (required and not value):
        raise ManifestError(f"Manifest field {key!r} is missing or invalid.")
    return value


def parse_manifest(data):
    """Parse and validate manifest bytes; ManifestError when malformed."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("The manifest is too large.")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"The manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError("The manifest is not a JSON object.")
    if document.get("schema") != SCHEMA:
        raise ManifestError(
            f"Unsupported manifest schema {document.get('schema')!r}."
        )
    tag = _text(document, "tag")
    min_api = document.get("min_client_api", 1)
    if not isinstance(min_api, int) or isinstance(min_api, bool) or min_api < 1:
        raise ManifestError("Manifest field 'min_client_api' is invalid.")
    assets = document.get("assets")
    if not isinstance(assets, list):
        raise ManifestError("Manifest field 'assets' is missing.")
    entries = []
    for item in assets:
        if not isinstance(item, dict):
            raise ManifestError("A manifest asset is not an object.")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ManifestError("A manifest asset has an invalid size.")
        sha256 = _text(item, "sha256").lower()
        if not _SHA256.match(sha256):
            raise ManifestError("A manifest asset has an invalid sha256.")
        entries.append(
            ManifestEntry(
                name=_text(item, "name"),
                platform=_text(item, "platform"),
                kind=_text(item, "kind"),
                arch=_text(item, "arch", required=False),
                variant=_text(item, "variant", required=False),
                size=size,
                sha256=sha256,
            )
        )
    return Manifest(tag=tag, min_client_api=min_api, entries=tuple(entries))


def file_sha256(path, chunk_size=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(tag, paths, min_client_api=1):
    """A manifest document for the release files at ``paths``.

    Files the updater does not know are skipped; two files with one name
    are an error.
    """
    entries = {}
    for path in paths:
        name = os.path.basename(path)
        spec = classify_asset(name)
        if spec is None:
            continue
        if name in entries:
            raise ManifestError(f"Duplicate release file {name}.")
        entries[name] = {
            "name": name,
            "platform": spec.platform,
            "kind": spec.kind,
            "arch": spec.arch,
            "variant": spec.variant,
            "size": os.path.getsize(path),
            "sha256": file_sha256(path),
        }
    if not entries:
        raise ManifestError("No release files found for the manifest.")
    return {
        "schema": SCHEMA,
        "tag": str(tag),
        "min_client_api": int(min_client_api),
        "assets": [entries[name] for name in sorted(entries)],
    }


def dump_manifest(document):
    """Stable bytes for signing."""
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")

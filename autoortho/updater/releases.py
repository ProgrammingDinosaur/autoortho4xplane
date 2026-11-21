"""Find the newest release on GitHub and the file for this install."""

import logging
import os
import re
from dataclasses import dataclass, field

import requests

from .assets import KIND_NSIS, KIND_TAR, classify_asset
from .manifest import (
    CLIENT_API,
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    SIGNATURE_NAME,
    ManifestError,
    parse_manifest,
)
from .signing import signing_required, verify_manifest
from .versions import parse_release_version

log = logging.getLogger(__name__)

DEFAULT_REPO = "ProgrammingDinosaur/autoortho4xplane"
API_ROOT = "https://api.github.com"
USER_AGENT = "autoortho4xplane-updater"

CHANNEL_STABLE = "stable"
CHANNEL_RC = "rc"
CHANNELS = (CHANNEL_STABLE, CHANNEL_RC)

_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DIGEST = re.compile(r"^sha256:(?P<hex>[0-9a-fA-F]{64})$")


class ReleaseError(Exception):
    pass


def repository(environ=None):
    """``owner/name`` to update from. AO_UPDATE_REPO overrides it (testing)."""
    environ = os.environ if environ is None else environ
    value = str(environ.get("AO_UPDATE_REPO") or "").strip()
    if value and _REPO_NAME.match(value):
        return value
    return DEFAULT_REPO


def releases_page(repo=None):
    return f"https://github.com/{repo or repository()}/releases"


def normalize_channel(value):
    text = str(value or "").strip().lower()
    if text in ("rc", "release candidate", "release candidates", "beta"):
        return CHANNEL_RC
    return CHANNEL_STABLE


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    url: str
    sha256: str = ""  # from GitHub's own "digest" field, when present


@dataclass(frozen=True)
class Release:
    tag: str
    version: object
    title: str
    notes: str
    html_url: str
    prerelease: bool
    assets: tuple = field(default_factory=tuple)

    def asset(self, name):
        for item in self.assets:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True)
class SelectedAsset:
    name: str
    url: str
    size: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class UpdateOffer:
    """A newer release, and whether this install can apply it by itself."""

    release: Release
    asset: SelectedAsset | None
    reason: str = ""  # why it cannot self-update; "" when it can

    @property
    def version_text(self):
        return self.release.tag

    @property
    def can_install(self):
        return self.asset is not None and not self.reason


def _parse_release(data):
    tag = str(data.get("tag_name") or "")
    version = parse_release_version(tag)
    if version is None or data.get("draft"):
        return None
    assets = []
    for item in data.get("assets") or ():
        if not isinstance(item, dict):
            continue
        digest = _DIGEST.match(str(item.get("digest") or ""))
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append(
            ReleaseAsset(
                name=str(item.get("name") or ""),
                size=size,
                url=str(item.get("browser_download_url") or ""),
                sha256=digest.group("hex").lower() if digest else "",
            )
        )
    return Release(
        tag=tag,
        version=version,
        title=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        html_url=str(data.get("html_url") or releases_page()),
        prerelease=bool(data.get("prerelease")),
        assets=tuple(assets),
    )


class GitHubReleases:
    def __init__(self, session=None, repo=None, timeout=10):
        self.session = session or requests.Session()
        self.repo = repo or repository()
        self.timeout = timeout

    def _get(self, url, **kwargs):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ReleaseError(f"Could not reach GitHub: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ReleaseError(
                f"GitHub answered {response.status_code} for {url}."
            )
        return response

    def latest(self, channel=CHANNEL_STABLE):
        """The newest release for ``channel`` or None when there is none."""
        if normalize_channel(channel) == CHANNEL_STABLE:
            response = self._get(f"{API_ROOT}/repos/{self.repo}/releases/latest")
            if response is None:
                return None
            release = _parse_release(response.json())
            if release is None or release.version.is_prerelease:
                return None
            return release
        response = self._get(
            f"{API_ROOT}/repos/{self.repo}/releases",
            params={"per_page": 30},
        )
        if response is None:
            return None
        candidates = [
            release
            for release in (_parse_release(item) for item in response.json())
            if release is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda release: release.version)

    def fetch_small(self, url, limit=MAX_MANIFEST_BYTES):
        """A small release file (manifest, signature) as bytes."""
        try:
            response = self.session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ReleaseError(f"Could not download {url}: {exc}") from exc
        with response:
            if response.status_code >= 400:
                raise ReleaseError(
                    f"GitHub answered {response.status_code} for {url}."
                )
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise ReleaseError(f"{url} is larger than expected.")
        return bytes(data)


def _wanted(install):
    """(platform, kind, variant) of the release file for ``install``."""
    variant = install.linux_variant if install.kind == KIND_TAR else ""
    return install.platform, install.kind, variant


def select_asset(release, install, client):
    """(SelectedAsset or None, reason) for ``install`` from ``release``.

    Uses the manifest when the release has one (and requires a valid
    signature once public keys are configured); otherwise matches file names
    and uses GitHub's per-file SHA-256.
    """
    platform, kind, variant = _wanted(install)
    manifest_asset = release.asset(MANIFEST_NAME)

    if manifest_asset is not None:
        try:
            data = client.fetch_small(manifest_asset.url)
            manifest = parse_manifest(data)
        except (ReleaseError, ManifestError) as exc:
            return None, f"The release manifest could not be read: {exc}"
        if manifest.tag != release.tag:
            return None, "The release manifest belongs to another release."
        if signing_required():
            signature_asset = release.asset(SIGNATURE_NAME)
            if signature_asset is None:
                return None, "The release manifest is not signed."
            try:
                signature = client.fetch_small(signature_asset.url, limit=4096)
            except ReleaseError as exc:
                return None, f"The manifest signature could not be read: {exc}"
            if not verify_manifest(data, signature.decode("ascii", "replace")):
                return None, "The release manifest signature is not valid."
        if manifest.min_client_api > CLIENT_API:
            return None, (
                "This release needs a manual install; download it from the "
                "release page."
            )
        for entry in manifest.entries:
            if (entry.platform, entry.kind) != (platform, kind):
                continue
            if variant and entry.variant != variant:
                continue
            published = release.asset(entry.name)
            if published is None or not published.url:
                continue
            if published.size and published.size != entry.size:
                return None, (
                    f"{entry.name} on GitHub does not match the release "
                    "manifest."
                )
            return (
                SelectedAsset(
                    name=entry.name,
                    url=published.url,
                    size=entry.size,
                    sha256=entry.sha256,
                    kind=entry.kind,
                ),
                "",
            )
        return None, _missing_reason(platform, kind, variant)

    if signing_required():
        return None, "The release has no signed manifest."

    for item in release.assets:
        spec = classify_asset(item.name)
        if spec is None or (spec.platform, spec.kind) != (platform, kind):
            continue
        if variant and spec.variant != variant:
            continue
        if not item.sha256 or not item.size or not item.url:
            return None, (
                f"GitHub did not publish a checksum for {item.name}, so it "
                "cannot be verified."
            )
        return (
            SelectedAsset(
                name=item.name,
                url=item.url,
                size=item.size,
                sha256=item.sha256,
                kind=spec.kind,
            ),
            "",
        )
    return None, _missing_reason(platform, kind, variant)


def _missing_reason(platform, kind, variant):
    if kind == KIND_NSIS:
        what = "the Windows installer"
    elif variant:
        what = f"the {platform} ({variant}) build"
    else:
        what = f"the {platform} {kind} build"
    return f"This release does not include {what}."


def check_for_update(install, channel=CHANNEL_STABLE, current_version=None, client=None):
    """An :class:`UpdateOffer` for a newer release, or None when up to date.

    ``current_version`` is a packaging Version (defaults to the install's);
    callers that only want the banner can pass a leniently parsed one.
    """
    client = client or GitHubReleases()
    release = client.latest(channel)
    if release is None:
        return None
    current = current_version if current_version is not None else install.version
    if current is None or release.version <= current:
        return None
    if not install.can_self_update:
        return UpdateOffer(release=release, asset=None, reason=install.unsupported_reason)
    asset, reason = select_asset(release, install, client)
    return UpdateOffer(release=release, asset=asset, reason=reason)

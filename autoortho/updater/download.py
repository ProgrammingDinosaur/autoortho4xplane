"""Download a release file, resuming when possible, and verify it."""

import hashlib
import logging
import os
from pathlib import Path

import requests

from .releases import USER_AGENT

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 << 20


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


def _hash_file(path, digest):
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)


def _verified(path, size, sha256):
    try:
        if path.stat().st_size != size:
            return False
    except OSError:
        return False
    digest = hashlib.sha256()
    _hash_file(path, digest)
    return digest.hexdigest() == sha256


def download(
    url,
    destination,
    size,
    sha256,
    session=None,
    progress=None,
    cancelled=None,
    timeout=(15, 60),
):
    """Download ``url`` to ``destination`` and verify size and SHA-256.

    Data goes to ``<destination>.part`` first; an interrupted download
    resumes from there when the server honours the range request. Returns
    the destination path. ``progress(done, total)`` is called as data
    arrives; ``cancelled()`` returning True raises DownloadCancelled.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sha256 = str(sha256).lower()
    if _verified(destination, size, sha256):
        if progress:
            progress(size, size)
        return destination
    if destination.exists():
        destination.unlink()

    part = destination.with_name(destination.name + ".part")
    session = session or requests.Session()
    digest = hashlib.sha256()
    offset = 0
    if part.exists():
        offset = part.stat().st_size
        if 0 < offset < size:
            _hash_file(part, digest)
        else:
            part.unlink()
            offset = 0

    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        response = session.get(url, headers=headers, stream=True, timeout=timeout)
    except requests.RequestException as exc:
        raise DownloadError(f"Could not start the download: {exc}") from exc

    with response:
        if offset and response.status_code == 206:
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {offset}-"):
                raise DownloadError("The server resumed at the wrong position.")
            mode = "ab"
        elif response.status_code == 200:
            # The server ignored the range: start over.
            offset = 0
            digest = hashlib.sha256()
            mode = "wb"
        else:
            raise DownloadError(
                f"The download failed (HTTP {response.status_code})."
            )

        done = offset
        if progress:
            progress(done, size)
        try:
            with open(part, mode) as handle:
                for chunk in response.iter_content(CHUNK_SIZE):
                    if cancelled is not None and cancelled():
                        raise DownloadCancelled()
                    if not chunk:
                        continue
                    done += len(chunk)
                    if done > size:
                        raise DownloadError(
                            "The download is larger than the release says."
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                    if progress:
                        progress(done, size)
        except requests.RequestException as exc:
            # Keep the partial file: the next attempt resumes from it.
            raise DownloadError(f"The download was interrupted: {exc}") from exc

    if done != size:
        raise DownloadError(
            f"The download stopped early ({done} of {size} bytes)."
        )
    if digest.hexdigest() != sha256:
        try:
            part.unlink()
        except OSError:
            pass
        raise DownloadError(
            "The downloaded file failed its checksum and was deleted."
        )
    os.replace(part, destination)
    return destination

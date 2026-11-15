"""Connection test: how fast imagery downloads, and how many at once it takes.

Ported from OrthoStreamXP's ``ConnectionTest`` (shared with permission). An
imagery chunk is small (about 16 KB), so one download spends most of its time
waiting for the server; the line is full only with many at once. The test
downloads real chunks from the chosen provider in rounds of 16, 32, 64, 128
and 256 at once (new chunks each round, near random points in big cities so
no cache answers them) until more at once stops helping or the server starts
refusing. A round's speed is that of its steady middle, from the arrival of
its 10th to its 90th percent of chunks. The suggestion is the fewest at once
that reach 90 % of the best speed with under 1 % failed.

It uses the same kind of client as AutoOrtho's download broker (httpx over
HTTP/2), so the figures match what streaming gets. A run downloads about
30 MB. The result is kept in ``~/.autoortho-data/connection.json``: the
guards compare it with what a target needs, and it warm-starts the download
concurrency at the next start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

if __package__:
    from autoortho import provider_urls
else:
    import provider_urls

log = logging.getLogger(__name__)

STEPS = (16, 32, 64, 128, 256)
ZOOM = 17
TEXTURE_SPAN = 16
RESULT_PATH = os.path.join(
    os.path.expanduser("~"), ".autoortho-data", "connection.json"
)

# Big cities with imagery from every provider; US ones for US-only providers.
PLACES = (
    (51.11, 17.03), (52.23, 21.01), (50.06, 19.94), (52.52, 13.40),
    (48.86, 2.35), (40.42, -3.70), (41.90, 12.50), (51.50, -0.12),
    (41.88, -87.63), (32.78, -96.80), (-33.87, 151.20), (35.68, 139.69),
)
US_PLACES = (
    (41.88, -87.63), (32.78, -96.80), (34.05, -118.24), (40.71, -74.01),
    (29.76, -95.37), (33.45, -112.07), (39.74, -104.99),
)


@dataclass(frozen=True)
class Round:
    at_once: int
    chunks: int
    failed: int
    bytes: int
    seconds: float
    mean_chunk_seconds: float
    mbits: float

    @property
    def failed_share(self) -> float:
        return self.failed / self.chunks if self.chunks else 0.0


@dataclass(frozen=True)
class Result:
    when_utc: str
    provider: str
    rounds: List[Round] = field(default_factory=list)
    suggested: int = 0

    @property
    def mbits(self) -> float:
        """Best speed of a round with few failures, in Mbit/s."""
        return max((r.mbits for r in usable(self.rounds)), default=0.0)

    @property
    def chunk_seconds(self) -> float:
        for r in self.rounds:
            if r.at_once == self.suggested:
                return r.mean_chunk_seconds
        return 0.0

    @property
    def summary(self) -> str:
        if self.suggested <= 0:
            return (
                f"The test could not download from {self.provider}. Check "
                "the internet connection, then try again."
            )
        chosen = next(r for r in self.rounds if r.at_once == self.suggested)
        share = chosen.mbits / self.mbits if self.mbits else 1.0
        return (
            f"About {self.mbits:.0f} Mbit/s from {self.provider}; a chunk "
            f"takes {self.chunk_seconds:.2f} s. {self.suggested} downloads "
            f"at once reach {share:.0%} of that."
        )

    def age_days(self, now: Optional[datetime] = None) -> float:
        try:
            when = datetime.fromisoformat(self.when_utc)
        except ValueError:
            return math.inf
        now = now or datetime.now(timezone.utc)
        return (now - when).total_seconds() / 86400

    def to_dict(self) -> dict:
        return {
            "when_utc": self.when_utc,
            "provider": self.provider,
            "suggested": self.suggested,
            "rounds": [asdict(r) for r in self.rounds],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Result":
        return cls(
            str(data.get("when_utc", "")),
            str(data.get("provider", "")),
            [Round(**r) for r in data.get("rounds", [])],
            int(data.get("suggested", 0)),
        )


def usable(rounds):
    """Rounds that count: under 5 % of their chunks failed."""
    return [r for r in rounds if r.chunks > 0 and r.failed_share < 0.05]


def enough(rounds) -> bool:
    """Whether more at once would not help: the server refused (5 % or more
    failed), or the last round was not 5 % faster than the best before it."""
    if not rounds:
        return False
    last = rounds[-1]
    if last.failed_share >= 0.05:
        return True
    before = max((r.mbits for r in usable(rounds[:-1])), default=0.0)
    return before > 0 and last.mbits < before * 1.05


def suggest(rounds) -> int:
    """Fewest at once reaching 90 % of the best speed with under 1 % failed."""
    best = max((r.mbits for r in usable(rounds)), default=0.0)
    if best <= 0:
        return 0
    fitting = [
        r.at_once for r in rounds
        if r.failed_share < 0.01 and r.mbits >= best * 0.9
    ]
    if fitting:
        return min(fitting)
    return max(usable(rounds), key=lambda r: r.mbits).at_once


def steady_mbits(arrived) -> float:
    """Speed between the 10th and 90th percent of arrivals ``(t, bytes)``."""
    if len(arrived) < 10:
        return 0.0
    ordered = sorted(arrived)
    first = len(ordered) // 10
    last = len(ordered) - 1 - len(ordered) // 10
    seconds = ordered[last][0] - ordered[first][0]
    total = sum(size for _t, size in ordered[first + 1:last + 1])
    return total * 8 / seconds / 1e6 if seconds > 0 else 0.0


def provider_for(maptype: str) -> str:
    """The provider to test for a map type setting (tile default: Bing)."""
    kind = str(maptype or "").upper()
    return kind if kind in provider_urls.TESTABLE else "BI"


def _lat_lon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def sample_chunks(provider: str, rng=None):
    """Chunks never asked for twice: 16 x 16 blocks near random points."""
    rng = rng or random.Random()
    places = US_PLACES if provider in provider_urls.US_ONLY else PLACES
    block = 0
    while True:
        lat, lon = places[block % len(places)]
        x0, y0 = _lat_lon_to_tile(
            lat + (rng.random() - 0.5) * 0.2,
            lon + (rng.random() - 0.5) * 0.3,
            ZOOM,
        )
        for y in range(TEXTURE_SPAN):
            for x in range(TEXTURE_SPAN):
                yield x0 + x, y0 + y
        block += 1


async def _round(client, provider, chunks, at_once, count, cancel):
    batch = []
    for tile in chunks:
        batch.append(tile)
        if len(batch) >= count:
            break
    gate = asyncio.Semaphore(at_once)
    arrived = []
    stats = {"bytes": 0, "failed": 0, "time": 0.0}
    clock = time.monotonic

    async def one(x, y):
        async with gate:
            if cancel is not None and cancel.is_set():
                stats["failed"] += 1
                return
            started = clock()
            url, headers = provider_urls.tile_url(
                provider, x, y, ZOOM, server_num=(x + y) % 4
            )
            try:
                response = await client.get(url, headers=headers)
                body = response.content
                if response.status_code >= 400 or not body:
                    raise IOError(f"HTTP {response.status_code}")
                stats["bytes"] += len(body)
                arrived.append((clock(), len(body)))
            except Exception:
                stats["failed"] += 1
            finally:
                stats["time"] += clock() - started

    began = clock()
    await asyncio.gather(*(one(x, y) for x, y in batch))
    seconds = clock() - began
    return Round(
        at_once,
        len(batch),
        stats["failed"],
        stats["bytes"],
        seconds,
        stats["time"] / len(batch) if batch else 0.0,
        steady_mbits(arrived),
    )


async def _run_async(provider, progress, cancel, client_factory, max_connections):
    if client_factory is None:
        import httpx

        def client_factory():
            return httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(10.0, connect=8.0),
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_connections,
                ),
            )

    chunks = sample_chunks(provider)
    rounds = []
    async with client_factory() as client:
        # Open the connections first, so round one measures downloads.
        await _round(client, provider, chunks, 16, 16, cancel)
        for at_once in STEPS:
            if cancel is not None and cancel.is_set():
                break
            if progress:
                progress(f"Downloading {at_once} at once…")
            result = await _round(
                client, provider, chunks, at_once, max(64, 4 * at_once), cancel
            )
            rounds.append(result)
            log.info(
                "Connection test, %d at once: %.1f Mbit/s, a chunk %.2f s, "
                "%d of %d failed",
                at_once, result.mbits, result.mean_chunk_seconds,
                result.failed, result.chunks,
            )
            if enough(rounds):
                break
    return rounds


def run(
    maptype: str = "BI",
    progress: Optional[Callable[[str], None]] = None,
    cancel: Optional[threading.Event] = None,
    *,
    client_factory=None,
    max_connections: int = 160,
    save: bool = True,
) -> Result:
    """Run the test (blocking; call it off the UI thread)."""
    provider = provider_for(maptype)
    if progress:
        progress("Opening the connection…")
    rounds = asyncio.run(
        _run_async(provider, progress, cancel, client_factory, max_connections)
    )
    result = Result(
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        provider,
        rounds,
        suggest(rounds),
    )
    log.info("Connection test: %s", result.summary)
    if save and not (cancel is not None and cancel.is_set()):
        save_result(result)
    return result


def save_result(result: Result, path: Optional[str] = None) -> None:
    path = path or RESULT_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not save the connection test to %s: %s", path, exc)


def load_result(path: Optional[str] = None) -> Optional[Result]:
    path = path or RESULT_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return Result.from_dict(json.load(handle))
    except (OSError, ValueError, TypeError, KeyError):
        return None

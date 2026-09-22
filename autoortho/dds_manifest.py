"""Immutable mipmap-zero build provenance and row sealing."""

from __future__ import annotations

import base64
import hashlib
import threading
from collections import Counter
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Callable, Iterable, Mapping


class MipmapSource(IntEnum):
    UNKNOWN = 0
    TARGET_PROVIDER = 1
    LOWER_ZL_CACHE = 2
    LOWER_MIPMAP_MEMORY = 3
    CASCADE_NETWORK_FALLBACK = 4
    MISSING_COLOR = 5
    PROVIDER_TARGET_UNKNOWN_QUALITY = 6

    # Compatibility name retained for diagnostics and older callers. The
    # source proves the requested provider/zoom path, not ground resolution.
    EXACT_TARGET = TARGET_PROVIDER


_FALLBACK_SOURCES = frozenset(
    (
        MipmapSource.LOWER_ZL_CACHE,
        MipmapSource.LOWER_MIPMAP_MEMORY,
        MipmapSource.CASCADE_NETWORK_FALLBACK,
    )
)


@dataclass(frozen=True, slots=True)
class RowBuildManifest:
    tile_id: str
    target_zoom: int
    mipmap: int
    row_index: int
    build_generation: int
    sources: bytes
    compressed_length: int
    compressed_sha256: bytes

    def __post_init__(self) -> None:
        if not self.tile_id:
            raise ValueError("manifest tile_id must not be empty")
        if self.target_zoom < 0 or self.mipmap < 0:
            raise ValueError("manifest zoom and mipmap must be non-negative")
        if self.row_index < 0 or self.build_generation < 0:
            raise ValueError("manifest row and generation must be non-negative")
        if not self.sources:
            raise ValueError("manifest sources must not be empty")
        for value in self.sources:
            try:
                MipmapSource(value)
            except ValueError as exc:
                raise ValueError(f"invalid mipmap source value: {value}") from exc
        if self.compressed_length <= 0:
            raise ValueError("manifest compressed_length must be positive")
        if len(self.compressed_sha256) != hashlib.sha256().digest_size:
            raise ValueError("manifest checksum must be a SHA-256 digest")

    @classmethod
    def create(
        cls,
        *,
        tile_id: str,
        target_zoom: int,
        mipmap: int,
        row_index: int,
        build_generation: int,
        sources: bytes | bytearray | Iterable[int | MipmapSource],
        compressed_data: bytes,
    ) -> "RowBuildManifest":
        data = bytes(compressed_data)
        return cls(
            tile_id=str(tile_id),
            target_zoom=int(target_zoom),
            mipmap=int(mipmap),
            row_index=int(row_index),
            build_generation=int(build_generation),
            sources=bytes(int(source) for source in sources),
            compressed_length=len(data),
            compressed_sha256=hashlib.sha256(data).digest(),
        )

    @property
    def is_exact(self) -> bool:
        return all(
            source == int(MipmapSource.TARGET_PROVIDER)
            for source in self.sources
        )

    @property
    def fallback_indices(self) -> tuple[int, ...]:
        return tuple(
            index
            for index, value in enumerate(self.sources)
            if MipmapSource(value) in _FALLBACK_SOURCES
        )

    @property
    def missing_indices(self) -> tuple[int, ...]:
        return tuple(
            index
            for index, value in enumerate(self.sources)
            if value == int(MipmapSource.MISSING_COLOR)
        )

    @property
    def source_counts(self) -> Mapping[MipmapSource, int]:
        return dict(Counter(MipmapSource(value) for value in self.sources))

    def validate_data(self, data: bytes) -> bool:
        compressed = bytes(data)
        return (
            len(compressed) == self.compressed_length
            and hashlib.sha256(compressed).digest() == self.compressed_sha256
        )

    def to_dict(self) -> dict:
        return {
            "generation": self.build_generation,
            "sources": base64.b64encode(self.sources).decode("ascii"),
            "compressed_length": self.compressed_length,
            "compressed_sha256": self.compressed_sha256.hex(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping,
        *,
        tile_id: str,
        target_zoom: int,
        mipmap: int,
        row_index: int,
    ) -> "RowBuildManifest":
        if not isinstance(value, Mapping):
            raise ValueError("row manifest must be an object")
        try:
            sources = base64.b64decode(
                value["sources"],
                validate=True,
            )
            checksum = bytes.fromhex(value["compressed_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid row manifest encoding") from exc
        return cls(
            tile_id=tile_id,
            target_zoom=int(target_zoom),
            mipmap=int(mipmap),
            row_index=int(row_index),
            build_generation=int(value["generation"]),
            sources=sources,
            compressed_length=int(value["compressed_length"]),
            compressed_sha256=checksum,
        )


@dataclass(frozen=True, slots=True)
class CompressedRowResult:
    data: bytes
    manifest: RowBuildManifest

    def __post_init__(self) -> None:
        data = bytes(self.data)
        object.__setattr__(self, "data", data)
        if not self.manifest.validate_data(data):
            raise ValueError("compressed row does not match its manifest")


@dataclass(frozen=True, slots=True)
class ComposedImageResult:
    image: object
    source_manifest: bytes


@dataclass(frozen=True, slots=True)
class DDSBuildResult:
    dds_bytes: bytes
    mipmap_zero_manifest: tuple[RowBuildManifest, ...]


class RowState(str, Enum):
    UNREQUESTED = "unrequested"
    PREFETCHING = "prefetching"
    BUILDING = "building"
    READY_EXACT = "ready_exact"
    READY_DEGRADED = "ready_degraded"
    SERVED_EXACT = "served_exact"
    SERVED_DEGRADED = "served_degraded"
    SERVED_MISSING = "served_missing"
    CLOSED = "closed"


_SERVED_STATES = frozenset(
    (
        RowState.SERVED_EXACT,
        RowState.SERVED_DEGRADED,
        RowState.SERVED_MISSING,
    )
)


class RowStateCoordinator:
    """Atomically install row bytes/manifests and seal their first serve."""

    def __init__(self, total_rows: int):
        self.total_rows = max(1, int(total_rows))
        self._condition = threading.Condition()
        self._states = {
            row: RowState.UNREQUESTED for row in range(self.total_rows)
        }
        self._manifests: dict[int, RowBuildManifest] = {}
        self._generation = 0
        self._closed = False

    def next_generation(self) -> int:
        with self._condition:
            self._generation += 1
            return self._generation

    def mark_building(self, rows: Iterable[int], *, prefetch: bool = False) -> bool:
        state = RowState.PREFETCHING if prefetch else RowState.BUILDING
        with self._condition:
            normalized = self._normalize_rows(rows)
            if self._closed or any(
                self._states[row] in _SERVED_STATES for row in normalized
            ):
                return False
            for row in normalized:
                self._states[row] = state
            return True

    def commit(
        self,
        results: Iterable[CompressedRowResult],
        installer: Callable[[tuple[CompressedRowResult, ...]], None],
    ) -> bool:
        rows = tuple(results)
        if not rows:
            return False
        row_indices = self._validate_results(rows)
        with self._condition:
            if self._closed or any(
                self._states[row] in _SERVED_STATES for row in row_indices
            ):
                return False
            installer(rows)
            for result in rows:
                row = result.manifest.row_index
                self._manifests[row] = result.manifest
                self._states[row] = (
                    RowState.READY_EXACT
                    if result.manifest.is_exact
                    else RowState.READY_DEGRADED
                )
                self._generation = max(
                    self._generation,
                    result.manifest.build_generation,
                )
            self._condition.notify_all()
            return True

    def seal_and_read(
        self,
        rows: Iterable[int],
        reader: Callable[[], bytes],
        missing_result: Callable[[int], CompressedRowResult],
    ) -> tuple[bytes, tuple[RowBuildManifest, ...]]:
        normalized = self._normalize_rows(rows)
        with self._condition:
            if self._closed:
                return reader(), ()
            manifests = []
            for row in normalized:
                manifest = self._manifests.get(row)
                if manifest is None:
                    result = missing_result(row)
                    if result.manifest.row_index != row:
                        raise ValueError("missing row factory returned the wrong row")
                    manifest = result.manifest
                    self._manifests[row] = manifest
                    self._states[row] = RowState.SERVED_MISSING
                elif self._states[row] not in _SERVED_STATES:
                    self._states[row] = (
                        RowState.SERVED_EXACT
                        if manifest.is_exact
                        else RowState.SERVED_DEGRADED
                    )
                manifests.append(manifest)
            return reader(), tuple(manifests)

    def manifest(self, row: int) -> RowBuildManifest | None:
        with self._condition:
            return self._manifests.get(int(row))

    def manifests(self) -> dict[int, RowBuildManifest]:
        with self._condition:
            return dict(self._manifests)

    def state(self, row: int) -> RowState:
        with self._condition:
            return self._states[int(row)]

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for row in self._states:
                self._states[row] = RowState.CLOSED
            self._condition.notify_all()

    def _normalize_rows(self, rows: Iterable[int]) -> tuple[int, ...]:
        normalized = tuple(sorted(set(int(row) for row in rows)))
        if any(row < 0 or row >= self.total_rows for row in normalized):
            raise ValueError("row is outside the coordinator")
        return normalized

    def _validate_results(
        self,
        results: tuple[CompressedRowResult, ...],
    ) -> tuple[int, ...]:
        row_indices = self._normalize_rows(
            result.manifest.row_index for result in results
        )
        if len(row_indices) != len(results):
            raise ValueError("duplicate row results")
        first = results[0].manifest
        for result in results:
            manifest = result.manifest
            if (
                manifest.tile_id != first.tile_id
                or manifest.target_zoom != first.target_zoom
                or manifest.mipmap != first.mipmap
            ):
                raise ValueError("row results do not belong to one mipmap")
        return row_indices


def manifests_to_dict(
    manifests: Mapping[int, RowBuildManifest] | Iterable[RowBuildManifest],
    *,
    target_zoom: int,
) -> dict:
    if isinstance(manifests, Mapping):
        rows = manifests.values()
    else:
        rows = manifests
    return {
        "target_zoom": int(target_zoom),
        "rows": {
            str(manifest.row_index): manifest.to_dict()
            for manifest in sorted(rows, key=lambda item: item.row_index)
        },
    }


def manifests_from_dict(
    value: Mapping,
    *,
    tile_id: str,
    mipmap: int = 0,
) -> dict[int, RowBuildManifest]:
    if not isinstance(value, Mapping):
        raise ValueError("mipmap-zero manifest must be an object")
    target_zoom = int(value["target_zoom"])
    rows = value.get("rows")
    if not isinstance(rows, Mapping):
        raise ValueError("mipmap-zero manifest rows must be an object")
    manifests = {}
    for row, row_value in rows.items():
        if not isinstance(row, str) or not row.isdigit():
            raise ValueError("manifest row keys must be decimal strings")
        row_index = int(row)
        manifests[row_index] = RowBuildManifest.from_dict(
            row_value,
            tile_id=tile_id,
            target_zoom=target_zoom,
            mipmap=mipmap,
            row_index=row_index,
        )
    return manifests

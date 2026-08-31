"""Bounded, quality-aware prefetch coordination primitives.

The coordinator deliberately knows only the small duck-typed surface exposed by
``TileCacher``, ``ChunkGetter`` and ``BackgroundDDSBuilder``.  Producers publish
immutable keys and evidence; this module is the sole owner of tile opening,
chunk materialization and speculative submission.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import queue
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Hashable, Iterable, Optional

log = logging.getLogger(__name__)


class PrefetchSource(str, Enum):
    DSF = "dsf"
    VELOCITY = "velocity"
    SIMBRIEF = "simbrief"
    LIVE = "live"


class CandidateState(str, Enum):
    DISCOVERED = "discovered"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    ADMITTING = "admitting"
    DOWNLOADING = "downloading"
    TARGET_READY = "target_ready"
    BUILD_QUEUED = "build_queued"
    COMPLETE = "complete"
    STALE = "stale"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PrefetchBatchReason(str, Enum):
    PROGRESSED = "progressed"
    DOWNLOADING = "downloading"
    NO_CAPACITY = "no_capacity"
    LIVE_PRESSURE = "live_pressure"
    NO_USEFUL_WORK = "no_useful_work"
    TARGET_COMPLETE = "target_complete"
    STALE = "stale"
    DISABLED = "disabled"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"


class PrefetchSubmitStatus(str, Enum):
    ACCEPTED = "accepted"
    ALREADY_READY = "already_ready"
    ALREADY_QUEUED = "already_queued"
    ALREADY_ACTIVE = "already_active"
    NO_CAPACITY = "no_capacity"
    DISABLED = "disabled"
    CANCELLED = "cancelled"
    STOPPING = "stopping"

    @property
    def owns_work(self) -> bool:
        return self in {
            PrefetchSubmitStatus.ACCEPTED,
            PrefetchSubmitStatus.ALREADY_QUEUED,
            PrefetchSubmitStatus.ALREADY_ACTIVE,
        }


class PrefetchQualityMode(str, Enum):
    RESPONSIVE = "responsive"
    PREFER_TARGET = "prefer_target"
    STRICT_TARGET = "strict_target"


@dataclass(frozen=True, slots=True)
class PrefetchKey:
    scenery_id: str
    tile_row: int
    tile_col: int
    terrain_zoom: int
    target_zoom: int
    resolved_maptype: str


@dataclass(frozen=True, slots=True)
class PrefetchCursor:
    ordering_revision: int = 0
    next_position: int = 0
    target_zoom: int = 0


@dataclass(frozen=True, slots=True)
class PrefetchCapacitySnapshot:
    queue_available: int
    stage_available: int
    deferred_available: int
    live_queued: int = 0
    live_deferred: int = 0
    live_outstanding: int = 0
    background_outstanding: int = 0
    broker_live_pending: int = 0
    broker_background_pending: int = 0
    broker_background_available: int = 0

    @property
    def admission_available(self) -> int:
        values = (
            max(0, int(self.queue_available)),
            max(0, int(self.stage_available)),
            max(0, int(self.broker_background_available)),
        )
        positive = [value for value in values if value > 0]
        if len(positive) != len(values):
            return 0
        return min(positive)

    @property
    def live_pressure(self) -> bool:
        return any(
            (
                self.live_queued,
                self.live_deferred,
                self.live_outstanding,
                self.broker_live_pending,
            )
        )


@dataclass(frozen=True, slots=True)
class PrefetchBatchResult:
    submitted: int
    cache_hits: int
    scanned: int
    complete: bool
    cursor: PrefetchCursor
    reason: PrefetchBatchReason


@dataclass(slots=True)
class PrefetchCandidate:
    key: PrefetchKey
    sources: set[PrefetchSource] = field(default_factory=set)
    generations: dict[PrefetchSource, Hashable] = field(default_factory=dict)
    eta_seconds: float = math.inf
    distance_meters: float = math.inf
    quality_class: int = 5
    source_confidence: float = 0.0
    priority_revision: int = 0
    cursor: PrefetchCursor = field(default_factory=PrefetchCursor)
    coverage: set[int] = field(default_factory=set)
    active_chunks: set[str] = field(default_factory=set)
    state: CandidateState = CandidateState.DISCOVERED
    created_at: float = field(default_factory=time.monotonic)
    last_updated_at: float = field(default_factory=time.monotonic)
    next_attempt_at: float = 0.0
    retry_count: int = 0
    sequence: int = 0
    lease: Optional["PrefetchLease"] = None
    admitting: bool = False
    build_pending: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in {
            CandidateState.COMPLETE,
            CandidateState.STALE,
            CandidateState.FAILED,
            CandidateState.CANCELLED,
        }

    def priority(self, now: Optional[float] = None) -> tuple:
        now = time.monotonic() if now is None else now
        bounded_age = min(300.0, max(0.0, now - self.created_at))
        return (
            int(self.quality_class),
            float(self.eta_seconds),
            float(self.distance_meters),
            -float(self.source_confidence),
            -bounded_age,
            int(self.sequence),
        )


@dataclass(slots=True)
class _IndexedRecord:
    priority: Any
    sequence: int
    revision: int
    payload: Any


class IndexedPriorityQueue:
    """Thread-safe bounded heap whose active entries can be re-keyed.

    Reprioritization inserts a new revision.  Older heap records are tombstones
    and never count against ``maxsize``.
    """

    def __init__(
        self,
        maxsize: int = 0,
        *,
        key: Optional[Callable[[Any], Hashable]] = None,
        priority: Optional[Callable[[Any], Any]] = None,
        on_compact: Optional[Callable[[int], None]] = None,
    ):
        self.maxsize = max(0, int(maxsize))
        self._key = key or (lambda payload: payload)
        self._priority = priority or (lambda payload: 0)
        self._on_compact = on_compact
        self._heap: list[tuple[Any, int, int, Hashable]] = []
        self._entries: dict[Hashable, _IndexedRecord] = {}
        self._sequence = itertools.count()
        self._revision = itertools.count(1)
        self._tombstones = 0
        self._unfinished = 0
        self._condition = threading.Condition(threading.RLock())

    def qsize(self) -> int:
        with self._condition:
            return len(self._entries)

    def empty(self) -> bool:
        return self.qsize() == 0

    def full(self) -> bool:
        with self._condition:
            return bool(self.maxsize and len(self._entries) >= self.maxsize)

    def available(self) -> int:
        with self._condition:
            if not self.maxsize:
                return 2**31 - 1
            return max(0, self.maxsize - len(self._entries))

    @property
    def tombstones(self) -> int:
        with self._condition:
            return self._tombstones

    def put(
        self,
        payload: Any,
        block: bool = True,
        timeout: Optional[float] = None,
        *,
        item_key: Optional[Hashable] = None,
        item_priority: Any = None,
    ) -> bool:
        key = self._key(payload) if item_key is None else item_key
        priority = self._priority(payload) if item_priority is None else item_priority
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while (
                self.maxsize
                and len(self._entries) >= self.maxsize
                and key not in self._entries
            ):
                if not block:
                    raise queue.Full
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Full
                self._condition.wait(remaining)
            existing = self._entries.get(key)
            if existing is not None:
                self._tombstones += 1
            record = _IndexedRecord(
                priority=priority,
                sequence=(
                    existing.sequence if existing is not None else next(self._sequence)
                ),
                revision=next(self._revision),
                payload=payload,
            )
            self._entries[key] = record
            heapq.heappush(
                self._heap,
                (record.priority, record.sequence, record.revision, key),
            )
            self._maybe_compact_locked()
            self._condition.notify_all()
            return existing is None

    def put_nowait(
        self,
        payload: Any,
        *,
        item_key: Optional[Hashable] = None,
        item_priority: Any = None,
    ) -> bool:
        return self.put(
            payload,
            block=False,
            item_key=item_key,
            item_priority=item_priority,
        )

    def reprioritize(
        self,
        key: Hashable,
        priority: Any,
        payload: Any = None,
    ) -> bool:
        with self._condition:
            current = self._entries.get(key)
            if current is None:
                return False
            self._tombstones += 1
            record = _IndexedRecord(
                priority=priority,
                sequence=current.sequence,
                revision=next(self._revision),
                payload=current.payload if payload is None else payload,
            )
            self._entries[key] = record
            heapq.heappush(
                self._heap,
                (record.priority, record.sequence, record.revision, key),
            )
            self._maybe_compact_locked()
            self._condition.notify_all()
            return True

    def get(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
        *,
        predicate: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                selected = self._pop_locked(predicate)
                if selected is not None:
                    self._unfinished += 1
                    self._condition.notify_all()
                    return selected
                if not block:
                    raise queue.Empty
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)

    def get_nowait(
        self,
        *,
        predicate: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        return self.get(block=False, predicate=predicate)

    def peek(self, predicate: Optional[Callable[[Any], bool]] = None) -> Any:
        with self._condition:
            candidates = (
                record
                for record in self._entries.values()
                if predicate is None or predicate(record.payload)
            )
            record = min(
                candidates,
                key=lambda item: (item.priority, item.sequence),
                default=None,
            )
            return None if record is None else record.payload

    def remove(self, key: Hashable) -> Any:
        with self._condition:
            record = self._entries.pop(key, None)
            if record is None:
                return None
            self._tombstones += 1
            self._maybe_compact_locked()
            self._condition.notify_all()
            return record.payload

    def drain(
        self,
        predicate: Optional[Callable[[Any], bool]] = None,
    ) -> list[Any]:
        with self._condition:
            keys = [
                key
                for key, record in self._entries.items()
                if predicate is None or predicate(record.payload)
            ]
            removed = [self._entries.pop(key).payload for key in keys]
            self._tombstones += len(keys)
            self._maybe_compact_locked(force=not self._entries)
            self._condition.notify_all()
            return removed

    def items(self) -> tuple[Any, ...]:
        with self._condition:
            return tuple(record.payload for record in self._entries.values())

    def pop_worst(
        self,
        predicate: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        with self._condition:
            candidates = (
                (key, record)
                for key, record in self._entries.items()
                if predicate is None or predicate(record.payload)
            )
            selected = max(
                candidates,
                key=lambda item: (item[1].priority, item[1].sequence),
                default=None,
            )
            if selected is None:
                return None
            key, record = selected
            self._entries.pop(key, None)
            self._tombstones += 1
            self._maybe_compact_locked()
            self._condition.notify_all()
            return record.payload

    def task_done(self) -> None:
        with self._condition:
            if self._unfinished <= 0:
                raise ValueError("task_done() called too many times")
            self._unfinished -= 1
            if self._unfinished == 0:
                self._condition.notify_all()

    def join(self) -> None:
        with self._condition:
            while self._unfinished:
                self._condition.wait()

    def _pop_locked(
        self,
        predicate: Optional[Callable[[Any], bool]],
    ) -> Any:
        if predicate is not None:
            selected = min(
                (
                    (key, record)
                    for key, record in self._entries.items()
                    if predicate(record.payload)
                ),
                key=lambda item: (item[1].priority, item[1].sequence),
                default=None,
            )
            if selected is None:
                return None
            key, record = selected
            self._entries.pop(key, None)
            self._tombstones += 1
            self._maybe_compact_locked()
            return record.payload

        while self._heap:
            _priority, _sequence, revision, key = heapq.heappop(self._heap)
            record = self._entries.get(key)
            if record is None or record.revision != revision:
                self._tombstones = max(0, self._tombstones - 1)
                continue
            self._entries.pop(key, None)
            return record.payload
        return None

    def _maybe_compact_locked(self, force: bool = False) -> None:
        active = len(self._entries)
        if not force and self._tombstones <= max(32, active):
            return
        old_tombstones = self._tombstones
        self._heap = [
            (record.priority, record.sequence, record.revision, key)
            for key, record in self._entries.items()
        ]
        heapq.heapify(self._heap)
        self._tombstones = 0
        if old_tombstones and self._on_compact is not None:
            self._on_compact(old_tombstones)


class PrefetchLease:
    """Strong ownership for one candidate's tile and materialized chunks."""

    def __init__(
        self,
        candidate: PrefetchCandidate,
        tile: Any,
        release: Callable[[], None],
    ):
        self.candidate = candidate
        self.tile = tile
        self._release = release
        self.chunks: dict[int, Any] = {}
        self.resident_jpeg_bytes = 0
        self._closed = False
        self._lock = threading.Lock()

    def retain(self, index: int, chunk: Any) -> bool:
        with self._lock:
            if self._closed:
                return False
            is_new = index not in self.chunks
            self.chunks[index] = chunk
            return is_new

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.chunks.clear()
            self.tile = None
        self._release()


class CompletionLRU:
    def __init__(self, maxsize: int = 1024, ttl: float = 900.0):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(0.0, float(ttl))
        self._items: OrderedDict[Hashable, float] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, key: Hashable) -> None:
        now = time.monotonic()
        with self._lock:
            self._expire_locked(now)
            self._items[key] = now + self.ttl
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def __contains__(self, key: Hashable) -> bool:
        now = time.monotonic()
        with self._lock:
            self._expire_locked(now)
            return key in self._items

    def _expire_locked(self, now: float) -> None:
        while self._items:
            key, deadline = next(iter(self._items.items()))
            if deadline > now:
                break
            self._items.pop(key, None)


class _PressureGate:
    def __init__(
        self,
        *,
        high_water: float = 0.8,
        low_water: float = 0.4,
        minimum_pause: float = 0.5,
    ):
        self.high_water = high_water
        self.low_water = low_water
        self.minimum_pause = minimum_pause
        self.active = False
        self.entered_at = 0.0
        self.clear_since: Optional[float] = None

    def update(
        self,
        snapshot: PrefetchCapacitySnapshot,
        queue_capacity: int,
    ) -> tuple[bool, bool]:
        now = time.monotonic()
        queue_used = max(0, queue_capacity - snapshot.queue_available)
        ratio = queue_used / max(1, queue_capacity)
        enter = (
            snapshot.live_pressure
            or ratio >= self.high_water
            or snapshot.stage_available <= 0
            or snapshot.broker_background_available <= 0
        )
        changed = False
        if not self.active and enter:
            self.active = True
            self.entered_at = now
            self.clear_since = None
            changed = True
        elif self.active:
            clear = (
                not snapshot.live_pressure
                and ratio <= self.low_water
                and snapshot.stage_available > 0
                and snapshot.broker_background_available > 0
            )
            if not clear:
                self.clear_since = None
            elif self.clear_since is None:
                self.clear_since = now
            elif (
                now - self.entered_at >= self.minimum_pause
                and now - self.clear_since >= self.minimum_pause
            ):
                self.active = False
                self.clear_since = None
                changed = True
        return self.active, changed


class PrefetchCoordinator:
    """Single bounded owner of speculative tile and chunk work."""

    def __init__(
        self,
        *,
        tile_cacher: Any,
        chunk_getter: Any,
        scenery_id: str,
        admission_burst: int = 64,
        max_candidates: int = 256,
        max_tile_leases: int = 96,
        max_materialized_chunks: int = 512,
        max_jpeg_bytes: int = 512 * 1024 * 1024,
        metric: Optional[Callable[[str, int], None]] = None,
        gauge: Optional[Callable[[str, int], None]] = None,
    ):
        self.tile_cacher = tile_cacher
        self.chunk_getter = chunk_getter
        self.scenery_id = str(scenery_id)
        self.admission_burst = max(1, int(admission_burst))
        self.max_candidates = max(1, int(max_candidates))
        self.max_tile_leases = max(1, int(max_tile_leases))
        self.max_materialized_chunks = max(1, int(max_materialized_chunks))
        self.max_jpeg_bytes = max(1, int(max_jpeg_bytes))
        self._metric_cb = metric or (lambda _name, _value=1: None)
        self._gauge_cb = gauge or (lambda _name, _value: None)
        self._sequence = itertools.count()
        self._candidates: dict[PrefetchKey, PrefetchCandidate] = {}
        self._retired: dict[int, PrefetchCandidate] = {}
        self._tile_keys: dict[str, tuple[PrefetchKey, int]] = {}
        self._generation_keys: dict[
            tuple[PrefetchSource, Hashable], dict[PrefetchKey, int]
        ] = {}
        self._live_owned: set[PrefetchKey] = set()
        self._live_tile_keys: dict[str, PrefetchKey] = {}
        self._queue = IndexedPriorityQueue(
            maxsize=self.max_candidates,
            on_compact=lambda count: self._metric(
                "prefetch_queue_heap_compactions"
            ),
        )
        self._completed = CompletionLRU(maxsize=max_candidates * 4, ttl=900.0)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._accepting = True
        self._thread: Optional[threading.Thread] = None
        self._builder: Any = None
        self._pressure = _PressureGate()
        self._materialized_chunks = 0
        self._resident_jpeg_bytes = 0
        self._peak_candidates = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._accepting = True
            self._thread = threading.Thread(
                target=self._run,
                name="PrefetchCoordinator",
                daemon=True,
            )
            self._thread.start()
        self._gauge("prefetch_coordinator_threads", 1)

    def set_builder(self, builder: Any) -> None:
        self._builder = builder
        with self._lock:
            ready = [
                candidate
                for candidate in self._candidates.values()
                if candidate.state == CandidateState.TARGET_READY
            ]
        for candidate in ready:
            self._queue_build(candidate)
        self._wake.set()

    def make_key(
        self,
        row: int,
        col: int,
        terrain_zoom: int,
        target_zoom: int,
        maptype: str,
    ) -> PrefetchKey:
        resolved = self.tile_cacher._resolve_maptype(
            row, col, maptype, terrain_zoom
        )
        return PrefetchKey(
            scenery_id=self.scenery_id,
            tile_row=int(row),
            tile_col=int(col),
            terrain_zoom=int(terrain_zoom),
            target_zoom=int(target_zoom),
            resolved_maptype=str(resolved),
        )

    def publish(
        self,
        key: PrefetchKey,
        *,
        source: PrefetchSource | str,
        generation: Hashable,
        eta_seconds: float = math.inf,
        distance_meters: float = math.inf,
        quality_class: int = 5,
        source_confidence: float = 0.0,
    ) -> bool:
        source = PrefetchSource(source)
        now = time.monotonic()
        with self._lock:
            if not self._accepting or self._stop.is_set():
                return False
            if key in self._live_owned:
                self._metric("prefetch_candidates_coalesced")
                return False
            if key in self._completed:
                self._metric("prefetch_candidates_coalesced")
                return False
            candidate = self._candidates.get(key)
            if candidate is not None and candidate.terminal:
                # The retired object may still own active network/build work.
                # Its callbacks carry object identity, so a fresh generation can
                # safely install a replacement under the same canonical key.
                if self._candidates.get(key) is candidate:
                    self._candidates.pop(key, None)
                candidate = None
            created = candidate is None
            if candidate is None:
                self._make_candidate_room_locked()
                if len(self._candidates) >= self.max_candidates:
                    self._metric("prefetch_candidates_evicted")
                    return False
                candidate = PrefetchCandidate(
                    key=key,
                    sequence=next(self._sequence),
                    cursor=PrefetchCursor(target_zoom=key.target_zoom),
                )
                self._candidates[key] = candidate
                self._metric("prefetch_candidates_discovered")
                self._metric(
                    f"prefetch_candidates_discovered_{source.value}"
                )
                self._peak_candidates = max(
                    self._peak_candidates, len(self._candidates)
                )
            else:
                self._metric("prefetch_candidates_coalesced")

            old_priority = candidate.priority(now)
            candidate.sources.add(source)
            candidate.generations[source] = generation
            candidate.eta_seconds = min(
                candidate.eta_seconds, max(0.0, float(eta_seconds))
            )
            candidate.distance_meters = min(
                candidate.distance_meters, max(0.0, float(distance_meters))
            )
            candidate.quality_class = min(
                candidate.quality_class, max(0, int(quality_class))
            )
            candidate.source_confidence = max(
                candidate.source_confidence, float(source_confidence)
            )
            candidate.last_updated_at = now
            candidate.priority_revision += 1
            self._generation_keys.setdefault(
                (source, generation), {}
            )[key] = candidate.sequence
            priority = candidate.priority(now)
            self._queue.put(
                candidate,
                item_key=key,
                item_priority=priority,
            )
            if not created and priority != old_priority:
                self._metric("prefetch_candidates_reprioritized")
            self._report_locked()
        self._wake.set()
        return created

    def replace_generation(
        self,
        source: PrefetchSource | str,
        generation: Hashable,
        candidates: Iterable[dict[str, Any] | tuple],
    ) -> int:
        source = PrefetchSource(source)
        normalized = []
        keys: set[PrefetchKey] = set()
        for evidence in candidates:
            if isinstance(evidence, dict):
                key = evidence["key"]
                kwargs = {name: value for name, value in evidence.items() if name != "key"}
            else:
                key = evidence[0]
                kwargs = evidence[1] if len(evidence) > 1 else {}
            keys.add(key)
            normalized.append((key, kwargs))

        # Retire obsolete support before admitting the replacement generation,
        # so a full candidate set cannot reject all new work and then empty.
        with self._lock:
            obsolete_generations = [
                token
                for token in self._generation_keys
                if token[0] == source and token[1] != generation
            ]
            for token in obsolete_generations:
                previous = self._generation_keys.pop(token, {})
                for key, sequence in previous.items():
                    if key in keys:
                        continue
                    candidate = self._candidates.get(key)
                    if (
                        candidate is not None
                        and candidate.sequence == sequence
                        and candidate.generations.get(source) == token[1]
                    ):
                        candidate.generations.pop(source, None)
                        candidate.sources.discard(source)
                        if not candidate.sources:
                            self._stale_locked(candidate)
            self._report_locked()

        published = 0
        for key, kwargs in normalized:
            if self.publish(
                key,
                source=source,
                generation=generation,
                **kwargs,
            ):
                published += 1

        with self._lock:
            indexed = {
                key: self._candidates[key].sequence
                for key in keys
                if key in self._candidates
            }
            if indexed:
                self._generation_keys[(source, generation)] = indexed
            else:
                self._generation_keys.pop((source, generation), None)
            self._report_locked()
        self._wake.set()
        return published

    def promote_tile(self, tile: Any) -> bool:
        with self._lock:
            mapped = self._tile_keys.get(getattr(tile, "id", ""))
            key = mapped[0] if mapped is not None else None
            if key is None:
                key = next(
                    (
                        candidate_key
                        for candidate_key in self._candidates
                        if (
                            candidate_key.tile_row
                            == int(getattr(tile, "row", -1))
                            and candidate_key.tile_col
                            == int(getattr(tile, "col", -1))
                            and candidate_key.terrain_zoom
                            == int(getattr(tile, "tilename_zoom", -1))
                            and candidate_key.resolved_maptype
                            == str(getattr(tile, "maptype", ""))
                        )
                    ),
                    None,
                )
            candidate = self._candidates.get(key) if key is not None else None
            if candidate is None or candidate.terminal:
                return False
            candidate.sources.add(PrefetchSource.LIVE)
            candidate.generations[PrefetchSource.LIVE] = "live"
            candidate.quality_class = 0
            candidate.eta_seconds = 0.0
            candidate.distance_meters = 0.0
            candidate.priority_revision += 1
            candidate.last_updated_at = time.monotonic()
            candidate.state = CandidateState.CANCELLED
            self._queue.remove(key)
            self._live_owned.add(key)
            self._live_tile_keys[tile.id] = key
            lease = candidate.lease
            chunks = tuple(lease.chunks.values()) if lease is not None else ()
        for chunk in chunks:
            chunk.priority = 0
            chunk.prefetch = False
        self.chunk_getter.reprioritize_queue()
        builder = self._builder
        if builder is not None and hasattr(builder, "reprioritize"):
            builder.reprioritize(tile, 0)
        # The FUSE path now owns the tile. Drop only the coordinator's lease;
        # queued/active chunks remain strongly held by the live tile and Getter.
        self._release_candidate(candidate)
        self._metric("prefetched_tiles_promoted_live")
        self._wake.set()
        return True

    def release_live_tile(self, tile: Any) -> None:
        with self._lock:
            key = self._live_tile_keys.pop(
                getattr(tile, "id", ""), None
            )
            if key is not None:
                self._live_owned.discard(key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            states: dict[str, int] = {}
            candidates = tuple(self._candidates.values()) + tuple(
                self._retired.values()
            )
            for candidate in candidates:
                states[candidate.state.value] = states.get(candidate.state.value, 0) + 1
            return {
                "candidates": len(candidates),
                "states": states,
                "tile_leases": sum(
                    candidate.lease is not None
                    for candidate in candidates
                ),
                "materialized_chunks": self._materialized_chunks,
                "resident_jpeg_bytes": self._resident_jpeg_bytes,
                "pressure": self._pressure.active,
            }

    def is_known(self, key: PrefetchKey) -> bool:
        with self._lock:
            return (
                key in self._candidates
                or key in self._completed
                or key in self._live_owned
            )

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._stop.is_set() and self._thread is None:
                return
            self._accepting = False
            self._stop.set()
            candidates = list(self._candidates.values()) + list(
                self._retired.values()
            )
            for candidate in candidates:
                if not candidate.terminal:
                    candidate.state = CandidateState.CANCELLED
            self._queue.drain()
            thread, self._thread = self._thread, None
        self._wake.set()
        self.chunk_getter.cancel_prefetch_work("coordinator shutdown")
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        drain_deadline = time.monotonic() + min(
            2.0, max(0.0, timeout)
        )
        while time.monotonic() < drain_deadline:
            with self._lock:
                if not any(
                    candidate.active_chunks for candidate in candidates
                ):
                    break
            self._wake.wait(
                min(0.05, max(0.0, drain_deadline - time.monotonic()))
            )
            self._wake.clear()
        for candidate in candidates:
            self._release_candidate(candidate)
        with self._lock:
            self._candidates.clear()
            self._retired.clear()
            self._tile_keys.clear()
            self._generation_keys.clear()
            self._live_owned.clear()
            self._live_tile_keys.clear()
            self._report_locked()
        self._gauge("prefetch_coordinator_threads", 0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            while not self._stop.is_set():
                candidate = None
                try:
                    candidate = self._next_ready_candidate()
                    if candidate is None:
                        break
                    result = self._admit(candidate)
                    if result.reason in {
                        PrefetchBatchReason.NO_CAPACITY,
                        PrefetchBatchReason.LIVE_PRESSURE,
                        PrefetchBatchReason.DISABLED,
                    }:
                        self._defer_candidate(candidate, result.reason)
                    elif (
                        result.reason == PrefetchBatchReason.PROGRESSED
                        and not candidate.terminal
                    ):
                        self._requeue(candidate)
                    elif (
                        result.reason
                        == PrefetchBatchReason.SHUTTING_DOWN
                        and not candidate.terminal
                    ):
                        self._finish_candidate(
                            candidate, CandidateState.CANCELLED
                        )
                except Exception:
                    log.exception("Prefetch coordinator admission failed")
                    if candidate is not None and not candidate.terminal:
                        try:
                            self._finish_candidate(
                                candidate, CandidateState.FAILED
                            )
                        except Exception:
                            log.exception(
                                "Could not retire failed prefetch candidate"
                            )

    def _next_ready_candidate(self) -> Optional[PrefetchCandidate]:
        now = time.monotonic()
        deferred: list[PrefetchCandidate] = []
        while True:
            try:
                candidate = self._queue.get_nowait()
            except queue.Empty:
                for item in deferred:
                    self._requeue(item)
                return None
            self._queue.task_done()
            if candidate.terminal or candidate.key not in self._candidates:
                continue
            if candidate.next_attempt_at > now:
                deferred.append(candidate)
                continue
            for item in deferred:
                self._requeue(item)
            return candidate

    def _admit(self, candidate: PrefetchCandidate) -> PrefetchBatchResult:
        with self._lock:
            if (
                self._candidates.get(candidate.key) is not candidate
                or candidate.terminal
            ):
                return PrefetchBatchResult(
                    0,
                    0,
                    0,
                    False,
                    candidate.cursor,
                    PrefetchBatchReason.STALE,
                )
            candidate.admitting = True
        try:
            return self._admit_candidate(candidate)
        finally:
            with self._lock:
                candidate.admitting = False
                should_release = (
                    candidate.terminal and not candidate.active_chunks
                )
            if should_release:
                self._release_candidate(candidate)

    def _candidate_is_current(
        self, candidate: PrefetchCandidate
    ) -> bool:
        with self._lock:
            return (
                self._candidates.get(candidate.key) is candidate
                and not candidate.terminal
            )

    def _admit_candidate(
        self, candidate: PrefetchCandidate
    ) -> PrefetchBatchResult:
        cursor = candidate.cursor
        if self._stop.is_set():
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.SHUTTING_DOWN
            )
        snapshot = self.chunk_getter.prefetch_capacity_snapshot()
        self._gauge(
            "prefetch_available_queue_slots",
            snapshot.queue_available,
        )
        self._gauge(
            "prefetch_available_stage_slots",
            snapshot.stage_available,
        )
        self._gauge(
            "prefetch_broker_live_pending",
            snapshot.broker_live_pending,
        )
        self._gauge(
            "prefetch_broker_background_pending",
            snapshot.broker_background_pending,
        )
        if cursor.next_position:
            self._metric("prefetch_cursor_resumes")
        queue_capacity = (
            snapshot.queue_available + self.chunk_getter.queue_depths()["prefetch"]
        )
        pressure, changed = self._pressure.update(snapshot, queue_capacity)
        if changed:
            self._metric(
                "prefetch_pressure_enter"
                if pressure
                else "prefetch_pressure_exit"
            )
        if pressure and snapshot.live_pressure and candidate.quality_class > 1:
            self._metric("prefetch_batches_deferred_live_pressure")
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.LIVE_PRESSURE
            )
        scan_budget = min(
            self.admission_burst,
            snapshot.admission_available,
            max(0, self.max_materialized_chunks - self._materialized_chunks),
            max(
                0,
                (self.max_jpeg_bytes - self._resident_jpeg_bytes)
                // (256 * 1024),
            ),
        )
        if scan_budget <= 0:
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.NO_CAPACITY
            )

        lease = self._ensure_lease(candidate)
        if lease is None:
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.NO_CAPACITY
            )
        if not self._candidate_is_current(candidate):
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.STALE
            )
        tile = lease.tile
        if tile is None or getattr(tile, "_closed", False):
            self._finish_candidate(candidate, CandidateState.FAILED)
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.FAILED
            )

        with self._lock:
            if not self._candidate_is_current(candidate):
                return PrefetchBatchResult(
                    0, 0, 0, False, cursor, PrefetchBatchReason.STALE
                )
            candidate.state = CandidateState.ADMITTING
        grid = tile._get_chunk_grid(tile.max_zoom)
        if tile.max_zoom != candidate.key.target_zoom:
            self._finish_candidate(candidate, CandidateState.STALE)
            return PrefetchBatchResult(
                0,
                0,
                0,
                False,
                cursor,
                PrefetchBatchReason.STALE,
            )
        start = min(cursor.next_position, grid.logical_length)
        end = min(grid.logical_length, start + scan_budget)
        if start >= end:
            return self._settle_scanned_candidate(candidate, grid.logical_length)

        chunks = grid.ensure_range(start, end)
        if not self._candidate_is_current(candidate):
            return PrefetchBatchResult(
                0, 0, 0, False, cursor, PrefetchBatchReason.STALE
            )
        submitted = 0
        cache_hits = 0
        scanned = 0
        next_position = start
        for offset, chunk in enumerate(chunks):
            if not self._candidate_is_current(candidate):
                return PrefetchBatchResult(
                    submitted,
                    cache_hits,
                    scanned,
                    False,
                    candidate.cursor,
                    PrefetchBatchReason.STALE,
                )
            index = start + offset
            scanned += 1
            if lease.retain(index, chunk):
                with self._lock:
                    self._materialized_chunks += 1
            if chunk.ready.is_set():
                next_position = index + 1
                if chunk.data:
                    cache_hits += 1
                    self._record_ready(candidate, index, chunk)
                    continue
                self._finish_candidate(candidate, CandidateState.FAILED)
                return PrefetchBatchResult(
                    submitted,
                    cache_hits,
                    scanned,
                    False,
                    PrefetchCursor(
                        cursor.ordering_revision,
                        next_position,
                        cursor.target_zoom,
                    ),
                    PrefetchBatchReason.FAILED,
                )

            if candidate.quality_class == 0:
                chunk.priority = 0
                chunk.prefetch = False
            else:
                chunk.priority = max(100, 100 + candidate.quality_class)
                chunk.prefetch = True
            status = self.chunk_getter.submit_prefetch(
                chunk,
                timeout=(5, 10),
                max_attempts=8,
            )
            if status == PrefetchSubmitStatus.NO_CAPACITY:
                break
            if status == PrefetchSubmitStatus.ALREADY_READY:
                next_position = index + 1
                if chunk.data:
                    cache_hits += 1
                    self._record_ready(candidate, index, chunk)
                    continue
                self._finish_candidate(candidate, CandidateState.FAILED)
                return PrefetchBatchResult(
                    submitted,
                    cache_hits,
                    scanned,
                    False,
                    candidate.cursor,
                    PrefetchBatchReason.FAILED,
                )
            if status in {
                PrefetchSubmitStatus.DISABLED,
                PrefetchSubmitStatus.STOPPING,
            }:
                reason = (
                    PrefetchBatchReason.SHUTTING_DOWN
                    if status == PrefetchSubmitStatus.STOPPING
                    else PrefetchBatchReason.DISABLED
                )
                return PrefetchBatchResult(
                    submitted,
                    cache_hits,
                    scanned,
                    False,
                    PrefetchCursor(
                        cursor.ordering_revision,
                        next_position,
                        cursor.target_zoom,
                    ),
                    reason,
                )
            if status == PrefetchSubmitStatus.CANCELLED:
                self._finish_candidate(candidate, CandidateState.FAILED)
                return PrefetchBatchResult(
                    submitted,
                    cache_hits,
                    scanned,
                    False,
                    cursor,
                    PrefetchBatchReason.FAILED,
                )
            next_position = index + 1
            if status.owns_work:
                candidate.active_chunks.add(chunk.chunk_id)
                self._subscribe_chunk(candidate, index, chunk)
                if status == PrefetchSubmitStatus.ACCEPTED:
                    submitted += 1

        with self._lock:
            if not self._candidate_is_current(candidate):
                return PrefetchBatchResult(
                    submitted,
                    cache_hits,
                    scanned,
                    False,
                    candidate.cursor,
                    PrefetchBatchReason.STALE,
                )
            candidate.cursor = PrefetchCursor(
                cursor.ordering_revision,
                next_position,
                cursor.target_zoom,
            )
            candidate.state = (
                CandidateState.DOWNLOADING
                if candidate.active_chunks
                else CandidateState.WAITING_FOR_CAPACITY
            )
        self._metric("prefetch_chunks_scanned", scanned)
        self._metric("prefetch_chunks_cache_hits", cache_hits)
        self._metric("prefetch_chunks_admitted", submitted)
        self._report()
        if next_position < end:
            return PrefetchBatchResult(
                submitted,
                cache_hits,
                scanned,
                False,
                candidate.cursor,
                PrefetchBatchReason.NO_CAPACITY,
            )
        if next_position >= grid.logical_length:
            return self._settle_scanned_candidate(candidate, grid.logical_length)
        return PrefetchBatchResult(
            submitted,
            cache_hits,
            scanned,
            False,
            candidate.cursor,
            PrefetchBatchReason.PROGRESSED,
        )

    def _ensure_lease(
        self, candidate: PrefetchCandidate
    ) -> Optional[PrefetchLease]:
        with self._lock:
            if candidate.lease is not None:
                return candidate.lease
            leases = sum(
                item.lease is not None
                for item in (
                    tuple(self._candidates.values())
                    + tuple(self._retired.values())
                )
            )
            if leases >= self.max_tile_leases:
                return None
        key = candidate.key
        tile = self.tile_cacher._open_tile(
            key.tile_row,
            key.tile_col,
            key.resolved_maptype,
            key.terrain_zoom,
        )
        if tile is None:
            self._finish_candidate(candidate, CandidateState.FAILED)
            return None

        def release() -> None:
            close_prefetch = getattr(
                self.tile_cacher,
                "_close_prefetch_tile",
                self.tile_cacher._close_tile,
            )
            close_prefetch(
                key.tile_row,
                key.tile_col,
                key.resolved_maptype,
                key.terrain_zoom,
            )

        lease = PrefetchLease(candidate, tile, release)
        with self._lock:
            if candidate.terminal:
                lease.close()
                return None
            if self._candidates.get(candidate.key) is not candidate:
                lease.close()
                return None
            candidate.lease = lease
            self._tile_keys[tile.id] = (key, candidate.sequence)
            self._report_locked()
        return lease

    def _subscribe_chunk(
        self,
        candidate: PrefetchCandidate,
        index: int,
        chunk: Any,
    ) -> None:
        add_settled_callback = getattr(
            chunk, "add_settled_callback", None
        )
        if add_settled_callback is not None:
            add_settled_callback(
                lambda _chunk, candidate=candidate, index=index, chunk=chunk: (
                    self._on_chunk_ready(candidate, index, chunk)
                )
            )
            return
        ready = getattr(chunk, "ready", None)
        add_callback = getattr(ready, "add_callback", None)
        if add_callback is not None:
            add_callback(
                lambda _event, candidate=candidate, index=index, chunk=chunk: (
                    self._on_chunk_ready(candidate, index, chunk)
                )
            )

    def _on_chunk_ready(
        self,
        candidate: PrefetchCandidate,
        index: int,
        chunk: Any,
    ) -> None:
        release_terminal = False
        should_settle = False
        failed = False
        logical_length = 0
        with self._lock:
            candidate.active_chunks.discard(chunk.chunk_id)
            if candidate.terminal:
                release_terminal = not candidate.active_chunks
            elif chunk.data:
                self._record_ready_locked(candidate, index, chunk)
            elif not getattr(chunk, "cancelled", False):
                failed = True
            if not candidate.terminal and not failed:
                lease = candidate.lease
                tile = lease.tile if lease is not None else None
                if tile is not None:
                    grid = tile.chunks.get(tile.max_zoom)
                    logical_length = getattr(grid, "logical_length", 0)
                    should_settle = (
                        candidate.cursor.next_position >= logical_length
                        and not candidate.active_chunks
                    )
        if release_terminal:
            self._release_candidate(candidate)
            return
        if candidate.terminal:
            return
        if failed:
            self._finish_candidate(candidate, CandidateState.FAILED)
            return
        if should_settle:
            self._settle_scanned_candidate(candidate, logical_length)
        self._wake.set()

    def _record_ready(
        self,
        candidate: PrefetchCandidate,
        index: int,
        chunk: Any,
    ) -> None:
        with self._lock:
            self._record_ready_locked(candidate, index, chunk)

    def _record_ready_locked(
        self,
        candidate: PrefetchCandidate,
        index: int,
        chunk: Any,
    ) -> None:
        if index in candidate.coverage:
            return
        data_size = len(chunk.data or b"")
        candidate.coverage.add(index)
        lease = candidate.lease
        if lease is not None:
            lease.resident_jpeg_bytes += data_size
        self._resident_jpeg_bytes += data_size
        self._metric("prefetch_exact_target_chunks_completed")
        self._report_locked()

    def _settle_scanned_candidate(
        self,
        candidate: PrefetchCandidate,
        logical_length: int,
    ) -> PrefetchBatchResult:
        with self._lock:
            if candidate.terminal:
                return PrefetchBatchResult(
                    0,
                    0,
                    0,
                    False,
                    candidate.cursor,
                    PrefetchBatchReason.STALE,
                )
            if candidate.active_chunks:
                candidate.state = CandidateState.DOWNLOADING
                return PrefetchBatchResult(
                    0,
                    0,
                    0,
                    False,
                    candidate.cursor,
                    PrefetchBatchReason.DOWNLOADING,
                )
            target_complete = len(candidate.coverage) == logical_length
            cursor_complete = (
                candidate.cursor.next_position >= logical_length
            )
            if target_complete:
                candidate.state = CandidateState.TARGET_READY
            else:
                reason = (
                    PrefetchBatchReason.FAILED
                    if cursor_complete
                    else PrefetchBatchReason.NO_USEFUL_WORK
                )
        if not target_complete:
            self._finish_candidate(candidate, CandidateState.FAILED)
            return PrefetchBatchResult(
                0, 0, 0, False, candidate.cursor, reason
            )
        self._metric("prefetch_candidates_target_ready")
        self._queue_build(candidate)
        return PrefetchBatchResult(
            0,
            0,
            0,
            candidate.state == CandidateState.COMPLETE,
            candidate.cursor,
            PrefetchBatchReason.TARGET_COMPLETE,
        )

    def _queue_build(self, candidate: PrefetchCandidate) -> None:
        builder = self._builder
        lease = candidate.lease
        tile = lease.tile if lease is not None else None
        if tile is None:
            self._finish_candidate(candidate, CandidateState.FAILED)
            return
        if builder is False:
            self._finish_candidate(candidate, CandidateState.COMPLETE)
            return
        if builder is None:
            candidate.state = CandidateState.TARGET_READY
            return
        callback = (
            lambda success, candidate=candidate: self._on_build_complete(
                candidate, success
            )
        )
        candidate.state = CandidateState.BUILD_QUEUED
        candidate.build_pending = True
        try:
            try:
                accepted = builder.submit(
                    tile,
                    priority=candidate.priority(),
                    completion_callback=callback,
                    exact_target=True,
                )
            except TypeError:
                try:
                    accepted = builder.submit(
                        tile,
                        priority=candidate.priority(),
                        completion_callback=callback,
                    )
                except TypeError:
                    accepted = builder.submit(
                        tile, priority=candidate.priority()
                    )
                    callback(bool(accepted))
        except Exception:
            candidate.build_pending = False
            log.exception(
                "Predictive DDS submission failed for %s",
                candidate.key,
            )
            self._finish_candidate(candidate, CandidateState.FAILED)
            return
        if accepted:
            return
        candidate.build_pending = False
        self._metric("prefetch_build_admission_deferred")
        # Chunk readiness is emitted only after the cache writer owns immutable
        # source bytes, so exact imagery remains a valid durable completion even
        # when optional predictive DDS work cannot fit its byte budget.
        self._finish_candidate(candidate, CandidateState.COMPLETE)

    def _on_build_complete(
        self, candidate: PrefetchCandidate, success: bool
    ) -> None:
        with self._lock:
            candidate.build_pending = False
            terminal = candidate.terminal
            should_release = (
                terminal
                and not candidate.active_chunks
                and not candidate.admitting
            )
        if should_release:
            self._release_candidate(candidate)
            return
        if terminal:
            return
        if not success:
            self._metric("prefetch_build_failed_exact_sources_retained")
        self._finish_candidate(candidate, CandidateState.COMPLETE)
        self._wake.set()

    def _defer_candidate(
        self,
        candidate: PrefetchCandidate,
        reason: PrefetchBatchReason,
    ) -> None:
        with self._lock:
            if (
                self._candidates.get(candidate.key) is not candidate
                or candidate.terminal
            ):
                return
            candidate.state = CandidateState.WAITING_FOR_CAPACITY
            candidate.retry_count += 1
            base = min(
                5.0, 0.05 * (2 ** min(candidate.retry_count, 7))
            )
            candidate.next_attempt_at = (
                time.monotonic() + base * random.uniform(0.8, 1.2)
            )
            lease = candidate.lease
            tile = lease.tile if lease is not None else None
            grid = (
                tile.chunks.get(tile.max_zoom)
                if tile is not None
                else None
            )
            target_complete = (
                grid is not None
                and len(candidate.coverage)
                == getattr(grid, "logical_length", -1)
            )
            should_drop = (
                not candidate.active_chunks
                and not candidate.admitting
                and not candidate.build_pending
                and not target_complete
            )
        self._metric(f"prefetch_admission_{reason.value}")
        if should_drop:
            self._drop_lease(candidate)
        self._requeue(candidate)

    def _requeue(self, candidate: PrefetchCandidate) -> None:
        with self._lock:
            if candidate.terminal or candidate.key not in self._candidates:
                return
            self._queue.put(
                candidate,
                item_key=candidate.key,
                item_priority=candidate.priority(),
            )
        delay = max(0.0, candidate.next_attempt_at - time.monotonic())
        if delay <= 0:
            self._wake.set()

    def _stale_locked(self, candidate: PrefetchCandidate) -> None:
        if candidate.terminal:
            return
        candidate.state = CandidateState.STALE
        self._queue.remove(candidate.key)
        if self._candidates.get(candidate.key) is candidate:
            self._candidates.pop(candidate.key, None)
        self._metric("prefetch_candidates_stale")
        chunks = (
            tuple(candidate.lease.chunks.values())
            if candidate.lease is not None
            else ()
        )
        cancel_chunks = getattr(self.chunk_getter, "cancel_chunks", None)
        if cancel_chunks is not None:
            cancel_chunks(chunks, "candidate stale")
        else:
            for chunk in chunks:
                if getattr(chunk, "in_queue", False):
                    chunk.cancel()
        if (
            not candidate.active_chunks
            and not candidate.admitting
            and not candidate.build_pending
        ):
            self._release_candidate(candidate)
        else:
            self._retired[candidate.sequence] = candidate

    def _finish_candidate(
        self,
        candidate: PrefetchCandidate,
        state: CandidateState,
    ) -> None:
        with self._lock:
            if candidate.terminal:
                return
            candidate.state = state
            self._queue.remove(candidate.key)
            if state == CandidateState.COMPLETE:
                self._completed.add(candidate.key)
                self._metric("prefetch_candidates_complete")
            should_release = (
                not candidate.active_chunks
                and not candidate.admitting
                and not candidate.build_pending
            )
            if not should_release:
                if self._candidates.get(candidate.key) is candidate:
                    self._candidates.pop(candidate.key, None)
                self._retired[candidate.sequence] = candidate
        if should_release:
            self._release_candidate(candidate)

    def _release_candidate(self, candidate: PrefetchCandidate) -> None:
        lease = self._drop_lease(candidate)
        with self._lock:
            if self._candidates.get(candidate.key) is candidate:
                self._candidates.pop(candidate.key, None)
            self._retired.pop(candidate.sequence, None)
            for source, generation in tuple(
                candidate.generations.items()
            ):
                token = (source, generation)
                indexed = self._generation_keys.get(token)
                if (
                    indexed is not None
                    and indexed.get(candidate.key) == candidate.sequence
                ):
                    indexed.pop(candidate.key, None)
                    if not indexed:
                        self._generation_keys.pop(token, None)
            self._report_locked()

    def _drop_lease(
        self, candidate: PrefetchCandidate
    ) -> Optional[PrefetchLease]:
        with self._lock:
            lease, candidate.lease = candidate.lease, None
            if lease is None:
                return None
            tile_id = getattr(lease.tile, "id", "")
            if self._tile_keys.get(tile_id) == (
                candidate.key,
                candidate.sequence,
            ):
                self._tile_keys.pop(tile_id, None)
            self._materialized_chunks = max(
                0, self._materialized_chunks - len(lease.chunks)
            )
            self._resident_jpeg_bytes = max(
                0, self._resident_jpeg_bytes - lease.resident_jpeg_bytes
            )
            self._report_locked()
        lease.close()
        return lease

    def _make_candidate_room_locked(self) -> None:
        if len(self._candidates) < self.max_candidates:
            return
        candidates = [
            candidate
            for candidate in self._candidates.values()
            if (
                PrefetchSource.LIVE not in candidate.sources
                and not candidate.active_chunks
                and not candidate.admitting
                and candidate.state
                not in {
                    CandidateState.TARGET_READY,
                    CandidateState.BUILD_QUEUED,
                }
            )
        ]
        victim = max(
            candidates,
            key=lambda item: (
                item.distance_meters,
                item.eta_seconds,
                -len(item.coverage),
                -item.sequence,
            ),
            default=None,
        )
        if victim is not None:
            victim.state = CandidateState.STALE
            self._queue.remove(victim.key)
            self._metric("prefetch_candidates_evicted")
            self._release_candidate(victim)

    def _metric(self, name: str, value: int = 1) -> None:
        try:
            self._metric_cb(name, value)
        except Exception:
            pass

    def _gauge(self, name: str, value: int) -> None:
        try:
            self._gauge_cb(name, value)
        except Exception:
            pass

    def _report(self) -> None:
        with self._lock:
            self._report_locked()

    def _report_locked(self) -> None:
        total_candidates = len(self._candidates) + len(self._retired)
        self._gauge("prefetch_candidates_current", total_candidates)
        self._gauge("prefetch_candidates_peak", self._peak_candidates)
        self._gauge(
            "prefetch_candidate_estimated_bytes",
            total_candidates * 512,
        )
        self._gauge(
            "prefetch_background_tiles",
            sum(
                candidate.lease is not None
                for candidate in (
                    tuple(self._candidates.values())
                    + tuple(self._retired.values())
                )
            ),
        )
        self._gauge(
            "prefetch_materialized_chunks", self._materialized_chunks
        )
        self._gauge(
            "prefetch_resident_jpeg_bytes", self._resident_jpeg_bytes
        )
        self._gauge("prefetch_queue_tombstones", self._queue.tombstones)

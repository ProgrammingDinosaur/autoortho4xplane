"""
disk_budget_manager.py - Unified disk space management for AutoOrtho

Provides centralized disk accounting and eviction across cache types:
- DDS cache (.dds + .ddm) - compiled textures
- JPEGs (.jpg) - source tile images

Budget enforcement is soft: writes are never blocked. Instead, when a
category exceeds its allocation, background eviction reclaims space by
deleting the least-recently-accessed entries.
"""

import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


class DiskUsageReport:
    """Snapshot of disk usage across all cache categories."""
    __slots__ = ("dds_bytes", "jpeg_bytes",
                 "total_bytes", "budget_bytes", "scan_time_ms")

    def __init__(self):
        self.dds_bytes = 0
        self.jpeg_bytes = 0
        self.total_bytes = 0
        self.budget_bytes = 0
        self.scan_time_ms = 0.0

    def __repr__(self):
        return (f"DiskUsage(dds={self.dds_bytes/(1024**2):.0f}MB, "
                f"jpeg={self.jpeg_bytes/(1024**2):.0f}MB, "
                f"total={self.total_bytes/(1024**2):.0f}MB / "
                f"{self.budget_bytes/(1024**2):.0f}MB)")


class DiskBudgetManager:
    """
    Unified disk space management for AutoOrtho caches.
    
    Tracks disk usage across DDS cache and JPEG files.
    Enforces per-category budgets through LRU eviction.
    
    Budget allocation (configurable, % of ``total_budget_mb``):
    - DDS cache: 80% (primary persistent storage)
    - JPEGs: 20% (source images retained until DDS is complete)
    
    Thread Safety:
        All public methods are thread-safe. Eviction runs in background
        threads to avoid blocking callers.
    """

    def __init__(self, cache_dir: str, total_budget_mb: int,
                 dds_budget_pct: int = 80,
                 jpeg_budget_pct: int = 20,
                 dds_cache=None):
        """
        Args:
            cache_dir: Base cache directory.
            total_budget_mb: Total disk budget in MB across all categories.
            dds_budget_pct: Percentage allocated to DDS cache (10-90).
            jpeg_budget_pct: Percentage allocated to JPEGs (5-50).
            dds_cache: Optional DynamicDDSCache instance for DDS eviction.
        """
        self._cache_dir = cache_dir
        self._total_budget = total_budget_mb * 1024 * 1024  # bytes

        # Clamp percentages to valid ranges
        dds_budget_pct = max(10, min(90, dds_budget_pct))
        jpeg_budget_pct = max(5, min(50, jpeg_budget_pct))

        # Normalize percentages to sum to 100
        total_pct = dds_budget_pct + jpeg_budget_pct
        self._dds_budget = int(self._total_budget * dds_budget_pct / total_pct)
        self._jpeg_budget = int(self._total_budget * jpeg_budget_pct / total_pct)

        # Current usage tracking (updated by scan and accounting calls)
        self._dds_usage = 0
        self._jpeg_usage = 0

        self._dds_cache = dds_cache  # Reference to DynamicDDSCache for eviction

        self._lock = threading.Lock()
        self._scan_complete = threading.Event()
        self._last_scan_time = 0.0
        self._eviction_in_progress = False

        log.info(f"DiskBudgetManager initialized: total={total_budget_mb}MB "
                 f"(DDS={self._dds_budget/(1024**2):.0f}MB, "
                 f"JPEGs={self._jpeg_budget/(1024**2):.0f}MB)")

    # ------------------------------------------------------------------
    # Accounting (called after writes)
    # ------------------------------------------------------------------

    def account_dds(self, size_bytes: int) -> None:
        """Account for a DDS cache write.
        
        Args:
            size_bytes: Size of the DDS file written (positive for add,
                        negative for removal).
        """
        with self._lock:
            self._dds_usage += size_bytes
            self._dds_usage = max(0, self._dds_usage)

        if self._dds_usage > self._dds_budget:
            self._schedule_eviction("dds")

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def check_and_evict(self) -> None:
        """
        Check all categories and evict if over budget.
        
        Called periodically (e.g., from TileCacher.clean loop) and
        after accounting calls when a budget is exceeded.
        """
        # DDS eviction
        if self._dds_usage > self._dds_budget and self._dds_cache is not None:
            excess = self._dds_usage - int(self._dds_budget * 0.9)
            if excess > 0:
                freed = self._dds_cache.evict_lru(excess)
                with self._lock:
                    self._dds_usage -= freed

    def _schedule_eviction(self, category: str) -> None:
        """Schedule a background eviction check for the given category."""
        with self._lock:
            if self._eviction_in_progress:
                return
            self._eviction_in_progress = True

        def _run():
            try:
                self.check_and_evict()
            finally:
                with self._lock:
                    self._eviction_in_progress = False

        t = threading.Thread(target=_run, daemon=True, name=f"disk_evict_{category}")
        t.start()

    # ------------------------------------------------------------------
    # Disk scanning
    # ------------------------------------------------------------------

    def scan_disk_usage(self) -> DiskUsageReport:
        """
        Scan the cache directory tree and compute actual disk usage.
        
        This is I/O intensive and should be called from a background thread.
        
        Returns:
            DiskUsageReport with per-category byte counts.
        """
        report = DiskUsageReport()
        report.budget_bytes = self._total_budget
        start = time.monotonic()

        try:
            # Scan DDS cache
            dds_dir = os.path.join(self._cache_dir, "dds_cache")
            if os.path.isdir(dds_dir):
                report.dds_bytes = self._scan_dir_size(dds_dir, ".dds")

            # Scan JPEG files
            report.jpeg_bytes = self._scan_jpegs_size()

        except Exception as e:
            log.warning(f"Disk usage scan error: {e}")

        report.total_bytes = report.dds_bytes + report.jpeg_bytes
        report.scan_time_ms = (time.monotonic() - start) * 1000

        # Update tracked usage
        with self._lock:
            self._dds_usage = report.dds_bytes
            self._jpeg_usage = report.jpeg_bytes
            self._last_scan_time = time.time()

        self._scan_complete.set()

        log.info(f"Disk scan complete in {report.scan_time_ms:.0f}ms: {report}")
        return report

    def initial_scan(self) -> None:
        """
        Run initial disk scan and cleanup. Intended for background thread at startup.
        
        Performs:
        1. Full disk usage scan
        2. Budget enforcement (eviction if needed)
        """
        try:
            self.scan_disk_usage()
            self.check_and_evict()
        except Exception as e:
            log.warning(f"Initial disk scan error: {e}")
        finally:
            self._scan_complete.set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def usage_report(self) -> dict:
        """Return current usage statistics."""
        with self._lock:
            return {
                "dds_usage_mb": self._dds_usage / (1024 ** 2),
                "dds_budget_mb": self._dds_budget / (1024 ** 2),
                "jpeg_usage_mb": self._jpeg_usage / (1024 ** 2),
                "jpeg_budget_mb": self._jpeg_budget / (1024 ** 2),
                "total_usage_mb": (self._dds_usage + self._jpeg_usage) / (1024 ** 2),
                "total_budget_mb": self._total_budget / (1024 ** 2),
                "last_scan": self._last_scan_time,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_dir_size(root: str, extension: str) -> int:
        """Sum file sizes under ``root`` matching ``extension``."""
        total = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fname in filenames:
                    if fname.endswith(extension):
                        try:
                            total += os.path.getsize(os.path.join(dirpath, fname))
                        except OSError:
                            pass
        except OSError:
            pass
        return total

    def _scan_jpegs_size(self) -> int:
        """Estimate total size of JPEG files in the cache."""
        total = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(self._cache_dir):
                if "dds_cache" in dirpath:
                    continue
                for fname in filenames:
                    if fname.endswith((".jpg", ".jpeg")):
                        try:
                            total += os.path.getsize(os.path.join(dirpath, fname))
                        except OSError:
                            pass
        except OSError:
            pass
        return total

    @staticmethod
    def _safe_remove(path: str) -> None:
        """Remove a file, ignoring errors."""
        try:
            os.remove(path)
        except OSError:
            pass

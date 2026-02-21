"""
disk_budget_manager.py - Unified disk space management for AutoOrtho

Provides centralized disk accounting and eviction across all cache types:
- Bundles (.aob2) - source JPEGs, most valuable
- DDS cache (.dds + .ddm) - compiled textures, derived from bundles
- Orphan JPEGs - transient files awaiting bundle consolidation

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
    __slots__ = ("bundle_bytes", "dds_bytes", "jpeg_bytes",
                 "total_bytes", "budget_bytes", "scan_time_ms")

    def __init__(self):
        self.bundle_bytes = 0
        self.dds_bytes = 0
        self.jpeg_bytes = 0
        self.total_bytes = 0
        self.budget_bytes = 0
        self.scan_time_ms = 0.0

    def __repr__(self):
        return (f"DiskUsage(bundles={self.bundle_bytes/(1024**2):.0f}MB, "
                f"dds={self.dds_bytes/(1024**2):.0f}MB, "
                f"jpeg={self.jpeg_bytes/(1024**2):.0f}MB, "
                f"total={self.total_bytes/(1024**2):.0f}MB / "
                f"{self.budget_bytes/(1024**2):.0f}MB)")


class DiskBudgetManager:
    """
    Unified disk space management for AutoOrtho caches.
    
    Tracks disk usage across bundles, DDS cache, and orphan JPEGs.
    Enforces per-category budgets through LRU eviction.
    
    Budget allocation (configurable, % of ``total_budget_mb``):
    - Bundles: 55% (most valuable, expensive to rebuild)
    - DDS cache: 40% (derived, can be regenerated from bundles)
    - Orphan JPEGs: 5% (transient, should be consolidated ASAP)
    
    Thread Safety:
        All public methods are thread-safe. Eviction runs in background
        threads to avoid blocking callers.
    """

    def __init__(self, cache_dir: str, total_budget_mb: int,
                 dds_budget_pct: int = 40,
                 bundle_budget_pct: int = 55,
                 jpeg_budget_pct: int = 5,
                 dds_cache=None):
        """
        Args:
            cache_dir: Base cache directory.
            total_budget_mb: Total disk budget in MB across all categories.
            dds_budget_pct: Percentage allocated to DDS cache (10-60).
            bundle_budget_pct: Percentage allocated to bundles (30-80).
            jpeg_budget_pct: Percentage allocated to orphan JPEGs (1-20).
            dds_cache: Optional DynamicDDSCache instance for DDS eviction.
        """
        self._cache_dir = cache_dir
        self._total_budget = total_budget_mb * 1024 * 1024  # bytes

        # Clamp percentages to valid ranges
        dds_budget_pct = max(10, min(60, dds_budget_pct))
        bundle_budget_pct = max(30, min(80, bundle_budget_pct))
        jpeg_budget_pct = max(1, min(20, jpeg_budget_pct))

        # Normalize percentages to sum to 100
        total_pct = dds_budget_pct + bundle_budget_pct + jpeg_budget_pct
        self._dds_budget = int(self._total_budget * dds_budget_pct / total_pct)
        self._bundle_budget = int(self._total_budget * bundle_budget_pct / total_pct)
        self._jpeg_budget = int(self._total_budget * jpeg_budget_pct / total_pct)

        # Current usage tracking (updated by scan and accounting calls)
        self._dds_usage = 0
        self._bundle_usage = 0
        self._jpeg_usage = 0

        self._dds_cache = dds_cache  # Reference to DynamicDDSCache for eviction

        self._lock = threading.Lock()
        self._scan_complete = threading.Event()
        self._last_scan_time = 0.0
        self._eviction_in_progress = False

        log.info(f"DiskBudgetManager initialized: total={total_budget_mb}MB "
                 f"(DDS={self._dds_budget/(1024**2):.0f}MB, "
                 f"bundles={self._bundle_budget/(1024**2):.0f}MB, "
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

    def account_bundle(self, size_bytes: int) -> None:
        """Account for a bundle write.
        
        Args:
            size_bytes: Size of the bundle file written.
        """
        with self._lock:
            self._bundle_usage += size_bytes
            self._bundle_usage = max(0, self._bundle_usage)

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

        # Orphan JPEG cleanup
        if self._jpeg_usage > self._jpeg_budget:
            self.cleanup_orphan_jpegs()

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
            # Scan bundles
            bundles_dir = os.path.join(self._cache_dir, "bundles")
            if os.path.isdir(bundles_dir):
                report.bundle_bytes = self._scan_dir_size(bundles_dir, ".aob2")

            # Scan DDS cache
            dds_dir = os.path.join(self._cache_dir, "dds_cache")
            if os.path.isdir(dds_dir):
                report.dds_bytes = self._scan_dir_size(dds_dir, ".dds")

            # Scan orphan JPEGs (loose .jpg files in cache, not inside bundles dir)
            report.jpeg_bytes = self._scan_orphan_jpegs_size()

        except Exception as e:
            log.warning(f"Disk usage scan error: {e}")

        report.total_bytes = report.bundle_bytes + report.dds_bytes + report.jpeg_bytes
        report.scan_time_ms = (time.monotonic() - start) * 1000

        # Update tracked usage
        with self._lock:
            self._dds_usage = report.dds_bytes
            self._bundle_usage = report.bundle_bytes
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
        2. Orphan JPEG cleanup
        3. Stale DDS cleanup
        4. Budget enforcement (eviction if needed)
        """
        try:
            report = self.scan_disk_usage()

            # Cleanup orphan JPEGs
            self.cleanup_orphan_jpegs()

            # Cleanup stale DDS (no matching bundle)
            stale_count = self.cleanup_stale_dds()
            if stale_count > 0:
                log.info(f"Cleaned up {stale_count} stale DDS entries")

            # Enforce budgets
            self.check_and_evict()

        except Exception as e:
            log.warning(f"Initial disk scan error: {e}")
        finally:
            self._scan_complete.set()

    # ------------------------------------------------------------------
    # Cleanup routines
    # ------------------------------------------------------------------

    def cleanup_orphan_jpegs(self) -> int:
        """
        Clean up orphan JPEG files that have been consolidated into bundles.
        
        Delegates to the existing bundle_consolidator.cleanup_orphan_jpegs().
        
        Returns:
            Number of JPEGs deleted.
        """
        try:
            try:
                from autoortho.aopipeline.bundle_consolidator import cleanup_orphan_jpegs
            except ImportError:
                from aopipeline.bundle_consolidator import cleanup_orphan_jpegs  # type: ignore[no-redef]

            deleted, scanned = cleanup_orphan_jpegs(self._cache_dir)
            if deleted > 0:
                log.info(f"Orphan cleanup: deleted {deleted}/{scanned} JPEGs")
                # Re-estimate JPEG usage
                with self._lock:
                    # Approximate: assume average JPEG is 20KB
                    self._jpeg_usage -= deleted * 20 * 1024
                    self._jpeg_usage = max(0, self._jpeg_usage)
            return deleted
        except Exception as e:
            log.debug(f"Orphan JPEG cleanup error: {e}")
            return 0

    def cleanup_stale_dds(self) -> int:
        """
        Remove DDS cache entries whose source bundles no longer exist.
        
        Scans each .ddm file and checks if the corresponding bundle is present.
        If the bundle was evicted, the DDS is orphaned and should be removed.
        
        Returns:
            Number of stale DDS entries removed.
        """
        dds_dir = os.path.join(self._cache_dir, "dds_cache")
        if not os.path.isdir(dds_dir):
            return 0

        try:
            from autoortho.utils.bundle_paths import get_bundle2_path
        except ImportError:
            from utils.bundle_paths import get_bundle2_path  # type: ignore[no-redef]

        count = 0
        freed = 0

        try:
            for dirpath, _dirnames, filenames in os.walk(dds_dir):
                for fname in filenames:
                    if not fname.endswith(".ddm"):
                        continue

                    ddm_path = os.path.join(dirpath, fname)
                    dds_path = ddm_path[:-4] + ".dds"

                    try:
                        import json
                        with open(ddm_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        # Corrupted metadata - remove both files
                        self._safe_remove(dds_path)
                        self._safe_remove(ddm_path)
                        count += 1
                        continue

                    # Check if bundle exists
                    row = meta.get("tile_row")
                    col = meta.get("tile_col")
                    maptype = meta.get("map", "")
                    zl = meta.get("zl", 0)

                    if row is None or col is None:
                        self._safe_remove(dds_path)
                        self._safe_remove(ddm_path)
                        count += 1
                        continue

                    bundle_path = get_bundle2_path(self._cache_dir, row, col, maptype, zl)
                    if not os.path.exists(bundle_path):
                        # Bundle evicted - DDS is orphaned
                        try:
                            size = os.path.getsize(dds_path)
                            freed += size
                        except OSError:
                            pass
                        self._safe_remove(dds_path)
                        self._safe_remove(ddm_path)
                        count += 1

        except Exception as e:
            log.debug(f"Stale DDS cleanup error: {e}")

        if freed > 0:
            with self._lock:
                self._dds_usage -= freed
                self._dds_usage = max(0, self._dds_usage)

        return count

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
                "bundle_usage_mb": self._bundle_usage / (1024 ** 2),
                "bundle_budget_mb": self._bundle_budget / (1024 ** 2),
                "jpeg_usage_mb": self._jpeg_usage / (1024 ** 2),
                "jpeg_budget_mb": self._jpeg_budget / (1024 ** 2),
                "total_usage_mb": (self._dds_usage + self._bundle_usage + self._jpeg_usage) / (1024 ** 2),
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

    def _scan_orphan_jpegs_size(self) -> int:
        """Estimate total size of orphan JPEG files in the cache."""
        total = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(self._cache_dir):
                # Skip the dds_cache and bundles subdirectories
                if "dds_cache" in dirpath or "bundles" in dirpath:
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

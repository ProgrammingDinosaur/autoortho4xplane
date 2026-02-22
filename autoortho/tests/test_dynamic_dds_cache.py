"""
test_dynamic_dds_cache.py - Unit tests for DynamicDDSCache and DiskBudgetManager

Tests the persistent DDS cache system:
- DynamicDDSCache: load/store/staleness/upgrade_zl/eviction/scan
- DiskBudgetManager: budget enforcement, eviction, cleanup
- Integration: get_bytes cache hit path
"""

import json
import os
import sys
import tempfile
import threading
import time
import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# Mock objects to avoid importing the full autoortho stack
# ============================================================================

class MockDDS:
    """Minimal mock of pydds.DDS for testing."""
    def __init__(self, width, height, dxt_format="BC1"):
        from io import BytesIO
        self.width = width
        self.height = height
        self.mipMapCount = 0
        self.mipmap_list = []
        self.blocksize = 8 if dxt_format == "BC1" else 16
        self.dxt_format = dxt_format
        
        # Build mipmap list (same logic as pydds.DDS.__init__)
        curbytes = 128
        w, h = width, height
        while w >= 1 and h >= 1:
            mm = type('MipMap', (), {
                'idx': self.mipMapCount,
                'startpos': curbytes,
                'length': 0,
                'endpos': 0,
                'retrieved': False,
                'databuffer': None,
            })()
            curbytes += max(1, (w * h >> 4)) * self.blocksize
            mm.length = curbytes - mm.startpos
            mm.endpos = mm.startpos + mm.length
            self.mipmap_list.append(mm)
            w >>= 1
            h >>= 1
            self.mipMapCount += 1
        
        self.total_size = curbytes
        
        # Build a fake 128-byte header
        self.header = BytesIO(b'DDS ' + b'\x00' * 124)


class MockTile:
    """Minimal mock of Tile for testing."""
    def __init__(self, row=21728, col=34432, maptype="BI",
                 tilename_zoom=12, max_zoom=16, dds_width=4096, dds_height=4096):
        self.row = row
        self.col = col
        self.maptype = maptype
        self.tilename_zoom = tilename_zoom
        self.max_zoom = max_zoom
        self.cache_dir = ""  # Set by test fixture
        self.id = f"{row}_{col}_{maptype}_{tilename_zoom}"
        self._prepopulated = False
        self._dds_upgrade_available = None
        self._dds_downgrade_available = None
        self._dds_needs_healing = False
        self._dds_missing_indices = []
        self.chunks_per_row = 16
        self.dds = MockDDS(dds_width, dds_height)


class MockCFG:
    """Minimal mock of CFG for staleness checks."""
    class pydds:
        format = "BC1"
        compressor = "ISPC"
        dds_compression = "zstd"
        dds_compression_level = 3
    class paths:
        cache_dir = ""
    class autoortho:
        persistent_dds_cache_mb = 4096
        disk_budget_enabled = True
        dds_budget_pct = 40
    class cache:
        file_cache_size = 30


# Patch the CFG import used by DynamicDDSCache
import types
_mock_cfg_module = types.ModuleType('autoortho.aoconfig')
_mock_cfg_module.CFG = MockCFG()

# Store original module state
_original_modules = {}


@pytest.fixture(autouse=True)
def patch_cfg(monkeypatch):
    """Patch CFG for all tests."""
    monkeypatch.setitem(sys.modules, 'autoortho.aoconfig', _mock_cfg_module)
    monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'format', 'BC1')
    monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'compressor', 'ISPC')
    monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'dds_compression', 'zstd')
    monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'dds_compression_level', 3)


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def cache_dir():
    """Create a temporary cache directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def dds_cache(cache_dir):
    """Create a DynamicDDSCache for testing."""
    from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
    return DynamicDDSCache(cache_dir=cache_dir, max_size_mb=10, enabled=True)


@pytest.fixture
def mock_tile(cache_dir):
    """Create a mock tile for testing."""
    tile = MockTile()
    tile.cache_dir = cache_dir
    return tile


@pytest.fixture
def sample_dds_bytes():
    """Generate sample DDS bytes matching a 4096x4096 BC1 tile."""
    tile = MockTile()
    total_size = tile.dds.total_size
    # Header + mipmap data filled with a recognizable pattern
    data = bytearray(total_size)
    data[:4] = b'DDS '
    # Fill with recognizable pattern per mipmap
    for mm in tile.dds.mipmap_list:
        pattern = bytes([mm.idx & 0xFF]) * mm.length
        data[mm.startpos:mm.endpos] = pattern[:mm.length]
    return bytes(data)


# ============================================================================
# DynamicDDSCache Tests
# ============================================================================

class TestDynamicDDSCache:
    """Tests for the persistent DDS cache."""

    def test_store_and_load(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test basic store and load cycle."""
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Store
        result = dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        assert result is True
        
        # Load
        loaded = dds_cache.load(tile_id, max_zoom, mock_tile)
        assert loaded is not None
        assert loaded == sample_dds_bytes

    def test_cache_miss(self, dds_cache, mock_tile):
        """Test that load returns None on cache miss."""
        result = dds_cache.load("nonexistent_tile", 16, mock_tile)
        assert result is None

    def test_disabled_cache(self, cache_dir, mock_tile, sample_dds_bytes):
        """Test that disabled cache is a no-op."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=0, enabled=True)
        
        result = cache.store(mock_tile.id, 16, sample_dds_bytes, mock_tile)
        assert result is False
        
        loaded = cache.load(mock_tile.id, 16, mock_tile)
        assert loaded is None

    def test_store_too_small(self, dds_cache, mock_tile):
        """Test that storing tiny data (< 128 bytes) is rejected."""
        result = dds_cache.store(mock_tile.id, 16, b'too small', mock_tile)
        assert result is False

    def test_store_empty(self, dds_cache, mock_tile):
        """Test that storing empty data is rejected."""
        result = dds_cache.store(mock_tile.id, 16, b'', mock_tile)
        assert result is False
        result = dds_cache.store(mock_tile.id, 16, None, mock_tile)
        assert result is False

    def test_staleness_format_change(self, dds_cache, mock_tile, sample_dds_bytes, monkeypatch):
        """Test that format change invalidates cache."""
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Store with BC1
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        
        # Change format to BC3
        monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'format', 'BC3')
        
        # Load should miss (stale)
        loaded = dds_cache.load(tile_id, max_zoom, mock_tile)
        assert loaded is None

    def test_staleness_compressor_change(self, dds_cache, mock_tile, sample_dds_bytes, monkeypatch):
        """Test that compressor change invalidates cache."""
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Store with ISPC
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        
        # Change compressor to STB
        monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'compressor', 'STB')
        
        # Load should miss (stale)
        loaded = dds_cache.load(tile_id, max_zoom, mock_tile)
        assert loaded is None

    def test_size_mismatch_invalidates(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that corrupted DDS is detected.

        Truncation breaks both uncompressed (size mismatch) and compressed
        (zstd frame corruption) files, so this test works regardless of
        the disk_compression setting.
        """
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Store valid data
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        
        # Corrupt the file by truncating it
        dds_path, _ = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, max_zoom
        )
        with open(dds_path, 'r+b') as f:
            f.truncate(64)
        
        # Load should miss (size mismatch or decompression failure)
        loaded = dds_cache.load(tile_id, max_zoom, mock_tile)
        assert loaded is None

    def test_invalidate(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test explicit invalidation."""
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Store
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        
        # Verify it's there
        assert dds_cache.load(tile_id, max_zoom, mock_tile) is not None
        
        # Invalidate
        result = dds_cache.invalidate(tile_id, max_zoom)
        assert result is True
        
        # Verify it's gone
        assert dds_cache.load(tile_id, max_zoom, mock_tile) is None

    def test_lru_eviction(self, cache_dir):
        """Test that LRU eviction works when cache is full."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        # Very small cache (64KB)
        cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=0.0625)
        
        tiles = []
        for i in range(5):
            tile = MockTile(row=21728 + i, col=34432)
            tile.cache_dir = cache_dir
            tiles.append(tile)
        
        # Store 5 tiles (each ~5.3MB for 4096x4096 BC1... too big)
        # Use small fake data instead
        small_data = b'DDS ' + b'\x00' * (128 + 10000)  # ~10KB each
        
        for tile in tiles:
            # Override DDS total_size to match our fake data
            tile.dds.total_size = len(small_data)
            cache.store(tile.id, tile.max_zoom, small_data, tile)
        
        # Cache should have evicted some entries to stay within ~64KB budget
        stats = cache.stats
        assert stats['disk_usage_mb'] <= 0.0625 * 1.1  # Allow 10% overshoot

    def test_stats(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test statistics tracking."""
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Initial stats
        stats = dds_cache.stats
        assert stats['hits'] == 0
        assert stats['misses'] == 0
        assert stats['stores'] == 0
        
        # Miss
        dds_cache.load(tile_id, max_zoom, mock_tile)
        assert dds_cache.stats['misses'] == 1
        
        # Store
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        assert dds_cache.stats['stores'] == 1
        
        # Hit
        dds_cache.load(tile_id, max_zoom, mock_tile)
        assert dds_cache.stats['hits'] == 1

    def test_scan_existing(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that scan_existing discovers entries from previous sessions."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        # Store an entry
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        
        # Create a new cache instance (simulating new session)
        cache2 = DynamicDDSCache(
            cache_dir=dds_cache._cache_dir,
            max_size_mb=10,
            enabled=True
        )
        
        # Before scan: miss
        assert cache2.stats['entries'] == 0
        
        # Run scan
        count = cache2.scan_existing()
        assert count == 1
        assert cache2.stats['entries'] == 1
        
        # Now load should succeed
        loaded = cache2.load(tile_id, max_zoom, mock_tile)
        assert loaded is not None
        assert loaded == sample_dds_bytes

    def test_ddm_metadata_written(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that DDM sidecar metadata is correctly written."""
        tile_id = mock_tile.id
        max_zoom = mock_tile.max_zoom
        
        dds_cache.store(tile_id, max_zoom, sample_dds_bytes, mock_tile)
        
        _, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, max_zoom
        )
        
        assert os.path.exists(ddm_path)
        
        with open(ddm_path, 'r') as f:
            meta = json.load(f)
        
        assert meta['v'] == 2
        assert meta['w'] == 4096
        assert meta['h'] == 4096
        assert meta['max_zl'] == 16
        assert meta['fmt'] == 'BC1'
        assert meta['comp'] == 'ISPC'
        assert meta['map'] == 'BI'
        assert meta['tile_row'] == 21728
        assert meta['tile_col'] == 34432

    def test_store_from_file(self, dds_cache, mock_tile, sample_dds_bytes, cache_dir):
        """Test store_from_file copies an existing DDS into the persistent cache."""
        # Write a DDS file to a temporary location (simulating ephemeral cache)
        source_path = os.path.join(cache_dir, "ephemeral_tile.dds")
        with open(source_path, 'wb') as f:
            f.write(sample_dds_bytes)
        
        # Store from file
        result = dds_cache.store_from_file(
            mock_tile.id, mock_tile.max_zoom, source_path, mock_tile)
        assert result is True
        
        # Load should succeed
        loaded = dds_cache.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert loaded is not None
        assert loaded == sample_dds_bytes
        
        # Stats should show a store
        assert dds_cache.stats['stores'] == 1

    def test_store_from_file_missing_source(self, dds_cache, mock_tile):
        """Test store_from_file with non-existent source returns False."""
        result = dds_cache.store_from_file(
            mock_tile.id, 16, "/nonexistent/path.dds", mock_tile)
        assert result is False

    def test_atomic_write_no_partial_files(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that no .tmp files are left after successful store."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)
        
        dds_path, _ = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        parent_dir = os.path.dirname(dds_path)
        
        # Check no .tmp files remain
        for fname in os.listdir(parent_dir):
            assert not fname.endswith('.tmp'), f"Temp file left behind: {fname}"

    def test_concurrent_store_load(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test thread safety of concurrent store and load operations."""
        errors = []
        
        def store_worker():
            try:
                for i in range(10):
                    dds_cache.store(mock_tile.id, mock_tile.max_zoom,
                                   sample_dds_bytes, mock_tile)
            except Exception as e:
                errors.append(e)
        
        def load_worker():
            try:
                for i in range(10):
                    dds_cache.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=store_worker),
            threading.Thread(target=store_worker),
            threading.Thread(target=load_worker),
            threading.Thread(target=load_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert len(errors) == 0, f"Thread errors: {errors}"

    def test_zl_upgrade_hint(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that ZL upgrade hint is set when lower-ZL DDS exists."""
        # Store at ZL16
        dds_cache.store(mock_tile.id, 16, sample_dds_bytes, mock_tile)
        
        # Create a tile requesting ZL17 (dimensions change)
        tile_17 = MockTile(max_zoom=17, dds_width=8192, dds_height=8192)
        tile_17.cache_dir = mock_tile.cache_dir
        
        # Load at ZL17 should miss but set upgrade hint
        result = dds_cache.load(tile_17.id, 17, tile_17)
        assert result is None
        assert tile_17._dds_upgrade_available is not None
        old_path, old_meta = tile_17._dds_upgrade_available
        assert old_meta['max_zl'] == 16


class TestDynamicDDSCacheUpgrade:
    """Tests for ZL upgrade (mipmap shifting) functionality."""

    def test_upgrade_zl_basic(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test basic ZL upgrade with mipmap shifting."""
        # Store at ZL16 (4096x4096)
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)
        
        # Create tile at ZL17 (8192x8192)
        tile_17 = MockTile(max_zoom=17, dds_width=8192, dds_height=8192)
        tile_17.cache_dir = cache_dir
        
        # Create fake new mm0 bytes (sized for 8192x8192 BC1 mm0)
        new_mm0_size = tile_17.dds.mipmap_list[0].length
        new_mm0_bytes = bytes([0xAA]) * new_mm0_size
        
        # Perform upgrade
        result = dds_cache.upgrade_zl(
            tile_17.id, old_max_zoom=16, new_max_zoom=17,
            new_mm0_bytes=new_mm0_bytes, tile=tile_17
        )
        
        assert result is not None
        assert len(result) == tile_17.dds.total_size
        
        # Verify new mm0 was written
        mm0 = tile_17.dds.mipmap_list[0]
        assert result[mm0.startpos:mm0.startpos + 3] == b'\xAA\xAA\xAA'
        
        # Verify old mm0 was shifted to new mm1
        if len(tile_17.dds.mipmap_list) > 1:
            new_mm1 = tile_17.dds.mipmap_list[1]
            # Old mm0 was filled with pattern bytes([0])
            old_mm0 = tile_16.dds.mipmap_list[0]
            old_data = sample_dds_bytes[old_mm0.startpos:old_mm0.endpos]
            shifted_data = result[new_mm1.startpos:new_mm1.startpos + len(old_data)]
            assert shifted_data == old_data

    def test_upgrade_zl_rejects_multi_step(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test that multi-step upgrades (ZL15->ZL17) are rejected."""
        tile = MockTile(max_zoom=17, dds_width=8192, dds_height=8192)
        tile.cache_dir = cache_dir
        
        result = dds_cache.upgrade_zl(
            tile.id, old_max_zoom=15, new_max_zoom=17,
            new_mm0_bytes=b'\x00' * 1000, tile=tile
        )
        assert result is None


# ============================================================================
# DiskBudgetManager Tests
# ============================================================================

class TestDiskBudgetManager:
    """Tests for the disk budget enforcement system."""

    def test_initialization(self, cache_dir):
        """Test manager initializes with correct budget splits."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        mgr = DiskBudgetManager(
            cache_dir=cache_dir,
            total_budget_mb=1000,
            dds_budget_pct=40,
            bundle_budget_pct=55,
            jpeg_budget_pct=5
        )
        
        report = mgr.usage_report
        total_budget = report['dds_budget_mb'] + report['bundle_budget_mb'] + report['jpeg_budget_mb']
        assert abs(total_budget - 1000) < 1  # Allow rounding

    def test_account_dds(self, cache_dir):
        """Test DDS accounting updates usage."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        mgr = DiskBudgetManager(cache_dir=cache_dir, total_budget_mb=1000)
        
        mgr.account_dds(1024 * 1024)  # 1MB
        report = mgr.usage_report
        assert report['dds_usage_mb'] >= 0.9

    def test_account_bundle(self, cache_dir):
        """Test bundle accounting updates usage."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        mgr = DiskBudgetManager(cache_dir=cache_dir, total_budget_mb=1000)
        
        mgr.account_bundle(5 * 1024 * 1024)  # 5MB
        report = mgr.usage_report
        assert report['bundle_usage_mb'] >= 4.9

    def test_scan_empty_dir(self, cache_dir):
        """Test scanning an empty cache directory."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        mgr = DiskBudgetManager(cache_dir=cache_dir, total_budget_mb=1000)
        report = mgr.scan_disk_usage()
        
        assert report.bundle_bytes == 0
        assert report.dds_bytes == 0
        assert report.jpeg_bytes == 0
        assert report.scan_time_ms >= 0

    def test_scan_with_files(self, cache_dir):
        """Test scanning a cache directory with actual files."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        # Create some fake bundle and DDS files
        bundles_dir = os.path.join(cache_dir, "bundles", "+50+010", "+50+010", "BI")
        os.makedirs(bundles_dir, exist_ok=True)
        with open(os.path.join(bundles_dir, "21728_34432.aob2"), 'wb') as f:
            f.write(b'\x00' * 10000)
        
        dds_dir = os.path.join(cache_dir, "dds_cache", "+50+010", "+50+010", "BI")
        os.makedirs(dds_dir, exist_ok=True)
        with open(os.path.join(dds_dir, "21728_34432_z16.dds"), 'wb') as f:
            f.write(b'\x00' * 20000)
        
        mgr = DiskBudgetManager(cache_dir=cache_dir, total_budget_mb=1000)
        report = mgr.scan_disk_usage()
        
        assert report.bundle_bytes == 10000
        assert report.dds_bytes == 20000

    def test_cleanup_stale_dds_no_bundle(self, cache_dir):
        """Test that stale DDS (no matching bundle) is cleaned up."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        # Create a DDS + DDM with no matching bundle
        dds_dir = os.path.join(cache_dir, "dds_cache", "+50+010", "+50+010", "BI")
        os.makedirs(dds_dir, exist_ok=True)
        
        dds_path = os.path.join(dds_dir, "21728_34432_z16.dds")
        ddm_path = os.path.join(dds_dir, "21728_34432_z16.ddm")
        
        with open(dds_path, 'wb') as f:
            f.write(b'\x00' * 1000)
        
        meta = {
            "v": 1, "tile_row": 21728, "tile_col": 34432,
            "map": "BI", "zl": 12, "max_zl": 16,
            "fmt": "BC1", "comp": "ISPC"
        }
        with open(ddm_path, 'w') as f:
            json.dump(meta, f)
        
        mgr = DiskBudgetManager(cache_dir=cache_dir, total_budget_mb=1000)
        count = mgr.cleanup_stale_dds()
        
        assert count == 1
        assert not os.path.exists(dds_path)
        assert not os.path.exists(ddm_path)

    def test_usage_report_format(self, cache_dir):
        """Test usage_report returns expected keys."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        mgr = DiskBudgetManager(cache_dir=cache_dir, total_budget_mb=1000)
        report = mgr.usage_report
        
        expected_keys = [
            'dds_usage_mb', 'dds_budget_mb',
            'bundle_usage_mb', 'bundle_budget_mb',
            'jpeg_usage_mb', 'jpeg_budget_mb',
            'total_usage_mb', 'total_budget_mb',
            'last_scan'
        ]
        for key in expected_keys:
            assert key in report, f"Missing key: {key}"

    def test_percentage_clamping(self, cache_dir):
        """Test that budget percentages are clamped to valid ranges."""
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        # Pass extreme percentages
        mgr = DiskBudgetManager(
            cache_dir=cache_dir,
            total_budget_mb=1000,
            dds_budget_pct=100,     # Should be clamped to 60
            bundle_budget_pct=0,    # Should be clamped to 30
            jpeg_budget_pct=0       # Should be clamped to 1
        )
        
        report = mgr.usage_report
        # After clamping (60 + 30 + 1 = 91) normalized to sum to 1000MB
        assert report['dds_budget_mb'] > 0
        assert report['bundle_budget_mb'] > 0
        assert report['jpeg_budget_mb'] > 0


# ============================================================================
# Bundle Paths Tests
# ============================================================================

class TestDDSCachePaths:
    """Tests for the DDS cache path helpers."""

    def test_get_dds_cache_path(self):
        """Test DDS cache path generation."""
        from autoortho.utils.bundle_paths import get_dds_cache_path
        
        base = get_dds_cache_path(
            cache_dir="/tmp/cache",
            row=21728, col=34432,
            maptype="BI", zoom=12, max_zoom=16
        )
        
        # Should contain dds_cache in the path
        assert "dds_cache" in base
        # Should contain the z suffix
        assert base.endswith("_z16")
        # Should contain maptype directory
        assert os.sep + "BI" + os.sep in base or "/BI/" in base

    def test_dds_cache_path_different_zooms(self):
        """Test that different max_zoom produces different paths."""
        from autoortho.utils.bundle_paths import get_dds_cache_path
        
        path_z16 = get_dds_cache_path("/tmp/cache", 21728, 34432, "BI", 12, 16)
        path_z17 = get_dds_cache_path("/tmp/cache", 21728, 34432, "BI", 12, 17)
        
        assert path_z16 != path_z17
        assert "_z16" in path_z16
        assert "_z17" in path_z17

    def test_dds_cache_dir_structure(self):
        """Test DDS cache directory structure."""
        from autoortho.utils.bundle_paths import get_dds_cache_dir
        
        dds_dir = get_dds_cache_dir(
            cache_dir="/tmp/cache",
            row=21728, col=34432,
            zoom=12, maptype="BI"
        )
        
        assert "dds_cache" in dds_dir


# ============================================================================
# Integration-style Tests (without full autoortho stack)
# ============================================================================

class TestIntegration:
    """Test interactions between DynamicDDSCache and DiskBudgetManager."""

    def test_cache_with_budget_manager(self, cache_dir):
        """Test that DDS cache and budget manager work together."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        from autoortho.aopipeline.disk_budget_manager import DiskBudgetManager
        
        dds_cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=10)
        budget_mgr = DiskBudgetManager(
            cache_dir=cache_dir,
            total_budget_mb=100,
            dds_cache=dds_cache
        )
        
        tile = MockTile()
        tile.cache_dir = cache_dir
        
        # Create fake DDS data
        data = b'DDS ' + b'\x00' * (tile.dds.total_size - 4)
        assert len(data) == tile.dds.total_size
        
        # Store through cache
        dds_cache.store(tile.id, tile.max_zoom, data, tile)
        
        # Account in budget manager
        budget_mgr.account_dds(len(data))
        
        # Verify both track the size
        assert dds_cache.get_disk_usage() > 0
        assert budget_mgr.usage_report['dds_usage_mb'] > 0

    def test_initial_scan_thread_safe(self, cache_dir):
        """Test that initial_scan can run concurrently with stores."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        
        cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=100)
        errors = []
        
        def scan_worker():
            try:
                cache.scan_existing()
            except Exception as e:
                errors.append(e)
        
        def store_worker():
            try:
                tile = MockTile()
                tile.cache_dir = cache_dir
                data = b'DDS ' + b'\x00' * (tile.dds.total_size - 4)
                for i in range(5):
                    t = MockTile(row=21728 + i)
                    t.cache_dir = cache_dir
                    cache.store(t.id, t.max_zoom, data, t)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=scan_worker),
            threading.Thread(target=store_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert len(errors) == 0, f"Thread errors: {errors}"


# ============================================================================
# DDM v2 Tests
# ============================================================================

class TestDDMv2:
    """Tests for DDM version 2 metadata (missing indices, healing fields)."""

    def test_ddm_v2_complete_tile(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test DDM v2 for a fully complete tile (no missing chunks)."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        _, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        with open(ddm_path, 'r') as f:
            meta = json.load(f)

        assert meta['v'] == 2
        assert meta['needs_healing'] is False
        assert meta['healing_chunks'] == 0
        assert meta['missing_indices'] == []

    def test_ddm_v2_incomplete_tile(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test DDM v2 for a tile with missing chunks."""
        missing = [0, 3, 7]
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes,
                        mock_tile, mm0_missing_indices=missing)

        _, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        with open(ddm_path, 'r') as f:
            meta = json.load(f)

        assert meta['v'] == 2
        assert meta['needs_healing'] is True
        assert meta['healing_chunks'] == 3
        assert meta['missing_indices'] == [0, 3, 7]

    def test_ddm_v2_mipmap_completeness(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test DDM v2 mipmap completeness metadata for mm0."""
        missing = [2, 5]
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes,
                        mock_tile, mm0_missing_indices=missing)

        _, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        with open(ddm_path, 'r') as f:
            meta = json.load(f)

        mipmaps = meta.get('mipmaps', [])
        assert len(mipmaps) > 0
        mm0 = mipmaps[0]
        assert mm0.get('complete') is False
        total = mm0.get('total', 0)
        valid = mm0.get('valid', 0)
        assert total > 0
        assert valid == total - 2

    def test_load_sets_healing_flags(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that load() sets healing flags on tile when DDM indicates missing chunks."""
        missing = [1, 4]
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes,
                        mock_tile, mm0_missing_indices=missing)

        tile2 = MockTile()
        tile2.cache_dir = mock_tile.cache_dir
        loaded = dds_cache.load(tile2.id, tile2.max_zoom, tile2)

        assert loaded is not None
        assert tile2._dds_needs_healing is True
        assert tile2._dds_missing_indices == [1, 4]

    def test_load_complete_tile_no_healing(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that load() does not set healing flags for complete tiles."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        tile2 = MockTile()
        tile2.cache_dir = mock_tile.cache_dir
        loaded = dds_cache.load(tile2.id, tile2.max_zoom, tile2)

        assert loaded is not None
        assert tile2._dds_needs_healing is False
        assert tile2._dds_missing_indices == []

    def test_load_metadata(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test load_metadata() reads DDM without loading DDS bytes."""
        missing = [0]
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes,
                        mock_tile, mm0_missing_indices=missing)

        meta = dds_cache.load_metadata(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert meta is not None
        assert meta['needs_healing'] is True
        assert meta['missing_indices'] == [0]

    def test_load_metadata_miss(self, dds_cache, mock_tile):
        """Test load_metadata() returns None on cache miss."""
        meta = dds_cache.load_metadata("nonexistent", 16, mock_tile)
        assert meta is None

    def test_contains(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test contains() for lightweight existence checks."""
        assert dds_cache.contains(mock_tile.id, mock_tile.max_zoom, mock_tile) is False
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)
        assert dds_cache.contains(mock_tile.id, mock_tile.max_zoom, mock_tile) is True


# ============================================================================
# ZL Downgrade Tests
# ============================================================================

class TestDynamicDDSCacheDowngrade:
    """Tests for ZL downgrade (mipmap stripping) functionality."""

    def test_downgrade_zl_basic(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test basic ZL downgrade with mipmap stripping."""
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)

        tile_15 = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile_15.cache_dir = cache_dir

        result = dds_cache.downgrade_zl(
            tile_15.id, old_max_zoom=16, new_max_zoom=15, tile=tile_15
        )

        assert result is not None
        assert len(result) == tile_15.dds.total_size

        # old mm1 (2048x2048 data) becomes new mm0
        old_mm1 = tile_16.dds.mipmap_list[1]
        new_mm0 = tile_15.dds.mipmap_list[0]
        old_data = sample_dds_bytes[old_mm1.startpos:old_mm1.endpos]
        new_data = result[new_mm0.startpos:new_mm0.startpos + len(old_data)]
        assert new_data == old_data

    def test_downgrade_zl_rejects_multi_step(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test that multi-step downgrades (ZL17->ZL15) are rejected."""
        tile = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile.cache_dir = cache_dir

        result = dds_cache.downgrade_zl(
            tile.id, old_max_zoom=17, new_max_zoom=15, tile=tile
        )
        assert result is None

    def test_downgrade_zl_old_entry_deleted(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test that old ZL entry is cleaned up after downgrade."""
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)

        old_dds_path, old_ddm_path = dds_cache._paths_for(
            tile_16.row, tile_16.col, tile_16.maptype,
            tile_16.tilename_zoom, 16)
        assert os.path.exists(old_dds_path)

        tile_15 = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile_15.cache_dir = cache_dir
        result = dds_cache.downgrade_zl(
            tile_15.id, old_max_zoom=16, new_max_zoom=15, tile=tile_15
        )
        assert result is not None
        assert not os.path.exists(old_dds_path)
        assert not os.path.exists(old_ddm_path)

    def test_downgrade_zl_new_entry_loadable(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test that the downgraded entry can be loaded at the new ZL."""
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)

        tile_15 = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile_15.cache_dir = cache_dir
        downgraded = dds_cache.downgrade_zl(
            tile_15.id, old_max_zoom=16, new_max_zoom=15, tile=tile_15
        )
        assert downgraded is not None

        loaded = dds_cache.load(tile_15.id, 15, tile_15)
        assert loaded is not None
        assert loaded == downgraded

    def test_zl_downgrade_hint_in_load(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test that load() sets _dds_downgrade_available when higher-ZL DDS exists."""
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)

        tile_15 = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile_15.cache_dir = cache_dir

        result = dds_cache.load(tile_15.id, 15, tile_15)
        assert result is None
        assert tile_15._dds_downgrade_available is not None
        old_path, old_meta = tile_15._dds_downgrade_available
        assert old_meta['max_zl'] == 16

    def test_downgrade_zl_disabled_cache(self, cache_dir):
        """Test that downgrade_zl returns None when cache is disabled."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=0, enabled=True)
        tile = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile.cache_dir = cache_dir

        result = cache.downgrade_zl(tile.id, old_max_zoom=16, new_max_zoom=15, tile=tile)
        assert result is None


# ============================================================================
# JPEG Cleanup Tests
# ============================================================================

class TestJPEGCleanup:
    """Tests for JPEG cleanup after complete DDS store."""

    def test_cleanup_source_jpegs_basic(self, cache_dir):
        """Test that cleanup_source_jpegs deletes JPEG files."""
        from autoortho.aopipeline.dynamic_dds_cache import cleanup_source_jpegs

        for i in range(3):
            jpeg_path = os.path.join(cache_dir, f"{100+i}_{200}_{16}_BI.jpg")
            with open(jpeg_path, 'wb') as f:
                f.write(b'\xff\xd8\xff' + b'\x00' * 100)

        deleted = cleanup_source_jpegs(
            cache_dir=cache_dir,
            col=100, row=200,
            tilename_zoom=12, max_zoom=16, min_zoom=16,
            width=3, height=1, maptype="BI"
        )
        assert deleted == 3

        for i in range(3):
            assert not os.path.exists(
                os.path.join(cache_dir, f"{100+i}_{200}_{16}_BI.jpg"))

    def test_cleanup_source_jpegs_missing_files(self, cache_dir):
        """Test that cleanup handles missing JPEG files gracefully."""
        from autoortho.aopipeline.dynamic_dds_cache import cleanup_source_jpegs

        deleted = cleanup_source_jpegs(
            cache_dir=cache_dir,
            col=100, row=200,
            tilename_zoom=12, max_zoom=16, min_zoom=16,
            width=2, height=2, maptype="BI"
        )
        assert deleted == 0

    def test_cleanup_source_jpegs_partial(self, cache_dir):
        """Test cleanup deletes only existing files."""
        from autoortho.aopipeline.dynamic_dds_cache import cleanup_source_jpegs

        jpeg_path = os.path.join(cache_dir, f"100_200_16_BI.jpg")
        with open(jpeg_path, 'wb') as f:
            f.write(b'\xff\xd8\xff' + b'\x00' * 100)

        deleted = cleanup_source_jpegs(
            cache_dir=cache_dir,
            col=100, row=200,
            tilename_zoom=12, max_zoom=16, min_zoom=16,
            width=2, height=2, maptype="BI"
        )
        assert deleted == 1


# ============================================================================
# Get Staging Path Tests
# ============================================================================

class TestGetStagingPath:
    """Tests for get_staging_path used by native direct-to-disk writes."""

    def test_staging_path_format(self, dds_cache, mock_tile):
        """Test staging path has expected format."""
        path = dds_cache.get_staging_path(
            mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert path is not None
        assert path.endswith(f".tmp.{os.getpid()}")
        assert "dds_cache" in path

    def test_staging_path_disabled(self, cache_dir, mock_tile):
        """Test staging path returns None when cache is disabled."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache
        cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=0, enabled=True)
        path = cache.get_staging_path(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert path is None

    def test_staging_path_parent_dir_created(self, dds_cache, mock_tile):
        """Test that get_staging_path creates parent directories."""
        path = dds_cache.get_staging_path(
            mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert path is not None
        parent = os.path.dirname(path)
        assert os.path.isdir(parent)


# ============================================================================
# Zstd Compression Tests
# ============================================================================

class TestZstdCompression:
    """Tests for zstd disk compression of cached DDS files."""

    def test_compressed_round_trip(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test store + load round-trip returns identical bytes with compression."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)
        loaded = dds_cache.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert loaded is not None
        assert loaded == sample_dds_bytes

    def test_compressed_file_is_smaller(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that the on-disk file is smaller than uncompressed DDS."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        dds_path, _ = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        disk_size = os.path.getsize(dds_path)
        assert disk_size < len(sample_dds_bytes)

    def test_ddm_records_compression(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that DDM metadata records disk_compression field."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        _, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        with open(ddm_path, 'r') as f:
            meta = json.load(f)

        assert meta.get('disk_compression') == 'zstd'

    def test_uncompressed_files_still_loadable(self, cache_dir, mock_tile, sample_dds_bytes, monkeypatch):
        """Test backwards compat: uncompressed files load when compression is now enabled."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache

        # Store with compression disabled
        monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'dds_compression', 'none')
        cache_none = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=10, enabled=True)
        cache_none.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        # Load with compression enabled (new instance reads 'zstd' from config)
        monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'dds_compression', 'zstd')
        cache_zstd = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=10, enabled=True)
        cache_zstd.scan_existing()

        loaded = cache_zstd.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert loaded is not None
        assert loaded == sample_dds_bytes

    def test_no_compression_mode(self, cache_dir, mock_tile, sample_dds_bytes, monkeypatch):
        """Test dds_compression=none stores raw DDS data."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache

        monkeypatch.setattr(_mock_cfg_module.CFG.pydds, 'dds_compression', 'none')
        cache = DynamicDDSCache(cache_dir=cache_dir, max_size_mb=10, enabled=True)

        cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        dds_path, ddm_path = cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        assert os.path.getsize(dds_path) == len(sample_dds_bytes)

        with open(ddm_path, 'r') as f:
            meta = json.load(f)
        assert meta.get('disk_compression') == 'none'

    def test_lru_tracks_compressed_size(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that LRU disk usage tracks compressed (on-disk) size, not uncompressed."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        dds_path, _ = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        disk_size = os.path.getsize(dds_path)

        disk_usage = dds_cache.get_disk_usage()
        assert disk_usage == disk_size

    def test_store_from_file_compressed(self, dds_cache, mock_tile, sample_dds_bytes, cache_dir):
        """Test store_from_file compresses the source file."""
        source_path = os.path.join(cache_dir, "ephemeral_tile.dds")
        with open(source_path, 'wb') as f:
            f.write(sample_dds_bytes)

        dds_cache.store_from_file(
            mock_tile.id, mock_tile.max_zoom, source_path, mock_tile)

        loaded = dds_cache.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert loaded is not None
        assert loaded == sample_dds_bytes

        # Verify the cached file is compressed (smaller than source)
        dds_path, _ = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        assert os.path.getsize(dds_path) < len(sample_dds_bytes)

    def test_compressed_staleness_not_false_positive(self, dds_cache, mock_tile, sample_dds_bytes):
        """Test that compressed files are not falsely flagged as stale."""
        dds_cache.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        # Load twice -- if staleness check was wrong, second load would fail
        loaded1 = dds_cache.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        loaded2 = dds_cache.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert loaded1 is not None
        assert loaded2 is not None
        assert loaded1 == loaded2 == sample_dds_bytes

    def test_incremental_store_compressed(self, dds_cache, mock_tile):
        """Test that incremental stores are compressed when zstd is enabled."""
        total_size = 128 + 8192
        mm_data = {0: b'\xAA' * 8192}
        mm_offsets = {0: (128, 8192)}

        dds_cache.store_incremental(
            mock_tile.id, mock_tile.max_zoom,
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom,
            b'DDS ' + b'\x00' * 124,
            total_size, 4096, 4096, 1,
            mm_data, mm_offsets
        )

        dds_path, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom
        )
        with open(ddm_path, 'r') as f:
            meta = json.load(f)

        assert meta.get('disk_compression') == 'zstd'

        # On-disk file should be smaller than the uncompressed total
        disk_size = os.path.getsize(dds_path)
        assert disk_size < total_size

    def test_upgrade_zl_with_compression(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test ZL upgrade works with compressed source files."""
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)

        tile_17 = MockTile(max_zoom=17, dds_width=8192, dds_height=8192)
        tile_17.cache_dir = cache_dir

        new_mm0_size = tile_17.dds.mipmap_list[0].length
        new_mm0_bytes = bytes([0xBB]) * new_mm0_size

        result = dds_cache.upgrade_zl(
            tile_17.id, old_max_zoom=16, new_max_zoom=17,
            new_mm0_bytes=new_mm0_bytes, tile=tile_17
        )
        assert result is not None
        assert len(result) == tile_17.dds.total_size

    def test_downgrade_zl_with_compression(self, dds_cache, cache_dir, sample_dds_bytes):
        """Test ZL downgrade works with compressed source files."""
        tile_16 = MockTile(max_zoom=16, dds_width=4096, dds_height=4096)
        tile_16.cache_dir = cache_dir
        dds_cache.store(tile_16.id, 16, sample_dds_bytes, tile_16)

        tile_15 = MockTile(max_zoom=15, dds_width=2048, dds_height=2048)
        tile_15.cache_dir = cache_dir

        result = dds_cache.downgrade_zl(
            tile_15.id, old_max_zoom=16, new_max_zoom=15, tile=tile_15
        )
        assert result is not None
        assert len(result) == tile_15.dds.total_size

    def test_migrate_uncompressed(self, cache_dir, mock_tile, sample_dds_bytes, monkeypatch):
        """Test that migrate_uncompressed re-compresses old uncompressed files."""
        from autoortho.aopipeline.dynamic_dds_cache import DynamicDDSCache

        # Store with compression disabled
        monkeypatch.setattr(MockCFG.pydds, 'dds_compression', 'none')
        cache_none = DynamicDDSCache(cache_dir, max_size_mb=512, enabled=True)
        cache_none.store(mock_tile.id, mock_tile.max_zoom, sample_dds_bytes, mock_tile)

        dds_path, ddm_path = cache_none._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom)
        original_size = os.path.getsize(dds_path)

        # Create a new cache instance with compression enabled
        monkeypatch.setattr(MockCFG.pydds, 'dds_compression', 'zstd')
        cache_zstd = DynamicDDSCache(cache_dir, max_size_mb=512, enabled=True)
        cache_zstd.scan_existing()

        migrated = cache_zstd.migrate_uncompressed()
        assert migrated == 1

        # File should now be smaller
        new_size = os.path.getsize(dds_path)
        assert new_size < original_size

        # DDM should record zstd compression
        with open(ddm_path, 'r') as f:
            meta = json.load(f)
        assert meta.get('disk_compression') == 'zstd'

        # Round-trip: loading should still return original data
        loaded = cache_zstd.load(mock_tile.id, mock_tile.max_zoom, mock_tile)
        assert loaded == sample_dds_bytes

    def test_incremental_subsequent_writes_on_compressed(self, dds_cache, mock_tile):
        """Test that subsequent incremental writes work on already-compressed files."""
        total_size = 128 + 8192 + 2048
        header = b'DDS ' + b'\x00' * 124

        # First incremental write (mipmap 1)
        dds_cache.store_incremental(
            mock_tile.id, mock_tile.max_zoom,
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom,
            header, total_size, 4096, 4096, 2,
            {1: b'\xBB' * 2048}, {0: (128, 8192), 1: (128 + 8192, 2048)}
        )

        _, ddm_path = dds_cache._paths_for(
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom, mock_tile.max_zoom)
        with open(ddm_path, 'r') as f:
            meta1 = json.load(f)
        assert 1 in meta1['populated_mipmaps']
        assert 0 not in meta1['populated_mipmaps']

        # Second incremental write (mipmap 0) on top of compressed file
        dds_cache.store_incremental(
            mock_tile.id, mock_tile.max_zoom,
            mock_tile.row, mock_tile.col, mock_tile.maptype,
            mock_tile.tilename_zoom,
            header, total_size, 4096, 4096, 2,
            {0: b'\xAA' * 8192}, {0: (128, 8192), 1: (128 + 8192, 2048)}
        )

        with open(ddm_path, 'r') as f:
            meta2 = json.load(f)
        assert 0 in meta2['populated_mipmaps']
        assert 1 in meta2['populated_mipmaps']
        assert meta2.get('disk_compression') == 'zstd'

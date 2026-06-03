#!/usr/bin/env python3
"""Tests for virtual DDS size estimates reported through FUSE metadata."""

import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTOORTHO_DIR = ROOT / "autoortho"
for path in (str(ROOT), str(AUTOORTHO_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


from aoconfig import CFG
from utils.dynamic_zoom import DynamicZoomManager


SIZE_4K_BC1 = 11184952
SIZE_8K_BC1 = 44739384


def _load_autoortho_fuse(monkeypatch):
    """Import autoortho_fuse with optional runtime dependencies stubbed."""
    flighttrack = types.ModuleType("autoortho.flighttrack")
    monkeypatch.setitem(sys.modules, "flighttrack", flighttrack)
    monkeypatch.setitem(sys.modules, "autoortho.flighttrack", flighttrack)

    getortho = types.ModuleType("autoortho.getortho")
    getortho.clear_shutdown_request = lambda: None
    getortho.TileCacher = lambda cache_dir: None
    getortho.register_terrain_index = lambda *args, **kwargs: None
    getortho.start_prefetcher = lambda *args, **kwargs: None
    getortho.start_predictive_dds = lambda *args, **kwargs: None
    getortho.register_discovered_maptype = lambda *args, **kwargs: None
    getortho.is_shutdown_requested = lambda: False
    getortho.begin_shutdown = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "getortho", getortho)
    monkeypatch.setitem(sys.modules, "autoortho.getortho", getortho)

    mfusepy = types.ModuleType("autoortho.mfusepy")

    class FuseOSError(OSError):
        pass

    class Operations:
        pass

    mfusepy.FUSE = object
    mfusepy.FuseOSError = FuseOSError
    mfusepy.Operations = Operations
    mfusepy.fuse_get_context = lambda: (0, 0, 0)
    mfusepy._libfuse = types.SimpleNamespace(
        fuse_get_context=lambda: types.SimpleNamespace(
            contents=types.SimpleNamespace(fuse=None)
        )
    )
    monkeypatch.setitem(sys.modules, "mfusepy", mfusepy)
    monkeypatch.setitem(sys.modules, "autoortho.mfusepy", mfusepy)

    autoortho_stub = types.ModuleType("autoortho")
    autoortho_stub.flighttrack = flighttrack
    autoortho_stub.getortho = getortho
    monkeypatch.setitem(sys.modules, "autoortho", autoortho_stub)

    monkeypatch.delitem(sys.modules, "autoortho_fuse", raising=False)
    monkeypatch.delitem(sys.modules, "autoortho.autoortho_fuse", raising=False)
    return importlib.import_module("autoortho_fuse")


class _FakeTileCacher:
    def __init__(self, dynamic_zoom_manager=None):
        self.dynamic_zoom_manager = dynamic_zoom_manager
        self.target_zoom_level = 16
        self.target_zoom_level_near_airports = 18


class _FakeAutoOrtho:
    def __init__(self, dynamic_zoom_manager=None):
        self.tc = _FakeTileCacher(dynamic_zoom_manager)


def _manager(regular_zoom, airport_zoom):
    manager = DynamicZoomManager()
    manager.set_base_zoom(regular_zoom, airport_zoom)
    return manager


def _calculate_size(module, fake_ao, zoom):
    module.AutoOrtho._calculate_dds_size.cache_clear()
    return module.AutoOrtho._calculate_dds_size(fake_ao, str(zoom))


def test_dynamic_regular_zl16_reports_4k_size(monkeypatch):
    module = _load_autoortho_fuse(monkeypatch)
    monkeypatch.setattr(CFG.pydds, "format", "BC1")
    monkeypatch.setattr(CFG.autoortho, "max_zoom_mode", "dynamic")
    monkeypatch.setattr(CFG.autoortho, "using_custom_tiles", False)

    fake_ao = _FakeAutoOrtho(_manager(regular_zoom=16, airport_zoom=18))

    assert _calculate_size(module, fake_ao, 16) == SIZE_4K_BC1


def test_dynamic_airport_zl18_reports_4k_size(monkeypatch):
    module = _load_autoortho_fuse(monkeypatch)
    monkeypatch.setattr(CFG.pydds, "format", "BC1")
    monkeypatch.setattr(CFG.autoortho, "max_zoom_mode", "dynamic")
    monkeypatch.setattr(CFG.autoortho, "using_custom_tiles", False)

    fake_ao = _FakeAutoOrtho(_manager(regular_zoom=16, airport_zoom=18))

    assert _calculate_size(module, fake_ao, 18) == SIZE_4K_BC1


def test_dynamic_configured_zl17_or_zl19_still_reports_8k_size(monkeypatch):
    module = _load_autoortho_fuse(monkeypatch)
    monkeypatch.setattr(CFG.pydds, "format", "BC1")
    monkeypatch.setattr(CFG.autoortho, "max_zoom_mode", "dynamic")
    monkeypatch.setattr(CFG.autoortho, "using_custom_tiles", False)

    assert _calculate_size(
        module, _FakeAutoOrtho(_manager(regular_zoom=17, airport_zoom=18)), 16
    ) == SIZE_8K_BC1
    assert _calculate_size(
        module, _FakeAutoOrtho(_manager(regular_zoom=16, airport_zoom=19)), 18
    ) == SIZE_8K_BC1


def test_fixed_mode_size_behavior_is_unchanged(monkeypatch):
    module = _load_autoortho_fuse(monkeypatch)
    monkeypatch.setattr(CFG.pydds, "format", "BC1")
    monkeypatch.setattr(CFG.autoortho, "max_zoom_mode", "fixed")
    monkeypatch.setattr(CFG.autoortho, "using_custom_tiles", False)

    fake_ao = _FakeAutoOrtho()
    fake_ao.tc.target_zoom_level = 16
    fake_ao.tc.target_zoom_level_near_airports = 18
    assert _calculate_size(module, fake_ao, 16) == SIZE_4K_BC1
    assert _calculate_size(module, fake_ao, 18) == SIZE_4K_BC1

    high_zoom_fake_ao = _FakeAutoOrtho()
    high_zoom_fake_ao.tc.target_zoom_level = 17
    high_zoom_fake_ao.tc.target_zoom_level_near_airports = 19
    assert _calculate_size(module, high_zoom_fake_ao, 16) == SIZE_8K_BC1
    assert _calculate_size(module, high_zoom_fake_ao, 18) == SIZE_8K_BC1

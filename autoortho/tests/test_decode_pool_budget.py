"""The native decode pool's limit follows the live build memory budget.

A build holds every chunk it decodes until it composes them (256 MB for one
8192 px level), so the pool's old 128 MB limit was below a single large
build. See ``getortho._sync_decode_memory_limit``.
"""
import types

from autoortho import getortho


def test_decode_pool_limit_follows_the_live_build_budget(monkeypatch):
    limits = []
    native = types.SimpleNamespace(set_decode_build_memory=limits.append)
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: native)
    gate = getortho._live_tile_admission
    monkeypatch.setattr(gate, "_budget", gate.byte_budget)
    monkeypatch.setattr(
        getortho.TileCacher, "cache_mem_lim", getortho.TileCacher.cache_mem_lim
    )
    getortho._sync_decode_memory_limit()
    assert limits == [gate.byte_budget]
    # The governor changing the cache limit moves the build budget too.
    getortho._on_live_cache_limit(8)
    assert limits[-1] == getortho._live_build_memory_bytes(8) == gate.byte_budget


def test_decode_pool_limit_skips_libraries_without_a_setter(monkeypatch):
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: None)
    getortho._sync_decode_memory_limit()  # no error

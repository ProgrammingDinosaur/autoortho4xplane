"""The parent splits the download broker's settings across several processes."""

import importlib
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _autoortho():
    return importlib.import_module("autoortho")


def test_split_broker_kwargs_shares_only_the_rate():
    mod = _autoortho()
    parts = mod._split_broker_kwargs(
        {
            "max_concurrency": 320,
            "max_connections": 160,
            "origin_initial_concurrency": 160,
            "origin_max_concurrency": None,
            "max_pending": 320,
            "max_bytes_per_second": 4_000_000.0,
            "profile_environment": {"A": "1"},
        },
        3,
    )
    assert len(parts) == 3
    for part in parts:
        # Concurrency stays whole: URL routing can put more than a third on one.
        assert part["max_concurrency"] == 320
        assert part["max_connections"] == 160
        assert part["origin_initial_concurrency"] == 160
        assert part["origin_max_concurrency"] is None
        assert part["max_pending"] == 320  # the workers' client bounds the total
        assert abs(part["max_bytes_per_second"] - 4_000_000 / 3) < 1e-6
    # Each broker gets its own copy of the profiler environment.
    parts[0]["profile_environment"]["B"] = "2"
    assert "B" not in parts[1]["profile_environment"]


def test_broker_process_count_setting_or_one_per_four_threads(monkeypatch):
    mod = _autoortho()
    mount = mod.AOMount.__new__(mod.AOMount)
    mount.cfg = SimpleNamespace()
    monkeypatch.setattr(mod.aoconfig, "resolve_provider_setting", lambda name, cfg: 0)
    for cpus, expected in ((2, 1), (8, 2), (12, 3), (32, 4), (128, 4)):
        monkeypatch.setattr(mod.os, "cpu_count", lambda c=cpus: c)
        assert mount._broker_process_count() == expected
    monkeypatch.setattr(mod.aoconfig, "resolve_provider_setting", lambda name, cfg: 2)
    assert mount._broker_process_count() == 2

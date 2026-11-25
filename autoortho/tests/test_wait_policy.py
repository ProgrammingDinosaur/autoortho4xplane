"""[streaming] texture_wait_policy (hybrid read path plan, N5 / S1)."""

import pytest

from autoortho import getortho, streaming_options, wait_bands, wait_policy
from autoortho.prefetch import PrefetchQualityMode

CFG = streaming_options.CFG


@pytest.fixture
def policy(monkeypatch):
    """Set the policy; nothing is hand-set unless a test says so."""
    monkeypatch.setattr(CFG, "_explicit_options", {}, raising=False)
    monkeypatch.setattr(CFG.streaming, "band_max_wait_seconds", "6.0", raising=False)
    monkeypatch.setattr(CFG.streaming, "far_wait_seconds", "0.5", raising=False)
    monkeypatch.setattr(CFG.streaming, "near_wait_seconds", "60.0", raising=False)
    monkeypatch.setattr(CFG.autoortho, "strict_target_deadline_sec", 120.0, raising=False)
    monkeypatch.setattr(CFG.autoortho, "prefetch_quality_mode", "responsive", raising=False)

    def set_policy(value, hand_set=()):
        monkeypatch.setattr(CFG.streaming, "texture_wait_policy", value, raising=False)
        explicit = {}
        for name in hand_set:
            section, key = name.split(".", 1)
            explicit.setdefault(section, set()).add(key)
        monkeypatch.setattr(CFG, "_explicit_options", explicit, raising=False)

    return set_policy


def _waits(starting):
    return tuple(
        wait_bands.wait_seconds(band, starting)
        for band in (wait_bands.NEAR, wait_bands.MIDDLE, wait_bands.FAR)
    )


def test_balanced_is_todays_behaviour(policy):
    policy("balanced")
    assert _waits(False) == (6.0, 6.0, 0.5)
    assert _waits(True) == (60.0, 60.0, 0.5)
    assert getortho._prefetch_quality_mode() == PrefetchQualityMode.RESPONSIVE


def test_smooth_never_waits_in_flight(policy):
    policy("smooth")
    assert _waits(False) == (0.0, 0.0, 0.0)
    # The loading screen still waits.
    assert _waits(True) == (60.0, 60.0, 0.5)
    assert getortho._prefetch_quality_mode() == PrefetchQualityMode.RESPONSIVE


def test_quality_waits_for_the_strict_deadline(policy):
    policy("quality")
    assert _waits(False) == (120.0, 120.0, 120.0)
    assert _waits(True) == (60.0, 60.0, 0.5)
    assert getortho._prefetch_quality_mode() == PrefetchQualityMode.STRICT_TARGET
    # A tile's budget covers the longest wait.
    assert wait_bands.tile_budget_seconds() >= 120.0


def test_hand_set_values_are_kept(policy, monkeypatch):
    monkeypatch.setattr(CFG.streaming, "band_max_wait_seconds", "8.0", raising=False)
    monkeypatch.setattr(CFG.autoortho, "prefetch_quality_mode", "prefer_target", raising=False)
    policy("smooth", hand_set=("streaming.band_max_wait_seconds",))
    assert _waits(False) == (8.0, 8.0, 0.0)
    policy("quality", hand_set=(
        "streaming.band_max_wait_seconds", "autoortho.prefetch_quality_mode",
    ))
    assert _waits(False) == (8.0, 8.0, 120.0)
    assert getortho._prefetch_quality_mode() == PrefetchQualityMode.PREFER_TARGET
    assert wait_policy.overridden_by_hand() == (
        "streaming.band_max_wait_seconds", "autoortho.prefetch_quality_mode",
    )


def test_a_governor_change_is_not_hand_set(policy, monkeypatch):
    """The governor moves band_max_wait_seconds at run time: smooth wins."""
    policy("smooth")
    monkeypatch.setattr(CFG.streaming, "band_max_wait_seconds", "12.0", raising=False)
    assert wait_bands.wait_seconds(wait_bands.NEAR, False) == 0.0


def test_timeouts_keep_the_configured_wait(policy):
    policy("smooth")
    assert wait_bands.http_read_timeout_seconds() == 7.0
    assert getortho._live_queue_timeout_seconds() == 12.0


def test_unknown_policy_reads_as_balanced(policy):
    policy("whatever")
    assert wait_policy.current() == "balanced"
    assert _waits(False) == (6.0, 6.0, 0.5)


def test_every_policy_has_a_label_and_a_sentence():
    for value in wait_policy.POLICIES:
        assert wait_policy.LABELS[value]
        assert wait_policy.DESCRIPTIONS[value].endswith(".")
    assert "until X-Plane loads" in wait_policy.DESCRIPTIONS["smooth"]


def test_a_near_read_under_smooth_gets_no_wait(policy, monkeypatch, tmp_path):
    """The read's own budget is empty in flight: it is answered with what
    is at hand while the downloads it started go on."""
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    monkeypatch.setattr(getortho, "current_camera", lambda: None)
    monkeypatch.setattr(getortho, "flight_is_starting", lambda near_read=False: False)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    try:
        policy("smooth")
        budget, band, starting = tile._band_wait(0, None)
        assert band == wait_bands.NEAR and not starting
        assert budget.max_seconds == 0.0
        policy("balanced")
        assert tile._band_wait(0, None)[0].max_seconds == 6.0
    finally:
        tile.close()


def test_registered_as_a_user_setting():
    from autoortho.aoconfig import CFG as cfg
    spec = cfg.registry().get("streaming", "texture_wait_policy")
    assert spec is not None
    assert spec.default_text == "balanced"
    assert spec.owner == "user" and spec.live


def test_quality_still_fills_chunks_with_no_imagery(policy, monkeypatch, tmp_path):
    """Under quality, fallbacks are off so nothing coarser is served while
    the real chunk may still come; a chunk the provider has no imagery for
    (the sea at ZL16) never comes and gets the lower zoom, not missing_color.
    Every other chunk stays strict."""
    monkeypatch.setattr(getortho, "dynamic_dds_cache", None)
    monkeypatch.setattr(CFG.autoortho, "fallback_level", "cache", raising=False)
    tile = getortho.Tile(0, 0, "BI", 16, cache_dir=str(tmp_path), max_zoom=16)
    try:
        policy("quality")
        assert tile.get_fallback_level() == 0
        assert tile._no_imagery_fallback_level() == 1
        grid = tile._get_chunk_grid(tile.max_zoom)
        sea, land = grid.ensure_range(0, 2)
        sea.failure_reason = "no_imagery"
        land.failure_reason = None

        resolver = tile._new_fallback_resolver(tile.get_fallback_level())
        assert resolver.level_for(sea.col, sea.row, tile.max_zoom) == 1
        assert resolver.level_for(land.col, land.row, tile.max_zoom) == 0

        lower = (b"\x01" * 16, "lower_zl_cache")
        monkeypatch.setattr(
            resolver, "_try_disk_cache_fallback", lambda *a, **k: lower[0]
        )
        assert resolver.resolve_with_source(sea.col, sea.row, tile.max_zoom) == lower
        assert resolver.resolve_with_source(land.col, land.row, tile.max_zoom) is None

        # A configured fallback_level = none is the user's choice: kept.
        monkeypatch.setattr(CFG.autoortho, "fallback_level", "none", raising=False)
        assert tile._no_imagery_fallback_level() == 0
        policy("balanced")
        assert tile.get_fallback_level() == 0
    finally:
        tile.close()

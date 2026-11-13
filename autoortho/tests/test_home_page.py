"""Phase 9: the state-based Home and its snapshot."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import home_snapshot as hs  # noqa: E402
from governor import Decision  # noqa: E402
from ui.pages.home_page import HomePage  # noqa: E402

GB = 1024 ** 3


def _governor(**overrides):
    snap = {
        "mode": "auto",
        "state": "network_room",
        "state_text": "Network-bound, line has room",
        "fidelity": 0.96,
        "mbps": 142.0,
        "error_share": 0.003,
        "cpu": 45.0,
        "time_to_sharp_s": 38.0,
        "line_mbps": 160.0,
        "bandwidth_mbps": None,
        "ram_limit_bytes": 8 * GB,
        "graphics_memory_bytes": int(3.1 * GB),
        "vram_budget_bytes": 7 * GB,
        "pinned": ["autoortho.live_tile_admission", "cache.cache_mem_limit"],
        "decisions": [
            Decision(90.0, "autoortho.provider_origin_max_concurrency", "Parallel downloads",
                     128, 144, "network_room", "network-bound: queue full",
                     applied=True),
        ],
        "timeline": [{"t": float(t), "fidelity": 0.99 if t < 200 else 0.7}
                     for t in range(0, 300, 2)],
        "starting": False,
        "home": {
            "textures": 18420, "cache_share": 0.71, "downloaded_bytes": int(3.8 * GB),
            "building_now": 9, "horizon_m": 26 * 1852, "phase": "cruise",
            "vertical": "climbing", "altitude_ft": 8400.0, "speed_kt": 260.0,
            "connected": True, "paused": False, "ao_memory_bytes": int(6.1 * GB),
            "free_ram_bytes": 12 * GB, "total_ram_bytes": 32 * GB,
        },
    }
    snap.update(overrides)
    return snap


def _flying(**overrides):
    return hs.build(runtime="running", governor=_governor(**overrides), packs=4,
                    xplane_connected=True, provider="BI", route="EDDM → LOWI",
                    target_text="ZL17 · airports ZL18", cpu_limit_cores=10,
                    card_bytes=12 * GB, now=102.0)


# -- the snapshot --------------------------------------------------------------


def test_snapshot_while_flying_answers_the_four_questions():
    snap = _flying()
    assert snap.state == hs.FLYING and snap.pill == "Flying with AutoOrtho"
    assert snap.strip_detail == "4 scenery packs · X-Plane connected"
    assert snap.flight_line == "EDDM → LOWI · climbing 8,400 ft AGL · 260 kt · BI"
    assert snap.hero_value == "96 % sharp at target"
    assert snap.sharp_after == "Sharp 38 s after the last area load"
    network, computer, graphics = snap.gauges
    assert network.value == "142 Mbit/s of ~160" and network.bottleneck
    assert "0.3 % retries" in network.detail
    assert computer.value == "CPU 45 % · limit 10 cores"
    assert computer.detail == "RAM 6.1 GB of 8.0 GB (AutoOrtho)"
    assert not computer.bottleneck
    assert graphics.value == "~3.1 GB photo textures"
    assert graphics.detail == "of 7.0 GB budget (12.0 GB card)"
    assert snap.activity_lines[0] == (
        "18,420 textures served · 71 % from cache · 3.8 GB downloaded")
    assert snap.activity_lines[1] == "Building 9 now · Ahead of you: next ~6 min / 26 nm ready"
    assert snap.tuning_visible
    assert snap.tuning_lines[0].startswith("raise parallel downloads 128→144")
    assert snap.tuning_lines[0].endswith("12 s ago")
    assert snap.tuning_lines[1] == "1 changes this flight · 2 pinned values"
    assert snap.summary == "AutoOrtho · 96 % sharp · 142 Mbit/s · Flying"
    assert "limits the scenery now" in network.sentence


def test_trend_buckets_mark_blurry_moments():
    trend = _flying().trend
    assert trend and len(trend) <= hs.TREND_POINTS
    assert trend[0] == pytest.approx(0.99)
    assert trend[-1] == pytest.approx(0.7)


def test_states():
    stopped = hs.build(runtime="stopped", packs=3)
    assert stopped.state == hs.STOPPED and stopped.strip_detail == "3 scenery packs configured"
    after = hs.build(runtime="stopped", last_flight={"verdict": "Smooth flight"},
                     show_last_flight=True)
    assert after.state == hs.AFTER_FLIGHT
    starting = hs.build(runtime="running", governor=_governor(
        home={"connected": False}), packs=2, xplane_connected=False)
    assert starting.state == hs.STARTING
    assert starting.loading_lines[1] == "Waiting for X-Plane…"
    assert starting.loading_lines[2] == "Loading area: 96 % of nearby scenery sharp"
    paused = hs.build(runtime="running", governor=_governor(
        home={"connected": True, "paused": True}), xplane_connected=True)
    assert paused.state == hs.PAUSED and paused.pill == "Paused"
    assert hs.build(runtime="starting").state == hs.STARTING


def test_off_mode_hides_the_tuning_block_and_low_system_ram_adds_a_line():
    snap = _flying(mode="off")
    assert not snap.tuning_visible
    home = dict(_governor()["home"], free_ram_bytes=int(1.0 * GB))
    computer = _flying(home=home).gauges[1]
    assert "system free RAM low: 1.0 GB" in computer.detail
    assert computer.level == "error"


def test_graphics_chip_and_unknown_capacity():
    snap = _flying(state="vram", line_mbps=None, vram_budget_bytes=None)
    network, _computer, graphics = snap.gauges
    assert graphics.bottleneck and not network.bottleneck
    assert network.value == "142 Mbit/s" and network.share is None


def test_last_flight_lines():
    lines = hs.last_flight_lines({
        "verdict": "A few blurry moments",
        "fidelity_by_phase": {"departure_arrival": 0.81, "cruise": 0.99},
        "distance_nm": 212.0, "duration_min": 58.0, "downloaded_gb": 3.2,
        "cache_share": 0.64, "ram_brake_activations": 1,
        "learned": {"autoortho.provider_origin_max_concurrency": 144.0},
    })
    assert lines[0] == "Departure and arrival 81 % · Cruise 99 %"
    assert lines[1] == "212 nm · 58 min · 3.2 GB downloaded · 64 % from cache"
    assert lines[2] == "Guards stepped in 1 time"
    assert lines[3] == "Learned for next time: provider_origin_max_concurrency 144"
    assert hs.verdict_level({"verdict": "A few blurry moments"}) == "warning"


# -- the page --------------------------------------------------------------


def _shown(widget):
    return not widget.isHidden()


def test_stopped_layout_and_readiness(qt_app):
    page = HomePage()
    assert page.state_pill.text() == "■ Stopped"
    assert _shown(page.stopped_block) and not _shown(page.flying_block)
    assert not _shown(page.loading_card)
    page.set_readiness_state("Ready", "ignored when ready")
    assert page.ready_card.value_label.text() == "✓ Ready to fly"
    assert page.ready_card.detail_label.isHidden()
    page.set_readiness_state("2 issue(s)", "X-Plane folder, PC resources")
    assert page.ready_card.value_label.text() == "! 2 issue(s)"
    assert page.ready_card.detail_label.text() == "X-Plane folder, PC resources"
    assert page.primary_button.text() == "Start streaming"


def test_flying_layout(qt_app):
    page = HomePage()
    page.set_snapshot(_flying())
    assert _shown(page.flying_block) and not _shown(page.stopped_block)
    assert page.state_pill.text() == "● Flying with AutoOrtho"
    assert page.fidelity_label.text() == "96 % sharp at target"
    assert page.gauges["network"].chip.isVisibleTo(page.gauges["network"])
    assert not page.gauges["computer"].chip.isVisibleTo(page.gauges["computer"])
    assert "limits the scenery now" in page.gauges["network"].accessibleDescription()
    assert "Building 9 now" in page.activity_label.text()
    assert _shown(page.tuning_card)
    assert page.primary_button.text() == "Stop streaming"
    assert page.windowTitle() == ""  # the main window owns the title
    assert "blurry" in page.sparkline.accessibleDescription()


def test_paused_dims_and_freezes(qt_app):
    page = HomePage()
    page.set_snapshot(_flying())
    paused = hs.build(runtime="running", governor=_governor(
        fidelity=0.5, home=dict(_governor()["home"], paused=True)),
        xplane_connected=True)
    page.set_snapshot(paused)
    assert page.state_pill.text() == "❚❚ Paused"
    assert page._dim.opacity() < 1.0
    assert page.fidelity_label.text() == "96 % sharp at target"   # frozen


def test_starting_layout(qt_app):
    page = HomePage()
    page.set_snapshot(hs.build(runtime="starting", packs=4))
    assert _shown(page.loading_card) and not _shown(page.flying_block)
    assert "Mounting 4 scenery packs" in page.loading_label.text()


def test_after_flight_card_and_dismiss(qt_app):
    page = HomePage()
    summary = {"verdict": "Smooth flight", "fidelity_by_phase": {"cruise": 0.99},
               "distance_nm": 100.0, "duration_min": 30.0}
    page.set_snapshot(hs.build(runtime="stopped", last_flight=summary,
                               show_last_flight=True))
    assert _shown(page.last_flight_card)
    assert page.last_flight_verdict.text() == "Smooth flight"
    assert "100 nm · 30 min" in page.last_flight_details.text()
    assert _shown(page.dismiss_last_flight_button)
    seen = []
    page.last_flight_dismissed.connect(lambda: seen.append(True))
    page.dismiss_last_flight_button.click()
    assert seen == [True]
    # Stopped (not right after the flight): the card, collapsed.
    page.set_snapshot(hs.build(runtime="stopped", last_flight=summary))
    assert "100 nm" not in page.last_flight_details.text()
    assert page.dismiss_last_flight_button.isHidden()


def test_shortcuts_badge_and_compact(qt_app):
    page = HomePage()
    seen = []
    page.fix_config_requested.connect(lambda: seen.append("fix"))
    page.install_scenery_requested.connect(lambda: seen.append("install"))
    page.open_diagnostics_requested.connect(lambda: seen.append("diag"))
    page.open_map_requested.connect(lambda: seen.append("map"))
    page.open_library_requested.connect(lambda: seen.append("library"))
    page.primary_action_requested.connect(lambda: seen.append("run"))
    for button in (page.fix_config_button, page.install_scenery_button,
                   page.open_diagnostics_button, page.open_map_button,
                   page.library_button, page.primary_button):
        button.click()
    assert seen == ["fix", "install", "diag", "map", "library", "run"]
    page.set_task("1 active")
    assert page.library_button.text() == "Scenery library (1 active)"
    page.set_snapshot(_flying())
    page.set_compact(True)
    assert page.footer.isHidden() and page.tuning_card.isHidden()
    assert _shown(page.hero)
    page.set_compact(False)
    assert _shown(page.tuning_card)


def test_target_lights(qt_app):
    from guards import GuardResult, Level

    page = HomePage()
    page.set_target_lights([
        GuardResult("vram", Level.OK, "Graphics memory", "Fits."),
        GuardResult("ram", Level.WARN, "RAM", "Close."),
    ])
    assert "Graphics memory" in page.target_lights_label.text()
    assert "!" in page.target_lights_label.text()
    assert "RAM: Close." in page.target_card.accessibleDescription()


def test_tile_radar(qt_app):
    radar = [[1.0, 2.0, 0], [-3.0, 0.5, 1], [10.0, -4.0, 2], [0.0, 30.0, 3]]
    snap = _flying(home=dict(_governor()["home"], radar=radar, heading=90.0),
                   values={"autoortho.prefetch_lookahead": 10})
    assert snap.radar == radar and snap.heading == 90.0
    assert snap.corridor_nm == pytest.approx(260 / 60 * 10)
    page = HomePage()
    page.set_snapshot(snap)
    text = page.radar.accessibleDescription()
    assert text.startswith("4 tiles within 60 nm")
    assert "1 sharp at target" in text and "1 not loaded" in text
    page.radar.grab()   # paints
    seen = []
    page.open_map_requested.connect(lambda: seen.append(True))
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    page.radar.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    assert seen == [True]


def test_learned_values_use_lever_labels():
    lines = hs.last_flight_lines({
        "learned": {"autoortho.provider_origin_max_concurrency": 144.0},
        "learned_labels": {"autoortho.provider_origin_max_concurrency": "Parallel downloads"},
    })
    assert lines == ["Learned for next time: 144 parallel downloads"]

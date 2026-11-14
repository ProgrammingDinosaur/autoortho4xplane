"""Auto-Tuning page, Advise notices, pins, report section, mount lifecycle."""

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import aoconfig  # noqa: E402
import connection_test  # noqa: E402
import governor as gv  # noqa: E402
import hardware  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from hardware import HardwareInfo  # noqa: E402

GB = 1024 ** 3
PC = HardwareInfo(16, 8, 32 * GB, 12 * GB, "Test GPU", "registry")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(connection_test, "RESULT_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(gv, "DATA_DIR", str(tmp_path / "gov"))
    hardware.set_for_tests(PC)
    yield
    hardware.set_for_tests(None)


def test_report_renders_flight_verdict():
    import diagnostics

    lines = diagnostics._render_flight({
        "verdict": "A few blurry moments", "fidelity": 0.93,
        "fidelity_by_phase": {"cruise": 0.99, "departure_arrival": 0.81},
        "mode": "auto", "decisions": 7, "kept": 5, "reverted": 2,
        "peak_mbps": 142.0, "ram_brake_activations": 1,
        "disk_brake_activations": 0,
        "learned": {"autoortho.provider_origin_max_concurrency": 144.0},
    })
    text = "\n".join(lines)
    assert "## Flight verdict" in text and "**A few blurry moments**" in text
    assert "| Cruise | 99 % |" in text
    assert "7 decisions (5 kept, 2 undone)" in text
    assert "provider_origin_max_concurrency 144" in text
    assert "Blurry moments by cause" not in text
    assert diagnostics._render_flight({}) == []


def test_report_renders_blurry_moments_by_cause():
    import diagnostics

    lines = diagnostics._render_flight({
        "verdict": "A few blurry moments", "fidelity": 0.93,
        "misses_by_phase": {
            "cruise": {"never_wide": 6, "late": 2},
            "departure_arrival": {"late": 3, "evicted": 1},
            "taxi": {},
        },
    })
    text = "\n".join(lines)
    assert "### Blurry moments by cause" in text
    assert "| Cause | Cruise | Departure and arrival |" in text   # taxi had none
    assert "| Never queued, outside the corridor | 75 % | 0 % |" in text
    assert "| Queued, not ready in time | 25 % | 75 % |" in text
    assert "| Ready, then dropped from memory | 0 % | 25 % |" in text
    assert "| Blurry tiles counted | 8 | 4 |" in text
    assert "Queued at a coarser level only" not in text


def test_mount_lifecycle_runs_and_summarises(tmp_path, monkeypatch):
    import autoortho as ao_module
    import aostats

    mount = ao_module.AOMount.__new__(ao_module.AOMount)
    mount.cfg = AOConfig(str(tmp_path / ".autoortho"))
    mount.cfg.set_value("governor", "mode", "auto")
    mount.cfg.set_value("governor", "record_samples", "False")
    mount._download_broker = None
    published = []
    mount.publish_levers = lambda levers: published.append(levers) or levers
    gv.LearnedProfiles().put(
        mount._governor_profile_key(gv, connection_test),
        {"autoortho.provider_origin_max_concurrency": 160},
    )
    service = mount.start_governor()
    assert service is not None and service.governor.mode == "auto"
    assert published and published[0] == {"autoortho.provider_origin_max_concurrency": 160}
    stored = {}
    monkeypatch.setattr(aostats, "set_stat", lambda k, v: stored.__setitem__(k, v))
    summary = mount.stop_governor()
    assert summary["mode"] == "auto"
    assert stored["governor/summary"]["verdict"]
    assert mount.governor_service is None
    learned = gv.LearnedProfiles().get(mount._governor_key)
    assert learned["autoortho.provider_origin_max_concurrency"] == 160


def test_advise_flight_starts_from_accepted_suggestions(tmp_path):
    import autoortho as ao_module

    mount = ao_module.AOMount.__new__(ao_module.AOMount)
    mount.cfg = AOConfig(str(tmp_path / ".autoortho"))
    mount.cfg.set_value("governor", "mode", "advise")
    mount.cfg.set_value("governor", "record_samples", "False")
    mount._download_broker = None
    published = []
    mount.publish_levers = lambda levers: published.append(levers) or levers
    key = mount._governor_profile_key(gv, connection_test)
    store = gv.LearnedProfiles()
    # What an Auto flight settled on stays out of an Advise flight...
    store.put(key, {"autoortho.provider_origin_max_concurrency": 160})
    # ...but a suggestion the user accepted applies.
    store.put_applied(key, "autoortho.live_tile_admission", 20)
    service = mount.start_governor()
    try:
        assert published == [{"autoortho.live_tile_admission": 20}]
        assert service.governor.values["autoortho.live_tile_admission"] == 20
        assert not service.governor.pinned
    finally:
        mount.stop_governor()


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    from config_ui_qt import ConfigUI
    from ui.runtime_state import RuntimeState

    monkeypatch.setattr(ConfigUI, "refresh_scenery_list",
                        lambda self: self.scenery_layout.addStretch())
    monkeypatch.setattr(ConfigUI, "start_update_check", lambda self: None)
    for name in ("scenery", "cache", "downloads", "X-Plane"):
        (tmp_path / name).mkdir()
    config = AOConfig(str(tmp_path / ".autoortho"))
    config.paths.scenery_path = str(tmp_path / "scenery")
    config.paths.cache_dir = str(tmp_path / "cache")
    config.paths.download_dir = str(tmp_path / "downloads")
    config.paths.xplane_path = str(tmp_path / "X-Plane")
    config.save()
    config.get_config()
    ui = ConfigUI(config)
    for worker in (ui.readiness_worker, ui.storage_scan_worker):
        if worker is not None and worker.isRunning():
            worker.wait(2000)
    qt_app.processEvents()
    yield ui
    ui.mount_monitor_timer.stop()
    ui._tuning_timer.stop()
    ui._set_runtime_state(RuntimeState.STOPPED)
    ui.settings_session.mark_applied(ui._snapshot_settings())
    ui.close()
    ui.deleteLater()
    qt_app.processEvents()


def _fake_service(mode="advise"):
    levers = gv.levers_for(aoconfig.CFG)
    values = {name: spec.minimum for name, spec in levers.items()}
    governor = gv.Governor(levers, values, mode=mode)
    service = gv.GovernorService(governor, read_stat=lambda k: 0)
    return service


def test_tuning_page_lists_levers_when_not_streaming(qt_app, config_ui):
    page = config_ui.tuning_page
    config_ui._refresh_tuning()
    assert page.table.rowCount() == len(gv.levers_for(config_ui.cfg))
    assert "Not streaming" in page.status_label.text()
    labels = {page.table.item(r, 0).text() for r in range(page.table.rowCount())}
    assert "Parallel downloads" in labels


def test_mode_saves_and_mirrors_essentials(qt_app, config_ui):
    page = config_ui.tuning_page
    page.set_mode("auto")
    page.mode_combo.currentIndexChanged.emit(page.mode_combo.currentIndex())
    qt_app.processEvents()
    assert config_ui.essentials_page.governor_combo.currentData() == "auto"
    assert config_ui.save_config(refresh_scenery=False) is True
    assert AOConfig(config_ui.cfg.conf_file).governor.mode == "auto"
    config_ui.essentials_page.governor_combo.setCurrentIndex(
        config_ui.essentials_page.governor_combo.findData("off")
    )
    assert page.mode_combo.currentData() == "off"


def test_advise_notice_and_apply(qt_app, config_ui):
    service = _fake_service("advise")
    decision = gv.Decision(10.0, "autoortho.live_tile_admission", "Live tiles at once",
                           16, 20, "compute", "compute-bound")
    service.decisions.append(decision)
    config_ui.governor_service = service
    config_ui._governor_key = "profile"
    config_ui._refresh_tuning()
    notice = config_ui._notices.get("governor_advice")
    assert notice is not None and "suggests" in notice.text
    config_ui._on_notice_action("governor_advice", "apply")
    # The suggestion is AutoOrtho's value for the next flights, not a pin.
    assert not config_ui.cfg.is_custom("autoortho", "live_tile_admission")
    assert "autoortho.live_tile_admission" not in service.governor.pinned
    assert service.governor.values["autoortho.live_tile_admission"] == 20
    assert gv.LearnedProfiles().applied("profile") == {"autoortho.live_tile_admission": 20.0}
    assert "governor_advice" not in config_ui._notices
    config_ui._refresh_tuning()
    rows = {config_ui.tuning_page.table.item(r, 0).text(): r
            for r in range(config_ui.tuning_page.table.rowCount())}
    sources = [config_ui.tuning_page.table.item(rows["Live tiles at once"], c).text()
               for c in range(config_ui.tuning_page.table.columnCount())
               if config_ui.tuning_page.table.item(rows["Live tiles at once"], c)]
    assert "Learned" in sources
    # Reset learned settings forgets it.
    config_ui._reset_learned()
    assert gv.LearnedProfiles().applied("profile") == {}
    config_ui.governor_service = None


def test_pin_and_unpin(qt_app, config_ui):
    name = "autoortho.background_builder_workers"
    config_ui._toggle_pin(name, True)
    reloaded = AOConfig(config_ui.cfg.conf_file)
    assert reloaded.source("autoortho", "background_builder_workers") == "custom"
    config_ui._toggle_pin(name, False)
    reloaded = AOConfig(config_ui.cfg.conf_file)
    assert reloaded.source("autoortho", "background_builder_workers") != "custom"
    assert config_ui.settings_session.dirty is False


def test_unreachable_notice_navigates(qt_app, config_ui):
    service = _fake_service("auto")
    service.governor.notice = "The target reached 70 % sharp; the limit is your connection."
    config_ui.governor_service = service
    config_ui._refresh_tuning()
    assert "governor_unreachable" in config_ui._notices
    config_ui._on_notice_action("governor_unreachable", "limits")
    assert config_ui.categorized_settings_page.category_list.currentItem().text() == "Limits"
    config_ui.governor_service = None


def test_export_and_reset(qt_app, config_ui, tmp_path):
    path = config_ui._export_tuning_report()
    import json

    data = json.loads(open(path, encoding="utf-8").read())
    assert data["levers"]["cache.cache_mem_limit"]["label"] == "Memory cache"
    gv.LearnedProfiles().put("k", {"a.b": 1})
    config_ui._reset_learned()
    assert gv.LearnedProfiles().get("k") == {}


def test_timeline_chart_markers(qt_app):
    from ui.pages.tuning_page import TimelineChart

    chart = TimelineChart()
    chart.resize(400, 200)
    chart.set_points([
        {"t": i, "fidelity": 0.9, "mbps": 50, "cpu": 40, "ram_gb": 3,
         "decision": "raise x" if i == 5 else ""}
        for i in range(10)
    ])
    chart.grab()   # paints, which records the marker positions
    seen = []
    chart.marker_clicked.connect(seen.append)
    x, text = chart._markers[0]
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent

    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, 50), QPointF(x, 50),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    chart.mousePressEvent(event)
    assert seen == ["raise x"]


def test_protect_sim_fps_saves_and_reaches_governor(qt_app, config_ui):
    service = _fake_service("auto")
    config_ui.governor_service = service
    check = config_ui.protect_sim_fps_check
    assert check.isChecked()
    check.setChecked(False)
    qt_app.processEvents()
    assert config_ui.settings_session.dirty
    assert config_ui.save_config(refresh_scenery=False) is True
    assert AOConfig(config_ui.cfg.conf_file).limits.protect_sim_fps is False
    assert service.governor.protect_sim_fps is False
    config_ui.governor_service = None


def test_wait_sliders_save_and_apply_live(qt_app, config_ui):
    from ui.runtime_state import RuntimeState

    published = []
    config_ui.publish_levers = lambda levers: published.append(levers)
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    config_ui.band_wait_slider.setValue(9)
    qt_app.processEvents()
    assert config_ui.save_config(refresh_scenery=False) is True
    reloaded = AOConfig(config_ui.cfg.conf_file)
    assert float(reloaded.streaming.band_max_wait_seconds) == 9.0
    assert reloaded.is_custom("streaming", "band_max_wait_seconds")
    assert {"streaming.band_max_wait_seconds": 9.0} in published or any(
        float(levers.get("streaming.band_max_wait_seconds", 0)) == 9.0
        for levers in published
    )
    # Unchanged waits are not republished (the governor may have moved them).
    published.clear()
    assert config_ui.save_config(refresh_scenery=False) is True
    assert not any("streaming.near_wait_seconds" in levers for levers in published)
    assert not any("streaming.band_max_wait_seconds" in levers for levers in published)


def test_texture_cap_permission_saves_and_reaches_governor(qt_app, config_ui):
    service = _fake_service("auto")
    config_ui.governor_service = service
    check = config_ui.allow_texture_cap_check
    assert check is config_ui.tuning_page.texture_cap_check
    assert not check.isChecked()
    check.setChecked(True)
    qt_app.processEvents()
    assert config_ui.settings_session.dirty
    assert config_ui.save_config(refresh_scenery=False) is True
    assert AOConfig(config_ui.cfg.conf_file).governor.allow_texture_cap is True
    assert gv.TEXTURE_CAP_LEVER in service.governor.levers
    config_ui.governor_service = None
    config_ui._refresh_tuning()
    labels = {config_ui.tuning_page.table.item(r, 0).text()
              for r in range(config_ui.tuning_page.table.rowCount())}
    assert "Texture size cap" in labels


def test_home_follows_the_governor_and_the_last_flight(qt_app, config_ui, monkeypatch):
    from ui.runtime_state import RuntimeState

    home = config_ui.home_page
    service = _fake_service("auto")
    service.home = {"connected": True, "altitude_ft": 5000.0, "speed_kt": 200.0,
                    "textures": 10, "building_now": 2}
    config_ui.governor_service = service
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    config_ui._refresh_tuning()
    assert home.snapshot.state in ("flying", "starting")
    assert config_ui.windowTitle().startswith("AutoOrtho ·")
    # Unmount: the last-flight card, until dismissed.
    config_ui.governor_service = None
    config_ui.last_flight = {"verdict": "Smooth flight", "distance_nm": 12.0}
    config_ui._after_flight = True
    config_ui._set_runtime_state(RuntimeState.STOPPED)
    config_ui._refresh_tuning()
    assert home.snapshot.state == "after_flight"
    assert not home.last_flight_card.isHidden()
    assert "ver" in config_ui.windowTitle()
    home.dismiss_last_flight_button.click()
    assert home.snapshot.state == "stopped"


def test_minimize_to_tray_while_streaming(qt_app, config_ui, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    from ui.runtime_state import RuntimeState

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    config_ui.hide_check.setChecked(True)
    config_ui.show()
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    assert config_ui._tray is not None
    assert config_ui._hidden_to_tray and not config_ui.isVisible()
    config_ui._tray.setVisible(True)
    snapshot = SimpleNamespace(summary="AutoOrtho · 96 % sharp · 142 Mbit/s · Flying")
    config_ui._update_tray_summary(snapshot)
    assert config_ui._tray.toolTip().endswith("96 % sharp · 142 Mbit/s · Flying")
    config_ui._set_runtime_state(RuntimeState.STOPPED)
    assert not config_ui._hidden_to_tray and config_ui.isVisible()
    config_ui.hide_check.setChecked(False)


# -- pinned settings in Auto mode ---------------------------------------------


def _pin(config_ui, key, value):
    config_ui.cfg.set_value("autoortho", key, value)
    config_ui.cfg.pin("autoortho", key)


def test_pinned_tuned_values_lists_only_settings_autoortho_manages(tmp_path):
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.set_value("autoortho", "live_builder_concurrency", "5")
    cfg.set_value("autoortho", "prefetch_radius_nm", "45")
    cfg.set_value("autoortho", "using_custom_tiles", "True")   # the user's, not tuning
    assert cfg.pinned_tuned_values() == [
        "autoortho.live_builder_concurrency", "autoortho.prefetch_radius_nm",
    ]


def test_auto_mode_offers_to_hand_pinned_settings_back(qt_app, config_ui):
    _pin(config_ui, "live_builder_concurrency", "5")
    _pin(config_ui, "buffer_pool_size", "10")
    config_ui._refresh_pinned_tuning_notice()
    notice = config_ui._notices.get("pinned_tuning")
    assert notice is not None
    assert "2 settings you set yourself" in notice.text
    assert ("manage", "Let AutoOrtho manage them") in notice.actions

    config_ui._on_notice_action("pinned_tuning", "manage")
    assert "pinned_tuning" not in config_ui._notices
    reloaded = AOConfig(config_ui.cfg.conf_file)
    assert reloaded.pinned_tuned_values() == []
    assert reloaded.source("autoortho", "live_builder_concurrency") != "custom"
    assert config_ui.settings_session.dirty is False


def test_kept_pins_stay_quiet_until_a_new_one(qt_app, config_ui):
    _pin(config_ui, "live_builder_concurrency", "5")
    config_ui._refresh_pinned_tuning_notice()
    config_ui._dismiss_notice("pinned_tuning")
    config_ui._refresh_pinned_tuning_notice()
    assert "pinned_tuning" not in config_ui._notices
    reloaded = AOConfig(config_ui.cfg.conf_file)
    assert "autoortho.live_builder_concurrency" in str(reloaded.general.dismissed_pinned)

    _pin(config_ui, "prefetch_radius_nm", "45")
    config_ui._refresh_pinned_tuning_notice()
    notice = config_ui._notices.get("pinned_tuning")
    assert notice is not None and "2 settings" in notice.text


def test_no_offer_outside_auto_mode(qt_app, config_ui):
    _pin(config_ui, "live_builder_concurrency", "5")
    config_ui.cfg.governor.mode = "advise"
    config_ui._refresh_pinned_tuning_notice()
    assert "pinned_tuning" not in config_ui._notices


def test_pinning_on_the_tuning_page_is_not_offered_back(qt_app, config_ui):
    config_ui._toggle_pin("autoortho.prefetch_radius_nm", True)
    config_ui._refresh_pinned_tuning_notice()
    assert "pinned_tuning" not in config_ui._notices
    # Unpinned, then set by hand again: offered again.
    config_ui._toggle_pin("autoortho.prefetch_radius_nm", False)
    _pin(config_ui, "prefetch_radius_nm", "45")
    config_ui._refresh_pinned_tuning_notice()
    assert "pinned_tuning" in config_ui._notices

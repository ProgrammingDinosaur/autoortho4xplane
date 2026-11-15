"""Limits editor, connection test panel, setup wizard steps, Limits page."""

import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import connection_test  # noqa: E402
import hardware  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from connection_test import Result, Round  # noqa: E402
from hardware import GB, HardwareInfo  # noqa: E402
from ui.widgets.connection_test_panel import ConnectionTestPanel  # noqa: E402
from ui.widgets.limits_editor import LimitsEditor  # noqa: E402

PC = HardwareInfo(16, 8, 32 * GB, 12 * GB, "Test GPU", "registry")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(
        connection_test, "RESULT_PATH", str(tmp_path / "connection.json")
    )
    hardware.set_for_tests(PC)
    yield
    hardware.set_for_tests(None)


def _result(mbits=150.0, suggested=64):
    return Result(
        "2026-11-12T10:00:00+00:00", "BI",
        [Round(suggested, 256, 0, 4_000_000, 1.0, 0.1, mbits)], suggested,
    )


def _wait(qt_app, predicate, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_limits_editor_auto_and_custom(qt_app):
    editor = LimitsEditor()
    editor.set_hardware(PC)
    assert editor.values() == {
        "ram_gb": "auto", "vram_gb": "auto",
        "bandwidth_mbps": "auto", "cpu_cores": "auto",
    }
    assert "8.5 GB" in editor.hints["ram_gb"].text()
    assert not editor.spins["ram_gb"].isEnabled()

    changed = []
    editor.changed.connect(lambda: changed.append(1))
    editor.auto_checks["ram_gb"].setChecked(False)
    assert editor.spins["ram_gb"].isEnabled()
    assert editor.values()["ram_gb"] == "8.5"       # starts at the auto value
    editor.spins["cpu_cores"].setValue(4)            # disabled: not used
    assert editor.values()["cpu_cores"] == "auto"
    assert changed

    editor.set_values({"bandwidth_mbps": "50", "cpu_cores": "auto"})
    assert editor.values()["bandwidth_mbps"] == "50"
    assert editor.values()["ram_gb"] == "auto"
    editor.use_all_available()
    assert set(editor.values().values()) == {"auto"}


def test_connection_panel_runs_off_the_ui_thread(qt_app):
    calls = []

    def runner(maptype, progress, cancel, max_connections):
        calls.append((maptype, max_connections, threading.current_thread()))
        progress("Downloading 16 at once…")
        return _result()

    panel = ConnectionTestPanel(
        maptype=lambda: "ARC", max_connections=lambda: 96, runner=runner
    )
    assert panel.run_button.text() == "Test my connection"
    measured = []
    panel.measured.connect(measured.append)
    panel.start()
    assert _wait(qt_app, lambda: measured)
    assert calls[0][:2] == ("ARC", 96)
    assert calls[0][2] is not threading.main_thread()
    assert panel.run_button.text() == "Test again"
    assert "About 150 Mbit/s" in panel.status_label.text()


def test_connection_panel_blocked_and_failures(qt_app):
    def runner(*_args, **_kwargs):
        raise RuntimeError("no network")

    panel = ConnectionTestPanel(runner=runner)
    panel.set_blocked("Stop streaming first.")
    assert not panel.run_button.isEnabled()
    panel.start()
    assert not panel.running
    panel.set_blocked("")
    panel.start()
    assert _wait(qt_app, lambda: not panel.running and "failed" in panel.status_label.text())


# -- setup wizard ---------------------------------------------------------


def _wizard(**kwargs):
    from ui.dialogs.setup_wizard import SetupWizard

    return SetupWizard(
        initial_values={"xplane_path": "", "scenery_path": "", "cache_dir": ""},
        hardware_info=kwargs.pop("hardware_info", PC),
        **kwargs,
    )


def test_wizard_has_quality_and_limits_steps(qt_app):
    wizard = _wizard()
    pages = [wizard.page(page_id) for page_id in wizard.pageIds()]
    # The proposal's six steps: checks, X-Plane, scenery, cache, quality,
    # limits and ready.
    assert pages == [
        wizard.welcome_page,
        wizard.xplane_page,
        wizard.scenery_page,
        wizard.storage_page,
        wizard.quality_page,
        wizard.limits_page,
    ]
    assert wizard.review_page is wizard.limits_page
    assert wizard.start_now() is True
    assert wizard.storage_page.scenery_edit.parent() is not wizard.storage_page
    assert 10 <= wizard.storage_page.suggested_cache_gb() <= 100
    assert wizard.selected_target() == {
        "mode": "fixed", "max_zoom": 16, "airport_zoom": 18, "steps": [],
    }
    assert wizard.quality_page.cost_panel.result("vram") is not None


def test_wizard_altitude_preset_and_suggestion(qt_app):
    from ui.dialogs.setup_wizard import _preset_steps

    wizard = _wizard()
    page = wizard.quality_page
    page.altitude_radio.setChecked(True)
    page.preset_combo.setCurrentText("Airliner")
    target = wizard.selected_target()
    assert target["mode"] == "dynamic"
    assert target["steps"] == _preset_steps("Airliner")

    page.connection_panel.result = _result(mbits=500)
    page.cost_panel.suggest_button.click()
    target = wizard.selected_target()
    assert target["mode"] == "fixed"
    assert page.cost_panel.advice_label.text().startswith("Suggested ZL")


def test_wizard_keeps_a_custom_target_it_was_given(qt_app):
    steps = [{"altitude_ft": 0, "zoom_level": 17, "zoom_level_airports": 19}]
    wizard = _wizard(target={
        "mode": "dynamic", "max_zoom": 16, "airport_zoom": 18, "steps": steps,
    })
    assert wizard.selected_target()["steps"] == steps


def test_wizard_limits_step(qt_app):
    wizard = _wizard(limit_values={"ram_gb": "6"})
    page = wizard.limits_page
    assert page.custom_radio.isChecked()
    assert wizard.selected_limits()["ram_gb"] == "6"
    assert "6 GB (your limit)" in page.summary_label.text()

    page.auto_radio.setChecked(True)
    assert set(wizard.selected_limits().values()) == {"auto"}
    assert not page.editor.isEnabled()
    details = wizard._render_review_details()
    assert "Quality target: ZL16, airports ZL18" in details
    assert "Limits: RAM" in details


def test_wizard_limits_feed_the_cost_check(qt_app):
    wizard = _wizard()
    wizard.limits_page.custom_radio.setChecked(True)
    wizard.limits_page.editor.auto_checks["vram_gb"].setChecked(False)
    wizard.limits_page.editor.spins["vram_gb"].setValue(1)
    vram = wizard.quality_page.cost_panel.result("vram")
    assert "Over your graphics memory limit" in vram.message


# -- settings window ------------------------------------------------------


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    from config_ui_qt import ConfigUI
    from ui.runtime_state import RuntimeState

    monkeypatch.setattr(
        ConfigUI,
        "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
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
    ui._set_runtime_state(RuntimeState.STOPPED)
    ui.settings_session.mark_applied(ui._snapshot_settings())
    ui.close()
    ui.deleteLater()
    qt_app.processEvents()


def test_limits_save_and_revert(qt_app, config_ui):
    editor = config_ui.limits_editor
    editor.auto_checks["bandwidth_mbps"].setChecked(False)
    editor.spins["bandwidth_mbps"].setValue(40)
    qt_app.processEvents()
    assert config_ui.settings_session.dirty is True
    # Limits apply live while streaming: no restart needed.
    assert config_ui.settings_session.restart_required is False
    assert config_ui.save_config(refresh_scenery=False) is True
    config_ui.settings_session.mark_applied(config_ui._snapshot_settings())
    text = open(config_ui.cfg.conf_file, encoding="utf-8").read()
    assert "bandwidth_mbps = 40" in text
    assert "# ram_gb = auto" in text

    editor.auto_checks["ram_gb"].setChecked(False)
    qt_app.processEvents()
    config_ui.on_revert()
    assert config_ui.limits_editor.values()["ram_gb"] == "auto"
    assert config_ui.limits_editor.values()["bandwidth_mbps"] == "40"


def test_limits_are_locked_in_the_expert_table(qt_app, config_ui):
    page = config_ui.expert_settings_page
    for name in ("limits.ram_gb", "limits.bandwidth_mbps"):
        row = next(
            r for r in range(page.model.rowCount())
            if page.model.spec(r).name == name
        )
        assert not page.model.is_editable(page.model.spec(row))


def test_connection_test_blocked_while_streaming(qt_app, config_ui):
    from ui.runtime_state import RuntimeState

    config_ui._set_runtime_state(RuntimeState.RUNNING)
    assert not config_ui.connection_test_panel.run_button.isEnabled()
    config_ui._set_runtime_state(RuntimeState.STOPPED)
    assert config_ui.connection_test_panel.run_button.isEnabled()


def test_suggest_button_fills_the_controls(qt_app, config_ui):
    config_ui.connection_test_panel.result = _result(mbits=500)
    config_ui.max_zoom_mode_combo.setCurrentText("Dynamic")
    config_ui.target_cost_panel.suggest_button.click()
    qt_app.processEvents()
    assert config_ui.max_zoom_mode_combo.currentText() == "Fixed"
    assert "Press Apply" in config_ui.target_cost_panel.advice_label.text()
    assert config_ui.settings_session.dirty is True


def test_cost_panel_uses_connection_result(qt_app, config_ui):
    config_ui.connection_test_panel.result = _result(mbits=4)
    config_ui._update_target_cost()
    download = config_ui.target_cost_panel.result("download")
    assert "your connection" in download.message


def test_expert_table_names_automatic_sources(qt_app, tmp_path):
    from ui.pages.expert_settings_page import COL_SOURCE, ExpertSettingsModel

    connection_test.save_result(_result(suggested=256))
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[limits]\nram_gb = 3\n",
        encoding="utf-8",
    )
    model = ExpertSettingsModel(AOConfig(str(path)))

    def source(name):
        row = next(
            r for r in range(model.rowCount()) if model.spec(r).name == name
        )
        return model.data(model.index(row, COL_SOURCE)), model.tooltip(model.spec(row))

    text, tip = source("cache.cache_mem_limit")
    assert text == "Your limits"
    assert "Lowered to fit your limits" in tip
    text, tip = source("autoortho.provider_origin_initial_concurrency")
    assert text == "Measured"
    assert "From the connection test" in tip

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtWidgets import QMessageBox

from aoconfig import AOConfig
from config_ui_qt import ConfigUI
from ui.runtime_state import RuntimeState
from ui.services.common import ServiceResult


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        ConfigUI,
        "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
    monkeypatch.setattr(ConfigUI, "start_update_check", lambda self: None)

    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.save()
    cfg.get_config()

    ui = ConfigUI(cfg)
    readiness_worker = ui.readiness_worker
    if readiness_worker is not None and readiness_worker.isRunning():
        readiness_worker.wait(2000)
        qt_app.processEvents()
    storage_worker = ui.storage_scan_worker
    if storage_worker is not None and storage_worker.isRunning():
        storage_worker.wait(2000)
        qt_app.processEvents()
    yield ui
    worker = ui.mount_control_worker
    if worker is not None:
        worker.wait(2000)
        qt_app.processEvents()
    ui.mount_monitor_timer.stop()
    ui.cfg.cache.auto_clean_cache = False
    ui._set_runtime_state(RuntimeState.STOPPED)
    ui.settings_session.mark_applied(ui._snapshot_settings())
    ui.close()
    ui.deleteLater()
    qt_app.processEvents()
    qt_app.exit(0)


def _wait_for_state(app, ui, state, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if ui.runtime_state == state:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"Expected {state}, got {ui.runtime_state}"
    )


def test_runtime_state_controls(config_ui):
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    assert config_ui.run_button.text() == "Stop Streaming"
    assert config_ui.run_button.isEnabled()
    assert not config_ui.paths_group.isEnabled()
    assert not config_ui.tabs.isTabEnabled(
        config_ui.tabs.indexOf(config_ui.scenery_widget)
    )
    assert config_ui.maptype_combo.isEnabled()
    assert not config_ui.apply_button.isEnabled()

    config_ui._set_runtime_state(RuntimeState.STOPPED)
    assert config_ui.run_button.text() == "Start Streaming"
    assert config_ui.paths_group.isEnabled()
    assert not config_ui.apply_button.isEnabled()


def test_partial_loading_controls_are_safe_and_persist(config_ui):
    assert not config_ui.native_partial_allow_incomplete_check.isChecked()
    assert not config_ui.persist_partial_dds_cache_check.isChecked()

    config_ui.native_partial_allow_incomplete_check.setChecked(True)
    config_ui.persist_partial_dds_cache_check.setChecked(True)
    config_ui.save_config(persist=False, refresh_scenery=False)

    assert config_ui.cfg.autoortho.native_partial_allow_incomplete is True
    assert config_ui.cfg.autoortho.persist_partial_dds_cache is True

    config_ui._apply_settings_preset("Balanced")
    assert not config_ui.native_partial_allow_incomplete_check.isChecked()
    assert config_ui.persist_partial_dds_cache_check.isChecked()


def test_settings_change_enables_apply_and_revert(
    qt_app,
    config_ui,
    monkeypatch,
):
    original = config_ui.cache_dir_edit.text()
    config_ui.cache_dir_edit.setText(original + "-changed")
    qt_app.processEvents()

    assert config_ui.settings_session.dirty is True
    assert config_ui.settings_session.restart_required is True
    assert config_ui.apply_button.isEnabled()
    assert config_ui.revert_button.isEnabled()

    monkeypatch.setattr(config_ui, "save_config", lambda **kwargs: True)
    config_ui.on_revert()

    assert config_ui.cache_dir_edit.text() == original
    assert config_ui.settings_session.dirty is False


def test_revert_does_not_create_discarded_paths(
    qt_app,
    config_ui,
):
    discarded = Path(config_ui.cfg.conf_file).parent / "discarded-scenery"
    config_ui.scenery_path_edit.setText(str(discarded))
    qt_app.processEvents()

    config_ui.on_revert()

    assert not discarded.exists()


def test_apply_marks_settings_session_clean(
    qt_app,
    config_ui,
    monkeypatch,
):
    config_ui.showconfig_check.setChecked(
        not config_ui.showconfig_check.isChecked()
    )
    qt_app.processEvents()
    monkeypatch.setattr(
        "config_ui_qt.validate_configuration",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        config_ui,
        "_prepare_runtime_directories",
        lambda: [],
    )
    monkeypatch.setattr(config_ui, "save_config", lambda **kwargs: True)
    monkeypatch.setattr(config_ui, "refresh_scenery_list", lambda: None)

    assert config_ui.on_save() is True
    assert config_ui.settings_session.dirty is False
    assert not config_ui.apply_button.isEnabled()


def test_custom_tiles_rebuild_preserves_other_pending_values(
    qt_app,
    config_ui,
):
    new_cache_value = min(
        config_ui.mem_cache_slider.maximum(),
        config_ui.mem_cache_slider.value() + 1,
    )
    config_ui.mem_cache_slider.setValue(new_cache_value)
    config_ui.using_custom_tiles_check.setChecked(
        not config_ui.using_custom_tiles_check.isChecked()
    )
    qt_app.processEvents()

    assert config_ui.mem_cache_slider.value() == new_cache_value
    assert config_ui.settings_session.dirty is True


def test_apply_revert_hidden_on_non_settings_tabs(config_ui):
    config_ui.tabs.setCurrentWidget(config_ui.logs_widget)
    assert config_ui.shell.current_page() is config_ui.diagnostics_page

    config_ui.tabs.setCurrentWidget(config_ui.setup_widget)
    assert config_ui.shell.current_page() is config_ui.categorized_settings_page
    assert config_ui.apply_button.parent() is config_ui.categorized_settings_page
    assert config_ui.revert_button.parent() is config_ui.categorized_settings_page


def test_shell_exposes_five_task_oriented_destinations(config_ui):
    assert config_ui.shell.navigation.destination_keys() == [
        "home",
        "scenery-library",
        "flight-plan-map",
        "settings",
        "diagnostics",
    ]
    config_ui.navigate_to("diagnostics")
    assert config_ui.shell.current_page() is config_ui.diagnostics_page
    config_ui.navigate_to("settings", "Dynamic Zoom")
    assert (
        config_ui.shell.current_page()
        is config_ui.categorized_settings_page
    )
    assert (
        config_ui.categorized_settings_page.category_list.currentItem().text()
        == "Dynamic Zoom"
    )


def test_validation_navigates_visible_shell_even_when_legacy_tab_is_stale(
    config_ui,
):
    from ui.config_validation import ValidationIssue, ValidationSeverity

    config_ui.tabs.setCurrentWidget(config_ui.setup_widget)
    config_ui.navigate_to("home")
    config_ui._show_validation_issues(
        [
            ValidationIssue(
                "xplane_path",
                ValidationSeverity.ERROR,
                "Invalid X-Plane path",
            )
        ]
    )

    assert (
        config_ui.shell.current_page()
        is config_ui.categorized_settings_page
    )
    assert (
        config_ui.categorized_settings_page.category_list.currentItem().text()
        == "Paths & Storage"
    )


def test_header_start_action_stays_disabled_during_background_jobs(
    config_ui,
):
    config_ui.download_workers["na"] = object()
    config_ui._set_runtime_state(RuntimeState.STOPPED)
    assert not config_ui.run_button.isEnabled()

    config_ui._update_shell_status()
    assert not config_ui.run_button.isEnabled()
    config_ui.download_workers.clear()


def test_update_result_uses_nonmodal_header_banner(
    config_ui,
    monkeypatch,
):
    monkeypatch.setattr("config_ui_qt.__version__", "1.0.0")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: pytest.fail("update checks must not open a modal"),
    )

    config_ui.on_update_check_result(
        ("v2.0.0", "https://example.test/release")
    )

    assert not config_ui.shell.header.update_banner.isHidden()
    assert config_ui._latest_update_url == "https://example.test/release"
    config_ui._remind_update_later()
    assert config_ui.shell.header.update_banner.isHidden()


def test_shell_page_and_geometry_are_persisted(config_ui, monkeypatch):
    saved = []
    monkeypatch.setattr(config_ui.cfg, "save", lambda: saved.append(True))
    config_ui.navigate_to("diagnostics")

    config_ui._persist_shell_state()

    assert config_ui.cfg.general.last_page == "Diagnostics"
    assert config_ui.cfg.general.window_width == config_ui.width()
    assert saved == [True]


def test_existing_valid_setup_is_inferred(
    qt_app,
    config_ui,
    monkeypatch,
):
    saved = []
    config_ui.cfg.general.setup_complete = False
    monkeypatch.setattr(
        config_ui.readiness_service,
        "infer_complete",
        lambda values: SimpleNamespace(
            success=True,
            value=True,
            error=None,
        ),
    )
    monkeypatch.setattr(
        config_ui.cfg,
        "save",
        lambda: saved.append(True),
    )
    monkeypatch.setattr(
        "config_ui_qt.SetupWizard",
        lambda *args, **kwargs: pytest.fail(
            "established users should not see the wizard"
        ),
    )

    config_ui._maybe_show_setup_wizard()
    config_ui.setup_inference_worker.wait()
    qt_app.processEvents()

    assert config_ui.cfg.general.setup_complete is True
    assert saved == [True]


def test_setup_wizard_applies_selected_paths(config_ui, monkeypatch):
    paths = {
        "xplane_path": "/new/xplane",
        "scenery_path": "/new/scenery",
        "cache_dir": "/new/cache",
        "long_term_cache_dir": "/new/archive",
        "download_dir": "/new/downloads",
    }

    class FakeWizard:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def get_selected_paths(self):
            return paths

        def get_selected_region_ids(self):
            return []

    monkeypatch.setattr("config_ui_qt.SetupWizard", FakeWizard)
    def apply_settings():
        config_ui.settings_session.mark_applied(
            config_ui._snapshot_settings()
        )
        return True

    monkeypatch.setattr(config_ui, "on_save", apply_settings)
    monkeypatch.setattr(config_ui, "_run_readiness_checks", lambda: None)

    config_ui._maybe_show_setup_wizard(force=True)

    assert config_ui.xplane_path_edit.text() == "/new/xplane"
    assert config_ui.lt_cache_dir_edit.text() == "/new/archive"
    assert config_ui.cfg.general.setup_complete is True

def test_verify_invalid_configuration_is_recoverable(config_ui):
    config_ui.xplane_path_edit.setText("/path/that/does/not/exist")

    assert config_ui.verify() is False
    assert config_ui.runtime_state == RuntimeState.STOPPED
    assert config_ui.xplane_path_edit.property("validationError") is True


def test_start_and_stop_are_asynchronous(
    qt_app,
    config_ui,
    monkeypatch,
):
    process = SimpleNamespace(poll=lambda: None)
    handle = SimpleNamespace(process=process, mountpoint="/tmp/test-mount")
    config_ui.mount_workers = [handle]

    monkeypatch.setattr(config_ui, "verify", lambda: True)
    monkeypatch.setattr(
        config_ui,
        "_readiness_for_start",
        lambda: SimpleNamespace(can_finish=True, checks=[]),
    )
    monkeypatch.setattr(
        config_ui,
        "_prepare_runtime_directories",
        lambda: [],
    )
    monkeypatch.setattr(config_ui, "save_config", lambda: None)
    monkeypatch.setattr(
        config_ui,
        "preflight_mount_check_and_prompt",
        lambda: [],
    )
    monkeypatch.setattr(
        config_ui,
        "mount_sceneries",
        lambda blocking=False: True,
        raising=False,
    )
    monkeypatch.setattr(
        config_ui,
        "unmount_sceneries",
        lambda force=False: True,
        raising=False,
    )

    config_ui.on_run()
    assert config_ui.runtime_state == RuntimeState.STARTING
    _wait_for_state(qt_app, config_ui, RuntimeState.RUNNING)

    config_ui.on_run()
    assert config_ui.runtime_state == RuntimeState.STOPPING
    _wait_for_state(qt_app, config_ui, RuntimeState.STOPPED)


def test_start_waits_for_nonblocking_readiness(
    qt_app,
    config_ui,
    monkeypatch,
):
    existing = config_ui.readiness_worker
    if existing is not None and existing.isRunning():
        existing.wait(2000)
        qt_app.processEvents()
    config_ui.current_readiness = None
    config_ui._readiness_signature = None
    monkeypatch.setattr(
        config_ui.readiness_service,
        "check",
        lambda *args, **kwargs: ServiceResult(
            SimpleNamespace(can_finish=True, checks=[])
        ),
    )
    monkeypatch.setattr(config_ui, "verify", lambda: True)
    monkeypatch.setattr(
        config_ui,
        "_prepare_runtime_directories",
        lambda: [],
    )
    monkeypatch.setattr(config_ui, "save_config", lambda: True)
    monkeypatch.setattr(
        config_ui,
        "preflight_mount_check_and_prompt",
        lambda: [],
    )
    monkeypatch.setattr(
        config_ui,
        "mount_sceneries",
        lambda blocking=False: True,
        raising=False,
    )
    monkeypatch.setattr(
        config_ui,
        "unmount_sceneries",
        lambda force=False: True,
        raising=False,
    )

    config_ui.on_run()

    assert config_ui.runtime_state == RuntimeState.STOPPED
    worker = config_ui.readiness_worker
    assert worker is not None
    worker.wait(2000)
    _wait_for_state(qt_app, config_ui, RuntimeState.RUNNING)
    config_ui.on_run()
    _wait_for_state(qt_app, config_ui, RuntimeState.STOPPED)


def test_repeated_start_is_ignored_while_starting(config_ui, monkeypatch):
    calls = []
    monkeypatch.setattr(
        config_ui,
        "_start_mount_control",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    config_ui._set_runtime_state(RuntimeState.STARTING)

    config_ui.on_run()

    assert calls == []


def test_worker_exit_starts_error_cleanup(config_ui, monkeypatch):
    process = SimpleNamespace(poll=lambda: 7)
    config_ui.mount_workers = [
        SimpleNamespace(process=process, mountpoint="/tmp/failed-mount")
    ]
    calls = []
    monkeypatch.setattr(
        config_ui,
        "_start_mount_control",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    config_ui._set_runtime_state(RuntimeState.RUNNING)

    config_ui._check_mount_workers()

    assert "exited with code 7" in config_ui._runtime_error_message
    assert calls == [
        (("stop",), {"stop_target": RuntimeState.ERROR})
    ]


def test_failed_close_stop_clears_pending_quit(config_ui, monkeypatch):
    monkeypatch.setattr(config_ui, "display_error", lambda message: None)
    config_ui._close_after_stop = True
    config_ui._on_mount_control_completed(
        "stop",
        False,
        "mount still active",
    )

    assert config_ui._close_after_stop is False
    assert config_ui.runtime_state == RuntimeState.ERROR


def test_delete_cache_requires_confirmation(config_ui, monkeypatch):
    calls = []

    def reject(*args):
        calls.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", reject)
    monkeypatch.setattr(
        config_ui,
        "on_clean_cache",
        lambda **kwargs: pytest.fail("cache deletion should not start"),
    )

    config_ui.on_delete_cache()

    assert calls
    assert calls[0][-1] == QMessageBox.StandardButton.No


def test_jpeg_cleanup_cancels_without_partial_delete(config_ui, tmp_path):
    jpeg = tmp_path / "tile.jpg"
    jpeg.write_bytes(b"image")
    cancel_event = threading.Event()
    cancel_event.set()

    success, message, cancelled = config_ui.clean_jpegs_only(
        str(tmp_path),
        cancel_event=cancel_event,
        progress_callback=lambda text: None,
    )

    assert success is True
    assert cancelled is True
    assert jpeg.exists()


def test_exit_cache_cleanup_does_not_start_storage_scan(
    config_ui,
    monkeypatch,
):
    monkeypatch.setattr(
        config_ui,
        "_start_storage_scan",
        lambda: pytest.fail("shutdown must not start a new scan"),
    )

    config_ui._on_cache_cleanup_completed(
        True,
        "complete",
        False,
        True,
    )

    assert config_ui._cache_finalize_pending is True


def test_save_rejects_invalid_port_without_persisting(
    config_ui,
    monkeypatch,
):
    xplane_path = Path(config_ui.xplane_path_edit.text())
    (xplane_path / "Custom Scenery").mkdir(parents=True)
    config_ui.webui_port_edit.setText("invalid")
    monkeypatch.setattr(
        config_ui,
        "save_config",
        lambda: pytest.fail("invalid settings must not be persisted"),
    )

    config_ui.on_save()

    assert config_ui.webui_port_edit.property("validationError") is True
    assert "Save blocked" in config_ui.status_bar.currentMessage()


def test_uninstall_requires_confirmation(config_ui, monkeypatch):
    release = SimpleNamespace(subfolder_dir="/tmp/scenery/na")
    config_ui.dl.regions = {
        "na": SimpleNamespace(local_rel=release)
    }
    calls = []

    def reject(*args):
        calls.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", reject)

    config_ui.on_delete_scenery("na")

    assert calls
    assert calls[0][-1] == QMessageBox.StandardButton.No
    assert not config_ui.uninstall_workers


def test_close_while_running_defaults_to_cancel(config_ui, monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.No,
    )
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    event = QCloseEvent()

    config_ui.closeEvent(event)

    assert not event.isAccepted()
    assert config_ui.runtime_state == RuntimeState.RUNNING


def test_close_while_running_requests_async_stop(config_ui, monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Yes,
    )
    calls = []
    monkeypatch.setattr(
        config_ui,
        "_request_stop_streaming",
        lambda: calls.append("stop"),
    )
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    event = QCloseEvent()

    config_ui.closeEvent(event)

    assert not event.isAccepted()
    assert config_ui._close_after_stop is True
    assert calls == ["stop"]


def test_close_stays_open_when_unmount_fails(config_ui, monkeypatch):
    config_ui.cfg.cache.auto_clean_cache = False
    config_ui.settings_session.mark_applied(
        config_ui._snapshot_settings()
    )
    monkeypatch.setattr(
        config_ui,
        "unmount_sceneries",
        lambda: False,
        raising=False,
    )
    errors = []
    monkeypatch.setattr(config_ui, "display_error", errors.append)
    event = QCloseEvent()

    config_ui.closeEvent(event)

    assert not event.isAccepted()
    assert errors and "active mounts" in errors[0]


def test_production_aomountui_mro_runs_async_lifecycle(
    qt_app,
    monkeypatch,
    tmp_path,
):
    import autoortho as autoortho_module
    if not hasattr(autoortho_module, "AOMount"):
        from autoortho import autoortho as autoortho_module

    monkeypatch.setattr(
        ConfigUI,
        "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
    monkeypatch.setattr(ConfigUI, "start_update_check", lambda self: None)
    monkeypatch.setattr(
        autoortho_module.AOMount,
        "start_stats_manager",
        lambda self: None,
    )
    monkeypatch.setattr(
        autoortho_module.AOMount,
        "start_reporter",
        lambda self: None,
    )
    monkeypatch.setattr(
        autoortho_module.AOMount,
        "start_log_server",
        lambda self: None,
    )

    lifecycle_calls = []
    monkeypatch.setattr(
        autoortho_module.AOMount,
        "mount_sceneries",
        lambda self, blocking=True: lifecycle_calls.append(
            ("start", blocking)
        ) or True,
    )
    monkeypatch.setattr(
        autoortho_module.AOMount,
        "unmount_sceneries",
        lambda self, force=False: lifecycle_calls.append(
            ("stop", force)
        ) or True,
    )

    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()

    ui = autoortho_module.AOMountUI(cfg)
    process = SimpleNamespace(poll=lambda: None)
    ui.mount_workers = [
        SimpleNamespace(process=process, mountpoint="/tmp/test-mount")
    ]
    monkeypatch.setattr(ui, "verify", lambda: True)
    monkeypatch.setattr(
        ui,
        "_readiness_for_start",
        lambda: SimpleNamespace(can_finish=True, checks=[]),
    )
    monkeypatch.setattr(ui, "_prepare_runtime_directories", lambda: [])
    monkeypatch.setattr(ui, "save_config", lambda: None)
    monkeypatch.setattr(
        ui,
        "preflight_mount_check_and_prompt",
        lambda: [],
    )

    ui.on_run()
    _wait_for_state(qt_app, ui, RuntimeState.RUNNING)
    ui.on_run()
    _wait_for_state(qt_app, ui, RuntimeState.STOPPED)

    worker = ui.mount_control_worker
    if worker is not None:
        worker.wait(2000)
        qt_app.processEvents()
    assert lifecycle_calls[:2] == [
        ("start", False),
        ("stop", False),
    ]

    ui.mount_monitor_timer.stop()
    close_event = QCloseEvent()
    ui.closeEvent(close_event)
    assert close_event.isAccepted()
    storage_worker = ui.storage_scan_worker
    if storage_worker is not None and storage_worker.isRunning():
        storage_worker.requestInterruption()
        storage_worker.wait(2000)
    assert not any(
        worker.isRunning()
        for worker in ui.findChildren(QThread)
    )
    ui.deleteLater()
    qt_app.processEvents()
    qt_app.exit(0)

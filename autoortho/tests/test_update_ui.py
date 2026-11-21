"""Update banner, download flow and the hand-over to the update helper."""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from aoconfig import AOConfig  # noqa: E402
from config_ui_qt import ConfigUI  # noqa: E402
from ui.runtime_state import RuntimeState  # noqa: E402
from updater.install_info import InstallInfo  # noqa: E402
from updater.releases import Release, SelectedAsset, UpdateOffer  # noqa: E402
from updater.service import PreparedUpdate, StartupReport  # noqa: E402
from updater.versions import parse_release_version  # noqa: E402


class FakeManager:
    def __init__(self, prepared=None, error=None):
        self.install = InstallInfo(
            platform="windows",
            kind="zip",
            version_text="3.4.0",
            version=parse_release_version("3.4.0"),
            root=Path("C:/AutoOrtho"),
            executable=Path("C:/AutoOrtho/autoortho.exe"),
        )
        self.prepared = prepared
        self.error = error
        self.handed_over = []
        self.startup_report = StartupReport(result=None, started=None)

    def prepare(self, offer, progress=None, cancelled=None, stage_progress=None):
        if self.error:
            raise RuntimeError(self.error)
        progress(50, 100)
        progress(100, 100)
        return self.prepared

    def hand_over(self, prepared):
        self.handed_over.append(prepared)

    def rollback_target(self):
        return None

    def startup(self, version_text):
        return self.startup_report


def _offer(tag="3.5.0", installable=True, notes="## Fixed\n- things"):
    release = Release(
        tag=tag,
        version=parse_release_version(tag),
        title=tag,
        notes=notes,
        html_url="https://example.test/release",
        prerelease=False,
    )
    asset = (
        SelectedAsset("autoortho_win_3.5.0.zip", "https://example.test/z", 100, "a" * 64, "zip")
        if installable
        else None
    )
    return UpdateOffer(release=release, asset=asset, reason="" if installable else "source build")


PREPARED = PreparedUpdate("3.5.0", "swap", Path("C:/staged/autoortho.exe"), source=Path("C:/staged"))


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        ConfigUI,
        "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
    monkeypatch.setattr(ConfigUI, "start_update_check", lambda self, manual=False: None)
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()

    ui = ConfigUI(cfg)
    for worker in (ui.readiness_worker, ui.storage_scan_worker):
        if worker is not None and worker.isRunning():
            worker.wait(2000)
    qt_app.processEvents()
    ui._update_manager = FakeManager(prepared=PREPARED)
    yield ui
    ui.mount_monitor_timer.stop()
    ui._update_handover = None
    ui._update_close_confirmed = False
    ui._set_runtime_state(RuntimeState.STOPPED)
    ui.settings_session.mark_applied(ui._snapshot_settings())
    ui.close()
    ui.deleteLater()
    qt_app.processEvents()


def _banner(ui):
    header = ui.shell.header
    return header


def _wait_until(app, condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_installable_offer_offers_update_now(config_ui, monkeypatch):
    started = []
    monkeypatch.setattr(config_ui, "_start_update_download", lambda: started.append(True))
    config_ui._update_check_manual = False
    config_ui.on_update_check_result(_offer())

    header = _banner(config_ui)
    assert not header.update_banner.isHidden()
    assert header.update_banner_button.text() == "Update Now"
    assert not header.update_notes_button.isHidden()
    header.update_banner_button.click()
    assert started == [True]


def test_unsupported_offer_opens_release_page(config_ui, monkeypatch):
    opened = []
    monkeypatch.setattr("config_ui_qt.webbrowser.open", opened.append)
    config_ui.on_update_check_result(_offer(installable=False, notes=""))
    header = _banner(config_ui)
    assert header.update_banner_button.text() == "View release"
    assert header.update_notes_button.isHidden()
    assert header.update_banner_label.toolTip() == "source build"
    header.update_banner_button.click()
    assert opened == ["https://example.test/release"]


def test_dismissed_version_stays_hidden_on_automatic_checks(config_ui):
    config_ui.cfg.general.dismissed_update_version = "3.5.0"
    config_ui._update_check_manual = False
    config_ui.on_update_check_result(_offer())
    assert _banner(config_ui).update_banner.isHidden()
    config_ui._update_check_manual = True
    config_ui.on_update_check_result(_offer())
    assert not _banner(config_ui).update_banner.isHidden()


def test_auto_download_starts_when_enabled(config_ui, monkeypatch):
    started = []
    monkeypatch.setattr(config_ui, "_start_update_download", lambda: started.append(True))
    config_ui.cfg.general.update_auto_download = True
    config_ui.on_update_check_result(_offer())
    assert started == [True]


def test_download_reaches_ready_state(qt_app, config_ui):
    config_ui.on_update_check_result(_offer())
    config_ui._start_update_download()
    _wait_until(qt_app, lambda: config_ui._update_ui_state == "ready")
    assert config_ui._prepared_update is PREPARED
    assert _banner(config_ui).update_banner_button.text() == "Restart Now"
    task = config_ui.task_manager.task("update-download")
    assert task.stage == "Ready to install"


def test_failed_download_returns_to_offer(qt_app, config_ui):
    config_ui._update_manager = FakeManager(error="checksum mismatch")
    config_ui.on_update_check_result(_offer())
    config_ui._start_update_download()
    _wait_until(qt_app, lambda: config_ui._update_ui_state == "offer")
    assert _banner(config_ui).update_banner_button.text() == "Update Now"
    assert "checksum mismatch" in config_ui.task_manager.task("update-download").error


def test_restart_hands_over_on_close(config_ui):
    manager = config_ui._update_manager
    config_ui._restart_to_update(PREPARED)
    assert manager.handed_over == [PREPARED]
    assert config_ui._update_handover is None


def test_restart_waits_for_background_jobs(config_ui, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a))
    config_ui.download_workers["na"] = object()
    try:
        config_ui._restart_to_update(PREPARED)
    finally:
        config_ui.download_workers.clear()
    assert warnings
    assert config_ui._update_manager.handed_over == []
    assert config_ui._update_handover is None


def test_aborted_close_forgets_the_update(config_ui, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    config_ui._update_handover = PREPARED
    config_ui._update_close_confirmed = True
    config_ui.download_workers["na"] = object()
    try:
        event = QCloseEvent()
        config_ui.closeEvent(event)
    finally:
        config_ui.download_workers.clear()
    assert not event.isAccepted()
    assert config_ui._update_handover is None
    assert config_ui._update_manager.handed_over == []


def test_restart_while_streaming_stops_first_and_asks_once(config_ui, monkeypatch):
    asked = []

    def question(*args):
        asked.append(args[1])
        return QMessageBox.StandardButton.Yes

    stops = []
    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(config_ui, "_request_stop_streaming", lambda *a, **k: stops.append(True))
    config_ui._set_runtime_state(RuntimeState.RUNNING)
    config_ui._restart_to_update(PREPARED)
    assert asked == ["Stop Streaming and Restart?"]
    assert stops == [True]
    assert config_ui._close_after_stop
    assert config_ui._update_handover is PREPARED
    config_ui._close_after_stop = False


def test_channel_setting_is_saved(config_ui):
    config_ui.update_channel_combo.setCurrentIndex(1)
    assert config_ui._update_channel_value() == "rc"
    config_ui.update_channel_combo.setCurrentIndex(0)
    assert config_ui._update_channel_value() == "stable"


def test_startup_reports_update_results(config_ui, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    config_ui._update_manager.startup_report = StartupReport(
        result={"status": "rolled_back", "message": "AutoOrtho 3.5.0 closed right after starting."},
        started={"version": "3.4.0", "rollback": False},
    )
    config_ui._run_update_startup()
    task = config_ui.task_manager.task("update-installed")
    assert task is not None and "Updated to AutoOrtho" in task.title
    box = config_ui._update_result_box
    assert "closed right after starting" in box.text()
    box.close()

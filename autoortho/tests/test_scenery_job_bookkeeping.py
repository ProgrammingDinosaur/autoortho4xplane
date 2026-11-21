"""Finished scenery jobs must stop counting as active work.

A job left in its worker map makes ``_has_active_ui_jobs`` true for the rest
of the session, which blocks both Start Streaming and quitting.
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for directory in (os.path.dirname(TESTS_DIR), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

from PySide6.QtCore import QThread, Signal  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import config_ui_qt  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from config_ui_qt import ConfigUI  # noqa: E402


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class FakeRestoreWorker(QThread):
    """Stands in for RestoreDefaultDsfsWorker with the same signals."""

    finished = Signal(str, bool)
    error = Signal(str, str)
    progress = Signal(str, dict)
    outcome = "success"

    def __init__(self, dl_manager, region_id):
        super().__init__()
        self.region_id = region_id

    def run(self):
        self.progress.emit(self.region_id, {"pcnt_done": 100})
        if self.outcome == "error":
            self.error.emit(self.region_id, "boom")
        else:
            self.finished.emit(self.region_id, True)


def make_ui(monkeypatch, tmp_path):
    monkeypatch.setattr(ConfigUI, "refresh_scenery_list",
                        lambda self: self.scenery_layout.addStretch())
    monkeypatch.setattr(ConfigUI, "start_update_check",
                        lambda self, manual=False: None)
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "Custom Scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()
    return ConfigUI(cfg)


def run_restore(ui, monkeypatch, outcome):
    monkeypatch.setattr(FakeRestoreWorker, "outcome", outcome)
    monkeypatch.setattr(config_ui_qt, "RestoreDefaultDsfsWorker",
                        FakeRestoreWorker)
    ui.on_restore_default_dsfs("na")
    worker = ui.restore_default_dsfs_workers.get("na")
    assert worker is not None
    assert wait_until(lambda: worker.isFinished())
    task = ui.task_manager.task("scenery-restore:na")
    assert wait_until(lambda: task.state.terminal)
    QApplication.processEvents()


def assert_quit_not_blocked(ui, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *args, **kwargs: warnings.append(args))
    # Stop before the real shutdown runs; only the busy guard matters here.
    monkeypatch.setattr(ConfigUI, "_resolve_pending_settings",
                        lambda self, for_start=False: False)
    event = QCloseEvent()
    ui.closeEvent(event)
    assert warnings == [], "quit was refused as if a job were still running"


def test_finished_restore_does_not_block_quit(qt_app, monkeypatch, tmp_path):
    ui = make_ui(monkeypatch, tmp_path)
    try:
        run_restore(ui, monkeypatch, "success")
        assert_quit_not_blocked(ui, monkeypatch)
        assert "na" not in ui.restore_default_dsfs_workers
        assert not ui._has_active_ui_jobs()
    finally:
        ui.restore_default_dsfs_workers.clear()
        ui._ready_to_close = True
        ui.close()
        ui.deleteLater()


def test_failed_restore_does_not_block_quit(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(ConfigUI, "display_error", lambda self, msg: None)
    ui = make_ui(monkeypatch, tmp_path)
    try:
        run_restore(ui, monkeypatch, "error")
        assert "na" not in ui.restore_default_dsfs_workers
        assert not ui._has_active_ui_jobs()
    finally:
        ui.restore_default_dsfs_workers.clear()
        ui._ready_to_close = True
        ui.close()
        ui.deleteLater()


class BlockingWorker(QThread):
    """A job thread that runs until released."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def run(self):
        self.release.wait(10)


WORKER_MAPS = (
    "download_workers",
    "uninstall_workers",
    "add_seasons_workers",
    "restore_default_dsfs_workers",
    "add_roughness_workers",
    "restore_roughness_workers",
)


def test_stale_entries_for_finished_threads_do_not_block_quit(
        qt_app, monkeypatch, tmp_path):
    ui = make_ui(monkeypatch, tmp_path)
    finished = BlockingWorker()
    finished.release.set()
    finished.start()
    assert finished.wait(5000)
    try:
        for name in WORKER_MAPS:
            setattr(ui, name, {"na": finished})
        assert not ui._has_active_ui_jobs()
        assert_quit_not_blocked(ui, monkeypatch)
    finally:
        for name in WORKER_MAPS:
            setattr(ui, name, {})
        ui._ready_to_close = True
        ui.close()
        ui.deleteLater()


def test_running_job_still_blocks_quit(qt_app, monkeypatch, tmp_path):
    ui = make_ui(monkeypatch, tmp_path)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *args, **kwargs: warnings.append(args))
    try:
        for name in WORKER_MAPS:
            running = BlockingWorker()
            running.start()
            setattr(ui, name, {"na": running})
            try:
                assert ui._has_active_ui_jobs(), name
                warnings.clear()
                event = QCloseEvent()
                ui.closeEvent(event)
                assert not event.isAccepted(), name
                assert warnings and warnings[0][1] == (
                    "Background Operation In Progress"), name
            finally:
                running.release.set()
                running.wait(5000)
                setattr(ui, name, {})
        assert not ui._has_active_ui_jobs()
    finally:
        ui._ready_to_close = True
        ui.close()
        ui.deleteLater()


def test_reapply_hands_off_from_restore_to_seasons(qt_app, monkeypatch,
                                                   tmp_path):
    ui = make_ui(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(ConfigUI, "_start_add_seasons_job",
                        lambda self, rid: started.append(rid))
    try:
        ui.reapply_after_restore.add("na")
        run_restore(ui, monkeypatch, "success")
        assert started == ["na"]
        assert "na" not in ui.restore_default_dsfs_workers
    finally:
        ui.restore_default_dsfs_workers.clear()
        ui._ready_to_close = True
        ui.close()
        ui.deleteLater()

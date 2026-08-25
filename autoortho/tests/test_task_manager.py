import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication

from ui.task_manager import TaskManager, TaskPanel
from ui.task_models import TaskState, TaskType


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_completed_tasks_remain_until_dismissed():
    manager = TaskManager()
    manager.create_task(
        "install:na",
        TaskType.SCENERY_INSTALL,
        "Install scenery",
    )

    manager.complete_task("install:na", stage="Installed")

    assert manager.task("install:na").state == TaskState.COMPLETED
    assert manager.dismiss_task("install:na") is True
    assert manager.task("install:na") is None


def test_cancellation_uses_cooperative_callback():
    calls = []
    manager = TaskManager()
    manager.create_task(
        "cache",
        TaskType.CACHE,
        "Clean cache",
        cancellable=True,
        cancel_callback=lambda: calls.append("cancel"),
    )

    assert manager.cancel_task("cache") is True
    assert calls == ["cancel"]
    assert manager.task("cache").state == TaskState.CANCELLING

    manager.mark_cancelled("cache")
    assert manager.task("cache").state == TaskState.CANCELLED


def test_failed_task_retry_restarts_callback():
    calls = []
    manager = TaskManager()
    manager.create_task(
        "simbrief",
        TaskType.SIMBRIEF,
        "Fetch SimBrief",
        retry_callback=lambda: calls.append("retry"),
    )
    manager.fail_task("simbrief", "network unavailable")

    assert manager.retry_task("simbrief") is True
    assert calls == ["retry"]


def test_task_panel_preserves_terminal_rows(qt_app):
    manager = TaskManager()
    panel = TaskPanel(manager)
    manager.create_task("mount", TaskType.MOUNT, "Start streaming")
    manager.complete_task("mount")
    qt_app.processEvents()

    assert panel.isHidden() is False
    assert "mount" in panel.rows

    manager.dismiss_task("mount")
    qt_app.processEvents()
    assert panel.isHidden() is True

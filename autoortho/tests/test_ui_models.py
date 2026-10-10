import os
import sys
import logging
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QSpinBox, QWidget

from ui.models.dynamic_zoom_model import DynamicZoomTableModel, SpinBoxDelegate
from ui.models.log_model import LogEntry, LogFilterProxyModel, LogListModel
from ui.models.scenery_model import (
    SceneryFilterProxyModel,
    SceneryListModel,
)
from ui.models.task_model import TaskListModel
from ui.task_manager import TaskManager
from ui.task_models import TaskState, TaskType
from utils.dynamic_zoom import DynamicZoomManager


def scenery_item(**overrides):
    data = {
        "region_id": "nz",
        "name": "New Zealand",
        "latest_version": "2.0",
        "installed_version": "1.0",
        "size_bytes": 42,
        "download_count": 7,
        "install_path": "/scenery/nz",
        "seasons_status": "Applied",
        "roughness_status": "Not applied",
        "roughness_value": 1.5,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_scenery_model_roles_update_and_proxy():
    model = SceneryListModel([scenery_item(), scenery_item(region_id="au", name="Australia", installed_version="")])
    assert model.rowCount() == 2
    index = model.index(0, 0)
    assert "New Zealand" in model.data(index, Qt.ItemDataRole.DisplayRole)
    assert model.data(index, SceneryListModel.StatusRole) == "Update available"
    assert model.data(index, SceneryListModel.BusyRole) is False

    assert model.set_busy("nz", True) is True
    assert model.data(index, SceneryListModel.BusyRole) is True

    proxy = SceneryFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.set_search_text("australia")
    assert proxy.rowCount() == 1
    proxy.set_status_filter("available")
    assert proxy.rowCount() == 1
    proxy.set_region_filter("nz")
    assert proxy.rowCount() == 0
    proxy.set_region_filter("")
    proxy.set_sort_mode("region")
    proxy.sort(0, Qt.SortOrder.AscendingOrder)
    assert proxy.data(proxy.index(0, 0), SceneryListModel.RegionIdRole) == "au"


def test_task_model_tracks_manager_signals():
    manager = TaskManager()
    model = TaskListModel(manager)
    task = model.insert_task("install:nz", TaskType.SCENERY_INSTALL, "Install NZ")
    assert task.id == "install:nz"
    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert "Install NZ" in model.data(index, Qt.ItemDataRole.DisplayRole)

    model.update_task("install:nz", stage="Downloading", bytes_total=200, bytes_completed=50)
    assert model.data(index, TaskListModel.StageRole) == "Downloading"
    assert model.data(index, TaskListModel.BytesCompletedRole) == 50

    manager.complete_task("install:nz", stage="Installed")
    assert model.data(index, TaskListModel.StateRole) == TaskState.COMPLETED.value
    assert model.data(index, TaskListModel.TerminalRole) is True

    assert model.remove_task("install:nz") is True
    assert model.rowCount() == 0


def test_dynamic_zoom_table_is_transactional(qt_app):
    manager = DynamicZoomManager()
    manager.set_base_zoom(16, 18)
    manager.add_step(10000, 15, 17)
    model = DynamicZoomTableModel(manager)

    assert model.rowCount() == 2
    assert model.columnCount() == 4
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "0–9,999 ft"
    assert model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole) == "10,000 ft"

    assert model.setData(model.index(1, 2), 14, Qt.ItemDataRole.EditRole) is True
    assert manager.get_steps()[0].zoom_level == 15
    assert model.working_manager().get_steps()[0].zoom_level == 14
    assert model.commit() is True
    assert manager.get_steps()[0].zoom_level == 14

    assert model.setData(model.index(0, 1), 500, Qt.ItemDataRole.EditRole) is False
    assert model.remove_row(0) is False

    delegate = SpinBoxDelegate(12, 19)
    parent_widget = QWidget()
    editor = delegate.createEditor(parent_widget, None, model.index(1, 2))
    assert isinstance(editor, QSpinBox)
    delegate.setEditorData(editor, model.index(1, 2))
    editor.setValue(13)
    delegate.setModelData(editor, model, model.index(1, 2))
    assert model.data(model.index(1, 2), Qt.ItemDataRole.EditRole) == 13


def test_log_model_batches_and_filters():
    model = LogListModel(max_entries=3)
    model.append_entries(
        [
            LogEntry(level=10, message="debug one"),
            LogEntry(level=20, message="info two"),
            LogEntry(level=30, message="warning three"),
            LogEntry(level=40, message="error four"),
        ]
    )
    assert model.rowCount() == 3
    assert "info two" in model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole)
    assert model.data(model.index(2, 0), LogListModel.LevelRole) == 40

    proxy = LogFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.set_minimum_level(logging.WARNING)
    proxy.set_search_text("error")
    assert proxy.rowCount() == 1
    assert proxy.data(proxy.index(0, 0), LogListModel.MessageRole) == "error four"

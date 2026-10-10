import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication

from ui.dynamic_zoom_editor import DynamicZoomEditor
from ui.models.dynamic_zoom_model import DynamicZoomTableModel
from utils.dynamic_zoom import DynamicZoomManager


def test_presets_and_undo_are_transactional(qt_app):
    manager = DynamicZoomManager()
    manager.set_base_zoom(16, 18)
    editor = DynamicZoomEditor(manager)

    editor.apply_preset("Airliner")
    assert editor.manager().step_count() == 4
    assert manager.step_count() == 1

    editor.undo()
    assert editor.manager().step_count() == 1
    editor.redo()
    assert editor.manager().step_count() == 4


def test_inline_add_remove_and_preview(qt_app):
    manager = DynamicZoomManager()
    manager.set_base_zoom(16, 18)
    editor = DynamicZoomEditor(manager)
    editor.show()
    qt_app.processEvents()

    editor.add_step()
    assert editor.manager().step_count() == 2
    assert "and above" in editor.model.data(
        editor.model.index(1, DynamicZoomTableModel.RangeColumn)
    )
    assert not editor.preview.grab().isNull()

    editor.table.selectRow(1)
    editor.remove_selected()
    assert editor.manager().step_count() == 1
    editor.close()

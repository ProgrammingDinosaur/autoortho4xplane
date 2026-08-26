import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QSlider

from ui.pages.settings_page import SettingsPage


def test_settings_categories_search_and_presets(qt_app):
    apply_button = QPushButton("Apply")
    revert_button = QPushButton("Revert")
    restart = QLabel("Restart")
    page = SettingsPage(apply_button, revert_button, restart)
    slider = QSlider()
    slider.setRange(0, 10)
    page.add_category(
        "General",
        [QLabel("Startup behavior")],
        numeric_bindings=[("Exact", slider, 1, "")],
    )
    page.add_category("Pipeline", [QLabel("Buffer pool")])

    assert page.category_list.count() == 2
    page.search_edit.setText("buffer")
    assert page.category_list.count() == 1
    assert page.category_list.item(0).text() == "Pipeline"

    seen = []
    page.preset_requested.connect(seen.append)
    page.preset_combo.setCurrentText("Balanced")
    assert seen == ["Balanced"]

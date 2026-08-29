import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QLabel,
    QPushButton,
    QSlider,
)

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


def test_exact_value_emits_underlying_slider_change(qt_app):
    page = SettingsPage(
        QPushButton("Apply"),
        QPushButton("Revert"),
        QLabel("Restart"),
    )
    slider = QSlider()
    slider.setObjectName("maxwait")
    slider.setRange(1, 100)
    slider.setValue(20)
    seen = []
    slider.valueChanged.connect(seen.append)

    page.add_category(
        "Performance",
        [],
        numeric_bindings=[
            ("Per-chunk wait", slider, 10, " s"),
        ],
    )
    exact = next(
        spin for spin in page.findChildren(QDoubleSpinBox)
        if spin.property("boundSliderObjectName") == "maxwait"
    )

    exact.setValue(2.5)

    assert slider.value() == 25
    assert seen == [25]

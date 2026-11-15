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


def test_settings_categories_search_and_simple_view(qt_app):
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
    page.add_category("Essentials", [QLabel("Quality target")], essential=True)

    # The simple view shows only the essential categories.
    assert page.visible_category_names() == ["Essentials"]
    seen = []
    page.show_all_changed.connect(seen.append)
    page.set_show_all(True)
    assert seen == [True]
    assert page.category_list.count() == 3

    # Search looks through every category, whatever the view.
    page.set_show_all(False)
    page.search_edit.setText("buffer")
    assert page.category_list.count() == 1
    assert page.category_list.item(0).text() == "Pipeline"
    page.search_edit.clear()

    # Asking for a hidden category shows all of them.
    assert page.select_category("General") is True
    assert page.show_all is True
    assert page.category_list.currentItem().text() == "General"


def test_search_no_results_shows_empty_state_and_navigation_clears_filter(
    qt_app,
):
    page = SettingsPage(
        QPushButton("Apply"),
        QPushButton("Revert"),
        QLabel("Restart"),
    )
    page.add_category("Imagery", [QLabel("Altitude-Based Quality")])
    page.add_category("Streaming", [QLabel("Prefetch radius")])

    page.search_edit.setText("does-not-exist")
    assert page.category_list.count() == 0
    assert page.stack.currentWidget() is page.empty_page

    assert page.select_category("Dynamic Zoom") is True
    assert page.search_edit.text() == ""
    assert page.category_list.currentItem().text() == "Imagery"


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

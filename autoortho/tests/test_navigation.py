import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QSizePolicy

from ui.navigation import NavigationRail


def test_navigation_rail_exposes_five_destinations(qt_app):
    rail = NavigationRail()

    assert rail.destination_keys() == [
        "home",
        "scenery-library",
        "flight-plan-map",
        "settings",
        "diagnostics",
    ]
    assert rail.current_destination() == "home"
    assert rail.button_for("settings").text() == "Settings"
    assert (
        rail.button_for("settings").font().weight()
        >= QFont.Weight.DemiBold
    )
    assert rail.button_for("settings").font().pointSize() >= 12
    assert rail.button_for("settings").minimumHeight() >= 42
    assert (
        rail.sizePolicy().horizontalPolicy()
        == QSizePolicy.Policy.Minimum
    )


def test_navigation_rail_emits_selection_changes(qt_app):
    rail = NavigationRail()
    seen = []
    rail.destinationChanged.connect(seen.append)

    rail.set_current_destination("diagnostics")
    qt_app.processEvents()

    assert rail.current_destination() == "diagnostics"
    assert seen == ["diagnostics"]


def test_navigation_button_wraps_long_label(qt_app):
    rail = NavigationRail()
    button = rail.button_for("scenery-library")
    one_line_height = button.sizeHint().height()
    label = "Scenery Library With A Much Longer Localized Name"

    button.setText(label)

    assert button.text() == label
    assert button.sizeHint().height() > one_line_height
    assert rail.minimumSizeHint().width() >= button.sizeHint().width()


def test_navigation_short_labels_stay_on_one_line(qt_app):
    from PySide6.QtWidgets import QToolButton

    rail = NavigationRail()
    for key in rail.destination_keys():
        assert "\n" not in QToolButton.text(rail.button_for(key))

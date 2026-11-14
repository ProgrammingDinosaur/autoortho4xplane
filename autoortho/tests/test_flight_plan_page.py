import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication, QGroupBox

from ui.pages.flight_plan_page import FlightPlanPage


def test_flight_plan_fields_and_influence(qt_app):
    page = FlightPlanPage(QGroupBox("SimBrief"))
    page.set_flight_data(
        {
            "origin": {"icao_code": "KJFK"},
            "destination": {"icao_code": "KLAX"},
            "general": {
                "flight_number": "AO123",
                "initial_altitude": "35000",
                "route": "DCT ROUTE",
            },
            "aircraft": {"icaocode": "A320"},
            "times": {"est_time_enroute": "18000"},
            "navlog": {"fix": [{}, {}]},
        }
    )
    page.set_influence(True)

    assert page.flight_fields["origin"].text() == "KJFK"
    assert page.flight_fields["cruise"].text() == "35,000 ft"
    assert page.route_text.toPlainText() == "DCT ROUTE"
    assert "actively controlling" in page.influence_label.text()


def test_flight_plan_page_embeds_map_editor(qt_app):
    from PySide6.QtWidgets import QWidget

    editor = QWidget()
    page = FlightPlanPage(QGroupBox("SimBrief"), editor)

    assert page.map_editor is editor
    assert editor.parent() is not None
    assert page.isAncestorOf(editor)

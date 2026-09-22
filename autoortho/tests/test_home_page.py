import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication

from ui.pages.home_page import HomePage


def test_home_page_cards_have_empty_states(qt_app):
    page = HomePage()

    assert page.cards["runtime"].value_label.text() == "■ Stopped"
    assert (
        page.cards["recent_failure"].value_label.text()
        == "✓ No recent failures"
    )

    page.update_summary(
        runtime="Running",
        readiness="Ready",
        xplane="Connected",
        mounted_scenery=["na", "eu"],
        provider="SimHeaven",
        simbrief="Loaded",
        cache="Warm",
        task="Downloading",
        throughput="12.5 MB/s",
        recent_failure="Timeout",
        recent_failure_detail="Last error: network timeout",
    )

    assert page.cards["runtime"].value_label.text() == "● Running"
    assert (
        page.cards["runtime"].value_label.property("textRole")
        == "sectionTitle"
    )
    assert page.cards["mounted_scenery"].value_label.text() == "na, eu"
    assert page.cards["throughput"].value_label.text() == "12.5 MB/s"
    assert page.cards["recent_failure"].detail_label.text() == "Last error: network timeout"


def test_home_page_shortcuts_emit_signals(qt_app):
    page = HomePage()
    seen = []
    page.fix_config_requested.connect(lambda: seen.append("fix"))
    page.install_scenery_requested.connect(lambda: seen.append("install"))
    page.open_diagnostics_requested.connect(lambda: seen.append("diag"))
    page.open_map_requested.connect(lambda: seen.append("map"))

    page.fix_config_button.click()
    page.install_scenery_button.click()
    page.open_diagnostics_button.click()
    page.open_map_button.click()

    assert seen == ["fix", "install", "diag", "map"]

import logging
import os
import sys
from collections import deque

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication, QTextEdit

from ui.pages.diagnostics_page import DiagnosticsPage


class FakeHandler:
    def __init__(self):
        self.entries = deque()
        self.filters = []
        self.paused = False

    def set_filter(self, level, text):
        self.filters.append((level, text))

    def set_paused(self, paused):
        self.paused = paused


def test_diagnostics_filters_pause_and_reports(qt_app, tmp_path):
    report_dir = tmp_path / "reports"
    report = report_dir / "performance-test" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n\nHealthy")
    handler = FakeHandler()
    text = QTextEdit()
    page = DiagnosticsPage(
        text,
        handler,
        log_path=str(tmp_path / "autoortho.log"),
        report_dir=str(report_dir),
    )
    page.report_list_worker.wait()
    qt_app.processEvents()

    page.level_combo.setCurrentText("ERROR")
    page.search_edit.setText("timeout")
    page.pause_check.setChecked(True)

    assert handler.filters[-1] == (logging.ERROR, "timeout")
    assert handler.paused is True
    assert page.report_combo.count() == 1
    if page.report_read_worker is not None:
        page.report_read_worker.wait()
        qt_app.processEvents()
    assert "Healthy" in page.report_preview.toPlainText()
    page.shutdown()

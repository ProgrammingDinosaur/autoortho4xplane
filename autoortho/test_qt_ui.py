#!/usr/bin/env python
"""Test script to verify PyQt6 UI loads correctly"""

import os
import sys

from PyQt6.QtWidgets import QApplication

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autoortho.aoconfig import AOConfig
from autoortho.config_ui_qt import ConfigUI


def test_ui():
    """Test the PyQt6 UI"""
    app = QApplication(sys.argv)

    # Load config
    cfg = AOConfig()

    # Create and show UI
    ui = ConfigUI(cfg)
    ui.show()

    # Test Settings tab by switching to it
    ui.tabs.setCurrentIndex(1)  # Settings is the second tab

    print("UI loaded successfully with Settings tab!")
    tabs = [ui.tabs.tabText(i) for i in range(ui.tabs.count())]
    print(f"Available tabs: {tabs}")
    print(f"CPU threads available: {os.cpu_count()}")

    # Test fetch threads spinbox
    if hasattr(ui, 'fetch_threads_spinbox'):
        spinbox = ui.fetch_threads_spinbox
        print(f"Fetch threads range: {spinbox.minimum()}-{spinbox.maximum()}")
        print(f"Current value: {spinbox.value()}")

    # Run the application
    sys.exit(app.exec())


if __name__ == "__main__":
    test_ui()

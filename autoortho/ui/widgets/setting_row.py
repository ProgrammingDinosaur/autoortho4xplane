"""Reusable labelled setting row."""

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


class SettingRow(QWidget):
    def __init__(self, label, control, helper="", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.label.setBuddy(control)
        self.control = control
        layout.addWidget(self.label)
        layout.addWidget(control, 1)
        if helper:
            self.helper = QLabel(helper)
            self.helper.setWordWrap(True)
            self.helper.setProperty("textRole", "secondary")
            layout.addWidget(self.helper)

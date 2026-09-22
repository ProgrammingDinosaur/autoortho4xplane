"""Reusable accessible directory picker."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget


class PathPicker(QWidget):
    pathChanged = Signal(str)

    def __init__(self, label="folder", path="", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(path)
        self.edit.setAccessibleName(label)
        self.button = QPushButton("Browse…")
        self.button.setAccessibleName(f"Browse for {label}")
        self.button.clicked.connect(self.browse)
        self.edit.textChanged.connect(self.pathChanged)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def path(self):
        return self.edit.text()

    def setPath(self, path):
        self.edit.setText(path)

    def browse(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select folder",
            self.path(),
        )
        if folder:
            self.setPath(folder)

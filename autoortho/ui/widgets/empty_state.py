"""Reusable empty-state message with corrective action."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class EmptyState(QWidget):
    actionRequested = Signal()

    def __init__(self, title, detail="", action_text="", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch()
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setProperty("textRole", "sectionTitle")
        self.detail = QLabel(detail)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setProperty("textRole", "secondary")
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        self.action = QPushButton(action_text)
        self.action.clicked.connect(self.actionRequested)
        self.action.setVisible(bool(action_text))
        layout.addWidget(self.action, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()

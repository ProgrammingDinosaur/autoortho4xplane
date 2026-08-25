"""Semantic icon/text status badge."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.theme import repolish
else:
    from ui.theme import repolish


class StatusBadge(QLabel):
    SYMBOLS = {
        "success": "✓",
        "warning": "!",
        "error": "×",
        "info": "○",
        "pending": "◌",
    }

    def __init__(self, text="", state="info", parent=None):
        super().__init__(parent)
        self.setObjectName("chipLabel")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(text, state)

    def set_status(self, text, state="info"):
        self.setProperty("state", state)
        self.setText(f"{self.SYMBOLS.get(state, '•')} {text}")
        self.setAccessibleDescription(f"{state}: {text}")
        repolish(self)

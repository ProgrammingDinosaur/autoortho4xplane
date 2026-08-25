"""Small behavior-only control subclasses styled by the semantic theme."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QPushButton, QSlider, QSpinBox


class StyledButton(QPushButton):
    def __init__(self, text, primary=False, parent=None):
        super().__init__(text, parent)
        self.primary = primary
        self.setProperty("role", "primary" if primary else "secondary")

    def _get_style(self):
        return ""


class ModernSlider(QSlider):
    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


class ModernSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

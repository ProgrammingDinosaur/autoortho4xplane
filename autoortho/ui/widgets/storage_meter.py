"""Compact storage usage meter."""

from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.readiness import format_bytes
else:
    from ui.readiness import format_bytes


class StorageMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel("Storage has not been checked.")
        self.bar = QProgressBar()
        layout.addWidget(self.label)
        layout.addWidget(self.bar)

    def set_usage(self, used, free):
        total = max(0, int(used) + int(free))
        percentage = round(100 * used / total) if total else 0
        self.bar.setValue(percentage)
        self.label.setText(
            f"{format_bytes(used)} used • {format_bytes(free)} free"
        )

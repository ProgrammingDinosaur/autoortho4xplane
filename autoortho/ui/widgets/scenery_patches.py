"""Compact scenery patch status summary."""

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .status_badge import StatusBadge


def _status(status):
    value = status.value if hasattr(status, "value") else str(status or "")
    return {
        "applied": ("Applied", "success"),
        "partially_applied": ("Partial", "warning"),
    }.get(value, ("Not applied", "info"))


class SceneryPatchesWidget(QWidget):
    def __init__(
        self,
        parent=None,
        seasons_status=None,
        roughness_status=None,
        roughness_value=None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName("SceneryPatchFrame")
        frame_layout = QVBoxLayout(frame)
        title = QLabel("Scenery Patches")
        title.setProperty("textRole", "sectionTitle")
        frame_layout.addWidget(title)
        self._add_row(frame_layout, "Seasons", seasons_status)
        reflectivity = "Terrain reflectivity"
        if roughness_value is not None:
            reflectivity += f" ({roughness_value:.1f})"
        self._add_row(
            frame_layout,
            reflectivity,
            roughness_status,
        )
        layout.addWidget(frame)

    @staticmethod
    def _add_row(layout, title, status):
        row = QHBoxLayout()
        label = QLabel(title)
        label.setProperty("textRole", "secondary")
        text, state = _status(status)
        badge = StatusBadge(text, state)
        row.addWidget(label)
        row.addStretch()
        row.addWidget(badge)
        layout.addLayout(row)

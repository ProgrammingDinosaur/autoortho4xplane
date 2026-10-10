"""Terrain reflectivity value dialog."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)


class RoughnessValueDialog(QDialog):
    def __init__(
        self,
        parent=None,
        current_value: float = 1.0,
        is_update: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(
            "Change Terrain Reflectivity"
            if is_update
            else "Apply Terrain Reflectivity"
        )
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Terrain reflectivity controls X-Plane's SUPER_ROUGHNESS "
            "setting. Higher values reduce the shiny appearance of terrain "
            "at sunrise and sunset."
        )
        info.setWordWrap(True)
        info.setProperty("textRole", "secondary")
        layout.addWidget(info)

        value_row = QHBoxLayout()
        value_label = QLabel("&Reflectivity")
        self.value_spin = QDoubleSpinBox()
        self.value_spin.setRange(0.0, 1.0)
        self.value_spin.setDecimals(1)
        self.value_spin.setSingleStep(0.1)
        self.value_spin.setValue(current_value)
        self.value_spin.setAccessibleName("Terrain reflectivity value")
        value_label.setBuddy(self.value_spin)
        value_row.addWidget(value_label)
        value_row.addWidget(self.value_spin)
        layout.addLayout(value_row)

        self.roughness_slider = QSlider(Qt.Orientation.Horizontal)
        self.roughness_slider.setRange(0, 10)
        self.roughness_slider.setTickInterval(1)
        self.roughness_slider.setTickPosition(
            QSlider.TickPosition.TicksBelow
        )
        self.roughness_slider.setValue(round(current_value * 10))
        self.roughness_slider.setAccessibleName(
            "Terrain reflectivity slider"
        )
        layout.addWidget(self.roughness_slider)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("Presets"))
        for value, name in (
            (1.0, "Matte"),
            (0.8, "Semi-matte"),
            (0.5, "Balanced"),
        ):
            button = QPushButton(f"{name} ({value:.1f})")
            button.clicked.connect(
                lambda checked=False, selected=value: (
                    self.value_spin.setValue(selected)
                )
            )
            presets.addWidget(button)
        presets.addStretch()
        layout.addLayout(presets)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Update" if is_update else "Apply"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.roughness_slider.valueChanged.connect(
            lambda value: self.value_spin.setValue(value / 10.0)
        )
        self.value_spin.valueChanged.connect(
            lambda value: self.roughness_slider.setValue(round(value * 10))
        )

    def get_value(self) -> float:
        return self.value_spin.value()

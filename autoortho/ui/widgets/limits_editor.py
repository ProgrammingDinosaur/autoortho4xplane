"""Edit the resource limits: Auto, or the user's own ceiling per resource."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho import limits as limits_module
else:
    import limits as limits_module

GB = 1024 ** 3

# name: (label, unit, step, decimals, help)
ROWS = {
    "ram_gb": (
        "RAM", "GB", 0.5, 1,
        "Memory cache, build buffers and the app together.",
    ),
    "vram_gb": (
        "Graphics memory", "GB", 0.5, 1,
        "Photo textures in X-Plane. Auto leaves room for X-Plane itself.",
    ),
    "bandwidth_mbps": (
        "Download speed", "Mbit/s", 5, 0,
        "Leave some of your connection for other apps. Auto: no limit.",
    ),
    "cpu_cores": (
        "CPU cores", "cores", 1, 0,
        "Cores for building textures. Auto keeps cores free for X-Plane.",
    ),
}


class LimitsEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Resource limits")
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setColumnStretch(3, 1)
        self.auto_checks = {}
        self.spins = {}
        self.hints = {}
        self._auto = {}
        for row, (name, (label, unit, step, decimals, help_text)) in enumerate(
            ROWS.items()
        ):
            title = QLabel(label)
            title.setToolTip(help_text)
            auto = QCheckBox("Auto")
            auto.setChecked(True)
            auto.setAccessibleName(f"{label}: automatic")
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setSingleStep(step)
            spin.setRange(1, 100000)
            spin.setSuffix(f" {unit}")
            # Shown while Auto is ticked and there is no automatic number.
            spin.setSpecialValueText("no limit" if name == "bandwidth_mbps" else "unknown")
            spin.setEnabled(False)
            spin.setAccessibleName(f"{label} limit")
            spin.setToolTip(help_text)
            hint = QLabel("")
            hint.setProperty("textRole", "secondary")
            hint.setWordWrap(True)
            grid.addWidget(title, row, 0)
            grid.addWidget(auto, row, 1)
            grid.addWidget(spin, row, 2)
            grid.addWidget(hint, row, 3)
            auto.toggled.connect(lambda checked, n=name: self._auto_toggled(n, checked))
            spin.valueChanged.connect(lambda _v: self.changed.emit())
            self.auto_checks[name] = auto
            self.spins[name] = spin
            self.hints[name] = hint

    def set_hardware(self, hw, reserve_gb=limits_module.DEFAULT_XPLANE_VRAM_RESERVE_GB):
        """Show what Auto means on this PC and bound the spin boxes."""
        self._auto = {
            "ram_gb": limits_module.auto_ram_gb(hw),
            "vram_gb": limits_module.auto_vram_gb(hw, reserve_gb),
            "bandwidth_mbps": None,
            "cpu_cores": limits_module.auto_cpu_cores(hw),
        }
        tops = {
            "ram_gb": (hw.ram_total / GB) if hw and hw.ram_total else 1024,
            "vram_gb": (hw.vram / GB) if hw and hw.vram else 256,
            "bandwidth_mbps": 100000,
            "cpu_cores": hw.physical_cores if hw and hw.physical_cores else 512,
        }
        for name, spin in self.spins.items():
            spin.blockSignals(True)
            spin.setMaximum(max(1, float(tops[name])))
            spin.blockSignals(False)
            value = self._auto[name]
            unit = ROWS[name][1]
            if value is None:
                text = "Auto: no limit" if name == "bandwidth_mbps" else "Auto: unknown on this PC"
            else:
                text = f"Auto on this PC: {value:g} {unit}"
            self.hints[name].setText(text)
            if self.auto_checks[name].isChecked():
                self._show_auto(name)

    def _show_auto(self, name):
        """While Auto is ticked the greyed box shows the automatic value."""
        spin = self.spins[name]
        value = self._auto.get(name)
        spin.blockSignals(True)
        spin.setMinimum(0 if value is None else 1)
        spin.setValue(0 if value is None else float(value))
        spin.blockSignals(False)

    def _auto_toggled(self, name, checked):
        spin = self.spins[name]
        spin.setEnabled(not checked)
        if checked:
            self._show_auto(name)
        else:
            spin.setMinimum(1)
            start = self._auto.get(name)
            if start is None:
                start = 100 if name == "bandwidth_mbps" else spin.value()
            spin.blockSignals(True)
            spin.setValue(float(start))
            spin.blockSignals(False)
        self.changed.emit()

    def values(self):
        """name -> "auto" or the number as config text."""
        out = {}
        for name, auto in self.auto_checks.items():
            if auto.isChecked():
                out[name] = limits_module.AUTO
            else:
                value = self.spins[name].value()
                out[name] = (
                    str(int(round(value))) if ROWS[name][3] == 0 or value == int(value)
                    else f"{value:g}"
                )
        return out

    def set_values(self, values):
        for name, auto in self.auto_checks.items():
            parsed = limits_module.parse((values or {}).get(name, limits_module.AUTO))
            spin = self.spins[name]
            auto.blockSignals(True)
            spin.blockSignals(True)
            auto.setChecked(parsed is None)
            spin.setEnabled(parsed is not None)
            if parsed is not None:
                spin.setMinimum(1)
                spin.setValue(parsed)
            auto.blockSignals(False)
            spin.blockSignals(False)
            if parsed is None:
                self._show_auto(name)
        self.changed.emit()

    def snapshot(self):
        return tuple(sorted(self.values().items()))

    def use_all_available(self):
        self.set_values({name: limits_module.AUTO for name in ROWS})

"""Scenery streaming controls shown at the top of the Scenery Library."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho import tile_stream
    from autoortho.ui.readiness import format_bytes
else:
    import tile_stream
    from ui.readiness import format_bytes


class StreamPanel(QGroupBox):
    """Turn tile streaming on or off and set its disk limit.

    The panel only emits requests; the main window does the work and calls
    :meth:`set_state` with the result.
    """

    enable_requested = Signal(str, bool, int)   # source, overlays, disk limit GB
    disable_requested = Signal()
    limit_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__("Stream scenery", parent)
        self.enabled = False
        self.busy = False
        self.runtime_locked = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Fetch each scenery tile the first time X-Plane needs it instead "
            "of downloading whole regions. Tiles you have downloaded are "
            "always used first."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        source_label = QLabel("&Scenery set")
        self.source_combo = QComboBox()
        source_label.setBuddy(self.source_combo)
        model = self.source_combo.model()
        for key, spec in tile_stream.SOURCES.items():
            label = spec["label"] if spec.get("available") else f"{spec['label']} — Soon"
            self.source_combo.addItem(label, key)
            if not spec.get("available"):
                item = model.item(self.source_combo.count() - 1)
                item.setEnabled(False)
                item.setToolTip("Coming soon: the XP12 packages are being repackaged for streaming.")
        self.overlays_check = QCheckBox("Stream &overlays (roads, forests, buildings)")
        self.overlays_check.setChecked(True)
        self.overlays_check.setToolTip(
            "Turn off if you use simHeaven X-World or another overlay package."
        )
        limit_label = QLabel("Disk &limit")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(5, 1000)
        self.limit_spin.setSuffix(" GB")
        self.limit_spin.setValue(20)
        self.limit_spin.setToolTip(
            "Least recently used tiles are removed above this size. "
            "Overlays get a quarter of it on top."
        )
        limit_label.setBuddy(self.limit_spin)
        row.addWidget(source_label)
        row.addWidget(self.source_combo, 1)
        row.addWidget(limit_label)
        row.addWidget(self.limit_spin)
        layout.addLayout(row)
        layout.addWidget(self.overlays_check)

        actions = QHBoxLayout()
        self.status_label = QLabel("Streaming is off.")
        self.status_label.setWordWrap(True)
        self.toggle_button = QPushButton("Turn on streaming")
        self.toggle_button.setProperty("role", "primary")
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.toggle_button)
        layout.addLayout(actions)

        self.toggle_button.clicked.connect(self._toggle)
        self.limit_spin.editingFinished.connect(self._limit_edited)
        self._refresh_controls()

    def _toggle(self):
        if self.enabled:
            self.disable_requested.emit()
        else:
            self.enable_requested.emit(
                str(self.source_combo.currentData()),
                self.overlays_check.isChecked(),
                self.limit_spin.value(),
            )

    def _limit_edited(self):
        if self.enabled:
            self.limit_changed.emit(self.limit_spin.value())

    def set_runtime_locked(self, locked: bool) -> None:
        self.runtime_locked = bool(locked)
        self._refresh_controls()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = bool(busy)
        if message:
            self.status_label.setText(message)
        self._refresh_controls()

    def set_state(self, packages: dict, usage_bytes: int = 0, dsf_count: int = 0) -> None:
        """Show the streaming packages found on disk (name -> marker)."""
        self.busy = False
        ortho = [m for m in packages.values() if m.get("kind", "ortho") == "ortho"]
        self.enabled = bool(ortho)
        if self.enabled:
            marker = ortho[0]
            source = marker.get("source", "")
            index = self.source_combo.findData(source)
            if index >= 0:
                self.source_combo.setCurrentIndex(index)
            self.overlays_check.setChecked(
                any(m.get("kind") == "overlay" for m in packages.values())
            )
            limit = int(round(float(marker.get("disk_limit_gb") or 20)))
            self.limit_spin.setValue(max(self.limit_spin.minimum(), limit))
            label = tile_stream.SOURCES.get(source, {}).get("label", source)
            self.status_label.setText(
                f"Streaming is on: {label}. {dsf_count:,} tiles stored "
                f"({format_bytes(usage_bytes)} of {limit} GB)."
            )
        else:
            self.status_label.setText("Streaming is off.")
        self._refresh_controls()

    def _refresh_controls(self):
        editable = not self.busy and not self.runtime_locked
        self.toggle_button.setText(
            "Turn off streaming" if self.enabled else "Turn on streaming"
        )
        self.toggle_button.setProperty("role", "destructive" if self.enabled else "primary")
        self.toggle_button.setEnabled(editable)
        self.source_combo.setEnabled(editable and not self.enabled)
        self.overlays_check.setEnabled(editable and not self.enabled)
        self.limit_spin.setEnabled(not self.busy)
        tip = "Stop AutoOrtho to change streaming." if self.runtime_locked else ""
        self.toggle_button.setToolTip(tip)

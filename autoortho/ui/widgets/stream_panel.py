"""AutoOrtho scenery controls shown at the top of the Scenery Library.

One streaming package per scenery set holds every tile AutoOrtho has: tiles
downloaded on the map (kept) and, unless the user turns it off, tiles
downloaded the first time X-Plane needs them.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
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


OVERLAY_HELP = "Turn this off if you use simHeaven X-World or another overlay package."
NO_OVERLAY_HELP = (
    "This scenery set has no overlays. Use simHeaven X-World for roads, "
    "forests and buildings."
)
ROUGHNESS_HELP = (
    "Higher values make terrain less shiny at sunrise and sunset (1.0 is "
    "matte). Installed regions have their own setting in Scenery Patches."
)
FETCH_HELP = (
    "Tiles download the first time X-Plane needs them. Turn this off to use "
    "only the tiles you download on the map; elsewhere X-Plane shows its "
    "default scenery."
)
LOCKED_HELP = "Streaming settings are locked while AutoOrtho or X-Plane is running."


def _secondary(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("textRole", "secondary")
    return label


class StreamPanel(QGroupBox):
    """Streaming on/off switch plus its settings.

    Settings can be changed whether streaming is on or off; while it is on,
    each change is applied right away. The panel only emits requests: the
    main window does the work (asking first when tiles would be deleted)
    and calls :meth:`set_state` with the result, or :meth:`restore` when
    the user cancels.
    """

    enable_requested = Signal(str, bool, int)   # source, overlays, disk limit GB
    disable_requested = Signal()
    source_change_requested = Signal(str)
    overlays_change_requested = Signal(bool)
    limit_changed = Signal(int)
    roughness_change_requested = Signal(object)   # float, or None to clear
    fetch_missing_change_requested = Signal(bool)
    open_map_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("AutoOrtho scenery", parent)
        self.busy = False
        self.runtime_locked = False
        self.state = {
            "enabled": False, "source": None, "overlays": False,
            "disk_limit_gb": None, "roughness": None, "fetch_missing": True,
            "ortho": (0, 0), "overlay": (0, 0),
        }

        layout = QVBoxLayout(self)

        self.enable_check = QCheckBox("Use AutoOrtho &scenery")
        self.enable_check.setToolTip(
            "Download tiles on the map, or as X-Plane needs them, instead of "
            "installing whole regions."
        )
        layout.addWidget(self.enable_check)
        layout.addWidget(_secondary(
            "Pick a scenery set, then download the tiles or regions you want "
            "on the map. Regions installed earlier are always used first."
        ))

        row = QHBoxLayout()
        source_label = QLabel("&Scenery set")
        self.source_combo = QComboBox()
        source_label.setBuddy(self.source_combo)
        model = self.source_combo.model()
        first_available = -1
        for key, spec in tile_stream.SOURCES.items():
            available = bool(spec.get("available"))
            label = spec["label"] if available else f"{spec['label']} — Soon"
            self.source_combo.addItem(label, key)
            index = self.source_combo.count() - 1
            if available and first_available < 0:
                first_available = index
            if not available:
                item = model.item(index)
                item.setEnabled(False)
                item.setToolTip(
                    "Coming soon: these packages are being repackaged for streaming."
                )
        if first_available >= 0:
            self.source_combo.setCurrentIndex(first_available)
        limit_label = QLabel("Disk &limit")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(5, 1000)
        self.limit_spin.setSuffix(" GB")
        self.limit_spin.setValue(20)
        self.limit_spin.setToolTip(
            "Tiles downloaded while flying that you haven't used for a while "
            "are removed above this size. Tiles you download on the map don't "
            "count. Overlays get a quarter of it on top."
        )
        limit_label.setBuddy(self.limit_spin)
        row.addWidget(source_label)
        row.addWidget(self.source_combo, 1)
        row.addSpacing(12)
        row.addWidget(limit_label)
        row.addWidget(self.limit_spin)
        layout.addLayout(row)

        fetch_row = QHBoxLayout()
        self.fetch_check = QCheckBox("&Download tiles while flying")
        self.fetch_check.setChecked(True)
        self.map_button = QPushButton("Download Tiles on the Map…")
        self.map_button.setAccessibleName("Open the map to download tiles or regions")
        fetch_row.addWidget(self.fetch_check)
        fetch_row.addStretch()
        fetch_row.addWidget(self.map_button)
        layout.addLayout(fetch_row)
        layout.addWidget(_secondary(FETCH_HELP))

        self.overlays_check = QCheckBox("Include &overlays (roads, forests, buildings)")
        self.overlays_check.setChecked(True)
        self.overlays_help = _secondary(OVERLAY_HELP)
        layout.addWidget(self.overlays_check)
        layout.addWidget(self.overlays_help)

        roughness_row = QHBoxLayout()
        self.roughness_check = QCheckBox("Set terrain &reflectivity")
        self.roughness_spin = QDoubleSpinBox()
        self.roughness_spin.setRange(0.0, 1.0)
        self.roughness_spin.setDecimals(1)
        self.roughness_spin.setSingleStep(0.1)
        self.roughness_spin.setValue(1.0)
        self.roughness_spin.setAccessibleName("Terrain reflectivity of streamed tiles")
        roughness_row.addWidget(self.roughness_check)
        roughness_row.addWidget(self.roughness_spin)
        roughness_row.addStretch()
        layout.addLayout(roughness_row)
        layout.addWidget(_secondary(ROUGHNESS_HELP))

        usage_row = QHBoxLayout()
        self.usage_label = _secondary()
        self.usage_label.setWordWrap(False)
        self.usage_bar = QProgressBar()
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setStyleSheet(
            "QProgressBar { min-height: 6px; max-height: 6px; }"
        )
        self.usage_bar.setAccessibleName("Streamed tiles disk use")
        usage_row.addWidget(self.usage_label)
        usage_row.addSpacing(12)
        usage_row.addWidget(self.usage_bar, 1)
        layout.addLayout(usage_row)

        self.status_label = _secondary()
        layout.addWidget(self.status_label)

        self.enable_check.clicked.connect(self._enable_clicked)
        self.source_combo.activated.connect(self._source_activated)
        self.overlays_check.clicked.connect(self._overlays_clicked)
        self.limit_spin.editingFinished.connect(self._limit_edited)
        self.roughness_check.clicked.connect(self._roughness_edited)
        self.roughness_spin.editingFinished.connect(self._roughness_edited)
        self.fetch_check.clicked.connect(self._fetch_clicked)
        self.map_button.clicked.connect(self.open_map_requested)
        self._show_state()

    # -- state ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.state.get("enabled"))

    @property
    def roughness(self):
        """The terrain reflectivity picked, or None for X-Plane's default."""
        if not self.roughness_check.isChecked():
            return None
        return round(self.roughness_spin.value(), 1)

    def set_state(self, summary: dict) -> None:
        """Show what is on disk (see :func:`tile_stream.stream_summary`)."""
        self.busy = False
        self.state = dict(summary)
        self._show_state()

    def restore(self) -> None:
        """Put the controls back to the last known state (a change was cancelled)."""
        self.busy = False
        self._show_state()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = bool(busy)
        self._refresh_controls()
        if busy and message:
            self.status_label.setText(message)
            self.status_label.show()

    def set_runtime_locked(self, locked: bool) -> None:
        locked = bool(locked)
        unlocked = self.runtime_locked and not locked
        self.runtime_locked = locked
        self._refresh_controls()
        if unlocked:
            self.refresh_requested.emit()  # tiles were fetched while running

    def _show_state(self):
        state = self.state
        self.enable_check.setChecked(self.enabled)
        if self.enabled:
            index = self.source_combo.findData(state.get("source"))
            if index >= 0:
                self.source_combo.setCurrentIndex(index)
            self.overlays_check.setChecked(bool(state.get("overlays")))
            limit = int(round(float(state.get("disk_limit_gb") or 20)))
            self.limit_spin.setValue(max(self.limit_spin.minimum(), limit))
            self.fetch_check.setChecked(bool(state.get("fetch_missing", True)))
            roughness = state.get("roughness")
            self.roughness_check.setChecked(roughness is not None)
            if roughness is not None:
                self.roughness_spin.setValue(float(roughness))
        self._show_usage()
        self._refresh_controls()

    def _show_usage(self):
        if not self.enabled:
            self.usage_bar.hide()
            self.usage_label.hide()
            return
        size, tiles = self.state.get("ortho", (0, 0))
        overlay_size, _ = self.state.get("overlay", (0, 0))
        limit_gb = self.limit_spin.value()
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setValue(min(1000, int(1000 * size / (limit_gb * 1e9))))
        text = f"{tiles:,} tiles stored · {format_bytes(size)}"
        text += f" · limit {limit_gb} GB for tiles downloaded while flying"
        if self.state.get("overlays"):
            overlay_limit = max(1, limit_gb // 4)
            text += f" · overlays {format_bytes(overlay_size)} of {overlay_limit} GB"
        self.usage_label.setText(text)
        self.usage_bar.show()
        self.usage_label.show()

    def _source_has_overlays(self) -> bool:
        spec = tile_stream.SOURCES.get(self.source_combo.currentData(), {})
        return bool(spec.get("overlay_index"))

    def _refresh_controls(self):
        editable = not self.busy and not self.runtime_locked
        has_overlays = self._source_has_overlays()
        for widget in (self.enable_check, self.source_combo, self.limit_spin,
                       self.roughness_check, self.fetch_check):
            widget.setEnabled(editable)
        self.map_button.setEnabled(self.enabled)
        self.roughness_spin.setEnabled(editable and self.roughness_check.isChecked())
        self.overlays_check.setEnabled(editable and has_overlays)
        if not has_overlays:
            self.overlays_check.setChecked(False)
        self.overlays_help.setText(OVERLAY_HELP if has_overlays else NO_OVERLAY_HELP)
        if self.busy:
            return  # set_busy shows its own message
        if self.runtime_locked:
            message = LOCKED_HELP
        elif self.enabled:
            message = "Changes take effect the next time AutoOrtho starts."
        else:
            message = ""
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))

    # -- user actions ---------------------------------------------------

    def _enable_clicked(self, checked: bool):
        if checked:
            self.enable_requested.emit(
                str(self.source_combo.currentData()),
                self.overlays_check.isChecked() and self._source_has_overlays(),
                self.limit_spin.value(),
            )
        else:
            self.disable_requested.emit()

    def _fetch_clicked(self, checked: bool):
        if self.enabled and checked != bool(self.state.get("fetch_missing", True)):
            self.fetch_missing_change_requested.emit(checked)

    @property
    def fetch_missing(self) -> bool:
        return self.fetch_check.isChecked()

    def _roughness_edited(self, *_args):
        self.roughness_spin.setEnabled(
            self.roughness_check.isEnabled() and self.roughness_check.isChecked()
        )
        if self.enabled and self.roughness != self.state.get("roughness"):
            self.roughness_change_requested.emit(self.roughness)

    def _source_activated(self, _index: int):
        source = str(self.source_combo.currentData())
        self._refresh_controls()  # sets differ in whether they have overlays
        if self.enabled and source != self.state.get("source"):
            self.source_change_requested.emit(source)
        else:
            self._refresh_controls()  # overlay availability follows the set

    def _overlays_clicked(self, checked: bool):
        if self.enabled and checked != bool(self.state.get("overlays")):
            self.overlays_change_requested.emit(checked)

    def _limit_edited(self):
        if not self.enabled:
            return
        value = self.limit_spin.value()
        if value != int(round(float(self.state.get("disk_limit_gb") or 20))):
            self.limit_changed.emit(value)

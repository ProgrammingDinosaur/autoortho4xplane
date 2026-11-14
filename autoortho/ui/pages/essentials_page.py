"""Essentials: the settings most people need, on one page.

What only the user can decide: the quality target, the limits, the folders,
the imagery source and the look. AutoOrtho sizes everything else for this PC.
The controls here mirror the full settings pages (see ``ui.widgets.mirror``),
so saving and Revert work exactly as there.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.widgets.target_cost_panel import TargetCostPanel
else:
    from ui.widgets.target_cost_panel import TargetCostPanel

CUSTOM_PROFILE = "Custom steps"


def _path_row(edit, button):
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    layout.addWidget(button)
    return row


class EssentialsPage(QWidget):
    navigate_requested = Signal(str)        # a settings category name
    library_requested = Signal()
    browse_requested = Signal(object)        # the QLineEdit to fill
    profile_requested = Signal(str)          # an altitude profile name
    governor_mode_changed = Signal(str)

    def __init__(self, profile_names=(), parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # -- Quality target ---------------------------------------------------
        quality = QGroupBox("Quality Target")
        self.quality_group = quality
        q_layout = QVBoxLayout(quality)
        intro = QLabel(
            "How sharp the scenery should be. AutoOrtho sizes downloads, "
            "workers and memory to reach it on this PC."
        )
        intro.setWordWrap(True)
        q_layout.addWidget(intro)
        form = QFormLayout()
        self._quality_form = form
        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName("Detail mode")
        self.zoom_spin = QSpinBox()
        self.zoom_spin.setPrefix("ZL")
        self.zoom_spin.setAccessibleName("Scenery zoom level")
        self.airport_spin = QSpinBox()
        self.airport_spin.setPrefix("ZL")
        self.airport_spin.setAccessibleName("Airport zoom level")
        self.profile_combo = QComboBox()
        self.profile_combo.setAccessibleName("Altitude profile")
        self.profile_combo.addItems(list(profile_names) + [CUSTOM_PROFILE])
        form.addRow("Detail mode", self.mode_combo)
        form.addRow("Scenery zoom level", self.zoom_spin)
        form.addRow("Airport zoom level", self.airport_spin)
        form.addRow("Altitude profile", self.profile_combo)
        q_layout.addLayout(form)
        self.cost_panel = TargetCostPanel()
        q_layout.addWidget(self.cost_panel)
        more = QPushButton("More quality options…")
        more.clicked.connect(lambda: self.navigate_requested.emit("Imagery"))
        q_layout.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(quality)

        # -- Auto-tuning (governor) ----------------------------------------------
        tuning = QGroupBox("Auto-Tuning")
        self.tuning_group = tuning
        t_layout = QFormLayout(tuning)
        self.governor_combo = QComboBox()
        self.governor_combo.setAccessibleName("Auto-tuning mode")
        self.governor_label = QLabel()
        self.governor_label.setWordWrap(True)
        self.governor_label.setProperty("textRole", "secondary")
        t_layout.addRow("Mode", self.governor_combo)
        t_layout.addRow("", self.governor_label)
        layout.addWidget(tuning)
        tuning.setVisible(False)  # shown once the window gives it modes

        # -- Limits -------------------------------------------------------------
        limits = QGroupBox("Limits")
        l_layout = QVBoxLayout(limits)
        self.limits_label = QLabel()
        self.limits_label.setWordWrap(True)
        l_layout.addWidget(self.limits_label)
        change = QPushButton("Change limits or test my connection…")
        change.clicked.connect(lambda: self.navigate_requested.emit("Limits"))
        l_layout.addWidget(change, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(limits)

        # -- Folders ------------------------------------------------------------
        folders = QGroupBox("Folders and Cache")
        f_layout = QFormLayout(folders)
        self.xplane_edit = QLineEdit()
        self.scenery_edit = QLineEdit()
        self.cache_edit = QLineEdit()
        for label, edit in (
            ("X-Plane folder", self.xplane_edit),
            ("Scenery folder", self.scenery_edit),
            ("Image cache folder", self.cache_edit),
        ):
            edit.setAccessibleName(label)
            button = QPushButton("Browse…")
            button.clicked.connect(
                lambda _checked=False, e=edit: self.browse_requested.emit(e)
            )
            f_layout.addRow(label, _path_row(edit, button))
        self.disk_cache_spin = QSpinBox()
        self.disk_cache_spin.setSuffix(" GB")
        self.disk_cache_spin.setAccessibleName("Disk cache limit")
        f_layout.addRow("Disk cache limit", self.disk_cache_spin)
        layout.addWidget(folders)

        # -- Look ------------------------------------------------------------------
        look = QGroupBox("Imagery and Look")
        look_layout = QFormLayout(look)
        self.maptype_combo = QComboBox()
        self.maptype_combo.setAccessibleName("Imagery source")
        look_layout.addRow("Imagery source", self.maptype_combo)
        self.seasons_check = QCheckBox("Seasons")
        self.seasons_strength = QSlider(Qt.Orientation.Horizontal)
        self.seasons_strength.setRange(0, 100)
        self.seasons_strength.setAccessibleName("Seasons strength")
        self.seasons_strength.setToolTip(
            "Sets all four seasons at once. Fine-tune each one in "
            "Settings > Seasons."
        )
        look_layout.addRow(self.seasons_check, self.seasons_strength)
        self.night_check = QCheckBox(
            "Use X-Plane's own scenery at night"
        )
        look_layout.addRow("", self.night_check)
        shine = QPushButton("Reduce sunrise shine…")
        shine.setToolTip(
            "Terrain reflectivity is set per scenery pack, in the Scenery "
            "Library (Options on each pack)."
        )
        shine.clicked.connect(self.library_requested.emit)
        look_layout.addRow("", shine)
        layout.addWidget(look)

        # -- Flight plan and app -------------------------------------------------
        other = QGroupBox("Flight Plan and App")
        o_layout = QFormLayout(other)
        self.simbrief_edit = QLineEdit()
        self.simbrief_edit.setAccessibleName("SimBrief user ID")
        self.simbrief_check = QCheckBox("Use my SimBrief flight plan")
        self.hide_check = QCheckBox("Hide this window while streaming")
        o_layout.addRow("SimBrief user ID", self.simbrief_edit)
        o_layout.addRow("", self.simbrief_check)
        o_layout.addRow("", self.hide_check)
        layout.addWidget(other)

        self.mode_combo.currentIndexChanged.connect(self.update_mode_rows)
        self.profile_combo.currentTextChanged.connect(self._profile_chosen)
        self.governor_combo.currentIndexChanged.connect(
            lambda _i: self.governor_mode_changed.emit(
                self.governor_combo.currentData() or ""
            )
        )

    def groups(self):
        return [
            child for child in self.findChildren(QGroupBox)
            if child.parent() is self
        ]

    def _set_row_visible(self, field, visible):
        field.setVisible(visible)
        label = self._quality_form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def update_mode_rows(self, *_args):
        dynamic = self.mode_combo.currentIndex() == 1
        self._set_row_visible(self.zoom_spin, not dynamic)
        self._set_row_visible(self.airport_spin, not dynamic)
        self._set_row_visible(self.profile_combo, dynamic)

    def show_profile(self, name):
        """Show which altitude profile the steps match (or Custom)."""
        target = name if self.profile_combo.findText(name) >= 0 else CUSTOM_PROFILE
        self.profile_combo.blockSignals(True)
        self.profile_combo.setCurrentText(target)
        self.profile_combo.blockSignals(False)

    def _profile_chosen(self, name):
        if name and name != CUSTOM_PROFILE:
            self.profile_requested.emit(name)

    def set_limits_text(self, text):
        self.limits_label.setText(text)

    def set_governor_modes(self, modes, current, description=""):
        """modes: [(value, label)]."""
        self.governor_combo.blockSignals(True)
        self.governor_combo.clear()
        for value, label in modes:
            self.governor_combo.addItem(label, value)
        index = self.governor_combo.findData(current)
        self.governor_combo.setCurrentIndex(max(0, index))
        self.governor_combo.blockSignals(False)
        self.governor_label.setText(description)
        self.tuning_group.setVisible(bool(modes))

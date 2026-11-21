"""AutoOrtho scenery controls shown at the top of the Scenery Library.

One streaming package per scenery set holds every tile AutoOrtho has: tiles
downloaded on the map (kept) and, unless the user turns it off, tiles
downloaded the first time X-Plane needs them.

The panel answers three questions in order: which scenery set, where tiles
come from (as you fly, or only from the map), and how much disk the tiles
downloaded while flying may use. A hint card above them points out tiles of
another set (regions installed with an earlier version) and a set that
doesn't suit the X-Plane in use. The setup wizard reuses :class:`SetChoice`
and :class:`TileSourceChoice` so both say the same thing.
"""

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho import tile_stream
    from autoortho.ui.readiness import format_bytes
    from autoortho.ui.theme import repolish
else:
    import tile_stream
    from ui.readiness import format_bytes
    from ui.theme import repolish


INTRO_HELP = (
    "Scenery tiles are the terrain and water mesh that AutoOrtho's imagery is "
    "draped on. Pick a set, then choose where its tiles come from."
)
OVERLAY_HELP = "Turn this off if you use simHeaven X-World or another overlay package."
NO_OVERLAY_HELP = (
    "This scenery set has no overlays. Use simHeaven X-World for roads, "
    "forests and buildings."
)
ROUGHNESS_HELP = (
    "Higher values make terrain less shiny at sunrise and sunset (1.0 is "
    "matte). Installed regions have their own setting in Scenery Patches."
)
SEASONS_HELP = (
    "Adds X-Plane 12's seasons to the XP11 set's tiles you download on the "
    "map, a few seconds per tile. Tiles downloaded while flying stay as they are."
)
AUTO_HELP = (
    "Each tile downloads the first time X-Plane needs it, usually in a second "
    "or two, without slowing down imagery, and stays on disk for next time."
)
MAP_ONLY_HELP = (
    "Elsewhere X-Plane shows its default scenery. Use this to fly offline or "
    "to control disk use exactly."
)
MAP_HELP = (
    "Tiles you download on the map work offline and are never removed to "
    "make room."
)
LIMIT_SENTENCE = (
    "When they fill it, the ones you haven't flown over for longest are "
    "removed. Tiles you download on the map are never removed."
)
# The Scenery Library's link to help picking a set and where tiles come from.
CHOOSE_URL = (
    "https://programmingdinosaur.github.io/autoortho4xplane/scenery/"
    "#which-set-should-i-choose"
)
LIMIT_HELP = (
    "Tiles downloaded while flying that you haven't used for a while are "
    "removed above this size. Tiles you download on the map don't count. "
    "Overlays get a quarter of it on top."
)
LOCKED_HELP = "Streaming settings are locked while AutoOrtho or X-Plane is running."
SOON_HELP = "Coming soon: these packages are being repackaged for streaming."


def _secondary(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("textRole", "secondary")
    return label


def _indented(label: QLabel) -> QLabel:
    """Line a description up with its radio button's text."""
    label.setContentsMargins(24, 0, 0, 4)
    return label


def _chip(text: str) -> QLabel:
    chip = QLabel(text)
    chip.setObjectName("chipLabel")
    chip.setProperty("state", "success")
    return chip


def set_title(source: Optional[str]) -> str:
    spec = tile_stream.SOURCES.get(source or "", {})
    return spec.get("title") or spec.get("label") or (source or "")


class SetChoice(QWidget):
    """The scenery sets as radio buttons, each saying what it gives you.

    ``picked`` is sent for a user's click only, not for :meth:`set_current`.
    """

    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recommended: Optional[str] = None
        self.xplane_major: Optional[int] = None
        self.buttons: dict = {}
        self.chips: dict = {}
        self.notes: dict = {}
        self.group = QButtonGroup(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for key, spec in tile_stream.SOURCES.items():
            available = bool(spec.get("available"))
            row = QHBoxLayout()
            button = QRadioButton(set_title(key))
            button.setEnabled(available)
            chip = _chip("Recommended")
            chip.hide()
            row.addWidget(button)
            row.addWidget(chip)
            row.addStretch()
            layout.addLayout(row)
            summary = spec.get("summary", "") if available else SOON_HELP
            description = _indented(_secondary(summary))
            layout.addWidget(description)
            note = _indented(_secondary())
            note.setProperty("textRole", "warning")
            note.hide()
            layout.addWidget(note)
            credit = spec.get("credit")
            if credit:
                button.setToolTip(f"By {credit}.")
                button.setAccessibleDescription(f"{summary} By {credit}.")
            else:
                button.setAccessibleDescription(summary)
            self.group.addButton(button)
            self.buttons[key] = button
            self.chips[key] = chip
            self.notes[key] = note
        self.group.buttonClicked.connect(self._clicked)
        self.set_xplane_major(None)
        self.set_current(self.recommended)

    def current(self) -> Optional[str]:
        for key, button in self.buttons.items():
            if button.isChecked():
                return key
        return None

    def set_current(self, key: Optional[str]) -> None:
        button = self.buttons.get(key or "")
        if button is not None:
            button.setChecked(True)

    def is_available(self, key: Optional[str]) -> bool:
        return bool(tile_stream.SOURCES.get(key or "", {}).get("available"))

    def set_xplane_major(self, major: Optional[int]) -> None:
        """Recommend the set that suits this X-Plane, and say which sets it
        can't show."""
        self.xplane_major = major
        self.recommended = tile_stream.recommended_source(major)
        for key, chip in self.chips.items():
            chip.setVisible(key == self.recommended)
            need = tile_stream.SOURCES[key].get("xplane")
            note = self.notes[key]
            if major is not None and need and need > major:
                note.setText(f"Needs X-Plane {need}; this X-Plane is version {major}.")
                note.show()
            else:
                note.hide()

    def _clicked(self, button):
        for key, candidate in self.buttons.items():
            if candidate is button:
                self.picked.emit(key)
                return


class TileSourceChoice(QWidget):
    """Where tiles come from: as you fly (with the map), or the map only.

    ``picked(fetch_missing)`` is sent for a user's click only.
    """

    picked = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.auto_radio = QRadioButton("&Automatically as you fly")
        self.map_radio = QRadioButton("Only tiles I download on the &map")
        self.auto_chip = _chip("Recommended")
        row = QHBoxLayout()
        row.addWidget(self.auto_radio)
        row.addWidget(self.auto_chip)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(_indented(_secondary(AUTO_HELP)))
        layout.addWidget(self.map_radio)
        layout.addWidget(_indented(_secondary(MAP_ONLY_HELP)))
        self.group = QButtonGroup(self)
        self.group.addButton(self.auto_radio)
        self.group.addButton(self.map_radio)
        self.auto_radio.setChecked(True)
        self.group.buttonClicked.connect(
            lambda button: self.picked.emit(button is self.auto_radio))

    def fetch_missing(self) -> bool:
        return self.auto_radio.isChecked()

    def set_fetch_missing(self, enabled: bool) -> None:
        (self.auto_radio if enabled else self.map_radio).setChecked(True)


class SceneryHint(QFrame):
    """A card above the settings with one thing worth doing, and buttons.

    ``action(name)`` is sent with the name given to :meth:`show_hint`.
    """

    action = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.key = ""
        self.shown = None   # what show_hint was last given
        self.setObjectName("SceneryCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.title_label.setProperty("textRole", "sectionTitle")
        self.body_label = _secondary()
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        self.buttons_row = QHBoxLayout()
        self.buttons_row.addStretch()
        layout.addLayout(self.buttons_row)
        self.buttons: dict = {}
        self.hide()

    def show_hint(self, key: str, state: str, title: str, body: str,
                  buttons) -> None:
        """``buttons``: (action name, label, role) in order, the first
        being the main one."""
        self.shown = (key, state, title, body, tuple(buttons))
        self.key = key
        self.setProperty("state", state)
        repolish(self)
        self.title_label.setText(title)
        self.body_label.setText(body)
        self.setAccessibleName(title)
        for button in self.buttons.values():
            self.buttons_row.removeWidget(button)
            button.deleteLater()
        self.buttons = {}
        for name, label, role in buttons:
            button = QPushButton(label)
            button.setProperty("role", role)
            button.clicked.connect(lambda _=False, n=name: self.action.emit(n))
            self.buttons_row.addWidget(button)
            self.buttons[name] = button
        self.show()

    def clear(self) -> None:
        self.key = ""
        self.shown = None
        self.hide()

    def set_buttons_enabled(self, enabled: bool) -> None:
        for button in self.buttons.values():
            button.setEnabled(enabled)


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
    seasons_change_requested = Signal(bool)
    open_map_requested = Signal()
    refresh_requested = Signal()
    # Kept tiles of another set: "replace" (download the set in use's
    # version) or "remove".
    other_set_tiles_requested = Signal(str)
    hint_dismissed = Signal(str)                  # the hint's key, to remember

    def __init__(self, parent=None):
        super().__init__("AutoOrtho scenery", parent)
        self.busy = False
        self.runtime_locked = False
        self._source_chosen = False   # the user picked a set while it was off
        self.dismissed_hints: set = set()
        self.state = {
            "enabled": False, "source": None, "overlays": False,
            "disk_limit_gb": None, "roughness": None, "fetch_missing": True,
            "seasons": False, "ortho": (0, 0), "overlay": (0, 0),
            "other_sets": {}, "xplane_major": None,
        }

        layout = QVBoxLayout(self)

        self.enable_check = QCheckBox("Use AutoOrtho &scenery")
        self.enable_check.setToolTip(
            "Download tiles on the map, or as X-Plane needs them, instead of "
            "installing whole regions."
        )
        layout.addWidget(self.enable_check)
        layout.addWidget(_secondary(INTRO_HELP))

        self.hint = SceneryHint()
        layout.addWidget(self.hint)

        set_row = QHBoxLayout()
        set_title_label = QLabel("Scenery set")
        set_title_label.setProperty("textRole", "sectionTitle")
        self.choose_link = QLabel(f'<a href="{CHOOSE_URL}">Which set should I choose?</a>')
        self.choose_link.setTextFormat(Qt.TextFormat.RichText)
        self.choose_link.setOpenExternalLinks(True)
        self.choose_link.setToolTip("Opens the scenery guide in your browser.")
        set_row.addWidget(set_title_label)
        set_row.addStretch()
        set_row.addWidget(self.choose_link)
        layout.addLayout(set_row)
        self.source_choice = SetChoice()
        self.source_choice.setAccessibleName("Scenery set")
        layout.addWidget(self.source_choice)

        fetch_title = QLabel("Where tiles come from")
        fetch_title.setProperty("textRole", "sectionTitle")
        layout.addWidget(fetch_title)
        self.fetch_choice = TileSourceChoice()
        self.fetch_choice.setAccessibleName("Where tiles come from")
        layout.addWidget(self.fetch_choice)

        map_row = QHBoxLayout()
        self.map_button = QPushButton("Download Tiles on the Map…")
        self.map_button.setAccessibleName("Open the map to download tiles or regions")
        map_row.addWidget(_secondary(MAP_HELP), 1)
        map_row.addWidget(self.map_button)
        layout.addLayout(map_row)

        limit_row = QHBoxLayout()
        limit_label = QLabel("Keep &up to")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(5, 1000)
        self.limit_spin.setSuffix(" GB")
        self.limit_spin.setValue(20)
        self.limit_spin.setToolTip(LIMIT_HELP)
        self.limit_spin.setAccessibleName("Disk limit for tiles downloaded while flying")
        limit_label.setBuddy(self.limit_spin)
        limit_row.addWidget(limit_label)
        limit_row.addWidget(self.limit_spin)
        limit_row.addWidget(QLabel("of tiles downloaded while flying"))
        limit_row.addStretch()
        layout.addLayout(limit_row)
        layout.addWidget(_secondary(LIMIT_SENTENCE))

        # What is stored: kept tiles apart, since the limit doesn't apply
        # to them, and the bar for the tiles it does apply to.
        self.kept_label = _secondary()
        layout.addWidget(self.kept_label)
        usage_row = QHBoxLayout()
        self.usage_label = _secondary()
        self.usage_label.setWordWrap(False)
        self.usage_bar = QProgressBar()
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setProperty("thin", True)  # theme: a 6 px meter
        self.usage_bar.setAccessibleName(
            "Disk use of tiles downloaded while flying, against the limit")
        usage_row.addWidget(self.usage_label)
        usage_row.addSpacing(12)
        usage_row.addWidget(self.usage_bar, 1)
        layout.addLayout(usage_row)
        self.overlay_usage_label = _secondary()
        layout.addWidget(self.overlay_usage_label)

        self.overlays_check = QCheckBox("Include &overlays (roads, forests, buildings)")
        self.overlays_check.setChecked(True)
        self.overlays_help = _secondary(OVERLAY_HELP)
        layout.addWidget(self.overlays_check)
        layout.addWidget(self.overlays_help)

        self.seasons_check = QCheckBox("Add &seasons to downloaded tiles")
        self.seasons_help = _secondary(SEASONS_HELP)
        layout.addWidget(self.seasons_check)
        layout.addWidget(self.seasons_help)

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

        self.status_label = _secondary()
        layout.addWidget(self.status_label)

        self.enable_check.clicked.connect(self._enable_clicked)
        self.source_choice.picked.connect(self._source_picked)
        self.overlays_check.clicked.connect(self._overlays_clicked)
        self.limit_spin.editingFinished.connect(self._limit_edited)
        self.roughness_check.clicked.connect(self._roughness_edited)
        self.roughness_spin.editingFinished.connect(self._roughness_edited)
        self.fetch_choice.picked.connect(self._fetch_picked)
        self.seasons_check.clicked.connect(self._seasons_clicked)
        self.map_button.clicked.connect(self.open_map_requested)
        self.hint.action.connect(self._hint_action)
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

    @property
    def fetch_missing(self) -> bool:
        return self.fetch_choice.fetch_missing()

    def set_state(self, summary: dict) -> None:
        """Show what is on disk (see :func:`tile_stream.stream_summary`),
        plus ``xplane_major`` (:func:`tile_stream.xplane_major_version`)."""
        self.busy = False
        self.state = dict(summary)
        self._show_state()

    def set_dismissed_hints(self, keys) -> None:
        self.dismissed_hints = set(keys)
        self._show_hint()

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
        self.source_choice.set_xplane_major(state.get("xplane_major"))
        if not self.enabled and not self._source_chosen:
            # Until the user picks one, offer the set that suits this X-Plane.
            self.source_choice.set_current(self.source_choice.recommended)
        if self.enabled:
            self.source_choice.set_current(state.get("source"))
            self.overlays_check.setChecked(bool(state.get("overlays")))
            limit = int(round(float(state.get("disk_limit_gb") or 20)))
            self.limit_spin.setValue(max(self.limit_spin.minimum(), limit))
            self.fetch_choice.set_fetch_missing(bool(state.get("fetch_missing", True)))
            self.seasons_check.setChecked(bool(state.get("seasons")))
            roughness = state.get("roughness")
            self.roughness_check.setChecked(roughness is not None)
            if roughness is not None:
                self.roughness_spin.setValue(float(roughness))
        self._show_usage()
        self._show_hint()
        self._refresh_controls()

    def _show_usage(self):
        labels = (self.usage_bar, self.usage_label, self.kept_label,
                  self.overlay_usage_label)
        if not self.enabled:
            for widget in labels:
                widget.hide()
            return
        size, tiles = self.state.get("ortho", (0, 0))
        kept, kept_tiles = self.state.get("kept", (0, 0))
        cached, cached_tiles = max(0, size - kept), max(0, tiles - kept_tiles)
        limit_gb = self.limit_spin.value()
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setValue(min(1000, int(1000 * cached / (limit_gb * 1e9))))
        self.usage_label.setText(
            f"Downloaded while flying: {cached_tiles:,} "
            f"tile{'s' if cached_tiles != 1 else ''} · {format_bytes(cached)} "
            f"of {limit_gb} GB")
        self.kept_label.setText(
            f"Kept: {kept_tiles:,} tile{'s' if kept_tiles != 1 else ''} · "
            f"{format_bytes(kept)}, never removed")
        self.kept_label.setVisible(bool(kept_tiles))
        if self.state.get("overlays"):
            overlay_size, _ = self.state.get("overlay", (0, 0))
            overlay_limit = max(1, limit_gb // 4)
            self.overlay_usage_label.setText(
                f"Overlays: {format_bytes(overlay_size)} of {overlay_limit} GB "
                "(a quarter of the limit, on top of it)")
            self.overlay_usage_label.show()
        else:
            self.overlay_usage_label.hide()
        self.usage_bar.show()
        self.usage_label.show()

    def current_hint(self):
        """(key, state, title, body, buttons) of the hint to show, or None."""
        if not self.enabled:
            return None
        state = self.state
        current = state.get("source")
        recommended = self.source_choice.recommended
        major = state.get("xplane_major")
        if recommended and current != recommended:
            rec, cur = set_title(recommended), set_title(current)
            if major is not None:
                title = f"For X-Plane {major}, the {rec} set is recommended"
            else:
                need = tile_stream.SOURCES[recommended].get("xplane")
                title = (f"Flying X-Plane {need}? The {rec} set is recommended"
                         if need else f"The {rec} set is recommended")
            summary = tile_stream.SOURCES[recommended].get("summary", "")
            body = f"You're using the {cur} set."
            if current == "xp11":
                body += (" It's the set that regions installed with earlier "
                         "versions of AutoOrtho came from.")
            body += f" {rec}: {summary}"
            return (f"set:{current}", "info", title, body, (
                ("switch", f"Switch to {rec}…", "primary"),
                ("dismiss", f"Keep {cur}", "quiet"),
            ))
        others = {k: v for k, v in (state.get("other_sets") or {}).items()
                  if k != current and v and v[0]}
        if others:
            tiles = sum(v[0] for v in others.values())
            size = sum(v[1] for v in others.values())
            cur = set_title(current)
            old = " and ".join(set_title(k) for k in sorted(others))
            then = ("tiles then download as you fly"
                    if state.get("fetch_missing", True) else
                    "X-Plane then shows its default scenery there")
            title = (f"{tiles:,} kept tile{'s' if tiles != 1 else ''} "
                     f"({format_bytes(size)}) {'are' if tiles != 1 else 'is'} "
                     f"from the {old} set")
            body = (f"They came from regions installed with an earlier version "
                    f"of AutoOrtho, or were kept before you switched sets, so "
                    f"X-Plane shows the {old} scenery there. Replace them to get "
                    f"the {cur} version, or remove them to free the space "
                    f"({then}).")
            return (f"other:{current}:{tiles}", "warning", title, body, (
                ("replace", f"Replace with {cur}…", "primary"),
                ("remove", "Remove Them…", "secondary"),
                ("dismiss", "Keep Them", "quiet"),
            ))
        return None

    def _show_hint(self):
        hint = self.current_hint()
        if hint is None or hint[0] in self.dismissed_hints:
            self.hint.clear()
            return
        if hint != self.hint.shown or self.hint.isHidden():
            self.hint.show_hint(*hint)
        self.hint.set_buttons_enabled(not self.busy and not self.runtime_locked)

    def _refresh_controls(self):
        editable = not self.busy and not self.runtime_locked
        has_overlays = self._source_has_overlays()
        for widget in (self.enable_check, self.source_choice, self.limit_spin,
                       self.roughness_check, self.fetch_choice):
            widget.setEnabled(editable)
        self.hint.set_buttons_enabled(editable)
        self.map_button.setEnabled(self.enabled)
        self.roughness_spin.setEnabled(editable and self.roughness_check.isChecked())
        self.overlays_check.setEnabled(editable and has_overlays)
        if not has_overlays:
            self.overlays_check.setChecked(False)
        self.overlays_help.setText(OVERLAY_HELP if has_overlays else NO_OVERLAY_HELP)
        # Only the XP11 set lacks seasons; it applies to a set in use.
        seasons = self.enabled and self.state.get("source") == "xp11"
        self.seasons_check.setVisible(seasons)
        self.seasons_help.setVisible(seasons)
        self.seasons_check.setEnabled(editable and seasons)
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

    def _source_has_overlays(self) -> bool:
        spec = tile_stream.SOURCES.get(self.source_choice.current() or "", {})
        return bool(spec.get("overlay_index"))

    # -- user actions ---------------------------------------------------

    def _enable_clicked(self, checked: bool):
        if checked:
            self.enable_requested.emit(
                str(self.source_choice.current()),
                self.overlays_check.isChecked() and self._source_has_overlays(),
                self.limit_spin.value(),
            )
        else:
            self.disable_requested.emit()

    def _seasons_clicked(self, checked: bool):
        if self.enabled and checked != bool(self.state.get("seasons")):
            self.seasons_change_requested.emit(checked)

    def _fetch_picked(self, enabled: bool):
        if self.enabled and enabled != bool(self.state.get("fetch_missing", True)):
            self.fetch_missing_change_requested.emit(enabled)

    def _roughness_edited(self, *_args):
        self.roughness_spin.setEnabled(
            self.roughness_check.isEnabled() and self.roughness_check.isChecked()
        )
        if self.enabled and self.roughness != self.state.get("roughness"):
            self.roughness_change_requested.emit(self.roughness)

    def _source_picked(self, source: str):
        self._source_chosen = True
        self._refresh_controls()  # sets differ in whether they have overlays
        if self.enabled and source != self.state.get("source"):
            self.source_change_requested.emit(source)

    def _overlays_clicked(self, checked: bool):
        if self.enabled and checked != bool(self.state.get("overlays")):
            self.overlays_change_requested.emit(checked)

    def _limit_edited(self):
        if not self.enabled:
            return
        value = self.limit_spin.value()
        if value != int(round(float(self.state.get("disk_limit_gb") or 20))):
            self.limit_changed.emit(value)

    def _hint_action(self, name: str):
        if name == "dismiss":
            key = self.hint.key
            self.dismissed_hints.add(key)
            self.hint.clear()
            self.hint_dismissed.emit(key)
        elif name == "switch":
            recommended = self.source_choice.recommended
            if recommended:
                self.source_choice.set_current(recommended)
                self._source_picked(recommended)
        elif name in ("replace", "remove"):
            self.other_set_tiles_requested.emit(name)

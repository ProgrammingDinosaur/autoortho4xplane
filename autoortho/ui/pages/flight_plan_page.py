"""Consolidated SimBrief flight-plan and custom-map workflow."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.theme import repolish
else:
    from ui.theme import repolish


class FlightPlanPage(QWidget):
    def __init__(self, simbrief_widget, map_editor=None, parent=None):
        super().__init__(parent)
        self.map_editor = map_editor

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        scroll.setWidget(content)
        page_layout.addWidget(scroll)
        root = QVBoxLayout(content)
        root.setContentsMargins(14, 12, 14, 12)
        title = QLabel("Flight Plan & Map")
        title.setProperty("textRole", "pageTitle")
        root.addWidget(title)

        self.influence_label = QLabel(
            "Flight-plan guidance is inactive."
        )
        self.influence_label.setProperty("textRole", "secondary")
        root.addWidget(self.influence_label)

        body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._body = body
        left = QVBoxLayout()
        simbrief_widget.setParent(self)
        left.addWidget(simbrief_widget)

        summary = QGroupBox("Loaded Flight")
        summary_form = QFormLayout(summary)
        self.flight_fields = {}
        for key, label in (
            ("origin", "Departure"),
            ("destination", "Arrival"),
            ("flight", "Flight"),
            ("aircraft", "Aircraft"),
            ("cruise", "Cruise altitude"),
            ("duration", "Estimated duration"),
            ("waypoints", "Waypoints"),
        ):
            value = QLabel("—")
            value.setTextInteractionFlags(
                value.textInteractionFlags()
                | Qt.TextInteractionFlag.TextSelectableByMouse
            )
            summary_form.addRow(label, value)
            self.flight_fields[key] = value
        self.route_text = QTextEdit()
        self.route_text.setReadOnly(True)
        self.route_text.setPlaceholderText("No route loaded.")
        self.route_text.setAccessibleName("Flight plan route waypoints")
        summary_form.addRow("Route", self.route_text)
        left.addWidget(summary)
        body.addLayout(left, 2)

        map_group = QGroupBox("Custom Map")
        self.map_group = map_group
        map_layout = QVBoxLayout(map_group)
        map_description = QLabel(self.MAP_TEXT["sources"][1])
        map_description.setWordWrap(True)
        map_description.setProperty("textRole", "secondary")
        self.map_description = map_description
        map_layout.addWidget(map_description)
        if map_editor is not None:
            map_editor.setParent(map_group)
            map_layout.addWidget(map_editor, 1)
            view_changed = getattr(map_editor, "viewChanged", None)
            if view_changed is not None:
                view_changed.connect(self._on_map_view)
        else:
            map_layout.addWidget(QLabel("The custom map is unavailable."))
            map_layout.addStretch()
        map_group.setMinimumHeight(460)
        body.addWidget(map_group, 3)
        root.addLayout(body, 1)

    MAP_TEXT = {
        "sources": (
            "Custom Map",
            "Choose the imagery source for individual one-degree scenery "
            "tiles. Tiles you leave unpainted use the default source. "
            "Takes effect when Map type is set to Custom Map.",
        ),
        "cache": (
            "Scenery && Cache",  # && shows one & in a group box title
            "What AutoOrtho has stored per one-degree tile: scenery and cached "
            "imagery. Select tiles, or a whole region with Regions, to download "
            "their scenery and keep it, or to delete what is stored.",
        ),
    }

    def _on_map_view(self, view):
        title, text = self.MAP_TEXT.get(view, self.MAP_TEXT["sources"])
        self.map_group.setTitle(title)
        self.map_description.setText(text)

    # Below this width the map stacks under the flight details instead of
    # sharing the row, so the map editor never gets squeezed.
    STACK_BELOW_WIDTH = 1000

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = (
            QBoxLayout.Direction.TopToBottom
            if self.width() < self.STACK_BELOW_WIDTH
            else QBoxLayout.Direction.LeftToRight
        )
        if self._body.direction() != direction:
            self._body.setDirection(direction)

    def set_flight_data(self, data):
        origin = data.get("origin", {})
        destination = data.get("destination", {})
        general = data.get("general", {})
        aircraft = data.get("aircraft", {})
        times = data.get("times", {})
        fixes = data.get("navlog", {}).get("fix", [])
        try:
            cruise = f"{int(general.get('initial_altitude', 0)):,} ft"
        except (TypeError, ValueError):
            cruise = str(general.get("initial_altitude") or "—")
        try:
            seconds = int(times.get("est_time_enroute", 0))
            hours, remainder = divmod(seconds, 3600)
            minutes = remainder // 60
            duration = f"{hours}h {minutes:02d}m"
        except (TypeError, ValueError):
            duration = "—"
        values = {
            "origin": origin.get("icao_code", "—"),
            "destination": destination.get("icao_code", "—"),
            "flight": general.get("flight_number", "—"),
            "aircraft": aircraft.get("icaocode", "—"),
            "cruise": cruise,
            "duration": duration,
            "waypoints": str(len(fixes) if isinstance(fixes, list) else 0),
        }
        for key, value in values.items():
            self.flight_fields[key].setText(str(value))
        self.route_text.setPlainText(str(general.get("route") or ""))

    def clear_flight_data(self):
        for label in self.flight_fields.values():
            label.setText("—")
        self.route_text.clear()
        self.set_influence(False)

    def set_influence(self, active):
        self.influence_label.setText(
            "Flight-plan guidance is actively controlling dynamic zoom and "
            "prefetching."
            if active
            else "Flight-plan guidance is inactive."
        )
        self.influence_label.setProperty(
            "textRole",
            "success" if active else "secondary",
        )
        repolish(self.influence_label)

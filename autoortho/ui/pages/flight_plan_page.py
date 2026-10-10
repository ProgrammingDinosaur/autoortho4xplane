"""Consolidated SimBrief flight-plan and custom-map workflow."""

from PySide6.QtCore import QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.theme import announce_accessible, repolish
else:
    from ui.theme import announce_accessible, repolish


class FlightPlanPage(QWidget):
    map_ready_changed = Signal(bool)

    def __init__(self, simbrief_widget, parent=None):
        super().__init__(parent)
        self.map_url = ""
        self.network = QNetworkAccessManager(self)
        self.network.finished.connect(self._map_probe_finished)

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

        body = QHBoxLayout()
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

        map_group = QGroupBox("Custom Map Editor")
        map_layout = QVBoxLayout(map_group)
        self.map_status = QLabel("Map service has not been checked.")
        self.map_status.setAccessibleName("Map service status")
        self.map_status.setWordWrap(True)
        self.map_url_label = QLabel("")
        self.map_url_label.setWordWrap(True)
        self.map_url_label.setTextInteractionFlags(
            self.map_url_label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.open_map_button = QPushButton("&Open Map Editor")
        self.open_map_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_DialogOpenButton
            )
        )
        self.open_map_button.setAccessibleName("Open custom map editor")
        self.open_map_button.setEnabled(False)
        self.open_map_button.clicked.connect(self.open_map)
        copy_url = QPushButton("&Copy URL")
        copy_url.clicked.connect(
            lambda: QApplication.clipboard().setText(self.map_url)
        )
        retry = QPushButton("Check &Again")
        retry.clicked.connect(self.check_map_service)
        map_description = QLabel(
            "Assign imagery sources to individual one-degree scenery "
            "tiles in your browser."
        )
        map_description.setWordWrap(True)
        map_layout.addWidget(map_description)
        map_layout.addWidget(self.map_status)
        map_layout.addWidget(self.map_url_label)
        map_layout.addWidget(self.open_map_button)
        map_layout.addWidget(copy_url)
        map_layout.addWidget(retry)
        map_layout.addStretch()
        body.addWidget(map_group, 1)
        root.addLayout(body, 1)

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

    def set_map_port(self, port):
        self.map_url = f"http://127.0.0.1:{int(port)}/custommap"
        self.map_url_label.setText(self.map_url)

    def check_map_service(self):
        if not self.map_url:
            self.map_status.setText("Map service port is not configured.")
            return
        self.map_status.setText("Checking local map service…")
        self.open_map_button.setEnabled(False)
        self.network.get(QNetworkRequest(QUrl(self.map_url)))

    def _map_probe_finished(self, reply):
        status_code = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        ready = (
            reply.error() == reply.NetworkError.NoError
            and isinstance(status_code, int)
            and 200 <= status_code < 400
        )
        self.open_map_button.setEnabled(ready)
        self.map_status.setText(
            "Map service is ready."
            if ready
            else (
                "Map service is not available. Start streaming, then check "
                "again. The URL can still be copied for troubleshooting."
            )
        )
        announce_accessible(self.map_status, self.map_status.text())
        self.map_ready_changed.emit(ready)
        reply.deleteLater()

    def open_map(self):
        if not self.map_url:
            return
        opened = QDesktopServices.openUrl(QUrl(self.map_url))
        if not opened:
            self.map_status.setText(
                "The browser could not be opened. Copy the URL and open it "
                "manually."
            )

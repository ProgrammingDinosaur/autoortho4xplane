"""Auto-Tuning: what the governor is doing, and why.

A status line (bottleneck, fidelity, speed), a table of the levers it may
move (value, allowed range, source, last change and its reason, pin), a
timeline of the session with each decision marked, and buttons for the mode,
resetting what it learned and exporting a tuning report. Updated once a
second from ``GovernorService.snapshot()`` while streaming.
"""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

COLUMNS = ("Setting", "Value", "Allowed", "Source", "Last change", "")
SERIES = (
    ("fidelity", "Sharp %", QColor("#4caf50"), 1.0),
    ("mbps", "Mbit/s", QColor("#42a5f5"), None),
    ("cpu", "CPU %", QColor("#ffb74d"), 100.0),
    ("ram_gb", "RAM GB", QColor("#ba68c8"), None),
)


class TimelineChart(QWidget):
    """Session lines with a marker per decision; click a marker for its text."""

    marker_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setAccessibleName("Auto-tuning timeline")
        self.points = []
        self._markers = []

    def set_points(self, points):
        self.points = list(points)
        decisions = [p for p in self.points if p.get("decision")]
        self.setAccessibleDescription(
            f"{len(self.points)} samples, {len(decisions)} decisions"
        )
        self.update()

    def _x(self, index, rect):
        count = max(1, len(self.points) - 1)
        return rect.left() + rect.width() * index / count

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(8, 8, -8, -20)
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawRect(rect)
        self._markers = []
        if len(self.points) < 2:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "The timeline fills in while streaming.")
            return
        for key, _label, colour, scale in SERIES:
            values = [p.get(key) for p in self.points]
            known = [v for v in values if v is not None]
            if not known:
                continue
            top = scale or max(1e-6, max(known) * 1.1)
            painter.setPen(QPen(colour, 1.5))
            previous = None
            for index, value in enumerate(values):
                if value is None:
                    previous = None
                    continue
                y = rect.bottom() - rect.height() * min(1.0, value / top)
                point = QPointF(self._x(index, rect), y)
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point
        painter.setPen(QPen(QColor("#ef5350"), 1, Qt.PenStyle.DashLine))
        for index, point in enumerate(self.points):
            if point.get("decision"):
                x = self._x(index, rect)
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                self._markers.append((x, point["decision"]))
        x = rect.left()
        for _key, label, colour, _scale in SERIES:
            painter.setPen(colour)
            painter.drawText(QPointF(x, rect.bottom() + 15), label)
            x += 80

    def mousePressEvent(self, event):  # noqa: N802
        if not self._markers:
            return
        x = event.position().x()
        nearest = min(self._markers, key=lambda marker: abs(marker[0] - x))
        if abs(nearest[0] - x) <= 6:
            self.marker_clicked.emit(nearest[1])


class TuningPage(QWidget):
    mode_changed = Signal(str)
    pin_toggled = Signal(str, bool)       # lever name, pinned
    reset_learned_requested = Signal()
    export_requested = Signal()

    def __init__(self, modes, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.addWidget(QLabel("Mode"))
        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName("Auto-tuning mode")
        for value, label in modes:
            self.mode_combo.addItem(label, value)
        top.addWidget(self.mode_combo, 1)
        self.reset_button = QPushButton("Reset learned settings")
        self.export_button = QPushButton("Export tuning report")
        top.addWidget(self.reset_button)
        top.addWidget(self.export_button)
        layout.addLayout(top)

        self.texture_cap_check = QCheckBox(
            "Allow smaller textures when graphics memory is short"
        )
        self.texture_cap_check.setObjectName("allow_texture_cap")
        self.texture_cap_check.setToolTip(
            "In Auto mode, when the photo textures X-Plane holds near the\n"
            "graphics memory limit, textures above 4096 px (ZL17 on ZL16\n"
            "scenery, ZL19 near airports) are built at 4096 px: a quarter of\n"
            "the memory, with the detail of one zoom level lower. Your zoom\n"
            "target is not changed. Applies to textures opened afterwards."
        )
        layout.addWidget(self.texture_cap_check)

        self.status_label = QLabel("Auto-tuning starts with streaming.")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Auto-tuning status")
        layout.addWidget(self.status_label)
        self.notice_label = QLabel("")
        self.notice_label.setWordWrap(True)
        self.notice_label.setProperty("validationState", "warning")
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAccessibleName("Settings the governor may tune")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(300)
        layout.addWidget(self.table, 1)

        self.chart = TimelineChart()
        layout.addWidget(self.chart)
        self.decision_label = QLabel("")
        self.decision_label.setWordWrap(True)
        self.decision_label.setProperty("textRole", "secondary")
        layout.addWidget(self.decision_label)

        self.chart.marker_clicked.connect(self.decision_label.setText)
        self.mode_combo.currentIndexChanged.connect(
            lambda _i: self.mode_changed.emit(self.mode_combo.currentData() or "")
        )
        self.reset_button.clicked.connect(self.reset_learned_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self._pin_buttons = {}

    def set_mode(self, mode):
        index = self.mode_combo.findData(mode)
        if index >= 0 and index != self.mode_combo.currentIndex():
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(index)
            self.mode_combo.blockSignals(False)

    @staticmethod
    def _source(name, snapshot, learned):
        if name in snapshot.get("pinned", ()):
            return "Pinned"
        value = snapshot["values"].get(name)
        if value != snapshot["baseline"].get(name):
            return "Learned" if name in learned and learned[name] == value else "Governor"
        return "Baseline"

    def show_snapshot(self, snapshot, learned=None):
        """Show the governor's state; ``snapshot`` is None when not running."""
        learned = learned or {}
        if snapshot is None:
            self.status_label.setText(
                "Auto-tuning starts with streaming. The table shows what it "
                "may tune once it runs."
            )
            self.notice_label.setVisible(False)
            return
        fidelity = snapshot.get("fidelity")
        sharp = f"{fidelity * 100:.0f} % sharp at target" if fidelity is not None else "measuring sharpness"
        tts = snapshot.get("time_to_sharp_s")
        tts_text = f" · sharp {tts:.0f} s after loading" if tts else ""
        fps = snapshot.get("fps")
        fps_text = f" · X-Plane {fps:.0f} fps" if fps else ""
        vram = snapshot.get("graphics_memory_bytes") or 0
        budget = snapshot.get("vram_budget_bytes") or 0
        if vram and budget:
            fps_text += f" · textures {vram / 1024 ** 3:.1f} of {budget / 1024 ** 3:.1f} GB"
        elif vram:
            fps_text += f" · textures {vram / 1024 ** 3:.1f} GB"
        self.status_label.setText(
            f"{snapshot['state_text']} · {sharp} · {snapshot['mbps']:.0f} Mbit/s · "
            f"CPU {snapshot['cpu']:.0f} %{fps_text}{tts_text}"
        )
        notice = snapshot.get("notice")
        self.notice_label.setText(notice or "")
        self.notice_label.setVisible(bool(notice))

        decisions = snapshot.get("decisions", [])
        last_by_lever = {}
        for decision in decisions:
            last_by_lever[decision.lever] = decision
        levers = snapshot["levers"]
        self._set_rows(len(levers))
        for row, (name, spec) in enumerate(levers.items()):
            value = snapshot["values"].get(name)
            unit = f" {spec.unit}" if spec.unit else ""
            cells = [
                spec.label,
                f"{value:g}{unit}" if value is not None else "—",
                f"{spec.minimum:g}–{spec.maximum:g}{unit}",
                self._source(name, snapshot, learned),
            ]
            last = last_by_lever.get(name)
            if last is not None:
                tag = f" [{last.outcome}]" if last.outcome else (
                    "" if last.applied else " [suggested]"
                )
                cells.append(f"{last.text()}{tag}")
            else:
                cells.append("")
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{name}\n{text}")
                self.table.setItem(row, column, item)
            pinned = name in snapshot.get("pinned", ())
            button = self._pin_buttons.get(name)
            if button is None:
                button = QPushButton()
                button.clicked.connect(
                    lambda _c=False, n=name: self.pin_toggled.emit(
                        n, n not in self._pinned
                    )
                )
                self._pin_buttons[name] = button
            button.setText("Unpin" if pinned else "Pin")
            button.setToolTip(
                "Let auto-tuning move this again" if pinned
                else "Keep this value; auto-tuning will not move it"
            )
            self.table.setCellWidget(row, 5, button)
        self._pinned = set(snapshot.get("pinned", ()))
        self._fit_rows()
        self.chart.set_points(snapshot.get("timeline", []))

    _pinned = set()

    def _set_rows(self, count):
        self.table.setRowCount(count)
        self._fit_rows()

    def _fit_rows(self):
        # Rows tall enough for the pin buttons (measured on the buttons in the
        # table, which carry the app's style sheet); the table grows to show
        # every lever rather than scrolling inside a scrolling page.
        header = self.table.verticalHeader()
        tallest = max((b.sizeHint().height() for b in self._pin_buttons.values()),
                      default=0)
        if tallest + 4 > header.defaultSectionSize():
            header.setDefaultSectionSize(tallest + 4)
        height = (self.table.horizontalHeader().sizeHint().height()
                  + header.defaultSectionSize() * self.table.rowCount()
                  + 2 * self.table.frameWidth() + 2)
        self.table.setMinimumHeight(max(300, height))

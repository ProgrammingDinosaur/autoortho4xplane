"""What the zoom target costs on this PC, with a traffic light per resource.

Shows the guard results (see ``guards.py``): graphics memory, RAM, disk and
download. A result with a suggested fix gets a button; the window applies the
fix to its own controls, so it is pending until Apply like any other edit.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.widgets.status_badge import StatusBadge
else:
    from ui.widgets.status_badge import StatusBadge

_STATES = {
    "ok": "success",
    "info": "info",
    "warn": "warning",
    "bad": "error",
}


class TargetCostPanel(QFrame):
    fix_requested = Signal(object)
    suggest_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("targetCostPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName("Estimated cost of the zoom target")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self.title_label = QLabel("What this target needs on this PC")
        self.title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title_label)
        self.hardware_label = QLabel("")
        self.hardware_label.setProperty("textRole", "secondary")
        self.hardware_label.setWordWrap(True)
        layout.addWidget(self.hardware_label)

        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        layout.addLayout(self._grid)
        self._rows = {}

        note = QLabel(
            "Estimates from X-Plane's texture paging for a straight flight; "
            "wide screens and turns hold more."
        )
        note.setProperty("textRole", "secondary")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.suggest_button = QPushButton("Suggest a target for this PC")
        self.suggest_button.setToolTip(
            "The sharpest fixed zoom level this PC's graphics memory and "
            "connection carry. It fills in the controls; Apply saves it."
        )
        self.suggest_button.clicked.connect(self.suggest_requested.emit)
        layout.addWidget(self.suggest_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advice_label = QLabel("")
        self.advice_label.setWordWrap(True)
        self.advice_label.setVisible(False)
        layout.addWidget(self.advice_label)
        self.results = ()

    def set_advice(self, text):
        self.advice_label.setText(text)
        self.advice_label.setVisible(bool(text))

    def _row(self, resource):
        row = self._rows.get(resource)
        if row is not None:
            return row
        index = len(self._rows)
        badge = StatusBadge("", "info")
        badge.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        message = QLabel("")
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        button = QPushButton("Apply suggestion")
        button.setVisible(False)
        button.clicked.connect(lambda _checked=False, r=resource: self._fix(r))
        # The message gets the full width; the fix button sits on the grid
        # row under it (a wrapped label nested in another widget gets its
        # height wrong).
        self._grid.addWidget(badge, 2 * index, 0, Qt.AlignmentFlag.AlignTop)
        self._grid.addWidget(message, 2 * index, 1)
        self._grid.addWidget(
            button, 2 * index + 1, 1, Qt.AlignmentFlag.AlignLeft
        )
        row = (badge, message, button)
        self._rows[resource] = row
        return row

    def set_results(self, results, hardware_summary=""):
        self.results = tuple(results)
        self.hardware_label.setText(
            f"This PC: {hardware_summary}" if hardware_summary else ""
        )
        self.hardware_label.setVisible(bool(hardware_summary))
        seen = set()
        for result in self.results:
            badge, message, button = self._row(result.resource)
            badge.set_status(result.title, _STATES.get(result.level.value, "info"))
            text = result.message
            if result.fix is not None:
                text += f" Suggested: {result.fix.label}."
            message.setText(text)
            message.setAccessibleName(f"{result.title}: {text}")
            if result.fix is not None:
                button.setText("Apply suggestion")
                button.setToolTip(result.fix.label)
                button.setAccessibleName(f"Apply suggestion: {result.fix.label}")
                button.setVisible(True)
            else:
                button.setVisible(False)
            for widget in (badge, message):
                widget.setVisible(True)
            seen.add(result.resource)
        for resource, (badge, message, button) in self._rows.items():
            if resource not in seen:
                for widget in (badge, message, button):
                    widget.setVisible(False)

    def result(self, resource):
        return next((r for r in self.results if r.resource == resource), None)

    def _fix(self, resource):
        result = self.result(resource)
        if result is not None and result.fix is not None:
            self.fix_requested.emit(result.fix)

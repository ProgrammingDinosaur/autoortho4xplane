"""Inline transactional editor and preview for dynamic zoom quality steps."""

from PySide6.QtCore import QRectF, Signal, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.models.dynamic_zoom_model import (
        DynamicZoomTableModel,
        SpinBoxDelegate,
    )
    from autoortho.ui.theme import announce_accessible
    from autoortho.utils.dynamic_zoom import (
        BASE_ALTITUDE_FT,
        DynamicZoomManager,
    )
else:
    from ui.models.dynamic_zoom_model import (
        DynamicZoomTableModel,
        SpinBoxDelegate,
    )
    from ui.theme import announce_accessible
    from utils.dynamic_zoom import BASE_ALTITUDE_FT, DynamicZoomManager


PRESETS = {
    "Airliner": [
        (0, 16, 18),
        (10000, 15, 17),
        (25000, 14, 16),
        (40000, 13, 15),
    ],
    "General Aviation": [
        (0, 17, 18),
        (5000, 16, 18),
        (12000, 15, 17),
        (20000, 14, 16),
    ],
    "Low VRAM": [
        (0, 15, 17),
        (8000, 14, 16),
        (20000, 13, 15),
        (35000, 12, 14),
    ],
}


class ZoomPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.steps = []
        self.setMinimumHeight(150)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Dynamic zoom preview graph")
        self.setAccessibleDescription(
            "Chart of normal and airport imagery zoom levels across altitude."
        )

    def set_steps(self, steps):
        self.steps = sorted(steps, key=lambda step: step.altitude_ft)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(38, 12, -12, -28)
        painter.fillRect(self.rect(), QColor("#242424"))
        painter.setPen(QPen(QColor("#555"), 1))
        painter.drawRect(rect)
        if not self.steps:
            painter.setPen(QColor("#aaa"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No steps")
            return

        max_alt = max(10000, max(step.altitude_ft for step in self.steps))

        def point(altitude, zoom):
            x = rect.left() + rect.width() * max(0, altitude) / max_alt
            y = rect.bottom() - rect.height() * (zoom - 12) / 7
            return x, y

        for color, airport in (("#6da4e3", False), ("#f0ad4e", True)):
            painter.setPen(QPen(QColor(color), 2))
            previous = None
            for step in self.steps:
                zoom = (
                    step.zoom_level_airports
                    if airport
                    else step.zoom_level
                )
                current = point(step.altitude_ft, zoom)
                if previous is not None:
                    painter.drawLine(
                        previous[0],
                        previous[1],
                        current[0],
                        previous[1],
                    )
                    painter.drawLine(
                        current[0],
                        previous[1],
                        current[0],
                        current[1],
                    )
                previous = current
            if previous is not None:
                painter.drawLine(
                    previous[0],
                    previous[1],
                    rect.right(),
                    previous[1],
                )
        painter.setPen(QColor("#bbb"))
        painter.drawText(4, 22, "ZL")
        painter.drawText(
            int(rect.right() - 80),
            int(rect.bottom() + 20),
            "Altitude AGL",
        )
        painter.setPen(QColor("#6da4e3"))
        painter.drawText(int(rect.left() + 8), int(rect.top() + 16), "Normal")
        painter.setPen(QColor("#f0ad4e"))
        painter.drawText(
            int(rect.left() + 68),
            int(rect.top() + 16),
            "Airports",
        )


class DynamicZoomEditor(QWidget):
    changed = Signal(object)

    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        self._baseline = (
            manager.clone() if manager is not None else DynamicZoomManager()
        )
        if self._baseline.is_empty():
            self._baseline.set_base_zoom(16, 18)
        self._manager = self._baseline.clone()
        self._history = []
        self._redo = []
        self._restoring = False
        self.setAccessibleName("Dynamic zoom quality step editor")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        info = QLabel(
            "Each row applies from its minimum altitude up to the next row. "
            "Every additional zoom level can use roughly 4× more imagery data."
        )
        info.setWordWrap(True)
        info.setProperty("textRole", "secondary")
        layout.addWidget(info)

        self.model = DynamicZoomTableModel(self._manager, self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName("Dynamic zoom altitude steps")
        self.table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        self.altitude_delegate = SpinBoxDelegate(
            -1000,
            60000,
            1000,
            self.table,
        )
        self.normal_zoom_delegate = SpinBoxDelegate(
            12,
            19,
            1,
            self.table,
        )
        self.airport_zoom_delegate = SpinBoxDelegate(
            12,
            19,
            1,
            self.table,
        )
        self.table.setItemDelegateForColumn(
            DynamicZoomTableModel.MinAltitudeColumn,
            self.altitude_delegate,
        )
        self.table.setItemDelegateForColumn(
            DynamicZoomTableModel.NormalZoomColumn,
            self.normal_zoom_delegate,
        )
        self.table.setItemDelegateForColumn(
            DynamicZoomTableModel.AirportZoomColumn,
            self.airport_zoom_delegate,
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.model.about_to_change.connect(self._remember_snapshot)
        self.model.modelReset.connect(self._model_changed)
        self.model.dataChanged.connect(self._model_changed)
        self.model.validation_changed.connect(
            self._update_validation_message
        )
        layout.addWidget(self.table)

        self.validation_label = QLabel("")
        self.validation_label.setProperty("textRole", "error")
        self.validation_label.hide()
        layout.addWidget(self.validation_label)

        actions = QHBoxLayout()
        add_button = QPushButton("Add Step")
        add_button.clicked.connect(self.add_step)
        remove_button = QPushButton("Remove Selected")
        remove_button.setProperty("role", "destructive")
        remove_button.clicked.connect(self.remove_selected)
        duplicate_button = QPushButton("Duplicate")
        duplicate_button.clicked.connect(self.duplicate_selected)
        self.undo_button = QPushButton("Undo")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QPushButton("Redo")
        self.redo_button.clicked.connect(self.redo)
        reset_button = QPushButton("Reset")
        reset_button.clicked.connect(self.reset)
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addWidget(duplicate_button)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.redo_button)
        actions.addWidget(reset_button)
        actions.addStretch()
        for name in PRESETS:
            button = QPushButton(name)
            button.clicked.connect(
                lambda checked=False, preset=name: self.apply_preset(preset)
            )
            actions.addWidget(button)
        layout.addLayout(actions)

        self.preview = ZoomPreview()
        layout.addWidget(self.preview)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self.redo)
        QShortcut(
            QKeySequence(Qt.Key.Key_Delete),
            self,
            activated=self.remove_selected,
        )
        self.refresh()

    def manager(self):
        return self.model.working_manager()

    def set_manager(self, manager):
        self._baseline = manager.clone()
        self._manager = manager.clone()
        self._history.clear()
        self._redo.clear()
        self._restoring = True
        self.model.set_manager(manager)
        self._restoring = False
        self.refresh()

    def _remember(self):
        self._history.append(self.manager().save_to_config())
        self._history = self._history[-30:]
        self._redo.clear()

    def _remember_snapshot(self, snapshot):
        if self._restoring:
            return
        self._history.append(snapshot)
        self._history = self._history[-30:]
        self._redo.clear()

    def add_step(self):
        steps = self.manager().get_steps()
        altitude = max(step.altitude_ft for step in steps) + 5000
        altitude = min(60000, max(1000, altitude))
        while any(step.altitude_ft == altitude for step in steps):
            altitude += 1000
        if altitude > 60000:
            self.validation_label.setText(
                "No additional altitude threshold is available."
            )
            self.validation_label.show()
            announce_accessible(
                self.validation_label,
                self.validation_label.text(),
            )
            return
        highest = max(steps, key=lambda step: step.altitude_ft)
        if not self.model.append_step(
            altitude,
            max(12, highest.zoom_level - 1),
            max(12, highest.zoom_level_airports - 1),
        ):
            return

    def duplicate_selected(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return
        step = self.model.data(
            self.model.index(indexes[0].row(), 0),
            DynamicZoomTableModel.StepRole,
        )
        if step is None:
            return
        used = {
            row.altitude_ft
            for row in self.model.step_rows()
        }
        altitude = step.altitude_ft + 1000
        while altitude in used and altitude <= 60000:
            altitude += 1000
        if altitude > 60000:
            return
        self.model.append_step(
            altitude,
            step.zoom_level,
            step.zoom_level_airports,
        )

    def remove_selected(self):
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()},
            reverse=True,
        )
        removable = [
            row
            for row in rows
            if self.model.data(
                self.model.index(row, 0),
                DynamicZoomTableModel.AltitudeRole,
            )
            != BASE_ALTITUDE_FT
        ]
        if not removable:
            return
        self._remember()
        self._restoring = True
        for row in removable:
            self.model.remove_row(row)
        self._restoring = False
        self._commit_change()

    def undo(self):
        if not self._history:
            return
        self._redo.append(self.manager().save_to_config())
        manager = DynamicZoomManager()
        manager.load_from_config(self._history.pop())
        self._restoring = True
        self.model.set_manager(manager)
        self._restoring = False
        self._commit_change(remember=False)

    def redo(self):
        if not self._redo:
            return
        self._history.append(self.manager().save_to_config())
        manager = DynamicZoomManager()
        manager.load_from_config(self._redo.pop())
        self._restoring = True
        self.model.set_manager(manager)
        self._restoring = False
        self._commit_change(remember=False)

    def reset(self):
        self._remember()
        self._restoring = True
        self.model.set_manager(self._baseline)
        self._restoring = False
        self._commit_change()

    def apply_preset(self, name):
        values = PRESETS.get(name)
        if values is None:
            return
        self._remember()
        manager = DynamicZoomManager()
        for altitude, zoom, airports in values:
            if altitude == BASE_ALTITUDE_FT:
                manager.set_base_zoom(zoom, airports)
            else:
                manager.add_step(altitude, zoom, airports)
        self._restoring = True
        self.model.set_manager(manager)
        self._restoring = False
        self._commit_change()

    def _remove_altitude(self, altitude):
        if altitude == BASE_ALTITUDE_FT:
            return
        rows = self.model.step_rows()
        row = next(
            (
                index
                for index, step in enumerate(rows)
                if step.altitude_ft == altitude
            ),
            -1,
        )
        if row < 0:
            return
        self.model.remove_row(row)

    def _model_changed(self, *args):
        if self._restoring:
            return
        self._manager = self.manager()
        self._commit_change()

    def _commit_change(self, remember=False):
        self._update_validation_message()
        self.refresh()
        self.changed.emit(self.manager())

    def _update_validation_message(self):
        messages = self.model.validation_messages()
        if messages:
            self.validation_label.setText("\n".join(messages))
            self.validation_label.show()
        else:
            self.validation_label.hide()

    def refresh(self):
        self._manager = self.manager()
        self.preview.set_steps(
            sorted(
                self._manager.get_steps(),
                key=lambda step: step.altitude_ft,
            )
        )
        self.undo_button.setEnabled(bool(self._history))
        self.redo_button.setEnabled(bool(self._redo))

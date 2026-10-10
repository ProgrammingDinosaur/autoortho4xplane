"""Qt model for dynamic zoom quality steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Signal,
    Qt,
)
from PySide6.QtWidgets import QStyledItemDelegate, QSpinBox

if __package__ and __package__.startswith("autoortho."):
    from autoortho.utils.dynamic_zoom import (
        BASE_ALTITUDE_FT,
        DynamicZoomManager,
        MAX_ZOOM_LEVEL,
        MIN_ZOOM_LEVEL,
        QualityStep,
    )
else:
    from utils.dynamic_zoom import (
        BASE_ALTITUDE_FT,
        DynamicZoomManager,
        MAX_ZOOM_LEVEL,
        MIN_ZOOM_LEVEL,
        QualityStep,
    )


@dataclass(slots=True)
class StepRow:
    altitude_ft: int
    zoom_level: int
    zoom_level_airports: int


class DynamicZoomTableModel(QAbstractTableModel):
    about_to_change = Signal(object)
    validation_changed = Signal()
    RangeColumn = 0
    MinAltitudeColumn = 1
    NormalZoomColumn = 2
    AirportZoomColumn = 3

    AltitudeRole = Qt.ItemDataRole.UserRole + 1
    NormalZoomRole = Qt.ItemDataRole.UserRole + 2
    AirportZoomRole = Qt.ItemDataRole.UserRole + 3
    StepRole = Qt.ItemDataRole.UserRole + 4

    def __init__(
        self,
        manager: DynamicZoomManager | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._source_manager = manager
        self._working_manager = manager.clone() if manager is not None else DynamicZoomManager()
        self._validation_messages: list[str] = []

    @classmethod
    def from_manager(cls, manager: DynamicZoomManager) -> "DynamicZoomTableModel":
        return cls(manager)

    def roleNames(self) -> dict[int, bytes]:
        return {
            Qt.ItemDataRole.DisplayRole: b"display",
            self.AltitudeRole: b"altitudeFt",
            self.NormalZoomRole: b"normalZoom",
            self.AirportZoomRole: b"airportZoom",
            self.StepRole: b"step",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._working_manager.get_steps())

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 4

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return {
                self.RangeColumn: "Range",
                self.MinAltitudeColumn: "Min altitude",
                self.NormalZoomColumn: "Normal",
                self.AirportZoomColumn: "Airport",
            }.get(section)
        return section + 1

    def _rows(self) -> list[QualityStep]:
        return sorted(self._working_manager.get_steps(), key=lambda step: step.altitude_ft)

    def _step_for_row(self, row: int) -> QualityStep | None:
        rows = self._rows()
        if 0 <= row < len(rows):
            return rows[row]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        column = index.column()
        step = self._step_for_row(row)
        if step is None:
            return None

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if column == self.RangeColumn:
                rows = self._rows()
                if row + 1 < len(rows):
                    upper = rows[row + 1].altitude_ft - 1
                    return f"{step.altitude_ft:,}–{upper:,} ft"
                return f"{step.altitude_ft:,} ft and above"
            if column == self.MinAltitudeColumn:
                return f"{step.altitude_ft:,} ft"
            if column == self.NormalZoomColumn:
                return f"ZL{step.zoom_level}"
            if column == self.AirportZoomColumn:
                return f"ZL{step.zoom_level_airports}"
        if role == Qt.ItemDataRole.EditRole:
            if column == self.MinAltitudeColumn:
                return step.altitude_ft
            if column == self.NormalZoomColumn:
                return step.zoom_level
            if column == self.AirportZoomColumn:
                return step.zoom_level_airports
        if role == self.AltitudeRole:
            return step.altitude_ft
        if role == self.NormalZoomRole:
            return step.zoom_level
        if role == self.AirportZoomRole:
            return step.zoom_level_airports
        if role == self.StepRole:
            return step
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == self.MinAltitudeColumn and index.row() == 0:
            return flags
        if index.column() in (
            self.MinAltitudeColumn,
            self.NormalZoomColumn,
            self.AirportZoomColumn,
        ):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or role not in (
            Qt.ItemDataRole.EditRole,
            Qt.ItemDataRole.DisplayRole,
        ):
            return False
        step = self._step_for_row(index.row())
        if step is None:
            return False
        candidate = self._working_manager.clone()
        if index.column() == self.MinAltitudeColumn:
            if index.row() == 0:
                return False
            new_altitude = int(value)
            if not candidate.remove_step(step.altitude_ft):
                return False
            if not candidate.add_step(new_altitude, step.zoom_level, step.zoom_level_airports):
                self._validation_messages = [
                    "That altitude threshold already exists."
                ]
                self.validation_changed.emit()
                return False
        elif index.column() == self.NormalZoomColumn:
            if not candidate.update_step(step.altitude_ft, int(value), step.zoom_level_airports):
                return False
        elif index.column() == self.AirportZoomColumn:
            if not candidate.update_step(
                step.altitude_ft,
                step.zoom_level,
                int(value),
            ):
                return False
        else:
            return False

        if any(
            row.zoom_level_airports < row.zoom_level
            for row in candidate.get_steps()
        ):
            self._validation_messages = [
                "Airport zoom cannot be lower than normal zoom."
            ]
            self.validation_changed.emit()
            return False
        self.about_to_change.emit(
            self._working_manager.save_to_config()
        )
        if index.column() == self.MinAltitudeColumn:
            self.beginResetModel()
            self._working_manager = candidate
            self._validation_messages = self._validation_errors(
                candidate
            )
            self.endResetModel()
        else:
            self._working_manager = candidate
            self._validation_messages = self._validation_errors(
                candidate
            )
            self.dataChanged.emit(
                index,
                index,
                [
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.EditRole,
                    self.NormalZoomRole,
                    self.AirportZoomRole,
                    self.StepRole,
                ],
            )
        self.validation_changed.emit()
        return True

    def _validation_errors(self, manager: DynamicZoomManager) -> list[str]:
        warnings = list(manager.validate())
        if manager.is_empty():
            return warnings
        if not manager.has_base_step():
            warnings.append(f"Missing base step at {BASE_ALTITUDE_FT}ft")
        return warnings

    def validation_messages(self) -> list[str]:
        return list(self._validation_messages)

    def is_valid(self) -> bool:
        return not any(
            message.startswith("Missing base step")
            for message in self._validation_errors(self._working_manager)
        )

    def set_manager(self, manager: DynamicZoomManager) -> None:
        self.beginResetModel()
        self._source_manager = manager
        self._working_manager = manager.clone()
        self._validation_messages = self._validation_errors(self._working_manager)
        self.endResetModel()

    def working_manager(self) -> DynamicZoomManager:
        return self._working_manager.clone()

    def to_manager(self) -> DynamicZoomManager:
        return self.working_manager()

    def commit(self) -> bool:
        if not self.is_valid():
            return False
        if self._source_manager is None:
            return True
        self._source_manager.load_from_config(self._working_manager.save_to_config())
        return True

    def revert(self) -> None:
        if self._source_manager is None:
            self.beginResetModel()
            self._working_manager = DynamicZoomManager()
            self._validation_messages = []
            self.endResetModel()
            return
        self.set_manager(self._source_manager)

    def append_step(
        self,
        altitude_ft: int,
        zoom_level: int,
        zoom_level_airports: int,
    ) -> bool:
        candidate = self._working_manager.clone()
        if not candidate.add_step(altitude_ft, zoom_level, zoom_level_airports):
            self._validation_messages = [
                "That altitude threshold already exists."
            ]
            self.validation_changed.emit()
            return False
        if zoom_level_airports < zoom_level:
            self._validation_messages = [
                "Airport zoom cannot be lower than normal zoom."
            ]
            self.validation_changed.emit()
            return False
        self.about_to_change.emit(
            self._working_manager.save_to_config()
        )
        self.beginResetModel()
        self._working_manager = candidate
        self._validation_messages = self._validation_errors(candidate)
        self.endResetModel()
        self.validation_changed.emit()
        return True

    def remove_row(self, row: int) -> bool:
        step = self._step_for_row(row)
        if step is None or step.altitude_ft == BASE_ALTITUDE_FT:
            return False
        candidate = self._working_manager.clone()
        if not candidate.remove_step(step.altitude_ft):
            return False
        self.about_to_change.emit(
            self._working_manager.save_to_config()
        )
        self.beginResetModel()
        self._working_manager = candidate
        self._validation_messages = self._validation_errors(candidate)
        self.endResetModel()
        self.validation_changed.emit()
        return True

    def step_rows(self) -> list[StepRow]:
        return [
            StepRow(step.altitude_ft, step.zoom_level, step.zoom_level_airports)
            for step in self._rows()
        ]


class SpinBoxDelegate(QStyledItemDelegate):
    def __init__(
        self,
        minimum: int = MIN_ZOOM_LEVEL,
        maximum: int = MAX_ZOOM_LEVEL,
        step: int = 1,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.minimum = int(minimum)
        self.maximum = int(maximum)
        self.step = int(step)

    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QSpinBox(parent)
        editor.setRange(self.minimum, self.maximum)
        editor.setSingleStep(self.step)
        return editor

    def setEditorData(self, editor, index):  # noqa: N802
        value = index.data(Qt.ItemDataRole.EditRole)
        if value is None:
            value = index.data(Qt.ItemDataRole.DisplayRole)
        try:
            editor.setValue(int(value))
        except Exception:
            editor.setValue(self.minimum)

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, int(editor.value()), Qt.ItemDataRole.EditRole)

"""Qt models for background task state."""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.task_manager import TaskManager
    from autoortho.ui.task_models import BackgroundTask, TaskState, TaskType
else:
    from ui.task_manager import TaskManager
    from ui.task_models import BackgroundTask, TaskState, TaskType


class TaskListModel(QAbstractListModel):
    TaskIdRole = Qt.ItemDataRole.UserRole + 1
    TaskTypeRole = Qt.ItemDataRole.UserRole + 2
    TitleRole = Qt.ItemDataRole.UserRole + 3
    PackageRole = Qt.ItemDataRole.UserRole + 4
    StateRole = Qt.ItemDataRole.UserRole + 5
    StageRole = Qt.ItemDataRole.UserRole + 6
    ProgressRole = Qt.ItemDataRole.UserRole + 7
    BytesCompletedRole = Qt.ItemDataRole.UserRole + 8
    BytesTotalRole = Qt.ItemDataRole.UserRole + 9
    RateRole = Qt.ItemDataRole.UserRole + 10
    EtaSecondsRole = Qt.ItemDataRole.UserRole + 11
    CancellableRole = Qt.ItemDataRole.UserRole + 12
    StartedAtRole = Qt.ItemDataRole.UserRole + 13
    FinishedAtRole = Qt.ItemDataRole.UserRole + 14
    ErrorRole = Qt.ItemDataRole.UserRole + 15
    RecoveryActionRole = Qt.ItemDataRole.UserRole + 16
    TerminalRole = Qt.ItemDataRole.UserRole + 17
    TaskRole = Qt.ItemDataRole.UserRole + 18

    def __init__(
        self,
        manager: TaskManager | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager or TaskManager(self)
        self._task_ids: list[str] = list(self.manager.tasks.keys())
        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_updated.connect(self._on_task_updated)
        self.manager.task_removed.connect(self._on_task_removed)

    def roleNames(self) -> dict[int, bytes]:
        return {
            Qt.ItemDataRole.DisplayRole: b"display",
            self.TaskIdRole: b"taskId",
            self.TaskTypeRole: b"taskType",
            self.TitleRole: b"title",
            self.PackageRole: b"package",
            self.StateRole: b"state",
            self.StageRole: b"stage",
            self.ProgressRole: b"progress",
            self.BytesCompletedRole: b"bytesCompleted",
            self.BytesTotalRole: b"bytesTotal",
            self.RateRole: b"rate",
            self.EtaSecondsRole: b"etaSeconds",
            self.CancellableRole: b"cancellable",
            self.StartedAtRole: b"startedAt",
            self.FinishedAtRole: b"finishedAt",
            self.ErrorRole: b"error",
            self.RecoveryActionRole: b"recoveryAction",
            self.TerminalRole: b"terminal",
            self.TaskRole: b"task",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._task_ids)

    def _task_for_row(self, row: int) -> BackgroundTask | None:
        if 0 <= row < len(self._task_ids):
            task_id = self._task_ids[row]
            return self.manager.task(task_id)
        return None

    def _row_for_task_id(self, task_id: str) -> int:
        try:
            return self._task_ids.index(task_id)
        except ValueError:
            return -1

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        task = self._task_for_row(index.row())
        if task is None:
            return None

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            stage = task.stage or task.state.value.replace("_", " ").title()
            package = f" — {task.package}" if task.package else ""
            return f"{task.title}{package} — {stage}"
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, 92)
        if role == self.TaskIdRole:
            return task.id
        if role == self.TaskTypeRole:
            return task.type.value
        if role == self.TitleRole:
            return task.title
        if role == self.PackageRole:
            return task.package
        if role == self.StateRole:
            return task.state.value
        if role == self.StageRole:
            return task.stage
        if role == self.ProgressRole:
            return task.progress
        if role == self.BytesCompletedRole:
            return task.bytes_completed
        if role == self.BytesTotalRole:
            return task.bytes_total
        if role == self.RateRole:
            return task.rate
        if role == self.EtaSecondsRole:
            return task.eta_seconds
        if role == self.CancellableRole:
            return task.cancellable
        if role == self.StartedAtRole:
            return task.started_at
        if role == self.FinishedAtRole:
            return task.finished_at
        if role == self.ErrorRole:
            return task.error
        if role == self.RecoveryActionRole:
            return task.recovery_action
        if role == self.TerminalRole:
            return task.state.terminal
        if role == self.TaskRole:
            return task
        return None

    def task(self, row: int) -> BackgroundTask | None:
        return self._task_for_row(row)

    def task_by_id(self, task_id: str) -> BackgroundTask | None:
        return self.manager.task(task_id)

    def tasks(self) -> list[BackgroundTask]:
        tasks: list[BackgroundTask] = []
        for task_id in self._task_ids:
            task = self.manager.task(task_id)
            if task is not None:
                tasks.append(task)
        return tasks

    def insert_task(
        self,
        task_id: str,
        task_type: TaskType | str,
        title: str,
        *,
        package: str = "",
        stage: str = "",
        cancellable: bool = False,
        cancel_callback=None,
        retry_callback=None,
    ) -> BackgroundTask:
        return self.manager.create_task(
            task_id,
            TaskType(task_type),
            title,
            package=package,
            stage=stage,
            cancellable=cancellable,
            cancel_callback=cancel_callback,
            retry_callback=retry_callback,
        )

    def update_task(self, task_id: str, **changes: Any) -> BackgroundTask | None:
        return self.manager.update_task(task_id, **changes)

    def remove_task(self, task_id: str) -> bool:
        return self.manager.dismiss_task(task_id)

    def _on_task_added(self, task: BackgroundTask) -> None:
        row = self._row_for_task_id(task.id)
        if row >= 0:
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, list(self.roleNames().keys()))
            return
        self.beginInsertRows(QModelIndex(), len(self._task_ids), len(self._task_ids))
        self._task_ids.append(task.id)
        self.endInsertRows()

    def _on_task_updated(self, task: BackgroundTask) -> None:
        row = self._row_for_task_id(task.id)
        if row < 0:
            self._on_task_added(task)
            return
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, list(self.roleNames().keys()))

    def _on_task_removed(self, task_id: str) -> None:
        row = self._row_for_task_id(task_id)
        if row < 0:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._task_ids[row]
        self.endRemoveRows()

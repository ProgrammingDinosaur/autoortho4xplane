"""Central task state and a persistent Qt activity panel."""

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QListView,
    QToolButton,
    QVBoxLayout,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.task_models import (
        BackgroundTask,
        TaskState,
        TaskType,
    )
    from autoortho.ui.widgets.task_row import TaskRow
else:
    from ui.task_models import BackgroundTask, TaskState, TaskType
    from ui.widgets.task_row import TaskRow


CancelCallback = Callable[[], None]
RetryCallback = Callable[[], None]


class TaskManager(QObject):
    task_added = Signal(object)
    task_updated = Signal(object)
    task_removed = Signal(str)
    active_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks: OrderedDict[str, BackgroundTask] = OrderedDict()
        self._cancel_callbacks: dict[str, CancelCallback] = {}
        self._retry_callbacks: dict[str, RetryCallback] = {}

    def create_task(
        self,
        task_id: str,
        task_type: TaskType,
        title: str,
        *,
        package: str = "",
        stage: str = "",
        cancellable: bool = False,
        cancel_callback: Optional[CancelCallback] = None,
        retry_callback: Optional[RetryCallback] = None,
    ) -> BackgroundTask:
        existing = self.tasks.get(task_id)
        if existing is not None and not existing.state.terminal:
            return existing

        task = BackgroundTask(
            id=task_id,
            type=TaskType(task_type),
            title=title,
            package=package,
            state=TaskState.RUNNING,
            stage=stage,
            cancellable=bool(cancellable and cancel_callback),
        )
        self.tasks[task_id] = task
        if cancel_callback is not None:
            self._cancel_callbacks[task_id] = cancel_callback
        else:
            self._cancel_callbacks.pop(task_id, None)
        if retry_callback is not None:
            self._retry_callbacks[task_id] = retry_callback
        else:
            self._retry_callbacks.pop(task_id, None)
        self.task_added.emit(task)
        self._emit_active_count()
        return task

    def update_task(self, task_id: str, **changes) -> Optional[BackgroundTask]:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        for key, value in changes.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.update_eta()
        self.task_updated.emit(task)
        return task

    def complete_task(
        self,
        task_id: str,
        *,
        stage: str = "Completed",
    ) -> Optional[BackgroundTask]:
        return self._finish_task(
            task_id,
            TaskState.COMPLETED,
            stage=stage,
        )

    def fail_task(
        self,
        task_id: str,
        error: str,
        *,
        recovery_action: str = "Retry",
    ) -> Optional[BackgroundTask]:
        return self._finish_task(
            task_id,
            TaskState.FAILED,
            stage="Failed",
            error=error,
            recovery_action=(
                recovery_action
                if task_id in self._retry_callbacks
                else ""
            ),
        )

    def cancel_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        callback = self._cancel_callbacks.get(task_id)
        if (
            task is None
            or task.state != TaskState.RUNNING
            or not task.cancellable
            or callback is None
        ):
            return False
        task.state = TaskState.CANCELLING
        task.stage = "Cancelling…"
        task.cancellable = False
        self.task_updated.emit(task)
        callback()
        return True

    def mark_cancelled(self, task_id: str) -> Optional[BackgroundTask]:
        return self._finish_task(
            task_id,
            TaskState.CANCELLED,
            stage="Cancelled",
        )

    def retry_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        callback = self._retry_callbacks.get(task_id)
        if task is None or task.state != TaskState.FAILED or callback is None:
            return False
        callback()
        return True

    def dismiss_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None or not task.state.terminal:
            return False
        del self.tasks[task_id]
        self._cancel_callbacks.pop(task_id, None)
        self._retry_callbacks.pop(task_id, None)
        self.task_removed.emit(task_id)
        return True

    def task(self, task_id: str) -> Optional[BackgroundTask]:
        return self.tasks.get(task_id)

    def active_tasks(self) -> list[BackgroundTask]:
        return [
            task for task in self.tasks.values()
            if not task.state.terminal
        ]

    def _finish_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        stage: str,
        error: str = "",
        recovery_action: str = "",
    ) -> Optional[BackgroundTask]:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task.state = state
        task.stage = stage
        task.error = error
        task.recovery_action = recovery_action
        task.cancellable = False
        task.finished_at = datetime.now(timezone.utc)
        if state == TaskState.COMPLETED and task.progress is not None:
            task.progress = 100.0
        self._cancel_callbacks.pop(task_id, None)
        self.task_updated.emit(task)
        self._emit_active_count()
        return task

    def _emit_active_count(self) -> None:
        self.active_count_changed.emit(len(self.active_tasks()))




class TaskPanel(QGroupBox):
    EXPANDED_HEIGHT = 220
    COLLAPSED_HEIGHT = 62

    def __init__(self, manager: TaskManager, parent=None):
        super().__init__("Activity", parent)
        self.manager = manager
        self.rows: dict[str, TaskRow] = {}
        self._expanded = False
        self._had_active_tasks = False
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 8)
        outer.setSpacing(4)
        header = QHBoxLayout()
        self.summary_label = QLabel()
        self.summary_label.setProperty("textRole", "secondary")
        self.toggle_button = QToolButton()
        self.toggle_button.setProperty("role", "quiet")
        self.toggle_button.setAccessibleName("Show activity details")
        self.toggle_button.clicked.connect(self.toggle_expanded)
        header.addWidget(self.summary_label)
        header.addStretch()
        header.addWidget(self.toggle_button)
        outer.addLayout(header)

        if __package__ and __package__.startswith("autoortho."):
            from autoortho.ui.models.task_model import TaskListModel
        else:
            from ui.models.task_model import TaskListModel
        self._task_model_type = TaskListModel
        self.model = TaskListModel(manager, self)
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setUniformItemSizes(False)
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.view.setVerticalScrollMode(
            QListView.ScrollMode.ScrollPerPixel
        )
        self.view.setAccessibleName("Background task activity")
        outer.addWidget(self.view)

        self.model.rowsInserted.connect(self._sync_rows)
        self.model.rowsRemoved.connect(self._sync_rows)
        self.model.modelReset.connect(self._sync_rows)
        self.model.dataChanged.connect(self._sync_rows)
        self._sync_rows()

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.view.setVisible(self._expanded)
        self.setMaximumHeight(
            self.EXPANDED_HEIGHT
            if self._expanded
            else self.COLLAPSED_HEIGHT
        )
        self.toggle_button.setText(
            "Hide details" if self._expanded else "Show details"
        )
        self.toggle_button.setAccessibleName(
            "Hide activity details"
            if self._expanded
            else "Show activity details"
        )

    def _sync_rows(self, *args) -> None:
        live_ids = set()
        active_count = 0
        failed_count = 0
        for row_index in range(self.model.rowCount()):
            index = self.model.index(row_index, 0)
            task = self.model.data(
                index,
                self._task_model_type.TaskRole,
            )
            if task is None:
                continue
            live_ids.add(task.id)
            if not task.state.terminal:
                active_count += 1
            elif task.state == TaskState.FAILED:
                failed_count += 1
            row = self.rows.get(task.id)
            if row is None:
                row = TaskRow(self.manager, task)
                self.rows[task.id] = row
                self.view.setIndexWidget(index, row)
            else:
                row.update_task(task)
        for task_id in list(self.rows):
            if task_id not in live_ids:
                self.rows.pop(task_id)

        task_count = len(live_ids)
        if active_count:
            self.summary_label.setText(
                f"{active_count} active"
                + (
                    f" · {task_count - active_count} recent"
                    if task_count > active_count
                    else ""
                )
            )
            if not self._had_active_tasks:
                self.set_expanded(True)
        else:
            suffix = (
                f" · {failed_count} failed"
                if failed_count
                else ""
            )
            self.summary_label.setText(
                f"{task_count} recent task"
                f"{'s' if task_count != 1 else ''}{suffix}"
            )
            if self._had_active_tasks or task_count == 0:
                self.set_expanded(False)
        self._had_active_tasks = active_count > 0
        self.setVisible(bool(live_ids))

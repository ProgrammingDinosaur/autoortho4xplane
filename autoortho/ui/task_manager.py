"""Central task state and a persistent Qt activity panel."""

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.task_models import (
        BackgroundTask,
        TaskState,
        TaskType,
    )
else:
    from ui.task_models import BackgroundTask, TaskState, TaskType


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


class TaskRow(QWidget):
    def __init__(self, manager: TaskManager, task: BackgroundTask, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.task_id = task.id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        header = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: bold;")
        self.state_label = QLabel()
        header.addWidget(self.title_label)
        header.addStretch()
        header.addWidget(self.state_label)
        layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        footer = QHBoxLayout()
        self.detail_label = QLabel()
        self.detail_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self.detail_label.setWordWrap(True)
        self.action_button = QPushButton()
        self.action_button.clicked.connect(self._perform_action)
        footer.addWidget(self.detail_label, 1)
        footer.addWidget(self.action_button)
        layout.addLayout(footer)
        self.update_task(task)

    def update_task(self, task: BackgroundTask) -> None:
        title = task.title
        if task.package:
            title += f" — {task.package}"
        self.title_label.setText(title)
        self.state_label.setText(task.state.value.replace("_", " ").title())

        if task.progress is None and not task.state.terminal:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(task.stage or "Working…")
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(
                max(
                    0,
                    min(
                        100,
                        round(
                            task.progress
                            if task.progress is not None
                            else (
                                100
                                if task.state == TaskState.COMPLETED
                                else 0
                            )
                        ),
                    ),
                )
            )
            if task.progress is None and task.state.terminal:
                self.progress_bar.setFormat(task.stage or task.state.value)
            else:
                self.progress_bar.setFormat(
                    f"{task.stage} — %p%" if task.stage else "%p%"
                )

        details = []
        if task.bytes_total > 0:
            details.append(
                f"{task.bytes_completed / (1024 ** 2):.0f}/"
                f"{task.bytes_total / (1024 ** 2):.0f} MB"
            )
        if task.rate > 0:
            details.append(f"{task.rate / (1024 * 1024):.1f} MB/s")
        if task.eta_seconds is not None:
            minutes, seconds = divmod(round(task.eta_seconds), 60)
            details.append(
                f"ETA {minutes}m {seconds:02d}s"
                if minutes
                else f"ETA {seconds}s"
            )
        if task.error:
            details.append(task.error)
        self.detail_label.setText(" • ".join(details))

        if task.state == TaskState.RUNNING and task.cancellable:
            self.action_button.setText("Cancel")
            self.action_button.setProperty("taskAction", "cancel")
            self.action_button.show()
        elif task.state == TaskState.FAILED and task.recovery_action:
            self.action_button.setText(task.recovery_action)
            self.action_button.setProperty("taskAction", "retry")
            self.action_button.show()
        elif task.state.terminal:
            self.action_button.setText("Dismiss")
            self.action_button.setProperty("taskAction", "dismiss")
            self.action_button.show()
        else:
            self.action_button.hide()

    def _perform_action(self) -> None:
        action = self.action_button.property("taskAction")
        if action == "cancel":
            self.manager.cancel_task(self.task_id)
        elif action == "retry":
            self.manager.retry_task(self.task_id)
        elif action == "dismiss":
            self.manager.dismiss_task(self.task_id)


class TaskPanel(QGroupBox):
    def __init__(self, manager: TaskManager, parent=None):
        super().__init__("Activity", parent)
        self.manager = manager
        self.rows: dict[str, TaskRow] = {}
        self.setMaximumHeight(190)
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 8, 6, 6)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content = QWidget()
        self.rows_layout = QVBoxLayout(self.content)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        self.rows_layout.addStretch()
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll)

        manager.task_added.connect(self._add_task)
        manager.task_updated.connect(self._update_task)
        manager.task_removed.connect(self._remove_task)

    def _add_task(self, task: BackgroundTask) -> None:
        old = self.rows.pop(task.id, None)
        if old is not None:
            old.deleteLater()
        row = TaskRow(self.manager, task)
        self.rows[task.id] = row
        self.rows_layout.insertWidget(
            max(0, self.rows_layout.count() - 1),
            row,
        )
        self.setVisible(True)

    def _update_task(self, task: BackgroundTask) -> None:
        row = self.rows.get(task.id)
        if row is None:
            self._add_task(task)
        else:
            row.update_task(task)

    def _remove_task(self, task_id: str) -> None:
        row = self.rows.pop(task_id, None)
        if row is not None:
            row.deleteLater()
        self.setVisible(bool(self.rows))

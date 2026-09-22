"""Reusable background task row."""

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.task_models import BackgroundTask, TaskState
    from autoortho.ui.theme import announce_accessible, repolish
else:
    from ui.task_models import BackgroundTask, TaskState
    from ui.theme import announce_accessible, repolish


class TaskRow(QWidget):
    def __init__(self, manager, task: BackgroundTask, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.task_id = task.id
        self._last_announced_state = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        header = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setProperty("textRole", "sectionTitle")
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
        self.detail_label.setProperty("textRole", "secondary")
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
        self.state_label.setText(
            task.state.value.replace("_", " ").title()
        )

        if task.progress is None and not task.state.terminal:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(task.stage or "Working…")
        else:
            self.progress_bar.setRange(0, 100)
            value = (
                task.progress
                if task.progress is not None
                else 100
                if task.state == TaskState.COMPLETED
                else 0
            )
            self.progress_bar.setValue(
                max(0, min(100, round(value)))
            )
            self.progress_bar.setFormat(
                f"{task.stage} — %p%"
                if task.stage and task.progress is not None
                else task.stage
                if task.progress is None and task.state.terminal
                else "%p%"
            )

        details = []
        if task.bytes_total > 0:
            details.append(
                f"{task.bytes_completed / (1024 ** 2):.0f}/"
                f"{task.bytes_total / (1024 ** 2):.0f} MB"
            )
        if task.rate > 0:
            details.append(f"{task.rate / (1024 ** 2):.1f} MB/s")
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
            action, role, task_action = (
                "Cancel",
                "destructive",
                "cancel",
            )
        elif task.state == TaskState.FAILED and task.recovery_action:
            action, role, task_action = (
                task.recovery_action,
                "primary",
                "retry",
            )
        elif task.state.terminal:
            action, role, task_action = "Dismiss", "quiet", "dismiss"
        else:
            action, role, task_action = "", "", ""

        if action:
            self.action_button.setText(action)
            self.action_button.setProperty(
                "taskAction",
                task_action,
            )
            self.action_button.setProperty("role", role)
            self.action_button.setAccessibleName(
                f"{action} for {task.title}"
            )
            self.action_button.show()
        else:
            self.action_button.hide()
        repolish(self.action_button)

        self.setAccessibleName(f"Background task {task.title}")
        self.setAccessibleDescription(
            f"{task.state.value}. {task.stage}. {task.error}"
        )
        if (
            task.state.terminal
            and task.state != self._last_announced_state
        ):
            announce_accessible(
                self.state_label,
                f"{task.title}: {task.state.value}. {task.error}",
            )
        self._last_announced_state = task.state

    def _perform_action(self) -> None:
        action = self.action_button.property("taskAction")
        if action == "cancel":
            self.manager.cancel_task(self.task_id)
        elif action == "retry":
            self.manager.retry_task(self.task_id)
        elif action == "dismiss":
            self.manager.dismiss_task(self.task_id)

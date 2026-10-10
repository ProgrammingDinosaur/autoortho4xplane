"""Data models for user-visible background tasks."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class TaskType(str, Enum):
    CATALOG = "catalog"
    MOUNT = "mount"
    SCENERY_INSTALL = "scenery_install"
    SCENERY_UNINSTALL = "scenery_uninstall"
    SEASONS = "seasons"
    ROUGHNESS = "roughness"
    RESTORE = "restore"
    CACHE = "cache"
    SIMBRIEF = "simbrief"
    UPDATE = "update"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        )


@dataclass
class BackgroundTask:
    id: str
    type: TaskType
    title: str
    package: str = ""
    state: TaskState = TaskState.PENDING
    stage: str = ""
    progress: Optional[float] = None
    bytes_completed: int = 0
    bytes_total: int = 0
    rate: float = 0.0
    eta_seconds: Optional[float] = None
    cancellable: bool = False
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    finished_at: Optional[datetime] = None
    error: str = ""
    recovery_action: str = ""

    def update_eta(self) -> None:
        if self.rate > 0 and self.bytes_total > self.bytes_completed:
            self.eta_seconds = (
                self.bytes_total - self.bytes_completed
            ) / self.rate
        else:
            self.eta_seconds = None

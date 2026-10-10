"""Shared Qt models for the AutoOrtho UI."""

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.models.dynamic_zoom_model import (
        DynamicZoomTableModel,
        SpinBoxDelegate,
    )
    from autoortho.ui.models.log_model import (
        LogEntry,
        LogFilterProxyModel,
        LogListModel,
    )
    from autoortho.ui.models.scenery_model import (
        SceneryFilterProxyModel,
        SceneryListModel,
    )
    from autoortho.ui.models.task_model import TaskListModel
else:
    from ui.models.dynamic_zoom_model import (
        DynamicZoomTableModel,
        SpinBoxDelegate,
    )
    from ui.models.log_model import LogEntry, LogFilterProxyModel, LogListModel
    from ui.models.scenery_model import (
        SceneryFilterProxyModel,
        SceneryListModel,
    )
    from ui.models.task_model import TaskListModel

__all__ = [
    "DynamicZoomTableModel",
    "LogEntry",
    "LogFilterProxyModel",
    "LogListModel",
    "SceneryFilterProxyModel",
    "SceneryListModel",
    "SpinBoxDelegate",
    "TaskListModel",
]

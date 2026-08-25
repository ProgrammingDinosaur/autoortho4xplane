"""Qt models for structured log entries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSortFilterProxyModel, Qt

import logging


@dataclass(slots=True)
class LogEntry:
    level: int
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    logger_name: str = ""

    @property
    def level_name(self) -> str:
        return str(logging.getLevelName(int(self.level)))

    @property
    def display_text(self) -> str:
        stamp = self.timestamp.astimezone(timezone.utc).strftime("%H:%M:%S")
        logger = f"{self.logger_name}: " if self.logger_name else ""
        return f"{stamp} {self.level_name} {logger}{self.message}"

def _coerce_entry(value: Any) -> LogEntry:
    if isinstance(value, LogEntry):
        return value
    if isinstance(value, logging.LogRecord):
        return LogEntry(
            level=int(value.levelno),
            message=str(value.getMessage()),
            timestamp=datetime.fromtimestamp(value.created, tz=timezone.utc),
            logger_name=str(value.name),
        )
    if isinstance(value, Mapping):
        return LogEntry(
            level=int(value.get("level", value.get("levelno", logging.INFO))),
            message=str(value.get("message", "")),
            timestamp=value.get("timestamp", datetime.now(timezone.utc)),
            logger_name=str(value.get("logger_name", value.get("logger", ""))),
        )
    if isinstance(value, tuple) and len(value) >= 2:
        return LogEntry(level=int(value[0]), message=str(value[1]))
    return LogEntry(level=logging.INFO, message=str(value))


class LogListModel(QAbstractListModel):
    LevelRole = Qt.ItemDataRole.UserRole + 1
    LevelNameRole = Qt.ItemDataRole.UserRole + 2
    MessageRole = Qt.ItemDataRole.UserRole + 3
    TimestampRole = Qt.ItemDataRole.UserRole + 4
    LoggerNameRole = Qt.ItemDataRole.UserRole + 5
    EntryRole = Qt.ItemDataRole.UserRole + 6

    def __init__(self, entries: Iterable[Any] | None = None, *, max_entries: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self.max_entries = max(1, int(max_entries))
        self._entries: list[LogEntry] = []
        if entries is not None:
            self.append_entries(entries)

    def roleNames(self) -> dict[int, bytes]:
        return {
            Qt.ItemDataRole.DisplayRole: b"display",
            self.LevelRole: b"level",
            self.LevelNameRole: b"levelName",
            self.MessageRole: b"message",
            self.TimestampRole: b"timestamp",
            self.LoggerNameRole: b"loggerName",
            self.EntryRole: b"entry",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._entries):
            return None
        entry = self._entries[row]

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return entry.display_text
        if role == self.LevelRole:
            return entry.level
        if role == self.LevelNameRole:
            return entry.level_name
        if role == self.MessageRole:
            return entry.message
        if role == self.TimestampRole:
            return entry.timestamp
        if role == self.LoggerNameRole:
            return entry.logger_name
        if role == self.EntryRole:
            return entry
        return None

    def entries(self) -> list[LogEntry]:
        return list(self._entries)

    def clear(self) -> None:
        if not self._entries:
            return
        self.beginResetModel()
        self._entries.clear()
        self.endResetModel()

    def set_max_entries(self, max_entries: int) -> None:
        self.max_entries = max(1, int(max_entries))
        self._trim_to_limit()

    def append_entry(self, entry: Any) -> bool:
        return self.append_entries([entry]) > 0

    def append_entries(self, entries: Iterable[Any]) -> int:
        new_entries = [_coerce_entry(entry) for entry in entries]
        if not new_entries:
            return 0
        trimmed_entries = new_entries[-self.max_entries :]
        if len(trimmed_entries) == self.max_entries and len(new_entries) >= self.max_entries:
            self.beginResetModel()
            self._entries = list(trimmed_entries)
            self.endResetModel()
            return len(trimmed_entries)

        overflow = max(0, len(self._entries) + len(trimmed_entries) - self.max_entries)
        if overflow:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self._entries[:overflow]
            self.endRemoveRows()
        start = len(self._entries)
        end = start + len(trimmed_entries) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._entries.extend(trimmed_entries)
        self.endInsertRows()
        return len(trimmed_entries)

    def _trim_to_limit(self) -> None:
        overflow = max(0, len(self._entries) - self.max_entries)
        if not overflow:
            return
        self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
        del self._entries[:overflow]
        self.endRemoveRows()


class LogFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._minimum_level = logging.DEBUG
        self._search_text = ""
        self.setDynamicSortFilter(True)

    def set_minimum_level(self, level: int) -> None:
        self._minimum_level = int(level)
        self.invalidateFilter()

    def set_search_text(self, text: str) -> None:
        self._search_text = str(text or "").strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return False
        level = int(model.data(index, LogListModel.LevelRole) or 0)
        if level < self._minimum_level:
            return False
        if self._search_text:
            haystack = " ".join(
                [
                    str(model.data(index, LogListModel.MessageRole) or "").lower(),
                    str(model.data(index, LogListModel.LoggerNameRole) or "").lower(),
                    str(model.data(index, LogListModel.LevelNameRole) or "").lower(),
                ]
            )
            if self._search_text not in haystack:
                return False
        return True

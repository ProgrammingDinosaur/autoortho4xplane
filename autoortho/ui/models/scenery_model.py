"""Qt models for scenery library data."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Protocol

from packaging import version as packaging_version
from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)


class _SceneryLike(Protocol):
    region_id: str
    name: str
    latest_version: str
    installed_version: str
    size_bytes: int
    download_count: int
    install_path: str
    seasons_status: str
    roughness_status: str
    roughness_value: float | None

    @property
    def installed(self) -> bool: ...

    @property
    def update_available(self) -> bool: ...

    @property
    def status(self) -> str: ...


@dataclass(slots=True)
class SceneryRecord:
    region_id: str
    name: str
    latest_version: str
    installed_version: str = ""
    size_bytes: int = 0
    download_count: int = 0
    install_path: str = ""
    seasons_status: str = "Not applied"
    roughness_status: str = "Not applied"
    roughness_value: float | None = None
    busy: bool = False

    @property
    def installed(self) -> bool:
        return bool(self.installed_version)

    @property
    def update_available(self) -> bool:
        if not self.installed or not self.latest_version:
            return False
        try:
            return packaging_version.parse(self.latest_version) > packaging_version.parse(
                self.installed_version
            )
        except Exception:
            return self.latest_version != self.installed_version

    @property
    def status(self) -> str:
        if self.update_available:
            return "Update available"
        if self.installed:
            return "Installed"
        return "Available"

    @property
    def display_text(self) -> str:
        details = self.status
        if self.installed_version and self.latest_version:
            details = (
                f"{details} — installed {self.installed_version}, "
                f"latest {self.latest_version}"
            )
        if self.busy:
            details += " — busy"
        return f"{self.name} ({self.region_id}) — {details}"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _region_id_from_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("region_id", ""))
    return _text(getattr(value, "region_id", ""))


def _record_from_value(value: Any, busy: bool | None = None) -> SceneryRecord:
    if isinstance(value, SceneryRecord):
        return replace(
            value,
            busy=value.busy if busy is None else bool(busy),
        )

    if isinstance(value, Mapping):
        getter = value.get
    else:
        getter = lambda key, default=None: getattr(value, key, default)

    installed_version = _text(getter("installed_version", ""))
    latest_version = _text(getter("latest_version", ""))
    installed = bool(getter("installed", installed_version != ""))
    update_available = bool(getter("update_available", False))
    if not update_available and installed and latest_version and installed_version:
        try:
            update_available = (
                packaging_version.parse(latest_version)
                > packaging_version.parse(installed_version)
            )
        except Exception:
            update_available = latest_version != installed_version

    record = SceneryRecord(
        region_id=_text(getter("region_id", "")),
        name=_text(getter("name", "")),
        latest_version=latest_version,
        installed_version=installed_version if installed else "",
        size_bytes=int(getter("size_bytes", 0) or 0),
        download_count=int(getter("download_count", 0) or 0),
        install_path=_text(getter("install_path", "")),
        seasons_status=_text(getter("seasons_status", "Not applied")) or "Not applied",
        roughness_status=_text(
            getter("roughness_status", "Not applied")
        ) or "Not applied",
        roughness_value=getter("roughness_value", None),
        busy=bool(getter("busy", False) if busy is None else busy),
    )
    if update_available and not record.installed_version:
        record.installed_version = installed_version
    return record


class SceneryListModel(QAbstractListModel):
    RegionIdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    LatestVersionRole = Qt.ItemDataRole.UserRole + 3
    InstalledVersionRole = Qt.ItemDataRole.UserRole + 4
    SizeBytesRole = Qt.ItemDataRole.UserRole + 5
    DownloadCountRole = Qt.ItemDataRole.UserRole + 6
    InstallPathRole = Qt.ItemDataRole.UserRole + 7
    SeasonsStatusRole = Qt.ItemDataRole.UserRole + 8
    RoughnessStatusRole = Qt.ItemDataRole.UserRole + 9
    RoughnessValueRole = Qt.ItemDataRole.UserRole + 10
    InstalledRole = Qt.ItemDataRole.UserRole + 11
    UpdateAvailableRole = Qt.ItemDataRole.UserRole + 12
    StatusRole = Qt.ItemDataRole.UserRole + 13
    BusyRole = Qt.ItemDataRole.UserRole + 14
    RecordRole = Qt.ItemDataRole.UserRole + 15
    ItemRole = RecordRole

    def __init__(
        self,
        items: Iterable[_SceneryLike | Mapping[str, Any]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items: list[SceneryRecord] = []
        self._busy_region_ids: set[str] = set()
        if items is not None:
            self.set_items(items)

    def roleNames(self) -> dict[int, bytes]:
        return {
            Qt.ItemDataRole.DisplayRole: b"display",
            self.RegionIdRole: b"regionId",
            self.NameRole: b"name",
            self.LatestVersionRole: b"latestVersion",
            self.InstalledVersionRole: b"installedVersion",
            self.SizeBytesRole: b"sizeBytes",
            self.DownloadCountRole: b"downloadCount",
            self.InstallPathRole: b"installPath",
            self.SeasonsStatusRole: b"seasonsStatus",
            self.RoughnessStatusRole: b"roughnessStatus",
            self.RoughnessValueRole: b"roughnessValue",
            self.InstalledRole: b"installed",
            self.UpdateAvailableRole: b"updateAvailable",
            self.StatusRole: b"status",
            self.BusyRole: b"busy",
            self.RecordRole: b"record",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._items):
            return None
        item = self._items[row]

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return item.display_text

        if role == self.RegionIdRole:
            return item.region_id
        if role == self.NameRole:
            return item.name
        if role == self.LatestVersionRole:
            return item.latest_version
        if role == self.InstalledVersionRole:
            return item.installed_version
        if role == self.SizeBytesRole:
            return item.size_bytes
        if role == self.DownloadCountRole:
            return item.download_count
        if role == self.InstallPathRole:
            return item.install_path
        if role == self.SeasonsStatusRole:
            return item.seasons_status
        if role == self.RoughnessStatusRole:
            return item.roughness_status
        if role == self.RoughnessValueRole:
            return item.roughness_value
        if role == self.InstalledRole:
            return item.installed
        if role == self.UpdateAvailableRole:
            return item.update_available
        if role == self.StatusRole:
            return item.status
        if role == self.BusyRole:
            return item.busy
        if role == self.RecordRole:
            return item
        return None

    def set_items(
        self,
        items: Iterable[_SceneryLike | Mapping[str, Any]],
        *,
        busy_region_ids: Iterable[str] | None = None,
    ) -> None:
        self.beginResetModel()
        self._busy_region_ids = {str(region_id) for region_id in (busy_region_ids or ())}
        self._items = [
            _record_from_value(item, busy=_region_id_from_value(item) in self._busy_region_ids)
            for item in items
        ]
        for record in self._items:
            record.busy = record.busy or record.region_id in self._busy_region_ids
        self.endResetModel()

    def set_busy(self, region_id: str, busy: bool = True) -> bool:
        region = str(region_id)
        changed = False
        if busy:
            changed = region not in self._busy_region_ids
            self._busy_region_ids.add(region)
        else:
            changed = region in self._busy_region_ids
            self._busy_region_ids.discard(region)
        if not changed:
            return False
        for row, item in enumerate(self._items):
            if item.region_id == region:
                item.busy = busy
                index = self.index(row, 0)
                self.dataChanged.emit(
                    index,
                    index,
                    [self.BusyRole, Qt.ItemDataRole.DisplayRole],
                )
                return True
        return False

    def set_busy_packages(self, region_ids: Iterable[str]) -> None:
        busy_ids = {str(region_id) for region_id in region_ids}
        if busy_ids == self._busy_region_ids:
            return
        self._busy_region_ids = busy_ids
        changed_rows = []
        for row, item in enumerate(self._items):
            busy = item.region_id in busy_ids
            if item.busy != busy:
                item.busy = busy
                changed_rows.append(row)
        if changed_rows:
            self.dataChanged.emit(
                self.index(min(changed_rows), 0),
                self.index(max(changed_rows), 0),
                [self.BusyRole, Qt.ItemDataRole.DisplayRole],
            )

    def update(
        self,
        item_or_region_id: _SceneryLike | Mapping[str, Any] | str,
        *,
        busy: bool | None = None,
    ) -> bool:
        region_id = (
            str(item_or_region_id)
            if isinstance(item_or_region_id, str)
            else _region_id_from_value(item_or_region_id)
        )
        for row, current in enumerate(self._items):
            if current.region_id != region_id:
                continue
            if isinstance(item_or_region_id, str):
                updated = current
            else:
                updated = _record_from_value(
                    item_or_region_id,
                    busy=current.busy if busy is None else busy,
                )
            if busy is not None:
                updated.busy = busy
            elif region_id in self._busy_region_ids:
                updated.busy = True
            self._items[row] = updated
            index = self.index(row, 0)
            self.dataChanged.emit(
                index,
                index,
                list(self.roleNames().keys()),
            )
            return True
        return False

    def item(self, row: int) -> SceneryRecord | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def items(self) -> list[SceneryRecord]:
        return list(self._items)


class SceneryFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._search_text = ""
        self._status_filter = ""
        self._region_filter = ""
        self._sort_mode = "name"
        self.setDynamicSortFilter(True)

    def set_search_text(self, text: str) -> None:
        self._search_text = str(text or "").strip().lower()
        self.invalidateFilter()

    def set_status_filter(self, status: str) -> None:
        self._status_filter = str(status or "").strip().lower()
        self.invalidateFilter()

    def set_region_filter(self, region: str) -> None:
        self._region_filter = str(region or "").strip().lower()
        self.invalidateFilter()

    def set_sort_mode(self, mode: str) -> None:
        self._sort_mode = str(mode or "name").strip().lower() or "name"
        order = (
            Qt.SortOrder.DescendingOrder
            if self._sort_mode in {"size", "downloads"}
            else Qt.SortOrder.AscendingOrder
        )
        self.sort(0, order)

    def sort_mode(self) -> str:
        return self._sort_mode

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return False

        region_id = str(model.data(index, SceneryListModel.RegionIdRole) or "").lower()
        name = str(model.data(index, SceneryListModel.NameRole) or "").lower()
        status = str(model.data(index, SceneryListModel.StatusRole) or "").lower()

        if self._search_text:
            search_space = " ".join(
                [
                    region_id,
                    name,
                    str(model.data(index, SceneryListModel.LatestVersionRole) or "").lower(),
                    str(model.data(index, SceneryListModel.InstalledVersionRole) or "").lower(),
                    status,
                ]
            )
            if self._search_text not in search_space:
                return False

        if self._status_filter not in {"", "all"}:
            installed = bool(
                model.data(index, SceneryListModel.InstalledRole)
            )
            update = bool(
                model.data(index, SceneryListModel.UpdateAvailableRole)
            )
            if self._status_filter == "installed" and not installed:
                return False
            if self._status_filter == "updates" and not update:
                return False
            if self._status_filter == "available" and installed:
                return False

        if self._region_filter and self._region_filter != region_id:
            return False

        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return super().lessThan(left, right)

        def version_key(value: Any) -> tuple[int, str]:
            text = str(value or "")
            if not text:
                return (0, "")
            try:
                parsed = packaging_version.parse(text)
                return (1, str(parsed))
            except Exception:
                return (2, text.lower())

        def sort_key(index: QModelIndex) -> tuple[Any, ...]:
            if self._sort_mode == "recommended":
                status = str(
                    model.data(index, SceneryListModel.StatusRole) or ""
                ).lower()
                order = {
                    "update available": 0,
                    "installed": 1,
                    "available": 2,
                }
                return (
                    order.get(status, 99),
                    str(
                        model.data(index, SceneryListModel.NameRole) or ""
                    ).lower(),
                )
            if self._sort_mode == "status":
                order = {"update available": 0, "installed": 1, "available": 2}
                status = str(model.data(index, SceneryListModel.StatusRole) or "").lower()
                return (order.get(status, 99), str(model.data(index, SceneryListModel.NameRole) or "").lower())
            if self._sort_mode == "region":
                return (str(model.data(index, SceneryListModel.RegionIdRole) or "").lower(),)
            if self._sort_mode == "version":
                return version_key(model.data(index, SceneryListModel.LatestVersionRole))
            if self._sort_mode == "size":
                return (int(model.data(index, SceneryListModel.SizeBytesRole) or 0),)
            if self._sort_mode == "downloads":
                return (int(model.data(index, SceneryListModel.DownloadCountRole) or 0),)
            return (str(model.data(index, SceneryListModel.NameRole) or "").lower(),)

        return sort_key(left) < sort_key(right)

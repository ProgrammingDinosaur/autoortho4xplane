"""Searchable scenery library page."""

from dataclasses import dataclass
from typing import Iterable

from packaging import version
from PySide6.QtCore import QEvent, QSize, Signal, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.models.scenery_model import (
        SceneryFilterProxyModel,
        SceneryListModel,
    )
    from autoortho.ui.readiness import format_bytes
    from autoortho.ui.task_models import TaskState
    from autoortho.ui.theme import THEME, repolish
else:
    from ui.models.scenery_model import (
        SceneryFilterProxyModel,
        SceneryListModel,
    )
    from ui.readiness import format_bytes
    from ui.task_models import TaskState
    from ui.theme import THEME, repolish


@dataclass(frozen=True)
class SceneryItem:
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

    @property
    def installed(self) -> bool:
        return bool(self.installed_version)

    @property
    def update_available(self) -> bool:
        if not self.installed or not self.latest_version:
            return False
        try:
            return version.parse(self.latest_version) > version.parse(
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


def _status_text(value) -> str:
    raw = value.value if hasattr(value, "value") else str(value or "")
    return {
        "applied": "Applied",
        "partially_applied": "Partial",
        "not_applied": "Not applied",
    }.get(raw.lower(), raw.replace("_", " ").title() or "Not applied")


def item_from_region(region) -> SceneryItem | None:
    try:
        latest = region.get_latest_release()
        latest.parse()
    except Exception:
        latest = getattr(region, "local_rel", None)
    if latest is None:
        return None
    local = getattr(region, "local_rel", None)
    patch_source = local or latest
    return SceneryItem(
        region_id=str(region.region_id),
        name=str(getattr(latest, "name", region.region_id)),
        latest_version=str(getattr(latest, "ver", "")),
        installed_version=str(getattr(local, "ver", "")) if local else "",
        size_bytes=int(getattr(latest, "totalsize", 0) or 0),
        download_count=int(getattr(latest, "download_count", 0) or 0),
        install_path=str(getattr(local, "subfolder_dir", "")) if local else "",
        seasons_status=_status_text(
            getattr(patch_source, "seasons_apply_status", None)
        ),
        roughness_status=_status_text(
            getattr(patch_source, "roughness_apply_status", None)
        ),
        roughness_value=getattr(patch_source, "roughness_value", None),
    )


class SceneryCard(QFrame):
    install_requested = Signal(str)
    uninstall_requested = Signal(str)
    options_requested = Signal(str)
    selected = Signal(str)

    def __init__(self, item: SceneryItem, busy=False, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("SceneryCard")
        self.setAccessibleName(f"Scenery package {item.name}")
        self.setAccessibleDescription(
            f"{item.status}. Latest version {item.latest_version}. "
            f"Download size {format_bytes(item.size_bytes)}."
        )
        self.setProperty(
            "state",
            "warning"
            if item.update_available
            else "success"
            if item.installed
            else "info",
        )
        repolish(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)

        header = QHBoxLayout()
        title = QLabel(item.name)
        title.setProperty("textRole", "sectionTitle")
        status = QLabel(item.status)
        status.setProperty(
            "textRole",
            "warning"
            if item.update_available
            else "success"
            if item.installed
            else "secondary",
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(status)
        layout.addLayout(header)

        version_text = (
            f"Installed {item.installed_version} • Latest {item.latest_version}"
            if item.installed
            else f"Version {item.latest_version}"
        )
        metadata = QLabel(
            f"{version_text} • {format_bytes(item.size_bytes)} • "
            f"{item.download_count:,} downloads"
        )
        metadata.setProperty("textRole", "secondary")
        layout.addWidget(metadata)

        patches = QLabel(
            f"Seasons: {item.seasons_status} • "
            f"Terrain reflectivity: {item.roughness_status}"
        )
        patches.setProperty("textRole", "secondary")
        layout.addWidget(patches)

        actions = QHBoxLayout()
        details = QPushButton("Details")
        details.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileDialogDetailedView
            )
        )
        details.setAccessibleName(f"View details for {item.name}")
        details.clicked.connect(
            lambda: self.selected.emit(self.item.region_id)
        )
        actions.addWidget(details)
        actions.addStretch()
        if item.installed:
            if not item.update_available:
                options = QPushButton("Scenery Options")
                options.setAccessibleName(
                    f"Open scenery options for {item.name}"
                )
                options.clicked.connect(
                    lambda: self.options_requested.emit(
                        self.item.region_id
                    )
                )
                actions.addWidget(options)
            uninstall = QPushButton("Uninstall")
            uninstall.setIcon(
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_TrashIcon
                )
            )
            uninstall.setProperty("role", "destructive")
            uninstall.setAccessibleName(f"Uninstall {item.name}")
            uninstall.clicked.connect(
                lambda: self.uninstall_requested.emit(self.item.region_id)
            )
            actions.addWidget(uninstall)
        if not item.installed or item.update_available:
            install = QPushButton(
                "Update" if item.update_available else "Install"
            )
            install.setIcon(
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_DialogApplyButton
                )
            )
            install.setAccessibleName(
                f"{'Update' if item.update_available else 'Install'} "
                f"{item.name}"
            )
            install.setProperty("primary", True)
            install.setProperty("role", "primary")
            install.clicked.connect(
                lambda: self.install_requested.emit(self.item.region_id)
            )
            actions.addWidget(install)
        for index in range(actions.count()):
            widget = actions.itemAt(index).widget()
            if widget is not None and widget is not details:
                widget.setEnabled(not busy)
                if busy:
                    widget.setToolTip(
                        "An operation for this region is already running."
                    )
        layout.addLayout(actions)


class SceneryItemDelegate(QStyledItemDelegate):
    primary_requested = Signal(str)

    def sizeHint(self, option, index):
        return QSize(max(420, option.rect.width()), 72)

    @staticmethod
    def _action_rect(rect):
        return rect.adjusted(rect.width() - 130, 20, -12, -20)

    def paint(self, painter, option, index):
        item = index.data(SceneryListModel.ItemRole)
        if item is None:
            return
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(QPen(QColor(THEME.colors.divider)))
        painter.setBrush(
            QColor(
                THEME.colors.elevated
                if selected
                else THEME.colors.surface
            )
        )
        painter.drawRoundedRect(
            option.rect.adjusted(1, 1, -1, -1),
            THEME.radius.sm,
            THEME.radius.sm,
        )

        text_rect = option.rect.adjusted(12, 8, -145, -8)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(THEME.colors.text_primary))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            item.name,
        )
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(THEME.colors.text_secondary))
        painter.drawText(
            text_rect.adjusted(0, 24, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            f"{item.status}  •  {format_bytes(item.size_bytes)}  •  "
            f"Version {item.latest_version}",
        )

        busy = bool(index.data(SceneryListModel.BusyRole))
        action = (
            "Update"
            if item.update_available
            else "Options"
            if item.installed
            else "Install"
        )
        action_rect = self._action_rect(option.rect)
        painter.setPen(QPen(QColor(THEME.colors.border)))
        painter.setBrush(
            QColor(
                THEME.colors.divider
                if busy
                else THEME.colors.primary
            )
        )
        painter.drawRoundedRect(
            action_rect,
            THEME.radius.sm,
            THEME.radius.sm,
        )
        painter.setPen(QColor(THEME.colors.text_primary))
        painter.drawText(
            action_rect,
            Qt.AlignmentFlag.AlignCenter,
            "Working…" if busy else action,
        )
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self._action_rect(option.rect).contains(
                event.position().toPoint()
            )
            and not index.data(SceneryListModel.BusyRole)
        ):
            item = index.data(SceneryListModel.ItemRole)
            if item is not None:
                self.primary_requested.emit(item.region_id)
                return True
        return super().editorEvent(event, model, option, index)


class SceneryLibraryPage(QWidget):
    install_requested = Signal(str)
    uninstall_requested = Signal(str)
    options_requested = Signal(str)
    settings_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[SceneryItem] = []
        self.tasks = []
        self.runtime_locked = False
        self.model = SceneryListModel(parent=self)
        self.proxy_model = SceneryFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.model)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        title_row = QHBoxLayout()
        title = QLabel("Scenery Library")
        title.setProperty("textRole", "pageTitle")
        settings = QPushButton("Download Settings…")
        settings.clicked.connect(self.settings_requested)
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(settings)
        root.addLayout(title_row)

        toolbar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search scenery…")
        self.search_edit.setAccessibleName("Search scenery packages")
        self.status_filter = QComboBox()
        self.status_filter.addItems(
            ["All", "Installed", "Updates", "Available"]
        )
        self.region_filter = QComboBox()
        self.region_filter.addItem("All regions", "")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(
            ["Recommended", "Name", "Size", "Status"]
        )
        status_label = QLabel("S&tatus")
        status_label.setBuddy(self.status_filter)
        region_label = QLabel("&Region")
        region_label.setBuddy(self.region_filter)
        sort_label = QLabel("S&ort")
        sort_label.setBuddy(self.sort_combo)
        toolbar.addWidget(self.search_edit, 1)
        toolbar.addWidget(status_label)
        toolbar.addWidget(self.status_filter)
        toolbar.addWidget(region_label)
        toolbar.addWidget(self.region_filter)
        toolbar.addWidget(sort_label)
        toolbar.addWidget(self.sort_combo)
        root.addLayout(toolbar)

        body = QHBoxLayout()
        self.list_view = QListView()
        self.list_view.setModel(self.proxy_model)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSelectionMode(
            self.list_view.SelectionMode.SingleSelection
        )
        self.list_view.setAccessibleName("Scenery packages")
        self.item_delegate = SceneryItemDelegate(self.list_view)
        self.list_view.setItemDelegate(self.item_delegate)
        self.item_delegate.primary_requested.connect(
            self._request_primary_action
        )
        self.list_view.selectionModel().currentChanged.connect(
            self._show_selected_item
        )
        self.list_view.activated.connect(
            lambda index: self._request_primary_action(
                str(
                    index.data(SceneryListModel.RegionIdRole)
                    or ""
                )
            )
        )
        body.addWidget(self.list_view, 3)

        self.details_group = QGroupBox("Details")
        details_layout = QVBoxLayout(self.details_group)
        self.details_label = QLabel(
            "Select a scenery package to view installation details and "
            "recent activity."
        )
        self.details_label.setWordWrap(True)
        details_layout.addWidget(self.details_label)
        detail_actions = QHBoxLayout()
        self.options_button = QPushButton("Scenery Options")
        self.uninstall_button = QPushButton("Uninstall")
        self.uninstall_button.setProperty("role", "destructive")
        self.options_button.hide()
        self.uninstall_button.hide()
        detail_actions.addWidget(self.options_button)
        detail_actions.addWidget(self.uninstall_button)
        details_layout.addLayout(detail_actions)
        details_layout.addStretch()
        body.addWidget(self.details_group, 1)
        root.addLayout(body, 1)

        self.empty_label = QLabel("")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        root.addWidget(self.empty_label)

        self.search_edit.textChanged.connect(self.refresh)
        self.status_filter.currentTextChanged.connect(self.refresh)
        self.region_filter.currentIndexChanged.connect(self.refresh)
        self.sort_combo.currentTextChanged.connect(self.refresh)
        self.options_button.clicked.connect(
            lambda: self.options_requested.emit(
                str(self.options_button.property("regionId") or "")
            )
        )
        self.uninstall_button.clicked.connect(
            lambda: self.uninstall_requested.emit(
                str(self.uninstall_button.property("regionId") or "")
            )
        )

    def set_regions(self, regions: Iterable, tasks=()) -> None:
        self.items = [
            item for item in (item_from_region(region) for region in regions)
            if item is not None
        ]
        self.tasks = list(tasks)
        self.model.set_items(self.items)
        selected_region = self.region_filter.currentData()
        self.region_filter.blockSignals(True)
        self.region_filter.clear()
        self.region_filter.addItem("All regions", "")
        for item in sorted(self.items, key=lambda entry: entry.name.lower()):
            self.region_filter.addItem(item.name, item.region_id)
        index = self.region_filter.findData(selected_region)
        self.region_filter.setCurrentIndex(max(0, index))
        self.region_filter.blockSignals(False)
        self.refresh()

    def set_runtime_locked(self, locked: bool) -> None:
        locked = bool(locked)
        if locked == self.runtime_locked:
            return
        self.runtime_locked = locked
        self.refresh()

    def refresh(self) -> None:
        query = self.search_edit.text().strip().lower()
        status_filter = self.status_filter.currentText()
        region_filter = self.region_filter.currentData()
        sort_mode = self.sort_combo.currentText()
        active_packages = {
            task.package.lower()
            for task in self.tasks
            if not task.state.terminal and task.package
        }
        active_ids = {
            task.id.split(":")[-1].lower()
            for task in self.tasks
            if not task.state.terminal
        }
        busy_ids = {
            item.region_id
            for item in self.items
            if (
                self.runtime_locked
                or item.region_id.lower() in active_ids
                or item.name.lower() in active_packages
            )
        }
        self.model.set_busy_packages(busy_ids)
        self.proxy_model.set_search_text(query)
        self.proxy_model.set_status_filter(status_filter)
        self.proxy_model.set_region_filter(region_filter)
        self.proxy_model.set_sort_mode(sort_mode)

        if not self.items:
            self.empty_label.setText(
                "No scenery catalog is available. Check the network, the "
                "temporary download folder, and try refreshing."
            )
        elif self.proxy_model.rowCount() == 0:
            self.empty_label.setText(
                "No scenery packages match the current search and filter."
            )
        else:
            self.empty_label.clear()

    def _request_primary_action(self, region_id: str) -> None:
        item = next(
            (entry for entry in self.items if entry.region_id == region_id),
            None,
        )
        if item is None:
            return
        if item.installed and not item.update_available:
            self.options_requested.emit(region_id)
        else:
            self.install_requested.emit(region_id)

    def _show_selected_item(self, current, previous) -> None:
        item = current.data(SceneryListModel.ItemRole)
        if item is not None:
            self.show_details(item.region_id)

    def show_details(self, region_id: str) -> None:
        item = next(
            (item for item in self.items if item.region_id == region_id),
            None,
        )
        if item is None:
            return
        task_lines = [
            f"• {task.title}: {task.state.value}"
            for task in self.tasks
            if task.package in (item.name, item.region_id)
            or task.id.endswith(f":{item.region_id}")
        ]
        roughness = item.roughness_status
        if item.roughness_value is not None:
            roughness += f" ({item.roughness_value:.1f})"
        self.details_label.setText(
            f"<b>{item.name}</b><br>"
            f"Region ID: {item.region_id}<br>"
            f"Status: {item.status}<br>"
            f"Installed: {item.installed_version or 'No'}<br>"
            f"Latest: {item.latest_version}<br>"
            f"Download size: {format_bytes(item.size_bytes)}<br>"
            f"Installed location: {item.install_path or 'Not installed'}<br>"
            f"Seasons: {item.seasons_status}<br>"
            f"Terrain reflectivity: {roughness}<br><br>"
            f"<b>Recent activity</b><br>"
            + ("<br>".join(task_lines) if task_lines else "No recent tasks.")
        )
        self.options_button.setProperty("regionId", item.region_id)
        self.uninstall_button.setProperty("regionId", item.region_id)
        self.options_button.setVisible(item.installed)
        self.uninstall_button.setVisible(item.installed)

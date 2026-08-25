"""Application shell window for the AutoOrtho UI."""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.navigation import NavigationRail
    from autoortho.ui.pages.home_page import HomePage
    from autoortho.ui.runtime_state import RuntimeState
    from autoortho.ui.task_manager import TaskManager, TaskPanel
    from autoortho.version import __version__
    from autoortho.ui.theme import repolish
else:
    from ui.navigation import NavigationRail
    from ui.pages.home_page import HomePage
    from ui.runtime_state import RuntimeState
    from ui.task_manager import TaskManager, TaskPanel
    from version import __version__
    from ui.theme import repolish


class _ChipLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("chipLabel")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(False)
        self.setMinimumHeight(22)


class _PlaceholderPage(QWidget):
    def __init__(self, title: str, description: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName(f"{title.lower().replace(' ', '')}Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        title_label = QLabel(title)
        title_label.setProperty("textRole", "pageTitle")
        layout.addWidget(title_label)
        if description:
            detail_label = QLabel(description)
            detail_label.setWordWrap(True)
            detail_label.setProperty("textRole", "secondary")
            layout.addWidget(detail_label)
        layout.addStretch(2)


class CompactHeader(QFrame):
    """Compact header with status chips and shell actions."""

    startRequested = Signal()
    stopRequested = Signal()
    setupWizardRequested = Signal()
    docsRequested = Signal()
    aboutRequested = Signal()
    quitRequested = Signal()
    updateRequested = Signal()
    updateRemindRequested = Signal()
    updateDismissRequested = Signal()
    updateCheckRequested = Signal()

    def __init__(
        self,
        app_name: str = "AutoOrtho",
        version_text: str = __version__,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("compactHeader")
        self.setAccessibleName("Application status and controls")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)

        self._runtime_state = RuntimeState.STOPPED
        self._xplane_connected = False
        self._task_count = 0
        self._update_version_text = ""
        self.update_available = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 7)
        outer.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        outer.addLayout(top_row)

        self.app_icon_label = QLabel("AO")
        self.app_icon_label.setObjectName("appIconLabel")
        self.app_icon_label.setAccessibleName("AutoOrtho application icon")
        self.app_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.app_icon_label.setFixedSize(34, 34)
        top_row.addWidget(self.app_icon_label)

        self.brand_image_label = QLabel()
        self.brand_image_label.setObjectName("brandImageLabel")
        self.brand_image_label.setAccessibleName(
            "AutoOrtho mountain scenery"
        )
        self.brand_image_label.setFixedSize(146, 32)
        self.brand_image_label.hide()
        top_row.addWidget(self.brand_image_label)

        title_column = QVBoxLayout()
        title_column.setSpacing(0)
        self.app_name_label = QLabel(app_name)
        self.app_name_label.setProperty("textRole", "sectionTitle")
        self.app_version_label = QLabel(version_text)
        self.app_version_label.setProperty("textRole", "caption")
        title_column.addWidget(self.app_name_label)
        title_column.addWidget(self.app_version_label)
        top_row.addLayout(title_column)
        top_row.addStretch(1)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)

        self.runtime_chip = _ChipLabel("■ Stopped")
        self.runtime_chip.setAccessibleName("Streaming status")
        self.xplane_chip = _ChipLabel("○ X-Plane disconnected")
        self.xplane_chip.setAccessibleName("X-Plane connection status")
        self.task_chip = _ChipLabel("○ No active tasks")
        self.task_chip.setAccessibleName("Active background tasks")
        chip_row.addWidget(self.runtime_chip)
        chip_row.addWidget(self.xplane_chip)
        chip_row.addWidget(self.task_chip)

        self.start_stop_button = QPushButton("Start Streaming")
        self.start_stop_button.setObjectName("startStopButton")
        self.start_stop_button.setAccessibleName("Start scenery streaming")
        self.start_stop_button.clicked.connect(self._on_start_stop_clicked)
        chip_row.addWidget(self.start_stop_button)

        self.overflow_button = QToolButton()
        self.overflow_button.setObjectName("overflowMenuButton")
        self.overflow_button.setText("⋮")
        self.overflow_button.setAccessibleName("More options")
        self.overflow_button.setToolTip("More options")
        self.overflow_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.overflow_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.overflow_button.setAutoRaise(True)
        self.overflow_menu = QMenu(self)
        self.setup_wizard_action = QAction("Setup Wizard", self)
        self.docs_action = QAction("Docs", self)
        self.about_action = QAction("About", self)
        self.check_updates_action = QAction("Check for Updates", self)
        self.quit_action = QAction("Quit", self)
        for action, signal in (
            (self.setup_wizard_action, self.setupWizardRequested),
            (self.docs_action, self.docsRequested),
            (self.check_updates_action, self.updateCheckRequested),
            (self.about_action, self.aboutRequested),
            (self.quit_action, self.quitRequested),
        ):
            action.triggered.connect(signal.emit)
            self.overflow_menu.addAction(action)
        self.overflow_button.setMenu(self.overflow_menu)
        chip_row.addWidget(self.overflow_button)
        top_row.addLayout(chip_row)

        self.update_banner = QFrame()
        self.update_banner.setObjectName("updateBanner")
        self.update_banner.setAccessibleName("Software update notification")
        self.update_banner.setVisible(False)
        self.update_banner.setFrameShape(QFrame.Shape.StyledPanel)
        banner_layout = QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(10, 8, 10, 8)
        banner_layout.setSpacing(8)
        self.update_banner_label = QLabel("")
        self.update_banner_label.setWordWrap(True)
        self.update_banner_button = QPushButton("View release")
        self.update_banner_button.setProperty("role", "primary")
        self.update_banner_button.setAccessibleName("View update release")
        self.update_banner_button.clicked.connect(self.updateRequested.emit)
        self.update_remind_button = QPushButton("Remind me later")
        self.update_remind_button.setProperty("role", "quiet")
        self.update_remind_button.clicked.connect(
            self.updateRemindRequested.emit
        )
        self.update_dismiss_button = QPushButton("Dismiss")
        self.update_dismiss_button.setProperty("role", "quiet")
        self.update_dismiss_button.clicked.connect(
            self.updateDismissRequested.emit
        )
        banner_layout.addWidget(self.update_banner_label, 1)
        banner_layout.addWidget(self.update_banner_button)
        banner_layout.addWidget(self.update_remind_button)
        banner_layout.addWidget(self.update_dismiss_button)
        outer.addWidget(self.update_banner)

    def _on_start_stop_clicked(self) -> None:
        if self._runtime_state == RuntimeState.RUNNING:
            self.stopRequested.emit()
        elif self._runtime_state == RuntimeState.STOPPED:
            self.startRequested.emit()
        elif self._runtime_state == RuntimeState.ERROR:
            self.startRequested.emit()

    def set_app_identity(self, app_name: str, version_text: str) -> None:
        self.app_name_label.setText(app_name)
        self.app_version_label.setText(version_text)

    def set_icon(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self.app_icon_label.setText("")
        self.app_icon_label.setPixmap(
            pixmap.scaled(
                30,
                30,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_brand_banner(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self.brand_image_label.setPixmap(
            pixmap.scaled(
                self.brand_image_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.brand_image_label.show()
        self.app_icon_label.hide()

    def set_runtime_state(
        self,
        state: RuntimeState | str,
        message: str | None = None,
        action_enabled: bool | None = None,
    ) -> None:
        runtime_state = RuntimeState(state)
        self._runtime_state = runtime_state
        texts = {
            RuntimeState.STOPPED: "■ Stopped",
            RuntimeState.STARTING: "◌ Starting…",
            RuntimeState.RUNNING: "● Running",
            RuntimeState.STOPPING: "◌ Stopping…",
            RuntimeState.ERROR: "! Error",
        }
        semantic_states = {
            RuntimeState.STOPPED: "info",
            RuntimeState.STARTING: "info",
            RuntimeState.RUNNING: "success",
            RuntimeState.STOPPING: "warning",
            RuntimeState.ERROR: "error",
        }
        self.runtime_chip.setText(message or texts[runtime_state])
        self.runtime_chip.setProperty(
            "state",
            semantic_states[runtime_state],
        )
        repolish(self.runtime_chip)
        if runtime_state == RuntimeState.RUNNING:
            self.start_stop_button.setText("Stop Streaming")
            self.start_stop_button.setAccessibleName(
                "Stop scenery streaming"
            )
            self.start_stop_button.setEnabled(True)
        elif runtime_state in (RuntimeState.STARTING, RuntimeState.STOPPING):
            self.start_stop_button.setText(texts[runtime_state])
            self.start_stop_button.setEnabled(False)
        else:
            self.start_stop_button.setText("Start Streaming")
            self.start_stop_button.setAccessibleName(
                "Start scenery streaming"
            )
            self.start_stop_button.setEnabled(True)
        if action_enabled is not None:
            self.start_stop_button.setEnabled(
                self.start_stop_button.isEnabled() and bool(action_enabled)
            )

    def set_xplane_state(
        self,
        connected: bool | str,
        message: str | None = None,
    ) -> None:
        self._xplane_connected = bool(connected) if not isinstance(connected, str) else connected.lower() in {"1", "true", "connected", "yes"}
        if isinstance(connected, str) and not message:
            text = connected
        else:
            text = message or (
                "✓ X-Plane connected"
                if self._xplane_connected
                else "○ X-Plane disconnected"
            )
        self.xplane_chip.setText(text)
        self.xplane_chip.setProperty(
            "state",
            "success" if self._xplane_connected else "warning",
        )
        repolish(self.xplane_chip)

    def set_task_count(self, count: int) -> None:
        self._task_count = max(0, int(count))
        if self._task_count == 0:
            text = "○ No active tasks"
        elif self._task_count == 1:
            text = "● 1 active task"
        else:
            text = f"● {self._task_count} active tasks"
        self.task_chip.setText(text)
        self.task_chip.setProperty(
            "state",
            "info" if self._task_count else "",
        )
        repolish(self.task_chip)

    def set_update_available(
        self,
        version_text: str,
        message: str | None = None,
    ) -> None:
        self._update_version_text = version_text
        self.update_available = True
        self.update_banner_label.setText(
            message or f"ⓘ Update available: {version_text}"
        )
        self.update_banner.setVisible(True)

    def clear_update_available(self) -> None:
        self._update_version_text = ""
        self.update_available = False
        self.update_banner.setVisible(False)


class ApplicationShell(QMainWindow):
    """Main shell that composes navigation, pages and activity."""

    startRequested = Signal()
    stopRequested = Signal()
    setupWizardRequested = Signal()
    docsRequested = Signal()
    aboutRequested = Signal()
    quitRequested = Signal()
    updateRequested = Signal()
    updateRemindRequested = Signal()
    updateDismissRequested = Signal()
    updateCheckRequested = Signal()
    fixConfigRequested = Signal()
    installSceneryRequested = Signal()
    openDiagnosticsRequested = Signal()
    openMapRequested = Signal()
    pageChanged = Signal(str)

    def __init__(
        self,
        app_name: str = "AutoOrtho",
        version_text: str = __version__,
        task_manager: TaskManager | None = None,
        task_panel: TaskPanel | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("applicationShell")
        self.setWindowTitle(f"{app_name} {version_text}")

        self.task_manager = task_manager or TaskManager(self)
        self.task_panel = task_panel or TaskPanel(self.task_manager)
        self.navigation = NavigationRail()
        self.header = CompactHeader(app_name=app_name, version_text=version_text)
        self.stack = QStackedWidget()

        self._pages: OrderedDict[str, QWidget] = OrderedDict()
        self._page_titles: dict[str, str] = {}
        self._suppress_page_changed = False

        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self.navigation)

        content = QWidget(central)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(self.header)
        content_layout.addWidget(self.stack, 1)
        content_layout.addWidget(self.task_panel)
        content_layout.setStretch(0, 0)
        content_layout.setStretch(1, 1)
        content_layout.setStretch(2, 0)
        outer.addWidget(content, 1)

        self.header.startRequested.connect(self.startRequested.emit)
        self.header.stopRequested.connect(self.stopRequested.emit)
        self.header.setupWizardRequested.connect(
            self.setupWizardRequested.emit
        )
        self.header.docsRequested.connect(self.docsRequested.emit)
        self.header.aboutRequested.connect(self.aboutRequested.emit)
        self.header.quitRequested.connect(self.quitRequested.emit)
        self.header.updateRequested.connect(self.updateRequested.emit)
        self.header.updateRemindRequested.connect(
            self.updateRemindRequested.emit
        )
        self.header.updateDismissRequested.connect(
            self.updateDismissRequested.emit
        )
        self.header.updateCheckRequested.connect(
            self.updateCheckRequested.emit
        )

        self.navigation.destinationChanged.connect(self.set_page)
        self.stack.currentChanged.connect(self._on_current_page_changed)
        self.task_manager.active_count_changed.connect(self.set_task_count)

        self._build_default_pages()
        self.set_task_count(len(self.task_manager.active_tasks()))
        self.set_runtime_state(RuntimeState.STOPPED)
        self.set_xplane_state(False)

    def _build_default_pages(self) -> None:
        default_pages: dict[str, QWidget] = {
            "home": HomePage(),
            "scenery-library": _PlaceholderPage(
                "Scenery Library",
                "Browse and manage scenery packages.",
            ),
            "flight-plan-map": _PlaceholderPage(
                "Flight Plan & Map",
                "Review SimBrief plans and the map overlay.",
            ),
            "settings": _PlaceholderPage(
                "Settings",
                "Configure X-Plane, cache, and application preferences.",
            ),
            "diagnostics": _PlaceholderPage(
                "Diagnostics",
                "Inspect logs, connectivity, and runtime health.",
            ),
        }
        for key, widget in default_pages.items():
            self.add_page(widget, key=key, title=self._title_for_key(key))
        self.set_page("home")

    @staticmethod
    def _title_for_key(key: str) -> str:
        mapping = {
            "home": "Home",
            "scenery-library": "Scenery Library",
            "flight-plan-map": "Flight Plan & Map",
            "settings": "Settings",
            "diagnostics": "Diagnostics",
        }
        return mapping.get(key, key.replace("-", " ").title())

    def add_page(
        self,
        page: QWidget,
        *,
        key: str | None = None,
        title: str | None = None,
    ) -> QWidget:
        page_key = key or page.objectName() or page.__class__.__name__.lower()
        page_title = title or self._title_for_key(page_key)
        page.setObjectName(page_key)
        page.setProperty("pageKey", page_key)

        existing = self._pages.get(page_key)
        if existing is page:
            return page

        if existing is not None:
            index = self.stack.indexOf(existing)
            if index >= 0:
                self.stack.removeWidget(existing)
                existing.setParent(None)
            self._pages[page_key] = page
            self._page_titles[page_key] = page_title
            insert_index = max(index, 0)
            self.stack.insertWidget(insert_index, page)
        else:
            self._pages[page_key] = page
            self._page_titles[page_key] = page_title
            self.stack.addWidget(page)

        self._wire_home_shortcuts(page)
        return page

    def _wire_home_shortcuts(self, page: QWidget) -> None:
        for attr, signal in (
            ("fix_config_requested", self.fixConfigRequested),
            ("install_scenery_requested", self.installSceneryRequested),
            ("open_diagnostics_requested", self.openDiagnosticsRequested),
            ("open_map_requested", self.openMapRequested),
        ):
            candidate = getattr(page, attr, None)
            if candidate is not None:
                try:
                    candidate.connect(signal.emit)
                except Exception:
                    pass

    def pages(self) -> OrderedDict[str, QWidget]:
        return OrderedDict(self._pages)

    def page(self, key: str) -> QWidget | None:
        return self._pages.get(key)

    def current_page(self) -> QWidget | None:
        return self.stack.currentWidget()

    def set_page(self, page: str | int | QWidget) -> None:
        if isinstance(page, QWidget):
            index = self.stack.indexOf(page)
            if index < 0:
                raise KeyError("page is not part of the shell")
            self._suppress_page_changed = True
            self.stack.setCurrentIndex(index)
            self.pageChanged.emit(self._page_key_for_widget(page))
            return

        if isinstance(page, int):
            if page < 0 or page >= self.stack.count():
                raise IndexError("page index out of range")
            widget = self.stack.widget(page)
            self._suppress_page_changed = True
            self.stack.setCurrentIndex(page)
            if widget is not None:
                self.pageChanged.emit(self._page_key_for_widget(widget))
            return

        widget = self._pages.get(page)
        if widget is None:
            raise KeyError(f"Unknown page: {page}")
        self._suppress_page_changed = True
        self.stack.setCurrentWidget(widget)
        if self.navigation.button_for(page) is not None and self.navigation.current_destination() != page:
            self.navigation.set_current_destination(page, emit=False)
        self.pageChanged.emit(page)

    def _on_current_page_changed(self, index: int) -> None:
        if self._suppress_page_changed:
            self._suppress_page_changed = False
            return
        widget = self.stack.widget(index)
        if widget is None:
            return
        page_key = self._page_key_for_widget(widget)
        if self.navigation.button_for(page_key) is not None and self.navigation.current_destination() != page_key:
            self.navigation.set_current_destination(page_key, emit=False)
        self.pageChanged.emit(page_key)

    def _page_key_for_widget(self, widget: QWidget) -> str:
        page_key = widget.property("pageKey")
        if isinstance(page_key, str):
            return page_key
        for key, candidate in self._pages.items():
            if candidate is widget:
                return key
        return widget.objectName() or widget.__class__.__name__.lower()

    def set_runtime_state(
        self,
        state: RuntimeState | str,
        message: str | None = None,
        action_enabled: bool | None = None,
    ) -> None:
        self.header.set_runtime_state(
            state,
            message=message,
            action_enabled=action_enabled,
        )

    def set_xplane_state(
        self,
        connected: bool | str,
        message: str | None = None,
    ) -> None:
        self.header.set_xplane_state(connected, message=message)

    def set_task_count(self, count: int) -> None:
        self.header.set_task_count(count)

    def set_update_available(
        self,
        version_text: str,
        message: str | None = None,
    ) -> None:
        self.header.set_update_available(version_text, message=message)

    def clear_update_available(self) -> None:
        self.header.clear_update_available()

    # Compatibility aliases for downstream callers.
    addPage = add_page
    setCurrentPage = set_page
    currentPage = current_page
    setRuntimeState = set_runtime_state
    setXPlaneState = set_xplane_state
    setTaskCount = set_task_count
    setUpdateAvailable = set_update_available
    clearUpdateAvailable = clear_update_available


__all__ = ["ApplicationShell", "CompactHeader"]

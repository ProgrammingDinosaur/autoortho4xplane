#!/usr/bin/env python

"""Qt application controller and compatibility bindings."""

import os
import sys
import shutil
import pathlib
import threading
import time
import logging
import re
import webbrowser
from packaging import version

# Handle imports for both package and direct Python execution without importing
# autoortho.py while this module is still being initialized.
if __package__ and __package__.startswith("autoortho."):
    import autoortho.utils.resources_rc
    from autoortho.utils.constants import MAPTYPES, system_type
    from autoortho.utils.mappers import map_kubilus_region_to_simheaven_region
    from autoortho.utils.dsf_utils import DsfUtils, dsf_utils
    from autoortho.utils.mount_utils import cleanup_mountpoint, safe_ismount
    from autoortho.utils.dynamic_zoom import DynamicZoomManager, BASE_ALTITUDE_FT
    from autoortho.utils.custom_map import get_custom_map_config
    from autoortho.utils.simbrief_flight import simbrief_flight_manager
    from autoortho.ui.config_validation import (
        ConfigurationInput,
        ValidationIssue,
        ValidationSeverity,
        validate_configuration,
    )
    from autoortho.ui.runtime_state import RuntimeState
    from autoortho.ui.settings_session import SettingsSession
    from autoortho.ui.task_manager import TaskManager, TaskPanel
    from autoortho.ui.task_models import TaskState, TaskType
    from autoortho.ui.readiness import (
        ReadinessStatus,
        SceneryChoice,
        build_readiness,
        format_bytes,
        free_space_bytes,
        infer_setup_complete,
        package_storage_requirements,
        recursive_directory_usage_bytes,
    )
    from autoortho.ui.setup_wizard import SetupWizard
    from autoortho.ui.dialogs.installation_dialog import (
        InstallationDialog,
        InstallationReview,
    )
    from autoortho.ui.dialogs.roughness_dialog import RoughnessValueDialog
    from autoortho.ui.widgets.scenery_patches import SceneryPatchesWidget
    from autoortho.ui.shell import ApplicationShell
    from autoortho.ui.pages.scenery_page import SceneryLibraryPage
    from autoortho.ui.pages.settings_page import SettingsPage
    from autoortho.ui.pages.flight_plan_page import FlightPlanPage
    from autoortho.ui.pages.diagnostics_page import DiagnosticsPage
    from autoortho.ui.dynamic_zoom_editor import DynamicZoomEditor, PRESETS
    from autoortho.ui.theme import (
        THEME,
        announce_accessible,
        apply_theme,
        repolish,
    )
    from autoortho.ui.service_worker import ServiceWorker
    from autoortho.ui.services import (
        CatalogService,
        ConfigurationService,
        ReadinessService,
        StorageService,
    )
    from autoortho import downloader
    from autoortho.version import __version__
else:
    import utils.resources_rc
    from utils.constants import MAPTYPES, system_type
    from utils.mappers import map_kubilus_region_to_simheaven_region
    from utils.dsf_utils import DsfUtils, dsf_utils
    from utils.mount_utils import cleanup_mountpoint, safe_ismount
    from utils.dynamic_zoom import DynamicZoomManager, BASE_ALTITUDE_FT
    from utils.custom_map import get_custom_map_config
    from utils.simbrief_flight import simbrief_flight_manager
    from ui.config_validation import (
        ConfigurationInput,
        ValidationIssue,
        ValidationSeverity,
        validate_configuration,
    )
    from ui.runtime_state import RuntimeState
    from ui.settings_session import SettingsSession
    from ui.task_manager import TaskManager, TaskPanel
    from ui.task_models import TaskState, TaskType
    from ui.readiness import (
        ReadinessStatus,
        SceneryChoice,
        build_readiness,
        format_bytes,
        free_space_bytes,
        infer_setup_complete,
        package_storage_requirements,
        recursive_directory_usage_bytes,
    )
    from ui.setup_wizard import SetupWizard
    from ui.dialogs.installation_dialog import (
        InstallationDialog,
        InstallationReview,
    )
    from ui.dialogs.roughness_dialog import RoughnessValueDialog
    from ui.widgets.scenery_patches import SceneryPatchesWidget
    from ui.shell import ApplicationShell
    from ui.pages.scenery_page import SceneryLibraryPage
    from ui.pages.settings_page import SettingsPage
    from ui.pages.flight_plan_page import FlightPlanPage
    from ui.pages.diagnostics_page import DiagnosticsPage
    from ui.dynamic_zoom_editor import DynamicZoomEditor, PRESETS
    from ui.theme import (
        THEME,
        announce_accessible,
        apply_theme,
        repolish,
    )
    from ui.service_worker import ServiceWorker
    from ui.services import (
        CatalogService,
        ConfigurationService,
        ReadinessService,
        StorageService,
    )
    import downloader
    from version import __version__

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel, QLineEdit, QCheckBox, QComboBox,
    QSlider, QTextEdit, QFileDialog, QMessageBox, QScrollArea,
    QSplashScreen, QGroupBox, QProgressBar, QStatusBar, QFrame, QSpinBox, QDoubleSpinBox,
    QColorDialog, QRadioButton, QMenu, QStyle,
    QDialog, QApplication
)
from PySide6.QtCore import (
    Qt, Signal, QTimer, QSize, QPoint, QObject, QEvent
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QWheelEvent, QCursor, QKeySequence, QShortcut
)

log = logging.getLogger(__name__)

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    CUR_PATH = os.path.join(sys._MEIPASS, 'autoortho')
else:
    CUR_PATH = os.path.dirname(
        os.path.dirname(os.path.realpath(__file__))
    )










if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.dialogs.dynamic_zoom_dialog import (
        DynamicZoomDialog as QualityStepsDialog,
    )
    from autoortho.ui.widgets import (
        ModernSlider,
        ModernSpinBox,
        QTextEditLogger,
        StyledButton,
    )
    from autoortho.ui.workers import (
        AddRoughnessWorker,
        AddSeasonsWorker,
        CacheCleanupWorker,
        MountControlWorker,
        RestoreDefaultDsfsWorker,
        RestoreRoughnessWorker,
        SceneryDownloadWorker,
        SceneryUninstallWorker,
        SimBriefFetchWorker,
        StorageScanWorker,
        UpdateCheckWorker,
    )
else:
    from ui.dialogs.dynamic_zoom_dialog import (
        DynamicZoomDialog as QualityStepsDialog,
    )
    from ui.widgets import (
        ModernSlider,
        ModernSpinBox,
        QTextEditLogger,
        StyledButton,
    )
    from ui.workers import (
        AddRoughnessWorker,
        AddSeasonsWorker,
        CacheCleanupWorker,
        MountControlWorker,
        RestoreDefaultDsfsWorker,
        RestoreRoughnessWorker,
        SceneryDownloadWorker,
        SceneryUninstallWorker,
        SimBriefFetchWorker,
        StorageScanWorker,
        UpdateCheckWorker,
    )


class ConfigUI(QMainWindow):
    """Main configuration UI window using PyQt6"""

    SETTINGS_WIDGET_ATTRS = (
        "scenery_path_edit",
        "xplane_path_edit",
        "cache_dir_edit",
        "lt_cache_dir_edit",
        "download_dir_edit",
        "showconfig_check",
        "maptype_combo",
        "simheaven_compat_check",
        "using_custom_tiles_check",
        "simbrief_userid_edit",
        "simbrief_use_flight_data_check",
        "simbrief_consideration_radius_spin",
        "simbrief_deviation_threshold_spin",
        "simbrief_prefetch_parked_check",
        "noclean_check",
        "max_download_workers_spin",
        "storage_safety_margin_spin",
        "seasons_convert_workers_slider",
        "compress_dsf_check",
        "mem_cache_slider",
        "file_cache_slider",
        "auto_clean_cache_check",
        "min_zoom_slider",
        "max_zoom_mode_combo",
        "max_zoom_slider",
        "max_zoom_near_airports_slider",
        "use_time_budget_check",
        "tile_budget_slider",
        "maxwait_slider",
        "suspend_maxwait_check",
        "fallback_level_combo",
        "fallback_extends_budget_check",
        "fallback_timeout_slider",
        "prefetch_enabled_check",
        "prefetch_lookahead_slider",
        "prefetch_interval_slider",
        "prefetch_max_chunks_slider",
        "prefetch_radius_slider",
        "predictive_dds_enabled_check",
        "predictive_interval_slider",
        "background_workers_slider",
        "predictive_use_fallbacks_check",
        "pipeline_mode_combo",
        "live_concurrency_slider",
        "buffer_pool_slider",
        "provider_inflight_spinbox",
        "provider_connections_spinbox",
        "download_dispatch_workers_spinbox",
        "provider_adaptive_check",
        "live_tile_admission_spinbox",
        "tile_image_cache_mb_spinbox",
        "fetch_threads_spinbox",
        "seasons_enabled_check",
        "spr_sat_slider",
        "sum_sat_slider",
        "fal_sat_slider",
        "win_sat_slider",
        "compressor_combo",
        "format_combo",
        "gui_check",
        "hide_check",
        "console_log_level_combo",
        "file_log_level_combo",
        "performance_profiling_check",
        "performance_sample_interval_spin",
        "performance_checkpoint_interval_spin",
        "python_allocation_tracing_check",
        "threading_check",
        "winfsp_check",
        "webui_port_edit",
        "xplane_udp_port_edit",
        "time_exclusion_enabled_check",
        "time_exclusion_default_check",
        "sun_night_threshold_spin",
        "sun_day_threshold_spin",
    )
    RESTART_REQUIRED_SETTINGS = {
        "scenery_path_edit",
        "xplane_path_edit",
        "cache_dir_edit",
        "lt_cache_dir_edit",
        "download_dir_edit",
        "using_custom_tiles_check",
        "max_download_workers_spin",
        "seasons_convert_workers_slider",
        "compress_dsf_check",
        "mem_cache_slider",
        "file_cache_slider",
        "min_zoom_slider",
        "max_zoom_mode_combo",
        "max_zoom_slider",
        "max_zoom_near_airports_slider",
        "use_time_budget_check",
        "tile_budget_slider",
        "maxwait_slider",
        "suspend_maxwait_check",
        "fallback_level_combo",
        "fallback_extends_budget_check",
        "fallback_timeout_slider",
        "prefetch_enabled_check",
        "prefetch_lookahead_slider",
        "prefetch_interval_slider",
        "prefetch_max_chunks_slider",
        "prefetch_radius_slider",
        "predictive_dds_enabled_check",
        "predictive_interval_slider",
        "background_workers_slider",
        "predictive_use_fallbacks_check",
        "pipeline_mode_combo",
        "live_concurrency_slider",
        "buffer_pool_slider",
        "provider_inflight_spinbox",
        "provider_connections_spinbox",
        "download_dispatch_workers_spinbox",
        "provider_adaptive_check",
        "live_tile_admission_spinbox",
        "tile_image_cache_mb_spinbox",
        "fetch_threads_spinbox",
        "seasons_enabled_check",
        "spr_sat_slider",
        "sum_sat_slider",
        "fal_sat_slider",
        "win_sat_slider",
        "compressor_combo",
        "format_combo",
        "performance_profiling_check",
        "performance_sample_interval_spin",
        "performance_checkpoint_interval_spin",
        "python_allocation_tracing_check",
        "threading_check",
        "winfsp_check",
        "webui_port_edit",
        "xplane_udp_port_edit",
        "time_exclusion_enabled_check",
        "time_exclusion_default_check",
        "sun_night_threshold_spin",
        "sun_day_threshold_spin",
        "dynamic_zoom_steps",
        "missing_color",
    }

    status_update = Signal(str)
    log_update = Signal(str)
    show_error = Signal(str)

    def __init__(self, cfg, *args, **kwargs):
        super().__init__()
        self.cfg = cfg
        self.ready = threading.Event()
        self.ready.clear()
        self.system = system_type

        self.dl = downloader.OrthoManager(
            self.cfg.paths.scenery_path,
            self.cfg.paths.download_dir,
            noclean=self.cfg.scenery.noclean
        )
        self.catalog_service = CatalogService(self.dl)
        self.configuration_service = ConfigurationService(self.cfg)
        self.readiness_service = ReadinessService()
        self.storage_service = StorageService()
        self.catalog_worker = None
        self.readiness_worker = None
        self.setup_inference_worker = None
        self.install_preflight_workers = {}

        self.running = False
        self.warnings = []
        self.errors = []
        self.show_errs = []

        # Download management
        self.download_workers = {}
        self.download_progress = {}
        self.uninstall_workers = {}
        self.add_seasons_workers = {}
        self.add_seasons_queue = []  # queue of region_id/package_name waiting to run add seasons
        self.add_seasons_current = None  # currently processing region_id/package_name
        self.restore_default_dsfs_workers = {}
        self.reapply_after_restore = set()
        self.installed_package_names = []
        self.simheaven_config_changed_session = False
        self.installed_packages = []
        self.cache_thread = None
        self._closing = False
        self._shutdown_in_progress = False
        self._ready_to_close = False
        self.runtime_state = RuntimeState.STOPPED
        self.mount_control_worker = None
        self._stop_target_state = RuntimeState.STOPPED
        self._runtime_error_message = ""
        self._close_after_stop = False
        self._restoring_settings = False
        self._settings_tracking_ready = False
        self._restart_pending = False
        self._latest_update_url = ""
        self._latest_update_version = ""
        self._update_check_manual = False
        self._settings_observe_scheduled = False
        self._setup_wizard_checked = False
        self.phase3_active = False
        self.current_readiness = None
        self._readiness_signature = None
        self._start_requested_after_readiness = False
        self.storage_scan_worker = None
        self.task_manager = TaskManager(self)
        self.settings_session = SettingsSession(
            self.RESTART_REQUIRED_SETTINGS,
            self,
        )

        # Set up logging handler for UI (must be None before init_ui is called)
        self.ui_log_handler = None

        # Setup UI
        self.init_ui()

        # Connect signals
        self.status_update.connect(self.update_status_bar)
        self.log_update.connect(self.append_log)
        self.show_error.connect(self.display_error)

        self.mount_monitor_timer = QTimer(self)
        self.mount_monitor_timer.setInterval(1000)
        self.mount_monitor_timer.timeout.connect(self._check_mount_workers)
        self._set_runtime_state(RuntimeState.STOPPED)
        self._initialize_settings_session()
        self.readiness_timer = QTimer(self)
        self.readiness_timer.setSingleShot(True)
        self.readiness_timer.setInterval(300)
        self.readiness_timer.timeout.connect(
            self._start_readiness_checks_async
        )
        for edit in (
            self.xplane_path_edit,
            self.scenery_path_edit,
            self.cache_dir_edit,
            self.lt_cache_dir_edit,
            self.download_dir_edit,
        ):
            edit.textChanged.connect(self._schedule_readiness_checks)
        self._start_readiness_checks_async()

        self.ready.set()

        # Kick off asynchronous update check shortly after startup
        try:
            QTimer.singleShot(250, self.start_update_check)
        except Exception:
            pass

    def init_ui(self):
        """Initialize the user interface"""
        self.setWindowTitle(f'AutoOrtho ver {__version__}')
        width = int(getattr(self.cfg.general, "window_width", 1100))
        height = int(getattr(self.cfg.general, "window_height", 760))
        x = int(getattr(self.cfg.general, "window_x", 100))
        y = int(getattr(self.cfg.general, "window_y", 100))
        on_screen = any(
            screen.availableGeometry().contains(QPoint(x, y))
            for screen in QApplication.screens()
        )
        self.setGeometry(
            x if on_screen else 100,
            y if on_screen else 100,
            max(900, width),
            max(650, height),
        )

        # Set application style
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1E1E1E;
            }
            QWidget {
                background-color: #1E1E1E;
                color: #E0E0E0;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
            }
            QTabWidget::pane {
                border: 1px solid #3A3A3A;
                background-color: #2A2A2A;
                border-radius: 4px;
            }
            QTabBar::tab {
                background-color: #2A2A2A;
                color: #999;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background-color: #3A3A3A;
                color: #ffffff;
                font-weight: bold;
            }
            QTabBar::tab:hover {
                background-color: #3A3A3A;
                color: #E0E0E0;
            }
            QLineEdit {
                background-color: #3A3A3A;
                border: 1px solid #555;
                padding: 6px;
                border-radius: 4px;
                color: white;
            }
            QLineEdit:focus {
                border-color: #1d71d1;
            }
            QLineEdit[validationError="true"] {
                border: 1px solid #d9534f;
            }
            QTextEdit {
                background-color: #2A2A2A;
                border: 1px solid #3A3A3A;
                border-radius: 4px;
                padding: 4px;
                color: #E0E0E0;
                font-family: 'Consolas', 'Courier New', monospace;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 3px;
                border: 1px solid #555;
                background-color: #3A3A3A;
            }
            QCheckBox::indicator:checked {
                background-color: #1d71d1;
                border-color: #1d71d1;
            }
            QComboBox {
                background-color: #3A3A3A;
                border: 1px solid #555;
                padding: 6px;
                border-radius: 4px;
                min-width: 150px;
            }
            QComboBox:hover {
                border-color: #1d71d1;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: url(:/imgs/arrow-204-16.png);
                width: 16px;
                height: 16px;
                margin-right: 10px;
            }
            QGroupBox {
                border: 1px solid #3A3A3A;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                color: #ffffff;
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLabel {
                color: #E0E0E0;
            }
            QStatusBar {
                background-color: #2A2A2A;
                border-top: 1px solid #3A3A3A;
                color: #999;
            }
            QScrollBar:vertical {
                background-color: #2A2A2A;
                width: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background-color: #555;
                border-radius: 6px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #666;
            }
        """)

        # Set icon
        if self.system == 'windows':
            icon_path = ":/imgs/ao-icon.ico"
        else:
            icon_path = ":/imgs/ao-icon.png"
        self.setWindowIcon(QIcon(icon_path))

        self.shell = ApplicationShell(
            app_name="AutoOrtho",
            version_text=__version__,
            task_manager=self.task_manager,
        )
        shell_central = self.shell.takeCentralWidget()
        self.setCentralWidget(shell_central)
        self.task_panel = self.shell.task_panel
        self.shell.header.set_icon(QPixmap(icon_path))
        self.shell.header.set_brand_banner(QPixmap(":/imgs/banner1.png"))

        # Hidden compatibility host retained for internal navigation and tests.
        self.tabs = QTabWidget(self)
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.hide()

        # Create tabs
        self.create_setup_tab()
        self.create_scenery_tab()
        self.create_settings_tab()
        self.create_custom_map_tab()
        self.create_logs_tab()

        self.apply_button = StyledButton("Apply")
        self.apply_button.clicked.connect(self.on_save)
        self.save_button = self.apply_button

        self.revert_button = StyledButton("Revert")
        self.revert_button.clicked.connect(self.on_revert)

        self.restart_notice_label = QLabel(
            "Restart streaming to apply all changes."
        )
        self.restart_notice_label.setStyleSheet(
            "color: #f0ad4e; font-weight: bold;"
        )
        self.restart_notice_label.hide()

        self.quit_button = StyledButton("Quit")
        self.quit_button.setParent(self)
        self.quit_button.hide()
        self.quit_button.clicked.connect(self.close)
        self.run_button = self.shell.header.start_stop_button
        self.shell.startRequested.connect(self.on_run)
        self.shell.stopRequested.connect(self.on_run)
        self.shell.setupWizardRequested.connect(
            lambda: self._maybe_show_setup_wizard(force=True)
        )
        self.shell.docsRequested.connect(
            lambda: webbrowser.open(
                "https://programmingdinosaur.github.io/autoortho4xplane/"
            )
        )
        self.shell.aboutRequested.connect(self._show_about)
        self.shell.quitRequested.connect(self.close)
        self.shell.fixConfigRequested.connect(
            lambda: self.navigate_to("settings", "Paths & Storage")
        )
        self.shell.installSceneryRequested.connect(
            lambda: self.navigate_to("scenery-library")
        )
        self.shell.openDiagnosticsRequested.connect(
            lambda: self.navigate_to("diagnostics")
        )
        self.shell.openMapRequested.connect(
            lambda: self.navigate_to("flight-plan-map")
        )
        self.shell.pageChanged.connect(self._on_shell_page_changed)
        self.tabs.currentChanged.connect(self._on_legacy_tab_changed)

        self._build_phase3_pages()
        self.setMinimumSize(820, 620)
        self._clear_migrated_inline_styles()
        self.setStyleSheet("")
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, THEME)
        self._apply_semantic_roles()
        self._install_keyboard_shortcuts()

        # Status bar
        self.status_bar = QStatusBar()
        self.status_bar.setAccessibleName("Application status")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def _apply_semantic_roles(self):
        role_map = {
            self.apply_button: "primary",
            self.revert_button: "secondary",
            self.clean_cache_btn: "secondary",
            self.clean_jpegs_btn: "secondary",
            self.delete_cache_btn: "destructive",
            self.reset_color_button: "quiet",
        }
        for button, role in role_map.items():
            button.setProperty("role", role)
            repolish(button)

        accessible_names = {
            "scenery_path_edit": "Scenery installation folder",
            "xplane_path_edit": "X-Plane installation folder",
            "cache_dir_edit": "Image cache folder",
            "lt_cache_dir_edit": "Long-term cache folder",
            "download_dir_edit": "Temporary download folder",
            "maptype_combo": "Imagery source",
            "simbrief_userid_edit": "SimBrief pilot ID",
            "max_zoom_mode_combo": "Maximum detail mode",
            "webui_port_edit": "Web interface port",
            "xplane_udp_port_edit": "X-Plane UDP port",
        }
        for attr, name in accessible_names.items():
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setAccessibleName(name)
        for attr, widget in self._settings_widgets():
            if not widget.accessibleName():
                name = attr
                for suffix in (
                    "_edit",
                    "_check",
                    "_combo",
                    "_slider",
                    "_spinbox",
                    "_spin",
                ):
                    name = name.removesuffix(suffix)
                widget.setAccessibleName(
                    name.replace("_", " ").title()
                )
            if not widget.accessibleDescription() and widget.toolTip():
                widget.setAccessibleDescription(widget.toolTip())

        self.apply_button.setAccessibleName("Apply pending settings")
        self.revert_button.setAccessibleName("Revert pending settings")
        self.delete_cache_btn.setAccessibleDescription(
            "Permanently removes all cached imagery after confirmation."
        )
        self._set_primary_tab_order()

    def _clear_migrated_inline_styles(self):
        preserve = {self.missing_color_button}
        for root in (
            self.shell.header,
            self.shell.navigation,
            self.home_page,
            self.scenery_library_page,
            self.flight_plan_page,
            self.categorized_settings_page,
            self.diagnostics_page,
        ):
            for widget in [root, *root.findChildren(QWidget)]:
                if widget not in preserve:
                    widget.setStyleSheet("")
        self.simbrief_route_label.setProperty("textRole", "sectionTitle")
        self.simbrief_details_label.setProperty("textRole", "secondary")
        self.simbrief_error_label.setProperty("validationState", "error")
        self.setup_validation_label.setProperty(
            "validationState",
            "error",
        )
        self.cache_storage_label.setProperty("textRole", "caption")

    def _set_primary_tab_order(self):
        navigation_buttons = [
            self.shell.navigation.button_for(key)
            for key in self.shell.navigation.destination_keys()
        ]
        navigation_buttons = [
            button for button in navigation_buttons if button is not None
        ]
        for previous, current in zip(
            navigation_buttons,
            navigation_buttons[1:],
        ):
            self.setTabOrder(previous, current)
        if navigation_buttons:
            self.setTabOrder(
                navigation_buttons[-1],
                self.shell.header.start_stop_button,
            )
            self.setTabOrder(
                self.shell.header.start_stop_button,
                self.shell.header.overflow_button,
            )
        self.setTabOrder(
            self.categorized_settings_page.search_edit,
            self.categorized_settings_page.preset_combo,
        )
        self.setTabOrder(
            self.scenery_library_page.search_edit,
            self.scenery_library_page.status_filter,
        )
        self.setTabOrder(
            self.scenery_library_page.status_filter,
            self.scenery_library_page.region_filter,
        )

    def _install_keyboard_shortcuts(self):
        self._ui_shortcuts = []

        def shortcut(sequence, callback):
            binding = QShortcut(QKeySequence(sequence), self)
            binding.activated.connect(callback)
            self._ui_shortcuts.append(binding)

        shortcut(QKeySequence.StandardKey.Save, self.on_save)
        shortcut("Ctrl+Shift+R", self.on_revert)
        shortcut(QKeySequence.StandardKey.HelpContents, lambda: webbrowser.open(
            "https://programmingdinosaur.github.io/autoortho4xplane/"
        ))
        for number, page in enumerate(
            (
                "home",
                "scenery-library",
                "flight-plan-map",
                "settings",
                "diagnostics",
            ),
            start=1,
        ):
            shortcut(
                f"Ctrl+{number}",
                lambda page_key=page: self.navigate_to(page_key),
            )

    def _build_phase3_pages(self):
        self.phase3_active = True
        self.home_page = self.shell.page("home")

        self.scenery_library_page = SceneryLibraryPage()
        self.scenery_library_page.install_requested.connect(
            self.on_install_scenery
        )
        self.scenery_library_page.uninstall_requested.connect(
            self.on_delete_scenery
        )
        self.scenery_library_page.options_requested.connect(
            self._open_scenery_options_for_region
        )
        self.scenery_library_page.settings_requested.connect(
            lambda: self.navigate_to("settings", "Paths & Storage")
        )
        self.shell.add_page(
            self.scenery_library_page,
            key="scenery-library",
            title="Scenery Library",
        )

        self.flight_plan_page = FlightPlanPage(self.simbrief_group)
        self.flight_plan_page.set_map_port(self.cfg.flightdata.webui_port)
        self.simbrief_route_label.hide()
        self.simbrief_details_label.hide()
        self.shell.add_page(
            self.flight_plan_page,
            key="flight-plan-map",
            title="Flight Plan & Map",
        )

        self.dynamic_zoom_editor = DynamicZoomEditor(
            self._dynamic_zoom_manager
        )
        self.dynamic_zoom_btn.hide()
        self.dynamic_zoom_editor.changed.connect(
            self._on_inline_dynamic_zoom_changed
        )
        dynamic_group = QGroupBox("Altitude-Based Quality")
        dynamic_layout = QVBoxLayout(dynamic_group)
        dynamic_layout.addWidget(self.dynamic_zoom_editor)

        self.categorized_settings_page = SettingsPage(
            self.apply_button,
            self.revert_button,
            self.restart_notice_label,
        )
        self.categorized_settings_page.add_category(
            "General",
            [self.options_group, self.general_settings_group],
            recommendation=(
                "General startup behavior and live log verbosity. "
                "Most users can keep the recommended defaults."
            ),
        )
        self.categorized_settings_page.add_category(
            "Paths & Storage",
            [
                self.paths_group,
                self.scenery_settings_group,
                self.cache_settings_group,
            ],
            recommendation=(
                "Use an SSD for the image cache and keep enough free space "
                "for both downloads and installed scenery."
            ),
            numeric_bindings=(
                ("Memory cache", self.mem_cache_slider, 1, " GB"),
                ("File cache limit", self.file_cache_slider, 1, " GB"),
            ),
        )
        self.categorized_settings_page.add_category(
            "Imagery Quality",
            [self.imagery_settings_group],
            recommendation=(
                "Each additional zoom level can multiply imagery, network, "
                "and VRAM use by approximately four."
            ),
            numeric_bindings=(
                ("Minimum zoom", self.min_zoom_slider, 1, ""),
                ("Maximum zoom", self.max_zoom_slider, 1, ""),
                (
                    "Airport maximum zoom",
                    self.max_zoom_near_airports_slider,
                    1,
                    "",
                ),
            ),
        )
        self.categorized_settings_page.add_category(
            "Dynamic Zoom",
            [dynamic_group],
            recommendation=(
                "Use altitude steps to preserve detail near the ground while "
                "reducing cruise resource usage."
            ),
        )
        self.categorized_settings_page.add_category(
            "Prefetching",
            [self.prefetch_settings_group],
            recommendation=(
                "Balanced prefetching reduces stutters without saturating "
                "the network or CPU."
            ),
            numeric_bindings=(
                (
                    "Lookahead",
                    self.prefetch_lookahead_slider,
                    1,
                    " min",
                ),
                (
                    "Prefetch radius",
                    self.prefetch_radius_slider,
                    1,
                    " nm",
                ),
            ),
        )
        self.categorized_settings_page.add_category(
            "Performance",
            [self.performance_settings_group],
            recommendation=(
                "Change timeout and fallback behavior only when diagnosing "
                "loading stalls or missing imagery."
            ),
            numeric_bindings=(
                (
                    "Tile time budget",
                    self.tile_budget_slider,
                    1,
                    " s",
                ),
                (
                    "Per-chunk wait",
                    self.maxwait_slider,
                    10,
                    " s",
                ),
            ),
        )
        self.categorized_settings_page.add_category(
            "Seasons",
            [self.scenery_seasons_group, self.seasons_settings_group],
        )
        self.categorized_settings_page.add_category(
            "Compression & Pipeline",
            [self.dds_settings_group, self.pipeline_settings_group],
            numeric_bindings=(
                (
                    "Tile build workers",
                    self.live_concurrency_slider,
                    1,
                    "",
                ),
                (
                    "Buffer pool",
                    self.buffer_pool_slider,
                    1,
                    "",
                ),
            ),
        )
        self.categorized_settings_page.add_category(
            "Flight Data",
            [self.flightdata_settings_group],
        )
        self.categorized_settings_page.add_category(
            "Diagnostics",
            [self.diagnostics_settings_group],
        )
        self.categorized_settings_page.add_category(
            "Platform & Mounting",
            [self.fuse_settings_group, self.night_settings_group],
        )
        self.categorized_settings_page.preset_requested.connect(
            self._apply_settings_preset
        )
        self.categorized_settings_page.restore_defaults_requested.connect(
            self._restore_category_defaults
        )
        self.shell.add_page(
            self.categorized_settings_page,
            key="settings",
            title="Settings",
        )

        self.diagnostics_page = DiagnosticsPage(
            self.log_text,
            self.ui_log_handler,
            log_path=self.cfg.paths.log_file,
            report_dir=getattr(
                self.cfg.diagnostics,
                "report_dir",
                "~/.autoortho-data/reports",
            ),
        )
        self.diagnostics_page.settings_requested.connect(
            lambda: self.navigate_to("settings", "Diagnostics")
        )
        self.shell.add_page(
            self.diagnostics_page,
            key="diagnostics",
            title="Diagnostics",
        )

        self.task_manager.task_added.connect(
            lambda task: self._refresh_phase3_task_views()
        )
        self.task_manager.task_updated.connect(
            lambda task: self._refresh_phase3_task_views()
        )
        self.task_manager.task_removed.connect(
            lambda task_id: self._refresh_phase3_task_views()
        )
        self.task_manager.active_count_changed.connect(
            lambda count: self._update_shell_status()
        )
        self.shell.updateRequested.connect(self._open_available_update)
        self.shell.updateRemindRequested.connect(
            self._remind_update_later
        )
        self.shell.updateDismissRequested.connect(
            self._dismiss_available_update
        )
        self.shell.updateCheckRequested.connect(
            lambda: self.start_update_check(manual=True)
        )

        self.shell_status_timer = QTimer(self)
        self.shell_status_timer.setInterval(1000)
        self.shell_status_timer.timeout.connect(self._update_shell_status)
        self.shell_status_timer.start()

        saved_page = str(
            getattr(self.cfg.general, "last_page", "Home")
        ).strip().lower().replace(" ", "-")
        aliases = {
            "scenery": "scenery-library",
            "flight-plan-&-map": "flight-plan-map",
            "advanced-settings": "settings",
            "logs": "diagnostics",
            "map": "flight-plan-map",
        }
        saved_page = aliases.get(saved_page, saved_page)
        if self.shell.page(saved_page) is None:
            saved_page = "home"
        self.shell.set_page(saved_page)
        self._refresh_phase3_task_views()
        self._update_shell_status()

    def _open_scenery_options_for_region(self, region_id):
        region = self.dl.regions.get(region_id)
        if region is None:
            return
        release = region.get_latest_release()
        release.parse()
        patch_source = region.local_rel or release
        self.on_scenery_options_clicked(
            region_id,
            patch_source.seasons_apply_status,
            getattr(
                patch_source,
                "roughness_apply_status",
                downloader.RoughnessApplyStatus.NOT_APPLIED,
            ),
            getattr(patch_source, "roughness_value", None),
        )

    def _on_inline_dynamic_zoom_changed(self, manager):
        self._dynamic_zoom_manager = manager.clone()
        self._update_dynamic_summary()
        self._update_buffer_pool_label()
        self._on_settings_control_changed()

    def _apply_settings_preset(self, name):
        presets = {
            "Balanced": {
                "max_zoom_slider": 16,
                "tile_budget_slider": 120,
                "maxwait_slider": 20,
                "fallback_timeout_slider": 30,
                "prefetch_lookahead_slider": 10,
                "prefetch_max_chunks_slider": 48,
                "prefetch_radius_slider": 40,
                "background_workers_slider": 4,
                "live_concurrency_slider": 8,
                "provider_inflight_spinbox": 128,
                "provider_connections_spinbox": 64,
                "download_dispatch_workers_spinbox": 4,
                "live_tile_admission_spinbox": 16,
                "tile_image_cache_mb_spinbox": 96,
            },
            "Quality": {
                "max_zoom_slider": 17,
                "tile_budget_slider": 300,
                "maxwait_slider": 50,
                "fallback_timeout_slider": 60,
                "prefetch_lookahead_slider": 30,
                "prefetch_max_chunks_slider": 96,
                "prefetch_radius_slider": 50,
                "background_workers_slider": 6,
                "live_concurrency_slider": 12,
                "provider_inflight_spinbox": 192,
                "provider_connections_spinbox": 64,
                "download_dispatch_workers_spinbox": 6,
                "live_tile_admission_spinbox": 20,
                "tile_image_cache_mb_spinbox": 128,
            },
            "Low Bandwidth": {
                "max_zoom_slider": 15,
                "tile_budget_slider": 60,
                "maxwait_slider": 30,
                "fallback_timeout_slider": 30,
                "prefetch_lookahead_slider": 10,
                "prefetch_max_chunks_slider": 24,
                "prefetch_radius_slider": 30,
                "background_workers_slider": 2,
                "live_concurrency_slider": 6,
                "provider_inflight_spinbox": 64,
                "provider_connections_spinbox": 32,
                "download_dispatch_workers_spinbox": 3,
                "live_tile_admission_spinbox": 8,
                "tile_image_cache_mb_spinbox": 64,
            },
            "Low Resource": {
                "max_zoom_slider": 15,
                "tile_budget_slider": 60,
                "maxwait_slider": 20,
                "fallback_timeout_slider": 20,
                "prefetch_lookahead_slider": 5,
                "prefetch_max_chunks_slider": 16,
                "prefetch_radius_slider": 20,
                "background_workers_slider": 1,
                "live_concurrency_slider": 4,
                "provider_inflight_spinbox": 64,
                "provider_connections_spinbox": 16,
                "download_dispatch_workers_spinbox": 2,
                "live_tile_admission_spinbox": 6,
                "tile_image_cache_mb_spinbox": 64,
            },
        }
        values = presets.get(name)
        if values is None:
            return
        for attr, value in values.items():
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setValue(value)
        if name == "Quality":
            self.fallback_level_combo.setCurrentIndex(2)
            self.fallback_extends_budget_check.setChecked(True)
            self.suspend_maxwait_check.setChecked(True)
        else:
            self.fallback_level_combo.setCurrentIndex(1)
            self.fallback_extends_budget_check.setChecked(False)
            self.suspend_maxwait_check.setChecked(False)
        self.provider_adaptive_check.setChecked(True)
        if name in ("Balanced", "Quality", "Low Resource"):
            dynamic_preset = {
                "Balanced": "Airliner",
                "Quality": "General Aviation",
                "Low Resource": "Low VRAM",
            }[name]
            self.dynamic_zoom_editor.apply_preset(dynamic_preset)
        self._on_settings_control_changed()

    def _restore_category_defaults(self, category):
        defaults = {
            "General": {
                "showconfig_check": True,
                "gui_check": True,
                "hide_check": True,
                "console_log_level_combo": "INFO",
                "file_log_level_combo": "DEBUG",
            },
            "Imagery Quality": {
                "min_zoom_slider": 12,
                "max_zoom_slider": 16,
                "max_zoom_near_airports_slider": 18,
            },
            "Prefetching": {
                "prefetch_enabled_check": True,
                "prefetch_lookahead_slider": 10,
                "prefetch_max_chunks_slider": 48,
                "prefetch_radius_slider": 40,
            },
            "Performance": {
                "use_time_budget_check": True,
                "tile_budget_slider": 180,
                "maxwait_slider": 20,
            },
            "Compression & Pipeline": {
                "pipeline_mode_combo": "auto",
                "format_combo": "BC1",
                "live_concurrency_slider": 8,
            },
        }
        if category == "Dynamic Zoom":
            self.dynamic_zoom_editor.apply_preset("Airliner")
            return
        for attr, value in defaults.get(category, {}).items():
            widget = getattr(self, attr, None)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif widget is not None:
                widget.setValue(value)
        self._on_settings_control_changed()

    def navigate_to(self, page, category=None):
        self.shell.set_page(page)
        if page == "settings" and category:
            self.categorized_settings_page.select_category(category)

    def _on_legacy_tab_changed(self, index):
        if not self.phase3_active:
            return
        widget = self.tabs.widget(index)
        if widget is self.scenery_widget:
            self.navigate_to("scenery-library")
        elif widget is self.logs_widget:
            self.navigate_to("diagnostics")
        elif widget is self.custom_map_widget:
            self.navigate_to("flight-plan-map")
        elif widget is self.settings_widget:
            self.navigate_to("settings")
        elif widget is self.setup_widget:
            self.navigate_to("settings", "Paths & Storage")

    def _on_shell_page_changed(self, page):
        self.cfg.general.last_page = {
            "home": "Home",
            "scenery-library": "Scenery Library",
            "flight-plan-map": "Flight Plan & Map",
            "settings": "Settings",
            "diagnostics": "Diagnostics",
        }.get(page, page)
        if page == "flight-plan-map":
            self.flight_plan_page.check_map_service()
        elif page == "diagnostics":
            self.diagnostics_page.refresh_reports()
        elif page == "scenery-library":
            self._refresh_phase3_task_views()

    def _refresh_phase3_task_views(self):
        if not self.phase3_active:
            return
        self.scenery_library_page.set_regions(
            self.dl.regions.values(),
            self.task_manager.tasks.values(),
        )
        active = self.task_manager.active_tasks()
        failures = [
            task for task in self.task_manager.tasks.values()
            if task.state == TaskState.FAILED
        ]
        rate = sum(task.rate for task in active)
        self.home_page.set_task(
            f"{len(active)} active" if active else "Idle",
            detail=", ".join(task.title for task in active[:3]),
        )
        self.home_page.set_throughput(
            f"{rate / (1024 * 1024):.1f} MB/s"
        )
        if failures:
            failure = failures[-1]
            self.home_page.set_recent_failure(
                failure.title,
                detail=failure.error,
            )
        else:
            self.home_page.clear_recent_failure()

    def _update_shell_status(self):
        if not self.phase3_active:
            return
        try:
            try:
                from autoortho.datareftrack import dt
            except ImportError:
                from datareftrack import dt
            flight_data = dt.get_flight_data()
            xplane_connected = flight_data is not None
        except Exception:
            xplane_connected = False
        self.shell.set_runtime_state(
            self.runtime_state,
            action_enabled=(
                self.runtime_state
                in (
                    RuntimeState.STOPPED,
                    RuntimeState.RUNNING,
                    RuntimeState.ERROR,
                )
                and not self._has_active_ui_jobs()
            ),
        )
        self.shell.set_xplane_state(xplane_connected)
        self.scenery_library_page.set_runtime_locked(
            xplane_connected
            or self.runtime_state
            not in (RuntimeState.STOPPED, RuntimeState.ERROR)
        )
        self.home_page.set_runtime_state(
            self.runtime_state.value.title(),
            self.status_bar.currentMessage()
            if hasattr(self, "status_bar")
            else "",
        )
        self.home_page.set_xplane_state(
            "Connected" if xplane_connected else "Disconnected"
        )
        mounted_count = len(
            [
                handle for handle in getattr(self, "mount_workers", [])
                if handle.process.poll() is None
            ]
        )
        self.home_page.set_mounted_scenery(
            f"{mounted_count} active",
            detail=f"{len(self.cfg.scenery_mounts)} configured",
        )
        self.home_page.set_provider(self.maptype_combo.currentText())
        simbrief_loaded = bool(
            getattr(self, "simbrief_flight_data", None)
        )
        simbrief_active = (
            simbrief_loaded
            and self.simbrief_use_flight_data_check.isChecked()
        )
        self.home_page.set_simbrief(
            "Active" if simbrief_active else "Loaded"
            if simbrief_loaded
            else "No flight plan"
        )
        if self.current_readiness is not None:
            warnings = [
                check for check in self.current_readiness.checks
                if check.status != ReadinessStatus.SUCCESS
            ]
            self.home_page.set_readiness_state(
                "Ready" if not warnings else f"{len(warnings)} issue(s)",
                detail=", ".join(check.title for check in warnings),
            )
            self.diagnostics_page.set_health(
                self.current_readiness.checks,
                xplane_connected=xplane_connected,
                runtime_state=self.runtime_state.value,
            )
        self.flight_plan_page.set_influence(simbrief_active)
        try:
            port = self.cfg.flightdata.webui_port
            self.flight_plan_page.set_map_port(port)
        except Exception:
            pass

    def _show_about(self):
        QMessageBox.about(
            self,
            "About AutoOrtho",
            f"AutoOrtho {__version__}\n\n"
            "On-demand orthophoto scenery streaming for X-Plane.",
        )

    def _persist_shell_state(self):
        self.cfg.general.window_width = self.width()
        self.cfg.general.window_height = self.height()
        self.cfg.general.window_x = self.x()
        self.cfg.general.window_y = self.y()
        self.cfg.general.last_page = getattr(
            self.cfg.general,
            "last_page",
            "Home",
        )
        self._persist_configuration()

    def _persist_configuration(self, *, notify=False):
        result = self.configuration_service.persist(
            create_missing=False,
        )
        if result.success:
            return True
        message = result.error.message
        if result.error.detail:
            message += f"\n\n{result.error.detail}"
        log.error(message)
        if notify:
            self.display_error(message)
        return False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Use getattr() for all combo boxes as they may not exist during init
        if (
            obj is getattr(self, 'maptype_combo', None)
            or obj is getattr(self, 'max_zoom_mode_combo', None)
            or obj is getattr(self, 'fallback_level_combo', None)
            or (not self.system == "darwin" and obj is getattr(self, 'compressor_combo', None))
            or obj is getattr(self, 'format_combo', None)
            or obj is getattr(self, 'console_log_level_combo', None)
            or obj is getattr(self, 'file_log_level_combo', None)
            or obj is getattr(self, 'pipeline_mode_combo', None)
        ) and event.type() == QEvent.Type.Wheel:
            if not obj.hasFocus():
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def _settings_widgets(self):
        widgets = []
        for attr in self.SETTINGS_WIDGET_ATTRS:
            widget = getattr(self, attr, None)
            if widget is not None:
                widgets.append((attr, widget))
        return widgets

    @staticmethod
    def _settings_widget_value(widget):
        if isinstance(widget, QLineEdit):
            return widget.text()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, (QSlider, QSpinBox, QDoubleSpinBox)):
            return widget.value()
        raise TypeError(f"Unsupported settings widget: {type(widget)!r}")

    def _snapshot_settings(self):
        snapshot = {
            attr: self._settings_widget_value(widget)
            for attr, widget in self._settings_widgets()
        }
        if hasattr(self, "missing_color"):
            snapshot["missing_color"] = (
                self.missing_color.red(),
                self.missing_color.green(),
                self.missing_color.blue(),
            )
        if hasattr(self, "_dynamic_zoom_manager"):
            snapshot["dynamic_zoom_steps"] = (
                self._dynamic_zoom_manager.save_to_config()
            )
        return snapshot

    def _initialize_settings_session(self):
        self.settings_session.dirty_changed.connect(
            self._update_settings_actions
        )
        self.settings_session.restart_required_changed.connect(
            self._update_settings_actions
        )
        self._hook_settings_widgets()
        self.settings_session.initialize(self._snapshot_settings())
        self._settings_tracking_ready = True
        self._update_settings_actions()
        self._on_tab_changed(self.tabs.currentIndex())

    def _hook_settings_widgets(self):
        for _, widget in self._settings_widgets():
            if widget.property("settingsTracked"):
                continue
            widget.setProperty("settingsTracked", True)
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._on_settings_control_changed)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._on_settings_control_changed)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(
                    self._on_settings_control_changed
                )
            elif isinstance(widget, (QSlider, QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(self._on_settings_control_changed)

    def _on_settings_control_changed(self, *args):
        if (
            not self._settings_tracking_ready
            or self._restoring_settings
            or self._settings_observe_scheduled
        ):
            return
        self._settings_observe_scheduled = True
        QTimer.singleShot(0, self._observe_settings)

    def _observe_settings(self):
        self._settings_observe_scheduled = False
        if self._restoring_settings:
            return
        self.settings_session.observe(self._snapshot_settings())

    def _restore_settings_snapshot(self, snapshot):
        self._restoring_settings = True
        try:
            target_custom = snapshot.get("using_custom_tiles_check")
            if (
                target_custom is not None
                and bool(target_custom)
                != self.using_custom_tiles_check.isChecked()
            ):
                self.using_custom_tiles_check.blockSignals(True)
                self.using_custom_tiles_check.setChecked(bool(target_custom))
                self.using_custom_tiles_check.blockSignals(False)
                self.cfg.autoortho.using_custom_tiles = bool(target_custom)
                if self.phase3_active:
                    self.max_zoom_slider.setMaximum(
                        19 if target_custom else 17
                    )
                else:
                    self.refresh_settings_tab()

            for attr, widget in self._settings_widgets():
                if attr not in snapshot:
                    continue
                value = snapshot[attr]
                was_blocked = widget.blockSignals(True)
                try:
                    if isinstance(widget, QLineEdit):
                        widget.setText(str(value))
                    elif isinstance(widget, QCheckBox):
                        widget.setChecked(bool(value))
                    elif isinstance(widget, QComboBox):
                        widget.setCurrentText(str(value))
                    elif isinstance(
                        widget,
                        (QSlider, QSpinBox, QDoubleSpinBox),
                    ):
                        widget.setValue(value)
                finally:
                    widget.blockSignals(was_blocked)

            if "missing_color" in snapshot and hasattr(
                self,
                "missing_color",
            ):
                self.missing_color = QColor(*snapshot["missing_color"])
                self.update_missing_color_button()
            if "dynamic_zoom_steps" in snapshot and hasattr(
                self,
                "_dynamic_zoom_manager",
            ):
                self._dynamic_zoom_manager.load_from_config(
                    snapshot["dynamic_zoom_steps"]
                )
                self._update_dynamic_summary()
                if self.phase3_active:
                    self.dynamic_zoom_editor.set_manager(
                        self._dynamic_zoom_manager
                    )

            for method_name in (
                "_update_zoom_mode_visibility",
                "_update_time_budget_controls",
                "_update_prefetch_controls",
                "_update_predictive_dds_controls",
                "_update_fallback_extends_control",
                "_update_pipeline_controls",
                "_update_builder_concurrency_labels",
                "_update_buffer_pool_label",
                "_on_time_exclusion_toggled",
                "on_seasons_enabled_toggled",
            ):
                method = getattr(self, method_name, None)
                if method is not None:
                    method()
            self._hook_settings_widgets()
        finally:
            self._restoring_settings = False

    def _update_settings_actions(self, *args):
        transitioning = self.runtime_state in (
            RuntimeState.STARTING,
            RuntimeState.STOPPING,
        )
        self.apply_button.setEnabled(
            self.settings_session.dirty and not transitioning
        )
        self.revert_button.setEnabled(
            self.settings_session.dirty and not transitioning
        )
        self.restart_notice_label.setVisible(
            self._restart_pending
            or (
                self.settings_session.dirty
                and self.settings_session.restart_required
            )
        )

    def _on_tab_changed(self, index):
        if not hasattr(self, "apply_button"):
            return
        page = self.tabs.widget(index)
        show_settings_actions = page not in (
            self.custom_map_widget,
            self.logs_widget,
        )
        self.apply_button.setVisible(show_settings_actions)
        self.revert_button.setVisible(show_settings_actions)

    def on_revert(self):
        if not self.settings_session.dirty:
            return True
        snapshot = self.settings_session.revert()
        self._restore_settings_snapshot(snapshot)
        self.save_config(persist=False, refresh_scenery=False)
        self.update_ui_log_level()
        self.update_file_log_level()
        self._update_settings_actions()
        self.update_status_bar("Pending configuration changes reverted.")
        return True

    def _resolve_pending_settings(self, *, for_start=False):
        if not self.settings_session.dirty:
            return True

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        if for_start:
            dialog.setWindowTitle("Pending Configuration Changes")
            dialog.setText(
                "Apply pending configuration changes before starting?"
            )
            apply_button = dialog.addButton(
                "Apply and Start",
                QMessageBox.ButtonRole.AcceptRole,
            )
            discard_button = dialog.addButton(
                "Start With Saved Settings",
                QMessageBox.ButtonRole.DestructiveRole,
            )
        else:
            dialog.setWindowTitle("Unsaved Configuration Changes")
            dialog.setText("Save pending configuration changes before quitting?")
            apply_button = dialog.addButton(
                "Apply",
                QMessageBox.ButtonRole.AcceptRole,
            )
            discard_button = dialog.addButton(
                "Discard",
                QMessageBox.ButtonRole.DestructiveRole,
            )
        cancel_button = dialog.addButton(
            QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(cancel_button)
        dialog.exec()

        clicked = dialog.clickedButton()
        if clicked is apply_button:
            return bool(self.on_save())
        if clicked is discard_button:
            return bool(self.on_revert())
        return False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._setup_wizard_checked:
            QTimer.singleShot(0, self._maybe_show_setup_wizard)

    def _readiness_values(self):
        values = self._current_configuration_input().__dict__.copy()
        values["storage_safety_margin_gb"] = str(
            self.storage_safety_margin_spin.value()
            if hasattr(self, "storage_safety_margin_spin")
            else getattr(self.cfg.scenery, "storage_safety_margin_gb", 2)
        )
        return values

    def _scenery_choices(self):
        choices = []
        for region in self.dl.regions.values():
            try:
                latest = region.get_latest_release()
                latest.parse()
                title = latest.name
                size_bytes = int(getattr(latest, "totalsize", 0) or 0)
            except Exception:
                title = region.region_id
                size_bytes = 0
            choices.append(
                SceneryChoice(
                    region_id=region.region_id,
                    title=title,
                    installed=region.local_rel is not None,
                    size_bytes=size_bytes,
                )
            )
        return choices

    def _schedule_readiness_checks(self, *args):
        if hasattr(self, "readiness_timer"):
            self.readiness_timer.start()

    def _start_readiness_checks_async(self):
        if self._closing:
            return
        if (
            self.readiness_worker is not None
            and self.readiness_worker.isRunning()
        ):
            return
        values = self._readiness_values()
        choices = self._scenery_choices()
        signature = self._readiness_state_signature(values, choices)
        worker = ServiceWorker(
            lambda cancel_event: self.readiness_service.check(
                values,
                choices,
                cancel_event=cancel_event,
            ),
            self,
        )
        worker.readiness_signature = signature
        self.readiness_worker = worker
        worker.completed.connect(
            lambda result, current=worker: (
                self._on_readiness_service_result(result, current)
            )
        )
        worker.finished.connect(
            lambda current=worker: self._on_readiness_worker_finished(
                current
            )
        )
        for label in self.readiness_labels.values():
            label.setText("◌ Checking…")
        worker.start()

    @staticmethod
    def _readiness_state_signature(values, choices):
        return (
            tuple(
                sorted(
                    (str(key), repr(value))
                    for key, value in values.items()
                )
            ),
            tuple(
                sorted(
                    (choice.region_id, bool(choice.selected))
                    for choice in choices
                )
            ),
        )

    def _on_readiness_service_result(self, result, worker):
        if self._closing:
            return
        if isinstance(result, Exception) or not result.success:
            self._start_requested_after_readiness = False
            return
        self._readiness_signature = worker.readiness_signature
        self._apply_readiness_state(result.value)
        if self._start_requested_after_readiness:
            self._start_requested_after_readiness = False
            QTimer.singleShot(0, self.on_run)

    def _on_readiness_worker_finished(self, worker):
        if self.readiness_worker is worker:
            self.readiness_worker = None
        if self._start_requested_after_readiness:
            QTimer.singleShot(0, self._start_readiness_checks_async)

    def _run_readiness_checks(self):
        """Compatibility synchronous check used by tests and explicit preflight."""
        result = self.readiness_service.check(
            self._readiness_values(),
            self._scenery_choices(),
        )
        if result.success:
            self._readiness_signature = self._readiness_state_signature(
                self._readiness_values(),
                self._scenery_choices(),
            )
            return self._apply_readiness_state(result.value)
        return self.current_readiness

    def _readiness_for_start(self):
        signature = self._readiness_state_signature(
            self._readiness_values(),
            self._scenery_choices(),
        )
        if (
            self.current_readiness is not None
            and self._readiness_signature == signature
        ):
            return self.current_readiness
        self._start_requested_after_readiness = True
        self._start_readiness_checks_async()
        self.update_status_bar("Checking readiness before starting…")
        return None

    def _apply_readiness_state(self, readiness):
        self.current_readiness = readiness
        symbols = {
            ReadinessStatus.PENDING: "○",
            ReadinessStatus.SUCCESS: "✓",
            ReadinessStatus.WARNING: "!",
            ReadinessStatus.ERROR: "×",
        }
        for check in self.current_readiness.checks:
            label = self.readiness_labels.get(check.id)
            button = self.readiness_fix_buttons.get(check.id)
            if label is not None:
                label.setText(
                    f"{symbols[check.status]}  {check.title}: {check.message}"
                )
                label.setProperty(
                    "textRole",
                    {
                        ReadinessStatus.PENDING: "secondary",
                        ReadinessStatus.SUCCESS: "success",
                        ReadinessStatus.WARNING: "warning",
                        ReadinessStatus.ERROR: "error",
                    }[check.status],
                )
                repolish(label)
                label.setToolTip(check.fix_action)
            if button is not None:
                button.setVisible(check.status != ReadinessStatus.SUCCESS)
                button.setToolTip(check.fix_action)
        self._start_storage_scan()
        if self.phase3_active:
            self._update_shell_status()
        return self.current_readiness

    def _start_storage_scan(self):
        if self._closing:
            return
        if (
            self.storage_scan_worker is not None
            and self.storage_scan_worker.isRunning()
        ):
            return
        path = self.cache_dir_edit.text().strip()
        worker = StorageScanWorker(path)
        worker.completed.connect(self._on_storage_scan_completed)
        worker.finished.connect(
            lambda current=worker: self._on_storage_scan_finished(current)
        )
        self.storage_scan_worker = worker
        self.cache_storage_label.setText("Calculating cache usage…")
        worker.start()

    def _on_storage_scan_completed(self, path, usage, free):
        if path != self.cache_dir_edit.text().strip():
            return
        self.cache_storage_label.setText(
            f"Cache usage: {format_bytes(usage)} • "
            f"Free space: {format_bytes(free)}"
        )
        if self.phase3_active:
            self.home_page.set_cache(
                format_bytes(usage),
                detail=f"{format_bytes(free)} free",
            )

    def _on_storage_scan_finished(self, worker):
        if self.storage_scan_worker is worker:
            self.storage_scan_worker = None
        worker.deleteLater()
        if self.cache_dir_edit.text().strip() != worker.cache_path:
            self._start_storage_scan()

    def _fix_readiness(self, check_id):
        if check_id == "setup-xplane":
            self.navigate_to("settings", "Paths & Storage")
            self.browse_folder(self.xplane_path_edit)
        elif check_id == "setup-storage":
            self.navigate_to("settings", "Paths & Storage")
            self.scenery_path_edit.setFocus()
        elif check_id == "setup-scenery":
            self.navigate_to("scenery-library")
        elif check_id == "setup-dependencies":
            check = (
                self.current_readiness.by_id(check_id)
                if self.current_readiness
                else None
            )
            QMessageBox.information(
                self,
                "FUSE Dependency Required",
                check.fix_action if check else "Install the required FUSE backend.",
            )

    def _maybe_show_setup_wizard(self, force=False):
        self._setup_wizard_checked = True
        setup_complete = bool(
            getattr(self.cfg.general, "setup_complete", False)
        )
        if force:
            self._show_setup_wizard()
            return
        if setup_complete:
            return
        if (
            self.setup_inference_worker is not None
            and self.setup_inference_worker.isRunning()
        ):
            return
        values = self._readiness_values()
        worker = ServiceWorker(
            lambda cancel_event: self.readiness_service.infer_complete(values),
            self,
        )
        self.setup_inference_worker = worker
        worker.completed.connect(self._setup_inference_completed)
        worker.finished.connect(
            lambda current=worker: self._setup_inference_finished(current)
        )
        worker.start()

    def _setup_inference_completed(self, result):
        if isinstance(result, Exception) or not result.success:
            self._show_setup_wizard()
            return
        if result.value:
            self.cfg.general.setup_complete = True
            if not self._persist_configuration(notify=True):
                self.cfg.general.setup_complete = False
            return
        self._show_setup_wizard()

    def _setup_inference_finished(self, worker):
        if self.setup_inference_worker is worker:
            self.setup_inference_worker = None

    def _show_setup_wizard(self):
        wizard = SetupWizard(
            initial_values=self._readiness_values(),
            scenery_choices=self._scenery_choices(),
            parent=self,
        )
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return

        selected_paths = wizard.get_selected_paths()
        self._restoring_settings = True
        try:
            self.xplane_path_edit.setText(selected_paths["xplane_path"])
            self.scenery_path_edit.setText(selected_paths["scenery_path"])
            self.cache_dir_edit.setText(selected_paths["cache_dir"])
            self.lt_cache_dir_edit.setText(
                selected_paths["long_term_cache_dir"]
            )
            self.download_dir_edit.setText(selected_paths["download_dir"])
        finally:
            self._restoring_settings = False
        self.settings_session.observe(self._snapshot_settings())
        self.cfg.general.setup_complete = True
        if not self.on_save():
            self.cfg.general.setup_complete = False
            return

        self._run_readiness_checks()
        for region_id in wizard.get_selected_region_ids():
            region = self.dl.regions.get(region_id)
            if region is not None and region.local_rel is None:
                QTimer.singleShot(
                    0,
                    lambda rid=region_id: self.on_install_scenery(
                        rid,
                        skip_confirmation=True,
                    ),
                )

    def create_setup_tab(self):
        """Create the setup configuration tab"""
        setup_widget = QWidget()
        self.setup_widget = setup_widget

        # Create scroll area for setup content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        # Create the actual content widget
        setup_content = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(15)
        setup_content.setLayout(layout)

        self.readiness_group = QGroupBox("System Readiness")
        readiness_layout = QVBoxLayout(self.readiness_group)
        self.readiness_labels = {}
        self.readiness_fix_buttons = {}
        for check_id in (
            "setup-xplane",
            "setup-storage",
            "setup-dependencies",
            "setup-scenery",
        ):
            row = QHBoxLayout()
            label = QLabel("Pending…")
            label.setWordWrap(True)
            fix_button = StyledButton("Fix")
            fix_button.clicked.connect(
                lambda checked=False, cid=check_id: self._fix_readiness(cid)
            )
            row.addWidget(label, 1)
            row.addWidget(fix_button)
            readiness_layout.addLayout(row)
            self.readiness_labels[check_id] = label
            self.readiness_fix_buttons[check_id] = fix_button
        self.cache_storage_label = QLabel("")
        self.cache_storage_label.setStyleSheet(
            "color: #aaa; font-size: 11px;"
        )
        readiness_layout.addWidget(self.cache_storage_label)
        readiness_actions = QHBoxLayout()
        recheck_button = StyledButton("Run Checks")
        recheck_button.clicked.connect(self._start_readiness_checks_async)
        wizard_button = StyledButton("Setup Wizard…")
        wizard_button.clicked.connect(
            lambda: self._maybe_show_setup_wizard(force=True)
        )
        readiness_actions.addWidget(recheck_button)
        readiness_actions.addWidget(wizard_button)
        readiness_actions.addStretch()
        readiness_layout.addLayout(readiness_actions)
        layout.addWidget(self.readiness_group)

        # Paths group
        paths_group = QGroupBox("Paths Configuration")
        self.paths_group = paths_group
        self.path_browse_buttons = []
        paths_layout = QVBoxLayout()
        paths_group.setLayout(paths_layout)

        # Scenery path
        scenery_layout = QHBoxLayout()
        scenery_label = QLabel("Scenery Install Folder:") # Changed from "Custom Scenery folder:"
        scenery_label.setToolTip(
            "Directory where AutoOrtho scenery will be installed.\n"
            "This should be a your X-Plane Custom Scenery folder or another location."
        )
        scenery_layout.addWidget(scenery_label)
        self.scenery_path_edit = QLineEdit(self.cfg.paths.scenery_path)
        self.scenery_path_edit.setObjectName('scenery_path')
        self.scenery_path_edit.setToolTip(
            "Full path to your AutoOrtho scenery installation directory"
        )
        scenery_layout.addWidget(self.scenery_path_edit)
        browse_btn = StyledButton("Browse")
        self.path_browse_buttons.append(browse_btn)
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.scenery_path_edit)
        )
        scenery_layout.addWidget(browse_btn)
        paths_layout.addLayout(scenery_layout)

        # X-Plane path
        xplane_layout = QHBoxLayout()
        xplane_label = QLabel("X-Plane install dir:")
        xplane_label.setToolTip(
            "Your main X-Plane installation directory.\n"
            "This should contain the X-Plane.exe file and\n"
            "the 'Custom Scenery' folder.\n"
            "Example: C:\\X-Plane 12\\ or /Applications/X-Plane 12/"
        )
        xplane_layout.addWidget(xplane_label)
        self.xplane_path_edit = QLineEdit(self.cfg.paths.xplane_path)
        self.xplane_path_edit.setObjectName('xplane_path')
        self.xplane_path_edit.setToolTip(
            "Full path to your X-Plane installation directory"
        )
        xplane_layout.addWidget(self.xplane_path_edit)
        browse_btn = StyledButton("Browse")
        self.path_browse_buttons.append(browse_btn)
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.xplane_path_edit)
        )
        xplane_layout.addWidget(browse_btn)
        paths_layout.addLayout(xplane_layout)

        # Cache dir
        cache_layout = QHBoxLayout()
        cache_label = QLabel("Image cache dir:")
        cache_label.setToolTip(
            "Directory for caching downloaded imagery.\n"
            "Should be on a fast drive (SSD recommended) with plenty of "
            "space.\n"
            "Cache helps reduce download times for frequently visited "
            "areas.\n"
            "Optimal: SSD with 50-500GB available space"
        )
        cache_layout.addWidget(cache_label)
        self.cache_dir_edit = QLineEdit(self.cfg.paths.cache_dir)
        self.cache_dir_edit.setObjectName('cache_dir')
        self.cache_dir_edit.setToolTip(
            "Full path to your image cache directory"
        )
        cache_layout.addWidget(self.cache_dir_edit)
        browse_btn = StyledButton("Browse")
        self.path_browse_buttons.append(browse_btn)
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.cache_dir_edit)
        )
        cache_layout.addWidget(browse_btn)
        paths_layout.addLayout(cache_layout)

        # Long-term cache dir
        lt_cache_layout = QHBoxLayout()
        lt_cache_label = QLabel("Long-term cache dir:")
        lt_cache_label.setToolTip(
            "Optional permanent cache directory (e.g. large slow disk or\n"
            "network share). Leave empty to disable.\n"
            "Chunks are stored here permanently and promoted to local\n"
            "cache on access, avoiding re-downloads across reinstalls."
        )
        lt_cache_layout.addWidget(lt_cache_label)
        self.lt_cache_dir_edit = QLineEdit(
            str(getattr(self.cfg.paths, 'long_term_cache_dir', '') or '')
        )
        self.lt_cache_dir_edit.setObjectName('long_term_cache_dir')
        self.lt_cache_dir_edit.setPlaceholderText("Optional — leave empty to disable")
        self.lt_cache_dir_edit.setToolTip(
            "Full path to long-term cache directory"
        )
        lt_cache_layout.addWidget(self.lt_cache_dir_edit)
        browse_btn = StyledButton("Browse")
        self.path_browse_buttons.append(browse_btn)
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.lt_cache_dir_edit)
        )
        lt_cache_layout.addWidget(browse_btn)
        paths_layout.addLayout(lt_cache_layout)

        # Download dir
        download_layout = QHBoxLayout()
        download_label = QLabel("Temp download dir:")
        download_label.setToolTip(
            "Temporary directory for downloading scenery packages.\n"
            "Should have enough space for large scenery downloads "
            "(10-50GB).\n"
            "Files are deleted after successful installation.\n"
            "Can be on any drive with sufficient free space."
        )
        download_layout.addWidget(download_label)
        self.download_dir_edit = QLineEdit(self.cfg.paths.download_dir)
        self.download_dir_edit.setObjectName('download_dir')
        self.download_dir_edit.setToolTip(
            "Full path to temporary download directory"
        )
        download_layout.addWidget(self.download_dir_edit)
        browse_btn = StyledButton("Browse")
        self.path_browse_buttons.append(browse_btn)
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.download_dir_edit)
        )
        download_layout.addWidget(browse_btn)
        paths_layout.addLayout(download_layout)

        self.setup_validation_label = QLabel("")
        self.setup_validation_label.setWordWrap(True)
        self.setup_validation_label.setStyleSheet(
            "color: #ff8a80; padding: 8px; background: #3a2a2a; "
            "border-radius: 4px;"
        )
        self.setup_validation_label.hide()
        paths_layout.addWidget(self.setup_validation_label)

        layout.addWidget(paths_group)

        # Options group
        options_group = QGroupBox("Basic Settings")
        self.options_group = options_group
        options_layout = QVBoxLayout()
        options_group.setLayout(options_layout)

        self.showconfig_check = QCheckBox("Always show config menu")
        self.showconfig_check.setChecked(self.cfg.general.showconfig)
        self.showconfig_check.setObjectName('showconfig')
        self.showconfig_check.setToolTip(
            "If enabled, the configuration window will always appear on "
            "startup.\n"
            "If disabled, AutoOrtho will start directly without showing "
            "this window.\n"
            "Recommended: Enabled until you're satisfied with your "
            "configuration."
        )
        options_layout.addWidget(self.showconfig_check)

        # Map type
        maptype_layout = QHBoxLayout()
        maptype_label = QLabel("&Imagery source:")
        maptype_label.setToolTip(
            "Force AutoOrtho to use a specific imagery source:\n"
            "• Use tile default: Use source based on the tile default. For example display ARC if using custom ARC tiles.\n"
            "• BI (Bing): High quality, good worldwide coverage\n"
            "• NAIP: Very high quality for USA only\n"
            "• EOX: Good for Europe and some other regions\n"
            "• USGS: USA government imagery\n"
            "• Firefly: Alternative commercial source\n"
            "• GO2: Google Maps\n"
            "• ARC: ArcGIS\n"
            "• YNDX: Yandex Maps\n"
            "• APPLE: Apple Maps"
        )
        maptype_layout.addWidget(maptype_label)
        self.maptype_combo = QComboBox()
        self.maptype_combo.installEventFilter(self)
        self.maptype_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.maptype_combo.addItems(MAPTYPES)
        self.maptype_combo.setCurrentText(self.cfg.autoortho.maptype_override)
        self.maptype_combo.setObjectName('maptype_override')
        maptype_label.setBuddy(self.maptype_combo)
        self.maptype_combo.setToolTip(
            "Select a specific map provider. Use Auto to use the source based on the tile default (base scenery uses BI)."
        )
        maptype_layout.addWidget(self.maptype_combo)

        self.maptype_switch_btn = QPushButton("Switch")
        self.maptype_switch_btn.setToolTip(
            "Apply the new map type to all new tiles without restarting."
        )
        self.maptype_switch_btn.setVisible(False)
        self.maptype_switch_btn.clicked.connect(self._on_maptype_switch)
        maptype_layout.addWidget(self.maptype_switch_btn)

        self.maptype_combo.currentTextChanged.connect(self._on_maptype_combo_changed)

        maptype_layout.addStretch()
        options_layout.addLayout(maptype_layout)

        self.simheaven_compat_check = QCheckBox("SimHeaven compatibility mode")
        self.simheaven_compat_check.setChecked(self.cfg.autoortho.simheaven_compat)
        self.simheaven_compat_check.setObjectName('simheaven_compat')
        self.simheaven_compat_check.setToolTip(
            "Enable this if you are using SimHeaven scenery.\n"
            "This will disable AutoOrtho Overlays to use the SimHeaven "
            "overlay instead. This is done by changing values within scenery_packs.ini.\n"
            "Use with caution, this may cause issues with other scenery packs."
        )
        options_layout.addWidget(self.simheaven_compat_check)


        self.simheaven_compat_check.stateChanged.connect(self.on_simheaven_compat_check)

        # add space between options
        options_layout.addSpacing(10)

        self.using_custom_tiles_check = QCheckBox(
            "Enable custom Ortho4XP tiles"
        )
        self.using_custom_tiles_check.setChecked(self.cfg.autoortho.using_custom_tiles)
        self.using_custom_tiles_check.setObjectName('using_custom_tiles')
        self.using_custom_tiles_check.setToolTip(
            "Enable this if you are using custom build Ortho4XP tiles instead or along with base scenery packages from autoortho.\n"
            "NOTE: By using this option the Max Zoom near airports setting will be ignored and all tiles will be capped to the general max zoom level you set in advanced settings."
        )

        self.using_custom_tiles_check.stateChanged.connect(self.on_using_custom_tiles_check)
        options_layout.addWidget(self.using_custom_tiles_check)


        layout.addWidget(options_group)

        # SimBrief Integration group
        simbrief_group = QGroupBox("SimBrief Integration")
        self.simbrief_group = simbrief_group
        simbrief_layout = QVBoxLayout()
        simbrief_group.setLayout(simbrief_layout)

        # User ID row
        userid_layout = QHBoxLayout()
        userid_label = QLabel("SimBrief User ID:")
        userid_label.setToolTip(
            "Your SimBrief Pilot ID.\n"
            "Find it at: SimBrief → Account Settings → Pilot ID"
        )
        userid_layout.addWidget(userid_label)
        
        self.simbrief_userid_edit = QLineEdit(
            str(getattr(self.cfg.simbrief, 'userid', '')) if hasattr(self.cfg, 'simbrief') else ''
        )
        self.simbrief_userid_edit.setObjectName('simbrief_userid')
        self.simbrief_userid_edit.setPlaceholderText("Enter your SimBrief Pilot ID")
        self.simbrief_userid_edit.setToolTip("Your SimBrief Pilot ID (numeric)")
        self.simbrief_userid_edit.textChanged.connect(self._on_simbrief_userid_changed)
        userid_layout.addWidget(self.simbrief_userid_edit)
        
        simbrief_layout.addLayout(userid_layout)

        # Button row for Fetch and Unload
        simbrief_btn_layout = QHBoxLayout()
        simbrief_btn_layout.setSpacing(10)
        
        # Fetch button (only visible when userid is set)
        self.simbrief_fetch_btn = StyledButton("Fetch Flight Data")
        self.simbrief_fetch_btn.setToolTip("Fetch the latest flight plan from SimBrief")
        self.simbrief_fetch_btn.clicked.connect(self._on_simbrief_fetch)
        simbrief_btn_layout.addWidget(self.simbrief_fetch_btn)
        
        # Unload button (only visible when flight data is loaded)
        self.simbrief_unload_btn = StyledButton("Unload Flight")
        self.simbrief_unload_btn.setToolTip("Clear the loaded flight plan data")
        self.simbrief_unload_btn.clicked.connect(self._on_simbrief_unload)
        self.simbrief_unload_btn.hide()  # Hidden until flight is loaded
        simbrief_btn_layout.addWidget(self.simbrief_unload_btn)
        
        simbrief_btn_layout.addStretch()
        simbrief_layout.addLayout(simbrief_btn_layout)

        # Flight info display area (hidden initially)
        self.simbrief_info_frame = QFrame()
        self.simbrief_info_frame.setStyleSheet("""
            QFrame {
                background-color: #2A2A2A;
                border: 1px solid #3A3A3A;
                border-radius: 6px;
                padding: 10px;
            }
        """)
        simbrief_info_layout = QVBoxLayout()
        simbrief_info_layout.setSpacing(8)
        self.simbrief_info_frame.setLayout(simbrief_info_layout)

        # Flight route header
        self.simbrief_route_label = QLabel("")
        self.simbrief_route_label.setStyleSheet("""
            QLabel {
                font-size: 16px;
                font-weight: bold;
                color: #6da4e3;
            }
        """)
        simbrief_info_layout.addWidget(self.simbrief_route_label)

        # Flight details grid
        self.simbrief_details_label = QLabel("")
        self.simbrief_details_label.setStyleSheet("""
            QLabel {
                color: #E0E0E0;
                font-size: 13px;
            }
        """)
        self.simbrief_details_label.setWordWrap(True)
        simbrief_info_layout.addWidget(self.simbrief_details_label)

        # Error label (hidden initially)
        self.simbrief_error_label = QLabel("")
        self.simbrief_error_label.setStyleSheet("""
            QLabel {
                color: #ff6b6b;
                font-size: 13px;
                padding: 8px;
                background-color: #3a2a2a;
                border-radius: 4px;
            }
        """)
        self.simbrief_error_label.setWordWrap(True)
        self.simbrief_error_label.hide()
        simbrief_info_layout.addWidget(self.simbrief_error_label)

        # Toggle for using flight data (only visible when flight data is loaded)
        self.simbrief_use_flight_data_check = QCheckBox(
            "Use Flight Data for Dynamic Zoom Level and Pre-fetching Calculations"
        )
        self.simbrief_use_flight_data_check.setToolTip(
            "When enabled, AutoOrtho uses the SimBrief flight plan to:\n\n"
            "• Dynamic Zoom: Uses conservative AGL (Above Ground Level) altitude\n"
            "  to determine the appropriate zoom level for tiles.\n\n"
            "• Pre-fetching: Downloads tiles along your flight path ahead of time,\n"
            "  at the appropriate zoom level for each waypoint's AGL altitude.\n\n"
            "Conservative AGL calculation (when multiple waypoints are nearby):\n"
            "  • Uses LOWEST flight altitude (MSL) - accounts for descent\n"
            "  • Uses HIGHEST ground elevation - accounts for mountains\n"
            "  • AGL = lowest_MSL - highest_ground = most conservative result\n\n"
            "Why AGL? It represents actual height above terrain:\n"
            "  • 10,000ft MSL over 5,000ft mountains = 5,000ft AGL (higher zoom)\n"
            "  • 10,000ft MSL over ocean = 10,000ft AGL (lower zoom)\n\n"
            "If you deviate more than 40nm from the route, AutoOrtho falls back\n"
            "to DataRef-based AGL calculations using X-Plane's y_agl dataref."
        )
        # Load saved value from config
        use_flight_data = False
        if hasattr(self.cfg, 'simbrief'):
            use_flight_data = getattr(self.cfg.simbrief, 'use_flight_data', False)
            if isinstance(use_flight_data, str):
                use_flight_data = use_flight_data.lower() in ('true', '1', 'yes', 'on')
        self.simbrief_use_flight_data_check.setChecked(use_flight_data)
        self.simbrief_use_flight_data_check.setObjectName('simbrief_use_flight_data')
        self.simbrief_use_flight_data_check.hide()  # Hidden until flight data is loaded
        # Connect for immediate effect - allows loading SimBrief data after pressing Run
        self.simbrief_use_flight_data_check.stateChanged.connect(self._on_use_flight_data_changed)
        simbrief_info_layout.addWidget(self.simbrief_use_flight_data_check)

        # Route settings container (visible when flight data is loaded and use_flight_data is checked)
        self.simbrief_route_settings_frame = QFrame()
        self.simbrief_route_settings_frame.setStyleSheet("""
            QFrame {
                background-color: #333333;
                border: 1px solid #454545;
                border-radius: 6px;
                padding: 8px;
                margin-top: 8px;
            }
        """)
        route_settings_layout = QVBoxLayout()
        route_settings_layout.setSpacing(10)
        self.simbrief_route_settings_frame.setLayout(route_settings_layout)

        route_settings_header = QLabel("Route Calculation Settings")
        route_settings_header.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: bold;
                color: #B0B0B0;
                border: none;
                padding: 0px;
                margin-bottom: 4px;
            }
        """)
        route_settings_layout.addWidget(route_settings_header)

        # Route Consideration Radius
        consideration_layout = QHBoxLayout()
        consideration_label = QLabel("Route Consideration Radius:")
        consideration_label.setToolTip(
            "Radius in nautical miles to consider waypoints when calculating\n"
            "the altitude for a tile.\n\n"
            "When determining the zoom level for a tile, AutoOrtho looks at all\n"
            "waypoints within this radius and uses the lowest altitude among them.\n"
            "This ensures that when approaching lower altitude segments (like\n"
            "descent or approach), tiles are fetched at an appropriate zoom level.\n\n"
            "• Larger values: More conservative, fetches higher quality tiles earlier\n"
            "• Smaller values: More accurate to current position, but may need to\n"
            "  re-fetch tiles as you approach lower altitude segments\n\n"
            "Default: 50 nm\n\n"
            "ℹ Changes take effect immediately. Use 'Save Config' to persist\n"
            "the value for future sessions."
        )
        consideration_layout.addWidget(consideration_label)
        
        self.simbrief_consideration_radius_spin = ModernSpinBox()
        self.simbrief_consideration_radius_spin.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.simbrief_consideration_radius_spin.setMinimum(10)
        self.simbrief_consideration_radius_spin.setMaximum(200)
        self.simbrief_consideration_radius_spin.setSuffix(" nm")
        consideration_value = 50
        if hasattr(self.cfg, 'simbrief'):
            consideration_value = int(getattr(self.cfg.simbrief, 'route_consideration_radius_nm', 50))
        self.simbrief_consideration_radius_spin.setValue(consideration_value)
        self.simbrief_consideration_radius_spin.setToolTip(
            "Radius (nm) to search for waypoints when calculating tile altitude"
        )
        self.simbrief_consideration_radius_spin.valueChanged.connect(
            self._on_route_consideration_radius_changed
        )
        consideration_layout.addWidget(self.simbrief_consideration_radius_spin)
        consideration_layout.addStretch()
        route_settings_layout.addLayout(consideration_layout)

        # Route Deviation Threshold
        deviation_layout = QHBoxLayout()
        deviation_label = QLabel("Route Deviation Threshold:")
        deviation_label.setToolTip(
            "Maximum distance in nautical miles the aircraft can deviate from\n"
            "the flight plan before falling back to DataRef-based calculations.\n\n"
            "When you fly off-route (e.g., ATC vectors, weather avoidance, or\n"
            "free flight), the flight plan altitudes may no longer be accurate.\n"
            "If you exceed this distance from the nearest route segment, AutoOrtho\n"
            "will switch to using X-Plane's y_agl DataRef for altitude instead.\n\n"
            "• Larger values: Trust flight plan longer when deviating\n"
            "• Smaller values: Switch to DataRef sooner for accuracy\n\n"
            "Default: 40 nm\n\n"
            "ℹ Changes take effect immediately. Use 'Save Config' to persist\n"
            "the value for future sessions."
        )
        deviation_layout.addWidget(deviation_label)
        
        self.simbrief_deviation_threshold_spin = ModernSpinBox()
        self.simbrief_deviation_threshold_spin.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.simbrief_deviation_threshold_spin.setMinimum(5)
        self.simbrief_deviation_threshold_spin.setMaximum(100)
        self.simbrief_deviation_threshold_spin.setSuffix(" nm")
        deviation_value = 40
        if hasattr(self.cfg, 'simbrief'):
            deviation_value = int(getattr(self.cfg.simbrief, 'route_deviation_threshold_nm', 40))
        self.simbrief_deviation_threshold_spin.setValue(deviation_value)
        self.simbrief_deviation_threshold_spin.setToolTip(
            "Distance (nm) from route before falling back to DataRef altitude"
        )
        self.simbrief_deviation_threshold_spin.valueChanged.connect(
            self._on_route_deviation_threshold_changed
        )
        deviation_layout.addWidget(self.simbrief_deviation_threshold_spin)
        deviation_layout.addStretch()
        route_settings_layout.addLayout(deviation_layout)

        # Prefetch while parked checkbox
        prefetch_parked_layout = QHBoxLayout()
        self.simbrief_prefetch_parked_check = QCheckBox("Prefetch while parked")
        prefetch_parked_value = True
        if hasattr(self.cfg, 'simbrief'):
            val = getattr(self.cfg.simbrief, 'prefetch_while_parked', True)
            if isinstance(val, str):
                prefetch_parked_value = val.lower() in ('true', '1', 'yes', 'on')
            else:
                prefetch_parked_value = bool(val)
        self.simbrief_prefetch_parked_check.setChecked(prefetch_parked_value)
        self.simbrief_prefetch_parked_check.setToolTip(
            "Start prefetching the route immediately when flight plan is loaded.\n\n"
            "When enabled:\n"
            "  • Prefetching starts while parked at the gate\n"
            "  • Uses known route to fetch tiles ahead of time\n"
            "  • Great for loading flight plans before departure\n\n"
            "When disabled:\n"
            "  • Prefetching only starts once airborne and on-route\n"
            "  • Matches velocity-based prefetch behavior"
        )
        self.simbrief_prefetch_parked_check.stateChanged.connect(
            self._on_prefetch_while_parked_changed
        )
        prefetch_parked_layout.addWidget(self.simbrief_prefetch_parked_check)
        prefetch_parked_layout.addStretch()
        route_settings_layout.addLayout(prefetch_parked_layout)
        
        # Route Prefetch Radius note (moved to unified setting in Advanced)
        prefetch_note_layout = QHBoxLayout()
        prefetch_note_label = QLabel(
            "ℹ Prefetch radius is now in Advanced Settings → Prefetching"
        )
        prefetch_note_label.setStyleSheet("color: #8ab4f8; font-style: italic;")
        prefetch_note_label.setToolTip(
            "The prefetch radius setting has been unified for both SimBrief\n"
            "and velocity-based prefetching.\n\n"
            "Go to: Advanced Settings → AutoOrtho → Prefetching → Prefetch radius\n\n"
            "This single setting controls how wide a corridor of tiles is\n"
            "prefetched around your flight path, regardless of whether you're\n"
            "using SimBrief flight plans or simple heading-based prediction."
        )
        prefetch_note_layout.addWidget(prefetch_note_label)
        prefetch_note_layout.addStretch()
        route_settings_layout.addLayout(prefetch_note_layout)

        self.simbrief_route_settings_frame.hide()  # Hidden until use_flight_data is checked
        simbrief_info_layout.addWidget(self.simbrief_route_settings_frame)

        self.simbrief_info_frame.hide()
        simbrief_layout.addWidget(self.simbrief_info_frame)

        # Store the current flight data
        self.simbrief_flight_data = None
        self.simbrief_fetch_worker = None

        # Update initial visibility
        self._update_simbrief_ui_state()

        layout.addWidget(simbrief_group)
    
        # Set the content widget to the scroll area
        scroll_area.setWidget(setup_content)
        layout.addStretch()

        # Create the main layout for the tab
        tab_layout = QVBoxLayout()
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll_area)
        setup_widget.setLayout(tab_layout)

        self.tabs.addTab(setup_widget, "Setup")

    def create_settings_tab(self):
        """Create the advanced settings configuration tab"""
        settings_widget = QWidget()
        self.settings_widget = settings_widget

        # Create scroll area for settings content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        # Create the actual content widget
        settings_content = QWidget()
        self.settings_layout = QVBoxLayout()
        self.settings_layout.setSpacing(15)
        settings_content.setLayout(self.settings_layout)

        self.refresh_settings_tab()
        

        # Set the content widget to the scroll area
        scroll_area.setWidget(settings_content)

        # Create the main layout for the tab
        tab_layout = QVBoxLayout()
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll_area)
        settings_widget.setLayout(tab_layout)

        self.tabs.addTab(settings_widget, "Advanced Settings")

    def create_scenery_tab(self):
        """Create the scenery management tab"""
        scenery_widget = QWidget()
        self.scenery_widget = scenery_widget
        layout = QVBoxLayout()
        scenery_widget.setLayout(layout)

        # Scenery Settings group
        scenery_group = QGroupBox("Scenery Installation Settings")
        self.scenery_settings_group = scenery_group
        scenery_layout = QVBoxLayout()
        scenery_group.setLayout(scenery_layout)

        self.noclean_check = QCheckBox(
            "Keep downloaded installation files"
        )
        self.noclean_check.setChecked(self.cfg.scenery.noclean)
        self.noclean_check.setObjectName('noclean')
        self.noclean_check.setToolTip(
            "Keep downloaded scenery files after installation.\n"
            "Useful for reinstalling or sharing scenery packages.\n"
            "Warning: Can use significant disk space over time.\n"
            "Recommended: Disabled unless you need the original files."
        )
        scenery_layout.addWidget(self.noclean_check)

        # Max download workers
        dl_workers_layout = QHBoxLayout()
        dl_workers_label = QLabel("Parallel download workers:")
        dl_workers_label.setToolTip(
            "Number of files downloaded simultaneously.\n"
            "Higher values saturate your bandwidth faster,\n"
            "reducing total download time for multi-file packages.\n\n"
            "Recommended: 4 (default), 2 (slow connection), 8 (fast connection)"
        )
        dl_workers_layout.addWidget(dl_workers_label)

        self.max_download_workers_spin = ModernSpinBox()
        self.max_download_workers_spin.setFocusPolicy(Qt.StrongFocus)
        self.max_download_workers_spin.setRange(1, 8)
        dl_workers_value = 4
        try:
            dl_workers_value = int(self.cfg.scenery.max_download_workers)
        except (AttributeError, ValueError):
            pass
        self.max_download_workers_spin.setValue(dl_workers_value)
        self.max_download_workers_spin.setObjectName('max_download_workers')
        self.max_download_workers_spin.setToolTip(
            "Number of files downloaded simultaneously.\n"
            "Recommended: 4 (default), 2 (slow connection), 8 (fast connection)"
        )
        dl_workers_layout.addWidget(self.max_download_workers_spin)
        dl_workers_layout.addStretch()
        scenery_layout.addLayout(dl_workers_layout)

        storage_margin_layout = QHBoxLayout()
        storage_margin_label = QLabel("Free-space safety margin:")
        storage_margin_label.setToolTip(
            "Minimum disk space AutoOrtho keeps free when checking scenery "
            "downloads and storage locations."
        )
        self.storage_safety_margin_spin = QDoubleSpinBox()
        self.storage_safety_margin_spin.setRange(0.0, 100.0)
        self.storage_safety_margin_spin.setSingleStep(1.0)
        self.storage_safety_margin_spin.setDecimals(1)
        self.storage_safety_margin_spin.setSuffix(" GB")
        self.storage_safety_margin_spin.setValue(
            float(
                getattr(
                    self.cfg.scenery,
                    "storage_safety_margin_gb",
                    2,
                )
            )
        )
        storage_margin_layout.addWidget(storage_margin_label)
        storage_margin_layout.addWidget(self.storage_safety_margin_spin)
        storage_margin_layout.addStretch()
        scenery_layout.addLayout(storage_margin_layout)

        layout.addWidget(scenery_group)

        # Scenery Tab Seasons Settings group
        scenery_seasons_group = QGroupBox("Seasons Conversion Settings")
        self.scenery_seasons_group = scenery_seasons_group
        scenery_seasons_layout = QVBoxLayout()
        scenery_seasons_group.setLayout(scenery_seasons_layout)

        # Seasons convert workers
        seasons_convert_workers_row = QHBoxLayout()
        seasons_convert_workers_label = QLabel(
            "&Seasons conversion workers:"
        )
        self.seasons_convert_workers_slider = ModernSlider()
        self.seasons_convert_workers_slider.setRange(1, os.cpu_count())
        self.seasons_convert_workers_slider.setValue(int(self.cfg.seasons.seasons_convert_workers))
        self.seasons_convert_workers_slider.setObjectName('seasons_convert_workers')
        seasons_convert_workers_label.setBuddy(
            self.seasons_convert_workers_slider
        )
        self.seasons_convert_workers_slider.setToolTip(
            "Number of workers to use for converting DSF to XP12 native seasons format.\n"
            "More workers = faster conversion but higher CPU and RAM usage.\n"
            "Recommended: 4 and work your way up from there depending on your system."
        )
        self.seasons_convert_workers_value_label = QLabel(f"{self.cfg.seasons.seasons_convert_workers} workers")
        self.seasons_convert_workers_slider.valueChanged.connect(
            lambda v: self.seasons_convert_workers_value_label.setText(f"{v} workers")
        )
        seasons_convert_workers_row.addWidget(seasons_convert_workers_label)
        seasons_convert_workers_row.addWidget(self.seasons_convert_workers_slider)
        seasons_convert_workers_row.addWidget(self.seasons_convert_workers_value_label)
        scenery_seasons_layout.addLayout(seasons_convert_workers_row)

        # Compress DSF
        compress_dsf_row = QHBoxLayout()
        self.compress_dsf_check = QCheckBox("Compress DSF after conversion")
        self.compress_dsf_check.setChecked(self.cfg.seasons.compress_dsf)
        self.compress_dsf_check.setObjectName('compress_dsf')
        self.compress_dsf_check.setToolTip("Compress DSF to 7z format after conversion to XP12 format")
        compress_dsf_row.addWidget(self.compress_dsf_check)
        scenery_seasons_layout.addLayout(compress_dsf_row)

        layout.addWidget(scenery_seasons_group)
        layout.addWidget(QLabel("Remember to Save Config after making any changes to settings."))

        # Create scroll area for scenery list
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.scenery_content = QWidget()
        self.scenery_layout = QVBoxLayout()
        self.scenery_content.setLayout(self.scenery_layout)

        scroll_area.setWidget(self.scenery_content)
        layout.addWidget(scroll_area)

        # Refresh scenery list
        self.refresh_scenery_list()

        self.tabs.addTab(scenery_widget, "Scenery")

    def create_logs_tab(self):
        """Create the logs tab"""
        logs_widget = QWidget()
        self.logs_widget = logs_widget
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        logs_widget.setLayout(layout)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

        self.tabs.addTab(logs_widget, "Logs")
        
        # Set up the UI logging handler now that log_text exists
        self.setup_ui_logging()

    def create_custom_map_tab(self):
        """Create the Custom Map editor tab with a button to open in browser."""
        custom_map_widget = QWidget()
        self.custom_map_widget = custom_map_widget
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        custom_map_widget.setLayout(layout)

        layout.addStretch()

        # Info label
        info_label = QLabel(
            "The Custom Map Editor lets you assign different map sources\n"
            "to individual 1-degree tiles. It opens in your web browser."
        )
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setStyleSheet("font-size: 14px; color: #ccc;")
        layout.addWidget(info_label)

        # Open button
        open_btn = QPushButton("Open Map Editor in Browser")
        open_btn.setStyleSheet(
            "QPushButton { font-size: 16px; font-weight: bold; padding: 12px 24px; }"
        )
        open_btn.setFixedWidth(320)
        open_btn.clicked.connect(self._open_custom_map_in_browser)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(open_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

        # Register config UI ref for worker notifications
        try:
            import flighttrack
            flighttrack.set_config_ui_ref(self)
        except ImportError:
            from autoortho import flighttrack
            flighttrack.set_config_ui_ref(self)

        self.tabs.addTab(custom_map_widget, "Map")

    def _open_custom_map_in_browser(self):
        """Open the custom map editor in the default web browser."""
        if self.phase3_active:
            self.navigate_to("flight-plan-map")
            self.flight_plan_page.check_map_service()
            return
        try:
            import flighttrack
            port = flighttrack.active_port
        except (ImportError, AttributeError):
            port = None
        if not port:
            port = self.cfg.flightdata.webui_port
        webbrowser.open(f"http://localhost:{port}/custommap")

    def setup_ui_logging(self):
        """Set up the UI logging handler with the configured log level"""
        try:
            # Remove existing handler if present
            if hasattr(self, 'ui_log_handler') and self.ui_log_handler:
                logging.getLogger().removeHandler(self.ui_log_handler)
            
            # Create new handler
            self.ui_log_handler = QTextEditLogger(self.log_text)
            
            # Set the console log level from config
            console_level_str = getattr(self.cfg.general, 'console_log_level', 'INFO').upper()
            console_level = getattr(logging, console_level_str, logging.INFO)
            self.ui_log_handler.setLevel(console_level)
            
            # Set formatter
            formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%H:%M:%S'
            )
            self.ui_log_handler.setFormatter(formatter)
            
            # Add to root logger
            logging.getLogger().addHandler(self.ui_log_handler)
            
            # Update root logger level to ensure all messages can flow through
            self._update_root_logger_level()
            
            for message in (
                "=== AutoOrtho Logs ===",
                f"UI Log Level: {console_level_str}",
                f"File Log Level: {getattr(self.cfg.general, 'file_log_level', 'DEBUG').upper()}",
                f"Log file location: {self.cfg.paths.log_file}",
                "",
            ):
                self.ui_log_handler._append_text(message, logging.INFO)
            
            # Log initialization
            log.info(f"UI logging initialized at level: {console_level_str}")
        except Exception as e:
            # Try to display error in the text widget
            try:
                self.log_text.append(f"ERROR: Failed to setup UI logging: {e}")
            except Exception:
                pass
            log.error(f"Failed to setup UI logging: {e}")
    
    def update_ui_log_level(self):
        """Update the UI log handler level when config changes"""
        try:
            if hasattr(self, 'ui_log_handler') and self.ui_log_handler:
                console_level_str = getattr(self.cfg.general, 'console_log_level', 'INFO').upper()
                console_level = getattr(logging, console_level_str, logging.INFO)
                self.ui_log_handler.setLevel(console_level)
                
                # Also update any StreamHandler (terminal console) to match
                root_logger = logging.getLogger()
                for handler in root_logger.handlers:
                    if isinstance(handler, logging.StreamHandler) and not isinstance(handler, QTextEditLogger):
                        handler.setLevel(console_level)
                
                # Update root logger level to minimum of all handlers
                self._update_root_logger_level()
                
                log.info(f"UI log level updated to: {console_level_str}")
        except Exception as e:
            log.error(f"Failed to update UI log level: {e}")
    
    def on_console_log_level_changed(self, new_level):
        """Handle console log level change in UI"""
        try:
            self.cfg.general.console_log_level = new_level
            self.update_ui_log_level()
            log.info(f"Console/UI log level changed to: {new_level}")
        except Exception as e:
            log.error(f"Failed to change console log level: {e}")
    
    def on_file_log_level_changed(self, new_level):
        """Handle file log level change in UI"""
        try:
            self.cfg.general.file_log_level = new_level
            self.update_file_log_level()
            log.info(f"File log level changed to: {new_level}")
        except Exception as e:
            log.error(f"Failed to change file log level: {e}")
    
    def update_file_log_level(self):
        """Update the file log handler level when config changes"""
        try:
            file_level_str = getattr(self.cfg.general, 'file_log_level', 'DEBUG').upper()
            file_level = getattr(logging, file_level_str, logging.DEBUG)
            
            # Find and update the file handler
            root_logger = logging.getLogger()
            for handler in root_logger.handlers:
                # Check if this is a file handler (RotatingFileHandler or FileHandler)
                if isinstance(handler, (logging.handlers.RotatingFileHandler, logging.FileHandler)):
                    handler.setLevel(file_level)
                    log.info(f"File log level updated to: {file_level_str}")
                    break
            
            # Update root logger level to minimum of all handlers
            self._update_root_logger_level()
        except Exception as e:
            log.error(f"Failed to update file log level: {e}")
    
    def _update_root_logger_level(self):
        """Update root logger level to minimum of all active handlers
        
        This ensures that messages at any handler's level can flow through
        the root logger. Individual handlers then filter based on their own levels.
        """
        try:
            root_logger = logging.getLogger()
            
            # Find the minimum level across all handlers
            min_level = logging.CRITICAL  # Start with highest level
            handler_levels = []
            for handler in root_logger.handlers:
                if handler.level < min_level:
                    min_level = handler.level
                handler_name = handler.__class__.__name__
                handler_level_name = logging.getLevelName(handler.level)
                handler_levels.append(f"{handler_name}={handler_level_name}")
            
            # Set root logger to the minimum level so all messages can flow through
            if min_level != root_logger.level:
                old_level = logging.getLevelName(root_logger.level)
                root_logger.setLevel(min_level)
                level_name = logging.getLevelName(min_level)
                log.info(f"Root logger adjusted: {old_level} -> {level_name} (handlers: {', '.join(handler_levels)})")
        except Exception as e:
            log.error(f"Failed to update root logger level: {e}")

    @staticmethod
    def _move_layout_item(item, destination):
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            destination.addWidget(widget)
        elif child_layout is not None:
            destination.addLayout(child_layout)
        else:
            destination.addItem(item)

    def _partition_autoortho_settings(
        self,
        source_layout,
        performance_boundary,
        prefetch_boundary,
        pipeline_boundary,
    ):
        groups = {
            "imagery": QGroupBox("Imagery Quality"),
            "performance": QGroupBox("Performance"),
            "prefetch": QGroupBox("Prefetching"),
            "pipeline": QGroupBox("Compression & Pipeline"),
        }
        layouts = {
            key: QVBoxLayout(group) for key, group in groups.items()
        }
        current = "imagery"
        while source_layout.count():
            item = source_layout.takeAt(0)
            widget = item.widget()
            if widget is performance_boundary:
                current = "performance"
                continue
            if widget is prefetch_boundary:
                current = "prefetch"
            elif widget is pipeline_boundary:
                current = "pipeline"
            self._move_layout_item(item, layouts[current])
        self.imagery_settings_group = groups["imagery"]
        self.performance_settings_group = groups["performance"]
        self.prefetch_settings_group = groups["prefetch"]
        self.pipeline_settings_group = groups["pipeline"]

    def refresh_settings_tab(self):
        """Refresh the settings tab"""
        while self.settings_layout.count():
            child = self.settings_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        cache_group = QGroupBox("Cache Settings")
        self.cache_settings_group = cache_group
        cache_layout = QVBoxLayout()
        cache_group.setLayout(cache_layout)

        # Memory cache limit
        mem_cache_layout = QHBoxLayout()
        mem_cache_label = QLabel("Memory cache (GB):")
        mem_cache_label.setToolTip(
            "Maximum RAM used for caching images in memory.\n"
            "Higher values improve performance but use more RAM.\n"
            "Optimal: 4-16GB depending on your system RAM.\n"
            "Don't exceed 25% of your total system RAM."
        )
        mem_cache_layout.addWidget(mem_cache_label)
        self.mem_cache_slider = ModernSlider()
        self.mem_cache_slider.setRange(2, 64)
        self.mem_cache_slider.setValue(
            int(float(self.cfg.cache.cache_mem_limit))
        )
        self.mem_cache_slider.setObjectName('cache_mem_limit')
        self.mem_cache_slider.setToolTip(
            "Drag to adjust maximum memory cache size in gigabytes"
        )
        self.mem_cache_label = QLabel(f"{self.cfg.cache.cache_mem_limit} GB")
        self.mem_cache_slider.valueChanged.connect(
            lambda v: self.mem_cache_label.setText(f"{v} GB")
        )
        mem_cache_layout.addWidget(self.mem_cache_slider)
        mem_cache_layout.addWidget(self.mem_cache_label)
        cache_layout.addLayout(mem_cache_layout)

        # File cache size
        file_cache_layout = QHBoxLayout()
        file_cache_label = QLabel("File cache clean limit (GB):")
        file_cache_label.setToolTip(
            "This is the total size of the imagery files that the cache\n"
            "clean operation leaves in the file cache after cleaning.\n"
            "Note that this cache grows without bounds while AutoOrtho is running.\n"
            "Use the Clean Cache button to reduce the cache to this size.\n"
        )
        file_cache_layout.addWidget(file_cache_label)
        self.file_cache_slider = ModernSlider()
        self.file_cache_slider.setRange(0, 500)
        self.file_cache_slider.setSingleStep(5)
        self.file_cache_slider.setValue(
            int(float(self.cfg.cache.file_cache_size))
        )
        self.file_cache_slider.setObjectName('file_cache_size')
        self.file_cache_slider.setToolTip(
            "Drag to adjust the cache clean limit in gigabytes"
        )
        self.file_cache_label = QLabel(f"{self.cfg.cache.file_cache_size} GB")
        self.file_cache_slider.valueChanged.connect(
            lambda v: self.file_cache_label.setText(f"{v} GB")
        )
        file_cache_layout.addWidget(self.file_cache_slider)
        file_cache_layout.addWidget(self.file_cache_label)
        cache_layout.addLayout(file_cache_layout)

        # Auto clean checkbox row
        auto_clean_layout = QHBoxLayout()
        self.auto_clean_cache_check = QCheckBox("Auto clean file cache on AutoOrtho exit")
        self.auto_clean_cache_check.setChecked(self.cfg.cache.auto_clean_cache)
        self.auto_clean_cache_check.setObjectName('auto_clean_cache')
        self.auto_clean_cache_check.setToolTip(
            "Automatically clean cache when AutoOrtho exits.\n"
            "Note that this can take a long time."
        )
        auto_clean_layout.addWidget(self.auto_clean_cache_check)
        auto_clean_layout.addStretch()
        cache_layout.addLayout(auto_clean_layout)

        # Cache action buttons row
        cache_buttons_layout = QHBoxLayout()
        cache_buttons_layout.setSpacing(10)
        self.clean_cache_btn = StyledButton("Clean Cache")
        self.clean_cache_btn.clicked.connect(self.on_clean_cache)
        self.clean_cache_btn.setToolTip(
            "Delete cache files until the file cache clean limit is reached.\n"
            "This will delete the oldest cached images first.\n"
            "If the cache is smaller than the clean limit, no files are deleted.\n"
            "Note that this can take a long time."
        )
        cache_buttons_layout.addWidget(self.clean_cache_btn)
        self.clean_jpegs_btn = StyledButton("Clean JPEG Files")
        self.clean_jpegs_btn.clicked.connect(self.on_clean_jpegs)
        self.clean_jpegs_btn.setToolTip(
            "Delete only JPEG files from the cache.\n"
            "DDS cache files are not affected.\n"
            "Use this to free up space from source images."
        )
        cache_buttons_layout.addWidget(self.clean_jpegs_btn)
        self.delete_cache_btn = StyledButton("Delete Cache")
        self.delete_cache_btn.clicked.connect(self.on_delete_cache)
        self.delete_cache_btn.setToolTip(
            "Delete all cache files.\n"
            "This should be faster than cleaning with a non-zero limit."
        )
        cache_buttons_layout.addWidget(self.delete_cache_btn)
        cache_buttons_layout.addStretch()
        cache_layout.addLayout(cache_buttons_layout)

        self.settings_layout.addWidget(cache_group)

        # AutoOrtho Settings group
        autoortho_group = QGroupBox("AutoOrtho Settings")
        self.autoortho_settings_group = autoortho_group
        autoortho_layout = QVBoxLayout()
        autoortho_group.setLayout(autoortho_layout)

        # Min zoom level
        min_zoom_layout = QHBoxLayout()
        min_zoom_label = QLabel("Minimum zoom level:")
        min_zoom_label.setToolTip(
            "Minimum detail level for imagery downloads.\n"
            "Higher values = program will attempt to always download higher quality imagery, but may miss some tiles.\n"
            "Lower values = program will download fallback to lower quality imagery, but will not miss any tiles.\n"
            "Optimal: 12 for most users since will always attempt to at least get an image at this level."
        )
        min_zoom_layout.addWidget(min_zoom_label)
        self.min_zoom_slider = ModernSlider()
        self.min_zoom_slider.setRange(12, 18)
        self.min_zoom_slider.setValue(int(self.cfg.autoortho.min_zoom))
        self.min_zoom_slider.setObjectName('min_zoom')
        self.min_zoom_slider.setToolTip(
            "Drag to adjust minimum zoom level (12=low detail, 18=high detail)"
        )
        self.min_zoom_label = QLabel(f"{self.cfg.autoortho.min_zoom}")
        self.min_zoom_slider.valueChanged.connect(
            lambda v: (
                self.validate_min_and_max_zoom("min")
            )
        )
        min_zoom_layout.addWidget(self.min_zoom_slider)
        min_zoom_layout.addWidget(self.min_zoom_label)
        autoortho_layout.addLayout(min_zoom_layout)

        # === Max Zoom Mode Toggle ===
        # Allows switching between Fixed (single value) and Dynamic (altitude-based)
        max_zoom_mode_layout = QHBoxLayout()
        max_zoom_mode_label = QLabel("Maximum &detail mode:")
        max_zoom_mode_label.setToolTip(
            "Fixed: Use a single max zoom level for all tiles.\n"
            "Dynamic: Use different zoom levels based on aircraft altitude."
        )
        max_zoom_mode_layout.addWidget(max_zoom_mode_label)
        
        self.max_zoom_mode_combo = QComboBox()
        self.max_zoom_mode_combo.installEventFilter(self)
        self.max_zoom_mode_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.max_zoom_mode_combo.addItems(["Fixed", "Dynamic"])
        current_mode = str(getattr(self.cfg.autoortho, 'max_zoom_mode', 'fixed')).lower()
        self.max_zoom_mode_combo.setCurrentText("Dynamic" if current_mode == "dynamic" else "Fixed")
        self.max_zoom_mode_combo.setToolTip(
            "Fixed: Single max zoom for all tiles (simpler)\n"
            "Dynamic: Altitude-based zoom levels (saves VRAM at altitude)"
        )
        max_zoom_mode_label.setBuddy(self.max_zoom_mode_combo)
        self.max_zoom_mode_combo.currentTextChanged.connect(self._on_zoom_mode_changed)
        max_zoom_mode_layout.addWidget(self.max_zoom_mode_combo)
        max_zoom_mode_layout.addStretch()
        autoortho_layout.addLayout(max_zoom_mode_layout)

        # === Fixed Mode Controls (wrapped in widget for show/hide) ===
        self.fixed_zoom_widget = QWidget()
        fixed_zoom_layout = QVBoxLayout(self.fixed_zoom_widget)
        fixed_zoom_layout.setContentsMargins(0, 0, 0, 0)
        
        max_zoom_tooltip = (
            "Maximum zoom level for imagery downloads.\n"
            "Higher values = more detail but larger downloads and more VRAM usage.\n"
            "Optimal: 16 for most cases. Keep in mind that every extra ZL increases VRAM and potential network usage by 4x.\n"
        )
        if self.cfg.autoortho.using_custom_tiles:
            max_zoom_tooltip += "IMPORTANT: You are using custom tiles, you can set this to 19 if your tiles are built for higher ZL it.\n"
            "But be aware that in-game zoom level will be capped to tile default zoom level + 1 (only X-Plane 12)."

        fixed_max_zoom_row = QHBoxLayout()
        max_zoom_label = QLabel("Maximum zoom level:")
        max_zoom_label.setToolTip(max_zoom_tooltip)
        fixed_max_zoom_row.addWidget(max_zoom_label)
        self.max_zoom_slider = ModernSlider()
        self.max_zoom_slider.setRange(12, 17 if not self.cfg.autoortho.using_custom_tiles else 19)
        self.max_zoom_slider.setValue(int(self.cfg.autoortho.max_zoom))
        self.max_zoom_slider.setObjectName('max_zoom')
        self.max_zoom_slider.setToolTip(
            "Drag to adjust maximum zoom level (12=low detail, 17=high detail)"
        )
        self.max_zoom_label = QLabel(f"{self.cfg.autoortho.max_zoom}")
        self.max_zoom_slider.valueChanged.connect(
            lambda v: (
                self.validate_min_and_max_zoom("max"),
                self._update_buffer_pool_label()
            )
        )
        fixed_max_zoom_row.addWidget(self.max_zoom_slider)
        fixed_max_zoom_row.addWidget(self.max_zoom_label)
        fixed_zoom_layout.addLayout(fixed_max_zoom_row)
        
        autoortho_layout.addWidget(self.fixed_zoom_widget)

        # === Dynamic Mode Controls (wrapped in widget for show/hide) ===
        self.dynamic_zoom_widget = QWidget()
        dynamic_zoom_layout = QVBoxLayout(self.dynamic_zoom_widget)
        dynamic_zoom_layout.setContentsMargins(0, 0, 0, 0)
        
        dynamic_info = QLabel(
            "Dynamic zoom adjusts detail based on altitude. "
            "Higher = less detail (saves VRAM and makes scenery loading faster at cruise)."
        )
        dynamic_info.setStyleSheet("color: #888; font-size: 11px;")
        dynamic_info.setWordWrap(True)
        dynamic_zoom_layout.addWidget(dynamic_info)
        
        dynamic_btn_row = QHBoxLayout()
        self.dynamic_zoom_btn = StyledButton("Configure Quality Steps...")
        self.dynamic_zoom_btn.clicked.connect(self._open_quality_steps_dialog)
        dynamic_btn_row.addWidget(self.dynamic_zoom_btn)
        dynamic_btn_row.addStretch()
        dynamic_zoom_layout.addLayout(dynamic_btn_row)
        
        self.dynamic_zoom_summary = QLabel("No steps configured")
        self.dynamic_zoom_summary.setStyleSheet("color: #6da4e3; padding-left: 5px;")
        dynamic_zoom_layout.addWidget(self.dynamic_zoom_summary)
        
        autoortho_layout.addWidget(self.dynamic_zoom_widget)
        
        # Initialize dynamic zoom manager (visibility updated after all widgets created)
        self._init_dynamic_zoom_manager()

        # Max zoom near airports (wrapped in widget for show/hide with fixed mode)
        self.max_zoom_near_airports_widget = QWidget()
        max_zoom_near_airports_layout = QHBoxLayout(self.max_zoom_near_airports_widget)
        max_zoom_near_airports_layout.setContentsMargins(0, 0, 0, 0)
        max_zoom_near_airports_label = QLabel("Max zoom near airports:")
        max_zoom_near_airports_label.setToolTip(
            "Maximum zoom level to allow near airports. Zoom level around airports used by default is 18."
        )
        max_zoom_near_airports_layout.addWidget(max_zoom_near_airports_label)
        self.max_zoom_near_airports_slider = ModernSlider()
        self.max_zoom_near_airports_slider.setRange(12, 19) # Max X-Plane allows is tile zoom + 1 , 19 accounts for kubilus mesh near airports
        self.max_zoom_near_airports_slider.setValue(int(self.cfg.autoortho.max_zoom_near_airports))
        self.max_zoom_near_airports_slider.setObjectName('max_zoom_near_airports')
        self.max_zoom_near_airports_slider.setToolTip(
            "Drag to adjust maximum zoom level to allow near airports"
        )
        self.max_zoom_near_airports_label = QLabel(f"{self.cfg.autoortho.max_zoom_near_airports}")
        self.max_zoom_near_airports_slider.valueChanged.connect(
            lambda v: (
                self.max_zoom_near_airports_label.setText(f"{v}"),
                self.validate_max_zoom_near_airports(),
                self._update_buffer_pool_label()
            )
        )
        max_zoom_near_airports_layout.addWidget(self.max_zoom_near_airports_slider)
        max_zoom_near_airports_layout.addWidget(self.max_zoom_near_airports_label)

        # Always add to layout (to prevent orphan window popup), visibility controlled separately
        autoortho_layout.addWidget(self.max_zoom_near_airports_widget)

        # Now update visibility after all zoom widgets are created
        self._update_zoom_mode_visibility()

        # Performance Tuning Section
        # Separator line for visual grouping
        perf_separator = QFrame()
        perf_separator.setFrameShape(QFrame.Shape.HLine)
        perf_separator.setFrameShadow(QFrame.Shadow.Sunken)
        perf_separator.setStyleSheet("background-color: #555; margin: 10px 0;")
        autoortho_layout.addWidget(perf_separator)

        perf_header = QLabel("Performance Tuning")
        perf_header.setStyleSheet("font-weight: bold; font-size: 14px; color: #6da4e3; margin-bottom: 5px;")
        autoortho_layout.addWidget(perf_header)

        # Use Time Budget checkbox
        time_budget_layout = QHBoxLayout()
        self.use_time_budget_check = QCheckBox("Use time budget system (recommended)")
        self.use_time_budget_check.setChecked(self.cfg.autoortho.use_time_budget)
        self.use_time_budget_check.setObjectName('use_time_budget')
        self.use_time_budget_check.setToolTip(
            "When enabled, enforces a strict wall-clock time limit for tile requests.\n"
            "This provides more predictable performance and reduces stuttering.\n\n"
            "When disabled, falls back to legacy per-chunk maxwait behavior,\n"
            "which can result in longer cumulative wait times.\n\n"
            "Recommended: Enabled for most users."
        )
        self.use_time_budget_check.stateChanged.connect(self._update_time_budget_controls)
        time_budget_layout.addWidget(self.use_time_budget_check)
        autoortho_layout.addLayout(time_budget_layout)

        # Tile time budget slider
        tile_budget_layout = QHBoxLayout()
        self.tile_budget_label_title = QLabel("Tile time budget (seconds):")
        self.tile_budget_label_title.setToolTip(
            "Maximum wall-clock time for a COMPLETE tile (all mipmaps combined).\n"
            "When this time is reached, the tile is built with whatever has been downloaded.\n\n"
            "This measures ACTIVE PROCESSING TIME only - queue wait time doesn't count.\n"
            "The budget starts when chunks actually begin downloading, not when the\n"
            "tile is first requested. This ensures fair time allocation.\n\n"
            "Lower values = faster loading, but may have more missing/blurry areas\n"
            "Higher values = better quality, but longer initial load times\n\n"
            "Recommended values:\n"
            "  • 60.0 - Fast (quicker loading, more partial tiles, do not use along high zoom levels)\n"
            "  • 120.0 - Balanced (good for most users)\n"
            "  • 300.0 - Quality (for fast networks, slower loading, use along high zoom levels, but beware of stuttering and loading times)"
        )
        tile_budget_layout.addWidget(self.tile_budget_label_title)
        self.tile_budget_slider = ModernSlider()
        # Range: 1 to 300 seconds, with 1 second precision
        # Each tile has 256 chunks (16x16), so adequate time is needed for full quality
        self.tile_budget_slider.setRange(60, 600)  # 60 to 600 seconds in 1 second increments
        self.tile_budget_slider.setSingleStep(1)
        tile_budget_value = int(float(self.cfg.autoortho.tile_time_budget))
        tile_budget_value = max(60, min(600, tile_budget_value))  # Clamp to valid range
        self.tile_budget_slider.setValue(tile_budget_value)
        self.tile_budget_slider.setObjectName('tile_time_budget')
        self.tile_budget_slider.setToolTip(
            "Drag to adjust tile time budget (60-600.0 seconds)"
        )
        self.tile_budget_value_label = QLabel(f"{float(self.cfg.autoortho.tile_time_budget):.1f}")
        self.tile_budget_slider.valueChanged.connect(
            lambda v: self.tile_budget_value_label.setText(f"{v:.1f}")
        )
        tile_budget_layout.addWidget(self.tile_budget_slider)
        tile_budget_layout.addWidget(self.tile_budget_value_label)
        autoortho_layout.addLayout(tile_budget_layout)

        # Per-chunk max wait time (moved from general settings to performance)
        maxwait_layout = QHBoxLayout()
        self.maxwait_label_title = QLabel("Per-chunk max wait (seconds):")
        self.maxwait_label_title.setToolTip(
            "Maximum time to wait for a SINGLE chunk to download.\n\n"
            "This is separate from the tile time budget:\n"
            "  • Tile Budget: Total time for the entire tile (all chunks)\n"
            "  • Per-chunk Max Wait: Timeout for each individual chunk download\n\n"
            "A chunk will stop waiting when EITHER limit is reached.\n"
            "This prevents a single slow chunk from consuming the entire tile budget.\n\n"
            "Lower values = faster timeout per chunk, more fallback usage\n"
            "Higher values = more patience per chunk, better for slow networks\n\n"
            "Recommended values:\n"
            "  • 2.0 - Fast networks\n"
            "  • 5.0 - Normal networks (default)\n"
            "  • 10.0 - Slow/unreliable networks"
        )
        maxwait_layout.addWidget(self.maxwait_label_title)
        self.maxwait_slider = ModernSlider()
        self.maxwait_slider.setRange(1, 100)  # 0.1 to 10.0 seconds
        self.maxwait_slider.setSingleStep(1)
        # Convert maxwait to int for slider (multiply by 10 for 0.1 precision)
        maxwait_value = int(float(self.cfg.autoortho.maxwait) * 10)
        maxwait_value = max(1, min(100, maxwait_value))
        self.maxwait_slider.setValue(maxwait_value)
        self.maxwait_slider.setObjectName('maxwait')
        self.maxwait_slider.setToolTip(
            "Drag to adjust per-chunk max wait time (0.1-10.0 seconds)"
        )
        self.maxwait_value_label = QLabel(f"{float(self.cfg.autoortho.maxwait):.1f}")
        self.maxwait_slider.valueChanged.connect(
            lambda v: self.maxwait_value_label.setText(f"{v/10:.1f}")
        )
        maxwait_layout.addWidget(self.maxwait_slider)
        maxwait_layout.addWidget(self.maxwait_value_label)
        autoortho_layout.addLayout(maxwait_layout)

        # Extended loading time during startup
        startup_loading_layout = QHBoxLayout()
        self.suspend_maxwait_check = QCheckBox("Allow extra loading time during startup")
        self.suspend_maxwait_check.setChecked(self.cfg.autoortho.suspend_maxwait)
        self.suspend_maxwait_check.setObjectName('suspend_maxwait')
        self.suspend_maxwait_check.setToolTip(
            "Allow more time for tiles to load while X-Plane is loading scenery\n"
            "before the flight starts.\n\n"
            "When enabled:\n"
            "  • With Time Budget: Uses 10x the tile time budget during startup\n"
            "  • With Max Wait: Uses 20 seconds per chunk during startup\n\n"
            "Benefits:\n"
            "  • Reduces low-resolution and missing tiles at flight start\n"
            "  • Better initial scenery quality\n\n"
            "Trade-off:\n"
            "  • May increase initial scenery loading time\n\n"
            "Recommended: Enabled for best startup quality."
        )
        startup_loading_layout.addWidget(self.suspend_maxwait_check)
        autoortho_layout.addLayout(startup_loading_layout)

        # Fallback level dropdown
        fallback_layout = QHBoxLayout()
        fallback_label = QLabel("Fallback behavior:")
        fallback_label.setToolTip(
            "Controls what happens when image chunks fail to load in time.\n\n"
            "None (Fastest):\n"
            "  Skip all fallbacks. Fastest, but may have missing (gray) tiles.\n\n"
            "Cache Only (Balanced):\n"
            "  Use cached data and pre-built lower mipmaps only.\n"
            "  Good balance of speed and quality. No extra network requests.\n\n"
            "Full (Best Quality):\n"
            "  All fallbacks including on-demand network downloads.\n"
            "  Best quality but slowest. May cause extra stuttering.\n\n"
            "Recommended: Cache Only for most users."
        )
        fallback_layout.addWidget(fallback_label)
        self.fallback_level_combo = QComboBox()
        self.fallback_level_combo.installEventFilter(self)
        self.fallback_level_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.fallback_level_combo.addItems([
            "None (Fastest)",
            "Cache Only (Balanced)",
            "Full (Best Quality)"
        ])
        # Convert string fallback_level to index
        fb_value = getattr(self.cfg.autoortho, 'fallback_level', 'cache')
        current_fallback = self._fallback_str_to_index(fb_value)
        self.fallback_level_combo.setCurrentIndex(current_fallback)
        self.fallback_level_combo.setObjectName('fallback_level')
        self.fallback_level_combo.setToolTip(
            "Select fallback behavior when chunks timeout"
        )
        self.fallback_level_combo.currentIndexChanged.connect(self._update_fallback_extends_control)
        fallback_layout.addWidget(self.fallback_level_combo)
        fallback_layout.addStretch()
        autoortho_layout.addLayout(fallback_layout)
        
        # Fallback extends budget checkbox (only relevant when fallback_level is 'full')
        fallback_extends_layout = QHBoxLayout()
        self.fallback_extends_budget_check = QCheckBox("Allow fallbacks to extend time budget")
        fb_extends_value = getattr(self.cfg.autoortho, 'fallback_extends_budget', False)
        if isinstance(fb_extends_value, str):
            fb_extends_checked = fb_extends_value.lower().strip() in ('true', '1', 'yes', 'on')
        else:
            fb_extends_checked = bool(fb_extends_value)
        self.fallback_extends_budget_check.setChecked(fb_extends_checked)
        self.fallback_extends_budget_check.setToolTip(
            "When enabled with 'Full' fallback level, adds EXTRA time after the main\n"
            "budget expires to recover missing chunks using lower-detail fallbacks.\n\n"
            "How it works:\n"
            "  1. Main budget (e.g., 300s) is used for normal chunk downloads\n"
            "  2. When main budget expires, a 'fallback sweep' phase begins\n"
            "  3. All missing chunks are processed using lower-zoom alternatives\n"
            "  4. Maximum total time = Main budget + Extended fallback timeout\n\n"
            "• Enabled: Better quality, fewer missing tiles (quality priority)\n"
            "• Disabled: Strict timing, may have gray patches (speed priority)"
        )
        self.fallback_extends_budget_check.stateChanged.connect(self._update_fallback_extends_control)
        fallback_extends_layout.addWidget(self.fallback_extends_budget_check)
        fallback_extends_layout.addStretch()
        autoortho_layout.addLayout(fallback_extends_layout)
        
        # Fallback timeout slider (per-level timeout when extends_budget is enabled)
        fallback_timeout_layout = QHBoxLayout()
        self.fallback_timeout_label = QLabel("Extended fallback timeout:")
        self.fallback_timeout_label.setToolTip(
            "TOTAL extra time for the fallback sweep phase.\n"
            "This time is added AFTER the main budget expires to recover\n"
            "all missing chunks using lower-detail alternatives.\n\n"
            "Maximum total tile time = Main budget + This value\n"
            "Example: 300s main + 30s fallback = 330s maximum\n\n"
            "Higher values = more time to recover missing chunks\n"
            "Lower values = faster tile completion, may miss some chunks"
        )
        fallback_timeout_layout.addWidget(self.fallback_timeout_label)
        
        self.fallback_timeout_slider = ModernSlider(Qt.Orientation.Horizontal)
        # Range: 1 to 30 seconds, with 1 second precision
        self.fallback_timeout_slider.setRange(10, 120)  # 10 to 120 seconds in 1 second increments
        self.fallback_timeout_slider.setSingleStep(1)
        fallback_timeout_value = int(float(getattr(self.cfg.autoortho, 'fallback_timeout', 3.0)))
        fallback_timeout_value = max(10, min(120, fallback_timeout_value))  # Clamp to valid range
        self.fallback_timeout_slider.setValue(fallback_timeout_value)
        self.fallback_timeout_slider.setObjectName('fallback_timeout')
        self.fallback_timeout_slider.setToolTip(
            "Drag to adjust extra time for fallback sweep (10-120 seconds)\n"
            "This is ADDED to the main tile budget when recovering missing chunks."
        )
        self.fallback_timeout_value_label = QLabel(f"{fallback_timeout_value}s")
        self.fallback_timeout_slider.valueChanged.connect(
            lambda v: self.fallback_timeout_value_label.setText(f"{v}s")
        )
        fallback_timeout_layout.addWidget(self.fallback_timeout_slider)
        fallback_timeout_layout.addWidget(self.fallback_timeout_value_label)
        autoortho_layout.addLayout(fallback_timeout_layout)
        
        # Initially update the enabled state
        self._update_fallback_extends_control()

        # Prefetch Settings Sub-section
        prefetch_header = QLabel("Prefetching")
        prefetch_header.setStyleSheet("font-weight: bold; font-size: 12px; color: #8ab4f8; margin-top: 10px;")
        autoortho_layout.addWidget(prefetch_header)
        
        # Prefetch enable checkbox
        prefetch_enable_layout = QHBoxLayout()
        self.prefetch_enabled_check = QCheckBox("Enable spatial prefetching")
        self.prefetch_enabled_check.setChecked(
            getattr(self.cfg.autoortho, 'prefetch_enabled', True)
        )
        self.prefetch_enabled_check.setToolTip(
            "Proactively download tiles ahead of the aircraft to reduce stutters.\n"
            "Uses aircraft heading and speed to predict which tiles will be needed."
        )
        self.prefetch_enabled_check.stateChanged.connect(self._update_prefetch_controls)
        prefetch_enable_layout.addWidget(self.prefetch_enabled_check)
        prefetch_enable_layout.addStretch()
        autoortho_layout.addLayout(prefetch_enable_layout)
        
        # Prefetch lookahead slider (in minutes, 0 = Unlimited)
        lookahead_layout = QHBoxLayout()
        self.prefetch_lookahead_label = QLabel("Lookahead time:")
        self.prefetch_lookahead_label.setToolTip(
            "How far ahead (in minutes) to prefetch tiles.\n"
            "Higher = more tiles prefetched ahead, uses more bandwidth and memory\n"
            "Lower = fewer tiles prefetched, less resource usage\n\n"
            "Example at 300 knots:\n"
            "  • 5 min = ~25nm ahead\n"
            "  • 10 min = ~50nm ahead\n"
            "  • 30 min = ~150nm ahead\n"
            "  • Unlimited = continues until max chunks/cycle or other limits"
        )
        lookahead_layout.addWidget(self.prefetch_lookahead_label)
        
        self.prefetch_lookahead_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.prefetch_lookahead_slider.setRange(1, 61)  # 1-60 minutes, 61 = Unlimited
        # Load config: 0 means unlimited -> slider value 61
        lookahead_config = int(float(getattr(self.cfg.autoortho, 'prefetch_lookahead', 10)))
        if lookahead_config <= 0:
            self.prefetch_lookahead_slider.setValue(61)  # Unlimited
        else:
            self.prefetch_lookahead_slider.setValue(min(lookahead_config, 60))
        self.prefetch_lookahead_slider.setObjectName('prefetch_lookahead')
        self.prefetch_lookahead_value = QLabel(
            "Unlimited" if self.prefetch_lookahead_slider.value() == 61 
            else f"{self.prefetch_lookahead_slider.value()} min"
        )
        self.prefetch_lookahead_slider.valueChanged.connect(
            lambda v: self.prefetch_lookahead_value.setText(
                "Unlimited" if v == 61 else f"{v} min"
            )
        )
        lookahead_layout.addWidget(self.prefetch_lookahead_slider)
        lookahead_layout.addWidget(self.prefetch_lookahead_value)
        autoortho_layout.addLayout(lookahead_layout)
        
        # Prefetch interval slider (NEW)
        interval_layout = QHBoxLayout()
        self.prefetch_interval_label = QLabel("Check interval:")
        self.prefetch_interval_label.setToolTip(
            "How often (in seconds) to check for prefetch opportunities.\n"
            "Lower = more responsive prefetching, slightly higher CPU\n"
            "Higher = less frequent checks, lower CPU\n\n"
            "Recommended: 2.0 sec (balanced)"
        )
        interval_layout.addWidget(self.prefetch_interval_label)
        
        self.prefetch_interval_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.prefetch_interval_slider.setRange(10, 100)  # 1.0-10.0 seconds (x10)
        self.prefetch_interval_slider.setValue(
            int(float(getattr(self.cfg.autoortho, 'prefetch_interval', 2.0)) * 10)
        )
        self.prefetch_interval_slider.setObjectName('prefetch_interval')
        self.prefetch_interval_value = QLabel(
            f"{self.prefetch_interval_slider.value() / 10:.1f} sec"
        )
        self.prefetch_interval_slider.valueChanged.connect(
            lambda v: self.prefetch_interval_value.setText(f"{v / 10:.1f} sec")
        )
        interval_layout.addWidget(self.prefetch_interval_slider)
        interval_layout.addWidget(self.prefetch_interval_value)
        autoortho_layout.addLayout(interval_layout)
        
        # Prefetch max chunks slider (NEW)
        max_chunks_layout = QHBoxLayout()
        self.prefetch_max_chunks_label = QLabel("Max chunks/cycle:")
        self.prefetch_max_chunks_label.setToolTip(
            "Maximum number of chunks to submit per prefetch cycle.\n"
            "Higher = more aggressive prefetching, more bandwidth\n"
            "Lower = gentler prefetching, less bandwidth\n\n"
            "Recommended: 48 (balanced), 64-128 (fast internet), 16-32 (slow internet)\n"
            "Values above 128 are for very fast connections only."
        )
        max_chunks_layout.addWidget(self.prefetch_max_chunks_label)
        
        self.prefetch_max_chunks_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.prefetch_max_chunks_slider.setRange(8, 512)
        self.prefetch_max_chunks_slider.setValue(
            int(getattr(self.cfg.autoortho, 'prefetch_max_chunks', 48))
        )
        self.prefetch_max_chunks_slider.setObjectName('prefetch_max_chunks')
        self.prefetch_max_chunks_value = QLabel(
            f"{self.prefetch_max_chunks_slider.value()}"
        )
        self.prefetch_max_chunks_slider.valueChanged.connect(
            lambda v: self.prefetch_max_chunks_value.setText(f"{v}")
        )
        max_chunks_layout.addWidget(self.prefetch_max_chunks_slider)
        max_chunks_layout.addWidget(self.prefetch_max_chunks_value)
        autoortho_layout.addLayout(max_chunks_layout)
        
        # Prefetch radius slider (unified for both velocity and SimBrief methods)
        radius_layout = QHBoxLayout()
        self.prefetch_radius_label = QLabel("Prefetch radius:")
        self.prefetch_radius_label.setToolTip(
            "Radius (in nautical miles) around the flight path to prefetch tiles.\n\n"
            "Tiles within this radius of each sample point along the route are prefetched.\n"
            "Used by both velocity-based and SimBrief flight plan prefetching.\n\n"
            "Higher = wider coverage, more tiles prefetched, higher bandwidth\n"
            "Lower = narrower corridor, fewer tiles, less bandwidth\n\n"
            "Recommended:\n"
            "  • 20 nm - Conservative (slow internet)\n"
            "  • 40 nm - Balanced (default)\n"
            "  • 60+ nm - Aggressive (fast internet, wide turns)"
        )
        radius_layout.addWidget(self.prefetch_radius_label)
        
        self.prefetch_radius_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.prefetch_radius_slider.setRange(10, 150)  # 10-150 nm
        self.prefetch_radius_slider.setValue(
            int(float(getattr(self.cfg.autoortho, 'prefetch_radius_nm', 40)))
        )
        self.prefetch_radius_slider.setObjectName('prefetch_radius_nm')
        self.prefetch_radius_value = QLabel(
            f"{self.prefetch_radius_slider.value()} nm"
        )
        self.prefetch_radius_slider.valueChanged.connect(
            lambda v: self.prefetch_radius_value.setText(f"{v} nm")
        )
        radius_layout.addWidget(self.prefetch_radius_slider)
        radius_layout.addWidget(self.prefetch_radius_value)
        autoortho_layout.addLayout(radius_layout)
        
        # ═══════════════════════════════════════════════════════════════════
        # PREDICTIVE DDS SECTION (NEW)
        # ═══════════════════════════════════════════════════════════════════
        autoortho_layout.addSpacing(10)
        predictive_dds_header = QLabel("Predictive DDS Generation")
        predictive_dds_header.setStyleSheet("font-weight: bold; margin-top: 10px;")
        autoortho_layout.addWidget(predictive_dds_header)
        
        # Enable checkbox
        predictive_enable_layout = QHBoxLayout()
        self.predictive_dds_enabled_check = QCheckBox("Enable predictive DDS building")
        self.predictive_dds_enabled_check.setChecked(
            getattr(self.cfg.autoortho, 'predictive_dds_enabled', True)
        )
        self.predictive_dds_enabled_check.setToolTip(
            "Pre-build DDS textures in the background after tiles are prefetched.\n\n"
            "When enabled:\n"
            "  • Downloaded tiles are compressed to DDS in the background\n"
            "  • X-Plane reads are served from cache (near-instant)\n"
            "  • Dramatically reduces stutters when entering new areas\n\n"
            "When disabled:\n"
            "  • Tiles are only downloaded, not pre-compressed\n"
            "  • DDS compression happens when X-Plane reads (can stutter)"
        )
        self.predictive_dds_enabled_check.stateChanged.connect(
            self._update_predictive_dds_controls
        )
        predictive_enable_layout.addWidget(self.predictive_dds_enabled_check)
        predictive_enable_layout.addStretch()
        autoortho_layout.addLayout(predictive_enable_layout)
        
        # Compiled DDS cache is persistent and disk-budget managed.
        # OS file cache naturally keeps hot files in RAM when memory is available
        
        # Build interval slider
        build_interval_layout = QHBoxLayout()
        self.predictive_interval_label = QLabel("Build interval:")
        self.predictive_interval_label.setToolTip(
            "Minimum time between DDS builds (rate limiting).\n"
            "Higher = less CPU usage, slower pre-building\n"
            "Lower = faster pre-building, more CPU usage\n\n"
            "Recommended:\n"
            "  • 250 ms - Fast CPU, aggressive building\n"
            "  • 500 ms - Balanced (default)\n"
            "  • 1000 ms - Low-end CPU, minimal impact"
        )
        build_interval_layout.addWidget(self.predictive_interval_label)
        
        self.predictive_interval_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.predictive_interval_slider.setRange(100, 2000)
        self.predictive_interval_slider.setSingleStep(50)
        self.predictive_interval_slider.setPageStep(100)
        self.predictive_interval_slider.setTickInterval(50)
        # Snap to nearest 50ms step
        raw_value = int(getattr(self.cfg.autoortho, 'predictive_dds_build_interval_ms', 500))
        snapped_value = ((raw_value + 25) // 50) * 50  # Round to nearest 50
        snapped_value = max(100, min(2000, snapped_value))  # Clamp to range
        self.predictive_interval_slider.setValue(snapped_value)
        self.predictive_interval_slider.setObjectName('predictive_dds_build_interval_ms')
        self.predictive_interval_value = QLabel(
            f"{self.predictive_interval_slider.value()} ms"
        )
        # Snap value to 50ms increments and update label
        def on_interval_changed(v):
            snapped = ((v + 25) // 50) * 50
            snapped = max(100, min(2000, snapped))
            if snapped != v:
                self.predictive_interval_slider.setValue(snapped)
            self.predictive_interval_value.setText(f"{snapped} ms")
        self.predictive_interval_slider.valueChanged.connect(on_interval_changed)
        build_interval_layout.addWidget(self.predictive_interval_slider)
        build_interval_layout.addWidget(self.predictive_interval_value)
        autoortho_layout.addLayout(build_interval_layout)
        
        # Prefetch workers slider (formerly "Background workers")
        prefetch_layout = QHBoxLayout()
        self.prefetch_workers_label = QLabel("Prefetch workers:")
        self.prefetch_workers_label.setToolTip(
            "Number of parallel workers for predictive/prefetch DDS builds.\n\n"
            "These run in the background to pre-build tiles ahead of where\n"
            "you're flying, reducing stutters when tiles are needed.\n\n"
            "Higher values = faster prefetch, more CPU usage\n"
            "Lower values = slower prefetch, less CPU impact\n\n"
            "Recommended:\n"
            "  • 1-2 - Low-end CPU or battery saving\n"
            "  • 4 - Balanced (default)\n"
            "  • 6-8 - Fast CPU, maximize prefetch speed"
        )
        prefetch_layout.addWidget(self.prefetch_workers_label)
        
        self.background_workers_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.background_workers_slider.setRange(1, 8)
        self.background_workers_slider.setSingleStep(1)
        self.background_workers_slider.setPageStep(1)
        self.background_workers_slider.setTickInterval(1)
        self.background_workers_slider.setValue(
            int(getattr(self.cfg.autoortho, 'background_builder_workers', 4))
        )
        self.background_workers_slider.setObjectName('background_builder_workers')
        self.prefetch_workers_value = QLabel(
            f"{self.background_workers_slider.value()}"
        )
        self.background_workers_slider.valueChanged.connect(
            lambda v: self._update_builder_concurrency_labels()
        )
        prefetch_layout.addWidget(self.background_workers_slider)
        prefetch_layout.addWidget(self.prefetch_workers_value)
        autoortho_layout.addLayout(prefetch_layout)
        
        # Use fallbacks checkbox
        fallbacks_layout = QHBoxLayout()
        self.predictive_use_fallbacks_check = QCheckBox("Apply fallbacks to prebuilt DDS")
        self.predictive_use_fallbacks_check.setChecked(
            getattr(self.cfg.autoortho, 'predictive_dds_use_fallbacks', True)
        )
        self.predictive_use_fallbacks_check.setToolTip(
            "How to handle failed chunks when pre-building DDS.\n\n"
            "When enabled (default):\n"
            "  • Apply same fallback chain as live requests\n"
            "  • Search disk cache, use lower zoom data if available\n"
            "  • Best quality but may do extra disk/network I/O\n\n"
            "When disabled:\n"
            "  • Use missing color for failed chunks (fastest)\n"
            "  • No extra I/O, minimal CPU overhead\n"
            "  • Failed areas show configured missing color"
        )
        fallbacks_layout.addWidget(self.predictive_use_fallbacks_check)
        fallbacks_layout.addStretch()
        autoortho_layout.addLayout(fallbacks_layout)
        
        autoortho_layout.addSpacing(10)
        # ═══════════════════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════════════════
        # NATIVE PIPELINE SECTION
        # ═══════════════════════════════════════════════════════════════════
        native_pipeline_header = QLabel("Native Pipeline")
        native_pipeline_header.setStyleSheet("font-weight: bold; font-size: 12px; color: #8ab4f8; margin-top: 10px;")
        autoortho_layout.addWidget(native_pipeline_header)
        
        native_pipeline_info = QLabel(
            "Controls how DDS textures are built. Native code provides 3x faster compression."
        )
        native_pipeline_info.setStyleSheet("color: #888; font-size: 11px;")
        native_pipeline_info.setWordWrap(True)
        autoortho_layout.addWidget(native_pipeline_info)
        
        # Pipeline mode dropdown
        pipeline_mode_layout = QHBoxLayout()
        pipeline_mode_label = QLabel("Pipeline mode:")
        pipeline_mode_label.setToolTip(
            "Controls how DDS textures are built:\n\n"
            "• Auto (Recommended): Automatically selects best mode for your platform\n"
            "    - Windows → Native (C handles all I/O + decode + compress)\n"
            "    - macOS/Linux → Hybrid (Python I/O + C decode/compress)\n\n"
            "• Native: Full native pipeline - C code handles file I/O, JPEG decoding,\n"
            "  and DXT compression. Fastest on Windows with many CPU cores.\n\n"
            "• Hybrid: Python reads files, native code does decode + compress.\n"
            "  Fastest on macOS/Linux due to better VFS caching.\n\n"
            "• Python: Pure Python fallback. Slowest but most compatible.\n"
            "  Use if native pipeline causes crashes or issues."
        )
        pipeline_mode_layout.addWidget(pipeline_mode_label)
        
        self.pipeline_mode_combo = QComboBox()
        self.pipeline_mode_combo.installEventFilter(self)
        self.pipeline_mode_combo.setFocusPolicy(Qt.StrongFocus)
        self.pipeline_mode_combo.addItems(['auto', 'native', 'hybrid', 'python'])
        current_pipeline_mode = str(getattr(self.cfg.autoortho, 'pipeline_mode', 'auto')).lower().strip()
        if current_pipeline_mode not in ['auto', 'native', 'hybrid', 'python']:
            current_pipeline_mode = 'auto'
        self.pipeline_mode_combo.setCurrentText(current_pipeline_mode)
        self.pipeline_mode_combo.setObjectName('pipeline_mode')
        self.pipeline_mode_combo.setToolTip(
            "Select DDS building pipeline mode:\n\n"
            "• Auto (recommended): Uses hybrid with buffer pool optimization\n"
            "  ~65ms per tile (Python file reads + native compression)\n\n"
            "• Hybrid: Python reads files, native decode+compress\n"
            "  Fastest with buffer pool, lower thread overhead\n\n"
            "• Native: C handles all file I/O + decode + compress\n"
            "  May be better for cold cache scenarios\n\n"
            "• Python: Pure Python fallback (slowest)\n"
            "  Use if native pipeline causes issues"
        )
        self.pipeline_mode_combo.currentTextChanged.connect(self._update_pipeline_controls)
        pipeline_mode_layout.addWidget(self.pipeline_mode_combo)
        pipeline_mode_layout.addStretch()
        autoortho_layout.addLayout(pipeline_mode_layout)
        
        # Tile Build Workers slider (controls concurrent tile builds)
        tile_workers_layout = QHBoxLayout()
        self.tile_build_workers_label = QLabel("Tile build workers:")
        self.tile_build_workers_label.setToolTip(
            "Number of concurrent tile build workers.\n\n"
            "Controls how many tiles can be built simultaneously by the\n"
            "native pipeline (JPEG decode + DXT compress).\n\n"
            "Higher values = faster tile processing, more CPU/RAM usage\n"
            "Lower values = less resource usage, potential stutters\n\n"
            "Recommended:\n"
            "  • 4 - Low-end CPU (4-8 threads)\n"
            "  • 8 - Mid-range CPU (8-16 threads, default)\n"
            "  • 16-32 - High-end CPU (16+ threads)\n\n"
            "Also affects JPEG decoder pool size (workers × CPU threads)."
        )
        tile_workers_layout.addWidget(self.tile_build_workers_label)
        
        self.live_concurrency_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.live_concurrency_slider.setRange(1, 32)
        self.live_concurrency_slider.setSingleStep(1)
        self.live_concurrency_slider.setPageStep(4)
        self.live_concurrency_slider.setTickInterval(4)
        self.live_concurrency_slider.setValue(
            int(getattr(self.cfg.autoortho, 'live_builder_concurrency', 8))
        )
        self.live_concurrency_slider.setObjectName('live_builder_concurrency')
        self.live_concurrency_value = QLabel()
        self._update_builder_concurrency_labels()  # Set initial value with RAM estimate
        self.live_concurrency_slider.valueChanged.connect(
            lambda v: self._update_builder_concurrency_labels()
        )
        tile_workers_layout.addWidget(self.live_concurrency_slider)
        tile_workers_layout.addWidget(self.live_concurrency_value)
        autoortho_layout.addLayout(tile_workers_layout)
        
        # Builder RAM estimate label
        self.builder_ram_label = QLabel()
        self.builder_ram_label.setStyleSheet("color: #888; font-size: 11px; margin-left: 10px;")
        self._update_builder_concurrency_labels()
        autoortho_layout.addWidget(self.builder_ram_label)
        
        # Buffer pool size slider
        buffer_pool_layout = QHBoxLayout()
        self.buffer_pool_label = QLabel(
            "Concurrent imagery memory buffers:"
        )
        self.buffer_pool_label.setToolTip(
            "Number of pre-allocated buffers for zero-copy DDS building.\n\n"
            "Buffer size is calculated dynamically based on your settings:\n"
            "• ~11MB per buffer for 4K textures (max_zoom ≤ 16)\n"
            "• ~43MB per buffer for 8K textures (max_zoom > 16 or custom tiles)\n\n"
            "Default & Maximum: prefetch workers + live concurrency\n"
            "This is optimal because each concurrent build needs exactly one buffer.\n"
            "More buffers than workers would waste memory (never used simultaneously).\n\n"
            "The maximum adjusts automatically when you change worker counts.\n\n"
            "Only applies to Native and Hybrid modes."
        )
        buffer_pool_layout.addWidget(self.buffer_pool_label)
        
        self.buffer_pool_slider = ModernSlider(Qt.Orientation.Horizontal)
        # Calculate optimal pool size from worker counts (will be updated dynamically)
        prefetch = int(getattr(self.cfg.autoortho, 'background_builder_workers', 4))
        live = int(getattr(self.cfg.autoortho, 'live_builder_concurrency', 8))
        optimal_pool_size = prefetch + live
        self.buffer_pool_slider.setRange(2, optimal_pool_size)
        # Use configured value or optimal default, clamped to valid range
        buffer_pool_value = int(getattr(self.cfg.autoortho, 'buffer_pool_size', optimal_pool_size))
        buffer_pool_value = max(2, min(optimal_pool_size, buffer_pool_value))
        self.buffer_pool_slider.setValue(buffer_pool_value)
        self.buffer_pool_slider.setObjectName('buffer_pool_size')
        self.buffer_pool_slider.setToolTip(f"Number of pre-allocated DDS buffers (2-{optimal_pool_size})")
        
        self.buffer_pool_value_label = QLabel("")
        self._update_buffer_pool_label()
        self.buffer_pool_slider.valueChanged.connect(lambda v: self._update_buffer_pool_label())
        buffer_pool_layout.addWidget(self.buffer_pool_slider)
        buffer_pool_layout.addWidget(self.buffer_pool_value_label)
        autoortho_layout.addLayout(buffer_pool_layout)
        
        # Initialize pipeline control states
        self._update_pipeline_controls()
        
        autoortho_layout.addSpacing(10)
        # ═══════════════════════════════════════════════════════════════════
        
        # Initialize prefetch control states
        self._update_prefetch_controls()

        # Initialize time budget control states
        self._update_time_budget_controls()

        provider_header = QLabel("Provider Download Transport")
        provider_header.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #8ab4f8; margin-top: 10px;"
        )
        autoortho_layout.addWidget(provider_header)

        inflight_layout = QHBoxLayout()
        inflight_label = QLabel("Maximum requests in flight:")
        inflight_label.setToolTip(
            "Strict global limit for outstanding provider requests. HTTP/2 requests\n"
            "are multiplexed asynchronously, so this no longer requires one Python\n"
            "thread per request. Higher values need a fast provider and network."
        )
        inflight_layout.addWidget(inflight_label)
        self.provider_inflight_spinbox = ModernSpinBox()
        self.provider_inflight_spinbox.setRange(8, 1024)
        self.provider_inflight_spinbox.setValue(
            int(getattr(self.cfg.autoortho, "provider_max_in_flight", 128))
        )
        self.provider_inflight_spinbox.setObjectName("provider_max_in_flight")
        inflight_layout.addWidget(self.provider_inflight_spinbox)
        inflight_layout.addStretch()
        autoortho_layout.addLayout(inflight_layout)

        connections_layout = QHBoxLayout()
        connections_label = QLabel("Provider connections:")
        connections_label.setToolTip(
            "Maximum reusable physical connections. HTTP/2 can carry many streams\n"
            "per connection; 64 also preserves throughput for HTTP/1.1 providers."
        )
        connections_layout.addWidget(connections_label)
        self.provider_connections_spinbox = ModernSpinBox()
        self.provider_connections_spinbox.setRange(1, 256)
        self.provider_connections_spinbox.setValue(
            int(getattr(self.cfg.autoortho, "provider_max_connections", 64))
        )
        self.provider_connections_spinbox.setObjectName(
            "provider_max_connections"
        )
        connections_layout.addWidget(self.provider_connections_spinbox)
        connections_layout.addStretch()
        autoortho_layout.addLayout(connections_layout)

        dispatch_layout = QHBoxLayout()
        dispatch_label = QLabel("Download completion workers:")
        dispatch_label.setToolTip(
            "Small coordination pool that applies completed responses. This does\n"
            "not limit network concurrency; 4 is appropriate for most systems."
        )
        dispatch_layout.addWidget(dispatch_label)
        self.download_dispatch_workers_spinbox = ModernSpinBox()
        self.download_dispatch_workers_spinbox.setRange(1, 16)
        self.download_dispatch_workers_spinbox.setValue(
            int(getattr(self.cfg.autoortho, "download_dispatch_workers", 4))
        )
        self.download_dispatch_workers_spinbox.setObjectName(
            "download_dispatch_workers"
        )
        dispatch_layout.addWidget(self.download_dispatch_workers_spinbox)
        dispatch_layout.addStretch()
        autoortho_layout.addLayout(dispatch_layout)

        self.provider_adaptive_check = QCheckBox(
            "Adapt concurrency to each imagery provider"
        )
        self.provider_adaptive_check.setChecked(
            bool(
                getattr(
                    self.cfg.autoortho,
                    "provider_adaptive_concurrency",
                    True,
                )
            )
        )
        self.provider_adaptive_check.setObjectName(
            "provider_adaptive_concurrency"
        )
        self.provider_adaptive_check.setToolTip(
            "Raises concurrency after sustained successful responses and reduces\n"
            "it when a provider returns overload errors or timeouts."
        )
        autoortho_layout.addWidget(self.provider_adaptive_check)

        memory_layout = QHBoxLayout()
        memory_label = QLabel("Concurrent live tiles:")
        memory_label.setToolTip(
            "Bounds complete live tile builds before allocating large ZL17\n"
            "composition buffers. Downloads remain independently concurrent."
        )
        memory_layout.addWidget(memory_label)
        self.live_tile_admission_spinbox = ModernSpinBox()
        self.live_tile_admission_spinbox.setRange(1, 128)
        self.live_tile_admission_spinbox.setValue(
            int(getattr(self.cfg.autoortho, "live_tile_admission", 16))
        )
        self.live_tile_admission_spinbox.setObjectName("live_tile_admission")
        memory_layout.addWidget(self.live_tile_admission_spinbox)
        memory_layout.addWidget(QLabel("Fallback image cache/tile:"))
        self.tile_image_cache_mb_spinbox = ModernSpinBox()
        self.tile_image_cache_mb_spinbox.setRange(0, 512)
        self.tile_image_cache_mb_spinbox.setSuffix(" MB")
        self.tile_image_cache_mb_spinbox.setValue(
            int(getattr(self.cfg.autoortho, "tile_image_cache_mb", 96))
        )
        self.tile_image_cache_mb_spinbox.setObjectName("tile_image_cache_mb")
        memory_layout.addWidget(self.tile_image_cache_mb_spinbox)
        memory_layout.addStretch()
        autoortho_layout.addLayout(memory_layout)

        # HTTP/1.1 fallback threads
        threads_layout = QHBoxLayout()
        threads_label = QLabel("HTTP/1.1 fallback threads:")
        threads_label.setToolTip(
            "Used only when the shared asynchronous broker is unavailable.\n"
            "Normal network concurrency is controlled by Maximum requests in flight."
        )
        threads_layout.addWidget(threads_label)
        self.fetch_threads_spinbox = ModernSpinBox()
        self.fetch_threads_spinbox.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel

        max_threads = max(8, min(64, (os.cpu_count() or 4) * 2))
        self.fetch_threads_spinbox.setRange(1, max_threads)

        # Ensure initial value doesn't exceed available threads
        initial_threads = min(
            int(self.cfg.autoortho.fetch_threads), max_threads
        )
        self.fetch_threads_spinbox.setValue(initial_threads)
        self.fetch_threads_spinbox.setObjectName('fetch_threads')
        self.fetch_threads_spinbox.setToolTip(
            f"Number of local download dispatch threads per active mount "
            f"(1-{max_threads}). Network concurrency is controlled globally "
            "by the shared HTTP/2 broker."
        )

        threads_layout.addWidget(self.fetch_threads_spinbox)
        threads_layout.addStretch()
        autoortho_layout.addLayout(threads_layout)

        missing_color_layout = QHBoxLayout()
        missing_color_layout.setSpacing(10)
        missing_color_label = QLabel("Missing Tile Color:")
        missing_color_label.setToolTip(
            "This is the solid color used to fill a texture when\n"
            "scenery data cannot be fetched.  It can be useful to\n"
            "set this to a more visible color when tuning the maxwait\n"
            "setting to make it easier to see missing textures."
        )
        self.missing_color_button = StyledButton("Select")
        self.missing_color = QColor(
            self.cfg.autoortho.missing_color[0],
            self.cfg.autoortho.missing_color[1],
            self.cfg.autoortho.missing_color[2],
        )
        self.update_missing_color_button()
        self.missing_color_button.clicked.connect(self.show_missing_color_dialog)

        self.reset_color_button = StyledButton("Reset")
        self.reset_color_button.setToolTip(
            "Reset the missing texture color to the default gray."
        )
        self.reset_color_button.clicked.connect(self.reset_missing_color)
        missing_color_layout.addWidget(missing_color_label)
        missing_color_layout.addWidget(self.missing_color_button)
        missing_color_layout.addWidget(self.reset_color_button)
        missing_color_layout.addStretch()
        autoortho_layout.addLayout(missing_color_layout)

        if self.cfg.autoortho.using_custom_tiles:
            self.info_label = QLabel(
                "Note: You are using custom tiles. Max zoom near airports setting is incompatible with custom tiles, all tiles will be capped to the general max zoom level you set.\n\n"
                "You can use tiles with different zoom levels, they will be automatically capped to the maximum zoom level they support, even if a higher max zoom level than they support is set.\n"
            )
            self.info_label.setStyleSheet("color: #6da4e3; font-size: 14; font-weight: italic; font-weight: bold; text-align: justify;")
            # wrap text
            self.info_label.setWordWrap(True)
            autoortho_layout.addWidget(self.info_label)

        self._partition_autoortho_settings(
            autoortho_layout,
            perf_separator,
            prefetch_header,
            native_pipeline_header,
        )
        for section in (
            self.imagery_settings_group,
            self.performance_settings_group,
            self.prefetch_settings_group,
            self.pipeline_settings_group,
        ):
            self.settings_layout.addWidget(section)

        # Seasons Settings group
        seasons_group = QGroupBox("Seasons")
        self.seasons_settings_group = seasons_group
        seasons_layout = QVBoxLayout()
        seasons_group.setLayout(seasons_layout)

        # Enable control
        seasons_toggle_layout = QHBoxLayout()
        self.seasons_enabled_check = QCheckBox(
            "Enable Seasons Saturation Adjustments"
        )
        self.seasons_enabled_check.setToolTip(
            "Activates desaturation of base ortho texture to enhance the application of seasons effects.\n"
            "NOTE: This does not turn seasons on/off.\n"
            "Activate seasons by adding Seasons data in the Scenery tab."
        )

        seasons_enabled = bool(self.cfg.seasons.enabled)
        self.seasons_enabled_check.setChecked(seasons_enabled)
        self.seasons_enabled_check.toggled.connect(self.on_seasons_enabled_toggled)
        seasons_toggle_layout.addWidget(self.seasons_enabled_check)
        seasons_toggle_layout.addStretch()
        seasons_layout.addLayout(seasons_toggle_layout)

        # Spring saturation
        spr_row = QHBoxLayout()
        spr_label = QLabel("Spring Saturation")
        self.spr_sat_slider = ModernSlider()
        self.spr_sat_slider.setRange(0, 100)
        self.spr_sat_slider.setSingleStep(5)
        spr_val = int(float(self.cfg.seasons.spr_saturation))
        self.spr_sat_slider.setValue(spr_val)
        self.spr_sat_slider.setObjectName('spr_saturation')
        self.spr_sat_value_label = QLabel(f"{spr_val}%")
        self.spr_sat_slider.valueChanged.connect(
            lambda v: self.spr_sat_value_label.setText(f"{v}%")
        )
        spr_row.addWidget(spr_label)
        spr_row.addWidget(self.spr_sat_slider)
        spr_row.addWidget(self.spr_sat_value_label)
        seasons_layout.addLayout(spr_row)

        # Summer saturation
        sum_row = QHBoxLayout()
        sum_label = QLabel("Summer Saturation")
        self.sum_sat_slider = ModernSlider()
        self.sum_sat_slider.setRange(0, 100)
        self.sum_sat_slider.setSingleStep(5)
        sum_val = int(float(self.cfg.seasons.sum_saturation))
        self.sum_sat_slider.setValue(sum_val)
        self.sum_sat_slider.setObjectName('sum_saturation')
        self.sum_sat_value_label = QLabel(f"{sum_val}%")
        self.sum_sat_slider.valueChanged.connect(
            lambda v: self.sum_sat_value_label.setText(f"{v}%")
        )
        sum_row.addWidget(sum_label)
        sum_row.addWidget(self.sum_sat_slider)
        sum_row.addWidget(self.sum_sat_value_label)
        seasons_layout.addLayout(sum_row)

        # Fall saturation
        fal_row = QHBoxLayout()
        fal_label = QLabel("Fall Saturation")
        self.fal_sat_slider = ModernSlider()
        self.fal_sat_slider.setRange(0, 100)
        self.fal_sat_slider.setSingleStep(5)
        fal_val = int(float(self.cfg.seasons.fal_saturation))
        self.fal_sat_slider.setValue(fal_val)
        self.fal_sat_slider.setObjectName('fal_saturation')
        self.fal_sat_value_label = QLabel(f"{fal_val}%")
        self.fal_sat_slider.valueChanged.connect(
            lambda v: self.fal_sat_value_label.setText(f"{v}%")
        )
        fal_row.addWidget(fal_label)
        fal_row.addWidget(self.fal_sat_slider)
        fal_row.addWidget(self.fal_sat_value_label)
        seasons_layout.addLayout(fal_row)

        # Winter saturation
        win_row = QHBoxLayout()
        win_label = QLabel("Winter Saturation")
        self.win_sat_slider = ModernSlider()
        self.win_sat_slider.setRange(0, 100)
        self.win_sat_slider.setSingleStep(5)
        win_val = int(float(self.cfg.seasons.win_saturation))
        self.win_sat_slider.setValue(win_val)
        self.win_sat_slider.setObjectName('win_saturation')
        self.win_sat_value_label = QLabel(f"{win_val}%")
        self.win_sat_slider.valueChanged.connect(
            lambda v: self.win_sat_value_label.setText(f"{v}%")
        )
        win_row.addWidget(win_label)
        win_row.addWidget(self.win_sat_slider)
        win_row.addWidget(self.win_sat_value_label)
        seasons_layout.addLayout(win_row)

        # Initialize enabled state of sliders
        self._set_seasons_controls_enabled(seasons_enabled)

        self.settings_layout.addWidget(seasons_group)

        # DDS Compression Settings group
        dds_group = QGroupBox("DDS Compression Settings")
        self.dds_settings_group = dds_group
        dds_layout = QVBoxLayout()
        dds_group.setLayout(dds_layout)

        # Compressor
        supported_compressors = ['ISPC'] if self.system == "darwin" else ['ISPC', 'STB']
        if not self.system == "darwin":
            compressor_layout = QHBoxLayout()
            compressor_label = QLabel("Compressor:")
            compressor_label.setToolTip(
                "Algorithm used for DDS texture compression:\n"
                "• ISPC: Intel's high-performance compressor (recommended)\n"
                "  - Faster compression, better quality\n"
                "  - Requires modern CPU\n"
                "• STB: Standard compressor (compatibility)\n"
                "  - Slower but works on all systems\n"
                "  - Use if ISPC causes issues"
            )
            compressor_layout.addWidget(compressor_label)
            self.compressor_combo = QComboBox()
            self.compressor_combo.installEventFilter(self)
            self.compressor_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
            self.compressor_combo.addItems(supported_compressors)
            self.compressor_combo.setCurrentText(self.cfg.pydds.compressor)
            self.compressor_combo.setObjectName('compressor')
            self.compressor_combo.setToolTip(
                "Select compression algorithm (ISPC recommended)"
            )
            compressor_layout.addWidget(self.compressor_combo)
            compressor_layout.addStretch()
            dds_layout.addLayout(compressor_layout)
        else:
            if self.cfg.pydds.compressor not in supported_compressors:
                self.cfg.pydds.compressor = "ISPC"
                QMessageBox.warning(self, "Warning", "ISPC is the only supported compressor on MacOS, your current compressor has been changed to ISPC.")

        # Format
        format_layout = QHBoxLayout()
        format_label = QLabel("Format:")
        format_label.setToolTip(
            "DDS compression format:\n"
            "• BC1: Smaller files, no transparency, good for terrain\n"
            "  - 4:1 compression ratio\n"
            "  - Recommended for most scenery\n"
            "• BC3: Larger files, supports transparency\n"
            "  - 3:1 compression ratio\n"
            "  - Use only if transparency is needed"
        )
        format_layout.addWidget(format_label)
        self.format_combo = QComboBox()
        self.format_combo.installEventFilter(self)
        self.format_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.format_combo.addItems(['BC1', 'BC3'])
        self.format_combo.setCurrentText(self.cfg.pydds.format)
        self.format_combo.setObjectName('format')
        self.format_combo.setToolTip(
            "Select DDS format (BC1 recommended for most uses)"
        )
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()
        dds_layout.addLayout(format_layout)

        self.settings_layout.addWidget(dds_group)

        # General Settings group
        general_group = QGroupBox("General Settings")
        self.general_settings_group = general_group
        general_layout = QVBoxLayout()
        general_group.setLayout(general_layout)


        self.gui_check = QCheckBox("Use GUI at startup")
        self.gui_check.setChecked(self.cfg.general.gui)
        self.gui_check.setObjectName('gui')
        self.gui_check.setToolTip(
            "Show graphical interface when AutoOrtho starts.\n"
            "If disabled, AutoOrtho runs in background only.\n"
            "Recommended: Enabled for easier monitoring and control."
        )
        general_layout.addWidget(self.gui_check)

        general_layout.addSpacing(10)

        self.hide_check = QCheckBox("Hide window when running")
        self.hide_check.setChecked(self.cfg.general.hide)
        self.hide_check.setObjectName('hide')
        self.hide_check.setToolTip(
            "Minimize AutoOrtho window to system tray when running.\n"
            "Helps keep desktop clean during long flights.\n"
            "You can still access it from the system tray."
        )
        general_layout.addWidget(self.hide_check)

        # Console/UI log level
        console_log_level_layout = QHBoxLayout()
        console_log_level_label = QLabel("UI Log Level:")
        console_log_level_label.setToolTip(
            "Set the minimum log level displayed in the UI Logs tab.\n"
            "DEBUG: Show all messages (very verbose)\n"
            "INFO: Show informational messages and above (recommended)\n"
            "WARNING: Show only warnings and errors\n"
            "ERROR: Show only errors and critical messages\n"
            "CRITICAL: Show only critical errors\n\n"
            "Changes take effect immediately.\n"
            "This does not affect the log file."
        )
        console_log_level_layout.addWidget(console_log_level_label)
        self.console_log_level_combo = QComboBox()
        self.console_log_level_combo.installEventFilter(self)
        self.console_log_level_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.console_log_level_combo.addItems(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
        console_level = getattr(self.cfg.general, 'console_log_level', 'INFO').upper()
        self.console_log_level_combo.setCurrentText(console_level)
        self.console_log_level_combo.setObjectName('console_log_level')
        self.console_log_level_combo.setToolTip(
            "Select the minimum log level for the UI (INFO recommended)\n"
            "Changes take effect immediately - no restart needed!"
        )
        self.console_log_level_combo.currentTextChanged.connect(self.on_console_log_level_changed)
        console_log_level_layout.addWidget(self.console_log_level_combo)
        console_log_level_layout.addStretch()
        general_layout.addLayout(console_log_level_layout)

        # File log level
        file_log_level_layout = QHBoxLayout()
        file_log_level_label = QLabel("File Log Level:")
        file_log_level_label.setToolTip(
            "Set the minimum log level saved to the log file.\n"
            "DEBUG: Save all messages (recommended for bug reports)\n"
            "INFO: Save informational messages and above\n"
            "WARNING: Save only warnings and errors\n"
            "ERROR: Save only errors and critical messages\n"
            "CRITICAL: Save only critical errors\n\n"
            "Changes take effect immediately.\n"
            "This does not affect what's shown in the UI.\n"
            "DEBUG is recommended so bug reports include full details."
        )
        file_log_level_layout.addWidget(file_log_level_label)
        self.file_log_level_combo = QComboBox()
        self.file_log_level_combo.installEventFilter(self)
        self.file_log_level_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
        self.file_log_level_combo.addItems(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
        file_level = getattr(self.cfg.general, 'file_log_level', 'DEBUG').upper()
        self.file_log_level_combo.setCurrentText(file_level)
        self.file_log_level_combo.setObjectName('file_log_level')
        self.file_log_level_combo.setToolTip(
            "Select the minimum log level for the file (DEBUG recommended)\n"
            "Changes take effect immediately - no restart needed."
        )
        self.file_log_level_combo.currentTextChanged.connect(self.on_file_log_level_changed)
        file_log_level_layout.addWidget(self.file_log_level_combo)
        file_log_level_layout.addStretch()
        general_layout.addLayout(file_log_level_layout)

        self.settings_layout.addWidget(general_group)

        diagnostics_group = QGroupBox("Performance Diagnostics")
        self.diagnostics_settings_group = diagnostics_group
        diagnostics_layout = QVBoxLayout()
        diagnostics_group.setLayout(diagnostics_layout)

        self.performance_profiling_check = QCheckBox(
            "Create a performance report for each flight session"
        )
        self.performance_profiling_check.setChecked(
            bool(getattr(self.cfg.diagnostics, 'performance_profiling', True))
        )
        self.performance_profiling_check.setObjectName('performance_profiling')
        self.performance_profiling_check.setToolTip(
            "Records tile pipeline latency histograms and samples CPU/RAM for every\n"
            "AutoOrtho process. A Markdown report and raw JSON timeline are written\n"
            "to the diagnostics report directory when scenery is unmounted."
        )
        diagnostics_layout.addWidget(self.performance_profiling_check)

        sampling_layout = QHBoxLayout()
        sampling_label = QLabel("Resource sample interval:")
        sampling_label.setToolTip(
            "How often process memory, CPU, thread count, and I/O are sampled.\n"
            "One second is recommended and has low overhead."
        )
        sampling_layout.addWidget(sampling_label)
        self.performance_sample_interval_spin = QDoubleSpinBox()
        self.performance_sample_interval_spin.setRange(0.1, 60.0)
        self.performance_sample_interval_spin.setSingleStep(0.5)
        self.performance_sample_interval_spin.setDecimals(1)
        self.performance_sample_interval_spin.setSuffix(" s")
        self.performance_sample_interval_spin.setValue(
            float(getattr(self.cfg.diagnostics, 'sample_interval_seconds', 1.0))
        )
        self.performance_sample_interval_spin.setObjectName(
            'sample_interval_seconds'
        )
        sampling_layout.addWidget(self.performance_sample_interval_spin)
        sampling_layout.addStretch()
        diagnostics_layout.addLayout(sampling_layout)

        checkpoint_layout = QHBoxLayout()
        checkpoint_label = QLabel("Profile checkpoint interval:")
        checkpoint_label.setToolTip(
            "How often worker stage histograms and gauges are atomically persisted.\n"
            "Checkpoints preserve useful diagnostics after a forced worker exit."
        )
        checkpoint_layout.addWidget(checkpoint_label)
        self.performance_checkpoint_interval_spin = QDoubleSpinBox()
        self.performance_checkpoint_interval_spin.setRange(1.0, 300.0)
        self.performance_checkpoint_interval_spin.setSingleStep(5.0)
        self.performance_checkpoint_interval_spin.setDecimals(0)
        self.performance_checkpoint_interval_spin.setSuffix(" s")
        self.performance_checkpoint_interval_spin.setValue(
            float(
                getattr(
                    self.cfg.diagnostics,
                    "checkpoint_interval_seconds",
                    10.0,
                )
            )
        )
        self.performance_checkpoint_interval_spin.setObjectName(
            "checkpoint_interval_seconds"
        )
        checkpoint_layout.addWidget(self.performance_checkpoint_interval_spin)
        checkpoint_layout.addStretch()
        diagnostics_layout.addLayout(checkpoint_layout)

        self.python_allocation_tracing_check = QCheckBox(
            "Trace Python allocation growth (diagnostic flights only)"
        )
        self.python_allocation_tracing_check.setChecked(
            bool(getattr(self.cfg.diagnostics, 'python_allocation_tracing', False))
        )
        self.python_allocation_tracing_check.setObjectName(
            'python_allocation_tracing'
        )
        self.python_allocation_tracing_check.setToolTip(
            "Adds file-and-line allocation growth to the report. This can slow the\n"
            "application and cannot see native C image/DDS buffers, so leave it off\n"
            "when measuring normal tile latency."
        )
        diagnostics_layout.addWidget(self.python_allocation_tracing_check)

        diagnostics_path = QLabel(
            f"Reports: {getattr(self.cfg.diagnostics, 'report_dir', '~/.autoortho-data/reports')}"
        )
        diagnostics_path.setWordWrap(True)
        diagnostics_path.setStyleSheet("color: #888; font-size: 11px;")
        diagnostics_layout.addWidget(diagnostics_path)

        self.settings_layout.addWidget(diagnostics_group)

        # FUSE Settings group
        fuse_group = QGroupBox("FUSE Settings")
        self.fuse_settings_group = fuse_group
        fuse_layout = QVBoxLayout()
        fuse_group.setLayout(fuse_layout)

        self.threading_check = QCheckBox("Enable multi-threading")
        self.threading_check.setChecked(self.cfg.fuse.threading)
        self.threading_check.setObjectName('threading')
        self.threading_check.setToolTip(
            "Use multiple threads for file system operations.\n"
            "Improves performance on multi-core systems.\n"
            "May cause issues on some older systems.\n"
            "Recommended: Enabled on modern multi-core CPUs."
        )
        fuse_layout.addWidget(self.threading_check)

        # Windows specific
        if self.system == 'windows':
            self.winfsp_check = QCheckBox("Prefer WinFSP over Dokan")
            self.winfsp_check.setChecked(self.cfg.windows.prefer_winfsp)
            self.winfsp_check.setObjectName('prefer_winfsp')
            self.winfsp_check.setToolTip(
                "WinFSP generally provides better performance than Dokan.\n"
                "Enable this if you have WinFSP installed.\n"
                "If you experience issues, try disabling this option.\n"
                "Recommended: Enabled (if WinFSP is available)"
            )
            fuse_layout.addWidget(self.winfsp_check)

        self.settings_layout.addWidget(fuse_group)

        # Flight Data Settings group
        flightdata_group = QGroupBox("Flight Data Settings")
        self.flightdata_settings_group = flightdata_group
        flightdata_layout = QVBoxLayout()
        flightdata_group.setLayout(flightdata_layout)

        # Web UI port
        webui_port_layout = QHBoxLayout()
        webui_port_label = QLabel("Web UI port:")
        webui_port_label.setToolTip(
            "Port number for the web-based monitoring interface.\n"
            "Access via http://localhost:[port] in your browser.\n"
            "Must be an unused port between 1024-65535.\n"
            "Default: 8080. Change if port conflicts occur."
        )
        webui_port_layout.addWidget(webui_port_label)
        self.webui_port_edit = QLineEdit(str(self.cfg.flightdata.webui_port))
        self.webui_port_edit.setObjectName('webui_port')
        self.webui_port_edit.setToolTip(
            "Port number for web interface (default: 8080)"
        )
        webui_port_layout.addWidget(self.webui_port_edit)
        webui_port_layout.addStretch()
        flightdata_layout.addLayout(webui_port_layout)

        # X-Plane UDP port
        xplane_port_layout = QHBoxLayout()
        xplane_port_label = QLabel("X-Plane UDP port:")
        xplane_port_label.setToolTip(
            "UDP port for receiving flight data from X-Plane.\n"
            "Must match the port configured in X-Plane's data output "
            "settings.\n"
            "Default: 49001. Check X-Plane Settings > Data Output."
        )
        xplane_port_layout.addWidget(xplane_port_label)
        self.xplane_udp_port_edit = QLineEdit(
            str(self.cfg.flightdata.xplane_udp_port)
        )
        self.xplane_udp_port_edit.setObjectName('xplane_udp_port')
        self.xplane_udp_port_edit.setToolTip(
            "UDP port for X-Plane data (must match X-Plane settings)"
        )
        xplane_port_layout.addWidget(self.xplane_udp_port_edit)
        xplane_port_layout.addStretch()
        flightdata_layout.addLayout(xplane_port_layout)

        self.settings_layout.addWidget(flightdata_group)

        # Night Exclusion Settings group (sun-position based)
        time_exclusion_group = QGroupBox("Night Exclusion (Sun Position)")
        self.night_settings_group = time_exclusion_group
        time_exclusion_layout = QVBoxLayout()
        time_exclusion_group.setLayout(time_exclusion_layout)

        # Info label
        time_exclusion_info = QLabel(
            "Automatically disable orthophoto scenery at night based on the sun's\n"
            "position. X-Plane will use default scenery with night lighting instead.\n"
            "Works accurately across seasons, latitudes, and with time acceleration."
        )
        time_exclusion_info.setStyleSheet("color: #888; font-size: 11px;")
        time_exclusion_info.setWordWrap(True)
        time_exclusion_layout.addWidget(time_exclusion_info)
        
        time_exclusion_layout.addSpacing(5)

        # Enable checkbox
        self.time_exclusion_enabled_check = QCheckBox("Enable night exclusion")
        time_exclusion_enabled = getattr(self.cfg.time_exclusion, 'enabled', False)
        self.time_exclusion_enabled_check.setChecked(time_exclusion_enabled)
        self.time_exclusion_enabled_check.setObjectName('time_exclusion_enabled')
        self.time_exclusion_enabled_check.setToolTip(
            "When enabled, AutoOrtho scenery will be automatically disabled at night\n"
            "based on the sun's elevation angle (sun_pitch_degrees dataref).\n"
            "X-Plane will fall back to default scenery with night lighting.\n"
            "Hysteresis prevents rapid toggling during twilight transitions."
        )
        self.time_exclusion_enabled_check.toggled.connect(self._on_time_exclusion_toggled)
        time_exclusion_layout.addWidget(self.time_exclusion_enabled_check)

        time_exclusion_layout.addSpacing(5)

        # Default to exclusion checkbox
        self.time_exclusion_default_check = QCheckBox("Start flight with exclusion active")
        default_to_exclusion = getattr(self.cfg.time_exclusion, 'default_to_exclusion', False)
        self.time_exclusion_default_check.setChecked(default_to_exclusion)
        self.time_exclusion_default_check.setObjectName('time_exclusion_default')
        self.time_exclusion_default_check.setToolTip(
            "When enabled, AutoOrtho will start with exclusion active until X-Plane starts\n"
            "sending sun position data. This ensures night flights start with default scenery.\n\n"
            "When disabled, AutoOrtho works normally until sun data confirms exclusion."
        )
        time_exclusion_layout.addWidget(self.time_exclusion_default_check)

        time_exclusion_layout.addSpacing(5)

        # Sun threshold inputs
        sun_threshold_widget = QWidget()
        sun_threshold_layout = QHBoxLayout(sun_threshold_widget)
        sun_threshold_layout.setContentsMargins(0, 0, 0, 0)

        night_threshold_label = QLabel("Night threshold:")
        sun_threshold_layout.addWidget(night_threshold_label)

        self.sun_night_threshold_spin = QDoubleSpinBox()
        self.sun_night_threshold_spin.setRange(-18.0, 0.0)
        self.sun_night_threshold_spin.setSingleStep(1.0)
        self.sun_night_threshold_spin.setSuffix("°")
        self.sun_night_threshold_spin.setValue(
            getattr(self.cfg.time_exclusion, 'sun_night_threshold', -12.0)
        )
        self.sun_night_threshold_spin.setObjectName('time_exclusion_sun_night')
        self.sun_night_threshold_spin.setToolTip(
            "Sun elevation angle to switch to night mode (exclusion active).\n"
            "-6° = civil twilight, -12° = nautical twilight, -18° = astronomical twilight"
        )
        self.sun_night_threshold_spin.setMaximumWidth(80)
        sun_threshold_layout.addWidget(self.sun_night_threshold_spin)

        sun_threshold_layout.addSpacing(20)

        day_threshold_label = QLabel("Day threshold:")
        sun_threshold_layout.addWidget(day_threshold_label)

        self.sun_day_threshold_spin = QDoubleSpinBox()
        self.sun_day_threshold_spin.setRange(-18.0, 0.0)
        self.sun_day_threshold_spin.setSingleStep(1.0)
        self.sun_day_threshold_spin.setSuffix("°")
        self.sun_day_threshold_spin.setValue(
            getattr(self.cfg.time_exclusion, 'sun_day_threshold', -10.0)
        )
        self.sun_day_threshold_spin.setObjectName('time_exclusion_sun_day')
        self.sun_day_threshold_spin.setToolTip(
            "Sun elevation angle to switch to day mode (ortho enabled).\n"
            "Should be higher than night threshold to provide hysteresis\n"
            "and prevent rapid toggling during twilight."
        )
        self.sun_day_threshold_spin.setMaximumWidth(80)
        sun_threshold_layout.addWidget(self.sun_day_threshold_spin)

        sun_threshold_layout.addStretch()
        time_exclusion_layout.addWidget(sun_threshold_widget)

        # Sun position info label
        sun_info_label = QLabel(
            "Nautical twilight (-12°) is when artificial lights dominate the landscape.\n"
            "The 2° gap between thresholds provides hysteresis to prevent flickering."
        )
        sun_info_label.setStyleSheet("color: #666; font-size: 10px; font-style: italic;")
        sun_info_label.setWordWrap(True)
        time_exclusion_layout.addWidget(sun_info_label)

        # Set initial enabled state for controls
        self._set_time_exclusion_controls_enabled(time_exclusion_enabled)

        self.settings_layout.addWidget(time_exclusion_group)

        self.settings_layout.addStretch()
        if self._settings_tracking_ready:
            self._hook_settings_widgets()

    def show_missing_color_dialog(self):
        color = QColorDialog.getColor(
            self.missing_color, self, "Select missing tile color"
        )
        if color.isValid():
            self.missing_color = color
            self.update_missing_color_button()
            self._on_settings_control_changed()

    def _get_missing_color_style(self):
        return f"""
                QPushButton {{
                    background-color: {self.missing_color.name()};
                    color: white;
                    border: 1px solid #555;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    background-color: #4A4A4A;
                    border-color: #1d71d1;
                }}
                QPushButton:pressed {{
                    background-color: #2A2A2A;
                }}
                QPushButton:disabled {{
                    background-color: #2A2A2A;
                    color: #666;
                    border-color: #333;
                }}
            """

    def update_missing_color_button(self):
        self.missing_color_button.setStyleSheet(self._get_missing_color_style())

    def reset_missing_color(self):
        self.missing_color = QColor(66, 77, 55)
        self.update_missing_color_button()

    def on_seasons_enabled_toggled(self):
        try:
            enabled = self.seasons_enabled_check.isChecked()
            self._set_seasons_controls_enabled(enabled)
        except Exception:
            pass

    def _set_seasons_controls_enabled(self, enabled):
        try:
            for slider in (
                getattr(self, 'spr_sat_slider', None),
                getattr(self, 'sum_sat_slider', None),
                getattr(self, 'fal_sat_slider', None),
                getattr(self, 'win_sat_slider', None),
            ):
                if slider is not None:
                    slider.setEnabled(enabled)
            if self.compress_dsf_check is not None:
                self.compress_dsf_check.setEnabled(enabled)
        except Exception:
            pass

    def _on_time_exclusion_toggled(self):
        """Handle night exclusion checkbox toggle."""
        try:
            enabled = self.time_exclusion_enabled_check.isChecked()
            self._set_time_exclusion_controls_enabled(enabled)
        except Exception:
            pass

    def _set_time_exclusion_controls_enabled(self, enabled):
        """Enable/disable night exclusion controls."""
        try:
            if hasattr(self, 'time_exclusion_default_check'):
                self.time_exclusion_default_check.setEnabled(enabled)
            if hasattr(self, 'sun_night_threshold_spin'):
                self.sun_night_threshold_spin.setEnabled(enabled)
            if hasattr(self, 'sun_day_threshold_spin'):
                self.sun_day_threshold_spin.setEnabled(enabled)
        except Exception:
            pass

    def refresh_scenery_list(self):
        """Refresh the catalog asynchronously."""
        if (
            self.catalog_worker is not None
            and self.catalog_worker.isRunning()
        ):
            return
        worker = ServiceWorker(
            lambda cancel_event: self.catalog_service.fetch(
                cancel_event=cancel_event
            ),
            self,
        )
        self.catalog_worker = worker
        self.task_manager.create_task(
            "catalog-refresh",
            TaskType.CATALOG,
            "Refresh scenery catalog",
            stage="Checking releases",
            cancellable=True,
            cancel_callback=worker.cancel,
            retry_callback=self.refresh_scenery_list,
        )
        worker.completed.connect(self._on_catalog_refresh_result)
        worker.finished.connect(
            lambda current=worker: self._on_catalog_worker_finished(current)
        )
        worker.start()

    def _on_catalog_refresh_result(self, result):
        if isinstance(result, Exception):
            self.task_manager.fail_task(
                "catalog-refresh",
                str(result),
            )
            return
        if not result.success:
            if result.error.code.value == "cancelled":
                self.task_manager.mark_cancelled("catalog-refresh")
            else:
                self.task_manager.fail_task(
                    "catalog-refresh",
                    result.error.message,
                )
            return
        self.task_manager.complete_task(
            "catalog-refresh",
            stage="Catalog refreshed",
        )
        self._render_scenery_list()

    def _on_catalog_worker_finished(self, worker):
        if self.catalog_worker is worker:
            self.catalog_worker = None
        task = self.task_manager.task("catalog-refresh")
        if task is not None and task.state == TaskState.CANCELLING:
            self.task_manager.mark_cancelled("catalog-refresh")

    def _render_scenery_list(self):
        """Render the latest catalog snapshot without doing I/O."""
        # Clear existing widgets
        while self.scenery_layout.count():
            child = self.scenery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for r in self.dl.regions.values():
            latest = r.get_latest_release()
            latest.parse()

            # Create scenery item frame
            item_frame = QFrame()
            item_frame.setFrameStyle(QFrame.Shape.Box)
            item_frame.setStyleSheet("""
                QFrame {
                    background-color: #2A2A2A;
                    border: 1px solid #3A3A3A;
                    border-radius: 4px;
                    padding: 10px;
                    margin: 5px;
                }
            """)

            item_layout = QVBoxLayout()
            item_frame.setLayout(item_layout)

            # Title
            title_label = QLabel(f"<b>{latest.name}</b>")
            title_label.setStyleSheet("color: #6da4e3; font-size: 16px;")
            item_layout.addWidget(title_label)

            pending_update = False
            if r.local_rel:
                self.installed_packages.append(r.region_id)
                version_label = QLabel(f"Current version: {r.local_rel.ver}")
                item_layout.addWidget(version_label)
                if version.parse(latest.ver) > version.parse(r.local_rel.ver):
                    pending_update = True
            else:
                version_label = QLabel("Not installed")
                version_label.setStyleSheet("color: #999;")
                item_layout.addWidget(version_label)
                pending_update = True

            if pending_update:
                info_label = QLabel(
                    f"Available: v{latest.ver} | "
                    f"Size: {latest.totalsize/1048576:.2f} MB | "
                    f"Downloads: {latest.download_count}"
                )
                info_label.setStyleSheet("color: #BBB;")
                item_layout.addWidget(info_label)

                # Progress bars (hidden initially)
                progress_current = QProgressBar()
                progress_current.setVisible(False)
                progress_current.setObjectName(f"progress-current-{r.region_id}")
                progress_current.setToolTip("Current file download progress")
                item_layout.addWidget(progress_current)

                progress_overall = QProgressBar()
                progress_overall.setVisible(False)
                progress_overall.setObjectName(f"progress-overall-{r.region_id}")
                progress_overall.setToolTip("Overall download progress across all files")
                item_layout.addWidget(progress_overall)

                # Install button
                install_btn = StyledButton("Install", primary=True)
                install_btn.setFixedSize(150,35)
                install_btn.setStyleSheet(
                    """
                    background-color: #2d78ba;
                    font-size: 16px;
                    font-weight: bold;
                    text-align: center;
                    line-height: 30px;
                    """
                )
                install_btn.setObjectName(f"scenery-{r.region_id}")
                install_btn.clicked.connect(
                    lambda checked, rid=r.region_id: (
                        self.on_install_scenery(rid)
                    )
                )
                item_layout.addWidget(install_btn)

            else:
                status_label = QLabel("✓ Up to date")
                status_label.setStyleSheet("color: #4CAF50;")
                item_layout.addWidget(status_label)
                delete_btn = StyledButton("Uninstall", primary=False)
                delete_btn.setObjectName(f"uninstall-{r.region_id}")
                delete_btn.setToolTip(
                    f"Uninstall the scenery package {latest.name}.\n"
                    "This will remove the scenery package from your system."
                )
                delete_btn.setStyleSheet(
                    """
                    background-color: #ba0000;
                    color: white;
                    font-size: 16px;
                    font-weight: bold;
                    text-align: center;
                    line-height: 30px;
                    """
                )
                delete_btn.setFixedSize(150,35)
                delete_btn.clicked.connect(
                    lambda checked, rid=r.region_id: (
                        self.on_delete_scenery(rid)
                    )
                )
                seasons_apply_status = latest.seasons_apply_status
                roughness_apply_status = getattr(latest, 'roughness_apply_status', downloader.RoughnessApplyStatus.NOT_APPLIED)
                roughness_value = getattr(latest, 'roughness_value', None)

                package_name = os.path.basename(latest.subfolder_dir)
                if package_name not in self.installed_package_names:
                    self.installed_package_names.append(package_name)

                # Create the scenery patches status widget
                patches_widget = SceneryPatchesWidget(
                    parent=self,
                    seasons_status=seasons_apply_status,
                    roughness_status=roughness_apply_status,
                    roughness_value=roughness_value
                )
                patches_widget.setObjectName(f"patches-widget-{package_name}")

                # Scenery Options button - unified menu for all patch options
                scenery_options_btn = StyledButton("Scenery Options", primary=False)
                scenery_options_btn.setFixedSize(150, 35)
                scenery_options_btn.setStyleSheet(
                    """
                    background-color: #2d78ba;
                    font-size: 14px;
                    font-weight: bold;
                    text-align: center;
                    line-height: 30px;
                    """
                )
                scenery_options_btn.setObjectName(f"scenery-options-{package_name}")
                scenery_options_btn.clicked.connect(
                    lambda checked, rid=package_name, ss=seasons_apply_status, rs=roughness_apply_status, rv=roughness_value: (
                        self.on_scenery_options_clicked(rid, ss, rs, rv)
                    )
                )

                h_layout = QHBoxLayout()
                h_layout.setSpacing(10)
                h_layout.addWidget(patches_widget, 1)
                buttons_col = QVBoxLayout()
                buttons_col.addStretch()
                buttons_row = QHBoxLayout()
                buttons_row.setSpacing(10)
                buttons_row.addWidget(scenery_options_btn)
                buttons_row.addWidget(delete_btn)
                buttons_col.addLayout(buttons_row)
                buttons_col.addStretch()
                h_layout.addLayout(buttons_col)

                # Progress bar for patching operations (shared between seasons and roughness)
                patch_progress_bar = QProgressBar()
                patch_progress_bar.setVisible(False)
                patch_progress_bar.setObjectName(f"dsf-progress-bar-{package_name}")
                patch_progress_bar.setToolTip("Progress of patching operations")
                patch_progress_bar.setRange(0, 100)

                item_layout.addLayout(h_layout)
                item_layout.addWidget(patch_progress_bar)


            self.scenery_layout.addWidget(item_frame)

        self.scenery_layout.addStretch()
        if self.phase3_active:
            self.scenery_library_page.set_regions(
                self.dl.regions.values(),
                self.task_manager.tasks.values(),
            )

    def on_restore_default_dsfs(self, region_id):
        """Handle restoring default DSFs"""
        self.task_manager.create_task(
            f"scenery-restore:{region_id}",
            TaskType.RESTORE,
            "Restore default scenery files",
            package=region_id,
            stage="Preparing",
            retry_callback=lambda rid=region_id: self.on_restore_default_dsfs(
                rid
            ),
        )
        # Button now is scenery-options, disable it while working
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button:
            button.setEnabled(False)
            button.setText("Working...")

        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setVisible(True)

        # Create worker thread
        worker = RestoreDefaultDsfsWorker(self.dl, region_id)
        worker.finished.connect(self.on_restore_default_dsfs_finished)
        worker.error.connect(self.on_restore_default_dsfs_error)
        worker.progress.connect(self.on_restore_default_dsfs_progress)
        # Keep a strong reference so the thread isn't GC'd while running
        worker.setParent(self)
        self.restore_default_dsfs_workers[region_id] = worker
        worker.start()

    def on_restore_default_dsfs_error(self, region_id, error_msg):
        """Handle restore default DSFs error"""
        self.task_manager.fail_task(
            f"scenery-restore:{region_id}",
            error_msg,
        )
        self.show_error.emit(f"Failed to restore default DSFs to {region_id}:\n{error_msg}")
        self.on_restore_default_dsfs_finished(region_id, False)

    def on_restore_default_dsfs_finished(self, region_id, success):
        """Handle restore default DSFs completion"""
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button:
            button.setEnabled(True)
            button.setText("Scenery Options")
        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setVisible(False)
        task_id = f"scenery-restore:{region_id}"
        task = self.task_manager.task(task_id)
        if success:
            self.task_manager.complete_task(task_id, stage="Restored")
        elif task is not None and task.state != TaskState.FAILED:
            self.task_manager.fail_task(
                task_id,
                f"Failed to restore {region_id}.",
            )
        # If this was part of a reapply flow, start add seasons next
        try:
            if success and region_id in self.reapply_after_restore:
                self.reapply_after_restore.discard(region_id)
                self._start_add_seasons_job(region_id)
                return
        except Exception:
            pass
        self.refresh_scenery_list()

    def on_restore_default_dsfs_progress(self, region_id, progress_data):
        """Update restore default DSFs progress"""
        self.task_manager.update_task(
            f"scenery-restore:{region_id}",
            stage=progress_data.get("status", "Restoring files"),
            progress=float(progress_data.get("pcnt_done", 0) or 0),
        )
        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setValue(progress_data["pcnt_done"])

    def on_scenery_options_clicked(self, region_id, seasons_status, roughness_status, roughness_value):
        """Show unified Scenery Options menu with Seasons and SUPER_ROUGHNESS options"""
        if getattr(self, 'running', False):
            QMessageBox.warning(
                self,
                "Operation Not Allowed While Running",
                "Cannot modify scenery patches while AutoOrtho is running. Please stop AutoOrtho first."
            )
            return

        # Build a styled, informative menu
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #2A2A2A;
                border: 1px solid #3A3A3A;
                padding: 6px;
            }
            QMenu::item {
                color: #E0E0E0;
                padding: 8px 14px;
                background-color: transparent;
            }
            QMenu::icon {
                padding-left: 6px;
            }
            QMenu::item:selected {
                background-color: #3A3A3A;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #666;
            }
            QMenu::separator {
                height: 1px;
                background: #3A3A3A;
                margin: 6px 8px;
            }
            """
        )

        # Use standard icons
        style = self.style()
        icon_add = style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
        icon_repair = style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        icon_reapply = style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        icon_restore = style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        icon_edit = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)

        # === SEASONS SECTION ===
        seasons_header = menu.addAction("SEASONS")
        seasons_header.setEnabled(False)
        seasons_header_font = seasons_header.font()
        seasons_header_font.setBold(True)
        seasons_header.setFont(seasons_header_font)

        # Add Seasons - only if not applied
        add_seasons_action = None
        if seasons_status == downloader.SeasonsApplyStatus.NOT_APPLIED:
            add_seasons_action = menu.addAction(icon_add, "Add Native Seasons")

        # Repair - only if partially applied
        repair_seasons_action = None
        if seasons_status == downloader.SeasonsApplyStatus.PARTIALLY_APPLIED:
            repair_seasons_action = menu.addAction(icon_repair, "Repair Seasons")

        # Reapply and Restore - only if applied or partially applied
        reapply_seasons_action = None
        restore_seasons_action = None
        if seasons_status in (downloader.SeasonsApplyStatus.PARTIALLY_APPLIED, downloader.SeasonsApplyStatus.APPLIED):
            reapply_seasons_action = menu.addAction(icon_reapply, "Reapply Seasons")
            restore_seasons_action = menu.addAction(icon_restore, "Restore Default DSFs")

        menu.addSeparator()

        # === SUPER_ROUGHNESS SECTION ===
        roughness_header = menu.addAction(
            "TERRAIN REFLECTIVITY (SUPER_ROUGHNESS)"
        )
        roughness_header.setEnabled(False)
        roughness_header_font = roughness_header.font()
        roughness_header_font.setBold(True)
        roughness_header.setFont(roughness_header_font)

        # Apply SUPER_ROUGHNESS - only if not applied
        apply_roughness_action = None
        if roughness_status == downloader.RoughnessApplyStatus.NOT_APPLIED:
            apply_roughness_action = menu.addAction(
                icon_add,
                "Apply Terrain Reflectivity…",
            )

        # Change Value - only if applied or partially applied
        change_roughness_action = None
        remove_roughness_action = None
        if roughness_status in (downloader.RoughnessApplyStatus.PARTIALLY_APPLIED, downloader.RoughnessApplyStatus.APPLIED):
            current_val_text = f" (current: {roughness_value:.1f})" if roughness_value is not None else ""
            change_roughness_action = menu.addAction(
                icon_edit,
                f"Change Terrain Reflectivity{current_val_text}",
            )
            remove_roughness_action = menu.addAction(
                icon_restore,
                "Remove Terrain Reflectivity",
            )

        # Repair roughness - only if partially applied
        repair_roughness_action = None
        if roughness_status == downloader.RoughnessApplyStatus.PARTIALLY_APPLIED:
            repair_roughness_action = menu.addAction(
                icon_repair,
                "Repair Terrain Reflectivity",
            )

        # Position menu anchored to the triggering button, falling back to cursor
        btn = self.findChild(QPushButton, f"scenery-options-{region_id}")
        global_pos = QCursor.pos() if btn is None else btn.mapToGlobal(QPoint(0, btn.height()))
        chosen = menu.exec(global_pos)
        if not chosen:
            return

        # Handle chosen action
        if chosen == add_seasons_action:
            self.on_add_seasons(region_id, seasons_status)
        elif chosen == repair_seasons_action:
            self._start_add_seasons_job(region_id)
        elif chosen == reapply_seasons_action:
            msg = (
                "Reapply will first restore all DSFs to defaults (removing seasons), "
                "and then re-apply Native Seasons.\n\nProceed?"
            )
            reply = QMessageBox.question(
                self, "Confirm Reapply", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.reapply_after_restore.add(region_id)
                self.on_restore_default_dsfs(region_id)
        elif chosen == restore_seasons_action:
            msg = "Restore will revert all DSFs to their original state and remove Native Seasons.\n\nProceed?"
            reply = QMessageBox.question(
                self, "Confirm Restore", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.on_restore_default_dsfs(region_id)
        elif chosen == apply_roughness_action:
            self._show_roughness_dialog(region_id, is_update=False, current_value=1.0)
        elif chosen == change_roughness_action:
            current = roughness_value if roughness_value is not None else 1.0
            self._show_roughness_dialog(region_id, is_update=True, current_value=current)
        elif chosen == remove_roughness_action:
            msg = (
                "This will remove terrain reflectivity settings from all "
                ".ter files.\n\nProceed?"
            )
            reply = QMessageBox.question(
                self, "Confirm Remove", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_remove_roughness_job(region_id)
        elif chosen == repair_roughness_action:
            # Re-apply with same value to fix partial application
            current = roughness_value if roughness_value is not None else 1.0
            self._start_add_roughness_job(region_id, current)

    def _show_roughness_dialog(self, region_id, is_update=False, current_value=1.0):
        """Show the SUPER_ROUGHNESS value selection dialog."""
        dialog = RoughnessValueDialog(self, current_value=current_value, is_update=is_update)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            value = dialog.get_value()
            self._start_add_roughness_job(region_id, value)

    def _start_add_roughness_job(self, region_id, roughness_value):
        """Start the SUPER_ROUGHNESS patching job."""
        self.task_manager.create_task(
            f"roughness:{region_id}",
            TaskType.ROUGHNESS,
            "Apply terrain reflectivity",
            package=region_id,
            stage="Scanning terrain files",
            retry_callback=lambda rid=region_id, value=roughness_value: (
                self._start_add_roughness_job(rid, value)
            ),
        )
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button:
            button.setEnabled(False)
            button.setText("Scanning...")

        progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if progress_bar:
            progress_bar.setVisible(True)
            progress_bar.setRange(0, 0)  # indeterminate while scanning
            progress_bar.setFormat("Scanning for .ter files...")

        # Create worker thread
        worker = AddRoughnessWorker(region_id, self.cfg.paths.scenery_path, roughness_value)
        worker.progress.connect(self.on_add_roughness_progress)
        worker.finished.connect(self.on_add_roughness_finished)
        worker.error.connect(self.on_add_roughness_error)
        worker.setParent(self)
        
        # Store worker reference
        if not hasattr(self, 'add_roughness_workers'):
            self.add_roughness_workers = {}
        self.add_roughness_workers[region_id] = worker
        worker.start()
        self._update_run_button_for_seasons_state()

    def _start_remove_roughness_job(self, region_id):
        """Start the SUPER_ROUGHNESS removal job."""
        self.task_manager.create_task(
            f"roughness:{region_id}",
            TaskType.ROUGHNESS,
            "Remove terrain reflectivity",
            package=region_id,
            stage="Preparing",
            retry_callback=lambda rid=region_id: (
                self._start_remove_roughness_job(rid)
            ),
        )
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button:
            button.setEnabled(False)
            button.setText("Removing...")

        progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if progress_bar:
            progress_bar.setVisible(True)
            progress_bar.setRange(0, 100)
            progress_bar.setValue(0)

        # Create worker thread
        worker = RestoreRoughnessWorker(region_id, self.cfg.paths.scenery_path)
        worker.progress.connect(self.on_add_roughness_progress)
        worker.finished.connect(self.on_add_roughness_finished)
        worker.error.connect(self.on_add_roughness_error)
        worker.setParent(self)
        
        # Store worker reference
        if not hasattr(self, 'restore_roughness_workers'):
            self.restore_roughness_workers = {}
        self.restore_roughness_workers[region_id] = worker
        worker.start()
        self._update_run_button_for_seasons_state()

    def on_add_roughness_progress(self, region_id, progress_data):
        """Update SUPER_ROUGHNESS patching progress."""
        stage = progress_data.get("stage")
        self.task_manager.update_task(
            f"roughness:{region_id}",
            stage=(
                "Scanning terrain files"
                if stage == "scanning"
                else progress_data.get("status", "Updating terrain files")
            ),
            progress=(
                None
                if stage == "scanning"
                else float(progress_data.get("pcnt_done", 0) or 0)
            ),
        )
        progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if not progress_bar:
            return

        stage = progress_data.get("stage")
        if stage == "scanning":
            # Show indeterminate progress while scanning for .ter files
            progress_bar.setVisible(True)
            progress_bar.setRange(0, 0)  # indeterminate / "busy" animation
            progress_bar.setFormat("Scanning for .ter files...")
            return

        if "pcnt_done" in progress_data:
            # Switch back to determinate mode once patching starts
            progress_bar.setRange(0, 100)
            progress_bar.setValue(progress_data["pcnt_done"])
            files_done = progress_data.get('files_done')
            files_total = progress_data.get('files_total')
            if files_done is not None and files_total:
                progress_bar.setFormat(f"{files_done}/{files_total}")
            else:
                progress_bar.setFormat("%p%")
            # Update button text from "Scanning..." to "Patching..."
            button = self.findChild(QPushButton, f"scenery-options-{region_id}")
            if button and button.text() == "Scanning...":
                button.setText("Patching...")

    def on_add_roughness_finished(self, region_id, success):
        """Handle SUPER_ROUGHNESS patching completion."""
        task_id = f"roughness:{region_id}"
        task = self.task_manager.task(task_id)
        if success:
            self.task_manager.complete_task(task_id, stage="Completed")
        elif task is not None and task.state != TaskState.FAILED:
            self.task_manager.fail_task(
                task_id,
                f"Terrain update failed for {region_id}.",
            )
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button:
            button.setEnabled(True)
            button.setText("Scenery Options")

        progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if progress_bar:
            progress_bar.setVisible(False)

        # Clean up worker references
        if hasattr(self, 'add_roughness_workers') and region_id in self.add_roughness_workers:
            del self.add_roughness_workers[region_id]
        if hasattr(self, 'restore_roughness_workers') and region_id in self.restore_roughness_workers:
            del self.restore_roughness_workers[region_id]

        self._update_run_button_for_seasons_state()
        self.refresh_scenery_list()

        if success:
            log.info(f"SUPER_ROUGHNESS operation completed for {region_id}")
        else:
            log.error(f"SUPER_ROUGHNESS operation failed for {region_id}")

    def on_add_roughness_error(self, region_id, error_msg):
        """Handle SUPER_ROUGHNESS patching error."""
        self.task_manager.fail_task(
            f"roughness:{region_id}",
            error_msg,
        )
        self.show_error.emit(
            f"Failed terrain reflectivity operation on {region_id}:\n"
            f"{error_msg}"
        )
        self.on_add_roughness_finished(region_id, False)

    def on_seasons_options_clicked(self, region_id):
        """Show Seasons Options menu with Repair, Reapply, Restore (legacy - kept for compatibility)"""
        if getattr(self, 'running', False):
            QMessageBox.warning(
                self,
                "Operation Not Allowed While Running",
                "Cannot modify Native Seasons while AutoOrtho is running. Please stop AutoOrtho first."
            )
            return

        # Build a styled, informative menu
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #2A2A2A;
                border: 1px solid #3A3A3A;
                padding: 6px;
            }
            QMenu::item {
                color: #E0E0E0;
                padding: 8px 14px;
                background-color: transparent;
            }
            QMenu::icon {
                padding-left: 6px;
            }
            QMenu::item:selected {
                background-color: #3A3A3A;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background: #3A3A3A;
                margin: 6px 8px;
            }
            """
        )

        # Use standard icons for better feedback
        style = self.style()
        icon_repair = style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        icon_reapply = style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        icon_restore = style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)

        repair_action = menu.addAction(icon_repair, "Repair: Try to apply seasons to missing/failed tiles")
        reapply_action = menu.addAction(icon_reapply, "Reapply:  Restore then apply seasons again (clean install)")
        menu.addSeparator()
        restore_action = menu.addAction(icon_restore, "Restore Default DSFs: Uninstall seasons and revert to default DSFs")

        # Position menu anchored to the triggering button, falling back to cursor
        btn = self.findChild(QPushButton, f"seasons-options-{region_id}")
        global_pos = QCursor.pos() if btn is None else btn.mapToGlobal(QPoint(0, btn.height()))
        chosen = menu.exec(global_pos)
        if not chosen:
            return

        if chosen == repair_action:
            msg = (
                "Repair will scan the scenery and apply Native Seasons to any DSF tiles that are missing seasons.\n\n"
                "Proceed?"
            )
            reply = QMessageBox.question(
                self,
                "Confirm Repair",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                # Reuse existing flow to start add seasons
                self._start_add_seasons_job(region_id)
            return

        if chosen == reapply_action:
            msg = (
                "Reapply will first restore all DSFs to defaults (removing seasons), and then re-apply Native Seasons to all tiles (if any are missing/failed).\n\n"
                "This is a full clean and install process. Proceed?"
            )
            reply = QMessageBox.question(
                self,
                "Confirm Reapply",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                # Flag to auto-run add seasons after restore completes
                self.reapply_after_restore.add(region_id)
                self.on_restore_default_dsfs(region_id)
            return

        if chosen == restore_action:
            msg = (
                "Restore will revert all DSFs to their original state and remove Native Seasons.\n\n"
                "Proceed?"
            )
            reply = QMessageBox.question(
                self,
                "Confirm Restore",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.on_restore_default_dsfs(region_id)
            return

    def on_delete_scenery(self, region_id):
        """Handle scenery deletion"""
        region = self.dl.regions.get(region_id)
        local_release = region.local_rel if region is not None else None
        install_path = (
            getattr(local_release, "subfolder_dir", None)
            or os.path.join(self.cfg.paths.scenery_path, "z_autoortho")
        )
        reply = QMessageBox.question(
            self,
            "Uninstall Scenery?",
            f"Uninstall {region_id}?\n\n"
            f"Files will be removed from:\n{install_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.task_manager.create_task(
            f"scenery-uninstall:{region_id}",
            TaskType.SCENERY_UNINSTALL,
            "Uninstall scenery",
            package=region_id,
            stage="Removing files",
            retry_callback=lambda rid=region_id: self.on_delete_scenery(rid),
        )

        button = self.findChild(QPushButton, f"uninstall-{region_id}")
        if button:
            button.setEnabled(False)
            button.setText("Uninstalling...")
            self.update_status_bar(f"Uninstalling {region_id}...")

        # Create worker thread
        worker = SceneryUninstallWorker(self.dl, region_id)
        worker.finished.connect(self.on_uninstall_finished)
        worker.error.connect(self.on_uninstall_error)
        # Keep a strong reference so the thread isn't GC'd while running
        worker.setParent(self)
        self.uninstall_workers[region_id] = worker
        worker.start()

    def validate_max_zoom_near_airports(self):
        """Validate max zoom near airports value"""
        if self.max_zoom_near_airports_slider.value() < self.max_zoom_slider.value():
            QMessageBox.warning(
                self,
                "Invalid Zoom Settings",
                "Maximum zoom level to near airports must be greater or equal to maximum zoom level."
            )
            self.max_zoom_near_airports_slider.blockSignals(True)
            self.max_zoom_near_airports_slider.setValue(self.max_zoom_slider.value())
            self.max_zoom_near_airports_slider.blockSignals(False)
            self.max_zoom_near_airports_label.setText(f"{self.max_zoom_slider.value()}")

    def validate_min_and_max_zoom(
        self, instigator: str
    ):
        """Validate min and max zoom values"""
        if self.min_zoom_slider.value() >= self.max_zoom_slider.value():
            QMessageBox.warning(
                self,
                "Invalid Zoom Settings",
                "Minimum zoom level must be less than maximum zoom level."
            )
            if instigator == "min":
                current_value = int(self.min_zoom_label.text())
                self.min_zoom_slider.blockSignals(True)
                self.min_zoom_slider.setValue(current_value)
                self.min_zoom_slider.blockSignals(False)
            elif instigator == "max":
                current_value = int(self.max_zoom_label.text())
                self.max_zoom_slider.blockSignals(True)
                self.max_zoom_slider.setValue(current_value)
                self.max_zoom_slider.blockSignals(False)
            else:
                raise ValueError(f"Invalid instigator: {instigator}")
        else:
            if instigator == "min":
                self.min_zoom_label.setText(f"{self.min_zoom_slider.value()}")
            elif instigator == "max":
                self.max_zoom_label.setText(f"{self.max_zoom_slider.value()}")
                if self.max_zoom_near_airports_slider.value() < self.max_zoom_slider.value():
                    self.max_zoom_near_airports_slider.blockSignals(True)
                    self.max_zoom_near_airports_slider.setValue(self.max_zoom_slider.value())
                    self.max_zoom_near_airports_slider.blockSignals(False)
                    self.max_zoom_near_airports_label.setText(f"{self.max_zoom_slider.value()}")
            else:
                raise ValueError(f"Invalid instigator: {instigator}")

    def _update_time_budget_controls(self):
        """Update enabled state of performance tuning controls based on use_time_budget checkbox."""
        use_time_budget = self.use_time_budget_check.isChecked()
        
        # Time budget controls tile-level timeout
        # Maxwait controls per-chunk timeout (always enabled, works with or without time budget)
        # When time budget is disabled, tile budget slider is disabled (falls back to legacy mode)
        
        self.tile_budget_slider.setEnabled(use_time_budget)
        self.tile_budget_label_title.setEnabled(use_time_budget)
        self.tile_budget_value_label.setEnabled(use_time_budget)
        
        # Maxwait is now always enabled - it's a per-chunk timeout that works with the tile budget
        # When time budget is enabled: maxwait limits per-chunk waits within the tile budget
        # When time budget is disabled: maxwait is the only timeout mechanism (legacy mode)
        
        # Update visual styling to indicate disabled state
        disabled_style = "color: #666;"
        enabled_style = ""
        
        self.tile_budget_label_title.setStyleSheet(enabled_style if use_time_budget else disabled_style)
        self.tile_budget_value_label.setStyleSheet(enabled_style if use_time_budget else disabled_style)

    def _update_prefetch_controls(self):
        """Update enabled state of prefetch controls based on enable checkbox."""
        enabled = self.prefetch_enabled_check.isChecked()
        
        # Existing controls
        self.prefetch_lookahead_slider.setEnabled(enabled)
        self.prefetch_lookahead_label.setEnabled(enabled)
        self.prefetch_lookahead_value.setEnabled(enabled)
        
        # New prefetch controls
        self.prefetch_interval_slider.setEnabled(enabled)
        self.prefetch_interval_label.setEnabled(enabled)
        self.prefetch_interval_value.setEnabled(enabled)
        
        self.prefetch_max_chunks_slider.setEnabled(enabled)
        self.prefetch_max_chunks_label.setEnabled(enabled)
        self.prefetch_max_chunks_value.setEnabled(enabled)
        
        # Prefetch radius (unified setting for both methods)
        self.prefetch_radius_slider.setEnabled(enabled)
        self.prefetch_radius_label.setEnabled(enabled)
        self.prefetch_radius_value.setEnabled(enabled)
        
        # Predictive DDS controls depend on prefetch being enabled
        if not enabled and self.predictive_dds_enabled_check.isChecked():
            self.predictive_dds_enabled_check.setChecked(False)
        self.predictive_dds_enabled_check.setEnabled(enabled)
        self._update_predictive_dds_controls()
    
    def _update_predictive_dds_controls(self):
        """Update enabled state of predictive DDS controls."""
        # Predictive DDS requires prefetch to be enabled
        prefetch_enabled = self.prefetch_enabled_check.isChecked()
        predictive_enabled = self.predictive_dds_enabled_check.isChecked()
        
        enabled = prefetch_enabled and predictive_enabled
        
        self.predictive_interval_slider.setEnabled(enabled)
        self.predictive_interval_label.setEnabled(enabled)
        self.predictive_interval_value.setEnabled(enabled)
        
        self.predictive_use_fallbacks_check.setEnabled(enabled)

    def _update_buffer_pool_label(self):
        """Update buffer pool label with current memory estimate based on zoom settings."""
        if not hasattr(self, 'buffer_pool_slider') or not hasattr(self, 'buffer_pool_value_label'):
            return
        
        pool_count = self.buffer_pool_slider.value()
        pool_max = self.buffer_pool_slider.maximum()
        
        # Calculate buffer size based on current UI settings
        try:
            # Check current slider/combo values (not config, since user may have changed them)
            max_zoom = self.max_zoom_slider.value() if hasattr(self, 'max_zoom_slider') else 16
            max_zoom_airports = self.max_zoom_near_airports_slider.value() if hasattr(self, 'max_zoom_near_airports_slider') else 18
            using_custom = self.using_custom_tiles_check.isChecked() if hasattr(self, 'using_custom_tiles_check') else False
            is_dynamic = hasattr(self, 'max_zoom_mode_combo') and self.max_zoom_mode_combo.currentText() == "Dynamic"
            
            # For dynamic mode, check the manager's steps for max zoom
            max_step_zoom = 16
            max_step_zoom_airports = 18
            if is_dynamic and hasattr(self, '_dynamic_zoom_manager'):
                for step in self._dynamic_zoom_manager.get_steps():
                    max_step_zoom = max(max_step_zoom, step.zoom_level)
                    max_step_zoom_airports = max(max_step_zoom_airports, step.zoom_level_airports)
            
            # Determine if 8K needed
            if using_custom:
                buffer_mb = 43
            elif is_dynamic:
                needs_8k = (max_step_zoom > 16) or (max_step_zoom_airports > 18)
                buffer_mb = 43 if needs_8k else 11
            else:
                needs_8k = (max_zoom > 16) or (max_zoom_airports > 18)
                buffer_mb = 43 if needs_8k else 11
        except Exception:
            buffer_mb = 11  # Default to 4K estimate on error
        
        # Show buffer count, memory, and whether at optimal (max)
        total_mb = pool_count * buffer_mb
        if pool_count == pool_max:
            self.buffer_pool_value_label.setText(f"{pool_count}/{pool_max} (~{total_mb}MB) optimal")
        else:
            self.buffer_pool_value_label.setText(f"{pool_count}/{pool_max} (~{total_mb}MB)")

    def _update_builder_concurrency_labels(self):
        """Update tile build workers label, RAM estimate, and buffer pool cap."""
        # Get values from sliders (with defaults if not yet created)
        prefetch = 2
        tile_workers = 8
        
        if hasattr(self, 'background_workers_slider'):
            prefetch = self.background_workers_slider.value()
            if hasattr(self, 'prefetch_workers_value'):
                self.prefetch_workers_value.setText(str(prefetch))
        
        if hasattr(self, 'live_concurrency_slider'):
            tile_workers = self.live_concurrency_slider.value()
            if hasattr(self, 'live_concurrency_value'):
                self.live_concurrency_value.setText(str(tile_workers))
        
        total_builders = prefetch + tile_workers
        
        # ═══════════════════════════════════════════════════════════════════════
        # DYNAMIC BUFFER POOL CAP
        # ═══════════════════════════════════════════════════════════════════════
        # Maximum concurrent DDS builds = prefetch + live workers.
        # More buffers than this would never be used simultaneously (waste RAM).
        # Update the slider's maximum and clamp current value if needed.
        # ═══════════════════════════════════════════════════════════════════════
        if hasattr(self, 'buffer_pool_slider'):
            current_value = self.buffer_pool_slider.value()
            new_max = total_builders
            
            # Update the slider range
            self.buffer_pool_slider.setRange(2, new_max)
            self.buffer_pool_slider.setToolTip(f"Number of pre-allocated DDS buffers (2-{new_max})")
            
            # Clamp current value if it exceeds new maximum
            if current_value > new_max:
                self.buffer_pool_slider.setValue(new_max)
            
            # Update the label to reflect any changes
            self._update_buffer_pool_label()
        
        # Calculate decoder pool size: total_builders × CPU threads
        import os
        cpu_threads = os.cpu_count() or 1
        decoder_pool_size = total_builders * cpu_threads
        
        # Minimum of 1 (no upper limit)
        decoder_pool_size = max(1, decoder_pool_size)
        
        # Memory estimates:
        # - Decoder pool (peak): ~350KB per decoder during active decode
        # - Builder pool: ~10-40MB per builder (tile memory)
        decoder_memory_mb = (decoder_pool_size * 350) / 1024  # 350KB per active decoder
        builder_memory_mb = total_builders * 15  # ~15MB average per builder
        total_memory_mb = decoder_memory_mb + builder_memory_mb
        
        if hasattr(self, 'builder_ram_label'):
            self.builder_ram_label.setText(
                f"Builders: {tile_workers} + {prefetch} prefetch = {total_builders} total, "
                f"{decoder_pool_size} decoders (~{total_memory_mb:.0f}MB peak)"
            )

    def _update_pipeline_controls(self):
        """Update enabled state of pipeline controls based on pipeline mode."""
        mode = self.pipeline_mode_combo.currentText().lower()
        
        # Buffer pool is only relevant for native/hybrid modes
        buffer_pool_enabled = mode in ('auto', 'native', 'hybrid')
        
        self.buffer_pool_slider.setEnabled(buffer_pool_enabled)
        self.buffer_pool_label.setEnabled(buffer_pool_enabled)
        self.buffer_pool_value_label.setEnabled(buffer_pool_enabled)
        
        # Update styling for disabled state
        disabled_style = "color: #666;"
        enabled_style = ""
        
        self.buffer_pool_label.setStyleSheet(enabled_style if buffer_pool_enabled else disabled_style)
        self.buffer_pool_value_label.setStyleSheet(enabled_style if buffer_pool_enabled else disabled_style)
        
        # Update tooltip to explain why disabled
        if not buffer_pool_enabled:
            self.buffer_pool_label.setToolTip(
                "Buffer pool is only used in Native and Hybrid modes.\n"
                "Select a different pipeline mode to configure this setting."
            )

    def _init_dynamic_zoom_manager(self):
        """Initialize the dynamic zoom manager from config."""
        self._dynamic_zoom_manager = DynamicZoomManager()
        existing_steps = getattr(self.cfg.autoortho, 'dynamic_zoom_steps', [])
        
        if existing_steps and existing_steps != [] and existing_steps != "[]":
            self._dynamic_zoom_manager.load_from_config(existing_steps)
        else:
            # Initialize with current fixed max zoom values as base step
            max_zoom = int(self.cfg.autoortho.max_zoom)
            max_zoom_airports = int(self.cfg.autoortho.max_zoom_near_airports)
            self._dynamic_zoom_manager.set_base_zoom(max_zoom, max_zoom_airports)
        
        self._update_dynamic_summary()

    def _on_zoom_mode_changed(self, mode: str):
        """Handle zoom mode toggle between Fixed and Dynamic."""
        is_dynamic = mode == "Dynamic"
        
        # If switching to dynamic for first time and no steps configured
        if is_dynamic and self._dynamic_zoom_manager.is_empty():
            # Initialize with current fixed max zoom values
            max_zoom = int(self.cfg.autoortho.max_zoom)
            max_zoom_airports = int(self.cfg.autoortho.max_zoom_near_airports)
            self._dynamic_zoom_manager.set_base_zoom(max_zoom, max_zoom_airports)
            self._update_dynamic_summary()
        
        self._update_zoom_mode_visibility()
        self._update_buffer_pool_label()

    def _update_zoom_mode_visibility(self):
        """Show/hide zoom controls based on selected mode."""
        is_dynamic = self.max_zoom_mode_combo.currentText() == "Dynamic"
        self.fixed_zoom_widget.setVisible(not is_dynamic)
        self.dynamic_zoom_widget.setVisible(is_dynamic)
        # Also hide/show the airport zoom slider (only visible in fixed mode AND not using custom tiles)
        if hasattr(self, 'max_zoom_near_airports_widget'):
            using_custom_tiles = self.cfg.autoortho.using_custom_tiles
            self.max_zoom_near_airports_widget.setVisible(not is_dynamic and not using_custom_tiles)

    def _open_quality_steps_dialog(self):
        """Open the quality steps configuration dialog."""
        dialog = QualityStepsDialog(
            self, 
            manager=self._dynamic_zoom_manager,
            current_max_zoom=int(self.cfg.autoortho.max_zoom)
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._dynamic_zoom_manager = dialog.get_manager()
            if self.phase3_active:
                self.dynamic_zoom_editor.set_manager(
                    self._dynamic_zoom_manager
                )
            self._update_dynamic_summary()
            self._update_buffer_pool_label()
            self._on_settings_control_changed()

    def _update_dynamic_summary(self):
        """Update the summary label for dynamic zoom steps."""
        if not hasattr(self, '_dynamic_zoom_manager') or not hasattr(self, 'dynamic_zoom_summary'):
            return
        
        steps = self._dynamic_zoom_manager.get_steps()
        if not steps:
            self.dynamic_zoom_summary.setText("No steps configured")
        else:
            # Show steps sorted by altitude (ascending for display)
            # Format: ZL{normal}/ZL{airports}@{altitude}ft
            sorted_steps = sorted(steps, key=lambda s: s.altitude_ft)
            text = ", ".join(
                f"ZL{s.zoom_level}/ZL{s.zoom_level_airports}@{s.altitude_ft:+}ft" 
                for s in sorted_steps[:3]
            )
            if len(sorted_steps) > 3:
                text += f" (+{len(sorted_steps)-3} more)"
            self.dynamic_zoom_summary.setText(text)

    def _update_fallback_extends_control(self):
        """Update enabled state of fallback_extends_budget and timeout based on fallback level.
        
        The 'allow fallbacks to extend budget' option is only relevant when
        fallback_level is 'Full' (index 2), since that's the only level that
        does network fallbacks.
        
        The fallback timeout slider is only relevant when extends_budget is enabled.
        """
        is_full_fallback = self.fallback_level_combo.currentIndex() == 2
        extends_budget = self.fallback_extends_budget_check.isChecked()
        
        # Enable extends_budget checkbox only for Full fallback
        self.fallback_extends_budget_check.setEnabled(is_full_fallback)
        
        # Enable timeout slider only when Full fallback AND extends_budget is checked
        timeout_enabled = is_full_fallback and extends_budget
        self.fallback_timeout_slider.setEnabled(timeout_enabled)
        self.fallback_timeout_label.setEnabled(timeout_enabled)
        self.fallback_timeout_value_label.setEnabled(timeout_enabled)
        
        # Style for enabled/disabled labels
        enabled_style = ""
        disabled_style = "color: #666666;"
        self.fallback_timeout_label.setStyleSheet(enabled_style if timeout_enabled else disabled_style)
        self.fallback_timeout_value_label.setStyleSheet(enabled_style if timeout_enabled else disabled_style)
        
        # Update tooltips to explain why controls are disabled
        if is_full_fallback:
            self.fallback_extends_budget_check.setToolTip(
                "When enabled, adds EXTRA time after the main budget expires\n"
                "to recover missing chunks using lower-detail fallbacks.\n\n"
                "How it works:\n"
                "  • Main budget expires → 'Fallback Sweep' phase begins\n"
                "  • All missing chunks are processed with lower-zoom alternatives\n"
                "  • Maximum total time = Main budget + Extended fallback timeout\n\n"
                "• Enabled: Better quality, fewer gray patches (recommended)\n"
                "• Disabled: Strict timing, may have missing tiles"
            )
        else:
            self.fallback_extends_budget_check.setToolTip(
                "This option only applies when 'Full (Best Quality)' fallback is selected.\n"
                "Select 'Full' fallback level to enable this option."
            )
        
        if timeout_enabled:
            self.fallback_timeout_label.setToolTip(
                "TOTAL extra time for the fallback sweep phase.\n"
                "This time is added AFTER the main budget expires to recover\n"
                "all missing chunks using lower-detail alternatives.\n\n"
                "Maximum total tile time = Main budget + This value\n"
                "Example: 300s main + 30s fallback = 330s maximum\n\n"
                "The fallback sweep efficiently processes ALL missing chunks\n"
                "in batch, maximizing image quality within the extra time."
            )
        else:
            self.fallback_timeout_label.setToolTip(
                "Enable 'Allow fallbacks to extend time budget' to configure this setting."
            )

    def _fallback_str_to_index(self, value):
        """Convert fallback_level config value to combo box index.
        
        Handles both new string values (none, cache, full) and legacy integer values.
        """
        if isinstance(value, str):
            value_lower = value.lower().strip()
            if value_lower == 'none':
                return 0
            elif value_lower == 'cache':
                return 1
            elif value_lower == 'full':
                return 2
            else:
                # Try parsing as integer for backwards compatibility
                try:
                    return max(0, min(2, int(value)))
                except ValueError:
                    return 1  # Default to cache
        elif isinstance(value, bool):
            # Handle SectionParser converting '0' to False, '1' to True
            return 2 if value else 0
        elif isinstance(value, int):
            return max(0, min(2, value))
        else:
            return 1  # Default to cache
    
    def _fallback_index_to_str(self, index):
        """Convert combo box index to fallback_level config string."""
        return ['none', 'cache', 'full'][max(0, min(2, index))]

    def validate_threads(self, value):
        """Validate fetch threads value and show warning if too high"""
        max_threads = os.cpu_count() or 1
        if value > max_threads:
            QMessageBox.information(
                self,
                "Thread Limit",
                f"Number of threads cannot be greater than {max_threads} "
                f"(available CPU threads on this machine).\n"
                f"Value has been adjusted to {max_threads}."
            )
            self.fetch_threads_spinbox.setValue(max_threads)

    def on_simheaven_compat_check(self, state):
        """Handle SimHeaven compatibility check"""
        if state == Qt.CheckState.Checked:
            self.cfg.autoortho.simheaven_compat = True
        else:
            self.cfg.autoortho.simheaven_compat = False

    def _on_simbrief_userid_changed(self, text):
        """Handle SimBrief User ID text change"""
        self._update_simbrief_ui_state()
        # Update config
        if hasattr(self.cfg, 'simbrief'):
            self.cfg.simbrief.userid = text.strip()

    def _update_simbrief_ui_state(self):
        """Update SimBrief UI visibility based on current state"""
        userid = self.simbrief_userid_edit.text().strip()
        has_userid = bool(userid)
        
        # Show/hide fetch button based on whether we have a user ID
        self.simbrief_fetch_btn.setVisible(has_userid)
        
        # Hide error when user ID changes
        self.simbrief_error_label.hide()
        
        # If no user ID and we had flight data, clear it
        if not has_userid:
            self.simbrief_info_frame.hide()
            self.simbrief_use_flight_data_check.hide()
            self.simbrief_route_settings_frame.hide()
            self.simbrief_unload_btn.hide()
            self.simbrief_flight_data = None
            
            # Clear the flight manager
            simbrief_flight_manager.clear()
        else:
            # If we have a userid and use_flight_data was previously enabled in config,
            # show the checkbox and route settings so user can see the current state
            use_flight_data = False
            if hasattr(self.cfg, 'simbrief'):
                use_flight_data = getattr(self.cfg.simbrief, 'use_flight_data', False)
                if isinstance(use_flight_data, str):
                    use_flight_data = use_flight_data.lower() in ('true', '1', 'yes', 'on')
            
            if use_flight_data or self.simbrief_use_flight_data_check.isChecked():
                # Show the checkbox so user can see the current state
                self.simbrief_use_flight_data_check.show()
                # Show route settings if the checkbox is checked
                if self.simbrief_use_flight_data_check.isChecked():
                    self.simbrief_route_settings_frame.show()

    def _on_simbrief_fetch(self):
        """Handle SimBrief fetch button click"""
        userid = self.simbrief_userid_edit.text().strip()
        if not userid:
            return
        
        # Disable button while fetching
        self.simbrief_fetch_btn.setEnabled(False)
        self.simbrief_fetch_btn.setText("Fetching...")
        self.simbrief_error_label.hide()
        
        # Create and start worker
        self.simbrief_fetch_worker = SimBriefFetchWorker(userid)
        self.task_manager.create_task(
            "simbrief-fetch",
            TaskType.SIMBRIEF,
            "Fetch SimBrief flight plan",
            stage="Connecting to SimBrief",
            cancellable=True,
            cancel_callback=self.simbrief_fetch_worker.cancel,
            retry_callback=self._on_simbrief_fetch,
        )
        self.simbrief_fetch_worker.success.connect(self._on_simbrief_fetch_success)
        self.simbrief_fetch_worker.error.connect(self._on_simbrief_fetch_error)
        self.simbrief_fetch_worker.finished.connect(self._on_simbrief_fetch_finished)
        self.simbrief_fetch_worker.start()

    def _on_simbrief_fetch_success(self, data):
        """Handle successful SimBrief fetch"""
        self.task_manager.complete_task(
            "simbrief-fetch",
            stage="Flight plan loaded",
        )
        self.simbrief_flight_data = data
        self._display_simbrief_flight_info(data)
        self.simbrief_info_frame.show()
        self.simbrief_error_label.hide()
        self.simbrief_use_flight_data_check.show()  # Show toggle when flight data loaded
        self.simbrief_unload_btn.show()  # Show unload button when flight is loaded
        
        # Load flight data into the global flight manager for use by dynamic zoom and prefetcher
        if simbrief_flight_manager.load_flight_data(data):
            log.info(f"SimBrief flight loaded into manager: {simbrief_flight_manager.origin} -> {simbrief_flight_manager.destination}")
        else:
            log.warning("Failed to load SimBrief flight data into manager")
        
        log.info("SimBrief flight data fetched successfully")

    def _on_simbrief_fetch_error(self, error_msg):
        """Handle SimBrief fetch error"""
        self.task_manager.fail_task("simbrief-fetch", error_msg)
        self.simbrief_error_label.setText(f"⚠ {error_msg}")
        self.simbrief_error_label.show()
        self.simbrief_info_frame.show()
        self.simbrief_route_label.setText("")
        self.simbrief_details_label.setText("")
        self.simbrief_use_flight_data_check.hide()  # Hide toggle on error
        self.simbrief_route_settings_frame.hide()  # Hide route settings on error
        self.simbrief_unload_btn.hide()  # Hide unload button on error
        self.simbrief_flight_data = None
        if self.phase3_active:
            self.flight_plan_page.clear_flight_data()
        
        # Clear the flight manager
        simbrief_flight_manager.clear()
        
        log.warning(f"SimBrief fetch error: {error_msg}")

    def _on_simbrief_fetch_finished(self):
        """Handle SimBrief fetch completion (success or error)"""
        task = self.task_manager.task("simbrief-fetch")
        if task is not None and task.state == TaskState.CANCELLING:
            self.task_manager.mark_cancelled("simbrief-fetch")
        self.simbrief_fetch_btn.setEnabled(True)
        self.simbrief_fetch_btn.setText("Fetch Flight Data")

    def _on_simbrief_unload(self):
        """Handle SimBrief unload button click - clears flight data"""
        # Clear stored flight data
        self.simbrief_flight_data = None
        
        # Clear the global flight manager
        simbrief_flight_manager.clear()
        
        # Hide flight info UI
        self.simbrief_info_frame.hide()
        self.simbrief_use_flight_data_check.hide()
        self.simbrief_route_settings_frame.hide()
        self.simbrief_unload_btn.hide()
        self.simbrief_error_label.hide()
        if self.phase3_active:
            self.flight_plan_page.clear_flight_data()
        
        # Uncheck the toggle since there's no flight data
        self.simbrief_use_flight_data_check.setChecked(False)
        
        log.info("SimBrief flight data unloaded")

    def _on_use_flight_data_changed(self, state):
        """
        Immediately update config when the 'Use Flight Data' toggle changes.
        
        This allows users to load SimBrief data after pressing Run and have
        it take effect immediately without needing to save config.
        """
        is_checked = (state == 2)  # Qt.CheckState.Checked has value 2
        
        # Show/hide route settings based on checkbox state
        self.simbrief_route_settings_frame.setVisible(is_checked)
        
        if hasattr(self.cfg, 'simbrief'):
            self.cfg.simbrief.use_flight_data = is_checked
            log.debug(f"SimBrief use_flight_data toggled: {self.cfg.simbrief.use_flight_data}")
        if self.phase3_active:
            self.flight_plan_page.set_influence(
                is_checked and self.simbrief_flight_data is not None
            )

    def _on_route_consideration_radius_changed(self, value):
        """Handle route consideration radius spinbox change"""
        if hasattr(self.cfg, 'simbrief'):
            self.cfg.simbrief.route_consideration_radius_nm = value
            log.debug(f"SimBrief route_consideration_radius_nm changed: {value}")

    def _on_route_deviation_threshold_changed(self, value):
        """Handle route deviation threshold spinbox change"""
        if hasattr(self.cfg, 'simbrief'):
            self.cfg.simbrief.route_deviation_threshold_nm = value
            log.debug(f"SimBrief route_deviation_threshold_nm changed: {value}")
    
    def _on_prefetch_while_parked_changed(self, state):
        """Handle prefetch while parked checkbox change"""
        is_checked = (state == 2)  # Qt.CheckState.Checked has value 2
        if hasattr(self.cfg, 'simbrief'):
            self.cfg.simbrief.prefetch_while_parked = is_checked
            log.debug(f"SimBrief prefetch_while_parked changed: {is_checked}")

    def _display_simbrief_flight_info(self, data):
        """Display SimBrief flight information in the UI"""
        try:
            origin = data.get('origin', {})
            destination = data.get('destination', {})
            general = data.get('general', {})
            aircraft = data.get('aircraft', {})
            times = data.get('times', {})
            navlog = data.get('navlog', {})
            
            # Route header
            origin_icao = origin.get('icao_code', '????')
            dest_icao = destination.get('icao_code', '????')
            origin_name = origin.get('name', '')
            dest_name = destination.get('name', '')
            
            route_text = f"{origin_icao} → {dest_icao}"
            self.simbrief_route_label.setText(route_text)
            
            # Flight details
            flight_number = general.get('flight_number', 'N/A')
            aircraft_icao = aircraft.get('icaocode', 'N/A')
            aircraft_name = aircraft.get('name', '')
            
            # Calculate flight time
            est_time_enroute = times.get('est_time_enroute', '0')
            try:
                ete_seconds = int(est_time_enroute)
                hours = ete_seconds // 3600
                minutes = (ete_seconds % 3600) // 60
                flight_time = f"{hours}h {minutes:02d}m"
            except (ValueError, TypeError):
                flight_time = "N/A"
            
            # Get cruise altitude and format as flight level
            cruise_altitude_raw = general.get('initial_altitude', '')
            try:
                cruise_alt_ft = int(cruise_altitude_raw)
                # Flight level = altitude / 100 (e.g., 30000 ft = FL300)
                cruise_altitude = f"FL{cruise_alt_ft // 100}"
            except (ValueError, TypeError):
                cruise_altitude = cruise_altitude_raw if cruise_altitude_raw else "N/A"
            
            # Get number of waypoints
            fixes = navlog.get('fix', [])
            num_fixes = len(fixes) if isinstance(fixes, list) else 0
            
            # Get route
            route_str = general.get('route', '')
            # Truncate if too long
            if len(route_str) > 80:
                route_str = route_str[:77] + "..."
            
            details = (
                f"<b>Flight:</b> {flight_number}<br>"
                f"<b>Aircraft:</b> {aircraft_icao} ({aircraft_name})<br>"
                f"<b>Route:</b> {origin_name} → {dest_name}<br>"
                f"<b>Cruise Altitude:</b> {cruise_altitude}<br>"
                f"<b>Est. Flight Time:</b> {flight_time}<br>"
                f"<b>Waypoints:</b> {num_fixes} fixes"
            )
            
            if route_str:
                details += f"<br><b>Route:</b> <span style='color: #aaa; font-size: 11px;'>{route_str}</span>"
            
            self.simbrief_details_label.setText(details)
            if self.phase3_active:
                self.flight_plan_page.set_flight_data(data)
            
        except Exception as e:
            log.error(f"Error displaying SimBrief flight info: {e}")
            self.simbrief_error_label.setText(f"Error parsing flight data: {str(e)}")
            self.simbrief_error_label.show()

    def browse_folder(self, line_edit):
        """Open folder browser dialog"""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            line_edit.text()
        )
        if folder:
            line_edit.setText(folder)

    def _current_configuration_input(self):
        return ConfigurationInput(
            xplane_path=self.xplane_path_edit.text(),
            scenery_path=self.scenery_path_edit.text(),
            cache_dir=self.cache_dir_edit.text(),
            long_term_cache_dir=self.lt_cache_dir_edit.text(),
            download_dir=self.download_dir_edit.text(),
            webui_port=self.webui_port_edit.text(),
            xplane_udp_port=self.xplane_udp_port_edit.text(),
        )

    def _validation_field_widgets(self):
        return {
            "xplane_path": self.xplane_path_edit,
            "scenery_path": self.scenery_path_edit,
            "cache_dir": self.cache_dir_edit,
            "long_term_cache_dir": self.lt_cache_dir_edit,
            "download_dir": self.download_dir_edit,
            "webui_port": self.webui_port_edit,
            "xplane_udp_port": self.xplane_udp_port_edit,
        }

    def _clear_validation_feedback(self):
        self.setup_validation_label.hide()
        self.setup_validation_label.clear()
        for widget in self._validation_field_widgets().values():
            widget.setProperty("validationError", False)
            original_tooltip = widget.property("validationOriginalToolTip")
            if original_tooltip is not None:
                widget.setToolTip(original_tooltip)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _show_validation_issues(self, issues):
        self._clear_validation_feedback()
        errors = [
            issue for issue in issues
            if issue.severity == ValidationSeverity.ERROR
        ]
        if not errors:
            return

        widgets = self._validation_field_widgets()
        for issue in errors:
            widget = widgets.get(issue.field)
            if widget is not None:
                if widget.property("validationOriginalToolTip") is None:
                    widget.setProperty(
                        "validationOriginalToolTip",
                        widget.toolTip(),
                    )
                widget.setProperty("validationError", True)
                widget.setToolTip(issue.message)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

        self.setup_validation_label.setText(
            "\n".join(f"• {issue.message}" for issue in errors)
        )
        self.setup_validation_label.show()
        announce_accessible(
            self.setup_validation_label,
            self.setup_validation_label.text(),
        )

        if errors[0].field == "scenery":
            self.navigate_to("scenery-library")
        elif errors[0].field in ("webui_port", "xplane_udp_port"):
            self.navigate_to("settings", "Flight Data")
        else:
            self.navigate_to("settings", "Paths & Storage")
        first_widget = widgets.get(errors[0].field)
        if first_widget is not None:
            first_widget.setFocus()

    def _prepare_runtime_directories(self):
        issues = []
        paths = (
            ("scenery_path", "Scenery install folder", self.scenery_path_edit.text()),
            ("cache_dir", "Image cache folder", self.cache_dir_edit.text()),
            ("download_dir", "Temporary download folder", self.download_dir_edit.text()),
        )
        long_term = self.lt_cache_dir_edit.text().strip()
        if long_term:
            paths += (
                ("long_term_cache_dir", "Long-term cache folder", long_term),
            )

        for field, label, path in paths:
            try:
                os.makedirs(os.path.expanduser(path), exist_ok=True)
            except OSError as exc:
                issues.append(
                    ValidationIssue(
                        field,
                        ValidationSeverity.ERROR,
                        f"{label} could not be created: {exc}",
                    )
                )
        return issues

    def _has_active_ui_jobs(self):
        worker_maps = (
            self.download_workers,
            self.uninstall_workers,
            self.add_seasons_workers,
            self.restore_default_dsfs_workers,
            getattr(self, "add_roughness_workers", {}),
            getattr(self, "restore_roughness_workers", {}),
        )
        return (
            any(bool(workers) for workers in worker_maps)
            or self.add_seasons_current is not None
            or (
                self.cache_thread is not None
                and self.cache_thread.isRunning()
            )
        )

    def _set_runtime_state(self, state, message=None):
        self.runtime_state = RuntimeState(state)
        self.running = self.runtime_state == RuntimeState.RUNNING

        labels = {
            RuntimeState.STOPPED: ("Start Streaming", True),
            RuntimeState.STARTING: ("Starting…", False),
            RuntimeState.RUNNING: ("Stop Streaming", True),
            RuntimeState.STOPPING: ("Stopping…", False),
            RuntimeState.ERROR: ("Retry Streaming", True),
        }
        button_text, button_enabled = labels[self.runtime_state]
        self.run_button.setText(button_text)
        action_enabled = button_enabled and not self._has_active_ui_jobs()
        self.run_button.setEnabled(action_enabled)

        editable = self.runtime_state in (
            RuntimeState.STOPPED,
            RuntimeState.ERROR,
        )
        transitioning = self.runtime_state in (
            RuntimeState.STARTING,
            RuntimeState.STOPPING,
        )

        self.tabs.setTabEnabled(
            self.tabs.indexOf(self.setup_widget),
            not transitioning,
        )
        self.tabs.setTabEnabled(
            self.tabs.indexOf(self.scenery_widget),
            editable,
        )
        self.tabs.setTabEnabled(
            self.tabs.indexOf(self.settings_widget),
            editable,
        )
        self.save_button.setEnabled(not transitioning)

        self.paths_group.setEnabled(editable)
        self.showconfig_check.setEnabled(editable)
        self.simheaven_compat_check.setEnabled(editable)
        self.using_custom_tiles_check.setEnabled(editable)
        self.maptype_combo.setEnabled(
            self.runtime_state in (
                RuntimeState.STOPPED,
                RuntimeState.RUNNING,
                RuntimeState.ERROR,
            )
        )
        self.simbrief_group.setEnabled(not transitioning)
        self._update_settings_actions()
        if self.phase3_active:
            self.shell.set_runtime_state(
                self.runtime_state,
                action_enabled=action_enabled,
            )
            self.scenery_library_page.set_runtime_locked(
                self.runtime_state != RuntimeState.STOPPED
                and self.runtime_state != RuntimeState.ERROR
            )

        if message:
            self.update_status_bar(message)

    def _start_mount_control(
        self,
        action,
        *,
        lingering_mounts=None,
        stop_target=RuntimeState.STOPPED,
    ):
        if (
            self.mount_control_worker is not None
            and self.mount_control_worker.isRunning()
        ):
            return False

        if action == "start":
            self._set_runtime_state(
                RuntimeState.STARTING,
                "Starting scenery streaming…",
            )
        else:
            self._stop_target_state = RuntimeState(stop_target)
            self._set_runtime_state(
                RuntimeState.STOPPING,
                "Stopping scenery streaming…",
            )

        self.task_manager.create_task(
            "mount-control",
            TaskType.MOUNT,
            (
                "Start scenery streaming"
                if action == "start"
                else "Stop scenery streaming"
            ),
            stage=("Starting mounts" if action == "start" else "Unmounting"),
            retry_callback=(
                self.on_run
                if action == "start"
                else lambda: self._start_mount_control(
                    "stop",
                    stop_target=RuntimeState.ERROR,
                )
            ),
        )

        worker = MountControlWorker(
            self,
            action,
            lingering_mounts=lingering_mounts,
        )
        worker.completed.connect(self._on_mount_control_completed)
        worker.finished.connect(
            lambda current=worker: self._on_mount_control_thread_finished(
                current
            )
        )
        self.mount_control_worker = worker
        worker.start()
        return True

    def _on_mount_control_completed(self, action, success, message):
        if success:
            self.task_manager.complete_task(
                "mount-control",
                stage=message,
            )
        else:
            self.task_manager.fail_task("mount-control", message)
        if action == "start":
            if success:
                self._runtime_error_message = ""
                self._restart_pending = False
                self._set_runtime_state(RuntimeState.RUNNING, message)
                self.mount_monitor_timer.start()
            else:
                self._close_after_stop = False
                self._runtime_error_message = message
                self._set_runtime_state(RuntimeState.ERROR, message)
                self.display_error(
                    f"AutoOrtho could not start streaming.\n\n{message}"
                )
            return

        self.mount_monitor_timer.stop()
        if success:
            target = self._stop_target_state
            target_message = (
                self._runtime_error_message
                if target == RuntimeState.ERROR
                else message
            )
            self._set_runtime_state(target, target_message)
            if self._close_after_stop:
                self._close_after_stop = False
                QTimer.singleShot(0, self.close)
        else:
            self._close_after_stop = False
            self._runtime_error_message = message
            self._set_runtime_state(RuntimeState.ERROR, message)
            self.display_error(
                f"AutoOrtho could not stop streaming cleanly.\n\n{message}"
            )

    def _on_mount_control_thread_finished(self, worker):
        if self.mount_control_worker is worker:
            self.mount_control_worker = None

    def _check_mount_workers(self):
        if self.runtime_state != RuntimeState.RUNNING:
            return
        handles = list(getattr(self, "mount_workers", []))
        dead = [
            handle for handle in handles
            if handle.process.poll() is not None
        ]
        if handles and not dead:
            return

        self.mount_monitor_timer.stop()
        if dead:
            detail = (
                f"Mount worker for {dead[0].mountpoint} exited with "
                f"code {dead[0].process.poll()}."
            )
        else:
            detail = "No active mount workers remain."
        self._runtime_error_message = detail
        self._start_mount_control(
            "stop",
            stop_target=RuntimeState.ERROR,
        )

    def _request_stop_streaming(self, stop_target=RuntimeState.STOPPED):
        if self.runtime_state != RuntimeState.RUNNING:
            return
        self._start_mount_control("stop", stop_target=stop_target)

    def on_run(self):
        """Start or stop scenery streaming based on current runtime state."""
        if self.runtime_state == RuntimeState.RUNNING:
            self._request_stop_streaming()
            return
        if self.runtime_state not in (
            RuntimeState.STOPPED,
            RuntimeState.ERROR,
        ):
            return
        if not self._resolve_pending_settings(for_start=True):
            self.update_status_bar("Start cancelled.")
            return
        readiness = self._readiness_for_start()
        if readiness is None:
            return
        if not readiness.can_finish:
            blocking = [
                check for check in readiness.checks
                if check.status != ReadinessStatus.SUCCESS
            ]
            if any(check.id == "setup-scenery" for check in blocking):
                self.navigate_to("scenery-library")
            else:
                self.navigate_to("settings", "Paths & Storage")
            QMessageBox.warning(
                self,
                "AutoOrtho Is Not Ready",
                "\n".join(
                    f"• {check.title}: {check.message}"
                    for check in blocking
                ),
            )
            self.update_status_bar(
                "Streaming blocked: complete the readiness checks."
            )
            return
        if self._has_active_ui_jobs():
            QMessageBox.warning(
                self,
                "Operation In Progress",
                "Wait for active scenery or cache operations to finish before "
                "starting streaming.",
            )
            return
        if not self.verify():
            self.update_status_bar(
                "Streaming blocked: correct the highlighted configuration."
            )
            return

        directory_issues = self._prepare_runtime_directories()
        if directory_issues:
            self._show_validation_issues(directory_issues)
            self.update_status_bar(
                "Streaming blocked: required folders could not be created."
            )
            return

        lingering_mounts = self.preflight_mount_check_and_prompt()
        if lingering_mounts is None:
            self.update_status_bar("Start cancelled.")
            return

        self._start_mount_control(
            "start",
            lingering_mounts=lingering_mounts,
        )

    def _on_maptype_combo_changed(self, text):
        """Show the Switch button when maptype is changed while running."""
        if self.running and text != self.cfg.autoortho.maptype_override:
            self.maptype_switch_btn.setVisible(True)
        else:
            self.maptype_switch_btn.setVisible(False)

    def _on_maptype_switch(self):
        """Apply the new maptype to all live TileCacher instances."""
        import platform

        if self.settings_session.dirty and not self.on_save():
            return
        new_maptype = self.maptype_combo.currentText()
        self.cfg.autoortho.maptype_override = new_maptype

        if hasattr(self, 'mac_os_procs') and self.mac_os_procs:
            # TileCachers live in mount worker processes on all platforms.
            # Stats provides the cross-platform reload signal; SIGUSR1 is an
            # immediate POSIX nudge when available.
            try:
                try:
                    from autoortho import aostats
                    from autoortho.mount_worker import RELOAD_GENERATION_STAT
                except ImportError:
                    import aostats
                    from mount_worker import RELOAD_GENERATION_STAT
                import time
                aostats.set_stat(RELOAD_GENERATION_STAT, int(time.time() * 1000))
            except Exception as e:
                log.warning(f"Failed to publish worker reload signal: {e}")

            import signal
            sigusr1 = getattr(signal, "SIGUSR1", None)
            if sigusr1 is not None:
                for p in self.mac_os_procs:
                    try:
                        if p.poll() is None:
                            p.send_signal(sigusr1)
                            log.info(f"Sent SIGUSR1 to worker pid {p.pid}")
                    except Exception as e:
                        log.warning(f"Failed to signal worker pid {p.pid}: {e}")
        else:
            # Legacy in-process direct mount path.
            import gc
            import getortho
            for obj in gc.get_objects():
                try:
                    if isinstance(obj, getortho.TileCacher):
                        obj.maptype_override = new_maptype
                        if new_maptype == "Custom Map":
                            from utils.custom_map import get_custom_map_config
                            obj.custom_map = get_custom_map_config()
                        elif new_maptype == "APPLE":
                            from utils.apple_token_service import apple_token_service
                            apple_token_service.reset_apple_maps_token()
                        else:
                            obj.custom_map = None
                        log.info(f"Live-switched TileCacher maptype to {new_maptype}")
                except Exception:
                    pass

        self.maptype_switch_btn.setVisible(False)
        self.update_status_bar(f"Map type switched to {new_maptype}")

    def on_save(self):
        """Validate and apply the current settings session."""
        restart_required = self.settings_session.restart_required
        issues = validate_configuration(
            self._current_configuration_input(),
            scenery_mounts=self.cfg.scenery_mounts,
            require_installed_scenery=False,
        )
        errors = [
            issue for issue in issues
            if issue.severity == ValidationSeverity.ERROR
        ]
        if errors:
            self._show_validation_issues(errors)
            self.update_status_bar(
                "Save blocked: correct the highlighted configuration."
            )
            return False

        # Check if the directory exists
        scenery_path = self.scenery_path_edit.text()
        if not os.path.isdir(scenery_path):
            reply = QMessageBox.question(
                self,
                'Create Folder?',
                f"The directory '{scenery_path}' does not exist. Do you want to create it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.No:
                self.update_status_bar("Apply cancelled.")
                return False
        directory_issues = self._prepare_runtime_directories()
        if directory_issues:
            self._show_validation_issues(directory_issues)
            self.update_status_bar(
                "Apply blocked: required folders could not be created."
            )
            return False
        # Check if program is already running
        if self.running:
            reply = QMessageBox.question(
                self,
                "Apply Settings While Running",
                "Some settings will not take effect until streaming is "
                "restarted.\n\nApply the settings anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            
            if reply == QMessageBox.StandardButton.No:
                self.update_status_bar("Apply cancelled")
                return False
        
        if not self.save_config():
            self.update_status_bar(
                "Apply failed: configuration could not be saved."
            )
            return False
        self.refresh_scenery_list()
        snapshot = self._snapshot_settings()
        self.settings_session.mark_applied(snapshot)
        self._restart_pending = bool(self.running and restart_required)
        self._update_settings_actions()

        if self._restart_pending:
            self.update_status_bar(
                "Configuration applied — restart streaming to apply all changes."
            )
            QMessageBox.information(
                self,
                "Settings Applied",
                "Settings were saved. Restart streaming to apply all changes.",
            )
        else:
            self.update_status_bar("Configuration applied")
        return True

    def on_delete_cache(self):
        reply = QMessageBox.question(
            self,
            "Delete Entire Cache?",
            "Delete all cached imagery?\n\n"
            f"Cache location:\n{self.cfg.paths.cache_dir}\n\n"
            "AutoOrtho will need to download this imagery again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.on_clean_cache(delete_all=True)

    def on_clean_jpegs(self):
        """Handle Clean JPEG Files button click - only deletes legacy JPEG files"""
        if self.running:
            QMessageBox.warning(
                self,
                "Cannot clean cache while running",
                "Cannot clean JPEG files while AutoOrtho injection is running. Please stop AutoOrtho and try again."
            )
            return

        self.update_status_bar("Cleaning JPEG files...")
        self._set_cache_buttons_enabled(False)
        worker = CacheCleanupWorker(self, "jpeg")
        self.task_manager.create_task(
            "cache-cleanup",
            TaskType.CACHE,
            "Clean JPEG cache",
            stage="Scanning cache",
            cancellable=True,
            cancel_callback=worker.cancel,
            retry_callback=self.on_clean_jpegs,
        )
        worker.progress.connect(self._on_cache_cleanup_progress)
        worker.completed.connect(
            lambda success, message, cancelled: (
                self._on_cache_cleanup_completed(
                    success,
                    message,
                    cancelled,
                    False,
                )
            )
        )
        worker.finished.connect(
            lambda current=worker: self._on_cache_cleanup_thread_finished(
                current
            )
        )
        self.cache_thread = worker
        worker.start()

    def on_jpegs_cleaned(self):
        """Compatibility callback for older integrations."""
        self._on_cache_cleanup_completed(
            True,
            "JPEG file cleaning completed.",
            False,
            False,
        )

    def on_clean_cache(self, for_exit=False, delete_all=False):
        """Clean cached imagery without blocking the UI."""
        if self.running:
            QMessageBox.warning(
                self,
                "Cannot clean cache while running",
                "Stop scenery streaming before cleaning the cache.",
            )
            return

        self._closing = for_exit
        self.update_status_bar("Cleaning cache...")
        self._set_cache_buttons_enabled(False)
        target_size = int(
            self.file_cache_slider.value() if not delete_all else 0
        )
        worker = CacheCleanupWorker(self, "all", target_size)
        self.task_manager.create_task(
            "cache-cleanup",
            TaskType.CACHE,
            "Delete cache" if delete_all else "Clean cache",
            stage="Scanning cache",
            cancellable=not delete_all,
            cancel_callback=(worker.cancel if not delete_all else None),
            retry_callback=lambda: self.on_clean_cache(
                delete_all=delete_all
            ),
        )
        worker.progress.connect(self._on_cache_cleanup_progress)
        worker.completed.connect(
            lambda success, message, cancelled: (
                self._on_cache_cleanup_completed(
                    success,
                    message,
                    cancelled,
                    for_exit,
                )
            )
        )
        worker.finished.connect(
            lambda current=worker: self._on_cache_cleanup_thread_finished(
                current
            )
        )
        self.cache_thread = worker
        worker.start()

    def _on_cache_cleanup_progress(self, message):
        self.update_status_bar(message)
        self.task_manager.update_task(
            "cache-cleanup",
            stage=message,
        )

    def _on_cache_cleanup_completed(
        self,
        success,
        message,
        cancelled,
        for_exit,
    ):
        self._set_cache_buttons_enabled(True)
        if cancelled:
            self.task_manager.mark_cancelled("cache-cleanup")
        elif success:
            self.task_manager.complete_task(
                "cache-cleanup",
                stage=message or "Cache cleaned",
            )
            if not for_exit:
                QMessageBox.information(
                    self,
                    "Cache Cleaned",
                    message or "Cache cleaning completed.",
                )
        else:
            self.task_manager.fail_task("cache-cleanup", message)
            if not for_exit:
                self.display_error(f"Cache cleanup failed:\n{message}")
        self._cache_finalize_pending = for_exit
        if not for_exit:
            self._start_storage_scan()

    def _on_cache_cleanup_thread_finished(self, worker):
        if self.cache_thread is worker:
            self.cache_thread = None
        worker.deleteLater()
        if getattr(self, "_cache_finalize_pending", False):
            self._cache_finalize_pending = False
            QTimer.singleShot(0, self._finalize_shutdown)

    def _set_cache_buttons_enabled(self, enabled):
        """Enable or disable all cache-related buttons"""
        for btn_name in ('clean_cache_btn', 'clean_jpegs_btn', 'delete_cache_btn', 'run_button'):
            if hasattr(self, btn_name):
                getattr(self, btn_name).setEnabled(enabled)

    def on_cache_cleaned(self, for_exit=False):
        """Compatibility callback for older integrations."""
        self._on_cache_cleanup_completed(
            True,
            "Cache cleaning completed.",
            False,
            for_exit,
        )

    def on_install_scenery(self, region_id, skip_confirmation=False):
        """Start a nonblocking scenery installation preflight."""
        storage_issues = [
            issue for issue in validate_configuration(
                self._current_configuration_input(),
                require_installed_scenery=False,
            )
            if issue.field in ("scenery_path", "download_dir")
            and issue.severity == ValidationSeverity.ERROR
        ]
        if storage_issues:
            self._show_validation_issues(storage_issues)
            return
        region = self.dl.regions.get(region_id)
        if region is None:
            self.display_error(
                f"Scenery region {region_id} is no longer available."
            )
            return
        latest = region.get_latest_release()
        latest.parse()
        size_bytes = int(getattr(latest, "totalsize", 0) or 0)
        temporary_required, final_required = (
            package_storage_requirements(
                size_bytes,
                safety_margin_gb=float(
                    getattr(
                        self.cfg.scenery,
                        "storage_safety_margin_gb",
                        2,
                    )
                ),
            )
        )
        destination = self.cfg.paths.scenery_path
        download_path = self.cfg.paths.download_dir
        existing = self.install_preflight_workers.get(region_id)
        if existing is not None and existing.isRunning():
            return

        worker = ServiceWorker(
            lambda cancel_event: (
                self.storage_service.check_installation_capacity(
                    download_path,
                    destination,
                    temporary_required,
                    final_required,
                    cancel_event=cancel_event,
                )
            ),
            self,
        )
        self.install_preflight_workers[region_id] = worker
        self.task_manager.create_task(
            f"scenery-install:{region_id}",
            TaskType.SCENERY_INSTALL,
            "Install scenery",
            package=latest.name,
            stage="Checking storage",
            cancellable=True,
            cancel_callback=worker.cancel,
            retry_callback=lambda rid=region_id: self.on_install_scenery(
                rid,
                skip_confirmation=True,
            ),
        )
        worker.completed.connect(
            lambda result, rid=region_id, name=latest.name, ver=latest.ver: (
                self._continue_scenery_install(
                    rid,
                    name,
                    ver,
                    size_bytes,
                    destination,
                    temporary_required,
                    final_required,
                    skip_confirmation,
                    result,
                )
            )
        )
        worker.finished.connect(
            lambda rid=region_id, current=worker: (
                self._install_preflight_finished(rid, current)
            )
        )
        worker.start()

    def _continue_scenery_install(
        self,
        region_id,
        name,
        version,
        size_bytes,
        destination,
        temporary_required,
        final_required,
        skip_confirmation,
        result,
    ):
        task_id = f"scenery-install:{region_id}"
        task = self.task_manager.task(task_id)
        if task is None or task.state in (
            TaskState.CANCELLING,
            TaskState.CANCELLED,
        ):
            return
        if isinstance(result, Exception) or not result.success:
            message = (
                str(result)
                if isinstance(result, Exception)
                else result.error.message
            )
            if (
                not isinstance(result, Exception)
                and result.error.code.value == "cancelled"
            ):
                self.task_manager.mark_cancelled(task_id)
            else:
                self.task_manager.fail_task(task_id, message)
                self.display_error(message)
            return

        capacity = result.value
        if not capacity.sufficient:
            message = (
                f"{name} needs approximately "
                f"{format_bytes(final_required)} of final scenery space "
                f"and {format_bytes(temporary_required)} of temporary "
                "download space."
            )
            self.task_manager.fail_task(task_id, message)
            QMessageBox.critical(
                self,
                "Not Enough Disk Space",
                f"{name} needs approximately:\n\n"
                f"Temporary download space: "
                f"{format_bytes(temporary_required)} "
                f"(available "
                f"{format_bytes(capacity.download_free_bytes)})\n"
                f"Final scenery space: {format_bytes(final_required)} "
                f"(available "
                f"{format_bytes(capacity.destination_free_bytes)})",
            )
            return

        if not skip_confirmation:
            dialog = InstallationDialog(
                InstallationReview(
                    name=name,
                    version=str(version),
                    download_bytes=size_bytes,
                    temporary_bytes=temporary_required,
                    final_bytes=final_required,
                    destination=destination,
                ),
                self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.task_manager.mark_cancelled(task_id)
                return

        button = self.findChild(QPushButton, f"scenery-{region_id}")
        progress_current = self.findChild(QProgressBar, f"progress-current-{region_id}")
        progress_overall = self.findChild(QProgressBar, f"progress-overall-{region_id}")

        if button:
            button.setEnabled(False)
            button.setText("Downloading...")

        if progress_current:
            progress_current.setVisible(True)
        if progress_overall:
            progress_overall.setVisible(True)

        task = self.task_manager.update_task(
            task_id,
            stage="Downloading",
            cancellable=False,
        )
        if task is None or task.state.terminal:
            return
        task.bytes_total = size_bytes

        # Create worker thread
        worker = SceneryDownloadWorker(
            self.dl, region_id, self.cfg.paths.download_dir
        )
        worker.progress.connect(self.on_download_progress)
        worker.finished.connect(self.on_download_finished)
        worker.error.connect(self.on_download_error)

        self.download_workers[region_id] = worker
        worker.start()

    def _install_preflight_finished(self, region_id, worker):
        if self.install_preflight_workers.get(region_id) is worker:
            self.install_preflight_workers.pop(region_id, None)
        task = self.task_manager.task(
            f"scenery-install:{region_id}"
        )
        if task is not None and task.state == TaskState.CANCELLING:
            self.task_manager.mark_cancelled(task.id)

    def on_add_seasons(self, region_id, seasons_status: downloader.SeasonsApplyStatus):
        """Handle adding seasons"""
        # Block if AutoOrtho is running
        if getattr(self, 'running', False):
            QMessageBox.warning(
                self,
                "Operation Not Allowed While Running",
                "Cannot add Native Seasons while AutoOrtho is running. Please stop AutoOrtho first."
            )
            return

        # Button no longer exists directly; use scenery-options for state changes
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button is None:
            return

        # If something is already processing, enqueue this request
        if self.add_seasons_current is not None:
            # Avoid duplicates in queue
            if region_id not in self.add_seasons_queue:
                self.add_seasons_queue.append(region_id)
                self.task_manager.create_task(
                    f"seasons:{region_id}",
                    TaskType.SEASONS,
                    "Apply native seasons",
                    package=region_id,
                    stage="Queued",
                    retry_callback=lambda rid=region_id, status=seasons_status: (
                        self.on_add_seasons(rid, status)
                    ),
                )
                try:
                    button.setEnabled(False)
                    button.setText("Queued for seasons…")
                except Exception:
                    pass
            return

        # Nothing processing; start immediately
        self._start_add_seasons_job(region_id)

    def on_add_seasons_error(self, region_id, error_msg):
        """Handle add seasons error"""
        self.task_manager.fail_task(
            f"seasons:{region_id}",
            error_msg,
        )
        self.show_error.emit(f"Failed to add seasons to {region_id}:\n{error_msg}")
        # Ensure current is cleared so queue can progress
        try:
            if self.add_seasons_current == region_id:
                self.add_seasons_current = None
        except Exception:
            pass
        self.on_add_seasons_finished(region_id, False)

    def on_add_seasons_finished(self, region_id, success):
        """Handle add seasons completion"""
        button = self.findChild(QPushButton, f"scenery-options-{region_id}")
        if button:
            button.setEnabled(not self.running)
            button.setText("Seasons Options")

        if success:
            self.update_status_bar(f"Successfully added seasons to {region_id}")
        else:
            self.update_status_bar(f"Failed to add seasons to {region_id}")

        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setVisible(False)
        task_id = f"seasons:{region_id}"
        task = self.task_manager.task(task_id)
        if success:
            self.task_manager.complete_task(
                task_id,
                stage="Seasons applied",
            )
        elif task is not None and task.state != TaskState.FAILED:
            self.task_manager.fail_task(
                task_id,
                f"Failed to apply seasons to {region_id}.",
            )

        # Clean up worker
        if region_id in self.add_seasons_workers:
            try:
                self.add_seasons_workers[region_id].wait()
            except Exception:
                pass
            del self.add_seasons_workers[region_id]
        # Clear current and process next in queue
        if self.add_seasons_current == region_id:
            self.add_seasons_current = None
        self._update_run_button_for_seasons_state()
        # Start next queued seasons job if any
        self._dequeue_and_start_next_seasons_job()
        self.refresh_scenery_list()

    def _has_active_seasons_jobs(self):
        try:
            return (self.add_seasons_current is not None) or (len(self.add_seasons_workers) > 0)
        except Exception:
            return False

    def _update_run_button_for_seasons_state(self):
        """Disable Run while seasons/roughness jobs are active; re-enable otherwise when not running"""
        try:
            has_active_jobs = self._has_active_seasons_jobs() or self._has_active_roughness_jobs()
            if has_active_jobs:
                if hasattr(self, 'run_button'):
                    self.run_button.setEnabled(False)
                    self.run_button.setToolTip("Disabled: Scenery patches are being applied")
                # Also disable Scenery Options buttons
                try:
                    for rid in getattr(self, 'installed_package_names', []):
                        btn = self.findChild(QPushButton, f"scenery-options-{rid}")
                        if btn:
                            btn.setEnabled(False)
                except Exception:
                    pass
            else:
                if hasattr(self, 'run_button') and not self.running:
                    self.run_button.setToolTip("")
                    self._set_runtime_state(self.runtime_state)
                # Re-enable Scenery Options buttons when idle
                try:
                    for rid in getattr(self, 'installed_package_names', []):
                        btn = self.findChild(QPushButton, f"scenery-options-{rid}")
                        if btn:
                            btn.setEnabled(True)
                except Exception:
                    pass
        except Exception:
            pass

    def _has_active_roughness_jobs(self):
        """Check if there are any active roughness patching jobs."""
        try:
            if hasattr(self, 'add_roughness_workers') and self.add_roughness_workers:
                return True
            if hasattr(self, 'restore_roughness_workers') and self.restore_roughness_workers:
                return True
        except Exception:
            pass
        return False

    def _start_add_seasons_job(self, region_id):
        """Internal helper to begin processing a single add-seasons job for region_id."""
        try:
            task = self.task_manager.create_task(
                f"seasons:{region_id}",
                TaskType.SEASONS,
                "Apply native seasons",
                package=region_id,
                stage="Preparing",
                retry_callback=lambda rid=region_id: (
                    self._start_add_seasons_job(rid)
                ),
            )
            self.task_manager.update_task(task.id, stage="Converting files")
            button = self.findChild(QPushButton, f"scenery-options-{region_id}")
            if button:
                button.setEnabled(False)
                button.setText("Adding seasons...")

            dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
            if dsf_progress_bar:
                dsf_progress_bar.setVisible(True)
                dsf_progress_bar.setRange(0, 100)
                dsf_progress_bar.setValue(0)

            # Create worker thread
            worker = AddSeasonsWorker(region_id, self.cfg.paths.scenery_path)
            worker.progress.connect(self.on_add_seasons_progress)
            worker.finished.connect(self.on_add_seasons_finished)
            worker.error.connect(self.on_add_seasons_error)
            # Keep a strong reference so the thread isn't GC'd while running
            self.add_seasons_workers[region_id] = worker
            worker.setParent(self)
            self.add_seasons_current = region_id
            worker.start()
            # Disable Run while any seasons job is active
            self._update_run_button_for_seasons_state()
        except Exception:
            pass

    def _dequeue_and_start_next_seasons_job(self):
        try:
            if self.add_seasons_current is None and self.add_seasons_queue:
                next_region_id = self.add_seasons_queue.pop(0)
                # Start next job and update its button
                self._start_add_seasons_job(next_region_id)
            else:
                # If nothing pending, re-enable all scenery options buttons
                if not self._has_active_seasons_jobs():
                    for rid in self.installed_packages:
                        btn = self.findChild(QPushButton, f"scenery-options-{rid}")
                        if btn:
                            btn.setEnabled(True)
        except Exception:
            pass

    def on_add_seasons_progress(self, region_id, progress_data):
        """Handle add seasons progress"""
        self.task_manager.update_task(
            f"seasons:{region_id}",
            stage=progress_data.get("status", "Converting files"),
            progress=float(progress_data.get("pcnt_done", 0) or 0),
        )

        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setVisible(True)
            # Always use 0-100 range to match percent value
            try:
                dsf_progress_bar.setRange(0, 100)
                pcnt = int(progress_data.get('pcnt_done', 0))
                dsf_progress_bar.setValue(pcnt)
                files_done = progress_data.get('files_done')
                files_total = progress_data.get('files_total')
                if files_done is not None and files_total:
                    dsf_progress_bar.setFormat(f"{files_done}/{files_total}")
                else:
                    dsf_progress_bar.setFormat("%p%")
            except Exception:
                # Be resilient to unexpected payloads
                dsf_progress_bar.setRange(0, 100)
                dsf_progress_bar.setValue(0)
                dsf_progress_bar.setFormat("%p%")

        # While one is processing, show other packages as queued if they are in queue
        try:
            if self.add_seasons_queue:
                for rid in self.installed_packages:
                    if rid == self.add_seasons_current:
                        continue
                    btn = self.findChild(QPushButton, f"scenery-options-{rid}")
                    if not btn:
                        continue
                    if rid in self.add_seasons_queue:
                        btn.setEnabled(False)
                        btn.setText("Queued for seasons...")
                    else:
                        # If not queued and not current, ensure enabled state only if nothing running
                        if not self._has_active_seasons_jobs():
                            btn.setEnabled(True)
        except Exception:
            pass


    def on_uninstall_error(self, region_id, error_msg):
        """Handle uninstall error"""
        self.task_manager.fail_task(
            f"scenery-uninstall:{region_id}",
            error_msg,
        )
        self.show_error.emit(f"Failed to uninstall {region_id}:\n{error_msg}")
        self.on_uninstall_finished(region_id, False)

    def on_uninstall_finished(self, region_id, success):
        """Handle uninstall completion"""
        button = self.findChild(QPushButton, f"uninstall-{region_id}")
        if button:
            button.setEnabled(True)

        if success:
            self.task_manager.complete_task(
                f"scenery-uninstall:{region_id}",
                stage="Uninstalled",
            )
            self.update_status_bar(f"Successfully uninstalled {region_id}")
            self.refresh_scenery_list()
            if button:
                button.setText("Uninstalled")
        else:
            task = self.task_manager.task(
                f"scenery-uninstall:{region_id}"
            )
            if task is not None and task.state != TaskState.FAILED:
                self.task_manager.fail_task(
                    task.id,
                    f"Failed to uninstall {region_id}.",
                )
            self.update_status_bar(f"Failed to uninstall {region_id}")
            if button:
                button.setText("Uninstall")

        # Clean up worker
        if region_id in self.uninstall_workers:
            try:
                self.uninstall_workers[region_id].wait()
                self.uninstall_workers[region_id].deleteLater()
            except Exception:
                pass
            del self.uninstall_workers[region_id]
        if region_id in self.download_workers:
            del self.download_workers[region_id]
        if region_id in self.add_seasons_workers:
            try:
                self.add_seasons_workers[region_id].wait()
                self.add_seasons_workers[region_id].deleteLater()
            except Exception:
                pass
            del self.add_seasons_workers[region_id]

        if region_id in self.restore_default_dsfs_workers:
            try:
                self.restore_default_dsfs_workers[region_id].wait()
                self.restore_default_dsfs_workers[region_id].deleteLater()
            except Exception:
                pass
            del self.restore_default_dsfs_workers[region_id]

    def on_download_progress(self, region_id, progress_data):
        """Update download progress.

        Handles both aggregated progress (from ProgressAggregator, with
        'aggregate_MBps' / 'active_downloads' keys) and legacy per-file
        progress (with 'pcnt_done' / 'MBps' keys).
        """
        # Throttle UI updates to avoid freezing
        if not hasattr(self, '_last_ui_progress'):
            self._last_ui_progress = {}
        last = self._last_ui_progress.get(region_id, 0)
        now = time.time()
        if now - last < 0.1:
            return
        self._last_ui_progress[region_id] = now

        progress_current = self.findChild(QProgressBar, f"progress-current-{region_id}")
        progress_overall = self.findChild(QProgressBar, f"progress-overall-{region_id}")

        stage = progress_data.get('stage')
        task_id = f"scenery-install:{region_id}"
        if stage == 'verify':
            verify_progress = float(progress_data.get('verify_pcnt', 0) or 0)
            self.task_manager.update_task(
                task_id,
                stage=progress_data.get("status", "Installing"),
                progress=verify_progress,
                cancellable=False,
            )
            if progress_current:
                progress_current.setVisible(False)
            if progress_overall:
                progress_overall.setVisible(True)
                progress_overall.setValue(int(progress_data.get('verify_pcnt', 0)))
            button = self.findChild(QPushButton, f"scenery-{region_id}")
            if button:
                button.setText("Installing...")
            status = progress_data.get('status', 'Installing...')
            self.update_status_bar(f"{region_id}: {status}")
            return

        is_aggregate = 'aggregate_MBps' in progress_data
        overall_pcnt = progress_data.get('overall_pcnt', 0) or 0

        if is_aggregate:
            # Aggregated progress: hide the per-file bar, show only overall
            if progress_current:
                progress_current.setVisible(False)
            if progress_overall:
                progress_overall.setVisible(True)
                progress_overall.setValue(int(overall_pcnt))

            status = progress_data.get('status', 'Downloading...')
            task = self.task_manager.task(task_id)
            overall = float(overall_pcnt)
            total_bytes = task.bytes_total if task is not None else 0
            self.task_manager.update_task(
                task_id,
                stage=status,
                progress=overall,
                bytes_completed=int(total_bytes * overall / 100.0),
                rate=float(
                    progress_data.get("aggregate_MBps", 0) or 0
                ) * 1024 * 1024,
            )
            self.update_status_bar(f"{region_id}: {status}")
        else:
            # Legacy per-file progress (single-threaded fallback)
            pcnt_done = progress_data.get('pcnt_done', 0)
            files_done = progress_data.get('files_done')
            files_total = progress_data.get('files_total')

            if progress_current is not None:
                progress_current.setVisible(True)
                progress_current.setValue(int(pcnt_done))

            if progress_overall is not None:
                if overall_pcnt == 0 and files_done is not None and files_total:
                    try:
                        overall_pcnt = (float(files_done) / float(files_total)) * 100.0
                    except Exception:
                        overall_pcnt = 0
                progress_overall.setVisible(True)
                progress_overall.setValue(int(overall_pcnt))

            MBps = progress_data.get('MBps', 0)
            status = progress_data.get('status', 'Downloading...')
            if pcnt_done > 0:
                task = self.task_manager.task(task_id)
                total_bytes = task.bytes_total if task is not None else 0
                self.task_manager.update_task(
                    task_id,
                    stage=status,
                    progress=float(overall_pcnt or pcnt_done),
                    bytes_completed=int(
                        total_bytes * float(overall_pcnt or pcnt_done) / 100.0
                    ),
                    rate=float(MBps or 0) * 1024 * 1024,
                )
                self.update_status_bar(
                    f"{region_id}: {pcnt_done:.1f}% ({MBps:.1f} MB/s)"
                )
            else:
                self.update_status_bar(f"{region_id}: {status}")

    def on_download_finished(self, region_id, success):
        """Handle download completion"""
        button = self.findChild(QPushButton, f"scenery-{region_id}")
        progress_current = self.findChild(QProgressBar, f"progress-current-{region_id}")
        progress_overall = self.findChild(QProgressBar, f"progress-overall-{region_id}")

        if success:
            self.task_manager.complete_task(
                f"scenery-install:{region_id}",
                stage="Installed",
            )
            if button:
                button.setVisible(False)
            if progress_current:
                progress_current.setVisible(False)
            if progress_overall:
                progress_overall.setVisible(False)
            self.update_status_bar(f"Successfully installed {region_id}")
            # Refresh the scenery list
            self.refresh_scenery_list()
        else:
            task = self.task_manager.task(
                f"scenery-install:{region_id}"
            )
            if task is not None and task.state != TaskState.FAILED:
                self.task_manager.fail_task(
                    task.id,
                    f"Failed to install {region_id}.",
                )
            if button:
                button.setText("Retry?")
                button.setEnabled(True)
            if progress_current:
                progress_current.setVisible(False)
            if progress_overall:
                progress_overall.setVisible(False)
            self.update_status_bar(f"Failed to install {region_id}")

        # Clean up worker
        if region_id in self.download_workers:
            del self.download_workers[region_id]

    def on_download_error(self, region_id, error_msg):
        """Handle download error"""
        self.task_manager.fail_task(
            f"scenery-install:{region_id}",
            error_msg,
        )
        self.show_error.emit(f"Failed to install {region_id}:\n{error_msg}")
        self.on_download_finished(region_id, False)

    def save_config(self, persist=True, refresh_scenery=True):
        """Copy UI values to config, optionally persisting them to disk."""
        self.ready.clear()

        # Save paths
        self.cfg.paths.scenery_path = self.scenery_path_edit.text()
        self.cfg.paths.xplane_path = self.xplane_path_edit.text()
        self.cfg.paths.cache_dir = self.cache_dir_edit.text()
        self.cfg.paths.long_term_cache_dir = self.lt_cache_dir_edit.text()
        self.cfg.paths.download_dir = self.download_dir_edit.text()

        # Save options
        self.cfg.general.showconfig = self.showconfig_check.isChecked()
        self.cfg.autoortho.maptype_override = self.maptype_combo.currentText()
        if self.cfg.autoortho.simheaven_compat != self.simheaven_compat_check.isChecked():
            self.cfg.autoortho.simheaven_compat = self.simheaven_compat_check.isChecked()
            self.simheaven_config_changed_session = True

        self.cfg.cache.auto_clean_cache = self.auto_clean_cache_check.isChecked()
        self.cfg.autoortho.using_custom_tiles = self.using_custom_tiles_check.isChecked()

        # Windows specific
        if self.system == 'windows' and hasattr(self, 'winfsp_check'):
            self.cfg.windows.prefer_winfsp = self.winfsp_check.isChecked()

        # Save Settings tab values
        if hasattr(self, 'file_cache_slider'):
            # Cache settings
            self.cfg.cache.file_cache_size = str(
                self.file_cache_slider.value()
            )
            self.cfg.cache.cache_mem_limit = str(
                self.mem_cache_slider.value()
            )

            # AutoOrtho settings
            self.cfg.autoortho.min_zoom = str(self.min_zoom_slider.value())
            self.cfg.autoortho.max_zoom_near_airports = str(self.max_zoom_near_airports_slider.value())
            self.cfg.autoortho.max_zoom = str(self.max_zoom_slider.value())
            
            # Dynamic zoom settings
            zoom_mode = "dynamic" if self.max_zoom_mode_combo.currentText() == "Dynamic" else "fixed"
            self.cfg.autoortho.max_zoom_mode = zoom_mode
            if hasattr(self, '_dynamic_zoom_manager'):
                self.cfg.autoortho.dynamic_zoom_steps = self._dynamic_zoom_manager.save_to_config()
            
            # Reset buffer pool so it will be recreated with correct size for new zoom settings
            try:
                from getortho import reset_dds_buffer_pool
                reset_dds_buffer_pool()
            except ImportError:
                pass  # Module not loaded yet, pool will be created correctly on first use
            
            self.cfg.autoortho.maxwait = str(
                self.maxwait_slider.value() / 10.0
            )
            self.cfg.autoortho.suspend_maxwait = self.suspend_maxwait_check.isChecked()
            
            # Performance tuning settings
            self.cfg.autoortho.use_time_budget = self.use_time_budget_check.isChecked()
            self.cfg.autoortho.tile_time_budget = str(self.tile_budget_slider.value())
            self.cfg.autoortho.fallback_level = self._fallback_index_to_str(
                self.fallback_level_combo.currentIndex()
            )
            self.cfg.autoortho.fallback_extends_budget = self.fallback_extends_budget_check.isChecked()
            self.cfg.autoortho.fallback_timeout = str(
                self.fallback_timeout_slider.value()
            )
            
            # Prefetch settings
            self.cfg.autoortho.prefetch_enabled = self.prefetch_enabled_check.isChecked()
            # Slider value 61 = Unlimited, save as 0 to config
            lookahead_val = self.prefetch_lookahead_slider.value()
            self.cfg.autoortho.prefetch_lookahead = str(
                0 if lookahead_val == 61 else lookahead_val
            )
            self.cfg.autoortho.prefetch_interval = str(
                self.prefetch_interval_slider.value() / 10.0
            )
            self.cfg.autoortho.prefetch_max_chunks = str(
                self.prefetch_max_chunks_slider.value()
            )
            self.cfg.autoortho.prefetch_radius_nm = str(
                self.prefetch_radius_slider.value()
            )
            
            # Predictive DDS settings
            self.cfg.autoortho.predictive_dds_enabled = (
                self.prefetch_enabled_check.isChecked()
                and self.predictive_dds_enabled_check.isChecked()
            )
            self.cfg.autoortho.predictive_dds_build_interval_ms = str(
                self.predictive_interval_slider.value()
            )
            self.cfg.autoortho.background_builder_workers = str(
                self.background_workers_slider.value()
            )
            self.cfg.autoortho.live_builder_concurrency = str(
                self.live_concurrency_slider.value()
            )
            self.cfg.autoortho.predictive_dds_use_fallbacks = self.predictive_use_fallbacks_check.isChecked()
            
            # Native pipeline settings
            self.cfg.autoortho.pipeline_mode = self.pipeline_mode_combo.currentText()
            self.cfg.autoortho.buffer_pool_size = str(self.buffer_pool_slider.value())

            self.cfg.autoortho.fetch_threads = str(
                self.fetch_threads_spinbox.value()
            )
            self.cfg.autoortho.provider_max_in_flight = str(
                self.provider_inflight_spinbox.value()
            )
            self.cfg.autoortho.provider_max_connections = str(
                self.provider_connections_spinbox.value()
            )
            self.cfg.autoortho.download_dispatch_workers = str(
                self.download_dispatch_workers_spinbox.value()
            )
            self.cfg.autoortho.provider_adaptive_concurrency = (
                self.provider_adaptive_check.isChecked()
            )
            self.cfg.autoortho.live_tile_admission = str(
                self.live_tile_admission_spinbox.value()
            )
            self.cfg.autoortho.tile_image_cache_mb = str(
                self.tile_image_cache_mb_spinbox.value()
            )
            self.cfg.autoortho.missing_color = [self.missing_color.red(),
                                                self.missing_color.green(),
                                                self.missing_color.blue()]

            # DDS settings
            if not self.system == "darwin":
                self.cfg.pydds.compressor = self.compressor_combo.currentText()
            self.cfg.pydds.format = self.format_combo.currentText()

            # General settings
            self.cfg.general.gui = self.gui_check.isChecked()
            self.cfg.general.hide = self.hide_check.isChecked()
            self.cfg.general.console_log_level = self.console_log_level_combo.currentText()
            self.cfg.general.file_log_level = self.file_log_level_combo.currentText()

            # Performance diagnostics settings
            self.cfg.diagnostics.performance_profiling = (
                self.performance_profiling_check.isChecked()
            )
            self.cfg.diagnostics.sample_interval_seconds = str(
                self.performance_sample_interval_spin.value()
            )
            self.cfg.diagnostics.checkpoint_interval_seconds = str(
                self.performance_checkpoint_interval_spin.value()
            )
            self.cfg.diagnostics.python_allocation_tracing = (
                self.python_allocation_tracing_check.isChecked()
            )

            # Scenery settings
            self.cfg.scenery.noclean = self.noclean_check.isChecked()
            self.dl.noclean = self.cfg.scenery.noclean
            self.cfg.scenery.max_download_workers = self.max_download_workers_spin.value()
            self.cfg.scenery.storage_safety_margin_gb = str(
                self.storage_safety_margin_spin.value()
            )

            # FUSE settings
            self.cfg.fuse.threading = self.threading_check.isChecked()

            # Flight data settings
            self.cfg.flightdata.webui_port = str(
                self.webui_port_edit.text()
            )
            self.cfg.flightdata.xplane_udp_port = str(
                self.xplane_udp_port_edit.text()
            )

            # Seasons settings
            self.cfg.seasons.enabled = self.seasons_enabled_check.isChecked()
            self.cfg.seasons.compress_dsf = self.compress_dsf_check.isChecked()
            self.cfg.seasons.seasons_convert_workers = str(self.seasons_convert_workers_slider.value())
            self.cfg.seasons.spr_saturation = str(self.spr_sat_slider.value())
            self.cfg.seasons.sum_saturation = str(self.sum_sat_slider.value())
            self.cfg.seasons.fal_saturation = str(self.fal_sat_slider.value())
            self.cfg.seasons.win_saturation = str(self.win_sat_slider.value())

            # Night exclusion settings (sun-position based)
            if hasattr(self, 'time_exclusion_enabled_check'):
                self.cfg.time_exclusion.enabled = self.time_exclusion_enabled_check.isChecked()
            if hasattr(self, 'time_exclusion_default_check'):
                self.cfg.time_exclusion.default_to_exclusion = self.time_exclusion_default_check.isChecked()
            if hasattr(self, 'sun_night_threshold_spin'):
                self.cfg.time_exclusion.sun_night_threshold = self.sun_night_threshold_spin.value()
            if hasattr(self, 'sun_day_threshold_spin'):
                self.cfg.time_exclusion.sun_day_threshold = self.sun_day_threshold_spin.value()

        # SimBrief settings
        if hasattr(self.cfg, 'simbrief'):
            if hasattr(self, 'simbrief_userid_edit'):
                self.cfg.simbrief.userid = self.simbrief_userid_edit.text().strip()
            if hasattr(self, 'simbrief_use_flight_data_check'):
                self.cfg.simbrief.use_flight_data = self.simbrief_use_flight_data_check.isChecked()

        if persist:
            if not self._persist_configuration(notify=True):
                self.ready.set()
                return False
        else:
            self.cfg.set_config()
            self.cfg.refresh_derived_paths(create_missing=False)
        self.ready.set()
        if refresh_scenery:
            self.refresh_scenery()
        return True

    def preflight_mount_check_and_prompt(self):
        """Confirm cleanup of lingering mounts without blocking the UI."""
        lingering = []
        for scenery in self.cfg.scenery_mounts:
            mount = scenery.get("mount")
            if mount and safe_ismount(mount):
                lingering.append(mount)
        if not lingering:
            return []

        msg = (
            "Previous AutoOrtho mounts are still active:\n\n"
            + "\n".join(lingering)
            + "\n\nStop these mounts before starting?"
        )
        reply = QMessageBox.question(
            self,
            "Existing Mounts Detected",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return None
        return lingering

    def cleanup_lingering_mounts(self, lingering):
        """Unmount stale mountpoints from the mount-control worker thread."""
        import subprocess as _sp

        for mount in lingering:
            if safe_ismount(mount):
                if system_type == "darwin":
                    _sp.run(
                        ["diskutil", "unmount", "force", mount],
                        check=False,
                        stdout=_sp.DEVNULL,
                        stderr=_sp.DEVNULL,
                    )
                elif system_type == "linux":
                    command = (
                        ["fusermount", "-u", "-z", mount]
                        if shutil.which("fusermount")
                        else ["umount", "-l", mount]
                    )
                    _sp.run(
                        command,
                        check=False,
                        stdout=_sp.DEVNULL,
                        stderr=_sp.DEVNULL,
                    )
                elif system_type == "windows":
                    try:
                        from autoortho import winsetup
                    except ImportError:
                        import winsetup
                    winsetup.force_unmount(mount)

            if not safe_ismount(mount):
                cleanup_mountpoint(mount)

        deadline = time.time() + 10
        while time.time() < deadline:
            if not any(safe_ismount(path) for path in lingering):
                log.info("All lingering mounts cleaned up successfully")
                return
            time.sleep(0.3)

        remaining = [
            path for path in lingering if safe_ismount(path)
        ]
        raise RuntimeError(
            "These mounts could not be stopped automatically:\n"
            + "\n".join(remaining)
        )

    def on_using_custom_tiles_check(self, state):
        """Handle using custom tiles check"""
        snapshot = (
            self._snapshot_settings()
            if self._settings_tracking_ready
            else None
        )
        if not state: 
            if self.cfg.autoortho.using_custom_tiles and int(self.cfg.autoortho.max_zoom) > 17:
                log.info("Max zoom being capped to 17 after custom tiles disabled")
                self.cfg.autoortho.max_zoom = 17
                if snapshot is not None:
                    snapshot["max_zoom_slider"] = min(
                        17,
                        int(snapshot.get("max_zoom_slider", 17)),
                    )
            self.cfg.autoortho.using_custom_tiles = False
        else:
            self.cfg.autoortho.using_custom_tiles = True

        if self.phase3_active:
            self.max_zoom_slider.setMaximum(19 if state else 17)
            if not state and self.max_zoom_slider.value() > 17:
                self.max_zoom_slider.setValue(17)
            self._update_zoom_mode_visibility()
            self._on_settings_control_changed()
        else:
            self.refresh_settings_tab()
            if snapshot is not None:
                self._restore_settings_snapshot(snapshot)
                self.settings_session.observe(self._snapshot_settings())
        self._update_buffer_pool_label()

    def apply_simheaven_compat(self, use_simheaven_overlay=False):
        """
        Modify scenery_packs.ini to enable/disable AutoOrtho overlays based on SimHeaven compatibility
        
        Args:
            use_simheaven_overlay (bool): If True, disable AutoOrtho overlays (for SimHeaven compatibility)
                                        If False, enable AutoOrtho overlays (normal mode)
        """
        if use_simheaven_overlay:
            log.info("Applying SimHeaven compatibility overlay - disabling AutoOrtho overlays.")
        else:
            log.info("Applying included overlay - enabling AutoOrtho overlays.")
        
        # Get the scenery_packs.ini file path
        xplane_path = self.cfg.paths.xplane_path
        if not xplane_path:
            log.warning("X-Plane path not configured. Cannot modify scenery_packs.ini")
            return
        
        scenery_packs_path = os.path.join(xplane_path, "Custom Scenery", "scenery_packs.ini")
        
        if not os.path.exists(scenery_packs_path):
            log.warning(f"scenery_packs.ini not found at {scenery_packs_path}")
            return
        
        try:
            # Read the current content
            with open(scenery_packs_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            overlay_pattern = "Custom Scenery/yAutoOrtho_Overlays/"
            simheaven_overlay_pattern_xp11 = "Custom Scenery/simHeaven_X-{region_id}"
            simheaven_overlay_pattern_xp12 = "Custom Scenery/simHeaven_X-World_{region_id}"
            
            # First, check if SimHeaven overlay pattern exists
            simheaven_found_required_libs = {x: False for x in self.installed_packages}
            missing_simheaven_libs = []
            for line in lines:
                line_stripped = line.strip()
                for region_id in self.installed_packages:
                    simheaven_region = map_kubilus_region_to_simheaven_region(region_id)
                    if simheaven_overlay_pattern_xp11.format(region_id=simheaven_region) in line_stripped or simheaven_overlay_pattern_xp12.format(region_id=simheaven_region) in line_stripped:
                        simheaven_found_required_libs[region_id] = True
                        log.info(f"Found SimHeaven overlay entry: {line_stripped}")
                        continue

            for region_id, found in simheaven_found_required_libs.items():
                if not found:
                    simheaven_region = map_kubilus_region_to_simheaven_region(region_id)
                    missing_simheaven_libs.append(simheaven_region)
                    log.error(f"SimHeaven overlay entry not found for {region_id}")

            missing_simheaven_libs = set(missing_simheaven_libs) # Remove duplicates
            if missing_simheaven_libs:
                log.info("Required SimHeaven packages missing in scenery_packs.ini - skipping AutoOrtho overlay modifications")
                QMessageBox.information(
                    self,
                    "SimHeaven Compatibility",
                    "Missing SimHeaven scenery in scenery_packs.ini - skipping AutoOrtho overlay modifications, make sure to install required SimHeaven scenery and run X-Plane once."
                    f"Missing SimHeaven Packages: {', '.join(missing_simheaven_libs)}"
                )
                return
            
            modified = False
            
            # Process each line to modify AutoOrtho overlays
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                
                if use_simheaven_overlay:
                    # Disable AutoOrtho overlays (for SimHeaven compatibility)
                    if line_stripped.startswith("SCENERY_PACK ") and overlay_pattern in line_stripped:
                        lines[i] = line.replace("SCENERY_PACK ", "SCENERY_PACK_DISABLED ", 1)
                        modified = True
                        log.info(f"Disabled AutoOrtho overlay: {line_stripped}")
                else:
                    # Enable AutoOrtho overlays (normal mode)
                    if line_stripped.startswith("SCENERY_PACK_DISABLED ") and overlay_pattern in line_stripped:
                        lines[i] = line.replace("SCENERY_PACK_DISABLED ", "SCENERY_PACK ", 1)
                        modified = True
                        log.info(f"Enabled AutoOrtho overlay: {line_stripped}")
            
            # Write back the modified content if changes were made
            if modified:
                with open(scenery_packs_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                log.info(f"Successfully updated scenery_packs.ini at {scenery_packs_path}")
            else:
                log.info("No AutoOrtho overlay found in scenery_packs.ini - skipping AutoOrtho overlay modifications")
                if not use_simheaven_overlay:
                    QMessageBox.information(
                        self,
                        "SimHeaven Compatibility",
                        "No AutoOrtho overlay entry found in scenery_packs.ini, therefore it was not activated.\n"
                        "If not using any external overlays, make sure to install AutoOrthoOverlays scenery and run X-Plane once."
                    )
                
        except Exception as e:
            log.error(f"Failed to modify scenery_packs.ini: {e}")
            raise

    def refresh_scenery(self):
        """Refresh scenery data"""
        self.dl.regions = {}
        self.dl.extract_dir = self.cfg.paths.scenery_path
        self.dl.download_dir = self.cfg.paths.download_dir
        if self.simheaven_config_changed_session:
            self.apply_simheaven_compat(self.cfg.autoortho.simheaven_compat)
            self.simheaven_config_changed_session = False

        
        self.refresh_scenery_list()

    def _parse_version(self, text):
        """Extract and parse a semantic version from arbitrary text.
        Returns packaging.version.Version or None if not found.
        """
        try:
            if not text:
                return None
            match = re.search(r"\d+(?:\.\d+){1,3}(?:[-._]rc[-._]?\d+)?", str(text), re.IGNORECASE)
            if not match:
                return None
            ver_str = match.group(0)
            # Normalize rc format for packaging.version
            ver_str = re.sub(r"[-._]rc[-._]?(\d+)", r"rc\1", ver_str, flags=re.IGNORECASE)
            return version.parse(ver_str)
        except Exception:
            return None

    def start_update_check(self, manual=False):
        """Start background update check against GitHub releases"""
        self._update_check_manual = bool(manual)
        try:
            self._update_worker = UpdateCheckWorker()
            self.task_manager.create_task(
                "update-check",
                TaskType.UPDATE,
                "Check for AutoOrtho updates",
                stage="Contacting GitHub",
                cancellable=True,
                cancel_callback=self._update_worker.cancel,
                retry_callback=lambda: self.start_update_check(manual=True),
            )
            self._update_worker.result.connect(self.on_update_check_result)
            self._update_worker.error.connect(self.on_update_check_error)
            self._update_worker.finished.connect(
                self.on_update_check_finished
            )
            self._update_worker.start()
        except Exception as exc:
            log.exception("Failed to start update check")
            if self._update_check_manual:
                self.task_manager.fail_task("update-check", str(exc))
            else:
                self.task_manager.complete_task(
                    "update-check",
                    stage="Update check unavailable",
                )
                self.task_manager.dismiss_task("update-check")

    def on_update_check_result(self, data):
        """Handle result from update check worker"""
        self.task_manager.complete_task(
            "update-check",
            stage=("Update available" if data else "Up to date"),
        )
        try:
            if not data:
                if self._update_check_manual:
                    self.update_status_bar("No update information available.")
                return
            latest_tag, html_url = data
            latest_ver = self._parse_version(latest_tag)
            current_ver = self._parse_version(__version__)
            if latest_ver is None or current_ver is None:
                return
            if latest_ver > current_ver:
                normalized = str(latest_ver)
                dismissed = str(
                    getattr(
                        self.cfg.general,
                        "dismissed_update_version",
                        "",
                    )
                )
                self._latest_update_version = normalized
                self._latest_update_url = (
                    html_url
                    or "https://github.com/ProgrammingDinosaur/autoortho4xplane/releases"
                )
                if dismissed != normalized:
                    self.shell.set_update_available(normalized)
            elif self._update_check_manual:
                self.update_status_bar("AutoOrtho is up to date.")
        except Exception:
            log.exception("Failed to process update check result")

    def on_update_check_error(self, error):
        if self._update_check_manual:
            self.task_manager.fail_task("update-check", error)
        else:
            self.task_manager.complete_task(
                "update-check",
                stage="Update check unavailable",
            )
            self.task_manager.dismiss_task("update-check")

    def _open_available_update(self):
        if self._latest_update_url:
            webbrowser.open(self._latest_update_url)

    def _remind_update_later(self):
        self.shell.clear_update_available()

    def _dismiss_available_update(self):
        if self._latest_update_version:
            self.cfg.general.dismissed_update_version = (
                self._latest_update_version
            )
            self._persist_configuration()
        self.shell.clear_update_available()

    def on_update_check_finished(self):
        task = self.task_manager.task("update-check")
        if task is not None and task.state == TaskState.CANCELLING:
            self.task_manager.mark_cancelled("update-check")


    def update_status_bar(self, message):
        """Update status bar message"""
        self.status_bar.showMessage(message)
        self.status_bar.setAccessibleDescription(message)
        log.info(message)

    def append_log(self, message):
        """Append message to log display"""
        if self.ui_log_handler is not None:
            self.ui_log_handler._append_text(message, logging.INFO)
        else:
            self.log_text.append(message)

    def display_error(self, message):
        """Display error message dialog"""
        announce_accessible(self.status_bar, message)
        QMessageBox.critical(self, "Error", message)

    def verify(self):
        """Validate current form values without saving or exiting."""
        self.warnings = []
        self.errors = []
        issues = validate_configuration(
            self._current_configuration_input(),
            scenery_mounts=self.cfg.scenery_mounts,
            require_installed_scenery=True,
        )
        self.warnings = [
            issue.message for issue in issues
            if issue.severity == ValidationSeverity.WARNING
        ]
        self.errors = [
            issue.message for issue in issues
            if issue.severity == ValidationSeverity.ERROR
        ]

        for warning in self.warnings:
            log.warning(warning)
        for error in self.errors:
            log.error(error)

        self._show_validation_issues(issues)
        if self.errors:
            return False
        if self.warnings:
            reply = QMessageBox.question(
                self,
                "Configuration Warnings",
                "\n".join(f"• {warning}" for warning in self.warnings)
                + "\n\nStart streaming anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes
        return True

    def clean_cache(
        self,
        cache_dir,
        size_gb,
        *,
        cancel_event=None,
        progress_callback=None,
    ):
        """Clean cache with 2-phase deletion order:
        1. JPEGs (always delete all)
        2. Dynamic DDS (LRU eviction if still over budget)
        """
        emit_progress = progress_callback or self.status_update.emit
        emit_progress(
            f"Cleaning up cache_dir {cache_dir}. Please wait..."
        )

        try:
            target_bytes = size_gb * (1024 ** 3)

            # --- Phase 1: JPEGs ---
            jpeg_count = 0
            for entry in os.scandir(cache_dir):
                if cancel_event is not None and cancel_event.is_set():
                    return True, "Cache cleanup cancelled safely.", True
                if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg')):
                    try:
                        os.remove(entry.path)
                        jpeg_count += 1
                    except OSError:
                        pass
            emit_progress(f"Phase 1: Deleted {jpeg_count} JPEG files.")

            # --- Phase 2: DDS cache (LRU, only if still over target) ---
            dds_root = os.path.join(cache_dir, "dds_cache")
            if cancel_event is not None and cancel_event.is_set():
                return True, "Cache cleanup cancelled safely.", True
            if size_gb == 0:
                if os.path.isdir(dds_root):
                    shutil.rmtree(dds_root, ignore_errors=True)
                    emit_progress("Phase 2: Deleted all DDS cache files.")
            else:
                try:
                    from autoortho.getortho import dynamic_dds_cache
                except ImportError:
                    dynamic_dds_cache = None
                if dynamic_dds_cache is not None:
                    usage = dynamic_dds_cache.get_disk_usage()
                    remaining_budget = target_bytes
                    if usage > remaining_budget:
                        excess = usage - int(remaining_budget * 0.9)
                        freed = dynamic_dds_cache.evict_lru(excess)
                        emit_progress(
                            f"Phase 2: Evicted {freed // (1024*1024)} MB from DDS cache.")
                    else:
                        emit_progress("Phase 2: DDS cache within budget.")
                else:
                    emit_progress("Phase 2: DDS cache not initialized, skipping.")

            emit_progress("Cache cleanup done.")
            return True, "Cache cleaning completed.", False
        except Exception as e:
            emit_progress(f"Cache cleanup error: {str(e)}")
            return False, str(e), False

    def clean_jpegs_only(
        self,
        cache_dir,
        *,
        cancel_event=None,
        progress_callback=None,
    ):
        """Clean only JPEG files from cache directory, leaving DDS cache untouched."""
        emit_progress = progress_callback or self.status_update.emit
        emit_progress(
            f"Cleaning JPEG files from {cache_dir}. Please wait..."
        )

        try:
            jpeg_count = 0
            for entry in os.scandir(cache_dir):
                if cancel_event is not None and cancel_event.is_set():
                    return True, "JPEG cleanup cancelled safely.", True
                if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg')):
                    os.remove(entry.path)
                    jpeg_count += 1
            
            if jpeg_count > 0:
                emit_progress(f"Deleted {jpeg_count} JPEG files.")
            else:
                emit_progress("No JPEG files found to delete.")
            
            emit_progress("JPEG cleanup done.")
            return True, "JPEG file cleaning completed.", False
        except Exception as e:
            emit_progress(f"JPEG cleanup error: {str(e)}")
            return False, str(e), False

    def _check_ortho_dir(self, path):
        """Check if orthophoto directory is valid"""
        ret = True
        if not sorted(pathlib.Path(path).glob("Earth nav data/*/*.dsf")):
            self.warnings.append(
                f"Orthophoto dir {path} seems wrong. "
                "This may cause issues."
            )
            ret = False
        return ret

    def _check_xplane_dir(self, path):
        """Check if X-Plane directory is valid"""
        if not os.path.isdir(path):
            self.errors.append(
                f"XPlane install directory '{path}' is not a directory."
            )
            return False

        if "Custom Scenery" not in os.listdir(path):
            self.errors.append(
                f"XPlane install directory '{path}' seems wrong."
            )
            return False

        return True

    def closeEvent(self, event):
        """Handle window close event without freezing UI during cache clean"""
        # If we're in the second pass (ready to close), just accept and exit
        if self._ready_to_close:
            event.accept()
            return

        if self.runtime_state == RuntimeState.RUNNING:
            reply = QMessageBox.question(
                self,
                "Stop Streaming and Quit?",
                "AutoOrtho is currently streaming scenery.\n\n"
                "Stop streaming and quit the application?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_stop = True
            self._request_stop_streaming()
            event.ignore()
            return

        if self.runtime_state in (
            RuntimeState.STARTING,
            RuntimeState.STOPPING,
        ):
            QMessageBox.information(
                self,
                "Operation In Progress",
                "Wait for the streaming operation to finish before quitting.",
            )
            event.ignore()
            return

        if self._has_active_ui_jobs():
            QMessageBox.warning(
                self,
                "Background Operation In Progress",
                "Wait for active scenery or cache operations to finish before "
                "quitting. This prevents partial installations and cache damage.",
            )
            event.ignore()
            return

        if not self._resolve_pending_settings(for_start=False):
            event.ignore()
            return

        self._closing = True
        self._persist_shell_state()

        self.running = False

        for worker in self.install_preflight_workers.values():
            if worker.isRunning():
                worker.cancel()
                worker.wait(2000)
        self.install_preflight_workers.clear()

        for attribute in (
            "catalog_worker",
            "readiness_worker",
            "setup_inference_worker",
        ):
            worker = getattr(self, attribute, None)
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(2000)
            setattr(self, attribute, None)
        if hasattr(self, "diagnostics_page"):
            self.diagnostics_page.shutdown()

        if (
            self.storage_scan_worker is not None
            and self.storage_scan_worker.isRunning()
        ):
            self.storage_scan_worker.requestInterruption()
            self.storage_scan_worker.wait()
            self.storage_scan_worker = None

        if hasattr(self, "unmount_sceneries"):
            try:
                unmounted = self.unmount_sceneries()
            except Exception as exc:
                self._closing = False
                self.display_error(
                    f"AutoOrtho could not stop all scenery mounts:\n{exc}"
                )
                event.ignore()
                return
            if unmounted is False:
                self._closing = False
                self.display_error(
                    "AutoOrtho could not stop all scenery mounts. "
                    "The application will remain open to avoid leaving "
                    "active mounts behind."
                )
                event.ignore()
                return

        # Clean up UI logging handler
        try:
            if hasattr(self, 'ui_log_handler') and self.ui_log_handler:
                logging.getLogger().removeHandler(self.ui_log_handler)
                self.ui_log_handler = None
        except Exception:
            pass

        # Stop all background workers immediately
        try:
            for worker in self.download_workers.values():
                if worker.isRunning():
                    worker.wait()
        except Exception:
            pass
        try:
            for worker in self.uninstall_workers.values():
                if worker.isRunning():
                    worker.wait()
        except Exception:
            pass
        self.uninstall_workers.clear()

        # Stop update check worker if running
        try:
            if hasattr(self, '_update_worker') and self._update_worker:
                self._update_worker.cancel()
                self._update_worker.wait()
                self._update_worker = None
        except Exception:
            pass

        # Stop SimBrief fetch worker if running
        try:
            if hasattr(self, 'simbrief_fetch_worker') and self.simbrief_fetch_worker:
                self.simbrief_fetch_worker.cancel()
                self.simbrief_fetch_worker.wait()
                self.simbrief_fetch_worker = None
        except Exception:
            pass

        # Stop cache thread if running
        try:
            if hasattr(self, 'cache_thread') and self.cache_thread:
                self.cache_thread.quit()
                self.cache_thread.wait(2000)
                self.cache_thread = None
        except Exception:
            pass

        # Stop any AddSeasons workers
        try:
            if hasattr(self, 'add_seasons_worker') and self.add_seasons_worker:
                self.add_seasons_worker.wait()
                self.add_seasons_worker = None
        except Exception:
            pass

        # Stop any RestoreDefaultDsfs workers
        try:
            if hasattr(self, 'restore_dsfs_worker') and self.restore_dsfs_worker:
                self.restore_dsfs_worker.wait()
                self.restore_dsfs_worker = None
        except Exception:
            pass

        # If auto-clean is enabled and we haven't started shutdown cleaning yet,
        # kick it off asynchronously and ignore this close event.
        if self.cfg.cache.auto_clean_cache and not self._shutdown_in_progress:
            self._shutdown_in_progress = True
            self.update_status_bar("Auto cleaning cache before exit...")
            # Fire off cleaning without blocking
            self.on_clean_cache(for_exit=True)
            # Prevent immediate close; we'll close when cleaning finishes
            event.ignore()
            # Optionally hide or disable the window to indicate shutdown
            try:
                self.setEnabled(False)
            except Exception:
                pass
            return

        # No auto-clean requested or already handled; proceed to close
        event.accept()

    def _finalize_shutdown(self):
        """Finalize app shutdown after async cache clean completes"""
        self._ready_to_close = True
        try:
            # Trigger close again; closeEvent will accept immediately
            self.close()
        except Exception:
            # As a fallback, force quit the application
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.quit()


# This class needs to be imported from the parent module
# We'll create a stub here and modify autoortho.py to use the Qt version
class AOMountUI(ConfigUI):
    """Combined UI and mount functionality"""
    def __init__(self, cfg):
        super().__init__(cfg)
        self.mount_threads = []
        self.mounts_running = False

    def mount_sceneries(self, blocking=True):
        """Mount sceneries (stub - implemented in parent)"""
        pass

    def unmount_sceneries(self, force=False):
        """Unmount sceneries (stub - implemented in parent)"""
        pass

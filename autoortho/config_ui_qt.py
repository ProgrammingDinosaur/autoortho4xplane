#!/usr/bin/env python

from ast import If
import os
import sys
import pathlib
import platform
import threading
import time
import traceback
import logging
import re
import webbrowser
import requests
from packaging import version
import utils.resources_rc
from utils.constants import MAPTYPES, system_type
from utils.mappers import map_kubilus_region_to_simheaven_region
from utils.dsf_utils import DsfUtils, dsf_utils
from utils.mount_utils import cleanup_mountpoint

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel, QLineEdit, QCheckBox, QComboBox,
    QSlider, QTextEdit, QFileDialog, QMessageBox, QScrollArea,
    QSplashScreen, QGroupBox, QProgressBar, QStatusBar, QFrame, QSpinBox,
    QColorDialog, QRadioButton, QMenu, QStyle
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSize, QPoint
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QWheelEvent, QCursor
)

import downloader
from version import __version__

log = logging.getLogger(__name__)

CUR_PATH = os.path.dirname(os.path.realpath(__file__))


class SceneryDownloadWorker(QThread):
    """Worker thread for downloading scenery"""
    progress = Signal(str, dict)  # region_id, progress_data
    finished = Signal(str, bool)  # region_id, success
    error = Signal(str, str)  # region_id, error_message

    def __init__(self, dl_manager, region_id, download_dir):
        super().__init__()
        self.dl_manager = dl_manager
        self.region_id = region_id
        self.download_dir = download_dir

    def run(self):
        try:
            self.dl_manager.download_dir = self.download_dir
            region = self.dl_manager.regions.get(self.region_id)

            def progress_callback(progress_data):
                self.progress.emit(self.region_id, progress_data)

            success = region.install_release(
                progress_callback=progress_callback,
                noclean=self.dl_manager.noclean
            )
            self.finished.emit(self.region_id, success)

        except Exception as err:
            tb = traceback.format_exc()
            self.error.emit(self.region_id, str(err))
            log.error(tb)

class SceneryUninstallWorker(QThread):
    """Worker thread for uninstalling scenery"""
    finished = Signal(str, bool)  # region_id, success
    error = Signal(str, str)  # region_id, error_message
    
    def __init__(self, dl_manager, region_id):
        super().__init__()
        self.dl_manager = dl_manager
        self.region_id = region_id
    
    def run(self):
        try:
            success = self.dl_manager.regions[self.region_id].local_rel.uninstall()
            self.finished.emit(self.region_id, success)
        except Exception as err:
            tb = traceback.format_exc()
            self.error.emit(self.region_id, str(err))
            log.error(tb)


class UpdateCheckWorker(QThread):
    """Worker thread to check for updates from GitHub releases"""
    result = Signal(object)  # tuple(latest_version_str, html_url) or None
    error = Signal(str)

    def run(self):
        try:
            api_url = "https://api.github.com/repos/ProgrammingDinosaur/autoortho4xplane/releases/latest"
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "autoortho4xplane-update-check"
            }
            resp = requests.get(api_url, timeout=7, headers=headers)
            if resp.status_code != 200:
                self.result.emit(None)
                return
            data = resp.json()
            tag = data.get("tag_name") or data.get("name") or ""
            html_url = data.get("html_url") or "https://github.com/ProgrammingDinosaur/autoortho4xplane/releases"
            self.result.emit((tag, html_url))
        except Exception as err:
            self.error.emit(str(err))

class AddSeasonsWorker(QThread):
    """Worker thread for adding seasons"""
    finished = Signal(str, bool)  # region_id, success
    error = Signal(str, str)  # region_id, error_message
    progress = Signal(str, dict)  # region_id, progress_data

    def __init__(self, scenery_name: str, scenery_path: str):
        super().__init__()
        self.scenery_name = scenery_name
        self.scenery_path = os.path.join(scenery_path, "z_autoortho", "scenery", self.scenery_name)

    def run(self):
        """Run the worker thread"""
        try:
            log.info(f"Adding seasons to {self.scenery_name}")
            def progress_callback(progress_data):
                self.progress.emit(self.scenery_name, progress_data)

            success = dsf_utils.add_seasons_to_package(self.scenery_name, progress_callback=progress_callback)

            log.info(f"Finished adding seasons to {self.scenery_name}")
            self.progress.emit(self.scenery_name, {"stage": "finished"})
            self.finished.emit(self.scenery_name, success)
        except Exception as err:
            tb = traceback.format_exc()
            log.error(tb)
            self.error.emit(self.scenery_name, str(err))


class RestoreDefaultDsfsWorker(QThread):
    """Worker thread for restoring default DSFs"""
    finished = Signal(str, bool)  # region_id, success
    error = Signal(str, str)  # region_id, error_message
    progress = Signal(str, dict)  # region_id, progress_data
    
    def __init__(self, dl_manager, region_id):
        super().__init__()
        self.dl_manager = dl_manager
        self.region_id = region_id
    
    def run(self):
        try:
            def progress_callback(progress_data):
                self.progress.emit(self.region_id, progress_data)

            success = dsf_utils.restore_default_dsfs(self.region_id, progress_callback=progress_callback)
            self.finished.emit(self.region_id, success)
        except Exception as err:
            tb = traceback.format_exc()
            self.error.emit(self.region_id, str(err))
            log.error(tb)

class StyledButton(QPushButton):
    """Custom styled button with hover effects"""
    def __init__(self, text, primary=False):
        super().__init__(text)
        self.primary = primary
        self.setStyleSheet(self._get_style())

    def _get_style(self):
        if self.primary:
            return """
                QPushButton {
                    background-color: #1d71d1;
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: 4px;
                    font-weight: bold;
                    font-size: 14px;
                }
                QPushButton:hover {
                    background-color: #5183bd;
                }
                QPushButton:pressed {
                    background-color: #E55B25;
                }
                QPushButton:disabled {
                    background-color: #666;
                    color: #999;
                }
            """
        else:
            return """
                QPushButton {
                    background-color: #3A3A3A;
                    color: white;
                    border: 1px solid #555;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 13px;
                }
                QPushButton:hover {
                    background-color: #4A4A4A;
                    border-color: #1d71d1;
                }
                QPushButton:pressed {
                    background-color: #2A2A2A;
                }
                QPushButton:disabled {
                    background-color: #2A2A2A;
                    color: #666;
                    border-color: #333;
                }
            """


class ModernSlider(QSlider):
    """Custom styled slider"""
    def __init__(self, orientation=Qt.Orientation.Horizontal):
        super().__init__(orientation)
        self.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #3A3A3A;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #6da4e3;
                width: 18px;
                height: 18px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:hover {
                background: #5183bd;
            }
            QSlider::sub-page:horizontal {
                background: #6da4e3;
                border-radius: 3px;
            }
        """)
    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


class ModernSpinBox(QSpinBox):
    """Custom styled spinbox"""
    def __init__(self):
        super().__init__()
        self.setStyleSheet("""
            QSpinBox {
                background-color: #3A3A3A;
                border: 1px solid #555;
                padding: 6px;
                border-radius: 4px;
                color: white;
                min-width: 80px;
            }
            QSpinBox:focus {
                border-color: #6da4e3;
            }
            QSpinBox::up-button {
                background-color: #6da4e3;
                border: none;
                border-radius: 2px;
                width: 16px;
            }
            QSpinBox::up-button:hover {
                background-color: #5183bd;
            }
            QSpinBox::up-arrow {
                image: url(:/imgs/plus-16.png);
                width: 12px;
                height: 12px;
            }
            QSpinBox::down-button {
                background-color: #6da4e3;
                border: none;
                border-radius: 2px;
                width: 16px;
            }
            QSpinBox::down-button:hover {
                background-color: #5183bd;
            }
            QSpinBox::down-arrow {
                image: url(:/imgs/minus-16.png);
                width: 12px;
                height: 12px;
            }
        """)


class ConfigUI(QMainWindow):
    """Main configuration UI window using PyQt6"""

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

        # Setup UI
        self.init_ui()

        # Connect signals
        self.status_update.connect(self.update_status_bar)
        self.log_update.connect(self.append_log)
        self.show_error.connect(self.display_error)

        # Start log update timer
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.update_logs)
        self.log_timer.start(1000)

        self.ready.set()

        # Kick off asynchronous update check shortly after startup
        try:
            QTimer.singleShot(250, self.start_update_check)
        except Exception:
            pass

    def init_ui(self):
        """Initialize the user interface"""
        self.setWindowTitle(f'AutoOrtho ver {__version__}')
        self.setGeometry(100, 100, 900, 700)

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

        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 10)
        central_widget.setLayout(main_layout)

        # Add banner
        banner_label = QLabel()
        banner_pixmap = QPixmap(":/imgs/banner1.png")
        banner_label.setPixmap(
            banner_pixmap.scaled(
                QSize(400, 100),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        )
        banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(banner_label)

        # Create tab widget
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        main_layout.addWidget(self.tabs)

        # Create tabs
        self.create_setup_tab()
        self.create_scenery_tab()
        self.create_settings_tab()
        self.create_logs_tab()

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        self.run_button = StyledButton("Run", primary=True)
        self.run_button.clicked.connect(self.on_run)

        self.save_button = StyledButton("Save Config")
        self.save_button.clicked.connect(self.on_save)

        self.quit_button = StyledButton("Quit")
        self.quit_button.clicked.connect(self.close)

        button_layout.addStretch()
        button_layout.addWidget(self.run_button)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.quit_button)

        main_layout.addLayout(button_layout)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def create_setup_tab(self):
        """Create the setup configuration tab"""
        setup_widget = QWidget()

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

        # Paths group
        paths_group = QGroupBox("Paths Configuration")
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
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.cache_dir_edit)
        )
        cache_layout.addWidget(browse_btn)
        paths_layout.addLayout(cache_layout)

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
        browse_btn.clicked.connect(
            lambda: self.browse_folder(self.download_dir_edit)
        )
        download_layout.addWidget(browse_btn)
        paths_layout.addLayout(download_layout)

        layout.addWidget(paths_group)

        # Options group
        options_group = QGroupBox("Basic Settings")
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
        maptype_label = QLabel("Map type override:")
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
        self.maptype_combo.addItems(MAPTYPES)
        self.maptype_combo.setCurrentText(self.cfg.autoortho.maptype_override)
        self.maptype_combo.setObjectName('maptype_override')
        self.maptype_combo.setToolTip(
            "Select a specific map provider. Use Auto to use the source based on the tile default (base scenery uses BI)."
        )
        maptype_layout.addWidget(self.maptype_combo)
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

        self.using_custom_tiles_check = QCheckBox("Using Custom Tiles")
        self.using_custom_tiles_check.setChecked(self.cfg.autoortho.using_custom_tiles)
        self.using_custom_tiles_check.setObjectName('using_custom_tiles')
        self.using_custom_tiles_check.setToolTip(
            "Enable this if you are using custom build Ortho4XP tiles instead or along with base scenery packages from autoortho.\n"
            "NOTE: By using this option the Max Zoom near airports setting will be ignored and all tiles will be capped to the general max zoom level you set in advanced settings."
        )

        self.using_custom_tiles_check.stateChanged.connect(self.on_using_custom_tiles_check)
        options_layout.addWidget(self.using_custom_tiles_check)


        layout.addWidget(options_group)
    
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
        layout = QVBoxLayout()
        scenery_widget.setLayout(layout)

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
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        logs_widget.setLayout(layout)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

        self.tabs.addTab(logs_widget, "Logs")

    def refresh_settings_tab(self):
        """Refresh the settings tab"""
        while self.settings_layout.count():
            child = self.settings_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        cache_group = QGroupBox("Cache Settings")
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

        clean_cache_controls_layout = QHBoxLayout()
        clean_cache_controls_layout.setSpacing(10)
        self.clean_cache_btn = StyledButton("Clean Cache")
        self.clean_cache_btn.clicked.connect(self.on_clean_cache)
        self.clean_cache_btn.setToolTip(
            "Delete cache files until the file cache clean limit is reached.\n"
            "This will delete the oldest cached images first.\n"
            "If the cache is smaller than the clean limit, no files are deleted.\n"
            "Note that this can take a long time."
        )
        clean_cache_controls_layout.addWidget(self.clean_cache_btn)
        self.auto_clean_cache_check = QCheckBox("Auto clean file cache on AutoOrtho exit")
        self.auto_clean_cache_check.setChecked(self.cfg.cache.auto_clean_cache)
        self.auto_clean_cache_check.setObjectName('auto_clean_cache')
        self.auto_clean_cache_check.setToolTip(
            "Automatically clean cache when AutoOrtho exits.\n"
            "Note that this can take a long time."
        )

        clean_cache_controls_layout.addWidget(self.auto_clean_cache_check)
        clean_cache_controls_layout.addStretch()
        cache_layout.addLayout(clean_cache_controls_layout)

        self.settings_layout.addWidget(cache_group)

        # AutoOrtho Settings group
        autoortho_group = QGroupBox("AutoOrtho Settings")
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

        max_zoom_tooltip = (
            "Maximum zoom level for imagery downloads.\n"
            "Higher values = more detail but larger downloads and more VRAM usage.\n"
            "Optimal: 16 for most cases. Keep in mind that every extra ZL increases VRAM and potential network usage by 4x.\n"
        )
        if self.cfg.autoortho.using_custom_tiles:
            max_zoom_tooltip += "IMPORTANT: You are using custom tiles, you can set this to 19 if your tiles are built for higher ZL it.\n"
            "But be aware that in-game zoom level will be capped to tile default zoom level + 1 (only X-Plane 12)."

        max_zoom_layout = QHBoxLayout()
        max_zoom_label = QLabel("Maximum zoom level:")
        max_zoom_label.setToolTip(max_zoom_tooltip)
        max_zoom_layout.addWidget(max_zoom_label)
        self.max_zoom_slider = ModernSlider()
        self.max_zoom_slider.setRange(12, 17 if not self.cfg.autoortho.using_custom_tiles else 19) # Max X-Plane allows is tile zoom + 1 , 17 accounts for kubilus mesh
        self.max_zoom_slider.setValue(int(self.cfg.autoortho.max_zoom))
        self.max_zoom_slider.setObjectName('max_zoom')
        self.max_zoom_slider.setToolTip(
            "Drag to adjust maximum zoom level (12=low detail, 17=high detail)"
        )
        self.max_zoom_label = QLabel(f"{self.cfg.autoortho.max_zoom}")
        self.max_zoom_slider.valueChanged.connect(
            lambda v: (
                self.validate_min_and_max_zoom("max")
            )
        )
        max_zoom_layout.addWidget(self.max_zoom_slider)
        max_zoom_layout.addWidget(self.max_zoom_label)
        autoortho_layout.addLayout(max_zoom_layout)

        # Max zoom near airports
        max_zoom_near_airports_layout = QHBoxLayout()
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
                self.validate_max_zoom_near_airports()
            )
        )
        max_zoom_near_airports_layout.addWidget(self.max_zoom_near_airports_slider)
        max_zoom_near_airports_layout.addWidget(self.max_zoom_near_airports_label)

        if not self.cfg.autoortho.using_custom_tiles:
            autoortho_layout.addLayout(max_zoom_near_airports_layout)

        # Max wait time
        maxwait_layout = QHBoxLayout()
        maxwait_label = QLabel("Max wait time (seconds):")
        maxwait_label.setToolTip(
            "Maximum time to wait for single imagery downloads before timing out.\n"
            "Lower values = faster response but may have green or blank tiles\n"
            "Higher values = better change at getting tiles but stutters and missing tiles while they load\n"
            "Default: 0.5 seconds\n"
            "Optimal: 2 seconds is a good compromise.\n"
            "Increase this if you are using higher zoom levels and/or have a slow internet connection."
        )
        maxwait_layout.addWidget(maxwait_label)
        self.maxwait_slider = ModernSlider()
        self.maxwait_slider.setRange(1, 100)
        self.maxwait_slider.setSingleStep(1)
        # Convert maxwait to int for slider (multiply by 10 for 0.1 precision)
        maxwait_value = int(float(self.cfg.autoortho.maxwait) * 10)
        self.maxwait_slider.setValue(maxwait_value)
        self.maxwait_slider.setObjectName('maxwait')
        self.maxwait_slider.setToolTip(
            "Drag to adjust maximum wait time in seconds"
        )
        self.maxwait_label = QLabel(f"{self.cfg.autoortho.maxwait}")
        self.maxwait_slider.valueChanged.connect(
            lambda v: self.maxwait_label.setText(f"{v/10:.1f}")
        )
        maxwait_layout.addWidget(self.maxwait_slider)
        maxwait_layout.addWidget(self.maxwait_label)
        autoortho_layout.addLayout(maxwait_layout)

        # Fetch threads
        threads_layout = QHBoxLayout()
        threads_label = QLabel("Fetch threads per mount:" if self.system == "darwin" else "Global fetch threads:")
        threads_label.setToolTip(
            "Number of simultaneous download threads.\n"
            "More threads = faster downloads but higher CPU/network usage.\n"
            "Too many threads may cause timeouts or instability.\n"
            "On macOS, this is the number of threads per mount.\n"
            "On other systems, fetch threads are shared globally."
        )
        threads_layout.addWidget(threads_label)
        self.fetch_threads_spinbox = ModernSpinBox()

        max_threads = 1000
        self.fetch_threads_spinbox.setRange(1, max_threads)

        # Ensure initial value doesn't exceed available threads
        initial_threads = min(
            int(self.cfg.autoortho.fetch_threads), max_threads
        )
        self.fetch_threads_spinbox.setValue(initial_threads)
        self.fetch_threads_spinbox.setObjectName('fetch_threads')
        self.fetch_threads_spinbox.setToolTip(
            f"Number of download threads per mount (1-{max_threads})" if self.system == "darwin" else f"Number of global download threads (1-{max_threads})"
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

        self.settings_layout.addWidget(autoortho_group)

        # Seasons Settings group
        seasons_group = QGroupBox("Seasons")
        seasons_layout = QVBoxLayout()
        seasons_group.setLayout(seasons_layout)

        # Enable/Disable controls
        seasons_toggle_layout = QHBoxLayout()
        self.seasons_enabled_radio = QRadioButton("Enabled")
        self.seasons_disabled_radio = QRadioButton("Disabled")
        seasons_enabled = bool(self.cfg.seasons.enabled)
        self.seasons_enabled_radio.setChecked(seasons_enabled)
        self.seasons_disabled_radio.setChecked(not seasons_enabled)
        self.seasons_enabled_radio.toggled.connect(self.on_seasons_enabled_toggled)
        self.seasons_disabled_radio.toggled.connect(self.on_seasons_enabled_toggled)
        seasons_toggle_layout.addWidget(self.seasons_enabled_radio)
        seasons_toggle_layout.addWidget(self.seasons_disabled_radio)
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

        # Seasons convert workers
        seasons_convert_workers_row = QHBoxLayout()
        seasons_convert_workers_label = QLabel("DSF Seasons convert workers:")
        self.seasons_convert_workers_slider = ModernSlider()
        self.seasons_convert_workers_slider.setRange(1, os.cpu_count())
        self.seasons_convert_workers_slider.setValue(int(self.cfg.seasons.seasons_convert_workers))
        self.seasons_convert_workers_slider.setObjectName('seasons_convert_workers')
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
        seasons_layout.addLayout(seasons_convert_workers_row)

        # Compress DSF

        compress_dsf_row = QHBoxLayout()
        self.compress_dsf_check = QCheckBox("Compress DSF after conversion")
        self.compress_dsf_check.setChecked(self.cfg.seasons.compress_dsf)
        self.compress_dsf_check.setObjectName('compress_dsf')
        self.compress_dsf_check.setToolTip("Compress DSF to 7z format after conversion to XP12 format")
        compress_dsf_row.addWidget(self.compress_dsf_check)
        seasons_layout.addLayout(compress_dsf_row)

        # Initialize enabled state of sliders
        self._set_seasons_controls_enabled(seasons_enabled)

        self.settings_layout.addWidget(seasons_group)

        # DDS Compression Settings group
        dds_group = QGroupBox("DDS Compression Settings")
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

        self.hide_check = QCheckBox("Hide window when running")
        self.hide_check.setChecked(self.cfg.general.hide)
        self.hide_check.setObjectName('hide')
        self.hide_check.setToolTip(
            "Minimize AutoOrtho window to system tray when running.\n"
            "Helps keep desktop clean during long flights.\n"
            "You can still access it from the system tray."
        )
        general_layout.addWidget(self.hide_check)

        self.debug_check = QCheckBox("Debug mode")
        self.debug_check.setChecked(self.cfg.general.debug)
        self.debug_check.setObjectName('debug')
        self.debug_check.setToolTip(
            "Enable detailed logging for troubleshooting.\n"
            "Creates larger log files with more information.\n"
            "Only enable if experiencing issues or when asked by support."
        )
        general_layout.addWidget(self.debug_check)

        self.settings_layout.addWidget(general_group)

        # Scenery Settings group
        scenery_group = QGroupBox("Scenery Settings")
        scenery_layout = QVBoxLayout()
        scenery_group.setLayout(scenery_layout)

        self.noclean_check = QCheckBox("Don't cleanup downloads")
        self.noclean_check.setChecked(self.cfg.scenery.noclean)
        self.noclean_check.setObjectName('noclean')
        self.noclean_check.setToolTip(
            "Keep downloaded scenery files after installation.\n"
            "Useful for reinstalling or sharing scenery packages.\n"
            "Warning: Can use significant disk space over time.\n"
            "Recommended: Disabled unless you need the original files."
        )
        scenery_layout.addWidget(self.noclean_check)

        self.settings_layout.addWidget(scenery_group)

        # FUSE Settings group
        fuse_group = QGroupBox("FUSE Settings")
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

        self.settings_layout.addStretch()

    def show_missing_color_dialog(self):
        color = QColorDialog.getColor(
            self.missing_color, self, "Select missing tile color"
        )
        if color.isValid():
            self.missing_color = color
            self.update_missing_color_button()

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
            enabled = self.seasons_enabled_radio.isChecked()
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

    def refresh_scenery_list(self):
        """Refresh the scenery list display"""
        # Clear existing widgets
        while self.scenery_layout.count():
            child = self.scenery_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self.dl.find_regions()

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
                if seasons_apply_status == downloader.SeasonsApplyStatus.NOT_APPLIED:
                    status_btn_text = "Add Native Seasons"
                    status_btn_color = "#2d78ba"
                elif seasons_apply_status == downloader.SeasonsApplyStatus.PARTIALLY_APPLIED:
                    status_btn_text = "Partially Added Seasons"
                    status_btn_color = "#db7100"
                elif seasons_apply_status == downloader.SeasonsApplyStatus.APPLIED:
                    status_btn_text = "Seasons Added"
                    status_btn_color = "#4CAF50"
                else:
                    status_btn_text = "Add Native Seasons"
                    status_btn_color = "#2d78ba"

                package_name = os.path.basename(latest.subfolder_dir)
                if package_name not in self.installed_package_names:
                    self.installed_package_names.append(package_name)

                # Status button (clickable when seasons not yet applied)
                seasons_status_btn = StyledButton(status_btn_text, primary=False)
                seasons_status_btn.setFixedSize(200,35)
                seasons_status_btn.setStyleSheet(
                    f"""
                    background-color: {status_btn_color};
                    font-size: 16px;
                    font-weight: bold;
                    text-align: center;
                    line-height: 30px;
                    """
                )
                if seasons_apply_status == downloader.SeasonsApplyStatus.NOT_APPLIED:
                    seasons_status_btn.setEnabled(True)
                    seasons_status_btn.setObjectName(f"seasons-options-{package_name}")
                    seasons_status_btn.clicked.connect(
                        lambda checked, rid=package_name, st=seasons_apply_status: (
                            self.on_add_seasons(rid, st)
                        )
                    )
                else:
                    seasons_status_btn.setEnabled(False)
                    seasons_status_btn.setObjectName(f"seasons-status-{package_name}")

                # Seasons Options button (menu) - only when seasons are partially or fully added
                seasons_options_btn = None
                if seasons_apply_status in (
                    downloader.SeasonsApplyStatus.PARTIALLY_APPLIED,
                    downloader.SeasonsApplyStatus.APPLIED,
                ):
                    seasons_options_btn = StyledButton("Seasons Options", primary=False)
                    seasons_options_btn.setFixedSize(200,35)
                    seasons_options_btn.setStyleSheet(
                        f"""
                        background-color: #2d78ba;
                        font-size: 16px;
                        font-weight: bold;
                        text-align: center;
                        line-height: 30px;
                        """
                    )
                    seasons_options_btn.setObjectName(f"seasons-options-{package_name}")
                    seasons_options_btn.clicked.connect(
                        lambda checked, rid=package_name: (
                            self.on_seasons_options_clicked(rid)
                        )
                    )
                h_layout = QHBoxLayout()
                h_layout.addWidget(seasons_status_btn)
                if seasons_options_btn:
                    h_layout.addWidget(seasons_options_btn)
                h_layout.addWidget(delete_btn)

                dsf_progress_bar = QProgressBar()
                dsf_progress_bar.setVisible(False)
                dsf_progress_bar.setObjectName(f"dsf-progress-bar-{package_name}")
                dsf_progress_bar.setToolTip("Progress of adding seasons to DSFs")
                dsf_progress_bar.setRange(0, 100)

                item_layout.addLayout(h_layout)
                item_layout.addWidget(dsf_progress_bar)


            self.scenery_layout.addWidget(item_frame)

        self.scenery_layout.addStretch()

    def on_restore_default_dsfs(self, region_id):
        """Handle restoring default DSFs"""
        # Button now is seasons-options, disable it while working
        button = self.findChild(QPushButton, f"seasons-options-{region_id}")
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
        self.show_error.emit(f"Failed to restore default DSFs to {region_id}:\n{error_msg}")
        self.on_restore_default_dsfs_finished(region_id, False)

    def on_restore_default_dsfs_finished(self, region_id, success):
        """Handle restore default DSFs completion"""
        button = self.findChild(QPushButton, f"seasons-options-{region_id}")
        if button:
            button.setEnabled(True)
            button.setText("Seasons Options")
        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setVisible(False)
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
        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setValue(progress_data["pcnt_done"])

    def on_seasons_options_clicked(self, region_id):
        """Show Seasons Options menu with Repair, Reapply, Restore"""
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
        button = self.findChild(QPushButton, f"uninstall-{region_id}")
        if button:
            button.setEnabled(False)
            button.setText("Uninstalling...")

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
            self.max_zoom_near_airports_slider.setValue(self.max_zoom_slider.value())
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
                self.min_zoom_slider.setValue(current_value)
            elif instigator == "max":
                current_value = int(self.max_zoom_label.text())
                self.max_zoom_slider.setValue(current_value)
            else:
                raise ValueError(f"Invalid instigator: {instigator}")
        else:
            if instigator == "min":
                self.min_zoom_label.setText(f"{self.min_zoom_slider.value()}")
            elif instigator == "max":
                self.max_zoom_label.setText(f"{self.max_zoom_slider.value()}")
                if self.max_zoom_near_airports_slider.value() < self.max_zoom_slider.value():
                    self.max_zoom_near_airports_slider.setValue(self.max_zoom_slider.value())
                    self.max_zoom_near_airports_label.setText(f"{self.max_zoom_slider.value()}")
            else:
                raise ValueError(f"Invalid instigator: {instigator}")

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

    def browse_folder(self, line_edit):
        """Open folder browser dialog"""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            line_edit.text()
        )
        if folder:
            line_edit.setText(folder)

    def on_run(self):
        """Handle Run button click"""
        # Block run while seasons are being added
        try:
            if getattr(self, 'add_seasons_workers', None) and len(self.add_seasons_workers) > 0:
                QMessageBox.warning(
                    self,
                    "Seasons In Progress",
                    "Cannot Run while Native Seasons are being added. Please wait for the seasons operation to finish."
                )
                self.update_status_bar("Run blocked: adding seasons in progress")
                return
        except Exception:
            pass
        # Disable Add Seasons buttons while running
        try:
            for rid in self.installed_packages:
                btn = self.findChild(QPushButton, f"seasons-options-{rid}")
                if btn:
                    btn.setEnabled(False)
                    btn.setToolTip("Disabled while AutoOrtho is running")
        except Exception:
            pass
        self.save_config()
        self.cfg.load()
        # Preflight check: prompt to unmount previous mounts if detected
        try:
            if not self.preflight_mount_check_and_prompt():
                self.update_status_bar("Run cancelled by user")
                return
        except Exception:
            # Non-fatal; continue
            pass
        self.update_status_bar("Mounting sceneries...")
        self.run_button.setEnabled(False)
        self.run_button.setText("Running")
        self.mount_sceneries(blocking=False)
        self.verify()
        self.running = True  # Set running state
        self.update_status_bar("Running")
        self.showMinimized()

    def on_save(self):
        """Handle Save button click"""
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
                self.update_status_bar("Save cancelled.")
                return
        # Check if program is already running
        if self.running:
            reply = QMessageBox.question(
                self,
                "Save Settings While Running",
                "AutoOrtho Injection is already running. Some settings may not take effect until you restart AutoOrtho.\n\n"
                "Do you want to save the settings anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            
            if reply == QMessageBox.StandardButton.No:
                self.update_status_bar("Save cancelled")
                return
        
        self.save_config()
        self.cfg.load()
        self.refresh_scenery_list()

        
        if self.running:
            self.update_status_bar("Configuration saved - some changes may require restart")
            QMessageBox.information(
                self,
                "Settings Saved",
                "Settings have been saved. Some changes may not take effect until you restart AutoOrtho."
            )
        else:
            self.update_status_bar("Configuration saved")

    def on_clean_cache(self, for_exit=False):
        """Handle Clean Cache button click

        Args:
            for_exit (bool): When True, invoked from closeEvent - suppress dialogs
                             and allow closeEvent to wait on the thread.
        """

        if self.running:
            QMessageBox.warning(
                self,
                "Cannot clean cache while running",
                "Cannot clean cache while AutoOrtho injection is running. Please stop AutoOrtho and try again."
            )
            return

        self._closing = for_exit
        self.update_status_bar("Cleaning cache...")
        if hasattr(self, 'clean_cache_btn'):
            self.clean_cache_btn.setEnabled(False)
        if hasattr(self, 'run_button'):
            self.run_button.setEnabled(False)

        # Run in separate thread and keep reference so we can wait on exit
        self.cache_thread = QThread()
        self.cache_thread.run = lambda: self.clean_cache(
            self.cfg.paths.cache_dir,
            int(self.file_cache_slider.value())
        )
        self.cache_thread.finished.connect(lambda: self.on_cache_cleaned(for_exit))
        self.cache_thread.start()

    def on_cache_cleaned(self, for_exit=False):
        """Called when cache cleaning is complete"""
        try:
            if hasattr(self, 'clean_cache_btn'):
                self.clean_cache_btn.setEnabled(True)
            if hasattr(self, 'run_button'):
                self.run_button.setEnabled(True)
            if not for_exit:
                QMessageBox.information(
                    self, "Cache Cleaned", "Cache cleaning completed!"
                )
        finally:
            # Clean up the thread reference
            if self.cache_thread is not None:
                try:
                    self.cache_thread.quit()
                except Exception:
                    pass
                try:
                    self.cache_thread.wait()
                except Exception:
                    pass
                try:
                    self.cache_thread.deleteLater()
                except Exception:
                    pass
                self.cache_thread = None
            # If invoked during shutdown, finalize closing without blocking UI
            if for_exit:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._finalize_shutdown)

    def on_install_scenery(self, region_id):
        """Handle scenery installation"""
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

        # Create worker thread
        worker = SceneryDownloadWorker(
            self.dl, region_id, self.cfg.paths.download_dir
        )
        worker.progress.connect(self.on_download_progress)
        worker.finished.connect(self.on_download_finished)
        worker.error.connect(self.on_download_error)

        self.download_workers[region_id] = worker
        worker.start()

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

        # Button no longer exists directly; use seasons-options for state changes
        button = self.findChild(QPushButton, f"seasons-options-{region_id}")
        if button is None:
            return

        # If something is already processing, enqueue this request
        if self.add_seasons_current is not None:
            # Avoid duplicates in queue
            if region_id not in self.add_seasons_queue:
                self.add_seasons_queue.append(region_id)
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
        button = self.findChild(QPushButton, f"seasons-options-{region_id}")
        if button:
            button.setEnabled(not self.running)
            button.setText("Seasons Options")

        if success:
            self.update_status_bar(f"Successfully added seasons to {region_id}")
            button.setText("Native Seasons Added")
        else:
            self.update_status_bar(f"Failed to add seasons to {region_id}")
            button.setText("Add Native Seasons")

        dsf_progress_bar = self.findChild(QProgressBar, f"dsf-progress-bar-{region_id}")
        if dsf_progress_bar:
            dsf_progress_bar.setVisible(False)

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
        """Disable Run while seasons jobs are active; re-enable otherwise when not running"""
        try:
            if self._has_active_seasons_jobs():
                if hasattr(self, 'run_button'):
                    self.run_button.setEnabled(False)
                    self.run_button.setToolTip("Disabled: Native Seasons are being added")
                # Also disable Seasons Options buttons
                try:
                    for rid in getattr(self, 'installed_package_names', []):
                        btn = self.findChild(QPushButton, f"seasons-options-{rid}")
                        if btn:
                            btn.setEnabled(False)
                except Exception:
                    pass
            else:
                if hasattr(self, 'run_button') and not self.running:
                    self.run_button.setEnabled(True)
                    self.run_button.setToolTip("")
                    try:
                        self.run_button.setText("Run")
                    except Exception:
                        pass
                # Re-enable Seasons Options buttons when idle
                try:
                    for rid in getattr(self, 'installed_package_names', []):
                        btn = self.findChild(QPushButton, f"seasons-options-{rid}")
                        if btn:
                            btn.setEnabled(True)
                except Exception:
                    pass
        except Exception:
            pass

    def _start_add_seasons_job(self, region_id):
        """Internal helper to begin processing a single add-seasons job for region_id."""
        try:
            button = self.findChild(QPushButton, f"seasons-options-{region_id}")
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
                # If nothing pending, re-enable all seasons options buttons
                if not self._has_active_seasons_jobs():
                    for rid in self.installed_packages:
                        btn = self.findChild(QPushButton, f"seasons-options-{rid}")
                        if btn:
                            btn.setEnabled(True)
        except Exception:
            pass

    def on_add_seasons_progress(self, region_id, progress_data):
        """Handle add seasons progress"""

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
                    btn = self.findChild(QPushButton, f"seasons-options-{rid}")
                    if not btn:
                        continue
                    if rid in self.add_seasons_queue:
                        btn.setEnabled(False)
                        btn.setText("Queued for seasons…")
                    else:
                        # If not queued and not current, ensure enabled state only if nothing running
                        if not self._has_active_seasons_jobs():
                            btn.setEnabled(True)
        except Exception:
            pass


    def on_uninstall_error(self, region_id, error_msg):
        """Handle uninstall error"""
        self.show_error.emit(f"Failed to uninstall {region_id}:\n{error_msg}")
        self.on_uninstall_finished(region_id, False)

    def on_uninstall_finished(self, region_id, success):
        """Handle uninstall completion"""
        button = self.findChild(QPushButton, f"uninstall-{region_id}")
        if button:
            button.setEnabled(True)

        if success:
            self.update_status_bar(f"Successfully uninstalled {region_id}")
            self.refresh_scenery_list()
            button.setText("Uninstalled")
        else:
            self.update_status_bar(f"Failed to uninstall {region_id}")
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
        """Update download progress"""
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
        if stage == 'verify':
            # Switch to verification mode: show a single bar (overall), hide current
            if progress_current:
                progress_current.setVisible(False)
            if progress_overall:
                progress_overall.setVisible(True)
                progress_overall.setValue(int(progress_data.get('verify_pcnt', 0)))
            # Also change button text while verifying
            button = self.findChild(QPushButton, f"scenery-{region_id}")
            if button:
                button.setText("Verifying...")
            # Update status with verification state
            status = progress_data.get('status', 'Verifying...')
            self.update_status_bar(f"{region_id}: {status}")
            return
        else:
            pcnt_done = progress_data.get('pcnt_done', 0)
            overall_pcnt = progress_data.get('overall_pcnt')
            files_done = progress_data.get('files_done')
            files_total = progress_data.get('files_total')

            if progress_current is not None:
                progress_current.setVisible(True)
                progress_current.setValue(int(pcnt_done))

            if progress_overall is not None:
                if overall_pcnt is None and files_done is not None and files_total:
                    try:
                        overall_pcnt = (float(files_done) / float(files_total)) * 100.0
                    except Exception:
                        overall_pcnt = 0
                if overall_pcnt is None:
                    overall_pcnt = 0
                progress_overall.setVisible(True)
                progress_overall.setValue(int(overall_pcnt))

        status = progress_data.get('status', 'Downloading...')
        MBps = progress_data.get('MBps', 0)
        try:
            if pcnt_done > 0:
                self.update_status_bar(
                    f"{region_id}: {pcnt_done:.1f}% ({MBps:.1f} MB/s)"
                )
            else:
                self.update_status_bar(f"{region_id}: {status}")
        except UnboundLocalError:
            # If pcnt_done wasn't defined (e.g., stage mismatch), fall back to status
            self.update_status_bar(f"{region_id}: {status}")

    def on_download_finished(self, region_id, success):
        """Handle download completion"""
        button = self.findChild(QPushButton, f"scenery-{region_id}")
        progress_current = self.findChild(QProgressBar, f"progress-current-{region_id}")
        progress_overall = self.findChild(QProgressBar, f"progress-overall-{region_id}")

        if success:
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
        self.show_error.emit(f"Failed to install {region_id}:\n{error_msg}")
        self.on_download_finished(region_id, False)

    def save_config(self):
        """Save configuration from UI to config object"""
        self.ready.clear()

        # Save paths
        self.cfg.paths.scenery_path = self.scenery_path_edit.text()
        self.cfg.paths.xplane_path = self.xplane_path_edit.text()
        self.cfg.paths.cache_dir = self.cache_dir_edit.text()
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
            self.cfg.autoortho.maxwait = str(
                self.maxwait_slider.value() / 10.0
            )
            self.cfg.autoortho.fetch_threads = str(
                self.fetch_threads_spinbox.value()
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
            self.cfg.general.debug = self.debug_check.isChecked()

            # Scenery settings
            self.cfg.scenery.noclean = self.noclean_check.isChecked()
            self.dl.noclean = self.cfg.scenery.noclean

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
            self.cfg.seasons.enabled = self.seasons_enabled_radio.isChecked()
            self.cfg.seasons.compress_dsf = self.compress_dsf_check.isChecked()
            self.cfg.seasons.seasons_convert_workers = str(self.seasons_convert_workers_slider.value())
            self.cfg.seasons.spr_saturation = str(self.spr_sat_slider.value())
            self.cfg.seasons.sum_saturation = str(self.sum_sat_slider.value())
            self.cfg.seasons.fal_saturation = str(self.fal_sat_slider.value())
            self.cfg.seasons.win_saturation = str(self.win_sat_slider.value())

        self.cfg.save()
        self.ready.set()
        self.refresh_scenery()

    def preflight_mount_check_and_prompt(self):
        """Detect lingering mounts and prompt user to unmount/clean.

        Returns True if it's OK to proceed with Run, False if user cancels.
        """
        try:
            lingering = []
            for scenery in self.cfg.scenery_mounts:
                mount = scenery.get('mount')
                if not mount:
                    continue
                try:
                    if os.path.ismount(mount):
                        lingering.append(mount)
                except Exception:
                    continue
            if not lingering:
                return True

            msg = (
                "Previous AutoOrtho mounts are still active:\n\n"
                + "\n".join(lingering)
                + "\n\nDo you want AutoOrtho to unmount them now?"
            )
            reply = QMessageBox.question(
                self,
                "Existing Mounts Detected",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

            # Attempt unmount via AOMount implementation if available
            for scenery in self.cfg.scenery_mounts:
                try:
                    mount = scenery.get('mount')
                    cleanup_mountpoint(mount)
                    log.info(f"Cleaned up mountpoint: {mount}")
                except Exception:
                    log.error(f"Failed to cleanup mountpoint: {mount}")

            # Brief wait loop for unmount completion
            import time as _time
            deadline = _time.time() + 10
            while _time.time() < deadline:
                if not any(os.path.ismount(x) for x in lingering):
                    break
                _time.sleep(0.3)
            if any(os.path.ismount(x) for x in lingering):
                QMessageBox.warning(
                    self,
                    "Unmount Incomplete",
                    "Some mounts could not be unmounted automatically.\n"
                    "Please remove the z_ao_<scenery_name> directories from your Custom Scenery directory manually and run AutoOrtho again."
                )
            else:
                log.info("All mounts cleaned up successfully")
            return True
        except Exception:
            return True

    def on_using_custom_tiles_check(self, state):
        """Handle using custom tiles check"""
        if state == False: 
            if self.cfg.autoortho.using_custom_tiles == True and int(self.cfg.autoortho.max_zoom) > 17:
                log.info(f"Max zoom being capped to 17 after custom tiles disabled")
                self.cfg.autoortho.max_zoom = 17
            self.cfg.autoortho.using_custom_tiles = False
        else:
            self.cfg.autoortho.using_custom_tiles = True

        self.refresh_settings_tab()

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
                log.info(f"No AutoOrtho overlay found in scenery_packs.ini - skipping AutoOrtho overlay modifications")
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

        
        self.dl.find_regions()
        for r in self.dl.regions.values():
            latest = r.get_latest_release()
            latest.parse()

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

    def start_update_check(self):
        """Start background update check against GitHub releases"""
        try:
            self._update_worker = UpdateCheckWorker()
            self._update_worker.result.connect(self.on_update_check_result)
            self._update_worker.error.connect(lambda e: None)
            self._update_worker.start()
        except Exception:
            pass

    def on_update_check_result(self, data):
        """Handle result from update check worker"""
        try:
            if not data:
                return
            latest_tag, html_url = data
            from version import __version__ as current_version
            latest_ver = self._parse_version(latest_tag)
            current_ver = self._parse_version(current_version)
            if latest_ver is None or current_ver is None:
                return
            if latest_ver > current_ver:
                reply = QMessageBox.question(
                    self,
                    "Update Available",
                    "An Update for AutoOrtho is Available. Do you want to go to the download page?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply == QMessageBox.StandardButton.Yes:
                    try:
                        webbrowser.open(html_url or "https://github.com/ProgrammingDinosaur/autoortho4xplane/releases")
                    except Exception:
                        pass
        except Exception:
            pass

    def update_logs(self):
        """Update log display"""
        try:
            if os.path.exists(self.cfg.paths.log_file):
                with open(self.cfg.paths.log_file) as h:
                    lines = h.readlines()[-50:]  # Last 50 lines
                    self.log_text.setPlainText(''.join(lines))
                    # Auto scroll to bottom
                    scrollbar = self.log_text.verticalScrollBar()
                    scrollbar.setValue(scrollbar.maximum())
        except Exception:
            pass

    def update_status_bar(self, message):
        """Update status bar message"""
        self.status_bar.showMessage(message)
        log.info(message)

    def append_log(self, message):
        """Append message to log display"""
        self.log_text.append(message)

    def display_error(self, message):
        """Display error message dialog"""
        QMessageBox.critical(self, "Error", message)

    def verify(self):
        """Verify configuration"""
        self._check_xplane_dir(self.cfg.paths.xplane_path)
        for scenery in self.cfg.scenery_mounts:
            self._check_ortho_dir(scenery.get('root'))

        if not self.cfg.scenery_mounts:
            self.errors.append("No installed scenery detected!")

        msg = []
        if self.warnings:
            msg.append("WARNINGS:")
            msg.extend(self.warnings)
            msg.append("\n")

        for warn in self.warnings:
            log.warning(warn)

        if self.errors:
            msg.append("ERRORS:")
            msg.extend(self.errors)
            msg.append("\nWILL EXIT DUE TO ERRORS")

        for err in self.errors:
            log.error(err)

        if msg:
            if self.cfg.general.gui:
                QMessageBox.warning(
                    self, "Configuration Issues", "\n".join(msg)
                )

        if self.errors:
            log.error("ERRORS DETECTED. Exiting.")
            sys.exit(1)

    def clean_cache(self, cache_dir, size_gb):
        """Clean cache directory"""
        self.status_update.emit(
            f"Cleaning up cache_dir {cache_dir}. Please wait..."
        )

        target_bytes = pow(2, 30) * size_gb

        try:
            if size_gb == 0:
                for entry in os.scandir(cache_dir):
                    if entry.is_file():
                        os.remove(entry.path)
            else:
                cfiles = sorted(
                    pathlib.Path(cache_dir).glob('**/*'), key=os.path.getmtime
                )
                if not cfiles:
                    self.status_update.emit("Cache is empty.")
                    return

                cache_bytes = sum(
                    file.stat().st_size for file in cfiles if file.is_file()
                )
                cachecount = len(cfiles)
                avgcachesize = cache_bytes / cachecount if cachecount > 0 else 0

                self.status_update.emit(
                    f"Cache has {cachecount} files. "
                    f"Total size approx {cache_bytes//1048576} MB."
                )

                empty_files = [
                    x for x in cfiles if x.is_file() and x.stat().st_size == 0
                ]
                self.status_update.emit(
                    f"Found {len(empty_files)} empty files to cleanup."
                )
                for file in empty_files:
                    if os.path.exists(file):
                        os.remove(file)

                if target_bytes > cache_bytes:
                    self.status_update.emit("Cache within size limits.")
                    return

                to_delete = int((cache_bytes - target_bytes) // avgcachesize)

                self.status_update.emit(
                    f"Over cache size limit, will remove {to_delete} files."
                )
                for file in cfiles[:to_delete]:
                    if file.is_file():
                        os.remove(file)

            self.status_update.emit("Cache cleanup done.")
        except Exception as e:
            self.status_update.emit(f"Cache cleanup error: {str(e)}")

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
        self.running = False

        # If we're in the second pass (ready to close), just accept and exit
        if self._ready_to_close:
            event.accept()
            return

        # Stop periodic UI updates early
        if hasattr(self, 'log_timer'):
            try:
                self.log_timer.stop()
            except Exception:
                pass

        # Stop all background workers immediately
        try:
            for worker in self.download_workers.values():
                worker.terminate()
                worker.wait()
        except Exception:
            pass
        try:
            for worker in self.uninstall_workers.values():
                worker.terminate()
                worker.wait()
        except Exception:
            pass
        self.uninstall_workers.clear()

        # Clean up background mount processes
        if hasattr(self, 'unmount_sceneries'):
            try:
                self.unmount_sceneries()
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

    def unmount_sceneries(self):
        """Unmount sceneries (stub - implemented in parent)"""
        pass

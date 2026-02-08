#!/usr/bin/env python

from ast import If
import os
import sys
import shutil
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

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    import autoortho.utils.resources_rc
except ImportError:
    import utils.resources_rc

try:
    from autoortho.utils.constants import MAPTYPES, system_type
except ImportError:
    from utils.constants import MAPTYPES, system_type

try:
    from autoortho.utils.mappers import map_kubilus_region_to_simheaven_region
except ImportError:
    from utils.mappers import map_kubilus_region_to_simheaven_region

try:
    from autoortho.utils.dsf_utils import DsfUtils, dsf_utils
except ImportError:
    from utils.dsf_utils import DsfUtils, dsf_utils

try:
    from autoortho.utils.mount_utils import cleanup_mountpoint, safe_ismount
except ImportError:
    from utils.mount_utils import cleanup_mountpoint, safe_ismount

try:
    from autoortho.utils.dynamic_zoom import DynamicZoomManager, BASE_ALTITUDE_FT
except ImportError:
    from utils.dynamic_zoom import DynamicZoomManager, BASE_ALTITUDE_FT

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel, QLineEdit, QCheckBox, QComboBox,
    QSlider, QTextEdit, QFileDialog, QMessageBox, QScrollArea,
    QSplashScreen, QGroupBox, QProgressBar, QStatusBar, QFrame, QSpinBox,
    QColorDialog, QRadioButton, QMenu, QStyle,
    QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QInputDialog,
    QHeaderView
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSize, QPoint, QObject, QEvent
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QWheelEvent, QCursor
)

try:
    from autoortho import downloader
except ImportError:
    import downloader

try:
    from autoortho.version import __version__
except ImportError:
    from version import __version__

log = logging.getLogger(__name__)

CUR_PATH = os.path.dirname(os.path.realpath(__file__))


class QTextEditLogger(logging.Handler):
    """Custom logging handler that writes to a QTextEdit widget"""
    
    # Create a signal class for thread-safe communication
    class _SignalEmitter(QObject):
        log_signal = Signal(str)
    
    def __init__(self, text_edit):
        super().__init__()
        self.text_edit = text_edit
        self.max_lines = 1000  # Keep last 1000 lines in UI
        
        # Create signal emitter
        self._emitter = self._SignalEmitter()
        self._emitter.log_signal.connect(self._append_text)
        
    def emit(self, record):
        try:
            msg = self.format(record)
            # Emit signal to ensure thread-safe UI updates
            # The signal-slot mechanism handles cross-thread communication properly
            self._emitter.log_signal.emit(msg)
        except Exception as e:
            # Fail silently in production, but useful for debugging
            import sys
            print(f"QTextEditLogger emit error: {e}", file=sys.stderr)
    
    def _append_text(self, msg):
        """Append text to the widget (called from main thread via signal)"""
        try:
            self.text_edit.append(msg)
            self._trim_text()
            # Auto-scroll to bottom
            scrollbar = self.text_edit.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except Exception as e:
            import sys
            print(f"QTextEditLogger append error: {e}", file=sys.stderr)
    
    def _trim_text(self):
        """Keep only the last max_lines in the text edit"""
        try:
            doc = self.text_edit.document()
            if doc.blockCount() > self.max_lines:
                cursor = self.text_edit.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                cursor.movePosition(
                    cursor.MoveOperation.Down,
                    cursor.MoveMode.KeepAnchor,
                    doc.blockCount() - self.max_lines
                )
                cursor.removeSelectedText()
        except Exception as e:
            import sys
            print(f"QTextEditLogger trim error: {e}", file=sys.stderr)


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


class SimBriefFetchWorker(QThread):
    """Worker thread for fetching SimBrief flight plan data"""
    success = Signal(dict)  # flight data
    error = Signal(str)  # error message
    
    SIMBRIEF_API_URL = "https://www.simbrief.com/api/xml.fetcher.php"
    
    def __init__(self, userid):
        super().__init__()
        self.userid = userid
    
    def run(self):
        try:
            url = f"{self.SIMBRIEF_API_URL}?userid={self.userid}&json=1"
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # Check for API-level errors
            fetch_info = data.get('fetch', {})
            status = fetch_info.get('status', '')
            if status.lower().startswith('error'):
                self.error.emit(status)
                return
            
            self.success.emit(data)
            
        except requests.exceptions.Timeout:
            self.error.emit("Request timed out. Please check your internet connection.")
        except requests.exceptions.ConnectionError:
            self.error.emit("Connection error. Please check your internet connection.")
        except requests.exceptions.HTTPError as e:
            self.error.emit(f"HTTP error: {e.response.status_code}")
        except ValueError as e:
            self.error.emit(f"Invalid response from SimBrief: {str(e)}")
        except Exception as e:
            self.error.emit(f"Unexpected error: {str(e)}")


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
    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class QualityStepsDialog(QDialog):
    """
    Dialog for managing dynamic zoom quality steps.
    
    Allows users to define altitude-based zoom level thresholds.
    Each step specifies a max zoom level for altitudes at or above
    a threshold. The base step (0 ft AGL) cannot be removed.
    """
    
    def __init__(self, parent=None, manager=None, current_max_zoom=16):
        """
        Initialize the quality steps dialog.
        
        Args:
            parent: Parent widget
            manager: DynamicZoomManager instance (creates new if None)
            current_max_zoom: Current fixed max zoom (for initializing base step)
        """
        super().__init__(parent)
        self.manager = manager if manager is not None else DynamicZoomManager()
        self.current_max_zoom = current_max_zoom
        self.setWindowTitle("Dynamic Zoom - Quality Steps")
        self.setMinimumSize(550, 450)
        self._setup_ui()
        
    def _setup_ui(self):
        """Set up the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        
        # Info label
        info = QLabel(
            "Define maximum zoom levels for different altitude ranges.\n"
            "Cruising at higher altitudes doesn't need as much detail - saves VRAM and speeds up loading.\n"
            "'Near Airports' uses higher zoom when approaching airports.\n"
            "The base level (0 ft AGL) is the default for ground level and cannot be removed."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; padding: 10px; background: #333; border-radius: 5px;")
        layout.addWidget(info)
        
        # Steps table
        self.steps_table = QTableWidget()
        self.steps_table.setColumnCount(4)
        self.steps_table.setHorizontalHeaderLabels([
            "Altitude (ft)", "Max Zoom Level", "Near Airports", "Actions"
        ])
        self.steps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.steps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.steps_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.steps_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.steps_table.setColumnWidth(3, 100)
        self.steps_table.verticalHeader().setDefaultSectionSize(36)  # Ensure enough row height
        self.steps_table.setStyleSheet("""
            QTableWidget {
                background-color: #2d2d2d;
                border: 1px solid #444;
                border-radius: 5px;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QHeaderView::section {
                background-color: #3d3d3d;
                padding: 8px;
                border: none;
                border-bottom: 1px solid #555;
            }
            QSpinBox {
                background-color: #3d3d3d;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 4px;
                color: white;
                min-height: 24px;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                width: 16px;
            }
        """)
        layout.addWidget(self.steps_table)
        
        # Add step button
        add_btn = StyledButton("Add Quality Step")
        add_btn.clicked.connect(self._add_step)
        layout.addWidget(add_btn)
        
        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | 
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
        self._refresh_table()
    
    def _refresh_table(self):
        """Refresh the steps table from manager."""
        steps = self.manager.get_steps()
        self.steps_table.setRowCount(len(steps))
        
        for i, step in enumerate(steps):
            # Altitude (editable except for base)
            alt_item = QTableWidgetItem(str(step.altitude_ft))
            if step.altitude_ft == BASE_ALTITUDE_FT:
                alt_item.setFlags(alt_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                alt_item.setToolTip("Base altitude cannot be changed")
                alt_item.setBackground(QColor(60, 60, 60))
            self.steps_table.setItem(i, 0, alt_item)
            
            # Zoom level - use QSpinBox for proper validation (12-17 range)
            zoom_spinbox = QSpinBox()
            zoom_spinbox.setMinimum(12)
            zoom_spinbox.setMaximum(17)
            zoom_spinbox.setValue(min(17, max(12, step.zoom_level)))  # Clamp to valid range
            zoom_spinbox.setAlignment(Qt.AlignmentFlag.AlignCenter)
            zoom_spinbox.setProperty("altitude_ft", step.altitude_ft)
            zoom_spinbox.setProperty("row", i)
            self.steps_table.setCellWidget(i, 1, zoom_spinbox)
            
            # Airport zoom level - use QSpinBox for proper validation (12-18 range)
            airport_zoom_spinbox = QSpinBox()
            airport_zoom_spinbox.setMinimum(12)
            airport_zoom_spinbox.setMaximum(18)
            airport_zoom_spinbox.setValue(min(18, max(12, step.zoom_level_airports)))
            airport_zoom_spinbox.setAlignment(Qt.AlignmentFlag.AlignCenter)
            airport_zoom_spinbox.setProperty("altitude_ft", step.altitude_ft)
            airport_zoom_spinbox.setProperty("row", i)
            self.steps_table.setCellWidget(i, 2, airport_zoom_spinbox)
            
            # Connect validation: airport zoom must be >= regular zoom
            zoom_spinbox.valueChanged.connect(
                lambda val, row=i: self._validate_zoom_pair(row, "zoom")
            )
            airport_zoom_spinbox.valueChanged.connect(
                lambda val, row=i: self._validate_zoom_pair(row, "airport")
            )
            
            # Delete button (not for base)
            if step.altitude_ft != BASE_ALTITUDE_FT:
                del_btn = QPushButton("Remove")
                del_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #8b3030;
                        color: white;
                        border: none;
                        padding: 3px 8px;
                        border-radius: 3px;
                    }
                    QPushButton:hover {
                        background-color: #a04040;
                    }
                """)
                # Capture altitude value in lambda closure
                del_btn.clicked.connect(
                    lambda checked, alt=step.altitude_ft: self._remove_step(alt)
                )
                self.steps_table.setCellWidget(i, 3, del_btn)
            else:
                # Empty cell for base step
                self.steps_table.setCellWidget(i, 3, QLabel(""))
    
    def _add_step(self):
        """Add a new quality step."""
        # Show dialog to enter altitude
        altitude, ok = QInputDialog.getInt(
            self, "Add Quality Step", 
            "Altitude (ft) - step applies at and above this altitude:",
            10000, 0, 60000, 1000
        )
        if not ok:
            return
            
        # Show dialog to enter zoom level
        zoom, ok = QInputDialog.getInt(
            self, "Add Quality Step", 
            "Maximum Zoom Level (12-17):",
            15, 12, 17, 1
        )
        if not ok:
            return
        
        # Show dialog to enter airport zoom level
        airport_zoom, ok = QInputDialog.getInt(
            self, "Add Quality Step", 
            "Maximum Zoom Level Near Airports (12-18):",
            min(18, zoom + 1), 12, 18, 1  # Default to one level higher than normal
        )
        if not ok:
            return
        
        # Validate: airport zoom must be >= regular zoom
        if airport_zoom < zoom:
            QMessageBox.warning(
                self, "Invalid Zoom Settings",
                f"Airport zoom level ({airport_zoom}) cannot be lower than regular zoom level ({zoom}).\n"
                "Setting airport zoom to match regular zoom."
            )
            airport_zoom = zoom
            
        if not self.manager.add_step(altitude, zoom, airport_zoom):
            QMessageBox.warning(
                self, "Duplicate Altitude", 
                f"A quality step at {altitude} ft already exists.\n"
                "Please choose a different altitude or edit the existing step."
            )
            return
            
        self._refresh_table()
    
    def _remove_step(self, altitude: int):
        """Remove a quality step."""
        self.manager.remove_step(altitude)
        self._refresh_table()
    
    def _validate_zoom_pair(self, row: int, changed: str):
        """
        Validate that airport zoom >= regular zoom for a row.
        
        Args:
            row: The table row to validate
            changed: Which spinbox changed ("zoom" or "airport")
        """
        zoom_spinbox = self.steps_table.cellWidget(row, 1)
        airport_spinbox = self.steps_table.cellWidget(row, 2)
        
        if not zoom_spinbox or not airport_spinbox:
            return
        
        zoom_val = zoom_spinbox.value()
        airport_val = airport_spinbox.value()
        
        if airport_val < zoom_val:
            # Block signals to prevent recursion
            if changed == "zoom":
                # User increased zoom, bump airport to match
                airport_spinbox.blockSignals(True)
                airport_spinbox.setValue(zoom_val)
                airport_spinbox.blockSignals(False)
            else:
                # User decreased airport zoom below zoom, bump it back up
                airport_spinbox.blockSignals(True)
                airport_spinbox.setValue(zoom_val)
                airport_spinbox.blockSignals(False)
    
    def _on_accept(self):
        """Handle dialog acceptance - apply table edits to manager."""
        # Apply any table edits to manager
        for i in range(self.steps_table.rowCount()):
            alt_item = self.steps_table.item(i, 0)
            zoom_spinbox = self.steps_table.cellWidget(i, 1)
            airport_zoom_spinbox = self.steps_table.cellWidget(i, 2)
            if alt_item and zoom_spinbox and isinstance(zoom_spinbox, QSpinBox):
                try:
                    alt = int(alt_item.text())
                    zoom = zoom_spinbox.value()  # Already validated by spinbox
                    airport_zoom = None
                    if airport_zoom_spinbox and isinstance(airport_zoom_spinbox, QSpinBox):
                        airport_zoom = airport_zoom_spinbox.value()
                    self.manager.update_step(alt, zoom, airport_zoom)
                except ValueError:
                    pass
        self.accept()
    
    def get_manager(self) -> DynamicZoomManager:
        """
        Get the configured manager after dialog closes.
        
        Returns:
            The DynamicZoomManager with any changes applied
        """
        return self.manager


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

        # Set up logging handler for UI (must be None before init_ui is called)
        self.ui_log_handler = None

        # Setup UI
        self.init_ui()

        # Connect signals
        self.status_update.connect(self.update_status_bar)
        self.log_update.connect(self.append_log)
        self.show_error.connect(self.display_error)

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
        self.maptype_combo.installEventFilter(self)
        self.maptype_combo.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel
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

        # SimBrief Integration group
        simbrief_group = QGroupBox("SimBrief Integration")
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
        
        # Set up the UI logging handler now that log_text exists
        self.setup_ui_logging()

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
            
            # Add welcome message directly to the log text
            self.log_text.append("=== AutoOrtho Logs ===")
            self.log_text.append(f"UI Log Level: {console_level_str}")
            self.log_text.append(f"File Log Level: {getattr(self.cfg.general, 'file_log_level', 'DEBUG').upper()}")
            self.log_text.append(f"Log file location: {self.cfg.paths.log_file}")
            self.log_text.append("")
            
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
            "Delete only legacy JPEG files from the cache.\n"
            "Bundle files (.aob2) are not affected.\n"
            "Use this to free up space from old cache format."
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
        max_zoom_mode_label = QLabel("Max Zoom Mode:")
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
            "Recommended: 32 (balanced), 64-128 (fast internet), 16 (slow internet)\n"
            "Values above 128 are for very fast connections only."
        )
        max_chunks_layout.addWidget(self.prefetch_max_chunks_label)
        
        self.prefetch_max_chunks_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.prefetch_max_chunks_slider.setRange(8, 512)
        self.prefetch_max_chunks_slider.setValue(
            int(getattr(self.cfg.autoortho, 'prefetch_max_chunks', 32))
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
        
        # Note: DDS cache is disk-only (ephemeral_dds_cache_mb in advanced settings)
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
            "  • 1 - Low-end CPU or battery saving\n"
            "  • 2 - Balanced (default)\n"
            "  • 4-8 - Fast CPU, maximize prefetch speed"
        )
        prefetch_layout.addWidget(self.prefetch_workers_label)
        
        self.background_workers_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.background_workers_slider.setRange(1, 8)
        self.background_workers_slider.setSingleStep(1)
        self.background_workers_slider.setPageStep(1)
        self.background_workers_slider.setTickInterval(1)
        self.background_workers_slider.setValue(
            int(getattr(self.cfg.autoortho, 'background_builder_workers', 2))
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
        self.buffer_pool_label = QLabel("Buffer pool size:")
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
        prefetch = int(getattr(self.cfg.autoortho, 'background_builder_workers', 2))
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
        
        # --- Min Chunk Ratio (quality vs speed tradeoff) ---
        min_chunk_layout = QHBoxLayout()
        self.min_chunk_ratio_label = QLabel("Min chunk ratio:")
        self.min_chunk_ratio_label.setToolTip(
            "Minimum ratio of chunks that must be available before\n"
            "falling back to lower zoom levels or missing color.\n\n"
            "Lower values = faster (allows more missing chunks)\n"
            "Higher values = better quality (waits for more data)\n\n"
            "• 80% - Fast: Allows up to 20% missing chunks\n"
            "• 90% - Balanced (default): Up to 10% missing\n"
            "• 95% - Quality: Only 5% missing allowed\n\n"
            "Missing chunks are filled with fallback images\n"
            "or the missing color if no fallback is available."
        )
        min_chunk_layout.addWidget(self.min_chunk_ratio_label)
        
        self.min_chunk_ratio_slider = ModernSlider(Qt.Orientation.Horizontal)
        self.min_chunk_ratio_slider.setRange(50, 100)  # 50% to 100%
        current_ratio = float(getattr(self.cfg.autoortho, 'live_aopipeline_min_chunk_ratio', 0.9))
        current_ratio_pct = int(current_ratio * 100)
        current_ratio_pct = max(50, min(100, current_ratio_pct))
        self.min_chunk_ratio_slider.setValue(current_ratio_pct)
        self.min_chunk_ratio_slider.setObjectName('live_aopipeline_min_chunk_ratio')
        self.min_chunk_ratio_slider.setToolTip("Minimum chunk availability ratio (50-100%)")
        
        self.min_chunk_ratio_value_label = QLabel(f"{current_ratio_pct}%")
        self.min_chunk_ratio_slider.valueChanged.connect(
            lambda v: self.min_chunk_ratio_value_label.setText(f"{v}%")
        )
        min_chunk_layout.addWidget(self.min_chunk_ratio_slider)
        min_chunk_layout.addWidget(self.min_chunk_ratio_value_label)
        autoortho_layout.addLayout(min_chunk_layout)
        
        # Initialize pipeline control states
        self._update_pipeline_controls()
        
        autoortho_layout.addSpacing(10)
        # ═══════════════════════════════════════════════════════════════════
        
        # Initialize prefetch control states
        self._update_prefetch_controls()

        # Initialize time budget control states
        self._update_time_budget_controls()

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
        self.fetch_threads_spinbox.setFocusPolicy(Qt.StrongFocus) # Prevent focus by hovering mouse wheel

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
        # TEMP pyside6 / Python 3.14 QRadioButton workaround
        # self.seasons_enabled_radio = QRadioButton("Enabled")
        # self.seasons_disabled_radio = QRadioButton("Disabled")
        self.seasons_enabled_radio = QCheckBox("Enabled")
        self.seasons_disabled_radio = QCheckBox("Disabled")

        seasons_enabled = bool(self.cfg.seasons.enabled)
        self.seasons_enabled_radio.setChecked(seasons_enabled)
        self.seasons_disabled_radio.setChecked(not seasons_enabled)
        # TEMP pyside6 / Python 3.14 QRadioButton workaround
        # self.seasons_enabled_radio.toggled.connect(self.on_seasons_enabled_toggled)
        # self.seasons_disabled_radio.toggled.connect(self.on_seasons_enabled_toggled)
        self.seasons_enabled_radio.clicked.connect(self.on_seasons_enabled_checked)
        self.seasons_disabled_radio.clicked.connect(self.on_seasons_disabled_checked)

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

        # Time Exclusion Settings group
        time_exclusion_group = QGroupBox("Time Exclusion Settings")
        time_exclusion_layout = QVBoxLayout()
        time_exclusion_group.setLayout(time_exclusion_layout)

        # Info label
        time_exclusion_info = QLabel(
            "Configure a time range during which AutoOrtho scenery will be disabled.\n"
            "X-Plane will use default scenery during this time (e.g., for night flying)."
        )
        time_exclusion_info.setStyleSheet("color: #888; font-size: 11px;")
        time_exclusion_info.setWordWrap(True)
        time_exclusion_layout.addWidget(time_exclusion_info)
        
        time_exclusion_layout.addSpacing(5)

        # Enable checkbox
        self.time_exclusion_enabled_check = QCheckBox("Enable time-based exclusion")
        time_exclusion_enabled = getattr(self.cfg.time_exclusion, 'enabled', False)
        self.time_exclusion_enabled_check.setChecked(time_exclusion_enabled)
        self.time_exclusion_enabled_check.setObjectName('time_exclusion_enabled')
        self.time_exclusion_enabled_check.setToolTip(
            "When enabled, AutoOrtho scenery will be hidden from X-Plane\n"
            "during the specified time range (based on simulator local time).\n"
            "X-Plane will fall back to default scenery during this period.\n"
            "Useful for night flying when satellite imagery is less useful."
        )
        self.time_exclusion_enabled_check.toggled.connect(self._on_time_exclusion_toggled)
        time_exclusion_layout.addWidget(self.time_exclusion_enabled_check)

        # Time range inputs
        time_range_layout = QHBoxLayout()
        
        # Start time
        start_time_label = QLabel("Start time:")
        start_time_label.setToolTip("Start of exclusion period (24-hour format HH:MM)")
        time_range_layout.addWidget(start_time_label)
        
        self.time_exclusion_start_edit = QLineEdit(
            str(getattr(self.cfg.time_exclusion, 'start_time', '22:00'))
        )
        self.time_exclusion_start_edit.setObjectName('time_exclusion_start_time')
        self.time_exclusion_start_edit.setMaximumWidth(80)
        self.time_exclusion_start_edit.setPlaceholderText("HH:MM")
        self.time_exclusion_start_edit.setToolTip(
            "Start time for exclusion in 24-hour format (e.g., 22:00 for 10 PM)"
        )
        time_range_layout.addWidget(self.time_exclusion_start_edit)
        
        time_range_layout.addSpacing(20)
        
        # End time
        end_time_label = QLabel("End time:")
        end_time_label.setToolTip("End of exclusion period (24-hour format HH:MM)")
        time_range_layout.addWidget(end_time_label)
        
        self.time_exclusion_end_edit = QLineEdit(
            str(getattr(self.cfg.time_exclusion, 'end_time', '06:00'))
        )
        self.time_exclusion_end_edit.setObjectName('time_exclusion_end_time')
        self.time_exclusion_end_edit.setMaximumWidth(80)
        self.time_exclusion_end_edit.setPlaceholderText("HH:MM")
        self.time_exclusion_end_edit.setToolTip(
            "End time for exclusion in 24-hour format (e.g., 06:00 for 6 AM)"
        )
        time_range_layout.addWidget(self.time_exclusion_end_edit)
        
        time_range_layout.addStretch()
        time_exclusion_layout.addLayout(time_range_layout)

        # Example label
        time_example_label = QLabel(
            "Example: 22:00 to 06:00 hides AutoOrtho during night hours"
        )
        time_example_label.setStyleSheet("color: #666; font-size: 10px; font-style: italic;")
        time_exclusion_layout.addWidget(time_example_label)

        time_exclusion_layout.addSpacing(5)

        # Default to exclusion checkbox
        self.time_exclusion_default_check = QCheckBox("Start Flight with AutoOrtho Exclusion")
        default_to_exclusion = getattr(self.cfg.time_exclusion, 'default_to_exclusion', False)
        self.time_exclusion_default_check.setChecked(default_to_exclusion)
        self.time_exclusion_default_check.setObjectName('time_exclusion_default')
        self.time_exclusion_default_check.setToolTip(
            "When enabled, AutoOrtho will start with exclusion active until X-Plane starts\n"
            "sending sim time data. This ensures night flights start with default scenery from the very beginning. Useful for night flights.\n\n"
            "When disabled, AutoOrtho will start with normal operation until X-Plane starts sending sim time data."
        )
        time_exclusion_layout.addWidget(self.time_exclusion_default_check)

        # Set initial enabled state for time inputs
        self._set_time_exclusion_controls_enabled(time_exclusion_enabled)

        self.settings_layout.addWidget(time_exclusion_group)

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

    # TEMP pyside6 / Python 3.14 QRadioButton workaround
    def on_seasons_enabled_checked(self):
        try:
            self.seasons_enabled_radio.setChecked(True)
            self.seasons_disabled_radio.setChecked(False)
            self._set_seasons_controls_enabled(True)
        except Exception:
            pass

    # TEMP pyside6 / Python 3.14 QRadioButton workaround
    def on_seasons_disabled_checked(self):
        try:
            self.seasons_enabled_radio.setChecked(False)
            self.seasons_disabled_radio.setChecked(True)
            self._set_seasons_controls_enabled(False)
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
        """Handle time exclusion checkbox toggle."""
        try:
            enabled = self.time_exclusion_enabled_check.isChecked()
            self._set_time_exclusion_controls_enabled(enabled)
        except Exception:
            pass

    def _set_time_exclusion_controls_enabled(self, enabled):
        """Enable/disable time exclusion time input fields."""
        try:
            if hasattr(self, 'time_exclusion_start_edit'):
                self.time_exclusion_start_edit.setEnabled(enabled)
            if hasattr(self, 'time_exclusion_end_edit'):
                self.time_exclusion_end_edit.setEnabled(enabled)
            if hasattr(self, 'time_exclusion_default_check'):
                self.time_exclusion_default_check.setEnabled(enabled)
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
                        """
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
        
        # Min chunk ratio is also only relevant for native/hybrid modes
        if hasattr(self, 'min_chunk_ratio_slider'):
            self.min_chunk_ratio_slider.setEnabled(buffer_pool_enabled)
            self.min_chunk_ratio_label.setEnabled(buffer_pool_enabled)
            self.min_chunk_ratio_value_label.setEnabled(buffer_pool_enabled)
            self.min_chunk_ratio_label.setStyleSheet(enabled_style if buffer_pool_enabled else disabled_style)
            self.min_chunk_ratio_value_label.setStyleSheet(enabled_style if buffer_pool_enabled else disabled_style)
        
        # Update tooltip to explain why disabled
        if not buffer_pool_enabled:
            self.buffer_pool_label.setToolTip(
                "Buffer pool is only used in Native and Hybrid modes.\n"
                "Select a different pipeline mode to configure this setting."
            )
            if hasattr(self, 'min_chunk_ratio_label'):
                self.min_chunk_ratio_label.setToolTip(
                    "Min chunk ratio is only used in Native and Hybrid modes.\n"
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
            self._update_dynamic_summary()
            self._update_buffer_pool_label()

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
            from utils.simbrief_flight import simbrief_flight_manager
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
        self.simbrief_fetch_worker.success.connect(self._on_simbrief_fetch_success)
        self.simbrief_fetch_worker.error.connect(self._on_simbrief_fetch_error)
        self.simbrief_fetch_worker.finished.connect(self._on_simbrief_fetch_finished)
        self.simbrief_fetch_worker.start()

    def _on_simbrief_fetch_success(self, data):
        """Handle successful SimBrief fetch"""
        self.simbrief_flight_data = data
        self._display_simbrief_flight_info(data)
        self.simbrief_info_frame.show()
        self.simbrief_error_label.hide()
        self.simbrief_use_flight_data_check.show()  # Show toggle when flight data loaded
        self.simbrief_unload_btn.show()  # Show unload button when flight is loaded
        
        # Load flight data into the global flight manager for use by dynamic zoom and prefetcher
        from utils.simbrief_flight import simbrief_flight_manager
        if simbrief_flight_manager.load_flight_data(data):
            log.info(f"SimBrief flight loaded into manager: {simbrief_flight_manager.origin} -> {simbrief_flight_manager.destination}")
        else:
            log.warning("Failed to load SimBrief flight data into manager")
        
        log.info("SimBrief flight data fetched successfully")

    def _on_simbrief_fetch_error(self, error_msg):
        """Handle SimBrief fetch error"""
        self.simbrief_error_label.setText(f"⚠ {error_msg}")
        self.simbrief_error_label.show()
        self.simbrief_info_frame.show()
        self.simbrief_route_label.setText("")
        self.simbrief_details_label.setText("")
        self.simbrief_use_flight_data_check.hide()  # Hide toggle on error
        self.simbrief_route_settings_frame.hide()  # Hide route settings on error
        self.simbrief_unload_btn.hide()  # Hide unload button on error
        self.simbrief_flight_data = None
        
        # Clear the flight manager
        from utils.simbrief_flight import simbrief_flight_manager
        simbrief_flight_manager.clear()
        
        log.warning(f"SimBrief fetch error: {error_msg}")

    def _on_simbrief_fetch_finished(self):
        """Handle SimBrief fetch completion (success or error)"""
        self.simbrief_fetch_btn.setEnabled(True)
        self.simbrief_fetch_btn.setText("Fetch Flight Data")

    def _on_simbrief_unload(self):
        """Handle SimBrief unload button click - clears flight data"""
        # Clear stored flight data
        self.simbrief_flight_data = None
        
        # Clear the global flight manager
        from utils.simbrief_flight import simbrief_flight_manager
        simbrief_flight_manager.clear()
        
        # Hide flight info UI
        self.simbrief_info_frame.hide()
        self.simbrief_use_flight_data_check.hide()
        self.simbrief_route_settings_frame.hide()
        self.simbrief_unload_btn.hide()
        self.simbrief_error_label.hide()
        
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
        # Note: cfg.load() removed - save_config() already saves to disk and updates cfg object
        # The redundant load() was creating a race condition window where defaults could be exposed
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
        # Minimize window if hide setting is enabled
        if self.cfg.general.hide:
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
        # Note: cfg.load() removed - save_config() already saves to disk and updates cfg object
        # The redundant load() was creating a race condition window where defaults could be exposed
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

    def on_delete_cache(self):
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

        # Run in separate thread
        self.cache_thread = QThread()
        self.cache_thread.run = lambda: self.clean_jpegs_only(self.cfg.paths.cache_dir)
        self.cache_thread.finished.connect(self.on_jpegs_cleaned)
        self.cache_thread.start()

    def on_jpegs_cleaned(self):
        """Called when JPEG cleaning is complete"""
        try:
            self._set_cache_buttons_enabled(True)
            QMessageBox.information(
                self, "JPEG Files Cleaned", "JPEG file cleaning completed!"
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

    def on_clean_cache(self, for_exit=False, delete_all=False):
        """Handle Clean Cache button click

        Args:
            for_exit (bool): When True, invoked from closeEvent - suppress dialogs
                             and allow closeEvent to wait on the thread.
            delete_all (bool) : When True, all files in the cache should be deleted.
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
        self._set_cache_buttons_enabled(False)

        # Run in separate thread and keep reference so we can wait on exit
        self.cache_thread = QThread()
        self.cache_thread.run = lambda: self.clean_cache(
            self.cfg.paths.cache_dir,
            int(self.file_cache_slider.value() if not delete_all else 0)
        )
        self.cache_thread.finished.connect(lambda: self.on_cache_cleaned(for_exit))
        self.cache_thread.start()

    def _set_cache_buttons_enabled(self, enabled):
        """Enable or disable all cache-related buttons"""
        for btn_name in ('clean_cache_btn', 'clean_jpegs_btn', 'delete_cache_btn', 'run_button'):
            if hasattr(self, btn_name):
                getattr(self, btn_name).setEnabled(enabled)

    def on_cache_cleaned(self, for_exit=False):
        """Called when cache cleaning is complete"""
        try:
            self._set_cache_buttons_enabled(True)
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
            self.cfg.autoortho.predictive_dds_enabled = self.predictive_dds_enabled_check.isChecked()
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
            self.cfg.autoortho.live_aopipeline_min_chunk_ratio = self.min_chunk_ratio_slider.value() / 100.0
            
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
            self.cfg.general.console_log_level = self.console_log_level_combo.currentText()
            self.cfg.general.file_log_level = self.file_log_level_combo.currentText()

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

            # Time exclusion settings
            if hasattr(self, 'time_exclusion_enabled_check'):
                self.cfg.time_exclusion.enabled = self.time_exclusion_enabled_check.isChecked()
            if hasattr(self, 'time_exclusion_start_edit'):
                self.cfg.time_exclusion.start_time = self.time_exclusion_start_edit.text()
            if hasattr(self, 'time_exclusion_end_edit'):
                self.cfg.time_exclusion.end_time = self.time_exclusion_end_edit.text()
            if hasattr(self, 'time_exclusion_default_check'):
                self.cfg.time_exclusion.default_to_exclusion = self.time_exclusion_default_check.isChecked()

        # SimBrief settings
        if hasattr(self.cfg, 'simbrief'):
            if hasattr(self, 'simbrief_userid_edit'):
                self.cfg.simbrief.userid = self.simbrief_userid_edit.text().strip()
            if hasattr(self, 'simbrief_use_flight_data_check'):
                self.cfg.simbrief.use_flight_data = self.simbrief_use_flight_data_check.isChecked()

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
                if safe_ismount(mount):
                    lingering.append(mount)
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
                if not any(safe_ismount(x) for x in lingering):
                    break
                _time.sleep(0.3)
            if any(safe_ismount(x) for x in lingering):
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
        if not state: 
            if self.cfg.autoortho.using_custom_tiles and int(self.cfg.autoortho.max_zoom) > 17:
                log.info("Max zoom being capped to 17 after custom tiles disabled")
                self.cfg.autoortho.max_zoom = 17
            self.cfg.autoortho.using_custom_tiles = False
        else:
            self.cfg.autoortho.using_custom_tiles = True

        self.refresh_settings_tab()
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
        """Clean cache directory
        
        Steps:
        1. Delete all JPEGs from cache folder (legacy files)
        2. Apply size-based cleanup to bundles in their new locations
        """
        self.status_update.emit(
            f"Cleaning up cache_dir {cache_dir}. Please wait..."
        )

        try:
            # Step 1: Always delete all legacy JPEGs from cache_dir root
            jpeg_count = 0
            for entry in os.scandir(cache_dir):
                if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg')):
                    os.remove(entry.path)
                    jpeg_count += 1
            if jpeg_count > 0:
                self.status_update.emit(f"Deleted {jpeg_count} legacy JPEG files.")

            # Step 2: Handle bundles
            bundles_dir = os.path.join(cache_dir, "bundles")

            if size_gb == 0:
                # Delete all bundles
                if os.path.isdir(bundles_dir):
                    shutil.rmtree(bundles_dir, ignore_errors=True)
                    self.status_update.emit("Deleted all bundle files.")
            else:
                # Size-based cleanup for bundles
                target_bytes = pow(2, 30) * size_gb

                # Find all bundle files
                bundle_files = sorted(
                    pathlib.Path(bundles_dir).glob('**/*.aob2'),
                    key=os.path.getmtime
                ) if os.path.isdir(bundles_dir) else []

                if not bundle_files:
                    self.status_update.emit("No bundles to clean.")
                    self.status_update.emit("Cache cleanup done.")
                    return

                cache_bytes = sum(f.stat().st_size for f in bundle_files)
                bundle_count = len(bundle_files)
                avg_size = cache_bytes / bundle_count if bundle_count > 0 else 0

                self.status_update.emit(
                    f"Cache has {bundle_count} bundles. "
                    f"Total size approx {cache_bytes // 1048576} MB."
                )

                # Remove empty bundles
                empty_bundles = [f for f in bundle_files if f.stat().st_size == 0]
                for f in empty_bundles:
                    if f.exists():
                        os.remove(f)
                if empty_bundles:
                    self.status_update.emit(f"Removed {len(empty_bundles)} empty bundles.")

                if target_bytes >= cache_bytes:
                    self.status_update.emit("Cache within size limits.")
                    self.status_update.emit("Cache cleanup done.")
                    return

                to_delete = int((cache_bytes - target_bytes) // avg_size)
                self.status_update.emit(
                    f"Over cache size limit, will remove {to_delete} bundles."
                )

                for f in bundle_files[:to_delete]:
                    if f.is_file():
                        os.remove(f)

            self.status_update.emit("Cache cleanup done.")
        except Exception as e:
            self.status_update.emit(f"Cache cleanup error: {str(e)}")

    def clean_jpegs_only(self, cache_dir):
        """Clean only JPEG files from cache directory, leaving bundles untouched"""
        self.status_update.emit(
            f"Cleaning JPEG files from {cache_dir}. Please wait..."
        )

        try:
            jpeg_count = 0
            for entry in os.scandir(cache_dir):
                if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg')):
                    os.remove(entry.path)
                    jpeg_count += 1
            
            if jpeg_count > 0:
                self.status_update.emit(f"Deleted {jpeg_count} JPEG files.")
            else:
                self.status_update.emit("No JPEG files found to delete.")
            
            self.status_update.emit("JPEG cleanup done.")
        except Exception as e:
            self.status_update.emit(f"JPEG cleanup error: {str(e)}")

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

        # Stop update check worker if running
        try:
            if hasattr(self, '_update_worker') and self._update_worker:
                self._update_worker.terminate()
                self._update_worker.wait(2000)  # 2 second timeout
                self._update_worker = None
        except Exception:
            pass

        # Stop SimBrief fetch worker if running
        try:
            if hasattr(self, 'simbrief_fetch_worker') and self.simbrief_fetch_worker:
                self.simbrief_fetch_worker.terminate()
                self.simbrief_fetch_worker.wait(2000)
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
                self.add_seasons_worker.terminate()
                self.add_seasons_worker.wait(2000)
                self.add_seasons_worker = None
        except Exception:
            pass

        # Stop any RestoreDefaultDsfs workers
        try:
            if hasattr(self, 'restore_dsfs_worker') and self.restore_dsfs_worker:
                self.restore_dsfs_worker.terminate()
                self.restore_dsfs_worker.wait(2000)
                self.restore_dsfs_worker = None
        except Exception:
            pass

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

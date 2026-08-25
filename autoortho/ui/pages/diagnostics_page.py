"""Combined health, reports, and searchable log diagnostics."""

import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QSplitter,
    QStyle,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.models.log_model import (
        LogFilterProxyModel,
        LogListModel,
    )
    from autoortho.ui.service_worker import ServiceWorker
    from autoortho.ui.services.diagnostics_service import DiagnosticsService
else:
    from ui.models.log_model import LogFilterProxyModel, LogListModel
    from ui.service_worker import ServiceWorker
    from ui.services.diagnostics_service import DiagnosticsService


class DiagnosticsPage(QWidget):
    settings_requested = Signal()

    def __init__(
        self,
        log_text,
        log_handler,
        *,
        log_path="",
        report_dir="",
        parent=None,
    ):
        super().__init__(parent)
        self.log_text = log_text
        self.log_handler = log_handler
        self.log_path = str(log_path)
        self.report_dir = Path(report_dir).expanduser()
        self.report_paths: list[Path] = []
        self.report_records = []
        self.diagnostics_service = DiagnosticsService(self.report_dir)
        self.report_list_worker = None
        self.report_read_worker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        title_row = QHBoxLayout()
        title = QLabel("Diagnostics")
        title.setProperty("textRole", "pageTitle")
        settings = QPushButton("Diagnostics &Settings…")
        settings.clicked.connect(self.settings_requested)
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(settings)
        root.addLayout(title_row)

        self.health_group = QGroupBox("System Health")
        health_layout = QVBoxLayout(self.health_group)
        self.health_label = QLabel("Health checks have not run yet.")
        self.health_label.setWordWrap(True)
        health_layout.addWidget(self.health_label)
        root.addWidget(self.health_group)

        splitter = QSplitter()
        logs = QWidget()
        logs_layout = QVBoxLayout(logs)
        logs_layout.setContentsMargins(0, 0, 0, 0)
        filter_controls = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search logs…")
        self.search_edit.setAccessibleName("Search diagnostic logs")
        self.level_combo = QComboBox()
        self.level_combo.addItems(
            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        )
        self.pause_check = QCheckBox("&Pause")
        clear_button = QPushButton("C&lear")
        copy_button = QPushButton("&Copy")
        export_button = QPushButton("&Export…")
        open_log_button = QPushButton("Open Log &File")
        open_log_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileIcon
            )
        )
        self.search_edit.setMinimumWidth(180)
        level_label = QLabel("Minimum &level")
        level_label.setBuddy(self.level_combo)
        filter_controls.addWidget(self.search_edit, 1)
        filter_controls.addWidget(level_label)
        filter_controls.addWidget(self.level_combo)
        filter_controls.addWidget(self.pause_check)
        log_actions = QHBoxLayout()
        log_actions.addWidget(clear_button)
        log_actions.addWidget(copy_button)
        log_actions.addWidget(export_button)
        log_actions.addWidget(open_log_button)
        log_actions.addStretch()
        logs_layout.addLayout(filter_controls)
        logs_layout.addLayout(log_actions)
        self.log_model = LogListModel(max_entries=1000, parent=self)
        self.log_proxy = LogFilterProxyModel(self)
        self.log_proxy.setSourceModel(self.log_model)
        self.log_view = QListView()
        self.log_view.setModel(self.log_proxy)
        self.log_view.setUniformItemSizes(True)
        self.log_view.setSelectionMode(
            self.log_view.SelectionMode.ExtendedSelection
        )
        self.log_view.setAccessibleName("Diagnostic log entries")
        logs_layout.addWidget(self.log_view, 1)
        self.log_text.hide()
        if hasattr(self.log_handler, "attach_model"):
            self.log_handler.attach_model(self.log_model)
        else:
            self.log_model.append_entries(
                getattr(self.log_handler, "entries", ())
            )
        self.log_model.rowsInserted.connect(self._logs_inserted)
        splitter.addWidget(logs)

        reports = QWidget()
        reports_layout = QVBoxLayout(reports)
        reports_layout.setContentsMargins(8, 0, 0, 0)
        reports_title = QLabel("Performance Reports")
        reports_title.setProperty("textRole", "sectionTitle")
        self.report_combo = QComboBox()
        self.report_combo.setAccessibleName("Performance report history")
        report_actions = QHBoxLayout()
        open_report = QPushButton("Open Report")
        open_folder = QPushButton("Open Folder")
        refresh_reports = QPushButton("Refresh")
        open_report.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileIcon
            )
        )
        open_folder.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_DirOpenIcon
            )
        )
        refresh_reports.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_BrowserReload
            )
        )
        report_actions.addWidget(open_report)
        report_actions.addWidget(open_folder)
        report_actions.addWidget(refresh_reports)
        self.report_preview = QTextBrowser()
        reports_layout.addWidget(reports_title)
        reports_layout.addWidget(self.report_combo)
        reports_layout.addLayout(report_actions)
        reports_layout.addWidget(self.report_preview, 1)
        splitter.addWidget(reports)
        splitter.setSizes([700, 350])
        root.addWidget(splitter, 1)

        self.search_edit.textChanged.connect(self._apply_log_filter)
        self.level_combo.currentTextChanged.connect(self._apply_log_filter)
        self.pause_check.toggled.connect(self._pause_changed)
        clear_button.clicked.connect(self._clear_logs)
        copy_button.clicked.connect(self._copy_logs)
        export_button.clicked.connect(self._export_logs)
        open_log_button.clicked.connect(self.open_log_file)
        self.report_combo.currentIndexChanged.connect(
            self._show_selected_report
        )
        open_report.clicked.connect(self.open_selected_report)
        open_folder.clicked.connect(self.open_report_folder)
        refresh_reports.clicked.connect(self.refresh_reports)
        self.refresh_reports()

    def _apply_log_filter(self):
        level = getattr(logging, self.level_combo.currentText(), logging.DEBUG)
        self.log_proxy.set_minimum_level(level)
        self.log_proxy.set_search_text(self.search_edit.text())
        if hasattr(self.log_handler, "set_filter"):
            self.log_handler.set_filter(level, self.search_edit.text())

    def _pause_changed(self, paused):
        self.log_handler.set_paused(paused)
        if not paused:
            self.log_view.scrollToBottom()

    def _logs_inserted(self, parent, first, last):
        if not self.pause_check.isChecked():
            self.log_view.scrollToBottom()

    def _clear_logs(self):
        self.log_handler.entries.clear()
        self.log_model.clear()

    def _copy_logs(self):
        QApplication.clipboard().setText(self._visible_log_text())

    def _export_logs(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export AutoOrtho logs",
            "autoortho-ui-log.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if path:
            Path(path).write_text(
                self._visible_log_text(),
                encoding="utf-8",
            )

    def _visible_log_text(self):
        return "\n".join(
            str(
                self.log_proxy.data(
                    self.log_proxy.index(row, 0),
                    Qt.ItemDataRole.DisplayRole,
                )
                or ""
            )
            for row in range(self.log_proxy.rowCount())
        )

    def set_health(self, checks, xplane_connected=False, runtime_state=""):
        lines = [
            f"X-Plane: {'Connected' if xplane_connected else 'Disconnected'}",
            f"Streaming: {runtime_state or 'Unknown'}",
        ]
        for check in checks or ():
            marker = {
                "success": "✓",
                "warning": "!",
                "error": "×",
                "pending": "○",
            }.get(check.status.value, "•")
            lines.append(f"{marker} {check.title}: {check.message}")
        self.health_label.setText("\n".join(lines))

    def refresh_reports(self):
        if (
            self.report_list_worker is not None
            and self.report_list_worker.isRunning()
        ):
            return
        selected = self.report_combo.currentText()
        worker = ServiceWorker(
            lambda cancel_event: self.diagnostics_service.list_reports(
                cancel_event=cancel_event,
            ),
            self,
        )
        self.report_list_worker = worker
        worker.completed.connect(
            lambda result, current=selected: self._reports_loaded(
                result,
                current,
            )
        )
        worker.finished.connect(
            lambda current=worker: self._worker_finished(
                "report_list_worker",
                current,
            )
        )
        worker.start()

    def _reports_loaded(self, result, selected):
        if isinstance(result, Exception) or not result.success:
            return
        reports = list(result.value)
        self.report_records = reports
        self.report_paths = [Path(report.path) for report in reports]
        self.report_combo.blockSignals(True)
        self.report_combo.clear()
        self.report_combo.addItems([report.name for report in reports])
        if selected:
            self.report_combo.setCurrentText(selected)
        self.report_combo.blockSignals(False)
        self._show_selected_report()

    def _selected_report(self):
        index = self.report_combo.currentIndex()
        if 0 <= index < len(self.report_paths):
            return self.report_paths[index]
        return None

    def _show_selected_report(self):
        report = self._selected_report()
        if report is None:
            self.report_preview.setPlainText(
                "No performance reports are available yet."
            )
            return
        if (
            self.report_read_worker is not None
            and self.report_read_worker.isRunning()
        ):
            self.report_read_worker.cancel()
        self.report_preview.setPlainText("Loading report…")
        index = self.report_combo.currentIndex()
        report_record = (
            self.report_records[index]
            if 0 <= index < len(self.report_records)
            else None
        )
        if report_record is None:
            self.report_preview.setPlainText(
                "Could not locate the selected report."
            )
            return
        worker = ServiceWorker(
            lambda cancel_event: self.diagnostics_service.read_report(
                report_record,
                cancel_event=cancel_event,
            ),
            self,
        )
        self.report_read_worker = worker
        worker.completed.connect(
            lambda result, path=str(report): self._report_loaded(
                result,
                path,
            )
        )
        worker.finished.connect(
            lambda current=worker: self._worker_finished(
                "report_read_worker",
                current,
            )
        )
        worker.start()

    def _report_loaded(self, result, path):
        selected = self._selected_report()
        if selected is None or str(selected) != path:
            return
        if isinstance(result, Exception) or not result.success:
            message = (
                str(result)
                if isinstance(result, Exception)
                else result.error.message
            )
            self.report_preview.setPlainText(
                f"Could not read report: {message}"
            )
            return
        self.report_preview.setMarkdown(result.value.markdown)

    def _worker_finished(self, attribute, worker):
        if getattr(self, attribute) is worker:
            setattr(self, attribute, None)

    def shutdown(self):
        for worker in (self.report_list_worker, self.report_read_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(1000)

    def open_selected_report(self):
        report = self._selected_report()
        if report is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(report)))

    def open_report_folder(self):
        self.report_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.report_dir))
        )

    def open_log_file(self):
        if self.log_path:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(self.log_path)
            )

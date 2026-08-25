"""Qt setup wizard for phase 2 readiness and scenery selection."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.readiness import (
        SceneryChoice,
        SetupReadiness,
        build_readiness,
        detect_xplane_installation,
        format_bytes,
        free_space_bytes,
        recursive_directory_usage_bytes,
    )
else:
    from ui.readiness import (
        SceneryChoice,
        SetupReadiness,
        build_readiness,
        detect_xplane_installation,
        format_bytes,
        free_space_bytes,
        recursive_directory_usage_bytes,
    )

from PySide6.QtCore import QSignalBlocker, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QFileDialog,
    QTextEdit,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)


class _DirectoryUsageWorker(QThread):
    completed = Signal(str, object)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self) -> None:
        self.completed.emit(
            self.path,
            recursive_directory_usage_bytes(
                self.path,
                cancel_callback=self.isInterruptionRequested,
            ),
        )


def _default_paths() -> dict[str, str]:
    home = Path.home()
    return {
        "xplane_path": "",
        "scenery_path": str(home / ".autoortho-data" / "scenery"),
        "cache_dir": str(home / ".autoortho-data" / "cache"),
        "long_term_cache_dir": "",
        "download_dir": str(home / ".autoortho-data" / "downloads"),
        "webui_port": "5847",
        "xplane_udp_port": "49000",
        "storage_safety_margin_gb": "2",
    }


def _value_from(source: Any, key: str, default: str = "") -> str:
    if source is None:
        return default
    if isinstance(source, dict):
        if key in source:
            return str(source.get(key) or "")
        paths = source.get("paths")
        if isinstance(paths, dict) and key in paths:
            return str(paths.get(key) or "")
    if hasattr(source, key):
        return str(getattr(source, key) or "")
    paths = getattr(source, "paths", None)
    if paths is not None and hasattr(paths, key):
        return str(getattr(paths, key) or "")
    return default


def _normalize_choice(choice: Any, selected_region_ids: set[str]) -> SceneryChoice:
    if isinstance(choice, SceneryChoice):
        selected = choice.selected or choice.region_id in selected_region_ids
        return SceneryChoice(
            region_id=choice.region_id,
            title=choice.title or choice.region_id,
            selected=selected,
            installed=choice.installed,
            description=choice.description,
            size_bytes=choice.size_bytes,
        )
    if isinstance(choice, dict):
        region_id = str(choice.get("region_id") or choice.get("id") or choice.get("value") or "")
        title = str(choice.get("title") or choice.get("name") or choice.get("label") or region_id)
        installed = bool(choice.get("installed", False))
        selected = bool(choice.get("selected", installed or region_id in selected_region_ids))
        return SceneryChoice(
            region_id=region_id,
            title=title,
            selected=selected,
            installed=installed,
            description=str(choice.get("description") or ""),
            size_bytes=int(choice.get("size_bytes") or choice.get("size") or 0),
        )
    region_id = str(getattr(choice, "region_id", getattr(choice, "id", getattr(choice, "value", ""))))
    title = str(getattr(choice, "title", getattr(choice, "name", getattr(choice, "label", region_id))))
    installed = bool(getattr(choice, "installed", False))
    selected = bool(getattr(choice, "selected", installed or region_id in selected_region_ids))
    return SceneryChoice(
        region_id=region_id,
        title=title,
        selected=selected,
        installed=installed,
        description=str(getattr(choice, "description", "")),
        size_bytes=int(getattr(choice, "size_bytes", getattr(choice, "size", 0)) or 0),
    )


class _BasePage(QWizardPage):
    def __init__(self, wizard: "SetupWizard", title: str, subtitle: str = ""):
        super().__init__(wizard)
        self._wizard = wizard
        self.setTitle(title)
        if subtitle:
            self.setSubTitle(subtitle)

    @property
    def wizard_ref(self) -> "SetupWizard":
        return self._wizard


class WelcomePage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard, "Welcome", "Phase 2 setup readiness checks")
        layout = QVBoxLayout(self)
        label = QLabel(
            "AutoOrtho streams satellite scenery to X-Plane while you fly.\n\n"
            "Setup verifies X-Plane and FUSE, prepares storage locations, "
            "and lets you choose scenery regions. Plan for at least 20–30 GB "
            "per region plus temporary download space."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)

    def isComplete(self) -> bool:  # noqa: N802
        return True


class XPlanePage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard, "X-Plane", "Choose the X-Plane installation folder")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("xplanePathEdit")
        self.path_edit.textChanged.connect(self._on_text_changed)
        self.detect_button = QPushButton("Auto-detect")
        self.detect_button.clicked.connect(self._auto_detect)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        button_row = QHBoxLayout()
        button_row.addWidget(self.path_edit)
        button_row.addWidget(self.detect_button)
        button_row.addWidget(self.browse_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        form.addRow("X-Plane install path", button_row)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def _on_text_changed(self, value: str) -> None:
        self.wizard_ref.set_value("xplane_path", value)

    def _auto_detect(self) -> None:
        detected = detect_xplane_installation(self.wizard_ref.xplane_search_roots)
        if detected:
            self.path_edit.setText(str(detected))
            self.wizard_ref.set_value("xplane_path", str(detected))

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select X-Plane installation",
            self.path_edit.text(),
        )
        if folder:
            self.path_edit.setText(folder)

    def isComplete(self) -> bool:  # noqa: N802
        return self.wizard_ref.readiness.checks[0].is_ready


class StoragePage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard, "Storage", "Choose scenery and cache folders")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.scenery_edit = QLineEdit()
        self.cache_edit = QLineEdit()
        self.long_term_edit = QLineEdit()
        self.download_edit = QLineEdit()
        for edit, key in (
            (self.scenery_edit, "scenery_path"),
            (self.cache_edit, "cache_dir"),
            (self.long_term_edit, "long_term_cache_dir"),
            (self.download_edit, "download_dir"),
        ):
            edit.setObjectName(f"{key}Edit")
            edit.textChanged.connect(lambda value, field=key: self.wizard_ref.set_value(field, value))
        self._add_path_row(form, "Scenery path", self.scenery_edit)
        self._add_path_row(form, "Cache dir", self.cache_edit)
        self._add_path_row(
            form,
            "Long-term cache (optional)",
            self.long_term_edit,
        )
        self._add_path_row(form, "Download dir", self.download_edit)
        layout.addLayout(form)

        self.free_space_label = QLabel()
        self.cache_usage_label = QLabel()
        self._usage_worker = None
        self._pending_usage_path = ""
        self.free_space_label.setWordWrap(True)
        self.cache_usage_label.setWordWrap(True)
        layout.addWidget(self.free_space_label)
        layout.addWidget(self.cache_usage_label)
        layout.addStretch(1)

    def _add_path_row(self, form, label, edit) -> None:
        row = QHBoxLayout()
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(edit))
        row.addWidget(edit)
        row.addWidget(browse)
        form.addRow(label, row)

    def _browse(self, edit) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select folder",
            edit.text(),
        )
        if folder:
            edit.setText(folder)

    def _refresh_storage_summary(self) -> None:
        values = self.wizard_ref.selected_paths
        scenery_free = format_bytes(free_space_bytes(values["scenery_path"] or values["cache_dir"] or Path.home()))
        cache_free = format_bytes(free_space_bytes(values["cache_dir"]))
        download_free = format_bytes(free_space_bytes(values["download_dir"]))
        self.free_space_label.setText(
            f"Free space — scenery: {scenery_free}, cache: {cache_free}, download: {download_free}"
        )
        self._start_usage_scan(values["cache_dir"])

    def _start_usage_scan(self, path: str) -> None:
        if (
            self._usage_worker is not None
            and self._usage_worker.isRunning()
        ):
            self._pending_usage_path = path
            return
        worker = _DirectoryUsageWorker(path)
        worker.completed.connect(self._usage_completed)
        worker.finished.connect(
            lambda current=worker: self._usage_finished(current)
        )
        self._usage_worker = worker
        self.cache_usage_label.setText("Current cache usage: calculating…")
        worker.start()

    def _usage_completed(self, path: str, usage: int) -> None:
        if path == self.cache_edit.text():
            self.cache_usage_label.setText(
                f"Current cache usage: {format_bytes(usage)}"
            )

    def _usage_finished(self, worker) -> None:
        if self._usage_worker is worker:
            self._usage_worker = None
        worker.deleteLater()
        pending = self._pending_usage_path
        self._pending_usage_path = ""
        if pending and pending != worker.path:
            self._start_usage_scan(pending)

    def stop_usage_scan(self) -> None:
        worker = self._usage_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            if not worker.wait(2000):
                worker.terminate()
                worker.wait()
        self._usage_worker = None

    def isComplete(self) -> bool:  # noqa: N802
        return not self.wizard_ref.readiness.checks[1].is_blocking


class DependenciesPage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard, "Dependencies", "FUSE backends for this platform")
        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)

    def isComplete(self) -> bool:  # noqa: N802
        return self.wizard_ref.readiness.checks[2].is_ready


class SceneryPage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard, "Scenery", "Choose one or more regions to install")
        layout = QVBoxLayout(self)
        self.note_label = QLabel()
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.list_widget)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.storage_label = QLabel()
        self.storage_label.setWordWrap(True)
        layout.addWidget(self.storage_label)
        layout.addStretch(1)

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        self.wizard_ref._sync_scenery_selection()

    def isComplete(self) -> bool:  # noqa: N802
        return True


class ReviewPage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard, "Review", "Confirm the setup state")
        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.details)

    def isComplete(self) -> bool:  # noqa: N802
        return self.wizard_ref.readiness.can_finish


class SetupWizard(QWizard):
    def __init__(
        self,
        initial_values: Any | None = None,
        scenery_choices: Iterable[Any] = (),
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("AutoOrtho Setup Wizard")
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        self._values = _default_paths()
        if initial_values is not None:
            for key in self._values:
                value = _value_from(initial_values, key, self._values[key])
                if value:
                    self._values[key] = value

        self.xplane_search_roots = getattr(initial_values, "xplane_search_roots", None)
        if isinstance(initial_values, dict):
            self.xplane_search_roots = initial_values.get("xplane_search_roots", self.xplane_search_roots)

        selected_region_ids = set()
        if isinstance(initial_values, dict):
            selected_region_ids = set(initial_values.get("selected_region_ids") or [])
        else:
            selected_region_ids = set(getattr(initial_values, "selected_region_ids", []) or [])

        self._choices = [
            _normalize_choice(choice, selected_region_ids)
            for choice in scenery_choices
        ]
        self._readiness: SetupReadiness = build_readiness(self._values, self._choices, search_roots=self.xplane_search_roots)

        self._build_pages()
        self._refresh_widgets()
        self.currentIdChanged.connect(lambda _index: self._refresh_widgets())

    @property
    def readiness(self) -> SetupReadiness:
        return self._readiness

    @property
    def selected_paths(self) -> dict[str, str]:
        keys = (
            "xplane_path",
            "scenery_path",
            "cache_dir",
            "long_term_cache_dir",
            "download_dir",
        )
        return {key: self._values.get(key, "") for key in keys}

    @property
    def selected_region_ids(self) -> tuple[str, ...]:
        return self._readiness.selected_region_ids

    def get_selected_paths(self) -> dict[str, str]:
        return dict(self.selected_paths)

    def get_selected_region_ids(self) -> list[str]:
        return list(self.selected_region_ids)

    def set_value(self, key: str, value: str) -> None:
        self._values[key] = value
        self._refresh_readiness()

    def _build_pages(self) -> None:
        self.welcome_page = WelcomePage(self)
        self.xplane_page = XPlanePage(self)
        self.storage_page = StoragePage(self)
        self.dependencies_page = DependenciesPage(self)
        self.scenery_page = SceneryPage(self)
        self.review_page = ReviewPage(self)

        for page in (
            self.welcome_page,
            self.xplane_page,
            self.storage_page,
            self.dependencies_page,
            self.scenery_page,
            self.review_page,
        ):
            self.addPage(page)
        self._populate_scenery_choices()

    def _refresh_widgets(self) -> None:
        for edit, key in (
            (self.xplane_page.path_edit, "xplane_path"),
            (self.storage_page.scenery_edit, "scenery_path"),
            (self.storage_page.cache_edit, "cache_dir"),
            (self.storage_page.long_term_edit, "long_term_cache_dir"),
            (self.storage_page.download_edit, "download_dir"),
        ):
            blocker = QSignalBlocker(edit)
            edit.setText(self._values.get(key, ""))
            del blocker
        self._refresh_readiness()

    def _populate_scenery_choices(self) -> None:
        self.scenery_page.list_widget.clear()
        blocker = QSignalBlocker(self.scenery_page.list_widget)
        for choice in self._choices:
            size = (
                f" — {format_bytes(choice.size_bytes)}"
                if choice.size_bytes
                else ""
            )
            item = QListWidgetItem(
                f"{choice.title or choice.region_id}{size}"
            )
            item.setData(Qt.ItemDataRole.UserRole, choice.region_id)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(Qt.CheckState.Checked if choice.selected else Qt.CheckState.Unchecked)
            if choice.installed:
                item.setText(f"{item.text()} (installed)")
            self.scenery_page.list_widget.addItem(item)
        del blocker

    def _sync_scenery_selection(self) -> None:
        if not hasattr(self, "scenery_page"):
            return
        current_items = []
        existing = {choice.region_id: choice for choice in self._choices}
        for index in range(self.scenery_page.list_widget.count()):
            item = self.scenery_page.list_widget.item(index)
            region_id = item.data(Qt.ItemDataRole.UserRole)
            selected = item.checkState() == Qt.CheckState.Checked
            choice = existing.get(region_id)
            if choice:
                current_items.append(
                    SceneryChoice(
                        region_id=choice.region_id,
                        title=choice.title,
                        selected=selected,
                        installed=choice.installed,
                        description=choice.description,
                        size_bytes=choice.size_bytes,
                    )
                )
        if current_items:
            self._choices = current_items
        self._refresh_readiness()

    def _refresh_readiness(self) -> None:
        self._readiness = build_readiness(self._values, self._choices, search_roots=self.xplane_search_roots)
        self.xplane_page.status_label.setText(self._format_status(self._readiness.checks[0]))
        self.storage_page._refresh_storage_summary()
        self.dependencies_page.summary_label.setText(self._format_status(self._readiness.checks[2]))
        self.scenery_page.note_label.setText(
            "Existing scenery means you can finish without selecting a region."
            if self._readiness.installed_scenery_present
            else "Select at least one region to enable Finish."
        )
        self.scenery_page.status_label.setText(self._format_status(self._readiness.checks[3]))
        selected_bytes = sum(
            choice.size_bytes
            for choice in self._choices
            if choice.selected and not choice.installed
        )
        self.scenery_page.storage_label.setText(
            "Selected download size: " + format_bytes(selected_bytes)
        )
        self.review_page.summary_label.setText(
            "Finish is available."
            if self._readiness.can_finish
            else "Finish remains disabled until X-Plane, storage, and dependencies are ready."
        )
        self.review_page.details.setPlainText(self._render_review_details())
        for page in (
            self.welcome_page,
            self.xplane_page,
            self.storage_page,
            self.dependencies_page,
            self.scenery_page,
            self.review_page,
        ):
            page.completeChanged.emit()
        finish_button = self.button(QWizard.WizardButton.FinishButton)
        if finish_button is not None:
            finish_button.setEnabled(self._readiness.can_finish)

    def _format_status(self, check) -> str:
        return f"{check.title}: {check.status.value} — {check.message}"

    def _render_review_details(self) -> str:
        lines = []
        for check in self._readiness.checks:
            lines.append(f"[{check.status.value}] {check.title}: {check.message}")
            if check.fix_action:
                lines.append(f"  Fix: {check.fix_action}")
        lines.append("")
        lines.append(f"Selected region IDs: {', '.join(self._readiness.selected_region_ids) or 'none'}")
        lines.append(f"Scenery installed: {'yes' if self._readiness.installed_scenery_present else 'no'}")
        return "\n".join(lines)

    def done(self, result: int) -> None:
        self.storage_page.stop_usage_scan()
        super().done(result)

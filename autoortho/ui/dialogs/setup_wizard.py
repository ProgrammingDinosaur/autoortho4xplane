"""Guided first-run setup wizard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


if __package__ and __package__.startswith("autoortho."):
    from autoortho import guards, hardware
    from autoortho import limits as limits_module
    from autoortho import target_cost
    from autoortho.ui.dynamic_zoom_editor import PRESETS
    from autoortho.ui.widgets.connection_test_panel import ConnectionTestPanel
    from autoortho.ui.widgets.limits_editor import LimitsEditor
    from autoortho.ui.widgets.target_cost_panel import TargetCostPanel
    from autoortho.ui.service_worker import ServiceWorker
    from autoortho.ui.services.readiness_service import ReadinessService
    from autoortho.ui.readiness import (
        SceneryChoice,
        SetupReadiness,
        build_readiness,
        format_bytes,
        free_space_bytes,
        recursive_directory_usage_bytes,
    )
else:
    import guards
    import hardware
    import limits as limits_module
    import target_cost
    from ui.dynamic_zoom_editor import PRESETS
    from ui.widgets.connection_test_panel import ConnectionTestPanel
    from ui.widgets.limits_editor import LimitsEditor
    from ui.widgets.target_cost_panel import TargetCostPanel
    from ui.service_worker import ServiceWorker
    from ui.services.readiness_service import ReadinessService
    from ui.readiness import (
        SceneryChoice,
        SetupReadiness,
        build_readiness,
        format_bytes,
        free_space_bytes,
        recursive_directory_usage_bytes,
    )

from PySide6.QtCore import QSignalBlocker, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QFileDialog,
    QCheckBox,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QWidget,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)


class _DirectoryUsageWorker(QThread):
    completed = Signal(str, object, object)

    def __init__(self, path: str, free_paths: dict[str, str]):
        super().__init__()
        self.path = path
        self.free_paths = dict(free_paths)

    def run(self) -> None:
        usage = recursive_directory_usage_bytes(
            self.path,
            cancel_callback=self.isInterruptionRequested,
        )
        free = {}
        for name, path in self.free_paths.items():
            if self.isInterruptionRequested():
                return
            free[name] = free_space_bytes(path)
        self.completed.emit(
            self.path,
            usage,
            free,
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
        super().__init__(
            wizard,
            "Welcome",
            "Prepare AutoOrtho for X-Plane",
        )
        layout = QVBoxLayout(self)
        label = QLabel(
            "AutoOrtho streams satellite scenery to X-Plane while you fly.\n\n"
            "Setup checks this PC, finds X-Plane, sets up scenery and the "
            "image cache, then the quality you want and what AutoOrtho may "
            "use. Plan for at least 20–30 GB per region plus temporary "
            "download space."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.checks_label = QLabel()
        self.checks_label.setWordWrap(True)
        self.checks_label.setAccessibleName("System checks")
        layout.addWidget(self.checks_label)
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
        self.path_edit.setAccessibleName("X-Plane installation folder")
        self.path_edit.textChanged.connect(self._on_text_changed)
        self.detect_button = QPushButton("Auto-&detect")
        self.detect_button.clicked.connect(self._auto_detect)
        self.browse_button = QPushButton("&Browse…")
        self.browse_button.clicked.connect(self._browse)
        button_row = QHBoxLayout()
        button_row.addWidget(self.path_edit)
        button_row.addWidget(self.detect_button)
        button_row.addWidget(self.browse_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        path_label = QLabel("&X-Plane install path")
        path_label.setBuddy(self.path_edit)
        form.addRow(path_label, button_row)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def _on_text_changed(self, value: str) -> None:
        self.wizard_ref.set_value("xplane_path", value)

    def _auto_detect(self) -> None:
        self.detect_button.setEnabled(False)
        self.status_label.setText("Searching for X-Plane…")
        self.wizard_ref.start_xplane_detection(
            self._detection_finished
        )

    def _detection_finished(self, result) -> None:
        self.detect_button.setEnabled(True)
        if isinstance(result, Exception) or not result.success:
            message = (
                str(result)
                if isinstance(result, Exception)
                else result.error.message
            )
            self.status_label.setText(message)
            return
        if result.value:
            self.path_edit.setText(result.value)
            self.wizard_ref.set_value("xplane_path", result.value)
        else:
            self.status_label.setText(
                "X-Plane was not found automatically. Choose its folder."
            )

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
        super().__init__(
            wizard,
            "Image cache",
            "Where downloaded imagery is kept, and how much of it",
        )
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.scenery_edit = QLineEdit()
        self.cache_edit = QLineEdit()
        self.long_term_edit = QLineEdit()
        self.download_edit = QLineEdit()
        self._browse_buttons = []
        for edit, key in (
            (self.scenery_edit, "scenery_path"),
            (self.cache_edit, "cache_dir"),
            (self.long_term_edit, "long_term_cache_dir"),
            (self.download_edit, "download_dir"),
        ):
            edit.setObjectName(f"{key}Edit")
            edit.textChanged.connect(lambda value, field=key: self.wizard_ref.set_value(field, value))
        # The scenery folder is asked on the Scenery step (it takes this row).
        self.scenery_row = self._make_path_row("&Scenery path", self.scenery_edit)
        self._add_path_row(form, "&Cache directory", self.cache_edit)
        self.disk_cache_spin = QSpinBox()
        self.disk_cache_spin.setRange(0, 2000)
        self.disk_cache_spin.setSingleStep(5)
        self.disk_cache_spin.setSuffix(" GB")
        self.disk_cache_spin.setAccessibleName("Disk cache limit")
        self.disk_cache_spin.setValue(int(wizard.disk_cache_gb))
        self.disk_cache_spin.valueChanged.connect(
            lambda value: setattr(wizard, "disk_cache_gb", int(value))
        )
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.disk_cache_spin)
        self.suggest_cache_button = QPushButton("Use suggestion")
        self.suggest_cache_button.clicked.connect(self._use_suggested_cache)
        cache_row.addWidget(self.suggest_cache_button)
        cache_row.addStretch(1)
        form.addRow("Disk cache &limit", cache_row)
        self.cache_hint_label = QLabel()
        self.cache_hint_label.setWordWrap(True)
        self.cache_hint_label.setProperty("textRole", "secondary")
        form.addRow("", self.cache_hint_label)
        layout.addLayout(form)

        # The long-term cache and download folders are Expert settings: the
        # defaults suit almost everyone, so they wait behind a toggle.
        self.more_folders_button = QPushButton("More folders…")
        self.more_folders_button.setCheckable(True)
        self.more_folders_button.setAccessibleName("Show more folders")
        layout.addWidget(self.more_folders_button)
        self.more_folders = QWidget()
        more_form = QFormLayout(self.more_folders)
        more_form.setContentsMargins(0, 0, 0, 0)
        self._add_path_row(
            more_form,
            "&Long-term cache (optional)",
            self.long_term_edit,
        )
        self._add_path_row(
            more_form,
            "&Temporary download directory",
            self.download_edit,
        )
        self.more_folders.setVisible(False)
        self.more_folders_button.toggled.connect(self.more_folders.setVisible)
        layout.addWidget(self.more_folders)

        self.free_space_label = QLabel()
        self.cache_usage_label = QLabel()
        self._usage_worker = None
        self._pending_usage_path = None
        self.free_space_label.setWordWrap(True)
        self.cache_usage_label.setWordWrap(True)
        layout.addWidget(self.free_space_label)
        layout.addWidget(self.cache_usage_label)
        layout.addStretch(1)
        focus_widgets = [
            self.cache_edit,
            self._browse_buttons[0],
            self.disk_cache_spin,
            self.suggest_cache_button,
            self.more_folders_button,
            self.long_term_edit,
            self._browse_buttons[1],
            self.download_edit,
            self._browse_buttons[2],
        ]
        for previous, current in zip(focus_widgets, focus_widgets[1:]):
            self.setTabOrder(previous, current)

    def suggested_cache_gb(self) -> int:
        """A quarter of the cache drive's free space, 10 to 100 GB."""
        free = free_space_bytes(self.cache_edit.text() or str(Path.home()))
        if not free:
            return 30
        gb = free / 1024 ** 3 * 0.25
        return int(max(10, min(100, round(gb / 5) * 5)))

    def _use_suggested_cache(self) -> None:
        self.disk_cache_spin.setValue(self.suggested_cache_gb())

    def refresh_cache_hint(self) -> None:
        self.cache_hint_label.setText(
            f"Suggested for this drive: {self.suggested_cache_gb()} GB. "
            "AutoOrtho removes the oldest imagery beyond the limit."
        )

    def _make_path_row(self, label, edit):
        row = QHBoxLayout()
        label_widget = QLabel(label)
        label_widget.setBuddy(edit)
        edit.setAccessibleName(label.replace("&", ""))
        browse = QPushButton("Browse…")
        browse.setAccessibleName(f"Browse for {label.replace('&', '')}")
        browse.clicked.connect(lambda: self._browse(edit))
        row.addWidget(edit)
        row.addWidget(browse)
        return label_widget, row

    def _add_path_row(self, form, label, edit) -> None:
        row = QHBoxLayout()
        label_widget = QLabel(label)
        label_widget.setBuddy(edit)
        edit.setAccessibleName(label.replace("&", ""))
        browse = QPushButton("Browse…")
        browse.setAccessibleName(
            f"Browse for {label.replace('&', '')}"
        )
        self._browse_buttons.append(browse)
        browse.clicked.connect(lambda: self._browse(edit))
        row.addWidget(edit)
        row.addWidget(browse)
        form.addRow(label_widget, row)

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
        self.free_space_label.setText("Free space: calculating…")
        self._start_usage_scan(
            values["cache_dir"],
            {
                "scenery": (
                    values["scenery_path"]
                    or values["cache_dir"]
                    or str(Path.home())
                ),
                "cache": values["cache_dir"],
                "download": values["download_dir"],
            },
        )

    def _start_usage_scan(
        self,
        path: str,
        free_paths: dict[str, str],
    ) -> None:
        if (
            self._usage_worker is not None
            and self._usage_worker.isRunning()
        ):
            self._pending_usage_path = (path, free_paths)
            return
        worker = _DirectoryUsageWorker(path, free_paths)
        worker.completed.connect(self._usage_completed)
        worker.finished.connect(
            lambda current=worker: self._usage_finished(current)
        )
        self._usage_worker = worker
        self.cache_usage_label.setText("Current cache usage: calculating…")
        worker.start()

    def _usage_completed(
        self,
        path: str,
        usage: int,
        free: dict[str, int],
    ) -> None:
        if path == self.cache_edit.text():
            self.cache_usage_label.setText(
                f"Current cache usage: {format_bytes(usage)}"
            )
            self.free_space_label.setText(
                "Free space — "
                f"scenery: {format_bytes(free.get('scenery', 0))}, "
                f"cache: {format_bytes(free.get('cache', 0))}, "
                f"download: {format_bytes(free.get('download', 0))}"
            )

    def _usage_finished(self, worker) -> None:
        if self._usage_worker is worker:
            self._usage_worker = None
        worker.deleteLater()
        pending = self._pending_usage_path
        self._pending_usage_path = None
        if pending and (
            pending[0] != worker.path
            or pending[1] != worker.free_paths
        ):
            self._start_usage_scan(*pending)

    def stop_usage_scan(self) -> None:
        worker = self._usage_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait()
        self._usage_worker = None

    def isComplete(self) -> bool:  # noqa: N802
        return not self.wizard_ref.readiness.checks[1].is_blocking


class DependenciesPage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(
            wizard,
            "Dependencies",
            "Virtual drive file system driver (FUSE)",
        )
        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)

    def isComplete(self) -> bool:  # noqa: N802
        return self.wizard_ref.readiness.checks[2].is_ready


class SceneryPage(_BasePage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(
            wizard,
            "Scenery",
            "Install a region, or point at scenery you already have",
        )
        layout = QVBoxLayout(self)
        folder_form = QFormLayout()
        label_widget, row = wizard.storage_page.scenery_row
        folder_form.addRow(label_widget, row)
        layout.addLayout(folder_form)
        self.note_label = QLabel()
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)
        self.list_widget = QListWidget()
        self.list_widget.setAccessibleName(
            "Scenery regions available for installation"
        )
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


def _preset_steps(name):
    return [
        {"altitude_ft": alt, "zoom_level": zoom, "zoom_level_airports": airport}
        for alt, zoom, airport in PRESETS.get(name, ())
    ]


ADJUSTED = "Adjusted to fit this PC"


class QualityPage(_BasePage):
    """Step 5: the quality target, what it needs here, and the connection."""

    def __init__(self, wizard: "SetupWizard"):
        super().__init__(
            wizard,
            "Quality target",
            "How sharp the scenery should be, checked against this PC",
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)

        intro = QLabel(
            "Pick one zoom level everywhere (Fixed), or one per altitude (By "
            "altitude), which keeps detail near the ground and saves graphics "
            "memory and downloads at cruise. Each extra level is four times "
            "the imagery. You can change this later in Settings > Imagery."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        modes = QHBoxLayout()
        self.fixed_radio = QRadioButton("Fixed")
        self.altitude_radio = QRadioButton("By altitude")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.fixed_radio)
        self.mode_group.addButton(self.altitude_radio)
        modes.addWidget(self.fixed_radio)
        modes.addWidget(self.altitude_radio)
        modes.addStretch(1)
        layout.addLayout(modes)

        form = QFormLayout()
        self.zoom_spin = QSpinBox()
        self.zoom_spin.setRange(12, 17)
        self.zoom_spin.setPrefix("ZL")
        self.airport_spin = QSpinBox()
        self.airport_spin.setRange(12, 19)
        self.airport_spin.setPrefix("ZL")
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(PRESETS))
        form.addRow("Scenery zoom level", self.zoom_spin)
        form.addRow("Airport zoom level", self.airport_spin)
        form.addRow("Altitude profile", self.preset_combo)
        self._form = form
        layout.addLayout(form)

        self.cost_panel = TargetCostPanel()
        layout.addWidget(self.cost_panel)
        test_title = QLabel("Connection test")
        test_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(test_title)
        self.connection_panel = ConnectionTestPanel(
            maptype=lambda: wizard.maptype,
            max_connections=lambda: wizard.max_connections,
        )
        layout.addWidget(self.connection_panel)
        layout.addStretch(1)

        self._adjusted_steps = None
        target = wizard.target
        self.zoom_spin.setValue(int(target.get("max_zoom", 16)))
        self.airport_spin.setValue(int(target.get("airport_zoom", 18)))
        dynamic = str(target.get("mode", "fixed")).lower() == "dynamic"
        steps = target.get("steps") or []
        if dynamic and steps:
            def key(items):
                return sorted(
                    (int(s["altitude_ft"]), int(s["zoom_level"]),
                     int(s.get("zoom_level_airports", 18)))
                    for s in items
                )

            match = next(
                (name for name in PRESETS if key(_preset_steps(name)) == key(steps)),
                None,
            )
            if match is None:
                self._adjusted_steps = list(steps)
                self.preset_combo.addItem(ADJUSTED)
                self.preset_combo.setCurrentText(ADJUSTED)
            else:
                self.preset_combo.setCurrentText(match)
        (self.altitude_radio if dynamic else self.fixed_radio).setChecked(True)

        for signal in (
            self.fixed_radio.toggled,
            self.zoom_spin.valueChanged,
            self.airport_spin.valueChanged,
            self.preset_combo.currentTextChanged,
        ):
            signal.connect(lambda *_a: self.update_cost())
        self.cost_panel.fix_requested.connect(self._apply_fix)
        self.cost_panel.suggest_requested.connect(self._suggest)
        self.connection_panel.measured.connect(lambda _r: self.update_cost())
        self._update_mode_rows()
        self.update_cost()

    def _set_row_visible(self, field, visible):
        field.setVisible(visible)
        label = self._form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _update_mode_rows(self):
        fixed = self.fixed_radio.isChecked()
        self._set_row_visible(self.zoom_spin, fixed)
        self._set_row_visible(self.airport_spin, fixed)
        self._set_row_visible(self.preset_combo, not fixed)

    def target(self):
        if self.fixed_radio.isChecked():
            return {
                "mode": "fixed",
                "max_zoom": self.zoom_spin.value(),
                "airport_zoom": self.airport_spin.value(),
                "steps": [],
            }
        name = self.preset_combo.currentText()
        steps = (
            list(self._adjusted_steps or [])
            if name == ADJUSTED
            else _preset_steps(name)
        )
        return {
            "mode": "dynamic",
            "max_zoom": self.zoom_spin.value(),
            "airport_zoom": self.airport_spin.value(),
            "steps": steps,
        }

    def update_cost(self):
        self._update_mode_rows()
        wizard = self.wizard_ref
        target = self.target()
        hw = wizard.hardware
        results = guards.check_target(
            target["mode"],
            target["max_zoom"],
            target["airport_zoom"],
            target["steps"],
            hw,
            cache_dir=wizard.selected_paths.get("cache_dir") or None,
            reserve_gb=wizard.reserve_gb,
            k=wizard.paging_k,
            fmt=wizard.texture_format,
            limits=wizard.limits(),
            connection=self.connection_panel.result,
        )
        self.cost_panel.set_results(results, hw.summary() if hw else "")

    def _apply_fix(self, fix):
        changes = dict(fix.changes)
        if "autoortho.dynamic_zoom_steps" in changes:
            self._adjusted_steps = list(changes["autoortho.dynamic_zoom_steps"])
            if self.preset_combo.findText(ADJUSTED) < 0:
                self.preset_combo.addItem(ADJUSTED)
            self.preset_combo.setCurrentText(ADJUSTED)
        if changes.get("autoortho.max_zoom_mode") == "fixed":
            self.fixed_radio.setChecked(True)
        if "autoortho.max_zoom" in changes:
            self.zoom_spin.setValue(int(changes["autoortho.max_zoom"]))
        if "autoortho.max_zoom_near_airports" in changes:
            self.airport_spin.setValue(
                int(changes["autoortho.max_zoom_near_airports"])
            )
        self.update_cost()

    def _suggest(self):
        wizard = self.wizard_ref
        fix = guards.suggest_target_for_pc(
            wizard.hardware,
            wizard.limits(),
            self.connection_panel.result,
            wizard.reserve_gb,
            k=wizard.paging_k,
            fmt=wizard.texture_format,
        )
        self._apply_fix(fix)
        self.cost_panel.set_advice(f"Suggested {fix.label}")


class LimitsPage(_BasePage):
    """Step 6: what AutoOrtho may use of this PC."""

    def __init__(self, wizard: "SetupWizard"):
        super().__init__(
            wizard,
            "Limits and ready",
            "What AutoOrtho may use of this PC, then a last check",
        )
        layout = QVBoxLayout(self)
        self.auto_radio = QRadioButton("Use what's available (recommended)")
        self.custom_radio = QRadioButton("Set my own limits")
        group = QButtonGroup(self)
        group.addButton(self.auto_radio)
        group.addButton(self.custom_radio)
        layout.addWidget(self.auto_radio)
        explain = QLabel(
            "AutoOrtho works out each limit from this PC and keeps room for "
            "X-Plane. Set your own to keep resources for other apps."
        )
        explain.setWordWrap(True)
        explain.setProperty("textRole", "secondary")
        layout.addWidget(explain)
        layout.addWidget(self.custom_radio)
        self.editor = LimitsEditor()
        self.editor.set_hardware(wizard.hardware, wizard.reserve_gb)
        self.editor.set_values(wizard.limit_values)
        layout.addWidget(self.editor)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        ready_title = QLabel("Ready to fly?")
        ready_title.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(ready_title)
        self.review_summary_label = QLabel()
        self.review_summary_label.setWordWrap(True)
        layout.addWidget(self.review_summary_label)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(120)
        layout.addWidget(self.details, 1)
        self.start_now_check = QCheckBox("Start AutoOrtho when setup finishes")
        self.start_now_check.setChecked(True)
        layout.addWidget(self.start_now_check)

        custom = any(
            limits_module.parse(value) is not None
            for value in wizard.limit_values.values()
        )
        (self.custom_radio if custom else self.auto_radio).setChecked(True)
        self.auto_radio.toggled.connect(self._mode_changed)
        self.editor.changed.connect(self._changed)
        self._mode_changed()

    def _mode_changed(self, *_args):
        auto = self.auto_radio.isChecked()
        self.editor.setEnabled(not auto)
        if auto:
            self.editor.use_all_available()
        self._changed()

    def isComplete(self) -> bool:  # noqa: N802
        return self.wizard_ref.readiness.can_finish

    def _changed(self):
        self.wizard_ref.set_limit_values(self.editor.values())
        lines = limits_module.describe(self.wizard_ref.limits())
        self.summary_label.setText(
            "AutoOrtho will use at most: RAM {ram_gb}, graphics memory "
            "{vram_gb}, downloads {bandwidth_mbps}, CPU {cpu_cores}.".format(
                **lines
            )
        )


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
        *,
        target: dict | None = None,
        limit_values: dict | None = None,
        hardware_info=None,
        reserve_gb: float = limits_module.DEFAULT_XPLANE_VRAM_RESERVE_GB,
        maptype: str = "BI",
        max_connections: int = 160,
        paging_k: float = target_cost.DEFAULT_K,
        texture_format: str = "BC1",
        disk_cache_gb: int = 30,
    ):
        super().__init__(parent)
        self.disk_cache_gb = int(disk_cache_gb)
        self.target = dict(target or {
            "mode": "fixed", "max_zoom": 16, "airport_zoom": 18, "steps": [],
        })
        self.limit_values = {
            name: limits_module.AUTO for name in limits_module.NAMES
        }
        self.limit_values.update(limit_values or {})
        self.hardware = (
            hardware_info or hardware.cached() or hardware.basic()
        )
        self.reserve_gb = float(reserve_gb)
        self.maptype = maptype
        self.max_connections = int(max_connections)
        self.paging_k = float(paging_k)
        self.texture_format = texture_format
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
        self.readiness_service = ReadinessService()
        self._xplane_detection_worker = None
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

    def set_limit_values(self, values: dict) -> None:
        self.limit_values = dict(values)
        page = getattr(self, "quality_page", None)
        if page is not None:
            page.update_cost()

    def limits(self):
        return limits_module.resolve(
            self.limit_values, self.hardware, self.reserve_gb
        )

    def selected_target(self) -> dict:
        return self.quality_page.target()

    def selected_limits(self) -> dict:
        return dict(self.limit_values)

    def selected_disk_cache_gb(self) -> int:
        return int(self.disk_cache_gb)

    def start_now(self) -> bool:
        return self.limits_page.start_now_check.isChecked()

    def start_xplane_detection(self, callback) -> None:
        worker = self._xplane_detection_worker
        if worker is not None and worker.isRunning():
            return
        roots = self.xplane_search_roots
        worker = ServiceWorker(
            lambda cancel_event: self.readiness_service.detect_xplane(
                roots,
                cancel_event=cancel_event,
            ),
            self,
        )
        self._xplane_detection_worker = worker
        worker.completed.connect(callback)
        worker.finished.connect(
            lambda current=worker: self._xplane_detection_finished(
                current
            )
        )
        worker.start()

    def _xplane_detection_finished(self, worker) -> None:
        if self._xplane_detection_worker is worker:
            self._xplane_detection_worker = None

    def _stop_xplane_detection(self) -> None:
        worker = self._xplane_detection_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait()
        self._xplane_detection_worker = None

    def _build_pages(self) -> None:
        self.welcome_page = WelcomePage(self)
        self.xplane_page = XPlanePage(self)
        self.storage_page = StoragePage(self)
        self.dependencies_page = DependenciesPage(self)
        self.scenery_page = SceneryPage(self)
        self.quality_page = QualityPage(self)
        self.limits_page = LimitsPage(self)
        # The last step is limits and the final check together.
        self.review_page = self.limits_page

        # Six steps: checks, X-Plane, scenery, cache, quality, limits/ready.
        # The dependency check shows on the welcome step.
        for page in (
            self.welcome_page,
            self.xplane_page,
            self.scenery_page,
            self.storage_page,
            self.quality_page,
            self.limits_page,
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
        self.welcome_page.checks_label.setText(
            self._format_status(self._readiness.checks[2])
        )
        self.storage_page.refresh_cache_hint()
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
        self.limits_page.review_summary_label.setText(
            "Ready to fly ✓ Finish saves the setup."
            if self._readiness.can_finish
            else "Finish stays disabled until X-Plane, storage, and the file system driver are ready."
        )
        self.limits_page.details.setPlainText(self._render_review_details())
        for page in (
            self.welcome_page,
            self.xplane_page,
            self.storage_page,
            self.scenery_page,
            self.limits_page,
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
        if hasattr(self, "quality_page"):
            target = self.quality_page.target()
            if target["mode"] == "fixed":
                lines.append(
                    f"Quality target: ZL{target['max_zoom']}, airports "
                    f"ZL{target['airport_zoom']}"
                )
            else:
                lines.append(
                    f"Quality target: by altitude "
                    f"({self.quality_page.preset_combo.currentText()})"
                )
            described = limits_module.describe(self.limits())
            lines.append(
                "Limits: RAM {ram_gb}, graphics memory {vram_gb}, downloads "
                "{bandwidth_mbps}, CPU {cpu_cores}".format(**described)
            )
        lines.append(f"Selected region IDs: {', '.join(self._readiness.selected_region_ids) or 'none'}")
        lines.append(f"Scenery installed: {'yes' if self._readiness.installed_scenery_present else 'no'}")
        return "\n".join(lines)

    def done(self, result: int) -> None:
        self.quality_page.connection_panel.stop()
        self.storage_page.stop_usage_scan()
        self._stop_xplane_detection()
        super().done(result)

    def closeEvent(self, event) -> None:
        self.storage_page.stop_usage_scan()
        self._stop_xplane_detection()
        super().closeEvent(event)

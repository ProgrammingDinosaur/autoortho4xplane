"""Categorized settings workspace with search and presets."""

from dataclasses import dataclass

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


@dataclass
class CategoryEntry:
    name: str
    page: QWidget
    searchable_text: str = ""


class SettingsPage(QWidget):
    preset_requested = Signal(str)
    restore_defaults_requested = Signal(str)

    def __init__(self, apply_button, revert_button, restart_label, parent=None):
        super().__init__(parent)
        self.entries: list[CategoryEntry] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        title_row = QHBoxLayout()
        title = QLabel("Settings")
        title.setProperty("textRole", "pageTitle")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search settings…")
        self.search_edit.setAccessibleName("Search settings")
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(
            [
                "Choose preset…",
                "Balanced",
                "Quality",
                "Low Bandwidth",
                "Low Resource",
            ]
        )
        title_row.addWidget(title)
        title_row.addStretch()
        preset_label = QLabel("&Preset")
        preset_label.setBuddy(self.preset_combo)
        title_row.addWidget(self.search_edit)
        title_row.addWidget(preset_label)
        title_row.addWidget(self.preset_combo)
        root.addLayout(title_row)

        body = QHBoxLayout()
        self.category_list = QListWidget()
        self.category_list.setObjectName("settingsCategoryList")
        self.category_list.setSpacing(1)
        self.category_list.setMinimumWidth(180)
        self.category_list.setAccessibleName("Settings categories")
        self.stack = QStackedWidget()
        body.addWidget(self.category_list)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        restart_label.setParent(self)
        apply_button.setParent(self)
        revert_button.setParent(self)
        footer.addWidget(restart_label)
        footer.addStretch()
        footer.addWidget(revert_button)
        footer.addWidget(apply_button)
        root.addLayout(footer)

        self.search_edit.textChanged.connect(self._refresh_categories)
        self.category_list.currentRowChanged.connect(
            self._show_category_row
        )
        self.preset_combo.currentTextChanged.connect(
            self._preset_selected
        )

    def add_category(
        self,
        name,
        widgets,
        *,
        recommendation="",
        numeric_bindings=(),
    ):
        page_content = QWidget()
        content_layout = QVBoxLayout(page_content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        if recommendation:
            recommendation_label = QLabel(recommendation)
            recommendation_label.setWordWrap(True)
            recommendation_label.setProperty("validationState", "info")
            content_layout.addWidget(recommendation_label)
        if numeric_bindings:
            exact_group = QGroupBox("Exact Values")
            exact_form = QFormLayout(exact_group)
            for label, slider, scale, suffix in numeric_bindings:
                if scale == 1:
                    spin = QSpinBox()
                    spin.setRange(slider.minimum(), slider.maximum())
                    spin.setValue(slider.value())
                else:
                    spin = QDoubleSpinBox()
                    spin.setDecimals(2)
                    spin.setRange(
                        slider.minimum() / scale,
                        slider.maximum() / scale,
                    )
                    spin.setValue(slider.value() / scale)
                spin.setSuffix(suffix)
                self._bind_slider(slider, spin, scale)
                exact_form.addRow(label, spin)
            content_layout.addWidget(exact_group)
        for widget in widgets:
            if widget is None:
                continue
            widget.setParent(page_content)
            content_layout.addWidget(widget)
        content_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setWidget(page_content)

        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        category_header = QHBoxLayout()
        heading = QLabel(name)
        heading.setProperty("textRole", "sectionTitle")
        restore = QPushButton("Restore Category Defaults")
        restore.clicked.connect(
            lambda checked=False, category=name: (
                self.restore_defaults_requested.emit(category)
            )
        )
        category_header.addWidget(heading)
        category_header.addStretch()
        category_header.addWidget(restore)
        wrapper_layout.addLayout(category_header)
        wrapper_layout.addWidget(scroll, 1)

        search_parts = [name, recommendation]
        for widget in widgets:
            if widget is None:
                continue
            if hasattr(widget, "text"):
                search_parts.append(str(widget.text()))
            if widget.toolTip():
                search_parts.append(widget.toolTip())
            search_parts.extend(
                child.text() for child in widget.findChildren(QLabel)
            )
            search_parts.extend(
                child.toolTip()
                for child in widget.findChildren(QWidget)
                if child.toolTip()
            )
        text = " ".join(search_parts).lower()
        self.entries.append(
            CategoryEntry(name, wrapper, searchable_text=text)
        )
        self.stack.addWidget(wrapper)
        self._refresh_categories()

    def select_category(self, name):
        for row in range(self.category_list.count()):
            item = self.category_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                self.category_list.setCurrentRow(row)
                return

    def _refresh_categories(self):
        current_name = ""
        current = self.category_list.currentItem()
        if current is not None:
            current_name = current.data(Qt.ItemDataRole.UserRole)
        query = self.search_edit.text().strip().lower()
        self.category_list.blockSignals(True)
        self.category_list.clear()
        for entry in self.entries:
            if query and query not in entry.searchable_text:
                continue
            item = QListWidgetItem(entry.name)
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)
        if self.category_list.count() == 0:
            return
        target = 0
        for row in range(self.category_list.count()):
            if (
                self.category_list.item(row).data(
                    Qt.ItemDataRole.UserRole
                )
                == current_name
            ):
                target = row
                break
        self.category_list.setCurrentRow(target)
        self._show_category_row(target)

    def _show_category_row(self, row):
        item = self.category_list.item(row)
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        entry = next(
            (entry for entry in self.entries if entry.name == name),
            None,
        )
        if entry is not None:
            self.stack.setCurrentWidget(entry.page)

    def _preset_selected(self, name):
        if name == "Choose preset…":
            return
        self.preset_requested.emit(name)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)

    @staticmethod
    def _bind_slider(slider, spin, scale):
        def slider_changed(value):
            target = value / scale
            if spin.value() != target:
                blocked = spin.blockSignals(True)
                spin.setValue(target)
                spin.blockSignals(blocked)

        def spin_changed(value):
            target = round(value * scale)
            if slider.value() != target:
                blocked = slider.blockSignals(True)
                slider.setValue(target)
                slider.blockSignals(blocked)

        slider.valueChanged.connect(slider_changed)
        spin.valueChanged.connect(spin_changed)

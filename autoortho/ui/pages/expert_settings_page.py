"""Every registered setting in one searchable table (Expert settings).

The table lists each setting with its value, default and source. Settings the
other settings pages already edit are shown read-only here, so two controls
never disagree about one value. Everything else can be edited in place or
reset to its default. Edits are pending until the main Apply button saves
them, and Revert discards them, like every other settings control.
"""

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

# Same rule as the other UI modules: never import autoortho.py while the UI is
# still being initialized.
if __package__ and __package__.startswith("autoortho."):
    from autoortho.settings_registry import Tier
else:
    from settings_registry import Tier

# Pending value meaning "return this setting to its default".
RESET = "\x00reset"

_TIER_LABELS = {
    Tier.ESSENTIAL: "Essential",
    Tier.TUNING: "Tuning",
    Tier.EXPERT: "Expert",
}

COLUMNS = ("Setting", "Value", "Default", "Source")

# Sources of values AutoOrtho picked itself (not the user's own).
_AUTOMATIC_SOURCES = {
    "hardware": "This PC",
    "limit": "Your limits",
    "measured": "Measured",
}
_NOT_CHANGED = ("Default",) + tuple(_AUTOMATIC_SOURCES.values())
COL_SETTING, COL_VALUE, COL_DEFAULT, COL_SOURCE = range(4)


def _text(value):
    return "" if value is None else str(value)


class ExpertSettingsModel(QAbstractTableModel):
    pending_changed = Signal()
    validation_failed = Signal(str)

    def __init__(self, config, locked_names=(), parent=None):
        super().__init__(parent)
        self._config = config
        self._locked = frozenset(locked_names)
        self._specs = [
            spec for spec in config.registry()
            if spec.tier in _TIER_LABELS
        ]
        self._pending = {}

    # -- Qt model ----------------------------------------------------------

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self._specs)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return COLUMNS[section]
        return None

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == COL_VALUE and self.is_editable(self.spec(index.row())):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        spec = self._specs[index.row()]
        column = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if column == COL_SETTING:
                return spec.label
            if column == COL_VALUE:
                return self.value_text(spec)
            if column == COL_DEFAULT:
                return self.default_text(spec)
            if column == COL_SOURCE:
                return self.source_text(spec)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.tooltip(spec)
        if role == Qt.ItemDataRole.FontRole and self.source_text(spec) not in (
            _NOT_CHANGED
        ):
            font = QFont()
            font.setBold(True)
            return font
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and column == COL_VALUE
            and not self.is_editable(spec)
        ):
            return QApplication.palette().placeholderText()
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{spec.label}: {self.value_text(spec)}, {self.source_text(spec)}"
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if (
            role != Qt.ItemDataRole.EditRole
            or index.column() != COL_VALUE
        ):
            return False
        spec = self._specs[index.row()]
        if not self.is_editable(spec):
            return False
        ok, normalized, message = spec.validate(value)
        if not ok:
            self.validation_failed.emit(f"{spec.label}: {message}")
            return False
        if normalized == self._saved_text(spec):
            self._pending.pop(spec.name, None)
        else:
            self._pending[spec.name] = normalized
        self._row_changed(index.row())
        return True

    # -- Settings ----------------------------------------------------------

    def spec(self, row):
        return self._specs[row]

    def is_editable(self, spec):
        return spec.name not in self._locked

    def _saved_text(self, spec):
        return _text(self._config.value(spec.section, spec.key))

    def default_text(self, spec):
        """What Reset gives: this PC's value where AutoOrtho scales it."""
        automatic = self._config.automatic_text(spec.section, spec.key)
        return spec.default_text if automatic is None else automatic

    def value_text(self, spec):
        pending = self._pending.get(spec.name)
        if pending == RESET:
            return self.default_text(spec)
        if pending is not None:
            return pending
        return self._saved_text(spec)

    def source_text(self, spec):
        pending = self._pending.get(spec.name)
        if pending == RESET:
            return "Reset (pending)"
        if pending is not None:
            return "Edited (pending)"
        source = self._config.source(spec.section, spec.key)
        if source == "custom":
            return "Custom"
        if source in _AUTOMATIC_SOURCES:
            return _AUTOMATIC_SOURCES[source]
        return "Default"

    def tooltip(self, spec):
        parts = [spec.name, f"Group: {_TIER_LABELS[spec.tier]}"]
        if spec.range is not None:
            parts.append(f"Range: {spec.range[0]} to {spec.range[1]}")
        if self.default_text(spec) != spec.default_text:
            reason = {
                "limit": "Lowered to fit your limits",
                "measured": "From the connection test",
            }.get(
                self._config.source(spec.section, spec.key),
                "Scaled to this PC",
            )
            parts.append(
                f"{reason}; the packaged default is {spec.default_text}."
            )
        if not self.is_editable(spec):
            parts.append("Change this one on its own settings page.")
        if spec.help:
            parts.append(spec.help)
        return "\n".join(parts)

    def search_text(self, row):
        spec = self._specs[row]
        return f"{spec.label} {spec.name} {spec.help}".lower()

    def reset_rows(self, rows):
        for row in rows:
            spec = self._specs[row]
            if not self.is_editable(spec):
                continue
            if self._config.source(spec.section, spec.key) == "custom":
                self._pending[spec.name] = RESET
            else:
                self._pending.pop(spec.name, None)
            self._row_changed(row)

    def pending(self):
        return dict(self._pending)

    def set_pending(self, pending):
        self.beginResetModel()
        self._pending = dict(pending)
        self.endResetModel()
        self.pending_changed.emit()

    def refresh(self):
        """Show the config's current values (after Apply or a reload)."""
        self.beginResetModel()
        self.endResetModel()

    def apply_to(self, config):
        """Write the pending edits into the live config (not to disk)."""
        for name, value in self._pending.items():
            section, key = name.split(".", 1)
            if value == RESET:
                config.reset_to_default(section, key)
            else:
                config.set_value(section, key, value)

    def _row_changed(self, row):
        self.dataChanged.emit(
            self.index(row, 0),
            self.index(row, len(COLUMNS) - 1),
        )
        self.pending_changed.emit()


class _ExpertFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._words = []
        self._only_custom = False
        self._tier = None

    def set_query(self, text):
        self._words = str(text).lower().split()
        self.invalidateFilter()

    def set_only_custom(self, only_custom):
        self._only_custom = bool(only_custom)
        self.invalidateFilter()

    def set_tier(self, tier):
        self._tier = tier
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        model = self.sourceModel()
        spec = model.spec(source_row)
        if self._tier and spec.tier.value != self._tier:
            return False
        if self._only_custom and model.source_text(spec) in _NOT_CHANGED:
            return False
        text = model.search_text(source_row)
        return all(word in text for word in self._words)


class ExpertSettingsPage(QWidget):
    """Search, edit and reset any registered setting."""

    changed = Signal()

    def __init__(self, config, locked_names=(), parent=None):
        super().__init__(parent)
        self.model = ExpertSettingsModel(config, locked_names, self)
        self.proxy = _ExpertFilter(self)
        self.proxy.setSourceModel(self.model)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search all settings…")
        self.search_edit.setAccessibleName("Search all settings")
        self.search_edit.setClearButtonEnabled(True)
        self.tier_combo = QComboBox()
        self.tier_combo.setAccessibleName("Setting group")
        # Item data holds the tier's string value: Qt returns str-based enums
        # as plain strings.
        self.tier_combo.addItem("All groups", "")
        for tier, label in _TIER_LABELS.items():
            self.tier_combo.addItem(label, tier.value)
        self.only_custom_check = QCheckBox("Only changed settings")
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.tier_combo)
        filters.addWidget(self.only_custom_check)
        layout.addLayout(filters)

        self.table = QTableView()
        self.table.setObjectName("expertSettingsTable")
        self.table.setAccessibleName("All settings")
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_SETTING, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_DEFAULT, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(COL_VALUE, 200)
        self.table.setColumnWidth(COL_DEFAULT, 160)
        self.table.setMinimumHeight(440)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel(
            "Double-click a value to change it. Greyed values are changed "
            "on their own settings page. Apply saves your changes."
        )
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("textRole", "secondary")
        self.reset_button = QPushButton("Reset Selected to Default")
        self.reset_button.setEnabled(False)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.reset_button)
        layout.addLayout(footer)

        self.search_edit.textChanged.connect(self.proxy.set_query)
        self.tier_combo.currentIndexChanged.connect(
            lambda _index: self.proxy.set_tier(self.tier_combo.currentData())
        )
        self.only_custom_check.toggled.connect(self.proxy.set_only_custom)
        self.reset_button.clicked.connect(self._reset_selected)
        self.table.selectionModel().selectionChanged.connect(
            self._update_reset_button
        )
        self.model.pending_changed.connect(self.changed.emit)
        self.model.validation_failed.connect(self._show_validation)

    def _selected_rows(self):
        rows = {
            self.proxy.mapToSource(index).row()
            for index in self.table.selectionModel().selectedRows()
        }
        return sorted(rows)

    def _update_reset_button(self, *_args):
        self.reset_button.setEnabled(
            any(
                self.model.is_editable(self.model.spec(row))
                for row in self._selected_rows()
            )
        )

    def _reset_selected(self):
        self.model.reset_rows(self._selected_rows())

    def _show_validation(self, message):
        self.status_label.setText(message)
        self.status_label.setProperty("validationState", "error")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    # -- Settings session --------------------------------------------------

    def snapshot(self):
        """The pending edits, comparable and copyable for Apply/Revert."""
        return tuple(sorted(self.model.pending().items()))

    def restore(self, snapshot):
        self.model.set_pending(dict(snapshot or ()))

    def apply_to(self, config):
        self.model.apply_to(config)

    def mark_saved(self):
        """Forget the pending edits once they are saved, and show new values."""
        self.model.set_pending({})
        self.model.refresh()

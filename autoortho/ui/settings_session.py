"""In-memory Apply/Revert state for configuration widgets."""

from copy import deepcopy
from typing import Any, Iterable

from PySide6.QtCore import QObject, Signal


class SettingsSession(QObject):
    dirty_changed = Signal(bool)
    restart_required_changed = Signal(bool)

    def __init__(
        self,
        restart_required_keys: Iterable[str] = (),
        parent=None,
    ):
        super().__init__(parent)
        self.restart_required_keys = set(restart_required_keys)
        self._baseline: dict[str, Any] = {}
        self._current: dict[str, Any] = {}
        self._dirty = False
        self._restart_required = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def restart_required(self) -> bool:
        return self._restart_required

    @property
    def changed_keys(self) -> set[str]:
        keys = set(self._baseline) | set(self._current)
        return {
            key for key in keys
            if self._baseline.get(key) != self._current.get(key)
        }

    def initialize(self, snapshot: dict[str, Any]) -> None:
        self._baseline = deepcopy(snapshot)
        self._current = deepcopy(snapshot)
        self._set_flags(False, False)

    def observe(self, snapshot: dict[str, Any]) -> None:
        self._current = deepcopy(snapshot)
        changed = self.changed_keys
        self._set_flags(
            bool(changed),
            bool(changed & self.restart_required_keys),
        )

    def mark_applied(self, snapshot: dict[str, Any]) -> None:
        self.initialize(snapshot)

    def revert(self) -> dict[str, Any]:
        self._current = deepcopy(self._baseline)
        self._set_flags(False, False)
        return deepcopy(self._baseline)

    def baseline(self) -> dict[str, Any]:
        return deepcopy(self._baseline)

    def _set_flags(self, dirty: bool, restart_required: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)
        if restart_required != self._restart_required:
            self._restart_required = restart_required
            self.restart_required_changed.emit(restart_required)

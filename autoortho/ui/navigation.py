"""Navigation rail widgets for the application shell."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QToolButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class NavigationDestination:
    """One destination in the shell navigation rail."""

    key: str
    title: str
    tooltip: str = ""


class NavigationRail(QFrame):
    """Compact vertical navigation rail with exclusive destinations."""

    destinationChanged = Signal(str)

    def __init__(
        self,
        destinations: Iterable[NavigationDestination] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("navigationRail")
        self.setAccessibleName("Primary navigation")
        self.setAccessibleDescription(
            "Choose one of the five main AutoOrtho destinations."
        )
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setMinimumWidth(176)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._destinations: list[NavigationDestination] = []
        self._buttons: dict[str, QToolButton] = {}
        self._current_key: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        layout.addStretch(1)
        self._button_layout = layout

        self._group.buttonClicked.connect(self._on_button_clicked)

        for destination in destinations or self._default_destinations():
            self.add_destination(destination)

        if self._destinations:
            self.set_current_destination(self._destinations[0].key, emit=False)

    @staticmethod
    def _default_destinations() -> list[NavigationDestination]:
        return [
            NavigationDestination("home", "Home"),
            NavigationDestination("scenery-library", "Scenery Library"),
            NavigationDestination("flight-plan-map", "Flight Plan & Map"),
            NavigationDestination("settings", "Settings"),
            NavigationDestination("diagnostics", "Diagnostics"),
        ]

    def add_destination(
        self,
        destination: NavigationDestination | str,
        title: str | None = None,
        tooltip: str = "",
    ) -> QToolButton:
        if isinstance(destination, NavigationDestination):
            item = destination
        else:
            if title is None:
                raise ValueError("title is required when destination is a key")
            item = NavigationDestination(destination, title, tooltip)

        button = QToolButton(self)
        button.setObjectName(f"{item.key}Destination")
        button.setText(item.title.replace("&", "&&"))
        button.setToolTip(item.tooltip or item.title)
        button.setAccessibleName(f"Open {item.title}")
        button.setAccessibleDescription(item.tooltip or item.title)
        button.setCheckable(True)
        button.setAutoRaise(False)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        font = button.font()
        font.setPointSize(max(12, font.pointSize() + 2))
        font.setWeight(QFont.Weight.DemiBold)
        button.setFont(font)
        button.setMinimumHeight(42)
        button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        button.clicked.connect(
            lambda _checked=False, key=item.key: self.set_current_destination(key)
        )

        insert_at = max(0, self._button_layout.count() - 1)
        self._button_layout.insertWidget(insert_at, button)
        self._group.addButton(button)
        self._destinations.append(item)
        self._buttons[item.key] = button
        return button

    def destinations(self) -> list[NavigationDestination]:
        return list(self._destinations)

    def destination_keys(self) -> list[str]:
        return [destination.key for destination in self._destinations]

    def button_for(self, key: str) -> QToolButton | None:
        return self._buttons.get(key)

    def current_destination(self) -> str | None:
        return self._current_key

    def current_index(self) -> int:
        if self._current_key is None:
            return -1
        for index, destination in enumerate(self._destinations):
            if destination.key == self._current_key:
                return index
        return -1

    def set_current_destination(
        self,
        destination: str | int | NavigationDestination,
        *,
        emit: bool = True,
    ) -> None:
        if isinstance(destination, NavigationDestination):
            key = destination.key
        elif isinstance(destination, int):
            if destination < 0 or destination >= len(self._destinations):
                raise IndexError("destination index out of range")
            key = self._destinations[destination].key
        else:
            key = destination

        button = self._buttons.get(key)
        if button is None:
            raise KeyError(f"Unknown destination: {key}")

        if self._current_key == key:
            return

        for current_button in self._buttons.values():
            with QSignalBlocker(current_button):
                current_button.setChecked(current_button is button)

        self._current_key = key
        if emit:
            self.destinationChanged.emit(key)

    def _on_button_clicked(self, button: QToolButton) -> None:
        for key, candidate in self._buttons.items():
            if candidate is button:
                self.set_current_destination(key)
                break


__all__ = ["NavigationDestination", "NavigationRail"]

"""Home dashboard page for the application shell."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.theme import repolish
else:
    from ui.theme import repolish


class StatusCard(QFrame):
    """Simple title/value/detail card with empty-state handling."""

    def __init__(
        self,
        key: str,
        title: str,
        empty_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.empty_text = empty_text
        self.setObjectName("statusCard")
        self.setAccessibleName(f"{title} status")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        self.title_label = QLabel(title)
        self.title_label.setObjectName(f"{key}Title")
        self.title_label.setProperty("textRole", "caption")
        layout.addWidget(self.title_label)

        self.value_label = QLabel()
        self.value_label.setObjectName(f"{key}Value")
        self.value_label.setWordWrap(True)
        self.value_label.setProperty("textRole", "sectionTitle")
        layout.addWidget(self.value_label)

        self.detail_label = QLabel()
        self.detail_label.setObjectName(f"{key}Detail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("textRole", "secondary")
        layout.addWidget(self.detail_label)

        layout.addStretch(1)
        self.set_state(None)

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            parts = [f"{k}: {v}" for k, v in value.items()]
            return ", ".join(parts)
        if isinstance(value, Iterable):
            return ", ".join(str(item) for item in value)
        return str(value)

    def set_state(
        self,
        value: Any,
        *,
        detail: Any | None = None,
        empty_text: str | None = None,
    ) -> None:
        empty = empty_text or self.empty_text
        value_text = self._stringify(value)
        if value_text:
            self.value_label.setProperty("textRole", "sectionTitle")
            repolish(self.value_label)
            self.value_label.setText(value_text)
            self.detail_label.setText(self._stringify(detail))
            self.detail_label.setVisible(bool(self.detail_label.text()))
        else:
            self.value_label.setText(empty)
            self.value_label.setProperty("textRole", "secondary")
            repolish(self.value_label)
            self.detail_label.setText(self._stringify(detail))
            self.detail_label.setVisible(bool(self.detail_label.text()))
        self.setAccessibleDescription(
            f"{self.title_label.text()}: {self.value_label.text()}. "
            f"{self.detail_label.text()}"
        )

    def clear(self) -> None:
        self.set_state(None)

    def set_semantic_state(self, state: str) -> None:
        self.setProperty("state", state)
        repolish(self)


class HomePage(QWidget):
    """Dashboard landing page with summary cards and shortcuts."""

    fix_config_requested = Signal()
    install_scenery_requested = Signal()
    open_diagnostics_requested = Signal()
    open_map_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("homePage")
        self.cards: dict[str, StatusCard] = {}
        self.shortcuts: dict[str, QPushButton] = {}

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        scroll.setWidget(content)
        page_layout.addWidget(scroll)

        outer = QVBoxLayout(content)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        title = QLabel("Home")
        title.setProperty("textRole", "pageTitle")
        outer.addWidget(title)

        subtitle = QLabel("At a glance runtime status and common actions.")
        subtitle.setWordWrap(True)
        subtitle.setProperty("textRole", "secondary")
        outer.addWidget(subtitle)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        outer.addLayout(grid)

        card_specs = [
            ("runtime", "Runtime", "■ Stopped"),
            ("readiness", "Readiness", "○ Not checked"),
            ("xplane", "X-Plane", "○ Disconnected"),
            ("mounted_scenery", "Mounted Scenery", "None mounted"),
            ("provider", "Provider", "No provider selected"),
            ("simbrief", "SimBrief", "No flight plan"),
            ("cache", "Cache", "No cache activity"),
            ("task", "Task", "○ Idle"),
            ("throughput", "Throughput", "0 B/s"),
            (
                "recent_failure",
                "Recent Failure",
                "✓ No recent failures",
            ),
        ]
        for index, (key, title_text, empty_text) in enumerate(card_specs):
            card = StatusCard(key, title_text, empty_text, self)
            self.cards[key] = card
            grid.addWidget(card, index // 2, index % 2)

        shortcuts = QHBoxLayout()
        shortcuts.setSpacing(8)
        outer.addLayout(shortcuts)

        self.fix_config_button = QPushButton("Fix config")
        self.install_scenery_button = QPushButton("Install scenery")
        self.open_diagnostics_button = QPushButton("Open diagnostics")
        self.open_map_button = QPushButton("Open map")
        for button in (
            self.fix_config_button,
            self.install_scenery_button,
            self.open_diagnostics_button,
            self.open_map_button,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Fixed,
            )
            shortcuts.addWidget(button)

        shortcuts.addStretch(1)
        self.fix_config_button.clicked.connect(self.fix_config_requested.emit)
        self.install_scenery_button.clicked.connect(
            self.install_scenery_requested.emit
        )
        self.open_diagnostics_button.clicked.connect(
            self.open_diagnostics_requested.emit
        )
        self.open_map_button.clicked.connect(self.open_map_requested.emit)

        self.shortcuts = {
            "fix_config": self.fix_config_button,
            "install_scenery": self.install_scenery_button,
            "open_diagnostics": self.open_diagnostics_button,
            "open_map": self.open_map_button,
        }

    def _card(self, key: str) -> StatusCard:
        if key not in self.cards:
            raise KeyError(f"Unknown card: {key}")
        return self.cards[key]

    def set_runtime_state(self, value: Any, detail: Any | None = None) -> None:
        card = self._card("runtime")
        text = self._card_text(value)
        card.set_state(
            ("● " if text.lower() == "running" else "■ ")
            + text,
            detail=detail,
        )
        card.set_semantic_state(
            "success" if text.lower() == "running" else "info"
        )

    def set_readiness_state(self, value: Any, detail: Any | None = None) -> None:
        card = self._card("readiness")
        text = self._card_text(value)
        ready = text.lower() == "ready"
        card.set_state(("✓ " if ready else "! ") + text, detail=detail)
        card.set_semantic_state("success" if ready else "warning")

    def set_xplane_state(self, value: Any, detail: Any | None = None) -> None:
        card = self._card("xplane")
        text = self._card_text(value)
        connected = text.lower() == "connected"
        card.set_state(
            ("✓ " if connected else "○ ") + text,
            detail=detail,
        )
        card.set_semantic_state("success" if connected else "warning")

    def set_mounted_scenery(
        self,
        value: Any,
        detail: Any | None = None,
    ) -> None:
        self._card("mounted_scenery").set_state(value, detail=detail)

    def set_provider(self, value: Any, detail: Any | None = None) -> None:
        self._card("provider").set_state(value, detail=detail)

    def set_simbrief(self, value: Any, detail: Any | None = None) -> None:
        self._card("simbrief").set_state(value, detail=detail)

    def set_cache(self, value: Any, detail: Any | None = None) -> None:
        self._card("cache").set_state(value, detail=detail)

    def set_task(self, value: Any, detail: Any | None = None) -> None:
        card = self._card("task")
        text = self._card_text(value)
        active = text.lower() != "idle"
        card.set_state(("● " if active else "○ ") + text, detail=detail)
        card.set_semantic_state("info" if active else "")

    def set_throughput(self, value: Any, detail: Any | None = None) -> None:
        self._card("throughput").set_state(value, detail=detail)

    def set_recent_failure(
        self,
        value: Any,
        detail: Any | None = None,
    ) -> None:
        card = self._card("recent_failure")
        card.set_state("! " + self._card_text(value), detail=detail)
        card.set_semantic_state("error")

    def clear_recent_failure(self) -> None:
        card = self._card("recent_failure")
        card.clear()
        card.set_semantic_state("")

    @staticmethod
    def _card_text(value: Any) -> str:
        return StatusCard._stringify(value) or "Unknown"

    def update_summary(self, **kwargs: Any) -> None:
        mapping = {
            "runtime": self.set_runtime_state,
            "readiness": self.set_readiness_state,
            "xplane": self.set_xplane_state,
            "mounted_scenery": self.set_mounted_scenery,
            "provider": self.set_provider,
            "simbrief": self.set_simbrief,
            "cache": self.set_cache,
            "task": self.set_task,
            "throughput": self.set_throughput,
            "recent_failure": self.set_recent_failure,
        }
        for key, setter in mapping.items():
            if key in kwargs:
                value = kwargs[key]
                detail = kwargs.get(f"{key}_detail")
                setter(value, detail)

    # Compatibility aliases for likely downstream use.
    setRuntimeState = set_runtime_state
    setReadinessState = set_readiness_state
    setXPlaneState = set_xplane_state
    setMountedScenery = set_mounted_scenery
    setProvider = set_provider
    setSimBrief = set_simbrief
    setCache = set_cache
    setTask = set_task
    setThroughput = set_throughput
    setRecentFailure = set_recent_failure
    clearRecentFailure = clear_recent_failure


__all__ = ["HomePage", "StatusCard"]

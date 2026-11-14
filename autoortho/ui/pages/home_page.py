"""Home: is it working, am I getting the quality I asked for, what is it
using, do I need to do anything (proposal section 13).

The layout follows the state:

* **Stopped**: the status strip with Start, a one-line readiness that lists
  problems only when there are some, the quality target with the cost
  preview's lights, the last flight, storage and installed scenery.
* **Starting**: a progress block (mounting, waiting for X-Plane, how much of
  the nearby scenery is sharp).
* **Flying**: the quality hero with the session trend, three resource gauges
  (the bottleneck gets a ◆ chip), an activity row with the prefetch horizon,
  and the auto-tuning block.
* **Paused**: the same, dimmed; the numbers stop.
* **After flight**: the last-flight card expanded.

``set_snapshot()`` takes a :class:`home_snapshot.HomeSnapshot` (built once a
second from the governor's sampler) and only formats it. The older setters
(``set_runtime_state`` and friends) still feed the strip and the stopped
blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
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
    from autoortho import home_snapshot as hs
    from autoortho.ui.theme import THEME, repolish
    from autoortho.ui.widgets.notice_bar import NoticeBar
else:
    import home_snapshot as hs
    from ui.theme import THEME, repolish
    from ui.widgets.notice_bar import NoticeBar


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, Iterable):
        return ", ".join(str(item) for item in value)
    return str(value)


def _colour(level: str) -> QColor:
    colors = THEME.colors
    return QColor({
        "success": colors.success,
        "warning": colors.warning,
        "error": colors.error,
    }.get(level, colors.info))


class StatusCard(QFrame):
    """Simple title/value/detail card with empty-state handling."""

    def __init__(self, key: str, title: str, empty_text: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.empty_text = empty_text
        self.setObjectName("statusCard")
        self.setAccessibleName(f"{title} status")
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
        self.set_state(None)

    _stringify = staticmethod(_stringify)

    def set_state(self, value: Any, *, detail: Any | None = None,
                  empty_text: str | None = None) -> None:
        text = _stringify(value)
        self.value_label.setText(text or empty_text or self.empty_text)
        self.value_label.setProperty("textRole", "sectionTitle" if text else "secondary")
        repolish(self.value_label)
        self.detail_label.setText(_stringify(detail))
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


class SparkLine(QWidget):
    """The session's sharpness; blurry moments (under 95 %) in red."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values: List[Optional[float]] = []
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Sharpness this session")

    def set_values(self, values) -> None:
        self.values = list(values or [])
        known = [v for v in self.values if v is not None]
        blurry = len([v for v in known if v < hs.SHARP_GOOD])
        self.setAccessibleDescription(
            "No measurements yet" if not known else
            f"{len(known)} five-second buckets, {blurry} blurry, "
            f"lowest {min(known) * 100:.0f} %"
        )
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 2, -1, -2)
        if not self.values:
            painter.setPen(self.palette().mid().color())
            painter.drawLine(QPointF(rect.left(), rect.bottom()),
                             QPointF(rect.right(), rect.bottom()))
            return
        width = rect.width() / max(1, len(self.values))
        floor = 0.5      # 50 % and below sit on the baseline
        for index, value in enumerate(self.values):
            if value is None:
                continue
            share = max(0.05, (value - floor) / (1 - floor))
            height = rect.height() * min(1.0, share)
            colour = _colour("success" if value >= hs.SHARP_GOOD else "error")
            painter.fillRect(QRectF(rect.left() + index * width,
                                    rect.bottom() - height,
                                    max(1.0, width - 1), height), colour)


class Bar(QWidget):
    """A thin usage bar coloured by headroom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.share: Optional[float] = None
        self.level = "info"
        self.setFixedHeight(6)

    def set_share(self, share, level):
        self.share, self.level = share, level
        self.setVisible(share is not None)
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        if self.share is None:
            return
        painter = QPainter(self)
        rect = QRectF(self.rect())
        painter.fillRect(rect, self.palette().mid().color())
        painter.fillRect(QRectF(rect.left(), rect.top(),
                                rect.width() * max(0.0, min(1.0, self.share)),
                                rect.height()), _colour(self.level))


class TileRadar(QWidget):
    """Tiles around the aircraft by state, and the prefetch corridor.

    North up, the aircraft in the middle, 60 nm to the edge. Green: sharp at
    target; blue: being read now; amber: below target; grey: not loaded.
    Click to open the map.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.heading = None
        self.corridor_nm = 0.0
        self.setFixedSize(150, 150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Tile radar")
        self.setToolTip("Tiles around the aircraft (north up, 60 nm). Click for the map.")

    def set_radar(self, points, heading=None, corridor_nm=0.0):
        self.points = list(points or [])
        self.heading = heading
        self.corridor_nm = float(corridor_nm or 0.0)
        self.setAccessibleDescription(hs.radar_text(self.points))
        self.update()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event):  # noqa: N802
        import math

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        centre = rect.center()
        radius = min(rect.width(), rect.height()) / 2
        scale = radius / hs.RADAR_RADIUS_NM
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawEllipse(centre, radius, radius)
        painter.drawEllipse(centre, radius / 2, radius / 2)
        colours = [_colour("success"), _colour("info"), _colour("warning"),
                   self.palette().mid().color()]
        painter.setPen(Qt.PenStyle.NoPen)
        for dx, dy, code in self.points:
            painter.setBrush(colours[int(code)] if 0 <= int(code) < 4 else colours[3])
            painter.drawEllipse(QPointF(centre.x() + dx * scale, centre.y() - dy * scale),
                                2.5, 2.5)
        if self.heading is not None and self.corridor_nm > 0:
            angle = math.radians(self.heading)
            end = QPointF(centre.x() + math.sin(angle) * self.corridor_nm * scale,
                          centre.y() - math.cos(angle) * self.corridor_nm * scale)
            painter.setPen(QPen(self.palette().text().color(), 1, Qt.PenStyle.DashLine))
            painter.drawLine(centre, end)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().text().color())
        painter.drawEllipse(centre, 3, 3)


class GaugeCard(QFrame):
    """One resource: headline, detail, bar, and a ◆ chip on the bottleneck."""

    def __init__(self, key: str, title: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("statusCard")
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)
        top = QHBoxLayout()
        self.title_label = QLabel(title.upper())
        self.title_label.setProperty("textRole", "caption")
        top.addWidget(self.title_label)
        top.addStretch(1)
        self.chip = QLabel("◆ limit")
        self.chip.setObjectName("chipLabel")
        self.chip.setProperty("state", "warning")
        self.chip.setToolTip("Auto-tuning finds this is what holds the scenery back now")
        self.chip.setVisible(False)
        top.addWidget(self.chip)
        layout.addLayout(top)
        self.value_label = QLabel("—")
        self.value_label.setProperty("textRole", "sectionTitle")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)
        self.bar = Bar()
        layout.addWidget(self.bar)
        self.detail_label = QLabel("")
        self.detail_label.setProperty("textRole", "secondary")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)
        self.setAccessibleName(f"{title} gauge")

    def set_gauge(self, gauge: "hs.Gauge") -> None:
        self.value_label.setText(gauge.value)
        self.detail_label.setText(gauge.detail)
        self.bar.set_share(gauge.share, gauge.level)
        self.chip.setVisible(gauge.bottleneck)
        self.setProperty("state", gauge.level if gauge.share is not None or gauge.bottleneck
                         else "")
        repolish(self)
        self.setAccessibleDescription(gauge.sentence)


class ClickableCard(QFrame):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statusCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("textRole", "caption")
    return label


def _secondary(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("textRole", "secondary")
    label.setWordWrap(True)
    return label


class HomePage(QWidget):
    """State-based Home (see the module docstring)."""

    fix_config_requested = Signal()
    install_scenery_requested = Signal()
    open_diagnostics_requested = Signal()
    open_map_requested = Signal()
    open_library_requested = Signal()
    primary_action_requested = Signal()
    tuning_requested = Signal()
    edit_target_requested = Signal()
    open_report_requested = Signal()
    last_flight_dismissed = Signal()
    compact_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("homePage")
        self.snapshot: Optional[hs.HomeSnapshot] = None
        self._runtime_text = "Stopped"
        self._xplane_connected = False
        self._packs_text = ""
        self._provider = ""
        self._readiness_issues: List[str] = []
        self._task_text = ""

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        scroll.setWidget(content)
        page_layout.addWidget(scroll)
        outer = QVBoxLayout(content)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("Home")
        title.setProperty("textRole", "pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.compact_check = QCheckBox("Compact")
        self.compact_check.setToolTip(
            "Only the status, quality and gauges: for a second monitor"
        )
        self.compact_check.toggled.connect(self.set_compact)
        title_row.addWidget(self.compact_check)
        outer.addLayout(title_row)

        # -- status strip ---------------------------------------------------
        self.status_strip = QFrame()
        self.status_strip.setObjectName("statusCard")
        self.status_strip.setAccessibleName("Status")
        strip = QGridLayout(self.status_strip)
        strip.setContentsMargins(10, 8, 10, 8)
        self.state_pill = QLabel("■ Stopped")
        self.state_pill.setObjectName("chipLabel")
        self.state_pill.setAccessibleName("AutoOrtho state")
        self.strip_detail_label = _secondary("")
        self.flight_label = _secondary("")
        self.flight_label.setAccessibleName("Flight")
        self.primary_button = QPushButton("Start streaming")
        self.primary_button.setProperty("role", "primary")
        self.primary_button.clicked.connect(self.primary_action_requested.emit)
        strip.addWidget(self.state_pill, 0, 0)
        strip.addWidget(self.strip_detail_label, 0, 1)
        strip.addWidget(self.primary_button, 0, 2, 2, 1, Qt.AlignmentFlag.AlignRight)
        strip.addWidget(self.flight_label, 1, 0, 1, 2)
        strip.setColumnStretch(1, 1)
        outer.addWidget(self.status_strip)

        self.notice_bar = NoticeBar(self)
        outer.addWidget(self.notice_bar)

        # -- stopped: readiness, target, storage, scenery ---------------------
        self.stopped_block = QWidget()
        stopped = QVBoxLayout(self.stopped_block)
        stopped.setContentsMargins(0, 0, 0, 0)
        stopped.setSpacing(8)
        self.ready_card = StatusCard("readiness", "Readiness", "○ Not checked")
        stopped.addWidget(self.ready_card)
        self.target_card = QFrame()
        self.target_card.setObjectName("statusCard")
        self.target_card.setAccessibleName("Quality target")
        target = QVBoxLayout(self.target_card)
        target.setContentsMargins(10, 8, 10, 8)
        target_top = QHBoxLayout()
        target_top.addWidget(_caption("QUALITY TARGET"))
        target_top.addStretch(1)
        self.edit_target_button = QPushButton("Edit")
        self.edit_target_button.clicked.connect(self.edit_target_requested.emit)
        target_top.addWidget(self.edit_target_button)
        target.addLayout(target_top)
        self.target_label = QLabel("")
        self.target_label.setProperty("textRole", "sectionTitle")
        self.target_label.setWordWrap(True)
        target.addWidget(self.target_label)
        self.target_lights_label = _secondary("")
        self.target_lights_label.setTextFormat(Qt.TextFormat.RichText)
        target.addWidget(self.target_lights_label)
        stopped.addWidget(self.target_card)
        row = QHBoxLayout()
        self.storage_card = StatusCard("cache", "Storage", "Not scanned yet")
        self.scenery_card = StatusCard("mounted_scenery", "Scenery", "None installed")
        row.addWidget(self.storage_card)
        row.addWidget(self.scenery_card)
        stopped.addLayout(row)
        outer.addWidget(self.stopped_block)

        # -- files mode (streaming without FUSE) -------------------------------
        self.files_card = QFrame()
        self.files_card.setObjectName("statusCard")
        self.files_card.setAccessibleName("Files mode")
        files = QVBoxLayout(self.files_card)
        files.setContentsMargins(10, 8, 10, 8)
        files.addWidget(_caption("FILES MODE"))
        self.files_label = QLabel("")
        self.files_label.setWordWrap(True)
        files.addWidget(self.files_label)
        self.files_detail_label = _secondary("")
        files.addWidget(self.files_detail_label)
        self.files_card.setVisible(False)
        outer.addWidget(self.files_card)

        # -- starting ----------------------------------------------------------
        self.loading_card = QFrame()
        self.loading_card.setObjectName("statusCard")
        self.loading_card.setProperty("state", "info")
        self.loading_card.setAccessibleName("Starting")
        loading = QVBoxLayout(self.loading_card)
        loading.setContentsMargins(10, 8, 10, 8)
        loading.addWidget(_caption("STARTING"))
        self.loading_label = QLabel("")
        self.loading_label.setWordWrap(True)
        self.loading_label.setProperty("textRole", "sectionTitle")
        loading.addWidget(self.loading_label)
        outer.addWidget(self.loading_card)

        # -- flying: hero, gauges, activity, auto-tuning ----------------------
        self.flying_block = QWidget()
        flying = QVBoxLayout(self.flying_block)
        flying.setContentsMargins(0, 0, 0, 0)
        flying.setSpacing(8)
        self.hero = ClickableCard()
        self.hero.setAccessibleName("Scenery quality")
        self.hero.setToolTip("Open Auto-Tuning for the timeline")
        self.hero.clicked.connect(self.tuning_requested.emit)
        hero = QGridLayout(self.hero)
        hero.setContentsMargins(10, 8, 10, 8)
        hero.addWidget(_caption("SCENERY QUALITY"), 0, 0, 1, 2)
        self.fidelity_label = QLabel("Measuring sharpness")
        self.fidelity_label.setProperty("textRole", "pageTitle")
        hero.addWidget(self.fidelity_label, 1, 0)
        self.hero_target_label = _secondary("")
        self.hero_target_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        hero.addWidget(self.hero_target_label, 1, 1)
        self.sparkline = SparkLine()
        hero.addWidget(self.sparkline, 2, 0, 1, 2)
        self.sharp_after_label = _secondary("")
        hero.addWidget(self.sharp_after_label, 3, 0, 1, 2)
        flying.addWidget(self.hero)

        gauges = QHBoxLayout()
        self.gauges = {
            "network": GaugeCard("network", "Network"),
            "computer": GaugeCard("computer", "Computer"),
            "graphics": GaugeCard("graphics", "Graphics memory"),
        }
        for gauge in self.gauges.values():
            gauges.addWidget(gauge)
        flying.addLayout(gauges)

        self.activity_card = QFrame()
        self.activity_card.setObjectName("statusCard")
        self.activity_card.setAccessibleName("Activity")
        activity = QGridLayout(self.activity_card)
        activity.setContentsMargins(10, 8, 10, 8)
        activity.addWidget(_caption("ACTIVITY"), 0, 0)
        self.activity_label = QLabel("")
        self.activity_label.setWordWrap(True)
        activity.addWidget(self.activity_label, 1, 0, Qt.AlignmentFlag.AlignTop)
        self.radar = TileRadar()
        self.radar.clicked.connect(self.open_map_requested.emit)
        activity.addWidget(self.radar, 0, 1, 2, 1)
        activity.setColumnStretch(0, 1)
        flying.addWidget(self.activity_card)

        self.tuning_card = QFrame()
        self.tuning_card.setObjectName("statusCard")
        self.tuning_card.setAccessibleName("Auto-tuning")
        tuning = QGridLayout(self.tuning_card)
        tuning.setContentsMargins(10, 8, 10, 8)
        self.tuning_title = _caption("AUTO-TUNING")
        tuning.addWidget(self.tuning_title, 0, 0)
        self.tuning_label = QLabel("")
        self.tuning_label.setWordWrap(True)
        tuning.addWidget(self.tuning_label, 1, 0)
        self.see_tuning_button = QPushButton("See tuning")
        self.see_tuning_button.clicked.connect(self.tuning_requested.emit)
        tuning.addWidget(self.see_tuning_button, 1, 1, Qt.AlignmentFlag.AlignBottom)
        tuning.setColumnStretch(0, 1)
        flying.addWidget(self.tuning_card)
        outer.addWidget(self.flying_block)
        self._dim = QGraphicsOpacityEffect(self.flying_block)
        self._dim.setOpacity(1.0)
        self.flying_block.setGraphicsEffect(self._dim)

        # -- last flight -------------------------------------------------------
        self.last_flight_card = QFrame()
        self.last_flight_card.setObjectName("statusCard")
        self.last_flight_card.setAccessibleName("Last flight")
        last = QGridLayout(self.last_flight_card)
        last.setContentsMargins(10, 8, 10, 8)
        self.last_flight_title = _caption("LAST FLIGHT")
        last.addWidget(self.last_flight_title, 0, 0)
        self.last_flight_verdict = QLabel("")
        self.last_flight_verdict.setProperty("textRole", "sectionTitle")
        last.addWidget(self.last_flight_verdict, 1, 0)
        self.last_flight_details = QLabel("")
        self.last_flight_details.setWordWrap(True)
        last.addWidget(self.last_flight_details, 2, 0)
        buttons = QHBoxLayout()
        self.open_report_button = QPushButton("Open report")
        self.open_report_button.clicked.connect(self.open_report_requested.emit)
        self.last_flight_map_button = QPushButton("Open map")
        self.last_flight_map_button.clicked.connect(self.open_map_requested.emit)
        self.dismiss_last_flight_button = QPushButton("Dismiss")
        self.dismiss_last_flight_button.clicked.connect(self.last_flight_dismissed.emit)
        for button in (self.open_report_button, self.last_flight_map_button,
                       self.dismiss_last_flight_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        last.addLayout(buttons, 3, 0)
        outer.addWidget(self.last_flight_card)

        # -- footer ------------------------------------------------------------
        self.footer = QWidget()
        shortcuts = QHBoxLayout(self.footer)
        shortcuts.setContentsMargins(0, 0, 0, 0)
        shortcuts.setSpacing(8)
        self.open_map_button = QPushButton("Open map")
        self.library_button = QPushButton("Scenery library")
        self.open_diagnostics_button = QPushButton("Diagnostics")
        self.install_scenery_button = QPushButton("Install scenery")
        self.fix_config_button = QPushButton("Fix config")
        for button in (self.open_map_button, self.library_button,
                       self.open_diagnostics_button, self.install_scenery_button,
                       self.fix_config_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            shortcuts.addWidget(button)
        shortcuts.addStretch(1)
        outer.addWidget(self.footer)
        outer.addStretch(1)
        self.fix_config_button.clicked.connect(self.fix_config_requested.emit)
        self.install_scenery_button.clicked.connect(self.install_scenery_requested.emit)
        self.open_diagnostics_button.clicked.connect(self.open_diagnostics_requested.emit)
        self.open_map_button.clicked.connect(self.open_map_requested.emit)
        self.library_button.clicked.connect(self.open_library_requested.emit)
        self.shortcuts = {
            "fix_config": self.fix_config_button,
            "install_scenery": self.install_scenery_button,
            "open_diagnostics": self.open_diagnostics_button,
            "open_map": self.open_map_button,
            "library": self.library_button,
        }
        # Reading order: strip, notices, then the state's blocks.
        self.cards = {
            "readiness": self.ready_card,
            "cache": self.storage_card,
            "mounted_scenery": self.scenery_card,
        }
        self.compact = False
        self.set_snapshot(hs.build(runtime="stopped"))

    # -- the snapshot --------------------------------------------------------

    def set_snapshot(self, snapshot: "hs.HomeSnapshot") -> None:
        paused = snapshot.state == hs.PAUSED
        frozen = paused and self.snapshot is not None and self.snapshot.state in (
            hs.FLYING, hs.PAUSED)
        self.snapshot = snapshot
        self._show_strip(snapshot)
        state = snapshot.state
        stopped = state in (hs.STOPPED, hs.AFTER_FLIGHT)
        flying = state in (hs.FLYING, hs.PAUSED)
        self.stopped_block.setVisible(stopped and not self.compact)
        self.loading_card.setVisible(state == hs.STARTING)
        self.flying_block.setVisible(flying)
        self._dim.setOpacity(0.55 if paused else 1.0)
        if state == hs.STARTING:
            self.loading_label.setText("\n".join(snapshot.loading_lines))
            self.loading_card.setAccessibleDescription(
                ". ".join(snapshot.loading_lines))
        if flying and not frozen:
            self._show_flying(snapshot)
        self._show_last_flight(snapshot)
        if snapshot.target_text:
            self.target_label.setText(snapshot.target_text)

    def _show_strip(self, snapshot) -> None:
        symbol = {
            hs.FLYING: "●", hs.STARTING: "◐", hs.PAUSED: "❚❚",
        }.get(snapshot.state, "■")
        self.state_pill.setText(f"{symbol} {snapshot.pill}")
        self.state_pill.setProperty("state", {
            hs.FLYING: "success", hs.STARTING: "info", hs.PAUSED: "warning",
        }.get(snapshot.state, "error" if snapshot.pill == "Error" else ""))
        repolish(self.state_pill)
        detail = snapshot.strip_detail or self._packs_text
        if self._task_text:
            detail = f"{detail} · {self._task_text}" if detail else self._task_text
        self.strip_detail_label.setText(detail)
        self.flight_label.setText(snapshot.flight_line)
        self.flight_label.setVisible(bool(snapshot.flight_line))
        running = snapshot.state not in (hs.STOPPED, hs.AFTER_FLIGHT)
        self.primary_button.setText("Stop streaming" if running else "Start streaming")
        self.primary_button.setProperty("role", "" if running else "primary")
        repolish(self.primary_button)
        self.status_strip.setAccessibleDescription(
            f"{snapshot.pill}. {detail}. {snapshot.flight_line}".strip(". ")
        )

    def _show_flying(self, snapshot) -> None:
        self.fidelity_label.setText(snapshot.hero_value)
        self.hero_target_label.setText(snapshot.target_text)
        self.sparkline.set_values(snapshot.trend)
        self.sharp_after_label.setText(snapshot.sharp_after)
        self.sharp_after_label.setVisible(bool(snapshot.sharp_after))
        level = ("info" if snapshot.fidelity is None else
                 "success" if snapshot.fidelity >= hs.SHARP_GOOD else
                 "warning" if snapshot.fidelity >= 0.85 else "error")
        self.hero.setProperty("state", level)
        repolish(self.hero)
        self.hero.setAccessibleDescription(
            f"{snapshot.hero_value}. {snapshot.target_text}. {snapshot.sharp_after}"
        )
        for gauge, card in zip(snapshot.gauges, self.gauges.values()):
            card.set_gauge(gauge)
        self.activity_label.setText("\n".join(snapshot.activity_lines))
        self.radar.set_radar(snapshot.radar, snapshot.heading, snapshot.corridor_nm)
        self.activity_card.setAccessibleDescription(". ".join(snapshot.activity_lines))
        self.activity_card.setVisible(bool(snapshot.activity_lines) and not self.compact)
        self.tuning_card.setVisible(snapshot.tuning_visible and not self.compact)
        if snapshot.tuning_visible:
            self.tuning_label.setText("\n".join(snapshot.tuning_lines))
            self.tuning_card.setAccessibleDescription(". ".join(snapshot.tuning_lines))

    def _show_last_flight(self, snapshot) -> None:
        summary = snapshot.last_flight
        show = bool(summary) and snapshot.state in (hs.STOPPED, hs.AFTER_FLIGHT) and not self.compact
        self.last_flight_card.setVisible(show)
        if not show:
            return
        expanded = snapshot.state == hs.AFTER_FLIGHT
        when = hs.ended_text(summary)
        self.last_flight_title.setText("LAST FLIGHT" + (f" · {when.upper()}" if when else ""))
        self.last_flight_verdict.setText(summary.get("verdict", ""))
        lines = hs.last_flight_lines(summary)
        self.last_flight_details.setText("\n".join(lines if expanded else lines[:1]))
        self.last_flight_card.setProperty("state", hs.verdict_level(summary))
        repolish(self.last_flight_card)
        self.dismiss_last_flight_button.setVisible(expanded)
        self.last_flight_card.setAccessibleDescription(
            f"{summary.get('verdict', '')}. " + ". ".join(lines)
        )

    def set_files_mode(self, status: Optional[Mapping[str, Any]]) -> None:
        """Files mode's card: ``None`` hides it (FUSE mode).

        ``status`` has ``headline``, ``detail`` and ``state`` (ok, info,
        warning, error), from :func:`files_mode_status`.
        """
        if not status:
            self.files_card.setVisible(False)
            return
        self.files_label.setText(str(status.get("headline", "")))
        self.files_detail_label.setText(str(status.get("detail", "")))
        self.files_card.setProperty("state", status.get("state", "info"))
        self.files_card.style().unpolish(self.files_card)
        self.files_card.style().polish(self.files_card)
        self.files_card.setVisible(True)

    def set_compact(self, compact: bool) -> None:
        """Status strip, quality and gauges only (for a second monitor)."""
        self.compact = bool(compact)
        if self.compact_check.isChecked() != self.compact:
            self.compact_check.setChecked(self.compact)
        for widget in (self.footer,):
            widget.setVisible(not self.compact)
        if self.snapshot is not None:
            self.set_snapshot(self.snapshot)
        self.compact_changed.emit(self.compact)

    def set_target_lights(self, results) -> None:
        """The cost preview's lights for the saved target (stopped state)."""
        parts = []
        symbol = {"ok": "✓", "warn": "!", "bad": "✗", "info": "○"}
        level = {"ok": "success", "warn": "warning", "bad": "error", "info": "info"}
        for result in results or ():
            key = str(getattr(result, "level", "info"))
            key = getattr(getattr(result, "level", None), "value", key)
            colour = _colour(level.get(key, "info")).name()
            parts.append(
                f'<span style="color:{colour}">{symbol.get(key, "○")}</span> '
                f"{getattr(result, 'title', '')}"
            )
        self.target_lights_label.setText(" &nbsp; ".join(parts))
        self.target_lights_label.setVisible(bool(parts))
        self.target_card.setAccessibleDescription(
            "; ".join(f"{getattr(r, 'title', '')}: {getattr(r, 'message', '')}"
                      for r in results or ())
        )

    def set_notices(self, notices) -> None:
        self.notice_bar.set_notices(notices)

    # -- the older setters (strip and stopped blocks) ------------------------

    def _card(self, key: str) -> StatusCard:
        if key not in self.cards:
            raise KeyError(f"Unknown card: {key}")
        return self.cards[key]

    def set_runtime_state(self, value: Any, detail: Any | None = None) -> None:
        self._runtime_text = _stringify(value) or "Stopped"
        if self.snapshot is None or self.snapshot.state in (hs.STOPPED, hs.AFTER_FLIGHT):
            running = self._runtime_text.lower() == "running"
            if running:
                self.state_pill.setText("● Running")

    def set_readiness_state(self, value: Any, detail: Any | None = None) -> None:
        text = _stringify(value) or "Unknown"
        ready = text.lower() == "ready"
        # One line when ready; the list only when something is wrong.
        self.ready_card.set_state(
            "✓ Ready to fly" if ready else f"! {text}",
            detail=None if ready else detail,
        )
        self.ready_card.set_semantic_state("success" if ready else "warning")

    def set_xplane_state(self, value: Any, detail: Any | None = None) -> None:
        self._xplane_connected = (_stringify(value).lower() == "connected")

    def set_mounted_scenery(self, value: Any, detail: Any | None = None) -> None:
        self.scenery_card.set_state(value, detail=detail)
        self._packs_text = _stringify(detail) or _stringify(value)

    def set_provider(self, value: Any, detail: Any | None = None) -> None:
        self._provider = _stringify(value)

    def set_simbrief(self, value: Any, detail: Any | None = None) -> None:
        self._simbrief = _stringify(value)

    def set_cache(self, value: Any, detail: Any | None = None) -> None:
        self.storage_card.set_state(value, detail=detail)

    def set_task(self, value: Any, detail: Any | None = None) -> None:
        """Scenery installs: a badge on Scenery library, not a card."""
        text = _stringify(value)
        active = bool(text) and text.lower() != "idle"
        self._task_text = f"installing: {text}" if active else ""
        self.library_button.setText(f"Scenery library ({text})" if active else "Scenery library")
        if self.snapshot is not None:
            self._show_strip(self.snapshot)

    def set_throughput(self, value: Any, detail: Any | None = None) -> None:
        self.library_button.setToolTip(
            f"Scenery downloads: {_stringify(value)}" if value else ""
        )

    def set_recent_failure(self, value: Any, detail: Any | None = None) -> None:
        """Kept for callers; failures reach Home as notices."""
        self.library_button.setProperty("state", "error")
        repolish(self.library_button)

    def clear_recent_failure(self) -> None:
        self.library_button.setProperty("state", "")
        repolish(self.library_button)

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
                setter(kwargs[key], kwargs.get(f"{key}_detail"))


__all__ = ["HomePage", "StatusCard", "SparkLine", "GaugeCard", "TileRadar"]

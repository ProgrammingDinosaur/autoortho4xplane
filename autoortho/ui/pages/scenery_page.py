"""Scenery Library page: the AutoOrtho scenery set, and regions installed
with the old region installer.

Tiles are downloaded on the map (Flight Plan & Map, Scenery & Cache view),
one by one or a whole region at a time, so this page only holds the
scenery set's settings. Regions installed earlier still work and are listed
here with their patches and a Remove button, and can be moved into the
AutoOrtho scenery (see :mod:`region_adopt`).
"""

from dataclasses import dataclass
from typing import Iterable

from packaging import version
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.readiness import format_bytes
    from autoortho.ui.theme import repolish
    from autoortho.ui.widgets.stream_panel import StreamPanel
else:
    from ui.readiness import format_bytes
    from ui.theme import repolish
    from ui.widgets.stream_panel import StreamPanel


@dataclass(frozen=True)
class SceneryItem:
    region_id: str
    name: str
    latest_version: str
    installed_version: str = ""
    size_bytes: int = 0
    download_count: int = 0
    install_path: str = ""
    seasons_status: str = "Not applied"
    roughness_status: str = "Not applied"
    roughness_value: float | None = None

    @property
    def installed(self) -> bool:
        return bool(self.installed_version)

    @property
    def update_available(self) -> bool:
        if not self.installed or not self.latest_version:
            return False
        try:
            return version.parse(self.latest_version) > version.parse(
                self.installed_version
            )
        except Exception:
            return self.latest_version != self.installed_version


def _status_text(value) -> str:
    raw = value.value if hasattr(value, "value") else str(value or "")
    return {
        "applied": "Applied",
        "partially_applied": "Partial",
        "not_applied": "Not applied",
        "included": "Included",
    }.get(raw.lower(), raw.replace("_", " ").title() or "Not applied")


def item_from_region(region) -> SceneryItem | None:
    try:
        latest = region.get_latest_release()
        latest.parse()
    except Exception:
        latest = getattr(region, "local_rel", None)
    if latest is None:
        return None
    local = getattr(region, "local_rel", None)
    patch_source = local or latest
    return SceneryItem(
        region_id=str(region.region_id),
        name=str(getattr(latest, "name", region.region_id)),
        latest_version=str(getattr(latest, "ver", "")),
        installed_version=str(getattr(local, "ver", "")) if local else "",
        size_bytes=int(getattr(latest, "totalsize", 0) or 0),
        download_count=int(getattr(latest, "download_count", 0) or 0),
        install_path=str(getattr(local, "subfolder_dir", "")) if local else "",
        seasons_status=_status_text(
            getattr(patch_source, "seasons_apply_status", None)
        ),
        roughness_status=_status_text(
            getattr(patch_source, "roughness_apply_status", None)
        ),
        roughness_value=getattr(patch_source, "roughness_value", None),
    )


def _secondary(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("textRole", "secondary")
    return label


class SceneryCard(QFrame):
    """One installed region: its patches, Scenery Options and Remove."""

    uninstall_requested = Signal(str)
    options_requested = Signal(str)

    def __init__(self, item: SceneryItem, check=None, busy=False, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("SceneryCard")
        self.setAccessibleName(f"Installed region {item.name}")
        self.setProperty("state", "success")
        repolish(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)

        header = QHBoxLayout()
        title = QLabel(item.name)
        title.setProperty("textRole", "sectionTitle")
        header.addWidget(title)
        header.addStretch()
        if check is not None:
            size = format_bytes(check.size)
            tiles = f"{len(check.tiles):,} tiles · {size}"
        else:
            tiles = f"Version {item.installed_version}"
        header.addWidget(_secondary(tiles))
        layout.addLayout(header)

        roughness = item.roughness_status
        if item.roughness_value is not None:
            roughness += f" ({item.roughness_value:.1f})"
        layout.addWidget(_secondary(
            f"Seasons: {item.seasons_status} · Terrain reflectivity: {roughness}"))
        self.check_label = _secondary(
            "" if check is None else
            "Can move into AutoOrtho scenery." if check.movable else check.reason)
        self.check_label.setVisible(check is not None)
        layout.addWidget(self.check_label)

        actions = QHBoxLayout()
        actions.addStretch()
        self.options_button = QPushButton("Scenery Options")
        self.options_button.setAccessibleName(f"Open scenery options for {item.name}")
        self.options_button.clicked.connect(
            lambda: self.options_requested.emit(self.item.region_id))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.remove_button.setProperty("role", "destructive")
        self.remove_button.setAccessibleName(f"Remove {item.name}")
        self.remove_button.clicked.connect(
            lambda: self.uninstall_requested.emit(self.item.region_id))
        for button in (self.options_button, self.remove_button):
            button.setEnabled(not busy)
            if busy:
                button.setToolTip("Not available while AutoOrtho runs or a "
                                  "task for this region is running.")
            actions.addWidget(button)
        layout.addLayout(actions)


class SceneryLibraryPage(QWidget):
    uninstall_requested = Signal(str)
    options_requested = Signal(str)
    adopt_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[SceneryItem] = []
        self.tasks = []
        self.checks: dict = {}           # region id -> region_adopt.RegionCheck
        self.runtime_locked = False
        self.cards: list[SceneryCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(14, 12, 14, 12)

        title = QLabel("Scenery Library")
        title.setProperty("textRole", "pageTitle")
        root.addWidget(title)

        self.stream_panel = StreamPanel()
        root.addWidget(self.stream_panel)

        self.regions_group = QGroupBox("Installed regions")
        regions_layout = QVBoxLayout(self.regions_group)
        regions_layout.addWidget(_secondary(
            "Regions installed with earlier versions of AutoOrtho. They keep "
            "working and are used before AutoOrtho scenery where both have a "
            "tile. Moving them into AutoOrtho scenery keeps their files (nothing "
            "is downloaded again) and shows them on the map as downloaded tiles."))
        adopt_row = QHBoxLayout()
        self.adopt_label = _secondary("Checking the installed regions…")
        self.adopt_button = QPushButton("Move into AutoOrtho Scenery…")
        self.adopt_button.setAccessibleName("Move installed regions into AutoOrtho scenery")
        self.adopt_button.setEnabled(False)
        self.adopt_button.clicked.connect(self.adopt_requested)
        adopt_row.addWidget(self.adopt_label, 1)
        adopt_row.addWidget(self.adopt_button)
        regions_layout.addLayout(adopt_row)
        self.cards_layout = QVBoxLayout()
        regions_layout.addLayout(self.cards_layout)
        root.addWidget(self.regions_group)
        self.regions_group.hide()
        root.addStretch(1)

    # -- data -------------------------------------------------------------
    def set_regions(self, regions: Iterable, tasks=()) -> None:
        """Show the installed regions among ``regions`` (downloader.Region)."""
        self.items = [
            item for item in (item_from_region(region) for region in regions)
            if item is not None and item.installed
        ]
        self.tasks = list(tasks)
        self._rebuild()

    def set_adopt_checks(self, checks) -> None:
        """Show which installed regions can move (``region_adopt.check_regions``),
        or None while checking."""
        if checks is None:
            self.checks = {}
            self.adopt_label.setText("Checking the installed regions…")
            self.adopt_button.setEnabled(False)
            self._rebuild()
            return
        self.checks = {c.region_id: c for c in checks}
        movable = [c for c in checks if c.movable]
        if movable:
            tiles = sum(len(c.tiles) for c in movable)
            size = sum(c.size for c in movable)
            text = (f"{len(movable)} of {len(checks)} region"
                    f"{'s' if len(checks) != 1 else ''} can move "
                    f"({tiles:,} tiles, {format_bytes(size)}).")
        elif checks:
            text = "None of the installed regions can move into the scenery set " \
                   "you use; each region says why."
        else:
            text = ""
        self.adopt_label.setText(text)
        self._rebuild()

    def set_runtime_locked(self, locked: bool) -> None:
        locked = bool(locked)
        self.stream_panel.set_runtime_locked(locked)
        if locked == self.runtime_locked:
            return
        self.runtime_locked = locked
        self._rebuild()

    @property
    def movable_regions(self) -> list:
        return [c for c in self.checks.values() if c.movable]

    # -- view -------------------------------------------------------------
    def _busy_ids(self) -> set:
        active_packages = {
            task.package.lower()
            for task in self.tasks
            if not task.state.terminal and task.package
        }
        active_ids = {
            task.id.split(":")[-1].lower()
            for task in self.tasks
            if not task.state.terminal
        }
        return {
            item.region_id for item in self.items
            if self.runtime_locked
            or item.region_id.lower() in active_ids
            or item.name.lower() in active_packages
        }

    def _rebuild(self) -> None:
        for card in self.cards:
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self.cards = []
        busy = self._busy_ids()
        for item in sorted(self.items, key=lambda entry: entry.name.lower()):
            card = SceneryCard(item, self.checks.get(item.region_id),
                               busy=item.region_id in busy)
            card.uninstall_requested.connect(self.uninstall_requested)
            card.options_requested.connect(self.options_requested)
            self.cards_layout.addWidget(card)
            self.cards.append(card)
        self.regions_group.setVisible(bool(self.items))
        self.adopt_button.setEnabled(
            bool(self.movable_regions) and not self.runtime_locked and not busy)
        self.adopt_button.setToolTip(
            "Stop AutoOrtho and X-Plane first." if self.runtime_locked else "")

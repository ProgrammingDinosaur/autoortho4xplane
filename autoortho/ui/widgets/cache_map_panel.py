"""Cache view of the map: what AutoOrtho has stored per 1x1 degree tile.

Shows streamed base mesh and cached imagery on the shared MapCanvas, lets
the user select tiles (one by one, by box, or a whole region of the scenery
set), delete what is stored, and download tiles to keep them (their
scenery is stored now and pinned, so it works offline and is never removed
to make room). This replaces installing regions. The scanning and
file work lives in :mod:`cache_inventory`; this module is the toolbar and
the glue.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QToolButton,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho import cache_inventory as ci
    from autoortho.ui.readiness import format_bytes
    from autoortho.ui.task_models import TaskType
    from autoortho.ui.widgets.custom_map_editor import (
        CACHE_VIEW, PAN, PINNED_COLOR, SELECT, cell_label, cells_in_box)
else:
    import cache_inventory as ci
    from ui.readiness import format_bytes
    from ui.task_models import TaskType
    from ui.widgets.custom_map_editor import (
        CACHE_VIEW, PAN, PINNED_COLOR, SELECT, cell_label, cells_in_box)

log = logging.getLogger(__name__)

STATE_COLORS = {
    "mesh": "#4c8df6",      # streamed scenery only
    "imagery": "#e8a33d",   # imagery only
    "both": "#3fb27f",
}
STATE_LABELS = {
    "mesh": "Scenery",
    "imagery": "Imagery",
    "both": "Both",
}
# Upper bounds (bytes) and colours for "Colour by size", light to dark.
SIZE_BINS = [
    (100e6, "#fde79a", "Under 100 MB"),
    (500e6, "#fbb35a", "100–500 MB"),
    (2e9, "#f0743e", "0.5–2 GB"),
    (8e9, "#d43d51", "2–8 GB"),
    (float("inf"), "#8e1b5f", "Over 8 GB"),
]
BY_STATE, BY_SIZE = "state", "size"
ESTIMATE_ZOOMS = (16, 17, 18)
LOCKED_TIP = "Stop AutoOrtho and X-Plane to delete or download tiles."
RESCAN_AFTER = 30.0  # seconds before showing the view scans again


def size_color(total: int) -> tuple[str, str]:
    for limit, color, label in SIZE_BINS:
        if total < limit:
            return color, label
    return SIZE_BINS[-1][1], SIZE_BINS[-1][2]


def describe_cell(cell, usage: Optional["ci.CellUsage"]) -> str:
    """Hover text for one tile."""
    title = cell_label(*cell)
    if usage is None or (usage.total == 0 and not usage.pinned):
        return f"{title}\nNothing stored"
    lines = [title + ("  ·  downloaded (kept)" if usage.pinned else "")]
    if usage.has_mesh:
        parts = []
        if usage.dsf_bytes:
            parts.append(f"base mesh {format_bytes(usage.dsf_bytes)}")
        if usage.overlay_bytes:
            parts.append(f"overlay {format_bytes(usage.overlay_bytes)}")
        if usage.mask_files:
            parts.append(f"{usage.mask_files} water mask"
                         f"{'s' if usage.mask_files != 1 else ''} "
                         f"{format_bytes(usage.mask_bytes)}")
        lines.append(f"Scenery: {format_bytes(usage.mesh_bytes)}")
        lines.append("  " + ", ".join(parts))
    elif usage.pinned:
        lines.append("Scenery: not downloaded yet")
    if usage.has_imagery:
        detail = []
        if usage.chunk_files:
            detail.append(f"{usage.chunk_files:,} image chunks")
        if usage.dds_files:
            detail.append(f"{usage.dds_files:,} texture files")
        if usage.zooms:
            lo, hi = min(usage.zooms), max(usage.zooms)
            detail.append(f"ZL{lo}" if lo == hi else f"ZL{lo}–{hi}")
        if usage.maptypes:
            detail.append(", ".join(sorted(usage.maptypes)))
        lines.append(f"Imagery: {format_bytes(usage.imagery_bytes)}")
        lines.append("  " + " · ".join(detail))
    if usage.last_used:
        lines.append("Last used: " + datetime.fromtimestamp(usage.last_used)
                     .strftime("%Y-%m-%d %H:%M"))
    return "\n".join(lines)


class CachePanel(QWidget):
    """Toolbar and logic of the map's Cache view."""

    statusChanged = Signal(str)
    cacheChanged = Signal()           # files were deleted or downloaded
    _scanned = Signal(int, object)    # generation, CacheInventory or Exception
    _finished = Signal(object, object)  # callback, result
    _progress = Signal(int, int, int)   # done, total, bytes
    _regions_read = Signal(object)      # list of regions, or Exception

    def __init__(self, canvas, paths: Callable[[], tuple[str, str, str]],
                 task_manager=None, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._paths = paths
        self.task_manager = task_manager
        self.inventory: Optional[ci.CacheInventory] = None
        self.selection: set[tuple[int, int]] = set()
        self.locked = False
        self.busy = ""
        self.active = False
        self._generation = 0
        self._scanning = False
        self._scanned_at = 0.0
        self._cancel = threading.Event()
        self._message = ""
        self.mode = SELECT

        bar = QHBoxLayout(self)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for mode, label, tip in (
            (SELECT, "Select", "Click a tile to select it, drag to select "
             "a box. Ctrl or Shift adds to the selection; Esc clears it."),
            (PAN, "Pan", "Left-drag moves the map"),
        ):
            button = QToolButton()
            button.setText(label)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setProperty("chip", True)
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            button.toggled.connect(lambda on, m=mode: on and self._set_mode(m))
            bar.addWidget(button)

        colour_label = QLabel("Colour by")
        colour_label.setProperty("textRole", "secondary")
        bar.addWidget(colour_label)
        self.colour_combo = QComboBox()
        self.colour_combo.setAccessibleName("Colour tiles by")
        self.colour_combo.addItem("What's stored", BY_STATE)
        self.colour_combo.addItem("Size", BY_SIZE)
        self.colour_combo.currentIndexChanged.connect(self._paint_layer)
        bar.addWidget(self.colour_combo)

        self.regions_button = QToolButton()
        self.regions_button.setText("Regions")
        self.regions_button.setAccessibleName("Select a whole region of the scenery set")
        self.regions_button.setToolTip("Select every tile of a region, then Download")
        self.regions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.regions_menu = QMenu(self.regions_button)
        self.regions_button.setMenu(self.regions_menu)
        self.regions: list = []           # (region id, label, cells)
        bar.addWidget(self.regions_button)

        self.delete_button = QToolButton()
        self.delete_button.setText("Delete")
        self.delete_button.setAccessibleName("Delete stored data of the selected tiles")
        self.delete_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        delete_menu = QMenu(self.delete_button)
        self.delete_mesh_action = delete_menu.addAction("Scenery…")
        self.delete_mesh_action.triggered.connect(lambda: self.delete_selected("mesh"))
        self.delete_imagery_action = delete_menu.addAction("Imagery…")
        self.delete_imagery_action.triggered.connect(lambda: self.delete_selected("imagery"))
        self.delete_both_action = delete_menu.addAction("Both…")
        self.delete_both_action.triggered.connect(lambda: self.delete_selected("both"))
        self.delete_button.setMenu(delete_menu)
        bar.addWidget(self.delete_button)

        self.keep_button = QToolButton()
        self.keep_button.setText("Download")
        self.keep_button.setAccessibleName("Download the selected tiles and keep them")
        self.keep_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.keep_button.clicked.connect(self.keep_selected)
        keep_menu = QMenu(self.keep_button)
        self.keep_action = keep_menu.addAction("Download and Keep…")
        self.keep_action.triggered.connect(self.keep_selected)
        self.unkeep_action = keep_menu.addAction("Stop Keeping")
        self.unkeep_action.setToolTip(
            "The tiles stay on disk but may be removed to make room again")
        self.unkeep_action.triggered.connect(self.unkeep_selected)
        self.keep_button.setMenu(keep_menu)
        bar.addWidget(self.keep_button)
        bar.addStretch(1)

        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setAccessibleName("More cache actions")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more_button)
        self.select_visible_action = more.addAction("Select Stored Tiles in View")
        self.select_visible_action.triggered.connect(self.select_visible)
        self.clear_selection_action = more.addAction("Clear Selection")
        self.clear_selection_action.triggered.connect(self.clear_selection)
        self.fit_action = more.addAction("Zoom to Stored Tiles")
        self.fit_action.triggered.connect(self.fit_to_cache)
        more.addSeparator()
        self.rescan_action = more.addAction("Rescan")
        self.rescan_action.triggered.connect(self.refresh)
        self.more_button.setMenu(more)
        bar.addWidget(self.more_button)

        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), canvas)
        escape.setContext(Qt.ShortcutContext.WidgetShortcut)
        escape.activated.connect(lambda: self.active and self.clear_selection())

        self._regions_read.connect(self._on_regions)
        self._scanned.connect(self._on_scanned)
        self._finished.connect(self._call_back)
        self._progress.connect(self._on_progress)
        canvas.cellsPicked.connect(self._on_picked)
        self.mode_buttons[SELECT].setChecked(True)
        self._update_actions()

    # -- view ---------------------------------------------------------------
    def activate(self):
        """The Cache view was switched on."""
        self.active = True
        self.canvas.set_view(CACHE_VIEW)
        self.canvas.set_mode(self.mode)
        self.canvas.set_selection(self.selection)
        self._paint_layer()
        self.refresh_if_stale()
        self._emit_status()

    def deactivate(self):
        self.active = False

    def refresh_if_stale(self):
        if self.inventory is None or time.monotonic() - self._scanned_at > RESCAN_AFTER:
            self.refresh()

    def _set_mode(self, mode: str):
        self.mode = mode
        if self.active:
            self.canvas.set_mode(mode)
            self._emit_status()

    def set_runtime_locked(self, locked: bool):
        self.locked = bool(locked)
        self._update_actions()
        self._emit_status()

    # -- scanning -------------------------------------------------------------
    def paths(self) -> tuple[str, str, str]:
        try:
            ao_dir, cache_dir, custom = self._paths()
        except Exception:
            log.warning("Cache view: could not read the folders", exc_info=True)
            return "", "", ""
        return str(ao_dir or ""), str(cache_dir or ""), str(custom or "")

    def refresh(self):
        self._generation += 1
        generation = self._generation
        self._scanning = True
        self._emit_status()
        ao_dir, cache_dir, _ = self.paths()

        def work():
            try:
                result = ci.scan(ao_dir, cache_dir)
            except Exception as exc:  # reported in the status line
                log.warning("Cache view: scan failed", exc_info=True)
                result = exc
            self._scanned.emit(generation, result)
            self._read_regions(ao_dir)

        threading.Thread(target=work, name="cache-map-scan", daemon=True).start()

    def _read_regions(self, ao_dir: str):
        """Read the scenery set's regions (on the scan thread)."""
        try:
            regions = ci.stream_regions(ao_dir) if ao_dir else []
        except Exception as exc:
            log.warning("Cache view: could not read the regions", exc_info=True)
            regions = exc
        self._regions_read.emit(regions)

    def _on_regions(self, regions):
        self.regions_menu.clear()
        if isinstance(regions, Exception):
            regions = []
        self.regions = regions
        for rid, label, cells in regions:
            action = self.regions_menu.addAction(f"{label} ({len(cells):,} tiles)")
            action.triggered.connect(lambda _=False, r=rid: self.select_region(r))
        if not regions:
            empty = self.regions_menu.addAction("Choose a scenery set in the Scenery Library first")
            empty.setEnabled(False)

    def select_region(self, region_id: str):
        """Select every tile of a region and show it."""
        for rid, label, cells in self.regions:
            if rid == region_id:
                self.set_selection(cells)
                self.canvas.fit_cells(cells)
                self._message = f"{label} selected"
                self._emit_status()
                return

    def _on_scanned(self, generation: int, result):
        if generation != self._generation:
            return  # a newer scan is running
        self._scanning = False
        self._scanned_at = time.monotonic()
        if isinstance(result, Exception):
            self._message = f"Could not read the cache: {result}"
            self._emit_status()
            return
        self.inventory = result
        self._paint_layer()
        self._update_actions()
        self._emit_status()

    def _paint_layer(self, *_):
        inv = self.inventory
        if inv is None:
            self.canvas.set_cache_layer([], (), [])
            return
        by = self.colour_combo.currentData()
        groups: dict[str, list] = {}
        labels: dict[str, str] = {}
        for cell, usage in inv.cells.items():
            if usage.total == 0:
                continue
            if by == BY_SIZE:
                color, label = size_color(usage.total)
            else:
                color, label = STATE_COLORS[usage.state], STATE_LABELS[usage.state]
            groups.setdefault(color, []).append(cell)
            labels[color] = label
        if by == BY_SIZE:
            order = [c for _l, c, _t in SIZE_BINS]
        else:
            order = [STATE_COLORS[k] for k in ("mesh", "imagery", "both")]
        legend = [(c, labels[c]) for c in order if c in labels]
        pinned = [c for c, u in inv.cells.items() if u.pinned]
        if pinned:
            legend.append((f"outline:{PINNED_COLOR.name()}", "Downloaded (kept)"))
        self.canvas.set_cache_layer([(c, groups[c]) for c in order if c in groups],
                                    pinned, legend)

    def usage(self, cell) -> Optional["ci.CellUsage"]:
        return self.inventory.cells.get(cell) if self.inventory else None

    def hover_text(self, cell) -> str:
        if self.inventory is None:
            return f"{cell_label(*cell)}\nReading the cache…"
        return describe_cell(cell, self.usage(cell))

    # -- selection ------------------------------------------------------------
    def _on_picked(self, cells: list, additive: bool):
        if not self.active:
            return
        cells = set(cells)
        if not additive:
            selection = cells
        elif len(cells) == 1:
            selection = self.selection ^ cells
        else:
            selection = self.selection | cells
        self.set_selection(selection)

    def set_selection(self, cells: Iterable[tuple[int, int]]):
        self.selection = set(cells)
        self.canvas.set_selection(self.selection)
        self._message = ""
        self._update_actions()
        self._emit_status()

    def clear_selection(self):
        self.set_selection(())

    def stored_cells(self) -> list:
        if self.inventory is None:
            return []
        return [c for c, u in self.inventory.cells.items() if u.total or u.pinned]

    def select_visible(self):
        south, north, west, east = self.canvas.visible_bounds()
        stored = set(self.stored_cells())
        self.set_selection(c for c in cells_in_box(south, north, west, east)
                           if c in stored)

    def fit_to_cache(self):
        self.canvas.fit_cells(self.stored_cells())

    def selection_bytes(self, kind: str) -> int:
        total = 0
        for cell in self.selection:
            usage = self.usage(cell)
            if usage is None:
                continue
            if kind in ("mesh", "both"):
                total += usage.mesh_bytes
            if kind in ("imagery", "both"):
                total += usage.imagery_bytes
        return total

    # -- actions --------------------------------------------------------------
    def _can_change(self) -> bool:
        return not self.locked and not self.busy

    def _update_actions(self):
        has_selection = bool(self.selection)
        can_change = self._can_change() and has_selection
        tip = LOCKED_TIP if self.locked else ""
        for widget in (self.delete_button, self.keep_button):
            widget.setEnabled(can_change)
            widget.setToolTip(tip or (
                "Select tiles on the map first" if not has_selection else ""))
        if not self.locked and has_selection:
            self.delete_button.setToolTip("Delete what is stored for the selected tiles")
            self.keep_button.setToolTip(
                "Download the scenery of the selected tiles now and keep it: "
                "it works offline and is never removed to make room")
        self.delete_mesh_action.setEnabled(can_change and self.selection_bytes("mesh") > 0)
        self.delete_imagery_action.setEnabled(
            can_change and self.selection_bytes("imagery") > 0)
        self.delete_both_action.setEnabled(can_change and self.selection_bytes("both") > 0)
        pinned = any(u.pinned for u in (self.usage(c) for c in self.selection) if u)
        self.unkeep_action.setEnabled(can_change and pinned)
        self.clear_selection_action.setEnabled(has_selection)
        has_stored = bool(self.stored_cells())
        self.select_visible_action.setEnabled(has_stored)
        self.fit_action.setEnabled(has_stored)

    def _emit_status(self):
        parts = []
        inv = self.inventory
        if self.busy:
            parts.append(self.busy)
        elif self._scanning and inv is None:
            parts.append("Reading the cache…")
        if inv is not None:
            mesh_tiles = inv.mesh_cells
            parts.append(f"Scenery {format_bytes(inv.mesh_bytes)} in "
                         f"{mesh_tiles} tile{'s' if mesh_tiles != 1 else ''}"
                         + (f" ({inv.pinned_cells} downloaded)" if inv.pinned_cells else ""))
            parts.append(f"imagery {format_bytes(inv.imagery_bytes)} in "
                         f"{inv.imagery_cells} tile{'s' if inv.imagery_cells != 1 else ''}")
        if self.selection:
            count = len(self.selection)
            parts.append(f"{count} selected ({format_bytes(self.selection_bytes('both'))})")
        elif inv is not None and not self.busy:
            parts.append("click or drag to select tiles" if self.mode == SELECT
                         else "drag to pan")
        if self.locked:
            parts.append("stop AutoOrtho to delete or download tiles")
        if self._message:
            parts.append(self._message)
        self.statusChanged.emit(" · ".join(parts))

    def _run(self, work: Callable[[], object], done: Callable[[object], None]):
        """Run ``work`` off the UI thread and pass its result (or the
        exception it raised) to ``done`` on the UI thread."""
        def target():
            try:
                result = work()
            except Exception as exc:
                log.warning("Cache view: job failed", exc_info=True)
                result = exc
            self._finished.emit(done, result)

        threading.Thread(target=target, name="cache-map-job", daemon=True).start()

    def _call_back(self, callback, result):
        callback(result)  # bound method: queued onto the UI thread

    def _set_busy(self, text: str):
        self.busy = text
        self._update_actions()
        self._emit_status()

    def _confirm(self, title: str, text: str, detail: str, accept: str,
                 destructive: bool = True) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setInformativeText(detail)
        role = (QMessageBox.ButtonRole.DestructiveRole if destructive
                else QMessageBox.ButtonRole.AcceptRole)
        ok = box.addButton(accept, role)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is ok

    def delete_selected(self, kind: str):
        if not self._can_change() or not self.selection:
            return
        size = self.selection_bytes(kind)
        cells = sorted(self.selection)
        count = len(cells)
        tiles = f"{count} tile{'s' if count != 1 else ''}"
        if size == 0:
            self._message = "nothing of that kind is stored in the selection"
            self._emit_status()
            return
        what = {"mesh": "scenery", "imagery": "imagery",
                "both": "scenery and imagery"}[kind]
        details = []
        if kind in ("mesh", "both"):
            details.append("Scenery is downloaded again the next time you fly "
                           "there if downloading while flying is on, so it needs "
                           "internet then. Downloaded tiles stop being kept.")
        if kind in ("imagery", "both"):
            details.append("Imagery is downloaded again when X-Plane needs it.")
        if not self._confirm(f"Delete {what}?",
                             f"Delete {format_bytes(size)} of {what} from {tiles}?",
                             " ".join(details), f"Delete {format_bytes(size)}"):
            return
        ao_dir, cache_dir, _ = self.paths()
        mesh, imagery = kind in ("mesh", "both"), kind in ("imagery", "both")
        task_id = "cache-map-delete"
        if self.task_manager is not None:
            self.task_manager.create_task(task_id, TaskType.CACHE, f"Delete cached {what}",
                                          package=tiles, stage="Deleting files")
        self._set_busy(f"Deleting {what}…")

        def done(result):
            self.busy = ""
            if isinstance(result, Exception):
                self._message = f"Delete failed: {result}"
                if self.task_manager is not None:
                    self.task_manager.fail_task(task_id, str(result))
            else:
                self._message = f"deleted {format_bytes(result.bytes)} from {tiles}"
                if result.errors:
                    self._message += f" ({len(result.errors)} files could not be removed)"
                if self.task_manager is not None:
                    self.task_manager.complete_task(
                        task_id, stage=f"Deleted {format_bytes(result.bytes)}")
            self.cacheChanged.emit()
            self.refresh()
            self._update_actions()

        self._run(lambda: ci.delete_cells(ao_dir, cache_dir, cells,
                                          mesh=mesh, imagery=imagery), done)

    def unkeep_selected(self):
        if not self._can_change() or not self.selection:
            return
        ao_dir, _, _ = self.paths()
        try:
            ci.pin_cells(ao_dir, self.selection, pinned=False)
        except OSError as exc:
            self._message = f"Could not change kept tiles: {exc}"
        else:
            self._message = "the tiles are no longer kept and may be removed to make room"
        self.refresh()

    def keep_selected(self):
        if not self._can_change() or not self.selection:
            return
        if self.inventory is not None and not self.inventory.streaming:
            QMessageBox.information(
                self, "Download Tiles",
                "Choose a scenery set in the Scenery Library first.")
            return
        ao_dir, _, custom = self.paths()
        cells = sorted(self.selection)
        inventory = self.inventory
        self._set_busy("Working out the download size…")

        def estimate():
            mesh = ci.mesh_download_estimate(ao_dir, cells)
            imagery = ci.estimate_imagery(cells, ESTIMATE_ZOOMS, inventory)
            return mesh, imagery

        def confirmed(result):
            self.busy = ""
            self._update_actions()
            if isinstance(result, Exception):
                self._message = f"Could not read the tile index: {result}"
                self._emit_status()
                return
            self._emit_status()
            (size, files, covered), imagery = result
            count = len(cells)
            tiles = f"{count} tile{'s' if count != 1 else ''}"
            if not covered:
                QMessageBox.information(
                    self, "Download Tiles",
                    f"None of the {tiles} have scenery in the scenery set (open "
                    "water, or not covered).")
                return
            text = (f"Download about {format_bytes(size)} of scenery now "
                    f"({files} DSF{'s' if files != 1 else ''} not stored yet)?"
                    if files else "The scenery of these tiles is already "
                    "stored. Keep it?")
            details = ["Downloaded tiles work offline, are never removed to make "
                       "room and don't count toward the disk limit. Water masks "
                       "add a little to the download."]
            if covered < count:
                details.append(f"{count - covered} of the {tiles} have no "
                               "scenery (open water or not covered) and are skipped.")
            details.append("Imagery isn't downloaded. Caching it for these tiles "
                           "would take about " + ", ".join(
                               f"{format_bytes(b)} at ZL{z}" for z, (_n, b) in
                               sorted(imagery.items())) + ".")
            if self._confirm(f"Download {tiles}?", text,
                             "\n\n".join(details),
                             "Download and Keep" if files else "Keep",
                             destructive=False):
                self._start_keep(ao_dir, custom, cells, size)

        self._run(estimate, confirmed)

    def _start_keep(self, ao_dir: str, custom: str, cells: list, size: int):
        self._cancel = threading.Event()
        cancel = self._cancel
        count = len(cells)
        tiles = f"{count} tile{'s' if count != 1 else ''}"
        task_id = "cache-map-keep"
        self._keep_total = size
        if self.task_manager is not None:
            self.task_manager.create_task(
                task_id, TaskType.CACHE, "Download scenery tiles",
                package=tiles, stage="Downloading scenery",
                cancellable=True, cancel_callback=cancel.set)
            self.task_manager.update_task(task_id, progress=0.0, bytes_total=size)
        self._set_busy(f"Downloading scenery for {tiles}…")

        def done(result):
            self.busy = ""
            failed = isinstance(result, Exception)
            if failed:
                self._message = f"Download failed: {result}"
            else:
                self._message = (f"downloaded {count - len(result.missing)} tiles "
                                 f"({format_bytes(result.bytes)})")
                if result.errors:
                    self._message += f", {len(result.errors)} failed (try again later)"
            if self.task_manager is not None:
                if failed:
                    self.task_manager.fail_task(task_id, str(result))
                elif result.cancelled and cancel.is_set():
                    self.task_manager.mark_cancelled(task_id)
                elif result.errors:
                    self.task_manager.fail_task(
                        task_id, f"{len(result.errors)} tiles could not be "
                        f"downloaded: {result.errors[0]}")
                else:
                    self.task_manager.complete_task(
                        task_id, stage=f"Downloaded {format_bytes(result.bytes)}")
            self.cacheChanged.emit()
            self.refresh()
            self._update_actions()

        self._run(lambda: ci.keep_offline(
            ao_dir, cells, custom_scenery_dir=custom,
            progress=lambda d, t, b: self._progress.emit(d, t, b),
            cancelled=cancel.is_set), done)

    def _on_progress(self, done: int, total: int, size: int):
        self.busy = f"Downloading scenery: {done} of {total} tiles…"
        self._emit_status()
        if self.task_manager is not None:
            self.task_manager.update_task(
                "cache-map-keep", progress=100.0 * done / max(1, total),
                bytes_completed=size, stage=f"{done} of {total} tiles")

    def cancel(self):
        self._cancel.set()

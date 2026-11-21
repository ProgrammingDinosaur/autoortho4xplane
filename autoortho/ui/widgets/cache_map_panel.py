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
import os
import threading
import time
from datetime import datetime
from typing import Callable, Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho import cache_inventory as ci
    from autoortho import kept_imagery
    from autoortho.ui.readiness import format_bytes
    from autoortho.ui.task_models import TaskType
    from autoortho.ui.widgets.custom_map_editor import (
        CACHE_VIEW, PINNED_COLOR, SELECT, cell_label)
else:
    import cache_inventory as ci
    import kept_imagery
    from ui.readiness import format_bytes
    from ui.task_models import TaskType
    from ui.widgets.custom_map_editor import (
        CACHE_VIEW, PINNED_COLOR, SELECT, cell_label)

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
BY_STATE, BY_SIZE, BY_SET = "state", "size", "set"
# Colours for "Colour by: Scenery set", per set id; others get the last one.
SET_COLORS = {"xp12": "#3fa7d6", "xp11": "#c77dff"}
OTHER_SET_COLOR = "#9aa0a6"
ESTIMATE_ZOOMS = (16, 17, 18)
LOCKED_TIP = "Stop AutoOrtho and X-Plane to delete or download tiles."
RESCAN_AFTER = 30.0  # seconds before showing the view scans again


def size_color(total: int) -> tuple[str, str]:
    for limit, color, label in SIZE_BINS:
        if total < limit:
            return color, label
    return SIZE_BINS[-1][1], SIZE_BINS[-1][2]


def set_label(source: str) -> str:
    return f"{ci.tile_stream.short_label(source)} set" if source else ""


def describe_cell(cell, usage: Optional["ci.CellUsage"], current_set: str = "") -> str:
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
        scenery = f"Scenery: {format_bytes(usage.mesh_bytes)}"
        if usage.mesh_set:
            scenery += f"  ·  {set_label(usage.mesh_set)}"
            if current_set and usage.mesh_set != current_set:
                scenery += f" (you use the {set_label(current_set)})"
        lines.append(scenery)
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
        kept = f"  ·  kept up to ZL{usage.imagery_kept}" if usage.imagery_kept else ""
        lines.append(f"Imagery: {format_bytes(usage.imagery_bytes)}{kept}")
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
    _regions_read = Signal(object)      # (regions, covered tiles) or Exception
    _imagery_progress = Signal(int, int, int)  # chunks done, total, bytes

    def __init__(self, canvas, paths: Callable[[], tuple[str, str, str]],
                 task_manager=None, parent=None,
                 imagery_options: Optional[Callable[[], dict]] = None):
        super().__init__(parent)
        self.canvas = canvas
        self._paths = paths
        # {"override": maptype override, "custom_map": CustomMapConfig or None}
        self._imagery_options = imagery_options or (lambda: {})
        self.imagery_zoom: Optional[int] = None   # last choice in the dialog
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

        # One row, built around what can be done: pick tiles (on the map or
        # a whole region), then Download or Delete them. No modes: left
        # click/drag selects, right-drag pans, the wheel zooms.
        bar = QHBoxLayout(self)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)

        self.region_combo = QComboBox()
        self.region_combo.setAccessibleName("Select a whole region of the scenery set")
        self.region_combo.setToolTip("Selects every tile of a region, so you can "
                                     "download it in one go")
        self.region_combo.addItem("Select a region…", None)
        self.region_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.region_combo.activated.connect(self._region_picked)
        self.regions: list = []           # (region id, label, cells)
        # Tiles the scenery set has; None until read (or with no set).
        self.coverage: Optional[set] = None
        bar.addWidget(self.region_combo)

        self.keep_button = QPushButton("Download…")
        self.keep_button.setAccessibleName("Download the selected tiles and keep them")
        self.keep_button.clicked.connect(self.keep_selected)
        bar.addWidget(self.keep_button)

        self.delete_button = QPushButton("Delete…")
        self.delete_button.setAccessibleName("Delete what is stored for the selected tiles")
        self.delete_button.clicked.connect(lambda: self.delete_selected())
        bar.addWidget(self.delete_button)

        self.unkeep_button = QPushButton("Stop Keeping")
        self.unkeep_button.setAccessibleName("Stop keeping the selected tiles")
        self.unkeep_button.setToolTip(
            "The tiles stay on disk, but may be removed to make room again")
        self.unkeep_button.clicked.connect(self.unkeep_selected)
        bar.addWidget(self.unkeep_button)

        self.switch_button = QPushButton()
        self.switch_button.setAccessibleName(
            "Replace the selected tiles with the scenery set in use")
        self.switch_button.clicked.connect(self.switch_selected)
        bar.addWidget(self.switch_button)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setAccessibleName("Clear the selection")
        self.clear_button.setToolTip("Clear the selection (Esc)")
        self.clear_button.clicked.connect(self.clear_selection)
        bar.addWidget(self.clear_button)
        bar.addStretch(1)

        colour_label = QLabel("Colour by")
        colour_label.setProperty("textRole", "secondary")
        bar.addWidget(colour_label)
        self.colour_combo = QComboBox()
        self.colour_combo.setAccessibleName("Colour tiles by")
        self.colour_combo.addItem("What's stored", BY_STATE)
        self.colour_combo.addItem("Size", BY_SIZE)
        self.colour_combo.addItem("Scenery set", BY_SET)
        self.colour_combo.currentIndexChanged.connect(self._paint_layer)
        colour_label.setBuddy(self.colour_combo)
        bar.addWidget(self.colour_combo)

        self.fit_button = QPushButton("Show Stored")
        self.fit_button.setAccessibleName("Zoom the map to the tiles that are stored")
        self.fit_button.setToolTip("Zoom the map to the tiles that are stored")
        self.fit_button.clicked.connect(self.fit_to_cache)
        bar.addWidget(self.fit_button)

        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), canvas)
        escape.setContext(Qt.ShortcutContext.WidgetShortcut)
        escape.activated.connect(lambda: self.active and self.clear_selection())

        self._regions_read.connect(self._on_regions)
        self._scanned.connect(self._on_scanned)
        self._finished.connect(self._call_back)
        self._progress.connect(self._on_progress)
        self._imagery_progress.connect(self._on_imagery_progress)
        canvas.cellsPicked.connect(self._on_picked)
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
            result = ci.stream_coverage(ao_dir) if ao_dir else ([], None)
        except Exception as exc:
            log.warning("Cache view: could not read the regions", exc_info=True)
            result = exc
        self._regions_read.emit(result)

    def _on_regions(self, result):
        regions, coverage = ([], None) if isinstance(result, Exception) else result
        self.regions = regions
        self.coverage = set(coverage) if coverage is not None else None
        if self.selection:  # a set change may leave tiles it doesn't have
            self.set_selection(c for c in self.selection if self.selectable(c))
        combo = self.region_combo
        combo.blockSignals(True)
        combo.clear()
        if regions:
            combo.addItem("Select a region…", None)
            for rid, label, cells in regions:
                combo.addItem(f"{label} ({len(cells):,} tiles)", rid)
        else:
            combo.addItem("No regions: choose a scenery set first", None)
        combo.setCurrentIndex(0)
        combo.blockSignals(False)
        self._update_actions()

    def _region_picked(self, index: int):
        region_id = self.region_combo.itemData(index)
        self.region_combo.setCurrentIndex(0)  # a picker, not a setting
        if region_id:
            self.select_region(region_id)

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
            if by == BY_SET:
                if not usage.dsf_bytes:
                    continue        # imagery only: no scenery set
                color = SET_COLORS.get(usage.mesh_set, OTHER_SET_COLOR)
                label = set_label(usage.mesh_set) or "Other"
            elif by == BY_SIZE:
                color, label = size_color(usage.total)
            else:
                color, label = STATE_COLORS[usage.state], STATE_LABELS[usage.state]
            groups.setdefault(color, []).append(cell)
            labels[color] = label
        if by == BY_SET:
            order = list(dict.fromkeys(list(SET_COLORS.values()) + [OTHER_SET_COLOR]))
        elif by == BY_SIZE:
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
        if not self.selectable(cell):
            return (f"{cell_label(*cell)}\nNot in the scenery set (open water "
                    "or not covered)")
        return describe_cell(cell, self.usage(cell), self.inventory.ortho_source)

    # -- selection ------------------------------------------------------------
    def covered(self, cell) -> bool:
        """Whether the scenery set has the tile (True while unknown)."""
        return self.coverage is None or cell in self.coverage

    def selectable(self, cell) -> bool:
        """Tiles of the scenery set, and tiles with something stored (so it
        can be deleted). Open water and areas the set doesn't cover can't
        be selected: there would be nothing to download."""
        if self.covered(cell):
            return True
        usage = self.usage(cell)
        return usage is not None and bool(usage.total or usage.pinned)

    def _on_picked(self, cells: list, additive: bool):
        if not self.active:
            return
        picked = set(cells)
        cells = {c for c in picked if self.selectable(c)}
        left_out = len(picked) - len(cells)
        if not additive:
            selection = cells
        elif len(cells) == 1:
            selection = self.selection ^ cells
        else:
            selection = self.selection | cells
        self.set_selection(selection)
        if left_out:
            if len(picked) == 1:
                self._message = (f"{cell_label(*next(iter(picked)))} has no scenery "
                                 "in the scenery set (open water or not covered)")
            else:
                self._message = (f"{left_out:,} tile{'s' if left_out != 1 else ''} "
                                 "without scenery in the set left out")
            self._emit_status()

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
        stored = self.selection_bytes("both") > 0
        kept = any(u.pinned for u in (self.usage(c) for c in self.selection) if u)
        locked = LOCKED_TIP if self.locked else ""
        count = len(self.selection)
        tiles = f" {count:,} tile{'s' if count != 1 else ''}" if count else ""
        self.keep_button.setText(f"Download{tiles}…")
        self.delete_button.setText(f"Delete{tiles if stored else ''}…")
        self.keep_button.setEnabled(can_change)
        self.keep_button.setToolTip(locked or (
            "Download the scenery of the selected tiles (and their imagery, if "
            "you like) and keep it: it works offline and is never removed to "
            "make room" if has_selection else
            "Select tiles on the map, or a region, first"))
        self.delete_button.setEnabled(can_change and stored)
        self.delete_button.setToolTip(locked or (
            "Delete the scenery or imagery stored for the selected tiles"
            if stored else "Nothing is stored for the selected tiles"
            if has_selection else "Select stored tiles on the map first"))
        self.unkeep_button.setVisible(kept)
        other = (ci.other_set_cells(self.inventory, self.selection)
                 if self.inventory is not None else [])
        current = set_label(self.inventory.ortho_source) if self.inventory else ""
        self.switch_button.setVisible(bool(other))
        self.switch_button.setEnabled(self._can_change() and bool(other))
        if other:
            n = len(other)
            self.switch_button.setText(
                f"Use {ci.tile_stream.short_label(self.inventory.ortho_source)} "
                f"for {n:,} tile{'s' if n != 1 else ''}…")
            self.switch_button.setToolTip(locked or (
                f"Download the {current}'s version of the selected tiles that "
                "are from another set, and delete their old files"))
        self.unkeep_button.setEnabled(can_change and kept)
        self.clear_button.setVisible(has_selection)
        self.region_combo.setEnabled(bool(self.regions))
        self.fit_button.setEnabled(bool(self.stored_cells()))

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
            parts.append("click a tile or drag a box to select, or pick a region; "
                         "right-drag pans, the wheel zooms")
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

    def _ask_delete(self) -> Optional[str]:
        """Ask what to delete from the selection: "mesh", "imagery", "both",
        or None if cancelled."""
        mesh, imagery = self.selection_bytes("mesh"), self.selection_bytes("imagery")
        count = len(self.selection)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Delete from {count} tile{'s' if count != 1 else ''}")
        layout = QVBoxLayout(dialog)
        heading = QLabel("What do you want to delete?")
        layout.addWidget(heading)
        mesh_check = QCheckBox(f"&Scenery ({format_bytes(mesh)})")
        mesh_check.setEnabled(mesh > 0)
        mesh_check.setChecked(mesh > 0)
        layout.addWidget(mesh_check)
        layout.addWidget(self._note(
            "Downloaded again the next time you fly there if downloading while "
            "flying is on. Downloaded tiles stop being kept."))
        imagery_check = QCheckBox(f"&Imagery ({format_bytes(imagery)})")
        imagery_check.setEnabled(imagery > 0)
        imagery_check.setChecked(imagery > 0 and mesh == 0)
        layout.addWidget(imagery_check)
        layout.addWidget(self._note("Downloaded again when X-Plane needs it."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
        ok.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def update():
            size = ((mesh if mesh_check.isChecked() else 0)
                    + (imagery if imagery_check.isChecked() else 0))
            ok.setEnabled(size > 0)
            ok.setText(f"Delete {format_bytes(size)}" if size else "Delete")

        mesh_check.toggled.connect(update)
        imagery_check.toggled.connect(update)
        update()
        self._delete_dialog = dialog  # for tests
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        m, i = mesh_check.isChecked(), imagery_check.isChecked()
        return "both" if m and i else "mesh" if m else "imagery" if i else None

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setProperty("textRole", "secondary")
        label.setContentsMargins(22, 0, 0, 6)
        return label

    def delete_selected(self, kind: Optional[str] = None):
        """Delete what is stored for the selection; asks what (and to
        confirm) unless ``kind`` is given."""
        if not self._can_change() or not self.selection:
            return
        if kind is None:
            kind = self._ask_delete()
            if kind is None:
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

    def switch_selected(self):
        """Replace selected tiles of another set with the set in use: their
        old files go (with the terrain only they used), they stay kept, and
        the set in use downloads its version."""
        if not self._can_change() or self.inventory is None:
            return
        cells = ci.other_set_cells(self.inventory, self.selection)
        if not cells:
            return
        current = self.inventory.ortho_source
        label = set_label(current)
        olds = sorted({set_label(self.usage(c).mesh_set) for c in cells})
        n = len(cells)
        tiles = f"{n:,} tile{'s' if n != 1 else ''}"
        tile_mb = ci.tile_stream.SOURCES.get(current, {}).get("tile_mb")
        size = f" (about {format_bytes(n * tile_mb * 1e6)})" if tile_mb else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"Use the {label}")
        box.setText(f"Use the {label} for {tiles}?")
        box.setInformativeText(
            f"Their {' and '.join(olds)} files are deleted and the {label}'s "
            f"version downloads{size}. They stay downloaded (kept). Until a "
            "tile is done, X-Plane shows its default scenery there if "
            "downloading while flying is off.")
        ok = box.addButton(f"Use {label}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(ok)
        box.exec()
        if box.clickedButton() is not ok:
            return
        ao_dir, _cache, custom = self.paths()
        self._set_busy(f"Removing the old files of {tiles}…")

        def removed(result):
            self.busy = ""
            if isinstance(result, Exception):
                self._message = f"Could not switch the tiles: {result}"
                self._update_actions()
                self._emit_status()
                return
            # Imagery doesn't depend on the scenery set: it stays as it is.
            self._start_keep(ao_dir, custom, cells, int(n * (tile_mb or 0) * 1e6), None)

        self._run(lambda: ci.switch_to_current_set(ao_dir, cells), removed)

    def unkeep_selected(self):
        if not self._can_change() or not self.selection:
            return
        ao_dir, cache_dir, _ = self.paths()
        try:
            ci.pin_cells(ao_dir, self.selection, pinned=False)
            if cache_dir:
                kept_imagery.set_kept(cache_dir, self.selection, None)
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
            ok, zoom = self._ask_download(
                f"Download {tiles}?", text, "\n\n".join(details),
                "Download and Keep" if files else "Keep", imagery)
            if ok:
                self._start_keep(ao_dir, custom, cells, size, zoom)

        self._run(estimate, confirmed)

    def _ask_download(self, title: str, text: str, detail: str, accept: str,
                      imagery: dict) -> tuple[bool, Optional[int]]:
        """The Download confirmation, with a choice of imagery to download
        too. Returns (accepted, zoom or None)."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        heading = QLabel(text)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        info = QLabel(detail)
        info.setWordWrap(True)
        info.setProperty("textRole", "secondary")
        layout.addWidget(info)
        row = QHBoxLayout()
        label = QLabel("&Imagery")
        combo = QComboBox()
        combo.setAccessibleName("Imagery to download with the tiles")
        label.setBuddy(combo)
        combo.addItem("Don't download imagery", None)
        for zoom, (_count, size) in sorted(imagery.items()):
            combo.addItem(f"Up to ZL{zoom} (about {format_bytes(size)})", zoom)
        index = combo.findData(self.imagery_zoom)
        combo.setCurrentIndex(max(0, index))
        row.addWidget(label)
        row.addWidget(combo, 1)
        layout.addLayout(row)
        note = QLabel("Downloaded imagery is kept: the cache size limit doesn't "
                      "remove it. Use the zoom AutoOrtho flies with (Settings, "
                      "Max zoom); higher zooms are four times larger each.")
        note.setWordWrap(True)
        note.setProperty("textRole", "secondary")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.addButton(accept, QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._download_dialog = dialog  # for tests
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False, None
        self.imagery_zoom = combo.currentData()
        return True, self.imagery_zoom

    def _start_keep(self, ao_dir: str, custom: str, cells: list, size: int,
                    imagery_zoom: Optional[int] = None):
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
                imagery = getattr(result, "imagery", None)
                if imagery is not None:
                    self._message += (f", imagery {format_bytes(imagery.bytes)} in "
                                      f"{imagery.files:,} chunks")
                    if imagery.throttled:
                        self._message += f" (stopped: {imagery.throttled}; try again later)"
                    if imagery.unsupported:
                        self._message += (" (" + ", ".join(sorted(imagery.unsupported))
                                          + " imagery can't be downloaded ahead)")
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

        _, cache_dir, _ = self.paths()
        options = dict(self._imagery_options() or {})

        def work():
            result = ci.keep_offline(
                ao_dir, cells, custom_scenery_dir=custom,
                progress=lambda d, t, b: self._progress.emit(d, t, b),
                cancelled=cancel.is_set)
            if imagery_zoom and cache_dir and not result.cancelled:
                result.imagery = download_imagery(
                    ao_dir, cache_dir, cells, imagery_zoom, options,
                    progress=lambda d, t, b: self._imagery_progress.emit(d, t, b),
                    cancelled=cancel.is_set)
            return result

        self._run(work, done)

    def _on_progress(self, done: int, total: int, size: int):
        self.busy = f"Downloading scenery: {done} of {total} tiles…"
        self._emit_status()
        if self.task_manager is not None:
            self.task_manager.update_task(
                "cache-map-keep", progress=100.0 * done / max(1, total),
                bytes_completed=size, stage=f"{done} of {total} tiles")

    def _on_imagery_progress(self, done: int, total: int, size: int):
        self.busy = f"Downloading imagery: {done:,} of {total:,} chunks…"
        self._emit_status()
        if self.task_manager is not None:
            self.task_manager.update_task(
                "cache-map-keep", progress=100.0 * done / max(1, total),
                bytes_completed=size, stage=f"Imagery: {done:,} of {total:,} chunks")

    def cancel(self):
        self._cancel.set()


def download_imagery(ao_dir: str, cache_dir: str, cells: list, zoom: int,
                     options: dict, progress=None, cancelled=lambda: False):
    """Download the imagery of downloaded tiles at ``zoom`` and keep it.
    Returns a kept_imagery.DownloadResult with ``unsupported`` added."""
    root = None
    for name, marker in ci.tile_stream.stream_packages(ao_dir).items():
        if marker.get("kind", "ortho") == "ortho":
            root = os.path.join(ao_dir, name)
    result = kept_imagery.DownloadResult()
    result.unsupported = set()
    if root is None:
        return result
    resolve = kept_imagery.maptype_resolver(options.get("override", ""),
                                            options.get("custom_map"))
    plan = kept_imagery.plan(root, cache_dir, cells, zoom, resolve, cancelled)
    result.unsupported = plan.unsupported
    if plan.chunks and not cancelled():
        result = kept_imagery.download(plan.chunks, cache_dir, progress=progress,
                                       cancelled=cancelled)
        result.unsupported = plan.unsupported
    if not result.cancelled and not result.throttled and plan.tiles:
        kept_imagery.set_kept(cache_dir, plan.tiles, zoom)
    return result

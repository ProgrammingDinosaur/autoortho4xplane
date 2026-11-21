"""Native Custom Map editor: assign imagery sources to 1-degree tiles.

Replaces the browser-based Leaflet editor. Draws a bundled Natural Earth
world outline (no tile server, no API key, works offline) on a Web Mercator
canvas, overlays the scenery tiles found on disk and the user's per-cell
maptype assignments, and edits them through the CustomMapConfig object.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTransform,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

WORLD = 256.0  # Web Mercator world width/height in map units at zoom 0.
MAX_LAT = 85.05112878
MIN_ZOOM = 1.5
MAX_ZOOM = 10.0

MAPTYPE_COLORS = {
    "BI": "#4285F4",
    "NAIP": "#34A853",
    "EOX": "#FBBC04",
    "USGS": "#EA4335",
    "Firefly": "#FF6D01",
    "GO2": "#46BDC6",
    "ARC": "#9C4DCC",
    "YNDX": "#D81B60",
    "APPLE": "#B0BEC5",
}
FALLBACK_COLOR = "#888888"

OCEAN = QColor("#15202b")
LAND = QColor("#33393f")
COAST = QColor("#59626b")
BORDER = QColor("#4a5259")
GRID = QColor(255, 255, 255, 26)
AVAILABLE_FILL = QColor(200, 200, 200, 40)
AVAILABLE_EDGE = QColor(200, 200, 200, 150)
LABEL = QColor("#b0b0b0")

PAINT, ERASE, PAN, SELECT = "paint", "erase", "pan", "select"
SOURCES_VIEW, CACHE_VIEW = "sources", "cache"
SELECTION_COLOR = QColor("#4dd0e1")
PINNED_COLOR = QColor("#ffffff")
MAX_UNDO = 100


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------

def lon_to_x(lon: float) -> float:
    return (lon + 180.0) / 360.0 * WORLD


def lat_to_y(lat: float) -> float:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    phi = math.radians(lat)
    return (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) \
        / 2.0 * WORLD


def x_to_lon(x: float) -> float:
    return x / WORLD * 360.0 - 180.0


def y_to_lat(y: float) -> float:
    n = math.pi * (1.0 - 2.0 * y / WORLD)
    return math.degrees(math.atan(math.sinh(n)))


def wrap_lon(lon: int) -> int:
    return ((lon + 180) % 360) - 180


def cell_key(lat: int, lon: int) -> str:
    return f"{lat},{lon}"


def parse_key(key: str) -> Optional[tuple[int, int]]:
    try:
        lat, lon = key.split(",")
        return int(lat), int(lon)
    except (ValueError, AttributeError):
        return None


def cell_label(lat: int, lon: int) -> str:
    return f"{lat:+03d}{lon:+04d}"


def cell_rect(lat: int, lon: int) -> QRectF:
    x0 = lon_to_x(lon)
    x1 = lon_to_x(lon + 1)
    y0 = lat_to_y(lat + 1)
    y1 = lat_to_y(lat)
    return QRectF(x0, y0, x1 - x0, y1 - y0)


def cells_in_box(lat_a: float, lat_b: float,
                 lon_a: float, lon_b: float) -> list[tuple[int, int]]:
    """All 1-degree cells touched by a box; longitudes may be unwrapped."""
    lat_lo = max(-90, math.floor(min(lat_a, lat_b)))
    lat_hi = min(89, math.floor(max(lat_a, lat_b)))
    lon_lo = math.floor(min(lon_a, lon_b))
    lon_hi = math.floor(max(lon_a, lon_b))
    lon_hi = min(lon_hi, lon_lo + 359)
    cells = []
    for lat in range(lat_lo, lat_hi + 1):
        for lon in range(lon_lo, lon_hi + 1):
            cells.append((lat, wrap_lon(lon)))
    return cells


# ---------------------------------------------------------------------------
# World outline
# ---------------------------------------------------------------------------

def outline_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS) / "autoortho" / "data"
    else:
        root = Path(__file__).resolve().parent.parent.parent / "data"
    return root / "world_outline.json.gz"


_OUTLINE_CACHE: Optional[dict[str, QPainterPath]] = None


def _decode(flat: list[int], scale: float) -> list[tuple[float, float]]:
    points = []
    x = y = 0
    for i in range(0, len(flat) - 1, 2):
        if i == 0:
            x, y = flat[0], flat[1]
        else:
            x += flat[i]
            y += flat[i + 1]
        points.append((lon_to_x(x / scale), lat_to_y(y / scale)))
    return points


def _path(rings: Iterable[list[int]], scale: float, closed: bool) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for ring in rings:
        pts = _decode(ring, scale)
        if len(pts) < 2:
            continue
        path.moveTo(*pts[0])
        for px, py in pts[1:]:
            path.lineTo(px, py)
        if closed:
            path.closeSubpath()
    return path


def load_outline() -> dict[str, QPainterPath]:
    """World outline paths in Mercator map units, loaded once."""
    global _OUTLINE_CACHE
    if _OUTLINE_CACHE is not None:
        return _OUTLINE_CACHE
    paths = {
        "land": QPainterPath(),
        "lakes": QPainterPath(),
        "borders": QPainterPath(),
    }
    try:
        with gzip.open(outline_path(), "rt", encoding="ascii") as handle:
            data = json.load(handle)
        scale = float(data["scale"])
        paths["land"] = _path(data["land"], scale, True)
        paths["lakes"] = _path(data["lakes"], scale, True)
        paths["borders"] = _path(data["borders"], scale, False)
    except Exception:
        log.warning("Custom map: world outline unavailable", exc_info=True)
    _OUTLINE_CACHE = paths
    return paths


def swatch_icon(color: str, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

class MapCanvas(QWidget):
    """Mercator map that paints cells and reports drag boxes."""

    boxSelected = Signal(list)       # list[(lat, lon)]
    # Cache view: (cells, additive). A click selects one cell; Ctrl or
    # Shift adds to (or, for a single cell, toggles) the selection.
    cellsPicked = Signal(list, bool)
    hoverChanged = Signal(object)    # (lat, lon) or None
    viewChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Custom map")
        self.setAccessibleDescription(
            "Left-drag to paint or erase one-degree tiles. Right-drag or "
            "arrow keys pan; mouse wheel or plus and minus keys zoom."
        )
        self.zoom = 2.0
        self.center = QPointF(WORLD / 2, WORLD / 2 - 20)
        self.mode = PAINT
        self.outline = load_outline()
        self.available: set[tuple[int, int]] = set()
        self.cells: dict[tuple[int, int], str] = {}
        # Cell rectangles in map units, drawn with drawRects (much faster
        # than filling a QPainterPath with thousands of subpaths).
        self._available_rects: list[QRectF] = []
        self._cell_rects: dict[str, list[QRectF]] = {}
        self._drag_start: Optional[QPointF] = None
        self._drag_now: Optional[QPointF] = None
        self._pan_start: Optional[tuple[QPointF, QPointF]] = None
        self._hover: Optional[tuple[int, int]] = None
        # Cache view layer: colour -> rects, plus pinned and selected cells.
        self.view = SOURCES_VIEW
        self._cache_rects: list[tuple[QColor, list[QRectF]]] = []
        self._cache_legend: list[tuple[str, str]] = []
        self.pinned: set[tuple[int, int]] = set()
        self._pinned_rects: list[QRectF] = []
        self.selection: set[tuple[int, int]] = set()
        self._selection_rects: list[QRectF] = []
        self._update_cursor()

    # -- data -------------------------------------------------------------
    def set_available(self, cells: Iterable[tuple[int, int]]):
        self.available = set(cells)
        self._available_rects = [cell_rect(*c) for c in self.available]
        self.update()

    def set_cells(self, cells: dict[tuple[int, int], str]):
        self.cells = dict(cells)
        rects: dict[str, list[QRectF]] = {}
        for (lat, lon), maptype in self.cells.items():
            rects.setdefault(maptype, []).append(cell_rect(lat, lon))
        self._cell_rects = rects
        self.update()

    def set_mode(self, mode: str):
        self.mode = mode
        self._update_cursor()

    def set_view(self, view: str):
        self.view = view
        self.update()

    def set_cache_layer(self, groups: list[tuple[str, Iterable[tuple[int, int]]]],
                        pinned: Iterable[tuple[int, int]],
                        legend: list[tuple[str, str]]):
        """Cells to fill per colour in the cache view, and its legend."""
        layer = []
        for color, cells in groups:
            fill = QColor(color)
            fill.setAlpha(130)
            layer.append((fill, [cell_rect(*c) for c in cells]))
        self._cache_rects = layer
        self.pinned = set(pinned)
        self._pinned_rects = [cell_rect(*c) for c in self.pinned]
        self._cache_legend = list(legend)
        self.update()

    def set_selection(self, cells: Iterable[tuple[int, int]]):
        self.selection = set(cells)
        self._selection_rects = [cell_rect(*c) for c in self.selection]
        self.update()

    # -- view -------------------------------------------------------------
    @property
    def scale(self) -> float:
        return 2.0 ** self.zoom

    def to_screen(self, wx: float, wy: float) -> QPointF:
        s = self.scale
        return QPointF((wx - self.center.x()) * s + self.width() / 2,
                       (wy - self.center.y()) * s + self.height() / 2)

    def to_world(self, sx: float, sy: float) -> QPointF:
        s = self.scale
        return QPointF(self.center.x() + (sx - self.width() / 2) / s,
                       self.center.y() + (sy - self.height() / 2) / s)

    def lonlat_at(self, sx: float, sy: float) -> tuple[float, float]:
        w = self.to_world(sx, sy)
        return x_to_lon(w.x()), y_to_lat(min(max(w.y(), 0.0), WORLD))

    def cell_at(self, sx: float, sy: float) -> Optional[tuple[int, int]]:
        w = self.to_world(sx, sy)
        if not 0.0 <= w.y() <= WORLD:
            return None
        lon, lat = x_to_lon(w.x()), y_to_lat(w.y())
        return math.floor(lat), wrap_lon(math.floor(lon))

    def _clamp(self):
        half_h = self.height() / 2 / self.scale
        lo, hi = half_h, WORLD - half_h
        y = self.center.y()
        y = WORLD / 2 if lo > hi else min(max(y, lo), hi)
        x = self.center.x() % WORLD
        self.center = QPointF(x, y)

    def set_zoom(self, zoom: float, anchor: Optional[QPointF] = None):
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)
        before = self.to_world(anchor.x(), anchor.y())
        self.zoom = zoom
        after = self.to_world(anchor.x(), anchor.y())
        self.center += before - after
        self._clamp()
        self.update()
        self.viewChanged.emit()

    def pan_pixels(self, dx: float, dy: float):
        self.center -= QPointF(dx / self.scale, dy / self.scale)
        self._clamp()
        self.update()
        self.viewChanged.emit()

    def fit_cells(self, cells: Iterable[tuple[int, int]]):
        cells = list(cells)
        if not cells:
            self.zoom = 2.0
            self.center = QPointF(WORLD / 2, lat_to_y(25))
            self._clamp()
            self.update()
            return
        lats = [c[0] for c in cells]
        lons = [c[1] for c in cells]
        x0, x1 = lon_to_x(min(lons)), lon_to_x(max(lons) + 1)
        y0, y1 = lat_to_y(max(lats) + 1), lat_to_y(min(lats))
        w = max(self.width(), 360) * 0.9
        h = max(self.height(), 260) * 0.9
        span = max((x1 - x0) / w, (y1 - y0) / h, 1e-6)
        self.zoom = min(max(-math.log2(span), MIN_ZOOM), 8.0)
        self.center = QPointF((x0 + x1) / 2, (y0 + y1) / 2)
        self._clamp()
        self.update()

    def visible_bounds(self) -> tuple[float, float, float, float]:
        """(south, north, west, east); west/east are unwrapped."""
        west, north = self.lonlat_at(0, 0)
        east, south = self.lonlat_at(self.width(), self.height())
        return south, north, west, east

    # -- painting ---------------------------------------------------------
    def _offsets(self) -> list[float]:
        """World copies (in map units) needed to cover the viewport."""
        left = self.to_world(0, 0).x()
        right = self.to_world(self.width(), 0).x()
        first = math.floor(left / WORLD)
        last = math.floor(right / WORLD)
        return [k * WORLD for k in range(first, last + 1)]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), OCEAN)

        s = self.scale
        cell_px = WORLD / 360.0 * s
        offsets = self._offsets()
        base = QTransform(
            s, 0, 0, s,
            self.width() / 2 - self.center.x() * s,
            self.height() / 2 - self.center.y() * s,
        )

        coast = QPen(COAST, 1.0)
        coast.setCosmetic(True)
        border = QPen(BORDER, 1.0, Qt.PenStyle.DashLine)
        border.setCosmetic(True)

        # Antialiased fills of the 100k-point outline are ~25x slower than
        # aliased ones, so fill aliased and stroke the edges antialiased.
        for dx in offsets:
            painter.setTransform(QTransform().translate(dx, 0) * base)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(LAND)
            painter.drawPath(self.outline["land"])
            painter.setBrush(OCEAN)
            painter.drawPath(self.outline["lakes"])
            if self.view == CACHE_VIEW:
                for fill, rects in self._cache_rects:
                    painter.setBrush(fill)
                    painter.drawRects(rects)
            else:
                painter.setBrush(AVAILABLE_FILL)
                painter.drawRects(self._available_rects)
                for maptype, rects in self._cell_rects.items():
                    color = QColor(MAPTYPE_COLORS.get(maptype, FALLBACK_COLOR))
                    color.setAlpha(110)
                    painter.setBrush(color)
                    painter.drawRects(rects)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(coast)
            painter.drawPath(self.outline["land"])
            painter.drawPath(self.outline["lakes"])
            painter.setPen(border)
            painter.drawPath(self.outline["borders"])
            if self.view == CACHE_VIEW:
                self._paint_cache_marks(painter)
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        south, north, west, east = self.visible_bounds()
        if cell_px >= 12 and self.view != CACHE_VIEW:
            self._paint_cell_edges(painter, south, north, west, east)
        if cell_px >= 5:
            self._paint_grid(painter, south, north, west, east)
        self._paint_labels(painter, south, north, west, east, cell_px)
        self._paint_hover(painter)
        self._paint_selection(painter)
        self._paint_legend(painter)
        painter.end()

    def _screen_rect(self, lat: int, lon_unwrapped: int) -> QRectF:
        r = cell_rect(lat, lon_unwrapped)
        tl = self.to_screen(r.left(), r.top())
        br = self.to_screen(r.right(), r.bottom())
        return QRectF(tl, br)

    def _paint_cache_marks(self, painter):
        """Pinned outlines and the selection, in map units (current transform)."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if self._selection_rects:
            fill = QColor(SELECTION_COLOR)
            fill.setAlpha(70)
            pen = QPen(SELECTION_COLOR, 1.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(fill)
            painter.drawRects(self._selection_rects)
        if self._pinned_rects:
            pen = QPen(PINNED_COLOR, 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRects(self._pinned_rects)

    def _paint_cell_edges(self, painter, south, north, west, east):
        dashed = QPen(AVAILABLE_EDGE, 1.0, Qt.PenStyle.DashLine)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for lat in range(max(-90, math.floor(south)),
                         min(89, math.floor(north)) + 1):
            for lon in range(math.floor(west), math.floor(east) + 1):
                key = (lat, wrap_lon(lon))
                maptype = self.cells.get(key)
                if maptype is not None:
                    painter.setPen(QPen(QColor(
                        MAPTYPE_COLORS.get(maptype, FALLBACK_COLOR)), 1.2))
                elif key in self.available:
                    painter.setPen(dashed)
                else:
                    continue
                painter.drawRect(
                    self._screen_rect(lat, lon).adjusted(1, 1, -1, -1)
                )

    def _paint_grid(self, painter, south, north, west, east):
        painter.setPen(QPen(GRID, 1.0))
        for lat in range(max(-85, math.floor(south)),
                         min(85, math.ceil(north)) + 1):
            y = self.to_screen(0, lat_to_y(lat)).y()
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
        for lon in range(math.floor(west), math.ceil(east) + 1):
            x = self.to_screen(lon_to_x(lon), 0).x()
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))

    def _paint_labels(self, painter, south, north, west, east, cell_px):
        step = 1
        for candidate in (1, 2, 5, 10, 15, 30, 45, 90):
            step = candidate
            if candidate * cell_px >= 56:
                break
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        painter.setFont(font)
        painter.setPen(LABEL)
        lat = math.ceil(max(-80, south) / step) * step
        while lat <= min(80, north):
            y = self.to_screen(0, lat_to_y(lat)).y()
            painter.drawText(QPointF(4, y - 3),
                             f"{abs(lat)}°{'N' if lat >= 0 else 'S'}")
            lat += step
        lon = math.ceil(west / step) * step
        while lon <= east:
            x = self.to_screen(lon_to_x(lon), 0).x()
            w = wrap_lon(int(lon))
            text = f"{abs(w)}°{'E' if w >= 0 else 'W'}" if w not in (0, -180) \
                else ("0°" if w == 0 else "180°")
            painter.drawText(QPointF(x + 3, self.height() - 5), text)
            lon += step

    def _paint_legend(self, painter):
        """Compact key for the sources in use, top-right corner."""
        if self.view == CACHE_VIEW:
            items = list(self._cache_legend)
        else:
            items = [(MAPTYPE_COLORS.get(m, FALLBACK_COLOR), m)
                     for m in sorted(set(self.cells.values()))]
            if self.available:
                items.append(("#6a6f74", "Installed (default source)"))
        if not items:
            return
        metrics = painter.fontMetrics()
        row = metrics.height() + 4
        width = max(metrics.horizontalAdvance(t) for _c, t in items) + 30
        height = row * len(items) + 8
        box = QRectF(self.width() - width - 8, 8, width, height)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 24, 30, 215))
        painter.drawRoundedRect(box, 6, 6)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        y = box.top() + 4
        for color, text in items:
            swatch = QRectF(box.left() + 8, y + (row - 10) / 2, 10, 10)
            if color.startswith("outline:"):  # an outline, not a fill
                painter.setPen(QPen(QColor(color.split(":", 1)[1]), 2.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(swatch.adjusted(1, 1, -1, -1))
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawRect(swatch)
            painter.setPen(LABEL)
            painter.drawText(QPointF(swatch.right() + 8,
                                     y + row / 2 + metrics.ascent() / 2 - 1),
                             text)
            y += row

    def _paint_hover(self, painter):
        if self._hover is None or self._drag_start is not None:
            return
        lat, lon = self._hover
        w = self.to_world(self.width() / 2, 0).x()
        # Choose the copy of the cell nearest the view centre.
        base_lon = lon + 360 * round((x_to_lon(w) - lon) / 360)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self._screen_rect(lat, base_lon))

    def _paint_selection(self, painter):
        if self._drag_start is None or self._drag_now is None:
            return
        rect = QRectF(self._drag_start, self._drag_now).normalized()
        if self.mode == ERASE:
            color = QColor("#dd7373")
        elif self.mode == SELECT:
            color = QColor(SELECTION_COLOR)
        else:
            color = QColor("#ffffff")
        painter.setPen(QPen(color, 1.5, Qt.PenStyle.DashLine))
        fill = QColor(color)
        fill.setAlpha(28)
        painter.setBrush(fill)
        painter.drawRect(rect)

    # -- interaction ------------------------------------------------------
    def _update_cursor(self):
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if self.mode == PAN
            else Qt.CursorShape.CrossCursor
        )

    def mousePressEvent(self, event):
        pos = event.position()
        pans = (
            event.button() in (Qt.MouseButton.RightButton,
                               Qt.MouseButton.MiddleButton)
            or (event.button() == Qt.MouseButton.LeftButton
                and self.mode == PAN)
        )
        if pans:
            self._pan_start = (pos, QPointF(self.center))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = pos
            self._drag_now = pos
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._pan_start is not None:
            start, center = self._pan_start
            delta = pos - start
            self.center = center - QPointF(delta.x() / self.scale,
                                           delta.y() / self.scale)
            self._clamp()
            self.update()
            self.viewChanged.emit()
            return
        if self._drag_start is not None:
            self._drag_now = pos
        cell = self.cell_at(pos.x(), pos.y())
        if cell != self._hover:
            self._hover = cell
            self.hoverChanged.emit(cell)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._pan_start is not None:
            self._pan_start = None
            self._update_cursor()
            return
        if self._drag_start is None:
            return
        start, end = self._drag_start, event.position()
        self._drag_start = self._drag_now = None
        a = self.to_world(start.x(), start.y())
        b = self.to_world(end.x(), end.y())
        ya = min(max(a.y(), 0.0), WORLD)
        yb = min(max(b.y(), 0.0), WORLD)
        cells = cells_in_box(y_to_lat(ya), y_to_lat(yb),
                             x_to_lon(a.x()), x_to_lon(b.x()))
        self.update()
        if self.mode == SELECT:
            additive = bool(event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier))
            self.cellsPicked.emit(cells, additive)
        else:
            self.boxSelected.emit(cells)

    def leaveEvent(self, _event):
        if self._hover is not None:
            self._hover = None
            self.hoverChanged.emit(None)
            self.update()

    def wheelEvent(self, event):
        pixel = event.pixelDelta()
        modifiers = event.modifiers()
        trackpad_scroll = (
            not pixel.isNull()
            and not modifiers & Qt.KeyboardModifier.ControlModifier
            and sys.platform == "darwin"
        )
        if trackpad_scroll:
            self.pan_pixels(pixel.x(), pixel.y())
        else:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.set_zoom(self.zoom + steps * 0.5, event.position())
        event.accept()

    def event(self, event):
        if event.type() == event.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.set_zoom(self.zoom + event.value() * 2.0,
                              event.position())
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        key = event.key()
        step = 80
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.set_zoom(self.zoom + 0.5)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.set_zoom(self.zoom - 0.5)
        elif key == Qt.Key.Key_Left:
            self.pan_pixels(step, 0)
        elif key == Qt.Key.Key_Right:
            self.pan_pixels(-step, 0)
        elif key == Qt.Key.Key_Up:
            self.pan_pixels(0, step)
        elif key == Qt.Key.Key_Down:
            self.pan_pixels(0, -step)
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp()

    def sizeHint(self):
        return QSize(720, 480)


# ---------------------------------------------------------------------------
# Editor (toolbar + canvas + status)
# ---------------------------------------------------------------------------

class CustomMapEditor(QWidget):
    """Toolbar, map canvas and status line around a CustomMapConfig."""

    cellsChanged = Signal()
    viewChanged = Signal(str)   # SOURCES_VIEW or CACHE_VIEW
    cacheChanged = Signal()     # the Cache view deleted or downloaded files
    _tilesLoaded = Signal(list)

    def __init__(
        self,
        config,
        maptypes: list[str],
        scenery_path: Callable[[], str],
        discover: Callable[[str], list[str]],
        parent=None,
        cache_paths: Optional[Callable[[], tuple[str, str, str]]] = None,
        task_manager=None,
        imagery_options: Optional[Callable[[], dict]] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.maptypes = list(maptypes) or list(MAPTYPE_COLORS)
        self._scenery_path = scenery_path
        self._discover = discover
        self._undo: list[tuple[dict, dict]] = []
        self._redo: list[tuple[dict, dict]] = []
        self._tiles_loaded = False
        self._scanning = False
        self._hover = None
        self._fitted = False
        self.view = SOURCES_VIEW
        self.cache_panel = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # View toggle (only when the cache view can be offered).
        self.view_buttons = {}
        if cache_paths is not None:
            toggle = QHBoxLayout()
            toggle.setSpacing(0)
            self.view_group = QButtonGroup(self)
            self.view_group.setExclusive(True)
            for view, label, tip in (
                (SOURCES_VIEW, "Imagery Sources",
                 "Choose the imagery source per tile"),
                (CACHE_VIEW, "Scenery && Cache",  # && shows one &
                 "Download scenery tiles or regions, and see or delete what "
                 "AutoOrtho has stored per tile"),
            ):
                button = QToolButton()
                button.setText(label)
                button.setToolTip(tip)
                button.setCheckable(True)
                button.setProperty("chip", True)
                self.view_group.addButton(button)
                self.view_buttons[view] = button
                button.toggled.connect(
                    lambda on, v=view: on and self.set_view(v)
                )
                toggle.addWidget(button)
            toggle.addStretch(1)
            root.addLayout(toggle)

        self.sources_bar = QWidget()
        bar = QHBoxLayout(self.sources_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.maptype_combo = QComboBox()
        self.maptype_combo.setToolTip("Imagery source to paint")
        self.maptype_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.maptype_combo.setMinimumContentsLength(7)
        self.maptype_combo.setAccessibleName("Imagery source to paint")
        for maptype in self.maptypes:
            self.maptype_combo.addItem(
                swatch_icon(MAPTYPE_COLORS.get(maptype, FALLBACK_COLOR)),
                maptype,
            )
        self.maptype_combo.currentTextChanged.connect(self._refresh_status)
        bar.addWidget(self.maptype_combo)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for mode, label, tip in (
            (PAINT, "Paint", "Left-drag paints tiles with the source"),
            (ERASE, "Erase", "Left-drag returns tiles to the default source"),
            (PAN, "Pan", "Left-drag moves the map"),
        ):
            button = QToolButton()
            button.setText(label)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setProperty("chip", True)  # themed checked state
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            button.toggled.connect(
                lambda on, m=mode: on and self._set_mode(m)
            )
            bar.addWidget(button)

        self.undo_button = QToolButton()
        self.undo_button.setText("Undo")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QToolButton()
        self.redo_button.setText("Redo")
        self.redo_button.clicked.connect(self.redo)
        bar.addWidget(self.undo_button)
        bar.addWidget(self.redo_button)
        bar.addStretch(1)

        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setAccessibleName("More map actions")
        self.more_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        menu = QMenu(self.more_button)
        self.fill_action = menu.addAction("Fill Visible Tiles")
        self.fill_action.setToolTip(
            "Paint every installed tile in view with the selected source"
        )
        self.fill_action.triggered.connect(self.fill_visible)
        self.fit_action = menu.addAction("Zoom to Installed Scenery")
        self.fit_action.triggered.connect(self.fit_to_scenery)
        menu.addSeparator()
        self.import_action = menu.addAction("Import…")
        self.import_action.triggered.connect(self.import_map)
        self.export_action = menu.addAction("Export…")
        self.export_action.triggered.connect(self.export_map)
        self.rescan_action = menu.addAction("Rescan Installed Scenery")
        self.rescan_action.triggered.connect(self.refresh_tiles)
        menu.addSeparator()
        self.clear_action = menu.addAction("Clear All…")
        self.clear_action.triggered.connect(self.clear_all)
        self.more_button.setMenu(menu)
        bar.addWidget(self.more_button)

        self.canvas = MapCanvas(self)
        self.canvas.boxSelected.connect(self._apply_box)
        self.canvas.hoverChanged.connect(self._on_hover)

        self.toolbars = QStackedWidget()
        self.toolbars.addWidget(self.sources_bar)
        if cache_paths is not None:
            if __package__ and __package__.startswith("autoortho."):
                from autoortho.ui.widgets.cache_map_panel import CachePanel
            else:
                from ui.widgets.cache_map_panel import CachePanel
            self.cache_panel = CachePanel(self.canvas, cache_paths,
                                          task_manager=task_manager,
                                          imagery_options=imagery_options)
            self.cache_panel.statusChanged.connect(self._on_cache_status)
            self.cache_panel.cacheChanged.connect(self.cacheChanged)
            self.toolbars.addWidget(self.cache_panel)
        self.toolbars.setSizePolicy(QSizePolicy.Policy.Preferred,
                                    QSizePolicy.Policy.Maximum)
        root.addWidget(self.toolbars)
        root.addWidget(self.canvas, 1)

        self.status = QLabel("Looking for installed scenery tiles…")
        self.status.setProperty("textRole", "secondary")
        self.status.setAccessibleName("Custom map status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        for seq, slot in (
            (QKeySequence.StandardKey.Undo, self.undo),
            (QKeySequence.StandardKey.Redo, self.redo),
            (QKeySequence("Ctrl+Y"), self.redo),
        ):
            shortcut = QShortcut(seq, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        self._tilesLoaded.connect(self._on_tiles_loaded)
        self.mode_buttons[PAINT].setChecked(True)
        if self.view_buttons:
            self.view_buttons[SOURCES_VIEW].setChecked(True)
        self.reload_cells()
        self._update_buttons()

    # -- views ------------------------------------------------------------
    def set_view(self, view: str):
        """Switch between the imagery-source editor and the Cache view."""
        if view == CACHE_VIEW and self.cache_panel is None:
            return
        if view == self.view:
            return
        self.view = view
        button = self.view_buttons.get(view)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        QToolTip.hideText()
        if view == CACHE_VIEW:
            self.toolbars.setCurrentWidget(self.cache_panel)
            self.cache_panel.activate()
        else:
            if self.cache_panel is not None:
                self.cache_panel.deactivate()
            self.toolbars.setCurrentWidget(self.sources_bar)
            self.canvas.set_view(SOURCES_VIEW)
            self.canvas.set_mode(self._sources_mode())
            self._refresh_status()
        self.viewChanged.emit(view)

    def _sources_mode(self) -> str:
        for mode, button in self.mode_buttons.items():
            if button.isChecked():
                return mode
        return PAINT

    def set_runtime_locked(self, locked: bool):
        """Cache changes are only allowed while AutoOrtho is stopped."""
        if self.cache_panel is not None:
            self.cache_panel.set_runtime_locked(locked)

    def _on_cache_status(self, text: str):
        if self.view == CACHE_VIEW:
            self.status.setText(text)

    # -- data -------------------------------------------------------------
    def reload_cells(self):
        cells = {}
        for key, maptype in self.config.get_all_cells().items():
            parsed = parse_key(key)
            if parsed is not None:
                cells[parsed] = maptype
        self.canvas.set_cells(cells)
        self._refresh_status()

    def refresh_tiles(self):
        if self._scanning:
            return
        self._scanning = True
        self.rescan_action.setEnabled(False)
        self.status.setText("Looking for installed scenery tiles…")
        try:
            path = self._scenery_path()
        except Exception:
            path = ""

        def work():
            try:
                keys = self._discover(path) if path else []
            except Exception:
                log.warning("Custom map: tile scan failed", exc_info=True)
                keys = []
            self._tilesLoaded.emit(list(keys))

        threading.Thread(target=work, name="custom-map-scan",
                         daemon=True).start()

    def _on_tiles_loaded(self, keys: list):
        self._scanning = False
        self._tiles_loaded = True
        self.rescan_action.setEnabled(True)
        cells = [c for c in (parse_key(k) for k in keys) if c is not None]
        self.canvas.set_available(cells)
        if not self._fitted:
            self._fitted = True
            self.canvas.fit_cells(cells or list(self.canvas.cells))
        self._update_buttons()
        self._refresh_status()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._tiles_loaded and not self._scanning:
            self.refresh_tiles()
        if self.view == CACHE_VIEW:
            self.cache_panel.refresh_if_stale()

    # -- editing ----------------------------------------------------------
    def _set_mode(self, mode: str):
        if self.view != SOURCES_VIEW:
            return
        self.canvas.set_mode(mode)
        self._refresh_status()

    def _apply(self, after: dict[tuple[int, int], Optional[str]],
               record: bool = True):
        """Apply {cell: maptype or None}; persist, redraw and record undo."""
        current = self.canvas.cells
        before = {c: current.get(c) for c in after}
        if before == after:
            return
        to_set = {cell_key(*c): m for c, m in after.items() if m is not None}
        to_remove = [cell_key(*c) for c, m in after.items() if m is None]
        if to_set:
            self.config.set_cells(to_set)
        if to_remove:
            self.config.remove_cells(to_remove)
        cells = dict(current)
        for c, m in after.items():
            if m is None:
                cells.pop(c, None)
            else:
                cells[c] = m
        self.canvas.set_cells(cells)
        if record:
            self._undo.append((before, dict(after)))
            del self._undo[:-MAX_UNDO]
            self._redo.clear()
        self._update_buttons()
        self._refresh_status()
        self.cellsChanged.emit()

    def _apply_box(self, cells: list):
        if self.view != SOURCES_VIEW:
            return
        cells = [c for c in cells if c in self.canvas.available]
        if not cells:
            return
        if self.canvas.mode == ERASE:
            self._apply({c: None for c in cells})
        elif self.canvas.mode == PAINT:
            maptype = self.maptype_combo.currentText()
            self._apply({c: maptype for c in cells})

    def undo(self):
        if not self._undo or self.view != SOURCES_VIEW:
            return
        before, after = self._undo.pop()
        self._redo.append((before, after))
        self._apply(before, record=False)

    def redo(self):
        if not self._redo or self.view != SOURCES_VIEW:
            return
        before, after = self._redo.pop()
        self._undo.append((before, after))
        self._apply(after, record=False)

    def fit_to_scenery(self):
        self.canvas.fit_cells(self.canvas.available)

    def fill_visible(self):
        south, north, west, east = self.canvas.visible_bounds()
        cells = [c for c in cells_in_box(south, north, west, east)
                 if c in self.canvas.available]
        maptype = self.maptype_combo.currentText()
        if not cells:
            self.status.setText("No installed scenery tiles in view.")
            return
        answer = QMessageBox.question(
            self, "Fill Visible",
            f"Paint {len(cells)} visible tile"
            f"{'s' if len(cells) != 1 else ''} with {maptype}?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply({c: maptype for c in cells})

    def clear_all(self):
        if not self.canvas.cells:
            return
        answer = QMessageBox.question(
            self, "Clear All",
            f"Remove all {len(self.canvas.cells)} tile assignments? "
            "You can undo this.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply({c: None for c in self.canvas.cells})

    def import_map(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Custom Map", "", "Custom map (*.json);;All files (*)"
        )
        if not path:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Import Custom Map")
        box.setText("Merge the imported tiles with your current map, or "
                    "replace it?")
        merge = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        replace = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (merge, replace):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                self.config.import_json(handle.read(), merge=clicked is merge)
        except Exception as exc:
            QMessageBox.warning(self, "Import Custom Map",
                                f"Could not import {Path(path).name}:\n{exc}")
            return
        self._undo.clear()
        self._redo.clear()
        self.reload_cells()
        self._update_buttons()
        self.cellsChanged.emit()

    def export_map(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Custom Map", "custom_map.json",
            "Custom map (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.config.export_json())
        except OSError as exc:
            QMessageBox.warning(self, "Export Custom Map",
                                f"Could not save the file:\n{exc}")
            return
        self.status.setText(f"Exported to {path}")

    # -- status -----------------------------------------------------------
    def _on_hover(self, cell):
        self._hover = cell
        if self.view == CACHE_VIEW:
            if cell is None or self.canvas._drag_start is not None:
                QToolTip.hideText()
            else:
                QToolTip.showText(QCursor.pos(),
                                  self.cache_panel.hover_text(cell), self.canvas)
            return
        self._refresh_status()

    def _update_buttons(self):
        has_tiles = bool(self.canvas.available)
        self.undo_button.setEnabled(bool(self._undo))
        self.redo_button.setEnabled(bool(self._redo))
        self.fill_action.setEnabled(has_tiles)
        self.fit_action.setEnabled(has_tiles)
        self.clear_action.setEnabled(bool(self.canvas.cells))

    def _refresh_status(self, *_):
        if self.view != SOURCES_VIEW:
            return
        count = len(self.canvas.cells)
        parts = [f"{count} tile{'s' if count != 1 else ''} assigned"]
        if not self._tiles_loaded:
            parts.append("looking for installed scenery…")
        elif not self.canvas.available:
            parts.append("no installed AutoOrtho scenery found, so there "
                         "is nothing to paint yet")
        else:
            parts.append(f"{len(self.canvas.available)} installed")
            mode = self.canvas.mode
            if mode == PAINT:
                parts.append(f"drag to paint {self.maptype_combo.currentText()}")
            elif mode == ERASE:
                parts.append("drag to erase")
            else:
                parts.append("drag to pan")
            parts.append("right-drag pans, wheel zooms")
        if self._hover is not None:
            lat, lon = self._hover
            source = self.canvas.cells.get(self._hover)
            if source is None:
                source = ("default source" if self._hover in
                          self.canvas.available else "not installed")
            parts.append(f"{cell_label(lat, lon)}: {source}")
        self.status.setText(" · ".join(parts))

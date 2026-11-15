import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import QPointF

from ui.widgets import custom_map_editor as cme
from ui.widgets.custom_map_editor import CustomMapEditor
from utils.custom_map import CustomMapConfig

MAPTYPES = ["BI", "NAIP", "EOX"]
TILES = [f"{lat},{lon}" for lat in range(30, 40) for lon in range(-120, -110)]


@pytest.fixture
def editor(qt_app, tmp_path):
    config = CustomMapConfig(str(tmp_path / "custom_map.json"))
    widget = CustomMapEditor(
        config, MAPTYPES, scenery_path=lambda: str(tmp_path),
        discover=lambda _path: TILES,
    )
    widget.resize(900, 600)
    widget._on_tiles_loaded(TILES)
    yield widget
    widget.deleteLater()


def test_projection_round_trip():
    for lat in (-80.0, -33.5, 0.0, 12.25, 47.0, 84.0):
        assert cme.y_to_lat(cme.lat_to_y(lat)) == pytest.approx(lat, abs=1e-9)
    for lon in (-180.0, -75.5, 0.0, 179.0):
        assert cme.x_to_lon(cme.lon_to_x(lon)) == pytest.approx(lon)


def test_cells_in_box_wraps_antimeridian():
    cells = cme.cells_in_box(10.2, 11.7, 178.5, 181.5)
    assert set(cells) == {(lat, lon) for lat in (10, 11)
                          for lon in (178, 179, -180, -179)}


def test_world_outline_is_bundled():
    outline = cme.load_outline()
    assert cme.outline_path().is_file()
    assert outline["land"].elementCount() > 10_000
    assert outline["borders"].elementCount() > 1_000


def test_paint_only_installed_tiles_and_persist(editor):
    editor.maptype_combo.setCurrentText("NAIP")
    editor._apply_box([(35, -115), (35, -100)])  # second is not installed

    assert editor.config.get_all_cells() == {"35,-115": "NAIP"}
    assert editor.canvas.cells == {(35, -115): "NAIP"}


def test_erase_undo_redo(editor):
    changes = []
    editor.cellsChanged.connect(lambda: changes.append(1))
    editor._apply_box([(31, -119), (32, -119)])
    editor.canvas.set_mode(cme.ERASE)
    editor._apply_box([(31, -119)])
    assert editor.config.get_all_cells() == {"32,-119": "BI"}

    editor.undo()
    assert editor.config.get_all_cells() == {"31,-119": "BI", "32,-119": "BI"}
    editor.undo()
    assert editor.config.get_all_cells() == {}
    editor.redo()
    assert editor.config.get_all_cells() == {"31,-119": "BI", "32,-119": "BI"}
    assert len(changes) == 5


def test_reload_reads_existing_assignments(qt_app, tmp_path):
    config = CustomMapConfig(str(tmp_path / "custom_map.json"))
    config.set_cells({"35,-115": "EOX", "bad": "BI"})
    widget = CustomMapEditor(config, MAPTYPES, lambda: "", lambda _p: [])
    assert widget.canvas.cells == {(35, -115): "EOX"}
    widget.deleteLater()


def test_canvas_hit_testing_and_paint(editor):
    canvas = editor.canvas
    canvas.zoom = 6
    canvas.center = QPointF(cme.lon_to_x(-114.5), cme.lat_to_y(35.5))
    middle = canvas.cell_at(canvas.width() / 2, canvas.height() / 2)
    assert middle == (35, -115)
    image = canvas.grab()  # exercises every paint layer
    assert not image.isNull()

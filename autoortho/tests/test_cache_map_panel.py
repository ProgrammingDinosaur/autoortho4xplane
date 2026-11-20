"""Tests for the Cache view of the custom map."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for directory in (os.path.dirname(TESTS_DIR), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import pytest  # noqa: E402

import cache_inventory as ci  # noqa: E402
import tile_stream as ts  # noqa: E402
from test_cache_inventory import stores  # noqa: E402,F401  (fixture)
from ui.task_manager import TaskManager  # noqa: E402
from ui.task_models import TaskState  # noqa: E402
from ui.widgets import cache_map_panel as cmp  # noqa: E402
from ui.widgets import custom_map_editor as cme  # noqa: E402
from ui.widgets.custom_map_editor import CustomMapEditor  # noqa: E402
from utils.custom_map import CustomMapConfig  # noqa: E402


@pytest.fixture
def setup(qt_app, tmp_path, stores):  # noqa: F811
    ao_dir, cache, ortho, overlay = stores
    tasks = TaskManager()
    editor = CustomMapEditor(
        CustomMapConfig(str(tmp_path / "custom_map.json")), ["BI", "EOX"],
        scenery_path=lambda: ao_dir, discover=lambda _p: [],
        cache_paths=lambda: (ao_dir, cache, str(tmp_path)),
        task_manager=tasks,
    )
    editor.resize(900, 600)
    panel = editor.cache_panel
    # Run scans and jobs on the test thread.
    panel.refresh = lambda: panel._on_scanned(panel._generation, ci.scan(ao_dir, cache))
    panel._run = lambda work, done: done(work())
    yield editor, panel, tasks, stores
    try:
        editor.deleteLater()
    except RuntimeError:  # already deleted with a parent page
        pass


def test_toggle_switches_toolbar_canvas_and_mode(setup):
    editor, panel, _, _ = setup
    views = []
    editor.viewChanged.connect(views.append)
    editor.mode_buttons[cme.ERASE].setChecked(True)

    editor.view_buttons[cme.CACHE_VIEW].setChecked(True)
    assert editor.view == cme.CACHE_VIEW
    assert editor.toolbars.currentWidget() is panel
    assert editor.canvas.view == cme.CACHE_VIEW
    assert editor.canvas.mode == cme.SELECT
    assert panel.inventory is not None
    assert "Streamed scenery 3.3 KB in 2 tiles (1 kept offline)" in editor.status.text()

    editor.view_buttons[cme.SOURCES_VIEW].setChecked(True)
    assert editor.canvas.view == cme.SOURCES_VIEW
    assert editor.canvas.mode == cme.ERASE
    assert views == [cme.CACHE_VIEW, cme.SOURCES_VIEW]


def test_editor_without_cache_has_no_toggle(qt_app, tmp_path):
    editor = CustomMapEditor(CustomMapConfig(str(tmp_path / "m.json")), ["BI"],
                             lambda: "", lambda _p: [])
    assert editor.cache_panel is None and editor.view_buttons == {}
    editor.set_view(cme.CACHE_VIEW)
    assert editor.view == cme.SOURCES_VIEW


def test_layer_colours_and_legend(setup):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    legend = [text for _c, text in editor.canvas._cache_legend]
    assert legend == ["Both", "Kept for offline flying"]
    assert editor.canvas.pinned == {(47, -123)}
    panel.colour_combo.setCurrentIndex(1)  # by size
    assert [t for _c, t in editor.canvas._cache_legend] == [
        "Under 100 MB", "Kept for offline flying"]


def test_click_box_and_additive_selection(setup):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    canvas = editor.canvas
    canvas.cellsPicked.emit([(39, 23)], False)
    assert panel.selection == {(39, 23)}
    canvas.cellsPicked.emit([(47, -123)], True)
    assert panel.selection == {(39, 23), (47, -123)}
    canvas.cellsPicked.emit([(39, 23)], True)  # toggles off
    assert panel.selection == {(47, -123)}
    canvas.cellsPicked.emit([(1, 1), (1, 2)], True)
    assert panel.selection == {(47, -123), (1, 1), (1, 2)}
    assert canvas.selection == panel.selection
    assert panel.selection_bytes("mesh") == 2000
    assert panel.selection_bytes("imagery") == 660
    panel.clear_selection()
    assert canvas.selection == set()


def test_picks_are_ignored_in_sources_view(setup):
    editor, panel, _, _ = setup
    editor.canvas.cellsPicked.emit([(39, 23)], False)
    assert panel.selection == set()


def test_delete_imagery_after_confirmation(setup, monkeypatch):
    editor, panel, tasks, (ao_dir, cache, _, _) = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(47, -123)})
    asked = []
    monkeypatch.setattr(panel, "_confirm", lambda *a, **k: asked.append(a) or True)
    changed = []
    editor.cacheChanged.connect(lambda: changed.append(1))

    panel.delete_selected("imagery")

    assert "Delete 660 B of imagery from 1 tile?" in asked[0][1]
    assert panel.inventory.cells[(47, -123)].imagery_bytes == 0
    assert panel.inventory.cells[(47, -123)].dsf_bytes == 2000
    assert changed == [1]
    assert tasks.task("cache-map-delete").state == TaskState.COMPLETED
    assert "deleted 660 B from 1 tile" in editor.status.text()


def test_delete_cancelled_keeps_files(setup, monkeypatch):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(39, 23)})
    monkeypatch.setattr(panel, "_confirm", lambda *a, **k: False)
    panel.delete_selected("both")
    assert panel.inventory.cells[(39, 23)].total == 1360


def test_locked_blocks_changes(setup, monkeypatch):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(39, 23)})
    assert panel.delete_button.isEnabled() and panel.keep_button.isEnabled()

    editor.set_runtime_locked(True)
    assert not panel.delete_button.isEnabled()
    assert not panel.keep_button.isEnabled()
    assert panel.delete_button.toolTip() == cmp.LOCKED_TIP
    assert "stop AutoOrtho" in editor.status.text()
    monkeypatch.setattr(panel, "_confirm", lambda *a, **k: pytest.fail("asked"))
    panel.delete_selected("both")
    panel.keep_selected()

    editor.set_runtime_locked(False)
    assert panel.delete_button.isEnabled()


def test_keep_offline_runs_as_an_activity_task(setup, monkeypatch):
    editor, panel, tasks, (ao_dir, _, ortho, _) = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(39, 23), (40, 23)})
    monkeypatch.setattr(ci, "mesh_download_estimate", lambda d, cells: (30_000_000, 1, 2))
    asked = []
    monkeypatch.setattr(panel, "_confirm", lambda *a, **k: asked.append(a) or True)
    calls = []

    def fake_keep(ao, cells, custom_scenery_dir="", progress=None, cancelled=None):
        calls.append(cells)
        progress(1, 2, 10)
        ci.pin_cells(ao, cells)
        return ci.ChangeResult(bytes=20, files=2, tiles=2)

    monkeypatch.setattr(ci, "keep_offline", fake_keep)
    panel.keep_selected()

    assert "Download about 28.6 MB" in asked[0][1]
    assert "at ZL16" in asked[0][2] and "at ZL18" in asked[0][2]
    assert calls == [[(39, 23), (40, 23)]]
    task = tasks.task("cache-map-keep")
    assert task.state == TaskState.COMPLETED
    assert ts.read_pins(ortho) >= {(39, 23), (40, 23)}
    assert panel.inventory.cells[(40, 23)].pinned


def test_keep_needs_streaming(setup, monkeypatch, tmp_path):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.inventory = ci.CacheInventory()  # nothing streamed
    panel.set_selection({(1, 1)})
    shown = []
    monkeypatch.setattr(cmp.QMessageBox, "information", lambda *a: shown.append(a))
    panel.keep_selected()
    assert "Scenery Library" in shown[0][2]


def test_unkeep_removes_pin(setup):
    editor, panel, _, (_, _, ortho, _) = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(47, -123)})
    assert panel.unkeep_action.isEnabled()
    panel.unkeep_selected()
    assert ts.read_pins(ortho) == set()
    assert not panel.inventory.cells[(47, -123)].pinned


def test_hover_text_lists_sizes_zooms_and_pin(setup):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    text = panel.hover_text((47, -123))
    assert text.startswith("+47-123  ·  kept for offline flying")
    assert "Streamed scenery: 2.0 KB" in text
    assert "Imagery: 660 B" in text and "ZL16–18" in text and "BI" in text
    assert "Last used:" in text
    assert panel.hover_text((0, 0)) == "+00+000\nNothing stored"


def test_flight_plan_page_retitles_the_map_box(setup):
    from PySide6.QtWidgets import QWidget
    from ui.pages.flight_plan_page import FlightPlanPage
    editor, _, _, _ = setup
    page = FlightPlanPage(QWidget(), editor)
    editor.set_view(cme.CACHE_VIEW)
    assert page.map_group.title() == "Cache"
    editor.set_view(cme.SOURCES_VIEW)
    assert page.map_group.title() == "Custom Map"

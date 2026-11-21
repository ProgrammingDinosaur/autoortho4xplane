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
    assert "Scenery 3.3 KB in 2 tiles (1 downloaded)" in editor.status.text()

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
    assert legend == ["Both", "Downloaded (kept)"]
    assert editor.canvas.pinned == {(47, -123)}
    panel.colour_combo.setCurrentIndex(1)  # by size
    assert [t for _c, t in editor.canvas._cache_legend] == [
        "Under 100 MB", "Downloaded (kept)"]


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
    monkeypatch.setattr(panel, "_ask_delete", lambda: "imagery")
    changed = []
    editor.cacheChanged.connect(lambda: changed.append(1))

    panel.delete_button.click()

    assert panel.inventory.cells[(47, -123)].imagery_bytes == 0
    assert panel.inventory.cells[(47, -123)].dsf_bytes == 2000
    assert changed == [1]
    assert tasks.task("cache-map-delete").state == TaskState.COMPLETED
    assert "deleted 660 B from 1 tile" in editor.status.text()


def test_delete_cancelled_keeps_files(setup, monkeypatch):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(39, 23)})
    monkeypatch.setattr(panel, "_ask_delete", lambda: None)
    panel.delete_selected()
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
    monkeypatch.setattr(panel, "_ask_delete", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr(panel, "_ask_download", lambda *a, **k: pytest.fail("asked"))
    panel.delete_selected()
    panel.keep_selected()

    editor.set_runtime_locked(False)
    assert panel.delete_button.isEnabled()


def test_keep_offline_runs_as_an_activity_task(setup, monkeypatch):
    editor, panel, tasks, (ao_dir, _, ortho, _) = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(39, 23), (40, 23)})
    monkeypatch.setattr(ci, "mesh_download_estimate", lambda d, cells: (30_000_000, 1, 2))
    asked = []
    monkeypatch.setattr(panel, "_ask_download",
                        lambda *a, **k: asked.append(a) or (True, None))
    calls = []

    def fake_keep(ao, cells, custom_scenery_dir="", progress=None, cancelled=None):
        calls.append(cells)
        progress(1, 2, 10)
        ci.pin_cells(ao, cells)
        return ci.ChangeResult(bytes=20, files=2, tiles=2)

    monkeypatch.setattr(ci, "keep_offline", fake_keep)
    panel.keep_selected()

    assert "Download about 28.6 MB" in asked[0][1]
    assert sorted(asked[0][4]) == [16, 17, 18]   # imagery offered per zoom
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
    assert not panel.unkeep_button.isHidden() and panel.unkeep_button.isEnabled()
    panel.unkeep_button.click()
    assert ts.read_pins(ortho) == set()
    assert not panel.inventory.cells[(47, -123)].pinned


def test_hover_text_lists_sizes_zooms_and_pin(setup):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    text = panel.hover_text((47, -123))
    assert text.startswith("+47-123  ·  downloaded (kept)")
    assert "Scenery: 2.0 KB" in text
    assert "Imagery: 660 B" in text and "ZL16–18" in text and "BI" in text
    assert "Last used:" in text
    assert panel.hover_text((0, 0)) == "+00+000\nNothing stored"


def test_flight_plan_page_retitles_the_map_box(setup):
    from PySide6.QtWidgets import QWidget
    from ui.pages.flight_plan_page import FlightPlanPage
    editor, _, _, _ = setup
    page = FlightPlanPage(QWidget(), editor)
    editor.set_view(cme.CACHE_VIEW)
    assert page.map_group.title() == "Scenery && Cache"   # shown as one &
    editor.set_view(cme.SOURCES_VIEW)
    assert page.map_group.title() == "Custom Map"


def test_regions_menu_selects_a_whole_region(setup, monkeypatch):
    editor, panel, _, _ = setup
    cells = [(47, -123), (47, -122), (48, -123)]
    monkeypatch.setattr(ci, "stream_coverage", lambda _ao: (
        [("na-n", "North America North", cells)], set(cells)))
    panel._read_regions("ao")
    combo = panel.region_combo
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Select a region…", "North America North (3 tiles)"]
    editor.set_view(cme.CACHE_VIEW)
    combo.setCurrentIndex(1)
    combo.activated.emit(1)       # what a user's pick sends
    assert panel.selection == set(cells)
    assert combo.currentIndex() == 0   # a picker, not a setting
    assert "North America North selected" in editor.status.text()
    assert panel.keep_button.isEnabled() and not panel.clear_button.isHidden()


def test_regions_menu_without_a_scenery_set(setup, monkeypatch):
    _, panel, _, _ = setup
    monkeypatch.setattr(ci, "stream_coverage", lambda _ao: ([], None))
    panel._read_regions("ao")
    assert not panel.region_combo.isEnabled()
    assert "scenery set" in panel.region_combo.itemText(0)


def test_download_dialog_offers_imagery_per_zoom(setup, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QDialog
    _, panel, _, _ = setup
    picked = {}

    def exec_dialog(dialog):
        combo = dialog.findChild(QComboBox)
        picked["items"] = [combo.itemText(i) for i in range(combo.count())]
        combo.setCurrentIndex(2)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", exec_dialog)
    ok, zoom = panel._ask_download("Download 2 tiles?", "text", "detail", "Download",
                                   {16: (100, 1_000_000), 17: (400, 4_000_000)})
    assert ok and zoom == 17 and panel.imagery_zoom == 17
    assert picked["items"][0] == "Don't download imagery"
    assert picked["items"][2].startswith("Up to ZL17 (about")
    monkeypatch.setattr(QDialog, "exec", lambda d: QDialog.DialogCode.Rejected)
    assert panel._ask_download("t", "x", "y", "Download", {16: (1, 1)}) == (False, None)


def test_hover_shows_kept_imagery(setup):
    usage = ci.CellUsage(chunk_bytes=2048, chunk_files=2, zooms={16}, imagery_kept=17)
    assert "kept up to ZL17" in cmp.describe_cell((47, -123), usage)


def test_toolbar_shows_only_what_the_selection_allows(setup):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    assert not panel.keep_button.isEnabled() and not panel.delete_button.isEnabled()
    assert panel.clear_button.isHidden() and panel.unkeep_button.isHidden()
    assert "pick a region" in editor.status.text()
    panel.set_selection({(1, 1)})           # nothing stored there
    assert panel.keep_button.isEnabled() and not panel.delete_button.isEnabled()
    assert panel.unkeep_button.isHidden() and not panel.clear_button.isHidden()
    panel.set_selection({(47, -123)})       # stored and kept
    assert panel.delete_button.isEnabled() and not panel.unkeep_button.isHidden()
    panel.clear_button.click()
    assert panel.selection == set() and panel.clear_button.isHidden()


def test_delete_dialog_offers_what_is_stored(setup, monkeypatch):
    from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(47, -123)})
    seen = {}

    def exec_dialog(dialog):
        mesh, imagery = dialog.findChildren(QCheckBox)
        seen["boxes"] = [(b.text(), b.isEnabled(), b.isChecked()) for b in (mesh, imagery)]
        imagery.setChecked(True)
        ok = [b for b in dialog.findChild(QDialogButtonBox).buttons()
              if b.text().startswith("Delete")][0]
        seen["button"] = ok.text()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", exec_dialog)
    assert panel._ask_delete() == "both"
    assert seen["boxes"] == [("&Scenery (2.0 KB)", True, True),
                             ("&Imagery (660 B)", True, False)]
    assert seen["button"] == "Delete 2.6 KB"
    monkeypatch.setattr(QDialog, "exec", lambda d: QDialog.DialogCode.Rejected)
    assert panel._ask_delete() is None


def test_only_tiles_of_the_set_or_with_something_stored_can_be_selected(setup, monkeypatch):
    editor, panel, _, _ = setup
    covered = {(47, -122), (48, -123)}
    monkeypatch.setattr(ci, "stream_coverage", lambda _ao: (
        [("na-n", "North America North", sorted(covered))], covered))
    panel._read_regions("ao")
    editor.set_view(cme.CACHE_VIEW)
    canvas = editor.canvas

    canvas.cellsPicked.emit([(0, -30)], False)          # open ocean
    assert panel.selection == set()
    assert "+00-030 has no scenery in the scenery set" in editor.status.text()
    assert "Not in the scenery set" in panel.hover_text((0, -30))

    canvas.cellsPicked.emit([(47, -122)], False)
    assert panel.selection == {(47, -122)}
    canvas.cellsPicked.emit([(0, -30), (1, -30), (48, -123)], True)   # a box
    assert panel.selection == {(47, -122), (48, -123)}
    assert "2 tiles without scenery in the set left out" in editor.status.text()

    # Stored tiles stay selectable even outside the set, so they can be deleted.
    canvas.cellsPicked.emit([(39, 23)], False)
    assert panel.selection == {(39, 23)}
    assert "Not in the scenery set" not in panel.hover_text((39, 23))


def test_switching_sets_drops_tiles_the_new_one_lacks(setup, monkeypatch):
    editor, panel, _, _ = setup
    editor.set_view(cme.CACHE_VIEW)
    panel.set_selection({(10, 10), (47, -122)})       # coverage not known yet
    monkeypatch.setattr(ci, "stream_coverage", lambda _ao: ([], {(47, -122)}))
    panel._read_regions("ao")
    assert panel.selection == {(47, -122)}


def test_colour_by_scenery_set_and_switching_tiles(setup, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    editor, panel, _, (ao_dir, cache, ortho, _) = setup
    # The ortho package here is the XP11 set; mark one stored tile as XP12.
    ts.write_tile_sources(ortho, {(47, -123): "xp12"})
    panel.refresh()
    editor.set_view(cme.CACHE_VIEW)
    panel.colour_combo.setCurrentIndex(panel.colour_combo.findData(cmp.BY_SET))
    legend = [t for _c, t in editor.canvas._cache_legend]
    assert legend[:2] == ["XP12 set", "XP11 set"]
    assert "XP12 set (you use the XP11 set)" in panel.hover_text((47, -123))
    assert "XP11 set" in panel.hover_text((39, 23))

    panel.set_selection({(47, -123), (39, 23)})
    assert not panel.switch_button.isHidden()
    assert panel.switch_button.text() == "Use XP11 for 1 tile…"
    panel.set_selection({(39, 23)})
    assert panel.switch_button.isHidden()

    panel.set_selection({(47, -123)})
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda box: [b for b in box.buttons() if b.text().startswith("Use")][0])
    started = []
    monkeypatch.setattr(panel, "_start_keep", lambda *a: started.append(a))
    panel.switch_button.click()
    assert started and started[0][2] == [(47, -123)] and started[0][4] is None
    assert not os.path.exists(os.path.join(ortho, *ts.dsf_rel(47, -123).split("/")))
    assert ts.read_pins(ortho) >= {(47, -123)}

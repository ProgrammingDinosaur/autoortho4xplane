"""The main window moves installed regions into AutoOrtho scenery on start."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for directory in (os.path.dirname(TESTS_DIR), TESTS_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

from PySide6.QtWidgets import QApplication  # noqa: E402

import region_adopt as ra  # noqa: E402
import tile_stream as ts  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from config_ui_qt import ConfigUI  # noqa: E402
from test_region_adopt import TILES, make_region, make_streaming  # noqa: E402


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_installed_regions_move_on_start_and_say_so(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(ConfigUI, "refresh_scenery_list",
                        lambda self: self.scenery_layout.addStretch())
    monkeypatch.setattr(ConfigUI, "start_update_check",
                        lambda self, manual=False: None)
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "Custom Scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()
    ao_dir = os.path.join(cfg.paths.scenery_path, "z_autoortho", "scenery")
    os.makedirs(ao_dir, exist_ok=True)
    pkg = make_streaming(ao_dir, source="xp12")
    region, _ = make_region(ao_dir, "eur", TILES)

    ui = ConfigUI(cfg)
    try:
        assert wait_until(lambda: not os.path.exists(region))
        assert wait_until(lambda: "region_migration" in ui._notices)
        notice = ui._notices["region_migration"]
        assert notice.level == "info" and "eur" in notice.text
        assert ts.read_pins(pkg) == set(TILES)
        assert not ra.needs_migration(ao_dir)
        task = ui.task_manager.task("scenery-migrate")
        assert task is not None and task.state.terminal
    finally:
        ui.close()
        ui.deleteLater()


def test_scenery_library_replaces_or_removes_tiles_of_another_set(
        qt_app, monkeypatch, tmp_path):
    """Regions moved into the XP12 set stay XP11 tiles; the Scenery Library's
    hint replaces them (only where the XP12 set has the tile) or removes them."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(ConfigUI, "refresh_scenery_list",
                        lambda self: self.scenery_layout.addStretch())
    monkeypatch.setattr(ConfigUI, "start_update_check",
                        lambda self, manual=False: None)
    accept = (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.DestructiveRole)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
        b for b in self.buttons() if self.buttonRole(b) in accept))
    downloads = []
    monkeypatch.setattr(ConfigUI, "_download_kept",
                        lambda self, cells: downloads.append(list(cells)))
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "Custom Scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    os.makedirs(cfg.paths.xplane_path)
    with open(os.path.join(cfg.paths.xplane_path, "Log.txt"), "wb") as fh:
        fh.write(b"Log.txt for X-Plane 12.4.4-r2 (build 124419)\n")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()
    ao_dir = os.path.join(cfg.paths.scenery_path, "z_autoortho", "scenery")
    os.makedirs(ao_dir, exist_ok=True)
    pkg = make_streaming(ao_dir, source="xp12", tiles=TILES[:2])
    make_region(ao_dir, "eur", TILES)

    ui = ConfigUI(cfg)
    panel = ui.scenery_library_page.stream_panel
    try:
        assert wait_until(lambda: not ra.needs_migration(ao_dir))
        assert wait_until(lambda: not panel.hint.isHidden())
        assert panel.state["xplane_major"] == 12
        assert panel.hint.title_label.text().startswith("3 kept tiles")

        ui._on_other_set_tiles("replace")
        assert wait_until(lambda: downloads)
        assert downloads == [sorted(TILES[:2])]
        assert ts.read_tile_sources(pkg) == {TILES[2]: "xp11"}  # XP12 lacks it
        assert ts.read_pins(pkg) == set(TILES)
        assert wait_until(lambda: panel.hint.title_label.text().startswith("1 kept tile"))

        ui._on_other_set_tiles("remove")
        assert wait_until(lambda: panel.hint.isHidden())
        assert ts.read_tile_sources(pkg) == {}
        assert ts.read_pins(pkg) == set(TILES[:2])

        # Upgraders get the one-time explanation, without it blocking.
        box = ui._scenery_intro_box
        assert box is not None and box.isVisible() and not box.isModal()
        assert "tile by tile" in box.text()
        assert cfg.general.scenery_intro_shown is True
        library = next(b for b in box.buttons() if b.text() == "Open Scenery Library")
        library.click()
        assert ui._scenery_intro_box is None
        ui._refresh_stream_panel()
        QApplication.processEvents()
        assert ui._scenery_intro_box is None  # once only

        # A dismissed hint is remembered in the config.
        ui._on_scenery_hint_dismissed("other:xp12:7")
        assert "other:xp12:7" in cfg.general.dismissed_scenery_hints
    finally:
        ui.close()
        ui.deleteLater()


def test_no_scenery_explanation_when_set_up_the_recommended_way(
        qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(ConfigUI, "refresh_scenery_list",
                        lambda self: self.scenery_layout.addStretch())
    monkeypatch.setattr(ConfigUI, "start_update_check",
                        lambda self, manual=False: None)
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "Custom Scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()
    ao_dir = os.path.join(cfg.paths.scenery_path, "z_autoortho", "scenery")
    os.makedirs(ao_dir, exist_ok=True)
    make_streaming(ao_dir, source="xp12")

    ui = ConfigUI(cfg)
    try:
        assert wait_until(lambda: ui.scenery_library_page.stream_panel.enabled)
        assert wait_until(lambda: cfg.general.scenery_intro_shown is True)
        assert getattr(ui, "_scenery_intro_box", None) is None
        assert ui.scenery_library_page.stream_panel.hint.isHidden()
    finally:
        ui.close()
        ui.deleteLater()


def test_a_burst_of_task_updates_refreshes_the_library_once(
        qt_app, monkeypatch, tmp_path):
    """Moving a region reports progress many times a second; rebuilding the
    Scenery Library for each report froze the window (3.4.1 start-up move)."""
    from ui.task_models import TaskType
    monkeypatch.setattr(ConfigUI, "refresh_scenery_list",
                        lambda self: self.scenery_layout.addStretch())
    monkeypatch.setattr(ConfigUI, "start_update_check",
                        lambda self, manual=False: None)
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "Custom Scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()
    ao_dir = os.path.join(cfg.paths.scenery_path, "z_autoortho", "scenery")
    os.makedirs(ao_dir, exist_ok=True)
    make_streaming(ao_dir, source="xp12")

    ui = ConfigUI(cfg)
    try:
        assert wait_until(lambda: ui.scenery_library_page.stream_panel.enabled)
        page = ui.scenery_library_page
        real_set_regions = page.set_regions
        calls = []

        def set_regions(*args, **kwargs):
            calls.append(1)
            return real_set_regions(*args, **kwargs)

        monkeypatch.setattr(page, "set_regions", set_regions)
        manager = ui.task_manager
        manager.create_task("burst", TaskType.SCENERY_INSTALL, "Burst")
        for i in range(1000):
            manager.update_task("burst", progress=i / 10.0,
                                stage=f"Moving eur: {i} of 1000 files")
        assert calls == []  # coalesced, not one rebuild per update
        assert wait_until(lambda: len(calls) >= 1)
        deadline = time.time() + 0.6
        while time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        assert len(calls) == 1
    finally:
        ui.close()
        ui.deleteLater()

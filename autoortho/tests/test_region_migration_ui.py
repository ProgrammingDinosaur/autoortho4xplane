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

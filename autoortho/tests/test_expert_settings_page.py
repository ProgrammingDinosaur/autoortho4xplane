"""The Expert settings table: every setting, searchable, safe to edit."""

import inspect
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

import aoconfig  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from settings_registry import Tier  # noqa: E402
from ui.pages.expert_settings_page import (  # noqa: E402
    COL_SOURCE,
    COL_VALUE,
    RESET,
    ExpertSettingsPage,
)


@pytest.fixture
def cfg(tmp_path):
    return AOConfig(str(tmp_path / ".autoortho"))


def _row(page, name):
    model = page.model
    for row in range(model.rowCount()):
        if model.spec(row).name == name:
            return row
    raise AssertionError(name)


def _set(page, name, text):
    index = page.model.index(_row(page, name), COL_VALUE)
    return page.model.setData(index, text, Qt.ItemDataRole.EditRole)


def _shown(page):
    return {
        page.model.spec(page.proxy.mapToSource(page.proxy.index(r, 0)).row()).name
        for r in range(page.proxy.rowCount())
    }


def test_lists_visible_tiers_only(qt_app, cfg):
    page = ExpertSettingsPage(cfg)
    names = {page.model.spec(r).name for r in range(page.model.rowCount())}

    assert "autoortho.max_zoom" in names
    assert "pydds.dds_compression_level" in names
    assert "general.window_x" not in names
    assert "autoortho.max_concurrent_downloads" not in names


def test_locked_settings_are_read_only(qt_app, cfg):
    page = ExpertSettingsPage(cfg, locked_names={"autoortho.max_zoom"})
    index = page.model.index(_row(page, "autoortho.max_zoom"), COL_VALUE)

    assert not page.model.flags(index) & Qt.ItemFlag.ItemIsEditable
    assert _set(page, "autoortho.max_zoom", "17") is False
    assert page.model.pending() == {}


def test_edit_validates_and_tracks_pending(qt_app, cfg):
    page = ExpertSettingsPage(cfg)
    messages = []
    page.model.validation_failed.connect(messages.append)
    changes = []
    page.changed.connect(lambda: changes.append(True))

    assert _set(page, "pydds.dds_compression_level", "40") is False
    assert messages and "1 to 19" in messages[0]

    assert _set(page, "pydds.dds_compression_level", " 6 ") is True
    assert page.model.pending() == {"pydds.dds_compression_level": "6"}
    row = _row(page, "pydds.dds_compression_level")
    assert page.model.index(row, COL_SOURCE).data() == "Edited (pending)"
    assert changes

    # Typing the saved value again drops the pending edit.
    assert _set(page, "pydds.dds_compression_level", "3") is True
    assert page.model.pending() == {}


def test_apply_writes_and_reset_returns_to_default(qt_app, cfg):
    page = ExpertSettingsPage(cfg)
    _set(page, "autoortho.prefetch_interval", "2.5")
    page.apply_to(cfg)
    cfg.save()
    page.mark_saved()

    text = open(cfg.conf_file, encoding="utf-8").read()
    assert "prefetch_interval = 2.5" in text
    row = _row(page, "autoortho.prefetch_interval")
    assert page.model.index(row, COL_SOURCE).data() == "Custom"

    page.model.reset_rows([row])
    assert page.model.pending() == {"autoortho.prefetch_interval": RESET}
    page.apply_to(cfg)
    cfg.save()
    page.mark_saved()

    assert cfg.autoortho.prefetch_interval == 1.0
    assert "# prefetch_interval = 1.0" in open(cfg.conf_file, encoding="utf-8").read()
    assert page.model.index(row, COL_SOURCE).data() == "Default"


def test_snapshot_round_trip(qt_app, cfg):
    page = ExpertSettingsPage(cfg)
    _set(page, "autoortho.prefetch_interval", "3.0")
    snapshot = page.snapshot()

    page.restore(())
    assert page.model.pending() == {}
    page.restore(snapshot)
    assert page.model.pending() == {"autoortho.prefetch_interval": "3.0"}


def test_filters(qt_app, cfg):
    cfg.set_value("autoortho", "prefetch_interval", "2.0")
    page = ExpertSettingsPage(cfg)

    page.search_edit.setText("compression level")
    assert _shown(page) == {"pydds.dds_compression_level"}

    page.search_edit.setText("")
    page.only_custom_check.setChecked(True)
    assert _shown(page) == {"autoortho.prefetch_interval"}

    page.only_custom_check.setChecked(False)
    page.tier_combo.setCurrentIndex(
        page.tier_combo.findData(Tier.ESSENTIAL.value)
    )
    assert "autoortho.max_zoom" in _shown(page)
    assert "pydds.compressor" not in _shown(page)


def test_widget_bound_list_matches_main_window_assignments():
    """Every setting a main-window control writes must be locked in the table."""
    from ui import main_window

    source = inspect.getsource(main_window)
    assigned = set(
        re.findall(r"self\.cfg\.([a-z_]+)\.([a-z_0-9]+)\s*=(?!=)", source)
    )
    registry = aoconfig.settings_registry()
    expected = set()
    for section, key in assigned:
        spec = registry.get(section, key)
        if spec is None or spec.tier in (Tier.INTERNAL, Tier.RETIRED):
            continue
        expected.add(spec.name)

    bound = main_window.ConfigUI.WIDGET_BOUND_SETTINGS
    assert expected - bound == set()
    assert {
        name for name in bound
        if registry.get(*name.split(".")) is None
    } == set()


# -- Integration with the main window's Apply / Revert ---------------------


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    from config_ui_qt import ConfigUI
    from ui.runtime_state import RuntimeState

    monkeypatch.setattr(
        ConfigUI,
        "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
    monkeypatch.setattr(ConfigUI, "start_update_check", lambda self: None)
    for name in ("scenery", "cache", "downloads", "X-Plane"):
        (tmp_path / name).mkdir()
    config = AOConfig(str(tmp_path / ".autoortho"))
    config.paths.scenery_path = str(tmp_path / "scenery")
    config.paths.cache_dir = str(tmp_path / "cache")
    config.paths.download_dir = str(tmp_path / "downloads")
    config.paths.xplane_path = str(tmp_path / "X-Plane")
    config.save()
    config.get_config()

    ui = ConfigUI(config)
    for worker in (ui.readiness_worker, ui.storage_scan_worker):
        if worker is not None and worker.isRunning():
            worker.wait(2000)
    qt_app.processEvents()
    yield ui
    ui.mount_monitor_timer.stop()
    ui._set_runtime_state(RuntimeState.STOPPED)
    ui.settings_session.mark_applied(ui._snapshot_settings())
    ui.close()
    ui.deleteLater()
    qt_app.processEvents()


def test_expert_edit_dirties_session_and_saves(qt_app, config_ui):
    page = config_ui.expert_settings_page
    assert "autoortho.max_zoom" in config_ui.WIDGET_BOUND_SETTINGS
    assert not page.model.is_editable(
        page.model.spec(_row(page, "autoortho.max_zoom"))
    )

    _set(page, "pydds.dds_compression_level", "7")
    qt_app.processEvents()
    assert config_ui.settings_session.dirty is True
    assert config_ui.settings_session.restart_required is True

    assert config_ui.save_config(refresh_scenery=False) is True
    config_ui.settings_session.mark_applied(config_ui._snapshot_settings())

    assert config_ui.cfg.pydds.dds_compression_level == 7
    text = open(config_ui.cfg.conf_file, encoding="utf-8").read()
    assert "dds_compression_level = 7" in text
    assert page.model.pending() == {}


def test_revert_discards_expert_edits(qt_app, config_ui):
    page = config_ui.expert_settings_page
    _set(page, "pydds.dds_compression_level", "9")
    qt_app.processEvents()
    assert config_ui.settings_session.dirty is True

    config_ui.on_revert()

    assert page.model.pending() == {}
    assert config_ui.settings_session.dirty is False
    assert config_ui.cfg.pydds.dds_compression_level == 3

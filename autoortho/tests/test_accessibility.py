import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QDoubleSpinBox

from aoconfig import AOConfig
from config_ui_qt import ConfigUI
from ui.dynamic_zoom_editor import DynamicZoomEditor
from ui.main_window import ApplicationShell
from utils.dynamic_zoom import DynamicZoomManager


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        ConfigUI,
        "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
    monkeypatch.setattr(
        ConfigUI,
        "start_update_check",
        lambda self, manual=False: None,
    )
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    cfg.paths.scenery_path = str(tmp_path / "scenery")
    cfg.paths.cache_dir = str(tmp_path / "cache")
    cfg.paths.download_dir = str(tmp_path / "downloads")
    cfg.paths.xplane_path = str(tmp_path / "X-Plane")
    cfg.general.setup_complete = True
    cfg.cache.auto_clean_cache = False
    cfg.save()
    cfg.get_config()
    ui = ConfigUI(cfg)
    yield ui
    ui.settings_session.mark_applied(ui._snapshot_settings())
    ui.close()
    ui.deleteLater()
    qt_app.processEvents()


def test_shell_custom_controls_have_accessible_names(qt_app):
    shell = ApplicationShell()
    assert shell.header.accessibleName()
    assert shell.header.runtime_chip.accessibleName()
    assert shell.header.start_stop_button.accessibleName()
    assert shell.header.overflow_button.accessibleName()
    assert all(
        shell.navigation.button_for(key).accessibleName()
        for key in shell.navigation.destination_keys()
    )


def test_settings_controls_and_shortcuts_are_accessible(config_ui):
    assert all(
        widget.accessibleName()
        for _, widget in config_ui._settings_widgets()
    )
    assert len(config_ui._ui_shortcuts) >= 7
    first = config_ui.shell.navigation.button_for("home")
    second = config_ui.shell.navigation.button_for("scenery-library")
    assert first.nextInFocusChain() is second


def test_advanced_adaptive_tuning_is_collapsed_and_explained(config_ui):
    assert config_ui.advanced_adaptive_widget.isHidden()
    assert config_ui.advanced_adaptive_toggle.isCheckable()

    config_ui.advanced_adaptive_toggle.setChecked(True)
    assert not config_ui.advanced_adaptive_widget.isHidden()

    assert "start more cautiously" in (
        config_ui.provider_initial_concurrency_spinbox.toolTip()
    )
    assert "128 requests" in (
        config_ui.provider_decrease_factor_spinbox.toolTip()
    )
    assert "correlated" in config_ui.provider_cooldown_spinbox.toolTip()

    config_ui.provider_adaptive_check.setChecked(False)
    assert config_ui.advanced_adaptive_widget.isHidden()
    assert not config_ui.advanced_adaptive_toggle.isEnabled()


def test_per_chunk_exact_value_enables_apply(config_ui, qt_app):
    exact_control = next(
        spin
        for spin in config_ui.categorized_settings_page.findChildren(
            QDoubleSpinBox
        )
        if spin.property("boundSliderObjectName") == "maxwait"
    )
    assert not config_ui.apply_button.isEnabled()

    exact_control.setValue(exact_control.value() + 0.1)
    qt_app.processEvents()

    assert config_ui.maxwait_slider.value() == round(
        exact_control.value() * 10
    )
    assert config_ui.settings_session.dirty is True
    assert config_ui.apply_button.isEnabled()


def test_dynamic_zoom_table_exposes_context(qt_app):
    manager = DynamicZoomManager()
    manager.set_base_zoom(16, 18)
    manager.add_step(10000, 15, 17)
    editor = DynamicZoomEditor(manager)

    assert editor.accessibleName()
    assert editor.preview.accessibleDescription()
    assert editor.table.accessibleName()
    assert all(
        editor.model.headerData(
            index,
            Qt.Orientation.Horizontal,
        )
        for index in range(editor.model.columnCount())
    )
    altitude_index = editor.model.index(1, 1)
    assert altitude_index.flags() & Qt.ItemFlag.ItemIsEditable
    assert "10,000" in editor.model.data(
        editor.model.index(1, 0)
    )


def test_active_shell_uses_approved_terminology(config_ui):
    text = " ".join(
        label.text()
        for label in config_ui.shell.findChildren(QLabel)
    )
    for outdated in (
        "Map type override",
        "Using Custom Tiles",
        "Don't cleanup downloads",
        "DSF Seasons convert workers",
        "Max Zoom Mode",
        "AutoOrtho Injection",
    ):
        assert outdated not in text
    assert (
        config_ui.setup_validation_label.property("validationState")
        == "error"
    )


def test_shell_layout_survives_two_hundred_percent_scaling():
    root = Path(__file__).resolve().parents[2]
    script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'autoortho'))
from PySide6.QtWidgets import QApplication
from ui.main_window import ApplicationShell
app = QApplication([])
shell = ApplicationShell()
shell.resize(900, 650)
shell.show()
app.processEvents()
assert shell.minimumSizeHint().width() <= shell.width()
assert shell.header.start_stop_button.width() >= shell.header.start_stop_button.sizeHint().width()
assert shell.page('home').findChild(__import__('PySide6.QtWidgets', fromlist=['QScrollArea']).QScrollArea) is not None
shell.close()
"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_SCALE_FACTOR"] = "2"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_long_localized_navigation_label_is_not_clipped(qt_app):
    shell = ApplicationShell()
    button = shell.navigation.button_for("scenery-library")
    button.setText("Scenery Library With A Much Longer Localized Name")
    shell.resize(1200, 700)
    shell.show()
    qt_app.processEvents()

    assert button.width() >= button.sizeHint().width()
    shell.close()


def test_storage_browse_buttons_do_not_share_mnemonics(
    qt_app,
):
    from ui.setup_wizard import SetupWizard

    wizard = SetupWizard()
    texts = [
        button.text()
        for button in wizard.storage_page._browse_buttons
    ]
    assert all("&" not in text for text in texts)
    wizard.storage_page.stop_usage_scan()
    wizard.close()

"""The zoom target cost panel and resource guards in the settings window."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import aoconfig  # noqa: E402
import hardware  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from guards import Fix, GuardResult, Level  # noqa: E402
from hardware import GB, HardwareInfo  # noqa: E402
from ui.pages.expert_settings_page import COL_SOURCE, ExpertSettingsModel  # noqa: E402
from ui.readiness import ReadinessStatus  # noqa: E402
from ui.widgets.target_cost_panel import TargetCostPanel  # noqa: E402

SMALL_CARD = HardwareInfo(16, 8, 32 * GB, 8 * GB, "Test GPU", "registry")
BIG_CARD = HardwareInfo(16, 8, 32 * GB, 24 * GB, "Test GPU", "registry")


@pytest.fixture
def pc():
    def use(info):
        hardware.set_for_tests(info)
    yield use
    hardware.set_for_tests(None)


def test_panel_shows_results_and_fix_button(qt_app):
    panel = TargetCostPanel()
    fix = Fix("Use ZL16 (airports ZL18)", {"autoortho.max_zoom": "16"})
    emitted = []
    panel.fix_requested.connect(emitted.append)
    panel.set_results(
        [
            GuardResult("vram", Level.BAD, "Graphics memory", "Too much.", fix),
            GuardResult("download", Level.INFO, "Download", "About 5 Mbit/s."),
        ],
        "8 cores, 32 GB RAM",
    )
    badge, message, button = panel._rows["vram"]
    assert badge.property("state") == "error"
    assert "Suggested: Use ZL16" in message.text()
    assert not button.isHidden()
    button.click()
    assert emitted == [fix]
    assert panel._rows["download"][2].isHidden()

    panel.set_results([GuardResult("vram", Level.OK, "Graphics memory", "Fits.")])
    assert panel._rows["vram"][2].isHidden()
    assert panel._rows["download"][1].isHidden()


def test_expert_table_shows_this_pc_values(qt_app, tmp_path, pc):
    pc(HardwareInfo(32, 16, 64 * GB))
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    model = ExpertSettingsModel(cfg)
    row = next(
        r for r in range(model.rowCount())
        if model.spec(r).name == "cache.cache_mem_limit"
    )
    spec = model.spec(row)
    assert model.data(model.index(row, COL_SOURCE)) == "This PC"
    assert model.default_text(spec) == "8"
    assert "packaged default is 4" in model.tooltip(spec)


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path, pc):
    from config_ui_qt import ConfigUI
    from ui.runtime_state import RuntimeState

    pc(SMALL_CARD)
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
    config.autoortho.max_zoom = 17
    config.autoortho.max_zoom_near_airports = 19
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


def test_panel_warns_and_fix_updates_controls(qt_app, config_ui):
    config_ui._update_target_cost()
    vram = config_ui.target_cost_panel.result("vram")
    assert vram.level is Level.BAD
    assert vram.fix is not None
    assert "Test GPU" in config_ui.target_cost_panel.hardware_label.text()

    config_ui._apply_guard_fix(vram.fix)
    qt_app.processEvents()
    assert config_ui.max_zoom_slider.value() < 17
    assert config_ui.settings_session.dirty is True

    config_ui._update_target_cost()
    assert config_ui.target_cost_panel.result("vram").level is Level.OK


def test_panel_follows_the_controls_before_apply(qt_app, config_ui, pc):
    pc(BIG_CARD)
    config_ui._update_target_cost()
    assert config_ui.target_cost_panel.result("vram").level is Level.OK
    config_ui.max_zoom_near_airports_slider.setValue(19)
    config_ui.mem_cache_slider.setValue(28)
    config_ui._update_target_cost()
    assert config_ui.target_cost_panel.result("ram").level is Level.BAD


def test_dynamic_fix_replaces_the_steps(qt_app, config_ui):
    config_ui.max_zoom_mode_combo.setCurrentText("Dynamic")
    config_ui._dynamic_zoom_manager.load_from_config([
        {"altitude_ft": 0, "zoom_level": 17, "zoom_level_airports": 19},
        {"altitude_ft": 10000, "zoom_level": 17, "zoom_level_airports": 18},
    ])
    config_ui._update_target_cost()
    vram = config_ui.target_cost_panel.result("vram")
    assert vram.level is Level.BAD
    config_ui._apply_guard_fix(vram.fix)
    zooms = [s.zoom_level for s in config_ui._dynamic_zoom_manager.get_steps()]
    assert max(zooms) < 17


def test_resources_show_in_readiness(qt_app, config_ui):
    readiness = config_ui._run_readiness_checks()
    check = readiness.by_id("resources")
    assert check is not None
    assert check.status is ReadinessStatus.WARNING
    assert "graphics card" in check.message


def test_warnings_off_hides_panel_and_check(qt_app, config_ui):
    config_ui.cfg.guards.warnings = False
    config_ui._update_target_cost()
    assert config_ui.target_cost_panel.isHidden()
    assert config_ui._resource_readiness_check() is None


def test_save_does_not_pin_this_pc_values(qt_app, config_ui):
    assert config_ui.save_config(refresh_scenery=False) is True
    text = open(config_ui.cfg.conf_file, encoding="utf-8").read()
    active = [
        line for line in text.splitlines()
        if line and not line.startswith(("#", "["))
    ]
    for key in (
        "cache_mem_limit",
        "live_builder_concurrency",
        "background_builder_workers",
    ):
        assert not any(line.startswith(f"{key} =") for line in active), key
    assert aoconfig.AOConfig(config_ui.cfg.conf_file).source(
        "cache", "cache_mem_limit"
    ) == "hardware"


def test_apply_refreshes_the_resources_check(qt_app, config_ui, pc):
    config_ui._run_readiness_checks()
    assert config_ui.current_readiness.by_id("resources").status is (
        ReadinessStatus.WARNING
    )
    config_ui.max_zoom_slider.setValue(14)
    config_ui.max_zoom_near_airports_slider.setValue(16)
    assert config_ui.save_config(refresh_scenery=False) is True
    config_ui.settings_session.mark_applied(config_ui._snapshot_settings())
    assert config_ui.current_readiness.by_id("resources").status is (
        ReadinessStatus.SUCCESS
    )

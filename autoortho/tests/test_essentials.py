"""Essentials view, mirrored controls, Home notices and the phase 1-3 gaps."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QSlider, QSpinBox  # noqa: E402

import aoconfig  # noqa: E402
import connection_test  # noqa: E402
import guards  # noqa: E402
import hardware  # noqa: E402
import limits  # noqa: E402
import target_cost as tc  # noqa: E402
from aoconfig import AOConfig  # noqa: E402
from connection_test import Result, Round  # noqa: E402
from guards import GB, Level  # noqa: E402
from hardware import HardwareInfo  # noqa: E402
from ram_brake import DiskBrake  # noqa: E402
from ui.widgets.mirror import mirror  # noqa: E402
from ui.widgets.notice_bar import Notice, NoticeBar  # noqa: E402

PC = HardwareInfo(16, 8, 32 * GB, 12 * GB, "Test GPU", "registry")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(
        connection_test, "RESULT_PATH", str(tmp_path / "connection.json")
    )
    hardware.set_for_tests(PC)
    yield
    hardware.set_for_tests(None)


def _result(provider="BI", mbits=90.0, suggested=64, seconds=0.13):
    return Result(
        "2026-11-12T10:00:00+00:00", provider,
        [Round(suggested, 256, 0, 256 * 16_000, 1.0, seconds, mbits)],
        suggested,
    )


# -- mirror -----------------------------------------------------------------


def test_mirror_both_ways(qt_app):
    owner, proxy = QSlider(), QSpinBox()
    owner.setRange(12, 17)
    owner.setValue(16)
    seen = []
    owner.valueChanged.connect(seen.append)
    mirror(owner, proxy)
    assert (proxy.minimum(), proxy.maximum(), proxy.value()) == (12, 17, 16)
    proxy.setValue(14)                  # the user edits the mirror
    assert owner.value() == 14 and seen == [14]
    owner.setValue(13)                  # Revert or another page
    assert proxy.value() == 13


def test_mirror_kinds(qt_app):
    pairs = [
        (QLineEdit("a"), QLineEdit(), "setText", "b", "text"),
        (QCheckBox(), QCheckBox(), "setChecked", True, "isChecked"),
    ]
    for owner, proxy, setter, value, getter in pairs:
        mirror(owner, proxy)
        getattr(proxy, setter)(value)
        assert getattr(owner, getter)() == value
    owner, proxy = QComboBox(), QComboBox()
    owner.addItems(["BI", "EOX"])
    mirror(owner, proxy)
    assert proxy.count() == 2
    proxy.setCurrentIndex(1)
    assert owner.currentText() == "EOX"
    with pytest.raises(TypeError):
        mirror(QCheckBox(), QLineEdit())


# -- notice bar -------------------------------------------------------------


def test_notice_bar_orders_and_limits(qt_app):
    bar = NoticeBar()
    assert bar.isHidden()
    actions, dismissed = [], []
    bar.action_triggered.connect(lambda n, a: actions.append((n, a)))
    bar.dismissed.connect(dismissed.append)
    bar.set_notices([
        Notice("a", "info", "Info"),
        Notice("b", "error", "Broken", (("fix", "Fix"),)),
        Notice("c", "warning", "Careful"),
        Notice("d", "info", "Extra"),
    ])
    assert [n.id for n in bar.notices] == ["b", "c", "a"]
    assert not bar.isHidden()
    from PySide6.QtWidgets import QPushButton

    buttons = bar.findChildren(QPushButton)
    next(b for b in buttons if b.text() == "Fix").click()
    assert actions == [("b", "fix")]
    next(b for b in buttons if b.text() == "×").click()
    assert dismissed
    bar.set_notices([])
    assert bar.isHidden()


# -- config: kept values, owners, warm start, ceilings ------------------------


def test_old_config_records_kept_engine_values(tmp_path):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 1\n"
        "[autoortho]\nprefetch_interval = 4.0\nmax_zoom = 17\nusing_custom_tiles = True\n"
        "[paths]\ndownload_dir = D:/elsewhere\n",
        encoding="utf-8",
    )
    cfg = AOConfig(str(path))
    # Engine values only: the target, paths and custom tiles are intent.
    assert cfg.general.kept_values == "autoortho.prefetch_interval"
    again = AOConfig(str(path))
    assert again.general.kept_values == "autoortho.prefetch_interval"


def test_registry_owners():
    registry = aoconfig.settings_registry()
    owners = {
        "autoortho.max_zoom": "target",
        "limits.ram_gb": "limit",
        "cache.file_cache_size": "limit",
        "cache.cache_mem_limit": "governed",
        "streaming.paging_k": "measured",
        "streaming.paging_lead_seconds": "governed",
        "paths.cache_dir": "user",
        "seasons.enabled": "user",
        "general.window_x": "internal",
        "pydds.compressor": "fixed",
    }
    for name, owner in owners.items():
        assert registry.get(*name.split(".", 1)).owner == owner, name
    assert registry.get("streaming", "paging_k").live is True
    assert registry.get("streaming", "paging_lead_seconds").live is True
    assert registry.get("autoortho", "prefetch_pipeline_v2").retire_after
    assert all(s.tier.value == "tuning" for s in registry.by_owner("governed"))


def test_warm_start_only_for_the_tested_provider(tmp_path):
    connection_test.save_result(_result(provider="EOX", suggested=256))
    cfg = AOConfig(str(tmp_path / ".autoortho"))
    assert cfg.source("autoortho", "provider_origin_initial_concurrency") == "default"


def test_bandwidth_limit_sets_a_concurrency_ceiling(tmp_path):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 2\n[limits]\nbandwidth_mbps = 20\n",
        encoding="utf-8",
    )
    connection_test.save_result(_result())
    cfg = AOConfig(str(path))
    # 2.5 MB/s at 16 KB per 0.13 s, half again: 31, raised to the floor (64).
    assert cfg.autoortho.provider_origin_max_concurrency == 64
    assert cfg.source("autoortho", "provider_origin_max_concurrency") == "limit"

    path.write_text(
        "[general]\nconfig_revision = 2\n[limits]\nbandwidth_mbps = 400\n",
        encoding="utf-8",
    )
    cfg = AOConfig(str(path))
    # The 90 Mbit/s line is the tighter cap now: 11.25 MB/s at 16 KB per
    # 0.13 s, half again: 138, sized from the line (the tuning plan).
    assert cfg.autoortho.provider_origin_max_concurrency == 138
    assert cfg.source("autoortho", "provider_origin_max_concurrency") == "planned"

    connection_test.save_result(_result(mbits=900.0))
    cfg = AOConfig(str(path))
    # 610 needed for the 400 Mbit/s limit, capped at the requests in flight.
    assert cfg.autoortho.provider_origin_max_concurrency == 320
    assert cfg.source("autoortho", "provider_origin_max_concurrency") == "limit"


# -- guards: disk and limit fixes --------------------------------------------


def test_disk_guard_counts_what_the_cache_may_still_take(monkeypatch):
    cost = tc.target_cost("fixed", 16, 18)
    monkeypatch.setattr(guards, "free_bytes", lambda _p: 40 * GB)
    ok = guards.check_disk("C:/c", cost, cache_limit_gb=50, cache_used_bytes=20 * GB)
    assert ok.level is Level.OK
    tight = guards.check_disk("C:/c", cost, cache_limit_gb=100, cache_used_bytes=10 * GB)
    assert tight.level is Level.WARN
    assert "may still grow by 90 GB" in tight.message
    assert tight.fix.changes == {"cache.file_cache_size": "48"}
    monkeypatch.setattr(guards, "free_bytes", lambda _p: 1 * GB)
    assert guards.check_disk("C:/c", cost, 30).level is Level.BAD


def test_vram_fix_offers_the_automatic_limit():
    results = guards.check_target(
        "fixed", 16, 18, [], PC,
        limits=limits.resolve({"vram_gb": "2"}, PC),
    )
    vram = next(r for r in results if r.resource == "vram")
    assert vram.fix.changes == {"limits.vram_gb": "auto"}


def test_download_fix_raises_a_custom_limit():
    cost = tc.target_cost("fixed", 17, 18)
    result = guards.check_download(
        cost, limits.resolve({"bandwidth_mbps": "8"}, PC), _result(mbits=200)
    )
    assert result.level is Level.WARN
    raised = int(result.fix.changes["limits.bandwidth_mbps"])
    assert cost.peak_rate.mbit_per_s / 0.8 <= raised <= 200


# -- disk brake --------------------------------------------------------------


def test_disk_brake_engages_and_releases():
    now = [0.0]
    free = [10 * GB]
    events = []
    brake = DiskBrake(
        "C:/cache", 2 * GB, reader=lambda _p: free[0], clock=lambda: now[0],
        on_change=lambda engaged, _f: events.append(engaged),
    )
    assert brake.writes_allowed() is True
    free[0] = 1 * GB
    now[0] += 1                      # within 5 s: not sampled again
    assert brake.writes_allowed() is True
    now[0] += 5
    assert brake.writes_allowed() is False
    free[0] = int(2.5 * GB)          # above the margin, not by 1 GB
    now[0] += 6
    assert brake.writes_allowed() is False
    free[0] = 4 * GB
    now[0] += 6
    assert brake.writes_allowed() is True
    assert events == [True, False]
    assert DiskBrake("", 1).writes_allowed() is True


def test_dds_cache_store_respects_the_disk_brake(monkeypatch, tmp_path):
    from aopipeline import dynamic_dds_cache as ddc

    cache = ddc.DynamicDDSCache(cache_dir=str(tmp_path), max_size_mb=64)
    monkeypatch.setattr(ddc, "writes_allowed", lambda: False)
    assert cache.store("1_2_BI_16", 16, b"x" * 4096, None, mm0_manifest={"a": 1}) is False


# -- settings window ----------------------------------------------------------


@pytest.fixture
def config_ui(qt_app, monkeypatch, tmp_path):
    from config_ui_qt import ConfigUI
    from ui.runtime_state import RuntimeState

    monkeypatch.setattr(
        ConfigUI, "refresh_scenery_list",
        lambda self: self.scenery_layout.addStretch(),
    )
    monkeypatch.setattr(ConfigUI, "start_update_check", lambda self: None)
    for name in ("scenery", "cache", "downloads", "X-Plane"):
        (tmp_path / name).mkdir()
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\nconfig_revision = 1\n[autoortho]\nprefetch_interval = 4.0\n"
        "live_builder_concurrency = 6\n",
        encoding="utf-8",
    )
    config = AOConfig(str(path))
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


def test_simple_view_shows_essentials_and_limits(qt_app, config_ui):
    page = config_ui.categorized_settings_page
    assert page.show_all is False
    assert page.visible_category_names() == ["Essentials", "Limits"]
    page.set_show_all(True)
    assert config_ui.cfg.general.show_all_settings is True
    assert "Advanced" in page.visible_category_names()


def test_essentials_edits_drive_the_full_controls(qt_app, config_ui):
    ess = config_ui.essentials_page
    ess.zoom_spin.setValue(15)
    assert config_ui.max_zoom_slider.value() == 15
    ess.maptype_combo.setCurrentText("EOX")
    assert config_ui.maptype_combo.currentText() == "EOX"
    ess.hide_check.setChecked(not config_ui.hide_check.isChecked())
    qt_app.processEvents()
    assert config_ui.settings_session.dirty is True

    config_ui.on_revert()
    assert ess.zoom_spin.value() == config_ui.max_zoom_slider.value()
    assert ess.maptype_combo.currentText() == config_ui.maptype_combo.currentText()


def test_essentials_profile_and_seasons(qt_app, config_ui):
    ess = config_ui.essentials_page
    ess.mode_combo.setCurrentIndex(1)
    assert config_ui.max_zoom_mode_combo.currentText() == "Dynamic"
    ess.profile_combo.setCurrentText("Airliner")
    steps = config_ui._dynamic_zoom_manager.save_to_config()
    ground = next(s for s in steps if s["altitude_ft"] == 0)
    assert ground["zoom_level"] == 16
    assert ess.profile_combo.currentText() == "Airliner"

    ess.seasons_strength.setValue(40)
    assert config_ui.spr_sat_slider.value() == 40
    assert config_ui.win_sat_slider.value() == 40
    config_ui.sum_sat_slider.setValue(80)
    assert ess.seasons_strength.value() == 50


def test_essentials_cost_panel_follows(qt_app, config_ui):
    config_ui._update_target_cost()
    assert config_ui.essentials_page.cost_panel.result("vram") is not None
    assert "AutoOrtho may use" in config_ui.essentials_page.limits_label.text()


def test_min_zoom_is_an_expert_setting(qt_app, config_ui):
    assert config_ui.min_zoom_widget.isHidden()
    page = config_ui.expert_settings_page
    row = next(
        r for r in range(page.model.rowCount())
        if page.model.spec(r).name == "autoortho.min_zoom"
    )
    assert page.model.is_editable(page.model.spec(row))


def test_kept_values_notice_and_automatic_values(qt_app, config_ui):
    notice = config_ui._notices.get("kept_values")
    assert notice is not None
    assert "2 of your own values were kept" in notice.text
    config_ui._on_notice_action("kept_values", "automatic")
    qt_app.processEvents()
    # The live builders control goes back to this PC's value (pending).
    assert config_ui.live_concurrency_slider.value() == int(
        config_ui.cfg.automatic_text("autoortho", "live_builder_concurrency")
    )
    # The prefetch interval's slider holds tenths of a second: back to 1.0 s.
    assert config_ui._control_for("autoortho", "prefetch_interval").value() == 10
    assert "kept_values" not in config_ui._notices
    assert config_ui.settings_session.dirty is True


def test_dismissed_notice_stays_away(qt_app, config_ui):
    config_ui._dismiss_notice("kept_values")
    assert "kept_values" not in config_ui._notices
    reloaded = AOConfig(config_ui.cfg.conf_file)
    assert not str(reloaded.general.kept_values or "")


def test_fix_can_change_limits(qt_app, config_ui):
    config_ui._apply_guard_fix(guards.Fix("x", {"limits.bandwidth_mbps": "40"}))
    assert config_ui.limits_editor.values()["bandwidth_mbps"] == "40"
    config_ui._apply_guard_fix(guards.Fix("y", {"cache.file_cache_size": "55"}))
    assert config_ui.file_cache_slider.value() == 55

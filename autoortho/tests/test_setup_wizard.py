import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWizard

from ui.readiness import ReadinessCheck, ReadinessStatus
from ui.setup_wizard import SetupWizard


def _deps_ready():
    return ReadinessCheck(
        id="setup-dependencies",
        title="FUSE dependencies",
        status=ReadinessStatus.SUCCESS,
        message="ready",
        fix_action="",
    )


def _build_wizard(tmp_path, monkeypatch, scenery_choices=None, installed=False):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    scenery = tmp_path / "scenery"
    scenery.mkdir()
    if installed:
        (scenery / "z_autoortho" / "scenery" / "na").mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    download = tmp_path / "download"
    download.mkdir()
    monkeypatch.setattr("ui.readiness._check_dependencies", _deps_ready)
    wizard = SetupWizard(
        initial_values={
            "xplane_path": str(xplane),
            "scenery_path": str(scenery),
            "cache_dir": str(cache),
            "download_dir": str(download),
            "webui_port": "5847",
            "xplane_udp_port": "49000",
        },
        scenery_choices=scenery_choices or [],
    )
    wizard.show()
    return wizard


def test_wizard_tracks_selected_regions(qt_app, tmp_path, monkeypatch):
    wizard = _build_wizard(
        tmp_path,
        monkeypatch,
        scenery_choices=[
            {"region_id": "na", "title": "North America"},
            {"region_id": "eu", "title": "Europe"},
        ],
    )
    qt_app.processEvents()

    assert wizard.selected_paths["xplane_path"].endswith("X-Plane 12")
    assert wizard.readiness.can_finish is False
    assert wizard.review_page.isComplete() is False

    item = wizard.scenery_page.list_widget.item(0)
    item.setCheckState(Qt.CheckState.Checked)
    qt_app.processEvents()

    assert wizard.selected_region_ids == ("na",)
    assert wizard.readiness.can_finish is True
    assert wizard.review_page.isComplete() is True

    finish_button = wizard.button(QWizard.WizardButton.FinishButton)
    assert finish_button is not None and finish_button.isEnabled()

    wizard.close()
    wizard.deleteLater()


def test_wizard_accepts_established_install_without_selection(qt_app, tmp_path, monkeypatch):
    wizard = _build_wizard(
        tmp_path,
        monkeypatch,
        scenery_choices=[],
        installed=True,
    )
    qt_app.processEvents()

    assert wizard.readiness.installed_scenery_present is True
    assert wizard.readiness.can_finish is True
    assert wizard.review_page.isComplete() is True

    finish_button = wizard.button(QWizard.WizardButton.FinishButton)
    assert finish_button is not None and finish_button.isEnabled()

    wizard.close()
    wizard.deleteLater()


# -- Scenery streaming on the Scenery step ------------------------------------

import tile_stream
from ui.dialogs.setup_wizard import existing_stream
from ui.widgets.stream_panel import LOCKED_HELP, OVERLAY_HELP


def _region_choices():
    return [
        {"region_id": "na", "title": "North America"},
        {"region_id": "eu", "title": "Europe"},
    ]


def _write_stream_package(scenery, name, marker):
    root = scenery / "z_autoortho" / "scenery" / name
    root.mkdir(parents=True)
    tile_stream.write_marker(str(root), marker)


def test_streaming_without_regions_enables_finish(qt_app, tmp_path, monkeypatch):
    wizard = _build_wizard(tmp_path, monkeypatch, scenery_choices=_region_choices())
    page = wizard.scenery_page
    qt_app.processEvents()

    assert page.stream_check.isChecked() is False
    assert wizard.selected_streaming() is None
    assert wizard.readiness.can_finish is False
    # The settings wait until streaming is ticked.
    assert page.stream_source_combo.isEnabled() is False

    page.stream_check.setChecked(True)
    qt_app.processEvents()

    assert page.stream_source_combo.isEnabled() is True
    assert wizard.selected_region_ids == ()
    assert wizard.readiness.can_finish is True
    assert wizard.review_page.isComplete() is True
    assert wizard.button(QWizard.WizardButton.FinishButton).isEnabled()
    assert wizard.selected_streaming() == {
        "source": "xp11", "overlays": True, "disk_limit_gb": 20,
    }
    assert "finish without selecting a region" in page.note_label.text()
    assert wizard.readiness.by_id("setup-scenery").message.startswith(
        "Scenery streaming"
    )
    assert "Scenery streaming: turn on" in wizard.limits_page.details.toPlainText()

    page.stream_check.setChecked(False)
    qt_app.processEvents()
    assert wizard.readiness.can_finish is False

    wizard.close()
    wizard.deleteLater()


def test_streaming_alongside_regions(qt_app, tmp_path, monkeypatch):
    wizard = _build_wizard(tmp_path, monkeypatch, scenery_choices=_region_choices())
    page = wizard.scenery_page
    page.list_widget.item(1).setCheckState(Qt.CheckState.Checked)
    page.stream_check.setChecked(True)
    page.stream_limit_spin.setValue(50)
    page.stream_overlays_check.setChecked(False)
    qt_app.processEvents()

    assert wizard.selected_region_ids == ("eu",)
    assert wizard.selected_streaming() == {
        "source": "xp11", "overlays": False, "disk_limit_gb": 50,
    }
    assert wizard.readiness.can_finish is True
    assert "Streaming covers the rest" in wizard.readiness.by_id("setup-scenery").message

    wizard.close()
    wizard.deleteLater()


def test_streaming_offers_only_available_sets(qt_app, tmp_path, monkeypatch):
    wizard = _build_wizard(tmp_path, monkeypatch)
    page = wizard.scenery_page
    combo = page.stream_source_combo
    model = combo.model()

    assert combo.count() == len(tile_stream.SOURCES)
    for index in range(combo.count()):
        spec = tile_stream.SOURCES[combo.itemData(index)]
        assert model.item(index).isEnabled() is bool(spec.get("available"))
        if not spec.get("available"):
            assert combo.itemText(index).endswith("Soon")
    assert tile_stream.SOURCES[combo.currentData()]["available"] is True
    # The overlays question carries the simHeaven hint.
    assert page.stream_overlays_help.text() == OVERLAY_HELP
    assert "simHeaven" in OVERLAY_HELP

    wizard.close()
    wizard.deleteLater()


def test_unavailable_set_is_never_chosen(qt_app, tmp_path, monkeypatch):
    monkeypatch.setitem(
        tile_stream.SOURCES, "xp11",
        dict(tile_stream.SOURCES["xp11"], available=False),
    )
    wizard = _build_wizard(tmp_path, monkeypatch, scenery_choices=_region_choices())
    wizard.scenery_page.stream_check.setChecked(True)
    qt_app.processEvents()

    assert wizard.selected_streaming() is None
    assert wizard.readiness.can_finish is False

    wizard.close()
    wizard.deleteLater()


def test_streaming_is_locked_while_autoortho_runs(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.readiness._check_dependencies", _deps_ready)
    wizard = SetupWizard(
        initial_values={"scenery_path": str(tmp_path / "scenery")},
        streaming_locked=True,
    )
    page = wizard.scenery_page

    assert page.stream_check.isEnabled() is False
    assert page.stream_status_label.text() == LOCKED_HELP
    page.stream_check.setChecked(True)  # not possible for a user; still ignored
    assert wizard.selected_streaming() is None

    wizard.close()
    wizard.deleteLater()


def test_existing_streaming_is_shown_and_not_set_up_again(qt_app, tmp_path, monkeypatch):
    scenery = tmp_path / "scenery"
    _write_stream_package(scenery, "z_ao_stream_xp11", {
        "source": "xp11", "kind": "ortho", "disk_limit_gb": 40.0,
    })
    assert existing_stream(str(scenery)) == {
        "source": "xp11", "disk_limit_gb": 40.0, "overlays": False,
    }
    monkeypatch.setattr("ui.readiness._check_dependencies", _deps_ready)
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    wizard = SetupWizard(initial_values={
        "xplane_path": str(xplane),
        "scenery_path": str(scenery),
        "cache_dir": str(tmp_path / "cache"),
        "download_dir": str(tmp_path / "download"),
    })
    page = wizard.scenery_page
    qt_app.processEvents()

    assert page.stream_check.isChecked() is True
    assert page.stream_check.isEnabled() is False
    assert page.stream_limit_spin.value() == 40
    assert page.stream_overlays_check.isChecked() is False
    assert "already on" in page.stream_status_label.text()
    assert wizard.selected_streaming() is None
    assert wizard.readiness.can_finish is True

    # Another scenery folder has no streaming: the choice is open again.
    page.wizard_ref.set_value("scenery_path", str(tmp_path / "other"))
    assert page.stream_check.isChecked() is False
    assert page.stream_check.isEnabled() is True

    wizard.close()
    wizard.deleteLater()

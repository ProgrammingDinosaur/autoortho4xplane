import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWizard

from ui.readiness import ReadinessCheck, ReadinessStatus
from ui.setup_wizard import SetupWizard


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


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

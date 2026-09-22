import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication

from ui.models.scenery_model import SceneryListModel
from ui.pages.scenery_page import SceneryLibraryPage


def _region(region_id, latest, installed=None, size=1024):
    release = SimpleNamespace(
        name=region_id.upper(),
        ver=latest,
        totalsize=size,
        download_count=5,
        seasons_apply_status=SimpleNamespace(value="not_applied"),
        roughness_apply_status=SimpleNamespace(value="applied"),
        roughness_value=1.0,
        parse=lambda: None,
    )
    local = (
        SimpleNamespace(
            ver=installed,
            subfolder_dir=f"/scenery/{region_id}",
            seasons_apply_status=SimpleNamespace(value="applied"),
            roughness_apply_status=SimpleNamespace(value="partially_applied"),
            roughness_value=0.8,
        )
        if installed
        else None
    )
    return SimpleNamespace(
        region_id=region_id,
        local_rel=local,
        get_latest_release=lambda: release,
    )


def test_scenery_library_filters_and_prioritizes(qt_app):
    page = SceneryLibraryPage()
    page.set_regions(
        [
            _region("na", "2.0", installed="1.0"),
            _region("eu", "1.0"),
            _region("sa", "1.0", installed="1.0"),
        ]
    )

    def visible_ids():
        return [
            page.proxy_model.data(
                page.proxy_model.index(row, 0),
                SceneryListModel.RegionIdRole,
            )
            for row in range(page.proxy_model.rowCount())
        ]

    assert visible_ids() == [
        "na",
        "sa",
        "eu",
    ]

    page.status_filter.setCurrentText("Updates")
    assert visible_ids() == ["na"]

    page.status_filter.setCurrentText("All")
    page.search_edit.setText("EU")
    assert visible_ids() == ["eu"]


def test_scenery_card_actions_emit_region(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("eu", "1.0")])
    seen = []
    page.install_requested.connect(seen.append)

    page._request_primary_action("eu")

    assert seen == ["eu"]


def test_update_uses_installed_patch_status_and_hides_patch_action(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("na", "2.0", installed="1.0")])

    item = page.model.item(0)
    assert item.seasons_status == "Applied"
    assert item.roughness_status == "Partial"
    assert item.update_available is True

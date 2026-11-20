import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from region_adopt import RegionCheck  # noqa: E402
from ui.pages.scenery_page import SceneryLibraryPage  # noqa: E402


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


def _check(region_id, reason="", tiles=2, size=2 * 1024 ** 3):
    return RegionCheck(name=f"z_ao_{region_id}", region_id=region_id,
                       dsfs=tiles, known=tiles, size=size,
                       tiles=[(i, i) for i in range(tiles)], reason=reason)


def test_only_installed_regions_are_listed(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("eu", "1.0")])
    assert page.cards == [] and page.regions_group.isHidden()

    page.set_regions([_region("na", "2.0", installed="1.0"), _region("eu", "1.0"),
                      _region("sa", "1.0", installed="1.0")])
    assert [c.item.region_id for c in page.cards] == ["na", "sa"]
    assert not page.regions_group.isHidden()


def test_card_shows_patches_and_emits_actions(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("na", "1.0", installed="1.0")])
    seen = []
    page.uninstall_requested.connect(lambda r: seen.append(("remove", r)))
    page.options_requested.connect(lambda r: seen.append(("options", r)))
    (card,) = page.cards
    assert card.item.seasons_status == "Applied"
    assert card.item.roughness_status == "Partial"
    card.options_button.click()
    card.remove_button.click()
    assert seen == [("options", "na"), ("remove", "na")]


def test_adopt_checks_enable_moving_and_explain_the_rest(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("eur", "1.0", installed="1.0"),
                      _region("afr", "1.0", installed="1.0")])
    page.set_adopt_checks(None)
    assert "Checking" in page.adopt_label.text() and not page.adopt_button.isEnabled()

    page.set_adopt_checks([_check("eur"), _check("afr", reason="It is part of the XP11 set.")])
    assert page.adopt_button.isEnabled()
    assert "1 of 2 regions can move (2 tiles, 2.0 GB)" in page.adopt_label.text()
    texts = {c.item.region_id: c.check_label.text() for c in page.cards}
    assert texts == {"eur": "Can move into AutoOrtho scenery.",
                     "afr": "It is part of the XP11 set."}
    seen = []
    page.adopt_requested.connect(lambda: seen.append(True))
    page.adopt_button.click()
    assert seen == [True]
    assert [c.region_id for c in page.movable_regions] == ["eur"]

    page.set_adopt_checks([_check("eur", reason="x"), _check("afr", reason="y")])
    assert not page.adopt_button.isEnabled()
    assert "None of the installed regions" in page.adopt_label.text()


def test_running_locks_region_actions(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("eur", "1.0", installed="1.0")])
    page.set_adopt_checks([_check("eur")])
    page.set_runtime_locked(True)
    assert not page.adopt_button.isEnabled()
    assert not page.cards[0].remove_button.isEnabled()
    assert not page.stream_panel.enable_check.isEnabled()
    page.set_runtime_locked(False)
    assert page.adopt_button.isEnabled() and page.cards[0].remove_button.isEnabled()


def test_removed_region_disappears(qt_app):
    page = SceneryLibraryPage()
    page.set_regions([_region("na", "1.0", installed="1.0")])
    page.set_regions([])
    assert page.cards == [] and page.regions_group.isHidden()

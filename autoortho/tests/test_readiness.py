import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.readiness import (
    ReadinessCheck,
    ReadinessStatus,
    build_readiness,
    detect_xplane_installation,
    format_bytes,
    free_space_bytes,
    nearest_existing_parent,
    package_storage_requirements,
    recursive_directory_usage_bytes,
)


def test_storage_helpers(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    assert nearest_existing_parent(nested) == tmp_path

    file_path = tmp_path / "cache" / "tile.bin"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"abc")
    assert recursive_directory_usage_bytes(file_path.parent) == 3
    assert format_bytes(1536) == "1.5 KB"
    assert free_space_bytes(tmp_path) > 0
    temporary, final = package_storage_requirements(
        10 * 1024 ** 3,
        safety_margin_gb=2,
    )
    assert temporary == 12 * 1024 ** 3
    assert final == 17 * 1024 ** 3


def test_detect_xplane_installation_from_install_txt(tmp_path):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    (xplane / "x-plane_install.txt").write_text(str(xplane))

    assert detect_xplane_installation([tmp_path]) == xplane


def test_build_readiness_uses_configuration_validation(tmp_path, monkeypatch):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    scenery = tmp_path / "scenery"
    scenery.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    download = tmp_path / "download"
    download.mkdir()

    monkeypatch.setattr(
        "ui.readiness._check_dependencies",
        lambda: ReadinessCheck(
            id="setup-dependencies",
            title="FUSE dependencies",
            status=ReadinessStatus.SUCCESS,
            message="ready",
            fix_action="",
        ),
    )

    readiness = build_readiness(
        {
            "xplane_path": str(xplane),
            "scenery_path": str(scenery),
            "cache_dir": str(cache),
            "download_dir": str(download),
            "webui_port": "5847",
            "xplane_udp_port": "49000",
        },
        scenery_choices=[{"region_id": "na", "selected": False}],
    )

    assert readiness.by_id("setup-xplane").status == ReadinessStatus.SUCCESS
    assert readiness.by_id("setup-storage").status == ReadinessStatus.SUCCESS
    assert readiness.by_id("setup-scenery").status == ReadinessStatus.WARNING
    assert readiness.can_finish is False


def test_build_readiness_allows_established_setup(tmp_path, monkeypatch):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    scenery = tmp_path / "scenery"
    installed = scenery / "z_autoortho" / "scenery" / "na"
    installed.mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    download = tmp_path / "download"
    download.mkdir()

    monkeypatch.setattr(
        "ui.readiness._check_dependencies",
        lambda: ReadinessCheck(
            id="setup-dependencies",
            title="FUSE dependencies",
            status=ReadinessStatus.SUCCESS,
            message="ready",
            fix_action="",
        ),
    )

    readiness = build_readiness(
        {
            "xplane_path": str(xplane),
            "scenery_path": str(scenery),
            "cache_dir": str(cache),
            "download_dir": str(download),
            "webui_port": "5847",
            "xplane_udp_port": "49000",
        },
        scenery_choices=[],
    )

    assert readiness.by_id("setup-scenery").status == ReadinessStatus.SUCCESS
    assert readiness.can_finish is True


def test_selected_scenery_blocks_when_storage_is_insufficient(
    tmp_path,
    monkeypatch,
):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    scenery = tmp_path / "scenery"
    scenery.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    download = tmp_path / "download"
    download.mkdir()
    monkeypatch.setattr("ui.readiness._check_dependencies", lambda: ReadinessCheck(
        id="setup-dependencies",
        title="FUSE dependencies",
        status=ReadinessStatus.SUCCESS,
        message="ready",
    ))
    monkeypatch.setattr("ui.readiness.free_space_bytes", lambda path: 1)

    readiness = build_readiness(
        {
            "xplane_path": str(xplane),
            "scenery_path": str(scenery),
            "cache_dir": str(cache),
            "download_dir": str(download),
            "storage_safety_margin_gb": "2",
        },
        scenery_choices=[
            {
                "region_id": "na",
                "selected": True,
                "size_bytes": 10 * 1024 ** 3,
            }
        ],
    )

    assert readiness.by_id("setup-scenery").status == ReadinessStatus.ERROR
    assert readiness.can_finish is False


def test_existing_scenery_does_not_skip_new_region_storage_check(
    tmp_path,
    monkeypatch,
):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    scenery = tmp_path / "scenery"
    (scenery / "z_autoortho" / "scenery" / "installed").mkdir(
        parents=True
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    download = tmp_path / "download"
    download.mkdir()
    monkeypatch.setattr("ui.readiness._check_dependencies", lambda: ReadinessCheck(
        id="setup-dependencies",
        title="FUSE dependencies",
        status=ReadinessStatus.SUCCESS,
        message="ready",
    ))
    monkeypatch.setattr("ui.readiness.free_space_bytes", lambda path: 1)

    readiness = build_readiness(
        {
            "xplane_path": str(xplane),
            "scenery_path": str(scenery),
            "cache_dir": str(cache),
            "download_dir": str(download),
        },
        scenery_choices=[
            {
                "region_id": "new",
                "selected": True,
                "size_bytes": 1024 ** 3,
            }
        ],
    )

    assert readiness.installed_scenery_present is True
    assert readiness.by_id("setup-scenery").status == ReadinessStatus.ERROR
    assert readiness.can_finish is False

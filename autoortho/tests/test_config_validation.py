import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.config_validation import (
    ConfigurationInput,
    ValidationSeverity,
    validate_configuration,
)


def _values(tmp_path, xplane_path):
    return ConfigurationInput(
        xplane_path=str(xplane_path),
        scenery_path=str(tmp_path / "scenery"),
        cache_dir=str(tmp_path / "cache"),
        long_term_cache_dir="",
        download_dir=str(tmp_path / "downloads"),
        webui_port="5847",
        xplane_udp_port="49000",
    )


def test_valid_configuration_with_installed_scenery(tmp_path):
    xplane = tmp_path / "X-Plane"
    (xplane / "Custom Scenery").mkdir(parents=True)
    scenery_root = tmp_path / "scenery" / "z_autoortho" / "scenery" / "na"
    scenery_root.mkdir(parents=True)

    issues = validate_configuration(_values(tmp_path, xplane))

    assert not [
        issue for issue in issues
        if issue.severity == ValidationSeverity.ERROR
    ]


def test_invalid_xplane_path_is_structured_error(tmp_path):
    issues = validate_configuration(
        _values(tmp_path, tmp_path / "missing"),
        require_installed_scenery=False,
    )

    assert any(
        issue.field == "xplane_path"
        and issue.severity == ValidationSeverity.ERROR
        for issue in issues
    )


def test_missing_custom_scenery_is_error(tmp_path):
    xplane = tmp_path / "X-Plane"
    xplane.mkdir()

    issues = validate_configuration(
        _values(tmp_path, xplane),
        require_installed_scenery=False,
    )

    assert any("Custom Scenery" in issue.message for issue in issues)


def test_invalid_ports_are_reported_per_field(tmp_path):
    xplane = tmp_path / "X-Plane"
    (xplane / "Custom Scenery").mkdir(parents=True)
    values = _values(tmp_path, xplane)
    values = ConfigurationInput(
        **{
            **values.__dict__,
            "webui_port": "abc",
            "xplane_udp_port": "70000",
        }
    )

    issues = validate_configuration(
        values,
        require_installed_scenery=False,
    )

    assert {issue.field for issue in issues} >= {
        "webui_port",
        "xplane_udp_port",
    }


def test_incomplete_scenery_is_warning(tmp_path):
    xplane = tmp_path / "X-Plane"
    (xplane / "Custom Scenery").mkdir(parents=True)
    root = tmp_path / "scenery" / "z_autoortho" / "scenery" / "na"
    root.mkdir(parents=True)

    issues = validate_configuration(
        _values(tmp_path, xplane),
        scenery_mounts=[{"root": str(root), "mount": "/unused"}],
    )

    assert any(
        issue.field == "scenery"
        and issue.severity == ValidationSeverity.WARNING
        for issue in issues
    )


def test_stale_mounts_do_not_satisfy_current_scenery_path(tmp_path):
    xplane = tmp_path / "X-Plane"
    (xplane / "Custom Scenery").mkdir(parents=True)
    stale_root = tmp_path / "old-scenery" / "z_autoortho" / "scenery" / "na"
    stale_root.mkdir(parents=True)

    issues = validate_configuration(
        _values(tmp_path, xplane),
        scenery_mounts=[{"root": str(stale_root), "mount": "/unused"}],
    )

    assert any(
        issue.field == "scenery"
        and issue.severity == ValidationSeverity.ERROR
        for issue in issues
    )

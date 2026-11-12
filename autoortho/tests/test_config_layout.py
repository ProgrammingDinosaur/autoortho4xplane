"""Saved config files keep the packaged comment layout."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aoconfig  # noqa: E402


def _lines_before(text, key):
    """The line before a setting, active or written as a commented default."""
    lines = text.splitlines()
    index = next(
        i for i, line in enumerate(lines)
        if line.startswith(f"{key} =") or line.startswith(f"# {key} =")
    )
    return lines[index - 1]


def _active_lines(text):
    return [
        line for line in text.splitlines()
        if line and not line.startswith(("#", "["))
    ]


def test_save_restores_template_comments_and_keeps_values(tmp_path):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[autoortho]\n"
        "# this prevents a single slow chunk from consuming the entire tile budget.\n"
        "# recommended = 250 (fast CPU), 500 (balanced), 1000 (low-end CPU)\n"
        "maxwait = 4.0\n"
        "custom_user_key = 7\n"
        "# stale comment from an old release\n"
        "[my_plugin]\n"
        "enabled = yes\n",
        encoding="utf-8",
    )

    aoconfig.AOConfig(str(path))
    text = path.read_text()

    assert _lines_before(text, "maxwait").startswith(
        "# Recommended: 2.0 (fast networks)"
    )
    assert text.count("250 (fast CPU)") == 1
    assert _lines_before(text, "predictive_dds_build_interval_ms") == (
        "# Recommended: 250 (fast CPU), 500 (balanced), 1000 (low-end CPU)"
    )
    assert "stale comment from an old release" not in text
    assert "recommended =" not in text
    assert "custom_user_key = 7" in text
    assert "[my_plugin]" in text and "enabled = True" in text

    reloaded = aoconfig.AOConfig(str(path))
    assert float(reloaded.autoortho.maxwait) == 4.0
    assert reloaded.autoortho.custom_user_key == 7
    assert path.read_text() == text


def test_render_documents_every_setting_but_retired_ones(tmp_path):
    cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    text = cfg.render_config_text()
    lines = text.splitlines()

    for spec in cfg.registry():
        assert f"[{spec.section}]" in text
        present = any(
            line.startswith(f"{spec.key} =")
            or line.startswith(f"# {spec.key} =")
            for line in lines
        )
        if spec.tier is aoconfig.Tier.RETIRED:
            assert not present, spec.name
        else:
            assert present, spec.name


def test_fresh_config_has_only_the_revision_active(tmp_path):
    path = tmp_path / ".autoortho"
    aoconfig.AOConfig(str(path))
    text = path.read_text(encoding="utf-8")

    assert _active_lines(text) == [
        f"config_revision = {aoconfig.CONFIG_REVISION}"
    ]
    assert "# max_zoom = 16" in text


def test_full_file_from_older_release_becomes_sparse(tmp_path):
    """A pre-sparse file wrote every value: defaults become commented."""
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\n"
        "config_revision = 1\n"
        "gui = True\n"
        "[autoortho]\n"
        "max_zoom = 16\n"
        "maxwait = 4.0\n"
        "max_concurrent_downloads = 256\n"
        "ephemeral_dds_cache_mb = 4096\n",
        encoding="utf-8",
    )

    cfg = aoconfig.AOConfig(str(path))
    text = path.read_text(encoding="utf-8")

    assert _active_lines(text) == [
        f"config_revision = {aoconfig.CONFIG_REVISION}",
        "maxwait = 4.0",
    ]
    assert "max_concurrent_downloads" not in text
    assert "ephemeral_dds_cache_mb" not in text
    assert cfg.source("autoortho", "maxwait") == "custom"
    assert cfg.source("autoortho", "max_zoom") == "default"
    # Retired keys are still readable for code that has not moved on.
    assert cfg.autoortho.max_concurrent_downloads == 256


def test_explicit_line_equal_to_default_stays_until_changed(tmp_path):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\n"
        f"config_revision = {aoconfig.CONFIG_REVISION}\n"
        "[autoortho]\n"
        "max_zoom = 16\n",
        encoding="utf-8",
    )

    cfg = aoconfig.AOConfig(str(path))
    assert cfg.source("autoortho", "max_zoom") == "custom"
    assert "max_zoom = 16" in _active_lines(path.read_text(encoding="utf-8"))

    cfg.autoortho.max_zoom = 17
    cfg.save()
    cfg.autoortho.max_zoom = 16
    cfg.save()
    assert cfg.source("autoortho", "max_zoom") == "default"
    assert "# max_zoom = 16" in path.read_text(encoding="utf-8")


def test_reset_to_default_comments_the_setting(tmp_path):
    path = tmp_path / ".autoortho"
    cfg = aoconfig.AOConfig(str(path))
    cfg.set_value("autoortho", "prefetch_interval", "2.5")
    cfg.save()
    assert "prefetch_interval = 2.5" in path.read_text(encoding="utf-8")
    assert aoconfig.AOConfig(str(path)).autoortho.prefetch_interval == 2.5

    cfg.reset_to_default("autoortho", "prefetch_interval")
    cfg.save()
    assert cfg.autoortho.prefetch_interval == 1.0
    assert "# prefetch_interval = 1.0" in path.read_text(encoding="utf-8")


def test_commented_defaults_are_not_read_as_values(tmp_path):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[general]\n"
        f"config_revision = {aoconfig.CONFIG_REVISION}\n"
        "[autoortho]\n"
        "# max_zoom = 19\n",
        encoding="utf-8",
    )

    cfg = aoconfig.AOConfig(str(path))
    assert cfg.autoortho.max_zoom == 16
    assert not any(k.startswith("#") for k in cfg._explicit_options.get("autoortho", ()))


def test_paths_compare_in_canonical_form(tmp_path):
    cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    default = cfg.registry().get("paths", "cache_dir").default_text
    cfg.paths.cache_dir = default.replace(os.sep, "/") + "/"
    assert cfg.source("paths", "cache_dir") == "default"


def test_file_is_utf8_and_older_locale_files_still_load(tmp_path):
    path = tmp_path / ".autoortho"
    aoconfig.AOConfig(str(path))
    path.read_text(encoding="utf-8")

    path.write_bytes(
        b"[autoortho]\n# an old dash \x97 in cp1252\nmaxwait = 3.0\n"
    )
    assert float(aoconfig.AOConfig(str(path)).autoortho.maxwait) == 3.0


def test_old_config_gets_new_partial_build_default_once(tmp_path):
    path = tmp_path / ".autoortho"
    path.write_text(
        "[autoortho]\n"
        "native_partial_allow_incomplete = False\n",
        encoding="utf-8",
    )

    cfg = aoconfig.AOConfig(str(path))
    assert cfg.autoortho.native_partial_allow_incomplete is True
    assert cfg.general.config_revision == aoconfig.CONFIG_REVISION

    # A user who turns it off again after the migration keeps that choice.
    cfg.autoortho.native_partial_allow_incomplete = False
    cfg.save()
    reloaded = aoconfig.AOConfig(str(path))
    assert reloaded.autoortho.native_partial_allow_incomplete is False


def test_fresh_config_starts_at_current_revision(tmp_path):
    cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    assert cfg.general.config_revision == aoconfig.CONFIG_REVISION
    assert cfg.autoortho.native_partial_allow_incomplete is True

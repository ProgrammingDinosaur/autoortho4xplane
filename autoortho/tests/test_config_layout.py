"""Saved config files keep the packaged comment layout."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aoconfig  # noqa: E402


def _lines_before(text, key):
    lines = text.splitlines()
    index = next(
        i for i, line in enumerate(lines) if line.startswith(f"{key} =")
    )
    return lines[index - 1]


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


def test_render_writes_every_template_setting(tmp_path):
    cfg = aoconfig.AOConfig(str(tmp_path / ".autoortho"))
    text = cfg.render_config_text()

    for section in cfg.config.sections():
        assert f"[{section}]" in text
        for key in cfg.config.options(section):
            if key.startswith("#"):
                continue
            assert any(
                line == key or line.startswith(f"{key} =")
                for line in text.splitlines()
            ), (section, key)


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

"""The settings registry built from the packaged template."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aoconfig  # noqa: E402
from settings_registry import (  # noqa: E402
    RETIRED,
    Tier,
    build_registry,
    parse_value,
)


def _registry():
    return aoconfig.settings_registry()


def test_every_template_setting_is_registered():
    defaults = aoconfig.AOConfig._load_defaults_parser(aoconfig.CFG)
    expected = {
        (section, key)
        for section in defaults.sections()
        for key in defaults.options(section)
        if not key.startswith("#")
    }
    registered = {(spec.section, spec.key) for spec in _registry()}
    assert registered == expected


def test_specs_carry_default_kind_and_help():
    spec = _registry().get("autoortho", "max_zoom")
    assert spec.default == 16
    assert spec.kind == "int"
    assert spec.tier is Tier.ESSENTIAL
    assert "Maximum zoom level" in spec.help
    assert spec.label == "Max zoom"

    assert _registry().get("streaming", "wait_bands").kind == "bool"
    assert _registry().get("autoortho", "maxwait").kind == "float"
    assert _registry().get("autoortho", "missing_color").kind == "list"
    assert _registry().get("paths", "xplane_path").kind == "text"


def test_tiers_follow_the_proposal():
    registry = _registry()
    assert registry.get("general", "window_x").tier is Tier.INTERNAL
    assert registry.get("autoortho", "live_builder_concurrency").tier is Tier.TUNING
    assert registry.get("pydds", "compressor").tier is Tier.EXPERT
    for name, replacement in RETIRED.items():
        section, key = name.split(".")
        spec = registry.get(section, key)
        assert spec.tier is Tier.RETIRED
        assert spec.replaced_by == replacement


def test_provider_ranges_come_from_provider_settings():
    spec = _registry().get("autoortho", "provider_max_in_flight")
    assert spec.range == aoconfig.PROVIDER_SETTINGS["provider_max_in_flight"]["range"]


def test_validate_by_kind_and_range():
    registry = _registry()
    workers = registry.get("autoortho", "live_builder_concurrency")
    assert workers.validate(" 12 ") == (True, "12", "")
    assert workers.validate("40")[0] is False
    assert workers.validate("8.5")[0] is False

    flag = registry.get("streaming", "wait_bands")
    assert flag.validate("off") == (True, "False", "")
    assert flag.validate("maybe")[0] is False

    colour = registry.get("autoortho", "missing_color")
    assert colour.validate("[1, 2, 3]") == (True, "[1, 2, 3]", "")
    assert colour.validate("red")[0] is False


def test_parse_value_matches_section_parser():
    for raw in ("True", "off", "0", "1", "-3", "2.5", "1e3", "[1, 2]", "abc", "", None):
        parsed = aoconfig.SectionParser(v=raw).v
        assert parsed == parse_value(raw)
        assert type(parsed) is type(parse_value(raw))


def test_help_resets_at_blank_lines_and_sections():
    registry = build_registry(
        "[a]\n# about x\nx = 1\n\n# about y\ny = yes\n[b]\nz = text\n"
    )
    assert registry.get("a", "x").help == "about x"
    assert registry.get("a", "y").help == "about y"
    assert registry.get("b", "z").help == ""

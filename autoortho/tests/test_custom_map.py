"""Tests for CustomMapConfig."""
import json
import os
import sys
import tempfile
import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.custom_map import CustomMapConfig, _cell_key


@pytest.fixture
def config(tmp_path):
    """Create a CustomMapConfig with a temp file."""
    path = str(tmp_path / "custom_map.json")
    return CustomMapConfig(path=path)


def test_cell_key_floor():
    assert _cell_key(48.7, -122.3) == "48,-123"


def test_negative_coords():
    assert _cell_key(-33.9, -70.6) == "-34,-71"


def test_cell_key_exact_integer():
    assert _cell_key(48.0, -122.0) == "48,-122"


def test_get_maptype_hit_miss(config):
    config.set_cells({"48,-122": "BI"})
    assert config.get_maptype(48.5, -121.5) == "BI"
    assert config.get_maptype(49.0, -121.5) is None


def test_load_save_roundtrip(tmp_path):
    path = str(tmp_path / "custom_map.json")
    c1 = CustomMapConfig(path=path)
    c1.set_cells({"48,-122": "BI", "37,-122": "USGS"})

    c2 = CustomMapConfig(path=path)
    assert c2.get_all_cells() == {"48,-122": "BI", "37,-122": "USGS"}


def test_import_export_roundtrip(config):
    config.set_cells({"48,-122": "BI", "37,-122": "USGS"})
    exported = config.export_json()
    data = json.loads(exported)
    assert data["version"] == 1
    assert data["cells"] == {"48,-122": "BI", "37,-122": "USGS"}

    config.clear()
    assert config.get_all_cells() == {}

    config.import_json(exported)
    assert config.get_all_cells() == {"48,-122": "BI", "37,-122": "USGS"}


def test_import_merge_vs_replace(config):
    config.set_cells({"48,-122": "BI", "37,-122": "USGS"})

    # Replace mode: clears existing
    config.import_json(json.dumps({"version": 1, "cells": {"10,20": "EOX"}}), merge=False)
    assert config.get_all_cells() == {"10,20": "EOX"}

    # Merge mode: overlays on existing
    config.import_json(json.dumps({"version": 1, "cells": {"48,-122": "NAIP"}}), merge=True)
    assert config.get_all_cells() == {"10,20": "EOX", "48,-122": "NAIP"}


def test_clear(config):
    config.set_cells({"48,-122": "BI"})
    assert len(config.get_all_cells()) == 1
    config.clear()
    assert config.get_all_cells() == {}


def test_remove_cells(config):
    config.set_cells({"48,-122": "BI", "37,-122": "USGS"})
    config.remove_cells(["48,-122", "nonexistent"])
    assert config.get_all_cells() == {"37,-122": "USGS"}


def test_import_invalid_json(config):
    with pytest.raises(ValueError, match="missing 'cells' key"):
        config.import_json(json.dumps({"bad": "format"}))

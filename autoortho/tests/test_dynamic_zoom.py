#!/usr/bin/env python3
"""
Unit tests for the dynamic zoom management system.

Tests cover:
- QualityStep dataclass creation and serialization
- DynamicZoomManager step management (add/remove/update)
- Zoom level computation for different altitudes
- Config loading and saving
- Edge cases and validation
"""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.dynamic_zoom import (
    QualityStep,
    DynamicZoomManager,
    BASE_ALTITUDE_FT,
    DEFAULT_ZOOM_LEVEL,
    MIN_ZOOM_LEVEL,
    MAX_ZOOM_LEVEL,
)


# =============================================================================
# QualityStep Tests
# =============================================================================


class TestQualityStep:
    """Tests for QualityStep dataclass."""

    def test_basic_creation(self):
        """Test creating a QualityStep with valid values."""
        step = QualityStep(altitude_ft=20000, zoom_level=15)
        assert step.altitude_ft == 20000
        assert step.zoom_level == 15

    def test_string_input_conversion(self):
        """Test that string inputs are converted to int."""
        step = QualityStep(altitude_ft="20000", zoom_level="15")
        assert step.altitude_ft == 20000
        assert step.zoom_level == 15
        assert isinstance(step.altitude_ft, int)
        assert isinstance(step.zoom_level, int)

    def test_zoom_level_clamping_high(self):
        """Test that zoom levels above MAX are clamped."""
        step = QualityStep(altitude_ft=0, zoom_level=25)
        assert step.zoom_level == MAX_ZOOM_LEVEL

    def test_zoom_level_clamping_low(self):
        """Test that zoom levels below MIN are clamped."""
        step = QualityStep(altitude_ft=0, zoom_level=5)
        assert step.zoom_level == MIN_ZOOM_LEVEL

    def test_negative_altitude(self):
        """Test that negative altitudes are allowed."""
        step = QualityStep(altitude_ft=-1000, zoom_level=17)
        assert step.altitude_ft == -1000

    def test_to_dict(self):
        """Test serialization to dictionary."""
        step = QualityStep(altitude_ft=25000, zoom_level=15)
        d = step.to_dict()
        assert d == {
            "altitude_ft": 25000,
            "zoom_level": 15,
            "zoom_level_airports": 18,
        }

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {"altitude_ft": 30000, "zoom_level": 14}
        step = QualityStep.from_dict(data)
        assert step.altitude_ft == 30000
        assert step.zoom_level == 14

    def test_from_dict_with_string_values(self):
        """Test deserialization handles string values from config."""
        data = {"altitude_ft": "35000", "zoom_level": "13"}
        step = QualityStep.from_dict(data)
        assert step.altitude_ft == 35000
        assert step.zoom_level == 13

    def test_from_dict_missing_key_raises(self):
        """Test that missing keys raise KeyError."""
        with pytest.raises(KeyError):
            QualityStep.from_dict({"altitude_ft": 10000})

        with pytest.raises(KeyError):
            QualityStep.from_dict({"zoom_level": 15})

    def test_from_dict_invalid_value_raises(self):
        """Test that non-numeric values raise ValueError."""
        with pytest.raises(ValueError):
            QualityStep.from_dict({"altitude_ft": "not_a_number", "zoom_level": 15})

    def test_repr(self):
        """Test string representation."""
        step = QualityStep(altitude_ft=20000, zoom_level=15)
        repr_str = repr(step)
        assert "20000" in repr_str
        assert "15" in repr_str

    def test_roundtrip(self):
        """Test that to_dict -> from_dict preserves values."""
        original = QualityStep(altitude_ft=12345, zoom_level=16)
        recreated = QualityStep.from_dict(original.to_dict())
        assert original.altitude_ft == recreated.altitude_ft
        assert original.zoom_level == recreated.zoom_level


# =============================================================================
# DynamicZoomManager Tests - Basic Operations
# =============================================================================


class TestDynamicZoomManagerBasic:
    """Tests for basic DynamicZoomManager operations."""

    def test_initialization(self):
        """Test that manager starts empty."""
        manager = DynamicZoomManager()
        assert manager.is_empty()
        assert manager.step_count() == 0

    def test_add_single_step(self):
        """Test adding a single step."""
        manager = DynamicZoomManager()
        result = manager.add_step(20000, 15)
        assert result is True
        assert manager.step_count() == 1

    def test_add_duplicate_altitude_fails(self):
        """Test that duplicate altitudes are rejected."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)
        result = manager.add_step(20000, 14)  # Same altitude
        assert result is False
        assert manager.step_count() == 1

    def test_add_multiple_steps(self):
        """Test adding multiple steps."""
        manager = DynamicZoomManager()
        manager.add_step(10000, 16)
        manager.add_step(20000, 15)
        manager.add_step(30000, 14)
        assert manager.step_count() == 3

    def test_remove_step(self):
        """Test removing an existing step."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)
        result = manager.remove_step(20000)
        assert result is True
        assert manager.is_empty()

    def test_remove_nonexistent_step(self):
        """Test removing a step that doesn't exist."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)
        result = manager.remove_step(30000)
        assert result is False
        assert manager.step_count() == 1

    def test_remove_base_step_fails(self):
        """Test that base step cannot be removed."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        result = manager.remove_step(BASE_ALTITUDE_FT)
        assert result is False
        assert manager.step_count() == 1

    def test_update_step(self):
        """Test updating zoom level of existing step."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)
        result = manager.update_step(20000, 14)
        assert result is True
        # Verify the change
        steps = manager.get_steps()
        assert steps[0].zoom_level == 14

    def test_update_nonexistent_step(self):
        """Test updating a step that doesn't exist."""
        manager = DynamicZoomManager()
        result = manager.update_step(20000, 15)
        assert result is False

    def test_update_clamps_zoom(self):
        """Test that update clamps zoom to valid range."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)
        manager.update_step(20000, 99)
        steps = manager.get_steps()
        assert steps[0].zoom_level == MAX_ZOOM_LEVEL

    def test_clear(self):
        """Test clearing all steps."""
        manager = DynamicZoomManager()
        manager.add_step(10000, 16)
        manager.add_step(20000, 15)
        manager.clear()
        assert manager.is_empty()


# =============================================================================
# DynamicZoomManager Tests - Base Step
# =============================================================================


class TestDynamicZoomManagerBaseStep:
    """Tests for base step handling."""

    def test_set_base_zoom_creates_step(self):
        """Test that set_base_zoom creates base step if missing."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        assert manager.has_base_step()
        base = manager.get_base_step()
        assert base.altitude_ft == BASE_ALTITUDE_FT
        assert base.zoom_level == 17

    def test_set_base_zoom_updates_existing(self):
        """Test that set_base_zoom updates existing base step."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.set_base_zoom(16)
        assert manager.step_count() == 1  # Still just one step
        assert manager.get_base_step().zoom_level == 16

    def test_has_base_step_false(self):
        """Test has_base_step when no base step exists."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)  # Not a base step
        assert not manager.has_base_step()

    def test_get_base_step_none(self):
        """Test get_base_step returns None when missing."""
        manager = DynamicZoomManager()
        assert manager.get_base_step() is None

    def test_initialize_from_fixed_zoom(self):
        """Test initialization from fixed zoom value."""
        manager = DynamicZoomManager()
        manager.initialize_from_fixed_zoom(17)
        assert manager.step_count() == 1
        assert manager.has_base_step()
        assert manager.get_base_step().zoom_level == 17

    def test_initialize_from_fixed_zoom_clamps(self):
        """Test that initialization clamps invalid zoom."""
        manager = DynamicZoomManager()
        manager.initialize_from_fixed_zoom(99)
        assert manager.get_base_step().zoom_level == MAX_ZOOM_LEVEL


# =============================================================================
# DynamicZoomManager Tests - Zoom Computation
# =============================================================================


class TestDynamicZoomManagerZoomComputation:
    """Tests for zoom level computation based on altitude."""

    def test_empty_manager_returns_default(self):
        """Test that empty manager returns default zoom."""
        manager = DynamicZoomManager()
        assert manager.get_zoom_for_altitude(10000) == DEFAULT_ZOOM_LEVEL

    def test_single_base_step(self):
        """Test with only base step - all altitudes use it."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)

        assert manager.get_zoom_for_altitude(-500) == 17
        assert manager.get_zoom_for_altitude(0) == 17
        assert manager.get_zoom_for_altitude(10000) == 17
        assert manager.get_zoom_for_altitude(40000) == 17

    def test_two_steps(self):
        """Test with two steps."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)  # Ground level
        manager.add_step(20000, 15)  # Above 20,000 ft

        assert manager.get_zoom_for_altitude(-500) == 17  # Below base
        assert manager.get_zoom_for_altitude(0) == 17  # At ground
        assert manager.get_zoom_for_altitude(19999) == 17  # Just below 20k
        assert manager.get_zoom_for_altitude(20000) == 15  # Exactly at 20k
        assert manager.get_zoom_for_altitude(25000) == 15  # Above 20k

    def test_multiple_steps(self):
        """Test with multiple altitude steps."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(10000, 16)
        manager.add_step(25000, 15)
        manager.add_step(40000, 14)

        # Below all steps
        assert manager.get_zoom_for_altitude(-500) == 17

        # In each range
        assert manager.get_zoom_for_altitude(5000) == 17  # Below 10k
        assert manager.get_zoom_for_altitude(15000) == 16  # 10k-25k
        assert manager.get_zoom_for_altitude(30000) == 15  # 25k-40k
        assert manager.get_zoom_for_altitude(45000) == 14  # Above 40k

        # At exact thresholds
        assert manager.get_zoom_for_altitude(10000) == 16
        assert manager.get_zoom_for_altitude(25000) == 15
        assert manager.get_zoom_for_altitude(40000) == 14

    def test_float_altitude(self):
        """Test that float altitudes work correctly."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)

        assert manager.get_zoom_for_altitude(19999.9) == 17
        assert manager.get_zoom_for_altitude(20000.0) == 15
        assert manager.get_zoom_for_altitude(20000.1) == 15

    def test_negative_altitude(self):
        """Test handling of negative altitudes (below sea level)."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)

        # Altitude below base step threshold
        assert manager.get_zoom_for_altitude(-2000) == 17

    def test_steps_sorted_correctly(self):
        """Test that steps are returned sorted by altitude descending."""
        manager = DynamicZoomManager()
        # Add in random order
        manager.add_step(20000, 15)
        manager.add_step(40000, 14)
        manager.add_step(10000, 16)
        manager.set_base_zoom(17)

        steps = manager.get_steps()
        altitudes = [s.altitude_ft for s in steps]
        assert altitudes == sorted(altitudes, reverse=True)


# =============================================================================
# DynamicZoomManager Tests - Config Loading/Saving
# =============================================================================


class TestDynamicZoomManagerConfig:
    """Tests for config loading and saving."""

    def test_save_to_config_empty(self):
        """Test saving empty config."""
        manager = DynamicZoomManager()
        result = manager.save_to_config()
        assert result == []

    def test_save_to_config_single_step(self):
        """Test saving single step."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        result = manager.save_to_config()
        assert len(result) == 1
        assert result[0] == {
            "altitude_ft": BASE_ALTITUDE_FT,
            "zoom_level": 17,
            "zoom_level_airports": 18,
        }

    def test_save_to_config_multiple_steps(self):
        """Test saving multiple steps."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)
        result = manager.save_to_config()
        assert len(result) == 2

    def test_load_from_config_list(self):
        """Test loading from parsed list (normal case from SectionParser)."""
        manager = DynamicZoomManager()
        config_value = [
            {"altitude_ft": -1000, "zoom_level": 17},
            {"altitude_ft": 20000, "zoom_level": 15},
        ]
        manager.load_from_config(config_value)
        assert manager.step_count() == 2
        assert manager.get_zoom_for_altitude(0) == 17
        assert manager.get_zoom_for_altitude(25000) == 15

    def test_load_from_config_none(self):
        """Test loading from None value."""
        manager = DynamicZoomManager()
        manager.add_step(10000, 16)  # Pre-existing step
        manager.load_from_config(None)
        assert manager.is_empty()

    def test_load_from_config_empty_list(self):
        """Test loading from empty list."""
        manager = DynamicZoomManager()
        manager.load_from_config([])
        assert manager.is_empty()

    def test_load_from_config_empty_string(self):
        """Test loading from empty string representation."""
        manager = DynamicZoomManager()
        manager.load_from_config("[]")
        assert manager.is_empty()

    def test_load_from_config_skips_invalid(self):
        """Test that invalid entries are skipped."""
        manager = DynamicZoomManager()
        config_value = [
            {"altitude_ft": 20000, "zoom_level": 15},  # Valid
            {"altitude_ft": "not_a_number", "zoom_level": 15},  # Invalid
            "not_a_dict",  # Invalid
            {"zoom_level": 15},  # Missing altitude_ft
            {"altitude_ft": 30000, "zoom_level": 14},  # Valid
        ]
        manager.load_from_config(config_value)
        assert manager.step_count() == 2  # Only valid entries

    def test_roundtrip_config(self):
        """Test that save -> load preserves data."""
        manager1 = DynamicZoomManager()
        manager1.set_base_zoom(17)
        manager1.add_step(15000, 16)
        manager1.add_step(30000, 14)

        saved = manager1.save_to_config()

        manager2 = DynamicZoomManager()
        manager2.load_from_config(saved)

        assert manager2.step_count() == manager1.step_count()
        assert manager2.get_zoom_for_altitude(0) == manager1.get_zoom_for_altitude(0)
        assert manager2.get_zoom_for_altitude(20000) == manager1.get_zoom_for_altitude(20000)
        assert manager2.get_zoom_for_altitude(40000) == manager1.get_zoom_for_altitude(40000)


# =============================================================================
# DynamicZoomManager Tests - Utility Methods
# =============================================================================


class TestDynamicZoomManagerUtility:
    """Tests for utility methods."""

    def test_get_max_zoom_levels_empty_uses_defaults(self):
        """Empty manager returns runtime fallback zoom limits."""
        manager = DynamicZoomManager()
        assert manager.get_max_zoom_levels() == (DEFAULT_ZOOM_LEVEL, 18)

    def test_get_max_zoom_levels_uses_highest_configured_values(self):
        """Maxima are computed independently for regular and airport zooms."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(16, 18)
        manager.add_step(15000, 15, 16)
        manager.add_step(30000, 14, 17)
        assert manager.get_max_zoom_levels() == (16, 18)

    def test_get_max_zoom_levels_includes_missing_base_fallbacks(self):
        """Missing base step still includes runtime fallback defaults."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 14, 15)
        assert manager.get_max_zoom_levels() == (DEFAULT_ZOOM_LEVEL, 18)

    def test_get_max_zoom_levels_tracks_airport_zoom_independently(self):
        """Airport max can exceed regular max without changing regular max."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(15, 18)
        manager.add_step(25000, 14, 19)
        assert manager.get_max_zoom_levels() == (15, 19)

    def test_get_summary_empty(self):
        """Test summary for empty manager."""
        manager = DynamicZoomManager()
        assert manager.get_summary() == "No steps configured"

    def test_get_summary_single_step(self):
        """Test summary for single step."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        summary = manager.get_summary()
        assert "ZL17" in summary
        assert f"{BASE_ALTITUDE_FT:+}" in summary

    def test_get_summary_multiple_steps(self):
        """Test summary for multiple steps."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)
        summary = manager.get_summary()
        assert "ZL17" in summary
        assert "ZL15" in summary

    def test_validate_empty(self):
        """Test validation of empty manager."""
        manager = DynamicZoomManager()
        warnings = manager.validate()
        assert len(warnings) > 0
        assert "No quality steps" in warnings[0]

    def test_validate_missing_base(self):
        """Test validation warns about missing base step."""
        manager = DynamicZoomManager()
        manager.add_step(20000, 15)  # No base step
        warnings = manager.validate()
        assert any("base step" in w.lower() for w in warnings)

    def test_validate_valid_config(self):
        """Test validation of valid config with base step."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)
        manager.add_step(40000, 14)
        warnings = manager.validate()
        # Should have no warnings for this standard config
        assert len(warnings) == 0

    def test_validate_unusual_zoom_order(self):
        """Test validation warns about unusual zoom ordering."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(15)  # Lower zoom at ground
        manager.add_step(20000, 17)  # Higher zoom at altitude (unusual)
        warnings = manager.validate()
        assert any("unusual" in w.lower() for w in warnings)

    def test_repr(self):
        """Test string representation of manager."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        repr_str = repr(manager)
        assert "DynamicZoomManager" in repr_str
        assert "steps=1" in repr_str


# =============================================================================
# Integration Tests
# =============================================================================


class TestDynamicZoomIntegration:
    """Integration tests simulating real usage patterns."""

    def test_typical_vfr_setup(self):
        """Test a typical VFR flying setup."""
        manager = DynamicZoomManager()
        # Ground level: high detail
        manager.set_base_zoom(17)
        # Pattern altitude: still high detail
        manager.add_step(3000, 16)
        # Cruise: reduced detail
        manager.add_step(10000, 15)

        # Parked
        assert manager.get_zoom_for_altitude(0) == 17
        # Taking off
        assert manager.get_zoom_for_altitude(500) == 17
        # In pattern
        assert manager.get_zoom_for_altitude(3500) == 16
        # Climbing out
        assert manager.get_zoom_for_altitude(8000) == 16
        # Cruise
        assert manager.get_zoom_for_altitude(12000) == 15

    def test_typical_airliner_setup(self):
        """Test a typical airliner flying setup."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)  # Ground
        manager.add_step(10000, 16)  # Climb/descent
        manager.add_step(25000, 15)  # Mid altitude
        manager.add_step(35000, 14)  # High altitude

        # FL350 cruise
        assert manager.get_zoom_for_altitude(35000) == 14
        # FL410 cruise
        assert manager.get_zoom_for_altitude(41000) == 14
        # Approach
        assert manager.get_zoom_for_altitude(5000) == 17
        # Final
        assert manager.get_zoom_for_altitude(500) == 17

    def test_transition_from_fixed_mode(self):
        """Test transitioning from fixed mode to dynamic mode."""
        # Simulate user had max_zoom = 16 in fixed mode
        fixed_zoom = 16

        manager = DynamicZoomManager()
        manager.initialize_from_fixed_zoom(fixed_zoom)

        # Initially, all altitudes should use the original fixed zoom
        assert manager.get_zoom_for_altitude(0) == 16
        assert manager.get_zoom_for_altitude(40000) == 16

        # User adds high altitude step
        manager.add_step(30000, 14)

        # Now behavior changes
        assert manager.get_zoom_for_altitude(0) == 16
        assert manager.get_zoom_for_altitude(40000) == 14

    def test_config_persistence_simulation(self):
        """Simulate saving to config and loading in new session."""
        # First session: user configures dynamic zoom
        manager1 = DynamicZoomManager()
        manager1.initialize_from_fixed_zoom(17)
        manager1.add_step(20000, 15)
        manager1.add_step(40000, 13)

        # Simulate save to config (what aoconfig.set_config does)
        config_value = manager1.save_to_config()

        # Simulate what str() does when saving
        config_string = str(config_value)

        # Verify the string can be parsed back
        # (SectionParser uses ast.literal_eval)
        import ast
        parsed = ast.literal_eval(config_string)

        # Second session: load from config
        manager2 = DynamicZoomManager()
        manager2.load_from_config(parsed)

        # Verify same behavior
        assert manager2.get_zoom_for_altitude(0) == 17
        assert manager2.get_zoom_for_altitude(25000) == 15
        assert manager2.get_zoom_for_altitude(45000) == 13

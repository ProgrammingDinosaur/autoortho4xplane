#!/usr/bin/env python3
"""
Unit tests for QualityStepsDialog and dynamic zoom UI components.

These tests focus on UI-specific logic and data flow rather than actual UI rendering,
as full Qt widget testing requires a running QApplication.

Note: Core DynamicZoomManager functionality (add/remove/update steps, zoom computation,
serialization) is tested in test_dynamic_zoom.py. This file tests only the UI integration
aspects that are not covered there.

Tests cover:
- Dialog initialization behavior (what happens when dialog is created)
- Mode switching logic (fixed <-> dynamic)
- ConfigUI integration patterns (how config values flow)
- Table editing simulation (altitude changes via table edits)
"""

import pytest
import sys
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from config_ui_qt import QualityStepsDialog
from utils.dynamic_zoom import DynamicZoomManager, BASE_ALTITUDE_FT


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


# =============================================================================
# QualityStepsDialog Initialization Tests
# =============================================================================


class TestQualityStepsDialogInitialization:
    """Tests for dialog initialization behavior."""

    def test_dialog_creates_manager_when_none_provided(self):
        """Simulate dialog creating its own manager when None is passed."""
        # This simulates: manager = manager or DynamicZoomManager()
        provided_manager = None
        manager = provided_manager or DynamicZoomManager()
        assert manager.is_empty()

    def test_dialog_uses_existing_manager(self, qt_app):
        """The dialog edits an independent working copy."""
        existing = DynamicZoomManager()
        existing.set_base_zoom(17)
        existing.add_step(20000, 15)

        dialog = QualityStepsDialog(manager=existing)

        assert dialog.manager is not existing
        assert dialog.manager.save_to_config() == existing.save_to_config()


# =============================================================================
# Mode Switching Logic Tests
# =============================================================================


class TestZoomModeSwitch:
    """Tests for switching between fixed and dynamic zoom modes."""

    def test_switch_to_dynamic_first_time(self):
        """Test first-time switch to dynamic mode initializes from fixed zoom."""
        # User had fixed zoom at 16
        current_fixed_zoom = 16
        manager = DynamicZoomManager()

        # When switching to dynamic mode for first time:
        # UI should initialize manager from current fixed zoom if empty
        if manager.is_empty():
            manager.set_base_zoom(current_fixed_zoom)

        assert manager.has_base_step()
        assert manager.get_base_step().zoom_level == 16

    def test_switch_to_dynamic_preserves_existing_steps(self):
        """Test that switching back to dynamic mode preserves user's steps."""
        # User previously configured dynamic zoom
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)
        manager.add_step(40000, 13)

        # User switches to fixed, then back to dynamic
        # The manager should NOT be re-initialized
        if manager.is_empty():
            manager.set_base_zoom(16)  # This should NOT happen

        # Steps should be preserved
        assert manager.step_count() == 3
        assert manager.get_zoom_for_altitude(30000) == 15

    def test_mode_string_normalization(self):
        """Test that mode string is normalized correctly for config."""
        # UI combo box text -> config value mapping
        test_cases = [
            ("Fixed", "fixed"),
            ("Dynamic", "dynamic"),
        ]
        for ui_text, expected_config in test_cases:
            config_value = "dynamic" if ui_text == "Dynamic" else "fixed"
            assert config_value == expected_config


# =============================================================================
# Table Editing Simulation Tests
# =============================================================================


class TestTableEditSimulation:
    """Tests simulating table edits in QualityStepsDialog."""

    def test_edit_altitude_in_table_creates_new_step(self):
        """Simulate user changing an altitude value in the table."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)

        # User edits altitude from 20000 to 25000
        # This requires: 1) remove old step, 2) add new step
        old_altitude = 20000
        new_altitude = 25000
        old_zoom = 15

        # Find the old step's zoom level before removing
        steps = manager.get_steps()
        step = next((s for s in steps if s.altitude_ft == old_altitude), None)
        if step:
            old_zoom = step.zoom_level
            manager.remove_step(old_altitude)
            manager.add_step(new_altitude, old_zoom)

        # Verify the change
        assert manager.step_count() == 2  # base + 25000
        assert manager.get_zoom_for_altitude(25000) == 15
        # Old altitude should now fall through to base
        assert manager.get_zoom_for_altitude(20000) == 17

    def test_edit_zoom_in_table(self):
        """Simulate user changing a zoom value in the table."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)

        # User edits zoom from 15 to 14
        manager.update_step(20000, 14)

        assert manager.get_zoom_for_altitude(25000) == 14

    def test_edit_base_step_altitude_blocked(self):
        """Test that base step altitude cannot be edited (protected)."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)

        # Base step should NOT be removable (required for the system to work)
        # UI should prevent this by disabling the altitude cell for base step
        result = manager.remove_step(BASE_ALTITUDE_FT)
        assert result is False
        assert manager.has_base_step()


# =============================================================================
# ConfigUI Integration Pattern Tests
# =============================================================================


class TestConfigUIIntegrationPatterns:
    """Tests for patterns used when integrating with ConfigUI."""

    def test_init_manager_from_existing_config(self):
        """Test the pattern for initializing manager from saved config."""
        # Simulate config values from CFG.autoortho.dynamic_zoom_steps
        saved_config = [
            {"altitude_ft": -1000, "zoom_level": 17},
            {"altitude_ft": 25000, "zoom_level": 15},
        ]

        manager = DynamicZoomManager()
        if saved_config and saved_config != []:
            manager.load_from_config(saved_config)
        else:
            # Fall back to current fixed zoom
            manager.set_base_zoom(16)

        assert manager.step_count() == 2
        assert manager.get_zoom_for_altitude(30000) == 15

    def test_init_manager_from_empty_config(self):
        """Test the pattern when config is empty (first-time user)."""
        saved_config = []
        current_fixed_zoom = 16

        manager = DynamicZoomManager()
        if saved_config and saved_config != []:
            manager.load_from_config(saved_config)
        else:
            manager.set_base_zoom(current_fixed_zoom)

        assert manager.step_count() == 1
        assert manager.get_base_step().zoom_level == 16

    def test_save_config_pattern(self):
        """Test the pattern for saving config values."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)
        manager.add_step(20000, 15)

        # Pattern used in ConfigUI.save_config:
        zoom_mode = "dynamic"
        dynamic_zoom_steps = manager.save_to_config()

        # These would be assigned to cfg.autoortho attributes
        assert zoom_mode == "dynamic"
        assert isinstance(dynamic_zoom_steps, list)
        assert len(dynamic_zoom_steps) == 2


# =============================================================================
# Edge Cases for UI Interactions
# =============================================================================


class TestUIEdgeCases:
    """Tests for edge cases specific to UI interactions."""

    def test_add_step_at_ground_level(self):
        """The required ground-level step cannot be duplicated."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)

        result = manager.add_step(0, 16)
        assert result is False
        assert manager.get_zoom_for_altitude(0) == 17

    def test_dialog_with_many_steps(self):
        """Test dialog behavior with many altitude steps."""
        manager = DynamicZoomManager()
        manager.set_base_zoom(17)

        # Add many steps (user might have complex setup)
        for alt in range(5000, 50000, 5000):
            zoom = max(12, 17 - alt // 10000)
            manager.add_step(alt, zoom)

        # Dialog should handle this gracefully
        assert manager.step_count() > 5
        # All altitudes should still resolve correctly
        assert manager.get_zoom_for_altitude(0) == 17
        assert manager.get_zoom_for_altitude(50000) is not None

    def test_steps_added_in_random_order(self):
        """Test that step order in dialog doesn't affect behavior."""
        manager = DynamicZoomManager()

        # User might add steps in any order
        manager.add_step(30000, 14)
        manager.set_base_zoom(17)
        manager.add_step(10000, 16)
        manager.add_step(20000, 15)

        # get_steps should return sorted for display
        steps = manager.get_steps()
        altitudes = [s.altitude_ft for s in steps]
        assert altitudes == sorted(altitudes, reverse=True)

        # Zoom lookups should work correctly regardless
        assert manager.get_zoom_for_altitude(5000) == 17
        assert manager.get_zoom_for_altitude(15000) == 16
        assert manager.get_zoom_for_altitude(25000) == 15
        assert manager.get_zoom_for_altitude(35000) == 14


class TestQualityStepsDialogInteractions:
    def test_cancel_discards_removed_step(self, qt_app):
        manager = DynamicZoomManager()
        manager.set_base_zoom(17, 18)
        manager.add_step(20000, 15, 16)
        original = manager.save_to_config()

        dialog = QualityStepsDialog(manager=manager)
        dialog._remove_step(20000)
        dialog.reject()

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert manager.save_to_config() == original

    def test_accept_applies_edited_altitude_atomically(self, qt_app):
        manager = DynamicZoomManager()
        manager.set_base_zoom(17, 18)
        manager.add_step(20000, 15, 16)

        dialog = QualityStepsDialog(manager=manager)
        row = next(
            row for row in range(dialog.steps_table.rowCount())
            if dialog.steps_table.item(row, 0).text() == "20000"
        )
        dialog.steps_table.item(row, 0).setText("25000")
        dialog._on_accept()
        result = dialog.get_manager()

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert all(step.altitude_ft != 20000 for step in result.get_steps())
        assert any(step.altitude_ft == 25000 for step in result.get_steps())
        assert all(step.altitude_ft != 25000 for step in manager.get_steps())

    def test_duplicate_altitude_blocks_accept(self, qt_app):
        manager = DynamicZoomManager()
        manager.set_base_zoom(17, 18)
        manager.add_step(10000, 16, 17)
        manager.add_step(20000, 15, 16)

        dialog = QualityStepsDialog(manager=manager)
        row = next(
            row for row in range(dialog.steps_table.rowCount())
            if dialog.steps_table.item(row, 0).text() == "20000"
        )
        dialog.steps_table.item(row, 0).setText("10000")
        dialog._on_accept()

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert not dialog.validation_label.isHidden()
        assert "already exists" in dialog.validation_label.text()

    def test_base_altitude_cell_is_not_editable(self, qt_app):
        manager = DynamicZoomManager()
        manager.set_base_zoom(17, 18)
        dialog = QualityStepsDialog(manager=manager)
        item = dialog.steps_table.item(0, 0)

        assert not bool(item.flags() & Qt.ItemFlag.ItemIsEditable)

    def test_legacy_negative_base_rule_can_round_trip(self, qt_app):
        manager = DynamicZoomManager()
        manager.add_step(-1000, 17, 18)
        manager.add_step(25000, 15, 16)

        dialog = QualityStepsDialog(manager=manager)
        dialog._on_accept()
        result = dialog.get_manager()

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert result.has_base_step()
        assert any(
            step.altitude_ft == -1000 for step in result.get_steps()
        )

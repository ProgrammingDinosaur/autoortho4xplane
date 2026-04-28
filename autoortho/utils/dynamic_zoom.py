#!/usr/bin/env python3
"""
Dynamic zoom level management based on altitude AGL (Above Ground Level).

Quality Steps define max zoom levels for different altitude ranges.
The system selects the appropriate zoom level based on predicted
AGL altitude at the closest approach to each tile.

Using AGL instead of MSL (Mean Sea Level) altitude provides more accurate
imagery quality decisions based on actual height above terrain:
    - 10,000ft MSL over 5,000ft mountains = 5,000ft AGL (higher zoom needed)
    - 10,000ft MSL over ocean = 10,000ft AGL (lower zoom acceptable)

Configuration Integration:
    - Steps are stored in CFG.autoortho.dynamic_zoom_steps as a list of dicts
    - The SectionParser in aoconfig.py parses this using ast.literal_eval
    - When saving, Python's str() converts the list back to string format

Usage:
    from utils.dynamic_zoom import DynamicZoomManager

    manager = DynamicZoomManager()
    manager.load_from_config(CFG.autoortho.dynamic_zoom_steps)
    zoom = manager.get_zoom_for_altitude(5000)  # Returns appropriate ZL for 5000ft AGL
"""

from dataclasses import dataclass
from typing import List, Optional, Union
import logging

log = logging.getLogger(__name__)


# Base altitude threshold (feet AGL) - cannot be modified by users
# This represents ground level (AGL = 0)
BASE_ALTITUDE_FT = 0

# Default zoom level when no steps are configured
DEFAULT_ZOOM_LEVEL = 16

# Valid zoom level range
MIN_ZOOM_LEVEL = 12
MAX_ZOOM_LEVEL = 19


@dataclass
class QualityStep:
    """
    A single zoom level threshold defined by altitude AGL.

    Attributes:
        altitude_ft: Altitude threshold in feet AGL (at or above this altitude)
        zoom_level: Maximum zoom level to use at this altitude and above
        zoom_level_airports: Maximum zoom level near airports at this altitude

    Example:
        QualityStep(altitude_ft=20000, zoom_level=15, zoom_level_airports=16)
        means "at or above 20,000 ft AGL, use max zoom 15, but 16 near airports"
    """

    altitude_ft: int
    zoom_level: int
    zoom_level_airports: int = 18  # Default to 18 for backwards compatibility

    def __post_init__(self):
        """Validate and normalize values after initialization."""
        # Ensure types are correct (handles string inputs from config)
        self.altitude_ft = int(self.altitude_ft)
        self.zoom_level = int(self.zoom_level)
        self.zoom_level_airports = int(self.zoom_level_airports)

        # Clamp zoom levels to valid range
        self.zoom_level = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, self.zoom_level))
        self.zoom_level_airports = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, self.zoom_level_airports))

    def to_dict(self) -> dict:
        """Convert to dictionary for config serialization."""
        return {
            "altitude_ft": self.altitude_ft,
            "zoom_level": self.zoom_level,
            "zoom_level_airports": self.zoom_level_airports,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QualityStep":
        """
        Create QualityStep from dictionary.

        Args:
            data: Dictionary with 'altitude_ft', 'zoom_level', and optionally
                  'zoom_level_airports' keys

        Returns:
            New QualityStep instance

        Raises:
            KeyError: If required keys are missing
            ValueError: If values cannot be converted to int
        """
        return cls(
            altitude_ft=int(data["altitude_ft"]),
            zoom_level=int(data["zoom_level"]),
            # Default to 18 for backwards compatibility with old configs
            zoom_level_airports=int(data.get("zoom_level_airports", 18))
        )

    def __repr__(self) -> str:
        return f"QualityStep(alt={self.altitude_ft}ft, zl={self.zoom_level}, zl_apt={self.zoom_level_airports})"


class DynamicZoomManager:
    """
    Manages quality steps and computes appropriate zoom levels.

    The manager maintains a sorted list of QualityStep objects and provides
    methods for:
    - Loading/saving steps to config format
    - Adding, removing, and updating steps
    - Computing the appropriate zoom level for a given altitude

    The steps are always kept sorted by altitude in descending order
    (highest altitude first) to enable efficient lookup.

    Thread Safety:
        This class is NOT thread-safe. External synchronization is required
        if accessed from multiple threads. In practice, the manager is
        typically configured at startup and then only read during flight.
    """

    def __init__(self):
        """Initialize with empty steps list."""
        self._steps: List[QualityStep] = []

    def load_from_config(self, config_value: Union[str, list, None]) -> None:
        """
        Load quality steps from config value.

        The config value can be:
        - A list of dicts (already parsed by SectionParser)
        - A string representation of a list (for manual parsing)
        - None or empty list (resets to no steps)

        Args:
            config_value: Value from CFG.autoortho.dynamic_zoom_steps

        Note:
            Invalid entries are logged and skipped, not raised as errors.
            This provides graceful degradation if config is corrupted.
        """
        self._steps = []

        # Handle None or empty
        if not config_value:
            return

        # Handle empty list
        if config_value == [] or config_value == "[]":
            return

        # If already a list (parsed by SectionParser), use directly
        if isinstance(config_value, list):
            data = config_value
        else:
            # Shouldn't normally happen, but handle string for robustness
            log.warning(
                "dynamic_zoom_steps is string, expected list. "
                "Config may need re-saving."
            )
            return

        # Parse each step
        for i, item in enumerate(data):
            try:
                if not isinstance(item, dict):
                    log.warning(f"Skipping invalid step at index {i}: not a dict")
                    continue
                step = QualityStep.from_dict(item)
                self._steps.append(step)
            except (KeyError, ValueError, TypeError) as e:
                log.warning(f"Skipping invalid step at index {i}: {e}")
                continue

        # Sort by altitude descending (highest first)
        self._steps.sort(key=lambda s: s.altitude_ft, reverse=True)

        log.debug(f"Loaded {len(self._steps)} dynamic zoom steps")

    def save_to_config(self) -> list:
        """
        Serialize steps to config format.

        Returns:
            List of dicts suitable for storing in config.
            The list can be assigned directly to CFG.autoortho.dynamic_zoom_steps
            and will be converted to string by set_config().

        Note:
            Returns a list (not string) because SectionParser/set_config
            handles the str() conversion during save.
        """
        return [step.to_dict() for step in self._steps]

    def get_steps(self) -> List[QualityStep]:
        """
        Get all steps sorted by altitude (highest first).

        Returns:
            List of QualityStep objects. Returns a new sorted list,
            so modifications won't affect the internal state.
        """
        return sorted(self._steps, key=lambda s: s.altitude_ft, reverse=True)

    def get_max_zoom_levels(self) -> tuple[int, int]:
        """
        Get the highest regular and airport zoom levels this config can return.

        This is used for conservative sizing when the exact altitude-based zoom
        for a tile is not known yet.  If the base step is missing, include the
        runtime fallback defaults so callers do not under-report possible output.

        Returns:
            Tuple of (max_regular_zoom, max_airport_zoom).
        """
        if not self._steps:
            return DEFAULT_ZOOM_LEVEL, 18

        max_regular = max(step.zoom_level for step in self._steps)
        max_airport = max(step.zoom_level_airports for step in self._steps)

        if not self.has_base_step():
            max_regular = max(max_regular, DEFAULT_ZOOM_LEVEL)
            max_airport = max(max_airport, 18)

        return max_regular, max_airport

    def add_step(self, altitude_ft: int, zoom_level: int, 
                 zoom_level_airports: int = 18) -> bool:
        """
        Add a new quality step.

        Args:
            altitude_ft: Altitude threshold in feet
            zoom_level: Maximum zoom level for this altitude
            zoom_level_airports: Maximum zoom level near airports

        Returns:
            True if step was added, False if altitude already exists
        """
        # Check for duplicate altitude
        if any(s.altitude_ft == altitude_ft for s in self._steps):
            log.debug(f"Cannot add step: altitude {altitude_ft} already exists")
            return False

        step = QualityStep(altitude_ft, zoom_level, zoom_level_airports)
        self._steps.append(step)
        self._steps.sort(key=lambda s: s.altitude_ft, reverse=True)

        log.debug(f"Added quality step: {step}")
        return True

    def remove_step(self, altitude_ft: int) -> bool:
        """
        Remove step by altitude.

        Args:
            altitude_ft: Altitude of the step to remove

        Returns:
            True if step was removed, False if:
            - Step doesn't exist
            - Attempting to remove base step (BASE_ALTITUDE_FT)
        """
        if altitude_ft == BASE_ALTITUDE_FT:
            log.debug("Cannot remove base step")
            return False

        original_count = len(self._steps)
        self._steps = [s for s in self._steps if s.altitude_ft != altitude_ft]

        removed = len(self._steps) < original_count
        if removed:
            log.debug(f"Removed step at altitude {altitude_ft}")
        return removed

    def update_step(self, altitude_ft: int, new_zoom: int, 
                    new_zoom_airports: Optional[int] = None) -> bool:
        """
        Update zoom levels for an existing step.

        Args:
            altitude_ft: Altitude of the step to update
            new_zoom: New zoom level to set
            new_zoom_airports: New airport zoom level (None = don't change)

        Returns:
            True if step was updated, False if step doesn't exist
        """
        for step in self._steps:
            if step.altitude_ft == altitude_ft:
                old_zoom = step.zoom_level
                step.zoom_level = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, new_zoom))
                if new_zoom_airports is not None:
                    step.zoom_level_airports = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, new_zoom_airports))
                log.debug(
                    f"Updated step at {altitude_ft}ft: ZL{old_zoom} -> ZL{step.zoom_level}"
                )
                return True
        return False

    def get_base_step(self) -> Optional[QualityStep]:
        """
        Get the base step (lowest altitude, always BASE_ALTITUDE_FT).

        Returns:
            The base QualityStep if it exists, None otherwise
        """
        for step in self._steps:
            if step.altitude_ft == BASE_ALTITUDE_FT:
                return step
        return None

    def has_base_step(self) -> bool:
        """Check if the base step exists."""
        return self.get_base_step() is not None

    def set_base_zoom(self, zoom_level: int, zoom_level_airports: int = 18) -> None:
        """
        Set or update the base step zoom level.

        If the base step doesn't exist, it will be created.
        The base step uses BASE_ALTITUDE_FT as its altitude.

        Args:
            zoom_level: Zoom level for the base step
            zoom_level_airports: Zoom level near airports for the base step
        """
        zoom_level = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, zoom_level))
        zoom_level_airports = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, zoom_level_airports))

        for step in self._steps:
            if step.altitude_ft == BASE_ALTITUDE_FT:
                step.zoom_level = zoom_level
                step.zoom_level_airports = zoom_level_airports
                log.debug(f"Updated base step to ZL{zoom_level}, ZL_apt{zoom_level_airports}")
                return

        # Create base step
        self._steps.append(QualityStep(BASE_ALTITUDE_FT, zoom_level, zoom_level_airports))
        self._steps.sort(key=lambda s: s.altitude_ft, reverse=True)
        log.debug(f"Created base step at ZL{zoom_level}")

    def get_zoom_for_altitude(self, altitude_ft: float) -> int:
        """
        Get the max zoom level for a given altitude.

        The algorithm finds the highest altitude step that the given
        altitude is at or above. Steps are sorted highest to lowest,
        so we return the first step where altitude >= step.altitude_ft.

        Args:
            altitude_ft: Aircraft altitude in feet

        Returns:
            The appropriate max zoom level for this altitude.
            Returns DEFAULT_ZOOM_LEVEL if no steps are configured.

        Example:
            With steps: [(40000, 14), (20000, 15), (-1000, 17)]
            - altitude 45000 -> ZL14 (above 40000)
            - altitude 35000 -> ZL15 (above 20000, below 40000)
            - altitude 10000 -> ZL17 (above -1000, below 20000)
            - altitude -500  -> ZL17 (above -1000)
        """
        if not self._steps:
            return DEFAULT_ZOOM_LEVEL

        # Steps are sorted descending by altitude
        for step in self.get_steps():
            if altitude_ft >= step.altitude_ft:
                return step.zoom_level

        # Below all thresholds - should not happen if base step exists
        # Fall back to base step or default
        base = self.get_base_step()
        return base.zoom_level if base else DEFAULT_ZOOM_LEVEL

    def get_airport_zoom_for_altitude(self, altitude_ft: float) -> int:
        """
        Get the max zoom level near airports for a given altitude.

        Same algorithm as get_zoom_for_altitude but returns the
        airport-specific zoom level instead.

        Args:
            altitude_ft: Aircraft altitude in feet

        Returns:
            The appropriate max airport zoom level for this altitude.
            Returns 18 (default airport zoom) if no steps are configured.
        """
        if not self._steps:
            return 18  # Default airport zoom

        # Steps are sorted descending by altitude
        for step in self.get_steps():
            if altitude_ft >= step.altitude_ft:
                return step.zoom_level_airports

        # Below all thresholds - should not happen if base step exists
        base = self.get_base_step()
        return base.zoom_level_airports if base else 18

    def initialize_from_fixed_zoom(self, fixed_zoom: int, 
                                   fixed_zoom_airports: int = 18) -> None:
        """
        Initialize with a single base step from fixed zoom values.

        This is used when transitioning from fixed mode to dynamic mode
        for the first time. The previous fixed max_zoom becomes the
        base step zoom level.

        Args:
            fixed_zoom: The current fixed max_zoom setting
            fixed_zoom_airports: The current fixed max_zoom_near_airports setting
        """
        fixed_zoom = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, fixed_zoom))
        fixed_zoom_airports = max(MIN_ZOOM_LEVEL, min(MAX_ZOOM_LEVEL, fixed_zoom_airports))
        self._steps = [QualityStep(BASE_ALTITUDE_FT, fixed_zoom, fixed_zoom_airports)]
        log.info(f"Initialized dynamic zoom from fixed ZL{fixed_zoom}, ZL_apt{fixed_zoom_airports}")

    def is_empty(self) -> bool:
        """Check if no steps are configured."""
        return len(self._steps) == 0

    def step_count(self) -> int:
        """Get the number of configured steps."""
        return len(self._steps)

    def clear(self) -> None:
        """Remove all steps."""
        self._steps = []

    def get_summary(self) -> str:
        """
        Get a human-readable summary of configured steps.

        Returns:
            Summary string like "ZL17/18@-1000ft, ZL15/16@20000ft"
            (format: normal/airport @ altitude)
            or "No steps configured" if empty
        """
        if not self._steps:
            return "No steps configured"

        # Sort by altitude ascending for display
        sorted_steps = sorted(self._steps, key=lambda s: s.altitude_ft)
        return ", ".join(
            f"ZL{s.zoom_level}/{s.zoom_level_airports}@{s.altitude_ft:+}ft" 
            for s in sorted_steps
        )

    def validate(self) -> List[str]:
        """
        Validate the current configuration.

        Returns:
            List of warning messages. Empty list means valid.
        """
        warnings = []

        if not self._steps:
            warnings.append("No quality steps configured")
            return warnings

        if not self.has_base_step():
            warnings.append(f"Missing base step at {BASE_ALTITUDE_FT}ft")

        # Check for zoom levels that decrease with altitude (unusual but valid)
        sorted_steps = self.get_steps()  # Highest alt first
        for i in range(len(sorted_steps) - 1):
            higher = sorted_steps[i]
            lower = sorted_steps[i + 1]
            if higher.zoom_level > lower.zoom_level:
                warnings.append(
                    f"Unusual: higher altitude ({higher.altitude_ft}ft) has "
                    f"higher zoom (ZL{higher.zoom_level}) than lower altitude "
                    f"({lower.altitude_ft}ft, ZL{lower.zoom_level})"
                )

        return warnings

    def __repr__(self) -> str:
        return f"DynamicZoomManager(steps={len(self._steps)}, summary='{self.get_summary()}')"

"""Shell pages."""

from .home_page import HomePage, StatusCard
from .diagnostics_page import DiagnosticsPage
from .flight_plan_page import FlightPlanPage
from .scenery_page import SceneryLibraryPage
from .settings_page import SettingsPage

__all__ = [
    "DiagnosticsPage",
    "FlightPlanPage",
    "HomePage",
    "SceneryLibraryPage",
    "SettingsPage",
    "StatusCard",
]

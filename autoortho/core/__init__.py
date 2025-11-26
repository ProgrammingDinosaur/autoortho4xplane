"""
AutoOrtho Core Module

Contains core application logic, configuration, and crash handling.
"""

from .app import *  # Main application (autoortho.py)
from .config import AOConfig  # Configuration (aoconfig.py)
from .crash_handler import *  # Crash handling

__all__ = ['AOConfig']


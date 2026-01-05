#!/usr/bin/env python3
"""
PyInstaller runtime hook for enabling free-threading in Python 3.14+.

This hook runs before the main application starts and ensures that
free-threading is enabled if the Python runtime supports it.

Note: For free-threading to work, the application must be built with
Python 3.14t (the free-threading build). The PYTHON_GIL=0 environment
variable enables free-threading at runtime.
"""
import os
import sys


def enable_free_threading():
    """
    Attempt to enable free-threading mode if available.
    
    This must be done before any significant Python code runs.
    The PYTHON_GIL=0 environment variable is the standard way to
    enable free-threading in Python 3.14+.
    """
    # Check Python version
    if sys.version_info < (3, 14):
        return False
    
    # Check if free-threading is available in this build
    if not hasattr(sys, '_is_gil_enabled'):
        return False
    
    # Check if already running without GIL
    if not sys._is_gil_enabled():
        return True  # Already free-threaded
    
    # Set environment variable for any child processes
    os.environ.setdefault('PYTHON_GIL', '0')
    
    return False  # Can't change GIL state after interpreter starts


# Run on import (during PyInstaller runtime hook phase)
_free_threading_enabled = enable_free_threading()

# Note: Detailed free-threading status is logged in __main__.py after logging is set up
# This hook only sets up the environment; the main app logs the status


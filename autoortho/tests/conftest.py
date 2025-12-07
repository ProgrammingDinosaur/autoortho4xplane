"""
pytest configuration and fixtures for autoortho tests.

This conftest.py adds the parent directory (autoortho/) to sys.path
so that tests can import modules like getortho, pydds, etc.
"""

import os
import sys

# Add the autoortho package directory to the path so tests can import modules
autoortho_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if autoortho_dir not in sys.path:
    sys.path.insert(0, autoortho_dir)

# Also add the project root for any top-level imports
project_root = os.path.dirname(autoortho_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)


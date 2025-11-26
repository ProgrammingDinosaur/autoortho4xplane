"""
AutoOrtho Platform-Specific Module

Contains platform-specific setup and configuration.
"""

from autoortho.utils.constants import system_type

if system_type == 'windows':
    from .windows import *
elif system_type == 'darwin':
    from .macos import *
    from .macos_fuse_worker import *

__all__ = []


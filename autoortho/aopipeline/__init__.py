"""
aopipeline - Native Performance Pipeline for AutoOrtho

This module provides native (C) implementations of performance-critical
operations that bypass Python's GIL limitation:

- AoCache: Parallel batch cache file I/O
- AoDecode: Parallel JPEG decoding  
- AoDDS: Native DDS texture building with ISPC compression

Optimal Usage (Hybrid Pipeline):
    from autoortho.aopipeline import AoDDS
    
    # Python reads files, native decodes + compresses
    jpeg_datas = [Path(p).read_bytes() for p in chunk_paths]
    dds_bytes = AoDDS.build_from_jpegs(jpeg_datas)
"""

import logging
import os
import sys

log = logging.getLogger(__name__)

# On Windows, add the DLL directory to PATH BEFORE importing modules
# This ensures dependencies (libturbojpeg, libgomp, etc.) can be found
if sys.platform == 'win32':
    _lib_dir = None
    
    # Check if running as PyInstaller frozen executable
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # PyInstaller: library is in _MEIPASS/autoortho/aopipeline/lib/windows/
        _lib_dir = os.path.join(sys._MEIPASS, 'autoortho', 'aopipeline', 'lib', 'windows')
        if not os.path.isdir(_lib_dir):
            # Fallback: check without autoortho prefix
            _lib_dir = os.path.join(sys._MEIPASS, 'aopipeline', 'lib', 'windows')
    
    # Development mode: library is relative to this file
    if _lib_dir is None or not os.path.isdir(_lib_dir):
        _lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib', 'windows')
    
    if os.path.isdir(_lib_dir):
        # Add to PATH so Windows can find DLL dependencies
        os.environ['PATH'] = _lib_dir + os.pathsep + os.environ.get('PATH', '')
        # Also use add_dll_directory if available (Python 3.8+)
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(_lib_dir)

# LAZY imports to avoid deadlock when multiple threads import concurrently.
# Each module is imported on first access via __getattr__.
# This prevents Python's import lock from causing deadlocks.

_module_cache = {}


def _get_module(name):
    """Internal helper to get a lazily-imported module."""
    if name not in _module_cache:
        import importlib
        _module_cache[name] = importlib.import_module(f'.{name}', __name__)
    return _module_cache[name]


def __getattr__(name):
    """Lazy import of submodules to avoid deadlock on concurrent imports."""
    if name in ('AoCache', 'AoDecode', 'AoDDS'):
        return _get_module(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def is_available() -> bool:
    """Check if any native pipeline component is available."""
    try:
        return any([
            _get_module('AoCache').is_available(),
            _get_module('AoDecode').is_available(),
            _get_module('AoDDS').is_available(),
        ])
    except Exception:
        return False


def get_available_components() -> list:
    """Return list of available native components."""
    components = []
    try:
        if _get_module('AoCache').is_available():
            components.append('cache')
    except Exception:
        pass
    try:
        if _get_module('AoDecode').is_available():
            components.append('decode')
    except Exception:
        pass
    try:
        if _get_module('AoDDS').is_available():
            components.append('dds')
    except Exception:
        pass
    return components


__all__ = [
    'AoCache', 'AoDecode', 'AoDDS',
    'is_available', 'get_available_components'
]

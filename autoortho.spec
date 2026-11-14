# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for AutoOrtho.

This spec file bundles the application with all required native libraries
and executables for Windows, Linux, and macOS.

Usage:
    pyinstaller autoortho.spec

The resulting executable will be in dist/autoortho/
"""

import sys
import os
import re
import tomllib
import runpy
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all

block_cipher = None

# Release workflows set VERNAME. Local builds use project metadata, never a
# hardcoded bundle version or the separately versioned scenery file.
with open(os.path.join(SPECPATH, 'pyproject.toml'), 'rb') as project_file:
    project_version = tomllib.load(project_file)['tool']['poetry']['version']
release_version = os.environ.get('VERNAME') or project_version
version_match = re.match(r'^v?(\d+\.\d+\.\d+)', release_version)
if version_match is None:
    raise ValueError(f'Invalid application release version: {release_version!r}')
bundle_version = version_match.group(1)

# =============================================================================
# Collect modules with native extensions (required for Linux - have .so files)
# =============================================================================

def safe_collect_all(module_name):
    """Safely collect all components of a module, returning empty lists on failure."""
    try:
        return collect_all(module_name)
    except Exception:
        return [], [], []

# System monitoring
psutil_datas, psutil_binaries, psutil_hiddenimports = safe_collect_all('psutil')

# Numerical/scientific
numpy_datas, numpy_binaries, numpy_hiddenimports = safe_collect_all('numpy')

# Async/networking (gevent stack)
greenlet_datas, greenlet_binaries, greenlet_hiddenimports = safe_collect_all('greenlet')
gevent_datas, gevent_binaries, gevent_hiddenimports = safe_collect_all('gevent')
zope_datas, zope_binaries, zope_hiddenimports = safe_collect_all('zope.interface')

# Serialization
msgpack_datas, msgpack_binaries, msgpack_hiddenimports = safe_collect_all('msgpack')
zmq_datas, zmq_binaries, zmq_hiddenimports = safe_collect_all('zmq')

# Shared HTTP/2 broker
httpx_datas, httpx_binaries, httpx_hiddenimports = safe_collect_all('httpx')
h2_datas, h2_binaries, h2_hiddenimports = safe_collect_all('h2')

# Flask dependencies with C extensions
markupsafe_datas, markupsafe_binaries, markupsafe_hiddenimports = safe_collect_all('markupsafe')

# Character encoding
charset_datas, charset_binaries, charset_hiddenimports = safe_collect_all('charset_normalizer')

# Modular Qt UI packages include conditional direct-execution imports that are
# not always visible to PyInstaller's static analysis.
ui_hiddenimports = collect_submodules('autoortho.ui')

# Collect all datas/binaries/hiddenimports for native modules
native_module_datas = (
    psutil_datas + numpy_datas + greenlet_datas + gevent_datas + 
    zope_datas + msgpack_datas + zmq_datas + httpx_datas + h2_datas +
    markupsafe_datas + charset_datas
)
native_module_binaries = (
    psutil_binaries + numpy_binaries + greenlet_binaries + gevent_binaries + 
    zope_binaries + msgpack_binaries + zmq_binaries + httpx_binaries +
    h2_binaries + markupsafe_binaries + charset_binaries
)
native_module_hiddenimports = (
    psutil_hiddenimports + numpy_hiddenimports + 
    greenlet_hiddenimports + gevent_hiddenimports + zope_hiddenimports +
    msgpack_hiddenimports + zmq_hiddenimports + httpx_hiddenimports +
    h2_hiddenimports +
    markupsafe_hiddenimports + charset_hiddenimports
)

# Determine platform
if sys.platform == 'win32':
    platform_name = 'windows'
    exe_suffix = '.exe'
    dll_suffix = '.dll'
elif sys.platform == 'darwin':
    platform_name = 'macos'
    exe_suffix = ''
    dll_suffix = '.dylib'
else:
    platform_name = 'linux'
    exe_suffix = ''
    dll_suffix = '.so'

# Base paths
autoortho_path = os.path.join(os.getcwd(), 'autoortho')
lib_path = os.path.join(autoortho_path, 'lib', platform_name)
aoimage_path = os.path.join(autoortho_path, 'aoimage')
aopipeline_path = os.path.join(autoortho_path, 'aopipeline')
aopipeline_lib_path = os.path.join(aopipeline_path, 'lib', platform_name)

# Fail packaging instead of silently shipping an old checked-in native library.
# run_path avoids importing the application's same-named top-level module.
native_provenance = runpy.run_path(os.path.join(autoortho_path, 'native_build.py'))
native_provenance['verify_bundle'](aopipeline_lib_path, SPECPATH, expected_platform=platform_name)

# =============================================================================
# Binary files (DLLs, shared libraries, executables)
# =============================================================================
binaries = []

# AoImage library
if sys.platform == 'win32':
    binaries.append((os.path.join(aoimage_path, 'aoimage.dll'), 'autoortho/aoimage'))
elif sys.platform == 'darwin':
    binaries.append((os.path.join(aoimage_path, 'aoimage.dylib'), 'autoortho/aoimage'))
else:
    binaries.append((os.path.join(aoimage_path, 'aoimage.so'), 'autoortho/aoimage'))

# AoPipeline library and dependencies
if sys.platform == 'win32':
    binaries.append((os.path.join(aopipeline_lib_path, 'aopipeline.dll'), 'autoortho/aopipeline/lib/windows'))
    binaries.append((os.path.join(aopipeline_lib_path, 'libgcc_s_seh-1.dll'), 'autoortho/aopipeline/lib/windows'))
    binaries.append((os.path.join(aopipeline_lib_path, 'libgomp-1.dll'), 'autoortho/aopipeline/lib/windows'))
    binaries.append((os.path.join(aopipeline_lib_path, 'libturbojpeg.dll'), 'autoortho/aopipeline/lib/windows'))
    binaries.append((os.path.join(aopipeline_lib_path, 'libwinpthread-1.dll'), 'autoortho/aopipeline/lib/windows'))
    binaries.append((os.path.join(aopipeline_lib_path, 'ao-crash-collector.exe'), 'autoortho/aopipeline/lib/windows'))
elif sys.platform == 'darwin':
    binaries.append((os.path.join(aopipeline_lib_path, 'libaopipeline.dylib'), 'autoortho/aopipeline/lib/macos'))
else:
    binaries.append((os.path.join(aopipeline_lib_path, 'libaopipeline.so'), 'autoortho/aopipeline/lib/linux'))

# Platform-specific compression libraries and tools
if sys.platform == 'win32':
    # Compression libraries
    binaries.append((os.path.join(lib_path, 'ispc_texcomp.dll'), 'autoortho/lib/windows'))
    binaries.append((os.path.join(lib_path, 'stb_dxt.dll'), 'autoortho/lib/windows'))
    
    # 7-Zip
    binaries.append((os.path.join(lib_path, '7zip', '7za.exe'), 'autoortho/lib/windows/7zip'))
    binaries.append((os.path.join(lib_path, '7zip', '7za.dll'), 'autoortho/lib/windows/7zip'))
    binaries.append((os.path.join(lib_path, '7zip', '7zxa.dll'), 'autoortho/lib/windows/7zip'))
    
    # DSFTool
    binaries.append((os.path.join(lib_path, 'DSFTool.exe'), 'autoortho/lib/windows'))

elif sys.platform == 'darwin':
    # Compression libraries
    binaries.append((os.path.join(lib_path, 'libispc_texcomp.dylib'), 'autoortho/lib/macos'))
    binaries.append((os.path.join(lib_path, 'libstbdxt.dylib'), 'autoortho/lib/macos'))
    
    # 7-Zip
    binaries.append((os.path.join(lib_path, '7zip', '7zz'), 'autoortho/lib/macos/7zip'))
    
    # DSFTool
    binaries.append((os.path.join(lib_path, 'DSFTool'), 'autoortho/lib/macos'))

else:  # Linux
    # Compression libraries
    binaries.append((os.path.join(lib_path, 'libispc_texcomp.so'), 'autoortho/lib/linux'))
    binaries.append((os.path.join(lib_path, 'lib_stb_dxt.so'), 'autoortho/lib/linux'))
    
    # 7-Zip
    binaries.append((os.path.join(lib_path, '7zip', '7zz'), 'autoortho/lib/linux/7zip'))
    
    # DSFTool
    binaries.append((os.path.join(lib_path, 'DSFTool'), 'autoortho/lib/linux'))

# =============================================================================
# Data files
# =============================================================================
datas = []
datas.append((os.path.join(aopipeline_lib_path, 'native-build.json'),
              'autoortho/aopipeline/lib/' + platform_name))

# Version file - auto-generate from git if not present
version_file = os.path.join(autoortho_path, '.version')
if not os.path.exists(version_file):
    # Try to get version from git
    import subprocess
    try:
        git_version = subprocess.check_output(
            ['git', 'describe', '--tags', '--always', '--dirty'],
            stderr=subprocess.DEVNULL,
            cwd=os.getcwd()
        ).decode().strip()
        # Write to .version file
        with open(version_file, 'w') as f:
            f.write(git_version)
        print(f"Generated .version file: {git_version}")
    except Exception as e:
        # Fallback: use current date/time as version
        from datetime import datetime
        fallback_version = f"build-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        with open(version_file, 'w') as f:
            f.write(fallback_version)
        print(f"Generated fallback .version file: {fallback_version}")

if os.path.exists(version_file):
    datas.append((version_file, 'autoortho'))

# Flask templates (required for web UI)
templates_path = os.path.join(autoortho_path, 'templates')
if os.path.isdir(templates_path):
    datas.append((templates_path, 'autoortho/templates'))

# Images/icons for UI
imgs_path = os.path.join(autoortho_path, 'imgs')
if os.path.isdir(imgs_path):
    datas.append((imgs_path, 'autoortho/imgs'))

# X-Plane plugin for files mode (installed into X-Plane by the files worker)
_plugin_dir = {'win32': 'win_x64', 'darwin': 'mac_x64'}.get(sys.platform, 'lin_x64')
plugin_bin_path = os.path.join(autoortho_path, 'xplane_plugin', 'bin', _plugin_dir)
if os.path.isdir(plugin_bin_path):
    datas.append((plugin_bin_path, f'autoortho/xplane_plugin/bin/{_plugin_dir}'))

# 7-Zip license
if sys.platform == 'win32':
    license_path = os.path.join(lib_path, '7zip', 'License.txt')
else:
    license_path = os.path.join(lib_path, '7zip', 'License.txt')
if os.path.exists(license_path):
    datas.append((license_path, f'autoortho/lib/{platform_name}/7zip'))

# Both Requests and HTTPX use certifi by default on every platform. Bundle its
# data explicitly so Linux builds do not depend on the build host's CA paths.
datas += collect_data_files('certifi')

# =============================================================================
# Hidden imports (modules that PyInstaller might miss)
# =============================================================================
hiddenimports = [
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'geocoder',
    'geocoder.osm',
    'psutil',
    'requests',
    'httpx',
    'httpcore',
    'h2',
    'hpack',
    'hyperframe',
    'zmq',
    'zmq.asyncio',
    'PIL',
    'PIL.Image',
    # Flask-SocketIO async drivers (required for frozen apps)
    'engineio.async_drivers.threading',
    'socketio.async_drivers.threading',
    # AoPipeline modules (lazy imports - PyInstaller won't detect automatically)
    'autoortho.aopipeline',
    'autoortho.aopipeline.AoCache',
    'autoortho.aopipeline.AoDecode',
    'autoortho.aopipeline.AoDDS',
    'autoortho.aopipeline.fallback_resolver',
    # Cache path utilities
    'autoortho.utils.cache_paths',
    # 7-Zip wrapper (replaces py7zr)
    'autoortho.utils.sevenzip',
    # Async/networking stack
    'greenlet',
    'gevent',
    'gevent.resolver',
    'gevent.ssl',
    'zope',
    'zope.interface',
    'zope.event',
    # Serialization
    'msgpack',
    # NumPy
    'numpy',
    'numpy.core',
    'numpy.core._multiarray_umath',
    # Flask/Jinja dependencies
    'markupsafe',
    'markupsafe._speedups',
    # Character encoding
    'charset_normalizer',
    'charset_normalizer.md',
]

# Platform-specific hidden imports
if sys.platform == 'darwin':
    hiddenimports.extend([
        'macsetup',
        'macfuse_worker',
    ])
elif sys.platform == 'win32':
    hiddenimports.extend([
        'winsetup',
        'winfspy',
        'winfspy.plumbing',
    ])

# =============================================================================
# Analysis
# =============================================================================
a = Analysis(
    [os.path.join(autoortho_path, '__main__.py')],
    pathex=[autoortho_path],
    binaries=binaries + native_module_binaries,
    datas=datas + native_module_datas,
    hiddenimports=(
        hiddenimports
        + ui_hiddenimports
        + native_module_hiddenimports
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'numpy.testing',
        'IPython',
        'jupyter',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebChannel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# =============================================================================
# PYZ (Python bytecode archive)
# =============================================================================
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# =============================================================================
# EXE
# =============================================================================
# Determine icon path based on platform
if sys.platform == 'win32':
    icon_path = os.path.join(autoortho_path, 'imgs', 'ao-icon.ico')
elif sys.platform == 'darwin':
    icon_path = os.path.join(autoortho_path, 'imgs', 'ao-icon.icns')
else:
    icon_path = None

# Verify icon exists
if icon_path and os.path.exists(icon_path):
    print(f"Using icon: {icon_path}")
else:
    print(f"WARNING: Icon not found at {icon_path}")
    icon_path = None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='autoortho',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI-only mode - no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
    contents_directory='ao_files',  # Custom folder name instead of '_internal'
)

# =============================================================================
# COLLECT (bundle everything together)
# =============================================================================
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    # Compression would change the native artifact hashes in its manifest.
    upx_exclude=['aopipeline.dll', 'ao-crash-collector.exe', 'libgomp-1.dll',
                 'libgcc_s_seh-1.dll', 'libwinpthread-1.dll', 'libturbojpeg.dll'],
    name='autoortho',
)

# =============================================================================
# macOS App Bundle (optional)
# =============================================================================
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='AutoOrtho.app',
        icon=os.path.join(autoortho_path, 'imgs', 'ao-icon.icns'),
        bundle_identifier='com.autoortho.app',
        info_plist={
            'CFBundleName': 'AutoOrtho',
            'CFBundleDisplayName': 'AutoOrtho',
            'CFBundleVersion': bundle_version,
            'CFBundleShortVersionString': bundle_version,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.15',
        },
    )

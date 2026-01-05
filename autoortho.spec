# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for AutoOrtho.

This spec file bundles the application with all required native libraries
and executables for Windows, Linux, and macOS.

Usage:
    pyinstaller autoortho.spec

The resulting executable will be in dist/autoortho/

Python 3.14+ Free-Threading Notes:
    - Build with Python 3.14t (free-threading build) for optimal performance
    - The bundled app will automatically enable free-threading if available
    - Set PYTHON_GIL=0 environment variable to enable at runtime
"""

import sys
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# =============================================================================
# Free-Threading Detection (Python 3.14+)
# =============================================================================
# Check if we're building with a free-threading Python
FREE_THREADING_BUILD = False
if sys.version_info >= (3, 14) and hasattr(sys, '_is_gil_enabled'):
    FREE_THREADING_BUILD = True
    print(f"Building with Python {sys.version_info.major}.{sys.version_info.minor} "
          f"(free-threading {'enabled' if not sys._is_gil_enabled() else 'available'})")
else:
    print(f"Building with Python {sys.version_info.major}.{sys.version_info.minor} "
          f"(standard build, free-threading not available)")

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

# 7-Zip license
if sys.platform == 'win32':
    license_path = os.path.join(lib_path, '7zip', 'License.txt')
else:
    license_path = os.path.join(lib_path, '7zip', 'License.txt')
if os.path.exists(license_path):
    datas.append((license_path, f'autoortho/lib/{platform_name}/7zip'))

# SSL Certificates (required for macOS - system certs not accessible in bundled app)
if sys.platform == 'darwin':
    import certifi
    cacert_path = certifi.where()
    if os.path.exists(cacert_path):
        # Bundle certifi's CA bundle for SSL verification
        datas.append((cacert_path, 'certifi'))
        print(f"Bundling SSL certificates from: {cacert_path}")

# =============================================================================
# Hidden imports (modules that PyInstaller might miss)
# =============================================================================
hiddenimports = [
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'geocoder',
    'geocoder.osm',
    'psutil',
    'requests',
    'PIL',
    'PIL.Image',
    # Flask-SocketIO async drivers (required for frozen apps)
    'engineio.async_drivers.threading',
    'socketio.async_drivers.threading',
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
# Runtime Hooks
# =============================================================================
# Runtime hooks run before the main application starts
runtime_hooks_list = []

# Add free-threading runtime hook if building with Python 3.14+
freethreading_hook = os.path.join(autoortho_path, 'runtime_hook_freethreading.py')
if os.path.exists(freethreading_hook):
    runtime_hooks_list.append(freethreading_hook)
    print(f"Added free-threading runtime hook: {freethreading_hook}")

# =============================================================================
# Analysis
# =============================================================================
a = Analysis(
    [os.path.join(autoortho_path, '__main__.py')],
    pathex=[autoortho_path],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=runtime_hooks_list,
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'numpy.testing',
        'IPython',
        'jupyter',
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
    upx_exclude=[],
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
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.15',
        },
    )


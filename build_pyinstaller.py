#!/usr/bin/env python3
"""
Build script for AutoOrtho using PyInstaller.

Usage:
    python build_pyinstaller.py [--onefile] [--debug]

Options:
    --onefile   Create a single executable (slower startup, easier distribution)
    --debug     Include debug symbols and console output
"""

import subprocess
import sys
import os
import shutil
from pathlib import Path


def check_dependencies():
    """Check that required build tools are installed."""
    try:
        import PyInstaller
        print(f"✓ PyInstaller {PyInstaller.__version__} found")
    except ImportError:
        print("✗ PyInstaller not found. Install with: pip install pyinstaller")
        return False
    
    # Check that autoortho directory exists
    if not os.path.isdir('autoortho'):
        print("✗ autoortho directory not found. Run from project root.")
        return False
    print("✓ autoortho directory found")
    
    return True


def check_free_threading():
    """Check if Python is built with free-threading support."""
    print()
    print("Free-Threading Status:")
    print("-" * 40)
    print(f"  Python version: {sys.version}")
    
    if sys.version_info < (3, 14):
        print("  ⚠ Python < 3.14: Free-threading not available")
        print("    Recommendation: Use Python 3.14t for optimal performance")
        return False
    
    # Check for free-threading support
    if hasattr(sys, '_is_gil_enabled'):
        gil_enabled = sys._is_gil_enabled()
        if gil_enabled:
            print("  ⚠ Free-threading available but GIL is ENABLED")
            print("    To enable free-threading at runtime:")
            print("      - Set environment variable: PYTHON_GIL=0")
            print("      - Or use flag: python -X gil=0")
        else:
            print("  ✓ Free-threading ENABLED (no GIL)")
        return True
    else:
        print("  ⚠ Free-threading not available in this Python build")
        print("    To get free-threading support:")
        print("      - Install Python 3.14t (free-threading build)")
        print("      - On macOS: brew install python@3.14 --with-freethreading")
        print("      - Using pyenv: pyenv install 3.14.0t")
        return False


def clean_build():
    """Remove previous build artifacts."""
    dirs_to_clean = ['build', 'dist']
    for d in dirs_to_clean:
        if os.path.isdir(d):
            print(f"Cleaning {d}/...")
            shutil.rmtree(d)
    
    # Remove .pyc files
    for pyc in Path('.').rglob('*.pyc'):
        pyc.unlink()
    for pycache in Path('.').rglob('__pycache__'):
        if pycache.is_dir():
            shutil.rmtree(pycache)
    
    print("✓ Build directory cleaned")


def build(onefile=False, debug=False):
    """Run PyInstaller build."""
    cmd = ['pyinstaller']
    
    if onefile:
        # Modify spec for onefile mode
        cmd.extend(['--onefile'])
        cmd.extend(['--name', 'autoortho'])
        cmd.extend(['autoortho/__main__.py'])
        
        # Add all the binaries and data manually for onefile
        # This is more complex, so we recommend using the spec file
        print("Note: --onefile mode may require manual adjustment of binaries")
    else:
        # Use the spec file (recommended)
        cmd.append('autoortho.spec')
    
    if debug:
        cmd.append('--debug=all')
    
    cmd.append('--noconfirm')  # Don't ask for confirmation
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    
    return result.returncode == 0


def verify_build():
    """Verify the build output."""
    if sys.platform == 'win32':
        exe_path = Path('dist/autoortho/autoortho.exe')
    elif sys.platform == 'darwin':
        exe_path = Path('dist/autoortho/autoortho')
        app_path = Path('dist/AutoOrtho.app')
    else:
        exe_path = Path('dist/autoortho/autoortho')
    
    if exe_path.exists():
        print(f"✓ Executable created: {exe_path}")
        print(f"  Size: {exe_path.stat().st_size / (1024*1024):.1f} MB")
        return True
    else:
        print(f"✗ Executable not found at {exe_path}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Build AutoOrtho with PyInstaller')
    parser.add_argument('--onefile', action='store_true', 
                        help='Create single executable')
    parser.add_argument('--debug', action='store_true',
                        help='Include debug symbols')
    parser.add_argument('--no-clean', action='store_true',
                        help='Skip cleaning previous builds')
    parser.add_argument('--check-only', action='store_true',
                        help='Only check dependencies and free-threading status')
    args = parser.parse_args()
    
    print("=" * 60)
    print("AutoOrtho PyInstaller Build")
    print("=" * 60)
    print()
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check free-threading status
    has_free_threading = check_free_threading()
    
    if args.check_only:
        print()
        print("=" * 60)
        if has_free_threading:
            print("✓ Ready to build with free-threading support")
        else:
            print("⚠ Build will proceed without free-threading optimizations")
        print("=" * 60)
        sys.exit(0)
    
    # Clean previous builds
    if not args.no_clean:
        clean_build()
    
    print()
    print("Building...")
    print("-" * 40)
    
    # Build
    if not build(onefile=args.onefile, debug=args.debug):
        print()
        print("✗ Build failed!")
        sys.exit(1)
    
    print()
    print("-" * 40)
    
    # Verify
    if verify_build():
        print()
        print("=" * 60)
        print("✓ Build completed successfully!")
        print("=" * 60)
        print()
        print("Output location: dist/autoortho/")
        if sys.platform == 'darwin':
            print("macOS App Bundle: dist/AutoOrtho.app/")
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()


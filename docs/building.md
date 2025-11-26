# Building AutoOrtho with Crash Protection

This guide explains how to build AutoOrtho 1.4.2+ with the integrated crash protection features.

---

## Overview

Crash protection is now built into AutoOrtho by default. When you compile the native libraries, crash guards are automatically included.

**What gets built:**
1. `aoimage.dll/.so/.dylib` - Image processing library (with crash guard)
2. `pydds_safe.dll/.so/.dylib` - Compression wrapper library (with crash guard)

---

## Prerequisites

### Windows

**Required:**
- MinGW-w64 (x86_64)
- libjpeg-turbo development files
- Make

**Install:**
```bash
# Using MSYS2
pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-make
```

### Linux

**Required:**
- GCC
- libjpeg-turbo development files
- Make

**Install:**
```bash
# Ubuntu/Debian
sudo apt install build-essential libturbojpeg0-dev

# Fedora/RHEL
sudo dnf install gcc make turbojpeg-devel
```

### macOS

**Required:**
- Xcode Command Line Tools
- Homebrew
- jpeg-turbo

**Install:**
```bash
xcode-select --install
brew install jpeg-turbo
```

---

## Building aoimage Library

### Windows

```bash
cd autoortho/aoimage
make -f Makefile.mgw64

# Output: aoimage.dll
```

### Linux

```bash
cd autoortho/aoimage
make -f Makefile.lin64

# Output: aoimage.so
```

### macOS

```bash
cd autoortho/aoimage
make -f Makefile.macos

# Output: aoimage.dylib
```

### What Gets Compiled

Both `aoimage.c` and `crash_guard.c` are compiled and linked:

```
aoimage.c → aoimage.o
crash_guard.c → crash_guard.o
    ↓
aoimage.dll/.so/.dylib (includes both)
```

**Crash protection is automatically enabled** - no configuration needed!

---

## Building pydds_safe Wrapper Library

This optional but recommended library provides crash protection for external compression libraries.

### Windows

```bash
cd autoortho
make -f Makefile.pydds_safe_win

# Output: pydds_safe.dll
```

### Linux

```bash
cd autoortho
make -f Makefile.pydds_safe_linux

# Output: libpydds_safe.so
```

### macOS

```bash
cd autoortho
make -f Makefile.pydds_safe_macos

# Output: libpydds_safe.dylib
```

### What Gets Compiled

```
pydds_safe_wrapper.c → pydds_safe_wrapper.o
crash_guard.c → crash_guard.o (shared with aoimage)
    ↓
pydds_safe.dll/.so/.dylib
```

---

## Full Build Process

### Automated (Recommended)

If your project has a top-level Makefile:

```bash
# Build everything
make all

# Or build specific targets
make aoimage
make pydds_safe
```

### Manual

**Windows:**
```bash
# 1. Build aoimage
cd autoortho/aoimage
make -f Makefile.mgw64
cd ../..

# 2. Build pydds_safe
cd autoortho
make -f Makefile.pydds_safe_win
cd ..

# 3. Verify outputs
ls -l autoortho/aoimage/aoimage.dll
ls -l autoortho/pydds_safe.dll
```

**Linux:**
```bash
# 1. Build aoimage
cd autoortho/aoimage
make -f Makefile.lin64
cd ../..

# 2. Build pydds_safe
cd autoortho
make -f Makefile.pydds_safe_linux
cd ..

# 3. Verify outputs
ls -l autoortho/aoimage/aoimage.so
ls -l autoortho/libpydds_safe.so
```

**macOS:**
```bash
# 1. Build aoimage
cd autoortho/aoimage
make -f Makefile.macos
cd ../..

# 2. Build pydds_safe
cd autoortho
make -f Makefile.pydds_safe_macos
cd ..

# 3. Verify outputs
ls -l autoortho/aoimage/aoimage.dylib
ls -l autoortho/libpydds_safe.dylib
```

---

## Testing the Build

### Test aoimage Crash Protection

```bash
cd autoortho/aoimage
python3 -c "
from AoImage import AoImage
import os

# This should NOT crash Python
img = AoImage()
print('Crash protection test: PASS')
"
```

### Test pydds_safe

```bash
cd autoortho
python3 -c "
from pydds import DDS
print('pydds_safe loaded successfully')
"
```

### Check for Crash Guard

```bash
# Windows
strings autoortho/aoimage/aoimage.dll | grep "crash_guard"

# Linux
strings autoortho/aoimage/aoimage.so | grep "crash_guard"

# macOS
strings autoortho/aoimage/aoimage.dylib | grep "crash_guard"
```

You should see function names like:
```
crash_guard_init
crash_guard_cleanup
aoimage_crash_guard_init
```

---

## Debugging Build Issues

### Issue: "crash_guard.h not found"

**Cause:** Header file missing

**Solution:**
```bash
# Ensure crash_guard.h exists
ls -l autoortho/aoimage/crash_guard.h

# If missing, check repository:
git status
```

---

### Issue: "undefined reference to crash_guard_init"

**Cause:** crash_guard.o not linked

**Solution:**
Check Makefile has crash_guard.o in OBJECTS:
```makefile
OBJECTS=aoimage.o crash_guard.o
```

---

### Issue: "turbojpeg.h not found"

**Cause:** libjpeg-turbo not installed

**Solution:**
```bash
# Windows (MSYS2)
pacman -S mingw-w64-x86_64-libjpeg-turbo

# Linux
sudo apt install libturbojpeg0-dev

# macOS
brew install jpeg-turbo
```

---

### Issue: "ispc_texcomp not found" (for pydds_safe)

**Cause:** Compression library not in lib/ directory

**Solution:**
```bash
# Ensure libraries are present
ls -l autoortho/lib/windows/ispc_texcomp.dll
ls -l autoortho/lib/linux/libispc_texcomp.so
ls -l autoortho/lib/macos/libispc_texcomp.dylib
```

---

## Distribution

### What to Include

When distributing AutoOrtho, include:

**Windows:**
```
autoortho/aoimage/aoimage.dll
autoortho/pydds_safe.dll (optional but recommended)
autoortho/lib/windows/ispc_texcomp.dll
autoortho/lib/windows/stb_dxt.dll
```

**Linux:**
```
autoortho/aoimage/aoimage.so
autoortho/libpydds_safe.so (optional but recommended)
autoortho/lib/linux/libispc_texcomp.so
autoortho/lib/linux/lib_stb_dxt.so
```

**macOS:**
```
autoortho/aoimage/aoimage.dylib
autoortho/libpydds_safe.dylib (optional but recommended)
autoortho/lib/macos/libispc_texcomp.dylib
autoortho/lib/macos/libstbdxt.dylib
```

### What NOT to Include

**Don't distribute:**
- ❌ `.o` object files
- ❌ `.c`/`.h` source files (unless open source)
- ❌ Development tools (Makefiles, etc.)
- ❌ Debug symbols (unless separate debug package)

### Size

**Typical sizes:**
- `aoimage.dll/.so/.dylib`: ~100-200 KB
- `pydds_safe.dll/.so/.dylib`: ~30-50 KB

**Total overhead from crash protection:** ~10-20 KB (negligible)

---

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Build with Crash Protection

on: [push, pull_request]

jobs:
  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Setup MinGW
        uses: msys2/setup-msys2@v2
        with:
          install: mingw-w64-x86_64-gcc make
      
      - name: Build aoimage
        shell: msys2 {0}
        run: |
          cd autoortho/aoimage
          make -f Makefile.mgw64
      
      - name: Build pydds_safe
        shell: msys2 {0}
        run: |
          cd autoortho
          make -f Makefile.pydds_safe_win
      
      - name: Upload artifacts
        uses: actions/upload-artifact@v3
        with:
          name: windows-binaries
          path: |
            autoortho/aoimage/aoimage.dll
            autoortho/pydds_safe.dll

  build-linux:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Install dependencies
        run: |
          sudo apt update
          sudo apt install -y build-essential libturbojpeg0-dev
      
      - name: Build aoimage
        run: |
          cd autoortho/aoimage
          make -f Makefile.lin64
      
      - name: Build pydds_safe
        run: |
          cd autoortho
          make -f Makefile.pydds_safe_linux
      
      - name: Upload artifacts
        uses: actions/upload-artifact@v3
        with:
          name: linux-binaries
          path: |
            autoortho/aoimage/aoimage.so
            autoortho/libpydds_safe.so

  build-macos:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Install dependencies
        run: brew install jpeg-turbo
      
      - name: Build aoimage
        run: |
          cd autoortho/aoimage
          make -f Makefile.macos
      
      - name: Build pydds_safe
        run: |
          cd autoortho
          make -f Makefile.pydds_safe_macos
      
      - name: Upload artifacts
        uses: actions/upload-artifact@v3
        with:
          name: macos-binaries
          path: |
            autoortho/aoimage/aoimage.dylib
            autoortho/libpydds_safe.dylib
```

---

## Development vs Production

### Development Build

**For debugging:**
```bash
# Add debug symbols
CFLAGS="-g -O0" make -f Makefile.xxx

# Result: Larger binaries, easier debugging
```

### Production Build

**For distribution:**
```bash
# Optimized, stripped
make -f Makefile.xxx  # Uses defaults (-O2 -s)

# Result: Smaller binaries, faster execution
```

**Difference:**
- Development: ~500 KB (with symbols)
- Production: ~200 KB (stripped)

---

## Verification

### After Building

**1. Check symbols exist:**
```bash
nm aoimage.dll | grep crash_guard_init  # Windows
nm aoimage.so | grep crash_guard_init   # Linux
nm aoimage.dylib | grep crash_guard_init  # macOS
```

**2. Test in Python:**
```python
#!/usr/bin/env python3
from autoortho.aoimage.AoImage import AoImage
import logging

logging.basicConfig(level=logging.INFO)

# Should see: "C-level crash protection enabled"
img = AoImage()
print("✅ Crash protection working!")
```

**3. Check log directory:**
```bash
# After running AutoOrtho once:
ls -l ~/.autoortho-data/logs/

# Should see:
# - autoortho.log (always)
# - c_crash.log (only if crash occurred)
```

---

## Summary

**Standard build:**
```bash
# 1. Build aoimage
cd autoortho/aoimage && make -f Makefile.{mgw64|lin64|macos}

# 2. Build pydds_safe (optional)
cd ../.. && make -f Makefile.pydds_safe_{win|linux|macos}

# Done! Crash protection included automatically.
```

**Verify:**
```bash
# Check for crash_guard symbols
strings autoortho/aoimage/aoimage.* | grep crash_guard

# Test in Python
python3 -c "from autoortho.aoimage.AoImage import AoImage; print('OK')"
```

**No special configuration needed** - crash protection is always enabled when you build from source!

---

*For usage information, see [Crash Protection Guide](crash-protection.md)*


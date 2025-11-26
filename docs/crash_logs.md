# Crash Logs and Error Reporting

AutoOrtho includes comprehensive crash protection to ensure stability and provide useful information when errors occur.

## Overview

AutoOrtho uses multiple layers of crash protection:

1. **Python-level crash handler** - Catches most Python exceptions
2. **C-level crash guard** - Catches crashes in native libraries (JPEG, compression)
3. **Orphaned mount cleanup** - Prevents filesystem issues after crashes

This means that even if part of AutoOrtho encounters an error, the application will:
- ✅ Continue running when possible
- ✅ Create detailed logs for bug reports
- ✅ Prevent cascading failures (e.g., X-Plane crashes)
- ✅ Clean up resources properly

---

## Log File Locations

### Main Application Log

**Location:**
- Windows: `%USERPROFILE%\.autoortho-data\logs\autoortho.log`
- Linux: `~/.autoortho-data/logs/autoortho.log`
- macOS: `~/.autoortho-data/logs/autoortho.log`

**Contains:**
- General application activity
- Configuration changes
- Image processing status
- Network requests
- Warnings and errors

**Example path (Windows):**
```
C:\Users\YourName\.autoortho-data\logs\autoortho.log
```

### C Crash Log (If Native Crash Occurs)

**Location:**
- Windows: `%USERPROFILE%\.autoortho-data\logs\c_crash.log`
- Linux: `~/.autoortho-data/logs/c_crash.log`
- macOS: `~/.autoortho-data/logs/c_crash.log`

**Contains:**
- Details about crashes in C libraries
- Function name where crash occurred
- Exception type and address
- Timestamp
- Relevant context (image dimensions, tile coordinates)

**Example content:**
```
--- CRASH DETECTED ---
Time:      2025-11-26 17:30:45
Function:  tjDecompress2
Exception: Access Violation (0xC0000005)
Address:   0x7FF8ABCD1234
Platform:  Windows 10
Version:   AutoOrtho 1.4.2
Tile:      12345_67890_BI16.dds
---
```

**File size:** ~1-10 KB per crash (small and easy to share)

---

## What Happens When a Crash Occurs

### Graceful Degradation

Instead of the entire application crashing:

1. **Crash is detected** by the crash guard
2. **Details are logged** to `c_crash.log`
3. **Error is returned** to Python code
4. **Fallback is used:**
   - Skip problematic tile
   - Use lower quality compression
   - Use cached version if available
5. **AutoOrtho continues running**
6. **X-Plane is unaffected**

### Example Flow

```
User flying over France
    ↓
AutoOrtho requests tile 12345_67890
    ↓
JPEG decompression encounters corrupt data
    ↓
Crash guard catches exception
    ↓
Log entry created in c_crash.log
    ↓
Python receives error code
    ↓
AutoOrtho uses fallback tile
    ↓
User sees slightly lower quality for one tile
    ↓
Flight continues normally ✅
```

**Without crash protection:**
```
JPEG decompression crashes
    ↓
AutoOrtho crashes
    ↓
Virtual filesystem orphaned
    ↓
X-Plane crashes loading DDS
    ↓
User loses flight progress ❌
```

---

## Reporting Bugs

If you experience issues with AutoOrtho, please report them on GitHub:
https://github.com/your-repo/autoortho/issues

### What to Include

**Always include:**
1. ✅ `autoortho.log` (last ~100 lines)
2. ✅ `c_crash.log` (if it exists)
3. ✅ AutoOrtho version
4. ✅ Operating system
5. ✅ Description of what happened

**Optional but helpful:**
- Screenshots
- Configuration settings
- Location where issue occurred (lat/lon or tile name)

### How to Find Logs

#### Windows

1. Press `Win+R` to open Run dialog
2. Type: `%USERPROFILE%\.autoortho-data\logs`
3. Press Enter
4. Find `autoortho.log` and `c_crash.log` (if present)

#### Linux / macOS

Open Terminal and run:
```bash
cd ~/.autoortho-data/logs
ls -la
```

Then view/copy the logs:
```bash
# View last 100 lines of main log
tail -100 autoortho.log

# View crash log if it exists
cat c_crash.log

# Copy to Desktop for easy sharing
cp autoortho.log ~/Desktop/
cp c_crash.log ~/Desktop/  # if it exists
```

### Privacy Note

**These logs are safe to share:**
- ✅ No passwords or API keys
- ✅ No personal information
- ✅ Only technical details about AutoOrtho's operation
- ✅ File sizes are small (1-100 KB typically)

**They may contain:**
- Your AutoOrtho configuration paths
- Tile coordinates you've visited
- Timestamps of when you used AutoOrtho

If you're concerned about privacy, you can:
- Redact any file paths you don't want to share
- Remove timestamps if needed
- Just share the crash-specific sections

---

## Understanding Crash Logs

### Common Crash Types

#### Access Violation (0xC0000005)

**What it means:**
- Program tried to read/write protected memory
- Most common cause: corrupt or unexpected data

**What AutoOrtho does:**
- Logs details
- Skips problematic image/tile
- Uses fallback

**Action needed:**
- Report with `c_crash.log` if it happens frequently
- Usually harmless if rare

---

#### EXCEPTION_IN_PAGE_ERROR (0xC0000120)

**What it means:**
- Failed to read a memory-mapped file
- Usually seen in X-Plane when loading DDS files

**What AutoOrtho does:**
- Cleanup orphaned mounts on next startup
- Prevent this from happening in the first place

**Action needed:**
- If you see this in X-Plane logs after AutoOrtho crashes:
  1. Restart AutoOrtho (it will auto-cleanup)
  2. Report the crash with logs

---

#### Segmentation Fault (Linux/Mac)

**What it means:**
- Similar to Access Violation on Windows
- Invalid memory access

**What AutoOrtho does:**
- Catches via signal handler
- Logs backtrace (if available)
- Returns error to Python

**Action needed:**
- Report with `c_crash.log` and last lines of `autoortho.log`

---

### Log Levels

AutoOrtho uses different log levels:

- **DEBUG** - Detailed information (usually not shown)
- **INFO** - Normal operation messages
- **WARNING** - Something unexpected but not fatal
- **ERROR** - Operation failed but AutoOrtho continues
- **CRITICAL** - Severe error that may cause shutdown

You can change the log level in the configuration:
```ini
[general]
loglevel = INFO  # or DEBUG, WARNING, ERROR
```

---

## Troubleshooting

### "Tile failed to load"

**Check:**
1. `autoortho.log` for network errors
2. `c_crash.log` for compression crashes
3. Internet connection
4. API key configuration

**Solution:**
- Usually temporary - AutoOrtho will retry
- If persistent for specific area, report with logs

---

### "Compression failed"

**Check:**
- `c_crash.log` for crash details

**What happened:**
- Compression library encountered an error
- AutoOrtho used fallback

**Solution:**
- Usually harmless
- Report if happens frequently

---

### "Virtual filesystem not responding"

**Check:**
- Did AutoOrtho crash recently?

**Solution:**
1. Close X-Plane
2. Restart AutoOrtho (auto-cleanup runs)
3. Restart X-Plane

---

### X-Plane shows "Fatal error when loading DDS"

**What happened:**
- AutoOrtho crashed while X-Plane was reading a file
- Virtual filesystem became orphaned

**Solution:**
1. Note the error in X-Plane logs
2. Close X-Plane
3. Restart AutoOrtho (auto-cleanup runs)
4. Restart X-Plane
5. Report AutoOrtho crash with logs

**Prevention:**
- This should be rare with crash protection
- If frequent, please report

---

## Advanced: Debug Mode

For development or deep troubleshooting, you can enable debug mode:

**In configuration file:**
```ini
[general]
loglevel = DEBUG
```

**Effect:**
- Much more detailed logging
- Shows every image operation
- Network request details
- Compression steps

**Note:** Debug logs can grow large quickly (100+ MB). Use temporarily.

---

## FAQ

### Q: Why are there two log files?

**A:** 
- `autoortho.log` - Python-level logging (main application)
- `c_crash.log` - C-level crash details (native libraries)

Most of the time you'll only have `autoortho.log`. The `c_crash.log` only appears if a native library crashes.

---

### Q: Is it safe to delete old logs?

**A:** Yes! AutoOrtho appends to logs but you can safely delete them. They'll be recreated on next run.

---

### Q: Can crashes cause data corruption?

**A:** No. AutoOrtho only reads your X-Plane installation and writes to its own cache directory. Crashes cannot corrupt:
- ❌ X-Plane installation
- ❌ Scenery files
- ❌ Aircraft
- ❌ Settings

The only thing affected is AutoOrtho's own cache, which can be safely deleted.

---

### Q: How do I clear the cache?

**Location:**
- Windows: `%USERPROFILE%\.autoortho-data\cache`
- Linux/Mac: `~/.autoortho-data/cache`

**To clear:**
1. Close AutoOrtho and X-Plane
2. Delete the cache directory
3. Restart AutoOrtho

AutoOrtho will re-download tiles as needed.

---

### Q: What if AutoOrtho keeps crashing?

**Try:**
1. Update to latest version
2. Clear cache
3. Check for conflicting software (antivirus, etc.)
4. Report issue with logs on GitHub

**Include in report:**
- Full `autoortho.log`
- All `c_crash.log` entries
- Steps to reproduce
- System information

---

## Summary

**For Normal Use:**
- ✅ AutoOrtho handles crashes gracefully
- ✅ Logs are automatically created
- ✅ Application continues running
- ✅ No action needed from you

**If Issues Occur:**
1. Check `~/.autoortho-data/logs/` for log files
2. Share logs when reporting bugs (they're small and safe)
3. Restart AutoOrtho if needed (auto-cleanup runs)

**Peace of Mind:**
- Crashes won't corrupt your X-Plane installation
- Logs help developers fix issues
- Crash protection prevents most serious problems

---

*For more information, see:*
- [Configuration Guide](config.md)
- [FAQ](faq.md)
- [GitHub Issues](https://github.com/your-repo/autoortho/issues)


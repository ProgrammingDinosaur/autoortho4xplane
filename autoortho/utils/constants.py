"""module to hold constants used throughout the project"""
import platform
import os

MAPTYPES = ['Use tile default', 'Use tile settings', 'BI', 'NAIP', 'EOX', 'USGS', 'Firefly', 'GO2', 'ARC', 'YNDX', 'APPLE']

system_type = platform.system().lower()

DSFTOOL_PATH = ""

# DSF in autoortho/lib/windows/DSFTool.exe
# We are in autoortho/utils
# So we need to go up 2 levels to get to the lib directory and get the correct full path
if system_type == 'windows':
    DSFTOOL_PATH = os.path.join("autoortho", "lib", "windows", "DSFTool.exe")
elif system_type == 'linux':
    DSFTOOL_PATH = os.path.join("autoortho", "lib", "linux", "DSFTool")
elif system_type == 'darwin':
    DSFTOOL_PATH = os.path.join("autoortho", "lib", "macos", "DSFTool")
else:
    print("System is not supported")
    exit()

DSFTOOL_PATH = os.path.abspath(DSFTOOL_PATH)
print(f"DSFTOOL_PATH: {DSFTOOL_PATH}")
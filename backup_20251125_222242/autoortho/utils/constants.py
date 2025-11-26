"""module to hold constants used throughout the project"""
import platform
import os

MAPTYPES = ['Use tile default', 'BI', 'NAIP', 'EOX', 'USGS', 'Firefly', 'GO2', 'ARC', 'YNDX', 'APPLE']

system_type = platform.system().lower()

CURRENT_CPU_COUNT = os.cpu_count() or 1

# Spatial priority system constants
EARTH_RADIUS_M = 6371000
PRIORITY_DISTANCE_WEIGHT = 1.0
PRIORITY_DIRECTION_WEIGHT = 0.5
PRIORITY_MIPMAP_WEIGHT = 2.0
LOOKAHEAD_TIME_SEC = 30

LOGS_DIR = os.path.join(os.path.expanduser("~"), ".autoortho-data", "logs")
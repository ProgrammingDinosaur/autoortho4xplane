"""module to hold constants used throughout the project"""
import platform
import os
import sys

MAPTYPES = ['Use tile default', 'BI', 'NAIP', 'EOX', 'USGS', 'Firefly', 'GO2', 'ARC', 'YNDX', 'APPLE']

system_type = platform.system().lower()

CURRENT_CPU_COUNT = os.cpu_count() or 1


# ============================================================================
# PYTHON 3.14+ FREE-THREADING DETECTION
# ============================================================================
# Python 3.14 introduced free-threading (no-GIL) mode which allows true
# parallel execution of Python code. This section provides utilities to
# detect the threading mode and calculate optimal worker counts.
# ============================================================================

def is_free_threaded() -> bool:
    """
    Check if running in Python 3.14+ free-threaded mode (no GIL).
    
    Returns:
        True if running without GIL, False otherwise
    """
    if sys.version_info < (3, 14):
        return False
    # sys._is_gil_enabled() returns True if GIL is active, False if free-threaded
    return not getattr(sys, '_is_gil_enabled', lambda: True)()


FREE_THREADING_ENABLED = is_free_threaded()


def get_optimal_worker_count(base_count: int, purpose: str = "cpu") -> int:
    """
    Get optimal worker count based on threading mode and purpose.
    
    Args:
        base_count: Base number of workers (typically CPU count)
        purpose: "cpu" for CPU-bound, "io" for I/O-bound tasks
    
    Returns:
        Optimal worker count for the current threading mode
    """
    if purpose == "io":
        # I/O-bound: more workers OK regardless of GIL
        return min(base_count * 4, 64)
    elif purpose == "cpu":
        if FREE_THREADING_ENABLED:
            # Free-threaded: can use all cores effectively
            return base_count
        else:
            # GIL mode: more threads = more contention, limit to 2
            return min(base_count, 2)
    return base_count

# Spatial priority system constants
EARTH_RADIUS_M = 6371000
PRIORITY_DISTANCE_WEIGHT = 1.0
PRIORITY_DIRECTION_WEIGHT = 0.5
PRIORITY_MIPMAP_WEIGHT = 2.0
LOOKAHEAD_TIME_SEC = 30

LOGS_DIR = os.path.join(os.path.expanduser("~"), ".autoortho-data", "logs")
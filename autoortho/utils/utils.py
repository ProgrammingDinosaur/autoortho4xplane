""" module to hold utility functions used throughout the project """
import psutil


def is_xplane_running():
    """ check if xplane is running """
    # Process is called "X-Plane"
    for proc in psutil.process_iter():
        if "X-Plane" in proc.name():
            return True
    return False
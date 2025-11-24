import os
import sys

__version__ = "unknown"


def _find_version_file():
    """Find .version file in various possible locations for compiled and
    dev environments.
    """
    # Try multiple locations for the .version file
    search_paths = []

    # Path 1: Same directory as this file
    # (works in dev and most compiled scenarios)
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller
        base_path = sys._MEIPASS
        search_paths.append(os.path.join(base_path, 'autoortho',
                                         '.version'))
        search_paths.append(os.path.join(base_path, '.version'))
    else:
        # Standard location relative to this file
        CUR_PATH = os.path.dirname(os.path.realpath(__file__))
        search_paths.append(os.path.join(CUR_PATH, '.version'))

        # Path 2: In case of Nuitka standalone, try relative to
        # sys.executable
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            search_paths.append(os.path.join(exe_dir, 'autoortho',
                                             '.version'))
            search_paths.append(os.path.join(exe_dir, '.version'))
            # For macOS app bundles, also check Contents/Resources
            search_paths.append(os.path.join(exe_dir, '..', 'Resources',
                                             'autoortho', '.version'))

    # Path 3: Git HEAD for development
    head_file = os.path.join(os.curdir, '.git', 'HEAD')

    # Try all version file locations
    for ver_file in search_paths:
        if os.path.exists(ver_file):
            try:
                with open(ver_file, 'r') as h:
                    return h.read().strip()
            except Exception:
                continue

    # Try git HEAD as fallback
    if os.path.exists(head_file):
        try:
            with open(head_file, 'r') as h:
                return h.read().strip()
        except Exception:
            pass

    return "unknown"

# TODO: Remove this once the version file is working
__version__ = "1.4.2" # _find_version_file()

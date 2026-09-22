"""Compatibility alias for the modular Qt application window.

New code should import from ``autoortho.ui.main_window``. The module alias
keeps historical imports and monkeypatch targets working during migration.
"""

import sys

if __package__:
    from autoortho.ui import main_window as _implementation
else:
    from ui import main_window as _implementation

sys.modules[__name__] = _implementation

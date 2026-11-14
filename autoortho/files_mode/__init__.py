"""Files mode: streaming ortho without a virtual file system.

X-Plane reads ordinary files from a real scenery folder (the *pack*).
AutoOrtho runs beside it, predicts which mip level X-Plane will read of
each texture and when, and writes the texture to disk before that.

See ``docs/developer/files-mode-plan.md`` for the design and the numbered
assumptions (A1-A10) the modules refer to. Measurements quoted in
docstrings come from OrthoStudio XP Live's published probe flights
(``docs/specs/live*.md`` there); no code is taken from it.
"""

LAYOUT_VERSION = 1

"""Typed access to the ``[streaming]`` config section.

Values are read at call time so tests (and the settings UI) can change them
without reloading modules. A missing section or key falls back to the
packaged default.
"""

try:
    from autoortho.aoconfig import CFG
except ImportError:  # direct execution / PyInstaller layout
    from aoconfig import CFG

_TRUE = ("true", "1", "yes", "on")

DEFAULTS = {
    "read_classifier": True,
    "low_zoom_base": True,
    "low_zoom_base_max_zoom": 12,
    "paging_prefetch": True,
    "paging_k": 0.0,
    "paging_lead_seconds": 60.0,
    "ter_load_ahead": True,
    "wait_bands": True,
    "near_wait_seconds": 60.0,
    "band_max_wait_seconds": 6.0,
    "far_wait_seconds": 0.5,
    "band_priorities": True,
    "prefetch_during_live_reads": True,
    "read_time_colours": True,
    "real_from_index": True,
}


def _raw(name):
    section = getattr(CFG, "streaming", None)
    if section is None:
        return DEFAULTS[name]
    return getattr(section, name, DEFAULTS[name])


def flag(name):
    value = _raw(name)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return bool(value)


def number(name):
    value = _raw(name)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(DEFAULTS[name])

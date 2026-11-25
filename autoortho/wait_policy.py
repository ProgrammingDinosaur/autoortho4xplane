"""What happens when X-Plane reads a texture that is not ready.

One setting, ``[streaming] texture_wait_policy``, shown in the UI as "When
scenery is not ready", sets the existing wait knobs together (hybrid read
path plan, part 3). No new mechanism: it supplies values for the knobs the
user has not set by hand, as auto-tuning does.

=========  =========================================  ==========  ===========
Policy     In flight                                  Loading     Level 0 may
                                                                  be coarser
=========  =========================================  ==========  ===========
quality    waits up to ``strict_target_deadline_sec``  as today    no
balanced   near and middle ``band_max_wait_seconds``,  as today    after the
           far ``far_wait_seconds`` (today)                        wait
smooth     no wait in any band                         as today    at once
=========  =========================================  ==========  ===========

"Loading" is the loading screen and the time parked after it
(``wait_bands.StartTracker``): every policy waits ``near_wait_seconds`` then,
because X-Plane is not drawing and does not read those textures again soon.

Under ``smooth`` a read in flight is answered at once with the tile's own
bytes (built levels as they are, unbuilt ones upscaled from the nearest
coarser level) while the downloads it started go on. X-Plane keeps what it
read until it opens the texture again, so a coarse texture stays until then.

``quality`` keeps a deadline: a provider that stops answering must not freeze
X-Plane for good. After it the read is answered as a fallback is today.
A chunk the provider has no imagery for (the sea at high zoom) never comes,
so even under ``quality`` it is filled from a lower zoom, as the other
policies do, not painted in ``missing_color`` (``Tile._no_imagery_fallback_level``).

Hand-set means written in the user's config file when this process loaded
it. The governor moves ``band_max_wait_seconds`` at run time; that is not
the user's choice, and the policy wins over it.
"""



def _options():
    """streaming_options, imported on first use.

    At import time it would pull in aoconfig, and with the autoortho folder
    on sys.path "from autoortho import" finds autoortho.py, which imports the
    UI while ui.main_window is still importing this module.
    """
    global _streaming_options
    if _streaming_options is None:
        if __package__:
            from autoortho import streaming_options
        else:
            import streaming_options
        _streaming_options = streaming_options
    return _streaming_options


_streaming_options = None

QUALITY, BALANCED, SMOOTH = "quality", "balanced", "smooth"
POLICIES = (QUALITY, BALANCED, SMOOTH)
DEFAULT = BALANCED

# What the pilot sees, for the UI and the docs.
LABELS = {
    QUALITY: "Wait for full quality",
    BALANCED: "Balanced",
    SMOOTH: "Never wait in flight",
}
DESCRIPTIONS = {
    QUALITY: (
        "X-Plane waits until nearby scenery is built at your zoom level "
        "(up to the strict deadline, 120 s by default). Scenery is always "
        "sharp, but X-Plane may pause on a slow connection. Where the "
        "provider has no imagery at your zoom (open sea), a lower zoom is "
        "shown."
    ),
    BALANCED: (
        "Nearby scenery waits a few seconds for full quality, then shows "
        "lower-resolution imagery until X-Plane loads it again."
    ),
    SMOOTH: (
        "X-Plane never waits in flight: scenery that is not built yet shows "
        "at once with the best imagery at hand (often blurry). It stays "
        "that way until X-Plane loads that area again. The loading screen "
        "still waits."
    ),
}


def normalize(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in POLICIES else DEFAULT


def current() -> str:
    """The policy in effect (unknown values read as ``balanced``)."""
    return normalize(_options().text("texture_wait_policy"))


def hand_set(section: str, key: str) -> bool:
    """Whether the user's config file set ``section.key`` (at load)."""
    CFG = _options().CFG
    explicit = getattr(CFG, "_explicit_options", None)
    if not isinstance(explicit, dict):
        return False
    return key in (explicit.get(section) or ())


def in_flight_wait(band_is_far: bool):
    """The policy's wait in flight for a band, or None for the configured one.

    ``balanced`` and any knob set by hand keep the configured value.
    """
    policy = current()
    if policy == BALANCED:
        return None
    key = "far_wait_seconds" if band_is_far else "band_max_wait_seconds"
    if hand_set("streaming", key):
        return None
    if policy == SMOOTH:
        return 0.0
    return strict_deadline()


def strict_deadline() -> float:
    CFG = _options().CFG
    section = getattr(CFG, "autoortho", None)
    try:
        return max(1.0, float(getattr(section, "strict_target_deadline_sec", 120.0)))
    except (TypeError, ValueError):
        return 120.0


def quality_mode(configured: str) -> str:
    """``prefetch_quality_mode`` under the policy.

    ``quality`` makes it ``strict_target`` (level 0 holds lower-zoom imagery
    only where the provider has none at the target zoom) unless the user set
    the mode by hand.
    """
    if current() == QUALITY and not hand_set("autoortho", "prefetch_quality_mode"):
        return "strict_target"
    return configured


def overridden_by_hand() -> tuple:
    """The knobs set by hand that the current policy leaves alone (for the UI)."""
    policy = current()
    if policy == BALANCED:
        return ()
    names = [
        ("streaming", "band_max_wait_seconds"),
        ("streaming", "far_wait_seconds"),
    ]
    if policy == QUALITY:
        names.append(("autoortho", "prefetch_quality_mode"))
    return tuple(f"{s}.{k}" for s, k in names if hand_set(s, k))

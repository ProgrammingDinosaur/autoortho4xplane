"""One description of every AutoOrtho setting.

The packaged template (``AOConfig._defaults``) stays the source of each
setting's default and its comment. :func:`build_registry` reads it into
:class:`SettingSpec` entries and adds what the template cannot say: which
tier of the settings UI a setting belongs to, its valid range, whether it is
retired, and what replaced it.

Tiers follow the settings refactor proposal
(``docs/developer/settings-refactor-proposal.md``):

* ``ESSENTIAL``: what only the user can decide (folders, quality target,
  imagery source, look).
* ``TUNING``: engine values a later phase sizes automatically.
* ``EXPERT``: everything else that stays editable.
* ``INTERNAL``: application state, never shown.
* ``RETIRED``: still read for compatibility, never written to the file.
"""

import ast
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Optional

TRUE_WORDS = ("true", "yes", "on")
FALSE_WORDS = ("false", "no", "off")


def _looks_like_float(text):
    body = text.lstrip("-")
    if not any(c.isdigit() for c in body):
        return False
    if "." in body or "e" in body.lower():
        try:
            float(text)
            return True
        except ValueError:
            return False
    return False


def parse_value(raw):
    """Parse one config value the way :class:`aoconfig.SectionParser` does.

    '0' and '1' stay integers, so numeric settings are never turned into
    booleans.
    """
    text = "" if raw is None else str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            return ast.literal_eval(text)
        except Exception:
            return text
    if text.lower() in TRUE_WORDS:
        return True
    if text.lower() in FALSE_WORDS:
        return False
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return text
    if _looks_like_float(text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def kind_of(default_text):
    """The value kind a default implies: bool, int, float, list or text."""
    value = parse_value(default_text)
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    return "text"


class Tier(str, Enum):
    ESSENTIAL = "essential"
    TUNING = "tuning"
    EXPERT = "expert"
    INTERNAL = "internal"
    RETIRED = "retired"


@dataclass(frozen=True)
class SettingSpec:
    section: str
    key: str
    default_text: str
    kind: str
    help: str
    tier: Tier
    label: str
    range: Optional[tuple] = None
    replaced_by: Optional[str] = None
    # Written even when equal to its default (the file needs it to know
    # which migrations it has seen).
    persist_always: bool = False

    @property
    def name(self):
        return f"{self.section}.{self.key}"

    @property
    def default(self):
        return parse_value(self.default_text)

    def validate(self, text):
        """Check a value typed for this setting.

        Returns ``(ok, normalized_text, message)``. The normalized text is
        what the config file gets.
        """
        raw = "" if text is None else str(text).strip()
        if self.kind == "bool":
            lowered = raw.lower()
            if lowered in TRUE_WORDS:
                return True, "True", ""
            if lowered in FALSE_WORDS:
                return True, "False", ""
            return False, raw, "Enter True or False."
        if self.kind in ("int", "float"):
            try:
                number = int(raw) if self.kind == "int" else float(raw)
            except ValueError:
                word = "a whole number" if self.kind == "int" else "a number"
                return False, raw, f"Enter {word}."
            if self.range is not None:
                low, high = self.range
                if number < low or number > high:
                    return False, raw, f"Enter a value from {low} to {high}."
            return True, str(number), ""
        if self.kind == "list":
            try:
                value = ast.literal_eval(raw)
            except Exception:
                value = None
            if not isinstance(value, list):
                return False, raw, "Enter a list, for example [1, 2, 3]."
            return True, str(value), ""
        return True, raw, ""


# Tiers by section.key. Keys not listed are EXPERT.
_ESSENTIAL = {
    "general.hide",
    "paths.xplane_path",
    "paths.scenery_path",
    "paths.cache_dir",
    "cache.file_cache_size",
    "autoortho.maptype_override",
    "autoortho.max_zoom_mode",
    "autoortho.max_zoom",
    "autoortho.max_zoom_near_airports",
    "autoortho.dynamic_zoom_steps",
    "seasons.enabled",
    "terrain.default_roughness",
    "time_exclusion.enabled",
    "simbrief.userid",
    "simbrief.use_flight_data",
}

_TUNING = {
    "autoortho.provider_max_in_flight",
    "autoortho.provider_origin_initial_concurrency",
    "autoortho.provider_origin_max_concurrency",
    "autoortho.live_builder_concurrency",
    "autoortho.background_builder_workers",
    "autoortho.live_tile_admission",
    "autoortho.buffer_pool_size",
    "autoortho.streaming_builder_pool_size",
    "autoortho.predictive_dds_build_interval_ms",
    "autoortho.predictive_dds_memory_mb",
    "autoortho.tile_image_cache_mb",
    "autoortho.cache_write_buffer_mb",
    "autoortho.prefetch_lookahead",
    "autoortho.prefetch_radius_nm",
    "autoortho.prefetch_max_chunks",
    "cache.cache_mem_limit",
    "streaming.near_wait_seconds",
    "streaming.band_max_wait_seconds",
    "streaming.paging_k",
}

_INTERNAL = {
    "general.gui",
    "general.showconfig",
    "general.setup_complete",
    "general.config_revision",
    "general.window_width",
    "general.window_height",
    "general.window_x",
    "general.window_y",
    "general.last_page",
    "general.dismissed_update_version",
}

# Retired keys and what replaced them (None: nothing, the value is ignored).
RETIRED = {
    "autoortho.live_aopipeline_min_chunk_ratio": None,
    "autoortho.ephemeral_dds_cache_mb": "autoortho.persistent_dds_cache_mb",
    "autoortho.max_concurrent_downloads": "autoortho.provider_max_in_flight",
    "autoortho.http2_max_connections": "autoortho.provider_max_connections",
}

_PERSIST_ALWAYS = {"general.config_revision"}

# Ranges the template comments or the runtime clamps state.
_RANGES = {
    "autoortho.prefetch_lookahead": (0, 60),
    "autoortho.prefetch_interval": (0.5, 10),
    "autoortho.prefetch_radius_nm": (10, 150),
    "autoortho.predictive_dds_build_interval_ms": (50, 2000),
    "autoortho.background_builder_workers": (1, 16),
    "autoortho.live_builder_concurrency": (1, 32),
    "autoortho.buffer_pool_size": (2, 64),
    "autoortho.streaming_builder_pool_size": (2, 64),
    "autoortho.tile_queue_max_size": (10, 500),
    "autoortho.dds_budget_pct": (10, 90),
    "autoortho.fallback_timeout": (10, 120),
    "pydds.dds_compression_level": (1, 19),
    "scenery.max_download_workers": (1, 8),
    "terrain.default_roughness": (0.0, 1.0),
    "time_exclusion.sun_night_threshold": (-18.0, 0.0),
    "time_exclusion.sun_day_threshold": (-18.0, 0.0),
}


def _tier_for(name):
    if name in RETIRED:
        return Tier.RETIRED
    if name in _INTERNAL:
        return Tier.INTERNAL
    if name in _ESSENTIAL:
        return Tier.ESSENTIAL
    if name in _TUNING:
        return Tier.TUNING
    return Tier.EXPERT


def _label_for(key):
    words = key.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _clean_comment(line):
    text = line.strip()[1:]
    return text[1:] if text.startswith(" ") else text


def build_registry(
    template_text: str,
    ranges: Optional[Mapping[str, tuple]] = None,
) -> "SettingsRegistry":
    """Read the packaged template into a registry.

    ``ranges`` adds or overrides ranges by ``section.key`` (the provider
    settings keep theirs in ``aoconfig.PROVIDER_SETTINGS``).
    """
    all_ranges = dict(_RANGES)
    if ranges:
        all_ranges.update(ranges)
    specs = []
    section = None
    comments = []
    for raw_line in template_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            comments = []
            continue
        if not stripped:
            comments = []
            continue
        if stripped.startswith("#"):
            comments.append(_clean_comment(stripped))
            continue
        if section is None or "=" not in stripped:
            continue
        key, default_text = (part.strip() for part in stripped.split("=", 1))
        key = key.lower()
        name = f"{section}.{key}"
        specs.append(
            SettingSpec(
                section=section,
                key=key,
                default_text=default_text,
                kind=kind_of(default_text),
                help=" ".join(c for c in comments if c).strip(),
                tier=_tier_for(name),
                label=_label_for(key),
                range=all_ranges.get(name),
                replaced_by=RETIRED.get(name),
                persist_always=name in _PERSIST_ALWAYS,
            )
        )
        comments = []
    return SettingsRegistry(specs)


class SettingsRegistry:
    def __init__(self, specs: Iterable[SettingSpec]):
        self._specs = {}
        for spec in specs:
            self._specs[(spec.section, spec.key)] = spec

    def __iter__(self):
        return iter(self._specs.values())

    def __len__(self):
        return len(self._specs)

    def get(self, section, key) -> Optional[SettingSpec]:
        return self._specs.get((section, str(key).lower()))

    def by_tier(self, *tiers: Tier):
        wanted = set(tiers)
        return [spec for spec in self if spec.tier in wanted]

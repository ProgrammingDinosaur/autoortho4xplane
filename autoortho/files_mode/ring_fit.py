"""Fit files mode's rings from Phase 0 telemetry (FUSE-mode reads).

Input: samples written by ``telemetry.ReadTelemetry``. Output: what
``rings.RingModel.load`` reads from ``rings.json``, plus a report on each
assumption:

* **A3 rings**: for each level, how far from the aircraft X-Plane first read
  it. A ring is the ``QUANTILE`` of those distances, normalised to a ZL16
  texture at 2704 px and 65 degrees (A5), and only at texture quality
  Maximum (A6).
* **Scaling**: rings normalised by zoom (``2^(zoom-16)``) and by ground size
  (``size_m / 7100``) are both fitted; the one whose distances spread less
  (coefficient of variation per level) wins.
* **3-D or ground distance**: the same comparison between the two.
* **A1**: opens per level read (about 1 when X-Plane opens again for each
  sharper level).
* **A2**: the level X-Plane reads first of a texture (the load level).
* **A6**: the finest level read per texture-quality notch.
"""

import json
import math
import statistics
from collections import Counter, defaultdict

from . import rings

QUANTILE = 0.98
MIN_PER_LEVEL = 30
QUALITY_MAX = rings.QUALITY_MAX


def load(paths):
    samples = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    samples.append(json.loads(line))
                except ValueError:
                    continue
    return samples


def _quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _distance_km(sample, three_d):
    ground = float(sample.get("ground_m", 0)) / 1000.0
    if not three_d:
        return ground
    return math.hypot(ground, float(sample.get("agl_m", 0)) / 1000.0)


def _normalised(sample, by, three_d):
    d = _distance_km(sample, three_d)
    scale = rings.screen_scale(sample.get("width", 0), sample.get("fov", 0))
    d /= scale
    if by == "zoom":
        return d * 2.0 ** (int(sample["z"]) - 16)
    size = float(sample.get("size_m") or 0)
    return d * rings.ZL16_SIZE_M / size if size > 0 else None


def _file_level(sample):
    """Level counted from the file's top (A4)."""
    return int(sample["mip"])


def _ring_samples(samples):
    """Upgrade reads at quality Maximum: not the load-level read."""
    out = []
    for s in samples:
        tex = s.get("tex_res")
        if tex is not None and int(tex) < QUALITY_MAX:
            continue
        level = _file_level(s)
        if level >= rings.LOAD_LEVEL:
            continue
        out.append(s)
    return out


def _fit_variant(samples, by, three_d):
    per_level = defaultdict(list)
    for s in samples:
        d = _normalised(s, by, three_d)
        if d is not None:
            per_level[_file_level(s)].append(d)
    ringsfit = {}
    spread = []
    for level, values in per_level.items():
        if len(values) < MIN_PER_LEVEL:
            continue
        ringsfit[level] = _quantile(values, QUANTILE)
        mean = statistics.fmean(values)
        if mean > 0 and len(values) > 1:
            spread.append(statistics.pstdev(values) / mean)
    score = statistics.fmean(spread) if spread else math.inf
    return ringsfit, score, {lvl: len(v) for lvl, v in per_level.items()}


def fit(samples):
    """``(rings_json, report)``."""
    ring_samples = _ring_samples(samples)
    variants = {}
    for by in ("zoom", "size"):
        for three_d in (False, True):
            variants[(by, three_d)] = _fit_variant(ring_samples, by, three_d)
    (by, three_d), (fitted, score, counts) = min(variants.items(), key=lambda kv: kv[1][1])
    # Rings must grow with the level; fill gaps from the default table.
    table = {}
    for level in sorted(rings.RINGS_ZL16_KM):
        value = fitted.get(level)
        table[level] = round(value, 2) if value else rings.RINGS_ZL16_KM[level]
    for level in sorted(table)[1:]:
        table[level] = max(table[level], table[level - 1] * 1.2)
    rings_json = {
        "rings_zl16_km": {str(k): v for k, v in table.items()},
        "margin": rings.MARGIN,
        "size_scaling": by == "size",
        "three_d": three_d,
        "samples": len(ring_samples),
        "fitted_levels": sorted(fitted),
        "source": "fit_rings.py from FUSE-mode read telemetry",
    }
    # A1: opens per texture vs levels read.
    levels_per_texture = defaultdict(set)
    opens_per_texture = defaultdict(int)
    for s in samples:
        key = (s["row"], s["col"], s.get("mt"), s["z"])
        levels_per_texture[key].add(int(s["mip"]))
        opens_per_texture[key] = max(opens_per_texture[key], int(s.get("opens") or 0))
    ratios = [opens_per_texture[k] / len(v) for k, v in levels_per_texture.items()
              if opens_per_texture[k] > 0]
    first_levels = Counter(int(s["mip"]) for s in samples if s.get("first"))
    finest = defaultdict(lambda: 99)
    for s in samples:
        tex = s.get("tex_res")
        finest[str(tex)] = min(finest[str(tex)], int(s["mip"]))
    report = {
        "samples": len(samples),
        "ring_samples": len(ring_samples),
        "best_variant": {"scaling": by, "three_d": three_d, "spread": score},
        "variants": {f"{b}/{'3d' if t else 'ground'}": {"rings": v[0], "spread": v[1]}
                     for (b, t), v in variants.items()},
        "per_level_samples": counts,
        "A1_opens_per_level_median": statistics.median(ratios) if ratios else None,
        "A2_first_read_levels": dict(first_levels),
        "A6_finest_level_by_quality": dict(finest),
        "default_rings": rings.RINGS_ZL16_KM,
    }
    return rings_json, report

"""Texture sizes: the optional size cap, and the graphics memory X-Plane holds.

A tile's texture is 4096 px at its file zoom (16 x 16 chunks of 256 px) and
doubles per zoom level above it, so a ZL16 tile built at ZL17 is 8192 px and
takes four times the graphics memory. The **texture cap** (auto-tuning's
optional lever, ``[governor] allow_texture_cap``) builds textures above the
cap at the largest zoom that fits it instead. It applies to textures opened
after it is set (X-Plane keeps a texture's size while it holds it), and the
DDS caches are keyed by the build zoom, so capped and full-size textures
never mix.

A capped texture shows the detail of the zoom it is built at. OrthoStreamXP
downloads the finer zoom and shrinks it; AutoOrtho does not, because that
downloads four times the imagery for little visible gain.

:class:`HeldTextures` estimates the graphics memory of the photo textures
X-Plane holds, as OrthoStreamXP's ``GraphicsMemoryMeter`` does: each texture
counts at the finest level X-Plane read in the last 20 minutes (X-Plane keeps
a texture while its scenery is loaded).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional, Tuple

FILE_TEXTURE_PX = 4096
CAP_PX = 4096
HELD_FOR_S = 20 * 60.0
# How far below the file zoom a cap may take a texture (dynamic zoom goes
# lower on its own; this only bounds the loop).
_MAX_LEVELS_DOWN = 4


def texture_px(tile_zoom: int, build_zoom: int) -> int:
    """Side in pixels of a tile's texture built at ``build_zoom``."""
    shift = int(build_zoom) - int(tile_zoom)
    if shift >= 0:
        return FILE_TEXTURE_PX << shift
    return max(1, FILE_TEXTURE_PX >> -shift)


def capped_zoom(tile_zoom: int, build_zoom: int, cap_px: int) -> int:
    """``build_zoom``, lowered until the texture fits ``cap_px`` (0: no cap)."""
    if not cap_px or cap_px <= 0:
        return int(build_zoom)
    zoom = int(build_zoom)
    floor = int(tile_zoom) - _MAX_LEVELS_DOWN
    while zoom > floor and texture_px(tile_zoom, zoom) > cap_px:
        zoom -= 1
    return zoom


def bytes_per_pixel(fmt: str) -> float:
    """BC1 (DXT1) packs half a byte a pixel; BC3 (DXT5) a byte."""
    return 1.0 if str(fmt).upper() in ("BC3", "DXT5") else 0.5


def texture_bytes(px: int, fmt: str = "BC1") -> int:
    """A texture's graphics memory with its smaller levels (a third more)."""
    side = max(4, int(px))
    return int(side * side * bytes_per_pixel(fmt) * 4 / 3)


class HeldTextures:
    """The photo textures X-Plane holds: finest level read, per texture."""

    def __init__(self, held_for: float = HELD_FOR_S,
                 clock: Callable[[], float] = time.monotonic):
        self._held_for = float(held_for)
        self._clock = clock
        self._lock = threading.Lock()
        self._textures: Dict[str, Tuple[int, float]] = {}

    def note(self, texture_id: str, px: int) -> None:
        """X-Plane read ``texture_id`` at ``px`` pixels a side."""
        now = self._clock()
        px = int(px)
        with self._lock:
            held = self._textures.get(texture_id)
            if held is not None and now - held[1] <= self._held_for:
                px = max(px, held[0])
            self._textures[texture_id] = (px, now)

    def _prune(self, now: float) -> None:
        old = [key for key, (_px, t) in self._textures.items()
               if now - t > self._held_for]
        for key in old:
            del self._textures[key]

    def bytes(self, fmt: str = "BC1") -> int:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return sum(texture_bytes(px, fmt) for px, _t in self._textures.values())

    def count(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._textures)

    def clear(self) -> None:
        with self._lock:
            self._textures.clear()


def cap_from_config(cfg) -> int:
    """The cap auto-tuning set (``[governor] texture_cap_px``), 0 when none."""
    try:
        value = int(float(getattr(getattr(cfg, "governor", None), "texture_cap_px", 0) or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def describe(cap_px: Optional[int]) -> str:
    if not cap_px:
        return "no texture size cap"
    return f"textures capped at {int(cap_px)} px"

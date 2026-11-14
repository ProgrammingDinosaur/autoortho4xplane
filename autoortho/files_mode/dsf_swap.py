"""Time exclusion in files mode: swap DSF hard links (plan section 5.10).

FUSE mode redirects each DSF open to X-Plane's global scenery at night.
Files mode has no opens to redirect; instead the pack's ``Earth nav data``
holds per-DSF hard links, and each one is pointed at the region's DSF (day)
or the global scenery's (night). Only squares X-Plane cannot be holding
(outside its window, with lag) are swapped, so a loaded DSF never changes
under X-Plane; the swap reaches it when it next loads that square.

Needs the pack's DSFs to be per-file links (the files folder on the same
volume as the scenery and the global scenery); otherwise it stays off and
says so once.
"""

import logging
import os
import re

from . import fsops, geo

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^([-+]\d{2})([-+]\d{3})\.dsf$", re.IGNORECASE)


def global_dsf(xplane_path, square):
    """X-Plane's own DSF for a square (global scenery, then demo areas)."""
    lat, lon = square
    folder = geo.square_folder(lat, lon)
    name = geo.square_name(lat, lon) + ".dsf"
    for pack in ("X-Plane 12 Global Scenery", "X-Plane 12 Demo Areas"):
        path = os.path.join(xplane_path, "Global Scenery", pack, "Earth nav data", folder, name)
        if os.path.isfile(path):
            return path
    return None


class DsfSwapper:
    def __init__(self, pack, xplane_path, is_night, enabled=lambda: True,
                 find_global=global_dsf):
        self.pack = pack
        self.xplane_path = xplane_path
        self.is_night = is_night
        self.enabled = enabled
        self.find_global = find_global
        self._warned = set()
        self.counters = {"files_dsf_swaps": 0}
        self.night = False

    def _pack_dsf(self, region, square):
        lat, lon = square
        return os.path.join(self.pack.region_dir(region), "Earth nav data",
                            geo.square_folder(lat, lon), geo.square_name(lat, lon) + ".dsf")

    def _region_dsf(self, region, square):
        lat, lon = square
        return os.path.join(self.pack.regions[region], "Earth nav data",
                            geo.square_folder(lat, lon), geo.square_name(lat, lon) + ".dsf")

    def sync(self, squares, xp_tiles):
        """Point the DSFs of ``squares`` (minus ``xp_tiles``) at day or night."""
        try:
            on = bool(self.enabled())
            night = on and bool(self.is_night())
        except Exception as exc:
            log.debug("Time exclusion state unavailable: %s", exc)
            return 0
        self.night = night
        swapped = 0
        for region in self.pack.regions:
            end = os.path.join(self.pack.region_dir(region), "Earth nav data")
            if fsops.is_link_or_junction(end):
                if on and region not in self._warned:
                    self._warned.add(region)
                    log.warning("Time exclusion is unavailable in files mode for %s: its "
                                "files folder is on another disk than the scenery", region)
                continue
            for square in squares:
                if square in xp_tiles:
                    continue
                dst = self._pack_dsf(region, square)
                if not os.path.exists(dst):
                    continue  # the region has no DSF here
                want = self._region_dsf(region, square)
                if night:
                    alt = self.find_global(self.xplane_path, square)
                    if alt is not None:
                        want = alt
                if self._swap(dst, want):
                    swapped += 1
        if swapped:
            self.counters["files_dsf_swaps"] += swapped
            log.info("Time exclusion: %d DSFs now %s", swapped,
                     "X-Plane's (night)" if night else "AutoOrtho's (day)")
        return swapped

    @staticmethod
    def _swap(dst, want):
        try:
            if fsops.file_id(dst) == fsops.file_id(want):
                return False
        except OSError:
            return False
        tmp = dst + f".ao-tmp-{os.getpid()}"
        if fsops.hard_link(want, tmp) != 0:
            log.debug("Cannot link %s (another volume?)", want)
            return False
        result = fsops.replace(tmp, dst, retry_s=0.5)
        if not result.ok:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        return True

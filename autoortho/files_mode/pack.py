"""The on-disk layout of files mode, and its links into Custom Scenery.

::

    <files root>/
      _ao/  textures.sqlite, catalogue/, neutral/, pack.json, rings.json
      z_ao_eur/
        Earth nav data/   hard links to the region's DSFs (junction if on another volume)
        terrain           junction -> <region root>/terrain (never written)
        textures/         managed by files mode: textures, mask links
        AOISWORKING       present while files mode runs
    Custom Scenery/z_ao_eur -> junction -> <files root>/z_ao_eur

The pack names equal FUSE's mount names, so ``scenery_packs.ini`` and the
overlays don't change between backends. Nothing here deletes recursively
through Custom Scenery (see ``fsops.remove_pack_link``).
"""

import json
import logging
import os
import time

from . import LAYOUT_VERSION, catalogue, fsops

log = logging.getLogger(__name__)

MARKER = "AOISWORKING"


class PackError(Exception):
    pass


def default_files_root(scenery_path):
    return os.path.join(scenery_path, "z_autoortho", "files")


class Pack:
    def __init__(self, files_root, custom_scenery, regions):
        """``regions``: ``{name: region_root}``."""
        self.files_root = os.path.abspath(files_root)
        self.custom_scenery = os.path.abspath(custom_scenery) if custom_scenery else None
        self.regions = dict(regions)
        self.meta_dir = os.path.join(self.files_root, "_ao")
        self.meta_path = os.path.join(self.meta_dir, "pack.json")
        os.makedirs(self.meta_dir, exist_ok=True)

    # -- metadata --------------------------------------------------------
    def _read_meta(self):
        try:
            with open(self.meta_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"layout": LAYOUT_VERSION, "regions": {}}
        if data.get("layout") != LAYOUT_VERSION:
            return {"layout": LAYOUT_VERSION, "regions": {}}
        return data

    def _write_meta(self, data):
        tmp = self.meta_path + f".ao-tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.meta_path)

    def region_dir(self, name):
        return os.path.join(self.files_root, name)

    def textures_dir(self, name):
        return os.path.join(self.region_dir(name), "textures")

    # -- setup -----------------------------------------------------------
    def setup(self, xplane_running=False, progress=None):
        """Set every region up (fast when unchanged). Returns changed names."""
        meta = self._read_meta()
        changed = []
        for name, root in sorted(self.regions.items()):
            fingerprint = catalogue.region_fingerprint(root)
            entry = meta["regions"].get(name)
            complete = entry and entry.get("fingerprint") == fingerprint
            if complete and self._looks_intact(name, root):
                continue
            if xplane_running and entry:
                raise PackError(
                    f"Region {name} changed since files mode last set it up. "
                    "Close X-Plane first, so it never sees new terrain next to old DSFs."
                )
            meta["regions"].pop(name, None)
            self._write_meta(meta)  # incomplete until rewritten below
            self._setup_region(name, root, progress)
            meta["regions"][name] = {
                "fingerprint": fingerprint, "root": root, "set_up_at": time.time(),
                "dsf_linked": not fsops.is_link_or_junction(
                    os.path.join(self.region_dir(name), "Earth nav data")
                ),
            }
            self._write_meta(meta)
            changed.append(name)
        for name in list(meta["regions"]):
            if name not in self.regions:
                self.remove_region(name)
                meta["regions"].pop(name, None)
                self._write_meta(meta)
        return changed

    def _looks_intact(self, name, root):
        rdir = self.region_dir(name)
        terrain = os.path.join(rdir, "terrain")
        return (
            os.path.isdir(self.textures_dir(name))
            and fsops.is_link_or_junction(terrain)
            and fsops.link_target(terrain) is not None
            and os.path.normcase(fsops.link_target(terrain))
            == os.path.normcase(os.path.normpath(os.path.join(root, "terrain")))
            and os.path.isdir(os.path.join(rdir, "Earth nav data"))
        )

    def _setup_region(self, name, root, progress=None):
        started = time.monotonic()
        rdir = self.region_dir(name)
        os.makedirs(rdir, exist_ok=True)
        textures = self.textures_dir(name)
        os.makedirs(textures, exist_ok=True)
        # terrain: one junction instead of copying every .ter.
        fsops.ensure_dir_link(os.path.join(root, "terrain"), os.path.join(rdir, "terrain"),
                              files_root=self.files_root)
        # Earth nav data: per-DSF hard links on the same volume, so single DSFs
        # can be swapped later (time exclusion); a junction otherwise.
        src_end = os.path.join(root, "Earth nav data")
        dst_end = os.path.join(rdir, "Earth nav data")
        if fsops.same_volume(root, self.files_root) and os.path.isdir(src_end):
            if fsops.is_link_or_junction(dst_end):
                _unlink_own_link(dst_end, (self.files_root, root))
            os.makedirs(dst_end, exist_ok=True)
            linked = 0
            for folder in os.scandir(src_end):
                if not folder.is_dir():
                    continue
                out = os.path.join(dst_end, folder.name)
                os.makedirs(out, exist_ok=True)
                for dsf in os.scandir(folder.path):
                    if dsf.name.lower().endswith(".dsf"):
                        if fsops.link_or_copy(dsf.path, os.path.join(out, dsf.name)):
                            linked += 1
            log.info("Pack %s: %d DSFs linked", name, linked)
        elif os.path.isdir(src_end):
            if os.path.isdir(dst_end) and not fsops.is_link_or_junction(dst_end):
                _remove_tree_under(dst_end, self.files_root)
            fsops.ensure_dir_link(src_end, dst_end, files_root=None)
            log.info("Pack %s: Earth nav data is on another volume; linked as a folder "
                     "(time exclusion unavailable in files mode)", name)
        # Masks and other non-texture files the .ter files name.
        src_tex = os.path.join(root, "textures")
        linked = 0
        if os.path.isdir(src_tex):
            for entry in os.scandir(src_tex):
                if entry.is_file() and not entry.name.lower().endswith(".dds"):
                    if fsops.link_or_copy(entry.path, os.path.join(textures, entry.name)):
                        linked += 1
                        if progress is not None and linked % 20000 == 0:
                            progress(name, linked)
        self.cleanup_temp(name)
        log.info("Pack %s set up in %.1fs (%d mask files)", name,
                 time.monotonic() - started, linked)

    def cleanup_temp(self, name):
        removed = 0
        try:
            for entry in os.scandir(self.textures_dir(name)):
                if entry.name.startswith(".ao-tmp-"):
                    try:
                        os.remove(entry.path)
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def remove_region(self, name):
        rdir = self.region_dir(name)
        if os.path.isdir(rdir):
            _remove_tree_under(rdir, self.files_root)
            log.info("Removed pack folder of region %s", name)

    # -- links in Custom Scenery -------------------------------------------
    def link_path(self, name):
        return os.path.join(self.custom_scenery, name)

    def install_links(self):
        """Junctions in Custom Scenery. Raises PackError on anything foreign."""
        if not self.custom_scenery or not os.path.isdir(self.custom_scenery):
            raise PackError(f"Custom Scenery folder not found: {self.custom_scenery}")
        for name in sorted(self.regions):
            link = self.link_path(name)
            try:
                fsops.ensure_dir_link(self.region_dir(name), link, files_root=self.files_root)
            except fsops.RefuseToRemove as exc:
                raise PackError(
                    f"{link} is in the way ({exc}). If AutoOrtho's FUSE mode is still "
                    "mounted, stop it first; otherwise move that folder away."
                ) from exc

    def remove_links(self):
        """Remove our junctions from Custom Scenery; never anything else."""
        removed = []
        if not self.custom_scenery or not os.path.isdir(self.custom_scenery):
            return removed
        for entry in os.listdir(self.custom_scenery):
            if not entry.startswith("z_ao_"):
                continue
            link = os.path.join(self.custom_scenery, entry)
            if not fsops.is_link_or_junction(link):
                continue
            try:
                fsops.remove_pack_link(link, self.files_root)
                removed.append(entry)
            except fsops.RefuseToRemove:
                continue
            except OSError as exc:
                log.warning("Could not remove %s: %s", link, exc)
        return removed

    def links_ok(self):
        missing = []
        for name in self.regions:
            link = self.link_path(name)
            if not (fsops.is_link_or_junction(link) and fsops.resolves_under(link, self.files_root)):
                missing.append(name)
        return missing

    def write_markers(self):
        for name in self.regions:
            try:
                with open(os.path.join(self.region_dir(name), MARKER), "w") as handle:
                    handle.write(str(os.getpid()))
            except OSError:
                pass

    def remove_markers(self):
        for name in self.regions:
            try:
                os.remove(os.path.join(self.region_dir(name), MARKER))
            except OSError:
                pass


def _unlink_own_link(path, allowed_roots):
    """Remove a link inside the pack when it points under one of our roots."""
    for root in allowed_roots:
        if fsops.resolves_under(path, root):
            if fsops.is_junction(path):
                os.rmdir(path)
            else:
                os.unlink(path)
            return
    raise fsops.RefuseToRemove(f"{path} points outside AutoOrtho's folders")


def _remove_tree_under(path, files_root):
    """Delete a folder inside the files root without following any link."""
    root_n = os.path.normcase(os.path.abspath(files_root)) + os.sep
    if not os.path.normcase(os.path.abspath(path)).startswith(root_n):
        raise fsops.RefuseToRemove(f"{path} is not under {files_root}")
    if fsops.is_link_or_junction(path):
        if fsops.is_junction(path):
            os.rmdir(path)
        else:
            os.unlink(path)
        return
    for entry in os.scandir(path):
        full = entry.path
        if fsops.is_link_or_junction(full):
            if fsops.is_junction(full):
                os.rmdir(full)
            else:
                os.unlink(full)
        elif entry.is_dir(follow_symlinks=False):
            _remove_tree_under(full, files_root)
        else:
            try:
                os.remove(full)
            except OSError as exc:
                log.debug("Could not remove %s: %s", full, exc)
    os.rmdir(path)

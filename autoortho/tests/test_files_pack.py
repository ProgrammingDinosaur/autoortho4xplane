"""Files mode: catalogue, pack layout and the link-safety rules."""

import os
import sys

import pytest

import files_mode_helpers as h
from files_mode import catalogue, fsops, geo, pack


def test_catalogue_scan_dedupes_and_groups(tmp_path):
    textures = h.textures_around(47.5, 8.5, radius=2)
    root = h.make_region(str(tmp_path), "z_ao_eur", textures)
    # An odd name read through its BASE_TEX line.
    with open(os.path.join(root, "terrain", "custom.ter"), "w") as handle:
        handle.write("A\n800\nTERRAIN\nBASE_TEX ../textures/123_456_XY17.dds\n")
    cat = catalogue.scan_region(root)
    assert len(cat) == len(textures) + 1  # overlays deduped
    names = {cat.name(i) for i in range(len(cat))}
    assert "123_456_XY17.dds" in names
    for row, col, mt, zoom in textures:
        for sq in geo.squares_of(row, col, zoom):
            idx = cat.indices_in(sq)
            assert geo.texture_name(row, col, mt, zoom) in {cat.name(i) for i in idx}


def test_catalogue_cache_and_fingerprint(tmp_path):
    root = h.make_region(str(tmp_path), "z_ao_eur", h.textures_around(47.5, 8.5, radius=1))
    cats = catalogue.Catalogue(str(tmp_path / "cache"))
    first = cats.load_or_scan("z_ao_eur", root)
    assert os.path.isfile(cats.cache_path("z_ao_eur"))
    again = catalogue.Catalogue(str(tmp_path / "cache")).load_or_scan("z_ao_eur", root)
    assert len(again) == len(first)
    assert again.fingerprint == first.fingerprint


def _pack(tmp_path, textures=None):
    scenery = tmp_path / "scenery"
    root = h.make_region(str(scenery), "z_ao_eur", textures or h.textures_around(47.5, 8.5, radius=1))
    custom = tmp_path / "Custom Scenery"
    custom.mkdir()
    files_root = tmp_path / "files"
    return pack.Pack(str(files_root), str(custom), {"z_ao_eur": root}), root, custom, files_root


def test_pack_setup_links_everything(tmp_path):
    p, root, custom, files_root = _pack(tmp_path)
    assert p.setup() == ["z_ao_eur"]
    rdir = p.region_dir("z_ao_eur")
    assert fsops.is_link_or_junction(os.path.join(rdir, "terrain"))
    assert os.path.isfile(os.path.join(rdir, "terrain", os.listdir(os.path.join(root, "terrain"))[0]))
    dsfs = [f for _d, _s, files in os.walk(os.path.join(rdir, "Earth nav data")) for f in files]
    assert dsfs and all(f.endswith(".dsf") for f in dsfs)
    assert os.path.isfile(os.path.join(p.textures_dir("z_ao_eur"), "water_transition.png"))
    # Unchanged: nothing to do.
    assert p.setup() == []


def test_install_and_remove_links(tmp_path):
    p, root, custom, files_root = _pack(tmp_path)
    p.setup()
    p.install_links()
    link = custom / "z_ao_eur"
    assert fsops.is_link_or_junction(str(link))
    assert p.links_ok() == []
    assert p.remove_links() == ["z_ao_eur"]
    assert not os.path.lexists(link)
    # The pack and the region survive.
    assert os.path.isdir(p.region_dir("z_ao_eur"))
    assert os.listdir(os.path.join(root, "terrain"))


def test_install_refuses_a_real_folder(tmp_path):
    p, root, custom, files_root = _pack(tmp_path)
    p.setup()
    real = custom / "z_ao_eur"
    real.mkdir()
    (real / "keep.txt").write_text("user data")
    with pytest.raises(pack.PackError):
        p.install_links()
    assert (real / "keep.txt").read_text() == "user data"


def test_install_replaces_an_empty_stale_mount_folder(tmp_path):
    p, root, custom, files_root = _pack(tmp_path)
    p.setup()
    (custom / "z_ao_eur").mkdir()
    p.install_links()
    assert fsops.is_link_or_junction(str(custom / "z_ao_eur"))


def test_remove_pack_link_refuses_folders_and_foreign_links(tmp_path):
    files_root = tmp_path / "files"
    files_root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (real / "x").write_text("x")
    with pytest.raises(fsops.RefuseToRemove):
        fsops.remove_pack_link(str(real), str(files_root))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = tmp_path / "foreign"
    fsops.create_dir_link(str(elsewhere), str(foreign))
    with pytest.raises(fsops.RefuseToRemove):
        fsops.remove_pack_link(str(foreign), str(files_root))
    assert (real / "x").exists() and elsewhere.exists()


def test_remove_region_never_follows_the_terrain_junction(tmp_path):
    p, root, custom, files_root = _pack(tmp_path)
    p.setup()
    before = sorted(os.listdir(os.path.join(root, "terrain")))
    p.remove_region("z_ao_eur")
    assert not os.path.exists(p.region_dir("z_ao_eur"))
    assert sorted(os.listdir(os.path.join(root, "terrain"))) == before
    assert os.listdir(os.path.join(root, "Earth nav data"))


def test_remove_tree_refuses_outside_files_root(tmp_path):
    with pytest.raises(fsops.RefuseToRemove):
        pack._remove_tree_under(str(tmp_path / "x"), str(tmp_path / "files"))


def test_sparse_files_and_holes(tmp_path):
    path = str(tmp_path / "t.dds")
    size = 11184952
    fd = fsops.open_new(path)
    try:
        fsops.make_sparse(fd, size)
        fsops.pwrite(fd, b"DDS " + b"\0" * 124, 0)
        fsops.pwrite(fd, b"\x01" * 43704, size - 43704)
    finally:
        os.close(fd)
    assert os.path.getsize(path) == size
    small = fsops.allocated_size(path)
    assert small <= 256 * 1024
    fd = fsops.open_rw(path)
    try:
        fsops.pwrite(fd, b"\x02" * (4 << 20), 128)
    finally:
        os.close(fd)
    assert fsops.allocated_size(path) >= 4 << 20
    fd = fsops.open_rw(path)
    try:
        assert fsops.punch_hole(fd, 65536, (4 << 20) - 65536 * 2)
    finally:
        os.close(fd)
    assert fsops.allocated_size(path) < 1 << 20
    with open(path, "rb") as handle:
        handle.seek(1 << 20)
        assert handle.read(16) == b"\0" * 16


def test_replace(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"new")
    b.write_bytes(b"old")
    assert fsops.replace(str(a), str(b)).ok
    assert b.read_bytes() == b"new"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS sharing rules")
def test_windows_mapped_name_refuses_replace_but_allows_in_place(tmp_path):
    """A9 on Windows: what the store's strategy relies on."""
    import subprocess
    target = tmp_path / "mapped.dds"
    target.write_bytes(b"A" * 100000)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import mmap,sys,time;f=open(sys.argv[1],'rb');m=mmap.mmap(f.fileno(),0,"
         "access=mmap.ACCESS_READ);f.close();print('ok',flush=True);time.sleep(20)",
         str(target)],
        stdout=subprocess.PIPE,
    )
    try:
        holder.stdout.readline()
        tmp = tmp_path / "tmp.dds"
        tmp.write_bytes(b"B" * 100000)
        res = fsops.replace(str(tmp), str(target), retry_s=0.2)
        assert not res.ok
        fd = fsops.open_rw(str(target))
        try:
            fsops.pwrite(fd, b"C", 10)
        finally:
            os.close(fd)
        assert target.read_bytes()[10:11] == b"C"
    finally:
        holder.kill()
        holder.wait()

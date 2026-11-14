import os

from autoortho.utils.cache_cleanup import (
    delete_all_cache_contents,
    is_cache_file,
    remove_stale_temp_files,
)


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x")


def _populate(root):
    files = [
        "21071_14027_15_GO2.jpg",
        ".21071_14027_15_GO2.jpg.aa4ae0bf040d4e4b865fd66703d2e473.tmp",
        "+24+049.si",
        "-05-123.si",
        "ao_+24+049.dsf.txt",
        "global_+24+049.dsf.txt",
        "temp_mesh_+24+049.dsf",
        "row_col_BI_16_16.dds",
        "tile.dds.tmp.4242",
        os.path.join("terrain_index", "abc.json"),
        os.path.join("dds_cache", "+20+040", "+24+049", "BI", "1_2_z16.dds"),
    ]
    for name in files:
        _touch(os.path.join(root, name))


def test_delete_all_empties_cache_dir(tmp_path):
    _populate(str(tmp_path))
    removed, failures = delete_all_cache_contents(str(tmp_path))
    assert failures == 0
    assert removed > 0
    assert os.listdir(tmp_path) == []


def test_delete_all_keeps_unrelated_files(tmp_path):
    _populate(str(tmp_path))
    _touch(str(tmp_path / "notes.txt"))
    _touch(str(tmp_path / "my_folder" / "keep.bin"))
    delete_all_cache_contents(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["my_folder", "notes.txt"]


def test_delete_all_missing_dir(tmp_path):
    assert delete_all_cache_contents(str(tmp_path / "missing")) == (0, 0)


def test_remove_stale_temp_files_only_removes_temps(tmp_path):
    _populate(str(tmp_path))
    removed = remove_stale_temp_files(str(tmp_path))
    assert removed == 2
    remaining = os.listdir(tmp_path)
    assert "+24+049.si" in remaining
    assert "21071_14027_15_GO2.jpg" in remaining
    assert not any(name.endswith(".tmp") or ".tmp." in name for name in remaining)


def test_is_cache_file_patterns():
    assert is_cache_file("+26+053.si")
    assert is_cache_file(".x.jpg.847ee94889a24d08a77fdd54ab49418e.tmp")
    assert not is_cache_file("readme.si.bak")
    assert not is_cache_file("config.ini")

"""Scenery actions keep UI region IDs separate from installed package names."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from downloader import Release, RoughnessApplyStatus
from ui import workers
from utils.ter_utils import ter_utils


@pytest.mark.parametrize("region_id", ["na", "z_ao_na"])
@pytest.mark.parametrize("restore", [False, True])
def test_seasons_worker_targets_package_and_preserves_signal_id(
    qt_app, monkeypatch, tmp_path, region_id, restore
):
    calls = []

    def patch(package_name, progress_callback):
        calls.append(package_name)
        progress_callback({"pcnt_done": 100})
        return True

    if restore:
        monkeypatch.setattr(workers.dsf_utils, "restore_default_dsfs", patch)
        worker = workers.RestoreDefaultDsfsWorker(None, region_id)
    else:
        monkeypatch.setattr(workers.dsf_utils, "add_seasons_to_package", patch)
        worker = workers.AddSeasonsWorker(region_id, str(tmp_path))
    finished, progress = [], []
    worker.finished.connect(lambda rid, success: finished.append((rid, success)))
    worker.progress.connect(lambda rid, payload: progress.append(rid))

    worker.run()

    assert calls == ["z_ao_na"]
    assert finished == [(region_id, True)]
    assert progress and set(progress) == {region_id}


@pytest.mark.parametrize("region_id", ["na", "z_ao_na"])
def test_reflectivity_worker_patches_installed_files_and_updates_status(
    qt_app, monkeypatch, tmp_path, region_id
):
    monkeypatch.setattr(ter_utils, "ao_path", str(tmp_path))
    terrain = tmp_path / "z_autoortho" / "scenery" / "z_ao_na" / "terrain"
    terrain.mkdir(parents=True)
    ter_file = terrain / "tile.ter"
    ter_file.write_text("A\n800\nTERRAIN\n")
    info_path = tmp_path / "z_autoortho" / "na_info.json"
    info_path.write_text(json.dumps({"ver": "1.0"}))

    worker = workers.AddRoughnessWorker(region_id, str(tmp_path), 0.8)
    finished = []
    worker.finished.connect(lambda rid, success: finished.append((rid, success)))
    worker.run()

    assert finished == [(region_id, True)]
    assert "SUPER_ROUGHNESS 0.8" in ter_file.read_text()
    release = Release("na", install_dir=str(tmp_path))
    release.load(str(info_path))
    assert release.roughness_apply_status == RoughnessApplyStatus.APPLIED
    assert release.roughness_value == 0.8

    restore = workers.RestoreRoughnessWorker(region_id, str(tmp_path))
    restore.finished.connect(lambda rid, success: finished.append((rid, success)))
    restore.run()

    assert finished == [(region_id, True), (region_id, True)]
    assert "SUPER_ROUGHNESS" not in ter_file.read_text()
    release.load(str(info_path))
    assert release.roughness_apply_status == RoughnessApplyStatus.NOT_APPLIED

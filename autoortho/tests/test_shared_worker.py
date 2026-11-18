"""One mount worker serving every scenery mount ([fuse] shared_worker)."""
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

import mount_worker
import process_supervisor as ps
from process_supervisor import AOProcessSupervisor


class DummyProcess:
    pid = 4321
    returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0


MOUNTS = [
    ("/ao/z_ao_na", "/xp/Custom Scenery/z_ao_na", "z_ao_na"),
    ("/ao/z_ao_eur", "/xp/Custom Scenery/z_ao_eur", "z_ao_eur"),
]


def _start(monkeypatch, tmp_path, mounts):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return DummyProcess()

    monkeypatch.setattr(ps, "_is_frozen", lambda: False)
    monkeypatch.setattr(ps.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(AOProcessSupervisor, "_attach_windows_job", lambda self, p: None)
    supervisor = AOProcessSupervisor()
    first = mounts[0]
    handle = supervisor.start_mount_worker(
        first[0], first[1], first[2], nothreads=False, mounts=mounts,
    )
    return supervisor, handle, captured["cmd"]


def test_shared_worker_command_lists_every_mount(monkeypatch, tmp_path):
    supervisor, handle, cmd = _start(monkeypatch, tmp_path, MOUNTS)
    listed = json.loads(cmd[cmd.index("--mounts-json") + 1])
    assert [tuple(m) for m in listed] == [
        (os.path.expanduser(r), os.path.expanduser(m), v) for r, m, v in MOUNTS
    ]
    assert len(handle.mounts) == 2
    assert handle.mountpoint == os.path.expanduser(MOUNTS[0][1])
    handle.process.returncode = 0
    supervisor.stop_all(timeout=0)


def test_single_mount_has_no_mount_list(monkeypatch, tmp_path):
    supervisor, handle, cmd = _start(monkeypatch, tmp_path, MOUNTS[:1])
    assert "--mounts-json" not in cmd
    assert len(handle.mounts) == 1
    handle.process.returncode = 0
    supervisor.stop_all(timeout=0)


class _FakeCacher:
    instances = []

    def __init__(self, cache_dir):
        _FakeCacher.instances.append(self)


class _FakeAutoOrtho:
    def __init__(self, root, use_ns=False, tile_cacher=None):
        self.root = root
        self.tc = tile_cacher


def _runtime(fuse):
    return (fuse, _FakeAutoOrtho, lambda nothreads, name: {"VolumeName": name}, "winfsp-FUSE")


@pytest.fixture
def worker_env(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def fake_setupmount(mountpoint, systemtype):
        yield mountpoint

    _FakeCacher.instances = []
    monkeypatch.setattr(mount_worker, "setupmount", fake_setupmount)
    monkeypatch.setattr(mount_worker, "_clear_root_poison", lambda root: None)
    import getortho
    monkeypatch.setattr(getortho, "TileCacher", _FakeCacher)
    mount_worker._worker_stopping.clear()
    yield monkeypatch
    mount_worker._worker_stopping.clear()


def test_every_mount_shares_one_tile_cacher(worker_env):
    served = []
    release = threading.Event()

    def fuse(ao, path, **options):
        served.append((ao.root, ao.tc, options["VolumeName"]))
        release.wait(5)

    poisoned = []
    worker_env.setattr(mount_worker, "_poison_mount", poisoned.append)
    runner = threading.Thread(
        target=mount_worker._serve_shared, args=(MOUNTS, False, _runtime(fuse))
    )
    runner.start()
    for _ in range(100):
        if len(served) == 2:
            break
        threading.Event().wait(0.02)
    mount_worker._worker_stopping.set()  # a normal shutdown
    release.set()
    runner.join(5)
    assert not runner.is_alive()
    assert len(_FakeCacher.instances) == 1
    assert {s[1] for s in served} == {_FakeCacher.instances[0]}
    assert sorted(s[2] for s in served) == ["z_ao_eur", "z_ao_na"]
    assert poisoned == []


def test_one_failed_mount_takes_the_others_down(worker_env):
    stop_eur = threading.Event()

    def fuse(ao, path, **options):
        if options["VolumeName"] == "z_ao_na":
            raise RuntimeError("mount refused")
        stop_eur.wait(5)

    poisoned = []

    def poison(path):
        poisoned.append(path)
        stop_eur.set()

    worker_env.setattr(mount_worker, "_poison_mount", poison)
    with pytest.raises(RuntimeError, match="mount refused"):
        mount_worker._serve_shared(MOUNTS, False, _runtime(fuse))
    assert set(poisoned) <= {os.path.abspath(MOUNTS[1][1])}


def test_parent_launches_one_shared_worker(monkeypatch):
    import autoortho as ao_main

    launched = []
    mount = SimpleNamespace(
        cfg=SimpleNamespace(fuse=SimpleNamespace(shared_worker=True)),
        _active_mountpoints=[],
        stats_addr=None, stats_auth=None, log_addr=None,
        launch_mount_worker=lambda *a, **k: launched.append((a, k)) or "handle",
    )
    monkeypatch.setattr(ao_main, "system_type", "windows")
    assert ao_main.AOMount._shared_worker_enabled(mount)
    sceneries = [{"root": r, "mount": m} for r, m, _ in MOUNTS]
    assert ao_main.AOMount._launch_shared_worker(mount, sceneries) == "handle"
    args, kwargs = launched[0]
    assert [m[2] for m in kwargs["mounts"]] == ["z_ao_na", "z_ao_eur"]
    assert mount._active_mountpoints == [os.path.expanduser(m) for _, m, _ in MOUNTS]

    monkeypatch.setattr(ao_main, "system_type", "darwin")
    assert not ao_main.AOMount._shared_worker_enabled(mount)


def test_worker_refuses_several_mounts_on_macos(monkeypatch):
    monkeypatch.setattr(mount_worker, "system_type", "darwin")
    monkeypatch.setattr(sys, "argv", [
        "worker", "--root", MOUNTS[0][0], "--mountpoint", MOUNTS[0][1],
        "--mounts-json", json.dumps([list(m) for m in MOUNTS]),
    ])
    with pytest.raises(SystemExit):
        mount_worker.main()

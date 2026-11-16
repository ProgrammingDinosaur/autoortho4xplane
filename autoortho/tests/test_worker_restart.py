import importlib
import os
import sys
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

from worker_restart import RestartPolicy


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_policy_allows_a_few_restarts_per_mount():
    clock = Clock()
    policy = RestartPolicy(max_restarts=2, window_seconds=600, clock=clock)

    assert policy.allow("/x/z_ao_aus_pac")
    assert policy.allow("/x/z_ao_aus_pac")
    assert not policy.allow("/x/z_ao_aus_pac")
    # Another region keeps its own allowance.
    assert policy.allow("/x/z_ao_eur")


def test_policy_forgets_restarts_outside_the_window():
    clock = Clock()
    policy = RestartPolicy(max_restarts=1, window_seconds=600, clock=clock)

    assert policy.allow("m")
    assert not policy.allow("m")
    clock.now = 601.0
    assert policy.allow("m")
    assert policy.count("m") == 1


def test_policy_reset_clears_history():
    policy = RestartPolicy(max_restarts=1, clock=Clock())
    assert policy.allow("m")
    policy.reset()
    assert policy.allow("m")


class DeadProcess:
    pid = 4242

    def poll(self):
        return 1


def _mount_with_dead_worker(autoortho_mod, mounts=()):
    mount = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    process = DeadProcess()
    handle = SimpleNamespace(
        process=process,
        pid=process.pid,
        root="/r/aus",
        mountpoint="/m/z_ao_aus_pac",
        volname="z_ao_aus_pac",
        mounts=mounts,
    )
    stopped = []
    mount.mount_worker_supervisor = SimpleNamespace(
        stop_worker=lambda h, timeout=None: stopped.append(h) or True
    )
    mount.mount_workers = [handle]
    mount.mac_os_procs = [process]
    mount.mounts_running = True
    mount.cfg = SimpleNamespace(fuse=SimpleNamespace(threading=True))
    mount.stats_addr = "stats"
    mount.stats_auth = b"auth"
    mount.log_addr = "log"
    return mount, handle, stopped


def test_restart_detaches_stale_mount_and_relaunches(monkeypatch):
    autoortho_mod = importlib.import_module("autoortho")
    monkeypatch.setattr(autoortho_mod, "system_type", "linux")
    mount, handle, stopped = _mount_with_dead_worker(autoortho_mod)
    unmounted = []
    monkeypatch.setattr(
        mount, "unmount",
        lambda path, force=False, wait_timeout=10.0: unmounted.append((path, force)) or True,
    )
    launched = []
    new_handle = SimpleNamespace(pid=5151)

    def launch(root, mountpoint, volname, nothreads, *args, mounts=None):
        launched.append((root, mountpoint, volname, nothreads, args, mounts))
        return new_handle

    monkeypatch.setattr(mount, "launch_mount_worker", launch)

    assert mount.restart_mount_worker(handle) is new_handle
    assert stopped == [handle]
    assert handle not in mount.mount_workers
    assert handle.process not in mount.mac_os_procs
    assert unmounted == [("/m/z_ao_aus_pac", True)]
    assert launched == [(
        "/r/aus", "/m/z_ao_aus_pac", "z_ao_aus_pac", False,
        ("stats", b"auth", "log"), None,
    )]


def test_restart_relaunches_every_mount_of_a_shared_worker(monkeypatch):
    autoortho_mod = importlib.import_module("autoortho")
    monkeypatch.setattr(autoortho_mod, "system_type", "windows")
    mounts = (("/r/a", "/m/a", "a"), ("/r/b", "/m/b", "b"))
    mount, handle, _stopped = _mount_with_dead_worker(autoortho_mod, mounts)
    unmounted = []
    monkeypatch.setattr(
        mount, "unmount",
        lambda path, force=False, wait_timeout=10.0: unmounted.append(path) or True,
    )
    launched = []
    monkeypatch.setattr(
        mount, "launch_mount_worker",
        lambda *args, mounts=None: launched.append(mounts) or SimpleNamespace(pid=1),
    )

    assert mount.restart_mount_worker(handle) is not None
    assert unmounted == ["/m/a", "/m/b"]
    assert launched == [list(mounts)]


def test_restart_gives_up_when_stale_mount_stays_attached(monkeypatch):
    autoortho_mod = importlib.import_module("autoortho")
    monkeypatch.setattr(autoortho_mod, "system_type", "linux")
    mount, handle, _stopped = _mount_with_dead_worker(autoortho_mod)
    monkeypatch.setattr(mount, "unmount", lambda *a, **k: False)
    launched = []
    monkeypatch.setattr(
        mount, "launch_mount_worker", lambda *a, **k: launched.append(a)
    )

    assert mount.restart_mount_worker(handle) is None
    assert launched == []


def test_restart_skipped_once_streaming_stopped(monkeypatch):
    autoortho_mod = importlib.import_module("autoortho")
    monkeypatch.setattr(autoortho_mod, "system_type", "linux")
    mount, handle, _stopped = _mount_with_dead_worker(autoortho_mod)
    mount.mounts_running = False
    monkeypatch.setattr(mount, "unmount", lambda *a, **k: True)
    launched = []
    monkeypatch.setattr(
        mount, "launch_mount_worker", lambda *a, **k: launched.append(a)
    )

    assert mount.restart_mount_worker(handle) is None
    assert launched == []

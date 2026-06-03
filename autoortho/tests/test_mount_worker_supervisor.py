import os
import signal
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from process_supervisor import AOProcessSupervisor, WorkerHandle
from worker_modes import is_mount_worker_mode


class DummyProcess:
    def __init__(self, pid=1234, wait_timeout=False):
        self.pid = pid
        self._handle = 999
        self.returncode = None
        self.wait_timeout = wait_timeout
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.wait_timeout:
            raise subprocess.TimeoutExpired(["dummy"], timeout)
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_mount_worker_command_and_env_non_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import process_supervisor as ps

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(ps, "_is_frozen", lambda: False)
    monkeypatch.setattr(ps.subprocess, "Popen", fake_popen)

    supervisor = AOProcessSupervisor()
    handle = supervisor.start_mount_worker(
        "/ao/root",
        "/xp/Custom Scenery/z_autoortho",
        "z_autoortho",
        nothreads=True,
        stats_addr="127.0.0.1:1234",
        stats_auth=b"AUTH",
        log_addr="127.0.0.1:2345",
        loglevel="debug",
    )

    assert captured["cmd"][:3] == [sys.executable, "-m", "autoortho"]
    assert "--root" in captured["cmd"]
    assert "--mountpoint" in captured["cmd"]
    assert "--nothreads" in captured["cmd"]
    assert captured["kwargs"]["env"]["AO_RUN_MODE"] == "mount_worker"
    assert captured["kwargs"]["env"]["AO_STATS_ADDR"] == "127.0.0.1:1234"
    assert captured["kwargs"]["env"]["AO_STATS_AUTH"] == "AUTH"
    assert captured["kwargs"]["env"]["AO_LOG_ADDR"] == "127.0.0.1:2345"
    assert captured["kwargs"]["start_new_session"] is True
    assert "creationflags" not in captured["kwargs"]

    handle.process.returncode = 0
    supervisor.stop_all(timeout=0)


def test_mount_worker_command_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import process_supervisor as ps

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(ps, "_is_frozen", lambda: True)
    monkeypatch.setattr(ps.subprocess, "Popen", fake_popen)

    supervisor = AOProcessSupervisor()
    handle = supervisor.start_mount_worker("/ao/root", "/xp/z_autoortho", "z_autoortho", False)

    assert captured["cmd"][0] == sys.executable
    assert "-m" not in captured["cmd"]
    assert captured["kwargs"]["env"]["AO_RUN_MODE"] == "mount_worker"

    handle.process.returncode = 0
    supervisor.stop_all(timeout=0)


def test_posix_stop_escalates_from_sigterm_to_sigkill(monkeypatch):
    import process_supervisor as ps

    process = DummyProcess(pid=4321, wait_timeout=True)
    handle = WorkerHandle(process, "/root", "/mount", "mount")
    supervisor = AOProcessSupervisor()
    supervisor.handles.append(handle)

    calls = []
    monkeypatch.setattr(ps.os, "getpgid", lambda pid: 9876)
    monkeypatch.setattr(ps.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    supervisor.stop_worker(handle, timeout=0.01)

    assert calls == [(9876, signal.SIGTERM), (9876, signal.SIGKILL)]
    assert handle not in supervisor.handles


def test_windows_worker_uses_process_group_and_job_object(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import process_supervisor as ps

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(ps.os, "name", "nt", raising=False)
    monkeypatch.setattr(ps.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ps, "_is_frozen", lambda: False)
    monkeypatch.setattr(ps.subprocess, "Popen", fake_popen)

    supervisor = AOProcessSupervisor()
    monkeypatch.setattr(supervisor, "_attach_windows_job", lambda process: 55)
    monkeypatch.setattr(supervisor, "_close_windows_handle", lambda handle: None)

    handle = supervisor.start_mount_worker(
        "/ao/root",
        "C:/X-Plane/Custom Scenery/z_autoortho",
        "z_autoortho",
        nothreads=False,
    )

    assert captured["kwargs"]["env"]["AO_RUN_MODE"] == "mount_worker"
    assert captured["kwargs"]["creationflags"] & getattr(ps.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert captured["kwargs"]["creationflags"] & getattr(ps.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert handle.job_handle == 55

    handle.process.returncode = 0
    supervisor.stop_all(timeout=0)


def test_windows_force_kill_falls_back_to_taskkill(monkeypatch):
    import process_supervisor as ps

    process = DummyProcess(pid=2468)
    handle = WorkerHandle(process, "/root", "C:/mount", "mount")
    supervisor = AOProcessSupervisor()

    calls = []
    monkeypatch.setattr(ps.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        ps.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(cmd),
    )

    supervisor.kill_worker_tree(handle)

    assert calls == [["taskkill", "/T", "/F", "/PID", "2468"]]
    assert process.killed is True


def test_worker_mode_aliases():
    assert is_mount_worker_mode("mount_worker")
    assert is_mount_worker_mode("macfuse_worker")
    assert not is_mount_worker_mode("gui")
    assert not is_mount_worker_mode(None)


def test_unmount_sceneries_unmounts_before_stopping_workers():
    import importlib
    autoortho_mod = importlib.import_module("autoortho.autoortho")

    aom = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    aom.cfg = SimpleNamespace(scenery_mounts=[])
    aom._active_mountpoints = ["/tmp/ao-mount"]
    aom.mounts_running = True

    calls = []

    def fake_unmount(mountpoint, force=False, wait_timeout=None):
        calls.append(("unmount", mountpoint, force, wait_timeout))

    def fake_stop_mount_workers(timeout=None):
        calls.append(("stop_workers", timeout))

    aom.unmount = fake_unmount
    aom.stop_mount_workers = fake_stop_mount_workers
    aom.stop_reporter = lambda: calls.append(("stop_reporter",))
    aom.stop_stats_manager = lambda: calls.append(("stop_stats",))
    aom.stop_log_server = lambda: calls.append(("stop_log",))

    autoortho_mod.AOMount.unmount_sceneries(aom)

    assert calls[0] == ("unmount", "/tmp/ao-mount", False, 3.0)
    assert calls[1] == ("stop_workers", 3.0)

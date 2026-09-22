import os
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

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
        extra_env={"AO_PROFILE_SESSION_ID": "test-profile"},
    )

    assert captured["cmd"][:3] == [sys.executable, "-m", "autoortho"]
    assert "--root" in captured["cmd"]
    assert "--mountpoint" in captured["cmd"]
    assert "--nothreads" in captured["cmd"]
    assert captured["kwargs"]["env"]["AO_RUN_MODE"] == "mount_worker"
    assert captured["kwargs"]["env"]["AO_STATS_ADDR"] == "127.0.0.1:1234"
    assert captured["kwargs"]["env"]["AO_STATS_AUTH"] == "AUTH"
    assert captured["kwargs"]["env"]["AO_LOG_ADDR"] == "127.0.0.1:2345"
    assert captured["kwargs"]["env"]["AO_PROFILE_SESSION_ID"] == "test-profile"
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


def test_parent_broker_environment_is_forwarded(monkeypatch):
    import importlib

    autoortho_mod = importlib.import_module("autoortho")
    mount = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    mount.cfg = SimpleNamespace(
        autoortho=SimpleNamespace(
            http2_enabled=True,
            max_concurrent_downloads=32,
            http2_max_connections=8,
        )
    )
    mount._download_broker = None
    mount._download_broker_env = {}

    class FakeBroker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.stopped = False

        def start(self):
            return None

        def client_environment(self):
            return {
                "AO_HTTP2_BROKER_ADDR": "tcp://127.0.0.1:1234",
                "AO_HTTP2_BROKER_TOKEN": "token",
            }

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(autoortho_mod, "HTTP2Broker", FakeBroker)
    mount.start_download_broker()

    assert mount._download_broker.kwargs["max_concurrency"] == 32
    assert mount._download_broker_env["AO_HTTP2_BROKER_TOKEN"] == "token"
    broker = mount._download_broker
    mount.stop_download_broker()
    assert broker.stopped is True


def test_parent_retries_broker_in_process_after_spawn_failure(monkeypatch):
    import importlib

    autoortho_mod = importlib.import_module("autoortho")
    mount = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    mount.cfg = SimpleNamespace(
        autoortho=SimpleNamespace(
            http2_enabled=True,
            max_concurrent_downloads=32,
            http2_max_connections=8,
        )
    )
    mount._download_broker = None
    mount._download_broker_env = {}
    created = []

    class FakeBroker:
        def __init__(self, in_process=False, **kwargs):
            self.in_process = in_process
            created.append(self)

        def start(self):
            if not self.in_process:
                raise autoortho_mod.HTTP2BrokerStartupError(
                    "spawn handshake failed"
                )

        def client_environment(self):
            return {
                "AO_HTTP2_BROKER_ADDR": "tcp://127.0.0.1:1234",
                "AO_HTTP2_BROKER_TOKEN": "token",
            }

        def stop(self):
            return None

    monkeypatch.setattr(autoortho_mod, "HTTP2Broker", FakeBroker)
    mount.start_download_broker()

    assert [broker.in_process for broker in created] == [False, True]
    assert mount._download_broker is created[1]


def test_windows_runtime_selects_fuse_library_before_import(
    monkeypatch, tmp_path
):
    import mount_worker

    libpath = tmp_path / "winfsp-x64.dll"
    libpath.touch()
    fake_mfusepy = SimpleNamespace(
        _libfuse_path=str(libpath),
        FUSE=object(),
    )
    fake_fuse_module = SimpleNamespace(
        AutoOrtho=object(),
        fuse_option_profiles_by_os=lambda *args: {},
    )
    monkeypatch.setattr(mount_worker, "system_type", "windows")
    autoortho_module = sys.modules.get("autoortho")
    if autoortho_module is not None:
        monkeypatch.setattr(
            autoortho_module,
            "winsetup",
            SimpleNamespace(
                find_win_libs=lambda: ("WinFSP", str(libpath))
            ),
            raising=False,
        )
    monkeypatch.setitem(
        sys.modules,
        "winsetup",
        SimpleNamespace(
            find_win_libs=lambda: ("WinFSP", str(libpath))
        ),
    )
    monkeypatch.setitem(sys.modules, "mfusepy", fake_mfusepy)
    monkeypatch.setitem(sys.modules, "autoortho_fuse", fake_fuse_module)

    _fuse, _ao, _options, systemtype = (
        mount_worker._runtime_for_platform()
    )

    assert systemtype == "WinFSP"
    assert os.environ["FUSE_LIBRARY_PATH"] == str(libpath.resolve())


def test_unmount_sceneries_unmounts_before_stopping_workers():
    import importlib
    autoortho_mod = importlib.import_module("autoortho")

    aom = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    aom.cfg = SimpleNamespace(scenery_mounts=[])
    aom._active_mountpoints = ["/tmp/ao-mount"]
    aom.mounts_running = True

    calls = []

    def fake_unmount(mountpoint, force=False, wait_timeout=None):
        calls.append(("unmount", mountpoint, force, wait_timeout))
        return True

    def fake_stop_mount_workers(timeout=None):
        calls.append(("stop_workers", timeout))

    aom.unmount = fake_unmount
    aom.stop_mount_workers = fake_stop_mount_workers
    aom.stop_reporter = lambda: calls.append(("stop_reporter",))
    aom.stop_stats_manager = lambda: calls.append(("stop_stats",))
    aom.stop_log_server = lambda: calls.append(("stop_log",))

    assert autoortho_mod.AOMount.unmount_sceneries(aom) is True

    assert calls[0] == ("unmount", "/tmp/ao-mount", False, 8.0)
    assert calls[1] == ("stop_workers", 8.0)


def test_unmount_success_is_checked_after_workers_stop(monkeypatch):
    import importlib
    autoortho_mod = importlib.import_module("autoortho")

    aom = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    aom.cfg = SimpleNamespace(scenery_mounts=[])
    aom._active_mountpoints = ["/tmp/ao-mount"]
    aom.mounts_running = True
    mounted = {"value": True}

    aom.unmount = lambda *args, **kwargs: False
    aom.stop_mount_workers = lambda timeout=None: mounted.update(value=False)
    aom.stop_reporter = lambda: None
    aom.stop_stats_manager = lambda: None
    aom.stop_log_server = lambda: None
    aom._finish_performance_diagnostics = lambda: None
    monkeypatch.setattr(
        autoortho_mod,
        "safe_ismount",
        lambda path: mounted["value"],
    )

    assert autoortho_mod.AOMount.unmount_sceneries(aom) is True


def test_nonblocking_mount_reports_success(monkeypatch):
    import importlib
    autoortho_mod = importlib.import_module("autoortho")

    aom = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    aom.cfg = SimpleNamespace(
        scenery_mounts=[{"root": "/root", "mount": "/mount"}],
        fuse=SimpleNamespace(threading=True),
        xplane_custom_scenery_path="/xplane/Custom Scenery",
    )
    aom.mount_workers = []
    aom.mac_os_procs = []
    aom._active_mountpoints = []
    aom._ensure_parent_services = lambda: None
    handle = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    aom._launch_scenery_worker = lambda *args: aom.mount_workers.append(handle)

    monkeypatch.setattr(
        autoortho_mod,
        "cleanup_stale_mount_folders",
        lambda path: None,
    )
    monkeypatch.setattr(autoortho_mod, "diagnose", lambda cfg: True)
    monkeypatch.setattr(autoortho_mod.time, "sleep", lambda seconds: None)

    assert aom.mount_sceneries(blocking=False) is True


def test_nonblocking_mount_cleans_up_failed_diagnostics(monkeypatch):
    import importlib
    autoortho_mod = importlib.import_module("autoortho")

    aom = autoortho_mod.AOMount.__new__(autoortho_mod.AOMount)
    aom.cfg = SimpleNamespace(
        scenery_mounts=[{"root": "/root", "mount": "/mount"}],
        fuse=SimpleNamespace(threading=True),
        xplane_custom_scenery_path="/xplane/Custom Scenery",
    )
    aom.mount_workers = []
    aom.mac_os_procs = []
    aom._active_mountpoints = []
    aom._ensure_parent_services = lambda: None
    handle = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    aom._launch_scenery_worker = lambda *args: aom.mount_workers.append(handle)
    cleanup_calls = []
    aom.unmount_sceneries = (
        lambda force=False: cleanup_calls.append(force)
    )

    monkeypatch.setattr(
        autoortho_mod,
        "cleanup_stale_mount_folders",
        lambda path: None,
    )
    monkeypatch.setattr(autoortho_mod, "diagnose", lambda cfg: False)
    monkeypatch.setattr(autoortho_mod.time, "sleep", lambda seconds: None)

    assert aom.mount_sceneries(blocking=False) is False
    assert cleanup_calls == [True]


def test_provider_probe_failure_does_not_fail_healthy_mounts(monkeypatch):
    import importlib

    autoortho_mod = importlib.import_module("autoortho")
    cfg = SimpleNamespace(
        scenery_mounts=[{"root": "/root", "mount": "/mount"}]
    )
    monkeypatch.setattr(
        autoortho_mod.geocoder,
        "ip",
        lambda _value: SimpleNamespace(address="test"),
    )
    monkeypatch.setattr(
        autoortho_mod.os.path,
        "isdir",
        lambda path: path == "/mount/textures",
    )
    monkeypatch.setattr(autoortho_mod, "system_type", "windows")
    monkeypatch.setattr(autoortho_mod, "MAPTYPES", ["ARC"])
    monkeypatch.setattr(autoortho_mod.time, "sleep", lambda _seconds: None)

    class FailedProviderChunk:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self):
            return False

    monkeypatch.setitem(
        sys.modules,
        "getortho",
        SimpleNamespace(Chunk=FailedProviderChunk),
    )

    assert autoortho_mod.diagnose(cfg, mount_timeout=0.1) is True


def test_mount_readiness_failure_remains_fatal(monkeypatch):
    import importlib

    autoortho_mod = importlib.import_module("autoortho")
    cfg = SimpleNamespace(
        scenery_mounts=[{"root": "/root", "mount": "/mount"}]
    )
    monkeypatch.setattr(
        autoortho_mod.geocoder,
        "ip",
        lambda _value: SimpleNamespace(address="test"),
    )
    monkeypatch.setattr(autoortho_mod.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(autoortho_mod, "system_type", "windows")
    monkeypatch.setattr(autoortho_mod, "MAPTYPES", [])
    monkeypatch.setattr(autoortho_mod.time, "sleep", lambda _seconds: None)

    assert autoortho_mod.diagnose(cfg, mount_timeout=0.01) is False


def test_hung_mount_probe_respects_global_deadline(monkeypatch):
    import importlib
    import time

    autoortho_mod = importlib.import_module("autoortho")
    cfg = SimpleNamespace(
        scenery_mounts=[{"root": "/root", "mount": "/mount"}]
    )
    release = threading.Event()
    monkeypatch.setattr(
        autoortho_mod.geocoder,
        "ip",
        lambda _value: SimpleNamespace(address="test"),
    )
    monkeypatch.setattr(
        autoortho_mod.os.path,
        "isdir",
        lambda _path: release.wait(5.0),
    )
    monkeypatch.setattr(autoortho_mod, "system_type", "windows")
    monkeypatch.setattr(autoortho_mod, "MAPTYPES", [])

    started = time.monotonic()
    try:
        assert autoortho_mod.diagnose(
            cfg, mount_timeout=0.05
        ) is False
        assert time.monotonic() - started < 0.5
    finally:
        release.set()

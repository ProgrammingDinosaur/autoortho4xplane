"""Shutdown regressions. No test mounts FUSE or runs a real unmount command."""
import ctypes
import importlib
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macsetup
import mount_setup
import process_supervisor
from utils import macos_mounts, mount_utils


@pytest.fixture
def parent(monkeypatch):
    module = importlib.import_module("autoortho")
    monkeypatch.setattr(module, "system_type", "darwin")
    monkeypatch.setattr(module, "safe_ismount", lambda path: False)
    calls = []
    aom = module.AOMount.__new__(module.AOMount)
    aom.cfg = SimpleNamespace(scenery_mounts=[])
    aom._active_mountpoints = ["/test/z_ao_eur", "/test/z_ao_na"]
    aom.mount_workers = [object()]
    aom.mounts_running = True
    aom.mount_worker_supervisor = SimpleNamespace(
        prepare_shutdown=lambda: calls.append("quiesce")
    )
    for name in ("stop_download_broker", "stop_reporter", "stop_stats_manager",
                 "stop_log_server", "_finish_performance_diagnostics"):
        setattr(aom, name, lambda name=name: calls.append(name))
    aom.stop_mount_workers = lambda **kw: calls.append("stop_workers") or True
    monkeypatch.setattr(module, "cleanup_mountpoint", lambda path: calls.append("cleanup"))
    return aom, calls


def test_detach_all_mounts_before_workers_and_services_stop(parent):
    aom, calls = parent
    aom.unmount = lambda path, **kw: calls.append(path) or True
    assert aom.unmount_sceneries()
    assert calls[0] == "quiesce"
    assert set(calls[1:3]) == {"/test/z_ao_eur", "/test/z_ao_na"}
    assert calls[3] == "stop_workers"
    assert calls.index("cleanup") > calls.index("stop_workers")
    assert calls.index("stop_stats_manager") > calls.index("stop_workers")
    assert not aom._active_mountpoints


def test_failed_detach_preserves_workers_services_and_prevents_restart(parent):
    aom, calls = parent
    aom.unmount = lambda path, **kw: path.endswith("na")
    assert aom.unmount_sceneries() is False
    assert calls == ["quiesce"]
    assert len(aom.mount_workers) == 1
    assert len(aom._active_mountpoints) == 2
    assert aom.mount_sceneries(blocking=False) is False


def test_stuck_worker_prevents_teardown_even_after_detach(parent):
    aom, calls = parent
    aom.unmount = lambda path, **kw: True
    aom.stop_mount_workers = lambda **kw: False
    assert aom.unmount_sceneries() is False
    assert calls == ["quiesce"]
    assert aom._active_mountpoints


def test_single_mount_uses_single_worker_cpu_share(parent):
    aom, _ = parent
    aom.mount_workers = []
    aom._active_mountpoints = []
    aom.cfg.scenery_mounts = [{}] * 6
    aom._ensure_parent_services = lambda: None
    aom._launch_scenery_worker = lambda *args: aom._worker_cpu_share_count
    assert aom.mount_single("/root", "/mount", blocking=False) == 1


def test_macos_unmount_never_probes_poison(parent, monkeypatch):
    aom, _ = parent
    received = []
    monkeypatch.setattr(macsetup, "unmount_macfuse", lambda *a, **kw: received.append((a, kw)) or True)
    def forbidden(*args, **kwargs):
        raise AssertionError("FUSE shutdown must not stat .poison")
    monkeypatch.setattr(os, "lstat", forbidden)
    assert aom.unmount("/test/z_ao", wait_timeout=2)
    assert received == [(("/test/z_ao",), {"force": False, "timeout": 2})]


def test_timed_out_helper_does_not_wait_after_kill_or_spawn_again(monkeypatch, tmp_path):
    calls = []
    class StuckProcess:
        def poll(self):
            return None
        def wait(self, timeout=None):
            assert timeout is not None and timeout <= 0.1
            calls.append("wait")
            raise subprocess.TimeoutExpired("umount", timeout)
        def kill(self):
            calls.append("kill")
    def launch(command, **kwargs):
        calls.append(command)
        assert kwargs["stdin"] == subprocess.DEVNULL
        return StuckProcess()
    monkeypatch.setattr(macsetup, "_pending_unmounts", {})
    monkeypatch.setattr(macsetup.subprocess, "Popen", launch)
    monkeypatch.setattr(macsetup, "safe_ismount", lambda path: True)
    path = str(tmp_path / "mount with spaces")
    assert macsetup.unmount_macfuse(path, timeout=0.1) is False
    # A missing table entry alone does not mean a previously stuck helper exited.
    monkeypatch.setattr(macsetup, "safe_ismount", lambda path: False)
    assert macsetup.unmount_macfuse(path, timeout=0.1) is False
    assert calls == [["/sbin/umount", path], "wait", "kill"]


def test_os_detach_retries_busy_mount_with_force(monkeypatch, tmp_path):
    commands = []
    class Process:
        def __init__(self, result):
            self.result = result
        def poll(self):
            return self.result
        def wait(self, timeout=None):
            return self.result
    def launch(command, **kwargs):
        commands.append(command)
        return Process(1 if len(commands) == 1 else 0)
    monkeypatch.setattr(macsetup, "_pending_unmounts", {})
    monkeypatch.setattr(macsetup.subprocess, "Popen", launch)
    monkeypatch.setattr(macsetup, "safe_ismount", lambda path: len(commands) < 2)
    path = str(tmp_path / "z_ao")
    assert macsetup.unmount_macfuse(path)
    assert commands == [["/sbin/umount", path], ["/sbin/umount", "-f", path]]
    assert not macsetup._pending_unmounts


def test_worker_refuses_to_take_over_existing_macos_mount(monkeypatch):
    monkeypatch.setattr(mount_setup, "safe_ismount", lambda path: True)
    with pytest.raises(mount_setup.MountError):
        with mount_setup.setupmount("/test/z_ao", "macOS"):
            pytest.fail("Existing mount must not be reused")


def test_mounted_cleanup_performs_no_directory_operations(monkeypatch):
    monkeypatch.setattr(mount_utils, "safe_ismount", lambda path: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not access a mounted filesystem")
    monkeypatch.setattr(os.path, "lexists", forbidden)
    monkeypatch.setattr(os, "rmdir", forbidden)
    monkeypatch.setattr(os, "makedirs", forbidden)
    assert mount_utils.cleanup_mountpoint("/test/mount") is False


def test_placeholder_cleanup_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(mount_utils, "safe_ismount", lambda path: False)
    assert mount_utils.cleanup_mountpoint(tmp_path)
    assert mount_utils.cleanup_mountpoint(tmp_path)
    assert (tmp_path / ".AO_PLACEHOLDER").is_file()


def test_cleanup_never_labels_unrelated_content_as_disposable(monkeypatch, tmp_path):
    monkeypatch.setattr(mount_utils, "safe_ismount", lambda path: False)
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep")
    assert mount_utils.cleanup_mountpoint(tmp_path) is False
    assert sentinel.read_text() == "keep"
    assert not (tmp_path / ".AO_PLACEHOLDER").exists()


def test_cached_mount_detection_does_not_stat_mount_leaf(monkeypatch, tmp_path):
    path = str(tmp_path / "z_ao")
    monkeypatch.setattr(macos_mounts, "read_mount_table", lambda: [
        macos_mounts.MountEntry(path, "autoortho", "macfuse")
    ])
    monkeypatch.setattr(mount_utils.sys, "platform", "darwin")
    original = os.lstat
    def guarded(path_arg, *args, **kwargs):
        assert os.fsdecode(path_arg) != path
        return original(path_arg, *args, **kwargs)
    monkeypatch.setattr(os, "lstat", guarded)
    assert mount_utils.safe_ismount(path)
    assert macsetup.is_macfuse_mount(path)


def test_cached_mount_table_uses_nowait_and_private_buffer(monkeypatch):
    calls = []
    def getfsstat(buf, size, flags):
        calls.append((size, flags))
        if buf is not None:
            buf[0].f_mntonname = b"/mount with spaces"
            buf[0].f_mntfromname = b"fuse-t:/ao"
            buf[0].f_fstypename = b"nfs"
        return 1
    monkeypatch.setattr(macos_mounts, "_getfsstat", lambda: getfsstat)
    assert macos_mounts.read_mount_table() == [
        macos_mounts.MountEntry("/mount with spaces", "fuse-t:/ao", "nfs")
    ]
    assert calls == [(0, 2), (9 * ctypes.sizeof(macos_mounts._StatFS), 2)]


def test_mount_table_failure_blocks_cleanup(monkeypatch, tmp_path):
    def fail():
        raise OSError("mount table unavailable")
    monkeypatch.setattr(macos_mounts, "read_mount_table", fail)
    monkeypatch.setattr(mount_utils.sys, "platform", "darwin")
    assert mount_utils.safe_ismount(tmp_path)
    assert mount_utils.cleanup_mountpoint(tmp_path) is False


def test_workers_receive_cpu_budget_before_native_libraries_load(monkeypatch, tmp_path):
    captured = {}
    process = SimpleNamespace(pid=123, poll=lambda: 0, stdin=io.BytesIO())
    def launch(command, **kwargs):
        captured.update(kwargs)
        return process
    monkeypatch.setattr(process_supervisor.subprocess, "Popen", launch)
    monkeypatch.setattr(process_supervisor.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(process_supervisor.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("OMP_THREAD_LIMIT", "32")
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    supervisor = process_supervisor.AOProcessSupervisor()
    supervisor.start_mount_worker("/root", "/mount", "z_ao", False,
                                  extra_env={"AO_MOUNT_WORKER_COUNT": "6"})
    assert captured["env"]["OMP_NUM_THREADS"] == "2"
    assert captured["env"]["OMP_THREAD_LIMIT"] == "2"
    assert captured["env"]["OMP_WAIT_POLICY"] == "PASSIVE"
    assert captured["env"]["AO_WORKER_CONTROL_STDIN"] == "1"
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["env"]["AO_WORKER_CONTROL_FD"] == str(captured["pass_fds"][0])
    assert supervisor.stop_all(timeout=0)


def test_quiesce_uses_control_pipe_only_once():
    stream = io.BytesIO()
    process = SimpleNamespace(pid=123, poll=lambda: None, stdin=stream)
    handle = process_supervisor.WorkerHandle(process, "/root", "/mount", "ao")
    supervisor = process_supervisor.AOProcessSupervisor()
    supervisor.handles = [handle]
    supervisor.prepare_shutdown()
    supervisor.prepare_shutdown()
    assert stream.getvalue() == b"shutdown\n"


def test_worker_control_works_without_gui_standard_streams(monkeypatch, tmp_path):
    import mount_worker
    reader, writer = os.pipe()
    received = []
    monkeypatch.setenv("AO_WORKER_CONTROL_STDIN", "1")
    monkeypatch.setenv("AO_WORKER_CONTROL_FD", str(reader))
    monkeypatch.setattr(mount_worker.sys, "stdin", None)
    monkeypatch.setattr(mount_worker, "_worker_stopping", threading.Event())
    monkeypatch.setattr(mount_worker, "_begin_worker_shutdown", received.append)
    thread = mount_worker._start_control_thread(str(tmp_path))
    try:
        os.write(writer, b"shutdown\n")
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert received == ["parent requested shutdown"]
        assert mount_worker._worker_stopping.is_set()
    finally:
        os.close(writer)


def test_stop_all_retains_worker_which_survives_kill(monkeypatch):
    class StuckProcess:
        pid = 123
        def poll(self):
            return None
        def wait(self, timeout=None):
            assert timeout is not None
            raise subprocess.TimeoutExpired("worker", timeout)
    handle = process_supervisor.WorkerHandle(StuckProcess(), "/root", "/mount", "ao")
    supervisor = process_supervisor.AOProcessSupervisor()
    supervisor.handles = [handle]
    monkeypatch.setattr(supervisor, "_request_worker_stop", lambda handle: None)
    monkeypatch.setattr(supervisor, "kill_worker_tree", lambda handle: None)
    assert supervisor.stop_all(timeout=0) is False
    assert supervisor.handles == [handle]

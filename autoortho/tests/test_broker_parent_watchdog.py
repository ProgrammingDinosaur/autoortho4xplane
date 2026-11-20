"""The HTTP/2 broker process must exit on its own when its parent dies.

Daemon ``multiprocessing`` children are only reaped by the parent's normal
exit path, so a parent that crashes, is killed, or leaves through
``os._exit`` used to leave broker processes running indefinitely.
"""

import multiprocessing
import os
import subprocess
import sys
import textwrap
import time

import pytest

AUTOORTHO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AUTOORTHO_DIR)

pytest.importorskip("httpx", reason="httpx is required for http2_broker")
pytest.importorskip("zmq", reason="pyzmq is required for http2_broker")
pytest.importorskip("msgpack", reason="msgpack is required for http2_broker")
psutil = pytest.importorskip("psutil")

import http2_broker as hb  # noqa: E402  (import after importorskip guards)


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# _ParentWatchdog unit tests
# ---------------------------------------------------------------------------

def test_watchdog_stays_quiet_while_parent_is_alive():
    calls = []
    reader, writer = multiprocessing.Pipe(duplex=False)
    try:
        watchdog = hb._ParentWatchdog(
            os.getpid(),
            lambda: calls.append(1),
            interval=0.05,
            hard_exit_after=None,
            sentinel=reader,
        ).start()
        time.sleep(1.0)
        watchdog.stop()
        assert not watchdog.triggered
        assert calls == []
    finally:
        reader.close()
        writer.close()


def test_watchdog_fires_when_parent_pid_exits():
    parent = _sleeper()
    calls = []
    try:
        watchdog = hb._ParentWatchdog(
            parent.pid,
            lambda: calls.append(1),
            interval=0.05,
            hard_exit_after=None,
            sentinel=None,
        ).start()
        time.sleep(0.3)
        assert not watchdog.triggered

        parent.kill()
        parent.wait(timeout=10)

        assert _wait_until(lambda: watchdog.triggered, 5.0)
        assert calls == [1]
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


def test_watchdog_fires_when_parent_sentinel_closes():
    reader, writer = multiprocessing.Pipe(duplex=False)
    calls = []
    try:
        watchdog = hb._ParentWatchdog(
            None,
            lambda: calls.append(1),
            interval=0.05,
            hard_exit_after=None,
            sentinel=reader,
        ).start()
        time.sleep(0.3)
        assert not watchdog.triggered

        writer.close()  # what the OS does to the parent's end when it dies

        assert _wait_until(lambda: watchdog.triggered, 5.0)
        assert calls == [1]
    finally:
        reader.close()


def test_watchdog_stop_prevents_firing():
    reader, writer = multiprocessing.Pipe(duplex=False)
    calls = []
    try:
        watchdog = hb._ParentWatchdog(
            None,
            lambda: calls.append(1),
            interval=0.05,
            hard_exit_after=None,
            sentinel=reader,
        ).start()
        watchdog.stop()
        time.sleep(0.2)
        writer.close()
        time.sleep(0.3)
        assert not watchdog.triggered
        assert calls == []
    finally:
        reader.close()


def test_watchdog_without_parent_info_is_disabled():
    watchdog = hb._ParentWatchdog(None, lambda: None, sentinel=None)
    assert not watchdog.enabled
    watchdog.start()
    watchdog.stop()
    assert not watchdog.triggered


# ---------------------------------------------------------------------------
# End to end: a real broker process under a parent that gets killed
# ---------------------------------------------------------------------------

PARENT_SCRIPT = textwrap.dedent(
    """
    import sys
    import time

    sys.path.insert(0, {autoortho_dir!r})

    import http2_broker as hb

    if __name__ == "__main__":
        count = int(sys.argv[1])
        if count == 1:
            broker = hb.HTTP2Broker(handshake_timeout=30.0)
            broker.start()
            pids = [broker.pid]
        else:
            broker = hb.BrokerGroup(
                [hb.HTTP2Broker(handshake_timeout=30.0) for _ in range(count)]
            )
            broker.start()
            pids = broker.pids
        print(" ".join(str(pid) for pid in pids), flush=True)
        # Stay up until the test kills us; never reach a normal exit.
        time.sleep(300)
    """
)


@pytest.mark.parametrize("broker_count", [1, 2], ids=["single", "group"])
def test_broker_process_exits_after_parent_is_killed(tmp_path, broker_count):
    pytest.importorskip("h2", reason="process-mode broker requires h2")

    script = tmp_path / "broker_parent.py"
    script.write_text(PARENT_SCRIPT.format(autoortho_dir=AUTOORTHO_DIR))

    parent = subprocess.Popen(
        [sys.executable, str(script), str(broker_count)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(tmp_path),
    )
    broker_procs = []
    try:
        line = parent.stdout.readline().strip()
        assert line, "parent did not report broker pids"
        broker_procs = [psutil.Process(int(pid)) for pid in line.split()]
        assert len(broker_procs) == broker_count
        assert all(proc.is_running() for proc in broker_procs)

        # Normal operation: brokers must not shut themselves down while
        # their parent is still alive.
        time.sleep(2.0)
        assert all(proc.is_running() for proc in broker_procs)

        parent.kill()  # abnormal exit: no atexit, no daemon-child reaping
        parent.wait(timeout=10)

        _gone, alive = psutil.wait_procs(broker_procs, timeout=8.0)
        assert not alive, "broker processes kept running after their parent was killed"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if parent.stdout is not None:
            parent.stdout.close()
        for proc in broker_procs:
            try:
                if proc.is_running():
                    proc.kill()
            except psutil.Error:
                pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only shortcut")
def test_watchdog_does_not_ask_windows_for_the_parent_status(monkeypatch):
    # status() lists every process on the system on Windows; there are no
    # zombies there, so a running parent is alive without asking.
    watchdog = hb._ParentWatchdog(
        os.getpid(), lambda: None, interval=0.05, hard_exit_after=None
    )

    def no_status(self):
        raise AssertionError("status() was called")

    monkeypatch.setattr(psutil.Process, "status", no_status)
    assert watchdog._pid_alive()

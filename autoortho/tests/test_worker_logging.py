"""Worker logging hands records to a queue so FUSE threads never send them."""
import logging
import logging.handlers
import os
import pickle
import socket
import struct
import sys

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

import mount_worker


def _read_records(conn, want, limit=50):
    """Yield messages from SocketHandler frames until ``want`` arrives."""
    buf = b""
    seen = []
    while len(seen) < limit:
        while len(buf) < 4:
            chunk = conn.recv(65536)
            if not chunk:
                return seen
            buf += chunk
        size = struct.unpack(">L", buf[:4])[0]
        while len(buf) < 4 + size:
            chunk = conn.recv(65536)
            if not chunk:
                return seen
            buf += chunk
        record = pickle.loads(buf[4:4 + size])
        buf = buf[4 + size:]
        seen.append(record)
        if record.get("msg") == want:
            return seen
    return seen


def test_worker_records_reach_parent_through_a_queue(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)
    monkeypatch.setenv("AO_LOG_ADDR", f"127.0.0.1:{server.getsockname()[1]}")

    root = logging.getLogger()
    saved = (root.handlers[:], root.level, root.filters[:])
    try:
        mount_worker.configure_worker_logging("z_test", "DEBUG")

        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.handlers.QueueHandler)
        assert mount_worker._log_listener is not None

        logging.getLogger("aotest").debug("hello %s", "there")

        conn, _ = server.accept()
        conn.settimeout(10)
        with conn:
            records = _read_records(conn, "hello there")
        match = [r for r in records if r.get("msg") == "hello there"]
        assert match, [r.get("msg") for r in records]
        # The mount name still travels with each record.
        assert match[0]["mount"] == "z_test"
        assert match[0]["levelno"] == logging.DEBUG
    finally:
        if mount_worker._log_listener is not None:
            mount_worker._log_listener.stop()
            mount_worker._log_listener = None
        root.handlers[:], root.level, root.filters[:] = saved
        server.close()


def _configure_without_parent(monkeypatch, tmp_path):
    import pathlib

    monkeypatch.delenv("AO_LOG_ADDR", raising=False)
    # The worker writes its own log under ~/.autoortho-data when it has no
    # parent to send records to; keep that out of the real home directory.
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    mount_worker.configure_worker_logging("z_test", "DEBUG")


def test_mfusepy_per_call_debug_is_off_unless_tracing(monkeypatch, tmp_path):
    # mfusepy logs every errno it returns at DEBUG; X-Plane's library probes
    # made that most of the log during a scenery load.
    root = logging.getLogger()
    fuse_log = logging.getLogger("fuse")
    saved = (root.handlers[:], root.level, root.filters[:], fuse_log.level)
    try:
        monkeypatch.delenv("AO_TRACE_READS", raising=False)
        _configure_without_parent(monkeypatch, tmp_path)
        assert not fuse_log.isEnabledFor(logging.DEBUG)
        assert fuse_log.isEnabledFor(logging.ERROR)

        fuse_log.setLevel(logging.NOTSET)
        monkeypatch.setenv("AO_TRACE_READS", "1")
        _configure_without_parent(monkeypatch, tmp_path)
        assert fuse_log.isEnabledFor(logging.DEBUG)
    finally:
        for handler in root.handlers:
            if handler not in saved[0]:
                handler.close()
        root.handlers[:], root.level, root.filters[:] = saved[:3]
        fuse_log.setLevel(saved[3])

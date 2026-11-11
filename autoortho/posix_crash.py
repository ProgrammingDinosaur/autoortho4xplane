"""Local macOS/Linux worker fault logs and parent-side crash evidence.

Fatal signals use CPython's C faulthandler, not Python signal callbacks. The
surviving parent records the exit and collects existing OS crash diagnostics.
No debugger attachment, privilege escalation or core-dump setting changes.
"""

import faulthandler
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

if __package__:
    from .native_build import file_sha256
else:
    from native_build import file_sha256


log = logging.getLogger(__name__)
PREFIX_ENV = "AO_CRASH_PREFIX"
MAX_REPORTS = 3
REPORT_SUFFIXES = (
    ".fault.log", ".worker.json", ".images.json", ".crash.json", ".system.json",
    ".system.txt", ".macos.ips", ".macos.crash", ".complete",
)
FATAL_SIGNALS = frozenset(
    int(value) for name in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL", "SIGFPE", "SIGTRAP", "SIGSYS")
    if (value := getattr(signal, name, None)) is not None
)


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _native_fingerprints():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if (base / "autoortho").is_dir():
        base = base / "autoortho"
    platform_name = "macos" if sys.platform == "darwin" else "linux"
    extension = "dylib" if sys.platform == "darwin" else "so"
    paths = (
        base / "aopipeline" / "lib" / platform_name / ("libaopipeline." + extension),
        base / "aoimage" / ("aoimage." + extension),
        base / "lib" / platform_name / ("libispc_texcomp." + extension),
    )
    result = []
    for path in paths:
        try:
            if path.is_file():
                result.append({"path": str(path), "sha256": file_sha256(path)})
        except OSError as exc:
            result.append({"path": str(path), "error": str(exc)})
    return result


def _core_settings():
    import resource
    result = {"rlimit_core": list(resource.getrlimit(resource.RLIMIT_CORE))}
    if sys.platform.startswith("linux"):
        try:
            result["core_pattern"] = Path("/proc/sys/kernel/core_pattern").read_text()[:4096].strip()
        except OSError as exc:
            result["core_pattern_error"] = str(exc)
    return result


def enable_worker_fault_logging(prefix=None):
    """Called before application/native imports; descriptor lives until exit."""
    if sys.platform == "win32":
        return False
    prefix = prefix or os.environ.get(PREFIX_ENV)
    if not prefix:
        return False
    prefix = Path(prefix)
    descriptor = None
    try:
        descriptor = os.open(str(prefix) + ".fault.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, f"AutoOrtho worker fault log\npid={os.getpid()}\n".encode())
        if sys.version_info >= (3, 14):
            faulthandler.enable(file=descriptor, all_threads=True, c_stack=True)
        else:
            faulthandler.enable(file=descriptor, all_threads=True)
        # Keep the raw descriptor open: faulthandler retains its number, not a
        # Python file object. The OS closes it when this worker exits.
        _write_json(str(prefix) + ".worker.json", {
            "pid": os.getpid(), "platform": sys.platform, "python": sys.version,
            "executable": sys.executable, "native_files": _native_fingerprints(),
            "fault_handler": {"all_threads": True, "c_stack_requested": sys.version_info >= (3, 14)},
            "core_settings": _core_settings(),
        })
        capture_worker_images(prefix)
        return True
    except Exception as exc:
        # Leave an enabled handler's descriptor open even if metadata failed.
        if descriptor is not None and not faulthandler.is_enabled():
            os.close(descriptor)
        print(f"Could not fully initialize worker fault logging: {exc}", file=sys.stderr)
        return False


def capture_worker_images(prefix=None):
    """Refresh after runtime imports, before mounting FUSE. Never raises."""
    prefix = prefix or os.environ.get(PREFIX_ENV)
    if not prefix or sys.platform == "win32":
        return
    try:
        images = []
        if sys.platform.startswith("linux"):
            for line in Path("/proc/self/maps").read_text().splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and "x" in fields[1] and fields[5].startswith("/"):
                    images.append({"range": fields[0], "offset": fields[2], "path": fields[5]})
        elif sys.platform == "darwin":
            import ctypes
            dyld = ctypes.CDLL(None)
            dyld._dyld_image_count.restype = ctypes.c_uint32
            dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
            dyld._dyld_get_image_name.restype = ctypes.c_char_p
            dyld._dyld_get_image_header.argtypes = [ctypes.c_uint32]
            dyld._dyld_get_image_header.restype = ctypes.c_void_p
            for index in range(dyld._dyld_image_count()):
                name = dyld._dyld_get_image_name(index)
                if name:
                    images.append({"path": os.fsdecode(name), "base": dyld._dyld_get_image_header(index)})
        _write_json(str(prefix) + ".images.json", {"pid": os.getpid(), "captured_at": time.time(), "images": images})
    except Exception as exc:
        print(f"Could not snapshot worker native images: {exc}", file=sys.stderr)


def prune_reports(directory):
    completed = []
    for marker in Path(directory).glob("posix-*.complete"):
        try:
            if not marker.is_symlink():
                completed.append((marker.stat().st_mtime, marker))
        except OSError:
            continue  # Another process may already have pruned this bundle.
    for _, marker in sorted(completed, reverse=True)[MAX_REPORTS:]:
        prefix = marker.name.removesuffix(".complete")
        for suffix in REPORT_SUFFIXES:
            path = marker.parent / (prefix + suffix)
            if not path.is_symlink():
                path.unlink(missing_ok=True)


def _report_matches_pid(data, suffix, pid):
    if suffix == ".crash":
        return re.search(r"^Process:\s+[^\n]*\[" + str(pid) + r"\]\s*$", data, re.MULTILINE) is not None
    # Modern .ips reports contain a metadata JSON object followed by a body.
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(data):
        offset += len(data[offset:]) - len(data[offset:].lstrip())
        if offset == len(data):
            break
        try:
            value, offset = decoder.raw_decode(data, offset)
        except ValueError:
            return False
        if isinstance(value, dict) and value.get("pid") == pid:
            return True
    return False


def _find_macos_report(pid, started, directories):
    for directory in directories:
        try:
            candidates = []
            for path in directory.iterdir():
                try:
                    if path.suffix in (".ips", ".crash") and not path.is_symlink() and path.is_file():
                        candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            continue
        for _, path in sorted(candidates, reverse=True)[:100]:
            try:
                info = path.stat()
                if info.st_mtime < started or info.st_size > 16 * 1024 * 1024:
                    continue
                data = path.read_text(encoding="utf-8", errors="replace")
                if _report_matches_pid(data, path.suffix, pid):
                    return path.suffix, data
            except OSError:
                continue
    return None


class PosixCrashCollector:
    """Retains evidence only for failed workers; OS collection is asynchronous."""

    def __init__(self, directory, *, collect_system=True):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        prune_reports(self.directory)
        self.prefix = self.directory / ("posix-" + uuid.uuid4().hex)
        self.started = time.time()
        self.pid = None
        self.failed = False
        self.closed = False
        self.collect_system = collect_system
        self.thread = None
        self.exited = None

    def prepare_environment(self, env):
        env[PREFIX_ENV] = str(self.prefix)

    def start(self, worker_pid, stderr):
        self.pid = worker_pid

    def record_exit(self, returncode, expected=False):
        if returncode is None:
            return
        number = -returncode if returncode < 0 else None
        native_fault = number in FATAL_SIGNALS
        self.failed = native_fault or (returncode != 0 and not expected)
        self.exited = time.time()
        if not self.failed:
            return
        try:
            name = signal.Signals(number).name if number is not None else None
        except ValueError:
            name = "UNKNOWN"
        _write_json(str(self.prefix) + ".crash.json", {
            "pid": self.pid, "platform": sys.platform, "returncode": returncode,
            "signal": name, "signal_number": number, "native_fault": native_fault,
            "shutdown_requested": expected, "started_at": self.started, "exited_at": self.exited,
        })
        # no-mse: rule=sdk-compliance reason="Local crash capture diagnostics"
        log.error("Worker crash evidence saved: %s (exit=%s signal=%s)", self.prefix, returncode, name)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.failed:
            for suffix in REPORT_SUFFIXES:
                Path(str(self.prefix) + suffix).unlink(missing_ok=True)
            return
        if self.collect_system:
            self.thread = threading.Thread(target=self._collect_system_report, name="AO-CrashEvidence", daemon=True)
            self.thread.start()
        else:
            Path(str(self.prefix) + ".complete").touch()

    def _collect_system_report(self):
        status = {"status": "unavailable"}
        try:
            if sys.platform == "darwin":
                directories = (Path.home() / "Library/Logs/DiagnosticReports", Path("/Library/Logs/DiagnosticReports"))
                deadline = time.monotonic() + 8
                while True:
                    report = _find_macos_report(self.pid, self.started, directories)
                    if report:
                        suffix, data = report
                        descriptor = os.open(str(self.prefix) + ".macos" + suffix, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                            output.write(data)
                        status = {"status": "collected", "source": "macOS DiagnosticReports"}
                        break
                    if time.monotonic() >= deadline:
                        status["reason"] = "No matching readable macOS crash report appeared within 8 seconds"
                        break
                    time.sleep(0.25)
            elif sys.platform.startswith("linux"):
                tool = next((p for p in ("/usr/bin/coredumpctl", "/bin/coredumpctl") if Path(p).is_file()), None)
                if tool:
                    deadline = time.monotonic() + 8
                    while True:
                        descriptor = os.open(str(self.prefix) + ".system.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(descriptor, "wb") as output:
                            result = subprocess.run(
                                [tool, "--no-pager", "--since=@" + str(int(self.started)),
                                 "--until=@" + str(int(self.exited or time.time()) + 10), "info",
                                 "COREDUMP_PID=" + str(self.pid), "COREDUMP_UID=" + str(os.getuid()),
                                 "COREDUMP_EXE=" + os.path.realpath(sys.executable)],
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                timeout=min(3, max(0.1, deadline - time.monotonic())),
                            )
                        status = {"status": "collected" if result.returncode == 0 else "unavailable",
                                  "source": "coredumpctl info", "returncode": result.returncode}
                        if result.returncode == 0 or time.monotonic() >= deadline:
                            break
                        time.sleep(0.25)
                else:
                    status["reason"] = "coredumpctl is not installed; see fault log and recorded core settings"
        except Exception as exc:
            status["reason"] = str(exc)
        finally:
            try:
                _write_json(str(self.prefix) + ".system.json", status)
                Path(str(self.prefix) + ".complete").touch()
            except OSError as exc:
                log.warning("Could not finalize OS crash evidence: %s", exc)

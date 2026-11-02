import ctypes
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional

try:
    from autoortho.utils.mount_utils import _is_frozen
except ImportError:
    from utils.mount_utils import _is_frozen

log = logging.getLogger(__name__)


DEFAULT_WORKER_STOP_TIMEOUT = 8.0
_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class WorkerHandle:
    process: subprocess.Popen
    root: str
    mountpoint: str
    volname: str
    stdout_file: Optional[BinaryIO] = None
    job_handle: Optional[int] = None
    shutdown_requested: bool = False
    control_stream: Optional[BinaryIO] = None

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self):
        return self.process.poll()


class AOProcessSupervisor:
    """Owns AO-launched mount workers and their process-tree shutdown."""

    def __init__(self):
        self.handles: List[WorkerHandle] = []

    def start_mount_worker(
        self,
        root,
        mountpoint,
        volname,
        nothreads,
        stats_addr=None,
        stats_auth=None,
        log_addr=None,
        loglevel="INFO",
        extra_env=None,
    ) -> WorkerHandle:
        env = os.environ.copy()
        env["AO_RUN_MODE"] = "mount_worker"
        env["AO_WORKER_CONTROL_STDIN"] = "1"

        if stats_addr:
            env["AO_STATS_ADDR"] = stats_addr
            if isinstance(stats_auth, bytes):
                env["AO_STATS_AUTH"] = stats_auth.decode("utf-8")
            elif stats_auth:
                env["AO_STATS_AUTH"] = str(stats_auth)

        if log_addr:
            env["AO_LOG_ADDR"] = log_addr

        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})

        try:
            workers = max(1, int(env.get("AO_MOUNT_WORKER_COUNT", "1")))
        except ValueError:
            workers = 1
        cpu_budget = max(1, (os.cpu_count() or 1) // workers)
        # Set BEFORE the child imports any OpenMP-backed native libraries.
        # Partial builds and warmup use the runtime defaults, not max_threads.
        env.setdefault("OMP_NUM_THREADS", str(cpu_budget))
        try:
            thread_limit = max(1, int(env.get("OMP_THREAD_LIMIT", cpu_budget)))
        except ValueError:
            thread_limit = cpu_budget
        env["OMP_THREAD_LIMIT"] = str(min(cpu_budget, thread_limit))
        env.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        env.setdefault("KMP_BLOCKTIME", "0")

        if _is_frozen():
            cmd = [sys.executable]
            cwd = None
        else:
            cmd = [sys.executable, "-m", "autoortho"]
            cwd = str(_REPO_ROOT)

        cmd += [
            "--root",
            os.path.expanduser(root),
            "--mountpoint",
            os.path.expanduser(mountpoint),
            "--loglevel",
            str(loglevel).upper(),
        ]
        if volname:
            cmd += ["--volname", str(volname)]
        if nothreads:
            cmd.append("--nothreads")

        log_dir = Path.home() / ".autoortho-data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_worker_log_name(volname or Path(mountpoint).name or "mount")
        std_file = open(log_dir / f"worker-{safe_name}.log", "ab", buffering=0)

        popen_kwargs = {
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": std_file,
            "stderr": std_file,
        }
        control_read = None
        control_stream = None
        if cwd:
            popen_kwargs["cwd"] = cwd
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        else:
            popen_kwargs["start_new_session"] = True
            # A private fd survives GUI/windowed bootloaders which replace or
            # disable sys.stdin. Nothing in the mounted filesystem is touched.
            control_read, control_write = os.pipe()
            control_stream = os.fdopen(control_write, "wb", buffering=0)
            env["AO_WORKER_CONTROL_FD"] = str(control_read)
            popen_kwargs["pass_fds"] = (control_read,)
            popen_kwargs["stdin"] = subprocess.DEVNULL

        log.debug(
            "Launching mount worker: frozen=%s exe=%s cwd=%s cmd=%s",
            _is_frozen(),
            sys.executable,
            cwd,
            cmd,
        )
        try:
            process = subprocess.Popen(cmd, **popen_kwargs)
        except Exception:
            _close_quietly(std_file)
            _close_quietly(control_stream)
            raise
        finally:
            if control_read is not None:
                os.close(control_read)

        handle = WorkerHandle(
            process=process,
            root=os.path.expanduser(root),
            mountpoint=os.path.expanduser(mountpoint),
            volname=str(volname or ""),
            stdout_file=std_file,
            control_stream=control_stream,
        )

        if os.name == "nt":
            handle.job_handle = self._attach_windows_job(process)

        self.handles.append(handle)
        log.info("Mount worker for %s started with pid: %s", volname, process.pid)
        return handle

    def prepare_shutdown(self):
        """Cancel expensive work out of band while keeping FUSE responsive."""
        for handle in list(self.handles):
            if handle.poll() is not None or handle.shutdown_requested:
                continue
            stream = handle.control_stream or getattr(handle.process, "stdin", None)
            if stream is None:
                continue
            try:
                stream.write(b"shutdown\n")
                stream.flush()
                handle.shutdown_requested = True
            except (OSError, ValueError) as exc:
                log.warning("Cannot quiesce worker pid %s: %s", handle.pid, exc)

    def stop_worker(self, handle: WorkerHandle, timeout=DEFAULT_WORKER_STOP_TIMEOUT):
        if handle.process.poll() is not None:
            self._cleanup_handle(handle)
            return True

        self._request_worker_stop(handle)
        try:
            handle.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning(
                "Mount worker pid %s did not exit within %.1fs; force killing tree",
                handle.pid,
                timeout,
            )
            self.kill_worker_tree(handle)
            try:
                handle.process.wait(timeout=timeout)
            except Exception:
                pass
        finally:
            if handle.poll() is not None:
                self._cleanup_handle(handle)
        return handle.poll() is not None

    def kill_worker_tree(self, handle: WorkerHandle):
        if handle.process.poll() is not None:
            return

        if os.name == "nt":
            self._kill_windows_tree(handle)
            return

        try:
            pgid = os.getpgid(handle.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception as exc:
            log.debug("killpg failed for worker pid %s: %s", handle.pid, exc)
            try:
                handle.process.kill()
            except Exception:
                pass

    def stop_all(self, timeout=DEFAULT_WORKER_STOP_TIMEOUT):
        live = [h for h in self.handles if h.process.poll() is None]

        # FUSE has already been unmounted by the caller. Give workers a short
        # window to persist diagnostics and exit naturally before signaling.
        natural_exit_deadline = time.monotonic() + min(1.0, max(0.0, timeout))
        while live and time.monotonic() < natural_exit_deadline:
            live = [h for h in live if h.process.poll() is None]
            if live:
                time.sleep(0.05)

        for handle in live:
            self._request_worker_stop(handle)

        deadline = time.monotonic() + timeout
        for handle in live:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                handle.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass

        for handle in live:
            if handle.process.poll() is None:
                log.warning(
                    "Mount worker pid %s survived graceful stop; force killing tree",
                    handle.pid,
                )
                self.kill_worker_tree(handle)

        kill_deadline = time.monotonic() + max(0.0, timeout)
        for handle in list(self.handles):
            if handle.process.poll() is None:
                try:
                    handle.process.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
                except Exception:
                    pass
            if handle.poll() is not None:
                self._cleanup_handle(handle)
            else:
                log.error("Worker pid %s is still alive after kill; retaining its handle", handle.pid)
        return not self.handles

    def _request_worker_stop(self, handle: WorkerHandle):
        if handle.process.poll() is not None:
            return

        if os.name == "nt":
            try:
                handle.process.terminate()
            except Exception as exc:
                log.debug("terminate failed for worker pid %s: %s", handle.pid, exc)
            return

        try:
            pgid = os.getpgid(handle.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log.debug("SIGTERM process group failed for worker pid %s: %s", handle.pid, exc)
            try:
                handle.process.terminate()
            except Exception:
                pass

    def _cleanup_handle(self, handle: WorkerHandle):
        if handle in self.handles:
            try:
                self.handles.remove(handle)
            except ValueError:
                pass

        _close_quietly(handle.stdout_file)
        _close_quietly(getattr(handle.process, "stdin", None))
        _close_quietly(handle.control_stream)
        handle.control_stream = None
        handle.stdout_file = None

        if os.name == "nt" and handle.job_handle:
            self._close_windows_handle(handle.job_handle)
            handle.job_handle = None

    def _attach_windows_job(self, process):
        if os.name != "nt":
            return None

        try:
            import ctypes.wintypes as wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JobObjectExtendedLimitInformation = 9
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                err = ctypes.WinError(ctypes.get_last_error())
                self._close_windows_handle(job)
                raise err

            ok = kernel32.AssignProcessToJobObject(job, process._handle)
            if not ok:
                err = ctypes.WinError(ctypes.get_last_error())
                self._close_windows_handle(job)
                raise err

            return job
        except Exception as exc:
            log.warning(
                "Could not attach worker pid %s to a Windows Job Object; "
                "taskkill fallback will be used if needed: %s",
                getattr(process, "pid", "<unknown>"),
                exc,
            )
            return None

    def _kill_windows_tree(self, handle: WorkerHandle):
        if handle.job_handle:
            job = handle.job_handle
            handle.job_handle = None
            self._close_windows_handle(job)

        if handle.process.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(handle.pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                log.debug("taskkill failed for worker pid %s: %s", handle.pid, exc)

        if handle.process.poll() is None:
            try:
                handle.process.kill()
            except Exception:
                pass

    def _close_windows_handle(self, handle):
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _safe_worker_log_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("._") or "mount"


def _close_quietly(fp):
    if not fp:
        return
    try:
        fp.close()
    except Exception:
        pass

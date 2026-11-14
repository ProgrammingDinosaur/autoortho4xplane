"""UDP gate between the X-Plane plugin and files mode (plan section 5.8).

Text datagrams on 127.0.0.1, one message each, all starting with ``AO1``:

plugin -> AutoOrtho::

    AO1 HELLO <xp_version> <pid>
    AO1 LOAD <lat> <lon>            a flight is starting there
    AO1 HOLD <lat> <lon> <seq>      X-Plane is held; may it go on?
    AO1 FRAME <ms>                  a slow frame (stutter log)
    AO1 POLL <lat> <lon>            in flight, every second: anything to do?
    AO1 BYE

AutoOrtho -> plugin::

    AO1 ACK
    AO1 WAIT <built> <total> <seq>
    AO1 GO <seq> <reason>
    AO1 PAUSE / AO1 RESUME          answers to POLL: the aircraft was moved far
                                    in flight; X-Plane stays paused until the
                                    new area is built (the plugin only resumes
                                    a pause it started itself)

The gate answers HOLD from a status the session keeps current, never by
waiting on the planner. It answers GO when the start area is built, when
nothing has progressed for ``STALL_S`` (no internet, provider refusing),
when the session's loop has been silent for ``LOOP_SILENT_S``, or when the
user stops AutoOrtho.
"""

import logging
import socket
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_PORT = 49317
MAX_DATAGRAM = 512
STALL_S = 60.0
LOOP_SILENT_S = 60.0


@dataclass
class StartStatus:
    ready: bool = False
    built: int = 0
    total: int = 0
    reason: str = ""
    updated: float = 0.0     # time.monotonic() of the session loop's last update


def parse(message):
    """``(verb, [args])`` of a datagram, or None when malformed."""
    try:
        text = message.decode("ascii", errors="strict").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    if len(text) > MAX_DATAGRAM:
        return None
    parts = text.split()
    if len(parts) < 2 or parts[0] != "AO1":
        return None
    verb = parts[1].upper()
    args = parts[2:]
    try:
        if verb == "HELLO":
            return verb, [args[0] if args else "", int(args[1]) if len(args) > 1 else 0]
        if verb == "LOAD":
            return verb, [_lat(args[0]), _lon(args[1])]
        if verb == "HOLD":
            return verb, [_lat(args[0]), _lon(args[1]), int(args[2]) if len(args) > 2 else 0]
        if verb == "FRAME":
            return verb, [float(args[0])]
        if verb == "POLL":
            return verb, [_lat(args[0]), _lon(args[1])]
        if verb == "POLL":
            self.last_poll = time.time()
            if self.last_hello is None:
                self.last_hello = self.last_poll  # AutoOrtho restarted mid-flight
            if self._released.is_set() or self.pause_fn is None:
                return "AO1 RESUME"
            try:
                return "AO1 PAUSE" if self.pause_fn() else "AO1 RESUME"
            except Exception:
                return "AO1 RESUME"
        if verb == "BYE":
            return verb, []
    except (IndexError, ValueError):
        return None
    return None


def _lat(text):
    value = float(text)
    if not -90.0 <= value <= 90.0:
        raise ValueError("latitude")
    return value


def _lon(text):
    value = float(text)
    if not -180.0 <= value <= 180.0:
        raise ValueError("longitude")
    return value


class Gate:
    def __init__(self, status_fn, on_load=None, on_frame=None, on_position=None,
                 pause_fn=None, port=DEFAULT_PORT, host="127.0.0.1", clock=time.monotonic):
        self.status_fn = status_fn
        self.on_load = on_load
        self.on_frame = on_frame
        # X-Plane's main thread is held, so it sends no UDP datarefs: the
        # position in each HOLD keeps the flight feed alive meanwhile.
        self.on_position = on_position
        # pause_fn() -> True while X-Plane should stay paused (relocation).
        self.pause_fn = pause_fn
        self.last_poll = None
        self.host = host
        self.port = int(port)
        self.clock = clock
        self.sock = None
        self._thread = None
        self._stop = threading.Event()
        self._released = threading.Event()
        self.last_hello = None
        self.plugin_version = ""
        self.holding_since = None
        self._progress = (0, clock())  # (built, when it last increased)
        self.counters = {"files_start_s": 0.0, "files_holds": 0}
        self.last_go_reason = ""

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
        except OSError:
            sock.bind((self.host, 0))  # port taken: any port (written for the plugin)
        sock.settimeout(0.25)
        self.sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="AO-FilesGate", daemon=True)
        self._thread.start()
        log.info("Files mode gate listening on %s:%d", self.host, self.port)
        return self.port

    def stop(self):
        self.release("AutoOrtho stopped")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    def release(self, reason="released"):
        """Answer GO to every hold from now on (user Stop, shutdown)."""
        self.last_go_reason = reason
        self._released.set()

    def rearm(self):
        self._released.clear()

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(MAX_DATAGRAM + 1)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue  # Windows: ICMP port unreachable from an earlier reply
            except OSError:
                if self._stop.is_set():
                    return
                continue
            reply = self.handle(data)
            if reply:
                try:
                    self.sock.sendto(reply.encode("ascii"), addr)
                except OSError:
                    pass

    def handle(self, data):
        parsed = parse(data)
        if parsed is None:
            return None
        verb, args = parsed
        if verb == "HELLO":
            self.last_hello = time.time()
            self.plugin_version = str(args[0])
            return "AO1 ACK"
        if verb == "LOAD":
            self.rearm_if_idle()
            if self.on_load is not None:
                try:
                    self.on_load(args[0], args[1])
                except Exception as exc:
                    log.warning("Start at %s %s could not be planned: %s", args[0], args[1], exc)
            self._progress = (0, self.clock())
            return "AO1 ACK"
        if verb == "HOLD":
            if self.on_position is not None:
                try:
                    self.on_position(args[0], args[1])
                except Exception:
                    pass
            return self._answer_hold(args[2])
        if verb == "FRAME":
            if self.on_frame is not None:
                try:
                    self.on_frame(args[0])
                except Exception:
                    pass
            return None
        if verb == "POLL":
            self.last_poll = time.time()
            if self.last_hello is None:
                self.last_hello = self.last_poll  # AutoOrtho restarted mid-flight
            if self._released.is_set() or self.pause_fn is None:
                return "AO1 RESUME"
            try:
                return "AO1 PAUSE" if self.pause_fn() else "AO1 RESUME"
            except Exception:
                return "AO1 RESUME"
        if verb == "BYE":
            self.last_hello = None
            return "AO1 ACK"
        return None

    def rearm_if_idle(self):
        if self.holding_since is None:
            self._released.clear()

    def _answer_hold(self, seq):
        now = self.clock()
        if self.holding_since is None:
            self.holding_since = now
            self.counters["files_holds"] += 1
            log.info("X-Plane is held at a flight start until the area is built")
        if self._released.is_set():
            return self._go(seq, self.last_go_reason or "released")
        try:
            status = self.status_fn()
        except Exception as exc:
            log.warning("Start status unavailable (%s); letting X-Plane go on", exc)
            return self._go(seq, "status unavailable")
        if status.updated and now - status.updated > LOOP_SILENT_S:
            return self._go(seq, "files mode loop silent")
        if status.ready:
            return self._go(seq, status.reason or "built")
        built, since = self._progress
        if status.built > built:
            self._progress = (status.built, now)
        elif now - since > STALL_S:
            return self._go(seq, "no progress for 60 s (internet or imagery provider?)")
        return f"AO1 WAIT {int(status.built)} {int(status.total)} {int(seq)}"

    def _go(self, seq, reason):
        if self.holding_since is not None:
            held = self.clock() - self.holding_since
            self.counters["files_start_s"] = held
            log.info("X-Plane released after %.1fs: %s", held, reason)
        self.holding_since = None
        self.last_go_reason = reason
        return f"AO1 GO {int(seq)} {reason.replace(' ', '_')[:80]}"

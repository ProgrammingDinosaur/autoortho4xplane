"""Files the app and the update helper share.

All of them live in ``~/.autoortho-data/updates``:

* ``<tag>/``: the download and its unpacked staging folder
* ``plan.json``: what the helper should do (written by the app)
* ``result.json``: what happened (written by the helper, read and removed
  by the next AutoOrtho that starts)
* ``pending-start.json``: the version the helper just started; that version
  writes ``started.json`` once its window is up, which tells the helper the
  update worked. While it exists, startup cleanup leaves the staging folders
  alone (the helper may still be running from one).
* ``state.json``: the previous version kept for rolling back, and folders
  left to delete

The helper may be a newer (update) or older (rollback) AutoOrtho than the
app that wrote the plan, so the plan format only ever grows.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PLAN_SCHEMA = 1

ACTION_SWAP = "swap"  # replace the install folder with a staged folder
ACTION_INSTALLER = "installer"  # run the NSIS installer silently


def updates_dir(home=None):
    base = Path(home) if home else Path(os.path.expanduser("~"))
    return base / ".autoortho-data" / "updates"


def plan_path(root=None):
    return Path(root or updates_dir()) / "plan.json"


def result_path(root=None):
    return Path(root or updates_dir()) / "result.json"


def pending_start_path(root=None):
    return Path(root or updates_dir()) / "pending-start.json"


def started_path(root=None):
    return Path(root or updates_dir()) / "started.json"


def state_path(root=None):
    return Path(root or updates_dir()) / "state.json"


@dataclass
class UpdatePlan:
    action: str
    wait_pid: int
    target: str  # install folder (or .app) being replaced
    platform: str
    from_version: str
    to_version: str
    wait_pid_created: float = 0.0  # its start time, to tell a reused PID apart
    source: str = ""  # swap: staged build folder to copy in
    backup: str = ""  # swap: where the current install is moved
    installer: str = ""  # installer: downloaded AutoOrtho_win_<v>.exe
    installer_args: list = field(default_factory=list)
    relaunch: list = field(default_factory=list)  # command for the new app
    rollback: bool = False  # True when going back to the kept version
    health_timeout: float = 120.0
    updates_root: str = ""
    schema: int = PLAN_SCHEMA


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def write_plan(plan, path=None):
    path = path or plan_path(plan.updates_root or None)
    _write_json(path, asdict(plan))
    return Path(path)


def read_plan(path):
    data = _read_json(path)
    if data is None:
        raise ValueError(f"No readable update plan at {path}.")
    # A newer schema only adds fields; ignore the ones this version lacks.
    known = UpdatePlan.__dataclass_fields__
    plan = UpdatePlan(**{key: value for key, value in data.items() if key in known})
    if plan.action not in (ACTION_SWAP, ACTION_INSTALLER):
        raise ValueError(f"Unknown update action {plan.action!r}.")
    return plan


# -- result ------------------------------------------------------------------

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"  # nothing changed; the old version runs again
STATUS_ROLLED_BACK = "rolled_back"  # new version failed to start; restored
STATUS_CANCELLED = "cancelled"  # e.g. the administrator prompt was declined
STATUS_BROKEN = "broken"  # the installer stopped part way; reinstall


def write_result(status, message, plan, root=None, **extra):
    data = {
        "status": status,
        "message": message,
        "from_version": plan.from_version,
        "to_version": plan.to_version,
        "rollback": plan.rollback,
        "time": time.time(),
    }
    data.update(extra)
    _write_json(result_path(root or plan.updates_root or None), data)


def take_result(root=None):
    """The helper's last result (removed once read), or None."""
    path = result_path(root)
    data = _read_json(path)
    if data is not None:
        _remove(path)
    return data


# -- start check -----------------------------------------------------------


def write_pending_start(version_text, root=None, rollback=False, from_version=""):
    _remove(started_path(root))
    _write_json(
        pending_start_path(root),
        {
            "version": version_text,
            "from_version": from_version,
            "rollback": bool(rollback),
            "time": time.time(),
        },
    )


def clear_pending_start(root=None):
    _remove(pending_start_path(root))
    _remove(started_path(root))


def pending_start(root=None):
    return _read_json(pending_start_path(root))


def confirm_started(version_text, root=None):
    """Called by AutoOrtho once its window is up.

    When this start is the one the helper waits for, records it and returns
    the pending record (``version``, ``from_version``, ``rollback``) so the
    app can say what just happened; otherwise returns None.
    """
    pending = pending_start(root)
    if not pending or pending.get("version") != version_text:
        return None
    _write_json(
        started_path(root),
        {"version": version_text, "pid": os.getpid(), "time": time.time()},
    )
    return pending


def has_started(version_text, root=None):
    data = _read_json(started_path(root))
    return bool(data) and data.get("version") == version_text


# -- state -------------------------------------------------------------------


def read_state(root=None):
    data = _read_json(state_path(root)) or {}
    backup = data.get("backup")
    cleanup = data.get("cleanup")
    return {
        "backup": backup if isinstance(backup, dict) else None,
        "cleanup": [str(item) for item in cleanup] if isinstance(cleanup, list) else [],
    }


def write_state(state, root=None):
    _write_json(
        state_path(root),
        {"backup": state.get("backup"), "cleanup": list(state.get("cleanup") or [])},
    )

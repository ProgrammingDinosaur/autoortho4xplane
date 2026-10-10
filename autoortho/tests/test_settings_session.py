import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.settings_session import SettingsSession


def test_observe_tracks_dirty_and_restart_keys():
    session = SettingsSession({"path", "workers"})
    session.initialize({"path": "/a", "log_level": "INFO"})

    session.observe({"path": "/b", "log_level": "INFO"})

    assert session.dirty is True
    assert session.restart_required is True
    assert session.changed_keys == {"path"}


def test_runtime_safe_change_does_not_require_restart():
    session = SettingsSession({"path"})
    session.initialize({"path": "/a", "log_level": "INFO"})

    session.observe({"path": "/a", "log_level": "DEBUG"})

    assert session.dirty is True
    assert session.restart_required is False


def test_revert_returns_independent_baseline():
    session = SettingsSession({"path"})
    baseline = {"path": "/a", "steps": [{"altitude": 0}]}
    session.initialize(baseline)
    session.observe({"path": "/b", "steps": [{"altitude": 0}]})

    restored = session.revert()
    restored["steps"][0]["altitude"] = 100

    assert session.dirty is False
    assert session.baseline()["steps"][0]["altitude"] == 0


def test_mark_applied_replaces_baseline():
    session = SettingsSession({"path"})
    session.initialize({"path": "/a"})
    session.observe({"path": "/b"})

    session.mark_applied({"path": "/b"})

    assert session.dirty is False
    assert session.baseline() == {"path": "/b"}

"""Which threads keep their OpenMP team after a native build.

See ``getortho._settle_native_team`` and test_native_thread_teams.py.
"""
import threading
import types

import pytest

from autoortho import getortho


@pytest.fixture
def released(monkeypatch):
    calls = []
    native = types.SimpleNamespace(
        release_thread_team=lambda: calls.append(threading.get_ident()) or True
    )
    monkeypatch.setattr(getortho, "_get_native_dds", lambda: native)
    monkeypatch.setattr(getortho, "_native_cpu_budget", lambda: 8)
    monkeypatch.setattr(getortho, "_native_team_keepers", {})
    monkeypatch.setattr(getortho, "_native_team_kept", 0)
    return calls


def _build_on_new_thread(threads):
    worker = threading.Thread(target=getortho._settle_native_team, args=(threads,))
    worker.start()
    worker.join()
    return worker.ident


def test_threads_keep_teams_until_one_cpu_budget_is_kept(released):
    # Teams of 4 keep 3 workers each: two fit in a budget of 8, a third not.
    first = _build_on_new_thread(4)
    second = _build_on_new_thread(4)
    third = _build_on_new_thread(4)
    assert released == [third]
    assert first != third and second != third


def test_a_keeper_keeps_its_team_across_builds(released):
    getortho._settle_native_team(4)
    getortho._settle_native_team(4)
    getortho._settle_native_team(2)
    assert released == []
    assert getortho._native_team_kept == 3


def test_a_keeper_that_outgrows_the_budget_releases(released):
    getortho._settle_native_team(4)
    _build_on_new_thread(6)  # 5 more workers: 8 kept in all
    getortho._settle_native_team(8)  # 7 workers would need 4 more
    assert released == [threading.get_ident()]


def test_an_exited_keepers_share_is_not_handed_on(released):
    # On Windows libgomp does not free a team when its thread exits.
    for _ in range(2):
        _build_on_new_thread(5)  # 4 workers each: the budget is now kept
    late = _build_on_new_thread(2)
    assert released == [late]


def test_single_thread_builds_keep_nothing(released):
    getortho._settle_native_team(1)
    assert released == []
    assert getortho._native_team_kept == 0


def test_build_context_settles_the_team(released, monkeypatch):
    settled = []
    monkeypatch.setattr(getortho, "_settle_native_team", settled.append)
    with getortho._native_build_context(threads=2) as threads:
        pass
    assert settled == [threads]


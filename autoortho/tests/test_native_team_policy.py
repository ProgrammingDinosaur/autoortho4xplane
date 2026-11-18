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
    # Teams of 3 keep 2 workers each: four fit in a budget of 8, a fifth not.
    keepers = [_build_on_new_thread(3) for _ in range(4)]
    late = _build_on_new_thread(3)
    assert released == [late]
    assert late not in keepers


def test_a_keeper_keeps_its_team_across_builds(released):
    getortho._settle_native_team(3)
    getortho._settle_native_team(3)
    getortho._settle_native_team(2)
    assert released == []
    assert getortho._native_team_kept == 2


def test_one_wide_team_does_not_take_the_whole_share(released):
    # 4 workers is over a quarter of the budget: freed, and smaller teams
    # on other threads still keep theirs.
    getortho._settle_native_team(5)
    assert released == [threading.get_ident()]
    assert getortho._native_team_kept == 0
    _build_on_new_thread(3)
    assert len(released) == 1


def test_a_keeper_that_outgrows_the_cap_hands_its_share_on(released):
    for _ in range(3):
        _build_on_new_thread(3)  # 6 of 8 kept
    getortho._settle_native_team(3)  # this thread keeps the last 2
    getortho._settle_native_team(4)  # 3 workers: over the cap
    assert released == [threading.get_ident()]
    assert getortho._native_team_kept == 6
    newcomer = _build_on_new_thread(3)
    assert newcomer not in released


def test_an_exited_keepers_share_is_not_handed_on(released):
    # On Windows libgomp does not free a team when its thread exits.
    for _ in range(4):
        _build_on_new_thread(3)  # 2 workers each: the budget is now kept
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


def test_a_capped_build_gets_at_most_its_cap(monkeypatch):
    monkeypatch.setattr(getortho, "_native_cpu_budget", lambda: 8)
    monkeypatch.setattr(getortho, "_settle_native_team", lambda threads: None)
    with getortho._native_build_context(cap=2) as threads:
        assert threads == 2
    with getortho._native_build_context(cap=100) as threads:
        assert threads == 4  # the default share, half the budget

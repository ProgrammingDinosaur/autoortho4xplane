"""Performance plan, item 5: the stats store served from a thread of the parent."""

import multiprocessing
import os
import types

import pytest


def _client_process(addr, auth, count):
    os.environ["AO_STATS_ADDR"] = addr
    os.environ["AO_STATS_AUTH"] = auth
    from autoortho import aostats

    manager, store = aostats._connect_manager_from_env()
    for _ in range(count):
        store.inc_many({"chunks": 1, "bytes": 10})
    store.set("proc_alive_ts:1", 123)


@pytest.fixture
def mount():
    from autoortho import autoortho as ao

    obj = types.SimpleNamespace()
    ao.AOMount.start_stats_manager(obj)
    yield ao, obj
    ao.AOMount.stop_stats_manager(obj)


def test_workers_counters_reach_the_parent_store(mount):
    ao, obj = mount
    assert isinstance(obj.stats_manager, ao._ThreadedStatsServer)
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_client_process, args=(obj.stats_addr, obj.stats_auth.decode(), 500))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    snapshot = obj._shared_store.snapshot()
    assert snapshot["chunks"] == 1000
    assert snapshot["bytes"] == 10000
    assert snapshot["proc_alive_ts:1"] == 123


def test_parent_counters_skip_the_socket(mount):
    from autoortho import aostats

    _ao, obj = mount
    aostats.inc_stat("parent_counter", 3)
    assert obj._shared_store.get("parent_counter") == 3


def test_stop_ends_the_server_and_a_new_session_starts_clean():
    from autoortho import aostats
    from autoortho import autoortho as ao

    obj = types.SimpleNamespace()
    ao.AOMount.start_stats_manager(obj)
    aostats.inc_stat("x", 5)
    server = obj.stats_manager
    ao.AOMount.stop_stats_manager(obj)
    assert not server._accept_thread.is_alive()
    ao.AOMount.start_stats_manager(obj)
    try:
        assert obj._shared_store.get("x", 0) == 0
    finally:
        ao.AOMount.stop_stats_manager(obj)

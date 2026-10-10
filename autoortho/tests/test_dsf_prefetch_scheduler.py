#!/usr/bin/env python3
"""Focused tests for bounded DSF prefetch scheduling."""

import os
import sys
import threading
import time


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
AUTOORTHO_DIR = os.path.join(ROOT, "autoortho")
for directory in (ROOT, AUTOORTHO_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)

from autoortho_fuse import DSFPrefetchScheduler


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_duplicate_paths_are_coalesced():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def submit(path):
        calls.append(path)
        started.set()
        assert release.wait(1.0)
        return 4

    scheduler = DSFPrefetchScheduler(
        submit, lambda: True, grace_period=0, poll_interval=0.01
    )
    try:
        assert scheduler.schedule("/Earth nav data/+00+000.dsf")
        assert started.wait(1.0)
        assert not scheduler.schedule("/Earth nav data/+00+000.dsf")
        release.set()
        assert _wait_for(
            lambda: scheduler.state("/Earth nav data/+00+000.dsf")
            == scheduler.COMPLETE
        )
        assert calls == ["/Earth nav data/+00+000.dsf"]
    finally:
        release.set()
        scheduler.shutdown()


def test_worker_count_is_fixed_and_bounded():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def submit(path):
        calls.append(path)
        if len(calls) == 2:
            started.set()
        assert release.wait(1.0)
        return 1

    scheduler = DSFPrefetchScheduler(
        submit,
        lambda: True,
        worker_count=99,
        queue_size=8,
        grace_period=0,
        poll_interval=0.01,
    )
    try:
        for index in range(5):
            assert scheduler.schedule(f"/Earth nav data/{index}.dsf")
        assert started.wait(1.0)
        assert scheduler.worker_count() == 2
        assert len(scheduler._workers) == 2
        assert all(worker.is_alive() for worker in scheduler._workers)
    finally:
        release.set()
        scheduler.shutdown()


def test_failed_submission_retries_then_completes():
    calls = []

    def submit(path):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return 3

    scheduler = DSFPrefetchScheduler(
        submit,
        lambda: True,
        grace_period=0,
        retry_delay=0.01,
        max_retry_delay=0.02,
        poll_interval=0.005,
    )
    try:
        assert scheduler.schedule("/Earth nav data/+01+001.dsf")
        assert _wait_for(
            lambda: scheduler.state("/Earth nav data/+01+001.dsf")
            == scheduler.COMPLETE
        )
        assert calls == ["/Earth nav data/+01+001.dsf"] * 2
    finally:
        scheduler.shutdown()


def test_pressure_defers_with_backoff_and_preserves_cursor():
    calls = []
    first_submission = threading.Event()

    def submit(path, cursor=None):
        calls.append((path, cursor, time.monotonic()))
        if len(calls) == 1:
            first_submission.set()
            return {
                "submitted": 1,
                "complete": False,
                "pressure": True,
                "cursor": "chunk-17",
            }
        return {"submitted": 2, "complete": True}

    scheduler = DSFPrefetchScheduler(
        submit,
        lambda: True,
        grace_period=0,
        retry_delay=0.04,
        max_retry_delay=0.04,
        poll_interval=0.005,
    )
    path = "/Earth nav data/+02+002.dsf"
    try:
        assert scheduler.schedule(path)
        assert first_submission.wait(1.0)
        assert _wait_for(lambda: scheduler.state(path) == scheduler.DEFERRED)
        assert _wait_for(lambda: scheduler.state(path) == scheduler.COMPLETE)
        assert [call[1] for call in calls] == [None, "chunk-17"]
        assert calls[1][2] - calls[0][2] >= 0.03
    finally:
        scheduler.shutdown()


def test_shutdown_cancels_deferred_work_and_joins_workers():
    scheduler = DSFPrefetchScheduler(
        lambda _path: 1,
        lambda: False,
        grace_period=0,
        poll_interval=0.01,
    )
    assert scheduler.schedule("/Earth nav data/+03+003.dsf")
    assert _wait_for(
        lambda: scheduler.state("/Earth nav data/+03+003.dsf")
        == scheduler.DEFERRED
    )

    scheduler.shutdown()

    assert scheduler.state("/Earth nav data/+03+003.dsf") is None
    assert not any(worker.is_alive() for worker in scheduler._workers)


def test_flight_gate_parks_without_retries_and_wakes_once():
    class Gate:
        def __init__(self):
            self.allowed = False
            self.condition = threading.Condition()

        def wait_until_allowed(self, stop_event):
            with self.condition:
                while not self.allowed and not stop_event.is_set():
                    self.condition.wait()
                return self.allowed

        def notify_state_change(self):
            with self.condition:
                self.condition.notify_all()

        def open(self):
            with self.condition:
                self.allowed = True
                self.condition.notify_all()

    gate = Gate()
    calls = []
    scheduler = DSFPrefetchScheduler(
        lambda path: calls.append(path) or 1,
        lambda: gate.allowed,
        runtime_gate=gate,
        grace_period=0,
        queue_size=8,
    )
    path = "/Earth nav data/+04+004.dsf"
    try:
        assert scheduler.schedule(path)
        assert _wait_for(
            lambda: scheduler.state(path) == scheduler.WAITING_FOR_FLIGHT
        )
        time.sleep(0.03)
        assert calls == []
        assert scheduler._entries[path]["retries"] == 0
        assert scheduler._entries[path]["pressure_retries"] == 0

        gate.open()
        assert _wait_for(lambda: scheduler.state(path) == scheduler.COMPLETE)
        assert calls == [os.path.normcase(os.path.abspath(path))]
    finally:
        scheduler.shutdown()


def test_flight_gate_permission_race_does_not_kill_worker():
    class Gate:
        def __init__(self):
            self.condition = threading.Condition()
            self.allowed = False

        def wait_until_allowed(self, stop_event):
            with self.condition:
                self.allowed = True
                self.condition.notify_all()
                return True

        def notify_state_change(self):
            with self.condition:
                self.condition.notify_all()

    gate = Gate()
    checks = {"count": 0}

    def allowed():
        checks["count"] += 1
        return checks["count"] not in {1, 2}

    scheduler = DSFPrefetchScheduler(
        lambda _path: 1,
        allowed,
        runtime_gate=gate,
        grace_period=0,
        poll_interval=0.01,
    )
    path = "/Earth nav data/+05+005.dsf"
    try:
        assert scheduler.schedule(path)
        assert _wait_for(
            lambda: scheduler.state(path) in {
                scheduler.DEFERRED,
                scheduler.COMPLETE,
            }
        )
        assert all(worker.is_alive() for worker in scheduler._workers)
    finally:
        scheduler.shutdown()

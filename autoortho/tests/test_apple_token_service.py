import os
import sys
import threading
import time

import requests

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

from utils.apple_token_service import AppleTokenService


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_concurrent_refresh_is_single_flight(monkeypatch):
    service = AppleTokenService()
    calls = []
    calls_lock = threading.Lock()

    def fake_get(url, **kwargs):
        with calls_lock:
            calls.append(url)
        time.sleep(0.02)
        if "duckduckgo" in url:
            return FakeResponse(text="bootstrap-token")
        return FakeResponse(
            payload={
                "tileSources": [
                    {
                        "tileSource": "satellite",
                        "path": "/tile?v=9&accessKey=new-key",
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "utils.apple_token_service.requests.get",
        fake_get,
    )
    generation = service.generation
    threads = [
        threading.Thread(
            target=service.reset_apple_maps_token,
            kwargs={"expected_generation": generation},
        )
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert service.apple_token == "new-key"
    assert service.generation == 1
    assert len(calls) == 2


def test_concurrent_refresh_failure_reaches_waiters(monkeypatch):
    service = AppleTokenService()
    started = threading.Event()
    release = threading.Event()
    errors = []

    def failing_get(_url, **_kwargs):
        started.set()
        release.wait(1)
        raise requests.exceptions.Timeout("offline")

    monkeypatch.setattr(
        "utils.apple_token_service.requests.get",
        failing_get,
    )

    def refresh():
        try:
            service.reset_apple_maps_token(expected_generation=0)
        except RuntimeError as exc:
            errors.append(str(exc))

    leader = threading.Thread(target=refresh)
    leader.start()
    assert started.wait(1)
    waiters = [threading.Thread(target=refresh) for _ in range(5)]
    for waiter in waiters:
        waiter.start()
    time.sleep(0.02)
    release.set()
    leader.join(1)
    for waiter in waiters:
        waiter.join(1)

    assert len(errors) == 6
    assert service.generation == 0

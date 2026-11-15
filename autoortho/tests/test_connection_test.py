"""The connection test (ported from OrthoStreamXP) and provider URLs."""

import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connection_test as ct  # noqa: E402
import provider_urls  # noqa: E402
from connection_test import Round  # noqa: E402


def _round(at_once, mbits, failed=0, chunks=100):
    return Round(at_once, chunks, failed, 1_000_000, 1.0, 0.1, mbits)


def test_suggest_is_fewest_reaching_ninety_percent():
    rounds = [_round(16, 40), _round(32, 75), _round(64, 92), _round(128, 95)]
    assert ct.suggest(rounds) == 64


def test_suggest_skips_rounds_with_failures():
    rounds = [_round(16, 40), _round(32, 95, failed=2), _round(64, 96)]
    assert ct.suggest(rounds) == 64
    assert ct.suggest([_round(16, 0)]) == 0


def test_enough_when_flat_or_refused():
    assert not ct.enough([_round(16, 40)])
    assert not ct.enough([_round(16, 40), _round(32, 60)])
    assert ct.enough([_round(16, 40), _round(32, 41)])
    assert ct.enough([_round(16, 40), _round(32, 90, failed=10)])


def test_steady_speed_ignores_first_and_last_tenth():
    arrived = [(i * 0.1, 125_000) for i in range(20)]
    arrived[-1] = (100.0, 125_000)      # one straggler
    # 16 chunks of 1 Mbit between t=0.2 and t=1.8 (1.6 s): 10 Mbit/s.
    assert ct.steady_mbits(arrived) == pytest.approx(16 * 1.0 / 1.6)
    assert ct.steady_mbits(arrived[:5]) == 0.0


def test_provider_for_map_type():
    assert ct.provider_for("Use tile default") == "BI"
    assert ct.provider_for("Firefly") == "FIREFLY"
    assert ct.provider_for("APPLE") == "BI"      # needs a session token
    assert ct.provider_for("Custom Map") == "BI"
    assert ct.provider_for("ARC") == "ARC"


def test_sample_chunks_do_not_repeat():
    chunks = ct.sample_chunks("BI")
    seen = [next(chunks) for _ in range(512)]
    assert len(set(seen)) == len(seen)


def test_us_only_providers_test_in_the_us():
    import random

    x, y = next(ct.sample_chunks("NAIP", random.Random(1)))
    lon = x / 2 ** ct.ZOOM * 360 - 180
    assert -125 < lon < -65


class _Response:
    def __init__(self, status, size):
        self.status_code = status
        self.content = b"x" * size


class _FakeClient:
    """Answers every chunk after a short wait; fails one in fail_every."""

    def __init__(self, fail_every=0):
        self.fail_every = fail_every
        self.calls = 0
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls += 1
        self.urls.append(url)
        await asyncio.sleep(0.001)
        if self.fail_every and self.calls % self.fail_every == 0:
            return _Response(503, 0)
        return _Response(200, 16_000)


def test_run_saves_and_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RESULT_PATH", str(tmp_path / "connection.json"))
    client = _FakeClient()
    lines = []
    result = ct.run("ARC", lines.append, client_factory=lambda: client)
    assert result.provider == "ARC"
    assert result.rounds and result.rounds[0].at_once == 16
    assert all("arcgisonline" in url for url in client.urls)
    assert lines[0] == "Opening the connection…"
    loaded = ct.load_result()
    assert loaded == result
    assert loaded.summary.startswith("About ")


def test_refusing_server_stops_early(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RESULT_PATH", str(tmp_path / "connection.json"))
    result = ct.run("BI", client_factory=lambda: _FakeClient(fail_every=5))
    assert len(result.rounds) == 1                # 20 % failed: enough
    assert result.suggested == 0
    assert "could not download" in result.summary


def test_cancelled_run_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RESULT_PATH", str(tmp_path / "connection.json"))
    cancel = threading.Event()
    cancel.set()
    ct.run("BI", cancel=cancel, client_factory=lambda: _FakeClient())
    assert ct.load_result() is None


def test_load_result_tolerates_bad_files(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text("{not json", encoding="utf-8")
    assert ct.load_result(str(path)) is None
    assert ct.load_result(str(tmp_path / "missing.json")) is None


def test_bing_quadkey():
    assert provider_urls.quadkey(3, 5, 3) == "213"
    url, headers = provider_urls.tile_url("BI", 3, 5, 3)
    assert url == "https://t.ssl.ak.tiles.virtualearth.net/tiles/a213.jpeg?g=15312"
    assert headers == {"user-agent": "curl/7.68.0"}


def test_eox_has_referer_and_unknown_raises():
    _url, headers = provider_urls.tile_url("eox", 1, 2, 3)
    assert headers["referer"] == "https://s2maps.eu/"
    with pytest.raises(KeyError):
        provider_urls.tile_url("NOPE", 1, 2, 3)

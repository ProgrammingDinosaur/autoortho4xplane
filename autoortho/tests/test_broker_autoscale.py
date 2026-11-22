"""Performance plan, item 5: download brokers started and retired with the load."""

import threading
import time

import pytest

from autoortho.http2_broker import BrokerAutoscaler, BrokerGroup, MultiBrokerClient


class FakeBroker:
    count = 0

    def __init__(self, entries=0):
        FakeBroker.count += 1
        self.n = FakeBroker.count
        self.entries = entries
        self.started = False
        self.stopped = False
        self.configured = []

    def start(self):
        self.started = True

    def stop(self, timeout=5.0):
        self.stopped = True

    def client_environment(self):
        return {
            "AO_HTTP2_BROKER_ADDR": f"tcp://127.0.0.1:{9000 + self.n}",
            "AO_HTTP2_BROKER_TOKEN": f"t{self.n}",
        }

    def server_stats(self, timeout=5.0):
        return {"entries": self.entries}

    def configure(self, **kwargs):
        self.configured.append(kwargs)
        return True

    @property
    def pid(self):
        return 100 + self.n


def _group(published, max_brokers=4, entries=0):
    created = []

    def factory():
        broker = FakeBroker(entries=entries)
        created.append(broker)
        return broker

    group = BrokerGroup(
        [FakeBroker(entries=entries)],
        factory=factory,
        max_brokers=max_brokers,
        publish=published.append,
    )
    group.start()
    return group, created


def test_group_grows_to_its_maximum_and_publishes_each_change():
    published = []
    group, created = _group(published, max_brokers=2)
    assert group.client_environment()["AO_HTTP2_BROKER_DYNAMIC"] == "1"
    assert group.add_broker()
    assert not group.add_broker()  # at the maximum
    assert len(group.brokers) == 2 and created[0].started
    assert published[-1]["addresses"] == [
        b.client_environment()["AO_HTTP2_BROKER_ADDR"] for b in group.brokers
    ]


def test_rate_is_shared_again_after_scaling():
    published = []
    group, created = _group(published)
    group.configure(max_bytes_per_second=1000.0)
    group.add_broker()
    assert created[0].configured[-1]["max_bytes_per_second"] == 500.0


def test_retire_drains_first_and_never_stops_the_first_broker():
    published = []
    group, created = _group(published)
    group.add_broker()
    extra = created[0]
    extra.entries = 3

    def empty_later():
        time.sleep(0.3)
        extra.entries = 0

    threading.Thread(target=empty_later).start()
    started = time.monotonic()
    assert group.retire_broker(poll=0.05)
    assert time.monotonic() - started >= 0.25  # waited for its requests
    # Marked draining before it was stopped.
    address = extra.client_environment()["AO_HTTP2_BROKER_ADDR"]
    assert any(address in table["draining"] for table in published)
    assert extra.stopped
    assert address not in published[-1]["addresses"]
    assert not group.retire_broker()  # one broker always stays


def test_autoscaler_adds_after_sustained_pressure_and_retires_after_calm():
    published = []
    group, _created = _group(published)
    now = [0.0]
    scaler = BrokerAutoscaler(group, clock=lambda: now[0], calm_seconds=300.0)
    assert scaler.tick(pressured=True) is None
    now[0] += 1.0
    assert scaler.tick(pressured=True) is None
    now[0] += 1.0
    assert scaler.tick(pressured=True) == "add"
    assert len(group.brokers) == 2
    now[0] += 100.0
    assert scaler.tick(pressured=False) is None
    now[0] += 250.0
    assert scaler.tick(pressured=False) == "retire"


def test_pressure_starts_a_second_broker_within_three_seconds():
    published = []
    group, _created = _group(published, entries=100)
    scaler = BrokerAutoscaler(group, interval=0.2)
    scaler.start()
    try:
        deadline = time.monotonic() + 3.0
        while len(group.brokers) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(group.brokers) >= 2
    finally:
        scaler.stop()


# --------------------------------------------------------------- the workers


class FakeClient:
    def __init__(self, address):
        self.address = address
        self.stopped = False
        self.requests = []

    def get(self, url, headers=None, request_id=None, **kwargs):
        self.requests.append(url)
        return self.address

    def stop(self, timeout=5.0):
        self.stopped = True

    def pending_count(self):
        return 0


def test_client_follows_the_routing_table():
    first = FakeClient("a")
    connected = {}

    def connector(address, token):
        connected[address] = FakeClient(address)
        return connected[address]

    client = MultiBrokerClient([first], addresses=["a"], connector=connector)
    assert client.apply_routing({"generation": 1, "addresses": ["a", "b"], "tokens": ["x", "y"], "draining": []})
    urls = [f"https://t/{i}" for i in range(200)]
    owners = {client.get(url) for url in urls}
    assert owners == {"a", "b"}

    # b drains: no new request goes to it, it stays connected.
    client.apply_routing({"generation": 2, "addresses": ["a", "b"], "tokens": ["x", "y"], "draining": ["b"]})
    before = len(connected["b"].requests)
    assert {client.get(url) for url in urls} == {"a"}
    assert len(connected["b"].requests) == before
    assert not connected["b"].stopped

    # b is gone: its connection closes.
    client.apply_routing({"generation": 3, "addresses": ["a"], "tokens": ["x"], "draining": []})
    assert connected["b"].stopped
    # The same generation again changes nothing.
    assert not client.apply_routing({"generation": 3, "addresses": ["a"], "tokens": ["x"], "draining": []})


def test_client_watch_polls_the_table():
    tables = [{"generation": 1, "addresses": ["a", "b"], "tokens": ["x", "y"], "draining": []}]
    client = MultiBrokerClient(
        [FakeClient("a")], addresses=["a"], connector=lambda a, t: FakeClient(a)
    )
    client.watch_routing(lambda: tables[-1], interval=0.05)
    deadline = time.monotonic() + 2
    while len(client.clients) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    assert len(client.clients) == 2

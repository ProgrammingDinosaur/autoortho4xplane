"""Build slots: resizable DDS buffer and builder pools, the live-builders
lever, the build-slot signal, and the plan record in flight reports."""

import os
import sys
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import governor as gv  # noqa: E402
import target_cost as tc  # noqa: E402
import tuning_plan  # noqa: E402
from aopipeline import AoDDS  # noqa: E402
from governor import Governor, LeverSpec, Sample  # noqa: E402
from test_governor import VALUES, Flight, _levers  # noqa: E402

GB = 1024 ** 3
LIVE = gv.LIVE_BUILDERS_LEVER


# ------------------------------------------------------------ buffer pool


def _pool(size=2):
    return AoDDS.DDSBufferPool(buffer_size=64, pool_size=size)


def test_buffer_pool_grows_and_serves_a_waiter():
    pool = _pool(1)
    held, held_id = pool.acquire(timeout=0.1)
    got = []

    def wait():
        got.append(pool.acquire(timeout=2.0))

    waiter = threading.Thread(target=wait)
    waiter.start()
    while pool.waiter_count == 0:
        pass
    pool.resize(2)
    waiter.join(1.0)
    assert got and got[0][1] != held_id
    assert pool.pool_size == 2 and pool.in_use_count == 2


def test_buffer_pool_shrinks_without_taking_buffers_from_holders():
    pool = _pool(3)
    a = pool.acquire(timeout=0.1)
    b = pool.acquire(timeout=0.1)
    pool.resize(1)              # one idle buffer freed now, one busy later
    assert pool.available_count == 0 and pool.in_use_count == 2
    pool.release(a[1])          # retired: the pool is over its size
    assert pool.available_count == 0 and pool.in_use_count == 1
    pool.release(b[1])          # kept: the pool is at its size
    assert pool.available_count == 1
    assert len(pool._buffers) == 1


def test_buffer_pool_never_reuses_a_retired_id():
    pool = _pool(2)
    a = pool.acquire(timeout=0.1)
    pool.resize(1)
    pool.resize(3)
    ids = set(pool._buffers)
    assert a[1] in ids and len(ids) == 3
    assert len(ids | {0, 1}) == 4      # the new ids are fresh


def test_buffer_handed_over_as_the_wait_times_out_is_not_leaked(monkeypatch):
    pool = _pool(1)
    _held, held_id = pool.acquire(timeout=0.1)

    class LateEvent(threading.Event):
        def wait(self, timeout=None):
            pool.release(held_id)   # the release lands just as we time out
            return False

    monkeypatch.setattr(AoDDS.threading, "Event", LateEvent)
    buffer, buffer_id = pool.acquire(timeout=0.01)
    assert buffer_id == held_id
    assert pool.in_use_count == 1 and pool.waiter_count == 0


# ----------------------------------------------------------- builder pool


class FakeLib:
    def __init__(self):
        self.destroyed = []

    def aodds_builder_destroy(self, handle):
        self.destroyed.append(handle)


def _builder_pool(size, created):
    pool = AoDDS.StreamingBuilderPool(pool_size=size)
    pool._lib = FakeLib()
    pool._initialized = True
    pool._available = [f"h{i}" for i in range(created)]
    pool._total_created = created
    return pool


def test_builder_pool_shrink_destroys_idle_then_returned_builders():
    pool = _builder_pool(4, 2)
    busy = "busy"
    pool._in_use.add(id(busy))
    pool._total_created = 3
    pool.resize(1)
    assert pool._lib.destroyed == ["h1", "h0"]
    assert pool._total_created == 1
    pool._return_builder(busy)
    assert pool._available == [busy]     # at size: kept
    pool.resize(0)                        # floor of one
    assert pool.pool_size == 1


def test_builder_pool_returned_builder_is_destroyed_while_over_size():
    pool = _builder_pool(4, 0)
    pool._total_created = 3
    pool.resize(2)
    pool._return_builder("x")
    assert pool._lib.destroyed == ["x"] and pool._total_created == 2


# ------------------------------------------------------------ sizing rules


class SlotCfg:
    def __init__(self, custom=(), **values):
        self.autoortho = SimpleNamespace(**values)
        self.custom = set(custom)

    def is_custom(self, section, key):
        return key in self.custom


def test_build_slots_follow_the_builders_unless_set_by_hand():
    cfg = SlotCfg(background_builder_workers=3, live_builder_concurrency=9,
                  streaming_builder_pool_size=4, buffer_pool_size=10)
    assert AoDDS.build_slot_count(cfg, "streaming_builder_pool_size", 4) == 12
    assert AoDDS.build_slot_count(cfg, "buffer_pool_size", 10) == 12
    mine = SlotCfg(custom={"buffer_pool_size"}, background_builder_workers=3,
                   live_builder_concurrency=9, buffer_pool_size=6)
    assert AoDDS.build_slot_count(mine, "buffer_pool_size", 10) == 6


def test_live_builders_ceiling_follows_cores_and_ram():
    assert gv.live_builders_ceiling(6, 8.5) == 8            # cores + 2
    assert gv.live_builders_ceiling(30, 64) == 32           # hard cap
    # 2.5 GB RAM limit: 0.625 GB of 8K buffers = 14, minus background.
    assert gv.live_builders_ceiling(16, 2.5) == 14 - 8
    assert gv.live_builders_ceiling(2, 1) == 4              # floor


def test_levers_include_live_builders_within_the_limits():
    import limits
    from hardware import HardwareInfo

    pc = HardwareInfo(16, 8, 32 * GB, 12 * GB)
    specs = gv.levers_for(SimpleNamespace(), limits.resolve({}, pc))
    spec = specs[LIVE]
    assert spec.group == "compute" and spec.minimum == 2
    assert spec.maximum == 8          # 6 cores for AutoOrtho + 2


# --------------------------------------------------------- governor signal


def _levers_with_live():
    levers = _levers()
    levers[LIVE] = LeverSpec(LIVE, "Live builders", 2, 2, 16, group="compute")
    return levers


def _slot_samples(n, *, slots=20, slot_waits=0, timeouts=0, cpu=40.0, ao=None, **step):
    """Samples with ``slot_waits`` of every ``slots`` live builds waiting;
    ``step`` goes to Flight.step (its ``waits`` are admission waits)."""
    flight = Flight()
    out = []
    total = dict(build_slots=0, build_slot_waits=0, build_slot_timeouts=0)
    for _ in range(n):
        sample = flight.step(cpu=cpu, **step)
        total["build_slots"] += slots
        total["build_slot_waits"] += slot_waits
        total["build_slot_timeouts"] += timeouts
        out.append(Sample(**{**sample.__dict__, **total, "ao_cpu_percent": ao}))
    return out


def test_build_slot_state_and_move():
    gov = Governor(_levers_with_live(), dict(VALUES, **{LIVE: 8}), mode="advise")
    gov.starting = False
    samples = _slot_samples(6, slots=20, slot_waits=8, waits=10, admissions=20)
    window = gv.window_of(samples)
    assert window.build_wait_share == pytest.approx(0.4)
    assert gov.classify(window) == "build_slots"
    assert gov._candidates("build_slots") == [(LIVE, +1)]
    decisions = [d for d in (gov.observe(s, starting=False) for s in samples) if d]
    assert decisions and decisions[0].lever == LIVE and decisions[0].new == 10
    assert "of live builds waited" in decisions[0].reason


def test_build_slots_at_the_cpu_limit_ease_background_work():
    gov = Governor(_levers_with_live(), dict(VALUES, **{LIVE: 8}),
                   ao_cpu_budget_percent=50.0)
    gov.starting = False
    window = gv.window_of(_slot_samples(6, slots=20, slot_waits=8, ao=49.0))
    assert gov.classify(window) == "compute_limit"


def test_few_build_waits_leave_the_admission_rule_in_charge():
    gov = Governor(_levers_with_live(), dict(VALUES, **{LIVE: 8}))
    gov.starting = False
    flight = Flight()
    samples = []
    for i in range(6):
        sample = flight.step(waits=10, admissions=20, cpu=40)
        samples.append(Sample(**{**sample.__dict__, "build_slots": 20 * (i + 1),
                                 "build_slot_waits": i}))
    assert gov.classify(gv.window_of(samples)) == "compute"


def test_service_samples_the_build_slot_counters():
    counters = {"live_build_slots": 40, "live_build_slot_waits": 9,
                "live_build_slot_timeouts": 2}
    service = gv.GovernorService(Governor(_levers(), VALUES), read_stat=counters.get,
                                 ao_cpu=lambda: None)
    sample = service.sample()
    assert (sample.build_slots, sample.build_slot_waits,
            sample.build_slot_timeouts) == (40, 9, 2)


# ------------------------------------------------------- getortho wiring


@pytest.fixture
def getortho_module(monkeypatch):
    try:
        from autoortho import getortho
    except ImportError:
        import getortho
    # The pool sizes follow the builders unless set by hand; keep this
    # machine's own config out of it.
    monkeypatch.setattr(getortho.CFG, "is_custom", lambda section, key: False)
    return getortho


def test_buffer_pool_target_is_capped_by_the_ram_share(getortho_module, monkeypatch):
    go = getortho_module
    monkeypatch.setattr(go.CFG.autoortho, "background_builder_workers", 4, raising=False)
    monkeypatch.setattr(go.CFG.autoortho, "live_builder_concurrency", 20, raising=False)
    monkeypatch.setattr(go, "_ram_limit_bytes", 8 * GB)
    assert go._buffer_pool_target(11_186_000) == 24          # 2 GB fits 192
    assert go._buffer_pool_target(44_740_000) == 24          # 2 GB fits 48
    monkeypatch.setattr(go, "_ram_limit_bytes", 1 * GB)
    assert go._buffer_pool_target(44_740_000) == 5           # 256 MB fits 5.99


def test_note_build_slot_counts_waits_and_timeouts(getortho_module, monkeypatch):
    go = getortho_module
    seen = []
    monkeypatch.setattr(go, "bump", lambda key, *a, **k: seen.append(key))
    go.note_build_slot(2.0)
    go.note_build_slot(50.0)
    go.note_build_slot(5000.0, timed_out=True)
    assert seen.count("live_build_slots") == 3
    assert seen.count("live_build_slot_waits") == 2
    assert seen.count("live_build_slot_timeouts") == 1


def test_live_builders_change_resizes_the_pools(getortho_module, monkeypatch):
    go = getortho_module
    pool = _pool(4)
    monkeypatch.setattr(go, "_dds_buffer_pool", pool)
    monkeypatch.setattr(go, "_ram_limit_bytes", None)
    monkeypatch.setattr(go.CFG.autoortho, "background_builder_workers", 2, raising=False)
    monkeypatch.setattr(go.CFG.autoortho, "live_builder_concurrency", 10, raising=False)
    go._on_live_builders(10)
    assert pool.pool_size == 12


# ---------------------------------------------------- plan in the report


def test_plan_record_shows_where_settings_settled():
    import autoortho as ao_module

    plan = tuning_plan.plan(tc.target_cost("fixed", 17, 18), line_mbps=18)
    record = plan.describe()
    assert record["need_mbps"] > 0 and record["capacity_mbps"] == 18
    gov = Governor(_levers(), dict(VALUES, **{"autoortho.prefetch_radius_nm": 40}))
    out = ao_module.AOMount._settled_plan(record, gov)
    radius = out["settings"]["autoortho.prefetch_radius_nm"]
    assert radius["planned"] == float(plan.values["autoortho.prefetch_radius_nm"])
    assert radius["settled"] == 40 and radius["moved"]
    chunks = out["settings"]["autoortho.prefetch_max_chunks"]
    assert "settled" not in chunks        # not a lever in this governor


def test_report_renders_the_plan_table():
    import diagnostics

    lines = diagnostics._render_flight({
        "verdict": "Smooth flight",
        "plan": {
            "target": {"mode": "fixed", "zoom": 17, "airport_zoom": 18},
            "need_mbps": 12.1, "capacity_mbps": 18.0, "ratio": 1.49,
            "settings": {
                "autoortho.prefetch_radius_nm": {
                    "planned": 15, "settled": 25, "moved": True, "pinned": False,
                    "reason": "corridor"},
                "streaming.band_max_wait_seconds": {
                    "planned": 12, "settled": 12, "moved": False, "pinned": False,
                    "reason": "texture time"},
            },
        },
    })
    text = "\n".join(lines)
    assert "### Tuning plan" in text
    assert "target fixed ZL17 (airports ZL18); needs about 12.1 Mbit/s; " \
           "line 18 Mbit/s (x1.49 the need)" in text
    assert "| prefetch_radius_nm | 15 | **25** | corridor |" in text
    assert "| band_max_wait_seconds | 12 | 12 | texture time |" in text
    assert diagnostics._render_plan({}) == []

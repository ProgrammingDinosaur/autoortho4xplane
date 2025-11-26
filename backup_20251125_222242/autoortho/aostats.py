import os
import time
import threading
import collections
from collections.abc import MutableMapping
from multiprocessing.managers import BaseManager
import psutil


from aoconfig import CFG
import logging
log = logging.getLogger(__name__)


STATS = {}
_local_lock = threading.RLock()
_local_stats = {}

_manager = None
_store = None


def _connect_manager_from_env():
    addr = os.getenv("AO_STATS_ADDR")
    auth = os.getenv("AO_STATS_AUTH")
    if not addr or not auth:
        return None, None
    host, port = addr.split(":")

    class _StatsManager(BaseManager):
        pass
    _StatsManager.register(
        'get_store',
        exposed=['inc', 'inc_many', 'set', 'get', 'delete', 'keys', 'snapshot']
    )
    m = _StatsManager(address=(host, int(port)), authkey=auth.encode('utf-8'))
    m.connect()
    return m, m.get_store()


def bind_local_store(store):
    """
    In the parent process, bind the module to an already-created StatsStore
    instance (not a proxy). All helpers (get/set/inc) and STATS[...] will
    use this store from now on.
    """
    global _store, STATS
    _store = store
    STATS = _StatsMapping(_store)
    log.info("aostats: bound to shared StatsStore")


try:
    _manager, _store = _connect_manager_from_env()
except Exception as e:
    _manager, _store = None, None
    log.debug(f"aostats: manager connect failed: {e}")


class _StatsMapping(MutableMapping):
    def __init__(self, remote_store=None):
        self._remote = remote_store

    def __getitem__(self, key):
        if self._remote:
            val = self._remote.get(key, None)
            if val is None: raise KeyError(key)
            return val
        with _local_lock:
            return _local_stats[key]

    def __setitem__(self, key, value):
        if self._remote:
            self._remote.set(key, value)
            return
        with _local_lock:
            _local_stats[key] = value

    def __delitem__(self, key):
        if self._remote:
            self._remote.delete(key)
            return
        with _local_lock:
            del _local_stats[key]

    def __iter__(self):
        if self._remote:
            # Remote has no native iter; snapshot for iteration
            return iter(self._remote.snapshot().keys())
        with _local_lock:
            return iter(dict(_local_stats).keys())

    def __len__(self):
        if self._remote:
            return len(self._remote.keys())
        with _local_lock:
            return len(_local_stats)

    # Provide dict-like helpers
    def get(self, key, default=None):
        if self._remote:
            val = self._remote.get(key, None)
            return default if val is None else val
        with _local_lock:
            return _local_stats.get(key, default)

    def items(self):
        if self._remote:
            return self._remote.snapshot().items()
        with _local_lock:
            return dict(_local_stats).items()


STATS = _StatsMapping(_store)


def set_stat(stat, value):
    if _store:
        _store.set(stat, value)
    else:
        with _local_lock:
            _local_stats[stat] = value


def get_stat(stat):
    if _store:
        return _store.get(stat, 0)
    with _local_lock:
        return _local_stats.get(stat, 0)


def inc_stat(stat, amount=1):
    if _store:
        return _store.inc(stat, amount)  # atomic on the shared store
    with _local_lock:
        _local_stats[stat] = _local_stats.get(stat, 0) + amount
        return _local_stats[stat]


def inc_many(items: dict):
    """Batch increment for workers; falls back to local dict when no manager."""
    if _store and hasattr(_store, "inc_many"):
        _store.inc_many(items)
    else:
        with _local_lock:
            for k, v in items.items():
                _local_stats[k] = _local_stats.get(k, 0) + int(v)


def delete_stat(stat: str):
    """Delete a stat from the shared store or local fallback."""
    if _store:
        try:
            _store.delete(stat)
        except Exception:
            pass
    else:
        with _local_lock:
            _local_stats.pop(stat, None)


def update_process_memory_stat():
    """Update this process's RSS and heartbeat in the shared stats store.

    Writes two keys:
      - proc_mem_rss_bytes:<pid> = RSS in bytes
      - proc_alive_ts:<pid> = unix timestamp of last heartbeat
    """
    try:
        pid = os.getpid()
        rss = psutil.Process(pid).memory_info().rss
        now_ts = int(time.time())
        set_stat(f"proc_mem_rss_bytes:{pid}", int(rss))
        set_stat(f"proc_alive_ts:{pid}", now_ts)
    except Exception as _err:
        # Best-effort; ignore failures
        log.debug(f"update_process_memory_stat: {_err}")


def clear_process_memory_stat():
    """Remove this process's memory/heartbeat keys from the stats store."""
    pid = os.getpid()
    delete_stat(f"proc_mem_rss_bytes:{pid}")
    delete_stat(f"proc_alive_ts:{pid}")


class AOStats(object):
    def __init__(self):
        self.running = False
        self._t = threading.Thread(daemon=True, target=self.show)

    def start(self):
        self.running = True
        self._t.start()

    def stop(self):
        self.running = False
        self._t.join()

    def show(self):
        while self.running:
            time.sleep(10)
            try:
                snap = _store.snapshot() if _store else dict(STATS.items())
                log.info(f"STATS: {snap}")
            except Exception as e:
                log.debug(f"aostats.show error: {e}")


class StatTracker(object):

    def __init__(self, start=None, end=None, default=-1, maxlen=None):
        self.fetch_times = {}
        self.averages = {}
        self.counts = {}
        self.maxlen = 25

        if maxlen:
            self.maxlen = maxlen

        if start is not None and end is not None:
            if end < start:
                inc = -1
            else:
                inc = 1

            for i in range(start, end, inc):
                self.averages[i] = default
                self.counts[i] = default

    def set(self, key, value):
        self.counts[key] = self.counts.get(key, 0) + 1
        self.fetch_times.setdefault(key, collections.deque(maxlen=self.maxlen)).append(value)
        self.averages[key] = round(sum(self.fetch_times.get(key))/len(self.fetch_times.get(key)), 3)


class StatsStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = {}  # dict[str, any]

    # Atomic increment
    def inc(self, key, amount=1):
        with self._lock:
            self._data[key] = self._data.get(key, 0) + amount
            return self._data[key]

    def inc_many(self, items):
        # items: dict[str, int]
        with self._lock:
            for k, a in items.items():
                self._data[k] = self._data.get(k, 0) + a

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def get(self, key, default=0):
        with self._lock:
            return self._data.get(key, default)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def keys(self):
        with self._lock:
            return list(self._data.keys())

    def snapshot(self):
        with self._lock:
            return dict(self._data)


class StatsBatcher:
    def __init__(self, flush_interval=0.05, max_items=200):
        self._buf = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._flush_interval = flush_interval
        self._max_items = max_items
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def add(self, key: str, amount: int = 1):
        with self._lock:
            self._buf[key] = self._buf.get(key, 0) + int(amount)
            if len(self._buf) >= self._max_items:
                self._flush_unlocked()

    def add_many(self, items: dict):
        with self._lock:
            for k, v in items.items():
                self._buf[k] = self._buf.get(k, 0) + int(v)
            if len(self._buf) >= self._max_items:
                self._flush_unlocked()

    def flush(self):
        with self._lock:
            self._flush_unlocked()

    def stop(self):
        self._stop.set()
        self.flush()

    def _flush_unlocked(self):
        if not self._buf:
            return
        payload, self._buf = self._buf, {}
        try:
            inc_many(payload)
        except Exception:
            # As a safety net, fall back to single increments.
            for k, v in payload.items():
                inc_stat(k, v)

    def _loop(self):
        while not self._stop.wait(self._flush_interval):
            self.flush()

"""Coalesce overlapping observation reads only; never cache mutation guards."""
import copy
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout


class ReadDeferred(RuntimeError):
    pass


class ListReads:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.failures = {}
        self.joined = 0
        self.deferred = 0

    def read(self, key, fetch, deadline):
        with self.lock:
            future = self.active.get(key)
            owner = future is None
            if owner:
                count, until = self.failures.get(key, (0, 0))
                if time.monotonic() < until:
                    self.deferred += 1
                    raise ReadDeferred("卡片列表读取退避中，稍后重新读取")
                future = Future()
                self.active[key] = future
            else:
                self.joined += 1
        if not owner:
            try:
                return copy.deepcopy(future.result(timeout=max(0, deadline - time.monotonic())))
            except FutureTimeout as exc:
                raise ReadDeferred("等待共享列表超过本轮预算") from exc
        try:
            result = fetch()
        except Exception as exc:
            with self.lock:
                count = self.failures.get(key, (0, 0))[0] + 1
                self.failures[key] = (count, time.monotonic() + (1, 3, 10)[min(count - 1, 2)])
                future.set_exception(exc)
                self.active.pop(key, None)
            raise
        else:
            with self.lock:
                self.failures.pop(key, None)
                future.set_result(copy.deepcopy(result))
                self.active.pop(key, None)
            return result

    def snapshot(self):
        with self.lock:
            return {"inflight": len(self.active), "joined": self.joined, "deferred": self.deferred,
                    "coolingDown": sum(until > time.monotonic() for _, until in self.failures.values()),
                    "mutationGuardsAlwaysFresh": True}


list_reads = ListReads()

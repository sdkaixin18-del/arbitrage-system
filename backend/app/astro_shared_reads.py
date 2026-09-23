"""Coalesce concurrent read-only lookups without caching completed results."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from copy import deepcopy
from threading import Lock
from typing import Any, Callable


class InflightReads:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: dict[tuple[str, ...], Future] = {}
        self._metrics: dict[str, dict[str, int]] = {}

    def read(self, key: tuple[str, ...], fetch: Callable[[], Any], *, timeout: float) -> Any:
        # The key includes data kind, transport, exchange and exact symbol.
        # No worker is spawned: the first caller owns the existing API call.
        with self._lock:
            future = self._pending.get(key)
            owner = future is None
            metrics = self._metrics.setdefault(key[0], {"reads": 0, "joined": 0, "errors": 0})
            if owner:
                future = Future()
                self._pending[key] = future
                metrics["reads"] += 1
            else:
                metrics["joined"] += 1
        if owner:
            try:
                result = fetch()
            except BaseException as exc:
                # Release all followers even if the owner is interrupted.
                with self._lock:
                    metrics["errors"] += 1
                    self._pending.pop(key, None)
                    future.set_exception(exc)
                raise
            else:
                with self._lock:
                    self._pending.pop(key, None)
                    future.set_result(result)
                return deepcopy(result)
        try:
            return deepcopy(future.result(timeout=timeout))
        except FutureTimeoutError as exc:
            # An owner's TimeoutError is its actual request failure. A wait
            # timeout must not cancel/remove another route's running read.
            if future.done():
                raise
            raise RuntimeError(f"共享{key[0]}请求等待超时 (timed out)") from exc

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "inflightCount": len(self._pending),
                "completedResultCacheSeconds": 0,
                "byKind": deepcopy(self._metrics),
            }

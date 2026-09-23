from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest

from app.astro_shared_reads import InflightReads


def wait_for_join(reads, kind):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if reads.snapshot()["byKind"].get(kind, {}).get("joined", 0):
            return
        Event().wait(0.001)
    pytest.fail("second read did not join")


@pytest.mark.parametrize("fail", [False, True])
def test_concurrent_identical_reads_share_success_or_failure_and_release(fail):
    reads = InflightReads()
    entered, release = Event(), Event()
    calls = []
    key = ("funding", "local_proxy", "gate", "ABC")

    def fetch():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        if fail:
            raise ValueError("upstream unavailable")
        return {"nested": {"value": 1}}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reads.read, key, fetch, timeout=2)
        assert entered.wait(2)
        second = pool.submit(reads.read, key, fetch, timeout=2)
        try:
            wait_for_join(reads, "funding")
        finally:
            release.set()
        if fail:
            for result in (first, second):
                with pytest.raises(ValueError, match="upstream unavailable"):
                    result.result(2)
        else:
            left, right = first.result(2), second.result(2)
            left["nested"]["value"] = 9
            assert right["nested"]["value"] == 1
    assert calls == [1]
    assert reads.snapshot()["inflightCount"] == 0
    assert reads.read(key, lambda: {"value": 2}, timeout=1) == {"value": 2}
    assert reads.snapshot()["byKind"]["funding"]["reads"] == 2


@pytest.mark.parametrize("key", [
    ("funding", "tencent_cloud", "gate", "ABC"),
    ("funding", "local_proxy", "bybit", "ABC"),
    ("funding", "local_proxy", "gate", "XYZ"),
    ("contract_multiplier", "local_proxy", "gate", "ABC"),
])
def test_transport_exchange_symbol_and_data_kind_never_share(key):
    reads = InflightReads()
    entered, release = Event(), Event()

    def fetch():
        entered.set()
        assert release.wait(2)
        return "first"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reads.read, ("funding", "local_proxy", "gate", "ABC"), fetch, timeout=1)
        assert entered.wait(2)
        try:
            assert reads.read(key, lambda: "independent", timeout=0.01) == "independent"
        finally:
            release.set()
        assert future.result(2) == "first"


def test_wait_timeout_does_not_cancel_running_read():
    reads = InflightReads()
    entered, release = Event(), Event()
    key = ("funding", "local_proxy", "gate", "ABC")

    def fetch():
        entered.set()
        assert release.wait(2)
        return 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reads.read, key, fetch, timeout=1)
        assert entered.wait(2)
        try:
            with pytest.raises(RuntimeError, match="共享funding请求等待超时"):
                reads.read(key, lambda: pytest.fail("duplicate request"), timeout=0.001)
            assert reads.snapshot()["inflightCount"] == 1
        finally:
            release.set()
        assert future.result(2) == 1
    assert reads.snapshot()["inflightCount"] == 0

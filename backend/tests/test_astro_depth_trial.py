from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import astro_depth_trial as trial


def request(symbol="BTCUSDT", request_id="abcdefghijklmnop"):
    return trial.TrialRequest(requestId=request_id, url="https://api.binance.com/api/v3/depth",
                              params={"symbol": symbol, "limit": 5})


def result(_):
    now = int(time.time() * 1000)
    return {"receivedAtMs": now, "requestStartedAtMs": now, "statusCode": 200,
            "payload": {"bids": [["1", "2"]], "asks": [["2", "3"]]}}


def test_concurrent_paths_fetch_once_and_preserve_timestamps():
    entered, release = threading.Event(), threading.Event()
    def fetch(req):
        entered.set()
        assert release.wait(2)
        return result(req)
    flight = trial.SingleFlight(fetch)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(flight.run, request())
        assert entered.wait(1)
        b = pool.submit(flight.run, request())
        release.set()
        assert a.result() == b.result()
    assert flight.metrics["upstreamCalls"] == 1
    assert flight.metrics["duplicates"] == 1


def test_new_round_fetches_again_and_conflict_rejected():
    flight = trial.SingleFlight(result)
    flight.run(request())
    with pytest.raises(HTTPException) as error:
        flight.run(request("ETHUSDT"))
    assert error.value.status_code == 409
    flight.run(request(request_id="another-round-12345"))
    assert flight.metrics["upstreamCalls"] == 2


def test_expired_duplicate_does_not_refetch():
    def old(req):
        return {**result(req), "receivedAtMs": int(time.time() * 1000) - 4000}
    flight = trial.SingleFlight(old)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            flight.run(request())
        assert error.value.status_code == 410
    assert flight.metrics["upstreamCalls"] == 1


def test_failure_shared_and_capacity_bounded():
    def fail(_):
        raise HTTPException(429, "rate limited", headers={"Retry-After": "5"})
    flight = trial.SingleFlight(fail, capacity=1)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            flight.run(request())
        assert error.value.headers == {"Retry-After": "5"}
    with pytest.raises(HTTPException) as error:
        flight.run(request(request_id="another-round-12345"))
    assert error.value.status_code == 429
    assert flight.metrics["upstreamCalls"] == 1


def test_auth_and_only_readonly_endpoints(monkeypatch):
    monkeypatch.setenv("ASTRO_DEPTH_TRIAL_TOKEN", "x" * 40)
    client = TestClient(trial.app)
    assert client.get("/health").status_code == 401
    auth = {"Authorization": "Bearer " + "x" * 40}
    assert client.get("/health", headers=auth).json()["readOnly"]
    for path in ("/dex-quote", "/dex-coins", "/orders", "/docs"):
        assert client.get(path, headers=auth).status_code == 404


def test_private_target_rejected_before_upstream():
    flight = trial.SingleFlight(result)
    req = request().model_copy(update={"url": "http://127.0.0.1/admin"})
    with pytest.raises(HTTPException):
        flight.run(req)
    assert flight.metrics["upstreamCalls"] == 0


def test_concurrency_limit_does_not_start_extra_upstream():
    entered, release = threading.Event(), threading.Event()
    def fetch(req):
        entered.set()
        assert release.wait(2)
        return result(req)
    flight = trial.SingleFlight(fetch, concurrency=1)
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(flight.run, request())
        assert entered.wait(1)
        with pytest.raises(HTTPException) as error:
            flight.run(request(request_id="another-round-12345"))
        assert error.value.status_code == 429
        release.set()
        owner.result()
    assert flight.metrics["upstreamCalls"] == 1


def test_trial_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("ASTRO_DEPTH_TRIAL_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="strong token"):
        with TestClient(trial.app):
            pass


def test_unexpected_failure_shared_without_sensitive_details():
    def fail(_):
        raise RuntimeError("sensitive internal detail")
    flight = trial.SingleFlight(fail)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            flight.run(request())
        assert error.value.status_code == 502
        assert error.value.detail == "Trial upstream failure"
    assert flight.metrics["upstreamCalls"] == 1

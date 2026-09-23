import threading
import time

import httpx
import pytest

from app import crypto
from app import astro_spread_scanner as scanner


@pytest.fixture(autouse=True)
def isolated_evidence_state(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_REVIEW_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_api_rate_limit_next_at", {})
    monkeypatch.setattr(crypto, "_api_rate_limit_backoff_until", {})
    monkeypatch.setattr(scanner, "_depth_request_cache", {})
    monkeypatch.setattr(scanner, "_depth_request_errors", {})
    monkeypatch.setattr(scanner, "_depth_request_inflight", set())
    monkeypatch.setattr(scanner, "append_system_runtime_event", lambda *args, **kwargs: {})


def test_nested_deadlines_only_shorten_and_restore():
    assert crypto.api_request_remaining_seconds() is None
    with crypto.api_request_deadline(timeout_seconds=1):
        first = crypto.api_request_remaining_seconds()
        with crypto.api_request_deadline(timeout_seconds=5):
            assert crypto.api_request_remaining_seconds() <= first
        with crypto.api_request_deadline(timeout_seconds=0.02):
            assert 0 < crypto.api_request_remaining_seconds() <= 0.02
        assert crypto.api_request_remaining_seconds() > 0.5
    assert crypto.api_request_remaining_seconds() is None


def test_normal_wait_deadline_expires_under_continuous_hot_activity():
    errors = []

    def waiting_worker():
        try:
            with crypto.api_request_deadline(timeout_seconds=0.03):
                crypto.wait_api_rate_limit("test:blocked-evidence")
        except Exception as exc:
            errors.append(exc)

    with crypto.api_request_priority("astro"):
        worker = threading.Thread(target=waiting_worker)
        started = time.monotonic()
        worker.start()
        worker.join(0.4)
    assert not worker.is_alive()
    assert time.monotonic() - started < 0.3
    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)


def test_evidence_lane_remains_normal_priority_and_runs_during_hot_activity():
    grants, errors = [], []

    def evidence_worker():
        try:
            with crypto.api_rate_limit_lane("astro_evidence"), crypto.api_request_deadline(timeout_seconds=0.1):
                crypto.wait_api_rate_limit("test:evidence-progress")
                grants.append(getattr(crypto._api_priority_local, "level", "normal"))
        except Exception as exc:
            errors.append(exc)

    with crypto.api_request_priority("astro"):
        worker = threading.Thread(target=evidence_worker)
        worker.start()
        worker.join(0.3)
    assert not worker.is_alive() and not errors
    assert grants == ["normal"]


def test_sixty_second_exchange_backoff_cannot_consume_more_than_budget():
    with crypto.api_rate_limit_lane("astro_evidence"):
        crypto.defer_api_rate_limit("test:long-backoff", 60)
        started = time.monotonic()
        with pytest.raises(TimeoutError), crypto.api_request_deadline(timeout_seconds=0.03):
            crypto.wait_api_rate_limit("test:long-backoff")
    assert time.monotonic() - started < 0.3


def test_http_phase_timeouts_fit_budget_and_429_does_not_sleep_thirty_seconds():
    calls = []

    class Client:
        timeout = httpx.Timeout(12)

        def get(self, url, **kwargs):
            calls.append(kwargs)
            return httpx.Response(429, headers={"Retry-After": "30"})

    started = time.monotonic()
    with crypto.api_rate_limit_lane("astro_evidence"), crypto.api_request_deadline(timeout_seconds=0.08):
        with pytest.raises(TimeoutError, match="backoff"):
            crypto.rate_limited_get(Client(), "https://example.test", bucket="test:429")
    assert time.monotonic() - started < 0.3
    assert len(calls) == 1
    assert all(0 < value <= 0.02 for value in calls[0]["timeout"].as_dict().values())


def test_late_http_result_is_not_accepted_after_budget():
    class Client:
        timeout = httpx.Timeout(12)

        def get(self, url, **kwargs):
            time.sleep(0.03)
            return httpx.Response(200)

    with crypto.api_rate_limit_lane("astro_evidence"), crypto.api_request_deadline(timeout_seconds=0.02):
        with pytest.raises(TimeoutError):
            crypto.rate_limited_get(Client(), "https://example.test", bucket="test:late-result")


def test_shared_depth_join_respects_evidence_budget(monkeypatch):
    url = "https://fapi.binance.com/fapi/v1/depth"
    params = {"symbol": "WOOUSDT", "limit": 20}
    key = (url, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    scanner._depth_request_inflight.add(key)
    monkeypatch.setattr(scanner, "_scanner_public_get", lambda *args, **kwargs: pytest.fail("must only join existing work"))
    started = time.monotonic()
    with httpx.Client() as client, crypto.api_request_deadline(timeout_seconds=0.03):
        with pytest.raises((TimeoutError, RuntimeError)):
            scanner._shared_depth_json_get(client, url, params=params)
    assert time.monotonic() - started < 0.4







from __future__ import annotations

import time
import threading
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import astro_spread_scanner as scanner
from app import astro_depth_cloud as cloud
from app import astro_depth_transport as transport
from app import system_runtime_log

PAIR = {"name": "ICX", "type": "FF", "buyEx": "bybit", "sellEx": "gate"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from app.astro_route_policy import RoutePolicy, LatestQueue
    monkeypatch.setattr(scanner, "_route_policy", RoutePolicy())
    monkeypatch.setattr(scanner, "_cloud_route_queue", LatestQueue())
    monkeypatch.setenv("ASTRO_DEPTH_CLOUD_ENABLED", "1")
    monkeypatch.setattr(scanner, "_notify_api_degraded_fault", lambda **kw: {})
    monkeypatch.setattr(scanner, "append_system_runtime_event", lambda *a, **kw: {})
    monkeypatch.setattr(scanner, "_api_transition_log_windows", {})
    monkeypatch.setattr(scanner, "_local_depth_deadline_seconds", lambda: 0.08)
    scanner._stop.clear()
    scanner._depth_backup_sources.clear()
    scanner._depth_probe_targets.clear()
    scanner._api_degraded_incidents.clear()
    scanner._api_recovery_probe_state.clear()
    scanner._depth_exchange_health.clear()
    scanner._depth_request_cache.clear()
    scanner._depth_request_errors.clear()
    yield
    scanner._cloud_route_queue.stop()
    executor, scanner._local_depth_deadline_executor = scanner._local_depth_deadline_executor, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    scanner._depth_backup_sources.clear()
    scanner._api_degraded_incidents.clear()


@pytest.mark.parametrize("url,params", [
    ("http://127.0.0.1:8000/api/manual-orders/status", {}),
    ("https://fapi.binance.com/fapi/v1/order", {}),
    ("https://fapi.binance.com@evil.example/fapi/v1/depth", {}),
    ("https://fapi.binance.com/fapi/v1/depth", {"limit": 100}),
    ("https://fapi.binance.com/fapi/v1/depth", {"signature": "abc"}),
    ("https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT", {}),
    ("https://api.gateio.ws/api/v4/futures/usdt/contracts/../accounts", {}),
])
def test_cloud_relay_rejects_private_or_arbitrary_targets(url, params):
    with pytest.raises(HTTPException):
        cloud.validate_target(cloud.PublicGet(url=url, params=params))


def test_cloud_accepts_native_chinese_symbols():
    assert cloud.validate_target(cloud.PublicGet(url="https://api.gateio.ws/api/v4/futures/usdt/contracts/币安人生_USDT")) == "api.gateio.ws"


def test_first_local_transport_failure_uses_cloud_and_next_round_stays_cloud(monkeypatch):
    calls = []
    def observe(pair, config, *, executable_depth_first, use_cloud=False):
        calls.append(use_cloud)
        if not use_cloud:
            return None, {"reason": "cex_executable_depth_unavailable", "error": "gate SSL handshake timed out"}
        return dict(pair), {"reason": "eligible", "latestOpenSpreadPct": 2.0}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    result, report = scanner._fetch_direct_route_once(PAIR, None, executable_depth_first=True)
    assert calls == [False, True]
    assert report["transport"] == "tencent_cloud"
    assert result["_depthTransport"] == "tencent_cloud"
    assert "gate" in scanner._depth_backup_sources
    scanner._fetch_direct_route_once(result, None, executable_depth_first=True)
    assert calls == [False, True, True]
    assert "gate" in scanner._api_degraded_incidents  # Cloud success is not local recovery.


@pytest.mark.parametrize("reason,error", [
    ("spread_below_threshold", ""), ("direct_quote_time_skew", ""),
    ("cex_executable_depth_unavailable", "depth not sufficient"),
    ("cex_executable_depth_unavailable", "HTTP 429 Too Many Requests"),
])
def test_semantic_failure_never_shops_for_a_better_cloud_quote(monkeypatch, reason, error):
    calls = []
    def observe(pair, config, **kwargs):
        calls.append(kwargs)
        return None, {"reason": reason, "error": error}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    assert scanner._fetch_direct_route_once(PAIR, None)[0] is None
    assert len(calls) == 1
    assert scanner._depth_backup_sources == {}


def test_both_transports_unavailable_never_return_a_card(monkeypatch):
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", lambda *a, **kw: (None, {"reason": "direct_quote_unavailable", "error": "gate timeout"}))
    result, report = scanner._fetch_direct_route_once(PAIR, None)
    assert result is None
    assert report["unverifiedCardAllowed"] is False
    assert scanner.astro_spread_pair_submit_guard({**PAIR, "_apiDegradedEvidence": {"eligible": True}})[0] is False
    assert scanner.revalidate_astro_api_degraded_pair(PAIR, None)[0] is None


def test_recovery_requires_new_generation_two_books_and_minimum_dwell(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(scanner.time, "monotonic", lambda: now[0])
    scanner._activate_cloud_backup(PAIR, {"error": "gate timeout"})
    generation = scanner._api_recovery_probe_state["gate"]["generation"]
    args = {"success": True, "error": None, "duration_ms": 10, "generation": generation}
    assert not scanner._record_api_recovery_probe_result("gate", **args)
    assert not scanner._record_api_recovery_probe_result("gate", **args)
    assert "gate" in scanner._depth_backup_sources
    for value in (102,104,106,108):
        now[0]=value
        assert not scanner._record_api_recovery_probe_result("gate", **args)
    now[0] = 110
    assert scanner._record_api_recovery_probe_result("gate", **args)
    assert "gate" not in scanner._depth_backup_sources
    scanner._activate_cloud_backup(PAIR, {"error": "gate timeout"})
    assert not scanner._record_api_recovery_probe_result("gate", **args)  # previous incident response


@pytest.mark.parametrize("payload", [
    {}, {"server_time": 1788567000000}, {"label": "TOO_MANY_REQUESTS"},
    {"bids": [], "asks": [[1, 1]]}, {"bids": [[2, 1]], "asks": [[1, 1]]},
    {"bids": [[1, 0]], "asks": [[2, 1]]},
    {"bids": [[1, 1]], "asks": [[2, 1]], "update": 1},
])
def test_ping_empty_bad_or_stale_books_do_not_confirm_recovery(payload):
    with pytest.raises((ValueError, RuntimeError)):
        scanner._validate_depth_probe_payload("gate", payload)


def test_actual_fresh_book_confirms_probe_payload():
    scanner._validate_depth_probe_payload("gate", {"bids": [{"p": "1", "s": 3}], "asks": [{"p": "1.01", "s": 2}], "update": time.time()})


@pytest.mark.parametrize("scale", [1, 1000])
def test_gate_probe_uses_fresh_snapshot_even_when_book_has_not_changed(scale):
    now = time.time()
    payload = {"bids": [["1", "3"]], "asks": [["1.01", "2"]],
               "current": now * scale, "update": (now - 30) * scale}
    scanner._validate_depth_probe_payload("gate", payload)
    payload["current"] = (now - 10) * scale
    with pytest.raises(ValueError, match="陈旧"):
        scanner._validate_depth_probe_payload("gate", payload)


@pytest.mark.parametrize("reason", ["stale_direct_quote", "direct_quote_time_skew"])
def test_cloud_quality_rejection_does_not_retry_local_or_count_as_api_failure(monkeypatch, reason):
    scanner._activate_cloud_backup(PAIR, {"error": "gate timeout"})
    before_failed = scanner._depth_cloud_metrics.get("failedObservations", 0)
    before_rejected = scanner._depth_cloud_metrics.get("qualityRejectedObservations", 0)
    calls = []
    def observe(pair, config, *, use_cloud=False, **kwargs):
        calls.append(use_cloud)
        assert use_cloud
        return None, {"reason": reason, "latestOpenSpreadPct": 4.0}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    monkeypatch.setattr(scanner, "_local_emergency_probe_ok", lambda _: pytest.fail("quality is not a failback trigger"))
    result, report = scanner._fetch_direct_route_once(PAIR, None)
    assert result is None and report["reason"] == reason
    assert calls == [True]
    assert scanner._depth_cloud_metrics.get("failedObservations", 0) == before_failed
    assert scanner._depth_cloud_metrics["qualityRejectedObservations"] == before_rejected + 1
    assert scanner._depth_cloud_metrics["lastError"] is None
    assert scanner._route_policy.routes["ICX|FF|BYBIT|GATE"]["alertable"] is False


def test_healthy_cloud_status_does_not_inherit_a_route_rejection(monkeypatch):
    monkeypatch.setattr(transport, "status", lambda: {"state": "ready", "enabled": True, "lastError": None})
    scanner._finish_cloud_observation(PAIR, None, {"reason": "stale_direct_quote"})
    cloud_status = scanner._api_degraded_status()["cloudBackup"]
    assert cloud_status["state"] == "ready"
    assert cloud_status["lastError"] is None
    assert cloud_status["lastObservationError"] == "stale_direct_quote"


def test_cloud_health_timeout_needs_two_failures_and_records_probe_details(monkeypatch):
    outcomes = iter(["timeout", "timeout", "ok"])
    events = []
    real_client = httpx.Client

    def respond(request):
        if next(outcomes) == "timeout":
            raise httpx.ReadTimeout("slow health response", request=request)
        return httpx.Response(200, json={"service": "astro-depth-cloud", "protocol": 1,
                                         "readOnly": True, "serverTimeMs": int(time.time() * 1000)})

    def client(**kwargs):
        assert kwargs["timeout"].read == 2.0
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(transport, "_start_tunnel", lambda: None)
    monkeypatch.setattr(transport.httpx, "Client", client)
    monkeypatch.setattr(transport, "_status", {"state": "ready", "lastError": None})
    monkeypatch.setattr(system_runtime_log, "append_system_runtime_event", lambda *args, **kw: events.append(args[0]))

    first = transport.maintain(force=True)
    assert first["state"] == "ready"
    assert first["lastProbeStage"] == "health"
    assert first["lastProbeErrorType"] == "ReadTimeout"
    assert first["consecutiveTimeouts"] == 1
    assert first["lastError"] is None
    assert events == []

    second = transport.maintain(force=True)
    assert second["state"] == "unavailable"
    assert second["consecutiveTimeouts"] == 2
    assert second["healthCheckFailures"] == 2
    assert events == ["astro_depth_cloud_unavailable"]

    recovered = transport.maintain(force=True)
    assert recovered["state"] == "ready"
    assert recovered["consecutiveTimeouts"] == 0
    assert recovered["lastProbeError"] is None
    assert recovered["lastHealthRttMs"] >= 0
    assert events == ["astro_depth_cloud_unavailable", "astro_depth_cloud_ready"]


def test_cloud_health_clock_failure_is_reported_immediately(monkeypatch):
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"service": "astro-depth-cloud", "protocol": 1, "readOnly": True,
                       "serverTimeMs": int(time.time() * 1000) - 1500})), **kwargs)

    monkeypatch.setattr(transport, "_start_tunnel", lambda: None)
    monkeypatch.setattr(transport.httpx, "Client", client)
    monkeypatch.setattr(transport, "_status", {"state": "ready", "lastError": None})
    monkeypatch.setattr(system_runtime_log, "append_system_runtime_event", lambda *args, **kw: None)
    state = transport.maintain(force=True)
    assert state["state"] == "unavailable"
    assert state["lastProbeStage"] == "clock"
    assert state["serverTimeDeltaMs"] > 1000


def test_quote_timing_failures_are_sampled_in_five_minute_summary(monkeypatch):
    events = []
    monkeypatch.setattr(scanner, "astro_route_dedupe_state", lambda _: None)
    monkeypatch.setattr(scanner, "append_system_runtime_event", lambda *args, **kw: events.append((args[0], kw)))
    monkeypatch.setattr(scanner, "_decision_audit_summary", {
        "startedAt": "2026-09-23T00:00:00+00:00", "lastLoggedAtMonotonic": 0.0,
        "count": 0, "reasons": {}, "samples": [], "quoteTimingSamples": [],
        "durationCount": 0, "durationMsTotal": 0.0, "durationMsMax": 0.0, "routeStats": {},
    })
    scanner._append_decision_audit(
        ("ICX", "FF", "bybit", "gate", "", ""), stage="hot_direct_check",
        decision="direct_quote_time_skew",
        details={"pulseOpenSpreadPct": 2.0, "report": {
            "transport": "tencent_cloud", "quoteSkewSeconds": 1.4,
            "quoteAgeLimitsSeconds": {"buy": 3.0, "sell": 3.0},
            "buyQuote": {"quoteAgeSeconds": 0.3, "timestampSource": "exchange_depth_update", "requestDurationMs": 240},
            "sellQuote": {"quoteAgeSeconds": 1.7, "timestampSource": "response_received_depth", "requestDurationMs": 510},
        }},
    )
    summary = next(kw["details"] for name, kw in events if name == "astro_auto_card_decision_summary")
    sample = summary["quoteTimingSamples"][0]
    assert sample["decision"] == "direct_quote_time_skew"
    assert sample["quoteSkewSeconds"] == 1.4
    assert sample["buyTimestampSource"] == "exchange_depth_update"
    assert sample["sellQuoteAgeSeconds"] == 1.7


def test_local_and_cloud_depth_caches_never_mix_and_rounds_are_independent(monkeypatch):
    monkeypatch.setattr(scanner, "_before_depth_request", lambda _: False)
    monkeypatch.setattr(scanner, "_record_depth_request_success", lambda *a, **kw: None)
    count = [0]
    def get(client, url, *, params):
        count[0] += 1
        return httpx.Response(200, json={"sample": count[0]}, request=httpx.Request("GET", url))
    monkeypatch.setattr(scanner, "_scanner_public_get", get)
    local = type("Local", (), {"cloud_depth": False})()
    remote = type("Cloud", (), {"cloud_depth": True})()
    url = "https://fapi.binance.com/fapi/v1/depth"
    assert scanner._shared_depth_json_get(local, url, params={"symbol": "BTCUSDT", "limit": 20})[0]["sample"] == 1
    assert scanner._shared_depth_json_get(remote, url, params={"symbol": "BTCUSDT", "limit": 20})[0]["sample"] == 2
    remote.depth_round_started_ms = int(time.time() * 1000) + 1
    assert scanner._shared_depth_json_get(remote, url, params={"symbol": "BTCUSDT", "limit": 20})[0]["sample"] == 3


def test_cloud_client_is_not_allowed_to_consume_local_queue(monkeypatch):
    from app import crypto
    monkeypatch.setattr(crypto, "wait_api_rate_limit", lambda *a: pytest.fail("shared local queue was used"))
    client = type("Cloud", (), {"cloud_depth": True, "get": lambda self, *a, **kw: "ok"})()
    assert scanner._scanner_public_get(client, "https://fapi.binance.com/fapi/v1/depth") == "ok"


def test_cloud_http_surface_has_no_trading_routes():
    client = TestClient(cloud.app)
    assert client.get("/health").json()["readOnly"] is True
    assert client.post("/api/manual-orders/kstr/batches", json={}).status_code == 404
    assert client.post("/v1/public-get", json={"url": "https://fapi.binance.com/fapi/v1/order"}).status_code == 400


@pytest.mark.parametrize("second_passes", [True, False])
def test_mid_confirmation_failover_needs_two_new_cloud_checks(monkeypatch, second_passes):
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(scanner, "_record_revalidation_outcome", lambda *a, **kw: None)
    pair = {**PAIR, "_hotDirectHit": {"verifiedAtMs": int(time.time()*1000), "report": {"transport": "local_proxy", "reason": "eligible"}}}
    calls = []
    def observe(pair, config, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1 or second_passes:
            return {**PAIR, "_depthTransport": "tencent_cloud"}, {"transport": "tencent_cloud", "reason": "eligible"}
        return None, {"transport": "tencent_cloud", "reason": "below_threshold_or_rule_failed"}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once", observe)
    result, report = scanner.revalidate_astro_hot_direct_hit(pair, None)
    assert len(calls) == 2
    assert (result is not None) == second_passes
    assert all(check["transport"] == "tencent_cloud" for check in report["checks"])
    assert report["roundsPassed"] == (2 if second_passes else 1)


@pytest.mark.parametrize("age_ms", [4000, -2000])
def test_cloud_transport_rejects_stale_or_future_envelope(monkeypatch, age_ms):
    monkeypatch.setattr(transport, "_start_tunnel", lambda: None)
    url = "https://fapi.binance.com/fapi/v1/depth"
    with transport.CloudPublicClient() as client:
        client.client.close()
        client.client = httpx.Client(base_url=transport.origin(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"service": "astro-depth-cloud", "protocol": 1, "url": url, "receivedAtMs": int(time.time()*1000)-age_ms}, request=request)))
        with pytest.raises(RuntimeError, match="陈旧|时钟"):
            client.get(url, params={"symbol": "BTCUSDT", "limit": 20})


def test_local_deadline_selects_cloud_without_waiting_for_late_local_result(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    calls = []
    def observe(pair, config, *, use_cloud=False, **kwargs):
        calls.append((use_cloud, time.monotonic()))
        if not use_cloud:
            release.wait(2)
            finished.set()
            return {**pair, "lateLocal": True}, {"reason": "eligible"}
        return dict(pair), {"reason": "eligible"}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    started = time.monotonic()
    try:
        result, report = scanner._fetch_direct_route_once(PAIR, None)
        elapsed = time.monotonic() - started
        assert 0.075 <= elapsed < 0.5
        assert result["_depthTransport"] == "tencent_cloud"
        assert "lateLocal" not in result
        assert report["localFailure"]["reason"] == "local_depth_deadline_exceeded"
        assert calls[1][0] is True
        assert not finished.is_set()
    finally:
        release.set()
    assert finished.wait(1)
    assert "lateLocal" not in result


def test_early_transport_failure_waits_for_elapsed_deadline_and_local_push_is_absent(monkeypatch):
    pushes, calls = [], []
    monkeypatch.setattr(scanner, "_notify_api_degraded_fault", lambda **kw: pushes.append(kw))
    def observe(pair, config, *, use_cloud=False, **kwargs):
        calls.append((use_cloud, time.monotonic()))
        if not use_cloud:
            return None, {"reason": "direct_quote_unavailable", "error": "gate connection refused"}
        return dict(pair), {"reason": "eligible"}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    started = time.monotonic()
    result, _ = scanner._fetch_direct_route_once(PAIR, None)
    assert result is not None
    assert calls[1][1] - started >= 0.075
    assert len(calls) == 2  # No failure-count retry loop.
    assert pushes == []


@pytest.mark.parametrize("reason,error,expected_push", [
    ("direct_quote_unavailable", "SSL timeout", True),
    ("direct_quote_unavailable", "HTTP 429", True),
    ("stale_direct_quote", "", True),
    ("direct_quote_time_skew", "", True),
    ("cloud_depth_busy", "", True),
    ("open_spread_non_positive", "", False),
    ("below_threshold_or_rule_failed", "", False),
    ("sf_negative_short_funding", "", False),
    ("cex_executable_depth_unavailable", "买入腿卖盘深度不足：缺少 5 USDT", False),
])
def test_only_unavailable_cloud_verification_notifies(monkeypatch, reason, error, expected_push):
    pushes = []
    monkeypatch.setattr(scanner, "_notify_api_degraded_fault", lambda **kw: pushes.append(kw))
    scanner._finish_cloud_observation(PAIR, None, {"reason": reason, "error": error})
    assert pushes == []  # First observation never notifies immediately.
    row=scanner._route_policy.snapshot()["routes"][0]
    assert (row["mode"] == "paused") is expected_push


def test_local_semantic_rejection_does_not_wait_or_switch(monkeypatch):
    calls=[]
    def observe(*args, **kw):
        calls.append(kw)
        return None, {"reason": "open_spread_non_positive"}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    started=time.monotonic()
    result, report=scanner._fetch_direct_route_once(PAIR, None)
    assert time.monotonic()-started < 0.07
    assert result is None and report["reason"] == "open_spread_non_positive"
    assert len(calls) == 1


def test_status_exposes_local_mute_and_time_based_failover():
    status=scanner._api_degraded_status()
    assert status["localFaultPushEnabled"] is False
    assert status["cloudFaultPushEnabled"] is True
    assert status["switchPolicy"] == "elapsed_time"


def test_real_three_second_deadline_does_not_wait_for_blocked_worker(monkeypatch):
    monkeypatch.setattr(scanner, "_local_depth_deadline_seconds", lambda: 3.0)
    release = threading.Event()
    def observe(pair, config, *, use_cloud=False, **kwargs):
        if not use_cloud:
            release.wait(6)
        return dict(pair), {"reason": "eligible"}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once_local", observe)
    started = time.monotonic()
    try:
        result, report = scanner._fetch_direct_route_once(PAIR, None)
        elapsed = time.monotonic()-started
        assert 2.99 <= elapsed < 3.4
        assert result["_depthTransport"] == "tencent_cloud"
        assert report["localFailure"]["localDeadlineMs"] == 3000
    finally:
        release.set()


def test_slow_route_does_not_switch_other_symbols_on_same_exchange(monkeypatch):
    scanner._activate_cloud_backup(PAIR, {"error":"gate timeout"})
    calls=[]
    def observe(pair, config, **kw):
        calls.append(kw.get("use_cloud",False))
        return dict(pair), {"reason":"eligible"}
    monkeypatch.setattr(scanner,"_fetch_direct_route_once_local",observe)
    result,report=scanner._fetch_direct_route_once({**PAIR,"name":"OTHER"},None)
    assert calls==[False]
    assert report["transport"]=="local_proxy"


def test_cloud_failure_can_fail_back_without_ten_second_wait(monkeypatch):
    scanner._activate_cloud_backup(PAIR,{"error":"gate timeout"})
    monkeypatch.setattr(scanner,"_local_emergency_probe_ok",lambda _:True)
    calls=[]
    def observe(pair,config,*,use_cloud=False,**kw):
        calls.append(use_cloud)
        if use_cloud:return None,{"reason":"direct_quote_unavailable","error":"SSL timeout"}
        return dict(pair),{"reason":"eligible"}
    monkeypatch.setattr(scanner,"_fetch_direct_route_once_local",observe)
    result,report=scanner._fetch_direct_route_once({**PAIR,"_depthTransport":"tencent_cloud"},None)
    assert calls==[True,False]
    assert report["emergencyFailback"] is True
    assert result["_depthTransport"]=="local_proxy"


@pytest.mark.parametrize("second_transport",["local_proxy","tencent_cloud"])
def test_cloud_to_local_confirmation_requires_two_same_channel_checks(monkeypatch,second_transport):
    monkeypatch.setattr(scanner.time,"sleep",lambda *_:None)
    monkeypatch.setattr(scanner,"_record_revalidation_outcome",lambda *a,**kw:None)
    calls=[]
    pair={**PAIR,"_hotDirectHit":{"verifiedAtMs":int(time.time()*1000),"report":{"transport":"tencent_cloud","reason":"eligible"}}}
    def observe(pair,config,**kw):
        transport="local_proxy" if not calls else second_transport
        calls.append(transport)
        return {**PAIR,"_depthTransport":transport},{"reason":"eligible","transport":transport}
    monkeypatch.setattr(scanner,"_fetch_direct_route_once",observe)
    result,report=scanner.revalidate_astro_hot_direct_hit(pair,None)
    assert len(calls)==2
    assert (result is not None)==(second_transport=="local_proxy")
    if result is not None:
        assert all(x["transport"]=="local_proxy" for x in report["checks"])

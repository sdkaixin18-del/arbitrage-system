"""Scheduling, duplicate and request-scope regressions for paused card discovery."""
from copy import deepcopy
import threading
import time

import pytest

from app import astro_spread_scanner as scanner
from app import astro_depth_cloud, astro_depth_transport, astro_sdk
from test_astro_spread_scanner import sdk_config


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_REVIEW_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scanner, "_hot_routes", {})
    monkeypatch.setattr(scanner, "_hot_recent_hits", {})
    monkeypatch.setattr(scanner, "_hot_recent_hit_at_ms", {})
    monkeypatch.setattr(scanner, "_state", {})
    monkeypatch.setattr(scanner, "spread_scan_ff_min_open_pct", lambda: 0.8)
    monkeypatch.setattr(scanner, "spread_scan_sf_min_open_pct", lambda: 0.8)
    monkeypatch.setattr(scanner, "spread_hot_monitor_interval_seconds", lambda: 0.5)
    monkeypatch.setattr(scanner, "_spread_block_rule_sets", lambda: (set(), set()))
    monkeypatch.setattr(scanner, "astro_route_dedupe_state", lambda *_a: None)
    scanner._stop.clear()


def test_removed_funding_endpoints_and_scanner_readers():
    assert not hasattr(scanner, "_fetch_current_funding_snapshot")
    assert not hasattr(scanner, "_prefetch_arbitrage_funding")
    assert not hasattr(astro_depth_transport, "funding_snapshot")
    paths = {route.path for route in astro_depth_cloud.app.routes}
    assert {"/health", "/v1/public-get", "/dex-quote", "/v1/dex-quote", "/dex-coins"} == paths


def test_distant_price_misses_back_off_but_near_threshold_recovers():
    item = {"pair": {"type": "FF"}, "lastDirectCheckStartedMonotonic": 100}
    report = {"reason": "below_threshold_or_rule_failed", "latestOpenSpreadPct": 0.1, "durationMs": 700}
    actual = []
    for _ in range(6):
        scanner._update_hot_price_cadence(item, report)
        actual.append(item["pollIntervalMs"])
    assert actual == [500, 2000, 4000, 5000, 5000, 5000]
    assert item["nextPollMonotonic"] == 105  # Request duration is part of the interval.
    scanner._update_hot_price_cadence(item, {**report, "latestOpenSpreadPct": 0.7})
    assert item["pollIntervalMs"] == 500 and item["priceBackoffCount"] == 0


@pytest.mark.parametrize("reason", ["stale_direct_quote", "direct_quote_time_skew", "direct_quote_unavailable"])
def test_unavailable_evidence_is_not_mistaken_for_a_distant_price(reason):
    item = {"pair": {"type": "SF"}, "priceBackoffCount": 4}
    scanner._update_hot_price_cadence(item, {"reason": reason, "latestOpenSpreadPct": -1})
    assert item["priceBackoffCount"] == 0


def test_unchanged_pulse_keeps_backoff_and_new_improvement_wakes(monkeypatch):
    monkeypatch.setattr(scanner, "_meets_auto_card_rule", lambda *_a: True)
    monkeypatch.setattr(scanner, "build_astro_spread_pairs", lambda item, _config: [{
        "name": item["symbol"], "type": item["type"], "buyEx": item["buyExchange"], "sellEx": item["sellExchange"]}])
    candidate = {"symbol": "WOO", "type": "FF", "buyExchange": "binance", "sellExchange": "bitget",
                 "openSpreadPct": 1.0, "quoteAt": 1000}
    scanner._register_hot_candidates([candidate], set(), sdk_config())
    item = next(iter(scanner._hot_routes.values()))
    deadline = time.monotonic() + 5
    item.update(nextPollMonotonic=deadline, priceBackoffCount=4, pollIntervalMs=5000)
    scanner._register_hot_candidates([{**candidate, "quoteAt": 2000}], set(), sdk_config())
    unchanged = next(iter(scanner._hot_routes.values()))
    assert unchanged["nextPollMonotonic"] == deadline
    assert unchanged["priceBackoffCount"] == 4
    scanner._register_hot_candidates([{**candidate, "quoteAt": 3000, "openSpreadPct": 1.2}], set(), sdk_config())
    improved = next(iter(scanner._hot_routes.values()))
    assert improved["nextPollMonotonic"] < deadline
    assert improved["priceBackoffCount"] == 0


def test_new_route_and_near_threshold_precede_distant_route():
    common = {"identity": ("WOO", "FF", "binance", "gate"), "reasons": ["pulse_above_threshold"],
              "registeredAtMs": 9000, "lastDirectCheckStartedAtMs": 9500, "checks": 2}
    near, distant = deepcopy(common), {**common, "priceBackoffCount": 4}
    first = {**common, "checks": 0, "lastDirectCheckStartedAtMs": None}
    assert scanner._hot_route_priority(first, now_ms=10000) < scanner._hot_route_priority(near, now_ms=10000)
    assert scanner._hot_route_priority(near, now_ms=10000) < scanner._hot_route_priority(distant, now_ms=10000)


def test_first_pulse_source_is_processed_before_slow_source_finishes(monkeypatch):
    release, partial_seen = threading.Event(), threading.Event()
    monkeypatch.setattr(scanner, "PULSE_URLS", ("fast", "slow"))
    def fetch(url):
        if url == "slow":
            assert release.wait(2)
        return {"code": 0, "data": {url: {}}}
    monkeypatch.setattr(scanner, "_fetch_pulse", fetch)
    partials = []
    def partial(rows):
        partials.append(rows)
        partial_seen.set()
        release.set()
    payloads, summary = scanner._fetch_direct_pulse_payloads(on_partial=partial)
    assert partial_seen.is_set() and partials == [[{"code": 0, "data": {"fast": {}}}]]
    assert len(payloads) == 2 and summary["successCount"] == 2


def test_partial_discovery_never_submits_or_cleans_cards(monkeypatch):
    monkeypatch.setattr(scanner, "astro_sdk_config", sdk_config)
    monkeypatch.setattr(scanner, "scan_pulse_spreads", lambda *_a: ([{"symbol": "WOO", "type": "FF"}], 2))
    monkeypatch.setattr(scanner, "_meets_auto_card_rule", lambda *_a: True)
    monkeypatch.setattr(scanner, "_filter_delisted_exchange_candidates", lambda candidates: (candidates, {}))
    registered = []
    monkeypatch.setattr(scanner, "_register_hot_candidates", lambda candidates, *_a: registered.extend(candidates) or {"addedCount": 1})
    monkeypatch.setattr(scanner, "schedule_astro_pairs", lambda *_a, **_k: pytest.fail("partial discovery must not submit"))
    scanner._register_partial_pulse_candidates([{"data": {}}])
    assert registered == [{"symbol": "WOO", "type": "FF"}]


@pytest.mark.parametrize("age_ms,expected_wait", [(100, 0.15), (800, None)])
def test_second_round_only_waits_remaining_interval(monkeypatch, age_ms, expected_wait):
    waits, reads = [], []
    monkeypatch.setattr(scanner.time, "time", lambda: 1000)
    monkeypatch.setattr(scanner.time, "sleep", waits.append)
    monkeypatch.setattr(scanner, "spread_hot_monitor_confirmation_interval_seconds", lambda: 0.25)
    monkeypatch.setattr(scanner, "_record_revalidation_outcome", lambda *_a, **_k: None)
    def fetch(pair, *_a, **_k):
        reads.append(1)
        return pair, {"reason": "eligible", "transport": "local_proxy"}
    monkeypatch.setattr(scanner, "_fetch_direct_route_once", fetch)
    pair = {"type": "FF", "name": "WOO", "buyEx": "binance", "sellEx": "bitget",
            "_hotDirectHit": {"verifiedAtMs": 1_000_000-age_ms, "report": {"transport": "local_proxy"}}}
    _, report = scanner.revalidate_astro_hot_direct_hit(pair, sdk_config())
    assert report["roundsPassed"] == 2 and reads == [1]
    assert waits == ([] if expected_wait is None else [pytest.approx(expected_wait)])


def test_sdk_keeps_final_remote_dedupe_after_removing_middle_read(monkeypatch):
    pair = {"name": "WOO", "type": "FF", "buyEx": "binance", "sellEx": "bitget"}
    calls = []
    class Client:
        def list_pairs(self):
            calls.append("list")
            return [pair]  # Manual creation raced with this worker's batch snapshot.
        def add_pair(self, _pair):
            pytest.fail("must not duplicate the manually created card")
    monkeypatch.setattr(astro_sdk, "_prepare_dex_route", lambda *_a: (True, {}))
    monkeypatch.setattr(astro_sdk, "_refresh_pending_submission_routes", lambda: None)
    monkeypatch.setattr(astro_sdk, "_pending_submission_routes", set())
    monkeypatch.setattr(astro_sdk, "_log", lambda *_a, **_k: None)
    pair.update(openPosition="0.02", disableOpen=False)
    assert astro_sdk._sync_candidate_pair(Client(), pair, sdk_config(), None, set()) is False
    assert calls == ["list"]

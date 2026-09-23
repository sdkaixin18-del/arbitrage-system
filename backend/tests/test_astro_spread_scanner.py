from __future__ import annotations

import time
import threading

import httpx
import pytest
import app.astro_spread_scanner as scanner_module

from app.astro_spread_scanner import (
    _auto_card_volume_check,
    _auto_card_route_observations,
    _annotate_okxdex_manual_mappings,
    _confirmed_candidates,
    _fetch_direct_route_once,
    _fetch_direct_quote,
    _fetch_okxdex_executable_preflight,
    _filter_delisted_exchange_candidates,
    _hot_routes,
    _register_hot_candidates,
    _hits,
    _meets_auto_card_rule,
    _merge_pending_dex_mapping_items,
    _select_okxdex_targets,
    _verify_okxdex_candidate_identities,
    _verify_okxdex_identity,
    astro_spread_scanner_status,
    revalidate_astro_spread_pair,
    revalidate_astro_hot_direct_hit,
    revalidate_astro_cleanup,
    scan_pulse_spreads,
    spread_scan_confirmations,
    spread_scan_exclude_delisted_exchange_cards,
    spread_scan_ff_min_open_pct,
    spread_scan_max_quote_age_seconds,
    spread_scan_sf_min_open_pct,
    spread_scan_sf_min_short_funding_pct,
    spread_scan_sf_okxdex_auto_card_enabled,
    update_astro_spread_subscriptions,
)
from app.astro_sdk import (
    AstroSdkConfig,
    astro_greater_price_alert_pct,
    astro_price_change_alert_only_rise,
    astro_price_change_alert_pct,
)
from app.astro_spread_history import assess_ff_structure_history


def market(timestamp: int, rows: list[dict[str, object]]) -> dict[str, object]:
    return {"ts": timestamp, "list": rows}


def sdk_config() -> AstroSdkConfig:
    return AstroSdkConfig(
        base_url="https://astro.example",
        admin_prefix="prefix",
        api_key="secret",
        enabled=True,
        dry_run=False,
        tls_verify=True,
        timeout_seconds=5,
        restart_wait_seconds=3,
        max_cards_per_scan=0,
        max_trade_usdt=10,
        leverage=3,
        spot_margin_type="cross",
        dex_api_path="",
    )


def cex_preflight(now_ms: int, buy_price: float = 100.0, sell_price: float = 102.0) -> dict[str, object]:
    depth_common = {
        "timestamp": now_ms,
        "timestampSource": "exchange_depth_update",
        "receivedAt": now_ms,
        "requestDurationMs": 10,
        "bestBid": buy_price - 0.1,
        "bestAsk": buy_price,
        "bestBidQuantity": 1000,
        "bestAskQuantity": 1000,
        "endpoint": "https://exchange.example/depth",
    }
    return {
        "source": "exchange_public_depth_same_quantity",
        "quoteNotionalUsdt": 10.0,
        "tokenQuantity": 0.1,
        "buyAveragePrice": buy_price,
        "sellAveragePrice": sell_price,
        "executableSpreadPct": 2 * (sell_price - buy_price) / (sell_price + buy_price) * 100,
        "buyExecution": {"quantity": 0.1, "averagePrice": buy_price},
        "sellExecution": {"targetQuantity": 0.1, "averagePrice": sell_price},
        "buyDepth": dict(depth_common),
        "sellDepth": {
            **depth_common,
            "bestBid": sell_price,
            "bestAsk": sell_price + 0.1,
        },
    }


@pytest.fixture(autouse=True)
def _default_okxdex_safety_gates_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep legacy safety expectations explicit; bypass tests opt out themselves."""

    # Never inherit the running site's saved thresholds/identity switches.
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "isolated-scanner-settings.json"))

    # These regressions exercise the established transport/legacy rule path.
    # The enabled economic policy is covered separately with complete plan
    # evidence, rather than allowing missing new evidence to alter old mocks.
    monkeypatch.setattr(scanner_module, "_load_pulse_symbol_aliases", lambda: {})
    monkeypatch.setattr(scanner_module, "_active_delisting_exchange_blocks", lambda: set())
    import app.astro_contract_safety as contract_safety
    monkeypatch.setattr(contract_safety, "route_check", lambda *_args, **_kwargs: None)
    # Scanner tests must never add synthetic ABC decisions or settings events
    # to the live runtime audit log.
    monkeypatch.setattr(scanner_module, "append_system_runtime_event", lambda *_args, **_kwargs: {})
    import app.notifications as notifications_module
    monkeypatch.setattr(notifications_module, "send_bark_or_log", lambda *_args, **_kwargs: ("ok", None))
    monkeypatch.setenv("ASTRO_SPREAD_OKXDEX_IDENTITY_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("ASTRO_PULSE_CLOUD_BRIDGE_ENABLED", "0")
    monkeypatch.setenv("ASTRO_DEPTH_CLOUD_ENABLED", "0")
    from app.astro_route_policy import RoutePolicy, LatestQueue
    from app.astro_shared_reads import InflightReads
    monkeypatch.setattr(scanner_module, "_route_policy", RoutePolicy())
    monkeypatch.setattr(scanner_module, "_cloud_route_queue", LatestQueue())
    monkeypatch.setattr(scanner_module, "_auxiliary_reads", InflightReads())
    monkeypatch.setattr(scanner_module, "_api_transition_log_windows", {})
    scanner_module._stop.clear()
    scanner_module._depth_backup_sources.clear()
    scanner_module._depth_probe_targets.clear()
    monkeypatch.setattr(scanner_module.astro_pulse_health, "_sources", {})
    scanner_module._pulse_consecutive_failures = 0
    scanner_module._pulse_outage_started_at = None
    scanner_module._api_degraded_route_failures.clear()
    scanner_module._api_degraded_push_last.clear()
    scanner_module._api_degraded_incidents.clear()
    scanner_module._api_recovery_probe_state.clear()
    scanner_module._depth_exchange_health.clear()
    scanner_module._api_degraded_runtime.update({
        "fallbackCardCount": 0,
        "lastFallbackCardAt": None,
        "lastPushAt": None,
        "lastPushStatus": None,
        "lastPushMessage": None,
    })
    scanner_module._pulse_outage_last_summary_at = 0.0
    scanner_module._pulse_unhealthy_logged = False
    scanner_module._pulse_source_log_at = 0.0
    scanner_module._pulse_source_degraded_streak = 0
    scanner_module._pulse_source_degraded_started_at = None
    scanner_module._pulse_source_degraded_logged = False
    with scanner_module._state_lock:
        scanner_module._state.update(
            {
                "pulseHealthy": True,
                "pulseConsecutiveFailureCount": 0,
                "pulseOutageStartedAt": None,
                "cleanupPaused": False,
            }
        )
    with scanner_module._hot_lock:
        scanner_module._hot_routes.clear()
        scanner_module._hot_recent_hits.clear()
    with scanner_module._depth_request_condition:
        scanner_module._depth_request_cache.clear()
        scanner_module._depth_request_errors.clear()
        scanner_module._depth_request_inflight.clear()
        scanner_module._depth_request_metrics.update(
            {
                "startedAt": None,
                "networkRequests": 0,
                "cacheHits": 0,
                "joinedInflight": 0,
                "http429": 0,
                "errors": 0,
                "expandedDepthRequests": 0,
                "circuitRejected": 0,
            }
        )
    with scanner_module._depth_exchange_health_lock:
        scanner_module._depth_exchange_health.clear()
    with scanner_module._decision_audit_lock:
        scanner_module._missed_opportunity_last.clear()
        scanner_module._direct_failure_streaks.clear()
    yield
    scanner_module._cloud_route_queue.stop()
    executor, scanner_module._api_notification_executor = scanner_module._api_notification_executor, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    with scanner_module._hot_lock:
        scanner_module._hot_routes.clear()
        scanner_module._hot_recent_hits.clear()


def test_pulse_sf_uses_spot_ask_and_future_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_EXCHANGES", "binance,gate")
    payload = {
        "data": {
            "binanceSpot": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 100.0, "b": 99.0, "trade24Count": 300_000}],
            ),
            "gateFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 103.0, "b": 102.0, "trade24Count": 300_000}],
            ),
        }
    }
    candidates, count = scan_pulse_spreads([payload], now_ms=1_001_000)
    sf = next(item for item in candidates if item["type"] == "SF")
    assert count == 2
    assert sf["buyExchange"] == "binance"
    assert sf["sellExchange"] == "gate"
    assert sf["openSpreadPct"] == pytest.approx(2 * (102 - 100) / (102 + 100) * 100)
    assert sf["closeSpreadPct"] == pytest.approx(2 * (103 - 99) / (103 + 99) * 100)


def test_pulse_ff_selects_better_executable_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_EXCHANGES", "binance,gate")
    payload = {
        "data": {
            "binanceFuture": market(
                2_000_000,
                [{"name": "XYZUSDT", "a": 10.0, "b": 9.9, "rate": 0.02, "trade24Count": 300_000}],
            ),
            "gateFuture": market(
                2_000_000,
                [{"name": "XYZUSDT", "a": 10.5, "b": 10.4, "rate": -0.10, "trade24Count": 300_000}],
            ),
        }
    }
    candidates, _ = scan_pulse_spreads([payload], now_ms=2_001_000)
    ff = next(item for item in candidates if item["type"] == "FF")
    assert ff["buyExchange"] == "binance"
    assert ff["sellExchange"] == "gate"
    assert ff["openSpreadPct"] == pytest.approx(2 * (10.4 - 10.0) / (10.4 + 10.0) * 100)
    assert ff["buyFundingRatePct"] is None
    assert ff["sellFundingRatePct"] is None
    assert ff["netFundingRatePct"] is None
    assert ff["quoteSource"] == "astro_pulse_aggregate"
    assert ff["quoteSkewSeconds"] == pytest.approx(0.0)
    assert ff["buyQuote"] == {
        "exchange": "binance",
        "market": "future",
        "bid": 9.9,
        "ask": 10.0,
        "timestamp": 2_000_000,
    }
    assert ff["sellQuote"] == {
        "exchange": "gate",
        "market": "future",
        "bid": 10.4,
        "ask": 10.5,
        "timestamp": 2_000_000,
    }


def test_non_positive_open_direction_cannot_be_offset_by_positive_close_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    candidate = {
        "type": "FF",
        "symbol": "BASECAT",
        "buyExchange": "gate",
        "sellExchange": "aster",
        "openSpreadPct": -0.76,
        "closeSpreadPct": 2.80,
    }

    decision = scanner_module._candidate_rule_decision(candidate)

    assert _meets_auto_card_rule(candidate) is False
    assert decision["eligible"] is False
    assert decision["primaryReason"] == "open_spread_non_positive"
    assert "open_spread_non_positive" in decision["failedRules"]


def test_new_listing_symbol_is_retained_below_spread_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_EXCHANGES", "binance,gate")
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "spread_scan_ff_min_open_pct", lambda: 1.0)
    payload = {
        "data": {
            "binanceFuture": market(
                3_000_000,
                [{"name": "NEWUSDT", "a": 10.0, "b": 9.99, "rate": 0.01, "trade24Count": 300_000}],
            ),
            "gateFuture": market(
                3_000_000,
                [{"name": "NEWUSDT", "a": 10.03, "b": 10.02, "rate": 0.01, "trade24Count": 300_000}],
            ),
        }
    }

    ordinary, _ = scan_pulse_spreads([payload], now_ms=3_001_000)
    priority, _ = scan_pulse_spreads([payload], now_ms=3_001_000, priority_symbols={"NEW"})

    assert ordinary == []
    assert len(priority) == 1
    assert priority[0]["symbol"] == "NEW"
    assert priority[0]["openSpreadPct"] < 1.0


def test_new_listing_bypasses_only_explicit_two_hour_market_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_EXCHANGES", "binance,gate")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "spread_scan_ff_min_open_pct", lambda: 1.0)
    monkeypatch.setattr(scanner_module.time, "time", lambda: 3001)
    payload = {
        "data": {
            "binanceFuture": market(
                3_000_000,
                [{"name": "牛来USDT", "a": 10.0, "b": 9.99, "rate": 0.01, "trade24Count": 12_000}],
            ),
            "gateFuture": market(
                3_000_000,
                [{"name": "牛来USDT", "a": 10.31, "b": 10.3, "rate": 0.01, "trade24Count": None}],
            ),
        }
    }

    ordinary, _ = scan_pulse_spreads([payload], now_ms=3_001_000)
    priority, _ = scan_pulse_spreads(
        [payload],
        now_ms=3_001_000,
        priority_symbols={"牛来"},
        listing_market_windows={
            ("牛来", "binance", "future"): 3_000_000,
            ("牛来", "gate", "future"): 3_000_000,
        },
    )

    assert ordinary == []
    assert len(priority) == 1
    assert priority[0]["symbol"] == "牛来"
    assert priority[0]["priorityNewListing"] is True
    assert _auto_card_volume_check(priority[0]) == (True, "new_listing_bypass")
    assert _meets_auto_card_rule(priority[0]) is True


def test_new_listing_registers_for_hot_direct_monitor_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "spread_scan_ff_min_open_pct", lambda: 1.0)
    candidate = {
        "key": "FF:NEW:binance:gate",
        "symbol": "NEW",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "buyMarket": "future",
        "sellMarket": "future",
        "openSpreadPct": 0.25,
        "closeSpreadPct": 0.05,
        "buyVolume24hUsdt": 300_000,
        "sellVolume24hUsdt": 400_000,
        "listingVolumeWindows": {"buy": int(time.time() * 1000)},
    }

    summary = _register_hot_candidates([candidate], {"NEW"}, sdk_config())

    assert summary["routeCount"] == 1
    assert summary["newListingRouteCount"] == 1
    with scanner_module._hot_lock:
        item = next(iter(_hot_routes.values()))
    assert item["reasons"] == ["new_listing"]
    assert item["pair"]["name"] == "NEW"


def test_listing_announcements_remain_discovery_only_until_pulse_has_real_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "spread_scan_ff_min_open_pct", lambda: 1.0)
    routes = [
        {
            "exchange": "bg",
            "market_type": "futures",
            "symbol": "PONS",
            "event_action": "listing",
            "event_source": "1",
            "pending": "1",
        },
        {
            "exchange": "as",
            "market_type": "futures",
            "symbol": "PONS",
            "event_action": "listing",
            "event_source": "0",
            "pending": "0",
        },
    ]

    hints = scanner_module._listing_route_hint_candidates("PONS", routes)
    summary = _register_hot_candidates(hints, {"PONS"}, sdk_config())

    assert {(item["buyExchange"], item["sellExchange"]) for item in hints} == {
        ("bitget", "aster"),
        ("aster", "bitget"),
    }
    assert summary["routeCount"] == 0
    assert summary["aboveThresholdRouteCount"] == 0
    assert summary["newListingRouteCount"] == 0
    with scanner_module._hot_lock:
        reasons = {tuple(item["reasons"]) for item in _hot_routes.values()}
    assert reasons == set()


def test_hot_scheduler_prioritizes_unchecked_threshold_routes_and_groups_symbol() -> None:
    def route(
        symbol: str,
        sell_exchange: str,
        *,
        checks: int,
        spread: float,
        reasons: list[str],
    ) -> dict[str, object]:
        return {
            "identity": (symbol, "FF", "okx", sell_exchange, "", ""),
            "candidate": {"openSpreadPct": spread},
            "checks": checks,
            "firstSeenAtMs": 1000,
            "nextPollMonotonic": 1.0,
            "reasons": reasons,
        }

    zora = [
        route("ZORA", exchange, checks=0, spread=1.2, reasons=["pulse_above_threshold"])
        for exchange in ("binance", "gate", "aster", "bitget")
    ]
    recycled = route("OLD", "gate", checks=8, spread=3.0, reasons=["pulse_above_threshold"])
    listing_only = route("NEW", "gate", checks=0, spread=0.2, reasons=["new_listing"])

    selected = scanner_module._select_hot_routes_for_cycle(
        [recycled, listing_only, *reversed(zora)],
        limit=6,
        per_symbol_limit=4,
    )

    assert [item["identity"][0] for item in selected[:4]] == ["ZORA"] * 4
    assert {item["identity"][3] for item in selected[:4]} == {
        "binance",
        "gate",
        "aster",
        "bitget",
    }
    assert selected[4]["identity"][0] == "OLD"
    assert selected[5]["identity"][0] == "NEW"


def test_hot_scheduler_revisits_every_due_route_under_persistent_spread_rank() -> None:
    routes = [
        {
            "identity": (symbol, "FF", f"buy{index}", "bitget", "", ""),
            "candidate": {"openSpreadPct": 3.0 - index * 0.1},
            "reasons": ["pulse_above_threshold"],
            "checks": 1,
            "firstSeenAtMs": 1000,
            "firstDirectCheckAtMs": 2000,
            "lastDirectCheckStartedAtMs": 2000,
            "nextPollMonotonic": 1.0,
        }
        for symbol in ("WOO", "OTHER", "THIRD")
        for index in range(8)
    ]
    seen = set()
    for cycle in range(3):
        selected = scanner_module._select_hot_routes_for_cycle(
            routes, limit=8, per_symbol_limit=4,
        )
        assert len(selected) == 8
        for symbol in ("WOO", "OTHER", "THIRD"):
            assert sum(item["identity"][0] == symbol for item in selected) <= 4
        for item in selected:
            seen.add(item["identity"])
            item["checks"] += 1
            item["lastDirectCheckStartedAtMs"] = 3000 + cycle * 1000
            item["nextPollMonotonic"] = 3.0 + cycle
    assert seen == {item["identity"] for item in routes}


@pytest.mark.parametrize("dedupe", [None, "queued", "syncing", "existing"])
def test_recent_first_round_hit_only_blocks_registration_while_route_is_tracked(
    monkeypatch: pytest.MonkeyPatch, dedupe: str | None,
) -> None:
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _route: dedupe)
    candidate = {
        "key": "FF:WOO:binance:bitget", "symbol": "WOO", "type": "FF",
        "buyExchange": "binance", "sellExchange": "bitget",
        "buyMarket": "future", "sellMarket": "future",
        "openSpreadPct": 1.4, "closeSpreadPct": 0.0,
        "buyVolume24hUsdt": 300_000, "sellVolume24hUsdt": 400_000,
    }
    identity = scanner_module._candidate_route_identity(candidate)
    scanner_module._hot_recent_hits[identity] = time.monotonic()
    summary = _register_hot_candidates([candidate], set(), sdk_config())
    assert summary["routeCount"] == (1 if dedupe is None else 0)
    if dedupe is None:
        pair = scanner_module._hot_routes[identity]["pair"]
        assert pair["buyEx"] == "binance"
        assert pair["sellEx"] == "bitget"


def test_hot_candidate_refresh_preserves_original_registration_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    candidate = {
        "key": "FF:FRESH:binance:gate",
        "symbol": "FRESH",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "buyMarket": "future",
        "sellMarket": "future",
        "openSpreadPct": 1.4,
        "closeSpreadPct": 0.0,
        "buyVolume24hUsdt": 300_000,
        "sellVolume24hUsdt": 400_000,
    }

    _register_hot_candidates([candidate], set(), sdk_config())
    with scanner_module._hot_lock:
        original = next(iter(scanner_module._hot_routes.values()))["registeredAtMs"]
    time.sleep(0.002)
    _register_hot_candidates([candidate], set(), sdk_config())
    with scanner_module._hot_lock:
        refreshed = next(iter(scanner_module._hot_routes.values()))

    assert refreshed["registeredAtMs"] == original
    assert refreshed["pair"]["_pipeline"]["hotWatchRegisteredAtMs"] == original


def test_existing_card_route_never_enters_hot_direct_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(
        scanner_module,
        "astro_route_dedupe_state",
        lambda _route: "existing",
    )
    candidate = {
        "key": "FF:SIREN:binance:gate",
        "symbol": "SIREN",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "buyMarket": "future",
        "sellMarket": "future",
        "openSpreadPct": 1.4,
        "closeSpreadPct": 0.0,
        "buyVolume24hUsdt": 300_000,
        "sellVolume24hUsdt": 400_000,
    }

    summary = _register_hot_candidates([candidate], set(), sdk_config())

    assert summary["routeCount"] == 0
    assert scanner_module._hot_routes == {}


def test_route_created_after_registration_is_pruned_before_depth_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ("SIREN", "FF", "binance", "gate", "", "")
    with scanner_module._hot_lock:
        scanner_module._hot_routes[identity] = {"identity": identity}
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _route: "existing")

    assert scanner_module._prune_deduplicated_hot_routes() == 1
    assert scanner_module._hot_routes == {}


def test_hot_revalidation_requires_a_second_independent_executable_depth_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = {
        "name": "FLASH",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "openPosition": "0.012",
        "_hotDirectHit": {
            "verifiedAtMs": int(time.time() * 1000),
            "report": {
                "reason": "eligible",
                "latestOpenSpreadPct": 1.2,
                "quoteSkewSeconds": 0.04,
            },
        },
    }
    outcomes: list[tuple[bool, str]] = []
    refreshed = {**pair, "openPosition": "0.0115"}
    monkeypatch.setattr(scanner_module.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", lambda *_args, **_kwargs: (
        refreshed,
        {"reason": "eligible", "latestOpenSpreadPct": 1.15, "quoteSkewSeconds": 0.03},
    ))
    monkeypatch.setattr(
        scanner_module,
        "_record_revalidation_outcome",
        lambda _pair, *, passed, report: outcomes.append((passed, report["mode"])),
    )

    latest, report = revalidate_astro_hot_direct_hit(pair, sdk_config())

    assert latest is refreshed
    assert report["roundsRequired"] == 2
    assert report["roundsPassed"] == 2
    assert report["mode"] == "two_independent_executable_depth_checks"
    assert len(report["checks"]) == 2
    assert outcomes == [(True, "two_independent_executable_depth_checks")]


def test_hot_direct_check_skips_preliminary_ticker_and_uses_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    ticker_calls = 0

    def ticker_should_not_run(*_args, **_kwargs):
        nonlocal ticker_calls
        ticker_calls += 1
        raise AssertionError("hot direct check must not request a preliminary ticker")

    monkeypatch.setattr(scanner_module, "_load_pulse_symbol_aliases", lambda: {})
    monkeypatch.setattr(scanner_module, "_fetch_direct_quote", ticker_should_not_run)
    monkeypatch.setattr(
        scanner_module,
        "_fetch_cex_executable_preflight",
        lambda *_args, **_kwargs: cex_preflight(now_ms),
    )
    pair = {
        "name": "FLASH",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "_buyVolume24hUsdt": 500_000,
        "_sellVolume24hUsdt": 500_000,
    }

    latest, report = scanner_module._fetch_direct_route_once(
        pair,
        sdk_config(),
        executable_depth_first=True,
    )

    assert latest is not None
    assert ticker_calls == 0
    assert report["preliminaryTickerSkipped"] is True
    assert report["topBookDiscoveryOnly"] is False


def test_direct_depth_rejects_negative_open_even_when_reverse_spread_is_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(scanner_module, "_load_pulse_symbol_aliases", lambda: {})
    monkeypatch.setattr(
        scanner_module,
        "_fetch_cex_executable_preflight",
        lambda *_args, **_kwargs: cex_preflight(now_ms, buy_price=102.0, sell_price=100.0),
    )
    pair = {
        "name": "BASECAT",
        "type": "FF",
        "buyEx": "gate",
        "sellEx": "aster",
        "_buyVolume24hUsdt": 500_000,
        "_sellVolume24hUsdt": 500_000,
    }

    latest, report = _fetch_direct_route_once(
        pair,
        sdk_config(),
        executable_depth_first=True,
    )

    assert latest is None
    assert report["reason"] == "open_spread_non_positive"
    assert report["latestOpenSpreadPct"] < 0


def test_hot_route_direct_check_records_rejection_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        scanner_module,
        "_fetch_direct_route_once",
        lambda *_args, **_kwargs: (None, {"reason": "direct_spread_below_threshold"}),
    )
    monkeypatch.setattr(
        scanner_module,
        "_append_decision_audit",
        lambda _identity, *, stage, decision, details: recorded.append(
            (stage, decision, details)
        )
        or True,
    )
    item = {
        "identity": ("FLASH", "FF", "binance", "gate", "", ""),
        "pair": {"name": "FLASH", "type": "FF", "buyEx": "binance", "sellEx": "gate"},
        "candidate": {"openSpreadPct": 1.2},
        "reasons": ["pulse_above_threshold"],
    }

    identity, latest, report = scanner_module._hot_route_direct_check(item, sdk_config())

    assert identity == item["identity"]
    assert latest is None
    assert report["reason"] == "direct_spread_below_threshold"
    assert report["durationMs"] >= 0
    assert recorded[0][0:2] == ("hot_direct_check", "direct_spread_below_threshold")
    assert recorded[0][2]["watchReasons"] == ["pulse_above_threshold"]


def test_hot_revalidation_rejects_when_second_depth_hit_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = {
        "name": "FLASH",
        "type": "FF",
        "buyEx": "gate",
        "sellEx": "bitget",
        "openPosition": "0.014",
        "_hotDirectHit": {
            "verifiedAtMs": int(time.time() * 1000),
            "report": {"reason": "eligible", "latestOpenSpreadPct": 1.4},
        },
    }
    monkeypatch.setattr(scanner_module.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        scanner_module,
        "_fetch_direct_route_once",
        lambda *_args, **_kwargs: (
            None,
            {"reason": "below_threshold_or_rule_failed", "latestOpenSpreadPct": 0.4},
        ),
    )
    monkeypatch.setattr(scanner_module, "_record_revalidation_outcome", lambda *_args, **_kwargs: None)

    latest, report = revalidate_astro_hot_direct_hit(pair, sdk_config())

    assert latest is None
    assert report["reason"] == "below_threshold_or_rule_failed"
    assert report["roundsRequired"] == 2
    assert report["roundsPassed"] == 1
    assert report["mode"] == "two_independent_executable_depth_checks"


def test_hot_revalidation_allows_gate_card_when_second_transport_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    pair = {
        "name": "FLASH",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "openPosition": "0.012",
        "_hotDirectHit": {
            "verifiedAtMs": now_ms,
            "report": {
                "reason": "eligible",
                "latestOpenSpreadPct": 1.2,
                "quoteSkewSeconds": 0.2,
                "buyQuote": {"quoteAgeSeconds": 0.3},
                "sellQuote": {"quoteAgeSeconds": 0.4},
                "cexExecutablePreflight": {"quoteNotionalUsdt": 10.0},
            },
        },
    }
    events: list[dict] = []
    outcomes: list[tuple[bool, str]] = []
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setattr(scanner_module.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        scanner_module,
        "_fetch_direct_route_once",
        lambda *_args, **_kwargs: (
            None,
            {
                "reason": "cex_executable_depth_unavailable",
                "error": "gate深度请求失败：_ssl.c:993 handshake operation timed out",
            },
        ),
    )
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr(
        scanner_module,
        "_record_revalidation_outcome",
        lambda _pair, *, passed, report: outcomes.append((passed, report["mode"])),
    )

    latest, report = revalidate_astro_hot_direct_hit(pair, sdk_config())

    assert latest is None
    assert report["reason"] == "cex_executable_depth_unavailable"
    assert report["roundsPassed"] == 1
    assert report["mode"] == "two_independent_executable_depth_checks"
    assert outcomes == [(False, "two_independent_executable_depth_checks")]
    assert events == []


def test_gate_transport_failure_never_falls_back_without_valid_executable_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = {
        "name": "FLASH",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "openPosition": "0.012",
        "_hotDirectHit": {
            "verifiedAtMs": int(time.time() * 1000),
            "report": {
                "reason": "eligible",
                "latestOpenSpreadPct": 1.2,
                "quoteSkewSeconds": 0.2,
                "buyQuote": {"quoteAgeSeconds": 0.3},
                "sellQuote": {"quoteAgeSeconds": 0.4},
                # No executable-depth evidence: this is equivalent to Pulse/top-book only.
            },
        },
    }
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setattr(scanner_module.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        scanner_module,
        "_fetch_direct_route_once",
        lambda *_args, **_kwargs: (
            None,
            {
                "reason": "cex_executable_depth_unavailable",
                "error": "gate深度请求失败：handshake operation timed out",
            },
        ),
    )
    monkeypatch.setattr(scanner_module, "_record_revalidation_outcome", lambda *_args, **_kwargs: None)

    latest, report = revalidate_astro_hot_direct_hit(pair, sdk_config())

    assert latest is None
    assert report["reason"] == "cex_executable_depth_unavailable"
    assert report["roundsPassed"] == 1


def test_cex_preflight_uses_configured_max_buy_and_same_quantity_sell_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_CEX_QUOTE_NOTIONAL_USDT", "10")
    now_ms = int(time.time() * 1000)

    def depth_book(_client, exchange, market_type, _symbol, _aliases):
        if exchange == "gate":
            return {
                "exchange": exchange,
                "market": market_type,
                "bids": [[99.9, 100]],
                "asks": [[100.0, 0.02], [101.0, 100]],
                "bestBid": 99.9,
                "bestAsk": 100.0,
                "bestBidQuantity": 100,
                "bestAskQuantity": 0.02,
                "timestamp": now_ms,
                "timestampSource": "exchange_depth_update",
                "receivedAt": now_ms,
                "requestDurationMs": 1,
                "endpoint": "gate-depth",
            }
        return {
            "exchange": exchange,
            "market": market_type,
            "bids": [[102.0, 0.01], [100.2, 100]],
            "asks": [[102.1, 100]],
            "bestBid": 102.0,
            "bestAsk": 102.1,
            "bestBidQuantity": 0.01,
            "bestAskQuantity": 100,
            "timestamp": now_ms,
            "timestampSource": "exchange_depth_update",
            "receivedAt": now_ms,
            "requestDurationMs": 1,
            "endpoint": "bitget-depth",
        }

    monkeypatch.setattr(scanner_module, "_fetch_direct_depth_book", depth_book)
    # Explicit plan size: this regression must not read the live saved 20U setting.
    from dataclasses import replace
    result = scanner_module._fetch_cex_executable_preflight(
        object(), "4", "FF", "gate", "bitget", {}, replace(sdk_config(), max_trade_usdt=20)
    )

    assert result["quoteNotionalUsdt"] == pytest.approx(20)
    assert result["buyExecution"]["usedLevels"] == 2
    assert result["sellExecution"]["usedLevels"] == 2
    assert result["tokenQuantity"] == pytest.approx(result["sellExecution"]["targetQuantity"])
    assert result["executableSpreadPct"] < 1


@pytest.mark.parametrize("market,scale", [("spot", 1000), ("future", 1)])
def test_gate_depth_uses_official_snapshot_time_and_retains_last_change(monkeypatch, market, scale):
    now = time.time()
    changed = now - 30
    payload = {"current": now * scale, "update": changed * scale,
               "bids": [["100", "20"]], "asks": [["101", "20"]]}
    monkeypatch.setattr(scanner_module, "_shared_depth_json_get", lambda *a, **k: (
        payload, {"receivedAt": int(now * 1000), "requestDurationMs": 500},
    ))
    monkeypatch.setattr(scanner_module, "_futures_contract_quantity_multiplier", lambda *a: 1.0)
    book = scanner_module._fetch_direct_depth_book(object(), "gate", market, "IDOL", {})
    assert book["timestamp"] == int(now * 1000)
    assert book["bookChangedAt"] == int(changed * 1000)
    assert book["timestampSource"] == "exchange_snapshot_generated"
    assert book["bestBidQuantity"] == 20


@pytest.mark.parametrize("data", [
    {}, {"current": 0}, {"current": "invalid"}, {"current": True},
    {"current": float("nan")}, {"current": float("inf")},
    {"current": 0, "update": 1788646563000},
    {"current": 1788646563000, "update": 1788646566000},
])
def test_gate_bad_native_time_never_falls_back_to_receipt_time(data):
    with pytest.raises(ValueError):
        scanner_module._gate_depth_timestamps(data)


def test_gate_missing_generation_time_conservatively_uses_update():
    stamp, changed, source = scanner_module._gate_depth_timestamps({"update": 1788646563.751})
    assert stamp == changed == 1788646563751
    assert source == "exchange_depth_update"


@pytest.mark.parametrize("snapshot_age,expected_reason", [(0.1, "eligible"), (10, "stale_direct_quote"), (1.8, "direct_quote_time_skew")])
def test_gate_snapshot_mapping_preserves_real_age_and_skew_gates(monkeypatch, snapshot_age, expected_reason):
    now_ms = int(time.time() * 1000)
    stamp, changed, source = scanner_module._gate_depth_timestamps({
        "current": now_ms - snapshot_age * 1000, "update": now_ms - 30000,
    })
    preflight = cex_preflight(now_ms)
    preflight["buyDepth"].update(timestamp=stamp, timestampSource=source, bookChangedAt=changed)
    monkeypatch.setattr(scanner_module, "_load_pulse_symbol_aliases", lambda: {})
    monkeypatch.setattr(scanner_module, "_fetch_cex_executable_preflight", lambda *a, **k: preflight)
    pair = {"name": "IDOL", "type": "FF", "buyEx": "gate", "sellEx": "binance"}
    latest, report = scanner_module._fetch_direct_route_once_local(pair, sdk_config(), executable_depth_first=True)
    assert report["reason"] == expected_reason
    assert (latest is not None) == (expected_reason == "eligible")
    assert report["buyQuote"]["bookChangedAt"] == changed


def test_depth_future_timestamp_is_rejected(monkeypatch):
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(scanner_module, "_shared_depth_json_get", lambda *a, **k: (
        {"current": now_ms + 5000, "update": now_ms, "bids": [["100", "2"]], "asks": [["101", "2"]]},
        {"receivedAt": now_ms},
    ))
    with pytest.raises(ValueError, match="时钟偏差"):
        scanner_module._fetch_direct_depth_book(object(), "gate", "spot", "IDOL", {})


@pytest.mark.parametrize("reason", ["stale_direct_quote", "direct_quote_time_skew", "open_spread_non_positive"])
def test_final_safety_rejection_keeps_audit_but_not_warning(monkeypatch, reason):
    events = []
    monkeypatch.setattr(scanner_module, "append_system_runtime_event", lambda event, **kw: events.append((event, kw)))
    assert scanner_module._append_decision_audit(("IDOL", "FF", "gate", "binance", "", ""),
        stage="final_revalidation", decision=reason, details={"report": {"reason": reason}})
    assert events[0][0] == "astro_auto_card_decision_audit"
    assert events[0][1]["level"] == "info"
    assert events[0][1]["details"]["decision"] == reason


def test_gate_depth_sizes_are_converted_from_contracts_to_base_quantity() -> None:
    scanner_module._contract_quantity_multiplier_cache.clear()

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def get(self, url, params=None):
            if "/contracts/" in url:
                return FakeResponse({"quanto_multiplier": "10"})
            assert params["limit"] == 20
            return FakeResponse({
                "update": int(time.time() * 1000),
                "bids": [{"p": "0.019", "s": 3}],
                "asks": [{"p": "0.020", "s": 2}],
            })

    book = scanner_module._fetch_direct_depth_book(
        FakeClient(), "gate", "future", "4", {}
    )

    assert book["bestAskQuantity"] == pytest.approx(20)
    assert book["bestBidQuantity"] == pytest.approx(30)
    assert book["quantityMultiplier"] == pytest.approx(10)


def test_cex_depth_preflight_reads_at_most_twenty_levels() -> None:
    asks = [[100 + index, 0.01] for index in range(25)]
    bids = [[102 - index * 0.01, 0.01] for index in range(25)]
    buy = scanner_module._consume_asks_by_notional(asks[:20], 10)
    sell = scanner_module._consume_bids_by_quantity(bids[:20], buy["quantity"])

    assert buy["usedLevels"] <= 20
    assert sell["usedLevels"] <= 20


def test_cex_depth_preflight_rejects_when_twenty_levels_cannot_fill_ten_usdt() -> None:
    asks = [[100 + index, 0.001] for index in range(20)]

    with pytest.raises(RuntimeError, match="买入腿卖盘深度不足"):
        scanner_module._consume_asks_by_notional(asks, 10)


def test_identical_depth_request_is_reused_inside_short_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"bids": [["1", "10"]], "asks": [["2", "10"]]}

    def fake_get(_client, url, *, params=None):
        calls.append((url, dict(params or {})))
        return FakeResponse()

    monkeypatch.setattr(scanner_module, "_scanner_public_get", fake_get)
    endpoint = "https://fapi.binance.com/fapi/v1/depth"
    params = {"symbol": "ABCUSDT", "limit": 100}

    first, first_meta = scanner_module._shared_depth_json_get(object(), endpoint, params=params)
    second, second_meta = scanner_module._shared_depth_json_get(object(), endpoint, params=params)

    assert first == second
    assert calls == [(endpoint, params)]
    assert first_meta["cacheHit"] is False
    assert second_meta["cacheHit"] is True
    assert scanner_module._depth_request_control_status()["networkRequests"] == 1
    assert scanner_module._depth_request_control_status()["cacheHits"] == 1


def test_gate_depth_circuit_pauses_then_allows_one_recovery_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict] = []
    clock = [100.0]
    monkeypatch.setenv("ASTRO_GATE_DEPTH_CIRCUIT_FAILURES", "2")
    monkeypatch.setenv("ASTRO_GATE_DEPTH_CIRCUIT_PAUSE_SECONDS", "1")
    monkeypatch.setattr(scanner_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )
    error = RuntimeError("_ssl.c:993 handshake operation timed out")

    scanner_module._record_depth_request_failure(
        "gate", symbol="ABC", error=error, was_probe=False
    )
    scanner_module._record_depth_request_failure(
        "gate", symbol="XYZ", error=error, was_probe=False
    )

    with pytest.raises(scanner_module._GateDepthCircuitOpen, match="短暂停用"):
        scanner_module._before_depth_request("gate")
    clock[0] += 1.01
    assert scanner_module._before_depth_request("gate") is True
    with pytest.raises(scanner_module._GateDepthCircuitOpen, match="恢复探测"):
        scanner_module._before_depth_request("gate")

    scanner_module._record_depth_request_success("gate", was_probe=True)
    assert scanner_module._depth_exchange_health["gate"]["incidentOpen"] is True
    for _ in range(6):
        scanner_module._record_api_recovery_probe_result("gate", success=True, error=None, duration_ms=10)
        clock[0] += 2

    assert [event["event"] for event in events] == [
        "astro_exchange_depth_unavailable",
        "astro_exchange_depth_recovered",
    ]
    assert events[0]["details"]["affectedSymbols"] == ["ABC", "XYZ"]
    assert events[1]["details"]["failureCount"] == 2
    assert events[1]["details"]["recoveredByProbe"] is True
    assert scanner_module._before_depth_request("gate") is False


def test_binance_hedge_depth_starts_at_100_and_only_expands_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_limits: list[int] = []

    def fake_depth(_client, _url, *, params):
        limit = int(params["limit"])
        requested_limits.append(limit)
        quantity = 4 if limit == 100 else 20
        return (
            {"bids": [["10", str(quantity)]]},
            {
                "receivedAt": int(time.time() * 1000),
                "requestDurationMs": 1,
                "cacheHit": False,
                "joinedInflight": False,
            },
        )

    monkeypatch.setattr(scanner_module, "_shared_depth_json_get", fake_depth)
    monkeypatch.setattr(scanner_module, "_futures_contract_quantity_multiplier", lambda *_args: 1.0)

    result = scanner_module._fetch_futures_bid_depth_execution(
        object(), "binance", "ABC", {}, target_quantity=10
    )

    assert requested_limits == [100, 500]
    assert result["depthLimit"] == 500
    assert result["filledQuantity"] == pytest.approx(10)
    assert scanner_module._depth_request_control_status()["expandedDepthRequests"] == 1


def test_fast_scan_drops_low_volume_new_route_but_keeps_tracked_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_MARKETS", "binanceSpot,gateFuture")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    payload = {
        "data": {
            "binanceSpot": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 100.0, "b": 99.0, "trade24Count": 100_000}],
            ),
            "gateFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 103.0, "b": 102.0, "trade24Count": 300_000}],
            ),
        }
    }

    candidates, _ = scan_pulse_spreads(payloads=[payload], now_ms=1_001_000, symbol_aliases={})
    assert candidates == []

    retained = {("ABC", "SF", "binance", "gate", "", "")}
    candidates, _ = scan_pulse_spreads(
        payloads=[payload],
        now_ms=1_001_000,
        symbol_aliases={},
        retained_routes=retained,
    )
    assert len(candidates) == 1
    assert candidates[0]["buyVolume24hUsdt"] == 100_000
    assert _auto_card_route_observations(candidates)[("ABC", "SF", "binance", "gate")]["reason"] == "volume_below_threshold"


def test_fast_scan_only_keeps_new_routes_above_entry_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_MARKETS", "binanceFuture,gateFuture")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    payload = {
        "data": {
            "binanceFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 100.0, "b": 99.9, "trade24Count": 300_000}],
            ),
            "gateFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 100.5, "b": 100.4, "trade24Count": 300_000}],
            ),
        }
    }

    candidates, _ = scan_pulse_spreads(payloads=[payload], now_ms=1_001_000, symbol_aliases={})
    assert candidates == []

    retained = {("ABC", "FF", "binance", "gate", "", "")}
    candidates, _ = scan_pulse_spreads(
        payloads=[payload],
        now_ms=1_001_000,
        symbol_aliases={},
        retained_routes=retained,
    )
    assert [(item["buyExchange"], item["sellExchange"]) for item in candidates] == [("binance", "gate")]


def test_fast_scan_drops_unsupported_direction_unless_it_is_tracked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_MARKETS", "binanceFuture,bybitFuture")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    payload = {
        "data": {
            "binanceFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 100.0, "b": 99.9, "trade24Count": 300_000}],
            ),
            "bybitFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 103.0, "b": 102.0, "trade24Count": 300_000}],
            ),
        }
    }

    candidates, _ = scan_pulse_spreads(payloads=[payload], now_ms=1_001_000, symbol_aliases={})
    assert candidates == []

    retained = {("ABC", "FF", "binance", "bybit", "", "")}
    candidates, _ = scan_pulse_spreads(
        payloads=[payload],
        now_ms=1_001_000,
        symbol_aliases={},
        retained_routes=retained,
    )
    assert [(item["buyExchange"], item["sellExchange"]) for item in candidates] == [("binance", "bybit")]


def test_editable_auto_card_rules_persist_in_local_subscription_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "astro-spread-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(path))
    update_astro_spread_subscriptions(
        ["binanceSpot", "gateFuture"],
        ff_min_open_spread_pct=1.2,
        sf_min_open_spread_pct=1.6,
        sf_min_short_funding_rate_pct=-0.05,
        sf_okxdex_auto_card_enabled=False,
        confirmations=4,
        max_quote_age_seconds=15,
        exclude_delisted_exchange_cards=False,
        greater_price_alert_pct=2.5,
        price_change_alert_pct=4.0,
        price_change_alert_only_rise=True,
        min_notional_usdt=6,
        max_notional_usdt=40,
    )
    assert spread_scan_ff_min_open_pct() == pytest.approx(1.2)
    assert spread_scan_sf_min_open_pct() == pytest.approx(1.6)
    assert spread_scan_sf_min_short_funding_pct() == pytest.approx(0.0)
    assert spread_scan_sf_okxdex_auto_card_enabled() is False
    assert spread_scan_confirmations() == 4
    assert spread_scan_max_quote_age_seconds() == pytest.approx(15)
    assert spread_scan_exclude_delisted_exchange_cards() is False
    assert astro_greater_price_alert_pct() == pytest.approx(2.5)
    assert astro_price_change_alert_pct() == pytest.approx(4.0)
    assert astro_price_change_alert_only_rise() is True
    status = astro_spread_scanner_status()
    assert status["minNotionalUsdt"] == pytest.approx(6)
    assert status["maxNotionalUsdt"] == pytest.approx(40)
    assert status["autoCardRules"]["sf"]["okxDexRoute"]["autoCardEnabled"] is False


def test_paused_okxdex_sf_auto_card_rule_blocks_creation_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-spread-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_OKXDEX_IDENTITY_VERIFICATION_ENABLED", "0")
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        sf_okxdex_auto_card_enabled=False,
        dex_mapped_assets=[{"symbol": "ABC", "chainIndex": "56", "contractAddress": "0xabc"}],
    )
    candidate = {
        "type": "SF",
        "symbol": "ABC",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellExchange": "gate",
        "sellMarket": "future",
        "openSpreadPct": 2.0,
        "sellFundingRatePct": 0.01,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 1_000_000,
        "dexConfig": {"chainIndex": "56", "contractAddress": "0xabc"},
        "dexMapping": {"status": "confirmed"},
    }

    assert _meets_auto_card_rule(candidate) is False
    decision = scanner_module._candidate_rule_decision(candidate)
    assert decision["primaryReason"] == "sf_okxdex_auto_card_paused"
    assert decision["eligible"] is False

    pair = {"name": "ABC", "type": "SF", "buyEx": "okxdex", "sellEx": "gate"}
    latest, report = revalidate_astro_hot_direct_hit(pair, sdk_config())
    assert latest is None
    assert report["reason"] == "sf_okxdex_auto_card_paused"


def test_blank_new_card_alerts_disable_both_alerts(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "astro-spread-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(path))
    status = update_astro_spread_subscriptions(
        ["binanceSpot", "gateFuture"],
        greater_price_alert_pct=None,
        price_change_alert_pct=None,
    )
    assert status["greaterPriceAlertPct"] is None
    assert status["priceChangeAlertPct"] is None


def test_delisting_rule_removes_only_cards_using_the_announced_exchange(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_EXCLUDE_DELISTED_EXCHANGE_CARDS", "1")
    monkeypatch.setattr(
        "app.astro_spread_scanner._active_delisting_exchange_blocks",
        lambda: {("ABC", "gate")},
    )
    candidates = [
        {"symbol": "ABC", "buyExchange": "binance", "sellExchange": "gate"},
        {"symbol": "ABC", "buyExchange": "binance", "sellExchange": "okx"},
        {"symbol": "XYZ", "buyExchange": "gate", "sellExchange": "okx"},
    ]
    kept, status = _filter_delisted_exchange_candidates(candidates)
    assert kept == candidates[1:]
    assert status["activeBlockCount"] == 1
    assert status["filteredCandidateCount"] == 1
    assert status["items"] == [{"symbol": "ABC", "exchange": "gate"}]


def test_final_revalidation_uses_latest_spread_for_same_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    now_ms = int(__import__("time").time() * 1000)
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_MARKETS", "binanceFuture,gateFuture")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    monkeypatch.setattr("app.astro_spread_scanner._load_pulse_symbol_aliases", lambda: {})
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_ROUNDS", "2")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_INTERVAL_SECONDS", "0.1")
    monkeypatch.setattr("app.astro_spread_scanner.time.sleep", lambda *_args: None)

    def direct_quote(_client, exchange, market_type, _symbol, _aliases):
        if exchange == "binance":
            return {
                "exchange": exchange,
                "market": market_type,
                "rawSymbol": "ABC",
                "bid": 99.9,
                "ask": 100.0,
                "timestamp": now_ms,
                "timestampSource": "exchange",
                "requestDurationMs": 10,
            }
        return {
            "exchange": exchange,
            "market": market_type,
            "rawSymbol": "ABC",
            "bid": 102.0,
            "ask": 102.1,
            "timestamp": now_ms,
            "timestampSource": "response_received",
            "requestDurationMs": 10,
        }

    monkeypatch.setattr("app.astro_spread_scanner._fetch_direct_quote", direct_quote)
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_cex_executable_preflight",
        lambda *_args: cex_preflight(now_ms),
    )
    original = {
        "name": "ABC",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "openPosition": "0.011",
        "_pipeline": {"firstSeenAtMs": now_ms - 2_000, "pulseOpenSpreadPct": 1.1},
    }

    latest, report = revalidate_astro_spread_pair(original, sdk_config())

    assert latest is not None
    assert float(latest["openPosition"]) == pytest.approx(2 * (102 - 100) / (102 + 100))
    assert report["reason"] == "eligible"
    assert report["sourceCount"] == 2
    assert report["roundsPassed"] == 2
    assert len(report["checks"]) == 2
    assert latest["_pipeline"] == original["_pipeline"]


def test_aster_direct_revalidation_uses_fresh_depth_snapshot() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "lastUpdateId": 123,
                "E": 1_787_966_110_179,
                "T": 1_787_966_100_150,
                "bids": [["0.115650", "4847"]],
                "asks": [["0.120470", "4700"]],
            }

    class FakeClient:
        def __init__(self) -> None:
            self.url = ""
            self.params: dict[str, object] = {}

        def get(self, url: str, params: dict[str, object]):
            self.url = url
            self.params = params
            return FakeResponse()

    client = FakeClient()
    before_ms = int(__import__("time").time() * 1000)
    quote = _fetch_direct_quote(client, "aster", "future", "NES", {})

    assert client.url == "https://fapi.asterdex.com/fapi/v1/depth"
    assert client.params == {"symbol": "NESUSDT", "limit": 5}
    assert quote["bid"] == pytest.approx(0.115650)
    assert quote["ask"] == pytest.approx(0.120470)
    assert quote["timestamp"] >= before_ms
    assert quote["timestampSource"] == "response_received_snapshot"
    assert quote["quoteEndpoint"] == "depth"
    assert quote["exchangeEventTimestamp"] == 1_787_966_110_179
    assert quote["exchangeTransactionTimestamp"] == 1_787_966_100_150


def test_pulse_fetch_continues_when_one_source_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    successful_url = scanner_module.PULSE_URLS[0]

    def fake_fetch(url: str) -> dict[str, object]:
        if url == successful_url:
            return {"code": 0, "data": {}}
        raise RuntimeError("temporary source failure")

    monkeypatch.setattr(scanner_module, "_fetch_pulse", fake_fetch)
    payloads, summary = scanner_module._fetch_pulse_payloads()

    assert len(payloads) == 1
    assert summary["successCount"] == 1
    assert summary["failureCount"] == len(scanner_module.PULSE_URLS) - 1


def test_pulse_fetch_uses_local_proxy_without_fallback_when_primary_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_PULSE_SSH_ENABLED", "1")
    expected = [{"code": 0, "data": {}}]
    monkeypatch.setattr(
        scanner_module,
        "_fetch_direct_pulse_payloads",
        lambda: (
            expected,
            {"successCount": 1, "failureCount": 1, "failures": [], "sourceCount": 2},
        ),
    )

    payloads, summary = scanner_module._fetch_pulse_payloads()

    assert payloads == expected
    assert summary["transport"] == "local_proxy"
    assert summary["raceMode"] == "local_proxy_then_cloud"
    assert summary["fallbackUsed"] is False
    assert summary["primaryFailure"] is None
    assert summary["cloudRttMs"] is None


def test_pulse_degraded_log_waits_three_rounds_then_recovers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda name, **_kwargs: events.append(name),
    )
    degraded = {
        "successCount": 2,
        "failureCount": 1,
        "failures": [{"url": "pulse-2", "error": "down"}],
        "primaryFailure": None,
    }
    healthy = {"successCount": 2, "failureCount": 0, "failures": [], "primaryFailure": None}

    scanner_module._log_pulse_source_status(degraded)
    scanner_module._log_pulse_source_status(degraded)
    assert events == []
    scanner_module._log_pulse_source_status(degraded)
    scanner_module._log_pulse_source_status(degraded)
    scanner_module._log_pulse_source_status(healthy)
    scanner_module._log_pulse_source_status(healthy)

    assert events == ["astro_pulse_source_degraded", "astro_pulse_sources_recovered"]


def test_pulse_outage_logs_only_start_threshold_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda name, **_kwargs: events.append(name),
    )

    first = scanner_module._record_pulse_failure(scanner_module.PulseUnavailableError("down"))
    second = scanner_module._record_pulse_failure(scanner_module.PulseUnavailableError("down"))
    third = scanner_module._record_pulse_failure(scanner_module.PulseUnavailableError("down"))
    fourth = scanner_module._record_pulse_failure(scanner_module.PulseUnavailableError("down"))
    recovered = scanner_module._record_pulse_success(
        {"transport": "mac_direct_no_proxy", "fallbackUsed": False}
    )

    assert first["cleanupPaused"] is True
    assert second["pulseHealthy"] is True
    assert third["pulseHealthy"] is False
    assert fourth["pulseConsecutiveFailureCount"] == 4
    assert recovered["cleanupPaused"] is False
    assert events == [
        "astro_pulse_outage_started",
        "astro_pulse_watchdog_unhealthy",
        "astro_pulse_outage_recovered",
    ]


def test_total_pulse_outage_skips_card_creation_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scanner_module,
        "_fetch_pulse_payloads",
        lambda **_kwargs: (_ for _ in ()).throw(scanner_module.PulseUnavailableError("all transports down")),
    )
    monkeypatch.setattr(
        scanner_module,
        "schedule_astro_pairs",
        lambda *_args, **_kwargs: pytest.fail("card scheduling and cleanup must be skipped"),
    )
    monkeypatch.setattr(scanner_module, "append_system_runtime_event", lambda *_args, **_kwargs: None)

    status = scanner_module.run_astro_spread_scan_once()

    assert status["lastError"]
    assert status["candidateCount"] == 0
    assert status["confirmedCount"] == 0
    assert status["pulseSources"]["cleanupPaused"] is True


def test_revalidation_failure_cooldown_allows_early_retry_after_spread_expands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_REVALIDATION_RETRY_SECONDS", "10")
    monkeypatch.setenv("ASTRO_REVALIDATION_RETRY_IMPROVEMENT_PCT_POINTS", "0.15")
    scanner_module._revalidation_failures.clear()
    pair = {
        "name": "ABC",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "_pipeline": {"pulseOpenSpreadPct": 1.10},
    }
    scanner_module._record_revalidation_outcome(
        pair,
        passed=False,
        report={"reason": "below_threshold_or_rule_failed", "durationMs": 10},
    )

    base = {
        "symbol": "ABC",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "openSpreadPct": 1.20,
    }
    assert scanner_module._revalidation_retry_allowed(base) is False
    assert scanner_module._revalidation_retry_allowed({**base, "openSpreadPct": 1.25}) is True
    scanner_module._revalidation_failures.clear()


def test_final_revalidation_rejects_route_after_spread_collapses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_ROUNDS", "2")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_INTERVAL_SECONDS", "0.1")
    monkeypatch.setattr("app.astro_spread_scanner.time.sleep", lambda *_args: None)
    original = {
        "name": "ABC",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
        "openPosition": "0.015",
    }

    refreshed = {**original, "openPosition": "0.02"}
    checks = iter(
        [
            (refreshed, {"reason": "eligible", "latestOpenSpreadPct": 2.0}),
            (None, {"reason": "below_threshold_or_rule_failed", "latestOpenSpreadPct": 0.5}),
        ]
    )
    monkeypatch.setattr("app.astro_spread_scanner._fetch_direct_route_once", lambda *_args, **_kwargs: next(checks))
    latest, report = revalidate_astro_spread_pair(original, sdk_config())

    assert latest is None
    assert report["reason"] == "below_threshold_or_rule_failed"
    assert report["latestOpenSpreadPct"] == pytest.approx(0.5)
    assert report["roundsPassed"] == 1


def test_okxdex_final_revalidation_rejects_repeated_snapshot(monkeypatch):
    monkeypatch.setattr(scanner_module, "spread_scan_dex_auto_card_enabled", lambda ex: True)
    monkeypatch.setattr(scanner_module, "spread_scan_sf_route_min_open_pct", lambda ex: 1.5)
    monkeypatch.setattr(scanner_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(scanner_module.time, "sleep", lambda _: None)
    pair = {"name": "ABC", "type": "SF", "buyEx": "okxdex", "sellEx": "gate"}
    report = {"reason": "eligible", "latestOpenSpreadPct": 2.0,
              "okxdexExecutablePreflight": {"source": "real_quote"},
              "buyQuote": {"timestamp": 100000}, "sellQuote": {"timestamp": 100000}}
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", lambda *a, **kw: (pair, report))
    latest, final_report = revalidate_astro_spread_pair(pair, sdk_config())
    assert latest is None
    assert final_report["reason"] == "okxdex_quote_not_advanced"
    assert final_report["roundsPassed"] == 1
    assert final_report["roundsRequired"] == 3


def test_direct_revalidation_rejects_quotes_over_one_second_apart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    now_ms = int(__import__("time").time() * 1000)
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_MAX_QUOTE_AGE_SECONDS", "3")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_MAX_SKEW_SECONDS", "1")
    monkeypatch.setattr("app.astro_spread_scanner._load_pulse_symbol_aliases", lambda: {})

    def skewed_quote(_client, exchange, market_type, _symbol, _aliases):
        is_buy = exchange == "binance"
        return {
            "exchange": exchange,
            "market": market_type,
            "rawSymbol": "ABC",
            "bid": 99.9 if is_buy else 102.0,
            "ask": 100.0 if is_buy else 102.1,
            "timestamp": now_ms if is_buy else now_ms - 1_500,
            "timestampSource": "exchange",
            "requestDurationMs": 10,
        }

    monkeypatch.setattr("app.astro_spread_scanner._fetch_direct_quote", skewed_quote)
    latest, report = _fetch_direct_route_once(
        {"name": "ABC", "type": "FF", "buyEx": "binance", "sellEx": "gate"},
        sdk_config(),
    )
    assert latest is None
    assert report["reason"] == "direct_quote_time_skew"
    assert report["quoteSkewSeconds"] == pytest.approx(1.5)
    assert report["timestampSkewCheckApplied"] is True


def test_okxdex_revalidation_uses_fresh_executable_quote_instead_of_old_pulse_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("app.astro_sf_funding.check", lambda *a, **kw: {"eligible": True, "status": "non_negative", "read": True})
    now_ms = int(__import__("time").time() * 1000)
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_MAX_QUOTE_AGE_SECONDS", "3")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_OKXDEX_MAX_QUOTE_AGE_SECONDS", "20")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_MAX_SKEW_SECONDS", "1")
    monkeypatch.setattr("app.astro_spread_scanner._load_pulse_symbol_aliases", lambda: {})

    def direct_quote(_client, exchange, market_type, _symbol, _aliases, *_extra):
        is_dex = exchange == "okxdex"
        quote = {
            "exchange": exchange,
            "market": market_type,
            "rawSymbol": "ABC",
            "bid": 99.9 if is_dex else 102.0,
            "ask": 100.0 if is_dex else 102.1,
            "timestamp": now_ms - (12_000 if is_dex else 500),
            "timestampSource": "exchange",
            "requestDurationMs": 10,
        }
        if is_dex:
            quote["dexConfig"] = {
                "name": "ABC",
                "chainIndex": "56",
                "contractAddress": "0xabc",
                "quote": "USDT",
                "slippage": "1",
            }
        return quote

    monkeypatch.setattr("app.astro_spread_scanner._fetch_direct_quote", direct_quote)
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_cex_executable_preflight",
        lambda *_args: cex_preflight(now_ms),
    )
    monkeypatch.setattr(
        "app.astro_spread_scanner._verify_okxdex_candidate_identities",
        lambda candidates, eligible_only=False: (
            [{**candidates[0], "dexIdentity": {"status": "verified"}}],
            {"verifiedCount": 1},
        ),
    )
    monkeypatch.setattr(
        "app.astro_spread_scanner._dex_mapping_confirmation",
        lambda _candidate: {"status": "confirmed", "mode": "exact"},
    )
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_okxdex_executable_preflight",
        lambda *_args: {
            "dexQuotedAverageCost": 100.0,
            "dexConservativeAverageCost": 100.2,
            "quotedAt": now_ms,
            "netExecutableSpreadPct": 1.78,
            "futuresDepth": {"averageSellPrice": 102.0, "timestamp": now_ms},
        },
    )

    pair = {
        "name": "ABC",
        "type": "SF",
        "buyEx": "okxdex",
        "sellEx": "gate",
        "_dexConfig": {"chainIndex": "56", "contractAddress": "0xabc"},
        "_buyVolume24hUsdt": 1_000_000,
        "_sellVolume24hUsdt": 1_000_000,
    }
    latest, report = _fetch_direct_route_once(pair, sdk_config())

    assert latest is not None
    assert report["reason"] == "eligible"
    assert report["quoteSkewSeconds"] == pytest.approx(0)
    assert report["timestampSkewCheckApplied"] is False
    assert report["quoteAgeLimitsSeconds"] == {"buy": 5.0, "sell": 3.0}
    assert report["pulseDiscoveryOnly"] is True


def test_okxdex_revalidation_fails_closed_when_executable_quote_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    now_ms = int(__import__("time").time() * 1000)
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_MAX_QUOTE_AGE_SECONDS", "3")
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_OKXDEX_MAX_QUOTE_AGE_SECONDS", "20")
    monkeypatch.setattr("app.astro_spread_scanner._load_pulse_symbol_aliases", lambda: {})

    def direct_quote(_client, exchange, market_type, _symbol, _aliases, *_extra):
        is_dex = exchange == "okxdex"
        quote = {
            "exchange": exchange,
            "market": market_type,
            "rawSymbol": "ABC",
            "bid": 99.9 if is_dex else 102.0,
            "ask": 100.0 if is_dex else 102.1,
            "timestamp": now_ms - (21_000 if is_dex else 500),
            "timestampSource": "exchange",
            "requestDurationMs": 10,
        }
        if is_dex:
            quote["dexConfig"] = {
                "name": "ABC",
                "chainIndex": "56",
                "contractAddress": "0xabc",
            }
        return quote

    monkeypatch.setattr("app.astro_spread_scanner._fetch_direct_quote", direct_quote)
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_okxdex_executable_preflight",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("no executable DEX route")),
    )
    latest, report = _fetch_direct_route_once(
        {
            "name": "ABC",
            "type": "SF",
            "buyEx": "okxdex",
            "sellEx": "gate",
            "_dexConfig": {"chainIndex": "56", "contractAddress": "0xabc"},
        },
        sdk_config(),
    )

    assert latest is None
    assert report["reason"] == "okxdex_executable_quote_unavailable"
    assert "no executable DEX route" in report["error"]


def test_okxdex_executable_preflight_uses_card_amount_and_same_quantity_futures_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_MIN_NOTIONAL_USDT", "6")
    monkeypatch.setenv("ASTRO_OKXDEX_QUOTE_NOTIONAL_USDT", "10")
    monkeypatch.setenv("ASTRO_OKXDEX_SLIPPAGE_PCT", "1")
    monkeypatch.setattr(scanner_module, "astro_max_notional_usdt", lambda _config: 20.0)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "chainIndex": "56",
                "contractAddress": "0xabc",
                "fromAmount": "20",
                "toAmount": "10",
                "tradeFeeUsd": "0.06",
                "quotedAt": int(time.time() * 1000),
                "priceImpactPercent": "-0.4",
                "estimateGasFee": "12345",
                "routeDexes": ["PancakeSwap"],
            }

    class FakeClient:
        def get(self, *_args, **_kwargs):
            assert _kwargs["params"]["amountUsdt"] == 20
            return FakeResponse()

    monkeypatch.setattr(
        scanner_module,
        "_fetch_futures_bid_depth_execution",
        lambda _client, exchange, symbol, _aliases, quantity: {
            "source": "exchange_public_depth",
            "exchange": exchange,
            "targetQuantity": quantity,
            "filledQuantity": quantity,
            "proceedsUsdt": quantity * 2.2,
            "averageSellPrice": 2.2,
            "timestamp": int(time.time() * 1000),
        },
    )

    result = _fetch_okxdex_executable_preflight(
        FakeClient(),
        "ABC",
        {"chainIndex": "56", "contractAddress": "0xABC", "slippage": "1"},
        "binance",
        {},
        sdk_config(),
    )

    assert result["quoteNotionalUsdt"] == 20
    assert result["tokenQuantity"] == 10
    assert result["futuresDepth"]["targetQuantity"] == 10
    assert result["slippagePct"] == pytest.approx(1.0)
    assert result["slippageAppliedAsLimitOnly"] is True
    assert result["slippageReserveUsd"] == pytest.approx(0.0)
    assert result["networkFeeUsd"] == pytest.approx(0.06)
    assert result["networkFeeReserveUsd"] == pytest.approx(0.06)
    assert result["networkFeeAmortizedAtUsdt"] == pytest.approx(20.0)
    assert result["dexActualCostUsdt"] == pytest.approx(20.06)
    assert result["dexConservativeAverageCost"] == pytest.approx(2.006)
    assert result["netExecutableSpreadPct"] > 0


def test_sf_direct_revalidation_uses_targeted_funding_gate_without_full_market_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("app.astro_sf_funding.check", lambda *a, **kw: {"eligible": True, "status": "non_negative", "read": True})
    now_ms = int(__import__("time").time() * 1000)
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    monkeypatch.setattr("app.astro_spread_scanner._load_pulse_symbol_aliases", lambda: {})

    def direct_quote(_client, exchange, market_type, _symbol, _aliases):
        spot = exchange == "binance"
        return {
            "exchange": exchange,
            "market": market_type,
            "rawSymbol": "ABC",
            "bid": 99.9 if spot else 102.0,
            "ask": 100.0 if spot else 102.1,
            "timestamp": now_ms,
            "timestampSource": "exchange",
            "requestDurationMs": 1,
        }

    pair = {
        "name": "ABC",
        "type": "SF",
        "buyEx": "binance",
        "sellEx": "gate",
        "_buyVolume24hUsdt": 500_000,
        "_sellVolume24hUsdt": 500_000,
    }
    monkeypatch.setattr("app.astro_spread_scanner._fetch_direct_quote", direct_quote)
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_cex_executable_preflight",
        lambda *_args: cex_preflight(now_ms),
    )
    latest, report = _fetch_direct_route_once(pair, sdk_config())
    assert latest is not None
    assert report["fundingIncluded"] is True
    assert "sellFunding" not in report
    from app import crypto
    monkeypatch.setattr(crypto, "fetch_fs_market_quote", lambda *a, **k: pytest.fail("funding read removed"))
    latest, report = _fetch_direct_route_once(pair, sdk_config())
    assert latest is not None and report["fundingIncluded"] is True


def test_stale_market_leg_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_EXCHANGES", "binance,gate")
    monkeypatch.setenv("ASTRO_SPREAD_MAX_QUOTE_AGE_SECONDS", "20")
    payload = {
        "data": {
            "binanceSpot": market(1_000_000, [{"name": "ABCUSDT", "a": 100.0, "b": 99.0}]),
            "gateFuture": market(970_000, [{"name": "ABCUSDT", "a": 103.0, "b": 102.0}]),
        }
    }
    candidates, count = scan_pulse_spreads([payload], now_ms=1_001_000)
    assert count == 1
    assert candidates == []




def test_market_level_subscription_excludes_aster_spot_and_keeps_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_MARKETS", "asterFuture,hlFuture")
    monkeypatch.delenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", raising=False)
    monkeypatch.delenv("STOCK_REVIEW_DATA_DIR", raising=False)
    payload = {
        "data": {
            "asterSpot": market(1_000_000, [{"name": "ABCUSDT", "a": 100.0, "b": 99.0}]),
            "asterFuture": market(1_000_000, [{"name": "ABCUSDT", "a": 103.0, "b": 102.0}]),
            "hlFuture": market(1_000_000, [{"name": "ABCUSDT", "a": 104.0, "b": 103.0}]),
        }
    }
    candidates, count = scan_pulse_spreads([payload], now_ms=1_001_000)
    assert count == 2
    assert all(item["type"] == "FF" for item in candidates)
    assert all(item["buyMarket"] == "future" and item["sellMarket"] == "future" for item in candidates)


def test_subscription_update_is_saved_and_reflected_in_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    status = update_astro_spread_subscriptions(
        ["binanceSpot", "gateFuture"],
        delete_rearm_pct=20,
        delete_pullback_pct_points=0.5,
    )
    assert status["subscriptions"] == ["binanceSpot", "gateFuture"]
    assert status["subscriptionSource"] == "saved"
    assert status["deleteRearmPct"] == 20
    assert status["deletePullbackPctPoints"] == 0.5
    assert astro_spread_scanner_status()["subscriptions"] == ["binanceSpot", "gateFuture"]


def test_dex_manual_mapping_uses_exchange_chain_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    status = update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[
            {"symbol": "abc", "chainIndex": "56", "contractAddress": "0xABC"},
        ],
    )

    assert status["dexMappedAssets"][0]["symbol"] == "ABC"
    assert status["dexMappedAssets"][0]["chainIndex"] == "56"
    assert status["dexMappedAssets"][0]["chainLabel"] == "BNB Smart Chain"
    assert status["dexMappedAssets"][0]["contractAddress"] == "0xabc"
    assert status["dexMappedAssets"][0]["mappingMode"] == "exchange_chain_contract"
    assert status["dexMappedAssets"][0]["autoCreateEligible"] is True


def test_dex_unmapped_candidate_is_reported_and_cannot_build_until_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[],
    )
    candidate = {
        "type": "SF",
        "symbol": "NEW",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellExchange": "gate",
        "sellMarket": "future",
        "openSpreadPct": 1.8,
        "sellFundingRatePct": 0.0,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 1_000_000,
        "dexConfig": {"chainIndex": "56", "contractAddress": "0xABC"},
        "dexIdentity": {"status": "verified"},
    }

    annotated, summary = _annotate_okxdex_manual_mappings([candidate])
    assert annotated[0]["dexMapping"]["status"] == "missing"
    assert summary["missingCount"] == 1
    assert _meets_auto_card_rule(annotated[0]) is False

    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[{"symbol": "NEW", "chainIndex": "56", "contractAddress": "0xabc"}],
    )
    annotated, summary = _annotate_okxdex_manual_mappings([candidate])
    assert annotated[0]["dexMapping"]["status"] == "confirmed"
    assert summary["missingCount"] == 0
    assert _meets_auto_card_rule(annotated[0]) is True


def test_dex_mapping_prompt_remains_until_symbol_mapping_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[],
    )
    missing = {
        "symbol": "NEW",
        "chainIndex": "56",
        "chainLabel": "BNB Smart Chain",
        "contractAddress": "0xABC",
        "targetExchanges": ["gate"],
        "maxOpenSpreadPct": 1.8,
    }

    assert _merge_pending_dex_mapping_items([missing], []) == [{
        "symbol": "NEW",
        "chainIndex": "56",
        "chainLabel": "BNB Smart Chain",
        "contractAddress": "0xabc",
        "targetExchanges": ["gate"],
        "maxOpenSpreadPct": 1.8,
        "maxVolume24hUsdt": None,
        "reason": "请在 Astro 配置后确认链和合约地址",
        "exchange": "okxdex",
    }]

    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[{"symbol": "NEW", "chainIndex": "56", "contractAddress": "0xabc"}],
    )
    assert _merge_pending_dex_mapping_items([missing], []) == []


def test_symbol_only_dex_mapping_is_preserved_but_requires_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[{"symbol": "PEPE"}],
    )
    candidate = {
        "type": "SF",
        "symbol": "PEPE",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellExchange": "gate",
        "sellMarket": "future",
        "openSpreadPct": 1.8,
        "sellFundingRatePct": 0.0,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 1_000_000,
        "dexConfig": {"chainIndex": "1", "contractAddress": "0xabc"},
        "dexIdentity": {"status": "verified"},
    }

    annotated, summary = _annotate_okxdex_manual_mappings([candidate])

    assert annotated[0]["dexMapping"]["status"] == "missing"
    assert summary["missingCount"] == 1
    assert _meets_auto_card_rule(annotated[0]) is False
    status = astro_spread_scanner_status()
    assert status["autoCardRules"]["sf"]["okxDexRoute"]["mappedAssetCount"] == 0
    assert status["autoCardRules"]["sf"]["okxDexRoute"]["legacyDisplayOnlyCount"] == 1
    assert status["dexMappedAssets"][0]["mappingMode"] == "exchange_chain_contract"
    assert status["dexMappedAssets"][0]["autoCreateEligible"] is False


def test_dex_mapping_rejects_different_chain_or_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["okxdexSpot", "gateFuture"],
        dex_mapped_assets=[
            {"symbol": "NEW", "chainIndex": "56", "contractAddress": "0xabc"},
        ],
    )
    candidate = {
        "type": "SF",
        "symbol": "NEW",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellExchange": "gate",
        "sellMarket": "future",
        "openSpreadPct": 1.8,
        "sellFundingRatePct": 0.0,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 1_000_000,
        "dexConfig": {"chainIndex": "1", "contractAddress": "0xdef"},
        "dexIdentity": {"status": "verified"},
    }

    annotated, summary = _annotate_okxdex_manual_mappings([candidate])

    assert annotated[0]["dexMapping"]["status"] == "missing"
    assert summary["missingCount"] == 1
    assert _meets_auto_card_rule(annotated[0]) is False


def test_volume_threshold_filters_a_known_low_volume_leg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["binanceSpot", "gateFuture"],
        min_volume_usdt=10_000,
        blocked_pairs=[],
    )
    payload = {
        "data": {
            "binanceSpot": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 100.0, "b": 99.0, "trade24Count": 9_999}],
            ),
            "gateFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 103.0, "b": 102.0, "trade24Count": 20_000}],
            ),
        }
    }
    candidates, _ = scan_pulse_spreads([payload], now_ms=1_001_000)
    assert candidates == []


def test_missing_volume_is_fail_closed_for_card_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_CONFIRMATIONS", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    _hits.clear()
    common = {
        "key": "FF:ABC:binance:gate",
        "symbol": "ABC",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "openSpreadPct": 1.5,
    }
    assert _confirmed_candidates([{**common, "sellVolume24hUsdt": 300_000}]) == []
    assert _confirmed_candidates([{**common, "buyVolume24hUsdt": 300_000}]) == []
    _hits.clear()
    confirmed = _confirmed_candidates([
        {**common, "buyVolume24hUsdt": 200_000, "sellVolume24hUsdt": 200_000}
    ])
    assert [item["symbol"] for item in confirmed] == ["ABC"]
    _hits.clear()


def test_confirmation_keeps_multiple_routes_for_the_same_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_CONFIRMATIONS", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    monkeypatch.setattr(scanner_module, "spread_scan_confirmations", lambda: 1)
    _hits.clear()
    scanner_module._revalidation_failures.clear()
    common = {"symbol": "ABC", "type": "FF", "openSpreadPct": 1.5}
    confirmed = _confirmed_candidates([
        {**common, "key": "FF:ABC:binance:gate", "buyExchange": "binance", "sellExchange": "gate"},
        {**common, "key": "FF:ABC:okx:gate", "buyExchange": "okx", "sellExchange": "gate"},
    ])
    assert {(item["buyExchange"], item["sellExchange"]) for item in confirmed} == {
        ("binance", "gate"),
        ("okx", "gate"),
    }
    _hits.clear()
    scanner_module._revalidation_failures.clear()


def test_blocked_pair_only_removes_the_selected_market_leg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["binanceSpot", "gateFuture", "bybitFuture"],
        min_volume_usdt=0,
        blocked_pairs=[{"marketKey": "gateFuture", "symbol": "abc"}],
    )
    payload = {
        "data": {
            "binanceSpot": market(1_000_000, [{"name": "ABCUSDT", "a": 100.0, "b": 99.0}]),
            "gateFuture": market(1_000_000, [{"name": "ABCUSDT", "a": 103.0, "b": 102.0}]),
            "bybitFuture": market(1_000_000, [{"name": "ABCUSDT", "a": 104.0, "b": 103.0}]),
        }
    }
    candidates, _ = scan_pulse_spreads([payload], now_ms=1_001_000)
    assert candidates
    assert all(item["buyExchange"] != "gate" and item["sellExchange"] != "gate" for item in candidates)


def test_submit_guard_rechecks_exact_market_leg_after_candidate_was_queued(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["gateSpot", "gateFuture", "bybitFuture"],
        min_volume_usdt=0,
        blocked_pairs=[{"marketKey": "bybitFuture", "symbol": "scrt"}],
    )

    blocked, report = scanner_module.astro_spread_pair_submit_guard(
        {"name": "SCRT", "type": "SF", "buyEx": "gate", "sellEx": "bybit"}
    )
    allowed_other_leg, other_report = scanner_module.astro_spread_pair_submit_guard(
        {"name": "SCRT", "type": "SF", "buyEx": "gate", "sellEx": "gate"}
    )

    assert blocked is False
    assert report == {
        "reason": "blocked_pair",
        "symbol": "SCRTUSDT",
        "matchedPairs": [
            {"marketKey": "bybitFuture", "symbol": "SCRTUSDT", "side": "sell"}
        ],
    }
    assert allowed_other_leg is True
    assert other_report == {"reason": "allowed"}


def test_listing_hint_cannot_register_a_directionally_blocked_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_module._hot_routes.clear()
    scanner_module._hot_recent_hits.clear()
    monkeypatch.setattr(
        scanner_module,
        "_spread_block_rule_sets",
        lambda: ({("gateFuture", "ABCUSDT")}, set()),
    )
    monkeypatch.setattr(scanner_module, "build_astro_spread_pairs", lambda candidate, _config: [{
        "name": candidate["symbol"],
        "type": candidate["type"],
        "buyEx": candidate["buyExchange"],
        "sellEx": candidate["sellExchange"],
    }])
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _pair: None)
    hint = {
        "symbol": "ABC",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "openSpreadPct": 2.0,
        "announcementRouteHint": True,
    }

    summary = scanner_module._register_hot_candidates([hint], {"ABC"}, object())

    assert summary["routeCount"] == 0
    assert not scanner_module._hot_routes


def test_globally_blocked_coin_is_removed_from_every_market(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["binanceSpot", "gateFuture"],
        min_volume_usdt=0,
        blocked_pairs=[],
        blocked_coins=["abc"],
    )
    payload = {
        "data": {
            "binanceSpot": market(1_000_000, [{"name": "ABCUSDT", "a": 100.0, "b": 99.0}]),
            "gateFuture": market(1_000_000, [{"name": "ABCUSDT", "a": 103.0, "b": 102.0}]),
        }
    }
    candidates, _ = scan_pulse_spreads(payloads=[payload], now_ms=1_001_000, symbol_aliases={})
    assert candidates == []


def test_exchange_symbol_aliases_join_different_names_and_normalize_quote_scale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["binanceSpot", "bybitFuture"],
        min_volume_usdt=0,
        blocked_pairs=[],
        blocked_coins=[],
    )
    payload = {
        "data": {
            "binanceSpot": market(1_000_000, [{"name": "NESAUSDT", "a": 1.00, "b": 0.99}]),
            "bybitFuture": market(1_000_000, [{"name": "1000NESUSDT", "a": 1030.0, "b": 1020.0}]),
        }
    }
    aliases = {("bybit", "future", "1000NES"): ("NESA", 1000.0)}
    candidates, _ = scan_pulse_spreads(payloads=[payload], now_ms=1_001_000, symbol_aliases=aliases)
    sf = next(item for item in candidates if item["type"] == "SF")
    assert sf["symbol"] == "NESA"
    assert sf["buyExchange"] == "binance"
    assert sf["sellExchange"] == "bybit"
    assert sf["openSpreadPct"] == pytest.approx(2 * (1.02 - 1.00) / (1.02 + 1.00) * 100)


def test_confirmation_rejects_spreads_above_astro_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_SPREAD_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_MAX_OPEN_PCT", "10")
    monkeypatch.setenv("ASTRO_SPREAD_CONFIRMATIONS", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    _hits.clear()
    candidates = [
        {"key": "FF:BAD", "symbol": "BAD", "type": "FF", "buyExchange": "binance", "sellExchange": "okx", "openSpreadPct": 53.0},
        {"key": "FF:OK", "symbol": "OK", "type": "FF", "buyExchange": "binance", "sellExchange": "okx", "openSpreadPct": 2.0},
    ]
    confirmed = _confirmed_candidates(candidates)
    assert [item["symbol"] for item in confirmed] == ["OK"]
    _hits.clear()


def test_ff_auto_card_rule_is_strict_and_allows_only_supported_direct_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_ff_min_open_pct", lambda: 1.0)
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    common = {"type": "FF", "buyExchange": "binance", "sellExchange": "okx"}
    assert _meets_auto_card_rule({**common, "openSpreadPct": 1.0001}) is True
    assert _meets_auto_card_rule({**common, "openSpreadPct": 1.0}) is False
    assert _meets_auto_card_rule({**common, "sellExchange": "bybit", "openSpreadPct": 2.0}) is False
    assert _meets_auto_card_rule({**common, "sellExchange": "hl", "openSpreadPct": 2.0}) is False


def test_candidate_rule_decision_excludes_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "_read_saved_subscription_payload", lambda: {})
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.2")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    candidate = {
        "key": "SF:ABC:binance:gate",
        "symbol": "ABC",
        "type": "SF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "buyMarket": "spot",
        "sellMarket": "future",
        "openSpreadPct": 1.8,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 2_000_000,
        "sellFundingRatePct": -0.01,
    }

    decision = scanner_module._candidate_rule_decision(candidate)

    assert decision["eligible"] is True
    assert decision["primaryReason"] == "eligible"
    assert decision["values"]["openSpreadPct"] == pytest.approx(1.8)
    assert decision["thresholds"]["minOpenSpreadPctExclusive"] == pytest.approx(1.2)






def test_decision_audit_only_records_routes_above_base_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "_read_saved_subscription_payload", lambda: {})
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scanner_module,
        "_append_decision_audit",
        lambda _identity, *, stage, decision, details: recorded.append((stage, decision)) or True,
    )
    below = {
        "key": "FF:LOW:binance:gate",
        "symbol": "LOW",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "openSpreadPct": 0.9,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 1_000_000,
    }
    above = {
        "key": "FF:HIGH:binance:gate",
        "symbol": "HIGH",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "openSpreadPct": 1.2,
        "buyVolume24hUsdt": 1_000_000,
        "sellVolume24hUsdt": 1_000_000,
        "quoteSource": "astro_pulse_aggregate",
        "quoteAt": 1_000_000,
        "quoteSkewSeconds": 0.25,
        "buyQuote": {"exchange": "binance", "market": "future", "ask": 100.0, "timestamp": 1_000_000},
        "sellQuote": {"exchange": "gate", "market": "future", "bid": 101.2, "timestamp": 1_000_250},
    }

    summary = scanner_module._record_candidate_decision_audit([below, above], [above], [above])

    assert recorded == [("hot_watch_registration", "registered_for_direct_check")]
    assert summary["aboveBaseThresholdCount"] == 1
    assert summary["blockedCount"] == 0

    decision = scanner_module._candidate_rule_decision(above)
    assert decision["values"]["quoteSource"] == "astro_pulse_aggregate"
    assert decision["values"]["quoteSkewSeconds"] == pytest.approx(0.25)
    assert decision["values"]["buyQuote"]["ask"] == pytest.approx(100.0)
    assert decision["values"]["sellQuote"]["bid"] == pytest.approx(101.2)


def _degraded_test_item() -> tuple[tuple[str, ...], dict[str, object]]:
    identity = ("ABC", "FF", "binance", "gate", "future", "future")
    candidate = {
        "symbol": "ABC",
        "type": "FF",
        "buyExchange": "binance",
        "sellExchange": "gate",
        "openSpreadPct": 2.0,
    }
    item = {
        "identity": identity,
        "candidate": candidate,
        "reasons": ["pulse_above_threshold"],
        "pair": {"name": "ABC", "type": "FF", "buyEx": "binance", "sellEx": "gate"},
    }
    return identity, item


def test_api_degraded_fallback_requires_all_pulse_sources_and_three_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, item = _degraded_test_item()
    monkeypatch.setattr(scanner_module, "_meets_auto_card_rule", lambda _candidate: True)
    monkeypatch.setattr(scanner_module, "_notify_api_degraded_fault", lambda **_kwargs: {"attempted": True})
    now_iso = scanner_module.datetime.now(scanner_module.timezone.utc).isoformat()
    scanner_module._state.update({
        "lastScanAt": now_iso,
        "pulseSourceSuccessCount": 1,
        "pulseSourceFailureCount": 1,
        "pulseHealthy": True,
    })
    transport_report = {
        "reason": "cex_executable_depth_unavailable",
        "error": "Proxy connection timed out",
    }

    pair, report = scanner_module._register_api_degraded_failure(identity, item, transport_report)
    assert pair is None
    assert report["reason"] == "api_unverified_card_disabled"

    scanner_module._state.update({
        "pulseSourceSuccessCount": 2,
        "pulseSourceFailureCount": 0,
    })
    for expected_count in (1, 2):
        pair, report = scanner_module._register_api_degraded_failure(identity, item, transport_report)
        assert pair is None
        assert report["reason"] == "api_unverified_card_disabled"

    pair, report = scanner_module._register_api_degraded_failure(identity, item, transport_report)
    assert pair is None
    assert report["eligible"] is False


def test_api_degraded_fallback_never_accepts_semantic_api_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, item = _degraded_test_item()
    monkeypatch.setattr(scanner_module, "_meets_auto_card_rule", lambda _candidate: True)
    scanner_module._state.update({
        "lastScanAt": scanner_module.datetime.now(scanner_module.timezone.utc).isoformat(),
        "pulseSourceSuccessCount": 2,
        "pulseSourceFailureCount": 0,
        "pulseHealthy": True,
    })

    pair, report = scanner_module._register_api_degraded_failure(
        identity,
        item,
        {"reason": "spread_below_threshold", "error": "latest spread 0.2%"},
    )

    assert pair is None
    assert report["reason"] == "api_unverified_card_disabled"
    assert identity not in scanner_module._api_degraded_route_failures


def test_api_degraded_notification_is_throttled_per_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.notifications as notifications_module

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        notifications_module,
        "send_bark_or_log",
        lambda *args, **kwargs: calls.append(kwargs) or ("ok", None),
    )
    kwargs = {
        "sources": ["gate"],
        "fault_origin": "tencent_cloud",
        "symbol": "ABC",
        "pair_type": "FF",
        "buy_exchange": "binance",
        "sell_exchange": "gate",
        "error": "proxy connection timed out",
    }

    first = scanner_module._notify_api_degraded_fault(**kwargs)
    second = scanner_module._notify_api_degraded_fault(**kwargs)
    assert first == second == {"attempted": False, "muted": True, "sources": ["gate"]}
    assert calls == []  # All legacy per-source paths are muted; only summaries may send.


def test_local_api_fault_notification_is_muted(monkeypatch):
    import app.notifications as notifications_module
    monkeypatch.setattr(notifications_module, "send_bark_or_log", lambda *a, **kw: pytest.fail("local fault must not push"))
    report = scanner_module._notify_api_degraded_fault(
        sources=["gate"], symbol="ABC", pair_type="FF",
        buy_exchange="binance", sell_exchange="gate", error="SSL timeout",
    )
    assert report == {"attempted": False, "muted": True, "sources": ["gate"]}
    assert scanner_module._api_notification_executor is None


def test_slow_fault_push_never_ages_or_blocks_fallback_evidence(monkeypatch):
    import app.notifications as notifications_module
    entered, release = threading.Event(), threading.Event()
    def slow_push(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return "ok", None
    monkeypatch.setattr(notifications_module, "send_bark_or_log", slow_push)
    monkeypatch.setattr(scanner_module, "_meets_auto_card_rule", lambda _: True)
    monkeypatch.setattr(scanner_module, "_pulse_dual_source_healthy", lambda: (True, {}))
    monkeypatch.setattr(scanner_module, "astro_spread_pair_submit_guard", lambda _: (True, {}))
    monkeypatch.setattr(scanner_module, "_record_revalidation_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(scanner_module, "spread_api_degraded_failure_threshold", lambda: 1)
    identity, item = _degraded_test_item()
    clock = [time.time()]
    monkeypatch.setattr(scanner_module.time, "time", lambda: clock[0])
    route_clock = [0.0]
    scanner_module._route_policy.clock = lambda: route_clock[0]
    try:
        started = time.monotonic()
        for value in range(0, 21, 2):
            route_clock[0] = value
            scanner_module._finish_cloud_observation(item["pair"], None, {"reason": "cex_executable_depth_unavailable", "error": "gate connection timed out"})
        scanner_module._queue_route_alerts()
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        assert scanner_module._api_degraded_runtime["lastPushStatus"] == "queued"
        accepted, report = scanner_module.revalidate_astro_api_degraded_pair(item["pair"], sdk_config())
        assert accepted is None
        assert report["reason"] == "api_unverified_card_disabled"
        # A delayed push cannot make Pulse-only evidence eligible.
        clock[0] += 4
        accepted, report = scanner_module.revalidate_astro_api_degraded_pair(item["pair"], sdk_config())
        assert accepted is None
        assert report["reason"] == "api_unverified_card_disabled"
    finally:
        release.set()


def test_api_recovery_requires_two_consecutive_successful_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_API_RECOVERY_PROBE_SUCCESSES", "2")
    probe_clock = [100.0]
    monkeypatch.setattr(scanner_module.time, "monotonic", lambda: probe_clock[0])
    scanner_module._api_degraded_incidents["gate"] = {
        "source": "gate",
        "startedAt": "2026-09-04T00:00:00+00:00",
        "failureCount": 1,
        "affectedSymbols": {"ABC"},
    }

    first = scanner_module._record_api_recovery_probe_result(
        "gate", success=True, error=None, duration_ms=12.0
    )
    assert first is False
    assert "gate" in scanner_module._api_degraded_incidents
    assert scanner_module._api_recovery_probe_state["gate"]["consecutiveSuccesses"] == 1

    scanner_module._record_api_recovery_probe_result(
        "gate", success=False, error="proxy timeout", duration_ms=2000.0
    )
    assert scanner_module._api_recovery_probe_state["gate"]["consecutiveSuccesses"] == 0

    scanner_module._record_api_recovery_probe_result(
        "gate", success=True, error=None, duration_ms=10.0
    )
    for _ in range(5):
        probe_clock[0] += 2
        recovered = scanner_module._record_api_recovery_probe_result("gate", success=True, error=None, duration_ms=11.0)

    assert recovered is True
    assert "gate" not in scanner_module._api_degraded_incidents
    assert scanner_module._api_recovery_probe_state["gate"]["recoveryConfirmedAt"]


def test_new_fault_needs_two_new_successes_and_discards_old_probe(monkeypatch):
    error = RuntimeError("proxy handshake timed out")
    monkeypatch.setenv("ASTRO_GATE_DEPTH_CIRCUIT_FAILURES", "1")
    probe_clock=[100.0]
    monkeypatch.setattr(scanner_module.time, "monotonic", lambda:probe_clock[0])
    def stable():
        for _ in range(5):
            probe_clock[0]+=2
            recovered=probe()
        return recovered
    def fail():
        scanner_module._record_depth_request_failure("gate", symbol="ABC", error=error, was_probe=True)
    def probe(**kwargs):
        return scanner_module._record_api_recovery_probe_result("gate", success=True, error=None, duration_ms=10, **kwargs)
    fail()
    assert probe() is False
    assert stable() is True
    fail()
    assert probe() is False
    old_generation = scanner_module._api_recovery_probe_state["gate"]["generation"]
    fail()
    assert probe(generation=old_generation) is False
    assert scanner_module._api_recovery_probe_state["gate"]["consecutiveSuccesses"] == 0
    assert probe() is False
    assert stable() is True
    assert probe() is False  # No active incident: no repeated recovery event.


def test_normal_depth_success_cannot_clear_fault(monkeypatch):
    scanner_module._record_depth_request_failure(
        "gate", symbol="ABC", error=RuntimeError("proxy timed out"), was_probe=True
    )
    scanner_module._record_depth_request_success("gate", was_probe=False)
    assert scanner_module._depth_exchange_health["gate"]["incidentOpen"] is True


@pytest.mark.parametrize("source", ["binance", "aster", "gate", "bybit", "bitget", "okx", "okxdex"])
def test_recovery_payload_requires_exchange_business_success(source):
    ms = int(time.time() * 1000)
    good = {
        "binance": {}, "aster": {}, "gate": {"server_time": ms},
        "bybit": {"retCode": 0, "result": {"timeSecond": str(ms // 1000)}},
        "bitget": {"code": "00000", "data": {"serverTime": str(ms)}},
        "okx": {"code": "0", "data": [{"ts": str(ms)}]},
        "okxdex": {"code": "0", "data": [{"ts": str(ms)}]},
    }
    scanner_module._validate_api_recovery_payload(source, good[source])
    for invalid in ("<html>proxy error</html>", [], {"code": 500, "message": "error"}, {"server_time": ms - 120_000}):
        with pytest.raises(ValueError):
            scanner_module._validate_api_recovery_payload(source, invalid)


def test_http_200_html_does_not_recover_api(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is True
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def get(self, url):
            return httpx.Response(200, text="<html>proxy error</html>", request=httpx.Request("GET", url))
    monkeypatch.setattr(scanner_module.httpx, "Client", Client)
    assert scanner_module._api_recovery_probe_once("gate")[0] is False


def test_routine_decision_audits_are_aggregated_instead_of_warning_per_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict] = []
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )
    scanner_module._decision_audit_summary.update(
        {
            "startedAt": "test",
            "lastLoggedAtMonotonic": 0.0,
            "count": 0,
            "reasons": {},
            "samples": [],
        }
    )
    identity = ("CAKE", "SF", "okxdex", "gate", "56", "0xabc")
    details = {"values": {"openSpreadPct": 1.31}}

    first = scanner_module._append_decision_audit(
        identity,
        stage="rule_filter",
        decision="sf_negative_short_funding",
        details=details,
    )
    second = scanner_module._append_decision_audit(
        ("UNI", "SF", "okxdex", "gate", "1", "0xdef"),
        stage="rule_filter",
        decision="sf_negative_short_funding",
        details={"values": {"openSpreadPct": 1.41}},
    )

    assert first is True
    assert second is False
    assert len(events) == 1
    assert events[0]["event"] == "astro_auto_card_decision_summary"
    assert events[0]["level"] == "info"


def test_hot_direct_routine_failures_wait_for_summary_instead_of_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict] = []
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )
    scanner_module._decision_audit_summary.update(
        {
            "startedAt": "test",
            "lastLoggedAtMonotonic": time.monotonic(),
            "count": 0,
            "reasons": {},
            "samples": [],
            "durationCount": 0,
            "durationMsTotal": 0.0,
            "durationMsMax": 0.0,
            "routeStats": {},
        }
    )

    written = scanner_module._append_decision_audit(
        ("ABC", "FF", "binance", "gate", "", ""),
        stage="hot_direct_check",
        decision="direct_spread_below_threshold",
        details={
            "pulseOpenSpreadPct": 1.2,
            "report": {"latestOpenSpreadPct": 0.4, "durationMs": 312},
        },
    )

    assert written is False
    assert events == []
    assert scanner_module._decision_audit_summary["count"] == 1


def test_direct_api_failures_are_not_counted_as_missed_opportunities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict] = []
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )
    scanner_module._decision_audit_summary.update(
        {
            "startedAt": "test",
            "lastLoggedAtMonotonic": time.monotonic(),
            "count": 0,
            "reasons": {},
            "samples": [],
            "durationCount": 0,
            "durationMsTotal": 0.0,
            "durationMsMax": 0.0,
            "routeStats": {},
        }
    )
    identity = ("ABC", "FF", "binance", "gate", "", "")
    details = {
        "pulseOpenSpreadPct": 1.4,
        "report": {"error": "HTTP 429 Too Many Requests", "durationMs": 550},
    }

    for _ in range(3):
        scanner_module._append_decision_audit(
            identity,
            stage="hot_direct_check",
            decision="direct_quote_unavailable",
            details=details,
        )

    missed = [event for event in events if event["event"] == "astro_possible_missed_opportunity"]
    assert missed == []
    assert scanner_module._decision_audit_summary["count"] == 3


def test_existing_card_route_never_creates_missed_opportunity_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict] = []
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _route: "existing")
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )

    written = scanner_module._append_possible_missed_opportunity(
        ("SIREN", "FF", "binance", "gate", "", ""),
        decision="quote_time_skew",
        details={
            "pulseOpenSpreadPct": 1.4,
            "report": {"latestOpenSpreadPct": 1.3, "durationMs": 300},
        },
    )

    assert written is False
    assert events == []


def test_fresh_executable_spread_above_threshold_but_safety_blocked_is_audited_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict] = []
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setattr(
        scanner_module,
        "append_system_runtime_event",
        lambda event, **kwargs: events.append({"event": event, **kwargs}),
    )
    scanner_module._decision_audit_summary["lastLoggedAtMonotonic"] = time.monotonic()

    scanner_module._append_decision_audit(
        ("ABC", "FF", "binance", "gate", "", ""),
        stage="hot_direct_check",
        decision="ff_structure_filter",
        details={
            "pulseOpenSpreadPct": 1.3,
            "report": {
                "latestOpenSpreadPct": 1.18,
                "quoteSkewSeconds": 0.4,
                "buyQuote": {"quoteAgeSeconds": 0.8},
                "sellQuote": {"quoteAgeSeconds": 0.4},
                "cexExecutablePreflight": {"executableSpreadPct": 1.18},
                "durationMs": 420,
            },
        },
    )

    missed = [event for event in events if event["event"] == "astro_possible_missed_opportunity"]
    assert len(missed) == 1
    assert missed[0]["details"]["confidence"] == "confirmed_executable_spread"
    assert missed[0]["details"]["directOpenSpreadPct"] == pytest.approx(1.18)


def test_cleanup_observations_mark_below_threshold_and_reverse_ff_route_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_FF_MIN_OPEN_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "0")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    observations = _auto_card_route_observations(
        [
            {
                "symbol": "RVN",
                "type": "FF",
                "buyExchange": "gate",
                "sellExchange": "okx",
                "openSpreadPct": 0.8,
                "quoteAt": 123,
            }
        ]
    )
    assert observations[("RVN", "FF", "gate", "okx")]["state"] == "invalid"
    assert observations[("RVN", "FF", "gate", "okx")]["reason"] == "spread_below_ff_threshold"
    assert observations[("RVN", "FF", "okx", "gate")]["reason"] == "ff_route_direction_changed"
    assert observations[("RVN", "FF", "okx", "gate")]["openSpreadPct"] == -0.8


def test_cleanup_observation_does_not_count_missing_sf_funding_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    observations = _auto_card_route_observations(
        [
            {
                "symbol": "ABC",
                "type": "SF",
                "buyExchange": "binance",
                "buyMarket": "spot",
                "sellExchange": "gate",
                "sellMarket": "future",
                "openSpreadPct": 1.5,
                "sellFundingRatePct": None,
                "quoteAt": 123,
            }
        ]
    )
    assert observations[("ABC", "SF", "binance", "gate")]["state"] == "eligible"


def test_cleanup_final_revalidation_aborts_when_spread_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(__import__("time").time() * 1000)
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_direct_route_once",
        lambda pair, _config: (pair, {"reason": "eligible", "latestOpenSpreadPct": 1.2}),
    )
    confirmed, report = revalidate_astro_cleanup(
        {"name": "RVN", "type": "FF", "buyEx": "gate", "sellEx": "okx"},
        sdk_config(),
        {"reason": "spread_below_ff_threshold", "quoteAt": now_ms},
    )
    assert confirmed is False
    assert report["reason"] == "route_became_eligible_again"


def test_cleanup_accepts_two_non_positive_open_spread_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_ROUNDS", "2")
    monkeypatch.setattr(scanner_module.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        scanner_module,
        "_fetch_direct_route_once",
        lambda *_args, **_kwargs: (
            None,
            {"reason": "open_spread_non_positive", "latestOpenSpreadPct": -0.5},
        ),
    )

    confirmed, report = revalidate_astro_cleanup(
        {"name": "BASECAT", "type": "FF", "buyEx": "gate", "sellEx": "aster"},
        sdk_config(),
        {"reason": "spread_below_ff_threshold", "quoteAt": now_ms},
    )

    assert confirmed is True
    assert report["reason"] == "continuous_invalidity_confirmed"
    assert report["roundsPassed"] == 2


def test_ff_structure_filter_rejects_missing_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED", "1")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    candidate = {
        "type": "FF",
        "buyExchange": "gate",
        "sellExchange": "okx",
        "openSpreadPct": 1.05,
    }
    assert _meets_auto_card_rule(candidate) is False


def test_persistent_adverse_funding_and_gradual_spread_is_structural() -> None:
    now_ms = 10_000_000
    points = [
        {
            "key": "FF:RVN:gate:okx",
            "at": float(now_ms - (120 - index) * 60_000),
            "openSpreadPct": 0.80 + 0.20 * index / 120,
            "netFundingRatePct": -0.15,
            "indexFingerprint": "stable-gate-index",
        }
        for index in range(121)
    ]
    result = assess_ff_structure_history(
        {"key": "FF:RVN:gate:okx", "openSpreadPct": 1.0, "indexEvidenceRequired": True},
        points,
        now_ms=now_ms,
        history_hours=2,
        sample_seconds=60,
    )
    assert result["historyReady"] is True
    assert result["classification"] == "structural"
    assert result["autoCardEligible"] is False
    assert result["persistentAdverseFunding"] is True
    assert result["gradualFormation"] is True
    assert result["indexRegime"] == "stable"


def test_gate_route_rejects_missing_index_component_history() -> None:
    now_ms = 15_000_000
    points = [
        {
            "key": "FF:NOINDEX:gate:okx",
            "at": float(now_ms - (120 - index) * 60_000),
            "openSpreadPct": 0.90,
            "netFundingRatePct": -0.10,
        }
        for index in range(121)
    ]
    result = assess_ff_structure_history(
        {"key": "FF:NOINDEX:gate:okx", "openSpreadPct": 0.90, "indexEvidenceRequired": True},
        points,
        now_ms=now_ms,
        history_hours=2,
        sample_seconds=60,
    )
    assert result["classification"] == "index_history_missing"
    assert result["autoCardEligible"] is False


def test_sudden_or_outlier_spread_remains_an_auto_card_candidate() -> None:
    now_ms = 20_000_000
    points = [
        {
            "key": "FF:NEWS:gate:okx",
            "at": float(now_ms - (120 - index) * 60_000),
            "openSpreadPct": 0.80 if index < 120 else 1.40,
            "netFundingRatePct": -0.15,
        }
        for index in range(121)
    ]
    result = assess_ff_structure_history(
        {"key": "FF:NEWS:gate:okx", "openSpreadPct": 1.40},
        points,
        now_ms=now_ms,
        history_hours=2,
        sample_seconds=60,
    )
    assert result["historyReady"] is True
    assert result["classification"] == "abnormal"
    assert result["autoCardEligible"] is True
    assert result["gradualFormation"] is False
    assert result["withinHistoricalBand"] is False


def test_index_component_change_overrides_structural_classification() -> None:
    now_ms = 30_000_000
    points = [
        {
            "key": "FF:INDEX:binance:okx",
            "at": float(now_ms - (120 - index) * 60_000),
            "openSpreadPct": 0.90,
            "netFundingRatePct": -0.10,
            "indexFingerprint": "before" if index < 60 else "after",
        }
        for index in range(121)
    ]
    result = assess_ff_structure_history(
        {"key": "FF:INDEX:binance:okx", "openSpreadPct": 0.90, "indexEvidenceRequired": True},
        points,
        now_ms=now_ms,
        history_hours=2,
        sample_seconds=60,
    )
    assert result["classification"] == "abnormal"
    assert result["autoCardEligible"] is True
    assert result["indexRegime"] == "changed"


def test_sf_auto_card_rule_allows_all_supported_spots_with_non_hl_future_and_non_negative_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    common = {
        "type": "SF",
        "buyExchange": "binance",
        "buyMarket": "spot",
        "sellExchange": "gate",
        "sellMarket": "future",
        "openSpreadPct": 1.31,
    }
    assert _meets_auto_card_rule({**common, "sellFundingRatePct": 0.0}) is True
    assert _meets_auto_card_rule({**common, "sellFundingRatePct": -0.001}) is True
    assert _meets_auto_card_rule({**common, "sellFundingRatePct": None}) is True
    assert _meets_auto_card_rule({**common, "sellExchange": "hl", "sellFundingRatePct": 0.1}) is False
    for exchange in ("bybit", "bitget", "okx", "gate"):
        assert _meets_auto_card_rule({**common, "buyExchange": exchange, "sellFundingRatePct": 0.1}) is True
    assert _meets_auto_card_rule({**common, "buyExchange": "hl", "sellFundingRatePct": 0.1}) is False


def test_okxdex_sf_rule_allows_configured_target_futures_after_dex_sdk_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "spread_scan_sf_okxdex_auto_card_enabled", lambda: True)
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "0")
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 0.0)
    monkeypatch.setenv("ASTRO_SDK_BASE_URL", "https://astro.example")
    monkeypatch.setenv("ASTRO_SDK_ADMIN_PREFIX", "prefix")
    monkeypatch.setenv("ASTRO_SDK_API_KEY", "secret")
    monkeypatch.setenv("ASTRO_SDK_DEX_API_PATH", "/prefix/api/config/sdk-update-dex-coin")
    common = {
        "type": "SF",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellExchange": "binance",
        "sellMarket": "future",
        "openSpreadPct": 1.6,
        "sellFundingRatePct": 0.0,
        "dexConfig": {
            "name": "ABC",
            "chainIndex": "501",
            "contractAddress": "So11111111111111111111111111111111111111112",
            "quote": "USDT",
            "slippage": "1",
        },
        "dexIdentity": {"status": "verified"},
        "dexMapping": {"status": "confirmed"},
    }
    assert _meets_auto_card_rule(common) is True
    for exchange in ("gate", "bybit", "bitget", "aster", "okx"):
        assert _meets_auto_card_rule({**common, "sellExchange": exchange}) is True
    assert _meets_auto_card_rule({**common, "sellExchange": "hl"}) is False
    monkeypatch.delenv("ASTRO_SDK_DEX_API_PATH")
    assert _meets_auto_card_rule(common) is True
    assert _meets_auto_card_rule({**common, "dexMapping": {"status": "missing"}}) is False


def test_okxdex_identity_requires_exact_chain_and_contract_match() -> None:
    binance_config = {
        "AAVE": {
            "networkList": [
                {
                    "network": "ETH",
                    "name": "Ethereum (ERC20)",
                    "contractAddress": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2dDAE9",
                },
                {
                    "network": "BSC",
                    "contractAddress": "0xfb6115445Bff7b52FeB98650C87f44907E58f802",
                },
            ]
        }
    }
    verified = _verify_okxdex_identity(
        "AAVE",
        {
            "chainIndex": "1",
            "contractAddress": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
        },
        binance_config,
    )
    wrong_chain = _verify_okxdex_identity(
        "AAVE",
        {
            "chainIndex": "8453",
            "contractAddress": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
        },
        binance_config,
    )
    wrong_contract = _verify_okxdex_identity(
        "AAVE",
        {
            "chainIndex": "1",
            "contractAddress": "0x0000000000000000000000000000000000000001",
        },
        binance_config,
    )

    assert verified["status"] == "verified"
    assert verified["matchedNetwork"] == "ETH"
    assert wrong_chain["status"] == "chain_not_found"
    assert wrong_contract["status"] == "contract_mismatch"


def test_okxdex_identity_uses_selected_target_exchange_network_table() -> None:
    gate_row = {
        "asset": "TUT",
        "networkList": [
            {
                "network": "BSC",
                "chainIndex": "56",
                "contractAddress": "0xCaFe00000000000000000000000000000000Beef",
            }
        ],
    }
    result = _verify_okxdex_identity(
        "TUT",
        {
            "chainIndex": "56",
            "contractAddress": "0xcafe00000000000000000000000000000000beef",
        },
        gate_row,
        "gate",
    )

    assert result["status"] == "verified"
    assert result["targetExchange"] == "gate"
    assert result["matchedNetwork"] == "BSC"


def test_okxdex_build_selects_largest_spread_and_highest_volume_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "spread_scan_sf_okxdex_auto_card_enabled", lambda: True)
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SDK_BASE_URL", "https://astro.example")
    monkeypatch.setenv("ASTRO_SDK_ADMIN_PREFIX", "prefix")
    monkeypatch.setenv("ASTRO_SDK_API_KEY", "secret")
    monkeypatch.setenv("ASTRO_SDK_DEX_API_PATH", "/prefix/api/config/sdk-update-dex-coin")
    common = {
        "type": "SF",
        "symbol": "ABC",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellMarket": "future",
        "openSpreadPct": 1.8,
        "sellFundingRatePct": 0.0,
        "buyVolume24hUsdt": 2_000_000,
        "dexConfig": {"chainIndex": "56", "contractAddress": "0xabc"},
        "dexIdentity": {"status": "verified"},
        "dexMapping": {"status": "confirmed"},
    }
    candidates = [
        {**common, "key": "SF:ABC:okxdex:binance", "sellExchange": "binance", "openSpreadPct": 2.1, "sellVolume24hUsdt": 8_000_000},
        {**common, "key": "SF:ABC:okxdex:gate", "sellExchange": "gate", "openSpreadPct": 1.8, "sellVolume24hUsdt": 12_000_000},
        {**common, "key": "SF:ABC:okxdex:okx", "sellExchange": "okx", "sellVolume24hUsdt": None},
    ]

    selected, summary = _select_okxdex_targets(candidates)

    assert [item["sellExchange"] for item in selected] == ["binance", "gate"]
    assert selected[0]["targetSelection"]["selectionReasons"] == ["max_executable_spread"]
    assert selected[1]["targetSelection"]["selectionReasons"] == ["highest_24h_futures_volume"]
    assert summary["missingVolumeBlockedCount"] == 1
    assert summary["selectedCount"] == 2
    assert {item["targetExchange"] for item in summary["selected"]} == {"binance", "gate"}


def test_okxdex_target_selection_deduplicates_when_one_route_wins_both_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "spread_scan_sf_okxdex_auto_card_enabled", lambda: True)
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    monkeypatch.setenv("ASTRO_SDK_BASE_URL", "https://astro.example")
    monkeypatch.setenv("ASTRO_SDK_ADMIN_PREFIX", "prefix")
    monkeypatch.setenv("ASTRO_SDK_API_KEY", "secret")
    monkeypatch.setenv("ASTRO_SDK_DEX_API_PATH", "/prefix/api/config/sdk-update-dex-coin")
    common = {
        "type": "SF",
        "symbol": "ABC",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellMarket": "future",
        "sellFundingRatePct": 0.0,
        "buyVolume24hUsdt": 2_000_000,
        "dexConfig": {"chainIndex": "56", "contractAddress": "0xabc"},
        "dexIdentity": {"status": "verified"},
        "dexMapping": {"status": "confirmed"},
    }
    candidates = [
        {**common, "key": "binance", "sellExchange": "binance", "openSpreadPct": 2.2, "sellVolume24hUsdt": 15_000_000},
        {**common, "key": "gate", "sellExchange": "gate", "openSpreadPct": 1.8, "sellVolume24hUsdt": 12_000_000},
    ]

    selected, summary = _select_okxdex_targets(candidates)

    assert [item["sellExchange"] for item in selected] == ["binance"]
    assert selected[0]["targetSelection"]["selectionReasons"] == [
        "max_executable_spread",
        "highest_24h_futures_volume",
    ]
    assert summary["selectedCount"] == 1


def test_okxdex_target_selection_keeps_each_exact_chain_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanner_module, "spread_scan_sf_okxdex_auto_card_enabled", lambda: True)
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.3")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    monkeypatch.setenv("ASTRO_SDK_BASE_URL", "https://astro.example")
    monkeypatch.setenv("ASTRO_SDK_ADMIN_PREFIX", "prefix")
    monkeypatch.setenv("ASTRO_SDK_API_KEY", "secret")
    monkeypatch.setenv("ASTRO_SDK_DEX_API_PATH", "/prefix/api/config/sdk-update-dex-coin")
    common = {
        "type": "SF",
        "symbol": "ABC",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellMarket": "future",
        "openSpreadPct": 1.8,
        "sellFundingRatePct": 0.0,
        "buyVolume24hUsdt": 1_000_000,
        "dexIdentity": {"status": "verified"},
        "dexMapping": {"status": "confirmed"},
    }
    candidates = [
        {
            **common,
            "key": "chain-1-binance",
            "sellExchange": "binance",
            "sellVolume24hUsdt": 2_000_000,
            "dexConfig": {"chainIndex": "1", "contractAddress": "0x111"},
        },
        {
            **common,
            "key": "chain-1-gate",
            "sellExchange": "gate",
            "sellVolume24hUsdt": 3_000_000,
            "dexConfig": {"chainIndex": "1", "contractAddress": "0x111"},
        },
        {
            **common,
            "key": "chain-56-binance",
            "sellExchange": "binance",
            "sellVolume24hUsdt": 4_000_000,
            "dexConfig": {"chainIndex": "56", "contractAddress": "0x222"},
        },
    ]

    selected, summary = _select_okxdex_targets(candidates)

    assert {(item["dexConfig"]["chainIndex"], item["sellExchange"]) for item in selected} == {
        ("1", "gate"),
        ("56", "binance"),
    }
    assert summary["selectedCount"] == 2


def test_okxdex_final_identity_revalidation_does_not_require_pulse_funding_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_target_asset_identity_config",
        lambda *_args: {
            "asset": "ABC",
            "networkList": [
                {"network": "BSC", "chainIndex": "56", "contractAddress": "0xabc"},
            ],
        },
    )
    candidate = {
        "type": "SF",
        "symbol": "ABC",
        "buyExchange": "okxdex",
        "sellExchange": "gate",
        "dexConfig": {"chainIndex": "56", "contractAddress": "0xABC"},
    }

    verified, summary = _verify_okxdex_candidate_identities([candidate], eligible_only=False)

    assert verified[0]["dexIdentity"]["status"] == "verified"
    assert summary["verifiedCount"] == 1


def test_okxdex_identity_bypass_still_requires_saved_local_mapping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_OKXDEX_IDENTITY_VERIFICATION_ENABLED", "0")
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    monkeypatch.setenv("ASTRO_SPREAD_SF_MIN_OPEN_PCT", "1.2")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    update_astro_spread_subscriptions(
        ["okxdexSpot", "asterFuture"],
        dex_mapped_assets=[{
            "symbol": "FONE",
            "chainIndex": "501",
            "contractAddress": "CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump",
        }],
    )
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_target_asset_identity_config",
        lambda *_args: pytest.fail("identity API must not be called while the safety gate is off"),
    )
    candidate = {
        "type": "SF",
        "symbol": "FONE",
        "buyExchange": "okxdex",
        "buyMarket": "spot",
        "sellExchange": "aster",
        "sellMarket": "future",
        "openSpreadPct": 1.6,
        "sellFundingRatePct": 0.001,
        "buyVolume24hUsdt": 48_100_000,
        "sellVolume24hUsdt": 862_200,
        "dexConfig": {
            "name": "FONE",
            "chainIndex": "501",
            "contractAddress": "CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump",
        },
    }

    verified, identity_summary = _verify_okxdex_candidate_identities([candidate])
    annotated, mapping_summary = _annotate_okxdex_manual_mappings(verified)

    assert identity_summary["identityVerificationEnabled"] is False
    assert identity_summary["bypassedCount"] == 1
    assert annotated[0]["dexIdentity"]["status"] == "bypassed"
    assert mapping_summary["mappingConfirmationRequired"] is True
    assert annotated[0]["dexMapping"]["status"] == "confirmed"
    assert _meets_auto_card_rule(annotated[0]) is True

    update_astro_spread_subscriptions(
        ["okxdexSpot", "asterFuture"],
        dex_mapped_assets=[],
    )
    without_mapping = {key: value for key, value in annotated[0].items() if key != "dexMapping"}
    remapped, missing_summary = _annotate_okxdex_manual_mappings([without_mapping])
    assert missing_summary["missingCount"] == 1
    assert remapped[0]["dexMapping"]["status"] == "missing"
    assert _meets_auto_card_rule(remapped[0]) is False


def test_okxdex_identity_verifies_same_symbol_on_each_exact_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_binance_asset_identity_config",
        lambda: {
            "ABC": {
                "networkList": [
                    {"network": "ETH", "contractAddress": "0x1111111111111111111111111111111111111111"},
                    {"network": "BSC", "contractAddress": "0x2222222222222222222222222222222222222222"},
                ]
            }
        },
    )
    common = {
        "type": "SF",
        "symbol": "ABC",
        "buyExchange": "okxdex",
        "sellExchange": "binance",
        "openSpreadPct": 1.6,
        "sellFundingRatePct": 0.0,
    }
    candidates = [
        {
            **common,
            "dexConfig": {
                "chainIndex": "1",
                "contractAddress": "0x1111111111111111111111111111111111111111",
            },
        },
        {
            **common,
            "dexConfig": {
                "chainIndex": "56",
                "contractAddress": "0x2222222222222222222222222222222222222222",
            },
        },
    ]

    verified, summary = _verify_okxdex_candidate_identities(candidates)

    assert summary["verifiedCount"] == 2
    assert summary["blockedCount"] == 0
    assert summary["statusCounts"] == {"verified": 2}
    assert all(item["dexIdentity"]["status"] == "verified" for item in verified)


def test_okxdex_identity_blocks_same_name_with_unrelated_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.astro_spread_scanner._fetch_target_asset_identity_config",
        lambda *_args: None,
    )
    candidate = {
        "type": "SF",
        "symbol": "AAPL",
        "buyExchange": "okxdex",
        "sellExchange": "binance",
        "openSpreadPct": 1.6,
        "sellFundingRatePct": 0.0,
        "dexConfig": {"chainIndex": "501", "contractAddress": "FakeSolanaContract"},
    }

    verified, summary = _verify_okxdex_candidate_identities([candidate])

    assert verified[0]["dexIdentity"]["status"] == "asset_not_found"
    assert summary["verifiedCount"] == 0
    assert summary["blockedCount"] == 1


def test_okxdex_pulse_candidate_preserves_chain_and_contract_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    subscription_file = tmp_path / "astro-subscriptions.json"
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(subscription_file))
    update_astro_spread_subscriptions(
        ["okxdexSpot", "binanceFuture"],
        min_volume_usdt=0,
        blocked_pairs=[],
        blocked_coins=[],
    )
    payload = {
        "data": {
            "okxdexSpot": market(
                1_000_000,
                [
                    {
                        "name": "ABCUSDT",
                        "a": 100.0,
                        "b": 99.0,
                        "trade24Count": 1_000_000,
                        "chainIndex": "501",
                        "ca": "So11111111111111111111111111111111111111112",
                    }
                ],
            ),
            "binanceFuture": market(
                1_000_000,
                [{"name": "ABCUSDT", "a": 103.0, "b": 102.0, "trade24Count": 1_000_000, "rate": 0.01}],
            ),
        }
    }
    candidates, _ = scan_pulse_spreads([payload], now_ms=1_001_000, symbol_aliases={})
    sf = next(item for item in candidates if item["type"] == "SF")
    assert sf["buyExchange"] == "okxdex"
    assert sf["sellExchange"] == "binance"
    assert sf["dexConfig"]["chainIndex"] == "501"
    assert sf["dexConfig"]["contractAddress"].startswith("So111")


@pytest.mark.parametrize("blocked", ["blocked_pair", "existing", "queued", "syncing", "volume_below_threshold", "volume_unavailable"])
def test_hot_local_filter_spends_zero_api_calls(monkeypatch, blocked):
    monkeypatch.setattr(scanner_module, "astro_spread_pair_submit_guard", lambda _pair: (
        blocked != "blocked_pair", {"reason": "blocked_pair"},
    ))
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _pair: (
        blocked if blocked in {"existing", "queued", "syncing"} else None
    ))
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 200_000)
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", lambda *_a, **_kw: pytest.fail("must not spend API calls"))
    pair = {"name": "ABC", "type": "SF", "buyEx": "gate", "sellEx": "binance",
            "_buyVolume24hUsdt": None if blocked == "volume_unavailable" else 199_999,
            "_sellVolume24hUsdt": 300_000}
    item = {"pair": pair, "identity": ("ABC", "SF", "gate", "binance", "", "")}
    _, latest, report = scanner_module._hot_route_direct_check(item, sdk_config())
    assert latest is None
    assert report["preApiFiltered"] is True
    assert report["reason"] == ("route_already_tracked" if blocked in {"existing", "queued", "syncing"} else blocked)


def test_hot_filter_rechecks_current_volume_threshold_and_preserves_listing_and_dips(monkeypatch):
    monkeypatch.setattr(scanner_module, "_hot_funding_precheck", lambda _: {"eligible": True})
    monkeypatch.setattr(scanner_module, "astro_spread_pair_submit_guard", lambda _: (True, {}))
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _: None)
    threshold = [200_000]
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: threshold[0])
    pair = {"name": "ABC", "type": "SF", "buyEx": "gate", "sellEx": "binance",
            "_buyVolume24hUsdt": 250_000, "_sellVolume24hUsdt": 300_000}
    assert scanner_module._hot_pre_api_filter(pair) is None
    threshold[0] = 260_000
    assert scanner_module._hot_pre_api_filter(pair)["reason"] == "volume_below_threshold"
    assert scanner_module._hot_pre_api_filter({**pair, "_priorityNewListing": True})["reason"] == "volume_below_threshold"
    assert scanner_module._hot_pre_api_filter({**pair, "_listingVolumeWindows": {"buy": int(time.time() * 1000)}}) is None
    threshold[0] = 200_000
    calls = []
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", lambda *_a, **_kw: (calls.append(1), {"reason": "below_threshold"}))
    item = {"pair": pair, "identity": ("ABC", "SF", "gate", "binance", "", ""),
            "candidate": {"openSpreadPct": -1}, "reasons": ["new_listing"]}
    scanner_module._hot_route_direct_check(item, sdk_config())
    assert calls == [1]  # A hot route is not stopped just because Pulse dipped.


@pytest.mark.parametrize("kind", ["contract_multiplier"])
def test_scanner_concurrent_auxiliary_reads_are_coalesced(monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"ratePct": 0.01, "periodHours": 4} if kind == "funding" else 0.01

    monkeypatch.setattr(scanner_module, "_contract_quantity_multiplier_cache", {})
    monkeypatch.setattr(scanner_module, "_load_futures_contract_quantity_multiplier", load)
    fetch = lambda: scanner_module._futures_contract_quantity_multiplier(object(), "gate", "ABC")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch)
        assert entered.wait(2)
        second = pool.submit(fetch)
        deadline = time.monotonic() + 2
        try:
            while time.monotonic() < deadline:
                if scanner_module._auxiliary_reads.snapshot()["byKind"].get(kind, {}).get("joined", 0):
                    break
                threading.Event().wait(0.001)
            else:
                pytest.fail("expected overlapping route to reuse request")
        finally:
            release.set()
        assert first.result(2) == second.result(2)
    assert calls == [1]
    fetch()
    assert calls == [1, 1]  # The coalescer retains no completed funding result.


def test_next_depth_round_does_not_reuse_previous_round_book(monkeypatch):
    from types import SimpleNamespace
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(scanner_module.time, "time", lambda: clock[0])

    def get(*_args, **_kwargs):
        calls.append(1)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": len(calls)})

    monkeypatch.setattr(scanner_module, "_scanner_public_get", get)
    client = SimpleNamespace(depth_round_started_ms=1_000_000)
    first, _ = scanner_module._shared_depth_json_get(client, "https://fapi.binance.com/fapi/v1/depth", params={"symbol": "ABCUSDT", "limit": 20})
    clock[0] += 0.001
    client.depth_round_started_ms = 1_000_001
    second, _ = scanner_module._shared_depth_json_get(client, "https://fapi.binance.com/fapi/v1/depth", params={"symbol": "ABCUSDT", "limit": 20})
    assert first["id"] == 1
    assert second["id"] == 2
    assert calls == [1, 1]




def test_funding_error_only_marks_futures_exchange_as_failed():
    assert scanner_module._api_degraded_fault_sources(
        {"name": "RATS", "buyEx": "gate", "sellEx": "binance"},
        {"reason": "direct_funding_unavailable", "error": "funding timeout", "failureSources": ["binance"]},
    ) == ["binance"]


def test_hot_symbol_cap_is_shared_across_types_bands_and_inflight():
    rows = []
    for symbol, pair_type, checked in [("ONE", "SF", False), ("ONE", "FF", True), ("TWO", "FF", True)]:
        for index in range(4):
            rows.append({"identity": (symbol, pair_type, f"buy{index}", "sell", "", ""),
                         "reasons": ["pulse_above_threshold"], "checks": int(checked),
                         "candidate": {"openSpreadPct": 2}, "firstSeenAtMs": 1000})
    selected = scanner_module._select_hot_routes_for_cycle(
        rows, limit=8, per_symbol_limit=4, symbol_inflight_counts={"ONE": 1}, now_ms=2000,
    )
    assert sum(row["identity"][0] == "ONE" for row in selected) == 3
    assert sum(row["identity"][0] == "TWO" for row in selected) == 4


def test_waiting_listing_is_promoted_before_continuing_threshold_traffic():
    listing = {"identity": ("NEW", "FF", "a", "b", "", ""), "checks": 0,
               "registeredAtMs": 1000, "reasons": ["new_listing"], "candidate": {"openSpreadPct": .1}}
    fresh = {"identity": ("HOT", "FF", "a", "b", "", ""), "checks": 0,
             "registeredAtMs": 5900, "reasons": ["pulse_above_threshold"], "candidate": {"openSpreadPct": 3}}
    assert scanner_module._select_hot_routes_for_cycle([listing, fresh], limit=1, now_ms=5999) == [fresh]
    assert scanner_module._select_hot_routes_for_cycle([listing, fresh], limit=1, now_ms=6000) == [listing]


def test_route_waits_are_recomputed_from_worker_start(monkeypatch):
    scanner_module._hot_routes[("A", "FF", "a", "b", "", "")] = {
        "identity": ("A", "FF", "a", "b", "", ""), "registeredAtMs": 1000,
        "lastSelectedAtMs": 2000, "lastDirectCheckStartedAtMs": 3000, "inFlight": True,
    }
    now = [4.0]
    monkeypatch.setattr(scanner_module.time, "time", lambda: now[0])
    assert scanner_module._hot_route_wait_snapshot()["maxRouteWaitSinceLastStartMs"] == 1000
    now[0] = 5.0
    snapshot = scanner_module._hot_route_wait_snapshot()
    assert snapshot["maxRouteWaitSinceLastStartMs"] == 2000
    assert snapshot["routeWaits"][0]["lastSelectedAtMs"] == 2000
    assert snapshot["inFlightRouteCount"] == 1


def test_hot_pool_refills_a_free_worker_while_another_route_is_slow_and_stops_safely(monkeypatch):
    release, slow_entered, third_entered, slow_finished = (threading.Event() for _ in range(4))
    calls = []
    monkeypatch.setattr(scanner_module, "spread_hot_monitor_workers", lambda: 2)
    monkeypatch.setattr(scanner_module, "spread_hot_monitor_max_routes_per_cycle", lambda: 2)
    monkeypatch.setattr(scanner_module, "spread_hot_monitor_interval_seconds", lambda: 10)
    monkeypatch.setattr(scanner_module, "astro_sdk_config", sdk_config)
    monkeypatch.setattr(scanner_module, "_call_with_astro_api_priority", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(scanner_module, "_hot_pre_api_filter", lambda _: None)
    monkeypatch.setattr(scanner_module, "_prune_deduplicated_hot_routes", lambda: 0)
    monkeypatch.setattr(scanner_module, "_append_decision_audit", lambda *a, **kw: None)
    monkeypatch.setattr(scanner_module, "_record_hot_monitor_state", lambda **kw: None)
    monkeypatch.setattr(scanner_module, "schedule_astro_pairs", lambda *a, **kw: pytest.fail("late result submitted after stop"))

    def fetch(pair, config, **kwargs):
        name = pair["name"]
        calls.append(name)
        if name == "A":
            slow_entered.set()
            release.wait(3)
            slow_finished.set()
            return pair, {"reason": "eligible"}
        if name == "C":
            third_entered.set()
        return None, {"reason": "below_threshold_or_rule_failed"}

    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", fetch)
    now = int(time.time() * 1000)
    for index, symbol in enumerate(("A", "B", "C")):
        identity = (symbol, "FF", "binance", "gate", "", "")
        scanner_module._hot_routes[identity] = {
            "identity": identity, "pair": {"name": symbol, "type": "FF", "buyEx": "binance", "sellEx": "gate"},
            "candidate": {"openSpreadPct": 2}, "reasons": ["pulse_above_threshold"],
            "registeredAtMs": now + index, "expiresAtMonotonic": time.monotonic() + 60,
        }
    thread = threading.Thread(target=scanner_module._hot_monitor_loop, daemon=True)
    thread.start()
    try:
        assert slow_entered.wait(1)
        assert third_entered.wait(1), "a free worker must start C without waiting for slow A"
        assert calls.count("A") == 1
        scanner_module._stop.set()
        thread.join(1)
        assert not thread.is_alive()
    finally:
        scanner_module._stop.set()
        release.set()
        assert slow_finished.wait(1)
        thread.join(1)




def test_listing_volume_window_needs_known_time_actual_market_and_live_two_hour_window(monkeypatch):
    from datetime import datetime, timezone
    now_ms = 10_000_000
    monkeypatch.setattr(scanner_module.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(scanner_module, "spread_scan_min_volume_usdt", lambda: 200_000)
    def row(exchange, market, event, known=True):
        return {"symbol": "NEW", "exchange": exchange, "market_type": market,
                "listingEventAt": datetime.fromtimestamp(event/1000, timezone.utc).isoformat(),
                "listingEventTimeKnown": known}
    windows = scanner_module._listing_volume_market_windows([
        row("bn", "futures", now_ms-1000), row("bg", "spot", now_ms+1000),
        row("gt", "futures", now_ms-1000, False), row("ok", "futures", now_ms-7_200_000),
    ], now_ms=now_ms)
    assert windows == {("NEW", "binance", "future"): now_ms-1000}
    candidate = {"buyVolume24hUsdt": 10, "sellVolume24hUsdt": 300_000,
                 "priorityNewListing": True, "listingVolumeWindows": {"buy": now_ms-1000}}
    assert _auto_card_volume_check(candidate) == (True, "new_listing_bypass")
    assert _auto_card_volume_check({**candidate, "sellVolume24hUsdt": 10}) == (False, "volume_below_threshold")
    for event in (now_ms+1, now_ms-7_200_000):
        assert _auto_card_volume_check({**candidate, "listingVolumeWindows": {"buy": event}})[0] is False


def test_expired_hot_hit_restarts_two_depth_only_checks_without_ticker(monkeypatch):
    monkeypatch.setattr(scanner_module.time, "sleep", lambda _: None)
    calls = []
    pair = {"name": "ABC", "type": "FF", "buyEx": "binance", "sellEx": "gate",
            "_hotDirectHit": {"verifiedAtMs": int(time.time()*1000)-4000, "report": {"reason": "eligible"}}}
    def fetch(p, config, **kwargs):
        calls.append(kwargs)
        return p, {"reason": "eligible", "latestOpenSpreadPct": 2}
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", fetch)
    latest, report = revalidate_astro_hot_direct_hit(pair, sdk_config())
    assert latest is not None
    assert calls == [{"executable_depth_first": True}, {"executable_depth_first": True}]
    assert report["roundsPassed"] == 2
    assert report["intervalMs"] == 250




def test_listing_watch_uses_tradeable_pulse_route_and_only_actual_new_market(monkeypatch):
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _: None)
    candidate = {"key": "FF:NEW:binance:gate", "symbol": "NEW", "type": "FF",
                 "buyExchange": "binance", "sellExchange": "gate", "buyMarket": "future", "sellMarket": "future",
                 "openSpreadPct": .1, "closeSpreadPct": .2,
                 "buyVolume24hUsdt": 300_000, "sellVolume24hUsdt": 300_000, "priorityNewListing": True}
    assert _register_hot_candidates([candidate], {"NEW"}, sdk_config())["routeCount"] == 0
    future = {**candidate, "listingVolumeWindows": {"buy": int(time.time()*1000)+3600_000}}
    assert _register_hot_candidates([future], {"NEW"}, sdk_config())["routeCount"] == 0
    live = {**candidate, "listingVolumeWindows": {"buy": int(time.time()*1000)-1000}}
    assert _register_hot_candidates([live], {"NEW"}, sdk_config())["routeCount"] == 1


def launched_listing_hints(now_ms):
    from datetime import datetime, timezone
    event = datetime.fromtimestamp((now_ms-1000)/1000, timezone.utc).isoformat()
    return scanner_module._listing_route_hint_candidates("NEW", [
        {"symbol": "NEW", "exchange": "bg", "market_type": "futures", "event_action": "listing", "event_source": "1", "pending": "1",
         "listingEventAt": event, "listingEventTimeKnown": True},
        {"symbol": "NEW", "exchange": "as", "market_type": "futures", "event_action": "listing", "event_source": "0", "pending": "0"},
    ])


def test_due_launch_without_pulse_gets_bounded_probe_but_unknown_old_leg_volume_still_blocks(monkeypatch):
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _: None)
    monkeypatch.setattr(scanner_module, "astro_spread_pair_submit_guard", lambda _: (True, {}))
    hints = launched_listing_hints(int(time.time()*1000))
    assert _register_hot_candidates(hints, {"NEW"}, sdk_config())["routeCount"] == 2
    item = next(iter(scanner_module._hot_routes.values()))
    assert item["listingProbe"] is True
    assert scanner_module._hot_pre_api_filter(item["pair"]) is None
    evidence = {"reason": "volume_unavailable", "cexExecutablePreflight": {"executableSpreadPct": 2},
                "quoteSkewSeconds": .1, "buyQuote": {"quoteAgeSeconds": .2}, "sellQuote": {"quoteAgeSeconds": .1}}
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", lambda *_a, **_kw: (None, evidence))
    identity, latest, report = scanner_module._hot_route_direct_check(dict(item), sdk_config())
    assert latest is None
    scanner_module._process_hot_direct_result(identity, latest, report, sdk_config())
    assert item["marketBooksVerified"] is True
    assert item["listingProbe"] is False
    assert scanner_module._hot_pre_api_filter(item["pair"])["reason"] == "volume_unavailable"


def test_unpublished_launch_probe_retries_at_scan_cadence_and_refresh_does_not_accelerate_it(monkeypatch):
    from types import SimpleNamespace
    clock = [10_000.0]
    monkeypatch.setattr(scanner_module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "spread_scan_interval_seconds", lambda: 5)
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _: None)
    monkeypatch.setattr(scanner_module, "astro_spread_pair_submit_guard", lambda _: (True, {}))
    monkeypatch.setattr(scanner_module, "_fetch_direct_route_once", lambda *_a, **_kw: (None, {"reason": "direct_quote_unavailable"}))
    hints = launched_listing_hints(int(clock[0]*1000))
    _register_hot_candidates(hints, {"NEW"}, sdk_config())
    item = next(iter(scanner_module._hot_routes.values()))
    identity, latest, report = scanner_module._hot_route_direct_check(dict(item), sdk_config())
    scanner_module._process_hot_direct_result(identity, latest, report, sdk_config())
    assert item["listingProbe"] is True
    assert item["nextPollMonotonic"] == 10_005
    clock[0] += 1
    _register_hot_candidates(hints, {"NEW"}, sdk_config())
    assert scanner_module._hot_routes[identity]["nextPollMonotonic"] == 10_005


def test_ordinary_hot_routes_precede_aged_launch_probes_and_only_one_probe_can_run():
    def item(symbol, probe):
        return {"identity": (symbol, "FF", "a", "b", "", ""), "listingProbe": probe,
                "registeredAtMs": 1000 if probe else 9999, "checks": 0,
                "reasons": ["new_listing"] if probe else ["pulse_above_threshold"], "candidate": {"openSpreadPct": 2}}
    ordinary = [item(symbol, False) for symbol in ("A", "B", "C")]
    probes = [item(symbol, True) for symbol in ("P1", "P2", "P3")]
    selected = scanner_module._select_hot_routes_for_cycle([*probes, *ordinary], limit=8, now_ms=10_000)
    assert selected[:3] == ordinary
    assert sum(bool(row["listingProbe"]) for row in selected) == 1
    selected = scanner_module._select_hot_routes_for_cycle([*probes, *ordinary], limit=8, now_ms=10_000, listing_probe_inflight=1)
    assert selected == ordinary


def test_pulse_evidence_replaces_probe_and_later_hint_cannot_erase_actual_volume(monkeypatch):
    monkeypatch.setattr(scanner_module, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _: None)
    hints = launched_listing_hints(int(time.time()*1000))
    _register_hot_candidates(hints, {"NEW"}, sdk_config())
    pulse = {**hints[0], "announcementRouteHint": False, "quoteSource": "astro_pulse_aggregate",
             "openSpreadPct": 2, "buyVolume24hUsdt": 500_000, "sellVolume24hUsdt": 600_000}
    _register_hot_candidates([pulse], {"NEW"}, sdk_config())
    identity = scanner_module._candidate_route_identity(pulse)
    assert scanner_module._hot_routes[identity]["listingProbe"] is False
    assert scanner_module._hot_routes[identity]["pulseObserved"] is True
    _register_hot_candidates(hints, {"NEW"}, sdk_config())
    pair = scanner_module._hot_routes[identity]["pair"]
    assert pair["_buyVolume24hUsdt"] == 500_000
    assert pair["_sellVolume24hUsdt"] == 600_000
    assert pair["_listingDiscoveryOnly"] is False


@pytest.mark.parametrize("decision,overrides", [
    ("stale_direct_quote", {"buyQuote": {"quoteAgeSeconds": 3.878}}),
    ("direct_quote_time_skew", {"quoteSkewSeconds": 2.088}),
    ("direct_funding_unavailable", {}),
    ("ff_structure_filter", {"cexExecutablePreflight": None}),
    ("ff_structure_filter", {"buyQuote": {}}),
])
def test_unusable_depth_does_not_become_confirmed_missed_opportunity(monkeypatch, decision, overrides):
    events = []
    monkeypatch.setattr(scanner_module, "astro_route_dedupe_state", lambda _: None)
    monkeypatch.setattr(scanner_module, "append_system_runtime_event", lambda *a, **kw: events.append(a))
    monkeypatch.setattr(scanner_module, "spread_scan_ff_min_open_pct", lambda: 1)
    report = {"latestOpenSpreadPct": 2, "quoteSkewSeconds": 0.3,
              "buyQuote": {"quoteAgeSeconds": 0.5}, "sellQuote": {"quoteAgeSeconds": 0.2},
              "cexExecutablePreflight": {"executableSpreadPct": 2}, **overrides}
    assert not scanner_module._append_possible_missed_opportunity(
        ("ABC", "FF", "binance", "gate", "", ""), decision=decision,
        details={"pulseOpenSpreadPct": 3, "report": report},
    )
    assert events == []


def test_routine_channel_transitions_are_summarized_and_tail_is_flushed(monkeypatch):
    events, clock = [], [100.0]
    monkeypatch.setattr(scanner_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(scanner_module, "append_system_runtime_event", lambda event, **kw: events.append({"event": event, **kw}))
    for index in range(50):
        clock[0] = 100 + index
        scanner_module._log_api_channel_transition("astro_depth_backup_activated", level="info", details={"route": f"R{index}", "sources": ["gate"]})
    assert len(events) == 1
    clock[0] = 400
    scanner_module._flush_api_channel_transition_logs()
    assert len(events) == 2
    assert events[1]["event"] == "astro_api_channel_transition_summary"
    assert events[1]["details"]["count"] == 49
    assert len(events[1]["details"]["samples"]) == 20
    scanner_module._flush_api_channel_transition_logs()
    assert len(events) == 2

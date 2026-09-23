from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import app.astro_sdk as astro_sdk_module
import app.astro_card_registry as registry_module

from app.astro_sdk import (
    AstroSdkClient,
    AstroSdkConfig,
    assess_astro_fs_borrow_signal,
    astro_auto_card_status,
    astro_spread_card_routes,
    build_astro_fs_borrow_pairs,
    build_astro_fs_pair,
    build_astro_spread_pair,
    build_astro_spread_pairs,
    canonical_body,
    sign_astro_request,
)
from app.astro_card_registry import observe_auto_card_cleanup, register_auto_created_pair, reset_registry_for_tests


@pytest.fixture(autouse=True)
def isolate_sdk_registry(monkeypatch, tmp_path):
    from app import astro_sdk_budget
    monkeypatch.setattr(astro_sdk_budget, "_budgets", {})
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "sdk-registry.json"))
    with astro_sdk_module._route_dedupe_lock:
        astro_sdk_module._pending_submission_routes.clear()


def config(**overrides: object) -> AstroSdkConfig:
    values: dict[str, object] = {
        "base_url": "https://astro.example:8443",
        "admin_prefix": "prefix123",
        "api_key": "test-secret",
        "enabled": True,
        "dry_run": False,
        "tls_verify": True,
        "timeout_seconds": 5.0,
        "restart_wait_seconds": 3.0,
        "max_cards_per_scan": 0,
        "max_trade_usdt": 10.0,
        "leverage": 3.0,
        "spot_margin_type": "cross",
        "dex_api_path": "",
    }
    values.update(overrides)
    return AstroSdkConfig(**values)  # type: ignore[arg-type]


def fresh_quote_report(now_ms: int | None = None, limit: float = 3.0) -> dict:
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    return {"reason": "eligible", "buyQuote": {"timestamp": timestamp, "quoteAgeSeconds": 0}, "sellQuote": {"timestamp": timestamp, "quoteAgeSeconds": 0}, "quoteAgeLimitsSeconds": {"buy": limit, "sell": limit}}


def test_signature_matches_official_canonical_format() -> None:
    body = canonical_body({"action": "list"})
    canonical = "1700000000000\nnonce-value\nPOST\n/prefix123/api/config/sdk-update-pair\n" + body
    expected = hmac.new(b"test-secret", canonical.encode(), hashlib.sha256).hexdigest()
    assert sign_astro_request(
        "test-secret",
        1700000000000,
        "nonce-value",
        "/prefix123/api/config/sdk-update-pair",
        body,
    ) == expected


def test_client_sends_signed_list_request() -> None:
    resolved = config()

    def handler(request: httpx.Request) -> httpx.Response:
        raw_body = request.content.decode()
        timestamp = int(request.headers["x-timestamp"])
        nonce = request.headers["x-nonce"]
        assert request.url.path == resolved.api_path
        assert json.loads(raw_body) == {"action": "list"}
        assert request.headers["x-sign"] == sign_astro_request(
            resolved.api_key,
            timestamp,
            nonce,
            resolved.api_path,
            raw_body,
        )
        return httpx.Response(200, json={"code": 0, "data": [{"name": "ETH"}]})

    with AstroSdkClient(resolved, transport=httpx.MockTransport(handler)) as client:
        assert client.list_pairs() == [{"name": "ETH"}]


def test_client_sends_delete_with_only_pair_id() -> None:
    resolved = config()

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content.decode()) == {
            "action": "delete",
            "pair": {"id": "pair-123"},
        }
        return httpx.Response(200, json={"code": 0, "data": None})

    with AstroSdkClient(resolved, transport=httpx.MockTransport(handler)) as client:
        client.delete_pair("pair-123")


def test_client_ensures_dex_coin_before_pair_creation() -> None:
    resolved = config(dex_api_path="/prefix123/api/config/sdk-update-dex-coin")
    stored: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if request.url.path == resolved.dex_api_path:
            if body["action"] == "list":
                return httpx.Response(200, json={"code": 0, "data": stored})
            if body["action"] == "add":
                stored.append({key: value for key, value in body.items() if key != "action"})
                return httpx.Response(200, json={"code": 0, "data": None})
        raise AssertionError(f"unexpected request: {request.url.path} {body}")

    coin = {
        "name": "ABC",
        "chainIndex": "501",
        "contractAddress": "So11111111111111111111111111111111111111112",
        "quote": "USDT",
        "slippage": "1",
    }
    with AstroSdkClient(resolved, transport=httpx.MockTransport(handler)) as client:
        assert client.ensure_dex_coin(coin)["action"] == "created"
        assert client.ensure_dex_coin(coin)["action"] == "existing"


def test_add_pair_does_not_send_internal_dex_metadata() -> None:
    resolved = config()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["pair"]["buyEx"] == "okxdex"
        assert "_dexConfig" not in body["pair"]
        return httpx.Response(200, json={"code": 0, "data": None})

    with AstroSdkClient(resolved, transport=httpx.MockTransport(handler)) as client:
        client.add_pair({"name": "ABC", "buyEx": "okxdex", "_dexConfig": {"chainIndex": "501"}})


def test_fs_pair_uses_executable_route_and_starts_paused() -> None:
    pair = build_astro_fs_pair(
        {
            "symbol": "mmt",
            "futuresExchange": "bn",
            "spotExchange": "bg",
            "openSpreadRate": 0.01234,
        },
        config(),
    )
    assert pair["name"] == "MMT"
    assert pair["type"] == "FS"
    assert pair["buyEx"] == "binance"
    assert pair["sellEx"] == "bitget"
    assert pair["openPosition"] == "0.01234"
    assert float(pair["openPosition"]) * 100 == pytest.approx(1.234)
    assert pair["status"] is False
    assert pair["disableOpen"] is False
    assert pair["disableClose"] is False
    assert pair["closePosition"] == "0"
    assert pair["leverage"] == "3"
    assert pair["greaterPriceAlert"] == ""
    assert pair["priceAlert"] == ""
    assert pair["priceAlertOnlyRise"] is False


def test_fs_borrow_rule_aligns_hourly_borrow_cost_and_requires_positive_inventory(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_FS_BORROW_AUTO_CARD_ENABLED", "1")
    monkeypatch.setenv("ASTRO_FS_BORROW_MIN_CYCLE_PROFIT_PCT", "0.2")
    monkeypatch.setenv("ASTRO_FS_BORROW_MIN_OPEN_SPREAD_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    signal = {
        "symbol": "ABC",
        "futuresExchange": "okx",
        "spotExchange": "bg",
        "currentFundingRate": -0.005,
        "periodHours": 4,
        "volume24h": 500_000,
        "checks": {
            "bg": {
                "hourlyBorrowRate": 0.0005,
                "openSpreadRate": 0.012,
                "inventoryAvailable": True,
                "canBorrow": True,
                "borrowableAmount": 1000,
                "borrowableValueUsdt": 5000,
            }
        },
    }

    eligible, report = assess_astro_fs_borrow_signal(signal)
    pairs, summary = build_astro_fs_borrow_pairs([signal], config(leverage=8, spot_margin_type="isolated"))

    assert eligible is True
    assert report["borrowPeriodRate"] == 0.002
    assert report["cycleProfitPct"] == pytest.approx(0.3)
    assert report["inventoryAvailable"] is True
    assert report["borrowableAmount"] == 1000
    assert summary["eligibleCount"] == 1
    assert pairs[0]["type"] == "FS"
    assert pairs[0]["buyEx"] == "okx"
    assert pairs[0]["sellEx"] == "bitget"
    assert pairs[0]["spotMarginType"] == "cross"
    assert pairs[0]["leverage"] == "3"


@pytest.mark.parametrize(
    ("check", "expected_reason"),
    [
        ({"inventoryAvailable": False, "canBorrow": False}, "bitget_borrowable_amount_missing"),
        (
            {"inventoryAvailable": False, "canBorrow": False, "borrowableAmount": 0},
            "bitget_borrowable_amount_zero",
        ),
        (
            {"inventoryAvailable": False, "canBorrow": False, "borrowableAmount": 100},
            "bitget_borrow_inventory_not_executable",
        ),
    ],
)
def test_fs_borrow_rule_rejects_missing_zero_or_unusable_inventory(
    monkeypatch,
    tmp_path,
    check,
    expected_reason,
) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_FS_BORROW_AUTO_CARD_ENABLED", "1")
    monkeypatch.setenv("ASTRO_FS_BORROW_MIN_CYCLE_PROFIT_PCT", "0.2")
    monkeypatch.setenv("ASTRO_FS_BORROW_MIN_OPEN_SPREAD_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    signal = {
        "symbol": "ABC",
        "futuresExchange": "okx",
        "spotExchange": "bg",
        "currentFundingRate": -0.005,
        "periodHours": 4,
        "volume24h": 500_000,
        "checks": {
            "bg": {
                "hourlyBorrowRate": 0.0005,
                "openSpreadRate": 0.012,
                **check,
            }
        },
    }

    eligible, report = assess_astro_fs_borrow_signal(signal)
    pairs, summary = build_astro_fs_borrow_pairs([signal], config())

    assert eligible is False
    assert report["reason"] == expected_reason
    assert pairs == []
    assert summary["reasons"] == {expected_reason: 1}


def test_fs_borrow_rule_uses_strict_profit_and_spread_thresholds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ASTRO_FS_BORROW_MIN_CYCLE_PROFIT_PCT", "0.2")
    monkeypatch.setenv("ASTRO_FS_BORROW_MIN_OPEN_SPREAD_PCT", "1")
    monkeypatch.setenv("ASTRO_SPREAD_MIN_VOLUME_USDT", "200000")
    signal = {
        "symbol": "ABC",
        "futuresExchange": "okx",
        "currentFundingRate": -0.004,
        "periodHours": 4,
        "volume24h": 500_000,
        "checks": {
            "bg": {
                "hourlyBorrowRate": 0.0005,
                "openSpreadRate": 0.01,
            }
        },
    }

    eligible, report = assess_astro_fs_borrow_signal(signal)

    assert eligible is False
    assert report["cycleProfitPct"] == pytest.approx(0.2)
    assert report["reason"] == "cycle_profit_below_threshold"


def test_status_never_exposes_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    status = astro_auto_card_status(config(api_key="private-key"))
    assert status["state"] == "ready"
    assert status["defaultPaused"] is True
    assert status["defaultDisableOpen"] is False
    assert status["maxCardsPerScan"] == 0
    assert status["unlimitedCardsPerScan"] is True
    assert status["defaultLeverage"] == 3.0
    assert status["defaultMinNotionalUsdt"] == 6.0
    assert status["defaultMaxNotionalUsdt"] == 10.0
    assert status["defaultGreaterPriceAlertPct"] == 2.0
    assert status["defaultPriceChangeAlertPct"] is None
    assert status["defaultPriceChangeAlertOnlyRise"] is False
    assert "private-key" not in json.dumps(status)


def test_sf_card_starts_paused_but_open_rule_is_available_after_resume(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(tmp_path / "missing.json"))
    pair = build_astro_spread_pair(
        {
            "symbol": "BTC",
            "type": "SF",
            "buyExchange": "binance",
            "sellExchange": "gate",
            "openSpreadPct": 1.2,
            "closeSpreadPct": 0.1,
        },
        config(),
    )
    assert pair["status"] is False
    assert pair["openPosition"] == "0.012"
    assert pair["disableOpen"] is False
    assert pair["disableClose"] is False
    assert pair["leverage"] == "3"
    assert pair["greaterPriceAlert"] == "0.02"
    assert pair["priceAlert"] == ""
    assert pair["priceAlertOnlyRise"] is False


def test_spread_pair_uses_saved_alert_settings(monkeypatch, tmp_path) -> None:
    settings_path = tmp_path / "astro-spread-subscriptions.json"
    settings_path.write_text(
        json.dumps(
            {
                "greaterPriceAlertPct": 2.5,
                "priceChangeAlertPct": 4.0,
                "priceChangeAlertOnlyRise": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(settings_path))
    pair = build_astro_spread_pair(
        {
            "symbol": "BTC",
            "type": "FF",
            "buyExchange": "binance",
            "sellExchange": "gate",
            "openSpreadPct": 1.5,
        },
        config(),
    )
    assert pair["greaterPriceAlert"] == "0.025"
    assert pair["priceAlert"] == "0.04"
    assert pair["priceAlertOnlyRise"] is True


def test_spread_pair_uses_saved_order_range_and_blank_alerts(monkeypatch, tmp_path) -> None:
    settings_path = tmp_path / "astro-spread-subscriptions.json"
    settings_path.write_text(
        json.dumps(
            {
                "minNotionalUsdt": 6,
                "maxNotionalUsdt": 40,
                "greaterPriceAlertPct": None,
                "priceChangeAlertPct": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(settings_path))
    pair = build_astro_spread_pair(
        {
            "symbol": "BTC",
            "type": "FF",
            "buyExchange": "binance",
            "sellExchange": "gate",
            "openSpreadPct": 1.5,
        },
        config(),
    )
    assert pair["minNotional"] == "6"
    assert pair["maxNotional"] == "40"
    assert pair["greaterPriceAlert"] == ""
    assert pair["priceAlert"] == ""


def test_okxdex_sf_pair_carries_private_dex_configuration() -> None:
    pair = build_astro_spread_pair(
        {
            "symbol": "ABC",
            "type": "SF",
            "buyExchange": "okxdex",
            "sellExchange": "binance",
            "openSpreadPct": 1.5,
            "dexConfig": {
                "name": "ABC",
                "chainIndex": "501",
                "contractAddress": "So11111111111111111111111111111111111111112",
                "quote": "USDT",
                "slippage": "1",
            },
            "dexMapping": {"status": "confirmed"},
        },
        config(dex_api_path="/prefix123/api/config/sdk-update-dex-coin"),
    )
    assert pair["buyEx"] == "okxdex"
    assert pair["sellEx"] == "binance"
    assert pair["_dexConfig"]["chainIndex"] == "501"
    assert pair["_dexMappingConfirmed"] is True
    assert pair["_chainNote"] == "OKXDEX链：501"


def test_okxdex_sf_pair_uses_confirmed_chain_label_for_note() -> None:
    pair = build_astro_spread_pair(
        {
            "symbol": "ABC",
            "type": "SF",
            "buyExchange": "okxdex",
            "sellExchange": "gate",
            "openSpreadPct": 1.5,
            "dexConfig": {
                "name": "ABC",
                "chainIndex": "8453",
                "contractAddress": "0x1111111111111111111111111111111111111111",
            },
            "dexMapping": {"status": "confirmed", "chainLabel": "Base"},
        },
        config(),
    )
    assert pair["_chainNote"] == "OKXDEX链：Base"


def test_ff_spread_pair_builds_only_the_direct_route() -> None:
    pairs = build_astro_spread_pairs(
        {
            "symbol": "BTC",
            "type": "FF",
            "buyExchange": "binance",
            "sellExchange": "okx",
            "openSpreadPct": 1.2,
        },
        config(),
    )
    assert [(pair["buyEx"], pair["sellEx"]) for pair in pairs] == [
        ("binance", "okx"),
    ]
    assert pairs[0]["openPosition"] == "0.012"
    assert all(pair["status"] is False for pair in pairs)


def test_ff_spread_pair_never_adds_a_gc_route() -> None:
    pairs = build_astro_spread_pairs(
        {
            "symbol": "BTC",
            "type": "FF",
            "buyExchange": "gate",
            "sellExchange": "binance",
            "openSpreadPct": 1.2,
        },
        config(),
    )
    assert [(pair["buyEx"], pair["sellEx"]) for pair in pairs] == [
        ("gate", "binance"),
    ]


def test_ff_bybit_is_allowed_only_as_the_buy_leg() -> None:
    buy_routes = astro_spread_card_routes(
        {
            "type": "FF",
            "buyExchange": "bybit",
            "sellExchange": "binance",
        }
    )
    sell_routes = astro_spread_card_routes(
        {
            "type": "FF",
            "buyExchange": "binance",
            "sellExchange": "bybit",
        }
    )
    assert buy_routes == [("bybit", "binance")]
    assert sell_routes == []


def test_sf_spread_pair_builds_only_the_direct_route() -> None:
    pairs = build_astro_spread_pairs(
        {
            "symbol": "BTC",
            "type": "SF",
            "buyExchange": "binance",
            "sellExchange": "okx",
            "openSpreadPct": 1.4,
        },
        config(),
    )
    assert [(pair["buyEx"], pair["sellEx"]) for pair in pairs] == [
        ("binance", "okx"),
    ]
    assert all(not pair["buyEx"].startswith("gc-") and not pair["sellEx"].startswith("gc-") for pair in pairs)


def test_sync_worker_skips_add_when_final_revalidation_fails(monkeypatch) -> None:
    added: list[dict[str, object]] = []
    logged: list[str] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return []

        def add_pair(self, pair):
            added.append(pair)

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(
        astro_sdk_module,
        "_log",
        lambda event, **_kwargs: logged.append(event),
    )
    pair = build_astro_spread_pair(
        {
            "symbol": "BSB",
            "type": "FF",
            "buyExchange": "okx",
            "sellExchange": "gate",
            "openSpreadPct": 1.64,
        },
        config(),
    )
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker(
        [pair],
        config(),
        lambda _pair, _config: (None, {"reason": "below_threshold_or_rule_failed"}),
    )

    assert added == []
    assert "astro_card_skipped_revalidation" in logged


def test_sync_worker_has_no_local_creation_cap_when_limit_is_zero(monkeypatch) -> None:
    stored: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return stored

        def add_pair(self, pair):
            stored.append({**pair, "id": f"pair-{len(stored) + 1}"})

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(astro_sdk_module, "register_auto_created_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(astro_sdk_module, "_log", lambda *_args, **_kwargs: None)
    pairs = [
        build_astro_spread_pair(
            {
                "symbol": symbol,
                "type": "FF",
                "buyExchange": "binance",
                "sellExchange": "gate",
                "openSpreadPct": 1.2,
            },
            config(),
        )
        for symbol in ("AAA", "BBB", "CCC")
    ]
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker(pairs, config(max_cards_per_scan=0))

    assert [item["name"] for item in stored] == ["AAA", "BBB", "CCC"]


def test_sync_worker_jit_revalidates_each_route_immediately_before_submit(monkeypatch) -> None:
    stored: list[dict[str, object]] = []
    events: list[str] = []
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return stored

        def add_pair(self, pair):
            events.append(f"add:{pair['name']}")
            stored.append({**pair, "id": f"pair-{len(stored) + 1}"})

    def revalidate(pair, _config):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        events.append(f"revalidate:{pair['name']}")
        time.sleep(0.05)
        with active_lock:
            active -= 1
        return {**pair}, {**fresh_quote_report(), "durationMs": 50}

    monkeypatch.setenv("ASTRO_FINAL_REVALIDATION_WORKERS", "3")
    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(astro_sdk_module, "register_auto_created_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(astro_sdk_module, "_log", lambda *_args, **_kwargs: None)
    pairs = [
        build_astro_spread_pair(
            {
                "symbol": symbol,
                "type": "FF",
                "buyExchange": "binance",
                "sellExchange": "gate",
                "openSpreadPct": spread,
            },
            config(),
        )
        for symbol, spread in (("AAA", 1.5), ("BBB", 1.4), ("CCC", 1.3))
    ]
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker(pairs, config(), revalidate)

    assert max_active == 1
    assert events == [
        "revalidate:AAA", "add:AAA",
        "revalidate:BBB", "add:BBB",
        "revalidate:CCC", "add:CCC",
    ]
    assert [item["name"] for item in stored] == ["AAA", "BBB", "CCC"]


def test_core_not_ready_is_the_only_add_failure_retried(monkeypatch) -> None:
    attempts = 0

    class Client:
        def add_pair(self, _pair):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise astro_sdk_module.AstroSdkError("Astro core 还未准备好")

    monkeypatch.setattr(astro_sdk_module.time, "sleep", lambda _seconds: None)
    assert astro_sdk_module._add_pair_with_core_retry(Client(), {"name": "AAA"}) == 3
    assert attempts == 3

    class FatalClient:
        def add_pair(self, _pair):
            raise RuntimeError("invalid pair")

    with pytest.raises(RuntimeError, match="invalid pair"):
        astro_sdk_module._add_pair_with_core_retry(FatalClient(), {"name": "AAA"})


def test_sync_worker_isolates_one_card_failure_and_continues(monkeypatch) -> None:
    stored: list[dict[str, object]] = []
    logged: list[str] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return stored

        def add_pair(self, pair):
            if pair["name"] == "AAA":
                raise RuntimeError("route rejected")
            stored.append({**pair, "id": "pair-bbb"})

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(astro_sdk_module, "register_auto_created_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        astro_sdk_module,
        "_log",
        lambda event, **_kwargs: logged.append(event),
    )
    pairs = [
        build_astro_spread_pair(
            {
                "symbol": symbol,
                "type": "FF",
                "buyExchange": "binance",
                "sellExchange": "gate",
                "openSpreadPct": 1.2,
            },
            config(),
        )
        for symbol in ("AAA", "BBB")
    ]
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker(pairs, config())

    assert [item["name"] for item in stored] == ["BBB"]
    assert "astro_card_sync_item_failed" in logged
    assert not astro_sdk_module._sync_lock.locked()


def test_live_submit_guard_closes_block_update_race_before_add(monkeypatch) -> None:
    added: list[dict[str, object]] = []
    logged: list[tuple[str, dict[str, object]]] = []
    guard_calls = 0

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return []

        def add_pair(self, pair):
            added.append(pair)

    def guard(_pair):
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 1:
            return True, {"reason": "allowed"}
        return False, {
            "reason": "blocked_pair",
            "matchedPairs": [
                {"marketKey": "gateFuture", "symbol": "SCRTUSDT", "side": "sell"}
            ],
        }

    pair = build_astro_spread_pair(
        {
            "symbol": "SCRT",
            "type": "FF",
            "buyExchange": "bybit",
            "sellExchange": "gate",
            "openSpreadPct": 1.2,
        },
        config(),
    )
    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(
        astro_sdk_module,
        "_log",
        lambda event, **kwargs: logged.append((event, kwargs.get("details") or {})),
    )
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker(
        [pair],
        config(),
        revalidator=lambda candidate, _config: (candidate, fresh_quote_report()),
        submit_guard=guard,
    )

    assert guard_calls == 2
    assert added == []
    assert (
        "astro_card_submit_guard_blocked",
        {
            "symbol": "SCRT",
            "type": "FF",
            "buyEx": "bybit",
            "sellEx": "gate",
            "stage": "immediately_before_add",
            "reason": "blocked_pair",
            "matchedPairs": [
                {"marketKey": "gateFuture", "symbol": "SCRTUSDT", "side": "sell"}
            ],
        },
    ) in logged
    assert not astro_sdk_module._sync_lock.locked()


def test_chain_label_publisher_uses_card_id_and_never_changes_pair(monkeypatch) -> None:
    monkeypatch.setenv("ASTRO_CHAIN_LABEL_PUBLISH_ENABLED", "1")
    calls: list[dict[str, object]] = []
    logged: list[str] = []

    class Completed:
        returncode = 0
        stdout = '{"ok":true,"id":"card-123","text":"OKXDEX链：Base"}'
        stderr = ""

    monkeypatch.setattr(astro_sdk_module, "_astro_chain_label_ssh_command", lambda: ["ssh", "updater"])
    monkeypatch.setattr(
        astro_sdk_module.subprocess,
        "run",
        lambda command, **kwargs: calls.append({"command": command, **kwargs}) or Completed(),
    )
    monkeypatch.setattr(astro_sdk_module, "_log", lambda event, **_kwargs: logged.append(event))
    pair = {
        "name": "VVV",
        "buyEx": "okxdex",
        "sellEx": "aster",
        "_chainNote": "OKXDEX链：Base",
        "_dexConfig": {"chainIndex": "8453", "contractAddress": "0x1234"},
    }

    astro_sdk_module._publish_astro_chain_label(pair, {"id": "card-123"})

    assert len(calls) == 1
    assert calls[0]["command"] == ["ssh", "updater"]
    payload = json.loads(str(calls[0]["input"]))
    assert payload == {
        "id": "card-123",
        "text": "OKXDEX链：Base",
        "symbol": "VVV",
        "chainIndex": "8453",
        "contractAddress": "0x1234",
        "updatedAt": payload["updatedAt"],
    }
    assert pair["name"] == "VVV"
    assert logged == ["astro_chain_label_published"]


def test_jit_revalidation_rejects_dex_fingerprint_drift(monkeypatch) -> None:
    added: list[dict[str, object]] = []
    logged: list[str] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return []

        def add_pair(self, pair):
            added.append(pair)

    original = build_astro_spread_pair(
        {
            "symbol": "PEPE",
            "type": "SF",
            "buyExchange": "okxdex",
            "sellExchange": "binance",
            "openSpreadPct": 1.5,
            "dexConfig": {
                "chainIndex": "1",
                "contractAddress": "0x1111111111111111111111111111111111111111",
            },
            "dexMapping": {"status": "confirmed", "chainLabel": "Ethereum"},
        },
        config(),
    )

    def revalidate(pair, _config):
        changed = {**pair, "_dexConfig": {**pair["_dexConfig"], "contractAddress": "0x2222222222222222222222222222222222222222"}}
        return changed, {"reason": "eligible"}

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(astro_sdk_module, "_log", lambda event, **_kwargs: logged.append(event))
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker([original], config(), revalidate)

    assert not added
    assert "astro_card_revalidation_failed" in logged


def test_busy_sync_coalesces_routes_without_dropping_rounds() -> None:
    astro_sdk_module._reset_sync_queue_for_tests()
    pair_a = build_astro_spread_pair(
        {"symbol": "AAA", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.2},
        config(),
    )
    pair_b = build_astro_spread_pair(
        {"symbol": "BBB", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.3},
        config(),
    )

    def revalidate(pair, _config):
        return pair, {"reason": "eligible"}

    astro_sdk_module._sync_lock.acquire()
    try:
        first = astro_sdk_module.schedule_astro_pairs([pair_a], config(), revalidator=revalidate)
        second = astro_sdk_module.schedule_astro_pairs([pair_b], config(), revalidator=revalidate)
        assert first["state"] == "queued_pending"
        assert second["state"] == "queued_pending"
        assert len(astro_sdk_module._pending_sync_batches) == 1
        assert {pair["name"] for pair in astro_sdk_module._pending_sync_batches[0].pairs} == {"AAA", "BBB"}
    finally:
        astro_sdk_module._reset_sync_queue_for_tests()
        if astro_sdk_module._sync_lock.locked():
            astro_sdk_module._sync_lock.release()


def test_route_dedupe_state_includes_remote_snapshot() -> None:
    astro_sdk_module._reset_sync_queue_for_tests()
    pair = build_astro_spread_pair(
        {"symbol": "SIREN", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.2},
        config(),
    )

    astro_sdk_module._replace_existing_route_snapshot([pair])

    assert astro_sdk_module.astro_route_dedupe_state(pair) == "existing"
    assert astro_sdk_module.astro_route_dedupe_state(("OTHER", "FF", "binance", "gate")) is None
    astro_sdk_module._reset_sync_queue_for_tests()


def test_route_dedupe_state_includes_pending_sync_batch() -> None:
    astro_sdk_module._reset_sync_queue_for_tests()
    pair = build_astro_spread_pair(
        {"symbol": "SIREN", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.2},
        config(),
    )
    astro_sdk_module._sync_lock.acquire()
    try:
        astro_sdk_module.schedule_astro_pairs([pair], config(), priority=True)
        assert astro_sdk_module.astro_route_dedupe_state(pair) == "queued"
    finally:
        astro_sdk_module._reset_sync_queue_for_tests()
        if astro_sdk_module._sync_lock.locked():
            astro_sdk_module._sync_lock.release()


def test_busy_sync_keeps_fs_and_spread_revalidators_in_separate_batches() -> None:
    astro_sdk_module._reset_sync_queue_for_tests()
    spread_pair = build_astro_spread_pair(
        {"symbol": "AAA", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.2},
        config(),
    )
    fs_pair = build_astro_fs_pair(
        {"symbol": "BBB", "futuresExchange": "bn", "spotExchange": "bg", "openSpreadRate": 0.012},
        config(),
    )

    def spread_revalidator(pair, _config):
        return pair, {"reason": "eligible"}

    def fs_revalidator(pair, _config):
        return pair, {"reason": "eligible"}

    astro_sdk_module._sync_lock.acquire()
    try:
        astro_sdk_module.schedule_astro_pairs([spread_pair], config(), revalidator=spread_revalidator)
        astro_sdk_module.schedule_astro_pairs([fs_pair], config(), revalidator=fs_revalidator)
        assert len(astro_sdk_module._pending_sync_batches) == 2
        assert [batch.pairs[0]["type"] for batch in astro_sdk_module._pending_sync_batches] == ["FF", "FS"]
    finally:
        astro_sdk_module._reset_sync_queue_for_tests()
        if astro_sdk_module._sync_lock.locked():
            astro_sdk_module._sync_lock.release()


def test_busy_sync_places_hot_priority_batch_before_normal_batches() -> None:
    astro_sdk_module._reset_sync_queue_for_tests()
    normal_pair = build_astro_spread_pair(
        {"symbol": "NORMAL", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.2},
        config(),
    )
    hot_pair = build_astro_spread_pair(
        {"symbol": "HOT", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.3},
        config(),
    )

    def normal_revalidator(pair, _config):
        return pair, {"reason": "eligible"}

    def hot_revalidator(pair, _config):
        return pair, {"reason": "eligible"}

    astro_sdk_module._sync_lock.acquire()
    try:
        astro_sdk_module.schedule_astro_pairs(
            [normal_pair], config(), revalidator=normal_revalidator
        )
        result = astro_sdk_module.schedule_astro_pairs(
            [hot_pair], config(), revalidator=hot_revalidator, priority=True
        )

        assert result["state"] == "queued_pending"
        assert [batch.pairs[0]["name"] for batch in astro_sdk_module._pending_sync_batches] == [
            "HOT",
            "NORMAL",
        ]
        assert astro_sdk_module._pending_sync_batches[0].priority is True
    finally:
        astro_sdk_module._reset_sync_queue_for_tests()
        if astro_sdk_module._sync_lock.locked():
            astro_sdk_module._sync_lock.release()


def test_sync_worker_processes_creation_before_sustained_cleanup(monkeypatch) -> None:
    order: list[str] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

    pair = build_astro_spread_pair(
        {"symbol": "HOT", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.3},
        config(),
    )
    cleanup_pair = {"name": "OLD", "type": "FF", "buyEx": "binance", "sellEx": "gate"}
    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(astro_sdk_module, "_list_pairs_with_retry", lambda _client: [])
    monkeypatch.setattr(
        astro_sdk_module,
        "observe_auto_card_cleanup",
        lambda *_args, **_kwargs: [{"pair": cleanup_pair}],
    )
    monkeypatch.setattr(
        astro_sdk_module,
        "apply_delete_rearm_rules",
        lambda pairs, _existing, **_kwargs: (pairs, [], []),
    )
    monkeypatch.setattr(
        astro_sdk_module,
        "_sync_candidate_pair",
        lambda *_args, **_kwargs: order.append("create") or False,
    )
    monkeypatch.setattr(
        astro_sdk_module,
        "_process_cleanup_item",
        lambda *_args, **_kwargs: order.append("cleanup") or None,
    )
    astro_sdk_module._sync_lock.acquire()

    astro_sdk_module._sync_pair_worker([pair], config())

    assert order == ["create", "cleanup"]


def test_fast_verification_poll_stops_as_soon_as_pair_appears() -> None:
    target = {
        "name": "ARX",
        "type": "FF",
        "buyEx": "binance",
        "sellEx": "gate",
    }

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def list_pairs(self):
            self.calls += 1
            return [] if self.calls == 1 else [target]

    client = FakeClient()
    _pairs, verification = astro_sdk_module._wait_for_pair(
        client,  # type: ignore[arg-type]
        astro_sdk_module._pair_identity(target),
        timeout_seconds=1,
        poll_seconds=0.001,
    )

    assert client.calls == 2
    assert verification["mode"] == "fast_poll"
    assert verification["attempts"] == 2
    assert verification["durationMs"] < 200


def test_cleanup_deletes_only_verified_never_traded_paused_auto_card(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    monkeypatch.setenv("ASTRO_AUTO_CARD_CLEANUP_GRACE_SECONDS", "30")
    monkeypatch.setenv("ASTRO_AUTO_CARD_CLEANUP_INVALID_SECONDS", "10")
    reset_registry_for_tests()
    expected = build_astro_spread_pair(
        {
            "symbol": "RVN",
            "type": "FF",
            "buyExchange": "gate",
            "sellExchange": "okx",
            "openSpreadPct": 1.2,
        },
        config(),
    )
    live = {
        **expected,
        "id": "pair-rvn",
        "status": False,
        "aExPosition": 0,
        "bExPosition": 0,
        "aMaxPos": 0,
        "bMaxPos": 0,
        "avgOpenAExPrice": 0,
        "avgOpenBExPrice": 0,
        "realizedProfit": 0,
    }
    now = datetime.now(timezone.utc)
    register_auto_created_pair(expected, astro_pair=live, now=now - timedelta(minutes=5))
    observation = {
        ("RVN", "FF", "gate", "okx"): {
            "state": "invalid",
            "reason": "spread_below_ff_threshold",
            "quoteAt": int(now.timestamp() * 1000),
        }
    }
    observe_auto_card_cleanup(
        observation,
        [live],
        now=now - timedelta(seconds=11),
        grace_seconds=30,
        invalid_seconds=10,
    )
    deleted: list[str] = []
    logged: list[str] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            self.deleted = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return [] if self.deleted else [live]

        def delete_pair(self, pair_id):
            deleted.append(pair_id)
            self.deleted = True

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(astro_sdk_module, "_log", lambda event, **_kwargs: logged.append(event))
    astro_sdk_module._sync_lock.acquire()
    astro_sdk_module._sync_pair_worker(
        [],
        config(),
        route_observations=observation,
        cleanup_revalidator=lambda *_args: (True, {"reason": "confirmed"}),
    )

    assert deleted == ["pair-rvn"]
    assert "astro_card_auto_deleted" in logged


def test_cleanup_protects_card_after_any_trade_or_exposure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    monkeypatch.setenv("ASTRO_AUTO_CARD_CLEANUP_GRACE_SECONDS", "30")
    monkeypatch.setenv("ASTRO_AUTO_CARD_CLEANUP_INVALID_SECONDS", "10")
    reset_registry_for_tests()
    expected = build_astro_spread_pair(
        {"symbol": "RVN", "type": "FF", "buyExchange": "gate", "sellExchange": "okx", "openSpreadPct": 1.2},
        config(),
    )
    live = {
        **expected,
        "id": "pair-rvn",
        "status": False,
        "aExPosition": 0,
        "bExPosition": 0,
        "aMaxPos": 1,
        "bMaxPos": 0,
        "avgOpenAExPrice": 0,
        "avgOpenBExPrice": 0,
        "realizedProfit": 0,
    }
    now = datetime.now(timezone.utc)
    register_auto_created_pair(expected, astro_pair=live, now=now - timedelta(minutes=5))
    observation = {("RVN", "FF", "gate", "okx"): {"state": "invalid", "reason": "spread_below_ff_threshold"}}
    observe_auto_card_cleanup(
        observation, [live], now=now - timedelta(seconds=11), grace_seconds=30, invalid_seconds=10
    )
    deleted: list[str] = []

    class FakeClient:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_pairs(self):
            return [live]

        def delete_pair(self, pair_id):
            deleted.append(pair_id)

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", FakeClient)
    monkeypatch.setattr(astro_sdk_module, "_log", lambda *_args, **_kwargs: None)
    astro_sdk_module._sync_lock.acquire()
    astro_sdk_module._sync_pair_worker([], config(), route_observations=observation)
    assert deleted == []


def test_cleanup_blocked_log_is_daily_per_card_and_reason(monkeypatch) -> None:
    written: list[dict[str, object]] = []
    clock = {"now": 100.0}
    monkeypatch.setattr(astro_sdk_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        astro_sdk_module,
        "append_system_runtime_event",
        lambda event, **kwargs: written.append({"event": event, **kwargs}),
    )
    with astro_sdk_module._log_throttle_lock:
        astro_sdk_module._log_throttle.clear()

    common = {
        "symbol": "NES",
        "type": "FF",
        "buyEx": "aster",
        "sellEx": "okx",
        "reason": "card_not_paused",
    }
    astro_sdk_module._log(
        "astro_card_cleanup_blocked",
        level="info",
        message="protected",
        details={**common, "cardId": "pair-1"},
    )
    clock["now"] += 300
    astro_sdk_module._log(
        "astro_card_cleanup_blocked",
        level="info",
        message="protected",
        details={**common, "cardId": "pair-1"},
    )
    astro_sdk_module._log(
        "astro_card_cleanup_blocked",
        level="info",
        message="protected",
        details={**common, "cardId": "pair-2"},
    )
    clock["now"] += astro_sdk_module._CLEANUP_BLOCKED_LOG_INTERVAL_SECONDS
    astro_sdk_module._log(
        "astro_card_cleanup_blocked",
        level="info",
        message="protected",
        details={**common, "cardId": "pair-1"},
    )

    assert [item["details"]["cardId"] for item in written] == ["pair-1", "pair-2", "pair-1"]


def test_running_card_cleanup_protection_is_info_and_keeps_card_id(monkeypatch) -> None:
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        astro_sdk_module,
        "_log",
        lambda event, **kwargs: logged.append((event, kwargs)),
    )
    card = {
        "id": "pair-live",
        "name": "NES",
        "type": "FF",
        "buyEx": "aster",
        "sellEx": "okx",
        "status": True,
    }
    item = {
        "pair": card,
        "record": {},
        "observation": {"reason": "spread_below_ff_threshold"},
        "invalidForSeconds": 120.0,
    }

    result = astro_sdk_module._process_cleanup_item(object(), item, config(), None)  # type: ignore[arg-type]

    assert result is None
    assert logged == [
        (
            "astro_card_cleanup_blocked",
            {
                "level": "info",
                "message": "Astro 自动清理已保护卡片，不执行删除：NES",
                "details": {
                    "symbol": "NES",
                    "type": "FF",
                    "buyEx": "aster",
                    "sellEx": "okx",
                    "cardId": "pair-live",
                    "reason": "card_not_paused",
                },
            },
        )
    ]


def cleanup_fixture() -> tuple[dict, dict]:
    from app.astro_card_registry import _route_record

    pair = build_astro_spread_pair(
        {"symbol": "REVIEW", "type": "FF", "buyExchange": "binance", "sellExchange": "gate", "openSpreadPct": 1.2},
        config(),
    )
    pair.update(id="review-card", aExPosition=0, bExPosition=0, aMaxPos=0, bMaxPos=0,
                avgOpenAExPrice=0, avgOpenBExPrice=0, realizedProfit=0,
                adjustParams={"enabled": False, "step": 0}, boostMode=False,
                slowMode=False, stepOpen="0", stepClose="0", stopLoss="", lessPriceAlert="")
    return pair, _route_record(pair, datetime.now(timezone.utc), astro_pair=pair)


@pytest.mark.parametrize("field,value", [
    ("minNotional", "20"), ("maxNotional", "80"), ("greaterPriceAlert", "0.03"),
    ("lessPriceAlert", "0.001"), ("priceAlertOnlyRise", True), ("startTime", "1234"),
    ("adjustParams", {"enabled": False, "step": False}), ("boostMode", True),
    ("slowMode", True), ("stepOpen", "0.002"), ("stepClose", "0.001"), ("stopLoss", "0.1"),
])
def test_cleanup_protects_every_editable_setting(field, value) -> None:
    pair, record = cleanup_fixture()
    assert astro_sdk_module._cleanup_safety_check(record, pair)[0] is True
    pair[field] = value
    assert astro_sdk_module._cleanup_safety_check(record, pair) == (False, f"card_config_changed:{field}")


def test_cleanup_protects_legacy_or_unreadable_config() -> None:
    pair, record = cleanup_fixture()
    legacy = {key: value for key, value in record.items() if key != "createdPairSnapshotVersion"}
    assert astro_sdk_module._cleanup_safety_check(legacy, pair)[1] == "legacy_record_without_complete_config_snapshot"
    record["submittedOnlyConfigFields"] = ["priceAlertOnlyRise"]
    pair.pop("priceAlertOnlyRise")
    pair["priceAlert"] = record["createdPair"]["priceAlert"] = "0.02"
    assert astro_sdk_module._cleanup_safety_check(record, pair)[1] == "submitted_config_not_readable:priceAlertOnlyRise"


@pytest.mark.parametrize("current_alert", ["", "0.02", None])
def test_unreadable_direction_is_ignored_only_when_alert_explicitly_disabled(current_alert) -> None:
    pair, _ = cleanup_fixture()
    submitted = dict(pair)
    pair.pop("priceAlertOnlyRise")
    record = registry_module._route_record(submitted, datetime.now(timezone.utc), astro_pair=pair)
    if current_alert is None:
        pair.pop("priceAlert")
    else:
        pair["priceAlert"] = current_alert
    safe, reason = astro_sdk_module._cleanup_safety_check(record, pair)
    assert safe is (current_alert == "")
    if not safe:
        assert reason == "submitted_config_not_readable:priceAlertOnlyRise"


@pytest.mark.parametrize("changed", [
    {"status": True}, {"aExPosition": 1}, {"aMaxPos": 1}, {"minNotional": "20"},
    {"id": "replacement-card"},
])
def test_cleanup_rechecks_live_card_after_market_validation(monkeypatch, changed) -> None:
    pair, record = cleanup_fixture()
    state = {"card": pair}
    deleted = []
    monkeypatch.setattr(astro_sdk_module, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(astro_sdk_module, "reset_auto_card_invalid_observation", lambda *_args: None)
    monkeypatch.setattr(astro_sdk_module, "_creation_sync_pending", lambda: False)

    class Client:
        def list_pairs(self):
            return [state["card"]]

        def delete_pair(self, pair_id):
            deleted.append(pair_id)

    def validate(*_args):
        state["card"] = {**pair, **changed}
        return True, {"reason": "continuous_invalidity_confirmed"}

    item = {"pair": pair, "record": record, "observation": {"reason": "spread_below_ff_threshold"}, "invalidForSeconds": 120}
    assert astro_sdk_module._process_cleanup_item(Client(), item, config(), validate) is None
    assert deleted == []


def test_cleanup_yields_when_creation_arrives_during_market_validation(monkeypatch) -> None:
    pair, record = cleanup_fixture()
    pending = [False]
    monkeypatch.setattr(astro_sdk_module, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(astro_sdk_module, "_creation_sync_pending", lambda: pending[0])

    def validate(*_args):
        pending[0] = True
        return True, {"reason": "continuous_invalidity_confirmed"}

    # An object with no SDK methods proves no extra read/delete starts after
    # the new candidate arrives; the next batch will revisit housekeeping.
    item = {"pair": pair, "record": record, "observation": {}, "invalidForSeconds": 120}
    assert astro_sdk_module._process_cleanup_item(object(), item, config(), validate) is None


def sdk_fake_clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(astro_sdk_module, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))
    return clock


@pytest.mark.parametrize("action", ["list", "add"])
def test_sdk_deadline_cancels_slow_network_and_preserves_transport(action) -> None:
    resolved = config(timeout_seconds=12.0)
    cancelled = []
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["user-agent"] == "stock-review-mac/astro-sdk"
        assert request.headers["x-sign"] == sign_astro_request(resolved.api_key, int(request.headers["x-timestamp"]), request.headers["x-nonce"], resolved.api_path, request.content.decode())
        if len(requests) == 1:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return httpx.Response(200, json={"code": 0, "data": []})

    with AstroSdkClient(resolved, transport=httpx.MockTransport(handler)) as client:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            if action == "list":
                client.list_pairs(deadline=started + 0.05)
            else:
                client.add_pair({"name": "ABC", "status": False}, deadline=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert cancelled == [True]
        assert client.list_pairs(deadline=time.monotonic() + 0.5) == []
        assert requests[0]["action"] == action


@pytest.mark.parametrize("wait_mode", ["list_retry", "present", "absent"])
def test_read_and_confirmation_share_one_total_deadline(wait_mode, monkeypatch) -> None:
    cancellations = []

    async def slow_handler(_request):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancellations.append(True)
            raise

    with AstroSdkClient(config(timeout_seconds=12.0), transport=httpx.MockTransport(slow_handler)) as client:
        started = time.monotonic()
        with pytest.raises(astro_sdk_module.AstroSdkError):
            if wait_mode == "list_retry":
                astro_sdk_module._list_pairs_with_retry(client, attempts=4, deadline=started + 0.1)
            else:
                wait = astro_sdk_module._wait_for_pair if wait_mode == "present" else astro_sdk_module._wait_for_pair_absent
                wait(client, ("ABC", "FF", "binance", "bitget"), timeout_seconds=0.1, poll_seconds=0.4)
        assert time.monotonic() - started < 0.5
        assert cancellations == [True]


@pytest.mark.parametrize("operation", ["list", "add"])
def test_retry_sleep_and_requests_consume_same_budget(monkeypatch, operation) -> None:
    clock = sdk_fake_clock(monkeypatch)
    deadlines = []

    class Client:
        deadline_support = True

        def list_pairs(self, *, deadline):
            deadlines.append(deadline)
            clock[0] += 0.1
            raise astro_sdk_module.AstroSdkError("temporary read error")

        def add_pair(self, _pair, *, deadline):
            deadlines.append(deadline)
            clock[0] += 0.1
            raise astro_sdk_module.AstroSdkError("core not ready")

    deadline = clock[0] + 0.4
    with pytest.raises((astro_sdk_module.AstroSdkError, TimeoutError)):
        if operation == "list":
            astro_sdk_module._list_pairs_with_retry(Client(), deadline=deadline)
        else:
            astro_sdk_module._add_pair_with_core_retry(Client(), {"name": "ABC"}, deadline=deadline)
    # This fake advances time in a request; production additionally cancels
    # the await at deadline, tested above. No later retry gets a fresh budget.
    assert all(value == deadline for value in deadlines)
    assert clock[0] <= deadline + 0.1


@pytest.mark.parametrize("outcome", ["acknowledged", "timeout"])
def test_uncertain_submission_is_persistent_blocks_readd_and_reconciles(monkeypatch, outcome) -> None:
    pair, _ = cleanup_fixture()
    visible = []
    added = []
    monkeypatch.setattr(astro_sdk_module, "_log", lambda *_args, **_kwargs: None)

    class Client:
        def list_pairs(self):
            return list(visible)

        def add_pair(self, candidate):
            added.append(candidate)
            if outcome == "timeout":
                raise httpx.ReadTimeout("unknown add outcome")

    def unconfirmed(*_args, **_kwargs):
        raise astro_sdk_module.AstroSdkError("confirmation deadline exhausted")

    monkeypatch.setattr(astro_sdk_module, "_wait_for_pair", unconfirmed)
    if outcome == "timeout":
        with pytest.raises(httpx.ReadTimeout):
            astro_sdk_module._sync_candidate_pair(Client(), pair, config(), None, set())
    else:
        assert astro_sdk_module._sync_candidate_pair(Client(), pair, config(), None, set()) is False
    assert len(added) == 1
    status = astro_auto_card_status(config())
    assert status["pendingSubmissionCount"] == 1
    assert status["pendingSubmissions"]["items"][0]["state"] == ("outcome_unknown" if outcome == "timeout" else "awaiting_confirmation")
    # Simulate process-local cache loss. Durable pending still blocks a
    # candidate which was already queued before the original submission.
    astro_sdk_module._pending_submission_routes.clear()
    assert astro_sdk_module._sync_candidate_pair(Client(), pair, config(), None, set()) is False
    assert len(added) == 1
    assert astro_sdk_module.astro_route_dedupe_state(pair) == "submission_pending"
    visible.append(pair)
    astro_sdk_module._replace_existing_route_snapshot(visible)
    assert astro_auto_card_status(config())["pendingSubmissionCount"] == 0
    adopted = registry_module.auto_created_route_records()[0]
    assert adopted["astroPairId"] == pair["id"]
    assert astro_sdk_module._cleanup_safety_check(adopted, pair) == (False, "submission_confirmed_after_uncertain_wait")
    assert astro_sdk_module._sync_candidate_pair(Client(), pair, config(), None, set()) is False
    assert len(added) == 1


def test_transport_error_cannot_masquerade_as_safe_core_retry() -> None:
    assert astro_sdk_module._core_not_ready_error(httpx.ReadTimeout("core not ready")) is False


def test_corrupt_registry_preserves_pending_cache_and_prevents_add(monkeypatch) -> None:
    pair, _ = cleanup_fixture()
    astro_sdk_module._record_pending_submission(pair, "outcome_unknown")
    path = registry_module._registry_path()
    path.write_text("{incomplete", encoding="utf-8")
    with pytest.raises(RuntimeError, match="注册表读取失败"):
        astro_sdk_module._refresh_pending_submission_routes()
    assert astro_sdk_module.astro_route_dedupe_state(pair) == "submission_pending"

    class Client:
        def list_pairs(self):
            return []

        def add_pair(self, _candidate):
            raise AssertionError("corrupt pending storage must prevent remote add")

    with pytest.raises(RuntimeError, match="注册表读取失败"):
        astro_sdk_module._sync_candidate_pair(Client(), pair, config(), None, set())
    assert path.read_text(encoding="utf-8") == "{incomplete"


def test_pending_refresh_cannot_overwrite_concurrent_submission(monkeypatch) -> None:
    pair, _ = cleanup_fixture()
    status_read = threading.Event()
    resume_refresh = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    original_status = astro_sdk_module.pending_astro_submission_status

    def delayed_status():
        status = original_status()
        status_read.set()
        assert resume_refresh.wait(2)
        return status

    def record():
        writer_started.set()
        astro_sdk_module._record_pending_submission(pair, "submitting")
        writer_done.set()

    monkeypatch.setattr(astro_sdk_module, "pending_astro_submission_status", delayed_status)
    refresher = threading.Thread(target=astro_sdk_module._refresh_pending_submission_routes)
    writer = threading.Thread(target=record)
    refresher.start()
    assert status_read.wait(2)
    writer.start()
    assert writer_started.wait(2)
    writer_done.wait(0.1)
    resume_refresh.set()
    refresher.join(2)
    writer.join(2)
    assert not refresher.is_alive() and not writer.is_alive()
    assert original_status()["count"] == 1
    assert astro_sdk_module.astro_route_dedupe_state(pair) == "submission_pending"


@pytest.mark.parametrize("scenario", ["slow_post_jit_list", "expired_core_retry", "missing_proof"])
def test_sdk_never_submits_expired_or_missing_final_proof(monkeypatch, scenario) -> None:
    clock = sdk_fake_clock(monkeypatch)
    pair, _ = cleanup_fixture()
    calls = {"list": 0, "add": 0}
    logs = []
    monkeypatch.setattr(astro_sdk_module, "_log", lambda event, **kwargs: logs.append((event, kwargs)))

    class Client:
        def list_pairs(self):
            calls["list"] += 1
            if calls["list"] == 1 and scenario == "slow_post_jit_list":
                clock[0] += 3.1
            return []

        def add_pair(self, _pair):
            calls["add"] += 1
            raise astro_sdk_module.AstroSdkError("core not ready")

    def validate(candidate, _config):
        if scenario == "missing_proof":
            return candidate, {"reason": "eligible"}
        age_ms = 2700 if scenario == "expired_core_retry" else 0
        return candidate, fresh_quote_report(int(clock[0] * 1000) - age_ms)

    assert astro_sdk_module._sync_candidate_pair(Client(), pair, config(), validate, set()) is False
    assert calls["add"] == (1 if scenario == "expired_core_retry" else 0)
    rejection = next(details for event, details in logs if event == "astro_card_submit_quote_expired")
    assert rejection["details"]["reason"] == ("submit_quote_evidence_missing" if scenario == "missing_proof" else "submit_deadline_exhausted")


def test_submission_freshness_uses_dex_limit_and_monotonic_age(monkeypatch) -> None:
    clock = sdk_fake_clock(monkeypatch)
    report = fresh_quote_report(999_000, limit=3)
    report["quoteAgeLimitsSeconds"]["buy"] = 1.5
    clock[0] = 999.0  # wall clock moved backwards after validation
    passed, details = astro_sdk_module._submission_quote_freshness(report, completed_at_ms=1_000_000, elapsed_seconds=0.6)
    assert passed is False
    assert details["side"] == "buy"
    assert details["legs"]["buy"]["ageSeconds"] == 1.6


def test_queue_coalescing_retains_real_first_enqueue_and_latest_payload(monkeypatch) -> None:
    clock = sdk_fake_clock(monkeypatch)
    astro_sdk_module._reset_sync_queue_for_tests()
    pair, _ = cleanup_fixture()
    astro_sdk_module._sync_lock.acquire()
    try:
        astro_sdk_module.schedule_astro_pairs([pair], config())
        clock[0] += 5
        astro_sdk_module.schedule_astro_pairs([{**pair, "openPosition": "0.02"}], config())
        queued = astro_sdk_module._pending_sync_batches[0].pairs[0]
        assert queued["openPosition"] == "0.02"
        assert queued["_pipeline"]["queuedAtMs"] == 1_000_000
        assert queued["_pipeline"]["latestEnqueuedAtMs"] == 1_005_000
        assert "workerStartedAtMs" not in queued["_pipeline"]
    finally:
        astro_sdk_module._reset_sync_queue_for_tests()
        astro_sdk_module._sync_lock.release()


def test_worker_start_does_not_rewrite_queue_entry(monkeypatch) -> None:
    clock = sdk_fake_clock(monkeypatch)
    pair, _ = cleanup_fixture()
    pair["_pipeline"] = {"queuedAtMs": 990_000}
    captured = []

    class Client:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def listing(_client):
        clock[0] += 2
        return []

    monkeypatch.setattr(astro_sdk_module, "AstroSdkClient", Client)
    monkeypatch.setattr(astro_sdk_module, "_list_pairs_with_retry", listing)
    monkeypatch.setattr(astro_sdk_module, "observe_auto_card_cleanup", lambda *_args: [])
    monkeypatch.setattr(astro_sdk_module, "apply_delete_rearm_rules", lambda pairs, *_args, **_kwargs: (pairs, [], []))
    monkeypatch.setattr(astro_sdk_module, "_sync_candidate_pair", lambda _client, candidate, *_args: captured.append(candidate["_pipeline"]) or False)
    astro_sdk_module._sync_lock.acquire()
    astro_sdk_module._sync_pair_worker([pair], config())
    assert captured[0]["queuedAtMs"] == 990_000
    assert captured[0]["workerStartedAtMs"] == 1_000_000
    assert captured[0]["queueWaitMs"] == 10_000
    assert captured[0]["batchPreparedAtMs"] == 1_002_000


def test_sync_timeout_log_has_root_type_stage_and_duration(monkeypatch):
    logs=[]
    class Client:
        def __init__(self,*a,**k): pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def list_pairs(self):raise TimeoutError()
    monkeypatch.setattr(astro_sdk_module,'AstroSdkClient',Client)
    monkeypatch.setattr(astro_sdk_module.time,'sleep',lambda _:None)
    monkeypatch.setattr(astro_sdk_module,'_log',lambda event,**k:logs.append((event,k)))
    monkeypatch.setattr(astro_sdk_module,'_start_next_pending_sync',lambda:None)
    astro_sdk_module._sync_pair_worker([],config())
    event,entry=next(r for r in logs if r[0]=='astro_card_sync_failed')
    assert entry['message'] and 'TimeoutError' in entry['message']
    assert entry['details']['errorType']=='TimeoutError'
    assert entry['details']['stage']=='list_pairs'
    assert entry['details']['durationMs']>=0


def test_final_jit_spread_must_still_pass_rearm_before_add(monkeypatch):
    route=build_astro_spread_pair({'symbol':'BNC','type':'FF','buyExchange':'bybit','sellExchange':'bitget','openSpreadPct':1.5},config())
    start=datetime.now(timezone.utc)-timedelta(minutes=2)
    register_auto_created_pair({**route,'openPosition':'.012'},now=start)
    for seconds in (5,10):
        registry_module.apply_delete_rearm_rules([],[],now=start+timedelta(seconds=seconds),confirmation_seconds=5)
    monkeypatch.setattr(registry_module,'astro_delete_rearm_pct',lambda:20)
    monkeypatch.setattr(astro_sdk_module,'_submission_quote_freshness',lambda *a,**k:(True,{'legs':{'buy':{'maxAgeSeconds':30,'ageSeconds':0},'sell':{'maxAgeSeconds':30,'ageSeconds':0}}}))
    monkeypatch.setattr(astro_sdk_module,'_log',lambda *a,**k:None)
    class Client:
        def list_pairs(self):return []
        def add_pair(self,*a,**k):pytest.fail('spread fell below rearm threshold')
    def validate(pair,cfg):return {**pair,'openPosition':'.013'}, {'reason':'eligible'}
    assert astro_sdk_module._sync_candidate_pair(Client(),route,config(),validate,set()) is False

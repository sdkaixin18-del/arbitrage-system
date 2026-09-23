"""Removing optional economics must leave the executable card gate intact."""
from copy import deepcopy
import json
import time

import pytest

from app import astro_spread_scanner as scanner, crypto
from test_astro_spread_scanner import sdk_config


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_REVIEW_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scanner, "_state", {})
    monkeypatch.setattr(scanner, "append_system_runtime_event", lambda *a, **kw: {})
    monkeypatch.setattr(scanner, "_load_pulse_symbol_aliases", lambda: {})
    monkeypatch.setattr(scanner, "spread_scan_exclude_delisted_exchange_cards", lambda: False)
    monkeypatch.setattr(scanner, "structure_filter_enabled", lambda: False)
    monkeypatch.setattr(crypto, "fetch_fs_market_quote", lambda *a, **kw: pytest.fail("funding read"))
    path = tmp_path / "astro-spread-subscriptions.json"
    path.write_text(json.dumps({
        "markets": ["binanceSpot", "binanceFuture", "bitgetFuture"],
        "ffMinOpenSpreadPct": 0.8, "sfMinOpenSpreadPct": 0.8,
        "minNotionalUsdt": 6, "maxNotionalUsdt": 20,
        "arbitragePolicy": {"enabled": True, "minNetEdgePct": 99,
                            "unknownEvidenceAction": "skip", "routeTargets": []},
    }))
    return path


@pytest.mark.parametrize("kind", ["SF", "FF"])
def test_old_enabled_policy_cannot_block_or_modify_paused_cards(isolated, monkeypatch, kind):
    calls = []
    def read(_client, exchange, market, symbol, aliases):
        calls.append((exchange, market))
        stamp = int(time.time() * 1000)
        buy = exchange == "binance"
        return {"asks": [[100 if buy else 104, 100]], "bids": [[99 if buy else 103, 100]],
                "timestamp": stamp, "receivedAt": stamp, "localReceivedAt": stamp,
                "timestampSource": "exchange_depth_update", "bestAsk": 100 if buy else 104,
                "bestBid": 99 if buy else 103, "bestAskQuantity": 100, "bestBidQuantity": 100,
                "exchange": exchange, "market": market, "requestDurationMs": 1,
                "endpoint": "https://exchange.example/depth"}
    monkeypatch.setattr(scanner, "_fetch_direct_depth_book", read)
    route = {"name": "WOO", "type": kind, "buyEx": "binance", "sellEx": "bitget",
             "_buyVolume24hUsdt": 500_000, "_sellVolume24hUsdt": 500_000}
    pair, report = scanner._fetch_direct_route_once_local(route, sdk_config(), executable_depth_first=True)
    assert pair is not None, report
    assert pair["status"] is False and pair["disableOpen"] is False
    assert "_arbitragePlan" not in pair and "arbitragePlan" not in report
    preflight = report["cexExecutablePreflight"]
    assert preflight["quoteNotionalUsdt"] == 20
    assert preflight["tokenQuantity"] == pytest.approx(0.2)
    assert preflight["sellExecution"]["filledQuantity"] == preflight["tokenQuantity"]
    assert "qualityAssessment" not in preflight
    assert sorted(calls) == [("binance", "spot" if kind == "SF" else "future"), ("bitget", "future")]


def test_saved_malformed_policy_is_ignored_and_dropped_on_next_save(isolated, monkeypatch):
    settings = json.loads(isolated.read_text())
    settings["arbitragePolicy"] = "invalid retired policy"
    isolated.write_text(json.dumps(settings))
    monkeypatch.setattr(scanner, "astro_spread_scanner_status", lambda: {})
    scanner.update_astro_spread_subscriptions(settings["markets"], min_notional_usdt=6, max_notional_usdt=20)
    saved = json.loads(isolated.read_text())
    assert "arbitragePolicy" not in saved
    assert saved["markets"] == settings["markets"]
    assert saved["maxNotionalUsdt"] == 20
    assert saved["ffMinOpenSpreadPct"] == 0.8


def test_insufficient_real_entry_depth_still_blocks(isolated, monkeypatch):
    def read(*a):
        return {"asks": [[100, 0.01]], "bids": [[103, 0.01]]}
    monkeypatch.setattr(scanner, "_fetch_direct_depth_book", read)
    with pytest.raises(RuntimeError, match="深度不足"):
        scanner._fetch_cex_executable_preflight(object(), "WOO", "FF", "binance", "bitget", {}, sdk_config())

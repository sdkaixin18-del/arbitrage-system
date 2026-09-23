import time
from concurrent.futures import Future
import pytest
import app.astro_contract_safety as safety
import app.astro_spread_scanner as scanner


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(safety, "prefetch", lambda: None)
    monkeypatch.setattr(safety, "_fetched_at", time.time())
    monkeypatch.setattr(safety, "_rows", {})
    monkeypatch.setattr(scanner, "_active_delisting_exchange_blocks", lambda: set())
    monkeypatch.setattr(scanner, "spread_scan_exclude_delisted_exchange_cards", lambda: True)
    monkeypatch.setattr(scanner, "_state", {})


def payload(symbol="ESPORTS", off="1788850800000", limit="1788840000000"):
    return {"code": "00000", "data": [{"symbol": symbol+"USDT", "baseCoin": symbol, "quoteCoin": "USDT",
            "symbolStatus": "normal", "offTime": off, "limitOpenTime": limit}]}


def test_trading_normally_still_blocks_a_future_delisting(monkeypatch):
    rows = safety.parse_bitget_contracts(payload())
    monkeypatch.setattr(safety, "_rows", rows)
    row = safety.route_check("ESPORTS", "FF", "binance", "bitget")
    assert row["decision"] == "reject"
    assert row["reason"] == "contract_delisting_announced"
    assert row["offTimeMs"] == 1788850800000
    assert row["limitOpenTimeMs"] == 1788840000000


def test_delisting_is_only_for_the_affected_future_leg(monkeypatch):
    monkeypatch.setattr(safety, "_rows", safety.parse_bitget_contracts(payload()))
    assert safety.route_check("ESPORTS", "FF", "binance", "gate") is None
    assert safety.route_check("ESPORTS", "SF", "bitget", "gate") is None
    assert safety.route_check("ESPORTS", "FF", "bitget", "gate")["decision"] == "reject"


def test_normal_negative_sentinels_and_stale_inventory(monkeypatch):
    monkeypatch.setattr(safety, "_rows", safety.parse_bitget_contracts(payload("WOO", "-1", "-1")))
    assert safety.route_check("WOO", "FF", "binance", "bitget") is None
    monkeypatch.setattr(safety, "_fetched_at", time.time() - 301)
    assert safety.route_check("WOO", "FF", "binance", "bitget")["reason"] == "contract_status_pending"


def test_known_delisting_survives_refresh_failure(monkeypatch):
    monkeypatch.setattr(safety, "_rows", safety.parse_bitget_contracts(payload()))
    monkeypatch.setattr(safety, "_fetched_at", time.time() - 600)
    f = Future(); f.set_exception(TimeoutError())
    safety._completed(f)
    assert safety.route_check("ESPORTS", "FF", "binance", "bitget")["decision"] == "reject"


@pytest.mark.parametrize("body", [{}, {"code": "40000", "data": []}, {"code": "00000", "data": []}])
def test_bad_or_empty_inventory_never_means_no_delisting(body):
    with pytest.raises(ValueError):
        safety.parse_bitget_contracts(body)


def test_direct_route_stops_before_books_for_delisting(monkeypatch):
    monkeypatch.setattr(safety, "_rows", safety.parse_bitget_contracts(payload()))
    monkeypatch.setattr(scanner, "_load_pulse_symbol_aliases", lambda: pytest.fail("must reject before metadata/books"))
    pair, report = scanner._fetch_direct_route_once_local(
        {"name": "ESPORTS", "type": "FF", "buyEx": "binance", "sellEx": "bitget"}, None)
    assert pair is None and report["reason"] == "contract_delisting_announced"
    assert report["contractLifecycle"]["decision"] == "reject"


def test_discovery_filter_records_the_concrete_delisting_reason(monkeypatch):
    events = []
    monkeypatch.setattr(scanner, "_append_decision_audit", lambda *args, **kwargs: events.append(kwargs))
    monkeypatch.setattr(safety, "_rows", safety.parse_bitget_contracts(payload()))
    candidate = {"symbol": "ESPORTS", "type": "FF", "buyExchange": "binance", "sellExchange": "bitget"}
    kept, summary = scanner._filter_delisted_exchange_candidates([candidate])
    assert kept == [] and summary["filteredCandidateCount"] == 1
    assert events[0]["decision"] == "contract_delisting_announced"
    assert "latestArbitrageDecision" not in scanner._state

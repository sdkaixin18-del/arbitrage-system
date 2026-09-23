"""Funding interval provenance must survive display defaults without new reads."""
import math

import pytest

from app import crypto


NOW = 1_800_000_000_000
BINANCE_DEFAULT_SOURCE = "official_default_8h_after_successful_adjustment_list"


def fake_market_api(exchange, known, calls):
    def request(_client, url, params=None, **_kwargs):
        calls.append(url)
        if "fundingInfo" in url:
            return [{"symbol": "ABCUSDT", "fundingIntervalHours": "4"}] if known else []
        if "bookTicker" in url:
            return {"bidPrice": "99", "askPrice": "100"}
        if "premiumIndex" in url:
            return {"markPrice": "100", "indexPrice": "100", "lastFundingRate": "-0.001", "nextFundingTime": NOW}
        if exchange in {"bn", "as"}:
            return {}
        if exchange == "by":
            if "orderbook" in url:
                return {"result": {"b": [["99", "1"]], "a": [["100", "1"]]}}
            if "instruments-info" in url:
                return {"result": {"list": [{"fundingInterval": "240"} if known else {}]}}
            return {"result": {"list": [{"fundingRate": "-0.001", "nextFundingTime": NOW}]}}
        if exchange == "gt":
            if "order_book" in url:
                return {"bids": [{"p": "99", "s": "1"}], "asks": [{"p": "100", "s": "1"}]}
            if "/contracts/" in url:
                return {"funding_rate": "-0.001", "funding_interval": 14400 if known else None}
            return []
        if exchange == "okx":
            if "funding-rate" in url:
                row = {"fundingRate": "-0.001", "fundingTime": NOW}
                if known:
                    row["nextFundingTime"] = NOW + 4 * 3600_000
            elif "books" in url:
                row = {"bids": [["99", "1"]], "asks": [["100", "1"]]}
            else:
                row = {}
            return {"code": "0", "data": [row]}
        if exchange == "bg":
            if "orderbook" in url:
                data = {"bids": [["99", "1"]], "asks": [["100", "1"]]}
            elif "current-fund-rate" in url:
                row = {"fundingRate": "-0.001", "nextUpdate": NOW}
                if known:
                    row["fundingRateInterval"] = "4"
                data = [row]
            else:
                data = [{}]
            return {"code": "00000", "data": data}
        raise AssertionError(url)
    return request


ADAPTERS = [
    ("bn", crypto.fetch_binance, 8, "fundingInfo.fundingIntervalHours"),
    ("by", crypto.fetch_bybit, 8, "instruments-info.fundingInterval"),
    ("gt", crypto.fetch_gate, None, "contracts.funding_interval"),
    ("okx", crypto.fetch_okx, 8, "funding-rate.fundingTime_nextFundingTime"),
    ("bg", crypto.fetch_bitget, 8, "current-fund-rate.fundingRateInterval"),
    ("as", crypto.fetch_aster, 1, "fundingInfo.fundingIntervalHours"),
]


@pytest.mark.parametrize("exchange,adapter,default,source", ADAPTERS)
def test_api_interval_or_verified_official_default_has_traceable_evidence(monkeypatch, exchange, adapter, default, source):
    request_counts = []
    for known in (True, False):
        calls = []
        monkeypatch.setattr(crypto, "request_json", fake_market_api(exchange, known, calls))
        quote = adapter(object(), "ABC")
        assert quote.period_hours == (4 if known else default)
        assert quote.funding_period_verified is (known or exchange == "bn")
        expected_source = source if known else BINANCE_DEFAULT_SOURCE if exchange == "bn" else "display_default_or_unknown"
        assert quote.funding_period_source == expected_source
        assert quote.funding_rate == -0.001
        request_counts.append(len(calls))
    # Evidence annotation uses existing responses, not an extra metadata read.
    assert request_counts[0] == request_counts[1]


def test_metadata_request_failure_cannot_turn_binance_default_into_verified_period(monkeypatch):
    base = fake_market_api("bn", False, [])
    def request(client, url, params=None, **kwargs):
        if "fundingInfo" in url:
            raise RuntimeError("metadata timeout")
        return base(client, url, params, **kwargs)
    monkeypatch.setattr(crypto, "request_json", request)
    quote = crypto.fetch_binance(object(), "ABC")
    assert quote.status == "ok"
    assert quote.period_hours == 8
    assert quote.funding_period_verified is False


@pytest.mark.parametrize("rows", [[], [{"symbol": "OTHERUSDT", "fundingIntervalHours": 4}]])
def test_binance_successful_adjustment_list_absence_verifies_official_default(monkeypatch, rows):
    calls = []
    base = fake_market_api("bn", False, calls)
    def request(client, url, params=None, **kwargs):
        if "fundingInfo" in url:
            calls.append(url)
            return rows
        return base(client, url, params, **kwargs)
    monkeypatch.setattr(crypto, "request_json", request)
    quote = crypto.fetch_binance(object(), "ABC")
    assert quote.period_hours == 8
    assert quote.funding_period_verified is True
    assert quote.funding_period_source == BINANCE_DEFAULT_SOURCE
    assert sum("fundingInfo" in url for url in calls) == 1


@pytest.mark.parametrize("rows,display_period", [
    ({"code": -1, "msg": "unavailable"}, 8),
    (None, 8),
    ([None], 8),
    ([{}], 8),
    ([{"symbol": "OTHERUSDT"}], 8),
    ([{"symbol": " OTHERUSDT", "fundingIntervalHours": 4}], 8),
    ([{"symbol": "OTHERUSDT", "fundingIntervalHours": 0}], 8),
    ([{"symbol": "OTHERUSDT", "fundingIntervalHours": True}], 8),
    ([{"symbol": "OTHERUSDT", "fundingIntervalHours": math.nan}], 8),
    ([{"symbol": "OTHERUSDT", "fundingIntervalHours": 4}] * 2, 8),
    ([{"symbol": "ABCUSDT"}], 8),
    ([{"symbol": "ABCUSDT", "fundingIntervalHours": 4}, None], 4),
])
def test_binance_malformed_list_or_target_without_period_remains_unknown(monkeypatch, rows, display_period):
    base = fake_market_api("bn", False, [])
    def request(client, url, params=None, **kwargs):
        if "fundingInfo" in url:
            return rows
        return base(client, url, params, **kwargs)
    monkeypatch.setattr(crypto, "request_json", request)
    quote = crypto.fetch_binance(object(), "ABC")
    assert quote.period_hours == display_period
    assert quote.funding_period_verified is False
    assert quote.funding_period_source == "display_default_or_unknown"


@pytest.mark.parametrize("period,raw", [(8, None), (8, 0), (8, -1), (8, math.nan), (8, math.inf), (-1, -1), (8, 4), (1, True)])
def test_invalid_missing_or_mismatching_api_period_is_not_verified(period, raw):
    assert crypto._funding_period_evidence(period, ("api.interval", raw))["funding_period_verified"] is False


def test_dataclass_legacy_construction_does_not_imply_verified_period():
    quote = crypto.MarketQuote(exchange="bg", symbol="ABC", period_hours=8)
    assert quote.period_hours == 8
    assert quote.funding_period_verified is False
    assert quote.funding_period_source == "unknown"


def test_bitget_contract_interval_is_traceable_when_funding_row_omits_it(monkeypatch):
    monkeypatch.setattr(crypto, "request_json", fake_market_api("bg", False, []))
    monkeypatch.setattr(crypto, "bitget_contract_info", lambda *_args: {"fundInterval": "2"})
    quote = crypto.fetch_bitget(object(), "ABC")
    assert quote.period_hours == 2
    assert quote.funding_period_verified is True
    assert quote.funding_period_source == "contracts.fundInterval"

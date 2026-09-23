import pytest
from app import astro_spread_scanner as s


@pytest.mark.parametrize("exchange,source,estimated", [
    ("okxdex", "okx_v6_quote+exchange_public_depth", False),
    ("pancakeswapv3", "pancakeswap_v3_quote+exchange_public_depth", True),
])
@pytest.mark.parametrize("entry", ["hot", "regular"])
@pytest.mark.parametrize("mode", ["pass", "fail_first", "fail_second", "fail_third", "duplicate", "stale", "stale_future", "missing_execution", "threshold_changed"])
def test_dex_three_fresh_quotes(monkeypatch, exchange, source, estimated, entry, mode):
    monkeypatch.setattr(s, "spread_scan_dex_auto_card_enabled", lambda ex: True)
    monkeypatch.setattr(s, "spread_scan_sf_route_min_open_pct", lambda ex: 1.5)
    clock = [100.0]
    monkeypatch.setattr(s.time, "time", lambda: clock[0])
    monkeypatch.setattr(s.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    recorded = []
    monkeypatch.setattr(s, "_record_revalidation_outcome", lambda *a, **kw: recorded.append(kw))
    pair = {"name": "ABC", "type": "SF", "buyEx": exchange, "sellEx": "gate", "_pipeline": {"test": 1},
            "_hotDirectHit": {"verifiedAtMs": 100000, "report": {"reason": "eligible", "latestOpenSpreadPct": 9}}}
    calls = []
    def fetch(*args, **kwargs):
        calls.append(clock[0])
        index = len(calls)
        timestamp = clock[0] * 1000
        report = {"reason": "eligible", "latestOpenSpreadPct": 2.0,
                  "okxdexExecutablePreflight": {"source": source, "networkFeeEstimated": estimated},
                  "buyQuote": {"timestamp": timestamp}, "sellQuote": {"timestamp": timestamp}}
        if mode == "duplicate" and index == 2: report["buyQuote"]["timestamp"] = 100000
        if mode == "stale": report["buyQuote"]["timestamp"] -= 6000
        if mode == "stale_future": report["sellQuote"]["timestamp"] -= 4000
        if mode == "missing_execution": report.pop("okxdexExecutablePreflight")
        if mode == "threshold_changed" and index == 2:
            monkeypatch.setattr(s, "spread_scan_sf_route_min_open_pct", lambda ex: 2.0)
        if mode == {1: "fail_first", 2: "fail_second", 3: "fail_third"}.get(index):
            return None, {**report, "reason": "below_threshold_or_rule_failed", "latestOpenSpreadPct": 0.5}
        return {**pair, "openPosition": "0.02"}, report
    monkeypatch.setattr(s, "_fetch_direct_route_once", fetch)
    fn = s.revalidate_astro_hot_direct_hit if entry == "hot" else s.revalidate_astro_spread_pair
    result, report = fn(pair, None)
    passed = mode == "pass"
    assert (result is not None) == passed
    assert report["roundsRequired"] == 3
    assert recorded[-1]["passed"] == passed
    if passed:
        assert calls == [100, 101, 102]
        assert report["roundsPassed"] == 3
        assert report["dexQuotesDistinct"]
        assert report["source"] == source and report["networkFeeEstimated"] == estimated
        assert result["_pipeline"] == {"test": 1}
    else:
        count = {"fail_first": 1, "fail_second": 2, "fail_third": 3, "duplicate": 2,
                 "threshold_changed": 2}.get(mode, 1)
        assert len(calls) == count
        assert report["roundsPassed"] == count - 1
        if mode == "duplicate": assert report["reason"] == "okxdex_quote_not_advanced"

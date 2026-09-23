from datetime import date, timedelta

from app.expectation_gap_backtest_core import (
    GapThresholds,
    attach_horizon_returns,
    classify_event,
    cluster_bootstrap_ci,
    market_confirmation,
    next_tradable_entry,
    pre_event_pricing,
    validation_verdict,
)


def _bars(start: date, count: int, start_price: float = 100.0):
    rows = []
    for index in range(count):
        close = start_price + index
        rows.append({
            "trade_date": start + timedelta(days=index),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
            "amount": 1_000_000,
        })
    return rows


def test_a_share_limit_up_defers_entry_and_keeps_next_open() -> None:
    rows = _bars(date(2024, 1, 1), 70)
    rows[1].update({"open": 109.8, "high": 110.0, "low": 109.8, "close": 110.0})
    rows[2].update({"open": 108.0, "high": 109.0, "low": 107.0, "close": 108.5})
    entry = next_tradable_entry(rows, date(2024, 1, 1), "A股", "600001", "测试股份")
    assert entry["available"] is True
    assert entry["entry_date"] == "2024-01-03"
    assert entry["deferred_sessions"] == 1
    assert entry["defer_reasons"] == ["open_at_or_near_limit_up"]


def test_us_entry_is_strictly_after_filing_date() -> None:
    rows = _bars(date(2024, 1, 1), 70)
    entry = next_tradable_entry(rows, date(2024, 1, 10), "美股", "NVDA")
    assert entry["entry_date"] == "2024-01-11"
    result = attach_horizon_returns(rows, entry, rows, cost_bps=10)
    assert result["5"]["available"] is True
    assert result["5"]["market_excess_return_pct"] == -0.1


def test_classification_separates_evidence_from_pricing() -> None:
    thresholds = GapThresholds(30, 0, 3, 15, 25, 0)
    base = {
        "profit_growth_pct": 80,
        "revenue_growth_pct": 20,
        "growth_acceleration_pct": 15,
        "evidence_grade": "A",
        "negative_evidence": False,
        "pre_60d_excess_pct": 8,
        "market_confirmation_excess_pct": 1,
    }
    assert classify_event({**base, "pre_20d_excess_pct": -2}, thresholds)["decision_code"] == "A"
    assert classify_event({**base, "pre_20d_excess_pct": 18}, thresholds)["decision_code"] == "B"
    assert classify_event({**base, "profit_growth_pct": 10, "pre_20d_excess_pct": -2}, thresholds)["decision_code"] == "C"
    assert classify_event({**base, "profit_growth_pct": -30, "pre_20d_excess_pct": -2}, thresholds)["decision_code"] == "D"


def test_pre_event_pricing_excludes_signal_day_reaction() -> None:
    stock = _bars(date(2023, 10, 1), 100)
    benchmark = _bars(date(2023, 10, 1), 100, start_price=200)
    signal = date(2023, 12, 20)
    signal_index = (signal - date(2023, 10, 1)).days
    stock[signal_index]["close"] = 999
    result = pre_event_pricing(stock, benchmark, signal)
    assert result["pre_20d_excess_pct"] is not None
    assert result["pre_20d_excess_pct"] < 10


def test_market_confirmation_uses_only_observation_session_open_to_close() -> None:
    stock = _bars(date(2024, 1, 1), 5)
    benchmark = _bars(date(2024, 1, 1), 5, start_price=200)
    stock[2].update({"open": 100, "close": 105})
    benchmark[2].update({"open": 200, "close": 202})
    result = market_confirmation(stock, benchmark, date(2024, 1, 3))
    assert round(result["confirmation_return_pct"], 6) == 5
    assert round(result["confirmation_benchmark_return_pct"], 6) == 1
    assert round(result["market_confirmation_excess_pct"], 6) == 4


def test_validation_gate_never_claims_success_on_small_sample() -> None:
    summary = {
        "by_decision": {
            "A": {
                "sample_count": 5,
                "median_excess_return_pct": 10,
                "bootstrap_95ci_low_pct": 1,
            }
        },
        "pricing_gain_a_vs_b_median_pct": 5,
        "by_year_A": {
            "2024": {"sample_count": 3, "median_excess_return_pct": 10},
            "2025": {"sample_count": 2, "median_excess_return_pct": 5},
        },
    }
    verdict = validation_verdict(summary, minimum_samples=30)
    assert verdict["state"] == "not_validated"
    assert verdict["checks"]["sample_sufficient"] is False


def test_cluster_bootstrap_matches_median_and_resists_one_extreme_outlier() -> None:
    events = [
        {
            "symbol": f"S{index}",
            "horizons": {"20": {"peer_excess_return_pct": 10_000 if index == 9 else 1}},
        }
        for index in range(10)
    ]
    lower, upper = cluster_bootstrap_ci(events, iterations=500)
    assert lower == 1
    assert upper == 1

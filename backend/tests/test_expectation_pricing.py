from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.expectation_pricing import (
    _consistency_result,
    _group_statistics,
    build_company_expectation_gap_gate,
    build_eastmoney_expectation_payload,
    build_expectation_gap_calibration,
    build_expectation_payload,
    build_sec_forward_valuation_backtest,
    yahoo_symbol_for_company,
)
from app.models import IndustryChainCompany


def raw(value: float) -> dict[str, float]:
    return {"raw": value}


def annual_fact(start: str, end: str, filed: str, value: float) -> dict[str, object]:
    return {
        "form": "10-K",
        "start": start,
        "end": end,
        "filed": filed,
        "val": value,
    }


def test_build_expectation_payload_keeps_consensus_separate_from_reverse_check() -> None:
    payload = {
        "earningsTrend": {
            "trend": [
                {
                    "period": "0y",
                    "endDate": "2026-08-31",
                    "revenueEstimate": {"avg": raw(130e9), "low": raw(120e9), "high": raw(140e9), "numberOfAnalysts": raw(36), "growth": raw(0.4)},
                    "earningsEstimate": {"avg": raw(73), "low": raw(67), "high": raw(80), "numberOfAnalysts": raw(34), "growth": raw(0.5)},
                    "epsTrend": {"current": raw(73), "30daysAgo": raw(60), "90daysAgo": raw(58)},
                    "epsRevisions": {"upLast30days": raw(29), "downLast30days": raw(0)},
                },
                {
                    "period": "+1y",
                    "endDate": "2027-08-31",
                    "revenueEstimate": {"avg": raw(240e9), "low": raw(180e9), "high": raw(300e9), "numberOfAnalysts": raw(40), "growth": raw(0.85)},
                    "earningsEstimate": {"avg": raw(160), "low": raw(100), "high": raw(220), "numberOfAnalysts": raw(37), "growth": raw(1.0)},
                    "epsTrend": {"current": raw(160), "30daysAgo": raw(120), "90daysAgo": raw(100)},
                    "epsRevisions": {"upLast30days": raw(30), "downLast30days": raw(0)},
                },
            ]
        },
        "price": {
            "regularMarketPrice": raw(800),
            "regularMarketTime": int(datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp()),
            "marketCap": raw(960e9),
            "currency": "USD",
            "quoteSourceName": "Nasdaq Real Time Price",
        },
        "defaultKeyStatistics": {"sharesOutstanding": raw(1.2e9)},
        "financialData": {"targetMeanPrice": raw(1200), "numberOfAnalystOpinions": raw(42)},
    }
    backtest = {
        "forward_ps": {"p25": 1.2, "median": 1.9, "p75": 3.0},
        "profitable_forward_pe": {"p25": 4.0, "median": 9.0, "p75": 14.0},
    }

    consensus, valuation = build_expectation_payload("MU", payload, backtest)

    assert consensus["next_year"]["revenue"]["average"] == 240e9
    assert consensus["next_year"]["revenue"]["analyst_count"] == 40
    assert valuation["forward_ps"] == 4.0
    assert valuation["forward_pe"] == 5.0
    assert valuation["pricing_state"] == "高增长已定价，持续性被折价"
    assert valuation["reverse_check"]["revenue_required_at_historical_median_ps"] != consensus["next_year"]["revenue"]["average"]
    assert "不是新的券商一致预期" in valuation["reverse_check"]["interpretation"]
    assert valuation["market_cap_bridge"]["market_cap_from_profit"] == 960e9
    assert valuation["market_cap_bridge"]["market_cap_from_revenue"] == 960e9
    drivers = {item["key"]: item for item in valuation["support_drivers"]}
    assert drivers["revenue_scale"]["status"] == "支撑"
    assert drivers["profit_quality"]["status"] == "重点验证"
    assert drivers["consensus_revision"]["status"] == "支撑"
    assert drivers["valuation_duration"]["status"] == "高要求"


def test_sec_backtest_uses_next_year_actual_after_filing() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            annual_fact("2021-09-01", "2022-08-31", "2022-10-07", 100e9),
                            annual_fact("2022-09-01", "2023-08-31", "2023-10-06", 120e9),
                            annual_fact("2023-09-01", "2024-08-31", "2024-10-04", 150e9),
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            annual_fact("2021-09-01", "2022-08-31", "2022-10-07", 10e9),
                            annual_fact("2022-09-01", "2023-08-31", "2023-10-06", 15e9),
                            annual_fact("2023-09-01", "2024-08-31", "2024-10-04", 20e9),
                        ]
                    }
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "units": {
                        "shares": [
                            annual_fact("2021-09-01", "2022-08-31", "2022-10-07", 1e9),
                            annual_fact("2022-09-01", "2023-08-31", "2023-10-06", 1e9),
                            annual_fact("2023-09-01", "2024-08-31", "2024-10-04", 1e9),
                        ]
                    }
                },
            }
        }
    }
    timestamps = [
        int(datetime(2022, 10, 10, tzinfo=timezone.utc).timestamp()),
        int(datetime(2023, 10, 9, tzinfo=timezone.utc).timestamp()),
        int(datetime(2024, 10, 7, tzinfo=timezone.utc).timestamp()),
    ]
    chart = {"timestamp": timestamps, "indicators": {"quote": [{"close": [60, 90, 110]}]}}

    result = build_sec_forward_valuation_backtest(facts, chart)

    assert len(result["rows"]) == 2
    assert result["rows"][0]["fiscal_year"] == 2022
    assert result["rows"][0]["next_year_revenue"] == 120e9
    assert result["rows"][0]["forward_ps"] == 0.5
    assert "不冒充当时一致预期" in result["method"]


def test_a_share_consensus_uses_forecast_revenue_profit_and_market_cap_in_same_unit() -> None:
    company = IndustryChainCompany(
        chain_id=1,
        name="示例公司",
        code="300001",
        full_code="SZ300001",
        market="A股",
    )
    payload = {
        "yctj_list": [
            {
                "YEAR": 2025,
                "YEAR_MARK": "A",
                "TOTAL_OPERATE_INCOME": 50e9,
                "PARENT_NETPROFIT": 5e9,
                "EPS": 5,
            },
            {
                "YEAR": 2026,
                "YEAR_MARK": "E",
                "TOTAL_OPERATE_INCOME": 75e9,
                "TOTAL_OPERATE_INCOME_COUNT": 20,
                "PARENT_NETPROFIT": 10e9,
                "PARENT_NETPROFIT_COUNT": 18,
                "EPS": 10,
                "EPS_COUNT": 18,
            },
            {
                "YEAR": 2027,
                "YEAR_MARK": "E",
                "TOTAL_OPERATE_INCOME": 100e9,
                "TOTAL_OPERATE_INCOME_COUNT": 22,
                "PARENT_NETPROFIT": 20e9,
                "PARENT_NETPROFIT_COUNT": 21,
                "EPS": 20,
                "EPS_COUNT": 21,
            },
        ]
    }
    market_anchor = {
        "market_cap": 400e9,
        "current_price": 400,
        "shares": 1e9,
        "as_of": "2026-07-17",
        "source": "test",
    }

    consensus, valuation, backtest = build_eastmoney_expectation_payload(company, payload, market_anchor)

    assert consensus["current_year"]["revenue"]["average"] == 75e9
    assert consensus["next_year"]["revenue"]["average"] == 100e9
    assert consensus["next_year"]["revenue"]["analyst_count"] == 22
    assert valuation["forward_ps"] == 4.0
    assert valuation["forward_pe"] == 20.0
    assert valuation["consensus_net_margin_pct"] == 20.0
    assert valuation["market_cap_bridge"]["market_cap"] == 400e9
    assert len(valuation["support_drivers"]) == 4
    assert backtest["rows"] == []
    assert "逐日积累" in valuation["reverse_check"]["interpretation"]


def test_global_company_symbols_cover_listed_markets_but_not_private_firms() -> None:
    cases = [
        ("台湾", "TWSE", "2330", "2330.TW"),
        ("台湾", "TPEX", "3163", "3163.TWO"),
        ("日本", "TSE", "6857", "6857.T"),
        ("韩国", "KOSDAQ", "138080", "138080.KQ"),
        ("其他", "HKEX", "700", "0700.HK"),
        ("其他", "LSE", "IQE", "IQE.L"),
        ("其他", "PRIVATE", "AYAR", None),
    ]
    for market, exchange, code, expected in cases:
        company = IndustryChainCompany(chain_id=1, name=code, market=market, exchange=exchange, code=code)
        assert yahoo_symbol_for_company(company) == expected


def test_industry_consistency_separates_growth_breadth_from_revision_breadth() -> None:
    rows = [
        {"coverage_status": "已覆盖", "next_revenue_growth_pct": 20, "next_eps_growth_pct": 30, "eps_revision_30d_pct": 5, "forward_ps": 4, "forward_pe": 20},
        {"coverage_status": "已覆盖", "next_revenue_growth_pct": 15, "next_eps_growth_pct": 22, "eps_revision_30d_pct": 2, "forward_ps": 5, "forward_pe": 24},
        {"coverage_status": "已覆盖", "next_revenue_growth_pct": 8, "next_eps_growth_pct": 12, "eps_revision_30d_pct": -3, "forward_ps": 3, "forward_pe": 18},
        {"coverage_status": "无分析师覆盖", "next_revenue_growth_pct": None, "next_eps_growth_pct": None, "eps_revision_30d_pct": None, "forward_ps": None, "forward_pe": None},
    ]
    all_group = _group_statistics(rows, "all", "全部")
    global_group = _group_statistics(rows[:2], "global", "海外")
    domestic_group = _group_statistics(rows[1:3], "domestic", "国内")
    result = _consistency_result(all_group, global_group, domestic_group)

    assert all_group["positive_growth_ratio"] == 100.0
    assert result["growth_status"] == "高度同向增长"
    assert result["revision_status"] == "盈利预期同步上修"
    assert result["cross_group_status"] == "海外龙头与国内映射同向"


def test_a_share_without_forecast_is_kept_as_uncovered_not_failed() -> None:
    company = IndustryChainCompany(chain_id=1, name="少覆盖公司", code="300002", full_code="SZ300002", market="A股")
    consensus, valuation, _ = build_eastmoney_expectation_payload(
        company,
        {"yctj_list": [{"YEAR": 2025, "YEAR_MARK": "A", "TOTAL_OPERATE_INCOME": 1e9, "EPS": 1}]},
        {"market_cap": 10e9, "current_price": 10, "shares": 1e9, "as_of": "2026-07-17", "source": "test"},
    )

    assert consensus["next_year"] is None
    assert valuation["market_cap"] == 10e9
    assert valuation["pricing_state"] == "无分析师覆盖"


def expectation_gap_calibration_fixture() -> dict[str, object]:
    return build_expectation_gap_calibration(
        {
            "status": "completed",
            "method_version": "expectation-gap-2.1-five-year-test",
            "period": {"start": "2021-07-19", "end": "2026-07-17", "years": 4.99},
            "overall_verdict": {"state": "not_validated", "interpretation": "跨市场不自动使用。"},
            "markets": {
                "A股": {
                    "thresholds": {
                        "evidence_growth_pct": 50.0,
                        "evidence_acceleration_pct": 20.0,
                        "unpriced_20d_excess_pct": 0.0,
                        "priced_20d_excess_pct": 10.0,
                        "priced_60d_excess_pct": 25.0,
                        "confirmation_excess_pct": 1.0,
                    },
                    "validation": {"horizon": 20, "by_decision": {"A": {"sample_count": 89}}},
                    "verdict": {"state": "validated", "interpretation": "A股通过。"},
                },
                "美股": {
                    "thresholds": {},
                    "validation": {"horizon": 20, "by_decision": {"A": {"sample_count": 29}}},
                    "verdict": {"state": "not_validated", "interpretation": "美股未通过。"},
                },
            },
        }
    )


def expectation_gap_price_fixture(priced: bool = False) -> tuple[list[dict[str, object]], list[dict[str, object]], date]:
    start = date(2026, 1, 1)
    rows: list[dict[str, object]] = []
    benchmark: list[dict[str, object]] = []
    signal_index = 79
    for index in range(90):
        trade_date = start + timedelta(days=index)
        close = 100.0
        if priced and 59 <= index <= signal_index:
            close = 100.0 + (index - 59)
        open_value = close
        if index == signal_index + 1:
            open_value = 120.0 if priced else 100.0
            close = open_value * 1.03
        elif index == signal_index + 2:
            open_value = rows[-1]["close"]
            close = float(open_value)
        rows.append({"trade_date": trade_date, "open": open_value, "high": max(open_value, close), "low": min(open_value, close), "close": close, "amount": 1_000_000})
        benchmark.append({"trade_date": trade_date, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "amount": 1_000_000})
    return rows, benchmark, start + timedelta(days=signal_index)


def test_five_year_gate_requires_hard_evidence_unpriced_and_market_confirmation() -> None:
    bars, benchmark, signal_date = expectation_gap_price_fixture()
    company = IndustryChainCompany(
        chain_id=1,
        name="校准公司",
        code="300001",
        full_code="SZ300001",
        market="A股",
        verification_status="已确认",
        expectation_gap_status="正向预期差",
        expectation_as_of=signal_date,
        expectation_evidence_growth_pct=60,
        expectation_evidence_acceleration_pct=25,
    )
    evidence = [{"source_tier": "公司披露", "evidence_state": "有效", "title": "业绩公告"}]

    result = build_company_expectation_gap_gate(company, evidence, bars, benchmark, expectation_gap_calibration_fixture())

    assert result["decision_code"] == "A"
    assert result["evidence"]["state"] == "通过"
    assert result["pricing"]["state"] == "披露前未定价"
    assert result["confirmation"]["state"] == "通过"
    assert result["does_not_overwrite_formal_conclusion"] is True


def test_five_year_gate_downgrades_priced_or_incomplete_evidence_and_never_applies_us_thresholds() -> None:
    bars, benchmark, signal_date = expectation_gap_price_fixture(priced=True)
    company = IndustryChainCompany(
        chain_id=1,
        name="已提前交易公司",
        code="300002",
        full_code="SZ300002",
        market="A股",
        verification_status="已确认",
        expectation_gap_status="正向预期差",
        expectation_as_of=signal_date,
        expectation_evidence_growth_pct=60,
        expectation_evidence_acceleration_pct=25,
    )
    evidence = [{"source_tier": "官方硬证据", "evidence_state": "有效", "title": "正式公告"}]
    calibration = expectation_gap_calibration_fixture()

    priced_result = build_company_expectation_gap_gate(company, evidence, bars, benchmark, calibration)
    company.expectation_evidence_growth_pct = None
    incomplete_result = build_company_expectation_gap_gate(company, evidence, bars, benchmark, calibration)
    company.market = "美股"
    us_result = build_company_expectation_gap_gate(company, evidence, bars, benchmark, calibration)

    assert priced_result["decision_code"] == "B"
    assert priced_result["pricing"]["state"] == "已提前定价"
    assert incomplete_result["decision_code"] == "C"
    assert us_result["decision_code"] == "观察"
    assert us_result["label"] == "方法未验证｜仅观察"

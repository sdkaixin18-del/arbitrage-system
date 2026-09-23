from __future__ import annotations

import bisect
import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import get_data_root, get_db
from app.expectation_gap_backtest_core import (
    GapThresholds,
    classify_event,
    market_confirmation,
    next_tradable_entry,
    pre_event_pricing,
)
from app.models import (
    CompanyExpectationSnapshot,
    FactorMarketCapSnapshot,
    IndustryChain,
    IndustryChainCompany,
    IndustryChainEvidence,
    IndustryExpectationSnapshot,
    StockDailyBar,
    now_utc,
)


router = APIRouter(prefix="/api/investment/industry-trends", tags=["expectation-pricing"])

YAHOO_PROVIDER = "Yahoo Finance + SEC"
GLOBAL_YAHOO_PROVIDER = "Yahoo Finance 全球市场"
EASTMONEY_PROVIDER = "东方财富盈利预测"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 stock-review-mac/1.0"}
SEC_HEADERS = {
    "User-Agent": "stock-review-mac/1.0 local-research@example.com",
    "Accept": "application/json",
}
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
YAHOO_ANALYSIS_URL = "https://finance.yahoo.com/quote/{symbol}/analysis/"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
EASTMONEY_FORECAST_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax"
KNOWN_SEC_CIKS = {"MU": "0000723125"}
EXPECTATION_GAP_SUMMARY_RELATIVE_PATH = "research/expectation_gap_backtest/summary.json"
EXPECTATION_GAP_BENCHMARK_RELATIVE_PATH = "research/expectation_gap_backtest/raw/prices/benchmarks/CSI300.json"


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _raw(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _percent_change(current: float | None, previous: float | None) -> float | None:
    ratio = _safe_ratio(current, previous)
    return _round((ratio - 1) * 100, 2) if ratio is not None else None


def _percentile(values: list[float], fraction: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    position = (len(clean) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    weight = position - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return {
        "count": len(clean),
        "min": _round(min(clean), 2) if clean else None,
        "p25": _round(_percentile(clean, 0.25), 2),
        "median": _round(statistics.median(clean), 2) if clean else None,
        "p75": _round(_percentile(clean, 0.75), 2),
        "max": _round(max(clean), 2) if clean else None,
    }


def _estimate_block(trend: dict[str, Any] | None) -> dict[str, Any] | None:
    if not trend:
        return None
    revenue = trend.get("revenueEstimate") or {}
    earnings = trend.get("earningsEstimate") or {}
    eps_trend = trend.get("epsTrend") or {}
    revisions = trend.get("epsRevisions") or {}
    revenue_average = _raw(revenue.get("avg"))
    revenue_year_ago = _raw(revenue.get("yearAgoRevenue"))
    revenue_growth = _raw(revenue.get("growth"))
    eps_average = _raw(earnings.get("avg"))
    eps_year_ago = _raw(earnings.get("yearAgoEps"))
    eps_growth = _raw(earnings.get("growth"))
    return {
        "period": trend.get("period"),
        "end_date": trend.get("endDate"),
        "revenue": {
            "average": revenue_average,
            "low": _raw(revenue.get("low")),
            "high": _raw(revenue.get("high")),
            "analyst_count": int(_raw(revenue.get("numberOfAnalysts")) or 0),
            "year_ago": revenue_year_ago,
            "growth_pct": _round(revenue_growth * 100, 2) if revenue_growth is not None else _percent_change(revenue_average, revenue_year_ago),
        },
        "eps": {
            "average": eps_average,
            "low": _raw(earnings.get("low")),
            "high": _raw(earnings.get("high")),
            "analyst_count": int(_raw(earnings.get("numberOfAnalysts")) or 0),
            "year_ago": eps_year_ago,
            "growth_pct": _round(eps_growth * 100, 2) if eps_growth is not None else _percent_change(eps_average, eps_year_ago),
        },
        "eps_revision": {
            "current": _raw(eps_trend.get("current")),
            "7_days_ago": _raw(eps_trend.get("7daysAgo")),
            "30_days_ago": _raw(eps_trend.get("30daysAgo")),
            "60_days_ago": _raw(eps_trend.get("60daysAgo")),
            "90_days_ago": _raw(eps_trend.get("90daysAgo")),
            "change_30d_pct": _percent_change(_raw(eps_trend.get("current")), _raw(eps_trend.get("30daysAgo"))),
            "change_90d_pct": _percent_change(_raw(eps_trend.get("current")), _raw(eps_trend.get("90daysAgo"))),
            "up_30d": int(_raw(revisions.get("upLast30days")) or 0),
            "down_30d": int(_raw(revisions.get("downLast30days")) or 0),
        },
    }


def _market_cap_bridge(
    consensus: dict[str, Any],
    valuation: dict[str, Any],
) -> dict[str, Any]:
    """Expose the assumptions that have to hold up the current market cap.

    This is deliberately an identity bridge, not a target-price model.  It lets
    the UI show which revenue, margin and valuation assumptions are carrying the
    quoted market cap, so later evidence can strengthen or invalidate each leg.
    """

    next_year = consensus.get("next_year") or {}
    revenue = _raw((next_year.get("revenue") or {}).get("average"))
    eps = _raw((next_year.get("eps") or {}).get("average"))
    net_margin_pct = _raw(valuation.get("consensus_net_margin_pct"))
    net_income = _raw(valuation.get("consensus_net_income"))
    market_cap = _raw(valuation.get("market_cap"))
    forward_pe = _safe_ratio(market_cap, net_income) or _raw(valuation.get("forward_pe"))
    forward_ps = _safe_ratio(market_cap, revenue) or _raw(valuation.get("forward_ps"))
    return {
        "forecast_period": next_year.get("end_date"),
        "currency": consensus.get("currency"),
        "revenue": _round(revenue, 2),
        "net_margin_pct": _round(net_margin_pct, 2),
        "net_income": _round(net_income, 2),
        "eps": _round(eps, 4),
        "forward_pe": _round(forward_pe, 2),
        "forward_ps": _round(forward_ps, 2),
        "market_cap": _round(market_cap, 2),
        "market_cap_from_profit": _round(net_income * forward_pe, 2) if net_income is not None and forward_pe is not None else None,
        "market_cap_from_revenue": _round(revenue * forward_ps, 2) if revenue is not None and forward_ps is not None else None,
        "formula": "营收 × 净利率 × 市场愿意给的PE = 市值；同时用营收 × PS交叉校验。",
        "note": "这是当前市值的因子拆解，不是目标市值。任何一项转弱，都要重新计算可支撑市值。",
    }


def _financial_support_drivers(
    consensus: dict[str, Any],
    valuation: dict[str, Any],
    backtest: dict[str, Any],
) -> list[dict[str, Any]]:
    next_year = consensus.get("next_year") or {}
    revenue = next_year.get("revenue") or {}
    eps = next_year.get("eps") or {}
    revisions = next_year.get("eps_revision") or {}
    currency = consensus.get("currency")
    revenue_growth = _raw(revenue.get("growth_pct"))
    margin_pct = _raw(valuation.get("consensus_net_margin_pct"))
    revision_30d = _raw(revisions.get("change_30d_pct"))
    revision_90d = _raw(revisions.get("change_90d_pct"))
    revision_up = int(_raw(revisions.get("up_30d")) or 0)
    revision_down = int(_raw(revisions.get("down_30d")) or 0)
    forward_ps = _raw(valuation.get("forward_ps"))
    forward_pe = _raw(valuation.get("forward_pe"))
    ps_median = _raw((backtest.get("forward_ps") or {}).get("median"))
    pe_median = _raw((backtest.get("profitable_forward_pe") or {}).get("median"))

    if revenue_growth is None:
        revenue_status = "数据不足"
    elif revenue_growth > 0:
        revenue_status = "支撑"
    else:
        revenue_status = "转弱"

    if margin_pct is None:
        margin_status = "数据不足"
    elif margin_pct <= 0:
        margin_status = "转弱"
    elif margin_pct >= 50:
        margin_status = "重点验证"
    else:
        margin_status = "支撑"

    if revision_30d is None:
        revision_status = "积累中"
    elif revision_30d > 0 and revision_up >= revision_down:
        revision_status = "支撑"
    elif revision_30d < 0:
        revision_status = "转弱"
    else:
        revision_status = "观察"

    pricing_state = str(valuation.get("pricing_state") or "")
    if "高增长" in pricing_state or "高预期" in pricing_state:
        valuation_status = "高要求"
    elif "兑现要求较低" in pricing_state:
        valuation_status = "有缓冲"
    elif "分化" in pricing_state:
        valuation_status = "分歧"
    else:
        valuation_status = "待对照"

    return [
        {
            "key": "revenue_scale",
            "group": "收入",
            "name": "远期营收规模",
            "formula_role": "决定利润池上限",
            "status": revenue_status,
            "value": _raw(revenue.get("average")),
            "value_kind": "currency",
            "currency": currency,
            "change_pct": revenue_growth,
            "coverage_count": int(_raw(revenue.get("analyst_count")) or 0),
            "support_condition": "季度收入、公司指引和机构下一财年营收预测保持兑现或上修。",
            "invalidation": "公司指引或一致预期连续下修，且订单、价格不能解释缺口。",
            "source": "机构一致预期；仍需公司财报与订单互证",
        },
        {
            "key": "profit_quality",
            "group": "利润",
            "name": "利润率与EPS持续性",
            "formula_role": "把收入转换成净利润",
            "status": margin_status,
            "value": _raw(eps.get("average")),
            "value_kind": "eps",
            "currency": currency,
            "change_pct": margin_pct,
            "coverage_count": int(_raw(eps.get("analyst_count")) or 0),
            "support_condition": "产品价格、结构和成本使毛利率、净利率及EPS能够跨季度维持。",
            "invalidation": "ASP回落、成本或资本开支上升导致利润率明显低于预测。",
            "source": "机构EPS预测；需公司毛利率和现金流互证",
        },
        {
            "key": "consensus_revision",
            "group": "预期",
            "name": "盈利预期修订方向",
            "formula_role": "决定新增买盘是否继续上调模型",
            "status": revision_status,
            "value": revision_30d,
            "value_kind": "percent",
            "currency": None,
            "change_pct": revision_90d,
            "coverage_count": revision_up + revision_down,
            "support_condition": "30日和90日EPS预测继续上修，上调机构数明显多于下调机构。",
            "invalidation": "连续出现净下调，且股价仍按旧高预期交易。",
            "source": "机构一致预期修订快照",
        },
        {
            "key": "valuation_duration",
            "group": "估值",
            "name": "市场愿意给的倍数",
            "formula_role": "决定同样利润能承载多少市值",
            "status": valuation_status,
            "value": forward_pe,
            "value_kind": "multiple",
            "currency": None,
            "change_pct": forward_ps,
            "coverage_count": int((backtest.get("forward_ps") or {}).get("count") or 0),
            "support_condition": "利润持续时间、合同可见性和自由现金流足以避免估值倍数压缩。",
            "invalidation": "利润被确认是短周期峰值，远期PE/PS回到更低区间。",
            "source": f"当前远期PS/PE；历史中位PS {ps_median or '-'}、PE {pe_median or '-'}",
        },
    ]


@lru_cache(maxsize=512)
def _fetch_yahoo_consensus_cached(symbol: str, observed_on: str) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    with httpx.Client(headers=YAHOO_HEADERS, timeout=20, follow_redirects=True) as client:
        client.get("https://fc.yahoo.com")
        crumb_response = client.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        crumb_response.raise_for_status()
        crumb = crumb_response.text.strip()
        response = client.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
            params={
                "modules": "earningsTrend,financialData,defaultKeyStatistics,price",
                "crumb": crumb,
            },
        )
        response.raise_for_status()
    result = ((response.json().get("quoteSummary") or {}).get("result") or [])
    if not result:
        raise ValueError("一致预期数据源没有返回有效结果")
    return result[0]


def fetch_yahoo_consensus(symbol: str) -> dict[str, Any]:
    return _fetch_yahoo_consensus_cached(symbol.strip().upper(), date.today().isoformat())


@lru_cache(maxsize=256)
def _fetch_eastmoney_consensus_cached(full_code: str, observed_on: str) -> dict[str, Any]:
    code = full_code.strip().upper()
    response = httpx.get(
        EASTMONEY_FORECAST_URL,
        params={"code": code},
        headers={
            "User-Agent": "Mozilla/5.0 stock-review-mac/1.0",
            "Referer": f"https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/Index?code={code}&type=web",
        },
        timeout=20,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("yctj_list"):
        raise ValueError("公开盈利预测没有返回有效结果")
    return payload


def fetch_eastmoney_consensus(full_code: str) -> dict[str, Any]:
    return _fetch_eastmoney_consensus_cached(full_code.strip().upper(), date.today().isoformat())


@lru_cache(maxsize=1)
def _sec_ticker_map() -> dict[str, str]:
    response = httpx.get(SEC_TICKERS_URL, headers=SEC_HEADERS, timeout=30, follow_redirects=True)
    response.raise_for_status()
    mapping: dict[str, str] = {}
    for row in response.json().values():
        ticker = str(row.get("ticker") or "").upper()
        cik = str(row.get("cik_str") or "")
        if ticker and cik:
            mapping[ticker] = cik.zfill(10)
    return mapping


@lru_cache(maxsize=256)
def fetch_sec_company_facts(symbol: str) -> tuple[str, dict[str, Any]]:
    clean_symbol = symbol.strip().upper()
    cik = KNOWN_SEC_CIKS.get(clean_symbol)
    if not cik:
        cik = _sec_ticker_map().get(clean_symbol)
    if not cik:
        raise ValueError("SEC公司代码表没有找到该股票")
    response = httpx.get(SEC_COMPANY_FACTS.format(cik=cik), headers=SEC_HEADERS, timeout=45, follow_redirects=True)
    response.raise_for_status()
    return cik, response.json()


@lru_cache(maxsize=256)
def fetch_yahoo_daily_chart(symbol: str, start_year: int = 2009) -> dict[str, Any]:
    period1 = int(datetime(start_year, 1, 1, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.now(timezone.utc).timestamp())
    response = httpx.get(
        YAHOO_CHART_URL.format(symbol=symbol.strip().upper()),
        params={"period1": period1, "period2": period2, "interval": "1d", "events": "div,splits"},
        headers=YAHOO_HEADERS,
        timeout=45,
        follow_redirects=True,
    )
    response.raise_for_status()
    result = ((response.json().get("chart") or {}).get("result") or [])
    if not result:
        raise ValueError("历史价格数据源没有返回有效结果")
    return result[0]


def yahoo_symbol_for_company(company: IndustryChainCompany) -> str | None:
    """Return the public Yahoo symbol without pretending private firms are listed."""

    code = (company.code or "").strip().upper()
    exchange = (company.exchange or "").strip().upper()
    market = (company.market or "").strip()
    if not code or exchange == "PRIVATE":
        return None
    if market == "美股":
        return code
    if market == "台湾":
        return f"{code}.TWO" if exchange == "TPEX" else f"{code}.TW"
    if market == "日本":
        return f"{code}.T"
    if market == "韩国":
        return f"{code}.KQ" if exchange == "KOSDAQ" else f"{code}.KS"
    if exchange == "HKEX" or market in {"港股", "香港"}:
        return f"{code.zfill(4)}.HK"
    if exchange == "LSE":
        return f"{code}.L"
    if exchange in {"STO", "STOCKHOLM"}:
        return f"{code}.ST"
    return None


def _empty_backtest(method: str) -> dict[str, Any]:
    return {
        "method": method,
        "sample_start": None,
        "sample_end": None,
        "forward_ps": _distribution([]),
        "profitable_forward_pe": _distribution([]),
        "rows": [],
    }


def _annual_facts(
    company_facts: dict[str, Any],
    concepts: list[str],
    unit: str,
) -> dict[date, dict[str, Any]]:
    facts = ((company_facts.get("facts") or {}).get("us-gaap") or {})
    result: dict[date, dict[str, Any]] = {}
    for priority, concept in enumerate(concepts):
        rows = (((facts.get(concept) or {}).get("units") or {}).get(unit) or [])
        for item in rows:
            if item.get("form") != "10-K" or not item.get("start") or not item.get("end") or not item.get("filed"):
                continue
            try:
                start = date.fromisoformat(item["start"])
                end = date.fromisoformat(item["end"])
                filed = date.fromisoformat(item["filed"])
            except ValueError:
                continue
            if (end - start).days < 300 or (filed - end).days > 180:
                continue
            candidate = {**item, "priority": priority, "concept": concept, "filed_date": filed}
            existing = result.get(end)
            if existing is None or (filed, priority) < (existing["filed_date"], existing["priority"]):
                result[end] = candidate
    return result


def _daily_prices(chart: dict[str, Any]) -> list[tuple[date, float]]:
    timestamps = chart.get("timestamp") or []
    quotes = (((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    result: list[tuple[date, float]] = []
    for timestamp, close in zip(timestamps, quotes):
        value = _raw(close)
        if value is None or value <= 0:
            continue
        result.append((datetime.fromtimestamp(int(timestamp), timezone.utc).date(), value))
    return result


def build_sec_forward_valuation_backtest(
    company_facts: dict[str, Any],
    chart: dict[str, Any],
) -> dict[str, Any]:
    revenues = _annual_facts(
        company_facts,
        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
        "USD",
    )
    net_income = _annual_facts(company_facts, ["NetIncomeLoss"], "USD")
    diluted_shares = _annual_facts(company_facts, ["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares")
    periods: dict[int, dict[str, Any]] = {}
    for end, revenue in sorted(revenues.items()):
        income = net_income.get(end)
        shares = diluted_shares.get(end)
        if not income or not shares:
            continue
        periods[end.year] = {
            "fiscal_year": end.year,
            "fiscal_end": end,
            "filed": revenue["filed_date"],
            "revenue": float(revenue["val"]),
            "net_income": float(income["val"]),
            "shares": float(shares["val"]),
        }

    prices = _daily_prices(chart)
    price_dates = [item[0] for item in prices]
    rows: list[dict[str, Any]] = []
    for fiscal_year, period in sorted(periods.items()):
        next_period = periods.get(fiscal_year + 1)
        if not next_period or fiscal_year < 2012:
            continue
        index = bisect.bisect_left(price_dates, period["filed"] + timedelta(days=1))
        if index >= len(prices):
            continue
        price_date, price = prices[index]
        market_cap = price * period["shares"]
        return_index = index + 252
        next_year_return = None
        if return_index < len(prices):
            next_year_return = (prices[return_index][1] / price - 1) * 100
        forward_ps = market_cap / next_period["revenue"] if next_period["revenue"] > 0 else None
        forward_pe = market_cap / next_period["net_income"] if next_period["net_income"] > 0 else None
        rows.append(
            {
                "fiscal_year": fiscal_year,
                "filing_date": period["filed"],
                "price_date": price_date,
                "price": _round(price, 2),
                "market_cap": _round(market_cap, 2),
                "next_year_revenue": _round(next_period["revenue"], 2),
                "next_year_net_income": _round(next_period["net_income"], 2),
                "next_year_revenue_growth_pct": _percent_change(next_period["revenue"], period["revenue"]),
                "forward_ps": _round(forward_ps, 2),
                "forward_pe": _round(forward_pe, 2),
                "next_252d_return_pct": _round(next_year_return, 2),
            }
        )
    ps_values = [float(row["forward_ps"]) for row in rows if row.get("forward_ps") is not None]
    pe_values = [float(row["forward_pe"]) for row in rows if row.get("forward_pe") is not None]
    return {
        "method": "每年10-K披露后的下一交易日市值，除以下一财年实际营收或净利润；只用于检验历史定价区间，不冒充当时一致预期。",
        "sample_start": rows[0]["fiscal_year"] if rows else None,
        "sample_end": rows[-1]["fiscal_year"] if rows else None,
        "forward_ps": _distribution(ps_values),
        "profitable_forward_pe": _distribution(pe_values),
        "rows": rows,
    }


def build_expectation_payload(
    symbol: str,
    yahoo_payload: dict[str, Any],
    backtest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    trends = {item.get("period"): item for item in ((yahoo_payload.get("earningsTrend") or {}).get("trend") or [])}
    current_year = _estimate_block(trends.get("0y"))
    next_year = _estimate_block(trends.get("+1y"))
    price = yahoo_payload.get("price") or {}
    stats = yahoo_payload.get("defaultKeyStatistics") or {}
    financial = yahoo_payload.get("financialData") or {}
    current_price = _raw(price.get("regularMarketPrice"))
    market_cap = _raw(price.get("marketCap"))
    shares = _raw(stats.get("sharesOutstanding")) or _raw(stats.get("impliedSharesOutstanding"))
    next_revenue = ((next_year or {}).get("revenue") or {}).get("average")
    next_eps = ((next_year or {}).get("eps") or {}).get("average")
    implied_net_income = next_eps * shares if next_eps is not None and shares is not None else None
    forward_ps = _safe_ratio(market_cap, next_revenue)
    forward_pe = _safe_ratio(current_price, next_eps) or _safe_ratio(market_cap, implied_net_income)
    consensus_net_margin = _safe_ratio(implied_net_income, next_revenue)
    ps_history = backtest.get("forward_ps") or {}
    pe_history = backtest.get("profitable_forward_pe") or {}
    ps_p75 = _raw(ps_history.get("p75"))
    ps_median = _raw(ps_history.get("median"))
    pe_p75 = _raw(pe_history.get("p75"))
    pe_median = _raw(pe_history.get("median"))

    if forward_ps is None or forward_pe is None:
        pricing_state = "数据不足"
        conclusion = "一致预期或当前市值不完整，暂不能反推市场已经交易到哪一步。"
    elif ps_median is None and pe_median is None:
        pricing_state = "一致预期已取得，历史区间待补"
        conclusion = "当前已能看到下一财年一致预期和对应估值，但还没有可比的公司历史定价区间；先用于横向比较，不单独据此判断贵或便宜。"
    elif ps_p75 is not None and pe_median is not None and forward_ps > ps_p75 and forward_pe < pe_median:
        pricing_state = "高增长已定价，持续性被折价"
        conclusion = "当前市值对远期营收给出的倍数高于历史常态，但对远期利润给出的倍数偏低：市场相信高利润会出现，同时担心它接近周期峰值、难以长期维持。"
    elif ps_p75 is not None and pe_p75 is not None and forward_ps > ps_p75 and forward_pe > pe_p75:
        pricing_state = "增长与持续性均高预期"
        conclusion = "当前市值既给了高营收预期，也给了高利润倍数；后续需要持续超预期才能消化定价。"
    elif ps_median is not None and pe_median is not None and forward_ps < ps_median and forward_pe < pe_median:
        pricing_state = "一致预期兑现要求较低"
        conclusion = "按当前一致预期计算，营收与利润倍数都低于自身历史中枢，但仍需确认一致预期本身没有处在下修阶段。"
    else:
        pricing_state = "收入与利润定价分化"
        conclusion = "收入倍数与利润倍数给出不同信号，不能用单一PE或PS判断，需要继续看利润率和周期持续时间。"

    consensus = {
        "symbol": symbol,
        "currency": financial.get("financialCurrency") or price.get("currency") or "USD",
        "current_year": current_year,
        "next_year": next_year,
        "analyst_target": {
            "average": _raw(financial.get("targetMeanPrice")),
            "median": _raw(financial.get("targetMedianPrice")),
            "low": _raw(financial.get("targetLowPrice")),
            "high": _raw(financial.get("targetHighPrice")),
            "analyst_count": int(_raw(financial.get("numberOfAnalystOpinions")) or 0),
        },
        "source_note": "一致预期为第三方汇总的分析师预测，不等于公司指引或硬事实；必须保留分析师数量、区间和更新时间。",
    }
    valuation = {
        "as_of": datetime.fromtimestamp(int(price.get("regularMarketTime") or datetime.now(timezone.utc).timestamp()), timezone.utc).date(),
        "current_price": _round(current_price, 2),
        "post_market_price": _round(_raw(price.get("postMarketPrice")), 2),
        "market_cap": _round(market_cap, 2),
        "shares_outstanding": _round(shares, 2),
        "quote_source": price.get("quoteSourceName") or "Yahoo Finance",
        "forward_ps": _round(forward_ps, 2),
        "forward_pe": _round(forward_pe, 2),
        "consensus_net_income": _round(implied_net_income, 2),
        "consensus_net_margin_pct": _round(consensus_net_margin * 100, 2) if consensus_net_margin is not None else None,
        "pricing_state": pricing_state,
        "conclusion": conclusion,
        "reverse_check": {
            "revenue_required_at_historical_median_ps": _round(_safe_ratio(market_cap, ps_median), 2),
            "revenue_required_at_historical_p75_ps": _round(_safe_ratio(market_cap, ps_p75), 2),
            "eps_required_at_historical_median_pe": _round(_safe_ratio(_safe_ratio(market_cap, pe_median), shares), 2),
            "consensus_revenue_gap_vs_median_ps_pct": _percent_change(_safe_ratio(market_cap, ps_median), next_revenue),
            "consensus_eps_gap_vs_median_pe_pct": _percent_change(_safe_ratio(_safe_ratio(market_cap, pe_median), shares), next_eps),
            "interpretation": "反推值是历史倍数校验，不是新的券商一致预期。历史倍数失效时应调整模型，而不是强行给结论。",
        },
        "current_fundamentals": {
            "trailing_revenue": _raw(financial.get("totalRevenue")),
            "profit_margin_pct": _round((_raw(financial.get("profitMargins")) or 0) * 100, 2),
            "gross_margin_pct": _round((_raw(financial.get("grossMargins")) or 0) * 100, 2),
            "cash": _raw(financial.get("totalCash")),
            "debt": _raw(financial.get("totalDebt")),
        },
    }
    valuation["market_cap_bridge"] = _market_cap_bridge(consensus, valuation)
    valuation["support_drivers"] = _financial_support_drivers(consensus, valuation, backtest)
    return consensus, valuation


def _a_share_market_anchor(db: Session, company: IndustryChainCompany) -> dict[str, Any] | None:
    if not company.full_code:
        return None
    cap = db.scalar(select(FactorMarketCapSnapshot).where(FactorMarketCapSnapshot.full_code == company.full_code))
    latest_bar = db.scalar(
        select(StockDailyBar)
        .where(StockDailyBar.full_code == company.full_code)
        .order_by(desc(StockDailyBar.trade_date))
        .limit(1)
    )
    if not cap or cap.total_market_cap is None or not latest_bar:
        return None
    total_market_cap = float(cap.total_market_cap)
    float_market_cap = float(cap.float_market_cap) if cap.float_market_cap is not None else None
    if cap.source == "tencent" and float_market_cap is not None and float_market_cap > total_market_cap:
        total_market_cap, float_market_cap = float_market_cap, total_market_cap
    as_of = cap.trade_date
    estimated = False
    if cap.trade_date and latest_bar.trade_date > cap.trade_date:
        anchor_bar = db.scalar(
            select(StockDailyBar)
            .where(StockDailyBar.full_code == company.full_code, StockDailyBar.trade_date <= cap.trade_date)
            .order_by(desc(StockDailyBar.trade_date))
            .limit(1)
        )
        if anchor_bar and anchor_bar.close and float(anchor_bar.close) > 0:
            ratio = float(latest_bar.close) / float(anchor_bar.close)
            if 0.25 <= ratio <= 4:
                total_market_cap *= ratio
                as_of = latest_bar.trade_date
                estimated = True
    return {
        "market_cap": total_market_cap,
        "current_price": float(latest_bar.close),
        "shares": total_market_cap / float(latest_bar.close) if latest_bar.close else None,
        "as_of": as_of,
        "source": cap.source or latest_bar.source or "本地行情",
        "estimated": estimated,
    }


def _eastmoney_period(rows: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    if index >= len(rows):
        return None
    row = rows[index]
    previous = rows[index - 1] if index > 0 else None
    revenue = _raw(row.get("TOTAL_OPERATE_INCOME"))
    eps = _raw(row.get("EPS"))
    previous_revenue = _raw((previous or {}).get("TOTAL_OPERATE_INCOME"))
    previous_eps = _raw((previous or {}).get("EPS"))
    return {
        "period": f"FY{row.get('YEAR')}",
        "end_date": f"{int(row.get('YEAR'))}-12-31" if row.get("YEAR") else None,
        "revenue": {
            "average": revenue,
            "low": None,
            "high": None,
            "analyst_count": int(_raw(row.get("TOTAL_OPERATE_INCOME_COUNT")) or 0),
            "year_ago": previous_revenue,
            "growth_pct": _percent_change(revenue, previous_revenue),
        },
        "eps": {
            "average": eps,
            "low": None,
            "high": None,
            "analyst_count": int(_raw(row.get("EPS_COUNT")) or 0),
            "year_ago": previous_eps,
            "growth_pct": _percent_change(eps, previous_eps),
        },
        "eps_revision": {
            "current": eps,
            "7_days_ago": None,
            "30_days_ago": None,
            "60_days_ago": None,
            "90_days_ago": None,
            "change_30d_pct": None,
            "change_90d_pct": None,
            "up_30d": 0,
            "down_30d": 0,
        },
        "net_income": _raw(row.get("PARENT_NETPROFIT")),
    }


def build_eastmoney_expectation_payload(
    company: IndustryChainCompany,
    payload: dict[str, Any],
    market_anchor: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rows = sorted(payload.get("yctj_list") or [], key=lambda item: int(item.get("YEAR") or 0))
    forecast_indexes = [index for index, row in enumerate(rows) if row.get("YEAR_MARK") == "E"]
    if not forecast_indexes:
        consensus = {
            "symbol": company.full_code,
            "currency": "CNY",
            "current_year": None,
            "next_year": None,
            "analyst_target": {"average": None, "median": None, "low": None, "high": None, "analyst_count": 0},
            "source_note": "当前公开汇总没有有效机构预测年份；这表示覆盖缺失，不代表盈利预期为零或看空。",
        }
        valuation = {
            "as_of": market_anchor.get("as_of") or date.today(),
            "current_price": _round(_raw(market_anchor.get("current_price")), 2),
            "post_market_price": None,
            "market_cap": _round(_raw(market_anchor.get("market_cap")), 2),
            "shares_outstanding": _round(_raw(market_anchor.get("shares")), 2),
            "quote_source": market_anchor.get("source") or "本地A股行情",
            "forward_ps": None,
            "forward_pe": None,
            "consensus_net_income": None,
            "consensus_net_margin_pct": None,
            "pricing_state": "无分析师覆盖",
            "conclusion": "股票和市值数据存在，但当前没有有效机构一致预测，不纳入板块预期一致性分母。",
            "reverse_check": {
                "revenue_required_at_historical_median_ps": None,
                "revenue_required_at_historical_p75_ps": None,
                "eps_required_at_historical_median_pe": None,
                "consensus_revenue_gap_vs_median_ps_pct": None,
                "consensus_eps_gap_vs_median_pe_pct": None,
                "interpretation": "缺少一致预期时不反向虚构分析师预测。",
            },
            "current_fundamentals": {},
        }
        backtest = _empty_backtest("当前没有机构一致预测；后续出现覆盖后再开始积累修订历史。")
        valuation["market_cap_bridge"] = _market_cap_bridge(consensus, valuation)
        valuation["support_drivers"] = []
        return consensus, valuation, backtest
    current_index = forecast_indexes[0]
    next_index = forecast_indexes[1] if len(forecast_indexes) > 1 else current_index
    current_year = _eastmoney_period(rows, current_index)
    next_year = _eastmoney_period(rows, next_index)
    market_cap = _raw(market_anchor.get("market_cap"))
    current_price = _raw(market_anchor.get("current_price"))
    shares = _raw(market_anchor.get("shares"))
    next_revenue = ((next_year or {}).get("revenue") or {}).get("average")
    next_eps = ((next_year or {}).get("eps") or {}).get("average")
    next_net_income = (next_year or {}).get("net_income")
    forward_ps = _safe_ratio(market_cap, next_revenue)
    forward_pe = _safe_ratio(market_cap, next_net_income) or _safe_ratio(current_price, next_eps)
    net_margin = _safe_ratio(next_net_income, next_revenue)
    consensus = {
        "symbol": company.full_code,
        "currency": "CNY",
        "current_year": current_year,
        "next_year": next_year,
        "analyst_target": {"average": None, "median": None, "low": None, "high": None, "analyst_count": 0},
        "source_note": "东方财富根据各机构研报摘录形成一致预测，属于二手汇总，不等于公司指引；分析师数量可能随覆盖变化。",
    }
    valuation = {
        "as_of": market_anchor.get("as_of") or date.today(),
        "current_price": _round(current_price, 2),
        "post_market_price": None,
        "market_cap": _round(market_cap, 2),
        "shares_outstanding": _round(shares, 2),
        "quote_source": market_anchor.get("source") or "本地A股行情",
        "forward_ps": _round(forward_ps, 2),
        "forward_pe": _round(forward_pe, 2),
        "consensus_net_income": _round(next_net_income, 2),
        "consensus_net_margin_pct": _round(net_margin * 100, 2) if net_margin is not None else None,
        "pricing_state": "一致预期已取得，历史修订开始留档",
        "conclusion": "当前已能看到券商一致预期对应的远期营收、净利润和估值；是否存在预期差，还要与订单、价格、产能和公司指引支持的区间比较。",
        "reverse_check": {
            "revenue_required_at_historical_median_ps": None,
            "revenue_required_at_historical_p75_ps": None,
            "eps_required_at_historical_median_pe": None,
            "consensus_revenue_gap_vs_median_ps_pct": None,
            "consensus_eps_gap_vs_median_pe_pct": None,
            "interpretation": "A股历史一致预期快照从本次接入后逐日积累；在样本充分前不虚构历史倍数中枢。",
        },
        "current_fundamentals": {},
    }
    backtest = {
        "method": "从当前日期开始保存每日公开一致预期快照；后续用实际财报回填预测误差，并回测市值变化。",
        "sample_start": None,
        "sample_end": None,
        "forward_ps": _distribution([]),
        "profitable_forward_pe": _distribution([]),
        "rows": [],
    }
    valuation["market_cap_bridge"] = _market_cap_bridge(consensus, valuation)
    valuation["support_drivers"] = _financial_support_drivers(consensus, valuation, backtest)
    return consensus, valuation, backtest


def _snapshot_out(row: CompanyExpectationSnapshot, stale: bool = False) -> dict[str, Any]:
    return {
        "id": row.id,
        "company_id": row.company_id,
        "symbol": row.symbol,
        "market": row.market,
        "provider": row.provider,
        "snapshot_date": row.snapshot_date,
        "status": row.status,
        "message": row.message,
        "consensus": _json_load(row.consensus_json, {}),
        "valuation": _json_load(row.valuation_json, {}),
        "backtest": _json_load(row.backtest_json, {}),
        "sources": _json_load(row.sources_json, []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "stale": stale,
    }


def _company_or_404(db: Session, chain_id: int, company_id: int) -> IndustryChainCompany:
    company = db.scalar(
        select(IndustryChainCompany).where(
            IndustryChainCompany.id == company_id,
            IndustryChainCompany.chain_id == chain_id,
        )
    )
    if not company:
        raise HTTPException(status_code=404, detail="产业公司不存在")
    return company


def _latest_snapshot(db: Session, company_id: int) -> CompanyExpectationSnapshot | None:
    return db.scalar(
        select(CompanyExpectationSnapshot)
        .where(CompanyExpectationSnapshot.company_id == company_id)
        .order_by(desc(CompanyExpectationSnapshot.snapshot_date), desc(CompanyExpectationSnapshot.id))
        .limit(1)
    )


def refresh_company_expectation(db: Session, company: IndustryChainCompany) -> CompanyExpectationSnapshot:
    market = company.market or "A股"
    cik: str | None = None
    history_error: str | None = None
    if market == "美股" and company.code:
        symbol = company.code.strip().upper()
        yahoo_payload = fetch_yahoo_consensus(symbol)
        try:
            cik, company_facts = fetch_sec_company_facts(symbol)
            chart = fetch_yahoo_daily_chart(symbol)
            backtest = build_sec_forward_valuation_backtest(company_facts, chart)
        except Exception as exc:
            history_error = str(exc)
            backtest = _empty_backtest("已取得美股一致预期；SEC历史财务回测暂不可用，不影响当前一致预期展示。")
        provider = YAHOO_PROVIDER if backtest.get("rows") else GLOBAL_YAHOO_PROVIDER
        consensus, valuation = build_expectation_payload(symbol, yahoo_payload, backtest)
    elif market == "A股" and company.full_code:
        symbol = company.full_code.strip().upper()
        provider = EASTMONEY_PROVIDER
        anchor = _a_share_market_anchor(db, company)
        if not anchor:
            raise HTTPException(status_code=400, detail="当前A股市值或收盘价锚缺失，暂不能计算一致预期估值。")
        eastmoney_payload = fetch_eastmoney_consensus(symbol)
        consensus, valuation, backtest = build_eastmoney_expectation_payload(company, eastmoney_payload, anchor)
    else:
        yahoo_symbol = yahoo_symbol_for_company(company)
        if not yahoo_symbol:
            raise HTTPException(status_code=400, detail="该公司未上市或当前市场代码无法自动匹配，不纳入板块一致预期分母。")
        symbol = yahoo_symbol
        provider = GLOBAL_YAHOO_PROVIDER
        yahoo_payload = fetch_yahoo_consensus(symbol)
        backtest = _empty_backtest("已取得全球市场分析师一致预期；历史预测修订从本地每日快照开始积累。")
        consensus, valuation = build_expectation_payload(symbol, yahoo_payload, backtest)
    # The snapshot date represents when the consensus was observed.  Quote dates
    # remain in valuation.as_of.  Keeping the two clocks separate is essential:
    # otherwise a weekend or stale quote would overwrite a newer analyst revision.
    snapshot_date = date.today()
    row = db.scalar(
        select(CompanyExpectationSnapshot).where(
            CompanyExpectationSnapshot.company_id == company.id,
            CompanyExpectationSnapshot.provider == provider,
            CompanyExpectationSnapshot.snapshot_date == snapshot_date,
        )
    )
    if not row:
        row = CompanyExpectationSnapshot(
            company_id=company.id,
            symbol=symbol,
            market=company.market or "美股",
            provider=provider,
            snapshot_date=snapshot_date,
        )
        db.add(row)
    next_year = consensus.get("next_year") or {}
    next_revenue = (next_year.get("revenue") or {}).get("average")
    next_eps = (next_year.get("eps") or {}).get("average")
    row.status = "ready" if next_revenue is not None or next_eps is not None else "no_consensus"
    row.message = "当前没有有效分析师预测覆盖。" if row.status == "no_consensus" else history_error
    row.consensus_json = _json_dump(consensus)
    row.valuation_json = _json_dump(valuation)
    row.backtest_json = _json_dump(backtest)
    if market == "美股":
        sources = [
            {
                "name": "Yahoo Finance 分析师一致预期",
                "url": YAHOO_ANALYSIS_URL.format(symbol=symbol),
                "tier": "二手汇总",
                "note": "保存分析师数量、预测区间和修订趋势；不作为公司硬证据。",
            },
            {
                "name": "Yahoo Finance / Nasdaq行情",
                "url": f"https://finance.yahoo.com/quote/{symbol}/",
                "tier": "市场数据",
                "note": "用于价格、市值和历史交易日对齐。",
            },
        ]
        if cik:
            sources.insert(
                1,
                {
                    "name": "SEC Company Facts",
                    "url": SEC_COMPANY_FACTS.format(cik=cik),
                    "tier": "监管原始数据",
                    "note": "历史财务用于回测下一财年实际兑现，不代表当时一致预期。",
                },
            )
    elif market == "A股":
        sources = [
            {
                "name": "东方财富盈利预测",
                "url": f"https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/Index?code={symbol}&type=web",
                "tier": "券商预测二手汇总",
                "note": "根据机构研报摘录的一致预测，保留预测年份与覆盖机构数量。",
            },
            {
                "name": "本地A股行情与市值快照",
                "url": "",
                "tier": "市场数据",
                "note": "用于把一致预期换算成当前远期PE和PS。",
            },
        ]
    else:
        sources = [
            {
                "name": "Yahoo Finance 全球市场一致预期",
                "url": YAHOO_ANALYSIS_URL.format(symbol=symbol),
                "tier": "分析师预测二手汇总",
                "note": "用于跨市场一致预期、修订和估值比较；不替代公司公告。",
            },
            {
                "name": "Yahoo Finance 全球市场行情",
                "url": f"https://finance.yahoo.com/quote/{symbol}/",
                "tier": "市场数据",
                "note": "用于当前价格与市值锚。",
            },
        ]
    row.sources_json = _json_dump(sources)
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return row


def _median_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [_raw(row.get(key)) for row in rows]
    clean = [value for value in values if value is not None]
    return _round(statistics.median(clean), 2) if clean else None


def build_expectation_gap_calibration(summary: dict[str, Any]) -> dict[str, Any]:
    """Shrink the five-year artifact into a stable, UI-safe calibration contract."""

    markets: dict[str, Any] = {}
    for market_name in ("A股", "美股"):
        source = (summary.get("markets") or {}).get(market_name) or {}
        validation = source.get("validation") or {}
        validation_a = ((validation.get("by_decision") or {}).get("A") or {})
        verdict = source.get("verdict") or {}
        state = verdict.get("state") or "missing"
        markets[market_name] = {
            "state": state,
            "interpretation": verdict.get("interpretation") or "尚无留出样本结论。",
            "thresholds": source.get("thresholds") or {},
            "validation": {
                "horizon": validation.get("horizon"),
                "a_sample_count": validation_a.get("sample_count"),
                "a_win_rate_pct": validation_a.get("win_rate_pct"),
                "a_median_excess_return_pct": validation_a.get("median_excess_return_pct"),
                "a_ci_low_pct": validation_a.get("bootstrap_95ci_low_pct"),
                "a_ci_high_pct": validation_a.get("bootstrap_95ci_high_pct"),
                "pricing_gain_a_vs_b_median_pct": validation.get("pricing_gain_a_vs_b_median_pct"),
                "filter_gain_a_vs_bc_median_pct": validation.get("filter_gain_a_vs_bc_median_pct"),
            },
            "usage": (
                "已通过留出样本，可作为A股公司预期差的严格校准门；仍不等于自动买入。"
                if market_name == "A股" and state == "validated"
                else "方法未通过留出样本，只保留观察和继续积累，不使用该市场阈值自动升级。"
            ),
        }
    overall = summary.get("overall_verdict") or {}
    return {
        "status": "ready" if summary.get("status") == "completed" else summary.get("status") or "missing",
        "method_version": summary.get("method_version"),
        "generated_at": summary.get("generated_at"),
        "period": summary.get("period") or {},
        "validation_start": summary.get("validation_start"),
        "overall_state": overall.get("state") or "not_validated",
        "overall_interpretation": overall.get("interpretation") or "跨市场整体方法尚未验证。",
        "markets": markets,
        "decision_rules": {
            "A": "硬证据强、披露前未定价、首个可交易日确认；才进入可执行候选。",
            "B": "硬证据强，但价格已提前交易或首日尚未确认；等价格或新催化。",
            "C": "方向偏正，但硬证据、加速度或定价数据不完整；继续验证。",
            "D": "证据为负、偏弱或不满足可验证变化门槛；回避或下调。",
        },
        "guardrails": [
            "机构一致预期只判断方向，不替代公司硬证据。",
            "校准建议不自动覆盖产业趋势里的正式预期差结论。",
            "A股通过校准；美股未验证，只观察，不套用A股阈值。",
            "首日确认后从下一可交易日计算，不把一字涨停或停牌当成可成交。",
        ],
    }


def _load_expectation_gap_calibration() -> dict[str, Any]:
    path = get_data_root() / EXPECTATION_GAP_SUMMARY_RELATIVE_PATH
    if not path.is_file():
        return {
            "status": "missing",
            "method_version": None,
            "period": {},
            "overall_state": "missing",
            "overall_interpretation": "五年预期差校准文件尚未生成。",
            "markets": {},
            "decision_rules": {},
            "guardrails": [],
        }
    try:
        return build_expectation_gap_calibration(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return {
            "status": "error",
            "method_version": None,
            "period": {},
            "overall_state": "error",
            "overall_interpretation": f"五年预期差校准读取失败：{exc}",
            "markets": {},
            "decision_rules": {},
            "guardrails": [],
        }


@lru_cache(maxsize=1)
def _load_expectation_gap_benchmark() -> list[dict[str, Any]]:
    path = get_data_root() / EXPECTATION_GAP_BENCHMARK_RELATIVE_PATH
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


def _model_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def build_company_expectation_gap_gate(
    company: Any,
    evidence_rows: list[Any],
    stock_bars: list[dict[str, Any]],
    benchmark_bars: list[dict[str, Any]],
    calibration: dict[str, Any],
) -> dict[str, Any]:
    """Apply the frozen qq 2.1 gate without mutating the saved company conclusion."""

    market = str(_model_value(company, "market", "A股") or "A股")
    market_method = (calibration.get("markets") or {}).get(market) or {}
    method_state = market_method.get("state") or "missing"
    signal_value = _model_value(company, "expectation_as_of")
    try:
        signal_date = signal_value if isinstance(signal_value, date) else date.fromisoformat(str(signal_value)[:10])
    except (TypeError, ValueError):
        signal_date = None

    hard_sources = [
        item for item in evidence_rows
        if _model_value(item, "source_tier") in {"官方硬证据", "公司披露", "行业标准"}
        and _model_value(item, "evidence_state", "有效") == "有效"
        and (not _model_value(item, "valid_until") or _model_value(item, "valid_until") >= date.today())
    ]
    source_titles = [str(_model_value(item, "title", "未命名硬证据")) for item in hard_sources[:3]]
    verified = _model_value(company, "verification_status") == "已确认"
    failed = _model_value(company, "verification_status") == "失败"
    formal_gap = str(_model_value(company, "expectation_gap_status", "无法判断") or "无法判断")
    growth = _raw(_model_value(company, "expectation_evidence_growth_pct"))
    acceleration = _raw(_model_value(company, "expectation_evidence_acceleration_pct"))

    base = {
        "method_state": method_state,
        "signal_date": signal_date.isoformat() if signal_date else None,
        "formal_gap_status": formal_gap,
        "does_not_overwrite_formal_conclusion": True,
        "evidence": {
            "state": "pending",
            "growth_pct": growth,
            "acceleration_pct": acceleration,
            "hard_source_count": len(hard_sources),
            "source_titles": source_titles,
            "company_verified": verified,
            "threshold_growth_pct": (market_method.get("thresholds") or {}).get("evidence_growth_pct"),
            "threshold_acceleration_pct": (market_method.get("thresholds") or {}).get("evidence_acceleration_pct"),
        },
        "pricing": {
            "state": "pending",
            "pre_20d_excess_pct": None,
            "pre_60d_excess_pct": None,
            "unpriced_threshold_pct": (market_method.get("thresholds") or {}).get("unpriced_20d_excess_pct"),
        },
        "confirmation": {
            "state": "pending",
            "observation_date": None,
            "excess_pct": None,
            "threshold_pct": (market_method.get("thresholds") or {}).get("confirmation_excess_pct"),
        },
        "execution": {"state": "not_ready", "entry_date": None},
    }

    if market != "A股" or method_state != "validated":
        return {
            **base,
            "decision_code": "观察",
            "label": "方法未验证｜仅观察",
            "reason": "该市场尚未通过留出样本，不套用A股阈值，也不自动升级A类。",
        }

    thresholds_payload = market_method.get("thresholds") or {}
    required_threshold_keys = {
        "evidence_growth_pct",
        "evidence_acceleration_pct",
        "unpriced_20d_excess_pct",
        "priced_20d_excess_pct",
        "priced_60d_excess_pct",
        "confirmation_excess_pct",
    }
    if not required_threshold_keys.issubset(thresholds_payload):
        return {
            **base,
            "decision_code": "观察",
            "label": "校准缺失｜仅观察",
            "reason": "A股冻结阈值不完整，暂不进行A/B/C/D分层。",
        }
    thresholds = GapThresholds(**{key: float(thresholds_payload[key]) for key in required_threshold_keys})

    pricing = {
        "pre_20d_excess_pct": None,
        "pre_60d_excess_pct": None,
        "distance_to_60d_high_pct": None,
    }
    confirmation = {
        "confirmation_return_pct": None,
        "confirmation_benchmark_return_pct": None,
        "market_confirmation_excess_pct": None,
    }
    observation: dict[str, Any] = {"available": False, "reason": "missing_signal_or_price"}
    entry: dict[str, Any] = {"available": False, "reason": "confirmation_not_ready"}
    code = str(_model_value(company, "code", "") or _model_value(company, "full_code", "") or "")
    if signal_date and stock_bars and benchmark_bars:
        pricing = pre_event_pricing(stock_bars, benchmark_bars, signal_date)
        observation = next_tradable_entry(stock_bars, signal_date, market, code, _model_value(company, "name"))
        if observation.get("available"):
            observation_date = date.fromisoformat(str(observation["entry_date"]))
            confirmation = market_confirmation(stock_bars, benchmark_bars, observation_date)
            entry = next_tradable_entry(stock_bars, observation_date, market, code, _model_value(company, "name"))

    evidence_ready = verified and bool(hard_sources)
    event = {
        "profit_growth_pct": growth if evidence_ready else None,
        "growth_acceleration_pct": acceleration if evidence_ready else None,
        "evidence_grade": "A" if evidence_ready else "",
        "negative_evidence": failed or formal_gap == "负向预期差",
        **pricing,
        **confirmation,
    }
    classified = classify_event(event, thresholds)
    decision_code = classified["decision_code"]
    reason = classified["decision_reason"]
    if formal_gap == "无法判断" and decision_code in {"A", "B"}:
        decision_code = "C"
        reason = "硬证据指标可能达标，但正式预期差仍未完成同口径判断，先留在C类验证。"

    evidence_state_labels = {
        "strong": "通过",
        "positive_unconfirmed": "方向偏正",
        "insufficient": "数据待补",
        "weak": "未达门槛",
        "negative": "负向",
    }
    pricing_state_labels = {"unpriced": "披露前未定价", "partial": "部分提前交易", "priced": "已提前定价", "unknown": "行情待补"}
    confirmation_excess = _raw(confirmation.get("market_confirmation_excess_pct"))
    confirmation_state = (
        "通过"
        if confirmation_excess is not None and confirmation_excess >= thresholds.confirmation_excess_pct
        else "未通过"
        if confirmation_excess is not None
        else "待首个可交易日"
    )
    label_map = {"A": "A｜可执行候选", "B": "B｜等价格/确认", "C": "C｜继续验证", "D": "D｜回避/下调"}
    return {
        **base,
        "decision_code": decision_code,
        "label": label_map[decision_code],
        "reason": reason,
        "evidence": {
            **base["evidence"],
            "state": evidence_state_labels.get(classified["evidence_state"], classified["evidence_state"]),
        },
        "pricing": {
            **base["pricing"],
            "state": pricing_state_labels.get(classified["pricing_state"], classified["pricing_state"]),
            "pre_20d_excess_pct": _round(_raw(pricing.get("pre_20d_excess_pct")), 2),
            "pre_60d_excess_pct": _round(_raw(pricing.get("pre_60d_excess_pct")), 2),
        },
        "confirmation": {
            **base["confirmation"],
            "state": confirmation_state,
            "observation_date": observation.get("entry_date"),
            "excess_pct": _round(confirmation_excess, 2),
        },
        "execution": {
            "state": "next_session_available" if decision_code == "A" and entry.get("available") else "not_ready",
            "entry_date": entry.get("entry_date") if entry.get("available") else None,
            "deferred_sessions": entry.get("deferred_sessions"),
        },
    }


def _company_summary_row(
    company: IndustryChainCompany,
    snapshot: CompanyExpectationSnapshot | None,
    expectation_gap_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    is_private = (company.exchange or "").upper() == "PRIVATE"
    is_supported = bool(company.market == "A股" and company.full_code) or yahoo_symbol_for_company(company) is not None
    consensus = _json_load(snapshot.consensus_json, {}) if snapshot else {}
    valuation = _json_load(snapshot.valuation_json, {}) if snapshot else {}
    next_year = consensus.get("next_year") or {}
    revenue = next_year.get("revenue") or {}
    eps = next_year.get("eps") or {}
    revision = next_year.get("eps_revision") or {}
    covered = revenue.get("average") is not None or eps.get("average") is not None
    if is_private:
        coverage_status = "未上市"
    elif not is_supported:
        coverage_status = "市场待接入"
    elif snapshot is None:
        coverage_status = "待抓取"
    elif not covered:
        coverage_status = "无分析师覆盖"
    else:
        coverage_status = "已覆盖"
    return {
        "company_id": company.id,
        "name": company.name,
        "market": company.market,
        "exchange": company.exchange,
        "code": company.code,
        "full_code": company.full_code,
        "is_listed": not is_private,
        "is_supported": is_supported,
        "is_global_leader": company.is_global_leader,
        "is_domestic_alternative": company.is_domestic_alternative,
        "is_primary": company.is_primary,
        "coverage_status": coverage_status,
        "provider": snapshot.provider if snapshot else None,
        "snapshot_date": snapshot.snapshot_date if snapshot else None,
        "forecast_period": next_year.get("end_date"),
        "revenue_analyst_count": int(revenue.get("analyst_count") or 0),
        "eps_analyst_count": int(eps.get("analyst_count") or 0),
        "next_revenue_growth_pct": _round(_raw(revenue.get("growth_pct")), 2),
        "next_eps_growth_pct": _round(_raw(eps.get("growth_pct")), 2),
        "eps_revision_30d_pct": _round(_raw(revision.get("change_30d_pct")), 2),
        "forward_ps": _round(_raw(valuation.get("forward_ps")), 2),
        "forward_pe": _round(_raw(valuation.get("forward_pe")), 2),
        "pricing_state": valuation.get("pricing_state"),
        "market_cap": _raw(valuation.get("market_cap")),
        "currency": consensus.get("currency"),
        "expectation_gap_gate": expectation_gap_gate,
    }


def _group_statistics(rows: list[dict[str, Any]], key: str, label: str) -> dict[str, Any]:
    covered = [row for row in rows if row.get("coverage_status") == "已覆盖"]
    paired_growth = [
        row for row in covered
        if row.get("next_revenue_growth_pct") is not None and row.get("next_eps_growth_pct") is not None
    ]
    positive = [row for row in paired_growth if row["next_revenue_growth_pct"] > 0 and row["next_eps_growth_pct"] > 0]
    negative = [row for row in paired_growth if row["next_revenue_growth_pct"] < 0 and row["next_eps_growth_pct"] < 0]
    revisions = [row for row in covered if row.get("eps_revision_30d_pct") is not None]
    revision_up = [row for row in revisions if row["eps_revision_30d_pct"] > 0.5]
    revision_down = [row for row in revisions if row["eps_revision_30d_pct"] < -0.5]
    return {
        "key": key,
        "label": label,
        "company_count": len(rows),
        "consensus_count": len(covered),
        "paired_growth_count": len(paired_growth),
        "positive_growth_count": len(positive),
        "negative_growth_count": len(negative),
        "positive_growth_ratio": _round(len(positive) / len(paired_growth) * 100, 1) if paired_growth else None,
        "median_revenue_growth_pct": _median_metric(covered, "next_revenue_growth_pct"),
        "median_eps_growth_pct": _median_metric(covered, "next_eps_growth_pct"),
        "revision_sample_count": len(revisions),
        "revision_up_count": len(revision_up),
        "revision_down_count": len(revision_down),
        "revision_up_ratio": _round(len(revision_up) / len(revisions) * 100, 1) if revisions else None,
        "median_eps_revision_30d_pct": _median_metric(revisions, "eps_revision_30d_pct"),
        "median_forward_ps": _median_metric(covered, "forward_ps"),
        "median_forward_pe": _median_metric(covered, "forward_pe"),
    }


def _consistency_result(
    all_group: dict[str, Any],
    global_group: dict[str, Any],
    domestic_group: dict[str, Any],
) -> dict[str, Any]:
    paired = int(all_group.get("paired_growth_count") or 0)
    positive_ratio = _raw(all_group.get("positive_growth_ratio"))
    if paired < 3 or positive_ratio is None:
        growth_status = "样本不足"
    elif positive_ratio >= 75:
        growth_status = "高度同向增长"
    elif positive_ratio >= 55:
        growth_status = "多数同向增长"
    elif positive_ratio >= 40:
        growth_status = "预期分化"
    else:
        growth_status = "多数转弱"

    revision_count = int(all_group.get("revision_sample_count") or 0)
    revision_up_ratio = _raw(all_group.get("revision_up_ratio"))
    if revision_count < 3 or revision_up_ratio is None:
        revision_status = "修订样本积累中"
    elif revision_up_ratio >= 65:
        revision_status = "盈利预期同步上修"
    elif revision_up_ratio <= 35:
        revision_status = "盈利预期同步下修"
    else:
        revision_status = "盈利预期修订分化"

    global_revenue = _raw(global_group.get("median_revenue_growth_pct"))
    domestic_revenue = _raw(domestic_group.get("median_revenue_growth_pct"))
    global_eps = _raw(global_group.get("median_eps_growth_pct"))
    domestic_eps = _raw(domestic_group.get("median_eps_growth_pct"))
    if min(int(global_group.get("consensus_count") or 0), int(domestic_group.get("consensus_count") or 0)) < 2:
        cross_group_status = "对照样本不足"
    elif None in {global_revenue, domestic_revenue, global_eps, domestic_eps}:
        cross_group_status = "对照指标不完整"
    elif global_revenue > 0 and global_eps > 0 and domestic_revenue > 0 and domestic_eps > 0:
        cross_group_status = "海外龙头与国内映射同向"
    elif (global_revenue > 0 and global_eps > 0) != (domestic_revenue > 0 and domestic_eps > 0):
        cross_group_status = "海外与国内预期分化"
    else:
        cross_group_status = "两组共同偏弱"

    if growth_status in {"高度同向增长", "多数同向增长"} and cross_group_status == "海外龙头与国内映射同向":
        conclusion = "产业公司的一致预期多数同向增长，海外龙头与国内映射也同步；下一步重点看修订能否继续上行，以及股价是否已经提前交易。"
    elif "分化" in growth_status or "分化" in cross_group_status:
        conclusion = "板块内部预期存在分化，不能把产业景气直接外推到全部股票；应优先跟踪盈利预测上修且有公司硬证据的表达。"
    elif growth_status == "多数转弱":
        conclusion = "多数公司的下一财年增长预期偏弱，产业叙事暂未形成广泛盈利确认。"
    else:
        conclusion = "一致预期覆盖仍在补齐，当前只展示可取得的机构预测，不把缺失样本视为看空。"
    return {
        "growth_status": growth_status,
        "revision_status": revision_status,
        "cross_group_status": cross_group_status,
        "conclusion": conclusion,
    }


def build_industry_expectation_summary(
    db: Session,
    chain: IndustryChain,
    refresh_result: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    companies = list(
        db.scalars(
            select(IndustryChainCompany)
            .where(IndustryChainCompany.chain_id == chain.id)
            .order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)
        )
    )
    company_ids = [company.id for company in companies]
    snapshots = list(
        db.scalars(
            select(CompanyExpectationSnapshot)
            .where(CompanyExpectationSnapshot.company_id.in_(company_ids or [-1]))
            .order_by(desc(CompanyExpectationSnapshot.snapshot_date), desc(CompanyExpectationSnapshot.id))
        )
    )
    latest_by_company: dict[int, CompanyExpectationSnapshot] = {}
    for snapshot in snapshots:
        latest_by_company.setdefault(snapshot.company_id, snapshot)
    calibration = _load_expectation_gap_calibration()
    evidence_rows = list(
        db.scalars(
            select(IndustryChainEvidence)
            .where(IndustryChainEvidence.company_id.in_(company_ids or [-1]))
            .order_by(desc(IndustryChainEvidence.evidence_date), desc(IndustryChainEvidence.id))
        )
    )
    evidence_by_company: dict[int, list[IndustryChainEvidence]] = {}
    for evidence in evidence_rows:
        if evidence.company_id is not None:
            evidence_by_company.setdefault(evidence.company_id, []).append(evidence)
    full_codes = [company.full_code for company in companies if company.market == "A股" and company.full_code]
    stock_bar_rows = list(
        db.scalars(
            select(StockDailyBar)
            .where(StockDailyBar.full_code.in_(full_codes or ["__none__"]))
            .order_by(StockDailyBar.full_code, StockDailyBar.trade_date)
        )
    )
    bars_by_code: dict[str, list[dict[str, Any]]] = {}
    for bar in stock_bar_rows:
        bars_by_code.setdefault(bar.full_code, []).append(
            {
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "amount": bar.amount,
            }
        )
    benchmark_bars = _load_expectation_gap_benchmark()
    rows = []
    for company in companies:
        gate = build_company_expectation_gap_gate(
            company,
            evidence_by_company.get(company.id, []),
            bars_by_code.get(company.full_code or "", []),
            benchmark_bars,
            calibration,
        )
        rows.append(_company_summary_row(company, latest_by_company.get(company.id), gate))
    listed = [row for row in rows if row["is_listed"]]
    all_group = _group_statistics(listed, "all", "全部上市公司")
    global_group = _group_statistics([row for row in listed if row["is_global_leader"]], "global", "全球优势公司")
    domestic_group = _group_statistics(
        [row for row in rows if row["market"] == "A股" and row["is_domestic_alternative"]],
        "domestic",
        "国内可替代公司",
    )
    other_group = _group_statistics(
        [row for row in listed if not row["is_global_leader"] and not row["is_domestic_alternative"]],
        "other",
        "其他产业公司",
    )
    covered = [row for row in rows if row["coverage_status"] == "已覆盖"]
    universe = {
        "company_rows": len(rows),
        "unique_stocks": len({row["full_code"] for row in listed if row.get("full_code")}),
        "listed_companies": len(listed),
        "unlisted_companies": len(rows) - len(listed),
        "supported_companies": len([row for row in listed if row["is_supported"]]),
        "consensus_companies": len(covered),
        "coverage_rate": _round(len(covered) / len(listed) * 100, 1) if listed else None,
        "no_analyst_coverage": len([row for row in rows if row["coverage_status"] == "无分析师覆盖"]),
        "pending_fetch": len([row for row in rows if row["coverage_status"] == "待抓取"]),
    }
    summary = {
        "chain_id": chain.id,
        "chain_name": chain.name,
        "snapshot_date": date.today(),
        "universe": universe,
        "groups": [all_group, global_group, domestic_group, other_group],
        "consistency": _consistency_result(all_group, global_group, domestic_group),
        "expectation_gap_calibration": calibration,
        "companies": rows,
        "refresh": refresh_result or {},
        "method_note": "公司等权统计，不把不同币种市值直接相加；一致预期只判断方向，A股公司再经过硬证据、披露前定价与首日盘面确认的五年校准门。校准建议不自动覆盖正式结论。",
    }
    if persist:
        snapshot_row = db.scalar(
            select(IndustryExpectationSnapshot).where(
                IndustryExpectationSnapshot.chain_id == chain.id,
                IndustryExpectationSnapshot.snapshot_date == date.today(),
            )
        )
        if not snapshot_row:
            snapshot_row = IndustryExpectationSnapshot(chain_id=chain.id, snapshot_date=date.today())
            db.add(snapshot_row)
        snapshot_row.status = "ready"
        snapshot_row.summary_json = _json_dump({key: value for key, value in summary.items() if key != "refresh"})
        snapshot_row.updated_at = now_utc()
        db.commit()
        db.refresh(snapshot_row)
        summary["id"] = snapshot_row.id
    history_rows = list(
        db.scalars(
            select(IndustryExpectationSnapshot)
            .where(IndustryExpectationSnapshot.chain_id == chain.id)
            .order_by(desc(IndustryExpectationSnapshot.snapshot_date))
            .limit(30)
        )
    )
    summary["history"] = [
        {
            "snapshot_date": item.snapshot_date,
            "coverage_rate": (_json_load(item.summary_json, {}).get("universe") or {}).get("coverage_rate"),
            "growth_status": (_json_load(item.summary_json, {}).get("consistency") or {}).get("growth_status"),
            "revision_status": (_json_load(item.summary_json, {}).get("consistency") or {}).get("revision_status"),
        }
        for item in history_rows
    ]
    return summary


def _chain_or_404(db: Session, chain_id: int) -> IndustryChain:
    chain = db.get(IndustryChain, chain_id)
    if not chain:
        raise HTTPException(status_code=404, detail="产业不存在")
    return chain


@router.get("/{chain_id}/expectation-summary")
def industry_expectation_summary(
    chain_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return build_industry_expectation_summary(db, _chain_or_404(db, chain_id))


@router.post("/{chain_id}/expectation-summary/refresh")
def refresh_industry_expectation_summary(
    chain_id: int,
    mode: str = Query(default="missing", pattern="^(missing|all)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    companies = list(
        db.scalars(
            select(IndustryChainCompany)
            .where(IndustryChainCompany.chain_id == chain_id)
            .order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)
        )
    )
    result: dict[str, Any] = {
        "mode": mode,
        "attempted": 0,
        "succeeded": 0,
        "no_consensus": 0,
        "skipped_today": 0,
        "unsupported": 0,
        "failed": 0,
        "errors": [],
    }
    for company in companies:
        supported = bool(company.market == "A股" and company.full_code) or yahoo_symbol_for_company(company) is not None
        if not supported:
            result["unsupported"] += 1
            continue
        cached = _latest_snapshot(db, company.id)
        if mode == "missing" and cached and cached.snapshot_date == date.today():
            result["skipped_today"] += 1
            continue
        result["attempted"] += 1
        try:
            snapshot = refresh_company_expectation(db, company)
            if snapshot.status == "ready":
                result["succeeded"] += 1
            else:
                result["no_consensus"] += 1
        except Exception as exc:
            db.rollback()
            result["failed"] += 1
            if len(result["errors"]) < 20:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                result["errors"].append({"company_id": company.id, "name": company.name, "message": str(detail)})
    return build_industry_expectation_summary(db, chain, refresh_result=result)


@router.get("/{chain_id}/companies/{company_id}/expectation-analysis")
def company_expectation_analysis(
    chain_id: int,
    company_id: int,
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    company = _company_or_404(db, chain_id, company_id)
    cached = _latest_snapshot(db, company.id)
    if cached and not refresh and cached.snapshot_date >= date.today() - timedelta(days=1):
        return _snapshot_out(cached)
    try:
        return _snapshot_out(refresh_company_expectation(db, company))
    except HTTPException:
        raise
    except Exception as exc:
        if cached:
            payload = _snapshot_out(cached, stale=True)
            payload["message"] = f"实时更新失败，暂用最近快照：{exc}"
            return payload
        raise HTTPException(status_code=502, detail=f"一致预期更新失败：{exc}") from exc


@router.post("/{chain_id}/companies/{company_id}/expectation-analysis/refresh")
def refresh_company_expectation_analysis(
    chain_id: int,
    company_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    company = _company_or_404(db, chain_id, company_id)
    try:
        return _snapshot_out(refresh_company_expectation(db, company))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"一致预期更新失败：{exc}") from exc

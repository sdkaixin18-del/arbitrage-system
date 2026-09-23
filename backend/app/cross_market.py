from __future__ import annotations

import math
import threading
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from statistics import fmean, pstdev
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import IndustryChain, IndustryChainCompany, IndustryTrendNode, StockDailyBar


YAHOO_CACHE_TTL = timedelta(minutes=20)
GLOBAL_DEMAND_PROXIES = (
    {"symbol": "NVDA", "name": "NVIDIA", "market": "美股", "role": "需求温度"},
    {"symbol": "GOOGL", "name": "Alphabet", "market": "美股", "role": "需求温度"},
)
GLOBAL_NETWORK_PROXIES = (
    {"symbol": "ANET", "name": "Arista Networks", "market": "美股", "role": "网络传导"},
    {"symbol": "AVGO", "name": "Broadcom", "market": "美股", "role": "网络传导"},
    {"symbol": "MRVL", "name": "Marvell", "market": "美股", "role": "网络传导"},
)
DIRECT_DEFAULTS = {
    "optical": (
        {"symbol": "COHR", "name": "Coherent", "market": "美股", "role": "直接产业链"},
        {"symbol": "LITE", "name": "Lumentum", "market": "美股", "role": "直接产业链"},
        {"symbol": "FN", "name": "Fabrinet", "market": "美股", "role": "直接产业链"},
        {"symbol": "CIEN", "name": "Ciena", "market": "美股", "role": "直接产业链"},
    ),
    "pcb": (
        {"symbol": "TTMI", "name": "TTM Technologies", "market": "美股", "role": "直接产业链"},
    ),
}
DEMAND_SYMBOLS = {"NVDA", "GOOGL", "MSFT", "AMZN", "META"}
NETWORK_SYMBOLS = {"ANET", "AVGO", "MRVL", "CSCO"}
SYMBOL_OVERRIDES = {
    ("韩国", "138080"): "138080.KQ",
    ("其他", "IQE"): "IQE.L",
}

_yahoo_cache_lock = threading.Lock()
_yahoo_cache: dict[str, tuple[datetime, list[tuple[date, float]]]] = {}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _chain_kind(name: str) -> str:
    upper = name.upper()
    if "PCB" in upper or "覆铜板" in name:
        return "pcb"
    if any(token in name for token in ("光通信", "光互联", "光模块")):
        return "optical"
    return "other"


def _is_ai_chain(row: IndustryChain) -> bool:
    text = " ".join(filter(None, (row.name, row.summary, row.investment_logic, row.change_summary, row.why_now))).lower()
    return any(token in text for token in ("ai", "算力", "数据中心", "光通信", "光互联", "光模块", "pcb", "服务器", "高速交换"))


def yahoo_symbol_for_company(row: IndustryChainCompany) -> str | None:
    code = (row.code or "").strip().upper()
    market = row.market or "A股"
    if not code or market == "A股":
        return None
    override = SYMBOL_OVERRIDES.get((market, code))
    if override:
        return override
    if market == "美股":
        return code if code.replace("-", "").replace(".", "").isalnum() else None
    if market == "台湾" and code.isdigit():
        return f"{code}.TW"
    if market == "日本" and code.isdigit():
        return f"{code}.T"
    if market == "韩国" and code.isdigit():
        return f"{code}.KS"
    return None


def _role_for_symbol(symbol: str) -> str:
    base = symbol.split(".", 1)[0]
    if base in DEMAND_SYMBOLS:
        return "需求温度"
    if base in NETWORK_SYMBOLS:
        return "网络传导"
    return "直接产业链"


def fetch_yahoo_daily_series(symbol: str, force_refresh: bool = False) -> list[tuple[date, float]]:
    now = datetime.now(timezone.utc)
    with _yahoo_cache_lock:
        cached = _yahoo_cache.get(symbol)
    if not force_refresh and cached and now - cached[0] < YAHOO_CACHE_TTL:
        return cached[1]

    encoded = quote(symbol, safe=".-")
    payload: Any = None
    last_error: Exception | None = None
    with httpx.Client(
        timeout=12.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    ) as client:
        for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
            url = f"https://{host}/v8/finance/chart/{encoded}?interval=1d&range=1y&events=history&crumb="
            try:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
    if payload is None:
        if "." not in symbol:
            points = _fetch_nasdaq_daily_series(symbol)
            with _yahoo_cache_lock:
                _yahoo_cache[symbol] = (now, points)
            return points
        raise last_error or ValueError(f"{symbol}历史行情请求失败")
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
    if not result:
        raise ValueError(f"{symbol}未返回历史行情")
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
    adjclose_rows = indicators.get("adjclose") if isinstance(indicators, dict) else None
    quote_rows = indicators.get("quote") if isinstance(indicators, dict) else None
    adjusted = adjclose_rows[0].get("adjclose") if isinstance(adjclose_rows, list) and adjclose_rows and isinstance(adjclose_rows[0], dict) else None
    closes = quote_rows[0].get("close") if isinstance(quote_rows, list) and quote_rows and isinstance(quote_rows[0], dict) else None
    prices = adjusted if isinstance(adjusted, list) and len(adjusted) == len(timestamps) else closes
    if not isinstance(prices, list):
        raise ValueError(f"{symbol}历史行情缺少收盘价")
    points: list[tuple[date, float]] = []
    for timestamp, raw_price in zip(timestamps, prices):
        price_value = _float(raw_price)
        if price_value is None or price_value <= 0:
            continue
        try:
            trade_date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
        except (TypeError, ValueError, OSError):
            continue
        if points and points[-1][0] == trade_date:
            points[-1] = (trade_date, price_value)
        else:
            points.append((trade_date, price_value))
    if len(points) < 22:
        raise ValueError(f"{symbol}有效历史行情不足")
    with _yahoo_cache_lock:
        _yahoo_cache[symbol] = (now, points)
    return points


def _fetch_nasdaq_daily_series(symbol: str) -> list[tuple[date, float]]:
    start = date.today() - timedelta(days=400)
    url = (
        f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='-')}/historical"
        f"?assetclass=stocks&fromdate={start.isoformat()}&todate={date.today().isoformat()}&limit=5000"
    )
    with httpx.Client(
        timeout=18.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    table = data.get("tradesTable") if isinstance(data, dict) else None
    rows = table.get("rows") if isinstance(table, dict) else None
    points: list[tuple[date, float]] = []
    for row in reversed(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        try:
            trade_date = datetime.strptime(str(row.get("date") or ""), "%m/%d/%Y").date()
        except ValueError:
            continue
        close = _float(str(row.get("close") or "").replace("$", "").replace(",", ""))
        if close is not None and close > 0:
            points.append((trade_date, close))
    if len(points) < 22:
        raise ValueError(f"{symbol} Nasdaq历史行情不足")
    return points


def _daily_returns(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    rows: list[tuple[date, float]] = []
    for index in range(1, len(points)):
        previous = points[index - 1][1]
        current = points[index][1]
        if previous > 0:
            rows.append((points[index][0], current / previous - 1))
    return rows


def _period_return(points: list[tuple[date, float]], period: int) -> float | None:
    if len(points) <= period:
        return None
    base = points[-period - 1][1]
    return (points[-1][1] / base - 1) * 100 if base > 0 else None


def _ticker_summary(spec: dict[str, Any], points: list[tuple[date, float]] | None) -> dict[str, Any]:
    return {
        **spec,
        "available": bool(points),
        "trade_date": points[-1][0] if points else None,
        "return_1d_pct": round(_period_return(points, 1), 2) if points and _period_return(points, 1) is not None else None,
        "return_5d_pct": round(_period_return(points, 5), 2) if points and _period_return(points, 5) is not None else None,
        "return_20d_pct": round(_period_return(points, 20), 2) if points and _period_return(points, 20) is not None else None,
    }


def _basket_returns(series_by_symbol: dict[str, list[tuple[date, float]]]) -> list[tuple[date, float]]:
    by_date: dict[date, list[float]] = {}
    for points in series_by_symbol.values():
        for trade_date, value in _daily_returns(points):
            by_date.setdefault(trade_date, []).append(value)
    return [(trade_date, fmean(values)) for trade_date, values in sorted(by_date.items()) if values]


def _compounded_return(rows: list[tuple[date, float]], period: int) -> float | None:
    if len(rows) < period:
        return None
    value = 1.0
    for _, daily_return in rows[-period:]:
        value *= 1 + daily_return
    return (value - 1) * 100


def _trend_state(return_5d: float | None, return_20d: float | None) -> str:
    if return_5d is None or return_20d is None:
        return "数据不足"
    if return_5d > 0 and return_20d > 0:
        return "增强"
    if return_5d < 0 and return_20d < 0:
        return "转弱"
    return "分化"


def _basket_summary(series_by_symbol: dict[str, list[tuple[date, float]]]) -> tuple[dict[str, Any], list[tuple[date, float]]]:
    rows = _basket_returns(series_by_symbol)
    return_1d = _compounded_return(rows, 1)
    return_5d = _compounded_return(rows, 5)
    return_20d = _compounded_return(rows, 20)
    ticker_5d = [_period_return(points, 5) for points in series_by_symbol.values()]
    ticker_5d = [value for value in ticker_5d if value is not None]
    summary = {
        "status": _trend_state(return_5d, return_20d),
        "return_1d_pct": round(return_1d, 2) if return_1d is not None else None,
        "return_5d_pct": round(return_5d, 2) if return_5d is not None else None,
        "return_20d_pct": round(return_20d, 2) if return_20d is not None else None,
        "breadth_5d_pct": round(sum(value > 0 for value in ticker_5d) / len(ticker_5d) * 100, 1) if ticker_5d else None,
        "sample_count": len(series_by_symbol),
        "trade_date": rows[-1][0] if rows else None,
    }
    return summary, rows


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 12:
        return None
    left_mean = fmean(item[0] for item in pairs)
    right_mean = fmean(item[1] for item in pairs)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_sum = sum((left - left_mean) ** 2 for left, _ in pairs)
    right_sum = sum((right - right_mean) ** 2 for _, right in pairs)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator > 0 else None


def _align_previous_overseas(
    overseas_rows: list[tuple[date, float]],
    a_share_rows: list[tuple[date, float]],
) -> list[tuple[date, float, float]]:
    overseas_dates = [row[0] for row in overseas_rows]
    overseas_map = dict(overseas_rows)
    aligned: list[tuple[date, float, float]] = []
    last_used: date | None = None
    for a_date, a_return in a_share_rows:
        position = bisect_left(overseas_dates, a_date) - 1
        if position < 0:
            continue
        overseas_date = overseas_dates[position]
        if overseas_date == last_used or (a_date - overseas_date).days > 4:
            continue
        last_used = overseas_date
        aligned.append((a_date, a_return, overseas_map[overseas_date]))
    return aligned


def cross_market_relation(
    overseas_rows: list[tuple[date, float]],
    a_share_rows: list[tuple[date, float]],
) -> dict[str, Any]:
    aligned = _align_previous_overseas(overseas_rows, a_share_rows)
    recent = aligned[-60:]
    correlation = _pearson([(a_return, overseas_return) for _, a_return, overseas_return in recent])
    relation_state = "数据不足"
    if correlation is not None:
        relation_state = "有效" if correlation >= 0.3 else "减弱" if correlation >= 0.1 else "失效"

    divergence_z: float | None = None
    beta: float | None = None
    residual_20d_pct: float | None = None
    sample = aligned[-140:]
    if len(sample) >= 40:
        overseas_values = [row[2] for row in sample]
        a_values = [row[1] for row in sample]
        overseas_mean = fmean(overseas_values)
        a_mean = fmean(a_values)
        variance = sum((value - overseas_mean) ** 2 for value in overseas_values)
        if variance > 0:
            beta = sum((a_value - a_mean) * (overseas_value - overseas_mean) for a_value, overseas_value in zip(a_values, overseas_values)) / variance
            residuals = [a_value - beta * overseas_value for a_value, overseas_value in zip(a_values, overseas_values)]
            rolling = [sum(residuals[index - 20:index]) * 100 for index in range(20, len(residuals) + 1)]
            if rolling:
                residual_20d_pct = rolling[-1]
                deviation = pstdev(rolling)
                if deviation > 1e-9:
                    divergence_z = (rolling[-1] - fmean(rolling)) / deviation
    if divergence_z is None:
        divergence_state = "数据不足"
    elif divergence_z <= -1:
        divergence_state = "A股落后"
    elif divergence_z >= 1:
        divergence_state = "A股领先"
    else:
        divergence_state = "基本同步"
    return {
        "correlation_60d": round(correlation, 3) if correlation is not None else None,
        "relation_state": relation_state,
        "beta_60d": round(beta, 3) if beta is not None else None,
        "residual_20d_pct": round(residual_20d_pct, 2) if residual_20d_pct is not None else None,
        "divergence_z": round(divergence_z, 2) if divergence_z is not None else None,
        "divergence_state": divergence_state,
        "aligned_samples": len(recent),
    }


def _action_for_sector(direct_state: str, a_state: str, divergence_state: str) -> tuple[str, str, str]:
    if direct_state == "增强" and a_state == "增强":
        return "跨市场共振", "positive", "海外直接产业链与A股同时增强；下一步只检查订单、业绩等硬证据，不直接据此买入。"
    if direct_state == "增强" and a_state != "增强":
        return "偏离观察", "watch", "海外直接产业链走强而A股尚未形成自身表达；等待放量、首阳或板块共振。"
    if direct_state == "转弱" and a_state == "转弱":
        return "风险收缩", "negative", "海外直接链与A股同步转弱，先降低预期并检查产业证伪。"
    if direct_state == "转弱" and a_state == "增强":
        return "检查独立催化", "watch", "A股强于海外直接链，需要确认是否存在国内订单、政策或替代逻辑。"
    if divergence_state == "A股落后":
        return "偏离观察", "watch", "当前偏离只产生观察线索；没有A股自身表达前，不按补涨处理。"
    return "继续观察", "neutral", "跨市场趋势仍在分化，暂不据此调整正式产业判断。"


def build_cross_market_intelligence(db: Session, force_refresh: bool = False) -> dict[str, Any]:
    chains = [
        row for row in db.scalars(
            select(IndustryChain).where(
                IndustryChain.attention_level != "暂停",
                IndustryChain.status != "archived",
            )
        ).all()
        if _is_ai_chain(row)
    ]
    chains.sort(key=lambda row: (row.sort_order, row.id))
    chain_ids = [row.id for row in chains]
    if not chain_ids:
        return {
            "as_of": None,
            "source_status": "empty",
            "principle": "跨市场行情只作为盘面确认，不替代订单、业绩、客户和产品证据。",
            "global_demand": {},
            "sectors": [],
            "errors": [],
        }

    companies = list(db.scalars(select(IndustryChainCompany).where(IndustryChainCompany.chain_id.in_(chain_ids))).all())
    nodes = list(db.scalars(select(IndustryTrendNode).where(IndustryTrendNode.chain_id.in_(chain_ids)).order_by(IndustryTrendNode.sort_order, IndustryTrendNode.id)).all())
    companies_by_chain: dict[int, list[IndustryChainCompany]] = {chain_id: [] for chain_id in chain_ids}
    nodes_by_chain: dict[int, list[IndustryTrendNode]] = {chain_id: [] for chain_id in chain_ids}
    for row in companies:
        companies_by_chain[row.chain_id].append(row)
    for row in nodes:
        nodes_by_chain[row.chain_id].append(row)

    proxy_specs: dict[str, dict[str, Any]] = {}
    for spec in (*GLOBAL_DEMAND_PROXIES, *GLOBAL_NETWORK_PROXIES):
        proxy_specs[spec["symbol"]] = {**spec, "company_id": None, "node_ids": []}
    for chain in chains:
        for spec in DIRECT_DEFAULTS.get(_chain_kind(chain.name), ()):
            proxy_specs.setdefault(spec["symbol"], {**spec, "company_id": None, "node_ids": []})
        for company in companies_by_chain[chain.id]:
            symbol = yahoo_symbol_for_company(company)
            if not symbol:
                continue
            proxy_specs[symbol] = {
                "symbol": symbol,
                "name": company.name,
                "market": company.market or "其他",
                "role": _role_for_symbol(symbol),
                "company_id": company.id,
                "node_ids": list(company.node_ids_json and _json_list(company.node_ids_json) or []),
            }

    external_series: dict[str, list[tuple[date, float]]] = {}
    errors: list[str] = []
    fetch_symbols = {spec["symbol"] for spec in (*GLOBAL_DEMAND_PROXIES, *GLOBAL_NETWORK_PROXIES)}
    for chain in chains:
        fetch_symbols.update(spec["symbol"] for spec in DIRECT_DEFAULTS.get(_chain_kind(chain.name), ()))
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(fetch_symbols)))) as executor:
        futures = {executor.submit(fetch_yahoo_daily_series, symbol, force_refresh): symbol for symbol in fetch_symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                external_series[symbol] = future.result()
            except Exception as exc:
                errors.append(f"{symbol}: {str(exc)[:120]}")

    a_companies = [row for row in companies if (row.market or "A股") == "A股" and row.full_code]
    a_codes = [row.full_code for row in a_companies if row.full_code]
    local_rows = list(
        db.scalars(
            select(StockDailyBar)
            .where(
                StockDailyBar.full_code.in_(a_codes),
                StockDailyBar.trade_date >= date.today() - timedelta(days=400),
            )
            .order_by(StockDailyBar.full_code, StockDailyBar.trade_date)
        ).all()
    ) if a_codes else []
    local_series: dict[str, list[tuple[date, float]]] = {}
    for row in local_rows:
        local_series.setdefault(row.full_code, []).append((row.trade_date, float(row.close)))

    global_symbols = [spec["symbol"] for spec in (*GLOBAL_DEMAND_PROXIES, *GLOBAL_NETWORK_PROXIES)]
    global_summary, _ = _basket_summary({symbol: external_series[symbol] for symbol in global_symbols if symbol in external_series})
    sectors: list[dict[str, Any]] = []
    for chain in chains:
        chain_companies = companies_by_chain[chain.id]
        kind = _chain_kind(chain.name)
        direct_symbols = [spec["symbol"] for spec in DIRECT_DEFAULTS.get(kind, ())]
        for company in chain_companies:
            symbol = yahoo_symbol_for_company(company)
            if symbol and _role_for_symbol(symbol) == "直接产业链" and (company.market or "") == "美股" and symbol not in direct_symbols:
                direct_symbols.append(symbol)
        direct_series = {symbol: external_series[symbol] for symbol in direct_symbols if symbol in external_series}
        direct_summary, direct_returns = _basket_summary(direct_series)
        chain_a_companies = [row for row in chain_companies if (row.market or "A股") == "A股" and row.full_code]
        chain_a_series = {row.full_code: local_series[row.full_code] for row in chain_a_companies if row.full_code in local_series}
        a_summary, a_returns = _basket_summary(chain_a_series)
        relation = cross_market_relation(direct_returns, a_returns)
        action, action_tone, action_reason = _action_for_sector(direct_summary["status"], a_summary["status"], relation["divergence_state"])

        chain_proxy_symbols: list[str] = []
        for company in chain_companies:
            symbol = yahoo_symbol_for_company(company)
            if symbol and symbol not in chain_proxy_symbols:
                chain_proxy_symbols.append(symbol)
        for symbol in direct_symbols:
            if symbol not in chain_proxy_symbols:
                chain_proxy_symbols.append(symbol)
        proxy_rows = [_ticker_summary(proxy_specs[symbol], external_series.get(symbol)) for symbol in chain_proxy_symbols if symbol in proxy_specs]
        a_rows = [
            {
                "company_id": company.id,
                "name": company.name,
                "symbol": company.full_code,
                "market": "A股",
                "role": "A股表达",
                "node_ids": _json_list(company.node_ids_json),
                **{key: value for key, value in _ticker_summary({}, local_series.get(company.full_code or "")).items() if key.startswith("return_") or key in {"available", "trade_date"}},
            }
            for company in chain_a_companies
        ]
        node_mappings: list[dict[str, Any]] = []
        for node in nodes_by_chain[chain.id]:
            overseas = [row for row in proxy_rows if node.id in row.get("node_ids", [])]
            domestic = [row for row in a_rows if node.id in row.get("node_ids", [])]
            node_mappings.append({
                "node_id": node.id,
                "node_name": node.name,
                "node_type": node.node_type,
                "overseas": overseas,
                "a_share": domestic,
            })
        sectors.append({
            "id": chain.id,
            "name": chain.name,
            "global_demand": global_summary,
            "overseas_direct": direct_summary,
            "a_share": a_summary,
            **relation,
            "action": action,
            "action_tone": action_tone,
            "action_reason": action_reason,
            "proxies": {
                "demand": [_ticker_summary(spec, external_series.get(spec["symbol"])) for spec in GLOBAL_DEMAND_PROXIES],
                "network": [_ticker_summary(spec, external_series.get(spec["symbol"])) for spec in GLOBAL_NETWORK_PROXIES],
                "direct": [row for row in proxy_rows if row.get("role") == "直接产业链"],
                "a_share": a_rows,
            },
            "node_mappings": node_mappings,
        })

    dates = [
        summary.get("trade_date")
        for sector in sectors
        for summary in (sector["global_demand"], sector["overseas_direct"], sector["a_share"])
        if summary.get("trade_date")
    ]
    available_sector_count = sum(bool(row["overseas_direct"].get("sample_count") and row["a_share"].get("sample_count")) for row in sectors)
    return {
        "as_of": max(dates) if dates else None,
        "source_status": "ok" if available_sector_count == len(sectors) and not errors else "partial" if available_sector_count else "unavailable",
        "principle": "跨市场行情只作为盘面确认，不替代订单、业绩、客户和产品证据；偏离只进入观察池。",
        "global_demand": global_summary,
        "sectors": sectors,
        "errors": errors[:12],
        "method": {
            "alignment": "美股前一交易日对应A股下一交易日",
            "windows": [5, 20, 60],
            "source": "Yahoo Finance + 本地A股历史行情",
            "cache_minutes": int(YAHOO_CACHE_TTL.total_seconds() // 60),
        },
    }


def _json_list(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        import json

        raw = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result

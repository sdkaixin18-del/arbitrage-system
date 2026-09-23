"""On-demand third-party BBO history. Never used by trading or the scanner."""
from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
import math
import re
import threading
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
ORIGIN = "https://www.perpdexlist.com"
VENUES = {
    "binance": "Binance", "bitget": "Bitget", "gateio": "Gate", "bybit": "Bybit",
    "okx": "OKX", "asterdex": "Aster", "hyperliquid": "Hyperliquid", "lighter": "Lighter",
    "backpack": "Backpack", "okxdex": "OKXDEX", "pancakeswapv3": "PancakeSwap V3",
}
ALIASES = {"bn": "binance", "bg": "bitget", "gt": "gateio", "gate": "gateio", "by": "bybit", "aster": "asterdex"}
DEX_SPOT = {"okxdex", "pancakeswapv3"}
_network_slots = threading.BoundedSemaphore(2)
_series_lock = threading.Lock()
_series: OrderedDict[tuple, dict] = OrderedDict()


def _venue(value: str) -> str:
    result = ALIASES.get(value.strip().lower(), value.strip().lower())
    if result not in VENUES:
        raise HTTPException(400, "不支持此交易所的盘口历史")
    return result


def _symbol(value: str) -> str:
    result = re.sub(r"[/_\-\s]", "", value.strip().upper())
    if result.endswith("USDTSWAP"):
        result = result[:-8]
    elif result.endswith("USDT"):
        result = result[:-4]
    if not re.fullmatch(r"[A-Z0-9\u4e00-\u9fff.]{1,32}", result):
        raise HTTPException(400, "请输入有效币名或 USDT 交易对")
    return result


def _get_json(path: str, params: dict | None = None) -> dict:
    if not _network_slots.acquire(blocking=False):
        raise HTTPException(429, "盘口历史查询繁忙，请稍后重试")
    try:
        with httpx.Client(timeout=httpx.Timeout(12, connect=4), follow_redirects=True) as client:
            response = client.get(ORIGIN + path, params=params)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid payload")
        # This service can return HTTP 200 with a business error.
        if data.get("error"):
            raise HTTPException(422, "第三方尚未收录此市场的盘口历史，请核对现货／合约和交易对")
        return data
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "第三方盘口历史暂时无法读取，请稍后重试") from exc
    finally:
        _network_slots.release()


def _markets() -> list[dict]:
    from app.dex_history import _cached_read
    def read():
        data = _get_json("/api/dashboard/markets-v2")
        if not isinstance(data.get("markets"), list):
            raise HTTPException(502, "第三方市场目录格式异常")
        # Do not retain funding/account data; this page only needs market identity.
        return [{"exchange": str(r.get("exchange", "")), "symbol": str(r.get("symbol", "")),
                 "asset": str(r.get("asset", "")), "spot": r.get("spot") is True}
                for r in data["markets"] if isinstance(r, dict)]
    return _cached_read(("book-markets",), read, ttl=600)


def _resolve_market(markets: list[dict], venue: str, symbol: str, spot: bool) -> dict | None:
    # A CEX perpetual MUST NOT stand in for an absent CEX/DEX spot market.
    exchange = venue + "_spot" if spot else venue
    matches = []
    for row in markets:
        if row["exchange"] != exchange and not (spot and row["exchange"] == venue and row["spot"]):
            continue
        if not spot and (row["spot"] or row["exchange"].endswith("_spot")):
            continue
        try:
            if _symbol(row["symbol"]) == symbol:
                matches.append(row)
        except HTTPException:
            continue
    # Never silently choose among multiple instruments with one ticker.
    return matches[0] if len(matches) == 1 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def normalize_bars(bars: list, start: int, end: int, bucket: int, left_divisor: float, right_divisor: float):
    from app.dex_history import astro_symmetric_spread_pct
    points: dict[int, dict] = {}
    conflicts: set[int] = set()
    invalid = duplicates = 0
    for bar in bars:
        if not isinstance(bar, dict):
            invalid += 1
            continue
        ts = _number(bar.get("time"))
        if ts is None or ts != int(ts) or int(ts) % bucket:
            invalid += 1
            continue
        ts = int(ts)
        if ts < start or ts + bucket > end:
            continue  # Never include unfinished buckets or invent missing history.
        values = [_number((bar.get(k) or {}).get("close")) if isinstance(bar.get(k), dict) else None
                  for k in ("l_bid", "l_ask", "s_bid", "s_ask")]
        if any(v is None or v <= 0 for v in values):
            invalid += 1
            continue
        lb, la, sb, sa = values
        if lb > la or sb > sa:
            invalid += 1
            continue
        lb, la, sb, sa = lb / left_divisor, la / left_divisor, sb / right_divisor, sa / right_divisor
        opening = astro_symmetric_spread_pct(la, sb)
        closing = astro_symmetric_spread_pct(lb, sa)
        if opening is None or closing is None:
            invalid += 1
            continue
        point = {"timestamp": ts * 1000, "openSpreadPct": opening, "closeSpreadPct": closing,
                 "leftBid": lb, "leftAsk": la, "rightBid": sb, "rightAsk": sa}
        if ts in points:
            duplicates += 1
            if points[ts] != point:
                conflicts.add(ts)
        points[ts] = point
    for ts in conflicts:
        points.pop(ts, None)
    return [points[k] for k in sorted(points)], {"invalidBars": invalid, "duplicateBars": duplicates, "conflictingBars": len(conflicts)}


def _load_points(left: dict, right: dict, start: int, end: int, bucket: int, ld: float, rd: float):
    key = (left["exchange"], left["symbol"], right["exchange"], right["symbol"], bucket, ld, rd)
    with _series_lock:
        previous = _series.get(key)
    # Refresh only the tail while the cached window still covers the request.
    incremental = (previous is not None and bool(previous["points"]) and previous["start"] <= start
                   and previous["end"] >= start and time.time() - previous["fullReadAt"] < 600)
    fetch_start = max(start, previous["end"] - bucket * 2) if incremental else start
    data = _get_json("/api/arbitrage/live-pair-history", {
        "le": left["exchange"], "ls": left["symbol"], "se": right["exchange"], "ss": right["symbol"],
        "minutes": max(1, math.ceil((end - fetch_start) / 60)), "bucket": bucket, "to": end - bucket,
        "lmul": 1, "smul": 1,
    })
    if data.get("source") != "archive_15s" or not isinstance(data.get("bars"), list):
        raise HTTPException(502, "第三方未返回已验证的盘口档案格式")
    new, quality = normalize_bars(data["bars"], fetch_start, end, bucket, ld, rd)
    # Replace overlap including holes: an invalid revised bar must not keep an old value.
    kept = [p for p in previous["points"] if start * 1000 <= p["timestamp"] < fetch_start * 1000] if incremental else []
    points = kept + new
    with _series_lock:
        _series[key] = {"start": start, "end": end, "points": points,
                        "fullReadAt": previous["fullReadAt"] if incremental else time.time()}
        _series.move_to_end(key)
        while len(_series) > 16:
            _series.popitem(last=False)
    return points, {**quality, "incremental": incremental}


@router.get("/books")
def book_history(
    pair_type: str = Query(default="FF", alias="pairType"),
    left_venue: str = Query(default="aster", alias="leftVenue"),
    right_venue: str = Query(default="gate", alias="rightVenue"),
    left_symbol: str = Query(alias="leftSymbol"),
    right_symbol: str = Query(alias="rightSymbol"),
    range_hours: int = Query(default=24, alias="rangeHours"),
    left_divisor: float = Query(default=1, alias="leftDivisor", gt=0, le=1e12),
    right_divisor: float = Query(default=1, alias="rightDivisor", gt=0, le=1e12),
):
    from app.dex_history import _cached_read, SUPPORTED_RANGES
    if pair_type not in {"SF", "FF"} or range_hours not in SUPPORTED_RANGES:
        raise HTTPException(400, "请选择 SF / FF 和受支持的时间范围")
    if not all(math.isfinite(v) and 0 < v <= 1e12 for v in (left_divisor, right_divisor)):
        raise HTTPException(400, "每币换算倍数无效")
    lv, rv, ls, rs = _venue(left_venue), _venue(right_venue), _symbol(left_symbol), _symbol(right_symbol)
    if rv in DEX_SPOT or (pair_type == "FF" and lv in DEX_SPOT):
        raise HTTPException(400, "FF 两侧均须为合约；SF 右侧须为合约")
    bucket = 15 if range_hours == 4 else 60 if range_hours <= 72 else 300 if range_hours <= 168 else 900
    now = time.time()
    end = int(now // bucket) * bucket
    start = end - range_hours * 3600
    pair = {"type": pair_type, "leftVenue": lv, "rightVenue": rv, "leftLabel": VENUES[lv], "rightLabel": VENUES[rv],
            "leftSymbol": ls, "rightSymbol": rs, "leftDivisor": left_divisor, "rightDivisor": right_divisor}
    base = {"pair": pair, "source": "PERPDEXLIST", "sourceUrl": ORIGIN + "/arbitrage/grapher", "rangeHours": range_hours,
            "bucketSeconds": bucket, "requestedStart": start * 1000, "requestedEnd": end * 1000,
            "observedAt": datetime.now(UTC).isoformat(), "points": [], "sourceQuoteTimesAvailable": False}
    if lv in DEX_SPOT:
        return {**base, "status": "unsupported", "message": f"{VENUES[lv]} 暂无此来源的现货盘口历史，可切换 DEX 主池价格参考。"}
    def read():
        markets = _markets()
        left = _resolve_market(markets, lv, ls, pair_type == "SF")
        right = _resolve_market(markets, rv, rs, False)
        if left is None or right is None:
            missing = f"{VENUES[lv]} {ls} {'现货' if pair_type == 'SF' else '合约'}" if left is None else f"{VENUES[rv]} {rs} 合约"
            return {**base, "status": "unsupported", "message": f"第三方尚未收录 {missing} 的盘口历史；不会用其他市场代替。"}
        if left["exchange"] == right["exchange"] and left["symbol"] == right["symbol"]:
            raise HTTPException(400, "请选择两个不同市场")
        if not left["asset"] or left["asset"] != right["asset"]:
            raise HTTPException(422, "两腿在来源目录中的币种身份不一致，请核对交易对及每币换算")
        points, quality = _load_points(left, right, start, end, bucket, left_divisor, right_divisor)
        expected = range_hours * 3600 // bucket
        gaps = sum(max(0, (b["timestamp"] - a["timestamp"]) // (bucket * 1000) - 1) for a, b in zip(points, points[1:]))
        return {**base, "status": "ok" if points else "empty", "message": "" if points else "此区间没有已记录的盘口历史", "points": points,
                "quality": quality, "stats": {"pointCount": len(points), "expectedPointCount": expected,
                    "coveragePct": min(100, len(points) / expected * 100), "internalMissingBuckets": gaps,
                    "firstTimestamp": points[0]["timestamp"] if points else None, "lastTimestamp": points[-1]["timestamp"] if points else None},
                "definition": {"open": "200 × (右侧买价 − 左侧卖价) ÷ (右侧买价 + 左侧卖价)",
                    "close": "200 × (右侧卖价 − 左侧买价) ÷ (右侧卖价 + 左侧买价)", "priceBasis": "best_bid_ask_bucket_close"}}
    return _cached_read(("book-history", pair_type, lv, rv, ls, rs, range_hours, left_divisor, right_divisor, int(now // 30)), read, ttl=30)


def build_book_cards(cards, aliases):
    from app.astro_spread_scanner import _direct_market_symbol
    result = []
    for card in cards:
        kind = card.get("type")
        buy, sell = str(card.get("buyEx", "")).lower(), str(card.get("sellEx", "")).lower()
        if kind not in {"SF", "FF"} or ALIASES.get(buy, buy) not in VENUES or ALIASES.get(sell, sell) not in VENUES:
            continue
        if ALIASES.get(sell, sell) in DEX_SPOT or (kind == "FF" and buy in DEX_SPOT):
            continue
        symbol = str(card.get("name", "")).strip().upper()
        left, ld = _direct_market_symbol(buy, "spot" if kind == "SF" else "future", symbol, aliases)
        right, rd = _direct_market_symbol(sell, "future", symbol, aliases)
        result.append({"id": str(card.get("id", "")), "type": kind, "symbol": symbol, "buyExchange": buy, "sellExchange": sell,
                       "leftSymbol": left, "rightSymbol": right, "leftDivisor": ld, "rightDivisor": rd})
    return result

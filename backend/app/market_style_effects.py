from __future__ import annotations

import csv
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.orm import Session

from app.database import get_database_path
from app.models import (
    AStock,
    MarketStyleCacheRefreshRun,
    MarketStyleDailyBar,
    MarketStyleEffectSnapshot,
    MarketStyleThsBar,
    MarketStyleThsGroup,
    MarketStyleThsMember,
    SectorIndex,
    SectorIndexBar,
    SectorIndexMember,
    StockDailyBar,
    now_utc,
)
from app.sector_indices import sector_summary_maps

ROOT = Path("/home/example/Documents/十倍起点")
RESEARCH_CACHE = ROOT / "research" / "backtest_system" / "market_cache.sqlite"
RESEARCH_BACKTEST = ROOT / "research" / "backtest_system" / "run_sector_1000_realistic.py"
RESEARCH_TRADES = ROOT / "research" / "backtest_system" / "sector_1000_realistic_trades.csv"
THS_GROUP_LOOKBACK_DAYS = 180
THS_MEMBER_RANK_LIMIT = 180


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def pct(value: float | None) -> float:
    return float(value or 0.0) / 100.0


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def normalize_ratio(value: float, low: float, high: float) -> float:
    if high == low:
        return 0.0
    return clamp((value - low) / (high - low), 0.0, 1.0)


def first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "").replace("%", "")
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except Exception:
        return None


def parse_trade_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except Exception:
            continue
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def exchange_from_code(code: str) -> str:
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("8", "4")):
        return "BJ"
    return "SZ"


def normalize_stock_code(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def full_code_from_code(code: str) -> str:
    return f"{exchange_from_code(code)}{code}"


def ret(closes: list[float], idx: int, days: int) -> float:
    if idx < days or closes[idx - days] <= 0:
        return 0.0
    return closes[idx] / closes[idx - days] - 1


def ma(values: list[float], idx: int, days: int) -> float | None:
    if idx + 1 < days:
        return None
    return mean(values[idx - days + 1 : idx + 1])


def amount_ratio(amounts: list[float], idx: int) -> float:
    if idx + 1 < 20:
        return 1.0
    amount5 = mean(amounts[idx - 4 : idx + 1])
    amount20 = mean(amounts[idx - 19 : idx + 1])
    return amount5 / amount20 if amount20 > 0 else 1.0


def heat_status(overheat_score: float) -> str:
    if overheat_score >= 70:
        return "过热"
    if overheat_score >= 35:
        return "合理"
    return "低温"


def direction_status(effect_score: float, overheat_score: float) -> str:
    if effect_score >= 60 and overheat_score >= 70:
        return "强但过热"
    if effect_score >= 75:
        return "强赚钱效应"
    if effect_score >= 60:
        return "可参与"
    if effect_score >= 45:
        return "轮动观察"
    return "弱赚钱/亏钱效应"


def market_action(status: str, risk_flags: list[str]) -> str:
    if status == "强赚钱效应":
        return "允许围绕主线进攻，优先选强方向里的低过热承接股"
    if status == "可参与":
        return "可以参与，但仓位跟随广度和主线确认逐步提高"
    if status == "轮动观察":
        return "以观察和小仓试错为主，避免把局部强势当成全面行情"
    if "局部抱团" in risk_flags:
        return "强股不少但赚钱面偏窄，追高要降级处理"
    return "控制仓位，优先等待广度修复和亏钱面收敛"


def score_direction_from_bars(
    bars: list[Any],
    breadth_ratio: float = 0.0,
    new_high_ratio: float = 0.0,
    core_support_ratio: float = 0.0,
) -> dict[str, Any]:
    if len(bars) < 21:
        return {
            "effect_score": 0.0,
            "overheat_score": 0.0,
            "status": "弱赚钱/亏钱效应",
            "heat_status": "低温",
            "components": {},
            "breadth_score": 0.0,
            "sentiment_score": 0.0,
            "leadership_score": 0.0,
            "risk_score": 0.0,
            "persistence_score": 0.0,
            "market_state": "等待数据",
            "score_reason": "方向指数历史不足，暂不参与赚钱效应判断。",
            "risk_flags": ["历史不足"],
        }
    closes = [float(row.close) for row in bars]
    amounts = [float(row.amount or 0) for row in bars]
    idx = len(bars) - 1
    r5 = ret(closes, idx, 5)
    r20 = ret(closes, idx, 20)
    ratio = amount_ratio(amounts, idx)
    high60 = max(closes[max(0, idx - 59) : idx + 1])
    ma20 = ma(closes, idx, 20) or closes[idx]
    ma60 = ma(closes, idx, 60) or ma20
    pct_today = pct(getattr(bars[idx], "change_pct", None))

    distance_ma20 = closes[idx] / ma20 - 1 if ma20 else 0.0
    distance_ma60 = closes[idx] / ma60 - 1 if ma60 else 0.0
    high_pressure = closes[idx] / high60 if high60 > 0 else 0.0
    overheat_score = clamp(
        max(r5 - 0.20, 0) * 130
        + max(r20 - 0.45, 0) * 85
        + max(distance_ma20 - 0.15, 0) * 120
        + max(high_pressure - 0.97, 0) * 150
        + max(pct_today - 0.08, 0) * 150
        + max(ratio - 2.5, 0) * 8
    )
    trend_score = clamp(
        48
        + r20 * 115
        + r5 * 140
        + distance_ma20 * 65
        + distance_ma60 * 25,
        0,
        100,
    )
    breadth_score = clamp(breadth_ratio * 70 + new_high_ratio * 20 + core_support_ratio * 10)
    amount_score = clamp(46 + (ratio - 1) * 24)
    persistence_score = clamp(
        42
        + sum(1 for value in [pct(getattr(row, "change_pct", None)) for row in bars[-5:]] if value > 0) * 8
        + max(r5, 0) * 80
        + (12 if closes[idx] >= ma20 else 0)
    )
    risk_score = clamp(overheat_score * 0.55 + max(-r5, 0) * 140 + max(0.35 - breadth_ratio, 0) * 70)
    sentiment_score = clamp(45 + pct_today * 220 + max(ratio - 1, 0) * 18 - max(-pct_today, 0) * 160)
    effect_score = clamp(
        trend_score * 0.28
        + breadth_score * 0.30
        + amount_score * 0.17
        + sentiment_score * 0.10
        + persistence_score * 0.10
        + 5
        - risk_score * 0.10
    )
    risk_flags: list[str] = []
    if overheat_score >= 70:
        risk_flags.append("强但过热")
    if effect_score >= 60 and breadth_ratio < 0.35:
        risk_flags.append("局部抱团")
    if pct_today > 0.01 and breadth_ratio < 0.35:
        risk_flags.append("指数强、个股弱")
    if risk_score >= 65:
        risk_flags.append("回撤风险高")
    state = direction_status(effect_score, overheat_score)
    if risk_flags and state == "强赚钱效应":
        state = risk_flags[0]
    reason = (
        f"趋势 {trend_score:.0f}，扩散 {breadth_score:.0f}，成交额 {amount_score:.0f}，"
        f"风险 {risk_score:.0f}；成分上涨率 {breadth_ratio:.0%}。"
    )
    return {
        "effect_score": round(effect_score, 2),
        "overheat_score": round(overheat_score, 2),
        "status": state,
        "heat_status": heat_status(overheat_score),
        "components": {
            "return_20d": round(r20 * 100, 2),
            "return_5d": round(r5 * 100, 2),
            "amount_ratio": round(ratio, 2),
            "breadth_ratio": round(breadth_ratio, 3),
            "new_high_ratio": round(new_high_ratio, 3),
            "core_support_ratio": round(core_support_ratio, 3),
            "distance_ma20": round(distance_ma20 * 100, 2),
            "distance_ma60": round(distance_ma60 * 100, 2),
            "trend_score": round(trend_score, 2),
            "amount_score": round(amount_score, 2),
        },
        "breadth_score": round(breadth_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "leadership_score": round(trend_score, 2),
        "risk_score": round(risk_score, 2),
        "persistence_score": round(persistence_score, 2),
        "market_state": state,
        "score_reason": reason,
        "risk_flags": risk_flags,
    }


def candidate_status(strength_score: float, overheat_score: float, signal_score: float) -> str:
    if strength_score < 45:
        return "走弱降级"
    if overheat_score >= 70 or signal_score > 120:
        return "过热回避"
    if 50 <= signal_score <= 120 and strength_score >= 50:
        return "合理买点"
    return "可观察"


def candidate_sort_key(item: dict[str, Any]) -> tuple[int, float, float]:
    status_rank = {
        "合理买点": 3,
        "可观察": 2,
        "过热回避": 1,
        "走弱降级": 0,
    }.get(item.get("candidate_status"), 0)
    signal = float(item.get("signal_score") or 0)
    signal_fit = -abs(signal - 95)
    return status_rank, signal_fit, float(item.get("strength_score") or 0)


def limit_threshold(row: MarketStyleDailyBar) -> float:
    code = (row.code or row.full_code[-6:]).strip()
    if code.startswith(("30", "68")):
        return 19.5
    if code.startswith(("8", "4")):
        return 29.0
    return 9.5


def index_at_trade_date(bars: list[MarketStyleDailyBar], target_date: date) -> int | None:
    for idx in range(len(bars) - 1, -1, -1):
        row_date = bars[idx].trade_date
        if row_date == target_date:
            return idx
        if row_date < target_date:
            return None
    return None


def market_breadth_snapshot(histories: dict[str, list[MarketStyleDailyBar]], target_date: date | None) -> dict[str, Any]:
    if target_date is None:
        return {
            "trade_date": None,
            "total_count": 0,
            "breadth_score": 0.0,
            "sentiment_score": 0.0,
            "risk_score": 100.0,
            "base_effect_score": 0.0,
            "components": {},
        }
    pct_values: list[float] = []
    ma5_flags: list[bool] = []
    ma20_flags: list[bool] = []
    ma60_flags: list[bool] = []
    new_high_flags: list[bool] = []
    rising = falling = limit_up = limit_down = large_up = large_down = 0
    for bars in histories.values():
        idx = index_at_trade_date(bars, target_date)
        if idx is None:
            continue
        row = bars[idx]
        change = float(row.change_pct or 0.0)
        pct_values.append(change)
        if change > 0:
            rising += 1
        elif change < 0:
            falling += 1
        if change >= 5:
            large_up += 1
        if change <= -5:
            large_down += 1
        threshold = limit_threshold(row)
        if change >= threshold:
            limit_up += 1
        if change <= -threshold:
            limit_down += 1
        closes = [float(item.close) for item in bars[: idx + 1]]
        close = closes[-1]
        if idx >= 4:
            ma5_flags.append(close >= mean(closes[-5:]))
        if idx >= 19:
            ma20_flags.append(close >= mean(closes[-20:]))
        if idx >= 59:
            ma60_flags.append(close >= mean(closes[-60:]))
            new_high_flags.append(close >= max(closes[-60:]) * 0.995)

    total = len(pct_values)
    if total == 0:
        return {
            "trade_date": target_date,
            "total_count": 0,
            "breadth_score": 0.0,
            "sentiment_score": 0.0,
            "risk_score": 100.0,
            "base_effect_score": 0.0,
            "components": {},
        }
    rising_ratio = rising / total
    falling_ratio = falling / total
    large_up_ratio = large_up / total
    large_down_ratio = large_down / total
    ma5_ratio = sum(ma5_flags) / len(ma5_flags) if ma5_flags else 0.0
    ma20_ratio = sum(ma20_flags) / len(ma20_flags) if ma20_flags else 0.0
    ma60_ratio = sum(ma60_flags) / len(ma60_flags) if ma60_flags else 0.0
    new_high_ratio = sum(new_high_flags) / len(new_high_flags) if new_high_flags else 0.0
    avg_pct = mean(pct_values)
    median_pct = median(pct_values)

    breadth_score = clamp(
        rising_ratio * 32
        + ma20_ratio * 30
        + ma60_ratio * 18
        + ma5_ratio * 12
        + normalize_ratio(median_pct, -2.5, 2.5) * 8
    )
    sentiment_score = clamp(
        min(limit_up / 90, 1) * 24
        + normalize_ratio(limit_up - limit_down, -25, 80) * 20
        + min(large_up_ratio / 0.08, 1) * 22
        + (1 - min(large_down_ratio / 0.12, 1)) * 18
        + rising_ratio * 16
    )
    risk_score = clamp(
        falling_ratio * 32
        + min(large_down_ratio / 0.12, 1) * 28
        + min(limit_down / 50, 1) * 18
        + normalize_ratio(-median_pct, 0, 3) * 15
        + max(0.35 - rising_ratio, 0) * 25
    )
    base_effect_score = clamp(breadth_score * 0.55 + sentiment_score * 0.25 + (100 - risk_score) * 0.20)
    return {
        "trade_date": target_date,
        "total_count": total,
        "rising_count": rising,
        "falling_count": falling,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "large_up_count": large_up,
        "large_down_count": large_down,
        "avg_pct": round(avg_pct, 4),
        "median_pct": round(median_pct, 4),
        "breadth_score": round(breadth_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "risk_score": round(risk_score, 2),
        "base_effect_score": round(base_effect_score, 2),
        "components": {
            "total_count": total,
            "rising_count": rising,
            "falling_count": falling,
            "rising_ratio": round(rising_ratio, 4),
            "falling_ratio": round(falling_ratio, 4),
            "ma5_ratio": round(ma5_ratio, 4),
            "ma20_ratio": round(ma20_ratio, 4),
            "ma60_ratio": round(ma60_ratio, 4),
            "new_high_ratio": round(new_high_ratio, 4),
            "large_up_ratio": round(large_up_ratio, 4),
            "large_down_ratio": round(large_down_ratio, 4),
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "avg_pct": round(avg_pct, 4),
            "median_pct": round(median_pct, 4),
        },
    }


def market_breadth_snapshot_from_rows(rows: list[dict[str, Any]], target_date: date | None) -> dict[str, Any]:
    if not rows:
        return {
            "trade_date": target_date,
            "total_count": 0,
            "breadth_score": 0.0,
            "sentiment_score": 0.0,
            "risk_score": 100.0,
            "base_effect_score": 0.0,
            "components": {},
        }
    pct_values: list[float] = []
    ma5_flags: list[bool] = []
    ma20_flags: list[bool] = []
    ma60_flags: list[bool] = []
    new_high_flags: list[bool] = []
    rising = falling = limit_up = limit_down = large_up = large_down = 0
    for row in rows:
        change = float(row.get("change_pct") or 0.0)
        pct_values.append(change)
        if change > 0:
            rising += 1
        elif change < 0:
            falling += 1
        if change >= 5:
            large_up += 1
        if change <= -5:
            large_down += 1
        code = str(row.get("code") or "")
        if code.startswith(("30", "68")):
            threshold = 19.5
        elif code.startswith(("8", "4")):
            threshold = 29.0
        else:
            threshold = 9.5
        if change >= threshold:
            limit_up += 1
        if change <= -threshold:
            limit_down += 1
        close = float(row.get("close") or 0.0)
        ma5_value = row.get("ma5")
        ma20_value = row.get("ma20")
        ma60_value = row.get("ma60")
        high60 = row.get("high60")
        if ma5_value:
            ma5_flags.append(close >= float(ma5_value))
        if ma20_value:
            ma20_flags.append(close >= float(ma20_value))
        if ma60_value:
            ma60_flags.append(close >= float(ma60_value))
        if high60:
            new_high_flags.append(close >= float(high60) * 0.995)

    total = len(pct_values)
    rising_ratio = rising / total
    falling_ratio = falling / total
    large_up_ratio = large_up / total
    large_down_ratio = large_down / total
    ma5_ratio = sum(ma5_flags) / len(ma5_flags) if ma5_flags else 0.0
    ma20_ratio = sum(ma20_flags) / len(ma20_flags) if ma20_flags else 0.0
    ma60_ratio = sum(ma60_flags) / len(ma60_flags) if ma60_flags else 0.0
    new_high_ratio = sum(new_high_flags) / len(new_high_flags) if new_high_flags else 0.0
    avg_pct = mean(pct_values)
    median_pct = median(pct_values)
    breadth_score = clamp(
        rising_ratio * 32
        + ma20_ratio * 30
        + ma60_ratio * 18
        + ma5_ratio * 12
        + normalize_ratio(median_pct, -2.5, 2.5) * 8
    )
    sentiment_score = clamp(
        min(limit_up / 90, 1) * 24
        + normalize_ratio(limit_up - limit_down, -25, 80) * 20
        + min(large_up_ratio / 0.08, 1) * 22
        + (1 - min(large_down_ratio / 0.12, 1)) * 18
        + rising_ratio * 16
    )
    risk_score = clamp(
        falling_ratio * 32
        + min(large_down_ratio / 0.12, 1) * 28
        + min(limit_down / 50, 1) * 18
        + normalize_ratio(-median_pct, 0, 3) * 15
        + max(0.35 - rising_ratio, 0) * 25
    )
    base_effect_score = clamp(breadth_score * 0.55 + sentiment_score * 0.25 + (100 - risk_score) * 0.20)
    return {
        "trade_date": target_date,
        "total_count": total,
        "rising_count": rising,
        "falling_count": falling,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "large_up_count": large_up,
        "large_down_count": large_down,
        "avg_pct": round(avg_pct, 4),
        "median_pct": round(median_pct, 4),
        "breadth_score": round(breadth_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "risk_score": round(risk_score, 2),
        "base_effect_score": round(base_effect_score, 2),
        "components": {
            "total_count": total,
            "rising_count": rising,
            "falling_count": falling,
            "rising_ratio": round(rising_ratio, 4),
            "falling_ratio": round(falling_ratio, 4),
            "ma5_ratio": round(ma5_ratio, 4),
            "ma20_ratio": round(ma20_ratio, 4),
            "ma60_ratio": round(ma60_ratio, 4),
            "new_high_ratio": round(new_high_ratio, 4),
            "large_up_ratio": round(large_up_ratio, 4),
            "large_down_ratio": round(large_down_ratio, 4),
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "avg_pct": round(avg_pct, 4),
            "median_pct": round(median_pct, 4),
        },
    }


def load_market_breadth_rows(db: Session, target_date: date) -> list[dict[str, Any]]:
    start_date = target_date - timedelta(days=150)
    rows = db.execute(
        text(
            """
            with ranked as (
                select
                    full_code,
                    code,
                    trade_date,
                    close,
                    change_pct,
                    row_number() over (partition by full_code order by trade_date desc) as rn
                from market_style_daily_bars
                where trade_date <= :target_date and trade_date >= :start_date
            ),
            latest as (
                select full_code, code, close, change_pct
                from ranked
                where rn = 1 and trade_date = :target_date
            ),
            agg as (
                select
                    full_code,
                    avg(case when rn <= 5 then close end) as ma5,
                    avg(case when rn <= 20 then close end) as ma20,
                    avg(case when rn <= 60 then close end) as ma60,
                    max(case when rn <= 60 then close end) as high60
                from ranked
                where rn <= 60
                group by full_code
            )
            select
                latest.full_code,
                latest.code,
                latest.close,
                latest.change_pct,
                agg.ma5,
                agg.ma20,
                agg.ma60,
                agg.high60
            from latest
            left join agg on agg.full_code = latest.full_code
            """
        ),
        {"target_date": target_date, "start_date": start_date},
    ).mappings()
    return [dict(row) for row in rows]


def load_stock_breadth_rows(db: Session, target_date: date) -> list[dict[str, Any]]:
    start_date = target_date - timedelta(days=150)
    rows = db.execute(
        text(
            """
            with ranked as (
                select
                    full_code,
                    code,
                    trade_date,
                    close,
                    change_pct,
                    row_number() over (partition by full_code order by trade_date desc) as rn
                from stock_daily_bars
                where trade_date <= :target_date and trade_date >= :start_date
            ),
            latest as (
                select full_code, code, close, change_pct
                from ranked
                where rn = 1 and trade_date = :target_date
            ),
            agg as (
                select
                    full_code,
                    avg(case when rn <= 5 then close end) as ma5,
                    avg(case when rn <= 20 then close end) as ma20,
                    avg(case when rn <= 60 then close end) as ma60,
                    max(case when rn <= 60 then close end) as high60
                from ranked
                where rn <= 60
                group by full_code
            )
            select
                latest.full_code,
                latest.code,
                latest.close,
                latest.change_pct,
                agg.ma5,
                agg.ma20,
                agg.ma60,
                agg.high60
            from latest
            left join agg on agg.full_code = latest.full_code
            """
        ),
        {"target_date": target_date, "start_date": start_date},
    ).mappings()
    return [dict(row) for row in rows]


def market_context_from_snapshot(row: MarketStyleEffectSnapshot) -> dict[str, Any]:
    components = json.loads(row.components_json or "{}")
    return {
        "trade_date": row.trade_date,
        "total_count": int(components.get("total_count") or 0),
        "breadth_score": row.breadth_score,
        "sentiment_score": row.sentiment_score,
        "risk_score": row.risk_score,
        "base_effect_score": row.effect_score,
        "components": components,
    }


def upsert_market_breadth_snapshot(db: Session, context: dict[str, Any]) -> None:
    trade_date = context.get("trade_date")
    if not trade_date:
        return
    row = db.scalar(
        select(MarketStyleEffectSnapshot).where(
            MarketStyleEffectSnapshot.scope_key == "market-breadth",
            MarketStyleEffectSnapshot.trade_date == trade_date,
        )
    )
    if row is None:
        row = MarketStyleEffectSnapshot(scope_key="market-breadth", trade_date=trade_date)
    row.effect_score = float(context.get("base_effect_score") or 0)
    row.breadth_score = float(context.get("breadth_score") or 0)
    row.sentiment_score = float(context.get("sentiment_score") or 0)
    row.leadership_score = 0
    row.risk_score = float(context.get("risk_score") or 0)
    row.persistence_score = float(context.get("persistence_score") or 0)
    row.market_state = "市场广度快照"
    row.components_json = json.dumps(context.get("components") or {}, ensure_ascii=False)
    row.risk_flags_json = "[]"
    row.score_reason = "全市场广度、情绪和风险的日度快照。"
    row.updated_at = now_utc()
    db.add(row)


def build_market_effect_context_from_db(db: Session) -> dict[str, Any]:
    latest_market_style_date = db.scalar(select(func.max(MarketStyleDailyBar.trade_date)))
    latest_stock_date = db.scalar(select(func.max(StockDailyBar.trade_date)))
    latest_date = max(
        [row for row in [latest_market_style_date, latest_stock_date] if row is not None],
        default=None,
    )
    if latest_date is None:
        return market_breadth_snapshot_from_rows([], None)

    def load_rows_for_date(trade_date: date) -> list[dict[str, Any]]:
        rows = load_market_breadth_rows(db, trade_date) if latest_market_style_date and trade_date <= latest_market_style_date else []
        return rows or load_stock_breadth_rows(db, trade_date)

    latest_snapshot = db.scalar(
        select(MarketStyleEffectSnapshot).where(
            MarketStyleEffectSnapshot.scope_key == "market-breadth",
            MarketStyleEffectSnapshot.trade_date == latest_date,
        )
    )
    if latest_snapshot is not None:
        latest = market_context_from_snapshot(latest_snapshot)
        snapshot_dirty = False
        if not latest.get("components"):
            latest = market_breadth_snapshot_from_rows(load_rows_for_date(latest_date), latest_date)
            upsert_market_breadth_snapshot(db, latest)
            snapshot_dirty = True
    else:
        latest = market_breadth_snapshot_from_rows(load_rows_for_date(latest_date), latest_date)
        upsert_market_breadth_snapshot(db, latest)
        snapshot_dirty = True

    snapshot_rows = list(
        db.scalars(
            select(MarketStyleEffectSnapshot)
            .where(
                MarketStyleEffectSnapshot.scope_key == "market-breadth",
                MarketStyleEffectSnapshot.trade_date <= latest_date,
            )
            .order_by(desc(MarketStyleEffectSnapshot.trade_date))
            .limit(5)
        )
    )
    contexts = [market_context_from_snapshot(row) for row in reversed(snapshot_rows)]
    if not contexts or contexts[-1].get("trade_date") != latest.get("trade_date"):
        contexts.append(latest)
    recent_scores = [float(item.get("base_effect_score") or 0) for item in contexts if item.get("total_count")]
    recent_breadth = [float(item.get("breadth_score") or 0) for item in contexts if item.get("total_count")]
    if recent_scores:
        delta = recent_scores[-1] - recent_scores[0]
        above_55 = sum(1 for value in recent_scores[-3:] if value >= 55)
        persistence_score = clamp(mean(recent_scores[-3:]) * 0.70 + max(delta, 0) * 1.2 + above_55 * 5)
    else:
        delta = 0.0
        persistence_score = 0.0
    context = dict(latest)
    context["persistence_score"] = round(persistence_score, 2)
    context["recent_effect_scores"] = [round(value, 2) for value in recent_scores]
    context["recent_breadth_scores"] = [round(value, 2) for value in recent_breadth]
    context["effect_score_delta_5d"] = round(delta, 2)
    context["_snapshot_dirty"] = snapshot_dirty
    return context


def market_trade_dates(histories: dict[str, list[MarketStyleDailyBar]], limit: int = 5) -> list[date]:
    dates: set[date] = set()
    for bars in histories.values():
        for row in bars[-12:]:
            dates.add(row.trade_date)
    return sorted(dates)[-limit:]


def build_market_effect_context(histories: dict[str, list[MarketStyleDailyBar]], latest_date: date | None) -> dict[str, Any]:
    dates = market_trade_dates(histories, 5)
    if latest_date and latest_date not in dates:
        dates.append(latest_date)
        dates = sorted(set(dates))[-5:]
    snapshots = [market_breadth_snapshot(histories, trade_date) for trade_date in dates]
    latest = snapshots[-1] if snapshots else market_breadth_snapshot(histories, latest_date)
    recent_scores = [float(item.get("base_effect_score") or 0) for item in snapshots if item.get("total_count")]
    recent_breadth = [float(item.get("breadth_score") or 0) for item in snapshots if item.get("total_count")]
    if recent_scores:
        delta = recent_scores[-1] - recent_scores[0]
        above_55 = sum(1 for value in recent_scores[-3:] if value >= 55)
        persistence_score = clamp(mean(recent_scores[-3:]) * 0.70 + max(delta, 0) * 1.2 + above_55 * 5)
    else:
        delta = 0.0
        persistence_score = 0.0
    context = dict(latest)
    context["persistence_score"] = round(persistence_score, 2)
    context["recent_effect_scores"] = [round(value, 2) for value in recent_scores]
    context["recent_breadth_scores"] = [round(value, 2) for value in recent_breadth]
    context["effect_score_delta_5d"] = round(delta, 2)
    return context


def build_leadership_score(directions: list[dict[str, Any]]) -> float:
    if not directions:
        return 0.0
    top3 = directions[:3]
    top3_avg = mean([float(item.get("effect_score") or 0) for item in top3])
    top3_breadth = mean([float(item.get("breadth_score") or 0) for item in top3])
    strong_count = sum(1 for item in directions if float(item.get("effect_score") or 0) >= 65)
    active_count = sum(int(item.get("active_candidate_count") or 0) for item in directions[:6])
    quality_ratio = sum(1 for item in directions[:8] if item.get("quality_status") == "可信") / min(len(directions), 8)
    return round(
        clamp(
            top3_avg * 0.48
            + top3_breadth * 0.22
            + min(strong_count / 5, 1) * 15
            + min(active_count / 12, 1) * 10
            + quality_ratio * 5
        ),
        2,
    )


def effect_status(effect_score: float, breadth_score: float, risk_score: float) -> str:
    if effect_score >= 75 and breadth_score >= 55 and risk_score < 55:
        return "强赚钱效应"
    if effect_score >= 60:
        return "可参与"
    if effect_score >= 45:
        return "轮动观察"
    return "弱赚钱/亏钱效应"


def build_gate(directions: list[dict[str, Any]], market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    top = directions[0] if directions else None
    top_score = float(top.get("effect_score") or 0) if top else 0.0
    top3 = directions[:3]
    top3_avg = mean([float(item.get("effect_score") or 0) for item in top3])
    strong_count = sum(1 for item in directions if float(item.get("effect_score") or 0) >= 70)
    top_heat = float(top.get("overheat_score") or 0) if top else 0.0
    if market_context is not None:
        breadth_score = float(market_context.get("breadth_score") or 0)
        sentiment_score = float(market_context.get("sentiment_score") or 0)
        risk_score = float(market_context.get("risk_score") or 100)
        persistence_score = float(market_context.get("persistence_score") or 0)
        leadership_score = build_leadership_score(directions)
        effect_score = clamp(
            breadth_score * 0.35
            + sentiment_score * 0.20
            + leadership_score * 0.25
            + persistence_score * 0.10
            + (100 - risk_score) * 0.10
        )
        components = dict(market_context.get("components") or {})
        components.update(
            {
                "breadth_score": round(breadth_score, 2),
                "sentiment_score": round(sentiment_score, 2),
                "leadership_score": round(leadership_score, 2),
                "risk_score": round(risk_score, 2),
                "persistence_score": round(persistence_score, 2),
                "effect_score_delta_5d": round(float(market_context.get("effect_score_delta_5d") or 0), 2),
            }
        )
        risk_flags: list[str] = []
        rising_ratio = float(components.get("rising_ratio") or 0)
        ma20_ratio = float(components.get("ma20_ratio") or 0)
        if sentiment_score >= 60 and (rising_ratio < 0.35 or ma20_ratio < 0.35):
            risk_flags.append("局部抱团")
        if top_heat >= 70 and effect_score >= 55:
            risk_flags.append("强但过热")
        if top and (float(top.get("latest_change_pct") or 0) > 0.8 or top_score >= 65) and rising_ratio < 0.35:
            risk_flags.append("指数强、个股弱")
        if risk_score >= 65:
            risk_flags.append("亏钱面偏大")
        status = effect_status(effect_score, breadth_score, risk_score)
        market_state = risk_flags[0] if risk_flags else status
        if not top:
            status = "等待数据"
            market_state = "等待数据"
            action = "等待缓存刷新"
            reason = "没有足够的方向数据。"
        else:
            action = market_action(status, risk_flags)
            reason = (
                f"综合分 {effect_score:.0f}：广度 {breadth_score:.0f}，情绪 {sentiment_score:.0f}，"
                f"主线 {leadership_score:.0f}，持续性 {persistence_score:.0f}，风险 {risk_score:.0f}。"
            )
        return {
            "status": status,
            "action": action,
            "reason": reason,
            "top_direction_score": round(top_score, 2),
            "top3_avg_score": round(top3_avg, 2),
            "strong_direction_count": strong_count,
            "top_overheat_score": round(top_heat, 2),
            "effect_score": round(effect_score, 2),
            "breadth_score": round(breadth_score, 2),
            "sentiment_score": round(sentiment_score, 2),
            "leadership_score": round(leadership_score, 2),
            "risk_score": round(risk_score, 2),
            "persistence_score": round(persistence_score, 2),
            "market_state": market_state,
            "score_reason": reason,
            "risk_flags": risk_flags,
            "components": components,
        }
    if not top:
        status = "等待数据"
        action = "等待缓存刷新"
        reason = "没有足够的方向数据。"
    elif top_heat >= 70:
        status = "过热降级"
        action = "不追高，等待分歧承接"
        reason = "最强方向过热，赚钱效应可能已进入一致性高位。"
    elif top_score >= 70 and top3_avg >= 60 and strong_count >= 2:
        status = "强开机"
        action = "允许核心仓进攻，卫星仓必须严格筛选"
        reason = "最强方向和前三强方向同时扩散，主线共振较强。"
    elif top_score >= 60 and top3_avg >= 50:
        status = "观察开机"
        action = "可小仓试错，等主线进一步扩散"
        reason = "最强方向较强，但共振强度还没有完全打开。"
    else:
        status = "不开机"
        action = "保持观察，避免在轮动里硬找机会"
        reason = "方向强度或扩散不足，容易只是局部轮动。"
    return {
        "status": status,
        "action": action,
        "reason": reason,
        "top_direction_score": round(top_score, 2),
        "top3_avg_score": round(top3_avg, 2),
        "strong_direction_count": strong_count,
        "top_overheat_score": round(top_heat, 2),
        "effect_score": round(top3_avg, 2),
        "breadth_score": 0,
        "sentiment_score": 0,
        "leadership_score": round(top3_avg, 2),
        "risk_score": round(top_heat, 2),
        "persistence_score": 0,
        "market_state": status,
        "score_reason": reason,
        "risk_flags": ["强但过热"] if top_heat >= 70 else [],
        "components": {},
    }


def days_since(value: datetime | None) -> int | None:
    if not value:
        return None
    current = now_utc().replace(tzinfo=None)
    observed = value.replace(tzinfo=None)
    return max((current - observed).days, 0)


def build_full_market_quality(total: int) -> dict[str, Any]:
    if total >= 3000:
        return {
            "quality_score": 95.0,
            "quality_status": "可信",
            "quality_reasons": ["全市场缓存覆盖较完整。"],
            "coverage_ratio": 1.0,
            "active_candidate_count": 0,
        }
    if total >= 1000:
        return {
            "quality_score": 75.0,
            "quality_status": "需复核",
            "quality_reasons": [f"全市场缓存当前覆盖 {total} 只，低于完整 A 股样本。"],
            "coverage_ratio": 1.0,
            "active_candidate_count": 0,
        }
    return {
        "quality_score": 45.0,
        "quality_status": "样本失真",
        "quality_reasons": [f"全市场缓存当前仅覆盖 {total} 只，赚钱效应可能失真。"],
        "coverage_ratio": 1.0,
        "active_candidate_count": 0,
    }


def build_sector_quality(
    sector: SectorIndex,
    members: list[SectorIndexMember],
    member_codes: set[str],
    histories: dict[str, list[MarketStyleDailyBar]],
    member_candidates: list[dict[str, Any]],
    bars: list[SectorIndexBar],
    latest_history_date: date | None,
) -> dict[str, Any]:
    total = len(member_codes)
    available_count = sum(1 for code in member_codes if histories.get(code))
    coverage_ratio = available_count / total if total else 0.0
    active_candidate_count = sum(
        1
        for candidate in member_candidates
        if candidate.get("candidate_status") in {"合理买点", "可观察"} and float(candidate.get("strength_score") or 0) >= 60
    )
    active_ratio = active_candidate_count / total if total else 0.0
    score = 100.0
    reasons: list[str] = []

    if total < 5:
        score -= 35
        reasons.append(f"成分股只有 {total} 只，样本太少，容易被单股波动带偏。")
    elif total < 10:
        score -= 15
        reasons.append(f"成分股只有 {total} 只，样本偏少，建议补充核心成分。")

    if coverage_ratio < 0.6:
        score -= 35
        reasons.append(f"行情覆盖率 {coverage_ratio:.0%}，板块指数和强度评分可能失真。")
    elif coverage_ratio < 0.8:
        score -= 20
        reasons.append(f"行情覆盖率 {coverage_ratio:.0%}，建议检查缺失成分。")

    latest_update = max((member.updated_at for member in members), default=sector.updated_at)
    age_days = days_since(latest_update)
    if age_days is not None and age_days > 90:
        score -= 20
        reasons.append(f"成分最近维护距今 {age_days} 天，可能跟不上当前产业链变化。")
    elif age_days is not None and age_days > 45:
        score -= 10
        reasons.append(f"成分最近维护距今 {age_days} 天，建议复核是否遗漏新强势股。")

    latest_bar_date = bars[-1].trade_date if bars else None
    if not bars:
        score -= 25
        reasons.append("板块指数 K 线为空，需要重新计算。")
    elif latest_history_date and latest_bar_date and latest_bar_date < latest_history_date:
        score -= 20
        reasons.append(f"板块指数停在 {latest_bar_date}，落后于全市场缓存 {latest_history_date}。")

    if total >= 5 and active_candidate_count == 0:
        score -= 20
        reasons.append("成分里暂时没有强势候选，可能和当前市场强势股脱节。")
    elif total >= 10 and active_ratio < 0.08:
        score -= 10
        reasons.append(f"强势候选占比仅 {active_ratio:.0%}，板块内部赚钱效应偏窄。")

    score = clamp(score)
    if score >= 80:
        status = "可信"
    elif score >= 60:
        status = "需复核"
    else:
        status = "样本失真"
    if not reasons:
        reasons.append("成分数量、行情覆盖和强势候选匹配度正常。")
    return {
        "quality_score": round(score, 2),
        "quality_status": status,
        "quality_reasons": reasons,
        "coverage_ratio": round(coverage_ratio, 4),
        "active_candidate_count": active_candidate_count,
    }


def assign_candidate_roles(candidates: list[dict[str, Any]], directions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    top_direction = directions[0] if directions else None
    top_direction_id = top_direction.get("id") if top_direction else None
    full_market_mode = top_direction_id == "FULL_MARKET"
    strong_direction_ids = {item.get("id") for item in directions[:3] if float(item.get("effect_score") or 0) >= 60}
    core_assigned = False
    role_counts = {"核心": 0, "卫星": 0, "噪音": 0, "过热": 0}
    enriched: list[dict[str, Any]] = []
    for item in candidates:
        candidate = dict(item)
        sector_id = candidate.get("sector_key", candidate.get("sector_id"))
        same_top_direction = full_market_mode or (top_direction_id is not None and sector_id == top_direction_id)
        in_strong_direction = full_market_mode or (sector_id in strong_direction_ids if sector_id is not None else False)
        status = candidate.get("candidate_status") or ""
        signal_score = float(candidate.get("signal_score") or 0)
        overheat_score = float(candidate.get("overheat_score") or 0)
        strength_score = float(candidate.get("strength_score") or 0)
        if overheat_score >= 70 or "过热" in status:
            role = "过热"
            reason = "强度够但过热，不适合作为追击对象。"
        elif same_top_direction and not core_assigned and status == "合理买点":
            role = "核心"
            reason = "属于当前最强方向，且强度、承接和过热度同时通过。"
            core_assigned = True
        elif same_top_direction and strength_score >= 60 and signal_score >= 80:
            role = "卫星"
            reason = "属于最强方向，但不是首位核心，适合小仓辅助验证扩散。"
        elif in_strong_direction and strength_score >= 65 and 80 <= signal_score <= 140:
            role = "卫星"
            reason = "属于前三强方向之一，可作为强分支卫星观察。"
        else:
            role = "噪音"
            reason = "和当前最强主线不够贴合，或强度/承接不足。"
        candidate["candidate_role"] = role
        candidate["role_reason"] = reason
        candidate.pop("sector_key", None)
        role_counts[role] = role_counts.get(role, 0) + 1
        enriched.append(candidate)
    return enriched


def score_stock(full_code: str, name: str, bars: list[MarketStyleDailyBar]) -> dict[str, Any] | None:
    if len(bars) < 80:
        return None
    closes = [float(row.close) for row in bars]
    amounts = [float(row.amount or 0) for row in bars]
    idx = len(bars) - 1
    close = closes[idx]
    high60 = max(closes[idx - 59 : idx + 1])
    low20 = min(closes[idx - 19 : idx + 1])
    ma10 = ma(closes, idx, 10) or close
    ma20 = ma(closes, idx, 20) or close
    r5 = ret(closes, idx, 5)
    r20 = ret(closes, idx, 20)
    r60 = ret(closes, idx, 60)
    ratio = amount_ratio(amounts, idx)
    drawdown = close / high60 - 1 if high60 > 0 else 0
    recovery = close / low20 - 1 if low20 > 0 else 0
    pct_today = pct(bars[idx].change_pct)
    strength_score = clamp(45 + r20 * 120 + r60 * 55 + max(ratio - 1, 0) * 8 + max(recovery, 0) * 15)
    overheat_score = clamp(
        max(r5 - 0.24, 0) * 140
        + max(close / ma20 - 1 - 0.16, 0) * 120
        + max(close / high60 - 0.985, 0) * 170
        + max(pct_today - 0.085, 0) * 160
        + max(ratio - 2.8, 0) * 8
    )
    signal_score = clamp(
        55
        + r20 * 110
        + r60 * 35
        + min(ratio, 3.0) * 7
        + recovery * 18
        + drawdown * 25
        - max(r5 - 0.28, 0) * 80,
        0,
        180,
    )
    return {
        "full_code": full_code,
        "code": bars[idx].code,
        "name": name,
        "trade_date": bars[idx].trade_date,
        "latest_close": round(close, 4),
        "change_pct": bars[idx].change_pct,
        "strength_score": round(strength_score, 2),
        "support_score": round(clamp(55 + recovery * 60 + max(ratio - 1, 0) * 10 - max(abs(drawdown) - 0.16, 0) * 80), 2),
        "overheat_score": round(overheat_score, 2),
        "signal_score": round(signal_score, 2),
        "heat_status": heat_status(overheat_score),
        "candidate_status": candidate_status(strength_score, overheat_score, signal_score),
        "return_20d": round(r20 * 100, 2),
        "return_60d": round(r60 * 100, 2),
        "amount_ratio": round(ratio, 2),
    }


def cache_status(db: Session) -> dict[str, Any]:
    row = db.execute(
        select(
            func.count(MarketStyleDailyBar.id),
            func.count(func.distinct(MarketStyleDailyBar.full_code)),
            func.min(MarketStyleDailyBar.trade_date),
            func.max(MarketStyleDailyBar.trade_date),
            func.max(MarketStyleDailyBar.fetched_at),
        )
    ).one()
    latest_run = db.scalar(select(MarketStyleCacheRefreshRun).order_by(desc(MarketStyleCacheRefreshRun.started_at)).limit(1))
    return {
        "status": "ok" if row[0] else "empty",
        "bar_count": int(row[0] or 0),
        "stock_count": int(row[1] or 0),
        "min_trade_date": row[2],
        "max_trade_date": row[3],
        "updated_at": row[4],
        "latest_run": cache_run_to_out(latest_run) if latest_run else None,
    }


def cache_run_to_out(run: MarketStyleCacheRefreshRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "message": run.message,
        "imported_count": run.imported_count,
        "failed_count": run.failed_count,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def dataframe_records(frame: Any, limit: int | None = None) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", False):
        return []
    records = frame.to_dict("records")
    return records[:limit] if limit else records


def fetch_ths_group_list(group_type: str) -> list[dict[str, str]]:
    import akshare as ak  # type: ignore

    fn = ak.stock_board_industry_name_ths if group_type == "industry" else ak.stock_board_concept_name_ths
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        frame = fn()
    groups = []
    for row in dataframe_records(frame):
        name = clean_text(first_value(row, "name", "名称", "板块名称"))
        code = clean_text(first_value(row, "code", "代码", "板块代码"))
        if name and code:
            groups.append({"group_type": group_type, "code": code, "name": name})
    return groups


def fetch_ths_group_index_rows(group: dict[str, str], start_text: str, end_text: str) -> list[dict[str, Any]]:
    import akshare as ak  # type: ignore

    fn = ak.stock_board_industry_index_ths if group["group_type"] == "industry" else ak.stock_board_concept_index_ths
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        frame = fn(symbol=group["name"], start_date=start_text, end_date=end_text)
    records = dataframe_records(frame)
    bars: list[dict[str, Any]] = []
    previous_close: float | None = None
    fetched_at = now_utc()
    for row in records:
        trade_date = parse_trade_date(first_value(row, "日期", "date"))
        open_ = parse_float(first_value(row, "开盘价", "开盘", "open"))
        high = parse_float(first_value(row, "最高价", "最高", "high"))
        low = parse_float(first_value(row, "最低价", "最低", "low"))
        close = parse_float(first_value(row, "收盘价", "收盘", "close"))
        amount = parse_float(first_value(row, "成交额", "amount"))
        if not trade_date or open_ is None or high is None or low is None or close is None:
            continue
        change_pct = (close / previous_close - 1) * 100 if previous_close and previous_close > 0 else None
        previous_close = close
        bars.append(
            {
                "group_type": group["group_type"],
                "group_code": group["code"],
                "group_name": group["name"],
                "trade_date": trade_date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "amount": amount,
                "change_pct": round(change_pct, 4) if change_pct is not None else None,
                "source": "ths",
                "fetched_at": fetched_at,
            }
        )
    return bars


def fetch_ths_rank_industry_members(group_code_by_name: dict[str, str]) -> list[dict[str, Any]]:
    import akshare as ak  # type: ignore

    rank_sources = [
        "stock_rank_ljqs_ths",
        "stock_rank_lxsz_ths",
        "stock_rank_cxg_ths",
        "stock_rank_cxfl_ths",
        "stock_rank_ljqd_ths",
        "stock_rank_lxxd_ths",
    ]
    fetched_at = now_utc()
    members: dict[tuple[str, str], dict[str, Any]] = {}
    for func_name in rank_sources:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                frame = getattr(ak, func_name)()
            for row in dataframe_records(frame, THS_MEMBER_RANK_LIMIT):
                group_name = clean_text(first_value(row, "所属行业"))
                code = normalize_stock_code(first_value(row, "股票代码", "代码"))
                name = clean_text(first_value(row, "股票简称", "名称"))
                if not group_name or not code or not name:
                    continue
                full_code = full_code_from_code(code)
                members[(group_name, full_code)] = {
                    "group_type": "industry",
                    "group_code": group_code_by_name.get(group_name),
                    "group_name": group_name,
                    "code": code,
                    "name": name,
                    "exchange": exchange_from_code(code),
                    "full_code": full_code,
                    "source": func_name,
                    "fetched_at": fetched_at,
                }
        except Exception:
            continue
    return list(members.values())


def refresh_ths_group_cache(db: Session) -> dict[str, Any]:
    start = (datetime.now().date() - timedelta(days=THS_GROUP_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = datetime.now().date().strftime("%Y%m%d")
    group_rows: list[dict[str, Any]] = []
    bar_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    fetched_at = now_utc()
    for group_type in ("industry", "concept"):
        try:
            groups = fetch_ths_group_list(group_type)
            for group in groups:
                group_rows.append({**group, "source": "ths", "fetched_at": fetched_at})
                try:
                    bar_rows.extend(fetch_ths_group_index_rows(group, start, end))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{group['name']}: {clean_text(str(exc))[:80]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{group_type}: {clean_text(str(exc))[:120]}")
    if not group_rows or not bar_rows:
        return {
            "status": "error",
            "message": "同花顺分组刷新失败，保留旧缓存。" + ("；".join(errors[:3]) if errors else ""),
            "group_count": 0,
            "bar_count": 0,
            "member_count": 0,
            "failed_count": max(len(errors), 1),
        }
    group_code_by_name = {row["name"]: row["code"] for row in group_rows if row["group_type"] == "industry"}
    member_rows = fetch_ths_rank_industry_members(group_code_by_name)
    db.execute(delete(MarketStyleThsMember))
    db.execute(delete(MarketStyleThsBar))
    db.execute(delete(MarketStyleThsGroup))
    db.execute(MarketStyleThsGroup.__table__.insert(), group_rows)
    db.execute(MarketStyleThsBar.__table__.insert(), bar_rows)
    if member_rows:
        db.execute(MarketStyleThsMember.__table__.insert().prefix_with("OR IGNORE"), member_rows)
    db.commit()
    status = "partial_error" if errors else "ok"
    return {
        "status": status,
        "message": f"同花顺分组：{len(group_rows)} 个方向，{len(bar_rows)} 条指数，{len(member_rows)} 条重点成分。"
        + (f" 部分失败：{'；'.join(errors[:3])}" if errors else ""),
        "group_count": len(group_rows),
        "bar_count": len(bar_rows),
        "member_count": len(member_rows),
        "failed_count": len(errors),
    }


def refresh_market_style_cache(db: Session) -> dict[str, Any]:
    run = MarketStyleCacheRefreshRun(status="running", message="正在刷新市场风格全市场缓存")
    db.add(run)
    db.commit()
    db.refresh(run)
    imported = 0
    failed = 0
    try:
        db.execute(delete(MarketStyleDailyBar))
        db.commit()
        imported += import_from_stock_daily_bars(db)
        imported += import_from_research_cache(db)
        ths_result = refresh_ths_group_cache(db)
        run.status = "partial_error" if ths_result["status"] == "error" else "ok"
        run.message = f"已刷新 {imported} 条全市场日线缓存。{ths_result['message']}"
        failed = int(ths_result.get("failed_count") or 0)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(MarketStyleCacheRefreshRun, run.id) or MarketStyleCacheRefreshRun(status="error")
        failed = 1
        run.status = "error"
        run.message = str(exc)[:500]
    run.imported_count = imported
    run.failed_count = failed
    run.completed_at = now_utc()
    db.add(run)
    db.commit()
    return {"status": run.status, "message": run.message, "run": cache_run_to_out(run), "cache": cache_status(db)}


def import_from_stock_daily_bars(db: Session) -> int:
    rows = list(db.scalars(select(StockDailyBar)))
    if not rows:
        return 0
    now = now_utc()
    payload = [
        {
            "full_code": row.full_code,
            "code": row.code,
            "name": row.name,
            "exchange": row.exchange,
            "trade_date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "amount": row.amount,
            "change_pct": row.change_pct,
            "source": "stock_daily_bars",
            "fetched_at": now,
        }
        for row in rows
    ]
    insert_market_style_rows(db, payload)
    db.commit()
    return len(payload)


def import_from_research_cache(db: Session) -> int:
    if not RESEARCH_CACHE.exists():
        return 0
    with sqlite3.connect(RESEARCH_CACHE) as conn:
        rows = conn.execute(
            """
            select full_code, code, name, exchange, trade_date, open, high, low, close, amount, pct
            from daily_bars
            where full_code not like 'IDX%'
            """
        ).fetchall()
    if not rows:
        return 0
    now = now_utc()
    chunk: list[dict[str, Any]] = []
    count = 0
    for row in rows:
        chunk.append(
            {
                "full_code": row[0],
                "code": row[1],
                "name": row[2],
                "exchange": row[3],
                "trade_date": date.fromisoformat(row[4]),
                "open": row[5],
                "high": row[7],
                "low": row[8],
                "close": row[6],
                "amount": row[9],
                "change_pct": row[10],
                "source": "research_cache",
                "fetched_at": now,
            }
        )
        if len(chunk) >= 5000:
            insert_market_style_rows(db, chunk)
            count += len(chunk)
            chunk = []
    if chunk:
        insert_market_style_rows(db, chunk)
        count += len(chunk)
    db.commit()
    return count


def insert_market_style_rows(db: Session, rows: list[dict[str, Any]]) -> None:
    if rows:
        db.execute(MarketStyleDailyBar.__table__.insert().prefix_with("OR IGNORE"), rows)


def load_market_histories(db: Session, limit_codes: set[str] | None = None, lookback_days: int = 180) -> dict[str, list[MarketStyleDailyBar]]:
    latest_date = db.scalar(select(func.max(MarketStyleDailyBar.trade_date)))
    start_date = latest_date - timedelta(days=lookback_days) if latest_date else None
    query = select(MarketStyleDailyBar).order_by(MarketStyleDailyBar.full_code, MarketStyleDailyBar.trade_date)
    if limit_codes:
        query = query.where(MarketStyleDailyBar.full_code.in_(limit_codes))
    if start_date:
        query = query.where(MarketStyleDailyBar.trade_date >= start_date)
    rows = db.scalars(query).all()
    grouped: dict[str, list[MarketStyleDailyBar]] = defaultdict(list)
    for row in rows:
        grouped[row.full_code].append(row)
    return grouped


def market_effect_candidate_codes(db: Session) -> set[str]:
    codes = set(db.scalars(select(MarketStyleThsMember.full_code)))
    active_sector_ids = list(db.scalars(select(SectorIndex.id).where(SectorIndex.active.is_(True))))
    if active_sector_ids:
        codes.update(
            db.scalars(
                select(SectorIndexMember.full_code).where(SectorIndexMember.sector_id.in_(active_sector_ids))
            )
        )
    return codes


def upsert_effect_snapshot(db: Session, scope_key: str, scope: dict[str, Any]) -> None:
    trade_date = scope.get("trade_date")
    gate = scope.get("gate") or {}
    if not trade_date or not gate:
        return
    row = db.scalar(
        select(MarketStyleEffectSnapshot).where(
            MarketStyleEffectSnapshot.scope_key == scope_key,
            MarketStyleEffectSnapshot.trade_date == trade_date,
        )
    )
    if row is None:
        row = MarketStyleEffectSnapshot(scope_key=scope_key, trade_date=trade_date)
    row.effect_score = float(gate.get("effect_score") or 0)
    row.breadth_score = float(gate.get("breadth_score") or 0)
    row.sentiment_score = float(gate.get("sentiment_score") or 0)
    row.leadership_score = float(gate.get("leadership_score") or 0)
    row.risk_score = float(gate.get("risk_score") or 0)
    row.persistence_score = float(gate.get("persistence_score") or 0)
    row.market_state = str(gate.get("market_state") or gate.get("status") or "等待数据")
    row.components_json = json.dumps(gate.get("components") or {}, ensure_ascii=False)
    row.risk_flags_json = json.dumps(gate.get("risk_flags") or [], ensure_ascii=False)
    row.score_reason = gate.get("score_reason") or gate.get("reason")
    row.updated_at = now_utc()
    db.add(row)


def ths_group_label(group_type: str) -> str:
    return "行业" if group_type == "industry" else "概念" if group_type == "concept" else group_type


def ths_group_quality(
    bars: list[MarketStyleThsBar],
    member_count: int,
    active_candidate_count: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    score = 100.0
    if len(bars) < 21:
        score -= 45
        reasons.append("同花顺指数历史不足，方向评分可信度偏低。")
    if member_count == 0:
        score -= 10
        reasons.append("暂未拿到该方向的重点成分，候选股归属需要复核。")
    elif active_candidate_count == 0:
        score -= 15
        reasons.append("重点成分里暂时没有强势候选，方向可能只有指数强。")
    if score >= 80:
        status = "可信"
    elif score >= 60:
        status = "需复核"
    else:
        status = "样本失真"
    if not reasons:
        reasons.append("同花顺指数和重点成分匹配正常。")
    return {
        "quality_score": round(clamp(score), 2),
        "quality_status": status,
        "quality_reasons": reasons,
        "coverage_ratio": 1.0 if member_count else 0.0,
        "active_candidate_count": active_candidate_count,
    }


def ths_group_scope(
    db: Session,
    candidates: list[dict[str, Any]],
    histories: dict[str, list[MarketStyleDailyBar]],
    latest_date: date | None,
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bar_rows = list(db.scalars(select(MarketStyleThsBar).order_by(MarketStyleThsBar.group_type, MarketStyleThsBar.group_code, MarketStyleThsBar.trade_date)))
    if not bar_rows:
        return {"name": "同花顺全市场口径", "trade_date": latest_date, "directions": [], "candidates": [], "gate": build_gate([], market_context)}

    bars_by_group: dict[tuple[str, str], list[MarketStyleThsBar]] = defaultdict(list)
    for row in bar_rows:
        bars_by_group[(row.group_type, row.group_code)].append(row)
    member_rows = list(db.scalars(select(MarketStyleThsMember)))
    members_by_group: dict[tuple[str, str], list[MarketStyleThsMember]] = defaultdict(list)
    for member in member_rows:
        key = (member.group_type, member.group_code or member.group_name)
        members_by_group[key].append(member)

    candidate_by_code = {item["full_code"]: item for item in candidates}
    directions: list[dict[str, Any]] = []
    candidate_pool: list[dict[str, Any]] = []
    for (group_type, group_code), bars in bars_by_group.items():
        if len(bars) < 21:
            continue
        group_name = bars[-1].group_name
        direction_id = f"{group_type}:{group_code}"
        members = members_by_group.get((group_type, group_code), [])
        member_codes = {member.full_code for member in members}
        member_candidates = [
            {
                **candidate_by_code[member.full_code],
                "sector_name": group_name,
                "sector_type": ths_group_label(group_type),
                "sector_key": direction_id,
            }
            for member in members
            if member.full_code in candidate_by_code
        ]
        total = len(member_codes)
        rising = 0
        new_high = 0
        for code in member_codes:
            stock_bars = histories.get(code) or []
            if not stock_bars:
                continue
            latest_bar = stock_bars[-1]
            if latest_date is None or latest_bar.trade_date == latest_date:
                rising += 1 if (latest_bar.change_pct or 0) > 0 else 0
            if len(stock_bars) >= 60:
                closes = [row.close for row in stock_bars]
                if closes[-1] >= max(closes[-60:]) * 0.98:
                    new_high += 1
        active_candidate_count = len([c for c in member_candidates if c["candidate_status"] == "合理买点"])
        scored = score_direction_from_bars(
            bars,
            breadth_ratio=rising / total if total else 0,
            new_high_ratio=new_high / total if total else 0,
            core_support_ratio=active_candidate_count / max(total, 1) if total else 0,
        )
        direction = {
            "id": direction_id,
            "name": group_name,
            "group_type": group_type,
            "member_count": total,
            "rising_member_count": rising,
            "new_high_count": new_high,
            "latest_change_pct": bars[-1].change_pct,
            **scored,
            **ths_group_quality(bars, total, active_candidate_count),
        }
        directions.append(direction)
        candidate_pool.extend(member_candidates)

    directions.sort(key=lambda item: item["effect_score"], reverse=True)
    candidate_pool.sort(key=candidate_sort_key, reverse=True)
    scoped_candidates = assign_candidate_roles(candidate_pool[:80], directions)
    if scoped_candidates and not any(item.get("candidate_role") == "核心" for item in scoped_candidates):
        for item in scoped_candidates:
            if item.get("candidate_status") == "合理买点":
                item["candidate_role"] = "核心"
                item["role_reason"] = "同花顺最强方向暂缺成分映射，按重点成分里的强度和承接临时选为核心。"
                break
    return {
        "name": "同花顺全市场口径",
        "trade_date": latest_date,
        "directions": directions,
        "candidates": scoped_candidates,
        "gate": build_gate(directions, market_context),
    }


def full_market_scope(
    candidates: list[dict[str, Any]],
    histories: dict[str, list[MarketStyleDailyBar]],
    latest_date: date | None,
    db: Session | None = None,
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if db is not None:
        ths_scope = ths_group_scope(db, candidates, histories, latest_date, market_context)
        if ths_scope["directions"]:
            return ths_scope
    return synthetic_full_market_scope(candidates, histories, latest_date, market_context)


def synthetic_full_market_scope(
    candidates: list[dict[str, Any]],
    histories: dict[str, list[MarketStyleDailyBar]],
    latest_date: date | None,
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_bars = [bars[-1] for bars in histories.values() if bars and (latest_date is None or bars[-1].trade_date == latest_date)]
    rising = sum(1 for row in latest_bars if (row.change_pct or 0) > 0)
    new_high = 0
    amount_ratios = []
    for bars in histories.values():
        if len(bars) < 60 or (latest_date is not None and bars[-1].trade_date != latest_date):
            continue
        closes = [b.close for b in bars]
        if closes[-1] >= max(closes[-60:]) * 0.98:
            new_high += 1
        amounts = [float(b.amount or 0) for b in bars]
        amount_ratios.append(amount_ratio(amounts, len(amounts) - 1))
    total = len(latest_bars)
    pseudo_bars = synthetic_market_bars(histories, latest_date)
    direction = score_direction_from_bars(
        pseudo_bars,
        breadth_ratio=rising / total if total else 0,
        new_high_ratio=new_high / total if total else 0,
        core_support_ratio=len([c for c in candidates[:50] if c["candidate_status"] == "合理买点"]) / 50 if candidates else 0,
    )
    directions = [
        {
            "id": "FULL_MARKET",
            "name": "全市场强势池",
            "group_type": "market",
            "member_count": total,
            "rising_member_count": rising,
            "new_high_count": new_high,
            **direction,
            "components": {**direction["components"], "amount_ratio": round(mean(amount_ratios), 2) if amount_ratios else 1.0},
            **build_full_market_quality(total),
        }
    ]
    scoped_candidates = assign_candidate_roles(candidates[:80], directions)
    return {
        "name": "全市场口径",
        "trade_date": latest_date,
        "directions": directions,
        "candidates": scoped_candidates,
        "gate": build_gate(directions, market_context),
    }


def synthetic_market_bars(histories: dict[str, list[MarketStyleDailyBar]], latest_date: date | None) -> list[Any]:
    by_date: dict[date, list[MarketStyleDailyBar]] = defaultdict(list)
    for bars in histories.values():
        for row in bars[-90:]:
            by_date[row.trade_date].append(row)
    rows = []
    for trade_date in sorted(by_date)[-90:]:
        day = by_date[trade_date]
        close = mean([row.close for row in day])
        open_ = mean([row.open for row in day])
        high = mean([row.high for row in day])
        low = mean([row.low for row in day])
        amount = sum(float(row.amount or 0) for row in day)
        previous = rows[-1].close if rows else None
        change_pct = (close / previous - 1) * 100 if previous else None
        rows.append(type("SyntheticBar", (), {"trade_date": trade_date, "open": open_, "high": high, "low": low, "close": close, "amount": amount, "change_pct": change_pct})())
    return rows


def custom_sector_scope(db: Session, histories: dict[str, list[MarketStyleDailyBar]], market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    sectors = list(db.scalars(select(SectorIndex).where(SectorIndex.active.is_(True)).order_by(SectorIndex.sort_order, SectorIndex.id)))
    stats_map, _latest_bar_map = sector_summary_maps(db, [sector.id for sector in sectors])
    latest_history_date = max((bars[-1].trade_date for bars in histories.values() if bars), default=None)
    directions = []
    candidate_pool: list[dict[str, Any]] = []
    for sector in sectors:
        bars = list(db.scalars(select(SectorIndexBar).where(SectorIndexBar.sector_id == sector.id).order_by(SectorIndexBar.trade_date)))
        members = list(db.scalars(select(SectorIndexMember).where(SectorIndexMember.sector_id == sector.id)))
        member_codes = {member.full_code for member in members}
        member_candidates = [
            {**item, "sector_id": sector.id, "sector_name": sector.name}
            for code in member_codes
            if (bars_for_code := histories.get(code))
            if (item := score_stock(code, bars_for_code[-1].name, bars_for_code))
        ]
        candidate_pool.extend(member_candidates)
        total = len(member_codes)
        new_high = count_new_high(member_codes, histories)
        stats = stats_map.get(sector.id) or {}
        rising = int(stats.get("rising_member_count") or 0)
        quoted = int(stats.get("quoted_member_count") or total or 0)
        scored = score_direction_from_bars(
            bars,
            breadth_ratio=rising / quoted if quoted else 0,
            new_high_ratio=new_high / total if total else 0,
            core_support_ratio=len([c for c in member_candidates if c["candidate_status"] == "合理买点"]) / max(total, 1),
        )
        directions.append(
            {
                "id": sector.id,
                "name": sector.name,
                "group_type": "custom",
                "member_count": total,
                "rising_member_count": rising,
                "new_high_count": new_high,
                "latest_change_pct": stats.get("latest_change_pct"),
                **scored,
                **build_sector_quality(sector, members, member_codes, histories, member_candidates, bars, latest_history_date),
            }
        )
    directions.sort(key=lambda item: item["effect_score"], reverse=True)
    candidate_pool.sort(key=candidate_sort_key, reverse=True)
    scoped_candidates = assign_candidate_roles(candidate_pool[:80], directions)
    return {
        "name": "自定义板块池口径",
        "trade_date": latest_history_date,
        "directions": directions,
        "candidates": scoped_candidates,
        "gate": build_gate(directions, market_context),
    }


def count_new_high(full_codes: set[str], histories: dict[str, list[MarketStyleDailyBar]]) -> int:
    count = 0
    for code in full_codes:
        bars = histories.get(code) or []
        if len(bars) < 60:
            continue
        closes = [row.close for row in bars]
        if closes[-1] >= max(closes[-60:]) * 0.98:
            count += 1
    return count


def run_fixed_backtest() -> dict[str, Any]:
    if not RESEARCH_BACKTEST.exists():
        raise ValueError("固定策略回测脚本不存在")
    completed = subprocess.run(
        [sys.executable, "-B", str(RESEARCH_BACKTEST)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError((completed.stderr or completed.stdout or "回测失败")[-1000:])
    trades = []
    if RESEARCH_TRADES.exists():
        with RESEARCH_TRADES.open(encoding="utf-8") as f:
            trades = list(csv.DictReader(f))
    summary = parse_backtest_stdout(completed.stdout)
    return {
        "status": "ok",
        "message": "固定策略回测已完成。结果用于复盘校准，不是买卖建议。",
        **summary,
        "trade_samples": trades[-12:],
        "stdout": completed.stdout.strip(),
    }


def parse_backtest_stdout(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "initial_cash": 1_000_000,
        "ending_equity": None,
        "return_pct": None,
        "max_drawdown_pct": None,
        "trade_count": None,
    }
    for token in text.replace("\n", " ").split():
        if token.startswith("ending_equity="):
            result["ending_equity"] = float(token.split("=", 1)[1])
        elif token.startswith("return="):
            result["return_pct"] = float(token.split("=", 1)[1].rstrip("%"))
        elif token.startswith("max_dd="):
            result["max_drawdown_pct"] = float(token.split("=", 1)[1].rstrip("%"))
        elif token.startswith("trades="):
            result["trade_count"] = int(token.split("=", 1)[1])
    return result

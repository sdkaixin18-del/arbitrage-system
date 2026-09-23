from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_data_root, get_db
from app.decision_policy import attach_manual_comparison, evaluation_for_frozen_decision
from app.expectation_gap_backtest import router as five_year_validation_router
from app.information_screening import item_to_out
from app.models import IndustryTrendDecision, InformationScreeningItem, InvestmentDecisionEvent, StockDailyBar, now_utc


router = APIRouter(prefix="/api/investment/decision-review", tags=["decision-review"])
router.include_router(five_year_validation_router)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
HORIZONS = (5, 20, 60)
DECISION_CODES = ("A", "B", "C", "D")


class DecisionCreate(BaseModel):
    information_item_id: int
    decision_code: Literal["A", "B", "C", "D"]
    primary_stock_code: str | None = Field(default=None, max_length=16)
    primary_stock_name: str | None = Field(default=None, max_length=80)
    alternatives: list[str] = Field(default_factory=list, max_length=20)
    direction_verdict: str = Field(default="", max_length=1000)
    stock_verdict: str = Field(default="", max_length=1000)
    pricing_verdict: str = Field(default="", max_length=1000)
    thesis: str = Field(default="", max_length=2000)
    why_best: str = Field(default="", max_length=1500)
    trigger_conditions: list[str] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)
    planned_horizon: Literal[5, 20, 60] = 20
    cost_bps: int = Field(default=20, ge=0, le=200)

    @model_validator(mode="after")
    def validate_trade_expression(self):
        if self.decision_code in {"A", "B", "C"}:
            if not (self.primary_stock_code or "").strip() or not (self.primary_stock_name or "").strip():
                raise ValueError("A/B/C 决策必须填写主选股票代码和名称")
        if not self.thesis.strip():
            raise ValueError("请写清这次决策所依据的核心变化")
        if not self.pricing_verdict.strip():
            raise ValueError("请判断股价已经反映什么、尚未反映什么")
        return self


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(raw: str | None, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def normalize_stock_code(value: str | None) -> tuple[str | None, str | None]:
    raw = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    market_match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if market_match:
        return market_match.group(2), raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 6:
        return None, None
    if digits.startswith(("6", "688")):
        exchange = "SH"
    elif digits.startswith(("0", "2", "3")):
        exchange = "SZ"
    elif digits.startswith(("4", "8", "9")):
        exchange = "BJ"
    else:
        return None, None
    return digits, f"{exchange}{digits}"


def _market_date(value: datetime) -> date:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(SHANGHAI_TZ).date()


@lru_cache(maxsize=1)
def _load_hs300_bars() -> dict[date, dict[str, float]]:
    path = get_data_root() / "research" / "zsxq_backtest" / "benchmark_hs300.csv"
    if not path.exists():
        return {}
    result: dict[date, dict[str, float]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    trade_date = date.fromisoformat(str(row.get("trade_date", "")))
                    result[trade_date] = {
                        "open": float(row["open"]),
                        "close": float(row["close"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return {}
    return result


def _benchmark_return(entry_date: date, exit_date: date) -> float | None:
    bars = _load_hs300_bars()
    entry = bars.get(entry_date)
    exit_bar = bars.get(exit_date)
    if not entry or not exit_bar or entry["open"] <= 0:
        return None
    return round((exit_bar["close"] / entry["open"] - 1) * 100, 4)


def refresh_decision_performance(db: Session, row: InvestmentDecisionEvent | IndustryTrendDecision) -> None:
    if not row.primary_full_code:
        row.execution_status = "no_stock"
        row.performance_json = _dumps({"horizons": {}, "benchmark_status": "not_applicable"})
        return

    bars = list(
        db.scalars(
            select(StockDailyBar)
            .where(
                StockDailyBar.full_code == row.primary_full_code,
                StockDailyBar.trade_date > _market_date(row.signal_at),
            )
            .order_by(StockDailyBar.trade_date.asc())
            .limit(max(HORIZONS))
        )
    )
    shadow = row.decision_code != "A"
    if not bars:
        row.execution_status = "shadow_waiting" if shadow else "waiting_entry"
        row.entry_date = None
        row.entry_price = None
        row.performance_json = _dumps({
            "horizons": {},
            "bars_available": 0,
            "benchmark_status": "waiting_for_market_data",
        })
        return

    entry = bars[0]
    row.entry_date = entry.trade_date
    row.entry_price = float(entry.open)
    cost_pct = row.cost_bps / 100.0
    horizon_results: dict[str, dict[str, Any]] = {}
    has_benchmark = False
    for horizon in HORIZONS:
        if len(bars) < horizon:
            horizon_results[str(horizon)] = {"available": False, "bars_available": len(bars)}
            continue
        window = bars[:horizon]
        exit_bar = window[-1]
        gross_return = (float(exit_bar.close) / float(entry.open) - 1) * 100
        net_return = gross_return - cost_pct
        mfe = (max(float(bar.high) for bar in window) / float(entry.open) - 1) * 100 - cost_pct
        mae = (min(float(bar.low) for bar in window) / float(entry.open) - 1) * 100 - cost_pct
        benchmark_return = _benchmark_return(entry.trade_date, exit_bar.trade_date)
        has_benchmark = has_benchmark or benchmark_return is not None
        horizon_results[str(horizon)] = {
            "available": True,
            "exit_date": exit_bar.trade_date.isoformat(),
            "exit_price": round(float(exit_bar.close), 4),
            "gross_return_pct": round(gross_return, 4),
            "return_pct": round(net_return, 4),
            "benchmark_return_pct": benchmark_return,
            "excess_return_pct": round(net_return - benchmark_return, 4) if benchmark_return is not None else None,
            "mfe_pct": round(mfe, 4),
            "mae_pct": round(mae, 4),
        }

    mature = bool(horizon_results.get(str(row.planned_horizon), {}).get("available"))
    row.execution_status = (
        "shadow_matured" if shadow and mature else
        "shadow_tracking" if shadow else
        "matured" if mature else
        "tracking"
    )
    row.performance_json = _dumps({
        "entry_date": entry.trade_date.isoformat(),
        "entry_price": round(float(entry.open), 4),
        "bars_available": len(bars),
        "cost_bps": row.cost_bps,
        "horizons": horizon_results,
        "benchmark_status": "available" if has_benchmark else "pending_or_unavailable",
    })


def decision_to_out(row: InvestmentDecisionEvent | IndustryTrendDecision) -> dict[str, Any]:
    is_industry = isinstance(row, IndustryTrendDecision)
    snapshot = _loads(row.snapshot_json, {})
    policy_evaluation = evaluation_for_frozen_decision(
        snapshot,
        source_kind="industry_trend" if is_industry else "information",
        primary_stock_name=row.primary_stock_name,
        primary_full_code=row.primary_full_code,
        why_best=row.why_best,
    )
    return {
        "id": row.id,
        "decision_key": row.decision_key,
        "source_kind": "industry_trend" if is_industry else "information",
        "industry_chain_id": row.chain_id if is_industry else None,
        "information_item_id": None if is_industry else row.information_item_id,
        "information_item_key": None if is_industry else row.information_item_key,
        "information_title": row.chain_name if is_industry else row.information_title,
        "source_type": "industry_trend" if is_industry else row.source_type,
        "source_name": "产业趋势工作台" if is_industry else row.source_name,
        "snapshot": snapshot,
        "policy_evaluation": attach_manual_comparison(policy_evaluation, row.decision_code),
        "signal_at": row.signal_at,
        "decision_code": row.decision_code,
        "primary_stock_code": row.primary_stock_code,
        "primary_stock_name": row.primary_stock_name,
        "primary_full_code": row.primary_full_code,
        "alternatives": _loads(row.alternatives_json, []),
        "direction_verdict": row.direction_verdict,
        "stock_verdict": row.stock_verdict,
        "timing_verdict": row.timing_verdict if is_industry else "",
        "pricing_verdict": row.pricing_verdict,
        "thesis": row.thesis,
        "why_best": row.why_best,
        "trigger_conditions": _loads(row.trigger_conditions_json, []),
        "invalidation_conditions": _loads(row.invalidation_conditions_json, []),
        "planned_horizon": row.planned_horizon,
        "benchmark_code": row.benchmark_code,
        "cost_bps": row.cost_bps,
        "execution_status": row.execution_status,
        "entry_date": row.entry_date,
        "entry_price": row.entry_price,
        "performance": _loads(row.performance_json, {}),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def create_decision(
    payload: DecisionCreate,
    db: Session,
    clock: Callable[[], datetime] = now_utc,
) -> InvestmentDecisionEvent:
    item = db.get(InformationScreeningItem, payload.information_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="信息卡片不存在")
    existing = db.scalar(
        select(InvestmentDecisionEvent).where(InvestmentDecisionEvent.information_item_id == item.id)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="这条信息已经形成过冻结决策，请到决策复盘查看")

    code, full_code = normalize_stock_code(payload.primary_stock_code)
    if payload.primary_stock_code and not full_code:
        raise HTTPException(status_code=422, detail="股票代码格式不正确，请填写六位 A 股代码")
    signal_at = clock()
    decision_key = "decision-" + hashlib.sha1(
        f"{item.id}|{signal_at.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    information_snapshot = item_to_out(item)
    snapshot = {
        "information": information_snapshot,
        "policy_evaluation": information_snapshot.get("decision_policy", {}),
        "decision_inputs": payload.model_dump(),
        "frozen_at": signal_at,
        "rule": "从决策形成后的下一交易日开盘模拟成交；原始快照不可修改",
    }
    row = InvestmentDecisionEvent(
        decision_key=decision_key,
        information_item_id=item.id,
        information_item_key=item.item_key,
        information_title=item.title,
        source_type=item.source_type,
        source_name=item.source_name,
        snapshot_json=_dumps(snapshot),
        signal_at=signal_at,
        decision_code=payload.decision_code,
        primary_stock_code=code,
        primary_stock_name=(payload.primary_stock_name or "").strip() or None,
        primary_full_code=full_code,
        alternatives_json=_dumps([value.strip() for value in payload.alternatives if value.strip()]),
        direction_verdict=payload.direction_verdict.strip(),
        stock_verdict=payload.stock_verdict.strip(),
        pricing_verdict=payload.pricing_verdict.strip(),
        thesis=payload.thesis.strip(),
        why_best=payload.why_best.strip(),
        trigger_conditions_json=_dumps([value.strip() for value in payload.trigger_conditions if value.strip()]),
        invalidation_conditions_json=_dumps([value.strip() for value in payload.invalidation_conditions if value.strip()]),
        planned_horizon=payload.planned_horizon,
        cost_bps=payload.cost_bps,
        execution_status="shadow_waiting" if payload.decision_code != "A" else "waiting_entry",
        performance_json="{}",
    )
    db.add(row)
    db.flush()
    refresh_decision_performance(db, row)
    db.commit()
    db.refresh(row)
    return row


def _metric(rows: list[InvestmentDecisionEvent | IndustryTrendDecision], horizon: int) -> dict[str, Any]:
    values: list[float] = []
    excess_values: list[float] = []
    mfe_values: list[float] = []
    mae_values: list[float] = []
    for row in rows:
        result = _loads(row.performance_json, {}).get("horizons", {}).get(str(horizon), {})
        if not result.get("available"):
            continue
        values.append(float(result["return_pct"]))
        if result.get("excess_return_pct") is not None:
            excess_values.append(float(result["excess_return_pct"]))
        if result.get("mfe_pct") is not None:
            mfe_values.append(float(result["mfe_pct"]))
        if result.get("mae_pct") is not None:
            mae_values.append(float(result["mae_pct"]))
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = abs(sum(losses) / len(losses)) if losses else None
    payoff_ratio = avg_win / avg_loss if avg_win is not None and avg_loss else None
    return {
        "sample_count": len(values),
        "win_rate": round(len(wins) / len(values) * 100, 2) if values else None,
        "avg_return_pct": round(sum(values) / len(values), 4) if values else None,
        "expected_value_pct": round(sum(values) / len(values), 4) if values else None,
        "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
        "avg_excess_return_pct": round(sum(excess_values) / len(excess_values), 4) if excess_values else None,
        "avg_mfe_pct": round(sum(mfe_values) / len(mfe_values), 4) if mfe_values else None,
        "avg_mae_pct": round(sum(mae_values) / len(mae_values), 4) if mae_values else None,
    }


def _overview(db: Session, limit: int = 500) -> dict[str, Any]:
    information_rows = list(
        db.scalars(select(InvestmentDecisionEvent).order_by(InvestmentDecisionEvent.signal_at.desc()).limit(limit))
    )
    industry_rows = list(
        db.scalars(select(IndustryTrendDecision).order_by(IndustryTrendDecision.signal_at.desc()).limit(limit))
    )
    rows: list[InvestmentDecisionEvent | IndustryTrendDecision] = sorted(
        [*information_rows, *industry_rows], key=lambda row: row.signal_at, reverse=True
    )[:limit]
    for row in rows:
        refresh_decision_performance(db, row)
    db.commit()

    code_counts = {code: sum(row.decision_code == code for row in rows) for code in DECISION_CODES}
    info_total = int(db.scalar(select(func.count()).select_from(InformationScreeningItem)) or 0)
    verified_total = int(
        db.scalar(
            select(func.count()).select_from(InformationScreeningItem).where(
                InformationScreeningItem.verification_status.in_(("verified", "cross_verified"))
            )
        ) or 0
    )
    simulated_total = code_counts["A"]
    matured_total = sum(row.execution_status in {"matured", "shadow_matured"} for row in rows)
    tracking_total = sum(row.execution_status in {"tracking", "shadow_tracking"} for row in rows)
    waiting_total = sum(row.execution_status in {"waiting_entry", "shadow_waiting"} for row in rows)
    items = [decision_to_out(row) for row in rows]
    policy_counts: dict[str, dict[str, int]] = {}
    for item in items:
        for policy in item["policy_evaluation"].get("policies", []):
            counts = policy_counts.setdefault(policy["policy_id"], {code: 0 for code in DECISION_CODES})
            recommendation = policy.get("recommendation")
            if recommendation in counts:
                counts[recommendation] += 1
    policy_disagreement_total = sum(bool(item["policy_evaluation"].get("disagreement")) for item in items)
    manual_disagreement_total = sum(
        not bool(item["policy_evaluation"].get("manual_aligned_with_all")) for item in items
    )
    quality_incomplete_total = sum(
        not bool(item["policy_evaluation"].get("quality_complete")) for item in items
    )
    return {
        "status": "ok",
        "items": items,
        "summary": {
            "total": len(rows),
            "decision_counts": code_counts,
            "policy_counts": policy_counts,
            "policy_disagreement_total": policy_disagreement_total,
            "manual_disagreement_total": manual_disagreement_total,
            "quality_incomplete_total": quality_incomplete_total,
            "waiting": waiting_total,
            "tracking": tracking_total,
            "matured": matured_total,
            "pipeline": {
                "information_total": info_total,
                "verified_total": verified_total,
                "decision_total": len(rows),
                "industry_decision_total": len(industry_rows),
                "simulated_total": simulated_total,
            },
            "horizons": {str(horizon): _metric(rows, horizon) for horizon in HORIZONS},
            "calibration_20d": {code: _metric([row for row in rows if row.decision_code == code], 20) for code in DECISION_CODES},
            "method": {
                "signal": "形成冻结决策的时间",
                "entry": "信号后的下一交易日开盘价",
                "cost": "默认计入20bp双向成本与滑点",
                "benchmark": "沪深300；缺少同期数据时明确标记待补",
                "preservation": "原始快照不可修改，A为模拟持仓，B/C/D为影子跟踪",
            },
        },
    }


@router.get("")
def decision_review_overview(
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    return _overview(db, limit)


@router.post("")
def create_decision_endpoint(payload: DecisionCreate, db: Session = Depends(get_db)):
    row = create_decision(payload, db)
    return {"status": "created", "item": decision_to_out(row)}


@router.post("/refresh")
def refresh_decisions(db: Session = Depends(get_db)):
    return _overview(db)

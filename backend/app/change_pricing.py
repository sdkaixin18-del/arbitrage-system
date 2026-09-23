from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ChangePricingAnalysis, ChangePricingFeedback, now_utc


SourceTier = Literal["S", "A", "B", "clue"]
router = APIRouter(prefix="/api/change-pricing", tags=["change-pricing"])

SOURCE_FACTORS: dict[str, float] = {"S": 1.0, "A": 0.97, "B": 0.92, "clue": 0.85}


class ChangePricingInput(BaseModel):
    analysis_key: str | None = Field(default=None, max_length=180)
    subject_name: str = Field(min_length=1, max_length=120)
    subject_code: str = Field(default="", max_length=40)
    change_title: str = Field(min_length=1, max_length=280)
    change_date: date | None = None
    source_tier: SourceTier = "B"
    source_label: str = Field(default="", max_length=160)
    source_summary: str = ""
    source_urls: list[str] = Field(default_factory=list)
    fundamental_score: float = Field(ge=0, le=5)
    freshness_score: float = Field(ge=0, le=5)
    chain_score: float = Field(ge=0, le=5)
    evidence_score: float = Field(ge=0, le=5)
    chart_score: float = Field(ge=0, le=5)
    excess_return: float = Field(default=0, ge=-100, le=500)
    fair_value_uplift: float = Field(default=1, ge=0.1, le=500)
    earnings_revision: float = Field(default=0, ge=0, le=100)
    valuation_percentile: float = Field(default=0, ge=0, le=100)
    crowding: float = Field(default=0, ge=0, le=100)
    chain_diffusion: float = Field(default=0, ge=0, le=100)
    days_elapsed: int = Field(default=0, ge=0, le=3650)
    reflected_items: list[str] = Field(default_factory=list)
    unreflected_items: list[str] = Field(default_factory=list)
    validation_points: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    reasoning: str = ""
    conversation_thread_id: str | None = Field(default=None, max_length=80)


class ChangePricingBulkUpsert(BaseModel):
    analyses: list[ChangePricingInput] = Field(min_length=1, max_length=100)
    restore_deleted: bool = False


class ChangePricingOut(ChangePricingInput):
    id: int
    analysis_key: str
    raw_change_score: float
    change_score: float
    reaction_ratio: float
    digestion_score: float
    opportunity_score: float
    pricing_stage: str
    action: str
    created_at: datetime
    updated_at: datetime


class ChangePricingListOut(BaseModel):
    status: str = "ok"
    items: list[ChangePricingOut]


class ChangePricingBulkOut(BaseModel):
    status: str = "ok"
    created: int
    updated: int
    suppressed: int
    items: list[ChangePricingOut]


def _loads(value: str, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _analysis_key(payload: ChangePricingInput) -> str:
    name = re.sub(r"\s+", " ", payload.subject_name.strip().lower())
    title = re.sub(r"\s+", " ", payload.change_title.strip().lower())
    day = payload.change_date.isoformat() if payload.change_date else "undated"
    raw = f"{payload.subject_code.strip().lower()}|{name}|{title}|{day}"
    return f"conversation-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return min(high, max(low, value))


def _opportunity_fit(digestion: float) -> float:
    if digestion <= 20:
        return 55 + digestion * 1.75
    if digestion <= 35:
        return 90 + (digestion - 20) * (10 / 15)
    if digestion <= 50:
        return 100 - (digestion - 35)
    if digestion <= 75:
        return 85 - (digestion - 50) * 1.8
    return max(0, 40 - (digestion - 75) * 1.6)


def _computed(row: ChangePricingAnalysis) -> dict[str, float | str]:
    raw_change = (
        row.fundamental_score / 5 * 40
        + row.freshness_score / 5 * 25
        + row.chain_score / 5 * 20
        + row.evidence_score / 5 * 10
        + row.chart_score / 5 * 5
    )
    change_score = _clamp(raw_change * SOURCE_FACTORS.get(row.source_tier, 0.92))
    reaction_ratio = _clamp(max(0, row.excess_return) / max(0.1, row.fair_value_uplift) * 100, 0, 150)
    time_score = _clamp(row.days_elapsed / 60 * 100)
    digestion = _clamp(
        _clamp(reaction_ratio) * 0.35
        + row.earnings_revision * 0.2
        + row.valuation_percentile * 0.15
        + row.crowding * 0.1
        + row.chain_diffusion * 0.1
        + time_score * 0.1
    )
    opportunity = _clamp(change_score * 0.75 + _opportunity_fit(digestion) * 0.25)
    if digestion <= 20:
        stage = "unseen"
    elif digestion <= 50:
        stage = "early"
    elif digestion <= 75:
        stage = "priced"
    else:
        stage = "over"
    if change_score >= 75 and 20 < digestion <= 50:
        action = "优先交易候选：强变化处于初步定价区"
    elif change_score >= 75 and digestion <= 20:
        action = "重点研究：预期差较大，但先确认市场为何尚未表达"
    elif change_score >= 75 and digestion <= 75:
        action = "等待二次变化或回调，不追原始逻辑"
    elif digestion > 75:
        action = "原逻辑已充分交易，除非出现新的盈利上调"
    else:
        action = "观察，等待硬证据升级"
    return {
        "raw_change_score": round(raw_change, 2),
        "change_score": round(change_score, 2),
        "reaction_ratio": round(reaction_ratio, 2),
        "digestion_score": round(digestion, 2),
        "opportunity_score": round(opportunity, 2),
        "pricing_stage": stage,
        "action": action,
    }


def analysis_to_out(row: ChangePricingAnalysis) -> ChangePricingOut:
    return ChangePricingOut(
        id=row.id,
        analysis_key=row.analysis_key,
        subject_name=row.subject_name,
        subject_code=row.subject_code,
        change_title=row.change_title,
        change_date=row.change_date,
        source_tier=row.source_tier,
        source_label=row.source_label,
        source_summary=row.source_summary,
        source_urls=_loads(row.source_urls_json, []),
        fundamental_score=row.fundamental_score,
        freshness_score=row.freshness_score,
        chain_score=row.chain_score,
        evidence_score=row.evidence_score,
        chart_score=row.chart_score,
        excess_return=row.excess_return,
        fair_value_uplift=row.fair_value_uplift,
        earnings_revision=row.earnings_revision,
        valuation_percentile=row.valuation_percentile,
        crowding=row.crowding,
        chain_diffusion=row.chain_diffusion,
        days_elapsed=row.days_elapsed,
        reflected_items=_loads(row.reflected_items_json, []),
        unreflected_items=_loads(row.unreflected_items_json, []),
        validation_points=_loads(row.validation_points_json, []),
        invalidation_conditions=_loads(row.invalidation_conditions_json, []),
        reasoning=row.reasoning,
        conversation_thread_id=row.conversation_thread_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **_computed(row),
    )


def upsert_change_pricing(db: Session, payload: ChangePricingInput) -> tuple[ChangePricingAnalysis, bool]:
    key = (payload.analysis_key or "").strip() or _analysis_key(payload)
    row = db.scalar(select(ChangePricingAnalysis).where(ChangePricingAnalysis.analysis_key == key))
    created = row is None
    if row is None:
        row = ChangePricingAnalysis(analysis_key=key, subject_name=payload.subject_name, change_title=payload.change_title)
        db.add(row)
    for field in (
        "subject_name", "subject_code", "change_title", "change_date", "source_tier", "source_label",
        "source_summary", "fundamental_score", "freshness_score", "chain_score", "evidence_score", "chart_score",
        "excess_return", "fair_value_uplift", "earnings_revision", "valuation_percentile", "crowding",
        "chain_diffusion", "days_elapsed", "reasoning", "conversation_thread_id",
    ):
        setattr(row, field, getattr(payload, field))
    row.source_urls_json = _dumps(payload.source_urls)
    row.reflected_items_json = _dumps(payload.reflected_items)
    row.unreflected_items_json = _dumps(payload.unreflected_items)
    row.validation_points_json = _dumps(payload.validation_points)
    row.invalidation_conditions_json = _dumps(payload.invalidation_conditions)
    row.updated_at = now_utc()
    db.flush()
    return row, created


@router.get("", response_model=ChangePricingListOut)
def list_change_pricing(limit: int = Query(default=200, ge=1, le=1000), db: Session = Depends(get_db)) -> ChangePricingListOut:
    rows = list(db.scalars(select(ChangePricingAnalysis).order_by(ChangePricingAnalysis.updated_at.desc()).limit(limit)))
    return ChangePricingListOut(items=[analysis_to_out(row) for row in rows])


@router.post("/bulk", response_model=ChangePricingBulkOut)
def bulk_upsert_change_pricing(payload: ChangePricingBulkUpsert, db: Session = Depends(get_db)) -> ChangePricingBulkOut:
    rows: list[ChangePricingAnalysis] = []
    created = 0
    suppressed = 0
    for item in payload.analyses:
        key = (item.analysis_key or "").strip() or _analysis_key(item)
        if not payload.restore_deleted:
            deleted = db.scalar(
                select(ChangePricingFeedback.id).where(
                    ChangePricingFeedback.analysis_key == key,
                    ChangePricingFeedback.action == "manual_delete",
                )
            )
            if deleted is not None:
                suppressed += 1
                continue
        row, was_created = upsert_change_pricing(db, item)
        rows.append(row)
        created += int(was_created)
    db.commit()
    for row in rows:
        db.refresh(row)
    return ChangePricingBulkOut(
        created=created,
        updated=len(rows) - created,
        suppressed=suppressed,
        items=[analysis_to_out(row) for row in rows],
    )


@router.delete("/{analysis_id}")
def delete_change_pricing(analysis_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    row = db.get(ChangePricingAnalysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="变化定价记录不存在")
    snapshot = analysis_to_out(row).model_dump(mode="json")
    db.add(
        ChangePricingFeedback(
            analysis_key=row.analysis_key,
            subject_name=row.subject_name,
            subject_code=row.subject_code,
            action="manual_delete",
            note="用户从变化定价网页删除；保留快照，避免相同对话卡自动重建。",
            snapshot_json=_dumps(snapshot),
            conversation_thread_id=row.conversation_thread_id,
        )
    )
    db.delete(row)
    db.commit()
    return {"status": "ok", "message": "变化定价记录已删除"}

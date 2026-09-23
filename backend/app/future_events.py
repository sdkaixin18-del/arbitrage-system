from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FutureEvent, FutureEventFeedback, now_utc


Priority = Literal["S", "A", "B"]
Stage = Literal["clue", "window", "date_locked", "time_locked", "completed"]
SourceStatus = Literal["official", "media", "zsxq", "xueqiu", "mixed", "unverified"]
PricedInStatus = Literal["no", "partial", "yes", "unknown"]

router = APIRouter(prefix="/api/future-events", tags=["future-events"])


class StockMapping(BaseModel):
    market: str = ""
    code: str = ""
    name: str = ""
    role: str = ""


class StageHistoryItem(BaseModel):
    stage: Stage
    at: date
    note: str = ""


class FutureEventInput(BaseModel):
    event_key: str | None = Field(default=None, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    category: str = Field(default="其他", max_length=48)
    priority: Priority = "B"
    stage: Stage = "clue"
    start_date: date
    end_date: date | None = None
    exact_time: str | None = Field(default=None, max_length=80)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    source_status: SourceStatus = "unverified"
    source_summary: str = ""
    source_urls: list[str] = Field(default_factory=list)
    market_scope: str = Field(default="", max_length=80)
    stock_mappings: list[StockMapping] = Field(default_factory=list)
    impact_chain: str = ""
    priced_in_status: PricedInStatus = "unknown"
    price_expression: str = ""
    validation_points: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    stage_history: list[StageHistoryItem] = Field(default_factory=list)
    conversation_thread_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_window(self) -> "FutureEventInput":
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        return self


class FutureEventBulkUpsert(BaseModel):
    events: list[FutureEventInput] = Field(min_length=1, max_length=200)
    restore_deleted: bool = False


class FutureEventOut(FutureEventInput):
    id: int
    event_key: str
    created_at: datetime
    updated_at: datetime


class FutureEventListOut(BaseModel):
    status: str = "ok"
    today: date
    days: int | None
    items: list[FutureEventOut]


class FutureEventBulkUpsertOut(BaseModel):
    status: str = "ok"
    created: int
    updated: int
    suppressed: int = 0
    items: list[FutureEventOut]


class FutureEventFeedbackOut(BaseModel):
    id: int
    event_key: str
    title: str
    category: str
    priority: str
    stage: str
    action: str
    reason_category: str | None
    note: str | None
    snapshot: dict
    conversation_thread_id: str | None
    created_at: datetime


class FutureEventDeleteInput(BaseModel):
    reason_category: str | None = Field(default=None, max_length=48)
    reason: str | None = Field(default=None, max_length=500)


def _json_loads(value: str, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _generated_event_key(payload: FutureEventInput) -> str:
    normalized = re.sub(r"\s+", " ", payload.title.strip().lower())
    raw = f"{normalized}|{payload.start_date.isoformat()}|{payload.category.strip().lower()}"
    return f"conversation-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]}"


def event_to_out(event: FutureEvent) -> FutureEventOut:
    return FutureEventOut(
        id=event.id,
        event_key=event.event_key,
        title=event.title,
        category=event.category,
        priority=event.priority,
        stage=event.stage,
        start_date=event.start_date,
        end_date=event.end_date,
        exact_time=event.exact_time,
        timezone=event.timezone,
        source_status=event.source_status,
        source_summary=event.source_summary,
        source_urls=_json_loads(event.source_urls_json, []),
        market_scope=event.market_scope,
        stock_mappings=_json_loads(event.stock_mappings_json, []),
        impact_chain=event.impact_chain,
        priced_in_status=event.priced_in_status,
        price_expression=event.price_expression,
        validation_points=_json_loads(event.validation_points_json, []),
        invalidation_conditions=_json_loads(event.invalidation_conditions_json, []),
        stage_history=_json_loads(event.stage_history_json, []),
        conversation_thread_id=event.conversation_thread_id,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def upsert_future_event(db: Session, payload: FutureEventInput) -> tuple[FutureEvent, bool]:
    event_key = (payload.event_key or "").strip() or _generated_event_key(payload)
    event = db.scalar(select(FutureEvent).where(FutureEvent.event_key == event_key))
    created = event is None
    if event is None:
        event = FutureEvent(event_key=event_key, title=payload.title, start_date=payload.start_date)
        db.add(event)

    event.title = payload.title.strip()
    event.category = payload.category.strip() or "其他"
    event.priority = payload.priority
    event.stage = payload.stage
    event.start_date = payload.start_date
    event.end_date = payload.end_date
    event.exact_time = (payload.exact_time or "").strip() or None
    event.timezone = payload.timezone.strip() or "Asia/Shanghai"
    event.source_status = payload.source_status
    event.source_summary = payload.source_summary.strip()
    event.source_urls_json = _json_dumps(payload.source_urls)
    event.market_scope = payload.market_scope.strip()
    event.stock_mappings_json = _json_dumps([item.model_dump() for item in payload.stock_mappings])
    event.impact_chain = payload.impact_chain.strip()
    event.priced_in_status = payload.priced_in_status
    event.price_expression = payload.price_expression.strip()
    event.validation_points_json = _json_dumps(payload.validation_points)
    event.invalidation_conditions_json = _json_dumps(payload.invalidation_conditions)
    event.stage_history_json = _json_dumps(
        [{**item.model_dump(), "at": item.at.isoformat()} for item in payload.stage_history]
    )
    event.conversation_thread_id = (payload.conversation_thread_id or "").strip() or None
    event.updated_at = now_utc()
    db.flush()
    return event, created


@router.get("", response_model=FutureEventListOut)
def list_future_events(
    days: int | None = Query(default=30, ge=1, le=3650),
    include_past: bool = False,
    db: Session = Depends(get_db),
) -> FutureEventListOut:
    today = date.today()
    statement = select(FutureEvent)
    if not include_past:
        statement = statement.where(or_(FutureEvent.end_date >= today, FutureEvent.start_date >= today))
    if days is not None:
        statement = statement.where(FutureEvent.start_date <= today + timedelta(days=days - 1))
    priority_order = case((FutureEvent.priority == "S", 0), (FutureEvent.priority == "A", 1), else_=2)
    items = list(db.scalars(statement.order_by(FutureEvent.start_date, priority_order, FutureEvent.id)))
    return FutureEventListOut(today=today, days=days, items=[event_to_out(item) for item in items])


@router.post("/bulk", response_model=FutureEventBulkUpsertOut)
def bulk_upsert_future_events(
    payload: FutureEventBulkUpsert,
    db: Session = Depends(get_db),
) -> FutureEventBulkUpsertOut:
    items: list[FutureEvent] = []
    created = 0
    suppressed = 0
    for event_payload in payload.events:
        event_key = (event_payload.event_key or "").strip() or _generated_event_key(event_payload)
        if not payload.restore_deleted:
            deleted_before = db.scalar(
                select(FutureEventFeedback.id).where(
                    FutureEventFeedback.event_key == event_key,
                    FutureEventFeedback.action == "manual_delete",
                )
            )
            if deleted_before is not None:
                suppressed += 1
                continue
        event, was_created = upsert_future_event(db, event_payload)
        items.append(event)
        created += int(was_created)
    db.commit()
    for item in items:
        db.refresh(item)
    return FutureEventBulkUpsertOut(
        created=created,
        updated=len(items) - created,
        suppressed=suppressed,
        items=[event_to_out(item) for item in items],
    )


@router.get("/feedback", response_model=list[FutureEventFeedbackOut])
def list_future_event_feedback(
    limit: int = Query(default=200, ge=1, le=1000),
    keyword: str | None = Query(default=None, max_length=120),
    reason_category: str | None = Query(default=None, max_length=48),
    category: str | None = Query(default=None, max_length=48),
    db: Session = Depends(get_db),
) -> list[FutureEventFeedbackOut]:
    statement = select(FutureEventFeedback).where(FutureEventFeedback.action == "manual_delete")
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        statement = statement.where(
            or_(FutureEventFeedback.title.ilike(pattern), FutureEventFeedback.note.ilike(pattern))
        )
    if reason_category and reason_category.strip():
        statement = statement.where(FutureEventFeedback.reason_category == reason_category.strip())
    if category and category.strip():
        statement = statement.where(FutureEventFeedback.category == category.strip())
    rows = list(db.scalars(statement.order_by(FutureEventFeedback.created_at.desc()).limit(limit)))
    return [
        FutureEventFeedbackOut(
            id=row.id,
            event_key=row.event_key,
            title=row.title,
            category=row.category,
            priority=row.priority,
            stage=row.stage,
            action=row.action,
            reason_category=row.reason_category,
            note=row.note,
            snapshot=_json_loads(row.snapshot_json, {}),
            conversation_thread_id=row.conversation_thread_id,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/{event_id}")
def delete_future_event(
    event_id: int,
    payload: FutureEventDeleteInput | None = None,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    event = db.get(FutureEvent, event_id)
    if event is None:
        # DELETE is intentionally idempotent: if the server committed the
        # deletion but the browser lost the response during a local restart,
        # a safe retry should still report success instead of a false failure.
        return {"status": "ok", "message": "未来事件已不存在"}
    snapshot = event_to_out(event).model_dump(mode="json")
    reason_category = (payload.reason_category or "").strip() if payload else ""
    reason = (payload.reason or "").strip() if payload else ""
    db.add(
        FutureEventFeedback(
            event_key=event.event_key,
            title=event.title,
            category=event.category,
            priority=event.priority,
            stage=event.stage,
            action="manual_delete",
            reason_category=reason_category or None,
            note=reason or None,
            snapshot_json=_json_dumps(snapshot),
            conversation_thread_id=event.conversation_thread_id,
        )
    )
    db.delete(event)
    db.commit()
    return {"status": "ok", "message": "未来事件已删除"}

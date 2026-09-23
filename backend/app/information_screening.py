from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.decision_policy import evaluate_information_policies
from app.models import (
    IndustryChain,
    IndustryChainEvidence,
    IndustryChainTask,
    IndustryTrendUpdate,
    InformationScreeningBatch,
    InformationScreeningFeedback,
    InformationScreeningItem,
    now_utc,
)
from app.watchlist_announcements import lookup_cninfo_announcements


router = APIRouter(prefix="/api/investment/information-screening", tags=["information-screening"])

Bucket = Literal["verified", "eye_catching", "filtered"]
VerificationStatus = Literal["verified", "cross_verified", "partial", "unverified", "disproved"]
PriceStatus = Literal["untraded", "first_expression", "multi_rounds", "negative", "no_confirmation", "unknown"]
OfficialLookup = Callable[[Session, str, date, date], dict[str, Any]]

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
OFFICIAL_CLAIM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "performance": ("业绩预告", "业绩快报", "净利润", "扣非", "营业收入", "营收", "同比增长", "同比预增", "扭亏"),
    "contract": ("合同金额", "重大合同", "中标", "订单金额", "签订合同"),
    "capital": ("回购", "增持", "减持", "定增", "重组", "股权激励", "员工持股"),
}
OFFICIAL_TITLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "performance": ("业绩预告", "业绩预增", "业绩预减", "业绩快报", "主要经营数据", "年度报告", "半年度报告", "季度报告"),
    "contract": ("合同", "中标", "订单"),
    "capital": ("回购", "增持", "减持", "定增", "重组", "股权激励", "员工持股"),
}
FALSE_NEGATIVE_PHRASES = ("被传", "据传", "公告待补", "公告待核", "未见公告", "未找到公告", "没有公告", "尚无公告")


class ScreeningItemInput(BaseModel):
    item_key: str | None = Field(default=None, max_length=180)
    title: str = Field(min_length=1, max_length=500)
    summary: str = ""
    source_type: str = Field(default="other", max_length=40)
    source_name: str = Field(default="", max_length=160)
    source_url: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    bucket: Bucket = "eye_catching"
    is_top: bool = False
    importance: int = Field(default=3, ge=1, le=5)
    marginal_change: str = ""
    verification_status: VerificationStatus = "unverified"
    evidence_summary: str = ""
    related_sectors: list[str] = Field(default_factory=list)
    related_stocks: list[str] = Field(default_factory=list)
    price_status: PriceStatus = "unknown"
    price_summary: str = ""
    validation_points: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    filter_reason: str = ""
    recovery_condition: str = ""
    conversation_thread_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_bucket_fields(self):
        if self.bucket == "filtered" and not self.filter_reason.strip():
            raise ValueError("已读过滤项必须填写过滤原因")
        if self.bucket != "filtered" and not self.marginal_change.strip():
            raise ValueError("保留项必须写清边际变化或重要线索")
        return self


class ScreeningBatchInput(BaseModel):
    batch_key: str | None = Field(default=None, max_length=180)
    title: str = Field(min_length=1, max_length=240)
    window_start: datetime | None = None
    window_end: datetime | None = None
    source_scope: list[str] = Field(default_factory=list)
    read_count: int = Field(default=0, ge=0)
    deduplicated_count: int = Field(default=0, ge=0)
    conversation_thread_id: str | None = Field(default=None, max_length=80)
    restore_deleted: bool = False
    items: list[ScreeningItemInput] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_counts(self):
        if self.deduplicated_count > self.read_count and self.read_count:
            raise ValueError("去重后数量不能大于已读取数量")
        return self


class ScreeningItemPatch(BaseModel):
    bucket: Bucket | None = None
    is_top: bool | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    verification_status: VerificationStatus | None = None
    evidence_summary: str | None = None
    price_status: PriceStatus | None = None
    price_summary: str | None = None
    validation_points: list[str] | None = None
    invalidation_conditions: list[str] | None = None
    filter_reason: str | None = None
    recovery_condition: str | None = None


class PromotionInput(BaseModel):
    chain_id: int


class ScreeningDeleteInput(BaseModel):
    reason_category: str | None = Field(default=None, max_length=48)
    note: str | None = Field(default=None, max_length=500)


def _loads(raw: str | None, fallback):
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _stable_key(*parts: str) -> str:
    normalized = "|".join(re.sub(r"\s+", " ", part.strip().lower()) for part in parts)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:24]


def _batch_key(payload: ScreeningBatchInput) -> str:
    if payload.batch_key:
        return payload.batch_key.strip()
    start = payload.window_start.isoformat() if payload.window_start else ""
    end = payload.window_end.isoformat() if payload.window_end else ""
    return f"screening-{_stable_key(payload.title, start, end)}"


def _item_key(payload: ScreeningItemInput) -> str:
    if payload.item_key:
        return payload.item_key.strip()
    published = payload.published_at.isoformat() if payload.published_at else ""
    return f"item-{_stable_key(payload.title, payload.source_name, published)}"


def _official_claim_kind(payload: ScreeningItemInput) -> str | None:
    text_value = " ".join((payload.title, payload.summary, payload.marginal_change, payload.evidence_summary))
    for kind, keywords in OFFICIAL_CLAIM_KEYWORDS.items():
        if any(keyword in text_value for keyword in keywords):
            return kind
    return None


def _related_stock_codes(payload: ScreeningItemInput) -> list[str]:
    text_value = " ".join(payload.related_stocks)
    return list(dict.fromkeys(re.findall(r"(?<!\d)(\d{6})(?!\d)", text_value)))


def _official_query_window(payload: ScreeningItemInput, batch: ScreeningBatchInput) -> tuple[date, date]:
    if payload.published_at:
        published = payload.published_at
        if published.tzinfo is not None:
            published = published.astimezone(SHANGHAI_TZ)
        day = published.date()
        if published.hour >= 18:
            return day, day + timedelta(days=1)
        if published.hour < 8:
            return day - timedelta(days=1), day
        return day, day
    if batch.window_start or batch.window_end:
        start_dt = batch.window_start or batch.window_end
        end_dt = batch.window_end or batch.window_start
        assert start_dt is not None and end_dt is not None
        if start_dt.tzinfo is not None:
            start_dt = start_dt.astimezone(SHANGHAI_TZ)
        if end_dt.tzinfo is not None:
            end_dt = end_dt.astimezone(SHANGHAI_TZ)
        start_day, end_day = sorted((start_dt.date(), end_dt.date()))
        if end_day - start_day > timedelta(days=3):
            start_day = end_day - timedelta(days=3)
        return start_day, end_day
    today = datetime.now(SHANGHAI_TZ).date()
    return today, today


def _run_official_guard(
    payload: ScreeningItemInput,
    batch: ScreeningBatchInput,
    db: Session,
    lookup: OfficialLookup,
) -> dict[str, Any]:
    kind = _official_claim_kind(payload)
    codes = _related_stock_codes(payload)
    if kind is None or not codes:
        return {
            "status": "not_required",
            "message": "该线索不属于带A股代码的强制公告核验类型。",
            "source_url": None,
            "stock_codes": codes,
        }
    official_hosts = ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn")
    official_urls = [url for url in [payload.source_url, *payload.source_urls] if url and any(host in url for host in official_hosts)]
    if official_urls:
        return {
            "status": "matched",
            "message": "输入已提供交易所或巨潮官方公告链接。",
            "source_url": official_urls[0],
            "stock_codes": codes,
        }

    start_date, end_date = _official_query_window(payload, batch)
    title_keywords = OFFICIAL_TITLE_KEYWORDS[kind]
    matched: list[dict[str, Any]] = []
    failures: list[str] = []
    completed = 0
    for code in codes:
        result = lookup(db, code, start_date, end_date)
        if result.get("status") in {"matched", "not_found"}:
            completed += 1
        elif result.get("message"):
            failures.append(str(result["message"]))
        for candidate in result.get("matches") or []:
            candidate_title = str(candidate.get("title") or "")
            if any(keyword in candidate_title for keyword in title_keywords):
                matched.append({**candidate, "stock_code": code})

    if matched:
        first = matched[0]
        text_value = " ".join((payload.title, payload.summary, payload.evidence_summary))
        if payload.verification_status == "unverified" or any(phrase in text_value for phrase in FALSE_NEGATIVE_PHRASES):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{payload.title}：官方源已命中公告《{first.get('title')}》。"
                    "禁止继续写成‘被传/公告待补/暂未验证’，请读取原文后改为已验证或部分验证。"
                ),
            )
        return {
            "status": "matched",
            "message": f"官方源命中公告《{first.get('title')}》。",
            "source_url": first.get("source_url"),
            "stock_codes": codes,
        }
    if failures and completed == 0:
        raise HTTPException(
            status_code=503,
            detail=f"{payload.title}：官方公告核验失败，已阻止写入。{'；'.join(failures)}",
        )
    return {
        "status": "not_found",
        "message": f"官方公告源已查询 {start_date.isoformat()}—{end_date.isoformat()}，未命中与该断言匹配的公告。",
        "source_url": None,
        "stock_codes": codes,
    }


def item_to_out(row: InformationScreeningItem) -> dict:
    result = {
        "id": row.id,
        "batch_id": row.batch_id,
        "item_key": row.item_key,
        "title": row.title,
        "summary": row.summary,
        "source_type": row.source_type,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "source_urls": _loads(row.source_urls_json, []),
        "published_at": row.published_at,
        "bucket": row.bucket,
        "is_top": row.is_top,
        "importance": row.importance,
        "marginal_change": row.marginal_change,
        "verification_status": row.verification_status,
        "evidence_summary": row.evidence_summary,
        "related_sectors": _loads(row.related_sectors_json, []),
        "related_stocks": _loads(row.related_stocks_json, []),
        "price_status": row.price_status,
        "price_summary": row.price_summary,
        "validation_points": _loads(row.validation_points_json, []),
        "invalidation_conditions": _loads(row.invalidation_conditions_json, []),
        "filter_reason": row.filter_reason,
        "recovery_condition": row.recovery_condition,
        "official_check_status": row.official_check_status,
        "official_check_message": row.official_check_message,
        "official_source_url": row.official_source_url,
        "official_stock_codes": _loads(row.official_stock_codes_json, []),
        "official_checked_at": row.official_checked_at,
        "promoted_chain_id": row.promoted_chain_id,
        "conversation_thread_id": row.conversation_thread_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    result["decision_policy"] = evaluate_information_policies(result)
    return result


def batch_to_out(row: InformationScreeningBatch, items: list[InformationScreeningItem] | None = None) -> dict:
    items = items if items is not None else list(row.items)
    verified = sum(item.bucket == "verified" for item in items)
    eye_catching = sum(item.bucket == "eye_catching" for item in items)
    filtered = sum(item.bucket == "filtered" for item in items)
    top = sum(item.is_top and item.bucket != "filtered" for item in items)
    official_required = sum(item.official_check_status != "not_required" for item in items)
    official_matched = sum(item.official_check_status == "matched" for item in items)
    official_unresolved = sum(item.official_check_status in {"not_found", "error", "missing_stock"} for item in items)
    return {
        "id": row.id,
        "batch_key": row.batch_key,
        "title": row.title,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "source_scope": _loads(row.source_scope_json, []),
        "read_count": row.read_count,
        "deduplicated_count": row.deduplicated_count,
        "conversation_thread_id": row.conversation_thread_id,
        "stats": {
            "read": row.read_count,
            "deduplicated": row.deduplicated_count,
            "top": top,
            "verified": verified,
            "eye_catching": eye_catching,
            "filtered": filtered,
            "official_required": official_required,
            "official_matched": official_matched,
            "official_unresolved": official_unresolved,
        },
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def feedback_to_out(row: InformationScreeningFeedback) -> dict:
    return {
        "id": row.id,
        "item_key": row.item_key,
        "batch_key": row.batch_key,
        "title": row.title,
        "source_type": row.source_type,
        "source_name": row.source_name,
        "bucket": row.bucket,
        "verification_status": row.verification_status,
        "action": row.action,
        "reason_category": row.reason_category,
        "note": row.note,
        "snapshot": _loads(row.snapshot_json, {}),
        "conversation_thread_id": row.conversation_thread_id,
        "created_at": row.created_at,
    }


def _apply_item(row: InformationScreeningItem, payload: ScreeningItemInput, official_check: dict[str, Any]) -> None:
    row.title = payload.title.strip()
    row.summary = payload.summary.strip()
    row.source_type = payload.source_type.strip() or "other"
    row.source_name = payload.source_name.strip()
    row.source_url = payload.source_url
    row.source_urls_json = _dumps(payload.source_urls)
    row.published_at = payload.published_at
    row.bucket = payload.bucket
    row.is_top = payload.is_top and payload.bucket != "filtered"
    row.importance = payload.importance
    row.marginal_change = payload.marginal_change.strip()
    row.verification_status = payload.verification_status
    row.evidence_summary = payload.evidence_summary.strip()
    row.related_sectors_json = _dumps(payload.related_sectors)
    row.related_stocks_json = _dumps(payload.related_stocks)
    row.price_status = payload.price_status
    row.price_summary = payload.price_summary.strip()
    row.validation_points_json = _dumps(payload.validation_points)
    row.invalidation_conditions_json = _dumps(payload.invalidation_conditions)
    row.filter_reason = payload.filter_reason.strip()
    row.recovery_condition = payload.recovery_condition.strip()
    row.official_check_status = str(official_check.get("status") or "not_required")
    row.official_check_message = str(official_check.get("message") or "")
    row.official_source_url = official_check.get("source_url")
    row.official_stock_codes_json = _dumps(official_check.get("stock_codes") or [])
    row.official_checked_at = now_utc() if row.official_check_status != "not_required" else None
    row.conversation_thread_id = payload.conversation_thread_id
    row.updated_at = now_utc()


def upsert_batch(
    payload: ScreeningBatchInput,
    db: Session,
    official_lookup: OfficialLookup = lookup_cninfo_announcements,
) -> dict:
    key = _batch_key(payload)
    batch = db.scalar(select(InformationScreeningBatch).where(InformationScreeningBatch.batch_key == key))
    created = batch is None
    if batch is None:
        batch = InformationScreeningBatch(batch_key=key, title=payload.title.strip())
        db.add(batch)
        db.flush()
    batch.title = payload.title.strip()
    batch.window_start = payload.window_start
    batch.window_end = payload.window_end
    batch.source_scope_json = _dumps(payload.source_scope)
    batch.read_count = payload.read_count
    batch.deduplicated_count = payload.deduplicated_count
    batch.conversation_thread_id = payload.conversation_thread_id
    batch.updated_at = now_utc()

    created_items = 0
    updated_items = 0
    suppressed_items = 0
    saved_items: list[InformationScreeningItem] = []
    for item_payload in payload.items:
        official_check = _run_official_guard(item_payload, payload, db, official_lookup)
        key_item = _item_key(item_payload)
        if not payload.restore_deleted:
            deleted_before = db.scalar(
                select(InformationScreeningFeedback.id).where(
                    InformationScreeningFeedback.item_key == key_item,
                    InformationScreeningFeedback.action == "manual_delete",
                )
            )
            if deleted_before is not None:
                suppressed_items += 1
                continue
        item = db.scalar(
            select(InformationScreeningItem).where(
                InformationScreeningItem.batch_id == batch.id,
                InformationScreeningItem.item_key == key_item,
            )
        )
        if item is None:
            item = InformationScreeningItem(batch_id=batch.id, item_key=key_item, title=item_payload.title)
            db.add(item)
            created_items += 1
        else:
            updated_items += 1
        _apply_item(item, item_payload, official_check)
        saved_items.append(item)

    db.commit()
    db.refresh(batch)
    for item in saved_items:
        db.refresh(item)
    all_items = db.scalars(
        select(InformationScreeningItem)
        .where(InformationScreeningItem.batch_id == batch.id)
        .order_by(desc(InformationScreeningItem.is_top), desc(InformationScreeningItem.importance), desc(InformationScreeningItem.published_at), desc(InformationScreeningItem.id))
    ).all()
    return {
        "status": "ok",
        "created": created,
        "created_items": created_items,
        "updated_items": updated_items,
        "suppressed_items": suppressed_items,
        "batch": batch_to_out(batch, list(all_items)),
        "items": [item_to_out(item) for item in all_items],
    }


@router.post("/batches")
def create_or_update_batch(payload: ScreeningBatchInput, db: Session = Depends(get_db)) -> dict:
    return upsert_batch(payload, db)


@router.get("")
def get_overview(
    batch_id: int | None = Query(default=None),
    batch_limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    batches = db.scalars(
        select(InformationScreeningBatch)
        .order_by(desc(InformationScreeningBatch.window_end), desc(InformationScreeningBatch.created_at))
        .limit(batch_limit)
    ).all()
    current = db.get(InformationScreeningBatch, batch_id) if batch_id is not None else (batches[0] if batches else None)
    if batch_id is not None and current is None:
        raise HTTPException(status_code=404, detail="筛选批次不存在")
    if current is None:
        return {"status": "ok", "batch": None, "batches": [], "items": []}
    items = db.scalars(
        select(InformationScreeningItem)
        .where(InformationScreeningItem.batch_id == current.id)
        .order_by(desc(InformationScreeningItem.is_top), desc(InformationScreeningItem.importance), desc(InformationScreeningItem.published_at), desc(InformationScreeningItem.id))
    ).all()
    return {
        "status": "ok",
        "batch": batch_to_out(current, list(items)),
        "batches": [batch_to_out(batch, list(items) if batch.id == current.id else None) for batch in batches],
        "items": [item_to_out(item) for item in items],
    }


@router.get("/feedback")
def list_feedback(
    limit: int = Query(default=500, ge=1, le=1000),
    keyword: str | None = Query(default=None, max_length=120),
    reason_category: str | None = Query(default=None, max_length=48),
    db: Session = Depends(get_db),
) -> dict:
    statement = select(InformationScreeningFeedback).where(
        InformationScreeningFeedback.action == "manual_delete"
    )
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        statement = statement.where(
            or_(
                InformationScreeningFeedback.title.ilike(pattern),
                InformationScreeningFeedback.note.ilike(pattern),
                InformationScreeningFeedback.source_name.ilike(pattern),
            )
        )
    if reason_category and reason_category.strip():
        statement = statement.where(InformationScreeningFeedback.reason_category == reason_category.strip())
    rows = db.scalars(statement.order_by(desc(InformationScreeningFeedback.created_at)).limit(limit)).all()
    return {"status": "ok", "items": [feedback_to_out(row) for row in rows]}


@router.patch("/items/{item_id}")
def patch_item(item_id: int, payload: ScreeningItemPatch, db: Session = Depends(get_db)) -> dict:
    row = db.get(InformationScreeningItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="筛选信息不存在")
    values = payload.model_dump(exclude_unset=True)
    json_fields = {
        "validation_points": "validation_points_json",
        "invalidation_conditions": "invalidation_conditions_json",
    }
    for key, value in values.items():
        if key in json_fields:
            setattr(row, json_fields[key], _dumps(value))
        else:
            setattr(row, key, value)
    if row.bucket == "filtered":
        row.is_top = False
        if not row.filter_reason.strip():
            raise HTTPException(status_code=400, detail="已读过滤项必须填写过滤原因")
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return {"status": "ok", "item": item_to_out(row)}


@router.delete("/items/{item_id}")
def delete_item(
    item_id: int,
    payload: ScreeningDeleteInput | None = None,
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(InformationScreeningItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="筛选信息不存在")
    snapshot = item_to_out(row)
    batch = db.get(InformationScreeningBatch, row.batch_id)
    reason_category = (payload.reason_category or "").strip() if payload else ""
    note = (payload.note or "").strip() if payload else ""
    db.add(
        InformationScreeningFeedback(
            item_key=row.item_key,
            batch_key=batch.batch_key if batch else "",
            title=row.title,
            source_type=row.source_type,
            source_name=row.source_name,
            bucket=row.bucket,
            verification_status=row.verification_status,
            action="manual_delete",
            reason_category=reason_category or None,
            note=note or None,
            snapshot_json=_dumps(snapshot),
            conversation_thread_id=row.conversation_thread_id,
        )
    )
    db.delete(row)
    db.commit()
    return {
        "status": "ok",
        "message": "卡片已删除，完整快照和原因已写入筛选反馈",
        "feedback_rule": reason_category or "未分类",
    }


@router.post("/items/{item_id}/promote")
def promote_item(item_id: int, payload: PromotionInput, db: Session = Depends(get_db)) -> dict:
    row = db.get(InformationScreeningItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="筛选信息不存在")
    if row.bucket == "filtered":
        raise HTTPException(status_code=400, detail="已过滤信息需先恢复后再转入产业趋势")
    chain = db.get(IndustryChain, payload.chain_id)
    if chain is None:
        raise HTTPException(status_code=404, detail="产业不存在")
    if row.promoted_chain_id == chain.id:
        return {"status": "ok", "message": "该信息已转入这个产业", "item": item_to_out(row)}

    source_url = row.source_url or next(iter(_loads(row.source_urls_json, [])), None)
    validation_points = _loads(row.validation_points_json, [])
    is_verified = row.bucket == "verified" or row.verification_status in {"verified", "cross_verified"}
    effective_date: date = (row.published_at or now_utc()).date()
    if is_verified:
        db.add(
            IndustryChainEvidence(
                chain_id=chain.id,
                title=row.title,
                content=row.evidence_summary or row.summary,
                source_name=row.source_name or row.source_type,
                source_url=source_url,
                impact_level="强" if row.importance >= 4 else "中",
                evidence_date=effective_date,
            )
        )
        db.add(
            IndustryTrendUpdate(
                chain_id=chain.id,
                update_date=effective_date,
                content=row.marginal_change or row.summary or row.title,
                source_name=row.source_name or row.source_type,
                source_url=source_url,
                impact=row.price_summary or row.evidence_summary,
                next_verification="；".join(validation_points),
            )
        )
        promotion_type = "evidence_and_update"
    else:
        db.add(
            IndustryChainTask(
                chain_id=chain.id,
                title=f"验证：{row.title}"[:240],
                description=row.summary,
                criteria="；".join(validation_points) or row.recovery_condition,
                source_name=row.source_name or row.source_type,
                source_url=source_url,
                priority="高" if row.importance >= 4 else "中",
                status="验证中",
            )
        )
        promotion_type = "validation_task"

    row.promoted_chain_id = chain.id
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return {
        "status": "ok",
        "message": f"已转入产业趋势：{chain.name}",
        "promotion_type": promotion_type,
        "item": item_to_out(row),
    }

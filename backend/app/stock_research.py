from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    AStock,
    FactorQuoteSnapshot,
    FactorStockTag,
    FactorTag,
    StockResearchEvidence,
    StockResearchVersion,
    StockResearchWorkspace,
    now_utc,
)
from app.research_runs import ensure_stock, stock_relations
from app.watchlist_announcements import search_a_stocks


router = APIRouter(tags=["stock-research"])

RESEARCH_STATUSES = {"待研究", "跟踪中", "重点跟踪", "已验证", "已证伪", "暂不跟踪"}
EVIDENCE_CATEGORIES = {"hard_fact", "xueqiu_clue", "market_hypothesis", "tape_confirmation"}
EVIDENCE_STATUSES = {"待确认", "已确认", "部分确认", "已证伪"}
WORKSPACE_FIELDS = (
    "research_status",
    "current_conclusion",
    "chart_expression",
    "fundamental_change",
    "market_trading",
    "industry_position",
    "risks_and_invalidation",
    "next_validation",
)


class ResearchWorkspacePayload(BaseModel):
    research_status: str = Field(default="待研究", max_length=32)
    current_conclusion: str = ""
    chart_expression: str = ""
    fundamental_change: str = ""
    market_trading: str = ""
    industry_position: str = ""
    risks_and_invalidation: str = ""
    next_validation: str = ""
    tag_names: list[str] = Field(default_factory=list, max_length=80)


class ResearchEvidencePayload(BaseModel):
    category: str = Field(max_length=32)
    confirmation_status: str = Field(default="待确认", max_length=24)
    evidence_date: date | None = None
    title: str = Field(min_length=1, max_length=240)
    content: str = ""
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None


class ResearchEvidencePatch(BaseModel):
    category: str | None = Field(default=None, max_length=32)
    confirmation_status: str | None = Field(default=None, max_length=24)
    evidence_date: date | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    content: str | None = None
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None


def _clean_tag_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = " ".join(str(raw or "").replace("#", "").split())[:80]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def _tag_names(db: Session, full_code: str) -> list[str]:
    rows = db.execute(
        select(FactorTag.name)
        .join(FactorStockTag, FactorStockTag.tag_id == FactorTag.id)
        .where(FactorStockTag.full_code == full_code)
        .order_by(FactorTag.name)
    ).all()
    return [row[0] for row in rows]


def _replace_tags(db: Session, full_code: str, names: list[str]) -> list[str]:
    clean_names = _clean_tag_names(names)
    tags: list[FactorTag] = []
    for name in clean_names:
        tag = db.scalar(select(FactorTag).where(FactorTag.name == name))
        if not tag:
            tag = FactorTag(name=name)
            db.add(tag)
            db.flush()
        tags.append(tag)
    db.execute(delete(FactorStockTag).where(FactorStockTag.full_code == full_code))
    for tag in tags:
        db.add(FactorStockTag(full_code=full_code, tag_id=tag.id))
    db.flush()
    return [tag.name for tag in tags]


def _evidence_out(row: StockResearchEvidence) -> dict[str, Any]:
    return {
        "id": row.id,
        "category": row.category,
        "confirmation_status": row.confirmation_status,
        "evidence_date": row.evidence_date,
        "title": row.title,
        "content": row.content,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _evidence_rows(db: Session, workspace_id: int) -> list[StockResearchEvidence]:
    return list(
        db.scalars(
            select(StockResearchEvidence)
            .where(StockResearchEvidence.workspace_id == workspace_id)
            .order_by(desc(StockResearchEvidence.evidence_date), desc(StockResearchEvidence.updated_at))
        ).all()
    )


def _workspace_snapshot(db: Session, workspace: StockResearchWorkspace) -> dict[str, Any]:
    return {
        "workspace": {field: getattr(workspace, field) for field in WORKSPACE_FIELDS},
        "tag_names": _tag_names(db, workspace.full_code),
        "evidence": [_evidence_out(row) for row in _evidence_rows(db, workspace.id)],
    }


def _workspace_out(workspace: StockResearchWorkspace | None) -> dict[str, Any] | None:
    if not workspace:
        return None
    return {
        "id": workspace.id,
        **{field: getattr(workspace, field) for field in WORKSPACE_FIELDS},
        "revision": workspace.revision,
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
    }


def _version_out(row: StockResearchVersion, include_snapshot: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row.id,
        "revision": row.revision,
        "created_at": row.created_at,
    }
    if include_snapshot:
        result["snapshot"] = json.loads(row.snapshot_json)
    return result


def _save_version(db: Session, workspace: StockResearchWorkspace) -> StockResearchVersion:
    version = StockResearchVersion(
        workspace_id=workspace.id,
        revision=workspace.revision,
        snapshot_json=json.dumps(_workspace_snapshot(db, workspace), ensure_ascii=False, default=str),
    )
    db.add(version)
    db.flush()
    return version


def _workspace_or_404(db: Session, full_code: str) -> StockResearchWorkspace:
    workspace = db.scalar(select(StockResearchWorkspace).where(StockResearchWorkspace.full_code == full_code.upper()))
    if not workspace:
        raise HTTPException(status_code=404, detail="该股票还没有投研工作区")
    return workspace


def _research_bundle(db: Session, full_code: str) -> dict[str, Any]:
    stock = ensure_stock(db, full_code)
    workspace = db.scalar(select(StockResearchWorkspace).where(StockResearchWorkspace.full_code == stock.full_code))
    snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == stock.full_code))
    evidence = _evidence_rows(db, workspace.id) if workspace else []
    relations = stock_relations(db, stock)
    for relation in relations:
        if relation.get("link", "").startswith("/market-style"):
            relation["link"] = "/sector-indices"
    return {
        "stock": {
            "code": stock.code,
            "name": stock.name,
            "exchange": stock.exchange,
            "full_code": stock.full_code,
            "latest_price": snapshot.latest_price if snapshot else None,
            "change_pct": snapshot.change_pct if snapshot else None,
            "quote_date": snapshot.trade_date if snapshot else None,
            "quote_updated_at": snapshot.updated_at if snapshot else None,
        },
        "workspace": _workspace_out(workspace),
        "tag_names": _tag_names(db, stock.full_code),
        "relations": relations,
        "evidence": [_evidence_out(row) for row in evidence],
    }


@router.get("/api/stocks/search")
def search_stocks(
    keyword: str = Query(default="", max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return search_a_stocks(db, keyword, limit)


@router.get("/api/research/stocks")
def recent_research_stocks(
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=40, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = select(StockResearchWorkspace)
    keyword = query.strip()
    if keyword:
        statement = statement.where(
            or_(
                StockResearchWorkspace.code.like(f"%{keyword}%"),
                StockResearchWorkspace.full_code.like(f"%{keyword.upper()}%"),
                StockResearchWorkspace.name.like(f"%{keyword}%"),
            )
        )
    rows = list(db.scalars(statement.order_by(desc(StockResearchWorkspace.updated_at)).limit(limit)).all())
    return {
        "items": [
            {
                "code": row.code,
                "name": row.name,
                "exchange": row.exchange,
                "full_code": row.full_code,
                "research_status": row.research_status,
                "current_conclusion": row.current_conclusion,
                "revision": row.revision,
                "updated_at": row.updated_at,
                "tag_names": _tag_names(db, row.full_code),
            }
            for row in rows
        ]
    }


@router.get("/api/stocks/{full_code}/research")
def get_stock_research(full_code: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return _research_bundle(db, full_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/api/stocks/{full_code}/research")
def save_stock_research(
    full_code: str,
    payload: ResearchWorkspacePayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.research_status not in RESEARCH_STATUSES:
        raise HTTPException(status_code=400, detail="研究状态不正确")
    try:
        stock = ensure_stock(db, full_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    workspace = db.scalar(select(StockResearchWorkspace).where(StockResearchWorkspace.full_code == stock.full_code))
    if not workspace:
        workspace = StockResearchWorkspace(
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            full_code=stock.full_code,
        )
        db.add(workspace)
        db.flush()
    for field in WORKSPACE_FIELDS:
        setattr(workspace, field, getattr(payload, field))
    workspace.name = stock.name
    workspace.revision += 1
    workspace.updated_at = now_utc()
    _replace_tags(db, stock.full_code, payload.tag_names)
    _save_version(db, workspace)
    db.commit()
    return _research_bundle(db, stock.full_code)


@router.post("/api/stocks/{full_code}/research/evidence")
def create_research_evidence(
    full_code: str,
    payload: ResearchEvidencePayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.category not in EVIDENCE_CATEGORIES or payload.confirmation_status not in EVIDENCE_STATUSES:
        raise HTTPException(status_code=400, detail="证据分类或确认状态不正确")
    workspace = _workspace_or_404(db, full_code)
    row = StockResearchEvidence(
        workspace_id=workspace.id,
        full_code=workspace.full_code,
        **payload.model_dump(),
    )
    db.add(row)
    workspace.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return _evidence_out(row)


@router.patch("/api/stocks/{full_code}/research/evidence/{evidence_id}")
def update_research_evidence(
    full_code: str,
    evidence_id: int,
    payload: ResearchEvidencePatch,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _workspace_or_404(db, full_code)
    row = db.scalar(
        select(StockResearchEvidence).where(
            StockResearchEvidence.id == evidence_id,
            StockResearchEvidence.workspace_id == workspace.id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="证据不存在")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("category") not in EVIDENCE_CATEGORIES and "category" in changes:
        raise HTTPException(status_code=400, detail="证据分类不正确")
    if changes.get("confirmation_status") not in EVIDENCE_STATUSES and "confirmation_status" in changes:
        raise HTTPException(status_code=400, detail="确认状态不正确")
    for field, value in changes.items():
        setattr(row, field, value)
    row.updated_at = now_utc()
    workspace.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return _evidence_out(row)


@router.delete("/api/stocks/{full_code}/research/evidence/{evidence_id}")
def delete_research_evidence(full_code: str, evidence_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    workspace = _workspace_or_404(db, full_code)
    row = db.scalar(
        select(StockResearchEvidence).where(
            StockResearchEvidence.id == evidence_id,
            StockResearchEvidence.workspace_id == workspace.id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="证据不存在")
    db.delete(row)
    workspace.updated_at = now_utc()
    db.commit()
    return {"status": "ok", "message": "证据已删除"}


@router.get("/api/stocks/{full_code}/research/versions")
def list_research_versions(full_code: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = _workspace_or_404(db, full_code)
    rows = db.scalars(
        select(StockResearchVersion)
        .where(StockResearchVersion.workspace_id == workspace.id)
        .order_by(desc(StockResearchVersion.revision))
    ).all()
    return {"items": [_version_out(row, include_snapshot=True) for row in rows]}


@router.post("/api/stocks/{full_code}/research/versions/{version_id}/restore")
def restore_research_version(full_code: str, version_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = _workspace_or_404(db, full_code)
    version = db.scalar(
        select(StockResearchVersion).where(
            StockResearchVersion.id == version_id,
            StockResearchVersion.workspace_id == workspace.id,
        )
    )
    if not version:
        raise HTTPException(status_code=404, detail="历史版本不存在")
    snapshot = json.loads(version.snapshot_json)
    for field in WORKSPACE_FIELDS:
        if field in snapshot.get("workspace", {}):
            setattr(workspace, field, snapshot["workspace"][field])
    _replace_tags(db, workspace.full_code, snapshot.get("tag_names", []))
    db.execute(delete(StockResearchEvidence).where(StockResearchEvidence.workspace_id == workspace.id))
    for item in snapshot.get("evidence", []):
        raw_date = item.get("evidence_date")
        parsed_date = date.fromisoformat(raw_date) if isinstance(raw_date, str) and raw_date else raw_date
        db.add(
            StockResearchEvidence(
                workspace_id=workspace.id,
                full_code=workspace.full_code,
                category=item.get("category", "hard_fact"),
                confirmation_status=item.get("confirmation_status", "待确认"),
                evidence_date=parsed_date,
                title=item.get("title") or "未命名证据",
                content=item.get("content") or "",
                source_name=item.get("source_name"),
                source_url=item.get("source_url"),
            )
        )
    workspace.revision += 1
    workspace.updated_at = now_utc()
    db.flush()
    _save_version(db, workspace)
    db.commit()
    return _research_bundle(db, workspace.full_code)


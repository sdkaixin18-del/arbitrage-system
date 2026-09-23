from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.factors import extract_auto_tags_from_text, normalize_auto_tag_list, normalize_auto_tag_name, parse_returns, tags_for_full_codes
from app.models import (
    AStock,
    EditedNewsItem,
    FactorQuoteSnapshot,
    FactorExcludedStock,
    FactorRefreshRun,
    FactorReviewBatch,
    FactorReviewItem,
    FactorReviewVersion,
    FactorStockTag,
    FactorTag,
    IndustryChain,
    IndustryChainCompany,
    IndustryChainSegment,
    MarketReviewMaterial,
    MarketStyleThsBar,
    MarketStyleThsMember,
    StockResearchEvidence,
    StockDailyBar,
    WatchlistAnnouncementItem,
    XueqiuPost,
    XueqiuRecommendation,
    now_utc,
)


router = APIRouter(prefix="/api/factor-review", tags=["factor-review"])

STAGES = {"initial_pending", "second_pending", "confirmed"}
EVIDENCE_CATEGORIES = {"hard_fact", "zsxq", "xueqiu", "market_guess", "market_confirmation"}
THS_CONTEXT_ONLY = {
    "融资融券", "深股通", "沪股通", "转融券标的", "注册制次新股", "创业板综",
    "MSCI", "富时罗素", "标普道琼斯A股", "同花顺漂亮100", "年报预增", "季报预增",
}
NON_FACTOR_TAGS = {
    "涨幅榜前200", "成交额榜前200", "双榜重合", "高成交额", "强势股", "活跃成交",
    "容量核心", "行业龙头候选", "盘面异动待验证", "小作文叙事",
}


class FactorReviewBatchCreate(BaseModel):
    trade_date: date | None = None
    gain_limit: int = Field(default=200, ge=1, le=500)
    amount_limit: int = Field(default=200, ge=1, le=500)


class FactorReviewDraftUpdate(BaseModel):
    tag_names: list[str] = Field(default_factory=list, max_length=50)


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in raw] if isinstance(raw, list) else []


def parse_json_objects(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def tag_supports_from_evidence(evidence: list[dict[str, Any]], suggested_tags: list[str] | None = None) -> list[dict[str, Any]]:
    weights = {"hard_fact": 3.0, "zsxq": 2.0, "xueqiu": 1.0, "market_guess": 1.25, "market_confirmation": 1.0}
    supports: dict[str, dict[str, Any]] = {}
    for row in evidence:
        category = str(row.get("category") or "market_guess")
        source_name = str(row.get("source_name") or row.get("source_type") or "未知来源")
        for tag in row.get("tags") or []:
            clean = normalize_auto_tag_name(str(tag))
            if not clean or clean in NON_FACTOR_TAGS:
                continue
            bucket = supports.setdefault(clean, {"tag": clean, "score": 0.0, "evidence_count": 0, "categories": set(), "sources": set()})
            bucket["score"] += weights.get(category, 1.0)
            bucket["evidence_count"] += 1
            bucket["categories"].add(category)
            bucket["sources"].add(source_name)
    rows: list[dict[str, Any]] = []
    allowed = set(suggested_tags or []) if suggested_tags is not None else None
    for tag, bucket in supports.items():
        if allowed is not None and tag not in allowed:
            continue
        categories = set(bucket["categories"])
        if "hard_fact" in categories:
            strength = "硬证据"
        elif len(categories) >= 2:
            strength = "多源支持"
        elif "zsxq" in categories:
            strength = "星球线索"
        elif "xueqiu" in categories:
            strength = "雪球线索"
        elif "market_confirmation" in categories:
            strength = "盘面支持"
        else:
            strength = "资料线索"
        rows.append({
            "tag": tag,
            "strength": strength,
            "score": round(float(bucket["score"]), 2),
            "evidence_count": int(bucket["evidence_count"]),
            "categories": sorted(categories),
            "sources": sorted(bucket["sources"])[:6],
        })
    rows.sort(key=lambda row: (-row["score"], -row["evidence_count"], row["tag"]))
    return rows


def dump_json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def normalize_tag_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = " ".join(str(raw or "").replace("#", "").split())[:80]
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def effective_tags_map(db: Session, full_codes: list[str]) -> dict[str, list[str]]:
    if not full_codes:
        return {}
    rows = db.execute(
        select(FactorStockTag.full_code, FactorTag.name)
        .join(FactorTag, FactorTag.id == FactorStockTag.tag_id)
        .where(FactorStockTag.full_code.in_(sorted(set(full_codes))))
        .order_by(FactorTag.name)
    ).all()
    result: dict[str, list[str]] = {}
    for full_code, name in rows:
        result.setdefault(str(full_code), []).append(str(name))
    return result


def item_to_out(row: FactorReviewItem, effective_tags: list[str] | None = None) -> dict[str, Any]:
    evidence = parse_json_objects(row.evidence_json)
    suggested_tags = parse_json_list(row.suggested_tags_json)
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "code": row.code,
        "name": row.name,
        "exchange": row.exchange,
        "full_code": row.full_code,
        "change_pct": row.change_pct,
        "amount": row.amount,
        "gain_rank": row.gain_rank,
        "amount_rank": row.amount_rank,
        "reasons": parse_json_list(row.reasons_json),
        "market_tags": parse_json_list(row.market_tags_json),
        "suggested_tags": suggested_tags,
        "tag_supports": tag_supports_from_evidence(evidence, suggested_tags),
        "evidence": evidence,
        "source_statuses": parse_json_objects(row.source_status_json),
        "research_status": row.research_status,
        "researched_at": row.researched_at,
        "initial_tags": parse_json_list(row.initial_tags_json),
        "draft_tags": parse_json_list(row.draft_tags_json),
        "confirmed_tags": parse_json_list(row.confirmed_tags_json),
        "effective_tags": effective_tags or [],
        "stage": row.stage,
        "revision": row.revision,
        "initial_completed_at": row.initial_completed_at,
        "confirmed_at": row.confirmed_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def batch_counts(db: Session, batch_id: int) -> dict[str, int]:
    rows = db.execute(
        select(FactorReviewItem.stage, func.count(FactorReviewItem.id))
        .where(FactorReviewItem.batch_id == batch_id)
        .group_by(FactorReviewItem.stage)
    ).all()
    counts = {str(stage): int(count or 0) for stage, count in rows}
    return {stage: counts.get(stage, 0) for stage in STAGES}


def batch_to_out(db: Session, batch: FactorReviewBatch, include_items: bool = False) -> dict[str, Any]:
    counts = batch_counts(db, batch.id)
    result: dict[str, Any] = {
        "id": batch.id,
        "trade_date": batch.trade_date,
        "gain_limit": batch.gain_limit,
        "amount_limit": batch.amount_limit,
        "status": batch.status,
        "source_status": batch.source_status,
        "source_message": batch.source_message,
        "stock_count": batch.stock_count,
        "initial_pending_count": counts["initial_pending"],
        "second_pending_count": counts["second_pending"],
        "confirmed_count": counts["confirmed"],
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }
    if include_items:
        items = list(
            db.scalars(
                select(FactorReviewItem)
                .where(FactorReviewItem.batch_id == batch.id)
                .order_by(
                    FactorReviewItem.stage,
                    FactorReviewItem.gain_rank.is_(None),
                    FactorReviewItem.gain_rank,
                    FactorReviewItem.amount_rank.is_(None),
                    FactorReviewItem.amount_rank,
                )
            ).all()
        )
        effective_map = effective_tags_map(db, [item.full_code for item in items])
        result["items"] = [item_to_out(item, effective_map.get(item.full_code, [])) for item in items]
    return result


def latest_trade_date(db: Session) -> date:
    value = db.scalar(select(func.max(FactorQuoteSnapshot.trade_date)))
    if not value:
        raise ValueError("还没有可用的A股行情日期")
    return value


def one_day_return(snapshot: FactorQuoteSnapshot) -> float | None:
    value = parse_returns(snapshot.returns_json).get("1")
    return float(value) if value is not None else snapshot.change_pct


def market_tags_for_item(gain_rank: int | None, amount_rank: int | None, amount: float | None) -> list[str]:
    tags: list[str] = []
    if gain_rank:
        tags.append("涨幅榜前200")
    if amount_rank:
        tags.append("成交额榜前200")
    if gain_rank and amount_rank:
        tags.append("双榜重合")
    if amount is not None and amount >= 2_000_000_000:
        tags.append("高成交额")
    return tags


def create_factor_review_batch(db: Session, payload: FactorReviewBatchCreate) -> tuple[dict[str, Any], bool]:
    trade_date = payload.trade_date or latest_trade_date(db)
    existing = db.scalar(
        select(FactorReviewBatch).where(
            FactorReviewBatch.trade_date == trade_date,
            FactorReviewBatch.gain_limit == payload.gain_limit,
            FactorReviewBatch.amount_limit == payload.amount_limit,
        )
    )
    if existing:
        return batch_to_out(db, existing, include_items=True), False

    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    snapshots_query = select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.trade_date == trade_date)
    if excluded_codes:
        snapshots_query = snapshots_query.where(~FactorQuoteSnapshot.full_code.in_(excluded_codes))
    snapshots = list(db.scalars(snapshots_query).all())
    if not snapshots:
        raise ValueError(f"{trade_date} 没有可用的A股行情")
    full_codes = [snapshot.full_code for snapshot in snapshots]
    amount_rows = db.execute(
        select(StockDailyBar.full_code, StockDailyBar.amount).where(
            StockDailyBar.trade_date == trade_date,
            StockDailyBar.full_code.in_(full_codes),
            StockDailyBar.amount.is_not(None),
        )
    ).all()
    amount_map = {str(full_code): float(amount) for full_code, amount in amount_rows if amount is not None}
    gain_rows = [snapshot for snapshot in snapshots if one_day_return(snapshot) is not None]
    gain_rows.sort(key=lambda snapshot: one_day_return(snapshot) or 0, reverse=True)
    amount_rank_rows = [snapshot for snapshot in snapshots if snapshot.full_code in amount_map]
    amount_rank_rows.sort(key=lambda snapshot: amount_map[snapshot.full_code], reverse=True)
    gain_top = gain_rows[: payload.gain_limit]
    amount_top = amount_rank_rows[: payload.amount_limit]

    selected: dict[str, dict[str, Any]] = {}
    for rank, snapshot in enumerate(gain_top, start=1):
        selected.setdefault(snapshot.full_code, {"snapshot": snapshot, "gain_rank": None, "amount_rank": None})["gain_rank"] = rank
    for rank, snapshot in enumerate(amount_top, start=1):
        selected.setdefault(snapshot.full_code, {"snapshot": snapshot, "gain_rank": None, "amount_rank": None})["amount_rank"] = rank

    latest_run = db.scalar(select(FactorRefreshRun).order_by(desc(FactorRefreshRun.started_at)).limit(1))
    batch = FactorReviewBatch(
        trade_date=trade_date,
        gain_limit=payload.gain_limit,
        amount_limit=payload.amount_limit,
        source_status=latest_run.status if latest_run else "local_quotes_only",
        source_message=latest_run.message if latest_run else "使用本地行情缓存生成",
        stock_count=len(selected),
    )
    db.add(batch)
    db.flush()
    existing_tag_map = tags_for_full_codes(db, list(selected))
    for data in selected.values():
        snapshot: FactorQuoteSnapshot = data["snapshot"]
        reasons: list[str] = []
        if data["gain_rank"]:
            reasons.append(f"涨幅前{payload.gain_limit} #{data['gain_rank']}")
        if data["amount_rank"]:
            reasons.append(f"成交额前{payload.amount_limit} #{data['amount_rank']}")
        existing_names = [tag.name for tag in existing_tag_map.get(snapshot.full_code, [])]
        market_tags = market_tags_for_item(data["gain_rank"], data["amount_rank"], amount_map.get(snapshot.full_code))
        db.add(
            FactorReviewItem(
                batch_id=batch.id,
                code=snapshot.code,
                name=snapshot.name,
                exchange=snapshot.exchange,
                full_code=snapshot.full_code,
                change_pct=one_day_return(snapshot),
                amount=amount_map.get(snapshot.full_code),
                gain_rank=data["gain_rank"],
                amount_rank=data["amount_rank"],
                reasons_json=dump_json_list(reasons),
                market_tags_json=dump_json_list(market_tags),
                suggested_tags_json=dump_json_list(existing_names),
                draft_tags_json=dump_json_list(existing_names),
            )
        )
    db.commit()
    db.refresh(batch)
    return batch_to_out(db, batch, include_items=True), True


def evidence_item(
    category: str,
    source_type: str,
    source_name: str,
    title: str,
    content: str,
    published_at: date | datetime | str | None = None,
    link: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    if isinstance(published_at, (date, datetime)):
        published_value = published_at.isoformat()
    else:
        published_value = published_at
    return {
        "category": category if category in EVIDENCE_CATEGORIES else "market_guess",
        "source_type": source_type,
        "source_name": source_name,
        "title": " ".join(str(title or source_name).split())[:160],
        "content": " ".join(str(content or "").split())[:1200],
        "published_at": published_value,
        "link": link,
        "tags": normalize_auto_tag_list(tags or [])[:8],
    }


def stock_text_matches(item: FactorReviewItem, value: str | None) -> bool:
    text_value = str(value or "")
    lowered = text_value.casefold()
    return bool(
        (item.name and item.name in text_value)
        or (item.code and item.code.casefold() in lowered)
        or (item.full_code and item.full_code.casefold() in lowered)
    )


def stock_research_category(value: str | None) -> str:
    raw = str(value or "").lower()
    if "hard" in raw or "事实" in raw:
        return "hard_fact"
    if "雪球" in raw or "xueqiu" in raw:
        return "xueqiu"
    if "星球" in raw or "zsxq" in raw:
        return "zsxq"
    if "盘面" in raw or "confirm" in raw:
        return "market_confirmation"
    return "market_guess"


def refresh_factor_review_research(db: Session, item_id: int, include_live_zsxq: bool = True) -> dict[str, Any]:
    item = item_or_error(db, item_id)
    batch = db.get(FactorReviewBatch, item.batch_id)
    if not batch:
        raise ValueError("复核批次不存在")
    cutoff_date = batch.trade_date - timedelta(days=30)
    cutoff_at = datetime.combine(cutoff_date, time.min, tzinfo=timezone.utc)
    evidence: list[dict[str, Any]] = []
    seen_evidence: set[tuple[str, str, str]] = set()

    def append_evidence(record: dict[str, Any]) -> None:
        key = (
            str(record.get("category") or ""),
            str(record.get("source_name") or ""),
            str(record.get("title") or record.get("content") or "")[:160],
        )
        if key in seen_evidence:
            return
        category_count = sum(1 for row in evidence if row.get("category") == record.get("category"))
        if category_count >= 10 or len(evidence) >= 45:
            return
        seen_evidence.add(key)
        evidence.append(record)

    market_tags = market_tags_for_item(item.gain_rank, item.amount_rank, item.amount)
    append_evidence(
        evidence_item(
            "market_confirmation",
            "market",
            "A股行情",
            "当日盘面表达",
            f"{item.name} 当日涨幅 {item.change_pct if item.change_pct is not None else '-'}%；"
            f"涨幅榜排名 {item.gain_rank or '-'}，成交额榜排名 {item.amount_rank or '-'}。",
            batch.trade_date,
            tags=market_tags,
        )
    )

    memberships = list(
        db.scalars(
            select(MarketStyleThsMember)
            .where(MarketStyleThsMember.full_code == item.full_code)
            .order_by(MarketStyleThsMember.group_type, MarketStyleThsMember.group_name)
        ).all()
    )
    group_codes = [row.group_code for row in memberships if row.group_code]
    bars = list(
        db.scalars(
            select(MarketStyleThsBar).where(
                MarketStyleThsBar.trade_date == batch.trade_date,
                MarketStyleThsBar.group_code.in_(group_codes),
            )
        ).all()
    ) if group_codes else []
    bar_map = {(row.group_type, row.group_code): row for row in bars}
    concept_rows: list[tuple[MarketStyleThsMember, MarketStyleThsBar | None]] = []
    industry_rows: list[tuple[MarketStyleThsMember, MarketStyleThsBar | None]] = []
    for member in memberships:
        pair = (member, bar_map.get((member.group_type, member.group_code)))
        if member.group_type == "concept":
            concept_rows.append(pair)
        elif member.group_type == "industry":
            industry_rows.append(pair)
    concept_rows.sort(key=lambda pair: (pair[1].change_pct if pair[1] and pair[1].change_pct is not None else -999), reverse=True)
    hot_concepts: list[str] = []
    for member, bar in concept_rows:
        tag = normalize_auto_tag_name(member.group_name)
        if not tag or tag in THS_CONTEXT_ONLY or any(word in tag for word in THS_CONTEXT_ONLY):
            continue
        change_pct = bar.change_pct if bar else None
        if change_pct is None or change_pct < 0.8:
            continue
        hot_concepts.append(tag)
        append_evidence(
            evidence_item(
                "market_confirmation",
                "ths",
                "同花顺概念",
                member.group_name,
                f"该股属于同花顺概念；概念指数 {batch.trade_date} 涨跌幅 {change_pct:+.2f}%。",
                batch.trade_date,
                tags=[tag],
            )
        )
        if len(hot_concepts) >= 6:
            break
    if industry_rows:
        member, bar = max(
            industry_rows,
            key=lambda pair: pair[1].change_pct if pair[1] and pair[1].change_pct is not None else -999,
        )
        change_text = f"；行业指数当日涨跌幅 {bar.change_pct:+.2f}%" if bar and bar.change_pct is not None else ""
        append_evidence(
            evidence_item(
                "market_confirmation",
                "ths",
                "同花顺行业",
                member.group_name,
                f"同花顺行业归属：{member.group_name}{change_text}。行业归属仅作背景，不自动写入正式因子。",
                batch.trade_date,
            )
        )

    stock_names = set(db.scalars(select(AStock.name)).all())
    all_concept_names = []
    for name in db.scalars(select(MarketStyleThsMember.group_name).distinct()).all():
        clean_name = normalize_auto_tag_name(str(name or ""))
        if clean_name and clean_name not in stock_names:
            all_concept_names.append(str(name))

    def tags_from_text(text_value: str, extra: list[str] | None = None) -> list[str]:
        return normalize_auto_tag_list([*extract_auto_tags_from_text(text_value, all_concept_names), *(extra or [])])[:8]

    research_rows = list(
        db.scalars(
            select(StockResearchEvidence)
            .where(StockResearchEvidence.full_code == item.full_code)
            .order_by(desc(StockResearchEvidence.evidence_date), desc(StockResearchEvidence.id))
            .limit(20)
        ).all()
    )
    for row in research_rows:
        text_value = f"{row.title} {row.content}"
        append_evidence(
            evidence_item(
                stock_research_category(row.category),
                "stock_research",
                row.source_name or "个股证据台账",
                row.title,
                row.content,
                row.evidence_date,
                row.source_url,
                tags_from_text(text_value),
            )
        )

    announcement_rows = list(
        db.scalars(
            select(WatchlistAnnouncementItem)
            .where(
                WatchlistAnnouncementItem.full_code == item.full_code,
                WatchlistAnnouncementItem.crawled_at >= cutoff_at,
            )
            .order_by(desc(WatchlistAnnouncementItem.published_at), desc(WatchlistAnnouncementItem.id))
            .limit(12)
        ).all()
    )
    for row in announcement_rows:
        text_value = f"{row.title} {row.summary}"
        append_evidence(
            evidence_item("hard_fact", "announcement", row.source_name, row.title, row.summary, row.published_at or row.crawled_at, row.source_url, tags_from_text(text_value))
        )

    news_rows = list(
        db.scalars(
            select(EditedNewsItem)
            .where(
                EditedNewsItem.crawled_at >= cutoff_at,
                or_(
                    EditedNewsItem.title.contains(item.name),
                    EditedNewsItem.summary.contains(item.name),
                    EditedNewsItem.related_stocks.contains(item.code),
                    EditedNewsItem.related_stocks.contains(item.name),
                ),
            )
            .order_by(desc(EditedNewsItem.published_at), desc(EditedNewsItem.id))
            .limit(16)
        ).all()
    )
    official_words = ("公告", "交易所", "证监会", "工信部", "发改委", "公司官网", "互动易")
    for row in news_rows:
        text_value = f"{row.title} {row.summary} {row.impact_path} {row.related_sectors}"
        related_tags = parse_json_list(row.related_sectors)
        category = "hard_fact" if any(word in row.source_name for word in official_words) or row.source_category in {"regulation", "policy"} else "market_guess"
        append_evidence(
            evidence_item(category, "news", row.source_name, row.title, row.summary or row.impact_path, row.published_at or row.crawled_at, row.source_url, tags_from_text(text_value, related_tags))
        )

    material_rows = list(
        db.scalars(
            select(MarketReviewMaterial)
            .where(
                MarketReviewMaterial.report_date >= cutoff_date,
                MarketReviewMaterial.status != "deleted",
                or_(
                    MarketReviewMaterial.title.contains(item.name),
                    MarketReviewMaterial.content.contains(item.name),
                    MarketReviewMaterial.content.contains(item.code),
                ),
            )
            .order_by(desc(MarketReviewMaterial.report_date), desc(MarketReviewMaterial.id))
            .limit(20)
        ).all()
    )
    for row in material_rows:
        source_text = f"{row.source_name} {row.source_type}"
        category = "xueqiu" if "雪球" in source_text else "zsxq" if "星球" in source_text else "market_confirmation" if "行情" in source_text else "market_guess"
        text_value = f"{row.theme} {row.title} {row.content}"
        append_evidence(
            evidence_item(category, "review_material", row.source_name, row.title, row.content, row.report_date, row.url, tags_from_text(text_value))
        )

    xq_posts = list(
        db.scalars(
            select(XueqiuPost)
            .where(
                XueqiuPost.crawled_at >= cutoff_at,
                or_(XueqiuPost.content.contains(item.name), XueqiuPost.content.contains(item.code)),
            )
            .order_by(desc(XueqiuPost.published_at), desc(XueqiuPost.id))
            .limit(12)
        ).all()
    )
    for row in xq_posts:
        append_evidence(
            evidence_item("xueqiu", "xueqiu", "雪球发言", f"{row.author_name}提及{item.name}", row.content, row.published_at or row.crawled_at, row.source_url, tags_from_text(row.content))
        )
    recommendations = list(
        db.scalars(
            select(XueqiuRecommendation)
            .where(XueqiuRecommendation.full_code == item.full_code, XueqiuRecommendation.created_at >= cutoff_at)
            .order_by(desc(XueqiuRecommendation.created_at), desc(XueqiuRecommendation.id))
            .limit(8)
        ).all()
    )
    for row in recommendations:
        excerpt = row.source_excerpt or f"雪球用户将该股加入推荐，当前状态：{row.status}。"
        append_evidence(
            evidence_item("xueqiu", "xueqiu_recommendation", "雪球推荐", f"雪球推荐：{item.name}", excerpt, row.created_at, row.source_url, tags_from_text(excerpt))
        )

    chain_rows = db.execute(
        select(IndustryChainCompany, IndustryChain, IndustryChainSegment)
        .join(IndustryChain, IndustryChain.id == IndustryChainCompany.chain_id)
        .outerjoin(IndustryChainSegment, IndustryChainSegment.id == IndustryChainCompany.segment_id)
        .where(IndustryChainCompany.full_code == item.full_code)
    ).all()
    for company, chain, segment in chain_rows:
        chain_tags = [chain.name, segment.name if segment else ""]
        append_evidence(
            evidence_item(
                "market_guess",
                "industry_chain",
                "产业链库",
                f"{chain.name}{f' / {segment.name}' if segment else ''}",
                company.position or company.core_logic or "已在产业链库建立股票关系，仍需用订单、客户或收入验证。",
                company.updated_at,
                tags=chain_tags,
            )
        )

    zsxq_items: list[dict[str, Any]] = []
    zsxq_source: dict[str, Any] = {"status": "skipped", "message": "本次未读取知识星球。"}
    if include_live_zsxq:
        try:
            from app.main import fetch_zsxq_network_items

            zsxq_items, zsxq_source = fetch_zsxq_network_items(
                [(f"{item.name} {item.full_code}", (item.name, item.code, item.full_code))],
                has_watch_stocks=True,
            )
        except Exception as exc:  # noqa: BLE001
            zsxq_source = {"status": "error", "message": f"知识星球读取失败：{str(exc)[:180]}"}
    for row in zsxq_items[:12]:
        text_value = f"{row.get('title') or ''} {row.get('content') or ''} {row.get('image_text') or ''}"
        append_evidence(
            evidence_item(
                "zsxq",
                "zsxq",
                str(row.get("message_type") or "知识星球"),
                str(row.get("title") or f"星球提及{item.name}"),
                str(row.get("content") or row.get("image_text") or ""),
                row.get("created_at"),
                row.get("link"),
                tags_from_text(text_value),
            )
        )

    category_counts = {category: sum(1 for row in evidence if row.get("category") == category) for category in EVIDENCE_CATEGORIES}
    source_statuses = [
        {"key": "market", "name": "盘面", "status": "ok", "count": category_counts["market_confirmation"], "message": "涨幅、成交额与同花顺板块表现已读取。"},
        {"key": "ths", "name": "同花顺", "status": "ok" if memberships else "not_found", "count": len(memberships), "message": f"行业/概念归属 {len(memberships)} 个，当日走强概念 {len(hot_concepts)} 个。"},
        {"key": "zsxq", "name": "知识星球", "status": str(zsxq_source.get("status") or "not_found"), "count": category_counts["zsxq"], "message": str(zsxq_source.get("message") or "暂无直接命中。")},
        {"key": "xueqiu", "name": "雪球", "status": "ok" if category_counts["xueqiu"] else "not_found", "count": category_counts["xueqiu"], "message": f"近30天直接命中 {category_counts['xueqiu']} 条。"},
        {"key": "hard_fact", "name": "硬事实", "status": "ok" if category_counts["hard_fact"] else "not_found", "count": category_counts["hard_fact"], "message": f"公告/证据台账命中 {category_counts['hard_fact']} 条。"},
        {"key": "materials", "name": "新闻与复盘", "status": "ok" if category_counts["market_guess"] else "not_found", "count": category_counts["market_guess"], "message": f"新闻、复盘与产业链线索 {category_counts['market_guess']} 条。"},
    ]

    support_rows = tag_supports_from_evidence(evidence)
    suggested = [str(row["tag"]) for row in support_rows if float(row["score"]) >= 2.0]
    non_market_count = sum(category_counts[key] for key in ("hard_fact", "zsxq", "xueqiu", "market_guess"))
    caution_tag: str | None = None
    if non_market_count == 0 or not suggested:
        caution_tag = "盘面异动待验证"
    elif category_counts["hard_fact"] == 0 and category_counts["zsxq"] + category_counts["xueqiu"] > 0:
        caution_tag = "小作文叙事"
    suggested = normalize_auto_tag_list(suggested)
    if "AI算力" in suggested and "AI" in suggested:
        suggested.remove("AI")
    suggested = suggested[:8]
    if caution_tag:
        suggested.append(caution_tag)
    if not suggested:
        suggested = ["盘面异动待验证"]

    old_suggested = parse_json_list(item.suggested_tags_json)
    current_draft = parse_json_list(item.draft_tags_json)
    item.market_tags_json = dump_json_list(market_tags)
    item.suggested_tags_json = dump_json_list(suggested)
    item.evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)
    item.source_status_json = json.dumps(source_statuses, ensure_ascii=False, default=str)
    item.research_status = "partial" if any(row["status"] in {"error", "partial_error"} for row in source_statuses) else "ready"
    item.researched_at = now_utc()
    if item.stage == "initial_pending" and (not current_draft or current_draft == old_suggested):
        item.draft_tags_json = dump_json_list(suggested)
    save_item_version(db, item, "evidence_refresh")
    db.commit()
    db.refresh(item)
    return item_to_out(item, effective_tags_map(db, [item.full_code]).get(item.full_code, []))


def item_or_error(db: Session, item_id: int) -> FactorReviewItem:
    item = db.get(FactorReviewItem, item_id)
    if not item:
        raise ValueError("复核股票不存在")
    return item


def save_item_version(db: Session, item: FactorReviewItem, action: str) -> None:
    item.revision += 1
    item.updated_at = now_utc()
    snapshot = item_to_out(item, effective_tags_map(db, [item.full_code]).get(item.full_code, []))
    db.add(
        FactorReviewVersion(
            item_id=item.id,
            revision=item.revision,
            action=action,
            stage=item.stage,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False, default=str),
        )
    )


def save_factor_review_draft(db: Session, item_id: int, tag_names: list[str]) -> dict[str, Any]:
    item = item_or_error(db, item_id)
    if item.stage == "confirmed":
        raise ValueError("请先点击再次修改")
    item.draft_tags_json = dump_json_list(normalize_tag_names(tag_names))
    save_item_version(db, item, "draft_save")
    db.commit()
    db.refresh(item)
    return item_to_out(item, effective_tags_map(db, [item.full_code]).get(item.full_code, []))


def submit_factor_review_initial(db: Session, item_id: int) -> dict[str, Any]:
    item = item_or_error(db, item_id)
    if item.stage != "initial_pending":
        raise ValueError("该股票已完成初标")
    if not item.researched_at:
        raise ValueError("请先刷新同花顺、知识星球、雪球和本地材料证据，再完成初标")
    item.initial_tags_json = item.draft_tags_json
    item.stage = "second_pending"
    item.initial_completed_at = now_utc()
    save_item_version(db, item, "initial_submit")
    db.commit()
    db.refresh(item)
    return item_to_out(item, effective_tags_map(db, [item.full_code]).get(item.full_code, []))


def replace_effective_tags(db: Session, full_code: str, tag_names: list[str]) -> list[str]:
    clean_names = normalize_tag_names(tag_names)
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


def confirm_factor_review_item(db: Session, item_id: int) -> dict[str, Any]:
    item = item_or_error(db, item_id)
    if item.stage != "second_pending":
        raise ValueError("请先完成初标或再次打开修改")
    confirmed = replace_effective_tags(db, item.full_code, parse_json_list(item.draft_tags_json))
    item.confirmed_tags_json = dump_json_list(confirmed)
    item.stage = "confirmed"
    item.confirmed_at = now_utc()
    save_item_version(db, item, "confirm")
    db.commit()
    db.refresh(item)
    return item_to_out(item, confirmed)


def reopen_factor_review_item(db: Session, item_id: int) -> dict[str, Any]:
    item = item_or_error(db, item_id)
    if item.stage != "confirmed":
        raise ValueError("当前股票尚未正式生效")
    item.draft_tags_json = item.confirmed_tags_json
    item.stage = "second_pending"
    save_item_version(db, item, "reopen")
    db.commit()
    db.refresh(item)
    return item_to_out(item, effective_tags_map(db, [item.full_code]).get(item.full_code, []))


def list_item_versions(db: Session, item_id: int) -> list[dict[str, Any]]:
    item_or_error(db, item_id)
    rows = db.scalars(
        select(FactorReviewVersion)
        .where(FactorReviewVersion.item_id == item_id)
        .order_by(desc(FactorReviewVersion.revision))
    ).all()
    return [
        {
            "id": row.id,
            "revision": row.revision,
            "action": row.action,
            "stage": row.stage,
            "snapshot": json.loads(row.snapshot_json),
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/batches")
def list_batches(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(FactorReviewBatch).order_by(desc(FactorReviewBatch.trade_date), desc(FactorReviewBatch.id)).limit(limit)).all()
    return {"items": [batch_to_out(db, row) for row in rows]}


@router.post("/batches")
def create_batch(payload: FactorReviewBatchCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        batch, created = create_factor_review_batch(db, payload)
        return {"created": created, "batch": batch}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    batch = db.get(FactorReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="复核批次不存在")
    return batch_to_out(db, batch, include_items=True)


@router.patch("/items/{item_id}/draft")
def update_draft(item_id: int, payload: FactorReviewDraftUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return save_factor_review_draft(db, item_id, payload.tag_names)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items/{item_id}/research")
def refresh_research(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return refresh_factor_review_research(db, item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items/{item_id}/submit-first")
def submit_first(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return submit_factor_review_initial(db, item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items/{item_id}/confirm")
def confirm_item(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return confirm_factor_review_item(db, item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items/{item_id}/reopen")
def reopen_item(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return reopen_factor_review_item(db, item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/items/{item_id}/versions")
def item_versions(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return {"items": list_item_versions(db, item_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

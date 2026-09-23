from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.factors import create_factor_tags_bulk, factor_stock_tags, update_factor_stock_tags
from app.models import (
    AStock,
    FactorStockReview,
    FactorQuoteSnapshot,
    FactorTag,
    MarketOpportunityGroup,
    MarketOpportunityItem,
    MarketStyleThsMember,
    SectorIndex,
    SectorIndexMember,
    StockAnalysisRecord,
    StockResearchGenerationItem,
    StockResearchGenerationRun,
    WatchlistAnnouncementItem,
    XueqiuPost,
    now_utc,
)
from app.watchlist_announcements import exchange_from_code

INITIAL_SCREENSHOT_SOURCE_REF = "screenshot-b51ada39-20260625"
QQ_TEMPLATE_VERSION = "qq-v1"
RESEARCH_SKILL_META = {
    "qq": {
        "source_type": "qq_generation",
        "label": "qq",
        "default_trigger": "qq_chat",
        "default_version": QQ_TEMPLATE_VERSION,
        "default_summary": "来自 Codex qq skill 的个股判断。",
    },
    "majia": {
        "source_type": "majia_review",
        "label": "马甲",
        "default_trigger": "majia_skill",
        "default_version": "majia-v1",
        "default_summary": "来自马甲式产业投研 Skill 的个股判断。",
    },
    "keke": {
        "source_type": "keke_review",
        "label": "keke",
        "default_trigger": "keke_skill",
        "default_version": "keke-v1",
        "default_summary": "来自停牌的 keke Skill 的个股判断。",
    },
    "qq_majia": {
        "source_type": "qq_majia_review",
        "label": "qq+马甲",
        "default_trigger": "qq_majia_skill",
        "default_version": "qq-majia-v1",
        "default_summary": "来自 qq + 马甲式产业投研 Skill 的个股判断。",
    },
}
IMA_TARGET_KNOWLEDGE_BASE_NAMES = (
    "原文可查看【浑水调研】",
    "「智汇研」券商投行研报精选（持续更新）",
)
IMA_KNOWLEDGE_BASE_CACHE_SECONDS = 600
_ima_target_kb_cache: dict[str, Any] = {"at": 0.0, "items": []}

INITIAL_SCREENSHOT_STOCKS: list[dict[str, Any]] = [
    {"code": "300650", "name": "太龙股份"},
    {"code": "688093", "name": "世华科技"},
    {"code": "300819", "name": "聚杰微纤", "tags": ["电子布", "新材料"]},
    {"code": "688669", "name": "聚石化学"},
    {"code": "688381", "name": "帝奥微", "tags": ["模拟芯片"]},
    {"code": "688380", "name": "中微半导", "tags": ["MCU", "功率半导体"]},
    {"code": "688376", "name": "美埃科技"},
    {"code": "300223", "name": "北京君正", "tags": ["存储", "AIoT"]},
    {"code": "688025", "name": "杰普特", "tags": ["激光设备"]},
    {"code": "688118", "name": "思林杰", "tags": ["测试设备"]},
    {"code": "300806", "name": "斯迪克", "tags": ["功能膜"]},
    {"code": "301418", "name": "协昌科技", "tags": ["电机控制"]},
    {"code": "300227", "name": "光韵达", "tags": ["激光加工"]},
    {"code": "300420", "name": "五洋自控", "tags": ["机器人"]},
    {"code": "688525", "name": "佰维存储", "tags": ["存储", "AI存储"]},
    {"code": "301265", "name": "华新科技"},
    {"code": "688403", "name": "汇成股份", "tags": ["封测", "先进封装"]},
    {"code": "688545", "name": "兴福电子", "tags": ["电子化学品", "半导体材料"]},
    {"code": "300195", "name": "长荣股份"},
    {"code": "301051", "name": "信濠光电", "tags": ["玻璃加工"]},
    {"code": "301392", "name": "汇成真空", "tags": ["真空设备"]},
    {"code": "301013", "name": "利和兴", "tags": ["智能制造"]},
    {"code": "688123", "name": "聚辰股份", "tags": ["存储", "EEPROM"]},
    {"code": "688136", "name": "科兴制药", "tags": ["医药"]},
    {"code": "300136", "name": "信维通信", "tags": ["消费电子", "射频"]},
    {"code": "300909", "name": "汇创达", "tags": ["连接器", "消费电子"]},
    {"code": "300408", "name": "三环集团", "tags": ["陶瓷材料", "MLCC"]},
    {"code": "300077", "name": "国民技术", "tags": ["安全芯片"]},
    {"code": "300120", "name": "经纬辉开", "tags": ["触控显示"]},
    {"code": "688521", "name": "芯原股份", "tags": ["IP", "Chiplet", "AI芯片"]},
    {"code": "300105", "name": "龙源技术", "tags": ["电力设备"]},
    {"code": "300747", "name": "锐科激光", "tags": ["激光器"]},
    {"code": "301678", "name": "新恒汇", "tags": ["智能卡"]},
    {"code": "688260", "name": "昀冢科技", "tags": ["精密结构件"]},
    {"code": "688592", "name": "司南导航", "tags": ["卫星导航"]},
]


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def dump_list(values: list[Any]) -> str:
    return json.dumps(values, ensure_ascii=False)


def normalize_tag_names(values: list[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        name = " ".join(str(raw or "").replace("#", "").strip().split())
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name[:80])
    return result


def factor_tags_by_names(db: Session, names: list[str]) -> list[FactorTag]:
    clean_names = normalize_tag_names(names)
    if not clean_names:
        return []
    create_factor_tags_bulk(db, clean_names)
    return list(db.scalars(select(FactorTag).where(FactorTag.name.in_(clean_names))).all())


def normalize_full_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")) and len(raw) >= 8:
        return raw[:8]
    digits = "".join(ch for ch in raw if ch.isdigit())[:6]
    if len(digits) != 6:
        raise ValueError("股票代码格式不正确")
    return f"{exchange_from_code(digits)}{digits}"


def ensure_stock(db: Session, code_or_full_code: str, name: str | None = None) -> AStock:
    full_code = normalize_full_code(code_or_full_code)
    code = full_code[-6:]
    stock = db.scalar(select(AStock).where(AStock.full_code == full_code))
    if not stock:
        stock = AStock(
            code=code,
            name=name or full_code,
            exchange=full_code[:2],
            full_code=full_code,
            pinyin="",
            source="manual",
        )
        db.add(stock)
    if name and stock.name != name:
        stock.name = name
    stock.exchange = full_code[:2]
    stock.updated_at = now_utc()
    db.flush()
    return stock


def tag_to_out(tag: FactorTag) -> dict[str, Any]:
    return {
        "id": tag.id,
        "name": tag.name,
        "color": tag.color,
        "stock_count": 0,
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
    }


def analysis_record_to_out(row: StockAnalysisRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "exchange": row.exchange,
        "full_code": row.full_code,
        "title": row.title,
        "summary": row.summary,
        "body": row.body,
        "source_type": row.source_type,
        "source_ref": row.source_ref,
        "verification_status": row.verification_status,
        "suggested_tags": [str(item) for item in parse_json_list(row.suggested_tags_json)],
        "source_summary": row.source_summary,
        "generation_item_id": row.generation_item_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def factor_review_analysis_record_to_out(row: FactorStockReview) -> dict[str, Any]:
    tags = [str(item) for item in parse_json_list(row.reviewed_tag_signature)]
    reasons = [str(item) for item in parse_json_list(row.reviewed_reasons_json)] or ["Codex/qq全A自动打标"]
    tag_text = "、".join(tags) if tags else "暂无明确标签"
    primary = "、".join(tags[:3]) if tags else "待继续观察"
    summary = (
        f"{row.name}：Codex/qq 全A自动打标结论，当前主要归入 {'、'.join(tags[:6])}。"
        if tags
        else f"{row.name}：Codex/qq 全A自动打标已覆盖，但暂未形成明确标签。"
    )
    body = (
        f"结论：{row.name}（{row.full_code}）已由 Codex/qq 全A自动打标覆盖，当前核心标签为：{tag_text}。\n\n"
        f"核心逻辑：先按 {primary} 这组市场赚钱因子观察，后续结合公告、雪球、自选变动和市场风格强弱继续验证。\n\n"
        "跟踪要点：如果后续标签变化、进入强势榜/亏钱榜，或出现公告与雪球证据，应回到个股档案更新结论。\n\n"
        f"来源：{'、'.join(reasons)}。这条记录来自全A批量自动打标恢复，不是单股深度长文。"
    )
    created_at = row.reviewed_at or row.updated_at or row.created_at
    return {
        "id": -row.id,
        "code": row.code,
        "name": row.name,
        "exchange": row.exchange,
        "full_code": row.full_code,
        "title": f"{row.name} Codex/qq 全A自动打标结论",
        "summary": summary,
        "body": body,
        "source_type": "qq_auto_tag_review",
        "source_ref": "factor_stock_reviews",
        "verification_status": "自动打标",
        "suggested_tags": tags,
        "source_summary": f"来源：{'、'.join(reasons)}；标签：{tag_text}",
        "generation_item_id": None,
        "created_at": created_at,
        "updated_at": row.updated_at,
    }


def generation_item_to_out(row: StockResearchGenerationItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "code": row.code,
        "name": row.name,
        "exchange": row.exchange,
        "full_code": row.full_code,
        "status": row.status,
        "action_status": row.action_status,
        "summary": row.summary,
        "thesis": row.thesis,
        "body": row.body,
        "market_tags": [str(item) for item in parse_json_list(row.market_tags_json)],
        "suggested_tags": [str(item) for item in parse_json_list(row.suggested_tags_json)],
        "selected_tags": [str(item) for item in parse_json_list(row.selected_tags_json)],
        "suggested_market_style": row.suggested_market_style,
        "verification_status": row.verification_status,
        "source_summary": row.source_summary,
        "analysis_record_id": row.analysis_record_id,
        "tags_applied": row.tags_applied,
        "processed": row.processed,
        "ignored": row.ignored,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def generation_run_to_out(row: StockResearchGenerationRun, include_items: bool = False) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "title": row.title,
        "trigger_type": row.trigger_type,
        "template_name": row.template_name,
        "template_version": row.template_version,
        "source_ref": row.source_ref,
        "status": row.status,
        "message": row.message,
        "item_count": row.item_count,
        "success_count": row.success_count,
        "failed_count": row.failed_count,
        "applied_count": row.applied_count,
        "processed_count": row.processed_count,
        "ignored_count": row.ignored_count,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "updated_at": row.updated_at,
    }
    if include_items:
        items = sorted(row.items, key=lambda item: (item.status == "error", item.id))
        payload["items"] = [generation_item_to_out(item) for item in items]
    return payload


def refresh_run_counts(db: Session, run: StockResearchGenerationRun) -> None:
    items = list(db.scalars(select(StockResearchGenerationItem).where(StockResearchGenerationItem.run_id == run.id)).all())
    run.item_count = len(items)
    run.success_count = len([item for item in items if item.status != "error"])
    run.failed_count = len([item for item in items if item.status == "error"])
    run.applied_count = len([item for item in items if item.tags_applied])
    run.processed_count = len([item for item in items if item.processed])
    run.ignored_count = len([item for item in items if item.ignored])
    run.updated_at = now_utc()
    if run.completed_at is None:
        run.completed_at = now_utc()


def list_research_runs(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(select(StockResearchGenerationRun).order_by(desc(StockResearchGenerationRun.created_at))).all()
    return [generation_run_to_out(row) for row in rows]


def get_research_run(db: Session, run_id: int) -> dict[str, Any]:
    run = db.get(StockResearchGenerationRun, run_id)
    if not run:
        raise ValueError("生成任务不存在")
    return generation_run_to_out(run, include_items=True)


def applied_tag_names_from_other_items(db: Session, item: StockResearchGenerationItem) -> set[str]:
    rows = db.scalars(
        select(StockResearchGenerationItem).where(
            StockResearchGenerationItem.full_code == item.full_code,
            StockResearchGenerationItem.id != item.id,
            StockResearchGenerationItem.tags_applied.is_(True),
        )
    ).all()
    names: set[str] = set()
    for row in rows:
        names.update(normalize_tag_names([str(value) for value in parse_json_list(row.selected_tags_json)]))
    return names


def sync_applied_item_tags(db: Session, item: StockResearchGenerationItem, new_names: list[str]) -> None:
    if not item.tags_applied:
        return
    old_names = normalize_tag_names([str(value) for value in parse_json_list(item.selected_tags_json)])
    clean_new_names = normalize_tag_names(new_names)
    protected_names = applied_tag_names_from_other_items(db, item)
    removable_names = set(old_names) - set(clean_new_names) - protected_names
    new_tag_rows = factor_tags_by_names(db, clean_new_names)
    existing = factor_stock_tags(db, item.full_code)["tags"]
    final_ids = {tag["id"] for tag in existing if tag["name"] not in removable_names}
    final_ids.update(tag.id for tag in new_tag_rows)
    update_factor_stock_tags(db, item.full_code, sorted(final_ids))


def update_generation_item(db: Session, item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    item = db.get(StockResearchGenerationItem, item_id)
    if not item:
        raise ValueError("生成项不存在")
    content_changed = False
    if "selected_tags" in payload and payload.get("selected_tags") is not None:
        selected_tags = normalize_tag_names(payload.get("selected_tags"))
        sync_applied_item_tags(db, item, selected_tags)
        item.selected_tags_json = dump_list(selected_tags)
        content_changed = True
    if "summary" in payload and payload.get("summary") is not None:
        item.summary = text_value(payload.get("summary"), "", None)
        content_changed = True
    if "thesis" in payload and payload.get("thesis") is not None:
        item.thesis = text_value(payload.get("thesis"), "", None)
        content_changed = True
    if "body" in payload and payload.get("body") is not None:
        item.body = text_value(payload.get("body"), "", None)
        content_changed = True
    if "verification_status" in payload and payload.get("verification_status") is not None:
        item.verification_status = text_value(payload.get("verification_status"), "待验证", 32) or "待验证"
        content_changed = True
    if "source_summary" in payload:
        item.source_summary = str(payload.get("source_summary")).strip() if payload.get("source_summary") is not None else None
        content_changed = True
    if payload.get("processed") is not None:
        item.processed = bool(payload["processed"])
        if item.processed and not item.ignored:
            item.action_status = "processed"
    if payload.get("ignored") is not None:
        item.ignored = bool(payload["ignored"])
        if item.ignored:
            item.processed = True
            item.action_status = "ignored"
    if payload.get("action_status"):
        item.action_status = str(payload["action_status"])[:40]
    item.updated_at = now_utc()
    if content_changed and item.status != "error":
        create_or_update_analysis_record_for_item(db, item, item.run.source_ref)
    run = item.run
    refresh_run_counts(db, run)
    db.commit()
    db.refresh(item)
    return generation_item_to_out(item)


def apply_item_tags(db: Session, item_id: int, tag_names: list[str] | None = None) -> dict[str, Any]:
    item = db.get(StockResearchGenerationItem, item_id)
    if not item:
        raise ValueError("生成项不存在")
    if item.status == "error":
        raise ValueError("生成失败的条目不能应用标签")
    names = normalize_tag_names(tag_names or parse_json_list(item.selected_tags_json) or parse_json_list(item.suggested_tags_json))
    if not names:
        raise ValueError("没有可应用的建议标签")
    ensure_stock(db, item.full_code, item.name)
    tag_rows = factor_tags_by_names(db, names)
    existing = factor_stock_tags(db, item.full_code)["tags"]
    merged_ids = sorted({tag["id"] for tag in existing} | {tag.id for tag in tag_rows})
    update_factor_stock_tags(db, item.full_code, merged_ids)
    item.selected_tags_json = dump_list(names)
    item.tags_applied = True
    item.processed = True
    item.ignored = False
    item.action_status = "applied_tags"
    item.updated_at = now_utc()
    refresh_run_counts(db, item.run)
    db.commit()
    db.refresh(item)
    return generation_item_to_out(item)


def apply_run_tags(db: Session, run_id: int) -> dict[str, Any]:
    run = db.get(StockResearchGenerationRun, run_id)
    if not run:
        raise ValueError("生成任务不存在")
    applied = 0
    skipped = 0
    for item in sorted(run.items, key=lambda row: row.id):
        if item.status == "error" or item.tags_applied or item.ignored:
            skipped += 1
            continue
        try:
            apply_item_tags(db, item.id)
            applied += 1
        except ValueError:
            skipped += 1
    db.refresh(run)
    return {"status": "ok", "message": f"已应用 {applied} 只股票的建议标签，跳过 {skipped} 只。", "run": generation_run_to_out(run, include_items=True)}


def delete_research_run(db: Session, run_id: int, delete_records: bool = False) -> dict[str, Any]:
    run = db.get(StockResearchGenerationRun, run_id)
    if not run:
        raise ValueError("生成任务不存在")
    deleted_records = 0
    if delete_records:
        item_ids = [item.id for item in run.items]
        record_ids = [item.analysis_record_id for item in run.items if item.analysis_record_id]
        filters = []
        if item_ids:
            filters.append(StockAnalysisRecord.generation_item_id.in_(item_ids))
        if record_ids:
            filters.append(StockAnalysisRecord.id.in_(record_ids))
        records = db.scalars(select(StockAnalysisRecord).where(or_(*filters))).all() if filters else []
        deleted_records = len(records)
        for record in records:
            db.delete(record)
    title = run.title
    db.delete(run)
    db.commit()
    return {
        "status": "ok",
        "message": f"已删除生成任务：{title}",
        "deleted_run_id": run_id,
        "deleted_records": deleted_records,
    }


def mark_run_processed(db: Session, run_id: int) -> dict[str, Any]:
    run = db.get(StockResearchGenerationRun, run_id)
    if not run:
        raise ValueError("生成任务不存在")
    for item in run.items:
        if not item.ignored:
            item.processed = True
            if item.action_status == "unprocessed":
                item.action_status = "processed"
            item.updated_at = now_utc()
    refresh_run_counts(db, run)
    db.commit()
    db.refresh(run)
    return {"status": "ok", "message": "已标记本批次为已处理。", "run": generation_run_to_out(run, include_items=True)}


def create_analysis_record(db: Session, full_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    stock = ensure_stock(db, full_code)
    record = StockAnalysisRecord(
        code=stock.code,
        name=stock.name,
        exchange=stock.exchange,
        full_code=stock.full_code,
        title=str(payload.get("title") or f"{stock.name} 投研记录")[:240],
        summary=str(payload.get("summary") or ""),
        body=str(payload.get("body") or ""),
        source_type=str(payload.get("source_type") or "manual_note")[:40],
        source_ref=payload.get("source_ref"),
        verification_status=str(payload.get("verification_status") or "待验证")[:32],
        suggested_tags_json=dump_list(normalize_tag_names(payload.get("suggested_tags"))),
        source_summary=payload.get("source_summary"),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return analysis_record_to_out(record)


def text_value(value: Any, fallback: Any = "", limit: int | None = None) -> str:
    base = value if value is not None else fallback
    if base is None:
        return ""
    text = str(base).strip()
    if limit is not None:
        text = text[:limit]
    return text


def compact_body_summary(body: str, fallback: str) -> str:
    for line in body.splitlines():
        clean = line.strip(" #*-　")
        if clean:
            return clean[:240]
    return fallback[:240]


def research_skill_meta(template_name: str | None) -> dict[str, str]:
    key = (template_name or "qq").strip().lower()
    return RESEARCH_SKILL_META.get(key, RESEARCH_SKILL_META["qq"])


def create_or_update_analysis_record_for_item(
    db: Session,
    item: StockResearchGenerationItem,
    source_ref: str,
) -> None:
    meta = research_skill_meta(item.run.template_name if item.run else "qq")
    record_tags_json = item.selected_tags_json if parse_json_list(item.selected_tags_json) else item.suggested_tags_json
    record = db.get(StockAnalysisRecord, item.analysis_record_id) if item.analysis_record_id else None
    if not record:
        record = StockAnalysisRecord(
            code=item.code,
            name=item.name,
            exchange=item.exchange,
            full_code=item.full_code,
            title=f"{item.name} {meta['label']} 生成",
            summary=item.summary,
            body=item.body,
            source_type=meta["source_type"],
            source_ref=source_ref,
            verification_status=item.verification_status,
            suggested_tags_json=record_tags_json,
            source_summary=item.source_summary,
            generation_item_id=item.id,
        )
        db.add(record)
        db.flush()
        item.analysis_record_id = record.id
        return
    record.code = item.code
    record.name = item.name
    record.exchange = item.exchange
    record.full_code = item.full_code
    record.title = f"{item.name} {meta['label']} 生成"
    record.summary = item.summary
    record.body = item.body
    record.source_type = meta["source_type"]
    record.source_ref = source_ref
    record.verification_status = item.verification_status
    record.suggested_tags_json = record_tags_json
    record.source_summary = item.source_summary
    record.generation_item_id = item.id
    record.updated_at = now_utc()


def create_qq_generation_items(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw_items = payload.get("items") or []
    if not raw_items:
        raise ValueError("没有可写入的 qq 生成结果")
    created_at = now_utc()
    template_name = text_value(payload.get("template_name"), "qq", 80).strip().lower() or "qq"
    if template_name not in RESEARCH_SKILL_META:
        raise ValueError(f"不支持的投研 Skill：{template_name}")
    meta = research_skill_meta(template_name)
    source_ref = text_value(payload.get("source_ref"), f"{template_name}-{created_at.strftime('%Y%m%d%H%M%S%f')}", 160)
    run = db.scalar(select(StockResearchGenerationRun).where(StockResearchGenerationRun.source_ref == source_ref))
    if not run:
        first = raw_items[0] if isinstance(raw_items[0], dict) else {}
        fallback_title = f"{meta['label']}生成：{first.get('name') or first.get('code')}" if len(raw_items) == 1 else f"{meta['label']}批量生成：{len(raw_items)}只股票"
        run = StockResearchGenerationRun(
            title=text_value(payload.get("title"), fallback_title, 240),
            trigger_type=text_value(payload.get("trigger_type"), meta["default_trigger"], 40),
            template_name=template_name,
            template_version=text_value(payload.get("template_version"), meta["default_version"], 40),
            source_ref=source_ref,
            status="ok",
            message=text_value(payload.get("message"), f"{meta['label']} Skill 已写入 {len(raw_items)} 条生成结果。"),
            input_json=dump_list(raw_items),
            completed_at=created_at,
        )
        db.add(run)
        db.flush()
    else:
        run.title = text_value(payload.get("title"), run.title, 240)
        run.trigger_type = text_value(payload.get("trigger_type"), run.trigger_type, 40)
        run.template_name = template_name
        run.template_version = text_value(payload.get("template_version"), run.template_version, 40)
        run.message = text_value(payload.get("message"), run.message or f"{meta['label']} Skill 已写入 {len(raw_items)} 条生成结果。")
        run.input_json = dump_list(raw_items)
        run.completed_at = created_at

    failed = 0
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            failed += 1
            continue
        raw_code = text_value(raw.get("code"), f"ITEM{index + 1}", 16)
        error_message = text_value(raw.get("error_message"), "")
        item_status = text_value(raw.get("status"), "generated", 40) or "generated"
        try:
            stock = ensure_stock(db, raw_code, text_value(raw.get("name"), None, 80) or None)
            code = stock.code
            name = stock.name
            exchange = stock.exchange
            full_code = stock.full_code
        except ValueError as exc:
            code = raw_code or f"ITEM{index + 1}"
            name = text_value(raw.get("name"), code, 80)
            exchange = ""
            full_code = code
            item_status = "error"
            error_message = error_message or str(exc)

        tags = normalize_tag_names(raw.get("suggested_tags"))
        selected_tags = normalize_tag_names(raw.get("selected_tags")) or tags
        body = text_value(raw.get("body"), "")
        summary = text_value(raw.get("summary"), "")
        if not summary:
            summary = compact_body_summary(body, f"{name} {meta['label']} 生成结果")
        if not body:
            body = summary or error_message or f"{name} {meta['label']} 生成结果为空。"
        thesis = text_value(raw.get("thesis"), "")
        source_summary = raw.get("source_summary") or meta["default_summary"]
        if item_status == "error":
            failed += 1

        item = db.scalar(
            select(StockResearchGenerationItem).where(
                StockResearchGenerationItem.run_id == run.id,
                StockResearchGenerationItem.full_code == full_code,
            )
        )
        is_new_item = item is None
        if not item:
            item = StockResearchGenerationItem(run_id=run.id, code=code, name=name, exchange=exchange, full_code=full_code, body=body)
            db.add(item)
        item.code = code
        item.name = name
        item.exchange = exchange
        item.full_code = full_code
        item.status = item_status
        if is_new_item or item.action_status in {"unprocessed", "error"}:
            item.action_status = "unprocessed" if item_status != "error" else "error"
        item.summary = summary
        item.thesis = thesis
        item.body = body
        item.market_tags_json = dump_list(normalize_tag_names(raw.get("market_tags")))
        item.suggested_tags_json = dump_list(tags)
        item.selected_tags_json = dump_list(selected_tags)
        item.suggested_market_style = text_value(raw.get("suggested_market_style"), None, 120) or None
        item.verification_status = text_value(raw.get("verification_status"), "待验证", 32) or "待验证"
        item.source_summary = str(source_summary)
        if is_new_item:
            item.tags_applied = False
            item.processed = False
            item.ignored = False
        item.error_message = error_message or None
        item.updated_at = now_utc()
        db.flush()
        if item.status != "error":
            create_or_update_analysis_record_for_item(db, item, run.source_ref)

    success = len(raw_items) - failed
    run.status = "error" if success == 0 else "partial_error" if failed else "ok"
    if failed:
        run.message = f"{meta['label']} Skill 已写入 {success} 条生成结果，{failed} 条失败。"
    refresh_run_counts(db, run)
    db.commit()
    db.refresh(run)
    return {"status": run.status, "message": run.message or f"{meta['label']} 生成结果已写入。", "run": generation_run_to_out(run, include_items=True)}


def stock_detail(db: Session, full_code: str) -> dict[str, Any]:
    stock = ensure_stock(db, full_code)
    snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == stock.full_code))
    factor_meta = factor_stock_tags(db, stock.full_code)
    latest_item = db.scalar(
        select(StockResearchGenerationItem)
        .where(StockResearchGenerationItem.full_code == stock.full_code)
        .order_by(desc(StockResearchGenerationItem.created_at))
        .limit(1)
    )
    records = db.scalars(
        select(StockAnalysisRecord)
        .where(StockAnalysisRecord.full_code == stock.full_code)
        .order_by(desc(StockAnalysisRecord.created_at))
        .limit(30)
    ).all()
    factor_review = db.scalar(select(FactorStockReview).where(FactorStockReview.full_code == stock.full_code))
    analysis_records = [analysis_record_to_out(row) for row in records]
    if factor_review and not any(row["source_type"] == "qq_auto_tag_review" for row in analysis_records):
        analysis_records.append(factor_review_analysis_record_to_out(factor_review))
    return {
        "code": stock.code,
        "name": stock.name,
        "exchange": stock.exchange,
        "full_code": stock.full_code,
        "latest_price": snapshot.latest_price if snapshot else None,
        "change_pct": snapshot.change_pct if snapshot else None,
        "factor_tags": factor_meta["tags"],
        "factor_tag_status": factor_meta["tag_status"],
        "latest_generation_item": generation_item_to_out(latest_item) if latest_item else None,
        "analysis_records": analysis_records,
        "relations": [],
        "information_flow": stock_information_flow(db, stock),
        "ima_status": "skipped",
        "ima_message": None,
    }


def stock_information_flow_endpoint(db: Session, full_code: str) -> dict[str, Any]:
    stock = ensure_stock(db, full_code)
    return {
        "status": "ok",
        "items": stock_information_flow(db, stock),
        "ima_status": "skipped",
        "ima_message": None,
    }


def stock_information_flow_with_ima(db: Session, stock: AStock) -> tuple[list[dict[str, Any]], str, str]:
    items = stock_information_flow(db, stock)
    ima_items, ima_status, ima_message = ima_search_stock_items(stock)
    combined = [*items, *ima_items]
    combined.sort(key=lambda item: sort_time(item.get("created_at")), reverse=True)
    return combined[:40], ima_status, ima_message


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ima_credentials() -> tuple[str, str] | None:
    client_id = os.environ.get("IMA_CLIENT_ID") or os.environ.get("IMA_OPENAPI_CLIENTID")
    api_key = os.environ.get("IMA_API_KEY") or os.environ.get("IMA_OPENAPI_APIKEY")
    if not client_id:
        client_id = read_text_file(Path.home() / ".config/ima/client_id")
    if not api_key:
        api_key = read_text_file(Path.home() / ".config/ima/api_key")
    if client_id and api_key:
        return client_id, api_key
    return None


def ima_post(api_path: str, body: dict[str, Any], credentials: tuple[str, str]) -> dict[str, Any]:
    client_id, api_key = credentials
    base_url = os.environ.get("IMA_BASE_URL", "https://ima.qq.com").rstrip("/")
    with httpx.Client(timeout=2.5) as client:
        response = client.post(
            f"{base_url}/{api_path}",
            headers={
                "ima-openapi-clientid": client_id,
                "ima-openapi-apikey": api_key,
                "ima-openapi-ctx": "skill_version=stock-review-mac",
                "Content-Type": "application/json",
            },
            json=body,
        )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code", -1)) != 0:
        raise ValueError(str(payload.get("msg") or "IMA 请求失败"))
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def ima_row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("kb_id") or "")


def ima_row_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("kb_name") or "")


def normalize_ima_name(value: str) -> str:
    return " ".join(value.strip().split())


def ima_target_knowledge_bases(credentials: tuple[str, str]) -> tuple[list[dict[str, str]], list[str]]:
    now = time.time()
    cached = _ima_target_kb_cache.get("items")
    if cached and now - float(_ima_target_kb_cache.get("at") or 0) < IMA_KNOWLEDGE_BASE_CACHE_SECONDS:
        return list(cached), []

    cursor = ""
    rows: list[dict[str, Any]] = []
    for _ in range(5):
        data = ima_post(
            "openapi/wiki/v1/search_knowledge_base",
            {"query": "", "cursor": cursor, "limit": 20},
            credentials,
        )
        rows.extend([row for row in data.get("info_list", []) if isinstance(row, dict)])
        if data.get("is_end", True):
            break
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break

    matched: list[dict[str, str]] = []
    missing: list[str] = []
    normalized_rows = [(normalize_ima_name(ima_row_name(row)), row) for row in rows]
    for target in IMA_TARGET_KNOWLEDGE_BASE_NAMES:
        normalized_target = normalize_ima_name(target)
        match = next((row for name, row in normalized_rows if name == normalized_target), None)
        if match is None:
            match = next((row for name, row in normalized_rows if normalized_target in name or name in normalized_target), None)
        if match is None or not ima_row_id(match):
            missing.append(target)
            continue
        matched.append({"id": ima_row_id(match), "name": ima_row_name(match) or target})

    _ima_target_kb_cache["at"] = now
    _ima_target_kb_cache["items"] = matched
    return matched, missing


def ima_search_stock_items(stock: AStock) -> tuple[list[dict[str, Any]], str, str]:
    credentials = ima_credentials()
    if not credentials:
        return [], "not_configured", "IMA 未配置，当前展示本地已入库信息流。"
    try:
        knowledge_bases, missing = ima_target_knowledge_bases(credentials)
        if not knowledge_bases:
            return [], "partial_error", "IMA 已配置，但没有找到指定的两个知识库：浑水调研、智汇研。"
        results: list[dict[str, Any]] = []
        query = f"{stock.name} {stock.code}"
        try:
            per_kb_limit = int(os.environ.get("IMA_STOCK_SEARCH_RESULTS_PER_KB", "3"))
        except ValueError:
            per_kb_limit = 3
        per_kb_limit = max(1, min(per_kb_limit, 5))
        for kb in knowledge_bases:
            search_data = ima_post(
                "openapi/wiki/v1/search_knowledge",
                {"query": query, "knowledge_base_id": kb["id"], "cursor": ""},
                credentials,
            )
            for row in search_data.get("info_list", [])[:per_kb_limit]:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or "IMA 搜索结果")
                highlight = str(row.get("highlight_content") or "")
                media_id = str(row.get("media_id") or title)
                results.append(
                    {
                        "id": f"ima-{kb['id']}-{media_id}",
                        "source_type": "ima",
                        "source_name": kb["name"],
                        "title": title,
                        "content": highlight or f"知识库中匹配 {stock.name}/{stock.code} 的资料。",
                        "link": None,
                        "created_at": now_utc(),
                    }
                )
        missing_text = f"；未找到：{'、'.join(missing)}" if missing else ""
        source_names = "、".join(kb["name"] for kb in knowledge_bases)
        return results[:8], "ok", f"IMA 已检索固定来源：{source_names}，返回 {len(results[:8])} 条结果{missing_text}。"
    except Exception as exc:  # noqa: BLE001
        return [], "partial_error", f"IMA 检索失败：{str(exc)[:160]}"


def stock_relations(db: Session, stock: AStock) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sectors = db.execute(
        select(SectorIndexMember, SectorIndex)
        .join(SectorIndex, SectorIndex.id == SectorIndexMember.sector_id)
        .where(SectorIndexMember.full_code == stock.full_code)
        .order_by(SectorIndex.sort_order, SectorIndex.name)
    ).all()
    for member, sector in sectors:
        rows.append({"id": sector.id, "name": sector.name, "type": "板块工作台", "detail": member.source_note, "link": f"/market-style/sectors?sector={sector.id}"})
    ths_rows = db.scalars(
        select(MarketStyleThsMember)
        .where(MarketStyleThsMember.full_code == stock.full_code)
        .order_by(MarketStyleThsMember.group_type, MarketStyleThsMember.group_name)
        .limit(10)
    ).all()
    for row in ths_rows:
        rows.append({"id": row.id, "name": row.group_name, "type": "市场风格", "detail": row.group_type, "link": "/market-style"})
    opp_rows = db.execute(
        select(MarketOpportunityItem, MarketOpportunityGroup)
        .join(MarketOpportunityGroup, MarketOpportunityGroup.id == MarketOpportunityItem.group_id)
        .where(or_(MarketOpportunityItem.stock_code == stock.code, MarketOpportunityItem.company_name == stock.name))
        .limit(10)
    ).all()
    for item, group in opp_rows:
        rows.append({"id": item.id, "name": group.name, "type": "机会地图", "detail": item.feature_title, "link": "/industry-chain/opportunity-map"})
    return rows


def stock_information_flow(db: Session, stock: AStock) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    factor_review = db.scalar(select(FactorStockReview).where(FactorStockReview.full_code == stock.full_code))
    if factor_review:
        review_record = factor_review_analysis_record_to_out(factor_review)
        items.append(
            {
                "id": f"factor-review-{factor_review.id}",
                "source_type": "qq_auto_tag_review",
                "source_name": "Codex/qq全A自动打标",
                "title": review_record["summary"],
                "content": review_record["body"],
                "link": "/market-style/tagging",
                "created_at": factor_review.reviewed_at or factor_review.updated_at,
            }
        )
    generation_items = db.scalars(
        select(StockResearchGenerationItem)
        .where(StockResearchGenerationItem.full_code == stock.full_code)
        .order_by(desc(StockResearchGenerationItem.created_at))
        .limit(8)
    ).all()
    for row in generation_items:
        meta = research_skill_meta(row.run.template_name if row.run else "qq")
        items.append(
            {
                "id": f"research-item-{row.id}",
                "source_type": meta["source_type"],
                "source_name": f"{meta['label']} Skill",
                "title": row.summary or f"{row.name} 生成结论",
                "content": row.body,
                "link": f"/research-runs/{row.run_id}",
                "created_at": row.created_at,
            }
        )
    ann_items = db.scalars(
        select(WatchlistAnnouncementItem)
        .where(WatchlistAnnouncementItem.full_code == stock.full_code)
        .order_by(desc(WatchlistAnnouncementItem.published_at), desc(WatchlistAnnouncementItem.crawled_at))
        .limit(8)
    ).all()
    for row in ann_items:
        items.append(
            {
                "id": f"announcement-{row.id}",
                "source_type": row.source_type,
                "source_name": row.source_name,
                "title": row.title,
                "content": row.summary,
                "link": row.source_url,
                "created_at": row.published_at or row.crawled_at,
            }
        )
    opp_rows = db.execute(
        select(MarketOpportunityItem, MarketOpportunityGroup)
        .join(MarketOpportunityGroup, MarketOpportunityGroup.id == MarketOpportunityItem.group_id)
        .where(or_(MarketOpportunityItem.stock_code == stock.code, MarketOpportunityItem.company_name == stock.name))
        .limit(8)
    ).all()
    for row, group in opp_rows:
        items.append(
            {
                "id": f"opportunity-{row.id}",
                "source_type": "opportunity_map",
                "source_name": group.name,
                "title": row.feature_title or row.company_name,
                "content": row.feature_desc or row.source_note,
                "link": "/industry-chain/opportunity-map",
                "created_at": row.updated_at,
            }
        )
    items.sort(key=lambda item: sort_time(item.get("created_at")), reverse=True)
    return items[:30]


def sort_time(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def ensure_initial_research_seed(db: Session) -> StockResearchGenerationRun:
    existing = db.scalar(select(StockResearchGenerationRun).where(StockResearchGenerationRun.source_ref == INITIAL_SCREENSHOT_SOURCE_REF))
    if existing:
        return existing
    run = StockResearchGenerationRun(
        title="截图批量 qq 初评",
        trigger_type="screenshot_batch",
        template_name="qq",
        template_version=QQ_TEMPLATE_VERSION,
        source_ref=INITIAL_SCREENSHOT_SOURCE_REF,
        status="ok",
        message="已按 qq 模板生成首批个股小作文初评，验证状态默认待验证。",
        input_json=dump_list(INITIAL_SCREENSHOT_STOCKS),
        completed_at=now_utc(),
    )
    db.add(run)
    db.flush()
    for source in INITIAL_SCREENSHOT_STOCKS:
        stock = ensure_stock(db, source["code"], source["name"])
        tags = normalize_tag_names(source.get("tags") or infer_tags(source["name"]))
        summary = f"{stock.name}：先按{market_style_from_tags(tags)}方向纳入观察，当前结论属于待验证初评。"
        thesis = qq_thesis(stock.name, tags)
        body = qq_body(stock.name, stock.full_code, tags)
        item = StockResearchGenerationItem(
            run_id=run.id,
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            full_code=stock.full_code,
            status="generated",
            action_status="unprocessed",
            summary=summary,
            thesis=thesis,
            body=body,
            suggested_tags_json=dump_list(tags),
            selected_tags_json=dump_list(tags),
            suggested_market_style=market_style_from_tags(tags),
            verification_status="待验证",
            source_summary="来自截图名单的首批 qq 模板初评；未逐条做公开信息核验。",
        )
        db.add(item)
        db.flush()
        record = StockAnalysisRecord(
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            full_code=stock.full_code,
            title=f"{stock.name} qq 初评",
            summary=summary,
            body=body,
            source_type="qq_initial_review",
            source_ref=INITIAL_SCREENSHOT_SOURCE_REF,
            verification_status="待验证",
            suggested_tags_json=dump_list(tags),
            source_summary=item.source_summary,
            generation_item_id=item.id,
        )
        db.add(record)
        db.flush()
        item.analysis_record_id = record.id
    refresh_run_counts(db, run)
    db.commit()
    db.refresh(run)
    return run


def infer_tags(name: str) -> list[str]:
    rules = [
        ("微", "半导体"),
        ("芯", "半导体"),
        ("存储", "存储"),
        ("光", "光电"),
        ("激光", "激光"),
        ("材料", "新材料"),
        ("科技", "科技成长"),
        ("制药", "医药"),
        ("导航", "卫星导航"),
    ]
    tags = [tag for key, tag in rules if key in name]
    return normalize_tag_names(tags or ["待分类"])


def market_style_from_tags(tags: list[str]) -> str:
    joined = " ".join(tags)
    if any(key in joined for key in ("半导体", "存储", "封测", "电子化学品", "IP", "Chiplet")):
        return "半导体链"
    if any(key in joined for key in ("电子布", "材料", "功能膜", "陶瓷")):
        return "新材料"
    if any(key in joined for key in ("光", "激光")):
        return "光电/激光"
    if "医药" in joined:
        return "医药"
    return "待分类"


def qq_thesis(name: str, tags: list[str]) -> str:
    return f"{name}当前更适合放在“{market_style_from_tags(tags)}”里做预期差观察，先看叙事能否被公开订单、产能或价格变化验证。"


def qq_body(name: str, full_code: str, tags: list[str]) -> str:
    tag_text = "、".join(tags) if tags else "待分类"
    style = market_style_from_tags(tags)
    return "\n".join(
        [
            f"## {name}（{full_code}）qq 初评",
            "",
            f"**一句话结论**：先把{name}放入“{style}”观察池，建议标签为：{tag_text}。这条记录是首批截图初评，性质是正式沉淀但验证状态为待验证。",
            "",
            "**1. 产业链位置**",
            f"- 当前先按标签“{tag_text}”定位，后续要补充同细分产业链玩家数量、公司所处环节、客户与产能位置。",
            "",
            "**2. 最近变化**",
            "- 需要跟踪最近是否出现订单、价格、扩产、客户导入、政策或产业新闻变化。没有变化就只作为低优先级备选。",
            "",
            "**3. 互联网/小作文叙事**",
            "- 目前只来自截图名单与人工初筛，不能直接当硬事实。后续若星球、雪球、公告、IMA 有新增线索，应追加到信息流并修正本记录。",
            "",
            "**4. 可验证证据**",
            "- 待验证：公开公告、互动易/董秘回复、财报业务拆分、客户认证、价格或产能数据。",
            "- 不能验证前，不把它写成强确定性标签。",
            "",
            "**5. 对标与更优选择**",
            "- 若方向成立但同产业链里有更正宗、更有产能、更有业绩兑现的公司，应在后续记录里直接替换排序，不机械维护原名单。",
            "",
            "**6. 操作结论**",
            "- 先应用低争议标签，进入生成台待处理；处理后再进个股页补备注、补证据、改验证状态。",
        ]
    )

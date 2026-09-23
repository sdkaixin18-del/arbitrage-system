from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, delete, desc, func, or_, select, text
from sqlalchemy.orm import Session

from app.models import (
    AStock,
    EditedNewsItem,
    FactorAutoTagRun,
    FactorExcludedStock,
    FactorEffectDailyLog,
    FactorImaTagCache,
    FactorMarketCapSnapshot,
    FactorPeriodPreset,
    FactorQuoteSnapshot,
    FactorRefreshRun,
    FactorStockReview,
    FactorStockTag,
    FactorTagCandidateRun,
    FactorTag,
    MarketReviewMaterial,
    MarketStyleThsMember,
    SectorIndex,
    SectorIndexMember,
    StockDailyBar,
    WatchlistAnnouncementItem,
    WatchlistAnnouncementStock,
    XueqiuPost,
    now_utc,
)
from app.watchlist_announcements import ensure_stock_universe, exchange_from_code

BEIJING = ZoneInfo("Asia/Shanghai")
UNTAGGED_NAME = "未打标"
DEFAULT_TOP_N = 50
MARKET_EFFECT_PERIODS = [1, 2, 3, 5, 10, 20]
MIN_EFFECT_TAG_SAMPLE_COUNT = 4
MIN_AUTO_TAG_SAMPLE_COUNT = 4
FACTOR_REVIEW_STATUSES = {"unreviewed", "needs_more", "done"}
TAG_CANDIDATE_DEFAULT_LIMIT = 120
TAG_CANDIDATE_CONCEPT_INDEX_LIMIT = 24
FACTOR_BACKTEST_CONTEXT_TAGS = {
    "A股基础池",
    "北交所",
    "创业板",
    "大盘权重",
    "行业龙头候选",
    "容量核心",
    "中大盘",
    "中小盘",
    "中盘",
    "小市值",
    "小盘弹性",
    "市值龙头候选",
    "强势股",
    "技术形态",
    "活跃成交",
    "沪市主板",
    "硬科技",
    "深市主板",
    "科创板",
    "科技成长",
    "趋势强势",
    "盘面异动待验证",
    "亏钱效应",
    "高成交额",
    "高弹性",
}
FACTOR_BOARD_STYLE_TAGS = {
    "A股基础池",
    "北交所",
    "创业板",
    "科创板",
    "沪市主板",
    "深市主板",
    "大盘权重",
    "中大盘",
    "中小盘",
    "中盘",
    "小市值",
    "小盘弹性",
    "高弹性",
    "容量核心",
    "高成交额",
    "行业龙头候选",
    "市值龙头候选",
    "强势股",
    "趋势强势",
    "技术形态",
    "活跃成交",
    "盘面异动待验证",
    "亏钱效应",
    "硬科技",
    "科技成长",
}
GENERIC_DIRECTION_TAGS = {"AI", "AI算力", "半导体"}
SPECIFIC_MAINLINE_HINTS = {
    "AI服务器",
    "服务器产业链",
    "数据中心(AIDC)",
    "算力租赁",
    "液冷",
    "HBM",
    "存储产业链",
    "CPO",
    "光通信产业链",
    "光模块",
    "光纤",
    "光棒",
    "PCB",
    "半导体材料",
    "电子材料",
    "MLCC电容",
    "元件",
    "电子陶瓷材料",
}
FORBIDDEN_CANDIDATE_NAMES = {
    "个股",
    "股票",
    "其他",
    "暂无",
    "综合",
    "全部",
    "待人工研判",
    "全球财经",
    "全球市场",
    "宏观事件",
    "海外政策",
    "海外监管",
    "全球流动性",
    "全球资本市场",
    "鑫多多大V",
}
CANONICAL_TAG_ALIASES = {
    "AI服务器": "AI服务器",
    "服务器": "服务器产业链",
    "算力": "AI算力",
    "碳化硅SIC": "碳化硅",
    "碳化硅sic": "碳化硅",
    "SiC": "碳化硅",
    "SIC": "碳化硅",
    "电子化学品Ⅱ": "电子化学品",
    "电子化学品II": "电子化学品",
    "其他电子Ⅱ": "其他电子",
    "其他电子II": "其他电子",
    "涨价": "涨价线索",
    "证券": "券商",
    "光通信": "光通信产业链",
    "光纤光棒": "光棒",
    "光棒涨价": "光棒",
    "半导体石英砂": "高纯石英砂",
    "电子特气": "电子气",
    "电子级TEOS": "TEOS",
    "正硅酸乙酯": "TEOS",
}
TAG_PARENT_EXPANSIONS = {
    "AI服务器": ["AI算力", "服务器产业链"],
    "AI算力": ["AI"],
    "算力芯片": ["AI算力", "半导体"],
    "算力租赁": ["AI算力"],
    "数据中心电源": ["AI服务器", "电力设备"],
    "CPU": ["算力芯片", "半导体"],
    "人形机器人": ["机器人"],
    "液冷": ["AI服务器"],
    "CPO": ["光通信产业链", "光模块"],
    "硅光": ["CPO", "光通信产业链"],
    "OCS光交换机": ["光通信产业链"],
    "光模块": ["光通信产业链"],
    "光模块零件": ["光模块", "光通信产业链"],
    "光模块设备": ["光模块", "光通信产业链"],
    "光芯片": ["光通信产业链", "光模块"],
    "光纤": ["光通信产业链"],
    "光棒": ["光纤", "光通信产业链"],
    "保偏光纤": ["特种光纤", "光纤", "光通信产业链"],
    "特种光纤": ["光纤", "光通信产业链"],
    "光纤陀螺": ["特种光纤", "军工", "光通信产业链"],
    "光纤环": ["特种光纤", "光纤陀螺"],
    "GlassBridge": ["CPO", "玻璃基板", "光通信产业链"],
    "存储芯片": ["存储产业链", "半导体"],
    "存储": ["存储产业链"],
    "HBM": ["存储产业链", "AI算力"],
    "长鑫存储相关": ["存储产业链"],
    "芯片代工": ["半导体"],
    "芯片封装堆叠": ["半导体", "先进封装"],
    "芯片测试": ["半导体", "半导体设备"],
    "芯片设备": ["半导体设备", "半导体"],
    "PCB设备": ["PCB"],
    "PCB钻针": ["PCB"],
    "CCL覆铜板": ["PCB", "电子材料"],
    "电子布": ["PCB", "电子材料"],
    "铜箔": ["PCB", "电子材料"],
    "半导体材料": ["电子材料", "半导体"],
    "电子化学品": ["半导体材料", "电子材料"],
    "电子气": ["电子化学品", "半导体材料"],
    "高纯石英砂": ["半导体材料", "电子材料"],
    "四氯化硅": ["半导体材料", "光纤"],
    "TEOS": ["电子化学品", "半导体材料"],
    "氧化锆": ["锆材料", "电子陶瓷材料", "半导体材料", "新材料"],
    "正丙醇锆": ["锆材料", "氧化锆", "电子陶瓷材料"],
    "钛酸钡": ["电子陶瓷材料", "MLCC电容", "新材料"],
    "MLCC电容": ["元件", "电子陶瓷材料"],
    "碳化硅": ["第三代半导体", "半导体材料"],
    "磷化铟": ["第三代半导体", "半导体材料"],
    "金刚石散热": ["半导体材料", "新材料"],
    "玻璃基板": ["先进封装", "电子材料"],
    "涨价线索": ["盘面异动待验证"],
    "客户验证": ["盘面异动待验证"],
    "产能扩充": ["盘面异动待验证"],
}
LOW_SAMPLE_DETAIL_TAGS = {
    "GlassBridge",
    "TEOS",
    "保偏光纤",
    "光棒",
    "光纤环",
    "光纤陀螺",
    "四氯化硅",
    "正丙醇锆",
    "氧化锆",
    "特种光纤",
    "电子陶瓷材料",
    "电子气",
    "钛酸钡",
    "锆材料",
    "高纯石英砂",
}
STOCK_DETAIL_TAG_SEEDS = {
    "SZ301580": ["氧化锆", "电子陶瓷材料"],
    "SH603663": ["氧化锆", "锆材料", "新材料"],
    "SZ002167": ["氧化锆", "锆材料", "新材料"],
    "SZ300285": ["氧化锆", "钛酸钡", "电子陶瓷材料", "MLCC电容"],
    "SH600552": ["氧化锆", "钛酸钡", "电子陶瓷材料"],
    "SZ002584": ["正丙醇锆", "氧化锆", "电子化学品"],
    "SZ000636": ["MLCC电容", "电子陶瓷材料"],
    "SH603678": ["MLCC电容", "电子陶瓷材料"],
    "SH603267": ["MLCC电容", "电子陶瓷材料"],
    "SZ002859": ["MLCC电容", "电子材料"],
    "SH603938": ["四氯化硅", "半导体材料"],
    "SH605366": ["四氯化硅", "TEOS", "半导体材料"],
    "SH688106": ["TEOS", "电子气", "电子化学品"],
    "SH688146": ["电子气", "电子化学品"],
    "SH688548": ["电子气", "电子化学品"],
    "SH601869": ["光棒", "光纤"],
    "SH600487": ["光棒", "光纤"],
    "SH600522": ["光棒", "光纤"],
    "SZ002491": ["光棒", "光纤"],
    "SZ000070": ["光棒", "光纤"],
    "SH688143": ["保偏光纤", "光纤环", "光纤陀螺"],
}


def configured_factor_interval_seconds() -> int:
    return max(300, int(os.environ.get("FACTOR_REFRESH_INTERVAL_SECONDS", "900")))


def history_batch_size() -> int:
    return max(10, int(os.environ.get("FACTOR_HISTORY_BATCH_SIZE", "120")))


def history_timeout_seconds() -> float:
    return max(1.0, float(os.environ.get("FACTOR_HISTORY_TIMEOUT_SECONDS", "3")))


def history_source_order() -> list[str]:
    configured = os.environ.get("FACTOR_HISTORY_SOURCES", "tencent,eastmoney,sina")
    allowed = {"tencent", "eastmoney", "sina"}
    sources = [item.strip().lower() for item in configured.split(",") if item.strip()]
    result = [source for source in sources if source in allowed]
    return result or ["tencent", "eastmoney", "sina"]


def spot_source_order() -> list[str]:
    configured = os.environ.get("FACTOR_SPOT_SOURCES", "sina,eastmoney")
    allowed = {"sina", "eastmoney"}
    sources = [item.strip().lower() for item in configured.split(",") if item.strip()]
    result = [source for source in sources if source in allowed]
    return result or ["sina", "eastmoney"]


def ensure_factor_defaults(db: Session) -> None:
    presets = list(db.scalars(select(FactorPeriodPreset).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id)))
    if not presets:
        db.add(FactorPeriodPreset(name="短线", periods_json=json.dumps([1, 2, 3, 5]), active=True, sort_order=10))
        db.add(FactorPeriodPreset(name="趋势", periods_json=json.dumps([5, 10, 20]), active=False, sort_order=20))
        db.commit()
        return
    if not any(preset.active for preset in presets):
        presets[0].active = True
        db.commit()


def mark_interrupted_factor_runs(db: Session) -> None:
    rows = list(db.scalars(select(FactorRefreshRun).where(FactorRefreshRun.completed_at.is_(None))))
    if not rows:
        return
    for row in rows:
        row.status = "error"
        row.message = "上次市场赚钱因子刷新被中断，已等待下一轮定时刷新。"
        row.completed_at = now_utc()
    db.commit()


def parse_periods(value: str | None) -> list[int]:
    if not value:
        return [1, 2, 3, 5]
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return [1, 2, 3, 5]
    return normalize_periods(raw)


def normalize_periods(values: list[int] | list[Any]) -> list[int]:
    periods: list[int] = []
    for value in values:
        try:
            period = int(value)
        except (TypeError, ValueError):
            continue
        if period <= 0 or period > 250:
            continue
        if period not in periods:
            periods.append(period)
    if not periods:
        raise ValueError("周期必须包含 1-250 之间的整数")
    return periods[:8]


def preset_to_out(preset: FactorPeriodPreset) -> dict[str, Any]:
    return {
        "id": preset.id,
        "name": preset.name,
        "periods": parse_periods(preset.periods_json),
        "active": preset.active,
        "sort_order": preset.sort_order,
        "created_at": preset.created_at,
        "updated_at": preset.updated_at,
    }


def tag_stock_counts(db: Session, tag_ids: list[int]) -> dict[int, int]:
    if not tag_ids:
        return {}
    rows = db.execute(
        select(FactorStockTag.tag_id, func.count(FactorStockTag.id))
        .where(FactorStockTag.tag_id.in_(sorted(set(tag_ids))))
        .group_by(FactorStockTag.tag_id)
    ).all()
    return {int(tag_id): int(count or 0) for tag_id, count in rows}


def tag_to_out(db: Session, tag: FactorTag, stock_count: int | None = None) -> dict[str, Any]:
    count = stock_count
    if count is None:
        count = db.scalar(select(func.count(FactorStockTag.id)).where(FactorStockTag.tag_id == tag.id)) or 0
    role = factor_tag_trade_role(tag.name, 0, int(count or 0))
    return {
        "id": tag.id,
        "name": tag.name,
        "color": tag.color,
        "stock_count": int(count or 0),
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
        "is_generic_direction": role == "direction_only" and tag.name in GENERIC_DIRECTION_TAGS,
        "trade_role": role,
        "tag_kind": factor_tag_evidence_level(0, int(count or 0), role),
    }


def refresh_run_to_out(run: FactorRefreshRun | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "message": run.message,
        "fetched_count": run.fetched_count,
        "cached_count": run.cached_count,
        "failed_count": run.failed_count,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def list_factor_tags(db: Session) -> list[dict[str, Any]]:
    tags = list(db.scalars(select(FactorTag).order_by(FactorTag.name, FactorTag.id)).all())
    counts = tag_stock_counts(db, [tag.id for tag in tags])
    return [tag_to_out(db, tag, counts.get(tag.id, 0)) for tag in tags]


def create_factor_tag(db: Session, name: str, color: str | None = None) -> dict[str, Any]:
    cleaned = clean_tag_name(name)
    if cleaned == UNTAGGED_NAME:
        raise ValueError("未打标是系统标签，不能手动创建")
    existing = db.scalar(select(FactorTag).where(FactorTag.name == cleaned))
    if existing:
        return tag_to_out(db, existing)
    tag = FactorTag(name=cleaned, color=clean_color(color))
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag_to_out(db, tag)


def create_factor_tags_bulk(db: Session, names: list[str]) -> list[dict[str, Any]]:
    cleaned_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = clean_tag_name(name)
        if cleaned == UNTAGGED_NAME:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_names.append(cleaned)
    if not cleaned_names:
        raise ValueError("没有可加入的标签")
    return [create_factor_tag(db, name, None) for name in cleaned_names]


def update_factor_tag(db: Session, tag_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    tag = db.get(FactorTag, tag_id)
    if not tag:
        raise ValueError("标签不存在")
    if "name" in payload and payload["name"] is not None:
        cleaned = clean_tag_name(payload["name"])
        if cleaned == UNTAGGED_NAME:
            raise ValueError("未打标是系统标签，不能手动维护")
        duplicate = db.scalar(select(FactorTag).where(FactorTag.name == cleaned, FactorTag.id != tag_id))
        if duplicate:
            raise ValueError("标签已存在")
        tag.name = cleaned
    if "color" in payload:
        tag.color = clean_color(payload.get("color"))
    db.commit()
    db.refresh(tag)
    return tag_to_out(db, tag)


def delete_factor_tag(db: Session, tag_id: int) -> dict[str, Any]:
    tag = db.get(FactorTag, tag_id)
    if not tag:
        raise ValueError("标签不存在")
    removed_stock_count = db.scalar(select(func.count(FactorStockTag.id)).where(FactorStockTag.tag_id == tag.id)) or 0
    tag_name = tag.name
    db.delete(tag)
    db.commit()
    return {"status": "ok", "message": "标签已删除", "removed_stock_count": removed_stock_count, "tag_name": tag_name}


def clean_tag_name(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    cleaned = canonical_factor_tag_name(cleaned)
    if not cleaned:
        raise ValueError("标签名不能为空")
    if cleaned in FORBIDDEN_CANDIDATE_NAMES:
        raise ValueError("标签名不适合进入因子库")
    if len(cleaned) > 80:
        raise ValueError("标签名不能超过 80 个字符")
    return cleaned


def normalize_review_values(values: list[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = " ".join(str(value).strip().split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def review_signature(values: list[str] | None) -> str:
    parts = sorted(value.casefold() for value in normalize_review_values(values))
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def tag_signature(tags: list[FactorTag]) -> str:
    return review_signature([tag.name for tag in tags])


def clean_color(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:32]


def list_factor_presets(db: Session) -> list[dict[str, Any]]:
    ensure_factor_defaults(db)
    presets = db.scalars(select(FactorPeriodPreset).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id)).all()
    return [preset_to_out(preset) for preset in presets]


def create_factor_preset(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    name = clean_preset_name(payload.get("name") or "")
    periods = normalize_periods(payload.get("periods") or [])
    duplicate = db.scalar(select(FactorPeriodPreset).where(FactorPeriodPreset.name == name))
    if duplicate:
        raise ValueError("周期方案已存在")
    if payload.get("active"):
        db.execute(select(FactorPeriodPreset))
        for preset in db.scalars(select(FactorPeriodPreset)).all():
            preset.active = False
    preset = FactorPeriodPreset(
        name=name,
        periods_json=json.dumps(periods, ensure_ascii=False),
        active=bool(payload.get("active")),
        sort_order=int(payload.get("sort_order") or 100),
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)
    ensure_factor_defaults(db)
    return preset_to_out(preset)


def update_factor_preset(db: Session, preset_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    preset = db.get(FactorPeriodPreset, preset_id)
    if not preset:
        raise ValueError("周期方案不存在")
    if "name" in payload and payload["name"] is not None:
        name = clean_preset_name(payload["name"])
        duplicate = db.scalar(select(FactorPeriodPreset).where(FactorPeriodPreset.name == name, FactorPeriodPreset.id != preset_id))
        if duplicate:
            raise ValueError("周期方案已存在")
        preset.name = name
    if "periods" in payload and payload["periods"] is not None:
        preset.periods_json = json.dumps(normalize_periods(payload["periods"]), ensure_ascii=False)
    if "sort_order" in payload and payload["sort_order"] is not None:
        preset.sort_order = int(payload["sort_order"])
    db.commit()
    db.refresh(preset)
    return preset_to_out(preset)


def delete_factor_preset(db: Session, preset_id: int) -> None:
    presets = list(db.scalars(select(FactorPeriodPreset).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id)))
    if len(presets) <= 1:
        raise ValueError("至少保留一个周期方案")
    preset = db.get(FactorPeriodPreset, preset_id)
    if not preset:
        raise ValueError("周期方案不存在")
    was_active = preset.active
    db.delete(preset)
    db.commit()
    if was_active:
        replacement = db.scalar(select(FactorPeriodPreset).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id).limit(1))
        if replacement:
            replacement.active = True
            db.commit()


def activate_factor_preset(db: Session, preset_id: int) -> dict[str, Any]:
    preset = db.get(FactorPeriodPreset, preset_id)
    if not preset:
        raise ValueError("周期方案不存在")
    for row in db.scalars(select(FactorPeriodPreset)).all():
        row.active = row.id == preset_id
    db.commit()
    db.refresh(preset)
    return preset_to_out(preset)


def clean_preset_name(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError("周期方案名称不能为空")
    if len(cleaned) > 80:
        raise ValueError("周期方案名称不能超过 80 个字符")
    return cleaned


def factor_stock_tags(db: Session, full_code: str) -> dict[str, Any]:
    stock = resolve_stock(db, full_code)
    if not stock:
        raise ValueError("股票不存在")
    tags = tags_for_full_codes(db, [stock.full_code]).get(stock.full_code, [])
    counts = tag_stock_counts(db, [tag.id for tag in tags])
    review = db.scalar(select(FactorStockReview).where(FactorStockReview.full_code == stock.full_code))
    watch = db.scalar(select(WatchlistAnnouncementStock).where(WatchlistAnnouncementStock.full_code == stock.full_code))
    return {
        "code": stock.code,
        "name": stock.name,
        "exchange": stock.exchange,
        "full_code": stock.full_code,
        "tags": [tag_to_out(db, tag, counts.get(tag.id, 0)) for tag in tags],
        "tag_status": inferred_review_status(review.status if review else None, bool(tags)),
        "watchlist_added": bool(watch),
        "watchlist_enabled": bool(watch.enabled) if watch else False,
        "watchlist_push_enabled": bool(watch.push_enabled) if watch else False,
    }


def update_factor_stock_tags(db: Session, full_code: str, tag_ids: list[int]) -> dict[str, Any]:
    stock = resolve_stock(db, full_code)
    if not stock:
        raise ValueError("股票不存在")
    unique_ids = sorted({int(tag_id) for tag_id in tag_ids})
    if unique_ids:
        found_ids = {
            row.id
            for row in db.scalars(select(FactorTag).where(FactorTag.id.in_(unique_ids))).all()
        }
        missing = [tag_id for tag_id in unique_ids if tag_id not in found_ids]
        if missing:
            raise ValueError("存在无效标签")
    db.execute(delete(FactorStockTag).where(FactorStockTag.full_code == stock.full_code))
    for tag_id in unique_ids:
        db.add(FactorStockTag(full_code=stock.full_code, tag_id=tag_id))
    db.commit()
    return factor_stock_tags(db, stock.full_code)


def update_factor_stock_status(db: Session, full_code: str, status: str, review_reasons: list[str] | None = None) -> dict[str, Any]:
    if status not in FACTOR_REVIEW_STATUSES:
        raise ValueError("打标状态不正确")
    stock = resolve_stock(db, full_code)
    if not stock:
        raise ValueError("股票不存在")
    review = db.scalar(select(FactorStockReview).where(FactorStockReview.full_code == stock.full_code))
    if not review:
        review = FactorStockReview(
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            full_code=stock.full_code,
        )
        db.add(review)
    review.code = stock.code
    review.name = stock.name
    review.exchange = stock.exchange
    review.status = status
    if status == "done":
        tags = tags_for_full_codes(db, [stock.full_code]).get(stock.full_code, [])
        normalized_reasons = normalize_review_values(review_reasons)
        review.reviewed_reasons_json = json.dumps(normalized_reasons, ensure_ascii=False)
        review.reviewed_reason_signature = review_signature(normalized_reasons)
        review.reviewed_tag_signature = tag_signature(tags)
        review.reviewed_at = now_utc()
    db.commit()
    return factor_stock_tags(db, stock.full_code)


def exclude_factor_stock(db: Session, full_code: str, reason: str | None = None) -> dict[str, str]:
    normalized = normalize_full_code(full_code)
    snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == normalized))
    stock = db.scalar(select(AStock).where(AStock.full_code == normalized))
    code = (snapshot.code if snapshot else None) or (stock.code if stock else None) or normalized[-6:]
    exchange = (snapshot.exchange if snapshot else None) or (stock.exchange if stock else None) or normalized[:2]
    name = (snapshot.name if snapshot else None) or (stock.name if stock else None) or normalized
    excluded = db.scalar(select(FactorExcludedStock).where(FactorExcludedStock.full_code == normalized))
    if not excluded:
        excluded = FactorExcludedStock(code=code, name=name, exchange=exchange, full_code=normalized)
        db.add(excluded)
    excluded.code = code
    excluded.name = name
    excluded.exchange = exchange
    excluded.reason = reason or "manual"
    review = db.scalar(select(FactorStockReview).where(FactorStockReview.full_code == normalized))
    if review:
        review.status = "done"
    db.commit()
    return {"status": "ok", "message": f"{name} 已从市场风格池剔除"}


def resolve_stock(db: Session, value: str) -> AStock | None:
    normalized = normalize_full_code(value)
    stock = db.scalar(select(AStock).where(AStock.full_code == normalized))
    if stock:
        return stock
    ensure_stock_universe(db)
    return db.scalar(select(AStock).where(AStock.full_code == normalized))


def normalize_full_code(value: str) -> str:
    raw = value.strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")) and len(raw) >= 8:
        return raw[:8]
    code = "".join(ch for ch in raw if ch.isdigit())[:6]
    if len(code) != 6:
        raise ValueError("股票代码格式不正确")
    return f"{exchange_from_code(code)}{code}"


def active_preset(db: Session) -> FactorPeriodPreset:
    ensure_factor_defaults(db)
    preset = db.scalar(select(FactorPeriodPreset).where(FactorPeriodPreset.active.is_(True)).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id))
    if preset:
        return preset
    preset = db.scalar(select(FactorPeriodPreset).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id))
    if not preset:
        raise ValueError("周期方案初始化失败")
    preset.active = True
    db.commit()
    return preset


def style_clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def is_factor_board_style_tag(tag_name: str | None) -> bool:
    name = str(tag_name or "").strip()
    return name in FACTOR_BOARD_STYLE_TAGS


def factor_tag_trade_role(tag_name: str | None, evidence_days: int = 0, stock_count: int = 0) -> str:
    name = str(tag_name or "").strip()
    if not name:
        return "risk_only"
    if "亏钱" in name or "退潮" in name:
        return "risk_only"
    if name in GENERIC_DIRECTION_TAGS:
        return "direction_only"
    if name in FACTOR_BACKTEST_CONTEXT_TAGS or is_factor_board_style_tag(name):
        return "direction_only"
    if name in SPECIFIC_MAINLINE_HINTS or name in TAG_PARENT_EXPANSIONS or any(hint in name for hint in SPECIFIC_MAINLINE_HINTS):
        if evidence_days >= 2 and stock_count >= 2:
            return "confirmed_mainline"
        return "candidate_mainline"
    if evidence_days >= 2 and stock_count >= 3:
        return "candidate_mainline"
    return "direction_only"


def factor_tag_evidence_level(evidence_days: int, stock_count: int, role: str) -> str:
    if role == "risk_only":
        return "风险"
    if role == "direction_only":
        return "方向词"
    if evidence_days >= 3 and stock_count >= 4:
        return "强确认"
    if evidence_days >= 2 and stock_count >= 2:
        return "已确认"
    return "待验证"


def enrich_factor_tag_signal(row: dict[str, Any]) -> dict[str, Any]:
    evidence_days = len(set(int(period) for period in row.get("periods") or [] if period))
    stock_count = int(row.get("stock_count") or row.get("sample_count") or 0)
    tag_name = str(row.get("tag_name") or row.get("style_name") or "")
    role = factor_tag_trade_role(tag_name, evidence_days, stock_count)
    return {
        **row,
        "evidence_days": evidence_days,
        "evidence_level": factor_tag_evidence_level(evidence_days, stock_count, role),
        "is_generic_direction": role == "direction_only" and tag_name in GENERIC_DIRECTION_TAGS,
        "trade_role": role,
    }


def real_tag_rankings(
    rows: list[dict[str, Any]],
    limit: int = 3,
    board_style: bool | None = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("system") or row.get("tag_id") is None:
            continue
        is_board_style = is_factor_board_style_tag(row.get("tag_name"))
        if board_style is True and not is_board_style:
            continue
        if board_style is False and is_board_style:
            continue
        result.append(row)
        if len(result) >= limit:
            break
    return result


def aggregate_style_tags(
    stock_rankings: list[dict[str, Any]],
    key: str,
    reverse: bool,
    limit: int = 5,
    board_style: bool | None = False,
) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    weighted_values: dict[int, list[tuple[float, int]]] = {}
    period_weights = {1: 0.9, 2: 1.0, 3: 1.2, 5: 1.25, 10: 1.35}
    for ranking in stock_rankings:
        period = int(ranking.get("period") or 0)
        rows = real_tag_rankings(ranking.get(key) or [], 3, board_style)
        for row in rows:
            tag_id = int(row["tag_id"])
            weight = period_weights.get(period, 1.0)
            stock_count = max(1, int(row.get("stock_count") or 0))
            bucket = buckets.setdefault(
                tag_id,
                {
                    "tag_id": tag_id,
                    "tag_name": row.get("tag_name") or "",
                    "color": row.get("color"),
                    "stock_count": 0,
                    "stock_codes": [],
                    "system": False,
                    "periods": [],
                    "_score": 0.0,
                },
            )
            bucket["stock_count"] = max(int(bucket["stock_count"]), stock_count)
            bucket["periods"].append(period)
            bucket["_score"] += (abs(float(row.get("average_return_pct") or 0)) * 0.55 + min(stock_count, 12) * 1.3 + 8) * weight
            existing_codes = set(bucket["stock_codes"])
            for code in row.get("stock_codes") or []:
                if code not in existing_codes and len(bucket["stock_codes"]) < 20:
                    bucket["stock_codes"].append(code)
                    existing_codes.add(code)
            weighted_values.setdefault(tag_id, []).append((float(row.get("average_return_pct") or 0), stock_count))

    results: list[dict[str, Any]] = []
    for tag_id, bucket in buckets.items():
        values = weighted_values.get(tag_id) or []
        denominator = sum(weight for _value, weight in values) or 1
        average = sum(value * weight for value, weight in values) / denominator
        periods = sorted(set(int(period) for period in bucket["periods"] if period))
        results.append(
            {
                "tag_id": bucket["tag_id"],
                "tag_name": bucket["tag_name"],
                "color": bucket["color"],
                "stock_count": bucket["stock_count"],
                "average_return_pct": round(average, 4),
                "stock_codes": bucket["stock_codes"],
                "system": False,
                "periods": periods,
                "_score": bucket["_score"] + len(periods) * 12,
            }
        )
    results.sort(key=lambda item: item["_score"], reverse=True)
    return [
        enrich_factor_tag_signal({key: value for key, value in item.items() if key != "_score"})
        for item in results[:limit]
    ]


def period_top_tag_sets(
    stock_rankings: list[dict[str, Any]],
    key: str,
    board_style: bool | None = False,
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    for ranking in stock_rankings:
        period = int(ranking.get("period") or 0)
        result[period] = {int(row["tag_id"]) for row in real_tag_rankings(ranking.get(key) or [], 3, board_style)}
    return result


EFFECT_STYLE_RULES_ENABLED = False
STYLE_THEME_CATALOG: list[dict[str, Any]] = []


def text_matches_theme(text: str, keywords: list[str]) -> bool:
    normalized = text.casefold()
    return any(keyword.casefold() in normalized for keyword in keywords)


def build_recent_style_tags(dominant_tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    themes: dict[str, dict[str, Any]] = {}
    for tag in dominant_tags:
        if tag.get("trade_role") == "direction_only":
            continue
        tag_name = str(tag.get("tag_name") or "")
        if not tag_name:
            continue
        tag_strength = abs(float(tag.get("average_return_pct") or 0)) * 0.8
        tag_strength += min(int(tag.get("stock_count") or 0), 12) * 1.8
        tag_strength += len(tag.get("periods") or []) * 5
        for item in STYLE_THEME_CATALOG:
            if not text_matches_theme(tag_name, item["keywords"]):
                continue
            theme = themes.setdefault(
                item["name"],
                {
                    "name": item["name"],
                    "category": item["category"],
                    "score": 0.0,
                    "description": item["description"],
                    "action": item["action"],
                    "source_tags": [],
                },
            )
            theme["score"] += tag_strength
            if tag_name not in theme["source_tags"]:
                theme["source_tags"].append(tag_name)
    ranked = sorted(themes.values(), key=lambda row: row["score"], reverse=True)
    for row in ranked:
        row["score"] = round(style_clamp(float(row["score"]), 0, 100), 2)
        row["source_tags"] = row["source_tags"][:6]
    return ranked[:6]


def build_participation_advice(
    style_name: str,
    primary_style: dict[str, Any] | None,
    coverage_rate: float,
    breadth_score: float,
    risk_score: float,
    dominant_tags: list[dict[str, Any]],
) -> tuple[str, str]:
    primary_name = primary_style.get("name") if primary_style else None
    fallback_names = "、".join(row["tag_name"] for row in dominant_tags[:2]) or "强势标签"
    focus_name = primary_name or fallback_names
    if coverage_rate < 0.7:
        return (
            f"先把结论当线索：已有样本指向 {focus_name}，但标签覆盖不足，不适合据此定主仓。",
            "优先补齐强势榜和亏钱榜标签；样本不足时，少用单只股票涨幅推导整体风格。",
        )
    if style_name == "亏钱效应主导" or risk_score >= 70:
        return (
            f"当前主要任务是防守；若参与，只看 {focus_name} 里确认度最高、回撤可控的核心股。",
            "回避高位弱承接和只有题材名、没有订单/业绩验证的扩散股。",
        )
    if breadth_score < 45:
        return (
            f"赚钱效应偏窄，参与重点放在 {focus_name} 的核心和低位扩散，不做全市场扩散假设。",
            "回避跟风补涨过多、样本只有一两只的标签。",
        )
    return (
        f"当前优先围绕 {focus_name} 参与；若后续广度继续改善，可从核心向同链条扩散观察。",
        "回避亏钱标签里的反弹追高，以及与主导风格无关的随机拉升。",
    )


def market_effect_phase(
    breadth_score: float,
    risk_score: float,
    persistence_score: float,
    evidence_days: int,
    has_confirmed_mainline: bool,
) -> dict[str, str]:
    if risk_score >= 70:
        return {
            "key": "overheated",
            "label": "过热/退潮期",
            "description": "亏钱面或回撤风险偏高，主线信号需要降级处理。",
        }
    if has_confirmed_mainline and breadth_score >= 55 and persistence_score >= 50:
        return {
            "key": "confirmed",
            "label": "确认期",
            "description": "已有具体主线和扩散确认，按高门槛筛选核心与同链条扩散。",
        }
    if evidence_days <= 2 and breadth_score >= 40 and risk_score < 65:
        return {
            "key": "launch",
            "label": "启动期",
            "description": "主线刚开始扩散，可以低门槛捕捉第一段，但要等待具体标签验证。",
        }
    return {
        "key": "observe",
        "label": "观察期",
        "description": "赚钱效应尚未稳定，优先观察方向词是否落到具体产业链。",
    }


def build_factor_effect_style(
    db: Session,
    stock_rankings: list[dict[str, Any]],
    snapshots: list[FactorQuoteSnapshot],
    tag_map: dict[str, list[FactorTag]],
) -> dict[str, Any]:
    ranked_codes = {
        snapshot.full_code
        for snapshot in snapshots
        if parse_returns(snapshot.returns_json)
    }
    total_count = len(ranked_codes) or len(snapshots)
    tagged_count = sum(1 for code in ranked_codes if tag_map.get(code))
    coverage_rate = tagged_count / total_count if total_count else 0.0
    coverage_count = max((int(ranking.get("coverage_count") or 0) for ranking in stock_rankings), default=0)
    if coverage_rate >= 0.9:
        coverage_label = "标签覆盖充足"
    elif coverage_rate >= 0.7:
        coverage_label = "标签覆盖一般"
    else:
        coverage_label = "标签覆盖不足"

    try:
        from app import market_style_effects

        if hasattr(market_style_effects, "market_effect_context"):
            market_context = market_style_effects.market_effect_context(db)
        elif hasattr(market_style_effects, "build_market_effect_context_from_db"):
            market_context = market_style_effects.build_market_effect_context_from_db(db)
        else:
            market_context = {}
    except Exception:
        market_context = {}

    breadth_score = float(market_context.get("breadth_score") or 0)
    sentiment_score = float(market_context.get("sentiment_score") or 0)
    risk_score = float(market_context.get("risk_score") or 100)
    persistence_score = float(market_context.get("persistence_score") or 0)
    market_components = dict(market_context.get("components") or {})
    rising_ratio = float(market_components.get("rising_ratio") or 0)

    if not EFFECT_STYLE_RULES_ENABLED:
        return {
            "rules_enabled": False,
            "style_name": "",
            "summary": "",
            "confidence": 0.0,
            "health": "",
            "coverage_rate": round(coverage_rate, 4),
            "coverage_label": coverage_label,
            "coverage_count": coverage_count,
            "tagged_count": tagged_count,
            "total_count": total_count,
            "primary_style": None,
            "recent_style_tags": [],
            "participation_advice": None,
            "avoid_advice": None,
            "dominant_tags": [],
            "risk_tags": [],
            "board_dominant_tags": [],
            "board_risk_tags": [],
            "tradable_mainlines": [],
            "confirmed_mainlines": [],
            "direction_only_tags": [],
            "downgraded_tags": [],
            "current_phase": {},
            "risk_flags": [],
            "reasons": [],
            "action": None,
            "market": {
                "trade_date": market_context.get("trade_date"),
                "base_effect_score": round(float(market_context.get("base_effect_score") or 0), 2),
                "breadth_score": round(breadth_score, 2),
                "sentiment_score": round(sentiment_score, 2),
                "risk_score": round(risk_score, 2),
                "persistence_score": round(persistence_score, 2),
                "effect_score_delta_5d": round(float(market_context.get("effect_score_delta_5d") or 0), 2),
                "rising_ratio": round(rising_ratio, 4),
                "components": market_components,
            },
        }

    dominant_tags = aggregate_style_tags(stock_rankings, "winning_tags", reverse=True, board_style=False)
    risk_tags = aggregate_style_tags(stock_rankings, "losing_tags", reverse=False, board_style=False)
    board_dominant_tags = aggregate_style_tags(stock_rankings, "board_winning_tags", reverse=True, board_style=True)
    board_risk_tags = aggregate_style_tags(stock_rankings, "board_losing_tags", reverse=False, board_style=True)
    tradable_mainlines = [
        row for row in dominant_tags
        if row.get("trade_role") in {"confirmed_mainline", "candidate_mainline"}
    ]
    confirmed_mainlines = [row for row in tradable_mainlines if row.get("trade_role") == "confirmed_mainline"]
    direction_only_tags = [row for row in dominant_tags if row.get("trade_role") == "direction_only"]
    downgraded_tags = [
        row for row in dominant_tags
        if row.get("is_generic_direction") or row.get("trade_role") == "direction_only"
    ]
    recent_style_tags = build_recent_style_tags(tradable_mainlines)
    primary_style = recent_style_tags[0] if recent_style_tags else None
    top = (confirmed_mainlines or tradable_mainlines or dominant_tags)[0] if dominant_tags else None
    top_period_count = len(top.get("periods") or []) if top else 0
    top_sample_count = int(top.get("stock_count") or 0) if top else 0
    top_return = float(top.get("average_return_pct") or 0) if top else 0.0
    win_strength_rows = tradable_mainlines[:3] or dominant_tags[:3]
    win_strength = sum(float(row.get("average_return_pct") or 0) for row in win_strength_rows) / max(len(win_strength_rows), 1)
    loss_strength = sum(abs(float(row.get("average_return_pct") or 0)) for row in risk_tags[:3]) / max(len(risk_tags[:3]), 1)
    today_tags = period_top_tag_sets(stock_rankings, "winning_tags").get(1, set())
    longer_tags = set().union(*(tags for period, tags in period_top_tag_sets(stock_rankings, "winning_tags").items() if period in {3, 5, 10, 20}))
    today_overlap = len(today_tags & longer_tags) / max(len(today_tags), 1) if today_tags else 0.0
    top3_sample_total = sum(int(row.get("stock_count") or 0) for row in (tradable_mainlines[:3] or dominant_tags[:3]))
    concentration = top_sample_count / max(top3_sample_total, 1)

    reasons: list[str] = []
    risk_flags: list[str] = []
    if coverage_rate < 0.7:
        reasons.append(f"标签覆盖率 {coverage_rate * 100:.1f}%，低于 70%，风格判断先降级。")
    elif coverage_rate < 0.9:
        reasons.append(f"标签覆盖率 {coverage_rate * 100:.1f}%，可判断但置信度打折。")
    else:
        reasons.append(f"标签覆盖率 {coverage_rate * 100:.1f}%，可以按因子标签判断风格。")
    if top:
        reasons.append(f"主导标签 {top['tag_name']} 出现在 {top_period_count} 个周期，样本 {top_sample_count} 只。")
    if direction_only_tags:
        reasons.append(f"{'、'.join(row['tag_name'] for row in direction_only_tags[:3])} 只作为方向词，不单独生成参与信号。")
    if market_context:
        reasons.append(
            f"市场过滤：广度 {breadth_score:.0f}，情绪 {sentiment_score:.0f}，风险 {risk_score:.0f}，"
            f"上涨占比 {rising_ratio * 100:.1f}%。"
        )
    if breadth_score < 45:
        risk_flags.append("广度偏弱")
    if risk_score >= 60:
        risk_flags.append("亏钱面偏大")
    if top and top_sample_count <= 2:
        risk_flags.append("样本偏少")
    if concentration >= 0.65 and len(dominant_tags) >= 2:
        risk_flags.append("标签集中度高")
    if today_tags and longer_tags and today_overlap <= 0.34:
        risk_flags.append("短线轮动快")

    style_name = "风格不清晰"
    if coverage_rate < 0.7 or not top or top3_sample_total < 3:
        style_name = "风格不清晰"
    elif loss_strength > max(win_strength, 0) * 1.1 and risk_score >= 55:
        style_name = "亏钱效应主导"
    elif today_tags and longer_tags and today_overlap <= 0.34 and top_period_count <= 2:
        style_name = "轮动切换型"
    elif top_period_count >= 3 and breadth_score >= 48 and risk_score < 60 and top_sample_count >= 3:
        style_name = "主线扩散型"
    elif top_return >= 8 or breadth_score < 45 or risk_score >= 60 or top_sample_count <= 2:
        style_name = "局部抱团型"
    elif top_period_count >= 2:
        style_name = "主线扩散型"

    if dominant_tags and not tradable_mainlines:
        style_name = "方向观察型"
        risk_flags.append("缺少具体主线")

    if style_name == "主线扩散型" and (breadth_score < 55 or risk_score >= 55):
        risk_flags.append("扩散仍需确认")
    if style_name == "局部抱团型" and top:
        reasons.append("强标签存在，但样本、广度或亏钱风险不足以支持全面扩散。")
    if style_name == "轮动切换型":
        reasons.append("今天的赚钱标签与短中长期标签重合较低，说明风格切换较快。")
    if style_name == "亏钱效应主导":
        reasons.append("亏钱标签跌幅压力超过赚钱标签，且市场风险分偏高。")

    sample_score = min(top3_sample_total / 30, 1.0) * 20
    consistency_score = min(top_period_count / 4, 1.0) * 20
    coverage_score = min(coverage_rate / 0.9, 1.0) * 35
    market_score = ((breadth_score + sentiment_score) / 2) * 0.15 + (100 - risk_score) * 0.10
    confidence = style_clamp(coverage_score + sample_score + consistency_score + market_score)
    if coverage_rate < 0.7:
        confidence = min(confidence, 40)
    if style_name == "风格不清晰":
        confidence = min(confidence, 55)

    if risk_score >= 70:
        health = "风险高"
    elif breadth_score < 45 or "样本偏少" in risk_flags:
        health = "偏窄"
    elif breadth_score >= 55 and risk_score < 55 and top_period_count >= 3:
        health = "健康"
    else:
        health = "待确认"

    dominant_names = "、".join(row["tag_name"] for row in (tradable_mainlines[:2] or dominant_tags[:2])) or "暂无主导标签"
    style_focus_name = primary_style["name"] if primary_style else dominant_names
    style_source_names = "、".join(primary_style.get("source_tags") or []) if primary_style else dominant_names
    risk_names = "、".join(row["tag_name"] for row in risk_tags[:2]) or "亏钱标签不清晰"
    if coverage_rate < 0.7:
        if primary_style:
            summary = f"标签覆盖不足，但已有样本偏向{primary_style['name']}；当前仅 {coverage_rate * 100:.1f}% 股票有标签，先当线索看。"
        else:
            summary = f"标签覆盖不足：当前仅 {coverage_rate * 100:.1f}% 股票有标签，先补齐标签后再判断赚钱效应风格。"
        action = "先补标签，再用本页判断风格。"
    elif style_name == "主线扩散型":
        summary = f"当前是主线扩散型：{style_focus_name} 占优，具体落点是 {style_source_names}，市场广度对风格有一定确认。"
        action = "优先围绕反复出现的赚钱标签做复盘。"
    elif style_name == "局部抱团型":
        summary = f"当前是局部抱团型：{style_focus_name} 很强，具体落点是 {style_source_names}，但广度、样本或亏钱面提示不要当成全面行情。"
        action = "降低追高权重，重点看强标签内部是否继续扩散。"
    elif style_name == "轮动切换型":
        summary = f"当前是轮动切换型：短线标签和中长期标签重合低，风格切换快。"
        action = "用更短周期验证，避免拿旧主线解释新行情。"
    elif style_name == "亏钱效应主导":
        summary = f"当前亏钱效应主导：{risk_names} 的负反馈更强，赚钱标签需要降级看。"
        action = "先控制风险，等待亏钱标签收敛。"
    elif style_name == "方向观察型":
        summary = "当前只有泛方向词占优，还没有足够具体产业链主线；先观察扩散，不直接当买点。"
        action = "等待方向词落到具体链条，并至少出现两次确认。"
    else:
        summary = "当前风格不清晰：标签优势、样本数或市场确认度还不够。"
        action = "继续补标签并观察跨周期重复出现的方向。"
    participation_advice, avoid_advice = build_participation_advice(
        style_name,
        primary_style,
        coverage_rate,
        breadth_score,
        risk_score,
        tradable_mainlines or dominant_tags,
    )

    if direction_only_tags and not tradable_mainlines:
        participation_advice = "当前只适合观察方向词，等待具体主线和证据天数补齐后再参与。"
        avoid_advice = "不要因为 AI/半导体这类大词单独追买；优先补充光模块、PCB、服务器产业链等具体标签。"

    return {
        "style_name": style_name,
        "summary": summary,
        "confidence": round(confidence, 2),
        "health": health,
        "coverage_rate": round(coverage_rate, 4),
        "coverage_label": coverage_label,
        "coverage_count": coverage_count,
        "tagged_count": tagged_count,
        "total_count": total_count,
        "primary_style": primary_style,
        "recent_style_tags": recent_style_tags,
        "participation_advice": participation_advice,
        "avoid_advice": avoid_advice,
        "dominant_tags": dominant_tags[:5],
        "risk_tags": risk_tags[:5],
        "board_dominant_tags": board_dominant_tags[:5],
        "board_risk_tags": board_risk_tags[:5],
        "tradable_mainlines": tradable_mainlines[:5],
        "confirmed_mainlines": confirmed_mainlines[:5],
        "direction_only_tags": direction_only_tags[:5],
        "downgraded_tags": downgraded_tags[:5],
        "current_phase": market_effect_phase(breadth_score, risk_score, persistence_score, top_period_count, bool(confirmed_mainlines)),
        "risk_flags": risk_flags,
        "reasons": reasons[:6],
        "action": action,
        "market": {
            "trade_date": market_context.get("trade_date"),
            "base_effect_score": round(float(market_context.get("base_effect_score") or 0), 2),
            "breadth_score": round(breadth_score, 2),
            "sentiment_score": round(sentiment_score, 2),
            "risk_score": round(risk_score, 2),
            "persistence_score": round(persistence_score, 2),
            "effect_score_delta_5d": round(float(market_context.get("effect_score_delta_5d") or 0), 2),
            "rising_ratio": round(rising_ratio, 4),
            "components": market_components,
        },
    }


def factor_effect_backtest(
    db: Session,
    label_source: str = "factor",
    lookback_days: int = 20,
    momentum_period: int = 3,
    forward_period: int = 5,
    top_n: int = 80,
    min_sample: int = 4,
) -> dict[str, Any]:
    label_source = str(label_source or "factor").lower()
    if label_source not in {"network", "factor"}:
        label_source = "factor"
    lookback_days = max(5, min(int(lookback_days or 20), 60))
    momentum_period = max(1, min(int(momentum_period or 3), 20))
    forward_period = max(1, min(int(forward_period or 5), 20))
    top_n = max(30, min(int(top_n or 100), 500))
    min_sample = max(1, min(int(min_sample or 3), 10))

    table = factor_backtest_bars_table(db)
    if not table:
        return factor_backtest_empty("insufficient_data", "没有可用于回测的日线缓存。")

    tag_labels, tagged_stock_count, tag_link_count, label_source_name = factor_backtest_labels(db, label_source)
    if not tag_labels:
        message = "还没有可用的网络/板块标签，无法做网络标签回测。" if label_source == "network" else "还没有因子库标签，无法回测标签赚钱效应。"
        return factor_backtest_empty("insufficient_tags", message, label_source)

    trade_dates = factor_backtest_trade_dates(db, table)
    required_count = lookback_days + momentum_period + forward_period + 3
    if len(trade_dates) < required_count:
        return factor_backtest_empty("insufficient_data", "日线历史不足，无法完成过去一个月的前后验回测。")

    eval_dates = trade_dates[-(lookback_days + forward_period) : -forward_period]
    start_date = trade_dates[max(0, len(trade_dates) - (lookback_days + forward_period + momentum_period + 90))]
    histories = factor_backtest_histories(db, table, start_date)
    date_candidates = factor_backtest_date_candidates(histories, eval_dates, momentum_period, forward_period, tag_labels)
    result = factor_run_backtest(date_candidates, top_n, min_sample)
    sweep = []
    for sweep_top_n, sweep_min_sample in [(50, 2), (80, 3), (80, 4), (80, 6), (100, 3), (150, 4), (200, 4)]:
        sweep_result = factor_run_backtest(date_candidates, sweep_top_n, sweep_min_sample)
        sweep.append(
            {
                "top_n": sweep_top_n,
                "min_sample": sweep_min_sample,
                "signal_days": sweep_result["signal_days"],
                "avg_forward_return_pct": sweep_result["avg_forward_return_pct"],
                "hit_rate_pct": sweep_result["hit_rate_pct"],
                "best_match_rate_pct": sweep_result["best_match_rate_pct"],
            }
        )

    signal_days = int(result["signal_days"])
    avg_forward = result["avg_forward_return_pct"]
    hit_rate = result["hit_rate_pct"]
    min_labeled = 100
    is_valid = tagged_stock_count >= min_labeled and signal_days >= max(8, int(lookback_days * 0.4))
    if tagged_stock_count < min_labeled:
        status = "low_coverage"
        diagnosis = (
            f"当前只有 {tagged_stock_count} 只股票有{label_source_name}，能回测出线索，但还不足以证明全市场主线。"
            f"先把样本补到至少 {min_labeled} 只，结论会更稳。"
        )
    elif signal_days < max(8, int(lookback_days * 0.4)):
        status = "weak_signal"
        diagnosis = "过去一个月有效信号天数偏少，说明当前参数过严或标签覆盖结构不够。"
    elif (avg_forward or 0) > 0 and (hit_rate or 0) >= 55:
        status = "validated"
        diagnosis = "过去一个月标签风格信号有正向后验收益，可作为主线复盘依据。"
    else:
        status = "needs_adjustment"
        diagnosis = "当前参数选出的主线后验收益不稳定，需要继续调样本数、强势池范围或标签分组。"

    best_mainlines = result["best_mainlines"]
    if best_mainlines:
        leader = best_mainlines[0]
        mainline_text = (
            f"过去一个月最稳定线索是 {leader['style_name']}：出现 {leader['days_selected']} 天，"
            f"后续 {forward_period} 日平均 {leader['avg_forward_return_pct']:+.2f}%"
        )
        if leader.get("avg_forward_excess_pct") is not None:
            mainline_text += f"，超额 {leader['avg_forward_excess_pct']:+.2f}%"
        mainline_text += "。"
    else:
        mainline_text = "过去一个月没有形成满足样本数要求的标签主线。"

    return {
        "status": status,
        "label_source": label_source,
        "label_source_name": label_source_name,
        "method": f"按当日{momentum_period}日强势池Top{top_n}聚合{label_source_name}，至少{min_sample}只样本才计入；泛AI/半导体只作方向词，不单独生成参与信号；分数结合当日强度、扩散样本和延续热度，后续{forward_period}日只用于验证。",
        "diagnosis": diagnosis,
        "mainline_text": mainline_text,
        "is_statistically_valid": is_valid,
        "lookback_days": lookback_days,
        "momentum_period": momentum_period,
        "forward_period": forward_period,
        "top_n": top_n,
        "min_sample": min_sample,
        "tested_days": len(date_candidates),
        "signal_days": signal_days,
        "tagged_stock_count": tagged_stock_count,
        "tag_link_count": tag_link_count,
        "avg_forward_return_pct": avg_forward,
        "avg_forward_excess_pct": result["avg_forward_excess_pct"],
        "hit_rate_pct": hit_rate,
        "best_match_rate_pct": result["best_match_rate_pct"],
        "best_mainlines": best_mainlines,
        "recent_signals": result["recent_signals"],
        "parameter_sweep": sweep,
    }


def factor_backtest_empty(status: str, diagnosis: str, label_source: str = "factor") -> dict[str, Any]:
    return {
        "status": status,
        "label_source": label_source,
        "label_source_name": "网络标签" if label_source == "network" else "因子库标签",
        "method": "按标签风格回测赚钱效应。",
        "diagnosis": diagnosis,
        "mainline_text": diagnosis,
        "is_statistically_valid": False,
        "lookback_days": 0,
        "momentum_period": 3,
        "forward_period": 5,
        "top_n": 80,
        "min_sample": 4,
        "tested_days": 0,
        "signal_days": 0,
        "tagged_stock_count": 0,
        "tag_link_count": 0,
        "avg_forward_return_pct": None,
        "avg_forward_excess_pct": None,
        "hit_rate_pct": None,
        "best_match_rate_pct": None,
        "best_mainlines": [],
        "recent_signals": [],
        "parameter_sweep": [],
    }


def factor_backtest_bars_table(db: Session) -> str | None:
    rows = db.execute(
        text("select name from sqlite_master where type='table' and name in ('market_style_daily_bars','stock_daily_bars')")
    ).scalars().all()
    names = set(rows)
    if "market_style_daily_bars" in names:
        count = db.execute(text("select count(*) from market_style_daily_bars")).scalar_one()
        if count:
            return "market_style_daily_bars"
    if "stock_daily_bars" in names:
        count = db.execute(text("select count(*) from stock_daily_bars")).scalar_one()
        if count:
            return "stock_daily_bars"
    return None


def factor_backtest_labels(db: Session, label_source: str) -> tuple[dict[str, list[dict[str, Any]]], int, int, str]:
    if label_source == "network":
        labels = factor_backtest_network_labels(db)
        return labels, len(labels), sum(len(items) for items in labels.values()), "网络标签"
    labels, stock_count, link_count = factor_backtest_theme_labels(db)
    return labels, stock_count, link_count, "因子库标签"


def factor_backtest_network_labels(db: Session) -> dict[str, list[dict[str, Any]]]:
    labels: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    tables = set(db.execute(text("select name from sqlite_master where type='table'")).scalars().all())

    if "market_style_ths_members" in tables:
        rows = db.execute(
            text(
                """
                select group_type, group_name, full_code
                from market_style_ths_members
                where group_name is not null and full_code is not null
                """
            )
        ).mappings()
        for row in rows:
            group_type = str(row["group_type"] or "")
            label_name = str(row["group_name"] or "").strip()
            full_code = str(row["full_code"] or "").strip()
            if not label_name or not full_code:
                continue
            category = "同花顺行业" if group_type == "industry" else "同花顺概念" if group_type == "concept" else "同花顺标签"
            key = (full_code, category, label_name)
            if key in seen:
                continue
            seen.add(key)
            labels.setdefault(full_code, []).append(
                {
                    "style_name": label_name,
                    "category": category,
                    "tag_id": None,
                    "tag_name": label_name,
                    "color": None,
                }
            )

    if {"sector_indices", "sector_index_members"}.issubset(tables):
        rows = db.execute(
            text(
                """
                select s.name as sector_name, m.full_code
                from sector_index_members m
                join sector_indices s on s.id = m.sector_id
                where s.active = 1 and m.full_code is not null
                """
            )
        ).mappings()
        for row in rows:
            label_name = str(row["sector_name"] or "").strip()
            full_code = str(row["full_code"] or "").strip()
            if not label_name or not full_code:
                continue
            key = (full_code, "自定义板块", label_name)
            if key in seen:
                continue
            seen.add(key)
            labels.setdefault(full_code, []).append(
                {
                    "style_name": label_name,
                    "category": "自定义板块",
                    "tag_id": None,
                    "tag_name": label_name,
                    "color": None,
                }
            )
    return labels


def factor_backtest_theme_labels(db: Session) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    rows = db.execute(
        select(FactorStockTag.full_code, FactorTag.id, FactorTag.name, FactorTag.color)
        .join(FactorTag, FactorTag.id == FactorStockTag.tag_id)
    ).all()
    labels: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for full_code, tag_id, tag_name, color in rows:
        name = str(tag_name or "").strip()
        if not name or name in FACTOR_BACKTEST_CONTEXT_TAGS:
            continue
        key = (str(full_code), name)
        if key in seen:
            continue
        seen.add(key)
        themes = factor_tag_style_themes(name)
        category = themes[0]["category"] if themes and themes[0]["name"] != "其他风格" else "因子库标签"
        if category == "规模风格":
            continue
        labels.setdefault(str(full_code), []).append(
            {
                "style_name": name,
                "category": category,
                "tag_id": tag_id,
                "tag_name": name,
                "color": color,
            }
        )
    return labels, len(labels), sum(len(items) for items in labels.values())


def factor_tag_style_themes(tag_name: str) -> list[dict[str, str]]:
    matches = [
        {"name": theme["name"], "category": theme["category"]}
        for theme in STYLE_THEME_CATALOG
        if text_matches_theme(tag_name, theme["keywords"])
    ]
    return matches or [{"name": "其他风格", "category": "未分类"}]


def factor_parse_trade_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def factor_backtest_trade_dates(db: Session, table: str) -> list[date]:
    rows = db.execute(text(f"select distinct trade_date from {table} order by trade_date")).scalars().all()
    return [factor_parse_trade_date(row) for row in rows if row]


def factor_backtest_histories(db: Session, table: str, start_date: date) -> dict[str, list[dict[str, Any]]]:
    rows = db.execute(
        text(
            f"""
            select full_code, code, name, trade_date, close, change_pct
            from {table}
            where trade_date >= :start_date
            order by full_code, trade_date
            """
        ),
        {"start_date": start_date},
    ).mappings()
    histories: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        histories.setdefault(str(row["full_code"]), []).append(
            {
                "full_code": str(row["full_code"]),
                "code": str(row["code"]),
                "name": str(row["name"]),
                "trade_date": factor_parse_trade_date(row["trade_date"]),
                "close": float(row["close"] or 0),
                "change_pct": float(row["change_pct"] or 0),
            }
        )
    return histories


def factor_return_at(bars: list[dict[str, Any]], index: int, days: int, forward: bool = False) -> float | None:
    other = index + days if forward else index - days
    if other < 0 or other >= len(bars):
        return None
    base = bars[index]["close"] if forward else bars[other]["close"]
    end = bars[other]["close"] if forward else bars[index]["close"]
    if not base:
        return None
    return (end / base - 1) * 100


def factor_backtest_date_candidates(
    histories: dict[str, list[dict[str, Any]]],
    eval_dates: list[date],
    momentum_period: int,
    forward_period: int,
    labels: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    index_map = {
        full_code: {bar["trade_date"]: index for index, bar in enumerate(bars)}
        for full_code, bars in histories.items()
    }
    results: list[dict[str, Any]] = []
    for trade_date in eval_dates:
        candidates: list[dict[str, Any]] = []
        market_forwards: list[float] = []
        for full_code, bars in histories.items():
            index = index_map.get(full_code, {}).get(trade_date)
            if index is None:
                continue
            momentum_return = factor_return_at(bars, index, momentum_period)
            forward_return = factor_return_at(bars, index, forward_period, forward=True)
            if momentum_return is None or forward_return is None:
                continue
            market_forwards.append(forward_return)
            candidates.append(
                {
                    "full_code": full_code,
                    "code": bars[index]["code"],
                    "name": bars[index]["name"],
                    "momentum_return_pct": momentum_return,
                    "forward_return_pct": forward_return,
                    "labels": labels.get(full_code, []),
                }
            )
        candidates.sort(key=lambda item: item["momentum_return_pct"], reverse=True)
        market_forward = sum(market_forwards) / len(market_forwards) if market_forwards else None
        results.append({"trade_date": trade_date, "candidates": candidates, "market_forward_pct": market_forward})
    return results


def factor_run_backtest(date_candidates: list[dict[str, Any]], top_n: int, min_sample: int) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    label_heat: dict[str, float] = {}
    for day in date_candidates:
        buckets: dict[str, dict[str, Any]] = {}
        for candidate in day["candidates"][:top_n]:
            for label in candidate["labels"]:
                bucket = buckets.setdefault(
                    label["style_name"],
                    {
                        "style_name": label["style_name"],
                        "category": label["category"],
                        "stocks": {},
                        "source_tags": set(),
                    },
                )
                bucket["stocks"][candidate["full_code"]] = candidate
                bucket["source_tags"].add(label["tag_name"])
        scored = []
        for bucket in buckets.values():
            stocks = list(bucket["stocks"].values())
            sample_count = len(stocks)
            if sample_count < min_sample:
                continue
            source_tags = sorted(bucket["source_tags"])
            avg_momentum = sum(float(item["momentum_return_pct"]) for item in stocks) / sample_count
            avg_forward = sum(float(item["forward_return_pct"]) for item in stocks) / sample_count
            persistence_heat = label_heat.get(bucket["style_name"], 0)
            score = avg_momentum * 0.25 + min(sample_count, 15) * 2.5 + persistence_heat * 0.65 + min(len(source_tags), 5) * 1.5
            role = factor_tag_trade_role(bucket["style_name"], 1, sample_count)
            scored.append(
                {
                    "style_name": bucket["style_name"],
                    "category": bucket["category"],
                    "score": round(score, 4),
                    "sample_count": sample_count,
                    "tag_count": len(source_tags),
                    "source_tags": source_tags[:8],
                    "avg_momentum_return_pct": round(avg_momentum, 4),
                    "avg_forward_return_pct": round(avg_forward, 4),
                    "avg_forward_excess_pct": round(avg_forward - day["market_forward_pct"], 4) if day["market_forward_pct"] is not None else None,
                    "evidence_days": 1,
                    "evidence_level": factor_tag_evidence_level(1, sample_count, role),
                    "is_generic_direction": role == "direction_only" and bucket["style_name"] in GENERIC_DIRECTION_TAGS,
                    "trade_role": role,
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        if not scored:
            for name in list(label_heat):
                label_heat[name] *= 0.55
            continue
        selected = scored[0]
        if selected.get("trade_role") == "direction_only":
            for name in list(label_heat):
                label_heat[name] *= 0.55
            continue
        best_future = max(scored, key=lambda item: item["avg_forward_return_pct"])
        signals.append(
            {
                "trade_date": day["trade_date"].isoformat(),
                "selected": selected,
                "best_future_style": best_future["style_name"],
                "best_future_return_pct": best_future["avg_forward_return_pct"],
            }
        )
        for name in list(label_heat):
            label_heat[name] *= 0.55
        for rank, item in enumerate(sorted(scored, key=lambda row: row["avg_momentum_return_pct"], reverse=True)[:10], start=1):
            label_heat[item["style_name"]] = label_heat.get(item["style_name"], 0) + max(0, 11 - rank) + min(item["sample_count"], 12) * 0.5 + max(item["avg_momentum_return_pct"], 0) * 0.1

    if not signals:
        return {
            "signal_days": 0,
            "avg_forward_return_pct": None,
            "avg_forward_excess_pct": None,
            "hit_rate_pct": None,
            "best_match_rate_pct": None,
            "best_mainlines": [],
            "recent_signals": [],
        }

    selected = [signal["selected"] for signal in signals]
    forward_values = [float(item["avg_forward_return_pct"]) for item in selected]
    excess_values = [float(item["avg_forward_excess_pct"]) for item in selected if item["avg_forward_excess_pct"] is not None]
    mainlines: dict[str, dict[str, Any]] = {}
    for signal in signals:
        item = signal["selected"]
        bucket = mainlines.setdefault(
            item["style_name"],
            {
                "style_name": item["style_name"],
                "category": item["category"],
                "days_selected": 0,
                "forward_values": [],
                "excess_values": [],
                "momentum_values": [],
                "sample_counts": [],
                "source_tags": set(),
                "latest_seen": signal["trade_date"],
                "trade_role": item.get("trade_role", "candidate_mainline"),
                "is_generic_direction": bool(item.get("is_generic_direction")),
            },
        )
        bucket["days_selected"] += 1
        bucket["forward_values"].append(float(item["avg_forward_return_pct"]))
        if item["avg_forward_excess_pct"] is not None:
            bucket["excess_values"].append(float(item["avg_forward_excess_pct"]))
        bucket["momentum_values"].append(float(item["avg_momentum_return_pct"]))
        bucket["sample_counts"].append(int(item["sample_count"]))
        bucket["source_tags"].update(item["source_tags"])
        bucket["latest_seen"] = signal["trade_date"]

    best_mainlines = []
    for bucket in mainlines.values():
        values = bucket["forward_values"]
        excess = bucket["excess_values"]
        best_mainlines.append(
            enrich_factor_tag_signal({
                "style_name": bucket["style_name"],
                "tag_name": bucket["style_name"],
                "category": bucket["category"],
                "days_selected": bucket["days_selected"],
                "avg_forward_return_pct": round(sum(values) / len(values), 4),
                "avg_forward_excess_pct": round(sum(excess) / len(excess), 4) if excess else None,
                "win_rate_pct": round(sum(1 for value in values if value > 0) / len(values) * 100, 2),
                "avg_momentum_return_pct": round(sum(bucket["momentum_values"]) / len(bucket["momentum_values"]), 4),
                "avg_sample_count": round(sum(bucket["sample_counts"]) / len(bucket["sample_counts"]), 2),
                "stock_count": round(sum(bucket["sample_counts"]) / len(bucket["sample_counts"]), 2),
                "source_tags": sorted(bucket["source_tags"])[:10],
                "latest_seen": bucket["latest_seen"],
                "periods": list(range(1, int(bucket["days_selected"]) + 1)),
                "trade_role": bucket.get("trade_role", "candidate_mainline"),
                "is_generic_direction": bucket.get("is_generic_direction", False),
            })
        )
    best_mainlines.sort(key=lambda item: (item["days_selected"], item["avg_forward_return_pct"]), reverse=True)

    return {
        "signal_days": len(signals),
        "avg_forward_return_pct": round(sum(forward_values) / len(forward_values), 4),
        "avg_forward_excess_pct": round(sum(excess_values) / len(excess_values), 4) if excess_values else None,
        "hit_rate_pct": round(sum(1 for value in forward_values if value > 0) / len(forward_values) * 100, 2),
        "best_match_rate_pct": round(
            sum(1 for signal in signals if signal["selected"]["style_name"] == signal["best_future_style"]) / len(signals) * 100,
            2,
        ),
        "best_mainlines": best_mainlines[:8],
        "recent_signals": signals[-12:],
    }


EFFECT_LOG_HORIZONS = [1, 3, 5, 10, 20]
EFFECT_LOGIC_VERSION = "blank_rules_v1"
EFFECT_MAINLINE_PLAYBOOKS: list[dict[str, Any]] = []


def effect_log_json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def effect_log_json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=effect_log_json_default)


def effect_log_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def effect_text_has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def effect_log_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def effect_log_stock_tag_rows(overview: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        full_code = str(candidate.get("full_code") or "")
        name = str(candidate.get("name") or full_code)
        for tag in candidate.get("tags") or []:
            if not tag:
                continue
            key = (full_code, str(tag))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"full_code": full_code, "name": name, "tag": str(tag), "return_pct": candidate.get("return_pct")})
    for ranking in overview.get("stock_rankings") or []:
        period = int(ranking.get("period") or 0)
        if period not in {1, 3, 5}:
            continue
        for stock in (ranking.get("gainers") or [])[:35]:
            full_code = str(stock.get("full_code") or "")
            name = str(stock.get("name") or full_code)
            for tag in stock.get("tags") or []:
                tag_name = str(tag.get("name") or "")
                if not tag_name:
                    continue
                key = (full_code, tag_name)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"full_code": full_code, "name": name, "tag": tag_name, "return_pct": stock.get("return_pct"), "period": period})
    return rows


def effect_log_recent_text_evidence(db: Session, keywords: list[str], trade_date: date, limit: int = 8) -> list[dict[str, Any]]:
    cutoff_date = trade_date - timedelta(days=10)
    cutoff_dt = datetime.combine(cutoff_date, time.min, tzinfo=BEIJING).astimezone(timezone.utc)
    evidence: list[dict[str, Any]] = []

    news_rows = list(db.scalars(select(EditedNewsItem).order_by(desc(EditedNewsItem.published_at), desc(EditedNewsItem.crawled_at)).limit(120)))
    for row in news_rows:
        row_time = row.published_at or row.crawled_at
        if row_time and effect_log_utc_datetime(row_time) and effect_log_utc_datetime(row_time) < cutoff_dt:
            continue
        text_value = f"{row.title} {row.summary} {row.impact_path}"
        if effect_text_has_any(text_value, keywords):
            evidence.append({"source": row.source_name, "title": row.title, "time": row_time.isoformat() if row_time else None})
            if len(evidence) >= limit:
                return evidence

    material_rows = list(
        db.scalars(
            select(MarketReviewMaterial)
            .where(MarketReviewMaterial.report_date >= cutoff_date)
            .order_by(desc(MarketReviewMaterial.report_date), desc(MarketReviewMaterial.created_at))
            .limit(120)
        )
    )
    for row in material_rows:
        text_value = f"{row.title} {row.content} {row.theme}"
        if effect_text_has_any(text_value, keywords):
            evidence.append({"source": row.source_name, "title": row.title, "time": row.report_date.isoformat()})
            if len(evidence) >= limit:
                return evidence

    xueqiu_rows = list(db.scalars(select(XueqiuPost).order_by(desc(XueqiuPost.published_at), desc(XueqiuPost.crawled_at)).limit(180)))
    for row in xueqiu_rows:
        row_time = row.published_at or row.crawled_at
        if row_time and effect_log_utc_datetime(row_time) and effect_log_utc_datetime(row_time) < cutoff_dt:
            continue
        if effect_text_has_any(row.content, keywords):
            evidence.append({"source": row.author_name, "title": row.content[:80], "time": row_time.isoformat() if row_time else None})
            if len(evidence) >= limit:
                return evidence

    ann_rows = list(db.scalars(select(WatchlistAnnouncementItem).order_by(desc(WatchlistAnnouncementItem.published_at), desc(WatchlistAnnouncementItem.crawled_at)).limit(120)))
    for row in ann_rows:
        row_time = row.published_at or row.crawled_at
        if row_time and effect_log_utc_datetime(row_time) and effect_log_utc_datetime(row_time) < cutoff_dt:
            continue
        text_value = f"{row.title} {row.summary}"
        if effect_text_has_any(text_value, keywords):
            evidence.append({"source": row.source_name, "title": row.title, "time": row_time.isoformat() if row_time else None})
            if len(evidence) >= limit:
                return evidence
    return evidence


def effect_log_match_playbook(
    db: Session,
    overview: dict[str, Any],
    effect_style: dict[str, Any],
    candidates: list[dict[str, Any]],
    trade_date: date,
) -> dict[str, Any] | None:
    stock_tag_rows = effect_log_stock_tag_rows(overview, candidates)
    tag_names = {row["tag"] for row in stock_tag_rows}
    mainline_names = [
        str(row.get("tag_name") or "")
        for key in ("confirmed_mainlines", "tradable_mainlines", "dominant_tags", "board_dominant_tags")
        for row in effect_style.get(key) or []
        if row.get("tag_name")
    ]
    tag_text = " ".join([*tag_names, *mainline_names])
    best: dict[str, Any] | None = None
    for playbook in EFFECT_MAINLINE_PLAYBOOKS:
        phenomenon_hits = sorted({tag for tag in tag_names if effect_text_has_any(tag, playbook["phenomenon_keywords"])})
        chain_hits = sorted({tag for tag in tag_names if effect_text_has_any(tag, playbook["chain_keywords"])})
        catalyst_hits = sorted({keyword for keyword in playbook["catalyst_keywords"] if keyword in tag_text})
        text_evidence = effect_log_recent_text_evidence(db, playbook["catalyst_keywords"], trade_date)
        score = len(phenomenon_hits) * 2 + len(chain_hits) + len(catalyst_hits) + min(len(text_evidence), 3)
        if not phenomenon_hits and catalyst_hits:
            score += 1
        if score < 4 or not chain_hits:
            continue
        co_movement = []
        for row in stock_tag_rows:
            if effect_text_has_any(row["tag"], playbook["phenomenon_keywords"] + playbook["chain_keywords"]):
                item = {"name": row["name"], "full_code": row["full_code"], "tag": row["tag"], "return_pct": row.get("return_pct")}
                if item not in co_movement:
                    co_movement.append(item)
            if len(co_movement) >= 10:
                break
        reasoning = {
            "key": playbook["key"],
            "title": playbook["title"],
            "confidence": min(95, 45 + score * 5),
            "phenomenon": f"强势股标签集中在 {', '.join((phenomenon_hits + chain_hits)[:8])}，不是泛半导体平均上涨。",
            "catalysts": playbook["catalysts"],
            "transmission": playbook["transmission"],
            "co_movement": co_movement,
            "text_evidence": text_evidence,
            "summary": f"当前主线更精确看作“{playbook['title']}”。盘面从具体存储/扩产线索扩散到 {', '.join(chain_hits[:5])}。",
            "action": playbook["action"],
        }
        if not best or reasoning["confidence"] > best["confidence"]:
            best = reasoning
    return best


def effect_log_trade_date(db: Session, effect_style: dict[str, Any] | None) -> date:
    return datetime.now(BEIJING).date()


def effect_log_mainline_rows(effect_style: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not effect_style:
        return []
    confirmed = list(effect_style.get("confirmed_mainlines") or [])
    tradable = list(effect_style.get("tradable_mainlines") or [])
    rows = confirmed or tradable
    return [row for row in rows if row.get("trade_role") != "direction_only" and not row.get("is_generic_direction")][:6]


def effect_log_tag_names(rows: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for row in rows:
        for name in [row.get("tag_name"), *(row.get("source_tags") or [])]:
            if name:
                names.add(str(name))
    return names


def latest_stock_close_on_or_before(db: Session, full_code: str, trade_date: date) -> StockDailyBar | None:
    return db.scalar(
        select(StockDailyBar)
        .where(StockDailyBar.full_code == full_code, StockDailyBar.trade_date <= trade_date)
        .order_by(desc(StockDailyBar.trade_date))
        .limit(1)
    )


def build_effect_log_candidates(db: Session, overview: dict[str, Any], trade_date: date, mainlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mainline_names = effect_log_tag_names(mainlines)
    if not mainline_names:
        return []
    rankings = overview.get("stock_rankings") or []
    source_rankings = [
        ranking
        for period in (3, 5, 10)
        for ranking in rankings
        if int(ranking.get("period") or 0) == period
    ]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ranking in source_rankings:
        period = int(ranking.get("period") or 0)
        for stock in ranking.get("gainers") or []:
            full_code = stock.get("full_code")
            if not full_code or full_code in seen:
                continue
            tag_names = {tag.get("name") for tag in stock.get("tags") or [] if tag.get("name")}
            matched = sorted(tag_names & mainline_names)
            if not matched:
                continue
            entry_bar = latest_stock_close_on_or_before(db, str(full_code), trade_date)
            entry_price = entry_bar.close if entry_bar else stock.get("latest_price")
            candidates.append(
                {
                    "full_code": full_code,
                    "code": stock.get("code"),
                    "name": stock.get("name"),
                    "exchange": stock.get("exchange"),
                    "entry_date": entry_bar.trade_date.isoformat() if entry_bar else trade_date.isoformat(),
                    "entry_price": entry_price,
                    "return_pct": stock.get("return_pct"),
                    "change_pct": stock.get("change_pct"),
                    "matched_mainlines": matched,
                    "tags": sorted(tag_names),
                    "evidence": [
                        f"{period}日强势榜",
                        f"命中主线：{'/'.join(matched[:3])}",
                    ],
                    "reason": "主线内短期强势，作为影子候选跟踪。",
                }
            )
            seen.add(str(full_code))
            if len(candidates) >= 10:
                return candidates
    return candidates


def build_effect_log_filtered(effect_style: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not effect_style:
        return []
    rows: list[dict[str, Any]] = []
    for row in effect_style.get("direction_only_tags") or []:
        rows.append(
            {
                "name": row.get("tag_name"),
                "type": "direction_only",
                "reason": "泛方向词只作为背景，不单独触发候选。",
                "evidence_days": row.get("evidence_days"),
            }
        )
    for row in effect_style.get("downgraded_tags") or []:
        rows.append(
            {
                "name": row.get("tag_name"),
                "type": "downgraded",
                "reason": "证据或扩散不足，先降级观察。",
                "evidence_days": row.get("evidence_days"),
            }
        )
    for flag in effect_style.get("risk_flags") or []:
        rows.append({"name": flag, "type": "risk_flag", "reason": "当日风险提示。"})
    return rows[:12]


def update_effect_log_performance(db: Session, log: FactorEffectDailyLog) -> dict[str, Any]:
    candidates = parse_json_list(log.candidates_json)
    if not candidates:
        performance = {"status": "no_candidates", "horizons": {}, "items": []}
        log.performance_json = effect_log_json_dump(performance)
        return performance
    items: list[dict[str, Any]] = []
    horizon_values: dict[str, list[float]] = {str(day): [] for day in EFFECT_LOG_HORIZONS}
    for candidate in candidates:
        full_code = candidate.get("full_code")
        entry_price = candidate.get("entry_price")
        if not full_code or not isinstance(entry_price, (int, float)) or entry_price <= 0:
            items.append({"full_code": full_code, "name": candidate.get("name"), "status": "missing_entry"})
            continue
        bars = list(
            db.scalars(
                select(StockDailyBar)
                .where(StockDailyBar.full_code == full_code, StockDailyBar.trade_date > log.trade_date)
                .order_by(StockDailyBar.trade_date)
                .limit(max(EFFECT_LOG_HORIZONS))
            )
        )
        returns: dict[str, float | None] = {}
        for day in EFFECT_LOG_HORIZONS:
            value = None
            if len(bars) >= day:
                value = round((bars[day - 1].close - float(entry_price)) / float(entry_price) * 100, 4)
                horizon_values[str(day)].append(value)
            returns[f"t{day}"] = value
        max_high = max((bar.high for bar in bars), default=None)
        min_low = min((bar.low for bar in bars), default=None)
        items.append(
            {
                "full_code": full_code,
                "name": candidate.get("name"),
                "entry_price": entry_price,
                "available_days": len(bars),
                "returns": returns,
                "max_return_pct": round((max_high - float(entry_price)) / float(entry_price) * 100, 4) if max_high else None,
                "max_drawdown_pct": round((min_low - float(entry_price)) / float(entry_price) * 100, 4) if min_low else None,
            }
        )
    horizons = {
        key: {
            "avg_return_pct": round(sum(values) / len(values), 4) if values else None,
            "win_rate_pct": round(sum(1 for value in values if value > 0) / len(values) * 100, 2) if values else None,
            "sample_count": len(values),
        }
        for key, values in horizon_values.items()
    }
    performance = {
        "status": "tracking",
        "horizons": horizons,
        "items": items,
        "updated_at": now_utc().isoformat(),
    }
    log.performance_json = effect_log_json_dump(performance)
    return performance


def effect_log_to_out(db: Session, log: FactorEffectDailyLog, refresh_performance: bool = True) -> dict[str, Any]:
    performance = effect_log_json_dict(log.performance_json)
    if refresh_performance:
        performance = update_effect_log_performance(db, log)
    return {
        "id": log.id,
        "trade_date": log.trade_date,
        "status": log.status,
        "source_status": log.source_status,
        "phase_key": log.phase_key,
        "phase_label": log.phase_label,
        "action": log.action,
        "summary": log.summary,
        "signal": effect_log_json_dict(log.signal_json),
        "candidates": parse_json_list(log.candidates_json),
        "filtered": parse_json_list(log.filtered_json),
        "performance": performance,
        "created_at": log.created_at,
        "updated_at": log.updated_at,
    }


def list_factor_effect_logs(db: Session, limit: int = 30) -> dict[str, Any]:
    rows = list(db.scalars(select(FactorEffectDailyLog).order_by(desc(FactorEffectDailyLog.trade_date)).limit(max(1, min(120, limit)))))
    logs = [effect_log_to_out(db, row) for row in rows]
    if rows:
        db.commit()
    return {
        "status": "ok" if logs else "not_configured",
        "source_status": "ok" if logs else "not_configured",
        "updated_at": max((row["updated_at"] for row in logs), default=None),
        "logs": logs,
        "message": f"已生成 {len(logs)} 条赚钱效应影子日志。" if logs else "还没有生成赚钱效应影子日志。",
    }


def build_factor_effect_log_payload(db: Session, overview: dict[str, Any], effect_style: dict[str, Any], trade_date: date) -> dict[str, Any]:
    phase = effect_style.get("current_phase") or {}
    mainlines = effect_log_mainline_rows(effect_style)
    candidates = build_effect_log_candidates(db, overview, trade_date, mainlines)
    filtered = build_effect_log_filtered(effect_style)
    reasoning = effect_log_match_playbook(db, overview, effect_style, candidates, trade_date)
    summary = reasoning.get("summary") if reasoning else effect_style.get("summary")
    action = reasoning.get("action") if reasoning else (effect_style.get("action") or effect_style.get("participation_advice"))
    signal = {
        "logic_version": EFFECT_LOGIC_VERSION,
        "style_name": effect_style.get("style_name"),
        "health": effect_style.get("health"),
        "confidence": effect_style.get("confidence"),
        "coverage_rate": effect_style.get("coverage_rate"),
        "mainlines": mainlines,
        "mainline_reasoning": reasoning,
        "market": effect_style.get("market") or {},
        "reasons": effect_style.get("reasons") or [],
        "machine_summary": effect_style.get("summary"),
    }
    return {
        "phase": phase,
        "mainlines": mainlines,
        "candidates": candidates,
        "filtered": filtered,
        "signal": signal,
        "summary": summary,
        "action": action,
        "status": "ok" if candidates else "manual_only",
    }


def generate_factor_effect_log(db: Session) -> dict[str, Any]:
    overview = factor_overview(db)
    effect_style = overview.get("effect_style") or {}
    trade_date = effect_log_trade_date(db, effect_style)
    payload = build_factor_effect_log_payload(db, overview, effect_style, trade_date)
    existing = db.scalar(select(FactorEffectDailyLog).where(FactorEffectDailyLog.trade_date == trade_date).limit(1))
    if existing:
        existing_signal = effect_log_json_dict(existing.signal_json)
        if existing_signal.get("logic_version") != EFFECT_LOGIC_VERSION:
            if existing_signal.get("human_mainline"):
                payload["signal"]["human_mainline"] = existing_signal.get("human_mainline")
            existing.signal_json = effect_log_json_dump(payload["signal"])
            existing.candidates_json = effect_log_json_dump(payload["candidates"])
            existing.filtered_json = effect_log_json_dump(payload["filtered"])
            existing.status = payload["status"]
            existing.source_status = str(overview.get("source_status") or overview.get("status") or "manual_only")
            existing.phase_key = payload["phase"].get("key")
            existing.phase_label = payload["phase"].get("label")
            if not existing_signal.get("human_mainline"):
                existing.summary = payload["summary"]
                existing.action = payload["action"]
        update_effect_log_performance(db, existing)
        db.commit()
        return effect_log_to_out(db, existing, refresh_performance=False)

    log = FactorEffectDailyLog(
        trade_date=trade_date,
        status=payload["status"],
        source_status=str(overview.get("source_status") or overview.get("status") or "manual_only"),
        phase_key=payload["phase"].get("key"),
        phase_label=payload["phase"].get("label"),
        action=payload["action"],
        summary=payload["summary"],
        signal_json=effect_log_json_dump(payload["signal"]),
        candidates_json=effect_log_json_dump(payload["candidates"]),
        filtered_json=effect_log_json_dump(payload["filtered"]),
    )
    db.add(log)
    db.flush()
    update_effect_log_performance(db, log)
    db.commit()
    db.refresh(log)
    return effect_log_to_out(db, log, refresh_performance=False)


def factor_overview(db: Session) -> dict[str, Any]:
    ensure_factor_defaults(db)
    preset = active_preset(db)
    presets = list_factor_presets(db)
    tags = list_factor_tags(db)
    periods = sorted({*parse_periods(preset.periods_json), *MARKET_EFFECT_PERIODS})
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    snapshots_query = select(FactorQuoteSnapshot).order_by(desc(FactorQuoteSnapshot.updated_at))
    if excluded_codes:
        snapshots_query = snapshots_query.where(~FactorQuoteSnapshot.full_code.in_(excluded_codes))
    snapshots = list(db.scalars(snapshots_query).all())
    latest_run = db.scalar(select(FactorRefreshRun).order_by(desc(FactorRefreshRun.started_at)).limit(1))
    updated_at = max((snapshot.updated_at for snapshot in snapshots), default=None)
    tag_map = tags_for_full_codes(db, [snapshot.full_code for snapshot in snapshots])
    review_map = review_statuses_for_full_codes(db, [snapshot.full_code for snapshot in snapshots], tag_map)
    watchlist_map = watchlist_statuses_for_full_codes(db, [snapshot.full_code for snapshot in snapshots])
    stock_rankings = [build_period_ranking(period, snapshots, tag_map, review_map, watchlist_map) for period in periods]
    effect_style = build_factor_effect_style(db, stock_rankings, snapshots, tag_map)
    review_stocks = build_review_stocks(db, stock_rankings, tag_map)
    max_coverage = max((ranking["coverage_count"] for ranking in stock_rankings), default=0)
    source_status = latest_run.status if latest_run else "not_configured"
    if snapshots and source_status == "error":
        status = "partial_error"
    elif snapshots:
        status = "ok" if source_status in {"ok", "partial_error"} else "manual_only"
    else:
        status = "not_configured"
    if not snapshots:
        message = "还没有行情缓存，后台定时刷新成功后会显示强弱榜。"
    elif source_status in {"partial_error", "error"}:
        message = latest_run.message or "行情源当前不可用，已展示最近缓存。"
    elif max_coverage == 0:
        message = f"已建立 {len(snapshots)} 只 A股缓存，等待后台补齐涨幅数据。"
    else:
        message = f"已缓存 {len(snapshots)} 只 A股行情，当前最多覆盖 {max_coverage} 只，按 {preset.name} 周期方案展示。"
    return {
        "status": status,
        "source_status": source_status,
        "updated_at": updated_at,
        "active_preset": preset_to_out(preset),
        "presets": presets,
        "tags": tags,
        "stock_rankings": stock_rankings,
        "tag_rankings": stock_rankings,
        "effect_style": effect_style,
        "review_stocks": review_stocks,
        "latest_run": refresh_run_to_out(latest_run),
        "message": message,
    }


def factor_hot_tag_review(db: Session, gain_limit: int = 200, amount_limit: int = 200) -> dict[str, Any]:
    gain_limit = max(20, min(500, int(gain_limit or 200)))
    amount_limit = max(20, min(500, int(amount_limit or 200)))
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    snapshots_query = select(FactorQuoteSnapshot).order_by(desc(FactorQuoteSnapshot.updated_at))
    if excluded_codes:
        snapshots_query = snapshots_query.where(~FactorQuoteSnapshot.full_code.in_(excluded_codes))
    snapshots = list(db.scalars(snapshots_query).all())
    latest_run = db.scalar(select(FactorRefreshRun).order_by(desc(FactorRefreshRun.started_at)).limit(1))
    updated_at = max((snapshot.updated_at for snapshot in snapshots), default=None)
    tag_map = tags_for_full_codes(db, [snapshot.full_code for snapshot in snapshots])
    review_map = review_statuses_for_full_codes(db, [snapshot.full_code for snapshot in snapshots], tag_map)
    watchlist_map = watchlist_statuses_for_full_codes(db, [snapshot.full_code for snapshot in snapshots])
    amount_map = latest_amounts_for_full_codes(db, [snapshot.full_code for snapshot in snapshots])

    def one_day_return(snapshot: FactorQuoteSnapshot) -> float | None:
        value = parse_returns(snapshot.returns_json).get("1")
        if value is not None:
            return value
        return snapshot.change_pct

    gain_rows = [snapshot for snapshot in snapshots if one_day_return(snapshot) is not None]
    gain_rows.sort(key=lambda snapshot: one_day_return(snapshot) or 0, reverse=True)
    amount_rows = sorted(snapshots, key=lambda snapshot: amount_map.get(snapshot.full_code) or 0, reverse=True)
    if not any(amount_map.get(snapshot.full_code) for snapshot in snapshots):
        amount_rows = sorted(snapshots, key=lambda snapshot: abs(one_day_return(snapshot) or snapshot.change_pct or 0), reverse=True)
    gain_top_rows = gain_rows[:gain_limit]
    amount_top_rows = amount_rows[:amount_limit]
    gain_codes = {snapshot.full_code for snapshot in gain_top_rows}
    amount_codes = {snapshot.full_code for snapshot in amount_top_rows}

    selected: dict[str, dict[str, Any]] = {}
    for index, snapshot in enumerate(gain_top_rows, start=1):
        row = selected.setdefault(snapshot.full_code, {"snapshot": snapshot, "reasons": []})
        row["reasons"].append(f"涨幅前{gain_limit} #{index}")
    for index, snapshot in enumerate(amount_top_rows, start=1):
        row = selected.setdefault(snapshot.full_code, {"snapshot": snapshot, "reasons": []})
        row["reasons"].append(f"成交额前{amount_limit} #{index}")

    stocks: list[dict[str, Any]] = []
    tag_buckets: dict[int, dict[str, Any]] = {}
    tagged_sample_codes: set[str] = set()
    for payload in selected.values():
        snapshot = payload["snapshot"]
        return_pct = one_day_return(snapshot) or 0
        tags = tag_map.get(snapshot.full_code, [])
        if tags:
            tagged_sample_codes.add(snapshot.full_code)
        sources: list[str] = []
        if snapshot.full_code in gain_codes:
            sources.append(f"涨幅Top{gain_limit}")
        if snapshot.full_code in amount_codes:
            sources.append(f"成交额Top{amount_limit}")
        for tag in tags:
            bucket = tag_buckets.setdefault(
                tag.id,
                {
                    "tag_id": tag.id,
                    "tag_name": tag.name,
                    "total_codes": set(),
                    "gain_codes": set(),
                    "amount_codes": set(),
                    "overlap_codes": set(),
                    "sample_stocks": [],
                },
            )
            bucket["total_codes"].add(snapshot.full_code)
            if snapshot.full_code in gain_codes:
                bucket["gain_codes"].add(snapshot.full_code)
            if snapshot.full_code in amount_codes:
                bucket["amount_codes"].add(snapshot.full_code)
            if snapshot.full_code in gain_codes and snapshot.full_code in amount_codes:
                bucket["overlap_codes"].add(snapshot.full_code)
            if len(bucket["sample_stocks"]) < 5:
                bucket["sample_stocks"].append(
                    {
                        "full_code": snapshot.full_code,
                        "code": snapshot.code,
                        "name": snapshot.name,
                        "change_pct": snapshot.change_pct,
                        "return_pct": round(return_pct, 4),
                        "amount": amount_map.get(snapshot.full_code),
                        "sources": sources,
                    }
                )
        out = ranking_stock_to_out(snapshot, return_pct, tags, review_map.get(snapshot.full_code, "unreviewed"), watchlist_map.get(snapshot.full_code, {}))
        suggested = [tag.name for tag in tags]
        if not suggested:
            suggested = suggest_stock_tags_from_snapshot(snapshot, payload["reasons"])
        out.update(
            {
                "reasons": payload["reasons"],
                "suggested_tags": suggested,
                "amount": amount_map.get(snapshot.full_code),
            }
        )
        stocks.append(out)
    stocks.sort(key=lambda row: (row["tag_status"] == "done", not row["is_untagged"], -abs(row["return_pct"])))
    tag_stats = [
        {
            "tag_id": bucket["tag_id"],
            "tag_name": bucket["tag_name"],
            "total_count": len(bucket["total_codes"]),
            "gain_count": len(bucket["gain_codes"]),
            "amount_count": len(bucket["amount_codes"]),
            "overlap_count": len(bucket["overlap_codes"]),
            "sample_stocks": bucket["sample_stocks"],
        }
        for bucket in tag_buckets.values()
    ]
    tag_stats.sort(
        key=lambda row: (
            -int(row["total_count"]),
            -int(row["overlap_count"]),
            -int(row["amount_count"]),
            -int(row["gain_count"]),
            str(row["tag_name"]),
        )
    )
    source_status = latest_run.status if latest_run else "not_configured"
    status = "ok" if stocks else "not_configured"
    return {
        "status": status,
        "source_status": source_status,
        "updated_at": updated_at,
        "message": f"已生成涨幅前 {gain_limit} + 成交额前 {amount_limit} 合并校准清单，共 {len(stocks)} 只。",
        "gain_limit": gain_limit,
        "amount_limit": amount_limit,
        "sample_count": len(selected),
        "tagged_sample_count": len(tagged_sample_codes),
        "untagged_sample_count": len(selected) - len(tagged_sample_codes),
        "tag_stats": tag_stats,
        "stocks": stocks,
    }


def factor_auto_tag_overview(db: Session) -> dict[str, Any]:
    latest = db.scalar(select(FactorAutoTagRun).order_by(desc(FactorAutoTagRun.started_at), desc(FactorAutoTagRun.id)).limit(1))
    tagged_count = db.scalar(select(func.count(func.distinct(FactorStockTag.full_code)))) or 0
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    total_query = select(func.count(AStock.id))
    if excluded_codes:
        total_query = total_query.where(~AStock.full_code.in_(excluded_codes))
    total_count = db.scalar(total_query) or 0
    ima_cache = factor_ima_cache_status(db, total_count)
    if not latest:
        return {
            "status": "not_configured",
            "source_status": "not_configured",
            "updated_at": None,
            "latest_run": None,
            "ima_cache": ima_cache,
            "message": f"还没有运行全 A 自动打标；当前 {tagged_count}/{total_count} 只有生效标签。",
        }
    source_statuses = parse_json_list(latest.source_status_json)
    source_status = latest.status
    return {
        "status": latest.status,
        "source_status": source_status,
        "updated_at": latest.completed_at or latest.started_at,
        "latest_run": auto_tag_run_to_out(latest),
        "ima_cache": ima_cache,
        "message": latest.message or f"最近一次全 A 自动打标：{latest.tagged_count}/{latest.stock_count} 只股票已打标签。",
    }


def factor_ima_cache_status(db: Session, stock_count: int | None = None) -> dict[str, Any]:
    if stock_count is None:
        stock_count = db.scalar(select(func.count(AStock.id))) or 0
    cache_rows = db.scalar(select(func.count(FactorImaTagCache.id))) or 0
    ok_rows = db.scalar(select(func.count(FactorImaTagCache.id)).where(FactorImaTagCache.status == "ok")) or 0
    pending_rows = db.scalar(
        select(func.count(FactorImaTagCache.id)).where(
            or_(FactorImaTagCache.status.in_(["pending", "partial_error", "error"]), FactorImaTagCache.checked_at.is_(None))
        )
    ) or 0
    tagged_rows = db.scalar(
        select(func.count(FactorImaTagCache.id)).where(FactorImaTagCache.tags_json.notin_(["[]", "null", ""]))
    ) or 0
    latest_checked = db.scalar(select(func.max(FactorImaTagCache.checked_at)))
    pending_count = max(0, int(stock_count or 0) - int(cache_rows or 0)) + int(pending_rows or 0)
    return {
        "stock_count": int(stock_count or 0),
        "cache_rows": int(cache_rows or 0),
        "ok_rows": int(ok_rows or 0),
        "tagged_rows": int(tagged_rows or 0),
        "pending_count": pending_count,
        "latest_checked_at": latest_checked,
    }


def auto_tag_run_to_out(run: FactorAutoTagRun | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "message": run.message,
        "stock_count": run.stock_count,
        "tagged_count": run.tagged_count,
        "tag_count": run.tag_count,
        "source_statuses": parse_json_list(run.source_status_json),
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def run_factor_auto_tag_all(db: Session) -> dict[str, Any]:
    ensure_stock_universe(db)
    run = FactorAutoTagRun(status="running", message="Codex/qq 正在给全 A 自动打因子标签", started_at=now_utc())
    db.add(run)
    db.commit()
    try:
        result = build_and_apply_factor_auto_tags(db)
        run.status = "ok" if not result["errors"] else "partial_error"
        run.message = (
            f"Codex/qq 已给 {result['tagged_count']}/{result['stock_count']} 只 A股写入因子标签，"
            f"新增/复用标签 {result['tag_count']} 个。"
        )
        if result["errors"]:
            run.message += f" 部分来源降级：{'；'.join(result['errors'][:3])}"
        run.stock_count = result["stock_count"]
        run.tagged_count = result["tagged_count"]
        run.tag_count = result["tag_count"]
        run.source_status_json = json.dumps(result["sources"], ensure_ascii=False)
        run.completed_at = now_utc()
        db.commit()
        return factor_auto_tag_overview(db)
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.message = f"全 A 自动打标失败：{clean_text(str(exc))[:240]}"
        run.completed_at = now_utc()
        db.commit()
        return factor_auto_tag_overview(db)


def build_and_apply_factor_auto_tags(db: Session) -> dict[str, Any]:
    merge_canonical_factor_tags(db)
    market_cap_refresh_error: str | None = None
    market_cap_refresh_count = 0
    try:
        market_cap_refresh_count = refresh_market_cap_snapshots(db)
    except Exception as exc:  # noqa: BLE001
        market_cap_refresh_error = clean_text(str(exc))[:180]

    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    stocks_query = select(AStock).order_by(AStock.exchange, AStock.code)
    if excluded_codes:
        stocks_query = stocks_query.where(~AStock.full_code.in_(excluded_codes))
    stocks = list(db.scalars(stocks_query))
    full_codes = [stock.full_code for stock in stocks]
    snapshots = {
        row.full_code: row
        for row in db.scalars(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code.in_(full_codes))).all()
    }
    amount_map = latest_amounts_for_full_codes(db, full_codes)
    market_cap_map = latest_market_caps_for_full_codes(db, full_codes)
    ths_map = ths_groups_for_full_codes(db, full_codes)
    sector_map = sector_groups_for_full_codes(db, full_codes)
    candidate_names = latest_candidate_tag_names(db)
    material_tag_map, material_sources = auto_tag_material_hits(db, stocks, candidate_names)
    ima_enabled = os.environ.get("FACTOR_AUTO_TAG_IMA_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if ima_enabled:
        ima_tag_map, ima_sources, ima_errors = auto_tag_ima_hits(db, stocks, snapshots, amount_map, candidate_names)
    else:
        ima_tag_map, ima_sources, ima_errors = {}, [], []
    industry_leader_codes = infer_industry_leader_codes(ths_map, snapshots, amount_map)

    if market_cap_map:
        market_cap_status = "partial_error" if market_cap_refresh_error else "ok"
        market_cap_message = f"读取总市值/流通市值快照 {len(market_cap_map)} 只股票。"
        if market_cap_refresh_count:
            market_cap_message += f" 本次刷新 {market_cap_refresh_count} 条。"
        if market_cap_refresh_error:
            market_cap_message += f" 本次刷新失败，沿用旧快照：{market_cap_refresh_error}"
    else:
        market_cap_status = "partial_error" if market_cap_refresh_error else "not_configured"
        market_cap_message = (
            f"暂无市值快照；已降级为成交容量和板块风格。刷新失败：{market_cap_refresh_error}"
            if market_cap_refresh_error
            else "暂无市值快照；已降级为成交容量和板块风格。"
        )

    source_statuses = [
        {"name": "qq/Codex规则", "status": "ok", "message": "按方向、映射、风格、产业链位置生成标签。"},
        {"name": "市值风格", "status": market_cap_status, "message": market_cap_message},
        {"name": "同花顺行业/概念", "status": "ok", "message": f"覆盖 {len(ths_map)} 只股票。"},
        {"name": "板块工作台", "status": "ok", "message": f"覆盖 {len(sector_map)} 只股票。"},
        *material_sources,
        *ima_sources,
    ]
    errors = [source["message"] for source in source_statuses if source.get("status") in {"partial_error", "error"}]
    errors.extend(ima_errors)

    current_tag_map = tags_for_full_codes(db, full_codes)
    stock_tag_names: dict[str, list[str]] = {}
    stale_tag_names: dict[str, list[str]] = {}
    all_tag_names: set[str] = set()
    legacy_name_only_tags = {"光通信", "半导体", "电力设备"}
    for stock in stocks:
        supported_names = auto_tags_for_stock(
            stock,
            snapshots.get(stock.full_code),
            amount_map.get(stock.full_code),
            market_cap_map.get(stock.full_code),
            ths_map.get(stock.full_code, []),
            sector_map.get(stock.full_code, []),
            material_tag_map.get(stock.full_code, []),
            ima_tag_map.get(stock.full_code, []),
            stock.full_code in industry_leader_codes,
            [],
        )
        existing_names = [tag.name for tag in current_tag_map.get(stock.full_code, [])]
        stale_names = [name for name in existing_names if name in legacy_name_only_tags and name not in supported_names]
        if stale_names:
            stale_tag_names[stock.full_code] = stale_names
        effective_existing_names = [name for name in existing_names if name not in stale_names]
        names = auto_tags_for_stock(
            stock,
            snapshots.get(stock.full_code),
            amount_map.get(stock.full_code),
            market_cap_map.get(stock.full_code),
            ths_map.get(stock.full_code, []),
            sector_map.get(stock.full_code, []),
            material_tag_map.get(stock.full_code, []),
            ima_tag_map.get(stock.full_code, []),
            stock.full_code in industry_leader_codes,
            effective_existing_names,
        )
        stock_tag_names[stock.full_code] = names
        all_tag_names.update(names)

    tag_stock_counts: dict[str, int] = {}
    for names in stock_tag_names.values():
        for name in set(names):
            tag_stock_counts[name] = tag_stock_counts.get(name, 0) + 1
    low_sample_tag_names = {
        name
        for name, count in tag_stock_counts.items()
        if 0 < count < MIN_AUTO_TAG_SAMPLE_COUNT and name not in LOW_SAMPLE_DETAIL_TAGS
    }
    if low_sample_tag_names:
        for full_code, names in list(stock_tag_names.items()):
            stock_tag_names[full_code] = [name for name in names if name not in low_sample_tag_names]
        all_tag_names = {name for name in all_tag_names if name not in low_sample_tag_names}

    created_tags = create_factor_tags_bulk(db, sorted(all_tag_names)) if all_tag_names else []
    tag_rows = db.scalars(select(FactorTag).where(FactorTag.name.in_(sorted(all_tag_names | set(legacy_name_only_tags) | low_sample_tag_names)))).all()
    tag_by_name = {tag.name: tag for tag in tag_rows}
    for full_code, names in stale_tag_names.items():
        stale_ids = [tag_by_name[name].id for name in names if name in tag_by_name]
        if stale_ids:
            db.execute(delete(FactorStockTag).where(FactorStockTag.full_code == full_code, FactorStockTag.tag_id.in_(stale_ids)))
    low_sample_ids = [tag_by_name[name].id for name in low_sample_tag_names if name in tag_by_name]
    if low_sample_ids:
        db.execute(delete(FactorStockTag).where(FactorStockTag.tag_id.in_(low_sample_ids)))
        db.execute(delete(FactorTag).where(FactorTag.id.in_(low_sample_ids)))
    existing_links = {
        (row.full_code, row.tag_id)
        for row in db.scalars(select(FactorStockTag).where(FactorStockTag.full_code.in_(full_codes))).all()
    }
    tagged_count = 0
    for stock in stocks:
        names = stock_tag_names.get(stock.full_code, [])
        if not names:
            continue
        tagged_count += 1
        for name in names:
            tag = tag_by_name.get(name)
            if not tag or (stock.full_code, tag.id) in existing_links:
                continue
            db.add(FactorStockTag(full_code=stock.full_code, tag_id=tag.id))
            existing_links.add((stock.full_code, tag.id))
        review = db.scalar(select(FactorStockReview).where(FactorStockReview.full_code == stock.full_code))
        if not review:
            review = FactorStockReview(code=stock.code, name=stock.name, exchange=stock.exchange, full_code=stock.full_code)
            db.add(review)
        review.code = stock.code
        review.name = stock.name
        review.exchange = stock.exchange
        review.status = "done"
        review.reviewed_at = now_utc()
        review.reviewed_reasons_json = json.dumps(["Codex/qq全A自动打标"], ensure_ascii=False)
        review.reviewed_reason_signature = review_signature(["Codex/qq全A自动打标"])
        review.reviewed_tag_signature = review_signature(names)
    db.commit()
    return {
        "stock_count": len(stocks),
        "tagged_count": tagged_count,
        "tag_count": len(all_tag_names),
        "created_tag_count": len(created_tags),
        "sources": source_statuses,
        "errors": errors,
    }


def merge_canonical_factor_tags(db: Session) -> None:
    tags = list(db.scalars(select(FactorTag).order_by(FactorTag.id)).all())
    tag_by_name = {tag.name: tag for tag in tags}
    changed = False
    for tag in tags:
        canonical = canonical_factor_tag_name(tag.name)
        if not canonical or canonical in FORBIDDEN_CANDIDATE_NAMES:
            db.delete(tag)
            changed = True
            continue
        if canonical == tag.name:
            continue
        target = tag_by_name.get(canonical)
        if target and target.id != tag.id:
            links = list(db.scalars(select(FactorStockTag).where(FactorStockTag.tag_id == tag.id)).all())
            target_codes = set(
                db.scalars(select(FactorStockTag.full_code).where(FactorStockTag.tag_id == target.id)).all()
            )
            for link in links:
                if link.full_code in target_codes:
                    db.delete(link)
                else:
                    link.tag_id = target.id
                    target_codes.add(link.full_code)
            db.delete(tag)
            changed = True
            continue
        if canonical not in tag_by_name:
            tag.name = canonical
            tag_by_name[canonical] = tag
            changed = True
    if changed:
        db.commit()


def ths_groups_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, list[MarketStyleThsMember]]:
    if not full_codes:
        return {}
    rows = db.scalars(select(MarketStyleThsMember).where(MarketStyleThsMember.full_code.in_(full_codes))).all()
    result: dict[str, list[MarketStyleThsMember]] = {}
    for row in rows:
        result.setdefault(row.full_code, []).append(row)
    return result


def sector_groups_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, list[str]]:
    if not full_codes:
        return {}
    rows = db.execute(
        select(SectorIndexMember.full_code, SectorIndex.name)
        .join(SectorIndex, SectorIndex.id == SectorIndexMember.sector_id)
        .where(SectorIndexMember.full_code.in_(full_codes))
        .order_by(SectorIndex.sort_order, SectorIndex.name)
    ).all()
    result: dict[str, list[str]] = {}
    for full_code, name in rows:
        result.setdefault(str(full_code), []).append(str(name))
    return result


def latest_candidate_tag_names(db: Session) -> list[str]:
    run = db.scalar(
        select(FactorTagCandidateRun)
        .where(FactorTagCandidateRun.item_count > 0)
        .order_by(desc(FactorTagCandidateRun.completed_at), desc(FactorTagCandidateRun.id))
        .limit(1)
    )
    names: list[str] = []
    for item in parse_json_list(run.items_json if run else None):
        if not isinstance(item, dict):
            continue
        name = normalize_auto_tag_name(str(item.get("name") or ""))
        if name and name not in names:
            names.append(name)
    return names[:160]


def auto_tag_material_hits(
    db: Session,
    stocks: list[AStock],
    candidate_names: list[str],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    cutoff = now_utc() - timedelta(days=30)
    rows: list[tuple[str, str]] = []
    for item in db.scalars(select(EditedNewsItem).where(EditedNewsItem.crawled_at >= cutoff).order_by(desc(EditedNewsItem.crawled_at)).limit(500)):
        related = " ".join(str(value) for value in parse_json_list(item.related_sectors))
        rows.append(("资讯/新闻", f"{item.title} {item.summary} {item.impact_path} {related}"))
    for post in db.scalars(select(XueqiuPost).where(XueqiuPost.crawled_at >= cutoff).order_by(desc(XueqiuPost.crawled_at)).limit(300)):
        rows.append(("雪球/社区", post.content[:800]))
    for item in db.scalars(select(WatchlistAnnouncementItem).where(WatchlistAnnouncementItem.crawled_at >= cutoff).order_by(desc(WatchlistAnnouncementItem.crawled_at)).limit(300)):
        rows.append(("公告/自选股", f"{item.title} {item.summary}"))
    cutoff_date = cutoff.astimezone(BEIJING).date()
    for material in db.scalars(
        select(MarketReviewMaterial)
        .where(MarketReviewMaterial.report_date >= cutoff_date, MarketReviewMaterial.status != "deleted")
        .order_by(desc(MarketReviewMaterial.report_date), desc(MarketReviewMaterial.created_at))
        .limit(400)
    ):
        source_label = "星球/研报材料" if any(word in f"{material.source_name} {material.source_type}" for word in ("星球", "研报", "券商", "调研")) else "市场复盘材料"
        rows.append((source_label, f"{material.theme} {material.title} {material.content[:900]}"))
    zsxq_rows, zsxq_source_status = auto_tag_zsxq_material_rows()
    rows.extend(zsxq_rows)

    source_names = sorted({source for source, _text in rows})
    source_status = {
        "name": "网络信息热门标签",
        "status": "ok" if rows else "not_configured",
        "message": f"读取近 30 天 {len(rows)} 条本地网络/公告/社区材料。" if rows else "暂无本地网络材料缓存。",
    }
    result: dict[str, list[str]] = {}
    if not rows:
        return result, [source_status, *([zsxq_source_status] if zsxq_source_status else [])]
    stock_lookup = [(stock.full_code, stock.name, stock.code) for stock in stocks]
    hot_names = candidate_names + ["AI", "算力", "液冷", "光模块", "CPO", "硅光", "存储", "HBM", "机器人", "军工", "半导体", "新材料", "涨价", "出海"]
    for source, text in rows:
        clean = clean_text(text)
        if not clean:
            continue
        matched_tags = extract_auto_tags_from_text(clean, hot_names)
        if source in {"雪球/社区", "资讯/新闻", "星球/研报材料", "市场复盘材料", "星球新闻"} and any(word in clean for word in ("传闻", "据传", "小作文", "市场认为", "市场传")):
            matched_tags.append("小作文叙事")
        if not matched_tags:
            continue
        for full_code, name, code in stock_lookup:
            if name and name in clean or code and code in clean or full_code in clean:
                result.setdefault(full_code, [])
                for tag in matched_tags:
                    if tag not in result[full_code]:
                        result[full_code].append(tag)
    source_status["message"] += f" 来源：{'、'.join(source_names)}。"
    return result, [source_status, *([zsxq_source_status] if zsxq_source_status else [])]


def auto_tag_zsxq_material_rows() -> tuple[list[tuple[str, str]], dict[str, str] | None]:
    try:
        from app.main import config_value, zsxq_mcp_url, zsxq_tool_call, zsxq_topic_summary
    except Exception as exc:  # noqa: BLE001
        return [], {"name": "星球信息", "status": "partial_error", "message": f"星球模块不可用：{clean_text(str(exc))[:120]}"}
    mcp_url = zsxq_mcp_url()
    if not mcp_url:
        return [], {"name": "星球信息", "status": "not_configured", "message": "网站后台未配置 ZSXQ_MCP_URL。"}
    try:
        self_info = zsxq_tool_call(mcp_url, "get_self_info", {})
        user_id = str((self_info.get("user") or {}).get("user_id") or "")
        if not user_id:
            raise ValueError("未能从知识星球 MCP 读取用户 ID")
        groups_payload = zsxq_tool_call(mcp_url, "get_user_groups", {"user_id": user_id, "scope": "all", "limit": 50})
        groups = groups_payload.get("groups") or []
        topic_limit = max(1, min(30, int(config_value("ZSXQ_GROUP_TOPIC_LIMIT") or "10")))
        rows: list[tuple[str, str]] = []
        readable_count = 0
        blocked_count = 0
        for group in groups:
            group_id = str(group.get("group_id") or "")
            group_name = str(group.get("name") or "知识星球")
            if not group_id:
                continue
            payload = zsxq_tool_call(mcp_url, "get_group_topics", {"group_id": group_id, "scope": "all", "limit": topic_limit})
            if not payload.get("success"):
                blocked_count += 1
                continue
            readable_count += 1
            for topic in payload.get("topics_brief") or []:
                title = clean_text(topic.get("title") or "")
                content = clean_text(zsxq_topic_summary(topic))
                if title or content:
                    rows.append(("星球新闻", f"{group_name} {title} {content}"))
        status = "ok" if readable_count else "partial_error"
        return rows[:500], {
            "name": "星球信息",
            "status": status,
            "message": f"知识星球 MCP 已接入，可读 {readable_count} 个星球，受限 {blocked_count} 个；读取最近主题 {len(rows[:500])} 条用于全A打标。",
        }
    except Exception as exc:  # noqa: BLE001
        return [], {"name": "星球信息", "status": "partial_error", "message": f"星球信息读取失败：{clean_text(str(exc))[:160]}"}


def auto_tag_ima_hits(
    db: Session,
    stocks: list[AStock],
    snapshots: dict[str, FactorQuoteSnapshot],
    amount_map: dict[str, float],
    candidate_names: list[str],
) -> tuple[dict[str, list[str]], list[dict[str, str]], list[str]]:
    try:
        from app.research_runs import ima_credentials, ima_search_stock_items
    except Exception as exc:  # noqa: BLE001
        return {}, [{"name": "IMA", "status": "partial_error", "message": f"IMA 模块不可用：{clean_text(str(exc))[:120]}"}], []
    credentials = ima_credentials()
    if not credentials:
        return {}, [{"name": "IMA", "status": "not_configured", "message": "IMA 未配置，自动打标跳过知识库增强。"}], []
    try:
        limit = max(0, min(200, int(os.environ.get("FACTOR_AUTO_TAG_IMA_LIMIT", "20"))))
    except ValueError:
        limit = 80
    if limit <= 0:
        return {}, [{"name": "IMA", "status": "skipped", "message": "FACTOR_AUTO_TAG_IMA_LIMIT=0，已跳过 IMA。"}], []
    stock_codes = [stock.full_code for stock in stocks]
    cache_rows = {
        row.full_code: row
        for row in db.scalars(select(FactorImaTagCache).where(FactorImaTagCache.full_code.in_(stock_codes))).all()
    }
    created_pending = 0
    for stock in stocks:
        if stock.full_code in cache_rows:
            continue
        cache = FactorImaTagCache(code=stock.code, name=stock.name, exchange=stock.exchange, full_code=stock.full_code)
        db.add(cache)
        cache_rows[stock.full_code] = cache
        created_pending += 1
    if created_pending:
        db.flush()
    result: dict[str, list[str]] = {}
    fresh_cutoff = now_utc() - timedelta(days=7)
    cached_hit_count = 0
    for full_code, row in cache_rows.items():
        tags = [tag for tag in parse_json_list(row.tags_json) if isinstance(tag, str)]
        if tags:
            result[full_code] = normalize_auto_tag_list(tags)
            cached_hit_count += 1
    ranked = sorted(
        stocks,
        key=lambda stock: (
            -(amount_map.get(stock.full_code) or 0),
            -abs((snapshots.get(stock.full_code).change_pct if snapshots.get(stock.full_code) else 0) or 0),
        ),
    )
    refresh_targets: list[AStock] = []
    for stock in ranked:
        cache = cache_rows.get(stock.full_code)
        if cache and cache.checked_at and cache.checked_at >= fresh_cutoff:
            continue
        refresh_targets.append(stock)
        if len(refresh_targets) >= limit:
            break
    hot_names = candidate_names + ["AI", "算力", "液冷", "光模块", "CPO", "硅光", "存储", "HBM", "机器人", "军工", "半导体", "新材料", "涨价", "出海"]
    failed = 0
    quota_limited = False
    searched_count = 0
    for stock in refresh_targets:
        items, status, _message = ima_search_stock_items(stock)
        searched_count += 1
        if "次数已达上限" in (_message or ""):
            quota_limited = True
            failed += 1
            break
        if status not in {"ok", "not_configured"}:
            failed += 1
        tags: list[str] = []
        for item in items:
            text = f"{item.get('title') or ''} {item.get('content') or ''}"
            for tag in extract_auto_tags_from_text(text, hot_names):
                if tag not in tags:
                    tags.append(tag)
        if tags:
            tags.append("IMA线索")
            result[stock.full_code] = normalize_auto_tag_list(tags[:8])
        cache = cache_rows.get(stock.full_code)
        if not cache:
            cache = FactorImaTagCache(code=stock.code, name=stock.name, exchange=stock.exchange, full_code=stock.full_code)
            db.add(cache)
            cache_rows[stock.full_code] = cache
        cache.code = stock.code
        cache.name = stock.name
        cache.exchange = stock.exchange
        cache.tags_json = json.dumps(result.get(stock.full_code, []), ensure_ascii=False)
        cache.status = status
        cache.message = _message
        cache.checked_at = now_utc()
    db.commit()
    status = "partial_error" if failed else "ok"
    message = f"缓存命中 {cached_hit_count} 只；待补扫缓存新增 {created_pending} 只；本次按成交/异动检索 {searched_count}/{len(refresh_targets)} 只，累计命中 {len(result)} 只。"
    if quota_limited:
        message += " IMA 今日资料获取次数已达上限，后续沿用缓存并等待额度恢复。"
    return result, [{"name": "IMA", "status": status, "message": message}], []


def infer_industry_leader_codes(
    ths_map: dict[str, list[MarketStyleThsMember]],
    snapshots: dict[str, FactorQuoteSnapshot],
    amount_map: dict[str, float],
) -> set[str]:
    industries: dict[str, list[str]] = {}
    for full_code, groups in ths_map.items():
        for group in groups:
            if group.group_type == "industry":
                industries.setdefault(group.group_name, []).append(full_code)
    leaders: set[str] = set()
    for codes in industries.values():
        ranked = sorted(
            codes,
            key=lambda code: (
                -(amount_map.get(code) or 0),
                -abs((snapshots.get(code).change_pct if snapshots.get(code) else 0) or 0),
            ),
        )
        leaders.update(ranked[:2])
    return leaders


def auto_tags_for_stock(
    stock: AStock,
    snapshot: FactorQuoteSnapshot | None,
    amount: float | None,
    market_cap: dict[str, float | None] | None,
    ths_groups: list[MarketStyleThsMember],
    sector_names: list[str],
    material_tags: list[str],
    ima_tags: list[str],
    is_industry_leader: bool,
    existing_tag_names: list[str],
) -> list[str]:
    tags = normalize_auto_tag_list(existing_tag_names)
    tags.extend(board_style_tags(stock))
    if snapshot:
        returns = parse_returns(snapshot.returns_json)
        change = returns.get("1", snapshot.change_pct)
        if change is not None and change >= 7:
            tags.append("强势股")
        if change is not None and change <= -7:
            tags.append("亏钱效应")
        if returns.get("5") is not None and returns["5"] >= 12:
            tags.append("趋势强势")
    if amount is not None:
        if amount >= 2_000_000_000:
            tags.append("高成交额")
            tags.append("容量核心")
        elif amount >= 500_000_000:
            tags.append("活跃成交")
    tags.extend(market_cap_style_tags(market_cap))
    if is_industry_leader:
        tags.append("行业龙头候选")
    tags.extend(STOCK_DETAIL_TAG_SEEDS.get(stock.full_code, []))
    for group in ths_groups:
        name = normalize_auto_tag_name(group.group_name)
        if not name:
            continue
        if group.group_type == "industry":
            tags.append(name)
        elif group.group_type == "concept" and len(tags) < 14:
            tags.append(name)
    for name in sector_names[:4]:
        tag = normalize_auto_tag_name(name)
        if tag:
            tags.append(tag)
    tags.extend(material_tags)
    tags.extend(ima_tags)
    tags.extend(name_based_qq_tags(stock.name))
    if not normalize_auto_tag_list(tags):
        tags.append("A股基础池")
    return normalize_auto_tag_list(tags)[:18]


def market_cap_style_tags(market_cap: dict[str, float | None] | None) -> list[str]:
    total = (market_cap or {}).get("total_market_cap")
    if total is None or total <= 0:
        return []
    tags: list[str] = []
    if total >= 200_000_000_000:
        tags.extend(["大盘权重", "市值龙头候选"])
    elif total >= 50_000_000_000:
        tags.append("中大盘")
    elif total >= 10_000_000_000:
        tags.append("中盘")
    elif total < 5_000_000_000:
        tags.append("小市值")
    else:
        tags.append("中小盘")
    return tags


def board_style_tags(stock: AStock) -> list[str]:
    code = stock.code
    if code.startswith(("688", "689")):
        return ["科创板", "硬科技"]
    if code.startswith(("300", "301")):
        return ["创业板", "高弹性"]
    if code.startswith(("8", "4")) or stock.exchange == "BJ":
        return ["北交所", "小盘弹性"]
    if code.startswith(("600", "601", "603", "605")):
        return ["沪市主板"]
    if code.startswith(("000", "001", "002", "003")):
        return ["深市主板"]
    if stock.exchange == "SH":
        return ["沪市股票"]
    if stock.exchange == "SZ":
        return ["深市股票"]
    return ["A股基础池"]


def name_based_qq_tags(name: str) -> list[str]:
    rules = [
        ("光模块", "光模块"),
        ("光通信", "光通信"),
        ("光纤", "光纤"),
        ("光缆", "光纤光缆"),
        ("光电", "光学光电子"),
        ("光学", "光学光电子"),
        ("激光", "激光"),
        ("光子", "光芯片"),
        ("半导体", "半导体"),
        ("芯片", "半导体"),
        ("微电子", "半导体"),
        ("集成电路", "半导体"),
        ("电力", "电力设备"),
        ("电气", "电力设备"),
        ("电源", "电力设备"),
        ("药", "医药"),
        ("生物", "医药"),
        ("机器人", "机器人"),
        ("材料", "新材料"),
        ("科技", "科技成长"),
        ("航天", "军工"),
        ("航空", "军工"),
        ("能源", "能源"),
        ("证券", "券商"),
        ("银行", "银行"),
    ]
    return [tag for keyword, tag in rules if keyword in name]


def extract_auto_tags_from_text(text: str, candidates: list[str]) -> list[str]:
    tags: list[str] = []
    for name in candidates:
        tag = normalize_auto_tag_name(name)
        if tag and tag in text and tag not in tags:
            tags.append(tag)
    mapping = [
        ("光模块", "光模块"),
        ("CPO", "CPO"),
        ("硅光", "硅光"),
        ("GlassBridge", "GlassBridge"),
        ("玻璃桥", "GlassBridge"),
        ("液冷", "液冷"),
        ("HBM", "HBM"),
        ("存储", "存储"),
        ("AI服务器", "AI服务器"),
        ("算力", "AI算力"),
        ("机器人", "机器人"),
        ("涨价", "涨价线索"),
        ("供不应求", "供需紧缺"),
        ("断供", "供需紧缺"),
        ("扩产", "产能扩充"),
        ("送样", "客户验证"),
        ("验证", "客户验证"),
        ("军工", "军工"),
        ("半导体", "半导体"),
        ("四氯化硅", "四氯化硅"),
        ("正硅酸乙酯", "TEOS"),
        ("TEOS", "TEOS"),
        ("半导体石英砂", "高纯石英砂"),
        ("高纯石英砂", "高纯石英砂"),
        ("氧化锆", "氧化锆"),
        ("正丙醇锆", "正丙醇锆"),
        ("钛酸钡", "钛酸钡"),
        ("光纤光棒", "光棒"),
        ("光棒", "光棒"),
        ("保偏光纤", "保偏光纤"),
        ("特种光纤", "特种光纤"),
        ("光纤陀螺", "光纤陀螺"),
        ("光纤环", "光纤环"),
        ("新材料", "新材料"),
        ("出海", "出海"),
    ]
    upper_text = text.upper()
    for keyword, tag in mapping:
        haystack = upper_text if keyword.isascii() else text
        needle = keyword.upper() if keyword.isascii() else keyword
        if needle in haystack and tag not in tags:
            tags.append(tag)
    return tags[:8]


def canonical_factor_tag_name(value: str) -> str:
    cleaned = clean_text(value).replace("#", "")
    cleaned = re.sub(r"(概念|板块|指数|行业)$", "", cleaned).strip()
    if not cleaned:
        return ""
    cleaned = CANONICAL_TAG_ALIASES.get(cleaned, cleaned)
    casefold_map = {key.casefold(): canonical for key, canonical in CANONICAL_TAG_ALIASES.items()}
    cleaned = casefold_map.get(cleaned.casefold(), cleaned)
    return cleaned


def normalize_auto_tag_name(value: str) -> str:
    cleaned = canonical_factor_tag_name(value)
    if not cleaned or cleaned in FORBIDDEN_CANDIDATE_NAMES or len(cleaned) > 24:
        return ""
    return cleaned


def normalize_auto_tag_list(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    pending = list(values)
    index = 0
    while index < len(pending):
        value = pending[index]
        index += 1
        cleaned = normalize_auto_tag_name(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        for parent in TAG_PARENT_EXPANSIONS.get(cleaned, []):
            if parent.casefold() not in seen:
                pending.append(parent)
    return result


def factor_tagged_stocks(db: Session, limit: int = 800, offset: int = 0, query: str | None = None) -> dict[str, Any]:
    limit = max(20, min(10000, int(limit or 800)))
    offset = max(0, int(offset or 0))
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    stocks_query = select(AStock).order_by(AStock.exchange, AStock.code)
    if excluded_codes:
        stocks_query = stocks_query.where(~AStock.full_code.in_(excluded_codes))
    if query and query.strip():
        keyword = f"%{query.strip().upper()}%"
        stocks_query = stocks_query.where(or_(AStock.full_code.like(keyword), AStock.code.like(keyword), AStock.name.like(f"%{query.strip()}%")))
    total_count = db.scalar(select(func.count()).select_from(stocks_query.subquery())) or 0
    stocks = list(db.scalars(stocks_query.offset(offset).limit(limit)).all())
    full_codes = [stock.full_code for stock in stocks]
    tag_map = tags_for_full_codes(db, full_codes)
    review_map = review_statuses_for_full_codes(db, full_codes, tag_map)
    watchlist_map = watchlist_statuses_for_full_codes(db, full_codes)
    snapshot_map = {row.full_code: row for row in db.scalars(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code.in_(full_codes))).all()}
    amount_map = latest_amounts_for_full_codes(db, full_codes)
    rows: list[dict[str, Any]] = []
    for stock in stocks:
        snapshot = snapshot_map.get(stock.full_code) or FactorQuoteSnapshot(code=stock.code, name=stock.name, exchange=stock.exchange, full_code=stock.full_code)
        return_pct = parse_returns(snapshot.returns_json).get("1", snapshot.change_pct or 0) if snapshot else 0
        tags = tag_map.get(stock.full_code, [])
        out = ranking_stock_to_out(snapshot, return_pct or 0, tags, review_map.get(stock.full_code, "unreviewed"), watchlist_map.get(stock.full_code, {}))
        out.update(
            {
                "reasons": ["全A标签库"],
                "suggested_tags": [tag.name for tag in tags],
                "amount": amount_map.get(stock.full_code),
            }
        )
        rows.append(out)
    tagged_count = db.scalar(select(func.count(func.distinct(FactorStockTag.full_code)))) or 0
    latest = db.scalar(select(FactorAutoTagRun).order_by(desc(FactorAutoTagRun.started_at), desc(FactorAutoTagRun.id)).limit(1))
    return {
        "status": "ok" if rows else "not_configured",
        "source_status": latest.status if latest else "not_configured",
        "updated_at": latest.completed_at if latest else None,
        "message": f"全 A 标签库当前已覆盖 {tagged_count} 只股票，本页显示 {len(rows)} / {total_count} 只。",
        "total_count": total_count,
        "tagged_count": tagged_count,
        "stocks": rows,
    }


def latest_amounts_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, float]:
    if not full_codes:
        return {}
    codes = sorted(set(full_codes))
    latest_dates = (
        select(StockDailyBar.full_code, func.max(StockDailyBar.trade_date).label("latest_trade_date"))
        .where(StockDailyBar.full_code.in_(codes))
        .group_by(StockDailyBar.full_code)
        .subquery()
    )
    rows = db.execute(
        select(StockDailyBar.full_code, StockDailyBar.amount)
        .join(
            latest_dates,
            and_(
                StockDailyBar.full_code == latest_dates.c.full_code,
                StockDailyBar.trade_date == latest_dates.c.latest_trade_date,
            ),
        )
        .where(StockDailyBar.amount.is_not(None))
    ).all()
    return {full_code: float(amount) for full_code, amount in rows if amount is not None}


def latest_market_caps_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, dict[str, float | None]]:
    if not full_codes:
        return {}
    rows = db.scalars(select(FactorMarketCapSnapshot).where(FactorMarketCapSnapshot.full_code.in_(sorted(set(full_codes))))).all()
    return {
        row.full_code: {
            "total_market_cap": float(row.total_market_cap) if row.total_market_cap is not None else None,
            "float_market_cap": float(row.float_market_cap) if row.float_market_cap is not None else None,
        }
        for row in rows
    }


def suggest_stock_tags_from_snapshot(snapshot: FactorQuoteSnapshot, reasons: list[str]) -> list[str]:
    tags: list[str] = []
    name = snapshot.name
    if "涨幅" in " ".join(reasons):
        tags.append("强势股")
    if "成交额" in " ".join(reasons):
        tags.append("高成交额")
    rules = [
        ("光", "光通信"),
        ("芯", "半导体"),
        ("微", "半导体"),
        ("电", "电力设备"),
        ("药", "医药"),
        ("生物", "医药"),
        ("机器人", "机器人"),
        ("材料", "新材料"),
        ("科技", "科技成长"),
    ]
    for keyword, tag in rules:
        if keyword in name and tag not in tags:
            tags.append(tag)
    return tags[:5] or ["待分类"]


def factor_tag_stock_detail(db: Session, tag_id: int, period: int = 3, direction: str = "winning") -> dict[str, Any]:
    if direction not in {"winning", "losing"}:
        raise ValueError("方向参数不正确")
    tag = db.get(FactorTag, tag_id)
    if not tag:
        raise ValueError("标签不存在")
    period = int(period or 3)
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    snapshots_query = select(FactorQuoteSnapshot).order_by(desc(FactorQuoteSnapshot.updated_at))
    if excluded_codes:
        snapshots_query = snapshots_query.where(~FactorQuoteSnapshot.full_code.in_(excluded_codes))
    snapshots = list(db.scalars(snapshots_query).all())
    snapshot_codes = [snapshot.full_code for snapshot in snapshots]
    tag_map = tags_for_full_codes(db, snapshot_codes)
    review_map = review_statuses_for_full_codes(db, snapshot_codes, tag_map)
    watchlist_map = watchlist_statuses_for_full_codes(db, snapshot_codes)
    ranking = build_period_ranking(period, snapshots, tag_map, review_map, watchlist_map)
    source_stocks = ranking["gainers"] if direction == "winning" else ranking["losers"]
    sample_stocks = [
        stock
        for stock in source_stocks
        if any(item["id"] == tag.id for item in stock.get("tags", []))
    ]
    average_return = (
        round(sum(stock["return_pct"] for stock in sample_stocks) / len(sample_stocks), 4)
        if sample_stocks
        else None
    )
    all_full_codes = list(
        db.scalars(
            select(FactorStockTag.full_code)
            .where(FactorStockTag.tag_id == tag.id)
            .order_by(FactorStockTag.full_code)
        ).all()
    )
    all_stocks = factor_tag_stock_rows(db, all_full_codes, period)
    all_stocks.sort(key=lambda stock: (stock["return_pct"] is None, -(stock["return_pct"] or 0), stock["code"]))
    return {
        "tag": tag_to_out(db, tag, len(all_full_codes)),
        "period": period,
        "direction": direction,
        "direction_label": "赚钱标签" if direction == "winning" else "亏钱标签",
        "average_return_pct": average_return,
        "sample_count": len(sample_stocks),
        "coverage_count": ranking["coverage_count"],
        "sample_stocks": sample_stocks,
        "all_stocks": all_stocks,
    }


def factor_tag_stock_rows(db: Session, full_codes: list[str], period: int) -> list[dict[str, Any]]:
    if not full_codes:
        return []
    unique_codes = sorted(set(full_codes))
    stocks = {
        stock.full_code: stock
        for stock in db.scalars(select(AStock).where(AStock.full_code.in_(unique_codes))).all()
    }
    snapshots = {
        snapshot.full_code: snapshot
        for snapshot in db.scalars(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code.in_(unique_codes))).all()
    }
    tag_map = tags_for_full_codes(db, unique_codes)
    tag_counts = tag_stock_counts(db, [tag.id for tags in tag_map.values() for tag in tags])
    review_map = review_statuses_for_full_codes(db, unique_codes, tag_map)
    watchlist_map = watchlist_statuses_for_full_codes(db, unique_codes)
    rows: list[dict[str, Any]] = []
    for full_code in unique_codes:
        stock = stocks.get(full_code)
        snapshot = snapshots.get(full_code)
        if not stock and not snapshot:
            continue
        returns = parse_returns(snapshot.returns_json) if snapshot else {}
        value = returns.get(str(period))
        code = stock.code if stock else snapshot.code
        exchange = stock.exchange if stock else snapshot.exchange
        name = stock.name if stock else snapshot.name
        rows.append(
            {
                "code": code,
                "name": name,
                "exchange": exchange,
                "full_code": full_code,
                "latest_price": snapshot.latest_price if snapshot else None,
                "change_pct": snapshot.change_pct if snapshot else None,
                "return_pct": round(value, 4) if value is not None else None,
                "tags": [tag_to_out(db, tag, tag_counts.get(tag.id, 0)) for tag in tag_map.get(full_code, [])],
                "is_untagged": not tag_map.get(full_code),
                "tag_status": review_map.get(full_code, "unreviewed"),
                "watchlist_added": bool(watchlist_map.get(full_code, {}).get("watchlist_added")),
                "watchlist_enabled": bool(watchlist_map.get(full_code, {}).get("watchlist_enabled")),
                "watchlist_push_enabled": bool(watchlist_map.get(full_code, {}).get("watchlist_push_enabled")),
                "updated_at": snapshot.updated_at if snapshot else None,
            }
        )
    return rows


def build_review_stocks(
    db: Session,
    stock_rankings: list[dict[str, Any]],
    tag_map: dict[str, list[FactorTag]],
) -> list[dict[str, Any]]:
    ranked: dict[str, dict[str, Any]] = {}
    for ranking in stock_rankings:
        period = ranking["period"]
        for direction, stocks in (("强势", ranking["gainers"]), ("亏钱", ranking["losers"])):
            reason = f"{direction}{period}日"
            for stock in stocks:
                full_code = stock["full_code"]
                row = ranked.get(full_code)
                if not row or abs(stock["return_pct"]) > abs(row["return_pct"]):
                    row = {**stock, "reasons": row["reasons"] if row else []}
                    ranked[full_code] = row
                if reason not in row["reasons"]:
                    row["reasons"].append(reason)
    if not ranked:
        return []

    reviews = db.scalars(
        select(FactorStockReview).where(
            FactorStockReview.status == "done",
            FactorStockReview.full_code.in_(sorted(ranked)),
        )
    ).all()
    review_map = {review.full_code: review for review in reviews}
    result: list[dict[str, Any]] = []
    for full_code, stock in ranked.items():
        review = review_map.get(full_code)
        if not review:
            continue
        tags = tag_map.get(full_code, [])
        reasons = normalize_review_values(stock["reasons"])
        review_reasons: list[str] = []
        if not tags:
            review_reasons.append("标签缺失")
        if tags and review.reviewed_tag_signature and review.reviewed_tag_signature != tag_signature(tags):
            review_reasons.append("标签变化")
        if review.reviewed_reason_signature and review.reviewed_reason_signature != review_signature(reasons):
            review_reasons.append("来源变化")
        if not review_reasons:
            continue
        result.append(
            {
                **stock,
                "tags": [tag_to_plain(tag) for tag in tags],
                "is_untagged": not tags,
                "tag_status": "done",
                "reasons": reasons,
                "review_reasons": review_reasons,
            }
        )
    return sorted(result, key=lambda stock: (len(stock["review_reasons"]), abs(stock["return_pct"])), reverse=True)


def build_period_ranking(
    period: int,
    snapshots: list[FactorQuoteSnapshot],
    tag_map: dict[str, list[FactorTag]],
    review_map: dict[str, str],
    watchlist_map: dict[str, dict[str, bool]],
) -> dict[str, Any]:
    rows: list[tuple[FactorQuoteSnapshot, float]] = []
    key = str(period)
    for snapshot in snapshots:
        returns = parse_returns(snapshot.returns_json)
        value = returns.get(key)
        if value is None:
            continue
        rows.append((snapshot, value))
    rows.sort(key=lambda item: item[1], reverse=True)
    gainers = rows[:DEFAULT_TOP_N]
    losers = list(reversed(rows[-DEFAULT_TOP_N:])) if rows else []
    return {
        "period": period,
        "gainers": [ranking_stock_to_out(snapshot, value, tag_map.get(snapshot.full_code, []), review_map.get(snapshot.full_code, "unreviewed"), watchlist_map.get(snapshot.full_code, {})) for snapshot, value in gainers],
        "losers": [ranking_stock_to_out(snapshot, value, tag_map.get(snapshot.full_code, []), review_map.get(snapshot.full_code, "unreviewed"), watchlist_map.get(snapshot.full_code, {})) for snapshot, value in losers],
        "winning_tags": build_tag_rankings(gainers, tag_map, reverse=True, board_style=False),
        "losing_tags": build_tag_rankings(losers, tag_map, reverse=False, board_style=False),
        "board_winning_tags": build_tag_rankings(gainers, tag_map, reverse=True, board_style=True),
        "board_losing_tags": build_tag_rankings(losers, tag_map, reverse=False, board_style=True),
        "coverage_count": len(rows),
    }


def ranking_stock_to_out(snapshot: FactorQuoteSnapshot, return_pct: float, tags: list[FactorTag], tag_status: str, watchlist: dict[str, bool] | None = None) -> dict[str, Any]:
    watchlist = watchlist or {}
    return {
        "code": snapshot.code,
        "name": snapshot.name,
        "exchange": snapshot.exchange,
        "full_code": snapshot.full_code,
        "latest_price": snapshot.latest_price,
        "change_pct": snapshot.change_pct,
        "return_pct": round(return_pct, 4),
        "tags": [tag_to_plain(tag) for tag in tags],
        "is_untagged": not tags,
        "tag_status": tag_status,
        "watchlist_added": bool(watchlist.get("watchlist_added")),
        "watchlist_enabled": bool(watchlist.get("watchlist_enabled")),
        "watchlist_push_enabled": bool(watchlist.get("watchlist_push_enabled")),
        "updated_at": snapshot.updated_at,
    }


def tag_to_plain(tag: FactorTag) -> dict[str, Any]:
    return {
        "id": tag.id,
        "name": tag.name,
        "color": tag.color,
        "stock_count": 0,
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
    }


def inferred_review_status(status: str | None, has_tags: bool) -> str:
    if status in FACTOR_REVIEW_STATUSES:
        return status
    return "needs_more" if has_tags else "unreviewed"


def review_statuses_for_full_codes(
    db: Session,
    full_codes: list[str],
    tag_map: dict[str, list[FactorTag]],
) -> dict[str, str]:
    if not full_codes:
        return {}
    rows = db.scalars(select(FactorStockReview).where(FactorStockReview.full_code.in_(sorted(set(full_codes))))).all()
    explicit = {row.full_code: row.status for row in rows}
    return {
        full_code: inferred_review_status(explicit.get(full_code), bool(tag_map.get(full_code)))
        for full_code in set(full_codes)
    }


def watchlist_statuses_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, dict[str, bool]]:
    if not full_codes:
        return {}
    rows = db.scalars(
        select(WatchlistAnnouncementStock).where(WatchlistAnnouncementStock.full_code.in_(sorted(set(full_codes))))
    ).all()
    return {
        row.full_code: {
            "watchlist_added": True,
            "watchlist_enabled": bool(row.enabled),
            "watchlist_push_enabled": bool(row.push_enabled),
        }
        for row in rows
    }


def build_tag_rankings(
    rows: list[tuple[FactorQuoteSnapshot, float]],
    tag_map: dict[str, list[FactorTag]],
    reverse: bool,
    board_style: bool | None = None,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for snapshot, value in rows:
        tags = tag_map.get(snapshot.full_code) or []
        matching_tags: list[FactorTag] = []
        for tag in tags:
            is_board_style = is_factor_board_style_tag(tag.name)
            if board_style is True and not is_board_style:
                continue
            if board_style is False and is_board_style:
                continue
            matching_tags.append(tag)
        if not matching_tags:
            if board_style is True:
                continue
            bucket = buckets.setdefault(
                "__untagged__",
                {"tag_id": None, "tag_name": UNTAGGED_NAME, "color": None, "values": [], "stock_codes": [], "system": True},
            )
            bucket["values"].append(value)
            bucket["stock_codes"].append(snapshot.full_code)
            continue
        for tag in matching_tags:
            bucket = buckets.setdefault(
                str(tag.id),
                {"tag_id": tag.id, "tag_name": tag.name, "color": tag.color, "values": [], "stock_codes": [], "system": False},
            )
            bucket["values"].append(value)
            bucket["stock_codes"].append(snapshot.full_code)
    ranked: list[dict[str, Any]] = []
    for bucket in buckets.values():
        values = bucket.pop("values")
        if not bucket["system"] and len(values) < MIN_EFFECT_TAG_SAMPLE_COUNT:
            continue
        average = sum(values) / len(values) if values else 0
        ranked.append(
            {
                "tag_id": bucket["tag_id"],
                "tag_name": bucket["tag_name"],
                "color": bucket["color"],
                "stock_count": len(values),
                "average_return_pct": round(average, 4),
                "stock_codes": bucket["stock_codes"][:20],
                "system": bucket["system"],
            }
        )
    ranked.sort(key=lambda item: item["average_return_pct"], reverse=reverse)
    return ranked


def factor_tag_candidates_overview(db: Session) -> dict[str, Any]:
    latest_run = db.scalar(select(FactorTagCandidateRun).order_by(desc(FactorTagCandidateRun.started_at), desc(FactorTagCandidateRun.id)).limit(1))
    data_run = db.scalar(
        select(FactorTagCandidateRun)
        .where(FactorTagCandidateRun.item_count > 0)
        .order_by(desc(FactorTagCandidateRun.completed_at), desc(FactorTagCandidateRun.id))
        .limit(1)
    )
    if not latest_run and not data_run:
        return {
            "status": "not_configured",
            "source_status": "not_configured",
            "updated_at": None,
            "candidates": [],
            "sources": [],
            "latest_run": None,
            "message": "还没有生成候选标签。",
        }
    display_run = data_run or latest_run
    candidates = parse_json_list(display_run.items_json if display_run else None)
    latest_out = tag_candidate_run_to_out(latest_run)
    source_status = latest_run.status if latest_run else display_run.status
    if latest_run and latest_run.status == "error" and data_run and data_run.id != latest_run.id:
        source_status = "partial_error"
    return {
        "status": "ok" if candidates else source_status,
        "source_status": source_status,
        "updated_at": display_run.completed_at if display_run else None,
        "candidates": candidates,
        "sources": parse_json_list(display_run.sources_json if display_run else None),
        "latest_run": latest_out,
        "message": latest_run.message if latest_run and latest_run.message else "候选标签已生成。",
    }


def tag_candidate_run_to_out(run: FactorTagCandidateRun | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "message": run.message,
        "lookback_days": run.lookback_days,
        "item_count": run.item_count,
        "failed_count": run.failed_count,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def run_factor_tag_candidates(db: Session, lookback_days: int = 30, limit: int = TAG_CANDIDATE_DEFAULT_LIMIT) -> dict[str, Any]:
    lookback_days = max(5, min(90, int(lookback_days or 30)))
    limit = max(20, min(240, int(limit or TAG_CANDIDATE_DEFAULT_LIMIT)))
    run = FactorTagCandidateRun(status="manual_only", message="候选标签生成中", lookback_days=lookback_days, started_at=now_utc())
    db.add(run)
    db.commit()
    try:
        candidates, sources, errors = build_factor_tag_candidates(db, lookback_days, limit)
        status = "partial_error" if errors and candidates else ("error" if errors else "ok")
        message = f"已生成 {len(candidates)} 个候选标签。"
        if errors:
            message += f" 部分来源失败：{'；'.join(errors[:3])}"
        run.status = status
        run.message = message
        run.items_json = json.dumps(candidates, ensure_ascii=False)
        run.sources_json = json.dumps(sources, ensure_ascii=False)
        run.item_count = len(candidates)
        run.failed_count = len(errors)
        run.completed_at = now_utc()
        db.commit()
        return factor_tag_candidates_overview(db)
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.message = f"候选标签生成失败：{clean_text(str(exc))[:180]}"
        run.failed_count = 1
        run.completed_at = now_utc()
        db.commit()
        return factor_tag_candidates_overview(db)


def should_generate_tag_candidates_now(db: Session, moment: datetime | None = None) -> bool:
    now = moment.astimezone(BEIJING) if moment else datetime.now(BEIJING)
    if now.weekday() >= 5 or now.time() < time(15, 45):
        return False
    latest = db.scalar(
        select(FactorTagCandidateRun)
        .where(FactorTagCandidateRun.status.in_(["ok", "partial_error"]))
        .order_by(desc(FactorTagCandidateRun.completed_at), desc(FactorTagCandidateRun.id))
        .limit(1)
    )
    if not latest or not latest.completed_at:
        return True
    return latest.completed_at.astimezone(BEIJING).date() < now.date()


def build_factor_tag_candidates(db: Session, lookback_days: int, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    buckets: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    stock_names = set(db.scalars(select(AStock.name)).all())
    material_counts, corpus = recent_material_tag_counts(db, lookback_days)
    for name, count in material_counts.items():
        add_tag_candidate(
            buckets,
            name,
            "概念",
            "中性热度",
            min(40.0, count * 8.0),
            f"近 {lookback_days} 天新闻/材料命中 {count} 次。",
            "新闻/材料",
            stock_names=stock_names,
        )

    collect_rank_tag_candidates(buckets, sources, errors, stock_names)
    collect_concept_tag_candidates(buckets, sources, errors, stock_names, material_counts, corpus, lookback_days)
    collect_xueqiu_hot_candidates(buckets, sources, errors, stock_names)
    collect_eastmoney_hot_candidates(buckets, sources, errors, stock_names)

    candidates = finalize_tag_candidates(buckets, limit)
    return candidates, sources, errors


def recent_material_tag_counts(db: Session, lookback_days: int) -> tuple[dict[str, int], str]:
    cutoff = now_utc() - timedelta(days=lookback_days)
    counts: dict[str, int] = {}
    texts: list[str] = []
    news_rows = db.scalars(
        select(EditedNewsItem)
        .where(EditedNewsItem.crawled_at >= cutoff)
        .order_by(desc(EditedNewsItem.crawled_at))
        .limit(400)
    )
    for item in news_rows:
        texts.append(f"{item.title} {item.summary} {item.impact_path}")
        for sector in parse_json_list(item.related_sectors):
            if isinstance(sector, str):
                name = normalize_candidate_name(sector)
                if name:
                    counts[name] = counts.get(name, 0) + 1
    post_rows = db.scalars(
        select(XueqiuPost)
        .where(XueqiuPost.crawled_at >= cutoff)
        .order_by(desc(XueqiuPost.crawled_at))
        .limit(300)
    )
    for post in post_rows:
        texts.append(post.content[:500])
    cutoff_date = cutoff.astimezone(BEIJING).date()
    material_rows = db.scalars(
        select(MarketReviewMaterial)
        .where(MarketReviewMaterial.report_date >= cutoff_date, MarketReviewMaterial.status != "deleted")
        .order_by(desc(MarketReviewMaterial.report_date), desc(MarketReviewMaterial.created_at))
        .limit(400)
    )
    for material in material_rows:
        text = f"{material.theme} {material.title} {material.content[:700]}"
        texts.append(text)
        for name in extract_auto_tags_from_text(text, []):
            cleaned = normalize_candidate_name(name)
            if cleaned:
                counts[cleaned] = counts.get(cleaned, 0) + 1
    return counts, " ".join(texts)


def collect_rank_tag_candidates(
    buckets: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    errors: list[str],
    stock_names: set[str],
) -> None:
    rank_sources = [
        ("stock_rank_ljqs_ths", "量价齐升", "赚钱", "量价齐升股集中出现。", "阶段涨幅"),
        ("stock_rank_lxsz_ths", "连续上涨", "赚钱", "连续上涨股集中出现。", "连续涨跌幅"),
        ("stock_rank_cxg_ths", "创新高", "赚钱", "创阶段新高股票集中出现。", "涨跌幅"),
        ("stock_rank_cxfl_ths", "持续放量", "中性热度", "持续放量股票集中出现。", "阶段涨跌幅"),
        ("stock_rank_ljqd_ths", "量价齐跌", "亏钱", "量价齐跌股集中出现。", "阶段涨幅"),
        ("stock_rank_lxxd_ths", "连续下跌", "亏钱", "连续下跌股集中出现。", "连续涨跌幅"),
    ]
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        errors.append(f"akshare 不可用：{exc}")
        return
    for func_name, tag_name, direction, reason, return_field in rank_sources:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                frame = getattr(ak, func_name)()
            rows = dataframe_records(frame, 160)
            examples = stock_examples_from_rows(rows)
            values = [value for value in (parse_float(first_value(row, return_field, "阶段涨幅", "连续涨跌幅", "涨跌幅")) for row in rows[:80]) if value is not None]
            avg_return = sum(values) / len(values) if values else None
            score = min(86.0, 42.0 + len(rows[:80]) * 0.18 + (abs(avg_return or 0) * 0.8))
            add_tag_candidate(
                buckets,
                tag_name,
                "交易特征",
                direction,
                score,
                reason,
                "同花顺强弱榜",
                examples,
                {return_field: round(avg_return, 2)} if avg_return is not None else {},
                stock_names,
            )
            add_industry_candidates_from_rank_rows(buckets, rows, direction, return_field, func_name, stock_names)
            sources.append({"name": func_name, "status": "ok", "message": f"读取 {len(rows)} 条", "count": len(rows)})
        except Exception as exc:  # noqa: BLE001
            message = f"{func_name}: {clean_text(str(exc))[:120]}"
            errors.append(message)
            sources.append({"name": func_name, "status": "error", "message": message, "count": 0})


def add_industry_candidates_from_rank_rows(
    buckets: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    direction: str,
    return_field: str,
    source_name: str,
    stock_names: set[str],
) -> None:
    industries: dict[str, dict[str, Any]] = {}
    for row in rows[:160]:
        industry = normalize_candidate_name(str(first_value(row, "所属行业") or ""))
        if not industry:
            continue
        value = parse_float(first_value(row, return_field, "阶段涨幅", "连续涨跌幅", "涨跌幅"))
        bucket = industries.setdefault(industry, {"values": [], "examples": []})
        if value is not None:
            bucket["values"].append(value)
        name = clean_text(first_value(row, "股票简称", "名称") or "")
        code = normalize_code(first_value(row, "股票代码", "代码") or "")
        if name and len(bucket["examples"]) < 5:
            bucket["examples"].append(f"{name} {code}".strip())
    for industry, payload in industries.items():
        values = payload["values"]
        count = len(values) or len(payload["examples"])
        if count < 3:
            continue
        avg_return = sum(values) / len(values) if values else 0
        score = min(92.0, 28.0 + count * 2.2 + abs(avg_return) * 0.9)
        add_tag_candidate(
            buckets,
            industry,
            "行业",
            "赚钱" if avg_return >= 0 and direction != "亏钱" else "亏钱" if avg_return < 0 or direction == "亏钱" else direction,
            score,
            f"在同花顺强弱榜出现 {count} 次，平均表现 {avg_return:.2f}%。",
            source_name,
            payload["examples"],
            {"平均表现": round(avg_return, 2), "样本数": count},
            stock_names,
        )


def collect_concept_tag_candidates(
    buckets: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    errors: list[str],
    stock_names: set[str],
    material_counts: dict[str, int],
    corpus: str,
    lookback_days: int,
) -> None:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        errors.append(f"akshare 不可用：{exc}")
        return
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = ak.stock_board_concept_name_ths()
        rows = dataframe_records(frame, 500)
        concepts: list[tuple[str, str]] = []
        for row in rows:
            raw_name = clean_text(first_value(row, "name", "概念名称", "名称") or "")
            display_name = normalize_candidate_name(raw_name)
            if display_name and raw_name:
                concepts.append((display_name, raw_name))
        sources.append({"name": "stock_board_concept_name_ths", "status": "ok", "message": f"读取 {len(concepts)} 个概念", "count": len(concepts)})
    except Exception as exc:  # noqa: BLE001
        message = f"同花顺概念列表失败：{clean_text(str(exc))[:120]}"
        errors.append(message)
        sources.append({"name": "stock_board_concept_name_ths", "status": "error", "message": message, "count": 0})
        return

    seed_words = hot_concept_seed_words()
    scored: list[tuple[float, str, str]] = []
    corpus_lower = corpus.lower()
    for display_name, raw_name in concepts:
        material_score = material_counts.get(display_name, 0) * 10
        text_score = 8 if display_name.lower() in corpus_lower or raw_name.lower() in corpus_lower else 0
        seed_score = max((weight for keyword, weight in seed_words.items() if keyword.lower() in display_name.lower() or keyword.lower() in raw_name.lower()), default=0)
        total = material_score + text_score + seed_score
        if total > 0:
            scored.append((total, display_name, raw_name))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[:TAG_CANDIDATE_CONCEPT_INDEX_LIMIT]
    for base_score, concept, query_name in selected:
        try:
            returns = fetch_ths_concept_returns(query_name, lookback_days)
            month_return = returns.get("近30日", 0)
            direction = "赚钱" if month_return > 0 else "亏钱" if month_return < 0 else "中性热度"
            score = min(96.0, base_score + abs(month_return) * 1.6 + 20.0)
            add_tag_candidate(
                buckets,
                concept,
                "概念",
                direction,
                score,
                f"同花顺概念走势近 {lookback_days} 天表现 {month_return:.2f}%。",
                "同花顺概念走势",
                returns=returns,
                stock_names=stock_names,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{concept} 概念走势失败：{clean_text(str(exc))[:80]}")
            add_tag_candidate(
                buckets,
                concept,
                "概念",
                "中性热度",
                min(70.0, base_score + 12.0),
                "概念被近期材料或热门词命中，但走势暂未补齐。",
                "同花顺概念列表",
                stock_names=stock_names,
            )


def collect_xueqiu_hot_candidates(
    buckets: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    errors: list[str],
    stock_names: set[str],
) -> None:
    try:
        import akshare as ak  # type: ignore
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = ak.stock_hot_tweet_xq(symbol="最热门")
        rows = dataframe_records(frame, 80)
        examples = stock_examples_from_rows(rows[:12])
        add_tag_candidate(
            buckets,
            "雪球热议股",
            "论坛热度",
            "中性热度",
            min(88.0, 45.0 + len(rows) * 0.35),
            "雪球热门讨论榜集中出现。",
            "雪球热议",
            examples,
            {"样本数": len(rows)},
            stock_names,
        )
        sources.append({"name": "stock_hot_tweet_xq", "status": "ok", "message": f"读取 {len(rows)} 条", "count": len(rows)})
    except Exception as exc:  # noqa: BLE001
        message = f"雪球热议失败：{clean_text(str(exc))[:120]}"
        errors.append(message)
        sources.append({"name": "stock_hot_tweet_xq", "status": "error", "message": message, "count": 0})


def collect_eastmoney_hot_candidates(
    buckets: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    errors: list[str],
    stock_names: set[str],
) -> None:
    try:
        import akshare as ak  # type: ignore
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = ak.stock_hot_rank_em()
        rows = dataframe_records(frame, 60)
        examples = stock_examples_from_rows(rows[:10])
        add_tag_candidate(
            buckets,
            "东财热榜股",
            "论坛热度",
            "中性热度",
            min(82.0, 42.0 + len(rows) * 0.4),
            "东方财富热榜集中出现。",
            "东方财富热榜",
            examples,
            {"样本数": len(rows)},
            stock_names,
        )
        sources.append({"name": "stock_hot_rank_em", "status": "ok", "message": f"读取 {len(rows)} 条", "count": len(rows)})
    except Exception as exc:  # noqa: BLE001
        sources.append({"name": "stock_hot_rank_em", "status": "error", "message": clean_text(str(exc))[:120], "count": 0})


def fetch_ths_concept_returns(concept: str, lookback_days: int) -> dict[str, float]:
    import akshare as ak  # type: ignore

    end = datetime.now(BEIJING).date()
    start = end - timedelta(days=max(lookback_days + 10, 45))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        frame = ak.stock_board_concept_index_ths(
            symbol=concept,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    records = dataframe_records(frame, 120)
    closes = [parse_float(first_value(row, "收盘价", "close")) for row in records]
    closes = [value for value in closes if value is not None and value > 0]
    if len(closes) < 2:
        raise ValueError("概念走势不足")

    def ret(period: int) -> float:
        if len(closes) <= period:
            base = closes[0]
        else:
            base = closes[-period - 1]
        return round((closes[-1] / base - 1) * 100, 2) if base else 0.0

    return {
        "近5日": ret(5),
        "近10日": ret(10),
        "近20日": ret(20),
        "近30日": ret(30),
    }


def add_tag_candidate(
    buckets: dict[str, dict[str, Any]],
    name: str,
    category: str,
    direction: str,
    score: float,
    reason: str,
    source: str,
    examples: list[str] | None = None,
    returns: dict[str, Any] | None = None,
    stock_names: set[str] | None = None,
) -> None:
    normalized = normalize_candidate_name(name)
    if not normalized or normalized == UNTAGGED_NAME:
        return
    if stock_names and normalized in stock_names:
        return
    if normalized in FORBIDDEN_CANDIDATE_NAMES:
        return
    bucket = buckets.setdefault(
        normalized,
        {
            "name": normalized,
            "category": category,
            "direction": direction,
            "score": 0.0,
            "reasons": [],
            "sources": [],
            "example_stocks": [],
            "returns": {},
        },
    )
    bucket["score"] += max(0.0, float(score or 0))
    if bucket["direction"] == "中性热度" and direction != "中性热度":
        bucket["direction"] = direction
    if category == "概念" or bucket["category"] in {"论坛热度", "交易特征"}:
        bucket["category"] = category
    if reason and reason not in bucket["reasons"]:
        bucket["reasons"].append(clean_text(reason)[:120])
    if source and source not in bucket["sources"]:
        bucket["sources"].append(source)
    for example in examples or []:
        cleaned = clean_text(example)
        if cleaned and cleaned not in bucket["example_stocks"] and len(bucket["example_stocks"]) < 8:
            bucket["example_stocks"].append(cleaned)
    for key, value in (returns or {}).items():
        parsed = parse_float(value)
        bucket["returns"][str(key)] = round(parsed, 2) if parsed is not None else value


def finalize_tag_candidates(buckets: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for bucket in buckets.values():
        score = min(100.0, round(bucket["score"], 2))
        if score < 8:
            continue
        reasons = bucket.pop("reasons")
        result.append(
            {
                "name": bucket["name"],
                "category": bucket["category"],
                "direction": bucket["direction"],
                "score": score,
                "reason": reasons[0] if reasons else "近期市场热度候选。",
                "sources": bucket["sources"][:5],
                "example_stocks": bucket["example_stocks"][:8],
                "returns": bucket["returns"],
            }
        )
    result.sort(key=lambda item: item["score"], reverse=True)
    return result[:limit]


def stock_examples_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    examples: list[str] = []
    for row in rows:
        name = clean_text(first_value(row, "股票简称", "名称", "name") or "")
        code = clean_text(first_value(row, "股票代码", "代码", "code", "股票代码") or "")
        if not name:
            continue
        text = f"{name} {code}".strip()
        if text not in examples:
            examples.append(text)
        if len(examples) >= 8:
            break
    return examples


def hot_concept_seed_words() -> dict[str, float]:
    return {
        "AI": 18,
        "算力": 18,
        "光模块": 20,
        "CPO": 18,
        "PCB": 16,
        "半导体": 16,
        "芯片": 16,
        "机器人": 18,
        "创新药": 18,
        "医药": 12,
        "稳定币": 18,
        "数字货币": 12,
        "军工": 14,
        "低空": 14,
        "固态电池": 14,
        "新能源": 10,
        "稀土": 14,
        "有色": 12,
        "铜": 12,
        "电力": 10,
        "核聚变": 14,
        "脑机": 14,
        "消费电子": 12,
    }


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def normalize_candidate_name(value: str) -> str:
    cleaned = clean_text(value)
    cleaned = cleaned.replace("#", "").replace("　", " ").strip(" -_｜|/")
    if cleaned.endswith("概念") and len(cleaned) > 4:
        cleaned = cleaned[:-2]
    cleaned = " ".join(cleaned.split())
    if len(cleaned) < 2 or len(cleaned) > 24:
        return ""
    return cleaned


def tags_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, list[FactorTag]]:
    if not full_codes:
        return {}
    rows = db.execute(
        select(FactorStockTag.full_code, FactorTag)
        .join(FactorTag, FactorTag.id == FactorStockTag.tag_id)
        .where(FactorStockTag.full_code.in_(sorted(set(full_codes))))
        .order_by(FactorTag.name)
    ).all()
    result: dict[str, list[FactorTag]] = {}
    for full_code, tag in rows:
        result.setdefault(full_code, []).append(tag)
    return result


def parse_returns(value: str | None) -> dict[str, float]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    result: dict[str, float] = {}
    for key, raw in payload.items():
        parsed = parse_float(raw)
        if parsed is not None:
            result[str(key)] = parsed
    return result


def should_fetch_realtime_now(moment: datetime | None = None) -> bool:
    now = moment.astimezone(BEIJING) if moment else datetime.now(BEIJING)
    if now.weekday() >= 5:
        return False
    return time(9, 25) <= now.time() <= time(15, 10)


def should_fetch_history_now(db: Session, moment: datetime | None = None) -> bool:
    now = moment.astimezone(BEIJING) if moment else datetime.now(BEIJING)
    if now.weekday() >= 5 or now.time() < time(15, 30):
        return False
    latest_history = db.scalar(
        select(FactorRefreshRun)
        .where(FactorRefreshRun.message.like("%历史%"), FactorRefreshRun.status.in_(["ok", "partial_error"]))
        .order_by(desc(FactorRefreshRun.completed_at))
        .limit(1)
    )
    if not latest_history or not latest_history.completed_at:
        return True
    return latest_history.completed_at.astimezone(BEIJING).date() < now.date()


def refresh_factor_quotes(db: Session, include_spot: bool, include_history: bool) -> dict[str, Any]:
    ensure_factor_defaults(db)
    ensure_stock_universe(db)
    run = FactorRefreshRun(status="manual_only", message="市场赚钱因子刷新中", started_at=now_utc())
    db.add(run)
    db.commit()
    fetched_count = 0
    failed_count = 0
    messages: list[str] = []
    try:
        if include_spot:
            try:
                fetched_count += refresh_spot_quotes(db)
                messages.append("实时行情已更新")
            except Exception as exc:
                failed_count += 1
                messages.append(f"实时行情失败：{exc}")
        if include_history:
            try:
                fetched, failed = refresh_history_returns(db)
                fetched_count += fetched
                failed_count += failed
                history_message = f"历史涨幅已更新 {fetched} 只"
                if failed:
                    sample_errors = latest_factor_error_summaries(db)
                    if sample_errors:
                        history_message += f"，失败 {failed} 只：{'；'.join(sample_errors)}"
                    else:
                        history_message += f"，失败 {failed} 只"
                messages.append(history_message)
            except Exception as exc:
                failed_count += 1
                messages.append(f"历史涨幅失败：{exc}")
        cached_count = db.scalar(select(func.count(FactorQuoteSnapshot.id))) or 0
        if failed_count and fetched_count:
            status = "partial_error"
        elif failed_count and not fetched_count:
            status = "error"
        elif fetched_count:
            status = "ok"
        else:
            status = "manual_only"
            messages.append("当前时间未触发行情刷新")
        run.status = status
        run.message = "；".join(messages)
        run.fetched_count = fetched_count
        run.cached_count = cached_count
        run.failed_count = failed_count
        run.completed_at = now_utc()
        db.commit()
        return refresh_run_to_out(run) or {}
    except Exception as exc:
        run.status = "error"
        run.message = str(exc)
        run.failed_count = failed_count + 1
        run.cached_count = db.scalar(select(func.count(FactorQuoteSnapshot.id))) or 0
        run.completed_at = now_utc()
        db.commit()
        raise


def refresh_spot_quotes(db: Session) -> int:
    errors: list[str] = []
    for source in spot_source_order():
        try:
            df = fetch_spot_frame(source)
            count = save_spot_records(db, df.to_dict("records"))
            if count:
                return count
            errors.append(f"{source}: 实时行情为空")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {summarize_factor_error(str(exc)) or str(exc)[:120]}")
    raise ValueError("；".join(errors) or "实时行情获取失败")


def refresh_market_cap_snapshots(db: Session) -> int:
    errors: list[str] = []
    try:
        df = fetch_spot_frame("eastmoney")
        records = df.to_dict("records")
        return save_spot_records(db, records)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"东方财富：{summarize_factor_error(str(exc)) or clean_text(str(exc))[:120]}")
    try:
        count = refresh_tencent_market_cap_snapshots(db)
        if count:
            return count
        errors.append("腾讯行情：未返回有效市值")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"腾讯行情：{summarize_factor_error(str(exc)) or clean_text(str(exc))[:120]}")
    raise ValueError("；".join(errors))


def refresh_tencent_market_cap_snapshots(db: Session) -> int:
    stocks = list(db.scalars(select(AStock).order_by(AStock.exchange, AStock.code)).all())
    if not stocks:
        return 0
    today = datetime.now(BEIJING).date()
    count = 0
    batch_size = 80
    for index in range(0, len(stocks), batch_size):
        batch = stocks[index : index + batch_size]
        symbols = ",".join(tencent_symbol(stock) for stock in batch)
        if not symbols:
            continue
        url = f"https://qt.gtimg.cn/q={urllib.parse.quote(symbols, safe=',')}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        with urllib.request.urlopen(request, timeout=12) as response:  # noqa: S310
            payload = response.read().decode("gbk", "ignore")
        stock_by_code = {stock.code: stock for stock in batch}
        for line in payload.split(";"):
            line = line.strip()
            if not line or '="' not in line:
                continue
            raw = line.split('="', 1)[1].rsplit('"', 1)[0]
            parts = raw.split("~")
            if len(parts) <= 45:
                continue
            code = normalize_code(parts[2])
            stock = stock_by_code.get(code)
            if not stock:
                continue
            # Tencent quote fields: 44 is circulating market cap, 45 is total
            # market cap (both in CNY 100m).  Keep the semantic names aligned;
            # reversing them makes circulating value exceed total value.
            float_yi = parse_float(parts[44])
            total_yi = parse_float(parts[45])
            if total_yi is None and float_yi is None:
                continue
            cap_snapshot = db.scalar(select(FactorMarketCapSnapshot).where(FactorMarketCapSnapshot.full_code == stock.full_code))
            if not cap_snapshot:
                cap_snapshot = FactorMarketCapSnapshot(code=stock.code, name=stock.name, exchange=stock.exchange, full_code=stock.full_code)
                db.add(cap_snapshot)
            cap_snapshot.code = stock.code
            cap_snapshot.name = stock.name
            cap_snapshot.exchange = stock.exchange
            cap_snapshot.total_market_cap = total_yi * 100_000_000 if total_yi is not None else None
            cap_snapshot.float_market_cap = float_yi * 100_000_000 if float_yi is not None else None
            cap_snapshot.source = "tencent"
            cap_snapshot.trade_date = today
            count += 1
        db.commit()
    return count


def tencent_symbol(stock: AStock) -> str:
    prefix = stock.exchange.lower()
    if prefix not in {"sh", "sz", "bj"}:
        prefix = exchange_from_code(stock.code).lower()
    return f"{prefix}{stock.code}"


def fetch_spot_frame(source: str):
    import akshare as ak  # type: ignore

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if source == "sina":
            return ak.stock_zh_a_spot()
        return ak.stock_zh_a_spot_em()


def save_spot_records(db: Session, records: list[dict[str, Any]]) -> int:
    today = datetime.now(BEIJING).date()
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    count = 0
    for record in records:
        code = normalize_code(first_value(record, "代码", "code", "symbol"))
        name = clean_text(first_value(record, "名称", "name"))
        if not code or not name:
            continue
        exchange = exchange_from_code(code)
        full_code = f"{exchange}{code}"
        if full_code in excluded_codes:
            continue
        latest_price = parse_float(first_value(record, "最新价", "现价", "price", "latest_price"))
        change_pct = parse_float(first_value(record, "涨跌幅", "涨幅", "change_pct"))
        snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == full_code))
        if not snapshot:
            snapshot = FactorQuoteSnapshot(code=code, name=name, exchange=exchange, full_code=full_code)
            db.add(snapshot)
        snapshot.code = code
        snapshot.name = name
        snapshot.exchange = exchange
        snapshot.latest_price = latest_price
        snapshot.change_pct = change_pct
        snapshot.trade_date = today
        snapshot.source_status = "ok"
        snapshot.last_error = None
        returns = parse_returns(snapshot.returns_json)
        if change_pct is not None:
            returns["1"] = change_pct
        snapshot.returns_json = json.dumps(returns, ensure_ascii=False)
        total_market_cap = parse_float(first_value(record, "总市值", "总市值-元", "market_cap", "total_market_cap", "f20"))
        float_market_cap = parse_float(first_value(record, "流通市值", "流通市值-元", "float_market_cap", "circulating_market_cap", "f21"))
        if total_market_cap is not None or float_market_cap is not None:
            cap_snapshot = db.scalar(select(FactorMarketCapSnapshot).where(FactorMarketCapSnapshot.full_code == full_code))
            if not cap_snapshot:
                cap_snapshot = FactorMarketCapSnapshot(code=code, name=name, exchange=exchange, full_code=full_code)
                db.add(cap_snapshot)
            cap_snapshot.code = code
            cap_snapshot.name = name
            cap_snapshot.exchange = exchange
            cap_snapshot.total_market_cap = total_market_cap
            cap_snapshot.float_market_cap = float_market_cap
            cap_snapshot.source = "akshare"
            cap_snapshot.trade_date = today
        count += 1
    db.commit()
    return count


def refresh_history_returns(db: Session) -> tuple[int, int]:
    presets = list(db.scalars(select(FactorPeriodPreset).order_by(FactorPeriodPreset.sort_order, FactorPeriodPreset.id)))
    periods = sorted({period for preset in presets for period in parse_periods(preset.periods_json)})
    if not periods:
        return 0, 0
    max_period = max(periods)
    limit = history_batch_size()
    snapshots = list_history_refresh_batch(db, periods, limit)
    fetched = 0
    failed = 0
    for snapshot in snapshots:
        try:
            returns, latest_price, trade_date = fetch_history_returns(snapshot.code, periods, max_period)
            existing = parse_returns(snapshot.returns_json)
            existing.update(returns)
            snapshot.returns_json = json.dumps(existing, ensure_ascii=False)
            if latest_price is not None:
                snapshot.latest_price = latest_price
            if trade_date is not None:
                snapshot.trade_date = trade_date
            snapshot.source_status = "ok"
            snapshot.last_error = None
            fetched += 1
        except Exception as exc:
            snapshot.source_status = "error"
            snapshot.last_error = str(exc)[:1000]
            failed += 1
    db.commit()
    return fetched, failed


def list_history_refresh_batch(db: Session, periods: list[int], limit: int) -> list[FactorQuoteSnapshot]:
    excluded_codes = set(db.scalars(select(FactorExcludedStock.full_code)).all())
    missing_query = (
        select(FactorQuoteSnapshot)
        .where(FactorQuoteSnapshot.source_status != "error")
        .order_by(FactorQuoteSnapshot.updated_at.asc(), FactorQuoteSnapshot.id.asc())
        .limit(limit * 4)
    )
    if excluded_codes:
        missing_query = missing_query.where(~FactorQuoteSnapshot.full_code.in_(excluded_codes))
    missing_return_snapshots = [
        snapshot
        for snapshot in db.scalars(missing_query)
        if not has_all_period_returns(snapshot, periods)
    ][:limit]
    if len(missing_return_snapshots) >= limit:
        return missing_return_snapshots

    remaining = limit - len(missing_return_snapshots)
    existing_codes = select(FactorQuoteSnapshot.full_code)
    exchange_priority = case(
        (AStock.exchange == "SH", 0),
        (AStock.exchange == "SZ", 1),
        (AStock.exchange == "BJ", 2),
        else_=3,
    )
    stocks_query = (
        select(AStock)
        .where(~AStock.full_code.in_(existing_codes))
        .order_by(exchange_priority, AStock.full_code)
        .limit(remaining)
    )
    if excluded_codes:
        stocks_query = stocks_query.where(~AStock.full_code.in_(excluded_codes))
    stocks = list(db.scalars(stocks_query))
    created: list[FactorQuoteSnapshot] = []
    for stock in stocks:
        snapshot = FactorQuoteSnapshot(
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            full_code=stock.full_code,
            source_status="manual_only",
        )
        db.add(snapshot)
        created.append(snapshot)
    if created:
        db.commit()
        for snapshot in created:
            db.refresh(snapshot)
    batch = [*missing_return_snapshots, *created]
    if len(batch) >= limit:
        return batch[:limit]

    retry_limit = limit - len(batch)
    retry_query = (
        select(FactorQuoteSnapshot)
        .where(FactorQuoteSnapshot.source_status == "error")
        .order_by(FactorQuoteSnapshot.updated_at.asc(), FactorQuoteSnapshot.id.asc())
        .limit(retry_limit * 4)
    )
    if excluded_codes:
        retry_query = retry_query.where(~FactorQuoteSnapshot.full_code.in_(excluded_codes))
    retry_errors = [
        snapshot
        for snapshot in db.scalars(retry_query)
        if not has_all_period_returns(snapshot, periods)
    ][:retry_limit]
    return [*batch, *retry_errors]


def has_all_period_returns(snapshot: FactorQuoteSnapshot, periods: list[int]) -> bool:
    returns = parse_returns(snapshot.returns_json)
    return all(str(period) in returns for period in periods)


def latest_factor_error_summaries(db: Session, limit: int = 2) -> list[str]:
    rows = db.scalars(
        select(FactorQuoteSnapshot)
        .where(FactorQuoteSnapshot.last_error.is_not(None))
        .order_by(FactorQuoteSnapshot.updated_at.desc(), FactorQuoteSnapshot.id.desc())
        .limit(limit * 4)
    )
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        summary = summarize_factor_error(row.last_error or "")
        if not summary or summary in seen:
            continue
        seen.add(summary)
        result.append(summary)
        if len(result) >= limit:
            break
    return result


def summarize_factor_error(error: str) -> str:
    text = str(error)
    if not text:
        return ""
    if "push2his.eastmoney.com" in text:
        return "东方财富历史行情接口连接失败"
    if "Max retries exceeded" in text or "HTTPSConnectionPool" in text or "ProxyError" in text:
        return "行情源网络连接失败"
    if "历史行情不足" in text:
        return "部分股票历史行情不足"
    return text[:80]


def fetch_history_returns(code: str, periods: list[int], max_period: int) -> tuple[dict[str, float], float | None, date | None]:
    end = datetime.now(BEIJING).date()
    start = end - timedelta(days=max_period * 3 + 30)
    start_text = start.strftime("%Y%m%d")
    end_text = end.strftime("%Y%m%d")
    errors: list[str] = []
    for source in history_source_order():
        try:
            df = fetch_history_frame(source, code, start_text, end_text)
            return build_returns_from_history_records(df.to_dict("records"), periods, end)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {summarize_factor_error(str(exc)) or str(exc)[:120]}")
    raise ValueError("；".join(errors) or "历史行情获取失败")


def fetch_history_frame(source: str, code: str, start_date: str, end_date: str):
    import akshare as ak  # type: ignore

    symbol = history_market_symbol(code)
    if source == "tencent":
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
                timeout=history_timeout_seconds(),
            )
    if source == "sina":
        return ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date, adjust="qfq")
    return ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
        timeout=history_timeout_seconds(),
    )


def history_market_symbol(code: str) -> str:
    return f"{exchange_from_code(code).lower()}{code}"


def build_returns_from_history_records(records: list[dict[str, Any]], periods: list[int], end: date) -> tuple[dict[str, float], float | None, date | None]:
    closes: list[float] = []
    dates: list[date] = []
    for record in records:
        close = parse_float(first_value(record, "收盘", "close"))
        if close is None or close <= 0:
            continue
        closes.append(close)
        raw_date = first_value(record, "日期", "date")
        dates.append(parse_date(raw_date) or end)
    if len(closes) < 2:
        raise ValueError("历史行情不足")
    latest = closes[-1]
    result: dict[str, float] = {}
    for period in periods:
        if period == 1 and len(closes) >= 2:
            base = closes[-2]
        elif len(closes) > period:
            base = closes[-period - 1]
        else:
            continue
        if base:
            result[str(period)] = round((latest / base - 1) * 100, 4)
    return result, latest, dates[-1] if dates else None


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record.get(key)
    return None


def dataframe_records(frame: Any, limit: int = 100) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", False):
        return []
    try:
        return frame.head(limit).to_dict("records")
    except Exception:
        return []


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    code = "".join(ch for ch in text if ch.isdigit())
    return code[:6] if len(code) >= 6 else ""


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())

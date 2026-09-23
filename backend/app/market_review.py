from __future__ import annotations

import json
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import requests
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import ensure_external_path, get_data_root
from app.models import (
    MarketReviewAiSetting,
    MarketReviewMaterial,
    MarketReviewReport,
    MarketReviewUniverseItem,
    XueqiuPost,
    XueqiuWatchlistEvent,
    now_utc,
)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SOURCE_TIMEOUT_SECONDS = 8
DEFAULT_QUOTE_TIMEOUT_SECONDS = 8

A_INDEX_SECIDS = {
    "000001": "1.000001",
    "399001": "0.399001",
    "399006": "0.399006",
    "000688": "1.000688",
}

EASTMONEY_FIELDS = "f12,f13,f14,f2,f3,f4,f5,f6,f17,f18"

AI_PROVIDER_DEFAULTS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "model": "",
    },
}

DEFAULT_UNIVERSE = [
    ("A", "000001", "上证指数", "市场总览", "A股宽基指数", 10),
    ("A", "399001", "深证成指", "市场总览", "A股宽基指数", 20),
    ("A", "399006", "创业板指", "市场总览", "成长风格指数", 30),
    ("A", "000688", "科创50", "AI叙事", "半导体/硬科技指数", 40),
    ("A", "300308", "中际旭创", "AI叙事", "光模块龙头", 110),
    ("A", "300502", "新易盛", "AI叙事", "光模块龙头", 120),
    ("A", "300394", "天孚通信", "AI叙事", "光器件龙头", 130),
    ("A", "601138", "工业富联", "AI叙事", "AI服务器链", 140),
    ("A", "002371", "北方华创", "AI叙事", "半导体设备", 150),
    ("A", "688256", "寒武纪", "AI叙事", "AI芯片", 160),
    ("A", "300750", "宁德时代", "电力/储能", "储能链观察", 210),
    ("US", "NVDA", "NVIDIA", "AI叙事", "GPU/算力资本开支龙头", 110),
    ("US", "MSFT", "Microsoft", "AI叙事", "云资本开支", 120),
    ("US", "GOOGL", "Alphabet", "AI叙事", "云资本开支", 130),
    ("US", "AMZN", "Amazon", "AI叙事", "云资本开支", 140),
    ("US", "META", "Meta", "AI叙事", "AI应用/资本开支", 150),
    ("US", "AVGO", "Broadcom", "AI叙事", "ASIC/网络芯片", 160),
    ("US", "AMD", "AMD", "AI叙事", "GPU替代链", 170),
    ("US", "TSLA", "Tesla", "AI叙事", "机器人/自动驾驶", 180),
    ("US", "SMCI", "Super Micro", "AI叙事", "AI服务器", 190),
]

EDITORIAL_EVENT_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "突发事件",
        5,
        (
            "战争",
            "袭击",
            "冲突",
            "制裁",
            "禁令",
            "出口管制",
            "断供",
            "停产",
            "停工",
            "事故",
            "爆炸",
            "监管调查",
            "关税",
            "tariff",
            "sanction",
            "export control",
            "ban",
        ),
    ),
    (
        "第一次突破",
        5,
        (
            "首次",
            "首个",
            "第一",
            "突破",
            "新高",
            "历史新高",
            "量产",
            "试产",
            "投产",
            "交付",
            "认证",
            "获批",
            "批准",
            "first",
            "record high",
            "breakthrough",
            "approved",
        ),
    ),
    (
        "超预期数据",
        5,
        (
            "上调目标价",
            "下调目标价",
            "目标价",
            "评级上调",
            "上调评级",
            "指引",
            "超预期",
            "不及预期",
            "财报",
            "营收",
            "利润",
            "毛利率",
            "订单",
            "排产",
            "出货",
            "涨价",
            "提价",
            "库存",
            "price target",
            "upgrade",
            "guidance",
            "earnings",
            "revenue",
            "margin",
            "order",
            "shipment",
        ),
    ),
    (
        "产业确认",
        4,
        (
            "签署",
            "合同",
            "协议",
            "合作",
            "供应",
            "采购",
            "客户",
            "资本开支",
            "数据中心",
            "云厂商",
            "服务器",
            "光模块",
            "半导体设备",
            "液冷",
            "电力",
            "capex",
            "data center",
            "hyperscaler",
            "supply",
            "contract",
            "partnership",
        ),
    ),
    (
        "价格信号",
        4,
        (
            "报价",
            "价格上涨",
            "价格下跌",
            "合约价",
            "现货价",
            "现货",
            "dram",
            "nand",
            "hbm",
            "ssd",
            "存储",
            "内存",
            "闪存",
            "铜价",
            "电价",
            "memory price",
        ),
    ),
    (
        "资金映射",
        2,
        (
            "回购",
            "增持",
            "减持",
            "定增",
            "融资",
            "机构买入",
            "etf",
            "buyback",
            "stake",
        ),
    ),
)

EDITORIAL_HARD_LABELS = {"突发事件", "第一次突破", "超预期数据", "产业确认", "价格信号"}

PURE_MARKET_MOVE_HINTS = (
    "涨超",
    "涨幅",
    "大涨",
    "飙升",
    "拉升",
    "领涨",
    "收涨",
    "盘前涨",
    "盘后涨",
    "跌超",
    "大跌",
    "下跌",
    "创新高",
    "创历史新高",
    "shares rise",
    "shares gain",
    "stock rises",
    "stock gains",
)

NEWS_NOISE_HINTS = (
    "一图看懂",
    "隔夜全球要闻",
    "盘前必读",
    "午间公告",
    "早报",
    "收评",
    "快讯汇总",
    "市场消息",
    "据媒体",
    "据悉",
    "传闻",
    "rumor",
)

EXPLANATORY_CATALYST_HINTS = (
    "上调目标价",
    "下调目标价",
    "目标价",
    "评级上调",
    "上调评级",
    "指引",
    "超预期",
    "财报",
    "营收",
    "利润",
    "毛利率",
    "订单",
    "排产",
    "出货",
    "涨价",
    "提价",
    "报价",
    "价格上涨",
    "价格下跌",
    "合约价",
    "现货价",
    "签署",
    "合同",
    "协议",
    "合作",
    "供应",
    "采购",
    "客户",
    "资本开支",
    "数据中心",
    "量产",
    "试产",
    "投产",
    "交付",
    "认证",
    "获批",
    "批准",
    "出口管制",
    "制裁",
    "禁令",
    "停产",
    "断供",
    "price target",
    "upgrade",
    "guidance",
    "earnings",
    "revenue",
    "margin",
    "order",
    "shipment",
    "contract",
    "supply",
    "capex",
    "memory price",
)

NEWS_TOPIC_KEYWORDS = (
    "闪迪",
    "sandisk",
    "sndk",
    "美光",
    "micron",
    "mu",
    "西部数据",
    "western digital",
    "wdc",
    "英伟达",
    "nvidia",
    "nvda",
    "博通",
    "broadcom",
    "avgo",
    "amd",
    "台积电",
    "tsmc",
    "中际旭创",
    "新易盛",
    "天孚通信",
    "工业富联",
    "北方华创",
    "寒武纪",
    "宁德时代",
    "dram",
    "nand",
    "hbm",
    "ssd",
    "光模块",
    "数据中心",
    "服务器",
    "半导体",
    "电力",
    "液冷",
)

NEWS_TOPIC_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sandisk", ("闪迪", "sandisk", "sndk")),
    ("micron", ("美光", "micron", "mu")),
    ("western_digital", ("西部数据", "western digital", "wdc")),
    ("nvidia", ("英伟达", "nvidia", "nvda")),
    ("broadcom", ("博通", "broadcom", "avgo")),
    ("tsmc", ("台积电", "tsmc")),
    ("memory", ("dram", "nand", "hbm", "ssd", "存储", "内存", "闪存")),
    ("optical_module", ("光模块", "中际旭创", "新易盛", "天孚通信")),
    ("ai_server", ("服务器", "工业富联", "super micro", "smci")),
    ("data_center", ("数据中心", "电力", "液冷")),
)

EDITORIAL_NEWS_SOURCE_PREFIXES = ("akshare:全球财经快讯",)


@dataclass(frozen=True)
class SourceStatus:
    source: str
    status: str
    message: str
    count: int = 0


def today_beijing() -> date:
    return datetime.now(BEIJING_TZ).date()


def parse_report_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(value)
    return today_beijing()


def report_root() -> Path:
    configured = os.environ.get("MARKET_REVIEW_REPORT_DIR")
    if configured:
        root = Path(configured).expanduser()
    else:
        root = get_data_root() / "reports" / "market-review"
    ensure_external_path(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_default_universe(db: Session) -> None:
    existing = {
        (item.market, item.symbol)
        for item in db.scalars(select(MarketReviewUniverseItem)).all()
    }
    changed = False
    for market, symbol, name, theme, role, sort_order in DEFAULT_UNIVERSE:
        if (market, symbol) in existing:
            continue
        db.add(
            MarketReviewUniverseItem(
                market=market,
                symbol=symbol,
                name=name,
                theme=theme,
                role=role,
                enabled=True,
                sort_order=sort_order,
            )
        )
        changed = True
    if changed:
        db.commit()


def material_to_dict(material: MarketReviewMaterial) -> dict[str, Any]:
    return {
        "id": material.id,
        "report_date": material.report_date.isoformat(),
        "source_type": material.source_type,
        "source_name": material.source_name,
        "market": material.market,
        "theme": material.theme,
        "title": material.title,
        "content": material.content,
        "url": material.url,
        "status": material.status,
        "importance": material.importance,
        "created_at": material.created_at,
        "updated_at": material.updated_at,
    }


def universe_to_dict(item: MarketReviewUniverseItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "market": item.market,
        "symbol": item.symbol,
        "name": item.name,
        "theme": item.theme,
        "role": item.role,
        "enabled": item.enabled,
        "sort_order": item.sort_order,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def report_to_dict(report: MarketReviewReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "period": report.period,
        "report_date": report.report_date.isoformat(),
        "start_date": report.start_date.isoformat(),
        "end_date": report.end_date.isoformat(),
        "title": report.title,
        "status": report.status,
        "markdown_path": report.markdown_path,
        "content": report.content,
        "ai_status": report.ai_status,
        "source_status": json.loads(report.source_status_json or "[]"),
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


def normalize_ai_provider(provider: str | None) -> str:
    value = (provider or "deepseek").strip().lower()
    return value if value in AI_PROVIDER_DEFAULTS else "custom"


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def get_market_review_ai_settings(db: Session) -> MarketReviewAiSetting:
    settings = db.get(MarketReviewAiSetting, 1)
    if settings:
        return settings
    defaults = AI_PROVIDER_DEFAULTS["deepseek"]
    settings = MarketReviewAiSetting(
        id=1,
        provider="deepseek",
        model=defaults["model"],
        base_url=defaults["base_url"],
        enabled=True,
    )
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def effective_ai_config(db: Session) -> dict[str, str] | None:
    settings = get_market_review_ai_settings(db)
    if settings.enabled and settings.api_key and settings.model.strip() and settings.base_url.strip():
        return {
            "provider": settings.provider,
            "model": settings.model.strip(),
            "base_url": settings.base_url.strip(),
            "api_key": settings.api_key,
        }
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if deepseek_key:
        defaults = AI_PROVIDER_DEFAULTS["deepseek"]
        return {
            "provider": "deepseek",
            "model": os.environ.get("DEEPSEEK_MODEL", defaults["model"]),
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", defaults["base_url"]),
            "api_key": deepseek_key,
        }
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        defaults = AI_PROVIDER_DEFAULTS["openai"]
        return {
            "provider": "openai",
            "model": os.environ.get("OPENAI_MODEL", defaults["model"]),
            "base_url": os.environ.get("OPENAI_BASE_URL", defaults["base_url"]),
            "api_key": openai_key,
        }
    return None


def configured_ai_status(db: Session) -> str:
    return "ok" if effective_ai_config(db) else "not_configured"


def ai_settings_to_dict(db: Session) -> dict[str, Any]:
    settings = get_market_review_ai_settings(db)
    effective = effective_ai_config(db)
    return {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "enabled": settings.enabled,
        "api_key_configured": bool(settings.api_key),
        "api_key_masked": mask_secret(settings.api_key),
        "status": "ok" if effective else "not_configured",
        "effective_provider": effective["provider"] if effective else None,
    }


def update_ai_settings(db: Session, payload: dict[str, Any]) -> MarketReviewAiSetting:
    settings = get_market_review_ai_settings(db)
    provider = payload.get("provider")
    if provider is not None:
        normalized = normalize_ai_provider(provider)
        if normalized != settings.provider:
            defaults = AI_PROVIDER_DEFAULTS[normalized]
            settings.provider = normalized
            if "model" not in payload:
                settings.model = defaults["model"]
            if "base_url" not in payload:
                settings.base_url = defaults["base_url"]
    if payload.get("clear_api_key"):
        settings.api_key = None
    if "api_key" in payload and payload.get("api_key"):
        settings.api_key = str(payload["api_key"]).strip()
    if "model" in payload and payload.get("model") is not None:
        settings.model = str(payload["model"]).strip()
    if "base_url" in payload and payload.get("base_url") is not None:
        settings.base_url = str(payload["base_url"]).strip()
    if "enabled" in payload and payload.get("enabled") is not None:
        settings.enabled = bool(payload["enabled"])
    settings.updated_at = now_utc()
    db.commit()
    db.refresh(settings)
    return settings


def chat_completions_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def market_review_overview(db: Session) -> dict[str, Any]:
    ensure_default_universe(db)
    latest_report = db.scalar(select(MarketReviewReport).order_by(desc(MarketReviewReport.updated_at)).limit(1))
    reports = list(db.scalars(select(MarketReviewReport).order_by(desc(MarketReviewReport.updated_at)).limit(30)))
    materials = list(
        db.scalars(select(MarketReviewMaterial).order_by(desc(MarketReviewMaterial.created_at)).limit(80))
    )
    universe = list(
        db.scalars(
            select(MarketReviewUniverseItem).order_by(
                MarketReviewUniverseItem.market,
                MarketReviewUniverseItem.sort_order,
                MarketReviewUniverseItem.id,
            )
        )
    )
    status = latest_report.status if latest_report else "manual_only"
    return {
        "status": status,
        "source_status": status,
        "ai_status": configured_ai_status(db),
        "ai_settings": ai_settings_to_dict(db),
        "report_root": str(report_root()),
        "message": "每日 08:30 自动生成 A股+美股合并草稿；周报手动生成。",
        "latest_report": report_to_dict(latest_report) if latest_report else None,
        "reports": [report_to_dict(report) for report in reports],
        "materials": [material_to_dict(material) for material in materials],
        "universe": [universe_to_dict(item) for item in universe],
    }


def run_with_timeout(name: str, fn: Callable[[], Any], timeout_seconds: int | None = None) -> tuple[str, Any, str | None]:
    timeout = timeout_seconds or int(os.environ.get("MARKET_REVIEW_SOURCE_TIMEOUT_SECONDS", DEFAULT_SOURCE_TIMEOUT_SECONDS))
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return "ok", future.result(timeout=timeout), None
    except FutureTimeout:
        future.cancel()
        return "error", None, f"{name} 超过 {timeout} 秒未返回"
    except Exception as exc:
        message = str(exc).replace("\n", " ")
        if len(message) > 280:
            message = f"{message[:280]}..."
        return "error", None, message
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def add_auto_material(
    db: Session,
    report_date: date,
    source_name: str,
    market: str,
    theme: str,
    title: str,
    content: str,
    url: str | None = None,
    importance: int = 3,
) -> bool:
    title = title.strip()
    content = content.strip()
    if not title or not content:
        return False
    existing = db.scalar(
        select(MarketReviewMaterial).where(
            MarketReviewMaterial.report_date == report_date,
            MarketReviewMaterial.source_type == "auto",
            MarketReviewMaterial.source_name == source_name,
            MarketReviewMaterial.title == title,
        )
    )
    if existing:
        existing.content = content
        existing.url = url
        existing.market = market
        existing.theme = theme
        existing.importance = importance
        existing.updated_at = now_utc()
        return False
    db.add(
        MarketReviewMaterial(
            report_date=report_date,
            source_type="auto",
            source_name=source_name,
            market=market,
            theme=theme,
            title=title,
            content=content,
            url=url,
            status="ok",
            importance=importance,
        )
    )
    return True


def normalized_news_text(value: str | None) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.lower()
    text = re.sub(r"\b20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b", " ", text)
    text = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)
    text = re.sub(r"[【】\[\]（）()「」\"'“”‘’,，。！？!?；;：:、|｜/\\_\-·•]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_url_key(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(str(url).strip())
    except Exception:
        return ""
    if not parsed.netloc and not parsed.path:
        return ""
    path = re.sub(r"/+$", "", parsed.path or "")
    return f"{parsed.netloc.lower()}{path.lower()}"


def event_hits(text: str) -> list[tuple[str, list[str]]]:
    hits: list[tuple[str, list[str]]] = []
    for label, _weight, keywords in EDITORIAL_EVENT_RULES:
        matched = [keyword for keyword in keywords if keyword.lower() in text]
        if matched:
            hits.append((label, matched[:4]))
    return hits


def editorial_news_selection(title: str, content: str, impact_level: str | None = None) -> dict[str, Any]:
    text = normalized_news_text(f"{title} {content}")
    matches = event_hits(text)
    labels = [label for label, _matched in matches]
    score = 0
    reasons = []
    for label, matched in matches:
        weight = next(weight for rule_label, weight, _keywords in EDITORIAL_EVENT_RULES if rule_label == label)
        score += weight + min(max(len(matched) - 1, 0), 2)
        reasons.append(f"{label}:{'、'.join(matched[:3])}")
    if impact_level == "高":
        score += 2
    elif impact_level == "中":
        score += 1

    pure_move = any(hint.lower() in text for hint in PURE_MARKET_MOVE_HINTS)
    has_hard_label = any(label in EDITORIAL_HARD_LABELS for label in labels)
    has_explanatory_catalyst = any(hint.lower() in text for hint in EXPLANATORY_CATALYST_HINTS)
    noise_count = sum(1 for hint in NEWS_NOISE_HINTS if hint.lower() in text)
    if noise_count:
        score -= min(noise_count * 2, 4)

    if pure_move and (not has_hard_label or not has_explanatory_catalyst):
        return {
            "keep": False,
            "score": score,
            "strength": "弱",
            "labels": ["纯行情"],
            "reason": "只有涨跌、创新高或盘中表现，没有可验证催化。",
        }

    keep = score >= 5 and has_hard_label
    strength = "强" if score >= 8 else "中" if score >= 5 else "弱"
    return {
        "keep": keep,
        "score": score,
        "strength": strength,
        "labels": labels,
        "reason": "；".join(reasons) if reasons else "缺少新增事实、首次突破、超预期数据、产业确认或价格信号。",
    }


def news_market_hint(title: str, content: str, fallback: str = "both") -> str:
    text = normalized_news_text(f"{title} {content}")
    us_hits = ("美股", "纳斯达克", "标普", "道指", "nasdaq", "s&p", "dow", "nyse")
    a_hits = ("a股", "沪深", "上证", "深证", "创业板", "科创", "北向")
    has_us = any(hit in text for hit in us_hits)
    has_a = any(hit in text for hit in a_hits)
    if has_us and not has_a:
        return "US"
    if has_a and not has_us:
        return "A"
    return fallback


def editorial_news_content(content: str, selection: dict[str, Any]) -> str:
    body = str(content or "").strip()
    labels = "、".join(selection.get("labels") or ["待确认"])
    reason = str(selection.get("reason") or "待验证")
    return (
        f"编辑判断：{selection.get('strength', '中')}；新闻类型：{labels}。\n"
        f"入选原因：{reason}\n\n"
        f"原文摘要：{body[:1600]}"
    ).strip()


def news_title_signature(title: str) -> str:
    text = normalized_news_text(title)
    text = re.sub(r"\b(快讯|消息|独家|财联社|证券时报|华尔街见闻|路透|彭博|报道称|表示)\b", " ", text)
    return re.sub(r"\s+", "", text)[:90]


def news_topic_key(title: str, content: str) -> str:
    text = normalized_news_text(f"{title} {content}")
    labels = [label for label, _matched in event_hits(text) if label in EDITORIAL_HARD_LABELS]
    topics: list[str] = []
    alias_terms = {alias.lower() for _canonical, aliases in NEWS_TOPIC_ALIASES for alias in aliases}
    for canonical, aliases in NEWS_TOPIC_ALIASES:
        if any(alias.lower() in text for alias in aliases) and canonical not in topics:
            topics.append(canonical)
    for keyword in NEWS_TOPIC_KEYWORDS:
        normalized_keyword = keyword.lower()
        if normalized_keyword in alias_terms:
            continue
        if normalized_keyword in text and normalized_keyword not in topics:
            topics.append(normalized_keyword)
    if labels and topics:
        return "|".join(sorted(set(labels[:3] + topics[:4])))
    return ""


def editorial_dedupe_keys(title: str, content: str, url: str | None) -> set[str]:
    keys: set[str] = set()
    url_key = compact_url_key(url)
    if url_key:
        keys.add(f"url:{url_key}")
    signature = news_title_signature(title)
    if signature:
        keys.add(f"title:{signature}")
    topic_key = news_topic_key(title, content)
    if topic_key:
        keys.add(f"topic:{topic_key}")
    if not keys:
        raw = normalized_news_text(f"{title} {content}")[:180]
        keys.add(f"hash:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}")
    return keys


def similar_news_title(left: str, right: str) -> bool:
    left_signature = news_title_signature(left)
    right_signature = news_title_signature(right)
    if not left_signature or not right_signature:
        return False
    shorter, longer = sorted((left_signature, right_signature), key=len)
    if len(shorter) >= 18 and shorter in longer:
        return True
    return SequenceMatcher(None, left_signature, right_signature).ratio() >= 0.86


def duplicate_editorial_material(
    db: Session,
    report_date: date,
    title: str,
    content: str,
    url: str | None,
    seen_keys: set[str] | None = None,
) -> tuple[str, MarketReviewMaterial | None]:
    keys = editorial_dedupe_keys(title, content, url)
    if seen_keys is not None and keys & seen_keys:
        return "seen", None
    rows = list(
        db.scalars(
            select(MarketReviewMaterial).where(
                MarketReviewMaterial.report_date == report_date,
                MarketReviewMaterial.source_type == "auto",
            )
        )
    )
    for row in rows:
        row_keys = editorial_dedupe_keys(row.title, row.content, row.url)
        if keys & row_keys or similar_news_title(title, row.title):
            return "stored", row
    return "none", None


def should_replace_editorial_material(existing: MarketReviewMaterial, content: str, importance: int) -> bool:
    if importance > existing.importance:
        return True
    if importance == existing.importance and len(content) > len(existing.content) + 80:
        return True
    return False


def add_editorial_material(
    db: Session,
    report_date: date,
    source_name: str,
    market: str,
    theme: str,
    title: str,
    content: str,
    url: str | None = None,
    importance: int = 3,
    selection: dict[str, Any] | None = None,
    seen_keys: set[str] | None = None,
) -> str:
    selection = selection or editorial_news_selection(title, content)
    if not selection.get("keep"):
        return "skipped"
    content = editorial_news_content(content, selection)
    duplicate_kind, existing = duplicate_editorial_material(db, report_date, title, content, url, seen_keys)
    keys = editorial_dedupe_keys(title, content, url)
    if seen_keys is not None:
        seen_keys.update(keys)
    if duplicate_kind == "seen":
        return "duplicate"
    if existing:
        if should_replace_editorial_material(existing, content, importance):
            existing.source_name = source_name
            existing.market = market
            existing.theme = theme
            existing.title = title[:220]
            existing.content = content
            existing.url = url
            existing.importance = importance
            existing.updated_at = now_utc()
        return "duplicate"
    added = add_auto_material(
        db,
        report_date,
        source_name,
        market,
        theme,
        title[:220],
        content,
        url,
        importance=importance,
    )
    return "added" if added else "duplicate"


def clear_auto_quote_materials(db: Session, report_date: date, market: str) -> None:
    source_names = {
        "行情:A股",
        "行情:美股",
        "sina:A股行情",
        "nasdaq:美股行情",
        "eastmoney:A股行情",
        "eastmoney:美股行情",
        "tencent:A股行情",
        "tencent:美股行情",
    }
    rows = list(
        db.scalars(
            select(MarketReviewMaterial).where(
                MarketReviewMaterial.report_date == report_date,
                MarketReviewMaterial.source_type == "auto",
                MarketReviewMaterial.market == market,
                MarketReviewMaterial.source_name.in_(source_names),
            )
        )
    )
    for row in rows:
        db.delete(row)
    if rows:
        db.flush()


def dataframe_records(df: Any, limit: int = 80) -> list[dict[str, Any]]:
    if df is None:
        return []
    try:
        return df.head(limit).to_dict("records")
    except Exception:
        return []


def first_value(row: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def quote_timeout_seconds() -> int:
    return int(os.environ.get("MARKET_REVIEW_QUOTE_TIMEOUT_SECONDS", DEFAULT_QUOTE_TIMEOUT_SECONDS))


def format_quote_number(value: Any, percent: bool = False, amount: bool = False) -> str:
    if value in (None, "", "-"):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if percent:
        return f"{number:.2f}%"
    if amount:
        abs_number = abs(number)
        if abs_number >= 1_0000_0000_0000:
            return f"{number / 1_0000_0000_0000:.2f}万亿"
        if abs_number >= 1_0000_0000:
            return f"{number / 1_0000_0000:.2f}亿"
        if abs_number >= 1_0000:
            return f"{number / 1_0000:.2f}万"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


def eastmoney_get(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    url = f"https://push2.eastmoney.com{endpoint}"
    response = session.get(
        url,
        params=params,
        timeout=quote_timeout_seconds(),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("rc") not in (0, None):
        raise ValueError(f"Eastmoney 返回异常 rc={payload.get('rc')}")
    return payload


def eastmoney_diff(payload: dict[str, Any]) -> list[dict[str, Any]]:
    diff = (payload.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        return list(diff.values())
    if isinstance(diff, list):
        return diff
    return []


def chunked(values: list[str], size: int = 60) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def eastmoney_a_secid(item: MarketReviewUniverseItem) -> str:
    symbol = item.symbol.strip().upper()
    if symbol in A_INDEX_SECIDS:
        return A_INDEX_SECIDS[symbol]
    market_code = "1" if symbol.startswith(("5", "6", "9")) else "0"
    return f"{market_code}.{symbol}"


def sina_a_code(item: MarketReviewUniverseItem) -> str:
    symbol = item.symbol.strip().upper()
    if symbol in A_INDEX_SECIDS:
        market_code = A_INDEX_SECIDS[symbol].split(".", 1)[0]
        prefix = "sh" if market_code == "1" else "sz"
        return f"{prefix}{symbol.lower()}"
    prefix = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{symbol.lower()}"


def parse_numeric(value: Any) -> float | None:
    if value in (None, "", "-", "--", "N/A"):
        return None
    text = str(value).replace(",", "").replace("$", "").replace("%", "").replace("+", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def quote_row_from_sina(parts: list[str], symbol: str) -> dict[str, Any]:
    latest = parse_numeric(parts[3] if len(parts) > 3 else None)
    previous_close = parse_numeric(parts[2] if len(parts) > 2 else None)
    change = latest - previous_close if latest is not None and previous_close not in (None, 0) else None
    change_pct = change / previous_close * 100 if change is not None and previous_close not in (None, 0) else None
    return {
        "f12": symbol,
        "f14": parts[0] if parts else symbol,
        "f2": latest,
        "f3": change_pct,
        "f4": change,
        "f5": parse_numeric(parts[8] if len(parts) > 8 else None),
        "f6": parse_numeric(parts[9] if len(parts) > 9 else None),
        "f17": parse_numeric(parts[1] if len(parts) > 1 else None),
        "f18": previous_close,
    }


def quote_row_from_tencent(parts: list[str], symbol: str) -> dict[str, Any]:
    latest = parse_numeric(parts[3] if len(parts) > 3 else None)
    previous_close = parse_numeric(parts[4] if len(parts) > 4 else None)
    change = parse_numeric(parts[31] if len(parts) > 31 else None)
    change_pct = parse_numeric(parts[32] if len(parts) > 32 else None)
    amount = None
    compact = parts[35] if len(parts) > 35 else ""
    compact_parts = compact.split("/") if "/" in compact else []
    if len(compact_parts) >= 3:
        amount = parse_numeric(compact_parts[2])
    if amount is None:
        amount = parse_numeric(parts[37] if len(parts) > 37 else None)
    return {
        "f12": symbol,
        "f14": parts[1] if len(parts) > 1 and parts[1] else symbol,
        "f2": latest,
        "f3": change_pct,
        "f4": change,
        "f5": parse_numeric(parts[36] if len(parts) > 36 else parts[6] if len(parts) > 6 else None),
        "f6": amount,
        "f17": parse_numeric(parts[5] if len(parts) > 5 else None),
        "f18": previous_close,
    }


def sina_get_a_quotes(codes: list[str], code_to_item: dict[str, MarketReviewUniverseItem]) -> dict[str, dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://hq.sinajs.cn/list=" + ",".join(codes),
        timeout=quote_timeout_seconds(),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        },
    )
    response.raise_for_status()
    text = response.content.decode("gb18030", errors="ignore")
    rows: dict[str, dict[str, Any]] = {}
    for code, body in re.findall(r"var hq_str_([^=]+)=\"([^\"]*)\";", text):
        item = code_to_item.get(code)
        if not item:
            continue
        parts = body.split(",")
        if not parts or not parts[0]:
            continue
        rows[code] = quote_row_from_sina(parts, item.symbol)
    return rows


def tencent_get_quotes(codes: list[str], code_to_item: dict[str, MarketReviewUniverseItem]) -> dict[str, dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://qt.gtimg.cn/q=" + ",".join(codes),
        timeout=quote_timeout_seconds(),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    )
    response.raise_for_status()
    text = response.content.decode("gb18030", errors="ignore")
    rows: dict[str, dict[str, Any]] = {}
    for code, body in re.findall(r"v_([^=]+)=\"([^\"]*)\";", text):
        item = code_to_item.get(code)
        if not item:
            continue
        parts = body.split("~")
        if len(parts) < 5 or body == "1":
            continue
        rows[code] = quote_row_from_tencent(parts, item.symbol)
    return rows


def fetch_quotes_with_backup(
    primary_name: str,
    backup_name: str,
    code_to_item: dict[str, MarketReviewUniverseItem],
    primary_fetch: Callable[[list[str]], dict[str, dict[str, Any]]],
    backup_fetch: Callable[[list[str]], dict[str, dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], str, str | None]:
    rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    used_sources: list[str] = []
    try:
        for codes in chunked(list(code_to_item), 80):
            rows.update(primary_fetch(codes))
        used_sources.append(primary_name)
    except Exception as exc:
        errors.append(f"{primary_name}失败：{str(exc)[:160]}")
    missing = [code for code in code_to_item if code not in rows]
    if missing:
        try:
            for codes in chunked(missing, 80):
                rows.update(backup_fetch(codes))
            used_sources.append(backup_name)
        except Exception as exc:
            errors.append(f"{backup_name}失败：{str(exc)[:160]}")
    if not rows and errors:
        raise RuntimeError("；".join(errors))
    note = None
    if errors:
        note = "；".join(errors)
        if backup_name in used_sources:
            note += f"；已用{backup_name}补齐"
    elif len(used_sources) > 1:
        note = f"{primary_name}缺失部分标的，{backup_name}已补齐"
    return rows, "+".join(dict.fromkeys(used_sources)), note


def nasdaq_get_quote(symbol: str) -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        f"https://api.nasdaq.com/api/quote/{symbol}/info",
        params={"assetclass": "stocks"},
        timeout=quote_timeout_seconds(),
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.nasdaq.com",
            "Referer": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}",
        },
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    quote = data.get("secondaryData") or data.get("primaryData") or {}
    if not quote.get("lastSalePrice"):
        quote = data.get("primaryData") or quote
    latest = parse_numeric(quote.get("lastSalePrice"))
    change = parse_numeric(quote.get("netChange"))
    change_pct = parse_numeric(quote.get("percentageChange"))
    return {
        "f12": symbol.upper(),
        "f14": data.get("companyName") or symbol.upper(),
        "f2": latest,
        "f3": change_pct,
        "f4": change,
        "f5": parse_numeric(quote.get("volume")),
        "f6": None,
        "f17": None,
        "f18": latest - change if latest is not None and change is not None else None,
    }


def nasdaq_get_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    return {symbol: nasdaq_get_quote(symbol) for symbol in symbols}


def tencent_get_us_quotes(symbols: list[str], symbol_to_item: dict[str, MarketReviewUniverseItem]) -> dict[str, dict[str, Any]]:
    code_to_item = {f"us{symbol}": item for symbol, item in symbol_to_item.items()}
    rows_by_code = tencent_get_quotes(list(code_to_item), code_to_item)
    rows: dict[str, dict[str, Any]] = {}
    for code, row in rows_by_code.items():
        symbol = code.removeprefix("us").upper()
        rows[symbol] = row
    return rows


def quote_content(row: dict[str, Any], symbol: str | None = None) -> str:
    code = symbol or str(row.get("f12") or "-")
    return (
        f"代码 {code}，最新价 {format_quote_number(row.get('f2'))}，"
        f"涨跌幅 {format_quote_number(row.get('f3'), percent=True)}，"
        f"涨跌额 {format_quote_number(row.get('f4'))}。"
    )


def collect_a_quotes(db: Session, report_date: date) -> SourceStatus:
    enabled = list(
        db.scalars(
            select(MarketReviewUniverseItem).where(
                MarketReviewUniverseItem.market == "A",
                MarketReviewUniverseItem.enabled.is_(True),
            )
        )
    )
    if not enabled:
        return SourceStatus("A股行情", "not_configured", "没有启用的 A股标的", 0)
    code_to_item = {sina_a_code(item): item for item in enabled}

    def fetch() -> tuple[dict[str, dict[str, Any]], str, str | None]:
        return fetch_quotes_with_backup(
            "新浪行情",
            "腾讯行情",
            code_to_item,
            lambda codes: sina_get_a_quotes(codes, code_to_item),
            lambda codes: tencent_get_quotes(codes, code_to_item),
        )

    status, rows, error = run_with_timeout("A股行情", fetch, quote_timeout_seconds() + 3)
    if status != "ok":
        return SourceStatus("A股行情", "error", error or "A股行情获取失败")
    rows, data_source, note = rows
    clear_auto_quote_materials(db, report_date, "A")
    count = 0
    missing = set(code_to_item) - set(rows)
    for code, row in rows.items():
        item = code_to_item[code]
        title = f"{item.name}（{item.symbol}）A股行情"
        add_auto_material(
            db,
            report_date,
            "sina:A股行情",
            "A",
            item.theme or "市场总览",
            title,
            f"{quote_content(row, item.symbol)} 数据源：{data_source}。",
            importance=4,
        )
        count += 1
    message = f"处理 {count} 条 A股行情材料"
    if missing:
        message += f"，缺失 {len(missing)} 个标的"
    if note:
        message += f"；{note}"
    return SourceStatus("A股行情", "ok", message, count)


def collect_us_quotes(db: Session, report_date: date) -> SourceStatus:
    enabled = list(
        db.scalars(
            select(MarketReviewUniverseItem).where(
                MarketReviewUniverseItem.market == "US",
                MarketReviewUniverseItem.enabled.is_(True),
            )
        )
    )
    if not enabled:
        return SourceStatus("美股行情", "not_configured", "没有启用的美股标的", 0)
    symbol_to_item = {item.symbol.strip().upper(): item for item in enabled}

    def fetch() -> tuple[dict[str, dict[str, Any]], str, str | None]:
        return fetch_quotes_with_backup(
            "Nasdaq",
            "腾讯实时行情",
            symbol_to_item,
            nasdaq_get_quotes,
            lambda symbols: tencent_get_us_quotes(symbols, symbol_to_item),
        )

    status, rows, error = run_with_timeout(
        "美股行情",
        fetch,
        max(20, quote_timeout_seconds() * max(1, len(symbol_to_item))),
    )
    if status != "ok":
        return SourceStatus("美股行情", "error", error or "美股行情获取失败")
    rows, data_source, note = rows
    clear_auto_quote_materials(db, report_date, "US")
    count = 0
    missing = set(symbol_to_item)
    for symbol, row in rows.items():
        item = symbol_to_item.get(symbol)
        if not item:
            continue
        missing.discard(symbol)
        title = f"{item.name}（{item.symbol}）美股行情"
        add_auto_material(
            db,
            report_date,
            "nasdaq:美股行情",
            "US",
            item.theme or "AI叙事",
            title,
            f"{quote_content(row, item.symbol)} 数据源：{data_source}。",
            importance=4,
        )
        count += 1
    message = f"处理 {count} 条美股行情材料"
    if missing:
        message += f"，缺失 {len(missing)} 个标的"
    if note:
        message += f"；{note}"
    return SourceStatus("美股行情", "ok", message, count)


def collect_global_news(db: Session, report_date: date) -> SourceStatus:
    import akshare as ak

    status, payload, error = run_with_timeout("全球财经快讯", ak.stock_info_global_em)
    if status != "ok":
        return SourceStatus("全球财经快讯", "error", error or "全球财经快讯获取失败")
    count = 0
    skipped = 0
    duplicates = 0
    seen_keys: set[str] = set()
    for row in dataframe_records(payload, 80):
        title = str(first_value(row, ["标题", "title", "摘要", "内容"]) or "")
        content = str(first_value(row, ["内容", "摘要", "summary", "title"]) or title)
        url = first_value(row, ["链接", "url", "地址"])
        selection = editorial_news_selection(title, content)
        result = add_editorial_material(
            db,
            report_date,
            "akshare:全球财经快讯",
            news_market_hint(title, content),
            "AI叙事",
            title,
            content,
            str(url) if url else None,
            importance=5 if selection.get("strength") == "强" else 4,
            selection=selection,
            seen_keys=seen_keys,
        )
        if result == "added":
            count += 1
        elif result == "duplicate":
            duplicates += 1
        else:
            skipped += 1
        if count >= 10:
            break
    return SourceStatus("全球财经快讯", "ok", f"写入 {count} 条编辑筛选新闻，过滤 {skipped} 条，去重 {duplicates} 条", count)


def collect_xueqiu_materials(db: Session, report_date: date) -> SourceStatus:
    since = datetime.combine(report_date - timedelta(days=2), time.min, tzinfo=BEIJING_TZ).astimezone(ZoneInfo("UTC"))
    posts = list(db.scalars(select(XueqiuPost).where(XueqiuPost.crawled_at >= since).order_by(desc(XueqiuPost.crawled_at)).limit(20)))
    events = list(
        db.scalars(
            select(XueqiuWatchlistEvent)
            .where(XueqiuWatchlistEvent.created_at >= since)
            .order_by(desc(XueqiuWatchlistEvent.created_at))
            .limit(20)
        )
    )
    count = 0
    for post in posts:
        content = post.content[:900]
        title = f"{post.author_name} 雪球发言"
        if add_auto_material(db, report_date, "雪球发言", "A", "关注用户观点", title, content, post.source_url, importance=3):
            count += 1
    for event in events:
        verb = "新增" if event.event_type == "added" else "移除"
        title = f"{event.stock_name} {verb}自选"
        content = f"{event.stock_name}（{event.full_code}）被{verb}自选，记录价格 {event.price or '-'}。"
        if add_auto_material(db, report_date, "雪球自选", "A", "关注用户自选", title, content, event.source_url, importance=3):
            count += 1
    return SourceStatus("雪球材料", "ok", f"写入 {count} 条雪球材料", count)


def collect_daily_materials(db: Session, report_date_value: str | date | None = None) -> dict[str, Any]:
    ensure_default_universe(db)
    report_date = parse_report_date(report_date_value)
    statuses = [
        collect_a_quotes(db, report_date),
        collect_us_quotes(db, report_date),
        collect_global_news(db, report_date),
        collect_xueqiu_materials(db, report_date),
    ]
    db.commit()
    failed = [status for status in statuses if status.status != "ok"]
    return {
        "status": "partial_error" if failed else "ok",
        "message": "材料收集完成。" if not failed else "材料已部分收集，部分来源失败。",
        "report_date": report_date.isoformat(),
        "source_status": [status.__dict__ for status in statuses],
        "matched_count": sum(status.count for status in statuses),
        "ignored_count": len(failed),
    }


def materials_for_range(db: Session, start_date: date, end_date: date) -> list[MarketReviewMaterial]:
    return list(
        db.scalars(
            select(MarketReviewMaterial)
            .where(MarketReviewMaterial.report_date >= start_date, MarketReviewMaterial.report_date <= end_date)
            .order_by(MarketReviewMaterial.market, desc(MarketReviewMaterial.importance), desc(MarketReviewMaterial.created_at))
        )
    )


def format_material_lines(materials: list[MarketReviewMaterial], market: str | None = None, theme: str | None = None) -> str:
    rows = materials
    if market:
        rows = [item for item in rows if item.market in {market, "both"}]
    if theme:
        rows = [item for item in rows if theme in item.theme]
    if not rows:
        return "- 暂无材料。"
    lines = []
    for item in rows[:24]:
        link = f" [来源]({item.url})" if item.url else ""
        lines.append(f"- **{item.title}**：{item.content.strip().replace(chr(10), ' / ')[:500]}{link}")
    return "\n".join(lines)


def is_quote_material(item: MarketReviewMaterial) -> bool:
    return "行情" in item.source_name or "行情" in item.title or "涨跌幅" in item.content


def is_editorial_news_material(item: MarketReviewMaterial) -> bool:
    return item.source_name.startswith(EDITORIAL_NEWS_SOURCE_PREFIXES) or "编辑判断：" in item.content


def quote_change_pct(item: MarketReviewMaterial) -> float | None:
    match = re.search(r"涨跌幅\s*([+-]?\d+(?:\.\d+)?)%", item.content)
    return float(match.group(1)) if match else None


def quote_code(item: MarketReviewMaterial) -> str:
    match = re.search(r"代码\s*([A-Z0-9]+)", item.content)
    return match.group(1) if match else ""


def quote_name(item: MarketReviewMaterial) -> str:
    return item.title.split("（", 1)[0].replace("A股行情", "").replace("美股行情", "").strip()


def quote_rows(materials: list[MarketReviewMaterial], market: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for item in materials:
        if not is_quote_material(item):
            continue
        if market and item.market not in {market, "both"}:
            continue
        change_pct = quote_change_pct(item)
        if change_pct is None:
            continue
        rows.append(
            {
                "item": item,
                "name": quote_name(item),
                "code": quote_code(item),
                "market": item.market,
                "theme": item.theme,
                "change_pct": change_pct,
            }
        )
    return sorted(rows, key=lambda row: row["change_pct"], reverse=True)


def compact_market_snapshot(materials: list[MarketReviewMaterial], market: str | None = None, limit: int = 6) -> str:
    rows = quote_rows(materials, market)
    if not rows:
        return "- 暂无可识别的行情异动。"
    leaders = rows[:limit]
    return "\n".join(
        f"- {row['name']} {row['change_pct']:+.2f}%"
        for row in leaders
    )


@dataclass(frozen=True)
class NarrativeProfile:
    keywords: tuple[str, ...]
    business: str
    peers: tuple[str, ...]
    conclusion: str
    alpha: str
    beta: str
    macro: str
    verify: str
    falsify: str


NARRATIVE_PROFILES = [
    NarrativeProfile(
        ("闪迪", "SNDK", "Sandisk", "SanDisk", "西部数据", "WDC", "美光", "MU"),
        "存储/NAND/SSD",
        ("美光", "MU", "西部数据", "WDC", "SK海力士", "三星存储", "铠侠"),
        "先按存储周期Beta观察；若明显跑赢同行，再查目标价/指引等个股Alpha。",
        "目标价、拆分重估、盈利指引",
        "NAND/DRAM价格、AI服务器SSD需求、库存周期",
        "科技风险偏好",
        "存储价格、券商目标价、公司指引、同行同步性",
        "价格回落或毛利率没有改善",
    ),
    NarrativeProfile(
        ("中际旭创", "新易盛", "天孚通信"),
        "光模块/光器件",
        ("新易盛", "中际旭创", "天孚通信", "Broadcom", "AVGO"),
        "疑似AI网络链Beta扩散；个股Alpha仍要看订单和毛利率。",
        "客户结构、订单份额、毛利率",
        "800G/1.6T升级、北美云资本开支、网络瓶颈",
        "成长风格",
        "订单/排产、客户验证、毛利率",
        "云资本开支放缓或价格竞争压毛利",
    ),
    NarrativeProfile(
        ("寒武纪", "688256"),
        "国产AI芯片",
        ("NVIDIA", "NVDA", "AMD", "海光信息", "景嘉微"),
        "个股异动最强，可能叠加国产算力Beta；宏观只当背景。",
        "国产芯片订单、稀缺性、业绩兑现",
        "国产替代、信创采购、AI算力缺口",
        "科技风险偏好",
        "互联网/政企采购、收入确认、供给能力",
        "订单落空或估值先于业绩过度透支",
    ),
    NarrativeProfile(
        ("工业富联", "SMCI", "Super Micro"),
        "AI服务器/机柜集成",
        ("SMCI", "Super Micro", "工业富联", "戴尔", "DELL"),
        "疑似AI服务器链Beta，个股Alpha看客户和利润率。",
        "客户订单、交付节奏、毛利率",
        "AI服务器、液冷、机柜集成",
        "云资本开支",
        "排产、交付、毛利率、客户集中度",
        "订单传闻无法兑现或利润率下滑",
    ),
    NarrativeProfile(
        ("北方华创", "002371"),
        "半导体设备",
        ("中微公司", "盛美上海", "拓荆科技", "ASML", "AMAT"),
        "先按半导体设备Beta观察，Alpha看国产替代卡位。",
        "设备份额、订单、先进制程突破",
        "国产替代、晶圆厂资本开支",
        "政策和科技风格",
        "晶圆厂招标、订单、交付节奏",
        "资本开支推迟或国产替代进度低于预期",
    ),
    NarrativeProfile(
        ("NVIDIA", "NVDA", "英伟达"),
        "GPU/算力总开关",
        ("AMD", "AVGO", "SMCI", "台积电", "TSM"),
        "它是AI资本开支情绪锚，个股Alpha看供给和定价权。",
        "产品迭代、供给、定价权",
        "云厂商AI资本开支",
        "美股科技风险偏好",
        "云厂商CAPEX、订单能见度、毛利率",
        "客户ROI被质疑或资本开支放缓",
    ),
    NarrativeProfile(
        ("Broadcom", "AVGO", "博通"),
        "ASIC/网络芯片",
        ("NVIDIA", "NVDA", "AMD", "Marvell", "MRVL"),
        "疑似ASIC和网络瓶颈Beta，Alpha看AI收入占比。",
        "AI订单、定制芯片客户、网络芯片份额",
        "算力集群网络、ASIC替代、云资本开支",
        "美股科技风险偏好",
        "AI收入指引、定制芯片客户、交换网络需求",
        "AI收入增速放慢或客户集中风险暴露",
    ),
    NarrativeProfile(
        ("AMD", "超威"),
        "GPU替代/推理算力",
        ("NVIDIA", "NVDA", "Broadcom", "AVGO"),
        "个股Alpha看份额突破，行业Beta仍需客户采用验证。",
        "GPU份额、客户导入、产品竞争力",
        "AI推理需求、替代供给",
        "美股科技风险偏好",
        "客户采用、产品出货、毛利率",
        "份额未扩大或价格战压利润",
    ),
    NarrativeProfile(
        ("Microsoft", "MSFT", "Alphabet", "GOOGL", "Amazon", "AMZN", "Meta", "META"),
        "云厂商/AI应用",
        ("MSFT", "GOOGL", "AMZN", "META", "Oracle", "ORCL"),
        "它们是AI需求验证者；上涨看收入兑现，下跌看CAPEX压力。",
        "AI产品变现、云收入增速",
        "云CAPEX、AI应用渗透",
        "利率和美股科技风格",
        "云收入、AI收入、CAPEX与自由现金流",
        "投入压利润但收入没有跟上",
    ),
    NarrativeProfile(
        ("Tesla", "TSLA", "特斯拉"),
        "电动车/自动驾驶/机器人",
        ("Rivian", "RIVN", "Lucid", "LCID", "比亚迪"),
        "优先看个股Alpha，不能简单归入AI硬件链。",
        "自动驾驶、机器人、销量和毛利率",
        "电动车需求、能源业务",
        "利率和风险偏好",
        "交付、毛利率、FSD/Robotaxi进展",
        "销量走弱或自动驾驶预期降温",
    ),
    NarrativeProfile(
        ("宁德时代", "300750"),
        "电池/储能",
        ("比亚迪", "亿纬锂能", "阳光电源", "特斯拉"),
        "行业Beta看储能和电力需求是否落地，Alpha看份额和盈利。",
        "份额、海外订单、毛利率",
        "储能、数据中心用电、新能源周期",
        "商品价格和风险偏好",
        "储能订单、价格、毛利率",
        "电池价格下行或储能需求不及预期",
    ),
    NarrativeProfile(
        ("上证指数", "深证成指", "创业板指", "科创50", "Nasdaq", "NASDAQ", "S&P", "标普"),
        "宽基/风格",
        ("上证指数", "深证成指", "创业板指", "科创50", "Nasdaq", "S&P 500"),
        "只当市场背景，不当主因；真正结论要回到个股和行业。",
        "弱",
        "风格扩散",
        "流动性和风险偏好",
        "指数与主线是否共振",
        "指数强但主线不扩散",
    ),
]

DEFAULT_NARRATIVE_PROFILE = NarrativeProfile(
    (),
    "待识别业务",
    (),
    "先定位业务和同行，再判断是个股Alpha还是行业Beta。",
    "公司自身变化",
    "同行同步和行业价格",
    "风险偏好",
    "公司新闻、同行表现、行业数据",
    "只有股价异动，没有可验证催化",
)


def row_search_text(row: dict[str, Any]) -> str:
    return f"{row['name']} {row['code']} {row['theme']} {row['item'].title} {row['item'].content}"


def profile_for_row(row: dict[str, Any]) -> NarrativeProfile:
    text = row_search_text(row).lower()
    for profile in NARRATIVE_PROFILES:
        if any(keyword.lower() in text for keyword in profile.keywords):
            return profile
    return DEFAULT_NARRATIVE_PROFILE


def peer_rows_for(profile: NarrativeProfile, row: dict[str, Any], all_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not profile.peers:
        return []
    current_name = row["name"]
    current_code = row["code"]
    peers = []
    for candidate in all_rows:
        if candidate["name"] == current_name or candidate["code"] == current_code:
            continue
        text = f"{candidate['name']} {candidate['code']}".lower()
        if any(peer.lower() in text for peer in profile.peers):
            peers.append(candidate)
    return peers


def attribution_strengths(row: dict[str, Any], profile: NarrativeProfile, all_rows: list[dict[str, Any]]) -> tuple[str, str, str]:
    if profile.business == "宽基/风格":
        return ("弱", "中" if row["change_pct"] > 0 else "弱", "中")
    peer_rows = peer_rows_for(profile, row, all_rows)
    same_direction_peers = [peer for peer in peer_rows if peer["change_pct"] * row["change_pct"] > 0]
    alpha = "强" if abs(row["change_pct"]) >= 5 else "中"
    if peer_rows and row["change_pct"] <= max(peer["change_pct"] for peer in peer_rows) + 1:
        alpha = "中"
    beta = "疑似强" if len(same_direction_peers) >= 2 else "中" if same_direction_peers else "待验证"
    return (alpha, beta, "弱")


def peer_anchor_text(profile: NarrativeProfile, row: dict[str, Any], all_rows: list[dict[str, Any]]) -> str:
    peers = peer_rows_for(profile, row, all_rows)
    same_direction_peers = [peer for peer in peers if peer["change_pct"] * row["change_pct"] > 0]
    if same_direction_peers:
        names = "、".join(peer["name"] for peer in same_direction_peers[:3])
        return f"同行同步：{names}"
    if peers:
        names = "、".join(peer["name"] for peer in peers[:3])
        return f"同行对照：{names}"
    if profile.peers:
        names = "、".join(profile.peers[:3])
        return f"同行锚：{names}"
    return "同行锚：待补充"


def concise_story_lines(materials: list[MarketReviewMaterial], market: str | None = None, limit: int = 6) -> str:
    all_rows = quote_rows(materials)
    rows = sorted(quote_rows(materials, market), key=lambda row: abs(row["change_pct"]), reverse=True)[:limit]
    if not rows:
        return "- 暂无足够异动材料。"
    lines = []
    for row in rows:
        profile = profile_for_row(row)
        alpha, beta, macro = attribution_strengths(row, profile, all_rows)
        peer_text = peer_anchor_text(profile, row, all_rows)
        lines.append(
            f"- **{row['name']} {row['change_pct']:+.2f}%**：结论：{profile.conclusion} "
            f"归因：Alpha {alpha} / Beta {beta} / Macro {macro}。"
            f"画像：{profile.business}；{peer_text}。"
        )
    return "\n".join(lines)


def core_thesis(materials: list[MarketReviewMaterial]) -> str:
    rows = sorted(quote_rows(materials), key=lambda row: abs(row["change_pct"]), reverse=True)
    if not rows:
        return "核心结论：暂无足够行情锚点，先补材料。"
    top = rows[0]
    profile = profile_for_row(top)
    alpha, beta, macro = attribution_strengths(top, profile, rows)
    return (
        f"核心结论：先以 **{top['name']}** 为锚，判断它更像 "
        f"Alpha {alpha} / Beta {beta} / Macro {macro}；"
        f"重点不是涨幅，而是验证“{profile.verify}”。"
    )


def names_matching(materials: list[MarketReviewMaterial], keywords: tuple[str, ...]) -> str:
    names = []
    for row in quote_rows(materials):
        text = f"{row['name']} {row['code']} {row['item'].title}"
        if any(keyword.lower() in text.lower() for keyword in keywords) and row["name"] not in names:
            names.append(row["name"])
    return "、".join(names[:5]) or "暂无明显代表"


def ai_chain_story(materials: list[MarketReviewMaterial]) -> str:
    return "\n".join(
        [
            f"- 算力芯片：{names_matching(materials, ('寒武纪', 'NVIDIA', 'NVDA', 'AMD'))}。结论看国产替代/供给稀缺能否转成订单。",
            f"- 光模块/网络：{names_matching(materials, ('中际旭创', '新易盛', '天孚通信', 'Broadcom', 'AVGO'))}。结论看800G/1.6T和ASIC网络瓶颈是否扩散。",
            f"- AI服务器：{names_matching(materials, ('工业富联', 'Super Micro', 'SMCI'))}。结论看交付和毛利率，不看传闻本身。",
            f"- 云CAPEX：{names_matching(materials, ('Microsoft', 'MSFT', 'Alphabet', 'GOOGL', 'Amazon', 'AMZN', 'Meta', 'META'))}。结论看收入兑现能否覆盖投入压力。",
            f"- 电力/储能：{names_matching(materials, ('宁德时代', '300750'))}。结论看数据中心用电能否形成新Beta。",
        ]
    )


def cross_market_story(materials: list[MarketReviewMaterial]) -> str:
    a_rows = [row for row in quote_rows(materials, "A") if row["change_pct"] > 0]
    us_rows = [row for row in quote_rows(materials, "US") if row["change_pct"] > 0]
    a_names = "、".join(row["name"] for row in a_rows[:4]) or "暂无明显A股强势链条"
    us_names = "、".join(row["name"] for row in us_rows[:4]) or "暂无明显美股强势链条"
    return (
        f"- A股强势线索：{a_names}。\n"
        f"- 美股强势线索：{us_names}。\n"
        "- 二层看法：这只能说明AI基础设施链有疑似同步，不能直接等同于全球共振；还需要订单、财报、价格或资本开支指引验证。若只有A股走强，则更像国产替代、流动性或政策映射的本土叙事。"
    )


def observation_points(materials: list[MarketReviewMaterial]) -> str:
    rows = sorted(quote_rows(materials), key=lambda row: abs(row["change_pct"]), reverse=True)[:5]
    if not rows:
        return "- 先补行情、新闻、公告、研报材料。"
    lines = []
    for row in rows:
        profile = profile_for_row(row)
        lines.append(f"- **{row['name']}**：验证 {profile.verify}；反证 {profile.falsify}。")
    return "\n".join(lines)


def clean_material_text(text: str) -> str:
    cleaned = text.replace("\n", " ")
    chunks = [chunk.strip() for chunk in re.split(r"[｜|]", cleaned) if chunk.strip()]
    if len(chunks) > 1:
        deduped = []
        seen: set[str] = set()
        for chunk in chunks:
            key = normalized_news_text(chunk)[:120]
            if key and key in seen:
                continue
            seen.add(key)
            deduped.append(chunk)
        cleaned = "；".join(deduped)
    cleaned = re.sub(r"[｜/]\s*[｜/]+", "；", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:180]


def catalyst_keywords(materials: list[MarketReviewMaterial]) -> tuple[str, ...]:
    keywords = {
        "数据中心",
        "电网",
        "电力",
        "存储",
        "NAND",
        "DRAM",
        "HBM",
        "SSD",
        "光模块",
        "服务器",
        "半导体",
        "目标价",
        "券商",
        "涨价",
        "订单",
        "排产",
        "财报",
        "指引",
        "CAPEX",
        "资本开支",
    }
    for row in quote_rows(materials)[:8]:
        profile = profile_for_row(row)
        keywords.update({row["name"], row["code"], profile.business})
        keywords.update(profile.keywords[:4])
        keywords.update(profile.peers[:4])
    return tuple(keyword for keyword in keywords if keyword)


def evidence_lines(materials: list[MarketReviewMaterial], limit: int = 8) -> str:
    keywords = catalyst_keywords(materials)
    rows = []
    seen: set[tuple[str, str]] = set()
    for item in materials:
        if is_quote_material(item):
            continue
        search_text = (f"{item.title} {item.content}" if item.source_type == "manual" or is_editorial_news_material(item) else item.title).lower()
        if not any(keyword.lower() in search_text for keyword in keywords):
            continue
        key = (item.title.strip(), (item.url or "").strip())
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
        if len(rows) >= limit:
            break
    if not rows:
        return "- 暂无可靠催化材料命中；当前结论主要来自行情锚点和同行对照，需要补新闻、公告、研报或行业价格验证。"
    lines = []
    for item in rows[:limit]:
        link = f" [来源]({item.url})" if item.url else ""
        lines.append(f"- **{item.title}**：{clean_material_text(item.content)}{link}")
    return "\n".join(lines)


def locked_quote_facts(materials: list[MarketReviewMaterial], limit: int = 28) -> str:
    rows = quote_rows(materials)
    if not rows:
        return "- 无行情事实。"
    return "\n".join(
        f"- {row['market']}｜{row['name']}｜{row['code']}｜涨跌幅 {row['change_pct']:+.2f}%｜主题 {row['theme']}"
        for row in rows[:limit]
    )


def ai_hard_evidence(materials: list[MarketReviewMaterial], limit: int = 10) -> str:
    text = evidence_lines(materials, limit=limit)
    if text.startswith("- 暂无可靠催化材料命中"):
        return text
    return text


def fresh_report_draft(report: MarketReviewReport, materials: list[MarketReviewMaterial]) -> str:
    source_status = json.loads(report.source_status_json or "[]")
    return build_markdown(
        report.period,
        report.report_date,
        report.start_date,
        report.end_date,
        materials,
        source_status,
        report.status,
    )


def report_materials(db: Session, report: MarketReviewReport) -> list[MarketReviewMaterial]:
    if report.period == "daily":
        return materials_for_range(db, report.report_date, report.report_date)
    return materials_for_range(db, report.start_date, report.end_date)


def narrative_card_rows(materials: list[MarketReviewMaterial], limit: int = 8) -> list[dict[str, Any]]:
    rows = [row for row in quote_rows(materials) if profile_for_row(row).business != "宽基/风格"]
    return sorted(rows, key=lambda row: abs(row["change_pct"]), reverse=True)[:limit]


def material_search_text(item: MarketReviewMaterial) -> str:
    if item.source_type == "manual" or is_editorial_news_material(item):
        return f"{item.title} {item.content}"
    return item.title


def row_keywords(row: dict[str, Any]) -> set[str]:
    profile = profile_for_row(row)
    keywords = {row["name"], row["code"], profile.business}
    keywords.update(profile.keywords)
    keywords.update(profile.peers)
    for text in (profile.alpha, profile.beta, profile.verify):
        keywords.update(part for part in re.split(r"[、/，,；;。\s]+", text) if len(part) >= 2)
    return {keyword for keyword in keywords if keyword}


def business_terms(profile: NarrativeProfile) -> set[str]:
    parts = re.split(r"[、/，,；;。\s]+", profile.business)
    return {part for part in parts if len(part) >= 2 and part not in {"待识别业务", "宽基", "风格"}}


def evidence_match_score(row: dict[str, Any], item: MarketReviewMaterial) -> int:
    profile = profile_for_row(row)
    text = f"{item.title} {item.content}".lower()
    score = 0
    direct_terms = {row["name"], row["code"], *profile.keywords}
    anchor_hit = False
    for term in direct_terms:
        if term and term.lower() in text:
            score += 5
            anchor_hit = True
    for term in business_terms(profile):
        if term.lower() in text:
            score += 3
            anchor_hit = True
    peer_hits = 0
    for term in profile.peers:
        if term and term.lower() in text:
            peer_hits += 1
    if peer_hits:
        score += min(peer_hits, 2)
    catalyst_hits = 0
    for term in EXPLANATORY_CATALYST_HINTS:
        if term.lower() in text:
            catalyst_hits += 1
    if catalyst_hits:
        score += min(catalyst_hits, 3)
    if is_editorial_news_material(item):
        score += 1
    if not anchor_hit:
        return 0
    if score < 4:
        return 0
    return score


def evidence_for_row(row: dict[str, Any], materials: list[MarketReviewMaterial], limit: int = 4) -> list[dict[str, str]]:
    scored: list[tuple[int, MarketReviewMaterial]] = []
    seen: set[tuple[str, str]] = set()
    for item in materials:
        if is_quote_material(item):
            continue
        key = (item.title.strip(), (item.url or "").strip())
        if key in seen:
            continue
        seen.add(key)
        score = evidence_match_score(row, item)
        if not score:
            continue
        scored.append((score, item))
    evidence: list[dict[str, str]] = []
    for _score, item in sorted(scored, key=lambda pair: (pair[0], pair[1].importance, pair[1].created_at), reverse=True):
        evidence.append(
            {
                "title": item.title[:160],
                "summary": clean_material_text(item.content),
                "url": item.url or "",
            }
        )
        if len(evidence) >= limit:
            break
    return evidence


def card_payload(materials: list[MarketReviewMaterial]) -> dict[str, Any]:
    all_rows = quote_rows(materials)
    cards = []
    for index, row in enumerate(narrative_card_rows(materials), start=1):
        profile = profile_for_row(row)
        alpha, beta, macro = attribution_strengths(row, profile, all_rows)
        cards.append(
            {
                "id": f"c{index}",
                "market": row["market"],
                "name": row["name"],
                "code": row["code"],
                "change_pct": row["change_pct"],
                "business": profile.business,
                "peer_anchor": peer_anchor_text(profile, row, all_rows),
                "program_guess": profile.conclusion,
                "program_attribution": {"alpha": alpha, "beta": beta, "macro": macro},
                "verify_hint": profile.verify,
                "falsify_hint": profile.falsify,
                "evidence": evidence_for_row(row, materials),
            }
        )
    return {"cards": cards}


def strip_bullet(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text)
    return text.strip()


def split_markdown_lines(value: str, limit: int = 6) -> list[str]:
    lines = [strip_bullet(line) for line in str(value or "").splitlines()]
    return [line for line in lines if line][:limit]


def normalize_text_list(value: Any, fallback: list[str] | None = None, limit: int = 5, max_len: int = 220) -> list[str]:
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = split_markdown_lines(value, limit=limit)
    else:
        raw_items = fallback or []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        text = strip_bullet(text)[:max_len]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result or (fallback or [])


def evidence_sentence(evidence: list[dict[str, str]]) -> str:
    if not evidence:
        return "暂无直接催化，先当作行情线索。"
    first = evidence[0]
    summary = first.get("summary") or first.get("title") or ""
    return clean_material_text(summary)


def second_order_hint(row: dict[str, Any], profile: NarrativeProfile, evidence: list[dict[str, str]]) -> str:
    if evidence:
        return f"二层问题不是涨了多少，而是这条催化能否验证“{profile.verify}”。"
    return f"缺口在于只有行情锚，没有直接材料；下一步先补“{profile.verify}”。"


def editorial_brief_cards(materials: list[MarketReviewMaterial], limit: int = 7) -> list[dict[str, Any]]:
    all_rows = quote_rows(materials)
    rows = narrative_card_rows(materials, limit=limit)
    cards: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        profile = profile_for_row(row)
        alpha, beta, macro = attribution_strengths(row, profile, all_rows)
        peers = peer_rows_for(profile, row, all_rows)
        same_peers = [peer for peer in peers if peer["change_pct"] * row["change_pct"] > 0]
        opposite_peers = [peer for peer in peers if peer["change_pct"] * row["change_pct"] < 0]
        evidence = evidence_for_row(row, materials, limit=3)
        cards.append(
            {
                "id": f"s{index}",
                "market": row["market"],
                "name": row["name"],
                "code": row["code"],
                "visible_move": f"{row['name']} {row['change_pct']:+.2f}%",
                "business": profile.business,
                "peer_same_direction": [f"{peer['name']} {peer['change_pct']:+.2f}%" for peer in same_peers[:3]],
                "peer_divergence": [f"{peer['name']} {peer['change_pct']:+.2f}%" for peer in opposite_peers[:3]],
                "attribution": {"alpha": alpha, "beta": beta, "macro": macro},
                "program_view": profile.conclusion,
                "hidden_question": second_order_hint(row, profile, evidence),
                "verify": profile.verify,
                "falsify": profile.falsify,
                "evidence": evidence,
                "evidence_summary": evidence_sentence(evidence),
            }
        )
    return cards


def market_brief_lines(materials: list[MarketReviewMaterial], market: str | None = None, limit: int = 4) -> list[str]:
    rows = sorted(quote_rows(materials, market), key=lambda row: abs(row["change_pct"]), reverse=True)[:limit]
    result = []
    for row in rows:
        profile = profile_for_row(row)
        result.append(f"{row['name']} {row['change_pct']:+.2f}%｜{profile.business}｜验证：{profile.verify}")
    return result


def editorial_brief_payload(report: MarketReviewReport, materials: list[MarketReviewMaterial]) -> dict[str, Any]:
    cards = editorial_brief_cards(materials)
    hard_evidence = []
    seen: set[tuple[str, str]] = set()
    for item in materials:
        if is_quote_material(item):
            continue
        key = (item.title.strip(), (item.url or "").strip())
        if key in seen:
            continue
        seen.add(key)
        hard_evidence.append(
            {
                "title": item.title[:160],
                "source": item.source_name,
                "market": item.market,
                "summary": clean_material_text(item.content),
                "url": item.url or "",
            }
        )
        if len(hard_evidence) >= 10:
            break
    return {
        "period": report.period,
        "report_date": report.report_date.isoformat(),
        "range": [report.start_date.isoformat(), report.end_date.isoformat()],
        "writing_goal": "从显性异动倒推市场正在相信的故事；重点写看不见的催化、验证点和反证点。",
        "style_rules": [
            "少复述涨跌幅，涨跌幅只做线索。",
            "每条判断必须包含：表象、背后故事、验证或反证。",
            "证据不足时写待验证，不要假装确定。",
            "宁可少写，也不要泛泛写AI、政策、风险偏好。",
        ],
        "market_snapshot": {
            "A": market_brief_lines(materials, "A"),
            "US": market_brief_lines(materials, "US"),
        },
        "story_cards": cards,
        "hard_evidence": hard_evidence,
    }


def fallback_narrative_from_brief(brief: dict[str, Any]) -> dict[str, Any]:
    cards = brief.get("story_cards") or []
    top = cards[0] if cards else None
    if top:
        top_has_evidence = bool(top.get("evidence"))
        hidden = (
            f"可用催化是：{top['evidence_summary']}"
            if top_has_evidence
            else "目前没有可靠新闻解释它为什么涨，这本身就是结论：只能先当作资金在预演相关主题，不能写成已验证"
        )
        core = (
            f"先把 {top['name']} 当作叙事锚点：看得见的是{top['visible_move']}，"
            f"{hidden}。当前更像 "
            f"Alpha {top['attribution']['alpha']} / Beta {top['attribution']['beta']}，"
            f"关键是验证 {top['verify']}。"
        )
    else:
        core = "暂无足够异动和催化材料，先补行情、新闻、公告或行业价格。"

    def card_line(card: dict[str, Any]) -> str:
        peer_text = "、".join(card.get("peer_same_direction") or []) or "同行同步不明显"
        evidence_text = (
            f"可用催化：{card['evidence_summary']}；"
            if card.get("evidence")
            else "暂无直接催化，不能把涨幅当原因；"
        )
        return (
            f"{card['name']}：表象是{card['visible_move']}；{evidence_text}背后更像{card['program_view']} "
            f"同行：{peer_text}；验证 {card['verify']}，反证 {card['falsify']}。"
        )

    a_cards = [card for card in cards if card.get("market") == "A"]
    us_cards = [card for card in cards if card.get("market") == "US"]
    story_points = [card_line(card) for card in cards[:3]]
    return {
        "core_story": core,
        "story_points": story_points or ["暂无足够材料形成主线，只保留待验证状态。"],
        "a_market": [card_line(card) for card in a_cards[:3]] or ["A股没有足够清晰的主线，先看后续是否有订单、价格或政策催化。"],
        "us_market": [card_line(card) for card in us_cards[:3]] or ["美股没有足够清晰的主线，先看云CAPEX、AI收入和龙头指引。"],
        "ai_chain": split_markdown_lines(ai_chain_story_from_brief(brief), limit=5),
        "leader_effect": [card_line(card) for card in cards[:4]] or ["暂无可确认的龙头效应。"],
        "cross_market": ["A股和美股若只同涨，不能直接叫共振；只有当订单、价格、CAPEX或财报同时验证，才算叙事共振。"],
        "next_watch": [f"{card['name']}：验证 {card['verify']}；反证 {card['falsify']}。" for card in cards[:4]]
        or ["先补直接催化材料。"],
        "confidence": "中" if any(card.get("evidence") for card in cards) else "低",
        "evidence_used": [item["title"] for item in brief.get("hard_evidence", [])[:6]],
        "quality_flags": ["本地编辑稿：AI 未参与或 AI 输出不可用。"],
    }


def ai_chain_story_from_brief(brief: dict[str, Any]) -> str:
    cards = brief.get("story_cards") or []
    names_by_business: dict[str, list[str]] = {}
    for card in cards:
        names_by_business.setdefault(card.get("business") or "其他", []).append(card.get("name") or "")
    return "\n".join(
        f"- {business}：{'、'.join(name for name in names if name) or '暂无'}。验证点看订单、价格、指引或同行扩散。"
        for business, names in list(names_by_business.items())[:5]
    ) or "- 暂无足够产业链线索。"


def normalize_ai_narrative(ai_payload: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    fallback = fallback_narrative_from_brief(brief)
    if not isinstance(ai_payload, dict):
        return fallback
    core = text_value(ai_payload.get("core_story"), fallback["core_story"])
    return {
        "core_story": core,
        "story_points": normalize_text_list(ai_payload.get("story_points"), fallback["story_points"], limit=4),
        "a_market": normalize_text_list(ai_payload.get("a_market"), fallback["a_market"], limit=4),
        "us_market": normalize_text_list(ai_payload.get("us_market"), fallback["us_market"], limit=4),
        "ai_chain": normalize_text_list(ai_payload.get("ai_chain"), fallback["ai_chain"], limit=5),
        "leader_effect": normalize_text_list(ai_payload.get("leader_effect"), fallback["leader_effect"], limit=4),
        "cross_market": normalize_text_list(ai_payload.get("cross_market"), fallback["cross_market"], limit=4),
        "next_watch": normalize_text_list(ai_payload.get("next_watch"), fallback["next_watch"], limit=5),
        "confidence": text_value(ai_payload.get("confidence"), fallback["confidence"])[:20],
        "evidence_used": normalize_text_list(ai_payload.get("evidence_used"), fallback["evidence_used"], limit=8, max_len=160),
        "quality_flags": normalize_text_list(ai_payload.get("quality_flags"), fallback["quality_flags"], limit=6, max_len=180),
    }


def audit_narrative(narrative: dict[str, Any], brief: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    joined = " ".join(
        [str(narrative.get("core_story") or "")]
        + [line for key in ("story_points", "a_market", "us_market", "leader_effect") for line in narrative.get(key, [])]
    )
    banned = ("确定", "必然", "一定", "全面兑现", "已经验证", "闭眼买", "买入")
    if any(word in joined for word in banned):
        issues.append("存在过强表述，需降级为待验证。")
    if not any(word in joined for word in ("验证", "反证", "待验证", "订单", "价格", "指引", "财报", "目标价", "CAPEX", "资本开支")):
        issues.append("缺少验证/反证语言，容易退化成行情复述。")
    if "涨幅" in joined and not any(word in joined for word in ("因为", "背后", "催化", "验证", "反证")):
        issues.append("对行情表现解释不足。")
    if not brief.get("hard_evidence") and str(narrative.get("confidence")) == "高":
        narrative["confidence"] = "低"
        issues.append("缺少硬材料，高置信度已降级。")
    return issues


def render_narrative_lines(lines: list[str]) -> str:
    if not lines:
        return "- 暂无。"
    return "\n".join(f"- {strip_bullet(line)}" for line in lines)


def compose_ai_narrative_report(
    report: MarketReviewReport,
    materials: list[MarketReviewMaterial],
    narrative: dict[str, Any],
    issues: list[str],
) -> str:
    title = "A股 + 美股市场叙事日报" if report.period == "daily" else "A股 + 美股市场叙事周报"
    source_status = json.loads(report.source_status_json or "[]")
    review_flags = issues or narrative.get("quality_flags") or ["审稿通过：没有发现明显越界结论。"]
    return f"""# {title}｜{report.report_date.isoformat()}

> 覆盖区间：{report.start_date.isoformat()} 至 {report.end_date.isoformat()}
> 报告状态：{report.status}
> AI生成方式：编辑 brief + AI叙事 + 程序审稿
> 置信度：{narrative.get("confidence") or "待验证"}
> 说明：本报告用于复盘市场叙事，不构成确定性买卖建议。

## 今日核心故事

{narrative.get("core_story") or "暂无足够材料。"}

{render_narrative_lines(narrative.get("story_points") or [])}

## A股在讲什么

{render_narrative_lines(narrative.get("a_market") or [])}

## 美股在讲什么

{render_narrative_lines(narrative.get("us_market") or [])}

## AI叙事链条：资本开支、算力、光模块、半导体、电力/散热

{render_narrative_lines(narrative.get("ai_chain") or [])}

## 龙头与市场效应

{render_narrative_lines(narrative.get("leader_effect") or [])}

## A股与美股叙事对照

{render_narrative_lines(narrative.get("cross_market") or [])}

## 下一步观察点

{render_narrative_lines(narrative.get("next_watch") or [])}

## AI审稿结果

{render_narrative_lines(review_flags)}

## 数据源状态与引用材料

{source_status_markdown(source_status)}

### AI使用的关键材料

{render_narrative_lines(narrative.get("evidence_used") or [])}

### 引用材料

{evidence_lines(materials)}
"""


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.S)
    if fence:
        cleaned = fence.group(1)
    else:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("AI 返回不是 JSON 对象")
    return payload


def text_value(value: Any, fallback: str = "待验证") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:260] if text else fallback


def normalize_confidence(value: Any, has_evidence: bool) -> str:
    text = str(value or "").strip()
    if text in {"高", "中", "低"}:
        return text
    return "中" if has_evidence else "低"


def audit_card(card: dict[str, Any], source: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    evidence = source.get("evidence") or []
    joined = " ".join(
        text_value(card.get(field), "")
        for field in ("conclusion", "reason", "evidence", "gap")
    )
    banned = ("已经验证", "确定", "必然", "全球共振已成立", "周期加速", "已经兑现", "证实", "全面加速")
    if any(word in joined for word in banned):
        issues.append("存在过强表述，已按待验证处理")
    if not evidence and not any(word in joined for word in ("待验证", "疑似", "可能", "需要验证")):
        issues.append("缺少硬催化证据，结论需要降级为待验证")
    if source["name"] not in joined and source["code"] not in joined:
        issues.append("卡片未明确对应标的")
    return issues


def normalize_ai_cards(ai_payload: dict[str, Any], source_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    source_by_id = {item["id"]: item for item in source_payload["cards"]}
    raw_cards = ai_payload.get("cards")
    if not isinstance(raw_cards, list):
        raise ValueError("AI 返回缺少 cards 数组")
    cards: list[dict[str, Any]] = []
    report_issues: list[str] = []
    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        card_id = str(raw.get("id") or "")
        source = source_by_id.get(card_id)
        if not source:
            report_issues.append(f"忽略未知卡片 {card_id or '-'}")
            continue
        issues = audit_card(raw, source)
        if issues:
            report_issues.extend(f"{source['name']}：{issue}" for issue in issues)
        has_evidence = bool(source.get("evidence"))
        confidence = normalize_confidence(raw.get("confidence"), has_evidence)
        if not has_evidence and confidence == "高":
            confidence = "低"
            issues.append("缺少直接催化，高置信度已降级")
            report_issues.append(f"{source['name']}：缺少直接催化，高置信度已降级")
        if issues and confidence == "高":
            confidence = "低"
        cards.append(
            {
                "id": card_id,
                "market": source["market"],
                "name": source["name"],
                "code": source["code"],
                "change_pct": source["change_pct"],
                "business": source["business"],
                "peer_anchor": source["peer_anchor"],
                "program_attribution": source["program_attribution"],
                "verify_hint": source["verify_hint"],
                "falsify_hint": source["falsify_hint"],
                "source_evidence": source.get("evidence") or [],
                "conclusion": text_value(raw.get("conclusion")),
                "driver": text_value(raw.get("driver")),
                "reason": text_value(raw.get("reason")),
                "evidence": text_value(raw.get("evidence"), "暂无硬催化，待验证"),
                "gap": text_value(raw.get("gap")),
                "confidence": confidence,
                "issues": issues,
            }
        )
    missing = [item for item in source_payload["cards"] if item["id"] not in {card["id"] for card in cards}]
    for source in missing:
        report_issues.append(f"{source['name']}：AI 未返回卡片，已使用程序草稿兜底")
        attribution = source["program_attribution"]
        cards.append(
            {
                "id": source["id"],
                "market": source["market"],
                "name": source["name"],
                "code": source["code"],
                "change_pct": source["change_pct"],
                "business": source["business"],
                "peer_anchor": source["peer_anchor"],
                "program_attribution": attribution,
                "verify_hint": source["verify_hint"],
                "falsify_hint": source["falsify_hint"],
                "source_evidence": source.get("evidence") or [],
                "conclusion": source["program_guess"],
                "driver": f"Alpha {attribution['alpha']} / Beta {attribution['beta']} / Macro {attribution['macro']}",
                "reason": "AI 未返回该标的卡片，使用程序归因兜底。",
                "evidence": "暂无硬催化，待验证",
                "gap": source["verify_hint"],
                "confidence": "低",
                "issues": ["AI 未返回卡片"],
            }
        )
    order = {item["id"]: index for index, item in enumerate(source_payload["cards"])}
    cards.sort(key=lambda item: order.get(item["id"], 999))
    return cards, report_issues


def card_line(card: dict[str, Any]) -> str:
    attribution = card["program_attribution"]
    issue_text = "；审稿：需修正" if card["issues"] else ""
    return (
        f"- **{card['name']} {card['change_pct']:+.2f}%**："
        f"结论：{card['conclusion']}。"
        f"归因：Alpha {attribution['alpha']} / Beta {attribution['beta']} / Macro {attribution['macro']}。"
        f"原因：{card['reason']}。"
        f"证据：{card['evidence']}。"
        f"缺口：{card['gap']}。"
        f"置信度：{card['confidence']}{issue_text}。"
    )


def card_lines(cards: list[dict[str, Any]], market: str | None = None, limit: int = 8) -> str:
    filtered = [card for card in cards if market is None or card["market"] == market][:limit]
    if not filtered:
        return "- 暂无卡片。"
    return "\n".join(card_line(card) for card in filtered)


def card_core_story(cards: list[dict[str, Any]], issues: list[str]) -> str:
    if not cards:
        return "核心结论：暂无足够卡片，先补材料。"
    top = cards[0]
    caution = "；审稿器提示存在需修正项，本版只作为草稿" if issues else ""
    return (
        f"核心结论：先以 **{top['name']}** 为锚，当前更适合写成“{top['conclusion']}”。"
        f"证据强度：{top['confidence']}；关键缺口：{top['gap']}{caution}。"
    )


def render_card_evidence(cards: list[dict[str, Any]], limit: int = 10) -> str:
    lines = []
    seen: set[tuple[str, str]] = set()
    for card in cards:
        for item in card.get("source_evidence") or []:
            key = (item.get("title", ""), item.get("url", ""))
            if key in seen:
                continue
            seen.add(key)
            link = f" [来源]({item.get('url')})" if item.get("url") else ""
            lines.append(f"- **{item.get('title')}**：{item.get('summary')}{link}")
            if len(lines) >= limit:
                return "\n".join(lines)
    return "- 暂无直接催化材料命中；本版主要依据行情锚点和同行对照，需继续补证据。"


def render_review_issues(issues: list[str]) -> str:
    if not issues:
        return "- 审稿通过：没有发现明显越界结论。"
    return "\n".join(f"- {issue}" for issue in issues[:12])


def compose_ai_card_report(
    report: MarketReviewReport,
    materials: list[MarketReviewMaterial],
    cards: list[dict[str, Any]],
    issues: list[str],
) -> str:
    title = "A股 + 美股市场叙事日报" if report.period == "daily" else "A股 + 美股市场叙事周报"
    source_status = json.loads(report.source_status_json or "[]")
    return f"""# {title}｜{report.report_date.isoformat()}

> 覆盖区间：{report.start_date.isoformat()} 至 {report.end_date.isoformat()}
> 报告状态：{report.status}
> AI生成方式：单票归因卡片 + 程序拼接 + 审稿器
> 说明：本报告用于复盘市场叙事，不构成确定性买卖建议。

## 今日核心故事

{card_core_story(cards, issues)}

## A股在讲什么

{card_lines(cards, market="A", limit=5)}

## 美股在讲什么

{card_lines(cards, market="US", limit=5)}

## AI叙事链条：资本开支、算力、光模块、半导体、电力/散热

{ai_chain_story(materials)}

## 龙头与资金反馈

{card_lines(cards, limit=5)}

## A股与美股叙事对照

{cross_market_story(materials)}

## 下一步观察点

{observation_points(materials)}

## AI审稿结果

{render_review_issues(issues)}

## 数据源状态与引用材料

{source_status_markdown(source_status)}

### 引用材料

{render_card_evidence(cards)}
"""


def source_status_markdown(source_status: list[dict[str, Any]]) -> str:
    if not source_status:
        return "- 暂无来源状态。"
    return "\n".join(
        f"- {item.get('source')}: {item.get('status')}，{item.get('message')}"
        for item in source_status
    )


def report_path(period: str, report_date: date) -> Path:
    folder = report_root() / ("daily" if period == "daily" else "weekly")
    folder.mkdir(parents=True, exist_ok=True)
    if period == "weekly":
        year, week, _ = report_date.isocalendar()
        return folder / f"{year}-W{week:02d}.md"
    return folder / f"{report_date.isoformat()}.md"


def build_markdown(
    period: str,
    report_date: date,
    start_date: date,
    end_date: date,
    materials: list[MarketReviewMaterial],
    source_status: list[dict[str, Any]],
    status: str,
) -> str:
    title = "A股 + 美股市场叙事日报" if period == "daily" else "A股 + 美股市场叙事周报"
    failed = [item for item in source_status if item.get("status") != "ok"]
    failed_line = "无" if not failed else "；".join(f"{item.get('source')}：{item.get('message')}" for item in failed)
    return f"""# {title}｜{report_date.isoformat()}

> 覆盖区间：{start_date.isoformat()} 至 {end_date.isoformat()}  
> 报告状态：{status}  
> 数据源异常：{failed_line}  
> 说明：本报告用于复盘市场叙事，不构成确定性买卖建议。

## 今日核心故事

{core_thesis(materials)}

异动只做索引，不做结论：

{compact_market_snapshot(materials, limit=7)}

优先看这几个锚点：

{concise_story_lines(materials, limit=4)}

## A股在讲什么

{concise_story_lines(materials, market="A", limit=5)}

## 美股在讲什么

{concise_story_lines(materials, market="US", limit=5)}

## AI叙事链条：资本开支、算力、光模块、半导体、电力/散热

{ai_chain_story(materials)}

## 龙头与资金反馈

- 只看一个股票，容易误判；先看同行是否同步，再决定是个股Alpha还是行业Beta。
- Alpha 强：公司自己的订单、目标价、指引、业务重估。Beta 强：同行一起动、价格/景气度一起变。Macro 通常只当背景。

## A股与美股叙事对照

{cross_market_story(materials)}

## 下一步观察点

{observation_points(materials)}

## 数据源状态与引用材料

{source_status_markdown(source_status)}

### 引用材料

{evidence_lines(materials)}
"""


def upsert_report(
    db: Session,
    period: str,
    report_date: date,
    start_date: date,
    end_date: date,
    content: str,
    status: str,
    source_status: list[dict[str, Any]],
) -> MarketReviewReport:
    path = report_path(period, report_date)
    path.write_text(content, encoding="utf-8")
    title = ("A股 + 美股市场叙事日报" if period == "daily" else "A股 + 美股市场叙事周报") + f" {report_date.isoformat()}"
    report = db.scalar(
        select(MarketReviewReport).where(
            MarketReviewReport.period == period,
            MarketReviewReport.report_date == report_date,
        )
    )
    if not report:
        report = MarketReviewReport(
            period=period,
            report_date=report_date,
            start_date=start_date,
            end_date=end_date,
            title=title,
            markdown_path=str(path),
            content=content,
            status=status,
            ai_status="manual_only" if configured_ai_status(db) == "ok" else "not_configured",
            source_status_json=json.dumps(source_status, ensure_ascii=False),
        )
        db.add(report)
    else:
        report.start_date = start_date
        report.end_date = end_date
        report.title = title
        report.markdown_path = str(path)
        report.content = content
        report.status = status
        if report.ai_status in {"ok", "not_configured"}:
            report.ai_status = "manual_only" if configured_ai_status(db) == "ok" else "not_configured"
        elif report.ai_status == "error" and configured_ai_status(db) != "ok":
            report.ai_status = "not_configured"
        report.source_status_json = json.dumps(source_status, ensure_ascii=False)
        report.updated_at = now_utc()
    db.commit()
    db.refresh(report)
    return report


def generate_daily_report(db: Session, report_date_value: str | date | None = None, collect: bool = True) -> dict[str, Any]:
    report_date = parse_report_date(report_date_value)
    collection = collect_daily_materials(db, report_date) if collect else {
        "status": "ok",
        "source_status": [],
    }
    start_date = report_date - timedelta(days=1)
    end_date = report_date
    materials = materials_for_range(db, report_date, report_date)
    status = "partial_error" if collection["status"] == "partial_error" else "ok"
    if not materials:
        status = "manual_only" if status == "ok" else status
    content = build_markdown("daily", report_date, start_date, end_date, materials, collection["source_status"], status)
    report = upsert_report(db, "daily", report_date, start_date, end_date, content, status, collection["source_status"])
    return {
        "status": report.status,
        "message": "日报 Markdown 已生成。",
        "source_status": collection["source_status"],
        "report": report_to_dict(report),
        "matched_count": len(materials),
        "ignored_count": len([item for item in collection["source_status"] if item.get("status") != "ok"]),
    }


def generate_weekly_report(db: Session, report_date_value: str | date | None = None) -> dict[str, Any]:
    report_date = parse_report_date(report_date_value)
    start_date = report_date - timedelta(days=report_date.weekday())
    end_date = report_date
    materials = materials_for_range(db, start_date, end_date)
    source_status = [{"source": "本地材料", "status": "ok", "message": f"汇总 {len(materials)} 条材料", "count": len(materials)}]
    status = "ok" if materials else "manual_only"
    content = build_markdown("weekly", report_date, start_date, end_date, materials, source_status, status)
    report = upsert_report(db, "weekly", report_date, start_date, end_date, content, status, source_status)
    return {
        "status": report.status,
        "message": "周报 Markdown 已生成。",
        "source_status": source_status,
        "report": report_to_dict(report),
        "matched_count": len(materials),
        "ignored_count": 0,
    }


def seconds_until_next_daily_run() -> float:
    configured = os.environ.get("MARKET_REVIEW_DAILY_TIME", "08:30")
    hour_text, minute_text = configured.split(":", 1)
    now = datetime.now(BEIJING_TZ)
    target = datetime.combine(now.date(), time(int(hour_text), int(minute_text)), tzinfo=BEIJING_TZ)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def run_ai_for_report(db: Session, report_id: int) -> dict[str, Any]:
    report = db.get(MarketReviewReport, report_id)
    if not report:
        raise ValueError("报告不存在")
    ai_config = effective_ai_config(db)
    if not ai_config:
        report.ai_status = "not_configured"
        report.updated_at = now_utc()
        db.commit()
        return {
            "status": "not_configured",
            "message": "AI 未配置，请先在复盘页设置 DeepSeek 或其他模型。",
            "source_status": json.loads(report.source_status_json or "[]"),
            "report": report_to_dict(report),
        }

    materials = report_materials(db, report)
    brief = editorial_brief_payload(report, materials)
    system_prompt = (
        "你是市场叙事复盘主笔，不是新闻摘要员。"
        "任务：从显性异动倒推市场正在相信的故事，尤其写用户看不见的二层原因。"
        "写法必须短、准、有判断：表象 -> 背后故事 -> 验证/反证。"
        "硬规则：不要把涨幅、创新高、成交放大写成原因；它们只能是线索。"
        "没有硬证据时必须写“待验证”；不要新增 brief 以外的公司和事实。"
        "禁止输出买卖建议，禁止使用“确定、必然、全面兑现、已经验证、闭眼买”。"
        "必须只输出 JSON，不要 Markdown，不要解释。"
        "JSON 字段必须是："
        "{\"core_story\":\"...\",\"story_points\":[\"...\"],\"a_market\":[\"...\"],\"us_market\":[\"...\"],"
        "\"ai_chain\":[\"...\"],\"leader_effect\":[\"...\"],\"cross_market\":[\"...\"],"
        "\"next_watch\":[\"...\"],\"confidence\":\"低|中|高\",\"evidence_used\":[\"...\"],\"quality_flags\":[\"...\"]}"
    )
    user_prompt = (
        "请基于下面 brief 写一版复盘 JSON。"
        "不要面面俱到，只挑最能解释市场故事的 2-4 个点。"
        "每个点必须回答：市场看见了什么？背后可能在交易什么？下一步用什么验证或反证？"
        "如果只有行情没有催化，直接写待验证。\n\n"
        f"## brief JSON\n\n{json.dumps(brief, ensure_ascii=False)}"
    )
    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                chat_completions_url(ai_config["base_url"]),
                headers={"Authorization": f"Bearer {ai_config['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": ai_config["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                },
            )
            response.raise_for_status()
            response_payload = response.json()
        choices = response_payload.get("choices") or []
        raw_content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        if not raw_content:
            raise ValueError("AI 返回为空")
        ai_payload = extract_json_object(raw_content)
        narrative = normalize_ai_narrative(ai_payload, brief)
        audit_issues = audit_narrative(narrative, brief)
        content = compose_ai_narrative_report(report, materials, narrative, audit_issues)
        Path(report.markdown_path).write_text(content, encoding="utf-8")
        report.content = content
        report.ai_status = "partial_error" if audit_issues else "ok"
        report.updated_at = now_utc()
        db.commit()
        db.refresh(report)
        return {
            "status": report.ai_status,
            "message": "AI 叙事复盘已生成。" if not audit_issues else "AI 叙事复盘已生成，但审稿器发现需修正项。",
            "source_status": json.loads(report.source_status_json or "[]"),
            "report": report_to_dict(report),
        }
    except Exception as exc:
        narrative = fallback_narrative_from_brief(brief)
        audit_issues = [f"AI 未完成，已生成本地编辑稿：{str(exc)[:180]}"]
        content = compose_ai_narrative_report(report, materials, narrative, audit_issues)
        Path(report.markdown_path).write_text(content, encoding="utf-8")
        report.content = content
        report.ai_status = "partial_error"
        report.updated_at = now_utc()
        db.commit()
        db.refresh(report)
        return {
            "status": "partial_error",
            "message": "AI 未完成，已生成本地编辑稿。",
            "source_status": json.loads(report.source_status_json or "[]"),
            "report": report_to_dict(report),
        }

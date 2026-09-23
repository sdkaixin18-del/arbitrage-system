from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pypinyin import lazy_pinyin
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AStock,
    FactorStockReview,
    FactorStockTag,
    FactorTag,
    WatchlistAnnouncementAiSetting,
    WatchlistAnnouncementCrawlLog,
    WatchlistAnnouncementItem,
    WatchlistAnnouncementPushLog,
    WatchlistAnnouncementStock,
    now_utc,
)
from app.notifications import bark_status, send_bark_or_log


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

CNINFO_BASE = "https://www.cninfo.com.cn"
CNINFO_STOCK_URLS = [
    ("SZ", "https://www.cninfo.com.cn/new/data/szse_stock.json"),
    ("BJ", "https://www.cninfo.com.cn/new/data/bj_stock.json"),
]
CNINFO_ANNOUNCEMENT_API = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
SSE_ANNOUNCEMENT_API = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
SSE_DISCLOSURE_BASE = "https://www.sse.com.cn"
SSE_BASE = "https://sns.sseinfo.com"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

PINYIN_ALIAS_WORDS: dict[str, tuple[str, ...]] = {
    "长": ("chang", "zhang"),
    "重": ("chong", "zhong"),
    "行": ("hang", "xing"),
    "乐": ("le", "yue"),
    "厦": ("sha", "xia"),
}
MAX_PINYIN_ALIAS_VARIANTS = 64

SOURCE_LABELS = {
    "announcement": "官方公告",
    "investor_relation": "投资者交流",
    "irm_reply": "董秘回复",
}
WATCHLIST_PUSH_GROUP = "自选股推送"
INVESTOR_RELATION_KEYWORDS = (
    "投资者关系活动记录",
    "投资者关系活动",
    "调研活动",
    "机构调研",
    "现场调研",
    "电话会议",
    "业绩说明会",
    "路演",
)
AI_PROVIDER_DEFAULTS = {
    "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
    "openai": {"model": "gpt-4.1-mini", "base_url": "https://api.openai.com/v1"},
    "custom": {"model": "", "base_url": ""},
}

_STOCK_REFRESH_TTL_SECONDS = 24 * 60 * 60
_stock_refresh_at = 0.0


@dataclass
class CandidateItem:
    source_type: str
    source_name: str
    external_id: str
    title: str
    summary: str
    source_url: str
    published_at: datetime | None


def watchlist_overview(db: Session) -> dict[str, Any]:
    stocks = list(
        db.scalars(select(WatchlistAnnouncementStock).order_by(WatchlistAnnouncementStock.exchange, WatchlistAnnouncementStock.code))
    )
    items = list(db.scalars(select(WatchlistAnnouncementItem).order_by(desc(WatchlistAnnouncementItem.crawled_at)).limit(120)))
    logs = list(db.scalars(select(WatchlistAnnouncementCrawlLog).order_by(desc(WatchlistAnnouncementCrawlLog.created_at)).limit(60)))
    push_logs = list(db.scalars(select(WatchlistAnnouncementPushLog).order_by(desc(WatchlistAnnouncementPushLog.created_at)).limit(60)))
    status = "ok" if stocks else "not_configured"
    source_statuses = source_status_summary(logs)
    if stocks and any(source["status"] == "ok" for source in source_statuses):
        source_status = "ok"
    elif stocks and any(source["status"] == "error" for source in source_statuses):
        source_status = "partial_error"
    else:
        source_status = status
    latest_at = max((item.crawled_at for item in items), default=None)
    factor_map = factor_metadata_for_full_codes(db, [stock.full_code for stock in stocks])
    return {
        "status": status,
        "source_status": source_status,
        "ai_status": ai_status(db),
        "bark_status": bark_status(),
        "updated_at": latest_at,
        "message": overview_message(stocks, items),
        "stocks": [stock_to_out(stock, factor_map.get(stock.full_code)) for stock in stocks],
        "items": [item_to_out(item) for item in items],
        "sources": source_statuses,
        "crawl_logs": [crawl_log_to_out(log) for log in logs],
        "push_logs": [push_log_to_out(log) for log in push_logs],
    }


def search_a_stocks(db: Session, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    if (db.scalar(select(func.count(AStock.id))) or 0) == 0:
        ensure_stock_universe(db)
    query = normalize_keyword(keyword)
    if not query:
        return []
    query_pinyin = "".join(lazy_pinyin(query)).upper()
    query_initials = search_initials(query)

    candidate_limit = max(limit * 8, 80)
    clauses = [
        AStock.code.like(f"{query}%"),
        AStock.full_code.like(f"{query}%"),
        AStock.name.like(f"{query}%"),
        AStock.name.like(f"%{query}%"),
    ]
    if query_pinyin:
        pinyin = query_pinyin.lower()
        clauses.extend([AStock.pinyin.like(f"{pinyin}%"), AStock.pinyin.like(f"%{pinyin}%")])
    rows = list(
        db.scalars(
            select(AStock)
            .where(or_(*clauses))
            .order_by(AStock.code, AStock.exchange)
            .limit(candidate_limit)
        )
    )
    scored: list[tuple[float, AStock]] = []
    for stock in rows:
        score = stock_match_score(stock, query, query_pinyin, query_initials)
        if score >= 42:
            scored.append((score, stock))
    scored.sort(key=lambda row: (-row[0], row[1].exchange, row[1].code))
    return [a_stock_to_out(stock, score) for score, stock in scored[:limit]]


def add_watch_stock(db: Session, query: str) -> dict[str, Any]:
    ensure_stock_universe(db)
    stock = resolve_exact_stock(db, query)
    if not stock:
        raise ValueError("请选择候选股票后添加。")

    existing = db.scalar(select(WatchlistAnnouncementStock).where(WatchlistAnnouncementStock.full_code == stock.full_code))
    if existing:
        existing.enabled = True
        existing.push_enabled = True
        existing.name = stock.name
        existing.org_id = stock.org_id
        db.commit()
        db.refresh(existing)
        return stock_to_out(existing)

    row = WatchlistAnnouncementStock(
        code=stock.code,
        name=stock.name,
        exchange=stock.exchange,
        full_code=stock.full_code,
        org_id=stock.org_id,
        enabled=True,
        push_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return stock_to_out(row)


def update_watch_stock(db: Session, full_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    stock = db.scalar(select(WatchlistAnnouncementStock).where(WatchlistAnnouncementStock.full_code == full_code.upper()))
    if not stock:
        raise ValueError("自选股不存在。")
    if "enabled" in payload and payload["enabled"] is not None:
        stock.enabled = bool(payload["enabled"])
    if "pushEnabled" in payload and payload["pushEnabled"] is not None:
        stock.push_enabled = bool(payload["pushEnabled"])
    db.commit()
    db.refresh(stock)
    return stock_to_out(stock)


def delete_watch_stock(db: Session, full_code: str) -> None:
    stock = db.scalar(select(WatchlistAnnouncementStock).where(WatchlistAnnouncementStock.full_code == full_code.upper()))
    if not stock:
        raise ValueError("自选股不存在。")
    db.delete(stock)
    db.commit()


def get_watchlist_ai_settings(db: Session) -> WatchlistAnnouncementAiSetting:
    settings = db.get(WatchlistAnnouncementAiSetting, 1)
    if settings:
        return settings
    defaults = AI_PROVIDER_DEFAULTS["deepseek"]
    settings = WatchlistAnnouncementAiSetting(
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


def watchlist_ai_settings_to_dict(db: Session) -> dict[str, Any]:
    settings = get_watchlist_ai_settings(db)
    effective = effective_watchlist_ai_config(db)
    return {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "enabled": settings.enabled,
        "api_key_configured": bool(settings.api_key),
        "api_key_masked": mask_secret(settings.api_key),
        "status": ai_status(db),
        "effective_provider": effective["provider"] if effective else None,
        "effective_model": effective["model"] if effective else None,
        "effective_base_url": effective["base_url"] if effective else None,
    }


def update_watchlist_ai_settings(db: Session, payload: dict[str, Any]) -> WatchlistAnnouncementAiSetting:
    settings = get_watchlist_ai_settings(db)
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


def crawl_watchlist_announcements(db: Session) -> dict[str, Any]:
    stocks = list(db.scalars(select(WatchlistAnnouncementStock).where(WatchlistAnnouncementStock.enabled.is_(True))))
    if not stocks:
        log_crawl(db, "all", None, "not_configured", "没有开启监控的自选股。")
        db.commit()
        return {"status": "not_configured", "message": "没有开启监控的自选股。", "fetched_count": 0, "matched_count": 0, "ignored_count": 0}

    fetched = 0
    saved = 0
    ignored = 0
    errors: list[str] = []

    def fetch_for_stock(client: httpx.Client, stock: WatchlistAnnouncementStock) -> tuple[list[CandidateItem] | Exception, list[CandidateItem] | Exception]:
        try:
            cninfo: list[CandidateItem] | Exception = fetch_cninfo_items_for_stock(client, stock)
        except Exception as exc:  # noqa: BLE001
            cninfo = exc
        try:
            replies: list[CandidateItem] | Exception = fetch_irm_reply_items_for_stock(client, stock)
        except Exception as exc:  # noqa: BLE001
            replies = exc
        return cninfo, replies

    with http_client() as client, ThreadPoolExecutor(max_workers=min(3, len(stocks))) as executor:
        futures = {executor.submit(fetch_for_stock, client, stock): stock for stock in stocks}
        results = {stock.id: future.result() for future, stock in ((future, futures[future]) for future in as_completed(futures))}
        for stock in stocks:
            cninfo_result, reply_result = results[stock.id]
            if isinstance(cninfo_result, Exception):
                exc = cninfo_result
                errors.append(f"{stock.full_code} 官方公告: {exc}")
                log_crawl(db, "announcement", stock.full_code, "error", str(exc))
                log_crawl(db, "investor_relation", stock.full_code, "error", str(exc))
            else:
                try:
                    cninfo_candidates = cninfo_result
                    source_fetched = len(cninfo_candidates)
                    source_saved = save_candidates(db, stock, cninfo_candidates)
                    source_ignored = max(0, source_fetched - source_saved)
                    fetched += source_fetched
                    saved += source_saved
                    ignored += source_ignored
                    for logical_source in ("announcement", "investor_relation"):
                        logical_fetched = len([item for item in cninfo_candidates if item.source_type == logical_source])
                        log_crawl(
                            db,
                            logical_source,
                            stock.full_code,
                            "ok",
                            f"{stock.name} {SOURCE_LABELS[logical_source]}抓取完成。",
                            logical_fetched,
                            0,
                            0,
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{stock.full_code} 官方公告入库: {exc}")
                    log_crawl(db, "announcement", stock.full_code, "error", str(exc))
                    log_crawl(db, "investor_relation", stock.full_code, "error", str(exc))

            if isinstance(reply_result, NotConfiguredSource):
                log_crawl(db, "irm_reply", stock.full_code, "not_configured", str(reply_result))
            elif isinstance(reply_result, Exception):
                errors.append(f"{stock.full_code} 董秘回复: {reply_result}")
                log_crawl(db, "irm_reply", stock.full_code, "error", str(reply_result))
            else:
                reply_candidates = reply_result
                source_fetched = len(reply_candidates)
                source_saved = save_candidates(db, stock, reply_candidates)
                source_ignored = max(0, source_fetched - source_saved)
                fetched += source_fetched
                saved += source_saved
                ignored += source_ignored
                log_crawl(
                    db,
                    "irm_reply",
                    stock.full_code,
                    "ok",
                    f"{stock.name} 董秘回复抓取完成。",
                    source_fetched,
                    source_saved,
                    source_ignored,
                )

    status = "partial_error" if errors and saved else ("error" if errors and not saved else "ok")
    message = f"自选股公告抓取完成，新增 {saved} 条，忽略重复 {ignored} 条。"
    if errors:
        message += f" {len(errors)} 个源失败。"
    log_crawl(db, "all", None, status, message, fetched, saved, ignored)
    db.commit()
    return {"status": status, "message": message, "fetched_count": fetched, "matched_count": saved, "ignored_count": ignored}


def ensure_stock_universe(db: Session) -> None:
    global _stock_refresh_at
    count = db.scalar(select(func.count(AStock.id))) or 0
    if count >= 4000 and time.time() - _stock_refresh_at < _STOCK_REFRESH_TTL_SECONDS:
        return

    rows = fetch_stock_universe()
    if not rows and count:
        return
    if not rows:
        rows = fallback_stocks()

    for row in rows:
        existing = db.scalar(select(AStock).where(AStock.full_code == row["full_code"]))
        if not existing:
            existing = db.scalar(select(AStock).where(AStock.code == row["code"]))
        if existing:
            existing.code = row["code"]
            existing.name = row["name"]
            existing.exchange = row["exchange"]
            existing.full_code = row["full_code"]
            existing.pinyin = row["pinyin"]
            existing.org_id = row.get("org_id") or existing.org_id
            existing.source = row["source"]
        else:
            db.add(AStock(**row))
    db.commit()
    _stock_refresh_at = time.time()


def fetch_stock_universe() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    with http_client() as client:
        for exchange, url in CNINFO_STOCK_URLS:
            try:
                payload = client.get(url).json()
                for item in payload.get("stockList", []):
                    if item.get("category") != "A股":
                        continue
                    code = normalize_code(item.get("code"))
                    if not code:
                        continue
                    row = build_stock_row(
                        code=code,
                        name=clean_text(item.get("zwjc") or ""),
                        exchange=exchange_from_code(code),
                        org_id=str(item.get("orgId") or "") or None,
                        source="cninfo",
                    )
                    merged[row["full_code"]] = row
            except Exception:
                continue

    try:
        import akshare as ak  # type: ignore

        df = ak.stock_info_a_code_name()
        for record in df.to_dict("records"):
            code = normalize_code(record.get("code"))
            name = clean_text(record.get("name") or "")
            if not code or not name:
                continue
            row = build_stock_row(code=code, name=name, exchange=exchange_from_code(code), org_id=None, source="akshare")
            existing = merged.get(row["full_code"])
            if existing and existing.get("org_id"):
                continue
            merged[row["full_code"]] = {**row, "org_id": existing.get("org_id") if existing else None}
    except Exception:
        pass

    return dedupe_stock_rows(list(merged.values()))


def fetch_cninfo_items_for_stock(
    client: httpx.Client,
    stock: WatchlistAnnouncementStock | AStock,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[CandidateItem]:
    query_start = start_date or datetime.now(SHANGHAI_TZ).date()
    query_end = end_date or query_start
    stock_param = f"{stock.code},{stock.org_id}" if stock.org_id else stock.code
    response = client.post(
        CNINFO_ANNOUNCEMENT_API,
        data={
            "pageNum": "1",
            "pageSize": "30",
            "column": cninfo_column(stock.exchange),
            "tabName": "fulltext",
            "stock": stock_param,
            "seDate": f"{query_start.isoformat()}~{query_end.isoformat()}",
            "isHLtitle": "true",
        },
        headers={
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        },
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("announcements") or []
    items: list[CandidateItem] = []
    for row in rows:
        title = clean_html(row.get("announcementTitle") or "")
        if not title:
            continue
        source_type = "investor_relation" if is_investor_relation_title(title) else "announcement"
        url = cninfo_announcement_url(row)
        external_id = str(row.get("announcementId") or stable_external_id(url, title))
        items.append(
            CandidateItem(
                source_type=source_type,
                source_name=SOURCE_LABELS[source_type],
                external_id=f"cninfo:{external_id}",
                title=title,
                summary=title,
                source_url=url,
                published_at=parse_cninfo_time(row.get("announcementTime")),
            )
        )
    return items


def lookup_cninfo_announcements(
    db: Session,
    code: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Query official CNINFO disclosures directly, independent of the watchlist."""
    normalized = normalize_code(code)
    if not normalized:
        return {
            "status": "missing_stock",
            "stock_code": code,
            "message": "股票代码无效，无法执行官方公告核验。",
            "matches": [],
        }
    stock = db.scalar(select(AStock).where(AStock.code == normalized))
    if stock is None:
        return {
            "status": "missing_stock",
            "stock_code": normalized,
            "message": "本地股票库未找到该代码，官方公告核验未完成。",
            "matches": [],
        }
    try:
        with http_client() as client:
            items = (
                fetch_sse_items_for_stock(client, stock, start_date=start_date, end_date=end_date)
                if stock.exchange == "SH"
                else fetch_cninfo_items_for_stock(client, stock, start_date=start_date, end_date=end_date)
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "stock_code": normalized,
            "message": f"官方公告查询失败：{clean_text(str(exc))[:300]}",
            "matches": [],
        }
    return {
        "status": "matched" if items else "not_found",
        "stock_code": normalized,
        "message": f"官方公告查询完成，命中 {len(items)} 条。" if items else "官方公告查询完成，当前日期范围未命中公告。",
        "matches": [
            {
                "title": item.title,
                "source_name": item.source_name,
                "source_url": item.source_url,
                "published_at": item.published_at,
            }
            for item in items
        ],
    }


def fetch_sse_items_for_stock(
    client: httpx.Client,
    stock: AStock | WatchlistAnnouncementStock,
    start_date: date,
    end_date: date,
) -> list[CandidateItem]:
    response = client.get(
        SSE_ANNOUNCEMENT_API,
        params={
            "isPagination": "true",
            "productId": stock.code,
            "keyWord": "",
            "securityType": "0101,120100,020100,020200,120200",
            "beginDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.endPage": "5",
        },
        headers={"Referer": "https://www.sse.com.cn/"},
    )
    response.raise_for_status()
    rows = ((response.json().get("pageHelp") or {}).get("data") or [])
    items: list[CandidateItem] = []
    for row in rows:
        title = clean_text(row.get("TITLE") or "")
        published_text = clean_text(row.get("SSEDATE") or row.get("ADDDATE") or "")[:10]
        try:
            published_day = date.fromisoformat(published_text)
        except ValueError:
            published_day = None
        if published_day is not None and not (start_date <= published_day <= end_date):
            continue
        raw_url = str(row.get("URL") or "").strip()
        if not title or not raw_url:
            continue
        url = urljoin(SSE_DISCLOSURE_BASE, raw_url)
        source_type = "investor_relation" if is_investor_relation_title(title) else "announcement"
        items.append(
            CandidateItem(
                source_type=source_type,
                source_name="上交所官方公告",
                external_id=f"sse:{stable_external_id(stock.code, title, url)}",
                title=title,
                summary=title,
                source_url=url,
                published_at=(datetime.combine(published_day, datetime.min.time(), tzinfo=SHANGHAI_TZ) if published_day else None),
            )
        )
    return items


def fetch_irm_reply_items_for_stock(client: httpx.Client, stock: WatchlistAnnouncementStock) -> list[CandidateItem]:
    if stock.exchange == "SZ":
        raise NotConfiguredSource("深交所互动易当前没有稳定公开 JSON 入口，暂不自动抓董秘回复。")
    if stock.exchange == "BJ":
        raise NotConfiguredSource("北交所董秘回复源暂未配置。")
    uid_response = client.post(
        f"{SSE_BASE}/ajax/getCompany.do",
        data={"data": stock.code},
        headers={"Referer": f"{SSE_BASE}/"},
    )
    uid_response.raise_for_status()
    uid = uid_response.text.strip()
    if not uid.isdigit():
        raise NotConfiguredSource("上证 e 互动未找到公司 uid。")

    response = client.get(
        f"{SSE_BASE}/ajax/userfeeds.do",
        params={"typeCode": "company", "type": "11", "pageSize": "10", "uid": uid, "page": "1"},
        headers={"Referer": f"{SSE_BASE}/company.do?uid={uid}"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[CandidateItem] = []
    for feed in soup.select(".m_feed_item[id]"):
        external = feed.get("id") or stable_external_id(feed.get_text(" ", strip=True), stock.full_code)
        text = clean_text(feed.get_text(" ", strip=True))
        if not text:
            continue
        items.append(
            CandidateItem(
                source_type="irm_reply",
                source_name="董秘回复",
                external_id=f"sse-e:{external}",
                title=f"{stock.name} 董秘回复",
                summary=text[:500],
                source_url=f"{SSE_BASE}/company.do?uid={uid}",
                published_at=None,
            )
        )
    return items


def save_candidates(db: Session, stock: WatchlistAnnouncementStock, candidates: list[CandidateItem]) -> int:
    saved = 0
    for candidate in candidates:
        existing = db.scalar(
            select(WatchlistAnnouncementItem).where(
                WatchlistAnnouncementItem.source_type == candidate.source_type,
                WatchlistAnnouncementItem.external_id == candidate.external_id,
            )
        )
        if existing:
            continue

        importance_level, should_push, ai_status_value, ai_reason = evaluate_importance(db, stock, candidate)
        item = WatchlistAnnouncementItem(
            watch_stock_id=stock.id,
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            full_code=stock.full_code,
            source_type=candidate.source_type,
            source_name=candidate.source_name,
            external_id=candidate.external_id,
            title=candidate.title,
            summary=candidate.summary,
            source_url=candidate.source_url,
            published_at=candidate.published_at,
            importance_level=importance_level,
            should_push=should_push,
            ai_status=ai_status_value,
            ai_reason=ai_reason,
            push_status="manual_only",
            push_message=push_message_for_ai_status(ai_status_value),
        )
        db.add(item)
        db.flush()
        if should_push:
            push_item(db, stock, item)
        saved += 1
    return saved


def evaluate_importance(
    db: Session,
    stock: WatchlistAnnouncementStock,
    candidate: CandidateItem,
) -> tuple[str, bool, str, str | None]:
    mode = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODE", "").strip().lower()
    if mode == "test":
        text = f"{candidate.title} {candidate.summary}"
        important_keywords = ("重大", "停牌", "复牌", "重组", "业绩预告", "利润分配", "回购", "减持", "增持", "合同", "诉讼")
        should_push = any(keyword in text for keyword in important_keywords)
        return ("A" if should_push else "C", should_push, "ok", "测试 AI adapter 按关键词返回判断。")
    config = effective_watchlist_ai_config(db)
    if not config:
        return "pending_ai", False, "not_configured", "AI 未配置，暂不推送。"
    try:
        result = call_importance_ai(config, stock, candidate)
        importance_level = normalize_importance_level(result.get("importance_level"))
        should_push = bool(result.get("should_push")) and importance_level == "A"
        reason = clean_text(result.get("reason") or "")[:1000] or "AI 已完成重要性判断。"
        return importance_level, should_push, "ok", reason
    except Exception as exc:  # noqa: BLE001
        return "pending_ai", False, "error", f"AI 判断失败：{clean_text(str(exc))[:500]}"


def effective_watchlist_ai_config(db: Session) -> dict[str, str] | None:
    mode = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODE", "").strip().lower()
    if mode == "test":
        return {"provider": "test", "model": "local-test", "base_url": "local", "api_key": ""}
    if mode in {"off", "none", "disabled"}:
        return None

    settings = get_watchlist_ai_settings(db)
    if settings.enabled and settings.api_key and settings.model.strip() and settings.base_url.strip():
        return {
            "provider": settings.provider,
            "model": settings.model.strip(),
            "base_url": settings.base_url.strip(),
            "api_key": settings.api_key,
        }

    return env_watchlist_ai_config()


def env_watchlist_ai_config() -> dict[str, str] | None:
    mode = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODE", "").strip().lower()
    provider_env = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_PROVIDER", "").strip().lower()
    explicit_provider = provider_env or (mode if mode in AI_PROVIDER_DEFAULTS else "")
    providers = [normalize_ai_provider(explicit_provider)] if explicit_provider else ["deepseek", "openai"]

    for provider in providers:
        config = env_config_for_provider(provider)
        if config:
            return config
    return None


def env_config_for_provider(provider: str) -> dict[str, str] | None:
    defaults = AI_PROVIDER_DEFAULTS[provider]
    model = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODEL") or defaults["model"]
    base_url = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_BASE_URL") or defaults["base_url"]
    api_key = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_API_KEY")

    if provider == "deepseek":
        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        model = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODEL") or os.environ.get("DEEPSEEK_MODEL") or model
        base_url = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL") or base_url
    elif provider == "openai":
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODEL") or os.environ.get("OPENAI_MODEL") or model
        base_url = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or base_url

    if not api_key or not model or not base_url:
        return None
    return {"provider": provider, "model": model.strip(), "base_url": base_url.strip(), "api_key": api_key.strip()}


def call_importance_ai(
    config: dict[str, str],
    stock: WatchlistAnnouncementStock,
    candidate: CandidateItem,
) -> dict[str, Any]:
    system_prompt = (
        "你是A股自选股官方信息的重要性过滤器。只判断是否需要实时推送，不做投资建议。"
        "重要推送只给真正可能影响股价或投资判断的事项：重大资产重组、停复牌、业绩预告/快报/大幅变动、"
        "利润分配、回购、增减持、定增、控制权变化、重大合同、重大诉讼处罚、核心订单或经营拐点。"
        "普通会议通知、格式化公告、日常问答、无实质增量的信息不要推送。"
        "只返回 JSON，不要返回 Markdown。字段：importance_level(A/B/C), should_push(boolean), reason(string)。"
        "A=必须实时推送，B=值得页面关注但不推送，C=普通信息。should_push 只有 A 才能为 true。"
    )
    user_payload = {
        "stock": {"name": stock.name, "code": stock.code, "full_code": stock.full_code, "exchange": stock.exchange},
        "source_type": candidate.source_type,
        "source_name": candidate.source_name,
        "title": candidate.title,
        "summary": candidate.summary,
        "source_url": candidate.source_url,
        "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(
            chat_completions_url(config["base_url"]),
            headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
            json={
                "model": config["model"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": 0,
            },
        )
        response.raise_for_status()
        payload = response.json()
    choices = payload.get("choices") or []
    content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
    if not content:
        raise ValueError("AI 返回为空")
    parsed = parse_ai_json(content)
    if "should_push" not in parsed or "importance_level" not in parsed:
        raise ValueError("AI 返回缺少必要字段")
    return parsed


def parse_ai_json(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回不是 JSON 对象")
    return parsed


def normalize_importance_level(value: Any) -> str:
    text = clean_text(value).upper()
    return text if text in {"A", "B", "C"} else "C"


def push_message_for_ai_status(status: str) -> str | None:
    if status == "not_configured":
        return "AI 未配置，内容已入库但未推送。"
    if status == "error":
        return "AI 判断失败，内容已入库但未推送。"
    return None


def push_item(db: Session, stock: WatchlistAnnouncementStock, item: WatchlistAnnouncementItem) -> None:
    status, message = send_bark_or_log(
        enabled=stock.push_enabled,
        title=WATCHLIST_PUSH_GROUP,
        body=f"{stock.name} {item.source_name}：{item.title}",
        group=WATCHLIST_PUSH_GROUP,
        url=item.source_url,
        disabled_message="该股票已关闭公告推送，内容已入库。",
        icon_url=watchlist_bark_icon_url(),
        fallback_icon_url=None,
    )
    item.push_status = status
    item.push_message = message
    item.pushed = status == "ok"
    db.add(
        WatchlistAnnouncementPushLog(
            item_id=item.id,
            full_code=stock.full_code,
            group_name=WATCHLIST_PUSH_GROUP,
            title=WATCHLIST_PUSH_GROUP,
            body=f"{stock.name} {item.source_name}：{item.title}",
            link=item.source_url,
            status=status,
            message=message,
        )
    )


def log_crawl(
    db: Session,
    source_type: str,
    full_code: str | None,
    status: str,
    message: str | None,
    fetched_count: int = 0,
    saved_count: int = 0,
    ignored_count: int = 0,
) -> None:
    db.add(
        WatchlistAnnouncementCrawlLog(
            source_type=source_type,
            full_code=full_code,
            status=status,
            message=message,
            fetched_count=fetched_count,
            saved_count=saved_count,
            ignored_count=ignored_count,
        )
    )


def source_status_summary(logs: list[WatchlistAnnouncementCrawlLog]) -> list[dict[str, Any]]:
    latest: dict[str, WatchlistAnnouncementCrawlLog] = {}
    for log in logs:
        if log.source_type in ("announcement", "investor_relation", "irm_reply") and log.source_type not in latest:
            latest[log.source_type] = log
    rows: list[dict[str, Any]] = []
    for source_type, label in SOURCE_LABELS.items():
        log = latest.get(source_type)
        rows.append(
            {
                "source_type": source_type,
                "source_name": label,
                "status": log.status if log else "not_configured",
                "message": log.message if log else None,
                "updated_at": log.created_at if log else None,
            }
        )
    return rows


def overview_message(stocks: list[WatchlistAnnouncementStock], items: list[WatchlistAnnouncementItem]) -> str:
    if not stocks:
        return "添加本地自选股后，可抓取当天官方公告、投资者交流和董秘回复。"
    return f"已监控 {len(stocks)} 只本地自选股，当前展示最近 {len(items)} 条官方信息。"


def stock_to_out(stock: WatchlistAnnouncementStock, factor_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    factor_meta = factor_meta or {"factor_tags": [], "factor_tag_status": "unreviewed"}
    return {
        "id": stock.id,
        "code": stock.code,
        "name": stock.name,
        "exchange": stock.exchange,
        "full_code": stock.full_code,
        "enabled": stock.enabled,
        "pushEnabled": stock.push_enabled,
        "factor_tags": factor_meta["factor_tags"],
        "factor_tag_status": factor_meta["factor_tag_status"],
        "created_at": stock.created_at,
        "updated_at": stock.updated_at,
    }


def factor_metadata_for_full_codes(db: Session, full_codes: list[str]) -> dict[str, dict[str, Any]]:
    if not full_codes:
        return {}
    unique_codes = sorted(set(full_codes))
    tag_rows = db.execute(
        select(FactorStockTag.full_code, FactorTag)
        .join(FactorTag, FactorTag.id == FactorStockTag.tag_id)
        .where(FactorStockTag.full_code.in_(unique_codes))
        .order_by(FactorTag.name)
    ).all()
    tag_map: dict[str, list[FactorTag]] = {}
    for full_code, tag in tag_rows:
        tag_map.setdefault(full_code, []).append(tag)
    review_rows = db.scalars(select(FactorStockReview).where(FactorStockReview.full_code.in_(unique_codes))).all()
    review_map = {row.full_code: row.status for row in review_rows}
    return {
        full_code: {
            "factor_tags": [factor_tag_to_plain(tag) for tag in tag_map.get(full_code, [])],
            "factor_tag_status": factor_status_text(review_map.get(full_code), bool(tag_map.get(full_code))),
        }
        for full_code in unique_codes
    }


def factor_tag_to_plain(tag: FactorTag) -> dict[str, Any]:
    return {
        "id": tag.id,
        "name": tag.name,
        "color": tag.color,
        "stock_count": 0,
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
    }


def factor_status_text(status: str | None, has_tags: bool) -> str:
    if status in {"unreviewed", "needs_more", "done"}:
        return status
    return "needs_more" if has_tags else "unreviewed"


def item_to_out(item: WatchlistAnnouncementItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "exchange": item.exchange,
        "full_code": item.full_code,
        "source_type": item.source_type,
        "source_name": item.source_name,
        "title": item.title,
        "summary": item.summary,
        "source_url": item.source_url,
        "published_at": item.published_at,
        "crawled_at": item.crawled_at,
        "importance_level": item.importance_level,
        "should_push": item.should_push,
        "ai_status": item.ai_status,
        "ai_reason": item.ai_reason,
        "push_status": item.push_status,
        "push_message": item.push_message,
        "pushed": item.pushed,
    }


def crawl_log_to_out(log: WatchlistAnnouncementCrawlLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "source_type": log.source_type,
        "full_code": log.full_code,
        "status": log.status,
        "message": log.message,
        "fetched_count": log.fetched_count,
        "saved_count": log.saved_count,
        "ignored_count": log.ignored_count,
        "created_at": log.created_at,
    }


def push_log_to_out(log: WatchlistAnnouncementPushLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "item_id": log.item_id,
        "full_code": log.full_code,
        "group_name": log.group_name,
        "title": log.title,
        "body": log.body,
        "link": log.link,
        "status": log.status,
        "message": log.message,
        "created_at": log.created_at,
    }


def a_stock_to_out(stock: AStock, score: float) -> dict[str, Any]:
    return {
        "code": stock.code,
        "name": stock.name,
        "exchange": stock.exchange,
        "full_code": stock.full_code,
        "pinyin": stock.pinyin,
        "score": round(score, 2),
    }


def resolve_exact_stock(db: Session, query: str) -> AStock | None:
    normalized = normalize_keyword(query)
    if not normalized:
        return None
    rows = search_a_stocks(db, normalized, limit=10)
    exact = [
        row
        for row in rows
        if normalized in (
            row["code"].upper(),
            row["full_code"].upper(),
            row["name"].upper(),
            row["pinyin"].upper(),
            pinyin_initials(row["name"]),
        )
        or normalized in pinyin_aliases(row["name"])
        or normalized in pinyin_initial_aliases(row["name"])
    ]
    if not exact:
        return None
    full_code = exact[0]["full_code"]
    return db.scalar(select(AStock).where(AStock.full_code == full_code))


def stock_match_score(stock: AStock, query: str, query_pinyin: str, query_initials: str) -> float:
    name = stock.name.upper()
    code = stock.code.upper()
    full_code = stock.full_code.upper()
    pinyin = stock.pinyin.upper()
    initials = pinyin_initials(stock.name)
    initial_aliases = pinyin_initial_aliases(stock.name)
    pinyin_name_aliases = pinyin_aliases(stock.name)
    initial_queries = {part for part in (query, query_initials) if part}
    pinyin_queries = {part for part in (query, query_pinyin) if part}
    if query == code or query == full_code:
        return 100
    if query == name:
        return 96
    if query == initials or query_initials == initials:
        return 92
    if initial_queries & set(initial_aliases):
        return 92
    if code.startswith(query) or full_code.startswith(query):
        return 88
    if initials.startswith(query) or initials.startswith(query_initials):
        return 84
    if any(alias.startswith(term) for alias in initial_aliases for term in initial_queries):
        return 84
    if query in name:
        return 78
    if query in initials or query_initials in initials:
        return 74
    if any(term in alias for alias in initial_aliases for term in initial_queries):
        return 74
    if query_pinyin == pinyin:
        return 94
    if pinyin_queries & set(pinyin_name_aliases):
        return 94
    if pinyin.startswith(query_pinyin):
        return 82
    if any(alias.startswith(term) for alias in pinyin_name_aliases for term in pinyin_queries):
        return 82
    if pinyin.startswith(query):
        return 72
    if query_pinyin in pinyin:
        return 68
    if any(term in alias for alias in pinyin_name_aliases for term in pinyin_queries):
        return 68
    if query in pinyin:
        return 62
    return SequenceMatcher(None, query, name).ratio() * 72


def pinyin_initials(value: str) -> str:
    return "".join(part[:1] for part in lazy_pinyin(value) if part).upper()


@lru_cache(maxsize=8192)
def pinyin_aliases(value: str) -> tuple[str, ...]:
    variants = [""]
    for char in value:
        default_parts = lazy_pinyin(char)
        default_part = default_parts[0] if default_parts else char
        choices = (default_part, *PINYIN_ALIAS_WORDS.get(char, ()))
        normalized_choices = tuple(dict.fromkeys(part.upper() for part in choices if part))
        if not normalized_choices:
            continue
        next_variants: list[str] = []
        for prefix in variants:
            for choice in normalized_choices:
                next_variants.append(f"{prefix}{choice}")
                if len(next_variants) >= MAX_PINYIN_ALIAS_VARIANTS:
                    break
            if len(next_variants) >= MAX_PINYIN_ALIAS_VARIANTS:
                break
        variants = next_variants
    return tuple(dict.fromkeys(variants))


@lru_cache(maxsize=8192)
def pinyin_initial_aliases(value: str) -> tuple[str, ...]:
    variants = [""]
    for char in value:
        default_parts = lazy_pinyin(char)
        default_part = default_parts[0] if default_parts else char
        choices = (default_part, *PINYIN_ALIAS_WORDS.get(char, ()))
        normalized_choices = tuple(dict.fromkeys(part[:1].upper() for part in choices if part))
        if not normalized_choices:
            continue
        next_variants: list[str] = []
        for prefix in variants:
            for choice in normalized_choices:
                next_variants.append(f"{prefix}{choice}")
                if len(next_variants) >= MAX_PINYIN_ALIAS_VARIANTS:
                    break
            if len(next_variants) >= MAX_PINYIN_ALIAS_VARIANTS:
                break
        variants = next_variants
    return tuple(dict.fromkeys(variants))


def search_initials(value: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", value):
        return pinyin_initials(value)
    return value.upper()


def build_stock_row(code: str, name: str, exchange: str, org_id: str | None, source: str) -> dict[str, Any]:
    normalized_name = clean_text(name)
    return {
        "code": code,
        "name": normalized_name,
        "exchange": exchange,
        "full_code": f"{exchange}{code}",
        "pinyin": "".join(lazy_pinyin(normalized_name)).lower(),
        "org_id": org_id,
        "source": source,
    }


def fallback_stocks() -> list[dict[str, Any]]:
    return [
        build_stock_row("300476", "胜宏科技", "SZ", "gssz0000476", "fallback"),
        build_stock_row("000001", "平安银行", "SZ", "gssz0000001", "fallback"),
        build_stock_row("600000", "浦发银行", "SH", None, "fallback"),
    ]


def dedupe_stock_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {"cninfo": 3, "akshare": 2, "fallback": 1}
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        existing = deduped.get(row["code"])
        if not existing or priority.get(row["source"], 0) > priority.get(existing["source"], 0):
            deduped[row["code"]] = row
    return list(deduped.values())


def normalize_keyword(value: Any) -> str:
    return clean_text(str(value or "")).upper().replace(" ", "")


def normalize_code(value: Any) -> str | None:
    match = re.search(r"\d{6}", str(value or ""))
    return match.group(0) if match else None


def exchange_from_code(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "SH"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BJ"
    return "SZ"


def cninfo_column(exchange: str) -> str:
    return {"SH": "sse", "BJ": "bj", "SZ": "szse"}.get(exchange, "szse")


def cninfo_announcement_url(row: dict[str, Any]) -> str:
    adjunct = str(row.get("adjunctUrl") or "").strip()
    if adjunct:
        return urljoin("https://static.cninfo.com.cn/", adjunct)
    return f"{CNINFO_BASE}/new/disclosure/detail?stockCode={row.get('secCode') or ''}&announcementId={row.get('announcementId') or ''}"


def parse_cninfo_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        raw = int(value)
        if raw > 10_000_000_000:
            raw = raw // 1000
        return datetime.fromtimestamp(raw, timezone.utc)
    except Exception:
        return None


def is_investor_relation_title(title: str) -> bool:
    return any(keyword in title for keyword in INVESTOR_RELATION_KEYWORDS)


def stable_external_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def clean_html(value: str) -> str:
    return clean_text(BeautifulSoup(html.unescape(value), "html.parser").get_text(" ", strip=True))


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def ai_status(db: Session) -> str:
    mode = os.environ.get("WATCHLIST_ANNOUNCEMENT_AI_MODE", "").strip().lower()
    if mode == "test":
        return "test"
    if mode in {"off", "none", "disabled"}:
        return "not_configured"
    return "ok" if effective_watchlist_ai_config(db) else "not_configured"


def normalize_ai_provider(provider: str | None) -> str:
    value = (provider or "deepseek").strip().lower()
    return value if value in AI_PROVIDER_DEFAULTS else "custom"


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def chat_completions_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def watchlist_bark_icon_url() -> str | None:
    value = os.environ.get("WATCHLIST_ANN_BARK_ICON_URL", "").strip()
    return value or None


def http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"}, timeout=12, follow_redirects=True)


class NotConfiguredSource(RuntimeError):
    pass

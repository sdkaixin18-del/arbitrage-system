from __future__ import annotations

import html
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from itertools import combinations
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import (
    ExchangeAnnouncementChangeLog,
    ExchangeAnnouncementPushLog,
    ExchangeAnnouncementTimeline,
    ExchangeDelistingOpportunityAlertLog,
    ExchangeDelistingOpportunityMute,
    ExchangeDelistingOpportunityPairExclusion,
    ExchangeDelistingOpportunitySnapshot,
    ExchangeDelistingOpportunityWatch,
)
from app.notifications import bark_status, send_bark_or_log


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

BINANCE_ARTICLE_API = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
BYBIT_ANNOUNCEMENT_API = "https://api.bybit.com/v5/announcements/index"
BYBIT_INSTRUMENTS_API = "https://api.bybit.com/v5/market/instruments-info"
ASTER_ANNOUNCEMENT_API = (
    "https://www.asterdex.com/bapi/composite/v1/public/composite/ae/announcement/search"
)
ASTER_FUTURES_EXCHANGE_INFO_API = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"

BITGET_CATEGORY_URLS = [
    ("listing", "spot", "https://www.bitget.com/zh-CN/support/sections/5955813039257"),
    ("listing", "contract", "https://www.bitget.com/zh-CN/support/sections/12508313405000"),
    ("delisting", "unknown", "https://www.bitget.com/zh-CN/support/sections/12508313443290"),
    ("unknown", "unknown", "https://www.bitget.com/zh-CN/support/sections/12508313443483"),
]

OKX_SECTION_URLS = [
    # The global Simplified-Chinese route is intermittently served a stale
    # regional article list.  en-ar is the unrestricted official catalogue and
    # still provides exact publishTime values in the same SSR payload.
    ("listing", "spot", "https://www.okx.com/en-ar/help/section/announcements-new-listings"),
    ("delisting", "spot", "https://www.okx.com/en-ar/help/section/announcements-delistings"),
    ("unknown", "unknown", "https://www.okx.com/en-ar/help/section/announcements-trading-updates"),
    ("unknown", "unknown", "https://www.okx.com/en-ar/help/section/announcements-latest-announcements"),
]

GATE_SECTION_URLS = [
    ("listing", "unknown", "https://www.gate.com/zh/announcements/newlisted"),
    ("delisting", "unknown", "https://www.gate.com/zh/announcements/delisted"),
    ("unknown", "unknown", "https://www.gate.com/zh/announcements"),
]
GATE_FALLBACK_ORIGINS = (
    "https://www.gate.tv",
    "https://web.gate.it",
)

EXCHANGE_LABELS = {
    "bn": "Binance",
    "bg": "Bitget",
    "by": "Bybit",
    "gate": "Gate",
    "okx": "OKX",
    "aster": "Aster",
    "hl": "Hyperliquid",
}

CONTRACT_KEYWORDS = (
    "futures",
    "future",
    "perpetual",
    "perp",
    "contract",
    "usdt-m",
    "usdⓈ-m",
    "usd-m",
    "coin-m",
    "合约",
    "永续",
    "永續",
    "交割",
    "期货",
    "u本位",
    "币本位",
)
SPOT_KEYWORDS = ("spot", "trading pair", "trading pairs", "现货", "現貨", "币对", "幣對")
MARGIN_LOAN_KEYWORDS = ("margin", "loan", "杠杆", "槓桿", "借币", "借幣")
DELISTING_KEYWORDS = (
    "delist",
    "remove",
    "terminate",
    "cease trading",
    "discontinue",
    "下架",
    "移除",
    "停止交易",
)
LISTING_KEYWORDS = (
    "will list",
    "to list",
    "new listing",
    "launch",
    "opens",
    "add",
    "listed",
    "上线",
    "上線",
    "上架",
    "上市",
    "新增",
    "開放",
)
SUSPENSION_KEYWORDS = (
    "suspend trading",
    "suspension of trading",
    "trading suspension",
    "halt trading",
    "cease trading temporarily",
    "暂停交易",
    "暫停交易",
    "交易暂停",
    "交易暫停",
)
RESUMPTION_KEYWORDS = (
    "resume trading",
    "resumption of trading",
    "trading resumes",
    "恢复交易",
    "恢復交易",
    "重新开放交易",
    "重新開放交易",
)
MIGRATION_KEYWORDS = (
    "token migration",
    "contract migration",
    "ticker change",
    "symbol change",
    "token swap",
    "redenomination",
    "rename",
    "renaming",
    "migration",
    "更名",
    "迁移",
    "遷移",
    "置换",
    "置換",
    "代币兑换",
    "代幣兌換",
)
PARAMETER_CHANGE_KEYWORDS = (
    "funding rate",
    "funding interval",
    "funding cap",
    "funding floor",
    "maximum leverage",
    "max leverage",
    "position limit",
    "position tier",
    "risk limit",
    "tick size",
    "price precision",
    "order size precision",
    "minimum order",
    "min order",
    "contract size",
    "contract multiplier",
    "settlement rule",
    "资金费",
    "資金費",
    "杠杆倍数",
    "槓桿倍數",
    "最大杠杆",
    "最大槓桿",
    "仓位限额",
    "倉位限額",
    "持仓限额",
    "持倉限額",
    "仓位档位",
    "倉位檔位",
    "风险限额",
    "風險限額",
    "价格精度",
    "價格精度",
    "数量精度",
    "數量精度",
    "最小下单",
    "最小下單",
    "最小委托",
    "最小委託",
    "合约面值",
    "合約面值",
    "合约乘数",
    "合約乘數",
    "结算规则",
    "結算規則",
)
MONITORED_ACTIONS = {
    "listing",
    "delisting",
    "suspension",
    "resumption",
    "migration",
    "parameter_change",
}
ACTION_LABELS = {
    "listing": "上架",
    "delisting": "下架",
    "suspension": "暂停交易",
    "resumption": "恢复交易",
    "migration": "改名/迁移",
    "parameter_change": "合约参数变更",
}
STABLE_QUOTE_KEYWORDS = ("usdt", "usdⓈ", "usdⓢ", "usds")
TARGET_CONTRACT_QUOTES = {"usdt", "usds"}
NON_TARGET_CONTRACT_MARKETS = {"contract_usd", "contract_usdc"}
SYMBOL_EXCLUDE = {
    "AI",
    "API",
    "BTC",
    "ETF",
    "IPO",
    "USD",
    "USDT",
    "USDC",
    "BINANCE",
    "BITGET",
    "BYBIT",
    "GATE",
    "OKX",
    "ASTER",
    "BN",
    "BG",
    "BY",
    "DEFI",
    "GAMEFI",
    "SOLANA",
    "STABLECOIN",
    "PRE",
    "STOCK",
    "AND",
    "OR",
    "PERPETUAL",
    "FUTURE",
    "FUTURES",
    "CONTRACT",
    "CONTRACTS",
    "EQUITY",
    "EQUITIES",
    "STOCKS",
    "SPOT",
    "TRADING",
    "LIST",
    "LAUNCH",
}

_CACHE_TTL_SECONDS = 45
_CACHE_STALE_SECONDS = 6 * 60 * 60
_FETCH_BUDGET_SECONDS = 10
_MARKET_STATUS_CACHE_SECONDS = 300
_MARKET_STATUS_ERROR_RETRY_SECONDS = 15
_MARKET_STATUS_FETCH_BUDGET_SECONDS = 12
_OPPORTUNITY_WATCH_HOURS = 48
DELISTING_LOOKAHEAD_DAYS = 14
DELISTING_PUBLISHED_LOOKBACK_DAYS = 14
LISTING_LOOKAHEAD_DAYS = 2
LISTING_PUBLISHED_LOOKBACK_DAYS = 14
GENERAL_EVENT_LOOKAHEAD_DAYS = 14
GENERAL_EVENT_LOOKBACK_DAYS = 2
GENERAL_EVENT_PUBLISHED_LOOKBACK_DAYS = 14
_cache: dict[str, Any] | None = None
_cache_at = 0.0
_SOURCE_REFRESH_SECONDS = {
    "bn": 60,
    "by": 60,
    "aster": 60,
    "bg": 60,
    "gate": 60,
    "okx": 60,
    "hl": 60,
}
_source_cache_lock = threading.Lock()
_source_cache: dict[str, dict[str, Any]] = {}
_market_status_cache: dict[tuple[str, str], dict[str, Any]] | None = None
_market_status_cache_at = 0.0
_market_status_cache_lock = threading.Lock()
_market_status_refresh_lock = threading.Lock()
_monitor_logger_lock = threading.Lock()
_monitor_logger: logging.Logger | None = None
_monitor_state_lock = threading.Lock()
_monitor_event_states: dict[tuple[str, str], str] = {}
_inventory_state_lock = threading.Lock()
BEIJING_TZ = timezone(timedelta(hours=8))
MARKET_STATUS_EXCHANGES = (
    ("bn", "bn", "Binance"),
    ("bg", "bg", "Bitget"),
    ("by", "by", "Bybit"),
    ("gate", "gt", "Gate"),
    ("okx", "okx", "OKX"),
    ("aster", "as", "Aster"),
    ("hl", "hl", "Hyperliquid"),
)
MARKET_STATUS_MARKET_TYPES = {"aster": ("contract",), "hl": ("contract",)}
OPPORTUNITY_EXCHANGE_ORDER = {"bn": 0, "bg": 1, "by": 2, "gt": 3, "okx": 4, "as": 5, "hl": 6}


def exchange_monitor_log_path() -> Path:
    configured = os.environ.get("STOCK_REVIEW_LOG_DIR")
    log_dir = Path(configured).expanduser() if configured else Path.home() / "Library" / "Logs" / "stock-review-mac"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "exchange-announcements.log"


def exchange_monitor_logger() -> logging.Logger:
    global _monitor_logger
    if _monitor_logger is not None:
        return _monitor_logger
    with _monitor_logger_lock:
        if _monitor_logger is not None:
            return _monitor_logger
        logger = logging.getLogger("stock-review.exchange-announcements")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = TimedRotatingFileHandler(
                exchange_monitor_log_path(),
                when="midnight",
                backupCount=30,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        _monitor_logger = logger
    return _monitor_logger


def exchange_monitor_log(
    event: str,
    *,
    level: str = "info",
    exc_info: bool = False,
    **fields: Any,
) -> None:
    payload = {
        "at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "event": event,
        **{key: sanitize_monitor_value(value) for key, value in fields.items()},
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        logger = exchange_monitor_logger()
        log_method = logger.error if level == "error" else (logger.warning if level == "warning" else logger.info)
        log_method(serialized, exc_info=exc_info)
    except Exception:
        print(f"exchange announcement monitor log fallback: {serialized}", flush=True)


def exchange_monitor_log_on_change(
    event: str,
    *,
    state_key: str = "global",
    level: str = "info",
    exc_info: bool = False,
    state_fields: dict[str, Any] | None = None,
    **fields: Any,
) -> bool:
    """Write repetitive monitor events only when their operational state changes.

    Run identifiers and timings remain useful in the emitted record, but they do
    not define a state change.  Callers can provide the compact state explicitly
    so routine polling does not flood the announcement log.
    """

    normalized_state = sanitize_monitor_value(state_fields if state_fields is not None else fields)
    signature = json.dumps(normalized_state, ensure_ascii=False, sort_keys=True, default=str)
    identity = (event, str(state_key))
    with _monitor_state_lock:
        if _monitor_event_states.get(identity) == signature:
            return False
        _monitor_event_states[identity] = signature
    exchange_monitor_log(event, level=level, exc_info=exc_info, **fields)
    return True


def sanitize_monitor_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_monitor_text(value)
    if isinstance(value, dict):
        return {str(key): sanitize_monitor_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_monitor_value(item) for item in value]
    return value


def redact_monitor_text(value: str) -> str:
    text = value
    for env_key in ("BARK_WEBHOOK_URL", "BARK_URL"):
        secret = os.environ.get(env_key)
        if secret:
            text = text.replace(secret, "<redacted-bark-url>")
    text = re.sub(r"https?://[^\s]+", "<redacted-url>", text)
    text = re.sub(
        r"(?i)(api[_-]?key|secret|token|signature|sign)=([^&\s]+)",
        r"\1=<redacted>",
        text,
    )
    return text
EN_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
EVENT_TIME_STRONG_KEYWORDS = (
    "现货交易",
    "現貨交易",
    "合约交易",
    "合約交易",
    "永续合约交易",
    "永續合約交易",
    "标准永续合约",
    "標準永續合約",
    "交易开放",
    "交易開放",
    "开放交易",
    "開放交易",
    "开始交易",
    "開始交易",
    "open spot trading",
    "spot trading opens",
    "spot trading will open",
    "open perpetual futures trading",
    "perpetual futures trading opens",
    "perpetual futures trading will open",
    "standard perpetual futures",
)
EVENT_TIME_KEYWORDS = (
    "交易",
    "开放",
    "開放",
    "开盘",
    "開盤",
    "开始",
    "開始",
    "上线",
    "上線",
    "上架",
    "新增",
    "trading",
    "open",
    "opens",
    "launch",
    "listing",
    "convert",
)
ASTER_STOCK_SYMBOL_OVERRIDES = {"CXMT"}
NON_EVENT_TIME_KEYWORDS = (
    "发布",
    "發布",
    "公告",
    "published",
    "announcement",
    "充值",
    "充币",
    "充幣",
    "提现",
    "提币",
    "提幣",
    "deposit",
    "withdraw",
    "withdrawal",
    "集合竞价",
    "集合競價",
    "call auction",
)


def get_exchange_announcements(
    force_refresh: bool = False,
    db: Session | None = None,
    monitor_run_id: str | None = None,
) -> dict[str, Any]:
    global _cache, _cache_at
    run_id = monitor_run_id or uuid.uuid4().hex[:12]
    started_at = time.perf_counter()
    now = time.time()
    if not force_refresh and _cache and now - _cache_at < _CACHE_TTL_SECONDS:
        return with_push_context(dict(_cache), db)

    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    fetchers = [
        ("bn", fetch_binance),
        ("bg", fetch_bitget),
        ("by", fetch_bybit),
        ("gate", fetch_gate),
        ("okx", fetch_okx),
        ("aster", fetch_aster_announcements),
        ("hl", fetch_hyperliquid_market_events),
    ]

    source_results = fetch_announcement_sources(fetchers, force_refresh=force_refresh)
    for exchange, fetched, source_state in source_results:
        items.extend(fetched)
        source_status = str(source_state.get("status") or "error")
        source_message = source_state.get("message")
        exchange_monitor_log_on_change(
            "announcement_source_result",
            state_key=exchange,
            run_id=run_id,
            exchange=exchange,
            status=source_status,
            raw_count=len(fetched),
            error=source_message,
            cache_hit=source_state.get("cache_hit"),
            duration_ms=source_state.get("duration_ms"),
            last_success_at=source_state.get("last_success_at"),
            consecutive_failures=source_state.get("consecutive_failures"),
            state_fields={
                "status": source_status,
                "error": source_message,
                "stale": bool(source_state.get("stale")),
            },
        )
        sources.append(
            {
                "exchange": exchange,
                "exchange_name": EXCHANGE_LABELS[exchange],
                "status": source_status,
                "count": 0,
                "raw_count": len(fetched),
                "expired_count": 0,
                "message": source_message,
                **{
                    key: source_state.get(key)
                    for key in (
                        "last_success_at",
                        "last_attempt_at",
                        "age_seconds",
                        "duration_ms",
                        "refresh_interval_seconds",
                        "next_refresh_seconds",
                        "consecutive_failures",
                        "cache_hit",
                        "stale",
                    )
                },
            }
        )

    raw_items = dedupe_items(items)
    propagate_stock_asset_classification(raw_items)
    filter_diagnostics: dict[str, int] = {}
    active_items, expired_count = filter_active_announcements(raw_items, diagnostics=filter_diagnostics)
    source_map = {source["exchange"]: source for source in sources}
    for item in active_items:
        source_map[item["exchange"]]["count"] += 1
    for source in sources:
        source["expired_count"] = max(0, source.get("raw_count", 0) - source["count"])

    active_items.sort(key=lambda row: row.get("event_at_sort") or row.get("published_at_sort") or 0)
    merged_items = merge_announcements(active_items)
    merged_items.sort(key=lambda row: row.get("event_at_sort") or row.get("latest_published_at_sort") or 0)
    for row in merged_items:
        row["push_key"] = announcement_push_key(row)
    for row in merged_items:
        row.pop("latest_published_at_sort", None)
        row.pop("event_at_sort", None)
    for row in active_items:
        row.pop("published_at_sort", None)
        row.pop("event_at_sort", None)

    error_count = len([source for source in sources if source["status"] != "ok"])
    status = "error" if error_count == len(sources) else ("partial_error" if error_count else "ok")
    displayed = min(len(merged_items), 120)
    message = (
        f"已抓取 {len(raw_items)} 条原始公告，过滤过期/非监控窗口 {expired_count} 条，"
        f"合并为 {len(merged_items)} 条监控公告，显示 {displayed} 条。"
    )
    if error_count:
        message += f" {error_count} 个交易所源暂时不可用。"
    exchange_monitor_log_on_change(
        "announcement_fetch_completed",
        run_id=run_id,
        status=status,
        raw_count=len(raw_items),
        active_count=len(active_items),
        filtered_count=expired_count,
        merged_count=len(merged_items),
        source_error_count=error_count,
        filter_reasons=filter_diagnostics,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        state_fields={
            "status": status,
            "raw_count": len(raw_items),
            "active_count": len(active_items),
            "filtered_count": expired_count,
            "merged_count": len(merged_items),
            "source_error_count": error_count,
            "filter_reasons": filter_diagnostics,
        },
    )
    if error_count == len(sources) and not force_refresh and _cache and now - _cache_at < _CACHE_STALE_SECONDS:
        exchange_monitor_log_on_change(
            "announcement_stale_cache_used",
            run_id=run_id,
            cache_age_seconds=round(now - _cache_at, 3),
            cached_item_count=len(_cache.get("items") or []),
            level="warning",
            state_fields={
                "source_error_count": error_count,
                "cached_item_count": len(_cache.get("items") or []),
            },
        )
        stale_payload = dict(_cache)
        stale_payload["status"] = "error"
        stale_payload["source_status"] = "error"
        stale_payload["message"] = f"{message} 已先显示本地缓存。"
        return with_push_context(stale_payload, db)

    _cache = {
        "status": status,
        "source_status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": merged_items[:120],
        "sources": sources,
        "message": message,
    }
    _cache_at = now
    return with_push_context(dict(_cache), db)


def fetch_sources(fetchers: list[tuple[str, Any]]) -> list[tuple[str, list[dict[str, Any]], str | None]]:
    results: dict[str, tuple[str, list[dict[str, Any]], str | None]] = {}
    worker_limit = max(1, min(int(os.environ.get("EXCHANGE_ANN_FETCH_WORKERS", "3")), len(fetchers)))
    executor = ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="exchange-ann")
    futures = {executor.submit(fetcher): exchange for exchange, fetcher in fetchers}
    try:
        for future in as_completed(futures, timeout=_FETCH_BUDGET_SECONDS):
            exchange = futures[future]
            try:
                results[exchange] = (exchange, future.result(), None)
            except Exception as exc:
                results[exchange] = (exchange, [], str(exc))
    except TimeoutError:
        pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    for exchange, _fetcher in fetchers:
        if exchange not in results:
            results[exchange] = (exchange, [], f"读取超时（>{_FETCH_BUDGET_SECONDS}s）")
    return [results[exchange] for exchange, _fetcher in fetchers]


def source_refresh_seconds(exchange: str) -> int:
    default = int(_SOURCE_REFRESH_SECONDS.get(exchange, 120))
    key = f"EXCHANGE_ANN_SOURCE_{exchange.upper()}_SECONDS"
    try:
        configured = int(os.environ.get(key, str(default)))
    except ValueError:
        configured = default
    return max(30, min(configured, 600))


def fetch_announcement_sources(
    fetchers: list[tuple[str, Any]],
    *,
    force_refresh: bool,
) -> list[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    """Refresh only due sources and expose freshness without hiding failures."""

    now_monotonic = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    with _source_cache_lock:
        cached = {key: dict(value) for key, value in _source_cache.items()}

    durations: dict[str, float] = {}
    timed_fetchers: list[tuple[str, Any]] = []
    for exchange, fetcher in fetchers:
        state = cached.get(exchange) or {}
        interval = source_refresh_seconds(exchange)
        last_attempt_monotonic = float(state.get("last_attempt_monotonic") or 0.0)
        due = force_refresh or not state or now_monotonic - last_attempt_monotonic >= interval
        if not due:
            continue

        def timed_fetch(current_fetcher=fetcher, current_exchange=exchange):
            started = time.perf_counter()
            try:
                return current_fetcher()
            finally:
                durations[current_exchange] = round((time.perf_counter() - started) * 1000, 1)

        timed_fetchers.append((exchange, timed_fetch))

    fetched_results = fetch_sources(timed_fetchers) if timed_fetchers else []
    fetched_by_exchange = {
        exchange: (rows, error)
        for exchange, rows, error in fetched_results
    }
    results: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
    with _source_cache_lock:
        for exchange, _fetcher in fetchers:
            interval = source_refresh_seconds(exchange)
            state = dict(_source_cache.get(exchange) or cached.get(exchange) or {})
            fetched = fetched_by_exchange.get(exchange)
            cache_hit = fetched is None
            if fetched is not None:
                rows, error = fetched
                state["last_attempt_at"] = now_utc.isoformat()
                state["last_attempt_monotonic"] = now_monotonic
                state["duration_ms"] = durations.get(exchange)
                if error is None:
                    state["items"] = list(rows)
                    state["last_success_at"] = now_utc.isoformat()
                    state["last_success_monotonic"] = now_monotonic
                    state["consecutive_failures"] = 0
                    state["last_error"] = None
                else:
                    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                    state["last_error"] = error
            rows = list(state.get("items") or [])
            last_success_monotonic = float(state.get("last_success_monotonic") or 0.0)
            age_seconds = (
                max(0.0, now_monotonic - last_success_monotonic)
                if last_success_monotonic
                else None
            )
            stale = age_seconds is None or age_seconds > max(180, interval * 3)
            failures = int(state.get("consecutive_failures") or 0)
            if state.get("last_error"):
                status = "partial_error" if rows else "error"
                message = str(state.get("last_error"))
            elif stale:
                status = "partial_error" if rows else "error"
                message = "公告源已超过新鲜度阈值，正在继续重试。"
            else:
                status = "ok"
                message = None
            last_attempt_monotonic = float(state.get("last_attempt_monotonic") or 0.0)
            next_refresh_seconds = max(
                0,
                round(interval - max(0.0, now_monotonic - last_attempt_monotonic)),
            ) if last_attempt_monotonic else 0
            state["refresh_interval_seconds"] = interval
            _source_cache[exchange] = state
            results.append(
                (
                    exchange,
                    rows,
                    {
                        "status": status,
                        "message": message,
                        "last_success_at": state.get("last_success_at"),
                        "last_attempt_at": state.get("last_attempt_at"),
                        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                        "duration_ms": state.get("duration_ms"),
                        "refresh_interval_seconds": interval,
                        "next_refresh_seconds": next_refresh_seconds,
                        "consecutive_failures": failures,
                        "cache_hit": cache_hit,
                        "stale": stale,
                    },
                )
            )
    return results


def announcement_timeline_key(item: dict[str, Any], action: str) -> str:
    base = announcement_push_key(item)
    return f"{base}:listing" if action == "listing" else base


def _timeline_action(item: dict[str, Any]) -> str | None:
    actions = {
        str(detail.get("action") or "")
        for detail in item.get("announcements") or []
    }
    if "listing" in actions:
        return "listing"
    if "delisting" in actions:
        return "delisting"
    return None


def _to_utc(value: Any) -> datetime | None:
    parsed = parse_event_datetime(value)
    return parsed.astimezone(timezone.utc) if parsed else None


def _same_persisted_value(current: Any, desired: Any) -> bool:
    if isinstance(current, datetime) or isinstance(desired, datetime):
        def normalize_datetime(value: Any) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return (
                    value.replace(tzinfo=timezone.utc)
                    if value.tzinfo is None
                    else value.astimezone(timezone.utc)
                )
            return _to_utc(value)

        return normalize_datetime(current) == normalize_datetime(desired)
    return current == desired


def _assign_if_changed(target: Any, field: str, value: Any) -> bool:
    if _same_persisted_value(getattr(target, field), value):
        return False
    setattr(target, field, value)
    return True


def _detail_market_live(
    detail: dict[str, Any],
    symbol: str,
    snapshot: dict[tuple[str, str], dict[str, Any]] | None,
) -> bool:
    if not snapshot:
        return False
    market_type = "spot" if detail.get("market_type") == "spot" else "contract"
    source = snapshot.get((str(detail.get("exchange") or ""), market_type)) or {}
    return source.get("status") == "ok" and symbol in set(source.get("symbols") or set())


def sync_announcement_timelines(
    db: Session,
    items: list[dict[str, Any]],
    snapshot: dict[tuple[str, str], dict[str, Any]] | None,
) -> int:
    """Persist discovery and handoff milestones for current announcements."""

    now = datetime.now(timezone.utc)
    symbols = {
        symbol
        for item in items
        for symbol in opportunity_symbols(item)
    }
    try:
        from app.astro_spread_scanner import astro_listing_linkage_status

        linkage = astro_listing_linkage_status(symbols)
    except Exception as exc:
        linkage = {}
        exchange_monitor_log_on_change(
            "announcement_astro_linkage_unavailable",
            level="warning",
            error=str(exc),
            state_fields={"error": str(exc)},
        )

    updated = 0
    for item in items:
        action = _timeline_action(item)
        if action is None:
            continue
        details = [
            detail
            for detail in item.get("announcements") or []
            if detail.get("action") == action
        ]
        key = announcement_timeline_key(item, action)
        published_candidates = [
            value
            for value in (_to_utc(detail.get("published_at")) for detail in details)
            if value is not None
        ]
        published_at = max(published_candidates, default=_to_utc(item.get("latest_published_at")))
        event_at = _to_utc(item.get("event_at"))
        for symbol in opportunity_symbols(item):
            timeline = db.scalar(
                select(ExchangeAnnouncementTimeline).where(
                    ExchangeAnnouncementTimeline.announcement_key == key,
                    ExchangeAnnouncementTimeline.symbol == symbol,
                )
            )
            changed = False
            if timeline is None:
                timeline = ExchangeAnnouncementTimeline(
                    announcement_key=key,
                    symbol=symbol,
                    detected_at=now,
                )
                db.add(timeline)
                changed = True
            if published_at is not None:
                changed = _assign_if_changed(timeline, "published_at", published_at) or changed
            if event_at is not None:
                changed = _assign_if_changed(timeline, "event_at", event_at) or changed
            market_live = action == "listing" and any(
                _detail_market_live(detail, symbol, snapshot) for detail in details
            )
            if market_live and timeline.market_opened_at is None:
                timeline.market_opened_at = now
                changed = True

            current = linkage.get(symbol) or {}
            astro_status = str(current.get("status") or "waiting_market")
            astro_proves_market_open = astro_status in {
                "registered",
                "direct_checking",
                "direct_rejected",
                "submitted_for_card_sync",
                "card_created",
            }
            if action == "listing" and astro_proves_market_open and timeline.market_opened_at is None:
                timeline.market_opened_at = (
                    _to_utc(current.get("registeredAt"))
                    or _to_utc(current.get("firstDirectCheckAt"))
                    or _to_utc(current.get("cardCreatedAt"))
                    or now
                )
                changed = True
            if action != "listing":
                changed = _assign_if_changed(timeline, "astro_status", "not_applicable") or changed
                changed = _assign_if_changed(
                    timeline,
                    "astro_reason",
                    "下架公告不进入上新重点监控链路。",
                ) or changed
            elif astro_status != "waiting_market":
                changed = _assign_if_changed(timeline, "astro_status", astro_status) or changed
                changed = _assign_if_changed(
                    timeline,
                    "astro_reason",
                    str(current.get("reason") or "") or None,
                ) or changed
                registered_at = _to_utc(current.get("registeredAt"))
                if registered_at is not None:
                    changed = _assign_if_changed(
                        timeline,
                        "astro_registered_at",
                        registered_at,
                    ) or changed
                first_direct_check_at = _to_utc(current.get("firstDirectCheckAt"))
                if first_direct_check_at is not None:
                    changed = _assign_if_changed(
                        timeline,
                        "first_direct_check_at",
                        first_direct_check_at,
                    ) or changed
            elif market_live:
                changed = _assign_if_changed(timeline, "astro_status", "market_opened") or changed
                changed = _assign_if_changed(
                    timeline,
                    "astro_reason",
                    "交易所市场已开放，等待Pulse路线或直连候选。",
                ) or changed
            else:
                changed = _assign_if_changed(timeline, "astro_status", "waiting_market") or changed
                changed = _assign_if_changed(
                    timeline,
                    "astro_reason",
                    "上架市场尚未开放可用行情。",
                ) or changed
            if changed:
                timeline.updated_at = now
                updated += 1
    if updated:
        db.flush()
    return updated


def timeline_to_out(
    timeline: ExchangeAnnouncementTimeline | None,
    live_linkage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if timeline is None:
        return None
    live_linkage = live_linkage or {}
    live_status = str(live_linkage.get("status") or "")
    astro_status = (
        live_status
        if live_status and live_status != "waiting_market"
        else timeline.astro_status
    )
    return {
        "publishedAt": utc_datetime_to_beijing_iso(timeline.published_at),
        "detectedAt": utc_datetime_to_beijing_iso(timeline.detected_at),
        "pushedAt": utc_datetime_to_beijing_iso(timeline.pushed_at),
        "eventAt": utc_datetime_to_beijing_iso(timeline.event_at),
        "marketOpenedAt": utc_datetime_to_beijing_iso(timeline.market_opened_at),
        "astroRegisteredAt": (
            utc_datetime_to_beijing_iso(live_linkage.get("registeredAt"))
            or utc_datetime_to_beijing_iso(timeline.astro_registered_at)
        ),
        "firstDirectCheckAt": (
            utc_datetime_to_beijing_iso(live_linkage.get("firstDirectCheckAt"))
            or utc_datetime_to_beijing_iso(timeline.first_direct_check_at)
        ),
        "lastDirectCheckAt": utc_datetime_to_beijing_iso(
            live_linkage.get("lastDirectCheckAt")
        ),
        "astroStatus": astro_status,
        "astroReason": str(live_linkage.get("reason") or timeline.astro_reason or "") or None,
    }


def change_log_to_out(change: ExchangeAnnouncementChangeLog) -> dict[str, Any]:
    return {
        "id": change.id,
        "announcementKey": change.announcement_key,
        "symbol": change.symbol,
        "changeType": change.change_type,
        "summary": change.summary,
        "before": json.loads(change.before_json) if change.before_json else None,
        "after": json.loads(change.after_json) if change.after_json else None,
        "createdAt": utc_datetime_to_beijing_iso(change.created_at),
    }


def attach_announcement_context(payload: dict[str, Any], db: Session) -> None:
    items = list(payload.get("items") or [])
    symbols = {symbol for item in items for symbol in opportunity_symbols(item)}
    try:
        from app.astro_spread_scanner import astro_listing_linkage_status

        linkage = astro_listing_linkage_status(symbols)
    except Exception:
        linkage = {}
    timelines = list(db.scalars(select(ExchangeAnnouncementTimeline)))
    timeline_map = {
        (timeline.announcement_key, timeline.symbol): timeline
        for timeline in timelines
    }
    for item in items:
        action = _timeline_action(item)
        if action is None:
            item["timeline"] = None
            item["astro_linkage"] = None
            continue
        key = announcement_timeline_key(item, action)
        item_symbols = opportunity_symbols(item)
        rows = [
            timeline_to_out(timeline_map.get((key, symbol)), linkage.get(symbol))
            for symbol in item_symbols
        ]
        rows = [row for row in rows if row]
        item["timeline"] = rows[0] if rows else None
        item["astro_linkage"] = linkage.get(item_symbols[0]) if item_symbols else None

    changes = list(
        db.scalars(
            select(ExchangeAnnouncementChangeLog)
            .order_by(desc(ExchangeAnnouncementChangeLog.created_at))
            .limit(20)
        )
    )
    payload["announcement_changes"] = [change_log_to_out(change) for change in changes]


def with_push_context(payload: dict[str, Any], db: Session | None) -> dict[str, Any]:
    if db is None:
        payload.update(
            {
                "push_status": exchange_push_status(),
                "push_message": exchange_push_message(),
                "bark_status": bark_status(),
                "push_logs": [],
                "opportunities": {
                    "status": "not_configured",
                    "updated_at": None,
                    "items": [],
                    "watch_count": 0,
                    "muted_symbols": [],
                    "message": "后台机会监控数据未加载。",
                    "formula": "2×(卖出盘口价−买入盘口价)/(卖出盘口价+买入盘口价)",
                    "alert_threshold_pct": exchange_opportunity_alert_pct(),
                    "alert_cooldown_minutes": exchange_opportunity_cooldown_minutes(),
                    "high_alert_threshold_pct": exchange_opportunity_high_alert_pct(),
                    "high_alert_cooldown_minutes": exchange_opportunity_high_cooldown_minutes(),
                    "critical_alert_threshold_pct": exchange_opportunity_critical_alert_pct(),
                    "critical_alert_cooldown_minutes": exchange_opportunity_critical_cooldown_minutes(),
                    "scan_interval_seconds": exchange_opportunity_interval_seconds(),
                },
            }
        )
        return payload
    logs = list(
        db.scalars(
            select(ExchangeAnnouncementPushLog)
            .where(ExchangeAnnouncementPushLog.visible.is_(True))
            .order_by(desc(ExchangeAnnouncementPushLog.created_at))
            .limit(40)
        )
    )
    attach_announcement_context(payload, db)
    payload.update(
        {
            "push_status": exchange_push_status(),
            "push_message": exchange_push_message(),
            "bark_status": bark_status(),
            "push_logs": [push_log_to_out(log) for log in logs],
            "opportunities": delisting_opportunities_overview(db),
        }
    )
    return payload


def clear_exchange_announcement_push_logs(db: Session) -> dict[str, Any]:
    logs = list(
        db.scalars(
            select(ExchangeAnnouncementPushLog).where(ExchangeAnnouncementPushLog.visible.is_(True))
        )
    )
    for log in logs:
        log.visible = False
    db.commit()
    count = len(logs)
    return {
        "status": "ok",
        "message": f"已清除 {count} 条推送日志；去重记录已保留，不会重复推送旧公告。",
        "cleared_count": count,
    }


def exchange_push_enabled() -> bool:
    return os.environ.get("EXCHANGE_ANN_PUSH_ENABLED", "1") == "1"


def exchange_push_status() -> str:
    if not exchange_push_enabled():
        return "manual_only"
    status = bark_status()
    return "not_configured" if status == "error" else status


def exchange_push_message() -> str:
    if not exchange_push_enabled():
        return "交易所公告推送已关闭。"
    if exchange_push_status() != "ok":
        return "Bark 未配置，交易所公告会入库但不会发到手机。"
    return "交易所公告推送已开启。"


def exchange_push_interval_seconds() -> int:
    # The content sources keep their own per-exchange cache, so a 15-second
    # scheduler does not scrape every announcement page every 15 seconds.  It
    # does, however, let a just-opened contract market leave the pending state
    # quickly.
    return max(15, int(os.environ.get("EXCHANGE_ANN_PUSH_SECONDS", "15")))


def exchange_bark_icon_url() -> str:
    configured = os.environ.get("EXCHANGE_ANN_BARK_ICON_URL")
    if configured:
        return configured
    base_url = os.environ.get("FRONTEND_PUBLIC_BASE_URL") or os.environ.get("FRONTEND_BASE_URL") or default_frontend_base_url()
    return f"{base_url.rstrip('/')}/exchange-announcement-logo.png"


def default_frontend_base_url() -> str:
    host = local_lan_ip() or "127.0.0.1"
    port = os.environ.get("FRONTEND_PORT", "5173")
    return f"http://{host}:{port}"


def local_lan_ip() -> str | None:
    configured = os.environ.get("FRONTEND_LAN_IP")
    if configured:
        return configured
    ifconfig_ip = local_lan_ip_from_ifconfig()
    if ifconfig_ip:
        return ifconfig_ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def local_lan_ip_from_ifconfig() -> str | None:
    for interface in ("en0", "en1"):
        try:
            result = subprocess.run(["ifconfig", interface], capture_output=True, text=True, timeout=1, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"\binet\s+((?:\d{1,3}\.){3}\d{1,3})\b", result.stdout)
        if match and not match.group(1).startswith("127."):
            return match.group(1)
    return None


def exchange_market_status_snapshot(
    force_refresh: bool = False,
    monitor_run_id: str | None = None,
    refresh_keys: set[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    global _market_status_cache, _market_status_cache_at
    run_id = monitor_run_id or uuid.uuid4().hex[:12]
    started_at = time.perf_counter()
    now = time.time()
    with _market_status_cache_lock:
        previous_snapshot = copy_market_status_snapshot(_market_status_cache or {})
        cache_ttl = market_status_cache_ttl(previous_snapshot)
        if (
            not force_refresh
            and _market_status_cache
            and now - _market_status_cache_at < cache_ttl
        ):
            return copy_market_status_snapshot(_market_status_cache)

    # Avoid two schedulers starting the same full symbol-list refresh.  A
    # caller with a usable snapshot receives it immediately while the current
    # refresh finishes; the first-ever caller waits for the result.
    if not _market_status_refresh_lock.acquire(blocking=not bool(previous_snapshot)):
        return previous_snapshot
    try:
        return _fetch_exchange_market_status_snapshot(
            run_id=run_id,
            started_at=started_at,
            previous_snapshot=previous_snapshot,
            refresh_keys=refresh_keys,
            force_refresh=force_refresh,
        )
    finally:
        _market_status_refresh_lock.release()


def market_status_cache_ttl(
    snapshot: dict[tuple[str, str], dict[str, Any]],
) -> int:
    if any(str(value.get("status") or "error") != "ok" for value in snapshot.values()):
        return _MARKET_STATUS_ERROR_RETRY_SECONDS
    return _MARKET_STATUS_CACHE_SECONDS


def _fetch_exchange_market_status_snapshot(
    *,
    run_id: str,
    started_at: float,
    previous_snapshot: dict[tuple[str, str], dict[str, Any]],
    refresh_keys: set[tuple[str, str]] | None,
    force_refresh: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    global _market_status_cache, _market_status_cache_at
    from app.crypto import (
        EXCHANGE_ANNOUNCEMENT_API_LANE,
        api_rate_limit_lane,
        fetch_exchange_market_symbols,
        http_client,
    )

    specs = [
        (announcement_exchange, crypto_exchange, market_type)
        for announcement_exchange, crypto_exchange, _exchange_name in MARKET_STATUS_EXCHANGES
        for market_type in MARKET_STATUS_MARKET_TYPES.get(
            announcement_exchange,
            ("spot", "contract"),
        )
    ]
    # A targeted refresh is used while a newly listed contract is around its
    # launch time.  On cold start we still build one complete snapshot so the
    # new market can be paired with existing exchanges.
    if refresh_keys and previous_snapshot:
        specs = [
            spec
            for spec in specs
            if (spec[0], spec[2]) in refresh_keys
        ]
    results: dict[tuple[str, str], dict[str, Any]] = {}

    def fetch_one(crypto_exchange: str, market_type: str) -> set[str]:
        crypto_market_type = "spot" if market_type == "spot" else "futures"
        # This context is intentionally inside the worker: the lane selector
        # is thread-local.  Announcement checks no longer wait behind Astro's
        # continuously active critical section.
        with api_rate_limit_lane(EXCHANGE_ANNOUNCEMENT_API_LANE):
            with http_client(timeout=8.0) as client:
                return fetch_exchange_market_symbols(client, crypto_exchange, crypto_market_type)

    worker_limit = max(
        1,
        min(int(os.environ.get("EXCHANGE_MARKET_STATUS_WORKERS", "8")), len(specs)),
    )
    executor = ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="exchange-market-status")
    futures = {
        executor.submit(fetch_one, crypto_exchange, market_type): (announcement_exchange, market_type)
        for announcement_exchange, crypto_exchange, market_type in specs
    }
    try:
        for future in as_completed(futures, timeout=_MARKET_STATUS_FETCH_BUDGET_SECONDS):
            key = futures[future]
            try:
                symbols = set(future.result())
                results[key] = {"status": "ok", "symbols": symbols, "message": None}
                exchange_monitor_log_on_change(
                    "market_status_source_result",
                    state_key=f"{key[0]}:{key[1]}",
                    run_id=run_id,
                    exchange=key[0],
                    market_type=key[1],
                    status="ok",
                    symbol_count=len(symbols),
                    state_fields={"status": "ok", "symbol_count": len(symbols)},
                )
            except Exception as exc:
                results[key] = market_status_failure_state(
                    key,
                    str(exc),
                    previous_snapshot,
                )
                exchange_monitor_log_on_change(
                    "market_status_source_result",
                    state_key=f"{key[0]}:{key[1]}",
                    run_id=run_id,
                    exchange=key[0],
                    market_type=key[1],
                    status="error",
                    symbol_count=0,
                    error=str(exc),
                    level="warning",
                    state_fields={"status": "error", "error": str(exc)},
                )
    except TimeoutError:
        pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    for announcement_exchange, _crypto_exchange, market_type in specs:
        key = (announcement_exchange, market_type)
        if key not in results:
            message = f"读取超时（>{_MARKET_STATUS_FETCH_BUDGET_SECONDS}s）"
            results[key] = market_status_failure_state(
                key,
                message,
                previous_snapshot,
            )
            exchange_monitor_log_on_change(
                "market_status_source_result",
                state_key=f"{key[0]}:{key[1]}",
                run_id=run_id,
                exchange=key[0],
                market_type=key[1],
                status="error",
                symbol_count=0,
                error=message,
                level="warning",
                state_fields={"status": "error", "error": message},
            )

    merged_results = copy_market_status_snapshot(previous_snapshot)
    merged_results.update(results)
    with _market_status_cache_lock:
        _market_status_cache = copy_market_status_snapshot(merged_results)
        _market_status_cache_at = time.time()
    exchange_monitor_log_on_change(
        "market_status_fetch_completed",
        run_id=run_id,
        ok_count=sum(1 for value in merged_results.values() if value.get("status") == "ok"),
        stale_count=sum(1 for value in merged_results.values() if value.get("status") == "stale"),
        error_count=sum(1 for value in merged_results.values() if value.get("status") == "error"),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        state_fields={
            "ok_count": sum(1 for value in merged_results.values() if value.get("status") == "ok"),
            "stale_count": sum(1 for value in merged_results.values() if value.get("status") == "stale"),
            "error_count": sum(1 for value in merged_results.values() if value.get("status") == "error"),
        },
    )
    return copy_market_status_snapshot(merged_results)


def market_status_failure_state(
    key: tuple[str, str],
    message: str,
    previous_snapshot: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    previous = previous_snapshot.get(key) or {}
    previous_symbols = set(previous.get("symbols") or set())
    if previous_symbols:
        return {
            "status": "stale",
            "symbols": previous_symbols,
            "message": f"{message}；暂用上次成功数据，15秒后重试",
        }
    return {"status": "error", "symbols": set(), "message": message}


def copy_market_status_snapshot(
    snapshot: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        key: {
            **value,
            "symbols": set(value.get("symbols") or set()),
        }
        for key, value in snapshot.items()
    }


def symbol_market_statuses(
    symbol: str,
    snapshot: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_symbol = normalize_group_symbol(symbol)
    rows: list[dict[str, Any]] = []
    for announcement_exchange, _crypto_exchange, exchange_name in MARKET_STATUS_EXCHANGES:
        row: dict[str, Any] = {
            "exchange": announcement_exchange,
            "exchange_name": exchange_name,
        }
        supported_market_types = MARKET_STATUS_MARKET_TYPES.get(
            announcement_exchange,
            ("spot", "contract"),
        )
        for market_type in ("spot", "contract"):
            if market_type not in supported_market_types:
                row[market_type] = False
                row[f"{market_type}_status"] = "unsupported"
                row[f"{market_type}_message"] = "该市场类型暂未接入"
                continue
            state = snapshot.get((announcement_exchange, market_type)) or {}
            status = str(state.get("status") or "error")
            row[market_type] = (
                normalized_symbol in set(state.get("symbols") or set())
                if status in {"ok", "stale"}
                else None
            )
            row[f"{market_type}_status"] = status
            row[f"{market_type}_message"] = state.get("message")
        rows.append(row)
    return rows


def build_exchange_announcement_push_body(
    item: dict[str, Any],
    snapshot: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> str:
    title = str(item.get("title") or build_summary_title(item))
    statuses = symbol_market_statuses(
        str(item.get("symbol") or ""),
        snapshot if snapshot is not None else exchange_market_status_snapshot(),
    )
    status_by_exchange = {row["exchange"]: row for row in statuses}
    lines = [title]
    is_stock_asset = item.get("asset_type") == "stock"
    announced_stock_routes = {
        (
            str(detail.get("exchange") or ""),
            str(detail.get("market_type") or ""),
        )
        for detail in item.get("announcements") or []
        if detail.get("market_type") in {"spot", "contract"}
    }

    change_summaries = announcement_change_summaries(item)
    if change_summaries:
        lines.append(f"变动：{'；'.join(change_summaries)}")

    current_routes: list[str] = []
    missing_exchanges: list[str] = []
    failed_checks: list[str] = []
    for row in statuses:
        if is_stock_asset and not any(
            exchange == row["exchange"] for exchange, _market_type in announced_stock_routes
        ):
            continue
        markets: list[str] = []
        check_spot = not is_stock_asset or (row["exchange"], "spot") in announced_stock_routes
        check_contract = (
            not is_stock_asset or (row["exchange"], "contract") in announced_stock_routes
        )
        if check_spot and row["spot"] is True:
            markets.append("现货")
        if check_contract and row["contract"] is True:
            markets.append("合约")
        if markets:
            current_routes.append(f"{row['exchange_name']} {'+'.join(markets)}")
        checked_values = [
            value
            for enabled, value in (
                (check_spot, row["spot"]),
                (check_contract, row["contract"]),
            )
            if enabled
        ]
        if checked_values and all(value is False for value in checked_values):
            missing_exchanges.append(str(row["exchange_name"]))
        if check_spot and row["spot"] is None:
            failed_checks.append(f"{row['exchange_name']}现货")
        if check_contract and row["contract"] is None:
            failed_checks.append(f"{row['exchange_name']}合约")

    route_label = (
        "公告对应市场（股票/指数需核验底层与报价单位）"
        if is_stock_asset
        else "当前可交易（USDT）"
    )
    if current_routes:
        lines.append(f"{route_label}：{'｜'.join(current_routes)}")
    elif failed_checks:
        lines.append(f"{route_label}：已核验市场均未查到，部分接口失败")
    else:
        lines.append(f"{route_label}：已接入交易所均未查到现货或合约")
    if missing_exchanges:
        lines.append(f"未查到USDT市场：{'/'.join(missing_exchanges)}")

    conflicts = announcement_market_conflicts(item, status_by_exchange)
    if conflicts:
        lines.append(f"核验差异：{'；'.join(conflicts)}")
    if failed_checks:
        lines.append(f"状态核验失败：{'/'.join(failed_checks)}")
    return "\n".join(lines)


def is_funding_interval_notification_suppressed(item: dict[str, Any]) -> bool:
    """Keep funding-cycle notices in history without sending a notification.

    A grouped item is suppressed only when every announcement in the group is
    a contract parameter change about the funding interval. This prevents a
    same-symbol listing, delisting or other material change from being hidden.
    """

    details = list(item.get("announcements") or [])
    if not details:
        return False

    def is_interval_change(detail: dict[str, Any]) -> bool:
        if str(detail.get("action") or "") != "parameter_change":
            return False
        if str(detail.get("market_type") or "") not in {
            "contract",
            "contract_usd",
            "contract_usdc",
        }:
            return False
        title = str(detail.get("title") or "").lower()
        funding_context = any(token in title for token in ("funding", "资金费", "資金費"))
        interval_context = any(
            token in title
            for token in (
                "interval",
                "cycle",
                "frequency",
                "周期",
                "週期",
                "结算间隔",
                "結算間隔",
            )
        )
        return funding_context and interval_context

    return all(is_interval_change(detail) for detail in details)


def announcement_change_summaries(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for detail in item.get("announcements") or []:
        action = str(detail.get("action") or "")
        if action not in MONITORED_ACTIONS:
            continue
        exchange_name = str(detail.get("exchange_name") or detail.get("exchange") or "未知交易所")
        market_name = announcement_market_name(detail)
        event_dt = parse_event_datetime(detail.get("event_at"))
        if event_dt:
            occurred = event_dt <= datetime.now(BEIJING_TZ)
            time_label = event_dt.strftime("%Y-%m-%d %H:%M")
            if action == "delisting":
                summary = f"{exchange_name}{market_name}{'已于' if occurred else '将于'}{time_label}下架"
            elif action == "listing":
                summary = f"{exchange_name}{market_name}{'已于' if occurred else '将于'}{time_label}上架"
            else:
                summary = (
                    f"{exchange_name}{market_name}"
                    f"{'已于' if occurred else '将于'}{time_label}"
                    f"{ACTION_LABELS.get(action, '发生变更')}"
                )
        else:
            summary = (
                f"{exchange_name}{market_name}已公告"
                f"{ACTION_LABELS.get(action, '发生变更')}，时间待核验"
            )
        if summary not in result:
            result.append(summary)
    return result


def announcement_market_conflicts(
    item: dict[str, Any],
    status_by_exchange: dict[str, dict[str, Any]],
) -> list[str]:
    result: list[str] = []
    now = datetime.now(BEIJING_TZ)
    for detail in item.get("announcements") or []:
        market_type = str(detail.get("market_type") or "")
        if market_type not in {"spot", "contract"}:
            continue
        action = str(detail.get("action") or "")
        event_dt = parse_event_datetime(detail.get("event_at"))
        if action not in {"listing", "delisting"} or not event_dt:
            continue
        status = status_by_exchange.get(str(detail.get("exchange") or ""))
        if not status:
            continue
        is_live = status.get(market_type)
        if is_live is None:
            continue
        exchange_name = str(detail.get("exchange_name") or detail.get("exchange") or "未知交易所")
        market_name = announcement_market_name(detail)
        summary: str | None = None
        if action == "delisting" and event_dt <= now and is_live:
            summary = f"{exchange_name}{market_name}公告已到下架时间，但实时接口仍显示可交易"
        elif action == "delisting" and event_dt > now and not is_live:
            summary = f"{exchange_name}{market_name}尚未到下架时间，但实时接口已不显示交易"
        elif action == "listing" and event_dt <= now and not is_live:
            summary = f"{exchange_name}{market_name}公告已到上架时间，但实时接口仍未显示交易"
        elif action == "listing" and event_dt > now and is_live:
            summary = f"{exchange_name}{market_name}尚未到上架时间，但实时接口已显示可交易"
        if summary and summary not in result:
            result.append(summary)
    return result


def announcement_market_name(detail: dict[str, Any]) -> str:
    market_type = str(detail.get("market_type") or "")
    if market_type == "spot":
        return "现货"
    if market_type == "contract":
        return "合约"
    if market_type == "margin_loan":
        return "杠杆/借币"
    label = str(detail.get("market_label") or "")
    return label if label and label != "未分类" else "相关市场"


def exchange_opportunity_interval_seconds() -> int:
    return max(60, int(os.environ.get("EXCHANGE_OPPORTUNITY_SCAN_SECONDS", "120")))


def exchange_opportunity_alert_pct() -> float:
    return max(0.1, float(os.environ.get("EXCHANGE_OPPORTUNITY_ALERT_PCT", "1.0")))


def exchange_opportunity_cooldown_minutes() -> int:
    return max(5, int(os.environ.get("EXCHANGE_OPPORTUNITY_COOLDOWN_MINUTES", "30")))


def exchange_opportunity_high_alert_pct() -> float:
    return max(
        exchange_opportunity_alert_pct(),
        float(os.environ.get("EXCHANGE_OPPORTUNITY_HIGH_ALERT_PCT", "5.0")),
    )


def exchange_opportunity_high_cooldown_minutes() -> int:
    return max(1, int(os.environ.get("EXCHANGE_OPPORTUNITY_HIGH_COOLDOWN_MINUTES", "5")))


def exchange_opportunity_critical_alert_pct() -> float:
    return max(
        exchange_opportunity_high_alert_pct(),
        float(os.environ.get("EXCHANGE_OPPORTUNITY_CRITICAL_ALERT_PCT", "10.0")),
    )


def exchange_opportunity_critical_cooldown_minutes() -> int:
    return max(1, int(os.environ.get("EXCHANGE_OPPORTUNITY_CRITICAL_COOLDOWN_MINUTES", "1")))


def opportunity_alert_policy(spread: float | None) -> tuple[str, int] | None:
    if spread is None or spread <= exchange_opportunity_alert_pct():
        return None
    if spread > exchange_opportunity_critical_alert_pct():
        return ("critical", exchange_opportunity_critical_cooldown_minutes())
    if spread > exchange_opportunity_high_alert_pct():
        return ("high", exchange_opportunity_high_cooldown_minutes())
    return ("normal", exchange_opportunity_cooldown_minutes())


def opportunity_crypto_exchange(exchange: str) -> str:
    return {"gate": "gt", "aster": "as"}.get(exchange, exchange)


def opportunity_exchange_name(exchange: str) -> str:
    normalized = opportunity_crypto_exchange(exchange)
    for announcement_exchange, crypto_exchange, name in MARKET_STATUS_EXCHANGES:
        if normalized in {announcement_exchange, crypto_exchange}:
            return name
    return normalized.upper()


def opportunity_route_label(route: dict[str, str]) -> str:
    market_label = "现货" if route["market_type"] == "spot" else "合约"
    return f"{opportunity_exchange_name(route['exchange'])}{market_label}"


def opportunity_symbols(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in str(item.get("symbol") or "").split("/"):
        symbol = normalize_group_symbol(value)
        if symbol and symbol != "未识别" and symbol not in result:
            result.append(symbol)
    return result


def opportunity_details_for_symbol(
    item: dict[str, Any],
    symbol: str,
    action: str | None = None,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for detail in item.get("announcements") or []:
        if action is not None and detail.get("action") != action:
            continue
        if detail.get("action") not in {"listing", "delisting"}:
            continue
        symbols = [normalize_group_symbol(value) for value in detail.get("symbols") or []]
        if symbols and symbol not in symbols:
            continue
        details.append(detail)
    return details


def delisting_details_for_symbol(item: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    return opportunity_details_for_symbol(item, symbol, "delisting")


def remaining_routes_for_symbol(
    symbol: str,
    statuses: list[dict[str, Any]],
    delisting_details: list[dict[str, Any]],
) -> list[dict[str, str]]:
    excluded_exchanges = {
        opportunity_crypto_exchange(str(detail.get("exchange") or ""))
        for detail in delisting_details
        if detail.get("market_type") in {"spot", "contract"}
    }
    routes: list[dict[str, str]] = []
    for row in statuses:
        exchange = opportunity_crypto_exchange(str(row["exchange"]))
        if row.get("contract") is True and exchange not in excluded_exchanges:
            routes.append({"exchange": exchange, "market_type": "futures", "symbol": symbol})
    routes.sort(
        key=lambda route: (
            OPPORTUNITY_EXCHANGE_ORDER.get(route["exchange"], 99),
        )
    )
    return routes


def supported_opportunity_route_pairs(
    routes: list[dict[str, str]],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    return [
        (left, right)
        for left, right in combinations(routes, 2)
        if left["market_type"] == "futures" and right["market_type"] == "futures"
    ]


def opportunity_routes_action(routes: list[dict[str, str]]) -> str:
    return next(
        (
            str(route.get("event_action"))
            for route in routes
            if route.get("event_action") in {"listing", "delisting"}
        ),
        "delisting",
    )


def opportunity_watch_action(watch: ExchangeDelistingOpportunityWatch) -> str:
    if str(watch.announcement_key or "").endswith(":listing"):
        return "listing"
    return opportunity_routes_action(load_opportunity_routes(watch))


def supported_opportunity_watch_pairs(
    routes: list[dict[str, str]],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    pairs = supported_opportunity_route_pairs(routes)
    if opportunity_routes_action(routes) != "listing":
        return pairs
    return [
        (left, right)
        for left, right in pairs
        if left.get("event_source") == "1" or right.get("event_source") == "1"
    ]


def _listing_route_event_metadata(
    route: dict[str, Any], details: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep discovery priority separate from a verified per-market launch time."""

    matching = [
        detail for detail in details
        if opportunity_crypto_exchange(str(detail.get("exchange") or "")) == route.get("exchange")
        and {"spot": "spot", "contract": "futures"}.get(str(detail.get("market_type") or ""))
        == route.get("market_type")
    ]
    times = [parse_event_datetime(detail.get("event_at")) for detail in matching]
    known = {value.astimezone(timezone.utc).isoformat() for value in times if value is not None}
    # The aggregate item's timestamp and watch.event_at may be fallbacks or
    # belong to another market. Never turn those into volume-bypass evidence.
    time_known = bool(matching) and all(value is not None for value in times) and len(known) == 1
    return {
        "event_source": "1" if matching else "0",
        "listingEventAt": next(iter(known)) if time_known else None,
        "listingEventTimeKnown": time_known,
    }


def sync_delisting_opportunity_watches(
    db: Session,
    items: list[dict[str, Any]],
    snapshot: dict[tuple[str, str], dict[str, Any]],
    run_id: str,
) -> int:
    now = datetime.now(timezone.utc)
    synced = 0
    for item in items:
        if not any(
            detail.get("action") in {"listing", "delisting"}
            for detail in item.get("announcements") or []
        ):
            continue
        if item.get("asset_type") == "stock":
            base_announcement_key = announcement_push_key(item)
            stock_watch_keys = {base_announcement_key, f"{base_announcement_key}:listing"}
            disabled_symbols: list[str] = []
            stock_watches = list(
                db.scalars(
                    select(ExchangeDelistingOpportunityWatch).where(
                        ExchangeDelistingOpportunityWatch.announcement_key.in_(stock_watch_keys),
                    )
                )
            )
            for watch in stock_watches:
                was_enabled = watch.enabled
                changed = _assign_if_changed(watch, "enabled", False)
                changed = _assign_if_changed(watch, "status", "unsupported_asset") or changed
                changed = _assign_if_changed(watch, "last_error", (
                    "股票/指数需核验底层、合约乘数与报价单位，未建映射前停止通用跨所配对。"
                )) or changed
                if changed:
                    watch.updated_at = now
                if was_enabled:
                    disabled_symbols.append(watch.symbol)
            if disabled_symbols:
                exchange_monitor_log(
                    "opportunity_item_skipped",
                    run_id=run_id,
                    symbols=opportunity_symbols(item),
                    asset_type="stock",
                    reason="stock_symbol_identity_requires_mapping",
                    disabled_symbols=disabled_symbols,
                )
            continue
        for symbol in opportunity_symbols(item):
            for action in ("delisting", "listing"):
                details = opportunity_details_for_symbol(item, symbol, action)
                if not details:
                    continue
                all_delistings = [
                    detail
                    for candidate in items
                    if symbol in opportunity_symbols(candidate)
                    for detail in opportunity_details_for_symbol(
                        candidate,
                        symbol,
                        "delisting",
                    )
                    if detail.get("market_type") in {"spot", "contract"}
                ]
                statuses = symbol_market_statuses(symbol, snapshot)
                routes = remaining_routes_for_symbol(
                    symbol,
                    statuses,
                    all_delistings,
                )
                for route in routes:
                    route["event_action"] = action
                    route["event_source"] = "0"
                    route["pending"] = "0"
                # A contract-listing announcement is itself enough to start a
                # direct watch.  The announced leg may not appear in the
                # exchange inventory until the exact launch second, so keep a
                # pending source route instead of waiting for a later full
                # announcement cycle to discover it.
                if action == "listing":
                    for detail in details:
                        if detail.get("market_type") != "contract":
                            continue
                        source_exchange = opportunity_crypto_exchange(
                            str(detail.get("exchange") or "")
                        )
                        if not source_exchange or any(
                            route["exchange"] == source_exchange
                            and route["market_type"] == "futures"
                            for route in routes
                        ):
                            continue
                        routes.append(
                            {
                                "exchange": source_exchange,
                                "market_type": "futures",
                                "symbol": symbol,
                                "event_action": action,
                                "event_source": "1",
                                "pending": "1",
                            }
                        )
                    for route in routes:
                        route.update(_listing_route_event_metadata(route, details))
                event_dt = parse_event_datetime(item.get("event_at"))
                event_utc = event_dt.astimezone(timezone.utc) if event_dt else now
                expires_at = max(
                    now + timedelta(hours=1),
                    event_utc + timedelta(hours=_OPPORTUNITY_WATCH_HOURS),
                )
                source_detail = next(
                    (detail for detail in details if detail.get("market_type") == "contract"),
                    details[0],
                )
                base_announcement_key = announcement_push_key(item)
                announcement_key = (
                    base_announcement_key
                    if action == "delisting"
                    else f"{base_announcement_key}:listing"
                )
                symbol_watches = list(
                    db.scalars(
                        select(ExchangeDelistingOpportunityWatch)
                        .where(ExchangeDelistingOpportunityWatch.symbol == symbol)
                        .order_by(desc(ExchangeDelistingOpportunityWatch.updated_at))
                    )
                )
                watch = next(
                    (row for row in symbol_watches if row.announcement_key == announcement_key),
                    None,
                )
                if watch is None:
                    watch = next(
                        (
                            row
                            for row in symbol_watches
                            if row.enabled
                            and opportunity_watch_action(row) == action
                        ),
                        None,
                    )
                if watch is None:
                    watch = ExchangeDelistingOpportunityWatch(
                        announcement_key=announcement_key,
                        symbol=symbol,
                        source_exchange=opportunity_crypto_exchange(
                            str(source_detail.get("exchange") or "")
                        ),
                        source_market_type=(
                            "spot" if source_detail.get("market_type") == "spot" else "futures"
                        ),
                        event_at=event_utc,
                        expires_at=expires_at,
                        routes_json="[]",
                    )
                    db.add(watch)
                    watch_changed = True
                else:
                    watch_changed = False
                for stale_watch in symbol_watches:
                    if stale_watch is watch:
                        continue
                    if opportunity_watch_action(stale_watch) != action:
                        continue
                    stale_changed = _assign_if_changed(stale_watch, "enabled", False)
                    stale_changed = _assign_if_changed(stale_watch, "status", "superseded") or stale_changed
                    if stale_changed:
                        stale_watch.updated_at = now
                watch_changed = _assign_if_changed(watch, "announcement_key", announcement_key) or watch_changed
                watch_changed = _assign_if_changed(watch, "source_exchange", opportunity_crypto_exchange(
                    str(source_detail.get("exchange") or "")
                )) or watch_changed
                watch_changed = _assign_if_changed(watch, "source_market_type", (
                    "spot" if source_detail.get("market_type") == "spot" else "futures"
                )) or watch_changed
                watch_changed = _assign_if_changed(watch, "event_at", event_utc) or watch_changed
                watch_changed = _assign_if_changed(
                    watch,
                    "routes_json",
                    json.dumps(routes, ensure_ascii=False, separators=(",", ":")),
                ) or watch_changed
                watch_changed = _assign_if_changed(watch, "expires_at", expires_at) or watch_changed
                watch_changed = _assign_if_changed(watch, "enabled", True) or watch_changed
                pair_count = len(supported_opportunity_watch_pairs(routes))
                source_live = any(
                    route.get("event_source") == "1" and route.get("pending") != "1"
                    for route in routes
                )
                if action == "listing" and not source_live:
                    next_status = "waiting_source_market"
                    next_error = "上架市场尚未开放行情，继续等待交易所市场接口生效。"
                else:
                    next_status = "watch" if pair_count else "insufficient_routes"
                    next_error = (
                        None
                        if pair_count
                        else "当前市场暂不能组成两个合约市场的跨所价差对。"
                    )
                watch_changed = _assign_if_changed(watch, "status", next_status) or watch_changed
                watch_changed = _assign_if_changed(watch, "last_error", next_error) or watch_changed
                if watch_changed:
                    watch.updated_at = now
                    synced += 1
                    exchange_monitor_log(
                        "opportunity_watch_synced",
                        run_id=run_id,
                        symbol=symbol,
                        action=action,
                        route_count=len(routes),
                        pair_count=pair_count,
                        routes=[opportunity_route_label(route) for route in routes],
                        status=watch.status,
                        expires_at=expires_at,
                    )
    return synced


def load_opportunity_routes(watch: ExchangeDelistingOpportunityWatch) -> list[dict[str, Any]]:
    try:
        rows = json.loads(watch.routes_json or "[]")
    except json.JSONDecodeError:
        return []
    return [
        {
            "exchange": opportunity_crypto_exchange(str(row.get("exchange") or "")),
            "market_type": "spot" if row.get("market_type") == "spot" else "futures",
            "symbol": normalize_group_symbol(str(row.get("symbol") or watch.symbol)),
            "event_action": (
                str(row.get("event_action"))
                if row.get("event_action") in {"listing", "delisting"}
                else "delisting"
            ),
            "event_source": "1" if str(row.get("event_source") or "") == "1" else "0",
            "pending": "1" if str(row.get("pending") or "") == "1" else "0",
            "listingEventAt": row.get("listingEventAt") if isinstance(row.get("listingEventAt"), str) else None,
            "listingEventTimeKnown": row.get("listingEventTimeKnown") is True,
        }
        for row in rows
        if isinstance(row, dict) and row.get("exchange")
    ]


def canonical_opportunity_pair(
    left: dict[str, str],
    right: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    if left["market_type"] == "futures" and right["market_type"] == "spot":
        return right, left
    if left["market_type"] == right["market_type"]:
        left_order = OPPORTUNITY_EXCHANGE_ORDER.get(left["exchange"], 99)
        right_order = OPPORTUNITY_EXCHANGE_ORDER.get(right["exchange"], 99)
        if right_order < left_order:
            return right, left
    return left, right


def opportunity_pair_type(left: dict[str, str], right: dict[str, str]) -> str:
    if left["market_type"] == "spot" and right["market_type"] == "futures":
        return "SF"
    if left["market_type"] == "spot" and right["market_type"] == "spot":
        return "SS"
    return "FF"


def opportunity_pair_key(symbol: str, left: dict[str, str], right: dict[str, str]) -> str:
    return ":".join(
        [
            symbol,
            left["exchange"],
            left["market_type"],
            right["exchange"],
            right["market_type"],
        ]
    )


def quote_mid(quote: Any) -> float | None:
    if quote.best_bid is None or quote.best_ask is None:
        return None
    if quote.best_bid <= 0 or quote.best_ask <= 0:
        return None
    return (quote.best_bid + quote.best_ask) / 2


def build_opportunity_snapshot(
    watch: ExchangeDelistingOpportunityWatch,
    left_route: dict[str, str],
    right_route: dict[str, str],
    left_quote: Any,
    right_quote: Any,
) -> ExchangeDelistingOpportunitySnapshot:
    from app.crypto import fs_astro_spread_rate

    left_route, right_route = canonical_opportunity_pair(left_route, right_route)
    if left_quote.exchange != left_route["exchange"] or left_quote.market_type != left_route["market_type"]:
        left_quote, right_quote = right_quote, left_quote
    errors = [
        f"{opportunity_route_label(route)}：{quote.error or '行情不可用'}"
        for route, quote in ((left_route, left_quote), (right_route, right_quote))
        if quote.status != "ok" or quote.best_bid is None or quote.best_ask is None
    ]
    reference = fs_astro_spread_rate(quote_mid(left_quote), quote_mid(right_quote))
    sell_left = fs_astro_spread_rate(left_quote.best_bid, right_quote.best_ask)
    sell_right = fs_astro_spread_rate(right_quote.best_bid, left_quote.best_ask)
    reference_pct = reference * 100 if reference is not None else None
    sell_left_pct = sell_left * 100 if sell_left is not None else None
    sell_right_pct = sell_right * 100 if sell_right is not None else None
    directional = [
        (sell_left_pct, f"卖出{opportunity_route_label(left_route)} / 买入{opportunity_route_label(right_route)}"),
        (sell_right_pct, f"卖出{opportunity_route_label(right_route)} / 买入{opportunity_route_label(left_route)}"),
    ]
    valid_directional = [(spread, direction) for spread, direction in directional if spread is not None]
    best_spread, direction = max(
        valid_directional,
        default=(None, None),
        key=lambda row: row[0] if row[0] is not None else -10**9,
    )
    threshold = exchange_opportunity_alert_pct()
    status = "error" if errors else ("signal_unverified" if best_spread is not None and best_spread > threshold else "watch")
    return ExchangeDelistingOpportunitySnapshot(
        watch_id=watch.id,
        pair_key=opportunity_pair_key(watch.symbol, left_route, right_route),
        pair_type=opportunity_pair_type(left_route, right_route),
        symbol=watch.symbol,
        left_exchange=left_route["exchange"],
        left_market_type=left_route["market_type"],
        right_exchange=right_route["exchange"],
        right_market_type=right_route["market_type"],
        left_bid=left_quote.best_bid,
        left_ask=left_quote.best_ask,
        right_bid=right_quote.best_bid,
        right_ask=right_quote.best_ask,
        reference_spread_pct=reference_pct,
        sell_left_buy_right_pct=sell_left_pct,
        sell_right_buy_left_pct=sell_right_pct,
        best_executable_spread_pct=best_spread,
        direction=direction,
        left_volume_24h=left_quote.volume_24h,
        right_volume_24h=right_quote.volume_24h,
        status=status,
        last_error="；".join(errors) if errors else None,
    )


def maybe_push_opportunity_alert(
    db: Session,
    watch: ExchangeDelistingOpportunityWatch,
    snapshot: ExchangeDelistingOpportunitySnapshot,
    push: bool,
) -> bool:
    if snapshot.pair_type != "FF":
        return False
    spread = snapshot.best_executable_spread_pct
    policy = opportunity_alert_policy(spread)
    if not push or snapshot.status != "signal_unverified" or spread is None or policy is None:
        return False
    if is_opportunity_symbol_muted(db, watch.symbol):
        return False
    tier, cooldown_minutes = policy
    cooldown_start = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    alert_query = select(ExchangeDelistingOpportunityAlertLog.id).where(
        ExchangeDelistingOpportunityAlertLog.watch_id == watch.id,
        ExchangeDelistingOpportunityAlertLog.pair_key == snapshot.pair_key,
        ExchangeDelistingOpportunityAlertLog.created_at >= cooldown_start,
    )
    if tier == "critical":
        alert_query = alert_query.where(
            ExchangeDelistingOpportunityAlertLog.spread_pct > exchange_opportunity_critical_alert_pct()
        )
    elif tier == "high":
        alert_query = alert_query.where(
            ExchangeDelistingOpportunityAlertLog.spread_pct > exchange_opportunity_high_alert_pct()
        )
    else:
        alert_query = alert_query.where(
            ExchangeDelistingOpportunityAlertLog.spread_pct > exchange_opportunity_alert_pct()
        )
    existing = db.scalar(
        alert_query.limit(1)
    )
    if existing:
        return False
    tier_note = {
        "critical": "（>10% 1分钟档）",
        "high": "（>5% 高频档）",
    }.get(tier, "")
    title_label = {
        "critical": "1分钟提醒",
        "high": "高频提醒",
    }.get(tier, "提醒")
    body = (
        f"{watch.symbol} {snapshot.pair_type} 毛价差 {spread:.2f}%{tier_note}\n"
        f"{snapshot.direction}\n"
        "事件波动观察；未扣手续费/滑点，涉及现货卖出时还需核可借、持币、充提和账户额度。"
    )
    status, message = send_bark_or_log(
        enabled=True,
        title=f"波动机会{title_label} {watch.symbol} {spread:.2f}%",
        body=body,
        group="交易所波动机会",
        url=f"https://funding-v2.astro-btc.xyz/?coin={watch.symbol}",
        disabled_message="波动机会推送已关闭。",
        icon_url=exchange_bark_icon_url(),
    )
    db.add(
        ExchangeDelistingOpportunityAlertLog(
            watch_id=watch.id,
            pair_key=snapshot.pair_key,
            spread_pct=spread,
            direction=snapshot.direction,
            status=status,
            message=message,
        )
    )
    return status == "ok"


def normalize_opportunity_symbol(symbol: str) -> str:
    normalized = re.sub(r"[^A-Z0-9._-]", "", str(symbol or "").strip().upper())
    if not normalized or len(normalized) > 32:
        raise ValueError("标的格式无效。")
    return normalized


def is_opportunity_symbol_muted(db: Session, symbol: str) -> bool:
    normalized = normalize_opportunity_symbol(symbol)
    return (
        db.scalar(
            select(ExchangeDelistingOpportunityMute.id)
            .where(ExchangeDelistingOpportunityMute.symbol == normalized)
            .limit(1)
        )
        is not None
    )


def set_opportunity_symbol_mute(db: Session, symbol: str, muted: bool) -> dict[str, Any]:
    normalized = normalize_opportunity_symbol(symbol)
    existing = db.scalar(
        select(ExchangeDelistingOpportunityMute)
        .where(ExchangeDelistingOpportunityMute.symbol == normalized)
        .limit(1)
    )
    if muted and existing is None:
        db.add(ExchangeDelistingOpportunityMute(symbol=normalized))
    elif not muted and existing is not None:
        db.delete(existing)
    db.commit()
    exchange_monitor_log("opportunity_symbol_mute_updated", symbol=normalized, muted=muted)
    return {
        "status": "ok",
        "symbol": normalized,
        "muted": muted,
        "message": (
            f"{normalized} 已静音；仍继续监控和记录，但不再发送该币的波动机会提醒。"
            if muted
            else f"{normalized} 已恢复波动机会提醒。"
        ),
    }


def excluded_opportunity_pair_keys(db: Session) -> set[tuple[int, str]]:
    return {
        (int(watch_id), str(pair_key))
        for watch_id, pair_key in db.execute(
            select(
                ExchangeDelistingOpportunityPairExclusion.watch_id,
                ExchangeDelistingOpportunityPairExclusion.pair_key,
            )
        )
    }


def opportunity_watch_pair_key(
    watch: ExchangeDelistingOpportunityWatch,
    left_route: dict[str, str],
    right_route: dict[str, str],
) -> str:
    left_route, right_route = canonical_opportunity_pair(left_route, right_route)
    return opportunity_pair_key(watch.symbol, left_route, right_route)


def delete_opportunity_pair_monitor(
    db: Session,
    watch_id: int,
    pair_key: str,
) -> dict[str, Any]:
    watch = db.get(ExchangeDelistingOpportunityWatch, watch_id)
    snapshot = db.scalar(
        select(ExchangeDelistingOpportunitySnapshot)
        .where(
            ExchangeDelistingOpportunitySnapshot.watch_id == watch_id,
            ExchangeDelistingOpportunitySnapshot.pair_key == pair_key,
        )
        .order_by(desc(ExchangeDelistingOpportunitySnapshot.created_at))
        .limit(1)
    )
    if watch is None or snapshot is None:
        raise ValueError("监控交易对不存在或已经失效。")

    existing = db.scalar(
        select(ExchangeDelistingOpportunityPairExclusion)
        .where(
            ExchangeDelistingOpportunityPairExclusion.watch_id == watch_id,
            ExchangeDelistingOpportunityPairExclusion.pair_key == pair_key,
        )
        .limit(1)
    )
    if existing is None:
        db.add(
            ExchangeDelistingOpportunityPairExclusion(
                watch_id=watch_id,
                symbol=watch.symbol,
                pair_key=pair_key,
            )
        )
    db.commit()
    left_label = opportunity_route_label(
        {
            "exchange": snapshot.left_exchange,
            "market_type": snapshot.left_market_type,
        }
    )
    right_label = opportunity_route_label(
        {
            "exchange": snapshot.right_exchange,
            "market_type": snapshot.right_market_type,
        }
    )
    exchange_monitor_log(
        "opportunity_pair_monitor_deleted",
        watch_id=watch_id,
        pair_key=pair_key,
        symbol=watch.symbol,
    )
    return {
        "status": "ok",
        "watch_id": watch_id,
        "pair_key": pair_key,
        "symbol": watch.symbol,
        "message": (
            f"{watch.symbol} {left_label} ↔ {right_label} 已删除，"
            "后续不再扫描或发送该交易对提醒。"
        ),
    }


def scan_delisting_opportunities(db: Session, push: bool = True) -> dict[str, Any]:
    from app.crypto import fetch_market, mapped_symbol_for, persist_crypto_market_quote_rows

    run_id = uuid.uuid4().hex[:12]
    started_at = time.perf_counter()
    now = datetime.now(timezone.utc)
    enabled_watches = list(
        db.scalars(
            select(ExchangeDelistingOpportunityWatch).where(
                ExchangeDelistingOpportunityWatch.enabled.is_(True),
            )
        )
    )
    shortened_count = 0
    for watch in enabled_watches:
        if watch.event_at is None:
            continue
        event_at = watch.event_at
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=timezone.utc)
        else:
            event_at = event_at.astimezone(timezone.utc)
        policy_expires_at = event_at + timedelta(hours=_OPPORTUNITY_WATCH_HOURS)
        expires_at = watch.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
        if expires_at > policy_expires_at:
            watch.expires_at = policy_expires_at
            watch.updated_at = now
            shortened_count += 1
    expired = [
        watch
        for watch in enabled_watches
        if (
            watch.expires_at.replace(tzinfo=timezone.utc)
            if watch.expires_at.tzinfo is None
            else watch.expires_at.astimezone(timezone.utc)
        )
        <= now
    ]
    for watch in expired:
        watch.enabled = False
        watch.status = "expired"
        watch.updated_at = now
    watches = sorted(
        (watch for watch in enabled_watches if watch.enabled),
        key=lambda watch: watch.created_at,
    )
    snapshot_count = 0
    alert_count = 0
    excluded_pair_keys = excluded_opportunity_pair_keys(db)
    scan_plans: list[dict[str, Any]] = []
    for watch in watches:
        routes = load_opportunity_routes(watch)
        all_route_pairs = supported_opportunity_watch_pairs(routes)
        route_pairs = [
            (left_route, right_route)
            for left_route, right_route in all_route_pairs
            if (
                watch.id,
                opportunity_watch_pair_key(watch, left_route, right_route),
            )
            not in excluded_pair_keys
        ]
        if not route_pairs:
            watch.status = "removed" if all_route_pairs else "insufficient_routes"
            watch.last_error = (
                "监控交易对已全部删除。"
                if all_route_pairs
                else "剩余市场暂不能组成两个合约市场的跨所价差对。"
            )
            watch.last_scan_at = now
            watch.updated_at = now
            continue
        required_route_keys = {
            (route["exchange"], route["market_type"])
            for pair in route_pairs
            for route in pair
        }
        request_routes: list[tuple[dict[str, str], str]] = []
        for route in routes:
            if (route["exchange"], route["market_type"]) not in required_route_keys:
                continue
            mapped = mapped_symbol_for(db, watch.symbol, route["exchange"], route["market_type"])
            request_routes.append((route, mapped))

        scan_plans.append(
            {
                "watch_id": watch.id,
                "symbol": watch.symbol,
                "route_pairs": route_pairs,
                "request_routes": request_routes,
            }
        )

    # End the read/planning transaction before any exchange HTTP request.  A
    # slow or timed-out market source must never keep SQLite locked.
    db.commit()

    fetched_plans: list[dict[str, Any]] = []
    for plan in scan_plans:
        request_routes = plan["request_routes"]
        worker_limit = max(
            1,
            min(
                int(os.environ.get("EXCHANGE_OPPORTUNITY_WORKERS", "3")),
                len(request_routes),
            ),
        )
        with ThreadPoolExecutor(max_workers=worker_limit) as executor:
            futures = {
                executor.submit(fetch_market, route["exchange"], mapped, route["market_type"]): route
                for route, mapped in request_routes
            }
            quotes_by_route = {
                (route["exchange"], route["market_type"]): future.result()
                for future, route in futures.items()
            }
        successful_quotes = [quote for quote in quotes_by_route.values() if quote.status == "ok"]
        watch_ref = SimpleNamespace(id=plan["watch_id"], symbol=plan["symbol"])
        opportunities: list[ExchangeDelistingOpportunitySnapshot] = []
        watch_errors: list[str] = []
        for left_route, right_route in plan["route_pairs"]:
            left_quote = quotes_by_route[(left_route["exchange"], left_route["market_type"])]
            right_quote = quotes_by_route[(right_route["exchange"], right_route["market_type"])]
            opportunity = build_opportunity_snapshot(
                watch_ref,
                left_route,
                right_route,
                left_quote,
                right_quote,
            )
            opportunities.append(opportunity)
            if opportunity.last_error:
                watch_errors.append(opportunity.last_error)
        fetched_plans.append(
            {
                **plan,
                "successful_quotes": successful_quotes,
                "opportunities": opportunities,
                "status": "partial_error" if watch_errors else (
                    "ok" if opportunities else "insufficient_routes"
                ),
                "error": "；".join(ordered_unique(watch_errors)) if watch_errors else None,
            }
        )

    # Persist all fetched results in one short transaction after network I/O.
    for result in fetched_plans:
        watch = db.get(ExchangeDelistingOpportunityWatch, result["watch_id"])
        if watch is None or not watch.enabled:
            continue
        persist_crypto_market_quote_rows(
            db,
            result["successful_quotes"],
            now,
            "exchange_delisting",
        )
        for opportunity in result["opportunities"]:
            db.add(opportunity)
            db.flush()
            snapshot_count += 1
            if maybe_push_opportunity_alert(db, watch, opportunity, push):
                alert_count += 1
        state_changed = (
            watch.status != result["status"]
            or watch.last_error != result["error"]
        )
        watch.status = result["status"]
        watch.last_error = result["error"]
        watch.last_scan_at = now
        if state_changed:
            watch.updated_at = now
            exchange_monitor_log(
                "opportunity_watch_scanned",
                run_id=run_id,
                symbol=watch.symbol,
                route_count=len(result["request_routes"]),
                pair_count=len(result["opportunities"]),
                status=watch.status,
                error=watch.last_error,
            )
    cutoff = now - timedelta(days=7)
    db.query(ExchangeDelistingOpportunitySnapshot).filter(
        ExchangeDelistingOpportunitySnapshot.created_at < cutoff
    ).delete(synchronize_session=False)
    db.commit()
    exchange_monitor_log_on_change(
        "opportunity_scan_completed",
        run_id=run_id,
        watch_count=len(watches),
        snapshot_count=snapshot_count,
        alert_count=alert_count,
        expired_count=len(expired),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        state_fields={
            "watch_count": len(watches),
            "snapshot_count": snapshot_count,
            "alert_count": alert_count,
            "expired_count": len(expired),
            "shortened_count": shortened_count,
        },
    )
    return {
        "status": "ok" if watches else "not_configured",
        "watch_count": len(watches),
        "snapshot_count": snapshot_count,
        "alert_count": alert_count,
        "expired_count": len(expired),
        "shortened_count": shortened_count,
    }


def latest_opportunity_snapshots(
    db: Session,
    watch: ExchangeDelistingOpportunityWatch,
) -> list[ExchangeDelistingOpportunitySnapshot]:
    rows = list(
        db.scalars(
            select(ExchangeDelistingOpportunitySnapshot)
            .where(ExchangeDelistingOpportunitySnapshot.watch_id == watch.id)
            .order_by(
                desc(ExchangeDelistingOpportunitySnapshot.created_at),
                desc(ExchangeDelistingOpportunitySnapshot.id),
            )
            .limit(100)
        )
    )
    latest: dict[str, ExchangeDelistingOpportunitySnapshot] = {}
    for row in rows:
        latest.setdefault(row.pair_key, row)
    return list(latest.values())


def delisting_opportunities_overview(db: Session) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    muted_symbols = set(db.scalars(select(ExchangeDelistingOpportunityMute.symbol)))
    excluded_pair_keys = excluded_opportunity_pair_keys(db)
    watches = list(
        db.scalars(
            select(ExchangeDelistingOpportunityWatch)
            .where(
                ExchangeDelistingOpportunityWatch.enabled.is_(True),
                ExchangeDelistingOpportunityWatch.expires_at > now,
            )
            .order_by(desc(ExchangeDelistingOpportunityWatch.created_at))
        )
    )
    items: list[dict[str, Any]] = []
    monitored_watch_count = 0
    for watch in watches:
        routes = load_opportunity_routes(watch)
        valid_pair_keys: set[str] = set()
        for left_route, right_route in supported_opportunity_watch_pairs(routes):
            pair_key = opportunity_watch_pair_key(watch, left_route, right_route)
            if (watch.id, pair_key) not in excluded_pair_keys:
                valid_pair_keys.add(pair_key)
        if not valid_pair_keys:
            continue
        monitored_watch_count += 1
        for snapshot in latest_opportunity_snapshots(db, watch):
            if snapshot.pair_type != "FF" or snapshot.pair_key not in valid_pair_keys:
                continue
            items.append(
                {
                    "id": snapshot.id,
                    "watchId": watch.id,
                    "symbol": watch.symbol,
                    "muted": watch.symbol in muted_symbols,
                    "eventAt": utc_datetime_to_beijing_iso(watch.event_at),
                    "expiresAt": utc_datetime_to_beijing_iso(watch.expires_at),
                    "sourceExchange": watch.source_exchange,
                    "sourceMarketType": watch.source_market_type,
                    "pairKey": snapshot.pair_key,
                    "pairType": snapshot.pair_type,
                    "leftExchange": snapshot.left_exchange,
                    "leftExchangeName": opportunity_exchange_name(snapshot.left_exchange),
                    "leftMarketType": snapshot.left_market_type,
                    "rightExchange": snapshot.right_exchange,
                    "rightExchangeName": opportunity_exchange_name(snapshot.right_exchange),
                    "rightMarketType": snapshot.right_market_type,
                    "leftBid": snapshot.left_bid,
                    "leftAsk": snapshot.left_ask,
                    "rightBid": snapshot.right_bid,
                    "rightAsk": snapshot.right_ask,
                    "referenceSpreadPct": snapshot.reference_spread_pct,
                    "sellLeftBuyRightPct": snapshot.sell_left_buy_right_pct,
                    "sellRightBuyLeftPct": snapshot.sell_right_buy_left_pct,
                    "bestExecutableSpreadPct": snapshot.best_executable_spread_pct,
                    "direction": snapshot.direction,
                    "status": snapshot.status,
                    "lastError": snapshot.last_error,
                    "updatedAt": utc_datetime_to_beijing_iso(snapshot.created_at),
                    "astroUrl": f"https://funding-v2.astro-btc.xyz/?coin={watch.symbol}",
                    "executionGate": "仅计算合约—合约；毛价差未扣手续费、滑点、资金费和账户限制。",
                }
            )
    items.sort(
        key=lambda row: (
            row["bestExecutableSpreadPct"] is not None,
            row["bestExecutableSpreadPct"] or -10**9,
        ),
        reverse=True,
    )
    active_muted_symbols = sorted({row["symbol"] for row in items if row["muted"]})
    message = (
        f"正在监控 {monitored_watch_count} 个波动事件、{len(items)} 组市场价差。"
        if monitored_watch_count
        else "暂无波动机会监控。"
    )
    if active_muted_symbols:
        message += f" 已静音 {len(active_muted_symbols)} 个币。"
    return {
        "status": "ok" if items else ("manual_only" if monitored_watch_count else "not_configured"),
        "updated_at": max((row["updatedAt"] for row in items if row["updatedAt"]), default=None),
        "items": items,
        "watch_count": monitored_watch_count,
        "muted_symbols": active_muted_symbols,
        "message": message,
        "formula": "2×(卖出盘口价−买入盘口价)/(卖出盘口价+买入盘口价)",
        "alert_threshold_pct": exchange_opportunity_alert_pct(),
        "alert_cooldown_minutes": exchange_opportunity_cooldown_minutes(),
        "high_alert_threshold_pct": exchange_opportunity_high_alert_pct(),
        "high_alert_cooldown_minutes": exchange_opportunity_high_cooldown_minutes(),
        "critical_alert_threshold_pct": exchange_opportunity_critical_alert_pct(),
        "critical_alert_cooldown_minutes": exchange_opportunity_critical_cooldown_minutes(),
        "scan_interval_seconds": exchange_opportunity_interval_seconds(),
    }


def record_announcement_change(
    db: Session,
    *,
    announcement_key: str,
    symbol: str,
    change_type: str,
    summary: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> bool:
    existing = db.scalar(
        select(ExchangeAnnouncementChangeLog).where(
            ExchangeAnnouncementChangeLog.announcement_key == announcement_key,
            ExchangeAnnouncementChangeLog.change_type == change_type,
            ExchangeAnnouncementChangeLog.summary == summary,
        )
    )
    if existing is not None:
        return False
    db.add(
        ExchangeAnnouncementChangeLog(
            announcement_key=announcement_key,
            symbol=symbol,
            change_type=change_type,
            summary=summary,
            before_json=(json.dumps(before, ensure_ascii=False, default=str) if before else None),
            after_json=(json.dumps(after, ensure_ascii=False, default=str) if after else None),
        )
    )
    return True


def official_announcement_event_index(
    items: list[dict[str, Any]],
) -> set[tuple[str, str, str, str]]:
    index: set[tuple[str, str, str, str]] = set()
    for item in items:
        group_symbols = opportunity_symbols(item)
        for detail in item.get("announcements") or []:
            exchange = str(detail.get("exchange") or "")
            market_type = str(detail.get("market_type") or "")
            action = str(detail.get("action") or "")
            symbols = [
                normalize_group_symbol(symbol)
                for symbol in detail.get("symbols") or group_symbols
                if normalize_group_symbol(symbol)
            ]
            for symbol in symbols:
                index.add((exchange, market_type, action, symbol))
    return index


def inventory_delta_has_announcement(
    index: set[tuple[str, str, str, str]],
    *,
    exchange: str,
    market_type: str,
    action: str,
    symbol: str,
) -> bool:
    allowed_actions = (
        {"listing", "migration", "resumption"}
        if action == "listing"
        else {"delisting", "migration", "suspension"}
    )
    return any(
        (exchange, market_type, candidate, symbol) in index
        for candidate in allowed_actions
    )


def reconcile_market_inventory_changes(
    db: Session,
    *,
    items: list[dict[str, Any]],
    snapshot: dict[tuple[str, str], dict[str, Any]],
    run_id: str,
    state_path: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Continuously maintain the daily audit and persist unmatched inventory deltas."""

    state_path = state_path or market_inventory_reconciliation_state_path()
    now = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    official_index = official_announcement_event_index(items)
    anomaly_count = 0
    with _inventory_state_lock:
        state = load_json_state(state_path)
        # A reconciliation baseline is only comparable when it was written by
        # this schema.  Older/test state files may contain empty or partial
        # inventories; treating those as a real prior snapshot would report
        # every long-lived market as a new listing on the first production run.
        state_version = 1
        if state.get("version") != state_version:
            state = {"version": state_version, "inventories": {}}
        previous_inventories = state.setdefault("inventories", {})
        bootstrapped = not bool(previous_inventories)
        for (exchange, market_type), source in snapshot.items():
            if source.get("status") != "ok":
                continue
            key = f"{exchange}:{market_type}"
            current = sorted(
                {
                    normalize_group_symbol(symbol)
                    for symbol in source.get("symbols") or set()
                    if normalize_group_symbol(symbol)
                }
            )
            previous_raw = previous_inventories.get(key)
            if previous_raw is None:
                previous_inventories[key] = current
                continue
            previous = set(previous_raw)
            current_set = set(current)
            deltas = [
                *(('listing', symbol) for symbol in sorted(current_set - previous)),
                *(('delisting', symbol) for symbol in sorted(previous - current_set)),
            ]
            for action, symbol in deltas:
                if inventory_delta_has_announcement(
                    official_index,
                    exchange=exchange,
                    market_type=market_type,
                    action=action,
                    symbol=symbol,
                ):
                    continue
                action_label = "新增" if action == "listing" else "删除"
                market_name = "现货" if market_type == "spot" else "USDT合约"
                exchange_name = EXCHANGE_LABELS.get(exchange, exchange)
                summary = (
                    f"{exchange_name} {symbol} {market_name}市场列表{action_label}，"
                    "但当前公告抓取结果中没有匹配记录。"
                )
                raw_key = f"{now.date()}|{exchange}|{market_type}|{action}|{symbol}"
                announcement_key = f"inventory-audit:{hashlib.sha256(raw_key.encode()).hexdigest()[:48]}"
                if record_announcement_change(
                    db,
                    announcement_key=announcement_key,
                    symbol=symbol,
                    change_type="inventory_without_notice",
                    summary=summary,
                    before={"present": action == "delisting", "auditDate": str(now.date())},
                    after={
                        "present": action == "listing",
                        "exchange": exchange,
                        "marketType": market_type,
                        "detectedAt": now.isoformat(),
                    },
                ):
                    anomaly_count += 1
                    exchange_monitor_log(
                        "inventory_reconciliation_anomaly",
                        run_id=run_id,
                        audit_date=str(now.date()),
                        exchange=exchange,
                        market_type=market_type,
                        symbol=symbol,
                        action=action,
                        summary=summary,
                        level="warning",
                    )
            previous_inventories[key] = current
        state["last_reconciled_at"] = now.isoformat()
        state["last_reconciled_date"] = str(now.date())
        save_json_state(state_path, state)

    if bootstrapped or anomaly_count:
        exchange_monitor_log(
            "inventory_reconciliation_completed",
            run_id=run_id,
            audit_date=str(now.date()),
            status="baseline_created" if bootstrapped else "ok",
            anomaly_count=anomaly_count,
            inventory_count=len(state.get("inventories") or {}),
        )
    return anomaly_count


def mark_announcement_timeline_pushed(
    db: Session,
    item: dict[str, Any],
    pushed_at: datetime,
) -> None:
    for action in {str(detail.get("action") or "") for detail in item.get("announcements") or []}:
        if action not in {"listing", "delisting"}:
            continue
        key = announcement_timeline_key(item, action)
        for symbol in opportunity_symbols(item):
            timeline = db.scalar(
                select(ExchangeAnnouncementTimeline).where(
                    ExchangeAnnouncementTimeline.announcement_key == key,
                    ExchangeAnnouncementTimeline.symbol == symbol,
                )
            )
            if timeline is not None and timeline.pushed_at is None:
                timeline.pushed_at = pushed_at


def priority_listing_contract_refresh_keys(
    items: list[dict[str, Any]],
    new_item_keys: set[str] | None = None,
) -> set[tuple[str, str]]:
    """Return contract inventories that need the 15-second launch-time path.

    Announcement pages remain on their per-source cache.  Around a contract
    launch we refresh only contract inventories (not all spot inventories), so
    the source market and its possible counterpart exchanges are detected
    without competing with Astro's API lane.
    """

    now = datetime.now(BEIJING_TZ)
    new_item_keys = new_item_keys or set()
    active = False
    for item in items:
        item_is_new = announcement_push_key(item) in new_item_keys
        for detail in item.get("announcements") or []:
            if detail.get("action") != "listing" or detail.get("market_type") != "contract":
                continue
            event_at = parse_event_datetime(detail.get("event_at") or item.get("event_at"))
            if item_is_new or event_at is None:
                active = True
                break
            # Start fast contract-list comparison before launch and keep it for
            # the two-hour new-listing priority window used by Astro.
            if now - timedelta(hours=2) <= event_at <= now + timedelta(hours=48):
                active = True
                break
        if active:
            break
    if not active:
        return set()
    return {
        (announcement_exchange, "contract")
        for announcement_exchange, _crypto_exchange, _exchange_name in MARKET_STATUS_EXCHANGES
        if "contract" in MARKET_STATUS_MARKET_TYPES.get(
            announcement_exchange,
            ("spot", "contract"),
        )
    }


def push_exchange_announcements(db: Session, force_refresh: bool = True) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    started_at = time.perf_counter()
    payload = get_exchange_announcements(force_refresh=force_refresh, monitor_run_id=run_id)
    items = payload.get("items", [])
    keys = [announcement_push_key(item) for item in items]
    existing_by_key: dict[str, ExchangeAnnouncementPushLog] = {}
    existing_by_link: dict[str, ExchangeAnnouncementPushLog] = {}
    existing_by_body: dict[str, ExchangeAnnouncementPushLog] = {}
    all_existing_logs = list(db.scalars(select(ExchangeAnnouncementPushLog)))
    existing_by_key = {row.announcement_key: row for row in all_existing_logs}
    existing_by_link = {row.link: row for row in all_existing_logs if row.link}
    existing_by_body = {row.body: row for row in all_existing_logs if row.body}
    new_item_keys = {
        announcement_push_key(item)
        for item in items
        if announcement_push_key(item) not in existing_by_key
    }

    pushed_count = 0
    logged_count = 0
    suppressed_count = 0
    ignored_count = 0
    statuses: list[str] = []
    lifecycle_items_present = any(
        detail.get("action") in {"listing", "delisting"}
        for item in items
        for detail in item.get("announcements") or []
    )
    refresh_keys = priority_listing_contract_refresh_keys(items, new_item_keys)
    market_snapshot: dict[tuple[str, str], dict[str, Any]] = exchange_market_status_snapshot(
        force_refresh=bool(refresh_keys),
        monitor_run_id=run_id,
        refresh_keys=refresh_keys or None,
    )
    if lifecycle_items_present:
        sync_announcement_timelines(db, items, market_snapshot)
        sync_delisting_opportunity_watches(db, items, market_snapshot, run_id)
    reconcile_market_inventory_changes(
        db,
        items=items,
        snapshot=market_snapshot,
        run_id=run_id,
    )
    seen_log_ids: set[int] = set()
    active_keys = set(keys)
    active_links = {
        link for item in items if (link := original_announcement_url(item))
    }
    source_rows = list(payload.get("sources") or [])
    all_sources_healthy = bool(source_rows) and all(
        source.get("status") == "ok" for source in source_rows
    )
    for item in items:
        key = announcement_push_key(item)
        body = item.get("title") or build_summary_title(item)
        link = original_announcement_url(item)
        event_at = parse_event_datetime(item.get("event_at"))
        item_symbol = str(item.get("symbol") or "")
        item_asset_type = str(item.get("asset_type") or "unknown")
        item_asset_label = str(item.get("asset_label") or asset_label(item_asset_type))
        notification_suppressed = is_funding_interval_notification_suppressed(item)
        matched_log = existing_by_key.get(key)
        matched_by = "key" if matched_log else None
        if not matched_log and link:
            matched_log = existing_by_link.get(link)
            matched_by = "link" if matched_log else None
        if not matched_log:
            matched_log = existing_by_body.get(body)
            matched_by = "body" if matched_log else None
        if matched_log:
            dedup_state_changed = False
            matched_event_at = parse_event_datetime(matched_log.event_at)
            if event_at and matched_event_at != event_at:
                summary = (
                    f"{item_symbol} 公告时间由 "
                    f"{utc_datetime_to_beijing_iso(matched_event_at) or '-'} 改为 "
                    f"{utc_datetime_to_beijing_iso(event_at) or '-'}。"
                )
                changed = record_announcement_change(
                    db,
                    announcement_key=matched_log.announcement_key,
                    symbol=item_symbol,
                    change_type="event_time_changed",
                    summary=summary,
                    before={"eventAt": utc_datetime_to_beijing_iso(matched_event_at)},
                    after={"eventAt": utc_datetime_to_beijing_iso(event_at)},
                )
                if changed:
                    dedup_state_changed = True
                    if not notification_suppressed:
                        send_bark_or_log(
                            enabled=exchange_push_enabled(),
                            title="交易所公告更新",
                            body=summary,
                            group="交易所公告",
                            url=link,
                            disabled_message="公告改期已记录。",
                            icon_url=exchange_bark_icon_url(),
                        )
                matched_log.event_at = event_at
            if matched_by == "link" and matched_log.announcement_key != key:
                summary = f"{item_symbol} 官方公告内容已更新，链接未变。"
                changed = record_announcement_change(
                    db,
                    announcement_key=matched_log.announcement_key,
                    symbol=item_symbol,
                    change_type="content_changed",
                    summary=summary,
                    before={"announcementKey": matched_log.announcement_key},
                    after={"announcementKey": key, "title": item.get("title")},
                )
                if changed:
                    dedup_state_changed = True
                    if not notification_suppressed:
                        send_bark_or_log(
                            enabled=exchange_push_enabled(),
                            title="交易所公告更新",
                            body=summary,
                            group="交易所公告",
                            url=link,
                            disabled_message="公告内容更新已记录。",
                            icon_url=exchange_bark_icon_url(),
                        )
            if item_symbol and matched_log.symbol != item_symbol:
                matched_log.symbol = item_symbol
                dedup_state_changed = True
            if body and not matched_log.body:
                matched_log.body = body
                dedup_state_changed = True
            if matched_log.asset_type != item_asset_type:
                matched_log.asset_type = item_asset_type
                dedup_state_changed = True
            if matched_log.asset_label != item_asset_label:
                matched_log.asset_label = item_asset_label
                dedup_state_changed = True
            ignored_count += 1
            if matched_log.id is not None:
                seen_log_ids.add(matched_log.id)
            if dedup_state_changed:
                exchange_monitor_log(
                    "announcement_deduplicated",
                    run_id=run_id,
                    symbol=item_symbol,
                    announcement_key=key,
                    matched_by=matched_by,
                    matched_log_id=matched_log.id,
                    state_changed=True,
                )
            continue
        body = build_exchange_announcement_push_body(item, market_snapshot)
        exchange_monitor_log(
            "push_candidate_prepared",
            run_id=run_id,
            symbol=item_symbol,
            announcement_key=key,
            event_at=item.get("event_at"),
            exchange_names=item.get("exchange_names") or [],
            body=body,
        )
        if notification_suppressed:
            status, message = "suppressed", "资金费周期变化按规则仅记录，不发送提醒。"
            suppressed_count += 1
            exchange_monitor_log(
                "push_suppressed_policy",
                run_id=run_id,
                symbol=item_symbol,
                announcement_key=key,
                policy="funding_interval_change_no_notification",
            )
        else:
            status, message = send_bark_or_log(
                enabled=exchange_push_enabled(),
                title="交易所公告",
                body=body,
                group="交易所公告",
                url=link,
                disabled_message="交易所公告推送已关闭，公告已记录。",
                icon_url=exchange_bark_icon_url(),
            )
        log = ExchangeAnnouncementPushLog(
            announcement_key=key,
            symbol=item_symbol,
            group_name="交易所公告",
            title="交易所公告",
            body=body,
            link=link,
            event_at=event_at,
            asset_type=item_asset_type,
            asset_label=item_asset_label,
            status=status,
            message=message,
        )
        db.add(log)
        existing_by_key[key] = log
        if link:
            existing_by_link[link] = log
        existing_by_body[body] = log
        statuses.append(status)
        logged_count += 1
        if status == "ok":
            pushed_count += 1
            mark_announcement_timeline_pushed(db, item, datetime.now(timezone.utc))
        exchange_monitor_log(
            "push_attempt_completed",
            run_id=run_id,
            symbol=item_symbol,
            announcement_key=key,
            status=status,
            error=message,
        )

    now_beijing = datetime.now(BEIJING_TZ)
    for previous in all_existing_logs:
        if not all_sources_healthy:
            break
        if previous.id in seen_log_ids:
            continue
        previous_event = parse_event_datetime(previous.event_at)
        if previous_event is None or previous_event <= now_beijing:
            continue
        if previous.announcement_key in active_keys or (previous.link and previous.link in active_links):
            continue
        summary = (
            f"{previous.symbol} 未来公告已不在当前官方列表，"
            "可能已撤销、改期或移出首页，需要人工核验。"
        )
        changed = record_announcement_change(
            db,
            announcement_key=previous.announcement_key,
            symbol=previous.symbol,
            change_type="possibly_cancelled",
            summary=summary,
            before={
                "eventAt": utc_datetime_to_beijing_iso(previous_event),
                "link": previous.link,
            },
            after={"presentInCurrentOfficialList": False},
        )
        if changed and previous.status != "suppressed":
            send_bark_or_log(
                enabled=exchange_push_enabled(),
                title="交易所公告待核验",
                body=summary,
                group="交易所公告",
                url=previous.link,
                disabled_message="公告待核验变化已记录。",
                icon_url=exchange_bark_icon_url(),
            )
    db.commit()

    if not items:
        status = payload.get("status", "ok")
        message = "没有今日有效交易所公告。"
    elif not logged_count:
        status = "ok"
        message = "没有新的交易所公告需要推送。"
    elif any(row == "error" for row in statuses):
        status = "partial_error"
        message = f"新增 {logged_count} 条，成功推送 {pushed_count} 条，部分推送失败。"
    elif any(row == "not_configured" for row in statuses):
        status = "not_configured"
        message = f"新增 {logged_count} 条，Bark 未配置，已记录但未推送。"
    elif any(row == "manual_only" for row in statuses):
        status = "manual_only"
        message = f"新增 {logged_count} 条，交易所公告推送关闭，已记录但未推送。"
    elif suppressed_count:
        status = "ok"
        message = (
            f"新增 {logged_count} 条，推送 {pushed_count} 条；"
            f"{suppressed_count} 条资金费周期变化已静默记录。"
        )
    else:
        status = "ok"
        message = f"成功推送 {pushed_count} 条交易所公告。"

    exchange_monitor_log_on_change(
        "push_cycle_completed",
        run_id=run_id,
        status=status,
        item_count=len(items),
        logged_count=logged_count,
        pushed_count=pushed_count,
        suppressed_count=suppressed_count,
        ignored_count=ignored_count,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        state_fields={
            "status": status,
            "item_count": len(items),
            "logged_count": logged_count,
            "pushed_count": pushed_count,
            "suppressed_count": suppressed_count,
            "ignored_count": ignored_count,
        },
    )
    return {
        "status": status,
        "message": message,
        "pushed_count": pushed_count,
        "suppressed_count": suppressed_count,
        "logged_count": logged_count,
        "ignored_count": ignored_count,
        "bark_status": bark_status(),
        "push_status": exchange_push_status(),
        "push_logs": [
            push_log_to_out(log)
            for log in db.scalars(
                select(ExchangeAnnouncementPushLog)
                .where(ExchangeAnnouncementPushLog.visible.is_(True))
                .order_by(desc(ExchangeAnnouncementPushLog.created_at))
                .limit(40)
            )
        ],
    }


def announcement_push_key(item: dict[str, Any]) -> str:
    urls = sorted(str(row.get("url") or "") for row in item.get("announcements", []))
    raw = "|".join([str(item.get("title") or ""), *urls])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def original_announcement_url(item: dict[str, Any]) -> str | None:
    announcements = item.get("announcements") or []
    if announcements:
        return announcements[0].get("url")
    return None


def push_log_to_out(log: ExchangeAnnouncementPushLog) -> dict[str, Any]:
    event_at = parse_event_datetime(log.event_at)
    log_asset_type = log.asset_type or "unknown"
    log_asset_label = log.asset_label or asset_label(log_asset_type)
    return {
        "id": log.id,
        "announcement_key": log.announcement_key,
        "symbol": log.symbol,
        "group_name": log.group_name,
        "title": log.title,
        "body": log.body,
        "link": log.link,
        "event_at": event_at.isoformat() if event_at else None,
        "asset_type": log_asset_type,
        "asset_label": log_asset_label,
        "status": log.status,
        "message": log.message,
        "created_at": utc_datetime_to_beijing_iso(log.created_at),
    }


def fetch_binance() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    catalogs = [
        ("listing", 48, "新币上线"),
        ("delisting", 161, "下架公告"),
        ("unknown", 49, "币安最新公告"),
    ]
    with http_client() as client:
        for action, catalog_id, category in catalogs:
            payload = get_response(
                client,
                BINANCE_ARTICLE_API,
                params={"type": 1, "pageNo": 1, "pageSize": 20, "catalogId": catalog_id},
                headers={"lang": "zh-CN", "clienttype": "web"},
            )
            payload.raise_for_status()
            data = payload.json()
            for article in data.get("data", {}).get("catalogs", [{}])[0].get("articles", []):
                title = str(article.get("title") or "").strip()
                item = build_item(
                    exchange="bn",
                    action=action,
                    market_type=market_type_from_title(title, default="spot"),
                    title=title,
                    url=f"https://www.binance.com/zh-CN/support/announcement/{article.get('code')}",
                    published_at=iso_from_millis(article.get("releaseDate")),
                    category=category,
                )
                if is_relevant(item):
                    rows.append(item)
    return rows


def fetch_bybit() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    categories = [
        ("listing", "new_crypto"),
        ("delisting", "delistings"),
        ("unknown", "product_updates"),
        ("unknown", "maintenance_updates"),
    ]
    with http_client() as client:
        stock_symbols: set[str] = set()
        try:
            instrument_response = get_response(
                client,
                BYBIT_INSTRUMENTS_API,
                params={"category": "linear", "limit": 1000},
            )
            instrument_response.raise_for_status()
            stock_symbols = bybit_stock_symbols_from_instruments(
                instrument_response.json()
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            exchange_monitor_log(
                "bybit_instrument_metadata_failed",
                error=redact_monitor_text(str(exc)),
            )
        for action, category in categories:
            payload = get_response(
                client,
                BYBIT_ANNOUNCEMENT_API,
                params={"locale": "zh-TW", "type": category, "limit": 30},
            )
            payload.raise_for_status()
            data = payload.json()
            for article in data.get("result", {}).get("list", []):
                title = str(article.get("title") or "").strip()
                description = str(article.get("description") or "").strip()
                item = build_item(
                    exchange="by",
                    action=action,
                    market_type=market_type_from_title(title, default="unknown"),
                    title=title,
                    url=str(article.get("url") or "https://www.bybit.com/en/announcement-info/"),
                    published_at=iso_from_millis(article.get("publishTime") or article.get("dateTimestamp")),
                    category=str(article.get("type", {}).get("title") or category),
                    event_context=description,
                )
                if any(
                    normalize_group_symbol(symbol) in stock_symbols
                    for symbol in item.get("symbols") or []
                ):
                    item["asset_type"] = "stock"
                    item["asset_label"] = asset_label("stock")
                if is_relevant(item):
                    rows.append(item)
    return rows


def bybit_stock_symbols_from_instruments(
    payload: dict[str, Any],
) -> set[str]:
    instruments = payload.get("result", {}).get("list", [])
    if not isinstance(instruments, list):
        return set()
    return {
        normalize_group_symbol(str(instrument.get("symbol") or ""))
        for instrument in instruments
        if isinstance(instrument, dict)
        and str(instrument.get("symbolType") or "").lower() == "stock"
        and normalize_group_symbol(str(instrument.get("symbol") or ""))
    }


def fetch_aster_announcements() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    categories = [
        ("listing", "NEW_LISTING", "Aster New Listings"),
        ("delisting", "DELISTING", "Aster Delistings"),
    ]
    with http_client() as client:
        for action, category_code, category_label in categories:
            response = client.post(
                ASTER_ANNOUNCEMENT_API,
                json={"category": category_code, "page": 1, "size": 50},
                headers={"lang": "en"},
            )
            response.raise_for_status()
            rows.extend(
                extract_aster_announcement_rows(
                    response.json(),
                    action=action,
                    category_code=category_code,
                    category_label=category_label,
                )
            )
        try:
            response = get_response(client, ASTER_FUTURES_EXCHANGE_INFO_API)
            response.raise_for_status()
            market_payload = response.json()
            rows.extend(extract_aster_pending_market_rows(market_payload))
            apply_aster_market_asset_types(rows, market_payload)
        except Exception as exc:
            exchange_monitor_log(
                "aster_pending_market_fetch_failed",
                error=str(exc),
                level="warning",
            )
    return rows


def extract_aster_pending_market_rows(
    payload: dict[str, Any],
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    observed_at = observed_at or datetime.now(timezone.utc)
    observed_beijing = observed_at.astimezone(BEIJING_TZ)
    listing_window_start = observed_beijing.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    listing_window_end = listing_window_start + timedelta(
        days=LISTING_LOOKAHEAD_DAYS + 1
    )
    rows: list[dict[str, Any]] = []
    symbols = payload.get("symbols") if isinstance(payload, dict) else []
    for market in symbols if isinstance(symbols, list) else []:
        if not isinstance(market, dict):
            continue
        market_status = str(market.get("status") or "").upper()
        if market_status not in {"PENDING_TRADING", "TRADING"}:
            continue
        if str(market.get("quoteAsset") or "").upper() != "USDT":
            continue
        symbol = str(market.get("symbol") or "").upper()
        onboard_at = iso_from_millis(market.get("onboardDate"))
        onboard_dt = parse_event_datetime(onboard_at)
        if not symbol or not onboard_dt:
            continue
        if not listing_window_start <= onboard_dt < listing_window_end:
            continue
        onboard_utc = onboard_dt.astimezone(timezone.utc)
        title = (
            f"{symbol} Perpetual Scheduled Listing - "
            f"{onboard_utc.strftime('%Y-%m-%d %H:%M')} UTC "
            "[Aster Market Pre-listing]"
        )
        item = build_item(
            exchange="aster",
            action="listing",
            market_type="contract",
            title=title,
            url=(
                "https://www.asterdex.com/en/announcement"
                f"?category=NEW_LISTING&symbol={symbol}"
            ),
            published_at=observed_at.astimezone(timezone.utc).isoformat(),
            category="Aster Market Pre-listings",
            event_context=(
                "Aster futures exchangeInfo reports "
                f"{symbol} status={market_status}, onboardDate={onboard_at}."
            ),
        )
        item["asset_type"] = aster_market_asset_type(market)
        item["asset_label"] = asset_label(item["asset_type"])
        if is_relevant(item):
            rows.append(item)
    return rows


def apply_aster_market_asset_types(
    rows: list[dict[str, Any]],
    market_payload: dict[str, Any],
) -> None:
    market_rows = market_payload.get("symbols") if isinstance(market_payload, dict) else []
    if not isinstance(market_rows, list):
        return
    market_by_symbol = {
        normalize_group_symbol(str(market.get("baseAsset") or market.get("symbol") or "")): market
        for market in market_rows if isinstance(market, dict)
    }
    for row in rows:
        markets = [
            market_by_symbol.get(normalize_group_symbol(str(symbol)))
            for symbol in row.get("symbols") or []
        ]
        if any(market and aster_market_asset_type(market) == "stock" for market in markets):
            row["asset_type"] = "stock"
            row["asset_label"] = asset_label("stock")


def aster_market_asset_type(market: dict[str, Any]) -> str:
    symbol = normalize_group_symbol(
        str(market.get("baseAsset") or market.get("symbol") or "")
    )
    subtypes = {
        str(value).upper()
        for value in market.get("underlyingSubType") or []
    }
    tags = {
        str(value).lower()
        for value in market.get("tags") or []
    }
    channel = str(market.get("channel") or "").lower()
    if (
        "STOCK" in subtypes
        or symbol in ASTER_STOCK_SYMBOL_OVERRIDES
        or channel in {"nasdaq", "hkstock", "krstock", "cnstock"}
        or bool(tags & {"stock", "stocks", "股票"})
    ):
        return "stock"
    return "crypto"


def extract_aster_announcement_rows(
    payload: dict[str, Any],
    *,
    action: str,
    category_code: str,
    category_label: str,
) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    articles = data.get("rows") if isinstance(data, dict) else []
    rows: list[dict[str, Any]] = []
    for article in articles if isinstance(articles, list) else []:
        if not isinstance(article, dict):
            continue
        title = str(article.get("title") or "").strip()
        announcement_id = article.get("id") or article.get("announcementId")
        if not title or announcement_id is None:
            continue
        context = " ".join(
            value
            for value in (
                normalize_text(str(article.get("subtitle") or "")),
                normalize_text(
                    BeautifulSoup(
                        str(article.get("content") or ""),
                        "html.parser",
                    ).get_text(" ", strip=True)
                ),
            )
            if value
        )
        item = build_item(
            exchange="aster",
            action=action_from_title(title, action),
            market_type=market_type_from_title(title, default="contract"),
            title=title,
            url=(
                f"https://www.asterdex.com/en/announcement/{announcement_id}"
                f"?category={category_code}"
            ),
            published_at=iso_from_millis(
                article.get("publishTime")
                or article.get("updateTime")
                or article.get("time")
            ),
            category=category_label,
            event_context=context or None,
        )
        for symbol in re.findall(r"\$([A-Z0-9]{1,24})(?=\s*[(])", title):
            add_symbol(item["symbols"], symbol)
        item["symbols"] = order_symbols_by_title(item["symbols"], title)
        if is_relevant(item):
            rows.append(item)
    return rows


def fetch_bitget() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with http_client() as client:
        for action, default_market, url in BITGET_CATEGORY_URLS:
            response = get_response(client, url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            rows.extend(extract_html_articles("bg", soup, url, "unknown", default_market, "Bitget Support", client=client))
    return rows


def extract_okx_app_state_articles(
    html_text: str,
    *,
    source_url: str,
    default_action: str,
    default_market: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Read OKX's SSR payload instead of relying on the sparse fallback anchors."""

    soup = BeautifulSoup(html_text, "html.parser")
    script = soup.find("script", id="appState")
    if script is None or not script.string:
        return []
    try:
        payload = json.loads(script.string)
        articles = (
            payload.get("appContext", {})
            .get("initialProps", {})
            .get("sectionData", {})
            .get("articleList", {})
            .get("list", [])
        )
    except (TypeError, ValueError):
        return []

    parsed_url = urlsplit(source_url)
    path_parts = [part for part in parsed_url.path.split("/") if part]
    locale_prefix = f"/{path_parts[0]}" if path_parts and path_parts[0] != "help" else ""
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    rows: list[dict[str, Any]] = []
    for article in articles if isinstance(articles, list) else []:
        if not isinstance(article, dict):
            continue
        title = str(article.get("title") or "").strip()
        slug = str(article.get("slug") or article.get("id") or "").strip("/")
        if not title or not slug:
            continue
        url = f"{origin}{locale_prefix}/help/{slug}"
        action = action_from_title(title, default_action)
        market_type = market_type_from_title(title, default=default_market)
        context: str | None = None
        if client and should_fetch_article_context("okx", title, action, market_type):
            context = fetch_article_context(client, "okx", url)
        item = build_item(
            exchange="okx",
            action=action,
            market_type=market_type,
            title=title,
            url=url,
            published_at=iso_from_millis(article.get("publishTime")),
            category=str(article.get("sectionSlug") or "OKX Help"),
            event_context=context,
        )
        if is_relevant(item):
            rows.append(item)
    return rows


def fetch_okx() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with http_client() as client:
        for action, default_market, url in OKX_SECTION_URLS:
            response = get_response(client, url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            rows.extend(
                extract_okx_app_state_articles(
                    response.text,
                    source_url=url,
                    default_action=action,
                    default_market=default_market,
                    client=client if exchange_fetch_details_enabled() else None,
                )
            )
            rows.extend(
                extract_html_articles(
                    "okx",
                    soup,
                    url,
                    "unknown",
                    default_market,
                    "OKX Help",
                    client=client if exchange_fetch_details_enabled() else None,
                )
            )
    rows = dedupe_items(rows)
    rows.extend(
        fetch_market_inventory_delta_items(
            announcement_exchange="okx",
            crypto_exchange="okx",
            market_type="contract",
            official_items=rows,
        )
    )
    return dedupe_items(rows)


def fetch_hyperliquid_market_events() -> list[dict[str, Any]]:
    return fetch_market_inventory_delta_items(
        announcement_exchange="hl",
        crypto_exchange="hl",
        market_type="contract",
        official_items=[],
    )


def market_inventory_discovery_state_path() -> Path:
    return exchange_monitor_log_path().with_name("exchange-market-discovery-state.json")


def market_inventory_reconciliation_state_path() -> Path:
    return exchange_monitor_log_path().with_name("exchange-market-reconciliation-state.json")


def load_json_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_json_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def inventory_event_url(exchange: str, symbol: str) -> str:
    if exchange == "okx":
        return f"https://www.okx.com/trade-swap/{symbol.lower()}-usdt-swap"
    if exchange == "hl":
        return f"https://app.hyperliquid.xyz/trade/{symbol}"
    return ""


def inventory_action_matches_official(
    official_items: list[dict[str, Any]],
    *,
    symbol: str,
    action: str,
    market_type: str,
) -> bool:
    normalized_symbol = normalize_group_symbol(symbol)
    return any(
        action == str(item.get("action") or "")
        and market_type == str(item.get("market_type") or "")
        and normalized_symbol
        in {
            normalize_group_symbol(value)
            for value in item.get("symbols") or []
        }
        for item in official_items
    )


def build_inventory_event_item(
    *,
    exchange: str,
    symbol: str,
    action: str,
    market_type: str,
    detected_at: datetime,
    official_match: bool,
) -> dict[str, Any]:
    exchange_name = EXCHANGE_LABELS[exchange]
    action_text = "新增" if action == "listing" else "移除"
    title = f"{symbol}USDT 实时合约列表{action_text}"
    if not official_match:
        title += "（未匹配官方公告）"
    item = build_item(
        exchange=exchange,
        action=action,
        market_type=market_type,
        title=title,
        url=(
            f"{inventory_event_url(exchange, symbol)}"
            f"#inventory-{action}-{int(detected_at.timestamp())}"
        ),
        published_at=None,
        category=f"{exchange_name} 实时市场列表增量",
    )
    event_at = detected_at.astimezone(BEIJING_TZ).isoformat()
    item.update(
        {
            "event_at": event_at,
            "event_at_sort": detected_at.timestamp(),
            "has_occurred": True,
            "seconds_until_event": 0,
            "event_status": "已发生",
            "symbols": [normalize_group_symbol(symbol)],
            "discovery_source": "market_inventory_delta",
            "official_announcement_matched": official_match,
            "detected_at": event_at,
        }
    )
    return item


def market_inventory_delta_items_from_symbols(
    *,
    announcement_exchange: str,
    market_type: str,
    current_symbols: set[str],
    official_items: list[dict[str, Any]],
    state_path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Persist and return market-list discoveries without inventing publish times."""

    state_path = state_path or market_inventory_discovery_state_path()
    now = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    inventory_key = f"{announcement_exchange}:{market_type}"
    current = {
        normalize_group_symbol(symbol)
        for symbol in current_symbols
        if normalize_group_symbol(symbol)
    }
    with _inventory_state_lock:
        state = load_json_state(state_path)
        inventories = state.setdefault("inventories", {})
        missing_counts = state.setdefault("missing_counts", {})
        stored_events = state.setdefault("events", [])
        previous_raw = inventories.get(inventory_key)
        if previous_raw is None:
            inventories[inventory_key] = sorted(current)
            state["updated_at"] = now.isoformat()
            save_json_state(state_path, state)
            return []

        confirmed = {
            normalize_group_symbol(symbol)
            for symbol in previous_raw
            if normalize_group_symbol(symbol)
        }
        added = sorted(current - confirmed)
        confirmed_removed: list[str] = []
        for symbol in sorted(confirmed - current):
            count_key = f"{inventory_key}:{symbol}"
            count = int(missing_counts.get(count_key) or 0) + 1
            missing_counts[count_key] = count
            if count >= 2:
                confirmed_removed.append(symbol)
                missing_counts.pop(count_key, None)
        for symbol in current:
            missing_counts.pop(f"{inventory_key}:{symbol}", None)

        confirmed.update(added)
        confirmed.difference_update(confirmed_removed)
        inventories[inventory_key] = sorted(confirmed)

        for action, symbols in (("listing", added), ("delisting", confirmed_removed)):
            for symbol in symbols:
                official_match = inventory_action_matches_official(
                    official_items,
                    symbol=symbol,
                    action=action,
                    market_type=market_type,
                )
                item = build_inventory_event_item(
                    exchange=announcement_exchange,
                    symbol=symbol,
                    action=action,
                    market_type=market_type,
                    detected_at=now,
                    official_match=official_match,
                )
                stored_events.append(item)
                exchange_monitor_log(
                    "market_inventory_delta_detected",
                    exchange=announcement_exchange,
                    market_type=market_type,
                    symbol=symbol,
                    action=action,
                    official_announcement_matched=official_match,
                    detected_at=now.isoformat(),
                )

        cutoff = now - timedelta(days=GENERAL_EVENT_PUBLISHED_LOOKBACK_DAYS)
        retained_events = []
        for event in stored_events:
            detected = parse_event_datetime(event.get("detected_at") or event.get("event_at"))
            if detected and detected >= cutoff:
                retained_events.append(event)
        state["events"] = retained_events
        state["updated_at"] = now.isoformat()
        save_json_state(state_path, state)

    return [
        event
        for event in retained_events
        if event.get("exchange") == announcement_exchange
        and event.get("market_type") == market_type
    ]


def fetch_market_inventory_delta_items(
    *,
    announcement_exchange: str,
    crypto_exchange: str,
    market_type: str,
    official_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from app.crypto import (
        EXCHANGE_ANNOUNCEMENT_API_LANE,
        api_rate_limit_lane,
        fetch_exchange_market_symbols,
    )

    crypto_market_type = "spot" if market_type == "spot" else "futures"
    with api_rate_limit_lane(EXCHANGE_ANNOUNCEMENT_API_LANE):
        with http_client() as client:
            symbols = fetch_exchange_market_symbols(client, crypto_exchange, crypto_market_type)
    return market_inventory_delta_items_from_symbols(
        announcement_exchange=announcement_exchange,
        market_type=market_type,
        current_symbols=symbols,
        official_items=official_items,
    )


def fetch_gate() -> list[dict[str, Any]]:
    with http_client() as client:
        try:
            return fetch_gate_via_next_data(client)
        except Exception:
            return fetch_gate_via_reader(client)


def fetch_gate_via_next_data(client: httpx.Client) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for default_action, default_market, source_url in GATE_SECTION_URLS:
        section_rows: list[dict[str, Any]] = []
        section_errors: list[str] = []
        for candidate_url in gate_section_url_candidates(source_url):
            try:
                response = get_response(client, candidate_url)
                response.raise_for_status()
                section_rows = extract_gate_next_data_articles(
                    response.text,
                    source_url=candidate_url,
                    default_action=default_action,
                    default_market=default_market,
                    category="Gate 公告",
                )
                if section_rows:
                    break
                section_errors.append(f"{candidate_url}: 未找到目标公告")
            except Exception as exc:
                section_errors.append(f"{candidate_url}: {exc}")
        if section_rows:
            rows.extend(section_rows)
        elif section_errors:
            errors.append(
                f"{default_action}: "
                + "; ".join(error for error in section_errors if error)
            )
    if not rows and errors:
        raise RuntimeError("Gate 结构化公告读取失败：" + " | ".join(errors))
    return rows


def gate_section_url_candidates(source_url: str) -> list[str]:
    path = source_url.removeprefix("https://www.gate.com")
    return [
        *(f"{origin}{path}" for origin in GATE_FALLBACK_ORIGINS),
        source_url,
    ]


def extract_gate_next_data_articles(
    html_text: str,
    *,
    source_url: str,
    default_action: str,
    default_market: str,
    category: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    next_data_node = soup.select_one("script#__NEXT_DATA__")
    if not next_data_node or not next_data_node.string:
        raise ValueError("Gate 页面缺少 __NEXT_DATA__")
    payload = json.loads(next_data_node.string)
    articles = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("listData", {})
        .get("list", [])
    )
    if not isinstance(articles, list):
        raise ValueError("Gate 公告列表格式异常")

    rows: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = normalize_text(str(article.get("title") or ""))
        article_path = str(article.get("url") or "")
        if not title or not article_path:
            continue
        brief = normalize_text(str(article.get("brief") or ""))
        tags = normalize_text(str(article.get("tags") or ""))
        context = normalize_text(" ".join(value for value in (brief, tags) if value))
        category_market = gate_market_type_from_category(
            article.get("cate_id"),
            default_market,
        )
        item = build_item(
            exchange="gate",
            action=action_from_title(f"{title} {brief}", default_action),
            market_type=market_type_from_title(
                f"{title} {brief}",
                default=category_market,
            ),
            title=title,
            url=urljoin(source_url, article_path),
            published_at=iso_from_unix_timestamp(
                article.get("release_timestamp")
                or article.get("created_t")
                or article.get("updated_t")
            ),
            category=category,
            event_context=context or None,
        )
        for symbol in re.findall(
            r"(?<![A-Z0-9])[A-Z][A-Z0-9]{0,23}(?![A-Z0-9])",
            tags.upper(),
        ):
            add_symbol(item["symbols"], symbol)
        item["symbols"] = order_symbols_by_title(
            item["symbols"],
            f"{title} {brief} {tags}",
        )
        if gate_article_is_stock(title, brief):
            item["asset_type"] = "stock"
            item["asset_label"] = asset_label("stock")
        if is_relevant(item):
            rows.append(item)
    return rows


def gate_market_type_from_category(value: Any, default: str) -> str:
    try:
        category_id = int(value)
    except (TypeError, ValueError):
        return default
    if category_id == 37:
        return "contract"
    if category_id == 38:
        return "spot"
    return default


def gate_article_is_stock(title: str, brief: str) -> bool:
    text = f"{title} {brief}".lower()
    return any(
        keyword in text
        for keyword in (
            "gstocks",
            "合约股票",
            "代币化证券",
            "代幣化證券",
        )
    )


def fetch_gate_via_reader(client: httpx.Client) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for default_action, default_market, source_url in GATE_SECTION_URLS:
        section_rows: list[dict[str, Any]] = []
        section_errors: list[str] = []
        for reader_url in gate_reader_url_candidates(source_url):
            try:
                response = get_response(client, reader_url)
                response.raise_for_status()
                section_rows = extract_gate_reader_articles(
                    response.text,
                    default_action=default_action,
                    default_market=default_market,
                    category="Gate 公告",
                    client=client if exchange_fetch_details_enabled() else None,
                )
                break
            except Exception as exc:
                section_errors.append(str(exc))
        if section_rows:
            rows.extend(section_rows)
        elif section_errors:
            errors.append(
                f"{default_action}: "
                + "; ".join(error for error in section_errors if error)
            )
    if not rows and errors:
        raise RuntimeError("Gate 镜像读取失败：" + " | ".join(errors))
    return rows


def exchange_fetch_details_enabled() -> bool:
    return os.environ.get("EXCHANGE_ANN_FETCH_DETAILS", "0") == "1"


def gate_reader_url_candidates(source_url: str) -> list[str]:
    return [
        f"https://r.jina.ai/http://www.gate.com{source_url.removeprefix('https://www.gate.com')}",
    ]


def extract_gate_reader_articles(
    markdown_text: str,
    *,
    default_action: str,
    default_market: str,
    category: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = r"\[([^\]]+)\]\((https://www\.gate\.com/zh/announcements/article/[^)]+)\)"
    for match in re.finditer(pattern, markdown_text):
        link_text = normalize_text(match.group(1))
        url = match.group(2)
        if url in seen:
            continue
        seen.add(url)
        title, published_at = split_gate_reader_link_text(link_text)
        action = action_from_title(title, default_action)
        market_type = market_type_from_title(title, default=default_market)
        context = link_text
        if client and should_fetch_article_context("gate", title, action, market_type):
            article_context = fetch_article_context(client, "gate", url)
            if article_context:
                context = f"{context} {article_context}"
        item = build_item(
            exchange="gate",
            action=action,
            market_type=market_type,
            title=title,
            url=url,
            published_at=published_at,
            category=category,
            event_context=context,
        )
        if is_relevant(item):
            rows.append(item)
    return rows


def split_gate_reader_link_text(value: str) -> tuple[str, str | None]:
    match = re.match(r"^(.*?)\s+((?:\d+\s*(?:小时|天|月|年)前)|(?:20\d{2}-\d{2}-\d{2}))\s+[\d,]+$", value)
    if match:
        return normalize_text(match.group(1)), gate_reader_date_to_iso(match.group(2))
    return value, extract_date(value)


def gate_reader_date_to_iso(value: str) -> str | None:
    direct = extract_date(value)
    if direct:
        return direct
    match = re.match(r"(\d+)\s*(小时|天|月|年)前", value)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    now = datetime.now(timezone.utc)
    if unit == "小时":
        parsed = now - timedelta(hours=amount)
    elif unit == "天":
        parsed = now - timedelta(days=amount)
    elif unit == "月":
        parsed = now - timedelta(days=amount * 30)
    else:
        parsed = now - timedelta(days=amount * 365)
    return parsed.isoformat()


def http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(4.0, connect=3.0),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )


def get_response(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return client.get(url, **kwargs)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(0.3)
    if last_error:
        raise last_error
    raise RuntimeError("请求失败")


def extract_html_articles(
    exchange: str,
    soup: BeautifulSoup,
    base_url: str,
    default_action: str,
    default_market: str,
    category: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        title = normalize_text(anchor.get_text(" ", strip=True))
        href = str(anchor.get("href") or "")
        if not title or not looks_like_article_link(exchange, href):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        container = anchor.find_parent(["section", "article", "li", "div"]) or anchor.parent
        context = normalize_text(container.get_text(" ", strip=True) if container else title)
        published_at = extract_date(context)
        action = action_from_title(title, default_action)
        market_type = market_type_from_title(title, default=default_market)
        if client and should_fetch_article_context(exchange, title, action, market_type):
            article_context = fetch_article_context(client, exchange, url)
            if article_context:
                context = f"{context} {article_context}"
        item = build_item(
            exchange=exchange,
            action=action,
            market_type=market_type,
            title=title,
            url=url,
            published_at=published_at,
            category=category,
            event_context=context,
        )
        if is_relevant(item):
            rows.append(item)
    return rows


def should_fetch_article_context(exchange: str, title: str, action: str, market_type: str) -> bool:
    if exchange not in {"okx", "gate"}:
        return False
    if action not in {"listing", "delisting"}:
        return False
    title_lower = title.lower()
    return bool(
        extract_symbols(title)
        or market_type in {"spot", "contract"}
        or any(keyword in title_lower for keyword in STABLE_QUOTE_KEYWORDS)
    )


def fetch_article_context(client: httpx.Client, exchange: str, url: str) -> str | None:
    if exchange not in {"okx", "gate"}:
        return None
    best_text: str | None = None
    best_score = -1
    for article_url in article_context_urls(exchange, url):
        try:
            response = get_response(client, article_url)
            response.raise_for_status()
        except Exception:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        text = normalize_text(soup.get_text(" ", strip=True))
        text = trim_article_context(exchange, text)
        if text:
            score = article_context_score(exchange, text)
            if score > best_score:
                best_text = text
                best_score = score
    return best_text


def article_context_urls(exchange: str, url: str) -> list[str]:
    urls = [url]
    if exchange == "gate" and "/zh/announcements/article/" in url:
        urls.append(url.replace("/zh/announcements/article/", "/announcements/article/"))
    urls.extend(f"https://r.jina.ai/http://{candidate}" for candidate in list(urls))
    return ordered_unique(urls)


def article_context_score(exchange: str, text: str) -> int:
    if exchange == "gate":
        return len(strict_usdt_pair_symbols(text)) * 1000 + min(len(text), 1000)
    return len(text)


def trim_article_context(exchange: str, text: str) -> str:
    if exchange != "gate":
        return text
    article_heading = text.rfind(" # ")
    if article_heading >= 0:
        text = text[article_heading:].strip()
    footer_markers = (" Related Articles ", " Join Gate ", " About [About Us]", " Products [P2P]")
    footer_positions = [text.find(marker) for marker in footer_markers if text.find(marker) >= 0]
    if footer_positions:
        text = text[: min(footer_positions)].strip()
    return text


def looks_like_article_link(exchange: str, href: str) -> bool:
    if exchange == "okx":
        return "/help/" in href and "/help/section/" not in href and "/help/category/" not in href
    if exchange == "bg":
        return "/support/articles/" in href
    if exchange == "gate":
        return "/announcements/article/" in href
    return False


def build_item(
    *,
    exchange: str,
    action: str,
    market_type: str,
    title: str,
    url: str,
    published_at: str | None,
    category: str,
    event_context: str | None = None,
) -> dict[str, Any]:
    title = clean_title(normalize_text(html.unescape(title)))
    action = action_from_title(title, action)
    # Some exchanges omit the asset class from the localized headline while
    # retaining it in the canonical article slug (for example, `-equity`).
    # Include the official URL in classification so those listings keep their
    # stock marker even when article-body enrichment is unavailable.
    source_text = f"{title} {event_context or ''} {url}"
    symbols = extract_symbols(title, event_context)
    asset_type = asset_type_from_text(source_text, symbols)
    event_at = extract_event_at(f"{title} {event_context or ''}") or normalize_event_time(published_at)
    now = datetime.now(BEIJING_TZ)
    event_dt = parse_event_datetime(event_at)
    has_occurred = bool(event_dt and event_dt <= now)
    seconds_until_event = max(0, int((event_dt - now).total_seconds())) if event_dt and not has_occurred else 0
    return {
        "exchange": exchange,
        "exchange_name": EXCHANGE_LABELS[exchange],
        "action": action,
        "action_label": ACTION_LABELS.get(action, "其他"),
        "market_type": market_type,
        "market_label": market_label(market_type),
        "asset_type": asset_type,
        "asset_label": asset_label(asset_type),
        "title": title,
        "url": url,
        "published_at": published_at,
        "published_at_sort": sort_timestamp(published_at),
        "event_at": event_at,
        "event_at_sort": sort_timestamp(event_at),
        "has_occurred": has_occurred,
        "seconds_until_event": seconds_until_event,
        "event_status": "已发生" if has_occurred else "未发生",
        "category": category,
        "symbols": symbols,
        "raw_title": title,
    }


def is_relevant(item: dict[str, Any]) -> bool:
    title = item["title"].lower()
    if item["action"] not in MONITORED_ACTIONS:
        return False
    if not item["symbols"]:
        return False
    if item["market_type"] in {"spot", "contract", "contract_usd", "contract_usdc", "margin_loan"}:
        return True
    return any(keyword in title for keyword in STABLE_QUOTE_KEYWORDS)


def market_type_from_title(title: str, default: str = "unknown") -> str:
    lower = title.lower()
    if any(keyword in lower for keyword in ("funding rate", "funding interval", "资金费", "資金費")):
        return "contract"
    if any(keyword in lower for keyword in CONTRACT_KEYWORDS):
        quote = contract_quote_from_title(title)
        if quote == "usd":
            return "contract_usd"
        if quote == "usdc":
            return "contract_usdc"
        return "contract"
    if any(keyword in lower for keyword in MARGIN_LOAN_KEYWORDS):
        return "margin_loan"
    if any(keyword in lower for keyword in SPOT_KEYWORDS):
        return "spot"
    return default


def contract_quote_from_title(title: str) -> str | None:
    pair_quotes = contract_pair_quotes(title)
    if pair_quotes:
        if pair_quotes & TARGET_CONTRACT_QUOTES:
            return "usdt"
        if "usdc" in pair_quotes:
            return "usdc"
        if "usd" in pair_quotes:
            return "usd"

    lower = title.lower()
    if any(keyword in lower for keyword in STABLE_QUOTE_KEYWORDS) or "u本位" in lower:
        return "usdt"
    return None


def contract_pair_quotes(title: str) -> set[str]:
    normalized = (
        title.replace("（", " ")
        .replace("）", " ")
        .replace("，", ",")
        .replace("、", ",")
        .replace("ⓢ", "Ⓢ")
    )
    upper = normalized.upper()
    quotes: set[str] = set()
    pattern = r"(?<![A-Z0-9])([A-Z][A-Z0-9]{0,24})(USDT|USDC|USDⓈ|USDS|USD)(?=$|[^A-Z0-9])"
    for match in re.finditer(pattern, upper):
        base, quote = match.groups()
        if base in SYMBOL_EXCLUDE:
            continue
        normalized_quote = "usds" if quote in {"USDⓈ", "USDS"} else quote.lower()
        quotes.add(normalized_quote)
    return quotes


def market_label(market_type: str) -> str:
    if market_type == "contract":
        return "USDT合约"
    if market_type == "contract_usd":
        return "USD合约"
    if market_type == "contract_usdc":
        return "USDC合约"
    if market_type == "spot":
        return "现货"
    if market_type == "margin_loan":
        return "杠杆/借币"
    return "未分类"


def asset_type_from_text(text: str, symbols: list[str]) -> str:
    lower = text.lower()
    if any(keyword in lower for keyword in ("股票", "美股", "股本", "tradfi", "pre-ipo", "pre ipo")):
        return "stock"
    if any(symbol.upper().endswith("STOCK") for symbol in symbols):
        return "stock"
    if re.search(r"(?<![a-z])(?:stocks?|equities|equity|cfds?)(?![a-z])", lower):
        return "stock"
    return "crypto" if symbols else "unknown"


def propagate_stock_asset_classification(items: list[dict[str, Any]]) -> None:
    """Reuse official stock/TradFi evidence across rename and delisting notices."""

    stock_symbols = {
        normalize_group_symbol(symbol)
        for item in items
        if item.get("asset_type") == "stock"
        for symbol in item.get("symbols") or []
        if normalize_group_symbol(symbol)
    }
    if not stock_symbols:
        return
    for item in items:
        symbols = {
            normalize_group_symbol(symbol)
            for symbol in item.get("symbols") or []
            if normalize_group_symbol(symbol)
        }
        if symbols & stock_symbols:
            item["asset_type"] = "stock"
            item["asset_label"] = asset_label("stock")


def asset_label(asset_type: str) -> str:
    if asset_type == "stock":
        return "股票"
    if asset_type == "crypto":
        return "加密货币"
    return "未分类"


def action_from_title(title: str, default: str = "unknown") -> str:
    lower = title.lower()
    if any(keyword in lower for keyword in RESUMPTION_KEYWORDS) or re.search(
        r"\bresume(?:s|d|ing)?\b.{0,80}\btrading\b|恢复.{0,40}交易|恢復.{0,40}交易",
        lower,
    ):
        return "resumption"
    if any(keyword in lower for keyword in SUSPENSION_KEYWORDS) or re.search(
        r"\bsuspend(?:s|ed|ing)?\b.{0,80}\btrading\b|暂停.{0,40}交易|暫停.{0,40}交易",
        lower,
    ):
        return "suspension"
    if any(keyword in lower for keyword in MIGRATION_KEYWORDS):
        return "migration"
    if any(keyword in lower for keyword in DELISTING_KEYWORDS):
        return "delisting"
    if any(keyword in lower for keyword in PARAMETER_CHANGE_KEYWORDS):
        return "parameter_change"
    if any(keyword in lower for keyword in LISTING_KEYWORDS):
        return "listing"
    return default


def extract_symbols(title: str, context: str | None = None) -> list[str]:
    title_symbols = extract_symbols_from_text(title)
    context_pair_symbols = strict_usdt_pair_symbols(context or "")
    if len(context_pair_symbols) > 1:
        context_text = context or ""
        title_symbol_set = set(title_symbols)
        context_mentions_title_symbols = any(
            re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}\s*(?:[/_-]\s*)?USDT(?=$|[^A-Z0-9])", context_text)
            for symbol in title_symbol_set
        )
        if context_mentions_title_symbols:
            return order_symbols_by_title(ordered_unique([*title_symbols, *context_pair_symbols]), f"{title} {context_text}")
        return context_pair_symbols
    return order_symbols_by_title(ordered_unique([*title_symbols, *context_pair_symbols]), f"{title} {context or ''}")


def extract_symbols_from_text(text: str) -> list[str]:
    symbols: list[str] = []
    normalized = text.replace("（", "(").replace("）", ")").replace("，", ",").replace("、", ",")
    compact = normalized.replace("/", "").replace("_", "").replace(" ", "")

    for symbol in re.findall(r"([A-Z0-9]{1,24})(?:USDT|USDC|USDⓈ|USD)", compact):
        add_symbol(symbols, symbol)

    # Several 2026 contracts use a Chinese trading symbol verbatim (for
    # example 牛来USDT). The previous ASCII-only parser fetched the official
    # announcement and then discarded it as "no symbol".
    for symbol in re.findall(
        r"([\u3400-\u4dbf\u4e00-\u9fff]{1,16})\s*(?:[/_\-(（]\s*)?USDT",
        normalized,
        flags=re.IGNORECASE,
    ):
        add_symbol(symbols, symbol)

    for symbol in re.findall(
        r"(?:上线|上線|上架|上架|list|launch(?:es)?)\s*"
        r"([\u3400-\u4dbf\u4e00-\u9fff]{1,16})\s*"
        r"(?:USDT|[（(]\s*USDT|永续|永續|合约|合約)",
        normalized,
        flags=re.IGNORECASE,
    ):
        add_symbol(symbols, symbol)

    for symbol in re.findall(r"([A-Z0-9]{1,24})/(?:USDT|USDC|USDⓈ|USD)", normalized):
        add_symbol(symbols, symbol)

    for symbol in re.findall(r"[(]([A-Z][A-Z0-9]{0,24})(?:/(?:USDT|USDC|USDⓈ|USD))?[)]", normalized):
        add_symbol(symbols, symbol)

    for symbol in re.findall(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{0,24})\s*[(][^()]{1,40}[)]", normalized):
        add_symbol(symbols, symbol)

    for segment in re.findall(
        r"([A-Z][A-Z0-9]{0,24}(?:\s*(?:,|、|，|/|\band\b|和|及|&|\+)\s*[A-Z][A-Z0-9]{0,24}){1,30})\s*(?:USDT|USDC|USDⓈ|USD|永续|永續|合约|合約|现货|現貨|交易|股票|perpetual|futures|contracts?|spot|trading|equities|stocks?|cfds?)",
        normalized,
        flags=re.IGNORECASE,
    ):
        add_symbols_from_segment(symbols, segment)

    context_patterns = [
        r"(?:上线|上架|开启|新增|下架|移除|关于|list|launch|delist)\s*([A-Z][A-Z0-9]{0,24})\s*(?:现货|永续|合约|交易|专区|区|\b)",
        r"([A-Z][A-Z0-9]{0,24})\s*(?:永续合约|合约交易|现货交易|X-合约|专区)",
        r"[:：]\s*([A-Z][A-Z0-9]{0,24})\s*(?:CFD|現|现|已|上線|上线)",
    ]
    for pattern in context_patterns:
        for symbol in re.findall(pattern, normalized, flags=re.IGNORECASE):
            add_symbol(symbols, symbol)

    for segment in re.findall(
        r"(?:关于|上线|上架|下架|新增)\s*([A-Z0-9、,，\s和及&+/]+)\s*(?:股票)?(?:永续|永續|合约|合約|现货|現貨|交易|cfds?)",
        normalized,
        flags=re.IGNORECASE,
    ):
        add_symbols_from_segment(symbols, segment)

    for segment in re.findall(
        r"(?:下架|移除|delist|remove)\s*([A-Z0-9、,，\s和及&+/]+?)(?=\s*(?:（|\(|$|于|\bon\b|\bfrom\b|\beffective\b))",
        normalized,
        flags=re.IGNORECASE,
    ):
        add_symbols_from_segment(symbols, segment)

    return order_symbols_by_title(symbols, normalized)


def strict_usdt_pair_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    normalized = text.replace("（", " ").replace("）", " ").replace("，", ",").replace("、", ",")
    for match in re.finditer(
        r"(?<![A-Z0-9])([A-Z][A-Z0-9]{0,24})\s*(?:[/_-]\s*)?USDT(?=$|[^A-Z0-9])",
        normalized,
    ):
        add_symbol(symbols, match.group(1))
    for match in re.finditer(
        r"([\u3400-\u4dbf\u4e00-\u9fff]{1,16})\s*(?:[/_\-(]\s*)?USDT(?=$|[^A-Z0-9])",
        normalized,
        flags=re.IGNORECASE,
    ):
        add_symbol(symbols, match.group(1))
    return order_symbols_by_title(symbols, normalized)


def add_symbols_from_segment(symbols: list[str], segment: str) -> None:
    cleaned = re.sub(r"\b(?:and|or)\b|和|及|以及|、|，|,|/|&|\+", " ", segment, flags=re.IGNORECASE)
    for symbol in re.findall(r"[A-Z][A-Z0-9]{0,24}(?:USDT|USDC|USDⓈ|USD)?", cleaned):
        add_symbol(symbols, symbol)


def add_symbol(symbols: list[str], value: str) -> None:
    raw = value.strip("-_ /")
    if re.search(r"[a-z]", raw):
        return
    symbol = raw.upper()
    symbol = re.sub(r"(USDT|USDC|USDⓈ|USD|USDTM|USDCM)$", "", symbol)
    if not symbol or symbol in SYMBOL_EXCLUDE or len(symbol) > 24:
        return
    if symbol not in symbols:
        symbols.append(symbol)


def order_symbols_by_title(symbols: list[str], title: str) -> list[str]:
    positions: dict[str, int] = {}
    compact = title.replace("/", "").replace("_", "").replace(" ", "")
    for symbol in symbols:
        candidates = [
            title.find(f"({symbol})"),
            title.find(f"{symbol}/"),
            compact.find(f"{symbol}USDT"),
            compact.find(f"{symbol}USDC"),
            compact.find(f"{symbol}USD"),
            title.find(symbol),
        ]
        positions[symbol] = min([pos for pos in candidates if pos >= 0], default=10**9)
    return sorted(symbols, key=lambda symbol: (positions.get(symbol, 10**9), symbols.index(symbol)))


def merge_announcements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        key, symbol_label = announcement_group_identity(item)
        if key not in grouped:
            grouped[key] = {
                "key": key,
                "symbol": symbol_label,
                "title": "",
                "exchange_names": [],
                "exchange_codes": [],
                "action_label": "",
                "market_label": "",
                "asset_type": "unknown",
                "asset_label": asset_label("unknown"),
                "latest_published_at": None,
                "latest_published_at_sort": 0,
                "event_at": None,
                "event_at_sort": 0,
                "has_occurred": False,
                "seconds_until_event": 0,
                "event_status": "未发生",
                "announcements": [],
            }
        append_group_item(grouped[key], item)

    for group in grouped.values():
        group["title"] = build_summary_title(group)
    return list(grouped.values())


def announcement_group_identity(item: dict[str, Any]) -> tuple[str, str]:
    symbols = [normalize_group_symbol(symbol) for symbol in item.get("symbols", []) if normalize_group_symbol(symbol)]
    symbols = ordered_unique(symbols)
    if len(symbols) > 1:
        label = "/".join(symbols)
        raw = "|".join([str(item.get("exchange") or ""), str(item.get("url") or item.get("title") or label)])
        return f"article:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}", label
    if symbols:
        return symbols[0], symbols[0]
    fallback = fallback_symbol(item)
    return fallback, fallback


def append_group_item(group: dict[str, Any], item: dict[str, Any]) -> None:
    detail = {
        "exchange": item["exchange"],
        "exchange_name": item["exchange_name"],
        "action": item["action"],
        "action_label": item["action_label"],
        "market_type": item["market_type"],
        "market_label": item["market_label"],
        "asset_type": item.get("asset_type", "unknown"),
        "asset_label": item.get("asset_label", asset_label("unknown")),
        "title": item["title"],
        "url": item["url"],
        "published_at": item["published_at"],
        "event_at": item["event_at"],
        "has_occurred": item["has_occurred"],
        "seconds_until_event": item["seconds_until_event"],
        "event_status": item["event_status"],
        "category": item["category"],
        "symbols": item["symbols"],
        "discovery_source": item.get("discovery_source", "official_announcement"),
        "official_announcement_matched": item.get("official_announcement_matched"),
        "detected_at": item.get("detected_at"),
    }
    if not any(row["exchange"] == detail["exchange"] and row["url"] == detail["url"] for row in group["announcements"]):
        group["announcements"].append(detail)

    if item["exchange"] not in group["exchange_codes"]:
        group["exchange_codes"].append(item["exchange"])
        group["exchange_names"].append(item["exchange_name"])

    actions = ordered_unique([row["action_label"] for row in group["announcements"]])
    markets = ordered_unique([row["market_label"] for row in group["announcements"]])
    asset_types = ordered_unique([row.get("asset_type", "unknown") for row in group["announcements"]])
    group["action_label"] = "/".join(actions)
    group["market_label"] = "/".join(markets)
    group["asset_type"] = aggregate_asset_type(asset_types)
    group["asset_label"] = asset_label(group["asset_type"])

    published_sort = item.get("published_at_sort") or 0
    if published_sort >= group["latest_published_at_sort"]:
        group["latest_published_at_sort"] = published_sort
        group["latest_published_at"] = item.get("published_at")
    update_group_timing(group)


def update_group_timing(group: dict[str, Any]) -> None:
    now = datetime.now(BEIJING_TZ)
    details = group["announcements"]
    future_details = [
        row
        for row in details
        if parse_event_datetime(row.get("event_at")) and parse_event_datetime(row.get("event_at")) > now
    ]
    if future_details:
        selected = min(future_details, key=lambda row: parse_event_datetime(row.get("event_at")) or datetime.max.replace(tzinfo=BEIJING_TZ))
        event_dt = parse_event_datetime(selected.get("event_at"))
        group["event_at"] = selected.get("event_at")
        group["event_at_sort"] = event_dt.timestamp() if event_dt else 0
        group["has_occurred"] = False
        group["seconds_until_event"] = max(0, int((event_dt - now).total_seconds())) if event_dt else 0
        group["event_status"] = "未发生"
        return

    selected = max(details, key=lambda row: sort_timestamp(row.get("event_at")), default=None)
    event_dt = parse_event_datetime(selected.get("event_at")) if selected else None
    group["event_at"] = selected.get("event_at") if selected else None
    group["event_at_sort"] = event_dt.timestamp() if event_dt else 0
    group["has_occurred"] = True
    group["seconds_until_event"] = 0
    group["event_status"] = "已发生"


def build_summary_title(group: dict[str, Any]) -> str:
    exchanges = "/".join(group["exchange_names"])
    asset_suffix = "（股票）" if group.get("asset_type") == "stock" else ""
    return f"{group['symbol']}{asset_suffix} {group['market_label']} {group['action_label']}：{exchanges}"


def fallback_symbol(item: dict[str, Any]) -> str:
    if "多" in item["title"] or "Multiple" in item["title"]:
        return "多标的"
    return "未识别"


def normalize_group_symbol(symbol: str) -> str:
    return symbol.upper().replace("USDT", "").strip() or "未识别"


def ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def aggregate_asset_type(asset_types: list[str]) -> str:
    if "stock" in asset_types:
        return "stock"
    if "crypto" in asset_types:
        return "crypto"
    return "unknown"


def extract_event_at(text: str) -> str | None:
    normalized = normalize_text(text.replace("（", " ").replace("）", " "))
    candidates: list[dict[str, Any]] = []

    for match in re.finditer(
        r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*(?:at|于|起|,|，|:|：)?\s*(上午|下午|晚上|中午|凌晨)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
        normalized,
        flags=re.IGNORECASE,
    ):
        year, month, day, period, hour, minute, second = match.groups()
        add_event_time_candidate(candidates, normalized, match.start(), match.end(), year, month, day, hour, minute, second, period)

    for match in re.finditer(
        r"(\d{1,2})月(\d{1,2})日\s*(?:at|于|起|,|，|:|：)?\s*(上午|下午|晚上|中午|凌晨)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
        normalized,
        flags=re.IGNORECASE,
    ):
        month, day, period, hour, minute, second = match.groups()
        add_event_time_candidate(
            candidates,
            normalized,
            match.start(),
            match.end(),
            str(datetime.now(BEIJING_TZ).year),
            month,
            day,
            hour,
            minute,
            second,
            period,
        )

    month_names = "|".join(sorted(EN_MONTHS, key=len, reverse=True))
    for match in re.finditer(
        rf"\b({month_names})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})\s*(?:[,，]?\s*(?:at|于|起)?\s*)?(\d{{1,2}}):(\d{{2}})\s*(am|pm)?",
        normalized,
        flags=re.IGNORECASE,
    ):
        month_name, day, year, hour, minute, ampm = match.groups()
        add_event_time_candidate(
            candidates,
            normalized,
            match.start(),
            match.end(),
            year,
            str(EN_MONTHS[month_name.lower()]),
            day,
            hour,
            minute,
            None,
            ampm,
        )

    for match in re.finditer(
        rf"\b(\d{{1,2}}):(\d{{2}})\s*(am|pm)?\s*(?:\(?\s*(?:utc|gmt)\s*\)?\s*[,，]?\s*)?(?:on\s+)?({month_names})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})",
        normalized,
        flags=re.IGNORECASE,
    ):
        hour, minute, ampm, month_name, day, year = match.groups()
        add_event_time_candidate(
            candidates,
            normalized,
            match.start(),
            match.end(),
            year,
            str(EN_MONTHS[month_name.lower()]),
            day,
            hour,
            minute,
            None,
            ampm,
        )

    if candidates:
        candidates.sort(key=lambda row: (row["score"], row["timestamp"], -row["start"]), reverse=True)
        return str(candidates[0]["event_at"])
    return normalize_event_time(extract_date(normalized))


def add_event_time_candidate(
    candidates: list[dict[str, Any]],
    text: str,
    start: int,
    end: int,
    year: str,
    month: str,
    day: str,
    hour: str,
    minute: str,
    second: str | None,
    period: str | None,
) -> None:
    normalized_hour = normalize_hour(int(hour), period)
    try:
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            normalized_hour,
            int(minute),
            int(second or 0),
            tzinfo=timezone_from_context(text, start, end),
        )
    except ValueError:
        return
    event_at = parsed.astimezone(BEIJING_TZ).isoformat()
    candidates.append(
        {
            "event_at": event_at,
            "score": event_time_score(text, start, end),
            "timestamp": parsed.timestamp(),
            "start": start,
        }
    )


def normalize_hour(hour: int, period: str | None) -> int:
    marker = (period or "").lower()
    if marker in {"pm", "下午", "晚上"} and hour < 12:
        return hour + 12
    if marker == "中午" and hour < 11:
        return hour + 12
    if marker in {"am", "上午", "凌晨"} and hour == 12:
        return 0
    return hour


def timezone_from_context(text: str, start: int, end: int) -> timezone:
    window = text[max(0, start - 48) : min(len(text), end + 48)].lower()
    offset = re.search(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})", window)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        return timezone(timedelta(hours=sign * int(offset.group(2))))
    if "北京时间" in window or "香港时间" in window or "新加坡时间" in window:
        return BEIJING_TZ
    if "utc" in window or "gmt" in window or "协调世界时" in window or "世界标准时间" in window:
        return timezone.utc
    return BEIJING_TZ


def event_time_score(text: str, start: int, end: int) -> int:
    window = text[max(0, start - 120) : min(len(text), end + 120)].lower()
    near = text[max(0, start - 36) : min(len(text), end + 36)].lower()
    score = 0
    if any(keyword in window for keyword in EVENT_TIME_STRONG_KEYWORDS):
        score += 200
    if any(keyword in window for keyword in EVENT_TIME_KEYWORDS):
        score += 80
    if any(keyword in window for keyword in NON_EVENT_TIME_KEYWORDS):
        score -= 120
    if any(keyword in near for keyword in ("发布", "發布", "published", "公告", "announcement")):
        score -= 120
    if any(keyword in near for keyword in ("交易", "trading", "open", "opens", "开放", "開放")):
        score += 80
    return score


def extract_date(text: str) -> str | None:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?\b", text)
    if match:
        if match.group(2):
            return f"{match.group(1)}T{match.group(2)}:00"
        return f"{match.group(1)}T00:00:00"
    match = re.search(r"Published on\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s+(20\d{2})", text)
    if match:
        try:
            parsed = datetime.strptime(" ".join(match.groups()), "%b %d %Y")
            return parsed.strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            return None
    match = re.search(r"发布于\s*(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}T00:00:00"
    return None


def iso_from_millis(value: Any) -> str | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def iso_from_unix_timestamp(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def sort_timestamp(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["exchange"], item["url"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def filter_active_announcements(
    items: list[dict[str, Any]],
    diagnostics: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    now = datetime.now(BEIJING_TZ)
    today = now.date()
    delisting_event_end = today + timedelta(days=DELISTING_LOOKAHEAD_DAYS)
    delisting_published_start = today - timedelta(days=DELISTING_PUBLISHED_LOOKBACK_DAYS)
    listing_event_end = today + timedelta(days=LISTING_LOOKAHEAD_DAYS)
    listing_published_start = today - timedelta(days=LISTING_PUBLISHED_LOOKBACK_DAYS)
    general_event_start = today - timedelta(days=GENERAL_EVENT_LOOKBACK_DAYS)
    general_event_end = today + timedelta(days=GENERAL_EVENT_LOOKAHEAD_DAYS)
    general_published_start = today - timedelta(days=GENERAL_EVENT_PUBLISHED_LOOKBACK_DAYS)
    active: list[dict[str, Any]] = []
    expired = 0
    for item in items:
        event_dt = parse_event_datetime(item.get("event_at"))
        published_dt = parse_event_datetime(item.get("published_at"))
        if not event_dt:
            expired += 1
            increment_diagnostic(diagnostics, "missing_event_time")
            continue
        if item.get("action") == "delisting":
            event_date = event_dt.date()
            published_date = published_dt.date() if published_dt else None
            if event_date < today or event_date > delisting_event_end:
                expired += 1
                increment_diagnostic(diagnostics, "delisting_event_outside_window")
                continue
            if published_date and (published_date < delisting_published_start or published_date > today):
                expired += 1
                increment_diagnostic(diagnostics, "delisting_published_outside_window")
                continue
        elif item.get("action") == "listing":
            event_date = event_dt.date()
            published_date = published_dt.date() if published_dt else None
            if event_date < today or event_date > listing_event_end:
                expired += 1
                increment_diagnostic(diagnostics, "listing_event_outside_window")
                continue
            if published_date and (published_date < listing_published_start or published_date > today):
                expired += 1
                increment_diagnostic(diagnostics, "listing_published_outside_window")
                continue
        else:
            event_date = event_dt.date()
            published_date = published_dt.date() if published_dt else None
            if event_date < general_event_start or event_date > general_event_end:
                expired += 1
                increment_diagnostic(diagnostics, "general_event_outside_window")
                continue
            if published_date and (published_date < general_published_start or published_date > today):
                expired += 1
                increment_diagnostic(diagnostics, "general_published_outside_window")
                continue
        item["has_occurred"] = event_dt <= now
        item["seconds_until_event"] = max(0, int((event_dt - now).total_seconds())) if event_dt > now else 0
        item["event_status"] = "已发生" if item["has_occurred"] else "未发生"
        active.append(item)
        increment_diagnostic(diagnostics, "active")
    return active, expired


def increment_diagnostic(diagnostics: dict[str, int] | None, key: str) -> None:
    if diagnostics is not None:
        diagnostics[key] = diagnostics.get(key, 0) + 1


def parse_event_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def utc_datetime_to_beijing_iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING_TZ).isoformat()


def normalize_event_time(value: str | None) -> str | None:
    parsed = parse_event_datetime(value)
    return parsed.isoformat() if parsed else None


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def clean_title(value: str) -> str:
    value = re.sub(r"\s+Published on\s+[A-Z][a-z]{2}\s+\d{1,2},\s+20\d{2}.*$", "", value)
    value = re.sub(r"\s+发布于\s*20\d{2}年\d{1,2}月\d{1,2}日.*$", "", value)
    return normalize_text(value)

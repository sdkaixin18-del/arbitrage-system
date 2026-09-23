from __future__ import annotations

import json
import math
import os
import threading
import time
import hmac
import hashlib
import base64
import uuid
import urllib.request
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import fmean, median, pstdev
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, desc, select, update
from sqlalchemy.orm import Session

from app.models import (
    CryptoBoardSetting,
    CryptoBorrowWatchItem,
    CryptoBorrowWatchLog,
    CryptoCoinStatusLog,
    CryptoCompoundOpenSignalLog,
    CryptoFsObservationLog,
    CryptoFsRuntimeLog,
    CryptoFsSignalLog,
    CryptoFundingCapEvent,
    CryptoFundingCapSnapshot,
    CryptoFundingCapWatchItem,
    CryptoIndexComponentChangeLog,
    CryptoMarketQuoteSnapshot,
    CryptoMonitorEvent,
    CryptoPushLog,
    CryptoPushRule,
    CryptoSpreadSnapshot,
    CryptoSymbolMapping,
    CryptoWatchItem,
)
from app.asset_aliases import (
    resolve_canonical_asset as resolve_asset_canonical_symbol,
    resolve_exchange_symbol as resolve_asset_exchange_symbol,
)
from app.astro_sdk import (
    AstroSdkConfig,
    assess_astro_fs_borrow_signal,
    astro_auto_card_status,
    astro_sdk_config,
    build_astro_fs_borrow_pairs,
    build_astro_fs_pair,
    schedule_astro_pairs,
)
from app.astro_spread_scanner import astro_spread_scanner_status
from app.funding_formation import (
    build_cycle_premium_average_history,
    analyze_funding_formation,
    analyze_gate_shadow,
    analyze_rolling_reference,
    funding_formation_rule,
    gate_shadow_rule,
)
from app.notifications import send_bark_or_log
from app.kstr_quote_archive import try_archive_kstr_quote_payload
from app.kstr_runtime_log import append_kstr_runtime_event

SUPPORTED_EXCHANGES = {"bn", "by", "gt", "okx", "bg", "htx", "as", "hl"}
SUPPORTED_MARKET_TYPES = {"spot", "futures"}
BOARD_EXCHANGES = ["bn", "by", "gt", "okx", "bg", "as", "hl"]
BOARD_MARKET_TYPES = {
    "bn": ("futures", "spot"),
    "by": ("futures", "spot"),
    "gt": ("futures", "spot"),
    "okx": ("futures", "spot"),
    "as": ("futures",),
    "hl": ("futures",),
}
DEFAULT_MIN_QUOTE_VOLUME_24H_USDT = 30_000.0
DEFAULT_BASE_URLS = {
    "bn": "https://fapi.binance.com",
    "by": "https://api.bybit.com",
    "gt": "https://api.gateio.ws",
    "okx": "https://www.okx.com",
    "bg": "https://api.bitget.com",
    "htx": "https://api.hbdm.com",
    "as": "https://fapi.asterdex.com",
    "hl": "https://api.hyperliquid.xyz",
}
DEFAULT_SPOT_BASE_URLS = {
    "bn": "https://api.binance.com",
    "by": "https://api.bybit.com",
    "gt": "https://api.gateio.ws",
    "okx": "https://www.okx.com",
    "bg": "https://api.bitget.com",
    "htx": "https://api.huobi.pro",
    "as": "https://fapi.asterdex.com",
    "hl": "https://api.hyperliquid.xyz",
}
EXCHANGE_NAMES = {
    "bn": "Binance",
    "by": "Bybit",
    "gt": "Gate",
    "okx": "OKX",
    "bg": "Bitget",
    "htx": "HTX",
    "as": "Aster",
    "hl": "Hyperliquid",
}
FUNDING_HISTORY_CACHE_SECONDS = 60
_funding_history_cache: dict[tuple[str, str, int], tuple[datetime, list[dict[str, Any]], str | None]] = {}
FUNDING_FORMATION_CACHE_SECONDS = 60
FUNDING_FORMATION_STALE_MAX_AGE_SECONDS = 6 * 60 * 60
FUNDING_FORMATION_CACHE_MAX_ITEMS = max(
    1,
    min(int(os.environ.get("FUNDING_FORMATION_CACHE_MAX_ITEMS", "200")), 2_000),
)
_funding_formation_cache_lock = threading.Lock()
_funding_formation_cache: OrderedDict[
    tuple[str, str, float | None],
    tuple[datetime, dict[str, Any]],
] = OrderedDict()
ARB_RADAR_BOOK_CACHE_SECONDS = 1
_arb_radar_book_cache_lock = threading.Lock()
_arb_radar_book_cache: dict[tuple[str, ...], tuple[datetime, dict[str, Any]]] = {}
ARB_RADAR_ASTRO_DEFAULT_MAX_AGE_SECONDS = 2.0
ARB_RADAR_USDT_CNY_CACHE_SECONDS = 30
_arb_radar_usdt_cny_cache_lock = threading.Lock()
_arb_radar_usdt_cny_cache: tuple[datetime, dict[str, Any]] | None = None
ARB_RADAR_KSTR_BASELINE_CACHE_SECONDS = 30 * 60
ARB_RADAR_KSTR_BASELINE_SAMPLE_TIMES = (
    "09:45",
    "10:30",
    "11:15",
    "13:15",
    "14:00",
    "14:45",
)
ARB_RADAR_KSTR_BASELINE_MIN_DAILY_SAMPLES = 4
_arb_radar_kstr_baseline_cache_lock = threading.Lock()
_arb_radar_kstr_baseline_cache: tuple[datetime, dict[str, Any]] | None = None
_arb_radar_unitree_baseline_cache_lock = threading.Lock()
_arb_radar_unitree_baseline_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
FUNDING_FORMATION_RULE_SOURCES = {
    "bn": "https://www.binance.com/en/support/faq/detail/360033525031",
    "by": "https://www.bybit.com/en/help-center/article/Introduction-to-Funding-Rate",
    "gt": "https://www.gate.com/help/futures/futures-logic/27569/funding-rate-and-funding",
    "okx": "https://www.okx.com/help/perps-funding-fee-mechanism",
    "bg": "https://www.bitget.com/support/articles/12560603817108",
}
TURNOVER_4H_CACHE_SECONDS = 60
_turnover_4h_cache: dict[tuple[str, str, str], tuple[datetime, float | None]] = {}
BOARD_CACHE_SECONDS = 30
_board_cache_lock = threading.Lock()
_board_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
ASTRO_CACHE_SECONDS = 20
ASTRO_EXCHANGE_KEY_MAP = {
    "binance": "bn",
    "bybit": "by",
    "gate": "gt",
    "okx": "okx",
    "bitget": "bg",
    "htx": "htx",
    "aster": "as",
    "hl": "hl",
}
_astro_cache_lock = threading.Lock()
_astro_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
MARGIN_SHORT_CACHE_SECONDS = 30
_margin_short_cache_lock = threading.Lock()
_margin_short_cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
MARGIN_PRIVATE_API_EXCHANGES = {"bg", "bn", "by", "gt", "okx"}
MARGIN_SCAN_MAX_WORKERS = 2
FS_SIGNAL_FUTURES_EXCHANGES = ("bn", "by", "gt", "okx", "bg")
FS_SIGNAL_SPOT_EXCHANGES = ("bg", "bn")
FS_SIGNAL_SPOT_EXCHANGE = "bg"
FS_BORROW_CACHE_SECONDS = 300
PAIR_SPREAD_EXCHANGES = ("bn", "by", "gt", "okx", "bg", "hl")
PAIR_SPREAD_HL_MARKET_CACHE_SECONDS = 600
_pair_spread_hl_market_cache_lock = threading.Lock()
_pair_spread_hl_market_cache: tuple[datetime, tuple[str, ...]] | None = None
PAIR_SPREAD_LATEST_CACHE_SECONDS = 300
_pair_spread_latest_cache_lock = threading.Lock()
_pair_spread_latest_cache: dict[tuple[str, str, str, str, float], tuple[datetime, dict[str, Any]]] = {}
SK_HYNIX_STOCK_QUOTE_CACHE_SECONDS = 4
_sk_hynix_stock_quote_cache_lock = threading.Lock()
_sk_hynix_stock_quote_cache: tuple[datetime, dict[str, Any]] | None = None
SK_HYNIX_STOCK_HISTORY_CACHE_SECONDS = 300
_sk_hynix_stock_history_cache_lock = threading.Lock()
_sk_hynix_stock_history_cache: tuple[datetime, dict[str, Any]] | None = None
KSTR_SPREAD_CACHE_SECONDS = 4
KSTR_HISTORY_CACHE_SECONDS = 300
KSTR_ETF_COMPARISON_CACHE_SECONDS = 900
KSTR_STRUCTURAL_MODEL_CACHE_SECONDS = 3600
KSTR_PAGE_SNAPSHOT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
KSTR_PAGE_SNAPSHOT_WRITE_SECONDS = 60
KSTR_AUCTION_ARCHIVE_DAYS = 40
KSTR_CONTINUOUS_TICK_RETENTION_DAYS = 2
KSTR_CONTINUOUS_TICK_LIMIT = 3500
KSTR_DEFAULT_A_ETF_CODE = "588000"
KSTR_DEFAULT_CONTRACT_EXCHANGE = "bn"
KSTR_MIN_A_ETF_DAY_TURNOVER_CNY = 100_000_000
KSTR_CONTRACT_OPTIONS = (
    {
        "exchange": "bn",
        "exchangeName": "Binance",
        "symbol": "KSTRUSDT",
        "label": "Binance KSTRUSDT",
        "contractType": "TRADIFI_PERPETUAL",
        "underlyingType": "EQUITY",
        "sourceName": "Binance USDⓈ-M Futures",
        "sourceUrl": "https://www.binance.com/zh-CN/futures/KSTRUSDT",
    },
)
KSTR_CONTRACT_BY_EXCHANGE = {
    item["exchange"]: item for item in KSTR_CONTRACT_OPTIONS
}
KSTR_A_ETF_OPTIONS = (
    {
        "code": "588000",
        "symbol": "sh588000",
        "name": "科创50ETF华夏",
        "manager": "华夏基金",
        "fullName": "华夏上证科创板50成份交易型开放式指数证券投资基金",
        "optionEligible": True,
    },
)
KSTR_A_ETF_BY_CODE = {item["code"]: item for item in KSTR_A_ETF_OPTIONS}
_kstr_spread_cache_lock = threading.Lock()
_kstr_spread_cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_kstr_page_snapshot_lock = threading.Lock()
_kstr_page_snapshot_memory: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_kstr_page_snapshot_disk_write_at: dict[tuple[str, str], datetime] = {}
_kstr_page_refresh_threads: dict[tuple[str, str], threading.Thread] = {}
_kstr_history_cache_lock = threading.Lock()
_kstr_structural_model_cache_lock = threading.Lock()
_kstr_structural_model_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

# 2026-07-21 KSTR official holdings versus the 2026-06-30 STAR 50 close-weight
# file drifted forward with 2026-07-21 constituent closes. KSTR omits SMIC under
# the U.S. restricted-security rules, so the mismatch cannot be removed by one
# scalar hedge ratio alone.
KSTR_COMPONENT_MISMATCH_SNAPSHOT = {
    "asOf": "2026-07-21",
    "indexWeightBaseDate": "2026-06-30",
    "sameIndex": True,
    "kstrUniqueEquityCount": 49,
    "indexConstituentCount": 50,
    "kstrStockExposurePct": 98.87,
    "missingConstituents": [
        {
            "code": "688981",
            "name": "中芯国际",
            "estimatedIndexWeightPct": 8.71,
            "reason": "KSTR官方持仓未持有；美国限制证券规则下采用优化跟踪",
        }
    ],
    "estimatedActiveSharePct": 8.71,
    "kstrHoldingsUrl": "https://www.kraneshares.com/csv/07_21_2026_kstr_holdings.csv",
    "indexConstituentsUrl": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000688cons.xls",
    "indexWeightsUrl": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/closeweight/000688closeweight.xls",
    "restrictionUrl": "https://kraneshares.com/important-notice-regarding-executive-order-13959-and-office-of-foreign-assets-control-regarding-certain-chinese-securities/",
}
_kstr_history_cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_kstr_etf_comparison_cache_lock = threading.Lock()
_kstr_etf_comparison_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
_kstr_auction_ticks_lock = threading.Lock()
_kstr_auction_ticks: dict[tuple[str, str], list[dict[str, Any]]] = {}
_kstr_auction_archive_lock = threading.Lock()
_kstr_auction_archive_pruned_on: dict[Path, str] = {}
_kstr_continuous_ticks_lock = threading.Lock()
_kstr_continuous_ticks: dict[tuple[str, str], list[dict[str, Any]]] = {}


def astro_enabled() -> bool:
    return os.environ.get("CRYPTO_ASTRO_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
FS_SIGNAL_EXCHANGE_CHECKS = FS_SIGNAL_SPOT_EXCHANGES
FS_SIGNAL_NET_THRESHOLD = 0.0005
FS_SIGNAL_FEE_RATE = 0.0008
FS_SIGNAL_SLIPPAGE_RATE = 0.0005
FS_SIGNAL_BASIS_WATCH_RISK_RATE = 0.0005
FS_SIGNAL_BASIS_DANGER_RISK_RATE = 0.003
FS_REALTIME_MAX_CONTRACTS_PER_EXCHANGE = 240
FS_OKX_MAX_CONTRACTS_PER_SCAN = 500
FS_OKX_ACTIVE_CONTRACTS_PER_SCAN = 80
FS_OKX_ROTATION_CONTRACTS_PER_SCAN = 100
FS_OKX_INSTRUMENT_CACHE_SECONDS = 300
FS_CANDIDATE_SOURCE_STALE_SECONDS = 600
FS_CANDIDATE_SOURCE_FAILURE_COOLDOWN_SECONDS = 60
FS_SIGNAL_DEEP_DISCOUNT_THRESHOLD = -0.02
FS_SIGNAL_LARGE_SPREAD_THRESHOLD = 0.01
FS_SIGNAL_ABS_BASIS_MISMATCH_THRESHOLD = 0.08
FS_SIGNAL_IDEAL_BASIS_LOW = -0.01
FS_SIGNAL_IDEAL_BASIS_HIGH = 0.005
FS_SIGNAL_COOLDOWN_MINUTES = 30
FS_SIGNAL_CACHE_SECONDS = 60
FS_SIGNAL_CACHE_VERSION = 22
FS_SIGNAL_MAX_MINUTES_TO_FUNDING = 90
FS_SIGNAL_WATCH_MIN_NEGATIVE_RATE = 0.003
FS_SIGNAL_WATCH_MIN_NEGATIVE_DAILY_RATE = 0.01
FF_SIGNAL_LOOKBACK_MINUTES = 30
FF_SIGNAL_WATCH_THRESHOLD = 0.7
FF_SIGNAL_STRONG_THRESHOLD = 1.4
FF_SIGNAL_JUMP_THRESHOLD = 0.15
COMPOUND_OPEN_SIGNAL_COOLDOWN_MINUTES = 30
COMPOUND_OPEN_SIGNAL_REVIEW_MINUTES = (30, 60)
COMPOUND_OPEN_SIGNAL_SNAPSHOT_WINDOW_SECONDS = 90
COMPOUND_OPEN_SIGNAL_SOURCES = ("exchange_api", "astro_trigger", "astro_candidate")
COMPOUND_OPEN_SIGNAL_ENABLED = False
COMPOUND_OPEN_SIGNAL_SCAN_SECONDS = 30
COMPOUND_OPEN_SIGNAL_MAX_OPEN_SPREAD_PCT = 8.0
COMPOUND_OPEN_SIGNAL_MAX_FUTURES_INDEX_DIFF_PCT = 1.0
COMPOUND_OPEN_SIGNAL_CONFIRM_WINDOW_SECONDS = 35
COMPOUND_OPEN_SIGNAL_MIN_CONFIRM_POINTS = 2
BORROW_WATCH_EXCHANGES = ("bg", "bn", "by", "gt", "okx")
BORROW_WATCH_REFRESH_SECONDS = 30
BORROW_WATCH_ALLOWED_REFRESH_SECONDS = (10, 30, 60)
BORROW_WATCH_COOLDOWN_MINUTES = 30
MIN_BORROWABLE_VALUE_USDT = 100.0
COIN_STATUS_EXCHANGES = ("bg", "bn", "by", "gt", "okx")
COIN_INDEX_EXCHANGES = ("bn", "by", "okx", "bg")
COIN_TRANSFER_CACHE_SECONDS = 300
COIN_INDEX_CURRENT_CACHE_SECONDS = 60
COIN_INDEX_WATCH_CACHE_SECONDS = 300
COIN_INDEX_CHANGE_THRESHOLD = 0.01
DELISTED_CRYPTO_SYMBOLS = {"NFP"}
FS_SYMBOL_MISMATCH_EXCLUSIONS = {
    ("EDGE", "gt", "bg"): "Gate 的 EDGE 与 Bitget 的 EDGE 不是同一资产，已排除。",
}
_fs_signal_cache_lock = threading.Lock()
_fs_signal_cache: dict[int, tuple[datetime, dict[str, Any]]] = {}
_fs_signal_scan_state: dict[int, dict[str, Any]] = {}
_fs_borrow_cache_lock = threading.Lock()
_fs_borrow_cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_fs_futures_market_cache_lock = threading.Lock()
_fs_futures_market_cache: tuple[datetime, dict[str, set[str]]] | None = None
_borrow_watch_scan_lock = threading.Lock()
_borrow_watch_scan_state: dict[str, Any] = {"running": False, "startedAt": None, "finishedAt": None}
_coin_status_scan_lock = threading.Lock()
_coin_status_scan_state: dict[str, Any] = {"running": False, "startedAt": None, "finishedAt": None}
_compound_open_scan_lock = threading.Lock()
_compound_open_scan_state: dict[str, Any] = {"running": False, "startedAt": None, "finishedAt": None, "checkedCount": None, "createdCount": None}
_coin_status_cache_lock = threading.Lock()
_coin_transfer_cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_coin_index_cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_binance_transfer_config_cache_lock = threading.Lock()
_binance_transfer_config_cache: tuple[datetime, dict[str, dict[str, Any]]] | None = None
BINANCE_SERVER_TIME_CACHE_SECONDS = 30
_binance_server_time_lock = threading.Lock()
_binance_server_time_offset_cache: tuple[float, int] | None = None
BINANCE_MARGIN_INVENTORY_CACHE_SECONDS = 10
_binance_margin_inventory_cache_lock = threading.Lock()
_binance_margin_inventory_cache: tuple[datetime, dict[str, float]] | None = None
BINANCE_SPOT_MARKET_CACHE_SECONDS = 300
_binance_spot_market_cache_lock = threading.Lock()
_binance_spot_market_cache: tuple[datetime, set[str]] | None = None
BINANCE_MARGIN_RATE_CACHE_SECONDS = 60
_binance_margin_rate_cache_lock = threading.Lock()
_binance_margin_rate_cache: dict[str, tuple[datetime, float | None]] = {}
_api_rate_limit_lock = threading.Lock()
_api_rate_limit_condition = threading.Condition(_api_rate_limit_lock)
_api_rate_limit_next_at: dict[str, float] = {}
_api_rate_limit_backoff_until: dict[str, float] = {}
_api_priority_waiters: dict[str, int] = defaultdict(int)
_api_priority_grants: dict[str, int] = defaultdict(int)
_api_priority_local = threading.local()
_api_priority_astro_active_count = 0
FUNDING_FORMATION_API_LANE = "funding_formation"
EXCHANGE_ANNOUNCEMENT_API_LANE = "exchange_announcements"
_okx_funding_scan_lock = threading.Lock()
_okx_swap_instruments_cache: tuple[datetime, list[dict[str, Any]]] | None = None
_okx_previous_negative_instruments: set[str] = set()
_okx_rotation_cursor = 0
_okx_funding_scan_state: dict[str, Any] = {
    "availableCount": 0,
    "selectedCount": 0,
    "succeededCount": 0,
    "failedCount": 0,
    "negativeCount": 0,
    "coverageMode": "priority_rotation",
    "updatedAt": None,
}
_fs_candidate_source_lock = threading.Lock()
_fs_candidate_source_cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
_fs_candidate_source_failures: dict[str, tuple[int, float]] = {}

FS_BORROW_FAST_SCAN_INTERVAL_SECONDS = 30
FS_BORROW_FAST_SCAN_LIMIT = 8
FS_BORROW_FAST_RECHECK_LIMIT = 3
_fs_borrow_fast_scan_lock = threading.Lock()
_fs_borrow_fast_scan_state: dict[str, Any] = {
    "running": False,
    "startedAt": None,
    "finishedAt": None,
    "lastError": None,
    "candidateCount": 0,
    "checkedCount": 0,
    "fundingRefreshedCount": 0,
    "transitionCount": 0,
}
FUNDING_CAP_WATCH_EXCHANGES = (*FS_SIGNAL_FUTURES_EXCHANGES, "as")
FUNDING_CAP_WATCH_MAX_SYMBOLS = 20
FUNDING_CAP_WATCH_DEFAULT_EXCHANGES_JSON = json.dumps(
    FUNDING_CAP_WATCH_EXCHANGES,
    ensure_ascii=False,
    separators=(",", ":"),
)
_funding_cap_watch_scan_lock = threading.Lock()
_funding_cap_watch_scan_state: dict[str, Any] = {
    "running": False,
    "startedAt": None,
    "finishedAt": None,
    "lastError": None,
    "checkedSymbolCount": 0,
    "changedCount": 0,
    "pushedCount": 0,
    "status": "idle",
    "message": "未添加币种，当前不监控资金费规则。",
}

# Conservative client-side pacing. Do not aim at the published hard caps; leave
# room for the web UI, retries, and other local refresh jobs using the same keys.
API_RATE_LIMIT_INTERVALS_SECONDS = {
    "bn:public": 0.25,
    "bn:private": 0.35,
    "bn:margin_heavy": 5.0,
    "by:public": 0.25,
    "by:private": 0.35,
    "gt:public": 0.20,
    "gt:private": 0.35,
    "okx:public": 0.18,
    "okx:private": 0.55,
    "bg:public": 0.20,
    "bg:private": 0.35,
    "htx:public": 0.25,
    "as:public": 0.25,
    "hl:public": 0.35,
    "default": 0.25,
}


@dataclass
class MarketQuote:
    exchange: str
    symbol: str
    market_type: str = "futures"
    best_bid: float | None = None
    best_ask: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    premium_rate: float | None = None
    next_funding_time: datetime | None = None
    period_hours: float | None = None
    max_funding_rate: float | None = None
    min_funding_rate: float | None = None
    interest_rate: float | None = None
    funding_rule: str | None = None
    funding_formula: str | None = None
    premium_source: str | None = None
    open_interest: float | None = None
    risk_fund: float | None = None
    volume_24h: float | None = None
    updated_at: datetime | None = None
    status: str = "ok"
    error: str | None = None
    # Keep legacy period_hours display defaults separate from verified evidence.
    funding_period_verified: bool = False
    funding_period_source: str = "unknown"


@dataclass
class OrderBookLevel:
    side: str
    level: int
    price: float | None = None
    size: float | None = None

    @property
    def notional(self) -> float | None:
        if self.price is None or self.size is None:
            return None
        return self.price * self.size


@dataclass
class OrderBookQuote:
    exchange: str
    symbol: str
    market_type: str
    amount_unit: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    turnover_4h_usdt: float | None = None
    updated_at: datetime | None = None
    status: str = "ok"
    error: str | None = None


@dataclass
class FundingHistoryItem:
    exchange: str
    symbol: str
    funding_rate: float | None = None
    premium_rate: float | None = None
    mark_price: float | None = None
    funding_time: datetime | None = None


@dataclass
class MarginShortCheck:
    exchange: str
    symbol: str
    status: str
    message: str
    can_borrow: bool | None = None
    inventory_available: bool | None = None
    borrowable_amount: float | None = None
    borrowable_value_usdt: float | None = None
    hourly_borrow_rate: float | None = None
    daily_borrow_rate: float | None = None
    updated_at: datetime | None = None


@dataclass
class CoinChainStatus:
    chain: str
    deposit_enabled: bool | None = None
    withdraw_enabled: bool | None = None
    withdraw_fee: float | None = None
    min_withdraw: float | None = None


@dataclass
class CoinTransferStatus:
    exchange: str
    symbol: str
    status: str
    message: str
    deposit_enabled: bool | None = None
    withdraw_enabled: bool | None = None
    chains: list[CoinChainStatus] | None = None
    updated_at: datetime | None = None


@dataclass
class CoinIndexComponent:
    component: str
    weight: float | None = None
    raw_weight: str | None = None
    source: str | None = None
    price: float | None = None


@dataclass
class CoinIndexStatus:
    exchange: str
    symbol: str
    status: str
    message: str
    components: list[CoinIndexComponent] | None = None
    updated_at: datetime | None = None


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper().replace("-", "").replace("_", "").replace("/", "")
    if symbol.endswith("USDT"):
        symbol = symbol[:-4]
    if not symbol or not symbol.isalnum():
        raise ValueError("币种格式不正确")
    return symbol


def is_delisted_crypto_symbol(symbol: str) -> bool:
    try:
        return normalize_symbol(symbol) in DELISTED_CRYPTO_SYMBOLS
    except ValueError:
        return False


def delisted_crypto_symbol_reason(symbol: str) -> str | None:
    return f"{normalize_symbol(symbol)} 是下架币，已排除。" if is_delisted_crypto_symbol(symbol) else None


def normalize_exchange(value: str) -> str:
    exchange = value.strip().lower()
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError("交易所仅支持 bn、by、gt、okx、bg、htx、as、hl")
    return exchange


def normalize_market_type(value: str | None) -> str:
    market_type = (value or "futures").strip().lower()
    if market_type not in SUPPORTED_MARKET_TYPES:
        raise ValueError("市场类型仅支持 spot、futures")
    return market_type


def market_type_label(market_type: str) -> str:
    return "现货" if market_type == "spot" else "合约"


def label_for(
    symbol: str,
    left_exchange: str,
    right_exchange: str,
    left_market_type: str = "futures",
    right_market_type: str = "futures",
) -> str:
    return (
        f"{symbol}【{left_exchange}·{market_type_label(left_market_type)}】-"
        f"{symbol}【{right_exchange}·{market_type_label(right_market_type)}】"
    )


def validate_pair(
    symbol: str,
    left_exchange: str,
    right_exchange: str,
    left_market_type: str = "futures",
    right_market_type: str = "futures",
) -> tuple[str, str, str, str, str]:
    normalized_symbol = normalize_symbol(symbol)
    left = normalize_exchange(left_exchange)
    right = normalize_exchange(right_exchange)
    left_type = normalize_market_type(left_market_type)
    right_type = normalize_market_type(right_market_type)
    if left == right and left_type == right_type:
        raise ValueError("左右交易腿不能完全相同")
    if left_type == "spot" and right_type == "spot":
        raise ValueError("现货/现货交易对不纳入监控")
    if left_type == "futures" and right_type == "spot":
        left, right = right, left
        left_type, right_type = right_type, left_type
    return normalized_symbol, left, right, left_type, right_type


def normalize_exchange_list(values: list[str]) -> list[str]:
    enabled: list[str] = []
    for value in values:
        code = normalize_exchange(value)
        if code not in enabled:
            enabled.append(code)
    if not enabled:
        raise ValueError("至少保留一个可监控交易所")
    return [code for code in BOARD_EXCHANGES if code in enabled]


def exchange_options() -> list[dict[str, str]]:
    return [{"code": code, "name": EXCHANGE_NAMES.get(code, code)} for code in BOARD_EXCHANGES]


def board_exchange_codes() -> list[str]:
    raw = os.environ.get("CRYPTO_BOARD_EXCHANGES")
    if not raw:
        return BOARD_EXCHANGES
    codes = []
    for value in raw.split(","):
        try:
            codes.append(normalize_exchange(value))
        except ValueError:
            continue
    return normalize_exchange_list(codes) if codes else BOARD_EXCHANGES


def board_market_types(exchange: str) -> tuple[str, ...]:
    normalized = normalize_exchange(exchange)
    raw = os.environ.get(f"CRYPTO_{normalized.upper()}_BOARD_MARKET_TYPES")
    if raw:
        values: list[str] = []
        for item in raw.split(","):
            try:
                market_type = normalize_market_type(item)
            except ValueError:
                continue
            if market_type not in values:
                values.append(market_type)
        if values:
            return tuple(values)
    return BOARD_MARKET_TYPES.get(normalized, ("futures",))


def min_quote_volume_24h_usdt() -> float:
    raw = os.environ.get("CRYPTO_MIN_QUOTE_VOLUME_24H_USDT")
    if not raw:
        return DEFAULT_MIN_QUOTE_VOLUME_24H_USDT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MIN_QUOTE_VOLUME_24H_USDT


def setting_enabled_exchanges(setting: CryptoBoardSetting | None) -> list[str]:
    if not setting or not setting.enabled_exchanges_json:
        return board_exchange_codes()
    try:
        raw = json.loads(setting.enabled_exchanges_json)
    except json.JSONDecodeError:
        return board_exchange_codes()
    if not isinstance(raw, list):
        return board_exchange_codes()
    try:
        return normalize_exchange_list([str(value) for value in raw])
    except ValueError:
        return board_exchange_codes()


def crypto_board_setting(db: Session, create: bool = False) -> CryptoBoardSetting | None:
    setting = db.get(CryptoBoardSetting, 1)
    if setting or not create:
        return setting
    setting = CryptoBoardSetting(id=1, enabled_exchanges_json=json.dumps(board_exchange_codes()))
    db.add(setting)
    db.flush()
    return setting


def enabled_crypto_exchange_codes(db: Session) -> list[str]:
    return setting_enabled_exchanges(crypto_board_setting(db))


def normalize_mapping_payload(
    input_symbol: str,
    exchange: str,
    market_type: str | None,
    mapped_symbol: str,
) -> tuple[str, str, str, str]:
    return (
        normalize_symbol(input_symbol),
        normalize_exchange(exchange),
        normalize_market_type(market_type),
        normalize_symbol(mapped_symbol),
    )


def mapping_to_out(mapping: CryptoSymbolMapping) -> dict[str, Any]:
    return {
        "id": mapping.id,
        "inputSymbol": mapping.input_symbol,
        "exchange": mapping.exchange,
        "marketType": mapping.market_type,
        "mappedSymbol": mapping.mapped_symbol,
        "priceRatio": mapping.price_ratio or 1.0,
        "note": mapping.note,
        "updatedAt": mapping.updated_at,
    }


def symbol_mappings_to_out(db: Session) -> list[dict[str, Any]]:
    mappings = list(
        db.scalars(
            select(CryptoSymbolMapping).order_by(
                CryptoSymbolMapping.input_symbol,
                CryptoSymbolMapping.exchange,
                CryptoSymbolMapping.market_type,
            )
        )
    )
    return [mapping_to_out(mapping) for mapping in mappings]


def crypto_settings_to_out(db: Session) -> dict[str, Any]:
    return {
        "availableExchanges": exchange_options(),
        "enabledExchanges": enabled_crypto_exchange_codes(db),
        "symbolMappings": symbol_mappings_to_out(db),
    }


def update_crypto_settings(db: Session, enabled_exchanges: list[str]) -> dict[str, Any]:
    enabled = normalize_exchange_list(enabled_exchanges)
    setting = crypto_board_setting(db, create=True)
    assert setting is not None
    setting.enabled_exchanges_json = json.dumps(enabled)
    setting.updated_at = datetime.now(timezone.utc)
    db.flush()
    return crypto_settings_to_out(db)


def create_crypto_symbol_mapping(
    db: Session,
    input_symbol: str,
    exchange: str,
    market_type: str | None,
    mapped_symbol: str,
    price_ratio: float | None = None,
    note: str | None = None,
) -> CryptoSymbolMapping:
    normalized_input, normalized_exchange, normalized_market_type, normalized_mapped = normalize_mapping_payload(
        input_symbol,
        exchange,
        market_type,
        mapped_symbol,
    )
    existing = db.scalar(
        select(CryptoSymbolMapping)
        .where(
            CryptoSymbolMapping.input_symbol == normalized_input,
            CryptoSymbolMapping.exchange == normalized_exchange,
            CryptoSymbolMapping.market_type == normalized_market_type,
        )
        .limit(1)
    )
    normalized_price_ratio = normalize_mapping_price_ratio(price_ratio)
    if existing:
        existing.mapped_symbol = normalized_mapped
        existing.price_ratio = normalized_price_ratio
        existing.note = note
        existing.updated_at = datetime.now(timezone.utc)
        db.flush()
        return existing
    mapping = CryptoSymbolMapping(
        input_symbol=normalized_input,
        exchange=normalized_exchange,
        market_type=normalized_market_type,
        mapped_symbol=normalized_mapped,
        price_ratio=normalized_price_ratio,
        note=note,
    )
    db.add(mapping)
    db.flush()
    return mapping


def update_crypto_symbol_mapping(
    db: Session,
    mapping_id: int,
    input_symbol: str | None = None,
    exchange: str | None = None,
    market_type: str | None = None,
    mapped_symbol: str | None = None,
    price_ratio: float | None = None,
    note: str | None = None,
    update_price_ratio: bool = False,
    update_note: bool = False,
) -> CryptoSymbolMapping:
    mapping = db.get(CryptoSymbolMapping, mapping_id)
    if not mapping:
        raise ValueError("币名映射不存在")
    next_input = input_symbol if input_symbol is not None else mapping.input_symbol
    next_exchange = exchange if exchange is not None else mapping.exchange
    next_market_type = market_type if market_type is not None else mapping.market_type
    next_mapped = mapped_symbol if mapped_symbol is not None else mapping.mapped_symbol
    normalized_input, normalized_exchange, normalized_market_type, normalized_mapped = normalize_mapping_payload(
        next_input,
        next_exchange,
        next_market_type,
        next_mapped,
    )
    duplicate = db.scalar(
        select(CryptoSymbolMapping)
        .where(
            CryptoSymbolMapping.input_symbol == normalized_input,
            CryptoSymbolMapping.exchange == normalized_exchange,
            CryptoSymbolMapping.market_type == normalized_market_type,
            CryptoSymbolMapping.id != mapping.id,
        )
        .limit(1)
    )
    if duplicate:
        raise ValueError("同一币种、交易所和市场类型已存在映射")
    mapping.input_symbol = normalized_input
    mapping.exchange = normalized_exchange
    mapping.market_type = normalized_market_type
    mapping.mapped_symbol = normalized_mapped
    if update_price_ratio:
        mapping.price_ratio = normalize_mapping_price_ratio(price_ratio)
    if update_note:
        mapping.note = note
    mapping.updated_at = datetime.now(timezone.utc)
    db.flush()
    return mapping


def delete_crypto_symbol_mapping(db: Session, mapping_id: int) -> None:
    mapping = db.get(CryptoSymbolMapping, mapping_id)
    if not mapping:
        raise ValueError("币名映射不存在")
    db.delete(mapping)
    db.flush()


FACE_VALUE_MAPPING_PREFIXES: tuple[tuple[str, float], ...] = (
    ("1000000", 1_000_000.0),
    ("10000", 10_000.0),
    ("1000", 1_000.0),
    ("1M", 1_000_000.0),
    ("1K", 1_000.0),
)


def face_value_mapping_base(symbol: str) -> tuple[str, float] | None:
    normalized = normalize_symbol(symbol)
    for prefix, ratio in FACE_VALUE_MAPPING_PREFIXES:
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            base = normalized[len(prefix) :]
            if base:
                return base, ratio
    return None


def fetch_exchange_market_symbols(client: httpx.Client, exchange: str, market_type: str) -> set[str]:
    exchange = normalize_exchange(exchange)
    market_type = normalize_market_type(market_type)
    symbols: set[str] = set()
    if exchange == "bn" and market_type == "spot":
        payload = request_json(client, f"{spot_base_url('bn')}/api/v3/exchangeInfo")
        for row in payload.get("symbols", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict) or row.get("quoteAsset") != "USDT" or row.get("status") != "TRADING":
                continue
            symbols.add(normalize_symbol(str(row.get("baseAsset") or "")))
    elif exchange == "bn":
        payload = request_json(client, f"{base_url('bn')}/fapi/v1/exchangeInfo")
        for row in payload.get("symbols", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict) or row.get("quoteAsset") != "USDT" or row.get("status") != "TRADING":
                continue
            if row.get("contractType") and row.get("contractType") != "PERPETUAL":
                continue
            symbols.add(normalize_symbol(str(row.get("baseAsset") or "")))
    elif exchange == "by":
        category = "spot" if market_type == "spot" else "linear"
        payload = request_json(client, f"{spot_base_url('by')}/v5/market/instruments-info", {"category": category})
        rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("quoteCoin") or "").upper() != "USDT":
                continue
            status = str(row.get("status") or "").lower()
            if status and status not in {"trading", "tradingstatus"}:
                continue
            symbols.add(normalize_symbol(str(row.get("baseCoin") or "")))
    elif exchange == "gt" and market_type == "spot":
        rows = request_json(client, f"{spot_base_url('gt')}/api/v4/spot/currency_pairs")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("quote") or "").upper() != "USDT":
                continue
            trade_status = str(row.get("trade_status") or "tradable").lower()
            if trade_status != "tradable":
                continue
            symbols.add(normalize_symbol(str(row.get("base") or "")))
    elif exchange == "gt":
        rows = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/contracts")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("in_delisting") is True:
                continue
            name = str(row.get("name") or "")
            if not name.endswith("_USDT"):
                continue
            symbols.add(normalize_symbol(name[: -len("_USDT")]))
    elif exchange == "okx":
        inst_type = "SPOT" if market_type == "spot" else "SWAP"
        rows = okx_rows(request_json(client, f"{base_url('okx')}/api/v5/public/instruments", {"instType": inst_type}))
        for row in rows:
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or "live").lower()
            if state != "live":
                continue
            if market_type == "spot":
                if str(row.get("quoteCcy") or "").upper() != "USDT":
                    continue
                symbols.add(normalize_symbol(str(row.get("baseCcy") or "")))
            else:
                inst_id = str(row.get("instId") or "")
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                symbols.add(normalize_symbol(inst_id[: -len("-USDT-SWAP")]))
    elif exchange == "bg" and market_type == "spot":
        payload = request_json(client, f"{spot_base_url('bg')}/api/v2/spot/public/symbols")
        rows = bitget_data(payload)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("quoteCoin") or "").upper() != "USDT":
                continue
            status = str(row.get("status") or "online").lower()
            if status not in {"online", "tradable", "trading"}:
                continue
            symbols.add(normalize_symbol(str(row.get("baseCoin") or "")))
    elif exchange == "bg":
        rows = bitget_data(request_json(client, f"{base_url('bg')}/api/v2/mix/market/contracts", {"productType": "USDT-FUTURES"}))
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("symbolStatus") or row.get("status") or "normal").lower()
            if status not in {"normal", "online", "tradable", "trading"}:
                continue
            symbol = str(row.get("baseCoin") or "")
            if not symbol:
                symbol = usdt_perp_base_symbol(row.get("symbol")) or ""
            symbols.add(normalize_symbol(symbol))
    elif exchange == "htx" and market_type == "spot":
        payload = request_json(client, f"{spot_base_url('htx')}/v1/common/symbols")
        rows = payload.get("data") if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("quote-currency") or "").upper() != "USDT":
                continue
            state = str(row.get("state") or "online").lower()
            if state != "online":
                continue
            symbols.add(normalize_symbol(str(row.get("base-currency") or "")))
    elif exchange == "htx":
        payload = request_json(client, f"{base_url('htx')}/linear-swap-api/v1/swap_contract_info", {"contract_code": ""})
        rows = payload.get("data") if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("contract_status") or "1") != "1":
                continue
            contract_code = str(row.get("contract_code") or "")
            if not contract_code.endswith("-USDT"):
                continue
            symbols.add(normalize_symbol(contract_code[: -len("-USDT")]))
    elif exchange == "as" and market_type == "futures":
        payload = request_json(client, f"{base_url('as')}/fapi/v1/exchangeInfo")
        for row in payload.get("symbols", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict) or row.get("quoteAsset") != "USDT" or row.get("status") != "TRADING":
                continue
            symbols.add(normalize_symbol(str(row.get("baseAsset") or "")))
    elif exchange == "hl" and market_type == "futures":
        response = client.post(f"{base_url('hl')}/info", json={"type": "meta"})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("universe") if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            symbols.add(normalize_symbol(str(row.get("name") or "")))
    else:
        raise ValueError(f"{EXCHANGE_NAMES.get(exchange, exchange)} 暂未配置全市场映射扫描。")
    return {symbol for symbol in symbols if symbol}


def symbol_mapping_scan_market_types(exchange: str) -> tuple[str, ...]:
    if exchange in {"as", "hl"}:
        return ("futures",)
    return ("spot", "futures")


INDEX_ALIAS_SCAN_EXCHANGES = {"bn", "by", "okx", "bg"}
INDEX_ALIAS_MIN_COMPONENTS = 2
INDEX_ALIAS_MIN_SOURCES = 2
INDEX_ALIAS_MIN_SHARE = 0.8
INDEX_ALIAS_MAX_SYMBOLS = 300


def infer_index_component_alias_mapping(
    exchange: str,
    contract_symbol: str,
    status: CoinIndexStatus,
    known_symbols: set[str],
) -> dict[str, Any] | None:
    """Infer a strong execution alias from a perpetual index basket.

    A differently named contract is accepted only when at least two independent
    index sources overwhelmingly reference the same, already-listed USDT base
    asset. Price similarity alone is deliberately not used.
    """
    normalized_exchange = normalize_exchange(exchange)
    normalized_contract = normalize_symbol(contract_symbol)
    if status.status != "ok" or not status.components:
        return None

    grouped: dict[str, dict[str, Any]] = {}
    total_weight = 0.0
    weighted_component_count = 0
    component_count = 0
    for component in status.components:
        try:
            base_symbol = normalize_symbol(component.component)
        except ValueError:
            continue
        if not base_symbol:
            continue
        component_count += 1
        bucket = grouped.setdefault(base_symbol, {"count": 0, "weight": 0.0, "sources": set()})
        bucket["count"] += 1
        if component.source:
            bucket["sources"].add(str(component.source).strip().lower())
        if component.weight is not None and component.weight > 0:
            weight = float(component.weight)
            bucket["weight"] += weight
            total_weight += weight
            weighted_component_count += 1

    if component_count < INDEX_ALIAS_MIN_COMPONENTS or not grouped:
        return None
    dominant_symbol, dominant = max(
        grouped.items(),
        key=lambda item: (item[1]["weight"], item[1]["count"], item[0]),
    )
    if dominant_symbol == normalized_contract or dominant_symbol not in known_symbols:
        return None
    if dominant["count"] < INDEX_ALIAS_MIN_COMPONENTS or len(dominant["sources"]) < INDEX_ALIAS_MIN_SOURCES:
        return None

    if weighted_component_count == component_count and total_weight > 0:
        evidence_share = float(dominant["weight"]) / total_weight
    else:
        evidence_share = float(dominant["count"]) / float(component_count)
    if evidence_share < INDEX_ALIAS_MIN_SHARE:
        return None

    sources = sorted(str(source) for source in dominant["sources"] if source)
    return {
        "inputSymbol": dominant_symbol,
        "exchange": normalized_exchange,
        "marketType": "futures",
        "mappedSymbol": normalized_contract,
        "priceRatio": 1.0,
        "note": (
            "auto-scan: index-component alias; "
            f"{dominant['count']}/{component_count} components, "
            f"share {evidence_share:.1%}, sources {','.join(sources)}"
        ),
        "evidenceType": "index_components",
        "evidenceShare": evidence_share,
        "evidenceSources": sources,
    }


def scan_index_component_alias_mappings(
    client: httpx.Client,
    market_symbols: dict[tuple[str, str], set[str]],
    selected_exchanges: list[str],
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
    all_symbols = set().union(*market_symbols.values()) if market_symbols else set()
    futures_locations: dict[str, set[str]] = {}
    for (exchange, market_type), symbols in market_symbols.items():
        if market_type != "futures":
            continue
        for symbol in symbols:
            futures_locations.setdefault(symbol, set()).add(exchange)

    targets = [
        (exchange, symbol)
        for exchange in selected_exchanges
        if exchange in INDEX_ALIAS_SCAN_EXCHANGES
        for symbol in sorted(market_symbols.get((exchange, "futures"), set()))
        if len(futures_locations.get(symbol, set())) == 1 and face_value_mapping_base(symbol) is None
    ][:INDEX_ALIAS_MAX_SYMBOLS]

    def fetch_target(exchange: str, symbol: str) -> CoinIndexStatus:
        if exchange == "bn":
            return fetch_binance_index_components(client, symbol)
        if exchange == "by":
            return fetch_bybit_index_components(client, symbol)
        if exchange == "okx":
            return fetch_okx_index_components(client, symbol)
        return fetch_bitget_index_components(client, symbol)

    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if not targets:
        return candidates, 0, errors
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
        future_map = {
            executor.submit(fetch_target, exchange, symbol): (exchange, symbol)
            for exchange, symbol in targets
        }
        for future in as_completed(future_map):
            exchange, symbol = future_map[future]
            try:
                status = future.result()
            except Exception as exc:
                errors.append({"exchange": exchange, "symbol": symbol, "message": str(exc)})
                continue
            candidate = infer_index_component_alias_mapping(exchange, symbol, status, all_symbols)
            if candidate is not None:
                candidates.append(candidate)
    candidates.sort(key=lambda item: (item["inputSymbol"], item["exchange"], item["mappedSymbol"]))
    return candidates, len(targets), errors


def scan_crypto_symbol_mappings(db: Session, exchanges: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    selected = [normalize_exchange(item) for item in (exchanges or ["bn", "by", "gt", "okx", "bg", "htx", "as", "hl"])]
    selected = [item for item in selected if item in SUPPORTED_EXCHANGES]
    started_at = datetime.now(timezone.utc)
    market_symbols: dict[tuple[str, str], set[str]] = {}
    errors: list[dict[str, str]] = []
    alias_candidates: list[dict[str, Any]] = []
    alias_scanned_count = 0
    alias_scan_errors: list[dict[str, str]] = []
    with httpx.Client(timeout=20.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for exchange in selected:
            for market_type in symbol_mapping_scan_market_types(exchange):
                try:
                    market_symbols[(exchange, market_type)] = fetch_exchange_market_symbols(client, exchange, market_type)
                except Exception as exc:
                    market_symbols[(exchange, market_type)] = set()
                    errors.append({"exchange": exchange, "marketType": market_type, "message": str(exc)})
        alias_candidates, alias_scanned_count, alias_scan_errors = scan_index_component_alias_mappings(
            client,
            market_symbols,
            selected,
        )

    all_symbols: set[str] = set()
    for values in market_symbols.values():
        all_symbols.update(values)

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for (exchange, market_type), symbols in sorted(market_symbols.items()):
        for mapped_symbol in sorted(symbols):
            parsed = face_value_mapping_base(mapped_symbol)
            if not parsed:
                continue
            input_symbol, price_ratio = parsed
            if input_symbol not in all_symbols:
                skipped.append(
                    {
                        "inputSymbol": input_symbol,
                        "exchange": exchange,
                        "marketType": market_type,
                        "mappedSymbol": mapped_symbol,
                        "priceRatio": price_ratio,
                        "reason": "全市场未找到原始 base 币，跳过避免误合并。",
                    }
                )
                continue
            candidates.append(
                {
                    "inputSymbol": input_symbol,
                    "exchange": exchange,
                    "marketType": market_type,
                    "mappedSymbol": mapped_symbol,
                    "priceRatio": price_ratio,
                    "note": "auto-scan: face-value symbol mapping",
                }
            )

    candidates.extend(alias_candidates)
    deduplicated_candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["inputSymbol"], candidate["exchange"], candidate["marketType"])
        previous = deduplicated_candidates.get(key)
        if previous is None or candidate.get("evidenceType") == "index_components":
            deduplicated_candidates[key] = candidate
    candidates = list(deduplicated_candidates.values())

    created = 0
    updated = 0
    unchanged = 0
    applied: list[dict[str, Any]] = []
    for candidate in candidates:
        existing = db.scalar(
            select(CryptoSymbolMapping)
            .where(
                CryptoSymbolMapping.input_symbol == candidate["inputSymbol"],
                CryptoSymbolMapping.exchange == candidate["exchange"],
                CryptoSymbolMapping.market_type == candidate["marketType"],
            )
            .limit(1)
        )
        if existing:
            if (
                existing.mapped_symbol == candidate["mappedSymbol"]
                and float(existing.price_ratio or 1.0) == float(candidate["priceRatio"])
            ):
                unchanged += 1
                action = "unchanged"
            else:
                updated += 1
                action = "updated"
            if not dry_run:
                existing.mapped_symbol = candidate["mappedSymbol"]
                existing.price_ratio = candidate["priceRatio"]
                existing.note = candidate["note"]
                existing.updated_at = datetime.now(timezone.utc)
        else:
            created += 1
            action = "created"
            if not dry_run:
                db.add(
                    CryptoSymbolMapping(
                        input_symbol=candidate["inputSymbol"],
                        exchange=candidate["exchange"],
                        market_type=candidate["marketType"],
                        mapped_symbol=candidate["mappedSymbol"],
                        price_ratio=candidate["priceRatio"],
                        note=candidate["note"],
                    )
                )
        applied.append({**candidate, "action": action})
    db.flush()
    return {
        "status": "ok",
        "startedAt": started_at,
        "finishedAt": datetime.now(timezone.utc),
        "dryRun": dry_run,
        "exchangeCount": len(selected),
        "marketCount": len(market_symbols),
        "symbolCount": len(all_symbols),
        "candidateCount": len(candidates),
        "faceValueCandidateCount": len(candidates) - len(alias_candidates),
        "aliasCandidateCount": len(alias_candidates),
        "aliasScannedCount": alias_scanned_count,
        "aliasScanErrorCount": len(alias_scan_errors),
        "createdCount": created,
        "updatedCount": updated,
        "unchangedCount": unchanged,
        "skippedCount": len(skipped),
        "errorCount": len(errors),
        "items": applied,
        "skipped": skipped[:200],
        "errors": errors,
        "aliasScanErrors": alias_scan_errors[:100],
    }


def symbol_mapping_lookup(db: Session, symbol: str) -> dict[tuple[str, str], str]:
    normalized = normalize_symbol(symbol)
    mappings = db.scalars(select(CryptoSymbolMapping).where(CryptoSymbolMapping.input_symbol == normalized))
    return {(mapping.exchange, mapping.market_type): mapping.mapped_symbol for mapping in mappings}


def symbol_mapping_specs(db: Session, symbol: str) -> dict[tuple[str, str], dict[str, Any]]:
    normalized = normalize_symbol(symbol)
    mappings = db.scalars(select(CryptoSymbolMapping).where(CryptoSymbolMapping.input_symbol == normalized))
    return {
        (mapping.exchange, mapping.market_type): {
            "mappedSymbol": mapping.mapped_symbol,
            "priceRatio": mapping.price_ratio or 1.0,
        }
        for mapping in mappings
    }


def inverse_symbol_mapping_lookup(db: Session, market_type: str | None = None) -> dict[tuple[str, str, str], str]:
    query = select(CryptoSymbolMapping)
    if market_type is not None:
        query = query.where(CryptoSymbolMapping.market_type == normalize_market_type(market_type))
    mappings = db.scalars(query)
    return {
        (mapping.exchange, mapping.market_type, normalize_symbol(mapping.mapped_symbol)): normalize_symbol(mapping.input_symbol)
        for mapping in mappings
    }


def canonical_exchange_asset_symbol(
    raw_symbol: str,
    exchange: str,
    market_type: str,
    inverse_mappings: dict[tuple[str, str, str], str] | None = None,
) -> str:
    normalized_symbol = normalize_symbol(raw_symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market_type = normalize_market_type(market_type)
    mapped = (inverse_mappings or {}).get((normalized_exchange, normalized_market_type, normalized_symbol))
    if mapped:
        return normalize_symbol(mapped)
    try:
        canonical = resolve_asset_canonical_symbol(normalized_symbol)
        if canonical.usable_for_scan and canonical.canonical_symbol:
            return normalize_symbol(canonical.canonical_symbol)
    except Exception:
        pass
    return normalized_symbol


def normalize_mapping_price_ratio(value: float | None) -> float:
    if value is None:
        return 1.0
    parsed = parse_float(value)
    if parsed is None or parsed <= 0:
        raise ValueError("价格汇率必须大于 0")
    return parsed


def mapped_symbol_and_ratio_for(db: Session, symbol: str, exchange: str, market_type: str) -> tuple[str, float]:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market_type = normalize_market_type(market_type)
    try:
        resolved = resolve_asset_exchange_symbol(
            normalized_symbol,
            normalized_exchange,
            normalized_market_type,
            db=db,
        )
        if resolved.source != "unresolved":
            return resolved.request_symbol, resolved.price_ratio or 1.0
    except Exception:
        pass
    mapping = db.scalar(
        select(CryptoSymbolMapping)
        .where(
            CryptoSymbolMapping.input_symbol == normalized_symbol,
            CryptoSymbolMapping.exchange == normalized_exchange,
            CryptoSymbolMapping.market_type == normalized_market_type,
        )
        .limit(1)
    )
    if not mapping:
        return normalized_symbol, 1.0
    return mapping.mapped_symbol, mapping.price_ratio or 1.0


def mapped_symbol_for(db: Session, symbol: str, exchange: str, market_type: str) -> str:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market_type = normalize_market_type(market_type)
    mapping = db.scalar(
        select(CryptoSymbolMapping)
        .where(
            CryptoSymbolMapping.input_symbol == normalized_symbol,
            CryptoSymbolMapping.exchange == normalized_exchange,
            CryptoSymbolMapping.market_type == normalized_market_type,
        )
        .limit(1)
    )
    return mapping.mapped_symbol if mapping else normalized_symbol


def asset_alias_scan_block_reason(symbol: str) -> str | None:
    try:
        resolved = resolve_asset_canonical_symbol(symbol)
    except Exception:
        return None
    if resolved.status not in {"manual_review", "conflict"}:
        return None
    if not resolved.candidates:
        return None
    candidate_names = " / ".join(
        sorted(
            {
                f"{candidate.canonical_symbol}({candidate.canonical_asset_id})"
                for candidate in resolved.candidates
                if candidate.canonical_asset_id
            }
        )
    )
    suffix = f" 候选：{candidate_names}" if candidate_names else ""
    return f"{resolved.normalized_symbol} 需要人工确认，已禁止自动扫描和推送。{suffix}"


def base_url(exchange: str) -> str:
    env_key = {
        "bn": "CRYPTO_BINANCE_BASE_URL",
        "by": "CRYPTO_BYBIT_BASE_URL",
        "gt": "CRYPTO_GATE_BASE_URL",
        "okx": "CRYPTO_OKX_BASE_URL",
        "bg": "CRYPTO_BITGET_BASE_URL",
        "htx": "CRYPTO_HTX_BASE_URL",
        "as": "CRYPTO_ASTER_BASE_URL",
        "hl": "CRYPTO_HYPERLIQUID_BASE_URL",
    }[exchange]
    return os.environ.get(env_key, DEFAULT_BASE_URLS[exchange]).rstrip("/")


def spot_base_url(exchange: str) -> str:
    env_key = {
        "bn": "CRYPTO_BINANCE_SPOT_BASE_URL",
        "by": "CRYPTO_BYBIT_SPOT_BASE_URL",
        "gt": "CRYPTO_GATE_SPOT_BASE_URL",
        "okx": "CRYPTO_OKX_SPOT_BASE_URL",
        "bg": "CRYPTO_BITGET_SPOT_BASE_URL",
        "htx": "CRYPTO_HTX_SPOT_BASE_URL",
        "as": "CRYPTO_ASTER_SPOT_BASE_URL",
        "hl": "CRYPTO_HYPERLIQUID_SPOT_BASE_URL",
    }[exchange]
    return os.environ.get(env_key, DEFAULT_SPOT_BASE_URLS[exchange]).rstrip("/")


def http_timeout() -> float:
    return float(os.environ.get("CRYPTO_API_TIMEOUT_SECONDS", "6"))


def crypto_api_proxy() -> str | None:
    """Return the explicit proxy or the active macOS system web proxy.

    LaunchAgents do not inherit the shell's HTTP(S)_PROXY variables.  Python's
    urllib does, however, read the macOS SystemConfiguration proxy settings, so
    use that as a fallback for exchange APIs that are unreachable directly.
    """
    configured = str(os.environ.get("CRYPTO_API_PROXY") or "").strip()
    if configured:
        return configured
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    for scheme in ("https", "http"):
        proxy = str(proxies.get(scheme) or "").strip()
        if proxy:
            return proxy
    return None


def http_client(timeout: float | None = None) -> httpx.Client:
    proxy = crypto_api_proxy()
    return httpx.Client(
        timeout=timeout if timeout is not None else http_timeout(),
        proxy=proxy,
        follow_redirects=True,
        headers={"User-Agent": "stock-review-mac/0.1"},
    )


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timestamp_ms(value: Any) -> datetime | None:
    raw = parse_float(value)
    if raw is None or raw <= 0:
        return None
    return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)


def parse_timestamp_s(value: Any) -> datetime | None:
    raw = parse_float(value)
    if raw is None or raw <= 0:
        return None
    return datetime.fromtimestamp(raw, tz=timezone.utc)


def parse_datetime_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and not value.strip().replace(".", "", 1).isdigit():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raw = parse_float(value)
    if raw is None or raw <= 0:
        return None
    if raw > 1_000_000_000_000:
        return parse_timestamp_ms(raw)
    return parse_timestamp_s(raw)


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def computed_premium(mark_price: float | None, index_price: float | None) -> float | None:
    if mark_price is None or index_price is None or index_price == 0:
        return None
    return (mark_price - index_price) / index_price


def negative_cap(value: float | None) -> float | None:
    if value is None:
        return None
    return -abs(value)


def positive_cap(value: float | None) -> float | None:
    if value is None:
        return None
    return abs(value)


def first_float(*values: Any) -> float | None:
    for value in values:
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    return None


def quote_reference_price_usdt(
    best_bid: float | None,
    best_ask: float | None,
    mark_price: float | None = None,
    index_price: float | None = None,
) -> float | None:
    bid = parse_float(best_bid)
    ask = parse_float(best_ask)
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return min(bid, ask)
    return first_float(bid if bid and bid > 0 else None, ask if ask and ask > 0 else None, mark_price, index_price)


def borrowable_value_usdt(amount: float | None, price: float | None) -> float | None:
    parsed_amount = parse_float(amount)
    parsed_price = parse_float(price)
    if parsed_amount is None or parsed_amount <= 0 or parsed_price is None or parsed_price <= 0:
        return None
    return parsed_amount * parsed_price


def borrow_value_message_suffix(value: float | None) -> str:
    if value is None:
        return f"；缺少价格，未标记为可借（需 > {MIN_BORROWABLE_VALUE_USDT:g}U）"
    return f"；折算约 {value:g}U，未超过 {MIN_BORROWABLE_VALUE_USDT:g}U"


def funding_range_text(min_rate: float | None, max_rate: float | None) -> str:
    if min_rate is None and max_rate is None:
        return "未披露上下限"
    left = f"{min_rate * 100:.3f}%" if min_rate is not None else "--"
    right = f"+{max_rate * 100:.3f}%" if max_rate is not None and max_rate >= 0 else (f"{max_rate * 100:.3f}%" if max_rate is not None else "--")
    return f"{left} / {right}"


def api_rate_bucket_for_url(url: str, private: bool = False) -> str:
    if "binance.com" in url:
        return "bn:private" if private or "/sapi/" in url else "bn:public"
    if "bybit.com" in url:
        return "by:private" if private or "/v5/spot-margin-trade/" in url or "/v5/order/" in url else "by:public"
    if "gateio.ws" in url:
        # Gate uses /api/v4/ for both public market data and signed account
        # endpoints. The caller already tells us whether auth headers are
        # present; treating every v4 book as private unnecessarily slows Astro.
        return "gt:private" if private else "gt:public"
    if "okx.com" in url:
        return "okx:private" if private or "/api/v5/account/" in url else "okx:public"
    if "bitget.com" in url:
        return "bg:private" if private or "/api/v3/account/" in url or "/api/v2/margin/" in url else "bg:public"
    if "hbdm.com" in url or "huobi.pro" in url:
        return "htx:public"
    if "asterdex.com" in url:
        return "as:public"
    if "hyperliquid.xyz" in url:
        return "hl:public"
    return "default"


@contextmanager
def api_request_priority(level: str):
    """Mark exchange API calls made in this thread as Astro-critical.

    Background jobs may still run, but a waiting Astro quote/depth request
    always receives the next shared rate-limit slot.  Nested scopes restore
    the previous level so SDK callbacks and worker pools remain isolated.
    """

    global _api_priority_astro_active_count
    previous = getattr(_api_priority_local, "level", "normal")
    requested = "astro" if str(level).lower() == "astro" else "normal"
    _api_priority_local.level = requested
    registered = requested == "astro"
    if registered:
        with _api_rate_limit_condition:
            _api_priority_astro_active_count += 1
            _api_rate_limit_condition.notify_all()
    try:
        yield
    finally:
        _api_priority_local.level = previous
        if registered:
            with _api_rate_limit_condition:
                _api_priority_astro_active_count = max(0, _api_priority_astro_active_count - 1)
                _api_rate_limit_condition.notify_all()


@contextmanager
def api_rate_limit_lane(lane: str):
    """Select an independent rate-limit lane for one workload.

    User-facing funding reads and exchange-announcement market checks must not
    be starved by the continuously active Astro scanner.  A lane keeps its
    next-slot and backoff state separate while still using the same
    exchange-specific pacing intervals.
    """

    previous = getattr(_api_priority_local, "lane", "shared")
    normalized = str(lane or "shared").strip().lower() or "shared"
    _api_priority_local.lane = normalized
    try:
        yield
    finally:
        _api_priority_local.lane = previous


def api_rate_limit_state_key(bucket: str) -> tuple[str, str]:
    lane = getattr(_api_priority_local, "lane", "shared")
    if lane == "shared":
        return bucket, lane
    return f"{lane}:{bucket}", lane


def api_priority_status() -> dict[str, Any]:
    with _api_rate_limit_condition:
        return {
            "policy": "astro_first",
            "astroRequestsTakeNextRateLimitSlot": True,
            "astroCriticalSectionActive": _api_priority_astro_active_count > 0,
            "astroCriticalSectionCount": _api_priority_astro_active_count,
            "astroWaitingCount": sum(_api_priority_waiters.values()),
            "astroWaitingByBucket": {
                bucket: count
                for bucket, count in sorted(_api_priority_waiters.items())
                if count > 0
            },
            "isolatedLanes": [
                FUNDING_FORMATION_API_LANE,
                EXCHANGE_ANNOUNCEMENT_API_LANE,
                "astro_evidence",
            ],
            "grants": dict(sorted(_api_priority_grants.items())),
        }


@contextmanager
def api_request_deadline(timeout_seconds: float | None = None, *, deadline_monotonic: float | None = None):
    """A cooperative budget spanning limiter waits, HTTP calls and retries.

    Workers must enter this scope themselves: threading.local is intentionally
    not inherited by executor children. Nested scopes may shorten a budget.
    """
    if deadline_monotonic is None:
        if timeout_seconds is None or not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("positive_api_request_timeout_required")
        deadline_monotonic = time.monotonic() + float(timeout_seconds)
    if not math.isfinite(float(deadline_monotonic)):
        raise ValueError("invalid_api_request_deadline")
    previous = getattr(_api_priority_local, "deadline_monotonic", None)
    selected = float(deadline_monotonic)
    if previous is not None:
        selected = min(selected, previous)
    _api_priority_local.deadline_monotonic = selected
    try:
        api_request_remaining_seconds()
        yield selected
    finally:
        _api_priority_local.deadline_monotonic = previous


def api_request_remaining_seconds() -> float | None:
    deadline = getattr(_api_priority_local, "deadline_monotonic", None)
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("exchange_api_deadline_exceeded")
    return remaining


def api_request_timeout_kwargs(client: httpx.Client) -> dict[str, Any]:
    """Bound each HTTP phase without expanding the caller's smaller timeout."""
    remaining = api_request_remaining_seconds()
    if remaining is None:
        return {}
    phase_budget = max(0.001, remaining / 4)
    original = getattr(client, "timeout", None)
    phases = {}
    for phase in ("connect", "read", "write", "pool"):
        prior = getattr(original, phase, None)
        phases[phase] = min(float(prior), phase_budget) if prior is not None else phase_budget
    return {"timeout": httpx.Timeout(**phases)}


def _api_request_pause(seconds: float) -> None:
    remaining = api_request_remaining_seconds()
    if remaining is not None and seconds >= remaining:
        raise TimeoutError("exchange_api_deadline_exceeded_during_backoff")
    time.sleep(seconds)
    api_request_remaining_seconds()


def wait_api_rate_limit(bucket: str) -> None:
    interval = API_RATE_LIMIT_INTERVALS_SECONDS.get(bucket, API_RATE_LIMIT_INTERVALS_SECONDS["default"])
    state_key, lane = api_rate_limit_state_key(bucket)
    urgent = lane == "shared" and getattr(_api_priority_local, "level", "normal") == "astro"
    registered = False
    with _api_rate_limit_condition:
        if urgent:
            _api_priority_waiters[state_key] += 1
            registered = True
            _api_rate_limit_condition.notify_all()
        try:
            while True:
                now = time.monotonic()
                remaining = api_request_remaining_seconds()
                urgent_waiting = _api_priority_waiters.get(state_key, 0) > 0
                if lane == "shared" and not urgent and (
                    urgent_waiting or _api_priority_astro_active_count > 0
                ):
                    wait_seconds = max(0.01, interval)
                    _api_rate_limit_condition.wait(timeout=min(wait_seconds, remaining) if remaining is not None else wait_seconds)
                    continue
                allowed_at = max(
                    _api_rate_limit_next_at.get(state_key, now),
                    _api_rate_limit_backoff_until.get(state_key, now),
                )
                if now >= allowed_at:
                    _api_rate_limit_next_at[state_key] = now + interval
                    grant_key = "astro" if urgent else lane if lane != "shared" else "background"
                    _api_priority_grants[grant_key] += 1
                    return
                wait_seconds = max(0.001, allowed_at - now)
                _api_rate_limit_condition.wait(timeout=min(wait_seconds, remaining) if remaining is not None else wait_seconds)
        finally:
            if registered:
                _api_priority_waiters[state_key] = max(0, _api_priority_waiters[state_key] - 1)
            _api_rate_limit_condition.notify_all()


def defer_api_rate_limit(bucket: str, delay_seconds: float) -> None:
    """Apply backoff inside the caller's rate-limit lane."""
    delay = max(0.0, min(float(delay_seconds), 60.0))
    state_key, _lane = api_rate_limit_state_key(bucket)
    with _api_rate_limit_condition:
        blocked_until = time.monotonic() + delay
        _api_rate_limit_backoff_until[state_key] = max(
            _api_rate_limit_backoff_until.get(state_key, 0.0),
            blocked_until,
        )
        _api_rate_limit_condition.notify_all()


def retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, min(float(retry_after), 30.0))
    except ValueError:
        return None


def rate_limited_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    bucket: str | None = None,
) -> httpx.Response:
    selected_bucket = bucket or api_rate_bucket_for_url(url, private=bool(headers))
    for attempt in range(2):
        wait_api_rate_limit(selected_bucket)
        response = client.get(url, params=params, headers=headers, **api_request_timeout_kwargs(client))
        api_request_remaining_seconds()
        if response.status_code not in {418, 429}:
            return response
        delay_seconds = retry_after_seconds(response) or (5.0 * (attempt + 1))
        defer_api_rate_limit(selected_bucket, delay_seconds)
        if attempt == 0:
            _api_request_pause(delay_seconds)
    return response


def rate_limited_post(
    client: httpx.Client,
    url: str,
    *,
    json_payload: dict[str, Any] | None = None,
    content: str | bytes | None = None,
    headers: dict[str, str] | None = None,
    bucket: str | None = None,
) -> httpx.Response:
    selected_bucket = bucket or api_rate_bucket_for_url(url, private=bool(headers))
    for attempt in range(2):
        wait_api_rate_limit(selected_bucket)
        response = client.post(url, json=json_payload, content=content, headers=headers, **api_request_timeout_kwargs(client))
        api_request_remaining_seconds()
        if response.status_code not in {418, 429}:
            return response
        delay_seconds = retry_after_seconds(response) or (5.0 * (attempt + 1))
        defer_api_rate_limit(selected_bucket, delay_seconds)
        if attempt == 0:
            _api_request_pause(delay_seconds)
    return response


def request_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Any:
    response = rate_limited_get(client, url, params=params)
    response.raise_for_status()
    return response.json()


def request_post_json(client: httpx.Client, url: str, payload: dict[str, Any]) -> Any:
    response = rate_limited_post(client, url, json_payload=payload)
    response.raise_for_status()
    return response.json()


def first_env(*keys: str) -> str | None:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def private_credentials_path() -> str | None:
    configured = os.environ.get("CRYPTO_PRIVATE_CREDENTIALS_FILE")
    if configured:
        return configured
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "crypto-private-credentials.json")
    return None


def private_credentials_from_file(exchange: str) -> tuple[str | None, str | None, str | None]:
    path = private_credentials_path()
    if not path or not os.path.exists(path):
        return None, None, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, None, None
    normalized = normalize_exchange(exchange)
    section = payload.get(normalized)
    if not isinstance(section, dict):
        return None, None, None
    api_key = section.get("apiKey") or section.get("api_key")
    api_secret = section.get("apiSecret") or section.get("api_secret")
    passphrase = section.get("passphrase")
    return (
        str(api_key).strip() if api_key else None,
        str(api_secret).strip() if api_secret else None,
        str(passphrase).strip() if passphrase else None,
    )


def exchange_private_credentials(exchange: str) -> tuple[str | None, str | None, str | None]:
    normalized = normalize_exchange(exchange)
    names = {
        "bn": ("BINANCE", "BN"),
        "by": ("BYBIT", "BY"),
        "gt": ("GATE", "GT"),
        "okx": ("OKX",),
        "bg": ("BITGET", "BG"),
        "htx": ("HTX", "HUOBI"),
        "as": ("ASTER", "AS"),
        "hl": ("HYPERLIQUID", "HL"),
    }[normalized]
    api_key = first_env(*(f"CRYPTO_{name}_API_KEY" for name in names))
    api_secret = first_env(*(f"CRYPTO_{name}_API_SECRET" for name in names))
    passphrase = first_env(*(f"CRYPTO_{name}_API_PASSPHRASE" for name in names))
    if api_key and api_secret:
        return api_key, api_secret, passphrase
    file_key, file_secret, file_passphrase = private_credentials_from_file(normalized)
    return file_key, file_secret, file_passphrase


def has_private_credentials(exchange: str) -> bool:
    api_key, api_secret, _ = exchange_private_credentials(exchange)
    return bool(api_key and api_secret)


def binance_server_time_ms(client: httpx.Client) -> int:
    global _binance_server_time_offset_cache
    now = time.time()
    with _binance_server_time_lock:
        if _binance_server_time_offset_cache and now - _binance_server_time_offset_cache[0] < BINANCE_SERVER_TIME_CACHE_SECONDS:
            return int(time.time() * 1000) + _binance_server_time_offset_cache[1]
    payload = request_json(client, f"{spot_base_url('bn')}/api/v3/time")
    local_time = int(time.time() * 1000)
    server_time = int(payload.get("serverTime") or int(now * 1000))
    offset = server_time - local_time
    with _binance_server_time_lock:
        _binance_server_time_offset_cache = (now, offset)
    return int(time.time() * 1000) + offset


def http_error_message(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
    except ValueError:
        payload = exc.response.text[:200]
    if isinstance(payload, dict):
        code = payload.get("code")
        message = payload.get("msg") or payload.get("message")
        if code is not None or message:
            return f"HTTP {exc.response.status_code}: {code or ''} {message or ''}".strip()
    return f"HTTP {exc.response.status_code}: {payload}"


def signed_binance_margin_get(client: httpx.Client, path: str, params: dict[str, Any]) -> Any:
    api_key, api_secret, _ = exchange_private_credentials("bn")
    if not api_key or not api_secret:
        raise ValueError("未配置 Binance 杠杆 API Key/Secret")
    payload = {**params, "timestamp": binance_server_time_ms(client), "recvWindow": 60000}
    query = urlencode(payload)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    bucket = (
        "bn:margin_heavy"
        if path in {
            "/sapi/v1/margin/maxBorrowable",
            "/sapi/v1/margin/allAssets",
            "/sapi/v1/margin/available-inventory",
        }
        else "bn:private"
    )
    response = rate_limited_get(
        client,
        f"{spot_base_url('bn')}{path}",
        params={**payload, "signature": signature},
        headers={"X-MBX-APIKEY": api_key},
        bucket=bucket,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    return response.json()


def signed_bitget_margin_get(client: httpx.Client, path: str, params: dict[str, Any]) -> Any:
    api_key, api_secret, passphrase = exchange_private_credentials("bg")
    if not api_key or not api_secret or not passphrase:
        raise ValueError("未配置 Bitget 杠杆 API Key/Secret/Passphrase")
    timestamp = str(int(time.time() * 1000))
    query = urlencode(params)
    request_path = f"{path}?{query}" if query else path
    pre_hash = f"{timestamp}GET{request_path}"
    signature = base64.b64encode(hmac.new(api_secret.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
    response = rate_limited_get(
        client,
        f"{spot_base_url('bg')}{path}",
        params=params,
        headers={
            "ACCESS-KEY": api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        },
        bucket="bg:private",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    payload = response.json()
    if isinstance(payload, dict) and str(payload.get("code")) not in {"00000", "0"}:
        raise ValueError(f"Bitget {payload.get('code')}: {payload.get('msg') or payload.get('message') or '接口返回异常'}")
    return payload


def signed_bitget_uta_get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    api_key, api_secret, passphrase = exchange_private_credentials("bg")
    if not api_key or not api_secret or not passphrase:
        raise ValueError("未配置 Bitget UTA API Key/Secret/Passphrase")
    timestamp = str(int(time.time() * 1000))
    query = urlencode(params or {})
    request_path = f"{path}?{query}" if query else path
    pre_hash = f"{timestamp}GET{request_path}"
    signature = base64.b64encode(hmac.new(api_secret.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
    response = rate_limited_get(
        client,
        f"{spot_base_url('bg')}{path}",
        params=params or {},
        headers={
            "ACCESS-KEY": api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "locale": "zh-CN",
        },
        bucket="bg:private",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    payload = response.json()
    if isinstance(payload, dict) and str(payload.get("code")) not in {"00000", "0"}:
        raise ValueError(f"Bitget {payload.get('code')}: {payload.get('msg') or payload.get('message') or '接口返回异常'}")
    return payload


def signed_bitget_uta_post(client: httpx.Client, path: str, payload: dict[str, Any]) -> Any:
    api_key, api_secret, passphrase = exchange_private_credentials("bg")
    if not api_key or not api_secret or not passphrase:
        raise ValueError("未配置 Bitget UTA API Key/Secret/Passphrase")
    timestamp = str(int(time.time() * 1000))
    body = json.dumps(payload, separators=(",", ":"))
    pre_hash = f"{timestamp}POST{path}{body}"
    signature = base64.b64encode(hmac.new(api_secret.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
    response = rate_limited_post(
        client,
        f"{spot_base_url('bg')}{path}",
        content=body,
        headers={
            "ACCESS-KEY": api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "locale": "zh-CN",
        },
        bucket="bg:private",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    data = response.json()
    if isinstance(data, dict) and str(data.get("code")) not in {"00000", "0"}:
        raise ValueError(f"Bitget {data.get('code')}: {data.get('msg') or data.get('message') or '接口返回异常'}")
    return data


def signed_bybit_get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    api_key, api_secret, _ = exchange_private_credentials("by")
    if not api_key or not api_secret:
        raise ValueError("未配置 Bybit API Key/Secret")
    recv_window = "5000"
    query = urlencode(params or {})
    timestamp = str(int(time.time() * 1000))
    payload = f"{timestamp}{api_key}{recv_window}{query}"
    signature = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    response = rate_limited_get(
        client,
        f"{spot_base_url('by')}{path}",
        params=params or {},
        headers={
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
        },
        bucket="by:private",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    data = response.json()
    if isinstance(data, dict) and int(data.get("retCode") or 0) != 0:
        raise ValueError(f"Bybit {data.get('retCode')}: {data.get('retMsg') or '接口返回异常'}")
    return data


def signed_gate_get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    api_key, api_secret, _ = exchange_private_credentials("gt")
    if not api_key or not api_secret:
        raise ValueError("未配置 Gate API Key/Secret")
    query = urlencode(params or {})
    request_path = f"/api/v4{path}"
    timestamp = str(int(time.time()))
    body_hash = hashlib.sha512(b"").hexdigest()
    sign_text = "\n".join(["GET", request_path, query, body_hash, timestamp])
    signature = hmac.new(api_secret.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha512).hexdigest()
    response = rate_limited_get(
        client,
        f"{spot_base_url('gt')}{request_path}",
        params=params or {},
        headers={
            "KEY": api_key,
            "Timestamp": timestamp,
            "SIGN": signature,
            "Accept": "application/json",
        },
        bucket="gt:private",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    return response.json()


def okx_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def signed_okx_get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    api_key, api_secret, passphrase = exchange_private_credentials("okx")
    if not api_key or not api_secret or not passphrase:
        raise ValueError("未配置 OKX API Key/Secret/Passphrase")
    query = urlencode(params or {})
    request_path = f"{path}?{query}" if query else path
    timestamp = okx_timestamp()
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), f"{timestamp}GET{request_path}".encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    response = rate_limited_get(
        client,
        f"{spot_base_url('okx')}{request_path}",
        headers={
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
        },
        bucket="okx:private",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    data = response.json()
    if isinstance(data, dict) and str(data.get("code")) != "0":
        raise ValueError(f"OKX {data.get('code')}: {data.get('msg') or '接口返回异常'}")
    return data


def margin_short_check_to_out(check: MarginShortCheck | None) -> dict[str, Any] | None:
    if check is None:
        return None
    has_amount = check.borrowable_amount is not None and check.borrowable_amount > 0
    return {
        "exchange": check.exchange,
        "symbol": check.symbol,
        "status": check.status,
        "message": check.message,
        "canBorrow": check.can_borrow is True,
        "inventoryAvailable": check.inventory_available if check.inventory_available is not None else check.can_borrow is True,
        "borrowableAmount": check.borrowable_amount if has_amount else None,
        "borrowableValueUsdt": check.borrowable_value_usdt,
        "hourlyBorrowRate": check.hourly_borrow_rate,
        "dailyBorrowRate": check.daily_borrow_rate,
        "updatedAt": utc_datetime(check.updated_at),
    }


def unavailable_margin_short_check(exchange: str, symbol: str, status: str, message: str) -> MarginShortCheck:
    return MarginShortCheck(
        exchange=exchange,
        symbol=symbol,
        status=status,
        message=message,
        updated_at=datetime.now(timezone.utc),
    )


def margin_short_exception_check(exchange: str, symbol: str, exc: Exception) -> MarginShortCheck:
    normalized_symbol = normalize_symbol(symbol)
    message = str(exc)
    lower_message = message.lower()
    unsupported_markers = (
        "not supported",
        "doesn't exist",
        "does not exist",
        "instrument id",
        "不存在",
        "不支持",
    )
    if any(marker in lower_message for marker in unsupported_markers):
        return unavailable_margin_short_check(
            exchange,
            normalized_symbol,
            "not_supported",
            f"{EXCHANGE_NAMES.get(exchange, exchange)} 当前不支持借 {normalized_symbol}：{message}",
        )
    return unavailable_margin_short_check(exchange, normalized_symbol, "error", f"杠杆可借检查失败：{message}")


def fetch_binance_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    try:
        max_borrowable = signed_binance_margin_get(client, "/sapi/v1/margin/maxBorrowable", {"asset": asset})
    except ValueError as exc:
        if "-3045" in str(exc) or "does not have enough asset" in str(exc):
            return unavailable_margin_short_check("bn", asset, "not_borrowable", f"Binance 当前 {asset} 借币流动性不足")
        raise
    amount = parse_float(max_borrowable.get("amount"))
    interest_payload = signed_binance_margin_get(
        client,
        "/sapi/v1/margin/interestRateHistory",
        {"asset": asset, "vipLevel": 0, "limit": 1},
    )
    interest_row = interest_payload[0] if isinstance(interest_payload, list) and interest_payload else {}
    daily_rate = parse_float(interest_row.get("dailyInterestRate"))
    hourly_rate = daily_rate / 24 if daily_rate is not None else None
    can_borrow = amount is not None and amount > 0
    return MarginShortCheck(
        exchange="bn",
        symbol=asset,
        status="ok" if can_borrow else "not_borrowable",
        message=f"Binance 可借 {amount:g} {asset}" if can_borrow and amount is not None else "Binance 当前不可借或额度为 0",
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_binance_margin_available_inventory(client: httpx.Client) -> dict[str, float]:
    global _binance_margin_inventory_cache
    now = datetime.now(timezone.utc)
    with _binance_margin_inventory_cache_lock:
        cached = _binance_margin_inventory_cache
        if cached and (now - cached[0]).total_seconds() < BINANCE_MARGIN_INVENTORY_CACHE_SECONDS:
            return dict(cached[1])

    payload = signed_binance_margin_get(client, "/sapi/v1/margin/available-inventory", {"type": "MARGIN"})
    raw_assets = payload.get("assets") if isinstance(payload, dict) else None
    inventory: dict[str, float] = {}
    if isinstance(raw_assets, dict):
        for raw_asset, raw_amount in raw_assets.items():
            amount = parse_float(raw_amount)
            if amount is None:
                continue
            try:
                inventory[normalize_symbol(str(raw_asset))] = max(0.0, amount)
            except ValueError:
                continue
    with _binance_margin_inventory_cache_lock:
        _binance_margin_inventory_cache = (now, inventory)
    return dict(inventory)


def fetch_binance_spot_usdt_symbols(client: httpx.Client) -> set[str]:
    global _binance_spot_market_cache
    now = datetime.now(timezone.utc)
    with _binance_spot_market_cache_lock:
        cached = _binance_spot_market_cache
        if cached and (now - cached[0]).total_seconds() < BINANCE_SPOT_MARKET_CACHE_SECONDS:
            return set(cached[1])
    symbols = fetch_exchange_market_symbols(client, "bn", "spot")
    with _binance_spot_market_cache_lock:
        _binance_spot_market_cache = (now, symbols)
    return set(symbols)


def fetch_binance_next_hourly_interest_rates(client: httpx.Client, assets: list[str] | tuple[str, ...] | set[str]) -> dict[str, float]:
    normalized_assets = sorted({normalize_symbol(asset) for asset in assets if str(asset).strip()})
    if not normalized_assets:
        return {}
    now = datetime.now(timezone.utc)
    rates: dict[str, float] = {}
    missing: list[str] = []
    with _binance_margin_rate_cache_lock:
        for asset in normalized_assets:
            cached = _binance_margin_rate_cache.get(asset)
            if cached and (now - cached[0]).total_seconds() < BINANCE_MARGIN_RATE_CACHE_SECONDS:
                if cached[1] is not None:
                    rates[asset] = cached[1]
            else:
                missing.append(asset)
    if missing:
        payload = signed_binance_margin_get(
            client,
            "/sapi/v1/margin/next-hourly-interest-rate",
            {"assets": ",".join(missing), "isIsolated": "false"},
        )
        returned: dict[str, float] = {}
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, dict) or not row.get("asset"):
                continue
            hourly_rate = parse_float(row.get("nextHourlyInterestRate"))
            if hourly_rate is None:
                continue
            returned[normalize_symbol(str(row["asset"]))] = hourly_rate
        with _binance_margin_rate_cache_lock:
            for asset in missing:
                hourly_rate = returned.get(asset)
                _binance_margin_rate_cache[asset] = (now, hourly_rate)
                if hourly_rate is not None:
                    rates[asset] = hourly_rate
    return rates


def fetch_binance_fs_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    if asset not in fetch_binance_spot_usdt_symbols(client):
        return MarginShortCheck(
            exchange="bn",
            symbol=asset,
            status="not_supported",
            message=f"Binance 当前没有 {asset}/USDT 可交易现货",
            can_borrow=False,
            inventory_available=False,
            updated_at=datetime.now(timezone.utc),
        )
    hourly_rate = fetch_binance_next_hourly_interest_rates(client, [asset]).get(asset)
    amount = fetch_binance_margin_available_inventory(client).get(asset, 0.0)
    if amount <= 0:
        return MarginShortCheck(
            exchange="bn",
            symbol=asset,
            status="not_borrowable",
            message=f"Binance 当前 {asset} 可用借币库存为 0",
            can_borrow=False,
            inventory_available=False,
            hourly_borrow_rate=hourly_rate,
            daily_borrow_rate=hourly_rate * 24 if hourly_rate is not None else None,
            updated_at=datetime.now(timezone.utc),
        )
    if hourly_rate is None:
        try:
            interest_payload = signed_binance_margin_get(
                client,
                "/sapi/v1/margin/interestRateHistory",
                {"asset": asset, "vipLevel": 0, "limit": 1},
            )
            interest_row = interest_payload[0] if isinstance(interest_payload, list) and interest_payload else {}
            daily_rate = parse_float(interest_row.get("dailyInterestRate"))
            hourly_rate = daily_rate / 24 if daily_rate is not None else None
            if hourly_rate is not None:
                with _binance_margin_rate_cache_lock:
                    _binance_margin_rate_cache[asset] = (datetime.now(timezone.utc), hourly_rate)
        except Exception:
            hourly_rate = None
    return MarginShortCheck(
        exchange="bn",
        symbol=asset,
        status="ok",
        message=f"Binance 可用借币库存 {amount:g} {asset}，下单前按账户抵押物复核最大可借",
        can_borrow=True,
        inventory_available=True,
        borrowable_amount=amount,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=hourly_rate * 24 if hourly_rate is not None else None,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_binance_margin_asset_symbols(client: httpx.Client) -> set[str]:
    payload = signed_binance_margin_get(client, "/sapi/v1/margin/allAssets", {})
    if not isinstance(payload, list):
        return set()
    symbols: set[str] = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        is_borrowable = str(row.get("isBorrowable", "")).lower()
        if is_borrowable not in {"true", "1", "yes"}:
            continue
        asset = row.get("assetName") or row.get("asset")
        if not asset:
            continue
        try:
            symbols.add(normalize_symbol(str(asset)))
        except ValueError:
            continue
    return symbols


def fetch_bybit_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    spot_symbol = f"{asset}USDT"
    currency_payload = signed_bybit_get(client, "/v5/spot-margin-trade/currency-data", {"currency": asset})
    rows = ((currency_payload.get("result") or {}).get("list") or []) if isinstance(currency_payload, dict) else []
    if not rows:
        return unavailable_margin_short_check("by", asset, "not_supported", f"Bybit 当前不支持借 {asset}：未返回借币币种信息")
    currency_row = rows[0] if rows else {}
    borrowable_flag = bool(currency_row.get("flexibleManualBorrowable") or currency_row.get("fixedManualBorrowable"))
    max_payload = signed_bybit_get(client, "/v5/spot-margin-trade/max-borrowable", {"currency": asset})
    max_loan = first_float((max_payload.get("result") or {}).get("maxLoan") if isinstance(max_payload, dict) else None)
    trade_qty = None
    try:
        trade_payload = signed_bybit_get(client, "/v5/order/spot-borrow-check", {"category": "spot", "symbol": spot_symbol, "side": "Sell"})
        trade_qty = first_float((trade_payload.get("result") or {}).get("maxTradeQty") if isinstance(trade_payload, dict) else None)
    except Exception:
        trade_qty = None
    hourly_rate = fetch_bybit_public_hourly_borrow_rate(client, asset)
    if hourly_rate is None:
        rate_payload = signed_bybit_get(client, "/v5/spot-margin-trade/interest-rate-history", {"currency": asset})
        rate_rows = ((rate_payload.get("result") or {}).get("list") or []) if isinstance(rate_payload, dict) else []
        rate_row = rate_rows[0] if rate_rows else {}
        hourly_rate = first_float(rate_row.get("hourlyBorrowRate"), rate_row.get("hourlyInterestRate"), rate_row.get("borrowRate"))
    daily_rate = hourly_rate * 24 if hourly_rate is not None else None
    amount = trade_qty if trade_qty and trade_qty > 0 else max_loan
    can_borrow = borrowable_flag and amount is not None and amount > 0
    return MarginShortCheck(
        exchange="by",
        symbol=asset,
        status="ok" if can_borrow else "not_borrowable",
        message=f"Bybit 可卖/可借 {amount:g} {asset}" if can_borrow and amount is not None else f"Bybit 当前 {asset} 不可借或交易页可卖为 0",
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_bybit_public_hourly_borrow_rate(client: httpx.Client, asset: str) -> float | None:
    payload = request_json(client, f"{spot_base_url('by')}/v5/spot-margin-trade/data")
    vip_rows = ((payload.get("result") or {}).get("vipCoinList") or []) if isinstance(payload, dict) else []
    for vip_row in vip_rows:
        if not isinstance(vip_row, dict):
            continue
        for row in vip_row.get("list") or []:
            if not isinstance(row, dict) or str(row.get("currency") or "").upper() != asset:
                continue
            return first_float(row.get("hourlyBorrowRate"))
    return None


def fetch_gate_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    hourly_rate = None
    rate_error: str | None = None
    for path in ("/margin/uni/estimate_rate", "/unified/estimate_rate"):
        try:
            rate_payload = signed_gate_get(client, path, {"currencies": asset})
            hourly_rate = first_float(rate_payload.get(asset) if isinstance(rate_payload, dict) else None)
            if hourly_rate is not None:
                break
        except Exception as exc:
            rate_error = str(exc)
    daily_rate = hourly_rate * 24 if hourly_rate is not None else None

    amount = None
    amount_error: str | None = None
    try:
        amount_payload = signed_gate_get(client, "/margin/cross/borrowable", {"currency": asset})
        amount = first_float(amount_payload.get("amount") if isinstance(amount_payload, dict) else None)
    except Exception as exc:
        amount_error = str(exc)
        try:
            uni_payload = signed_gate_get(client, "/margin/uni/borrowable", {"currency_pair": f"{asset}_USDT", "currency": asset})
            amount = first_float(uni_payload.get("borrowable") if isinstance(uni_payload, dict) else None)
            amount_error = None
        except Exception as uni_exc:
            amount_error = str(uni_exc)

    can_borrow = amount is not None and amount > 0
    if can_borrow and amount is not None:
        message = f"Gate 可借 {amount:g} {asset}"
    elif hourly_rate is not None:
        message = f"Gate 已查到 {asset} 借币利率，但账户级可借为 0 或暂不可借"
    elif amount_error:
        message = f"Gate 借币额度/利率暂不可用：{amount_error}"
    elif rate_error:
        message = f"Gate 借币利率暂不可用：{rate_error}"
    else:
        message = f"Gate 当前 {asset} 不可借"
    return MarginShortCheck(
        exchange="gt",
        symbol=asset,
        status="ok" if can_borrow else "not_borrowable",
        message=message,
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_okx_public_daily_borrow_rate(client: httpx.Client, asset: str) -> float | None:
    payload = request_json(client, f"{spot_base_url('okx')}/api/v5/public/interest-rate-loan-quota", {"ccy": asset})
    rows = payload.get("data") if isinstance(payload, dict) else []
    buckets = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    for bucket_name in ("vip", "regular", "basic", "custom", "configCcyList"):
        bucket = buckets.get(bucket_name) if isinstance(buckets, dict) else None
        if not isinstance(bucket, list):
            continue
        for row in bucket:
            if not isinstance(row, dict) or str(row.get("ccy") or "").upper() != asset:
                continue
            rate = first_float(row.get("rate"))
            if rate is not None:
                return rate
    return None


def fetch_okx_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    inst_id = f"{asset}-USDT"
    size_payload = signed_okx_get(client, "/api/v5/account/max-size", {"instId": inst_id, "tdMode": "isolated"})
    rows = size_payload.get("data") if isinstance(size_payload, dict) else []
    usdt_row = next((row for row in rows if isinstance(row, dict) and row.get("ccy") == "USDT"), None)
    amount = first_float(usdt_row.get("maxSell") if usdt_row else None)
    if amount is None or amount <= 0:
        loan_payload = signed_okx_get(client, "/api/v5/account/max-loan", {"instId": inst_id, "mgnMode": "isolated", "mgnCcy": "USDT"})
        loan_rows = loan_payload.get("data") if isinstance(loan_payload, dict) else []
        loan_row = next((row for row in loan_rows if isinstance(row, dict) and row.get("ccy") == asset and row.get("side") == "sell"), None)
        amount = first_float(loan_row.get("maxLoan") if loan_row else None)
    hourly_rate = None
    try:
        rate_payload = signed_okx_get(client, "/api/v5/account/interest-rate", {"ccy": asset})
        rate_rows = rate_payload.get("data") if isinstance(rate_payload, dict) else []
        rate_row = rate_rows[0] if isinstance(rate_rows, list) and rate_rows else {}
        hourly_rate = first_float(rate_row.get("interestRate") if isinstance(rate_row, dict) else None)
    except Exception:
        hourly_rate = None
    daily_rate = hourly_rate * 24 if hourly_rate is not None else fetch_okx_public_daily_borrow_rate(client, asset)
    if hourly_rate is None and daily_rate is not None:
        hourly_rate = daily_rate / 24
    can_borrow = amount is not None and amount > 0
    return MarginShortCheck(
        exchange="okx",
        symbol=asset,
        status="ok" if can_borrow else "not_borrowable",
        message=f"OKX 杠杆交易页可卖 {amount:g} {asset}" if can_borrow and amount is not None else f"OKX 当前 {asset} 杠杆卖出可卖为 0",
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_bitget_uta_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    pair_symbol = f"{asset}USDT"
    loan_payload = request_json(client, f"{spot_base_url('bg')}/api/v3/market/margin-loans", {"coin": asset})
    loan_data = loan_payload.get("data") if isinstance(loan_payload, dict) else {}
    daily_rate = first_float(loan_data.get("dailyInterest") if isinstance(loan_data, dict) else None)
    platform_quota = first_float(
        loan_data.get("platformRemaingQuota") if isinstance(loan_data, dict) else None,
        loan_data.get("platformRemainingQuota") if isinstance(loan_data, dict) else None,
    )
    hourly_rate = daily_rate / 24 if daily_rate is not None else None
    try:
        payload = signed_bitget_uta_post(
            client,
            "/api/v3/account/max-open-available",
            {"category": "MARGIN", "symbol": pair_symbol, "orderType": "market", "side": "sell"},
        )
    except ValueError as exc:
        if "25112" in str(exc) or "未开启抵押" in str(exc):
            return MarginShortCheck(
                exchange="bg",
                symbol=asset,
                status="not_borrowable",
                message=f"Bitget 当前未开启 {asset} 抵押，暂时无 B",
                can_borrow=False,
                hourly_borrow_rate=hourly_rate,
                daily_borrow_rate=daily_rate,
                updated_at=datetime.now(timezone.utc),
            )
        raise
    data = payload.get("data") if isinstance(payload, dict) else {}
    available = first_float(data.get("available") if isinstance(data, dict) else None)
    max_open = first_float(
        data.get("maxOpen") if isinstance(data, dict) else None,
        data.get("maxSellAvailable") if isinstance(data, dict) else None,
    )
    # Bitget's UTA maxOpen includes the account's existing available base coin.
    # FS needs the incremental amount that can actually be borrowed, otherwise
    # an account balance (or a published interest rate) is mistaken for live
    # borrow inventory.
    amount = None
    if max_open is not None:
        amount = max(0.0, max_open - max(0.0, available or 0.0))
        # maxOpen is an account/order sizing result and can temporarily exceed
        # the exchange-wide inventory.  The public margin-loan quota is the
        # stricter source for whether fresh B is actually available.
        if platform_quota is not None:
            amount = min(amount, max(0.0, platform_quota))
    supported = daily_rate is not None or max_open is not None
    can_borrow = amount is not None and amount > 0
    if can_borrow and amount is not None:
        message = (
            f"Bitget UTA 新增可借 {amount:g} {asset}"
            f"（杠杆可卖上限 {max_open:g}，平台剩余 {(platform_quota or 0):g}，自有可用 {(available or 0):g}）"
        )
    elif supported:
        message = (
            f"Bitget UTA 支持 {asset} 保证金借币，但当前新增可借为 0"
            f"（杠杆可卖上限 {(max_open or 0):g}，自有可用 {(available or 0):g}）"
        )
    else:
        message = f"Bitget UTA 当前 {asset} 不可借或未返回借币利率"
    return MarginShortCheck(
        exchange="bg",
        symbol=asset,
        status="ok" if can_borrow else "not_borrowable",
        message=message,
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_bitget_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    try:
        rate_payload = signed_bitget_margin_get(
            client,
            "/api/v2/margin/crossed/interest-rate-and-limit",
            {"coin": asset},
        )
    except ValueError as exc:
        if "40085" in str(exc) or "Unified Account mode" in str(exc):
            return fetch_bitget_uta_margin_short_check(client, asset)
        if "does not support cross" in str(exc) or "50001" in str(exc):
            return fetch_bitget_isolated_margin_short_check(client, asset)
        if "cannot be borrowed" in str(exc) or "does not support isolated" in str(exc) or "50037" in str(exc) or "50002" in str(exc):
            return unavailable_margin_short_check("bg", asset, "not_borrowable", f"Bitget 当前不支持借 {asset}")
        raise
    rows = rate_payload.get("data") if isinstance(rate_payload, dict) else None
    row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
    try:
        amount_payload = signed_bitget_margin_get(
            client,
            "/api/v2/margin/crossed/account/max-borrowable-amount",
            {"coin": asset},
        )
    except ValueError as exc:
        if "40085" in str(exc) or "Unified Account mode" in str(exc):
            return fetch_bitget_uta_margin_short_check(client, asset)
        if "does not support cross" in str(exc) or "50001" in str(exc):
            return fetch_bitget_isolated_margin_short_check(client, asset)
        if "cannot be borrowed" in str(exc) or "does not support isolated" in str(exc) or "50037" in str(exc) or "50002" in str(exc):
            return unavailable_margin_short_check("bg", asset, "not_borrowable", f"Bitget 当前不支持借 {asset}")
        raise
    amount_data = amount_payload.get("data") if isinstance(amount_payload, dict) else {}
    amount = first_float(amount_data.get("maxBorrowableAmount") if isinstance(amount_data, dict) else None)
    daily_rate = first_float(row.get("dailyInterestRate"), row.get("dailyRate"), row.get("interestRate"))
    hourly_rate = daily_rate / 24 if daily_rate is not None else None
    borrowable_flag = str(row.get("borrowable", "")).lower()
    supported = borrowable_flag not in {"false", "0", "no"} and daily_rate is not None
    can_borrow = borrowable_flag not in {"false", "0", "no"} and amount is not None and amount > 0
    if can_borrow and amount is not None:
        message = f"Bitget 账户可借 {amount:g} {asset}"
    elif supported:
        message = f"Bitget 支持借 {asset}，但当前账户新增可借额度为 0"
    else:
        message = "Bitget 账户当前不可借或未返回借币利率"
    return MarginShortCheck(
        exchange="bg",
        symbol=asset,
        status="ok" if can_borrow else "not_borrowable",
        message=message,
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_bitget_isolated_margin_short_check(client: httpx.Client, symbol: str) -> MarginShortCheck:
    asset = normalize_symbol(symbol)
    pair_symbol = f"{asset}USDT"
    try:
        rate_payload = signed_bitget_margin_get(
            client,
            "/api/v2/margin/isolated/interest-rate-and-limit",
            {"symbol": pair_symbol},
        )
    except ValueError as exc:
        if "cannot be borrowed" in str(exc) or "does not support isolated" in str(exc) or "50037" in str(exc) or "50002" in str(exc):
            return unavailable_margin_short_check("bg", asset, "not_borrowable", f"Bitget 当前不支持借 {asset}")
        raise
    rows = rate_payload.get("data") if isinstance(rate_payload, dict) else None
    row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
    if not row:
        return unavailable_margin_short_check("bg", asset, "not_borrowable", f"Bitget isolated 未返回 {pair_symbol} 借币信息")
    base_coin = str(row.get("baseCoin") or asset).upper()
    base_borrowable = str(row.get("baseBorrowable", "")).lower() in {"true", "1", "yes"}
    daily_rate = first_float(row.get("baseDailyInterestRate"), row.get("dailyInterestRate"), row.get("baseRate"))
    platform_limit = first_float(row.get("baseMaxBorrowableAmount"))
    try:
        amount_payload = signed_bitget_margin_get(
            client,
            "/api/v2/margin/isolated/account/max-borrowable-amount",
            {"symbol": pair_symbol},
        )
    except ValueError as exc:
        if "cannot be borrowed" in str(exc) or "does not support isolated" in str(exc) or "50037" in str(exc) or "50002" in str(exc):
            return unavailable_margin_short_check("bg", asset, "not_borrowable", f"Bitget 当前不支持借 {asset}")
        raise
    amount_data = amount_payload.get("data") if isinstance(amount_payload, dict) else {}
    amount = first_float(amount_data.get("baseCoinMaxBorrowAmount") if isinstance(amount_data, dict) else None)
    hourly_rate = daily_rate / 24 if daily_rate is not None else None
    can_borrow = base_borrowable and base_coin == asset and amount is not None and amount > 0
    status = "ok" if can_borrow else "not_borrowable"
    amount_text = f"{amount:g}" if amount is not None else "--"
    platform_text = f"{platform_limit:g}" if platform_limit is not None else "--"
    message = (
        f"Bitget isolated 可借 {amount_text} {asset}，平台上限 {platform_text} {asset}"
        if can_borrow
        else (
            f"Bitget isolated 支持 {asset} 借币，但当前账户新增可借额度为 0，平台上限 {platform_text} {asset}"
            if base_borrowable and base_coin == asset
            else f"Bitget isolated 当前不支持借 {asset}"
        )
    )
    return MarginShortCheck(
        exchange="bg",
        symbol=asset,
        status=status,
        message=message,
        can_borrow=can_borrow,
        borrowable_amount=amount if amount and amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_margin_short_check(exchange: str, symbol: str) -> MarginShortCheck:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    key = (normalized_exchange, normalized_symbol)
    with _margin_short_cache_lock:
        cached = _margin_short_cache.get(key)
        if cached and (now - cached[0]).total_seconds() < MARGIN_SHORT_CACHE_SECONDS:
            cached_check = cached[1]
            return MarginShortCheck(
                exchange=str(cached_check["exchange"]),
                symbol=str(cached_check["symbol"]),
                status=str(cached_check["status"]),
                message=str(cached_check["message"]),
                can_borrow=cached_check.get("canBorrow"),
                borrowable_amount=cached_check.get("borrowableAmount"),
                borrowable_value_usdt=cached_check.get("borrowableValueUsdt"),
                hourly_borrow_rate=cached_check.get("hourlyBorrowRate"),
                daily_borrow_rate=cached_check.get("dailyBorrowRate"),
                updated_at=cached_check.get("updatedAt"),
            )

    api_key, api_secret, _ = exchange_private_credentials(normalized_exchange)
    if normalized_exchange not in MARGIN_PRIVATE_API_EXCHANGES:
        check = unavailable_margin_short_check(normalized_exchange, normalized_symbol, "not_supported", "该交易所杠杆可借私有 API 适配待接入")
    elif not api_key or not api_secret:
        check = unavailable_margin_short_check(normalized_exchange, normalized_symbol, "needs_authorization", "未配置该交易所杠杆 API Key/Secret")
    else:
        try:
            with http_client() as client:
                if normalized_exchange == "bn":
                    check = fetch_binance_margin_short_check(client, normalized_symbol)
                elif normalized_exchange == "by":
                    check = fetch_bybit_margin_short_check(client, normalized_symbol)
                elif normalized_exchange == "gt":
                    check = fetch_gate_margin_short_check(client, normalized_symbol)
                elif normalized_exchange == "okx":
                    check = fetch_okx_margin_short_check(client, normalized_symbol)
                elif normalized_exchange == "bg":
                    check = fetch_bitget_margin_short_check(client, normalized_symbol)
                else:
                    check = unavailable_margin_short_check(normalized_exchange, normalized_symbol, "not_supported", "该交易所杠杆可借私有 API 适配待接入")
        except Exception as exc:
            check = margin_short_exception_check(normalized_exchange, normalized_symbol, exc)

    with _margin_short_cache_lock:
        _margin_short_cache[key] = (now, margin_short_check_to_out(check) or {})
    return check


def exchange_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "enabled", "enable", "available", "normal", "allowed"}:
        return True
    if text in {"false", "0", "no", "n", "disabled", "disable", "unavailable", "suspend", "suspended", "prohibited"}:
        return False
    return None


def inverted_exchange_bool(value: Any) -> bool | None:
    parsed = exchange_bool(value)
    return None if parsed is None else not parsed


def chain_status_to_out(chain: CoinChainStatus) -> dict[str, Any]:
    return {
        "chain": chain.chain,
        "depositEnabled": chain.deposit_enabled,
        "withdrawEnabled": chain.withdraw_enabled,
        "withdrawFee": chain.withdraw_fee,
        "minWithdraw": chain.min_withdraw,
    }


def index_component_to_out(component: CoinIndexComponent) -> dict[str, Any]:
    return {
        "component": component.component,
        "weight": component.weight,
        "rawWeight": component.raw_weight,
        "source": component.source,
        "price": component.price,
    }


def transfer_status_to_out(status: CoinTransferStatus) -> dict[str, Any]:
    chains = status.chains or []
    return {
        "exchange": status.exchange,
        "symbol": status.symbol,
        "status": status.status,
        "message": status.message,
        "depositEnabled": status.deposit_enabled,
        "withdrawEnabled": status.withdraw_enabled,
        "chains": [chain_status_to_out(chain) for chain in chains],
        "updatedAt": utc_datetime(status.updated_at),
    }


def index_status_to_out(status: CoinIndexStatus) -> dict[str, Any]:
    components = status.components or []
    return {
        "exchange": status.exchange,
        "symbol": status.symbol,
        "status": status.status,
        "message": status.message,
        "components": [index_component_to_out(component) for component in components],
        "updatedAt": utc_datetime(status.updated_at),
    }


def unavailable_transfer_status(exchange: str, symbol: str, status: str, message: str) -> CoinTransferStatus:
    return CoinTransferStatus(exchange=exchange, symbol=normalize_symbol(symbol), status=status, message=message, updated_at=datetime.now(timezone.utc))


def unavailable_index_status(exchange: str, symbol: str, status: str, message: str) -> CoinIndexStatus:
    return CoinIndexStatus(exchange=exchange, symbol=normalize_symbol(symbol), status=status, message=message, components=[], updated_at=datetime.now(timezone.utc))


def aggregate_transfer_flags(chains: list[CoinChainStatus]) -> tuple[bool | None, bool | None]:
    if not chains:
        return None, None
    deposit_values = [chain.deposit_enabled for chain in chains if chain.deposit_enabled is not None]
    withdraw_values = [chain.withdraw_enabled for chain in chains if chain.withdraw_enabled is not None]
    deposit_enabled = any(deposit_values) if deposit_values else None
    withdraw_enabled = any(withdraw_values) if withdraw_values else None
    return deposit_enabled, withdraw_enabled


def normalize_weight(value: Any) -> float | None:
    parsed = first_float(value)
    if parsed is None:
        return None
    return parsed / 100 if abs(parsed) > 1 else parsed


def fetch_binance_transfer_config(client: httpx.Client, *, cache_seconds: float = COIN_TRANSFER_CACHE_SECONDS) -> dict[str, dict[str, Any]]:
    global _binance_transfer_config_cache
    now = datetime.now(timezone.utc)
    with _binance_transfer_config_cache_lock:
        cached = _binance_transfer_config_cache
        if cached and (now - cached[0]).total_seconds() < cache_seconds:
            return cached[1]
        payload = signed_binance_margin_get(client, "/sapi/v1/capital/config/getall", {})
        rows = payload if isinstance(payload, list) else []
        config = {
            str(item.get("coin") or "").upper(): item
            for item in rows
            if isinstance(item, dict) and str(item.get("coin") or "").strip()
        }
        _binance_transfer_config_cache = (now, config)
        return config


def fetch_binance_transfer_status(client: httpx.Client, symbol: str, *, cache_seconds: float = COIN_TRANSFER_CACHE_SECONDS) -> CoinTransferStatus:
    asset = normalize_symbol(symbol)
    config = fetch_binance_transfer_config(client, cache_seconds=cache_seconds)
    row = config.get(asset)
    if not row:
        return unavailable_transfer_status("bn", asset, "not_supported", f"Binance 未返回 {asset} 充提配置")
    chains: list[CoinChainStatus] = []
    for network in row.get("networkList") or []:
        if not isinstance(network, dict):
            continue
        chain_name = str(network.get("network") or network.get("name") or "").strip()
        if not chain_name:
            continue
        chains.append(
            CoinChainStatus(
                chain=chain_name,
                deposit_enabled=exchange_bool(network.get("depositEnable")),
                withdraw_enabled=exchange_bool(network.get("withdrawEnable")),
                withdraw_fee=first_float(network.get("withdrawFee")),
                min_withdraw=first_float(network.get("withdrawMin")),
            )
        )
    deposit_enabled, withdraw_enabled = aggregate_transfer_flags(chains)
    return CoinTransferStatus("bn", asset, "ok", "Binance 充提状态已更新。", deposit_enabled, withdraw_enabled, chains, datetime.now(timezone.utc))


def fetch_bybit_transfer_status(client: httpx.Client, symbol: str) -> CoinTransferStatus:
    asset = normalize_symbol(symbol)
    payload = signed_bybit_get(client, "/v5/asset/coin/query-info", {"coin": asset})
    rows = ((payload.get("result") or {}).get("rows") or []) if isinstance(payload, dict) else []
    row = rows[0] if rows else None
    if not isinstance(row, dict):
        return unavailable_transfer_status("by", asset, "not_supported", f"Bybit 未返回 {asset} 充提配置")
    chains: list[CoinChainStatus] = []
    for chain in row.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        chain_name = str(chain.get("chain") or chain.get("chainType") or "").strip()
        if not chain_name:
            continue
        chains.append(
            CoinChainStatus(
                chain=chain_name,
                deposit_enabled=exchange_bool(chain.get("chainDeposit") or chain.get("depositStatus")),
                withdraw_enabled=exchange_bool(chain.get("chainWithdraw") or chain.get("withdrawStatus")),
                withdraw_fee=first_float(chain.get("withdrawFee")),
                min_withdraw=first_float(chain.get("withdrawMin")),
            )
        )
    deposit_enabled, withdraw_enabled = aggregate_transfer_flags(chains)
    return CoinTransferStatus("by", asset, "ok", "Bybit 充提状态已更新。", deposit_enabled, withdraw_enabled, chains, datetime.now(timezone.utc))


def fetch_gate_transfer_status(client: httpx.Client, symbol: str) -> CoinTransferStatus:
    asset = normalize_symbol(symbol)
    response = rate_limited_get(client, f"{spot_base_url('gt')}/api/v4/wallet/currency_chains", params={"currency": asset}, bucket="gt:public")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(http_error_message(exc)) from exc
    payload = response.json()
    rows = payload if isinstance(payload, list) else []
    if not rows:
        return unavailable_transfer_status("gt", asset, "not_supported", f"Gate 未返回 {asset} 充提链")
    chains: list[CoinChainStatus] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chain_name = str(row.get("chain") or row.get("name") or "").strip()
        if not chain_name:
            continue
        deposit_enabled = exchange_bool(row.get("deposit_enabled"))
        if deposit_enabled is None:
            deposit_enabled = inverted_exchange_bool(row.get("is_deposit_disabled", row.get("deposit_disabled")))
        withdraw_enabled = exchange_bool(row.get("withdraw_enabled"))
        if withdraw_enabled is None:
            withdraw_enabled = inverted_exchange_bool(row.get("is_withdraw_disabled", row.get("withdraw_disabled")))
        if exchange_bool(row.get("is_disabled")) is True:
            deposit_enabled = withdraw_enabled = False
        chains.append(
            CoinChainStatus(
                chain=chain_name,
                deposit_enabled=deposit_enabled,
                withdraw_enabled=withdraw_enabled,
                withdraw_fee=first_float(row.get("withdraw_fee") or row.get("fee")),
                min_withdraw=first_float(row.get("withdraw_min") or row.get("min_withdraw_amount")),
            )
        )
    deposit_enabled, withdraw_enabled = aggregate_transfer_flags(chains)
    return CoinTransferStatus("gt", asset, "ok", "Gate 充提状态已更新。", deposit_enabled, withdraw_enabled, chains, datetime.now(timezone.utc))


def fetch_okx_transfer_status(client: httpx.Client, symbol: str) -> CoinTransferStatus:
    asset = normalize_symbol(symbol)
    payload = signed_okx_get(client, "/api/v5/asset/currencies", {"ccy": asset})
    rows = payload.get("data") if isinstance(payload, dict) else []
    if not isinstance(rows, list) or not rows:
        return unavailable_transfer_status("okx", asset, "not_supported", f"OKX 未返回 {asset} 充提配置")
    chains: list[CoinChainStatus] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chain_name = str(row.get("chain") or row.get("ccy") or "").strip()
        chains.append(
            CoinChainStatus(
                chain=chain_name or asset,
                deposit_enabled=exchange_bool(row.get("canDep")),
                withdraw_enabled=exchange_bool(row.get("canWd")),
                withdraw_fee=first_float(row.get("minFee")),
                min_withdraw=first_float(row.get("minWd")),
            )
        )
    deposit_enabled, withdraw_enabled = aggregate_transfer_flags(chains)
    return CoinTransferStatus("okx", asset, "ok", "OKX 充提状态已更新。", deposit_enabled, withdraw_enabled, chains, datetime.now(timezone.utc))


def fetch_bitget_transfer_status(client: httpx.Client, symbol: str) -> CoinTransferStatus:
    asset = normalize_symbol(symbol)
    payload = request_json(client, f"{spot_base_url('bg')}/api/v2/spot/public/coins", {"coin": asset})
    rows = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(rows, dict):
        rows = [rows]
    row = next((item for item in rows if isinstance(item, dict) and str(item.get("coin") or item.get("coinName") or "").upper() == asset), None)
    if not row:
        return unavailable_transfer_status("bg", asset, "not_supported", f"Bitget 未返回 {asset} 充提配置")
    raw_chains = row.get("chains") or row.get("chainList") or []
    chains: list[CoinChainStatus] = []
    for chain in raw_chains:
        if not isinstance(chain, dict):
            continue
        chain_name = str(chain.get("chain") or chain.get("chainName") or chain.get("network") or "").strip()
        if not chain_name:
            continue
        chains.append(
            CoinChainStatus(
                chain=chain_name,
                deposit_enabled=exchange_bool(chain.get("rechargeable") or chain.get("depositable") or chain.get("depositEnable")),
                withdraw_enabled=exchange_bool(chain.get("withdrawable") or chain.get("withdrawEnable")),
                withdraw_fee=first_float(chain.get("withdrawFee") or chain.get("withdrawalFee")),
                min_withdraw=first_float(chain.get("minWithdrawAmount") or chain.get("withdrawMin")),
            )
        )
    deposit_enabled, withdraw_enabled = aggregate_transfer_flags(chains)
    return CoinTransferStatus("bg", asset, "ok", "Bitget 充提状态已更新。", deposit_enabled, withdraw_enabled, chains, datetime.now(timezone.utc))


def fetch_coin_transfer_status(exchange: str, symbol: str, timeout: float | None = None, *, cache_seconds: float = COIN_TRANSFER_CACHE_SECONDS) -> CoinTransferStatus:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    key = (normalized_exchange, normalized_symbol)
    with _coin_status_cache_lock:
        cached = _coin_transfer_cache.get(key)
        if cached and (now - cached[0]).total_seconds() < cache_seconds:
            payload = cached[1]
            chains = [
                CoinChainStatus(
                    chain=str(item.get("chain") or ""),
                    deposit_enabled=item.get("depositEnabled"),
                    withdraw_enabled=item.get("withdrawEnabled"),
                    withdraw_fee=item.get("withdrawFee"),
                    min_withdraw=item.get("minWithdraw"),
                )
                for item in payload.get("chains", [])
                if isinstance(item, dict)
            ]
            return CoinTransferStatus(
                exchange=str(payload["exchange"]),
                symbol=str(payload["symbol"]),
                status=str(payload["status"]),
                message=str(payload["message"]),
                deposit_enabled=payload.get("depositEnabled"),
                withdraw_enabled=payload.get("withdrawEnabled"),
                chains=chains,
                updated_at=payload.get("updatedAt"),
            )
    try:
        with http_client(timeout=timeout) as client:
            if normalized_exchange == "bn":
                status = fetch_binance_transfer_status(client, normalized_symbol, cache_seconds=cache_seconds)
            elif normalized_exchange == "by":
                status = fetch_bybit_transfer_status(client, normalized_symbol)
            elif normalized_exchange == "gt":
                status = fetch_gate_transfer_status(client, normalized_symbol)
            elif normalized_exchange == "okx":
                status = fetch_okx_transfer_status(client, normalized_symbol)
            elif normalized_exchange == "bg":
                status = fetch_bitget_transfer_status(client, normalized_symbol)
            else:
                status = unavailable_transfer_status(normalized_exchange, normalized_symbol, "not_supported", "该交易所充提状态未适配")
    except Exception as exc:
        status = unavailable_transfer_status(normalized_exchange, normalized_symbol, "error", f"充提状态查询失败：{exc}")
    with _coin_status_cache_lock:
        _coin_transfer_cache[key] = (now, transfer_status_to_out(status))
    return status


def fetch_binance_index_components(client: httpx.Client, symbol: str) -> CoinIndexStatus:
    asset = normalize_symbol(symbol)
    candidates = (f"{asset}USDT", f"{asset}USD")
    for candidate in candidates:
        try:
            payload = request_json(client, f"{base_url('bn')}/fapi/v1/constituents", {"symbol": candidate})
        except Exception:
            continue
        raw_components = []
        if isinstance(payload, dict):
            raw_components = payload.get("constituents") or payload.get("components") or payload.get("data") or []
        if isinstance(raw_components, list) and raw_components:
            components: list[CoinIndexComponent] = []
            for item in raw_components:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("exchange") or item.get("source") or item.get("venue") or "").strip() or None
                name = str(item.get("symbol") or item.get("baseAsset") or item.get("asset") or "").strip()
                if not name:
                    continue
                raw_weight = item.get("weight") or item.get("weightInPercentage") or item.get("weightInQuantity")
                components.append(
                    CoinIndexComponent(
                        component=name,
                        weight=normalize_weight(raw_weight),
                        raw_weight=str(raw_weight) if raw_weight is not None else None,
                        source=source,
                        price=first_float(item.get("price"), item.get("lastPrice")),
                    )
                )
            if components:
                return CoinIndexStatus("bn", asset, "ok", "Binance 指数成分已更新。", components, datetime.now(timezone.utc))
    return unavailable_index_status("bn", asset, "not_supported", f"Binance 未返回 {asset} 指数成分")


def fetch_bybit_index_components(client: httpx.Client, symbol: str) -> CoinIndexStatus:
    asset = normalize_symbol(symbol)
    payload = request_json(client, f"{base_url('by')}/v5/market/index-price-components", {"indexName": f"{asset}USDT"})
    result = payload.get("result") if isinstance(payload, dict) else {}
    raw_components = result.get("components") if isinstance(result, dict) else []
    if not isinstance(raw_components, list) or not raw_components:
        return unavailable_index_status("by", asset, "not_supported", f"Bybit 未返回 {asset} 指数成分")
    components: list[CoinIndexComponent] = []
    for item in raw_components:
        if not isinstance(item, dict):
            continue
        exchange = str(item.get("exchange") or "").strip()
        spot_pair = str(item.get("spotPair") or item.get("symbol") or "").strip()
        name = spot_pair or exchange
        raw_weight = item.get("weight")
        if name:
            components.append(
                CoinIndexComponent(
                    component=name,
                    weight=normalize_weight(raw_weight),
                    raw_weight=str(raw_weight) if raw_weight is not None else None,
                    source=exchange or None,
                    price=first_float(item.get("price"), item.get("lastPrice")),
                )
            )
    if not components:
        return unavailable_index_status("by", asset, "not_supported", f"Bybit 未返回 {asset} 指数成分")
    return CoinIndexStatus("by", asset, "ok", "Bybit 指数成分已更新。", components, datetime.now(timezone.utc))


def fetch_okx_index_components(client: httpx.Client, symbol: str) -> CoinIndexStatus:
    asset = normalize_symbol(symbol)
    payload = request_json(client, f"{spot_base_url('okx')}/api/v5/market/index-components", {"index": f"{asset}-USDT"})
    rows = payload.get("data") if isinstance(payload, dict) else []
    row = rows[0] if isinstance(rows, list) and rows else {}
    raw_components = row.get("components") or row.get("constituents") if isinstance(row, dict) else []
    if not isinstance(raw_components, list) or not raw_components:
        return unavailable_index_status("okx", asset, "not_supported", f"OKX 未返回 {asset} 指数成分")
    components: list[CoinIndexComponent] = []
    for item in raw_components:
        if not isinstance(item, dict):
            continue
        source = str(item.get("exchange") or item.get("source") or item.get("venue") or "").strip() or None
        name = str(item.get("symbol") or item.get("instId") or item.get("ccy") or "").strip()
        raw_weight = item.get("weight") or item.get("wgt") or item.get("weightRatio")
        if name:
            components.append(
                CoinIndexComponent(
                    component=name,
                    weight=normalize_weight(raw_weight),
                    raw_weight=str(raw_weight) if raw_weight is not None else None,
                    source=source,
                    price=first_float(item.get("price"), item.get("lastPrice"), item.get("px")),
                )
            )
    if not components:
        return unavailable_index_status("okx", asset, "not_supported", f"OKX 未返回 {asset} 指数成分")
    return CoinIndexStatus("okx", asset, "ok", "OKX 指数成分已更新。", components, datetime.now(timezone.utc))


def fetch_bitget_index_components(client: httpx.Client, symbol: str) -> CoinIndexStatus:
    asset = normalize_symbol(symbol)
    payload = request_json(client, f"{spot_base_url('bg')}/api/v3/market/index-components", {"symbol": f"{asset}USDT"})
    data = payload.get("data") if isinstance(payload, dict) else {}
    raw_components = data.get("componentList") if isinstance(data, dict) else []
    if not isinstance(raw_components, list) or not raw_components:
        return unavailable_index_status("bg", asset, "not_supported", f"Bitget 未返回 {asset} 指数成分")
    components: list[CoinIndexComponent] = []
    for item in raw_components:
        if not isinstance(item, dict):
            continue
        exchange = str(item.get("exchange") or "").strip()
        spot_pair = str(item.get("spotPair") or item.get("symbol") or "").strip()
        name = spot_pair or exchange
        raw_weight = item.get("weight")
        if name:
            components.append(
                CoinIndexComponent(
                    component=name,
                    weight=normalize_weight(raw_weight),
                    raw_weight=str(raw_weight) if raw_weight is not None else None,
                    source=exchange or None,
                    price=first_float(item.get("price"), item.get("lastPrice")),
                )
            )
    if not components:
        return unavailable_index_status("bg", asset, "not_supported", f"Bitget 未返回 {asset} 指数成分")
    return CoinIndexStatus("bg", asset, "ok", "Bitget 指数成分已更新。", components, datetime.now(timezone.utc))


def fetch_coin_index_status(exchange: str, symbol: str, cache_seconds: int = COIN_INDEX_CURRENT_CACHE_SECONDS) -> CoinIndexStatus:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    key = (normalized_exchange, normalized_symbol)
    with _coin_status_cache_lock:
        cached = _coin_index_cache.get(key)
        if cached and (now - cached[0]).total_seconds() < cache_seconds:
            payload = cached[1]
            components = [
                CoinIndexComponent(
                    component=str(item.get("component") or ""),
                    weight=item.get("weight"),
                    raw_weight=item.get("rawWeight"),
                    source=str(item.get("source") or "") or None,
                    price=first_float(item.get("price")),
                )
                for item in payload.get("components", [])
                if isinstance(item, dict)
            ]
            return CoinIndexStatus(
                exchange=str(payload["exchange"]),
                symbol=str(payload["symbol"]),
                status=str(payload["status"]),
                message=str(payload["message"]),
                components=components,
                updated_at=payload.get("updatedAt"),
            )
    try:
        with http_client() as client:
            if normalized_exchange == "bn":
                status = fetch_binance_index_components(client, normalized_symbol)
            elif normalized_exchange == "by":
                status = fetch_bybit_index_components(client, normalized_symbol)
            elif normalized_exchange == "okx":
                status = fetch_okx_index_components(client, normalized_symbol)
            elif normalized_exchange == "bg":
                status = fetch_bitget_index_components(client, normalized_symbol)
            else:
                status = unavailable_index_status(normalized_exchange, normalized_symbol, "not_supported", "指数成分监控仅启用 BN/BY/OKX/BG")
    except Exception as exc:
        status = unavailable_index_status(normalized_exchange, normalized_symbol, "error", f"指数成分查询失败：{exc}")
    with _coin_status_cache_lock:
        _coin_index_cache[key] = (now, index_status_to_out(status))
    return status


def crypto_borrow_check_overview(exchange: str, symbol: str, db: Session | None = None) -> dict[str, Any]:
    if is_delisted_crypto_symbol(symbol):
        normalized_symbol = normalize_symbol(symbol)
        normalized_exchange = normalize_exchange(exchange)
        return margin_short_check_to_out(
            unavailable_margin_short_check(normalized_exchange, normalized_symbol, "not_supported", delisted_crypto_symbol_reason(normalized_symbol) or "下架币已排除。")
        ) or {}
    check = fetch_margin_short_check(exchange, symbol)
    if db is not None and check.can_borrow is True:
        price = latest_borrow_value_price_usdt(db, check.symbol, check.exchange, allow_fetch=True)
        check = apply_min_borrowable_value_threshold(check, price)
    return margin_short_check_to_out(check) or {}


def normalize_borrow_watch_exchanges(values: list[str] | None = None) -> list[str]:
    raw = values or list(BORROW_WATCH_EXCHANGES)
    exchanges: list[str] = []
    for value in raw:
        code = normalize_exchange(value)
        if code not in MARGIN_PRIVATE_API_EXCHANGES:
            continue
        if code not in exchanges:
            exchanges.append(code)
    return [exchange for exchange in BORROW_WATCH_EXCHANGES if exchange in exchanges] or list(BORROW_WATCH_EXCHANGES)


def normalize_borrow_watch_refresh_seconds(value: int | None = None) -> int:
    if value is None:
        return BORROW_WATCH_REFRESH_SECONDS
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return BORROW_WATCH_REFRESH_SECONDS
    return min(BORROW_WATCH_ALLOWED_REFRESH_SECONDS, key=lambda candidate: abs(candidate - raw))


def borrow_watch_exchanges(item: CryptoBorrowWatchItem) -> list[str]:
    try:
        raw = json.loads(item.exchanges_json or "[]")
    except json.JSONDecodeError:
        raw = []
    if not isinstance(raw, list):
        raw = []
    return normalize_borrow_watch_exchanges([str(value) for value in raw])


def market_snapshot_price_usdt(snapshot: CryptoMarketQuoteSnapshot | None) -> float | None:
    if snapshot is None:
        return None
    return quote_reference_price_usdt(snapshot.best_bid, snapshot.best_ask, snapshot.mark_price, snapshot.index_price)


def latest_borrow_value_price_usdt(db: Session, symbol: str, exchange: str, allow_fetch: bool = False) -> float | None:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    for market_type in ("spot", "futures"):
        snapshot = db.scalar(
            select(CryptoMarketQuoteSnapshot)
            .where(
                CryptoMarketQuoteSnapshot.symbol == normalized_symbol,
                CryptoMarketQuoteSnapshot.exchange == normalized_exchange,
                CryptoMarketQuoteSnapshot.market_type == market_type,
                CryptoMarketQuoteSnapshot.status == "ok",
            )
            .order_by(desc(CryptoMarketQuoteSnapshot.batch_time), desc(CryptoMarketQuoteSnapshot.id))
            .limit(1)
        )
        price = market_snapshot_price_usdt(snapshot)
        if price is not None and price > 0:
            return price
    if not allow_fetch:
        return None
    try:
        quote = fetch_market(normalized_exchange, normalized_symbol, "spot")
    except Exception:
        return None
    if quote.status != "ok":
        return None
    return quote_reference_price_usdt(quote.best_bid, quote.best_ask, quote.mark_price, quote.index_price)


def apply_min_borrowable_value_threshold(check: MarginShortCheck, price: float | None) -> MarginShortCheck:
    if check.can_borrow is not True or check.borrowable_amount is None or check.borrowable_amount <= 0:
        if check.inventory_available is None:
            check.inventory_available = False
        return check
    check.inventory_available = True
    value = borrowable_value_usdt(check.borrowable_amount, price)
    check.borrowable_value_usdt = value
    if value is not None and value > MIN_BORROWABLE_VALUE_USDT:
        return check
    check.can_borrow = False
    # `inventoryAvailable` is a user-facing executable state.  Keeping it true
    # for a dust-sized exchange quota makes the UI, ranking and alarms report
    # "有 B" even though the position cannot pass our minimum order threshold.
    check.inventory_available = False
    check.status = "not_borrowable"
    suffix = borrow_value_message_suffix(value)
    if check.exchange == "bg":
        check.message = (
            f"Bitget 仅返回 {check.borrowable_amount:g} {check.symbol} 零散额度"
            f"{suffix}，按暂无可用 B 处理"
        )
    else:
        check.message = f"{check.message}{suffix}" if check.message else suffix.lstrip("；")
    return check


def apply_min_borrowable_value_threshold_to_payload(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("canBorrow") is not True:
        return payload
    price = latest_borrow_value_price_usdt(db, str(payload.get("symbol") or ""), str(payload.get("exchange") or ""), allow_fetch=False)
    value = borrowable_value_usdt(payload.get("borrowableAmount"), price)
    payload["borrowableValueUsdt"] = value
    if value is not None and value > MIN_BORROWABLE_VALUE_USDT:
        return payload
    payload["canBorrow"] = False
    payload["inventoryAvailable"] = False
    payload["status"] = "not_borrowable"
    message = str(payload.get("message") or "")
    suffix = borrow_value_message_suffix(value)
    if normalize_exchange(str(payload.get("exchange") or "")) == "bg":
        amount = first_float(payload.get("borrowableAmount"))
        amount_text = f"{amount:g}" if amount is not None else "未知"
        payload["message"] = (
            f"Bitget 仅返回 {amount_text} {normalize_symbol(str(payload.get('symbol') or ''))} 零散额度"
            f"{suffix}，按暂无可用 B 处理"
        )
    else:
        payload["message"] = f"{message}{suffix}" if message else suffix.lstrip("；")
    return payload


def borrow_watch_log_to_out(log: CryptoBorrowWatchLog | None) -> dict[str, Any] | None:
    if log is None:
        return None
    return {
        "id": log.id,
        "symbol": log.symbol,
        "exchange": log.exchange,
        "status": log.status,
        "message": log.message,
        "canBorrow": log.can_borrow,
        "borrowableAmount": log.borrowable_amount,
        "borrowableValueUsdt": None,
        "hourlyBorrowRate": log.hourly_borrow_rate,
        "dailyBorrowRate": log.daily_borrow_rate,
        "pushed": bool(log.pushed),
        "pushStatus": log.push_status,
        "pushMessage": log.push_message,
        "createdAt": log.created_at,
    }


def latest_borrow_watch_logs(db: Session, item: CryptoBorrowWatchItem) -> dict[str, dict[str, Any]]:
    exchanges = borrow_watch_exchanges(item)
    rows = db.scalars(
        select(CryptoBorrowWatchLog)
        .where(CryptoBorrowWatchLog.watch_item_id == item.id)
        .order_by(desc(CryptoBorrowWatchLog.created_at))
        .limit(max(20, len(exchanges) * 8))
    )
    latest: dict[str, dict[str, Any]] = {}
    for log in rows:
        if log.exchange not in latest:
            latest[log.exchange] = apply_min_borrowable_value_threshold_to_payload(db, borrow_watch_log_to_out(log) or {})
        if len(latest) >= len(exchanges):
            break
    for exchange in exchanges:
        latest.setdefault(
            exchange,
            {
                "symbol": item.symbol,
                "exchange": exchange,
                "status": "pending",
                "message": "等待后台检测。",
                "canBorrow": None,
                "borrowableAmount": None,
                "borrowableValueUsdt": None,
                "hourlyBorrowRate": None,
                "dailyBorrowRate": None,
                "pushed": False,
                "pushStatus": None,
                "pushMessage": None,
                "createdAt": None,
            },
        )
    return latest


def latest_borrow_watch_log(db: Session, item: CryptoBorrowWatchItem, exchange: str) -> CryptoBorrowWatchLog | None:
    return db.scalar(
        select(CryptoBorrowWatchLog)
        .where(CryptoBorrowWatchLog.watch_item_id == item.id, CryptoBorrowWatchLog.exchange == exchange)
        .order_by(desc(CryptoBorrowWatchLog.created_at))
        .limit(1)
    )


def borrow_watch_item_to_out(db: Session, item: CryptoBorrowWatchItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "symbol": item.symbol,
        "exchanges": borrow_watch_exchanges(item),
        "enabled": bool(item.enabled),
        "cooldownMinutes": item.cooldown_minutes,
        "lastCheckedAt": item.last_checked_at,
        "updatedAt": item.updated_at,
        "checks": latest_borrow_watch_logs(db, item),
    }


def monitor_event_to_out(event: CryptoMonitorEvent) -> dict[str, Any]:
    try:
        details = json.loads(event.details_json or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    return {
        "id": event.id,
        "symbol": event.symbol,
        "exchange": event.exchange,
        "exchangeName": EXCHANGE_NAMES.get(event.exchange, event.exchange),
        "eventType": event.event_type,
        "severity": event.severity,
        "title": event.title,
        "body": event.body,
        "details": details,
        "pushed": bool(event.pushed),
        "pushStatus": event.push_status,
        "pushMessage": event.push_message,
        "acknowledgedAt": event.acknowledged_at,
        "createdAt": event.created_at,
    }


def latest_crypto_monitor_events(db: Session, limit: int = 50) -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 200))
    rows = list(db.scalars(select(CryptoMonitorEvent).order_by(desc(CryptoMonitorEvent.created_at)).limit(bounded_limit)))
    unread = list(db.scalars(select(CryptoMonitorEvent).where(CryptoMonitorEvent.acknowledged_at.is_(None)).order_by(desc(CryptoMonitorEvent.created_at)).limit(500)))
    return {
        "status": "ok",
        "cacheVersion": FS_SIGNAL_CACHE_VERSION,
        "updatedAt": datetime.now(timezone.utc),
        "unreadCount": len(unread),
        "items": [monitor_event_to_out(row) for row in rows],
    }


def ack_crypto_monitor_event(db: Session, event_id: int) -> dict[str, Any]:
    event = db.get(CryptoMonitorEvent, event_id)
    if not event:
        raise ValueError("监控事件不存在")
    event.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    return latest_crypto_monitor_events(db)


def ack_all_crypto_monitor_events(db: Session) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    rows = list(db.scalars(select(CryptoMonitorEvent).where(CryptoMonitorEvent.acknowledged_at.is_(None)).limit(1000)))
    for row in rows:
        row.acknowledged_at = now
    db.commit()
    return latest_crypto_monitor_events(db)


def recent_crypto_monitor_event(db: Session, symbol: str, exchange: str, event_type: str, cooldown_minutes: int = BORROW_WATCH_COOLDOWN_MINUTES) -> CryptoMonitorEvent | None:
    return db.scalar(
        select(CryptoMonitorEvent)
        .where(
            CryptoMonitorEvent.symbol == symbol,
            CryptoMonitorEvent.exchange == exchange,
            CryptoMonitorEvent.event_type == event_type,
            CryptoMonitorEvent.created_at >= datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes),
        )
        .order_by(desc(CryptoMonitorEvent.created_at))
        .limit(1)
    )


def monitor_event_needs_active_borrow_watch(event_type: str) -> bool:
    return event_type in {
        "borrow_available",
        "transfer_blocked",
        "transfer_recovered",
        "index_component_changed",
    }


def has_active_borrow_watch(db: Session, symbol: str) -> bool:
    return bool(
        db.scalar(
            select(CryptoBorrowWatchItem.id)
            .where(
                CryptoBorrowWatchItem.symbol == normalize_symbol(symbol),
                CryptoBorrowWatchItem.enabled.is_(True),
            )
            .limit(1)
        )
    )


def add_crypto_monitor_event(
    db: Session,
    symbol: str,
    exchange: str,
    event_type: str,
    severity: str,
    title: str,
    body: str,
    details: dict[str, Any] | None = None,
    push: bool = True,
    cooldown_minutes: int = BORROW_WATCH_COOLDOWN_MINUTES,
) -> CryptoMonitorEvent | None:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    if monitor_event_needs_active_borrow_watch(event_type) and not has_active_borrow_watch(db, normalized_symbol):
        return None
    if recent_crypto_monitor_event(db, normalized_symbol, normalized_exchange, event_type, cooldown_minutes):
        return None
    pushed = False
    push_status = None
    push_message = None
    if push:
        push_status, push_message = send_bark_or_log(
            enabled=True,
            title=title,
            body=body,
            group="FS机会",
            url=None,
            disabled_message="FS 推送未启用。",
        )
        pushed = push_status == "ok"
    event = CryptoMonitorEvent(
        symbol=normalized_symbol,
        exchange=normalized_exchange,
        event_type=event_type,
        severity=severity,
        title=title,
        body=body,
        details_json=json.dumps(details or {}, ensure_ascii=False),
        pushed=pushed,
        push_status=push_status,
        push_message=push_message,
    )
    db.add(event)
    db.flush()
    return event


def borrow_watch_push_body(item: CryptoBorrowWatchItem, check: MarginShortCheck) -> str:
    amount = check.borrowable_amount
    amount_text = f"{amount:g} {item.symbol}" if isinstance(amount, (int, float)) and amount > 0 else "以交易页可开为准"
    value_text = f"，折算约 {check.borrowable_value_usdt:g}U" if isinstance(check.borrowable_value_usdt, (int, float)) else ""
    return (
        f"{item.symbol} {check.exchange} 当前可借；"
        f"可借数量 {amount_text}{value_text}，"
        f"小时利率 {rate_pct_text(check.hourly_borrow_rate)}，"
        f"日利率 {rate_pct_text(check.daily_borrow_rate)}。"
        "是否适合 FS 需要继续结合当前资金费、周期、手续费和滑点计算。"
    )


def recent_borrow_watch_push_log(db: Session, item: CryptoBorrowWatchItem, exchange: str) -> CryptoBorrowWatchLog | None:
    return db.scalar(
        select(CryptoBorrowWatchLog)
        .where(
            CryptoBorrowWatchLog.watch_item_id == item.id,
            CryptoBorrowWatchLog.exchange == exchange,
            CryptoBorrowWatchLog.can_borrow.is_(True),
            CryptoBorrowWatchLog.push_status.is_not(None),
            CryptoBorrowWatchLog.push_status != "skipped",
        )
        .order_by(desc(CryptoBorrowWatchLog.created_at))
        .limit(1)
    )


def add_borrow_watch_log(
    db: Session,
    item: CryptoBorrowWatchItem,
    check: MarginShortCheck,
    pushed: bool,
    push_status: str | None,
    push_message: str | None,
) -> CryptoBorrowWatchLog:
    log = CryptoBorrowWatchLog(
        watch_item_id=item.id,
        symbol=item.symbol,
        exchange=check.exchange,
        status=check.status,
        message=check.message,
        can_borrow=check.can_borrow,
        borrowable_amount=check.borrowable_amount,
        hourly_borrow_rate=check.hourly_borrow_rate,
        daily_borrow_rate=check.daily_borrow_rate,
        pushed=pushed,
        push_status=push_status,
        push_message=push_message,
    )
    db.add(log)
    db.flush()
    return log


def refresh_crypto_borrow_watch_item(db: Session, item: CryptoBorrowWatchItem, push: bool = True) -> int:
    if is_delisted_crypto_symbol(item.symbol):
        item.enabled = False
        item.last_checked_at = datetime.now(timezone.utc)
        item.updated_at = item.last_checked_at
        db.flush()
        return 0
    refreshed = 0
    for exchange in borrow_watch_exchanges(item):
        previous = latest_borrow_watch_log(db, item, exchange)
        check = fetch_margin_short_check(exchange, item.symbol)
        if check.can_borrow is True:
            price = latest_borrow_value_price_usdt(db, item.symbol, exchange, allow_fetch=True)
            check = apply_min_borrowable_value_threshold(check, price)
        pushed = False
        push_status = None
        push_message = None
        previous_payload = apply_min_borrowable_value_threshold_to_payload(db, borrow_watch_log_to_out(previous) or {}) if previous else None
        is_available = (
            check.can_borrow is True
            and isinstance(check.borrowable_amount, (int, float))
            and check.borrowable_amount > 0
            and isinstance(check.borrowable_value_usdt, (int, float))
            and check.borrowable_value_usdt > MIN_BORROWABLE_VALUE_USDT
        )
        was_available = (
            previous_payload is not None
            and previous_payload.get("canBorrow") is True
        )
        if is_available and not was_available:
            title = f"可借变可用 {item.symbol} {exchange}"
            event = add_crypto_monitor_event(
                db,
                item.symbol,
                exchange,
                "borrow_available",
                "opportunity",
                title,
                borrow_watch_push_body(item, check),
                {
                    "borrowableAmount": check.borrowable_amount,
                    "borrowableValueUsdt": check.borrowable_value_usdt,
                    "hourlyBorrowRate": check.hourly_borrow_rate,
                    "dailyBorrowRate": check.daily_borrow_rate,
                },
                push=push,
                cooldown_minutes=item.cooldown_minutes,
            )
            if event:
                pushed = event.pushed
                push_status = event.push_status
                push_message = event.push_message
        elif not is_available and was_available:
            db.execute(
                update(CryptoMonitorEvent)
                .where(
                    CryptoMonitorEvent.symbol == item.symbol,
                    CryptoMonitorEvent.exchange == exchange,
                    CryptoMonitorEvent.event_type == "borrow_available",
                    CryptoMonitorEvent.acknowledged_at.is_(None),
                )
                .values(acknowledged_at=datetime.now(timezone.utc))
            )
        add_borrow_watch_log(db, item, check, pushed, push_status, push_message)
        refreshed += 1
    now = datetime.now(timezone.utc)
    item.last_checked_at = now
    item.updated_at = now
    db.flush()
    return refreshed


def refresh_crypto_borrow_watchlist(
    db: Session,
    symbol: str | None = None,
    force: bool = False,
    push: bool = True,
    refresh_seconds: int | None = None,
) -> dict[str, Any]:
    if not _borrow_watch_scan_lock.acquire(blocking=False):
        return {"status": "running", "message": "借币监控正在后台扫描。", "refreshed": 0}
    started_at = datetime.now(timezone.utc)
    _borrow_watch_scan_state.update({"running": True, "startedAt": started_at, "finishedAt": _borrow_watch_scan_state.get("finishedAt")})
    try:
        query = select(CryptoBorrowWatchItem).where(CryptoBorrowWatchItem.enabled.is_(True)).order_by(CryptoBorrowWatchItem.symbol)
        if symbol:
            query = query.where(CryptoBorrowWatchItem.symbol == normalize_symbol(symbol))
        items = list(db.scalars(query))
        now = datetime.now(timezone.utc)
        effective_refresh_seconds = normalize_borrow_watch_refresh_seconds(refresh_seconds)
        refreshed = 0
        skipped = 0
        for item in items:
            last_checked = utc_datetime(item.last_checked_at)
            if not force and last_checked and (now - last_checked).total_seconds() < effective_refresh_seconds:
                skipped += 1
                continue
            refreshed += refresh_crypto_borrow_watch_item(db, item, push=push)
        db.commit()
        status = "ok" if refreshed else ("skipped" if skipped else "not_configured")
        return {
            "status": status,
            "message": "借币监控已刷新。" if refreshed else "借币监控暂无需要刷新的币。",
            "refreshed": refreshed,
            "skipped": skipped,
        }
    finally:
        finished_at = datetime.now(timezone.utc)
        _borrow_watch_scan_state.update({"running": False, "startedAt": started_at, "finishedAt": finished_at})
        _borrow_watch_scan_lock.release()


def run_crypto_borrow_watch_background_scan(symbol: str | None = None, force: bool = False, push: bool = True, refresh_seconds: int | None = None) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        refresh_crypto_borrow_watchlist(db, symbol=symbol, force=force, push=push, refresh_seconds=refresh_seconds)
    finally:
        db.close()


def start_crypto_borrow_watch_background_scan(symbol: str | None = None, force: bool = False, push: bool = True, refresh_seconds: int | None = None) -> bool:
    if _borrow_watch_scan_state.get("running"):
        return False
    thread = threading.Thread(target=run_crypto_borrow_watch_background_scan, args=(symbol, force, push, refresh_seconds), daemon=True)
    thread.start()
    return True


def create_crypto_borrow_watch(db: Session, symbol: str, exchanges: list[str] | None = None) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    if is_delisted_crypto_symbol(normalized_symbol):
        raise ValueError(delisted_crypto_symbol_reason(normalized_symbol) or "下架币已排除。")
    normalized_exchanges = normalize_borrow_watch_exchanges(exchanges)
    item = db.scalar(select(CryptoBorrowWatchItem).where(CryptoBorrowWatchItem.symbol == normalized_symbol).limit(1))
    if item:
        item.enabled = True
        item.exchanges_json = json.dumps(normalized_exchanges)
        item.updated_at = datetime.now(timezone.utc)
    else:
        item = CryptoBorrowWatchItem(
            symbol=normalized_symbol,
            exchanges_json=json.dumps(normalized_exchanges),
            cooldown_minutes=BORROW_WATCH_COOLDOWN_MINUTES,
        )
        db.add(item)
    db.commit()
    start_crypto_borrow_watch_background_scan(normalized_symbol, force=True, push=True)
    return crypto_borrow_watch_overview(db)


def delete_crypto_borrow_watch(db: Session, symbol: str) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    item = db.scalar(select(CryptoBorrowWatchItem).where(CryptoBorrowWatchItem.symbol == normalized_symbol).limit(1))
    if not item:
        raise ValueError("借币监控币种不存在")
    db.delete(item)
    db.execute(
        update(CryptoMonitorEvent)
        .where(
            CryptoMonitorEvent.symbol == normalized_symbol,
            CryptoMonitorEvent.acknowledged_at.is_(None),
            CryptoMonitorEvent.event_type.in_(
                [
                    "borrow_available",
                    "transfer_blocked",
                    "transfer_recovered",
                    "index_component_changed",
                ]
            ),
        )
        .values(acknowledged_at=datetime.now(timezone.utc))
    )
    db.commit()
    return crypto_borrow_watch_overview(db)


def borrow_watch_needs_refresh(db: Session, refresh_seconds: int | None = None) -> bool:
    now = datetime.now(timezone.utc)
    effective_refresh_seconds = normalize_borrow_watch_refresh_seconds(refresh_seconds)
    items = list(db.scalars(select(CryptoBorrowWatchItem).where(CryptoBorrowWatchItem.enabled.is_(True)).limit(100)))
    for item in items:
        last_checked = utc_datetime(item.last_checked_at)
        if last_checked is None or (now - last_checked).total_seconds() >= effective_refresh_seconds:
            return True
    return False


def crypto_borrow_watch_overview(db: Session, refresh_seconds: int | None = None) -> dict[str, Any]:
    effective_refresh_seconds = normalize_borrow_watch_refresh_seconds(refresh_seconds)
    items = list(db.scalars(select(CryptoBorrowWatchItem).order_by(CryptoBorrowWatchItem.symbol)))
    if items and borrow_watch_needs_refresh(db, effective_refresh_seconds):
        start_crypto_borrow_watch_background_scan(force=False, push=True, refresh_seconds=effective_refresh_seconds)
    state = dict(_borrow_watch_scan_state)
    item_payloads = [borrow_watch_item_to_out(db, item) for item in items]
    checks = [check for item in item_payloads for check in item.get("checks", {}).values()]
    borrowable_count = len(
        [
            check
            for check in checks
            if check.get("canBorrow") is True
        ]
    )
    pushed_count = len([check for check in checks if check.get("pushed")])
    return {
        "status": "ok",
        "updatedAt": datetime.now(timezone.utc),
        "defaultExchanges": list(BORROW_WATCH_EXCHANGES),
        "refreshSeconds": effective_refresh_seconds,
        "cooldownMinutes": BORROW_WATCH_COOLDOWN_MINUTES,
        "scanning": bool(state.get("running")),
        "scanStartedAt": state.get("startedAt"),
        "scanFinishedAt": state.get("finishedAt"),
        "itemCount": len(item_payloads),
        "borrowableCount": borrowable_count,
        "pushedCount": pushed_count,
        "items": item_payloads,
    }


def safe_json_list(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def latest_coin_status_log(db: Session, symbol: str, exchange: str) -> CryptoCoinStatusLog | None:
    return db.scalar(
        select(CryptoCoinStatusLog)
        .where(CryptoCoinStatusLog.symbol == normalize_symbol(symbol), CryptoCoinStatusLog.exchange == normalize_exchange(exchange))
        .order_by(desc(CryptoCoinStatusLog.created_at))
        .limit(1)
    )


def coin_status_log_to_out(log: CryptoCoinStatusLog | None, symbol: str, exchange: str) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    if log is None:
        return {
            "exchange": normalized_exchange,
            "exchangeName": EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange),
            "symbol": normalized_symbol,
            "status": "pending",
            "message": "等待后台检测。",
            "transfer": {
                "exchange": normalized_exchange,
                "symbol": normalized_symbol,
                "status": "pending",
                "message": "等待后台检测。",
                "depositEnabled": None,
                "withdrawEnabled": None,
                "chains": [],
                "updatedAt": None,
            },
            "borrow": margin_short_check_to_out(MarginShortCheck(normalized_exchange, normalized_symbol, "pending", "等待后台检测。")),
            "index": {
                "exchange": normalized_exchange,
                "symbol": normalized_symbol,
                "status": "pending",
                "message": "等待后台检测。",
                "components": [],
                "updatedAt": None,
            },
            "updatedAt": None,
        }
    return {
        "exchange": log.exchange,
        "exchangeName": EXCHANGE_NAMES.get(log.exchange, log.exchange),
        "symbol": log.symbol,
        "status": log.status,
        "message": log.message,
        "transfer": {
            "exchange": log.exchange,
            "symbol": log.symbol,
            "status": log.status,
            "message": log.message,
            "depositEnabled": log.deposit_enabled,
            "withdrawEnabled": log.withdraw_enabled,
            "chains": safe_json_list(log.chains_json),
            "updatedAt": log.created_at,
        },
        "borrow": margin_short_check_to_out(
            MarginShortCheck(
                exchange=log.exchange,
                symbol=log.symbol,
                status=log.borrow_status or "pending",
                message=log.borrow_message or "等待后台检测。",
                can_borrow=log.can_borrow,
                borrowable_amount=log.borrowable_amount,
                hourly_borrow_rate=log.hourly_borrow_rate,
                daily_borrow_rate=log.daily_borrow_rate,
                updated_at=log.created_at,
            )
        ),
        "index": {
            "exchange": log.exchange,
            "symbol": log.symbol,
            "status": log.index_status or "pending",
            "message": log.index_message or "等待后台检测。",
            "components": safe_json_list(log.index_components_json),
            "updatedAt": log.created_at,
        },
        "updatedAt": log.created_at,
    }


def latest_coin_status_logs(db: Session, symbol: str) -> dict[str, dict[str, Any]]:
    normalized_symbol = normalize_symbol(symbol)
    rows = db.scalars(
        select(CryptoCoinStatusLog)
        .where(CryptoCoinStatusLog.symbol == normalized_symbol)
        .order_by(desc(CryptoCoinStatusLog.created_at))
        .limit(len(COIN_STATUS_EXCHANGES) * 8)
    )
    latest: dict[str, dict[str, Any]] = {}
    for log in rows:
        if log.exchange not in latest:
            payload = coin_status_log_to_out(log, normalized_symbol, log.exchange)
            borrow_payload = payload.get("borrow")
            if isinstance(borrow_payload, dict):
                payload["borrow"] = apply_min_borrowable_value_threshold_to_payload(db, borrow_payload)
            latest[log.exchange] = payload
        if len(latest) >= len(COIN_STATUS_EXCHANGES):
            break
    for exchange in COIN_STATUS_EXCHANGES:
        latest.setdefault(exchange, coin_status_log_to_out(None, normalized_symbol, exchange))
    return latest


def latest_coin_index_changes(db: Session, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    normalized_symbol = normalize_symbol(symbol)
    rows = db.scalars(
        select(CryptoIndexComponentChangeLog)
        .where(
            CryptoIndexComponentChangeLog.symbol == normalized_symbol,
            CryptoIndexComponentChangeLog.exchange.in_(COIN_INDEX_EXCHANGES),
        )
        .order_by(desc(CryptoIndexComponentChangeLog.created_at))
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "symbol": row.symbol,
            "exchange": row.exchange,
            "exchangeName": EXCHANGE_NAMES.get(row.exchange, row.exchange),
            "component": row.component,
            "oldWeight": row.old_weight,
            "newWeight": row.new_weight,
            "diff": row.diff,
            "pushed": row.pushed,
            "pushStatus": row.push_status,
            "pushMessage": row.push_message,
            "createdAt": row.created_at,
        }
        for row in rows
    ]


def coin_status_needs_refresh(db: Session, symbol: str, refresh_seconds: int) -> bool:
    normalized_symbol = normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    for exchange in COIN_STATUS_EXCHANGES:
        latest = latest_coin_status_log(db, normalized_symbol, exchange)
        if latest is None:
            return True
        created_at = utc_datetime(latest.created_at)
        if created_at is None or (now - created_at).total_seconds() >= refresh_seconds:
            return True
    return False


def index_component_key(component: dict[str, Any]) -> str:
    source = str(component.get("source") or "").strip()
    name = str(component.get("component") or "").strip()
    return " / ".join(part for part in (source, name) if part)


def index_components_by_key(components: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for component in components:
        key = index_component_key(component)
        weight = component.get("weight")
        if key and isinstance(weight, (int, float)):
            values[key] = {**component, "weight": float(weight)}
    return values


def record_index_component_changes(
    db: Session,
    symbol: str,
    exchange: str,
    previous: CryptoCoinStatusLog | None,
    current_components: list[dict[str, Any]],
    push: bool,
) -> int:
    if exchange not in COIN_INDEX_EXCHANGES:
        return 0
    if previous is None or not current_components:
        return 0
    previous_components = index_components_by_key(safe_json_list(previous.index_components_json))
    current_by_name = index_components_by_key(current_components)
    created = 0
    for component_key, current_component in current_by_name.items():
        previous_component = previous_components.get(component_key)
        if previous_component is None:
            continue
        component = str(current_component.get("component") or component_key).strip()
        source = str(current_component.get("source") or "").strip()
        new_weight = float(current_component["weight"])
        old_weight = float(previous_component["weight"])
        diff = new_weight - old_weight
        if abs(diff) < COIN_INDEX_CHANGE_THRESHOLD:
            continue
        recent = db.scalar(
            select(CryptoIndexComponentChangeLog)
            .where(
                CryptoIndexComponentChangeLog.symbol == symbol,
                CryptoIndexComponentChangeLog.exchange == exchange,
                CryptoIndexComponentChangeLog.component == component_key,
                CryptoIndexComponentChangeLog.created_at >= datetime.now(timezone.utc) - timedelta(minutes=BORROW_WATCH_COOLDOWN_MINUTES),
            )
            .order_by(desc(CryptoIndexComponentChangeLog.created_at))
            .limit(1)
        )
        if recent:
            continue
        pushed = False
        push_status = None
        push_message = None
        event = add_crypto_monitor_event(
            db,
            symbol,
            exchange,
            "index_component_changed",
            "change",
            f"指数成分变化 {symbol} {exchange}",
            (
                f"{EXCHANGE_NAMES.get(exchange, exchange)} · {source or '来源未记录'}: "
                f"{rate_pct_text(old_weight)} -> {rate_pct_text(new_weight)} ({rate_pct_text(diff)})"
            ),
            {
                "component": component,
                "source": source or None,
                "oldWeight": old_weight,
                "newWeight": new_weight,
                "diff": diff,
                "price": current_component.get("price"),
            },
            push=push,
        )
        if event:
            pushed = event.pushed
            push_status = event.push_status
            push_message = event.push_message
        db.add(
            CryptoIndexComponentChangeLog(
                symbol=symbol,
                exchange=exchange,
                component=component_key,
                old_weight=old_weight,
                new_weight=new_weight,
                diff=diff,
                pushed=pushed,
                push_status=push_status,
                push_message=push_message,
            )
        )
        created += 1
    for component_key, previous_component in previous_components.items():
        if component_key in current_by_name:
            continue
        old_weight = float(previous_component["weight"])
        diff = -old_weight
        if abs(diff) < COIN_INDEX_CHANGE_THRESHOLD:
            continue
        recent = db.scalar(
            select(CryptoIndexComponentChangeLog)
            .where(
                CryptoIndexComponentChangeLog.symbol == symbol,
                CryptoIndexComponentChangeLog.exchange == exchange,
                CryptoIndexComponentChangeLog.component == component_key,
                CryptoIndexComponentChangeLog.created_at >= datetime.now(timezone.utc) - timedelta(minutes=BORROW_WATCH_COOLDOWN_MINUTES),
            )
            .order_by(desc(CryptoIndexComponentChangeLog.created_at))
            .limit(1)
        )
        if recent:
            continue
        component = str(previous_component.get("component") or component_key).strip()
        source = str(previous_component.get("source") or "").strip()
        pushed = False
        push_status = None
        push_message = None
        event = add_crypto_monitor_event(
            db,
            symbol,
            exchange,
            "index_component_changed",
            "change",
            f"指数成分移除 {symbol} {exchange}",
            (
                f"{EXCHANGE_NAMES.get(exchange, exchange)} · {source or '来源未记录'}: "
                f"{rate_pct_text(old_weight)} -> 0.00% ({rate_pct_text(diff)})"
            ),
            {
                "component": component,
                "source": source or None,
                "oldWeight": old_weight,
                "newWeight": 0.0,
                "diff": diff,
                "price": previous_component.get("price"),
                "removed": True,
            },
            push=push,
        )
        if event:
            pushed = event.pushed
            push_status = event.push_status
            push_message = event.push_message
        db.add(
            CryptoIndexComponentChangeLog(
                symbol=symbol,
                exchange=exchange,
                component=component_key,
                old_weight=old_weight,
                new_weight=0.0,
                diff=diff,
                pushed=pushed,
                push_status=push_status,
                push_message=push_message,
            )
        )
        created += 1
    return created


def maybe_record_transfer_events(db: Session, symbol: str, exchange: str, previous: CryptoCoinStatusLog | None, transfer: CoinTransferStatus, push: bool) -> None:
    if previous is None:
        return
    changes: list[tuple[str, str, bool | None, bool | None]] = [
        ("deposit", "充币", previous.deposit_enabled, transfer.deposit_enabled),
        ("withdraw", "提币", previous.withdraw_enabled, transfer.withdraw_enabled),
    ]
    for key, label, old_value, new_value in changes:
        if new_value is False and old_value is not False:
            add_crypto_monitor_event(
                db=db,
                symbol=symbol,
                exchange=exchange,
                event_type="transfer_blocked",
                severity="risk",
                title=f"{label}不可用 {symbol} {exchange}",
                body=f"{symbol} 在 {EXCHANGE_NAMES.get(exchange, exchange)} 的{label}从正常/未知变为不可用。",
                details={"field": key, "old": old_value, "new": new_value},
                push=push,
            )
        elif new_value is True and old_value is False:
            add_crypto_monitor_event(
                db=db,
                symbol=symbol,
                exchange=exchange,
                event_type="transfer_recovered",
                severity="info",
                title=f"{label}恢复 {symbol} {exchange}",
                body=f"{symbol} 在 {EXCHANGE_NAMES.get(exchange, exchange)} 的{label}已恢复可用。",
                details={"field": key, "old": old_value, "new": new_value},
                push=push,
            )


def add_coin_status_log(
    db: Session,
    symbol: str,
    exchange: str,
    transfer: CoinTransferStatus,
    borrow: MarginShortCheck,
    index: CoinIndexStatus,
) -> CryptoCoinStatusLog:
    status_values = [transfer.status, borrow.status, index.status]
    if all(value in {"ok", "not_supported", "not_borrowable", "pending"} for value in status_values):
        status = "ok"
    elif any(value == "ok" for value in status_values):
        status = "partial_error"
    else:
        status = "error"
    chains = [chain_status_to_out(chain) for chain in (transfer.chains or [])]
    components = [index_component_to_out(component) for component in (index.components or [])]
    log = CryptoCoinStatusLog(
        symbol=symbol,
        exchange=exchange,
        status=status,
        message=transfer.message,
        deposit_enabled=transfer.deposit_enabled,
        withdraw_enabled=transfer.withdraw_enabled,
        chains_json=json.dumps(chains, ensure_ascii=False),
        borrow_status=borrow.status,
        borrow_message=borrow.message,
        can_borrow=borrow.can_borrow,
        borrowable_amount=borrow.borrowable_amount,
        hourly_borrow_rate=borrow.hourly_borrow_rate,
        daily_borrow_rate=borrow.daily_borrow_rate,
        index_status=index.status,
        index_message=index.message,
        index_components_json=json.dumps(components, ensure_ascii=False),
    )
    db.add(log)
    db.flush()
    return log


def refresh_crypto_coin_status_symbol(db: Session, symbol: str, push: bool = False, index_cache_seconds: int = COIN_INDEX_CURRENT_CACHE_SECONDS) -> int:
    normalized_symbol = normalize_symbol(symbol)
    refreshed = 0
    for exchange in COIN_STATUS_EXCHANGES:
        previous = latest_coin_status_log(db, normalized_symbol, exchange)
        transfer = fetch_coin_transfer_status(exchange, normalized_symbol)
        borrow = fetch_margin_short_check(exchange, normalized_symbol)
        if borrow.can_borrow is True:
            price = latest_borrow_value_price_usdt(db, normalized_symbol, exchange, allow_fetch=True)
            borrow = apply_min_borrowable_value_threshold(borrow, price)
        index = fetch_coin_index_status(exchange, normalized_symbol, cache_seconds=index_cache_seconds)
        maybe_record_transfer_events(db, normalized_symbol, exchange, previous, transfer, push)
        record_index_component_changes(
            db,
            normalized_symbol,
            exchange,
            previous,
            [index_component_to_out(component) for component in (index.components or [])],
            push=push,
        )
        add_coin_status_log(db, normalized_symbol, exchange, transfer, borrow, index)
        refreshed += 1
    db.commit()
    return refreshed


def refresh_crypto_coin_status_watchlist(db: Session, push: bool = True) -> dict[str, Any]:
    items = list(db.scalars(select(CryptoBorrowWatchItem).where(CryptoBorrowWatchItem.enabled.is_(True)).order_by(CryptoBorrowWatchItem.symbol)))
    refreshed = 0
    for item in items:
        if not coin_status_needs_refresh(db, item.symbol, COIN_INDEX_WATCH_CACHE_SECONDS):
            continue
        refreshed += refresh_crypto_coin_status_symbol(db, item.symbol, push=push, index_cache_seconds=COIN_INDEX_WATCH_CACHE_SECONDS)
    return {"status": "ok", "refreshed": refreshed, "symbols": len(items)}


def run_crypto_coin_status_background_scan(symbols: list[str], force: bool = False, push: bool = False) -> None:
    from app.database import SessionLocal

    if not _coin_status_scan_lock.acquire(blocking=False):
        return
    started_at = datetime.now(timezone.utc)
    _coin_status_scan_state.update({"running": True, "startedAt": started_at, "finishedAt": _coin_status_scan_state.get("finishedAt")})
    db = SessionLocal()
    try:
        for symbol in symbols:
            try:
                if push and not has_active_borrow_watch(db, symbol):
                    continue
                if force or coin_status_needs_refresh(db, symbol, COIN_INDEX_CURRENT_CACHE_SECONDS):
                    refresh_crypto_coin_status_symbol(db, symbol, push=push, index_cache_seconds=COIN_INDEX_CURRENT_CACHE_SECONDS)
            except Exception:
                db.rollback()
                continue
    finally:
        db.close()
        finished_at = datetime.now(timezone.utc)
        _coin_status_scan_state.update({"running": False, "startedAt": started_at, "finishedAt": finished_at})
        _coin_status_scan_lock.release()


def start_crypto_coin_status_background_scan(symbols: list[str], force: bool = False, push: bool = False) -> bool:
    if _coin_status_scan_state.get("running"):
        return False
    normalized: list[str] = []
    for symbol in symbols:
        try:
            value = normalize_symbol(symbol)
        except ValueError:
            continue
        if is_delisted_crypto_symbol(value):
            continue
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        return False
    thread = threading.Thread(target=run_crypto_coin_status_background_scan, args=(normalized, force, push), daemon=True)
    thread.start()
    return True


def crypto_coin_status_overview(db: Session, symbol: str, force: bool = False, push: bool = False) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    if force or coin_status_needs_refresh(db, normalized_symbol, COIN_INDEX_CURRENT_CACHE_SECONDS):
        refresh_crypto_coin_status_symbol(db, normalized_symbol, push=push, index_cache_seconds=COIN_INDEX_CURRENT_CACHE_SECONDS)
    exchange_statuses = latest_coin_status_logs(db, normalized_symbol)
    updated_at_values = [
        utc_datetime(value.get("updatedAt"))
        for value in exchange_statuses.values()
        if value.get("updatedAt") is not None
    ]
    return {
        "status": "ok",
        "symbol": normalized_symbol,
        "exchanges": list(COIN_STATUS_EXCHANGES),
        "exchangeStatuses": exchange_statuses,
        "indexChanges": latest_coin_index_changes(db, normalized_symbol),
        "indexChangeThreshold": COIN_INDEX_CHANGE_THRESHOLD,
        "updatedAt": max(updated_at_values) if updated_at_values else None,
    }


def crypto_coin_status_batch_overview(db: Session, symbols: list[str], force: bool = False) -> dict[str, Any]:
    normalized: list[str] = []
    for symbol in symbols:
        try:
            value = normalize_symbol(symbol)
        except ValueError:
            continue
        if value not in normalized:
            normalized.append(value)
        if len(normalized) >= 30:
            break
    if force:
        start_crypto_coin_status_background_scan(normalized, force=True, push=False)
    else:
        stale = [symbol for symbol in normalized if coin_status_needs_refresh(db, symbol, COIN_INDEX_CURRENT_CACHE_SECONDS)]
        if stale:
            start_crypto_coin_status_background_scan(stale, force=False, push=False)
    state = dict(_coin_status_scan_state)
    items: dict[str, Any] = {}
    updated_at_values: list[datetime] = []
    for symbol in normalized:
        exchange_statuses = latest_coin_status_logs(db, symbol)
        updated_at_values.extend(
            [
                value
                for value in (utc_datetime(status.get("updatedAt")) for status in exchange_statuses.values())
                if value is not None
            ]
        )
        items[symbol] = {
            "symbol": symbol,
            "exchangeStatuses": exchange_statuses,
            "indexChanges": latest_coin_index_changes(db, symbol, limit=5),
        }
    return {
        "status": "ok",
        "symbols": normalized,
        "items": items,
        "scanning": bool(state.get("running")),
        "scanStartedAt": state.get("startedAt"),
        "scanFinishedAt": state.get("finishedAt"),
        "updatedAt": max(updated_at_values) if updated_at_values else None,
    }


def recent_negative_funding_candidates(db: Session, exchanges: list[str], minutes: int = 30, limit: int = 500) -> list[dict[str, Any]]:
    latest_batch_time = db.scalar(
        select(CryptoMarketQuoteSnapshot.batch_time)
        .order_by(desc(CryptoMarketQuoteSnapshot.batch_time))
        .limit(1)
    )
    if latest_batch_time is None:
        return []
    cutoff = latest_batch_time - timedelta(minutes=max(1, minutes))
    rows = db.execute(
        select(
            CryptoMarketQuoteSnapshot.exchange,
            CryptoMarketQuoteSnapshot.symbol,
            CryptoMarketQuoteSnapshot.funding_rate,
            CryptoMarketQuoteSnapshot.premium_rate,
            CryptoMarketQuoteSnapshot.volume_24h,
            CryptoMarketQuoteSnapshot.batch_time,
        )
        .where(
            CryptoMarketQuoteSnapshot.batch_time >= cutoff,
            CryptoMarketQuoteSnapshot.exchange.in_(exchanges),
            CryptoMarketQuoteSnapshot.market_type == "futures",
            CryptoMarketQuoteSnapshot.status == "ok",
            CryptoMarketQuoteSnapshot.funding_rate < 0,
        )
        .order_by(desc(CryptoMarketQuoteSnapshot.batch_time))
        .limit(limit)
    )
    latest: dict[tuple[str, str], Any] = {}
    for exchange, symbol, funding_rate, premium_rate, volume_24h, batch_time in rows:
        if is_delisted_crypto_symbol(symbol):
            continue
        key = (exchange, symbol)
        if key not in latest:
            latest[key] = (exchange, symbol, funding_rate, premium_rate, volume_24h, batch_time)
    return [
        {
            "exchange": row[0],
            "symbol": row[1],
            "fundingRate": row[2],
            "premiumRate": row[3],
            "volume24h": row[4],
            "fundingUpdatedAt": row[5],
        }
        for row in latest.values()
    ]


def scan_one_margin_borrow(exchange: str, symbol: str) -> dict[str, Any]:
    check = fetch_margin_short_check(exchange, symbol)
    return margin_short_check_to_out(check) or {}


def scan_margin_borrow_exchange(exchange: str, candidates: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    normalized_exchange = normalize_exchange(exchange)
    if normalized_exchange not in MARGIN_PRIVATE_API_EXCHANGES:
        return {
            "exchange": normalized_exchange,
            "status": "not_supported",
            "message": "该交易所借币扫描暂未接入。",
            "checked": 0,
            "borrowableCount": 0,
            "notBorrowableCount": 0,
            "errorCount": 0,
            "items": [],
            "errors": [],
        }
    if not has_private_credentials(normalized_exchange):
        return {
            "exchange": normalized_exchange,
            "status": "needs_authorization",
            "message": "未配置该交易所 API Key/Secret。",
            "checked": 0,
            "borrowableCount": 0,
            "notBorrowableCount": 0,
            "errorCount": 0,
            "items": [],
            "errors": [],
        }
    exchange_candidates = [item for item in candidates if item.get("exchange") == normalized_exchange]
    if normalized_exchange == "bn":
        try:
            with http_client() as client:
                borrowable_assets = fetch_binance_margin_asset_symbols(client)
            if borrowable_assets:
                exchange_candidates = [item for item in exchange_candidates if item.get("symbol") in borrowable_assets]
        except Exception:
            pass
    if limit > 0:
        exchange_candidates = exchange_candidates[:limit]

    results: list[dict[str, Any]] = []
    candidate_by_symbol = {str(item.get("symbol")): item for item in exchange_candidates if item.get("symbol")}
    if exchange_candidates:
        with ThreadPoolExecutor(max_workers=min(MARGIN_SCAN_MAX_WORKERS, len(exchange_candidates))) as executor:
            future_map = {executor.submit(scan_one_margin_borrow, normalized_exchange, str(item.get("symbol"))): str(item.get("symbol")) for item in exchange_candidates if item.get("symbol")}
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = margin_short_check_to_out(
                        unavailable_margin_short_check(normalized_exchange, symbol, "error", f"借币扫描失败：{exc}")
                    ) or {}
                candidate = candidate_by_symbol.get(symbol) or {}
                results.append({**row, **candidate})

    borrowable = sorted(
        [row for row in results if row.get("canBorrow") is True],
        key=lambda row: (row.get("symbol") or ""),
    )
    errors = [row for row in results if row.get("status") == "error"]
    not_borrowable = [row for row in results if row.get("status") == "not_borrowable"]
    status = "ok" if borrowable else ("partial_error" if errors else "manual_only")
    return {
        "exchange": normalized_exchange,
        "status": status,
        "message": f"已扫描 {len(results)} 个负费率币，可借 {len(borrowable)} 个。",
        "negativeFundingCandidateCount": len(exchange_candidates),
        "checked": len(results),
        "borrowableCount": len(borrowable),
        "notBorrowableCount": len(not_borrowable),
        "errorCount": len(errors),
        "items": borrowable,
        "errors": errors[:20],
    }


def crypto_borrow_scan_overview(db: Session, exchanges: list[str] | None = None, limit: int = 1000) -> dict[str, Any]:
    selected = exchanges or sorted(MARGIN_PRIVATE_API_EXCHANGES)
    normalized_exchanges: list[str] = []
    for exchange in selected:
        code = normalize_exchange(exchange)
        if code not in normalized_exchanges:
            normalized_exchanges.append(code)
    candidates = recent_negative_funding_candidates(db, normalized_exchanges)
    exchange_results = [scan_margin_borrow_exchange(exchange, candidates, limit) for exchange in normalized_exchanges]
    total_borrowable = sum(int(result.get("borrowableCount") or 0) for result in exchange_results)
    return {
        "status": "ok",
        "updatedAt": datetime.now(timezone.utc),
        "source": "recent_negative_funding_futures",
        "negativeFundingCandidateCount": len(candidates),
        "limit": limit,
        "totalBorrowableCount": total_borrowable,
        "exchanges": exchange_results,
    }


def usdt_perp_base_symbol(value: Any, suffix: str = "USDT") -> str | None:
    text = str(value or "").strip().upper()
    if not text.endswith(suffix):
        return None
    base = text[: -len(suffix)]
    if not base or any(char in base for char in ("_", "-", "/")):
        return None
    return base


def fs_negative_potential(rate: Any, premium_rate: Any) -> tuple[bool, str | None, str | None]:
    funding = first_float(rate)
    premium = first_float(premium_rate)
    if funding is not None and funding < 0:
        return True, "current_negative", "当前资金费已为负"
    if premium is not None and premium < 0:
        return True, "premium_negative", "合约溢价已为负，资金费可能转负"
    return False, None, None


def append_negative_candidate(items: list[dict[str, Any]], row: dict[str, Any]) -> None:
    rate = first_float(row.get("fundingRate"))
    premium_rate = first_float(row.get("premiumRate"))
    symbol = row.get("symbol")
    exchange = row.get("exchange")
    potential, potential_type, potential_reason = fs_negative_potential(rate, premium_rate)
    if not symbol or not exchange or not potential:
        return
    if is_delisted_crypto_symbol(str(symbol)):
        return
    row["dailyFundingRate"] = fs_daily_funding_rate(rate, row.get("periodHours"))
    row["negativePotential"] = True
    row["potentialType"] = potential_type
    row["potentialReason"] = potential_reason
    items.append(row)


def fetch_binance_realtime_negative_funding_candidates(client: httpx.Client) -> list[dict[str, Any]]:
    rows = request_json(client, f"{base_url('bn')}/fapi/v1/premiumIndex")
    stats_rows: list[dict[str, Any]] = []
    try:
        stats_payload = request_json(client, f"{base_url('bn')}/fapi/v1/ticker/24hr")
        if isinstance(stats_payload, list):
            stats_rows = [row for row in stats_payload if isinstance(row, dict)]
    except Exception:
        stats_rows = []
    volume_by_symbol = {str(row.get("symbol") or ""): parse_float(row.get("quoteVolume")) for row in stats_rows}
    funding_info_rows = []
    try:
        funding_info_payload = request_json(client, f"{base_url('bn')}/fapi/v1/fundingInfo")
        if isinstance(funding_info_payload, list):
            funding_info_rows = [row for row in funding_info_payload if isinstance(row, dict)]
    except Exception:
        funding_info_rows = []
    interval_by_symbol = {str(row.get("symbol") or ""): first_float(row.get("fundingIntervalHours")) for row in funding_info_rows}
    candidates: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        market_symbol = str(row.get("symbol") or "")
        symbol = usdt_perp_base_symbol(market_symbol)
        if not symbol:
            continue
        mark_price = parse_float(row.get("markPrice"))
        index_price = parse_float(row.get("indexPrice"))
        append_negative_candidate(
            candidates,
            {
                "exchange": "bn",
                "symbol": symbol,
                "fundingRate": parse_float(row.get("lastFundingRate")),
                "premiumRate": computed_premium(mark_price, index_price),
                "volume24h": volume_by_symbol.get(market_symbol),
                "periodHours": interval_by_symbol.get(market_symbol) or 8,
                "fundingUpdatedAt": parse_timestamp_ms(row.get("nextFundingTime")) or datetime.now(timezone.utc),
            },
        )
    return candidates


def fetch_bybit_realtime_negative_funding_candidates(client: httpx.Client) -> list[dict[str, Any]]:
    payload = request_json(client, f"{base_url('by')}/v5/market/tickers", {"category": "linear"})
    rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else []
    candidates: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        market_symbol = str(row.get("symbol") or "")
        symbol = usdt_perp_base_symbol(market_symbol)
        if not symbol:
            continue
        mark_price = parse_float(row.get("markPrice"))
        index_price = parse_float(row.get("indexPrice"))
        append_negative_candidate(
            candidates,
            {
                "exchange": "by",
                "symbol": symbol,
                "fundingRate": parse_float(row.get("fundingRate")),
                "premiumRate": computed_premium(mark_price, index_price),
                "volume24h": parse_float(row.get("turnover24h")),
                "periodHours": first_float(row.get("fundingIntervalHour"), 8),
                "fundingUpdatedAt": parse_timestamp_ms(row.get("nextFundingTime")) or datetime.now(timezone.utc),
            },
        )
    return candidates


def fetch_gate_realtime_negative_funding_candidates(client: httpx.Client) -> list[dict[str, Any]]:
    rows = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/contracts")
    candidates: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("in_delisting") is True:
            continue
        contract = str(row.get("name") or "")
        if not contract.endswith("_USDT"):
            continue
        symbol = contract[: -len("_USDT")].upper()
        if not symbol or any(char in symbol for char in ("-", "/")):
            continue
        mark_price = parse_float(row.get("mark_price"))
        index_price = parse_float(row.get("index_price"))
        append_negative_candidate(
            candidates,
            {
                "exchange": "gt",
                "symbol": symbol,
                "fundingRate": parse_float(row.get("funding_rate")),
                "premiumRate": parse_float(row.get("funding_rate_indicative")) or computed_premium(mark_price, index_price),
                "volume24h": parse_float(row.get("volume_24h_quote")) or parse_float(row.get("volume_24h_settle")),
                "periodHours": (parse_float(row.get("funding_interval")) or 0) / 3600 or 8,
                "fundingUpdatedAt": parse_timestamp_s(row.get("funding_next_apply")) or datetime.now(timezone.utc),
            },
        )
    return candidates


def fetch_bitget_realtime_negative_funding_candidates(client: httpx.Client) -> list[dict[str, Any]]:
    funding_payload = bitget_data(request_json(client, f"{base_url('bg')}/api/v2/mix/market/current-fund-rate", {"productType": "USDT-FUTURES"}))
    ticker_payload: Any = []
    try:
        ticker_payload = bitget_data(request_json(client, f"{base_url('bg')}/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"}))
    except Exception:
        ticker_payload = []
    ticker_by_symbol = {
        str(row.get("symbol") or ""): row
        for row in (ticker_payload if isinstance(ticker_payload, list) else [])
        if isinstance(row, dict)
    }
    candidates: list[dict[str, Any]] = []
    for row in funding_payload if isinstance(funding_payload, list) else []:
        if not isinstance(row, dict):
            continue
        market_symbol = str(row.get("symbol") or "")
        symbol = usdt_perp_base_symbol(market_symbol)
        if not symbol:
            continue
        ticker = ticker_by_symbol.get(market_symbol) or {}
        mark_price = parse_float(ticker.get("markPrice"))
        index_price = parse_float(ticker.get("indexPrice"))
        append_negative_candidate(
            candidates,
            {
                "exchange": "bg",
                "symbol": symbol,
                "fundingRate": parse_float(row.get("fundingRate")),
                "premiumRate": computed_premium(mark_price, index_price),
                "volume24h": parse_float(ticker.get("quoteVolume")) or parse_float(ticker.get("usdtVolume")),
                "periodHours": first_float(row.get("fundingRateInterval"), row.get("fundingRateIntervalHour"), 8),
                "fundingUpdatedAt": parse_timestamp_ms(row.get("nextUpdate")) or datetime.now(timezone.utc),
            },
        )
    return candidates


def fetch_okx_live_usdt_swaps(client: httpx.Client) -> list[dict[str, Any]]:
    global _okx_swap_instruments_cache
    now = datetime.now(timezone.utc)
    with _okx_funding_scan_lock:
        cached = _okx_swap_instruments_cache
        if cached and (now - cached[0]).total_seconds() < FS_OKX_INSTRUMENT_CACHE_SECONDS:
            return deepcopy(cached[1])
    rows = okx_rows(request_json(client, f"{base_url('okx')}/api/v5/public/instruments", {"instType": "SWAP"}))
    live = [
        row
        for row in rows
        if str(row.get("instId") or "").endswith("-USDT-SWAP")
        and str(row.get("state") or "live") == "live"
    ]
    with _okx_funding_scan_lock:
        _okx_swap_instruments_cache = (now, deepcopy(live))
    return live


def select_okx_funding_scan_instruments(
    instruments: list[dict[str, Any]],
    ticker_by_instrument: dict[str, dict[str, Any]],
    previous_negative: set[str],
    *,
    active_limit: int,
    rotation_limit: int,
    rotation_cursor: int,
    max_contracts: int,
) -> tuple[list[str], int]:
    """Prioritize liquid and previously-negative swaps, then rotate the long tail."""
    available = sorted(
        {
            str(row.get("instId") or "")
            for row in instruments
            if str(row.get("instId") or "").endswith("-USDT-SWAP")
        }
    )
    available_set = set(available)
    previous = sorted(available_set.intersection(previous_negative))
    active = sorted(
        available,
        key=lambda inst_id: (
            -(first_float((ticker_by_instrument.get(inst_id) or {}).get("volCcy24h")) or 0.0),
            inst_id,
        ),
    )[:active_limit]
    priority = list(dict.fromkeys([*previous, *active]))
    remaining = [inst_id for inst_id in available if inst_id not in set(priority)]
    rotating: list[str] = []
    next_cursor = 0
    if remaining and rotation_limit > 0:
        start = rotation_cursor % len(remaining)
        take = min(rotation_limit, len(remaining))
        rotating = [remaining[(start + index) % len(remaining)] for index in range(take)]
        next_cursor = (start + take) % len(remaining)
    selected = list(dict.fromkeys([*priority, *rotating]))[:max_contracts]
    return selected, next_cursor


def fetch_okx_realtime_negative_funding_candidates(client: httpx.Client) -> list[dict[str, Any]]:
    global _okx_previous_negative_instruments, _okx_rotation_cursor
    instruments = fetch_okx_live_usdt_swaps(client)
    ticker_rows = okx_rows(request_json(client, f"{base_url('okx')}/api/v5/market/tickers", {"instType": "SWAP"}))
    ticker_by_instrument = {
        str(row.get("instId") or ""): row
        for row in ticker_rows
        if isinstance(row, dict) and str(row.get("instId") or "").endswith("-USDT-SWAP")
    }
    with _okx_funding_scan_lock:
        selected, next_cursor = select_okx_funding_scan_instruments(
            instruments,
            ticker_by_instrument,
            set(_okx_previous_negative_instruments),
            active_limit=fs_okx_active_contracts_per_scan(),
            rotation_limit=fs_okx_rotation_contracts_per_scan(),
            rotation_cursor=_okx_rotation_cursor,
            max_contracts=fs_okx_max_contracts_per_scan(),
        )
        previous_negative = set(_okx_previous_negative_instruments)
        _okx_rotation_cursor = next_cursor

    funding_by_instrument: dict[str, dict[str, Any]] = {}
    failed_instruments: set[str] = set()

    def fetch_one(inst_id: str) -> tuple[str, dict[str, Any]]:
        payload = request_json(
            client,
            f"{base_url('okx')}/api/v5/public/funding-rate",
            {"instId": inst_id},
        )
        return inst_id, okx_data(payload)

    worker_count = min(4, len(selected) or 1)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(fetch_one, inst_id): inst_id for inst_id in selected}
        for future in as_completed(futures):
            inst_id = futures[future]
            try:
                fetched_inst_id, funding = future.result()
                funding_by_instrument[fetched_inst_id] = funding
            except Exception:
                failed_instruments.add(inst_id)

    candidates: list[dict[str, Any]] = []
    negative_instruments: set[str] = set()
    for inst_id in selected:
        funding = funding_by_instrument.get(inst_id)
        if not funding:
            continue
        symbol = inst_id[: -len("-USDT-SWAP")].replace("-", "").upper()
        if not symbol:
            continue
        before_count = len(candidates)
        append_negative_candidate(
            candidates,
            {
                "exchange": "okx",
                "symbol": symbol,
                "fundingRate": parse_float(funding.get("fundingRate")),
                "premiumRate": parse_float(funding.get("premium")),
                "volume24h": first_float((ticker_by_instrument.get(inst_id) or {}).get("volCcy24h")),
                "periodHours": okx_period_hours(funding) or 8,
                "fundingUpdatedAt": parse_timestamp_ms(funding.get("fundingTime"))
                or parse_timestamp_ms(funding.get("nextFundingTime"))
                or datetime.now(timezone.utc),
            },
        )
        if len(candidates) > before_count:
            negative_instruments.add(inst_id)
    with _okx_funding_scan_lock:
        # A failed previously-negative route stays prioritized until one clean
        # response confirms that it is no longer negative.
        _okx_previous_negative_instruments = negative_instruments | (previous_negative & failed_instruments)
        _okx_funding_scan_state.update(
            {
                "availableCount": len(instruments),
                "selectedCount": len(selected),
                "succeededCount": len(funding_by_instrument),
                "failedCount": len(failed_instruments),
                "negativeCount": len(negative_instruments),
                "coverageMode": "priority_rotation",
                "updatedAt": datetime.now(timezone.utc),
            }
        )
    if selected and not funding_by_instrument:
        raise ValueError("OKX 资金费批量轮询全部失败")
    return candidates


def fs_candidate_source_cached(exchange: str, now: datetime | None = None) -> list[dict[str, Any]]:
    checked_at = now or datetime.now(timezone.utc)
    with _fs_candidate_source_lock:
        cached = _fs_candidate_source_cache.get(exchange)
        if not cached or (checked_at - cached[0]).total_seconds() > fs_candidate_source_stale_seconds():
            return []
        return deepcopy(cached[1])


def fs_candidate_source_blocked(exchange: str, now_monotonic: float | None = None) -> bool:
    checked_at = time.monotonic() if now_monotonic is None else now_monotonic
    with _fs_candidate_source_lock:
        failure = _fs_candidate_source_failures.get(exchange)
        return bool(failure and checked_at < failure[1])


def record_fs_candidate_source_success(exchange: str, candidates: list[dict[str, Any]]) -> None:
    with _fs_candidate_source_lock:
        _fs_candidate_source_cache[exchange] = (datetime.now(timezone.utc), deepcopy(candidates))
        _fs_candidate_source_failures.pop(exchange, None)


def record_fs_candidate_source_failure(exchange: str) -> None:
    with _fs_candidate_source_lock:
        previous_count = (_fs_candidate_source_failures.get(exchange) or (0, 0.0))[0]
        failure_count = previous_count + 1
        base = fs_candidate_source_failure_cooldown_seconds()
        cooldown = min(300, base * (2 ** min(failure_count - 1, 3)))
        _fs_candidate_source_failures[exchange] = (failure_count, time.monotonic() + cooldown)


def fs_candidate_source_scan_status() -> dict[str, Any]:
    now = time.monotonic()
    with _fs_candidate_source_lock:
        failures = {
            exchange: {
                "failureCount": failure_count,
                "cooldownSeconds": max(0.0, round(blocked_until - now, 1)),
            }
            for exchange, (failure_count, blocked_until) in _fs_candidate_source_failures.items()
            if blocked_until > now
        }
    with _okx_funding_scan_lock:
        okx_state = deepcopy(_okx_funding_scan_state)
    return {"okx": okx_state, "failedSources": failures}


def realtime_negative_funding_candidates(exchanges: list[str], limit: int = 500) -> list[dict[str, Any]]:
    normalized = [normalize_exchange(exchange) for exchange in exchanges]
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    with http_client() as client:
        fetchers = {
            "bn": fetch_binance_realtime_negative_funding_candidates,
            "by": fetch_bybit_realtime_negative_funding_candidates,
            "gt": fetch_gate_realtime_negative_funding_candidates,
            "bg": fetch_bitget_realtime_negative_funding_candidates,
            "okx": fetch_okx_realtime_negative_funding_candidates,
        }
        scheduled: list[tuple[str, Any]] = []
        for exchange in normalized:
            fetcher = fetchers.get(exchange)
            if fetcher is None:
                continue
            if fs_candidate_source_blocked(exchange):
                cached = fs_candidate_source_cached(exchange)
                if cached:
                    candidates.extend(cached)
                else:
                    errors.append(f"{exchange}: 失败源冷却中")
                continue
            scheduled.append((exchange, fetcher))
        with ThreadPoolExecutor(max_workers=min(3, len(scheduled) or 1)) as executor:
            futures = {executor.submit(fetcher, client): exchange for exchange, fetcher in scheduled}
            for future in as_completed(futures):
                exchange = futures[future]
                try:
                    source_candidates = future.result()
                    record_fs_candidate_source_success(exchange, source_candidates)
                    candidates.extend(source_candidates)
                except Exception as exc:
                    record_fs_candidate_source_failure(exchange)
                    cached = fs_candidate_source_cached(exchange)
                    if cached:
                        candidates.extend(cached)
                    errors.append(f"{exchange}: {exc}")
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        key = (str(item.get("exchange") or ""), str(item.get("symbol") or ""))
        if key[0] and key[1]:
            latest[key] = item
    result = sorted(latest.values(), key=fs_potential_sort_key)[: max(1, min(limit, 1000))]
    if errors and not result:
        raise ValueError("实时负费率读取失败：" + "；".join(errors[:5]))
    return result


def rate_pct_text(value: float | None) -> str:
    return f"{value * 100:.3f}%" if value is not None else "--"


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def fs_fee_rate() -> float:
    return max(0.0, env_float("FS_SIGNAL_FEE_RATE", FS_SIGNAL_FEE_RATE))


def fs_slippage_rate() -> float:
    return max(0.0, env_float("FS_SIGNAL_SLIPPAGE_RATE", FS_SIGNAL_SLIPPAGE_RATE))


def fs_basis_risk_rate(basis_risk: str | None) -> float:
    if basis_risk == "danger":
        return max(0.0, env_float("FS_SIGNAL_BASIS_DANGER_RISK_RATE", FS_SIGNAL_BASIS_DANGER_RISK_RATE))
    if basis_risk == "watch":
        return max(0.0, env_float("FS_SIGNAL_BASIS_WATCH_RISK_RATE", FS_SIGNAL_BASIS_WATCH_RISK_RATE))
    return 0.0


def fs_realtime_max_contracts_per_exchange() -> int:
    return max(20, min(env_int("FS_REALTIME_MAX_CONTRACTS_PER_EXCHANGE", FS_REALTIME_MAX_CONTRACTS_PER_EXCHANGE), 600))


def fs_okx_max_contracts_per_scan() -> int:
    return max(20, min(env_int("FS_OKX_MAX_CONTRACTS_PER_SCAN", FS_OKX_MAX_CONTRACTS_PER_SCAN), 800))


def fs_okx_active_contracts_per_scan() -> int:
    return max(20, min(env_int("FS_OKX_ACTIVE_CONTRACTS_PER_SCAN", FS_OKX_ACTIVE_CONTRACTS_PER_SCAN), 300))


def fs_okx_rotation_contracts_per_scan() -> int:
    return max(20, min(env_int("FS_OKX_ROTATION_CONTRACTS_PER_SCAN", FS_OKX_ROTATION_CONTRACTS_PER_SCAN), 300))


def fs_candidate_source_stale_seconds() -> int:
    return max(60, min(env_int("FS_CANDIDATE_SOURCE_STALE_SECONDS", FS_CANDIDATE_SOURCE_STALE_SECONDS), 3600))


def fs_candidate_source_failure_cooldown_seconds() -> int:
    return max(
        15,
        min(
            env_int(
                "FS_CANDIDATE_SOURCE_FAILURE_COOLDOWN_SECONDS",
                FS_CANDIDATE_SOURCE_FAILURE_COOLDOWN_SECONDS,
            ),
            300,
        ),
    )


def fs_signal_max_minutes_to_funding() -> int:
    return max(1, env_int("FS_SIGNAL_MAX_MINUTES_TO_FUNDING", FS_SIGNAL_MAX_MINUTES_TO_FUNDING))


def fs_signal_watch_min_negative_rate() -> float:
    return max(0.0, env_float("FS_SIGNAL_WATCH_MIN_NEGATIVE_RATE", FS_SIGNAL_WATCH_MIN_NEGATIVE_RATE))


def fs_signal_watch_min_negative_daily_rate() -> float:
    return max(0.0, env_float("FS_SIGNAL_WATCH_MIN_NEGATIVE_DAILY_RATE", FS_SIGNAL_WATCH_MIN_NEGATIVE_DAILY_RATE))


def fs_signal_large_spread_threshold() -> float:
    return max(0.0, env_float("FS_SIGNAL_LARGE_SPREAD_THRESHOLD", FS_SIGNAL_LARGE_SPREAD_THRESHOLD))


def fs_signal_large_spread(spread_rate: Any) -> bool:
    spread = first_float(spread_rate)
    return spread is not None and abs(spread) >= fs_signal_large_spread_threshold()


def fs_daily_funding_rate(funding_rate: Any, period_hours: Any) -> float | None:
    rate = first_float(funding_rate)
    hours = first_float(period_hours)
    if rate is None or hours is None or hours <= 0:
        return None
    return rate * 24 / hours


def fs_funding_sort_rate(item: dict[str, Any]) -> float:
    daily_rate = item.get("dailyFundingRate")
    if isinstance(daily_rate, (int, float)):
        return daily_rate
    calculated = fs_daily_funding_rate(item.get("fundingRate"), item.get("periodHours"))
    if calculated is not None:
        return calculated
    rate = item.get("fundingRate")
    return rate if isinstance(rate, (int, float)) else 0


def fs_potential_sort_key(item: dict[str, Any]) -> tuple[int, float, float]:
    potential_type = str(item.get("potentialType") or "")
    current_negative_rank = 0 if potential_type == "current_negative" else 1
    funding_rate = fs_funding_sort_rate(item)
    premium_rate = first_float(item.get("premiumRate"))
    return current_negative_rank, funding_rate, premium_rate if premium_rate is not None else 0.0


def fs_watch_threshold_met(funding_rate: Any, period_hours: Any) -> bool:
    daily_rate = fs_daily_funding_rate(funding_rate, period_hours)
    return daily_rate is not None and daily_rate <= -fs_signal_watch_min_negative_daily_rate()


def fs_watch_reason(funding_rate: Any, period_hours: Any) -> str:
    period = first_float(period_hours)
    period_text = f"{period:g}h" if period else "--"
    return (
        f"日化负费率达到观察阈值 {rate_pct_text(-fs_signal_watch_min_negative_daily_rate())}"
        f"（本期 {rate_pct_text(first_float(funding_rate))} / {period_text}），BG 可借，仅观察不推送。"
    )


def fs_borrow_value_block_reason(amount: Any, value: Any) -> str | None:
    borrow_amount = parse_float(amount)
    if borrow_amount is None or borrow_amount <= 0:
        return f"BG 未返回可借数量，不推送（需 > {MIN_BORROWABLE_VALUE_USDT:g}U）。"
    borrow_value = parse_float(value)
    if borrow_value is None:
        return f"BG 可借价值暂不可计算，不推送（需 > {MIN_BORROWABLE_VALUE_USDT:g}U）。"
    if borrow_value <= MIN_BORROWABLE_VALUE_USDT:
        return f"BG 可借价值约 {borrow_value:g}U，未超过 {MIN_BORROWABLE_VALUE_USDT:g}U，不推送。"
    return None


def fs_push_precheck_reason(
    signal: dict[str, Any],
    spot_exchange: str,
    borrow_check: MarginShortCheck,
    current_funding_rate: Any,
    period_hours: Any,
    current_net: Any,
) -> str | None:
    if normalize_exchange(spot_exchange) != FS_SIGNAL_SPOT_EXCHANGE:
        return "只推送 BG 可借币。"
    if borrow_check.status != "ok" or not borrow_check.can_borrow:
        return "BG 当前不可借，不推送。"
    borrow_block_reason = fs_borrow_value_block_reason(borrow_check.borrowable_amount, borrow_check.borrowable_value_usdt)
    if borrow_block_reason:
        return borrow_block_reason
    if not fs_watch_threshold_met(current_funding_rate, period_hours):
        return f"日化负费率未达到观察阈值 {rate_pct_text(-fs_signal_watch_min_negative_daily_rate())}。"
    if signal.get("basisRisk") != "normal":
        return signal.get("basisMessage") or "基差未处于优先区间，只观察不推送。"
    if current_net is None or current_net <= FS_SIGNAL_NET_THRESHOLD:
        return f"当前净费率未超过 {rate_pct_text(FS_SIGNAL_NET_THRESHOLD)}。"
    return None


def fs_signal_futures_exchanges() -> tuple[str, ...]:
    raw = os.environ.get("FS_SIGNAL_FUTURES_EXCHANGES", "")
    if not raw.strip():
        return FS_SIGNAL_FUTURES_EXCHANGES
    exchanges = tuple(
        exchange
        for exchange in (normalize_exchange(part.strip()) for part in raw.split(","))
        if exchange in {"bn", "by", "gt", "bg", "okx"}
    )
    return exchanges or FS_SIGNAL_FUTURES_EXCHANGES


def paused_fs_check(exchange: str) -> dict[str, Any]:
    return {
        "exchange": exchange,
        "status": "paused",
        "message": "接口已保留，当前阶段暂停扫描。",
        "canBorrow": None,
        "borrowableAmount": None,
        "dailyBorrowRate": None,
        "borrowPeriodRate": None,
    }


def latest_fs_signal_log(db: Session, signal_key: str) -> CryptoFsSignalLog | None:
    return db.scalar(
        select(CryptoFsSignalLog)
        .where(CryptoFsSignalLog.signal_key == signal_key)
        .order_by(desc(CryptoFsSignalLog.created_at))
        .limit(1)
    )


def fs_signal_log_to_out(log: CryptoFsSignalLog | None) -> dict[str, Any] | None:
    if log is None:
        return None
    return {
        "id": log.id,
        "signalKey": log.signal_key,
        "symbol": log.symbol,
        "futuresExchange": log.futures_exchange,
        "spotExchange": log.spot_exchange,
        "fundingTime": log.funding_time,
        "currentFundingRate": log.current_funding_rate,
        "borrowPeriodRate": log.borrow_period_rate,
        "netFundingRate": log.net_funding_rate,
        "borrowableAmount": log.borrowable_amount,
        "status": log.status,
        "message": log.message,
        "pushed": bool(log.pushed),
        "createdAt": log.created_at,
    }


def fs_runtime_scan_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def add_fs_runtime_log(
    db: Session,
    scan_id: str,
    event_type: str,
    *,
    level: str = "info",
    stage: str = "scan",
    symbol: str | None = None,
    futures_exchange: str | None = None,
    spot_exchange: str | None = None,
    status: str = "ok",
    message: str | None = None,
    duration_ms: float | None = None,
    details: dict[str, Any] | None = None,
) -> CryptoFsRuntimeLog:
    log = CryptoFsRuntimeLog(
        scan_id=scan_id,
        event_type=event_type,
        level=level,
        stage=stage,
        symbol=normalize_symbol(symbol) if symbol else None,
        futures_exchange=normalize_exchange(futures_exchange) if futures_exchange else None,
        spot_exchange=normalize_exchange(spot_exchange) if spot_exchange else None,
        status=status,
        message=str(message)[:4000] if message else None,
        duration_ms=round(float(duration_ms), 1) if duration_ms is not None else None,
        details_json=json.dumps(details or {}, ensure_ascii=False, default=str),
    )
    db.add(log)
    db.flush()
    return log


def fs_runtime_log_to_out(log: CryptoFsRuntimeLog) -> dict[str, Any]:
    try:
        details = json.loads(log.details_json or "{}")
    except json.JSONDecodeError:
        details = {}
    return {
        "id": log.id,
        "scanId": log.scan_id,
        "eventType": log.event_type,
        "level": log.level,
        "stage": log.stage,
        "symbol": log.symbol,
        "futuresExchange": log.futures_exchange,
        "spotExchange": log.spot_exchange,
        "status": log.status,
        "message": log.message,
        "durationMs": log.duration_ms,
        "details": details,
        "createdAt": log.created_at,
    }


def fs_runtime_logs_overview(
    db: Session,
    limit: int = 200,
    level: str | None = None,
    scan_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    query = select(CryptoFsRuntimeLog)
    if level:
        query = query.where(CryptoFsRuntimeLog.level == str(level).strip().lower())
    if scan_id:
        query = query.where(CryptoFsRuntimeLog.scan_id == str(scan_id).strip())
    if symbol:
        query = query.where(CryptoFsRuntimeLog.symbol == normalize_symbol(symbol))
    bounded_limit = max(1, min(int(limit), 1000))
    rows = list(db.scalars(query.order_by(desc(CryptoFsRuntimeLog.created_at), desc(CryptoFsRuntimeLog.id)).limit(bounded_limit)))
    return {
        "status": "ok",
        "count": len(rows),
        "items": [fs_runtime_log_to_out(row) for row in rows],
    }


def mark_interrupted_fs_runtime_scans(db: Session) -> int:
    terminal_scan_ids = select(CryptoFsRuntimeLog.scan_id).where(
        CryptoFsRuntimeLog.event_type.in_(("scan_completed", "scan_failed", "scan_interrupted"))
    )
    rows = list(
        db.scalars(
            select(CryptoFsRuntimeLog)
            .where(
                CryptoFsRuntimeLog.event_type == "scan_started",
                CryptoFsRuntimeLog.scan_id.not_in(terminal_scan_ids),
            )
            .order_by(desc(CryptoFsRuntimeLog.created_at), desc(CryptoFsRuntimeLog.id))
        )
    )
    now = datetime.now(timezone.utc)
    recorded = 0
    seen: set[str] = set()
    for row in rows:
        if row.scan_id in seen:
            continue
        seen.add(row.scan_id)
        duration_ms = None
        if row.created_at is not None:
            duration_ms = max(0.0, (now - utc_datetime(row.created_at)).total_seconds() * 1000)
        add_fs_runtime_log(
            db,
            row.scan_id,
            "scan_interrupted",
            level="error",
            stage="startup",
            status="interrupted",
            message="后台曾在扫描完成前退出，已标记为中断。",
            duration_ms=duration_ms,
            details={"startedAt": row.created_at},
        )
        recorded += 1
    if recorded:
        db.commit()
    return recorded


def fs_runtime_check_details(check: dict[str, Any]) -> dict[str, Any]:
    transfer = check.get("transferStatus")
    return {
        "status": check.get("status"),
        "message": check.get("message"),
        "canBorrow": check.get("canBorrow"),
        "inventoryAvailable": check.get("inventoryAvailable"),
        "borrowableAmount": check.get("borrowableAmount"),
        "borrowableValueUsdt": check.get("borrowableValueUsdt"),
        "hourlyBorrowRate": check.get("hourlyBorrowRate"),
        "dailyBorrowRate": check.get("dailyBorrowRate"),
        "borrowPeriodRate": check.get("borrowPeriodRate"),
        "spotBid": check.get("spotBid"),
        "spotAsk": check.get("spotAsk"),
        "openSpreadRate": check.get("openSpreadRate"),
        "closeSpreadRate": check.get("closeSpreadRate"),
        "transferStatus": {
            "status": transfer.get("status"),
            "message": transfer.get("message"),
            "depositEnabled": transfer.get("depositEnabled"),
            "withdrawEnabled": transfer.get("withdrawEnabled"),
        }
        if isinstance(transfer, dict)
        else None,
    }


def record_fs_candidate_runtime_log(db: Session, scan_id: str, signal: dict[str, Any]) -> CryptoFsRuntimeLog:
    raw_checks = signal.get("checks") if isinstance(signal.get("checks"), dict) else {}
    checks = {
        normalize_exchange(str(exchange)): fs_runtime_check_details(check)
        for exchange, check in raw_checks.items()
        if isinstance(check, dict)
    }
    evaluation_failed = str(signal.get("reason") or "").startswith("FS 检测失败")
    error_message_markers = (
        "client error",
        "server error",
        "timeout",
        "timed out",
        "connecterror",
        "readerror",
        "ssl",
        "handshake",
        "connection refused",
        "连接失败",
        "请求超时",
    )
    has_check_error = any(
        str(check.get("status") or "") == "error"
        or any(marker in str(check.get("message") or "").lower() for marker in error_message_markers)
        for check in checks.values()
    )
    if evaluation_failed:
        status = "error"
        level = "error"
    elif signal.get("actionable"):
        status = "actionable"
        level = "info"
    elif signal.get("inventoryAvailable"):
        status = "borrowable"
        level = "info"
    elif has_check_error:
        status = "partial_error"
        level = "warning"
    else:
        status = "watch"
        level = "info"
    message = (
        signal.get("reason")
        or signal.get("watchReason")
        or signal.get("potentialReason")
        or ("检测完成，保留观察。" if status != "error" else "FS 检测失败。")
    )
    return add_fs_runtime_log(
        db,
        scan_id,
        "candidate_result",
        level=level,
        stage="candidate",
        symbol=str(signal.get("symbol") or ""),
        futures_exchange=str(signal.get("futuresExchange") or ""),
        spot_exchange=str(signal.get("spotExchange") or FS_SIGNAL_SPOT_EXCHANGE),
        status=status,
        message=str(message),
        duration_ms=first_float(signal.get("_runtimeDurationMs")),
        details={
            "fundingTime": signal.get("fundingTime"),
            "currentFundingRate": signal.get("currentFundingRate"),
            "dailyFundingRate": signal.get("dailyFundingRate"),
            "premiumRate": signal.get("premiumRate"),
            "periodHours": signal.get("periodHours"),
            "actionable": bool(signal.get("actionable")),
            "watchOnly": bool(signal.get("watchOnly")),
            "inventoryAvailable": bool(signal.get("inventoryAvailable")),
            "borrowableAmount": signal.get("borrowableAmount"),
            "borrowableValueUsdt": signal.get("borrowableValueUsdt"),
            "borrowDailyRate": signal.get("borrowDailyRate"),
            "netFundingRate": signal.get("netFundingRate"),
            "basisRate": signal.get("basisRate"),
            "openSpreadRate": signal.get("openSpreadRate"),
            "closeSpreadRate": signal.get("closeSpreadRate"),
            "checks": checks,
        },
    )


def fs_signal_key(symbol: str, futures_exchange: str, spot_exchange: str, funding_time: Any) -> str:
    parsed_funding_time = parse_datetime_value(funding_time)
    if parsed_funding_time is not None:
        funding_value = parsed_funding_time.isoformat()
    else:
        funding_value = str(funding_time or "")
    return f"{normalize_symbol(symbol)}:{normalize_exchange(futures_exchange)}:{normalize_exchange(spot_exchange)}:{funding_value}"


def fs_funding_history_ok(futures_exchange: str, symbol: str, borrow_period_rate: float) -> tuple[bool, list[dict[str, Any]], str | None]:
    try:
        history = fetch_funding_history(futures_exchange, symbol, 3)
    except Exception as exc:
        return False, [], f"近2期资金费读取失败：{exc}"
    previous = history[:2]
    rows: list[dict[str, Any]] = []
    for item in previous:
        net = fs_net_funding_rate(item.funding_rate, borrow_period_rate, fs_fee_rate(), fs_slippage_rate(), 0.0)
        rows.append(
            {
                "fundingRate": item.funding_rate,
                "fundingTime": item.funding_time,
                "netFundingRate": net,
            }
        )
    if len(rows) < 2:
        return False, rows, "近2期资金费不足。"
    if any(row["netFundingRate"] is None or row["netFundingRate"] <= 0 for row in rows):
        return False, rows, "近2期资金费扣除借币成本后不满足。"
    return True, rows, None


def fs_signal_push_body(signal: dict[str, Any]) -> str:
    history_text = " / ".join(rate_pct_text(row.get("fundingRate")) for row in signal.get("historyFunding", [])) or "--"
    borrow_amount = signal.get("borrowableAmount")
    borrow_text = f"{borrow_amount:g} {signal['symbol']}" if isinstance(borrow_amount, (int, float)) and borrow_amount > 0 else f"{signal['spotExchange']}支持借，额度以App可开为准"
    basis_text = rate_pct_text(signal.get("basisRate"))
    basis_message = signal.get("basisMessage") or fs_basis_message(signal.get("basisRate"))
    return (
        f"{signal['symbol']} {signal['spotExchange']}/现货借币 -> {signal['futuresExchange']}/合约；"
        f"当前资金费 {rate_pct_text(signal.get('currentFundingRate'))}，"
        f"近2期 {history_text}，"
        f"借币 {borrow_text}，"
        f"日息 {rate_pct_text(signal.get('borrowDailyRate'))}，"
        f"周期成本 {rate_pct_text(signal.get('borrowPeriodRate'))}，"
        f"手续费 {rate_pct_text(signal.get('feeRate'))}，"
        f"滑点 {rate_pct_text(signal.get('slippageRate'))}，"
        f"基差风险扣减 {rate_pct_text(signal.get('basisRiskRate'))}，"
        f"净费率 {rate_pct_text(signal.get('netFundingRate'))}，"
        f"基差 {basis_text}（{basis_message}），"
        f"24h额 {signal.get('volume24h') or '--'}。手续费和滑点未硬过滤。"
    )


def add_fs_signal_log(
    db: Session,
    signal: dict[str, Any],
    status: str,
    message: str | None,
    pushed: bool,
) -> CryptoFsSignalLog:
    log = CryptoFsSignalLog(
        signal_key=signal["signalKey"],
        symbol=signal["symbol"],
        futures_exchange=signal["futuresExchange"],
        spot_exchange=signal["spotExchange"],
        funding_time=signal.get("fundingTime"),
        current_funding_rate=signal.get("currentFundingRate"),
        borrow_period_rate=signal.get("borrowPeriodRate"),
        net_funding_rate=signal.get("netFundingRate"),
        borrowable_amount=signal.get("borrowableAmount"),
        status=status,
        message=message,
        pushed=pushed,
    )
    db.add(log)
    db.flush()
    return log


def maybe_push_fs_signal(db: Session, signal: dict[str, Any], push: bool) -> tuple[bool, dict[str, Any] | None]:
    existing = latest_fs_signal_log(db, signal["signalKey"])
    signal["lastLog"] = fs_signal_log_to_out(existing)
    if not push or not signal.get("actionable") or signal.get("watchOnly"):
        return False, signal["lastLog"]
    now = datetime.now(timezone.utc)
    if existing and existing.created_at:
        elapsed = (now - utc_datetime(existing.created_at)).total_seconds() / 60
        if elapsed < FS_SIGNAL_COOLDOWN_MINUTES:
            signal["pushed"] = bool(existing.pushed)
            signal["pushStatus"] = "skipped"
            signal["pushMessage"] = f"同一信号 {FS_SIGNAL_COOLDOWN_MINUTES} 分钟内已推送。"
            return bool(existing.pushed), signal["lastLog"]
    title = f"FS机会 {signal['symbol']} 净费率 {rate_pct_text(signal.get('netFundingRate'))}"
    body = fs_signal_push_body(signal)
    status, message = send_bark_or_log(
        enabled=True,
        title=title,
        body=body,
        group="B圈FS机会",
        url=None,
        disabled_message="FS 推送未启用。",
    )
    log = add_fs_signal_log(db, signal, status, message, status == "ok")
    signal["pushStatus"] = status
    signal["pushMessage"] = message
    signal["lastLog"] = fs_signal_log_to_out(log)
    return status == "ok", signal["lastLog"]


def pending_fs_checks() -> dict[str, dict[str, Any]]:
    return {exchange: {"exchange": exchange, "status": "pending", "message": "等待检测。"} for exchange in FS_SIGNAL_EXCHANGE_CHECKS}


def fs_borrow_cache_seconds() -> int:
    return max(1, env_int("FS_BORROW_CACHE_SECONDS", FS_BORROW_CACHE_SECONDS))


def fs_borrow_cache_key(symbol: str, exchange: str = FS_SIGNAL_SPOT_EXCHANGE) -> tuple[str, str]:
    return normalize_exchange(exchange), normalize_symbol(symbol)


def fs_borrow_search_response(
    symbol: str,
    exchange: str,
    payload: dict[str, Any],
    checked_at: datetime,
    cached: bool,
    mapped_symbol: str | None = None,
    source: str = "search",
) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_mapped = normalize_symbol(mapped_symbol or normalized_symbol)
    ttl_seconds = fs_borrow_cache_seconds()
    result = {
        **payload,
        "symbol": normalized_symbol,
        "exchange": normalized_exchange,
        "mappedSymbol": normalized_mapped,
        "cached": cached,
        "checkedAt": checked_at,
        "expiresAt": checked_at + timedelta(seconds=ttl_seconds),
        "source": source,
    }
    result.setdefault("status", "error")
    result.setdefault("message", "可借状态未返回。")
    result.setdefault("canBorrow", False)
    result.setdefault("borrowableAmount", None)
    result.setdefault("borrowableValueUsdt", None)
    result.setdefault("hourlyBorrowRate", None)
    result.setdefault("dailyBorrowRate", None)
    return result


def read_fs_borrow_cache(symbol: str, exchange: str = FS_SIGNAL_SPOT_EXCHANGE) -> dict[str, Any] | None:
    key = fs_borrow_cache_key(symbol, exchange)
    now = datetime.now(timezone.utc)
    with _fs_borrow_cache_lock:
        cached = _fs_borrow_cache.get(key)
        if not cached:
            return None
        checked_at, payload = cached
        if (now - checked_at).total_seconds() >= fs_borrow_cache_seconds():
            _fs_borrow_cache.pop(key, None)
            return None
    return fs_borrow_search_response(
        key[1],
        key[0],
        payload,
        checked_at,
        cached=True,
        mapped_symbol=payload.get("mappedSymbol"),
        source=str(payload.get("source") or "cache"),
    )


def write_fs_borrow_cache(
    symbol: str,
    exchange: str,
    payload: dict[str, Any],
    mapped_symbol: str | None = None,
    source: str = "search",
) -> dict[str, Any]:
    normalized_exchange, normalized_symbol = fs_borrow_cache_key(symbol, exchange)
    normalized_mapped = normalize_symbol(mapped_symbol or normalized_symbol)
    checked_at = datetime.now(timezone.utc)
    stored = {
        **payload,
        "symbol": normalized_symbol,
        "exchange": normalized_exchange,
        "mappedSymbol": normalized_mapped,
        "source": source,
    }
    with _fs_borrow_cache_lock:
        _fs_borrow_cache[(normalized_exchange, normalized_symbol)] = (checked_at, stored)
    return fs_borrow_search_response(
        normalized_symbol,
        normalized_exchange,
        stored,
        checked_at,
        cached=False,
        mapped_symbol=normalized_mapped,
        source=source,
    )


def fs_borrow_search_overview(db: Session, symbol: str, refresh: bool = False) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    exchange = FS_SIGNAL_SPOT_EXCHANGE
    if not refresh:
        cached = read_fs_borrow_cache(normalized_symbol, exchange)
        if cached:
            return cached

    if is_delisted_crypto_symbol(normalized_symbol):
        payload = margin_short_check_to_out(
            unavailable_margin_short_check(exchange, normalized_symbol, "not_supported", delisted_crypto_symbol_reason(normalized_symbol) or "下架币已排除。")
        ) or {}
        return write_fs_borrow_cache(normalized_symbol, exchange, payload, mapped_symbol=normalized_symbol)
    block_reason = asset_alias_scan_block_reason(normalized_symbol)
    if block_reason:
        payload = margin_short_check_to_out(
            unavailable_margin_short_check(exchange, normalized_symbol, "manual_review", block_reason)
        ) or {}
        return write_fs_borrow_cache(normalized_symbol, exchange, payload, mapped_symbol=normalized_symbol)

    request_symbol, price_ratio = mapped_symbol_and_ratio_for(db, normalized_symbol, exchange, "spot")
    spot_quote = adjust_quote_price_ratio(fetch_market(exchange, request_symbol, "spot"), price_ratio, normalized_symbol)
    if spot_quote.status != "ok" or spot_quote.best_bid is None or spot_quote.best_ask is None:
        payload = {
            "status": "not_supported",
            "message": fs_spot_quote_failure_message(exchange, spot_quote.error),
            "canBorrow": False,
            "borrowableAmount": None,
            "borrowableValueUsdt": None,
            "hourlyBorrowRate": None,
            "dailyBorrowRate": None,
            "spotBid": spot_quote.best_bid,
            "spotAsk": spot_quote.best_ask,
            "spotVolume24h": spot_quote.volume_24h,
        }
        return write_fs_borrow_cache(normalized_symbol, exchange, payload, mapped_symbol=request_symbol)

    borrow_check = fetch_margin_short_check(exchange, request_symbol)
    if borrow_check.can_borrow is True:
        borrow_check = apply_min_borrowable_value_threshold(
            borrow_check,
            quote_reference_price_usdt(spot_quote.best_bid, spot_quote.best_ask, spot_quote.mark_price, spot_quote.index_price),
        )
    payload = {
        **(margin_short_check_to_out(borrow_check) or {}),
        "spotBid": spot_quote.best_bid,
        "spotAsk": spot_quote.best_ask,
        "spotVolume24h": spot_quote.volume_24h,
    }
    return write_fs_borrow_cache(normalized_symbol, exchange, payload, mapped_symbol=request_symbol)


def fs_symbol_mismatch_reason(symbol: str, futures_exchange: str, spot_exchange: str) -> str | None:
    normalized_symbol = normalize_symbol(symbol)
    futures = normalize_exchange(futures_exchange)
    spot = normalize_exchange(spot_exchange)
    return FS_SYMBOL_MISMATCH_EXCLUSIONS.get((normalized_symbol, futures, spot))


def fetch_fs_market_quote(exchange: str, symbol: str, market_type: str) -> MarketQuote:
    quote = fetch_market(exchange, symbol, market_type)
    if quote.status == "ok" and quote.best_bid is not None and quote.best_ask is not None:
        return quote
    return fetch_market(exchange, symbol, market_type)


def fs_spot_quote_failure_message(exchange: str, error: str | None) -> str:
    normalized_exchange = normalize_exchange(exchange)
    label = EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)
    raw_error = str(error or "").strip()
    if "400 bad request" in raw_error.lower():
        return f"{label} 现货未上线或无有效盘口。"
    return raw_error or f"{label} 现货未上线或无有效盘口。"


def evaluate_fs_spot_exchange(
    db: Session,
    symbol: str,
    futures_exchange: str,
    spot_exchange: str,
    period_hours: float | None,
    current_funding_rate: float | None,
    futures_bid: float | None = None,
    futures_ask: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    transfer_status_out: dict[str, Any] | None = None
    try:
        mismatch_reason = fs_symbol_mismatch_reason(symbol, futures_exchange, spot_exchange)
        if mismatch_reason:
            out = {
                "exchange": spot_exchange,
                "status": "not_supported",
                "message": mismatch_reason,
                "canBorrow": False,
            }
            write_fs_borrow_cache(symbol, spot_exchange, out, mapped_symbol=symbol, source="scan")
            return out, None
        request_symbol, price_ratio = mapped_symbol_and_ratio_for(db, symbol, spot_exchange, "spot")
        try:
            transfer_status = fetch_coin_transfer_status(spot_exchange, request_symbol, timeout=3.0)
        except Exception:
            transfer_status = None
        transfer_status_out = transfer_status_to_out(transfer_status) if transfer_status is not None else None
        if spot_exchange == "bn":
            spot_quote = adjust_quote_price_ratio(fetch_fs_market_quote(spot_exchange, request_symbol, "spot"), price_ratio, symbol)
            if spot_quote.status != "ok" or spot_quote.best_bid is None or spot_quote.best_ask is None:
                cached_out = cached_fs_spot_check_for_quote_failure(
                    symbol,
                    spot_exchange,
                    futures_bid,
                    futures_ask,
                    current_funding_rate,
                    spot_quote.error,
                )
                if cached_out is not None:
                    if transfer_status_out is not None:
                        cached_out["transferStatus"] = transfer_status_out
                    return cached_out, None
                out = {
                    "exchange": spot_exchange,
                    "status": "not_supported",
                    "message": fs_spot_quote_failure_message(spot_exchange, spot_quote.error),
                    "canBorrow": False,
                    "spotBid": spot_quote.best_bid,
                    "spotAsk": spot_quote.best_ask,
                    "spotVolume24h": spot_quote.volume_24h,
                    "transferStatus": transfer_status_out,
                }
                write_fs_borrow_cache(symbol, spot_exchange, out, mapped_symbol=request_symbol, source="scan")
                return out, None
            try:
                with http_client() as client:
                    borrow_check = fetch_binance_fs_margin_short_check(client, request_symbol)
            except Exception as exc:
                borrow_check = margin_short_exception_check(spot_exchange, request_symbol, exc)
            borrow_period = borrow_period_rate(borrow_check, period_hours)
            fee_rate = fs_fee_rate()
            slippage_rate = fs_slippage_rate()
            basis_rate = fs_basis_rate(futures_bid, spot_quote.best_ask)
            open_spread_rate = fs_astro_spread_rate(spot_quote.best_bid, futures_ask)
            close_spread_rate = fs_astro_spread_rate(spot_quote.best_ask, futures_bid)
            basis_risk = fs_basis_risk(basis_rate)
            basis_risk_rate = fs_basis_risk_rate(basis_risk)
            net_rate = fs_net_funding_rate(current_funding_rate, borrow_period, fee_rate, slippage_rate, basis_risk_rate)
            out = margin_short_check_to_out(borrow_check) or {
                "exchange": spot_exchange,
                "status": "error",
                "message": "Binance 可借库存未返回。",
                "canBorrow": False,
            }
            out.update(
                {
                    "borrowPeriodRate": borrow_period,
                    "netFundingRate": net_rate,
                    "basisRate": basis_rate,
                    "openSpreadRate": open_spread_rate,
                    "closeSpreadRate": close_spread_rate,
                    "basisRisk": basis_risk,
                    "basisMessage": fs_basis_message(basis_rate),
                    "feeRate": fee_rate,
                    "slippageRate": slippage_rate,
                    "basisRiskRate": basis_risk_rate,
                    "spotBid": spot_quote.best_bid,
                    "spotAsk": spot_quote.best_ask,
                    "spotVolume24h": spot_quote.volume_24h,
                    "transferStatus": transfer_status_out,
                }
            )
            write_fs_borrow_cache(symbol, spot_exchange, out, mapped_symbol=request_symbol, source="scan")
            if borrow_check.status != "ok" or not borrow_check.can_borrow:
                return out, None
            return (
                out,
                {
                    "exchange": spot_exchange,
                    "borrowCheck": borrow_check,
                    "borrowPeriodRate": borrow_period,
                    "netFundingRate": net_rate,
                    "basisRate": basis_rate,
                    "openSpreadRate": open_spread_rate,
                    "closeSpreadRate": close_spread_rate,
                    "basisRisk": basis_risk,
                    "basisMessage": fs_basis_message(basis_rate),
                    "feeRate": fee_rate,
                    "slippageRate": slippage_rate,
                    "basisRiskRate": basis_risk_rate,
                    "spotBid": spot_quote.best_bid,
                    "spotAsk": spot_quote.best_ask,
                    "transferStatus": transfer_status_out,
                },
            )
        spot_quote = adjust_quote_price_ratio(fetch_fs_market_quote(spot_exchange, request_symbol, "spot"), price_ratio, symbol)
        if spot_quote.status != "ok" or spot_quote.best_bid is None or spot_quote.best_ask is None:
            cached_out = cached_fs_spot_check_for_quote_failure(
                symbol,
                spot_exchange,
                futures_bid,
                futures_ask,
                current_funding_rate,
                spot_quote.error,
            )
            if cached_out is not None:
                if transfer_status_out is not None:
                    cached_out["transferStatus"] = transfer_status_out
                return cached_out, None
            out = {
                "exchange": spot_exchange,
                "status": "not_supported",
                "message": fs_spot_quote_failure_message(spot_exchange, spot_quote.error),
                "canBorrow": False,
                "spotBid": spot_quote.best_bid,
                "spotAsk": spot_quote.best_ask,
                "spotVolume24h": spot_quote.volume_24h,
                "transferStatus": transfer_status_out,
            }
            write_fs_borrow_cache(symbol, spot_exchange, out, mapped_symbol=request_symbol, source="scan")
            return out, None
        try:
            borrow_check = fetch_margin_short_check(spot_exchange, request_symbol)
        except Exception as exc:
            borrow_check = margin_short_exception_check(spot_exchange, request_symbol, exc)
        if borrow_check.can_borrow is True:
            borrow_check = apply_min_borrowable_value_threshold(
                borrow_check,
                quote_reference_price_usdt(spot_quote.best_bid, spot_quote.best_ask, spot_quote.mark_price, spot_quote.index_price),
            )
        out = {
            **(margin_short_check_to_out(borrow_check) or {}),
            "spotBid": spot_quote.best_bid,
            "spotAsk": spot_quote.best_ask,
            "spotVolume24h": spot_quote.volume_24h,
            "transferStatus": transfer_status_out,
        }
        basis_rate = fs_basis_rate(futures_bid, spot_quote.best_ask)
        open_spread_rate = fs_astro_spread_rate(spot_quote.best_bid, futures_ask)
        close_spread_rate = fs_astro_spread_rate(spot_quote.best_ask, futures_bid)
        basis_risk = fs_basis_risk(basis_rate)
        basis_risk_rate = fs_basis_risk_rate(basis_risk)
        fee_rate = fs_fee_rate()
        slippage_rate = fs_slippage_rate()
        out["basisRate"] = basis_rate
        out["openSpreadRate"] = open_spread_rate
        out["closeSpreadRate"] = close_spread_rate
        out["basisRisk"] = basis_risk
        out["basisMessage"] = fs_basis_message(basis_rate)
        out["feeRate"] = fee_rate
        out["slippageRate"] = slippage_rate
        out["basisRiskRate"] = basis_risk_rate
        borrow_period = borrow_period_rate(borrow_check, period_hours)
        out["borrowPeriodRate"] = borrow_period
        net_rate = fs_net_funding_rate(current_funding_rate, borrow_period, fee_rate, slippage_rate, basis_risk_rate)
        out["netFundingRate"] = net_rate
        write_fs_borrow_cache(symbol, spot_exchange, out, mapped_symbol=request_symbol, source="scan")
        if borrow_check.status != "ok" or not borrow_check.can_borrow:
            return out, None
        candidate = {
            "exchange": spot_exchange,
            "borrowCheck": borrow_check,
            "borrowPeriodRate": borrow_period,
            "netFundingRate": net_rate,
            "basisRate": basis_rate,
            "openSpreadRate": open_spread_rate,
            "closeSpreadRate": close_spread_rate,
            "basisRisk": basis_risk,
            "basisMessage": fs_basis_message(basis_rate),
            "feeRate": fee_rate,
            "slippageRate": slippage_rate,
            "basisRiskRate": basis_risk_rate,
            "spotBid": spot_quote.best_bid,
            "spotAsk": spot_quote.best_ask,
            "transferStatus": transfer_status_out,
        }
        return out, candidate
    except Exception as exc:
        out = {
            "exchange": spot_exchange,
            "status": "error",
            "message": f"{spot_exchange} 借币检测失败：{exc}",
            "canBorrow": False,
            "transferStatus": transfer_status_out,
        }
        try:
            write_fs_borrow_cache(symbol, spot_exchange, out, mapped_symbol=symbol, source="scan")
        except Exception:
            pass
        return out, None


def evaluate_fs_signal_candidate(
    db: Session,
    candidate: dict[str, Any],
    push: bool = False,
    check_exchanges: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    symbol = normalize_symbol(str(candidate.get("symbol") or ""))
    futures_exchange = normalize_exchange(str(candidate.get("exchange") or ""))
    spot_exchange = FS_SIGNAL_SPOT_EXCHANGE
    funding_time = candidate.get("fundingUpdatedAt")
    signal: dict[str, Any] = {
        "signalKey": fs_signal_key(symbol, futures_exchange, spot_exchange, funding_time),
        "symbol": symbol,
        "futuresExchange": futures_exchange,
        "spotExchange": spot_exchange,
        "fundingTime": funding_time,
        "currentFundingRate": candidate.get("fundingRate"),
        "dailyFundingRate": fs_daily_funding_rate(candidate.get("fundingRate"), candidate.get("periodHours") or candidate.get("fundingIntervalHours") or 8),
        "premiumRate": candidate.get("premiumRate"),
        "negativePotential": bool(candidate.get("negativePotential")),
        "potentialType": candidate.get("potentialType"),
        "potentialReason": candidate.get("potentialReason"),
        "volume24h": candidate.get("volume24h"),
        "checks": pending_fs_checks(),
        "historyFunding": [],
        "futuresBid": None,
        "futuresAsk": None,
        "openSpreadRate": None,
        "closeSpreadRate": None,
        "basisRate": None,
        "basisRisk": "unknown",
        "basisMessage": fs_basis_message(None),
        "feeRate": fs_fee_rate(),
        "slippageRate": fs_slippage_rate(),
        "basisRiskRate": 0.0,
        "actionable": False,
        "reason": None,
        "pushed": False,
        "pushStatus": None,
        "pushMessage": None,
        "lastLog": None,
        "watchOnly": bool(candidate.get("negativePotential")),
        "watchReason": candidate.get("potentialReason"),
    }
    if is_delisted_crypto_symbol(symbol):
        signal["reason"] = delisted_crypto_symbol_reason(symbol)
        return signal
    block_reason = asset_alias_scan_block_reason(symbol)
    if block_reason:
        signal["reason"] = block_reason
        return signal
    funding_window_ok = True
    funding_window_message: str | None = None
    funding_deadline = utc_datetime(parse_datetime_value(funding_time))
    if funding_deadline is not None:
        minutes_to_funding = (funding_deadline - datetime.now(timezone.utc)).total_seconds() / 60
        signal["minutesToFunding"] = minutes_to_funding
        if minutes_to_funding <= 0:
            funding_window_ok = False
            funding_window_message = "本轮资金费时间已过，保留观察并等待下一轮。"
        max_minutes = fs_signal_max_minutes_to_funding()
        if minutes_to_funding > max_minutes:
            funding_window_ok = False
            funding_window_message = f"离结算还有 {minutes_to_funding:.0f} 分钟，超过 {max_minutes} 分钟窗口，先观察。"
    signal["fundingWindowOk"] = funding_window_ok

    fallback_period_hours = first_float(candidate.get("periodHours"), candidate.get("fundingIntervalHours"))
    period_hours = fallback_period_hours
    current_funding_rate = signal["currentFundingRate"]
    futures_request_symbol = symbol
    try:
        futures_request_symbol, futures_price_ratio = mapped_symbol_and_ratio_for(db, symbol, futures_exchange, "futures")
        futures_quote = adjust_quote_price_ratio(
            fetch_fs_market_quote(futures_exchange, futures_request_symbol, "futures"),
            futures_price_ratio,
            symbol,
        )
        if futures_quote.status == "ok":
            period_hours = futures_quote.period_hours or period_hours
            current_funding_rate = futures_quote.funding_rate if futures_quote.funding_rate is not None else current_funding_rate
            signal["futuresBid"] = futures_quote.best_bid
            signal["futuresAsk"] = futures_quote.best_ask
    except Exception:
        pass
    period_hours = period_hours or 8
    signal.update(
        {
            "periodHours": period_hours,
            "currentFundingRate": current_funding_rate,
            "dailyFundingRate": fs_daily_funding_rate(current_funding_rate, period_hours),
        }
    )
    negative_potential, potential_type, potential_reason = fs_negative_potential(current_funding_rate, signal.get("premiumRate"))
    signal.update(
        {
            "negativePotential": negative_potential,
            "potentialType": potential_type,
            "potentialReason": potential_reason,
            "watchOnly": negative_potential,
            "watchReason": potential_reason,
        }
    )
    if not negative_potential:
        signal["reason"] = "当前资金费与溢价均已回到非负，候选条件暂时失效。"
        return signal

    candidates_by_spot: list[dict[str, Any]] = []
    for check_exchange in check_exchanges or FS_SIGNAL_SPOT_EXCHANGES:
        check_out, candidate_spot = evaluate_fs_spot_exchange(
            db,
            symbol,
            futures_exchange,
            check_exchange,
            period_hours,
            current_funding_rate,
            signal.get("futuresBid"),
            signal.get("futuresAsk"),
        )
        signal["checks"][check_exchange] = check_out
        if candidate_spot is not None:
            candidates_by_spot.append(candidate_spot)

    if not candidates_by_spot:
        primary_check = signal.get("checks", {}).get(FS_SIGNAL_SPOT_EXCHANGE)
        if isinstance(primary_check, dict):
            signal.update(
                {
                    "inventoryAvailable": primary_check.get("inventoryAvailable") is True,
                    "borrowableAmount": primary_check.get("borrowableAmount"),
                    "borrowableValueUsdt": primary_check.get("borrowableValueUsdt"),
                    "borrowDailyRate": primary_check.get("dailyBorrowRate"),
                    "borrowHourlyRate": primary_check.get("hourlyBorrowRate"),
                    "borrowPeriodRate": primary_check.get("borrowPeriodRate"),
                    "netFundingRate": primary_check.get("netFundingRate"),
                    "basisRate": primary_check.get("basisRate"),
                    "openSpreadRate": primary_check.get("openSpreadRate"),
                    "closeSpreadRate": primary_check.get("closeSpreadRate"),
                    "basisRisk": primary_check.get("basisRisk") or signal.get("basisRisk"),
                    "basisMessage": primary_check.get("basisMessage") or signal.get("basisMessage"),
                }
            )
            signal["reason"] = primary_check.get("message") or "当前没有真实新增 B 额度，保留潜在负费率观察。"
        else:
            signal["reason"] = "当前没有真实新增 B 额度，保留潜在负费率观察。"
        return signal

    calculable = [item for item in candidates_by_spot if item.get("netFundingRate") is not None]
    if not calculable:
        signal["reason"] = "有平台可借，但借币成本缺失，暂不推送。"
        best_display = max(candidates_by_spot, key=lambda item: item.get("borrowCheck").borrowable_amount or 0)
        borrow_check = best_display["borrowCheck"]
        signal.update(
            {
                "spotExchange": best_display["exchange"],
                "inventoryAvailable": bool(borrow_check.inventory_available if borrow_check.inventory_available is not None else borrow_check.can_borrow),
                "signalKey": fs_signal_key(symbol, futures_exchange, best_display["exchange"], funding_time),
                "borrowableAmount": borrow_check.borrowable_amount,
                "borrowableValueUsdt": borrow_check.borrowable_value_usdt,
                "borrowDailyRate": borrow_check.daily_borrow_rate,
                "borrowHourlyRate": borrow_check.hourly_borrow_rate,
                "borrowPeriodRate": None,
                "netFundingRate": None,
                "basisRate": best_display.get("basisRate"),
                "openSpreadRate": best_display.get("openSpreadRate"),
                "closeSpreadRate": best_display.get("closeSpreadRate"),
                "basisRisk": best_display.get("basisRisk"),
                "basisMessage": best_display.get("basisMessage"),
                "feeRate": best_display.get("feeRate"),
                "slippageRate": best_display.get("slippageRate"),
                "basisRiskRate": best_display.get("basisRiskRate"),
            }
        )
        if best_display["exchange"] == "bg" and bool(borrow_check.can_borrow) and fs_watch_threshold_met(current_funding_rate, period_hours):
            signal["watchOnly"] = True
            signal["watchReason"] = fs_watch_reason(current_funding_rate, period_hours)
        return signal

    best = max(calculable, key=lambda item: item.get("netFundingRate") or float("-inf"))
    spot_exchange = str(best["exchange"])
    borrow_check = best["borrowCheck"]
    borrow_period = best.get("borrowPeriodRate")
    current_net = best.get("netFundingRate")
    signal.update(
        {
            "spotExchange": spot_exchange,
            "inventoryAvailable": bool(borrow_check.inventory_available if borrow_check.inventory_available is not None else borrow_check.can_borrow),
            "signalKey": fs_signal_key(symbol, futures_exchange, spot_exchange, funding_time),
            "borrowableAmount": borrow_check.borrowable_amount,
            "borrowableValueUsdt": borrow_check.borrowable_value_usdt,
            "borrowDailyRate": borrow_check.daily_borrow_rate,
            "borrowHourlyRate": borrow_check.hourly_borrow_rate,
            "borrowPeriodRate": borrow_period,
            "netFundingRate": current_net,
            "basisRate": best.get("basisRate"),
            "openSpreadRate": best.get("openSpreadRate"),
            "closeSpreadRate": best.get("closeSpreadRate"),
            "basisRisk": best.get("basisRisk"),
            "basisMessage": best.get("basisMessage"),
            "feeRate": best.get("feeRate"),
            "slippageRate": best.get("slippageRate"),
            "basisRiskRate": best.get("basisRiskRate"),
        }
    )
    if spot_exchange == "bg" and bool(borrow_check.can_borrow) and fs_watch_threshold_met(current_funding_rate, period_hours):
        signal["watchOnly"] = True
        signal["watchReason"] = fs_watch_reason(current_funding_rate, period_hours)
    if signal.get("basisRisk") == "danger":
        signal["reason"] = signal.get("basisMessage") or "合约相对现货折价过深，只观察不推送。"
        return signal
    if not funding_window_ok:
        signal["reason"] = funding_window_message or "离结算过远，先观察。"
        return signal
    precheck_reason = fs_push_precheck_reason(signal, spot_exchange, borrow_check, current_funding_rate, period_hours, current_net)
    if precheck_reason:
        signal["reason"] = precheck_reason
        return signal

    if not push:
        signal["actionable"] = True
        signal["watchOnly"] = False
        signal["watchReason"] = None
        signal["reason"] = "当前净费率满足，待手动扫描确认近2期后推送。"
        maybe_push_fs_signal(db, signal, False)
        return signal

    history_ok, history_rows, history_reason = fs_funding_history_ok(
        futures_exchange,
        futures_request_symbol,
        borrow_period or 0,
    )
    signal["historyFunding"] = history_rows
    if not history_ok:
        signal["reason"] = history_reason
        return signal

    signal["actionable"] = True
    signal["watchOnly"] = False
    signal["watchReason"] = None
    signal["reason"] = "满足 FS 推送条件。"
    pushed, log = maybe_push_fs_signal(db, signal, push)
    signal["pushed"] = pushed
    signal["lastLog"] = log
    return signal


def select_fs_signal_scan_candidates(
    candidates: list[dict[str, Any]],
    known_borrowable: set[str],
    scan_limit: int,
) -> list[dict[str, Any]]:
    """Keep B-first coverage without dropping the strongest negative-funding opportunities."""
    normalized_limit = max(1, int(scan_limit))
    strongest = sorted(candidates, key=fs_potential_sort_key)
    borrow_first = sorted(
        candidates,
        key=lambda candidate: (
            0 if normalize_symbol(str(candidate.get("symbol") or "")) in known_borrowable else 1,
            *fs_potential_sort_key(candidate),
        ),
    )
    strongest_distinct_symbols: list[dict[str, Any]] = []
    strongest_symbols_seen: set[str] = set()
    for candidate in strongest:
        symbol = normalize_symbol(str(candidate.get("symbol") or ""))
        if not symbol or symbol in strongest_symbols_seen:
            continue
        strongest_symbols_seen.add(symbol)
        strongest_distinct_symbols.append(candidate)
    strongest_quota = min(normalized_limit, max(5, (normalized_limit + 1) // 2))
    borrowable_quota = normalized_limit - strongest_quota
    reserved_borrowable = [
        candidate
        for candidate in borrow_first
        if normalize_symbol(str(candidate.get("symbol") or "")) in known_borrowable
    ][:borrowable_quota]
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in (*reserved_borrowable, *strongest_distinct_symbols[:strongest_quota], *strongest, *borrow_first):
        key = (
            normalize_exchange(str(candidate.get("exchange") or "")),
            normalize_symbol(str(candidate.get("symbol") or "")),
        )
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= normalized_limit:
            break
    return selected


def canonicalize_fs_candidate_pool(db: Session, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inverse_mappings = inverse_symbol_mapping_lookup(db, "futures")
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        exchange = normalize_exchange(str(candidate.get("exchange") or ""))
        exchange_symbol = normalize_symbol(str(candidate.get("symbol") or ""))
        canonical_symbol = canonical_exchange_asset_symbol(
            exchange_symbol,
            exchange,
            "futures",
            inverse_mappings,
        )
        candidate["symbol"] = canonical_symbol
        if canonical_symbol != exchange_symbol:
            candidate["exchangeSymbol"] = exchange_symbol
        key = (exchange, canonical_symbol)
        previous = grouped.get(key)
        if previous is None or fs_potential_sort_key(candidate) < fs_potential_sort_key(previous):
            grouped[key] = candidate
    return list(grouped.values())


def canonicalize_fs_futures_market_symbols(
    db: Session,
    market_symbols: dict[str, set[str]],
) -> dict[str, set[str]]:
    inverse_mappings = inverse_symbol_mapping_lookup(db, "futures")
    return {
        exchange: {
            canonical_exchange_asset_symbol(symbol, exchange, "futures", inverse_mappings)
            for symbol in symbols
        }
        for exchange, symbols in market_symbols.items()
    }


def fetch_fs_futures_market_symbols(exchanges: list[str] | tuple[str, ...] | None = None) -> dict[str, set[str]]:
    global _fs_futures_market_cache
    selected_exchanges = (
        tuple(FS_SIGNAL_FUTURES_EXCHANGES)
        if exchanges is None
        else tuple(dict.fromkeys(normalize_exchange(exchange) for exchange in exchanges))
    )
    allowed_exchanges = set(FS_SIGNAL_FUTURES_EXCHANGES) | set(FUNDING_CAP_WATCH_EXCHANGES)
    selected_exchanges = tuple(
        exchange
        for exchange in selected_exchanges
        if exchange in allowed_exchanges
    )
    if not selected_exchanges:
        return {}
    now = datetime.now(timezone.utc)
    with _fs_futures_market_cache_lock:
        cached = _fs_futures_market_cache
        if (
            cached
            and (now - cached[0]).total_seconds() < 300
            and all(exchange in cached[1] for exchange in selected_exchanges)
        ):
            return {
                exchange: set(cached[1].get(exchange, set()))
                for exchange in selected_exchanges
            }
        previous = {exchange: set(symbols) for exchange, symbols in (cached[1] if cached else {}).items()}

    def fetch_one(exchange: str) -> tuple[str, set[str]]:
        last_error: Exception | None = None
        for _ in range(2):
            try:
                with http_client(timeout=12.0) as client:
                    return exchange, fetch_exchange_market_symbols(client, exchange, "futures")
            except Exception as exc:
                last_error = exc
        raise last_error or RuntimeError(f"{exchange} F 合约清单读取失败")

    market_symbols = previous
    with ThreadPoolExecutor(max_workers=len(selected_exchanges)) as executor:
        future_map = {
            executor.submit(fetch_one, exchange): exchange
            for exchange in selected_exchanges
        }
        for future in as_completed(future_map):
            try:
                exchange, symbols = future.result()
                market_symbols[exchange] = symbols
            except Exception:
                continue
    with _fs_futures_market_cache_lock:
        _fs_futures_market_cache = (now, market_symbols)
    return {
        exchange: set(market_symbols.get(exchange, set()))
        for exchange in selected_exchanges
    }


def fs_futures_routes_by_symbol(
    candidates: list[dict[str, Any]],
    market_symbols: dict[str, set[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    routes: dict[str, set[str]] = {}
    for candidate in candidates:
        symbol = normalize_symbol(str(candidate.get("symbol") or ""))
        exchange = normalize_exchange(str(candidate.get("exchange") or ""))
        if symbol and exchange:
            routes.setdefault(symbol, set()).add(exchange)
    if market_symbols:
        for symbol in tuple(routes):
            for exchange, symbols in market_symbols.items():
                if symbol in symbols:
                    routes[symbol].add(normalize_exchange(exchange))
    exchange_order = {exchange: index for index, exchange in enumerate(FS_SIGNAL_FUTURES_EXCHANGES)}
    return {
        symbol: tuple(sorted(exchanges, key=lambda exchange: (exchange_order.get(exchange, 99), exchange)))
        for symbol, exchanges in routes.items()
    }


def probe_missing_fs_futures_routes(
    routes: dict[str, tuple[str, ...]],
    candidates: list[dict[str, Any]],
    market_symbols: dict[str, set[str]],
) -> dict[str, tuple[str, ...]]:
    missing_exchanges = [
        exchange
        for exchange in FS_SIGNAL_FUTURES_EXCHANGES
        if exchange not in market_symbols
    ]
    symbols = sorted(
        {
            normalize_symbol(str(candidate.get("symbol") or ""))
            for candidate in candidates
            if str(candidate.get("symbol") or "").strip()
        }
    )
    if not missing_exchanges or not symbols:
        return routes
    route_sets = {symbol: set(exchanges) for symbol, exchanges in routes.items()}
    specs = [(exchange, symbol) for exchange in missing_exchanges for symbol in symbols]
    with ThreadPoolExecutor(max_workers=min(8, len(specs))) as executor:
        future_map = {
            executor.submit(fetch_fs_market_quote, exchange, symbol, "futures"): (exchange, symbol)
            for exchange, symbol in specs
        }
        for future in as_completed(future_map):
            exchange, symbol = future_map[future]
            try:
                quote = future.result()
            except Exception:
                continue
            if quote.status == "ok" and quote.best_bid is not None and quote.best_ask is not None:
                route_sets.setdefault(symbol, set()).add(exchange)
    exchange_order = {exchange: index for index, exchange in enumerate(FS_SIGNAL_FUTURES_EXCHANGES)}
    return {
        symbol: tuple(sorted(exchanges, key=lambda exchange: (exchange_order.get(exchange, 99), exchange)))
        for symbol, exchanges in route_sets.items()
    }


def cached_fs_spot_check_for_quote_failure(
    symbol: str,
    spot_exchange: str,
    futures_bid: float | None,
    futures_ask: float | None,
    current_funding_rate: float | None,
    failure_message: str | None,
) -> dict[str, Any] | None:
    cached = read_fs_borrow_cache(symbol, spot_exchange)
    if not cached:
        return None
    spot_bid = first_float(cached.get("spotBid"))
    spot_ask = first_float(cached.get("spotAsk"))
    if spot_bid is None or spot_ask is None or spot_bid <= 0 or spot_ask <= 0:
        return None
    basis_rate = fs_basis_rate(futures_bid, spot_ask)
    open_spread_rate = fs_astro_spread_rate(spot_bid, futures_ask)
    close_spread_rate = fs_astro_spread_rate(spot_ask, futures_bid)
    basis_risk = fs_basis_risk(basis_rate)
    basis_risk_rate = fs_basis_risk_rate(basis_risk)
    borrow_period = first_float(cached.get("borrowPeriodRate"))
    cached_message = str(cached.get("message") or "").strip()
    refresh_message = str(failure_message or "现货盘口暂时不可用").strip()
    return {
        **cached,
        "message": f"{cached_message}；现货刷新失败，暂用 5 分钟内缓存：{refresh_message}".strip("；"),
        "spotBid": spot_bid,
        "spotAsk": spot_ask,
        "basisRate": basis_rate,
        "openSpreadRate": open_spread_rate,
        "closeSpreadRate": close_spread_rate,
        "basisRisk": basis_risk,
        "basisMessage": fs_basis_message(basis_rate),
        "feeRate": fs_fee_rate(),
        "slippageRate": fs_slippage_rate(),
        "basisRiskRate": basis_risk_rate,
        "borrowPeriodRate": borrow_period,
        "netFundingRate": fs_net_funding_rate(
            current_funding_rate,
            borrow_period,
            fs_fee_rate(),
            fs_slippage_rate(),
            basis_risk_rate,
        ),
        "quoteCached": True,
        "source": "scan_cache",
    }


def fs_signal_evaluation_error(candidate: dict[str, Any], exc: Exception) -> dict[str, Any]:
    symbol = normalize_symbol(str(candidate.get("symbol") or ""))
    futures_exchange = normalize_exchange(str(candidate.get("exchange") or ""))
    return {
        "signalKey": fs_signal_key(symbol, futures_exchange, FS_SIGNAL_SPOT_EXCHANGE, candidate.get("fundingUpdatedAt")),
        "symbol": symbol,
        "futuresExchange": futures_exchange,
        "spotExchange": FS_SIGNAL_SPOT_EXCHANGE,
        "fundingTime": candidate.get("fundingUpdatedAt"),
        "currentFundingRate": candidate.get("fundingRate"),
        "premiumRate": candidate.get("premiumRate"),
        "volume24h": candidate.get("volume24h"),
        "checks": {
            **pending_fs_checks(),
            "bg": {"exchange": "bg", "status": "error", "message": f"FS 检测失败：{exc}"},
        },
        "historyFunding": [],
        "actionable": False,
        "reason": f"FS 检测失败：{exc}",
        "pushed": False,
        "pushStatus": None,
        "pushMessage": None,
        "lastLog": None,
    }


def evaluate_fs_signal_candidate_isolated(
    candidate: dict[str, Any],
    check_exchanges: tuple[str, ...],
) -> dict[str, Any]:
    from app.database import SessionLocal

    worker_db = SessionLocal()
    started_monotonic = time.monotonic()
    try:
        try:
            signal = evaluate_fs_signal_candidate(worker_db, candidate, False, check_exchanges)
        except Exception as exc:
            signal = fs_signal_evaluation_error(candidate, exc)
        signal["_runtimeDurationMs"] = (time.monotonic() - started_monotonic) * 1000
        return signal
    finally:
        worker_db.close()


def harmonize_fs_transfer_statuses(signals: list[dict[str, Any]]) -> None:
    status_rank = {"ok": 4, "not_supported": 3, "pending": 2, "error": 1}
    best_by_symbol_exchange: dict[tuple[str, str], dict[str, Any]] = {}
    for signal in signals:
        symbol = normalize_symbol(str(signal.get("symbol") or ""))
        checks = signal.get("checks")
        if not isinstance(checks, dict):
            continue
        for exchange in FS_SIGNAL_SPOT_EXCHANGES:
            check = checks.get(exchange)
            transfer_status = check.get("transferStatus") if isinstance(check, dict) else None
            if not isinstance(transfer_status, dict):
                continue
            key = (symbol, exchange)
            current = best_by_symbol_exchange.get(key)
            if current is None or status_rank.get(str(transfer_status.get("status") or ""), 0) > status_rank.get(
                str(current.get("status") or ""), 0
            ):
                best_by_symbol_exchange[key] = transfer_status
    for signal in signals:
        symbol = normalize_symbol(str(signal.get("symbol") or ""))
        checks = signal.get("checks")
        if not isinstance(checks, dict):
            continue
        for exchange in FS_SIGNAL_SPOT_EXCHANGES:
            check = checks.get(exchange)
            best = best_by_symbol_exchange.get((symbol, exchange))
            if isinstance(check, dict) and best is not None:
                check["transferStatus"] = best


def revalidate_astro_fs_borrow_pair(
    pair: dict[str, Any],
    config: AstroSdkConfig,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    candidate = pair.get("_fsCandidate") if isinstance(pair.get("_fsCandidate"), dict) else None
    if not candidate:
        return None, {"reason": "fs_candidate_missing", "roundsPassed": 0, "checks": []}
    checks: list[dict[str, Any]] = []
    latest_pair: dict[str, Any] | None = None
    started = time.monotonic()
    for index in range(2):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            signal = evaluate_fs_signal_candidate(
                db,
                candidate,
                push=False,
                check_exchanges=(FS_SIGNAL_SPOT_EXCHANGE,),
            )
        finally:
            db.close()
        eligible, assessment = assess_astro_fs_borrow_signal(signal)
        check = {"round": index + 1, **assessment}
        checks.append(check)
        if not eligible:
            return None, {
                **check,
                "roundsRequired": 2,
                "roundsPassed": index,
                "checks": checks,
                "durationMs": round((time.monotonic() - started) * 1000, 1),
                "source": "official_futures_and_bitget_margin_api",
            }
        latest_pair = build_astro_fs_pair(
            {
                **signal,
                "spotExchange": FS_SIGNAL_SPOT_EXCHANGE,
                "openSpreadRate": assessment["openSpreadRate"],
            },
            config,
        )
        latest_pair["_fsCandidate"] = {
            "symbol": assessment["symbol"],
            "exchange": assessment["futuresExchange"],
            "fundingRate": assessment["currentFundingRate"],
            "periodHours": assessment["fundingIntervalHours"],
            "fundingUpdatedAt": signal.get("fundingTime"),
            "premiumRate": signal.get("premiumRate"),
            "negativePotential": True,
            "volume24h": signal.get("volume24h"),
        }
        latest_pair["_fsAssessment"] = assessment
        if index == 0:
            time.sleep(0.5)
    final_check = checks[-1]
    return latest_pair, {
        **final_check,
        "reason": "eligible",
        "roundsRequired": 2,
        "roundsPassed": 2,
        "intervalMs": 500,
        "checks": checks,
        "durationMs": round((time.monotonic() - started) * 1000, 1),
        "source": "official_futures_and_bitget_margin_api",
    }


def _compute_crypto_fs_signals_overview(
    db: Session,
    limit: int = 50,
    push: bool = False,
    scan_id: str | None = None,
    schedule_auto_cards: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    runtime_scan_id = scan_id or fs_runtime_scan_id()
    candidate_pool_limit = min(1000, max(limit * 10, 200))
    candidate_pool = canonicalize_fs_candidate_pool(
        db,
        realtime_negative_funding_candidates(list(fs_signal_futures_exchanges()), limit=candidate_pool_limit),
    )
    futures_market_symbols = canonicalize_fs_futures_market_symbols(db, fetch_fs_futures_market_symbols())
    futures_routes = fs_futures_routes_by_symbol(candidate_pool, futures_market_symbols)
    candidates = candidate_pool
    source = "official_api_negative_potential"
    scan_limit = limit if push else min(limit, env_int("FS_QUICK_SCAN_LIMIT", min(limit, 10)))
    known_borrowable = recent_fs_borrowable_symbols(db)
    binance_inventory: dict[str, float] = {}
    try:
        with http_client() as client:
            binance_inventory = fetch_binance_margin_available_inventory(client)
            known_borrowable.update(
                symbol
                for symbol, amount in binance_inventory.items()
                if amount > 0
            )
    except Exception as exc:
        add_fs_runtime_log(
            db,
            runtime_scan_id,
            "source_error",
            level="warning",
            stage="binance_inventory",
            futures_exchange="bn",
            spot_exchange="bn",
            status="error",
            message=f"Binance 可借库存读取失败：{exc}",
        )
    candidates = select_fs_signal_scan_candidates(candidates, known_borrowable, scan_limit)
    futures_routes = probe_missing_fs_futures_routes(futures_routes, candidates, futures_market_symbols)
    try:
        with http_client() as client:
            spot_symbols = fetch_binance_spot_usdt_symbols(client)
            fetch_binance_next_hourly_interest_rates(
                client,
                {
                    symbol
                    for candidate in candidates
                    if (symbol := normalize_symbol(str(candidate.get("symbol") or ""))) in spot_symbols
                },
            )
    except Exception as exc:
        add_fs_runtime_log(
            db,
            runtime_scan_id,
            "source_error",
            level="warning",
            stage="binance_borrow_rate",
            futures_exchange="bn",
            spot_exchange="bn",
            status="error",
            message=f"Binance 借币利率读取失败：{exc}",
        )
    try:
        with http_client(timeout=6.0) as client:
            fetch_binance_transfer_config(client)
    except Exception as exc:
        add_fs_runtime_log(
            db,
            runtime_scan_id,
            "source_error",
            level="warning",
            stage="binance_transfer",
            futures_exchange="bn",
            spot_exchange="bn",
            status="error",
            message=f"Binance 充提状态读取失败：{exc}",
        )
    if push:
        signals = []
        for candidate in candidates:
            started_monotonic = time.monotonic()
            try:
                signal = evaluate_fs_signal_candidate(db, candidate, push=True, check_exchanges=FS_SIGNAL_SPOT_EXCHANGES)
            except Exception as exc:
                signal = fs_signal_evaluation_error(candidate, exc)
            signal["_runtimeDurationMs"] = (time.monotonic() - started_monotonic) * 1000
            signals.append(signal)
    else:
        quick_check_exchanges = FS_SIGNAL_SPOT_EXCHANGES
        signals_by_index: list[dict[str, Any] | None] = [None] * len(candidates)
        # FS is a background discovery path.  Keep spare connection/CPU slots
        # for the 5-second Astro scanner and its hot-route depth checks.
        worker_count = min(len(candidates), max(1, min(env_int("FS_SIGNAL_QUICK_SCAN_WORKERS", 2), 4)))
        with ThreadPoolExecutor(max_workers=worker_count or 1) as executor:
            future_map = {
                executor.submit(evaluate_fs_signal_candidate_isolated, candidate, quick_check_exchanges): (index, candidate)
                for index, candidate in enumerate(candidates)
            }
            for future in as_completed(future_map):
                index, candidate = future_map[future]
                try:
                    signals_by_index[index] = future.result()
                except Exception as exc:
                    signals_by_index[index] = fs_signal_evaluation_error(candidate, exc)
        signals = [signal for signal in signals_by_index if signal is not None]
    harmonize_fs_transfer_statuses(signals)
    fs_auto_card_summary: dict[str, Any] | None = None
    if schedule_auto_cards:
        resolved_astro = astro_sdk_config()
        fs_pairs, fs_build_summary = build_astro_fs_borrow_pairs(signals, resolved_astro)
        sync_status = schedule_astro_pairs(
            fs_pairs,
            resolved_astro,
            revalidator=revalidate_astro_fs_borrow_pair,
        )
        fs_auto_card_summary = {
            **fs_build_summary,
            "state": sync_status.get("state"),
            "message": sync_status.get("message"),
        }
        add_fs_runtime_log(
            db,
            runtime_scan_id,
            "astro_fs_borrow_cards_scheduled",
            stage="astro_fs_borrow",
            status="ok",
            message=(
                f"FS 借币建卡筛选完成：评估 {fs_build_summary['evaluatedCount']}，"
                f"符合 {fs_build_summary['eligibleCount']}。"
            ),
            details=fs_auto_card_summary,
        )
    for signal in signals:
        symbol = normalize_symbol(str(signal.get("symbol") or ""))
        symbol_routes = futures_routes.get(symbol) or ()
        signal["futuresExchanges"] = list(symbol_routes)
        signal["futuresRouteCount"] = len(symbol_routes)
        record_fs_candidate_runtime_log(db, runtime_scan_id, signal)
    payload = {
        "status": "ok",
        "updatedAt": datetime.now(timezone.utc),
        "source": source,
        "threshold": FS_SIGNAL_NET_THRESHOLD,
        "feeRate": fs_fee_rate(),
        "slippageRate": fs_slippage_rate(),
        "basisWatchRiskRate": fs_basis_risk_rate("watch"),
        "basisDangerRiskRate": fs_basis_risk_rate("danger"),
        "cooldownMinutes": FS_SIGNAL_COOLDOWN_MINUTES,
        "watchThreshold": -fs_signal_watch_min_negative_daily_rate(),
        "watchDailyThreshold": -fs_signal_watch_min_negative_daily_rate(),
        "futuresExchanges": list(fs_signal_futures_exchanges()),
        "sourceScan": fs_candidate_source_scan_status(),
        "candidateCount": len(candidates),
        "actionableCount": 0,
        "watchCount": 0,
        "pushedCount": 0,
        "exchangeChecks": {
            exchange: {
                "exchange": exchange,
                "status": "ok",
                "message": "首页自动检测。" if not push else "实际检测。",
            }
            for exchange in FS_SIGNAL_EXCHANGE_CHECKS
        },
        "items": signals,
        "watchItems": [],
        "fsBorrowAutoCard": fs_auto_card_summary,
    }
    normalized = normalize_fs_signals_payload(payload)
    record_fs_observations(db, normalized.get("items") or [], normalized["updatedAt"])
    db.commit()
    return normalized


def compute_crypto_fs_signals_overview(
    db: Session,
    limit: int = 50,
    push: bool = False,
    schedule_auto_cards: bool = False,
) -> dict[str, Any]:
    scan_id = fs_runtime_scan_id()
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    add_fs_runtime_log(
        db,
        scan_id,
        "scan_started",
        stage="scan",
        status="running",
        message="FS 后台扫描开始。",
        details={"limit": max(1, min(int(limit), 100)), "push": bool(push)},
    )
    db.commit()
    try:
        payload = _compute_crypto_fs_signals_overview(
            db,
            limit,
            push,
            scan_id,
            schedule_auto_cards=schedule_auto_cards,
        )
    except Exception as exc:
        db.rollback()
        duration_ms = (time.monotonic() - started_monotonic) * 1000
        add_fs_runtime_log(
            db,
            scan_id,
            "scan_failed",
            level="error",
            stage="scan",
            status="error",
            message=f"FS 后台扫描失败：{exc}",
            duration_ms=duration_ms,
            details={"startedAt": started_at, "limit": max(1, min(int(limit), 100)), "push": bool(push)},
        )
        db.commit()
        raise
    duration_ms = (time.monotonic() - started_monotonic) * 1000
    payload["scanId"] = scan_id
    add_fs_runtime_log(
        db,
        scan_id,
        "scan_completed",
        stage="scan",
        status="ok",
        message="FS 后台扫描完成。",
        duration_ms=duration_ms,
        details={
            "startedAt": started_at,
            "updatedAt": payload.get("updatedAt"),
            "candidateCount": payload.get("candidateCount"),
            "potentialCount": payload.get("potentialCount"),
            "borrowableCount": payload.get("borrowableCount"),
            "actionableCount": payload.get("actionableCount"),
            "watchCount": payload.get("watchCount"),
            "pushedCount": payload.get("pushedCount"),
            "hiddenHardUnavailableCount": payload.get("hiddenHardUnavailableCount"),
            "limit": max(1, min(int(limit), 100)),
            "push": bool(push),
        },
    )
    db.commit()
    return payload


def fs_signal_verified_borrow_check(signal: dict[str, Any]) -> dict[str, Any] | None:
    checks = signal.get("checks")
    if not isinstance(checks, dict):
        return None
    spot_exchange = normalize_exchange(str(signal.get("spotExchange") or FS_SIGNAL_SPOT_EXCHANGE))
    exchange_order = (spot_exchange, *(exchange for exchange in FS_SIGNAL_SPOT_EXCHANGES if exchange != spot_exchange))
    for exchange in exchange_order:
        check = checks.get(exchange)
        if not isinstance(check, dict) or check.get("canBorrow") is not True:
            continue
        if fs_borrow_value_block_reason(check.get("borrowableAmount"), check.get("borrowableValueUsdt")):
            continue
        return check
    return None


def fs_borrow_check_waitable(check: Any) -> bool:
    if not isinstance(check, dict):
        return False
    if check.get("inventoryAvailable") is True or check.get("canBorrow") is True:
        return True
    if str(check.get("status") or "") != "not_borrowable":
        return False
    message = str(check.get("message") or "")
    hard_unavailable_markers = (
        "当前不支持借",
        "未开启抵押",
        "25112",
        "参数不存在",
        "不存在",
        "未返回",
    )
    if any(marker in message for marker in hard_unavailable_markers):
        return False
    support_markers = ("支持借", "支持 ", "保证金借币")
    temporary_zero_markers = ("新增可借为 0", "新增可借额度为 0", "当前账户新增可借额度为 0")
    return any(marker in message for marker in support_markers) and any(
        marker in message for marker in temporary_zero_markers
    )


def backfill_fs_check_borrow_cost(signal: dict[str, Any], check: dict[str, Any]) -> None:
    period_hours = first_float(signal.get("periodHours"))
    borrow_period = first_float(check.get("borrowPeriodRate"))
    if borrow_period is None and period_hours is not None and period_hours > 0:
        hourly_rate = first_float(check.get("hourlyBorrowRate"))
        daily_rate = first_float(check.get("dailyBorrowRate"))
        if hourly_rate is not None:
            borrow_period = hourly_rate * period_hours
        elif daily_rate is not None:
            borrow_period = daily_rate / 24 * period_hours
        if borrow_period is not None:
            check["borrowPeriodRate"] = borrow_period
    if borrow_period is not None and first_float(check.get("netFundingRate")) is None:
        check["netFundingRate"] = fs_net_funding_rate(
            first_float(signal.get("currentFundingRate")),
            borrow_period,
            first_float(check.get("feeRate"), signal.get("feeRate")),
            first_float(check.get("slippageRate"), signal.get("slippageRate")),
            first_float(check.get("basisRiskRate"), signal.get("basisRiskRate")),
        )


def normalize_fs_signals_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    items: list[dict[str, Any]] = []
    unavailable_count = 0
    hidden_hard_unavailable_count = 0
    for raw in result.get("items") or []:
        if not isinstance(raw, dict):
            continue
        signal = dict(raw)
        negative_potential, potential_type, potential_reason = fs_negative_potential(
            signal.get("currentFundingRate"),
            signal.get("premiumRate"),
        )
        if not negative_potential:
            continue
        signal["negativePotential"] = True
        signal["potentialType"] = signal.get("potentialType") or potential_type
        signal["potentialReason"] = signal.get("potentialReason") or potential_reason
        raw_checks = signal.get("checks") if isinstance(signal.get("checks"), dict) else {}
        checks = {
            exchange: dict(check) if isinstance(check, dict) else check
            for exchange, check in raw_checks.items()
        }
        signal["checks"] = checks
        for check in checks.values():
            if isinstance(check, dict):
                backfill_fs_check_borrow_cost(signal, check)
        selected_check = checks.get(normalize_exchange(str(signal.get("spotExchange") or FS_SIGNAL_SPOT_EXCHANGE)))
        if isinstance(selected_check, dict) and first_float(signal.get("borrowPeriodRate")) is None:
            signal["borrowPeriodRate"] = first_float(selected_check.get("borrowPeriodRate"))
            signal["netFundingRate"] = first_float(selected_check.get("netFundingRate"))
        spread_check = selected_check if isinstance(selected_check, dict) else None
        if spread_check is None or first_float(spread_check.get("openSpreadRate")) is None:
            spread_check = next(
                (
                    check
                    for exchange in FS_SIGNAL_SPOT_EXCHANGES
                    if isinstance((check := checks.get(exchange)), dict)
                    and first_float(check.get("openSpreadRate")) is not None
                ),
                spread_check,
            )
        if isinstance(spread_check, dict):
            if first_float(signal.get("openSpreadRate")) is None:
                signal["openSpreadRate"] = first_float(spread_check.get("openSpreadRate"))
            if first_float(signal.get("closeSpreadRate")) is None:
                signal["closeSpreadRate"] = first_float(spread_check.get("closeSpreadRate"))
        platform_checks = [
            check
            for exchange in FS_SIGNAL_SPOT_EXCHANGES
            if isinstance((check := checks.get(exchange)), dict)
        ]
        bg_check = checks.get(FS_SIGNAL_SPOT_EXCHANGE)
        bg_can_borrow = isinstance(bg_check, dict) and bg_check.get("canBorrow") is True
        inventory_available = any(
            check.get("inventoryAvailable") is True or check.get("canBorrow") is True
            for check in platform_checks
        )
        borrow_route_waitable = any(fs_borrow_check_waitable(check) for check in platform_checks)
        borrow_route_unknown = any(
            str(check.get("status") or "") in {"error", "pending", "needs_authorization"}
            for check in platform_checks
        )
        open_spread_rate = first_float(signal.get("openSpreadRate"))
        spread_rate = open_spread_rate if open_spread_rate is not None else first_float(signal.get("basisRate"))
        large_spread = fs_signal_large_spread(spread_rate)
        if not inventory_available and not borrow_route_waitable and not (large_spread and borrow_route_unknown):
            hidden_hard_unavailable_count += 1
            continue
        executable_borrow = fs_signal_verified_borrow_check(signal) is not None
        signal["inventoryAvailable"] = inventory_available
        signal["borrowRouteWaitable"] = borrow_route_waitable
        signal["borrowRouteUnknown"] = borrow_route_unknown
        signal["executableBorrow"] = executable_borrow
        signal["borrowPriority"] = 1 if inventory_available else 0
        signal["largeSpread"] = large_spread
        signal["limitOrderCandidate"] = bool(
            large_spread and not inventory_available and (borrow_route_waitable or borrow_route_unknown)
        )
        signal["spreadRate"] = spread_rate
        if signal["limitOrderCandidate"]:
            wait_reason = "借币状态待确认，可先挂单观察。" if borrow_route_unknown and not borrow_route_waitable else "当前无 B，仍可挂单等待。"
            signal["limitOrderReason"] = f"现货高于合约约 {rate_pct_text(abs(float(spread_rate)))}，{wait_reason}"
        if not inventory_available:
            unavailable_count += 1
        borrow_block_reason = None
        if bg_can_borrow and isinstance(bg_check, dict):
            borrow_block_reason = fs_borrow_value_block_reason(
                first_float(signal.get("borrowableAmount"), bg_check.get("borrowableAmount")),
                first_float(signal.get("borrowableValueUsdt"), bg_check.get("borrowableValueUsdt")),
            )
        watch_met = fs_watch_threshold_met(signal.get("currentFundingRate"), signal.get("periodHours"))
        display_push_blocked = (
            signal.get("actionable")
            and (
                normalize_exchange(str(signal.get("spotExchange") or "")) != FS_SIGNAL_SPOT_EXCHANGE
                or not bg_can_borrow
                or bool(borrow_block_reason)
                or not watch_met
                or signal.get("basisRisk") != "normal"
                or first_float(signal.get("netFundingRate")) is None
                or float(first_float(signal.get("netFundingRate")) or 0) <= FS_SIGNAL_NET_THRESHOLD
            )
        )
        if display_push_blocked:
            signal["actionable"] = False
            signal["pushed"] = False
            signal["pushStatus"] = None
            signal["pushMessage"] = None
            if bg_can_borrow and watch_met:
                signal["watchOnly"] = True
                signal["watchReason"] = signal.get("watchReason") or fs_watch_reason(signal.get("currentFundingRate"), signal.get("periodHours"))
            if borrow_block_reason:
                signal["reason"] = borrow_block_reason
            elif signal.get("basisRisk") != "normal":
                signal["reason"] = signal.get("basisMessage")
            else:
                signal["reason"] = signal.get("reason")
        if signal.get("actionable"):
            signal["watchOnly"] = False
            signal["watchReason"] = None
        else:
            signal["watchOnly"] = True
            if signal.get("limitOrderCandidate"):
                signal["watchReason"] = "大差价 · 可挂单等待 B"
            else:
                signal["watchReason"] = signal.get("watchReason") or signal.get("potentialReason") or "负费率潜在候选"
            signal["pushed"] = False
            signal["pushStatus"] = None
            signal["pushMessage"] = None
        items.append(signal)
    items.sort(
        key=lambda signal: (
            0 if signal.get("inventoryAvailable") else 1,
            0 if signal.get("limitOrderCandidate") else 1,
            0 if signal.get("actionable") else 1,
            0 if signal.get("potentialType") == "current_negative" else 1,
            fs_funding_sort_rate(signal),
        )
    )
    watch_items = [signal for signal in items if signal.get("watchOnly") and not signal.get("actionable")]
    result["items"] = items
    result["watchItems"] = watch_items
    result["actionableCount"] = len([signal for signal in items if signal.get("actionable")])
    result["watchCount"] = len(watch_items)
    result["pushedCount"] = len([signal for signal in items if signal.get("actionable") and signal.get("pushed")])
    result["potentialCount"] = len(items)
    result["borrowableCount"] = len([signal for signal in items if signal.get("inventoryAvailable")])
    result["largeSpreadNoBorrowCount"] = len([signal for signal in items if signal.get("limitOrderCandidate")])
    result["unavailableCount"] = unavailable_count
    result["hiddenHardUnavailableCount"] = hidden_hard_unavailable_count
    result["hiddenUnavailableCount"] = 0
    return result


def fs_observation_interval_seconds() -> int:
    return max(60, env_int("FS_OBSERVATION_INTERVAL_SECONDS", 5 * 60))


def fs_observation_scope(signal: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_symbol(str(signal.get("symbol") or "")),
        normalize_exchange(str(signal.get("futuresExchange") or "")),
        normalize_exchange(str(signal.get("spotExchange") or FS_SIGNAL_SPOT_EXCHANGE)),
    )


def fs_observation_state(signal: dict[str, Any]) -> str:
    if signal.get("actionable"):
        return "actionable"
    if signal.get("executableBorrow"):
        return "borrow_executable"
    if signal.get("inventoryAvailable"):
        return "borrow_small"
    return "watch_no_borrow"


def fs_amount_changed_materially(previous: float | None, current: float | None) -> bool:
    old = first_float(previous)
    new = first_float(current)
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    if old == 0:
        return new != 0
    return abs(new - old) / abs(old) >= 0.25


def record_fs_observations(db: Session, signals: list[dict[str, Any]], batch_time: datetime | None = None) -> int:
    observed_at = batch_time or datetime.now(timezone.utc)
    created = 0
    for signal in signals:
        if not isinstance(signal, dict) or not signal.get("negativePotential"):
            continue
        symbol, futures_exchange, spot_exchange = fs_observation_scope(signal)
        previous = db.scalar(
            select(CryptoFsObservationLog)
            .where(
                CryptoFsObservationLog.symbol == symbol,
                CryptoFsObservationLog.futures_exchange == futures_exchange,
                CryptoFsObservationLog.spot_exchange == spot_exchange,
            )
            .order_by(desc(CryptoFsObservationLog.created_at))
            .limit(1)
        )
        inventory_available = bool(signal.get("inventoryAvailable"))
        state = fs_observation_state(signal)
        should_record = previous is None
        if previous is not None:
            elapsed = (observed_at - utc_datetime(previous.created_at)).total_seconds()
            should_record = (
                elapsed >= fs_observation_interval_seconds()
                or previous.inventory_available is not inventory_available
                or previous.state != state
                or previous.potential_type != str(signal.get("potentialType") or "current_negative")
                or fs_amount_changed_materially(previous.borrowable_amount, signal.get("borrowableAmount"))
            )
        if not should_record:
            continue
        db.add(
            CryptoFsObservationLog(
                batch_time=observed_at,
                symbol=symbol,
                futures_exchange=futures_exchange,
                spot_exchange=spot_exchange,
                funding_time=parse_datetime_value(signal.get("fundingTime")),
                current_funding_rate=first_float(signal.get("currentFundingRate")),
                daily_funding_rate=first_float(signal.get("dailyFundingRate")),
                premium_rate=first_float(signal.get("premiumRate")),
                potential_type=str(signal.get("potentialType") or "current_negative"),
                inventory_available=inventory_available,
                executable_borrow=bool(signal.get("executableBorrow")),
                borrowable_amount=first_float(signal.get("borrowableAmount")),
                borrowable_value_usdt=first_float(signal.get("borrowableValueUsdt")),
                daily_borrow_rate=first_float(signal.get("borrowDailyRate")),
                net_funding_rate=first_float(signal.get("netFundingRate")),
                basis_rate=first_float(signal.get("basisRate")),
                state=state,
                reason=str(signal.get("reason") or signal.get("watchReason") or "") or None,
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def fs_observation_band(row: CryptoFsObservationLog) -> tuple[str, str]:
    daily_rate = first_float(row.daily_funding_rate)
    if row.potential_type == "premium_negative" and (daily_rate is None or daily_rate >= 0):
        return "premium_negative", "溢价先负"
    if daily_rate is not None and daily_rate <= -fs_signal_watch_min_negative_daily_rate():
        return "deep_negative", "深负费率"
    if daily_rate is not None and daily_rate <= -0.006:
        return "medium_negative", "中度负费率"
    return "mild_negative", "轻度负费率"


def fs_observation_borrow_route_valid(row: CryptoFsObservationLog) -> bool:
    if row.inventory_available is True:
        return True
    return fs_borrow_check_waitable(
        {
            "status": "not_borrowable",
            "message": row.reason,
            "inventoryAvailable": row.inventory_available,
        }
    )


def fs_observation_summary(db: Session, days: int = 7) -> dict[str, Any]:
    selected_days = max(1, min(int(days), 30))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=selected_days)
    rows = list(
        db.scalars(
            select(CryptoFsObservationLog)
            .where(CryptoFsObservationLog.created_at >= cutoff)
            .order_by(CryptoFsObservationLog.created_at, CryptoFsObservationLog.id)
            .limit(100_000)
        )
    )
    latest_batch_time = max((row.batch_time for row in rows), default=None)
    latest_by_scope: dict[tuple[str, str, str], CryptoFsObservationLog] = {}
    symbol_counts: dict[str, int] = {}
    band_stats: dict[str, dict[str, Any]] = {}
    inventory_rows_by_batch: dict[tuple[str, str, datetime], CryptoFsObservationLog] = {}
    for row in rows:
        scope = (row.symbol, row.futures_exchange, row.spot_exchange)
        if latest_batch_time is not None and row.batch_time == latest_batch_time:
            latest_by_scope[scope] = row
        if row.inventory_available in {True, False}:
            inventory_rows_by_batch[(row.symbol, row.spot_exchange, row.batch_time)] = row
        symbol_counts[row.symbol] = symbol_counts.get(row.symbol, 0) + 1
        band_key, band_label = fs_observation_band(row)
        stats = band_stats.setdefault(band_key, {"key": band_key, "label": band_label, "count": 0, "borrowableCount": 0})
        stats["count"] += 1
        if row.inventory_available is True:
            stats["borrowableCount"] += 1

    transition_events: list[dict[str, Any]] = []
    previous_inventory_by_scope: dict[tuple[str, str], bool] = {}
    lost_before_deep = 0
    lost_count = 0
    recovered_count = 0
    for row in inventory_rows_by_batch.values():
        inventory_scope = (row.symbol, row.spot_exchange)
        previous_inventory = previous_inventory_by_scope.get(inventory_scope)
        current_inventory = bool(row.inventory_available)
        event_type = None
        if previous_inventory is True and current_inventory is False:
            event_type = "borrow_lost"
            lost_count += 1
            if first_float(row.daily_funding_rate) is None or float(row.daily_funding_rate or 0) > -fs_signal_watch_min_negative_daily_rate():
                lost_before_deep += 1
        elif previous_inventory is False and current_inventory is True:
            event_type = "borrow_recovered"
            recovered_count += 1
        if event_type:
            transition_events.append(
                {
                    "type": event_type,
                    "symbol": row.symbol,
                    "futuresExchange": row.futures_exchange,
                    "dailyFundingRate": row.daily_funding_rate,
                    "borrowableAmount": row.borrowable_amount,
                    "createdAt": row.created_at,
                }
            )
        previous_inventory_by_scope[inventory_scope] = current_inventory

    bands = []
    for key in ("premium_negative", "mild_negative", "medium_negative", "deep_negative"):
        stats = band_stats.get(key, {"key": key, "label": dict(
            premium_negative="溢价先负",
            mild_negative="轻度负费率",
            medium_negative="中度负费率",
            deep_negative="深负费率",
        )[key], "count": 0, "borrowableCount": 0})
        count = int(stats["count"])
        stats["borrowableRate"] = (int(stats["borrowableCount"]) / count) if count else None
        bands.append(stats)

    latest_rows = [row for row in latest_by_scope.values() if fs_observation_borrow_route_valid(row)]
    current_borrowable = len([row for row in latest_rows if row.inventory_available is True])
    current_large_spread_no_borrow = len(
        [
            row
            for row in latest_rows
            if row.inventory_available is not True and fs_signal_large_spread(row.basis_rate)
        ]
    )
    insights: list[str] = []
    if current_large_spread_no_borrow:
        insights.append(
            f"当前有 {current_large_spread_no_borrow} 路无 B 大差价候选，"
            f"差价达到 {rate_pct_text(fs_signal_large_spread_threshold())} 后保留为挂单观察。"
        )
    if lost_count:
        share = lost_before_deep / lost_count
        insights.append(f"{selected_days}日内 B 消失 {lost_count} 次，其中 {share:.0%} 发生在日化深负阈值前。")
    if recovered_count:
        insights.append(f"{selected_days}日内 B 恢复 {recovered_count} 次，可结合恢复时的费率区间观察提前量。")
    active_bands = [band for band in bands if band["count"] and band["borrowableCount"]]
    if active_bands:
        best_band = max(active_bands, key=lambda band: band["borrowableRate"] or 0)
        insights.append(f"B 可借观测率最高的区间是“{best_band['label']}”，样本 {best_band['count']} 条。")
    elif rows:
        insights.append(f"已积累 {len(rows)} 条潜在负费率观测，当前样本尚未出现真实新增 B 额度。")
    if not insights:
        insights.append("日志正在积累；出现 B 消失/恢复与不同费率区间后，会自动生成规律。")

    return {
        "status": "ok",
        "days": selected_days,
        "updatedAt": latest_batch_time or now,
        "observationCount": len(rows),
        "symbolCount": len(symbol_counts),
        "currentCandidateCount": len(latest_rows),
        "currentBorrowableCount": current_borrowable,
        "currentLargeSpreadNoBorrowCount": current_large_spread_no_borrow,
        "borrowLostCount": lost_count,
        "borrowRecoveredCount": recovered_count,
        "bands": bands,
        "topSymbols": [
            {"symbol": symbol, "observationCount": count}
            for symbol, count in sorted(symbol_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ],
        "recentEvents": list(reversed(transition_events[-12:])),
        "insights": insights,
    }


def recent_fs_borrowable_symbols(db: Session, hours: int = 24) -> set[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    rows = list(
        db.scalars(
            select(CryptoFsObservationLog)
            .where(CryptoFsObservationLog.created_at >= cutoff)
            .order_by(desc(CryptoFsObservationLog.created_at), desc(CryptoFsObservationLog.id))
            .limit(5_000)
        )
    )
    latest: dict[str, bool] = {}
    for row in rows:
        latest.setdefault(row.symbol, row.inventory_available is True)
    return {symbol for symbol, available in latest.items() if available}


def empty_crypto_fs_signals_overview(
    limit: int, message: str | None = None, *, scanning: bool = True
) -> dict[str, Any]:
    return {
        "status": "ok",
        "cacheVersion": FS_SIGNAL_CACHE_VERSION,
        "updatedAt": None,
        "source": "official_api_negative_potential",
        "threshold": FS_SIGNAL_NET_THRESHOLD,
        "feeRate": fs_fee_rate(),
        "slippageRate": fs_slippage_rate(),
        "basisWatchRiskRate": fs_basis_risk_rate("watch"),
        "basisDangerRiskRate": fs_basis_risk_rate("danger"),
        "cooldownMinutes": FS_SIGNAL_COOLDOWN_MINUTES,
        "watchThreshold": -fs_signal_watch_min_negative_daily_rate(),
        "watchDailyThreshold": -fs_signal_watch_min_negative_daily_rate(),
        "futuresExchanges": list(fs_signal_futures_exchanges()),
        "candidateCount": 0,
        "actionableCount": 0,
        "watchCount": 0,
        "pushedCount": 0,
        "exchangeChecks": {
            exchange: {
                "exchange": exchange,
                "status": "pending" if scanning else "paused",
                "message": "后台扫描中。" if scanning else "自动扫描已暂停，未触发新扫描。",
            }
            for exchange in FS_SIGNAL_EXCHANGE_CHECKS
        },
        "items": [],
        "watchItems": [],
        "astroAutoCard": {**astro_auto_card_status(), "spreadScanner": astro_spread_scanner_status()},
        "scanning": scanning,
        "scanStartedAt": None,
        "scanFinishedAt": None,
        "message": message or "暂无缓存，后台正在生成第一轮结果。",
    }


def jsonable_fs_signal_payload(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: jsonable_fs_signal_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable_fs_signal_payload(item) for item in value]
    return value


def fs_signal_good_cache_path(limit: int):
    from app.database import get_data_dir

    return get_data_dir() / f"crypto-fs-signals-last-good-{limit}.json"


def fs_signal_good_cache_max_age_seconds() -> int:
    return max(60, env_int("FS_SIGNAL_GOOD_CACHE_MAX_AGE_SECONDS", 15 * 60))


def fs_signal_stale_cache_max_age_seconds() -> int:
    return max(fs_signal_good_cache_max_age_seconds(), env_int("FS_SIGNAL_STALE_CACHE_MAX_AGE_SECONDS", 12 * 60 * 60))


def fs_signal_scan_timeout_seconds() -> int:
    return max(120, env_int("FS_SIGNAL_SCAN_TIMEOUT_SECONDS", 10 * 60))


def clear_fs_signal_good_cache(limit: int) -> None:
    try:
        fs_signal_good_cache_path(limit).unlink(missing_ok=True)
    except OSError:
        return


def fs_signal_payload_has_live_actionable_item(payload: dict[str, Any], now: datetime) -> bool:
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("actionable"):
            continue
        funding_time = parse_datetime_value(item.get("fundingTime"))
        if funding_time is None or utc_datetime(funding_time) > now:
            return True
    return False


def fs_signal_payload_has_live_display_item(payload: dict[str, Any], now: datetime) -> bool:
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        potential, _, _ = fs_negative_potential(item.get("currentFundingRate"), item.get("premiumRate"))
        if not potential:
            continue
        funding_time = parse_datetime_value(item.get("fundingTime"))
        if funding_time is None or utc_datetime(funding_time) > now:
            return True
    return False


def mark_fs_signal_payload_stale_display(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    stale_items: list[dict[str, Any]] = []
    for raw in result.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        potential, _, _ = fs_negative_potential(item.get("currentFundingRate"), item.get("premiumRate"))
        if not potential:
            continue
        item["actionable"] = False
        item["pushed"] = False
        item["pushStatus"] = None
        item["pushMessage"] = None
        item["watchOnly"] = True
        item["watchReason"] = "上一轮缓存，后台刷新中。"
        item["reason"] = "上一轮缓存可能已过期，仅供临时观察。"
        stale_items.append(item)
    result["items"] = stale_items
    result["watchItems"] = stale_items
    result["actionableCount"] = 0
    result["watchCount"] = len(stale_items)
    result["pushedCount"] = 0
    result["message"] = "上一轮缓存可能已过期，后台正在刷新。"
    return result


def fs_signal_good_cache_paths(limit: int) -> list[Path]:
    exact = fs_signal_good_cache_path(limit)
    paths = [exact]
    fallback_paths: list[Path] = []
    try:
        for path in exact.parent.glob("crypto-fs-signals-last-good-*.json"):
            if path == exact:
                continue
            suffix = path.stem.removeprefix("crypto-fs-signals-last-good-")
            try:
                path_limit = int(suffix)
            except ValueError:
                continue
            if path_limit >= limit:
                paths.append(path)
            else:
                fallback_paths.append(path)
    except OSError as exc:
        raise ValueError(f"KSTR集合竞价观察点落盘失败：{exc}") from exc
    primary = sorted(set(paths), key=lambda path: (path != exact, -(path.stat().st_mtime if path.exists() else 0)))
    fallback = sorted(set(fallback_paths), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return primary + fallback


def load_fs_signal_good_cache(limit: int) -> tuple[datetime, dict[str, Any]] | None:
    now = datetime.now(timezone.utc)
    for path in fs_signal_good_cache_paths(limit):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not payload.get("items"):
            continue
        if payload.get("cacheVersion") != FS_SIGNAL_CACHE_VERSION:
            continue
        if payload.get("source") not in {
            "recent_snapshot_negative_funding",
            "official_api_negative_funding",
            "official_api_negative_potential",
        }:
            continue
        try:
            cached_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            cached_at = now
        age_seconds = (now - cached_at).total_seconds()
        if age_seconds > fs_signal_stale_cache_max_age_seconds():
            continue
        live_display = fs_signal_payload_has_live_display_item(payload, now)
        if not live_display or age_seconds > fs_signal_good_cache_max_age_seconds():
            payload = mark_fs_signal_payload_stale_display(payload)
        payload["items"] = list(payload.get("items") or [])[:limit]
        payload["watchItems"] = []
        payload = normalize_fs_signals_payload(payload)
        payload["message"] = payload.get("message") or "显示上一轮缓存，后台正在刷新。"
        return cached_at, payload
    return None


def save_fs_signal_good_cache(limit: int, payload: dict[str, Any]) -> None:
    payload = normalize_fs_signals_payload(payload)
    if not payload.get("items"):
        clear_fs_signal_good_cache(limit)
        return
    if not fs_signal_payload_has_live_display_item(payload, datetime.now(timezone.utc)):
        clear_fs_signal_good_cache(limit)
        return
    payload = dict(payload)
    payload["cacheVersion"] = FS_SIGNAL_CACHE_VERSION
    path = fs_signal_good_cache_path(limit)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(jsonable_fs_signal_payload(payload), ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        return


def fs_signal_cached_response(limit: int) -> tuple[dict[str, Any] | None, bool]:
    now = datetime.now(timezone.utc)
    with _fs_signal_cache_lock:
        cached = _fs_signal_cache.get(limit)
        if cached:
            cached_at, payload = cached
            if payload.get("cacheVersion") != FS_SIGNAL_CACHE_VERSION:
                _fs_signal_cache.pop(limit, None)
            elif payload.get("source") not in {
                "recent_snapshot_negative_funding",
                "official_api_negative_funding",
                "official_api_negative_potential",
            }:
                _fs_signal_cache.pop(limit, None)
            else:
                age_seconds = (now - cached_at).total_seconds()
                stale = age_seconds >= FS_SIGNAL_CACHE_SECONDS
                if stale:
                    if age_seconds > fs_signal_stale_cache_max_age_seconds():
                        _fs_signal_cache.pop(limit, None)
                        cached = None
                    else:
                        live_display = fs_signal_payload_has_live_display_item(payload, now)
                        if not live_display or age_seconds > fs_signal_good_cache_max_age_seconds():
                            payload = mark_fs_signal_payload_stale_display(payload)
                            _fs_signal_cache[limit] = (cached_at, payload)
                        else:
                            payload = normalize_fs_signals_payload(payload)
                            payload["message"] = payload.get("message") or "后台刷新中，先显示上一轮有效结果。"
                            _fs_signal_cache[limit] = (cached_at, payload)
                if cached:
                    return dict(payload), stale
    good_cache = load_fs_signal_good_cache(limit)
    if good_cache:
        cached_at, payload = good_cache
        stale = (now - cached_at).total_seconds() >= FS_SIGNAL_CACHE_SECONDS
        with _fs_signal_cache_lock:
            _fs_signal_cache[limit] = (cached_at, payload)
        return dict(payload), stale
    if cached:
        return dict(cached[1]), True
    return None, True


def fs_borrow_fast_scan_interval_seconds() -> int:
    return max(
        FS_BORROW_FAST_SCAN_INTERVAL_SECONDS,
        env_int("FS_BORROW_FAST_SCAN_SECONDS", FS_BORROW_FAST_SCAN_INTERVAL_SECONDS),
    )


def fs_borrow_fast_scan_limit() -> int:
    return max(1, min(env_int("FS_BORROW_FAST_SCAN_LIMIT", FS_BORROW_FAST_SCAN_LIMIT), 50))


def fs_borrow_fast_scan_status() -> dict[str, Any]:
    state = dict(_fs_borrow_fast_scan_state)
    return {
        **state,
        "intervalSeconds": fs_borrow_fast_scan_interval_seconds(),
        "limit": fs_borrow_fast_scan_limit(),
    }


def fs_fast_signal_has_inventory(signal: dict[str, Any]) -> bool:
    if signal.get("inventoryAvailable") is True:
        return True
    checks = signal.get("checks")
    if not isinstance(checks, dict):
        return False
    return any(
        isinstance(check, dict)
        and (check.get("inventoryAvailable") is True or check.get("canBorrow") is True)
        for check in checks.values()
    )


def fs_fast_candidate_sort_key(signal: dict[str, Any]) -> tuple[int, int, float, float]:
    daily_rate = first_float(signal.get("dailyFundingRate"))
    if daily_rate is None:
        daily_rate = fs_daily_funding_rate(signal.get("currentFundingRate"), signal.get("periodHours") or 8)
    open_spread = first_float(signal.get("openSpreadRate"), signal.get("basisRate"))
    return (
        0 if fs_fast_signal_has_inventory(signal) else 1,
        0 if signal.get("limitOrderCandidate") else 1,
        daily_rate if daily_rate is not None else float("inf"),
        -(open_spread if open_spread is not None else 0.0),
    )


def fs_borrow_fast_scan_candidates(limit: int | None = None) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit or fs_borrow_fast_scan_limit()), 50))
    with _fs_signal_cache_lock:
        payloads = [
            (cached_at, deepcopy(payload))
            for cached_at, payload in _fs_signal_cache.values()
            if isinstance(payload, dict)
        ]
    payloads.sort(key=lambda item: item[0], reverse=True)
    candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for _, payload in payloads:
        items = payload.get("items")
        if not isinstance(items, list):
            continue
        for raw in sorted(
            (item for item in items if isinstance(item, dict)),
            key=fs_fast_candidate_sort_key,
        ):
            potential, _, _ = fs_negative_potential(raw.get("currentFundingRate"), raw.get("premiumRate"))
            if not potential:
                continue
            symbol = normalize_symbol(str(raw.get("symbol") or ""))
            if not symbol or symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            candidates.append(raw)
            if len(candidates) >= bounded_limit:
                return candidates
    return candidates


def fs_fast_previous_check(signal: dict[str, Any], exchange: str) -> dict[str, Any]:
    checks = signal.get("checks")
    if not isinstance(checks, dict):
        return {}
    check = checks.get(exchange)
    return dict(check) if isinstance(check, dict) else {}


def fs_fast_check_price(previous: dict[str, Any]) -> float | None:
    return quote_reference_price_usdt(
        previous.get("spotBid"),
        previous.get("spotAsk"),
        None,
        None,
    )


def fs_binance_fast_inventory_check(
    symbol: str,
    inventory: dict[str, float],
    previous: dict[str, Any],
) -> dict[str, Any]:
    asset = normalize_symbol(symbol)
    if previous.get("status") == "not_supported" and "现货" in str(previous.get("message") or ""):
        return previous
    amount = max(0.0, float(inventory.get(asset, 0.0) or 0.0))
    hourly_rate = first_float(previous.get("hourlyBorrowRate"))
    daily_rate = first_float(previous.get("dailyBorrowRate"))
    if daily_rate is None and hourly_rate is not None:
        daily_rate = hourly_rate * 24
    price = fs_fast_check_price(previous)
    value = borrowable_value_usdt(amount, price)
    inventory_available = amount > 0
    can_borrow = inventory_available and (value is None or value > MIN_BORROWABLE_VALUE_USDT)
    if inventory_available:
        message = f"Binance 当前有 B，可借库存 {amount:g} {asset}"
        if not can_borrow:
            message += borrow_value_message_suffix(value)
    else:
        message = f"Binance 当前 {asset} 无 B"
    return {
        "exchange": "bn",
        "symbol": asset,
        "status": "ok" if can_borrow else "not_borrowable",
        "message": message,
        "canBorrow": can_borrow,
        "inventoryAvailable": inventory_available,
        "borrowableAmount": amount if inventory_available else None,
        "borrowableValueUsdt": value,
        "hourlyBorrowRate": hourly_rate,
        "dailyBorrowRate": daily_rate,
        "updatedAt": datetime.now(timezone.utc),
        "fastChecked": True,
    }


def fetch_bitget_fs_fast_inventory_check(
    client: httpx.Client,
    symbol: str,
    previous: dict[str, Any],
) -> dict[str, Any]:
    asset = normalize_symbol(symbol)
    pair_symbol = f"{asset}USDT"
    hourly_rate = first_float(previous.get("hourlyBorrowRate"))
    daily_rate = first_float(previous.get("dailyBorrowRate"))
    if daily_rate is None and hourly_rate is not None:
        daily_rate = hourly_rate * 24
    try:
        loan_payload = request_json(
            client,
            f"{spot_base_url('bg')}/api/v3/market/margin-loans",
            {"coin": asset},
        )
        loan_data = loan_payload.get("data") if isinstance(loan_payload, dict) else {}
        platform_quota = first_float(
            loan_data.get("platformRemaingQuota") if isinstance(loan_data, dict) else None,
            loan_data.get("platformRemainingQuota") if isinstance(loan_data, dict) else None,
        )
        latest_daily_rate = first_float(
            loan_data.get("dailyInterest") if isinstance(loan_data, dict) else None
        )
        if latest_daily_rate is not None:
            daily_rate = latest_daily_rate
            hourly_rate = latest_daily_rate / 24
    except Exception as exc:
        return {
            "exchange": "bg",
            "symbol": asset,
            "status": "error",
            "message": f"Bitget 平台借币余额查询失败：{exc}",
            "canBorrow": None,
            "inventoryAvailable": None,
            "hourlyBorrowRate": hourly_rate,
            "dailyBorrowRate": daily_rate,
            "updatedAt": datetime.now(timezone.utc),
            "fastChecked": True,
        }
    try:
        payload = signed_bitget_uta_post(
            client,
            "/api/v3/account/max-open-available",
            {"category": "MARGIN", "symbol": pair_symbol, "orderType": "market", "side": "sell"},
        )
    except ValueError as exc:
        message = str(exc)
        if "25112" in message or "未开启抵押" in message:
            return {
                "exchange": "bg",
                "symbol": asset,
                "status": "error",
                "message": f"Bitget 当前未开启 {asset} 抵押，暂时无 B",
                "canBorrow": False,
                "inventoryAvailable": False,
                "borrowableAmount": None,
                "hourlyBorrowRate": hourly_rate,
                "dailyBorrowRate": daily_rate,
                "updatedAt": datetime.now(timezone.utc),
                "fastChecked": True,
            }
        return {
            "exchange": "bg",
            "symbol": asset,
            "status": "error",
            "message": f"Bitget B 快扫失败：{message}",
            "canBorrow": None,
            "inventoryAvailable": None,
            "hourlyBorrowRate": hourly_rate,
            "dailyBorrowRate": daily_rate,
            "updatedAt": datetime.now(timezone.utc),
            "fastChecked": True,
        }
    data = payload.get("data") if isinstance(payload, dict) else {}
    available = first_float(data.get("available") if isinstance(data, dict) else None)
    max_open = first_float(
        data.get("maxOpen") if isinstance(data, dict) else None,
        data.get("maxSellAvailable") if isinstance(data, dict) else None,
    )
    incremental_amount = max(0.0, max_open - max(0.0, available or 0.0)) if max_open is not None else 0.0
    amount = (
        min(incremental_amount, max(0.0, platform_quota))
        if platform_quota is not None
        else 0.0
    )
    check = MarginShortCheck(
        exchange="bg",
        symbol=asset,
        status="ok" if amount > 0 else "not_borrowable",
        message=(
            f"Bitget 当前有 B，可新增借 {amount:g} {asset}（平台剩余 {platform_quota:g}）"
            if amount > 0
            else (
                f"Bitget UTA 支持 {asset} 保证金借币，但平台当前新增可借为 0"
                if platform_quota is not None
                else f"Bitget 未返回 {asset} 平台借币余额，按暂无 B 处理"
            )
        ),
        can_borrow=amount > 0,
        inventory_available=amount > 0,
        borrowable_amount=amount if amount > 0 else None,
        hourly_borrow_rate=hourly_rate,
        daily_borrow_rate=daily_rate,
        updated_at=datetime.now(timezone.utc),
    )
    if check.can_borrow:
        check = apply_min_borrowable_value_threshold(check, fs_fast_check_price(previous))
    result = margin_short_check_to_out(check) or {}
    result["fastChecked"] = True
    return result


def merge_fs_fast_borrow_checks(
    signal: dict[str, Any],
    checks_by_exchange: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(signal)
    raw_checks = result.get("checks")
    checks = dict(raw_checks) if isinstance(raw_checks, dict) else {}
    period_hours = first_float(result.get("periodHours")) or 8.0
    for exchange, update_payload in checks_by_exchange.items():
        previous = checks.get(exchange)
        merged = dict(previous) if isinstance(previous, dict) else {"exchange": exchange}
        if update_payload.get("status") == "error":
            # A timeout/429 is unknown, not proof that previously available B disappeared.
            previous_inventory = merged.get("inventoryAvailable")
            previous_can_borrow = merged.get("canBorrow")
            merged.update(update_payload)
            merged["inventoryAvailable"] = previous_inventory
            merged["canBorrow"] = previous_can_borrow
            merged["staleInventory"] = True
        else:
            merged.update(update_payload)
            merged.pop("staleInventory", None)
        borrow_check = MarginShortCheck(
            exchange=exchange,
            symbol=normalize_symbol(str(result.get("symbol") or "")),
            status=str(merged.get("status") or "not_borrowable"),
            message=str(merged.get("message") or ""),
            can_borrow=merged.get("canBorrow"),
            inventory_available=merged.get("inventoryAvailable"),
            borrowable_amount=first_float(merged.get("borrowableAmount")),
            borrowable_value_usdt=first_float(merged.get("borrowableValueUsdt")),
            hourly_borrow_rate=first_float(merged.get("hourlyBorrowRate")),
            daily_borrow_rate=first_float(merged.get("dailyBorrowRate")),
            updated_at=datetime.now(timezone.utc),
        )
        period_rate = borrow_period_rate(borrow_check, period_hours)
        merged["borrowPeriodRate"] = period_rate
        merged["netFundingRate"] = fs_net_funding_rate(
            result.get("currentFundingRate"),
            period_rate,
            first_float(merged.get("feeRate"), result.get("feeRate")) or fs_fee_rate(),
            first_float(merged.get("slippageRate"), result.get("slippageRate")) or fs_slippage_rate(),
            first_float(merged.get("basisRiskRate"), result.get("basisRiskRate")) or 0.0,
        )
        checks[exchange] = merged
    result["checks"] = checks

    available_checks = [
        check
        for check in checks.values()
        if isinstance(check, dict)
        and (check.get("inventoryAvailable") is True or check.get("canBorrow") is True)
    ]
    if available_checks:
        best = max(
            available_checks,
            key=lambda check: (
                first_float(check.get("netFundingRate")) is not None,
                first_float(check.get("netFundingRate")) or float("-inf"),
                first_float(check.get("borrowableValueUsdt"), check.get("borrowableAmount")) or 0.0,
            ),
        )
        result.update(
            {
                "spotExchange": best.get("exchange"),
                "inventoryAvailable": True,
                "borrowableAmount": best.get("borrowableAmount"),
                "borrowableValueUsdt": best.get("borrowableValueUsdt"),
                "borrowDailyRate": best.get("dailyBorrowRate"),
                "borrowHourlyRate": best.get("hourlyBorrowRate"),
                "borrowPeriodRate": best.get("borrowPeriodRate"),
                "netFundingRate": best.get("netFundingRate"),
                "basisRate": best.get("basisRate", result.get("basisRate")),
                "openSpreadRate": best.get("openSpreadRate", result.get("openSpreadRate")),
                "closeSpreadRate": best.get("closeSpreadRate", result.get("closeSpreadRate")),
            }
        )
    else:
        result["inventoryAvailable"] = False
        result["borrowableAmount"] = None
        result["borrowableValueUsdt"] = None
        result["netFundingRate"] = None
    result["borrowFastUpdatedAt"] = datetime.now(timezone.utc)
    return result


def merge_fs_fast_market_quote(signal: dict[str, Any], quote: MarketQuote) -> dict[str, Any]:
    """Refresh the volatile futures fields without repeating private borrow checks."""
    result = deepcopy(signal)
    if quote.status != "ok" or quote.funding_rate is None:
        result["fundingStale"] = True
        result["fundingRefreshError"] = quote.error or "合约资金费接口未返回有效数据"
        return result

    refreshed_at = quote.updated_at or datetime.now(timezone.utc)
    period_hours = quote.period_hours or first_float(result.get("periodHours")) or 8.0
    funding_time = quote.next_funding_time or parse_datetime_value(result.get("fundingTime"))
    current_rate = quote.funding_rate
    premium_rate = quote.premium_rate
    if premium_rate is None:
        premium_rate = first_float(result.get("premiumRate"))
    result.update(
        {
            "signalKey": fs_signal_key(
                normalize_symbol(str(result.get("symbol") or quote.symbol)),
                normalize_exchange(str(result.get("futuresExchange") or quote.exchange)),
                normalize_exchange(str(result.get("spotExchange") or FS_SIGNAL_SPOT_EXCHANGE)),
                funding_time,
            ),
            "fundingTime": funding_time,
            "fundingUpdatedAt": refreshed_at,
            "fundingFastUpdatedAt": refreshed_at,
            "currentFundingRate": current_rate,
            "dailyFundingRate": fs_daily_funding_rate(current_rate, period_hours),
            "premiumRate": premium_rate,
            "periodHours": period_hours,
            "futuresBid": quote.best_bid if quote.best_bid is not None else result.get("futuresBid"),
            "futuresAsk": quote.best_ask if quote.best_ask is not None else result.get("futuresAsk"),
            "volume24h": quote.volume_24h if quote.volume_24h is not None else result.get("volume24h"),
            "fundingStale": False,
            "fundingRefreshError": None,
        }
    )

    funding_deadline = utc_datetime(parse_datetime_value(funding_time))
    if funding_deadline is not None:
        minutes_to_funding = (funding_deadline - datetime.now(timezone.utc)).total_seconds() / 60
        result["minutesToFunding"] = minutes_to_funding
        result["fundingWindowOk"] = 0 < minutes_to_funding <= fs_signal_max_minutes_to_funding()

    raw_checks = result.get("checks")
    checks = dict(raw_checks) if isinstance(raw_checks, dict) else {}
    for exchange, raw_check in list(checks.items()):
        if not isinstance(raw_check, dict):
            continue
        check = dict(raw_check)
        spot_bid = first_float(check.get("spotBid"))
        spot_ask = first_float(check.get("spotAsk"))
        basis_rate = fs_basis_rate(quote.best_bid, spot_ask)
        open_spread_rate = fs_astro_spread_rate(spot_bid, quote.best_ask)
        close_spread_rate = fs_astro_spread_rate(spot_ask, quote.best_bid)
        basis_risk = fs_basis_risk(basis_rate)
        basis_risk_rate = fs_basis_risk_rate(basis_risk)
        borrow_cost = first_float(check.get("borrowPeriodRate"))
        if borrow_cost is None:
            borrow_check = MarginShortCheck(
                exchange=str(exchange),
                symbol=normalize_symbol(str(result.get("symbol") or "")),
                status=str(check.get("status") or "not_borrowable"),
                message=str(check.get("message") or ""),
                can_borrow=check.get("canBorrow"),
                inventory_available=check.get("inventoryAvailable"),
                borrowable_amount=first_float(check.get("borrowableAmount")),
                borrowable_value_usdt=first_float(check.get("borrowableValueUsdt")),
                hourly_borrow_rate=first_float(check.get("hourlyBorrowRate")),
                daily_borrow_rate=first_float(check.get("dailyBorrowRate")),
                updated_at=parse_datetime_value(check.get("updatedAt")),
            )
            borrow_cost = borrow_period_rate(borrow_check, period_hours)
        check.update(
            {
                "basisRate": basis_rate,
                "openSpreadRate": open_spread_rate,
                "closeSpreadRate": close_spread_rate,
                "basisRisk": basis_risk,
                "basisMessage": fs_basis_message(basis_rate),
                "basisRiskRate": basis_risk_rate,
                "borrowPeriodRate": borrow_cost,
                "netFundingRate": fs_net_funding_rate(
                    current_rate,
                    borrow_cost,
                    first_float(check.get("feeRate"), result.get("feeRate")) or fs_fee_rate(),
                    first_float(check.get("slippageRate"), result.get("slippageRate")) or fs_slippage_rate(),
                    basis_risk_rate,
                ),
            }
        )
        checks[exchange] = check
    result["checks"] = checks

    selected_exchange = normalize_exchange(str(result.get("spotExchange") or FS_SIGNAL_SPOT_EXCHANGE))
    selected_check = checks.get(selected_exchange)
    if not isinstance(selected_check, dict):
        selected_check = next(
            (
                check
                for check in checks.values()
                if isinstance(check, dict)
                and (check.get("inventoryAvailable") is True or check.get("canBorrow") is True)
            ),
            None,
        )
    if isinstance(selected_check, dict):
        result.update(
            {
                "borrowPeriodRate": selected_check.get("borrowPeriodRate"),
                "netFundingRate": selected_check.get("netFundingRate"),
                "basisRate": selected_check.get("basisRate"),
                "openSpreadRate": selected_check.get("openSpreadRate"),
                "closeSpreadRate": selected_check.get("closeSpreadRate"),
                "basisRisk": selected_check.get("basisRisk"),
                "basisMessage": selected_check.get("basisMessage"),
                "basisRiskRate": selected_check.get("basisRiskRate"),
            }
        )

    negative_potential, potential_type, potential_reason = fs_negative_potential(current_rate, premium_rate)
    result["negativePotential"] = negative_potential
    result["potentialType"] = potential_type
    result["potentialReason"] = potential_reason
    if not negative_potential:
        result["actionable"] = False
        result["watchOnly"] = False
        result["watchReason"] = None
        result["reason"] = "当前资金费与溢价均已回到非负，等待完整扫描移出候选。"
    elif not result.get("actionable"):
        result["watchOnly"] = True
        result["watchReason"] = fs_watch_reason(current_rate, period_hours)
    return result


def fs_fast_cached_signal_to_candidate(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": signal.get("symbol"),
        "exchange": signal.get("futuresExchange"),
        "fundingRate": signal.get("currentFundingRate"),
        "premiumRate": signal.get("premiumRate"),
        "periodHours": signal.get("periodHours"),
        "fundingIntervalHours": signal.get("periodHours"),
        "fundingUpdatedAt": signal.get("fundingTime"),
        "volume24h": signal.get("volume24h"),
        "negativePotential": signal.get("negativePotential"),
        "potentialType": signal.get("potentialType"),
        "potentialReason": signal.get("potentialReason"),
    }


def update_fs_signal_caches_from_fast_scan(
    checks_by_symbol: dict[str, dict[str, dict[str, Any]]],
    reevaluated: dict[tuple[str, str], dict[str, Any]],
    *,
    persist: bool,
) -> int:
    updated_payloads: list[tuple[int, dict[str, Any]]] = []
    updated_count = 0
    with _fs_signal_cache_lock:
        for cache_limit, (cached_at, payload) in list(_fs_signal_cache.items()):
            raw_items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(raw_items, list):
                continue
            changed = False
            items: list[dict[str, Any]] = []
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                symbol = normalize_symbol(str(raw.get("symbol") or ""))
                futures_exchange = normalize_exchange(str(raw.get("futuresExchange") or ""))
                replacement = reevaluated.get((symbol, futures_exchange))
                if replacement is not None:
                    merged_replacement = deepcopy(replacement)
                    merged_replacement.setdefault("futuresExchanges", raw.get("futuresExchanges"))
                    merged_replacement.setdefault("futuresRouteCount", raw.get("futuresRouteCount"))
                    items.append(merged_replacement)
                    changed = True
                    updated_count += 1
                    continue
                symbol_checks = checks_by_symbol.get(symbol)
                if symbol_checks:
                    items.append(merge_fs_fast_borrow_checks(raw, symbol_checks))
                    changed = True
                    updated_count += 1
                else:
                    items.append(raw)
            if not changed:
                continue
            updated_payload = dict(payload)
            updated_payload["items"] = items
            updated_payload["borrowFastUpdatedAt"] = datetime.now(timezone.utc)
            funding_refresh_times = [
                utc_datetime(parse_datetime_value(item.get("fundingFastUpdatedAt")))
                for item in items
                if isinstance(item, dict) and item.get("fundingFastUpdatedAt")
            ]
            funding_refresh_times = [value for value in funding_refresh_times if value is not None]
            if funding_refresh_times:
                updated_payload["fundingFastUpdatedAt"] = max(funding_refresh_times)
                updated_payload["updatedAt"] = max(funding_refresh_times)
            # The fast path refreshes volatile market fields and B state, but it
            # must not run the full candidate filter again. A temporary no-B or
            # quote error must not remove a row before full reconciliation.
            updated_payload["watchItems"] = [
                item
                for item in items
                if item.get("watchOnly") and not item.get("actionable")
            ]
            updated_payload["potentialCount"] = len(items)
            updated_payload["borrowableCount"] = len(
                [item for item in items if fs_fast_signal_has_inventory(item)]
            )
            updated_payload["actionableCount"] = len(
                [item for item in items if item.get("actionable")]
            )
            updated_payload["watchCount"] = len(updated_payload["watchItems"])
            updated_payload["pushedCount"] = len(
                [item for item in items if item.get("actionable") and item.get("pushed")]
            )
            # Keep the full-scan timestamp so the fast path cannot suppress normal reconciliation.
            _fs_signal_cache[cache_limit] = (cached_at, updated_payload)
            updated_payloads.append((cache_limit, deepcopy(updated_payload)))
    if persist:
        for cache_limit, payload in updated_payloads:
            save_fs_signal_good_cache(cache_limit, payload)
    return updated_count


def refresh_fs_borrow_fast_inventory(
    db: Session,
    limit: int | None = None,
    push: bool = True,
) -> dict[str, Any]:
    if not _fs_borrow_fast_scan_lock.acquire(blocking=False):
        return {"status": "running", **fs_borrow_fast_scan_status()}
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    scan_id = fs_runtime_scan_id()
    candidates: list[dict[str, Any]] = []
    checked_count = 0
    funding_refreshed_count = 0
    transitions: list[dict[str, Any]] = []
    checks_by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    reevaluated: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        _fs_borrow_fast_scan_state.update(
            {
                "running": True,
                "startedAt": started_at,
                "finishedAt": _fs_borrow_fast_scan_state.get("finishedAt"),
                "lastError": None,
            }
        )
        candidates = fs_borrow_fast_scan_candidates(limit)
        if not candidates:
            result = {
                "status": "skipped",
                "candidateCount": 0,
                "checkedCount": 0,
                "fundingRefreshedCount": 0,
                "transitionCount": 0,
                "message": "暂无负费率候选，资金费与 B 快扫等待下一轮完整扫描。",
            }
            _fs_borrow_fast_scan_state.update(result)
            return result

        refreshed_candidates: list[dict[str, Any]] = []
        market_specs: list[tuple[dict[str, Any], str, str, float]] = []
        for signal in candidates:
            symbol = normalize_symbol(str(signal.get("symbol") or ""))
            futures_exchange = normalize_exchange(str(signal.get("futuresExchange") or ""))
            request_symbol, price_ratio = mapped_symbol_and_ratio_for(db, symbol, futures_exchange, "futures")
            market_specs.append((signal, futures_exchange, request_symbol, price_ratio))
        market_results: dict[tuple[str, str], MarketQuote] = {}
        with ThreadPoolExecutor(
            max_workers=min(
                max(1, env_int("FS_BORROW_FAST_MARKET_WORKERS", 2)),
                2,
                len(market_specs) or 1,
            )
        ) as executor:
            futures = {
                executor.submit(fetch_fs_market_quote, exchange, request_symbol, "futures"): (
                    signal,
                    exchange,
                    price_ratio,
                )
                for signal, exchange, request_symbol, price_ratio in market_specs
            }
            for future in as_completed(futures):
                signal, exchange, price_ratio = futures[future]
                symbol = normalize_symbol(str(signal.get("symbol") or ""))
                try:
                    quote = adjust_quote_price_ratio(future.result(), price_ratio, symbol)
                except Exception as exc:
                    quote = MarketQuote(
                        exchange=exchange,
                        symbol=symbol,
                        market_type="futures",
                        status="error",
                        error=str(exc),
                    )
                market_results[(symbol, exchange)] = quote
        for signal in candidates:
            symbol = normalize_symbol(str(signal.get("symbol") or ""))
            futures_exchange = normalize_exchange(str(signal.get("futuresExchange") or ""))
            quote = market_results.get((symbol, futures_exchange))
            refreshed = merge_fs_fast_market_quote(signal, quote) if quote is not None else signal
            if refreshed.get("fundingFastUpdatedAt"):
                funding_refreshed_count += 1
            refreshed_candidates.append(refreshed)
        candidates = refreshed_candidates

        binance_inventory: dict[str, float] | None = None
        try:
            with http_client(timeout=8.0) as client:
                binance_inventory = fetch_binance_margin_available_inventory(client)
        except Exception as exc:
            add_fs_runtime_log(
                db,
                scan_id,
                "borrow_fast_error",
                level="warning",
                stage="borrow_fast_bn",
                spot_exchange="bn",
                status="error",
                message=f"Binance B 快扫失败：{exc}",
            )

        with http_client(timeout=8.0) as bitget_client:
            for signal in candidates:
                symbol = normalize_symbol(str(signal.get("symbol") or ""))
                symbol_checks: dict[str, dict[str, Any]] = {}
                if binance_inventory is not None:
                    previous_bn = fs_fast_previous_check(signal, "bn")
                    symbol_checks["bn"] = fs_binance_fast_inventory_check(symbol, binance_inventory, previous_bn)
                    checked_count += 1
                previous_bg = fs_fast_previous_check(signal, "bg")
                symbol_checks["bg"] = fetch_bitget_fs_fast_inventory_check(bitget_client, symbol, previous_bg)
                checked_count += 1
                checks_by_symbol[symbol] = symbol_checks

                for exchange, current in symbol_checks.items():
                    if current.get("status") == "error":
                        continue
                    previous = fs_fast_previous_check(signal, exchange)
                    was_available = previous.get("inventoryAvailable") is True or previous.get("canBorrow") is True
                    is_available = current.get("inventoryAvailable") is True or current.get("canBorrow") is True
                    if was_available == is_available:
                        continue
                    transitions.append(
                        {
                            "symbol": symbol,
                            "futuresExchange": signal.get("futuresExchange"),
                            "spotExchange": exchange,
                            "fromAvailable": was_available,
                            "toAvailable": is_available,
                            "borrowableAmount": current.get("borrowableAmount"),
                        }
                    )

        for signal in candidates:
            key = (
                normalize_symbol(str(signal.get("symbol") or "")),
                normalize_exchange(str(signal.get("futuresExchange") or "")),
            )
            reevaluated[key] = merge_fs_fast_borrow_checks(
                signal,
                checks_by_symbol.get(key[0], {}),
            )

        recovered_keys: set[tuple[str, str]] = set()
        recovered_candidates = [
            signal
            for signal in candidates
            if any(
                transition["symbol"] == normalize_symbol(str(signal.get("symbol") or ""))
                and transition["toAvailable"] is True
                for transition in transitions
            )
        ][:FS_BORROW_FAST_RECHECK_LIMIT]
        for signal in recovered_candidates:
            key = (
                normalize_symbol(str(signal.get("symbol") or "")),
                normalize_exchange(str(signal.get("futuresExchange") or "")),
            )
            if key in recovered_keys:
                continue
            recovered_keys.add(key)
            try:
                reevaluated[key] = evaluate_fs_signal_candidate(
                    db,
                    fs_fast_cached_signal_to_candidate(signal),
                    push=push,
                    check_exchanges=FS_SIGNAL_SPOT_EXCHANGES,
                )
            except Exception as exc:
                add_fs_runtime_log(
                    db,
                    scan_id,
                    "borrow_fast_recheck_error",
                    level="warning",
                    stage="borrow_fast_recheck",
                    symbol=key[0],
                    futures_exchange=key[1],
                    status="error",
                    message=f"B 恢复后的完整复核失败：{exc}",
                )

        update_fs_signal_caches_from_fast_scan(
            checks_by_symbol,
            reevaluated,
            persist=bool(transitions),
        )
        if transitions:
            refreshed_signals = list(reevaluated.values())
            if refreshed_signals:
                record_fs_observations(db, refreshed_signals, datetime.now(timezone.utc))
            for transition in transitions:
                add_fs_runtime_log(
                    db,
                    scan_id,
                    "borrow_fast_transition",
                    stage="borrow_fast",
                    symbol=str(transition["symbol"]),
                    futures_exchange=str(transition.get("futuresExchange") or ""),
                    spot_exchange=str(transition["spotExchange"]),
                    status="ok",
                    message=(
                        f"{transition['symbol']} {transition['spotExchange']} "
                        f"{'出现 B' if transition['toAvailable'] else 'B 消失'}。"
                    ),
                    details=transition,
                )
        db.commit()
        result = {
            "status": "ok",
            "candidateCount": len(candidates),
            "checkedCount": checked_count,
            "fundingRefreshedCount": funding_refreshed_count,
            "transitionCount": len(transitions),
            "durationSeconds": round(time.monotonic() - started_monotonic, 2),
            "message": "资金费与 B 快扫完成。",
        }
        _fs_borrow_fast_scan_state.update(result)
        return result
    except Exception as exc:
        db.rollback()
        try:
            add_fs_runtime_log(
                db,
                scan_id,
                "borrow_fast_failed",
                level="error",
                stage="borrow_fast",
                status="error",
                message=f"B 快扫失败：{exc}",
                duration_ms=(time.monotonic() - started_monotonic) * 1000,
            )
            db.commit()
        except Exception:
            db.rollback()
        _fs_borrow_fast_scan_state.update(
            {
                "lastError": str(exc),
                "candidateCount": len(candidates),
                "checkedCount": checked_count,
                "fundingRefreshedCount": funding_refreshed_count,
                "transitionCount": len(transitions),
            }
        )
        return {
            "status": "error",
            "message": str(exc),
            "candidateCount": len(candidates),
            "checkedCount": checked_count,
            "fundingRefreshedCount": funding_refreshed_count,
            "transitionCount": len(transitions),
        }
    finally:
        finished_at = datetime.now(timezone.utc)
        _fs_borrow_fast_scan_state.update({"running": False, "finishedAt": finished_at})
        _fs_borrow_fast_scan_lock.release()


def fs_signal_cache_actionable_count(limit: int) -> int:
    cached, _ = fs_signal_cached_response(limit)
    return int(cached.get("actionableCount") or 0) if cached else 0


def attach_fs_signal_scan_state(limit: int, payload: dict[str, Any], default_message: str | None = None) -> dict[str, Any]:
    with _fs_signal_cache_lock:
        state = dict(_fs_signal_scan_state.get(limit) or {})
        started_at = utc_datetime(parse_datetime_value(state.get("startedAt")))
        if state.get("running") and started_at is not None:
            age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
            if age_seconds > fs_signal_scan_timeout_seconds():
                state["running"] = False
                state["finishedAt"] = datetime.now(timezone.utc)
                state["error"] = "扫描超时，已自动释放状态。"
                _fs_signal_scan_state[limit] = state
    result = normalize_fs_signals_payload(payload)
    result["astroAutoCard"] = {**astro_auto_card_status(), "spreadScanner": astro_spread_scanner_status()}
    scanning = bool(state.get("running"))
    result["scanning"] = scanning
    result["scanStartedAt"] = state.get("startedAt")
    result["scanFinishedAt"] = state.get("finishedAt")
    for check in result.get("exchangeChecks", {}).values():
        if isinstance(check, dict) and check.get("status") in {"pending", "paused", "waiting"}:
            check["status"] = "pending" if scanning else ("paused" if state.get("paused") else "waiting")
            check["message"] = (
                "后台扫描中。" if scanning else
                "自动扫描已暂停。" if state.get("paused") else "当前未扫描，等待下一轮。"
            )
    if scanning:
        result["message"] = default_message or ("后台扫描中，刷新当前资金费。" if not result.get("items") else "后台扫描中，先显示上一轮缓存。")
    elif default_message is not None:
        result["message"] = default_message
    else:
        result.setdefault("message", None)
    return result


def begin_fs_signal_scan(
    limit: int,
    *,
    push: bool = False,
    started_at: datetime | None = None,
) -> datetime:
    """Expose one authoritative scan state to every FS reader."""
    normalized_limit = max(1, min(int(limit), 100))
    normalized_started_at = started_at or datetime.now(timezone.utc)
    with _fs_signal_cache_lock:
        _fs_signal_scan_state[normalized_limit] = {
            "running": True,
            "startedAt": normalized_started_at,
            "finishedAt": None,
            "push": push,
        }
    return normalized_started_at


def cache_fs_signal_scan_result(
    limit: int,
    payload: dict[str, Any],
    *,
    started_at: datetime,
    finished_at: datetime | None = None,
    push: bool = False,
    message: str = "后台扫描已完成。",
) -> dict[str, Any]:
    """Atomically publish a completed scan to memory and the durable good cache."""
    normalized_limit = max(1, min(int(limit), 100))
    normalized_finished_at = finished_at or datetime.now(timezone.utc)
    result = normalize_fs_signals_payload(payload)
    result["cacheVersion"] = FS_SIGNAL_CACHE_VERSION
    result["scanning"] = False
    result["scanStartedAt"] = started_at
    result["scanFinishedAt"] = normalized_finished_at
    result["message"] = message
    with _fs_signal_cache_lock:
        _fs_signal_cache[normalized_limit] = (normalized_finished_at, result)
        _fs_signal_scan_state[normalized_limit] = {
            "running": False,
            "startedAt": started_at,
            "finishedAt": normalized_finished_at,
            "push": push,
        }
    save_fs_signal_good_cache(normalized_limit, result)
    return attach_fs_signal_scan_state(normalized_limit, result)


def fail_fs_signal_scan(
    limit: int,
    *,
    started_at: datetime,
    error: Exception | str,
    push: bool = False,
) -> None:
    """Release the shared scan state while retaining the last usable result."""
    normalized_limit = max(1, min(int(limit), 100))
    finished_at = datetime.now(timezone.utc)
    message = str(error)
    with _fs_signal_cache_lock:
        _fs_signal_scan_state[normalized_limit] = {
            "running": False,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "push": push,
            "error": message,
        }
        cached = _fs_signal_cache.get(normalized_limit)
        if cached:
            cached[1]["status"] = "partial_error"
            cached[1]["message"] = f"后台扫描失败，继续显示上一轮缓存：{message}"


def run_fs_signal_background_scan(limit: int, push: bool) -> None:
    started_at = begin_fs_signal_scan(limit, push=push)
    try:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            payload = compute_crypto_fs_signals_overview(db, limit, push=push)
        finally:
            db.close()
        cache_fs_signal_scan_result(
            limit,
            payload,
            started_at=started_at,
            push=push,
        )
    except Exception as exc:
        fail_fs_signal_scan(
            limit,
            started_at=started_at,
            error=exc,
            push=push,
        )


def start_fs_signal_background_scan(limit: int, push: bool) -> bool:
    now = datetime.now(timezone.utc)
    with _fs_signal_cache_lock:
        state = _fs_signal_scan_state.get(limit) or {}
        if state.get("running"):
            started_at = utc_datetime(parse_datetime_value(state.get("startedAt")))
            if started_at is not None and (now - started_at).total_seconds() > fs_signal_scan_timeout_seconds():
                state["running"] = False
                state["finishedAt"] = now
                state["error"] = "扫描超时，已自动释放状态。"
            else:
                return False
        _fs_signal_scan_state[limit] = {
            "running": True,
            "startedAt": now,
            "finishedAt": None,
            "push": push,
        }
    thread = threading.Thread(target=run_fs_signal_background_scan, args=(limit, push), daemon=True)
    thread.start()
    return True


def pause_fs_signal_background_scans(limit: int | None = None) -> None:
    now = datetime.now(timezone.utc)
    with _fs_signal_cache_lock:
        limits = [limit] if limit is not None else list(_fs_signal_scan_state.keys())
        for item_limit in limits:
            state = _fs_signal_scan_state.get(item_limit) or {}
            _fs_signal_scan_state[item_limit] = {
                "running": False,
                "startedAt": state.get("startedAt"),
                "finishedAt": now,
                "push": state.get("push", False),
                "paused": True,
            }
            cached = _fs_signal_cache.get(item_limit)
            if cached:
                cached[1]["scanning"] = False
                cached[1]["scanFinishedAt"] = now
                cached[1]["message"] = "自动扫描已暂停。"


def crypto_fs_signals_overview(db: Session, limit: int = 50, push: bool = False, allow_scan: bool = True) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    if push:
        with _fs_signal_cache_lock:
            scan_running = bool((_fs_signal_scan_state.get(limit) or {}).get("running"))
        if scan_running:
            add_fs_runtime_log(
                db,
                fs_runtime_scan_id(),
                "scan_skipped",
                level="warning",
                stage="scan",
                status="busy",
                message="FS 后台扫描已在进行，本次未重复启动。",
                details={"limit": limit, "push": True},
            )
            db.commit()
            cached, _ = fs_signal_cached_response(limit)
            return attach_fs_signal_scan_state(
                limit,
                cached or empty_crypto_fs_signals_overview(limit),
                "后台扫描已在进行，本次不重复启动。",
            )
        started_at = begin_fs_signal_scan(limit, push=True)
        try:
            payload = compute_crypto_fs_signals_overview(db, limit, push=True)
        except Exception as exc:
            fail_fs_signal_scan(
                limit,
                started_at=started_at,
                error=exc,
                push=True,
            )
            raise
        return cache_fs_signal_scan_result(
            limit,
            payload,
            started_at=started_at,
            push=True,
        )
    cached, stale = fs_signal_cached_response(limit)
    if (push or stale) and allow_scan:
        start_fs_signal_background_scan(limit, push=push)
    if cached:
        if stale and allow_scan:
            return attach_fs_signal_scan_state(limit, cached, "后台扫描中，先显示上一轮有效结果。")
        return attach_fs_signal_scan_state(limit, cached)
    if not allow_scan:
        payload = empty_crypto_fs_signals_overview(limit, "自动扫描已暂停，未触发新扫描。", scanning=False)
        payload["scanning"] = False
        payload["message"] = "自动扫描已暂停，未触发新扫描。"
        return payload
    return attach_fs_signal_scan_state(limit, empty_crypto_fs_signals_overview(limit))


def cached_margin_short_check(exchange: str, symbol: str) -> MarginShortCheck | None:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    key = (normalized_exchange, normalized_symbol)
    with _margin_short_cache_lock:
        cached = _margin_short_cache.get(key)
        if not cached or (now - cached[0]).total_seconds() >= MARGIN_SHORT_CACHE_SECONDS:
            return None
        cached_check = cached[1]
    return MarginShortCheck(
        exchange=str(cached_check["exchange"]),
        symbol=str(cached_check["symbol"]),
        status=str(cached_check["status"]),
        message=str(cached_check["message"]),
        can_borrow=cached_check.get("canBorrow"),
        borrowable_amount=cached_check.get("borrowableAmount"),
        borrowable_value_usdt=cached_check.get("borrowableValueUsdt"),
        hourly_borrow_rate=cached_check.get("hourlyBorrowRate"),
        daily_borrow_rate=cached_check.get("dailyBorrowRate"),
        updated_at=cached_check.get("updatedAt"),
    )


def refresh_margin_short_checks_for_rows(rows: list[dict[str, Any]]) -> None:
    targets: set[tuple[str, str]] = set()
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        normalized_exchange = FS_SIGNAL_SPOT_EXCHANGE
        if normalized_exchange not in MARGIN_PRIVATE_API_EXCHANGES or not has_private_credentials(normalized_exchange):
            continue
        targets.add((normalized_exchange, normalize_symbol(symbol)))
    for exchange, symbol in targets:
        fetch_margin_short_check(exchange, symbol)


def quote_volume_from_candle(row: Any, list_indexes: tuple[int, ...], dict_keys: tuple[str, ...] = ()) -> float | None:
    if isinstance(row, dict):
        value = first_float(
            *(row.get(key) for key in (
                *dict_keys,
                "quoteVolume",
                "quote_volume",
                "quoteVol",
                "turnover",
                "trade_turnover",
                "volCcyQuote",
                "volCcy24h",
                "vol",
            ))
        )
        if value is not None:
            return value
        base_volume = first_float(row.get("volume"), row.get("amount"), row.get("v"))
        close = first_float(row.get("close"), row.get("c"), row.get("close_price"))
        return base_volume * close if base_volume is not None and close is not None else None
    if isinstance(row, list):
        for index in list_indexes:
            if len(row) > index:
                value = parse_float(row[index])
                if value is not None:
                    return value
        base_volume = parse_float(row[5]) if len(row) > 5 else None
        close = parse_float(row[4]) if len(row) > 4 else None
        return base_volume * close if base_volume is not None and close is not None else None
    return None


def sum_quote_volume(rows: Any, list_indexes: tuple[int, ...], dict_keys: tuple[str, ...] = ()) -> float | None:
    source_rows = rows if isinstance(rows, list) else []
    total = 0.0
    found = False
    for row in source_rows[-4:]:
        value = quote_volume_from_candle(row, list_indexes, dict_keys)
        if value is not None:
            total += value
            found = True
    return total if found else None


def fetch_turnover_4h_usdt(client: httpx.Client, exchange: str, symbol: str, market_type: str) -> float | None:
    market_symbol = f"{symbol}USDT"
    if exchange == "bn":
        url = f"{spot_base_url('bn')}/api/v3/klines" if market_type == "spot" else f"{base_url('bn')}/fapi/v1/klines"
        rows = request_json(client, url, {"symbol": market_symbol, "interval": "1h", "limit": 4})
        return sum_quote_volume(rows, (7,))
    if exchange == "by":
        payload = request_json(
            client,
            f"{base_url('by')}/v5/market/kline",
            {"category": "spot" if market_type == "spot" else "linear", "symbol": market_symbol, "interval": "60", "limit": 4},
        )
        rows = (payload.get("result") or {}).get("list") if isinstance(payload, dict) else []
        return sum_quote_volume(rows, (6,))
    if exchange == "gt":
        if market_type == "spot":
            rows = request_json(
                client,
                f"{spot_base_url('gt')}/api/v4/spot/candlesticks",
                {"currency_pair": f"{symbol}_USDT", "interval": "1h", "limit": 4},
            )
            return sum_quote_volume(rows, (6, 1))
        rows = request_json(
            client,
            f"{base_url('gt')}/api/v4/futures/usdt/candlesticks",
            {"contract": f"{symbol}_USDT", "interval": "1h", "limit": 4},
        )
        return sum_quote_volume(rows, (6, 1), ("volume_24h_quote", "volume_quote", "sum"))
    if exchange == "okx":
        inst_id = f"{symbol}-USDT" if market_type == "spot" else f"{symbol}-USDT-SWAP"
        rows = okx_rows(request_json(client, f"{base_url('okx')}/api/v5/market/candles", {"instId": inst_id, "bar": "1H", "limit": 4}))
        return sum_quote_volume(rows, (7,), ("volCcyQuote",))
    if exchange == "bg":
        if market_type == "spot":
            rows = bitget_data(
                request_json(
                    client,
                    f"{spot_base_url('bg')}/api/v2/spot/market/candles",
                    {"symbol": market_symbol, "granularity": "1h", "limit": 4},
                )
            )
        else:
            rows = bitget_data(
                request_json(
                    client,
                    f"{base_url('bg')}/api/v2/mix/market/candles",
                    {"symbol": market_symbol, "productType": "USDT-FUTURES", "granularity": "1H", "limit": 4},
                )
            )
        return sum_quote_volume(rows, (6, 7))
    if exchange == "htx":
        if market_type == "spot":
            payload = request_json(
                client,
                f"{spot_base_url('htx')}/market/history/kline",
                {"symbol": f"{symbol.lower()}usdt", "period": "60min", "size": 4},
            )
        else:
            payload = request_json(
                client,
                f"{base_url('htx')}/linear-swap-ex/market/history/kline",
                {"contract_code": f"{symbol}-USDT", "period": "60min", "size": 4},
            )
        rows = payload.get("data") if isinstance(payload, dict) else []
        return sum_quote_volume(rows, (6, 7), ("trade_turnover", "vol"))
    if exchange == "as":
        if market_type == "spot":
            return None
        rows = request_json(client, f"{base_url('as')}/fapi/v1/klines", {"symbol": market_symbol, "interval": "1h", "limit": 4})
        return sum_quote_volume(rows, (7,))
    if exchange == "hl":
        if market_type == "spot":
            return None
        now = datetime.now(timezone.utc)
        payload = request_post_json(
            client,
            f"{base_url('hl')}/info",
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": "1h",
                    "startTime": int((now - timedelta(hours=4, minutes=5)).timestamp() * 1000),
                    "endTime": int(now.timestamp() * 1000),
                },
            },
        )
        return sum_quote_volume(payload, (6, 7), ("quoteVolume", "turnover"))
    return None


def cached_turnover_4h_usdt(client: httpx.Client, exchange: str, symbol: str, market_type: str) -> float | None:
    key = (normalize_exchange(exchange), normalize_symbol(symbol), normalize_market_type(market_type))
    now = datetime.now(timezone.utc)
    cached = _turnover_4h_cache.get(key)
    if cached and (now - cached[0]).total_seconds() < TURNOVER_4H_CACHE_SECONDS:
        return cached[1]
    try:
        value = fetch_turnover_4h_usdt(client, key[0], key[1], key[2])
    except Exception:
        value = None
    _turnover_4h_cache[key] = (now, value)
    return value


def binance_funding_info(client: httpx.Client, exchange: str, market_symbol: str) -> dict[str, Any]:
    try:
        rows = request_json(client, f"{base_url(exchange)}/fapi/v1/fundingInfo")
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    matched = next((row for row in rows if isinstance(row, dict) and row.get("symbol") == market_symbol), {})
    if exchange != "bn":
        return matched

    # Binance lists only adjusted contracts. A successful, well-formed complete
    # list that omits this symbol confirms the official default of 8 hours.
    # Aster's similar endpoint has no verified equivalent contract here.
    seen_symbols: set[str] = set()
    valid_list = True
    for row in rows:
        row_symbol = row.get("symbol") if isinstance(row, dict) else None
        raw_interval = row.get("fundingIntervalHours") if isinstance(row, dict) else None
        interval = None if isinstance(raw_interval, bool) else parse_float(raw_interval)
        if (
            not isinstance(row_symbol, str) or not row_symbol.strip()
            or row_symbol != row_symbol.strip() or row_symbol in seen_symbols
            or interval is None or not math.isfinite(interval) or interval <= 0
        ):
            valid_list = False
            break
        seen_symbols.add(row_symbol)
    if matched:
        # Keep existing display values, while withholding interval evidence if
        # any part of the adjustment list is malformed.
        return matched if valid_list else {**matched, "_funding_info_response_malformed": True}
    return {"_official_default_8h_verified": True} if valid_list else {}


def _funding_period_evidence(period_hours: float | None, *sources: tuple[str, Any]) -> dict[str, Any]:
    """Match API evidence or an adapter-verified default, never a UI fallback.

    Sources are expressed in hours by their adapter. This helper performs no
    network calls and does not change the interval displayed by older pages.
    """
    period = None if isinstance(period_hours, bool) else parse_float(period_hours)
    if period is not None and math.isfinite(period) and period > 0:
        for source, raw in sources:
            value = None if isinstance(raw, bool) else parse_float(raw)
            if value is not None and math.isfinite(value) and value > 0 and math.isclose(value, period, rel_tol=1e-12):
                return {"funding_period_verified": True, "funding_period_source": source}
    return {"funding_period_verified": False, "funding_period_source": "display_default_or_unknown"}


def bybit_instrument_info(client: httpx.Client, market_symbol: str) -> dict[str, Any]:
    try:
        payload = request_json(
            client,
            f"{base_url('by')}/v5/market/instruments-info",
            {"category": "linear", "symbol": market_symbol},
        )
    except Exception:
        return {}
    rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


def bitget_contract_info(client: httpx.Client, market_symbol: str) -> dict[str, Any]:
    try:
        payload = bitget_data(
            request_json(
                client,
                f"{base_url('bg')}/api/v2/mix/market/contracts",
                {"productType": "USDT-FUTURES", "symbol": market_symbol},
            )
        )
    except Exception:
        return {}
    if isinstance(payload, list) and payload:
        row = payload[0]
        return row if isinstance(row, dict) else {}
    return payload if isinstance(payload, dict) else {}


def htx_contract_info(client: httpx.Client, contract: str) -> dict[str, Any]:
    try:
        payload = request_json(
            client,
            f"{base_url('htx')}/linear-swap-api/v1/swap_contract_info",
            {"contract_code": contract},
        )
    except Exception:
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        row = data[0]
        return row if isinstance(row, dict) else {}
    return data if isinstance(data, dict) else {}


def okx_period_hours(funding: dict[str, Any]) -> float | None:
    funding_time = parse_timestamp_ms(funding.get("fundingTime"))
    next_funding_time = parse_timestamp_ms(funding.get("nextFundingTime"))
    if funding_time and next_funding_time:
        hours = (next_funding_time - funding_time).total_seconds() / 3600
        if hours > 0:
            return hours
    return None


def fetch_market(exchange: str, symbol: str, market_type: str = "futures") -> MarketQuote:
    exchange = normalize_exchange(exchange)
    symbol = normalize_symbol(symbol)
    market_type = normalize_market_type(market_type)
    try:
        with http_client() as client:
            if market_type == "spot":
                return fetch_spot_market(client, exchange, symbol)
            if exchange == "bn":
                return fetch_binance(client, symbol)
            if exchange == "by":
                return fetch_bybit(client, symbol)
            if exchange == "gt":
                return fetch_gate(client, symbol)
            if exchange == "okx":
                return fetch_okx(client, symbol)
            if exchange == "bg":
                return fetch_bitget(client, symbol)
            if exchange == "htx":
                return fetch_htx(client, symbol)
            if exchange == "hl":
                return fetch_hyperliquid(client, symbol)
            return fetch_aster(client, symbol)
    except Exception as exc:
        return MarketQuote(exchange=exchange, symbol=symbol, market_type=market_type, status="error", error=str(exc))


def adjust_quote_price_ratio(quote: MarketQuote, price_ratio: float | None, display_symbol: str | None = None) -> MarketQuote:
    ratio = parse_float(price_ratio)
    if ratio is None or ratio <= 0 or ratio == 1:
        if display_symbol:
            quote.symbol = normalize_symbol(display_symbol)
        return quote
    for field in ("best_bid", "best_ask", "mark_price", "index_price"):
        value = getattr(quote, field)
        if isinstance(value, (int, float)):
            setattr(quote, field, value / ratio)
    if display_symbol:
        quote.symbol = normalize_symbol(display_symbol)
    return quote


def amount_unit_for(market_type: str) -> str:
    return "币数" if market_type == "spot" else "手"


def order_book_level(side: str, level: int, row: Any) -> OrderBookLevel:
    price = None
    size = None
    if isinstance(row, dict):
        price = first_float(row.get("px"), row.get("p"), row.get("price"))
        size = first_float(row.get("sz"), row.get("s"), row.get("size"), row.get("amount"), row.get("qty"))
    elif isinstance(row, list):
        price = parse_float(row[0]) if len(row) > 0 else None
        size = parse_float(row[1]) if len(row) > 1 else None
    return OrderBookLevel(side=side, level=level, price=price, size=size)


def order_book_levels(rows: Any, side: str, limit: int = 2) -> list[OrderBookLevel]:
    source_rows = rows if isinstance(rows, list) else []
    levels = [order_book_level(side, index + 1, source_rows[index] if index < len(source_rows) else None) for index in range(limit)]
    return levels


def order_book_quote(
    exchange: str,
    symbol: str,
    market_type: str,
    bids: Any,
    asks: Any,
    turnover_4h_usdt: float | None = None,
    updated_at: datetime | None = None,
) -> OrderBookQuote:
    return OrderBookQuote(
        exchange=exchange,
        symbol=symbol,
        market_type=market_type,
        amount_unit=amount_unit_for(market_type),
        bids=order_book_levels(bids, "bid"),
        asks=order_book_levels(asks, "ask"),
        turnover_4h_usdt=turnover_4h_usdt,
        updated_at=updated_at or datetime.now(timezone.utc),
    )


def attach_turnover_4h(client: httpx.Client, quote: OrderBookQuote) -> OrderBookQuote:
    quote.turnover_4h_usdt = cached_turnover_4h_usdt(client, quote.exchange, quote.symbol, quote.market_type)
    return quote


def fetch_order_book(exchange: str, symbol: str, market_type: str = "futures") -> OrderBookQuote:
    exchange = normalize_exchange(exchange)
    symbol = normalize_symbol(symbol)
    market_type = normalize_market_type(market_type)
    with http_client() as client:
        if exchange == "bn":
            market_symbol = f"{symbol}USDT"
            url = f"{spot_base_url('bn')}/api/v3/depth" if market_type == "spot" else f"{base_url('bn')}/fapi/v1/depth"
            payload = request_json(client, url, {"symbol": market_symbol, "limit": 5})
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, payload.get("bids"), payload.get("asks")))
        if exchange == "by":
            market_symbol = f"{symbol}USDT"
            category = "spot" if market_type == "spot" else "linear"
            payload = request_json(client, f"{base_url('by')}/v5/market/orderbook", {"category": category, "symbol": market_symbol, "limit": 2})
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, result.get("b"), result.get("a")))
        if exchange == "gt":
            if market_type == "spot":
                payload = request_json(client, f"{spot_base_url('gt')}/api/v4/spot/order_book", {"currency_pair": f"{symbol}_USDT", "limit": 2})
            else:
                payload = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/order_book", {"contract": f"{symbol}_USDT", "limit": 2})
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, payload.get("bids"), payload.get("asks")))
        if exchange == "okx":
            inst_id = f"{symbol}-USDT" if market_type == "spot" else f"{symbol}-USDT-SWAP"
            payload = okx_data(request_json(client, f"{base_url('okx')}/api/v5/market/books", {"instId": inst_id, "sz": 2}))
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, payload.get("bids"), payload.get("asks")))
        if exchange == "bg":
            market_symbol = f"{symbol}USDT"
            if market_type == "spot":
                payload = bitget_data(
                    request_json(client, f"{spot_base_url('bg')}/api/v2/spot/market/orderbook", {"symbol": market_symbol, "type": "step0", "limit": 20})
                )
            else:
                payload = bitget_data(
                    request_json(client, f"{base_url('bg')}/api/v2/mix/market/orderbook", {"symbol": market_symbol, "productType": "USDT-FUTURES", "limit": 5})
                )
            payload = payload if isinstance(payload, dict) else {}
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, payload.get("bids"), payload.get("asks")))
        if exchange == "htx":
            if market_type == "spot":
                payload = request_json(client, f"{spot_base_url('htx')}/market/depth", {"symbol": f"{symbol.lower()}usdt", "type": "step0"})
            else:
                payload = request_json(client, f"{base_url('htx')}/linear-swap-ex/market/depth", {"contract_code": f"{symbol}-USDT", "type": "step0"})
            tick = payload.get("tick") if isinstance(payload, dict) else {}
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, (tick or {}).get("bids"), (tick or {}).get("asks")))
        if exchange == "as":
            if market_type == "spot":
                raise ValueError("Aster 现货盘口暂未接入")
            market_symbol = f"{symbol}USDT"
            payload = request_json(client, f"{base_url('as')}/fapi/v1/depth", {"symbol": market_symbol, "limit": 5})
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, payload.get("bids"), payload.get("asks")))
        if exchange == "hl":
            if market_type == "spot":
                raise ValueError("Hyperliquid 现货盘口暂未接入")
            payload = request_post_json(client, f"{base_url('hl')}/info", {"type": "l2Book", "coin": symbol})
            levels = payload.get("levels") if isinstance(payload, dict) else []
            bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
            asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
            return attach_turnover_4h(client, order_book_quote(exchange, symbol, market_type, bids, asks))
    raise ValueError(f"{EXCHANGE_NAMES.get(exchange, exchange)} 盘口暂未接入")


def fetch_order_book_safe(exchange: str, symbol: str, market_type: str = "futures") -> OrderBookQuote:
    try:
        return fetch_order_book(exchange, symbol, market_type)
    except Exception as exc:
        return OrderBookQuote(
            exchange=normalize_exchange(exchange),
            symbol=normalize_symbol(symbol),
            market_type=normalize_market_type(market_type),
            amount_unit=amount_unit_for(normalize_market_type(market_type)),
            bids=order_book_levels([], "bid"),
            asks=order_book_levels([], "ask"),
            updated_at=datetime.now(timezone.utc),
            status="error",
            error=str(exc),
        )


def fetch_spot_market(client: httpx.Client, exchange: str, symbol: str) -> MarketQuote:
    if exchange == "bn":
        return fetch_binance_spot(client, symbol)
    if exchange == "by":
        return fetch_bybit_spot(client, symbol)
    if exchange == "gt":
        return fetch_gate_spot(client, symbol)
    if exchange == "okx":
        return fetch_okx_spot(client, symbol)
    if exchange == "bg":
        return fetch_bitget_spot(client, symbol)
    if exchange == "htx":
        return fetch_htx_spot(client, symbol)
    raise ValueError(f"{EXCHANGE_NAMES.get(exchange, exchange)} 现货盘口暂未接入")


def fetch_binance_spot(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    ticker = request_json(
        client,
        f"{spot_base_url('bn')}/api/v3/ticker/bookTicker",
        {"symbol": market_symbol},
    )
    stats: dict[str, Any] = {}
    try:
        stats = request_json(client, f"{spot_base_url('bn')}/api/v3/ticker/24hr", {"symbol": market_symbol})
    except Exception:
        stats = {}
    return MarketQuote(
        exchange="bn",
        symbol=symbol,
        market_type="spot",
        best_bid=parse_float(ticker.get("bidPrice")),
        best_ask=parse_float(ticker.get("askPrice")),
        volume_24h=parse_float(stats.get("quoteVolume")),
        updated_at=datetime.now(timezone.utc),
    )


def fetch_bybit_spot(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    order_book = request_json(
        client,
        f"{spot_base_url('by')}/v5/market/orderbook",
        {"category": "spot", "symbol": market_symbol, "limit": 1},
    )
    ticker = bybit_first(
        request_json(
            client,
            f"{spot_base_url('by')}/v5/market/tickers",
            {"category": "spot", "symbol": market_symbol},
        )
    )
    result = order_book.get("result", {}) if isinstance(order_book, dict) else {}
    bid = parse_float(((result.get("b") or [[None]])[0] or [None])[0])
    ask = parse_float(((result.get("a") or [[None]])[0] or [None])[0])
    return MarketQuote(
        exchange="by",
        symbol=symbol,
        market_type="spot",
        best_bid=bid or parse_float(ticker.get("bid1Price")),
        best_ask=ask or parse_float(ticker.get("ask1Price")),
        volume_24h=parse_float(ticker.get("turnover24h")),
        updated_at=datetime.now(timezone.utc),
    )


def fetch_gate_spot(client: httpx.Client, symbol: str) -> MarketQuote:
    currency_pair = f"{symbol}_USDT"
    order_book = request_json(
        client,
        f"{spot_base_url('gt')}/api/v4/spot/order_book",
        {"currency_pair": currency_pair, "limit": 1},
    )
    ticker: dict[str, Any] = {}
    try:
        ticker_rows = request_json(client, f"{spot_base_url('gt')}/api/v4/spot/tickers", {"currency_pair": currency_pair})
        if isinstance(ticker_rows, list) and ticker_rows:
            ticker = ticker_rows[0]
    except Exception:
        ticker = {}
    return MarketQuote(
        exchange="gt",
        symbol=symbol,
        market_type="spot",
        best_bid=gate_price((order_book.get("bids") or [None])[0]) or parse_float(ticker.get("highest_bid")),
        best_ask=gate_price((order_book.get("asks") or [None])[0]) or parse_float(ticker.get("lowest_ask")),
        volume_24h=parse_float(ticker.get("quote_volume")),
        updated_at=datetime.now(timezone.utc),
    )


def fetch_okx_spot(client: httpx.Client, symbol: str) -> MarketQuote:
    inst_id = f"{symbol}-USDT"
    order_book = okx_data(
        request_json(client, f"{spot_base_url('okx')}/api/v5/market/books", {"instId": inst_id, "sz": 1})
    )
    ticker: dict[str, Any] = {}
    try:
        ticker = okx_data(request_json(client, f"{spot_base_url('okx')}/api/v5/market/ticker", {"instId": inst_id}))
    except Exception:
        ticker = {}
    return MarketQuote(
        exchange="okx",
        symbol=symbol,
        market_type="spot",
        best_bid=parse_float(((order_book.get("bids") or [[None]])[0] or [None])[0]) or parse_float(ticker.get("bidPx")),
        best_ask=parse_float(((order_book.get("asks") or [[None]])[0] or [None])[0]) or parse_float(ticker.get("askPx")),
        volume_24h=parse_float(ticker.get("volCcy24h")),
        updated_at=datetime.now(timezone.utc),
    )


def fetch_bitget_spot(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    ticker = bitget_data(
        request_json(
            client,
            f"{spot_base_url('bg')}/api/v2/spot/market/tickers",
            {"symbol": market_symbol},
        )
    )
    row = ticker[0] if isinstance(ticker, list) and ticker else (ticker if isinstance(ticker, dict) else {})
    return MarketQuote(
        exchange="bg",
        symbol=symbol,
        market_type="spot",
        best_bid=parse_float(row.get("bidPr")),
        best_ask=parse_float(row.get("askPr")),
        volume_24h=parse_float(row.get("quoteVolume")) or parse_float(row.get("usdtVolume")),
        updated_at=parse_timestamp_ms(row.get("ts")) or datetime.now(timezone.utc),
    )


def fetch_htx_spot(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol.lower()}usdt"
    depth = request_json(
        client,
        f"{spot_base_url('htx')}/market/depth",
        {"symbol": market_symbol, "type": "step0"},
    )
    if not isinstance(depth, dict) or depth.get("status") != "ok":
        raise ValueError(f"HTX 现货返回异常: {depth}")
    tick = depth.get("tick") or {}
    bid = parse_float(((tick.get("bids") or [[None]])[0] or [None])[0])
    ask = parse_float(((tick.get("asks") or [[None]])[0] or [None])[0])
    if bid is None or ask is None:
        raise ValueError("HTX 现货盘口为空")
    return MarketQuote(
        exchange="htx",
        symbol=symbol,
        market_type="spot",
        best_bid=bid,
        best_ask=ask,
        updated_at=datetime.now(timezone.utc),
    )


def fetch_binance(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    ticker = request_json(
        client,
        f"{base_url('bn')}/fapi/v1/ticker/bookTicker",
        {"symbol": market_symbol},
    )
    premium = request_json(
        client,
        f"{base_url('bn')}/fapi/v1/premiumIndex",
        {"symbol": market_symbol},
    )
    stats: dict[str, Any] = {}
    open_interest: dict[str, Any] = {}
    try:
        stats = request_json(client, f"{base_url('bn')}/fapi/v1/ticker/24hr", {"symbol": market_symbol})
    except Exception:
        stats = {}
    try:
        open_interest = request_json(client, f"{base_url('bn')}/fapi/v1/openInterest", {"symbol": market_symbol})
    except Exception:
        open_interest = {}
    funding_info = binance_funding_info(client, "bn", market_symbol)
    mark_price = parse_float(premium.get("markPrice"))
    index_price = parse_float(premium.get("indexPrice"))
    period_hours = first_float(funding_info.get("fundingIntervalHours"), 8)
    max_rate = first_float(funding_info.get("adjustedFundingRateCap"), 0.0075)
    min_rate = first_float(funding_info.get("adjustedFundingRateFloor"), -0.0075)
    interest_rate = parse_float(premium.get("interestRate"))
    return MarketQuote(
        exchange="bn",
        symbol=symbol,
        best_bid=parse_float(ticker.get("bidPrice")),
        best_ask=parse_float(ticker.get("askPrice")),
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(premium.get("lastFundingRate")),
        premium_rate=computed_premium(mark_price, index_price),
        next_funding_time=parse_timestamp_ms(premium.get("nextFundingTime")),
        period_hours=period_hours,
        **_funding_period_evidence(
            period_hours,
            (
                "fundingInfo.fundingIntervalHours",
                None if funding_info.get("_funding_info_response_malformed") else funding_info.get("fundingIntervalHours"),
            ),
            (
                "official_default_8h_after_successful_adjustment_list",
                8 if funding_info.get("_official_default_8h_verified") is True else None,
            ),
        ),
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        interest_rate=interest_rate,
        funding_rule=f"{period_hours:g}h {funding_range_text(min_rate, max_rate)}",
        funding_formula="Binance USD-M: funding = clamp(premium + clamp(interest - premium, +/-0.05%), floor/cap)",
        premium_source="mark/index proxy; cap/floor from fundingInfo when adjusted, otherwise Binance default",
        open_interest=parse_float(open_interest.get("openInterest")),
        volume_24h=parse_float(stats.get("quoteVolume")),
        updated_at=datetime.now(timezone.utc),
    )


def _validated_astro_quote_books(
    relay_payload: Any,
    requested: dict[str, set[str]],
    observed_at: datetime,
    max_age_seconds: float,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    accepted: dict[str, dict[str, dict[str, Any]]] = {venue: {} for venue in requested}
    snapshot = relay_payload.get("snapshot") if isinstance(relay_payload, dict) else None
    raw_books = snapshot.get("books") if isinstance(snapshot, dict) else None
    if not isinstance(raw_books, dict):
        raise ValueError("Astro 行情桥快照格式异常")
    rejected: list[str] = []
    for venue, symbols in requested.items():
        venue_rows = raw_books.get(venue)
        if not isinstance(venue_rows, dict):
            continue
        for symbol in symbols:
            row = venue_rows.get(symbol)
            if not isinstance(row, dict):
                continue
            bid = parse_float(row.get("bid"))
            ask = parse_float(row.get("ask"))
            received_at = parse_datetime_value(row.get("receivedAt"))
            updated_at = parse_datetime_value(row.get("sourceUpdatedAt")) or received_at
            age_seconds = (observed_at - received_at).total_seconds() if received_at else math.inf
            if (
                bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask
                or age_seconds < -10 or age_seconds > max_age_seconds
            ):
                rejected.append(f"{venue}:{symbol}")
                continue
            accepted[venue][symbol] = {
                "bid": bid,
                "ask": ask,
                "updatedAt": updated_at or observed_at,
                "source": str(row.get("source") or f"Astro Cloud WS · {venue.upper()} {symbol}"),
            }
    return accepted, rejected


def arb_radar_executable_books(
    pair_ids: set[str] | None = None,
    astro_consumer_id: str | None = None,
    astro_revision: int | None = None,
) -> dict[str, Any]:
    """Return the radar's crypto books through one local backend request."""
    now = datetime.now(timezone.utc)
    pair_books: dict[str, dict[str, set[str]]] = {
        "kstr": {"bn": {"KSTRUSDT"}},
        "minimax-bn": {"bn": {"MINIMAXUSDT"}},
        "cxmt-hl": {"hl": {"CXMT"}},
        "cxmt-gt": {"gt": {"CXMT_USDT"}},
        "cxmt-bn": {"bn": {"CXMTUSDT"}},
        "gigadev": {"hl": {"GIGADEV"}},
        "unitree-gt": {"gt": {"UNITREE_USDT"}},
        "unitree-bn": {"bn": {"UNITREEUSDT"}},
        "spcx-ob": {"bn": {"SPCXUSDT"}},
        "sndk-ob": {"bn": {"SNDKUSDT"}},
        "dram-ob": {"bn": {"DRAMUSDT"}},
        "mu-ob": {"bn": {"MUUSDT"}},
        "skhy-ob": {"bn": {"SKHYUSDT"}},
        "hood-ob": {"bn": {"HOODUSDT"}},
        "intc-ob": {"bn": {"INTCUSDT"}},
        "mstr-ob": {"bn": {"MSTRUSDT"}},
        "bot-og": {"gt": {"BOT_USDT"}},
        "skhy-bg": {"bg": {"SKHYNIXUSDT", "SKHYUSDT"}},
    }
    selected_pair_ids = set(pair_books) if pair_ids is None else pair_ids & set(pair_books)
    cache_key = tuple(sorted(selected_pair_ids))
    requested: dict[str, set[str]] = {"bn": set(), "bg": set(), "gt": set(), "hl": set()}
    for pair_id in selected_pair_ids:
        for venue, symbols in pair_books[pair_id].items():
            requested[venue].update(symbols)

    def astro_enabled() -> bool:
        return os.getenv("ASTRO_QUOTE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}

    # An enabled Astro bridge must receive every visible-card revision, including
    # the empty set. Reusing a quote cache before that control update could leave
    # a deleted card subscribed on the cloud host.
    if not astro_enabled():
        with _arb_radar_book_cache_lock:
            cached = _arb_radar_book_cache.get(cache_key)
            if cached and (now - cached[0]).total_seconds() < ARB_RADAR_BOOK_CACHE_SECONDS:
                return deepcopy(cached[1])

    def load_astro() -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
        empty: dict[str, dict[str, dict[str, Any]]] = {venue: {} for venue in requested}
        if not astro_enabled():
            return empty, {
                "enabled": False,
                "usedBooks": 0,
                "status": "idle" if not any(requested.values()) else "disabled",
            }
        port = int(os.getenv("ASTRO_QUOTE_BRIDGE_PORT", "8765"))
        max_age_seconds = max(
            0.5,
            min(
                60.0,
                first_float(
                    os.getenv("ASTRO_QUOTE_MAX_AGE_MS"),
                    ARB_RADAR_ASTRO_DEFAULT_MAX_AGE_SECONDS * 1000,
                ) / 1000,
            ),
        )
        requested_targets = sorted(
            f"{venue}:{symbol}"
            for venue, symbols in requested.items()
            for symbol in symbols
        )
        consumer_id = astro_consumer_id or "arb-radar-backend"
        revision = astro_revision if astro_revision is not None else time.time_ns()
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=2.2, trust_env=False) as client:
                response = client.get(
                    f"http://127.0.0.1:{port}/books",
                    params={
                        "maxAgeMs": int(max_age_seconds * 1000),
                        "targets": ",".join(requested_targets),
                        "consumer": consumer_id,
                        "revision": revision,
                    },
                )
                response.raise_for_status()
                relay_payload = response.json()
            snapshot = relay_payload.get("snapshot") if isinstance(relay_payload, dict) else None
            applied_targets = snapshot.get("targets") if isinstance(snapshot, dict) else None
            if applied_targets != requested_targets and astro_consumer_id is not None:
                # The bridge can union targets from multiple live radar tabs. It
                # still must contain this tab's exact requested targets.
                if not isinstance(applied_targets, list) or not set(requested_targets).issubset(applied_targets):
                    raise ValueError("Astro 行情订阅目标尚未同步")
            empty, rejected = _validated_astro_quote_books(
                relay_payload,
                requested,
                now,
                max_age_seconds,
            )
            accepted = sum(len(rows) for rows in empty.values())
            return empty, {
                "enabled": True,
                "status": "ok",
                "usedBooks": accepted,
                "rejectedBooks": rejected,
                "relayAgeMs": relay_payload.get("relayAgeMs"),
                "cloudRttMs": relay_payload.get("cloudRttMs"),
                "requestMs": round((time.perf_counter() - started) * 1000, 3),
                "gatewayConnections": snapshot.get("connections") if isinstance(snapshot, dict) else None,
                "selectedTargets": requested_targets,
                "appliedTargets": applied_targets,
                "activeConnectionCount": snapshot.get("activeConnectionCount") if isinstance(snapshot, dict) else None,
                "marketRequestCount": snapshot.get("marketRequestCount") if isinstance(snapshot, dict) else None,
                "subscriptionSynchronized": relay_payload.get("subscriptionSynchronized"),
            }
        except Exception as exc:  # noqa: BLE001
            return empty, {
                "enabled": True,
                "status": "fallback",
                "usedBooks": 0,
                "error": str(exc),
                "requestMs": round((time.perf_counter() - started) * 1000, 3),
            }

    venue_books, astro_status = load_astro()

    def load_binance() -> dict[str, dict[str, Any]]:
        missing = requested["bn"] - set(venue_books["bn"])
        if not missing:
            return {}
        books: dict[str, dict[str, Any]] = {}
        with http_client(timeout=6.0) as client:
            for symbol in sorted(missing):
                row = request_json(
                    client,
                    f"{base_url('bn')}/fapi/v1/ticker/bookTicker",
                    {"symbol": symbol},
                )
                if not isinstance(row, dict):
                    continue
                bid = parse_float(row.get("bidPrice"))
                ask = parse_float(row.get("askPrice"))
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue
                books[symbol] = {
                    "bid": bid,
                    "ask": ask,
                    "updatedAt": parse_timestamp_ms(row.get("time")) or now,
                    "source": f"Binance {symbol} · 本地后台",
                }
        return books

    def load_bitget() -> dict[str, dict[str, Any]]:
        missing = requested["bg"] - set(venue_books["bg"])
        if not missing:
            return {}
        books: dict[str, dict[str, Any]] = {}
        with http_client(timeout=6.0) as client:
            for symbol in sorted(missing):
                payload = bitget_data(
                    request_json(
                        client,
                        f"{base_url('bg')}/api/v2/mix/market/ticker",
                        {"symbol": symbol, "productType": "USDT-FUTURES"},
                    )
                )
                row = payload[0] if isinstance(payload, list) and payload else None
                if not isinstance(row, dict):
                    continue
                bid = parse_float(row.get("bidPr"))
                ask = parse_float(row.get("askPr"))
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue
                books[symbol] = {
                    "bid": bid,
                    "ask": ask,
                    "updatedAt": parse_timestamp_ms(row.get("ts")) or now,
                    "source": f"Bitget {symbol} · 本地后台",
                }
        return books

    def load_gate() -> dict[str, dict[str, Any]]:
        books: dict[str, dict[str, Any]] = {}
        missing = requested["gt"] - set(venue_books["gt"])
        if not missing:
            return books
        with http_client(timeout=6.0) as client:
            for contract in missing:
                payload = request_json(
                    client,
                    f"{base_url('gt')}/api/v4/futures/usdt/order_book",
                    {"contract": contract, "limit": 1},
                )
                bids = payload.get("bids") if isinstance(payload, dict) else None
                asks = payload.get("asks") if isinstance(payload, dict) else None
                bid = order_book_level("bid", 1, bids[0] if isinstance(bids, list) and bids else None).price
                ask = order_book_level("ask", 1, asks[0] if isinstance(asks, list) and asks else None).price
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue
                timestamp = first_float(payload.get("update"), payload.get("current"))
                books[contract] = {
                    "bid": bid,
                    "ask": ask,
                    "updatedAt": datetime.fromtimestamp(timestamp, timezone.utc) if timestamp else now,
                    "source": f"Gate {contract} · 本地后台",
                }
        return books

    def load_hyperliquid() -> dict[str, dict[str, Any]]:
        books: dict[str, dict[str, Any]] = {}
        missing = requested["hl"] - set(venue_books["hl"])
        if not missing:
            return books
        with http_client(timeout=6.0) as client:
            for coin in missing:
                payload = request_post_json(
                    client,
                    f"{base_url('hl')}/info",
                    {"type": "l2Book", "coin": f"xyz:{coin}"},
                )
                levels = payload.get("levels") if isinstance(payload, dict) else None
                bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else None
                asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else None
                bid = order_book_level("bid", 1, bids[0] if isinstance(bids, list) and bids else None).price
                ask = order_book_level("ask", 1, asks[0] if isinstance(asks, list) and asks else None).price
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue
                books[coin] = {
                    "bid": bid,
                    "ask": ask,
                    "updatedAt": parse_timestamp_ms(payload.get("time")) or now,
                    "source": f"Hyperliquid xyz:{coin} · 本地后台",
                }
        return books

    loaders = {
        "bn": load_binance,
        "bg": load_bitget,
        "gt": load_gate,
        "hl": load_hyperliquid,
    }
    loaders = {venue: loader for venue, loader in loaders.items() if requested[venue]}
    errors: list[str] = []
    if loaders:
        with ThreadPoolExecutor(max_workers=len(loaders)) as executor:
            future_map = {executor.submit(loader): venue for venue, loader in loaders.items()}
            for future in as_completed(future_map):
                venue = future_map[future]
                try:
                    venue_books[venue].update(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{EXCHANGE_NAMES[venue]}：{exc}")

    for venue, symbols in requested.items():
        missing = sorted(symbols - set(venue_books[venue]))
        if missing:
            errors.append(f"{EXCHANGE_NAMES[venue]} 未返回：{', '.join(missing)}")
    payload = {
        "status": "ok" if not errors else "partial_error",
        "observedAt": now,
        "books": venue_books,
        "errors": errors,
        "astro": astro_status,
    }
    if any(venue_books.values()) and not astro_enabled():
        with _arb_radar_book_cache_lock:
            _arb_radar_book_cache[cache_key] = (now, deepcopy(payload))
    return payload


def _binance_p2p_median_price(payload: Any) -> tuple[float, int]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    prices: list[float] = []
    for row in rows if isinstance(rows, list) else []:
        adv = row.get("adv") if isinstance(row, dict) else None
        price = parse_float(adv.get("price")) if isinstance(adv, dict) else None
        if price is not None and price > 0:
            prices.append(price)
        if len(prices) >= 10:
            break
    if len(prices) < 3:
        raise ValueError("Binance P2P 有效 USDT/CNY 报价不足")
    return float(median(prices)), len(prices)


def arb_radar_usdt_cny() -> dict[str, Any]:
    """Return a neutral executable USDT/CNY reference for KSTR sizing.

    The Node rendering service cannot reliably reach Binance P2P under the
    LaunchAgent network path.  Keep the exchange request in this backend,
    which shares the proxy discovery used by the other crypto quote sources.
    """
    global _arb_radar_usdt_cny_cache
    now = datetime.now(timezone.utc)
    with _arb_radar_usdt_cny_cache_lock:
        cached = _arb_radar_usdt_cny_cache
        if cached and (now - cached[0]).total_seconds() < ARB_RADAR_USDT_CNY_CACHE_SECONDS:
            return deepcopy(cached[1])

    endpoint = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

    def load(trade_type: str) -> tuple[float, int]:
        request_payload = {
            "page": 1,
            "rows": 10,
            "payTypes": [],
            "asset": "USDT",
            "tradeType": trade_type,
            "fiat": "CNY",
            "publisherType": None,
            "merchantCheck": False,
        }
        with http_client(timeout=8.0) as client:
            response = client.post(endpoint, json=request_payload)
            response.raise_for_status()
            return _binance_p2p_median_price(response.json())

    with ThreadPoolExecutor(max_workers=2) as executor:
        buy_future = executor.submit(load, "BUY")
        sell_future = executor.submit(load, "SELL")
        buy, buy_count = buy_future.result()
        sell, sell_count = sell_future.result()

    payload = {
        "status": "ok",
        "value": (buy + sell) / 2,
        "buy": buy,
        "sell": sell,
        "updatedAt": now,
        "source": "Binance P2P前10档中位数",
        "sampleCount": {"buy": buy_count, "sell": sell_count},
    }
    with _arb_radar_usdt_cny_cache_lock:
        _arb_radar_usdt_cny_cache = (now, deepcopy(payload))
    return payload


def kstr_twenty_day_price_baseline(
    etf_rows: dict[int, float],
    kstr_rows: list[dict[str, Any]],
    target_days: int = 20,
    minimum_days: int | None = None,
    pair_label: str = "KSTR/588000",
) -> dict[str, Any]:
    """Calculate a two-stage rolling median from six synchronized daily points.

    The result is KSTR USDT price per one yuan of 588000.  The live USDT/CNY
    quote is applied by the radar route so its displayed shares-per-KSTR ratio
    and its executable spread always use the same current conversion rate.

    A daily median is calculated first so every A-share trading day has equal
    weight.  The final center is the median of the latest ``target_days`` daily
    medians.  This avoids letting a day with more surviving five-minute bars
    dominate the execution baseline.
    """
    shanghai = ZoneInfo("Asia/Shanghai")
    by_day: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for timestamp, close in sorted(etf_rows.items()):
        if close <= 0:
            continue
        local = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone(shanghai)
        by_day[local.date().isoformat()].append((timestamp, close))
    kstr_by_ts = {
        int(row["ts"]): float(row["close"])
        for row in kstr_rows
        if row.get("ts") is not None
        and first_float(row.get("close")) is not None
        and float(row["close"]) > 0
    }
    target_time_set = set(ARB_RADAR_KSTR_BASELINE_SAMPLE_TIMES)
    eligible_days: list[dict[str, Any]] = []
    for day, rows in sorted(by_day.items()):
        if (
            len(rows) < 48
            or datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc)
            .astimezone(shanghai)
            .strftime("%H:%M")
            not in {"14:55", "15:00"}
        ):
            continue
        samples: list[float] = []
        sampled_times: list[str] = []
        for timestamp, etf_close in rows:
            local_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone(shanghai).strftime("%H:%M")
            if local_time not in target_time_set:
                continue
            kstr_close = kstr_by_ts.get(timestamp)
            if kstr_close is None:
                continue
            samples.append(kstr_close / etf_close)
            sampled_times.append(local_time)
        if len(samples) < ARB_RADAR_KSTR_BASELINE_MIN_DAILY_SAMPLES:
            continue
        eligible_days.append(
            {
                "date": day,
                "median": float(median(samples)),
                "sampleCount": len(samples),
                "sampleTimes": sampled_times,
            }
        )

    normalized_target_days = max(1, int(target_days))
    required_days = normalized_target_days if minimum_days is None else max(1, min(int(minimum_days), normalized_target_days))
    selected_days = eligible_days[-normalized_target_days:]
    if len(selected_days) < required_days:
        raise ValueError(
            f"{pair_label}六点同步交易日不足：{len(selected_days)}/{required_days}"
        )
    daily_medians = [float(row["median"]) for row in selected_days]
    center = float(median(daily_medians))
    daily_mad = float(median(abs(value - center) for value in daily_medians))
    sample_count = sum(int(row["sampleCount"]) for row in selected_days)
    return {
        "status": "ok",
        "method": "daily_six_point_median_then_twenty_day_median",
        "pairLabel": pair_label,
        "targetTradingDays": normalized_target_days,
        "provisional": len(selected_days) < normalized_target_days,
        "contractUsdPerEtfCnyMedian": center,
        "contractUsdPerEtfCnyDailyMad": daily_mad,
        "contractUsdPerEtfCnyRobustSigma": daily_mad * 1.4826,
        "tradingDays": len(selected_days),
        "dailyMedianCount": len(daily_medians),
        "sampleCount": sample_count,
        "sampleTimes": list(ARB_RADAR_KSTR_BASELINE_SAMPLE_TIMES),
        "minimumSamplesPerDay": ARB_RADAR_KSTR_BASELINE_MIN_DAILY_SAMPLES,
        "startDate": selected_days[0]["date"],
        "endDate": selected_days[-1]["date"],
        "updatedAt": datetime.now(timezone.utc),
    }


def arb_radar_kstr_baseline() -> dict[str, Any]:
    global _arb_radar_kstr_baseline_cache
    now = datetime.now(timezone.utc)
    with _arb_radar_kstr_baseline_cache_lock:
        cached = _arb_radar_kstr_baseline_cache
        if cached and (now - cached[0]).total_seconds() < ARB_RADAR_KSTR_BASELINE_CACHE_SECONDS:
            return deepcopy(cached[1])

    start_ms = int((now - timedelta(days=32)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    with http_client(timeout=28.0) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            etf_future = executor.submit(sina_intraday_close_rows, client, "sh588000", 5)
            kstr_future = executor.submit(
                contract_candle_rows,
                client,
                "bn",
                "KSTR",
                "5m",
                11000,
                start_ms,
                end_ms,
            )
            payload = kstr_twenty_day_price_baseline(etf_future.result(), kstr_future.result(), 20)
    with _arb_radar_kstr_baseline_cache_lock:
        _arb_radar_kstr_baseline_cache = (now, deepcopy(payload))
    return payload


def arb_radar_unitree_baseline(venue: str) -> dict[str, Any]:
    normalized_venue = pair_spread_exchange(venue)
    if normalized_venue not in {"gt", "bn"}:
        raise ValueError("宇树基线只支持 Gate 或 Binance")
    now = datetime.now(timezone.utc)
    with _arb_radar_unitree_baseline_cache_lock:
        cached = _arb_radar_unitree_baseline_cache.get(normalized_venue)
        if cached and (now - cached[0]).total_seconds() < ARB_RADAR_KSTR_BASELINE_CACHE_SECONDS:
            return deepcopy(cached[1])

    start_ms = int((now - timedelta(days=32)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    with http_client(timeout=28.0) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stock_future = executor.submit(sina_intraday_close_rows, client, "sh688836", 5)
            contract_future = executor.submit(
                contract_candle_rows,
                client,
                normalized_venue,
                "UNITREE",
                "5m",
                11000,
                start_ms,
                end_ms,
            )
            payload = kstr_twenty_day_price_baseline(
                stock_future.result(),
                contract_future.result(),
                target_days=20,
                minimum_days=3,
                pair_label=f"{EXCHANGE_NAMES[normalized_venue]} UNITREE/688836",
            )
    payload = {
        **payload,
        "venue": normalized_venue,
        "contractUsdPerStockCnyMedian": payload["contractUsdPerEtfCnyMedian"],
    }
    with _arb_radar_unitree_baseline_cache_lock:
        _arb_radar_unitree_baseline_cache[normalized_venue] = (now, deepcopy(payload))
    return payload


def arb_radar_unitree_history(venue: str, range_hours: int) -> dict[str, Any]:
    normalized_venue = pair_spread_exchange(venue)
    if normalized_venue not in {"gt", "bn"}:
        raise ValueError("宇树历史差价只支持 Gate 或 Binance")
    if range_hours not in {24, 72, 168, 720}:
        raise ValueError("宇树历史差价只支持1天、3天、7天或30天")
    granularity = "1m" if range_hours <= 24 else "5m" if range_hours <= 168 else "15m"
    source_granularity = "1m" if granularity == "1m" else "5m"
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(hours=range_hours)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    baseline = arb_radar_unitree_baseline(normalized_venue)
    reference_price_ratio = float(baseline["contractUsdPerStockCnyMedian"])
    with http_client(timeout=28.0) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stock_future = executor.submit(sina_intraday_close_rows, client, "sh688836", 1 if source_granularity == "1m" else 5)
            contract_future = executor.submit(
                contract_candle_rows,
                client,
                normalized_venue,
                "UNITREE",
                source_granularity,
                11000,
                start_ms,
                end_ms,
            )
            stock_rows = stock_future.result()
            contract_rows = contract_future.result()

    contract_by_ts = {
        int(row["ts"]): float(row["close"])
        for row in contract_rows
        if row.get("ts") is not None and first_float(row.get("close")) is not None and float(row["close"]) > 0
    }
    aligned = [
        {
            "timestamp": timestamp,
            "contractClose": contract_by_ts[timestamp],
            "stockClose": stock_close,
        }
        for timestamp, stock_close in sorted(stock_rows.items())
        if timestamp >= start_ms and timestamp in contract_by_ts
    ]
    if granularity == "15m":
        buckets: dict[int, dict[str, float | int]] = {}
        bucket_ms = 15 * 60_000
        for row in aligned:
            bucket = int(row["timestamp"]) // bucket_ms * bucket_ms
            buckets[bucket] = {**row, "timestamp": bucket}
        aligned = [buckets[key] for key in sorted(buckets)]
    points = []
    for row in aligned:
        contract_close = float(row["contractClose"])
        stock_equivalent = float(row["stockClose"]) * reference_price_ratio
        spread_pct = pair_spread_symmetric_pct(contract_close, stock_equivalent)
        if spread_pct is None:
            continue
        points.append({
            "timestamp": int(row["timestamp"]),
            "leftPrice": contract_close,
            "rightPrice": stock_equivalent,
            "spreadPct": spread_pct,
        })
    if len(points) < 2:
        raise ValueError(f"{EXCHANGE_NAMES[normalized_venue]} UNITREE与688836没有足够的同步历史行情")
    return {
        "rangeHours": range_hours,
        "granularity": granularity,
        "venue": normalized_venue,
        "referencePriceRatio": reference_price_ratio,
        "referenceTradingDays": baseline["tradingDays"],
        "provisional": baseline["provisional"],
        "points": points,
    }


def bybit_first(payload: Any) -> dict[str, Any]:
    data = payload.get("result", {}).get("list") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError(f"Bybit 返回空数据: {payload}")
    return data[0]


def fetch_bybit(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    order_book = request_json(
        client,
        f"{base_url('by')}/v5/market/orderbook",
        {"category": "linear", "symbol": market_symbol, "limit": 1},
    )
    ticker = bybit_first(
        request_json(
            client,
            f"{base_url('by')}/v5/market/tickers",
            {"category": "linear", "symbol": market_symbol},
        )
    )
    instrument = bybit_instrument_info(client, market_symbol)
    result = order_book.get("result", {}) if isinstance(order_book, dict) else {}
    bid = parse_float(((result.get("b") or [[None]])[0] or [None])[0])
    ask = parse_float(((result.get("a") or [[None]])[0] or [None])[0])
    mark_price = parse_float(ticker.get("markPrice"))
    index_price = parse_float(ticker.get("indexPrice"))
    interval_minutes = first_float(instrument.get("fundingInterval"))
    period_hours = interval_minutes / 60 if interval_minutes else first_float(ticker.get("fundingIntervalHour"), 8)
    symmetric_cap = first_float(ticker.get("fundingCap"), instrument.get("fundingCap"))
    max_rate = first_float(instrument.get("upperFundingRate"), positive_cap(symmetric_cap))
    min_rate = first_float(instrument.get("lowerFundingRate"), negative_cap(symmetric_cap))
    return MarketQuote(
        exchange="by",
        symbol=symbol,
        best_bid=bid or parse_float(ticker.get("bid1Price")),
        best_ask=ask or parse_float(ticker.get("ask1Price")),
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(ticker.get("fundingRate")),
        premium_rate=computed_premium(mark_price, index_price),
        next_funding_time=parse_timestamp_ms(ticker.get("nextFundingTime")),
        period_hours=period_hours,
        **_funding_period_evidence(
            period_hours,
            ("instruments-info.fundingInterval", interval_minutes / 60 if interval_minutes else None),
            ("tickers.fundingIntervalHour", ticker.get("fundingIntervalHour")),
        ),
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        interest_rate=0.0 if symbol == "USDC" else 0.0003 / (24 / period_hours),
        funding_rule=f"{period_hours:g}h {funding_range_text(min_rate, max_rate)}" if period_hours else funding_range_text(min_rate, max_rate),
        funding_formula="Bybit linear: interval from instruments-info; cap/floor from upper/lowerFundingRate or ticker fundingCap",
        premium_source="mark/index proxy",
        open_interest=parse_float(ticker.get("openInterest")),
        volume_24h=parse_float(ticker.get("turnover24h")),
        updated_at=datetime.now(timezone.utc),
    )


def gate_price(row: Any) -> float | None:
    if isinstance(row, dict):
        return parse_float(row.get("p"))
    if isinstance(row, list) and row:
        return parse_float(row[0])
    return None


def first_level_price(rows: Any) -> float | None:
    if not isinstance(rows, list) or not rows:
        return None
    return gate_price(rows[0])


def fetch_gate(client: httpx.Client, symbol: str) -> MarketQuote:
    contract = f"{symbol}_USDT"
    order_book = request_json(
        client,
        f"{base_url('gt')}/api/v4/futures/usdt/order_book",
        {"contract": contract, "limit": 1},
    )
    contract_info = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/contracts/{contract}")
    ticker: dict[str, Any] = {}
    try:
        ticker_rows = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/tickers", {"contract": contract})
        if isinstance(ticker_rows, list) and ticker_rows:
            ticker = ticker_rows[0]
    except Exception:
        ticker = {}
    mark_price = parse_float(contract_info.get("mark_price"))
    index_price = parse_float(contract_info.get("index_price"))
    cap = parse_float(contract_info.get("funding_rate_limit"))
    period_hours = (parse_float(contract_info.get("funding_interval")) or 0) / 3600 or None
    premium_rate = parse_float(contract_info.get("funding_rate_indicative")) or computed_premium(mark_price, index_price)
    return MarketQuote(
        exchange="gt",
        symbol=symbol,
        best_bid=gate_price((order_book.get("bids") or [None])[0]) or parse_float(ticker.get("highest_bid")),
        best_ask=gate_price((order_book.get("asks") or [None])[0]) or parse_float(ticker.get("lowest_ask")),
        mark_price=mark_price or parse_float(ticker.get("mark_price")),
        index_price=index_price or parse_float(ticker.get("index_price")),
        funding_rate=parse_float(contract_info.get("funding_rate")) or parse_float(ticker.get("funding_rate")),
        premium_rate=premium_rate,
        next_funding_time=parse_timestamp_s(contract_info.get("funding_next_apply")),
        period_hours=period_hours,
        **_funding_period_evidence(period_hours, ("contracts.funding_interval", period_hours)),
        max_funding_rate=positive_cap(cap),
        min_funding_rate=negative_cap(cap),
        interest_rate=0.0 if str(contract_info.get("contract_type") or "").lower() in {"stocks", "metals", "indices", "forex", "commodities"} else 0.0001,
        funding_rule=f"{period_hours:g}h {funding_range_text(negative_cap(cap), positive_cap(cap))}" if period_hours else funding_range_text(negative_cap(cap), positive_cap(cap)),
        funding_formula="Gate USDT futures: cap is symmetric funding_rate_limit; premium uses funding_rate_indicative when available",
        premium_source="funding_rate_indicative",
        open_interest=parse_float(contract_info.get("position_size")) or parse_float(ticker.get("total_size")),
        volume_24h=parse_float(ticker.get("volume_24h_quote")) or parse_float(ticker.get("volume_24h_settle")),
        updated_at=datetime.now(timezone.utc),
    )


def okx_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("code") not in (None, "0"):
        raise ValueError(f"OKX 返回异常: {payload}")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("OKX 返回空数据")
    return data[0]


def okx_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("code") not in (None, "0"):
        raise ValueError(f"OKX 返回异常: {payload}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("OKX 返回数据格式异常")
    return [row for row in data if isinstance(row, dict)]


def fetch_okx(client: httpx.Client, symbol: str) -> MarketQuote:
    inst_id = f"{symbol}-USDT-SWAP"
    index_inst_id = f"{symbol}-USDT"
    order_book = okx_data(
        request_json(client, f"{base_url('okx')}/api/v5/market/books", {"instId": inst_id, "sz": 1})
    )
    funding = okx_data(
        request_json(client, f"{base_url('okx')}/api/v5/public/funding-rate", {"instId": inst_id})
    )
    mark = okx_data(
        request_json(client, f"{base_url('okx')}/api/v5/public/mark-price", {"instType": "SWAP", "instId": inst_id})
    )
    ticker = okx_data(
        request_json(client, f"{base_url('okx')}/api/v5/market/ticker", {"instId": inst_id})
    )
    index = okx_data(
        request_json(client, f"{base_url('okx')}/api/v5/market/index-tickers", {"instId": index_inst_id})
    )
    mark_price = parse_float(mark.get("markPx"))
    index_price = parse_float(index.get("idxPx"))
    best_bid = parse_float(((order_book.get("bids") or [[None]])[0] or [None])[0])
    best_ask = parse_float(((order_book.get("asks") or [[None]])[0] or [None])[0])
    max_rate = parse_float(funding.get("maxFundingRate"))
    min_rate = parse_float(funding.get("minFundingRate"))
    period_hours = okx_period_hours(funding) or 8
    premium_rate = first_float(funding.get("premium"), computed_premium(mark_price, index_price))
    formula_type = funding.get("formulaType") or funding.get("method") or "current_period"
    return MarketQuote(
        exchange="okx",
        symbol=symbol,
        best_bid=best_bid,
        best_ask=best_ask,
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(funding.get("fundingRate")),
        premium_rate=premium_rate,
        next_funding_time=parse_timestamp_ms(funding.get("fundingTime"))
        or parse_timestamp_ms(funding.get("nextFundingTime")),
        period_hours=period_hours,
        **_funding_period_evidence(period_hours, ("funding-rate.fundingTime_nextFundingTime", okx_period_hours(funding))),
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        interest_rate=parse_float(funding.get("interestRate")),
        funding_rule=f"{period_hours:g}h {funding_range_text(min_rate, max_rate)}",
        funding_formula=f"OKX swap: formulaType={formula_type}; max/min from public funding-rate",
        premium_source="OKX premium field when present, otherwise mark/index proxy",
        volume_24h=parse_float(ticker.get("volCcy24h")) or parse_float(ticker.get("vol24h")),
        updated_at=datetime.now(timezone.utc),
    )


def bitget_data(payload: Any) -> Any:
    if not isinstance(payload, dict) or payload.get("code") not in ("00000", None):
        raise ValueError(f"Bitget 返回异常: {payload}")
    return payload.get("data")


def fetch_bitget(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    order_book = bitget_data(
        request_json(
            client,
            f"{base_url('bg')}/api/v2/mix/market/orderbook",
            {"symbol": market_symbol, "productType": "USDT-FUTURES", "limit": 1},
        )
    )
    ticker = bitget_data(
        request_json(
            client,
            f"{base_url('bg')}/api/v2/mix/market/ticker",
            {"symbol": market_symbol, "productType": "USDT-FUTURES"},
        )
    )
    funding = bitget_data(
        request_json(
            client,
            f"{base_url('bg')}/api/v2/mix/market/current-fund-rate",
            {"symbol": market_symbol, "productType": "USDT-FUTURES"},
        )
    )
    contract_info = bitget_contract_info(client, market_symbol)
    ticker_row = ticker[0] if isinstance(ticker, list) and ticker else (ticker if isinstance(ticker, dict) else {})
    funding_row = funding[0] if isinstance(funding, list) and funding else (funding if isinstance(funding, dict) else {})
    best_bid = first_level_price((order_book or {}).get("bids"))
    best_ask = first_level_price((order_book or {}).get("asks"))
    mark_price = parse_float(ticker_row.get("markPrice"))
    index_price = parse_float(ticker_row.get("indexPrice"))
    period_hours = first_float(
        funding_row.get("fundingRateInterval"),
        funding_row.get("fundingRateIntervalHour"),
        contract_info.get("fundingRateInterval"),
        contract_info.get("fundInterval"),
        8,
    )
    max_rate = first_float(
        funding_row.get("maxFundingRate"),
        contract_info.get("maxFundingRate"),
        contract_info.get("fundingRateLimit"),
    )
    min_rate = first_float(
        funding_row.get("minFundingRate"),
        contract_info.get("minFundingRate"),
        negative_cap(max_rate),
    )
    return MarketQuote(
        exchange="bg",
        symbol=symbol,
        best_bid=best_bid or parse_float(ticker_row.get("bidPr")),
        best_ask=best_ask or parse_float(ticker_row.get("askPr")),
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(funding_row.get("fundingRate")),
        premium_rate=computed_premium(mark_price, index_price),
        next_funding_time=parse_timestamp_ms(funding_row.get("nextUpdate")),
        period_hours=period_hours,
        **_funding_period_evidence(
            period_hours,
            ("current-fund-rate.fundingRateInterval", funding_row.get("fundingRateInterval")),
            ("current-fund-rate.fundingRateIntervalHour", funding_row.get("fundingRateIntervalHour")),
            ("contracts.fundingRateInterval", contract_info.get("fundingRateInterval")),
            ("contracts.fundInterval", contract_info.get("fundInterval")),
        ),
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        interest_rate=0.0001,
        funding_rule=f"{period_hours:g}h {funding_range_text(min_rate, max_rate)}" if period_hours else funding_range_text(min_rate, max_rate),
        funding_formula="Bitget USDT-FUTURES: current-fund-rate plus contracts config for interval and cap/floor",
        premium_source="mark/index proxy",
        open_interest=parse_float(ticker_row.get("openInterest")),
        volume_24h=parse_float(ticker_row.get("quoteVolume")),
        updated_at=datetime.now(timezone.utc),
    )


def funding_premium_history_row(
    timestamp: datetime | None,
    open_value: Any,
    high_value: Any,
    low_value: Any,
    close_value: Any,
    *,
    represented_samples: int = 1,
) -> dict[str, Any] | None:
    values = [parse_float(value) for value in (open_value, high_value, low_value, close_value)]
    if timestamp is None or any(value is None for value in values):
        return None
    return {
        "timestamp": timestamp,
        "open": values[0],
        "high": values[1],
        "low": values[2],
        "close": values[3],
        "representedSamples": represented_samples,
    }


def fetch_funding_premium_history(
    client: httpx.Client,
    exchange: str,
    symbol: str,
    history_start: datetime,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    start_ms = int(history_start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    rows: list[dict[str, Any]] = []

    if normalized_exchange == "bn":
        payload = request_json(
            client,
            f"{base_url('bn')}/fapi/v1/premiumIndexKlines",
            {
                "symbol": f"{normalized_symbol}USDT",
                "interval": "1m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        for raw in payload if isinstance(payload, list) else []:
            if not isinstance(raw, list) or len(raw) < 5:
                continue
            row = funding_premium_history_row(
                parse_timestamp_ms(raw[0]),
                raw[1],
                raw[2],
                raw[3],
                raw[4],
                represented_samples=12,
            )
            if row:
                rows.append(row)
        return rows, {
            "source": "Binance /fapi/v1/premiumIndexKlines",
            "publicResolutionSeconds": 60,
            "officialSampleSeconds": 5,
            "precision": "bounded_1m_ohlc",
        }

    if normalized_exchange == "by":
        payload = request_json(
            client,
            f"{base_url('by')}/v5/market/premium-index-price-kline",
            {
                "category": "linear",
                "symbol": f"{normalized_symbol}USDT",
                "interval": "1",
                "start": start_ms,
                "end": end_ms,
                "limit": 1000,
            },
        )
        data = payload.get("result", {}).get("list") if isinstance(payload, dict) else []
        for raw in data if isinstance(data, list) else []:
            if not isinstance(raw, list) or len(raw) < 5:
                continue
            row = funding_premium_history_row(
                parse_timestamp_ms(raw[0]),
                raw[1],
                raw[2],
                raw[3],
                raw[4],
            )
            if row:
                rows.append(row)
        return rows, {
            "source": "Bybit /v5/market/premium-index-price-kline",
            "publicResolutionSeconds": 60,
            "officialSampleSeconds": 60,
            "precision": "official_1m",
        }

    if normalized_exchange == "gt":
        payload = request_json(
            client,
            f"{base_url('gt')}/api/v4/futures/usdt/premium_index",
            {
                "contract": f"{normalized_symbol}_USDT",
                "from": int(history_start.timestamp()),
                "to": int(now.timestamp()),
                "interval": "1m",
                "limit": 1000,
            },
        )
        for raw in payload if isinstance(payload, list) else []:
            if not isinstance(raw, dict):
                continue
            row = funding_premium_history_row(
                parse_timestamp_s(raw.get("t")),
                raw.get("o"),
                raw.get("h"),
                raw.get("l"),
                raw.get("c"),
            )
            if row:
                rows.append(row)
        return rows, {
            "source": "Gate /api/v4/futures/usdt/premium_index",
            "publicResolutionSeconds": 60,
            "officialSampleSeconds": 60,
            "precision": "official_1m",
        }

    if normalized_exchange == "okx":
        inst_id = f"{normalized_symbol}-USDT-SWAP"
        after: str | None = None
        seen_timestamps: set[int] = set()
        for _ in range(8):
            params: dict[str, Any] = {"instId": inst_id, "limit": 100}
            if after:
                params["after"] = after
            payload = request_json(
                client,
                f"{base_url('okx')}/api/v5/public/premium-history",
                params,
            )
            page = okx_rows(payload)
            if not page:
                break
            oldest_ms: int | None = None
            for raw in page:
                timestamp_ms = int(float(raw.get("ts") or 0))
                if timestamp_ms <= 0 or timestamp_ms in seen_timestamps:
                    continue
                seen_timestamps.add(timestamp_ms)
                oldest_ms = timestamp_ms if oldest_ms is None else min(oldest_ms, timestamp_ms)
                value = parse_float(raw.get("premium"))
                timestamp = parse_timestamp_ms(timestamp_ms)
                if timestamp is None or value is None or timestamp < history_start or timestamp > now:
                    continue
                rows.append(
                    {
                        "timestamp": timestamp,
                        "open": value,
                        "high": value,
                        "low": value,
                        "close": value,
                        "representedSamples": 1,
                    }
                )
            if oldest_ms is None or oldest_ms <= start_ms:
                break
            after = str(oldest_ms)
        return rows, {
            "source": "OKX /api/v5/public/premium-history",
            "publicResolutionSeconds": 60,
            "officialSampleSeconds": 60,
            "precision": "official_samples",
        }

    if normalized_exchange == "bg":
        # Bitget excludes startTime. Align pages and overlap the boundary by
        # one millisecond so a rolling window does not lose a minute per page.
        cursor_ms = (start_ms // 60_000) * 60_000
        seen_timestamps: set[int] = set()
        while cursor_ms < end_ms:
            page_end_ms = min(end_ms, cursor_ms + 99 * 60 * 1000 - 1)
            payload = request_json(
                client,
                f"{base_url('bg')}/api/v3/market/candles",
                {
                    "category": "USDT-FUTURES",
                    "symbol": f"{normalized_symbol}USDT",
                    "interval": "1m",
                    "type": "premium",
                    "startTime": cursor_ms - 1,
                    "endTime": page_end_ms,
                    "limit": 100,
                },
            )
            data = bitget_data(payload)
            for raw in data if isinstance(data, list) else []:
                if not isinstance(raw, list) or len(raw) < 5:
                    continue
                timestamp_ms = int(float(raw[0]))
                if timestamp_ms in seen_timestamps:
                    continue
                seen_timestamps.add(timestamp_ms)
                row = funding_premium_history_row(
                    parse_timestamp_ms(timestamp_ms),
                    raw[1],
                    raw[2],
                    raw[3],
                    raw[4],
                    represented_samples=12,
                )
                if row:
                    rows.append(row)
            cursor_ms = page_end_ms + 1
        return rows, {
            "source": "Bitget /api/v3/market/candles?type=premium",
            "publicResolutionSeconds": 60,
            "officialSampleSeconds": 5,
            "precision": "bounded_1m_ohlc",
        }

    raise ValueError("该交易所暂未接入资金费形成监控")


def funding_formation_formula_text(exchange: str, interval_hours: float, formula_type: str | None) -> str:
    if exchange == "by":
        return (
            "F = clamp(P̄ + clamp(I−P̄, ±0.05%), floor, cap)；"
            f"I = 0.03% ÷ (24/{interval_hours:g})，按分钟线性加权。"
        )
    if exchange == "bn":
        weighting = "1小时周期等权" if math.isclose(interval_hours, 1.0) else "越接近结算权重越高"
        return (
            "F = clamp((P̄ + clamp(I−P̄, ±0.05%)) ÷ (8/N), floor, cap)；"
            f"每5秒采样，{weighting}。"
        )
    if exchange == "bg":
        return (
            "F = clamp((P̄ + clamp(I−P̄, ±0.05%)) ÷ (8/N), floor, cap)；"
            "每5秒采样并按1至n递增加权；当前参考值使用最近N小时滚动窗口。"
        )
    if exchange == "gt":
        return (
            "当前主值暂保留旧公式作对照；Gate官方影子值为每60秒先计算 "
            "p + clamp(I−p, ±0.05%)，再对本周期做算术平均并限幅。"
        )
    if exchange == "okx" and str(formula_type or "").lower() == "norate":
        return "OKX formulaType=noRate：不使用 8/N，新版迁移前旧公式。"
    return (
        "F = clamp((P̄ + clamp(I−P̄, ±0.05%)) ÷ (8/N), floor, cap)；"
        "每分钟采样，越接近结算权重越高。"
    )


def funding_formation_directional_target(
    current_funding_rate: Any,
    max_funding_rate: Any,
    custom_target_rate: float | None = None,
) -> tuple[list[dict[str, Any]], float | None, float | None]:
    max_rate = first_float(max_funding_rate)
    max_abs = abs(max_rate) if max_rate is not None else None
    floor = -max_abs if max_abs is not None else None
    cap = max_abs
    if custom_target_rate is not None:
        return (
            [
                {
                    "key": "custom",
                    "label": "自定义目标",
                    "targetFundingRate": custom_target_rate,
                }
            ],
            floor,
            cap,
        )
    if max_abs is None:
        return [], floor, cap
    current_rate = first_float(current_funding_rate)
    negative_direction = current_rate is not None and current_rate < 0
    target_rate = -max_abs if negative_direction else max_abs
    return (
        [
            {
                "key": "floor" if negative_direction else "cap",
                "label": "负向最大费率" if negative_direction else "正向最大费率",
                "targetFundingRate": target_rate,
            }
        ],
        floor,
        cap,
    )


def stale_funding_formation_payload(
    cached: tuple[datetime, dict[str, Any]] | None,
    *,
    now: datetime,
    error: Exception | str,
) -> dict[str, Any] | None:
    if cached is None:
        return None
    cached_at, payload = cached
    age_seconds = max(0.0, (now - cached_at).total_seconds())
    if age_seconds > FUNDING_FORMATION_STALE_MAX_AGE_SECONDS:
        return None
    result = deepcopy(payload)
    result["status"] = "partial_error"
    result["stale"] = True
    result["staleAgeSeconds"] = age_seconds
    result["predictedFundingRate"] = None
    result["predictedAveragePremiumRate"] = None
    result["predictionSensitivityLow"] = None
    result["predictionSensitivityHigh"] = None
    result["robustPredictedFundingRate"] = None
    result["predictionStatus"] = "insufficient"
    result["predictionConfidence"] = "low"
    result["predictionMessage"] = "上游刷新失败，旧预测已暂停展示；历史观测仅供参考。"
    result["errorMessage"] = f"上游刷新失败，暂用最近有效结果：{error}"
    return result


def _prune_funding_formation_cache(now: datetime) -> None:
    stale_cutoff = now - timedelta(seconds=FUNDING_FORMATION_STALE_MAX_AGE_SECONDS)
    expired_keys = [
        key
        for key, (cached_at, _payload) in _funding_formation_cache.items()
        if cached_at < stale_cutoff
    ]
    for key in expired_keys:
        _funding_formation_cache.pop(key, None)
    while len(_funding_formation_cache) > FUNDING_FORMATION_CACHE_MAX_ITEMS:
        _funding_formation_cache.popitem(last=False)


def funding_formation_cache_status() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _funding_formation_cache_lock:
        _prune_funding_formation_cache(now)
        return {
            "itemCount": len(_funding_formation_cache),
            "maxItems": FUNDING_FORMATION_CACHE_MAX_ITEMS,
            "freshSeconds": FUNDING_FORMATION_CACHE_SECONDS,
            "staleFallbackSeconds": FUNDING_FORMATION_STALE_MAX_AGE_SECONDS,
        }


def funding_formation_overview(
    exchange: str,
    symbol: str,
    custom_target_rate: float | None = None,
) -> dict[str, Any]:
    with api_rate_limit_lane(FUNDING_FORMATION_API_LANE):
        return _funding_formation_overview(
            exchange,
            symbol,
            custom_target_rate=custom_target_rate,
        )


def _funding_formation_overview(
    exchange: str,
    symbol: str,
    custom_target_rate: float | None = None,
) -> dict[str, Any]:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    if normalized_exchange not in {"bn", "by", "gt", "okx", "bg"}:
        raise ValueError("资金费形成监控仅支持 Binance、Bybit、Gate、OKX、Bitget")
    if custom_target_rate is not None and (
        not math.isfinite(custom_target_rate) or abs(custom_target_rate) > 1
    ):
        raise ValueError("自定义目标资金费率必须在 -100% 到 +100% 之间")

    cache_key = (normalized_exchange, normalized_symbol, custom_target_rate)
    now = datetime.now(timezone.utc)
    with _funding_formation_cache_lock:
        _prune_funding_formation_cache(now)
        cached = _funding_formation_cache.get(cache_key)
        if cached is not None:
            _funding_formation_cache.move_to_end(cache_key)
    if cached and (now - cached[0]).total_seconds() < FUNDING_FORMATION_CACHE_SECONDS:
        return deepcopy(cached[1])

    quote = fetch_market(normalized_exchange, normalized_symbol, "futures")
    if quote.status != "ok":
        stale = stale_funding_formation_payload(
            cached,
            now=now,
            error=quote.error or "交易所实时合约数据读取失败",
        )
        if stale is not None:
            return stale
        raise ValueError(quote.error or "交易所实时合约数据读取失败")
    interval_hours = first_float(quote.period_hours)
    next_funding_time = quote.next_funding_time
    if interval_hours is None or next_funding_time is None:
        raise ValueError("交易所未返回当前资金费周期或下一结算时间")
    interval_delta = timedelta(hours=interval_hours)
    while next_funding_time <= now:
        next_funding_time += interval_delta
    cycle_start = next_funding_time - interval_delta
    # Bitget's current estimate uses a trailing N-hour reference window.  Fetch
    # enough history to rebuild that value, while the final-settlement forecast
    # below still counts only samples that remain in the active cycle.
    history_start = min(cycle_start, now - interval_delta) if normalized_exchange == "bg" else cycle_start
    formula_type = (
        "noRate"
        if quote.funding_formula and "formulaType=noRate" in quote.funding_formula
        else "withRate" if normalized_exchange == "okx" else None
    )
    rule = funding_formation_rule(
        normalized_exchange,
        interval_hours,
        interest_rate=quote.interest_rate,
        formula_type=formula_type,
    )

    try:
        with http_client(timeout=12.0) as client:
            premium_rows, history_meta = fetch_funding_premium_history(
                client,
                normalized_exchange,
                normalized_symbol,
                history_start,
                now,
            )
    except httpx.HTTPError as exc:
        stale = stale_funding_formation_payload(
            cached,
            now=now,
            error=exc,
        )
        if stale is not None:
            return stale
        raise

    targets, effective_floor, effective_cap = funding_formation_directional_target(
        quote.funding_rate,
        quote.max_funding_rate,
        custom_target_rate,
    )

    calculation = analyze_funding_formation(
        rule=rule,
        cycle_start=cycle_start,
        cycle_end=next_funding_time,
        now=now,
        premium_rows=premium_rows,
        targets=targets,
        floor=effective_floor,
        cap=effective_cap,
    )
    rolling_reference = None
    if normalized_exchange == "bg":
        rolling_reference = analyze_rolling_reference(
            rule=rule,
            window_start=now - interval_delta,
            window_end=now,
            premium_rows=premium_rows,
            floor=effective_floor,
            cap=effective_cap,
        )
    shadow_calculation = None
    if normalized_exchange == "gt":
        shadow_calculation = analyze_gate_shadow(
            rule=gate_shadow_rule(interval_hours),
            cycle_start=cycle_start,
            cycle_end=next_funding_time,
            now=now,
            premium_rows=premium_rows,
            floor=effective_floor,
            cap=effective_cap,
        )
    latest_row = max(premium_rows, key=lambda row: row["timestamp"]) if premium_rows else None
    minutes_to_funding = max(0.0, (next_funding_time - now).total_seconds() / 60)
    precision = history_meta["precision"]
    history_status = calculation.get("historyStatus")
    if history_status == "insufficient":
        accuracy_status = "insufficient"
        accuracy_message = (
            f"本周期公开溢价历史覆盖 {calculation['coverage']:.1%}、"
            f"加权覆盖 {calculation['weightedCoverage']:.1%}，数据不足，暂不反推阈值。"
        )
    elif history_status == "estimated":
        accuracy_status = "estimated"
        accuracy_message = (
            f"本周期公开溢价历史覆盖 {calculation['coverage']:.1%}、"
            f"加权覆盖 {calculation['weightedCoverage']:.1%}；"
            "少量缺口按已覆盖样本的加权均值补齐，当前阈值为估算，数据完整后自动切换为严格值。"
        )
    elif precision == "bounded_1m_ohlc":
        accuracy_status = "bounded"
        accuracy_message = (
            "交易所内部每5秒采样，但公开历史为1分钟OHLC；"
            "只能给出收盘值估计与按分钟高低值计算的边界，不标记为5秒级精确值。"
        )
    else:
        accuracy_status = "official"
        accuracy_message = "按交易所公开溢价样本、当前周期和官方权重计算。"

    payload = {
        "status": "ok",
        "exchange": normalized_exchange,
        "exchangeName": EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange),
        "symbol": normalized_symbol,
        "currentFundingRate": quote.funding_rate,
        "latestPremiumRate": latest_row["close"] if latest_row else None,
        "latestPremiumCandleTime": latest_row["timestamp"] if latest_row else None,
        "latestPremiumKind": "one_minute_candle_close",
        "fundingIntervalHours": interval_hours,
        "nextFundingTime": next_funding_time,
        "cycleStartTime": cycle_start,
        "historyWindowStartTime": history_start,
        "historyWindowEndTime": now,
        "minutesToFunding": minutes_to_funding,
        "maxFundingRate": quote.max_funding_rate,
        "minFundingRate": quote.min_funding_rate,
        "interestRate": rule.interest_rate,
        "intervalScale": rule.interval_scale,
        "weighting": rule.weighting,
        "officialSampleSeconds": rule.sample_seconds,
        "formulaVersion": rule.formula_version,
        "ruleWindowMode": rule.window_mode,
        "aggregationMode": rule.aggregation_mode,
        "formula": funding_formation_formula_text(
            normalized_exchange,
            interval_hours,
            formula_type,
        ),
        "formulaType": formula_type,
        "ruleSourceUrl": FUNDING_FORMATION_RULE_SOURCES[normalized_exchange],
        "historySource": history_meta["source"],
        "publicResolutionSeconds": history_meta["publicResolutionSeconds"],
        "historyPrecision": precision,
        "premiumAverageHistory": build_cycle_premium_average_history(
            premium_rows,
            rule=rule,
            cycle_start=cycle_start,
            now=now,
        ),
        "effectiveFundingFloor": effective_floor,
        "effectiveFundingCap": effective_cap,
        "rollingReference": rolling_reference,
        "shadowCalculation": shadow_calculation,
        "accuracyStatus": accuracy_status,
        "accuracyMessage": accuracy_message,
        "updatedAt": now,
        **calculation,
    }
    with _funding_formation_cache_lock:
        _funding_formation_cache[cache_key] = (now, deepcopy(payload))
        _funding_formation_cache.move_to_end(cache_key)
        _prune_funding_formation_cache(now)
    return payload


def funding_cap_rate_changed(previous: Any, current: Any) -> bool:
    previous_rate = first_float(previous)
    current_rate = first_float(current)
    if previous_rate is None or current_rate is None:
        return False
    return not math.isclose(previous_rate, current_rate, rel_tol=0.0, abs_tol=1e-10)


def funding_interval_changed(previous: Any, current: Any) -> bool:
    previous_hours = first_float(previous)
    current_hours = first_float(current)
    if previous_hours is None or current_hours is None:
        return False
    return not math.isclose(
        previous_hours,
        current_hours,
        rel_tol=0.0,
        abs_tol=1e-8,
    )


def funding_cap_check_message(max_rate: float | None, _min_rate: float | None, period_hours: float | None) -> str:
    period_text = f"{period_hours:g}h" if period_hours else "周期未知"
    if max_rate is None:
        return f"{period_text}，交易所暂未返回资金费上限"
    return f"最大费率 {rate_pct_text(max_rate)}，周期 {period_text}"


def fetch_funding_cap_check(
    client: httpx.Client,
    exchange: str,
    symbol: str,
    market_symbols: dict[str, set[str]],
) -> dict[str, Any]:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    checked_at = datetime.now(timezone.utc)
    if normalized_symbol not in market_symbols.get(normalized_exchange, set()):
        return {
            "exchange": normalized_exchange,
            "status": "not_supported",
            "message": f"{EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)} 暂无 {normalized_symbol} 永续合约",
            "maxFundingRate": None,
            "minFundingRate": None,
            "fundingIntervalHours": None,
            "checkedAt": checked_at,
        }
    try:
        max_rate: float | None = None
        min_rate: float | None = None
        period_hours: float | None = None
        if normalized_exchange == "bn":
            market_symbol = f"{normalized_symbol}USDT"
            rows = request_json(client, f"{base_url('bn')}/fapi/v1/fundingInfo")
            if not isinstance(rows, list):
                raise ValueError("Binance 资金费配置格式异常")
            info = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict) and str(row.get("symbol") or "") == market_symbol
                ),
                {},
            )
            max_rate = first_float(info.get("adjustedFundingRateCap"), 0.0075)
            min_rate = first_float(info.get("adjustedFundingRateFloor"), -0.0075)
            period_hours = first_float(info.get("fundingIntervalHours"), 8)
        elif normalized_exchange == "by":
            market_symbol = f"{normalized_symbol}USDT"
            payload = request_json(
                client,
                f"{base_url('by')}/v5/market/instruments-info",
                {"category": "linear", "symbol": market_symbol},
            )
            rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                raise ValueError("Bybit 未返回资金费配置")
            info = rows[0]
            symmetric_cap = first_float(info.get("fundingCap"))
            max_rate = first_float(info.get("upperFundingRate"), positive_cap(symmetric_cap))
            min_rate = first_float(info.get("lowerFundingRate"), negative_cap(symmetric_cap))
            interval_minutes = first_float(info.get("fundingInterval"))
            period_hours = interval_minutes / 60 if interval_minutes else first_float(info.get("fundingIntervalHour"), 8)
        elif normalized_exchange == "gt":
            contract = f"{normalized_symbol}_USDT"
            info = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/contracts/{contract}")
            if not isinstance(info, dict):
                raise ValueError("Gate 未返回资金费配置")
            cap = first_float(info.get("funding_rate_limit"))
            max_rate = positive_cap(cap)
            min_rate = negative_cap(cap)
            interval_seconds = first_float(info.get("funding_interval"))
            period_hours = interval_seconds / 3600 if interval_seconds else None
        elif normalized_exchange == "okx":
            inst_id = f"{normalized_symbol}-USDT-SWAP"
            funding = okx_data(
                request_json(
                    client,
                    f"{base_url('okx')}/api/v5/public/funding-rate",
                    {"instId": inst_id},
                )
            )
            max_rate = first_float(funding.get("maxFundingRate"))
            min_rate = first_float(funding.get("minFundingRate"))
            period_hours = okx_period_hours(funding) or 8
        elif normalized_exchange == "bg":
            market_symbol = f"{normalized_symbol}USDT"
            funding_payload = bitget_data(
                request_json(
                    client,
                    f"{base_url('bg')}/api/v2/mix/market/current-fund-rate",
                    {"symbol": market_symbol, "productType": "USDT-FUTURES"},
                )
            )
            funding = (
                funding_payload[0]
                if isinstance(funding_payload, list) and funding_payload and isinstance(funding_payload[0], dict)
                else funding_payload if isinstance(funding_payload, dict) else {}
            )
            contract_payload = bitget_data(
                request_json(
                    client,
                    f"{base_url('bg')}/api/v2/mix/market/contracts",
                    {"productType": "USDT-FUTURES", "symbol": market_symbol},
                )
            )
            contract = (
                contract_payload[0]
                if isinstance(contract_payload, list) and contract_payload and isinstance(contract_payload[0], dict)
                else contract_payload if isinstance(contract_payload, dict) else {}
            )
            max_rate = first_float(
                funding.get("maxFundingRate"),
                contract.get("maxFundingRate"),
                contract.get("fundingRateLimit"),
            )
            min_rate = first_float(
                funding.get("minFundingRate"),
                contract.get("minFundingRate"),
                negative_cap(max_rate),
            )
            period_hours = first_float(
                funding.get("fundingRateInterval"),
                funding.get("fundingRateIntervalHour"),
                contract.get("fundingRateInterval"),
                contract.get("fundInterval"),
                8,
            )
        elif normalized_exchange == "as":
            market_symbol = f"{normalized_symbol}USDT"
            rows = request_json(client, f"{base_url('as')}/fapi/v1/fundingInfo")
            if not isinstance(rows, list):
                raise ValueError("Aster 资金费配置格式异常")
            info = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and str(row.get("symbol") or "") == market_symbol
                ),
                {},
            )
            if not info:
                raise ValueError("Aster 未返回该合约资金费配置")
            max_rate = first_float(
                info.get("fundingFeeCap"),
                info.get("adjustedFundingRateCap"),
                info.get("fundingRateCap"),
            )
            min_rate = first_float(
                info.get("fundingFeeFloor"),
                info.get("adjustedFundingRateFloor"),
                info.get("fundingRateFloor"),
                negative_cap(max_rate),
            )
            period_hours = first_float(
                info.get("fundingIntervalHours"),
                info.get("fundingIntervalHour"),
            )
        else:
            raise ValueError("暂未支持该交易所")
        status = (
            "ok"
            if max_rate is not None
            or min_rate is not None
            or period_hours is not None
            else "unknown"
        )
        return {
            "exchange": normalized_exchange,
            "status": status,
            "message": funding_cap_check_message(max_rate, min_rate, period_hours),
            "maxFundingRate": max_rate,
            "minFundingRate": min_rate,
            "fundingIntervalHours": period_hours,
            "checkedAt": checked_at,
        }
    except Exception as exc:
        return {
            "exchange": normalized_exchange,
            "status": "error",
            "message": f"{EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)} 资金费规则读取失败：{exc}",
            "maxFundingRate": None,
            "minFundingRate": None,
            "fundingIntervalHours": None,
            "checkedAt": checked_at,
        }


def funding_cap_snapshot_to_out(snapshot: CryptoFundingCapSnapshot) -> dict[str, Any]:
    return {
        "exchange": snapshot.exchange,
        "exchangeName": EXCHANGE_NAMES.get(snapshot.exchange, snapshot.exchange),
        "status": snapshot.status,
        "message": snapshot.message,
        "maxFundingRate": snapshot.max_funding_rate,
        "minFundingRate": snapshot.min_funding_rate,
        "fundingIntervalHours": snapshot.funding_interval_hours,
        "checkedAt": snapshot.checked_at,
    }


def funding_cap_event_to_out(event: CryptoFundingCapEvent) -> dict[str, Any]:
    change_kinds = funding_rule_change_kinds(
        event.previous_max_funding_rate,
        event.current_max_funding_rate,
        event.previous_min_funding_rate,
        event.current_min_funding_rate,
        event.previous_funding_interval_hours,
        event.funding_interval_hours,
    )
    return {
        "id": event.id,
        "symbol": event.symbol,
        "exchange": event.exchange,
        "exchangeName": EXCHANGE_NAMES.get(event.exchange, event.exchange),
        "previousMaxFundingRate": event.previous_max_funding_rate,
        "currentMaxFundingRate": event.current_max_funding_rate,
        "previousMinFundingRate": event.previous_min_funding_rate,
        "currentMinFundingRate": event.current_min_funding_rate,
        "fundingIntervalHours": event.funding_interval_hours,
        "previousFundingIntervalHours": event.previous_funding_interval_hours,
        "currentFundingIntervalHours": event.funding_interval_hours,
        "changeKinds": change_kinds,
        "pushed": event.pushed,
        "pushStatus": event.push_status,
        "pushMessage": event.push_message,
        "createdAt": event.created_at,
    }


def normalize_funding_cap_watch_exchanges(values: list[str] | tuple[str, ...]) -> list[str]:
    selected: set[str] = set()
    for value in values:
        exchange = normalize_exchange(str(value))
        if exchange not in FUNDING_CAP_WATCH_EXCHANGES:
            allowed = "、".join(EXCHANGE_NAMES.get(code, code) for code in FUNDING_CAP_WATCH_EXCHANGES)
            raise ValueError(f"资金费规则监控仅支持 {allowed}")
        selected.add(exchange)
    normalized = [exchange for exchange in FUNDING_CAP_WATCH_EXCHANGES if exchange in selected]
    if not normalized:
        raise ValueError("请至少选择一个交易所")
    return normalized


def funding_cap_watch_item_exchanges(item: CryptoFundingCapWatchItem) -> list[str]:
    try:
        raw = json.loads(item.exchanges_json or FUNDING_CAP_WATCH_DEFAULT_EXCHANGES_JSON)
    except (TypeError, ValueError):
        raw = list(FUNDING_CAP_WATCH_EXCHANGES)
    if not isinstance(raw, list):
        raw = list(FUNDING_CAP_WATCH_EXCHANGES)
    selected = {
        str(exchange).strip().lower()
        for exchange in raw
        if str(exchange).strip().lower() in FUNDING_CAP_WATCH_EXCHANGES
    }
    return [
        exchange
        for exchange in FUNDING_CAP_WATCH_EXCHANGES
        if exchange in selected
    ] or list(FUNDING_CAP_WATCH_EXCHANGES)


def funding_cap_watch_overview(db: Session) -> dict[str, Any]:
    items = list(
        db.scalars(
            select(CryptoFundingCapWatchItem)
            .where(CryptoFundingCapWatchItem.enabled.is_(True))
            .order_by(CryptoFundingCapWatchItem.created_at, CryptoFundingCapWatchItem.id)
        )
    )
    exchange_order = {exchange: index for index, exchange in enumerate(FUNDING_CAP_WATCH_EXCHANGES)}
    item_rows: list[dict[str, Any]] = []
    for item in items:
        selected_exchanges = funding_cap_watch_item_exchanges(item)
        snapshots = list(
            db.scalars(
                select(CryptoFundingCapSnapshot)
                .where(
                    CryptoFundingCapSnapshot.watch_item_id == item.id,
                    CryptoFundingCapSnapshot.exchange.in_(selected_exchanges),
                )
            )
        )
        snapshots.sort(key=lambda row: exchange_order.get(row.exchange, len(exchange_order)))
        item_rows.append(
            {
                "id": item.id,
                "symbol": item.symbol,
                "enabled": item.enabled,
                "lastCheckedAt": item.last_checked_at,
                "lastError": item.last_error,
                "createdAt": item.created_at,
                "selectedExchanges": selected_exchanges,
                "exchanges": [funding_cap_snapshot_to_out(snapshot) for snapshot in snapshots],
            }
        )
    events = list(
        db.scalars(
            select(CryptoFundingCapEvent)
            .order_by(desc(CryptoFundingCapEvent.created_at), desc(CryptoFundingCapEvent.id))
            .limit(20)
        )
    )
    recent_events = [
        event_out
        for event in events
        if (event_out := funding_cap_event_to_out(event))["changeKinds"]
    ]
    monitoring = bool(item_rows)
    return {
        "status": "ok",
        "monitoring": monitoring,
        "itemCount": len(item_rows),
        "exchanges": [
            {"code": exchange, "name": EXCHANGE_NAMES.get(exchange, exchange)}
            for exchange in FUNDING_CAP_WATCH_EXCHANGES
        ],
        "items": item_rows,
        "recentEvents": recent_events,
        "scan": dict(_funding_cap_watch_scan_state),
        "message": (
            f"正在监控 {len(item_rows)} 个币种；最大资金费上限变化时提醒，结算周期变化仅记录。"
            if monitoring
            else "未添加币种，当前不监控、不请求交易所、不发送提醒。"
        ),
    }


def funding_rule_change_kinds(
    previous_max: Any,
    current_max: Any,
    previous_min: Any,
    current_min: Any,
    previous_period_hours: Any,
    current_period_hours: Any,
) -> list[str]:
    changes: list[str] = []
    if funding_cap_rate_changed(previous_max, current_max):
        changes.append("cap")
    if funding_interval_changed(previous_period_hours, current_period_hours):
        changes.append("interval")
    return changes


def funding_period_text(value: Any) -> str:
    hours = first_float(value)
    return f"{hours:g}h" if hours is not None else "未知"


def funding_rule_change_push_body(
    symbol: str,
    exchange: str,
    previous_max: float | None,
    current_max: float | None,
    previous_min: float | None,
    current_min: float | None,
    previous_period_hours: float | None,
    current_period_hours: float | None,
    change_kinds: list[str],
) -> str:
    changes: list[str] = []
    if "cap" in change_kinds:
        if funding_cap_rate_changed(previous_max, current_max):
            changes.append(
                f"最大费率 {rate_pct_text(previous_max)} → {rate_pct_text(current_max)}"
            )
    if "interval" in change_kinds:
        changes.append(
            "结算周期 "
            f"{funding_period_text(previous_period_hours)} → "
            f"{funding_period_text(current_period_hours)}"
        )
    return (
        f"{EXCHANGE_NAMES.get(exchange, exchange)} {symbol} 资金费规则变动："
        + "；".join(changes)
        + "。"
    )


def refresh_funding_cap_watchlist(
    db: Session,
    *,
    push: bool = True,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    if not _funding_cap_watch_scan_lock.acquire(blocking=False):
        overview = funding_cap_watch_overview(db)
        overview["message"] = "资金费规则监控正在检查。"
        return overview
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    selected_symbols = {normalize_symbol(symbol) for symbol in symbols or [] if normalize_symbol(symbol)}
    changed_count = 0
    pushed_count = 0
    checked_count = 0
    try:
        _funding_cap_watch_scan_state.update(
            {
                "running": True,
                "startedAt": started_at,
                "finishedAt": None,
                "lastError": None,
                "checkedSymbolCount": 0,
                "changedCount": 0,
                "pushedCount": 0,
                "status": "running",
                "message": "正在检查最大资金费上限与结算周期。",
            }
        )
        query = select(CryptoFundingCapWatchItem).where(CryptoFundingCapWatchItem.enabled.is_(True))
        if selected_symbols:
            query = query.where(CryptoFundingCapWatchItem.symbol.in_(selected_symbols))
        items = list(db.scalars(query.order_by(CryptoFundingCapWatchItem.created_at, CryptoFundingCapWatchItem.id)))
        if not items:
            _funding_cap_watch_scan_state.update(
                {
                    "status": "idle",
                    "message": "未添加币种，当前不监控。",
                }
            )
            db.commit()
        else:
            item_exchanges = {
                item.id: funding_cap_watch_item_exchanges(item)
                for item in items
            }
            selected_exchanges = [
                exchange
                for exchange in FUNDING_CAP_WATCH_EXCHANGES
                if any(exchange in exchanges for exchanges in item_exchanges.values())
            ]
            market_symbols = fetch_fs_futures_market_symbols(selected_exchanges)
            with http_client(timeout=12.0) as client:
                for item in items:
                    checked_count += 1
                    errors: list[str] = []
                    for exchange in item_exchanges[item.id]:
                        check = fetch_funding_cap_check(client, exchange, item.symbol, market_symbols)
                        snapshot = db.scalar(
                            select(CryptoFundingCapSnapshot)
                            .where(
                                CryptoFundingCapSnapshot.watch_item_id == item.id,
                                CryptoFundingCapSnapshot.exchange == exchange,
                            )
                            .limit(1)
                        )
                        previous_max = snapshot.max_funding_rate if snapshot else None
                        previous_min = snapshot.min_funding_rate if snapshot else None
                        previous_period_hours = (
                            snapshot.funding_interval_hours if snapshot else None
                        )
                        current_max = first_float(check.get("maxFundingRate"))
                        current_min = first_float(check.get("minFundingRate"))
                        period_hours = first_float(check.get("fundingIntervalHours"))
                        change_kinds = (
                            funding_rule_change_kinds(
                                previous_max,
                                current_max,
                                previous_min,
                                current_min,
                                previous_period_hours,
                                period_hours,
                            )
                            if check.get("status") == "ok"
                            else []
                        )
                        if snapshot is None:
                            snapshot = CryptoFundingCapSnapshot(
                                watch_item_id=item.id,
                                symbol=item.symbol,
                                exchange=exchange,
                            )
                            db.add(snapshot)
                        snapshot.status = str(check.get("status") or "unknown")
                        snapshot.message = str(check.get("message") or "") or None
                        snapshot.checked_at = parse_datetime_value(check.get("checkedAt")) or datetime.now(timezone.utc)
                        if check.get("status") == "ok":
                            snapshot.max_funding_rate = current_max
                            snapshot.min_funding_rate = current_min
                            snapshot.funding_interval_hours = period_hours
                        snapshot.updated_at = datetime.now(timezone.utc)
                        if check.get("status") == "error":
                            errors.append(snapshot.message or f"{exchange} 读取失败")
                        if not change_kinds:
                            continue
                        changed_count += 1
                        push_change_kinds = [kind for kind in change_kinds if kind != "interval"]
                        if push_change_kinds:
                            push_body = funding_rule_change_push_body(
                                symbol=item.symbol,
                                exchange=exchange,
                                previous_max=previous_max,
                                current_max=current_max,
                                previous_min=previous_min,
                                current_min=current_min,
                                previous_period_hours=previous_period_hours,
                                current_period_hours=period_hours,
                                change_kinds=push_change_kinds,
                            )
                            push_status, push_message = send_bark_or_log(
                                enabled=push,
                                title=f"{item.symbol} 资金费规则变动",
                                body=push_body,
                                group="交易监控·资金费规则",
                                url=None,
                                disabled_message="本次仅记录变动，未发送提醒。",
                            )
                        else:
                            push_status = "suppressed"
                            push_message = "结算周期变化按规则仅记录，不发送提醒。"
                        pushed = push_status == "ok"
                        if pushed:
                            pushed_count += 1
                        db.add(
                            CryptoFundingCapEvent(
                                watch_item_id=item.id,
                                symbol=item.symbol,
                                exchange=exchange,
                                previous_max_funding_rate=previous_max,
                                current_max_funding_rate=current_max,
                                previous_min_funding_rate=previous_min,
                                current_min_funding_rate=current_min,
                                previous_funding_interval_hours=previous_period_hours,
                                funding_interval_hours=period_hours,
                                pushed=pushed,
                                push_status=push_status,
                                push_message=push_message,
                            )
                        )
                    item.last_checked_at = datetime.now(timezone.utc)
                    item.last_error = "；".join(errors) if errors else None
                    item.updated_at = datetime.now(timezone.utc)
            db.commit()
        _funding_cap_watch_scan_state.update(
            {
                "status": "ok" if items else "idle",
                "checkedSymbolCount": checked_count,
                "changedCount": changed_count,
                "pushedCount": pushed_count,
                "message": (
                    f"检查完成：{checked_count} 个币种，发现 {changed_count} 处资金费规则变化。"
                    if changed_count
                    else f"检查完成：{checked_count} 个币种，最大资金费上限和周期均无变化。"
                    if items
                    else "未添加币种，当前不监控。"
                ),
                "durationSeconds": round(time.monotonic() - started_monotonic, 2),
            }
        )
    except Exception as exc:
        db.rollback()
        _funding_cap_watch_scan_state.update(
            {
                "status": "error",
                "lastError": str(exc),
                "checkedSymbolCount": checked_count,
                "changedCount": changed_count,
                "pushedCount": pushed_count,
                "message": f"资金费规则检查失败：{exc}",
                "durationSeconds": round(time.monotonic() - started_monotonic, 2),
            }
        )
    finally:
        _funding_cap_watch_scan_state.update(
            {
                "running": False,
                "finishedAt": datetime.now(timezone.utc),
            }
        )
        _funding_cap_watch_scan_lock.release()
    return funding_cap_watch_overview(db)


def create_funding_cap_watch(db: Session, symbol: str, exchanges: list[str]) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("请输入币种")
    selected_exchanges = normalize_funding_cap_watch_exchanges(exchanges)
    existing = db.scalar(
        select(CryptoFundingCapWatchItem)
        .where(CryptoFundingCapWatchItem.symbol == normalized_symbol)
        .limit(1)
    )
    if existing:
        return update_funding_cap_watch_exchanges(db, normalized_symbol, selected_exchanges)
    count = len(
        list(
            db.scalars(
                select(CryptoFundingCapWatchItem.id)
                .where(CryptoFundingCapWatchItem.enabled.is_(True))
                .limit(FUNDING_CAP_WATCH_MAX_SYMBOLS)
            )
        )
    )
    if count >= FUNDING_CAP_WATCH_MAX_SYMBOLS:
        raise ValueError(f"最多监控 {FUNDING_CAP_WATCH_MAX_SYMBOLS} 个币种")
    db.add(
        CryptoFundingCapWatchItem(
            symbol=normalized_symbol,
            exchanges_json=json.dumps(selected_exchanges, separators=(",", ":")),
            enabled=True,
        )
    )
    db.commit()
    return refresh_funding_cap_watchlist(db, push=False, symbols=[normalized_symbol])


def update_funding_cap_watch_exchanges(
    db: Session,
    symbol: str,
    exchanges: list[str],
) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    selected_exchanges = normalize_funding_cap_watch_exchanges(exchanges)
    item = db.scalar(
        select(CryptoFundingCapWatchItem)
        .where(CryptoFundingCapWatchItem.symbol == normalized_symbol)
        .limit(1)
    )
    if not item:
        raise ValueError("监控币种不存在")
    item.exchanges_json = json.dumps(selected_exchanges, separators=(",", ":"))
    item.enabled = True
    item.last_error = None
    item.updated_at = datetime.now(timezone.utc)
    db.execute(
        delete(CryptoFundingCapSnapshot).where(
            CryptoFundingCapSnapshot.watch_item_id == item.id,
            CryptoFundingCapSnapshot.exchange.not_in(selected_exchanges),
        )
    )
    db.commit()
    return refresh_funding_cap_watchlist(db, push=False, symbols=[normalized_symbol])


def delete_funding_cap_watch(db: Session, symbol: str) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    item = db.scalar(
        select(CryptoFundingCapWatchItem)
        .where(CryptoFundingCapWatchItem.symbol == normalized_symbol)
        .limit(1)
    )
    if not item:
        raise ValueError("监控币种不存在")
    db.delete(item)
    db.commit()
    return funding_cap_watch_overview(db)


def bitget_mix_symbol(symbol: str) -> str:
    return f"{normalize_symbol(symbol)}USDT"


def bitget_mix_ticker_row(client: httpx.Client, symbol: str) -> dict[str, Any]:
    market_symbol = bitget_mix_symbol(symbol)
    payload = bitget_data(
        request_json(
            client,
            f"{base_url('bg')}/api/v2/mix/market/ticker",
            {"symbol": market_symbol, "productType": "USDT-FUTURES"},
        )
    )
    if isinstance(payload, list) and payload:
        row = payload[0]
    else:
        row = payload
    if not isinstance(row, dict):
        raise ValueError(f"Bitget {market_symbol} ticker 返回为空")
    return row


def bitget_mix_candle_rows(
    client: httpx.Client,
    symbol: str,
    granularity: str,
    limit: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    market_symbol = bitget_mix_symbol(symbol)
    params = {
        "symbol": market_symbol,
        "productType": "USDT-FUTURES",
        "granularity": granularity,
        "limit": str(max(1, min(limit, PAIR_SPREAD_CANDLE_REQUEST_LIMITS["bg"]))),
    }
    if start_ms is not None:
        params["startTime"] = str(start_ms)
    if end_ms is not None:
        params["endTime"] = str(end_ms)
    payload = bitget_data(
        request_json(
            client,
            f"{base_url('bg')}/api/v2/mix/market/candles",
            params,
        )
    )
    rows: list[dict[str, Any]] = []
    for raw in payload if isinstance(payload, list) else []:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        ts = parse_float(raw[0])
        close = parse_float(raw[4])
        if ts is None or close is None:
            continue
        rows.append({"ts": int(ts), "close": close})
    return sorted(rows, key=lambda row: row["ts"])


PAIR_SPREAD_GRANULARITY_MAP = {
    "bn": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h", "4H": "4h"},
    "by": {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1H": "60", "4H": "240"},
    "gt": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h", "4H": "4h"},
    "okx": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1H", "4H": "4H"},
    "bg": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1H", "4H": "4H"},
    "hl": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h", "4H": "4h"},
}
PAIR_SPREAD_CANDLE_REQUEST_LIMITS = {
    "bn": 1500,
    "by": 1000,
    "gt": 1000,
    "okx": 300,
    "bg": 1000,
    "hl": 500,
}
PAIR_SPREAD_MAX_POINTS = 12000


def pair_spread_exchange(value: str) -> str:
    exchange = normalize_exchange(value)
    if exchange not in PAIR_SPREAD_EXCHANGES:
        raise ValueError("价差监控暂支持 Binance、Bybit、Gate、OKX、Bitget、Hyperliquid")
    return exchange


def normalize_hyperliquid_pair_coin(value: str) -> str:
    raw = value.strip()
    if ":" not in raw:
        return normalize_symbol(raw)
    dex, coin = raw.split(":", 1)
    normalized_dex = dex.strip().lower()
    normalized_coin = coin.strip()
    if not normalized_dex or not normalized_coin or not normalized_dex.isalnum() or not normalized_coin.isalnum():
        raise ValueError("Hyperliquid 合约格式不正确，应为 合约名 或 DEX:合约名")
    return f"{normalized_dex}:{normalized_coin}"


def hyperliquid_pair_markets(client: httpx.Client) -> tuple[str, ...]:
    global _pair_spread_hl_market_cache
    now = datetime.now(timezone.utc)
    with _pair_spread_hl_market_cache_lock:
        cached = _pair_spread_hl_market_cache
        if cached and (now - cached[0]).total_seconds() < PAIR_SPREAD_HL_MARKET_CACHE_SECONDS:
            return cached[1]
    try:
        payload = request_post_json(client, f"{base_url('hl')}/info", {"type": "allPerpMetas"})
    except httpx.HTTPError as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        suffix = f"（HTTP {status}）" if status else ""
        raise ValueError(f"Hyperliquid 合约列表读取失败{suffix}，请稍后重试。") from exc
    names: list[str] = []
    for meta in payload if isinstance(payload, list) else []:
        universe = meta.get("universe") if isinstance(meta, dict) else []
        for row in universe if isinstance(universe, list) else []:
            name = str(row.get("name") or "").strip() if isinstance(row, dict) else ""
            if name and name not in names:
                names.append(name)
    if not names:
        raise ValueError("Hyperliquid 未返回可用合约列表，请稍后重试。")
    result = tuple(names)
    with _pair_spread_hl_market_cache_lock:
        _pair_spread_hl_market_cache = (now, result)
    return result


def resolve_hyperliquid_pair_coin(client: httpx.Client, symbol: str) -> str:
    requested = normalize_hyperliquid_pair_coin(symbol)
    markets = hyperliquid_pair_markets(client)
    exact = [name for name in markets if name.upper() == requested.upper()]
    if exact:
        return exact[0]
    if ":" not in requested:
        suffix_matches = [name for name in markets if name.rsplit(":", 1)[-1].upper() == requested.upper()]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        if len(suffix_matches) > 1:
            choices = "、".join(suffix_matches[:5])
            raise ValueError(f"Hyperliquid 的 {requested} 存在多个 HIP-3 市场：{choices}，请输入完整标识。")
    raise ValueError(f"Hyperliquid 未找到合约 {requested}，请确认合约名或输入完整的 DEX:合约名。")


def pair_spread_market_symbol(exchange: str, symbol: str) -> str:
    if exchange == "hl":
        return normalize_hyperliquid_pair_coin(symbol)
    normalized = normalize_symbol(symbol)
    if exchange == "gt":
        return f"{normalized}_USDT"
    if exchange == "okx":
        return f"{normalized}-USDT-SWAP"
    return f"{normalized}USDT"


def pair_spread_granularity(exchange: str, granularity: str) -> str:
    return PAIR_SPREAD_GRANULARITY_MAP.get(exchange, {}).get(granularity, granularity)


def pair_spread_bucket_ms(granularity: str) -> int | None:
    minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240}.get(granularity)
    return minutes * 60_000 if minutes else None


def pair_spread_point_limit(granularity: str, range_hours: float | None, requested_limit: int) -> int:
    requested = max(20, int(requested_limit))
    bucket_ms = pair_spread_bucket_ms(granularity)
    hours = max(1.0, min(first_float(range_hours) or 168.0, 24 * 120))
    if not bucket_ms:
        return min(max(requested, 200), PAIR_SPREAD_MAX_POINTS)
    needed = int(hours * 3600 * 1000 / bucket_ms) + 3
    return min(max(requested, needed), PAIR_SPREAD_MAX_POINTS)


def pair_spread_request_limit(exchange: str, requested_limit: int) -> int:
    return max(1, min(requested_limit, PAIR_SPREAD_CANDLE_REQUEST_LIMITS.get(exchange, 500)))


def pair_spread_price_row(ts: Any, price: Any, multiplier: float = 1.0) -> dict[str, Any] | None:
    parsed_ts = parse_float(ts)
    parsed_price = parse_float(price)
    if parsed_ts is None or parsed_price is None or parsed_price <= 0:
        return None
    if parsed_ts < 1_000_000_000_000:
        parsed_ts *= 1000
    return {"ts": int(parsed_ts), "close": parsed_price * multiplier}


def pair_spread_adjust_rows(rows: list[dict[str, Any]], price_ratio: float | None) -> list[dict[str, Any]]:
    ratio = parse_float(price_ratio)
    if ratio is None or ratio <= 0 or ratio == 1:
        return rows
    adjusted: list[dict[str, Any]] = []
    for row in rows:
        close = parse_float(row.get("close"))
        ts = parse_float(row.get("ts"))
        if close is None or ts is None:
            continue
        item = {**row, "ts": int(ts), "close": close / ratio}
        for key in ("bid", "ask"):
            value = parse_float(row.get(key))
            item[key] = value / ratio if value is not None and value > 0 else None
        adjusted.append(item)
    return adjusted


def pair_spread_rows_by_bucket(rows: list[dict[str, Any]], granularity: str) -> dict[int, dict[str, Any]]:
    bucket_ms = pair_spread_bucket_ms(granularity)
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        ts = parse_float(row.get("ts"))
        if ts is None:
            continue
        key = int(ts)
        if bucket_ms:
            key = int(key // bucket_ms * bucket_ms)
        out[key] = row
    return out


def contract_ticker_price(client: httpx.Client, exchange: str, symbol: str, price_ratio: float | None = None) -> dict[str, Any]:
    normalized_exchange = pair_spread_exchange(exchange)
    market_symbol = pair_spread_market_symbol(normalized_exchange, symbol)
    now_ms = int(time.time() * 1000)
    bid: float | None = None
    ask: float | None = None
    if normalized_exchange == "bg":
        row = bitget_mix_ticker_row(client, symbol)
        price = first_float(row.get("lastPr"), row.get("markPrice"), row.get("bidPr"))
        bid = first_float(row.get("bidPr"))
        ask = first_float(row.get("askPr"))
        ts = first_float(row.get("ts"), now_ms)
    elif normalized_exchange == "bn":
        row = request_json(client, f"{base_url('bn')}/fapi/v1/ticker/24hr", {"symbol": market_symbol})
        price = first_float(row.get("lastPrice"), row.get("bidPrice"), row.get("askPrice"))
        bid = first_float(row.get("bidPrice"))
        ask = first_float(row.get("askPrice"))
        ts = first_float(row.get("closeTime"), now_ms)
    elif normalized_exchange == "by":
        payload = request_json(client, f"{base_url('by')}/v5/market/tickers", {"category": "linear", "symbol": market_symbol})
        rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else []
        row = rows[0] if isinstance(rows, list) and rows else {}
        price = first_float(row.get("lastPrice"), row.get("markPrice"), row.get("bid1Price"))
        bid = first_float(row.get("bid1Price"))
        ask = first_float(row.get("ask1Price"))
        ts = first_float(payload.get("time") if isinstance(payload, dict) else None, now_ms)
    elif normalized_exchange == "gt":
        rows = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/tickers", {"contract": market_symbol})
        row = rows[0] if isinstance(rows, list) and rows else {}
        price = first_float(row.get("last"), row.get("mark_price"), row.get("highest_bid"))
        bid = first_float(row.get("highest_bid"))
        ask = first_float(row.get("lowest_ask"))
        ts = first_float(row.get("time_ms"), row.get("time"), now_ms)
    elif normalized_exchange == "okx":
        row = okx_data(request_json(client, f"{base_url('okx')}/api/v5/market/ticker", {"instId": market_symbol}))
        price = first_float(row.get("last"), row.get("markPx"), row.get("bidPx"))
        bid = first_float(row.get("bidPx"))
        ask = first_float(row.get("askPx"))
        ts = first_float(row.get("ts"), now_ms)
    elif normalized_exchange == "hl":
        try:
            payload = request_post_json(client, f"{base_url('hl')}/info", {"type": "l2Book", "coin": market_symbol})
        except httpx.HTTPError as exc:
            raise ValueError(f"Hyperliquid {market_symbol} 实时盘口读取失败，请稍后重试。") from exc
        levels = payload.get("levels") if isinstance(payload, dict) else []
        bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
        asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
        bid = first_float((bids[0] or {}).get("px")) if bids and isinstance(bids[0], dict) else None
        ask = first_float((asks[0] or {}).get("px")) if asks and isinstance(asks[0], dict) else None
        price = (bid + ask) / 2 if bid is not None and ask is not None else first_float(bid, ask)
        ts = first_float(payload.get("time") if isinstance(payload, dict) else None, now_ms)
    else:
        raise ValueError(f"{EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)} 合约价差监控暂未接入")
    parsed = pair_spread_price_row(ts, price)
    if parsed is None:
        raise ValueError(f"{EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)} {market_symbol} 实时价格为空")
    parsed["bid"] = bid if bid is not None and bid > 0 else None
    parsed["ask"] = ask if ask is not None and ask > 0 else None
    adjusted = pair_spread_adjust_rows([parsed], price_ratio)
    if not adjusted:
        raise ValueError(f"{EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)} {market_symbol} 实时价格无法折算")
    return adjusted[0]


def contract_candle_rows_page(
    client: httpx.Client,
    exchange: str,
    symbol: str,
    granularity: str,
    limit: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
    price_ratio: float | None = None,
) -> list[dict[str, Any]]:
    normalized_exchange = pair_spread_exchange(exchange)
    market_symbol = pair_spread_market_symbol(normalized_exchange, symbol)
    interval = pair_spread_granularity(normalized_exchange, granularity)
    normalized_limit = pair_spread_request_limit(normalized_exchange, limit)
    rows: list[dict[str, Any]] = []
    if normalized_exchange == "bg":
        rows = bitget_mix_candle_rows(client, symbol, interval, normalized_limit, start_ms, end_ms)
    elif normalized_exchange == "bn":
        params: dict[str, Any] = {"symbol": market_symbol, "interval": interval, "limit": normalized_limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        payload = request_json(client, f"{base_url('bn')}/fapi/v1/klines", params)
        for raw in payload if isinstance(payload, list) else []:
            if isinstance(raw, list) and len(raw) >= 5:
                if item := pair_spread_price_row(raw[0], raw[4]):
                    rows.append(item)
    elif normalized_exchange == "by":
        params = {"category": "linear", "symbol": market_symbol, "interval": interval, "limit": normalized_limit}
        if start_ms is not None:
            params["start"] = start_ms
        if end_ms is not None:
            params["end"] = end_ms
        payload = request_json(client, f"{base_url('by')}/v5/market/kline", params)
        data = payload.get("result", {}).get("list") if isinstance(payload, dict) else []
        for raw in data if isinstance(data, list) else []:
            if isinstance(raw, list) and len(raw) >= 5:
                if item := pair_spread_price_row(raw[0], raw[4]):
                    rows.append(item)
    elif normalized_exchange == "gt":
        params: dict[str, Any] = {"contract": market_symbol, "interval": interval}
        if start_ms is not None:
            params["from"] = int(start_ms / 1000)
        if end_ms is not None:
            params["to"] = int(end_ms / 1000)
        if "from" not in params and "to" not in params:
            params["limit"] = normalized_limit
        payload = request_json(client, f"{base_url('gt')}/api/v4/futures/usdt/candlesticks", params)
        for raw in payload if isinstance(payload, list) else []:
            if isinstance(raw, dict):
                if item := pair_spread_price_row(raw.get("t"), first_float(raw.get("c"), raw.get("close"))):
                    rows.append(item)
            elif isinstance(raw, list) and len(raw) >= 5:
                if item := pair_spread_price_row(raw[0], raw[4]):
                    rows.append(item)
    elif normalized_exchange == "okx":
        payload = request_json(
            client,
            f"{base_url('okx')}/api/v5/market/candles",
            {"instId": market_symbol, "bar": interval, "limit": normalized_limit},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        for raw in data if isinstance(data, list) else []:
            if isinstance(raw, list) and len(raw) >= 5:
                if item := pair_spread_price_row(raw[0], raw[4]):
                    rows.append(item)
    elif normalized_exchange == "hl":
        bucket_ms = pair_spread_bucket_ms(granularity) or 60_000
        request_end = int(end_ms or time.time() * 1000)
        request_start = int(start_ms or (request_end - max(1, normalized_limit) * bucket_ms))
        try:
            payload = request_post_json(
                client,
                f"{base_url('hl')}/info",
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": market_symbol,
                        "interval": interval,
                        "startTime": request_start,
                        "endTime": request_end,
                    },
                },
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"Hyperliquid {market_symbol} K线读取失败，请稍后重试。") from exc
        for raw in payload if isinstance(payload, list) else []:
            if isinstance(raw, dict):
                if item := pair_spread_price_row(raw.get("t"), raw.get("c")):
                    rows.append(item)
        rows = rows[-normalized_limit:]
    else:
        raise ValueError(f"{EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange)} 合约K线暂未接入")
    return pair_spread_adjust_rows(sorted(rows, key=lambda row: row["ts"]), price_ratio)


def contract_candle_rows(
    client: httpx.Client,
    exchange: str,
    symbol: str,
    granularity: str,
    limit: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
    price_ratio: float | None = None,
) -> list[dict[str, Any]]:
    normalized_exchange = pair_spread_exchange(exchange)
    normalized_limit = max(1, min(int(limit), PAIR_SPREAD_MAX_POINTS))
    per_request = pair_spread_request_limit(normalized_exchange, normalized_limit)
    bucket_ms = pair_spread_bucket_ms(granularity) or 60_000
    if start_ms is None or end_ms is None or normalized_limit <= per_request:
        rows = contract_candle_rows_page(
            client,
            normalized_exchange,
            symbol,
            granularity,
            normalized_limit,
            start_ms,
            end_ms,
            price_ratio,
        )
    else:
        rows_by_ts: dict[int, dict[str, Any]] = {}
        cursor = int(start_ms)
        final_end = int(end_ms)
        chunk_span = max(bucket_ms, bucket_ms * max(1, per_request - 2))
        max_pages = int((normalized_limit + per_request - 1) / per_request) + 4
        pages = 0
        while cursor <= final_end and len(rows_by_ts) < normalized_limit and pages < max_pages:
            chunk_end = min(final_end, cursor + chunk_span)
            page = contract_candle_rows_page(
                client,
                normalized_exchange,
                symbol,
                granularity,
                per_request,
                cursor,
                chunk_end,
                price_ratio,
            )
            for row in page:
                ts = parse_float(row.get("ts"))
                if ts is None:
                    continue
                ts_int = int(ts)
                if int(start_ms) <= ts_int <= final_end:
                    rows_by_ts[ts_int] = row
            if page:
                last_ts = max(int(row["ts"]) for row in page if row.get("ts") is not None)
                cursor = max(chunk_end + 1, last_ts + bucket_ms)
            else:
                cursor = chunk_end + bucket_ms
            pages += 1
        rows = [rows_by_ts[ts] for ts in sorted(rows_by_ts)][-normalized_limit:]
    if granularity == "1m" or len(rows) < 2:
        return rows

    rows_by_ts = {
        int(ts): row
        for row in rows
        if (ts := parse_float(row.get("ts"))) is not None
    }
    ordered_timestamps = sorted(rows_by_ts)
    missing_internal: list[int] = []
    for previous, current in zip(ordered_timestamps, ordered_timestamps[1:]):
        expected = previous + bucket_ms
        while expected < current and len(missing_internal) < 12:
            missing_internal.append(expected)
            expected += bucket_ms
        if len(missing_internal) >= 12:
            break
    for missing_ts in missing_internal:
        minute_rows = contract_candle_rows_page(
            client,
            normalized_exchange,
            symbol,
            "1m",
            max(3, int(bucket_ms / 60_000) + 2),
            missing_ts,
            missing_ts + bucket_ms - 1,
            price_ratio,
        )
        minute_candidates = [
            row
            for row in minute_rows
            if (
                (minute_ts := parse_float(row.get("ts"))) is not None
                and missing_ts <= int(minute_ts) < missing_ts + bucket_ms
                and parse_float(row.get("close")) is not None
            )
        ]
        if not minute_candidates:
            continue
        closing_row = max(minute_candidates, key=lambda row: int(parse_float(row.get("ts")) or 0))
        rows_by_ts[missing_ts] = {
            "ts": missing_ts,
            "close": parse_float(closing_row.get("close")),
            "backfilledFrom": "1m",
        }
    return [rows_by_ts[ts] for ts in sorted(rows_by_ts)][-normalized_limit:]


def pair_spread_auto_granularity(range_hours: float | None) -> str:
    hours = first_float(range_hours)
    if hours is None or hours <= 3:
        return "1m"
    if hours <= 16:
        return "5m"
    if hours <= 50:
        return "15m"
    if hours <= 200:
        return "1H"
    return "4H"


def pair_spread_symmetric_pct(left_price: float | None, right_price: float | None) -> float | None:
    left = parse_float(left_price)
    right = parse_float(right_price)
    if left is None or right is None or left <= 0 or right <= 0:
        return None
    denominator = left + right
    if denominator <= 0:
        return None
    return 2 * (left - right) / denominator * 100


def kstr_symmetric_normalized_spread_pct(
    raw_ratio: float | None,
    reference_ratio: float | None,
) -> float | None:
    """Symmetric spread between the live KSTR/A-share ratio and its historical baseline."""
    ratio = parse_float(raw_ratio)
    reference = parse_float(reference_ratio)
    if ratio is None or reference is None or ratio <= 0 or reference <= 0:
        return None
    return pair_spread_symmetric_pct(ratio, reference)


def kstr_bid1_spread_values(
    kstr_bid: float | None,
    usd_cny: float | None,
    a_etf_bid: float | None,
    reference_ratio: float | None,
) -> dict[str, float] | None:
    """Build the observable KSTR/A-share spread from both legs' best bids."""
    normalized_kstr_bid = parse_float(kstr_bid)
    normalized_usd_cny = parse_float(usd_cny)
    normalized_a_etf_bid = parse_float(a_etf_bid)
    normalized_reference = parse_float(reference_ratio)
    if (
        normalized_kstr_bid is None
        or normalized_usd_cny is None
        or normalized_a_etf_bid is None
        or normalized_reference is None
        or normalized_kstr_bid <= 0
        or normalized_usd_cny <= 0
        or normalized_a_etf_bid <= 0
        or normalized_reference <= 0
    ):
        return None
    raw_ratio = normalized_kstr_bid * normalized_usd_cny / normalized_a_etf_bid
    spread_pct = kstr_symmetric_normalized_spread_pct(
        raw_ratio,
        normalized_reference,
    )
    if spread_pct is None:
        return None
    return {
        "rawRatio": raw_ratio,
        "spreadPct": spread_pct,
        "kstrEquivalentPriceCny": (
            normalized_kstr_bid * normalized_usd_cny / normalized_reference
        ),
    }


def kstr_order_book_spread_values(
    kstr_bid: float | None,
    kstr_ask: float | None,
    usd_cny: float | None,
    a_etf_bid: float | None,
    a_etf_ask: float | None,
    reference_ratio: float | None,
    *,
    execution_ready: bool,
) -> dict[str, float | None]:
    """Separate executable spreads from valid order-book reference spreads."""
    normalized_kstr_bid = parse_float(kstr_bid)
    normalized_kstr_ask = parse_float(kstr_ask)
    normalized_usd_cny = parse_float(usd_cny)
    normalized_a_etf_bid = parse_float(a_etf_bid)
    normalized_a_etf_ask = parse_float(a_etf_ask)
    normalized_reference = parse_float(reference_ratio)

    indicative_open_spread_pct = None
    if (
        normalized_kstr_bid is not None
        and normalized_kstr_bid > 0
        and normalized_usd_cny is not None
        and normalized_usd_cny > 0
        and normalized_a_etf_ask is not None
        and normalized_a_etf_ask > 0
        and normalized_reference is not None
        and normalized_reference > 0
    ):
        indicative_open_spread_pct = kstr_symmetric_normalized_spread_pct(
            normalized_kstr_bid * normalized_usd_cny / normalized_a_etf_ask,
            normalized_reference,
        )

    indicative_close_spread_pct = None
    if (
        normalized_kstr_ask is not None
        and normalized_kstr_ask > 0
        and normalized_usd_cny is not None
        and normalized_usd_cny > 0
        and normalized_a_etf_bid is not None
        and normalized_a_etf_bid > 0
        and normalized_reference is not None
        and normalized_reference > 0
    ):
        indicative_close_spread_pct = kstr_symmetric_normalized_spread_pct(
            normalized_kstr_ask * normalized_usd_cny / normalized_a_etf_bid,
            normalized_reference,
        )

    return {
        "openSpreadPct": (
            indicative_open_spread_pct if execution_ready else None
        ),
        "closeSpreadPct": (
            indicative_close_spread_pct if execution_ready else None
        ),
        "indicativeOpenSpreadPct": indicative_open_spread_pct,
        "indicativeCloseSpreadPct": indicative_close_spread_pct,
    }


def pair_spread_item(
    ts: int,
    left_price: float | None,
    right_price_raw: float | None,
    right_ratio: float,
    source: str,
    *,
    left_bid: float | None = None,
    left_ask: float | None = None,
    right_bid_raw: float | None = None,
    right_ask_raw: float | None = None,
) -> dict[str, Any] | None:
    if left_price is None or right_price_raw is None or left_price <= 0 or right_ratio <= 0:
        return None
    right_price = right_price_raw / right_ratio
    spread_abs = left_price - right_price
    spread_pct = pair_spread_symmetric_pct(left_price, right_price)
    normalized_left_bid = parse_float(left_bid)
    normalized_left_ask = parse_float(left_ask)
    normalized_right_bid_raw = parse_float(right_bid_raw)
    normalized_right_ask_raw = parse_float(right_ask_raw)
    right_bid = normalized_right_bid_raw / right_ratio if normalized_right_bid_raw is not None and normalized_right_bid_raw > 0 else None
    right_ask = normalized_right_ask_raw / right_ratio if normalized_right_ask_raw is not None and normalized_right_ask_raw > 0 else None
    open_spread_pct = pair_spread_symmetric_pct(normalized_left_bid, right_ask)
    close_spread_pct = pair_spread_symmetric_pct(normalized_left_ask, right_bid)
    return {
        "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
        "leftPrice": left_price,
        "leftBid": normalized_left_bid,
        "leftAsk": normalized_left_ask,
        "rightPriceRaw": right_price_raw,
        "rightPrice": right_price,
        "rightBidRaw": normalized_right_bid_raw,
        "rightAskRaw": normalized_right_ask_raw,
        "rightBid": right_bid,
        "rightAsk": right_ask,
        "spreadAbs": spread_abs,
        "spreadPct": spread_pct,
        "openSpreadPct": open_spread_pct,
        "closeSpreadPct": close_spread_pct,
        "rawRatio": right_price_raw / left_price,
        "source": source,
    }


def stock_quote_number(value: Any) -> float | None:
    if value is None:
        return None
    cleaned = str(value).strip().replace(",", "").replace("$", "").replace("₩", "").replace("%", "")
    return parse_float(cleaned)


def external_stock_json(client: httpx.Client, url: str) -> Any:
    response = rate_limited_get(
        client,
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
    )
    response.raise_for_status()
    return response.json()


def yahoo_intraday_close_rows(client: httpx.Client, symbol: str) -> tuple[dict[int, float], dict[str, Any]]:
    """Fetch one month of five-minute closes from Yahoo with a host fallback."""
    last_error: Exception | None = None
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{symbol}"
        try:
            response = rate_limited_get(
                client,
                url,
                params={"interval": "5m", "range": "1mo", "events": "history", "crumb": ""},
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136 Safari/537.36",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            response.raise_for_status()
            payload = response.json()
            chart = payload.get("chart") if isinstance(payload, dict) else {}
            results = chart.get("result") if isinstance(chart, dict) else []
            result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            if not result:
                raise ValueError(f"Yahoo {symbol} 未返回行情")
            timestamps = result.get("timestamp") or []
            indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
            quote_rows = indicators.get("quote") if isinstance(indicators, dict) else []
            closes = quote_rows[0].get("close") if isinstance(quote_rows, list) and quote_rows and isinstance(quote_rows[0], dict) else []
            rows: dict[int, float] = {}
            for raw_ts, raw_price in zip(timestamps, closes if isinstance(closes, list) else []):
                ts = first_float(raw_ts)
                price = first_float(raw_price)
                if ts is not None and price is not None and price > 0:
                    rows[int(ts * 1000)] = price
            if len(rows) < 20:
                raise ValueError(f"Yahoo {symbol} 有效五分钟行情不足")
            return rows, result.get("meta") if isinstance(result.get("meta"), dict) else {}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            last_error = exc
    raise ValueError(f"{symbol} 五分钟历史行情读取失败：{last_error or '未知错误'}")


def yahoo_daily_adjusted_close_rows(
    client: httpx.Client,
    symbol: str,
    now_utc: datetime,
) -> dict[str, float]:
    """Return daily adjusted closes keyed by the exchange's economic trade date."""
    start_seconds = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp())
    end_seconds = int((now_utc + timedelta(days=2)).timestamp())
    last_error: Exception | None = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            response = rate_limited_get(
                client,
                f"https://{host}/v8/finance/chart/{symbol}",
                params={
                    "period1": start_seconds,
                    "period2": end_seconds,
                    "interval": "1d",
                    "events": "div,splits",
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136 Safari/537.36",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            response.raise_for_status()
            payload = response.json()
            chart = payload.get("chart") if isinstance(payload, dict) else {}
            results = chart.get("result") if isinstance(chart, dict) else []
            result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            if not result:
                raise ValueError(f"Yahoo {symbol} 未返回日线")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            timezone_name = str(meta.get("exchangeTimezoneName") or "UTC")
            try:
                exchange_timezone = ZoneInfo(timezone_name)
            except (KeyError, ValueError):
                exchange_timezone = timezone.utc
            timestamps = result.get("timestamp") or []
            indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
            adjusted_rows = indicators.get("adjclose") if isinstance(indicators, dict) else []
            quote_rows = indicators.get("quote") if isinstance(indicators, dict) else []
            adjusted = (
                adjusted_rows[0].get("adjclose")
                if isinstance(adjusted_rows, list) and adjusted_rows and isinstance(adjusted_rows[0], dict)
                else None
            )
            closes = (
                quote_rows[0].get("close")
                if isinstance(quote_rows, list) and quote_rows and isinstance(quote_rows[0], dict)
                else []
            )
            values = adjusted if isinstance(adjusted, list) and len(adjusted) == len(timestamps) else closes
            rows: dict[str, float] = {}
            for raw_timestamp, raw_price in zip(timestamps, values if isinstance(values, list) else []):
                timestamp = first_float(raw_timestamp)
                price = first_float(raw_price)
                if timestamp is None or price is None or price <= 0:
                    continue
                trade_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(exchange_timezone).date().isoformat()
                rows[trade_date] = price
            if len(rows) < 120:
                raise ValueError(f"Yahoo {symbol} 有效日线仅{len(rows)}条")
            return rows
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            last_error = exc
    raise ValueError(f"{symbol} 日线历史读取失败：{last_error or '未知错误'}")


def nasdaq_daily_close_rows(client: httpx.Client, symbol: str, now_utc: datetime) -> dict[str, float]:
    response = rate_limited_get(
        client,
        f"https://api.nasdaq.com/api/quote/{symbol}/historical",
        params={
            "assetclass": "etf",
            "fromdate": "2021-01-01",
            "todate": now_utc.date().isoformat(),
            "limit": 5000,
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        },
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    trades = data.get("tradesTable", {}).get("rows") if isinstance(data, dict) else None
    rows: dict[str, float] = {}
    for item in trades if isinstance(trades, list) else []:
        if not isinstance(item, dict):
            continue
        price = stock_quote_number(item.get("close"))
        try:
            trade_date = datetime.strptime(str(item.get("date") or ""), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        if price is not None and price > 0:
            rows[trade_date] = price
    if len(rows) < 120:
        raise ValueError(f"Nasdaq {symbol} 有效日线仅{len(rows)}条")
    return rows


def sina_daily_close_rows(client: httpx.Client, symbol: str) -> dict[str, float]:
    response = rate_limited_get(
        client,
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
        params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": 1023},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
    )
    response.raise_for_status()
    payload = response.json()
    rows = {
        str(item.get("day")): float(item["close"])
        for item in payload if isinstance(payload, list) and isinstance(item, dict)
        if first_float(item.get("close")) is not None and float(item["close"]) > 0
    }
    if len(rows) < 120:
        raise ValueError(f"新浪{symbol}有效日线仅{len(rows)}条")
    return rows


def frankfurter_usd_cny_long_rows(client: httpx.Client, now_utc: datetime) -> dict[str, float]:
    response = rate_limited_get(
        client,
        f"https://api.frankfurter.dev/v1/2021-01-01..{now_utc.date().isoformat()}",
        params={"from": "USD", "to": "CNY"},
        headers={"User-Agent": "stock-review-mac/0.1", "Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    rates = payload.get("rates") if isinstance(payload, dict) else {}
    rows = {
        str(day): float(value["CNY"])
        for day, value in rates.items()
        if isinstance(value, dict) and first_float(value.get("CNY")) is not None and float(value["CNY"]) > 0
    }
    if len(rows) < 120:
        raise ValueError(f"Frankfurter/ECB USD/CNY有效日线仅{len(rows)}条")
    return rows


def single_factor_return_stats(pairs: list[tuple[float, float]]) -> dict[str, float | int] | None:
    """OLS y = alpha + beta*x for synchronized log-return pairs."""
    if len(pairs) < 5:
        return None
    x_mean = fmean(pair[0] for pair in pairs)
    y_mean = fmean(pair[1] for pair in pairs)
    x_variance = sum((pair[0] - x_mean) ** 2 for pair in pairs)
    y_variance = sum((pair[1] - y_mean) ** 2 for pair in pairs)
    if x_variance <= 0 or y_variance <= 0:
        return None
    covariance = sum((pair[0] - x_mean) * (pair[1] - y_mean) for pair in pairs)
    beta = covariance / x_variance
    alpha = y_mean - beta * x_mean
    residuals = [y_value - alpha - beta * x_value for x_value, y_value in pairs]
    squared_error = sum(value**2 for value in residuals)
    degrees_of_freedom = len(pairs) - 2
    beta_se = math.sqrt((squared_error / degrees_of_freedom) / x_variance) if degrees_of_freedom > 0 else 0.0
    correlation = covariance / math.sqrt(x_variance * y_variance)
    return {
        "sampleCount": len(pairs),
        "alpha": alpha,
        "beta": beta,
        "betaSe": beta_se,
        "correlation": correlation,
        "rSquared": correlation**2,
        "residualStdPct": pstdev(residuals) * 100,
    }


def kstr_structural_hedge_model(
    now_utc: datetime,
    a_etf_code: str = KSTR_DEFAULT_A_ETF_CODE,
    cache_seconds: int = KSTR_STRUCTURAL_MODEL_CACHE_SECONDS,
) -> dict[str, Any]:
    """Long-history KSTR/STAR-50 beta after converting each KSTR close into CNY."""
    normalized_code = normalize_kstr_a_etf_code(a_etf_code)
    with _kstr_structural_model_cache_lock:
        cached = _kstr_structural_model_cache.get(normalized_code)
        if cache_seconds > 0 and cached and (now_utc - cached[0]).total_seconds() < cache_seconds:
            return dict(cached[1])

    source_mode = "nasdaq_sina_ecb"
    try:
        with http_client(timeout=30.0) as client:
            with ThreadPoolExecutor(max_workers=3) as pool:
                kstr_future = pool.submit(nasdaq_daily_close_rows, client, "KSTR", now_utc)
                etf_future = pool.submit(sina_daily_close_rows, client, KSTR_A_ETF_BY_CODE[normalized_code]["symbol"])
                fx_future = pool.submit(frankfurter_usd_cny_long_rows, client, now_utc)
                kstr_rows = kstr_future.result()
                etf_rows = etf_future.result()
                fx_rows = fx_future.result()
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        source_mode = "yahoo_fallback"
        with http_client(timeout=25.0) as client:
            with ThreadPoolExecutor(max_workers=3) as pool:
                kstr_future = pool.submit(yahoo_daily_adjusted_close_rows, client, "KSTR", now_utc)
                etf_future = pool.submit(yahoo_daily_adjusted_close_rows, client, f"{normalized_code}.SS", now_utc)
                fx_future = pool.submit(yahoo_daily_adjusted_close_rows, client, "CNY=X", now_utc)
                kstr_rows = kstr_future.result()
                etf_rows = etf_future.result()
                fx_rows = fx_future.result()

    common_dates = sorted(set(kstr_rows) & set(etf_rows) & set(fx_rows))
    pairs: list[tuple[float, float]] = []
    for previous_date, current_date in zip(common_dates, common_dates[1:]):
        previous_values = (etf_rows[previous_date], kstr_rows[previous_date], fx_rows[previous_date])
        current_values = (etf_rows[current_date], kstr_rows[current_date], fx_rows[current_date])
        if not all(math.isfinite(value) and value > 0 for value in (*previous_values, *current_values)):
            continue
        etf_return = math.log(current_values[0] / previous_values[0])
        # KSTR is USD-denominated. Multiplying by each day's USD/CNY rate puts
        # both legs in CNY before estimating their synchronized equity beta.
        kstr_cny_return = math.log((current_values[1] * current_values[2]) / (previous_values[1] * previous_values[2]))
        if abs(etf_return) <= 0.25 and abs(kstr_cny_return) <= 0.25:
            pairs.append((etf_return, kstr_cny_return))
    if len(pairs) < 120:
        raise ValueError(f"KSTR/{normalized_code}/USD-CNY长期共同日线仅{len(pairs)}组")

    windows: dict[str, dict[str, float | int]] = {}
    for window in (20, 60, 120, 252):
        stats = single_factor_return_stats(pairs[-window:])
        if stats:
            windows[str(window)] = stats
    execution_windows = [windows[key] for key in ("20", "60", "120") if key in windows]
    beta_values = sorted(float(item["beta"]) for item in execution_windows)
    dynamic_beta = median(beta_values)
    dynamic_beta = max(0.6, min(dynamic_beta, 1.4))
    correlations = [float(item["correlation"]) for item in execution_windows]
    correlation_range = max(correlations) - min(correlations)
    beta_range = max(beta_values) - min(beta_values)
    if min(correlations) >= 0.85 and beta_range <= 0.15:
        stability = "stable"
        stability_label = "结构相关性稳定"
    elif min(correlations) >= 0.7 and beta_range <= 0.25:
        stability = "watch"
        stability_label = "结构Beta有波动"
    else:
        stability = "unstable"
        stability_label = "结构相关性不稳定"
    payload = {
        "status": "ok",
        "aEtfCode": normalized_code,
        "historyStart": common_dates[0],
        "historyEnd": common_dates[-1],
        "sampleCount": len(pairs),
        "sourceMode": source_mode,
        "supplementedLongHistory": True,
        "dynamicBeta": dynamic_beta,
        "betaRange": beta_range,
        "correlationRange": correlation_range,
        "stability": stability,
        "stabilityLabel": stability_label,
        "windows": windows,
        "fxMethod": "每日KSTR复权收盘乘当日USD/CNY，再与境内ETF同交易日对齐",
        "sourceUrls": (
            {
                "kstr": "https://www.nasdaq.com/market-activity/etf/kstr/historical",
                "aEtf": f"https://finance.sina.com.cn/realstock/company/sh{normalized_code}/nc.shtml",
                "usdCny": "https://frankfurter.dev/",
            }
            if source_mode == "nasdaq_sina_ecb"
            else {
                "kstr": "https://finance.yahoo.com/quote/KSTR/history/",
                "aEtf": f"https://finance.yahoo.com/quote/{normalized_code}.SS/history/",
                "usdCny": "https://finance.yahoo.com/quote/CNY=X/history/",
            }
        ),
    }
    with _kstr_structural_model_cache_lock:
        _kstr_structural_model_cache[normalized_code] = (now_utc, payload)
    return dict(payload)


def normalize_kstr_a_etf_code(value: str | None) -> str:
    code = str(value or KSTR_DEFAULT_A_ETF_CODE).strip().upper().removeprefix("SH").removesuffix(".SH")
    if code not in KSTR_A_ETF_BY_CODE:
        supported = "、".join(KSTR_A_ETF_BY_CODE)
        raise ValueError(f"不支持的科创50ETF {value or '-'}；可选：{supported}")
    return code


def normalize_kstr_contract_exchange(value: str | None) -> str:
    raw_exchange = str(value or KSTR_DEFAULT_CONTRACT_EXCHANGE).strip().lower()
    exchange = pair_spread_exchange(
        {"binance": "bn"}.get(raw_exchange, raw_exchange)
    )
    if exchange not in KSTR_CONTRACT_BY_EXCHANGE:
        supported = "、".join(
            option["exchangeName"] for option in KSTR_CONTRACT_OPTIONS
        )
        raise ValueError(f"不支持的KSTR合约来源 {value or '-'}；可选：{supported}")
    return exchange


def kstr_pair_cache_key(
    a_etf_code: str,
    contract_exchange: str,
) -> tuple[str, str]:
    return (
        normalize_kstr_a_etf_code(a_etf_code),
        normalize_kstr_contract_exchange(contract_exchange),
    )


def kstr_page_snapshot_path(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    path: Path | None = None,
) -> Path:
    if path is not None:
        return path
    from app.database import get_data_dir

    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    filename = (
        f"{normalized_code}.json"
        if normalized_exchange == KSTR_DEFAULT_CONTRACT_EXCHANGE
        else f"{normalized_code}-{normalized_exchange}.json"
    )
    return get_data_dir() / "kstr-spread" / filename


def jsonable_kstr_spread_payload(payload: dict[str, Any]) -> dict[str, Any]:
    def encode(value: Any) -> Any:
        if isinstance(value, datetime):
            normalized = value
            if normalized.tzinfo is None:
                normalized = normalized.replace(tzinfo=timezone.utc)
            return normalized.astimezone(timezone.utc).isoformat()
        if isinstance(value, Path):
            return str(value)
        raise TypeError(f"unsupported snapshot value: {type(value).__name__}")

    return json.loads(json.dumps(payload, ensure_ascii=False, default=encode))


def kstr_auction_archive_path(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    path: Path | None = None,
) -> Path:
    if path is not None:
        return path
    from app.database import get_data_dir

    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    return (
        get_data_dir()
        / "kstr-auction"
        / f"{normalized_code}-{normalized_exchange}.jsonl"
    )


def kstr_auction_backfill_audit_path(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    path: Path | None = None,
) -> Path:
    if path is not None:
        return path
    from app.database import get_data_dir

    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    return (
        get_data_dir()
        / "kstr-auction"
        / f"{normalized_code}-{normalized_exchange}-audit.json"
    )


def load_kstr_auction_backfill_audit(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    path: Path | None = None,
) -> dict[str, Any] | None:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    target = kstr_auction_backfill_audit_path(
        normalized_code,
        normalized_exchange,
        path,
    )
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if (
        normalize_kstr_a_etf_code(value.get("aEtfCode")) != normalized_code
        or normalize_kstr_contract_exchange(value.get("contractExchange"))
        != normalized_exchange
    ):
        return None
    return value


def normalize_kstr_auction_tick(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    tick_time = parse_datetime_value(value.get("time"))
    observed_at = parse_datetime_value(value.get("observedAt")) or tick_time
    a_price = first_float(value.get("aEtfPriceCny"))
    kstr_price = first_float(value.get("kstrPriceUsdt"))
    usd_cny = first_float(value.get("usdCny"))
    raw_ratio = first_float(value.get("rawRatio"))
    spread_pct = first_float(value.get("spreadPct"))
    if (
        tick_time is None
        or observed_at is None
        or a_price is None
        or a_price <= 0
        or kstr_price is None
        or kstr_price <= 0
        or usd_cny is None
        or usd_cny <= 0
        or raw_ratio is None
        or raw_ratio <= 0
        or spread_pct is None
    ):
        return None
    local_tick_time = tick_time.astimezone(ZoneInfo("Asia/Shanghai"))
    if local_tick_time.hour == 9 and local_tick_time.minute == 25:
        normalized_time = local_tick_time.replace(second=0, microsecond=0).astimezone(
            timezone.utc
        )
    else:
        normalized_time = tick_time.replace(
            second=(tick_time.second // 10) * 10,
            microsecond=0,
        )
    return {
        **value,
        "time": normalized_time,
        "observedAt": observed_at,
        "aEtfPriceCny": a_price,
        "kstrPriceUsdt": kstr_price,
        "usdCny": usd_cny,
        "rawRatio": raw_ratio,
        "spreadPct": spread_pct,
        "source": "auction",
    }


def prefer_kstr_auction_tick(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> bool:
    """Keep the earliest 09:25 match observation and the latest other tick."""
    if current is None:
        return True
    local = candidate["time"].astimezone(ZoneInfo("Asia/Shanghai"))
    candidate_observed_at = candidate.get("observedAt") or candidate["time"]
    current_observed_at = current.get("observedAt") or current["time"]
    if local.hour == 9 and local.minute == 25:
        return candidate_observed_at < current_observed_at
    return candidate_observed_at >= current_observed_at


def load_kstr_auction_ticks(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    path: Path | None = None,
    now: datetime | None = None,
    retention_days: int = KSTR_AUCTION_ARCHIVE_DAYS,
) -> list[dict[str, Any]]:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    now_utc = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.tzinfo is not None
        else (now.replace(tzinfo=timezone.utc) if isinstance(now, datetime) else datetime.now(timezone.utc))
    )
    cutoff = now_utc - timedelta(days=max(1, int(retention_days)))
    target = kstr_auction_archive_path(
        normalized_code,
        normalized_exchange,
        path,
    )
    rows: list[dict[str, Any]] = []
    try:
        with _kstr_auction_archive_lock:
            raw_lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        raw_lines = []
    for raw_line in raw_lines:
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        tick = normalize_kstr_auction_tick(parsed)
        if (
            tick is not None
            and tick["observedAt"] >= cutoff
            and a_share_open_auction_observation_active(tick["time"])
        ):
            rows.append(tick)

    if path is None:
        with _kstr_auction_ticks_lock:
            rows.extend(_kstr_auction_ticks.get(cache_key, []))

    by_bucket: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        normalized = normalize_kstr_auction_tick(row)
        if (
            normalized is None
            or normalized["observedAt"] < cutoff
            or not a_share_open_auction_observation_active(normalized["time"])
        ):
            continue
        current = by_bucket.get(normalized["time"])
        if prefer_kstr_auction_tick(current, normalized):
            by_bucket[normalized["time"]] = normalized
    result = [by_bucket[key] for key in sorted(by_bucket)]
    if path is None:
        with _kstr_auction_ticks_lock:
            _kstr_auction_ticks[cache_key] = result[-500:]
    return result


def record_kstr_auction_tick(
    a_etf_code: str,
    contract_exchange: str,
    item: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    tick = normalize_kstr_auction_tick(
        {
            **item,
            "aEtfCode": normalized_code,
            "contractExchange": normalized_exchange,
            "source": "auction",
        }
    )
    if tick is None:
        raise ValueError("KSTR集合竞价观察点字段不完整")

    with _kstr_auction_ticks_lock:
        current_ticks = _kstr_auction_ticks.get(cache_key, [])
        by_bucket = {
            row["time"]: row
            for row in current_ticks
            if isinstance(row.get("time"), datetime)
        }
        current = by_bucket.get(tick["time"])
        accepted = prefer_kstr_auction_tick(current, tick)
        if accepted:
            by_bucket[tick["time"]] = tick
        _kstr_auction_ticks[cache_key] = [
            by_bucket[key] for key in sorted(by_bucket)
        ][-500:]
        stored_tick = by_bucket[tick["time"]]

    if not accepted:
        return stored_tick

    target = kstr_auction_archive_path(
        normalized_code,
        normalized_exchange,
        path,
    )
    jsonable = jsonable_kstr_spread_payload(tick)
    observed_at = tick["observedAt"].astimezone(timezone.utc)
    prune_key = observed_at.date().isoformat()
    cutoff = observed_at - timedelta(days=KSTR_AUCTION_ARCHIVE_DAYS)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _kstr_auction_archive_lock:
            if _kstr_auction_archive_pruned_on.get(target) != prune_key:
                retained_lines: list[str] = []
                try:
                    existing_lines = target.read_text(encoding="utf-8").splitlines()
                except OSError:
                    existing_lines = []
                for raw_line in existing_lines:
                    try:
                        existing = normalize_kstr_auction_tick(json.loads(raw_line))
                    except json.JSONDecodeError:
                        continue
                    if existing is not None and existing["observedAt"] >= cutoff:
                        retained_lines.append(
                            json.dumps(
                                jsonable_kstr_spread_payload(existing),
                                ensure_ascii=False,
                            )
                        )
                retained_lines.append(json.dumps(jsonable, ensure_ascii=False))
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(
                    "\n".join(retained_lines) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(target)
                _kstr_auction_archive_pruned_on[target] = prune_key
            else:
                with target.open("a", encoding="utf-8") as archive:
                    archive.write(json.dumps(jsonable, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return stored_tick


def normalize_kstr_continuous_tick(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    tick_time = parse_datetime_value(value.get("time"))
    observed_at = parse_datetime_value(value.get("observedAt")) or tick_time
    a_price = first_float(value.get("aEtfPriceCny"))
    kstr_price = first_float(value.get("kstrPriceUsdt"))
    usd_cny = first_float(value.get("usdCny"))
    raw_ratio = first_float(value.get("rawRatio"))
    spread_pct = first_float(value.get("spreadPct"))
    if (
        tick_time is None
        or observed_at is None
        or value.get("marketPhase") != "continuous"
        or a_price is None
        or a_price <= 0
        or kstr_price is None
        or kstr_price <= 0
        or usd_cny is None
        or usd_cny <= 0
        or raw_ratio is None
        or raw_ratio <= 0
        or spread_pct is None
    ):
        return None
    return {
        **value,
        "time": tick_time.replace(
            second=(tick_time.second // 10) * 10,
            microsecond=0,
        ),
        "observedAt": observed_at,
        "aEtfPriceCny": a_price,
        "kstrPriceUsdt": kstr_price,
        "usdCny": usd_cny,
        "rawRatio": raw_ratio,
        "spreadPct": spread_pct,
        "source": "live",
        "marketPhase": "continuous",
        "priceMode": "both_legs_bid1",
    }


def record_kstr_continuous_tick(
    a_etf_code: str,
    contract_exchange: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    tick = normalize_kstr_continuous_tick(
        {
            **item,
            "aEtfCode": normalized_code,
            "contractExchange": normalized_exchange,
            "source": "live",
            "marketPhase": "continuous",
        }
    )
    if tick is None:
        raise ValueError("KSTR连续竞价观察点字段不完整")
    with _kstr_continuous_ticks_lock:
        current_ticks = _kstr_continuous_ticks.get(cache_key, [])
        by_bucket = {
            row["time"]: row
            for row in current_ticks
            if isinstance(row.get("time"), datetime)
        }
        current = by_bucket.get(tick["time"])
        if current is None or tick["observedAt"] >= current.get(
            "observedAt",
            tick["time"],
        ):
            by_bucket[tick["time"]] = tick
        _kstr_continuous_ticks[cache_key] = [
            by_bucket[key] for key in sorted(by_bucket)
        ][-KSTR_CONTINUOUS_TICK_LIMIT:]
        return by_bucket[tick["time"]]


def load_kstr_continuous_ticks(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    now_utc = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.tzinfo is not None
        else (
            now.replace(tzinfo=timezone.utc)
            if isinstance(now, datetime)
            else datetime.now(timezone.utc)
        )
    )
    cutoff = now_utc - timedelta(days=KSTR_CONTINUOUS_TICK_RETENTION_DAYS)
    with _kstr_continuous_ticks_lock:
        rows = list(_kstr_continuous_ticks.get(cache_key, []))
    by_bucket: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        normalized = normalize_kstr_continuous_tick(row)
        if normalized is None or normalized["observedAt"] < cutoff:
            continue
        current = by_bucket.get(normalized["time"])
        if current is None or normalized["observedAt"] >= current["observedAt"]:
            by_bucket[normalized["time"]] = normalized
    result = [by_bucket[key] for key in sorted(by_bucket)]
    with _kstr_continuous_ticks_lock:
        _kstr_continuous_ticks[cache_key] = result[-KSTR_CONTINUOUS_TICK_LIMIT:]
    return result


def merge_kstr_auction_ticks_into_payload(
    payload: dict[str, Any],
    a_etf_code: str,
    contract_exchange: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    auction_ticks = load_kstr_auction_ticks(
        a_etf_code,
        contract_exchange,
        now=now,
    )
    backfill_audit = load_kstr_auction_backfill_audit(
        a_etf_code,
        contract_exchange,
    )
    by_key: dict[tuple[datetime, str], dict[str, Any]] = {}
    for raw_item in payload.get("items") or []:
        if not isinstance(raw_item, dict) or raw_item.get("source") == "auction":
            continue
        item_time = parse_datetime_value(raw_item.get("time"))
        if item_time is None:
            continue
        by_key[(item_time, str(raw_item.get("source") or ""))] = raw_item
    if not auction_ticks:
        auction_window = dict(payload.get("auctionWindow") or {})
        auction_window.update(
            {
                "start": "09:20",
                "end": "09:25",
                "backgroundCaptureEnabled": True,
                "captureIntervalSeconds": 10,
                "capturedPoints": 0,
                "lastCapturedAt": None,
                "backfillAudit": backfill_audit,
                "note": "每10秒刷新；9:20-9:25集合竞价价差与9:30后连续竞价价差在同一张图显示。",
            }
        )
        return {
            **payload,
            "items": [
                by_key[key]
                for key in sorted(by_key, key=lambda value: (value[0], value[1]))
            ],
            "auctionWindow": auction_window,
        }

    for tick in auction_ticks:
        by_key[(tick["time"], "auction")] = jsonable_kstr_spread_payload(tick)
    merged_items = [
        by_key[key]
        for key in sorted(by_key, key=lambda value: (value[0], value[1]))
    ]
    auction_window = dict(payload.get("auctionWindow") or {})
    auction_window.update(
        {
            "start": "09:20",
            "end": "09:25",
            "backgroundCaptureEnabled": True,
            "captureIntervalSeconds": 10,
            "capturedPoints": len(auction_ticks),
            "lastCapturedAt": auction_ticks[-1]["observedAt"].isoformat(),
            "backfillAudit": backfill_audit,
            "note": "每10秒刷新；9:20-9:25集合竞价价差与9:30后连续竞价价差在同一张图显示。",
        }
    )
    return {
        **payload,
        "items": merged_items,
        "auctionWindow": auction_window,
    }


def save_kstr_page_snapshot(
    a_etf_code: str,
    payload: dict[str, Any],
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    path: Path | None = None,
    force_disk: bool = False,
) -> dict[str, Any]:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    jsonable = restrict_kstr_payload_to_fixed_pair(
        jsonable_kstr_spread_payload(payload)
    )
    saved_at = datetime.now(timezone.utc)
    target = kstr_page_snapshot_path(
        normalized_code,
        normalized_exchange,
        path,
    )
    with _kstr_page_snapshot_lock:
        _kstr_page_snapshot_memory[cache_key] = (saved_at, jsonable)
        last_disk_write = _kstr_page_snapshot_disk_write_at.get(cache_key)
        should_write = (
            force_disk
            or not target.exists()
            or last_disk_write is None
            or (saved_at - last_disk_write).total_seconds() >= KSTR_PAGE_SNAPSHOT_WRITE_SECONDS
        )
    if not should_write:
        return dict(jsonable)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(jsonable, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        return dict(jsonable)
    with _kstr_page_snapshot_lock:
        _kstr_page_snapshot_disk_write_at[cache_key] = saved_at
    return dict(jsonable)


def restrict_kstr_payload_to_fixed_pair(payload: dict[str, Any]) -> dict[str, Any]:
    """Hide legacy selectors while retaining their archived data on disk."""
    result = dict(payload)
    option_rows = payload.get("aEtfOptions")
    fixed_etf_options = [
        {
            **row,
            "priceClosest": True,
            "executionDefault": True,
        }
        for row in option_rows if isinstance(row, dict)
        and str(row.get("code") or "") == KSTR_DEFAULT_A_ETF_CODE
    ] if isinstance(option_rows, list) else []
    if not fixed_etf_options:
        fixed_etf_options = [
            {
                **KSTR_A_ETF_BY_CODE[KSTR_DEFAULT_A_ETF_CODE],
                "exchangeCode": "SH",
                "trackingIndex": "上证科创板50成份指数",
                "tracksSameIndexAsKstr": True,
                "priceClosest": True,
                "executionDefault": True,
                "sampleCount": int(payload.get("sampleCount") or 0),
                "tradingDays": int(payload.get("historyTradingDays") or 0),
                "residualStdPct": first_float(payload.get("historyStdPct")),
                "returnCorrelation": None,
                "dayTurnoverCny": first_float(
                    (payload.get("aEtf") or {}).get("dayTurnoverCny")
                    if isinstance(payload.get("aEtf"), dict)
                    else None
                ),
                "bidAskSpreadBps": None,
                "comparisonError": payload.get("comparisonError"),
            }
        ]
    result["aEtfOptions"] = fixed_etf_options[:1]
    result["contractOptions"] = [
        {
            **KSTR_CONTRACT_BY_EXCHANGE[KSTR_DEFAULT_CONTRACT_EXCHANGE],
            "selected": True,
        }
    ]
    result["priceClosestAEtfCode"] = KSTR_DEFAULT_A_ETF_CODE
    result["executionDefaultAEtfCode"] = KSTR_DEFAULT_A_ETF_CODE
    liquidity_filter = dict(payload.get("liquidityFilter") or {})
    liquidity_filter.update({"excludedCount": 0, "excludedCodes": []})
    result["liquidityFilter"] = liquidity_filter
    return result


def hydrate_kstr_runtime_caches_from_page_snapshot(
    a_etf_code: str,
    payload: dict[str, Any],
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
) -> None:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    hydrated_at = datetime.now(timezone.utc)
    hydrated_items: list[dict[str, Any]] = []
    hydrated_continuous_ticks: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("source") == "auction":
            continue
        item_time = parse_datetime_value(item.get("time"))
        if item_time is None:
            continue
        hydrated_item = {**item, "time": item_time}
        hydrated_items.append(hydrated_item)
        if item.get("source") == "live" and item.get("marketPhase") == "continuous":
            normalized_tick = normalize_kstr_continuous_tick(hydrated_item)
            if normalized_tick is not None:
                hydrated_continuous_ticks.append(normalized_tick)
    if hydrated_continuous_ticks:
        with _kstr_continuous_ticks_lock:
            current_ticks = _kstr_continuous_ticks.get(cache_key, [])
            by_bucket = {
                row["time"]: row
                for row in [*current_ticks, *hydrated_continuous_ticks]
                if isinstance(row.get("time"), datetime)
            }
            _kstr_continuous_ticks[cache_key] = [
                by_bucket[key] for key in sorted(by_bucket)
            ][-KSTR_CONTINUOUS_TICK_LIMIT:]
    if hydrated_items and first_float(payload.get("referenceRatio")) is not None:
        history_payload = {
            "referenceRatio": payload.get("referenceRatio"),
            "items": hydrated_items,
            "sampleCount": payload.get("sampleCount", len(hydrated_items)),
            "baselineSampleCount": payload.get("baselineSampleCount", len(hydrated_items)),
            "historyStart": payload.get("historyStart"),
            "historyEnd": payload.get("historyEnd"),
            "historyGranularityMinutes": payload.get("historyGranularityMinutes", 5),
            "historyCoverageTradingDays": payload.get("historyCoverageTradingDays", 0),
            "historyDetailError": payload.get("historyDetailError"),
            "historyMeanPct": payload.get("historyMeanPct", 0),
            "historyStdPct": payload.get("historyStdPct", 0),
            "historyP10Pct": payload.get("historyP10Pct"),
            "historyP90Pct": payload.get("historyP90Pct"),
            "historyTradingDays": payload.get("historyTradingDays", 0),
            "baselineTargetDays": payload.get("baselineTargetDays", 20),
            "hedgeBeta": payload.get("hedgeBeta", 1),
            "hedgeBetaRaw": payload.get("hedgeBetaRaw"),
            "contractHedgeBeta": payload.get("contractHedgeBeta"),
            "betaSampleCount": payload.get("betaSampleCount", 0),
            "betaStatus": payload.get("betaStatus", "使用最近成功快照"),
            "structuralModel": payload.get("structuralModel"),
            "structuralModelError": payload.get("structuralModelError"),
            "componentMismatch": payload.get("componentMismatch", KSTR_COMPONENT_MISMATCH_SNAPSHOT),
            "historyStale": True,
            "historyError": "后台正在刷新历史基线",
            "aEtfCode": normalized_code,
            "contractExchange": normalized_exchange,
        }
        with _kstr_history_cache_lock:
            _kstr_history_cache[cache_key] = (hydrated_at, history_payload)
    structural_model = payload.get("structuralModel")
    if isinstance(structural_model, dict):
        with _kstr_structural_model_cache_lock:
            _kstr_structural_model_cache[normalized_code] = (hydrated_at, structural_model)
    options = payload.get("aEtfOptions")
    if isinstance(options, list) and options:
        comparison_payload = {
            "options": options,
            "priceClosestAEtfCode": payload.get("priceClosestAEtfCode"),
            "executionDefaultAEtfCode": payload.get("executionDefaultAEtfCode", KSTR_DEFAULT_A_ETF_CODE),
            "comparisonTradingDays": payload.get("comparisonTradingDays", 0),
            "comparisonUpdatedAt": payload.get("comparisonUpdatedAt"),
            "comparisonMethod": payload.get("comparisonMethod", ""),
            "comparisonError": payload.get("comparisonError"),
            "liquidityFilter": payload.get("liquidityFilter", {}),
            "priceClosestReason": payload.get("priceClosestReason", ""),
            "executionDefaultReason": payload.get("executionDefaultReason", ""),
            "kstrUnderlying": payload.get("kstrUnderlying", {}),
            "optionEvidenceUrl": payload.get("optionEvidenceUrl", ""),
            "contractExchange": normalized_exchange,
        }
        with _kstr_etf_comparison_cache_lock:
            _kstr_etf_comparison_cache[normalized_exchange] = (
                hydrated_at,
                comparison_payload,
            )


def load_kstr_page_snapshot(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    path: Path | None = None,
) -> dict[str, Any] | None:
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    if path is None:
        with _kstr_page_snapshot_lock:
            cached = _kstr_page_snapshot_memory.get(cache_key)
        if cached:
            return merge_kstr_auction_ticks_into_payload(
                restrict_kstr_payload_to_fixed_pair(dict(cached[1])),
                normalized_code,
                normalized_exchange,
            )
    target = kstr_page_snapshot_path(
        normalized_code,
        normalized_exchange,
        path,
    )
    try:
        age_seconds = time.time() - target.stat().st_mtime
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        age_seconds > KSTR_PAGE_SNAPSHOT_MAX_AGE_SECONDS
        or not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or not isinstance(payload.get("items"), list)
    ):
        return None
    kstr_payload = payload.get("kstr")
    snapshot_exchange = (
        normalize_kstr_contract_exchange(kstr_payload.get("exchange"))
        if isinstance(kstr_payload, dict) and kstr_payload.get("exchange")
        else KSTR_DEFAULT_CONTRACT_EXCHANGE
    )
    if snapshot_exchange != normalized_exchange:
        return None
    payload = restrict_kstr_payload_to_fixed_pair(payload)
    loaded_at = datetime.now(timezone.utc)
    if path is None:
        hydrate_kstr_runtime_caches_from_page_snapshot(
            normalized_code,
            payload,
            normalized_exchange,
        )
        with _kstr_page_snapshot_lock:
            _kstr_page_snapshot_memory[cache_key] = (loaded_at, payload)
            _kstr_page_snapshot_disk_write_at[cache_key] = datetime.fromtimestamp(
                target.stat().st_mtime,
                timezone.utc,
            )
    if path is None:
        return merge_kstr_auction_ticks_into_payload(
            dict(payload),
            normalized_code,
            normalized_exchange,
        )
    return dict(payload)


def sina_intraday_close_rows(client: httpx.Client, symbol: str = "sh588000", scale: int = 5) -> dict[int, float]:
    normalized_scale = 1 if int(scale) == 1 else 5
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    response = rate_limited_get(
        client,
        url,
        params={"symbol": symbol, "scale": normalized_scale, "ma": "no", "datalen": 1023},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
    )
    response.raise_for_status()
    payload = response.json()
    rows: dict[int, float] = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        price = first_float(item.get("close"))
        try:
            timestamp = datetime.strptime(str(item.get("day") or ""), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
        except ValueError:
            continue
        if price is not None and price > 0:
            rows[int(timestamp.timestamp() * 1000)] = price
    if len(rows) < 30:
        raise ValueError(f"新浪{symbol}的{normalized_scale}分钟历史行情不足")
    return rows


def frankfurter_usd_cny_daily_rows(client: httpx.Client, now_utc: datetime) -> dict[str, float]:
    start_date = (now_utc - timedelta(days=36)).date().isoformat()
    end_date = now_utc.date().isoformat()
    url = f"https://api.frankfurter.dev/v1/{start_date}..{end_date}"
    response = rate_limited_get(
        client,
        url,
        params={"from": "USD", "to": "CNY"},
        headers={"User-Agent": "stock-review-mac/0.1", "Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    rates = payload.get("rates") if isinstance(payload, dict) else {}
    rows = {
        str(day): float(value["CNY"])
        for day, value in rates.items()
        if isinstance(value, dict) and first_float(value.get("CNY")) is not None and float(value["CNY"]) > 0
    }
    if len(rows) < 5:
        raise ValueError("Frankfurter/ECB美元兑人民币历史汇率不足")
    return rows


def tencent_a_share_quote(client: httpx.Client, symbol: str = "sh588000") -> dict[str, Any]:
    url = f"https://qt.gtimg.cn/q={symbol}"
    response = rate_limited_get(
        client,
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    )
    response.raise_for_status()
    text = response.content.decode("gb18030", errors="replace")
    if '="' not in text:
        raise ValueError(f"腾讯行情未返回{symbol}报价")
    raw = text.split('="', 1)[1].rsplit('"', 1)[0]
    fields = raw.split("~")
    if len(fields) < 31:
        raise ValueError(f"腾讯{symbol}报价格式异常")
    price = first_float(fields[3])
    bid = first_float(fields[9])
    ask = first_float(fields[19])
    if price is None or price <= 0:
        raise ValueError(f"腾讯{symbol}最新价为空")
    reference_time = None
    try:
        reference_time = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        ).astimezone(timezone.utc)
    except (ValueError, IndexError):
        pass
    code = str(fields[2] or symbol.removeprefix("sh"))
    turnover_value = first_float(fields[57] if len(fields) > 57 else None)
    return {
        "code": f"{code}.SH",
        "shortCode": code,
        "name": fields[1] or f"科创50ETF{code}",
        "price": price,
        "bid": bid if bid is not None and bid > 0 else None,
        "ask": ask if ask is not None and ask > 0 else None,
        "previousClose": first_float(fields[4]),
        "open": first_float(fields[5]),
        "dayTurnoverCny": turnover_value * 10_000 if turnover_value is not None else None,
        "referenceTime": reference_time,
        "sourceName": "腾讯证券实时盘口",
        "sourceUrl": url,
    }


def kstr_etf_similarity_metrics(
    a_rows: dict[int, float],
    kstr_rows: list[dict[str, Any]],
    fx_rows: dict[str, float],
) -> dict[str, Any]:
    kstr_by_ts = {
        int(row["ts"]): float(row["close"])
        for row in kstr_rows
        if row.get("ts") is not None and first_float(row.get("close")) is not None and float(row["close"]) > 0
    }
    fx_dates = sorted(fx_rows)
    shanghai = ZoneInfo("Asia/Shanghai")
    aligned: list[tuple[int, float, float, float]] = []
    trading_days: set[str] = set()
    for ts, a_price in sorted(a_rows.items()):
        kstr_price = kstr_by_ts.get(ts)
        if kstr_price is None:
            continue
        local_day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(shanghai).date().isoformat()
        fx_index = bisect_right(fx_dates, local_day) - 1
        if fx_index < 0:
            continue
        raw_ratio = kstr_price * fx_rows[fx_dates[fx_index]] / a_price
        if raw_ratio <= 0:
            continue
        aligned.append((ts, a_price, kstr_price, raw_ratio))
        trading_days.add(local_day)
    if len(aligned) < 30:
        raise ValueError("共同交易时段样本不足")

    reference_ratio = median(row[3] for row in aligned)
    residuals = [(row[3] / reference_ratio - 1) * 100 for row in aligned]
    a_returns: list[float] = []
    kstr_returns: list[float] = []
    for previous, current in zip(aligned, aligned[1:]):
        if current[0] - previous[0] != 5 * 60 * 1000:
            continue
        a_return = current[1] / previous[1] - 1
        kstr_return = current[2] / previous[2] - 1
        if abs(a_return) <= 0.05 and abs(kstr_return) <= 0.05:
            a_returns.append(a_return)
            kstr_returns.append(kstr_return)
    correlation = None
    if len(a_returns) >= 5:
        a_mean = fmean(a_returns)
        kstr_mean = fmean(kstr_returns)
        covariance = sum((a - a_mean) * (k - kstr_mean) for a, k in zip(a_returns, kstr_returns))
        a_variance = sum((a - a_mean) ** 2 for a in a_returns)
        kstr_variance = sum((k - kstr_mean) ** 2 for k in kstr_returns)
        if a_variance > 0 and kstr_variance > 0:
            correlation = covariance / math.sqrt(a_variance * kstr_variance)
    return {
        "sampleCount": len(aligned),
        "tradingDays": len(trading_days),
        "residualStdPct": pstdev(residuals),
        "returnCorrelation": correlation,
    }


def kstr_liquid_etf_options(option_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = [
        row
        for row in option_rows
        if (first_float(row.get("dayTurnoverCny")) or 0) >= KSTR_MIN_A_ETF_DAY_TURNOVER_CNY
    ]
    if not eligible:
        fallback = next((row for row in option_rows if row.get("code") == KSTR_DEFAULT_A_ETF_CODE), None)
        eligible = [fallback] if fallback is not None else []
    eligible_codes = {str(row.get("code")) for row in eligible}
    excluded = [row for row in option_rows if str(row.get("code")) not in eligible_codes]
    return eligible, excluded


def kstr_etf_comparison(
    now_utc: datetime,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    cache_seconds: int = KSTR_ETF_COMPARISON_CACHE_SECONDS,
) -> dict[str, Any]:
    normalized_exchange = normalize_kstr_contract_exchange(contract_exchange)
    with _kstr_etf_comparison_cache_lock:
        cached = _kstr_etf_comparison_cache.get(normalized_exchange)
        if cache_seconds > 0 and cached and (now_utc - cached[0]).total_seconds() < cache_seconds:
            return dict(cached[1])

    baseline_start_ms = int((now_utc - timedelta(days=32)).timestamp() * 1000)
    end_ms = int(now_utc.timestamp() * 1000)
    option_rows: list[dict[str, Any]] = []
    comparison_error: str | None = None
    try:
        with http_client(timeout=28.0) as client:
            with ThreadPoolExecutor(max_workers=16) as pool:
                history_futures = {
                    item["code"]: pool.submit(sina_intraday_close_rows, client, item["symbol"], 5)
                    for item in KSTR_A_ETF_OPTIONS
                }
                quote_futures = {
                    item["code"]: pool.submit(tencent_a_share_quote, client, item["symbol"])
                    for item in KSTR_A_ETF_OPTIONS
                }
                kstr_future = pool.submit(
                    contract_candle_rows,
                    client,
                    normalized_exchange,
                    "KSTR",
                    "5m",
                    11000,
                    baseline_start_ms,
                    end_ms,
                )
                fx_future = pool.submit(frankfurter_usd_cny_daily_rows, client, now_utc)
                kstr_rows = kstr_future.result()
                fx_rows = fx_future.result()
                for item in KSTR_A_ETF_OPTIONS:
                    row = {
                        **item,
                        "exchangeCode": f"{item['code']}.SH",
                        "trackingIndex": "上证科创板50成份指数",
                        "tracksSameIndexAsKstr": True,
                        "priceClosest": False,
                        "executionDefault": item["code"] == KSTR_DEFAULT_A_ETF_CODE,
                        "sampleCount": 0,
                        "tradingDays": 0,
                        "residualStdPct": None,
                        "returnCorrelation": None,
                        "dayTurnoverCny": None,
                        "bidAskSpreadBps": None,
                        "comparisonError": None,
                    }
                    try:
                        row.update(kstr_etf_similarity_metrics(history_futures[item["code"]].result(), kstr_rows, fx_rows))
                    except (httpx.HTTPError, ValueError) as exc:
                        row["comparisonError"] = str(exc)
                    try:
                        quote = quote_futures[item["code"]].result()
                        row["dayTurnoverCny"] = quote.get("dayTurnoverCny")
                        bid = first_float(quote.get("bid"))
                        ask = first_float(quote.get("ask"))
                        mid = (bid + ask) / 2 if bid is not None and ask is not None and bid > 0 and ask > 0 else None
                        row["bidAskSpreadBps"] = (ask - bid) / mid * 10_000 if mid is not None else None
                        row["liveName"] = quote.get("name")
                    except (httpx.HTTPError, ValueError) as exc:
                        row["comparisonError"] = row["comparisonError"] or str(exc)
                    option_rows.append(row)
    except (httpx.HTTPError, ValueError) as exc:
        comparison_error = str(exc)
        option_rows = [
            {
                **item,
                "exchangeCode": f"{item['code']}.SH",
                "trackingIndex": "上证科创板50成份指数",
                "tracksSameIndexAsKstr": True,
                "priceClosest": False,
                "executionDefault": item["code"] == KSTR_DEFAULT_A_ETF_CODE,
                "sampleCount": 0,
                "tradingDays": 0,
                "residualStdPct": None,
                "returnCorrelation": None,
                "dayTurnoverCny": None,
                "bidAskSpreadBps": None,
                "comparisonError": comparison_error,
            }
            for item in KSTR_A_ETF_OPTIONS
        ]

    liquid_option_rows, excluded_option_rows = kstr_liquid_etf_options(option_rows)
    comparable = [row for row in liquid_option_rows if first_float(row.get("residualStdPct")) is not None]
    price_closest = min(comparable, key=lambda row: float(row["residualStdPct"])) if comparable else None
    if price_closest is not None:
        price_closest["priceClosest"] = True
    turnover_rows = [row for row in liquid_option_rows if first_float(row.get("dayTurnoverCny")) is not None]
    turnover_leader = max(turnover_rows, key=lambda row: float(row["dayTurnoverCny"])) if turnover_rows else None
    execution_default = KSTR_A_ETF_BY_CODE[KSTR_DEFAULT_A_ETF_CODE]
    payload = {
        "options": liquid_option_rows,
        "priceClosestAEtfCode": price_closest["code"] if price_closest else None,
        "executionDefaultAEtfCode": KSTR_DEFAULT_A_ETF_CODE,
        "comparisonTradingDays": price_closest["tradingDays"] if price_closest else 0,
        "comparisonUpdatedAt": now_utc,
        "comparisonMethod": (
            "仅在当日成交额不低于1亿元的ETF中比较；"
            "共同交易时段5分钟价差经各自历史中位数归一化，标准差越小表示近期价格路径越接近"
        ),
        "comparisonError": comparison_error,
        "liquidityFilter": {
            "minDayTurnoverCny": KSTR_MIN_A_ETF_DAY_TURNOVER_CNY,
            "excludedCount": len(excluded_option_rows),
            "excludedCodes": [str(row.get("code")) for row in excluded_option_rows],
        },
        "priceClosestReason": (
            f"日成交额不低于1亿元的候选中，最近{price_closest['tradingDays']}个共同交易日归一化价差标准差最低"
            if price_closest else "历史比较数据暂不可用"
        ),
        "executionDefaultReason": (
            f"{execution_default['name']}同指数且为上交所科创50ETF期权标的"
            + ("，当前候选池日内成交额最高" if turnover_leader and turnover_leader["code"] == KSTR_DEFAULT_A_ETF_CODE else "")
        ),
        "kstrUnderlying": {
            "name": "KraneShares SSE STAR Market 50 Index ETF",
            "ticker": "KSTR",
            "exchange": "NYSE Arca",
            "trackingIndex": "SSE Science and Technology Innovation Board 50 Index",
            "officialUrl": "https://engage.kraneshares.com/s/553aa2e9?ks_product=kstr&page=1",
        },
        "optionEvidenceUrl": "https://www.sse.com.cn/lawandrules/sselawsrules2025/option/c/c_20250611_10781536.shtml",
        "contractExchange": normalized_exchange,
    }
    with _kstr_etf_comparison_cache_lock:
        _kstr_etf_comparison_cache[normalized_exchange] = (now_utc, payload)
    return dict(payload)


def a_share_market_phase(now: datetime) -> str:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.weekday() >= 5:
        return "closed"
    minutes = local.hour * 60 + local.minute
    if 9 * 60 + 15 <= minutes < 9 * 60 + 20:
        return "open_auction_cancelable"
    if 9 * 60 + 20 <= minutes < 9 * 60 + 25:
        return "open_auction_no_cancel"
    if 9 * 60 + 25 <= minutes < 9 * 60 + 30:
        return "open_auction_matching"
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return "continuous"
    if 11 * 60 + 30 < minutes < 13 * 60:
        return "lunch_break"
    if 13 * 60 <= minutes < 14 * 60 + 57:
        return "continuous"
    if 14 * 60 + 57 <= minutes <= 15 * 60:
        return "close_auction"
    return "closed"


def a_share_cash_market_open(now: datetime) -> bool:
    return a_share_market_phase(now) == "continuous"


def a_share_kstr_refresh_window_active(now: datetime) -> bool:
    """Refresh only during the A-share opening auction and cash sessions."""
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return (
        9 * 60 + 20 <= minutes <= 11 * 60 + 30
        or 13 * 60 <= minutes <= 15 * 60
    )


def a_share_open_auction_observation_active(now: datetime) -> bool:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    market_phase = a_share_market_phase(now)
    return market_phase == "open_auction_no_cancel" or (
        market_phase == "open_auction_matching"
        and local.hour == 9
        and local.minute == 25
    )


def is_synchronized_kstr_spread_point(row: dict[str, Any]) -> bool:
    """Only continuous-auction points can form a tradable cross-market spread."""
    return row.get("marketPhase") == "continuous" and row.get("source") in {"history", "live"}


def kstr_session_comparison(
    items: list[dict[str, Any]],
    *,
    kstr_bid1: float,
    usd_cny: float,
    indicative_spread_pct: float,
    history_mean_pct: float,
    history_std_pct: float,
    execution_ready: bool,
    reference_time: datetime,
) -> dict[str, Any]:
    """Separate the last synchronized spread from off-session single-leg moves."""
    synchronized_items = [row for row in items if is_synchronized_kstr_spread_point(row)]
    last = synchronized_items[-1] if synchronized_items else None
    if last is None:
        return {
            "isSynchronizedNow": execution_ready,
            "spreadFrozen": not execution_ready,
            "lastSynchronizedTime": None,
            "lastSynchronizedSpreadPct": None,
            "lastSynchronizedZScore": None,
            "lastSynchronizedKstrPriceUsdt": None,
            "lastSynchronizedAEtfPriceCny": None,
            "lastSynchronizedUsdCny": None,
            "kstrMoveSinceSynchronizedPct": None,
            "fxMoveSinceSynchronizedPct": None,
            "indicativeDriftSinceSynchronizedPct": None,
            "elapsedMinutes": None,
            "note": "没有可用的连续竞价同步点，不能计算价差或盘外变化。",
        }

    last_time = last.get("time")
    last_spread = first_float(last.get("spreadPct"))
    last_kstr = first_float(last.get("kstrPriceUsdt"))
    last_fx = first_float(last.get("usdCny"))
    last_z_score = (
        (last_spread - history_mean_pct) / history_std_pct
        if last_spread is not None and history_std_pct > 0
        else None
    )
    elapsed_minutes = None
    if isinstance(last_time, datetime):
        elapsed_minutes = max(0.0, (reference_time - last_time.astimezone(timezone.utc)).total_seconds() / 60)
    return {
        "isSynchronizedNow": execution_ready,
        "spreadFrozen": not execution_ready,
        "lastSynchronizedTime": last_time,
        "lastSynchronizedSpreadPct": last_spread,
        "lastSynchronizedZScore": last_z_score,
        "lastSynchronizedKstrPriceUsdt": last_kstr,
        "lastSynchronizedAEtfPriceCny": first_float(last.get("aEtfPriceCny")),
        "lastSynchronizedUsdCny": last_fx,
        "kstrMoveSinceSynchronizedPct": (
            (kstr_bid1 / last_kstr - 1) * 100 if last_kstr is not None and last_kstr > 0 else None
        ),
        "fxMoveSinceSynchronizedPct": (
            (usd_cny / last_fx - 1) * 100 if last_fx is not None and last_fx > 0 else None
        ),
        "indicativeDriftSinceSynchronizedPct": (
            indicative_spread_pct - last_spread if last_spread is not None else None
        ),
        "elapsedMinutes": elapsed_minutes,
        "note": (
            "当前为双腿同步连续竞价，可按盘口计算价差。"
            if execution_ready
            else "价差停在最近同步点；之后仅展示KSTR与汇率单边变化，不生成价差信号。"
        ),
    }


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def synchronized_return_beta(items: list[dict[str, Any]]) -> tuple[float, float | None, int, str]:
    shanghai = ZoneInfo("Asia/Shanghai")
    daily_last: dict[str, dict[str, Any]] = {}
    for item in items:
        item_time = item.get("time")
        if isinstance(item_time, datetime):
            daily_last[item_time.astimezone(shanghai).date().isoformat()] = item
    pairs: list[tuple[float, float]] = []
    daily_rows = [daily_last[day] for day in sorted(daily_last)]
    for previous, current in zip(daily_rows, daily_rows[1:]):
        previous_a = first_float(previous.get("aEtfPriceCny"))
        current_a = first_float(current.get("aEtfPriceCny"))
        previous_kstr = first_float(previous.get("kstrEquivalentPriceCny"))
        current_kstr = first_float(current.get("kstrEquivalentPriceCny"))
        if not all(value is not None and value > 0 for value in (previous_a, current_a, previous_kstr, current_kstr)):
            continue
        a_return = current_a / previous_a - 1
        kstr_return = current_kstr / previous_kstr - 1
        if abs(a_return) <= 0.1 and abs(kstr_return) <= 0.1:
            pairs.append((a_return, kstr_return))
    if len(pairs) < 5:
        return 1.0, None, len(pairs), "日收益样本不足·暂用β 1.00"
    a_mean = fmean(pair[0] for pair in pairs)
    kstr_mean = fmean(pair[1] for pair in pairs)
    variance = sum((pair[0] - a_mean) ** 2 for pair in pairs)
    if variance <= 0:
        return 1.0, None, len(pairs), "日收益波动不足·暂用β 1.00"
    raw_beta = sum((pair[0] - a_mean) * (pair[1] - kstr_mean) for pair in pairs) / variance
    if not math.isfinite(raw_beta) or raw_beta <= 0:
        return 1.0, raw_beta if math.isfinite(raw_beta) else None, len(pairs), "日收益回归异常·暂用β 1.00"
    if len(pairs) < 20:
        return 1.0, raw_beta, len(pairs), f"仅{len(daily_rows)}个交易日·原始β {raw_beta:.2f}·暂不启用校正"
    hedge_beta = max(0.5, min(raw_beta, 1.5))
    status = "20日同步收盘收益回归"
    if hedge_beta != raw_beta:
        status = f"原始β {raw_beta:.2f}·执行值限于0.50-1.50"
    return hedge_beta, raw_beta, len(pairs), status


def kstr_history_reference(
    now_utc: datetime,
    a_etf_code: str = KSTR_DEFAULT_A_ETF_CODE,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    cache_seconds: int = KSTR_HISTORY_CACHE_SECONDS,
) -> dict[str, Any]:
    global _kstr_history_cache
    normalized_code = normalize_kstr_a_etf_code(a_etf_code)
    normalized_exchange = normalize_kstr_contract_exchange(contract_exchange)
    cache_key = (normalized_code, normalized_exchange)
    a_etf = KSTR_A_ETF_BY_CODE[normalized_code]
    with _kstr_history_cache_lock:
        cached = _kstr_history_cache.get(cache_key)
        if cache_seconds > 0 and cached and (now_utc - cached[0]).total_seconds() < cache_seconds:
            return dict(cached[1])

    detail_error: str | None = None
    detail_granularity_minutes = 1
    try:
        with http_client(timeout=22.0) as client:
            with ThreadPoolExecutor(max_workers=5) as pool:
                a_baseline_future = pool.submit(sina_intraday_close_rows, client, a_etf["symbol"], 5)
                a_detail_future = pool.submit(sina_intraday_close_rows, client, a_etf["symbol"], 1)
                fx_future = pool.submit(frankfurter_usd_cny_daily_rows, client, now_utc)
                baseline_start_ms = int((now_utc - timedelta(days=32)).timestamp() * 1000)
                detail_start_ms = int((now_utc - timedelta(days=9)).timestamp() * 1000)
                end_ms = int(now_utc.timestamp() * 1000)
                kstr_baseline_future = pool.submit(
                    contract_candle_rows,
                    client,
                    normalized_exchange,
                    "KSTR",
                    "5m",
                    11000,
                    baseline_start_ms,
                    end_ms,
                )
                kstr_detail_future = pool.submit(
                    contract_candle_rows,
                    client,
                    normalized_exchange,
                    "KSTR",
                    "1m",
                    12000,
                    detail_start_ms,
                    end_ms,
                )
                a_baseline_rows = a_baseline_future.result()
                fx_rows = fx_future.result()
                kstr_baseline_rows = kstr_baseline_future.result()
                try:
                    a_detail_rows = a_detail_future.result()
                    kstr_detail_rows = kstr_detail_future.result()
                except (httpx.HTTPError, ValueError) as exc:
                    detail_error = str(exc)
                    detail_granularity_minutes = 5
                    a_detail_rows = a_baseline_rows
                    kstr_detail_rows = kstr_baseline_rows
    except (httpx.HTTPError, ValueError) as exc:
        with _kstr_history_cache_lock:
            stale = _kstr_history_cache.get(cache_key)
        if stale:
            payload = dict(stale[1])
            payload["historyStale"] = True
            payload["historyError"] = str(exc)
            return payload
        raise ValueError(f"KSTR与{normalized_code}历史价差底图读取失败：{exc}") from exc

    structural_model: dict[str, Any] | None = None
    structural_error: str | None = None
    try:
        structural_model = kstr_structural_hedge_model(now_utc, normalized_code)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        # The short contract history still keeps the monitor usable if Yahoo is
        # temporarily unavailable; a cached structural model is preferred above.
        structural_error = str(exc)

    fx_dates = sorted(fx_rows)

    def aligned_points(a_rows: dict[int, float], kstr_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kstr_by_ts = {
            int(row["ts"]): float(row["close"])
            for row in kstr_rows
            if row.get("ts") is not None and row.get("close") is not None and float(row["close"]) > 0
        }
        points: list[dict[str, Any]] = []
        for ts, a_price in sorted(a_rows.items()):
            kstr_price = kstr_by_ts.get(ts)
            if kstr_price is None:
                continue
            local_day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            fx_index = bisect_right(fx_dates, local_day) - 1
            if fx_index < 0:
                continue
            usd_cny = fx_rows[fx_dates[fx_index]]
            raw_ratio = kstr_price * usd_cny / a_price
            if raw_ratio <= 0:
                continue
            points.append(
                {
                    "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    "aEtfPriceCny": a_price,
                    "kstrPriceUsdt": kstr_price,
                    "usdCny": usd_cny,
                    "rawRatio": raw_ratio,
                }
            )
        return points

    baseline_raw_points = aligned_points(a_baseline_rows, kstr_baseline_rows)
    if len(baseline_raw_points) < 30:
        raise ValueError(f"KSTR与{normalized_code}共同交易时段样本不足")
    detail_raw_points = aligned_points(a_detail_rows, kstr_detail_rows)
    if len(detail_raw_points) < 30:
        detail_error = detail_error or "1分钟共同交易样本不足，已回退5分钟"
        detail_granularity_minutes = 5
        detail_raw_points = baseline_raw_points

    reference_ratio = median([row["rawRatio"] for row in baseline_raw_points])

    def normalized_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in rows:
            spread_pct = kstr_symmetric_normalized_spread_pct(row["rawRatio"], reference_ratio)
            if spread_pct is None:
                continue
            items.append(
                {
                    **row,
                    "spreadPct": spread_pct,
                    "kstrEquivalentPriceCny": row["kstrPriceUsdt"] * row["usdCny"] / reference_ratio,
                    "source": "history",
                    "marketPhase": "continuous",
                }
            )
        return items

    baseline_items = normalized_items(baseline_raw_points)
    items = normalized_items(detail_raw_points)
    spreads = [row["spreadPct"] for row in baseline_items]
    mean_spread = fmean(spreads)
    std_spread = pstdev(spreads)
    contract_hedge_beta, hedge_beta_raw, beta_sample_count, contract_beta_status = synchronized_return_beta(baseline_items)
    hedge_beta = contract_hedge_beta
    beta_status = contract_beta_status
    if structural_model is not None:
        structural_beta = float(structural_model["dynamicBeta"])
        if hedge_beta_raw is not None and beta_sample_count >= 20:
            # Contract-specific observations are introduced gradually because
            # KSTRUSDT has a much shorter regime than the U.S. ETF history.
            contract_weight = min(0.5, beta_sample_count / (beta_sample_count + 60.0))
            contract_candidate = max(0.5, min(float(hedge_beta_raw), 1.5))
            hedge_beta = structural_beta * (1 - contract_weight) + contract_candidate * contract_weight
            beta_status = (
                f"执行β {hedge_beta:.2f}·长期结构 {structural_beta:.2f}·"
                f"合约{beta_sample_count}组日收益权重{contract_weight * 100:.0f}%"
            )
        else:
            hedge_beta = structural_beta
            beta_status = (
                f"自动补充{structural_model['sampleCount']}组KSTR/ETF/汇率日线·"
                f"执行β {hedge_beta:.2f}·合约有效日收益仅{beta_sample_count}组"
            )
    shanghai = ZoneInfo("Asia/Shanghai")
    trading_days = sorted({row["time"].astimezone(shanghai).date().isoformat() for row in baseline_items})
    detail_trading_days = sorted({row["time"].astimezone(shanghai).date().isoformat() for row in items})
    payload = {
        "referenceRatio": reference_ratio,
        "items": items,
        "sampleCount": len(items),
        "baselineSampleCount": len(baseline_items),
        "historyStart": items[0]["time"],
        "historyEnd": items[-1]["time"],
        "historyGranularityMinutes": detail_granularity_minutes,
        "historyCoverageTradingDays": len(detail_trading_days),
        "historyDetailError": detail_error,
        "historyMeanPct": mean_spread,
        "historyStdPct": std_spread,
        "historyP10Pct": percentile(spreads, 0.1),
        "historyP90Pct": percentile(spreads, 0.9),
        "historyTradingDays": len(trading_days),
        "baselineTargetDays": 20,
        "hedgeBeta": hedge_beta,
        "hedgeBetaRaw": hedge_beta_raw,
        "contractHedgeBeta": contract_hedge_beta,
        "betaSampleCount": beta_sample_count,
        "betaStatus": beta_status,
        "structuralModel": structural_model,
        "structuralModelError": structural_error,
        "componentMismatch": KSTR_COMPONENT_MISMATCH_SNAPSHOT,
        "historyStale": False,
        "historyError": None,
        "aEtfCode": normalized_code,
        "contractExchange": normalized_exchange,
    }
    with _kstr_history_cache_lock:
        _kstr_history_cache[cache_key] = (now_utc, payload)
    return dict(payload)


def kstr_contract_market_snapshot(
    client: httpx.Client,
    contract_exchange: str,
) -> dict[str, Any]:
    normalized_exchange = normalize_kstr_contract_exchange(contract_exchange)
    option = KSTR_CONTRACT_BY_EXCHANGE[normalized_exchange]
    book = request_json(
        client,
        f"{base_url('bn')}/fapi/v1/ticker/bookTicker",
        {"symbol": option["symbol"]},
    )
    premium = request_json(
        client,
        f"{base_url('bn')}/fapi/v1/premiumIndex",
        {"symbol": option["symbol"]},
    )
    bid = first_float(book.get("bidPrice") if isinstance(book, dict) else None)
    ask = first_float(book.get("askPrice") if isinstance(book, dict) else None)
    reference_time = parse_timestamp_ms(
        book.get("time") if isinstance(book, dict) else None
    )
    mark_price = first_float(
        premium.get("markPrice") if isinstance(premium, dict) else None
    )
    index_price = first_float(
        premium.get("indexPrice") if isinstance(premium, dict) else None
    )
    funding_rate = first_float(
        premium.get("lastFundingRate") if isinstance(premium, dict) else None
    )
    next_funding_time = parse_timestamp_ms(
        premium.get("nextFundingTime") if isinstance(premium, dict) else None
    )

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        raise ValueError(
            f"{option['exchangeName']} {option['symbol']} 未返回有效买卖盘"
        )
    return {
        **option,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "markPrice": mark_price,
        "indexPrice": index_price,
        "fundingRate": funding_rate,
        "nextFundingTime": next_funding_time,
        "referenceTime": reference_time or datetime.now(timezone.utc),
    }


def capture_kstr_auction_observations(
    now: datetime | None = None,
    a_etf_code: str = KSTR_DEFAULT_A_ETF_CODE,
    contract_exchanges: tuple[str, ...] = (KSTR_DEFAULT_CONTRACT_EXCHANGE,),
) -> dict[str, Any]:
    now_utc = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.tzinfo is not None
        else (now.replace(tzinfo=timezone.utc) if isinstance(now, datetime) else datetime.now(timezone.utc))
    )
    if not a_share_open_auction_observation_active(now_utc):
        raise ValueError("当前不在9:20-9:25开盘集合竞价采集窗口")

    normalized_code = normalize_kstr_a_etf_code(a_etf_code)
    a_etf = KSTR_A_ETF_BY_CODE[normalized_code]
    normalized_exchanges = tuple(
        dict.fromkeys(
            normalize_kstr_contract_exchange(exchange)
            for exchange in contract_exchanges
        )
    )
    reference_ratios: dict[str, float] = {}
    for exchange in normalized_exchanges:
        snapshot = load_kstr_page_snapshot(normalized_code, exchange)
        reference_ratio = first_float(
            snapshot.get("referenceRatio") if isinstance(snapshot, dict) else None
        )
        if reference_ratio is None or reference_ratio <= 0:
            reference_ratio = first_float(
                kstr_history_reference(
                    now_utc,
                    normalized_code,
                    contract_exchange=exchange,
                ).get("referenceRatio")
            )
        if reference_ratio is None or reference_ratio <= 0:
            raise ValueError(
                f"{KSTR_CONTRACT_BY_EXCHANGE[exchange]['exchangeName']}历史折算系数不可用"
            )
        reference_ratios[exchange] = reference_ratio

    with http_client(timeout=10.0) as client:
        with ThreadPoolExecutor(max_workers=2 + len(normalized_exchanges)) as pool:
            a_future = pool.submit(tencent_a_share_quote, client, a_etf["symbol"])
            fx_future = pool.submit(usd_cny_quote, client)
            contract_futures = {
                exchange: pool.submit(
                    kstr_contract_market_snapshot,
                    client,
                    exchange,
                )
                for exchange in normalized_exchanges
            }
            a_quote = a_future.result()
            usd_cny, fx_reference_time, fx_source_name, _ = fx_future.result()
            contract_results: dict[str, dict[str, Any]] = {}
            contract_errors: list[dict[str, str]] = []
            for exchange, future in contract_futures.items():
                try:
                    contract_results[exchange] = future.result()
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                    contract_errors.append(
                        {
                            "contractExchange": exchange,
                            "message": str(exc),
                        }
                    )

    a_reference_time = parse_datetime_value(a_quote.get("referenceTime"))
    a_quote_age_seconds = (
        max(0.0, (now_utc - a_reference_time).total_seconds())
        if a_reference_time is not None
        else None
    )
    if a_quote_age_seconds is None or a_quote_age_seconds > 45:
        raise ValueError(
            f"{normalized_code}竞价参考价时间戳超过45秒，未写入观察点"
        )

    market_phase = a_share_market_phase(now_utc)
    auction_time = now_utc.replace(
        second=(now_utc.second // 10) * 10,
        microsecond=0,
    )
    captured: list[dict[str, Any]] = []
    for exchange in normalized_exchanges:
        contract_quote = contract_results.get(exchange)
        if contract_quote is None:
            continue
        contract_reference_time = parse_datetime_value(
            contract_quote.get("referenceTime")
        )
        contract_age_seconds = (
            max(0.0, (now_utc - contract_reference_time).total_seconds())
            if contract_reference_time is not None
            else None
        )
        if contract_age_seconds is None or contract_age_seconds > 45:
            contract_errors.append(
                {
                    "contractExchange": exchange,
                    "message": "KSTR合约报价时间戳超过45秒，未写入观察点",
                }
            )
            continue
        a_price = float(a_quote["price"])
        a_bid = first_float(a_quote.get("bid"))
        a_bid1_price = a_bid if a_bid is not None and a_bid > 0 else a_price
        kstr_bid = float(contract_quote["bid"])
        reference_ratio = reference_ratios[exchange]
        bid1_values = kstr_bid1_spread_values(
            kstr_bid,
            usd_cny,
            a_bid1_price,
            reference_ratio,
        )
        if bid1_values is None:
            contract_errors.append(
                {
                    "contractExchange": exchange,
                    "message": "集合竞价对称价差率计算失败",
                }
            )
            continue
        tick = record_kstr_auction_tick(
            normalized_code,
            exchange,
            {
                "time": auction_time,
                "observedAt": now_utc,
                "aEtfPriceCny": a_bid1_price,
                "aEtfBid": a_bid,
                "aEtfAsk": first_float(a_quote.get("ask")),
                "aEtfReferenceTime": a_reference_time,
                "aEtfSourceName": a_quote.get("sourceName"),
                "kstrPriceUsdt": kstr_bid,
                "kstrBid": kstr_bid,
                "kstrAsk": first_float(contract_quote.get("ask")),
                "kstrReferenceTime": contract_reference_time,
                "usdCny": usd_cny,
                "fxReferenceTime": fx_reference_time,
                "fxSourceName": fx_source_name,
                **bid1_values,
                "marketPhase": market_phase,
                "priceMode": "both_legs_bid1",
                "isFinalMatchPoint": market_phase == "open_auction_matching",
            },
        )
        captured.append(tick)

    if not captured and contract_errors:
        raise ValueError("；".join(error["message"] for error in contract_errors))
    return {
        "status": "ok" if not contract_errors else "partial",
        "capturedAt": now_utc,
        "marketPhase": market_phase,
        "aEtfCode": normalized_code,
        "captured": captured,
        "errors": contract_errors,
    }


def crypto_kstr_a_share_spread(
    now: datetime | None = None,
    cache_seconds: int = KSTR_SPREAD_CACHE_SECONDS,
    a_etf_code: str = KSTR_DEFAULT_A_ETF_CODE,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
) -> dict[str, Any]:
    """Compare an A-share STAR 50 ETF with a selected KSTR equity perpetual."""
    global _kstr_spread_cache, _kstr_auction_ticks
    now_utc = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    requested_code = normalize_kstr_a_etf_code(a_etf_code)
    normalized_exchange = normalize_kstr_contract_exchange(contract_exchange)
    contract_option = KSTR_CONTRACT_BY_EXCHANGE[normalized_exchange]
    comparison = kstr_etf_comparison(
        now_utc,
        contract_exchange=normalized_exchange,
    )
    liquid_codes = {str(option.get("code")) for option in comparison["options"]}
    normalized_code = (
        requested_code
        if requested_code in liquid_codes
        else KSTR_DEFAULT_A_ETF_CODE
        if KSTR_DEFAULT_A_ETF_CODE in liquid_codes
        else next(iter(liquid_codes), KSTR_DEFAULT_A_ETF_CODE)
    )
    cache_key = (normalized_code, normalized_exchange)
    a_etf = KSTR_A_ETF_BY_CODE[normalized_code]
    with _kstr_spread_cache_lock:
        cached = _kstr_spread_cache.get(cache_key)
        if cache_seconds > 0 and cached and (now_utc - cached[0]).total_seconds() < cache_seconds:
            return dict(cached[1])

    history = kstr_history_reference(
        now_utc,
        normalized_code,
        contract_exchange=normalized_exchange,
    )
    with http_client(timeout=10.0) as client:
        with ThreadPoolExecutor(max_workers=3) as pool:
            a_future = pool.submit(tencent_a_share_quote, client, a_etf["symbol"])
            contract_future = pool.submit(
                kstr_contract_market_snapshot,
                client,
                normalized_exchange,
            )
            fx_future = pool.submit(usd_cny_quote, client)
            try:
                a_quote = a_future.result()
                contract_quote = contract_future.result()
                usd_cny, fx_reference_time, fx_source_name, fx_source_url = fx_future.result()
            except (httpx.HTTPError, ValueError) as exc:
                raise ValueError(f"科创50/KSTR实时价差读取失败：{exc}") from exc

    kstr_bid = float(contract_quote["bid"])
    kstr_ask = float(contract_quote["ask"])
    kstr_mid = float(contract_quote["mid"])
    raw_book_time = contract_quote["referenceTime"]
    book_time = raw_book_time
    reference_ratio = float(history["referenceRatio"])
    a_price = float(a_quote["price"])
    a_bid = first_float(a_quote.get("bid"))
    a_ask = first_float(a_quote.get("ask"))
    market_phase = a_share_market_phase(now_utc)
    market_open = market_phase == "continuous"

    def quote_age_seconds(reference_time: Any) -> float | None:
        if not isinstance(reference_time, datetime):
            return None
        normalized = reference_time.astimezone(timezone.utc)
        return max(0.0, (now_utc - normalized).total_seconds())

    quote_fresh_threshold_seconds = 45
    a_quote_age_seconds = quote_age_seconds(a_quote.get("referenceTime"))
    kstr_quote_age_seconds = quote_age_seconds(raw_book_time)
    a_quote_fresh = a_quote_age_seconds is not None and a_quote_age_seconds <= quote_fresh_threshold_seconds
    kstr_quote_fresh = kstr_quote_age_seconds is not None and kstr_quote_age_seconds <= quote_fresh_threshold_seconds
    quotes_fresh = a_quote_fresh and kstr_quote_fresh
    execution_ready = market_open and quotes_fresh
    if market_phase == "open_auction_no_cancel":
        execution_block_reason = "A股9:20-9:25开盘集合竞价不可撤单，虚拟参考价不保证同步成交"
    elif market_phase.startswith("open_auction"):
        execution_block_reason = "A股处于开盘集合竞价阶段，不按连续竞价盘口执行"
    elif market_phase == "close_auction":
        execution_block_reason = "A股处于收盘集合竞价阶段，不按连续竞价盘口执行"
    elif market_phase == "lunch_break":
        execution_block_reason = "A股午间休市，价差停在上午最近同步点"
    elif not market_open:
        execution_block_reason = "A股不在连续竞价时段"
    elif not a_quote_fresh:
        execution_block_reason = f"{normalized_code}报价超过45秒，A股可能休市或行情已停止"
    elif not kstr_quote_fresh:
        execution_block_reason = "KSTR报价超过45秒或缺少时间戳"
    elif a_bid is None or a_ask is None or a_bid <= 0 or a_ask <= 0:
        execution_block_reason = f"{normalized_code}买卖盘不完整"
        execution_ready = False
    else:
        execution_block_reason = None

    mid_raw_ratio = kstr_mid * usd_cny / a_price
    mid_spread_pct = kstr_symmetric_normalized_spread_pct(mid_raw_ratio, reference_ratio)
    if mid_spread_pct is None:
        raise ValueError("KSTR与A股ETF对称归一化价差率计算失败")
    a_bid1_price = a_bid if a_bid is not None and a_bid > 0 else a_price
    bid1_values = kstr_bid1_spread_values(
        kstr_bid,
        usd_cny,
        a_bid1_price,
        reference_ratio,
    )
    if bid1_values is None:
        raise ValueError("KSTR与A股ETF买一价差率计算失败")
    bid1_spread_pct = bid1_values["spreadPct"]
    order_book_spreads = kstr_order_book_spread_values(
        kstr_bid,
        kstr_ask,
        usd_cny,
        a_bid,
        a_ask,
        reference_ratio,
        execution_ready=execution_ready,
    )
    open_spread_pct = order_book_spreads["openSpreadPct"]
    close_spread_pct = order_book_spreads["closeSpreadPct"]
    history_std = first_float(history.get("historyStdPct")) or 0
    history_mean = first_float(history.get("historyMeanPct")) or 0
    z_score = (bid1_spread_pct - history_mean) / history_std if history_std > 0 else None
    open_z_score = (
        (open_spread_pct - history_mean) / history_std
        if open_spread_pct is not None and history_std > 0
        else None
    )
    live_observation_time = now_utc.replace(
        second=(now_utc.second // 10) * 10,
        microsecond=0,
    )
    live_item = {
        "time": live_observation_time,
        "observedAt": now_utc,
        "aEtfPriceCny": a_bid1_price,
        "kstrPriceUsdt": kstr_bid,
        "usdCny": usd_cny,
        **bid1_values,
        "source": "live",
        "marketPhase": market_phase,
        "priceMode": "both_legs_bid1",
    }
    items = list(history["items"])
    if execution_ready:
        record_kstr_continuous_tick(
            normalized_code,
            normalized_exchange,
            live_item,
        )
    continuous_ticks = load_kstr_continuous_ticks(
        normalized_code,
        normalized_exchange,
        now=now_utc,
    )
    if continuous_ticks:
        oldest_history_time = (
            items[0]["time"] if items else now_utc - timedelta(days=9)
        )
        items.extend(
            row for row in continuous_ticks if row["time"] >= oldest_history_time
        )

    auction_observation_active = a_share_open_auction_observation_active(now_utc)
    if auction_observation_active and quotes_fresh:
        auction_time = now_utc.replace(
            second=(now_utc.second // 10) * 10,
            microsecond=0,
        )
        record_kstr_auction_tick(
            normalized_code,
            normalized_exchange,
            {
                **live_item,
                "time": auction_time,
                "observedAt": now_utc,
                "aEtfBid": a_bid,
                "aEtfAsk": a_ask,
                "aEtfReferenceTime": a_quote.get("referenceTime"),
                "aEtfSourceName": a_quote.get("sourceName"),
                "kstrBid": kstr_bid,
                "kstrAsk": kstr_ask,
                "kstrReferenceTime": raw_book_time,
                "fxReferenceTime": fx_reference_time,
                "fxSourceName": fx_source_name,
                "priceMode": "tencent_realtime_auction_reference",
                "isFinalMatchPoint": market_phase == "open_auction_matching",
            },
        )

    auction_ticks = load_kstr_auction_ticks(
        normalized_code,
        normalized_exchange,
        now=now_utc,
    )
    if auction_ticks:
        oldest_history_time = items[0]["time"] if items else now_utc - timedelta(days=9)
        items.extend(row for row in auction_ticks if row["time"] >= oldest_history_time)
        items.sort(key=lambda row: row["time"])
    by_item_key: dict[tuple[datetime, str], dict[str, Any]] = {}
    for row in items:
        item_time = parse_datetime_value(row.get("time"))
        if item_time is None:
            continue
        by_item_key[(item_time, str(row.get("source") or ""))] = row
    items = [
        by_item_key[key]
        for key in sorted(by_item_key, key=lambda value: (value[0], value[1]))
    ]

    session_comparison = kstr_session_comparison(
        items,
        kstr_bid1=kstr_bid,
        usd_cny=usd_cny,
        indicative_spread_pct=bid1_spread_pct,
        history_mean_pct=history_mean,
        history_std_pct=history_std,
        execution_ready=execution_ready,
        reference_time=book_time,
    )

    trigger_threshold_pct = 2.0
    trigger_z_threshold = 1.5
    shanghai = ZoneInfo("Asia/Shanghai")
    local_trade_date = now_utc.astimezone(shanghai).date()
    today_items = [
        row
        for row in items
        if is_synchronized_kstr_spread_point(row)
        and row["time"].astimezone(shanghai).date() == local_trade_date
    ]
    first_trigger_row = next(
        (
            row
            for row in today_items
            if row["spreadPct"] >= trigger_threshold_pct
            and history_std > 0
            and (row["spreadPct"] - history_mean) / history_std >= trigger_z_threshold
        ),
        None,
    )
    first_trigger = None
    if first_trigger_row is not None:
        first_trigger = {
            "time": first_trigger_row["time"],
            "spreadPct": first_trigger_row["spreadPct"],
            "zScore": (first_trigger_row["spreadPct"] - history_mean) / history_std if history_std > 0 else None,
            "aEtfPriceCny": first_trigger_row["aEtfPriceCny"],
            "kstrPriceUsdt": first_trigger_row["kstrPriceUsdt"],
            "source": first_trigger_row["source"],
        }

    hedge_beta = first_float(history.get("hedgeBeta")) or 1.0
    hedge_etf_per_kstr = reference_ratio * hedge_beta

    if market_phase == "open_auction_no_cancel":
        signal_state = "9:20-9:25集合竞价·仅观察"
    elif market_phase.startswith("open_auction"):
        signal_state = "开盘集合竞价·仅观察"
    elif market_phase == "close_auction":
        signal_state = "收盘集合竞价·仅观察"
    elif market_phase == "lunch_break":
        signal_state = "A股午休·价差冻结"
    elif not market_open:
        signal_state = "A股休市·价差冻结"
    elif not execution_ready:
        signal_state = "报价未同步·价差冻结"
    elif z_score is not None and z_score >= 2:
        signal_state = "KSTR显著偏贵"
    elif z_score is not None and z_score <= -2:
        signal_state = "KSTR显著偏便宜"
    else:
        signal_state = "历史中性区"

    payload = {
        "status": "ok",
        "source": f"tencent_sina_ecb_{normalized_exchange}",
        "updatedAt": book_time,
        "aMarketOpen": market_open,
        "aMarketPhase": market_phase,
        "executionReady": execution_ready,
        "executionBlockReason": execution_block_reason,
        "quoteFreshness": {
            "thresholdSeconds": quote_fresh_threshold_seconds,
            "aEtfAgeSeconds": a_quote_age_seconds,
            "kstrAgeSeconds": kstr_quote_age_seconds,
        },
        "signalState": signal_state,
        "sessionComparison": session_comparison,
        "referenceRatio": reference_ratio,
        "referenceDescription": f"KSTR×USD/CNY÷{normalized_code} 的共同交易时段历史中位数",
        "spreadMetric": {
            "key": "symmetric_normalized_spread_pct",
            "name": "买一价对称归一化价差率",
            "formula": "2×(双方买一折算比率-历史中位比率)÷(双方买一折算比率+历史中位比率)×100%",
            "orientation": f"正值表示KSTR相对{normalized_code}偏贵，负值表示KSTR相对{normalized_code}偏便宜",
            "rangePct": [-200, 200],
        },
        "latest": {
            "midSpreadPct": mid_spread_pct,
            "bidSpreadPct": bid1_spread_pct,
            **order_book_spreads,
            "zScore": z_score,
            "openZScore": open_z_score,
            "hedgeKstrPer10000Etf": 10000 / hedge_etf_per_kstr,
            "hedgeEtfPer100Kstr": hedge_etf_per_kstr * 100,
            "etfSharesPerKstr": reference_ratio,
            "hedgeBeta": hedge_beta,
        },
        "aEtf": {
            **a_quote,
            "manager": a_etf["manager"],
            "fullName": a_etf["fullName"],
            "optionEligible": a_etf["optionEligible"],
            "isOpen": market_open,
            "tradeRule": "A股T+1",
            "trackingIndex": "上证科创板50成份指数",
        },
        "aEtfOptions": comparison["options"],
        "liquidityFilter": comparison["liquidityFilter"],
        "priceClosestAEtfCode": comparison["priceClosestAEtfCode"],
        "executionDefaultAEtfCode": comparison["executionDefaultAEtfCode"],
        "comparisonTradingDays": comparison["comparisonTradingDays"],
        "comparisonUpdatedAt": comparison["comparisonUpdatedAt"],
        "comparisonMethod": comparison["comparisonMethod"],
        "comparisonError": comparison["comparisonError"],
        "priceClosestReason": comparison["priceClosestReason"],
        "executionDefaultReason": comparison["executionDefaultReason"],
        "kstrUnderlying": comparison["kstrUnderlying"],
        "optionEvidenceUrl": comparison["optionEvidenceUrl"],
        "contractOptions": [
            {
                **option,
                "selected": option["exchange"] == normalized_exchange,
            }
            for option in KSTR_CONTRACT_OPTIONS
        ],
        "kstr": {
            "exchange": normalized_exchange,
            "exchangeName": contract_option["exchangeName"],
            "symbol": contract_option["symbol"],
            "contractType": contract_option["contractType"],
            "underlyingType": contract_option["underlyingType"],
            "bid": kstr_bid,
            "ask": kstr_ask,
            "mid": kstr_mid,
            "markPrice": contract_quote.get("markPrice"),
            "indexPrice": contract_quote.get("indexPrice"),
            "fundingRate": contract_quote.get("fundingRate"),
            "nextFundingTime": contract_quote.get("nextFundingTime"),
            "referenceTime": book_time,
            "sourceName": contract_option["sourceName"],
            "sourceUrl": contract_option["sourceUrl"],
        },
        "fx": {
            "usdCny": usd_cny,
            "referenceTime": fx_reference_time,
            "sourceName": fx_source_name,
            "sourceUrl": fx_source_url,
        },
        "sampleCount": history["sampleCount"],
        "historyStart": history["historyStart"],
        "historyEnd": history["historyEnd"],
        "historyGranularityMinutes": history.get("historyGranularityMinutes", 5),
        "historyCoverageTradingDays": history.get("historyCoverageTradingDays", history["historyTradingDays"]),
        "historyDetailError": history.get("historyDetailError"),
        "baselineSampleCount": history.get("baselineSampleCount", history["sampleCount"]),
        "historyMeanPct": history["historyMeanPct"],
        "historyStdPct": history["historyStdPct"],
        "historyP10Pct": history["historyP10Pct"],
        "historyP90Pct": history["historyP90Pct"],
        "historyTradingDays": history["historyTradingDays"],
        "baselineTargetDays": history["baselineTargetDays"],
        "hedgeBeta": history["hedgeBeta"],
        "hedgeBetaRaw": history["hedgeBetaRaw"],
        "contractHedgeBeta": history.get("contractHedgeBeta"),
        "betaSampleCount": history["betaSampleCount"],
        "betaStatus": history["betaStatus"],
        "structuralModel": history.get("structuralModel"),
        "structuralModelError": history.get("structuralModelError"),
        "componentMismatch": history.get("componentMismatch", KSTR_COMPONENT_MISMATCH_SNAPSHOT),
        "triggerThresholdPct": trigger_threshold_pct,
        "triggerZThreshold": trigger_z_threshold,
        "firstTrigger": first_trigger,
        "historyStale": history.get("historyStale", False),
        "historyError": history.get("historyError"),
        "auctionWindow": {
            "start": "09:20",
            "end": "09:25",
            "phase": "open_auction_no_cancel",
            "isActive": auction_observation_active,
            "capturedPoints": len(auction_ticks),
            "backgroundCaptureEnabled": True,
            "captureIntervalSeconds": 10,
            "lastCapturedAt": (
                auction_ticks[-1]["observedAt"] if auction_ticks else None
            ),
            "historyBackfillAvailable": False,
            "note": "每10秒刷新；9:20-9:25集合竞价价差与9:30后连续竞价价差在同一张图显示。",
        },
        "items": items,
        "riskNotes": [
            "主指标采用对称归一化价差率；交换两腿后绝对值相同、符号相反，取值范围为-200%至200%。",
            "实时观察价差与图表最新点采用KSTR买一和A股ETF买一；实际开仓仍按KSTR买一和A股ETF卖一计算。",
            "两只产品只共享指数方向，份额不可互换；折算系数采用共同交易时段历史中位数，不是申赎转换比例。",
            "KSTR当前官方持仓缺少中芯国际（688981），估算对科创50形成约8.71%成分主动偏离，单一Beta无法消除。",
            "长期结构Beta按每日KSTR美元价乘当日USD/CNY后与境内ETF对齐，不使用固定汇率。",
            f"KSTR合约跟踪美国上市KSTR ETF的市场定价，并非直接交割科创50指数或{normalized_code}份额。",
            f"A股ETF为T+1且有交易时段和涨跌幅限制；美股、A股与{contract_option['exchangeName']}交易时段不一致。",
            "周末和A股休市期间KSTR仍可能由离岸订单簿定价，不能把单边跳动直接当成可套利价差。",
            f"KSTR与{normalized_code}存在管理费、跟踪误差、ETF折溢价和各自申赎机制差异。",
            "KSTR资金费率、保证金和强平风险会改变持有期收益，正价差并不保证最终收敛。",
            "美元、人民币与USDT资金分离；汇率、滑点、两边手续费及报价时效都必须计入。",
        ],
        "message": f"观察价差按KSTR与{normalized_code}双方买一价；可成交开仓口径仍为买入{normalized_code}卖一价、做空KSTR买一价。",
    }
    try_archive_kstr_quote_payload(payload, normalized_code)
    with _kstr_spread_cache_lock:
        _kstr_spread_cache[cache_key] = (now_utc, payload)
    save_kstr_page_snapshot(
        normalized_code,
        payload,
        normalized_exchange,
    )
    return dict(payload)


def start_kstr_page_snapshot_refresh(
    a_etf_code: str,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    now: datetime | None = None,
) -> bool:
    now_utc = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.tzinfo is not None
        else (now.replace(tzinfo=timezone.utc) if isinstance(now, datetime) else datetime.now(timezone.utc))
    )
    if not a_share_kstr_refresh_window_active(now_utc):
        return False
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    cache_key = (normalized_code, normalized_exchange)
    with _kstr_page_snapshot_lock:
        active = _kstr_page_refresh_threads.get(cache_key)
        if active and active.is_alive():
            return False

        def worker() -> None:
            started_at = time.perf_counter()
            try:
                payload = crypto_kstr_a_share_spread(
                    a_etf_code=normalized_code,
                    contract_exchange=normalized_exchange,
                cache_seconds=KSTR_SPREAD_CACHE_SECONDS,
                )
                append_kstr_runtime_event(
                    "background_refresh_success",
                    stage="market_data",
                    etf_code=normalized_code,
                    message="后台实时行情刷新成功",
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    details={
                        "updatedAt": payload.get("updatedAt"),
                        "executionReady": payload.get("executionReady"),
                        "sampleCount": payload.get("sampleCount"),
                        "itemCount": len(payload.get("items") or []),
                        "contractExchange": normalized_exchange,
                    },
                )
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                append_kstr_runtime_event(
                    "background_refresh_failed",
                    level="error",
                    stage="market_data",
                    status="failed",
                    etf_code=normalized_code,
                    message=f"后台实时行情刷新失败：{exc}",
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    dedupe_key=f"background-refresh-error:{normalized_code}:{normalized_exchange}:{type(exc).__name__}",
                    min_interval_seconds=30,
                )
                return

        thread = threading.Thread(
            target=worker,
            name=f"kstr-page-refresh-{normalized_code}-{normalized_exchange}",
            daemon=True,
        )
        _kstr_page_refresh_threads[cache_key] = thread
        thread.start()
    return True


def crypto_kstr_a_share_spread_page(
    a_etf_code: str = KSTR_DEFAULT_A_ETF_CODE,
    contract_exchange: str = KSTR_DEFAULT_CONTRACT_EXCHANGE,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.tzinfo is not None
        else (now.replace(tzinfo=timezone.utc) if isinstance(now, datetime) else datetime.now(timezone.utc))
    )
    refresh_window_active = a_share_kstr_refresh_window_active(now_utc)
    normalized_code, normalized_exchange = kstr_pair_cache_key(
        a_etf_code,
        contract_exchange,
    )
    snapshot = load_kstr_page_snapshot(normalized_code, normalized_exchange)
    if snapshot is not None:
        refresh_started = start_kstr_page_snapshot_refresh(
            normalized_code,
            normalized_exchange,
            now=now_utc,
        )
        snapshot_time = parse_datetime_value(snapshot.get("updatedAt"))
        snapshot_age_seconds = (
            max(0.0, (now_utc - snapshot_time).total_seconds())
            if snapshot_time is not None
            else None
        )
        response_snapshot = dict(snapshot)
        if not refresh_window_active:
            current_phase = a_share_market_phase(now_utc)
            session_comparison = dict(response_snapshot.get("sessionComparison") or {})
            session_comparison.update(
                {
                    "isSynchronizedNow": False,
                    "spreadFrozen": True,
                }
            )
            auction_window = dict(response_snapshot.get("auctionWindow") or {})
            auction_window["isActive"] = False
            response_snapshot.update(
                {
                    "aMarketOpen": False,
                    "aMarketPhase": current_phase,
                    "executionReady": False,
                    "executionBlockReason": "当前不在A股交易时段，保留最后结果",
                    "signalState": "非刷新时段·保留最后结果",
                    "sessionComparison": session_comparison,
                    "auctionWindow": auction_window,
                }
            )
        return {
            **response_snapshot,
            "diagnostics": {
                "servedFrom": "snapshot",
                "snapshotAgeSeconds": snapshot_age_seconds,
                "backgroundRefreshStarted": refresh_started,
                "autoRefreshActive": refresh_window_active,
                "autoRefreshWindow": "A股交易日 09:20–11:30 / 13:00–15:00",
            },
        }
    if not refresh_window_active:
        raise ValueError("当前不在A股交易时段，且暂无可显示的历史结果")
    payload = crypto_kstr_a_share_spread(
        now=now_utc,
        a_etf_code=normalized_code,
        contract_exchange=normalized_exchange,
        cache_seconds=12,
    )
    saved = save_kstr_page_snapshot(
        normalized_code,
        payload,
        normalized_exchange,
        force_disk=True,
    )
    return {
        **saved,
        "diagnostics": {
            "servedFrom": "live",
            "snapshotAgeSeconds": 0,
            "backgroundRefreshStarted": False,
            "autoRefreshActive": True,
            "autoRefreshWindow": "A股交易日 09:20–11:30 / 13:00–15:00",
        },
    }


def kstr_spread_payload_for_range(
    payload: dict[str, Any],
    range_days: int = 7,
    granularity_minutes: int = 15,
) -> dict[str, Any]:
    """Return only the selected A-share trading sessions without altering the snapshot."""
    bounded_days = max(1, min(int(range_days), 40))
    requested_granularity = max(1, min(int(granularity_minutes), 30))
    source_granularity = max(1, int(first_float(payload.get("historyGranularityMinutes")) or 1))
    effective_granularity = max(source_granularity, requested_granularity)
    items = [row for row in payload.get("items") or [] if isinstance(row, dict)]
    shanghai = ZoneInfo("Asia/Shanghai")
    dated_items: list[tuple[dict[str, Any], date, datetime]] = []
    for row in items:
        row_time = parse_datetime_value(row.get("time"))
        if row_time is None:
            continue
        normalized_time = (
            row_time.replace(tzinfo=timezone.utc)
            if row_time.tzinfo is None
            else row_time.astimezone(timezone.utc)
        )
        dated_items.append(
            (
                row,
                normalized_time.astimezone(shanghai).date(),
                normalized_time,
            )
        )
    trading_days = sorted({trading_day for _, trading_day, _ in dated_items})
    selected_days = set(trading_days[-bounded_days:])
    selected_items = [
        (row, row_time)
        for row, trading_day, row_time in dated_items
        if trading_day in selected_days
    ]
    auction_items: list[tuple[dict[str, Any], datetime]] = []
    continuous_buckets: dict[int, tuple[dict[str, Any], datetime]] = {}
    bucket_seconds = effective_granularity * 60
    for row, row_time in selected_items:
        if row.get("source") == "auction":
            auction_items.append((row, row_time))
            continue
        bucket = int(row_time.timestamp()) // bucket_seconds
        previous = continuous_buckets.get(bucket)
        if previous is None or row_time >= previous[1]:
            continuous_buckets[bucket] = (row, row_time)
    returned_items = [
        row
        for row, _ in sorted(
            [*auction_items, *continuous_buckets.values()],
            key=lambda item: item[1],
        )
    ]
    result = dict(payload)
    result["items"] = returned_items
    result["rangeDays"] = bounded_days
    result["rangeTradingDays"] = len(selected_days)
    result["sourceHistoryGranularityMinutes"] = source_granularity
    result["historyGranularityMinutes"] = effective_granularity
    result["totalItemCount"] = len(items)
    result["returnedItemCount"] = len(returned_items)
    return result


def usd_cny_quote(client: httpx.Client) -> tuple[float, datetime | None, str, str]:
    usd_krw_url = "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_USDKRW"
    cny_krw_url = "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_CNYKRW"
    try:
        usd_payload = external_stock_json(client, usd_krw_url)
        cny_payload = external_stock_json(client, cny_krw_url)
        usd_result = usd_payload.get("result") if isinstance(usd_payload, dict) else {}
        cny_result = cny_payload.get("result") if isinstance(cny_payload, dict) else {}
        usd_krw = stock_quote_number(usd_result.get("closePrice") if isinstance(usd_result, dict) else None)
        cny_krw = stock_quote_number(cny_result.get("closePrice") if isinstance(cny_result, dict) else None)
        reference_time = parse_datetime_value(
            usd_result.get("localTradedAt") if isinstance(usd_result, dict) else None
        ) or parse_datetime_value(cny_result.get("localTradedAt") if isinstance(cny_result, dict) else None)
        if usd_krw is not None and cny_krw is not None and usd_krw > 0 and cny_krw > 0:
            return usd_krw / cny_krw, reference_time, "Naver Finance（交叉汇率）", usd_krw_url
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        pass

    yahoo_url = "https://query1.finance.yahoo.com/v8/finance/chart/CNY=X?interval=1m&range=1d"
    try:
        payload = external_stock_json(client, yahoo_url)
        chart = payload.get("chart") if isinstance(payload, dict) else {}
        results = chart.get("result") if isinstance(chart, dict) else []
        result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
        meta = result.get("meta") if isinstance(result, dict) else {}
        rate = first_float(meta.get("regularMarketPrice") if isinstance(meta, dict) else None)
        reference_time = parse_timestamp_s(meta.get("regularMarketTime") if isinstance(meta, dict) else None)
        if rate is not None and rate > 0:
            return rate, reference_time, "Yahoo Finance", yahoo_url
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        pass
    raise ValueError("未返回有效的美元兑人民币汇率")


def nasdaq_stock_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip().removeprefix("Closed at ").removesuffix(".").removesuffix(" ET")
    if not raw:
        return None
    parsed = None
    for pattern in ("%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(raw, pattern)
            if pattern == "%b %d, %Y":
                parsed = parsed.replace(hour=16)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return parsed.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def nasdaq_extended_trade(payload: Any) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return None
    trade_table = data.get("tradeDetailTable")
    trade_rows = trade_table.get("rows") if isinstance(trade_table, dict) else None
    latest_row = trade_rows[0] if isinstance(trade_rows, list) and trade_rows and isinstance(trade_rows[0], dict) else {}
    price = stock_quote_number(latest_row.get("price"))
    if price is None:
        info_table = data.get("infoTable")
        info_rows = info_table.get("rows") if isinstance(info_table, dict) else None
        info_row = info_rows[0] if isinstance(info_rows, list) and info_rows and isinstance(info_rows[0], dict) else {}
        consolidated = str(info_row.get("consolidated") or "").strip()
        price = stock_quote_number(consolidated.split()[0]) if consolidated else None
    if price is None or price <= 0:
        return None

    update_time = None
    update_lines = data.get("lastUpdateInfo")
    if isinstance(update_lines, list):
        for line in update_lines:
            raw_line = str(line or "").strip()
            prefix = "Data last updated "
            if raw_line.startswith(prefix):
                update_time = nasdaq_stock_timestamp(raw_line[len(prefix) :])
                if update_time is not None:
                    break
    trade_time = str(latest_row.get("time") or "").strip()
    if update_time is not None and trade_time:
        try:
            parsed_trade_time = datetime.strptime(trade_time, "%H:%M:%S")
            update_et = update_time.astimezone(ZoneInfo("America/New_York"))
            update_time = update_et.replace(
                hour=parsed_trade_time.hour,
                minute=parsed_trade_time.minute,
                second=parsed_trade_time.second,
                microsecond=0,
            ).astimezone(timezone.utc)
        except ValueError:
            pass
    return {"price": price, "referenceTime": update_time}


def nasdaq_stock_daily_close_rows(client: httpx.Client, symbol: str, now_utc: datetime) -> dict[str, float]:
    start_date = (now_utc - timedelta(days=120)).date().isoformat()
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/historical?"
        + urlencode(
            {
                "assetclass": "stocks",
                "fromdate": start_date,
                "todate": now_utc.date().isoformat(),
                "limit": 500,
            }
        )
    )
    payload = external_stock_json(client, url)
    data = payload.get("data") if isinstance(payload, dict) else {}
    table = data.get("tradesTable") if isinstance(data, dict) else {}
    source_rows = table.get("rows") if isinstance(table, dict) else []
    rows: dict[str, float] = {}
    for item in source_rows if isinstance(source_rows, list) else []:
        if not isinstance(item, dict):
            continue
        price = stock_quote_number(item.get("close"))
        try:
            trade_date = datetime.strptime(str(item.get("date") or ""), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        if price is not None and price > 0:
            rows[trade_date] = price
    if not rows:
        raise ValueError(f"Nasdaq {symbol} 未返回日线历史")
    return rows


def naver_stock_daily_close_rows(client: httpx.Client, code: str) -> dict[str, float]:
    url = f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=60&page=1"
    payload = external_stock_json(client, url)
    rows: dict[str, float] = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        trade_date = str(item.get("localTradedAt") or "").strip()
        price = stock_quote_number(item.get("closePrice"))
        if trade_date and price is not None and price > 0:
            rows[trade_date] = price
    if not rows:
        raise ValueError(f"Naver {code} 未返回日线历史")
    return rows


def naver_usd_krw_daily_close_rows(client: httpx.Client) -> dict[str, float]:
    url = (
        "https://m.stock.naver.com/front-api/marketIndex/prices?"
        + urlencode(
            {
                "category": "exchange",
                "reutersCode": "FX_USDKRW",
                "page": 1,
                "pageSize": 60,
            }
        )
    )
    payload = external_stock_json(client, url)
    source_rows = payload.get("result") if isinstance(payload, dict) else []
    rows: dict[str, float] = {}
    for item in source_rows if isinstance(source_rows, list) else []:
        if not isinstance(item, dict):
            continue
        trade_date = str(item.get("localTradedAt") or "").strip()
        price = stock_quote_number(item.get("closePrice"))
        if trade_date and price is not None and price > 0:
            rows[trade_date] = price
    if not rows:
        raise ValueError("Naver 未返回美元兑韩元日线历史")
    return rows


def sk_hynix_stock_history(
    now_utc: datetime,
    cache_seconds: int = SK_HYNIX_STOCK_HISTORY_CACHE_SECONDS,
) -> dict[str, Any]:
    global _sk_hynix_stock_history_cache
    with _sk_hynix_stock_history_cache_lock:
        cached = _sk_hynix_stock_history_cache
        if cache_seconds > 0 and cached and (now_utc - cached[0]).total_seconds() < cache_seconds:
            return dict(cached[1])

    with http_client(timeout=12.0) as client:
        with ThreadPoolExecutor(max_workers=3) as pool:
            us_future = pool.submit(nasdaq_stock_daily_close_rows, client, "SKHY", now_utc)
            kr_future = pool.submit(naver_stock_daily_close_rows, client, "000660")
            fx_future = pool.submit(naver_usd_krw_daily_close_rows, client)
            us_rows = us_future.result()
            kr_rows = kr_future.result()
            fx_rows = fx_future.result()

    adr_ratio = 0.1
    common_dates = sorted(set(us_rows) & set(kr_rows) & set(fx_rows))
    items: list[dict[str, Any]] = []
    for trade_date in common_dates:
        us_price = us_rows[trade_date]
        kr_price = kr_rows[trade_date]
        usd_krw = fx_rows[trade_date]
        kr_equivalent_usd = kr_price * adr_ratio / usd_krw
        spread_pct = pair_spread_symmetric_pct(us_price, kr_equivalent_usd)
        items.append(
            {
                "time": f"{trade_date}T12:00:00Z",
                "leftPrice": us_price,
                "leftBid": None,
                "leftAsk": None,
                "rightPriceRaw": kr_price,
                "rightPrice": kr_equivalent_usd,
                "rightBidRaw": None,
                "rightAskRaw": None,
                "rightBid": None,
                "rightAsk": None,
                "spreadAbs": us_price - kr_equivalent_usd,
                "spreadPct": spread_pct,
                "openSpreadPct": None,
                "closeSpreadPct": None,
                "rawRatio": us_price / kr_equivalent_usd,
                "source": "daily_close",
                "dataQuality": "same_trade_date_close",
                "fxRate": usd_krw,
                "standardPremiumPct": (us_price / kr_equivalent_usd - 1) * 100,
            }
        )
    if not items:
        raise ValueError("SKHY 与 KRX 000660 没有共同交易日日线")
    payload = {
        "items": items,
        "sampleCount": len(items),
        "historyStart": common_dates[0],
        "historyEnd": common_dates[-1],
        "sourceName": "Nasdaq / Naver Finance",
    }
    with _sk_hynix_stock_history_cache_lock:
        _sk_hynix_stock_history_cache = (now_utc, payload)
    return dict(payload)


def crypto_sk_hynix_us_kr_spread(
    now: datetime | None = None,
    cache_seconds: int = SK_HYNIX_STOCK_QUOTE_CACHE_SECONDS,
) -> dict[str, Any]:
    global _sk_hynix_stock_quote_cache
    now_utc = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    with _sk_hynix_stock_quote_cache_lock:
        cached = _sk_hynix_stock_quote_cache
        if cache_seconds > 0 and cached and (now_utc - cached[0]).total_seconds() < cache_seconds:
            return dict(cached[1])

    with http_client(timeout=8.0) as client:
        with ThreadPoolExecutor(max_workers=5) as pool:
            us_future = pool.submit(
                external_stock_json,
                client,
                "https://api.nasdaq.com/api/quote/SKHY/info?assetclass=stocks",
            )
            us_pre_future = pool.submit(
                external_stock_json,
                client,
                "https://api.nasdaq.com/api/quote/SKHY/extended-trading?assetclass=stocks&markettype=pre",
            )
            us_post_future = pool.submit(
                external_stock_json,
                client,
                "https://api.nasdaq.com/api/quote/SKHY/extended-trading?assetclass=stocks&markettype=post",
            )
            kr_future = pool.submit(external_stock_json, client, "https://m.stock.naver.com/api/stock/000660/basic")
            fx_future = pool.submit(
                external_stock_json,
                client,
                "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_USDKRW",
            )
            try:
                us_payload = us_future.result()
                kr_payload = kr_future.result()
                fx_payload = fx_future.result()
            except httpx.HTTPError as exc:
                raise ValueError(f"SK海力士股票行情源读取失败：{exc}") from exc
            try:
                us_pre_payload = us_pre_future.result()
            except (httpx.HTTPError, ValueError):
                us_pre_payload = {}
            try:
                us_post_payload = us_post_future.result()
            except (httpx.HTTPError, ValueError):
                us_post_payload = {}

    us_data = us_payload.get("data") if isinstance(us_payload, dict) else {}
    us_status = str(us_data.get("marketStatus") or "").strip()
    us_open = us_status.lower() in {"market open", "open"}
    us_primary = us_data.get("primaryData") if isinstance(us_data, dict) else {}
    us_secondary = us_data.get("secondaryData") if isinstance(us_data, dict) else {}
    us_quote = us_primary if us_open or not isinstance(us_secondary, dict) or not us_secondary else us_secondary
    us_price = stock_quote_number(us_quote.get("lastSalePrice") if isinstance(us_quote, dict) else None)
    us_bid = stock_quote_number(us_quote.get("bidPrice") if isinstance(us_quote, dict) else None)
    us_ask = stock_quote_number(us_quote.get("askPrice") if isinstance(us_quote, dict) else None)
    us_reference_time = nasdaq_stock_timestamp(us_quote.get("lastTradeTimestamp") if isinstance(us_quote, dict) else None)
    us_price_mode = "live" if us_open else "last_close"
    if not us_open:
        extended_candidates = []
        pre_trade = nasdaq_extended_trade(us_pre_payload)
        post_trade = nasdaq_extended_trade(us_post_payload)
        if pre_trade is not None:
            extended_candidates.append((pre_trade, "pre_market"))
        if post_trade is not None:
            extended_candidates.append((post_trade, "after_hours"))
        extended_candidates = [
            candidate
            for candidate in extended_candidates
            if candidate[0].get("referenceTime") is not None
            and (us_reference_time is None or candidate[0]["referenceTime"] > us_reference_time)
        ]
        if extended_candidates:
            extended_trade, us_price_mode = max(extended_candidates, key=lambda candidate: candidate[0]["referenceTime"])
            us_price = extended_trade["price"]
            us_reference_time = extended_trade["referenceTime"]
            us_bid = None
            us_ask = None

    kr_price = stock_quote_number(kr_payload.get("closePrice") if isinstance(kr_payload, dict) else None)
    kr_status = str(kr_payload.get("marketStatus") or "").strip()
    kr_trade_status = kr_payload.get("tradeStopType") if isinstance(kr_payload, dict) else {}
    kr_open = kr_status.upper() == "OPEN" and str(kr_trade_status.get("name") or "").upper() == "TRADING"
    kr_reference_time = parse_datetime_value(kr_payload.get("localTradedAt") if isinstance(kr_payload, dict) else None)

    fx_result = fx_payload.get("result") if isinstance(fx_payload, dict) else {}
    usd_krw = stock_quote_number(fx_result.get("closePrice") if isinstance(fx_result, dict) else None)
    fx_reference_time = parse_datetime_value(fx_result.get("localTradedAt") if isinstance(fx_result, dict) else None)

    if us_price is None or us_price <= 0:
        raise ValueError("Nasdaq 未返回 SKHY 股票价格")
    if kr_price is None or kr_price <= 0:
        raise ValueError("韩国行情源未返回 KRX 000660 股票价格")
    if usd_krw is None or usd_krw <= 0:
        raise ValueError("未返回美元兑韩元汇率")

    try:
        history = sk_hynix_stock_history(now_utc)
        history_error = None
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        history = {"items": [], "sampleCount": 0, "historyStart": None, "historyEnd": None}
        history_error = str(exc)

    adr_ratio = 0.1
    kr_equivalent_usd = kr_price * adr_ratio / usd_krw
    spread_pct = pair_spread_symmetric_pct(us_price, kr_equivalent_usd)
    standard_premium_pct = (us_price / kr_equivalent_usd - 1) * 100 if kr_equivalent_usd > 0 else None
    spread_abs = us_price - kr_equivalent_usd
    active_market = "both" if us_open and kr_open else "us" if us_open else "kr" if kr_open else "closed"
    latest = {
        "time": now_utc,
        "leftPrice": us_price,
        "leftBid": us_bid if us_open else us_price,
        "leftAsk": us_ask if us_open else us_price,
        "rightPriceRaw": kr_price,
        "rightPrice": kr_equivalent_usd,
        "rightBidRaw": kr_price,
        "rightAskRaw": kr_price,
        "rightBid": kr_equivalent_usd,
        "rightAsk": kr_equivalent_usd,
        "spreadAbs": spread_abs,
        "spreadPct": spread_pct,
        "openSpreadPct": spread_pct,
        "closeSpreadPct": spread_pct,
        "rawRatio": us_price / kr_equivalent_usd if kr_equivalent_usd > 0 else None,
        "source": "stock_market",
    }
    payload = {
        "status": "ok",
        "source": "stock_market_quotes",
        "updatedAt": now_utc,
        "activeMarket": active_market,
        "ratio": 10.0,
        "adrRatio": adr_ratio,
        "fxRate": usd_krw,
        "fxReferenceTime": fx_reference_time,
        "standardPremiumPct": standard_premium_pct,
        "latest": latest,
        "items": history["items"],
        "historySampleCount": history["sampleCount"],
        "historyStart": history["historyStart"],
        "historyEnd": history["historyEnd"],
        "historyError": history_error,
        "usSession": {
            "market": "us",
            "label": "美国股票",
            "symbol": "SKHY",
            "exchange": "Nasdaq",
            "currency": "USD",
            "rawPrice": us_price,
            "convertedPriceUsd": us_price,
            "isOpen": us_open,
            "priceMode": us_price_mode,
            "referenceTime": us_reference_time,
            "marketStatus": us_status,
            "sourceName": "Nasdaq",
            "sourceUrl": "https://www.nasdaq.com/market-activity/stocks/skhy",
        },
        "krSession": {
            "market": "kr",
            "label": "韩国股票",
            "symbol": "000660",
            "exchange": "KRX",
            "currency": "KRW",
            "rawPrice": kr_price,
            "convertedPriceUsd": kr_equivalent_usd,
            "isOpen": kr_open,
            "priceMode": "live" if kr_open else "last_close",
            "referenceTime": kr_reference_time,
            "marketStatus": kr_status,
            "sourceName": "Naver Finance / KRX",
            "sourceUrl": "https://m.stock.naver.com/domestic/stock/000660",
        },
        "message": "Nasdaq SKHY ADR 对 KRX 000660 的真实股票价差；1 ADR = 0.1 股韩国普通股，并按美元兑韩元换算。",
        "calendarNote": "美国股票来自 Nasdaq，并优先采用盘前、盘后或夜盘最新成交；韩国股票与美元兑韩元汇率来自 Naver Finance。",
        "historyNote": "历史曲线使用同一交易日的两地收盘价与当日美元兑韩元收盘汇率。韩国收盘早于美国，日线点不是同时可成交价差。",
    }
    with _sk_hynix_stock_quote_cache_lock:
        _sk_hynix_stock_quote_cache = (now_utc, payload)
    return dict(payload)


def crypto_pair_spread_overview(
    left_symbol: str = "SKHY",
    right_symbol: str = "SKHYNIX",
    right_ratio: float = 10.0,
    granularity: str = "auto",
    limit: int = 120,
    range_hours: float | None = 168.0,
    left_exchange: str = "bg",
    right_exchange: str = "bg",
    db: Session | None = None,
) -> dict[str, Any]:
    normalized_left_exchange = pair_spread_exchange(left_exchange)
    normalized_right_exchange = pair_spread_exchange(right_exchange)
    left = normalize_hyperliquid_pair_coin(left_symbol) if normalized_left_exchange == "hl" else normalize_symbol(left_symbol)
    right = normalize_hyperliquid_pair_coin(right_symbol) if normalized_right_exchange == "hl" else normalize_symbol(right_symbol)
    ratio = parse_float(right_ratio) or 10.0
    if ratio <= 0:
        raise ValueError("right_ratio 必须大于 0")
    allowed_granularities = {"auto", "1m", "3m", "5m", "15m", "30m", "1H", "4H"}
    normalized_granularity = granularity if granularity in allowed_granularities else "auto"
    if normalized_granularity == "auto":
        normalized_granularity = pair_spread_auto_granularity(range_hours)
    hours = first_float(range_hours)
    normalized_limit = pair_spread_point_limit(normalized_granularity, hours, int(limit))
    end_ms = int(time.time() * 1000)
    start_ms = int(end_ms - max(1.0, min(hours or 168.0, 24 * 120)) * 3600 * 1000)
    left_request_symbol = left
    right_request_symbol = right
    left_price_ratio = 1.0
    right_price_ratio = 1.0
    if db is not None and ":" not in left:
        left_request_symbol, left_price_ratio = mapped_symbol_and_ratio_for(db, left, normalized_left_exchange, "futures")
    if db is not None and ":" not in right:
        right_request_symbol, right_price_ratio = mapped_symbol_and_ratio_for(db, right, normalized_right_exchange, "futures")
    with http_client(timeout=6.0) as client:
        if normalized_left_exchange == "hl":
            left_request_symbol = resolve_hyperliquid_pair_coin(client, left_request_symbol)
        if normalized_right_exchange == "hl":
            right_request_symbol = resolve_hyperliquid_pair_coin(client, right_request_symbol)
        left_candles = contract_candle_rows(
            client,
            normalized_left_exchange,
            left_request_symbol,
            normalized_granularity,
            normalized_limit,
            start_ms,
            end_ms,
            left_price_ratio,
        )
        right_candles = contract_candle_rows(
            client,
            normalized_right_exchange,
            right_request_symbol,
            normalized_granularity,
            normalized_limit,
            start_ms,
            end_ms,
            right_price_ratio,
        )
        left_ticker = contract_ticker_price(client, normalized_left_exchange, left_request_symbol, left_price_ratio)
        right_ticker = contract_ticker_price(client, normalized_right_exchange, right_request_symbol, right_price_ratio)
    left_by_ts = pair_spread_rows_by_bucket(left_candles, normalized_granularity)
    right_by_ts = pair_spread_rows_by_bucket(right_candles, normalized_granularity)
    items: list[dict[str, Any]] = []
    for ts in sorted(set(left_by_ts) & set(right_by_ts)):
        item = pair_spread_item(ts, left_by_ts[ts].get("close"), right_by_ts[ts].get("close"), ratio, "candle")
        if item is None:
            continue
        if left_by_ts[ts].get("backfilledFrom") == "1m" or right_by_ts[ts].get("backfilledFrom") == "1m":
            item["dataQuality"] = "backfilled_1m"
        else:
            item["dataQuality"] = "direct"
        items.append(item)
    candle_items = list(items)
    candle_timestamps = [
        int(parsed.timestamp() * 1000)
        for row in candle_items
        if (parsed := parse_datetime_value(row.get("time"))) is not None
    ]
    bucket_ms = pair_spread_bucket_ms(normalized_granularity)
    expected_candle_count = (
        int((candle_timestamps[-1] - candle_timestamps[0]) / bucket_ms) + 1
        if bucket_ms and len(candle_timestamps) >= 2
        else len(candle_timestamps)
    )
    missing_candle_count = max(0, expected_candle_count - len(candle_items))
    backfilled_candle_count = sum(row.get("dataQuality") == "backfilled_1m" for row in candle_items)
    coverage_hours = (
        (candle_timestamps[-1] - candle_timestamps[0]) / 3_600_000
        if len(candle_timestamps) >= 2
        else 0.0
    )
    left_latest = first_float(left_ticker.get("close"))
    right_latest_raw = first_float(right_ticker.get("close"))
    latest_ts = int(max(first_float(left_ticker.get("ts")) or 0, first_float(right_ticker.get("ts")) or 0) or time.time() * 1000)
    latest = pair_spread_item(
        latest_ts,
        left_latest,
        right_latest_raw,
        ratio,
        "ticker",
        left_bid=first_float(left_ticker.get("bid")),
        left_ask=first_float(left_ticker.get("ask")),
        right_bid_raw=first_float(right_ticker.get("bid")),
        right_ask_raw=first_float(right_ticker.get("ask")),
    )
    if latest is not None and (not items or latest_ts > int(parse_datetime_value(items[-1].get("time")).timestamp() * 1000 if parse_datetime_value(items[-1].get("time")) else 0)):
        items.append(latest)
    if not items or latest is None:
        raise ValueError("未返回可对齐的价差数据，请确认两边交易所都已上线该合约。")
    left_exchange_name = EXCHANGE_NAMES.get(normalized_left_exchange, normalized_left_exchange)
    right_exchange_name = EXCHANGE_NAMES.get(normalized_right_exchange, normalized_right_exchange)
    return {
        "status": "ok",
        "source": "multi_exchange_contract_market",
        "exchange": normalized_left_exchange if normalized_left_exchange == normalized_right_exchange else "multi",
        "leftExchange": normalized_left_exchange,
        "rightExchange": normalized_right_exchange,
        "leftExchangeName": left_exchange_name,
        "rightExchangeName": right_exchange_name,
        "leftSymbol": left,
        "rightSymbol": right,
        "leftMarketSymbol": left_request_symbol,
        "rightMarketSymbol": right_request_symbol,
        "leftPriceRatio": left_price_ratio,
        "rightPriceRatio": right_price_ratio,
        "rightRatio": ratio,
        "granularity": normalized_granularity,
        "rangeHours": max(1.0, min(hours or 168.0, 24 * 120)),
        "updatedAt": datetime.now(timezone.utc),
        "latest": latest,
        "items": items[-normalized_limit:],
        "dataQuality": {
            "coverageStart": candle_items[0].get("time") if candle_items else None,
            "coverageEnd": candle_items[-1].get("time") if candle_items else None,
            "coverageHours": coverage_hours,
            "commonCandleCount": len(candle_items),
            "expectedCandleCount": expected_candle_count,
            "missingCandleCount": missing_candle_count,
            "backfilledCandleCount": backfilled_candle_count,
            "completenessPct": (len(candle_items) / expected_candle_count * 100) if expected_candle_count else None,
            "listingLimited": bool(hours and coverage_hours + (bucket_ms or 0) / 3_600_000 < hours - 1),
        },
        "message": f"Astro 对称价差：{left_exchange_name} {left} 对 {right_exchange_name} {right}/{'{:.8g}'.format(ratio)}",
    }


def crypto_pair_spread_latest(
    left_symbol: str = "SKHY",
    right_symbol: str = "SKHYNIX",
    right_ratio: float = 10.0,
    left_exchange: str = "bg",
    right_exchange: str = "bg",
    db: Session | None = None,
) -> dict[str, Any]:
    normalized_left_exchange = pair_spread_exchange(left_exchange)
    normalized_right_exchange = pair_spread_exchange(right_exchange)
    left = normalize_hyperliquid_pair_coin(left_symbol) if normalized_left_exchange == "hl" else normalize_symbol(left_symbol)
    right = normalize_hyperliquid_pair_coin(right_symbol) if normalized_right_exchange == "hl" else normalize_symbol(right_symbol)
    ratio = parse_float(right_ratio) or 10.0
    if ratio <= 0:
        raise ValueError("right_ratio 必须大于 0")

    left_request_symbol = left
    right_request_symbol = right
    left_price_ratio = 1.0
    right_price_ratio = 1.0
    if db is not None and ":" not in left:
        left_request_symbol, left_price_ratio = mapped_symbol_and_ratio_for(db, left, normalized_left_exchange, "futures")
    if db is not None and ":" not in right:
        right_request_symbol, right_price_ratio = mapped_symbol_and_ratio_for(db, right, normalized_right_exchange, "futures")

    cache_key = (normalized_left_exchange, left, normalized_right_exchange, right, ratio)
    try:
        with http_client(timeout=6.0) as client:
            if normalized_left_exchange == "hl":
                left_request_symbol = resolve_hyperliquid_pair_coin(client, left_request_symbol)
            if normalized_right_exchange == "hl":
                right_request_symbol = resolve_hyperliquid_pair_coin(client, right_request_symbol)
            left_ticker = contract_ticker_price(client, normalized_left_exchange, left_request_symbol, left_price_ratio)
            right_ticker = contract_ticker_price(client, normalized_right_exchange, right_request_symbol, right_price_ratio)
    except httpx.HTTPError:
        now = datetime.now(timezone.utc)
        with _pair_spread_latest_cache_lock:
            cached = _pair_spread_latest_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() <= PAIR_SPREAD_LATEST_CACHE_SECONDS:
            payload = dict(cached[1])
            payload["latest"] = dict(payload["latest"])
            payload["status"] = "partial_error"
            payload["source"] = "cached_contract_ticker"
            payload["staleAgeSeconds"] = round((now - cached[0]).total_seconds(), 1)
            payload["message"] = "实时行情暂时超时，显示最近一次成功报价；系统会自动重试。"
            return payload
        raise
    left_latest = first_float(left_ticker.get("close"))
    right_latest_raw = first_float(right_ticker.get("close"))
    latest_ts = int(max(first_float(left_ticker.get("ts")) or 0, first_float(right_ticker.get("ts")) or 0) or time.time() * 1000)
    latest = pair_spread_item(
        latest_ts,
        left_latest,
        right_latest_raw,
        ratio,
        "ticker",
        left_bid=first_float(left_ticker.get("bid")),
        left_ask=first_float(left_ticker.get("ask")),
        right_bid_raw=first_float(right_ticker.get("bid")),
        right_ask_raw=first_float(right_ticker.get("ask")),
    )
    if latest is None:
        raise ValueError("未返回实时价差数据，请确认两边交易所都已上线该合约。")

    left_exchange_name = EXCHANGE_NAMES.get(normalized_left_exchange, normalized_left_exchange)
    right_exchange_name = EXCHANGE_NAMES.get(normalized_right_exchange, normalized_right_exchange)
    result = {
        "status": "ok",
        "source": "multi_exchange_contract_ticker",
        "exchange": normalized_left_exchange if normalized_left_exchange == normalized_right_exchange else "multi",
        "leftExchange": normalized_left_exchange,
        "rightExchange": normalized_right_exchange,
        "leftExchangeName": left_exchange_name,
        "rightExchangeName": right_exchange_name,
        "leftSymbol": left,
        "rightSymbol": right,
        "leftMarketSymbol": left_request_symbol,
        "rightMarketSymbol": right_request_symbol,
        "leftPriceRatio": left_price_ratio,
        "rightPriceRatio": right_price_ratio,
        "rightRatio": ratio,
        "updatedAt": datetime.now(timezone.utc),
        "latest": latest,
        "message": f"Astro 对称价差：{left_exchange_name} {left} 对 {right_exchange_name} {right}/{'{:.8g}'.format(ratio)}",
    }
    cached_at = datetime.now(timezone.utc)
    with _pair_spread_latest_cache_lock:
        _pair_spread_latest_cache[cache_key] = (cached_at, result)
    return result


def fetch_htx(client: httpx.Client, symbol: str) -> MarketQuote:
    contract = f"{symbol}-USDT"
    depth = request_json(
        client,
        f"{base_url('htx')}/linear-swap-ex/market/depth",
        {"contract_code": contract, "type": "step0"},
    )
    funding = request_json(
        client,
        f"{base_url('htx')}/linear-swap-api/v1/swap_funding_rate",
        {"contract_code": contract},
    )
    contract_info = htx_contract_info(client, contract)
    tick = depth.get("tick") if isinstance(depth, dict) else {}
    funding_data = funding.get("data") if isinstance(funding, dict) else {}
    bid = parse_float(((tick or {}).get("bids") or [[None]])[0][0])
    ask = parse_float(((tick or {}).get("asks") or [[None]])[0][0])
    mark_price = parse_float(funding_data.get("mark_price"))
    index_price = parse_float(funding_data.get("index_price"))
    max_rate = first_float(
        funding_data.get("funding_rate_cap"),
        funding_data.get("max_funding_rate"),
        contract_info.get("funding_rate_cap"),
        contract_info.get("max_funding_rate"),
    )
    min_rate = first_float(
        funding_data.get("funding_rate_floor"),
        funding_data.get("min_funding_rate"),
        contract_info.get("funding_rate_floor"),
        contract_info.get("min_funding_rate"),
        negative_cap(max_rate),
    )
    period_hours = first_float(
        funding_data.get("funding_interval"),
        contract_info.get("funding_interval"),
        contract_info.get("settlement_period"),
        8,
    )
    return MarketQuote(
        exchange="htx",
        symbol=symbol,
        best_bid=bid,
        best_ask=ask,
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(funding_data.get("funding_rate")),
        premium_rate=computed_premium(mark_price, index_price),
        next_funding_time=parse_timestamp_ms(funding_data.get("funding_time")),
        period_hours=period_hours,
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        funding_rule=f"{period_hours:g}h {funding_range_text(min_rate, max_rate)}" if period_hours else funding_range_text(min_rate, max_rate),
        funding_formula="HTX linear swap: funding endpoint plus contract info when cap/floor are disclosed",
        premium_source="mark/index proxy",
        updated_at=datetime.now(timezone.utc),
    )


def fetch_aster(client: httpx.Client, symbol: str) -> MarketQuote:
    market_symbol = f"{symbol}USDT"
    ticker = request_json(
        client,
        f"{base_url('as')}/fapi/v1/ticker/bookTicker",
        {"symbol": market_symbol},
    )
    stats: dict[str, Any] = {}
    try:
        stats = request_json(client, f"{base_url('as')}/fapi/v1/ticker/24hr", {"symbol": market_symbol})
    except Exception:
        stats = {}
    premium = request_json(
        client,
        f"{base_url('as')}/fapi/v1/premiumIndex",
        {"symbol": market_symbol},
    )
    funding_info = binance_funding_info(client, "as", market_symbol)
    mark_price = parse_float(premium.get("markPrice"))
    index_price = parse_float(premium.get("indexPrice"))
    period_hours = first_float(funding_info.get("fundingIntervalHours"), premium.get("fundingIntervalHours"), 1)
    max_rate = first_float(funding_info.get("adjustedFundingRateCap"), funding_info.get("fundingRateCap"), 0.02)
    min_rate = first_float(funding_info.get("adjustedFundingRateFloor"), funding_info.get("fundingRateFloor"), -0.02)
    interest_rate = parse_float(premium.get("interestRate"))
    return MarketQuote(
        exchange="as",
        symbol=symbol,
        best_bid=parse_float(ticker.get("bidPrice")),
        best_ask=parse_float(ticker.get("askPrice")),
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(premium.get("lastFundingRate")),
        premium_rate=computed_premium(mark_price, index_price),
        next_funding_time=parse_timestamp_ms(premium.get("nextFundingTime")),
        period_hours=period_hours,
        **_funding_period_evidence(
            period_hours,
            ("fundingInfo.fundingIntervalHours", funding_info.get("fundingIntervalHours")),
            ("premiumIndex.fundingIntervalHours", premium.get("fundingIntervalHours")),
        ),
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        interest_rate=interest_rate,
        funding_rule=f"{period_hours:g}h {funding_range_text(min_rate, max_rate)}",
        funding_formula="Aster USDT perpetual: Binance-compatible premium/fundingInfo fields, Aster default used when not disclosed",
        premium_source="mark/index proxy",
        volume_24h=parse_float(stats.get("quoteVolume")),
        updated_at=datetime.now(timezone.utc),
    )


def fetch_hyperliquid(client: httpx.Client, symbol: str) -> MarketQuote:
    meta_payload = request_post_json(client, f"{base_url('hl')}/info", {"type": "metaAndAssetCtxs"})
    book_payload = request_post_json(client, f"{base_url('hl')}/info", {"type": "l2Book", "coin": symbol})
    meta = meta_payload[0] if isinstance(meta_payload, list) and meta_payload else {}
    contexts = meta_payload[1] if isinstance(meta_payload, list) and len(meta_payload) > 1 else []
    universe = meta.get("universe") or []
    index = next((i for i, item in enumerate(universe) if item.get("name") == symbol), None)
    context = contexts[index] if index is not None and isinstance(contexts, list) and index < len(contexts) else {}
    levels = book_payload.get("levels") if isinstance(book_payload, dict) else []
    bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
    asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
    best_bid = parse_float((bids[0] or {}).get("px")) if bids else None
    best_ask = parse_float((asks[0] or {}).get("px")) if asks else None
    mark_price = parse_float(context.get("markPx"))
    index_price = parse_float(context.get("oraclePx"))
    premium_rate = first_float(context.get("premium"), computed_premium(mark_price, index_price))
    interest_rate = 0.0000125
    max_rate = 0.04
    min_rate = -0.04
    return MarketQuote(
        exchange="hl",
        symbol=symbol,
        best_bid=best_bid,
        best_ask=best_ask,
        mark_price=mark_price,
        index_price=index_price,
        funding_rate=parse_float(context.get("funding")),
        premium_rate=premium_rate,
        period_hours=1,
        max_funding_rate=max_rate,
        min_funding_rate=min_rate,
        interest_rate=interest_rate,
        funding_rule=f"1h {funding_range_text(min_rate, max_rate)}",
        funding_formula="Hyperliquid: hourly funding from metaAndAssetCtxs; premium field preferred, oracle/mark proxy fallback",
        premium_source="context premium when present, otherwise mark/oracle proxy",
        open_interest=parse_float(context.get("openInterest")),
        volume_24h=parse_float(context.get("dayNtlVlm")),
        updated_at=datetime.now(timezone.utc),
    )


def funding_history_limit(value: int) -> int:
    return min(max(value, 1), 100)


def fetch_funding_history(exchange: str, symbol: str, limit: int = 20) -> list[FundingHistoryItem]:
    exchange = normalize_exchange(exchange)
    symbol = normalize_symbol(symbol)
    limit = funding_history_limit(limit)
    with http_client() as client:
        if exchange == "bn":
            items = fetch_binance_funding_history(client, symbol, limit)
        elif exchange == "by":
            items = fetch_bybit_funding_history(client, symbol, limit)
        elif exchange == "gt":
            items = fetch_gate_funding_history(client, symbol, limit)
        elif exchange == "okx":
            items = fetch_okx_funding_history(client, symbol, limit)
        elif exchange == "bg":
            items = fetch_bitget_funding_history(client, symbol, limit)
        elif exchange == "htx":
            items = fetch_htx_funding_history(client, symbol, limit)
        elif exchange == "hl":
            items = fetch_hyperliquid_funding_history(client, symbol, limit)
        else:
            items = fetch_aster_funding_history(client, symbol, limit)
    return sorted(items, key=lambda item: item.funding_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:limit]


def fetch_binance_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    rows = request_json(
        client,
        f"{base_url('bn')}/fapi/v1/fundingRate",
        {"symbol": f"{symbol}USDT", "limit": limit},
    )
    if not isinstance(rows, list):
        raise ValueError(f"Binance 返回异常: {rows}")
    return [
        FundingHistoryItem(
            exchange="bn",
            symbol=symbol,
            funding_rate=parse_float(row.get("fundingRate")),
            mark_price=parse_float(row.get("markPrice")),
            funding_time=parse_timestamp_ms(row.get("fundingTime")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_bybit_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    payload = request_json(
        client,
        f"{base_url('by')}/v5/market/funding/history",
        {"category": "linear", "symbol": f"{symbol}USDT", "limit": limit},
    )
    rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"Bybit 返回异常: {payload}")
    return [
        FundingHistoryItem(
            exchange="by",
            symbol=symbol,
            funding_rate=parse_float(row.get("fundingRate")),
            funding_time=parse_datetime_value(row.get("fundingRateTimestamp")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_gate_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    rows = request_json(
        client,
        f"{base_url('gt')}/api/v4/futures/usdt/funding_rate",
        {"contract": f"{symbol}_USDT", "limit": limit},
    )
    if not isinstance(rows, list):
        raise ValueError(f"Gate 返回异常: {rows}")
    return [
        FundingHistoryItem(
            exchange="gt",
            symbol=symbol,
            funding_rate=parse_float(row.get("r")),
            funding_time=parse_timestamp_s(row.get("t")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_okx_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    rows = okx_rows(
        request_json(
            client,
            f"{base_url('okx')}/api/v5/public/funding-rate-history",
            {"instId": f"{symbol}-USDT-SWAP", "limit": limit},
        )
    )
    return [
        FundingHistoryItem(
            exchange="okx",
            symbol=symbol,
            funding_rate=parse_float(row.get("realizedRate")) or parse_float(row.get("fundingRate")),
            funding_time=parse_timestamp_ms(row.get("fundingTime")),
        )
        for row in rows
    ]


def fetch_bitget_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    payload = bitget_data(
        request_json(
            client,
            f"{base_url('bg')}/api/v2/mix/market/history-fund-rate",
            {"symbol": f"{symbol}USDT", "productType": "USDT-FUTURES", "pageSize": limit},
        )
    )
    rows = payload if isinstance(payload, list) else payload.get("list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"Bitget 返回异常: {payload}")
    return [
        FundingHistoryItem(
            exchange="bg",
            symbol=symbol,
            funding_rate=parse_float(row.get("fundingRate")),
            funding_time=parse_datetime_value(row.get("fundingTime")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_htx_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    payload = request_json(
        client,
        f"{base_url('htx')}/linear-swap-api/v1/swap_historical_funding_rate",
        {"contract_code": f"{symbol}-USDT", "page_index": 1, "page_size": limit},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"HTX 返回异常: {payload}")
    return [
        FundingHistoryItem(
            exchange="htx",
            symbol=symbol,
            funding_rate=parse_float(row.get("funding_rate") or row.get("realized_rate")),
            funding_time=parse_datetime_value(row.get("funding_time")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_aster_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    rows = request_json(
        client,
        f"{base_url('as')}/fapi/v1/fundingRate",
        {"symbol": f"{symbol}USDT", "limit": limit},
    )
    if not isinstance(rows, list):
        raise ValueError(f"Aster 返回异常: {rows}")
    return [
        FundingHistoryItem(
            exchange="as",
            symbol=symbol,
            funding_rate=parse_float(row.get("fundingRate")),
            mark_price=parse_float(row.get("markPrice")),
            funding_time=parse_timestamp_ms(row.get("fundingTime")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_hyperliquid_funding_history(client: httpx.Client, symbol: str, limit: int) -> list[FundingHistoryItem]:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=max(limit + 4, 24))
    rows = request_post_json(
        client,
        f"{base_url('hl')}/info",
        {
            "type": "fundingHistory",
            "coin": symbol,
            "startTime": int(start_time.timestamp() * 1000),
            "endTime": int(end_time.timestamp() * 1000),
        },
    )
    if not isinstance(rows, list):
        raise ValueError(f"Hyperliquid 返回异常: {rows}")
    return [
        FundingHistoryItem(
            exchange="hl",
            symbol=symbol,
            funding_rate=parse_float(row.get("fundingRate")),
            premium_rate=parse_float(row.get("premium")),
            funding_time=parse_timestamp_ms(row.get("time")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def funding_history_item_to_out(item: FundingHistoryItem) -> dict[str, Any]:
    return {
        "exchange": item.exchange,
        "symbol": item.symbol,
        "fundingRate": item.funding_rate,
        "premiumRate": item.premium_rate,
        "markPrice": item.mark_price,
        "fundingTime": utc_datetime(item.funding_time),
    }


def crypto_funding_history_overview(exchange: str, symbol: str, limit: int = 20) -> dict[str, Any]:
    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol)
    try:
        items = fetch_funding_history(normalized_exchange, normalized_symbol, limit)
    except Exception as exc:
        return {
            "status": "error",
            "source_status": "error",
            "exchange": normalized_exchange,
            "exchangeName": EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange),
            "symbol": normalized_symbol,
            "items": [],
            "updated_at": datetime.now(timezone.utc),
            "message": str(exc),
        }
    status = "ok" if items else "not_found"
    return {
        "status": status,
        "source_status": status,
        "exchange": normalized_exchange,
        "exchangeName": EXCHANGE_NAMES.get(normalized_exchange, normalized_exchange),
        "symbol": normalized_symbol,
        "items": [funding_history_item_to_out(item) for item in items],
        "updated_at": datetime.now(timezone.utc),
        "message": "历史费率已更新。" if items else "没有找到历史费率。",
    }


def snapshot_history_to_out(snapshot: CryptoSpreadSnapshot) -> dict[str, Any]:
    view = snapshot_view(snapshot)
    return {
        "time": utc_datetime(snapshot.created_at),
        "spread": view["spread"],
        "spreadPct": view["spreadPct"],
        "bidSpreadPct": view["bidSpreadPct"],
        "askSpreadPct": view["askSpreadPct"],
        "leftBid": view["leftBid"],
        "leftAsk": view["leftAsk"],
        "rightBid": view["rightBid"],
        "rightAsk": view["rightAsk"],
        "leftFundingRate": view["leftFundingRate"],
        "rightFundingRate": view["rightFundingRate"],
        "leftPremiumRate": view["leftPremiumRate"],
        "rightPremiumRate": view["rightPremiumRate"],
        "leftMarkPrice": view["leftMarkPrice"],
        "rightMarkPrice": view["rightMarkPrice"],
        "leftIndexPrice": view["leftIndexPrice"],
        "rightIndexPrice": view["rightIndexPrice"],
        "status": snapshot.status,
    }


def market_snapshot_source_priority(source: str | None) -> int:
    priorities = {
        "astro_trigger": 4,
        "astro_candidate": 3,
        "astro_baseline": 2,
        "exchange_api": 1,
    }
    return priorities.get(source or "", 0)


def market_snapshot_key(snapshot: CryptoMarketQuoteSnapshot) -> tuple[datetime, str, str, str]:
    batch_time = utc_datetime(snapshot.batch_time) or utc_datetime(snapshot.created_at) or datetime.now(timezone.utc)
    return batch_time, snapshot.source, snapshot.exchange, snapshot.market_type


def market_snapshot_to_history_point(
    batch_time: datetime,
    left: CryptoMarketQuoteSnapshot,
    right: CryptoMarketQuoteSnapshot,
) -> dict[str, Any]:
    spread = spread_from_prices(left.best_ask, right.best_bid)
    spread_pct = pct_from_prices(left.best_ask, right.best_bid)
    ask_spread_pct = pct_from_prices(left.best_bid, right.best_ask)
    status = "ok" if left.status == "ok" and right.status == "ok" else "partial_error"
    return {
        "time": utc_datetime(batch_time),
        "spread": spread,
        "spreadPct": spread_pct,
        "bidSpreadPct": spread_pct,
        "askSpreadPct": ask_spread_pct,
        "leftBid": left.best_bid,
        "leftAsk": left.best_ask,
        "rightBid": right.best_bid,
        "rightAsk": right.best_ask,
        "leftFundingRate": left.funding_rate,
        "rightFundingRate": right.funding_rate,
        "leftPremiumRate": left.premium_rate,
        "rightPremiumRate": right.premium_rate,
        "leftMarkPrice": left.mark_price,
        "rightMarkPrice": right.mark_price,
        "leftIndexPrice": left.index_price,
        "rightIndexPrice": right.index_price,
        "status": status,
    }


def market_snapshot_history_item(
    symbol: str,
    left_exchange: str,
    right_exchange: str,
    left_market_type: str,
    right_market_type: str,
    latest: dict[str, Any] | None,
    updated_at: datetime | None,
) -> dict[str, Any]:
    return {
        "id": 0,
        "symbol": symbol,
        "leftExchange": left_exchange,
        "rightExchange": right_exchange,
        "leftMarketType": left_market_type,
        "rightMarketType": right_market_type,
        "label": label_for(symbol, left_exchange, right_exchange, left_market_type, right_market_type),
        "enabled": False,
        "note": "由全市场沉淀快照自动生成，未设置为监控。",
        "leftBid": latest["leftBid"] if latest else None,
        "leftAsk": latest["leftAsk"] if latest else None,
        "rightBid": latest["rightBid"] if latest else None,
        "rightAsk": latest["rightAsk"] if latest else None,
        "spread": latest["spread"] if latest else None,
        "spreadPct": latest["spreadPct"] if latest else None,
        "bidSpreadPct": latest["bidSpreadPct"] if latest else None,
        "askSpreadPct": latest["askSpreadPct"] if latest else None,
        "leftFundingRate": latest["leftFundingRate"] if latest else None,
        "rightFundingRate": latest["rightFundingRate"] if latest else None,
        "leftPremiumRate": latest["leftPremiumRate"] if latest else None,
        "rightPremiumRate": latest["rightPremiumRate"] if latest else None,
        "leftMarkPrice": latest["leftMarkPrice"] if latest else None,
        "rightMarkPrice": latest["rightMarkPrice"] if latest else None,
        "leftIndexPrice": latest["leftIndexPrice"] if latest else None,
        "rightIndexPrice": latest["rightIndexPrice"] if latest else None,
        "status": latest["status"] if latest else "not_found",
        "lastError": None,
        "updatedAt": utc_datetime(updated_at),
        "createdAt": utc_datetime(updated_at) or datetime.now(timezone.utc),
    }


def safe_funding_history(exchange: str, symbol: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    key = (exchange, symbol, limit)
    now = datetime.now(timezone.utc)
    cached = _funding_history_cache.get(key)
    if cached and (now - cached[0]).total_seconds() < FUNDING_HISTORY_CACHE_SECONDS:
        return cached[1], cached[2]
    try:
        items = [funding_history_item_to_out(item) for item in fetch_funding_history(exchange, symbol, limit)]
        _funding_history_cache[key] = (now, items, None)
        return items, None
    except Exception as exc:
        error = f"{exchange}: {exc}"
        _funding_history_cache[key] = (now, [], error)
        return [], error


def snapshot_minute_key(snapshot: CryptoSpreadSnapshot) -> datetime:
    created_at = snapshot.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc).replace(second=0, microsecond=0)


def minute_snapshots(snapshots: list[CryptoSpreadSnapshot], limit: int) -> list[CryptoSpreadSnapshot]:
    minute_rows: list[CryptoSpreadSnapshot] = []
    last_minute: datetime | None = None
    for snapshot in snapshots:
        minute = snapshot_minute_key(snapshot)
        if last_minute == minute and minute_rows:
            minute_rows[-1] = snapshot
        else:
            minute_rows.append(snapshot)
            last_minute = minute
    if len(minute_rows) > limit:
        return minute_rows[-limit:]
    return minute_rows


def sampled_history_snapshots(snapshots: list[CryptoSpreadSnapshot], limit: int) -> list[CryptoSpreadSnapshot]:
    minute_rows = minute_snapshots(snapshots, limit)
    if len(minute_rows) < 2 and len(snapshots) > len(minute_rows):
        return snapshots[-limit:]
    return minute_rows


def load_history_snapshots(db: Session, item_id: int, since: datetime, raw_limit: int) -> list[CryptoSpreadSnapshot]:
    snapshots = list(
        db.scalars(
            select(CryptoSpreadSnapshot)
            .where(
                CryptoSpreadSnapshot.watch_item_id == item_id,
                CryptoSpreadSnapshot.created_at >= since,
            )
            .order_by(desc(CryptoSpreadSnapshot.created_at))
            .limit(raw_limit)
        )
    )
    return list(reversed(snapshots))


def crypto_watch_history(db: Session, item_id: int, hours: int = 24, limit: int = 2000) -> dict[str, Any]:
    item = db.get(CryptoWatchItem, item_id)
    if not item:
        raise ValueError("加密货币跟踪项不存在")
    canonicalize_watch_item_direction(item)
    bounded_hours = min(max(hours, 1), 24 * 30)
    bounded_limit = min(max(limit, 10), 5000)
    raw_limit = min(max(bounded_limit * 8, bounded_limit), 100_000)
    since = datetime.now(timezone.utc) - timedelta(hours=bounded_hours)
    raw_snapshots = load_history_snapshots(db, item.id, since, raw_limit)
    snapshots = sampled_history_snapshots(raw_snapshots, bounded_limit)
    if item.enabled and len(snapshots) < 2:
        refresh_crypto_item(db, item)
        db.commit()
        raw_snapshots = load_history_snapshots(db, item.id, since, raw_limit)
        snapshots = sampled_history_snapshots(raw_snapshots, bounded_limit)
    funding_limit = min(max(int(bounded_hours / 8) + 12, 24), 100)
    left_market_type = item.left_market_type or "futures"
    right_market_type = item.right_market_type or "futures"
    left_history_symbol = mapped_symbol_for(db, item.symbol, item.left_exchange, left_market_type)
    right_history_symbol = mapped_symbol_for(db, item.symbol, item.right_exchange, right_market_type)
    left_funding, left_error = (
        safe_funding_history(item.left_exchange, left_history_symbol, funding_limit)
        if left_market_type == "futures"
        else ([], None)
    )
    right_funding, right_error = (
        safe_funding_history(item.right_exchange, right_history_symbol, funding_limit)
        if right_market_type == "futures"
        else ([], None)
    )
    errors = [error for error in (left_error, right_error) if error]
    point_count = len(snapshots)
    status = "ok" if point_count else "not_found"
    if errors and point_count:
        status = "partial_error"
    elif errors:
        status = "error"
    return {
        "status": status,
        "source_status": status,
        "updated_at": utc_datetime(snapshots[-1].created_at) if snapshots else None,
        "message": "历史曲线已更新。" if point_count else "还没有连续快照。保持监控开启后会自动沉淀历史曲线。",
        "item": crypto_item_to_out(db, item),
        "hours": bounded_hours,
        "points": [snapshot_history_to_out(snapshot) for snapshot in snapshots],
        "leftFundingHistory": left_funding,
        "rightFundingHistory": right_funding,
        "fundingErrors": errors,
    }


def crypto_pair_history_from_market_snapshots(
    db: Session,
    symbol: str,
    left_exchange: str,
    right_exchange: str,
    left_market_type: str = "futures",
    right_market_type: str = "futures",
    hours: int = 24,
    limit: int = 2000,
) -> dict[str, Any]:
    normalized_symbol = normalize_symbol(symbol)
    left = normalize_exchange(left_exchange)
    right = normalize_exchange(right_exchange)
    left_type = normalize_market_type(left_market_type)
    right_type = normalize_market_type(right_market_type)
    if left == right and left_type == right_type:
        raise ValueError("左右交易腿不能完全相同")
    bounded_hours = min(max(hours, 1), 24 * 30)
    bounded_limit = min(max(limit, 10), 5000)
    raw_limit = min(max(bounded_limit * 12, bounded_limit), 120_000)
    since = datetime.now(timezone.utc) - timedelta(hours=bounded_hours)

    rows = list(
        db.scalars(
            select(CryptoMarketQuoteSnapshot)
            .where(
                CryptoMarketQuoteSnapshot.symbol == normalized_symbol,
                CryptoMarketQuoteSnapshot.batch_time >= since,
                CryptoMarketQuoteSnapshot.exchange.in_([left, right]),
                CryptoMarketQuoteSnapshot.market_type.in_([left_type, right_type]),
            )
            .order_by(desc(CryptoMarketQuoteSnapshot.batch_time), desc(CryptoMarketQuoteSnapshot.id))
            .limit(raw_limit)
        )
    )

    by_leg: dict[tuple[datetime, str, str, str], CryptoMarketQuoteSnapshot] = {}
    for row in rows:
        key = market_snapshot_key(row)
        existing = by_leg.get(key)
        if existing is None or market_snapshot_source_priority(row.source) > market_snapshot_source_priority(existing.source):
            by_leg[key] = row

    def build_points_from_sources(source_names: tuple[str, ...]) -> list[dict[str, Any]]:
        paired_by_time: dict[datetime, tuple[int, dict[str, Any]]] = {}
        batch_times = sorted({key[0] for key in by_leg if key[1] in source_names})
        for batch_time in batch_times:
            for source in source_names:
                left_row = by_leg.get((batch_time, source, left, left_type))
                right_row = by_leg.get((batch_time, source, right, right_type))
                if not left_row or not right_row:
                    continue
                priority = market_snapshot_source_priority(source)
                existing = paired_by_time.get(batch_time)
                if existing is None or priority > existing[0]:
                    paired_by_time[batch_time] = (priority, market_snapshot_to_history_point(batch_time, left_row, right_row))
        return [row for _, row in sorted(paired_by_time.values(), key=lambda item: item[1]["time"])]

    points = build_points_from_sources(("astro_trigger", "astro_candidate", "astro_baseline"))
    if not points:
        points = build_points_from_sources(("exchange_api",))

    if len(points) > bounded_limit:
        points = points[-bounded_limit:]
    updated_at = points[-1]["time"] if points else None
    latest = points[-1] if points else None
    funding_limit = min(max(int(bounded_hours / 8) + 12, 24), 100)
    left_funding, left_error = safe_funding_history(left, normalized_symbol, funding_limit) if left_type == "futures" else ([], None)
    right_funding, right_error = safe_funding_history(right, normalized_symbol, funding_limit) if right_type == "futures" else ([], None)
    errors = [error for error in (left_error, right_error) if error]
    status = "ok" if points else "not_found"
    if errors and points:
        status = "partial_error"
    elif errors:
        status = "error"
    return {
        "status": status,
        "source_status": status,
        "updated_at": utc_datetime(updated_at),
        "message": "已用全市场沉淀生成历史曲线。" if points else "全市场沉淀里还没有这组交易对的连续快照。",
        "item": market_snapshot_history_item(normalized_symbol, left, right, left_type, right_type, latest, updated_at),
        "hours": bounded_hours,
        "points": points,
        "leftFundingHistory": left_funding,
        "rightFundingHistory": right_funding,
        "fundingErrors": errors,
    }


def latest_snapshot(db: Session, item_id: int) -> CryptoSpreadSnapshot | None:
    return db.scalar(
        select(CryptoSpreadSnapshot)
        .where(CryptoSpreadSnapshot.watch_item_id == item_id)
        .order_by(desc(CryptoSpreadSnapshot.created_at))
        .limit(1)
    )


def canonicalize_watch_item_direction(item: CryptoWatchItem) -> None:
    left_market_type = item.left_market_type or "futures"
    right_market_type = item.right_market_type or "futures"
    if left_market_type == "futures" and right_market_type == "spot":
        item.left_exchange, item.right_exchange = item.right_exchange, item.left_exchange
        item.left_market_type, item.right_market_type = right_market_type, left_market_type


def snapshot_from_quotes(db: Session, item: CryptoWatchItem, left: MarketQuote, right: MarketQuote) -> CryptoSpreadSnapshot:
    left, right = canonicalize_spot_pair(left, right)
    spread = executable_spread(left, right)
    spread_pct = executable_spread_pct(left, right)
    bid_spread_pct = spread_pct
    ask_spread_pct = exit_spread_pct(left, right)

    errors = [f"{quote.exchange}: {quote.error}" for quote in (left, right) if quote.status != "ok" and quote.error]
    if left.status == "ok" and right.status == "ok":
        status = "ok"
    elif left.status == "ok" or right.status == "ok":
        status = "partial_error"
    else:
        status = "error"

    snapshot = CryptoSpreadSnapshot(
        watch_item_id=item.id,
        symbol=item.symbol,
        left_exchange=left.exchange,
        right_exchange=right.exchange,
        left_market_type=left.market_type,
        right_market_type=right.market_type,
        label=label_for(item.symbol, left.exchange, right.exchange, left.market_type, right.market_type),
        left_bid=left.best_bid,
        left_ask=left.best_ask,
        right_bid=right.best_bid,
        right_ask=right.best_ask,
        spread=spread,
        spread_pct=spread_pct,
        bid_spread_pct=bid_spread_pct,
        ask_spread_pct=ask_spread_pct,
        left_mark_price=left.mark_price,
        right_mark_price=right.mark_price,
        left_index_price=left.index_price,
        right_index_price=right.index_price,
        left_funding_rate=left.funding_rate,
        right_funding_rate=right.funding_rate,
        left_premium_rate=left.premium_rate,
        right_premium_rate=right.premium_rate,
        left_next_funding_time=left.next_funding_time,
        right_next_funding_time=right.next_funding_time,
        status=status,
        last_error="; ".join(errors) if errors else None,
    )
    db.add(snapshot)
    return snapshot


def unavailable_market_quote(exchange: str, symbol: str, market_type: str, error: str) -> MarketQuote:
    return MarketQuote(
        exchange=exchange,
        symbol=symbol,
        market_type=market_type,
        status="error",
        error=error,
    )


def refresh_crypto_item(db: Session, item: CryptoWatchItem) -> CryptoSpreadSnapshot:
    canonicalize_watch_item_direction(item)
    enabled_exchanges = set(enabled_crypto_exchange_codes(db))
    left_market_type = item.left_market_type or "futures"
    right_market_type = item.right_market_type or "futures"
    disabled = [
        exchange
        for exchange in (item.left_exchange, item.right_exchange)
        if exchange not in enabled_exchanges
    ]
    if disabled:
        disabled_names = " / ".join(dict.fromkeys(disabled))
        error = f"交易所未启用: {disabled_names}"
        left = unavailable_market_quote(item.left_exchange, item.symbol, left_market_type, error)
        right = unavailable_market_quote(item.right_exchange, item.symbol, right_market_type, error)
        return snapshot_from_quotes(db, item, left, right)

    left_symbol = mapped_symbol_for(db, item.symbol, item.left_exchange, left_market_type)
    right_symbol = mapped_symbol_for(db, item.symbol, item.right_exchange, right_market_type)
    left = fetch_market(item.left_exchange, left_symbol, left_market_type)
    right = fetch_market(item.right_exchange, right_symbol, right_market_type)
    return snapshot_from_quotes(db, item, left, right)


def refresh_crypto_watchlist(db: Session) -> dict[str, int | str]:
    items = list(db.scalars(select(CryptoWatchItem).where(CryptoWatchItem.enabled.is_(True)).order_by(CryptoWatchItem.id)))
    refreshed = 0
    failed = 0
    for item in items:
        snapshot = refresh_crypto_item(db, item)
        evaluate_crypto_push_rule(db, item, snapshot)
        refreshed += 1
        if snapshot.status != "ok":
            failed += 1
    db.commit()
    status = "ok" if failed == 0 else ("partial_error" if refreshed > failed else "error")
    return {
        "status": status if refreshed else "not_configured",
        "message": "加密货币价差已刷新。" if refreshed else "没有启用的加密货币跟踪项。",
        "refreshed": refreshed,
        "failed": failed,
    }


def crypto_item_to_out(db: Session, item: CryptoWatchItem) -> dict[str, Any]:
    snapshot = latest_snapshot(db, item.id)
    left_exchange = item.left_exchange
    right_exchange = item.right_exchange
    left_market_type = item.left_market_type or "futures"
    right_market_type = item.right_market_type or "futures"
    if left_market_type == "futures" and right_market_type == "spot":
        left_exchange, right_exchange = right_exchange, left_exchange
        left_market_type, right_market_type = right_market_type, left_market_type
    view = snapshot_view(snapshot) if snapshot else None
    if view:
        left_exchange = view["leftExchange"]
        right_exchange = view["rightExchange"]
        left_market_type = view["leftMarketType"]
        right_market_type = view["rightMarketType"]
    status = "manual_only" if not item.enabled else "not_configured"
    if snapshot:
        status = snapshot.status
    return {
        "id": item.id,
        "symbol": item.symbol,
        "leftExchange": left_exchange,
        "rightExchange": right_exchange,
        "leftMarketType": left_market_type,
        "rightMarketType": right_market_type,
        "label": label_for(
            item.symbol,
            left_exchange,
            right_exchange,
            left_market_type,
            right_market_type,
        ),
        "enabled": item.enabled,
        "note": item.note,
        "leftBid": view["leftBid"] if view else None,
        "leftAsk": view["leftAsk"] if view else None,
        "rightBid": view["rightBid"] if view else None,
        "rightAsk": view["rightAsk"] if view else None,
        "spread": view["spread"] if view else None,
        "spreadPct": view["spreadPct"] if view else None,
        "bidSpreadPct": view["bidSpreadPct"] if view else None,
        "askSpreadPct": view["askSpreadPct"] if view else None,
        "leftFundingRate": view["leftFundingRate"] if view else None,
        "rightFundingRate": view["rightFundingRate"] if view else None,
        "leftPremiumRate": view["leftPremiumRate"] if view else None,
        "rightPremiumRate": view["rightPremiumRate"] if view else None,
        "leftMarkPrice": view["leftMarkPrice"] if view else None,
        "rightMarkPrice": view["rightMarkPrice"] if view else None,
        "leftIndexPrice": view["leftIndexPrice"] if view else None,
        "rightIndexPrice": view["rightIndexPrice"] if view else None,
        "status": status,
        "lastError": snapshot.last_error if snapshot else None,
        "updatedAt": snapshot.created_at if snapshot else None,
        "createdAt": item.created_at,
    }


def order_book_level_to_out(level: OrderBookLevel) -> dict[str, Any]:
    return {
        "side": level.side,
        "level": level.level,
        "price": level.price,
        "size": level.size,
        "notional": level.notional,
    }


def order_book_to_out(quote: OrderBookQuote) -> dict[str, Any]:
    return {
        "exchange": quote.exchange,
        "exchangeName": EXCHANGE_NAMES.get(quote.exchange, quote.exchange),
        "symbol": quote.symbol,
        "marketType": quote.market_type,
        "amountUnit": quote.amount_unit,
        "turnover4hUsdt": quote.turnover_4h_usdt,
        "bids": [order_book_level_to_out(level) for level in quote.bids],
        "asks": [order_book_level_to_out(level) for level in quote.asks],
        "updatedAt": quote.updated_at,
        "status": quote.status,
        "lastError": quote.error,
    }


def crypto_push_rule(db: Session, item_id: int, create: bool = False) -> CryptoPushRule | None:
    rule = db.scalar(select(CryptoPushRule).where(CryptoPushRule.watch_item_id == item_id).limit(1))
    if rule or not create:
        return rule
    rule = CryptoPushRule(
        watch_item_id=item_id,
        enabled=False,
        open_spread_pct=None,
        close_spread_pct=None,
        premium_diff_pct=None,
        cooldown_minutes=30,
    )
    db.add(rule)
    db.flush()
    return rule


def push_rule_to_out(rule: CryptoPushRule | None, item_id: int) -> dict[str, Any]:
    return {
        "id": rule.id if rule else None,
        "watchItemId": item_id,
        "enabled": bool(rule.enabled) if rule else False,
        "openSpreadPct": rule.open_spread_pct if rule else None,
        "closeSpreadPct": rule.close_spread_pct if rule else None,
        "premiumDiffPct": rule.premium_diff_pct if rule else None,
        "cooldownMinutes": rule.cooldown_minutes if rule else 30,
        "lastTriggeredAt": rule.last_triggered_at if rule else None,
        "updatedAt": rule.updated_at if rule else None,
    }


def push_log_to_out(log: CryptoPushLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "watchItemId": log.watch_item_id,
        "eventType": log.event_type,
        "title": log.title,
        "body": log.body,
        "spreadPct": log.spread_pct,
        "premiumDiffPct": log.premium_diff_pct,
        "status": log.status,
        "message": log.message,
        "createdAt": log.created_at,
    }


def recent_crypto_push_logs(db: Session, item_id: int, limit: int = 20) -> list[dict[str, Any]]:
    logs = list(
        db.scalars(
            select(CryptoPushLog)
            .where(CryptoPushLog.watch_item_id == item_id)
            .order_by(desc(CryptoPushLog.created_at))
            .limit(limit)
        )
    )
    return [push_log_to_out(log) for log in logs]


def update_crypto_push_rule(db: Session, item_id: int, values: dict[str, Any]) -> dict[str, Any]:
    item = db.get(CryptoWatchItem, item_id)
    if not item:
        raise ValueError("加密货币跟踪项不存在")
    rule = crypto_push_rule(db, item.id, create=True)
    assert rule is not None
    if "enabled" in values:
        rule.enabled = bool(values["enabled"])
    if "openSpreadPct" in values:
        rule.open_spread_pct = values["openSpreadPct"]
    if "closeSpreadPct" in values:
        rule.close_spread_pct = values["closeSpreadPct"]
    if "premiumDiffPct" in values:
        rule.premium_diff_pct = values["premiumDiffPct"]
    if "cooldownMinutes" in values and values["cooldownMinutes"] is not None:
        rule.cooldown_minutes = max(1, min(int(values["cooldownMinutes"]), 1440))
    rule.updated_at = datetime.now(timezone.utc)
    db.flush()
    return push_rule_to_out(rule, item.id)


def snapshot_premium_diff_pct(snapshot: CryptoSpreadSnapshot) -> float | None:
    view = snapshot_view(snapshot)
    left = view["leftPremiumRate"]
    right = view["rightPremiumRate"]
    if left is None or right is None:
        return None
    return abs((right - left) * 100)


def crypto_detail_url(item_id: int) -> str | None:
    return None


def add_crypto_push_log(
    db: Session,
    item: CryptoWatchItem,
    rule: CryptoPushRule | None,
    event_type: str,
    title: str,
    body: str,
    spread_pct: float | None,
    premium_diff_pct: float | None,
    status: str,
    message: str | None,
) -> CryptoPushLog:
    log = CryptoPushLog(
        watch_item_id=item.id,
        rule_id=rule.id if rule else None,
        event_type=event_type,
        title=title,
        body=body,
        spread_pct=spread_pct,
        premium_diff_pct=premium_diff_pct,
        status=status,
        message=message,
    )
    db.add(log)
    db.flush()
    return log


def crypto_push_body(item: CryptoWatchItem, spread_pct: float | None, premium_diff_pct: float | None) -> str:
    spread_text = f"{spread_pct:.2f}%" if spread_pct is not None else "--"
    premium_text = f"{premium_diff_pct:.2f}%" if premium_diff_pct is not None else "--"
    return f"{item.symbol} {item.left_exchange}/{item.right_exchange} 差价 {spread_text}，溢价差 {premium_text}"


def evaluate_crypto_push_rule(db: Session, item: CryptoWatchItem, snapshot: CryptoSpreadSnapshot) -> CryptoPushLog | None:
    rule = crypto_push_rule(db, item.id)
    if not rule or not rule.enabled or snapshot.status != "ok":
        return None
    view = snapshot_view(snapshot)
    spread_pct = view["bidSpreadPct"]
    premium_diff_pct = snapshot_premium_diff_pct(snapshot)
    triggers: list[str] = []
    if rule.open_spread_pct is not None and spread_pct is not None and spread_pct >= rule.open_spread_pct:
        triggers.append("开仓差价")
    if rule.close_spread_pct is not None and spread_pct is not None and spread_pct <= rule.close_spread_pct:
        triggers.append("回落差价")
    if rule.premium_diff_pct is not None and premium_diff_pct is not None and premium_diff_pct >= rule.premium_diff_pct:
        triggers.append("溢价差")
    if not triggers:
        return None
    now = datetime.now(timezone.utc)
    if rule.last_triggered_at:
        elapsed = (now - utc_datetime(rule.last_triggered_at)).total_seconds() / 60
        if elapsed < max(1, rule.cooldown_minutes):
            return None
    title = f"价差提醒 {item.symbol} {'/'.join(triggers)}"
    body = crypto_push_body(item, spread_pct, premium_diff_pct)
    status, message = send_bark_or_log(
        enabled=True,
        title=title,
        body=body,
        group="FS机会",
        url=crypto_detail_url(item.id),
        disabled_message="推送规则未启用。",
    )
    rule.last_triggered_at = now
    rule.updated_at = now
    return add_crypto_push_log(db, item, rule, "rule_trigger", title, body, spread_pct, premium_diff_pct, status, message)


def test_crypto_push_rule(db: Session, item_id: int) -> dict[str, Any]:
    item = db.get(CryptoWatchItem, item_id)
    if not item:
        raise ValueError("加密货币跟踪项不存在")
    rule = crypto_push_rule(db, item.id, create=True)
    snapshot = latest_snapshot(db, item.id)
    spread_pct = None
    premium_diff_pct = None
    if snapshot:
        view = snapshot_view(snapshot)
        spread_pct = view["bidSpreadPct"]
        premium_diff_pct = snapshot_premium_diff_pct(snapshot)
    title = f"价差提醒测试 {item.symbol}"
    body = crypto_push_body(item, spread_pct, premium_diff_pct)
    status, message = send_bark_or_log(
        enabled=True,
        title=title,
        body=body,
        group="FS机会",
        url=crypto_detail_url(item.id),
        disabled_message="推送规则未启用。",
    )
    log = add_crypto_push_log(db, item, rule, "test", title, body, spread_pct, premium_diff_pct, status, message)
    return {"status": status, "message": message, "log": push_log_to_out(log)}


def crypto_watch_order_books(db: Session, item_id: int) -> dict[str, Any]:
    item = db.get(CryptoWatchItem, item_id)
    if not item:
        raise ValueError("加密货币跟踪项不存在")
    canonicalize_watch_item_direction(item)
    current_item = db.get(CryptoWatchItem, item.id)
    assert current_item is not None
    left_market_type = current_item.left_market_type or "futures"
    right_market_type = current_item.right_market_type or "futures"
    left_symbol = mapped_symbol_for(db, current_item.symbol, current_item.left_exchange, left_market_type)
    right_symbol = mapped_symbol_for(db, current_item.symbol, current_item.right_exchange, right_market_type)
    with ThreadPoolExecutor(max_workers=2) as executor:
        left_future = executor.submit(fetch_order_book_safe, current_item.left_exchange, left_symbol, left_market_type)
        right_future = executor.submit(fetch_order_book_safe, current_item.right_exchange, right_symbol, right_market_type)
        left_book = left_future.result()
        right_book = right_future.result()
    source_statuses = [left_book.status, right_book.status]
    if all(status == "ok" for status in source_statuses):
        status = "ok"
    elif any(status == "ok" for status in source_statuses):
        status = "partial_error"
    else:
        status = "error"
    updated_at = max([value for value in (left_book.updated_at, right_book.updated_at) if value], default=None)
    return {
        "status": status,
        "source_status": status,
        "updated_at": updated_at,
        "message": "交易行情已更新。",
        "item": crypto_item_to_out(db, current_item),
        "leftOrderBook": order_book_to_out(left_book),
        "rightOrderBook": order_book_to_out(right_book),
    }


def crypto_watch_detail(db: Session, item_id: int, hours: int = 24, limit: int = 5000) -> dict[str, Any]:
    item = db.get(CryptoWatchItem, item_id)
    if not item:
        raise ValueError("加密货币跟踪项不存在")
    canonicalize_watch_item_direction(item)
    history = crypto_watch_history(db, item.id, hours, limit)
    current_item = db.get(CryptoWatchItem, item.id)
    assert current_item is not None
    order_books = crypto_watch_order_books(db, current_item.id)
    source_statuses = [
        history["source_status"],
        order_books["leftOrderBook"]["status"],
        order_books["rightOrderBook"]["status"],
    ]
    if all(status == "ok" for status in source_statuses):
        status = "ok"
    elif any(status == "ok" for status in source_statuses):
        status = "partial_error"
    else:
        status = "error"
    updated_at = max(
        [
            value
            for value in (
                history.get("updated_at"),
                order_books.get("updated_at"),
            )
            if value
        ],
        default=None,
    )
    return {
        "status": status,
        "source_status": status,
        "updated_at": updated_at,
        "message": "监控详情已更新。",
        "item": order_books["item"],
        "leftOrderBook": order_books["leftOrderBook"],
        "rightOrderBook": order_books["rightOrderBook"],
        "history": history,
        "pushRule": push_rule_to_out(crypto_push_rule(db, current_item.id), current_item.id),
        "pushLogs": recent_crypto_push_logs(db, current_item.id),
    }


def fetch_symbol_quotes(
    symbol: str,
    codes: list[str] | None = None,
    mappings: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[MarketQuote]:
    normalized = normalize_symbol(symbol)
    enabled_codes = normalize_exchange_list(codes) if codes else board_exchange_codes()
    symbol_mappings = mappings or {}
    futures_by_exchange: dict[str, MarketQuote] = {}
    spot_by_exchange: dict[str, MarketQuote] = {}
    requests = [(code, market_type) for code in enabled_codes for market_type in board_market_types(code)]
    request_specs = {
        (code, market_type): symbol_mappings.get((code, market_type)) or {"mappedSymbol": normalized, "priceRatio": 1.0}
        for code, market_type in requests
    }
    with ThreadPoolExecutor(max_workers=min(max(len(requests), 1), 4)) as executor:
        future_map = {
            executor.submit(fetch_market, code, str(request_specs[(code, market_type)]["mappedSymbol"]), market_type): (code, market_type)
            for code, market_type in requests
        }
        for future in as_completed(future_map):
            code, market_type = future_map[future]
            request_spec = request_specs[(code, market_type)]
            request_symbol = str(request_spec.get("mappedSymbol") or normalized)
            try:
                quote = future.result()
            except Exception as exc:
                quote = MarketQuote(exchange=code, symbol=request_symbol, market_type=market_type, status="error", error=str(exc))
            quote = adjust_quote_price_ratio(quote, parse_float(request_spec.get("priceRatio")), normalized)
            if market_type == "futures":
                futures_by_exchange[code] = quote
            elif market_quote_live_for_pair(quote):
                spot_by_exchange[code] = quote
    futures_quotes = [
        futures_by_exchange.get(code)
        or MarketQuote(
            exchange=code,
            symbol=str(request_specs.get((code, "futures"), {}).get("mappedSymbol") or normalized),
            market_type="futures",
            status="error",
            error="接口未返回数据",
        )
        for code in enabled_codes
    ]
    spot_quotes = [spot_by_exchange[code] for code in enabled_codes if code in spot_by_exchange]
    return apply_quote_volume_filter(futures_quotes + spot_quotes)


def quote_to_board_row(quote: MarketQuote) -> dict[str, Any]:
    return {
        "exchange": quote.exchange,
        "exchangeName": EXCHANGE_NAMES.get(quote.exchange, quote.exchange),
        "symbol": quote.symbol,
        "period": f"{quote.period_hours:g}h" if quote.period_hours else "--",
        "fundingIntervalHours": quote.period_hours,
        "maxFundingRate": quote.max_funding_rate,
        "minFundingRate": quote.min_funding_rate,
        "currentFundingRate": quote.funding_rate,
        "borrowStatus": None,
        "borrowMessage": None,
        "canBorrow": None,
        "borrowableAmount": None,
        "borrowHourlyRate": None,
        "borrowDailyRate": None,
        "borrowPeriodRate": None,
        "fsNetFundingRate": None,
        "interestRate": quote.interest_rate,
        "indexComponentRate": computed_premium(quote.mark_price, quote.index_price),
        "premiumRate": quote.premium_rate,
        "fundingRule": quote.funding_rule,
        "fundingFormula": quote.funding_formula,
        "premiumSource": quote.premium_source,
        "openInterest": quote.open_interest,
        "riskFund": quote.risk_fund,
        "volume24h": quote.volume_24h,
        "bestBid": quote.best_bid,
        "bestAsk": quote.best_ask,
        "markPrice": quote.mark_price,
        "indexPrice": quote.index_price,
        "updatedAt": quote.updated_at,
        "status": quote.status,
        "lastError": quote.error,
    }


def borrow_period_rate(check: MarginShortCheck, period_hours: float | None) -> float | None:
    if not period_hours or period_hours <= 0:
        return None
    if check.hourly_borrow_rate is not None:
        return check.hourly_borrow_rate * period_hours
    if check.daily_borrow_rate is not None:
        return check.daily_borrow_rate / 24 * period_hours
    return None


def fs_net_funding_rate(
    current_funding_rate: float | None,
    borrow_cost: float | None,
    fee_cost: float | None = None,
    slippage_cost: float | None = None,
    basis_risk_cost: float | None = None,
) -> float | None:
    if current_funding_rate is None or borrow_cost is None:
        return None
    # FS here means spot-margin short + long futures. Negative funding is income for the futures long.
    return (
        -current_funding_rate
        - borrow_cost
        - (fee_cost or 0.0)
        - (slippage_cost or 0.0)
        - (basis_risk_cost or 0.0)
    )


def fs_basis_rate(futures_bid: float | None, spot_ask: float | None) -> float | None:
    if futures_bid is None or spot_ask is None or futures_bid <= 0 or spot_ask <= 0:
        return None
    return futures_bid / spot_ask - 1


def fs_astro_spread_rate(spot_price: float | None, futures_price: float | None) -> float | None:
    """Astro FS spread: 2 * (spot - futures) / (spot + futures)."""
    spot = parse_float(spot_price)
    futures = parse_float(futures_price)
    if futures is None or spot is None or futures <= 0 or spot <= 0:
        return None
    denominator = futures + spot
    if denominator <= 0:
        return None
    return 2 * (spot - futures) / denominator


def fs_basis_risk(basis_rate: float | None) -> str:
    if basis_rate is None:
        return "unknown"
    if abs(basis_rate) > FS_SIGNAL_ABS_BASIS_MISMATCH_THRESHOLD:
        return "danger"
    if basis_rate <= FS_SIGNAL_DEEP_DISCOUNT_THRESHOLD:
        return "danger"
    if FS_SIGNAL_IDEAL_BASIS_LOW <= basis_rate <= FS_SIGNAL_IDEAL_BASIS_HIGH:
        return "normal"
    return "watch"


def fs_basis_message(basis_rate: float | None) -> str:
    risk = fs_basis_risk(basis_rate)
    if risk == "normal":
        return "基差在优先区间。"
    if risk == "danger":
        if basis_rate is not None and abs(basis_rate) > FS_SIGNAL_ABS_BASIS_MISMATCH_THRESHOLD:
            return f"基差超过 {rate_pct_text(FS_SIGNAL_ABS_BASIS_MISMATCH_THRESHOLD)}，疑似错配或不可交易，只观察不推送。"
        return f"合约相对现货折价超过 {rate_pct_text(abs(FS_SIGNAL_DEEP_DISCOUNT_THRESHOLD))}，只观察不推送。"
    if risk == "watch":
        return "基差不在优先区间，需降低权重。"
    return "基差暂不可计算。"


def enrich_exchange_borrow_checks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        symbol = row.get("symbol")
        check = cached_margin_short_check(FS_SIGNAL_SPOT_EXCHANGE, symbol) if isinstance(symbol, str) else None
        if not check:
            enriched.append(row)
            continue
        period_rate = borrow_period_rate(check, row.get("fundingIntervalHours"))
        enriched.append(
            {
                **row,
                "borrowStatus": check.status,
                "borrowMessage": check.message,
                "canBorrow": check.can_borrow,
                "borrowableAmount": check.borrowable_amount,
                "borrowHourlyRate": check.hourly_borrow_rate,
                "borrowDailyRate": check.daily_borrow_rate,
                "borrowPeriodRate": period_rate,
                "fsNetFundingRate": fs_net_funding_rate(row.get("currentFundingRate"), period_rate),
            }
        )
    return enriched


def pct_from_prices(left_price: float | None, right_price: float | None) -> float | None:
    if left_price is None or right_price is None:
        return None
    denominator = left_price + right_price
    if denominator == 0:
        return None
    return (right_price - left_price) / denominator * 2 * 100


def spread_from_prices(left_price: float | None, right_price: float | None) -> float | None:
    if left_price is None or right_price is None:
        return None
    return right_price - left_price


def executable_spread_pct(left: MarketQuote, right: MarketQuote) -> float | None:
    return pct_from_prices(left.best_ask, right.best_bid)


def exit_spread_pct(left: MarketQuote, right: MarketQuote) -> float | None:
    return pct_from_prices(left.best_bid, right.best_ask)


def executable_spread(left: MarketQuote, right: MarketQuote) -> float | None:
    return spread_from_prices(left.best_ask, right.best_bid)


def spot_leg_for_margin_short(left: MarketQuote, right: MarketQuote) -> MarketQuote | None:
    if left.market_type == "spot" and right.market_type != "spot":
        return left
    if right.market_type == "spot" and left.market_type != "spot":
        return right
    return None


def margin_short_fields(left: MarketQuote, right: MarketQuote, check: MarginShortCheck | None = None) -> dict[str, Any]:
    spot_leg = spot_leg_for_margin_short(left, right)
    if not spot_leg:
        return {
            "marginShortRequired": False,
            "marginShortExchange": None,
            "marginShortSymbol": None,
            "marginShortStatus": "not_applicable",
            "marginShortMessage": "合约/合约价差不需要杠杆卖出现货。",
            "marginShortCheck": None,
        }
    if check is None:
        check = unavailable_margin_short_check(
            spot_leg.exchange,
            spot_leg.symbol,
            "pending",
            "等待杠杆可借 API 复核。",
        )
    return {
        "marginShortRequired": True,
        "marginShortExchange": spot_leg.exchange,
        "marginShortSymbol": spot_leg.symbol,
        "marginShortStatus": check.status,
        "marginShortMessage": check.message,
        "marginShortCheck": margin_short_check_to_out(check),
    }


def snapshot_view(snapshot: CryptoSpreadSnapshot) -> dict[str, Any]:
    left_market_type = snapshot.left_market_type or "futures"
    right_market_type = snapshot.right_market_type or "futures"
    swap_sides = left_market_type == "futures" and right_market_type == "spot"
    if swap_sides:
        left_market_type, right_market_type = right_market_type, left_market_type
        left_exchange, right_exchange = snapshot.right_exchange, snapshot.left_exchange
        left_bid, right_bid = snapshot.right_bid, snapshot.left_bid
        left_ask, right_ask = snapshot.right_ask, snapshot.left_ask
        left_funding_rate, right_funding_rate = snapshot.right_funding_rate, snapshot.left_funding_rate
        left_premium_rate, right_premium_rate = snapshot.right_premium_rate, snapshot.left_premium_rate
        left_mark_price, right_mark_price = snapshot.right_mark_price, snapshot.left_mark_price
        left_index_price, right_index_price = snapshot.right_index_price, snapshot.left_index_price
    else:
        left_exchange, right_exchange = snapshot.left_exchange, snapshot.right_exchange
        left_bid, right_bid = snapshot.left_bid, snapshot.right_bid
        left_ask, right_ask = snapshot.left_ask, snapshot.right_ask
        left_funding_rate, right_funding_rate = snapshot.left_funding_rate, snapshot.right_funding_rate
        left_premium_rate, right_premium_rate = snapshot.left_premium_rate, snapshot.right_premium_rate
        left_mark_price, right_mark_price = snapshot.left_mark_price, snapshot.right_mark_price
        left_index_price, right_index_price = snapshot.left_index_price, snapshot.right_index_price
    spread = spread_from_prices(left_ask, right_bid)
    spread_pct = pct_from_prices(left_ask, right_bid)
    ask_spread_pct = pct_from_prices(left_bid, right_ask)
    return {
        "leftExchange": left_exchange,
        "rightExchange": right_exchange,
        "leftMarketType": left_market_type,
        "rightMarketType": right_market_type,
        "leftBid": left_bid,
        "leftAsk": left_ask,
        "rightBid": right_bid,
        "rightAsk": right_ask,
        "spread": spread,
        "spreadPct": spread_pct,
        "bidSpreadPct": spread_pct,
        "askSpreadPct": ask_spread_pct,
        "leftFundingRate": left_funding_rate,
        "rightFundingRate": right_funding_rate,
        "leftPremiumRate": left_premium_rate,
        "rightPremiumRate": right_premium_rate,
        "leftMarkPrice": left_mark_price,
        "rightMarkPrice": right_mark_price,
        "leftIndexPrice": left_index_price,
        "rightIndexPrice": right_index_price,
    }


def market_letter(market_type: str | None) -> str:
    return "S" if market_type == "spot" else "F"


def pair_type_for(left: MarketQuote, right: MarketQuote) -> str:
    return "S" if left.market_type == "spot" or right.market_type == "spot" else "F"


def market_quote_live_for_pair(quote: MarketQuote) -> bool:
    return quote.status == "ok" and quote.best_bid is not None and quote.best_ask is not None


def apply_quote_volume_filter(quotes: list[MarketQuote]) -> list[MarketQuote]:
    threshold = min_quote_volume_24h_usdt()
    for quote in quotes:
        if quote.status != "ok":
            continue
        if quote.volume_24h is None:
            quote.status = "unknown_volume"
            quote.error = f"24h 成交额未知，未纳入监控沉淀；阈值 {threshold:g} USDT"
            continue
        if quote.volume_24h < threshold:
            quote.status = "low_volume"
            quote.error = f"24h 成交额 {quote.volume_24h:g} USDT 低于阈值 {threshold:g} USDT"
    return quotes


def persist_crypto_market_quotes(db: Session, symbol: str, quotes: list[MarketQuote], batch_time: datetime) -> int:
    normalized = normalize_symbol(symbol)
    for quote in quotes:
        quote.symbol = normalized
    return persist_crypto_market_quote_rows(db, quotes, batch_time, "exchange_api")


def persist_crypto_market_quote_rows(db: Session, quotes: list[MarketQuote], batch_time: datetime, source: str) -> int:
    rows: list[CryptoMarketQuoteSnapshot] = []
    for quote in quotes:
        if quote.status != "ok":
            continue
        rows.append(
            CryptoMarketQuoteSnapshot(
                batch_time=batch_time,
                symbol=normalize_symbol(quote.symbol),
                exchange=quote.exchange,
                market_type=quote.market_type,
                best_bid=quote.best_bid,
                best_ask=quote.best_ask,
                mark_price=quote.mark_price,
                index_price=quote.index_price,
                funding_rate=quote.funding_rate,
                premium_rate=quote.premium_rate,
                max_funding_rate=quote.max_funding_rate,
                min_funding_rate=quote.min_funding_rate,
                open_interest=quote.open_interest,
                risk_fund=quote.risk_fund,
                volume_24h=quote.volume_24h,
                source=source,
                status=quote.status,
                last_error=quote.error,
                created_at=batch_time,
            )
        )
    if not rows:
        return 0
    db.add_all(rows)
    db.commit()
    return len(rows)


def canonicalize_spot_pair(left: MarketQuote, right: MarketQuote) -> tuple[MarketQuote, MarketQuote]:
    if left.market_type == "futures" and right.market_type == "spot":
        return right, left
    return left, right


def oriented_pair(left: MarketQuote, right: MarketQuote) -> tuple[MarketQuote, MarketQuote, float | None, float | None]:
    if left.market_type != right.market_type:
        spot_left, futures_right = canonicalize_spot_pair(left, right)
        return spot_left, futures_right, executable_spread_pct(spot_left, futures_right), exit_spread_pct(spot_left, futures_right)

    forward_bid_pct = executable_spread_pct(left, right)
    reverse_bid_pct = executable_spread_pct(right, left)
    forward_ask_pct = exit_spread_pct(left, right)
    reverse_ask_pct = exit_spread_pct(right, left)
    forward_score = forward_bid_pct if forward_bid_pct is not None else float("-inf")
    reverse_score = reverse_bid_pct if reverse_bid_pct is not None else float("-inf")
    if reverse_score > forward_score:
        return right, left, reverse_bid_pct, reverse_ask_pct
    return left, right, forward_bid_pct, forward_ask_pct


def build_pair_rows(quotes: list[MarketQuote], symbol: str | None = None) -> list[dict[str, Any]]:
    display_symbol = normalize_symbol(symbol) if symbol else None
    if display_symbol and is_delisted_crypto_symbol(display_symbol):
        return []
    live_quotes = [quote for quote in quotes if market_quote_live_for_pair(quote)]
    rows: list[dict[str, Any]] = []
    for left_index, left in enumerate(live_quotes):
        for right in live_quotes[left_index + 1 :]:
            if left.market_type == "spot" and right.market_type == "spot":
                continue
            if left.exchange == right.exchange and left.market_type == right.market_type:
                continue
            long_quote, short_quote, bid_spread_pct, ask_spread_pct = oriented_pair(left, right)
            if bid_spread_pct is None and ask_spread_pct is None:
                continue
            pair_type = pair_type_for(long_quote, short_quote)
            row_symbol = display_symbol or long_quote.symbol
            margin_fields = margin_short_fields(long_quote, short_quote)
            rows.append(
                {
                    "symbol": row_symbol,
                    "leftExchange": long_quote.exchange,
                    "rightExchange": short_quote.exchange,
                    "leftMarketType": long_quote.market_type,
                    "rightMarketType": short_quote.market_type,
                    "pairType": pair_type,
                    "pair": f"[{pair_type}] {long_quote.exchange} / {short_quote.exchange}",
                    "label": label_for(
                        row_symbol,
                        long_quote.exchange,
                        short_quote.exchange,
                        long_quote.market_type,
                        short_quote.market_type,
                    ),
                    "spread": spread_from_prices(long_quote.best_ask, short_quote.best_bid),
                    "spreadPct": bid_spread_pct,
                    "reverseSpreadPct": ask_spread_pct,
                    "bidSpreadPct": bid_spread_pct,
                    "askSpreadPct": ask_spread_pct,
                    "leftBid": long_quote.best_bid,
                    "leftAsk": long_quote.best_ask,
                    "rightBid": short_quote.best_bid,
                    "rightAsk": short_quote.best_ask,
                    "leftFundingRate": long_quote.funding_rate,
                    "rightFundingRate": short_quote.funding_rate,
                    "leftPremiumRate": long_quote.premium_rate,
                    "rightPremiumRate": short_quote.premium_rate,
                    "leftMarkPrice": long_quote.mark_price,
                    "rightMarkPrice": short_quote.mark_price,
                    "leftIndexPrice": long_quote.index_price,
                    "rightIndexPrice": short_quote.index_price,
                    "leftVolume24h": long_quote.volume_24h,
                    "rightVolume24h": short_quote.volume_24h,
                    "status": "ok",
                    **margin_fields,
                }
            )
    return sorted(rows, key=lambda row: row["bidSpreadPct"] if row["bidSpreadPct"] is not None else float("-inf"), reverse=True)


def pair_signal_empty() -> dict[str, Any]:
    return {
        "previousSpreadPct": None,
        "spreadJumpPct": None,
        "crossedWatch": False,
        "crossedStrong": False,
        "fundingDiffPct": None,
        "premiumDiffPct": None,
        "ffSignalLevel": "none",
        "ffSignalReason": None,
    }


def pair_signal_level(row: dict[str, Any], previous_spread: float | None, funding_diff: float | None, premium_diff: float | None) -> tuple[str, str | None]:
    current_spread = row.get("bidSpreadPct")
    if row.get("pairType") != "F" or not isinstance(current_spread, (int, float)):
        return "none", None
    spread_jump = current_spread - previous_spread if isinstance(previous_spread, (int, float)) else None
    if not isinstance(spread_jump, (int, float)):
        return "none", "缺少上一条快照，暂不判断上穿。"
    crossed_watch = previous_spread < FF_SIGNAL_WATCH_THRESHOLD <= current_spread
    crossed_strong = previous_spread < FF_SIGNAL_STRONG_THRESHOLD <= current_spread
    if current_spread >= FF_SIGNAL_STRONG_THRESHOLD and spread_jump >= FF_SIGNAL_JUMP_THRESHOLD:
        if (funding_diff is not None and funding_diff >= 0) and (premium_diff is not None and premium_diff < 0):
            return "strong", "差价 >=1.4%，跳幅 >=0.15%，资金费不拖后腿，且高价端溢价未同步确认。"
        if crossed_strong:
            return "watch", "上穿 1.4% 且跳幅达标，但资金费或溢价条件未达到强信号。"
        return "watch", "差价 >=1.4% 且跳幅达标，但资金费或溢价条件未达到强信号。"
    if current_spread >= FF_SIGNAL_WATCH_THRESHOLD and spread_jump >= FF_SIGNAL_JUMP_THRESHOLD:
        if crossed_watch:
            return "watch", "上穿 0.7% 且跳幅达标，进入观察。"
        return "watch", "差价 >=0.7% 且跳幅达标，进入观察。"
    if current_spread >= FF_SIGNAL_STRONG_THRESHOLD:
        return "state", "差价已在 1.4% 以上，但不是刚上穿。"
    return "none", None


def ff_signal_rank(row: dict[str, Any]) -> int:
    level = row.get("ffSignalLevel")
    if level == "strong":
        return 3
    if level == "watch":
        return 2
    if level == "state":
        return 1
    return 0


def spread_point_text(value: float | None) -> str:
    return f"{value:+.3f}%" if isinstance(value, (int, float)) else "--"


def ff_compound_signal_body(row: dict[str, Any]) -> str:
    symbol = normalize_symbol(str(row.get("symbol") or ""))
    left_exchange = str(row.get("leftExchange") or "--").upper()
    right_exchange = str(row.get("rightExchange") or "--").upper()
    spread = spread_point_text(row.get("bidSpreadPct"))
    jump = spread_point_text(row.get("spreadJumpPct"))
    funding = rate_pct_text(row.get("fundingDiffPct"))
    premium = rate_pct_text(row.get("premiumDiffPct"))
    return (
        f"{symbol} FF复利信号：买 {left_exchange} 合约，卖 {right_exchange} 合约；"
        f"差价 {spread}，跳幅 {jump}，资金费差 {funding}，溢价差 {premium}。"
        "按规则优先看 0.20%-0.30% 收敛退出。"
    )


def record_ff_compound_signal_event(db: Session, row: dict[str, Any]) -> None:
    if row.get("pairType") != "F" or row.get("ffSignalLevel") != "strong":
        return
    symbol = normalize_symbol(str(row.get("symbol") or ""))
    if not has_active_borrow_watch(db, symbol):
        return
    right_exchange = str(row.get("rightExchange") or "")
    left_exchange = str(row.get("leftExchange") or "")
    if right_exchange not in SUPPORTED_EXCHANGES or left_exchange not in SUPPORTED_EXCHANGES:
        return
    add_crypto_monitor_event(
        db=db,
        symbol=symbol,
        exchange=right_exchange,
        event_type="ff_compound_signal",
        severity="opportunity",
        title=f"{symbol} FF复利信号",
        body=ff_compound_signal_body(row),
        details={
            "leftExchange": left_exchange,
            "rightExchange": right_exchange,
            "leftMarketType": row.get("leftMarketType"),
            "rightMarketType": row.get("rightMarketType"),
            "bidSpreadPct": row.get("bidSpreadPct"),
            "previousSpreadPct": row.get("previousSpreadPct"),
            "spreadJumpPct": row.get("spreadJumpPct"),
            "fundingDiffPct": row.get("fundingDiffPct"),
            "premiumDiffPct": row.get("premiumDiffPct"),
            "leftFundingRate": row.get("leftFundingRate"),
            "rightFundingRate": row.get("rightFundingRate"),
            "leftPremiumRate": row.get("leftPremiumRate"),
            "rightPremiumRate": row.get("rightPremiumRate"),
            "leftVolume24h": row.get("leftVolume24h"),
            "rightVolume24h": row.get("rightVolume24h"),
            "reason": row.get("ffSignalReason"),
        },
    )


def snapshot_to_market_quote(snapshot: CryptoMarketQuoteSnapshot) -> MarketQuote:
    return MarketQuote(
        exchange=snapshot.exchange,
        symbol=snapshot.symbol,
        market_type=snapshot.market_type,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        mark_price=snapshot.mark_price,
        index_price=snapshot.index_price,
        funding_rate=snapshot.funding_rate,
        premium_rate=snapshot.premium_rate,
        max_funding_rate=snapshot.max_funding_rate,
        min_funding_rate=snapshot.min_funding_rate,
        open_interest=snapshot.open_interest,
        risk_fund=snapshot.risk_fund,
        volume_24h=snapshot.volume_24h,
        updated_at=utc_datetime(snapshot.batch_time),
        status=snapshot.status,
        error=snapshot.last_error,
    )


def compound_signal_key(strategy: str, symbol: str, left_exchange: str, left_market_type: str, right_exchange: str, right_market_type: str, opened_at: datetime) -> str:
    return (
        f"{strategy}:{normalize_symbol(symbol)}:"
        f"{normalize_exchange(left_exchange)}:{normalize_market_type(left_market_type)}:"
        f"{normalize_exchange(right_exchange)}:{normalize_market_type(right_market_type)}:"
        f"{utc_datetime(opened_at).isoformat() if utc_datetime(opened_at) else opened_at.isoformat()}"
    )


def latest_compound_open_signal_log(
    db: Session,
    strategy: str,
    symbol: str,
    left_exchange: str,
    left_market_type: str,
    right_exchange: str,
    right_market_type: str,
) -> CryptoCompoundOpenSignalLog | None:
    return db.scalar(
        select(CryptoCompoundOpenSignalLog)
        .where(
            CryptoCompoundOpenSignalLog.strategy == strategy,
            CryptoCompoundOpenSignalLog.symbol == normalize_symbol(symbol),
            CryptoCompoundOpenSignalLog.left_exchange == normalize_exchange(left_exchange),
            CryptoCompoundOpenSignalLog.left_market_type == normalize_market_type(left_market_type),
            CryptoCompoundOpenSignalLog.right_exchange == normalize_exchange(right_exchange),
            CryptoCompoundOpenSignalLog.right_market_type == normalize_market_type(right_market_type),
            CryptoCompoundOpenSignalLog.created_at >= datetime.now(timezone.utc) - timedelta(minutes=COMPOUND_OPEN_SIGNAL_COOLDOWN_MINUTES),
        )
        .order_by(desc(CryptoCompoundOpenSignalLog.created_at))
        .limit(1)
    )


def current_compound_snapshot_quotes(db: Session, limit_symbols: int = 220) -> tuple[list[MarketQuote], datetime | None]:
    latest_batch_time = db.scalar(
        select(CryptoMarketQuoteSnapshot.batch_time)
        .where(
            CryptoMarketQuoteSnapshot.status == "ok",
            CryptoMarketQuoteSnapshot.source.in_(COMPOUND_OPEN_SIGNAL_SOURCES),
        )
        .order_by(desc(CryptoMarketQuoteSnapshot.batch_time))
        .limit(1)
    )
    latest_batch_time = utc_datetime(latest_batch_time)
    if latest_batch_time is None:
        return [], None
    since = latest_batch_time - timedelta(seconds=COMPOUND_OPEN_SIGNAL_SNAPSHOT_WINDOW_SECONDS)
    rows = list(
        db.scalars(
            select(CryptoMarketQuoteSnapshot)
            .where(
                CryptoMarketQuoteSnapshot.batch_time >= since,
                CryptoMarketQuoteSnapshot.status == "ok",
                CryptoMarketQuoteSnapshot.source.in_(COMPOUND_OPEN_SIGNAL_SOURCES),
            )
            .order_by(desc(CryptoMarketQuoteSnapshot.batch_time))
            .limit(8000)
        )
    )
    latest_by_key: dict[tuple[str, str, str], CryptoMarketQuoteSnapshot] = {}
    for row in rows:
        symbol = normalize_symbol(row.symbol)
        if is_delisted_crypto_symbol(symbol):
            continue
        key = (symbol, row.exchange, row.market_type)
        current = latest_by_key.get(key)
        if current is None or utc_datetime(row.batch_time) > utc_datetime(current.batch_time):
            latest_by_key[key] = row
    selected_symbols: list[str] = []
    for symbol, _exchange, _market in latest_by_key:
        if symbol not in selected_symbols:
            selected_symbols.append(symbol)
        if len(selected_symbols) >= limit_symbols:
            break
    quotes = [
        snapshot_to_market_quote(row)
        for key, row in latest_by_key.items()
        if key[0] in selected_symbols
    ]
    return apply_quote_volume_filter(quotes), latest_batch_time


def pair_opened_at(row: dict[str, Any], quotes: list[MarketQuote]) -> datetime | None:
    left_time = next(
        (
            quote.updated_at
            for quote in quotes
            if quote.symbol == row.get("symbol")
            and quote.exchange == row.get("leftExchange")
            and quote.market_type == row.get("leftMarketType")
        ),
        None,
    )
    right_time = next(
        (
            quote.updated_at
            for quote in quotes
            if quote.symbol == row.get("symbol")
            and quote.exchange == row.get("rightExchange")
            and quote.market_type == row.get("rightMarketType")
        ),
        None,
    )
    times = [utc_datetime(value) for value in (left_time, right_time) if value is not None]
    return min(times) if times else None


def pair_spread_points(
    db: Session,
    symbol: str,
    left_exchange: str,
    left_market_type: str,
    right_exchange: str,
    right_market_type: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            CryptoMarketQuoteSnapshot.batch_time,
            CryptoMarketQuoteSnapshot.source,
            CryptoMarketQuoteSnapshot.exchange,
            CryptoMarketQuoteSnapshot.market_type,
            CryptoMarketQuoteSnapshot.best_bid,
            CryptoMarketQuoteSnapshot.best_ask,
        )
        .where(
            CryptoMarketQuoteSnapshot.symbol == normalize_symbol(symbol),
            CryptoMarketQuoteSnapshot.batch_time >= start_at,
            CryptoMarketQuoteSnapshot.batch_time <= end_at,
            CryptoMarketQuoteSnapshot.status == "ok",
            CryptoMarketQuoteSnapshot.exchange.in_([normalize_exchange(left_exchange), normalize_exchange(right_exchange)]),
            CryptoMarketQuoteSnapshot.market_type.in_([normalize_market_type(left_market_type), normalize_market_type(right_market_type)]),
            CryptoMarketQuoteSnapshot.source.in_(COMPOUND_OPEN_SIGNAL_SOURCES),
        )
        .order_by(CryptoMarketQuoteSnapshot.batch_time)
    ).all()
    by_time: dict[tuple[datetime, str], dict[tuple[str, str], Any]] = {}
    for item in rows:
        by_time.setdefault((utc_datetime(item[0]) or item[0], item[1]), {})[(item[2], item[3])] = item
    points: list[dict[str, Any]] = []
    for (batch_time, source), items in by_time.items():
        left = items.get((normalize_exchange(left_exchange), normalize_market_type(left_market_type)))
        right = items.get((normalize_exchange(right_exchange), normalize_market_type(right_market_type)))
        if not left or not right:
            continue
        spread = pct_from_prices(left[5], right[4])
        if spread is None:
            continue
        points.append({"time": utc_datetime(batch_time) or batch_time, "source": source, "spread": spread})
    return sorted(points, key=lambda item: item["time"])


def previous_compound_spread(db: Session, row: dict[str, Any], opened_at: datetime) -> float | None:
    points = pair_spread_points(
        db,
        str(row.get("symbol")),
        str(row.get("leftExchange")),
        str(row.get("leftMarketType")),
        str(row.get("rightExchange")),
        str(row.get("rightMarketType")),
        opened_at - timedelta(minutes=FF_SIGNAL_LOOKBACK_MINUTES),
        opened_at - timedelta(milliseconds=1),
    )
    return points[-1]["spread"] if points else None


def compound_recent_spread_confirmed(db: Session, row: dict[str, Any], opened_at: datetime) -> bool:
    points = pair_spread_points(
        db,
        str(row.get("symbol")),
        str(row.get("leftExchange")),
        str(row.get("leftMarketType")),
        str(row.get("rightExchange")),
        str(row.get("rightMarketType")),
        opened_at - timedelta(seconds=COMPOUND_OPEN_SIGNAL_CONFIRM_WINDOW_SECONDS),
        opened_at + timedelta(seconds=1),
    )
    confirmed = [
        point
        for point in points
        if isinstance(point.get("spread"), (int, float))
        and point["spread"] >= FF_SIGNAL_STRONG_THRESHOLD
    ]
    return len(confirmed) >= COMPOUND_OPEN_SIGNAL_MIN_CONFIRM_POINTS


def compound_futures_reference_diff_pct(row: dict[str, Any]) -> float | None:
    if row.get("pairType") != "F":
        return None
    left_reference = first_float(row.get("leftIndexPrice"), row.get("leftMarkPrice"))
    right_reference = first_float(row.get("rightIndexPrice"), row.get("rightMarkPrice"))
    if left_reference is None or right_reference is None:
        return None
    diff = pct_from_prices(left_reference, right_reference)
    return abs(diff) if diff is not None else None


def review_compound_spread(db: Session, log: CryptoCompoundOpenSignalLog, minutes: int) -> tuple[float | None, datetime | None]:
    target = utc_datetime(log.opened_at) + timedelta(minutes=minutes)
    points = pair_spread_points(
        db,
        log.symbol,
        log.left_exchange,
        log.left_market_type,
        log.right_exchange,
        log.right_market_type,
        target,
        target + timedelta(minutes=10),
    )
    if not points:
        return None, None
    return points[0]["spread"], points[0]["time"]


def compound_open_signal_to_out(log: CryptoCompoundOpenSignalLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "signalKey": log.signal_key,
        "strategy": log.strategy,
        "symbol": log.symbol,
        "leftExchange": log.left_exchange,
        "rightExchange": log.right_exchange,
        "leftMarketType": log.left_market_type,
        "rightMarketType": log.right_market_type,
        "openedAt": log.opened_at,
        "openSpreadPct": log.open_spread_pct,
        "previousSpreadPct": log.previous_spread_pct,
        "spreadJumpPct": log.spread_jump_pct,
        "fundingDiffPct": log.funding_diff_pct,
        "premiumDiffPct": log.premium_diff_pct,
        "leftVolume24h": log.left_volume_24h,
        "rightVolume24h": log.right_volume_24h,
        "reason": log.reason,
        "review30mSpreadPct": log.review_30m_spread_pct,
        "review30mConvergencePct": log.review_30m_convergence_pct,
        "review30mAt": log.review_30m_at,
        "review60mSpreadPct": log.review_60m_spread_pct,
        "review60mConvergencePct": log.review_60m_convergence_pct,
        "review60mAt": log.review_60m_at,
        "status": log.status,
        "pushed": bool(log.pushed),
        "pushStatus": log.push_status,
        "pushMessage": log.push_message,
        "createdAt": log.created_at,
    }


def compound_open_signal_reason(row: dict[str, Any], previous_spread: float | None, spread_jump: float | None) -> str | None:
    strategy = row.get("pairType")
    current_spread = row.get("bidSpreadPct")
    if not isinstance(current_spread, (int, float)) or current_spread < FF_SIGNAL_STRONG_THRESHOLD:
        return None
    if current_spread > COMPOUND_OPEN_SIGNAL_MAX_OPEN_SPREAD_PCT:
        return None
    if not isinstance(previous_spread, (int, float)) or not isinstance(spread_jump, (int, float)) or spread_jump < FF_SIGNAL_JUMP_THRESHOLD:
        return None
    if previous_spread >= FF_SIGNAL_STRONG_THRESHOLD:
        return None
    left_volume = row.get("leftVolume24h")
    right_volume = row.get("rightVolume24h")
    threshold = min_quote_volume_24h_usdt()
    if not isinstance(left_volume, (int, float)) or not isinstance(right_volume, (int, float)) or left_volume < threshold or right_volume < threshold:
        return None
    if strategy == "F":
        reference_diff = compound_futures_reference_diff_pct(row)
        if reference_diff is None or reference_diff > COMPOUND_OPEN_SIGNAL_MAX_FUTURES_INDEX_DIFF_PCT:
            return None
        funding_diff = row.get("rightFundingRate") - row.get("leftFundingRate") if isinstance(row.get("rightFundingRate"), (int, float)) and isinstance(row.get("leftFundingRate"), (int, float)) else None
        premium_diff = row.get("rightPremiumRate") - row.get("leftPremiumRate") if isinstance(row.get("rightPremiumRate"), (int, float)) and isinstance(row.get("leftPremiumRate"), (int, float)) else None
        if funding_diff is None or funding_diff < 0:
            return None
        if premium_diff is None or premium_diff >= 0:
            return None
        return "FF：差价>=1.4%，跳幅>=0.15%，资金费不拖后腿，卖出腿溢价未同步确认。"
    if strategy == "S":
        if row.get("leftMarketType") != "spot" or row.get("rightMarketType") != "futures":
            return None
        right_funding = row.get("rightFundingRate")
        if not isinstance(right_funding, (int, float)) or right_funding < 0:
            return None
        return "SF：买现货卖合约，差价>=1.4%，跳幅>=0.15%，卖出合约资金费不为负。"
    return None


def compound_open_signal_event_body(
    strategy: str,
    symbol: str,
    left_exchange: str,
    left_market_type: str,
    right_exchange: str,
    right_market_type: str,
    spread_pct: float | None,
    spread_jump: float | None,
    funding_diff: float | None,
    premium_diff: float | None,
) -> str:
    left_name = EXCHANGE_NAMES.get(left_exchange, left_exchange).upper()
    right_name = EXCHANGE_NAMES.get(right_exchange, right_exchange).upper()
    left_type = "现货" if left_market_type == "spot" else "合约"
    right_type = "现货" if right_market_type == "spot" else "合约"
    label = "FF" if strategy == "F" else "SF"
    return (
        f"{symbol} {label}系统开仓信号：买 {left_name} {left_type}，卖 {right_name} {right_type}；"
        f"差价 {spread_point_text(spread_pct)}，跳幅 {spread_point_text(spread_jump)}，"
        f"资金费差 {rate_pct_text(funding_diff)}，溢价差 {rate_pct_text(premium_diff)}。"
        "仅为系统信号，不自动下单。"
    )


def add_compound_open_signal_log(db: Session, row: dict[str, Any], opened_at: datetime, previous_spread: float, spread_jump: float, reason: str) -> CryptoCompoundOpenSignalLog | None:
    strategy = str(row.get("pairType"))
    symbol = normalize_symbol(str(row.get("symbol") or ""))
    left_exchange = normalize_exchange(str(row.get("leftExchange") or ""))
    right_exchange = normalize_exchange(str(row.get("rightExchange") or ""))
    left_market_type = normalize_market_type(str(row.get("leftMarketType") or "futures"))
    right_market_type = normalize_market_type(str(row.get("rightMarketType") or "futures"))
    if latest_compound_open_signal_log(db, strategy, symbol, left_exchange, left_market_type, right_exchange, right_market_type):
        return None
    funding_diff = row.get("rightFundingRate") - row.get("leftFundingRate") if isinstance(row.get("rightFundingRate"), (int, float)) and isinstance(row.get("leftFundingRate"), (int, float)) else None
    premium_diff = row.get("rightPremiumRate") - row.get("leftPremiumRate") if isinstance(row.get("rightPremiumRate"), (int, float)) and isinstance(row.get("leftPremiumRate"), (int, float)) else None
    log = CryptoCompoundOpenSignalLog(
        signal_key=compound_signal_key(strategy, symbol, left_exchange, left_market_type, right_exchange, right_market_type, opened_at),
        strategy=strategy,
        symbol=symbol,
        left_exchange=left_exchange,
        right_exchange=right_exchange,
        left_market_type=left_market_type,
        right_market_type=right_market_type,
        opened_at=opened_at,
        open_spread_pct=row.get("bidSpreadPct"),
        previous_spread_pct=previous_spread,
        spread_jump_pct=spread_jump,
        funding_diff_pct=funding_diff,
        premium_diff_pct=premium_diff,
        left_volume_24h=row.get("leftVolume24h"),
        right_volume_24h=row.get("rightVolume24h"),
        reason=reason,
        status="open",
    )
    db.add(log)
    db.flush()
    if has_active_borrow_watch(db, symbol):
        event_type = "ff_compound_signal" if strategy == "F" else "sf_compound_signal"
        event = add_crypto_monitor_event(
            db=db,
            symbol=symbol,
            exchange=right_exchange,
            event_type=event_type,
            severity="opportunity",
            title=f"{symbol} {'FF' if strategy == 'F' else 'SF'}系统开仓信号",
            body=compound_open_signal_event_body(
                strategy,
                symbol,
                left_exchange,
                left_market_type,
                right_exchange,
                right_market_type,
                row.get("bidSpreadPct"),
                spread_jump,
                funding_diff,
                premium_diff,
            ),
            details={
                "strategy": strategy,
                "leftExchange": left_exchange,
                "rightExchange": right_exchange,
                "leftMarketType": left_market_type,
                "rightMarketType": right_market_type,
                "openSpreadPct": row.get("bidSpreadPct"),
                "previousSpreadPct": previous_spread,
                "spreadJumpPct": spread_jump,
                "fundingDiffPct": funding_diff,
                "premiumDiffPct": premium_diff,
                "leftVolume24h": row.get("leftVolume24h"),
                "rightVolume24h": row.get("rightVolume24h"),
                "reason": reason,
                "openedAt": opened_at.isoformat(),
            },
            push=True,
            cooldown_minutes=COMPOUND_OPEN_SIGNAL_COOLDOWN_MINUTES,
        )
        if event:
            log.pushed = event.pushed
            log.push_status = event.push_status
            log.push_message = event.push_message
        else:
            log.push_status = "skipped"
            log.push_message = f"同一信号 {COMPOUND_OPEN_SIGNAL_COOLDOWN_MINUTES} 分钟内已记录或推送。"
    else:
        log.push_status = "log_only"
        log.push_message = "未加入监控清单，只写入复盘日志，不触发提醒。"
    db.flush()
    return log


def refresh_compound_open_signal_reviews(db: Session, limit: int = 200) -> int:
    now = datetime.now(timezone.utc)
    logs = list(
        db.scalars(
            select(CryptoCompoundOpenSignalLog)
            .where(
                CryptoCompoundOpenSignalLog.opened_at <= now - timedelta(minutes=min(COMPOUND_OPEN_SIGNAL_REVIEW_MINUTES)),
                (
                    (CryptoCompoundOpenSignalLog.review_30m_spread_pct.is_(None))
                    | (CryptoCompoundOpenSignalLog.review_60m_spread_pct.is_(None))
                ),
            )
            .order_by(desc(CryptoCompoundOpenSignalLog.opened_at))
            .limit(limit)
        )
    )
    updated = 0
    for log in logs:
        opened_at = utc_datetime(log.opened_at)
        if opened_at is None or log.open_spread_pct is None:
            continue
        if log.review_30m_spread_pct is None and opened_at <= now - timedelta(minutes=30):
            spread, reviewed_at = review_compound_spread(db, log, 30)
            if spread is not None:
                log.review_30m_spread_pct = spread
                log.review_30m_convergence_pct = log.open_spread_pct - spread
                log.review_30m_at = reviewed_at
                updated += 1
        if log.review_60m_spread_pct is None and opened_at <= now - timedelta(minutes=60):
            spread, reviewed_at = review_compound_spread(db, log, 60)
            if spread is not None:
                log.review_60m_spread_pct = spread
                log.review_60m_convergence_pct = log.open_spread_pct - spread
                log.review_60m_at = reviewed_at
                updated += 1
        if log.review_60m_spread_pct is not None:
            log.status = "reviewed"
    return updated


def scan_compound_open_signals(db: Session, limit: int = 50) -> dict[str, Any]:
    if not COMPOUND_OPEN_SIGNAL_ENABLED:
        return compound_open_signals_overview(db, limit=limit, checked=0, created=0, auto_scan=False)
    refresh_compound_open_signal_reviews(db)
    quotes, updated_at = current_compound_snapshot_quotes(db)
    grouped: dict[str, list[MarketQuote]] = {}
    for quote in quotes:
        symbol = normalize_symbol(quote.symbol)
        if is_delisted_crypto_symbol(symbol):
            continue
        grouped.setdefault(symbol, []).append(quote)
    created = 0
    checked = 0
    for symbol, symbol_quotes in grouped.items():
        rows = build_pair_rows(symbol_quotes, symbol)
        for row in rows:
            if row.get("pairType") not in {"F", "S"}:
                continue
            opened_at = pair_opened_at(row, symbol_quotes)
            if opened_at is None:
                continue
            previous_spread = previous_compound_spread(db, row, opened_at)
            current_spread = row.get("bidSpreadPct")
            spread_jump = current_spread - previous_spread if isinstance(current_spread, (int, float)) and isinstance(previous_spread, (int, float)) else None
            reason = compound_open_signal_reason(row, previous_spread, spread_jump)
            checked += 1
            if not reason:
                continue
            if not compound_recent_spread_confirmed(db, row, opened_at):
                continue
            log = add_compound_open_signal_log(db, row, opened_at, previous_spread, spread_jump, reason)
            if log is not None:
                created += 1
    db.commit()
    return compound_open_signals_overview(db, limit=limit, updated_at=updated_at, checked=checked, created=created, auto_scan=False)


def run_compound_open_signal_background_scan(limit: int) -> None:
    from app.database import SessionLocal

    if not _compound_open_scan_lock.acquire(blocking=False):
        return
    started_at = datetime.now(timezone.utc)
    _compound_open_scan_state.update({"running": True, "startedAt": started_at, "finishedAt": _compound_open_scan_state.get("finishedAt")})
    db = SessionLocal()
    try:
        result = scan_compound_open_signals(db, limit=limit)
        _compound_open_scan_state.update(
            {
                "checkedCount": result.get("checkedCount"),
                "createdCount": result.get("createdCount"),
            }
        )
    except Exception:
        db.rollback()
    finally:
        db.close()
        finished_at = datetime.now(timezone.utc)
        _compound_open_scan_state.update({"running": False, "startedAt": started_at, "finishedAt": finished_at})
        _compound_open_scan_lock.release()


def start_compound_open_signal_background_scan(limit: int) -> bool:
    if not COMPOUND_OPEN_SIGNAL_ENABLED:
        return False
    state = dict(_compound_open_scan_state)
    if state.get("running"):
        return False
    finished_at = utc_datetime(state.get("finishedAt"))
    now = datetime.now(timezone.utc)
    if finished_at and (now - finished_at).total_seconds() < COMPOUND_OPEN_SIGNAL_SCAN_SECONDS:
        return False
    thread = threading.Thread(target=run_compound_open_signal_background_scan, args=(max(1, min(int(limit), 100)),), daemon=True)
    thread.start()
    return True


def compound_open_signals_overview(
    db: Session,
    limit: int = 50,
    updated_at: datetime | None = None,
    checked: int | None = None,
    created: int | None = None,
    auto_scan: bool = True,
) -> dict[str, Any]:
    if auto_scan and COMPOUND_OPEN_SIGNAL_ENABLED:
        start_compound_open_signal_background_scan(limit)
    refresh_compound_open_signal_reviews(db)
    state = dict(_compound_open_scan_state)
    logs = list(
        db.scalars(
            select(CryptoCompoundOpenSignalLog)
            .order_by(desc(CryptoCompoundOpenSignalLog.opened_at), desc(CryptoCompoundOpenSignalLog.id))
            .limit(max(1, min(int(limit), 100)))
        )
    )
    if any(log.review_30m_spread_pct is not None or log.review_60m_spread_pct is not None for log in logs):
        db.commit()
    return {
        "status": "ok",
        "updatedAt": updated_at or datetime.now(timezone.utc),
        "threshold": FF_SIGNAL_STRONG_THRESHOLD,
        "jumpThreshold": FF_SIGNAL_JUMP_THRESHOLD,
        "cooldownMinutes": COMPOUND_OPEN_SIGNAL_COOLDOWN_MINUTES,
        "enabled": COMPOUND_OPEN_SIGNAL_ENABLED,
        "scanSeconds": COMPOUND_OPEN_SIGNAL_SCAN_SECONDS,
        "scanning": bool(state.get("running")),
        "scanStartedAt": state.get("startedAt"),
        "scanFinishedAt": state.get("finishedAt"),
        "checkedCount": checked if checked is not None else state.get("checkedCount"),
        "createdCount": created if created is not None else state.get("createdCount"),
        "items": [compound_open_signal_to_out(log) for log in logs],
    }


def enrich_pair_ff_signals(db: Session, rows: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    if not rows:
        return rows
    enriched: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=FF_SIGNAL_LOOKBACK_MINUTES)
    for index, row in enumerate(rows):
        if index >= limit or row.get("pairType") != "F":
            enriched.append({**row, **pair_signal_empty()})
            continue
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        left_exchange = row.get("leftExchange")
        right_exchange = row.get("rightExchange")
        left_market = row.get("leftMarketType")
        right_market = row.get("rightMarketType")
        signal = pair_signal_empty()
        if not all(isinstance(value, str) for value in (left_exchange, right_exchange, left_market, right_market)):
            enriched.append({**row, **signal})
            continue
        snapshots = db.execute(
            select(
                CryptoMarketQuoteSnapshot.batch_time,
                CryptoMarketQuoteSnapshot.source,
                CryptoMarketQuoteSnapshot.exchange,
                CryptoMarketQuoteSnapshot.market_type,
                CryptoMarketQuoteSnapshot.best_bid,
                CryptoMarketQuoteSnapshot.best_ask,
                CryptoMarketQuoteSnapshot.funding_rate,
                CryptoMarketQuoteSnapshot.premium_rate,
            )
            .where(
                CryptoMarketQuoteSnapshot.symbol == symbol,
                CryptoMarketQuoteSnapshot.created_at >= since,
                CryptoMarketQuoteSnapshot.status == "ok",
                CryptoMarketQuoteSnapshot.exchange.in_([left_exchange, right_exchange]),
                CryptoMarketQuoteSnapshot.market_type.in_([left_market, right_market]),
                CryptoMarketQuoteSnapshot.source == "exchange_api",
            )
            .order_by(CryptoMarketQuoteSnapshot.batch_time)
        ).all()
        by_time: dict[tuple[datetime, str], dict[tuple[str, str], Any]] = {}
        for item in snapshots:
            by_time.setdefault((item[0], item[1]), {})[(item[2], item[3])] = item
        points: list[dict[str, Any]] = []
        for (batch_time, _source), items in by_time.items():
            left = items.get((left_exchange, left_market))
            right = items.get((right_exchange, right_market))
            if not left or not right:
                continue
            spread = pct_from_prices(left[5], right[4])
            if spread is None:
                continue
            points.append(
                {
                    "time": batch_time,
                    "spread": spread,
                    "fundingDiff": (right[6] - left[6]) if isinstance(right[6], (int, float)) and isinstance(left[6], (int, float)) else None,
                    "premiumDiff": (right[7] - left[7]) if isinstance(right[7], (int, float)) and isinstance(left[7], (int, float)) else None,
                }
            )
        points.sort(key=lambda item: item["time"])
        previous = points[-2] if len(points) >= 2 else None
        previous_spread = previous.get("spread") if previous else None
        current_spread = row.get("bidSpreadPct")
        funding_diff = None
        premium_diff = None
        if isinstance(row.get("rightFundingRate"), (int, float)) and isinstance(row.get("leftFundingRate"), (int, float)):
            funding_diff = row["rightFundingRate"] - row["leftFundingRate"]
        if isinstance(row.get("rightPremiumRate"), (int, float)) and isinstance(row.get("leftPremiumRate"), (int, float)):
            premium_diff = row["rightPremiumRate"] - row["leftPremiumRate"]
        if funding_diff is None and points:
            funding_diff = points[-1].get("fundingDiff")
        if premium_diff is None and points:
            premium_diff = points[-1].get("premiumDiff")
        level, reason = pair_signal_level(row, previous_spread, funding_diff, premium_diff)
        signal.update(
            {
                "previousSpreadPct": previous_spread,
                "spreadJumpPct": (current_spread - previous_spread) if isinstance(current_spread, (int, float)) and isinstance(previous_spread, (int, float)) else None,
                "crossedWatch": isinstance(previous_spread, (int, float)) and isinstance(current_spread, (int, float)) and previous_spread < FF_SIGNAL_WATCH_THRESHOLD <= current_spread,
                "crossedStrong": isinstance(previous_spread, (int, float)) and isinstance(current_spread, (int, float)) and previous_spread < FF_SIGNAL_STRONG_THRESHOLD <= current_spread,
                "fundingDiffPct": funding_diff,
                "premiumDiffPct": premium_diff,
                "ffSignalLevel": level,
                "ffSignalReason": reason,
            }
        )
        enriched.append({**row, **signal})
    return enriched


def enrich_margin_short_checks(rows: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    targets: list[tuple[str, str]] = []
    for row in rows[:limit]:
        if not row.get("marginShortRequired"):
            continue
        exchange = row.get("marginShortExchange")
        symbol = row.get("marginShortSymbol") or row.get("symbol")
        if isinstance(exchange, str) and isinstance(symbol, str) and (exchange, symbol) not in targets:
            targets.append((exchange, symbol))
    checks: dict[tuple[str, str], MarginShortCheck] = {}
    if targets:
        with ThreadPoolExecutor(max_workers=min(len(targets), MARGIN_SCAN_MAX_WORKERS)) as executor:
            future_map = {executor.submit(fetch_margin_short_check, exchange, symbol): (exchange, symbol) for exchange, symbol in targets}
            for future in as_completed(future_map):
                exchange, symbol = future_map[future]
                try:
                    checks[(exchange, symbol)] = future.result()
                except Exception as exc:
                    checks[(exchange, symbol)] = margin_short_exception_check(exchange, symbol, exc)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        exchange = row.get("marginShortExchange")
        symbol = row.get("marginShortSymbol") or row.get("symbol")
        if row.get("marginShortRequired") and isinstance(exchange, str) and isinstance(symbol, str):
            check = checks.get((exchange, symbol))
            if check:
                enriched.append({**row, **margin_short_fields(MarketQuote(exchange=exchange, symbol=symbol, market_type="spot"), MarketQuote(exchange=row["rightExchange"], symbol=symbol), check)})
                continue
        enriched.append(row)
    return enriched


def astro_exchange_and_market(raw_key: str) -> tuple[str, str] | None:
    key = raw_key.strip()
    if key.endswith("Future"):
        exchange_name = key[: -len("Future")]
        market_type = "futures"
    elif key.endswith("Spot"):
        exchange_name = key[: -len("Spot")]
        market_type = "spot"
    else:
        return None
    exchange = ASTRO_EXCHANGE_KEY_MAP.get(exchange_name.lower())
    if not exchange:
        return None
    return exchange, market_type


def astro_item_to_quote(exchange: str, market_type: str, item: dict[str, Any]) -> MarketQuote:
    rate = parse_float(item.get("rate"))
    rate_max = parse_float(item.get("rateMax"))
    period_hours = parse_float(item.get("rateInterval"))
    return MarketQuote(
        exchange=exchange,
        symbol=str(item.get("name") or ""),
        market_type=market_type,
        best_bid=parse_float(item.get("b")),
        best_ask=parse_float(item.get("a")),
        mark_price=parse_float(item.get("markPrice")),
        index_price=parse_float(item.get("indexPrice")),
        funding_rate=rate / 100 if rate is not None else None,
        premium_rate=computed_premium(parse_float(item.get("markPrice")), parse_float(item.get("indexPrice"))) if market_type == "futures" else None,
        period_hours=period_hours,
        max_funding_rate=rate_max / 100 if rate_max is not None else None,
        min_funding_rate=-(rate_max / 100) if rate_max is not None else None,
        volume_24h=parse_float(item.get("trade24Count")),
        updated_at=datetime.now(timezone.utc),
        status="ok",
    )


def fetch_astro_quotes(symbol: str) -> tuple[list[MarketQuote], str | None]:
    return [], "Astro 行情站接口已删除。"


def fetch_astro_payload_blocks() -> tuple[dict[str, Any], str | None]:
    return {}, "Astro 行情站接口已删除。"


def fetch_astro_all_quotes() -> tuple[list[MarketQuote], str | None]:
    blocks, warning = fetch_astro_payload_blocks()
    quotes: list[MarketQuote] = []
    for raw_key, block in blocks.items():
        parsed = astro_exchange_and_market(raw_key)
        if not parsed or not isinstance(block, dict):
            continue
        exchange, market_type = parsed
        if exchange not in BOARD_EXCHANGES or market_type not in board_market_types(exchange):
            continue
        for item in block.get("list") or []:
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("name") or "")
            if not raw_name.endswith("USDT"):
                continue
            quote = astro_item_to_quote(exchange, market_type, item)
            try:
                quote.symbol = normalize_symbol(raw_name)
            except ValueError:
                continue
            if is_delisted_crypto_symbol(quote.symbol):
                continue
            if market_quote_live_for_pair(quote):
                quotes.append(quote)
    return quotes, warning


def crypto_arbitrage_candidate_threshold_pct() -> float:
    return float(os.environ.get("CRYPTO_ARBITRAGE_CANDIDATE_SPREAD_PCT", "0.6"))


def crypto_arbitrage_trigger_threshold_pct() -> float:
    return float(os.environ.get("CRYPTO_ARBITRAGE_TRIGGER_SPREAD_PCT", "1.2"))


def group_quotes_by_symbol(quotes: list[MarketQuote]) -> dict[str, list[MarketQuote]]:
    grouped: dict[str, list[MarketQuote]] = {}
    for quote in quotes:
        symbol = normalize_symbol(quote.symbol)
        if is_delisted_crypto_symbol(symbol):
            continue
        grouped.setdefault(symbol, []).append(quote)
    return grouped


def symbol_max_arbitrage_spreads(quotes_by_symbol: dict[str, list[MarketQuote]]) -> dict[str, float]:
    spreads: dict[str, float] = {}
    for symbol, quotes in quotes_by_symbol.items():
        rows = build_pair_rows(quotes, symbol)
        values = [row.get("bidSpreadPct") for row in rows if isinstance(row.get("bidSpreadPct"), (int, float))]
        if values:
            spreads[symbol] = max(values)
    return spreads


def run_crypto_arbitrage_deposition(db: Session, tiers: list[str] | None = None) -> dict[str, Any]:
    requested_tiers = tiers or ["baseline"]
    now = datetime.now(timezone.utc)
    raw_quotes, warning = fetch_astro_all_quotes()
    filtered_quotes = apply_quote_volume_filter(raw_quotes)
    eligible_quotes = [quote for quote in filtered_quotes if quote.status == "ok"]
    quotes_by_symbol = group_quotes_by_symbol(eligible_quotes)
    spreads_by_symbol = symbol_max_arbitrage_spreads(quotes_by_symbol)
    candidate_threshold = crypto_arbitrage_candidate_threshold_pct()
    trigger_threshold = crypto_arbitrage_trigger_threshold_pct()
    candidate_symbols = {symbol for symbol, spread in spreads_by_symbol.items() if spread >= candidate_threshold}
    trigger_symbols = {symbol for symbol, spread in spreads_by_symbol.items() if spread >= trigger_threshold}

    tier_by_symbol: dict[str, str] = {}
    if "baseline" in requested_tiers:
        tier_by_symbol.update({symbol: "baseline" for symbol in quotes_by_symbol})
    if "candidate" in requested_tiers:
        tier_by_symbol.update({symbol: "candidate" for symbol in candidate_symbols})
    if "trigger" in requested_tiers:
        tier_by_symbol.update({symbol: "trigger" for symbol in trigger_symbols})

    persisted: dict[str, int] = {"baseline": 0, "candidate": 0, "trigger": 0}
    for tier in ("baseline", "candidate", "trigger"):
        tier_symbols = {symbol for symbol, selected_tier in tier_by_symbol.items() if selected_tier == tier}
        if not tier_symbols:
            continue
        tier_quotes = [quote for symbol in tier_symbols for quote in quotes_by_symbol.get(symbol, [])]
        persisted[tier] = persist_crypto_market_quote_rows(db, tier_quotes, now, f"astro_{tier}")

    total_persisted = sum(persisted.values())
    status = "ok" if total_persisted else "not_found"
    if warning:
        status = "partial_error" if total_persisted else "error"
    return {
        "status": status,
        "message": f"价差数据处理完成：{total_persisted} 行。",
        "warning": warning,
        "requested_tiers": requested_tiers,
        "raw_quote_count": len(raw_quotes),
        "eligible_quote_count": len(eligible_quotes),
        "symbol_count": len(quotes_by_symbol),
        "candidate_symbol_count": len(candidate_symbols),
        "trigger_symbol_count": len(trigger_symbols),
        "candidate_threshold_pct": candidate_threshold,
        "trigger_threshold_pct": trigger_threshold,
        "persisted": persisted,
        "updated_at": now,
    }


def astro_pair_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        row["symbol"],
        row["leftExchange"],
        row["leftMarketType"],
        row["rightExchange"],
        row["rightMarketType"],
    )


def crypto_astro_board(symbol: str, local_pairs: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    now = datetime.now(timezone.utc)
    if not astro_enabled():
        return {
            "astroStatus": "manual_only",
            "astroMessage": "Astro 行情站接口已删除。",
            "astroUpdatedAt": now,
            "astroPairs": [],
        }
    with _astro_cache_lock:
        cached = _astro_cache.get(normalized)
        if cached and (now - cached[0]).total_seconds() < ASTRO_CACHE_SECONDS:
            return dict(cached[1])
    try:
        quotes, warning = fetch_astro_quotes(normalized)
        pairs = build_pair_rows(quotes, normalized)
        local_by_key = {astro_pair_key(row): row for row in local_pairs}
        enriched_pairs: list[dict[str, Any]] = []
        for row in pairs:
            local = local_by_key.get(astro_pair_key(row))
            local_bid = local.get("bidSpreadPct") if local else None
            local_ask = local.get("askSpreadPct") if local else None
            enriched = {
                **row,
                "astroBidSpreadPct": row.get("bidSpreadPct"),
                "astroAskSpreadPct": row.get("askSpreadPct"),
                "localBidSpreadPct": local_bid,
                "localAskSpreadPct": local_ask,
                "spreadDiffPct": (row.get("bidSpreadPct") - local_bid) if isinstance(row.get("bidSpreadPct"), (int, float)) and isinstance(local_bid, (int, float)) else None,
                "localMatched": bool(local),
                "source": "行情站",
                "leftFundingRate": next((quote.funding_rate for quote in quotes if quote.exchange == row["leftExchange"] and quote.market_type == row["leftMarketType"]), None),
                "rightFundingRate": next((quote.funding_rate for quote in quotes if quote.exchange == row["rightExchange"] and quote.market_type == row["rightMarketType"]), None),
                "leftVolume24h": next((quote.volume_24h for quote in quotes if quote.exchange == row["leftExchange"] and quote.market_type == row["leftMarketType"]), None),
                "rightVolume24h": next((quote.volume_24h for quote in quotes if quote.exchange == row["rightExchange"] and quote.market_type == row["rightMarketType"]), None),
            }
            enriched_pairs.append(enriched)
        status = "ok" if pairs else "not_found"
        matched_count = len([row for row in enriched_pairs if row["localMatched"]])
        message = f"行情站已返回 {len(pairs)} 个机会，本地已复核 {matched_count} 个。" if pairs else "行情站没有返回当前币种的本地可复核机会。"
        if warning:
            status = "partial_error" if pairs else "error"
            message = f"{message} 部分行情站接口异常：{warning}"
        board = {
            "astroStatus": status,
            "astroMessage": message,
            "astroUpdatedAt": now,
            "astroPairs": enriched_pairs[:40],
        }
    except Exception as exc:
        board = {
            "astroStatus": "error",
            "astroMessage": f"行情站读取失败：{exc}",
            "astroUpdatedAt": now,
            "astroPairs": [],
        }
    with _astro_cache_lock:
        _astro_cache[normalized] = (now, dict(board))
    return board


def crypto_board_cache_key(symbol: str, codes: list[str], mappings: dict[tuple[str, str], dict[str, Any]]) -> str:
    return json.dumps(
        {
            "symbol": normalize_symbol(symbol),
            "codes": codes,
            "mappings": sorted(
                (
                    exchange,
                    market_type,
                    str(spec.get("mappedSymbol") or ""),
                    parse_float(spec.get("priceRatio")) or 1.0,
                )
                for (exchange, market_type), spec in mappings.items()
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def clear_crypto_board_cache() -> None:
    with _board_cache_lock:
        _board_cache.clear()


def empty_crypto_symbol_board(symbol: str, db: Session, message: str = "交易所行情板后台加载中。") -> dict[str, Any]:
    return {
        "selectedSymbol": normalize_symbol(symbol),
        "supportedExchanges": enabled_crypto_exchange_codes(db),
        "supportedMarketTypes": {code: list(board_market_types(code)) for code in enabled_crypto_exchange_codes(db)},
        "minQuoteVolume24hUsdt": min_quote_volume_24h_usdt(),
        "persistedQuoteCount": 0,
        "exchangeRows": [],
        "pairs": [],
        "boardStatus": "manual_only",
        "boardMessage": message,
        "boardUpdatedAt": None,
    }


def crypto_symbol_board(symbol: str, db: Session, force_refresh: bool = False) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if is_delisted_crypto_symbol(normalized):
        return empty_crypto_symbol_board(normalized, db, delisted_crypto_symbol_reason(normalized) or "下架币已排除。")
    codes = enabled_crypto_exchange_codes(db)
    mappings = symbol_mapping_specs(db, normalized)
    cache_key = crypto_board_cache_key(normalized, codes, mappings)
    now = datetime.now(timezone.utc)
    if not force_refresh:
        with _board_cache_lock:
            cached = _board_cache.get(cache_key)
            if cached and (now - cached[0]).total_seconds() < BOARD_CACHE_SECONDS:
                return dict(cached[1])
    quotes = fetch_symbol_quotes(normalized, codes, mappings)
    persisted_count = persist_crypto_market_quotes(db, normalized, quotes, now)
    futures_quotes = [quote for quote in quotes if quote.market_type == "futures"]
    base_exchange_rows = [quote_to_board_row(quote) for quote in futures_quotes]
    refresh_margin_short_checks_for_rows(base_exchange_rows)
    exchange_rows = enrich_exchange_borrow_checks(base_exchange_rows)
    pairs = sorted(
        enrich_pair_ff_signals(db, build_pair_rows(quotes, normalized)),
        key=lambda row: (ff_signal_rank(row), row.get("bidSpreadPct") if isinstance(row.get("bidSpreadPct"), (int, float)) else float("-inf")),
        reverse=True,
    )
    for pair in pairs:
        record_ff_compound_signal_event(db, pair)
    source_error_count = len([quote for quote in futures_quotes if quote.status not in {"ok", "low_volume", "unknown_volume"}])
    filtered_count = len([quote for quote in futures_quotes if quote.status in {"low_volume", "unknown_volume"}])
    if source_error_count == len(futures_quotes):
        status = "error"
        message = "交易所接口当前都不可用。"
    elif source_error_count:
        status = "partial_error"
        message = "部分交易所接口不可用，已展示可用交易所。"
    else:
        status = "ok"
        message = "加密货币跨所价差监控正常。"
    if filtered_count:
        message = f"{message} {filtered_count} 条合约行情因成交额低或未知未纳入监控。"
    if persisted_count:
        message = f"{message} 已沉淀 {persisted_count} 条本币行情。"
    updated_at = max((row["updatedAt"] for row in exchange_rows if row["updatedAt"]), default=None)
    board = {
        "selectedSymbol": normalized,
        "supportedExchanges": codes,
        "supportedMarketTypes": {code: list(board_market_types(code)) for code in codes},
        "minQuoteVolume24hUsdt": min_quote_volume_24h_usdt(),
        "persistedQuoteCount": persisted_count,
        "exchangeRows": exchange_rows,
        "pairs": pairs,
        "boardStatus": status,
        "boardMessage": message,
        "boardUpdatedAt": updated_at,
    }
    with _board_cache_lock:
        _board_cache[cache_key] = (now, dict(board))
    return board


def crypto_watchlist_overview(
    db: Session,
    symbol: str | None = None,
    include_board: bool = True,
    force_board: bool = False,
) -> dict[str, Any]:
    items = list(db.scalars(select(CryptoWatchItem).order_by(CryptoWatchItem.created_at)))
    rows = [crypto_item_to_out(db, item) for item in items]
    enabled_rows = [row for row in rows if row["enabled"]]
    board_symbol = normalize_symbol(symbol) if symbol else (rows[0]["symbol"] if rows else "BTC")
    updated_at = max((row["updatedAt"] for row in rows if row["updatedAt"]), default=None)
    if not rows:
        status = "not_configured"
        message = "还没有加密货币跟踪项。"
    elif not enabled_rows:
        status = "manual_only"
        message = "加密货币跟踪项已全部关闭。"
    elif any(row["status"] in {"partial_error", "error"} for row in enabled_rows):
        status = "partial_error"
        message = "部分交易所接口不可用，已保留可用交易所数据。"
    elif all(row["status"] == "ok" for row in enabled_rows):
        status = "ok"
        message = "加密货币价差监控正常。"
    else:
        status = "manual_only"
        message = "加密货币跟踪项等待刷新。"
    board = crypto_symbol_board(board_symbol, db, force_refresh=force_board) if include_board else empty_crypto_symbol_board(board_symbol, db)
    astro_board = crypto_astro_board(board_symbol, board.get("pairs") or []) if include_board else {
        "astroStatus": "manual_only",
        "astroMessage": "行情站对照后台加载中。",
        "astroUpdatedAt": None,
        "astroPairs": [],
    }
    response_status = board["boardStatus"] if include_board else status
    response_message = board["boardMessage"] if include_board else message
    response_updated_at = board["boardUpdatedAt"] or updated_at if include_board else updated_at
    return {
        "status": response_status,
        "source_status": response_status,
        "updated_at": response_updated_at,
        "items": rows,
        "message": response_message,
        **board,
        **astro_board,
    }

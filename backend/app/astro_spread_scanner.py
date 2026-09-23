from __future__ import annotations

import base64
import gzip
import json
import hashlib
import math
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed, wait
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.astro_card_registry import (
    auto_created_route_records,
    astro_cleanup_rearm_buffer_pct_points,
    astro_delete_pullback_pct_points,
    astro_delete_rearm_pct,
    astro_delete_rearm_status,
)
from app.astro_sdk import (
    ASTRO_FF_BUY_EXCHANGES,
    ASTRO_FF_DIRECT_EXCHANGES,
    ASTRO_FF_SELL_EXCHANGES,
    astro_ff_bybit_sell_exception_enabled,
    astro_ff_bybit_sell_exception,
    astro_fs_borrow_auto_card_enabled,
    astro_fs_borrow_min_cycle_profit_pct,
    astro_fs_borrow_min_open_spread_pct,
    astro_greater_price_alert_pct,
    astro_max_notional_usdt,
    astro_min_notional_usdt,
    astro_price_change_alert_only_rise,
    astro_price_change_alert_pct,
    astro_route_dedupe_state,
    astro_sdk_config,
    astro_spread_card_routes,
    build_astro_spread_pairs,
    schedule_astro_pairs,
)
from app.astro_spread_history import (
    ff_structure_assessment,
    ff_structure_history_status,
    record_ff_structure_history,
    structure_adverse_share,
    structure_filter_enabled,
    structure_history_hours,
    structure_max_five_minute_increase_pct,
    structure_sample_seconds,
    structure_tracking_min_pct,
)
from app.system_runtime_log import append_system_runtime_event
from app.astro_shared_reads import InflightReads
from app import astro_pulse_health
from app.astro_route_policy import RoutePolicy, LatestQueue, QueueBusy, category as route_fault_category, route_key, LABELS as ROUTE_FAULT_LABELS


def _astro_api_priority(function):
    """Give scanner/revalidation exchange calls the next shared API slot."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        # Lazy import avoids a module cycle: crypto also reads scanner status.
        from app.crypto import api_request_priority

        with api_request_priority("astro"):
            return function(*args, **kwargs)

    return wrapped


def _call_with_astro_api_priority(function, *args, **kwargs):
    """Propagate Astro priority into ThreadPoolExecutor child threads.

    ``threading.local`` state is intentionally not inherited by new threads.
    Without this wrapper, an Astro parent could wait for a child that the
    shared API limiter had classified as background work.
    """

    from app.crypto import api_request_priority

    with api_request_priority("astro"):
        return function(*args, **kwargs)


PULSE_URLS = (
    "https://pulse-lite-api.astro-btc.xyz/api/query/new",
    "https://pulse-api.astro-btc.xyz/api/query/second",
)
_UNSET_SETTING = object()
PULSE_PRIMARY_EXCHANGES = frozenset({"binance", "bybit", "bitget", "okx", "gate", "aster"})
PULSE_MARKETS = (
    ("binanceSpot", "binance", "Binance", "spot"),
    ("binanceFuture", "binance", "Binance", "future"),
    ("bybitSpot", "bybit", "Bybit", "spot"),
    ("bybitFuture", "bybit", "Bybit", "future"),
    ("bitgetSpot", "bitget", "Bitget", "spot"),
    ("bitgetFuture", "bitget", "Bitget", "future"),
    ("okxSpot", "okx", "OKX", "spot"),
    ("okxFuture", "okx", "OKX", "future"),
    ("gateSpot", "gate", "Gate", "spot"),
    ("gateFuture", "gate", "Gate", "future"),
    ("asterSpot", "aster", "Aster", "spot"),
    ("asterFuture", "aster", "Aster", "future"),
    ("kucoinSpot", "kucoin", "Kucoin", "spot"),
    ("kucoinFuture", "kucoin", "Kucoin", "future"),
    ("hlSpot", "hl", "Hyperliquid", "spot"),
    ("hlFuture", "hl", "Hyperliquid", "future"),
    ("bpSpot", "bp", "Backpack", "spot"),
    ("bpFuture", "bp", "Backpack", "future"),
    ("lighterFuture", "lighter", "Lighter", "future"),
    ("mexcSpot", "mexc", "Mexc", "spot"),
    ("mexcFuture", "mexc", "Mexc", "future"),
    ("okxdexSpot", "okxdex", "OKX DEX", "spot"),
    ("jupSpot", "jup", "Jupiter", "spot"),
    ("pancakeswapv3Spot", "pancakeswapv3", "PancakeSwapV3", "spot"),
    ("uniswapv3Spot", "uniswapv3", "UniswapV3", "spot"),
)
PULSE_MARKET_BY_KEY = {item[0].lower(): item for item in PULSE_MARKETS}
PULSE_MARKET_KEY_BY_ROUTE = {
    (exchange, market_type): key
    for key, exchange, _label, market_type in PULSE_MARKETS
}
DEFAULT_MARKETS = (
    "binanceSpot",
    "binanceFuture",
    "bybitSpot",
    "bybitFuture",
    "bitgetSpot",
    "bitgetFuture",
    "okxSpot",
    "okxFuture",
    "gateSpot",
    "gateFuture",
    "asterFuture",
    "hlFuture",
    "okxdexSpot",
)
SF_DEX_EXCHANGES = frozenset({"okxdex", "pancakeswapv3"})
SF_AUTO_CARD_SPOT_EXCHANGES = frozenset(
    {"binance", "bybit", "bitget", "okx", "gate", "okxdex", "pancakeswapv3"}
)
SF_OKXDEX_FUTURES_EXCHANGES = frozenset({"binance", "bybit", "bitget", "gate", "aster", "okx"})
SF_AUTO_CARD_EXCLUDED_FUTURES = frozenset({"hl"})
OKXDEX_CHAIN_LABELS = {
    "1": "Ethereum",
    "56": "BNB Smart Chain",
    "501": "Solana",
    "8453": "Base",
    "42161": "Arbitrum One",
    "4663": "Robinhood Chain",
}
# These symbols were already visible in the user's Astro DEX configuration
# before the local confirmation registry was introduced.  They are accepted
# only after the scanner independently verifies the Pulse chain and contract
# against the selected futures exchange.  New symbols must be confirmed with
# an exact chain + contract tuple from the local rules page.
LEGACY_CONFIRMED_ASTRO_DEX_SYMBOLS = (
    "GUA", "SKYAI", "DRAM", "SPCX", "牛来", "AAPLX", "BOT", "MARSC01N",
    "SNDK", "MU", "HOOD", "INTC", "MSTR", "ETHFI", "ANSEM", "CATE",
    "TUT", "FET", "VVV", "AAVE", "ENA", "LINK", "SYRUP", "PEPE",
)
BINANCE_NETWORK_CHAIN_INDEX = {
    "ETH": "1",
    "ETHEREUM": "1",
    "ERC20": "1",
    "ETHEREUMERC20": "1",
    "BSC": "56",
    "BEP20": "56",
    "BNBSMARTCHAIN": "56",
    "BNBSMARTCHAINBEP20": "56",
    "SOL": "501",
    "SOLANA": "501",
    "BASE": "8453",
    "ARBITRUM": "42161",
    "ARBITRUMONE": "42161",
}
TARGET_IDENTITY_SOURCE = {
    "binance": "Binance 官方资产网络表",
    "bybit": "Bybit 官方资产网络表",
    "bitget": "Bitget 官方资产网络表",
    "gate": "Gate 官方资产网络表",
    "aster": "Aster 官方资产网络表",
    "okx": "OKX 官方资产网络表",
}
_stop = threading.Event()
_thread: threading.Thread | None = None
_hot_thread: threading.Thread | None = None
_api_recovery_thread: threading.Thread | None = None
_hot_wake = threading.Event()
_state_lock = threading.Lock()
_hit_lock = threading.Lock()
_hot_lock = threading.Lock()
_subscription_lock = threading.Lock()
_gate_index_lock = threading.Lock()
_dex_identity_log_lock = threading.Lock()
_dex_asset_identity_lock = threading.Lock()
_pulse_source_log_lock = threading.Lock()
_pulse_outage_lock = threading.Lock()
_revalidation_state_lock = threading.Lock()
_decision_audit_lock = threading.Lock()
_depth_request_condition = threading.Condition(threading.Lock())
# Fault generations, depth incidents and recovery must transition atomically.
_depth_exchange_health_lock = threading.RLock()
_api_degraded_lock = _depth_exchange_health_lock
_gate_index_cache: dict[str, tuple[float, str | None]] = {}
_dex_identity_log_signature: str | None = None
_dex_identity_log_at = 0.0
_pulse_source_log_at = 0.0
_pulse_source_degraded_streak = 0
_pulse_source_degraded_started_at: datetime | None = None
_pulse_source_degraded_logged = False
_pulse_consecutive_failures = 0
_pulse_outage_started_at: datetime | None = None
_pulse_outage_last_summary_at = 0.0
_pulse_unhealthy_logged = False
_dex_asset_identity_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None, str | None]] = {}
_hits: dict[str, dict[str, Any]] = {}
_hot_routes: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
_hot_recent_hits: dict[tuple[str, str, str, str, str, str], float] = {}
_hot_recent_hit_at_ms: dict[tuple[str, str, str, str, str, str], int] = {}
_revalidation_failures: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
_decision_audit_last: dict[tuple[str, ...], float] = {}
_missed_opportunity_last: dict[tuple[str, ...], float] = {}
_direct_failure_streaks: dict[tuple[str, ...], dict[str, Any]] = {}
_decision_audit_summary: dict[str, Any] = {
    "startedAt": datetime.now(timezone.utc).isoformat(),
    "lastLoggedAtMonotonic": 0.0,
    "count": 0,
    "reasons": {},
    "samples": [],
    "quoteTimingSamples": [],
    "durationCount": 0,
    "durationMsTotal": 0.0,
    "durationMsMax": 0.0,
    "routeStats": {},
}
_depth_request_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any, dict[str, Any]]] = {}
_depth_request_errors: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, str]] = {}
_depth_request_inflight: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
_auxiliary_reads = InflightReads()
_api_transition_log_lock = threading.Lock()
_api_transition_log_windows: dict[str, dict[str, Any]] = {}
_depth_request_metrics: dict[str, Any] = {
    "startedAt": datetime.now(timezone.utc).isoformat(),
    "networkRequests": 0,
    "cacheHits": 0,
    "joinedInflight": 0,
    "http429": 0,
    "errors": 0,
    "expandedDepthRequests": 0,
    "circuitRejected": 0,
}
_depth_exchange_health: dict[str, dict[str, Any]] = {}
_api_degraded_route_failures: dict[tuple[str, ...], dict[str, Any]] = {}
_api_degraded_push_last: dict[str, float] = {}
_api_degraded_incidents: dict[str, dict[str, Any]] = {}
_api_recovery_probe_state: dict[str, dict[str, Any]] = {}
_api_notification_executor: ThreadPoolExecutor | None = None
_api_degraded_runtime: dict[str, Any] = {
    "fallbackCardCount": 0,
    "lastFallbackCardAt": None,
    "lastPushAt": None,
    "lastPushStatus": None,
    "lastPushMessage": None,
}
_revalidation_metrics: dict[str, Any] = {
    "startedAt": datetime.now(timezone.utc).isoformat(),
    "attempted": 0,
    "passed": 0,
    "failed": 0,
    "durationMsTotal": 0.0,
    "reasons": {},
    "lastSummaryAtMonotonic": time.monotonic(),
}
_state: dict[str, Any] = {
    "running": False,
    "lastScanAt": None,
    "lastScanDurationMs": None,
    "lastError": None,
    "marketCount": 0,
    "evaluatedRouteCount": 0,
    "retainedRouteCount": 0,
    "candidateCount": 0,
    "confirmedCount": 0,
    "structureBlockedCount": 0,
    "structureWarmingCount": 0,
    "dexIdentityVerifiedCount": 0,
    "dexIdentityBlockedCount": 0,
    "dexIdentityStatusCounts": {},
    "dexIdentityLastError": None,
    "dexMappingConfirmedCount": 0,
    "dexMappingMissingCount": 0,
    "dexMappingMissingItems": [],
    "pulseSourceSuccessCount": 0,
    "pulseSourceFailureCount": 0,
    "pulseSourceFailures": [],
    "pulseTransport": None,
    "pulseFallbackActive": False,
    "pulseHealthy": True,
    "pulseConsecutiveFailureCount": 0,
    "pulseOutageStartedAt": None,
    "pulseLastRecoveredAt": None,
    "cleanupPaused": False,
    "delistingRule": {
        "enabled": True,
        "activeBlockCount": 0,
        "filteredCandidateCount": 0,
        "items": [],
        "lastError": None,
    },
    "topCandidate": None,
    "hotMonitor": {
        "running": False,
        "routeCount": 0,
        "aboveThresholdRouteCount": 0,
        "newListingRouteCount": 0,
        "lastCheckAt": None,
        "lastHitAt": None,
        "lastError": None,
    },
}

_depth_backup_sources: dict[str, dict[str, Any]] = {}
_depth_probe_targets: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
_depth_cloud_metrics: dict[str, Any] = {"attempted": 0, "passedObservations": 0, "failedObservations": 0, "lastError": None}
_depth_cloud_route_slots = threading.BoundedSemaphore(2)
_local_depth_deadline_slots = threading.BoundedSemaphore(6)
_local_depth_deadline_executor: ThreadPoolExecutor | None = None
_route_policy = RoutePolicy()
_cloud_route_queue = LatestQueue()


def _local_depth_deadline_seconds() -> float:
    # End-to-end local route budget, not a per-socket timeout or failure count.
    return 3.0


def _cloud_backup_min_dwell_seconds() -> float:
    return _env_float("ASTRO_DEPTH_CLOUD_MIN_DWELL_SECONDS", 10.0, 0.0)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def spread_shared_depth_cache_seconds() -> float:
    """Briefly reuse identical books across routes in the same hot cycle."""

    return min(1.0, _env_float("ASTRO_SHARED_DEPTH_CACHE_SECONDS", 0.35, 0.05))


def spread_gate_depth_circuit_failure_threshold() -> int:
    return _env_int("ASTRO_GATE_DEPTH_CIRCUIT_FAILURES", 3, 2, 10)


def spread_gate_depth_circuit_pause_seconds() -> float:
    return min(5.0, _env_float("ASTRO_GATE_DEPTH_CIRCUIT_PAUSE_SECONDS", 1.0, 0.2))


def spread_api_degraded_failure_threshold() -> int:
    return _env_int("ASTRO_API_DEGRADED_FAILURES", 3, 2, 10)


def spread_api_degraded_push_interval_seconds() -> int:
    return _env_int("ASTRO_API_DEGRADED_PUSH_INTERVAL_SECONDS", 600, 60, 3600)


def spread_api_recovery_probe_interval_seconds() -> float:
    return min(30.0, _env_float("ASTRO_API_RECOVERY_PROBE_INTERVAL_SECONDS", 2.0, 0.5))


def spread_api_recovery_probe_successes() -> int:
    return _env_int("ASTRO_API_RECOVERY_PROBE_SUCCESSES", 2, 2, 5)


def _depth_exchange_for_url(url: str) -> str | None:
    hostname = str(httpx.URL(url).host or "").lower()
    if "gateio" in hostname:
        return "gate"
    if "binance" in hostname:
        return "binance"
    if "asterdex" in hostname:
        return "aster"
    if "bybit" in hostname:
        return "bybit"
    if "bitget" in hostname:
        return "bitget"
    if hostname.endswith("okx.com"):
        return "okx"
    return None


def _depth_symbol_from_params(params: dict[str, Any]) -> str | None:
    value = params.get("symbol") or params.get("contract") or params.get("instId")
    text = str(value or "").strip().upper()
    if not text:
        return None
    return text.replace("-USDT-SWAP", "").replace("_USDT", "").removesuffix("USDT")


def _depth_transport_failure(error: Exception) -> bool:
    text = str(error or "").lower()
    return any(token in text for token in (
        "timed out",
        "timeout",
        "ssl",
        "handshake",
        "connection",
        "connecterror",
        "network",
        "proxy",
        "name resolution",
        "nodename nor servname",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
        "短暂停用",
        "恢复探测",
        "expecting value",
        "invalid json",
    ))


class _GateDepthCircuitOpen(RuntimeError):
    pass


def _before_depth_request(exchange: str | None) -> bool:
    """Fail fast during a Gate incident and allow one half-open probe."""

    if exchange != "gate":
        return False
    now = time.monotonic()
    reject_reason: str | None = None
    with _depth_exchange_health_lock:
        state = _depth_exchange_health.get(exchange)
        if not state or not state.get("incidentOpen"):
            return False
        open_until = float(state.get("openUntilMonotonic") or 0.0)
        if now < open_until:
            reject_reason = "Gate深度接口短暂停用，等待独立恢复探测"
        elif state.get("probeInFlight"):
            reject_reason = "Gate深度接口正在进行独立恢复探测"
        else:
            state["probeInFlight"] = True
            return True
    with _depth_request_condition:
        _depth_request_metrics["circuitRejected"] = int(
            _depth_request_metrics.get("circuitRejected") or 0
        ) + 1
    raise _GateDepthCircuitOpen(reject_reason or "Gate深度接口短暂停用")


def _record_depth_request_failure(
    exchange: str | None,
    *,
    symbol: str | None,
    error: Exception,
    was_probe: bool,
) -> None:
    if exchange is None or not _depth_transport_failure(error):
        if exchange == "gate" and was_probe:
            with _depth_exchange_health_lock:
                state = _depth_exchange_health.get(exchange)
                if state:
                    state["probeInFlight"] = False
        return
    now = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()
    opened = False
    snapshot: dict[str, Any]
    with _depth_exchange_health_lock:
        state = _depth_exchange_health.setdefault(exchange, {})
        state["consecutiveFailures"] = int(state.get("consecutiveFailures") or 0) + 1
        state["failureCount"] = int(state.get("failureCount") or 0) + 1
        state["lastFailureAt"] = now_iso
        state["lastError"] = str(error)[:800]
        affected = state.setdefault("affectedSymbols", set())
        if symbol:
            affected.add(symbol)
        incident_threshold = spread_gate_depth_circuit_failure_threshold()
        if was_probe or int(state["consecutiveFailures"]) >= incident_threshold:
            if not state.get("incidentOpen"):
                state["incidentOpen"] = True
                state["incidentStartedAt"] = now_iso
                state["incidentStartedMonotonic"] = now
                opened = True
            if exchange == "gate":
                state["openUntilMonotonic"] = now + spread_gate_depth_circuit_pause_seconds()
        state["probeInFlight"] = False
        _reset_api_recovery_streak_locked(exchange)
        snapshot = {
            **state,
            "affectedSymbols": sorted(affected),
        }
    if opened:
        display_exchange = exchange.capitalize()
        append_system_runtime_event(
            "astro_exchange_depth_unavailable",
            level="warning",
            source="backend",
            module="astro_spread_scanner",
            message=(
                "Gate 深度接口连续失败，已短暂停用并等待独立恢复探测"
                if exchange == "gate"
                else f"{display_exchange} 深度接口连续失败，复核暂不可用"
            ),
            details={
                "exchange": exchange,
                "startedAt": snapshot.get("incidentStartedAt"),
                "consecutiveFailureCount": snapshot.get("consecutiveFailures"),
                "failureCount": snapshot.get("failureCount"),
                "affectedSymbols": snapshot.get("affectedSymbols"),
                "pauseSeconds": spread_gate_depth_circuit_pause_seconds() if exchange == "gate" else None,
                "errorCategory": _decision_error_category(error),
                "error": str(error)[:800],
            },
        )


def _record_depth_request_success(
    exchange: str | None, *, was_probe: bool, recovery_confirmed: bool = False
) -> None:
    if exchange is None:
        return
    recovered: dict[str, Any] | None = None
    now = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _depth_exchange_health_lock:
        state = _depth_exchange_health.get(exchange)
        if not state:
            return
        if state.get("incidentOpen") and not recovery_confirmed:
            # One successful quote (including Gate's half-open request) is not
            # the independent two-probe recovery confirmation.
            state["probeInFlight"] = False
            state["consecutiveFailures"] = 0
            return
        if state.get("incidentOpen"):
            started = float(state.get("incidentStartedMonotonic") or now)
            recovered = {
                "exchange": exchange,
                "startedAt": state.get("incidentStartedAt"),
                "recoveredAt": now_iso,
                "durationSeconds": round(max(0.0, now - started), 3),
                "failureCount": int(state.get("failureCount") or 0),
                "affectedSymbols": sorted(state.get("affectedSymbols") or []),
                "recoveredByProbe": bool(was_probe),
            }
        _depth_exchange_health.pop(exchange, None)
    if recovered is not None:
        display_exchange = exchange.capitalize()
        append_system_runtime_event(
            "astro_exchange_depth_recovered",
            level="info",
            source="backend",
            module="astro_spread_scanner",
            message=f"{display_exchange} 深度接口已恢复",
            details=recovered,
        )


def _scanner_public_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Use the process-wide exchange limiter without blindly retrying a quote.

    A 429 updates the shared backoff used by Astro, borrow scans and the rest of
    the backend.  The current quote attempt still fails closed: retrying later
    must be a new hot-monitor observation, not an implicit duplicate request.
    """

    if getattr(client, "cloud_depth", False):
        # Separate machine, connection, worker and exchange-IP rate budget.
        return client.get(url, params=params)

    from app.crypto import (
        api_rate_bucket_for_url,
        api_request_remaining_seconds,
        api_request_timeout_kwargs,
        defer_api_rate_limit,
        retry_after_seconds,
        wait_api_rate_limit,
    )

    bucket = api_rate_bucket_for_url(url)
    wait_api_rate_limit(bucket)
    response = client.get(url, params=params, **api_request_timeout_kwargs(client))
    api_request_remaining_seconds()
    if getattr(response, "status_code", 200) in {418, 429}:
        delay = retry_after_seconds(response) or 5.0
        defer_api_rate_limit(bucket, delay)
        with _depth_request_condition:
            _depth_request_metrics["http429"] = int(_depth_request_metrics.get("http429") or 0) + 1
    return response


def _shared_depth_json_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Single-flight and briefly cache an identical public depth request."""

    cloud = bool(getattr(client, "cloud_depth", False))
    key = (("cloud:" + url) if cloud else url, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    exchange = _depth_exchange_for_url(url)
    if not cloud and exchange:
        with _api_degraded_lock:
            targets = _depth_probe_targets.setdefault(exchange, {})
            # Track both spot and futures endpoint families through the same
            # actual proxy. A time/ping endpoint cannot prove depth is usable.
            family = url + ":" + str(params.get("category") or ("SWAP" if "SWAP" in str(params.get("instId")) else ""))
            targets[family] = (url, dict(params))
    symbol = _depth_symbol_from_params(params)
    from app.crypto import api_request_remaining_seconds
    budget_remaining = api_request_remaining_seconds()
    deadline = time.monotonic() + min(8.0, budget_remaining) if budget_remaining is not None else time.monotonic() + 8.0
    joined_inflight = False
    with _depth_request_condition:
        while True:
            now = time.monotonic()
            api_request_remaining_seconds()
            cached = _depth_request_cache.get(key)
            if (cached and now < cached[0]
                    and int(cached[2].get("receivedAt") or 0) >= int(getattr(client, "depth_round_started_ms", 0))
                    and int(cached[2].get("requestStartedAt") or 0) >= int(getattr(client, "depth_refresh_started_ms", 0))):
                _depth_request_metrics["cacheHits"] = int(_depth_request_metrics.get("cacheHits") or 0) + 1
                return cached[1], {
                    **cached[2],
                    "cacheHit": True,
                    "joinedInflight": joined_inflight,
                }
            failed = _depth_request_errors.get(key)
            if failed and now < failed[0]:
                raise RuntimeError(f"共享深度请求暂不可用：{failed[1]}")
            if key not in _depth_request_inflight:
                _depth_request_inflight.add(key)
                break
            if not joined_inflight:
                _depth_request_metrics["joinedInflight"] = int(
                    _depth_request_metrics.get("joinedInflight") or 0
                ) + 1
                joined_inflight = True
            remaining = deadline - now
            if remaining <= 0:
                raise RuntimeError("等待同币种共享深度请求超时")
            _depth_request_condition.wait(timeout=min(remaining, 1.0))

    started_ms = int(time.time() * 1000)
    was_probe = False
    try:
        was_probe = _before_depth_request(exchange) if not cloud else False
        response = _scanner_public_get(client, url, params=params)
        local_received_ms = int(time.time() * 1000)
        response.raise_for_status()
        payload = response.json()
        origin_received_ms = int(getattr(response, "headers", {}).get("x-depth-received-at", local_received_ms))
        metadata = {
            "requestStartedAt": started_ms,
            "receivedAt": local_received_ms,
            "localReceivedAt": local_received_ms,
            "originReceivedAt": origin_received_ms,
            "requestDurationMs": round(max(0, local_received_ms - started_ms), 1),
            "cacheHit": False,
            "joinedInflight": False,
            "transport": "tencent_cloud" if cloud else "local_proxy",
        }
    except Exception as exc:
        circuit_rejected = isinstance(exc, _GateDepthCircuitOpen)
        error_text = (
            str(exc)
            if circuit_rejected or exchange is None
            else f"{exchange}深度请求失败：{exc}"
        )
        with _depth_request_condition:
            if not circuit_rejected:
                _depth_request_metrics["errors"] = int(_depth_request_metrics.get("errors") or 0) + 1
            _depth_request_errors[key] = (time.monotonic() + 0.5, error_text)
            _depth_request_inflight.discard(key)
            _depth_request_condition.notify_all()
        if not circuit_rejected and not cloud:
            _record_depth_request_failure(
                exchange,
                symbol=symbol,
                error=exc,
                was_probe=was_probe,
            )
        if error_text == str(exc):
            raise
        raise RuntimeError(error_text) from exc

    if not cloud:
        _record_depth_request_success(exchange, was_probe=was_probe)
    with _depth_request_condition:
        _depth_request_metrics["networkRequests"] = int(
            _depth_request_metrics.get("networkRequests") or 0
        ) + 1
        _depth_request_cache[key] = (
            time.monotonic() + spread_shared_depth_cache_seconds(),
            payload,
            metadata,
        )
        _depth_request_errors.pop(key, None)
        _depth_request_inflight.discard(key)
        # Bound old per-symbol entries during long-running listing monitors.
        if len(_depth_request_cache) > 2_000:
            cutoff = time.monotonic()
            for old_key, old_value in list(_depth_request_cache.items()):
                if old_value[0] < cutoff:
                    _depth_request_cache.pop(old_key, None)
                    _depth_request_errors.pop(old_key, None)
        _depth_request_condition.notify_all()
    return payload, metadata


def _depth_request_control_status() -> dict[str, Any]:
    with _depth_exchange_health_lock:
        exchange_health = {
            exchange: {
                **state,
                "affectedSymbols": sorted(state.get("affectedSymbols") or []),
                "openUntilMonotonic": None,
                "incidentStartedMonotonic": None,
            }
            for exchange, state in _depth_exchange_health.items()
        }
    with _depth_request_condition:
        return {
            **dict(_depth_request_metrics),
            "cacheSeconds": spread_shared_depth_cache_seconds(),
            "inflightCount": len(_depth_request_inflight),
            "cachedBookCount": len(_depth_request_cache),
            "sharedAuxiliaryReads": _auxiliary_reads.snapshot(),
            "timestampRepair": _timestamp_repair_status(),
            "policy": (
                f"普通CEX读取前20档并模拟{spread_cex_quote_notional_usdt():g} USDT同数量成交；"
                "OKXDEX按实际询价数量读取合约深度并在必要时扩档；"
                "同交易所/币种/档位单飞去重"
            ),
            "gateCircuit": {
                "failureThreshold": spread_gate_depth_circuit_failure_threshold(),
                "pauseSeconds": spread_gate_depth_circuit_pause_seconds(),
                "state": exchange_health.get("gate"),
            },
        }


def _pulse_dual_source_healthy(*, now_ms: int | None = None) -> tuple[bool, dict[str, Any]]:
    """Require a fresh successful frame from every configured Pulse source."""

    now_ms = now_ms or int(time.time() * 1000)
    with _state_lock:
        current = dict(_state)
    last_scan = current.get("lastScanAt")
    try:
        parsed = datetime.fromisoformat(str(last_scan).replace("Z", "+00:00"))
        scan_age = max(0.0, now_ms / 1000 - parsed.timestamp())
    except (TypeError, ValueError):
        scan_age = math.inf
    max_age = max(15.0, float(spread_scan_interval_seconds() * 3))
    report = {
        "configuredCount": len(PULSE_URLS),
        "successCount": int(current.get("pulseSourceSuccessCount") or 0),
        "failureCount": int(current.get("pulseSourceFailureCount") or 0),
        "healthy": bool(current.get("pulseHealthy", True)),
        "lastScanAt": last_scan,
        "scanAgeSeconds": None if not math.isfinite(scan_age) else round(scan_age, 3),
        "maxAgeSeconds": max_age,
    }
    eligible = bool(
        report["healthy"]
        and report["successCount"] == len(PULSE_URLS)
        and report["failureCount"] == 0
        and scan_age <= max_age
    )
    return eligible, report


def _api_degraded_transport_report(report: dict[str, Any]) -> bool:
    return bool(
        str(report.get("reason") or "") in {
            "local_depth_deadline_exceeded",
            "cex_executable_depth_unavailable",
            "direct_quote_unavailable",
            "direct_funding_unavailable",
            "okxdex_executable_quote_unavailable",
        }
        and _depth_transport_failure(RuntimeError(str(report.get("error") or "")))
    )


def _api_degraded_fault_sources(pair: dict[str, Any], report: dict[str, Any]) -> list[str]:
    route_exchanges = {
        str(pair.get("buyEx") or "").strip().lower(),
        str(pair.get("sellEx") or "").strip().lower(),
    }
    explicit = sorted(set(report.get("failureSources") or []) & route_exchanges)
    if explicit:
        return explicit
    with _depth_exchange_health_lock:
        opened = {
            exchange for exchange, state in _depth_exchange_health.items()
            if state.get("incidentOpen") and exchange in route_exchanges
        }
    if opened:
        return sorted(opened)
    error = str(report.get("error") or "").lower()
    named = sorted(exchange for exchange in route_exchanges if exchange and exchange in error)
    # When a shared proxy error does not mention the destination by name, keep
    # both route exchanges.  Each can then be recovered by its own official
    # lightweight endpoint instead of leaving an unprobeable generic incident.
    return named or sorted(exchange for exchange in route_exchanges if exchange) or ["proxy/api"]


_API_RECOVERY_PROBE_ENDPOINTS: dict[str, str] = {
    "binance": "https://fapi.binance.com/fapi/v1/ping",
    "bybit": "https://api.bybit.com/v5/market/time",
    "bitget": "https://api.bitget.com/api/v2/public/time",
    "okx": "https://www.okx.com/api/v5/public/time",
    "gate": "https://api.gateio.ws/api/v4/spot/time",
    "aster": "https://fapi.asterdex.com/fapi/v1/ping",
}


def _reset_api_recovery_streak_locked(source: str) -> None:
    """Caller holds the shared fault lock; invalidate in-flight old probes."""

    state = _api_recovery_probe_state.setdefault(source, {})
    state["generation"] = int(state.get("generation") or 0) + 1
    state["consecutiveSuccesses"] = 0
    state["healthySinceMonotonic"] = None
    state["recoveryConfirmedAt"] = None


def _validate_api_recovery_payload(source: str, payload: Any) -> None:
    """HTTP 200 alone is not evidence of a working exchange API."""

    if not isinstance(payload, dict):
        raise ValueError("恢复探针未返回交易所 JSON 对象")
    if source in {"binance", "aster"}:
        if payload != {}:
            raise ValueError("恢复探针 ping 返回异常")
        return
    timestamp: Any = None
    if source == "gate" and not payload.get("label"):
        timestamp = payload.get("server_time")
    elif source == "bybit" and payload.get("retCode") == 0:
        result = payload.get("result")
        if isinstance(result, dict):
            seconds = _finite(result.get("timeSecond"))
            timestamp = seconds * 1000 if seconds is not None else None
    elif source == "bitget" and payload.get("code") == "00000":
        data = payload.get("data")
        if isinstance(data, dict):
            timestamp = data.get("serverTime")
    elif source in {"okx", "okxdex"} and payload.get("code") == "0":
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            timestamp = data[0].get("ts")
    server_ms = _finite(timestamp)
    if server_ms is None or abs(server_ms - time.time() * 1000) > 60_000:
        raise ValueError("恢复探针业务状态异常、缺少服务器时间或响应已陈旧")


def _active_api_recovery_sources() -> list[str]:
    with _api_degraded_lock:
        degraded = set(_api_degraded_incidents)
    with _depth_exchange_health_lock:
        depth = {
            exchange for exchange, state in _depth_exchange_health.items()
            if state.get("incidentOpen")
        }
    return sorted((degraded | depth) & set(_API_RECOVERY_PROBE_ENDPOINTS))


def _default_depth_probe_target(source: str) -> tuple[str, dict[str, Any]]:
    targets = {
        "binance": ("https://fapi.binance.com/fapi/v1/depth", {"symbol": "BTCUSDT", "limit": 5}),
        "aster": ("https://fapi.asterdex.com/fapi/v1/depth", {"symbol": "BTCUSDT", "limit": 5}),
        "bybit": ("https://api.bybit.com/v5/market/orderbook", {"category": "linear", "symbol": "BTCUSDT", "limit": 5}),
        "bitget": ("https://api.bitget.com/api/v2/mix/market/orderbook", {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "limit": 5}),
        "okx": ("https://www.okx.com/api/v5/market/books", {"instId": "BTC-USDT-SWAP", "sz": 5}),
        "gate": ("https://api.gateio.ws/api/v4/futures/usdt/order_book", {"contract": "BTC_USDT", "limit": 5, "with_id": "true"}),
    }
    if source not in targets:
        raise RuntimeError("该市场必须由真实询价确认恢复，通用ping不能代替")
    return targets[source]


def _validate_depth_probe_payload(source: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("恢复探针未返回真实盘口JSON")
    data = payload
    if source == "bybit":
        if payload.get("retCode") != 0:
            raise ValueError("Bybit盘口返回错误码")
        data = payload.get("result") or {}
        bids, asks, native = data.get("b"), data.get("a"), data.get("ts")
    else:
        if source == "bitget":
            if payload.get("code") != "00000":
                raise ValueError("Bitget盘口返回错误码")
            data = payload.get("data") or {}
        elif source == "okx":
            if payload.get("code") != "0" or not payload.get("data"):
                raise ValueError("OKX盘口返回错误码")
            data = payload["data"][0]
        elif payload.get("label") or payload.get("code") is not None:
            raise ValueError("交易所盘口返回错误码")
        bids, asks = data.get("bids"), data.get("asks")
        native = _gate_depth_timestamps(data)[0] if source == "gate" else (
            data.get("ts") or data.get("E") or data.get("T") or data.get("update")
        )
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise ValueError("恢复探针盘口为空")
    bid, bid_size = _depth_level_values(bids[0])
    ask, ask_size = _depth_level_values(asks[0])
    if any(value is None or value <= 0 for value in (bid, ask, bid_size, ask_size)) or bid > ask:
        raise ValueError("恢复探针盘口价格或数量无效")
    if native is not None:
        age = (time.time() * 1000 - _timestamp_ms(native, 0)) / 1000
        if age > 3 or age < -1:
            raise ValueError("恢复探针盘口陈旧或时钟异常")
    elif source not in {"binance", "aster"}:
        raise ValueError("恢复探针盘口缺少交易所时间")


def _api_recovery_probe_once(source: str) -> tuple[bool, str | None, float]:
    """Require real, fresh books through the same local proxy, not ping alone."""

    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=2.0,
            trust_env=True,
            headers={"Accept": "application/json", "User-Agent": "stock-review-mac/api-recovery-probe"},
        ) as client:
            with _api_degraded_lock:
                targets = list(_depth_probe_targets.get(source, {}).values())
            if not targets:
                targets = [_default_depth_probe_target(source)]
            def probe_target(target):
                url, saved_params = target
                params = dict(saved_params)
                if "limit" in params:
                    params["limit"] = 5
                if "sz" in params:
                    params["sz"] = 5
                response = client.get(url, params=params)
                response.raise_for_status()
                _validate_depth_probe_payload(source, response.json())
            with ThreadPoolExecutor(max_workers=min(3, len(targets))) as executor:
                list(executor.map(probe_target, targets[:3]))
            if time.monotonic() - started > 2.0:
                raise RuntimeError("本机真实盘口恢复探测耗时超过2秒")
        return True, None, round((time.monotonic() - started) * 1000, 1)
    except Exception as exc:
        return False, str(exc)[:800], round((time.monotonic() - started) * 1000, 1)


def _record_api_recovery_probe_result(
    source: str,
    *,
    success: bool,
    error: str | None,
    duration_ms: float,
    generation: int | None = None,
) -> bool:
    """Return True only when consecutive successes confirm recovery."""

    now_iso = datetime.now(timezone.utc).isoformat()
    required = spread_api_recovery_probe_successes()
    with _api_degraded_lock:
        state = _api_recovery_probe_state.setdefault(source, {
            "attemptCount": 0,
            "consecutiveSuccesses": 0,
        })
        if generation is not None and generation != int(state.get("generation") or 0):
            return False
        depth_incident = (_depth_exchange_health.get(source) or {}).get("incidentOpen")
        if source not in _api_degraded_incidents and not depth_incident:
            return False
        state["attemptCount"] = int(state.get("attemptCount") or 0) + 1
        state["lastProbeAt"] = now_iso
        state["lastProbeDurationMs"] = duration_ms
        now_mono = time.monotonic()
        if success:
            if state.get("healthySinceMonotonic") is None or now_mono-state.get("lastHealthyProbeMonotonic", now_mono)>6:
                state["healthySinceMonotonic"] = now_mono
            state["lastHealthyProbeMonotonic"] = now_mono
            state["consecutiveSuccesses"] = int(state.get("consecutiveSuccesses") or 0) + 1
            state["lastProbeError"] = None
        else:
            state["healthySinceMonotonic"] = None
            state["consecutiveSuccesses"] = 0
            state["lastProbeError"] = error
        snapshot = dict(state)
        incident = _api_degraded_incidents.get(source)
        if incident is not None:
            incident.update({
                "probeAttempts": snapshot["attemptCount"],
                "consecutiveProbeSuccesses": snapshot["consecutiveSuccesses"],
                "lastProbeAt": now_iso,
                "lastProbeDurationMs": duration_ms,
                "lastProbeError": error,
            })
        backup = _depth_backup_sources.get(source)
        dwell_ok = not backup or time.monotonic() - backup["startedMonotonic"] >= _cloud_backup_min_dwell_seconds()
        stable = success and snapshot.get("healthySinceMonotonic") is not None and now_mono-snapshot["healthySinceMonotonic"]>=10
        confirmed = stable and int(snapshot["consecutiveSuccesses"]) >= required and dwell_ok
        recovered_incident = _api_degraded_incidents.pop(source, None) if confirmed else None
        if confirmed:
            _depth_backup_sources.pop(source, None)
            state["recoveryConfirmedAt"] = now_iso
            state["lastRecoverySuccessCount"] = snapshot["consecutiveSuccesses"]
            state["consecutiveSuccesses"] = 0
            # The same lock protects new failures and clearing the depth fault.
            if depth_incident:
                _record_depth_request_success(source, was_probe=True, recovery_confirmed=True)

    if not confirmed:
        return False

    _route_policy.source_recovered(source)

    if recovered_incident is not None and not depth_incident:
        _log_api_channel_transition(
            "astro_api_revalidation_fault_recovered",
            level="info",
            source="backend",
            module="astro_spread_scanner",
            message=f"{source} API复核已恢复",
            details={
                **recovered_incident,
                "affectedSymbols": sorted(recovered_incident.get("affectedSymbols") or []),
                "recoveredAt": now_iso,
                "probeAttempts": snapshot["attemptCount"],
                "requiredConsecutiveSuccesses": required,
                "lastProbeDurationMs": duration_ms,
                "durationSeconds": max(0.0, (
                    datetime.fromisoformat(now_iso)
                    - datetime.fromisoformat(recovered_incident["startedAt"])
                ).total_seconds()),
            },
        )
    return True


def _api_recovery_probe_loop() -> None:
    while not _stop.is_set():
        round_started = time.monotonic()
        sources = _active_api_recovery_sources()
        if sources:
            with _api_degraded_lock:
                generations = {
                    source: int((_api_recovery_probe_state.get(source) or {}).get("generation") or 0)
                    for source in sources
                }
            with ThreadPoolExecutor(max_workers=len(sources)) as executor:
                futures = {
                    executor.submit(_api_recovery_probe_once, source): source
                    for source in sources
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        success, error, duration_ms = future.result()
                    except Exception as exc:
                        success, error, duration_ms = False, str(exc), 0.0
                    _record_api_recovery_probe_result(
                        source,
                        success=success,
                        error=error,
                        duration_ms=duration_ms,
                        generation=generations[source],
                    )
        _queue_route_alerts()
        _flush_api_channel_transition_logs()
        _stop.wait(max(0.0, spread_api_recovery_probe_interval_seconds() - (time.monotonic() - round_started)))


def _flush_api_channel_transition_logs() -> None:
    summaries = []
    now = time.monotonic()
    with _api_transition_log_lock:
        for event, window in _api_transition_log_windows.items():
            if window["count"] and now - window["started"] >= 300:
                summaries.append({"transitionEvent": event, "windowSeconds": round(now-window["started"], 1),
                                  "count": window["count"], "samples": list(window["samples"])})
                window.update(started=now, count=0, samples=[])
    for summary in summaries:
        append_system_runtime_event("astro_api_channel_transition_summary", level="info", module="astro_spread_scanner",
            message=f"API通道切换/恢复汇总：{summary['count']}次状态变化", details=summary)


def _log_api_channel_transition(event: str, **entry: Any) -> None:
    """Keep the first event, then aggregate routine transitions for five minutes.

    This changes diagnostic volume only, never route selection or push policy.
    The recovery loop flushes the tail even when no new transition arrives.
    """
    _flush_api_channel_transition_logs()
    now = time.monotonic()
    immediate = False
    with _api_transition_log_lock:
        window = _api_transition_log_windows.get(event)
        if window is None or (not window["count"] and now-window["started"] >= 300):
            _api_transition_log_windows[event] = {"started": now, "count": 0, "samples": []}
            immediate = True
        else:
            window["count"] += 1
            details = entry.get("details") or {}
            local_failure = details.get("localFailure") or {}
            sample = {"route": details.get("route"), "source": details.get("source"),
                      "sources": details.get("sources"), "symbol": details.get("symbol"),
                      "affectedSymbols": list(details.get("affectedSymbols") or [])[:20],
                      "reason": local_failure.get("reason"),
                      "error": str(local_failure.get("error") or details.get("lastError") or "")[:350]}
            if len(window["samples"]) < 20 and sample not in window["samples"]:
                window["samples"].append(sample)
    if immediate:
        append_system_runtime_event(event, **entry)


def _notify_api_degraded_fault(**kwargs):
    """Legacy per-source notifications are disabled; only route summaries send."""
    return {"attempted": False, "muted": True, "sources": kwargs.get("sources", [])}


def _queue_route_alerts():
    global _api_notification_executor
    action = _route_policy.tick()
    if action is None:
        return
    with _api_degraded_lock:
        if _api_notification_executor is None:
            _api_notification_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astro-fault-notification")
        _api_degraded_runtime.update(lastPushStatus="queued", lastPushMessage="汇总提醒已进入独立发送队列")
        _api_notification_executor.submit(_send_route_summary, action)


def _send_route_summary(action):
    payload = _route_policy.prepare(action)
    if not payload:
        return
    kind = action["kind"]
    groups = {}
    for row in payload:
        label = ROUTE_FAULT_LABELS.get(row.get("category"), "复核异常")
        groups[label] = groups.get(label, 0) + 1
    routes = "、".join(item["route"].replace("|", "/") for item in payload[:5])
    if kind == "fault":
        title = "Astro 复核异常汇总"
        body = ("；".join(f"{name} {count}条" for name,count in groups.items())
                + f"；持续影响复核至少20秒。涉及{len(payload)}条路线，本轮不建卡，其他路线继续。{routes}")
    else:
        title = "Astro 复核已恢复"
        body = f"{len(payload)}条已提醒路线连续有效复核稳定10秒，恢复正常判断。{routes}"
    try:
        from app.notifications import send_bark_or_log
        status, message = send_bark_or_log(True, title, body, "Astro扫描故障", None, "Bark 未配置", trust_env=False)
    except Exception as exc:
        status, message = "error", str(exc)
    _route_policy.delivered(action, status == "ok")
    with _api_degraded_lock:
        _api_degraded_runtime.update(lastPushAt=datetime.now(timezone.utc).isoformat(),
                                    lastPushStatus=status, lastPushMessage=message)
    append_system_runtime_event(
        "astro_route_fault_summary_notified" if kind == "fault" else "astro_route_recovery_summary_notified",
        level="warning" if kind == "fault" else "info", source="backend", module="astro_spread_scanner",
        message=f"{title}：{len(payload)}条路线；发送状态{status}",
        details={"kind":kind, "categories":groups, "routes":payload[:30], "affectedRouteCount":len(payload),
                 "pushStatus":status, "pushMessage":message, "globalPushIntervalSeconds":600},
    )


def _register_api_degraded_failure(identity, item, report):
    """Compatibility hook only. API outages never manufacture eligible cards."""
    return None, {"eligible": False, "reason": "api_unverified_card_disabled", "failedReport": report}


def _reset_api_degraded_route(identity: tuple[str, ...]) -> None:
    with _api_degraded_lock:
        _api_degraded_route_failures.pop(identity, None)


def _api_degraded_status() -> dict[str, Any]:
    from app.astro_depth_transport import status as cloud_status
    cloud_health = cloud_status()
    with _depth_exchange_health_lock:
        incidents = [
            {
                "source": exchange,
                "startedAt": state.get("incidentStartedAt"),
                "lastFailureAt": state.get("lastFailureAt"),
                "failureCount": int(state.get("failureCount") or 0),
                "affectedSymbols": sorted(state.get("affectedSymbols") or []),
                "lastError": state.get("lastError"),
            }
            for exchange, state in _depth_exchange_health.items()
            if state.get("incidentOpen")
        ]
    with _api_degraded_lock:
        runtime = dict(_api_degraded_runtime)
        degraded_incidents = [
            {
                **value,
                "affectedSymbols": sorted(value.get("affectedSymbols") or []),
            }
            for value in _api_degraded_incidents.values()
        ]
        recovery_probe_state = {
            source: dict(value) for source, value in _api_recovery_probe_state.items()
        }
        cloud_metrics = dict(_depth_cloud_metrics)
        cloud_sources = sorted(_depth_backup_sources)
    combined_incidents = [*incidents, *degraded_incidents]
    # One snapshot drives both the banner and route table. Source incidents
    # remain diagnostic history; they are not proof of a current route outage.
    route_control = {**_route_policy.snapshot(), "cloudQueue": _cloud_route_queue.snapshot()}
    return {
        "active": bool(route_control["affectedRouteCount"] or route_control["cloudRouteCount"]),
        "incidents": combined_incidents,
        "affectedSources": route_control["activeSources"],
        "affectedSymbols": route_control["activeSymbols"],
        "confirmedRouteFailureCount": route_control["affectedRouteCount"],
        "failureThreshold": 1,
        "switchPolicy": "elapsed_time",
        "localDeadlineSeconds": _local_depth_deadline_seconds(),
        "localFaultPushEnabled": False,
        "cloudFaultPushEnabled": True,
        "pushIntervalSeconds": spread_api_degraded_push_interval_seconds(),
        "recoveryProbe": {
            "running": bool(_api_recovery_thread and _api_recovery_thread.is_alive()),
            "activeOnly": True,
            "intervalSeconds": spread_api_recovery_probe_interval_seconds(),
            "requiredConsecutiveSuccesses": spread_api_recovery_probe_successes(),
            "sources": recovery_probe_state,
        },
        **runtime,
        "unverifiedCardAllowed": False,
        "routeControl": route_control,
        "cloudBackup": {**cloud_health, **cloud_metrics,
                        # Service health and a rejected route are different
                        # observations. A quiet book must not paint a healthy
                        # cloud connection as broken.
                        "lastError": cloud_health.get("lastError"),
                        "lastObservationError": cloud_metrics.get("lastObservationError"),
                        "activeSources": cloud_sources,
                        "minDwellSeconds": _cloud_backup_min_dwell_seconds()},
        "rule": (
            "Pulse只负责发现；本机单轮3秒超时仅该路线切云；本机故障不推送。"
            "报价质量不达标只显示并记录，不推送、不触发应急切换；连接或排队故障持续20秒才汇总推送，全局10分钟限频。"
            "本机真实盘口稳定10秒后正常切回，云端失败可通过本机完整复核应急回切；没有两轮有效深度绝不建卡。"
            "Pulse不能代替API复核；Gate也不允许跳过第二轮复核。"
        ),
    }


def spread_scan_enabled() -> bool:
    return _env_bool("ASTRO_SPREAD_SCAN_ENABLED", True)


def spread_scan_interval_seconds() -> int:
    return _env_int("ASTRO_SPREAD_SCAN_SECONDS", 10, 5, 60)


def spread_scan_min_open_pct() -> float:
    return _env_float("ASTRO_SPREAD_MIN_OPEN_PCT", 1.0, 0.01)


def spread_scan_ff_min_open_pct() -> float:
    return _saved_float_setting(
        "ffMinOpenSpreadPct",
        _env_float("ASTRO_SPREAD_FF_MIN_OPEN_PCT", spread_scan_min_open_pct(), 0.01),
        0.01,
        100.0,
    )


def _route_max_open_pct(route: dict[str, Any]) -> float:
    # Only this explicit direction can exceed the ordinary 10% safety ceiling.
    if (route.get("type") == "FF"
            and (route.get("sellExchange") or route.get("sellEx")) == "bybit"
            and (route.get("buyExchange") or route.get("buyEx")) in ASTRO_FF_BUY_EXCHANGES - {"bybit"}
            and astro_ff_bybit_sell_exception_enabled()):
        return max(100.0, spread_scan_max_open_pct())
    return spread_scan_max_open_pct()


def spread_scan_sf_min_open_pct() -> float:
    return _saved_float_setting(
        "sfMinOpenSpreadPct",
        _env_float("ASTRO_SPREAD_SF_MIN_OPEN_PCT", 1.3, 0.01),
        0.01,
        100.0,
    )


def spread_scan_sf_route_min_open_pct(exchange: Any) -> float:
    key = {"okxdex": "sfOkxdexMinOpenSpreadPct", "pancakeswapv3": "sfPancakeswapV3MinOpenSpreadPct"}.get(str(exchange or "").lower())
    return _saved_float_setting(key, 1.5, 0.01, 100.0) if key else spread_scan_sf_min_open_pct()


def spread_scan_sf_min_short_funding_pct() -> float:
    return 0.0  # Fixed user rule; legacy saved values no longer alter this gate.


def spread_scan_sf_okxdex_auto_card_enabled() -> bool:
    saved = _read_saved_subscription_payload().get("sfOkxdexAutoCardEnabled")
    if isinstance(saved, bool):
        return saved
    return _env_bool("ASTRO_SPREAD_SF_OKXDEX_AUTO_CARD_ENABLED", True)


def spread_scan_sf_pancakeswap_auto_card_enabled() -> bool:
    return _read_saved_subscription_payload().get("sfPancakeswapV3AutoCardEnabled") is True


def spread_scan_dex_auto_card_enabled(exchange: str) -> bool:
    return spread_scan_sf_okxdex_auto_card_enabled() if exchange == "okxdex" else spread_scan_sf_pancakeswap_auto_card_enabled()


def spread_okxdex_slippage_pct() -> float:
    return _env_float("ASTRO_OKXDEX_SLIPPAGE_PCT", 1.0, 0.1)


def spread_okxdex_quote_notional_usdt(config: Any | None = None) -> float:
    """Return the read-only DEX quote amount, bounded by the card order range."""

    requested = _env_float("ASTRO_OKXDEX_QUOTE_NOTIONAL_USDT", 10.0, 0.01)
    return min(astro_max_notional_usdt(config), max(astro_min_notional_usdt(), requested))


def spread_cex_quote_notional_usdt(config: Any | None = None) -> float:
    """Return the executable CEX depth-check amount within the card range."""

    return astro_max_notional_usdt(config)


def spread_okxdex_quote_bridge_url() -> str:
    port = _env_int("ASTRO_QUOTE_BRIDGE_PORT", 8765, 1, 65535)
    return os.environ.get("ASTRO_OKXDEX_QUOTE_BRIDGE_URL", f"http://127.0.0.1:{port}/dex-quote").strip()


def spread_okxdex_quote_timeout_seconds() -> float:
    return _env_float("ASTRO_OKXDEX_QUOTE_TIMEOUT_SECONDS", 9.0, 3.0)


def spread_scan_max_open_pct() -> float:
    return max(spread_scan_min_open_pct(), _env_float("ASTRO_SPREAD_MAX_OPEN_PCT", 10.0, 0.01))


def spread_scan_max_quote_age_seconds() -> float:
    return _saved_float_setting(
        "maxQuoteAgeSeconds",
        _env_float("ASTRO_SPREAD_MAX_QUOTE_AGE_SECONDS", 20.0, 5.0),
        5.0,
        120.0,
    )


def spread_final_revalidation_max_quote_age_seconds() -> float:
    return _env_float("ASTRO_FINAL_REVALIDATION_MAX_QUOTE_AGE_SECONDS", 3.0, 0.5)


def spread_final_revalidation_okxdex_max_quote_age_seconds() -> float:
    return _env_float("ASTRO_FINAL_REVALIDATION_OKXDEX_MAX_QUOTE_AGE_SECONDS", 20.0, 3.0)


def spread_final_revalidation_okxdex_submit_max_quote_age_seconds() -> float:
    """Maximum DEX quote age at the instant an Astro card is submitted.

    The wider OKXDEX age above keeps a Pulse observation usable while waiting
    for the next snapshot.  Submission is intentionally stricter so a card is
    never created from a quote that was merely valid at the beginning of the
    confirmation window.
    """

    return min(
        spread_final_revalidation_okxdex_max_quote_age_seconds(),
        _env_float("ASTRO_FINAL_REVALIDATION_OKXDEX_SUBMIT_MAX_QUOTE_AGE_SECONDS", 5.0, 1.0),
    )


def spread_final_revalidation_okxdex_distinct_wait_seconds() -> float:
    return _env_float("ASTRO_FINAL_REVALIDATION_OKXDEX_DISTINCT_WAIT_SECONDS", 15.0, 1.0)


def spread_final_revalidation_okxdex_poll_seconds() -> float:
    return _env_float("ASTRO_FINAL_REVALIDATION_OKXDEX_POLL_SECONDS", 1.0, 0.1)


def spread_final_revalidation_timeout_seconds() -> float:
    return min(8.0, _env_float("ASTRO_FINAL_REVALIDATION_TIMEOUT_SECONDS", 3.0, 1.0))


def spread_final_revalidation_max_skew_seconds() -> float:
    """Maximum cross-leg timestamp skew for ordinary CEX routes."""

    return _env_float("ASTRO_FINAL_REVALIDATION_MAX_SKEW_SECONDS", 1.25, 0.05)


def spread_final_revalidation_rounds() -> int:
    return _env_int("ASTRO_FINAL_REVALIDATION_ROUNDS", 2, 2, 4)


def spread_final_revalidation_interval_seconds() -> float:
    return _env_float("ASTRO_FINAL_REVALIDATION_INTERVAL_SECONDS", 0.5, 0.1)


def spread_final_revalidation_workers() -> int:
    return _env_int("ASTRO_FINAL_REVALIDATION_WORKERS", 3, 1, 4)


def spread_hot_monitor_interval_seconds() -> float:
    return _env_float("ASTRO_HOT_MONITOR_INTERVAL_SECONDS", 0.5, 0.2)


def spread_hot_monitor_route_ttl_seconds() -> float:
    return _env_float("ASTRO_HOT_MONITOR_ROUTE_TTL_SECONDS", 60.0, 5.0)


def spread_hot_monitor_hit_ttl_seconds() -> float:
    return _env_float("ASTRO_HOT_MONITOR_HIT_TTL_SECONDS", 3.0, 1.0)


def spread_hot_monitor_confirmation_interval_seconds() -> float:
    return _env_float("ASTRO_HOT_MONITOR_CONFIRMATION_INTERVAL_SECONDS", 0.25, 0.05)


def spread_hot_monitor_workers() -> int:
    return _env_int("ASTRO_HOT_MONITOR_WORKERS", 4, 1, 8)


def spread_hot_monitor_max_routes_per_cycle() -> int:
    return _env_int("ASTRO_HOT_MONITOR_MAX_ROUTES_PER_CYCLE", 8, 1, 24)


def spread_hot_monitor_max_routes_per_symbol_cycle() -> int:
    return _env_int("ASTRO_HOT_MONITOR_MAX_ROUTES_PER_SYMBOL_CYCLE", 4, 1, 8)


def spread_revalidation_retry_seconds(reason: str | None = None) -> float:
    if reason in {"stale_direct_quote", "direct_quote_time_skew"}:
        return _env_float("ASTRO_REVALIDATION_QUOTE_RETRY_SECONDS", 5.0, 1.0)
    if reason == "direct_quote_unavailable":
        return _env_float("ASTRO_REVALIDATION_UNAVAILABLE_RETRY_SECONDS", 15.0, 1.0)
    return _env_float("ASTRO_REVALIDATION_RETRY_SECONDS", 10.0, 1.0)


def spread_revalidation_retry_improvement_pct_points() -> float:
    return _env_float("ASTRO_REVALIDATION_RETRY_IMPROVEMENT_PCT_POINTS", 0.15, 0.01)


def spread_scan_confirmations() -> int:
    return _saved_int_setting(
        "confirmations",
        _env_int("ASTRO_SPREAD_CONFIRMATIONS", 1, 1, 6),
        1,
        6,
    )


def spread_scan_exclude_delisted_exchange_cards() -> bool:
    saved = _read_saved_subscription_payload().get("excludeDelistedExchangeCards")
    if isinstance(saved, bool):
        return saved
    return _env_bool("ASTRO_EXCLUDE_DELISTED_EXCHANGE_CARDS", True)


def _subscription_path() -> Path | None:
    explicit = os.environ.get("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("STOCK_REVIEW_DATA_DIR", "").strip()
    return Path(data_dir).expanduser() / "astro-spread-subscriptions.json" if data_dir else None


def _normalize_market_keys(values: list[Any]) -> list[str]:
    selected: list[str] = []
    for raw in values:
        item = PULSE_MARKET_BY_KEY.get(str(raw).strip().lower())
        if item and item[0] not in selected:
            selected.append(item[0])
    return selected


def _read_saved_subscription_payload() -> dict[str, Any]:
    path = _subscription_path()
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _saved_float_setting(key: str, default: float, minimum: float, maximum: float) -> float:
    value = _read_saved_subscription_payload().get(key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(parsed, maximum))


def _saved_int_setting(key: str, default: int, minimum: int, maximum: int) -> int:
    value = _read_saved_subscription_payload().get(key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _configured_market_keys() -> tuple[list[str], str]:
    saved = _read_saved_subscription_payload()
    selected = _normalize_market_keys(saved.get("markets") if isinstance(saved.get("markets"), list) else [])
    if selected:
        return selected, "saved"
    raw_markets = os.environ.get("ASTRO_SPREAD_MARKETS", "").strip()
    selected = _normalize_market_keys(raw_markets.split(",")) if raw_markets else []
    if selected:
        return selected, "environment"
    raw_exchanges = os.environ.get("ASTRO_SPREAD_EXCHANGES", "").strip()
    if raw_exchanges:
        exchanges = {part.strip().lower() for part in raw_exchanges.split(",") if part.strip()}
        selected = [key for key, exchange, _label, _market in PULSE_MARKETS if exchange in exchanges]
        if selected:
            return selected, "legacy_environment"
    return list(DEFAULT_MARKETS), "default"


def _normalize_blocked_pairs(values: list[Any]) -> list[dict[str, str]]:
    blocked: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        market = PULSE_MARKET_BY_KEY.get(str(raw.get("marketKey") or "").strip().lower())
        symbol = str(raw.get("symbol") or raw.get("coin") or "").strip().upper()
        if symbol and not symbol.endswith("USDT"):
            symbol += "USDT"
        if not market or not symbol or not symbol[:-4].isalnum():
            continue
        identity = (market[0], symbol)
        if identity in seen:
            continue
        seen.add(identity)
        blocked.append({"marketKey": market[0], "symbol": symbol})
    return blocked


def _normalize_blocked_coins(values: list[Any]) -> list[str]:
    blocked: list[str] = []
    for raw in values:
        symbol = str(raw or "").strip().upper()
        if symbol.endswith("USDT"):
            symbol = symbol[:-4]
        if symbol and symbol.isalnum() and symbol not in blocked:
            blocked.append(symbol)
    return blocked


def _normalize_dex_mapped_assets(values: list[Any]) -> list[dict[str, Any]]:
    mapped = {}
    for raw in values:
        raw = {"symbol": raw} if isinstance(raw, str) else raw
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or raw.get("name") or "").strip().upper().removesuffix("USDT")
        exchange = str(raw.get("exchange") or "okxdex").lower()
        chain = str(raw.get("chainIndex") or "").strip()
        address = _normalized_contract_address(chain, raw.get("contractAddress"))
        if not symbol or exchange not in SF_DEX_EXCHANGES or bool(chain) != bool(address):
            continue
        mapped[(exchange, symbol, chain, address)] = {"exchange": exchange, "symbol": symbol,
            "chainIndex": chain, "chainLabel": OKXDEX_CHAIN_LABELS.get(chain, chain or "待补全链地址"),
            "contractAddress": address, "mappingMode": "exchange_chain_contract",
            "autoCreateEligible": bool(chain and address)}
    return list(mapped.values())


def spread_scan_dex_mapped_assets() -> list[dict[str, Any]]:
    saved = _read_saved_subscription_payload()
    values = saved.get("dexMappedAssets")
    if isinstance(values, list):
        return _normalize_dex_mapped_assets(values)
    return _normalize_dex_mapped_assets(list(LEGACY_CONFIRMED_ASTRO_DEX_SYMBOLS))


def spread_okxdex_identity_verification_enabled() -> bool:
    """Whether target-exchange asset metadata must verify the OKXDEX identity."""

    return _env_bool("ASTRO_SPREAD_OKXDEX_IDENTITY_VERIFICATION_ENABLED", True)


def _dex_mapping_confirmation(candidate: dict[str, Any]) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "").strip().upper().removesuffix("USDT")
    exchange = str(candidate.get("buyExchange") or "okxdex").lower()
    config = candidate.get("dexConfig") or {}
    chain = str(config.get("chainIndex") or "").strip()
    address = _normalized_contract_address(chain, config.get("contractAddress"))
    confirmed = bool(chain and address) and any(
        (item["symbol"], item["chainIndex"], item["contractAddress"]) == (symbol, chain, address)
        for item in spread_scan_dex_mapped_assets())
    return {"status": "confirmed" if confirmed else "missing", "mode": "exchange_chain_contract",
            "exchange": exchange, "symbol": symbol, "chainIndex": chain,
            "chainLabel": OKXDEX_CHAIN_LABELS.get(chain, chain or "未知链"), "contractAddress": address,
            "reason": "已确认共用币种的链和合约配置" if confirmed else "请在 Astro 配置后确认链和合约地址（两家 DEX 共用）"}


def spread_scan_min_volume_usdt() -> float:
    saved = _read_saved_subscription_payload()
    value = saved.get("minVolumeUsdt")
    if value is not None:
        try:
            parsed = float(value)
            if math.isfinite(parsed):
                return max(0.0, parsed)
        except (TypeError, ValueError):
            pass
    return _env_float("ASTRO_SPREAD_MIN_VOLUME_USDT", 200_000.0, 0.0)


def spread_scan_blocked_pairs() -> list[dict[str, str]]:
    saved = _read_saved_subscription_payload()
    values = saved.get("blockedPairs")
    return _normalize_blocked_pairs(values if isinstance(values, list) else [])


def _spread_block_rule_sets() -> tuple[set[tuple[str, str]], set[str]]:
    return (
        {(item["marketKey"], item["symbol"]) for item in spread_scan_blocked_pairs()},
        set(spread_scan_blocked_coins()),
    )


def _route_block_match(
    *,
    symbol: Any,
    pair_type: Any,
    buy_exchange: Any,
    sell_exchange: Any,
    blocked_pairs: set[tuple[str, str]] | None = None,
    blocked_coins: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return the exact live block that applies to an Astro spread route."""

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol.endswith("USDT"):
        normalized_symbol += "USDT"
    base = normalized_symbol.removesuffix("USDT")
    pair_rules, coin_rules = (
        (blocked_pairs, blocked_coins)
        if blocked_pairs is not None and blocked_coins is not None
        else _spread_block_rule_sets()
    )
    if base in coin_rules:
        return {
            "reason": "blocked_coin",
            "symbol": normalized_symbol,
            "matchedCoin": base,
        }

    normalized_type = str(pair_type or "").strip().upper()
    buy_market = "spot" if normalized_type == "SF" else "future"
    sell_market = "future"
    legs = (
        (str(buy_exchange or "").strip().lower(), buy_market, "buy"),
        (str(sell_exchange or "").strip().lower(), sell_market, "sell"),
    )
    matches: list[dict[str, str]] = []
    for exchange, market_type, side in legs:
        market_key = PULSE_MARKET_KEY_BY_ROUTE.get((exchange, market_type))
        if market_key and (market_key, normalized_symbol) in pair_rules:
            matches.append(
                {
                    "marketKey": market_key,
                    "symbol": normalized_symbol,
                    "side": side,
                }
            )
    if not matches:
        return None
    return {
        "reason": "blocked_pair",
        "symbol": normalized_symbol,
        "matchedPairs": matches,
    }


def astro_spread_pair_submit_guard(pair: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Fail closed against the latest saved blocks immediately before add."""

    from app.astro_news_policy import route_check as news_route_check
    news_block = news_route_check(pair.get("name"), pair.get("type"), pair.get("buyEx"), pair.get("sellEx"))
    if news_block:
        return False, news_block
    if str(pair.get("type")).upper() == "SF" and str(pair.get("buyEx")).lower() in SF_DEX_EXCHANGES:
        exchange = str(pair["buyEx"]).lower()
        if not spread_scan_dex_auto_card_enabled(exchange):
            return False, {"reason": f"sf_{exchange}_auto_card_paused"}
        mapping = _dex_mapping_confirmation({"symbol": pair.get("name"), "buyExchange": exchange, "dexConfig": pair.get("_dexConfig")})
        if mapping["status"] != "confirmed":
            return False, {"reason": "dex_mapping_unconfirmed"}
        opening = _finite(pair.get("openPosition"))
        minimum = spread_scan_sf_route_min_open_pct(exchange)
        if opening is None or opening * 100 <= minimum:
            return False, {"reason": "below_threshold_or_rule_failed", "requiredOpenSpreadPctExclusive": minimum}
    if pair.get("_apiDegradedEvidence"):
        return False, {"reason": "api_unverified_card_disabled"}
    match = _route_block_match(
        symbol=pair.get("name"),
        pair_type=pair.get("type"),
        buy_exchange=pair.get("buyEx"),
        sell_exchange=pair.get("sellEx"),
    )
    return (match is None, match or {"reason": "allowed"})


def spread_scan_blocked_coins() -> list[str]:
    saved = _read_saved_subscription_payload()
    values = saved.get("blockedCoins")
    return _normalize_blocked_coins(values if isinstance(values, list) else [])


def spread_scan_market_keys() -> set[str]:
    selected, _source = _configured_market_keys()
    return set(selected)


def spread_scan_exchanges() -> set[str]:
    selected = spread_scan_market_keys()
    return {exchange for key, exchange, _label, _market in PULSE_MARKETS if key in selected}


_settings_write_lock = threading.RLock()


def _serialize_settings_write(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _settings_write_lock:
            return function(*args, **kwargs)
    return wrapped


@_serialize_settings_write
def confirm_astro_dex_mapping(asset: dict[str, Any]) -> dict[str, Any]:
    rows = _normalize_dex_mapped_assets([asset])
    if len(rows) != 1 or not rows[0]["autoCreateEligible"]:
        raise ValueError("请填写币名、链 ID 和合约地址")
    row = rows[0]
    if not row["chainIndex"].isdigit():
        raise ValueError("链 ID 应为数字")
    if row["chainIndex"] == "501":
        valid = re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", row["contractAddress"])
    else:
        valid = re.fullmatch(r"0x[0-9a-f]{40}", row["contractAddress"])
    if not valid:
        raise ValueError("合约地址格式不正确")
    saved = spread_scan_dex_mapped_assets()
    identity = (row["symbol"],row["chainIndex"],row["contractAddress"])
    saved = [item for item in saved if (item["symbol"],item["chainIndex"],item["contractAddress"]) != identity
             and not (item["symbol"]==row["symbol"] and not item["contractAddress"])]
    saved.append(row)
    markets = _configured_market_keys()[0]
    return update_astro_spread_subscriptions(markets, dex_mapped_assets=saved)


@_serialize_settings_write
def update_astro_spread_subscriptions(
    values: list[Any],
    *,
    min_volume_usdt: Any = None,
    blocked_pairs: list[Any] | None = None,
    blocked_coins: list[Any] | None = None,
    dex_mapped_assets: list[Any] | None = None,
    delete_rearm_pct: Any = None,
    delete_pullback_pct_points: Any = None,
    ff_min_open_spread_pct: Any = None,
    ff_bybit_sell_exception_enabled: Any = None,
    sf_min_open_spread_pct: Any = None,
    sf_okxdex_min_open_spread_pct: Any = None,
    sf_pancakeswap_v3_min_open_spread_pct: Any = None,
    sf_min_short_funding_rate_pct: Any = None,
    sf_okxdex_auto_card_enabled: Any = None,
    sf_pancakeswap_v3_auto_card_enabled: Any = None,
    fs_borrow_auto_card_enabled: Any = None,
    fs_borrow_min_cycle_profit_pct: Any = None,
    fs_borrow_min_open_spread_pct: Any = None,
    confirmations: Any = None,
    max_quote_age_seconds: Any = None,
    exclude_delisted_exchange_cards: Any = None,
    greater_price_alert_pct: Any = _UNSET_SETTING,
    price_change_alert_pct: Any = _UNSET_SETTING,
    price_change_alert_only_rise: Any = None,
    min_notional_usdt: Any = _UNSET_SETTING,
    max_notional_usdt: Any = _UNSET_SETTING,
) -> dict[str, Any]:
    selected = _normalize_market_keys(values)
    if len(selected) < 2:
        raise ValueError("至少订阅两个行情源才能计算差价")
    path = _subscription_path()
    if path is None:
        raise ValueError("未配置本地数据目录，无法保存 Astro 订阅")
    try:
        parsed_min_volume = float(min_volume_usdt) if min_volume_usdt is not None else spread_scan_min_volume_usdt()
    except (TypeError, ValueError) as exc:
        raise ValueError("成交额门槛必须是数字") from exc
    if not math.isfinite(parsed_min_volume) or parsed_min_volume < 0:
        raise ValueError("成交额门槛不能小于 0")
    try:
        parsed_delete_rearm = (
            float(delete_rearm_pct)
            if delete_rearm_pct is not None
            else astro_delete_rearm_pct()
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("删除后重建涨幅必须是数字") from exc
    if not math.isfinite(parsed_delete_rearm) or not 0 <= parsed_delete_rearm <= 1000:
        raise ValueError("删除后重建涨幅必须在 0% 到 1000% 之间")
    try:
        parsed_delete_pullback = (
            float(delete_pullback_pct_points)
            if delete_pullback_pct_points is not None
            else astro_delete_pullback_pct_points()
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("删除后回弱幅度必须是数字") from exc
    if not math.isfinite(parsed_delete_pullback) or not 0 <= parsed_delete_pullback <= 100:
        raise ValueError("删除后回弱幅度必须在 0 到 100 个百分点之间")
    numeric_rules = (
        ("FF 开仓差价", ff_min_open_spread_pct, spread_scan_ff_min_open_pct(), 0.01, 100.0),
        ("SF 开仓差价", sf_min_open_spread_pct, spread_scan_sf_min_open_pct(), 0.01, 100.0),
        ("OKXDEX 开仓差价", sf_okxdex_min_open_spread_pct, spread_scan_sf_route_min_open_pct("okxdex"), 0.01, 100.0),
        ("PancakeSwap V3 开仓差价", sf_pancakeswap_v3_min_open_spread_pct, spread_scan_sf_route_min_open_pct("pancakeswapv3"), 0.01, 100.0),
        ("SF 最低资金费", 0.0, 0.0, 0.0, 0.0),
        ("FS 周期净收益", fs_borrow_min_cycle_profit_pct, astro_fs_borrow_min_cycle_profit_pct(), 0.0, 100.0),
        ("FS 开仓差价", fs_borrow_min_open_spread_pct, astro_fs_borrow_min_open_spread_pct(), 0.01, 100.0),
        ("行情有效秒数", max_quote_age_seconds, spread_scan_max_quote_age_seconds(), 5.0, 120.0),
    )
    parsed_rules: dict[str, float] = {}
    for label, raw_value, fallback, minimum, maximum in numeric_rules:
        try:
            parsed = float(raw_value) if raw_value is not None else fallback
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是数字") from exc
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ValueError(f"{label}必须在 {minimum:g} 到 {maximum:g} 之间")
        parsed_rules[label] = parsed

    def optional_alert(label: str, raw_value: Any, fallback: float | None) -> float | None:
        value = fallback if raw_value is _UNSET_SETTING else raw_value
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是数字或留空") from exc
        if not math.isfinite(parsed) or not 0 <= parsed <= 100:
            raise ValueError(f"{label}必须在 0 到 100 之间或留空")
        return parsed if parsed > 0 else None

    parsed_greater_price_alert = optional_alert(
        "实时差价报警",
        greater_price_alert_pct,
        astro_greater_price_alert_pct(),
    )
    parsed_price_change_alert = optional_alert(
        "价格涨跌幅报警",
        price_change_alert_pct,
        astro_price_change_alert_pct(),
    )

    def notional_value(label: str, raw_value: Any, fallback: float) -> float:
        value = fallback if raw_value is _UNSET_SETTING else raw_value
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是数字") from exc
        if not math.isfinite(parsed) or not 0.01 <= parsed <= 1_000_000:
            raise ValueError(f"{label}必须在 0.01 到 1000000 USDT 之间")
        return parsed

    parsed_min_notional = notional_value("最小单笔金额", min_notional_usdt, astro_min_notional_usdt())
    parsed_max_notional = notional_value("最大单笔金额", max_notional_usdt, astro_max_notional_usdt())
    if parsed_max_notional < parsed_min_notional:
        raise ValueError("最大单笔金额不能小于最小单笔金额")
    try:
        parsed_confirmations = int(confirmations) if confirmations is not None else spread_scan_confirmations()
    except (TypeError, ValueError) as exc:
        raise ValueError("连续确认轮数必须是整数") from exc
    if not 1 <= parsed_confirmations <= 6:
        raise ValueError("连续确认轮数必须在 1 到 6 之间")
    parsed_exclude_delisted = (
        exclude_delisted_exchange_cards
        if isinstance(exclude_delisted_exchange_cards, bool)
        else spread_scan_exclude_delisted_exchange_cards()
    )
    parsed_price_change_only_rise = (
        price_change_alert_only_rise
        if isinstance(price_change_alert_only_rise, bool)
        else astro_price_change_alert_only_rise()
    )
    if ff_bybit_sell_exception_enabled is not None and not isinstance(ff_bybit_sell_exception_enabled, bool):
        raise ValueError("Bybit 卖出腿例外必须是开关值")
    parsed_bybit_exception = (astro_ff_bybit_sell_exception_enabled() if ff_bybit_sell_exception_enabled is None else ff_bybit_sell_exception_enabled)
    parsed_fs_borrow_enabled = (
        fs_borrow_auto_card_enabled
        if isinstance(fs_borrow_auto_card_enabled, bool)
        else astro_fs_borrow_auto_card_enabled()
    )
    parsed_sf_okxdex_enabled = (
        sf_okxdex_auto_card_enabled
        if isinstance(sf_okxdex_auto_card_enabled, bool)
        else spread_scan_sf_okxdex_auto_card_enabled()
    )
    normalized_blocked = (
        _normalize_blocked_pairs(blocked_pairs)
        if blocked_pairs is not None
        else spread_scan_blocked_pairs()
    )
    normalized_blocked_coins = (
        _normalize_blocked_coins(blocked_coins)
        if blocked_coins is not None
        else spread_scan_blocked_coins()
    )
    normalized_dex_mapped_assets = (
        _normalize_dex_mapped_assets(dex_mapped_assets)
        if dex_mapped_assets is not None
        else spread_scan_dex_mapped_assets()
    )
    payload = {
        "markets": selected,
        "minVolumeUsdt": parsed_min_volume,
        "blockedPairs": normalized_blocked,
        "blockedCoins": normalized_blocked_coins,
        "dexMappedAssets": normalized_dex_mapped_assets,
        "deleteRearmPct": parsed_delete_rearm,
        "deletePullbackPctPoints": parsed_delete_pullback,
        "ffMinOpenSpreadPct": parsed_rules["FF 开仓差价"],
        "ffBybitSellExceptionEnabled": parsed_bybit_exception,
        "sfMinOpenSpreadPct": parsed_rules["SF 开仓差价"],
        "sfOkxdexMinOpenSpreadPct": parsed_rules["OKXDEX 开仓差价"],
        "sfPancakeswapV3MinOpenSpreadPct": parsed_rules["PancakeSwap V3 开仓差价"],
        "sfMinShortFundingRatePct": parsed_rules["SF 最低资金费"],
        "sfOkxdexAutoCardEnabled": parsed_sf_okxdex_enabled,
        "sfPancakeswapV3AutoCardEnabled": sf_pancakeswap_v3_auto_card_enabled if isinstance(sf_pancakeswap_v3_auto_card_enabled, bool) else spread_scan_sf_pancakeswap_auto_card_enabled(),
        "fsBorrowAutoCardEnabled": parsed_fs_borrow_enabled,
        "fsBorrowMinCycleProfitPct": parsed_rules["FS 周期净收益"],
        "fsBorrowMinOpenSpreadPct": parsed_rules["FS 开仓差价"],
        "confirmations": parsed_confirmations,
        "maxQuoteAgeSeconds": parsed_rules["行情有效秒数"],
        "excludeDelistedExchangeCards": parsed_exclude_delisted,
        "greaterPriceAlertPct": parsed_greater_price_alert,
        "priceChangeAlertPct": parsed_price_change_alert,
        "priceChangeAlertOnlyRise": parsed_price_change_only_rise,
        "minNotionalUsdt": parsed_min_notional,
        "maxNotionalUsdt": parsed_max_notional,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    with _subscription_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    with _hit_lock:
        _hits.clear()
    with _revalidation_state_lock:
        _revalidation_failures.clear()
    with _hot_lock:
        _hot_routes.clear()
        _hot_recent_hits.clear()
        _hot_recent_hit_at_ms.clear()
    _hot_wake.set()
    append_system_runtime_event(
        "astro_spread_subscriptions_updated",
        level="info",
        source="backend",
        module="astro_spread_scanner",
        message=(
            f"Astro 扫描规则已更新：{len(selected)} 个行情源，"
            f"{len(normalized_blocked_coins)} 个全局屏蔽币种，{len(normalized_blocked)} 条定向过滤"
        ),
        details={
            "markets": selected,
            "minVolumeUsdt": parsed_min_volume,
            "blockedPairs": normalized_blocked,
            "blockedCoins": normalized_blocked_coins,
            "dexMappedAssets": normalized_dex_mapped_assets,
            "deleteRearmPct": parsed_delete_rearm,
            "deletePullbackPctPoints": parsed_delete_pullback,
            "ffMinOpenSpreadPct": parsed_rules["FF 开仓差价"],
        "ffBybitSellExceptionEnabled": parsed_bybit_exception,
            "sfMinOpenSpreadPct": parsed_rules["SF 开仓差价"],
            "sfOkxdexMinOpenSpreadPct": parsed_rules["OKXDEX 开仓差价"],
            "sfPancakeswapV3MinOpenSpreadPct": parsed_rules["PancakeSwap V3 开仓差价"],
            "sfMinShortFundingRatePct": parsed_rules["SF 最低资金费"],
            "sfOkxdexAutoCardEnabled": parsed_sf_okxdex_enabled,
            "sfPancakeswapV3AutoCardEnabled": sf_pancakeswap_v3_auto_card_enabled if isinstance(sf_pancakeswap_v3_auto_card_enabled, bool) else spread_scan_sf_pancakeswap_auto_card_enabled(),
            "fsBorrowAutoCardEnabled": parsed_fs_borrow_enabled,
            "fsBorrowMinCycleProfitPct": parsed_rules["FS 周期净收益"],
            "fsBorrowMinOpenSpreadPct": parsed_rules["FS 开仓差价"],
            "confirmations": parsed_confirmations,
            "maxQuoteAgeSeconds": parsed_rules["行情有效秒数"],
            "excludeDelistedExchangeCards": parsed_exclude_delisted,
            "greaterPriceAlertPct": parsed_greater_price_alert,
            "priceChangeAlertPct": parsed_price_change_alert,
            "priceChangeAlertOnlyRise": parsed_price_change_only_rise,
            "minNotionalUsdt": parsed_min_notional,
            "maxNotionalUsdt": parsed_max_notional,
        },
    )
    return astro_spread_scanner_status()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalized_contract_address(chain_index: Any, address: Any) -> str:
    chain = str(chain_index or "").strip()
    value = str(address or "").strip()
    return value.lower() if chain in {"1", "56", "8453", "42161"} else value


def _binance_network_chain_index(network: dict[str, Any]) -> str | None:
    explicit = str(network.get("chainIndex") or network.get("chainId") or "").strip()
    if explicit in OKXDEX_CHAIN_LABELS:
        return explicit
    names = (network.get("network"), network.get("name"))
    for raw in names:
        normalized = "".join(character for character in str(raw or "").upper() if character.isalnum())
        if normalized in BINANCE_NETWORK_CHAIN_INDEX:
            return BINANCE_NETWORK_CHAIN_INDEX[normalized]
        for part in re.split(r"[^A-Za-z0-9]+", str(raw or "").upper()):
            if part in BINANCE_NETWORK_CHAIN_INDEX:
                return BINANCE_NETWORK_CHAIN_INDEX[part]
    url = str(network.get("contractAddressUrl") or "").lower()
    for domain, chain_index in (
        ("etherscan.io", "1"),
        ("bscscan.com", "56"),
        ("solscan.io", "501"),
        ("basescan.org", "8453"),
        ("arbiscan.io", "42161"),
    ):
        if domain in url:
            return chain_index
    return None


def _fetch_binance_asset_identity_config() -> dict[str, dict[str, Any]]:
    """Load Binance's signed asset/network contract table through the shared cache."""

    # Local import avoids the existing crypto -> scanner module dependency at import time.
    from app.crypto import fetch_binance_transfer_config

    with httpx.Client(
        timeout=8.0,
        headers={"Accept": "application/json", "User-Agent": "stock-review-mac/dex-identity-check"},
    ) as client:
        return fetch_binance_transfer_config(client)


def _asset_identity_row(asset: str, networks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"asset": asset, "networkList": networks}


def _fetch_target_asset_identity_uncached(exchange: str, asset: str) -> dict[str, Any] | None:
    """Read one asset's chain/contract identities from the target exchange itself."""

    from app.crypto import signed_bybit_get, signed_okx_get

    normalized_exchange = str(exchange or "").strip().lower()
    normalized_asset = str(asset or "").strip().upper().removesuffix("USDT")
    headers = {"Accept": "application/json", "User-Agent": "stock-review-mac/dex-identity-check"}
    with httpx.Client(timeout=8.0, headers=headers) as client:
        if normalized_exchange == "binance":
            return _fetch_binance_asset_identity_config().get(normalized_asset)
        if normalized_exchange == "bybit":
            payload = signed_bybit_get(client, "/v5/asset/coin/query-info", {"coin": normalized_asset})
            rows = ((payload.get("result") or {}).get("rows") or []) if isinstance(payload, dict) else []
            row = rows[0] if rows else None
            if not isinstance(row, dict):
                return None
            networks = [
                {
                    "network": chain.get("chain") or chain.get("chainType"),
                    "name": chain.get("chainType") or chain.get("chain"),
                    "chainIndex": chain.get("chainId"),
                    "contractAddress": chain.get("contractAddress"),
                }
                for chain in row.get("chains") or []
                if isinstance(chain, dict)
            ]
            return _asset_identity_row(normalized_asset, networks)
        if normalized_exchange == "bitget":
            response = client.get(
                "https://api.bitget.com/api/v2/spot/public/coins",
                params={"coin": normalized_asset},
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else []
            if isinstance(rows, dict):
                rows = [rows]
            row = next(
                (
                    item
                    for item in rows or []
                    if isinstance(item, dict)
                    and str(item.get("coin") or item.get("coinName") or "").strip().upper() == normalized_asset
                ),
                None,
            )
            if not isinstance(row, dict):
                return None
            networks = [
                {
                    "network": chain.get("chain") or chain.get("chainName") or chain.get("network"),
                    "name": chain.get("chainName") or chain.get("chain") or chain.get("network"),
                    "chainIndex": chain.get("chainId"),
                    "contractAddress": chain.get("contractAddress") or chain.get("contract"),
                }
                for chain in row.get("chains") or row.get("chainList") or []
                if isinstance(chain, dict)
            ]
            return _asset_identity_row(normalized_asset, networks)
        if normalized_exchange == "gate":
            response = client.get(
                "https://api.gateio.ws/api/v4/wallet/currency_chains",
                params={"currency": normalized_asset},
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                return None
            networks = [
                {
                    "network": row.get("chain") or row.get("name"),
                    "name": row.get("name") or row.get("chain"),
                    "chainIndex": row.get("chain_id") or row.get("chainId"),
                    "contractAddress": row.get("contract_address") or row.get("contractAddress"),
                }
                for row in rows
                if isinstance(row, dict)
            ]
            return _asset_identity_row(normalized_asset, networks)
        if normalized_exchange == "aster":
            response = client.get(
                "https://www.asterdex.com/bapi/futures/v1/public/future/aster/deposit/assets",
                params={
                    "chainIds": ",".join(OKXDEX_CHAIN_LABELS),
                    "networks": "EVM,SOLANA",
                    "accountType": "spot",
                },
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else []
            matched = [
                row
                for row in rows or []
                if isinstance(row, dict) and str(row.get("name") or "").strip().upper() == normalized_asset
            ]
            if not matched:
                return None
            networks = [
                {
                    "network": row.get("network"),
                    "name": row.get("network"),
                    "chainIndex": row.get("chainId"),
                    "contractAddress": row.get("contractAddress"),
                }
                for row in matched
            ]
            return _asset_identity_row(normalized_asset, networks)
        if normalized_exchange == "okx":
            payload = signed_okx_get(client, "/api/v5/asset/currencies", {"ccy": normalized_asset})
            rows = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(rows, list) or not rows:
                return None
            networks = [
                {
                    "network": row.get("chain") or row.get("ccy"),
                    "name": row.get("chain") or row.get("ccy"),
                    "chainIndex": row.get("chainId"),
                    "contractAddress": row.get("ctAddr") or row.get("contractAddress"),
                }
                for row in rows
                if isinstance(row, dict)
            ]
            return _asset_identity_row(normalized_asset, networks)
    raise ValueError(f"不支持的 OKXDEX 目标交易所：{normalized_exchange}")


def _fetch_target_asset_identity_config(exchange: str, asset: str) -> dict[str, Any] | None:
    key = (str(exchange or "").strip().lower(), str(asset or "").strip().upper().removesuffix("USDT"))
    now = time.time()
    with _dex_asset_identity_lock:
        cached = _dex_asset_identity_cache.get(key)
        if cached and now - cached[0] < (300.0 if cached[2] is None else 15.0):
            if cached[2] is not None:
                raise RuntimeError(cached[2])
            return cached[1]
    try:
        row = _fetch_target_asset_identity_uncached(*key)
    except Exception as exc:
        with _dex_asset_identity_lock:
            _dex_asset_identity_cache[key] = (now, None, str(exc))
        raise
    with _dex_asset_identity_lock:
        _dex_asset_identity_cache[key] = (now, row, None)
    return row


def _verify_okxdex_identity(
    symbol: str,
    dex_config: dict[str, Any],
    target_config: dict[str, dict[str, Any]] | dict[str, Any] | None,
    target_exchange: str = "binance",
) -> dict[str, Any]:
    asset = str(symbol or "").strip().upper().removesuffix("USDT")
    chain_index = str(dex_config.get("chainIndex") or "").strip()
    contract_address = str(dex_config.get("contractAddress") or "").strip()
    exchange = str(target_exchange or "").strip().lower()
    source = TARGET_IDENTITY_SOURCE.get(exchange, f"{exchange} 官方资产网络表")
    base = {
        "status": "unverified",
        "source": source,
        "targetExchange": exchange,
        "asset": asset,
        "chainIndex": chain_index,
        "chain": OKXDEX_CHAIN_LABELS.get(chain_index, chain_index or "未知链"),
    }
    if not asset or not chain_index or not contract_address:
        return {**base, "status": "missing_dex_identity", "reason": "DEX 缺少链或合约地址"}
    asset_row = target_config.get(asset) if isinstance(target_config, dict) and asset in target_config else target_config
    if not isinstance(asset_row, dict):
        return {**base, "status": "asset_not_found", "reason": f"{source}未返回同名资产"}
    same_chain: list[dict[str, Any]] = []
    expected_address = _normalized_contract_address(chain_index, contract_address)
    for network in asset_row.get("networkList") or []:
        if not isinstance(network, dict) or _binance_network_chain_index(network) != chain_index:
            continue
        same_chain.append(network)
        binance_address = str(network.get("contractAddress") or "").strip()
        if binance_address and _normalized_contract_address(chain_index, binance_address) == expected_address:
            return {
                **base,
                "status": "verified",
                "reason": f"链 ID 与合约地址均匹配{source}",
                "matchedNetwork": str(network.get("network") or network.get("name") or "").strip(),
            }
    if not same_chain:
        return {**base, "status": "chain_not_found", "reason": f"{source}中的同名资产不支持该链"}
    if not any(str(network.get("contractAddress") or "").strip() for network in same_chain):
        return {**base, "status": "target_contract_missing", "reason": f"{source}未公开该链合约地址，无法核验"}
    return {**base, "status": "contract_mismatch", "reason": "同名同链但合约地址不同"}


def _verify_okxdex_candidate_identities(
    candidates: list[dict[str, Any]],
    *,
    eligible_only: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    def requires_identity(candidate: dict[str, Any]) -> bool:
        opening = _finite(candidate.get("openSpreadPct"))
        is_okxdex_route = (
            str(candidate.get("type") or "").upper() == "SF"
            and str(candidate.get("buyExchange") or "").lower() in SF_DEX_EXCHANGES
            and str(candidate.get("sellExchange") or "").lower() in SF_OKXDEX_FUTURES_EXCHANGES
        )
        if not eligible_only:
            return is_okxdex_route
        return (
            is_okxdex_route
            and opening is not None
            and spread_scan_sf_route_min_open_pct(candidate.get("buyExchange")) < opening <= _route_max_open_pct(candidate)
        )

    dex_candidates = [candidate for candidate in candidates if requires_identity(candidate)]
    summary = {
        "identityVerificationEnabled": spread_okxdex_identity_verification_enabled(),
        "verifiedCount": 0,
        "bypassedCount": 0,
        "blockedCount": 0,
        "statusCounts": {},
        "blockedItems": [],
        "lastError": None,
    }
    if not dex_candidates:
        return candidates, summary
    if not spread_okxdex_identity_verification_enabled():
        for candidate in dex_candidates:
            candidate["dexIdentity"] = {
                "status": "bypassed",
                "reason": "OKXDEX 链地址身份安全拦截已临时关闭",
            }
        summary["bypassedCount"] = len(dex_candidates)
        summary["statusCounts"] = {"bypassed": len(dex_candidates)}
        return candidates, summary

    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in dex_candidates:
        grouped.setdefault(str(candidate.get("symbol") or "").strip().upper(), []).append(candidate)

    fetch_tasks: dict[tuple[str, str], Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(dex_candidates)))) as executor:
        for symbol, group in grouped.items():
            # A ticker can legitimately exist on several chains.  Treat the
            # exact (chain, contract) tuple as its identity and verify every
            # candidate against the target exchange's network table.
            for candidate in group:
                target_exchange = str(candidate.get("sellExchange") or "").strip().lower()
                key = (target_exchange, symbol)
                if key not in fetch_tasks:
                    fetch_tasks[key] = executor.submit(
                        _call_with_astro_api_priority,
                        _fetch_target_asset_identity_config,
                        *key,
                    )

        errors: list[str] = []
        for symbol, group in grouped.items():
            for candidate in group:
                if candidate.get("dexIdentity"):
                    continue
                target_exchange = str(candidate.get("sellExchange") or "").strip().lower()
                source = TARGET_IDENTITY_SOURCE.get(target_exchange, f"{target_exchange} 官方资产网络表")
                try:
                    target_config = fetch_tasks[(target_exchange, symbol)].result()
                except Exception as exc:
                    errors.append(f"{target_exchange}:{symbol}: {exc}")
                    candidate["dexIdentity"] = {
                        "status": "verification_unavailable",
                        "source": source,
                        "targetExchange": target_exchange,
                        "reason": f"{source}读取失败",
                    }
                    continue
                dex_config = candidate.get("dexConfig")
                candidate["dexIdentity"] = (
                    _verify_okxdex_identity(symbol, dex_config, target_config, target_exchange)
                    if isinstance(dex_config, dict)
                    else {"status": "missing_dex_identity", "reason": "DEX 缺少链或合约地址"}
                )
        if errors:
            summary["lastError"] = "; ".join(errors[:10])

    status_counts: dict[str, int] = {}
    for candidate in dex_candidates:
        status = str((candidate.get("dexIdentity") or {}).get("status") or "unverified")
        status_counts[status] = status_counts.get(status, 0) + 1
    summary["statusCounts"] = status_counts
    summary["verifiedCount"] = status_counts.get("verified", 0)
    summary["blockedCount"] = len(dex_candidates) - summary["verifiedCount"]
    blocked_items = [
        {
            "symbol": candidate.get("symbol"),
            "targetExchange": candidate.get("sellExchange"),
            "chainIndex": (candidate.get("dexConfig") or {}).get("chainIndex"),
            "status": (candidate.get("dexIdentity") or {}).get("status"),
            "reason": (candidate.get("dexIdentity") or {}).get("reason"),
        }
        for candidate in dex_candidates
        if (candidate.get("dexIdentity") or {}).get("status") != "verified"
    ]
    summary["blockedItems"] = sorted(
        blocked_items,
        key=lambda item: (
            str(item.get("symbol") or ""),
            str(item.get("chainIndex") or ""),
            str(item.get("status") or ""),
        ),
    )[:50]
    return candidates, summary


def _log_okxdex_identity_summary(summary: dict[str, Any]) -> None:
    global _dex_identity_log_signature, _dex_identity_log_at
    signature = json.dumps(
        {
            "blockedItems": summary.get("blockedItems"),
            "lastError": summary.get("lastError"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    now = time.monotonic()
    with _dex_identity_log_lock:
        # Candidate counts naturally fluctuate every five seconds. Log a
        # consolidated snapshot at most once per five minutes unless an error
        # appears for the first time.
        new_error = bool(summary.get("lastError")) and signature != _dex_identity_log_signature
        if not new_error and now - _dex_identity_log_at < 300:
            return
        _dex_identity_log_signature = signature
        _dex_identity_log_at = now
    blocked_count = int(summary.get("blockedCount") or 0)
    append_system_runtime_event(
        "astro_okxdex_identity_verified",
        level="warning" if blocked_count or summary.get("lastError") else "info",
        source="backend",
        module="astro_spread_scanner",
        message=(
            f"OKXDEX 身份复核：通过 {int(summary.get('verifiedCount') or 0)}，拦截 {blocked_count}。"
        ),
        details=summary,
    )


def _annotate_okxdex_manual_mappings(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity_verification_enabled = spread_okxdex_identity_verification_enabled()
    relevant = [
        candidate
        for candidate in candidates
        if str(candidate.get("type") or "").upper() == "SF"
        and str(candidate.get("buyExchange") or "").lower() in SF_DEX_EXCHANGES
        and str(candidate.get("sellExchange") or "").lower() in SF_OKXDEX_FUTURES_EXCHANGES
        and (
            not identity_verification_enabled
            or (candidate.get("dexIdentity") or {}).get("status") == "verified"
        )
    ]
    confirmed_count = 0
    missing_by_asset: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in relevant:
        mapping = _dex_mapping_confirmation(candidate)
        candidate["dexMapping"] = mapping
        if mapping["status"] == "confirmed":
            confirmed_count += 1
            continue
        identity = (mapping["exchange"], mapping["symbol"], mapping["chainIndex"], mapping["contractAddress"])
        item = missing_by_asset.setdefault(
            identity,
            {
                "symbol": mapping["symbol"],
                "exchange": mapping["exchange"],
                "chainIndex": mapping["chainIndex"],
                "chainLabel": mapping["chainLabel"],
                "contractAddress": mapping["contractAddress"],
                "targetExchanges": [],
                "maxOpenSpreadPct": None,
                "maxVolume24hUsdt": None,
                "reason": mapping["reason"],
            },
        )
        target_exchange = str(candidate.get("sellExchange") or "").lower()
        if target_exchange and target_exchange not in item["targetExchanges"]:
            item["targetExchanges"].append(target_exchange)
        opening = _finite(candidate.get("openSpreadPct"))
        volume = _finite(candidate.get("sellVolume24hUsdt"))
        if opening is not None:
            item["maxOpenSpreadPct"] = max(opening, item["maxOpenSpreadPct"] or opening)
        if volume is not None:
            item["maxVolume24hUsdt"] = max(volume, item["maxVolume24hUsdt"] or volume)
    missing_items = sorted(
        missing_by_asset.values(),
        key=lambda item: (-float(item.get("maxOpenSpreadPct") or 0.0), item["symbol"]),
    )[:50]
    for item in missing_items:
        item["targetExchanges"].sort()
    return candidates, {
        "mappingConfirmationRequired": True,
        "confirmedCount": confirmed_count,
        "missingCount": len(missing_items),
        "missingItems": missing_items,
    }


def _merge_pending_dex_mapping_items(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep a discovered missing mapping visible until the user confirms it."""

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in [*previous, *current]:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper().removesuffix("USDT")
        chain_index = str(raw.get("chainIndex") or "").strip()
        contract_address = _normalized_contract_address(chain_index, raw.get("contractAddress"))
        if not symbol or not chain_index or not contract_address:
            continue
        candidate = {
            "symbol": symbol,
            "buyExchange": str(raw.get("exchange") or "okxdex"),
            "dexConfig": {"chainIndex": chain_index, "contractAddress": contract_address},
        }
        if _dex_mapping_confirmation(candidate).get("status") == "confirmed":
            continue
        identity = (str(raw.get("exchange") or "okxdex"), symbol, chain_index, contract_address)
        item = merged.setdefault(
            identity,
            {
                "symbol": symbol,
                "chainIndex": chain_index,
                "chainLabel": str(raw.get("chainLabel") or OKXDEX_CHAIN_LABELS.get(chain_index, chain_index)),
                "contractAddress": contract_address,
                "targetExchanges": [],
                "maxOpenSpreadPct": None,
                "maxVolume24hUsdt": None,
                "reason": "请在 Astro 配置后确认链和合约地址",
                "exchange": str(raw.get("exchange") or "okxdex"),
            },
        )
        for exchange in raw.get("targetExchanges") or []:
            normalized_exchange = str(exchange or "").strip().lower()
            if normalized_exchange and normalized_exchange not in item["targetExchanges"]:
                item["targetExchanges"].append(normalized_exchange)
        opening = _finite(raw.get("maxOpenSpreadPct"))
        volume = _finite(raw.get("maxVolume24hUsdt"))
        if opening is not None:
            item["maxOpenSpreadPct"] = max(opening, item["maxOpenSpreadPct"] or opening)
        if volume is not None:
            item["maxVolume24hUsdt"] = max(volume, item["maxVolume24hUsdt"] or volume)
    result = sorted(
        merged.values(),
        key=lambda item: (-float(item.get("maxOpenSpreadPct") or 0.0), item["symbol"]),
    )[:50]
    for item in result:
        item["targetExchanges"].sort()
    return result


def _pulse_exchange_market(key: str) -> tuple[str, str] | None:
    if key.endswith("Spot"):
        return key[:-4].lower(), "spot"
    if key.endswith("Future"):
        return key[:-6].lower(), "future"
    return None


PULSE_TO_CRYPTO_EXCHANGE = {
    "binance": "bn",
    "bybit": "by",
    "bitget": "bg",
    "okx": "okx",
    "gate": "gt",
    "aster": "as",
    "hl": "hl",
}
CRYPTO_TO_PULSE_EXCHANGE = {value: key for key, value in PULSE_TO_CRYPTO_EXCHANGE.items()}


def _load_pulse_symbol_aliases() -> dict[tuple[str, str, str], tuple[str, float]]:
    """Return (Pulse exchange, market, raw symbol) -> (canonical symbol, quote divisor)."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import CryptoSymbolMapping

    inverse_exchange = {value: key for key, value in PULSE_TO_CRYPTO_EXCHANGE.items()}
    aliases: dict[tuple[str, str, str], tuple[str, float]] = {}
    with SessionLocal() as db:
        for mapping in db.scalars(select(CryptoSymbolMapping)):
            exchange = inverse_exchange.get(mapping.exchange)
            market = "spot" if mapping.market_type == "spot" else "future"
            raw_symbol = str(mapping.mapped_symbol or "").strip().upper().removesuffix("USDT")
            canonical = str(mapping.input_symbol or "").strip().upper().removesuffix("USDT")
            ratio = float(mapping.price_ratio or 1.0)
            if exchange and raw_symbol and canonical and math.isfinite(ratio) and ratio > 0:
                aliases[(exchange, market, raw_symbol)] = (canonical, ratio)
    return aliases


def _spread_pct(buy_price: float, sell_price: float) -> float | None:
    denominator = buy_price + sell_price
    if buy_price <= 0 or sell_price <= 0 or denominator <= 0:
        return None
    value = 2 * (sell_price - buy_price) / denominator * 100
    return value if -100 < value < 100 else None


def _candidate(
    pair_type: str,
    symbol: str,
    buy: dict[str, Any],
    sell: dict[str, Any],
) -> dict[str, Any] | None:
    opening = _spread_pct(buy["ask"], sell["bid"])
    closing = _spread_pct(buy["bid"], sell["ask"])
    if opening is None or closing is None:
        return None
    candidate = {
        "key": f"{pair_type}:{symbol}:{buy['exchange']}:{sell['exchange']}",
        "type": pair_type,
        "symbol": symbol.removesuffix("USDT"),
        "marketSymbol": symbol,
        "buyExchange": buy["exchange"],
        "sellExchange": sell["exchange"],
        "openSpreadPct": opening,
        "closeSpreadPct": closing,
        "buyMarket": buy["market"],
        "sellMarket": sell["market"],
        "buyFundingRatePct": buy.get("fundingRatePct") if buy["market"] == "future" else None,
        "sellFundingRatePct": sell.get("fundingRatePct") if sell["market"] == "future" else None,
        # Keep the raw string (including -0.000) separate from exact API evidence.
        "sellPulseFunding": sell.get("pulseFunding") if pair_type == "SF" else None,
        "buyVolume24hUsdt": buy.get("volume24hUsdt"),
        "sellVolume24hUsdt": sell.get("volume24hUsdt"),
        "sellFundingPeriodHours": sell.get("fundingPeriodHours"),
        "listingVolumeWindows": {
            "buy": buy.get("listingEventAtMs"),
            "sell": sell.get("listingEventAtMs"),
        },
        # Listing priority is short-lived and comes only from an active
        # exchange-listing watch. It bypasses the 24h-volume gate while a new
        # market is still accumulating its first day of turnover.
        "priorityNewListing": bool(
            buy.get("priorityNewListing") or sell.get("priorityNewListing")
        ),
        "netFundingRatePct": (
            sell.get("fundingRatePct") - buy.get("fundingRatePct")
            if buy["market"] == "future"
            and sell["market"] == "future"
            and buy.get("fundingRatePct") is not None
            and sell.get("fundingRatePct") is not None
            else None
        ),
        "quoteAt": min(buy["timestamp"], sell["timestamp"]),
        # Preserve the exact Pulse legs that produced the initial spread.  The
        # final revalidation already records direct executable quotes, but
        # without this evidence a later audit cannot distinguish a fleeting
        # opportunity from an asynchronous/stale aggregate snapshot.
        "quoteSource": "astro_pulse_aggregate",
        "quoteSkewSeconds": round(abs(buy["timestamp"] - sell["timestamp"]) / 1000, 3),
        "buyQuote": {
            "exchange": buy["exchange"],
            "market": buy["market"],
            "bid": buy["bid"],
            "ask": buy["ask"],
            "timestamp": buy["timestamp"],
        },
        "sellQuote": {
            "exchange": sell["exchange"],
            "market": sell["market"],
            "bid": sell["bid"],
            "ask": sell["ask"],
            "timestamp": sell["timestamp"],
        },
    }
    if pair_type == "SF" and buy.get("dexConfig"):
        candidate["dexConfig"] = dict(buy["dexConfig"])
        chain_index = str(candidate["dexConfig"].get("chainIndex") or "").strip()
        contract_address = _normalized_contract_address(
            chain_index,
            candidate["dexConfig"].get("contractAddress"),
        )
        fingerprint = hashlib.sha256(f"{chain_index}:{contract_address}".encode("utf-8")).hexdigest()[:12]
        candidate["key"] = f"{candidate['key']}:{chain_index}:{fingerprint}"
    return candidate


def _sf_route_supported(buy_exchange: str, sell_exchange: str) -> bool:
    if buy_exchange in SF_DEX_EXCHANGES:
        return sell_exchange in SF_OKXDEX_FUTURES_EXCHANGES
    return (
        buy_exchange in SF_AUTO_CARD_SPOT_EXCHANGES
        and sell_exchange not in SF_AUTO_CARD_EXCLUDED_FUTURES
    )


def _active_listing_leg_window(candidate: dict[str, Any], leg: str, *, now_ms: int | None = None) -> bool:
    windows = candidate.get("listingVolumeWindows") or {}
    event_ms = _finite(windows.get(leg)) if isinstance(windows, dict) else None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return event_ms is not None and 0 <= now_ms - event_ms < 2 * 3600 * 1000


def _auto_card_volume_check(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Require published 24h notional for both executable legs.

    Pulse occasionally omits ``trade24Count``.  Missing is not evidence of
    sufficient liquidity and must therefore fail closed for card creation.
    """

    threshold = spread_scan_min_volume_usdt()
    if threshold <= 0:
        return True, "disabled"
    now_ms = int(time.time() * 1000)
    bypassed = False
    for leg in ("buy", "sell"):
        if _active_listing_leg_window(candidate, leg, now_ms=now_ms):
            bypassed = True
            continue
        volume = _finite(candidate.get(f"{leg}Volume24hUsdt"))
        if volume is None:
            return False, "volume_unavailable"
        if volume < threshold:
            return False, "volume_below_threshold"
    return True, "new_listing_bypass" if bypassed else "eligible"


def _meets_auto_card_rule(candidate: dict[str, Any]) -> bool:
    opening = _finite(candidate.get("openSpreadPct"))
    # The left-hand/opening direction is the only value that may qualify a
    # card.  A positive reverse/closing spread must never compensate for a
    # zero or negative executable opening spread.
    if opening is None or opening <= 0 or opening > _route_max_open_pct(candidate):
        return False
    volume_eligible, _ = _auto_card_volume_check(candidate)
    if not volume_eligible:
        return False
    pair_type = str(candidate.get("type") or "").upper()
    buy_exchange = str(candidate.get("buyExchange") or "").lower()
    sell_exchange = str(candidate.get("sellExchange") or "").lower()
    if pair_type == "FF":
        base_eligible = opening > spread_scan_ff_min_open_pct() and bool(astro_spread_card_routes(candidate))
        if not base_eligible or not structure_filter_enabled():
            return base_eligible
        assessment = candidate.get("structureAssessment")
        return isinstance(assessment, dict) and assessment.get("autoCardEligible") is True
    if pair_type == "SF":
        dex_config = candidate.get("dexConfig")
        identity_verification_enabled = spread_okxdex_identity_verification_enabled()
        dex_ready = (
            buy_exchange not in SF_DEX_EXCHANGES
            or (
                isinstance(dex_config, dict)
                and bool(str(dex_config.get("chainIndex") or "").strip())
                and bool(str(dex_config.get("contractAddress") or "").strip())
                and (
                    not identity_verification_enabled
                    or (candidate.get("dexIdentity") or {}).get("status") == "verified"
                )
                and (
                    (candidate.get("dexMapping") or {}).get("status") == "confirmed"
                )
            )
        )
        return (
            opening > spread_scan_sf_route_min_open_pct(buy_exchange)
            and buy_exchange in SF_AUTO_CARD_SPOT_EXCHANGES
            and (buy_exchange not in SF_DEX_EXCHANGES or spread_scan_dex_auto_card_enabled(buy_exchange))
            and str(candidate.get("buyMarket") or "").lower() == "spot"
            and str(candidate.get("sellMarket") or "").lower() == "future"
            and _sf_route_supported(buy_exchange, sell_exchange)
            and dex_ready
        )
    return False


def _candidate_rule_decision(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return an explainable rule result for one Pulse route.

    This mirrors :func:`_meets_auto_card_rule` but preserves the first blocking
    rule and every input/threshold needed for later missed-opportunity review.
    """

    pair_type = str(candidate.get("type") or "").upper()
    buy_exchange = str(candidate.get("buyExchange") or "").lower()
    sell_exchange = str(candidate.get("sellExchange") or "").lower()
    opening = _finite(candidate.get("openSpreadPct"))
    volume_ok, volume_reason = _auto_card_volume_check(candidate)
    threshold = (
        spread_scan_ff_min_open_pct()
        if pair_type == "FF"
        else spread_scan_sf_route_min_open_pct(buy_exchange)
        if pair_type == "SF"
        else None
    )
    failures: list[str] = []
    if opening is None:
        failures.append("spread_unavailable")
    elif opening <= 0:
        failures.append("open_spread_non_positive")
    elif opening > _route_max_open_pct(candidate):
        failures.append("spread_above_safety_max")
    elif threshold is None:
        failures.append("unsupported_pair_type")
    elif opening <= threshold:
        failures.append("spread_below_threshold")
    if not volume_ok:
        failures.append(volume_reason)
    if pair_type == "FF":
        if not astro_spread_card_routes(candidate):
            failures.append("unsupported_route")
        if structure_filter_enabled():
            assessment = candidate.get("structureAssessment")
            if not isinstance(assessment, dict) or assessment.get("autoCardEligible") is not True:
                failures.append("ff_structure_filter")
    elif pair_type == "SF":
        if buy_exchange not in SF_AUTO_CARD_SPOT_EXCHANGES:
            failures.append("unsupported_spot_exchange")
        if str(candidate.get("buyMarket") or "").lower() != "spot":
            failures.append("buy_leg_not_spot")
        if str(candidate.get("sellMarket") or "").lower() != "future":
            failures.append("sell_leg_not_future")
        if not _sf_route_supported(buy_exchange, sell_exchange):
            failures.append("unsupported_route")
        if buy_exchange in SF_DEX_EXCHANGES:
            if not spread_scan_dex_auto_card_enabled(buy_exchange):
                failures.append(f"sf_{buy_exchange}_auto_card_paused")
            dex_config = candidate.get("dexConfig") if isinstance(candidate.get("dexConfig"), dict) else {}
            if not str(dex_config.get("chainIndex") or "").strip() or not str(
                dex_config.get("contractAddress") or ""
            ).strip():
                failures.append("dex_chain_or_address_missing")
            if spread_okxdex_identity_verification_enabled():
                identity_status = str((candidate.get("dexIdentity") or {}).get("status") or "unverified")
                if identity_status != "verified":
                    failures.append(f"dex_identity_{identity_status}")
            mapping_status = str((candidate.get("dexMapping") or {}).get("status") or "missing")
            if mapping_status != "confirmed":
                failures.append("dex_mapping_unconfirmed")
    failures = list(dict.fromkeys(failures))
    return {
        "eligible": not failures and _meets_auto_card_rule(candidate),
        "primaryReason": failures[0] if failures else "eligible",
        "failedRules": failures,
        "values": {
            "openSpreadPct": opening,
            "buyVolume24hUsdt": _finite(candidate.get("buyVolume24hUsdt")),
            "sellVolume24hUsdt": _finite(candidate.get("sellVolume24hUsdt")),
            "fundingIncluded": False,
            "quoteSource": candidate.get("quoteSource"),
            "quoteAt": candidate.get("quoteAt"),
            "quoteSkewSeconds": _finite(candidate.get("quoteSkewSeconds")),
            "buyQuote": candidate.get("buyQuote"),
            "sellQuote": candidate.get("sellQuote"),
        },
        "thresholds": {
            "minOpenSpreadPctExclusive": threshold,
            "maxOpenSpreadPct": spread_scan_max_open_pct(),
            "minVolumeUsdtPerLeg": spread_scan_min_volume_usdt(),

        },
    }


def _decision_error_category(error: Any) -> str | None:
    text = str(error or "").strip().lower()
    if not text:
        return None
    if "429" in text or "too many requests" in text:
        return "http_429"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "502" in text or "503" in text or "521" in text:
        return "upstream_unavailable"
    if "depth" in text or "深度" in text:
        return "depth_unavailable"
    if "connection" in text or "连接" in text:
        return "connection_error"
    return "other"


def _append_possible_missed_opportunity(
    identity: tuple[str, str, str, str, str, str],
    *,
    decision: str,
    details: dict[str, Any],
) -> bool:
    """Log only actionable blocked opportunities, not every routine miss."""

    if decision in {"sf_negative", "sf_pulse_negative", "sf_funding_pending"}:
        return False

    # Existing, in-flight and queued cards are already handled. Reporting
    # them as missed opportunities is both false and noisy.
    if astro_route_dedupe_state(identity) is not None:
        return False

    report = details.get("report") if isinstance(details.get("report"), dict) else {}
    pulse_spread = _finite(details.get("pulseOpenSpreadPct"))
    direct_spread = _finite(report.get("latestOpenSpreadPct"))
    threshold = (
        spread_scan_ff_min_open_pct()
        if identity[1] == "FF"
        else spread_scan_sf_route_min_open_pct(identity[2])
        if identity[1] == "SF"
        else None
    )
    if threshold is None:
        return False
    buy_quote, sell_quote = report.get("buyQuote") or {}, report.get("sellQuote") or {}
    buy_age, sell_age = _finite(buy_quote.get("quoteAgeSeconds")), _finite(sell_quote.get("quoteAgeSeconds"))
    skew = _finite(report.get("quoteSkewSeconds"))
    dex = bool(SF_DEX_EXCHANGES & set(identity[2:4]))
    limits = report.get("quoteAgeLimitsSeconds") or {}
    buy_limit = _finite(limits.get("buy")) or (spread_final_revalidation_okxdex_submit_max_quote_age_seconds() if dex else spread_final_revalidation_max_quote_age_seconds())
    sell_limit = _finite(limits.get("sell")) or spread_final_revalidation_max_quote_age_seconds()
    # A numeric spread alone is not executable evidence. Stale/asynchronous
    # books belong in the unavailable-verification summary, not missed trades.
    fresh_depth = bool(report.get("okxdexExecutablePreflight") if dex else report.get("cexExecutablePreflight"))
    fresh_depth = fresh_depth and buy_age is not None and sell_age is not None and 0 <= buy_age <= buy_limit and 0 <= sell_age <= sell_limit
    if not dex:
        fresh_depth = fresh_depth and skew is not None and 0 <= skew <= spread_final_revalidation_max_skew_seconds()
    confirmed_above = fresh_depth and direct_spread is not None and direct_spread > threshold
    unavailable = decision in {
        "cex_executable_depth_unavailable",
        "okxdex_executable_quote_unavailable",
        "direct_quote_unavailable",
        "direct_funding_unavailable",
        "local_depth_deadline_exceeded",
        "stale_direct_quote",
        "direct_quote_time_skew",
        "quote_time_skew",
    }
    # A transport failure proves only that verification was unavailable.  It
    # is grouped at exchange level by the depth-health incident logger and must
    # not be counted as a route-level missed opportunity.
    if unavailable:
        return False
    now = time.monotonic()
    streak_key = (*identity, "hot_direct_check")
    with _decision_audit_lock:
        _direct_failure_streaks.pop(streak_key, None)

        # Only a fresh executable spread above threshold is a confirmed
        # safety-rule block. API failures are reported separately as incidents.
        if not confirmed_above:
            return False
        confidence = "confirmed_executable_spread"
        log_key = (*identity, decision, confidence)
        if now - float(_missed_opportunity_last.get(log_key) or 0.0) < 300:
            return False
        _missed_opportunity_last[log_key] = now
        if len(_missed_opportunity_last) > 2_000:
            cutoff = now - 1800
            for old_key in list(_missed_opportunity_last):
                if _missed_opportunity_last[old_key] < cutoff:
                    _missed_opportunity_last.pop(old_key, None)

    error = report.get("error")
    funding = report.get("sfFundingCheck") or report.get("fundingPrecheck") or {}
    append_system_runtime_event(
        "astro_possible_missed_opportunity",
        level="warning",
        source="backend",
        module="astro_decision_audit",
        message=(
            f"Astro 可能错过机会：{identity[0]} {identity[1]} "
            f"{identity[2]}/{identity[3]} · {decision}"
        ),
        details={
            "symbol": identity[0],
            "type": identity[1],
            "buyEx": identity[2],
            "sellEx": identity[3],
            "chainIndex": identity[4] or None,
            "contractAddress": identity[5] or None,
            "stage": "hot_direct_check",
            "primaryReason": decision,
            "confidence": confidence,
            "thresholdPct": threshold,
            "pulseOpenSpreadPct": pulse_spread,
            "directOpenSpreadPct": direct_spread,
            "quoteSkewSeconds": _finite(report.get("quoteSkewSeconds")),
            "buyQuoteAgeSeconds": _finite((report.get("buyQuote") or {}).get("quoteAgeSeconds")),
            "sellQuoteAgeSeconds": _finite((report.get("sellQuote") or {}).get("quoteAgeSeconds")),
            "durationMs": _finite(report.get("durationMs")),
            "consecutiveFailureCount": 0,
            "errorCategory": funding.get("errorCategory") or _decision_error_category(error),
            "errorType": funding.get("errorType"),
            "fundingFailureCount": funding.get("failureCount"),
            "error": str(error)[:800] if error else None,
        },
    )
    return True


def _append_decision_audit(
    identity: tuple[str, str, str, str, str, str],
    *,
    stage: str,
    decision: str,
    details: dict[str, Any],
) -> bool:
    # Pulse snapshots can contain hundreds of above-threshold cross routes.
    # Routine rule/selection results are useful as a five-minute diagnostic
    # summary, but one warning per route hides actual creation failures.
    aggregate_stage = stage in {
        "rule_filter",
        "target_selection",
        "hot_direct_monitor",
        "hot_watch_registration",
        "hot_direct_check",
    }
    if aggregate_stage:
        missed_logged = (
            _append_possible_missed_opportunity(identity, decision=decision, details=details)
            if stage == "hot_direct_check"
            else False
        )
        summary: dict[str, Any] | None = None
        now = time.monotonic()
        with _decision_audit_lock:
            reasons = _decision_audit_summary.setdefault("reasons", {})
            reasons[decision] = int(reasons.get(decision) or 0) + 1
            _decision_audit_summary["count"] = int(_decision_audit_summary.get("count") or 0) + 1
            samples = _decision_audit_summary.setdefault("samples", [])
            sample = {
                "symbol": identity[0],
                "type": identity[1],
                "buyEx": identity[2],
                "sellEx": identity[3],
                "stage": stage,
                "decision": decision,
                "openSpreadPct": _finite(
                    (details.get("values") or {}).get("openSpreadPct")
                    if stage != "hot_direct_check"
                    else details.get("pulseOpenSpreadPct")
                ),
            }
            report = details.get("report") if isinstance(details.get("report"), dict) else {}
            funding = report.get("sfFundingCheck") or report.get("fundingPrecheck") or {}
            sample.update({"transport": report.get("transport"),
                           "errorCategory": funding.get("errorCategory") or _decision_error_category(report.get("error")),
                           "errorType": funding.get("errorType"),
                           "fundingStatus": funding.get("status"),
                           "error": str(report.get("error") or "")[:400] or None})
            if stage == "hot_direct_check" and decision in {"stale_direct_quote", "direct_quote_time_skew"}:
                timing_samples = _decision_audit_summary.setdefault("quoteTimingSamples", [])
                timing_key = (*identity[:4], decision)
                if len(timing_samples) < 20 and not any(
                    (item["symbol"], item["type"], item["buyEx"], item["sellEx"], item["decision"]) == timing_key
                    for item in timing_samples
                ):
                    buy_quote = report.get("buyQuote") or {}
                    sell_quote = report.get("sellQuote") or {}
                    timing_samples.append({
                        "symbol": identity[0], "type": identity[1],
                        "buyEx": identity[2], "sellEx": identity[3],
                        "decision": decision, "transport": report.get("transport"),
                        "quoteSkewSeconds": _finite(report.get("quoteSkewSeconds")),
                        "quoteAgeLimitsSeconds": report.get("quoteAgeLimitsSeconds"),
                        "buyQuoteAgeSeconds": _finite(buy_quote.get("quoteAgeSeconds")),
                        "sellQuoteAgeSeconds": _finite(sell_quote.get("quoteAgeSeconds")),
                        "buyTimestampSource": buy_quote.get("timestampSource"),
                        "sellTimestampSource": sell_quote.get("timestampSource"),
                        "buyRequestDurationMs": _finite(buy_quote.get("requestDurationMs")),
                        "sellRequestDurationMs": _finite(sell_quote.get("requestDurationMs")),
                    })
            duration_ms = _finite(report.get("durationMs"))
            direct_spread = _finite(report.get("latestOpenSpreadPct"))
            if duration_ms is not None:
                _decision_audit_summary["durationCount"] = int(
                    _decision_audit_summary.get("durationCount") or 0
                ) + 1
                _decision_audit_summary["durationMsTotal"] = float(
                    _decision_audit_summary.get("durationMsTotal") or 0.0
                ) + duration_ms
                _decision_audit_summary["durationMsMax"] = max(
                    float(_decision_audit_summary.get("durationMsMax") or 0.0), duration_ms
                )
            route_key = f"{identity[0]}:{identity[1]}:{identity[2]}/{identity[3]}"
            route_stats = _decision_audit_summary.setdefault("routeStats", {})
            route = route_stats.get(route_key)
            if route is None and len(route_stats) < 200:
                route = {
                    "symbol": identity[0],
                    "type": identity[1],
                    "buyEx": identity[2],
                    "sellEx": identity[3],
                    "count": 0,
                    "maxPulseSpreadPct": None,
                    "maxDirectSpreadPct": None,
                    "lastDecision": None,
                    "errorCounts": {},
                }
                route_stats[route_key] = route
            pulse = _finite(details.get("pulseOpenSpreadPct")) or sample["openSpreadPct"]
            if route is not None:
                route["count"] = int(route.get("count") or 0) + 1
                if pulse is not None:
                    route["maxPulseSpreadPct"] = max(
                        pulse, _finite(route.get("maxPulseSpreadPct")) or pulse
                    )
                if direct_spread is not None:
                    route["maxDirectSpreadPct"] = max(
                        direct_spread, _finite(route.get("maxDirectSpreadPct")) or direct_spread
                    )
                route["lastDecision"] = decision
                route["lastError"] = str(report.get("error") or "")[:400] or None
                route["lastTransport"] = report.get("transport")
                error_category = _decision_error_category(report.get("error"))
                if error_category:
                    error_counts = route.setdefault("errorCounts", {})
                    error_counts[error_category] = int(error_counts.get(error_category) or 0) + 1
            if len(samples) < 30 and sample not in samples:
                samples.append(sample)
            if now - float(_decision_audit_summary.get("lastLoggedAtMonotonic") or 0.0) >= 300:
                duration_count = int(_decision_audit_summary.get("durationCount") or 0)
                top_routes = sorted(
                    (dict(item) for item in route_stats.values()),
                    key=lambda item: (
                        _finite(item.get("maxPulseSpreadPct")) or float("-inf"),
                        int(item.get("count") or 0),
                    ),
                    reverse=True,
                )[:30]
                summary = {
                    "windowStartedAt": _decision_audit_summary.get("startedAt"),
                    "windowEndedAt": datetime.now(timezone.utc).isoformat(),
                    "count": int(_decision_audit_summary.get("count") or 0),
                    "reasons": dict(reasons),
                    "samples": list(samples),
                    "quoteTimingSamples": list(_decision_audit_summary.get("quoteTimingSamples") or []),
                    "directCheckLatencyMs": {
                        "count": duration_count,
                        "average": round(
                            float(_decision_audit_summary.get("durationMsTotal") or 0.0)
                            / max(1, duration_count),
                            1,
                        ),
                        "max": round(float(_decision_audit_summary.get("durationMsMax") or 0.0), 1),
                    },
                    "topRoutes": top_routes,
                }
                _decision_audit_summary.update(
                    {
                        "startedAt": datetime.now(timezone.utc).isoformat(),
                        "lastLoggedAtMonotonic": now,
                        "count": 0,
                        "reasons": {},
                        "samples": [],
                        "quoteTimingSamples": [],
                        "durationCount": 0,
                        "durationMsTotal": 0.0,
                        "durationMsMax": 0.0,
                        "routeStats": {},
                    }
                )
        if summary is None:
            return missed_logged
        append_system_runtime_event(
            "astro_auto_card_decision_summary",
            level="info",
            source="backend",
            module="astro_decision_audit",
            message=(
                f"Astro 建卡候选5分钟汇总：{summary['count']} 条，"
                f"{len(summary['reasons'])} 类结果。"
            ),
            details=summary,
        )
        return True

    key = (*identity, stage, decision)
    now = time.monotonic()
    with _decision_audit_lock:
        last_at = _decision_audit_last.get(key, 0.0)
        if now - last_at < 300:
            return False
        _decision_audit_last[key] = now
        if len(_decision_audit_last) > 5_000:
            cutoff = now - 1800
            for old_key in list(_decision_audit_last):
                if _decision_audit_last[old_key] < cutoff:
                    _decision_audit_last.pop(old_key, None)
    append_system_runtime_event(
        "astro_auto_card_decision_audit",
        level="info"
        if stage == "final_revalidation" or decision in {"eligible", "registered_for_direct_check", "created", "passed"}
        else "warning",
        source="backend",
        module="astro_decision_audit",
        message=(
            f"Astro 建卡决策：{identity[0]} {identity[1]} "
            f"{identity[2]}/{identity[3]} · {stage} · {decision}"
        ),
        details={
            "symbol": identity[0],
            "type": identity[1],
            "buyEx": identity[2],
            "sellEx": identity[3],
            "chainIndex": identity[4] or None,
            "contractAddress": identity[5] or None,
            "stage": stage,
            "decision": decision,
            **details,
        },
    )
    return True


def _record_candidate_decision_audit(
    candidates: list[dict[str, Any]],
    selected_candidates: list[dict[str, Any]],
    confirmed_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_ids = {id(item) for item in selected_candidates}
    confirmed_routes = {_candidate_route_identity(item) for item in confirmed_candidates}
    reason_counts: dict[str, int] = {}
    audited = 0
    blocked = 0
    ff_threshold = spread_scan_ff_min_open_pct()
    for candidate in candidates:
        pair_type = str(candidate.get("type") or "").upper()
        opening = _finite(candidate.get("openSpreadPct"))
        threshold = ff_threshold if pair_type == "FF" else spread_scan_sf_route_min_open_pct(candidate.get("buyExchange")) if pair_type == "SF" else None
        # A below-threshold observation is not a missed auto-card opportunity.
        # Check this before building the detailed explanation: a full Pulse
        # snapshot can contain thousands of cross-routes, while only a handful
        # are actual above-threshold opportunities.
        if opening is None or threshold is None or opening <= threshold:
            continue
        decision = _candidate_rule_decision(candidate)
        identity = _candidate_route_identity(candidate)
        reason = str(decision["primaryReason"])
        stage = "rule_filter"
        if decision["eligible"] and id(candidate) not in selected_ids:
            reason = "dex_not_max_spread_or_volume"
            stage = "target_selection"
        elif decision["eligible"] and identity not in confirmed_routes:
            reason = "hot_monitor_not_registered"
            stage = "hot_direct_monitor"
        elif decision["eligible"]:
            reason = "registered_for_direct_check"
            stage = "hot_watch_registration"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        blocked += int(reason not in {"eligible", "registered_for_direct_check"})
        audited += int(
            _append_decision_audit(
                identity,
                stage=stage,
                decision=reason,
                details=decision,
            )
        )
    return {
        "aboveBaseThresholdCount": sum(reason_counts.values()),
        "blockedCount": blocked,
        "eventsWritten": audited,
        "reasonCounts": reason_counts,
        "rule": (
            "仅审计已超过基础开仓阈值的路线；常规低于阈值/接口失败按5分钟汇总；"
            "仅新鲜同步深度价差达标但被其他规则拦截，单独记录可能错过机会；陈旧/异步/API失败属于复核不可用"
        ),
    }


def _auto_card_route_observations(
    candidates: list[dict[str, Any]],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Describe only routes backed by fresh, complete Pulse bid/ask data."""

    observations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        pair_type = str(candidate.get("type") or "").strip().upper()
        buy_exchange = str(candidate.get("buyExchange") or "").strip().lower()
        sell_exchange = str(candidate.get("sellExchange") or "").strip().lower()
        if not all((symbol, pair_type, buy_exchange, sell_exchange)):
            continue
        if (
            buy_exchange in SF_DEX_EXCHANGES
            and spread_okxdex_identity_verification_enabled()
            and (candidate.get("dexIdentity") or {}).get("status") != "verified"
        ):
            continue
        identity = (symbol, pair_type, buy_exchange, sell_exchange)
        opening = _finite(candidate.get("openSpreadPct"))
        base = {
            "openSpreadPct": opening,
            "quoteAt": candidate.get("quoteAt"),
            "sellFundingRatePct": candidate.get("sellFundingRatePct"),
        }
        volume_eligible, volume_reason = _auto_card_volume_check(candidate)
        if not volume_eligible:
            observations[identity] = {
                **base,
                # Missing data is not proof that a live card became invalid;
                # known low volume is a real rule failure.
                "state": "unavailable" if volume_reason == "volume_unavailable" else "invalid",
                "reason": volume_reason,
            }
            continue
        if opening is None:
            observations[identity] = {**base, "state": "unavailable", "reason": "spread_unavailable"}
            continue
        if opening > _route_max_open_pct(candidate):
            observations[identity] = {**base, "state": "invalid", "reason": "spread_above_safety_max"}
        elif pair_type == "FF":
            if not astro_spread_card_routes(candidate):
                observations[identity] = {**base, "state": "unavailable", "reason": "route_not_supported"}
            elif opening <= spread_scan_ff_min_open_pct():
                observations[identity] = {**base, "state": "invalid", "reason": "spread_below_ff_threshold"}
            elif structure_filter_enabled() and (
                not isinstance(candidate.get("structureAssessment"), dict)
                or candidate["structureAssessment"].get("autoCardEligible") is not True
            ):
                observations[identity] = {**base, "state": "invalid", "reason": "ff_structure_filter"}
            else:
                observations[identity] = {**base, "state": "eligible", "reason": "eligible"}

            # The scanner keeps the better executable FF direction. Fresh
            # books for that unordered exchange pair also prove that the
            # opposite, previously-created direction is no longer eligible.
            reverse = (symbol, pair_type, sell_exchange, buy_exchange)
            observations.setdefault(
                reverse,
                {
                    **base,
                    "openSpreadPct": -opening,
                    "state": "invalid",
                    "reason": "ff_route_direction_changed",
                },
            )
        elif pair_type == "SF":
            eligible = _meets_auto_card_rule(candidate)
            observations[identity] = {**base, "state": "eligible" if eligible else "invalid",
                                      "reason": "eligible" if eligible else "spread_below_sf_threshold"}
    return observations


def _retained_auto_card_route_identities() -> set[tuple[str, str, str, str, str, str]]:
    """Return locally tracked routes that must remain observable below entry gates."""

    retained: set[tuple[str, str, str, str, str, str]] = set()
    for record in auto_created_route_records():
        symbol = str(record.get("name") or "").strip().upper().removesuffix("USDT")
        pair_type = str(record.get("type") or "").strip().upper()
        buy_exchange = str(record.get("buyEx") or "").strip().lower()
        sell_exchange = str(record.get("sellEx") or "").strip().lower()
        chain_index = str(record.get("dexChainIndex") or "").strip()
        contract_address = _normalized_contract_address(chain_index, record.get("dexContractAddress"))
        if all((symbol, pair_type, buy_exchange, sell_exchange)) and pair_type in {"FF", "SF"}:
            retained.add(
                (symbol, pair_type, buy_exchange, sell_exchange, chain_index, contract_address)
            )
    return retained


def _retained_market_legs(
    retained_routes: set[tuple[str, str, str, str, str, str]],
) -> set[tuple[str, str, str]]:
    legs: set[tuple[str, str, str]] = set()
    for symbol, pair_type, buy_exchange, sell_exchange, _chain, _contract in retained_routes:
        if pair_type == "SF":
            legs.add((symbol, buy_exchange, "spot"))
            legs.add((symbol, sell_exchange, "future"))
        elif pair_type == "FF":
            legs.add((symbol, buy_exchange, "future"))
            legs.add((symbol, sell_exchange, "future"))
    return legs


def _keep_scanned_candidate(
    candidate: dict[str, Any],
    retained_routes: set[tuple[str, str, str, str, str, str]],
    *,
    ff_min_open_pct: float,
    sf_min_open_pct: float,
    structure_history_enabled: bool,
    structure_min_open_pct: float,
    priority_symbols: set[str] | None = None,
) -> bool:
    identity = _candidate_route_identity(candidate)
    if identity in retained_routes:
        return True
    symbol = str(candidate.get("symbol") or "").strip().upper().removesuffix("USDT")
    if symbol and symbol in set(priority_symbols or ()):
        return True
    opening = _finite(candidate.get("openSpreadPct"))
    if opening is None:
        return False
    pair_type = str(candidate.get("type") or "").upper()
    if pair_type == "FF":
        if candidate.get("sellExchange") == "bybit":
            return astro_ff_bybit_sell_exception(candidate) and opening > ff_min_open_pct
        return opening > ff_min_open_pct or (
            structure_history_enabled and opening >= structure_min_open_pct
        )
    if pair_type == "SF":
        return opening > (spread_scan_sf_route_min_open_pct(candidate.get("buyExchange")) if candidate.get("buyExchange") in SF_DEX_EXCHANGES else sf_min_open_pct)
    return False


def scan_pulse_spreads(
    payloads: list[dict[str, Any]],
    now_ms: int | None = None,
    symbol_aliases: dict[tuple[str, str, str], tuple[str, float]] | None = None,
    retained_routes: set[tuple[str, str, str, str, str, str]] | None = None,
    priority_symbols: set[str] | None = None,
    listing_market_windows: dict[tuple[str, str, str], int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            for key, value in data.items():
                if key not in merged and isinstance(value, dict):
                    merged[key] = value
    allowed_markets = spread_scan_market_keys()
    min_volume_usdt = spread_scan_min_volume_usdt()
    blocked_pairs = {(item["marketKey"], item["symbol"]) for item in spread_scan_blocked_pairs()}
    blocked_coins = set(spread_scan_blocked_coins())
    aliases = _load_pulse_symbol_aliases() if symbol_aliases is None else symbol_aliases
    priority = {
        str(symbol or "").strip().upper().removesuffix("USDT")
        for symbol in (priority_symbols or set())
        if str(symbol or "").strip()
    }
    retained = set(retained_routes or ())
    retained_base_routes = {identity[:4] for identity in retained}
    retained_legs = _retained_market_legs(retained)
    ff_min_open_pct = spread_scan_ff_min_open_pct()
    bybit_sell_enabled = astro_ff_bybit_sell_exception_enabled()
    sf_min_open_pct = spread_scan_sf_min_open_pct()
    structure_history_enabled = structure_filter_enabled()
    structure_min_open_pct = structure_tracking_min_pct()
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    max_age_ms = spread_scan_max_quote_age_seconds() * 1000
    markets_by_symbol: dict[str, list[dict[str, Any]]] = {}
    market_count = 0
    for key, market in merged.items():
        if key not in allowed_markets:
            continue
        route = _pulse_exchange_market(key)
        timestamp = _finite(market.get("ts"))
        if route is None or timestamp is None:
            continue
        exchange, market_type = route
        if current_ms - timestamp > max_age_ms or timestamp - current_ms > 5_000:
            continue
        rows = market.get("list")
        if not isinstance(rows, list):
            continue
        market_count += 1
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_symbol = str(row.get("name") or "").strip().upper()
            raw_base = raw_symbol.removesuffix("USDT")
            ask = _finite(row.get("a"))
            bid = _finite(row.get("b"))
            volume = _finite(row.get("trade24Count"))
            funding_rate = None  # Exact funding evidence only comes from the final API gate.
            if not raw_symbol or ask is None or bid is None or ask <= 0 or bid <= 0 or ask < bid:
                continue
            alias = aliases.get((exchange, market_type, raw_base))
            canonical_base, quote_divisor = alias if alias is not None else (raw_base, 1.0)
            symbol = f"{canonical_base}USDT"
            if raw_base in blocked_coins or canonical_base in blocked_coins:
                continue
            if (key, raw_symbol) in blocked_pairs or (key, symbol) in blocked_pairs:
                continue
            # New routes require a published amount at or above the threshold.
            # A locally tracked route keeps its legs so cleanup/rearm can still
            # observe known low volume or temporarily missing volume.
            retained_leg = (canonical_base, exchange, market_type) in retained_legs
            priority_new_listing = canonical_base in priority or raw_base in priority
            if (
                min_volume_usdt > 0
                and (volume is None or volume < min_volume_usdt)
                and not retained_leg
                and not priority_new_listing
            ):
                continue
            ask /= quote_divisor
            bid /= quote_divisor
            dex_config = None
            if exchange in SF_DEX_EXCHANGES:
                chain_index = str(row.get("chainIndex") or "").strip()
                contract_address = str(row.get("ca") or row.get("contractAddress") or "").strip()
                if not chain_index or not contract_address:
                    continue
                dex_config = {
                    "name": canonical_base,
                    "exchange": exchange,
                    "chainIndex": chain_index,
                    "contractAddress": contract_address,
                    "quote": "USDT",
                    "slippage": f"{spread_okxdex_slippage_pct():g}",
                }
            markets_by_symbol.setdefault(symbol, []).append(
                {
                    "exchange": exchange,
                    "market": market_type,
                    "ask": ask,
                    "bid": bid,
                    "volume24hUsdt": volume,
                    "priorityNewListing": priority_new_listing,
                    "listingEventAtMs": (listing_market_windows or {}).get((canonical_base, exchange, market_type)),
                    "fundingRatePct": funding_rate,
                    "pulseFunding": {"rawRate": row.get("rate"), "marketAtMs": int(timestamp),
                                     "rawSymbol": raw_base} if market_type == "future" else None,
                    "timestamp": int(timestamp),
                    "dexConfig": dex_config,
                }
            )

    candidates: list[dict[str, Any]] = []
    for symbol, legs in markets_by_symbol.items():
        spots = [leg for leg in legs if leg["market"] == "spot"]
        futures = [leg for leg in legs if leg["market"] == "future"]
        for spot in spots:
            for future in futures:
                base_route = (
                    symbol.removesuffix("USDT"),
                    "SF",
                    str(spot["exchange"]),
                    str(future["exchange"]),
                )
                if (
                    not _sf_route_supported(str(spot["exchange"]), str(future["exchange"]))
                    and base_route not in retained_base_routes
                ):
                    continue
                candidate = _candidate("SF", symbol, spot, future)
                if candidate is not None and _keep_scanned_candidate(
                    candidate,
                    retained,
                    ff_min_open_pct=ff_min_open_pct,
                    sf_min_open_pct=sf_min_open_pct,
                    structure_history_enabled=structure_history_enabled,
                    structure_min_open_pct=structure_min_open_pct,
                    priority_symbols=priority_symbols,
                ):
                    candidates.append(candidate)
        for left_index, left in enumerate(futures):
            for right in futures[left_index + 1 :]:
                directional: list[dict[str, Any]] = []
                for buy, sell in ((left, right), (right, left)):
                    base_route = (
                        symbol.removesuffix("USDT"),
                        "FF",
                        str(buy["exchange"]),
                        str(sell["exchange"]),
                    )
                    supported = (
                        str(buy["exchange"]) in ASTRO_FF_BUY_EXCHANGES
                        and (str(sell["exchange"]) in ASTRO_FF_SELL_EXCHANGES or (str(sell["exchange"]) == "bybit" and bybit_sell_enabled))
                    )
                    if not supported and base_route not in retained_base_routes:
                        continue
                    candidate = _candidate("FF", symbol, buy, sell)
                    if candidate is not None and _keep_scanned_candidate(
                        candidate,
                        retained,
                        ff_min_open_pct=ff_min_open_pct,
                        sf_min_open_pct=sf_min_open_pct,
                        structure_history_enabled=structure_history_enabled,
                        structure_min_open_pct=structure_min_open_pct,
                        priority_symbols=priority_symbols,
                    ):
                        directional.append(candidate)
                if not directional:
                    continue
                retained_directional = [
                    item for item in directional if _candidate_route_identity(item) in retained
                ]
                selected = {
                    _candidate_route_identity(item): item for item in retained_directional
                }
                best_new = max(directional, key=lambda item: item["openSpreadPct"])
                selected[_candidate_route_identity(best_new)] = best_new
                candidates.extend(selected.values())
    candidates.sort(key=lambda item: item["openSpreadPct"], reverse=True)
    return candidates, market_count


def _recent_index_change_scopes(candidates: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Return symbol/exchange scopes with a recorded component or weight change in the lookback."""
    from app.database import SessionLocal
    from app.models import CryptoIndexComponentChangeLog

    symbols = {
        str(candidate.get("symbol") or "").strip().upper()
        for candidate in candidates
        if str(candidate.get("type") or "").upper() == "FF"
    }
    exchange_codes = {
        PULSE_TO_CRYPTO_EXCHANGE[exchange]
        for candidate in candidates
        for exchange in (
            str(candidate.get("buyExchange") or "").lower(),
            str(candidate.get("sellExchange") or "").lower(),
        )
        if exchange in PULSE_TO_CRYPTO_EXCHANGE
    }
    if not symbols or not exchange_codes:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=structure_history_hours())
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(CryptoIndexComponentChangeLog.symbol, CryptoIndexComponentChangeLog.exchange).where(
                    CryptoIndexComponentChangeLog.symbol.in_(symbols),
                    CryptoIndexComponentChangeLog.exchange.in_(exchange_codes),
                    CryptoIndexComponentChangeLog.created_at >= cutoff,
                )
            ).all()
    except Exception:
        # Missing index evidence must never turn an otherwise structural gap
        # into an auto-card candidate. Treat it as no detected override.
        return set()
    return {
        (str(symbol).upper(), CRYPTO_TO_PULSE_EXCHANGE.get(str(exchange).lower(), str(exchange).lower()))
        for symbol, exchange in rows
    }


def _gate_index_fingerprint(symbol: str) -> str | None:
    normalized = str(symbol or "").strip().upper().removesuffix("USDT")
    if not normalized or not normalized.isalnum():
        return None
    now = time.monotonic()
    with _gate_index_lock:
        cached = _gate_index_cache.get(normalized)
        if cached and cached[0] > now:
            return cached[1]
    fingerprint: str | None = None
    ttl = 60.0
    try:
        response = httpx.get(
            f"https://api.gateio.ws/api/v4/futures/usdt/index_constituents/{normalized}_USDT",
            timeout=5.0,
            headers={"Accept": "application/json", "User-Agent": "stock-review-mac/astro-spread-scanner"},
        )
        response.raise_for_status()
        payload = response.json()
        constituents = payload.get("constituents") if isinstance(payload, dict) else None
        normalized_constituents = []
        if isinstance(constituents, list):
            for item in constituents:
                if not isinstance(item, dict):
                    continue
                exchange = str(item.get("exchange") or "").strip().lower()
                symbols = sorted(
                    str(value or "").strip().upper()
                    for value in (item.get("symbols") if isinstance(item.get("symbols"), list) else [])
                    if str(value or "").strip()
                )
                if exchange and symbols:
                    weight = _finite(item.get("weight"))
                    normalized_constituents.append(
                        {
                            "exchange": exchange,
                            "symbols": symbols,
                            "weight": round(weight, 10) if weight is not None else None,
                        }
                    )
        if normalized_constituents:
            canonical = json.dumps(
                sorted(normalized_constituents, key=lambda item: (item["exchange"], item["symbols"])),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            ttl = 300.0
    except Exception:
        fingerprint = None
    with _gate_index_lock:
        _gate_index_cache[normalized] = (now + ttl, fingerprint)
    return fingerprint


def _attach_gate_index_evidence(candidates: list[dict[str, Any]]) -> None:
    targets = {
        str(candidate.get("symbol") or "").strip().upper()
        for candidate in candidates
        if (_finite(candidate.get("openSpreadPct")) or float("-inf")) >= structure_tracking_min_pct()
        and "gate"
        in {
            str(candidate.get("buyExchange") or "").lower(),
            str(candidate.get("sellExchange") or "").lower(),
        }
    }
    fingerprints: dict[str, str | None] = {}
    if targets:
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
            fingerprints = dict(zip(sorted(targets), executor.map(_gate_index_fingerprint, sorted(targets))))
    for candidate in candidates:
        exchanges = {
            str(candidate.get("buyExchange") or "").lower(),
            str(candidate.get("sellExchange") or "").lower(),
        }
        if "gate" not in exchanges:
            continue
        symbol = str(candidate.get("symbol") or "").strip().upper()
        candidate["indexEvidenceRequired"] = True
        fingerprint = fingerprints.get(symbol)
        if fingerprint:
            candidate["indexFingerprint"] = fingerprint


def _annotate_ff_structure(candidates: list[dict[str, Any]], now_ms: int | None = None) -> list[dict[str, Any]]:
    if not structure_filter_enabled():
        return candidates
    tracked_ff = [
        candidate
        for candidate in candidates
        if str(candidate.get("type") or "").upper() == "FF" and astro_spread_card_routes(candidate)
    ]
    _attach_gate_index_evidence(tracked_ff)
    record_ff_structure_history(tracked_ff, now_ms=now_ms)
    actionable_ff = [
        candidate
        for candidate in tracked_ff
        if (_finite(candidate.get("openSpreadPct")) or float("-inf")) > spread_scan_ff_min_open_pct()
    ]
    actionable_keys = {str(candidate.get("key") or "") for candidate in actionable_ff}
    changed_scopes = _recent_index_change_scopes(actionable_ff)
    annotated: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("key") or "") not in actionable_keys:
            annotated.append(candidate)
            continue
        symbol = str(candidate.get("symbol") or "").strip().upper()
        exchanges = {
            str(candidate.get("buyExchange") or "").lower(),
            str(candidate.get("sellExchange") or "").lower(),
        }
        index_change_detected = any((symbol, exchange) in changed_scopes for exchange in exchanges)
        annotated.append(
            {
                **candidate,
                "structureAssessment": ff_structure_assessment(
                    candidate,
                    now_ms=now_ms,
                    index_change_detected=index_change_detected,
                ),
            }
        )
    return annotated


class PulseUnavailableError(RuntimeError):
    """All live Pulse transports failed for this scan round."""


def _fetch_pulse(url: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
    # Pulse responses are large; the macOS proxy is materially faster and more
    # reliable than the current no-proxy route. Executable books are still
    # independently revalidated through exchange APIs before any card is made.
    with httpx.Client(
        timeout=timeout_seconds,
        trust_env=True,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "stock-review-mac/astro-spread-scanner-local-proxy",
        },
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise RuntimeError(f"Pulse 行情返回异常：{url}")
    return payload


def _fetch_direct_pulse_payloads(*, on_partial=None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    active = [url for url in PULSE_URLS if astro_pulse_health.due(url)]
    for url in PULSE_URLS:
        if url not in active:
            failures.append({"url": url, "error": "source_backoff", "category": "backoff"})
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(PULSE_URLS)) as executor:
        futures = {
            executor.submit(_call_with_astro_api_priority, _fetch_pulse, url): url
            for url in active
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                payloads.append(future.result())
                astro_pulse_health.result(url, started)
            except Exception as exc:
                astro_pulse_health.result(url, started, exc)
                health = astro_pulse_health.snapshot()[url]
                failures.append({"url": url, "error": str(exc), "errorType": type(exc).__name__, "category": health["category"]})
            if on_partial is not None and payloads and len(payloads) + len(failures) < len(PULSE_URLS):
                # Register complete routes from the first source immediately.
                # A partial snapshot never authorizes a card or a cleanup.
                on_partial(list(payloads))
    summary = {
        "successCount": len(payloads),
        "failureCount": len(failures),
        "failures": sorted(failures, key=lambda item: item["url"]),
        "sourceCount": len(PULSE_URLS),
    }
    if not payloads:
        messages = "; ".join(item["error"] for item in failures[:3]) or "未返回行情"
        raise PulseUnavailableError(f"Astro Pulse 本机直连全部失败：{messages}")
    return payloads, summary


def _fetch_pulse_payloads(*, on_partial=None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    primary_error: Exception | None = None
    try:
        payloads, summary = _fetch_direct_pulse_payloads(on_partial=on_partial) if on_partial is not None else _fetch_direct_pulse_payloads()
        return payloads, {
            **summary, "transport": "local_proxy", "raceMode": "local_proxy_then_cloud",
            "fallbackUsed": False, "primaryFailure": None, "cloudRttMs": None,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }
    except PulseUnavailableError as error:
        primary_error = error

    if os.environ.get("ASTRO_PULSE_SSH_ENABLED", "0").lower() not in {"1", "true", "yes", "on"}:
        raise primary_error
    bridge_url = os.environ.get("ASTRO_PULSE_SSH_URL", "http://127.0.0.1:8766").rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=1.0), trust_env=False) as client:
            response = client.get(bridge_url + "/pulse", params={"timeoutSeconds": 5.0})
            response.raise_for_status()
            envelope = response.json()
        encoded = envelope.get("payloadsGzipBase64")
        if envelope.get("payloadEncoding") != "gzip+base64" or not isinstance(encoded, str):
            raise ValueError("腾讯云 Pulse SSH 返回格式异常")
        payloads = json.loads(gzip.decompress(base64.b64decode(encoded, validate=True)))
        if not isinstance(payloads, list) or not payloads:
            raise ValueError("腾讯云 Pulse SSH 未返回行情")
        if on_partial is not None:
            on_partial(list(payloads))
        failures = envelope.get("failures") if isinstance(envelope.get("failures"), list) else []
        return payloads, {
            "successCount": len(payloads), "failureCount": len(failures), "failures": failures,
            "sourceCount": int(envelope.get("sourceCount") or len(PULSE_URLS)),
            "transport": "tencent_cloud_ssh", "raceMode": "local_proxy_then_cloud",
            "fallbackUsed": True,
            "primaryFailure": {"transport": "local_proxy", "error": str(primary_error)},
            "cloudRttMs": _finite(envelope.get("sshRttMs")),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as cloud_error:
        raise PulseUnavailableError(
            f"Astro Pulse 本机代理与腾讯云SSH均失败：{primary_error}; {type(cloud_error).__name__}: {cloud_error}"
        ) from cloud_error


def _log_pulse_source_status(summary: dict[str, Any]) -> None:
    global _pulse_source_log_at, _pulse_source_degraded_streak
    global _pulse_source_degraded_started_at, _pulse_source_degraded_logged
    failures = summary.get("failures") if isinstance(summary.get("failures"), list) else []
    primary_failure = summary.get("primaryFailure") if isinstance(summary.get("primaryFailure"), dict) else None
    is_degraded = bool(failures or primary_failure)
    now = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    event_name: str | None = None
    event_level = "warning"
    message = ""
    consecutive = 0
    started_at: datetime | None = None
    with _pulse_source_log_lock:
        if is_degraded:
            _pulse_source_degraded_streak += 1
            consecutive = _pulse_source_degraded_streak
            if _pulse_source_degraded_started_at is None:
                _pulse_source_degraded_started_at = now_utc
            started_at = _pulse_source_degraded_started_at
            if consecutive >= 3 and not _pulse_source_degraded_logged:
                _pulse_source_degraded_logged = True
                _pulse_source_log_at = now
                event_name = "astro_pulse_source_degraded"
                message = "Astro Pulse 本机直连已连续3轮仅部分行情源可用，系统继续使用有效源扫描。"
            elif _pulse_source_degraded_logged and now - _pulse_source_log_at >= 300:
                _pulse_source_log_at = now
                event_name = "astro_pulse_source_degraded_summary"
                message = f"Astro Pulse 本机直连仍部分降级，已连续 {consecutive} 轮。"
        else:
            was_logged = _pulse_source_degraded_logged
            consecutive = _pulse_source_degraded_streak
            started_at = _pulse_source_degraded_started_at
            _pulse_source_degraded_streak = 0
            _pulse_source_degraded_started_at = None
            _pulse_source_degraded_logged = False
            if was_logged:
                event_name = "astro_pulse_sources_recovered"
                event_level = "info"
                message = "Astro Pulse 本机直连行情源已全部恢复。"
            _pulse_source_log_at = now
    if not event_name:
        return
    append_system_runtime_event(
        event_name,
        level=event_level,
        source="backend",
        module="astro_spread_scanner",
        message=message,
        details={
            **summary,
            "consecutiveDegradedRounds": consecutive,
            "degradedStartedAt": started_at.isoformat() if started_at else None,
            "logRule": "连续3轮记录；持续期间每5分钟汇总；恢复仅记录一次",
        },
    )


def _record_pulse_success(summary: dict[str, Any]) -> dict[str, Any]:
    global _pulse_consecutive_failures, _pulse_outage_started_at
    global _pulse_outage_last_summary_at, _pulse_unhealthy_logged
    recovered_details: dict[str, Any] | None = None
    now = datetime.now(timezone.utc)
    with _pulse_outage_lock:
        if _pulse_consecutive_failures:
            started_at = _pulse_outage_started_at
            recovered_details = {
                "startedAt": started_at.isoformat() if started_at else None,
                "recoveredAt": now.isoformat(),
                "durationSeconds": round((now - started_at).total_seconds(), 1) if started_at else None,
                "failedRounds": _pulse_consecutive_failures,
                "recoveredTransport": summary.get("transport"),
            }
        _pulse_consecutive_failures = 0
        _pulse_outage_started_at = None
        _pulse_outage_last_summary_at = 0.0
        _pulse_unhealthy_logged = False
    if recovered_details:
        append_system_runtime_event(
            "astro_pulse_outage_recovered",
            level="info",
            source="backend",
            module="astro_spread_scanner",
            message="Astro Pulse 实时行情已恢复，自动建卡与清卡流程恢复运行。",
            details=recovered_details,
        )
    return {
        "pulseTransport": summary.get("transport"),
        "pulseFallbackActive": bool(summary.get("fallbackUsed")),
        "pulseHealthy": True,
        "pulseConsecutiveFailureCount": 0,
        "pulseOutageStartedAt": None,
        "pulseLastRecoveredAt": now.isoformat() if recovered_details else _state.get("pulseLastRecoveredAt"),
        "cleanupPaused": False,
    }


def _record_pulse_failure(error: Exception) -> dict[str, Any]:
    global _pulse_consecutive_failures, _pulse_outage_started_at
    global _pulse_outage_last_summary_at, _pulse_unhealthy_logged
    now = datetime.now(timezone.utc)
    monotonic_now = time.monotonic()
    event_name: str | None = None
    event_level = "warning"
    event_message = ""
    with _pulse_outage_lock:
        _pulse_consecutive_failures += 1
        failure_count = _pulse_consecutive_failures
        if _pulse_outage_started_at is None:
            _pulse_outage_started_at = now
            _pulse_outage_last_summary_at = monotonic_now
            event_name = "astro_pulse_outage_started"
            event_message = "Astro Pulse 实时行情中断；本轮停止建卡与清卡，系统正在自动重连。"
        elif failure_count >= 3 and not _pulse_unhealthy_logged:
            _pulse_unhealthy_logged = True
            event_name = "astro_pulse_watchdog_unhealthy"
            event_level = "error"
            event_message = "Astro Pulse 已连续3轮不可用；健康检查进入异常状态并继续自动恢复。"
        elif monotonic_now - _pulse_outage_last_summary_at >= 300:
            _pulse_outage_last_summary_at = monotonic_now
            event_name = "astro_pulse_outage_summary"
            event_level = "error"
            event_message = f"Astro Pulse 仍不可用，已连续失败 {failure_count} 轮；建卡与清卡继续暂停。"
        started_at = _pulse_outage_started_at
    if event_name:
        append_system_runtime_event(
            event_name,
            level=event_level,
            source="backend",
            module="astro_spread_scanner",
            message=event_message,
            details={
                "error": str(error),
                "consecutiveFailures": failure_count,
                "failureThreshold": 3,
                "outageStartedAt": started_at.isoformat() if started_at else None,
                "cleanupPaused": True,
            },
        )
    return {
        "pulseTransport": None,
        "pulseFallbackActive": False,
        "pulseHealthy": failure_count < 3,
        "pulseConsecutiveFailureCount": failure_count,
        "pulseOutageStartedAt": started_at.isoformat() if started_at else None,
        "cleanupPaused": True,
    }


def _astro_route_identity(pair: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(pair.get("name") or "").strip().upper(),
        str(pair.get("type") or "").strip().upper(),
        str(pair.get("buyEx") or "").strip().lower(),
        str(pair.get("sellEx") or "").strip().lower(),
    )


def _dex_lifecycle_parts(value: Any) -> tuple[str, str]:
    config = value if isinstance(value, dict) else {}
    chain_index = str(config.get("chainIndex") or "").strip()
    return (
        chain_index,
        _normalized_contract_address(chain_index, config.get("contractAddress")),
    )


def _candidate_route_identity(candidate: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    chain_index, contract_address = _dex_lifecycle_parts(candidate.get("dexConfig"))
    return (
        str(candidate.get("symbol") or "").strip().upper(),
        str(candidate.get("type") or "").strip().upper(),
        str(candidate.get("buyExchange") or "").strip().lower(),
        str(candidate.get("sellExchange") or "").strip().lower(),
        chain_index,
        contract_address,
    )


def _pair_revalidation_identity(pair: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    chain_index, contract_address = _dex_lifecycle_parts(pair.get("_dexConfig"))
    return (*_astro_route_identity(pair), chain_index, contract_address)


def _pair_initial_spread_pct(pair: dict[str, Any]) -> float | None:
    pipeline = pair.get("_pipeline") if isinstance(pair.get("_pipeline"), dict) else {}
    value = _finite(pipeline.get("pulseOpenSpreadPct"))
    if value is not None:
        return value
    open_position = _finite(pair.get("openPosition"))
    return open_position * 100 if open_position is not None else None


def _revalidation_retry_allowed(candidate: dict[str, Any], *, now: float | None = None) -> bool:
    route = _candidate_route_identity(candidate)
    current_spread = _finite(candidate.get("openSpreadPct"))
    current_time = time.monotonic() if now is None else now
    with _revalidation_state_lock:
        failure = dict(_revalidation_failures.get(route) or {})
    if not failure:
        return True
    failed_at = _finite(failure.get("failedAtMonotonic")) or 0.0
    reason = str(failure.get("reason") or "")
    if current_time - failed_at >= spread_revalidation_retry_seconds(reason):
        return True
    failed_spread = _finite(failure.get("pulseOpenSpreadPct"))
    return (
        current_spread is not None
        and failed_spread is not None
        and current_spread - failed_spread
        >= spread_revalidation_retry_improvement_pct_points() - 1e-9
    )


def _record_revalidation_outcome(
    pair: dict[str, Any],
    *,
    passed: bool,
    report: dict[str, Any],
) -> None:
    route = _pair_revalidation_identity(pair)
    reason = "eligible" if passed else str(report.get("reason") or "unknown")
    duration_ms = _finite(report.get("durationMs")) or 0.0
    now = time.monotonic()
    summary: dict[str, Any] | None = None
    with _revalidation_state_lock:
        if passed:
            _revalidation_failures.pop(route, None)
        else:
            _revalidation_failures[route] = {
                "failedAtMonotonic": now,
                "failedAt": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "pulseOpenSpreadPct": _pair_initial_spread_pct(pair),
                "chainIndex": route[4],
                "contractAddress": route[5],
            }
        _revalidation_metrics["attempted"] += 1
        _revalidation_metrics["passed" if passed else "failed"] += 1
        _revalidation_metrics["durationMsTotal"] += duration_ms
        reasons = _revalidation_metrics.setdefault("reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1
        if now - float(_revalidation_metrics.get("lastSummaryAtMonotonic") or 0.0) >= 300:
            attempted = int(_revalidation_metrics.get("attempted") or 0)
            summary = {
                "windowStartedAt": _revalidation_metrics.get("startedAt"),
                "windowEndedAt": datetime.now(timezone.utc).isoformat(),
                "attempted": attempted,
                "passed": int(_revalidation_metrics.get("passed") or 0),
                "failed": int(_revalidation_metrics.get("failed") or 0),
                "passRatePct": round(int(_revalidation_metrics.get("passed") or 0) / attempted * 100, 2)
                if attempted
                else 0.0,
                "averageDurationMs": round(float(_revalidation_metrics.get("durationMsTotal") or 0.0) / attempted, 1)
                if attempted
                else 0.0,
                "reasons": dict(reasons),
            }
            _revalidation_metrics.update(
                {
                    "startedAt": datetime.now(timezone.utc).isoformat(),
                    "attempted": 0,
                    "passed": 0,
                    "failed": 0,
                    "durationMsTotal": 0.0,
                    "reasons": {},
                    "lastSummaryAtMonotonic": now,
                }
            )
    if summary is not None:
        append_system_runtime_event(
            "astro_revalidation_summary",
            level="info",
            source="backend",
            module="astro_spread_scanner",
            message=(
                f"Astro 最终复核 5 分钟汇总：通过 {summary['passed']} / {summary['attempted']}。"
            ),
            details=summary,
        )
    _append_decision_audit(
        route,
        stage="final_revalidation",
        decision="passed" if passed else reason,
        details={
            "pulseOpenSpreadPct": _pair_initial_spread_pct(pair),
            "durationMs": duration_ms,
            "report": report,
        },
    )


def _revalidation_runtime_status() -> dict[str, Any]:
    now = time.monotonic()
    with _revalidation_state_lock:
        active = [
            item
            for item in _revalidation_failures.values()
            if now - float(item.get("failedAtMonotonic") or 0.0)
            < spread_revalidation_retry_seconds(str(item.get("reason") or ""))
        ]
        attempted = int(_revalidation_metrics.get("attempted") or 0)
        passed = int(_revalidation_metrics.get("passed") or 0)
        return {
            "workers": spread_final_revalidation_workers(),
            "defaultRetrySeconds": spread_revalidation_retry_seconds(),
            "quoteRetrySeconds": spread_revalidation_retry_seconds("stale_direct_quote"),
            "unavailableRetrySeconds": spread_revalidation_retry_seconds("direct_quote_unavailable"),
            "earlyRetryImprovementPctPoints": spread_revalidation_retry_improvement_pct_points(),
            "activeCooldownCount": len(active),
            "metricsWindowStartedAt": _revalidation_metrics.get("startedAt"),
            "windowAttempted": attempted,
            "windowPassed": passed,
            "windowFailed": int(_revalidation_metrics.get("failed") or 0),
            "windowPassRatePct": round(passed / attempted * 100, 2) if attempted else 0.0,
            "windowReasons": dict(_revalidation_metrics.get("reasons") or {}),
        }


def _timestamp_ms(value: Any, fallback_ms: int) -> int:
    parsed = _finite(value)
    if parsed is None or parsed <= 0:
        return fallback_ms
    if parsed < 1_000_000_000_000:
        parsed *= 1000
    return int(parsed)


def _gate_depth_timestamps(data: dict[str, Any]) -> tuple[int, int | None, str]:
    """Gate current is snapshot generation; update is last book mutation.

    Spot uses milliseconds and futures uses fractional seconds. Do not use
    receipt time to freshen missing/invalid native timestamps. If an older
    response omits current, the conservative update timestamp remains usable.
    """
    def parse(value):
        parsed = _finite(value)
        if isinstance(value, bool) or parsed is None or parsed <= 0:
            raise ValueError("Gate 盘口时间无效或缺失")
        return _timestamp_ms(parsed, 0)

    changed = parse(data["update"]) if data.get("update") is not None else None
    if data.get("current") is not None:
        generated = parse(data["current"])
        if changed is not None and changed > generated + 1000:
            raise ValueError("Gate 盘口生成时间与变动时间存在时钟偏差")
        return generated, changed, "exchange_snapshot_generated"
    if changed is None:
        raise ValueError("Gate 盘口时间无效或缺失")
    return changed, changed, "exchange_depth_update"


def _direct_market_symbol(
    exchange: str,
    market_type: str,
    canonical_symbol: str,
    aliases: dict[tuple[str, str, str], tuple[str, float]],
) -> tuple[str, float]:
    canonical = canonical_symbol.strip().upper().removesuffix("USDT")
    for (alias_exchange, alias_market, raw_symbol), (mapped_symbol, divisor) in aliases.items():
        if alias_exchange == exchange and alias_market == market_type and mapped_symbol == canonical:
            return raw_symbol, divisor
    return canonical, 1.0


def _fetch_direct_quote(
    client: httpx.Client,
    exchange: str,
    market_type: str,
    canonical_symbol: str,
    aliases: dict[tuple[str, str, str], tuple[str, float]],
    expected_dex_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one exchange's executable top of book from its public API."""

    raw_base, divisor = _direct_market_symbol(exchange, market_type, canonical_symbol, aliases)
    compact = f"{raw_base}USDT"
    response_started_ms = int(time.time() * 1000)
    payload: Any
    row: dict[str, Any]
    timestamp: Any = None
    timestamp_source = "exchange"

    if exchange in {"binance", "aster"}:
        if market_type == "spot" and exchange != "binance":
            raise RuntimeError(f"{exchange} 现货直连复核未接入")
        base_url = "https://api.binance.com" if market_type == "spot" else (
            "https://fapi.binance.com" if exchange == "binance" else "https://fapi.asterdex.com"
        )
        # Aster's bookTicker ``time`` is the last book-change time.  A quiet
        # market can therefore look stale even though this request just read
        # the exchange's current executable book.  Use the official depth
        # snapshot instead: ``E`` is the message output time and its first
        # bid/ask are the current top of book.
        use_aster_depth = exchange == "aster" and market_type == "future"
        path = (
            "/fapi/v1/depth"
            if use_aster_depth
            else ("/api/v3/ticker/bookTicker" if market_type == "spot" else "/fapi/v1/ticker/bookTicker")
        )
        params = {"symbol": compact, "limit": 5} if use_aster_depth else {"symbol": compact}
        response = client.get(f"{base_url}{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        row = payload if isinstance(payload, dict) else {}
        if use_aster_depth:
            bids = row.get("bids") if isinstance(row.get("bids"), list) else []
            asks = row.get("asks") if isinstance(row.get("asks"), list) else []
            bid = _finite(bids[0][0]) if bids and isinstance(bids[0], list) and bids[0] else None
            ask = _finite(asks[0][0]) if asks and isinstance(asks[0], list) and asks[0] else None
            # Aster may keep E/T at the last order-book mutation even though
            # this REST call returned a fresh current snapshot.  Freshness and
            # cross-leg skew therefore use the time this snapshot was received;
            # the exchange's original E/T remain attached below for audit.
            timestamp = None
            timestamp_source = "response_received_snapshot"
        else:
            bid = _finite(row.get("bidPrice"))
            ask = _finite(row.get("askPrice"))
            timestamp = row.get("time")
    elif exchange == "bybit":
        response = client.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot" if market_type == "spot" else "linear", "symbol": compact},
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("result", {}).get("list") if isinstance(payload, dict) else []
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        bid = _finite(row.get("bid1Price"))
        ask = _finite(row.get("ask1Price"))
        timestamp = payload.get("time") if isinstance(payload, dict) else None
    elif exchange == "bitget":
        if market_type == "spot":
            path = "/api/v2/spot/market/tickers"
            params = {"symbol": compact}
        else:
            path = "/api/v2/mix/market/ticker"
            params = {"symbol": compact, "productType": "USDT-FUTURES"}
        response = client.get(f"https://api.bitget.com{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else []
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else (
            rows if isinstance(rows, dict) else {}
        )
        bid = _finite(row.get("bidPr"))
        ask = _finite(row.get("askPr"))
        timestamp = row.get("ts") or (payload.get("requestTime") if isinstance(payload, dict) else None)
    elif exchange == "okx":
        instrument = f"{raw_base}-USDT" + ("" if market_type == "spot" else "-SWAP")
        response = client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": instrument})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else []
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        bid = _finite(row.get("bidPx"))
        ask = _finite(row.get("askPx"))
        timestamp = row.get("ts")
    elif exchange in SF_DEX_EXCHANGES:
        if market_type != "spot":
            raise RuntimeError("okxdex 仅支持现货复核")
        response = client.get(PULSE_URLS[1])
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        market = data.get(PULSE_MARKET_KEY_BY_ROUTE[(exchange, "spot")]) if isinstance(data, dict) else None
        rows = market.get("list") if isinstance(market, dict) else None
        matching_rows = [
            item
            for item in rows or []
            if isinstance(item, dict) and str(item.get("name") or "").strip().upper() == compact
        ]
        if expected_dex_config:
            expected_chain = str(expected_dex_config.get("chainIndex") or "").strip()
            expected_address = _normalized_contract_address(
                expected_chain,
                expected_dex_config.get("contractAddress"),
            )
            matching_rows = [
                item
                for item in matching_rows
                if str(item.get("chainIndex") or "").strip() == expected_chain
                and _normalized_contract_address(
                    item.get("chainIndex"),
                    item.get("ca") or item.get("contractAddress"),
                ) == expected_address
            ]
        elif len(matching_rows) > 1:
            raise RuntimeError(f"okxdex {raw_base} 存在多个链或合约地址，拒绝按币名选择")
        if len(matching_rows) != 1:
            raise RuntimeError(f"okxdex {raw_base} 指定链与合约的盘口不存在")
        row = matching_rows[0]
        bid = _finite(row.get("b"))
        ask = _finite(row.get("a"))
        timestamp = market.get("ts") if isinstance(market, dict) else None
    elif exchange == "gate":
        if market_type == "spot":
            path = "/api/v4/spot/tickers"
            params = {"currency_pair": f"{raw_base}_USDT"}
        else:
            path = "/api/v4/futures/usdt/tickers"
            params = {"contract": f"{raw_base}_USDT"}
        response = client.get(f"https://api.gateio.ws{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        row = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else {}
        bid = _finite(row.get("highest_bid"))
        ask = _finite(row.get("lowest_ask"))
        timestamp = row.get("time_ms") or row.get("time")
    else:
        raise RuntimeError(f"{exchange} 直连复核未接入")

    received_ms = int(time.time() * 1000)
    exchange_timestamp_ms = _timestamp_ms(timestamp, received_ms)
    # A direct REST response is a newly requested current snapshot.  Exchange
    # timestamp fields are not comparable across venues: some are server output
    # times, while others are merely the last top-of-book mutation.  Use the
    # snapshot reception time for age/skew gates and retain the exchange time
    # separately for diagnostics.  OKXDEX remains different because it is a
    # Pulse aggregate snapshot whose source timestamp must advance.
    if exchange in SF_DEX_EXCHANGES:
        quote_timestamp_ms = exchange_timestamp_ms
    else:
        quote_timestamp_ms = received_ms
        timestamp_source = "response_received_snapshot"
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError(f"{exchange} {raw_base} {market_type} 盘口为空或倒挂")
    result = {
        "exchange": exchange,
        "market": market_type,
        "rawSymbol": raw_base,
        "bid": bid / divisor,
        "ask": ask / divisor,
        "timestamp": quote_timestamp_ms,
        "timestampSource": timestamp_source,
        "exchangeTimestamp": exchange_timestamp_ms,
        "requestDurationMs": round(max(0, received_ms - response_started_ms), 1),
    }
    if exchange == "aster" and market_type == "future":
        result["quoteEndpoint"] = "depth"
        result["exchangeEventTimestamp"] = _timestamp_ms(row.get("E"), quote_timestamp_ms)
        result["exchangeTransactionTimestamp"] = _timestamp_ms(row.get("T"), quote_timestamp_ms)
    if exchange in SF_DEX_EXCHANGES:
        chain_index = str(row.get("chainIndex") or "").strip()
        contract_address = str(row.get("ca") or row.get("contractAddress") or "").strip()
        if not chain_index or not contract_address:
            raise RuntimeError(f"okxdex {raw_base} 缺少链或合约地址")
        result["dexConfig"] = {
            "name": canonical_symbol.strip().upper().removesuffix("USDT"),
            "exchange": exchange,
            "chainIndex": chain_index,
            "contractAddress": contract_address,
            "quote": "USDT",
            "slippage": f"{spread_okxdex_slippage_pct():g}",
        }
    return result


def _depth_level_values(row: Any) -> tuple[float | None, float | None]:
    if isinstance(row, (list, tuple)) and len(row) >= 2:
        return _finite(row[0]), _finite(row[1])
    if isinstance(row, dict):
        return (
            _finite(row.get("price") or row.get("p") or row.get("px")),
            _finite(row.get("size") or row.get("s") or row.get("qty") or row.get("q") or row.get("sz")),
        )
    return None, None


_contract_quantity_multiplier_lock = threading.Lock()
_contract_quantity_multiplier_cache: dict[tuple[str, str], float] = {}


def _futures_contract_quantity_multiplier(
    client: httpx.Client,
    exchange: str,
    raw_base: str,
) -> float:
    """Return base-asset units represented by one order-book size unit."""

    if exchange not in {"gate", "okx"}:
        return 1.0
    with _contract_quantity_multiplier_lock:
        cached = _contract_quantity_multiplier_cache.get((exchange, raw_base))
    if cached is not None:
        return cached
    transport = "tencent_cloud" if bool(getattr(client, "cloud_depth", False)) else "local_proxy"
    return _auxiliary_reads.read(
        ("contract_multiplier", transport, exchange, raw_base),
        lambda: _load_futures_contract_quantity_multiplier(client, exchange, raw_base),
        timeout=spread_final_revalidation_timeout_seconds(),
    )


def _load_futures_contract_quantity_multiplier(
    client: httpx.Client,
    exchange: str,
    raw_base: str,
) -> float:
    # Double-check after becoming the owner; another completed route may have
    # populated the existing static-metadata cache in the meantime.
    if exchange not in {"gate", "okx"}:
        return 1.0
    key = (exchange, raw_base)
    with _contract_quantity_multiplier_lock:
        cached = _contract_quantity_multiplier_cache.get(key)
    if cached is not None:
        return cached
    if exchange == "gate":
        response = _scanner_public_get(
            client,
            f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{raw_base}_USDT"
        )
        response.raise_for_status()
        payload = response.json()
        multiplier = _finite(payload.get("quanto_multiplier")) if isinstance(payload, dict) else None
    else:
        response = _scanner_public_get(
            client,
            "https://www.okx.com/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": f"{raw_base}-USDT-SWAP"},
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        multiplier = _finite(row.get("ctVal"))
        if str(row.get("ctType") or "").lower() not in {"", "linear"}:
            raise RuntimeError(f"okx {raw_base} 非线性合约暂不支持深度数量换算")
    if multiplier is None or multiplier <= 0:
        raise RuntimeError(f"{exchange} {raw_base} 合约乘数不可用")
    with _contract_quantity_multiplier_lock:
        _contract_quantity_multiplier_cache[key] = multiplier
    return multiplier


def _fetch_direct_depth_book(
    client: httpx.Client,
    exchange: str,
    market_type: str,
    canonical_symbol: str,
    aliases: dict[tuple[str, str, str], tuple[str, float]],
) -> dict[str, Any]:
    """Fetch up to 20 public-book levels and normalize base quantities.

    The executable check prices the configured plan and exact same-quantity
    sell. If those 20 levels cannot fill both legs, the route fails closed;
    this path deliberately does not widen to a deeper snapshot.
    """

    raw_base, divisor = _direct_market_symbol(exchange, market_type, canonical_symbol, aliases)
    compact = f"{raw_base}USDT"
    started_ms = int(time.time() * 1000)
    payload: Any
    data: dict[str, Any]
    endpoint: str
    native_timestamp: Any = None
    book_changed_ms: int | None = None
    timestamp_source: str | None = None
    request_meta: dict[str, Any]
    if exchange in {"binance", "aster"}:
        if exchange == "aster" and market_type != "future":
            raise RuntimeError("aster 现货深度复核未接入")
        base_url = "https://api.binance.com" if market_type == "spot" else (
            "https://fapi.binance.com" if exchange == "binance" else "https://fapi.asterdex.com"
        )
        path = "/api/v3/depth" if market_type == "spot" else "/fapi/v1/depth"
        endpoint = f"{base_url}{path}"
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"symbol": compact, "limit": 20},
        )
        data = payload if isinstance(payload, dict) else {}
        native_timestamp = data.get("E") or data.get("T")
    elif exchange == "bybit":
        endpoint = "https://api.bybit.com/v5/market/orderbook"
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"category": "spot" if market_type == "spot" else "linear", "symbol": compact, "limit": 20},
        )
        data = payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else {}
        data = {**data, "bids": data.get("b"), "asks": data.get("a")}
        native_timestamp = data.get("ts") or (payload.get("time") if isinstance(payload, dict) else None)
    elif exchange == "bitget":
        if market_type == "spot":
            endpoint = "https://api.bitget.com/api/v2/spot/market/orderbook"
            params = {"symbol": compact, "type": "step0", "limit": 20}
        else:
            endpoint = "https://api.bitget.com/api/v2/mix/market/orderbook"
            params = {"symbol": compact, "productType": "USDT-FUTURES", "limit": 20}
        payload, request_meta = _shared_depth_json_get(client, endpoint, params=params)
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        native_timestamp = data.get("ts")
    elif exchange == "okx":
        endpoint = "https://www.okx.com/api/v5/market/books"
        inst_id = f"{raw_base}-USDT" + ("" if market_type == "spot" else "-SWAP")
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"instId": inst_id, "sz": 20},
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        data = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        native_timestamp = data.get("ts")
    elif exchange == "gate":
        if market_type == "spot":
            endpoint = "https://api.gateio.ws/api/v4/spot/order_book"
            params = {"currency_pair": f"{raw_base}_USDT", "limit": 20}
        else:
            endpoint = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
            params = {"contract": f"{raw_base}_USDT", "limit": 20, "with_id": "true"}
        payload, request_meta = _shared_depth_json_get(client, endpoint, params=params)
        data = payload if isinstance(payload, dict) else {}
        native_timestamp, book_changed_ms, timestamp_source = _gate_depth_timestamps(data)
    else:
        raise RuntimeError(f"{exchange} 深度复核未接入")

    quantity_multiplier = (
        _futures_contract_quantity_multiplier(client, exchange, raw_base)
        if market_type == "future"
        else 1.0
    )

    def normalized_levels(value: Any) -> list[list[float]]:
        result: list[list[float]] = []
        for raw_level in (value[:20] if isinstance(value, list) else []):
            raw_price, raw_quantity = _depth_level_values(raw_level)
            if raw_price is None or raw_quantity is None or raw_price <= 0 or raw_quantity <= 0:
                continue
            result.append([
                raw_price / divisor,
                raw_quantity * quantity_multiplier * divisor,
            ])
        return result

    bids = normalized_levels(data.get("bids"))
    asks = normalized_levels(data.get("asks"))
    if not bids or not asks:
        raise RuntimeError(f"{exchange} {raw_base} {market_type} 深度为空")
    received_ms = int(request_meta.get("localReceivedAt") or request_meta.get("receivedAt") or int(time.time() * 1000))
    native_timestamp_ms = _timestamp_ms(native_timestamp, received_ms)
    if native_timestamp_ms > received_ms + 1000:
        raise ValueError(f"{exchange} 盘口时钟偏差超过1秒")
    return {
        "source": "exchange_public_depth",
        "exchange": exchange,
        "market": market_type,
        "rawSymbol": raw_base,
        "bids": bids,
        "asks": asks,
        "bestBid": bids[0][0],
        "bestAsk": asks[0][0],
        "bestBidQuantity": bids[0][1],
        "bestAskQuantity": asks[0][1],
        "timestamp": native_timestamp_ms,
        "exchangeTimestamp": native_timestamp_ms,
        "timestampSource": timestamp_source or ("exchange_depth_update" if native_timestamp is not None else "response_received_depth"),
        "bookChangedAt": book_changed_ms,
        "receivedAt": received_ms,
        "localReceivedAt": received_ms,
        "originReceivedAt": request_meta.get("originReceivedAt", received_ms),
        "requestDurationMs": _finite(request_meta.get("requestDurationMs"))
        or round(max(0, received_ms - started_ms), 1),
        "cacheHit": bool(request_meta.get("cacheHit")),
        "joinedInflight": bool(request_meta.get("joinedInflight")),
        "quantityMultiplier": quantity_multiplier,
        "endpoint": endpoint,
        "transport": request_meta.get("transport", "local_proxy"),
    }


def _consume_asks_by_notional(asks: list[list[float]], quote_notional: float) -> dict[str, Any]:
    remaining = quote_notional
    quantity = 0.0
    used_levels = 0
    worst_price: float | None = None
    for price, available_quantity in asks:
        available_cost = price * available_quantity
        spend = min(remaining, available_cost)
        quantity += spend / price
        remaining -= spend
        used_levels += 1
        worst_price = price
        if remaining <= max(1e-12, quote_notional * 1e-12):
            remaining = 0.0
            break
    if remaining > 0 or quantity <= 0:
        raise RuntimeError(f"买入腿卖盘深度不足：缺少 {remaining:.8g} USDT")
    return {
        "spentUsdt": quote_notional,
        "quantity": quantity,
        "averagePrice": quote_notional / quantity,
        "worstPrice": worst_price,
        "usedLevels": used_levels,
    }


def _consume_bids_by_quantity(bids: list[list[float]], target_quantity: float) -> dict[str, Any]:
    remaining = target_quantity
    proceeds = 0.0
    used_levels = 0
    worst_price: float | None = None
    for price, available_quantity in bids:
        fill = min(remaining, available_quantity)
        proceeds += fill * price
        remaining -= fill
        used_levels += 1
        worst_price = price
        if remaining <= max(1e-12, target_quantity * 1e-12):
            remaining = 0.0
            break
    if remaining > 0 or proceeds <= 0:
        raise RuntimeError(f"卖出腿买盘深度不足：缺少 {remaining:.8g} 币")
    return {
        "targetQuantity": target_quantity,
        "filledQuantity": target_quantity,
        "proceedsUsdt": proceeds,
        "averagePrice": proceeds / target_quantity,
        "worstPrice": worst_price,
        "usedLevels": used_levels,
    }


_timestamp_repair_lock = threading.Lock()
_timestamp_repair_last: dict[tuple[str, ...], float] = {}
_timestamp_repair_counts: dict[str, int] = {}


def _timestamp_repair_status() -> dict[str, Any]:
    with _timestamp_repair_lock:
        return {"enabled": True, "maxExtraRequests": 1, "budgetSeconds": 1.5,
                "cooldownSeconds": 5, **_timestamp_repair_counts}


def _repair_depth_timing(client, buy_book, sell_book, *, symbol, pair_type,
                         buy_exchange, sell_exchange, aliases, deadline):
    """Refresh one older book; never alter native timestamps or relax gates."""
    report = {"attempted": False}
    def record(outcome):
        report["outcome"] = outcome
        with _timestamp_repair_lock:
            _timestamp_repair_counts[outcome] = _timestamp_repair_counts.get(outcome, 0) + 1
        return buy_book, sell_book, report

    now_ms = int(time.time() * 1000)
    skew = abs(buy_book["timestamp"] - sell_book["timestamp"]) / 1000
    report["beforeSkewSeconds"] = round(skew, 3)
    if skew <= spread_final_revalidation_max_skew_seconds():
        return buy_book, sell_book, report
    older_buy = buy_book["timestamp"] < sell_book["timestamp"]
    peer = sell_book if older_buy else buy_book
    peer_age = max(0, now_ms - peer["timestamp"]) / 1000
    age_limit = spread_final_revalidation_max_quote_age_seconds()
    if peer_age >= age_limit:
        return record("peer_expired")
    budget = min(1.5, deadline - time.monotonic(), age_limit - peer_age - 0.1)
    if budget < 0.2:
        return record("budget_exhausted")
    identity = (symbol, pair_type, buy_exchange, sell_exchange)
    now = time.monotonic()
    with _timestamp_repair_lock:
        last = _timestamp_repair_last.get(identity)
        if last is not None and now - last < 5:
            _timestamp_repair_counts["cooldown"] = _timestamp_repair_counts.get("cooldown", 0) + 1
            report["outcome"] = "cooldown"
            return buy_book, sell_book, report
        # Bounded memory even in a changing market; do not evict active cooldowns.
        if len(_timestamp_repair_last) >= 2000:
            for key, at in list(_timestamp_repair_last.items()):
                if now - at >= 5:
                    _timestamp_repair_last.pop(key, None)
            if len(_timestamp_repair_last) >= 2000:
                report["outcome"] = "capacity"
                return buy_book, sell_book, report
        _timestamp_repair_last[identity] = now
        _timestamp_repair_counts["attempted"] = _timestamp_repair_counts.get("attempted", 0) + 1
    report.update(attempted=True, refreshedLeg="buy" if older_buy else "sell",
                  budgetMs=round(budget * 1000))
    previous_boundary = getattr(client, "depth_refresh_started_ms", 0)
    client.depth_refresh_started_ms = int(time.time() * 1000) + 1
    from app.crypto import api_request_deadline, api_request_remaining_seconds
    try:
        with api_request_deadline(timeout_seconds=budget):
            fresh = _fetch_direct_depth_book(
                client, buy_exchange if older_buy else sell_exchange,
                ("spot" if pair_type == "SF" else "future") if older_buy else "future",
                symbol, aliases,
            )
            api_request_remaining_seconds()
        if time.monotonic() > deadline:
            return record("deadline_exceeded")
        original = buy_book if older_buy else sell_book
        if fresh["timestamp"] < original["timestamp"]:
            return record("snapshot_regressed")
        if older_buy:
            buy_book = fresh
        else:
            sell_book = fresh
        checked_ms = int(time.time() * 1000)
        new_skew = abs(buy_book["timestamp"] - sell_book["timestamp"]) / 1000
        ages = [max(0, checked_ms - book["timestamp"]) / 1000 for book in (buy_book, sell_book)]
        report.update(afterSkewSeconds=round(new_skew, 3), durationMs=round((time.monotonic()-now)*1000, 1))
        return record("synchronized" if max(ages) <= age_limit and new_skew <= spread_final_revalidation_max_skew_seconds() else "still_unsynchronized")
    except Exception as exc:
        # Retain the original failure evidence; never use a late or partial book.
        report["errorType"] = type(exc).__name__
        report["durationMs"] = round((time.monotonic()-now)*1000, 1)
        return record("refresh_failed")
    finally:
        client.depth_refresh_started_ms = previous_boundary


def _fetch_cex_executable_preflight(
    client: httpx.Client,
    symbol: str,
    pair_type: str,
    buy_exchange: str,
    sell_exchange: str,
    aliases: dict[tuple[str, str, str], tuple[str, float]],
    config: Any,
) -> dict[str, Any]:
    """Price the configured plan size and the exact same-quantity futures sell."""

    buy_market = "spot" if pair_type == "SF" else "future"
    quote_notional = spread_cex_quote_notional_usdt(config)
    repair_deadline = min(time.monotonic() + _local_depth_deadline_seconds() - 0.1,
                          getattr(client, "depth_observation_deadline", float("inf")))
    with ThreadPoolExecutor(max_workers=2) as executor:
        buy_future = executor.submit(
            _call_with_astro_api_priority,
            _fetch_direct_depth_book,
            client,
            buy_exchange,
            buy_market,
            symbol,
            aliases,
        )
        sell_future = executor.submit(
            _call_with_astro_api_priority,
            _fetch_direct_depth_book,
            client,
            sell_exchange,
            "future",
            symbol,
            aliases,
        )
        buy_book = buy_future.result()
        sell_book = sell_future.result()
    buy_execution = _consume_asks_by_notional(buy_book["asks"], quote_notional)
    sell_execution = _consume_bids_by_quantity(sell_book["bids"], buy_execution["quantity"])
    executable_spread = _spread_pct(buy_execution["averagePrice"], sell_execution["averagePrice"])
    timing_repair = {"attempted": False, "outcome": "price_not_eligible"}
    minimum = spread_scan_sf_route_min_open_pct(buy_exchange) if pair_type == "SF" else spread_scan_ff_min_open_pct()
    if executable_spread is not None and minimum < executable_spread <= _route_max_open_pct({"type": pair_type, "buyEx": buy_exchange, "sellEx": sell_exchange}):
        buy_book, sell_book, timing_repair = _repair_depth_timing(
            client, buy_book, sell_book, symbol=symbol, pair_type=pair_type,
            buy_exchange=buy_exchange, sell_exchange=sell_exchange, aliases=aliases,
            deadline=repair_deadline,
        )
        # A changed buy price changes quantity: reprice BOTH legs from the books.
        buy_execution = _consume_asks_by_notional(buy_book["asks"], quote_notional)
        sell_execution = _consume_bids_by_quantity(sell_book["bids"], buy_execution["quantity"])
        executable_spread = _spread_pct(buy_execution["averagePrice"], sell_execution["averagePrice"])
    if executable_spread is None:
        raise RuntimeError("CEX 同数量深度价差无法计算")
    return {
        "source": "exchange_public_depth_same_quantity",
        "timestampRepair": timing_repair,
        "quoteNotionalUsdt": quote_notional,
        "tokenQuantity": buy_execution["quantity"],
        "buyAveragePrice": buy_execution["averagePrice"],
        "sellAveragePrice": sell_execution["averagePrice"],
        "executableSpreadPct": executable_spread,
        "buyExecution": buy_execution,
        "sellExecution": sell_execution,
        "buyDepth": {key: value for key, value in buy_book.items() if key not in {"bids", "asks"}},
        "sellDepth": {key: value for key, value in sell_book.items() if key not in {"bids", "asks"}},
    }


def _route_contract_restriction(route):
    from app.astro_news_policy import route_check as news_route_check
    news_block = news_route_check(route["symbol"], route["type"], route["buyExchange"], route["sellExchange"])
    if news_block:
        return news_block
    if not spread_scan_exclude_delisted_exchange_cards():
        return None
    symbol, buy, sell = route["symbol"], route["buyExchange"], route["sellExchange"]
    try:
        blocks = _active_delisting_exchange_blocks()
    except Exception:
        return {"decision": "observe", "reason": "delisting_index_unavailable"}
    matched = _indexed_delisting_match(blocks, symbol, route["type"], buy, sell)
    if matched:
        return {"decision": "reject", "reason": "contract_delisting_announced", "exchange": matched,
                "source": "local_announcement_index"}
    from app.astro_contract_safety import route_check
    return route_check(symbol, route["type"], buy, sell)


def _record_contract_restriction(route, restriction, *, evaluation_source="card_preflight"):
    """Keep lifecycle rejection reasons in the existing bounded scanner audit."""
    _append_decision_audit(
        _candidate_route_identity(route), stage=evaluation_source,
        decision=restriction["reason"], details={"contractLifecycle": restriction},
    )


def _direct_quotes_from_cex_preflight(
    preflight: dict[str, Any],
    *,
    symbol: str,
    pair_type: str,
    buy_exchange: str,
    sell_exchange: str,
    buy_quote: dict[str, Any] | None = None,
    sell_quote: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build synchronized executable quotes directly from both depth books."""

    buy_depth = preflight["buyDepth"]
    sell_depth = preflight["sellDepth"]
    buy = {
        **dict(buy_quote or {}),
        "exchange": buy_exchange,
        "market": "spot" if pair_type == "SF" else "future",
        "rawSymbol": buy_depth.get("rawSymbol") or symbol,
        "bid": buy_depth["bestBid"],
        "ask": preflight["buyAveragePrice"],
        "timestamp": buy_depth["timestamp"],
        "timestampSource": buy_depth["timestampSource"],
        "bookChangedAt": buy_depth.get("bookChangedAt"),
        "receivedAt": buy_depth["receivedAt"],
        "requestDurationMs": buy_depth["requestDurationMs"],
        "quoteEndpoint": buy_depth["endpoint"],
        "topBookBid": buy_depth["bestBid"],
        "topBookAsk": buy_depth["bestAsk"],
        "topBookAskQuantity": buy_depth["bestAskQuantity"],
    }
    sell = {
        **dict(sell_quote or {}),
        "exchange": sell_exchange,
        "market": "future",
        "rawSymbol": sell_depth.get("rawSymbol") or symbol,
        "bid": preflight["sellAveragePrice"],
        "ask": sell_depth["bestAsk"],
        "timestamp": sell_depth["timestamp"],
        "timestampSource": sell_depth["timestampSource"],
        "bookChangedAt": sell_depth.get("bookChangedAt"),
        "receivedAt": sell_depth["receivedAt"],
        "requestDurationMs": sell_depth["requestDurationMs"],
        "quoteEndpoint": sell_depth["endpoint"],
        "topBookBid": sell_depth["bestBid"],
        "topBookAsk": sell_depth["bestAsk"],
        "topBookBidQuantity": sell_depth["bestBidQuantity"],
    }
    return buy, sell


def _fetch_futures_bid_depth_execution(
    client: httpx.Client,
    exchange: str,
    canonical_symbol: str,
    aliases: dict[tuple[str, str, str], tuple[str, float]],
    target_quantity: float,
) -> dict[str, Any]:
    """Return the weighted futures sell price for the exact DEX output size."""

    if target_quantity <= 0:
        raise RuntimeError("OKXDEX 实际买入数量无效")
    raw_base, divisor = _direct_market_symbol(exchange, "future", canonical_symbol, aliases)
    compact = f"{raw_base}USDT"
    started_ms = int(time.time() * 1000)
    quantity_multiplier = _futures_contract_quantity_multiplier(client, exchange, raw_base)
    payload: Any
    rows: Any
    endpoint: str
    request_meta: dict[str, Any]
    depth_limit: int

    def depth_covers_target(candidate_rows: Any) -> bool:
        available = 0.0
        for candidate_row in candidate_rows if isinstance(candidate_rows, list) else []:
            _price, raw_size = _depth_level_values(candidate_row)
            if raw_size is None or raw_size <= 0:
                continue
            available += raw_size * quantity_multiplier * divisor
            if available >= target_quantity:
                return True
        return False

    if exchange in {"binance", "aster"}:
        base_url = "https://fapi.binance.com" if exchange == "binance" else "https://fapi.asterdex.com"
        endpoint = f"{base_url}/fapi/v1/depth"
        rows = []
        request_meta = {}
        depth_limit = 100
        for index, candidate_limit in enumerate((100, 500, 1000)):
            payload, request_meta = _shared_depth_json_get(
                client,
                endpoint,
                params={"symbol": compact, "limit": candidate_limit},
            )
            rows = payload.get("bids") if isinstance(payload, dict) else None
            depth_limit = candidate_limit
            if depth_covers_target(rows):
                break
            if index < 2:
                with _depth_request_condition:
                    _depth_request_metrics["expandedDepthRequests"] = int(
                        _depth_request_metrics.get("expandedDepthRequests") or 0
                    ) + 1
    elif exchange == "bybit":
        endpoint = "https://api.bybit.com/v5/market/orderbook"
        depth_limit = 500
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"category": "linear", "symbol": compact, "limit": depth_limit},
        )
        rows = payload.get("result", {}).get("b") if isinstance(payload, dict) else None
    elif exchange == "bitget":
        endpoint = "https://api.bitget.com/api/v2/mix/market/orderbook"
        depth_limit = 150
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"symbol": compact, "productType": "USDT-FUTURES", "limit": depth_limit},
        )
        rows = payload.get("data", {}).get("bids") if isinstance(payload, dict) else None
    elif exchange == "okx":
        endpoint = "https://www.okx.com/api/v5/market/books"
        depth_limit = 400
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"instId": f"{raw_base}-USDT-SWAP", "sz": depth_limit},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data[0].get("bids") if isinstance(data, list) and data and isinstance(data[0], dict) else None
    elif exchange == "gate":
        endpoint = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
        depth_limit = 100
        payload, request_meta = _shared_depth_json_get(
            client,
            endpoint,
            params={"contract": f"{raw_base}_USDT", "limit": depth_limit},
        )
        rows = payload.get("bids") if isinstance(payload, dict) else None
    else:
        raise RuntimeError(f"{exchange} 合约深度复核未接入")

    remaining = target_quantity
    proceeds = 0.0
    used_levels = 0
    best_bid: float | None = None
    worst_bid: float | None = None
    for row in rows if isinstance(rows, list) else []:
        raw_price, raw_size = _depth_level_values(row)
        if raw_price is None or raw_size is None or raw_price <= 0 or raw_size <= 0:
            continue
        price = raw_price / divisor
        quantity = raw_size * quantity_multiplier * divisor
        if best_bid is None:
            best_bid = price
        fill = min(remaining, quantity)
        proceeds += fill * price
        remaining -= fill
        used_levels += 1
        worst_bid = price
        if remaining <= max(1e-12, target_quantity * 1e-12):
            remaining = 0.0
            break
    if remaining > 0:
        raise RuntimeError(
            f"{exchange} {canonical_symbol} 合约买盘深度不足："
            f"缺少 {remaining:.12g} / {target_quantity:.12g}"
        )
    received_ms = int(request_meta.get("receivedAt") or int(time.time() * 1000))
    return {
        "source": "exchange_public_depth",
        "exchange": exchange,
        "rawSymbol": raw_base,
        "targetQuantity": target_quantity,
        "filledQuantity": target_quantity,
        "proceedsUsdt": proceeds,
        "averageSellPrice": proceeds / target_quantity,
        "bestBid": best_bid,
        "worstBid": worst_bid,
        "usedLevels": used_levels,
        "availableLevels": len(rows) if isinstance(rows, list) else 0,
        "depthLimit": depth_limit,
        "quantityMultiplier": quantity_multiplier,
        "timestamp": received_ms,
        "requestDurationMs": round(max(0, received_ms - started_ms), 1),
        "cacheHit": bool(request_meta.get("cacheHit")),
        "joinedInflight": bool(request_meta.get("joinedInflight")),
        "endpoint": endpoint,
    }


def _fetch_okxdex_executable_preflight(
    client: httpx.Client,
    symbol: str,
    dex_config: dict[str, Any],
    sell_exchange: str,
    aliases: dict[tuple[str, str, str], tuple[str, float]],
    config: Any,
) -> dict[str, Any]:
    """Quote the real DEX route and hedge its exact output against futures depth."""

    chain_index = str(dex_config.get("chainIndex") or "").strip()
    contract_address = str(dex_config.get("contractAddress") or "").strip()
    if not chain_index or not contract_address:
        raise RuntimeError("OKXDEX 真实询价缺少链或合约地址")
    min_notional = astro_min_notional_usdt()
    quote_notional = astro_max_notional_usdt(config)
    response = client.get(
        spread_okxdex_quote_bridge_url(),
        params={
            "symbol": symbol,
            "chainIndex": chain_index,
            "contractAddress": contract_address,
            "amountUsdt": quote_notional,
            "exchange": dex_config.get("exchange", "okxdex"),
        },
        timeout=spread_okxdex_quote_timeout_seconds(),
    )
    response.raise_for_status()
    quote = response.json()
    if not isinstance(quote, dict):
        raise RuntimeError("OKXDEX 真实询价返回格式错误")
    dex_exchange = dex_config.get("exchange", "okxdex")
    if dex_exchange == "pancakeswapv3" and (quote.get("exchange") != dex_exchange or quote.get("source") != "pancakeswap_v3_quoter_eth_call"):
        raise RuntimeError("PancakeSwap 询价来源不匹配")
    quote_chain = str(quote.get("chainIndex") or "").strip()
    quote_contract = _normalized_contract_address(quote_chain, quote.get("contractAddress"))
    expected_contract = _normalized_contract_address(chain_index, contract_address)
    if quote_chain != chain_index or quote_contract != expected_contract:
        raise RuntimeError("OKXDEX 真实询价返回了不同链或不同合约")
    from_amount = _finite(quote.get("fromAmount"))
    token_quantity = _finite(quote.get("toAmount"))
    network_fee_usd = _finite(quote.get("tradeFeeUsd"))
    quoted_at = int(_finite(quote.get("quotedAt")) or 0)
    if (
        from_amount is None
        or token_quantity is None
        or network_fee_usd is None
        or from_amount <= 0
        or token_quantity <= 0
        or network_fee_usd < 0
        or quoted_at <= 0
    ):
        raise RuntimeError("OKXDEX 真实询价缺少成交数量或网络费用")
    if abs(from_amount - quote_notional) > max(0.01, quote_notional * 0.001):
        raise RuntimeError("OKXDEX 真实询价金额与卡片下单金额不一致")

    futures = _fetch_futures_bid_depth_execution(
        client,
        sell_exchange,
        symbol,
        aliases,
        token_quantity,
    )
    # The official quote's input/output already reflects the executable route,
    # including pool fees and price impact.  The configured slippage is only a
    # maximum execution tolerance; it is not a cost that always occurs, so do
    # not subtract the full tolerance from the spread.  The quote bridge
    # returns the route/network fee in USD, which is charged once at this exact
    # quote notional and must likewise not be scaled to another order size.
    slippage_pct = _finite(dex_config.get("slippage"))
    if slippage_pct is None:
        slippage_pct = spread_okxdex_slippage_pct()
    slippage_reserve_usd = 0.0
    network_fee_reserve_usd = network_fee_usd
    actual_cost_usdt = from_amount + network_fee_usd
    dex_average_cost = actual_cost_usdt / token_quantity
    futures_average_sell = _finite(futures.get("averageSellPrice"))
    if futures_average_sell is None or futures_average_sell <= 0:
        raise RuntimeError("合约同数量深度成交均价无效")
    net_spread_pct = _spread_pct(dex_average_cost, futures_average_sell)
    gross_spread_pct = _spread_pct(from_amount / token_quantity, futures_average_sell)
    if net_spread_pct is None or gross_spread_pct is None:
        raise RuntimeError("OKXDEX 净可执行价差无法计算")
    return {
        "source": str(quote.get("source") or "okx_v6_quote") + "+exchange_public_depth",
        "networkFeeEstimated": quote.get("networkFeeEstimated") is True,
        "quotedAt": quoted_at,
        "quoteAgeSeconds": round(max(0.0, (int(time.time() * 1000) - quoted_at) / 1000), 3),
        "quoteNotionalUsdt": quote_notional,
        "minCardNotionalUsdt": min_notional,
        "tokenQuantity": token_quantity,
        "dexQuotedAverageCost": from_amount / token_quantity,
        "dexConservativeAverageCost": dex_average_cost,
        "futuresAverageSellPrice": futures_average_sell,
        "grossExecutableSpreadPct": gross_spread_pct,
        "netExecutableSpreadPct": net_spread_pct,
        "slippagePct": slippage_pct,
        "slippageAppliedAsLimitOnly": True,
        "slippageReserveUsd": slippage_reserve_usd,
        "networkFeeUsd": network_fee_usd,
        "networkFeeReserveUsd": network_fee_reserve_usd,
        "networkFeeAmortizedAtUsdt": quote_notional,
        "dexActualCostUsdt": actual_cost_usdt,
        # Retain the legacy response names for existing logs/UI consumers. The
        # values now mean actual quoted cost plus the returned fee, not a full
        # configured-slippage deduction.
        "conservativeDexCostUsdt": actual_cost_usdt,
        "priceImpactPercent": _finite(quote.get("priceImpactPercent")),
        "buyTaxRate": _finite(quote.get("buyTaxRate")),
        "estimateGasFee": quote.get("estimateGasFee"),
        "quoteId": quote.get("quoteId"),
        "routeDexes": quote.get("routeDexes") if isinstance(quote.get("routeDexes"), list) else [],
        "futuresDepth": futures,
    }


def _activate_cloud_backup(pair: dict[str, Any], report: dict[str, Any]) -> None:
    if not _cloud_route_supported(pair):
        return
    sources = _api_degraded_fault_sources(pair, report)
    now_iso = datetime.now(timezone.utc).isoformat()
    newly_active = []
    route_changed = _route_policy.activate_cloud(pair, sources)
    with _api_degraded_lock:
        for source in sources:
            if source not in _depth_backup_sources:
                _depth_backup_sources[source] = {"startedAt": now_iso, "startedMonotonic": time.monotonic()}
                _reset_api_recovery_streak_locked(source)
                newly_active.append(source)
            incident = _api_degraded_incidents.setdefault(source, {
                "source": source, "startedAt": now_iso, "failureCount": 0, "affectedSymbols": set(),
            })
            incident["lastFailureAt"] = now_iso
            incident["failureCount"] += 1
            incident["lastError"] = str(report.get("error") or "")[:500]
            incident["affectedSymbols"].add(str(pair.get("name") or ""))
    if route_changed:
        _log_api_channel_transition("astro_depth_backup_activated", level="info", module="astro_spread_scanner",
            message="单条路线超过本机复核期限，切腾讯云备用；其他路线不受影响",
            details={"sources": sources, "route": route_key(pair), "symbol": pair.get("name"), "localFailure": report})


def _fetch_local_route_with_deadline(pair, config, *, executable_depth_first=False):
    """Bound the entire local observation, including queueing.

    Late workers can finish their read-only requests, but cannot submit cards
    or replace the selected cloud evidence. At most six workers can linger.
    """
    global _local_depth_deadline_executor
    started = time.monotonic()
    budget = _local_depth_deadline_seconds()
    future = None
    result = None
    report = {"reason": "local_depth_deadline_exceeded", "error": "local depth deadline timeout"}
    if _local_depth_deadline_slots.acquire(timeout=budget):
        try:
            with _api_degraded_lock:
                if _local_depth_deadline_executor is None:
                    _local_depth_deadline_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="astro-local-depth")
                future = _local_depth_deadline_executor.submit(
                    _fetch_direct_route_once_local,
                    {**pair, "_depthDeadlineMonotonic": started + budget - 0.1}, config,
                    executable_depth_first=executable_depth_first,
                )
            future.add_done_callback(lambda _: _local_depth_deadline_slots.release())
        except Exception:
            _local_depth_deadline_slots.release()
            raise
        try:
            result, report = future.result(timeout=max(0.0, budget - (time.monotonic() - started)))
        except FutureTimeoutError:
            future.cancel()
        except Exception as exc:
            report = {"reason": "direct_quote_unavailable", "error": str(exc)}
    elapsed = time.monotonic() - started
    if _stop.is_set():
        return None, {"reason": "scanner_stopped"}
    if elapsed >= budget:
        return None, {"reason": "local_depth_deadline_exceeded", "error": "local depth deadline timeout",
                      "localDeadlineMs": round(budget * 1000), "localElapsedMs": round(elapsed * 1000, 1)}
    if result is None and _api_degraded_transport_report(report):
        # Even an early connection error switches on elapsed time, not count.
        _stop.wait(max(0.0, budget - elapsed))
    if _stop.is_set():
        return None, {"reason": "scanner_stopped"}
    return result, {**report, "localDeadlineMs": round(budget * 1000),
                    "localElapsedMs": round((time.monotonic() - started) * 1000, 1)}


def _cloud_verification_unavailable(report: dict[str, Any]) -> bool:
    return route_fault_category(report) is not None


def _finish_cloud_observation(pair, result, report, *, observe=True):
    unavailable = result is None and _cloud_verification_unavailable(report)
    kind = route_fault_category(report) if result is None else None
    with _api_degraded_lock:
        # A successful HTTP book that fails freshness/skew is a safety
        # rejection, not a broken API or a failed cloud worker.
        key = "failedObservations" if unavailable and kind != "quote_quality" else "passedObservations" if result is not None else "rejectedObservations"
        _depth_cloud_metrics[key] = int(_depth_cloud_metrics.get(key) or 0) + 1
        if kind == "quote_quality":
            _depth_cloud_metrics["qualityRejectedObservations"] = int(_depth_cloud_metrics.get("qualityRejectedObservations") or 0) + 1
        _depth_cloud_metrics["lastError"] = str(report.get("error") or report.get("reason")) if kind in {"transport", "capacity"} else None
        _depth_cloud_metrics["lastObservationError"] = str(report.get("error") or report.get("reason")) if unavailable else None
        _depth_cloud_metrics["lastObservationReason"] = report.get("reason")
        _depth_cloud_metrics["lastDurationMs"] = report.get("cloudDurationMs")
    if observe:
        _route_policy.observe(pair, report, transport="tencent_cloud", available=not unavailable)
    return result, report


def _local_emergency_probe_ok(pair):
    now = time.time()
    with _api_degraded_lock:
        for source in {str(pair.get("buyEx")), str(pair.get("sellEx"))}:
            if source not in _api_degraded_incidents:
                continue
            state = _api_recovery_probe_state.get(source, {})
            try:
                age = now-datetime.fromisoformat(state["lastProbeAt"]).timestamp()
            except (KeyError, ValueError, TypeError):
                return False
            if state.get("lastProbeError") or not 0<=age<=4:
                return False
    return True


def _fetch_queued_cloud_route(pair, config):
    # The closure is replaced for duplicate pending routes; execution always
    # obtains fresh books, never a price saved while waiting in the queue.
    captured_pair = dict(pair)
    priority = _route_policy.priority(pair)
    return _cloud_route_queue.run(route_key(pair), lambda: _fetch_direct_route_once_local(captured_pair, config, executable_depth_first=True, use_cloud=True), priority=priority)


def _cloud_route_supported(pair):
    exchanges = {str(pair.get("buyEx")), str(pair.get("sellEx"))}
    return str(pair.get("type")) in {"FF", "SF"} and exchanges <= set(PULSE_PRIMARY_EXCHANGES)


def _fetch_direct_route_once(pair, config, *, executable_depth_first=False):
    from app.astro_depth_transport import enabled as cloud_enabled
    supported = _cloud_route_supported(pair)
    preferred = pair.get("_depthTransport") == "tencent_cloud" or (pair.get("_depthTransport") != "local_proxy" and _route_policy.use_cloud(pair))
    local_report = None
    if not preferred or not supported or not cloud_enabled():
        if supported:
            result, local_report = _fetch_local_route_with_deadline(pair, config, executable_depth_first=executable_depth_first)
        else:
            result, local_report = _fetch_direct_route_once_local(pair, config, executable_depth_first=executable_depth_first)
        local_report["transport"] = "local_proxy"
        if not supported:
            # DEX recovery is evidenced by the next actual route quote, not a
            # CEX depth probe. Never select an unavailable cloud fallback.
            _route_policy.observe(pair, local_report, transport="local_proxy",
                                  available=route_fault_category(local_report) is None)
            if result is not None:
                result["_depthTransport"] = "local_proxy"
            return result, local_report
        if result is not None or not _api_degraded_transport_report(local_report):
            if supported:
                _route_policy.observe(pair, local_report, transport="local_proxy", available=route_fault_category(local_report) is None, alertable=False)
            if result is not None:
                result["_depthTransport"] = "local_proxy"
            return result, local_report
        _activate_cloud_backup(pair, local_report)
        if not cloud_enabled():
            return _finish_cloud_observation(pair, None, {
                "reason": "cloud_depth_disabled",
                "error": "腾讯云备用未启用",
                "localFailure": local_report, "unverifiedCardAllowed": False, "transport": "tencent_cloud",
            })
    with _api_degraded_lock:
        _depth_cloud_metrics["attempted"] += 1
    started = time.monotonic()
    try:
        result, cloud_report = _fetch_queued_cloud_route(pair, config)
    except QueueBusy as exc:
        result, cloud_report = None, {"reason": "cloud_depth_busy", "error": str(exc)}
    except Exception as exc:
        result, cloud_report = None, {"reason": "direct_quote_unavailable", "error": str(exc)}
    cloud_report.update(transport="tencent_cloud", cloudDurationMs=round((time.monotonic()-started)*1000, 1), unverifiedCardAllowed=False)
    if local_report:
        cloud_report["localFailure"] = local_report
    if result is not None:
        result["_depthTransport"] = "tencent_cloud"
    if result is None and route_fault_category(cloud_report) in {"transport", "capacity"} and _local_emergency_probe_ok(pair):
        local_pair, emergency = _fetch_local_route_with_deadline(pair, config, executable_depth_first=True)
        if route_fault_category(emergency) is None and emergency.get("reason") != "scanner_stopped":
            _finish_cloud_observation(pair, result, cloud_report, observe=False)
            emergency.update(transport="local_proxy", emergencyFailback=True, cloudFailure=cloud_report)
            _route_policy.observe(pair, emergency, transport="local_proxy", available=True)
            if local_pair is not None:
                local_pair["_depthTransport"] = "local_proxy"
            return local_pair, emergency
    return _finish_cloud_observation(pair, result, cloud_report)


@_astro_api_priority
def _fetch_direct_route_once_local(
    pair: dict[str, Any],
    config: Any,
    *,
    executable_depth_first: bool = False,
    use_cloud: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    expected = _astro_route_identity(pair)
    symbol, pair_type, buy_exchange, sell_exchange = expected
    route = {"symbol": symbol, "type": pair_type, "buyExchange": buy_exchange, "sellExchange": sell_exchange}
    restriction = _route_contract_restriction(route)
    if restriction:
        _record_contract_restriction(route, restriction)
        return None, {"reason": restriction["reason"], "contractLifecycle": restriction}
    buy_market = "spot" if pair_type == "SF" else "future"
    sell_market = "future"
    aliases = _load_pulse_symbol_aliases()
    timeout_seconds = spread_final_revalidation_timeout_seconds()
    expected_dex_config = pair.get("_dexConfig") if isinstance(pair.get("_dexConfig"), dict) else None
    okxdex_preflight: dict[str, Any] | None = None
    cex_preflight: dict[str, Any] | None = None
    if use_cloud:
        from app.astro_depth_transport import CloudPublicClient
        client_factory = CloudPublicClient
    else:
        client_factory = httpx.Client
    try:
        with client_factory(
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": "stock-review-mac/astro-direct-revalidation"},
        ) as client:
            client.depth_round_started_ms = int(time.time() * 1000)
            client.depth_observation_deadline = pair.get("_depthDeadlineMonotonic", time.monotonic() + _local_depth_deadline_seconds() - 0.1)
            if executable_depth_first and buy_exchange not in SF_DEX_EXCHANGES:
                # Hot candidates already passed Pulse discovery. Fetch both
                # executable books immediately instead of paying for a
                # redundant ticker round before the depth round.
                cex_preflight = _fetch_cex_executable_preflight(
                    client, symbol, pair_type, buy_exchange, sell_exchange, aliases, config,
                )
                buy, sell = _direct_quotes_from_cex_preflight(
                    cex_preflight,
                    symbol=symbol,
                    pair_type=pair_type,
                    buy_exchange=buy_exchange,
                    sell_exchange=sell_exchange,
                )
            else:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    buy_args = [client, buy_exchange, buy_market, symbol, aliases]
                    if buy_exchange in SF_DEX_EXCHANGES:
                        buy_args.append(expected_dex_config)
                    buy_future = executor.submit(
                        _call_with_astro_api_priority,
                        _fetch_direct_quote,
                        *buy_args,
                    )
                    sell_future = executor.submit(
                        _call_with_astro_api_priority,
                        _fetch_direct_quote,
                        client,
                        sell_exchange,
                        sell_market,
                        symbol,
                        aliases,
                    )
                    try:
                        buy = buy_future.result()
                        sell = sell_future.result()
                    except Exception as exc:
                        return None, {"reason": "direct_quote_unavailable", "error": str(exc)}

    except Exception as exc:
        return None, {"reason": "direct_quote_unavailable", "error": str(exc)}

    if buy_exchange in SF_DEX_EXCHANGES:
        dex_config = buy.get("dexConfig") if isinstance(buy.get("dexConfig"), dict) else expected_dex_config
        if not isinstance(dex_config, dict):
            return None, {
                "reason": "okxdex_executable_quote_unavailable",
                "error": "OKXDEX 真实询价缺少链和合约配置",
            }
        try:
            with httpx.Client(
                timeout=timeout_seconds,
                headers={"Accept": "application/json", "User-Agent": "stock-review-mac/astro-okxdex-preflight"},
            ) as execution_client:
                okxdex_preflight = _fetch_okxdex_executable_preflight(
                    execution_client,
                    symbol,
                    dex_config,
                    sell_exchange,
                    aliases,
                    config,
                )
        except Exception as exc:
            return None, {
                "reason": "okxdex_executable_quote_unavailable",
                "error": str(exc),
                "pulseDiscoveryQuote": buy,
            }
        buy = {
            **buy,
            "bid": okxdex_preflight["dexQuotedAverageCost"],
            "ask": okxdex_preflight["dexConservativeAverageCost"],
            "timestamp": okxdex_preflight["quotedAt"],
            "timestampSource": "pancakeswap_v3_executable_quote" if buy_exchange == "pancakeswapv3" else "okx_v6_executable_quote",
            "quoteEndpoint": "okx_v6_dex_aggregator_quote",
            "pulseDiscoveryTimestamp": buy.get("timestamp"),
        }
        depth = okxdex_preflight["futuresDepth"]
        sell = {
            **sell,
            "bid": depth["averageSellPrice"],
            "timestamp": depth["timestamp"],
            "timestampSource": "response_received_depth",
            "quoteEndpoint": "order_book_depth",
            "topBookBid": sell.get("bid"),
        }
    elif cex_preflight is None:
        top_book_now_ms = int(time.time() * 1000)
        for leg in (buy, sell):
            leg["quoteAgeSeconds"] = round(
                max(0.0, (top_book_now_ms - leg["timestamp"]) / 1000),
                3,
            )
        top_book_skew_seconds = round(abs(buy["timestamp"] - sell["timestamp"]) / 1000, 3)
        standard_max_age = spread_final_revalidation_max_quote_age_seconds()
        top_book_report = {
            "latestOpenSpreadPct": _spread_pct(buy["ask"], sell["bid"]),
            "quoteSkewSeconds": top_book_skew_seconds,
            "buyQuote": buy,
            "sellQuote": sell,
            "quoteAgeLimitsSeconds": {"buy": standard_max_age, "sell": standard_max_age},
            "timestampSkewCheckApplied": True,
        }
        if buy["quoteAgeSeconds"] > standard_max_age or sell["quoteAgeSeconds"] > standard_max_age:
            return None, {**top_book_report, "reason": "stale_direct_quote"}
        if top_book_skew_seconds > spread_final_revalidation_max_skew_seconds():
            return None, {**top_book_report, "reason": "direct_quote_time_skew"}
        preliminary_opening = _spread_pct(buy["ask"], sell["bid"])
        minimum_opening = (
            spread_scan_ff_min_open_pct()
            if pair_type == "FF"
            else spread_scan_sf_route_min_open_pct(buy_exchange)
        )
        # A depth-weighted price cannot improve on the executable top of book.
        # Only routes whose top prices could pass need the extra two depth API
        # requests; every actual create decision still requires this preflight.
        if (
            preliminary_opening is not None
            and preliminary_opening > minimum_opening
            and preliminary_opening <= _route_max_open_pct(pair)
        ):
            try:
                with httpx.Client(
                    timeout=timeout_seconds,
                    headers={"Accept": "application/json", "User-Agent": "stock-review-mac/astro-cex-depth-preflight"},
                ) as execution_client:
                    cex_preflight = _fetch_cex_executable_preflight(
                        execution_client,
                        symbol,
                        pair_type,
                        buy_exchange,
                        sell_exchange,
                        aliases,
                        config,
                    )
            except Exception as exc:
                return None, {
                    "reason": "cex_executable_depth_unavailable",
                    "error": str(exc),
                    "topBookBuyQuote": buy,
                    "topBookSellQuote": sell,
                }
            buy, sell = _direct_quotes_from_cex_preflight(
                cex_preflight,
                symbol=symbol,
                pair_type=pair_type,
                buy_exchange=buy_exchange,
                sell_exchange=sell_exchange,
                buy_quote=buy,
                sell_quote=sell,
            )

    now_ms = int(time.time() * 1000)
    for leg in (buy, sell):
        leg["quoteAgeSeconds"] = round(max(0.0, (now_ms - leg["timestamp"]) / 1000), 3)
    quote_skew_seconds = round(abs(buy["timestamp"] - sell["timestamp"]) / 1000, 3)
    opening = _spread_pct(buy["ask"], sell["bid"])
    closing = _spread_pct(buy["bid"], sell["ask"])
    report = {
        "reason": "eligible",
        "latestOpenSpreadPct": opening,
        "quoteSkewSeconds": quote_skew_seconds,
        "buyQuote": buy,
        "sellQuote": sell,
    }
    if okxdex_preflight is not None:
        report["okxdexExecutablePreflight"] = okxdex_preflight
        report["pulseDiscoveryOnly"] = True
    if cex_preflight is not None:
        report["cexExecutablePreflight"] = cex_preflight
        report["topBookDiscoveryOnly"] = not executable_depth_first
        report["preliminaryTickerSkipped"] = executable_depth_first
    standard_max_age = spread_final_revalidation_max_quote_age_seconds()
    dex_max_age = spread_final_revalidation_okxdex_max_quote_age_seconds()
    buy_max_age = (
        spread_final_revalidation_okxdex_submit_max_quote_age_seconds()
        if buy_exchange in SF_DEX_EXCHANGES and okxdex_preflight is not None
        else dex_max_age
        if buy_exchange in SF_DEX_EXCHANGES
        else standard_max_age
    )
    sell_max_age = dex_max_age if sell_exchange in SF_DEX_EXCHANGES else standard_max_age
    # DEX validity is governed by its quote lifetime and the 3-second CEX leg
    # age.  A hard cross-leg timestamp match is meaningful only for CEX/CEX.
    enforce_timestamp_skew = not (SF_DEX_EXCHANGES & {buy_exchange, sell_exchange})
    report["quoteAgeLimitsSeconds"] = {
        "buy": buy_max_age,
        "sell": sell_max_age,
    }
    report["timestampSkewCheckApplied"] = enforce_timestamp_skew
    if buy["quoteAgeSeconds"] > buy_max_age or sell["quoteAgeSeconds"] > sell_max_age:
        return None, {**report, "reason": "stale_direct_quote"}
    if enforce_timestamp_skew and quote_skew_seconds > spread_final_revalidation_max_skew_seconds():
        return None, {**report, "reason": "direct_quote_time_skew"}
    if opening is None or closing is None:
        return None, {**report, "reason": "direct_spread_unavailable"}
    if opening <= 0:
        return None, {**report, "reason": "open_spread_non_positive"}

    current = {
        "type": pair_type,
        "symbol": symbol,
        "marketSymbol": f"{symbol}USDT",
        "buyExchange": buy_exchange,
        "sellExchange": sell_exchange,
        "openSpreadPct": opening,
        "closeSpreadPct": closing,
        "buyMarket": buy_market,
        "sellMarket": sell_market,
        "quoteAt": min(buy["timestamp"], sell["timestamp"]),
    }
    volume_evidence_required = (
        "_buyVolume24hUsdt" in pair or "_sellVolume24hUsdt" in pair
    )
    if volume_evidence_required:
        current["buyVolume24hUsdt"] = pair.get("_buyVolume24hUsdt")
        current["sellVolume24hUsdt"] = pair.get("_sellVolume24hUsdt")
    current["listingVolumeWindows"] = dict(pair.get("_listingVolumeWindows") or {})
    report["fundingIncluded"] = False
    if isinstance(pair.get("_targetSelection"), dict):
        current["targetSelection"] = dict(pair["_targetSelection"])
    if pair_type == "SF" and buy.get("dexConfig"):
        current["dexConfig"] = dict(buy["dexConfig"])
        if okxdex_preflight is not None:
            current["okxdexExecutablePreflight"] = okxdex_preflight
        if buy_exchange in SF_DEX_EXCHANGES:
            if spread_okxdex_identity_verification_enabled():
                verified, dex_summary = _verify_okxdex_candidate_identities([current], eligible_only=False)
                dex_identity = verified[0].get("dexIdentity") if verified else None
                report["dexIdentity"] = dex_identity
                if not isinstance(dex_identity, dict) or dex_identity.get("status") != "verified":
                    return None, {
                        **report,
                        "reason": "dex_identity_verification_failed",
                        "dexIdentitySummary": dex_summary,
                    }
            else:
                current["dexIdentity"] = {
                    "status": "bypassed",
                    "reason": "OKXDEX 链地址身份安全拦截已临时关闭",
                }
                report["dexIdentity"] = current["dexIdentity"]
            current["dexMapping"] = _dex_mapping_confirmation(current)
            report["dexMapping"] = current["dexMapping"]
            if current["dexMapping"].get("status") != "confirmed":
                return None, {**report, "reason": "dex_mapping_unconfirmed"}
    if pair_type == "FF":
        eligible = (
            (not volume_evidence_required or _auto_card_volume_check(current)[0])
            and
            opening <= _route_max_open_pct(current)
            and opening > spread_scan_ff_min_open_pct()
            and bool(astro_spread_card_routes(current))
        )
    else:
        eligible = (
            (not volume_evidence_required or _auto_card_volume_check(current)[0])
            and
            opening <= _route_max_open_pct(current)
            and
            opening > spread_scan_sf_route_min_open_pct(buy_exchange)
            and buy_exchange in SF_AUTO_CARD_SPOT_EXCHANGES
            and _sf_route_supported(buy_exchange, sell_exchange)
        )
    if not eligible:
        volume_ok, volume_reason = _auto_card_volume_check(current)
        if volume_evidence_required and not volume_ok:
            return None, {**report, "reason": volume_reason}
        return None, {**report, "reason": "below_threshold_or_rule_failed"}
    from app.astro_sf_funding import check as check_sf_funding
    raw_funding_symbol = _direct_market_symbol(sell_exchange, "future", symbol, aliases)[0]
    funding_check = check_sf_funding(pair_type, opening, sell_exchange, raw_funding_symbol, _fetch_sf_funding, background=True)
    report["sfFundingCheck"] = funding_check
    report["fundingIncluded"] = pair_type == "SF" and funding_check["status"] != "spread_exempt"
    if not funding_check["eligible"]:
        return None, {**report, "reason": "sf_" + funding_check["status"]}
    # A funding request must not turn a once-fresh book into stale submission evidence.
    checked_ms = int(time.time() * 1000)
    if (checked_ms - buy["timestamp"]) / 1000 > buy_max_age or (checked_ms - sell["timestamp"]) / 1000 > sell_max_age:
        return None, {**report, "reason": "stale_direct_quote"}
    latest_pairs = build_astro_spread_pairs(current, config)
    latest_pair = next((item for item in latest_pairs if _astro_route_identity(item) == expected), None)
    if latest_pair is None:
        return None, {**report, "reason": "route_build_failed"}
    latest_pair["_listingVolumeWindows"] = current["listingVolumeWindows"]
    return latest_pair, report


def revalidate_astro_cleanup(
    pair: dict[str, Any],
    config: Any,
    observation: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Confirm deletion with two fresh direct-book reads; fail closed on errors."""

    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    observation_reason = str(observation.get("reason") or "")

    # Pulse/database-only signal.
    pulse_only_reasons = {"ff_structure_filter"}
    quote_at = _finite(observation.get("quoteAt"))
    if quote_at is None or (int(time.time() * 1000) - quote_at) / 1000 > spread_scan_max_quote_age_seconds():
        return False, {"reason": "cleanup_pulse_observation_stale"}

    rounds = spread_final_revalidation_rounds()
    for index in range(rounds):
        latest_pair, report = _fetch_direct_route_once(pair, config)
        check = {"round": index + 1, **report}
        checks.append(check)
        if latest_pair is not None and observation_reason not in pulse_only_reasons:
            return False, {
                "reason": "route_became_eligible_again",
                "roundsPassed": index,
                "checks": checks,
            }
        if latest_pair is None and report.get("reason") not in {
            "below_threshold_or_rule_failed",
            "open_spread_non_positive",
        }:
            return False, {
                "reason": str(report.get("reason") or "cleanup_direct_quote_unavailable"),
                "roundsPassed": index,
                "checks": checks,
            }
        if index + 1 < rounds:
            time.sleep(spread_final_revalidation_interval_seconds())
    return True, {
        "reason": "continuous_invalidity_confirmed",
        "observationReason": observation_reason,
        "durationMs": round((time.monotonic() - started) * 1000, 1),
        "roundsPassed": rounds,
        "checks": checks,
    }


def revalidate_astro_spread_pair(
    pair: dict[str, Any],
    config: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Fail closed unless two independent fresh checks keep the route eligible."""

    started = time.monotonic()
    if (
        str(pair.get("type") or "").strip().upper() == "SF"
        and str(pair.get("buyEx") or "").strip().lower() in SF_DEX_EXCHANGES
        and not spread_scan_dex_auto_card_enabled(str(pair.get("buyEx") or "").lower())
    ):
        report = {
            "reason": f"sf_{str(pair.get('buyEx') or '').lower()}_auto_card_paused",
            "durationMs": round((time.monotonic() - started) * 1000, 1),
            "roundsRequired": 0,
            "roundsPassed": 0,
        }
        _record_revalidation_outcome(pair, passed=False, report=report)
        return None, report
    if str(pair.get("buyEx") or "").lower() in SF_DEX_EXCHANGES:
        return _revalidate_dex_three_quotes(pair, config)
    round_reports: list[dict[str, Any]] = []
    latest_pair: dict[str, Any] | None = None
    is_okxdex = bool(SF_DEX_EXCHANGES & {
        str(pair.get("buyEx") or "").strip().lower(),
        str(pair.get("sellEx") or "").strip().lower(),
    })
    # Pulse is only discovery for OKXDEX. One official executable quote plus
    # the same-size futures depth check is the final gate; asking Pulse for a
    # second snapshot adds latency but no execution evidence.
    rounds = 1 if is_okxdex else spread_final_revalidation_rounds()
    report_source = "okx_v6_quote+exchange_public_depth" if is_okxdex else "exchange_public_api"
    previous_dex_timestamp: int | None = None
    dex_timestamps: list[int] = []
    dex_prices: list[float] = []
    for index in range(rounds):
        attempts = 1
        if is_okxdex and index > 0:
            poll_seconds = spread_final_revalidation_okxdex_poll_seconds()
            attempts = max(
                1,
                int(math.ceil(spread_final_revalidation_okxdex_distinct_wait_seconds() / poll_seconds)),
            )

        report: dict[str, Any] = {}
        for attempt in range(attempts):
            latest_pair, report = _fetch_direct_route_once(
                pair, config, executable_depth_first=not is_okxdex,
            )
            if latest_pair is None:
                break
            dex_quote = report.get("buyQuote") if str(pair.get("buyEx") or "").lower() in SF_DEX_EXCHANGES else report.get("sellQuote")
            dex_timestamp = int(_finite((dex_quote or {}).get("timestamp")) or 0)
            if not is_okxdex or index == 0 or (previous_dex_timestamp is not None and dex_timestamp > previous_dex_timestamp):
                break
            if attempt + 1 < attempts:
                time.sleep(spread_final_revalidation_okxdex_poll_seconds())

        if latest_pair is not None and is_okxdex:
            dex_quote = report.get("buyQuote") if str(pair.get("buyEx") or "").lower() in SF_DEX_EXCHANGES else report.get("sellQuote")
            dex_timestamp = int(_finite((dex_quote or {}).get("timestamp")) or 0)
            if index > 0 and (previous_dex_timestamp is None or dex_timestamp <= previous_dex_timestamp):
                latest_pair = None
                report = {
                    **report,
                    "reason": "okxdex_quote_not_advanced",
                    "previousDexTimestamp": previous_dex_timestamp,
                    "latestDexTimestamp": dex_timestamp,
                    "pollAttempts": attempts,
                }
            else:
                previous_dex_timestamp = dex_timestamp
                dex_timestamps.append(dex_timestamp)
                dex_price = _finite((dex_quote or {}).get("ask"))
                if dex_price is not None:
                    dex_prices.append(dex_price)
        if not is_okxdex and latest_pair is not None and round_reports and (report.get("transport") or "local_proxy") != (round_reports[-1].get("transport") or "local_proxy"):
            # A mid-verification failover starts a fresh two-cloud-book proof;
            # never combine the old local hit with just one cloud observation.
            latest_pair["_hotDirectHit"] = {"verifiedAtMs": int(time.time() * 1000), "report": dict(report)}
            return revalidate_astro_hot_direct_hit(latest_pair, config)
        report = {"round": index + 1, **report}
        round_reports.append(report)
        if latest_pair is None:
            final_report = {
                **report,
                "durationMs": round((time.monotonic() - started) * 1000, 1),
                "source": report_source,
                "roundsRequired": rounds,
                "roundsPassed": index,
                "checks": round_reports,
            }
            _record_revalidation_outcome(pair, passed=False, report=final_report)
            return None, final_report
        if index + 1 < rounds:
            if not is_okxdex:
                time.sleep(spread_hot_monitor_confirmation_interval_seconds())

    if is_okxdex:
        final_dex_quote = round_reports[-1].get("buyQuote") if str(pair.get("buyEx") or "").lower() in SF_DEX_EXCHANGES else round_reports[-1].get("sellQuote")
        final_dex_age = _finite((final_dex_quote or {}).get("quoteAgeSeconds"))
        submit_max_age = spread_final_revalidation_okxdex_submit_max_quote_age_seconds()
        if final_dex_age is None or final_dex_age > submit_max_age:
            report = {
                **round_reports[-1],
                "reason": "okxdex_quote_stale_at_submit",
                "dexQuoteAgeAtSubmitSeconds": final_dex_age,
                "dexSubmitMaxQuoteAgeSeconds": submit_max_age,
                "dexQuoteTimestamps": dex_timestamps,
                "durationMs": round((time.monotonic() - started) * 1000, 1),
                "source": report_source,
                "roundsRequired": rounds,
                "roundsPassed": rounds,
                "checks": round_reports,
            }
            _record_revalidation_outcome(pair, passed=False, report=report)
            return None, report
    final_report = round_reports[-1]
    report = {
        **final_report,
        "reason": final_report.get("reason", "eligible"),
        "durationMs": round((time.monotonic() - started) * 1000, 1),
        "source": report_source,
        "sourceCount": 2,
        "roundsRequired": rounds,
        "roundsPassed": rounds,
        "intervalMs": round(spread_hot_monitor_confirmation_interval_seconds() * 1000),
        "checks": round_reports,
    }
    if is_okxdex:
        report.update({
            "dexQuoteTimestamps": dex_timestamps,
            "dexQuotesDistinct": len(dex_timestamps) == rounds and len(set(dex_timestamps)) == rounds,
            "dexQuoteAgeAtSubmitSeconds": _finite((final_dex_quote or {}).get("quoteAgeSeconds")),
            "dexSubmitMaxQuoteAgeSeconds": spread_final_revalidation_okxdex_submit_max_quote_age_seconds(),
            "dexPriceChangePct": (
                round((dex_prices[-1] / dex_prices[0] - 1) * 100, 6)
                if len(dex_prices) >= 2 and dex_prices[0] > 0
                else None
            ),
        })
    if latest_pair is not None and isinstance(pair.get("_pipeline"), dict):
        latest_pair["_pipeline"] = dict(pair["_pipeline"])
    _record_revalidation_outcome(pair, passed=True, report=report)
    return latest_pair, report


def _allow_gate_degraded_confirmation(pair, *, captured_report, failed_report, captured_age_seconds) -> bool:
    """Retired exception: every ordinary CEX card requires two depth checks."""
    return False


def _dex_execution_metadata(report, pair):
    """Carry the actual quote source/fee flag through final DEX validation."""
    execution = report.get("okxdexExecutablePreflight")
    execution = execution if isinstance(execution, dict) else {}
    fallback = ("pancakeswap_v3_quote" if str(pair.get("buyEx") or "").lower() == "pancakeswapv3"
                else "okx_v6_quote") + "+exchange_public_depth"
    return {"source": execution.get("source") or report.get("source") or fallback,
            "networkFeeEstimated": execution.get("networkFeeEstimated", report.get("networkFeeEstimated", False)) is True}



def _revalidate_dex_three_quotes(pair, config):
    """Three sequential real quotes; never count a cached hot hit as a round.

    The gateway calls the venue quote endpoint/quoter for each request. Equal
    prices are valid, but repeated or regressing quote timestamps are not new
    evidence. Reject immediately instead of polling a stale response in a loop.
    """
    started = time.monotonic()
    checks = []
    stamps = []
    latest_pair = None
    for index in range(3):
        if index:
            time.sleep(1.0)
        latest_pair, report = _fetch_direct_route_once(pair, config)
        report = dict(report)
        if latest_pair is not None:
            execution = report.get("okxdexExecutablePreflight")
            timestamp = _finite((report.get("buyQuote") or {}).get("timestamp"))
            sell_timestamp = _finite((report.get("sellQuote") or {}).get("timestamp"))
            now_ms = time.time() * 1000
            dex_age = (now_ms - timestamp) / 1000 if timestamp else math.inf
            sell_age = (now_ms - sell_timestamp) / 1000 if sell_timestamp else math.inf
            reason = None
            if not isinstance(execution, dict) or not execution:
                reason = "okxdex_executable_quote_unavailable"
            elif not timestamp or (stamps and timestamp <= stamps[-1]):
                reason = "okxdex_quote_not_advanced"
            elif dex_age < -1 or dex_age > spread_final_revalidation_okxdex_submit_max_quote_age_seconds():
                reason = "okxdex_quote_stale_at_submit"
            elif sell_age < -1 or sell_age > spread_final_revalidation_max_quote_age_seconds():
                reason = "stale_direct_quote"
            else:
                opening = _finite(report.get("latestOpenSpreadPct"))
                if opening is None or opening <= spread_scan_sf_route_min_open_pct(pair.get("buyEx")):
                    reason = "below_threshold_or_rule_failed"
            report.update(dexQuoteAgeAtSubmitSeconds=dex_age if math.isfinite(dex_age) else None)
            if reason:
                latest_pair = None
                report.update(reason=reason)
            else:
                stamps.append(timestamp)
        checks.append({**report, "round": index + 1})
        if latest_pair is None:
            break
    passed = latest_pair is not None and len(stamps) == 3
    result = {**report, **_dex_execution_metadata(report, pair),
              "mode": "three_independent_dex_executable_quotes",
              "roundsRequired": 3, "roundsPassed": len(stamps), "checks": checks,
              "dexQuoteTimestamps": stamps, "dexQuotesDistinct": len(set(stamps)) == 3,
              "intervalMs": 1000, "durationMs": round((time.monotonic() - started) * 1000, 1)}
    if passed and isinstance(pair.get("_pipeline"), dict):
        latest_pair["_pipeline"] = dict(pair["_pipeline"])
    _record_revalidation_outcome(pair, passed=passed, report=result)
    return latest_pair if passed else None, result


def revalidate_astro_hot_direct_hit(
    pair: dict[str, Any],
    config: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Confirm a hot CEX hit with a second independent depth snapshot.

    Pulse never satisfies this gate. DEX routes require three new executable
    quotes and same-quantity futures depth checks. Ordinary CEX routes require
    that evidence twice so one cached, thin or time-skewed top price cannot
    create a card.
    """

    started = time.monotonic()
    if (
        str(pair.get("type") or "").strip().upper() == "SF"
        and str(pair.get("buyEx") or "").strip().lower() in SF_DEX_EXCHANGES
        and not spread_scan_dex_auto_card_enabled(str(pair.get("buyEx") or "").lower())
    ):
        report = {
            "reason": f"sf_{str(pair.get('buyEx') or '').lower()}_auto_card_paused",
            "source": "local_auto_card_rule",
            "mode": "create_blocked",
            "roundsRequired": 0,
            "roundsPassed": 0,
            "durationMs": round((time.monotonic() - started) * 1000, 1),
        }
        _record_revalidation_outcome(pair, passed=False, report=report)
        return None, report
    if str(pair.get("buyEx") or "").lower() in SF_DEX_EXCHANGES:
        return _revalidate_dex_three_quotes(pair, config)
    now_ms = int(time.time() * 1000)
    captured = pair.get("_hotDirectHit") if isinstance(pair.get("_hotDirectHit"), dict) else {}
    verified_at_ms = int(_finite(captured.get("verifiedAtMs")) or 0)
    captured_age_seconds = max(0.0, (now_ms - verified_at_ms) / 1000) if verified_at_ms else math.inf
    is_okxdex = bool(SF_DEX_EXCHANGES & {
        str(pair.get("buyEx") or "").strip().lower(),
        str(pair.get("sellEx") or "").strip().lower(),
    })
    if verified_at_ms and captured_age_seconds <= spread_hot_monitor_hit_ttl_seconds():
        captured_report = captured.get("report") if isinstance(captured.get("report"), dict) else {}
        if not is_okxdex:
            # Queue preparation already separates the observations. Only wait
            # for the part of the minimum interval that has not elapsed yet.
            remaining = spread_hot_monitor_confirmation_interval_seconds() - captured_age_seconds
            if remaining > 0:
                time.sleep(remaining)
            latest_pair, direct_report = _fetch_direct_route_once(
                pair,
                config,
                executable_depth_first=True,
            )
            if latest_pair is not None and (direct_report.get("transport") or "local_proxy") != (captured_report.get("transport") or "local_proxy"):
                captured_report = dict(direct_report)
                captured_age_seconds = 0.0
                time.sleep(spread_hot_monitor_confirmation_interval_seconds())
                latest_pair, direct_report = _fetch_direct_route_once(
                    latest_pair, config, executable_depth_first=True,
                )
                if latest_pair is not None and (direct_report.get("transport") or "local_proxy") != (captured_report.get("transport") or "local_proxy"):
                    latest_pair = None
                    direct_report = {**direct_report, "reason": "verification_transport_changed", "error": "两轮复核通道发生变化，重新等待完整证据"}
            checks = [
                {"round": 1, **captured_report},
                {"round": 2, **direct_report},
            ]
            if latest_pair is None:
                report = {
                    **direct_report,
                    "source": "exchange_public_depth_same_quantity",
                    "mode": "two_independent_executable_depth_checks",
                    "roundsRequired": 2,
                    "roundsPassed": 1,
                    "capturedHitAgeSeconds": round(captured_age_seconds, 3),
                    "intervalMs": round(spread_hot_monitor_confirmation_interval_seconds() * 1000),
                    "checks": checks,
                    "durationMs": round((time.monotonic() - started) * 1000, 1),
                }
                _record_revalidation_outcome(pair, passed=False, report=report)
                return None, report
            if isinstance(pair.get("_pipeline"), dict):
                latest_pair["_pipeline"] = dict(pair["_pipeline"])
            report = {
                **direct_report,
                "reason": direct_report.get("reason", "eligible"),
                "source": "exchange_public_depth_same_quantity",
                "mode": "two_independent_executable_depth_checks",
                "roundsRequired": 2,
                "roundsPassed": 2,
                "capturedHitAgeSeconds": round(captured_age_seconds, 3),
                "intervalMs": round(spread_hot_monitor_confirmation_interval_seconds() * 1000),
                "checks": checks,
                "durationMs": round((time.monotonic() - started) * 1000, 1),
            }
            _record_revalidation_outcome(pair, passed=True, report=report)
            return latest_pair, report
    return revalidate_astro_spread_pair(pair, config)


def revalidate_astro_api_degraded_pair(pair, config):
    """Reject legacy queued Pulse-only cards, including after service restart."""
    report = {"reason": "api_unverified_card_disabled", "roundsRequired": 2,
              "roundsPassed": 0, "verificationDegraded": True, "source": "legacy_pulse_fallback_rejected"}
    _record_revalidation_outcome(pair, passed=False, report=report)
    return None, report


def _confirmed_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.monotonic()
    now_ms = int(time.time() * 1000)
    interval = spread_scan_interval_seconds()
    required = spread_scan_confirmations()
    qualifying = [item for item in candidates if _meets_auto_card_rule(item)]
    confirmed: list[dict[str, Any]] = []
    active_keys = {item["key"] for item in qualifying}
    with _hit_lock:
        for key in list(_hits):
            hit = _hits[key]
            if key not in active_keys or now - float(hit.get("lastHitMonotonic") or 0.0) > interval * 2.5:
                _hits.pop(key, None)
        for item in qualifying:
            previous = _hits.get(item["key"]) or {}
            last_hit = float(previous.get("lastHitMonotonic") or 0.0)
            continuous = now - last_hit <= interval * 2.5
            count = int(previous.get("count") or 0) + 1 if continuous else 1
            first_seen_ms = int(previous.get("firstSeenAtMs") or now_ms) if continuous else now_ms
            _hits[item["key"]] = {
                "count": count,
                "lastHitMonotonic": now,
                "firstSeenAtMs": first_seen_ms,
            }
            if count >= required and _revalidation_retry_allowed(item, now=now):
                pair_type = str(item.get("type") or "").upper()
                ordinary_threshold_pct = (
                    spread_scan_ff_min_open_pct()
                    if pair_type == "FF"
                    else spread_scan_sf_route_min_open_pct(item.get("buyExchange"))
                    if pair_type == "SF"
                    else None
                )
                confirmed.append(
                    {
                        **item,
                        **(
                            {
                                "_systemDeleteRearmOpenPosition": (
                                    ordinary_threshold_pct + astro_cleanup_rearm_buffer_pct_points()
                                )
                                / 100
                            }
                            if ordinary_threshold_pct is not None
                            else {}
                        ),
                        "_pipeline": {
                            "firstSeenAtMs": first_seen_ms,
                            "confirmedAtMs": now_ms,
                            "pulseOpenSpreadPct": _finite(item.get("openSpreadPct")),
                            "confirmationCount": count,
                        },
                    }
                )
    confirmed.sort(key=lambda item: item["openSpreadPct"], reverse=True)
    unique: list[dict[str, Any]] = []
    seen_routes: set[tuple[str, str, str, str, str, str]] = set()
    for item in confirmed:
        identity = _candidate_route_identity(item)
        if identity in seen_routes:
            continue
        seen_routes.add(identity)
        unique.append(item)
    return unique


def _active_listing_symbols() -> tuple[set[str], str | None]:
    """Return active exchange-listing symbols used by the priority monitor."""

    from app.database import SessionLocal
    from app.models import ExchangeDelistingOpportunityWatch

    now = datetime.now(timezone.utc)
    priority_started_at = now - timedelta(hours=2)
    try:
        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(ExchangeDelistingOpportunityWatch).where(
                        ExchangeDelistingOpportunityWatch.enabled.is_(True),
                        ExchangeDelistingOpportunityWatch.expires_at > now,
                        ExchangeDelistingOpportunityWatch.event_at >= priority_started_at,
                        ExchangeDelistingOpportunityWatch.announcement_key.endswith(":listing"),
                    )
                )
            )
    except Exception as exc:
        return set(), str(exc)
    return {
        str(row.symbol or "").strip().upper().removesuffix("USDT")
        for row in rows
        if str(row.symbol or "").strip()
    }, None


def _listing_volume_market_windows(routes: list[dict[str, Any]], *, now_ms: int | None = None) -> dict[tuple[str, str, str], int]:
    """Only explicit market launch times grant that market a two-hour window."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    windows = {}
    for route in routes:
        if route.get("listingEventTimeKnown") is not True:
            continue
        try:
            event_at = datetime.fromisoformat(str(route.get("listingEventAt") or "").replace("Z", "+00:00"))
            if event_at.tzinfo is None:
                continue
            event_ms = int(event_at.timestamp() * 1000)
        except (ValueError, TypeError, OverflowError):
            continue
        if not 0 <= now_ms - event_ms < 2 * 3600 * 1000:
            continue
        exchange = CRYPTO_TO_PULSE_EXCHANGE.get(str(route.get("exchange") or "").lower())
        market_type = "future" if route.get("market_type") in {"futures", "contract", "future"} else "spot" if route.get("market_type") == "spot" else None
        symbol = str(route.get("symbol") or "").upper().removesuffix("USDT")
        if exchange and market_type and symbol:
            windows[(symbol, exchange, market_type)] = event_ms
    return windows


def _active_listing_volume_windows() -> tuple[dict[tuple[str, str, str], int], str | None]:
    from app.database import SessionLocal
    from app.exchange_announcements import load_opportunity_routes
    from app.models import ExchangeDelistingOpportunityWatch

    now = datetime.now(timezone.utc)
    try:
        with SessionLocal() as db:
            rows = list(db.scalars(select(ExchangeDelistingOpportunityWatch).where(
                ExchangeDelistingOpportunityWatch.enabled.is_(True),
                ExchangeDelistingOpportunityWatch.expires_at > now,
                ExchangeDelistingOpportunityWatch.announcement_key.endswith(":listing"),
            )))
        windows = {}
        for row in rows:
            windows.update(_listing_volume_market_windows(load_opportunity_routes(row)))
        return windows, None
    except Exception as exc:
        return {}, str(exc)


def _listing_route_hint_candidates(
    symbol: str,
    routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build both executable FF directions from an announcement watch.

    These are discovery hints only.  Their placeholder spread can never create
    a card by itself: the hot monitor still has to fetch fresh same-quantity
    depth twice and pass every ordinary create gate.
    """

    from app.exchange_announcements import supported_opportunity_watch_pairs

    normalized_symbol = str(symbol or "").strip().upper().removesuffix("USDT")
    if not normalized_symbol:
        return []
    threshold = spread_scan_ff_min_open_pct()
    now_ms = int(time.time() * 1000)
    listing_windows = {}
    for route in routes:
        if route.get("listingEventTimeKnown") is not True:
            continue
        try:
            event_at = datetime.fromisoformat(str(route.get("listingEventAt") or "").replace("Z", "+00:00"))
            if event_at.tzinfo is None:
                continue
            event_ms = int(event_at.timestamp() * 1000)
        except (ValueError, TypeError, OverflowError):
            continue
        exchange = CRYPTO_TO_PULSE_EXCHANGE.get(str(route.get("exchange") or "").lower())
        market_type = "future" if route.get("market_type") in {"futures", "contract", "future"} else "spot" if route.get("market_type") == "spot" else None
        if exchange and market_type:
            listing_windows[(exchange, market_type)] = event_ms
    hints: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for left, right in supported_opportunity_watch_pairs(routes):
        left_exchange = CRYPTO_TO_PULSE_EXCHANGE.get(str(left.get("exchange") or "").lower())
        right_exchange = CRYPTO_TO_PULSE_EXCHANGE.get(str(right.get("exchange") or "").lower())
        if not left_exchange or not right_exchange:
            continue
        for buy_exchange, sell_exchange in (
            (left_exchange, right_exchange),
            (right_exchange, left_exchange),
        ):
            if (
                buy_exchange not in ASTRO_FF_BUY_EXCHANGES
                or sell_exchange not in ASTRO_FF_SELL_EXCHANGES
                or (buy_exchange, sell_exchange) in seen
            ):
                continue
            seen.add((buy_exchange, sell_exchange))
            hints.append(
                {
                    "key": f"FF:{normalized_symbol}:{buy_exchange}:{sell_exchange}:announcement",
                    "type": "FF",
                    "symbol": normalized_symbol,
                    "marketSymbol": f"{normalized_symbol}USDT",
                    "buyExchange": buy_exchange,
                    "sellExchange": sell_exchange,
                    "buyMarket": "future",
                    "sellMarket": "future",
                    "openSpreadPct": threshold + 0.000001,
                    "closeSpreadPct": 0.0,
                    "buyVolume24hUsdt": None,
                    "sellVolume24hUsdt": None,
                    "priorityNewListing": True,
                    "listingVolumeWindows": {
                        "buy": listing_windows.get((buy_exchange, "future")),
                        "sell": listing_windows.get((sell_exchange, "future")),
                    },
                    "announcementRouteHint": True,
                    "quoteAt": now_ms,
                    "quoteSource": "exchange_listing_announcement",
                }
            )
    return hints


def _active_listing_route_hints() -> tuple[list[dict[str, Any]], str | None]:
    from app.database import SessionLocal
    from app.exchange_announcements import load_opportunity_routes
    from app.models import ExchangeDelistingOpportunityWatch

    now = datetime.now(timezone.utc)
    priority_started_at = now - timedelta(hours=2)
    try:
        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(ExchangeDelistingOpportunityWatch).where(
                        ExchangeDelistingOpportunityWatch.enabled.is_(True),
                        ExchangeDelistingOpportunityWatch.expires_at > now,
                        ExchangeDelistingOpportunityWatch.event_at >= priority_started_at,
                        ExchangeDelistingOpportunityWatch.announcement_key.endswith(":listing"),
                    )
                )
            )
    except Exception as exc:
        return [], str(exc)
    hints: list[dict[str, Any]] = []
    for row in rows:
        hints.extend(
            _listing_route_hint_candidates(
                str(row.symbol or ""),
                load_opportunity_routes(row),
            )
        )
    return hints, None


def _hot_watch_base_eligible(candidate: dict[str, Any]) -> bool:
    """Apply every auto-card gate except the opening-spread threshold."""

    pair_type = str(candidate.get("type") or "").strip().upper()
    threshold = (
        spread_scan_ff_min_open_pct()
        if pair_type == "FF"
        else spread_scan_sf_route_min_open_pct(candidate.get("buyExchange"))
        if pair_type == "SF"
        else None
    )
    if threshold is None:
        return False
    probe = {**candidate, "openSpreadPct": threshold + 0.000001}
    if candidate.get("announcementRouteHint") is True:
        # Observe an announced market before Pulse publishes its turnover.
        # Actual creation still requires volume or a verified per-leg window.
        probe.update(buyVolume24hUsdt=spread_scan_min_volume_usdt(), sellVolume24hUsdt=spread_scan_min_volume_usdt())
    # New listings often have no meaningful historical structure window yet.
    # They still retain executable-price, liquidity, funding, route, mapping,
    # delisting and duplicate gates.
    if pair_type == "FF" and structure_filter_enabled():
        probe["structureAssessment"] = {"autoCardEligible": True, "priorityListingBypass": True}
    return _meets_auto_card_rule(probe)


def _hot_route_priority(item: dict[str, Any], *, now_ms: int | None = None) -> tuple[Any, ...]:
    """Keep first checks urgent, then revisit the least recently checked route.

    Pulse spread is only a tie breaker. Ranking it ahead of waiting time lets
    the same four routes monopolize a symbol indefinitely after the first pass.
    """

    reasons = set(item.get("reasons") or [])
    checks = int(item.get("checks") or 0)
    never_checked = checks <= 0 and not item.get("firstDirectCheckAtMs")
    above_threshold = "pulse_above_threshold" in reasons
    if never_checked and above_threshold:
        band = 0
    elif above_threshold and not item.get("priceBackoffCount"):
        band = 1
    elif never_checked:
        band = 2
    else:
        band = 3
    # Promotion is a scheduling guarantee at the next available worker, not a
    # promise that an in-flight exchange request can finish within five seconds.
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    last_start = int(item.get("lastDirectCheckStartedAtMs") or item.get("registeredAtMs") or 0)
    if item.get("registeredAtMs") and last_start and now_ms - last_start >= 5_000:
        band = -1
    if item.get("listingProbe"):
        # A scheduled launch without a verified book is discovery work. It
        # cannot outrank ordinary opportunities, even after waiting.
        band = 4
    spread = _finite((item.get("candidate") or {}).get("openSpreadPct"))
    identity = tuple(item.get("identity") or ())
    return (
        band,
        int(item.get("lastDirectCheckStartedAtMs") or item.get("registeredAtMs") or item.get("firstSeenAtMs") or 0),
        -(spread if spread is not None else -1_000_000.0),
        int(item.get("firstSeenAtMs") or 0),
        float(item.get("nextPollMonotonic") or 0.0),
        checks,
        identity,
    )


def _update_hot_price_cadence(item: dict[str, Any], report: dict[str, Any]) -> None:
    """Slow repeated, clearly distant price misses; never cache a passing book."""
    funding = report.get("sfFundingCheck") or report.get("fundingPrecheck") or {}
    if funding and not funding.get("eligible"):
        # This wait is independent of the existing price-miss backoff.
        delay = max(0.05, float(funding.get("retryAfterSeconds") or 0.5))
        item.update(fundingWaitUntilMonotonic=time.monotonic() + delay,
                    nextPollMonotonic=time.monotonic() + delay, pollIntervalMs=round(delay * 1000),
                    lastDirectDurationMs=report.get("durationMs"))
        # A newer Pulse may have crossed the warning band while funding was in flight.
        candidate = item.get("candidate") or {}
        pulse_changed = (funding.get("source") == "pulse_hint"
                         and (candidate.get("sellPulseFunding") or {}).get("rawRate") != funding.get("pulseRateRaw"))
        if (_finite(candidate.get("openSpreadPct")) or 0) >= 2.3 or pulse_changed:
            item["fundingWaitUntilMonotonic"] = None
            item["nextPollMonotonic"] = time.monotonic()
            item["pollIntervalMs"] = round(spread_hot_monitor_interval_seconds() * 1000)
        return
    item["fundingWaitUntilMonotonic"] = None
    opening = _finite(report.get("latestOpenSpreadPct"))
    kind = str((item.get("pair") or {}).get("type") or (item.get("candidate") or {}).get("type") or "")
    threshold = spread_scan_ff_min_open_pct() if kind == "FF" else spread_scan_sf_route_min_open_pct((item.get("pair") or {}).get("buyEx") or (item.get("candidate") or {}).get("buyExchange"))
    price_miss = report.get("reason") in {"open_spread_non_positive", "below_threshold_or_rule_failed"}
    distant = price_miss and opening is not None and opening < threshold - 0.15
    count = int(item.get("priceBackoffCount") or 0) + 1 if distant else 0
    interval = spread_hot_monitor_interval_seconds()
    if count >= 2:
        interval = max(interval, min(5.0, 2.0 ** min(count - 1, 3)))
    item.update(priceBackoffCount=count, pollIntervalMs=round(interval * 1000),
                lastExecutableSpreadPct=opening, lastDirectDurationMs=report.get("durationMs"))
    started = item.get("lastDirectCheckStartedMonotonic")
    if started is not None and not item.get("listingProbe"):
        item["nextPollMonotonic"] = float(started) + interval


def _select_hot_routes_for_cycle(
    all_due: list[dict[str, Any]],
    *,
    limit: int | None = None,
    per_symbol_limit: int | None = None,
    symbol_inflight_counts: dict[str, int] | None = None,
    listing_probe_inflight: int = 0,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Select a bounded batch while keeping same-symbol routes together.

    Grouping lets routes such as ZORA OKX/{BN,Gate,Aster,Bitget} share the
    identical OKX book through the single-flight depth cache. A per-symbol cap
    keeps one busy listing from starving every other newly discovered symbol.
    """

    route_limit = limit or spread_hot_monitor_max_routes_per_cycle()
    symbol_limit = per_symbol_limit or spread_hot_monitor_max_routes_per_symbol_cycle()
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ranked = sorted(all_due, key=lambda item: _hot_route_priority(item, now_ms=now_ms))
    grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    group_order: list[tuple[int, str, str]] = []
    for item in ranked:
        identity = tuple(item.get("identity") or ())
        key = (
            int(_hot_route_priority(item, now_ms=now_ms)[0]),
            str(identity[0] if len(identity) > 0 else ""),
            str(identity[1] if len(identity) > 1 else ""),
        )
        if key not in grouped:
            grouped[key] = []
            group_order.append(key)
        grouped[key].append(item)

    selected: list[dict[str, Any]] = []
    symbol_counts = dict(symbol_inflight_counts or {})
    probe_count = listing_probe_inflight
    for key in group_order:
        remaining = route_limit - len(selected)
        if remaining <= 0:
            break
        symbol = key[1]
        allowance = min(max(0, symbol_limit - symbol_counts.get(symbol, 0)), remaining)
        chosen = []
        for item in grouped[key]:
            if len(chosen) >= allowance:
                break
            if item.get("listingProbe") and probe_count >= 1:
                continue
            chosen.append(item)
            if item.get("listingProbe"):
                probe_count += 1
        selected.extend(chosen)
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + len(chosen)
    return selected


def _register_hot_candidates(
    candidates: list[dict[str, Any]],
    listing_symbols: set[str],
    config: Any,
) -> dict[str, Any]:
    """Refresh the continuously-polled direct-API watch set."""

    now = time.monotonic()
    now_ms = int(time.time() * 1000)
    ttl = spread_hot_monitor_route_ttl_seconds()
    added = 0
    refreshed = 0
    blocked_pairs, blocked_coins = _spread_block_rule_sets()
    with _hot_lock:
        for identity in list(_hot_routes):
            if now > float(_hot_routes[identity].get("expiresAtMonotonic") or 0.0):
                _hot_routes.pop(identity, None)
        for identity in list(_hot_recent_hits):
            if now - _hot_recent_hits[identity] > 10.0:
                _hot_recent_hits.pop(identity, None)
                _hot_recent_hit_at_ms.pop(identity, None)

        for candidate in candidates:
            if _route_block_match(
                symbol=candidate.get("symbol"),
                pair_type=candidate.get("type"),
                buy_exchange=candidate.get("buyExchange"),
                sell_exchange=candidate.get("sellExchange"),
                blocked_pairs=blocked_pairs,
                blocked_coins=blocked_coins,
            ) is not None:
                continue
            symbol = str(candidate.get("symbol") or "").strip().upper().removesuffix("USDT")
            announcement_hint = candidate.get("announcementRouteHint") is True
            # An explicit launch time can start one bounded discovery probe
            # even before Pulse covers the market. Future/unknown launches
            # remain discovery metadata and never consume a depth worker.
            if announcement_hint and not any(_active_listing_leg_window(candidate, leg) for leg in ("buy", "sell")):
                continue
            above_threshold = not announcement_hint and _meets_auto_card_rule(candidate)
            new_listing = (
                symbol in listing_symbols
                and any(_active_listing_leg_window(candidate, leg) for leg in ("buy", "sell"))
                and _hot_watch_base_eligible(candidate)
            )
            if not above_threshold and not new_listing:
                continue
            identity = _candidate_route_identity(candidate)
            # A first-round hit is not a created card. The actual existing /
            # syncing / queued state below deduplicates work; a failed second
            # round must not impose a separate ten-second discovery blackout.
            prepared = dict(candidate)
            pair_type = str(candidate.get("type") or "").strip().upper()
            ordinary_threshold_pct = (
                spread_scan_ff_min_open_pct()
                if pair_type == "FF"
                else spread_scan_sf_route_min_open_pct(candidate.get("buyExchange"))
                if pair_type == "SF"
                else None
            )
            if ordinary_threshold_pct is not None:
                prepared["_systemDeleteRearmOpenPosition"] = (
                    ordinary_threshold_pct + astro_cleanup_rearm_buffer_pct_points()
                ) / 100
            pairs = build_astro_spread_pairs(prepared, config)
            if not pairs:
                continue
            if astro_route_dedupe_state(pairs[0]) is not None:
                _hot_routes.pop(identity, None)
                continue
            previous = _hot_routes.get(identity)
            if announcement_hint and previous and previous.get("pulseObserved"):
                continue  # Never replace actual Pulse liquidity with a hint.
            prior_candidate = (previous or {}).get("candidate") or {}
            prior_spread = _finite(prior_candidate.get("openSpreadPct"))
            new_spread = _finite(candidate.get("openSpreadPct"))
            pulse_improved = bool(previous and not announcement_hint
                and (_finite(candidate.get("quoteAt")) or 0) > (_finite(prior_candidate.get("quoteAt")) or 0)
                and new_spread is not None and prior_spread is not None and new_spread >= prior_spread + 0.1)
            funding_hint_changed = ((candidate.get("sellPulseFunding") or {}).get("rawRate")
                                    != (prior_candidate.get("sellPulseFunding") or {}).get("rawRate"))
            funding_wake = bool(previous and previous.get("fundingWaitUntilMonotonic")
                and ((new_spread is not None and new_spread >= 2.3) or funding_hint_changed)
                and (_finite(candidate.get("quoteAt")) or 0) > (_finite(prior_candidate.get("quoteAt")) or 0))
            listing_probe = announcement_hint and not bool((previous or {}).get("marketBooksVerified"))
            reasons = sorted(
                reason
                for reason, enabled in (
                    ("pulse_above_threshold", above_threshold),
                    ("new_listing", new_listing),
                )
                if enabled
            )
            first_seen_at_ms = int((previous or {}).get("firstSeenAtMs") or now_ms)
            registered_at_ms = int((previous or {}).get("registeredAtMs") or now_ms)
            pair = pairs[0]
            windows = dict(candidate.get("listingVolumeWindows") or {})
            if announcement_hint and previous:
                windows = {**dict((previous.get("pair") or {}).get("_listingVolumeWindows") or {}),
                           **{leg: event for leg, event in windows.items() if event is not None}}
            pair["_listingVolumeWindows"] = windows
            pair["_listingDiscoveryOnly"] = listing_probe
            pair["_pipeline"] = {
                **dict(pair.get("_pipeline") or {}),
                "firstSeenAtMs": first_seen_at_ms,
                "hotWatchRegisteredAtMs": registered_at_ms,
                "hotWatchReasons": reasons,
                "pulseOpenSpreadPct": _finite(candidate.get("openSpreadPct")),
            }
            _hot_routes[identity] = {
                "identity": identity,
                "pair": pair,
                "candidate": prepared,
                "reasons": reasons,
                "firstSeenAtMs": first_seen_at_ms,
                "registeredAtMs": registered_at_ms,
                "lastPulseAtMs": now_ms,
                "expiresAtMonotonic": now + ttl,
                "nextPollMonotonic": (
                    now if (funding_wake or (pulse_improved and not (previous or {}).get("fundingWaitUntilMonotonic"))) else max(now, float((previous or {}).get("nextPollMonotonic") or now))
                ),
                "fundingWaitUntilMonotonic": None if funding_wake else (previous or {}).get("fundingWaitUntilMonotonic"),
                "priceBackoffCount": 0 if pulse_improved else int((previous or {}).get("priceBackoffCount") or 0),
                "pollIntervalMs": round(spread_hot_monitor_interval_seconds() * 1000) if (pulse_improved or funding_wake) else (previous or {}).get("pollIntervalMs"),
                "lastExecutableSpreadPct": (previous or {}).get("lastExecutableSpreadPct"),
                "lastDirectDurationMs": (previous or {}).get("lastDirectDurationMs"),
                "lastDirectCheckStartedMonotonic": (previous or {}).get("lastDirectCheckStartedMonotonic"),
                "lastReason": (previous or {}).get("lastReason"),
                "checks": int((previous or {}).get("checks") or 0),
                "firstDirectCheckStartedAtMs": (previous or {}).get("firstDirectCheckStartedAtMs"),
                "lastDirectCheckStartedAtMs": (previous or {}).get("lastDirectCheckStartedAtMs"),
                "lastSelectedAtMs": (previous or {}).get("lastSelectedAtMs"),
                "inFlight": (previous or {}).get("inFlight", False),
                "listingProbe": listing_probe,
                "marketBooksVerified": bool((previous or {}).get("marketBooksVerified")),
                "pulseObserved": not announcement_hint or bool((previous or {}).get("pulseObserved")),
                "firstDirectCheckAtMs": (previous or {}).get("firstDirectCheckAtMs"),
                "lastDirectCheckAtMs": (previous or {}).get("lastDirectCheckAtMs"),
            }
            if previous:
                refreshed += 1
            else:
                added += 1
        current = list(_hot_routes.values())
    _hot_wake.set()
    return {
        "routeCount": len(current),
        "addedCount": added,
        "refreshedCount": refreshed,
        "aboveThresholdRouteCount": sum("pulse_above_threshold" in item["reasons"] for item in current),
        "newListingRouteCount": sum("new_listing" in item["reasons"] for item in current),
        "listingSymbolCount": len(listing_symbols),
    }


def _prune_deduplicated_hot_routes() -> int:
    """Drop existing or queued routes before direct depth requests."""

    with _hot_lock:
        blocked = [
            identity
            for identity in _hot_routes
            if astro_route_dedupe_state(identity) is not None
        ]
        for identity in blocked:
            _hot_routes.pop(identity, None)
    return len(blocked)


def _hot_pre_api_filter(pair: dict[str, Any]) -> dict[str, Any] | None:
    """Recheck cheap live gates after scheduling, before spending any API slot.

    Do not reapply Pulse's spread to a hot route: a past threshold hit or new
    listing must keep continuous executable monitoring through price dips.
    """
    allowed, block_report = astro_spread_pair_submit_guard(pair)
    if not allowed:
        return block_report
    dedupe = astro_route_dedupe_state(pair)
    if dedupe is not None:
        return {"reason": "route_already_tracked", "dedupeState": dedupe}
    if pair.get("_listingDiscoveryOnly") and not any(
        _active_listing_leg_window({"listingVolumeWindows": pair.get("_listingVolumeWindows")}, leg)
        for leg in ("buy", "sell")
    ):
        return {"reason": "listing_discovery_window_expired"}
    # Built candidates carry liquidity evidence. Preserve the compatibility
    # path for callers without it; final validation remains authoritative.
    if "_buyVolume24hUsdt" in pair or "_sellVolume24hUsdt" in pair:
        eligible, reason = _auto_card_volume_check({
            "buyVolume24hUsdt": pair.get("_buyVolume24hUsdt"),
            "sellVolume24hUsdt": pair.get("_sellVolume24hUsdt"),
            "listingVolumeWindows": pair.get("_listingVolumeWindows") or {},
        })
        if not eligible and not pair.get("_listingDiscoveryOnly"):
            return {"reason": reason}
    return None


@_astro_api_priority
def _fetch_sf_funding(url, params):
    from app.crypto import api_request_deadline
    from app.astro_io_metrics import record
    started = time.monotonic()
    sample = {"success": False}
    try:
        # A failed pooled connection must not poison future background reads.
        # Keep the existing two-worker bound and per-contract cache; release
        # the entire transport on every success AND exception.
        with api_request_deadline(timeout_seconds=6.0), httpx.Client(timeout=2.0) as client:
            response = _scanner_public_get(client, url, params=params)
            try:
                response.raise_for_status()
                body = response.json()
                sample["success"] = True
                return body
            finally:
                response.close()
    except Exception as exc:
        sample["errorType"] = type(exc).__name__
        raise
    finally:
        sample["durationMs"] = round((time.monotonic() - started) * 1000, 1)
        record("fundingHttp", sample)


def _hot_funding_precheck(item):
    from app.astro_sf_funding import precheck
    pair = item["pair"]
    if pair.get("type") != "SF":
        return {"eligible": True, "status": "not_applicable", "read": False}
    exchange = str(pair.get("sellEx") or "").lower()
    symbol = str(pair.get("name") or "").upper().removesuffix("USDT")
    aliases = _load_pulse_symbol_aliases()
    raw = _direct_market_symbol(exchange, "future", symbol, aliases)[0]
    pulse_spread = _finite((item.get("candidate") or {}).get("openSpreadPct"))
    return precheck("SF", pulse_spread, exchange, raw, pulse=(item.get("candidate") or {}).get("sellPulseFunding"))


def _prefetch_hot_funding(item):
    from app.astro_sf_funding import prefetch
    pair = item["pair"]
    spread = _finite((item.get("candidate") or {}).get("openSpreadPct"))
    if pair.get("type") != "SF" or spread is None or spread >= 2.5:
        return
    if spread < spread_scan_sf_route_min_open_pct(pair.get("buyEx")) - 0.15:
        return
    exchange = str(pair.get("sellEx") or "").lower()
    symbol = str(pair.get("name") or "").upper().removesuffix("USDT")
    raw = _direct_market_symbol(exchange, "future", symbol, _load_pulse_symbol_aliases())[0]
    prefetch(exchange, raw, _fetch_sf_funding)


def _hot_route_direct_check(item: dict[str, Any], config: Any) -> tuple[tuple[str, str, str, str, str, str], dict[str, Any] | None, dict[str, Any]]:
    started = time.monotonic()
    identity = tuple(item["identity"])
    with _hot_lock:
        current = _hot_routes.get(identity)
        if current is not None:
            started_ms = int(time.time() * 1000)
            current["firstDirectCheckStartedAtMs"] = current.get("firstDirectCheckStartedAtMs") or started_ms
            current["lastDirectCheckStartedAtMs"] = started_ms
            current["lastDirectCheckStartedMonotonic"] = started
            current["nextPollMonotonic"] = started + (
                max(5.0, spread_scan_interval_seconds()) if current.get("listingProbe")
                else spread_hot_monitor_interval_seconds()
            )
    if _stop.is_set():
        return identity, None, {"reason": "scanner_stopped"}
    block_report = _hot_pre_api_filter(dict(item["pair"]))
    if block_report is not None:
        with _state_lock:
            monitor = _state.setdefault("hotMonitor", {})
            counts = monitor.setdefault("preApiFilteredReasons", {})
            reason = block_report["reason"]
            counts[reason] = counts.get(reason, 0) + 1
            monitor["preApiFilteredCount"] = monitor.get("preApiFilteredCount", 0) + 1
        return identity, None, {
            **block_report,
            "preApiFiltered": True,
            "durationMs": round((time.monotonic() - started) * 1000, 1),
        }
    funding_precheck = _hot_funding_precheck(item)
    if not funding_precheck["eligible"]:
        with _state_lock:
            monitor = _state.setdefault("hotMonitor", {})
            monitor["fundingDepthSkippedCount"] = monitor.get("fundingDepthSkippedCount", 0) + 1
        return identity, None, {
            "reason": "sf_" + funding_precheck["status"],
            "fundingPrecheck": funding_precheck, "depthSkipped": True,
            "durationMs": round((time.monotonic() - started) * 1000, 1),
        }
    _prefetch_hot_funding(item)
    latest_pair, report = _fetch_direct_route_once(
        dict(item["pair"]),
        config,
        executable_depth_first=True,
    )
    report = {
        **report,
        "fundingPrecheck": funding_precheck,
        "durationMs": round((time.monotonic() - started) * 1000, 1),
    }
    # A newly listed symbol without a Pulse threshold hit is watched
    # continuously, but its routine misses belong in hot-monitor metrics rather
    # than one warning per route. Threshold-triggered misses remain traceable.
    if latest_pair is None and "pulse_above_threshold" in set(item.get("reasons") or []):
        _append_decision_audit(
            identity,
            stage="hot_direct_check",
            decision=str(report.get("reason") or "unknown"),
            details={
                "pulseOpenSpreadPct": _finite(item.get("candidate", {}).get("openSpreadPct")),
                "watchReasons": list(item.get("reasons") or []),
                "report": report,
            },
        )
    return identity, latest_pair, report


def _hot_route_wait_snapshot() -> dict[str, Any]:
    with _hot_lock:
        current_routes = [dict(route) for route in _hot_routes.values()]
    now_ms = int(time.time() * 1000)
    route_waits = []
    for route in current_routes:
        identity = tuple(route.get("identity") or ())
        if len(identity) < 4:
            continue
        last_started = int(route.get("lastDirectCheckStartedAtMs") or 0)
        registered = int(route.get("registeredAtMs") or now_ms)
        route_waits.append({
            "symbol": identity[0], "type": identity[1],
            "buyExchange": identity[2], "sellExchange": identity[3],
            "registeredAtMs": registered,
            "firstDirectCheckStartedAtMs": route.get("firstDirectCheckStartedAtMs"),
            "lastDirectCheckStartedAtMs": last_started or None,
            "lastSelectedAtMs": route.get("lastSelectedAtMs"),
            "inFlight": bool(route.get("inFlight")),
            "listingProbe": bool(route.get("listingProbe")),
            "lastDirectCheckAtMs": route.get("lastDirectCheckAtMs"),
            "checks": int(route.get("checks") or 0),
            "lastReason": route.get("lastReason"),
            "fundingWaiting": float(route.get("fundingWaitUntilMonotonic") or 0) > time.monotonic(),
            "pollIntervalMs": route.get("pollIntervalMs") or round(spread_hot_monitor_interval_seconds() * 1000),
            "priceBackoffCount": int(route.get("priceBackoffCount") or 0),
            "lastDirectDurationMs": route.get("lastDirectDurationMs"),
            "lastExecutableSpreadPct": route.get("lastExecutableSpreadPct"),
            "waitSinceLastStartMs": max(0, now_ms - (last_started or registered)),
        })
    route_waits.sort(key=lambda item: item["waitSinceLastStartMs"], reverse=True)
    return {
        "routeWaits": route_waits[:50],
        "routeWaitSnapshotAtMs": now_ms,
        "maxRouteWaitSinceLastStartMs": max((item["waitSinceLastStartMs"] for item in route_waits), default=0),
        "inFlightRouteCount": sum(bool(route.get("inFlight")) for route in current_routes),
        "routeCount": len(current_routes),
        "aboveThresholdRouteCount": sum("pulse_above_threshold" in item.get("reasons", []) for item in current_routes),
        "newListingRouteCount": sum("new_listing" in item.get("reasons", []) for item in current_routes),
        "listingProbeRouteCount": sum(bool(item.get("listingProbe")) for item in current_routes),
        "listingProbeMaxConcurrent": 1,
        "listingProbeIntervalSeconds": max(5.0, spread_scan_interval_seconds()),
        "priceBackoffRouteCount": sum(int(item.get("priceBackoffCount") or 0) >= 2 for item in current_routes),
        "adaptivePollRule": "首次及接近门槛优先；连续远离门槛逐步降为2/4/5秒，价差改善恢复快速复查；间隔包含请求时间",
    }


def _record_hot_monitor_state(**updates: Any) -> None:
    route_snapshot = _hot_route_wait_snapshot()
    with _state_lock:
        current = dict(_state.get("hotMonitor") or {})
        current.update(
            {
                "running": bool(_hot_thread and _hot_thread.is_alive()),
                "intervalMs": round(spread_hot_monitor_interval_seconds() * 1000),
                "workers": spread_hot_monitor_workers(),
                "maxRoutesPerCycle": spread_hot_monitor_max_routes_per_cycle(),
                "apiRequestControl": _depth_request_control_status(),
                **route_snapshot,
                **updates,
            }
        )
        _state["hotMonitor"] = current


def _hot_report_has_fresh_books(report: dict[str, Any]) -> bool:
    if not isinstance(report.get("cexExecutablePreflight"), dict):
        return False
    skew = _finite(report.get("quoteSkewSeconds"))
    ages = [_finite((report.get(f"{leg}Quote") or {}).get("quoteAgeSeconds")) for leg in ("buy", "sell")]
    return (
        skew is not None and 0 <= skew <= spread_final_revalidation_max_skew_seconds()
        and all(age is not None and 0 <= age <= spread_final_revalidation_max_quote_age_seconds() for age in ages)
    )


def _process_hot_direct_result(identity, latest_pair, report, config) -> None:
    if _stop.is_set():
        return
    with _hot_lock:
        current = _hot_routes.get(identity)
        if current is not None:
            current["checks"] = int(current.get("checks") or 0) + 1
            current["lastReason"] = str(report.get("reason") or "unknown")
            _update_hot_price_cadence(current, report)
            current["firstDirectCheckAtMs"] = int(
                current.get("firstDirectCheckAtMs") or int(time.time() * 1000)
            )
            current["lastDirectCheckAtMs"] = int(time.time() * 1000)
            if current.get("listingProbe") and _hot_report_has_fresh_books(report):
                current["marketBooksVerified"] = True
                current["listingProbe"] = False
                current["pair"]["_listingDiscoveryOnly"] = False
                current["nextPollMonotonic"] = time.monotonic() + spread_hot_monitor_interval_seconds()
    if latest_pair is None or current is None or _stop.is_set():
        return

    _reset_api_degraded_route(identity)

    hit_at_ms = int(time.time() * 1000)
    pipeline = dict(latest_pair.get("_pipeline") or {})
    with _hot_lock:
        source = dict(_hot_routes.get(identity) or {})
    source_pair = source.get("pair") if isinstance(source.get("pair"), dict) else {}
    pipeline.update(dict(source_pair.get("_pipeline") or {}))
    pipeline.update(
        {
            "confirmedAtMs": hit_at_ms,
            "firstDirectCheckStartedAtMs": source.get("firstDirectCheckStartedAtMs"),
            "lastDirectCheckStartedAtMs": source.get("lastDirectCheckStartedAtMs"),
            "directCheckCount": source.get("checks"),
            "hotDirectHitAtMs": hit_at_ms,
            "hotDirectOpenSpreadPct": _finite(report.get("latestOpenSpreadPct")),
            "hotWatchReasons": list(source.get("reasons") or []),
            "jitRevalidationMode": "two_independent_executable_depth_checks",
        }
    )
    latest_pair["_pipeline"] = pipeline
    latest_pair["_hotDirectHit"] = {
        "verifiedAtMs": hit_at_ms,
        "report": report,
    }
    sync_status = schedule_astro_pairs(
        [latest_pair],
        config,
        revalidator=revalidate_astro_hot_direct_hit,
        submit_guard=astro_spread_pair_submit_guard,
        priority=True,
    )
    append_system_runtime_event(
        "astro_hot_route_direct_hit",
        level="info",
        source="backend",
        module="astro_spread_scanner",
        message=(
            f"热点路线出现同步直连机会，已优先提交建卡："
            f"{identity[0]} {identity[2]}/{identity[3]}"
        ),
        details={
            "symbol": identity[0],
            "type": identity[1],
            "buyExchange": identity[2],
            "sellExchange": identity[3],
            "reasons": list(source.get("reasons") or []),
            "directOpenSpreadPct": _finite(report.get("latestOpenSpreadPct")),
            "quoteSkewSeconds": _finite(report.get("quoteSkewSeconds")),
            "buyQuote": report.get("buyQuote"),
            "sellQuote": report.get("sellQuote"),
            "syncState": sync_status.get("state"),
        },
    )
    with _hot_lock:
        _hot_routes.pop(identity, None)
        _hot_recent_hits[identity] = time.monotonic()
        _hot_recent_hit_at_ms[identity] = hit_at_ms
    _record_hot_monitor_state(lastHitAt=datetime.now(timezone.utc).isoformat(), lastError=None)

def _hot_monitor_loop() -> None:
    """Keep a bounded worker pool busy without a slow-route batch barrier."""
    workers = spread_hot_monitor_workers()
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="astro-hot-depth")
    pending = {}
    pending_probes = set()
    _record_hot_monitor_state(running=True, lastError=None)
    try:
        while not _stop.is_set():
            # Process completed evidence immediately. No late result can submit
            # after stop, and no route is submitted twice while its worker runs.
            for future in [future for future in pending if future.done()]:
                identity = pending.pop(future)
                pending_probes.discard(future)
                with _hot_lock:
                    if identity in _hot_routes:
                        _hot_routes[identity]["inFlight"] = False
                try:
                    result_identity, latest_pair, report = future.result()
                    _process_hot_direct_result(result_identity, latest_pair, report, astro_sdk_config())
                except Exception as exc:
                    _record_hot_monitor_state(lastError=str(exc))
                _record_hot_monitor_state(lastCheckAt=datetime.now(timezone.utc).isoformat())
            if _stop.is_set():
                break
            deduplicated = _prune_deduplicated_hot_routes()
            now = time.monotonic()
            selected_at_ms = int(time.time() * 1000)
            with _hot_lock:
                for identity in list(_hot_routes):
                    if now > float(_hot_routes[identity].get("expiresAtMonotonic") or 0.0) and identity not in pending.values():
                        _hot_routes.pop(identity, None)
                all_due = [dict(item) for identity, item in _hot_routes.items()
                           if identity not in pending.values()
                           and now >= float(item.get("nextPollMonotonic") or 0.0)]
                counts = {}
                for identity in pending.values():
                    counts[identity[0]] = counts.get(identity[0], 0) + 1
                free_slots = min(workers - len(pending), spread_hot_monitor_max_routes_per_cycle())
                due = _select_hot_routes_for_cycle(
                    all_due, limit=free_slots,
                    per_symbol_limit=spread_hot_monitor_max_routes_per_symbol_cycle(),
                    symbol_inflight_counts=counts, now_ms=selected_at_ms,
                    listing_probe_inflight=len(pending_probes),
                ) if free_slots > 0 else []
                for item in due:
                    route = _hot_routes[tuple(item["identity"])]
                    route["lastSelectedAtMs"] = selected_at_ms
                    route["inFlight"] = True
            config = astro_sdk_config()
            for item in due:
                future = executor.submit(_call_with_astro_api_priority, _hot_route_direct_check, item, config)
                pending[future] = tuple(item["identity"])
                if item.get("listingProbe"):
                    pending_probes.add(future)
            if due:
                _record_hot_monitor_state(
                    lastBatchRouteCount=len(due),
                    lastBatchSymbolCount=len({item["identity"][0] for item in due}),
                    lastBatchUnreviewedRouteCount=sum(int(item.get("checks") or 0) <= 0 for item in due),
                    oldestSelectedUnreviewedWaitMs=max((selected_at_ms-int(item.get("registeredAtMs") or selected_at_ms)
                        for item in due if int(item.get("checks") or 0) <= 0), default=0),
                    schedulerPolicy="固定有界worker空槽即补；每路线仅一次在途；跨类型累计每币额度；等待5秒提升优先级",
                )
            elif deduplicated:
                _record_hot_monitor_state(lastDeduplicatedPrunedCount=deduplicated)
            if pending:
                wait(tuple(pending), timeout=min(0.1, spread_hot_monitor_interval_seconds()), return_when=FIRST_COMPLETED)
            else:
                _hot_wake.wait(min(0.1, spread_hot_monitor_interval_seconds()))
                _hot_wake.clear()
    finally:
        for future, identity in pending.items():
            future.cancel()
            with _hot_lock:
                if identity in _hot_routes:
                    _hot_routes[identity]["inFlight"] = False
        executor.shutdown(wait=False, cancel_futures=True)
        _record_hot_monitor_state(running=False)


def astro_listing_linkage_status(symbols: set[str] | list[str]) -> dict[str, dict[str, Any]]:
    """Return the current listing-to-Astro handoff state for announcement UI.

    This is deliberately read-only.  It reports hot-watch registration,
    direct-API checks, recent card-sync submissions and locally registered
    auto-created cards without changing scanner decisions.
    """

    normalized = {
        str(symbol or "").strip().upper().removesuffix("USDT")
        for symbol in symbols
        if str(symbol or "").strip()
    }
    result: dict[str, dict[str, Any]] = {
        symbol: {
            "status": "waiting_market",
            "reason": "尚未形成可用的直连监控路线。",
            "registeredAt": None,
            "firstDirectCheckAt": None,
            "lastDirectCheckAt": None,
            "routeCount": 0,
            "aboveThresholdRouteCount": 0,
        }
        for symbol in normalized
    }

    def iso_from_ms(value: Any) -> str | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat()

    with _hot_lock:
        routes = [dict(route) for route in _hot_routes.values()]
        recent_hits = dict(_hot_recent_hit_at_ms)

    for route in routes:
        identity = tuple(route.get("identity") or ())
        if not identity:
            continue
        symbol = str(identity[0]).strip().upper().removesuffix("USDT")
        if symbol not in result:
            continue
        row = result[symbol]
        row["routeCount"] += 1
        if "pulse_above_threshold" in list(route.get("reasons") or []):
            row["aboveThresholdRouteCount"] += 1
        registered_at = iso_from_ms(route.get("registeredAtMs") or route.get("firstSeenAtMs"))
        first_direct = iso_from_ms(route.get("firstDirectCheckAtMs"))
        last_direct = iso_from_ms(route.get("lastDirectCheckAtMs"))
        if registered_at and (not row["registeredAt"] or registered_at < row["registeredAt"]):
            row["registeredAt"] = registered_at
        if first_direct and (not row["firstDirectCheckAt"] or first_direct < row["firstDirectCheckAt"]):
            row["firstDirectCheckAt"] = first_direct
        if last_direct and (not row["lastDirectCheckAt"] or last_direct > row["lastDirectCheckAt"]):
            row["lastDirectCheckAt"] = last_direct
        checks = int(route.get("checks") or 0)
        if checks:
            row["status"] = "direct_rejected"
            row["reason"] = str(route.get("lastReason") or "直连复核暂未达标。")
        elif row["status"] == "waiting_market":
            row["status"] = "registered"
            row["reason"] = "已加入热点路线连续API监控。"

    for identity, hit_at_ms in recent_hits.items():
        symbol = str(identity[0]).strip().upper().removesuffix("USDT")
        if symbol not in result:
            continue
        row = result[symbol]
        row["status"] = "submitted_for_card_sync"
        row["reason"] = "直连可执行价差已达标，已提交Astro建卡同步。"
        row["lastDirectCheckAt"] = iso_from_ms(hit_at_ms)

    for record in auto_created_route_records():
        symbol = str(record.get("name") or "").strip().upper().removesuffix("USDT")
        if symbol not in result:
            continue
        result[symbol]["status"] = "card_created"
        result[symbol]["reason"] = "Astro自动建卡记录已存在。"
        result[symbol]["cardCreatedAt"] = record.get("createdAt")

    return result


def _select_okxdex_targets(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep up to two verified OKXDEX targets per exact asset identity.

    One route represents the largest executable spread and one represents the
    highest 24-hour futures volume.  When both rules select the same route it
    is kept only once.
    """

    dex_routes = [
        candidate
        for candidate in candidates
        if str(candidate.get("type") or "").upper() == "SF"
        and str(candidate.get("buyExchange") or "").lower() in SF_DEX_EXCHANGES
    ]
    eligible_dex = [candidate for candidate in dex_routes if _meets_auto_card_rule(candidate)]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for candidate in eligible_dex:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        chain_index, contract_address = _dex_lifecycle_parts(candidate.get("dexConfig"))
        grouped.setdefault((str(candidate.get("buyExchange")), symbol, chain_index, contract_address), []).append(candidate)

    selected_ids: set[int] = set()
    selected: list[dict[str, Any]] = []
    missing_volume_count = sum(
        1
        for candidate in dex_routes
        if _finite(candidate.get("buyVolume24hUsdt")) is None
        or _finite(candidate.get("sellVolume24hUsdt")) is None
    )
    for (_dex_exchange, symbol, chain_index, contract_address), group in grouped.items():
        comparable = [candidate for candidate in group if _finite(candidate.get("sellVolume24hUsdt")) is not None]
        if not comparable:
            continue
        highest_volume = max(
            comparable,
            key=lambda candidate: (
                float(_finite(candidate.get("sellVolume24hUsdt")) or 0.0),
                float(_finite(candidate.get("openSpreadPct")) or 0.0),
                str(candidate.get("sellExchange") or ""),
            ),
        )
        largest_spread = max(
            comparable,
            key=lambda candidate: (
                float(_finite(candidate.get("openSpreadPct")) or 0.0),
                float(_finite(candidate.get("sellVolume24hUsdt")) or 0.0),
                str(candidate.get("sellExchange") or ""),
            ),
        )
        comparison = sorted(
            [
                {
                    "exchange": str(candidate.get("sellExchange") or ""),
                    "volume24hUsdt": _finite(candidate.get("sellVolume24hUsdt")),
                    "openSpreadPct": _finite(candidate.get("openSpreadPct")),
                }
                for candidate in comparable
            ],
            key=lambda item: (
                -float(item["openSpreadPct"] or 0.0),
                -float(item["volume24hUsdt"] or 0.0),
                item["exchange"],
            ),
        )
        choices: dict[int, tuple[dict[str, Any], list[str]]] = {}
        for selection_rule, chosen in (
            ("max_executable_spread", largest_spread),
            ("highest_24h_futures_volume", highest_volume),
        ):
            candidate_id = id(chosen)
            if candidate_id not in choices:
                choices[candidate_id] = (chosen, [])
            choices[candidate_id][1].append(selection_rule)
        for candidate_id, (chosen, selection_rules) in choices.items():
            chosen["targetSelection"] = {
                "rule": "max_spread_and_highest_volume",
                "selectionReasons": selection_rules,
                "symbol": symbol,
                "chainIndex": chain_index,
                "contractAddress": contract_address,
                "targetExchange": chosen.get("sellExchange"),
                "openSpreadPct": _finite(chosen.get("openSpreadPct")),
                "volume24hUsdt": _finite(chosen.get("sellVolume24hUsdt")),
                "comparedTargets": comparison,
            }
            selected_ids.add(candidate_id)
            selected.append(chosen["targetSelection"])

    filtered = [
        candidate
        for candidate in candidates
        if candidate not in dex_routes or id(candidate) in selected_ids
    ]
    return filtered, {
        "rule": "max_spread_and_highest_volume",
        "selectedCount": len(selected),
        "missingVolumeBlockedCount": missing_volume_count,
        "selected": selected[:50],
    }


DELISTING_EXCHANGE_TO_PULSE = {
    "bn": "binance",
    "bg": "bitget",
    "by": "bybit",
    "gt": "gate",
    "gate": "gate",
    "okx": "okx",
    "as": "aster",
    "aster": "aster",
}


def _indexed_delisting_match(blocks, symbol, pair_type, buy, sell):
    from app.astro_news_policy import legs
    legacy = next((ex for ex in (buy, sell) if (symbol, ex) in blocks), None)
    return legacy or next((ex for ex, mk in legs(pair_type, buy, sell) if (symbol, ex, mk) in blocks), None)


def _active_delisting_exchange_blocks() -> set[tuple[str, str, str]]:
    from app.database import SessionLocal
    from app.models import ExchangeDelistingOpportunityWatch

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(ExchangeDelistingOpportunityWatch).where(
                    ExchangeDelistingOpportunityWatch.enabled.is_(True),
                    ExchangeDelistingOpportunityWatch.expires_at > now,
                )
            )
        )
    blocked: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.announcement_key or "").endswith(":listing"):
            continue
        symbol = str(row.symbol or "").strip().upper().removesuffix("USDT")
        exchange = DELISTING_EXCHANGE_TO_PULSE.get(str(row.source_exchange or "").strip().lower())
        from app.astro_news_policy import market
        market_type = market(row.source_market_type)
        if symbol and exchange and market_type:
            blocked.add((symbol, exchange, market_type))
    return blocked


def _filter_delisted_exchange_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.astro_news_policy import route_check as news_route_check
    news_filtered = []
    remaining = []
    for item in candidates:
        restriction = news_route_check(item.get("symbol"), item.get("type"), item.get("buyExchange"), item.get("sellExchange"))
        if restriction:
            news_filtered.append({"symbol": item.get("symbol"), "exchange": restriction.get("exchange", "unknown")})
            _record_contract_restriction(item, restriction, evaluation_source="discovery")
        else:
            remaining.append(item)
    candidates = remaining
    if not spread_scan_exclude_delisted_exchange_cards():
        return candidates, {
            "enabled": False,
            "activeBlockCount": 0,
            "filteredCandidateCount": len(news_filtered),
            "items": news_filtered,
            "lastError": None,
        }
    try:
        blocks = _active_delisting_exchange_blocks()
    except Exception as exc:
        # This is an opening-safety rule: when the announcement index cannot
        # be read, skip auto-card candidates instead of risking a forbidden card.
        return [], {
            "enabled": True,
            "activeBlockCount": 0,
            "filteredCandidateCount": len(candidates),
            "items": [],
            "lastError": f"下架公告索引读取失败：{exc}",
        }
    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, str]] = list(news_filtered)
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").strip().upper().removesuffix("USDT")
        buy_exchange = str(candidate.get("buyExchange") or "").strip().lower()
        sell_exchange = str(candidate.get("sellExchange") or "").strip().lower()
        matched_exchange = _indexed_delisting_match(blocks, symbol, candidate.get("type"), buy_exchange, sell_exchange)
        if matched_exchange:
            filtered.append({"symbol": symbol, "exchange": matched_exchange})
            if candidate.get("type") in {"SF", "FF"}:
                _record_contract_restriction(candidate, {"decision": "reject", "reason": "contract_delisting_announced",
                                                        "exchange": matched_exchange, "source": "local_announcement_index"},
                                             evaluation_source="discovery")
            continue
        if candidate.get("type") in {"SF", "FF"}:
            from app.astro_contract_safety import route_check
            restriction = route_check(symbol, candidate["type"], buy_exchange, sell_exchange)
            if restriction and restriction["decision"] == "reject":
                filtered.append({"symbol": symbol, "exchange": restriction["exchange"]})
                _record_contract_restriction(candidate, restriction, evaluation_source="discovery")
                continue
        kept.append(candidate)
    return kept, {
        "enabled": True,
        "activeBlockCount": len(blocks),
        "filteredCandidateCount": len(filtered),
        "items": list({(item["symbol"], item["exchange"]): item for item in filtered}.values())[:50],
        "lastError": None,
    }


def _register_partial_pulse_candidates(payloads: list[dict[str, Any]]) -> None:
    """Discovery-only fast path; full scans retain history and cleanup duties."""
    started = time.monotonic()
    try:
        config = astro_sdk_config()
        if not config.enabled or not config.configured:
            return
        candidates, _ = scan_pulse_spreads(payloads)
        candidates = [item for item in candidates if item.get("buyExchange") not in SF_DEX_EXCHANGES
                      and _meets_auto_card_rule(item)]
        candidates, _ = _filter_delisted_exchange_candidates(candidates)
        registration = _register_hot_candidates(candidates, set(), config)
        result = {"candidateCount": len(candidates), "addedCount": registration["addedCount"],
                  "lastAt": datetime.now(timezone.utc).isoformat(), "lastError": None}
    except Exception as exc:
        result = {"lastError": str(exc)[:250]}
    result["durationMs"] = round((time.monotonic() - started) * 1000, 1)
    with _state_lock:
        _state["partialDiscovery"] = result


@_astro_api_priority
def run_astro_spread_scan_once() -> dict[str, Any]:
    started = time.monotonic()
    try:
        payloads, pulse_source_summary = _fetch_pulse_payloads(on_partial=_register_partial_pulse_candidates)
        from app.astro_news_policy import update_trading_markets
        update_trading_markets(payloads)
        _log_pulse_source_status(pulse_source_summary)
        pulse_health = _record_pulse_success(pulse_source_summary)
        retained_routes = _retained_auto_card_route_identities()
        listing_symbols, listing_error = _active_listing_symbols()
        listing_route_hints, listing_route_error = _active_listing_route_hints()
        listing_market_windows, listing_volume_error = _active_listing_volume_windows()
        for hint in listing_route_hints:
            windows = dict(hint.get("listingVolumeWindows") or {})
            for leg in ("buy", "sell"):
                event_ms = listing_market_windows.get((hint["symbol"], hint[f"{leg}Exchange"], hint[f"{leg}Market"]))
                if event_ms is not None:
                    windows[leg] = event_ms
            hint["listingVolumeWindows"] = windows
        candidates, market_count = scan_pulse_spreads(
            payloads,
            retained_routes=retained_routes,
            priority_symbols=listing_symbols,
            listing_market_windows=listing_market_windows,
        )
        evaluated_route_count = len(candidates)
        candidates, delisting_rule_summary = _filter_delisted_exchange_candidates(candidates)
        candidates, dex_identity_summary = _verify_okxdex_candidate_identities(candidates)
        _log_okxdex_identity_summary(dex_identity_summary)
        candidates, dex_mapping_summary = _annotate_okxdex_manual_mappings(candidates)
        with _state_lock:
            previous_missing_items = list(_state.get("dexMappingMissingItems") or [])
        pending_missing_items = _merge_pending_dex_mapping_items(
            previous_missing_items,
            dex_mapping_summary["missingItems"],
        )
        candidates = _annotate_ff_structure(candidates)
        route_observations = _auto_card_route_observations(candidates)
        confirmation_candidates, dex_target_selection = _select_okxdex_targets(candidates)
        # A single Pulse observation is enough to register a route for direct
        # monitoring.  Pulse itself still cannot authorize card creation.
        confirmed = [item for item in confirmation_candidates if _meets_auto_card_rule(item)]
        decision_audit = _record_candidate_decision_audit(
            candidates,
            confirmation_candidates,
            confirmed,
        )
        config = astro_sdk_config()
        hot_registration = _register_hot_candidates(
            [*listing_route_hints, *confirmation_candidates],
            listing_symbols,
            config,
        )
        # Creation is owned by the continuous direct-API hot monitor.  The
        # ordinary scan still feeds lifecycle observations to the low-priority
        # cleanup worker, but no longer starts a competing two-round create.
        sync_status = schedule_astro_pairs(
            [],
            config,
            submit_guard=astro_spread_pair_submit_guard,
            route_observations=route_observations,
            cleanup_revalidator=revalidate_astro_cleanup,
        )
        top = candidates[0] if candidates else None
        safe_top = (
            {
                key: top[key]
                for key in ("type", "symbol", "buyExchange", "sellExchange", "openSpreadPct", "closeSpreadPct", "sellVolume24hUsdt", "quoteAt", "structureAssessment", "dexIdentity", "targetSelection")
                if key in top
            }
            if top
            else None
        )
        result = {
            "running": True,
            "lastScanAt": datetime.now(timezone.utc).isoformat(),
            "lastScanDurationMs": round((time.monotonic() - started) * 1000, 1),
            "lastError": None,
            "marketCount": market_count,
            "evaluatedRouteCount": evaluated_route_count,
            "retainedRouteCount": len(retained_routes),
            "candidateCount": len([item for item in candidates if _meets_auto_card_rule(item)]),
            "confirmedCount": len(confirmed),
            "structureBlockedCount": len(
                [
                    item
                    for item in candidates
                    if isinstance(item.get("structureAssessment"), dict)
                    and item["structureAssessment"].get("classification") == "structural"
                ]
            ),
            "structureWarmingCount": len(
                [
                    item
                    for item in candidates
                    if isinstance(item.get("structureAssessment"), dict)
                    and item["structureAssessment"].get("historyReady") is False
                ]
            ),
            "dexIdentityVerifiedCount": dex_identity_summary["verifiedCount"],
            "dexIdentityBlockedCount": dex_identity_summary["blockedCount"],
            "dexIdentityStatusCounts": dex_identity_summary["statusCounts"],
            "dexIdentityLastError": dex_identity_summary["lastError"],
            "dexMappingConfirmedCount": dex_mapping_summary["confirmedCount"],
            "dexMappingMissingCount": len(pending_missing_items),
            "dexMappingMissingItems": pending_missing_items,
            "pulseSourceSuccessCount": pulse_source_summary["successCount"],
            "pulseSourceFailureCount": pulse_source_summary["failureCount"],
            "pulseSourceFailures": pulse_source_summary["failures"],
            "pulsePrimaryFailure": pulse_source_summary.get("primaryFailure"),
            "pulseCloudRttMs": pulse_source_summary.get("cloudRttMs"),
            **pulse_health,
            "delistingRule": delisting_rule_summary,
            "dexTargetSelection": dex_target_selection,
            "listingMonitor": {
                "activeSymbols": sorted(listing_symbols),
                "activeSymbolCount": len(listing_symbols),
                "routeHintCount": len(listing_route_hints),
                "lastError": listing_error or listing_route_error or listing_volume_error,
                "volumeExemptMarketCount": len(listing_market_windows),
            },
            "hotRegistration": hot_registration,
            "decisionAudit": decision_audit,
            "topCandidate": safe_top,
            "cardSyncState": sync_status.get("state"),
        }
    except PulseUnavailableError as exc:
        pulse_health = _record_pulse_failure(exc)
        result = {
            "running": True,
            "lastScanAt": datetime.now(timezone.utc).isoformat(),
            "lastScanDurationMs": round((time.monotonic() - started) * 1000, 1),
            "lastError": str(exc),
            "candidateCount": 0,
            "confirmedCount": 0,
            "pulseSourceSuccessCount": 0,
            "pulseSourceFailureCount": len(PULSE_URLS),
            "pulseSourceFailures": [{"transport": "all", "error": str(exc)}],
            "pulsePrimaryFailure": {"transport": "mac_direct_no_proxy", "error": str(exc)},
            "pulseCloudRttMs": None,
            **pulse_health,
        }
    except Exception as exc:
        result = {
            "running": True,
            "lastScanAt": datetime.now(timezone.utc).isoformat(),
            "lastScanDurationMs": round((time.monotonic() - started) * 1000, 1),
            "lastError": str(exc),
        }
        append_system_runtime_event(
            "astro_spread_scan_failed",
            level="error",
            source="backend",
            module="astro_spread_scanner",
            message=str(exc),
        )
    with _state_lock:
        _state.update(result)
    return astro_spread_scanner_status()


def _scanner_loop() -> None:
    while not _stop.is_set():
        round_started = time.monotonic()
        run_astro_spread_scan_once()
        # Keep scans on a start-to-start cadence. Network/compute time is part
        # of the configured round interval instead of being added on top of it.
        remaining = spread_scan_interval_seconds() - (time.monotonic() - round_started)
        _stop.wait(max(0.0, remaining))
    with _state_lock:
        _state["running"] = False


def start_astro_spread_scanner() -> None:
    global _thread, _hot_thread, _api_recovery_thread, _cloud_route_queue
    from app.astro_sdk import start_submission_confirmation_monitor
    start_submission_confirmation_monitor()
    from app.astro_news_policy import start as start_news_policy
    start_news_policy()
    if not spread_scan_enabled() or (_thread and _thread.is_alive()):
        return
    auto_card = astro_sdk_config()
    append_system_runtime_event(
        "astro_auto_card_monitor_started",
        level="info",
        source="backend",
        module="astro_spread_scanner",
        message=(
            "Astro 自动建卡监控已启动；满足规则的新卡默认暂停。"
            if auto_card.enabled
            else "Astro 行情扫描已启动；自动建卡总开关关闭。"
        ),
        details={
            "autoCardEnabled": auto_card.enabled,
            "configured": auto_card.configured,
            "dryRun": auto_card.dry_run,
            "scanIntervalSeconds": spread_scan_interval_seconds(),
            "maxCardsPerScan": auto_card.max_cards_per_scan,
            "unlimitedCardsPerScan": auto_card.max_cards_per_scan == 0,
            "defaultLeverage": auto_card.leverage,
            "defaultMinNotionalUsdt": astro_min_notional_usdt(),
            "defaultMaxNotionalUsdt": astro_max_notional_usdt(auto_card),
            "hotMonitorIntervalMs": round(spread_hot_monitor_interval_seconds() * 1000),
            "hotMonitorWorkers": spread_hot_monitor_workers(),
            "hotMonitorMaxRoutesPerCycle": spread_hot_monitor_max_routes_per_cycle(),
            "hotTriggerRule": f"两次独立同步直连{spread_cex_quote_notional_usdt(auto_card):g} USDT同数量深度均达标后，建立默认暂停的新卡",
            "fundingReadEnabled": True,
        },
    )
    _stop.clear()
    from app.database import get_data_dir
    _route_policy.configure_storage(get_data_dir() / "system-runtime" / "route-alert-state.json")
    if _cloud_route_queue.stopped:
        _cloud_route_queue = LatestQueue()
    from app.astro_depth_transport import start as start_depth_tunnel
    start_depth_tunnel()
    _hot_wake.clear()
    _hot_thread = threading.Thread(target=_hot_monitor_loop, name="astro-hot-route-monitor", daemon=True)
    _api_recovery_thread = threading.Thread(
        target=_api_recovery_probe_loop,
        name="astro-api-recovery-probe",
        daemon=True,
    )
    _thread = threading.Thread(target=_scanner_loop, name="astro-spread-scanner", daemon=True)
    _hot_thread.start()
    _api_recovery_thread.start()
    _thread.start()


def stop_astro_spread_scanner() -> None:
    global _api_notification_executor, _local_depth_deadline_executor
    from app.astro_sdk import stop_submission_confirmation_monitor
    stop_submission_confirmation_monitor()
    from app.astro_news_policy import stop as stop_news_policy
    stop_news_policy()
    _stop.set()
    _hot_wake.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=2.0)
    if _hot_thread and _hot_thread.is_alive():
        _hot_thread.join(timeout=2.0)
    if _api_recovery_thread and _api_recovery_thread.is_alive():
        _api_recovery_thread.join(timeout=2.0)
    with _api_degraded_lock:
        executor, _api_notification_executor = _api_notification_executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    with _api_degraded_lock:
        local_executor, _local_depth_deadline_executor = _local_depth_deadline_executor, None
    if local_executor is not None:
        local_executor.shutdown(wait=False, cancel_futures=True)
    _cloud_route_queue.stop()
    from app.astro_depth_transport import stop as stop_depth_tunnel
    stop_depth_tunnel()


def astro_spread_scanner_status() -> dict[str, Any]:
    from app.astro_contract_safety import status as contract_safety_status
    from app.astro_news_policy import status as news_policy_status
    with _state_lock:
        current = dict(_state)
    selected, subscription_source = _configured_market_keys()
    selected_set = set(selected)
    mapped_assets = spread_scan_dex_mapped_assets()
    symbol_mapped_assets = [item for item in mapped_assets if item.get("autoCreateEligible") is True]
    legacy_mapped_assets = [item for item in mapped_assets if item.get("autoCreateEligible") is not True]
    return {
        "enabled": spread_scan_enabled(),
        "contractLifecycle": contract_safety_status(),
        "newsPolicy": news_policy_status(),
        "source": "Astro Pulse live local no-proxy direct only",
        "intervalSeconds": spread_scan_interval_seconds(),
        "minOpenSpreadPct": min(spread_scan_ff_min_open_pct(), spread_scan_sf_min_open_pct()),
        "maxOpenSpreadPct": spread_scan_max_open_pct(),
        "minVolumeUsdt": spread_scan_min_volume_usdt(),
        "newListingVolumeBypass": True,
        "blockedPairs": spread_scan_blocked_pairs(),
        "blockedCoins": spread_scan_blocked_coins(),
        "dexMappedAssets": mapped_assets,
        "deleteRearmPct": astro_delete_rearm_pct(),
        "deletePullbackPctPoints": astro_delete_pullback_pct_points(),
        "deleteRearm": astro_delete_rearm_status(),
        "confirmations": 1,
        "maxQuoteAgeSeconds": spread_scan_max_quote_age_seconds(),
        "greaterPriceAlertPct": astro_greater_price_alert_pct(),
        "priceChangeAlertPct": astro_price_change_alert_pct(),
        "priceChangeAlertOnlyRise": astro_price_change_alert_only_rise(),
        "minNotionalUsdt": astro_min_notional_usdt(),
        "maxNotionalUsdt": astro_max_notional_usdt(),
        "fundingRead": {"enabled": True, "scope": "sf_only", "precheckSource": "pulse_or_api_cache", "preciseReadStage": "candidate_prefetch_then_final_gate", "pulseNegativeMaxWaitSeconds": 10, "pulseMaxAgeSeconds": 15, "pulseNearZeroPct": 0.001, "precheckSpreadPct": 2.3, "retrySeconds": [1, 3, 10, 30], "backgroundOnly": True, "requestBudgetSeconds": 6, "rule": "Pulse 初筛后候选并行预取；盘口条件通过后核对精确资金费 ≥ 0，复用 10 秒缓存。实际价差 ≥ 2.5% 豁免；FF 不读取。", "cacheSeconds": 10, "exemptSpreadPct": 2.5},
        "finalRevalidation": {
            "enabled": True,
            "workers": spread_final_revalidation_workers(),
            "maxQuoteAgeSeconds": spread_final_revalidation_max_quote_age_seconds(),
            "okxDexMaxQuoteAgeSeconds": spread_final_revalidation_okxdex_max_quote_age_seconds(),
            "okxDexSubmitMaxQuoteAgeSeconds": spread_final_revalidation_okxdex_submit_max_quote_age_seconds(),
            "okxDexDistinctWaitSeconds": spread_final_revalidation_okxdex_distinct_wait_seconds(),
            "okxDexPollSeconds": spread_final_revalidation_okxdex_poll_seconds(),
            "maxQuoteSkewSeconds": spread_final_revalidation_max_skew_seconds(),
            "okxDexTimestampSkewCheck": False,
            "timeoutSeconds": spread_final_revalidation_timeout_seconds(),
            "rounds": spread_final_revalidation_rounds(),
            "cexRounds": spread_final_revalidation_rounds(),
            "okxDexRounds": 3,
            "dexConfirmationIntervalMs": 1000,
            "intervalMs": round(spread_final_revalidation_interval_seconds() * 1000),
            "cexQuoteNotionalUsdt": spread_cex_quote_notional_usdt(),
            "source": "CEX 双腿同数量深度 + OKXDEX / PancakeSwap V3 真实询价",
            "rule": (
                f"Pulse只发现候选；普通CEX必须连续{spread_final_revalidation_rounds()}次按"
                f"{spread_cex_quote_notional_usdt():g} USDT买入深度和同数量卖出深度均达标，"
                "热点路线不再额外请求ticker；"
                "链上候选建卡前连续3轮真实询价，每轮间隔1秒，不复用热点命中或相同时间戳；每轮按卡片实际金额取得对应DEX的可执行报价，"
                "用预计买入数量扫合约买盘深度；DEX询价已反映池费和价格冲击，"
                "滑点参数只是最大成交保护上限，不作为必然成本扣除；净价差额外扣网络费（PancakeSwap 为估算值）；"
                f"真实DEX询价不超过{spread_final_revalidation_okxdex_submit_max_quote_age_seconds():g}秒；"
                f"合约腿不超过{spread_final_revalidation_max_quote_age_seconds():g}秒；"
                f"普通CEX两腿时间差不超过{spread_final_revalidation_max_skew_seconds():g}秒，DEX不做硬时间戳匹配；"
                "SF 资金费须非负，实际价差至少 2.5% 时豁免；FF 不读资金费；必要盘口证据缺失时跳过；"
                "任何交易所都不能因API故障跳过深度复核"
            ),
        },
        "revalidationRetry": _revalidation_runtime_status(),
        "apiDegradedMode": _api_degraded_status(),
        "pulseSourceHealth": astro_pulse_health.snapshot(),
        "pulseSources": {
            "configuredCount": len(PULSE_URLS),
            "successCount": int(current.get("pulseSourceSuccessCount") or 0),
            "failureCount": int(current.get("pulseSourceFailureCount") or 0),
            "failures": list(current.get("pulseSourceFailures") or []),
            "primaryFailure": current.get("pulsePrimaryFailure"),
            "transport": current.get("pulseTransport"),
            "cloudRttMs": current.get("pulseCloudRttMs"),
            "fallbackActive": bool(current.get("pulseFallbackActive")),
            "healthy": bool(current.get("pulseHealthy", True)),
            "consecutiveFailureCount": int(current.get("pulseConsecutiveFailureCount") or 0),
            "failureThreshold": 3,
            "outageStartedAt": current.get("pulseOutageStartedAt"),
            "lastRecoveredAt": current.get("pulseLastRecoveredAt"),
            "cleanupPaused": bool(current.get("cleanupPaused")),
            "degraded": (
                bool(current.get("pulseFallbackActive"))
                or int(current.get("pulseSourceFailureCount") or 0) > 0
            ),
            "rule": (
                "仅使用本机无代理直连实时读取两路 Pulse，不使用云端转发或旧缓存。"
                "本机两路全部失败时本轮不建卡也不清卡，连续3轮失败标记不健康；"
                "Pulse只负责发现，热点路线由交易所直连API连续监控，"
                "普通CEX需两次独立同数量深度均达标才触发"
            ),
        },
        "pairTypes": ["SF", "FF", "FS"],
        "autoCardRules": {
            "ff": {
                "minOpenSpreadPctExclusive": spread_scan_ff_min_open_pct(),
                "exchanges": sorted(ASTRO_FF_DIRECT_EXCHANGES),
                "buyExchanges": sorted(ASTRO_FF_BUY_EXCHANGES),
                "sellExchanges": sorted(ASTRO_FF_SELL_EXCHANGES),
                "bybitSellException": {"enabled": astro_ff_bybit_sell_exception_enabled(), "minOpenSpreadPctExclusive": 10.0},
                "gcCompanion": "不创建任何 GC 卡",
                "structureFilter": {
                    "enabled": structure_filter_enabled(),
                    "historyHours": structure_history_hours(),
                    "sampleSeconds": structure_sample_seconds(),
                    "trackingMinSpreadPct": structure_tracking_min_pct(),
                    "adverseFundingShare": structure_adverse_share(),
                    "maxFiveMinuteIncreasePct": structure_max_five_minute_increase_pct(),
                    "missingHistoryRejected": structure_filter_enabled(),
                    "gateIndexEvidence": (
                        "Gate 官方指数成分与权重指纹；历史不足不建卡"
                        if structure_filter_enabled()
                        else "已关闭"
                    ),
                    "rule": (
                        "持续亏资金费 + 缓慢形成 + 位于历史常态区间 = 结构性价差，不建卡"
                        if structure_filter_enabled()
                        else "结构性历史过滤已关闭"
                    ),
                },
            },
            "sf": {
                "minOpenSpreadPctExclusive": spread_scan_sf_min_open_pct(),
                "spotExchanges": sorted(SF_AUTO_CARD_SPOT_EXCHANGES),
                "dexMinOpenSpreadPctExclusive": {ex: spread_scan_sf_route_min_open_pct(ex) for ex in sorted(SF_DEX_EXCHANGES)},
                "pancakeswapV3Enabled": spread_scan_sf_pancakeswap_auto_card_enabled(),
                "fundingRule": {"minimumRatePct": 0, "exemptSpreadPct": 2.5, "unknownAction": "wait"},
                "okxDexRoute": {
                    "buyExchange": "okxdex",
                    "autoCardEnabled": spread_scan_sf_okxdex_auto_card_enabled(),
                    "sellExchanges": sorted(SF_OKXDEX_FUTURES_EXCHANGES),
                    "identityVerificationEnabled": spread_okxdex_identity_verification_enabled(),
                    "dexConfigurationRequired": True,
                    "dexConfigurationApiReady": astro_sdk_config().dex_configured,
                    "mappingMode": "exchange_chain_contract",
                    "mappingRule": "按币名、链和合约地址确认，两家 DEX 共用；旧的无地址记录需补全",
                    "mappedAssets": mapped_assets,
                    "mappedAssetCount": len(symbol_mapped_assets),
                    "legacyDisplayOnlyCount": len(legacy_mapped_assets),
                    "legacyDisplayOnlyAssets": legacy_mapped_assets,
                    "chainNoteVisibility": "OKXDEX 自动标注链名；PancakeSwap V3 不额外标注",
                    "unmappedCandidateCount": current.get("dexMappingMissingCount", 0),
                    "unmappedItems": current.get("dexMappingMissingItems", []),
                    "slippagePct": spread_okxdex_slippage_pct(),
                    "executableQuoteRequired": True,
                    "quoteSource": "OKXDEX：官方聚合询价；PancakeSwap V3：链上只读 Quoter 询价（USDT 直连池）",
                    "quoteNotionalUsdt": astro_max_notional_usdt(astro_sdk_config()),
                    "networkFeeAmortizedAtUsdt": astro_max_notional_usdt(astro_sdk_config()),
                    "futuresDepthRule": "按DEX预计买入数量计算合约买盘加权成交均价",
                    "netSpreadRule": "DEX官方询价的实际输入/输出已含价格冲击和池费；滑点只是保护上限，净价差额外扣网络费（PancakeSwap 为估算值）",
                    "identityVerificationRequired": spread_okxdex_identity_verification_enabled(),
                    "identitySource": "各目标交易所官方资产网络表（链 ID + contractAddress）",
                    "identityRule": (
                        "链 ID 与合约地址同时一致才允许建卡；同名多链、缺失或不一致均拒绝"
                        if spread_okxdex_identity_verification_enabled()
                        else "目标合约交易所资产网络表验证已临时关闭"
                    ),
                    "targetSelectionRule": "每家 DEX 的同一链上资产最多选择两条通过全部规则的路线：可执行差价最大与 24 小时合约成交额最高；同一路线自动去重，成交额缺失不参与建卡",
                    "verifiedCandidateCount": current.get("dexIdentityVerifiedCount", 0),
                    "bypassedCandidateCount": int(
                        (current.get("dexIdentityStatusCounts") or {}).get("bypassed", 0)
                    ),
                    "blockedCandidateCount": current.get("dexIdentityBlockedCount", 0),
                    "statusCounts": current.get("dexIdentityStatusCounts", {}),
                    "lastError": current.get("dexIdentityLastError"),
                },
                "excludedFuturesExchanges": sorted(SF_AUTO_CARD_EXCLUDED_FUTURES),
                "missingFundingRejected": True,
                "fundingDecisionStage": "pulse_prefilter_then_api_after_executable_depth",
                "missingVolumeRejected": True,
                "minVolumeUsdtPerLeg": spread_scan_min_volume_usdt(),
                "gcCompanion": "不创建任何 GC 卡",
            },
            "fsBorrow": {
                "enabled": astro_fs_borrow_auto_card_enabled(),
                "futuresExchanges": "所有本地已接入且 Astro 支持的合约交易所",
                "spotExchange": "bitget",
                "spotMarginType": "cross",
                "leverage": 3,
                "inventoryRequired": True,
                "borrowableAmountRequired": True,
                "borrowableAmountRule": "Bitget 全仓杠杆实时可借额度必须大于0；额度缺失、查询失败或为0均不建卡",
                "borrowRateRequired": True,
                "minCycleProfitPctExclusive": astro_fs_borrow_min_cycle_profit_pct(),
                "minOpenSpreadPctExclusive": astro_fs_borrow_min_open_spread_pct(),
                "cycleProfitFormula": "-资金费率 - BG小时借币率 × 合约资金费周期小时数",
                "spreadFormula": "2 × (BG现货买一 - 合约卖一) ÷ (BG现货买一 + 合约卖一)",
            },
        },
        "exchanges": sorted(spread_scan_exchanges()),
        "subscriptions": selected,
        "subscriptionSource": subscription_source,
        "availableMarkets": [
            {
                "key": key,
                "exchange": exchange,
                "exchangeName": exchange_name,
                "marketType": market_type,
                "enabled": key in selected_set,
            }
            for key, exchange, exchange_name, market_type in PULSE_MARKETS
        ],
        "structureHistory": ff_structure_history_status(),
        **current,
        "hotMonitor": {
            "enabled": True,
            "intervalMs": round(spread_hot_monitor_interval_seconds() * 1000),
            "routeTtlSeconds": spread_hot_monitor_route_ttl_seconds(),
            "hitEvidenceTtlSeconds": spread_hot_monitor_hit_ttl_seconds(),
            "workers": spread_hot_monitor_workers(),
            "maxRoutesPerSymbolCycle": spread_hot_monitor_max_routes_per_symbol_cycle(),
            "triggerSources": ["pulse_above_threshold", "new_listing"],
            "confirmationIntervalMs": round(spread_hot_monitor_confirmation_interval_seconds() * 1000),
            "quoteNotionalUsdt": spread_cex_quote_notional_usdt(),
            "createRule": (
                f"热点CEX路线跳过ticker，须两次独立的{spread_cex_quote_notional_usdt():g} USDT"
                "同数量深度均达标；仅明确上市后2小时内的新市场豁免该腿成交额；"
                "DEX路线建卡前再连续3轮真实询价，每轮配合同数量合约深度"
            ),
            "pulseCanCreateDirectly": False,
            **(current.get("hotMonitor") or {}),
            **_hot_route_wait_snapshot(),
        },
        "delistingRule": {
            "rule": "币种存在下架公告时，不建立包含公告对应交易所的卡片",
            **(current.get("delistingRule") or {}),
            "enabled": spread_scan_exclude_delisted_exchange_cards(),
        },
    }

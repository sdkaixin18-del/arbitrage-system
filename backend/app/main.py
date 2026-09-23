from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.cache_maintenance import cache_status, run_cache_cleanup as perform_cache_cleanup
from app.astro_sdk import AstroSdkClient, AstroSdkError, astro_auto_card_status, astro_sdk_config
from app.astro_spread_scanner import (
    astro_spread_scanner_status,
    start_astro_spread_scanner,
    stop_astro_spread_scanner,
    update_astro_spread_subscriptions,
    confirm_astro_dex_mapping,
)
from app.database import SessionLocal, get_data_dir, get_database_path, get_db, init_db
from app.exchange_announcements import (
    clear_exchange_announcement_push_logs,
    delete_opportunity_pair_monitor,
    exchange_monitor_log,
    exchange_monitor_log_on_change,
    exchange_opportunity_interval_seconds,
    exchange_push_interval_seconds,
    delisting_opportunities_overview,
    get_exchange_announcements,
    push_exchange_announcements,
    scan_delisting_opportunities,
    set_opportunity_symbol_mute,
)
from app.future_events import router as future_events_router
from app.change_pricing import router as change_pricing_router
from app.factor_review import router as factor_review_router
from app.stock_research import router as stock_research_router
from app.industry_trends import mark_interrupted_industry_trend_jobs, router as industry_trends_router
from app.expectation_pricing import router as expectation_pricing_router
from app.information_screening import router as information_screening_router
from app.decision_review import router as decision_review_router
from app.decision_flow import router as decision_flow_router
from app.system_runtime_log import (
    append_system_runtime_event,
    install_global_exception_hooks,
    record_exception,
    router as system_runtime_log_router,
    uninstall_global_exception_hooks,
)
from app.dex_history import router as dex_history_router
from app.funding_prediction_review import (
    funding_formation_watch_overview,
    funding_prediction_review_overview,
    reconcile_pending_predictions,
    record_prediction_batch,
    start_funding_prediction_scheduler,
    stop_funding_prediction_scheduler,
    sync_funding_formation_watches,
)
from app.funding_cloud_client import (
    FundingCloudError,
    funding_cloud_enabled,
    funding_cloud_request,
    funding_cloud_status,
)
from app.factors import (
    activate_factor_preset,
    create_factor_preset,
    create_factor_tag,
    create_factor_tags_bulk,
    delete_factor_preset,
    delete_factor_tag,
    ensure_factor_defaults,
    exclude_factor_stock,
    factor_auto_tag_overview,
    factor_effect_backtest,
    generate_factor_effect_log,
    factor_hot_tag_review,
    factor_tag_candidates_overview,
    factor_overview,
    factor_tagged_stocks,
    factor_tag_stock_detail,
    factor_stock_tags,
    list_factor_presets,
    list_factor_tags,
    list_factor_effect_logs,
    mark_interrupted_factor_runs,
    run_factor_auto_tag_all,
    refresh_factor_quotes,
    run_factor_tag_candidates,
    update_factor_preset,
    update_factor_stock_status,
    update_factor_stock_tags,
    update_factor_tag,
)
from app.market_review import (
    ai_settings_to_dict,
    collect_daily_materials,
    ensure_default_universe,
    generate_daily_report,
    generate_weekly_report,
    get_market_review_ai_settings,
    market_review_overview,
    material_to_dict,
    parse_report_date,
    run_ai_for_report,
    today_beijing,
    update_ai_settings,
    universe_to_dict,
)
from app.judgment_assistant import append_judgment_record, judgment_assistant_overview
from app.models import (
    FactorQuoteSnapshot,
    MarketReviewMaterial,
    MarketReviewReport,
    MarketReviewUniverseItem,
    WatchlistAnnouncementStock,
    now_utc,
)
from app.opportunity_map import (
    create_opportunity_group,
    create_opportunity_item,
    delete_opportunity_group,
    delete_opportunity_item,
    ensure_opportunity_map_seed,
    opportunity_map_overview,
    update_opportunity_group,
    update_opportunity_item,
)
from app.industry_chains import (
    create_company as create_industry_company,
    create_evidence as create_industry_evidence,
    create_industry_chain,
    create_opportunity_link as create_industry_opportunity_link,
    create_segment as create_industry_segment,
    create_task as create_industry_task,
    delete_company as delete_industry_company,
    delete_evidence as delete_industry_evidence,
    delete_industry_chain,
    delete_opportunity_link as delete_industry_opportunity_link,
    delete_segment as delete_industry_segment,
    delete_task as delete_industry_task,
    ensure_industry_chain_seed,
    industry_chain_detail,
    industry_chain_overview,
    update_company as update_industry_company,
    update_evidence as update_industry_evidence,
    update_industry_chain,
    update_segment as update_industry_segment,
    update_task as update_industry_task,
)
from app.research_runs import (
    apply_item_tags,
    apply_run_tags,
    create_analysis_record,
    create_qq_generation_items,
    delete_research_run,
    ensure_initial_research_seed,
    get_research_run,
    list_research_runs,
    mark_run_processed,
    stock_detail,
    stock_information_flow_endpoint,
    update_generation_item,
)
from app.sector_indices import (
    add_sector_member,
    create_sector_index,
    delete_sector_index,
    delete_sector_member,
    recalculate_sector_result,
    recognize_sector_image_candidates,
    sector_index_bars,
    sector_index_detail,
    sector_indices_overview,
    stock_bars,
    update_sector_index,
)
from app.schemas import (
    CryptoSettingsOut,
    CryptoSymbolMappingCreate,
    CryptoSymbolMappingOut,
    CryptoSymbolMappingUpdate,
    CrawlResult,
    FactorOverview,
    FactorEffectBacktestOut,
    FactorEffectDailyLogOut,
    FactorEffectDailyLogsResponse,
    FactorAutoTagResponse,
    FactorHotTagReviewResponse,
    FactorPresetCreate,
    FactorPresetOut,
    FactorPresetUpdate,
    FactorTagBulkCreate,
    FactorTagCandidateRunRequest,
    FactorTagCandidatesResponse,
    FactorTaggedStocksResponse,
    IndustryChainCompanyCreate,
    IndustryChainCompanyOut,
    IndustryChainCompanyUpdate,
    IndustryChainCreate,
    IndustryChainDetailOut,
    IndustryChainEvidenceCreate,
    IndustryChainEvidenceOut,
    IndustryChainEvidenceUpdate,
    IndustryChainOpportunityLinkCreate,
    IndustryChainOverviewOut,
    IndustryChainSegmentCreate,
    IndustryChainSegmentOut,
    IndustryChainSegmentUpdate,
    IndustryChainTaskCreate,
    IndustryChainTaskOut,
    IndustryChainTaskUpdate,
    IndustryChainUpdate,
    FactorStockStatusUpdate,
    FactorStockTagOut,
    FactorStockTagsUpdate,
    FactorTagCreate,
    FactorTagOut,
    FactorTagStockDetailOut,
    FactorTagUpdate,
    MarketReviewActionResult,
    MarketReviewAiSettingsOut,
    MarketReviewAiSettingsUpdate,
    MarketReviewDatePayload,
    MarketReviewMaterialCreate,
    MarketReviewMaterialOut,
    MarketReviewMaterialUpdate,
    MarketReviewOverview,
    MarketReviewUniverseCreate,
    MarketReviewUniverseOut,
    MarketReviewUniverseUpdate,
    MarketOpportunityGroupCreate,
    MarketOpportunityGroupOut,
    MarketOpportunityGroupUpdate,
    MarketOpportunityItemCreate,
    MarketOpportunityItemOut,
    MarketOpportunityItemUpdate,
    MarketOpportunityOverview,
    NetworkMessageItemOut,
    NetworkMessageSourceOut,
    NetworkMessagesOverview,
    JudgmentAssistantOverview,
    JudgmentRecordCreate,
    JudgmentRecordOut,
    SectorIndexBarOut,
    SectorIndexCreate,
    SectorIndexDetailOut,
    SectorIndexImageImportResponse,
    SectorIndexMemberCreate,
    SectorIndexMemberOut,
    SectorIndexOverview,
    SectorIndexRecalculateResult,
    SectorIndexSummaryOut,
    SectorIndexUpdate,
    StockAnalysisRecordCreate,
    StockAnalysisRecordOut,
    StockBarOut,
    StockDetailOut,
    StockInformationFlowResponse,
    StockResearchApplyTagsRequest,
    StockResearchBatchInitialReviewOut,
    StockResearchGenerationItemOut,
    StockResearchGenerationItemUpdate,
    StockResearchGenerationRunDetailOut,
    StockResearchGenerationRunOut,
    StockResearchQqItemsCreate,
    WatchlistAnnouncementStockSearchOut,
)
from app.watchlist_announcements import (
    search_a_stocks,
)
from app.notifications import bark_status

app = FastAPI(title="Stock Review Mac Workbench", version="0.1.0")
app.include_router(future_events_router)
app.include_router(change_pricing_router)
app.include_router(factor_review_router)
app.include_router(stock_research_router)
app.include_router(industry_trends_router)
app.include_router(expectation_pricing_router)
app.include_router(information_screening_router)
app.include_router(decision_review_router)
app.include_router(decision_flow_router)
app.include_router(system_runtime_log_router)
app.include_router(dex_history_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        os.environ.get("FRONTEND_BASE_URL", "http://127.0.0.1:5173"),
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


@app.middleware("http")
async def system_runtime_observer(request: Request, call_next):
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        record_exception(
            exc,
            module="http",
            path=request.url.path,
            method=request.method,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            details={"query": str(request.url.query)[:1000]},
        )
        raise
    duration_ms = (time.perf_counter() - started_at) * 1000
    if response.status_code >= 400 and request.url.path != "/api/system/runtime-logs/frontend":
        append_system_runtime_event(
            "http_response_error",
            level="error" if response.status_code >= 500 else "warning",
            source="backend",
            module="http",
            message=f"{request.method} {request.url.path} returned {response.status_code}",
            path=request.url.path,
            method=request.method,
            status_code=response.status_code,
            duration_ms=duration_ms,
            details={"query": str(request.url.query)[:1000]},
        )
    elif duration_ms >= 10_000 and request.url.path != "/api/system/runtime-logs":
        append_system_runtime_event(
            "slow_request",
            level="warning",
            source="backend",
            module="http",
            message=f"{request.method} {request.url.path} took {duration_ms / 1000:.1f}s",
            path=request.url.path,
            method=request.method,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
    return response

_crypto_module: Any | None = None
_crypto_module_lock = threading.Lock()
_fs_scheduler_stop = threading.Event()
_fs_scheduler_thread: threading.Thread | None = None
_fs_scan_lock = threading.Lock()
_fs_scheduler_enabled_override: bool | None = None
_fs_scheduler_last_error: str | None = None
_fs_scheduler_last_scan_started_at: datetime | None = None
_fs_scheduler_last_scan_at: datetime | None = None
_fs_scheduler_last_scan_duration_seconds: float | None = None
_fs_scheduler_next_scan_at: datetime | None = None
_fs_scheduler_last_skip_reason: str | None = None
_fs_borrow_fast_scheduler_stop = threading.Event()
_fs_borrow_fast_scheduler_thread: threading.Thread | None = None
_fs_borrow_fast_last_scan_at: datetime | None = None
_fs_borrow_fast_last_scan_duration_seconds: float | None = None
_fs_borrow_fast_last_error: str | None = None
_fs_borrow_fast_next_scan_at: datetime | None = None
_fs_borrow_fast_last_skip_reason: str | None = None
_exchange_ann_scheduler_stop = threading.Event()
_exchange_ann_scheduler_thread: threading.Thread | None = None
_exchange_ann_push_lock = threading.Lock()
_exchange_opportunity_scheduler_stop = threading.Event()
_exchange_opportunity_scheduler_thread: threading.Thread | None = None
_exchange_opportunity_scan_lock = threading.Lock()
_runtime_cache_lock = threading.Lock()
_runtime_cache: dict[str, tuple[float, Any]] = {}
_runtime_cache_flights: dict[str, threading.Lock] = {}


def crypto_service() -> Any:
    global _crypto_module
    if _crypto_module is not None:
        return _crypto_module
    with _crypto_module_lock:
        if _crypto_module is None:
            from app import crypto as crypto_module

            _crypto_module = crypto_module
    return _crypto_module
_cache_cleanup_scheduler_stop = threading.Event()
_cache_cleanup_scheduler_thread: threading.Thread | None = None
_cache_cleanup_lock = threading.Lock()
_zsxq_cache: dict[str, Any] = {"at": 0.0, "key": "", "items": [], "source": None}
_zsxq_cache_seconds = 300
_network_messages_cache: dict[str, Any] = {"at": 0.0, "key": "", "payload": None}
_network_messages_cache_seconds = 600
_network_messages_cache_lock = threading.Lock()
_network_image_ocr_cache: dict[str, dict[str, str | None]] = {}


def cached_runtime_value(key: str, ttl_seconds: int, factory) -> Any:
    now = time.time()
    with _runtime_cache_lock:
        cached = _runtime_cache.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
        flight = _runtime_cache_flights.setdefault(key, threading.Lock())
    with flight:
        now = time.time()
        with _runtime_cache_lock:
            cached = _runtime_cache.get(key)
            if cached and now - cached[0] < ttl_seconds:
                return cached[1]
        value = factory()
        with _runtime_cache_lock:
            _runtime_cache[key] = (time.time(), value)
        return value


def clear_runtime_cache(*prefixes: str) -> None:
    with _runtime_cache_lock:
        if not prefixes:
            _runtime_cache.clear()
            return
        for key in list(_runtime_cache):
            if any(key.startswith(prefix) for prefix in prefixes):
                _runtime_cache.pop(key, None)


def clear_factor_runtime_cache() -> None:
    clear_runtime_cache("factors:", "factor-effect-backtest:")


def prewarm_factor_runtime_cache() -> None:
    def worker() -> None:
        time.sleep(8)
        try:
            with SessionLocal() as db:
                cached_runtime_value(
                    "factor-effect-backtest:20:3:5:80:4",
                    1800,
                    lambda: factor_effect_backtest(
                        db,
                        label_source="factor",
                        lookback_days=20,
                        momentum_period=3,
                        forward_period=5,
                        top_n=80,
                        min_sample=4,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            print(f"factor cache prewarm failed: {exc}", flush=True)

    threading.Thread(target=worker, name="factor-cache-prewarm", daemon=True).start()


def initialize_runtime_defaults() -> None:
    with SessionLocal() as db:
        crypto_service().mark_interrupted_fs_runtime_scans(db)
        ensure_default_universe(db)
        mark_interrupted_factor_runs(db)
        ensure_factor_defaults(db)
        ensure_opportunity_map_seed(db)
        ensure_industry_chain_seed(db)
        mark_interrupted_industry_trend_jobs(db)


def initialize_runtime_defaults_async() -> None:
    def worker() -> None:
        started_at = time.perf_counter()
        try:
            initialize_runtime_defaults()
            print(f"runtime defaults ready in {time.perf_counter() - started_at:.3f}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"runtime defaults failed: {exc}", flush=True)

    threading.Thread(target=worker, name="runtime-defaults", daemon=True).start()


@app.on_event("startup")
def on_startup() -> None:
    started_at = time.perf_counter()
    install_global_exception_hooks()
    schema_changed = init_db()
    if schema_changed:
        initialize_runtime_defaults()
    else:
        initialize_runtime_defaults_async()
    start_cache_cleanup_scheduler()
    run_cache_cleanup_async("startup")
    start_fs_scheduler()
    from app import transfer_watch_bridge
    transfer_watch_bridge.start()
    start_astro_spread_scanner()
    if not funding_cloud_enabled():
        start_funding_prediction_scheduler()
    start_exchange_ann_scheduler()
    start_exchange_opportunity_scheduler()
    print(f"backend startup ready in {time.perf_counter() - started_at:.3f}s", flush=True)


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_fs_scheduler()
    from app import transfer_watch_bridge
    transfer_watch_bridge.stop()
    stop_astro_spread_scanner()
    stop_funding_prediction_scheduler()
    stop_exchange_ann_scheduler()
    stop_exchange_opportunity_scheduler()
    stop_cache_cleanup_scheduler()
    uninstall_global_exception_hooks()


def run_cache_cleanup_once(reason: str) -> dict[str, Any]:
    if not _cache_cleanup_lock.acquire(blocking=False):
        return {
            "status": "manual_only",
            "reason": reason,
            "message": "缓存清理正在运行，本次请求已跳过。",
            "deleted_rows": {},
            "browser_cache": {"freed_bytes": 0, "removed_dirs": []},
        }
    db = SessionLocal()
    try:
        return perform_cache_cleanup(db, reason=reason)
    finally:
        db.close()
        _cache_cleanup_lock.release()


def run_cache_cleanup_async(reason: str) -> None:
    threading.Thread(
        target=run_cache_cleanup_once,
        args=(reason,),
        name=f"cache-cleanup-{reason}",
        daemon=True,
    ).start()


def start_cache_cleanup_scheduler() -> None:
    global _cache_cleanup_scheduler_thread
    if os.environ.get("CACHE_CLEANUP_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    if _cache_cleanup_scheduler_thread and _cache_cleanup_scheduler_thread.is_alive():
        return
    _cache_cleanup_scheduler_stop.clear()
    _cache_cleanup_scheduler_thread = threading.Thread(
        target=cache_cleanup_scheduler_loop,
        name="cache-cleanup-scheduler",
        daemon=True,
    )
    _cache_cleanup_scheduler_thread.start()


def stop_cache_cleanup_scheduler() -> None:
    _cache_cleanup_scheduler_stop.set()


def cache_cleanup_scheduler_loop() -> None:
    interval_seconds = max(3600, int(os.environ.get("CACHE_CLEANUP_INTERVAL_SECONDS", "86400")))
    while not _cache_cleanup_scheduler_stop.wait(interval_seconds):
        run_cache_cleanup_once("scheduled")


def fs_auto_scan_enabled() -> bool:
    if _fs_scheduler_enabled_override is not None:
        return _fs_scheduler_enabled_override
    state = load_fs_scheduler_state()
    if state and "enabled" in state:
        return bool(state["enabled"])
    return os.environ.get("FS_AUTO_SCAN_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def astro_background_work_allowed() -> tuple[bool, str | None]:
    """Keep lower-priority network scans out of an active Astro opportunity window."""

    scanner = astro_spread_scanner_status()
    if not scanner.get("enabled"):
        return True, None
    if not scanner.get("running") or not scanner.get("lastScanAt"):
        return False, "astro_warming_up"
    if scanner.get("lastError") or scanner.get("pulseHealthy") is False:
        return False, "astro_recovering"
    hot_monitor = scanner.get("hotMonitor") if isinstance(scanner.get("hotMonitor"), dict) else {}
    hot_registration = (
        scanner.get("hotRegistration")
        if isinstance(scanner.get("hotRegistration"), dict)
        else {}
    )
    # hotMonitor is updated by the direct-check loop and can lag while a batch
    # is in flight. hotRegistration is refreshed by every Pulse scan, so use
    # both views to keep background jobs out of any active opportunity window.
    active_hot_routes = max(
        int(hot_monitor.get("aboveThresholdRouteCount") or 0),
        int(hot_registration.get("aboveThresholdRouteCount") or 0),
    )
    if active_hot_routes > 0:
        return False, "astro_opportunity_active"
    interval_ms = max(1, int(scanner.get("intervalSeconds") or 5)) * 1000
    if float(scanner.get("lastScanDurationMs") or 0.0) > interval_ms:
        return False, "astro_scan_over_budget"
    return True, None


def fs_scheduler_state_path() -> Path:
    return get_data_dir() / "fs-scheduler-state.json"


def load_fs_scheduler_state() -> dict[str, Any] | None:
    path = fs_scheduler_state_path()
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_fs_scheduler_state(enabled: bool) -> None:
    path = fs_scheduler_state_path()
    payload = {
        "enabled": bool(enabled),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def fs_auto_scan_interval_seconds() -> int:
    try:
        value = int(os.environ.get("FS_AUTO_SCAN_SECONDS", "300"))
    except ValueError:
        value = 300
    return max(120, value)


def fs_auto_scan_limit() -> int:
    try:
        value = int(os.environ.get("FS_AUTO_SCAN_LIMIT", "12"))
    except ValueError:
        value = 12
    return max(1, min(value, 100))


def start_fs_borrow_fast_scheduler() -> None:
    global _fs_borrow_fast_scheduler_thread
    if not fs_auto_scan_enabled():
        return
    _fs_borrow_fast_scheduler_stop.clear()
    if _fs_borrow_fast_scheduler_thread and _fs_borrow_fast_scheduler_thread.is_alive():
        return
    _fs_borrow_fast_scheduler_thread = threading.Thread(
        target=fs_borrow_fast_scheduler_loop,
        name="fs-borrow-fast-scheduler",
        daemon=True,
    )
    _fs_borrow_fast_scheduler_thread.start()


def fs_borrow_fast_scheduler_loop() -> None:
    global _fs_borrow_fast_next_scan_at, _fs_borrow_fast_last_skip_reason
    service = crypto_service()
    interval_seconds = service.fs_borrow_fast_scan_interval_seconds()
    next_deadline = time.monotonic() + 5
    while True:
        wait_seconds = max(0.0, next_deadline - time.monotonic())
        _fs_borrow_fast_next_scan_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
        if _fs_borrow_fast_scheduler_stop.wait(wait_seconds):
            _fs_borrow_fast_next_scan_at = None
            return
        _fs_borrow_fast_next_scan_at = None
        allowed, skip_reason = astro_background_work_allowed()
        if not allowed:
            _fs_borrow_fast_last_skip_reason = skip_reason
            next_deadline = time.monotonic() + min(15, interval_seconds)
            continue
        _fs_borrow_fast_last_skip_reason = None
        run_scheduled_fs_borrow_fast_scan()
        next_deadline += interval_seconds
        while next_deadline <= time.monotonic():
            next_deadline += interval_seconds


def run_scheduled_fs_borrow_fast_scan() -> None:
    global _fs_borrow_fast_last_scan_at, _fs_borrow_fast_last_scan_duration_seconds
    global _fs_borrow_fast_last_error
    started_monotonic = time.monotonic()
    db = SessionLocal()
    try:
        service = crypto_service()
        result = service.refresh_fs_borrow_fast_inventory(
            db,
            limit=service.fs_borrow_fast_scan_limit(),
            push=False,
        )
        if result.get("status") == "error":
            _fs_borrow_fast_last_error = str(result.get("message") or "B 快扫失败")
        else:
            _fs_borrow_fast_last_error = None
        _fs_borrow_fast_last_scan_at = datetime.now(timezone.utc)
    except Exception as exc:
        _fs_borrow_fast_last_error = str(exc)
    finally:
        _fs_borrow_fast_last_scan_duration_seconds = round(time.monotonic() - started_monotonic, 2)
        db.close()


def start_fs_scheduler() -> None:
    global _fs_scheduler_thread
    if not fs_auto_scan_enabled():
        return
    _fs_scheduler_stop.clear()
    start_fs_borrow_fast_scheduler()
    if _fs_scheduler_thread and _fs_scheduler_thread.is_alive():
        return
    _fs_scheduler_thread = threading.Thread(target=fs_scheduler_loop, name="fs-signal-scheduler", daemon=True)
    _fs_scheduler_thread.start()


def stop_fs_scheduler() -> None:
    _fs_scheduler_stop.set()
    _fs_borrow_fast_scheduler_stop.set()


def fs_scheduler_status() -> dict[str, Any]:
    enabled = fs_auto_scan_enabled()
    running = bool(_fs_scheduler_thread and _fs_scheduler_thread.is_alive() and not _fs_scheduler_stop.is_set())
    if enabled and not running:
        start_fs_scheduler()
        running = bool(_fs_scheduler_thread and _fs_scheduler_thread.is_alive() and not _fs_scheduler_stop.is_set())
    borrow_fast_running = bool(
        _fs_borrow_fast_scheduler_thread
        and _fs_borrow_fast_scheduler_thread.is_alive()
        and not _fs_borrow_fast_scheduler_stop.is_set()
    )
    borrow_fast_state = crypto_service().fs_borrow_fast_scan_status()
    return {
        "status": "ok",
        "enabled": enabled,
        "running": running,
        "scanning": _fs_scan_lock.locked(),
        "intervalSeconds": fs_auto_scan_interval_seconds(),
        "limit": fs_auto_scan_limit(),
        "lastScanStartedAt": _fs_scheduler_last_scan_started_at,
        "lastScanAt": _fs_scheduler_last_scan_at,
        "lastScanDurationSeconds": _fs_scheduler_last_scan_duration_seconds,
        "nextScanAt": _fs_scheduler_next_scan_at,
        "lastError": _fs_scheduler_last_error,
        "borrowFastScan": {
            **borrow_fast_state,
            "enabled": enabled,
            "schedulerRunning": borrow_fast_running,
            "lastScanAt": _fs_borrow_fast_last_scan_at,
            "lastScanDurationSeconds": _fs_borrow_fast_last_scan_duration_seconds,
            "nextScanAt": _fs_borrow_fast_next_scan_at,
            "lastError": _fs_borrow_fast_last_error or borrow_fast_state.get("lastError"),
            "lastSkipReason": _fs_borrow_fast_last_skip_reason,
        },
        "lastSkipReason": _fs_scheduler_last_skip_reason,
    }


def set_fs_scheduler_enabled(enabled: bool) -> dict[str, Any]:
    global _fs_scheduler_enabled_override
    _fs_scheduler_enabled_override = enabled
    save_fs_scheduler_state(enabled)
    if enabled:
        start_fs_scheduler()
        threading.Thread(target=run_scheduled_fs_scan, name="fs-signal-manual-start", daemon=True).start()
    else:
        stop_fs_scheduler()
        crypto_service().pause_fs_signal_background_scans()
    return fs_scheduler_status()


def fs_scheduler_loop() -> None:
    global _fs_scheduler_next_scan_at, _fs_scheduler_last_skip_reason
    # Keep a fixed cadence: scan time no longer adds another full interval.
    interval_seconds = fs_auto_scan_interval_seconds()
    next_deadline = time.monotonic() + 60
    while True:
        wait_seconds = max(0.0, next_deadline - time.monotonic())
        _fs_scheduler_next_scan_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
        if _fs_scheduler_stop.wait(wait_seconds):
            _fs_scheduler_next_scan_at = None
            return
        _fs_scheduler_next_scan_at = None
        allowed, skip_reason = astro_background_work_allowed()
        if not allowed:
            _fs_scheduler_last_skip_reason = skip_reason
            next_deadline = time.monotonic() + min(30, interval_seconds)
            continue
        _fs_scheduler_last_skip_reason = None
        run_scheduled_fs_scan()
        next_deadline += interval_seconds
        while next_deadline <= time.monotonic():
            next_deadline += interval_seconds


def run_scheduled_fs_scan() -> None:
    global _fs_scheduler_last_error, _fs_scheduler_last_scan_at
    global _fs_scheduler_last_scan_started_at, _fs_scheduler_last_scan_duration_seconds
    if not _fs_scan_lock.acquire(blocking=False):
        db = SessionLocal()
        try:
            service = crypto_service()
            service.add_fs_runtime_log(
                db,
                service.fs_runtime_scan_id(),
                "scan_skipped",
                level="warning",
                stage="scheduler",
                status="busy",
                message="上一轮 FS 扫描仍在运行，本轮已跳过。",
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        return
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    _fs_scheduler_last_scan_started_at = started_at
    db = SessionLocal()
    service = crypto_service()
    scan_limit = fs_auto_scan_limit()
    service.begin_fs_signal_scan(scan_limit, push=False, started_at=started_at)
    try:
        payload = service.compute_crypto_fs_signals_overview(
            db,
            scan_limit,
            push=False,
            schedule_auto_cards=True,
        )
        service.cache_fs_signal_scan_result(
            scan_limit,
            payload,
            started_at=started_at,
            push=False,
        )
        service.refresh_funding_cap_watchlist(db, push=True)
        _fs_scheduler_last_scan_at = datetime.now(timezone.utc)
        _fs_scheduler_last_error = None
    except Exception as exc:
        service.fail_fs_signal_scan(
            scan_limit,
            started_at=started_at,
            error=exc,
            push=False,
        )
        _fs_scheduler_last_error = str(exc)
    finally:
        _fs_scheduler_last_scan_duration_seconds = round(time.monotonic() - started_monotonic, 1)
        db.close()
        _fs_scan_lock.release()


def start_exchange_ann_scheduler() -> None:
    global _exchange_ann_scheduler_thread
    if os.environ.get("EXCHANGE_ANN_AUTO_PUSH", "1") != "1":
        return
    if _exchange_ann_scheduler_thread and _exchange_ann_scheduler_thread.is_alive():
        return
    _exchange_ann_scheduler_stop.clear()
    _exchange_ann_scheduler_thread = threading.Thread(
        target=exchange_ann_scheduler_loop,
        name="exchange-announcement-push-scheduler",
        daemon=True,
    )
    _exchange_ann_scheduler_thread.start()


def stop_exchange_ann_scheduler() -> None:
    _exchange_ann_scheduler_stop.set()


def exchange_ann_scheduler_loop() -> None:
    if not _exchange_ann_scheduler_stop.wait(15):
        run_scheduled_exchange_ann_push()
    while not _exchange_ann_scheduler_stop.wait(exchange_push_interval_seconds()):
        run_scheduled_exchange_ann_push()


def run_scheduled_exchange_ann_push() -> None:
    if not _exchange_ann_push_lock.acquire(blocking=False):
        exchange_monitor_log("scheduler_cycle_skipped", reason="previous_cycle_still_running", level="warning")
        return
    db = SessionLocal()
    try:
        push_exchange_announcements(db, force_refresh=False)
    except Exception as exc:
        db.rollback()
        exchange_monitor_log(
            "scheduler_cycle_failed",
            error=str(exc),
            level="error",
            exc_info=True,
        )
    finally:
        db.close()
        _exchange_ann_push_lock.release()


def start_exchange_opportunity_scheduler() -> None:
    global _exchange_opportunity_scheduler_thread
    # The user-facing volatility-opportunity module has been retired. Keep the
    # scheduler opt-in so announcement discovery and Astro listing linkage can
    # continue without silently restoring the retired push channel.
    if os.environ.get("EXCHANGE_OPPORTUNITY_AUTO_SCAN", "0") != "1":
        return
    if _exchange_opportunity_scheduler_thread and _exchange_opportunity_scheduler_thread.is_alive():
        return
    _exchange_opportunity_scheduler_stop.clear()
    _exchange_opportunity_scheduler_thread = threading.Thread(
        target=exchange_opportunity_scheduler_loop,
        name="exchange-delisting-opportunity-scheduler",
        daemon=True,
    )
    _exchange_opportunity_scheduler_thread.start()


def stop_exchange_opportunity_scheduler() -> None:
    _exchange_opportunity_scheduler_stop.set()


def exchange_opportunity_scheduler_loop() -> None:
    if not _exchange_opportunity_scheduler_stop.wait(25):
        run_scheduled_exchange_opportunity_scan()
    while not _exchange_opportunity_scheduler_stop.wait(exchange_opportunity_interval_seconds()):
        run_scheduled_exchange_opportunity_scan()


def run_scheduled_exchange_opportunity_scan() -> None:
    allowed, skip_reason = astro_background_work_allowed()
    if not allowed:
        exchange_monitor_log_on_change(
            "opportunity_scheduler_state",
            status="deferred",
            reason=skip_reason,
            level="info",
            state_fields={"status": "deferred", "reason": skip_reason},
        )
        return
    if not _exchange_opportunity_scan_lock.acquire(blocking=False):
        exchange_monitor_log_on_change(
            "opportunity_scheduler_state",
            status="busy",
            reason="previous_scan_still_running",
            level="warning",
            state_fields={"status": "busy", "reason": "previous_scan_still_running"},
        )
        return
    db = SessionLocal()
    try:
        exchange_monitor_log_on_change(
            "opportunity_scheduler_state",
            status="running",
            reason=None,
            state_fields={"status": "running"},
        )
        scan_delisting_opportunities(db, push=True)
    except Exception as exc:
        db.rollback()
        exchange_monitor_log(
            "opportunity_scheduler_failed",
            error=str(exc),
            level="error",
            exc_info=True,
        )
    finally:
        db.close()
        _exchange_opportunity_scan_lock.release()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "stock-review-mac", "database": str(get_database_path())}


@app.get("/api/system/status")
def system_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    scanner = astro_spread_scanner_status()
    hot_monitor = scanner.get("hotMonitor") if isinstance(scanner.get("hotMonitor"), dict) else {}
    fs_status = fs_scheduler_status()
    api_priority = crypto_service().api_priority_status()
    funding_compute_location = "tencent_cloud" if funding_cloud_enabled() else "local"
    return {
        "status": "ok",
        "app_env": os.environ.get("APP_ENV", "local"),
        "data_dir": str(get_data_dir()),
        "database": {"status": "ok", "path": str(get_database_path())},
        "bark": {"status": bark_status()},
        "market_review": {"status": "manual_only"},
        "exchange_announcements": {
            "status": "ok",
            "auto_push": os.environ.get("EXCHANGE_ANN_AUTO_PUSH", "1") == "1",
            "interval_seconds": exchange_push_interval_seconds(),
        },
        "exchange_delisting_opportunities": {
            "status": "ok",
            "auto_scan": os.environ.get("EXCHANGE_OPPORTUNITY_AUTO_SCAN", "0") == "1",
            "interval_seconds": exchange_opportunity_interval_seconds(),
        },
        "workload_priority": {
            "policy": "astro_scan_and_card_first",
            "critical": {
                "modules": ["astro_spread_scan", "astro_hot_route_monitor", "astro_card_sync"],
                "scan_interval_seconds": scanner.get("intervalSeconds"),
                "last_scan_duration_ms": scanner.get("lastScanDurationMs"),
                "hot_monitor_interval_ms": hot_monitor.get("intervalMs"),
                "card_queue_preemption": True,
                "api_priority": api_priority,
            },
            "background": {
                "modules": ["fs_full_scan", "fs_borrow_fast_scan", "exchange_announcements", "exchange_opportunity_scan"],
                "fs_interval_seconds": fs_status.get("intervalSeconds"),
                "fs_limit": fs_status.get("limit"),
                "borrow_fast_interval_seconds": (fs_status.get("borrowFastScan") or {}).get("intervalSeconds"),
                "exchange_announcement_interval_seconds": exchange_push_interval_seconds(),
                "exchange_opportunity_interval_seconds": exchange_opportunity_interval_seconds(),
            },
            "maintenance": {
                "modules": ["cache_cleanup"] + (["funding_prediction"] if not funding_cloud_enabled() else []),
                "shared_crypto_api_priority": "normal",
                "funding_compute_location": funding_compute_location,
                "local_funding_prediction_paused": funding_cloud_enabled(),
            },
        },
        "llm": {"status": "not_configured" if os.environ.get("LLM_PROVIDER", "mock") == "mock" else "ok"},
        "quotes": {"status": "not_configured"},
    }


@app.get("/api/system/cache")
def system_cache_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return cache_status(db)


@app.post("/api/system/cache/cleanup")
def cleanup_system_cache() -> dict[str, Any]:
    return run_cache_cleanup_once("manual")


def simple_page(name: str, status: str = "manual_only") -> dict[str, Any]:
    return {
        "status": status,
        "source_status": status,
        "updated_at": None,
        "items": [],
        "message": f"{name} 已预留极简入口，当前支持本地维护或后续接入真实源。",
    }


@app.get("/api/factors", response_model=FactorOverview)
def factors(db: Session = Depends(get_db)) -> FactorOverview:
    return FactorOverview(**cached_runtime_value("factors:overview", 180, lambda: factor_overview(db)))


@app.get("/api/factors/status")
def factors_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    latest = db.scalar(select(FactorQuoteSnapshot).order_by(desc(FactorQuoteSnapshot.updated_at)).limit(1))
    return {
        "status": "ok" if latest else "not_configured",
        "source_status": latest.source_status if latest else "not_configured",
        "updated_at": latest.updated_at if latest else None,
        "message": latest.last_error if latest else "行情缓存尚未生成。",
    }


@app.get("/api/factors/effect-backtest", response_model=FactorEffectBacktestOut)
def factor_effect_backtest_endpoint(
    lookback_days: int = 20,
    momentum_period: int = 3,
    forward_period: int = 5,
    top_n: int = 80,
    min_sample: int = 4,
    db: Session = Depends(get_db),
) -> FactorEffectBacktestOut:
    cache_key = f"factor-effect-backtest:{lookback_days}:{momentum_period}:{forward_period}:{top_n}:{min_sample}"
    return FactorEffectBacktestOut(
        **cached_runtime_value(
            cache_key,
            1800,
            lambda: factor_effect_backtest(
                db,
                label_source="factor",
                lookback_days=lookback_days,
                momentum_period=momentum_period,
                forward_period=forward_period,
                top_n=top_n,
                min_sample=min_sample,
            ),
        )
    )


@app.get("/api/factors/effect-logs", response_model=FactorEffectDailyLogsResponse)
def factor_effect_logs_endpoint(limit: int = 30, db: Session = Depends(get_db)) -> FactorEffectDailyLogsResponse:
    return FactorEffectDailyLogsResponse(**list_factor_effect_logs(db, limit=limit))


@app.post("/api/factors/effect-logs", response_model=FactorEffectDailyLogOut)
def generate_factor_effect_log_endpoint(db: Session = Depends(get_db)) -> FactorEffectDailyLogOut:
    log = generate_factor_effect_log(db)
    return FactorEffectDailyLogOut(**log)


@app.get("/api/factors/auto-tags", response_model=FactorAutoTagResponse)
def factor_auto_tags_endpoint(db: Session = Depends(get_db)) -> FactorAutoTagResponse:
    return FactorAutoTagResponse(**factor_auto_tag_overview(db))


@app.post("/api/factors/auto-tags/run", response_model=FactorAutoTagResponse)
def run_factor_auto_tags_endpoint(db: Session = Depends(get_db)) -> FactorAutoTagResponse:
    result = run_factor_auto_tag_all(db)
    clear_factor_runtime_cache()
    return FactorAutoTagResponse(**result)


@app.get("/api/factors/tagged-stocks", response_model=FactorTaggedStocksResponse)
def factor_tagged_stocks_endpoint(
    limit: int = 800,
    offset: int = 0,
    query: str | None = None,
    db: Session = Depends(get_db),
) -> FactorTaggedStocksResponse:
    return FactorTaggedStocksResponse(**factor_tagged_stocks(db, limit, offset, query))


@app.get("/api/factors/hot-tag-review", response_model=FactorHotTagReviewResponse)
def factor_hot_tag_review_endpoint(
    gain_limit: int = 200,
    amount_limit: int = 200,
    db: Session = Depends(get_db),
) -> FactorHotTagReviewResponse:
    return FactorHotTagReviewResponse(**factor_hot_tag_review(db, gain_limit, amount_limit))


@app.get("/api/factors/opportunity-map", response_model=MarketOpportunityOverview)
def opportunity_map(db: Session = Depends(get_db)) -> MarketOpportunityOverview:
    return MarketOpportunityOverview(**opportunity_map_overview(db))


@app.post("/api/factors/opportunity-map/groups", response_model=MarketOpportunityGroupOut)
def create_opportunity_group_endpoint(
    payload: MarketOpportunityGroupCreate,
    db: Session = Depends(get_db),
) -> MarketOpportunityGroupOut:
    try:
        return MarketOpportunityGroupOut(**create_opportunity_group(db, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/factors/opportunity-map/groups/{group_id}", response_model=MarketOpportunityGroupOut)
def update_opportunity_group_endpoint(
    group_id: int,
    payload: MarketOpportunityGroupUpdate,
    db: Session = Depends(get_db),
) -> MarketOpportunityGroupOut:
    try:
        return MarketOpportunityGroupOut(**update_opportunity_group(db, group_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/factors/opportunity-map/groups/{group_id}")
def delete_opportunity_group_endpoint(group_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_opportunity_group(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/factors/opportunity-map/groups/{group_id}/items", response_model=MarketOpportunityItemOut)
def create_opportunity_item_endpoint(
    group_id: int,
    payload: MarketOpportunityItemCreate,
    db: Session = Depends(get_db),
) -> MarketOpportunityItemOut:
    try:
        return MarketOpportunityItemOut(**create_opportunity_item(db, group_id, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.patch("/api/factors/opportunity-map/items/{item_id}", response_model=MarketOpportunityItemOut)
def update_opportunity_item_endpoint(
    item_id: int,
    payload: MarketOpportunityItemUpdate,
    db: Session = Depends(get_db),
) -> MarketOpportunityItemOut:
    try:
        return MarketOpportunityItemOut(**update_opportunity_item(db, item_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/factors/opportunity-map/items/{item_id}")
def delete_opportunity_item_endpoint(item_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_opportunity_item(db, item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/industry-chains", response_model=IndustryChainOverviewOut)
def industry_chains(db: Session = Depends(get_db)) -> IndustryChainOverviewOut:
    return IndustryChainOverviewOut(**industry_chain_overview(db))


@app.post("/api/industry-chains", response_model=IndustryChainDetailOut)
def create_industry_chain_endpoint(payload: IndustryChainCreate, db: Session = Depends(get_db)) -> IndustryChainDetailOut:
    try:
        return IndustryChainDetailOut(**create_industry_chain(db, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/industry-chains/opportunity-links")
def create_industry_opportunity_link_endpoint(payload: IndustryChainOpportunityLinkCreate, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return create_industry_opportunity_link(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/industry-chains/opportunity-links/{link_id}")
def delete_industry_opportunity_link_endpoint(link_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_industry_opportunity_link(db, link_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/industry-chains/{chain_id}", response_model=IndustryChainDetailOut)
def industry_chain(chain_id: int, db: Session = Depends(get_db)) -> IndustryChainDetailOut:
    try:
        return IndustryChainDetailOut(**industry_chain_detail(db, chain_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/industry-chains/{chain_id}", response_model=IndustryChainDetailOut)
def update_industry_chain_endpoint(chain_id: int, payload: IndustryChainUpdate, db: Session = Depends(get_db)) -> IndustryChainDetailOut:
    try:
        return IndustryChainDetailOut(**update_industry_chain(db, chain_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/industry-chains/{chain_id}")
def delete_industry_chain_endpoint(chain_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_industry_chain(db, chain_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/industry-chains/{chain_id}/segments", response_model=IndustryChainSegmentOut)
def create_industry_segment_endpoint(chain_id: int, payload: IndustryChainSegmentCreate, db: Session = Depends(get_db)) -> IndustryChainSegmentOut:
    try:
        return IndustryChainSegmentOut(**create_industry_segment(db, chain_id, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.patch("/api/industry-chains/segments/{segment_id}", response_model=IndustryChainSegmentOut)
def update_industry_segment_endpoint(segment_id: int, payload: IndustryChainSegmentUpdate, db: Session = Depends(get_db)) -> IndustryChainSegmentOut:
    try:
        return IndustryChainSegmentOut(**update_industry_segment(db, segment_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/industry-chains/segments/{segment_id}")
def delete_industry_segment_endpoint(segment_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_industry_segment(db, segment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.post("/api/industry-chains/{chain_id}/companies", response_model=IndustryChainCompanyOut)
def create_industry_company_endpoint(chain_id: int, payload: IndustryChainCompanyCreate, db: Session = Depends(get_db)) -> IndustryChainCompanyOut:
    try:
        return IndustryChainCompanyOut(**create_industry_company(db, chain_id, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.patch("/api/industry-chains/companies/{company_id}", response_model=IndustryChainCompanyOut)
def update_industry_company_endpoint(company_id: int, payload: IndustryChainCompanyUpdate, db: Session = Depends(get_db)) -> IndustryChainCompanyOut:
    try:
        return IndustryChainCompanyOut(**update_industry_company(db, company_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/industry-chains/companies/{company_id}")
def delete_industry_company_endpoint(company_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_industry_company(db, company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/industry-chains/{chain_id}/evidence", response_model=IndustryChainEvidenceOut)
def create_industry_evidence_endpoint(chain_id: int, payload: IndustryChainEvidenceCreate, db: Session = Depends(get_db)) -> IndustryChainEvidenceOut:
    try:
        return IndustryChainEvidenceOut(**create_industry_evidence(db, chain_id, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.patch("/api/industry-chains/evidence/{evidence_id}", response_model=IndustryChainEvidenceOut)
def update_industry_evidence_endpoint(evidence_id: int, payload: IndustryChainEvidenceUpdate, db: Session = Depends(get_db)) -> IndustryChainEvidenceOut:
    try:
        return IndustryChainEvidenceOut(**update_industry_evidence(db, evidence_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/industry-chains/evidence/{evidence_id}")
def delete_industry_evidence_endpoint(evidence_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_industry_evidence(db, evidence_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/industry-chains/{chain_id}/tasks", response_model=IndustryChainTaskOut)
def create_industry_task_endpoint(chain_id: int, payload: IndustryChainTaskCreate, db: Session = Depends(get_db)) -> IndustryChainTaskOut:
    try:
        return IndustryChainTaskOut(**create_industry_task(db, chain_id, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.patch("/api/industry-chains/tasks/{task_id}", response_model=IndustryChainTaskOut)
def update_industry_task_endpoint(task_id: int, payload: IndustryChainTaskUpdate, db: Session = Depends(get_db)) -> IndustryChainTaskOut:
    try:
        return IndustryChainTaskOut(**update_industry_task(db, task_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/industry-chains/tasks/{task_id}")
def delete_industry_task_endpoint(task_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        return delete_industry_task(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/factors/tags", response_model=list[FactorTagOut])
def factor_tags(db: Session = Depends(get_db)) -> list[FactorTagOut]:
    return [FactorTagOut(**tag) for tag in list_factor_tags(db)]


@app.get("/api/factors/tags/{tag_id}/stocks", response_model=FactorTagStockDetailOut)
def factor_tag_stocks_endpoint(
    tag_id: int,
    period: int = 3,
    direction: str = "winning",
    db: Session = Depends(get_db),
) -> FactorTagStockDetailOut:
    try:
        return FactorTagStockDetailOut(**factor_tag_stock_detail(db, tag_id, period, direction))
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.post("/api/factors/tags/bulk", response_model=list[FactorTagOut])
def create_factor_tags_bulk_endpoint(payload: FactorTagBulkCreate, db: Session = Depends(get_db)) -> list[FactorTagOut]:
    try:
        result = create_factor_tags_bulk(db, payload.names)
        clear_factor_runtime_cache()
        return [FactorTagOut(**tag) for tag in result]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/factors/tag-candidates", response_model=FactorTagCandidatesResponse)
def factor_tag_candidates(db: Session = Depends(get_db)) -> FactorTagCandidatesResponse:
    return FactorTagCandidatesResponse(**factor_tag_candidates_overview(db))


@app.post("/api/factors/tag-candidates/run", response_model=FactorTagCandidatesResponse)
def run_factor_tag_candidates_endpoint(
    payload: FactorTagCandidateRunRequest | None = None,
    db: Session = Depends(get_db),
) -> FactorTagCandidatesResponse:
    data = payload or FactorTagCandidateRunRequest()
    return FactorTagCandidatesResponse(**run_factor_tag_candidates(db, data.lookback_days, data.limit))


@app.post("/api/factors/tags", response_model=FactorTagOut)
def create_factor_tag_endpoint(payload: FactorTagCreate, db: Session = Depends(get_db)) -> FactorTagOut:
    try:
        result = create_factor_tag(db, payload.name, payload.color)
        clear_factor_runtime_cache()
        return FactorTagOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/factors/tags/{tag_id}", response_model=FactorTagOut)
def update_factor_tag_endpoint(tag_id: int, payload: FactorTagUpdate, db: Session = Depends(get_db)) -> FactorTagOut:
    try:
        result = update_factor_tag(db, tag_id, payload.model_dump(exclude_unset=True))
        clear_factor_runtime_cache()
        return FactorTagOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/factors/tags/{tag_id}")
def delete_factor_tag_endpoint(tag_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = delete_factor_tag(db, tag_id)
        clear_factor_runtime_cache()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/factors/stocks/{full_code}/tags", response_model=FactorStockTagOut)
def get_factor_stock_tags_endpoint(full_code: str, db: Session = Depends(get_db)) -> FactorStockTagOut:
    try:
        return FactorStockTagOut(**factor_stock_tags(db, full_code))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/factors/stocks/{full_code}/tags", response_model=FactorStockTagOut)
def update_factor_stock_tags_endpoint(
    full_code: str,
    payload: FactorStockTagsUpdate,
    db: Session = Depends(get_db),
) -> FactorStockTagOut:
    try:
        result = update_factor_stock_tags(db, full_code, payload.tag_ids)
        clear_factor_runtime_cache()
        return FactorStockTagOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/factors/stocks/{full_code}/status", response_model=FactorStockTagOut)
def update_factor_stock_status_endpoint(
    full_code: str,
    payload: FactorStockStatusUpdate,
    db: Session = Depends(get_db),
) -> FactorStockTagOut:
    try:
        result = update_factor_stock_status(db, full_code, payload.status, payload.review_reasons)
        clear_factor_runtime_cache()
        return FactorStockTagOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/factors/stocks/{full_code}")
def exclude_factor_stock_endpoint(full_code: str, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        result = exclude_factor_stock(db, full_code)
        clear_factor_runtime_cache()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/factors/presets", response_model=list[FactorPresetOut])
def factor_presets(db: Session = Depends(get_db)) -> list[FactorPresetOut]:
    return [FactorPresetOut(**preset) for preset in list_factor_presets(db)]


@app.post("/api/factors/presets", response_model=FactorPresetOut)
def create_factor_preset_endpoint(payload: FactorPresetCreate, db: Session = Depends(get_db)) -> FactorPresetOut:
    try:
        result = create_factor_preset(db, payload.model_dump())
        clear_factor_runtime_cache()
        return FactorPresetOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/factors/presets/{preset_id}", response_model=FactorPresetOut)
def update_factor_preset_endpoint(
    preset_id: int,
    payload: FactorPresetUpdate,
    db: Session = Depends(get_db),
) -> FactorPresetOut:
    try:
        result = update_factor_preset(db, preset_id, payload.model_dump(exclude_unset=True))
        clear_factor_runtime_cache()
        return FactorPresetOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/factors/presets/{preset_id}")
def delete_factor_preset_endpoint(preset_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        delete_factor_preset(db, preset_id)
        clear_factor_runtime_cache()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "message": "周期方案已删除"}


@app.post("/api/factors/presets/{preset_id}/activate", response_model=FactorPresetOut)
def activate_factor_preset_endpoint(preset_id: int, db: Session = Depends(get_db)) -> FactorPresetOut:
    try:
        result = activate_factor_preset(db, preset_id)
        clear_factor_runtime_cache()
        return FactorPresetOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/sector-indices", response_model=SectorIndexOverview)
def sector_indices(db: Session = Depends(get_db)) -> SectorIndexOverview:
    return SectorIndexOverview(**cached_runtime_value("sector-indices:overview", 60, lambda: sector_indices_overview(db)))


@app.post("/api/sector-indices", response_model=SectorIndexSummaryOut)
def create_sector_index_endpoint(payload: SectorIndexCreate, db: Session = Depends(get_db)) -> SectorIndexSummaryOut:
    try:
        result = SectorIndexSummaryOut(**create_sector_index(db, payload.model_dump()))
        clear_runtime_cache("sector-indices:")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sector-indices/{sector_id}", response_model=SectorIndexDetailOut)
def sector_index_detail_endpoint(sector_id: int, db: Session = Depends(get_db)) -> SectorIndexDetailOut:
    try:
        return SectorIndexDetailOut(**sector_index_detail(db, sector_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/sector-indices/{sector_id}", response_model=SectorIndexSummaryOut)
def update_sector_index_endpoint(
    sector_id: int,
    payload: SectorIndexUpdate,
    db: Session = Depends(get_db),
) -> SectorIndexSummaryOut:
    try:
        result = SectorIndexSummaryOut(**update_sector_index(db, sector_id, payload.model_dump(exclude_unset=True)))
        clear_runtime_cache("sector-indices:")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/sector-indices/{sector_id}")
def delete_sector_index_endpoint(sector_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        result = delete_sector_index(db, sector_id)
        clear_runtime_cache("sector-indices:")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/sector-indices/{sector_id}/bars", response_model=list[SectorIndexBarOut])
def sector_index_bars_endpoint(
    sector_id: int,
    range: str = "6m",  # noqa: A002
    db: Session = Depends(get_db),
) -> list[SectorIndexBarOut]:
    try:
        return [SectorIndexBarOut(**row) for row in sector_index_bars(db, sector_id, range)]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/stocks/batch-initial-review", response_model=StockResearchBatchInitialReviewOut)
def seed_initial_stock_reviews_api(db: Session = Depends(get_db)) -> StockResearchBatchInitialReviewOut:
    run = ensure_initial_research_seed(db)
    return StockResearchBatchInitialReviewOut(
        status="ok",
        message="首批个股初评已就绪。",
        run=StockResearchGenerationRunDetailOut(**get_research_run(db, run.id)),
    )


@app.get("/api/stocks/{full_code}/information-flow", response_model=StockInformationFlowResponse)
def stock_information_flow_api(full_code: str, db: Session = Depends(get_db)) -> StockInformationFlowResponse:
    try:
        return StockInformationFlowResponse(**stock_information_flow_endpoint(db, full_code))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/stocks/{full_code}/bars", response_model=list[StockBarOut])
def stock_bars_endpoint(
    full_code: str,
    range: str = "6m",  # noqa: A002
    refresh: bool = False,
    db: Session = Depends(get_db),
) -> list[StockBarOut]:
    try:
        return [StockBarOut(**row) for row in stock_bars(db, full_code, range, refresh)]
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.get("/api/stocks/{full_code}", response_model=StockDetailOut)
def stock_detail_api(full_code: str, db: Session = Depends(get_db)) -> StockDetailOut:
    try:
        return StockDetailOut(**stock_detail(db, full_code))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/stocks/{full_code}/analysis-records", response_model=StockAnalysisRecordOut)
def create_stock_analysis_record_api(
    full_code: str,
    payload: StockAnalysisRecordCreate,
    db: Session = Depends(get_db),
) -> StockAnalysisRecordOut:
    try:
        return StockAnalysisRecordOut(**create_analysis_record(db, full_code, payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sector-indices/{sector_id}/members", response_model=SectorIndexMemberOut)
def add_sector_member_endpoint(
    sector_id: int,
    payload: SectorIndexMemberCreate,
    db: Session = Depends(get_db),
) -> SectorIndexMemberOut:
    try:
        result = SectorIndexMemberOut(**add_sector_member(db, sector_id, payload.query, payload.source, payload.source_note))
        clear_runtime_cache("sector-indices:")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/sector-indices/{sector_id}/members/{full_code}")
def delete_sector_member_endpoint(sector_id: int, full_code: str, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        result = delete_sector_member(db, sector_id, full_code)
        clear_runtime_cache("sector-indices:")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sector-indices/{sector_id}/recalculate", response_model=SectorIndexRecalculateResult)
def recalculate_sector_index_endpoint(sector_id: int, db: Session = Depends(get_db)) -> SectorIndexRecalculateResult:
    try:
        result = SectorIndexRecalculateResult(**recalculate_sector_result(db, sector_id))
        clear_runtime_cache("sector-indices:")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sector-indices/{sector_id}/image-import", response_model=SectorIndexImageImportResponse)
async def sector_index_image_import_endpoint(
    sector_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SectorIndexImageImportResponse:
    try:
        image_bytes = await file.read()
        return SectorIndexImageImportResponse(
            **recognize_sector_image_candidates(db, sector_id, image_bytes, file.content_type)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/research-runs", response_model=list[StockResearchGenerationRunOut])
def list_research_runs_api(db: Session = Depends(get_db)) -> list[StockResearchGenerationRunOut]:
    return [StockResearchGenerationRunOut(**row) for row in list_research_runs(db)]


@app.post("/api/research-runs/qq-items")
def create_qq_research_items_api(
    payload: StockResearchQqItemsCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return create_qq_generation_items(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/research-runs/items/{item_id}", response_model=StockResearchGenerationItemOut)
def update_research_run_item_api(
    item_id: int,
    payload: StockResearchGenerationItemUpdate,
    db: Session = Depends(get_db),
) -> StockResearchGenerationItemOut:
    try:
        return StockResearchGenerationItemOut(**update_generation_item(db, item_id, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/research-runs/items/{item_id}/apply-tags", response_model=StockResearchGenerationItemOut)
def apply_research_run_item_tags_api(
    item_id: int,
    payload: StockResearchApplyTagsRequest,
    db: Session = Depends(get_db),
) -> StockResearchGenerationItemOut:
    try:
        result = apply_item_tags(db, item_id, payload.tag_names)
        clear_factor_runtime_cache()
        return StockResearchGenerationItemOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/research-runs/{run_id}", response_model=StockResearchGenerationRunDetailOut)
def get_research_run_api(run_id: int, db: Session = Depends(get_db)) -> StockResearchGenerationRunDetailOut:
    try:
        return StockResearchGenerationRunDetailOut(**get_research_run(db, run_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/research-runs/{run_id}")
def delete_research_run_api(
    run_id: int,
    delete_records: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return delete_research_run(db, run_id, delete_records)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/research-runs/{run_id}/apply-tags")
def apply_research_run_tags_api(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = apply_run_tags(db, run_id)
        clear_factor_runtime_cache()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/research-runs/{run_id}/mark-processed")
def mark_research_run_processed_api(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return mark_run_processed(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/judgment-assistant", response_model=JudgmentAssistantOverview)
def judgment_assistant_endpoint() -> JudgmentAssistantOverview:
    return JudgmentAssistantOverview(**judgment_assistant_overview())


@app.post("/api/judgment-assistant/records", response_model=JudgmentRecordOut)
def create_judgment_record_endpoint(payload: JudgmentRecordCreate) -> JudgmentRecordOut:
    try:
        return JudgmentRecordOut(**append_judgment_record(payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/market-review", response_model=MarketReviewOverview)
def get_market_review(db: Session = Depends(get_db)) -> MarketReviewOverview:
    return MarketReviewOverview(**market_review_overview(db))


@app.get("/api/market-review/ai-settings", response_model=MarketReviewAiSettingsOut)
def get_market_review_ai_settings_api(db: Session = Depends(get_db)) -> MarketReviewAiSettingsOut:
    get_market_review_ai_settings(db)
    return MarketReviewAiSettingsOut(**ai_settings_to_dict(db))


@app.patch("/api/market-review/ai-settings", response_model=MarketReviewAiSettingsOut)
def update_market_review_ai_settings_api(
    payload: MarketReviewAiSettingsUpdate,
    db: Session = Depends(get_db),
) -> MarketReviewAiSettingsOut:
    data = payload.model_dump(exclude_unset=True)
    update_ai_settings(db, data)
    return MarketReviewAiSettingsOut(**ai_settings_to_dict(db))


@app.get("/api/market-review/materials", response_model=list[MarketReviewMaterialOut])
def list_market_review_materials(report_date: str | None = None, db: Session = Depends(get_db)) -> list[MarketReviewMaterialOut]:
    statement = select(MarketReviewMaterial).order_by(
        MarketReviewMaterial.report_date.desc(),
        MarketReviewMaterial.created_at.desc(),
    )
    if report_date:
        statement = statement.where(MarketReviewMaterial.report_date == parse_report_date(report_date))
    return [MarketReviewMaterialOut(**material_to_dict(item)) for item in db.scalars(statement).all()]


@app.post("/api/market-review/materials", response_model=MarketReviewMaterialOut)
def create_market_review_material(
    payload: MarketReviewMaterialCreate,
    db: Session = Depends(get_db),
) -> MarketReviewMaterialOut:
    material = MarketReviewMaterial(
        report_date=payload.report_date or today_beijing(),
        source_type="manual",
        source_name=payload.source_name.strip() or "手动材料",
        market=payload.market,
        theme=payload.theme,
        title=payload.title.strip(),
        content=payload.content.strip(),
        url=payload.url,
        status="ok",
        importance=payload.importance,
    )
    if not material.title or not material.content:
        raise HTTPException(status_code=400, detail="标题和内容不能为空")
    db.add(material)
    db.commit()
    db.refresh(material)
    return MarketReviewMaterialOut(**material_to_dict(material))


@app.patch("/api/market-review/materials/{material_id}", response_model=MarketReviewMaterialOut)
def update_market_review_material(
    material_id: int,
    payload: MarketReviewMaterialUpdate,
    db: Session = Depends(get_db),
) -> MarketReviewMaterialOut:
    material = db.get(MarketReviewMaterial, material_id)
    if not material:
        raise HTTPException(status_code=404, detail="复盘材料不存在")
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(material, key, value)
    material.updated_at = now_utc()
    db.commit()
    db.refresh(material)
    return MarketReviewMaterialOut(**material_to_dict(material))


@app.delete("/api/market-review/materials/{material_id}")
def delete_market_review_material(material_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    material = db.get(MarketReviewMaterial, material_id)
    if not material:
        raise HTTPException(status_code=404, detail="复盘材料不存在")
    db.delete(material)
    db.commit()
    return {"status": "ok", "message": "复盘材料已删除。"}


@app.get("/api/market-review/universe", response_model=list[MarketReviewUniverseOut])
def list_market_review_universe(db: Session = Depends(get_db)) -> list[MarketReviewUniverseOut]:
    ensure_default_universe(db)
    items = list(
        db.scalars(
            select(MarketReviewUniverseItem).order_by(
                MarketReviewUniverseItem.market,
                MarketReviewUniverseItem.sort_order,
                MarketReviewUniverseItem.id,
            )
        )
    )
    return [MarketReviewUniverseOut(**universe_to_dict(item)) for item in items]


@app.post("/api/market-review/universe", response_model=MarketReviewUniverseOut)
def create_market_review_universe_item(
    payload: MarketReviewUniverseCreate,
    db: Session = Depends(get_db),
) -> MarketReviewUniverseOut:
    item = MarketReviewUniverseItem(
        market=payload.market,
        symbol=payload.symbol.strip().upper(),
        name=payload.name.strip(),
        theme=payload.theme,
        role=payload.role,
        enabled=payload.enabled,
        sort_order=payload.sort_order,
    )
    if not item.symbol or not item.name:
        raise HTTPException(status_code=400, detail="代码和名称不能为空")
    db.add(item)
    db.commit()
    db.refresh(item)
    return MarketReviewUniverseOut(**universe_to_dict(item))


@app.patch("/api/market-review/universe/{item_id}", response_model=MarketReviewUniverseOut)
def update_market_review_universe_item(
    item_id: int,
    payload: MarketReviewUniverseUpdate,
    db: Session = Depends(get_db),
) -> MarketReviewUniverseOut:
    item = db.get(MarketReviewUniverseItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="复盘标的不存在")
    updates = payload.model_dump(exclude_unset=True)
    if "symbol" in updates and updates["symbol"]:
        updates["symbol"] = updates["symbol"].strip().upper()
    for key, value in updates.items():
        setattr(item, key, value)
    item.updated_at = now_utc()
    db.commit()
    db.refresh(item)
    return MarketReviewUniverseOut(**universe_to_dict(item))


@app.delete("/api/market-review/universe/{item_id}")
def delete_market_review_universe_item(item_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    item = db.get(MarketReviewUniverseItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="复盘标的不存在")
    db.delete(item)
    db.commit()
    return {"status": "ok", "message": "复盘标的已删除。"}


@app.post("/api/market-review/collect/daily", response_model=MarketReviewActionResult)
def collect_market_review_daily(
    payload: MarketReviewDatePayload | None = None,
    db: Session = Depends(get_db),
) -> MarketReviewActionResult:
    if not _market_review_lock.acquire(blocking=False):
        return MarketReviewActionResult(status="partial_error", message="市场复盘任务正在运行，请稍后再试。")
    try:
        result = collect_daily_materials(db, payload.report_date if payload else None)
        return MarketReviewActionResult(**result)
    finally:
        _market_review_lock.release()


@app.post("/api/market-review/reports/daily", response_model=MarketReviewActionResult)
def create_market_review_daily_report(
    payload: MarketReviewDatePayload | None = None,
    db: Session = Depends(get_db),
) -> MarketReviewActionResult:
    if not _market_review_lock.acquire(blocking=False):
        return MarketReviewActionResult(status="partial_error", message="市场复盘任务正在运行，请稍后再试。")
    try:
        return MarketReviewActionResult(**generate_daily_report(db, payload.report_date if payload else None, collect=True))
    finally:
        _market_review_lock.release()


@app.post("/api/market-review/reports/weekly", response_model=MarketReviewActionResult)
def create_market_review_weekly_report(
    payload: MarketReviewDatePayload | None = None,
    db: Session = Depends(get_db),
) -> MarketReviewActionResult:
    if not _market_review_lock.acquire(blocking=False):
        return MarketReviewActionResult(status="partial_error", message="市场复盘任务正在运行，请稍后再试。")
    try:
        return MarketReviewActionResult(**generate_weekly_report(db, payload.report_date if payload else None))
    finally:
        _market_review_lock.release()


@app.post("/api/market-review/reports/{report_id}/ai", response_model=MarketReviewActionResult)
def generate_market_review_ai(report_id: int, db: Session = Depends(get_db)) -> MarketReviewActionResult:
    try:
        return MarketReviewActionResult(**run_ai_for_report(db, report_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/market-review/reports/{report_id}/download")
def download_market_review_report(report_id: int, db: Session = Depends(get_db)) -> FileResponse:
    report = db.get(MarketReviewReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    path = Path(report.markdown_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Markdown 文件不存在，请重新生成报告")
    return FileResponse(
        path,
        media_type="text/markdown; charset=utf-8",
        filename=path.name,
    )


@app.get("/api/exchange-announcements")
def exchange_announcements(refresh: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_exchange_announcements(force_refresh=refresh, db=db)


@app.post("/api/exchange-announcements/push")
def push_exchange_announcements_now(db: Session = Depends(get_db)) -> dict[str, Any]:
    if not _exchange_ann_push_lock.acquire(blocking=False):
        exchange_monitor_log("manual_push_skipped", reason="push_cycle_still_running", level="warning")
        return {
            "status": "manual_only",
            "message": "交易所公告推送任务正在运行，请稍后再试。",
            "pushed_count": 0,
            "logged_count": 0,
            "ignored_count": 0,
            "push_logs": [],
        }
    try:
        return push_exchange_announcements(db)
    except Exception as exc:
        db.rollback()
        exchange_monitor_log(
            "manual_push_failed",
            error=str(exc),
            level="error",
            exc_info=True,
        )
        raise
    finally:
        _exchange_ann_push_lock.release()


@app.get("/api/exchange-announcements/opportunities")
def exchange_delisting_opportunities(refresh: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    if refresh:
        if not _exchange_opportunity_scan_lock.acquire(blocking=False):
            return {
                **delisting_opportunities_overview(db),
                "status": "manual_only",
                "message": "波动机会正在刷新，先显示最近结果。",
            }
        try:
            scan_delisting_opportunities(db, push=False)
        finally:
            _exchange_opportunity_scan_lock.release()
    return delisting_opportunities_overview(db)


@app.patch("/api/exchange-announcements/opportunities/{symbol}/mute")
def update_exchange_delisting_opportunity_mute(
    symbol: str,
    muted: bool,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return set_opportunity_symbol_mute(db, symbol, muted)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete(
    "/api/exchange-announcements/opportunities/{watch_id}/pairs/{pair_key}"
)
def delete_exchange_delisting_opportunity_pair(
    watch_id: int,
    pair_key: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return delete_opportunity_pair_monitor(db, watch_id, pair_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/exchange-announcements/push-logs")
def clear_exchange_announcement_push_logs_api(db: Session = Depends(get_db)) -> dict[str, Any]:
    return clear_exchange_announcement_push_logs(db)


@app.get("/api/a-stocks/search", response_model=list[WatchlistAnnouncementStockSearchOut])
def search_a_stocks_api(keyword: str, db: Session = Depends(get_db)) -> list[WatchlistAnnouncementStockSearchOut]:
    return [WatchlistAnnouncementStockSearchOut(**row) for row in search_a_stocks(db, keyword)]


@app.get("/api/fs/borrow-check")
def fs_borrow_check(exchange: str, symbol: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return crypto_service().crypto_borrow_check_overview(exchange, symbol, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fs/borrow-search")
def fs_borrow_search(symbol: str, refresh: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return crypto_service().fs_borrow_search_overview(db, symbol, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fs/borrow-watch")
def fs_borrow_watch(db: Session = Depends(get_db)) -> dict[str, Any]:
    return crypto_service().crypto_borrow_watch_overview(db)


@app.post("/api/fs/borrow-watch")
def add_fs_borrow_watch(symbol: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return crypto_service().create_crypto_borrow_watch(db, symbol, ["bg"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/fs/borrow-watch")
def remove_fs_borrow_watch(symbol: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return crypto_service().delete_crypto_borrow_watch(db, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fs/borrow-watch/refresh")
def refresh_fs_borrow_watch(db: Session = Depends(get_db)) -> dict[str, Any]:
    crypto_service().start_crypto_borrow_watch_background_scan(force=True, push=True)
    return crypto_service().crypto_borrow_watch_overview(db)


@app.get("/api/fs/funding-cap-watch")
def fs_funding_cap_watch(db: Session = Depends(get_db)) -> dict[str, Any]:
    return crypto_service().funding_cap_watch_overview(db)


@app.get("/api/fs/funding-formation/watch")
def fs_funding_formation_watch(db: Session = Depends(get_db)) -> dict[str, Any]:
    if funding_cloud_enabled():
        try:
            return funding_cloud_request("GET", "/v1/funding-formation/watch")
        except FundingCloudError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return funding_formation_watch_overview(db)


@app.put("/api/fs/funding-formation/watch")
def sync_fs_funding_formation_watch(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail="监控组合必须是列表")
    if len(raw_items) > 25:
        raise HTTPException(status_code=400, detail="最多保存 25 个资金费监控组合")
    if funding_cloud_enabled():
        try:
            return funding_cloud_request(
                "PUT",
                "/v1/funding-formation/watch",
                payload={"items": raw_items},
            )
        except FundingCloudError as exc:
            status_code = exc.status_code if 400 <= exc.status_code < 500 else 503
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return sync_funding_formation_watches(db, raw_items)


@app.get("/api/fs/funding-formation/review")
def fs_funding_formation_review(
    days: int = 30,
    checkpoint_minutes: int = 15,
    exchange: str = "",
    symbol: str = "",
    model_version: str = "",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if funding_cloud_enabled():
        try:
            return funding_cloud_request(
                "GET",
                "/v1/funding-formation/review",
                query={
                    "days": days,
                    "checkpoint_minutes": checkpoint_minutes,
                    "exchange": exchange,
                    "symbol": symbol,
                    "model_version": model_version,
                },
            )
        except FundingCloudError as exc:
            status_code = exc.status_code if 400 <= exc.status_code < 500 else 503
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    try:
        reconcile_pending_predictions(db, crypto_service().fetch_funding_history)
        return funding_prediction_review_overview(
            db,
            days=days,
            checkpoint_minutes=checkpoint_minutes,
            exchange=exchange or None,
            symbol=symbol or None,
            model_version=model_version or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fs/funding-formation")
def fs_funding_formation(
    exchange: str,
    symbol: str,
    target_rate: float | None = None,
) -> dict[str, Any]:
    if funding_cloud_enabled():
        try:
            return funding_cloud_request(
                "GET",
                "/v1/funding-formation",
                query={"exchange": exchange, "symbol": symbol, "target_rate": target_rate},
            )
        except FundingCloudError as exc:
            status_code = exc.status_code if 400 <= exc.status_code < 500 else 503
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    try:
        return crypto_service().funding_formation_overview(
            exchange,
            symbol,
            custom_target_rate=target_rate,
        )
    except ValueError as exc:
        message = str(exc)
        if "仅支持" in message or "必须在" in message:
            raise HTTPException(status_code=400, detail=message) from exc
        raise HTTPException(
            status_code=503,
            detail=f"交易所资金费数据暂时不可用：{message}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail="交易所资金费历史暂时不可用，系统将在下一轮自动重试。",
        ) from exc


@app.post("/api/fs/funding-formation/batch")
def fs_funding_formation_batch(payload: dict[str, Any]) -> dict[str, Any]:
    if funding_cloud_enabled():
        try:
            return funding_cloud_request(
                "POST",
                "/v1/funding-formation/batch",
                payload=payload,
            )
        except FundingCloudError as exc:
            status_code = exc.status_code if 400 <= exc.status_code < 500 else 503
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="至少需要一个资金费监控组合")
    if len(raw_items) > 25:
        raise HTTPException(status_code=400, detail="单次最多读取 25 个资金费监控组合")

    requests: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"第 {index + 1} 个监控组合格式无效")
        item_id = str(raw.get("id") or f"item-{index}").strip()
        if item_id in seen_ids:
            raise HTTPException(status_code=400, detail=f"监控组合 id 重复：{item_id}")
        seen_ids.add(item_id)
        requests.append(
            {
                "id": item_id,
                "exchange": str(raw.get("exchange") or "").strip(),
                "symbol": str(raw.get("symbol") or "").strip(),
                "targetRate": raw.get("targetRate"),
            }
        )

    def load_item(item: dict[str, Any]) -> dict[str, Any]:
        try:
            target_rate = item["targetRate"]
            result = crypto_service().funding_formation_overview(
                item["exchange"],
                item["symbol"],
                custom_target_rate=(
                    float(target_rate)
                    if target_rate is not None
                    else None
                ),
            )
            return {"id": item["id"], "status": "ok", "data": result, "error": None}
        except (ValueError, TypeError, httpx.HTTPError) as exc:
            return {
                "id": item["id"],
                "status": "error",
                "data": None,
                "error": str(exc),
            }

    results_by_id: dict[str, dict[str, Any]] = {}
    worker_count = min(4, len(requests))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(load_item, item): item["id"]
            for item in requests
        }
        for future in as_completed(future_map):
            item_id = future_map[future]
            try:
                results_by_id[item_id] = future.result()
            except Exception as exc:
                results_by_id[item_id] = {
                    "id": item_id,
                    "status": "error",
                    "data": None,
                    "error": str(exc),
                }

    items = [results_by_id[item["id"]] for item in requests]
    error_count = len([item for item in items if item["status"] == "error"])
    try:
        with SessionLocal() as db:
            sync_funding_formation_watches(db, requests)
            record_prediction_batch(db, items)
    except Exception as exc:  # noqa: BLE001
        record_exception(
            exc,
            event="funding_prediction_checkpoint_write_failed",
            module="funding_prediction",
        )
    return {
        "status": "ok" if error_count == 0 else "partial_error",
        "updatedAt": datetime.now(timezone.utc),
        "itemCount": len(items),
        "errorCount": error_count,
        "items": items,
    }


@app.get("/api/fs/funding-formation/cloud-status")
def fs_funding_cloud_status() -> dict[str, Any]:
    return funding_cloud_status()


@app.post("/api/fs/funding-cap-watch")
def add_fs_funding_cap_watch(symbol: str, exchanges: str = "", db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        selected = [item.strip() for item in exchanges.split(",") if item.strip()]
        return crypto_service().create_funding_cap_watch(db, symbol, selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/fs/funding-cap-watch")
def update_fs_funding_cap_watch(symbol: str, exchanges: str = "", db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        selected = [item.strip() for item in exchanges.split(",") if item.strip()]
        return crypto_service().update_funding_cap_watch_exchanges(db, symbol, selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/fs/funding-cap-watch")
def remove_fs_funding_cap_watch(symbol: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return crypto_service().delete_funding_cap_watch(db, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fs/funding-cap-watch/refresh")
def refresh_fs_funding_cap_watch(db: Session = Depends(get_db)) -> dict[str, Any]:
    return crypto_service().refresh_funding_cap_watchlist(db, push=True)


@app.get("/api/fs/borrow-scan")
def fs_borrow_scan(exchanges: str = "bg", limit: int = 1000, db: Session = Depends(get_db)) -> dict[str, Any]:
    selected = [item.strip() for item in exchanges.split(",") if item.strip()]
    try:
        return crypto_service().crypto_borrow_scan_overview(db, selected, max(0, min(limit, 5000)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fs/signals")
def fs_signals(limit: int = 50, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        scheduler_enabled = fs_auto_scan_enabled()
        effective_limit = fs_auto_scan_limit() if scheduler_enabled else max(1, min(limit, 100))
        result = crypto_service().crypto_fs_signals_overview(
            db,
            effective_limit,
            push=False,
            allow_scan=False,
        )
        if scheduler_enabled and result.get("updatedAt") is None:
            result["scanning"] = _fs_scan_lock.locked()
            result["message"] = "等待后台首次扫描结果。"
            for check in result.get("exchangeChecks", {}).values():
                if isinstance(check, dict) and check.get("status") in {"pending", "paused", "waiting"}:
                    check["status"] = "pending" if result["scanning"] else "waiting"
                    check["message"] = "后台扫描中。" if result["scanning"] else "等待后台首次扫描。"
        elif not scheduler_enabled and not result.get("scanning"):
            result["message"] = "自动扫描已暂停，显示上轮结果。" if result.get("updatedAt") else "自动扫描已暂停，未触发新扫描。"
            for check in result.get("exchangeChecks", {}).values():
                if isinstance(check, dict) and check.get("status") in {"pending", "paused", "waiting"}:
                    check.update(status="paused", message="自动扫描已暂停。")
        # watchItems is a strict subset of items. Returning it twice doubled the
        # polling payload and forced every client to deduplicate identical rows.
        result["watchItems"] = []
        result["watchItemsIncludedInItems"] = True
        result["requestedLimit"] = max(1, min(limit, 100))
        result["effectiveLimit"] = effective_limit
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fs/astro-auto-card/status")
def fs_astro_auto_card_status(check_connection: bool = False) -> dict[str, Any]:
    config = astro_sdk_config()
    status = {**astro_auto_card_status(config), "spreadScanner": astro_spread_scanner_status()}
    if not check_connection or not config.configured:
        return status
    try:
        with AstroSdkClient(config) as client:
            pair_count = len(client.list_pairs())
    except AstroSdkError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        **status,
        "connection": "ok",
        "pairCount": pair_count,
        "message": "Astro SDK 连接及签名校验成功；新卡片默认暂停。",
    }


@app.post("/api/fs/astro-auto-card/submissions/resolve")
def resolve_astro_submission(payload: dict[str, Any]) -> dict[str, Any]:
    from app.astro_card_registry import resolve_reviewed_submission, pending_astro_submission_status
    from app.astro_sdk import AstroSdkClient, astro_sdk_config, _replace_existing_route_snapshot
    if payload.get("confirmNotExecuted") is not True:
        raise HTTPException(status_code=400, detail="只有核实未执行后才能解除锁；卡片列表缺失不算证明")
    evidence = payload.get("evidence")
    submission_id = payload.get("submissionId")
    if not isinstance(submission_id, str) or not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 1000:
        raise HTTPException(status_code=400, detail="需要提交编号和已核实未执行的证据（10–1000字）")
    target = next((item for item in pending_astro_submission_status()["items"] if item.get("submissionId") == submission_id), None)
    if not target or target.get("state") != "needs_review":
        raise HTTPException(status_code=409, detail="提交状态已变化或尚在核对，请刷新")
    try:
        # A failed read must never turn into permission to recreate a card.
        with AstroSdkClient(astro_sdk_config()) as client:
            pairs = client.list_pairs(deadline=time.monotonic() + 3)
        _replace_existing_route_snapshot(pairs)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Astro 卡片列表读取失败，保留防重复锁") from exc
    if any(all(str(pair.get(k, "")).lower() == str(target.get(k, "")).lower() for k in ("name", "type", "buyEx", "sellEx")) for pair in pairs):
        raise HTTPException(status_code=409, detail="已发现同路线卡片，不能结案为未执行；请刷新核对")
    try:
        result = resolve_reviewed_submission(submission_id, evidence)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    append_system_runtime_event("astro_submission_manually_resolved", module="astro_sdk", message="已凭证据核实提交未执行，结束该次提交", details={**result, "evidence": evidence})
    return {"result": result, "pendingSubmissions": pending_astro_submission_status()}


@app.post("/api/fs/astro-auto-card/submissions/recheck")
def recheck_astro_submission(payload: dict[str, Any]) -> dict[str, Any]:
    """Force one fresh Astro list read without treating absence as non-execution."""
    from app.astro_card_registry import pending_astro_submission_status
    from app.astro_sdk import AstroSdkClient, astro_sdk_config, _replace_existing_route_snapshot
    submission_id = payload.get("submissionId")
    if not isinstance(submission_id, str) or not submission_id.strip():
        raise HTTPException(status_code=400, detail="需要提交编号")
    before = pending_astro_submission_status()
    target = next((item for item in before["items"] if item.get("submissionId") == submission_id), None)
    if not target or target.get("state") != "needs_review":
        raise HTTPException(status_code=409, detail="提交状态已变化或尚在自动核对，请刷新")
    try:
        with AstroSdkClient(astro_sdk_config()) as client:
            pairs = client.list_pairs(deadline=time.monotonic() + 6)
        _replace_existing_route_snapshot(pairs)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Astro 卡片列表读取失败，仍保留防重复锁") from exc
    current = pending_astro_submission_status()
    still_pending = any(item.get("submissionId") == submission_id for item in current["items"])
    result = {
        "submissionId": submission_id,
        "state": "still_unknown" if still_pending else "confirmed",
        "message": "列表读取成功，但仍未找到唯一匹配卡片；继续保留防重复锁。" if still_pending else "已在 Astro 卡片列表找到同路线卡片，提交确认成功。",
    }
    append_system_runtime_event(
        "astro_submission_manual_recheck",
        module="astro_sdk",
        message=result["message"],
        details={**result, "route": [target.get(k) for k in ("name", "type", "buyEx", "sellEx")]},
    )
    return {"result": result, "pendingSubmissions": current}


@app.post("/api/fs/astro-auto-card/dex-mappings/confirm")
def confirm_fs_astro_dex_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        scanner = confirm_astro_dex_mapping(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**astro_auto_card_status(), "spreadScanner": scanner}


@app.put("/api/fs/astro-auto-card/subscriptions")
def update_fs_astro_spread_subscriptions(payload: dict[str, Any]) -> dict[str, Any]:
    markets = payload.get("markets")
    if not isinstance(markets, list):
        raise HTTPException(status_code=400, detail="markets 必须是行情源列表")
    blocked_pairs = payload.get("blockedPairs")
    if blocked_pairs is not None and not isinstance(blocked_pairs, list):
        raise HTTPException(status_code=400, detail="blockedPairs 必须是过滤列表")
    blocked_coins = payload.get("blockedCoins")
    if blocked_coins is not None and not isinstance(blocked_coins, list):
        raise HTTPException(status_code=400, detail="blockedCoins 必须是币种列表")
    dex_mapped_assets = payload.get("dexMappedAssets")
    if dex_mapped_assets is not None and not isinstance(dex_mapped_assets, list):
        raise HTTPException(status_code=400, detail="dexMappedAssets 必须是 DEX 映射列表")
    current_scanner = astro_spread_scanner_status()
    try:
        scanner = update_astro_spread_subscriptions(
            markets,
            min_volume_usdt=payload.get("minVolumeUsdt"),
            blocked_pairs=blocked_pairs,
            blocked_coins=blocked_coins,
            dex_mapped_assets=dex_mapped_assets,
            delete_rearm_pct=payload.get("deleteRearmPct"),
            delete_pullback_pct_points=payload.get("deletePullbackPctPoints"),
            ff_min_open_spread_pct=payload.get("ffMinOpenSpreadPct"),
            ff_bybit_sell_exception_enabled=payload.get("ffBybitSellExceptionEnabled"),
            sf_min_open_spread_pct=payload.get("sfMinOpenSpreadPct"),
            sf_okxdex_min_open_spread_pct=payload.get("sfOkxdexMinOpenSpreadPct"),
            sf_pancakeswap_v3_min_open_spread_pct=payload.get("sfPancakeswapV3MinOpenSpreadPct"),
            sf_min_short_funding_rate_pct=payload.get("sfMinShortFundingRatePct"),
            sf_okxdex_auto_card_enabled=payload.get("sfOkxdexAutoCardEnabled"),
            sf_pancakeswap_v3_auto_card_enabled=payload.get("sfPancakeswapV3AutoCardEnabled"),
            fs_borrow_auto_card_enabled=payload.get("fsBorrowAutoCardEnabled"),
            fs_borrow_min_cycle_profit_pct=payload.get("fsBorrowMinCycleProfitPct"),
            fs_borrow_min_open_spread_pct=payload.get("fsBorrowMinOpenSpreadPct"),
            confirmations=payload.get("confirmations"),
            max_quote_age_seconds=payload.get("maxQuoteAgeSeconds"),
            exclude_delisted_exchange_cards=payload.get("excludeDelistedExchangeCards"),
            greater_price_alert_pct=payload.get("greaterPriceAlertPct", current_scanner.get("greaterPriceAlertPct")),
            price_change_alert_pct=payload.get("priceChangeAlertPct", current_scanner.get("priceChangeAlertPct")),
            price_change_alert_only_rise=payload.get("priceChangeAlertOnlyRise"),
            min_notional_usdt=payload.get("minNotionalUsdt", current_scanner.get("minNotionalUsdt")),
            max_notional_usdt=payload.get("maxNotionalUsdt", current_scanner.get("maxNotionalUsdt")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**astro_auto_card_status(), "spreadScanner": scanner}


@app.get("/api/fs/observations/summary")
def fs_observations_summary(days: int = 7, db: Session = Depends(get_db)) -> dict[str, Any]:
    return crypto_service().fs_observation_summary(db, days)


@app.get("/api/fs/runtime-logs")
def fs_runtime_logs(
    limit: int = 200,
    level: str | None = None,
    scan_id: str | None = None,
    symbol: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return crypto_service().fs_runtime_logs_overview(db, limit, level, scan_id, symbol)


@app.get("/api/fs/pair-spread")
def fs_pair_spread(
    left: str = "SKHY",
    right: str = "SKHYNIX",
    left_exchange: str = "bg",
    right_exchange: str = "bg",
    right_ratio: float = 10.0,
    granularity: str = "auto",
    limit: int = 120,
    range_hours: float = 168.0,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return crypto_service().crypto_pair_spread_overview(
            left,
            right,
            right_ratio,
            granularity,
            limit,
            range_hours,
            left_exchange,
            right_exchange,
            db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fs/pair-spread/latest")
def fs_pair_spread_latest(
    left: str = "SKHY",
    right: str = "SKHYNIX",
    left_exchange: str = "bg",
    right_exchange: str = "bg",
    right_ratio: float = 10.0,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return crypto_service().crypto_pair_spread_latest(
            left,
            right,
            right_ratio,
            left_exchange,
            right_exchange,
            db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="交易所实时行情暂时不可用，系统会自动重试。") from exc


@app.get("/api/fs/scheduler")
def get_fs_scheduler() -> dict[str, Any]:
    return fs_scheduler_status()


@app.post("/api/fs/scheduler/start")
def start_fs_scan_scheduler() -> dict[str, Any]:
    return set_fs_scheduler_enabled(True)


@app.post("/api/fs/scheduler/pause")
def pause_fs_scan_scheduler() -> dict[str, Any]:
    return set_fs_scheduler_enabled(False)


@app.get("/api/fs/settings", response_model=CryptoSettingsOut)
def fs_settings(db: Session = Depends(get_db)) -> CryptoSettingsOut:
    return CryptoSettingsOut(**crypto_service().crypto_settings_to_out(db))


@app.post("/api/fs/symbol-mappings/scan")
def scan_fs_symbol_mappings(exchanges: str = "bn,by,gt,okx,bg,htx,as,hl", dry_run: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    selected = [item.strip() for item in exchanges.split(",") if item.strip()]
    try:
        result = crypto_service().scan_crypto_symbol_mappings(db, selected, dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not dry_run:
        db.commit()
        crypto_service().clear_crypto_board_cache()
    return result


@app.post("/api/fs/symbol-mappings", response_model=CryptoSymbolMappingOut)
def create_fs_symbol_mapping(payload: CryptoSymbolMappingCreate, db: Session = Depends(get_db)) -> CryptoSymbolMappingOut:
    try:
        mapping = crypto_service().create_crypto_symbol_mapping(
            db,
            payload.inputSymbol,
            payload.exchange,
            payload.marketType,
            payload.mappedSymbol,
            payload.priceRatio,
            payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(mapping)
    crypto_service().clear_crypto_board_cache()
    return CryptoSymbolMappingOut(**crypto_service().mapping_to_out(mapping))


@app.patch("/api/fs/symbol-mappings/{mapping_id}", response_model=CryptoSymbolMappingOut)
def update_fs_symbol_mapping(mapping_id: int, payload: CryptoSymbolMappingUpdate, db: Session = Depends(get_db)) -> CryptoSymbolMappingOut:
    updates = payload.model_dump(exclude_unset=True)
    try:
        mapping = crypto_service().update_crypto_symbol_mapping(
            db,
            mapping_id,
            input_symbol=updates.get("inputSymbol"),
            exchange=updates.get("exchange"),
            market_type=updates.get("marketType"),
            mapped_symbol=updates.get("mappedSymbol"),
            price_ratio=updates.get("priceRatio"),
            note=updates.get("note"),
            update_price_ratio="priceRatio" in updates,
            update_note="note" in updates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc
    db.commit()
    db.refresh(mapping)
    crypto_service().clear_crypto_board_cache()
    return CryptoSymbolMappingOut(**crypto_service().mapping_to_out(mapping))


@app.delete("/api/fs/symbol-mappings/{mapping_id}")
def delete_fs_symbol_mapping(mapping_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        crypto_service().delete_crypto_symbol_mapping(db, mapping_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    crypto_service().clear_crypto_board_cache()
    return {"status": "ok", "message": "币名映射已删除。"}


def network_messages_cache_key(watch_stocks: list[WatchlistAnnouncementStock]) -> str:
    payload = {
        "stocks": [(stock.exchange, stock.code, stock.name, stock.enabled) for stock in watch_stocks],
        "zsxq": {
            "configured": bool(zsxq_mcp_url()),
            "topic_limit": str(config_value("ZSXQ_GROUP_TOPIC_LIMIT") or "10"),
        },
    }
    return stable_network_id(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cached_network_messages(cache_key: str) -> NetworkMessagesOverview | None:
    now = time.time()
    with _network_messages_cache_lock:
        cached = _network_messages_cache.get("payload")
        if (
            cached
            and _network_messages_cache.get("key") == cache_key
            and now - float(_network_messages_cache.get("at") or 0.0) < _network_messages_cache_seconds
        ):
            return cached.model_copy(deep=True)
    return None


def save_network_messages_cache(cache_key: str, payload: NetworkMessagesOverview) -> None:
    with _network_messages_cache_lock:
        _network_messages_cache.update({"at": time.time(), "key": cache_key, "payload": payload.model_copy(deep=True)})


def invalidate_network_messages_cache() -> None:
    with _network_messages_cache_lock:
        _network_messages_cache.update({"at": 0.0, "key": "", "payload": None})


@app.get("/api/network-messages", response_model=NetworkMessagesOverview)
def network_messages(refresh: bool = False, db: Session = Depends(get_db)) -> NetworkMessagesOverview:
    watch_stocks = list(
        db.scalars(
            select(WatchlistAnnouncementStock)
            .where(WatchlistAnnouncementStock.enabled.is_(True))
            .order_by(WatchlistAnnouncementStock.exchange, WatchlistAnnouncementStock.code)
        )
    )
    cache_key = network_messages_cache_key(watch_stocks)
    if not refresh:
        cached = cached_network_messages(cache_key)
        if cached:
            return cached

    watch_keywords = network_watchlist_keywords(watch_stocks)
    zsxq_items, zsxq_source = fetch_zsxq_network_items(watch_keywords, has_watch_stocks=bool(watch_stocks))
    items = list(zsxq_items)
    items.sort(key=lambda item: item["created_at"], reverse=True)
    items = items[:120]

    latest_item_at = max((item["created_at"] for item in items), default=None)
    sources = [zsxq_source]
    if any(source["status"] == "ok" for source in sources):
        source_status = "ok"
    elif any(source["status"] in {"needs_authorization", "partial_error", "error"} for source in sources):
        source_status = "partial_error"
    else:
        source_status = "not_configured"
    status = "ok" if items else source_status
    overview = NetworkMessagesOverview(
        status=status,
        source_status=source_status,
        updated_at=latest_item_at,
        message=f"按本地自选股筛选星球新闻，当前展示 {len(items)} 条网络消息。",
        sources=[NetworkMessageSourceOut(**source) for source in sources],
        items=[NetworkMessageItemOut(**item) for item in items],
    )
    save_network_messages_cache(cache_key, overview)
    return overview


@app.post("/api/network-messages/crawl/zsxq", response_model=CrawlResult)
def crawl_network_messages_zsxq(db: Session = Depends(get_db)) -> CrawlResult:
    watch_stocks = list(
        db.scalars(
            select(WatchlistAnnouncementStock)
            .where(WatchlistAnnouncementStock.enabled.is_(True))
            .order_by(WatchlistAnnouncementStock.exchange, WatchlistAnnouncementStock.code)
        )
    )
    _zsxq_cache.update({"at": 0.0, "key": "", "items": [], "source": None})
    invalidate_network_messages_cache()
    items, source = fetch_zsxq_network_items(network_watchlist_keywords(watch_stocks), has_watch_stocks=bool(watch_stocks))
    return CrawlResult(
        status=source["status"],
        message=source["message"],
        matched_count=len(items),
        ignored_count=0,
    )


def fetch_zsxq_network_items(
    watch_keywords: list[tuple[str, tuple[str, ...]]],
    has_watch_stocks: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not has_watch_stocks:
        configured = bool(zsxq_mcp_url())
        return [], {
            "source_type": "zsxq",
            "source_name": "星球新闻",
            "status": "ok" if configured else "not_configured",
            "message": (
                "知识星球 MCP 已配置；请先在公告页添加本地自选股，星球新闻只展示自选股相关内容。"
                if configured
                else "网站后台未配置 ZSXQ_MCP_URL。"
            ),
            "updated_at": None,
        }

    now = time.time()
    cache_key = "|".join(label for label, _variants in watch_keywords)
    if (
        _zsxq_cache["source"]
        and _zsxq_cache.get("key") == cache_key
        and now - float(_zsxq_cache["at"]) < _zsxq_cache_seconds
    ):
        return list(_zsxq_cache["items"]), dict(_zsxq_cache["source"])

    mcp_url = zsxq_mcp_url()
    if not mcp_url:
        source = {
            "source_type": "zsxq",
            "source_name": "星球新闻",
            "status": "not_configured",
            "message": "网站后台未配置 ZSXQ_MCP_URL。",
            "updated_at": None,
        }
        return [], source

    try:
        self_info = zsxq_tool_call(mcp_url, "get_self_info", {})
        user_id = str((self_info.get("user") or {}).get("user_id") or "")
        if not user_id:
            raise ValueError("未能从知识星球 MCP 读取用户 ID")

        groups_payload = zsxq_tool_call(mcp_url, "get_user_groups", {"user_id": user_id, "scope": "all", "limit": 50})
        groups = groups_payload.get("groups") or []
        topic_limit = max(1, min(30, int(config_value("ZSXQ_GROUP_TOPIC_LIMIT") or "10")))
        items: list[dict[str, Any]] = []
        seen_topic_keys: set[str] = set()
        readable_count = 0
        blocked_count = 0
        search_hit_count = 0
        image_topic_count = 0
        image_recognized_count = 0
        image_pending_count = 0
        image_error_count = 0
        latest_at: datetime | None = None
        search_queries = []
        for label, _variants in watch_keywords:
            name = label.split(" ", 1)[0].strip()
            if name and name not in search_queries:
                search_queries.append(name)

        def append_topic(topic: dict[str, Any], group_id: str, group_name: str) -> bool:
            nonlocal image_error_count, image_pending_count, image_recognized_count, image_topic_count, latest_at
            title = clean_network_text(topic.get("title") or "")
            content = zsxq_topic_summary(topic)
            if title and content.startswith(title):
                content = content[len(title) :].strip(" ：:-")
            created_at = parse_zsxq_datetime(topic.get("create_time")) or datetime.now(timezone.utc)
            latest_at = max(latest_at, created_at) if latest_at else created_at
            topic_id = str(topic.get("topic_id") or stable_network_id(group_id, title, str(created_at)))
            topic_key = f"{group_id}:{topic_id}"
            if topic_key in seen_topic_keys:
                return False
            seen_topic_keys.add(topic_key)
            image_refs = zsxq_topic_image_refs(topic)
            image_count = len(image_refs)
            image_status: str | None = None
            image_text: str | None = None
            image_message: str | None = None
            if image_count:
                image_topic_count += 1
                image_result = recognize_zsxq_topic_images(topic_key, image_refs)
                image_status = image_result["status"]
                image_text = image_result["text"]
                image_message = image_result["message"]
                if image_status == "ok" and image_text:
                    image_recognized_count += 1
                elif image_status == "not_configured":
                    image_pending_count += 1
                else:
                    image_error_count += 1
            matched_stock_labels = match_network_watchlist_stock_labels(f"{title} {content} {image_text or ''}", watch_keywords)
            if not matched_stock_labels:
                return False
            matched_stocks = "、".join(matched_stock_labels)
            owner = topic.get("owner") or {}
            display_title = title or matched_stocks
            items.append(
                {
                    "id": f"zsxq-topic-{group_id}-{topic_id}",
                    "source_type": "zsxq",
                    "source_name": "星球新闻",
                    "message_type": group_name,
                    "matched_stocks": matched_stock_labels,
                    "author": owner.get("alias") or owner.get("name") or group_name,
                    "title": display_title[:96],
                    "content": content or title,
                    "image_count": image_count,
                    "image_status": image_status,
                    "image_text": image_text,
                    "image_message": image_message,
                    "link": zsxq_topic_link(group_id, topic_id),
                    "status": "ok",
                    "created_at": created_at,
                }
            )
            return True

        for group in groups:
            group_id = str(group.get("group_id") or "")
            group_name = str(group.get("name") or "知识星球")
            if not group_id:
                continue
            payload = zsxq_tool_call(
                mcp_url,
                "get_group_topics",
                {"group_id": group_id, "scope": "all", "limit": topic_limit},
            )
            if not payload.get("success"):
                blocked_count += 1
                continue
            readable_count += 1
            for topic in payload.get("topics_brief") or []:
                append_topic(topic, group_id, group_name)
            for query in search_queries:
                search_payload = zsxq_tool_call(mcp_url, "search_topics", {"group_id": group_id, "query": query})
                if not search_payload.get("success"):
                    continue
                before_count = len(items)
                for topic in search_payload.get("topics_brief") or []:
                    append_topic(topic, group_id, group_name)
                search_hit_count += len(items) - before_count

        status = "ok" if readable_count else "partial_error"
        search_text = f"，搜索补充 {search_hit_count} 条" if search_hit_count else ""
        image_text = ""
        if image_topic_count:
            image_parts = [f"图片主题 {image_topic_count} 条"]
            if image_recognized_count:
                image_parts.append(f"已识别 {image_recognized_count} 条")
            if image_pending_count:
                image_parts.append(f"待配置识别 {image_pending_count} 条")
            if image_error_count:
                image_parts.append(f"识别失败 {image_error_count} 条")
            image_text = f"；{', '.join(image_parts)}"
        message = f"知识星球 MCP 已接入，可读 {readable_count} 个星球，受限 {blocked_count} 个；按本地自选股筛出 {len(items)} 条{search_text}{image_text}。"
        source = {
            "source_type": "zsxq",
            "source_name": "星球新闻",
            "status": status,
            "message": message,
            "updated_at": latest_at,
        }
        _zsxq_cache.update({"at": now, "key": cache_key, "items": items, "source": source})
        return items, source
    except Exception as exc:  # noqa: BLE001
        source = {
            "source_type": "zsxq",
            "source_name": "星球新闻",
            "status": "error",
            "message": f"知识星球 MCP 采集失败：{clean_network_text(str(exc))[:300]}",
            "updated_at": None,
        }
        return [], source


def zsxq_topic_image_refs(topic: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    images = topic.get("images")
    if not isinstance(images, list):
        return refs
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        best = image.get("large") or image.get("original") or image.get("thumbnail") or {}
        if not isinstance(best, dict):
            continue
        url = str(best.get("url") or "").strip()
        if not url:
            continue
        image_id = str(image.get("image_id") or stable_network_id(url, str(index)))
        refs.append({"id": image_id, "url": url})
    return refs


def network_image_ocr_config() -> dict[str, str] | None:
    enabled = (config_value("NETWORK_IMAGE_OCR_ENABLED") or "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None
    api_key = config_value("NETWORK_IMAGE_OCR_API_KEY") or config_value("OPENAI_API_KEY")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "model": config_value("NETWORK_IMAGE_OCR_MODEL") or config_value("OPENAI_VISION_MODEL") or "gpt-4o-mini",
        "base_url": config_value("NETWORK_IMAGE_OCR_BASE_URL") or config_value("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    }


def recognize_zsxq_topic_images(topic_key: str, image_refs: list[dict[str, str]]) -> dict[str, str | None]:
    if not image_refs:
        return {"status": "skipped", "text": None, "message": None}
    cache_key = stable_network_id(topic_key, *[ref["id"] for ref in image_refs])
    cached = _network_image_ocr_cache.get(cache_key)
    if cached:
        return dict(cached)

    config = network_image_ocr_config()
    if not config:
        return {"status": "not_configured", "text": None, "message": "图片识别未配置，图片内容暂未参与自选股匹配。"}

    try:
        limit = max(1, min(5, int(config_value("NETWORK_IMAGE_OCR_IMAGES_PER_TOPIC") or "3")))
    except ValueError:
        limit = 3
    selected_refs = image_refs[:limit]
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "请识别这些投资/股票信息截图里的文字。保留股票名称、股票代码、公司名、日期、数字、百分比、金额和事件。"
                "只输出可用于检索和摘要的纯文本，不要写解释。"
            ),
        }
    ]
    for ref in selected_refs:
        content.append({"type": "image_url", "image_url": {"url": ref["url"], "detail": "high"}})

    try:
        with httpx.Client(timeout=45) as client:
            response = client.post(
                chat_completions_url(config["base_url"]),
                headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": config["model"],
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            payload = response.json()
        choices = payload.get("choices") or []
        text = clean_network_text(choices[0].get("message", {}).get("content", "")) if choices else ""
        result = {
            "status": "ok" if text else "error",
            "text": text[:2000] if text else None,
            "message": f"已识别 {len(selected_refs)} 张图片。" if text else "图片识别返回为空。",
        }
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "text": None, "message": f"图片识别失败：{clean_network_text(str(exc))[:300]}"}
    _network_image_ocr_cache[cache_key] = result
    return dict(result)


def network_watchlist_keywords(stocks: list[WatchlistAnnouncementStock]) -> list[tuple[str, tuple[str, ...]]]:
    rows: list[tuple[str, tuple[str, ...]]] = []
    for stock in stocks:
        variants = {
            stock.name,
            stock.code,
            stock.full_code.upper(),
            f"{stock.exchange}.{stock.code}".upper(),
        }
        rows.append((f"{stock.name} {stock.full_code}", tuple(value for value in variants if value)))
    return rows


def match_network_watchlist_stock_labels(text: str, watch_keywords: list[tuple[str, tuple[str, ...]]]) -> list[str]:
    normalized = text.upper()
    return [label for label, variants in watch_keywords if any(variant.upper() in normalized for variant in variants)][:6]


def match_network_watchlist_stocks(text: str, watch_keywords: list[tuple[str, tuple[str, ...]]]) -> str:
    return "、".join(match_network_watchlist_stock_labels(text, watch_keywords))


def zsxq_mcp_url() -> str | None:
    return config_value("ZSXQ_MCP_URL") or codex_zsxq_mcp_url()


def config_value(key: str) -> str | None:
    value = os.environ.get(key)
    if value:
        return value.strip()
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, raw = stripped.split("=", 1)
            if name.strip() == key:
                return raw.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def chat_completions_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def codex_zsxq_mcp_url() -> str | None:
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    for name, server in servers.items():
        if "zsxq" not in str(name).lower() or not isinstance(server, dict):
            continue
        url = str(server.get("url") or "").strip()
        if url:
            return url
    return None


def zsxq_tool_call(mcp_url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 1_000_000,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.post(
            mcp_url,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json=payload,
        )
        response.raise_for_status()
    data = parse_mcp_response(response.text)
    if "error" in data:
        raise ValueError(data["error"].get("message") or data["error"])
    result = data.get("result") or {}
    if result.get("isError"):
        content = result.get("content") or []
        text = content[0].get("text") if content else "MCP 工具返回错误"
        raise ValueError(text)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content") or []
    if content and isinstance(content[0], dict):
        text = content[0].get("text") or "{}"
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_mcp_response(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def zsxq_topic_summary(topic: dict[str, Any]) -> str:
    parts = [
        topic.get("title"),
        topic.get("text"),
        topic.get("content"),
        topic.get("summary"),
        topic.get("excerpt"),
        topic.get("description"),
    ]
    cleaned_parts: list[str] = []
    for part in parts:
        cleaned = clean_zsxq_display_text(part)
        if cleaned and cleaned not in cleaned_parts:
            cleaned_parts.append(cleaned)
    text = " ".join(cleaned_parts)
    return text[:1200]


def clean_zsxq_display_text(value: Any) -> str:
    text = str(value or "")

    def marker_title(match: re.Match[str]) -> str:
        encoded_title = match.group(1)
        return unquote(encoded_title).strip()

    text = re.sub(r"<e\b[^>]*\btitle=\"([^\"]+)\"[^>]*/?>", marker_title, text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_network_text(unquote(text))


def parse_zsxq_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def zsxq_topic_link(group_id: str, topic_id: str) -> str:
    return f"https://wx.zsxq.com/group/{group_id}/topic/{topic_id}"


def clean_network_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_network_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


@app.get("/api/fs/transfer-watch")
def fs_transfer_watch():
    from app import transfer_watch_bridge
    return transfer_watch_bridge.overview()


@app.post("/api/fs/transfer-watch/refresh")
def fs_transfer_watch_refresh():
    from app import transfer_watch_bridge
    return transfer_watch_bridge.refresh(force=True)

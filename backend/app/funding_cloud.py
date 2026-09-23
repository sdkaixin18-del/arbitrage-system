from __future__ import annotations

import os
import resource
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, func, select

from app import models  # noqa: F401
from app import transfer_watch
from app.crypto import funding_formation_cache_status, funding_formation_overview
from app.database import Base, SessionLocal, engine
from app.funding_prediction_review import (
    funding_formation_watch_overview,
    funding_prediction_review_overview,
    reconcile_pending_predictions,
    record_prediction_batch,
    start_funding_prediction_scheduler,
    stop_funding_prediction_scheduler,
    sync_funding_formation_watches,
)
from app.models import CryptoFundingFormationPredictionLog, CryptoFundingFormationWatchItem
from app.system_runtime_log import append_system_runtime_event, record_exception


APP_VERSION = "1.0.1"
PREDICTION_RETENTION_DAYS = max(
    30,
    min(int(os.environ.get("FUNDING_PREDICTION_RETENTION_DAYS", "180")), 730),
)
BATCH_WORKERS = max(1, min(int(os.environ.get("FUNDING_CLOUD_BATCH_WORKERS", "2")), 4))
_cleanup_stop = threading.Event()
_cleanup_thread: threading.Thread | None = None

app = FastAPI(title="Astro Funding Formation Cloud", version=APP_VERSION)


def _cloud_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    payload["computeLocation"] = "tencent_cloud"
    payload["cloudUpdatedAt"] = datetime.now(timezone.utc)
    return payload


def _cleanup_prediction_rows() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=PREDICTION_RETENTION_DAYS)
    with SessionLocal() as db:
        result = db.execute(
            delete(CryptoFundingFormationPredictionLog).where(
                CryptoFundingFormationPredictionLog.created_at < cutoff
            )
        )
        db.commit()
        return max(0, int(result.rowcount or 0))


def _cleanup_loop() -> None:
    while not _cleanup_stop.wait(24 * 60 * 60):
        try:
            deleted = _cleanup_prediction_rows()
            if deleted:
                append_system_runtime_event(
                    "funding_cloud_prediction_cleanup",
                    level="info",
                    module="funding_cloud",
                    message=f"腾讯云资金费预测日志清理 {deleted} 条。",
                    details={"deleted": deleted, "retentionDays": PREDICTION_RETENTION_DAYS},
                )
        except Exception as exc:  # noqa: BLE001
            record_exception(exc, event="funding_cloud_cleanup_failed", module="funding_cloud")


@app.on_event("startup")
def startup() -> None:
    global _cleanup_thread
    Base.metadata.create_all(bind=engine)
    _cleanup_prediction_rows()
    start_funding_prediction_scheduler()
    transfer_watch.start()
    _cleanup_stop.clear()
    _cleanup_thread = threading.Thread(target=_cleanup_loop, name="funding-cloud-cleanup", daemon=True)
    _cleanup_thread.start()
    append_system_runtime_event(
        "funding_cloud_started",
        level="info",
        module="funding_cloud",
        message="腾讯云资金费形成服务已启动。",
        details={
            "version": APP_VERSION,
            "batchWorkers": BATCH_WORKERS,
            "predictionRetentionDays": PREDICTION_RETENTION_DAYS,
        },
    )


@app.on_event("shutdown")
def shutdown() -> None:
    _cleanup_stop.set()
    transfer_watch.stop()
    stop_funding_prediction_scheduler()


@app.get("/health")
def health() -> dict[str, Any]:
    with SessionLocal() as db:
        watch_counts = dict(db.execute(
            select(CryptoFundingFormationWatchItem.enabled, func.count())
            .group_by(CryptoFundingFormationWatchItem.enabled)
        ).all())
        watch_count = int(watch_counts.get(True, 0))
        total_watch_count = sum(watch_counts.values())
        prediction_count = int(
            db.scalar(select(func.count()).select_from(CryptoFundingFormationPredictionLog)) or 0
        )
    peak_rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return _cloud_metadata(
        {
            "status": "ok",
            "version": APP_VERSION,
            "watchCount": watch_count,
            "totalWatchCount": total_watch_count,
            "disabledWatchCount": total_watch_count - watch_count,
            "predictionCount": prediction_count,
            "predictionRetentionDays": PREDICTION_RETENTION_DAYS,
            "batchWorkers": BATCH_WORKERS,
            "peakRssMiB": round(peak_rss_kib / 1024, 1),
            "cache": funding_formation_cache_status(),
        }
    )


@app.get("/v1/funding-formation")
def formation(exchange: str, symbol: str, target_rate: float | None = None) -> dict[str, Any]:
    try:
        return _cloud_metadata(
            funding_formation_overview(exchange, symbol, custom_target_rate=target_rate)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"交易所资金费数据暂时不可用：{exc}") from exc


def _normalize_requests(payload: dict[str, Any]) -> list[dict[str, Any]]:
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
    return requests


@app.post("/v1/funding-formation/batch")
def formation_batch(payload: dict[str, Any]) -> dict[str, Any]:
    requests = _normalize_requests(payload)

    def load_item(item: dict[str, Any]) -> dict[str, Any]:
        try:
            target_rate = item["targetRate"]
            data = funding_formation_overview(
                item["exchange"],
                item["symbol"],
                custom_target_rate=float(target_rate) if target_rate is not None else None,
            )
            return {"id": item["id"], "status": "ok", "data": data, "error": None}
        except (ValueError, TypeError, httpx.HTTPError) as exc:
            return {"id": item["id"], "status": "error", "data": None, "error": str(exc)}

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(BATCH_WORKERS, len(requests))) as executor:
        future_map = {executor.submit(load_item, item): item["id"] for item in requests}
        for future in as_completed(future_map):
            item_id = future_map[future]
            try:
                results[item_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[item_id] = {"id": item_id, "status": "error", "data": None, "error": str(exc)}

    items = [results[item["id"]] for item in requests]
    error_count = len([item for item in items if item["status"] == "error"])
    with SessionLocal() as db:
        sync_funding_formation_watches(db, requests)
        record_prediction_batch(db, items)
    return _cloud_metadata(
        {
            "status": "ok" if error_count == 0 else "partial_error",
            "updatedAt": datetime.now(timezone.utc),
            "itemCount": len(items),
            "errorCount": error_count,
            "items": items,
        }
    )


@app.get("/v1/funding-formation/watch")
def watch() -> dict[str, Any]:
    with SessionLocal() as db:
        return _cloud_metadata(funding_formation_watch_overview(db))


@app.put("/v1/funding-formation/watch")
def sync_watch(payload: dict[str, Any]) -> dict[str, Any]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail="监控组合必须是列表")
    if len(raw_items) > 25:
        raise HTTPException(status_code=400, detail="最多保存 25 个资金费监控组合")
    with SessionLocal() as db:
        return _cloud_metadata(sync_funding_formation_watches(db, raw_items))


@app.get("/v1/funding-formation/review")
def review(
    days: int = 30,
    checkpoint_minutes: int = 15,
    exchange: str = "",
    symbol: str = "",
    model_version: str = "",
) -> dict[str, Any]:
    try:
        from app.crypto import fetch_funding_history

        with SessionLocal() as db:
            reconcile_pending_predictions(db, fetch_funding_history)
            return _cloud_metadata(
                funding_prediction_review_overview(
                    db,
                    days=days,
                    checkpoint_minutes=checkpoint_minutes,
                    exchange=exchange or None,
                    symbol=symbol or None,
                    model_version=model_version or None,
                )
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/transfer-watch")
def transfer_watch_overview():
    return transfer_watch.overview()


@app.put("/v1/transfer-watch")
def transfer_watch_sync(payload: dict[str, Any]):
    try:
        return transfer_watch.sync(payload.get("items"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/transfer-watch/refresh")
def transfer_watch_refresh():
    return transfer_watch.scan()


@app.post("/v1/transfer-watch/ack")
def transfer_watch_ack(payload: dict[str, Any]):
    try:
        return transfer_watch.acknowledge(payload.get("ids"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
